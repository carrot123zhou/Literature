"""CSP 兼容最小对照：用与 Tauri 一致的 CSP 响应头提供生产 dist，验证 srcdoc 内联脚本是否被阻断。

- 两个本地 server 指向同一份 prepared dist：一个带 Tauri 同款 CSP 头，一个不带（对照）。
- 真实书 native + 受控 provider（零付费）跑一次 fold 到 ready；两页面都点「启动」。
- 记录：console 里的 Refused/securitypolicyviolation、iframe 数量、每个 iframe 的
  window.__reader / readyState / #doc 内容，主框架是否进入 fold 模式。
- 不改产品/CSP；不安装；不占鼠标；零付费。
"""
import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.manual.engine_ui_browser_check import free_port, prepare_dist  # noqa: E402
from tests.manual.reader_ui_browser_check import make_real_pdf  # noqa: E402
from tests.test_reader_fold_run import make_document, make_translation  # noqa: E402
from literature.storage import ProjectStore  # noqa: E402

TAURI_CSP = ("default-src 'self'; connect-src 'self' ipc: http://ipc.localhost http://127.0.0.1:*; "
             "img-src 'self' blob: data: http://127.0.0.1:*; style-src 'self' 'unsafe-inline'; "
             "script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; frame-src 'self' blob:")


def serve(dist: Path, port: int, csp: str | None):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(dist), **kw)

        def end_headers(self):
            if csp:
                self.send_header("Content-Security-Policy", csp)
            super().end_headers()

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        os.environ["LITERATURE_RUNTIME"] = str(base / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(base / "library")
        os.environ["LITERATURE_SESSION_TOKEN"] = ""
        (base / "runtime").mkdir(parents=True, exist_ok=True)
        (base / "runtime/session-token").write_text("")
        import literature.model_services as ms
        ms.ModelServiceStore.api_key = lambda self, sid: "csp-probe-not-a-real-key"
        store = ProjectStore(base / "library")

        src = base / "book.pdf"
        make_real_pdf(src, pages=6)
        project = store.create_project(src, title="Silk Road Silver")
        pid = project["project_id"]
        document = make_document(pid)
        store.save_document(document)
        store.save_translation(make_translation(document))

        from literature.api import create_app
        import uvicorn
        app = create_app(store)
        from tests.test_reader_fold_run import ScriptedProvider
        provider = ScriptedProvider()
        app.state.job_manager.provider_from = lambda *a, **k: provider
        from fastapi.testclient import TestClient
        boot = TestClient(app)
        headers = {"x-literature-token": app.state.session_token}
        created = boot.post("/model-services", json={
            "name": "csp-fake", "preset": "custom",
            "base_url": "http://127.0.0.1:1/v1", "models": [{"model": "fake-model"}],
        }, headers=headers)
        assert created.status_code == 200, created.text
        roles = boot.put("/model-services/roles", json={
            "reader_fold": {"service_id": created.json()["service_id"], "model": "fake-model"},
        }, headers=headers)
        assert roles.status_code == 200, roles.text
        run = boot.post(f"/projects/{pid}/reader/fold-runs", json={"locale": "zh-CN"}, headers=headers)
        assert run.status_code == 200, run.text
        for _ in range(200):
            snap = boot.get(f"/projects/{pid}/reader/fold-run", headers=headers).json()
            if snap.get("status") in ("ready", "failed"):
                break
            time.sleep(0.05)

        ap, wp = free_port(), free_port()
        wp_csp = free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ap, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        dist = prepare_dist(base, ap)
        srv_plain = serve(dist, wp, None)
        srv_csp = serve(dist, wp_csp, TAURI_CSP)
        result_path = ROOT / "tmp/reader-csp-probe-result.json"
        try:
            cache = str(Path.home() / "Library/Caches/ms-playwright-go")
            node = sorted(glob.glob(cache + "/*/node"))[-1]
            pkg = sorted(glob.glob(cache + "/*/package"))[-1]
            args = [node, str(ROOT / "tests/manual/reader_csp_probe.cjs"),
                    f"http://127.0.0.1:{wp}", f"http://127.0.0.1:{wp_csp}", pid, str(result_path)]
            proc = subprocess.run(args, env={**os.environ, "PW_PACKAGE_PATH": pkg},
                                  capture_output=True, text=True, timeout=240)
            print(proc.stdout[-3000:])
            if proc.stderr.strip():
                print("STDERR:", proc.stderr[-800:])
            parsed = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
            ev = {
                "tauri_csp": TAURI_CSP,
                "run_status": snap.get("status"),
                "result": parsed,
                "repro": "python tests/manual/reader_csp_probe.py",
            }
            out = ROOT / "tmp/reader-csp-probe-result.json"
            out.write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
            print("EVIDENCE:", out)
            return proc.returncode
        finally:
            server.should_exit = True
            srv_plain.shutdown()
            srv_csp.shutdown()


if __name__ == "__main__":
    sys.exit(main())
