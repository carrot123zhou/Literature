"""真实浏览器验收（隔离）：导出管理（删除/重命名）、默认 EPUB 对照版、结果行不溢出、顶栏图标与间距。

在本地终端运行：
    python3 tests/manual/export_manage_browser_check.py

不联网、不读 Keychain、不调用模型：本地 API + 生产构建前端 + 隔离项目。
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

    from literature.api import create_app
    from literature.models import BBOX_SPACE_MINERU_1000, Block, Document, Page, TranslationEntry, TranslationLayer, TocNode
    from literature.storage import ProjectStore

    store = ProjectStore(library)
    from tests.manual.parse_ui_harness import make_pdf

    # 长书名：专门用来压力测试导出结果行的溢出约束。
    long_title = "Between Cultures Europe and Its Others in Five Exemplary Lives (Jerrold Seigel) (Z-Library)"
    pdf = make_pdf(base / "book.pdf", 2)
    project_id = store.create_project(pdf, title=long_title)["project_id"]
    document = Document(
        project_id=project_id, title=long_title, source_language="en", target_language="zh-CN",
        bbox_space=BBOX_SPACE_MINERU_1000, page_count=1,
        pages=[Page(page_index=0, width=420, height=595, source_page_index=0)],
    )
    document.blocks = [
        Block(block_id="t1", type="title", order=1, page_index=0, source_text="Chapter One", level=1),
        Block(block_id="p1", type="paragraph", order=2, page_index=0,
              source_text="A fairly long opening paragraph that keeps going so the exported file name is long too."),
        Block(block_id="t2", type="title", order=3, page_index=0, source_text="Chapter Two", level=1),
        Block(block_id="p2", type="paragraph", order=4, page_index=0, source_text="Second chapter body text."),
    ]
    document.toc = [
        TocNode("n1", "Chapter One", 1, "t1", 0, []),
        TocNode("n2", "Chapter Two", 1, "t2", 0, []),
    ]
    store.save_document(document)
    store.save_translation(TranslationLayer(
        project_id=project_id, locale="zh-CN",
        entries={
            "t1": TranslationEntry("t1", text="第一章"),
            "p1": TranslationEntry("p1", text="第一段译文。"),
            "t2": TranslationEntry("t2", text="第二章"),
            "p2": TranslationEntry("p2", text="第二段译文。"),
        },
    ))

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
    shot = ROOT / "tmp" / "export-manage.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    payload = run_node("export_manage_check.cjs", [str(web_port), str(api_port), project_id, str(shot)], env, timeout=240)
    checks = payload.get("checks", [])
    if not checks:
        print("EXPORT MANAGE UI FAILED:", payload.get("error", "unknown"))
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
