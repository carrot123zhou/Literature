"""真实浏览器验收（隔离）：AI 目录校准零高级配置。

在本地终端运行：
    python3 tests/manual/advanced_simplify_browser_check.py

不联网、不读 Keychain、不调用真实模型：本地 API + 生产构建前端 + 隔离项目。
验证项：高级选项整体移除（目录基准/视觉开关/本地工具按钮不再出现）；
档位 + 开始/暂停 + 按需展开的模型选择与「目录识别不准？」纠错入口保留；
零配置启动（配置桩服务后可创建 coarse 作业）；已保存基准复用；
纠错目录页优先于已保存基准；清空目录页恢复基准复用。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
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
    from literature.models import BBOX_SPACE_MINERU_1000, Block, Document, Page, TocNode
    from literature.storage import ProjectStore
    from tests.manual.parse_ui_browser_check import free_port, node_env, prepare_dist, run_node, start_web
    from tests.manual.parse_ui_harness import make_pdf

    store = ProjectStore(library)
    pdf = make_pdf(base / "book.pdf", 2)
    project_id = store.create_project(pdf, title="Advanced Fixture")["project_id"]
    document = Document(
        project_id=project_id, title="Advanced Fixture", source_language="en",
        bbox_space=BBOX_SPACE_MINERU_1000, page_count=2,
        pages=[Page(page_index=i, width=420, height=595, source_page_index=i) for i in range(2)],
    )
    document.blocks = [
        Block(block_id="mark", type="title", order=0, page_index=0, source_text="Contents", level=1),
        Block(block_id="t1", type="title", order=1, page_index=1, source_text="Chapter One", level=1),
        Block(block_id="p1", type="paragraph", order=2, page_index=1, source_text="Opening body text for the fixture."),
        Block(block_id="t2", type="title", order=3, page_index=1, source_text="Chapter Two", level=1),
        Block(block_id="p2", type="paragraph", order=4, page_index=1, source_text="Second chapter body text."),
    ]
    document.toc = [
        TocNode("n1", "Chapter One", 1, "t1", 1, []),
        TocNode("n2", "Chapter Two", 1, "t2", 1, []),
    ]
    store.save_document(document)

    api_port, web_port = free_port(), free_port()
    dist = prepare_dist(base, api_port)
    app = create_app(store)

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    threading_started = False
    import threading
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(80):
        if server.started:
            threading_started = True
            break
        import time
        time.sleep(0.25)
    if not threading_started:
        print("API 启动失败")
        return 2
    start_web(dist, web_port)

    env = node_env()
    shot = ROOT / "tmp" / "advanced-simplify.png"
    shot.parent.mkdir(parents=True, exist_ok=True)
    payload = run_node("advanced_simplify_check.cjs", [str(web_port), str(api_port), project_id, str(shot)], env, timeout=240)
    checks = payload.get("checks", [])
    if not checks:
        print("ADVANCED SIMPLIFY UI FAILED:", payload.get("error", "unknown"))
        server.should_exit = True
        return 2
    ok = True
    for check in checks:
        label, value = check[0], check[1]
        print(f"{'PASS' if value else 'FAIL'}: {label}")
        ok = ok and bool(value)
    print("console errors:", payload.get("consoleErrors"))
    print("截图:", shot)
    print("结果：", "全部通过" if ok else "存在失败")
    server.should_exit = True
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
