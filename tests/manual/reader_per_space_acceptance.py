"""逐空间（per-space）独立验收 runner：源 Key / 目标 Key / Takeaway / 源 Brief / 目标 derived Brief。

- 同一真实 ready 项目（只读复用 run），5 个场景各自全新 browser context；注释 id 独立，
  每场景只匹配/操作自己创建的笔记（按创建时间 + 选区短语双重过滤）。
- 每例记录所在 iframe 实际 __reader.lang/level 与可见正文；txt 模式原生支持真实文本
  Range + mouseup → token（不要求事先 data-id）。
- 自然 selbar 路径：真实可见选词 → 内核 selbar → POST 原生锚（iframe fetch 包装捕获）→
  宿主 PUT 信封（完整字段捕获）→ 响应/GET → 刷新同层可见精确 mark。
- 有 m / 无 m 来源不伪造：按各场景原生语义断言（brief: shown 必有、tokens/sents 按原生；
  full: tokens 摘句；derived: 独立空间）。
- 不依赖 __eaDebug。零付费/Keychain/真实库；不改产品。
"""
import glob
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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web  # noqa: E402
from tests.manual.reader_ui_browser_check import make_real_pdf  # noqa: E402
from tests.test_reader_fold_run import make_document, make_translation  # noqa: E402
from literature.storage import ProjectStore  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        os.environ["LITERATURE_RUNTIME"] = str(base / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(base / "library")
        os.environ["LITERATURE_SESSION_TOKEN"] = ""
        (base / "runtime").mkdir(parents=True, exist_ok=True)
        (base / "runtime/session-token").write_text("")
        import literature.model_services as ms
        ms.ModelServiceStore.api_key = lambda self, sid: "perspace-not-a-real-key"
        store = ProjectStore(base / "library")

        src = base / "book.pdf"
        make_real_pdf(src, pages=6)
        project = store.create_project(src, title="Silk Road Silver")
        pid = project["project_id"]
        document = make_document(pid)
        store.save_document(document)
        store.save_translation(make_translation(document))

        # 项目 B：unit takeaway 独立夹具（正文带 MARKERUNIT，provider 依此分派不同 unit/section takeaway）
        src_b = base / "book-unit.pdf"
        make_real_pdf(src_b, pages=6)
        project_b = store.create_project(src_b, title="Unit Fixture Book")
        pid_b = project_b["project_id"]
        document_b = make_document(pid_b)
        for block in document_b.blocks:
            if block.block_id == "b-p-1":
                block.source_text = ("MARKERUNIT " + block.source_text)
        store.save_document(document_b)
        store.save_translation(make_translation(document_b))

        from literature.api import create_app
        import uvicorn
        app = create_app(store)
        from tests.test_reader_fold_run import ScriptedProvider, reply_for, derive_reply_for
        from literature.providers import ProviderResponse
        import json as _json

        class UnitTakeProvider(ScriptedProvider):
            """项目 B 专用：unit takeaway 与章节 takeaway 明显不同（合法 schema）。"""
            def complete(self, *, system: str, user: str):
                import re as _re
                kind = "derive" if "source takeaway:" in user else "source"
                section = self._section_of(kind, user)
                self.calls.append((kind, section))
                if "MARKERUNIT" not in user:
                    payload = derive_reply_for(user) if kind == "derive" else reply_for(user)
                else:
                    units = [f"U{m.group(1)}" for m in
                             (_re.match(r"\[U(\d+)\]", line) for line in user.splitlines()) if m]
                    if kind == "source":
                        payload = {
                            # 章节 take（section summary 行）
                            "section": {"takeaway": "Section thesis: silver moved west along trade routes",
                                        "topic": "silver trade"},
                            "units": [
                                # unit take 与章节 take 不同，L1 才能看到真正的 unit 写行
                                {"id": uid, "evidence": [0],
                                 "takeaway": "Unit takeaway: hoards cluster in port cities",
                                 "reason": "Demand fell after the ninth century.",
                                 "topic": "silver hoards", "role": "claim"}
                                for uid in units
                            ],
                        }
                    else:
                        payload = {
                            "section": {"takeaway": "章节论点：银器沿贸易路线西传", "topic": "银器贸易"},
                            "units": [
                                {"id": uid, "evidence": [0],
                                 "takeaway": "单元要点：窖藏集中于港口城市",
                                 "reason": "依据证据句。", "topic": "银器窖藏", "role": "claim",
                                 "ev_sentences": ["这是分层译句。"]}
                                for uid in units
                            ],
                        }
                return ProviderResponse(
                    content=_json.dumps(payload, ensure_ascii=False), provider="scripted-v1",
                    model="fake-model", request_id=f"req-{len(self.calls)}",
                    input_tokens=100, output_tokens=50, estimated_cost=0.001,
                )

        provider = UnitTakeProvider()
        app.state.job_manager.provider_from = lambda *a, **k: provider
        from fastapi.testclient import TestClient
        boot = TestClient(app)
        headers = {"x-literature-token": app.state.session_token}
        created = boot.post("/model-services", json={
            "name": "perspace-fake", "preset": "custom",
            "base_url": "http://127.0.0.1:1/v1", "models": [{"model": "fake-model"}],
        }, headers=headers)
        assert created.status_code == 200, created.text
        roles = boot.put("/model-services/roles", json={
            "reader_fold": {"service_id": created.json()["service_id"], "model": "fake-model"},
        }, headers=headers)
        assert roles.status_code == 200, roles.text

        ap, wp = free_port(), free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ap, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        web = start_web(prepare_dist(base, ap), wp)

        result_path = ROOT / "tmp/reader-perspace-result.json"
        try:
            cache = str(Path.home() / "Library/Caches/ms-playwright-go")
            node = sorted(glob.glob(cache + "/*/node"))[-1]
            pkg = sorted(glob.glob(cache + "/*/package"))[-1]
            args = [node, str(ROOT / "tests/manual/reader_per_space_acceptance.cjs"),
                    str(wp), str(ap), _json.dumps([pid, pid_b]), str(result_path)]
            result = subprocess.run(args, env={**os.environ, "PW_PACKAGE_PATH": pkg},
                                    capture_output=True, text=True, timeout=420)
            (ROOT / "tmp/reader-perspace-stdout.txt").write_text(
                result.stdout + "\n--stderr--\n" + result.stderr, encoding="utf-8")
            print("stdout tail:", result.stdout[-2000:])
            if result.stderr.strip():
                print("STDERR:", result.stderr[-1200:])
            try:
                parsed = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception as error:
                parsed = {"parse_error": str(error)[:200]}
            import hashlib
            def sha(path):
                try:
                    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
                except Exception:
                    return None
            evidence = {
                "fingerprints": {
                    "reader_py": sha(ROOT / "literature/reader.py"),
                    "api_py": sha(ROOT / "literature/api.py"),
                    "adapter_py": sha(ROOT / "literature/textfold_adapter.py"),
                    "workspace_tsx": sha(ROOT / "frontend/src/reader/ReaderWorkspace.tsx"),
                    "textfold_pane": sha(ROOT / "frontend/src/reader/TextFoldPane.tsx"),
                    "engine_patched": sha(ROOT / "frontend/src/reader/textfold-engine/engine-patched.js"),
                    "dist_js": sorted((ROOT / "frontend/dist/assets").glob("index-*.js"))[0].name,
                },
                "pid": pid,
                "scenarios": parsed.get("scenarios", []),
                "assertions": parsed.get("assertions", []),
                "network_timeline": parsed.get("timeline", []),
                "provider_calls": [
                    {"kind": c[0] if isinstance(c, tuple) else c.get("kind"),
                     "section": c[1] if isinstance(c, tuple) else c.get("section")}
                    for c in provider.calls
                ],
            }
            out = ROOT / "tmp/reader-perspace-result.json"
            out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
            print("EVIDENCE:", out)
            return result.returncode
        finally:
            server.should_exit = True
            web.shutdown()


if __name__ == "__main__":
    sys.exit(main())
