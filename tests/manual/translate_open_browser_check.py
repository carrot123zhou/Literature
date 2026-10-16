"""真实浏览器验收（隔离）：文件资源「开始/继续翻译」确认后自动打开该项目（对照翻译页）。

在本地终端运行：
    python3 tests/manual/translate_open_browser_check.py

不联网、不读 Keychain：模型服务用 MockProvider 在本地完成翻译作业。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from tests.manual.parse_ui_browser_check import free_port, node_env, prepare_dist, run_node, start_web

    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    library, runtime = base / "library", base / "runtime"
    library.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""
    os.environ["MINERU_API_TOKEN"] = "fake-ui-token"

    import httpx
    _original_send = httpx.Client.send

    def _guarded_send(self, request, *args, **kwargs):
        host = request.url.host
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError(f"acceptance blocked non-local network: {request.url}")
        return _original_send(self, request, *args, **kwargs)

    httpx.Client.send = _guarded_send  # type: ignore[assignment]

    import literature.model_services as model_services
    import literature.providers as providers_module
    from literature.api import create_app
    from literature.models import BBOX_SPACE_MINERU_1000, Block, Document, Page, TocNode
    from literature.providers import MockProvider, ProviderResponse
    from literature.storage import ProjectStore

    providers_module.get_api_key = lambda provider: ""  # type: ignore[method-assign]
    model_services.ModelServiceStore.api_key = lambda self, service_id: "fake-key"  # type: ignore[method-assign]
    model_services.ModelServiceStore.configured = lambda self, service_id: True  # type: ignore[method-assign]

    store = ProjectStore(library)
    from tests.manual.parse_ui_harness import make_pdf

    pdf = make_pdf(base / "book.pdf", 2)
    project_id = store.create_project(pdf, title="Translate Open Book")["project_id"]
    document = Document(
        project_id=project_id, title="Translate Open Book", source_language="en", target_language="zh-CN",
        bbox_space=BBOX_SPACE_MINERU_1000, page_count=1,
        pages=[Page(page_index=0, width=420, height=595, source_page_index=0)],
    )
    document.blocks = [
        Block(block_id="t1", type="title", order=1, page_index=0, source_text="Chapter One", level=1),
        Block(block_id="p1", type="paragraph", order=2, page_index=0, source_text="Hello world."),
        Block(block_id="t2", type="title", order=3, page_index=0, source_text="Chapter Two", level=1),
        Block(block_id="p2", type="paragraph", order=4, page_index=0, source_text="More text."),
    ]
    document.toc = [
        TocNode("n1", "Chapter One", 1, "t1", 0, []),
        TocNode("n2", "Chapter Two", 1, "t2", 0, []),
    ]
    store.save_document(document)

    api_port, web_port = free_port(), free_port()
    dist = prepare_dist(base, api_port)
    app = create_app(store)

    # 隔离模型服务：注册一个本地服务（自动绑定全部角色），作业执行走 MockProvider。
    app.state.job_manager.models.create({
        "name": "Isolated Mock", "preset": "custom", "base_url": "http://127.0.0.1:9/v1", "model": "mock-model",
    })
    app.state.job_manager.provider_from = lambda service_id="", provider_input=None, model="": MockProvider()

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.25)
    start_web(dist, web_port)

    env = node_env()
    shot = ROOT / "tmp" / "translate-open.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    payload = run_node("translate_open_check.cjs", [str(web_port), str(api_port), project_id, str(shot)], env, timeout=240)
    checks = payload.get("checks", [])
    if not checks:
        print("TRANSLATE OPEN UI FAILED:", payload.get("error", "unknown"))
        server.should_exit = True
        return 2
    ok = True
    for check in checks:
        label, value = check[0], check[1]
        detail = f"  {check[2]}" if len(check) > 2 else ""
        print(f"{'PASS' if value else 'FAIL'}: {label}{detail}")
        ok = ok and bool(value)
    print("console errors:", payload.get("consoleErrors"))
    print("截图:", shot)
    print("结果：", "全部通过" if ok else "存在失败")
    server.should_exit = True
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
