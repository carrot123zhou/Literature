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

    pdf = make_pdf(base / "book.pdf", 1)
    project_id = store.create_project(pdf, title="TOC Book")["project_id"]
    project = store.project_path(project_id)
    # 目录页：每行都是一个矮 paragraph（高度 16~22/1000）。旧版会被全局 button{min-height:32px}
    # 撑高、彼此堆叠；本检查就是盯住这一点。
    toc = [
        ("title", "Contents", [442, 176, 555, 198]),
        ("paragraph", "Acknowledgments • xi", [268, 349, 482, 368]),
        ("paragraph", "Introduction • xiii", [268, 373, 448, 389]),
        ("paragraph", "Chapter 1 Bringing Transcendents Down to Earth • 1", [168, 417, 649, 435]),
        ("paragraph", "Chapter 2 The Transcendent's Cultural Repertoire • 39", [168, 438, 661, 456]),
        ("paragraph", "Chapter 3 Deeper Repertoire Analysis • 62", [168, 462, 717, 480]),
        ("paragraph", "Chapter 4 Secret Arts, Manifest Wonders • 88", [168, 483, 583, 503]),
        ("paragraph", "Chapter 5 Verbal Self-Presentation and Audience Response • 130", [168, 507, 741, 524]),
        ("paragraph", "Chapter 6 Adepts and Their Communities • 151", [168, 528, 607, 548]),
        ("paragraph", "Chapter 7 Adepts, Their Families, and the Imperium • 186", [168, 552, 685, 571]),
        ("paragraph", "Chapter 8 Hagiographic Persuasions • 216", [168, 575, 559, 593]),
        ("paragraph", "Epilogue • 259", [268, 620, 424, 638]),
        ("paragraph", "Bibliography • 267", [268, 643, 452, 661]),
        ("paragraph", "Index • 291", [268, 665, 398, 682]),
    ]
    raw = project / "ocr" / "raw" / "mineru" / "slice-001"
    raw.mkdir(parents=True, exist_ok=True)
    relative = "ocr/raw/mineru/slice-001/book_content_list_v2.json"
    (project / relative).write_text(json.dumps([[
        {"type": kind, "content": {"paragraph_content": [{"content": text}]} if kind == "paragraph" else {"title_content": [{"content": text}]}, "bbox": bbox}
        for kind, text, bbox in toc
    ]]), encoding="utf-8")
    document = Document(
        project_id=project_id, title="TOC Book", source_language="en", target_language="zh-CN",
        bbox_space=BBOX_SPACE_MINERU_1000, page_count=1,
        pages=[Page(page_index=0, width=420, height=595, source_page_index=0)],
        source_files=[{"path": relative, "representation": "content_list_v2", "sha256": "", "page_offset": 0, "page_count": 1}],
    )
    document.blocks = [
        Block(block_id=f"toc-{index}", type=kind, order=index + 1, page_index=0, bbox=bbox, source_text=text,
              level=1 if kind == "title" else 0)
        for index, (kind, text, bbox) in enumerate(toc)
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
    shot = ROOT / "tmp" / "toc-overlay.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    payload = run_node("toc_overlay_check.cjs", [str(web_port), str(api_port), project_id, str(shot)], env, timeout=180)
    checks = payload.get("checks", [])
    if not checks:
        print("TOC OVERLAY UI FAILED:", payload.get("error", "unknown"))
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
