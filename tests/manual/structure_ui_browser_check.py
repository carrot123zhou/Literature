"""真实浏览器验收（隔离）：AI 结构校对的复核参数、运行计时/阶段与思考配置。

在本地终端运行：
    source .venv/bin/activate && python tests/manual/structure_ui_browser_check.py

不联网、不读 Keychain、不调用付费模型；结构作业响应由浏览器路由 stub。
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
    # 在导入 api 前用非空假 Token 短路 Keychain 读取（get_mineru_token 先看 MINERU_API_TOKEN）。
    os.environ["MINERU_API_TOKEN"] = "fake-ui-token"

    # 阻断非本地网络：任何真实 MinerU/provider 请求都会直接失败，而不是静默出网。
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
    from literature.models import Block, Document
    from literature.storage import ProjectStore

    # 不读真实 Keychain，也不发出任何付费请求。
    providers_module.get_api_key = lambda provider: ""  # type: ignore[assignment]
    model_services.ModelServiceStore.api_key = lambda self, service_id: "fake-key"  # type: ignore[assignment]
    model_services.ModelServiceStore.configured = lambda self, service_id: True  # type: ignore[assignment]

    store = ProjectStore(library)
    from tests.manual.parse_ui_harness import make_pdf

    pdf = make_pdf(base / "book.pdf", 1)
    project_id = store.create_project(pdf, title="Structure UI Book")["project_id"]
    document = Document(project_id=project_id, title="Structure UI Book", source_language="en", target_language="zh-CN")
    document.blocks = [
        Block(block_id=f"t{i}", type="title", order=i, page_index=0, source_text=f"Chapter {i}", level=1)
        for i in range(3)
    ] + [
        Block(block_id=f"p{i}", type="paragraph", order=10 + i, page_index=0, source_text=f"Body paragraph {i}.") for i in range(3)
    ]
    document.page_count = 1
    store.save_document(document)
    models = model_services.ModelServiceStore(model_services.runtime_root())
    models.create({"name": "Test Mock", "preset": "custom", "base_url": "http://127.0.0.1:1/v1", "model": "mock"})

    api_port, web_port = free_port(), free_port()
    dist = prepare_dist(base, api_port)
    app = create_app(store)

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.25)
    start_web(dist, web_port)

    env = node_env()
    payload = run_node("structure_ui_check.cjs", [str(web_port), str(api_port), project_id], env, timeout=180)
    checks = payload.get("checks", [])
    if not checks:
        print("STRUCTURE UI FAILED:", payload.get("error", "unknown"))
        server.should_exit = True
        return 2
    ok = True
    for label, value in checks:
        print(f"{'PASS' if value else 'FAIL'}: {label}")
        ok = ok and bool(value)
    print("console errors:", payload.get("consoleErrors"))
    print("结果：", "全部通过" if ok else "存在失败")
    server.should_exit = True
    temporary.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
