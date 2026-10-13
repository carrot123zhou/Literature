from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from literature.mineru import merge_imported_project
from literature.mineru_workflow import SCHEMA_VERSION, MinerUWorkflow, WorkflowError
from literature.ocr import MinerUService
from literature.storage import ProjectStore, StorageError, atomic_json
from literature.task_history import JobArchiveStore, select_auto_archive


def make_pdf(path: Path, pages: int) -> Path:
    import fitz

    document = fitz.open()
    for _ in range(pages):
        document.new_page(width=300, height=400)
    document.save(path)
    document.close()
    return path


def make_result_dir(root: Path, name: str, pages: list[int]) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    content = [[{"type": "paragraph", "bbox": [10, 10, 200, 60], "content": {"paragraph_content": [{"content": f"page {page}"}]}}] for page in pages]
    geometry = [{"page_idx": index, "page_size": [300, 400]} for index in range(len(pages))]
    (directory / f"{name}_content_list_v2.json").write_text(json.dumps(content), encoding="utf-8")
    (directory / f"{name}_middle.json").write_text(json.dumps({"pdf_info": geometry}), encoding="utf-8")
    return directory


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class TaskHistoryBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        self.service = MinerUService(self.store, self.root / "runtime")
        source = make_pdf(self.root / "book.pdf", 6)
        self.project_id = self.store.create_project(source, title="History Book")["project_id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def seed_job(
        self, job_id: str, *, state: str = "completed", finished_at: str = "",
        created_at: str = "", updated_at: str = "", source_name: str = "book.pdf",
    ) -> str:
        workspace = self.service.jobs_root / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        created = created_at or iso(datetime.now(timezone.utc))
        value = {
            "job_id": job_id, "source_pdf": "/tmp/x.pdf", "output_epub": "/tmp/x.epub",
            "workspace": str(workspace), "raw_output": "/tmp/raw", "source_size": 1,
            "source_sha256": "digest", "state": state, "slices": [], "error": "",
            "created_at": created, "updated_at": updated_at or created,
            "finished_at": finished_at, "schema_version": SCHEMA_VERSION,
        }
        atomic_json(workspace / "job.json", value)
        atomic_json(workspace / "literature-bridge.json", {"project_id": self.project_id})
        return job_id


class TaskCenterTests(TaskHistoryBase):
    def test_recent_completed_only_three_and_24h_window(self) -> None:
        now = datetime.now(timezone.utc)
        self.seed_job("c1", finished_at=iso(now - timedelta(hours=1)))
        self.seed_job("c2", finished_at=iso(now - timedelta(hours=2)))
        self.seed_job("c3", finished_at=iso(now - timedelta(hours=3)))
        self.seed_job("c4", finished_at=iso(now - timedelta(hours=4)))
        self.seed_job("old", finished_at=iso(now - timedelta(hours=30)))
        recent = self.service.task_center(scope="recent_completed")
        self.assertEqual([item["job_id"] for item in recent["items"]], ["c1", "c2", "c3"])
        # 边界：now-24h+60s 仍在窗口内；now-24h-60s 在外（直接测纯函数，不受 3 条上限干扰）。
        from literature.task_history import filter_recent_completed
        boundary = filter_recent_completed([
            {"job_id": "edge_in", "state": "completed", "archived": False,
             "finished_at": iso(now - timedelta(hours=24) + timedelta(seconds=60)), "created_at": iso(now)},
            {"job_id": "edge_out", "state": "completed", "archived": False,
             "finished_at": iso(now - timedelta(hours=24) - timedelta(seconds=60)), "created_at": iso(now)},
        ], now=now, limit=10)
        ids = {item["job_id"] for item in boundary}
        self.assertIn("edge_in", ids)
        self.assertNotIn("edge_out", ids)

    def test_recent_completed_stable_same_timestamp(self) -> None:
        moment = iso(datetime.now(timezone.utc) - timedelta(hours=1))
        self.seed_job("same-a", finished_at=moment, created_at=iso(datetime.now(timezone.utc) - timedelta(hours=5)))
        self.seed_job("same-b", finished_at=moment, created_at=iso(datetime.now(timezone.utc) - timedelta(hours=4)))
        self.seed_job("same-c", finished_at=moment, created_at=iso(datetime.now(timezone.utc) - timedelta(hours=3)))
        first = [item["job_id"] for item in self.service.task_center(scope="recent_completed")["items"]]
        second = [item["job_id"] for item in self.service.task_center(scope="recent_completed")["items"]]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 3)

    def test_active_failed_not_limited_and_paginated(self) -> None:
        for index in range(5):
            self.seed_job(f"f{index}", state="failed")
        self.seed_job("q0", state="queued")
        first = self.service.task_center(scope="active", page=1, page_size=10)
        self.assertEqual(first["counts"]["failed"], 5)
        self.assertEqual(first["counts"]["queued"], 1)
        self.assertEqual(len(first["items"]), 6)
        self.assertEqual(len([item for item in first["items"] if item["state"] == "failed"]), 5)
        self.assertEqual(first["pages"], 1)
        # 失败超过 3 条仍可逐页翻到；分页边界正确。
        paged = self.service.task_center(scope="active", page=1, page_size=2)
        self.assertEqual(paged["pages"], 3)
        self.assertEqual(self.service.task_center(scope="active", page=3, page_size=2)["total"], 6)
        self.assertEqual(len(self.service.task_center(scope="active", page=3, page_size=2)["items"]), 2)

    def test_history_filter_and_pagination(self) -> None:
        for index in range(25):
            self.seed_job(f"job-{index:02d}", state="completed" if index % 2 else "failed",
                          finished_at=iso(datetime.now(timezone.utc) - timedelta(minutes=index)),
                          source_name="Alpha.pdf" if index < 10 else "Beta.pdf")
        history = self.service.task_center(scope="history", page=1, page_size=20)
        self.assertEqual(history["total"], 25)
        self.assertEqual(len(history["items"]), 20)
        page2 = self.service.task_center(scope="history", page=2, page_size=20)
        self.assertEqual(len(page2["items"]), 5)
        filtered = self.service.task_center(scope="history", status="failed")
        self.assertEqual(filtered["total"], 13)
        searched = self.service.task_center(scope="history", query="job-0")
        self.assertEqual(searched["total"], 10)

    def test_legacy_completed_without_finished_at_uses_updated_at(self) -> None:
        moment = iso(datetime.now(timezone.utc) - timedelta(days=3))
        self.seed_job("legacy", state="completed", finished_at="", updated_at=moment,
                      created_at=iso(datetime.now(timezone.utc) - timedelta(days=30)))
        value = self.service.get("legacy")
        self.assertEqual(value["finished_at"], moment)
        self.assertTrue(value["finished_at_estimated"])
        self.assertNotEqual(value["finished_at"], value["created_at"])


class ArchiveTests(TaskHistoryBase):
    def test_retention_90_days_archives_older_only(self) -> None:
        now = datetime.now(timezone.utc)
        self.seed_job("old", finished_at=iso(now - timedelta(days=91)))
        self.seed_job("new", finished_at=iso(now - timedelta(days=89)))
        result = self.service.auto_archive(force=True, now=now)
        self.assertIn("old", result["archived"])
        self.assertTrue(self.service.get("old")["archived"])
        self.assertEqual(self.service.get("old")["archive_reason"], "retention")
        self.assertFalse(self.service.get("new")["archived"])

    def test_capacity_archives_oldest_first(self) -> None:
        now = datetime.now(timezone.utc)
        self.service.set_archive_settings(retention_days=None, max_records=2)
        for index in range(5):
            self.seed_job(f"cap-{index}", finished_at=iso(now - timedelta(hours=index + 1)))
        result = self.service.auto_archive(force=True, now=now)
        self.assertEqual(sorted(result["archived"]), ["cap-2", "cap-3", "cap-4"])
        self.assertEqual(self.service.get("cap-2")["archive_reason"], "capacity")
        self.assertFalse(self.service.get("cap-0")["archived"])
        self.assertFalse(self.service.get("cap-1")["archived"])

    def test_forever_disables_time_but_keeps_capacity(self) -> None:
        now = datetime.now(timezone.utc)
        self.service.set_archive_settings(retention_days=None, max_records=1)
        for index in range(3):
            self.seed_job(f"forever-{index}", finished_at=iso(now - timedelta(days=500 + index)))
        result = self.service.auto_archive(force=True, now=now)
        self.assertEqual(sorted(result["archived"]), ["forever-1", "forever-2"])
        for job_id in result["archived"]:
            self.assertEqual(self.service.get(job_id)["archive_reason"], "capacity")

    def test_daily_pass_idempotent_and_persisted(self) -> None:
        now = datetime.now(timezone.utc)
        self.seed_job("old", finished_at=iso(now - timedelta(days=200)))
        first = self.service.auto_archive(now=now)
        self.assertFalse(first["skipped"])
        self.assertIn("old", first["archived"])
        second = self.service.auto_archive(now=now + timedelta(hours=1))
        self.assertTrue(second["skipped"])
        self.assertEqual(second["archived"], [])
        # 新实例读持久化时间：重启后当天仍跳过。
        restarted = MinerUService(self.store, self.root / "runtime")
        self.assertFalse(restarted.archive.due(now=now + timedelta(hours=2)))
        self.assertTrue(restarted.auto_archive(now=now + timedelta(hours=2))["skipped"])
        later = restarted.auto_archive(now=now + timedelta(hours=25))
        self.assertFalse(later["skipped"])

    def test_manual_archive_rejects_active_states(self) -> None:
        for state in ("queued", "waiting", "interrupted", "processing"):
            self.seed_job(f"act-{state}", state=state)
            with self.assertRaises(StorageError):
                self.service.archive_job(f"act-{state}")

    def test_manual_archive_unarchive_and_retry_clears_marker(self) -> None:
        self.seed_job("fail", state="failed")
        archived = self.service.archive_job("fail")
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["archive_reason"], "manual")
        # 归档后从首页当前任务移出。
        active_ids = {item["job_id"] for item in self.service.task_center(scope="active")["items"]}
        self.assertNotIn("fail", active_ids)
        # 取消归档找回。
        restored = self.service.unarchive_job("fail")
        self.assertFalse(restored["archived"])
        self.assertIn("fail", {item["job_id"] for item in self.service.task_center(scope="active")["items"]})
        # 重试已归档失败任务会自动清标记。
        self.service.archive_job("fail")
        self.assertTrue(self.service.get("fail")["archived"])
        self.service.retry("fail")
        value = self.service.get("fail")
        self.assertFalse(value["archived"])
        self.assertEqual(value["state"], "queued")

    def test_completed_manual_archive(self) -> None:
        self.seed_job("done", state="completed", finished_at=iso(datetime.now(timezone.utc)))
        value = self.service.archive_job("done")
        self.assertTrue(value["archived"])
        self.assertEqual(value["archive_reason"], "manual")
        self.assertEqual(value["state"], "completed")

    def test_completed_cannot_be_started_or_retried(self) -> None:
        self.seed_job("done", state="completed", finished_at=iso(datetime.now(timezone.utc)))
        with self.assertRaises(StorageError):
            self.service.start_job("done")
        with self.assertRaises((StorageError, WorkflowError)):
            self.service.retry_job("done")
        self.assertEqual(self.service.get("done")["state"], "completed")
        self.assertFalse(self.service.get("done")["archived"])

    def test_archive_retry_race_keeps_invariant(self) -> None:
        """并发归档 vs 重试：归档先持锁完成，retry 随后变 queued 并清标记；不允许活动任务带归档。"""
        self.seed_job("race", state="failed")
        service = self.service
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        real_archive_one = service.archive.archive_one

        def blocking_archive_one(job_id, *, reason, auto, now=None):
            entered.set()
            release.wait(timeout=5)
            return real_archive_one(job_id, reason=reason, auto=auto, now=now)

        with patch.object(service.archive, "archive_one", side_effect=blocking_archive_one):
            def archive_worker():
                try:
                    service.archive_job("race")
                except BaseException as error:  # noqa: BLE001 - 记录线程内异常供断言
                    errors.append(error)

            def retry_worker():
                try:
                    service.retry_job("race")
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            archive_thread = threading.Thread(target=archive_worker)
            archive_thread.start()
            self.assertTrue(entered.wait(timeout=5), "archive 未进入临界区")
            retry_thread = threading.Thread(target=retry_worker)
            retry_thread.start()
            # retry 应在 schedule_lock → job_lock 上等待 archive 完成，而不是并发改写状态。
            time.sleep(0.25)
            release.set()
            archive_thread.join(timeout=5)
            retry_thread.join(timeout=5)
        self.assertEqual(errors, [])
        value = service.get("race")
        self.assertEqual(value["state"], "queued")
        self.assertFalse(value["archived"])
        self.assertIn("race", service.queue.order())
        self.assert_no_active_archived(service)

    def test_retry_then_archive_is_rejected(self) -> None:
        self.seed_job("seq", state="failed")
        self.service.retry_job("seq")
        self.assertEqual(self.service.get("seq")["state"], "queued")
        with self.assertRaises(StorageError):
            self.service.archive_job("seq")
        self.assertFalse(self.service.get("seq")["archived"])
        self.assert_no_active_archived(self.service)

    def test_auto_archive_never_marks_active(self) -> None:
        self.seed_job("q", state="queued")
        self.seed_job("w", state="waiting")
        self.seed_job("i", state="interrupted")
        self.seed_job("r", state="processing")
        self.seed_job("f", state="failed")
        self.service.auto_archive(force=True, now=datetime.now(timezone.utc))
        self.assert_no_active_archived(self.service)
        self.assertFalse(self.service.get("f")["archived"])

    def assert_no_active_archived(self, service: MinerUService) -> None:
        active_states = {"queued", "preparing", "uploading", "processing", "downloading", "merging", "waiting", "interrupted"}
        for job in service.list():
            if job["state"] in active_states:
                self.assertFalse(job["archived"], f"活动任务不得归档：{job['job_id']} {job['state']}")

    def test_archive_keeps_job_and_raw_evidence(self) -> None:
        directory = make_result_dir(self.root / "results", "raw", [1, 2, 3])
        self.store.import_raw_mineru(self.project_id, [directory])
        raw_root = self.store.project_path(self.project_id) / "ocr" / "raw" / "mineru"
        merge_imported_project(
            self.store, self.project_id,
            [{"directory": raw_root / "slice-001", "source_pages": [1, 2, 3]}],
            source_page_count=6, append=False,
        )
        coverage_before = self.service.project_coverage(self.project_id)
        raw_manifest = self.store.project_path(self.project_id) / "ocr" / "raw" / "manifest.json"
        manifest_digest = hashlib.sha256(raw_manifest.read_bytes()).hexdigest()
        self.seed_job("evidence", state="completed", finished_at=iso(datetime.now(timezone.utc)))
        job_file = self.service._job_path("evidence")
        job_digest = hashlib.sha256(job_file.read_bytes()).hexdigest()
        self.service.archive_job("evidence")
        self.assertTrue(self.service.job_exists("evidence"))
        self.assertTrue(self.service._bridge_path("evidence").is_file())
        self.assertEqual(hashlib.sha256(raw_manifest.read_bytes()).hexdigest(), manifest_digest)
        self.assertEqual(hashlib.sha256(job_file.read_bytes()).hexdigest(), job_digest)
        self.assertEqual(self.service.project_coverage(self.project_id), coverage_before)
        self.assertEqual(self.store.verify_raw(self.project_id), [])


class ArchiveStoreTests(unittest.TestCase):
    def test_invalid_retention_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobArchiveStore(Path(temporary) / "archive.json")
            with self.assertRaises(ValueError):
                store.update_settings(retention_days=7)
            self.assertEqual(store.update_settings(retention_days=None, max_records=5), {"retention_days": None, "max_records": 5})

    def test_write_failure_does_not_publish_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "archive.json"
            store = JobArchiveStore(path)
            with patch("literature.task_history._atomic_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.archive_one("job-1", reason="manual", auto=False)
                self.assertIsNone(store.record("job-1"))
                with self.assertRaises(OSError):
                    store.update_settings(retention_days=30)
                self.assertEqual(store.settings()["retention_days"], 90)
            self.assertFalse(path.exists())
            # 写恢复后重试成功，且磁盘与内存一致。
            store.archive_one("job-1", reason="manual", auto=False)
            self.assertIsNotNone(store.record("job-1"))
            self.assertTrue(JobArchiveStore(path).is_archived("job-1"))
            with patch("literature.task_history._atomic_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.unarchive("job-1")
            self.assertIsNotNone(store.record("job-1"))
            self.assertTrue(store.unarchive("job-1"))
            self.assertFalse(store.is_archived("job-1"))

    def test_select_auto_archive_pure(self) -> None:
        now = datetime.now(timezone.utc)
        jobs = [
            {"job_id": "a", "state": "completed", "finished_at": iso(now - timedelta(days=100)), "archived": False},
            {"job_id": "b", "state": "completed", "finished_at": iso(now - timedelta(days=1)), "archived": False},
            {"job_id": "c", "state": "failed", "finished_at": "", "archived": False},
            {"job_id": "d", "state": "completed", "finished_at": iso(now - timedelta(days=200)), "archived": True},
        ]
        selected = select_auto_archive(jobs, {"retention_days": 90, "max_records": 1000}, now=now)
        self.assertEqual([item[0] for item in selected], ["a"])
        self.assertEqual(selected[0][1], "retention")


class ApiTaskHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
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
        self.service = self.client.app.state.ocr_service
        source = make_pdf(self.root / "book.pdf", 4)
        self.project_id = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(source)}).json()["project_id"]

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        os.environ.pop("LITERATURE_RUNTIME", None)

    def _seed(self, job_id: str, state: str) -> None:
        workspace = self.service.jobs_root / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        created = iso(datetime.now(timezone.utc))
        atomic_json(workspace / "job.json", {
            "job_id": job_id, "source_pdf": "/tmp/x.pdf", "output_epub": "/tmp/x.epub",
            "workspace": str(workspace), "raw_output": "/tmp/raw", "source_size": 1,
            "source_sha256": "digest", "state": state, "slices": [], "error": "",
            "created_at": created, "updated_at": created, "finished_at": created,
            "schema_version": SCHEMA_VERSION,
        })
        atomic_json(workspace / "literature-bridge.json", {"project_id": self.project_id})

    def test_tasks_endpoint_and_counts(self) -> None:
        self._seed("f1", "failed")
        self._seed("q1", "queued")
        response = self.client.get("/ocr/tasks", headers=self.headers, params={"scope": "active"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["counts"]["failed"], 1)
        self.assertEqual(body["counts"]["queued"], 1)

    def test_archive_endpoints(self) -> None:
        self._seed("q1", "queued")
        self._seed("c1", "completed")
        rejected = self.client.post("/ocr/jobs/q1/archive", headers=self.headers)
        self.assertEqual(rejected.status_code, 400, rejected.text)
        archived = self.client.post("/ocr/jobs/c1/archive", headers=self.headers)
        self.assertEqual(archived.status_code, 200, archived.text)
        self.assertTrue(archived.json()["archived"])
        restored = self.client.post("/ocr/jobs/c1/unarchive", headers=self.headers)
        self.assertFalse(restored.json()["archived"])
        missing = self.client.get("/ocr/jobs/nope", headers=self.headers)
        self.assertEqual(missing.status_code, 404)

    def test_archive_settings_endpoint(self) -> None:
        default = self.client.get("/ocr/archive-settings", headers=self.headers).json()
        self.assertEqual(default, {"retention_days": 90, "max_records": 1000})
        updated = self.client.put("/ocr/archive-settings", headers=self.headers,
                                  json={"retention_days": 30, "max_records": 50})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json(), {"retention_days": 30, "max_records": 50})
        invalid = self.client.put("/ocr/archive-settings", headers=self.headers, json={"retention_days": 7})
        self.assertEqual(invalid.status_code, 400, invalid.text)


if __name__ == "__main__":
    unittest.main()
