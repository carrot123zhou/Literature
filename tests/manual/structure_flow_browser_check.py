"""真实浏览器验收（隔离）：结构校对的跨页续接框/连线与对照观看优化。

在本地终端运行：
    source .venv/bin/activate && python tests/manual/structure_flow_browser_check.py

不联网、不读 Keychain、不调用模型：只起本地 API + 生产构建前端，用一份两页、
正文跨页断行的隔离项目验证 PDF 覆盖层与 Markdown 同步高亮。
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
    from literature.models import BBOX_SPACE_MINERU_1000, Block, Document, Page
    from literature.storage import ProjectStore

    providers_module.get_api_key = lambda provider: ""  # type: ignore[assignment]
    model_services.ModelServiceStore.api_key = lambda self, service_id: "fake-key"  # type: ignore[assignment]
    model_services.ModelServiceStore.configured = lambda self, service_id: True  # type: ignore[assignment]

    store = ProjectStore(library)
    from tests.manual.parse_ui_harness import make_pdf

    pdf = make_pdf(base / "book.pdf", 2)
    project_id = store.create_project(pdf, title="Cross Page Book")["project_id"]
    project = store.project_path(project_id)
    raw = project / "ocr" / "raw" / "mineru" / "slice-001"
    raw.mkdir(parents=True, exist_ok=True)
    relative = "ocr/raw/mineru/slice-001/book_content_list_v2.json"
    merged = "The gradual/sudden paradigm, which functions as the matrix of the Chan/Zen tradition, can be"
    (project / relative).write_text(json.dumps([
        [{"type": "paragraph", "content": {"paragraph_content": [{"content": merged}]}, "bbox": [100, 700, 900, 960]}],
        [
            {"type": "paragraph", "content": {"paragraph_content": []}, "bbox": [100, 80, 900, 380]},
            {"type": "paragraph", "content": {"paragraph_content": [{"content": "seen as a dialectical tension between mediate and immediate understanding and it persists"}]}, "bbox": [100, 380, 900, 700]},
        ],
    ]), encoding="utf-8")
    document = Document(
        project_id=project_id, title="Cross Page Book", source_language="en", target_language="zh-CN",
        bbox_space=BBOX_SPACE_MINERU_1000, page_count=2,
        pages=[Page(page_index=index, width=420, height=595, source_page_index=index) for index in range(2)],
        source_files=[{"path": relative, "representation": "content_list_v2", "sha256": "", "page_offset": 0, "page_count": 2}],
    )
    document.blocks = [
        Block(block_id="p0", type="paragraph", order=1, page_index=0, bbox=[100, 700, 900, 960], source_text=merged),
        Block(block_id="p1", type="paragraph", order=2, page_index=1, bbox=[100, 380, 900, 700],
              source_text="seen as a dialectical tension between mediate and immediate understanding and it persists"),
    ]
    store.save_document(document)

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
    shot = ROOT / "tmp" / "structure-flow-review.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    payload = run_node("structure_flow_check.cjs", [str(web_port), str(api_port), project_id, str(shot)], env, timeout=180)
    checks = payload.get("checks", [])
    if not checks:
        print("STRUCTURE FLOW UI FAILED:", payload.get("error", "unknown"))
        server.should_exit = True
        return 2
    ok = True
    for label, value in checks:
        print(f"{'PASS' if value else 'FAIL'}: {label}")
        ok = ok and bool(value)
    print("console errors:", payload.get("consoleErrors"))
    print("截图:", shot)
    print("结果：", "全部通过" if ok else "存在失败")
    server.should_exit = True
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
