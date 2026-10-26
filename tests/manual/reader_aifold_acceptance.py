"""reader AIFold 独立验收套件（整文件自包含，不继承 designer 的碎裂 contract 脚本）。

- 生产路由 #/p/<id>/reader；2 个确定存在的隔离项目（A=生命周期/矩阵，B=notes/暂停/morph）。
- 受控 scripted provider（v1 契约，零付费/Keychain/真实库/安装）：/arm 布防下一次 complete() 阻塞、
  /release 放行——给「请求边界暂停」确定性窗口。
- 每场景独立 browser context + 独立 try；失败如实记 FAIL，不跳过冒充通过；exit code=失败数。
- 证据落 tmp/reader-aifold-acceptance.json：断言、网络时序（含逐请求 project_id 校验）、
  provider 调用序列、notes 完整 payload 与 overlay/kernel 双侧差异、morph 观测（__drPerf/
  MutationObserver/rAF/ghost/动画数/可见 token 几何/iframe 实例是否更换）。
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
import sys as _sys
if str(ROOT) not in _sys.path: _sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT))

from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web  # noqa: E402
from tests.test_reader_fold_run import make_document, make_translation  # noqa: E402
from literature.providers import ProviderResponse  # noqa: E402
from literature.storage import ProjectStore  # noqa: E402

STATE = {"armed": False, "release": False, "entered": False, "calls": []}
LOCK = threading.Lock()


class GatedProvider:
    """scripted v1：直通；armed 后下一次 complete() 阻塞至 release（只挡一次）。"""

    config = type("C", (), {"provider": "controlled", "model": "fake-model"})()

    def complete(self, *, system: str, user: str):
        from tests.test_reader_fold_run import derive_reply_for, reply_for
        import re as _re
        with LOCK:
            STATE["entered"] = True
            armed = STATE["armed"]
        kind = "derive" if "source takeaway:" in user else "source"
        match = _re.search(r"This section: (\d+)", user)
        if kind == "source" and match:
            section = f"S{match.group(1)}"
        elif "Introduction" in user:
            section = "S1"
        elif "Conclusion" in user:
            section = "S2"
        else:
            section = "?"
        with LOCK:
            STATE["calls"].append({"t": round(time.time(), 3), "kind": kind, "section": section})
        if armed:
            deadline = time.time() + 60
            while not STATE["release"] and time.time() < deadline:
                time.sleep(0.05)
            with LOCK:
                STATE["armed"] = False
        payload = derive_reply_for(user) if kind == "derive" else reply_for(user)
        return ProviderResponse(
            content=json.dumps(payload, ensure_ascii=False), provider="scripted-v1", model="fake-model",
            request_id=f"req-{len(STATE['calls'])}", input_tokens=100, output_tokens=50, estimated_cost=0.001,
        )

    def close(self) -> None:
        pass


class Control(BaseHTTPRequestHandler):
    store_ref = None

    def do_GET(self):
        with LOCK:
            if self.path == "/arm":
                STATE["armed"] = True
                STATE["release"] = False
            elif self.path == "/release":
                STATE["release"] = True
            elif self.path.startswith("/deletenative?pid="):
                pid = self.path.split("pid=", 1)[1]
                removed = 0
                try:
                    reader_dir = self.store_ref.project_path(pid) / "reader"
                    for path in reader_dir.glob("native_*.json"):
                        path.unlink()
                        removed += 1
                except Exception as error:
                    body = json.dumps({"error": str(error)[:200]}).encode()
                    self.send_response(500)
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                STATE["native_removed"] = removed
            body = json.dumps(STATE).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        os.environ["LITERATURE_RUNTIME"] = str(base / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(base / "library")
        os.environ["LITERATURE_SESSION_TOKEN"] = ""
        (base / "runtime").mkdir(parents=True, exist_ok=True)
        (base / "runtime/session-token").write_text("")
        import literature.model_services as ms
        ms.ModelServiceStore.api_key = lambda self, sid: "acceptance-not-a-real-key"
        store = ProjectStore(base / "library")

        from tests.manual.reader_ui_browser_check import make_real_pdf
        pids = []
        for index in range(2):
            src = base / f"book{index}.pdf"
            make_real_pdf(src, pages=6)
            project = store.create_project(src, title="Silk Road Silver")
            pid = project["project_id"]
            document = make_document(pid)
            store.save_document(document)
            store.save_translation(make_translation(document))
            pids.append(pid)

        from literature.api import create_app
        import uvicorn
        app = create_app(store)
        app.state.job_manager.provider_from = lambda *a, **k: GatedProvider()
        from fastapi.testclient import TestClient
        boot = TestClient(app)
        headers = {"x-literature-token": app.state.session_token}
        created = boot.post("/model-services", json={
            "name": "acceptance-fake", "preset": "custom",
            "base_url": "http://127.0.0.1:1/v1", "models": [{"model": "fake-model"}],
        }, headers=headers)
        assert created.status_code == 200, created.text
        roles = boot.put("/model-services/roles", json={
            "reader_fold": {"service_id": created.json()["service_id"], "model": "fake-model"},
        }, headers=headers)
        assert roles.status_code == 200, roles.text

        control_port = free_port()
        Control.store_ref = store
        control = ThreadingHTTPServer(("127.0.0.1", control_port), Control)
        threading.Thread(target=control.serve_forever, daemon=True).start()

        ap, wp = free_port(), free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ap, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        web = start_web(prepare_dist(base, ap), wp)

        result_path = ROOT / "tmp/reader-aifold-acceptance.json"
        try:
            cache = str(Path.home() / "Library/Caches/ms-playwright-go")
            node = sorted(glob.glob(cache + "/*/node"))[-1]
            pkg = sorted(glob.glob(cache + "/*/package"))[-1]
            args = [node, str(ROOT / "tests/manual/reader_aifold_acceptance.cjs"),
                    str(wp), str(ap), json.dumps(pids), str(control_port), str(result_path)]
            result = subprocess.run(args, env={**os.environ, "PW_PACKAGE_PATH": pkg},
                                    capture_output=True, text=True, timeout=420)
            (ROOT / "tmp/reader-aifold-acceptance-stdout.txt").write_text(
                result.stdout + "\n--stderr--\n" + result.stderr, encoding="utf-8")
            print("stdout tail:", result.stdout[-1500:])
            if result.stderr.strip():
                print("STDERR:", result.stderr[-1000:])
            try:
                parsed = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception as error:
                parsed = {"parse_error": str(error)[:200]}
            import hashlib
            def _sha(path):
                try:
                    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
                except Exception:
                    return None
            evidence = {
                "fingerprints": {
                    "reader_py": _sha(ROOT / "literature/reader.py"),
                    "api_py": _sha(ROOT / "literature/api.py"),
                    "adapter_py": _sha(ROOT / "literature/textfold_adapter.py"),
                },
                "pids": pids,
                "scenarios": parsed.get("scenarios", []),
                "assertions": parsed.get("assertions", []),
                "network_timeline": parsed.get("timeline", []),
                "provider_calls": STATE["calls"],
                "provider_entered": STATE["entered"],
            }
            try:
                import hashlib
                evidence["fingerprints"]["workspace_tsx"] = hashlib.sha256(
                    (ROOT / "frontend/src/reader/ReaderWorkspace.tsx").read_bytes()).hexdigest()
                evidence["fingerprints"]["dist_js"] = sorted(
                    (ROOT / "frontend/dist/assets").glob("index-*.js"))[0].name
            except Exception:
                pass
            result_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
            print("EVIDENCE:", result_path)
            return result.returncode
        finally:
            server.should_exit = True
            web.shutdown()
            control.shutdown()


if __name__ == "__main__":
    sys.exit(main())
