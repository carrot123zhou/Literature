from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


class ApiIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        import fitz
        pdf = fitz.open()
        pdf.new_page(width=600, height=800)
        self.source = self.root / "scan.pdf"
        pdf.save(self.source)
        pdf.close()
        self.mineru = self.root / "mineru"
        self.mineru.mkdir()
        content = [[
            {"type": "title", "bbox": [20, 20, 300, 50], "content": {"level": 1, "title_content": [{"content": "Chapter One"}]}},
            {"type": "paragraph", "bbox": [20, 70, 560, 200], "content": {"paragraph_content": [{"content": "A source paragraph for API integration testing."}]}},
        ]]
        (self.mineru / "scan_content_list_v2.json").write_text(json.dumps(content), encoding="utf-8")
        (self.mineru / "scan_middle.json").write_text(json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [600, 800]}]}), encoding="utf-8")

        from fastapi.testclient import TestClient
        from literature.api import create_app
        from literature.storage import ProjectStore
        self.store = ProjectStore(self.root / "library")
        self.client = TestClient(create_app(self.store))
        self.headers = {"x-literature-token": self.client.app.state.session_token}

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        os.environ.pop("LITERATURE_RUNTIME", None)
        os.environ.pop("LITERATURE_LIBRARY", None)

    def test_authenticated_project_import_and_exports(self) -> None:
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertRegex(health.json()["runtime_revision"], r"^[0-9a-f]{20}$")
        self.assertIn("intake_items_v1", health.json()["capabilities"])
        self.assertEqual(self.client.get("/projects").status_code, 401)
        preflight = self.client.options("/projects", headers={
            "origin": "http://127.0.0.1:1420", "access-control-request-method": "GET",
            "access-control-request-headers": "x-literature-token",
        })
        self.assertEqual(preflight.status_code, 200)
        created = self.client.post("/projects", headers=self.headers, json={
            "source_pdf": str(self.source), "source_language": "auto", "target_language": "zh-CN",
        })
        self.assertEqual(created.status_code, 200, created.text)
        project_id = created.json()["project_id"]
        imported = self.client.post(
            f"/projects/{project_id}/mineru/import", headers=self.headers, json={"paths": [str(self.mineru)]},
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        graph = imported.json()
        self.assertEqual(graph["source_language"], "en")
        self.assertEqual(graph["pages"][0]["width"], 600)
        title_id = graph["blocks"][0]["block_id"]
        language = self.client.patch(f"/projects/{project_id}", headers=self.headers, json={
            "source_language": "fr", "chapter_language_overrides": {title_id: "ja"}, "budget_usd": 12.5,
        })
        self.assertEqual(language.status_code, 200, language.text)
        self.assertEqual(language.json()["source_language"], "fr")
        self.assertEqual(language.json()["chapter_language_overrides"][title_id], "ja")
        self.assertEqual(language.json()["budget_usd"], 12.5)
        patched = self.client.post(
            f"/projects/{project_id}/structure/patches/batch", headers=self.headers,
            json={"patches": [{
                "block_id": title_id, "operation": "set_level", "after": 2,
                "confidence": 0.8, "reason": "人工批量确认", "automatic": False,
            }]},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        patch_id = patched.json()[0]["patch_id"]
        reverted = self.client.post(
            f"/projects/{project_id}/structure/patches/revert-batch", headers=self.headers,
            json={"patch_ids": [patch_id]},
        )
        self.assertEqual(reverted.status_code, 200, reverted.text)
        draft = self.client.get(f"/projects/{project_id}/glossary/draft", headers=self.headers).json()
        frozen = self.client.put(
            f"/projects/{project_id}/translations/zh-CN/glossary", headers=self.headers,
            json={"entries": {**draft, "Paradigm": "范式"}},
        )
        self.assertEqual(frozen.status_code, 200, frozen.text)
        self.assertEqual(frozen.json()["entries"]["Paradigm"], "范式")
        records = self.client.get(f"/projects/{project_id}/usage/records", headers=self.headers)
        self.assertEqual(records.json(), [])
        tbl_config = self.client.put(
            "/tbl/config", headers=self.headers,
            json={"command": "", "url": "http://127.0.0.1:5001"},
        )
        self.assertEqual(tbl_config.status_code, 200, tbl_config.text)
        self.assertFalse(tbl_config.json()["configured"])
        self.assertEqual(self.client.get("/tbl/config", headers=self.headers).json()["url"], "http://127.0.0.1:5001")
        verified = self.client.get(f"/projects/{project_id}/mineru/verify", headers=self.headers)
        self.assertEqual(verified.json(), {"valid": True, "errors": []})
        exported = self.client.post(
            f"/projects/{project_id}/exports", headers=self.headers,
            json={"mode": "source", "locale": "zh-CN", "formats": ["html", "md+json", "epub", "docx"]},
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(set(exported.json()), {"html", "md+json", "epub", "docx"})

    def test_path_and_session_boundaries(self) -> None:
        missing = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.root / "missing.pdf")})
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(self.client.get("/projects/not-real/source.pdf", headers=self.headers).status_code, 400)

    def test_project_library_metadata_rename_and_recoverable_delete(self) -> None:
        created = self.client.post("/projects", headers=self.headers, json={
            "source_pdf": str(self.source), "title": "Initial project",
        })
        self.assertEqual(created.status_code, 200, created.text)
        project_id = created.json()["project_id"]

        listed = self.client.get("/projects", headers=self.headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        descriptor = listed.json()[0]
        self.assertFalse(descriptor["has_document"])
        self.assertEqual(descriptor["asset_count"], 0)
        self.assertEqual(descriptor["export_count"], 0)
        self.assertEqual(descriptor["source_size"], self.source.stat().st_size)

        renamed = self.client.patch(
            f"/projects/{project_id}", headers=self.headers, json={"title": "Renamed before parsing"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["title"], "Renamed before parsing")
        self.assertEqual(Path(renamed.json()["path"]).name, "Renamed before parsing")

        imported = self.client.post(
            f"/projects/{project_id}/mineru/import", headers=self.headers, json={"paths": [str(self.mineru)]},
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        renamed_after_import = self.client.patch(
            f"/projects/{project_id}", headers=self.headers, json={"title": "Final library title"},
        )
        self.assertEqual(renamed_after_import.status_code, 200, renamed_after_import.text)
        self.assertEqual(renamed_after_import.json()["title"], "Final library title")
        document = self.client.get(f"/projects/{project_id}/document", headers=self.headers)
        self.assertEqual(document.json()["title"], "Final library title")

        deleted = self.client.delete(f"/projects/{project_id}", headers=self.headers)
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertTrue(deleted.json()["recoverable"])
        self.assertTrue(Path(deleted.json()["trash_path"]).is_dir())
        self.assertEqual(self.client.get("/projects", headers=self.headers).json(), [])
        self.assertEqual(self.client.get(f"/projects/{project_id}", headers=self.headers).status_code, 400)

    def test_batch_intake_creates_persistent_queued_jobs(self) -> None:
        with patch("literature.api.get_mineru_token", return_value=""):
            created = self.client.post("/intake/jobs", headers=self.headers, json={
                "paths": [str(self.source), str(self.source)],
                "language": "auto", "page_limit": 200,
            })
        self.assertEqual(created.status_code, 200, created.text)
        value = created.json()
        self.assertFalse(value["token_configured"])
        self.assertEqual(len(value["jobs"]), 1)
        job = value["jobs"][0]
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["project_state"], "ocr_queued")
        self.assertEqual(job["slice_count"], 0)
        self.assertIsNone(job["stage_progress"])
        listed = self.client.get("/ocr/jobs", headers=self.headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()[0]["job_id"], job["job_id"])
        # Remote MinerU processing cannot actually be paused, so the local API
        # does not expose a misleading per-job pause operation.
        paused = self.client.post(f"/ocr/jobs/{job['job_id']}/pause", headers=self.headers)
        self.assertEqual(paused.status_code, 404)

    def test_web_upload_accepts_mineru_document_types_and_persists_settings(self) -> None:
        document = self.root / "notes.docx"
        document.write_bytes(b"not-a-real-docx")
        uploaded = self.client.post(
            "/intake/uploads", headers=self.headers,
            files=[
                ("files", ("notes.docx", document.read_bytes(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
                ("files", ("article.html", b"<h1>Article</h1>", "text/html")),
            ],
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        item, html = uploaded.json()
        self.assertEqual(item["extension"], "docx")
        self.assertEqual(item["source_kind"], "word")
        with patch("literature.api.get_mineru_token", return_value=""):
            created = self.client.post("/intake/jobs", headers=self.headers, json={
                "items": [
                    {"upload_id": item["upload_id"], "page_ranges": "", "extra_formats": ["docx", "latex"]},
                    {"upload_id": html["upload_id"], "page_ranges": "", "extra_formats": ["html"]},
                ],
                "language": "latin", "model_version": "pipeline",
                "is_ocr": False, "enable_table": False, "enable_formula": True,
            })
        self.assertEqual(created.status_code, 200, created.text)
        jobs = created.json()["jobs"]
        job = next(value for value in jobs if value["source_type"] == "word")
        self.assertEqual(job["source_name"], "notes.docx")
        self.assertEqual(job["source_type"], "word")
        self.assertEqual(job["model_version"], "pipeline")
        self.assertEqual(job["language"], "latin")
        self.assertFalse(job["is_ocr"])
        self.assertFalse(job["enable_table"])
        self.assertEqual(job["extra_formats"], ["docx", "latex"])
        html_job = next(value for value in jobs if value["source_type"] == "html")
        self.assertEqual(html_job["model_version"], "MinerU-HTML")
        self.assertEqual(html_job["extra_formats"], [])

    def test_intake_applies_page_ranges_and_extra_formats_per_file(self) -> None:
        second = self.root / "second.pdf"
        second.write_bytes(self.source.read_bytes())
        with patch("literature.api.get_mineru_token", return_value=""):
            created = self.client.post("/intake/jobs", headers=self.headers, json={
                "items": [
                    {"path": str(self.source), "page_ranges": "1", "extra_formats": ["docx"]},
                    {"path": str(second), "page_ranges": "", "extra_formats": ["html", "latex"]},
                ],
                "language": "ch", "model_version": "vlm",
            })
        self.assertEqual(created.status_code, 200, created.text)
        jobs = {value["source_name"]: value for value in created.json()["jobs"]}
        self.assertEqual(jobs[self.source.name]["page_ranges"], "1")
        self.assertEqual(jobs[self.source.name]["extra_formats"], ["docx"])
        self.assertEqual(jobs[second.name]["page_ranges"], "")
        self.assertEqual(jobs[second.name]["extra_formats"], ["html", "latex"])

    def test_batch_intake_rejects_formats_not_supported_by_mineru(self) -> None:
        document = self.root / "notes.txt"
        document.write_text("not supported", encoding="utf-8")
        rejected = self.client.post("/intake/jobs", headers=self.headers, json={"paths": [str(document)]})
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("MinerU 不支持", rejected.json()["detail"])
        self.assertEqual(self.client.get("/projects", headers=self.headers).json(), [])

    def test_intake_rejects_unknown_newer_client_fields(self) -> None:
        rejected = self.client.post(
            "/intake/jobs", headers=self.headers,
            json={"future_items": [{"path": str(self.source)}]},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertIn("future_items", rejected.text)

    def test_app_refuses_a_partially_visible_project_library(self) -> None:
        from literature.api import create_app
        from literature.storage import ProjectStore, StorageError

        inaccessible = ProjectStore(self.root / "inaccessible-library")
        with patch.object(inaccessible, "assert_accessible", side_effect=StorageError("library hidden")):
            with self.assertRaisesRegex(RuntimeError, "library hidden"):
                create_app(inaccessible)

        filtered = ProjectStore(self.root / "filtered-library")
        with patch.object(Path, "iterdir", return_value=iter(())):
            with self.assertRaisesRegex(StorageError, "无法完整读写项目库"):
                filtered.assert_accessible()
        self.assertEqual(list(filtered.root.glob(".literature-access-*.tmp")), [])

    def test_orphaned_runtime_job_does_not_break_job_listing(self) -> None:
        with patch("literature.api.get_mineru_token", return_value=""):
            created = self.client.post("/intake/jobs", headers=self.headers, json={
                "paths": [str(self.source)], "language": "ch",
            })
        self.assertEqual(created.status_code, 200, created.text)
        project_id = created.json()["jobs"][0]["project_id"]

        # Simulate a project directory being moved manually while its runtime
        # bridge remains behind.
        self.store.delete_project(project_id)
        listed = self.client.get("/ocr/jobs", headers=self.headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), [])

    def test_failed_intake_batch_rolls_back_projects_jobs_and_uploads(self) -> None:
        uploaded = self.client.post(
            "/intake/uploads", headers=self.headers,
            files=[
                ("files", ("first.html", b"<h1>First</h1>", "text/html")),
                ("files", ("second.html", b"<h1>Second</h1>", "text/html")),
            ],
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        upload_ids = [item["upload_id"] for item in uploaded.json()]
        service = self.client.app.state.ocr_service
        original_create = service.create
        calls = 0

        def fail_second(project_id: str, **options):
            nonlocal calls
            calls += 1
            if calls == 2:
                from literature.mineru_workflow import WorkflowError
                raise WorkflowError("synthetic second item failure")
            return original_create(project_id, **options)

        with patch.object(service, "create", side_effect=fail_second), \
             patch("literature.api.get_mineru_token", return_value=""):
            created = self.client.post("/intake/jobs", headers=self.headers, json={
                "items": [{"upload_id": upload_id} for upload_id in upload_ids],
            })
        self.assertEqual(created.status_code, 400, created.text)
        self.assertIn("synthetic second item failure", created.text)
        self.assertEqual(self.client.get("/projects", headers=self.headers).json(), [])
        self.assertEqual(self.client.get("/ocr/jobs", headers=self.headers).json(), [])
        for upload_id in upload_ids:
            self.assertFalse((self.root / "runtime" / "uploads" / upload_id).exists())

    def test_mineru_connection_check_uses_saved_token_without_exposing_it(self) -> None:
        fake_client = MagicMock()
        with patch("literature.api.get_mineru_token", return_value="secret-token"), \
             patch("literature.api.MinerUClient", return_value=fake_client) as client_type:
            checked = self.client.post("/mineru/token/check", headers=self.headers)
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json(), {"configured": True, "connected": True})
        client_type.assert_called_once_with("secret-token", max_attempts=2)
        fake_client.check_token.assert_called_once_with()
        fake_client.close.assert_called_once_with()
        self.assertNotIn("secret-token", checked.text)


if __name__ == "__main__":
    unittest.main()
