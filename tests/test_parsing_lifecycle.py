from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from literature.mineru import merge_imported_project
from literature.mineru_workflow import MinerUWorkflow, WorkflowJob
from literature.models import TranslationEntry, TranslationLayer
from literature.ocr import MinerUService
from literature.parse_queue import ParseQueue, select_next_runnable
from literature.storage import ProjectStore, StorageError


def make_pdf(path: Path, pages: int) -> Path:
    import fitz

    document = fitz.open()
    for _ in range(pages):
        document.new_page(width=300, height=400)
    document.save(path)
    document.close()
    return path


def make_result_dir(root: Path, name: str, pages: list[int]) -> Path:
    """构造一个 MinerU 结果目录，覆盖给定 1-based 源页。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    content = []
    geometry = []
    for offset, source_page in enumerate(pages):
        content.append([
            {"type": "title", "bbox": [20, 20, 300, 50],
             "content": {"level": 1, "title_content": [{"content": f"Source page {source_page}"}]}},
            {"type": "paragraph", "bbox": [20, 70, 560, 200],
             "content": {"paragraph_content": [{"content": f"Body on source page {source_page}."}]}},
        ])
        geometry.append({"page_idx": offset, "page_size": [300, 400]})
    (directory / f"{name}_content_list_v2.json").write_text(json.dumps(content), encoding="utf-8")
    (directory / f"{name}_middle.json").write_text(json.dumps({"pdf_info": geometry}), encoding="utf-8")
    return directory


class ParseQueueTests(unittest.TestCase):
    def test_reorder_and_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "queue.json"
            queue = ParseQueue(path)
            queue.enqueue("a")
            queue.enqueue("b")
            queue.enqueue("c")
            self.assertEqual(queue.order(), ["a", "b", "c"])
            self.assertEqual(queue.move("c", "top"), 0)
            self.assertEqual(queue.order(), ["c", "a", "b"])
            self.assertEqual(ParseQueue(path).order(), ["c", "a", "b"])
            queue.move("c", "down")
            self.assertEqual(queue.order(), ["a", "c", "b"])
            queue.move("a", "up")
            self.assertEqual(queue.order(), ["a", "c", "b"])
            self.assertTrue(queue.remove("c"))
            self.assertEqual(ParseQueue(path).order(), ["a", "b"])

    def test_enqueue_existing_preserves_when_position_end(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            queue = ParseQueue(Path(temporary) / "queue.json")
            queue.enqueue("a")
            queue.enqueue("b")
            queue.enqueue("a", position="end")
            self.assertEqual(queue.order(), ["b", "a"])

    def test_select_next_runnable_respects_order_and_state(self) -> None:
        states = {"a": "completed", "b": "interrupted", "c": "queued"}
        self.assertEqual(
            select_next_runnable(["a", "b", "c"], lambda job: states.get(job), {"queued", "waiting", "interrupted"}),
            "b",
        )
        self.assertIsNone(select_next_runnable(["a", "x"], lambda job: states.get(job), {"queued"}))


class PreparePageSelectionTests(unittest.TestCase):
    def test_non_contiguous_selection_uses_local_subsets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_pdf(root / "book.pdf", 6)
            workflow = MinerUWorkflow(root / "jobs")
            job = workflow.create_job(source, root / "book.epub", requested_pages=[1, 3, 5], page_limit=2)
            workflow._prepare(job, None)
            self.assertEqual([item.source_pages for item in job.slices], [[1, 3], [5]])
            self.assertTrue(all(item.page_ranges == "" for item in job.slices))
            self.assertEqual(job.page_ranges, "")
            self.assertEqual(job.requested_pages, [1, 3, 5])
            self.assertEqual(job.requested_ranges, "1,3,5")
            self.assertEqual(job.page_count, 3)
            self.assertEqual(job.source_page_count, 6)
            for item in job.slices:
                self.assertTrue(Path(item.local_pdf).is_file())

    def test_more_than_page_limit_selection_is_split(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = make_pdf(root / "book.pdf", 250)
            workflow = MinerUWorkflow(root / "jobs")
            job = workflow.create_job(source, root / "book.epub", requested_pages=list(range(1, 251)), page_limit=200)
            workflow._prepare(job, None)
            self.assertEqual([len(item.source_pages) for item in job.slices], [200, 50])
            covered = [page for item in job.slices for page in item.source_pages]
            self.assertEqual(covered, list(range(1, 251)))

    def test_subset_slice_omits_remote_page_ranges(self) -> None:
        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"code": 0, "data": {"batch_id": "b", "file_urls": ["https://upload"]}}

        class HTTP:
            def __init__(self):
                self.calls = []

            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return Response()

            def close(self):
                pass

        from literature import mineru_workflow as workflow_module

        http = HTTP()
        client = workflow_module.MinerUClient("token", http_client=http)
        job = WorkflowJob(
            job_id="subset", source_pdf="/tmp/book.pdf", output_epub="/tmp/book.epub",
            workspace="/tmp/subset", raw_output="/tmp/raw", source_size=1, source_sha256="digest",
        )
        item = workflow_module.SliceJob(1, 1, 3, "data", "book.pdf", "/tmp/book.pdf", source_pages=[1, 3, 5])
        client.create_batch([item], job)
        payload = http.calls[0][2]["json"]
        self.assertNotIn("page_ranges", payload["files"][0])


class MergeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = make_pdf(self.root / "scan.pdf", 9)
        project = self.store.create_project(source, title="Coverage")
        self.project_id = project["project_id"]
        self.project = self.store.project_path(self.project_id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _seal_fresh(self, name: str, pages: list[int]) -> Path:
        directory = make_result_dir(self.root / "results", name, pages)
        sealed = self.store.import_raw_mineru(self.project_id, [directory])
        return self.project / "ocr" / "raw" / "mineru" / sealed[0]["slice"]

    def _seal_append(self, name: str, pages: list[int]) -> Path:
        directory = make_result_dir(self.root / "results", name, pages)
        sealed = self.store.append_raw_mineru(self.project_id, [directory])
        return Path(sealed[0]["destination"])

    def test_fresh_partial_then_append_preserves_ids_and_translation(self) -> None:
        first = self._seal_fresh("first", [1, 2, 3])
        document = merge_imported_project(
            self.store, self.project_id,
            [{"directory": first, "source_pages": [1, 2, 3]}],
            source_page_count=9, append=False,
        )
        self.assertEqual(document.coverage["state"], "partial")
        self.assertEqual(document.coverage["covered_pages"], 3)
        old_ids = [block.block_id for block in document.blocks]
        self.assertEqual(len(old_ids), 6)
        raw_manifest_before = json.loads((self.project / "ocr" / "raw" / "manifest.json").read_text(encoding="utf-8"))
        old_hashes = {
            item["path"]: item["sha256"]
            for slice_info in raw_manifest_before["slices"] for item in slice_info["files"]
        }
        layer = TranslationLayer(project_id=self.project_id, locale="zh-CN")
        layer.entries[old_ids[0]] = TranslationEntry(
            block_id=old_ids[0], text="人工译文", status="edited", locked=True,
        )
        self.store.save_translation(layer)

        second = self._seal_append("second", [4, 5, 6, 7, 8, 9])
        document = merge_imported_project(
            self.store, self.project_id,
            [{"directory": second, "source_pages": [4, 5, 6, 7, 8, 9]}],
            source_page_count=9, append=True,
        )
        self.assertEqual(document.coverage["state"], "full")
        self.assertEqual(document.coverage["covered_pages"], 9)
        self.assertEqual(document.page_count, 9)
        # 旧 block_id 原样保留且顺序不变；新 block 使用独立命名空间。
        new_ids = [block.block_id for block in document.blocks]
        self.assertEqual(new_ids[:len(old_ids)], old_ids)
        self.assertTrue(all(block_id.startswith("blk-p") for block_id in new_ids[len(old_ids):]))
        # 译文与锁定保留。
        reloaded = self.store.load_translation(self.project_id, "zh-CN")
        self.assertEqual(reloaded.entries[old_ids[0]].text, "人工译文")
        self.assertTrue(reloaded.entries[old_ids[0]].locked)
        # 旧 raw 哈希未变，且新增了第二片。
        raw_manifest_after = json.loads((self.project / "ocr" / "raw" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw_manifest_after["slices"]), 2)
        new_hashes = {
            item["path"]: item["sha256"]
            for slice_info in raw_manifest_after["slices"] for item in slice_info["files"]
        }
        for path, digest in old_hashes.items():
            self.assertEqual(new_hashes[path], digest)

    def test_gap_fill_orders_by_source_page_without_touching_old_ids(self) -> None:
        first = self._seal_fresh("first", [1, 2, 3])
        last = self._seal_append("last", [7, 8, 9])
        document = merge_imported_project(
            self.store, self.project_id,
            [
                {"directory": first, "source_pages": [1, 2, 3]},
                {"directory": last, "source_pages": [7, 8, 9]},
            ],
            source_page_count=9, append=False,
        )
        first_ids = {block.block_id for block in document.blocks if block.page_index in (0, 1, 2)}
        middle = self._seal_append("middle", [4, 5, 6])
        document = merge_imported_project(
            self.store, self.project_id,
            [{"directory": middle, "source_pages": [4, 5, 6]}],
            source_page_count=9, append=True,
        )
        pages = [block.page_index for block in document.blocks]
        self.assertEqual(pages, sorted(pages))
        self.assertEqual(document.coverage["state"], "full")
        # 旧 block 未丢失。
        self.assertTrue(first_ids.issubset({block.block_id for block in document.blocks}))

    def test_overlapping_append_pages_are_filtered(self) -> None:
        first = self._seal_fresh("first", [1, 2, 3])
        document = merge_imported_project(
            self.store, self.project_id,
            [{"directory": first, "source_pages": [1, 2, 3]}],
            source_page_count=9, append=False,
        )
        old_ids = {block.block_id for block in document.blocks}
        overlap = self._seal_append("overlap", [1, 4])
        document = merge_imported_project(
            self.store, self.project_id,
            [{"directory": overlap, "source_pages": [1, 4]}],
            source_page_count=9, append=True,
        )
        # 页 1 被过滤，只有页 4 新增；覆盖 = 1,2,3,4。
        self.assertEqual(document.coverage["covered_pages"], 4)
        self.assertEqual(document.coverage["ranges"], [[1, 4]])
        self.assertTrue(old_ids.issubset({block.block_id for block in document.blocks}))
        page_one_blocks = [block for block in document.blocks if block.page_index == 0]
        self.assertEqual({block.block_id for block in page_one_blocks}, {i for i in old_ids if i.endswith(("00000001", "00000002"))})

    def test_continue_parse_filters_covered_pages(self) -> None:
        first = self._seal_fresh("first", [1, 2, 3])
        merge_imported_project(
            self.store, self.project_id,
            [{"directory": first, "source_pages": [1, 2, 3]}],
            source_page_count=9, append=False,
        )
        service = MinerUService(self.store, self.root / "runtime")
        job, skipped = service.continue_parse(self.project_id, page_ranges="1-5")
        self.assertEqual(skipped, [1, 2, 3])
        self.assertEqual(job["requested_pages"], [4, 5])
        self.assertTrue(job["append"])
        self.assertEqual(job["requested_ranges"], "")

    def test_continue_parse_requires_known_coverage(self) -> None:
        source = make_pdf(self.root / "plain.pdf", 3)
        from literature.mineru import import_mineru

        directory = make_result_dir(self.root / "results", "plain", [1, 2, 3])
        project = self.store.create_project(source, title="Plain")
        import_mineru(self.store, project["project_id"], [directory])
        service = MinerUService(self.store, self.root / "runtime")
        # 手动导入没有 coverage，必须按“覆盖未知”保守处理。
        coverage = service.project_coverage(project["project_id"])
        self.assertEqual(coverage.get("state"), "unknown")
        with self.assertRaises(StorageError):
            service.continue_parse(project["project_id"])

    def test_seal_raw_mineru_is_idempotent(self) -> None:
        directory = make_result_dir(self.root / "results", "idem", [1, 2])
        first = self.store.seal_raw_mineru(self.project_id, [directory])
        second = self.store.seal_raw_mineru(self.project_id, [directory])
        self.assertEqual(first[0]["destination"], second[0]["destination"])
        self.assertTrue(second[0]["reused"])
        manifest = json.loads((self.project / "ocr" / "raw" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["slices"]), 1)

    def test_merge_same_slice_twice_does_not_duplicate_or_overwrite(self) -> None:
        sealed = self.store.seal_raw_mineru(
            self.project_id, [make_result_dir(self.root / "results", "twice", [1, 2])],
        )
        entry = Path(sealed[0]["destination"])
        merge_imported_project(
            self.store, self.project_id, [{"directory": entry, "source_pages": [1, 2]}],
            source_page_count=9, append=False,
        )
        manifest_before = json.loads((self.project / "ocr" / "raw" / "manifest.json").read_text(encoding="utf-8"))
        first_ids = [block.block_id for block in self.store.load_document(self.project_id).blocks]
        again = self.store.seal_raw_mineru(self.project_id, [entry])
        self.assertTrue(again[0]["reused"])
        document = merge_imported_project(
            self.store, self.project_id, [{"directory": Path(again[0]["destination"]), "source_pages": [1, 2]}],
            source_page_count=9, append=True,
        )
        manifest_after = json.loads((self.project / "ocr" / "raw" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest_after["slices"]), len(manifest_before["slices"]))
        # 重叠页被过滤，block 未重复，覆盖不变。
        self.assertEqual([block.block_id for block in document.blocks], first_ids)
        self.assertEqual(document.coverage["covered_pages"], 2)

    def test_legacy_document_without_coverage_stays_unknown(self) -> None:
        from literature.mineru import import_mineru

        source = make_pdf(self.root / "legacy.pdf", 3)
        project = self.store.create_project(source, title="Legacy")
        project_id = project["project_id"]
        directory = make_result_dir(self.root / "results", "legacy", [1, 2, 3])
        import_mineru(self.store, project_id, [directory])
        document = self.store.load_document(project_id)
        self.assertEqual(document.coverage, {})
        old_ids = [block.block_id for block in document.blocks]
        sealed = self.store.seal_raw_mineru(project_id, [directory])
        self.assertTrue(sealed[0]["reused"])
        merged = merge_imported_project(
            self.store, project_id,
            [{"directory": Path(sealed[0]["destination"]), "source_pages": [1, 2, 3]}],
            source_page_count=3, append=True,
        )
        # 旧资源无 coverage：不据现有页声明覆盖，也不重复写入。
        self.assertEqual(merged.coverage["state"], "unknown")
        self.assertEqual([block.block_id for block in merged.blocks], old_ids)

    def test_recover_interrupted_marks_active_jobs(self) -> None:
        from literature.mineru_workflow import JobState

        directory = make_result_dir(self.root / "results", "recover", [1])
        jobs_root = self.root / "runtime-jobs" / "ocr-jobs"
        job = MinerUWorkflow(jobs_root).create_job(
            self.project / "source" / "origin.pdf", self.root / "recover.epub",
        )
        job.state = JobState.PROCESSING
        workflow = MinerUWorkflow(jobs_root)
        workflow.save_job(job)
        service = MinerUService(self.store, self.root / "runtime-jobs")
        self.assertEqual(service.recover_interrupted(), 1)
        self.assertEqual(workflow.load_job(job.job_file).state, JobState.INTERRUPTED)
        self.assertEqual(service.recover_interrupted(), 0)


class StartParseTests(unittest.TestCase):
    """从未解析的已有资源：“开始解析”创建首个作业，不推断覆盖、不重复创建。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        # 隔离凭据：不读 Keychain、不发外部请求。
        self.token_patch = patch("literature.ocr.get_mineru_token", return_value="test-token")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_never_parsed_project_reports_unparsed_with_page_count(self) -> None:
        source = make_pdf(self.root / "book.pdf", 338)
        project = self.store.create_project(source, title="Fresh")
        service = MinerUService(self.store, self.root / "runtime")
        coverage = service.project_coverage(project["project_id"])
        self.assertEqual(coverage["state"], "unparsed")
        self.assertEqual(coverage["source_page_count"], 338)
        self.assertEqual(coverage["covered_pages"], 0)
        self.assertEqual(coverage["ranges"], [])

    def test_legacy_document_without_coverage_stays_unknown(self) -> None:
        from literature.mineru import import_mineru

        source = make_pdf(self.root / "legacy.pdf", 4)
        project = self.store.create_project(source, title="Legacy")
        import_mineru(self.store, project["project_id"], [make_result_dir(self.root / "results", "legacy", [1, 2])])
        service = MinerUService(self.store, self.root / "runtime")
        # 有规范文档但无可信 coverage：仍按“旧已解析资源未知”保守处理。
        self.assertEqual(service.project_coverage(project["project_id"])["state"], "unknown")

    def test_start_parse_creates_full_first_job_without_document(self) -> None:
        source = make_pdf(self.root / "book.pdf", 338)
        project = self.store.create_project(source, title="Fresh")
        project_id = project["project_id"]
        service = MinerUService(self.store, self.root / "runtime")
        job = service.start_parse(project_id)
        self.assertFalse(job["append"])
        self.assertEqual(job["requested_pages"], [])
        self.assertEqual(job["state"], "queued")
        self.assertEqual(self.store.load_manifest(project_id)["state"], "ocr_queued")
        self.assertFalse((self.store.project_path(project_id) / "document" / "document.json").is_file())
        # 已创建即入队，队列中可见（创建+调度）。
        self.assertIn(job["job_id"], service.queue.order())
        # 存在活动作业时第二次必须冲突，不重复创建。
        with self.assertRaises(StorageError):
            service.start_parse(project_id)
        self.assertEqual(len(service.list(project_id)), 1)

    def test_start_parse_split_338_pages_across_200(self) -> None:
        source = make_pdf(self.root / "book.pdf", 338)
        project = self.store.create_project(source, title="Fresh 338")
        project_id = project["project_id"]
        service = MinerUService(self.store, self.root / "runtime")
        job = service.start_parse(project_id, page_limit=200)
        workflow_job = service.workflow.load_job(service._job_path(job["job_id"]))
        service.workflow._prepare(workflow_job, None)
        self.assertEqual([len(item.source_pages) for item in workflow_job.slices], [200, 138])
        covered = [page for item in workflow_job.slices for page in item.source_pages]
        self.assertEqual(covered, list(range(1, 339)))

    def test_start_parse_missing_token_creates_nothing(self) -> None:
        source = make_pdf(self.root / "book.pdf", 5)
        project = self.store.create_project(source, title="NoToken")
        project_id = project["project_id"]
        service = MinerUService(self.store, self.root / "runtime")
        with patch("literature.ocr.get_mineru_token", return_value=""):
            with self.assertRaises(StorageError):
                service.start_parse(project_id)
        self.assertEqual(service.list(project_id), [])
        self.assertEqual(service.queue.order(), [])

    def test_start_parse_schedule_failure_keeps_recoverable_job(self) -> None:
        source = make_pdf(self.root / "book.pdf", 5)
        project = self.store.create_project(source, title="ScheduleFail")
        project_id = project["project_id"]
        service = MinerUService(self.store, self.root / "runtime")
        with patch.object(service, "start_job", side_effect=StorageError("队列写入失败")):
            with self.assertRaises(StorageError) as error:
                service.start_parse(project_id)
        self.assertIn("已经创建", str(error.exception))
        jobs = service.list(project_id)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["state"], "queued")

    def test_concurrent_start_parse_creates_single_job(self) -> None:
        source = make_pdf(self.root / "book.pdf", 40)
        project = self.store.create_project(source, title="Race")
        project_id = project["project_id"]
        service = MinerUService(self.store, self.root / "runtime")
        results: list[str] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            try:
                service.start_parse(project_id)
                results.append("ok")
            except StorageError:
                results.append("conflict")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["conflict", "ok"])
        self.assertEqual(len(service.list(project_id)), 1)

    def test_start_parse_honours_page_range(self) -> None:
        source = make_pdf(self.root / "book.pdf", 10)
        project = self.store.create_project(source, title="Range")
        service = MinerUService(self.store, self.root / "runtime")
        job = service.start_parse(project["project_id"], page_ranges="3-5,8")
        self.assertEqual(job["requested_pages"], [3, 4, 5, 8])
        self.assertEqual(job["page_ranges"], "")

    def test_start_parse_rejects_existing_document(self) -> None:
        from literature.mineru import import_mineru

        source = make_pdf(self.root / "book.pdf", 4)
        project = self.store.create_project(source, title="HasDoc")
        project_id = project["project_id"]
        import_mineru(self.store, project_id, [make_result_dir(self.root / "results", "hasdoc", [1, 2])])
        service = MinerUService(self.store, self.root / "runtime")
        with self.assertRaises(StorageError):
            service.start_parse(project_id)
        self.assertEqual(service.list(project_id), [])

    def test_continue_parse_rejects_never_parsed_project(self) -> None:
        source = make_pdf(self.root / "book.pdf", 6)
        project = self.store.create_project(source, title="NoDoc")
        service = MinerUService(self.store, self.root / "runtime")
        with self.assertRaises(StorageError):
            service.continue_parse(project["project_id"])

    def test_start_parse_reports_missing_and_changed_source(self) -> None:
        source = make_pdf(self.root / "book.pdf", 6)
        project = self.store.create_project(source, title="Broken")
        project_id = project["project_id"]
        service = MinerUService(self.store, self.root / "runtime")
        origin = self.store.source_path(project_id)
        origin.write_bytes(origin.read_bytes() + b"tampered")
        with self.assertRaises(StorageError) as changed:
            service.start_parse(project_id)
        self.assertIn("源文件", str(changed.exception))
        origin.unlink()
        with self.assertRaises(StorageError) as missing:
            service.start_parse(project_id)
        self.assertIn("源文件", str(missing.exception))
        # 失败不得留下任务。
        self.assertEqual(service.list(project_id), [])


class ApiLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        # 显式隔离凭据：不依赖 Keychain、不依赖全量测试的先前副作用。
        self.token_patches = [
            patch("literature.api.get_mineru_token", return_value=""),
            patch("literature.ocr.get_mineru_token", return_value=""),
        ]
        for item in self.token_patches:
            item.start()
            self.addCleanup(item.stop)
        from fastapi.testclient import TestClient
        from literature.api import create_app

        self.store = ProjectStore(self.root / "library")
        self.client = TestClient(create_app(self.store))
        self.headers = {"x-literature-token": self.client.app.state.session_token}

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        os.environ.pop("LITERATURE_RUNTIME", None)
        os.environ.pop("LITERATURE_LIBRARY", None)

    def test_queue_endpoints_and_atomic_conflicts(self) -> None:
        source = make_pdf(self.root / "scan.pdf", 4)
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(source)})
        project_id = created.json()["project_id"]
        directory = make_result_dir(self.root / "results", "api", [1, 2])
        import_path = self.root / "results" / "api"
        imported = self.client.post(
            f"/projects/{project_id}/mineru/import", headers=self.headers, json={"paths": [str(import_path)]},
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        descriptor = self.client.get("/projects", headers=self.headers).json()[0]
        self.assertEqual(descriptor["parse_coverage"]["state"], "unknown")

        # 无 Token 时继续解析被拒绝，且不会创建作业。
        blocked = self.client.post(f"/projects/{project_id}/ocr/continue", headers=self.headers, json={})
        self.assertEqual(blocked.status_code, 400, blocked.text)

        missing_queue = self.client.post("/ocr/jobs/not-real/queue", headers=self.headers, json={"position": "top"})
        self.assertEqual(missing_queue.status_code, 404)
        missing_start = self.client.post("/ocr/jobs/not-real/start", headers=self.headers)
        self.assertEqual(missing_start.status_code, 404)
        # 无效任务不得进入 running 集合。
        self.assertIsNone(self.client.app.state.ocr_service.running_job_id())

        with patch("literature.api.get_mineru_token", return_value=""):
            created_jobs = self.client.post("/intake/jobs", headers=self.headers, json={"paths": [str(source)]})
        self.assertEqual(created_jobs.status_code, 200, created_jobs.text)
        job_id = created_jobs.json()["jobs"][0]["job_id"]
        queue = self.client.get("/ocr/queue", headers=self.headers).json()
        self.assertEqual(queue["entries"], [])

    def test_start_endpoint_requires_token_and_creates_nothing(self) -> None:
        source = make_pdf(self.root / "fresh.pdf", 5)
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(source)})
        project_id = created.json()["project_id"]
        # 无 Token：拒绝，且不创建任务。
        blocked = self.client.post(f"/projects/{project_id}/ocr/start", headers=self.headers, json={})
        self.assertEqual(blocked.status_code, 400, blocked.text)
        self.assertEqual(self.client.get("/ocr/jobs", headers=self.headers).json(), [])
        self.assertEqual(self.client.get("/ocr/queue", headers=self.headers).json()["entries"], [])

    def test_start_endpoint_creates_one_job_and_blocks_duplicate(self) -> None:
        source = make_pdf(self.root / "fresh.pdf", 40)
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(source)})
        project_id = created.json()["project_id"]
        # 从未解析但页数已知：区分于旧资源 unknown。
        descriptor = next(item for item in self.client.get("/projects", headers=self.headers).json() if item["project_id"] == project_id)
        self.assertEqual(descriptor["parse_coverage"]["state"], "unparsed")
        self.assertEqual(descriptor["parse_coverage"]["source_page_count"], 40)
        with patch("literature.ocr.get_mineru_token", return_value="token-value"):
            first = self.client.post(f"/projects/{project_id}/ocr/start", headers=self.headers, json={})
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()["state"], "queued")
            # 立即再次提交（并发点击语义）：已有活动作业，冲突且不重复创建。
            second = self.client.post(f"/projects/{project_id}/ocr/start", headers=self.headers, json={})
            self.assertEqual(second.status_code, 400, second.text)
        jobs = [job for job in self.client.get("/ocr/jobs", headers=self.headers).json() if job["project_id"] == project_id]
        self.assertEqual(len(jobs), 1)

    def test_continue_endpoint_creates_append_job(self) -> None:
        source = make_pdf(self.root / "scan.pdf", 6)
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(source)})
        project_id = created.json()["project_id"]
        directory = make_result_dir(self.root / "results", "cont", [1, 2, 3])
        # 直接走服务层建立带 coverage 的 document（模拟已经部分解析完成）。
        raw_root = self.store.project_path(project_id) / "ocr" / "raw" / "mineru"
        self.store.import_raw_mineru(project_id, [directory])
        merge_imported_project(
            self.store, project_id,
            [{"directory": raw_root / "slice-001", "source_pages": [1, 2, 3]}],
            source_page_count=6, append=False,
        )
        with patch("literature.api.get_mineru_token", return_value="token-value"):
            response = self.client.post(
                f"/projects/{project_id}/ocr/continue", headers=self.headers,
                json={"page_ranges": "1-6"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["skipped_pages"], [1, 2, 3])
        self.assertEqual(value["job"]["requested_pages"], [4, 5, 6])
        self.assertTrue(value["job"]["append"])
        queue = self.client.get("/ocr/queue", headers=self.headers).json()
        self.assertIn(value["job"]["job_id"], queue["order"])


if __name__ == "__main__":
    unittest.main()
