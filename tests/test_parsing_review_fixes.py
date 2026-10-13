"""第二轮 Lead 复查缺陷的回归测试。

覆盖：源页定位契约、并发 continue、封存事务恢复、旧任务/旧资源迁移、
document 写锁、解析完整性校验、队列状态限制。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from literature import storage as storage_module
from literature.mineru import MinerUImportError, _parse_slice, merge_imported_project
from literature.mineru_workflow import JobState, MinerUWorkflow, SliceJob, SliceState
from literature.models import TranslationEntry, TranslationLayer
from literature.ocr import MinerUService
from literature.storage import ProjectStore, StorageError, atomic_json
from literature.structure import apply_patch
from tests.test_parsing_lifecycle import make_pdf, make_result_dir


class ParsingReviewFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        self.source = make_pdf(self.root / "book.pdf", 9)
        project = self.store.create_project(self.source, title="Review")
        self.project_id = project["project_id"]
        self.project = self.store.project_path(self.project_id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _merge(self, name: str, pages: list[int], *, total: int = 9, append: bool = False):
        sealed = self.store.seal_raw_mineru(self.project_id, [make_result_dir(self.root / "results", name, pages)])
        return merge_imported_project(
            self.store, self.project_id,
            [{"directory": Path(sealed[0]["destination"]), "source_pages": pages}],
            source_page_count=total, append=append,
        )

    # ---- 1. 源页定位契约 ------------------------------------------------
    def test_page_index_is_source_page_index(self) -> None:
        document = self._merge("front", [5, 7])
        self.assertEqual([page.page_index for page in document.pages], [4, 6])
        self.assertEqual([page.source_page_index for page in document.pages], [4, 6])
        self.assertEqual([block.page_index for block in document.blocks], [4, 4, 6, 6])
        self.assertEqual(document.page_count, 9)

    def test_source_page_index_survives_appending_front_pages(self) -> None:
        self._merge("front", [5, 7])
        document = self._merge("head", [1, 2, 3, 4], append=True)
        by_page = {page.page_index: page.source_page_index for page in document.pages}
        self.assertEqual(by_page, {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 6: 6})
        self.assertEqual(sorted({block.page_index for block in document.blocks}), [0, 1, 2, 3, 4, 6])
        # 第 5/7 页仍然落在源页 4/6，而不是被压缩到 0/1。
        self.assertEqual([block.page_index for block in document.blocks if block.page_index >= 4], [4, 4, 6, 6])

    # ---- 2. 并发 continue ----------------------------------------------
    def test_concurrent_continue_creates_single_job(self) -> None:
        self._merge("part", [1, 2, 3])
        service = MinerUService(self.store, self.root / "runtime")
        original = service.active_job

        def slow_active(project_id: str):
            time.sleep(0.25)
            return original(project_id)

        results: list[tuple[str, object]] = []
        with patch.object(service, "active_job", side_effect=slow_active):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(service.continue_parse, self.project_id, page_ranges="4-9") for _ in range(2)]
                for future in futures:
                    try:
                        results.append(("ok", future.result()))
                    except Exception as error:  # noqa: BLE001
                        results.append(("err", type(error).__name__))
        self.assertEqual(len([item for item in results if item[0] == "ok"]), 1)
        self.assertEqual(len([item for item in results if item[0] == "err"]), 1)
        active = [job for job in service.list(self.project_id) if job["state"] in {"queued", "waiting", "interrupted"}]
        self.assertEqual(len(active), 1)

    def test_start_and_reorder_reject_invalid_states(self) -> None:
        directory = make_result_dir(self.root / "results", "states", [1, 2])
        service = MinerUService(self.store, self.root / "runtime")
        job = service.create(self.project_id, language="ch")
        job_id = job["job_id"]
        # 未入队的 queued 任务可启动。
        self.assertEqual(service.start_job(job_id), "queued")
        # 人为置为 completed 后不能再 start，也不能调序。
        path = service._job_path(job_id)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["state"] = "completed"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(StorageError):
            service.start_job(job_id)
        with self.assertRaises(StorageError):
            service.reorder_job(job_id, "top")

    # ---- 3. 封存事务恢复 ------------------------------------------------
    def test_seal_recovers_after_manifest_write_failure(self) -> None:
        directory = make_result_dir(self.root / "results", "crash", [1, 2])
        original = storage_module.atomic_json
        manifest_suffix = "ocr/raw/manifest.json"

        def failing(path, value):
            if str(path).endswith(manifest_suffix):
                raise OSError("injected manifest failure")
            return original(path, value)

        with patch.object(storage_module, "atomic_json", side_effect=failing):
            with self.assertRaises(OSError):
                self.store.seal_raw_mineru(self.project_id, [directory])
        # 不应留下阻塞孤儿；重试成功且不覆盖旧证据。
        sealed = self.store.seal_raw_mineru(self.project_id, [directory])
        self.assertFalse(sealed[0]["reused"])
        destination = Path(sealed[0]["destination"])
        self.assertTrue(destination.is_dir())
        manifest = json.loads((self.project / "ocr" / "raw" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["slices"]), 1)
        again = self.store.seal_raw_mineru(self.project_id, [directory])
        self.assertTrue(again[0]["reused"])
        self.assertEqual(len(json.loads((self.project / "ocr" / "raw" / "manifest.json").read_text(encoding="utf-8"))["slices"]), 1)

    def test_seal_deduplicates_repeated_sources(self) -> None:
        directory = make_result_dir(self.root / "results", "dup", [1, 2])
        sealed = self.store.seal_raw_mineru(self.project_id, [directory, directory])
        self.assertEqual(len(sealed), 2)
        self.assertEqual(sealed[0]["destination"], sealed[1]["destination"])
        manifest = json.loads((self.project / "ocr" / "raw" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["slices"]), 1)

    def test_seal_adopts_orphan_slice_without_blocking(self) -> None:
        # 模拟旧实现“先移动、后写 manifest”崩溃留下的完整孤儿目录。
        raw_root = self.project / "ocr" / "raw" / "mineru"
        raw_root.mkdir(parents=True, exist_ok=True)
        orphan = make_result_dir(raw_root, "slice-001", [1, 2])
        directory = make_result_dir(self.root / "results", "next", [3, 4])
        sealed = self.store.seal_raw_mineru(self.project_id, [directory])
        manifest = json.loads((raw_root.parent / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["slices"]), 2)
        adopted = next(item for item in manifest["slices"] if item["slice"] == "slice-001")
        self.assertTrue(adopted["files"])
        self.assertEqual(sealed[0]["destination"], str(raw_root / "slice-002"))

    # ---- 4. 旧任务/旧资源迁移 -------------------------------------------
    def test_finish_import_derives_source_pages_for_legacy_slices(self) -> None:
        from literature.mineru import import_mineru

        project = self.store.create_project(make_pdf(self.root / "legacy9.pdf", 9), title="LegacyJob")
        project_id = project["project_id"]
        import_mineru(self.store, project_id, [make_result_dir(self.root / "results", "legacy", [1, 2])])
        service = MinerUService(self.store, self.root / "runtime")
        directory1 = make_result_dir(self.root / "results", "legacyold1", [3, 4])
        directory2 = make_result_dir(self.root / "results", "legacyold2", [5, 6])
        job = service.workflow.create_job(
            self.store.project_path(project_id) / "source" / "origin.pdf",
            self.store.project_path(project_id) / "ocr" / ".x.epub",
        )
        job.slices = [
            SliceJob(1, 3, 4, "d1", "a.pdf", "/tmp/a.pdf", state=SliceState.DOWNLOADED, result_directory=str(directory1)),
            SliceJob(2, 5, 6, "d2", "b.pdf", "/tmp/b.pdf", state=SliceState.DOWNLOADED, result_directory=str(directory2)),
        ]
        service.workflow.save_job(job)
        atomic_json(service._bridge_path(job.job_id), {"project_id": project_id})
        from literature.parse_backends import ParseResult
        # 旧任务兼容：ParseResult 未带 source_pages 时回退到切片推导。
        service._finish_import(job, ParseResult(
            backend_id="mineru_remote", job_id=job.job_id, state=job.state.value,
            raw_directories=(str(directory1), str(directory2)), source_pages={},
        ))
        document = self.store.load_document(project_id)
        self.assertEqual(sorted({block.page_index for block in document.blocks}), [0, 1, 2, 3, 4, 5])

    def test_fake_local_parse_result_reaches_canonical_with_noncontiguous_pages(self) -> None:
        """fake local-like 后端无 SliceJob/无 Token，ParseResult 进入真实 seal/normalize/merge。"""
        from literature.parse_backends import LOCAL_BACKEND_ID, ParseResult, resolve_job_backend

        project = self.store.create_project(make_pdf(self.root / "local-like.pdf", 6), title="LocalLike")
        project_id = project["project_id"]
        from literature.mineru import import_mineru
        import_mineru(self.store, project_id, [make_result_dir(self.root / "results", "seed", [1])])
        service = MinerUService(self.store, self.root / "runtime-local")
        raw = make_result_dir(self.root / "results", "localraw", [3, 5])

        class FakeLocalBackend:
            def capabilities(self):
                return resolve_job_backend(LOCAL_BACKEND_ID)

            def prepare(self, job, *, progress=None):
                return job

            def execute(self, job, *, token=None, progress=None):
                return True

            def status(self, job):
                return job

            def result(self, job, *, progress=None):
                return ParseResult(
                    backend_id=LOCAL_BACKEND_ID, job_id=job.job_id, state=job.state.value,
                    raw_directories=(str(raw),), source_pages={str(raw): (3, 5)},
                )

        service.workflow.backend_factory = lambda wf, job: FakeLocalBackend()
        job = service.workflow.create_job(
            self.store.project_path(project_id) / "source" / "origin.pdf",
            self.store.project_path(project_id) / "ocr" / ".x.epub",
        )
        service.workflow.save_job(job)
        atomic_json(service._bridge_path(job.job_id), {"project_id": project_id})
        done = service.workflow.run_or_resume(job, token=None)
        self.assertEqual(done.state.value, "completed")
        document = self.store.load_document(project_id)
        # seed 导入了源页 1（0-based 0），本地-like 追加源页 3/5（0-based 2/4）。
        body_pages = sorted({b.page_index for b in document.blocks if b.source_text.startswith("Body")})
        self.assertEqual(body_pages, [0, 2, 4])
        self.assertTrue((self.store.project_path(project_id) / "ocr" / "raw" / "mineru").is_dir())

    def test_migrate_project_recovers_coverage_from_trusted_job(self) -> None:
        from literature.mineru import import_mineru

        project = self.store.create_project(make_pdf(self.root / "migrate.pdf", 6), title="Migrate")
        project_id = project["project_id"]
        import_mineru(self.store, project_id, [make_result_dir(self.root / "results", "mig", [1, 2, 3])])
        service = MinerUService(self.store, self.root / "runtime")
        job = service.workflow.create_job(
            self.store.project_path(project_id) / "source" / "origin.pdf",
            self.store.project_path(project_id) / "ocr" / ".x.epub",
        )
        job.state = JobState.COMPLETED
        job.slices = [SliceJob(1, 1, 3, "d", "a.pdf", "/tmp/a.pdf", state=SliceState.DOWNLOADED)]
        service.workflow.save_job(job)
        atomic_json(service._bridge_path(job.job_id), {"project_id": project_id})
        result = service.migrate_project(project_id)
        self.assertTrue(result["migrated"])
        self.assertEqual(result["coverage"]["state"], "partial")
        self.assertEqual(result["coverage"]["covered_pages"], 3)

    def test_migrate_refuses_untrusted_page_ranges_job(self) -> None:
        from literature.mineru import import_mineru

        project = self.store.create_project(make_pdf(self.root / "untrust.pdf", 6), title="Untrust")
        project_id = project["project_id"]
        import_mineru(self.store, project_id, [make_result_dir(self.root / "results", "unt", [1, 2])])
        service = MinerUService(self.store, self.root / "runtime")
        job = service.workflow.create_job(
            self.store.project_path(project_id) / "source" / "origin.pdf",
            self.store.project_path(project_id) / "ocr" / ".x.epub",
            page_ranges="1-2",
        )
        job.state = JobState.COMPLETED
        job.slices = [SliceJob(1, 1, 2, "d", "a.pdf", "/tmp/a.pdf", state=SliceState.DOWNLOADED)]
        service.workflow.save_job(job)
        atomic_json(service._bridge_path(job.job_id), {"project_id": project_id})
        result = service.migrate_project(project_id)
        self.assertFalse(result["migrated"])
        # 前缀 1-2 但无 layout / origin 页数证据：按缺证据拒绝，不猜。
        self.assertEqual(result["reason"], "prefix_no_page_evidence")

    # ---- 5. document 写锁 ----------------------------------------------
    def test_document_lock_serializes_merge_and_structure_patch(self) -> None:
        document = self._merge("base", [1, 2])
        first_block = document.blocks[0].block_id
        directory = make_result_dir(self.root / "results", "tail", [3, 4])
        sealed = self.store.seal_raw_mineru(self.project_id, [directory])
        original_save = self.store.save_document
        calls = {"n": 0}
        started = threading.Event()

        def slow_save(value):
            calls["n"] += 1
            if calls["n"] == 1:
                started.set()
                time.sleep(0.4)
            return original_save(value)

        with patch.object(self.store, "save_document", side_effect=slow_save):
            with ThreadPoolExecutor(max_workers=2) as pool:
                merge_future = pool.submit(
                    merge_imported_project, self.store, self.project_id,
                    [{"directory": Path(sealed[0]["destination"]), "source_pages": [3, 4]}],
                    source_page_count=9, append=True,
                )
                self.assertTrue(started.wait(2.0))
                patch_future = pool.submit(
                    apply_patch, self.store, self.project_id,
                    {"block_id": first_block, "operation": "set_level", "after": 2}, source="user",
                )
                merge_future.result()
                patch_future.result()
        merged = self.store.load_document(self.project_id)
        self.assertIn(2, {block.page_index for block in merged.blocks})
        self.assertIn(3, {block.page_index for block in merged.blocks})
        self.assertEqual(merged.block(first_block).level, 2)

    # ---- 6. 完整性校验 --------------------------------------------------
    def test_parse_slice_rejects_page_count_mismatch(self) -> None:
        directory = make_result_dir(self.root / "results", "mismatch", [1, 2])
        with self.assertRaises(MinerUImportError):
            _parse_slice(
                directory, self.project, self.project_id, {},
                source_pages=[1], block_ids=lambda index, seq: f"blk-{index}-{seq}",
            )

    def test_parse_slice_rejects_markdown_multi_page(self) -> None:
        directory = self.root / "results" / "md"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "full.md").write_text("# Title\n\nBody", encoding="utf-8")
        with self.assertRaises(MinerUImportError):
            _parse_slice(
                directory, self.project, self.project_id, {},
                source_pages=[1, 2], block_ids=lambda index, seq: f"blk-{index}-{seq}",
            )

    def test_parse_slice_accepts_blank_page_as_covered(self) -> None:
        directory = self.project / "ocr" / "raw" / "mineru" / "blank"
        directory.mkdir(parents=True, exist_ok=True)
        content = [[], [{"type": "paragraph", "bbox": [10, 10, 100, 40], "content": {"paragraph_content": [{"content": "x"}]}}]]
        (directory / "blank_content_list_v2.json").write_text(json.dumps(content), encoding="utf-8")
        (directory / "blank_middle.json").write_text(json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [300, 400]}, {"page_idx": 1, "page_size": [300, 400]}]}), encoding="utf-8")
        blocks, pages, _record = _parse_slice(
            directory, self.project, self.project_id, {},
            source_pages=[1, 2], block_ids=lambda index, seq: f"blk-{index}-{seq}",
        )
        self.assertEqual([page.source_page_index for page in pages], [0, 1])

    # ---- 7. resume 跳过孤儿 ---------------------------------------------
    def test_resume_all_skips_jobs_without_bridge(self) -> None:
        service = MinerUService(self.store, self.root / "runtime")
        job = service.workflow.create_job(self.source, self.root / "out.epub")
        service.workflow.save_job(job)
        # 没有 bridge：不得入队。
        self.assertEqual(service.resume_all(), 0)
        self.assertIsNone(service.queue.position(job.job_id))

    # ---- 8. 队列调序后真实执行顺序 + 重启保持 ---------------------------
    def test_queue_reorder_drives_real_execution_order(self) -> None:
        import literature.ocr as ocr_module
        from tests.manual.fake_mineru import FakeMinerU, fake_client_factory

        FakeMinerU.reset()
        service = MinerUService(self.store, self.root / "runtime-order")
        service.workflow.client_factory = fake_client_factory
        service.workflow.sleep = lambda _seconds: None
        service.workflow.poll_interval = 0.0

        def queue_job(title: str) -> str:
            source = make_pdf(self.root / f"{title}.pdf", 4)
            project = self.store.create_project(source, title=title)
            return service.create(project["project_id"], language="ch")["job_id"]

        first, second, third = queue_job("Order A"), queue_job("Order B"), queue_job("Order C")
        service.enqueue(first)
        service.enqueue(second)
        service.enqueue(third)
        service.reorder_job(third, "top")
        self.assertEqual(service.queue.order(), [third, first, second])

        with patch.object(ocr_module, "get_mineru_token", return_value="fake-token"):
            service.start_scheduler()
            deadline = time.time() + 30
            while time.time() < deadline:
                if all(service._job_state(job_id) == "completed" for job_id in (first, second, third)):
                    break
                time.sleep(0.1)
            service.stop_scheduler()
        executed_jobs = [FakeMinerU.batches[batch][0].rsplit("-001", 1)[0] for batch in FakeMinerU.executed]
        self.assertEqual(executed_jobs, [third, first, second])

    def test_queue_order_survives_restart(self) -> None:
        service = MinerUService(self.store, self.root / "runtime-restart")
        for job_id in ("a", "b", "c"):
            service.enqueue(job_id)
        service.queue.move("b", "top")
        order = service.queue.order()
        restarted = MinerUService(self.store, self.root / "runtime-restart")
        self.assertEqual(restarted.queue.order(), order)

    # ---- 9. scheduler 与调序共享临界区 -------------------------------
    def _queue_service(self, name: str) -> MinerUService:
        from tests.manual.fake_mineru import FakeMinerU, fake_client_factory

        FakeMinerU.reset()
        service = MinerUService(self.store, self.root / name)
        service.workflow.client_factory = fake_client_factory
        service.workflow.sleep = lambda _seconds: None
        service.workflow.poll_interval = 0.0
        return service

    def _queue_job(self, service: MinerUService, title: str, pages: int = 4) -> str:
        source = make_pdf(self.root / f"{title}.pdf", pages)
        project = self.store.create_project(source, title=title)
        return service.create(project["project_id"], language="ch")["job_id"]

    def test_scheduler_rechecks_order_after_selection(self) -> None:
        import literature.ocr as ocr_module
        from tests.manual.fake_mineru import FakeMinerU

        service = self._queue_service("runtime-recheck")
        first = self._queue_job(service, "ReA")
        second = self._queue_job(service, "ReB")
        service.enqueue(first)
        service.enqueue(second)
        original = ocr_module.select_next_runnable
        selected = threading.Event()
        release = threading.Event()
        state = {"n": 0}

        def blocking_select(order, state_of, runnable):
            job = original(order, state_of, runnable)
            state["n"] += 1
            if state["n"] == 1:
                selected.set()
                release.wait(timeout=10)
            return job

        with patch.object(ocr_module, "select_next_runnable", side_effect=blocking_select), \
             patch.object(ocr_module, "get_mineru_token", return_value="fake-token"):
            service.start_scheduler()
            self.assertTrue(selected.wait(5))
            # 第一次（锁外）候选已选出后调序：领取前必须重新检查。
            service.reorder_job(second, "top")
            release.set()
            deadline = time.time() + 30
            while time.time() < deadline:
                if all(service._job_state(job_id) == "completed" for job_id in (first, second)):
                    break
                time.sleep(0.1)
            service.stop_scheduler()
        executed = [FakeMinerU.batches[batch][0].rsplit("-001", 1)[0] for batch in FakeMinerU.executed]
        self.assertEqual(executed, [second, first])

    def test_shutdown_before_claim_does_not_run(self) -> None:
        import literature.ocr as ocr_module
        from tests.manual.fake_mineru import FakeMinerU

        service = self._queue_service("runtime-shutdown")
        job_id = self._queue_job(service, "Shutdown")
        service.enqueue(job_id)

        def stopping_select(order, state_of, runnable):
            service._scheduler_stop.set()
            return order[0] if order else None

        with patch.object(ocr_module, "select_next_runnable", side_effect=stopping_select), \
             patch.object(ocr_module, "get_mineru_token", return_value="fake-token"):
            service.start_scheduler()
            time.sleep(1.2)
        self.assertIsNone(service.running_job_id())
        self.assertEqual(FakeMinerU.executed, [])
        self.assertNotEqual(service._job_state(job_id), "completed")

    def test_concurrent_retry_enqueues_once(self) -> None:
        service = self._queue_service("runtime-retry")
        job_id = self._queue_job(service, "RetryOnce")
        # 设为 failed（无失败切片也允许重试），并发重试只能入队一次。
        path = service._job_path(job_id)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["state"] = "failed"
        path.write_text(json.dumps(value), encoding="utf-8")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.retry_job, job_id) for _ in range(2)]
            for future in futures:
                future.result()
        self.assertEqual(service.queue.order().count(job_id), 1)

    # ---- 10. 封存 reconcile 事务 --------------------------------------
    def test_manifest_reconcile_removes_missing_slice_record(self) -> None:
        directory = make_result_dir(self.root / "results", "rec", [1, 2])
        sealed = self.store.seal_raw_mineru(self.project_id, [directory])
        manifest_path = self.project / "ocr" / "raw" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["slices"].append({"slice": "slice-002", "source": "ghost", "files": []})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        # 重试应把缺失目录的记录剔除并回写，否则 verify_raw 永久失败。
        self.store.seal_raw_mineru(self.project_id, [directory])
        after = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual([item["slice"] for item in after["slices"]], [sealed[0]["slice"]])
        self.assertEqual(self.store.verify_raw(self.project_id), [])

    def test_manifest_reconcile_keeps_recorded_hash(self) -> None:
        directory = make_result_dir(self.root / "results", "tamper", [1, 2])
        sealed = self.store.seal_raw_mineru(self.project_id, [directory])
        manifest_path = self.project / "ocr" / "raw" / "manifest.json"
        entry = json.loads(manifest_path.read_text(encoding="utf-8"))["slices"][0]
        first = entry["files"][0]
        recorded = first["sha256"]
        target = self.project / first["path"]
        target.chmod(0o644)
        target.write_text("tampered", encoding="utf-8")
        # reconcile 不得把新算哈希冒充原证据：verify_raw 必须继续报错，且记录哈希不变。
        self.store.seal_raw_mineru(self.project_id, [directory])
        errors = self.store.verify_raw(self.project_id)
        self.assertTrue(any("哈希变化" in item for item in errors), errors)
        after = json.loads(manifest_path.read_text(encoding="utf-8"))["slices"][0]
        current = next(item["sha256"] for item in after["files"] if item["path"] == first["path"])
        self.assertEqual(current, recorded)

    # ---- 11. 401 页旧 normalize 迁移 ---------------------------------
    def test_migrate_401_page_old_document_maps_slices(self) -> None:
        from literature.mineru import import_mineru
        from literature.storage import sha256_file

        source = make_pdf(self.root / "big.pdf", 401)
        project = self.store.create_project(source, title="Big")
        project_id = project["project_id"]
        groups = [(1, 200), (201, 400), (401, 401)]
        directories = [make_result_dir(self.root / "results", f"big{i}", list(range(start, end + 1))) for i, (start, end) in enumerate(groups, 1)]
        import_mineru(self.store, project_id, directories)
        old = self.store.load_document(project_id)
        self.assertEqual(len(old.pages), 401)
        self.assertEqual(old.pages[200].source_page_index, 0)  # 旧 normalize 的局部页号重复
        service = MinerUService(self.store, self.root / "runtime-big")
        job = service.workflow.create_job(
            self.store.project_path(project_id) / "source" / "origin.pdf",
            self.store.project_path(project_id) / "ocr" / ".x.epub",
        )
        job.state = JobState.COMPLETED
        job.slices = [
            SliceJob(1, 1, 200, "d1", "a.pdf", "/tmp/a.pdf", state=SliceState.DOWNLOADED),
            SliceJob(2, 201, 400, "d2", "b.pdf", "/tmp/b.pdf", state=SliceState.DOWNLOADED),
            SliceJob(3, 401, 401, "d3", "c.pdf", "/tmp/c.pdf", state=SliceState.DOWNLOADED),
        ]
        service.workflow.save_job(job)
        atomic_json(service._bridge_path(job.job_id), {"project_id": project_id})
        result = service.migrate_project(project_id)
        self.assertTrue(result["migrated"], result)
        self.assertEqual(result["coverage"]["state"], "full")
        migrated = self.store.load_document(project_id)
        indices = [page.page_index for page in migrated.pages]
        self.assertEqual(indices, list(range(401)))
        self.assertEqual(len(set(indices)), 401)
        self.assertEqual(sha256_file(source), self.store.load_manifest(project_id)["source_sha256"])

    def test_migrate_rejects_source_change_and_wrong_slice_count(self) -> None:
        from literature.mineru import import_mineru

        source = make_pdf(self.root / "mid.pdf", 6)
        project = self.store.create_project(source, title="Mid")
        project_id = project["project_id"]
        import_mineru(self.store, project_id, [make_result_dir(self.root / "results", "mid", [1, 2, 3])])
        service = MinerUService(self.store, self.root / "runtime-mid")
        wrong = service.workflow.create_job(
            self.store.project_path(project_id) / "source" / "origin.pdf",
            self.store.project_path(project_id) / "ocr" / ".x.epub",
        )
        wrong.state = JobState.COMPLETED
        wrong.slices = [SliceJob(1, 1, 4, "d", "a.pdf", "/tmp/a.pdf", state=SliceState.DOWNLOADED)]
        service.workflow.save_job(wrong)
        atomic_json(service._bridge_path(wrong.job_id), {"project_id": project_id})
        self.assertEqual(service.migrate_project(project_id)["reason"], "completed_but_unverifiable")
        # 源文件（项目内的副本）被改：不得 full。
        copied = self.store.source_path(project_id)
        copied.write_bytes(copied.read_bytes() + b"x")
        result = service.migrate_project(project_id)
        self.assertFalse(result["migrated"])
        self.assertEqual(result["reason"], "source_changed")


def write_result_dir(root: Path, name: str, pages: list[int], *, blanks: set[int] | None = None, layout_pages: int | None = None) -> Path:
    """写入带 layout.json 的 MinerU 结果目录（模拟真实旧输出，含空白页）。"""
    blanks = blanks or set()
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    content = []
    geometry = []
    for index, source_page in enumerate(pages):
        if index in blanks:
            content.append([])
        else:
            content.append([
                {"type": "title", "bbox": [30, 30, 300, 60],
                 "content": {"level": 1, "title_content": [{"content": f"Chapter on page {source_page}"}]}},
                {"type": "paragraph", "bbox": [30, 80, 380, 200],
                 "content": {"paragraph_content": [{"content": f"Body text on source page {source_page}."}]}},
            ])
        geometry.append({"page_idx": index, "page_size": [420, 595]})
    (directory / f"{name}_content_list_v2.json").write_text(json.dumps(content), encoding="utf-8")
    if layout_pages is not None:
        geometry = geometry[:layout_pages]
    (directory / "layout.json").write_text(json.dumps({"pdf_info": geometry}), encoding="utf-8")
    return directory


class LegacyPrefixMigrationTests(unittest.TestCase):
    """旧 `page_ranges=1-N` 作业的可信连续前缀恢复。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _legacy_project(self, total: int, prefix: int, *, page_ranges: str | None = None, blanks: set[int] | None = None, layout_pages: int | None = None) -> tuple[str, Path]:
        from literature.mineru import import_mineru
        from literature.storage import sha256_file

        source = make_pdf(self.root / "book.pdf", total)
        project = self.store.create_project(source, title="Legacy Prefix")
        project_id = project["project_id"]
        directory = write_result_dir(self.root / "results", "legacy", list(range(1, prefix + 1)), blanks=blanks, layout_pages=layout_pages)
        import_mineru(self.store, project_id, [directory])
        service = MinerUService(self.store, self.root / "runtime")
        job = service.workflow.create_job(
            self.store.project_path(project_id) / "source" / "origin.pdf",
            self.store.project_path(project_id) / "ocr" / ".x.epub",
            page_ranges=page_ranges if page_ranges is not None else f"1-{prefix}",
        )
        job.state = JobState.COMPLETED
        job.slices = [SliceJob(1, 1, prefix, "d", "a.pdf", "/tmp/a.pdf", state=SliceState.DOWNLOADED)]
        service.workflow.save_job(job)
        atomic_json(service._bridge_path(job.job_id), {"project_id": project_id})
        self.assertEqual(job.source_sha256, sha256_file(self.store.source_path(project_id)))
        return project_id, self.store.source_path(project_id)

    def test_prefix_recovers_partial_and_keeps_ids_and_translation(self) -> None:
        project_id, _source = self._legacy_project(359, 50, blanks={3, 10, 20, 30, 45})
        service = MinerUService(self.store, self.root / "runtime")
        from literature.models import TranslationEntry, TranslationLayer
        from literature.structure import apply_patch

        document = self.store.load_document(project_id)
        old_ids = [block.block_id for block in document.blocks]
        first_block = old_ids[0]
        apply_patch(service.store, project_id, {"block_id": first_block, "operation": "set_level", "after": 2}, source="user")
        layer = TranslationLayer(project_id=project_id, locale="zh-CN")
        layer.entries[first_block] = TranslationEntry(block_id=first_block, text="人工译文", status="edited", locked=True)
        self.store.save_translation(layer)

        result = service.migrate_project(project_id)
        self.assertTrue(result["migrated"], result)
        self.assertEqual(result["mode"], "legacy_prefix")
        self.assertEqual(result["coverage"]["state"], "partial")
        self.assertEqual(result["coverage"]["covered_pages"], 50)
        self.assertEqual(result["coverage"]["source_page_count"], 359)
        self.assertEqual(result["coverage"]["ranges"], [[1, 50]])
        migrated = self.store.load_document(project_id)
        self.assertEqual(migrated.page_count, 359)
        self.assertEqual([page.source_page_index for page in migrated.pages], list(range(50)))
        self.assertEqual([block.block_id for block in migrated.blocks], old_ids)
        self.assertEqual(migrated.block(first_block).level, 2)
        reloaded = self.store.load_translation(project_id, "zh-CN")
        self.assertTrue(reloaded.entries[first_block].locked)
        self.assertEqual(reloaded.entries[first_block].text, "人工译文")

    def test_continue_after_prefix_splits_into_200_and_109(self) -> None:
        project_id, _source = self._legacy_project(359, 50)
        service = MinerUService(self.store, self.root / "runtime")
        self.assertTrue(service.migrate_project(project_id)["migrated"])
        job, skipped = service.continue_parse(project_id)
        self.assertEqual(skipped, [])
        self.assertEqual(job["requested_pages"][0], 51)
        self.assertEqual(job["requested_pages"][-1], 359)
        workflow_job = service.workflow.load_job(service._job_path(job["job_id"]))
        service.workflow._prepare(workflow_job, None)
        self.assertEqual([len(item.source_pages) for item in workflow_job.slices], [200, 109])
        self.assertEqual(workflow_job.slices[0].source_pages[0], 51)
        self.assertEqual(workflow_job.slices[0].source_pages[-1], 250)
        self.assertEqual(workflow_job.slices[1].source_pages[0], 251)
        self.assertEqual(workflow_job.slices[1].source_pages[-1], 359)

    def test_non_prefix_page_ranges_is_rejected(self) -> None:
        project_id, _source = self._legacy_project(20, 5, page_ranges="2-5")
        service = MinerUService(self.store, self.root / "runtime")
        result = service.migrate_project(project_id)
        self.assertFalse(result["migrated"])
        self.assertEqual(result["reason"], "page_ranges_not_trusted_prefix")

    def test_layout_page_mismatch_is_rejected(self) -> None:
        project_id, _source = self._legacy_project(20, 5, layout_pages=4)
        service = MinerUService(self.store, self.root / "runtime")
        result = service.migrate_project(project_id)
        self.assertFalse(result["migrated"])
        self.assertEqual(result["reason"], "prefix_layout_page_mismatch")

    def test_tampered_raw_fails_verification(self) -> None:
        project_id, _source = self._legacy_project(20, 5)
        service = MinerUService(self.store, self.root / "runtime")
        slice_dir = self.store.project_path(project_id) / "ocr" / "raw" / "mineru" / "slice-001"
        target = sorted(slice_dir.glob("*_content_list_v2.json"))[0]
        target.chmod(0o644)
        target.write_text("[]", encoding="utf-8")
        result = service.migrate_project(project_id)
        self.assertFalse(result["migrated"])
        self.assertEqual(result["reason"], "raw_verification_failed")

    def test_source_change_is_rejected(self) -> None:
        project_id, source = self._legacy_project(20, 5)
        service = MinerUService(self.store, self.root / "runtime")
        source.write_bytes(source.read_bytes() + b"x")
        result = service.migrate_project(project_id)
        self.assertFalse(result["migrated"])
        self.assertEqual(result["reason"], "source_changed")


if __name__ == "__main__":
    unittest.main()
