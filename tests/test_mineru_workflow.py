from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from literature import mineru_workflow as workflow


class WorkflowRegressionTests(unittest.TestCase):
    def test_page_ranges_are_contiguous_without_overlap(self):
        self.assertEqual(workflow.plan_page_ranges(401, 200), [(1, 200), (201, 400), (401, 401)])

    def test_real_large_pdf_is_split_into_valid_non_overlapping_files(self):
        import fitz
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "large.pdf"
            document = fitz.open()
            for _ in range(401):
                document.new_page(width=300, height=400)
            document.save(source)
            document.close()
            pieces = workflow.split_pdf(source, root / "split", page_limit=200)
            self.assertEqual([(start, end) for start, end, _path in pieces], [(1, 200), (201, 400), (401, 401)])
            counts = []
            for _start, _end, path in pieces:
                with fitz.open(path) as part:
                    counts.append(part.page_count)
            self.assertEqual(counts, [200, 200, 1])

    def test_language_mapping_and_friendly_names(self):
        self.assertEqual(workflow.epub_language_for_mineru("japan"), "ja")
        self.assertEqual(workflow.epub_language_for_mineru("latin"), "und")
        self.assertEqual(workflow.friendly_project_name("Book (Author) (z-library.sk).pdf"), "Book (Author)")

    def test_job_roundtrip_does_not_persist_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            manager = workflow.MinerUWorkflow(root / "jobs")
            job = manager.create_job(source, root / "book.epub")
            self.assertNotIn("token", job.job_file.read_text(encoding="utf-8").casefold())
            self.assertEqual(manager.load_job(job.job_file).job_id, job.job_id)

    def test_safe_extract_rejects_traversal_and_accepts_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsafe = root / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("../../outside", "bad")
            with self.assertRaises(workflow.WorkflowError):
                workflow.safe_extract_result(unsafe, root / "unsafe")
            valid = root / "valid.zip"
            with zipfile.ZipFile(valid, "w") as archive:
                archive.writestr("result/full.md", "# result")
            output = workflow.safe_extract_result(valid, root / "result")
            self.assertTrue((output / "full.md").is_file())

    def test_api_limits_remain_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "book.pdf"
            source.write_bytes(b"pdf")
            manager = workflow.MinerUWorkflow(root / "jobs")
            with self.assertRaises(workflow.WorkflowError):
                manager.create_job(source, root / "book.epub", page_limit=201)
            with self.assertRaises(workflow.WorkflowError):
                manager.create_job(source, root / "book.epub", language="xx")

    def test_slice_progress_reports_real_completed_pages(self):
        job = workflow.WorkflowJob(
            job_id="progress", source_pdf="/tmp/book.pdf", output_epub="/tmp/book.epub",
            workspace="/tmp/progress", raw_output="/tmp/raw", source_size=1, source_sha256="digest",
            page_count=401, state=workflow.JobState.PROCESSING,
            slices=[
                workflow.SliceJob(1, 1, 200, "one", "one.pdf", "/tmp/one.pdf", state=workflow.SliceState.DOWNLOADED),
                workflow.SliceJob(2, 201, 400, "two", "two.pdf", "/tmp/two.pdf", state=workflow.SliceState.REMOTE_DONE),
                workflow.SliceJob(3, 401, 401, "three", "three.pdf", "/tmp/three.pdf", state=workflow.SliceState.PROCESSING),
            ],
        )
        from literature.ocr import MinerUService
        status = MinerUService.describe(job)
        self.assertEqual(status["slice_count"], 3)
        self.assertEqual(status["completed_slices"], 2)
        self.assertEqual(status["completed_pages"], 400)
        self.assertAlmostEqual(status["stage_progress"], 400 / 401)


if __name__ == "__main__":
    unittest.main()
