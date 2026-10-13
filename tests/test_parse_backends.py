from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literature.mineru_workflow import (
    JobState,
    MinerUWorkflow,
    RemoteMinerUBackend,
    SliceJob,
    SliceState,
    WorkflowError,
    WorkflowJob,
)
from literature.parse_backends import (
    LOCAL_BACKEND_ID,
    REMOTE_BACKEND_ID,
    ParseBackendUnavailable,
    ParseResult,
    capabilities,
    describe_backends,
    resolve_job_backend,
)


def make_pdf(path: Path, pages: int = 2) -> None:
    import fitz

    document = fitz.open()
    for _ in range(pages):
        document.new_page(width=300, height=400)
    document.save(path)
    document.close()


class ParseBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_remote_available_and_local_explicitly_unavailable(self) -> None:
        described = {item["backend_id"]: item for item in describe_backends()}
        remote = described[REMOTE_BACKEND_ID]
        local = described[LOCAL_BACKEND_ID]
        # remote 是当前已实现方式，保留上传 URL + 轮询等远端语义。
        self.assertTrue(remote["available"])
        self.assertEqual(remote["execution"], "remote")
        self.assertTrue(remote["upload_url"])
        self.assertTrue(remote["polling"])
        self.assertTrue(remote["slicing"])
        # local 只预留：明确不可用，且不被要求实现远端专属细节。
        self.assertFalse(local["available"])
        self.assertEqual(local["execution"], "local")
        self.assertFalse(local["upload_url"])
        self.assertFalse(local["polling"])
        self.assertTrue(local["unavailable_reason"])

    def test_resolve_defaults_to_remote_for_legacy_jobs(self) -> None:
        self.assertEqual(resolve_job_backend(None).backend_id, REMOTE_BACKEND_ID)
        self.assertEqual(resolve_job_backend("").backend_id, REMOTE_BACKEND_ID)
        with self.assertRaises(ParseBackendUnavailable):
            capabilities("does-not-exist")

    def test_local_backend_cannot_start_a_job(self) -> None:
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        workflow = MinerUWorkflow(self.root / "jobs")
        with self.assertRaises(WorkflowError):
            workflow.create_job(source, self.root / "out.epub", backend_id=LOCAL_BACKEND_ID)
        # 明确拒绝后不得留下半成品任务目录。
        self.assertEqual(workflow.list_jobs(), [])

    def test_legacy_job_without_backend_fields_loads_as_remote(self) -> None:
        data = {
            "schema_version": 1, "job_id": "legacy", "source_pdf": "/tmp/a.pdf",
            "output_epub": "/tmp/a.epub", "workspace": "/tmp/legacy", "raw_output": "/tmp/raw",
            "source_size": 1, "source_sha256": "x", "state": "completed", "slices": [],
        }
        job = WorkflowJob.from_dict(data)
        self.assertEqual(job.backend_id, REMOTE_BACKEND_ID)
        self.assertEqual(job.backend_config, {})

    def test_created_job_snapshots_backend_and_config(self) -> None:
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        workflow = MinerUWorkflow(self.root / "jobs")
        job = workflow.create_job(
            source, self.root / "out.epub",
            backend_id=REMOTE_BACKEND_ID, backend_config={"language_pack": "ch"},
        )
        self.assertEqual(job.backend_id, REMOTE_BACKEND_ID)
        self.assertEqual(job.backend_config, {"language_pack": "ch"})
        reloaded = workflow.load_job(workflow.jobs_root / job.job_id)
        self.assertEqual(reloaded.backend_id, REMOTE_BACKEND_ID)
        self.assertEqual(reloaded.backend_config, {"language_pack": "ch"})

    def test_remote_backend_selected_for_legacy_and_remote_jobs(self) -> None:
        source = self.root / "book.pdf"
        make_pdf(source)
        workflow = MinerUWorkflow(self.root / "jobs-adapter", merge_callback=lambda job, parse_result: None)
        job = workflow.create_job(source, self.root / "out.epub")
        backend = workflow._backend_for(job)
        self.assertIsInstance(backend, RemoteMinerUBackend)
        self.assertEqual(backend.capabilities().backend_id, REMOTE_BACKEND_ID)
        # 旧任务缺 backend_id 时仍按 remote 选适配器。
        legacy = WorkflowJob.from_dict({
            "schema_version": 1, "job_id": "legacy", "source_pdf": str(source),
            "output_epub": str(self.root / "legacy.epub"), "workspace": str(self.root / "legacy"),
            "raw_output": str(self.root / "raw"), "source_size": 1, "source_sha256": "x",
            "state": "queued", "slices": [],
        })
        self.assertIsInstance(workflow._backend_for(legacy), RemoteMinerUBackend)

    def test_run_or_resume_goes_through_backend_boundary(self) -> None:
        from tests.manual.fake_mineru import FakeMinerU, fake_client_factory

        FakeMinerU.reset()
        source = self.root / "book.pdf"
        make_pdf(source, 2)
        calls: list[str] = []

        class SpyBackend(RemoteMinerUBackend):
            def prepare(self, job, **kwargs):
                calls.append("prepare")
                return super().prepare(job, **kwargs)

            def execute(self, job, **kwargs):
                calls.append("execute")
                return super().execute(job, **kwargs)

            def status(self, job):
                calls.append("status")
                return super().status(job)

            def result(self, job, **kwargs):
                calls.append("result")
                return super().result(job, **kwargs)

        workflow = MinerUWorkflow(
            self.root / "jobs-spy", client_factory=fake_client_factory,
            merge_callback=lambda job, parse_result: calls.append("merge"),
            backend_factory=lambda wf, job: SpyBackend(wf, client_factory=wf.client_factory),
            sleep=lambda _seconds: None, poll_interval=0.0,
        )
        job = workflow.create_job(source, self.root / "out-spy.epub")
        done = workflow.run_or_resume(job, token="fake-token")
        self.assertEqual(done.state, JobState.COMPLETED)
        # 真实 prepare/execute/status/result 均被调用，remote 确实经过适配器边界。
        self.assertEqual(calls, ["prepare", "execute", "status", "result", "merge"])

    def test_local_and_unknown_jobs_never_build_client_or_read_token(self) -> None:
        source = self.root / "book.pdf"
        make_pdf(source, 1)
        created: list[str] = []
        token_reads: list[str] = []

        def client_factory(token: str):
            created.append(token)
            raise AssertionError("不可用后端不得创建远端 client")

        workflow = MinerUWorkflow(
            self.root / "jobs-reject", client_factory=client_factory,
            merge_callback=lambda job, parse_result: None,
        )
        job = workflow.create_job(source, self.root / "out-reject.epub")
        for backend_id in (LOCAL_BACKEND_ID, "does-not-exist"):
            job.backend_id = backend_id
            job.state = JobState.QUEUED
            workflow.save_job(job)
            with patch(
                "literature.mineru_workflow.get_mineru_token",
                side_effect=lambda: token_reads.append("read") or "token",
            ):
                with self.assertRaises(WorkflowError):
                    workflow.run_or_resume(job, token=None)
        self.assertEqual(created, [])
        self.assertEqual(token_reads, [])

    def test_fake_local_like_adapter_reaches_shared_merge(self) -> None:
        """本地-like 适配器无需 Token/HTTP，产 ParseResult（raw 目录 + source_pages）进入同一 merge。"""
        source = self.root / "book.pdf"
        make_pdf(source, 1)
        raw = str(self.root / "fake-raw")
        Path(raw).mkdir(parents=True, exist_ok=True)
        merged: list[tuple[str, ...]] = []

        class FakeLocalBackend:
            def capabilities(self):
                return resolve_job_backend(LOCAL_BACKEND_ID)

            def prepare(self, job, *, progress=None):
                return job

            def execute(self, job, *, token=None, progress=None):
                # 本地-like 后端不建 SliceJob、不读 Token；只产出 raw 目录。
                return True

            def status(self, job):
                return job

            def result(self, job, *, progress=None):
                return ParseResult(
                    backend_id=LOCAL_BACKEND_ID, job_id=job.job_id, state=job.state.value,
                    raw_directories=(raw,), source_pages={raw: (3, 5)},
                )

        workflow = MinerUWorkflow(
            self.root / "jobs-fake-local", merge_callback=lambda job, parse_result: merged.append(tuple(parse_result.raw_directories)),
            backend_factory=lambda wf, job: FakeLocalBackend(),
        )
        job = workflow.create_job(source, self.root / "out-fake.epub")
        done = workflow.run_or_resume(job, token=None)
        self.assertEqual(done.state, JobState.COMPLETED)
        # 协调层收到统一 ParseResult（无需远端 SliceJob）并调用共享 merge。
        self.assertEqual(merged, [(raw,)])

    def test_backend_config_snapshot_is_validated(self) -> None:
        source = self.root / "book.pdf"
        make_pdf(source)
        workflow = MinerUWorkflow(self.root / "jobs-cfg", merge_callback=lambda job, parse_result: None)
        job = workflow.create_job(
            source, self.root / "out-cfg.epub", is_ocr=True,
            backend_config={"is_ocr": True, "enable_table": True, "enable_formula": True},
        )
        backend = workflow._backend_for(job)
        backend.prepare(job)  # 一致 → 通过
        job.enable_formula = False
        with self.assertRaises(WorkflowError):
            backend.prepare(job)

    def test_backend_config_model_language_page_ranges_validated(self) -> None:
        source = self.root / "book.pdf"
        make_pdf(source, 3)
        workflow = MinerUWorkflow(self.root / "jobs-cfg2", merge_callback=lambda job, parse_result: None)
        job = workflow.create_job(
            source, self.root / "out-cfg2.epub", model_version="vlm", language="ch", page_ranges="1-2",
            backend_config={"model_version": "vlm", "language_pack": "ch", "page_ranges": "1-2"},
        )
        backend = workflow._backend_for(job)
        backend.prepare(job)  # 一致 → 通过
        job.language = "en"
        with self.assertRaises(WorkflowError):
            backend.prepare(job)
        job.language = "ch"
        job.page_ranges = "1-3"
        with self.assertRaises(WorkflowError):
            backend.prepare(job)


if __name__ == "__main__":
    unittest.main()
