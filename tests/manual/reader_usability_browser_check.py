"""阅读器可用性验收（用户报告四项 + 真实几何）。

与 reader_ui_browser_check 同骨架（生产构建 dist + 真实 API + 隔离项目）。
四项用户报告（2026-10-25）：
U1 模型 popover 在 chat/设置内不裁切（真实四边 rect + elementFromPoint + 祖先裁剪链）
U2 设置布局自适应（320/390/768/1024/1440/短屏；长模型名不挤压思考控件；角色保存刷新匹配 API）
U3 L1-5 原件跟随原文/译文滚动；跟随开关关停后不再跟随
U4 原译 hover 视觉联动 + 点击整段出现高亮/笔记/追问 bar（无需划词）+ 点击后原件 bbox 居中
   （每次 click、跟随关闭时单次强制、同块重复 click、hover 不引发滚动、L4/5 锚点证据）

当前实现缺口会如实记录为 FAIL/DEFECT，修复后自动转绿。
用法：.venv/bin/python tests/manual/reader_usability_browser_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.manual.reader_ui_browser_check import seed_project
from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web


def main() -> int:
    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    runtime = base / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ["LITERATURE_LIBRARY"] = str(base / "library")
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""

    store, project_id = seed_project(base)
    projects = store.list_projects()
    second_id = next(item["project_id"] for item in projects if item["project_id"] != project_id)

    import uvicorn

    from literature.api import create_app
    from literature.providers import ProviderResponse

    app = create_app(store)
    manager = app.state.job_manager

    class FakeChat:
        def __init__(self, service_id: str, model: str) -> None:
            self._s, self._m = service_id, model
            self.config = type("C", (), {"model": model, "provider": service_id, "base_url": "https://a.invalid",
                                         "allow_plaintext_http": False, "reasoning_style": "", "reasoning_effort": "",
                                         "reasoning_budget": 0, "max_output_tokens": 4096, "temperature": 0.2,
                                         "input_price_per_million": 0, "output_price_per_million": 0})()

        def complete(self, *, system: str, user: str) -> ProviderResponse:
            time.sleep(0.2)
            return ProviderResponse(content=f"routed:{self._s}/{self._m} 回答 [#b-p-1]。",
                                    provider=self._s, model=self._m,
                                    request_id="r", input_tokens=10, output_tokens=5, estimated_cost=0.0)

        def close(self) -> None:
            pass

    manager.provider_from = lambda service_id, provider_input=None, model="": FakeChat(service_id, model or "model-c1")
    api_port, web_port = free_port(), free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.1)

    dist = prepare_dist(base, api_port)
    web = start_web(dist, web_port)

    import glob
    import json
    import subprocess

    packages = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/package")))
    nodes = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/node")))
    if not packages or not nodes:
        print(json.dumps({"checks": [], "error": "no ms-playwright-go node/package"}))
        return 2
    env = dict(os.environ)
    env["PW_PACKAGE_PATH"] = packages[-1]
    result = subprocess.run(
        [nodes[-1], str(ROOT / "tests/manual/reader_usability_check.cjs"),
         str(web_port), str(api_port), project_id, second_id,
         str(Path(tempfile.mkdtemp(prefix="reader-usability-")))],
        capture_output=True, text=True, timeout=600, env=env,
    )
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    print(line or json.dumps({"checks": [], "error": f"no stdout; stderr={result.stderr[-600:]}"}))
    server.should_exit = True
    web.shutdown()
    temporary.cleanup()
    ok = False
    try:
        payload = json.loads(line)
        ok = bool(payload.get("checks")) and all(item["pass"] for item in payload["checks"])
    except Exception:  # noqa: BLE001
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
