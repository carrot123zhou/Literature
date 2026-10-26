"""解析来源偏好（导入页）的隔离真实浏览器验收。

背景：显式选云端后重启被本地可用性覆盖（tester 复核发现）。本验收用真实生产构建
前端 + 真实 API（隔离 runtime，本地引擎置为「已安装」），验证：
1. 无本地记录 + 本地可用 → 默认本机；
2. 显式 remote + 本地可用 → 保持云端（刷新/重新挂载后仍云端）；
3. 显式 local → 保持本机（刷新后仍本机）；
4. 显式 local 但本地不可用 → 仍选本机并显示不可用原因，不静默转云端；
5. localStorage 为非法值时按「无记录」处理（默认策略生效）。

用法：python tests/manual/parse_source_pref_browser_check.py
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web

    runtime = Path(tempfile.mkdtemp(prefix="source-pref-runtime-"))
    library = Path(tempfile.mkdtemp(prefix="source-pref-library-"))
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""
    os.environ["MINERU_API_TOKEN"] = ""
    (runtime / "session-token").write_text("", encoding="utf-8")

    # 把隔离 runtime 里的 managed 引擎置为「已安装」：local 后端 available。
    from literature import mineru_local

    engine = mineru_local.LocalMinerUEngine(runtime / "mineru-local")
    engine.kit_bin.parent.mkdir(parents=True, exist_ok=True)
    engine.kit_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    engine.models_dir.mkdir(parents=True, exist_ok=True)
    (engine.models_dir / "w.bin").write_bytes(b"w")
    engine.marker_file.write_text("mineru==4.0.10\n", encoding="utf-8")
    mineru_local.set_shared_engine(engine)
    mineru_local.set_engine_selection("managed")

    import uvicorn

    from literature.api import create_app

    app = create_app()
    api_port, web_port = free_port(), free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.1)
    dist = prepare_dist(Path(tempfile.mkdtemp(prefix="source-pref-web-")), api_port)
    start_web(dist, web_port)

    packages = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/package")))
    nodes = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/node")))
    if not packages or not nodes:
        print(json.dumps({"checks": [], "error": "no ms-playwright-go node/package"}))
        return 2
    env = dict(os.environ)
    env["PW_PACKAGE_PATH"] = packages[-1]
    env["LOCAL_ENGINE_ROOT"] = str(engine.root)
    stats = Path(tempfile.mkdtemp(prefix="source-pref-shots-"))
    result = subprocess.run(
        [nodes[-1], str(ROOT / "tests/manual/parse_source_pref_check.cjs"), str(web_port), str(stats / "pref.png")],
        capture_output=True, text=True, timeout=400, env=env,
    )
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    print(line or json.dumps({"checks": [], "error": f"no stdout; stderr={result.stderr[-800:]}"}))
    server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
