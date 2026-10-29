"""冻结包新契约定向验收（fold-run / native / overlay engine_anchor）——可复用脚本。

用法（Lead 最终执行；对源码 API 或冻结 sidecar 均可）：
  # 1) 对源码 API（本地 830 基线）：
  .venv/bin/python tmp/reader_frozen_newcontract_check.py --mode source
  # 2) 对冻结 sidecar 二进制（打包后）：
  .venv/bin/python tmp/reader_frozen_newcontract_check.py --binary dist/sidecar/literature-sidecar

覆盖（全部零付费、隔离 runtime/library、不触真实 Keychain）：
  A. /health 能力位含 reader_native_v1 与 reader_fold_run_v1（缺失 = 包早于契约 → SKIP 提示重建）；
  B. GET /reader/state 含 fold_run 快照（idle）；
  C. POST /reader/fold-runs 未绑模型 → 400 且文案指向「折叠模型」；
  D. GET /reader/layers/native（无 native）可安全重建全文投影；无有效短层时 readable=false；
  E. 本地折叠（mode=local，零模型）→ native 落盘 → GET native 200 且
     units/chunks/blocks/atoms 字段齐（textfold/doc 形状）；
  F. overlay engine_anchor：由 native 派生合法锚（digest=tokenSpaceDigest 同算法）→
     保存 → 重读保留；伪造 digest → 锚失效但批注保留；
  G. 事实哈希不变（document.json / translation.json）。
"""
from __future__ import annotations

import argparse
import hashlib
import json as json_module
import threading
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def token_space_digest(toks, atoms) -> str | None:
    """与前端 tokenSpaceDigest / 后端 token_space_digest 同算法（UTF-16 码元 djb2）。"""
    def tok_plain(tk, atom_map):
        if isinstance(tk, str):
            return tk
        if not isinstance(tk, dict):
            return ""
        atom_id = tk.get("a")
        if atom_id:
            atom = atom_map.get(atom_id) or {}
            if atom.get("kind") == "math":
                return f"${atom.get('alt') or ''}$"
            return str(atom.get("text") or "")
        return f"${tk.get('x')}$" if tk.get("x") else str(tk.get("t") or "")

    def js_codes(text):
        codes = []
        for ch in text:
            code = ord(ch)
            if code > 0xFFFF:
                code -= 0x10000
                codes.extend((0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)))
            else:
                codes.append(code)
        return codes

    if not isinstance(toks, list) or not toks:
        return None
    h = 5381
    for tk in toks:
        for code in js_codes(tok_plain(tk, atoms or {})):
            h = ((h << 5) + h + code) & 0xFFFFFFFF
        h = (h + 1) & 0xFFFFFFFF
    return format(h, "08x")


class Client:
    def __init__(self, base: str, token: str):
        self.base = base
        self.token = token

    def request(self, method: str, path: str, payload=None, json=None):
        req = urllib.request.Request(self.base + path, method=method)
        req.add_header("x-literature-token", self.token)
        data = None
        if json is not None:
            data = json_module.dumps(json).encode()
            req.add_header("Content-Type", "application/json")
        elif payload is not None:
            data = payload
        try:
            with urllib.request.urlopen(req, data=data, timeout=30) as resp:
                return resp.status, json_module.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as error:
            return error.code, json_module.loads(error.read().decode() or "{}")


def find_real_book() -> Path | None:
    candidates = sorted((ROOT / "library").glob("*/document/document.json"))
    for path in candidates:
        if (path.parent.parent / "translations").is_dir():
            return path.parent.parent
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["source", "binary"], default="source")
    parser.add_argument("--binary", default=str(ROOT / "dist/sidecar/literature-sidecar"))
    # 最终验收默认**严格**：能力位缺失（旧包）= FAIL 退出 1，不误绿；
    # --diagnostic 只用于诊断旧包（显式 SKIP，退出 0），不是最终验收入口。
    parser.add_argument("--diagnostic", action="store_true",
                        help="诊断模式：能力位缺失时 SKIP 退出 0（最终验收不要用）")
    args = parser.parse_args()

    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    _base_resolved = str(base.resolve())
    runtime, library = base / "runtime", base / "library"
    runtime.mkdir()
    library.mkdir()
    book = find_real_book()
    if book is None:
        print("SKIP: library/ 没有带译文的已解析书目")
        return 2

    from literature.storage import ProjectStore

    origin = base / "sample.pdf"
    origin.write_bytes(b"%PDF-1.4\n% frozen new-contract fixture")
    store = ProjectStore(library)
    project = store.create_project(origin, title="frozen new-contract")
    project_id = str(project["project_id"])
    document_target = store.project_path(project_id) / "document" / "document.json"
    document_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(book / "document" / "document.json", document_target)
    translation_source = book / "translations" / "zh-CN" / "translation.json"
    if translation_source.is_file():
        layer = store.translation_path(project_id, "zh-CN")
        layer.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(translation_source, layer)
    before = {str(p): sha256(p) for p in (book / "document" / "document.json", translation_source) if p.is_file()}

    port = 47961
    # 旧残留判断：端口被占时只清理本脚本的 sidecar 残留（可执行名匹配且库路径在临时目录），
    # 不碰 launcher/真实 app；其它进程占用则直接报错退出。
    _stale = subprocess.run(["lsof", "-nP", "-i", f":{port}"], capture_output=True, text=True)
    for line in (_stale.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or "LISTEN" not in line:
            continue
        pid_s = parts[1]
        if not pid_s.isdigit():
            continue
        cmd = subprocess.run(["ps", "-p", pid_s, "-o", "command="], capture_output=True, text=True).stdout.strip()
        cwd = subprocess.run(["lsof", "-p", pid_s, "-o", "-Fn"], capture_output=True, text=True).stdout
        cmd_cwd = next((ln[1:] for ln in cwd.splitlines() if ln.startswith("n/")), "")
        if "literature-sidecar" in cmd and ("/T/" in cmd_cwd or "temp" in cmd_cwd.lower() or "var/folders" in cmd_cwd):
            print(f"  清理旧残留 sidecar pid={pid_s} cwd={cmd_cwd}")
            subprocess.run(["kill", pid_s])
            time.sleep(1.0)
        else:
            print(f"FAIL: 端口 {port} 被非本脚本进程占用（cmd={cmd[:80]} cwd={cmd_cwd}），不清理")
            return 2
    env = dict(os.environ)
    env.update({
        "LITERATURE_LIBRARY": str(library),
        "LITERATURE_RUNTIME": str(runtime),
        "LITERATURE_SESSION_TOKEN": "frozen-newcontract",
        "LITERATURE_PORT": str(port),
        "LITERATURE_OPENAI_API_KEY": "frozen-fake-key",
    })
    # in-process patch：get_api_key 返回假 key（受控测试，零 Keychain 读写）——仅源模式有效
    if args.mode == "binary":
        binary = Path(args.binary)
        if not binary.is_file():
            print("SKIP: 冻结 sidecar 不存在：", binary)
            return 2
        process = subprocess.Popen([str(binary)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        main._sidecar_process = process  # 供 __main__ finally 清理（仅本次 pid）
    else:
        import literature.providers as _prov
        import literature.model_services as _ms
        _prov.get_api_key = lambda provider: "frozen-not-a-real-key"
        _ms.get_api_key = lambda provider: "frozen-not-a-real-key"
        _ms.ModelServiceStore.api_key = lambda self, sid: "frozen-not-a-real-key"
        # 源模式：in-process 启动 API（subprocess 无法传递 patch）。
        # 隔离关键：必须写 os.environ 本身——env 副本只对 subprocess 有效；漏掉这步时
        # create_app 内 ModelServiceStore(application_support()) 会指向真实
        # ~/Library/Application Support/Literature，把 frozen-fake 服务和角色写进用户配置
        # （真实故障：27 个残留服务 + reader_fold 角色被覆盖）。恢复注册在 atexit，
        # 覆盖整个脚本生命周期（含 lifespan/请求期内的懒加载）。
        import atexit
        _saved_env = {k: os.environ.get(k) for k in env if k.startswith("LITERATURE_")}

        def _restore_literature_env() -> None:
            for k, v in _saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        atexit.register(_restore_literature_env)
        os.environ.update({k: v for k, v in env.items() if k.startswith("LITERATURE_")})
        import uvicorn
        from literature.api import create_app as _create_app
        literature_api_app = _create_app(store)
        api_config = uvicorn.Config(literature_api_app, host="127.0.0.1", port=port, log_level="error")
        api_server = uvicorn.Server(api_config)
        threading.Thread(target=api_server.run, daemon=True).start()
        process = None
    literature_api_app = locals().get("literature_api_app")
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            import urllib.request
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            urllib.request.urlopen(req, timeout=2)
            break
        except Exception:
            time.sleep(0.2)
    _token = literature_api_app.state.session_token if literature_api_app else "frozen-newcontract"
    client = Client(f"http://127.0.0.1:{port}", _token)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f"（{detail}）" if detail and not ok else ""))

    # 等 /health
    deadline = time.time() + 30
    health = {}
    while time.time() < deadline:
        try:
            status, health = client.request("GET", "/health")
            if status == 200:
                break
        except Exception:
            pass
        time.sleep(0.2)
    capabilities = set(health.get("capabilities") or [])
    has_new = {"reader_native_v1", "reader_fold_run_v1"} <= capabilities
    check("A. 能力位含 reader_native_v1/reader_fold_run_v1", has_new,
          "缺失 = 包早于新契约，需重建 sidecar 后重跑")
    if not has_new:
        if args.diagnostic:
            print("SKIP(诊断模式): 当前包不含新契约（能力位缺失）；请重建后重跑")
            return 0
        print("FAIL: 当前包不含新契约（能力位缺失）——最终验收不通过；请重建 sidecar 后重跑")
        return 1

    status, state = client.request("GET", f"/projects/{project_id}/reader/state")
    check("B1. GET /reader/state 200", status == 200)
    check("B2. state.fold_run 快照存在且 idle", (state.get("fold_run") or {}).get("status") == "idle")

    # 先暂停/停止已有 run（前一次运行可能残留）
    _, existing = client.request("GET", f"/projects/{project_id}/reader/fold-run")
    if existing.get("status") in ("running", "pausing"):
        rid = existing.get("run_id", "")
        if rid:
            client.request("POST", f"/projects/{project_id}/reader/fold-runs/{rid}/pause")
    _, fr_snap = client.request("GET", f"/projects/{project_id}/reader/fold-run")
    check("C. GET /fold-run 端点可达", "status" in fr_snap, str(fr_snap)[:100])
    check("C1. GET /fold-run 返回快照", "status" in fr_snap, str(fr_snap)[:100])

    # ---- E/F: fold-run + fake model server → native → engine_anchor → digest 失效 ----
    # fake model HTTP server（受控 provider，零付费）
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import re as _re

    # fake model HTTP server（OpenAI chat completions 格式，复用 tests 回复 helper）
    from tests.test_reader_fold_run import reply_for as _reply_for, derive_reply_for as _derive_reply_for
    import re as _re

    class FakeModelHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            req = json_module.loads(body)
            messages = req.get("messages") or []
            user_content = ""
            for msg in messages:
                if msg.get("role") == "user":
                    user_content = msg.get("content") or ""
                    break
            is_derive = "source takeaway:" in user_content
            # 提取 section id（从标题行）
            title_match = _re.search(r"This section: (\d+)", user_content)
            if title_match:
                section_num = title_match.group(1)
            elif "Introduction" in user_content:
                section_num = "1"
            elif "Conclusion" in user_content:
                section_num = "2"
            else:
                section_num = "1"
            if is_derive:
                payload = _derive_reply_for(user_content)
            else:
                payload = _reply_for(user_content)
            content = json_module.dumps(payload, ensure_ascii=False)
            resp = {
                "id": "chatcmpl-frozen",
                "object": "chat.completion",
                "choices": [{"index": 0,
                             "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }
            out = json_module.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        def log_message(self, *a): pass

    model_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeModelHandler)
    model_port = model_srv.server_address[1]
    threading.Thread(target=model_srv.serve_forever, daemon=True).start()
    # 创建 model service + 角色绑定（通过 API；preset=openai 使 provider 用 openai 兼容路径）
    # 隔离门：model-services 必须落在临时 runtime（LITERATURE_RUNTIME）内；历史上隔离缺失时
    # 本脚本向真实 ~/Library/Application Support/Literature/model-services.json 写入了 27 个
    # frozen-fake 服务并覆盖 reader_fold 角色（真实故障：Imperiled Destinies run 冻结假绑定）。
    resources_status, resources = client.request("GET", "/desktop/resources")
    actual_runtime = str(resources.get("runtime") or "") if resources_status == 200 else ""
    _ms_path = Path(actual_runtime) / "model-services.json"
    _ms_isolated = bool(actual_runtime) and _ms_path.resolve().is_relative_to(Path(_base_resolved))
    if args.mode == "source":
        from literature.api import application_support as _app_support
        _ms_isolated = _ms_isolated and _app_support().resolve().is_relative_to(Path(_base_resolved))
    check("E-iso. model-services 隔离在临时 runtime", _ms_isolated, str(_ms_path))
    if not _ms_isolated:
        return 1
    status, svc = client.request("POST", "/model-services", json={
        "name": "frozen-fake", "preset": "openai",
        "base_url": f"http://127.0.0.1:{model_port}/v1",
        "models": [{"model": "fake-model"}]})
    check("E0. model-services 创建", status == 200, f"{status}")
    svc_id = (svc or {}).get("service_id", "")
    # API key：binary 模式通过 env 预设；源模式通过 in-process patch
    if args.mode == "binary":
        pass  # env 已在启动前设置
    else:
        os.environ[f"LITERATURE_{svc_id.upper()}_API_KEY"] = "frozen-fake-key"
    client.request("PUT", "/model-services/roles",
                   json={"reader_fold": {"service_id": svc_id, "model": "fake-model"}})
    status, body = client.request("POST", f"/projects/{project_id}/reader/fold-runs",
                                  json={"locale": "zh-CN"})
    check("E1. fold-run 受理", status == 200, f"{status} {str(body)[:100]}")
    run_id = (body or {}).get("run_id", "")
    fr = {}
    if status == 200 and run_id:
        deadline = time.time() + 60
        while time.time() < deadline:
            _, fr = client.request("GET", f"/projects/{project_id}/reader/fold-run")
            if fr.get("status") in ("ready", "failed"):
                break
            time.sleep(0.3)
    check("E2. fold-run ready", fr.get("status") == "ready",
          f"status={fr.get('status')} basis={fr.get('ready_basis','')[:150]} errors={fr.get('errors', [])[:2]}")

    # native GET
    status, native = client.request("GET", f"/projects/{project_id}/reader/layers/native?locale=zh-CN")
    check("E3. GET native 200 且 textfold/doc 形状",
          status == 200 and native.get("format") == "textfold/doc",
          f"status={status} keys={sorted(native.keys())[:8] if isinstance(native, dict) else 'n/a'}")
    units = (native or {}).get("units") or {}
    sample_uid = next((uid for uid, unit in units.items()
                       if (((unit.get("tr") or {}).get("zh-CN") or {}).get("lad") or {}).get("brief")), None)
    check("E4. 存在带目标写行的 unit", sample_uid is not None,
          f"sample={sample_uid} total={len(units)}")    # F. overlay engine_anchor
    if sample_uid:
        unit = units[sample_uid]
        tr = (unit.get("tr") or {}).get("zh-CN") or {}
        brief = ((tr.get("lad") or {}).get("brief")) or []
        atoms = (native or {}).get("atoms") or {}
        hi = min(3, len(brief) - 1)
        digest = token_space_digest(brief, atoms)
        exact = "".join(
            (tk.get("t") or "") + (" " if isinstance(tk, dict) and tk.get("s") and i < hi else "")
            for i, tk in enumerate(brief[:hi + 1])
        )
        anchor = {
            "version": 1, "locale": "zh-CN", "space": "brief", "digest": digest,
            "anchor": {"on": "unit", "unit": sample_uid, "lang": "zh-CN", "level": 2,
                       "shown": {"words": [0, hi], "exact": exact},
                       "quote": {"exact": exact, "prefix": "", "suffix": ""}},
        }
        _, overlay = client.request("GET", f"/projects/{project_id}/reader/overlay")
        revision = overlay.get("revision")
        annotation = {"id": "ea-frozen", "anchor": "block", "block_id": sample_uid, "pane": "trans",
                      "start": 0, "end": 0, "color": "y", "note": "冻结验收", "detached": False,
                      "engine_anchor": anchor}
        status, saved = client.request("PUT", f"/projects/{project_id}/reader/overlay",
                                       json={"overlay": {**overlay, "annotations": [annotation]},
                                             "base_revision": revision})
        kept = (saved.get("annotations") or [{}])[0].get("engine_anchor")
        check("F1. 带 engine_anchor 的 overlay 保存 200", status == 200, f"{status}")
        check("F2. 锚完整保留（digest/space/snapshot）",
              kept is not None and kept.get("digest") == digest and kept.get("space") == "brief",
              str(kept)[:160])
        bad = {**annotation, "id": "ea-bad",
               "engine_anchor": {**anchor, "digest": "deadbeef" if digest != "deadbeef" else "12345678"}}
        _, overlay2 = client.request("GET", f"/projects/{project_id}/reader/overlay")
        status, saved2 = client.request("PUT", f"/projects/{project_id}/reader/overlay",
                                        json={"overlay": {**overlay2, "annotations": [bad]},
                                              "base_revision": overlay2.get("revision")})
        bad_kept = (saved2.get("annotations") or [{}])[0]
        check("F3. 伪造 digest → 锚失效、批注保留",
              status == 200 and bad_kept.get("id") == "ea-bad"
              and "engine_anchor" not in bad_kept, str(bad_kept)[:160])

    # ---- F4/F5: 自然章节摘要锚（on=summary，lad.take，locale=source native） ----
    _, native_src = client.request("GET", f"/projects/{project_id}/reader/layers/native?locale=source")
    src_atoms = (native_src or {}).get("atoms") or {}
    src_chunks = (native_src or {}).get("chunks") or []
    src_lang = str(((native_src or {}).get("meta") or {}).get("lang") or "source")
    src_chunk = next((c for c in src_chunks
                      if ((c.get("lad") or {}).get("take")) and c.get("block_id")), None)
    if src_chunk is None:
        check("F4. 自然 summary 锚保存读回", False, "native_source 无带 take 的 chunk")
        check("F5. summary 伪造 digest 锚失效笔记保留", False, "无 take chunk")
        check("F6. title 锚合法存续", False, "无 chunk")
    else:
        take = (src_chunk.get("lad") or {}).get("take") or []
        block_id = src_chunk.get("block_id") or ""
        section_id = src_chunk.get("id") or ""
        def _plain(toks, a, b):
            out = ""
            for idx in range(a, min(b, len(toks) - 1) + 1):
                tk = toks[idx]
                if isinstance(tk, dict) and tk.get("a"):
                    continue
                out += (tk.get("t") or "") if isinstance(tk, dict) else str(tk)
                if isinstance(tk, dict) and tk.get("s") and idx < b:
                    out += " "
            return out
        exact = _plain(take, 0, 2)
        sum_anchor = {
            "version": 1, "locale": src_lang, "space": "summary",
            "digest": token_space_digest(take, src_atoms),
            "anchor": {"on": "summary", "section": section_id, "lang": src_lang, "level": 1,
                       "tokens": [0, 2],
                       "quote": {"exact": exact, "prefix": "", "suffix": ""}},
        }
        ann_sum = {"id": "ea-sum", "anchor": "block", "block_id": block_id, "pane": "source",
                   "start": 0, "end": 0, "color": "y", "note": "章节摘要笔记", "detached": False,
                   "engine_anchor": sum_anchor}
        _, ov3 = client.request("GET", f"/projects/{project_id}/reader/overlay")
        status, saved3 = client.request("PUT", f"/projects/{project_id}/reader/overlay",
                                        json={"overlay": {**ov3, "annotations": [ann_sum]},
                                              "base_revision": ov3.get("revision")})
        kept3 = (saved3.get("annotations") or [{}])[0]
        check("F4. 自然 summary 锚保存读回", status == 200 and kept3.get("engine_anchor") == sum_anchor,
              f"status={status} kept={str(kept3)[:140]}")
        bad_sum = {**ann_sum, "id": "ea-sum-bad",
                   "engine_anchor": {**sum_anchor, "digest": "deadbeef" if sum_anchor["digest"] != "deadbeef" else "12345678"}}
        _, ov4 = client.request("GET", f"/projects/{project_id}/reader/overlay")
        status, saved4 = client.request("PUT", f"/projects/{project_id}/reader/overlay",
                                        json={"overlay": {**ov4, "annotations": [bad_sum]},
                                              "base_revision": ov4.get("revision")})
        kept4 = (saved4.get("annotations") or [{}])[0]
        check("F5. summary 伪造 digest 锚失效笔记保留",
              status == 200 and "engine_anchor" not in kept4 and kept4.get("note") == "章节摘要笔记",
              str(kept4)[:140])

        # ---- F6: title 空间（无 tokens；digest 基于 [{t: title}]） ----
        title = src_chunk.get("title") or ""
        title_anchor = {
            "version": 1, "locale": src_lang, "space": "title",
            "digest": token_space_digest([{"t": title}], {}),
            "anchor": {"on": "title", "section": section_id, "lang": src_lang, "level": 4,
                       "quote": {"exact": title[:6], "prefix": "", "suffix": ""}},
        }
        ann_title = {"id": "ea-title", "anchor": "block", "block_id": block_id, "pane": "source",
                     "start": 0, "end": 0, "color": "y", "note": "标题笔记", "detached": False,
                     "engine_anchor": title_anchor}
        _, ov5 = client.request("GET", f"/projects/{project_id}/reader/overlay")
        status, saved5 = client.request("PUT", f"/projects/{project_id}/reader/overlay",
                                        json={"overlay": {**ov5, "annotations": [ann_title]},
                                              "base_revision": ov5.get("revision")})
        kept5 = (saved5.get("annotations") or [{}])[0]
        check("F6. title 锚合法存续", status == 200 and kept5.get("engine_anchor") == title_anchor,
              f"status={status} kept={str(kept5)[:140]}")

    # ---- F7: 旧 unit 锚（无 engine_anchor）原样读回 ----
    _, ov6 = client.request("GET", f"/projects/{project_id}/reader/overlay")
    _unit_ids = sorted(((native_src or {}).get("units") or {}).keys())
    _legacy_bid = _unit_ids[0] if _unit_ids else "blk-00000001"
    ann_legacy = {"id": "ea-legacy", "anchor": "block", "block_id": _legacy_bid, "pane": "source",
                  "start": 0, "end": 0, "color": "y", "note": "旧批注保留", "detached": False}
    status, saved6 = client.request("PUT", f"/projects/{project_id}/reader/overlay",
                                    json={"overlay": {**ov6, "annotations": [ann_legacy]},
                                          "base_revision": ov6.get("revision")})
    kept6 = (saved6.get("annotations") or [{}])[0]
    check("F7. 旧 unit 锚（无 engine_anchor）原样读回",
          status == 200 and kept6.get("id") == "ea-legacy" and "engine_anchor" not in kept6,
          str(kept6)[:140])

    # ---- H: mode=local 422 + provider None 不建 job 不写文件 ----
    status, body = client.request("POST", f"/projects/{project_id}/reader/layers",
                                  json={"locale": "zh-CN", "mode": "local"})
    check("H1. mode=local → 422", status == 422, f"status={status} {str(body)[:100]}")

    # provider None（无角色绑定）：新 project 上 POST fold-runs → 400，不创建 run 文件
    from literature.storage import ProjectStore as _PS
    _store2 = _PS(library)
    _proj2 = _store2.create_project(origin, title="frozen-nobind")
    pid2 = str(_proj2["project_id"])
    status2, run2 = client.request("POST", f"/projects/{pid2}/reader/fold-runs", json={})
    check("H2a. 无绑定 fold-runs → 400", status2 == 400, f"status={status2} {str(run2)[:100]}")
    _, snap2 = client.request("GET", f"/projects/{pid2}/reader/fold-run")
    check("H2b. 无绑定 fold-run 快照 idle 且无 run_id",
          snap2.get("status") == "idle" and not snap2.get("run_id"), str(snap2)[:120])
    check("H2c. 无绑定不写 fold_run.json",
          not (library / pid2 / "reader" / "fold_run.json").is_file(),
          f"exists={(library / pid2 / 'reader' / 'fold_run.json').is_file()}")

    after = {str(p): sha256(p) for p in (book / "document" / "document.json", translation_source) if p.is_file()}
    check("G. 事实哈希不变", before == after)

    failed = [name for name, ok, _ in checks if not ok]
    print(f"\n{'PASS' if not failed else 'FAIL'}: {len(checks) - len(failed)}/{len(checks)}")
    return 0 if not failed else 1


def _cleanup(process):  # noqa: ANN001 - 清理本脚本启动的 sidecar，仅限准确 pid
    if process is not None and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    _exit_code = 2
    _proc = None
    try:
        _exit_code = main()
    finally:
        # main() 内的 process 无法直接取出；用模块级变量回传
        _proc = getattr(main, "_sidecar_process", None)
        _cleanup(_proc)
    raise SystemExit(_exit_code)
