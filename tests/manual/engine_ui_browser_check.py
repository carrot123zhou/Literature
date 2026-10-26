"""解析引擎管理 UI 的隔离浏览器验收服务。

起一个带隔离库/runtime 的真实 API（不调真实 MinerU、不读 Keychain 之外的密钥），
再起一个要求 Bearer 密钥的假 v1 服务（仅 /v1/health + /v1/tiers，验证「首次连接
带密钥服务」交互）。假服务是本机内存实现，与真实自建服务验收（见
docs/LOCAL_MINERU_BACKEND.md）分开。

用法：python tests/manual/engine_ui_browser_check.py
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FAKE_KEY = "test-key-123"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeV1Handler(BaseHTTPRequestHandler):
    """要求 Bearer 密钥的假 MinerU v1 服务（仅协议特征端点）。"""

    def _respond(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        auth = self.headers.get("Authorization", "")
        if self.path.startswith("/v1/health"):
            if auth != f"Bearer {FAKE_KEY}":
                self._respond({"error": {"message": "missing key"}}, status=401)
                return
            self._respond({"status": "ok", "version": "4.0.10", "features": {"output_formats": ["zip"]}})
            return
        if self.path.startswith("/v1/tiers"):
            if auth != f"Bearer {FAKE_KEY}":
                self._respond({"error": {"message": "missing key"}}, status=401)
                return
            self._respond({"object": "list", "data": [{"id": "standard", "description": "d", "current_model": "m"}]})
            return
        self._respond({"detail": "not found"}, status=404)

    def log_message(self, *args):  # noqa: N802
        pass


def prepare_dist(base: Path, api_port: int) -> Path:
    import http.server

    target = base / "dist"
    shutil.copytree(ROOT / "frontend" / "dist", target)
    for file in target.rglob("*"):
        if file.suffix in {".js", ".html", ".css"}:
            text = file.read_text(encoding="utf-8", errors="ignore")
            if "127.0.0.1:41731" in text:
                file.write_text(text.replace("127.0.0.1:41731", f"127.0.0.1:{api_port}"), encoding="utf-8")
    return target


def start_web(dist: Path, port: int) -> ThreadingHTTPServer:
    from http.server import SimpleHTTPRequestHandler

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(dist), **kwargs)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    runtime = tempfile.mkdtemp(prefix="engine-ui-runtime-")
    library = tempfile.mkdtemp(prefix="engine-ui-library-")
    os.environ["LITERATURE_RUNTIME"] = runtime
    os.environ["LITERATURE_LIBRARY"] = library
    os.environ["LITERATURE_SESSION_TOKEN"] = ""  # 生产构建前端未注入 Token，与既有浏览器验收一致
    # session_token() 在 env 为空时读文件：预置空 Token 文件（与 advanced_simplify 一致），
    # 否则会生成随机 Token，导致空头请求 401 且 401 响应不带 CORS 头。
    (Path(runtime) / "session-token").write_text("", encoding="utf-8")

    import uvicorn

    from literature.api import create_app

    app = create_app()
    api_port = free_port()
    web_port = free_port()
    fake_port = free_port()
    fake_server = ThreadingHTTPServer(("127.0.0.1", fake_port), FakeV1Handler)
    threading.Thread(target=fake_server.serve_forever, daemon=True).start()

    config = uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    import time

    deadline = time.time() + 15
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.1)

    dist = prepare_dist(Path(tempfile.mkdtemp(prefix="engine-ui-web-")), api_port)
    web = start_web(dist, web_port)

    packages = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/package")))
    nodes = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/node")))
    if not packages or not nodes:
        print(json.dumps({"checks": [], "error": "no ms-playwright-go node/package"}))
        return 2
    env = dict(os.environ)
    env["PW_PACKAGE_PATH"] = packages[-1]
    result = subprocess.run(
        [nodes[-1], str(ROOT / "tests/manual/engine_ui_check.cjs"),
         str(web_port), str(api_port), str(fake_port), str(Path(tempfile.mkdtemp(prefix="engine-ui-shots-")) / "engine-ui.png")],
        capture_output=True, text=True, timeout=400, env=env,
    )
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    print(line or json.dumps({"checks": [], "error": f"no stdout; stderr={result.stderr[-800:]}"}))
    server.should_exit = True
    fake_server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
