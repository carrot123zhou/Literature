"""隔离的解析生命周期 UI 验收服务。

用途：在完全不调用真实 MinerU / 付费模型、不读取 Keychain 的前提下，起一个带
隔离库与隔离 runtime 的本地 API，并用能真正跑完整条流水线的假 MinerU 客户端
（见 ``tests/manual/fake_mineru.py``）替换真实调用，供浏览器全流程点击验收。

用法：
    python tests/manual/parse_ui_harness.py --library <dir> --runtime <dir> --collector <dir> --port 41741

种子数据：
- Partial Book：9 页 PDF，已合并前 4 页（部分解析）——用于“继续解析到全文”。
- Locate Book：9 页 PDF，已合并 1、2、5、7 页——用于验证第 5/7 页定位。
- Fresh Book：260 页 PDF，从未解析（无 job、无规范文档）——用于“开始解析”与跨 200 页切片。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def make_pdf(path: Path, pages: int) -> Path:
    import fitz

    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=420, height=595)
        # 写入可辨页码，使截图/像素断言能证明 canvas 真的渲染出了内容。
        page.insert_text((48, 96), f"PAGE {index + 1}", fontsize=28, color=(0, 0, 0))
        page.draw_rect(fitz.Rect(40, 120, 380, 160), color=(0.2, 0.3, 0.6), width=2)
    document.save(path)
    document.close()
    return path


def make_result_dir(root: Path, name: str, pages: list[int]) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    content = []
    geometry = []
    for offset, source_page in enumerate(pages):
        content.append([
            {"type": "title", "bbox": [30, 30, 300, 60],
             "content": {"level": 1, "title_content": [{"content": f"Chapter on page {source_page}"}]}},
            {"type": "paragraph", "bbox": [30, 80, 380, 200],
             "content": {"paragraph_content": [{"content": f"Body text on source page {source_page}."}]}},
        ])
        geometry.append({"page_idx": offset, "page_size": [420, 595]})
    (directory / f"{name}_content_list_v2.json").write_text(json.dumps(content), encoding="utf-8")
    (directory / f"{name}_middle.json").write_text(json.dumps({"pdf_info": geometry}), encoding="utf-8")
    (directory / "layout.json").write_text(json.dumps({"pdf_info": geometry}), encoding="utf-8")
    return directory


def seed_history_job(workflow, project_id: str, job_id: str, *, state: str, finished_at: str = "") -> None:
    """直接写入一个归历史验收用的 job.json + bridge（不经真实解析）。"""
    from literature.mineru_workflow import SCHEMA_VERSION
    from literature.storage import atomic_json

    workspace = workflow.jobs_root / job_id
    workspace.mkdir(parents=True, exist_ok=True)
    created = datetime.now(timezone.utc).isoformat()
    atomic_json(workspace / "job.json", {
        "job_id": job_id, "source_pdf": "/tmp/history.pdf", "output_epub": "/tmp/history.epub",
        "workspace": str(workspace), "raw_output": "/tmp/raw", "source_size": 1,
        "source_sha256": "digest", "state": state, "slices": [], "error": "",
        "created_at": created, "updated_at": created, "finished_at": finished_at,
        "schema_version": SCHEMA_VERSION,
    })
    atomic_json(workspace / "literature-bridge.json", {"project_id": project_id})


def seed(store, collector: Path, runtime: Path) -> dict[str, str]:
    from literature.mineru import import_mineru, merge_imported_project
    from literature.mineru_workflow import JobState, MinerUWorkflow, SliceJob, SliceState
    from literature.storage import atomic_json

    ids: dict[str, str] = {}

    def partial(title: str, total: int, pages: list[int]) -> str:
        source = make_pdf(collector / f"{title}.pdf", total)
        project = store.create_project(source, title=title)
        project_id = project["project_id"]
        raw_root = store.project_path(project_id) / "ocr" / "raw" / "mineru"
        directory = make_result_dir(collector / "results", title.replace(" ", "-").lower(), pages)
        store.import_raw_mineru(project_id, [directory])
        merge_imported_project(
            store, project_id,
            [{"directory": raw_root / "slice-001", "source_pages": pages}],
            source_page_count=total, append=False,
        )
        ids[title] = project_id
        return project_id

    partial("Partial Book", 9, [1, 2, 3, 4])
    partial("Locate Book", 9, [1, 2, 5, 7])
    partial("Retry Book", 20, [1, 2])
    partial("Dup Book", 4, [1, 2])
    partial("Queue A", 4, [1, 2])
    partial("Queue B", 4, [1, 2])
    partial("Queue C", 4, [1, 2])

    # 从未解析的已有资源（既无 job 也无规范文档）：验证“开始解析”入口、跨 200 页切片与状态。
    fresh_source = make_pdf(collector / "Fresh Book.pdf", 260)
    fresh_project = store.create_project(fresh_source, title="Fresh Book")
    ids["Fresh Book"] = fresh_project["project_id"]

    # 旧资源：手工导入得到无 coverage 的 document；Legacy Book 另带一个可信的已完成旧作业。
    for title in ("Legacy Book", "Legacy Bad"):
        source = make_pdf(collector / f"{title}.pdf", 4)
        project = store.create_project(source, title=title)
        project_id = project["project_id"]
        pages = [1, 2, 3] if title == "Legacy Book" else [1, 2]
        import_mineru(store, project_id, [make_result_dir(collector / "results", title.replace(" ", "-").lower(), pages)])
        ids[title] = project_id
    legacy_id = ids["Legacy Book"]
    workflow = MinerUWorkflow(Path(runtime) / "ocr-jobs")
    job = workflow.create_job(
        store.project_path(legacy_id) / "source" / "origin.pdf",
        store.project_path(legacy_id) / "ocr" / ".x.epub",
    )
    job.state = JobState.COMPLETED
    job.slices = [SliceJob(1, 1, 3, "d", "a.pdf", "/tmp/a.pdf", state=SliceState.DOWNLOADED)]
    workflow.save_job(job)
    atomic_json(workflow.jobs_root / job.job_id / "literature-bridge.json", {"project_id": legacy_id})

    # 旧 page_ranges=1-N 前缀资源（带 layout.json 页数证据），用于前缀迁移 + 继续解析。
    prefix_source = make_pdf(collector / "Legacy Range Book.pdf", 30)
    prefix_project = store.create_project(prefix_source, title="Legacy Range Book")
    prefix_id = prefix_project["project_id"]
    import_mineru(store, prefix_id, [make_result_dir(collector / "results", "legacy-range", list(range(1, 11)))])
    prefix_job = workflow.create_job(
        store.project_path(prefix_id) / "source" / "origin.pdf",
        store.project_path(prefix_id) / "ocr" / ".x.epub",
        page_ranges="1-10",
    )
    prefix_job.state = JobState.COMPLETED
    prefix_job.slices = [SliceJob(1, 1, 10, "d", "a.pdf", "/tmp/a.pdf", state=SliceState.DOWNLOADED)]
    workflow.save_job(prefix_job)
    atomic_json(workflow.jobs_root / prefix_job.job_id / "literature-bridge.json", {"project_id": prefix_id})
    ids["Legacy Range Book"] = prefix_id

    # 解析历史 / 归档夹具：25 个已完成（含 1 个 100 天前超期，启动时自动归档）+ 12 个失败。
    history_source = make_pdf(collector / "History Book.pdf", 3)
    history_project = store.create_project(history_source, title="History Book")
    history_id = history_project["project_id"]
    ids["History Book"] = history_id
    now = datetime.now(timezone.utc)
    for index in range(25):
        finished = now - (timedelta(days=100) if index == 0 else timedelta(minutes=index))
        seed_history_job(workflow, history_id, f"hist-{index:02d}", state="completed", finished_at=finished.isoformat())
    for index in range(12):
        seed_history_job(workflow, history_id, f"hist-fail-{index:02d}", state="failed")

    # 大书夹具：1000+ blocks，用于阅读页窗口化（DOM 有界 + 跳转）验收。
    from literature.models import Block, Document, TranslationEntry, TranslationLayer, block_source_digest
    big_source = make_pdf(collector / "Big Book.pdf", 3)
    big_project = store.create_project(big_source, title="Big Book")
    big_id = big_project["project_id"]
    big_document = Document(project_id=big_id, title="Big Book", source_language="en", target_language="zh-CN")
    big_blocks: list[Block] = []
    for index in range(1000):
        if index % 50 == 0:
            big_blocks.append(Block(block_id=f"big-h{index}", type="title", order=len(big_blocks), page_index=index // 200, source_text=f"Chapter {index // 50 + 1}", level=1))
        big_blocks.append(Block(block_id=f"big-p{index}", type="paragraph", order=len(big_blocks), page_index=index // 200, source_text=f"Body paragraph {index}."))
    big_document.blocks = big_blocks
    big_document.page_count = 3
    store.save_document(big_document)
    # 为 big-p0 预置一条已译条目，供审校/采纳浏览器验收。
    target_block = next(block for block in big_blocks if block.block_id == "big-p0")
    layer = TranslationLayer(project_id=big_id, locale="zh-CN")
    layer.entries["big-p0"] = TranslationEntry(
        block_id="big-p0", text="译：Body paragraph 0.", status="translated",
        source_hash=block_source_digest(target_block), structure_revision=0,
    )
    store.save_translation(layer)
    ids["Big Book"] = big_id

    print(f"seeded {ids}")
    return ids


def configure(store, runtime: Path, *, run_jobs: bool = True):
    """注入假 MinerU 客户端与 Token（不读 Keychain、不发外部请求）。"""
    import literature.api as api_module
    import literature.ocr as ocr_module
    from tests.manual.fake_mineru import FakeMinerU, FakeMinerUClient, fake_client_factory

    FakeMinerU.reset()
    token = "fake-token" if run_jobs else ""
    ocr_module.get_mineru_token = lambda: token
    api_module.get_mineru_token = lambda: "fake-ui-token"
    api_module.MinerUClient = FakeMinerUClient
    # 注入一个假模型服务（不读 Keychain）：仅供前端“审校”按钮存在与 stub 端点使用。
    import literature.providers as providers_module
    from literature.api import application_support
    from literature.model_services import ModelServiceStore
    providers_module.get_api_key = lambda provider: "fake-key"  # type: ignore[assignment]
    models = ModelServiceStore(application_support())
    if not models.list():
        models.create({"provider": "custom", "name": "Test Mock", "base_url": "http://127.0.0.1:1/v1", "model": "mock"})
    app = api_module.create_app(store)
    service = app.state.ocr_service
    service.workflow.client_factory = fake_client_factory
    service.workflow.sleep = lambda _seconds: None
    service.workflow.poll_interval = 0.0
    return app, service


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", required=True)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--collector", required=True)
    parser.add_argument("--port", type=int, default=41741)
    args = parser.parse_args()

    collector = Path(args.collector)
    collector.mkdir(parents=True, exist_ok=True)

    from literature.storage import ProjectStore

    runtime = Path(args.runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")

    store = ProjectStore(Path(args.library))
    if not store.list_projects():
        seed(store, collector, runtime)

    app, _service = configure(store, runtime, run_jobs=True)
    import uvicorn

    print(f"UI acceptance API on http://127.0.0.1:{args.port} (empty session token, fake MinerU)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
