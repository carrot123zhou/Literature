"""局部失败 / 可阅读分离 真实 UI 独立验收 runner（生产 dist + 真实 API + 临时库）。

对应交付：tests/manual/reader_partial_check.cjs（Playwright 真实点击）。
覆盖场景（Lead 指定）：
  1 少量源失败 → readable 进入折叠、5 层 pending 全文+未生成、原译语言正确、补齐重试后
    ready 且成功缓存 0 新调用；
  2 仅目标派生失败 → 中文全文不回退英文；
  3 仅章节摘要失败 → unit 短层保留、resume 只补摘要（调用计数）；
  4 全部失败 → readable=false，普通全文可读、不假称 AI 就绪、不得进入折叠；
  5 过期数据 → 安全投影（旧派生短层不复活，带 #DS*-n 生成标记区分）、resume 精准补；
  6 刷新/切项目/暂停重试 → 状态从盘恢复、项目隔离、请求边界暂停；
  7 窄屏（320/390/768）无横向溢出、关键控件不堆叠。

执行边界：
- 受控 loopback OpenAI 格式 HTTP 服务注入失败（零付费）；失败/延迟经 /__control 运行时调整；
  模型调用次数以服务端 /__calls 为准（backend→service 的请求不经浏览器，Playwright 看不到）。
- 临时 LITERATURE_RUNTIME/LITERATURE_LIBRARY、空 session token；Keychain 读写全替身；
  结束时断言真实 ~/Library/Application Support/Literature 未被写入（E-iso 隔离门）。
- 不碰生产代码/现有测试；必须使用 Lead 冻结并构建后的生产 frontend/dist。
- 真实 UI 点击进入（#btn-aifold / #btn-foldstatus / #lvlbar），不允许只调接口冒充交互。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 隔离先行：必须在任何 literature 模块导入前指向临时目录（对齐 reader_user_feedback_check 模式）。
_TEMP_BASE = Path(tempfile.mkdtemp(prefix="reader-partial-ui-"))
os.environ["LITERATURE_RUNTIME"] = str(_TEMP_BASE / "runtime")
os.environ["LITERATURE_LIBRARY"] = str(_TEMP_BASE / "library")
os.environ["LITERATURE_SESSION_TOKEN"] = ""

from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web  # noqa: E402
from tests.test_reader_fold_run import derive_reply_for, reply_for  # noqa: E402
from tests.test_reader_partial_contract import make_partial_document  # noqa: E402

SECTION_BY_TITLE = (("Introduction", "S1"), ("Conclusion", "S2"), ("Epilogue", "S3"))
SECTION_BY_TITLE_ZH = (("引言", "S1"), ("结论", "S2"), ("尾声", "S3"))


def classify(user: str) -> tuple[str, str]:
    """与 PartialProvider 同一识别规则：derive / summary / source + 节 id。

    fixture 标题为中文（[译章] 引言），因此目标 lane 的 derive/summary 提示词
    含中文标题；英文标题兜底（source lane 与双语标题都兼容）。"""
    if "source takeaway:" in user:
        kind = "derive"
    elif "unit takeaways in reading order" in user or "condensed summaries of consecutive groups" in user:
        kind = "summary"
    else:
        kind = "source"
    section = "?"
    for marker, sid in SECTION_BY_TITLE + SECTION_BY_TITLE_ZH:
        if marker in user:
            section = sid
            break
    import re as _re
    match = _re.search(r"This section: (\d+)", user)
    if match:
        section = f"S{match.group(1)}"
    return kind, section


class FoldServiceHandler(BaseHTTPRequestHandler):
    """受控折叠模型服务：OpenAI /v1/chat/completions 格式 + 测试控制端点。

    - /__control（POST）：{"fail": {"source": ["S2"]}, "omit": ["S1"], "delay_ms": 0}
    - /__calls（GET）：{"calls": [{kind, section, t}], "config": {...}}
    - 目标 lane 派生/摘要回复的 section takeaway 追加生成标记 `#D{节}-{序号}`，
      供过期数据场景区分「旧短层」与「重折产物」。
    """

    lock = threading.Lock()
    config: dict = {"fail": {}, "omit": [], "delay_ms": 0}
    calls: list[dict] = []
    markers: list[dict] = []   # 每次成功回复实际写入的生成标记（seq 只在成功时递增）
    derive_seq: dict[str, int] = {}

    def log_message(self, *args):  # noqa: D102
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/__calls":
            with self.lock:
                return self._json({"calls": list(self.calls),
                                   "markers": list(self.markers),
                                   "config": dict(self.config)})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/__control":
            update = json.loads(raw or b"{}")
            with self.lock:
                self.config.update(update)
                return self._json({"ok": True, "config": dict(self.config)})
        if not self.path.endswith("/chat/completions"):
            return self._json({"error": "not found"}, 404)
        body = json.loads(raw or b"{}")
        user = ""
        for message in body.get("messages") or []:
            if message.get("role") == "user":
                user = str(message.get("content") or "")
        kind, section = classify(user)
        with self.lock:
            self.calls.append({"kind": kind, "section": section,
                               "target": "[译章]" in user, "t": time.time()})
            fail = section in (self.config.get("fail") or {}).get(kind, [])
            omit = section in (self.config.get("omit") or [])
            delay = int(self.config.get("delay_ms") or 0)
        if delay > 0:
            time.sleep(delay / 1000.0)
        if fail:
            return self._json(
                {"error": {"message": f"注入失败：{kind} {section}", "type": "injected_failure"}}, 500)
        payload = derive_reply_for(user) if kind == "derive" else reply_for(user)
        if kind != "summary" and omit:
            payload = {"units": payload.get("units") or []}
        if kind in ("derive", "summary") and "[译章]" in user and section != "?":
            raw_section = payload.get("section") if isinstance(payload.get("section"), dict) else None
            if raw_section is not None:
                with self.lock:
                    self.derive_seq[section] = int(self.derive_seq.get(section) or 0) + 1
                    seq = self.derive_seq[section]
                takeaway = str(raw_section.get("takeaway") or "")
                if takeaway and f"#D{section}-" not in takeaway:
                    raw_section["takeaway"] = f"{takeaway} #D{section}-{seq}"
                    with self.lock:
                        self.markers.append({"section": section,
                                             "marker": f"#D{section}-{seq}",
                                             "t": time.time()})
        content = json.dumps(payload, ensure_ascii=False)
        return self._json({
            "id": f"chatpl-{len(self.calls)}", "model": str(body.get("model") or "fake-model"),
            "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 60},
        })


def start_fold_service() -> tuple[ThreadingHTTPServer, int]:
    port = free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), FoldServiceHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def make_ui_translation(document):
    """UI 验收专用真实中文译文（pytest 的 [译]+英文 fixture 无法支撑语言正确性断言）。
    标题保留英文原词（[译章] Introduction）——受控服务的节识别与生成标记依赖它。"""
    from literature.models import TranslationEntry, TranslationLayer
    zh = {
        "b-p-1": "白银器皿沿丝绸之路流转。长安的作坊数十年间仿制粟特器形。九世纪之后需求衰落。",
        "b-p-2": "化验数据显示银含量稳定在百分之九十二。",
        "b-p-3": "白银窖藏从沿海贸易中消失。后来的发掘只找到旧商路的痕迹。",
    }
    titles = {"Introduction": "引言", "Conclusion": "结论", "Epilogue": "尾声"}
    entries = {}
    for block in document.blocks:
        if block.type == "paragraph":
            text = zh.get(block.block_id, f"[译] {block.source_text}")
            entries[block.block_id] = TranslationEntry(block_id=block.block_id, text=text)
        elif block.type == "title":
            entries[block.block_id] = TranslationEntry(
                block_id=block.block_id,
                text=f"[译章] {titles.get(block.source_text, block.source_text)}")
        elif block.type == "list":
            entries[block.block_id] = TranslationEntry(
                block_id=block.block_id,
                items=["窖藏集中在港口城市。", "内陆的发现渐稀。"])
    return TranslationLayer(project_id=document.project_id, locale="zh-CN", entries=entries)


def wait_ready(client, pid: str, headers: dict, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    snap: dict = {}
    while time.time() < deadline:
        snap = client.get(f"/projects/{pid}/reader/fold-run", headers=headers).json()
        if snap.get("status") in ("ready", "failed"):
            return snap
        time.sleep(0.05)
    raise AssertionError(f"预置 run 未结束：{snap.get('status')}")


def count_calls(calls: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for call in calls:
        key = f"{call.get('kind')}:{call.get('section')}"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def main() -> int:
    base = _TEMP_BASE
    (base / "runtime").mkdir(parents=True, exist_ok=True)
    (base / "runtime/session-token").write_text("")

    import literature.model_services as ms
    real_api_key, real_save = ms.ModelServiceStore.api_key, getattr(ms, "save_service_key", None)
    ms.ModelServiceStore.api_key = lambda store, sid: "partial-ui-not-a-real-key"
    if real_save is not None:
        ms.save_service_key = lambda *a, **k: None

    # 隔离门基线：真实 Application Support 的 model-services 快照（结束后必须不变）。
    app_support = Path.home() / "Library/Application Support/Literature/model-services.json"
    support_before = sha256(app_support)

    from literature.storage import ProjectStore
    store = ProjectStore(base / "library")
    pids: dict[str, str] = {}
    for title in ("partial-a-src", "partial-b-derive", "partial-c-summary",
                  "partial-d-allfail", "partial-e-stale", "partial-f-pause"):
        origin = base / f"{title}.pdf"
        origin.write_bytes(b"%PDF-1.4\n% partial ui fixture")
        pid = str(store.create_project(origin, title=title)["project_id"])
        document = make_partial_document(pid)
        store.save_document(document)
        store.save_translation(make_ui_translation(document))
        pids[title.split("-")[-1]] = pid

    service, svc_port = start_fold_service()

    from literature.api import create_app
    app = create_app(store)
    from fastapi.testclient import TestClient
    client = TestClient(app)
    headers = {"x-literature-token": app.state.session_token}
    svc = client.post("/model-services", json={
        "name": "partial-ui-fake", "preset": "custom",
        "base_url": f"http://127.0.0.1:{svc_port}/v1", "models": [{"model": "fake-model"}],
    }, headers=headers)
    assert svc.status_code == 200, svc.text
    roles = client.put("/model-services/roles", json={
        "reader_fold": {"service_id": svc.json()["service_id"], "model": "fake-model"},
    }, headers=headers)
    assert roles.status_code == 200, roles.text

    # 场景 E 预置：先折叠到 ready，再改动 S1 正文制造「过期数据」。
    run = client.post(f"/projects/{pids['stale']}/reader/fold-runs",
                      json={"locale": "zh-CN"}, headers=headers)
    assert run.status_code == 200, run.text
    ready = wait_ready(client, pids["stale"], headers)
    assert ready["status"] == "ready", ready
    document = store.load_document(pids["stale"])
    for block in document.blocks:
        if block.block_id == "b-p-1":
            block.source_text = (
                "Silver vessels travelled along the Silk Road. Workshops copied shapes "
                "for decades. Demand fell after the ninth century. New evidence emerged later on."
            )
    store.save_document(document)
    snap = client.get(f"/projects/{pids['stale']}/reader/fold-run", headers=headers).json()
    assert snap.get("status") == "failed" and not snap.get("ready"), snap

    # 事实层基线（结束后 document/translation 字节必须不变；reader 派生层除外）。
    facts_before = {
        pid: {
            rel: sha256(store.project_path(pid) / rel)
            for rel in ("document/document.json", "translations/zh-CN/translation.json")
        }
        for pid in pids.values()
    }

    import uvicorn
    ap, wp = free_port(), free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ap, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    web = start_web(prepare_dist(base, ap), wp)
    result_path = ROOT / "tmp/reader-partial-result.json"
    try:
        cache = str(Path.home() / "Library/Caches/ms-playwright-go")
        node = sorted(glob.glob(cache + "/*/node"))[-1]
        pkg = sorted(glob.glob(cache + "/*/package"))[-1]
        args = [node, str(ROOT / "tests/manual/reader_partial_check.cjs"),
                str(wp), str(ap), str(svc_port), json.dumps(pids), str(result_path),
                app.state.session_token]
        proc = subprocess.run(args, env={**os.environ, "PW_PACKAGE_PATH": pkg},
                              capture_output=True, text=True, timeout=900)
        (ROOT / "tmp/reader-partial-stdout.txt").write_text(
            proc.stdout + "\n--stderr--\n" + proc.stderr, encoding="utf-8")
        print(proc.stdout[-3000:])
        if proc.stderr.strip():
            print("STDERR:", proc.stderr[-800:])
        try:
            parsed = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            parsed = {"parse_error": True}

        assets = sorted((ROOT / "frontend/dist/assets").glob("index-*.js"))
        css_assets = sorted((ROOT / "frontend/dist/assets").glob("index-*.css"))
        engine_assets = sorted((ROOT / "frontend/dist/assets").glob("engine-patched-*.js"))
        evidence = {
            "fingerprints": {
                "served_dist_js": ({"name": assets[0].name, "sha256": sha256(assets[0])}
                                   if assets else {}),
                "served_dist_css": ({"name": css_assets[0].name, "sha256": sha256(css_assets[0])}
                                    if css_assets else {}),
                "served_dist_engine": ({"name": engine_assets[0].name, "sha256": sha256(engine_assets[0])}
                                       if engine_assets else {}),
                "note": "实际 served 的生产 dist 资产；版本归属以此为准",
            },
            "pids": pids,
            "results": parsed.get("results", parsed.get("assertions", [])),
            "failed": parsed.get("failed"),
            "screenshots": parsed.get("screenshots", []),
            "model_calls": {
                "total_kind_section_counts": count_calls(FoldServiceHandler.calls),
                "note": "全局计数；场景级差值断言由 .cjs 通过 /__calls 分段快照完成",
            },
            "isolation": {
                "app_support_model_services_unchanged": support_before == sha256(app_support),
                "facts_unchanged": {
                    pid: all(sha256(store.project_path(pid) / rel) == digest
                             for rel, digest in rels.items())
                    for pid, rels in facts_before.items()
                },
            },
        }
        out = ROOT / "tmp/reader-partial-result.json"
        out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
        print("EVIDENCE:", out)
        ok = (proc.returncode == 0
              and evidence["isolation"]["app_support_model_services_unchanged"]
              and all(evidence["isolation"]["facts_unchanged"].values()))
        return 0 if ok else 1
    finally:
        server.should_exit = True
        web.shutdown()
        service.shutdown()
        ms.ModelServiceStore.api_key = real_api_key
        if real_save is not None:
            ms.save_service_key = real_save
        import shutil
        shutil.rmtree(_TEMP_BASE, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
