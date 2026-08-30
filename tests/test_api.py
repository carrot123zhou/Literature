from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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
        self.client = TestClient(create_app(ProjectStore(self.root / "library")))
        self.headers = {"x-literature-token": self.client.app.state.session_token}

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        os.environ.pop("LITERATURE_RUNTIME", None)
        os.environ.pop("LITERATURE_LIBRARY", None)

    def test_authenticated_project_import_and_exports(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
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
                "upload_ids": [item["upload_id"], html["upload_id"]], "language": "latin", "model_version": "pipeline",
                "is_ocr": False, "enable_table": False, "enable_formula": True,
                "extra_formats": ["docx", "latex"],
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

    def test_batch_intake_rejects_formats_not_supported_by_mineru(self) -> None:
        document = self.root / "notes.txt"
        document.write_text("not supported", encoding="utf-8")
        rejected = self.client.post("/intake/jobs", headers=self.headers, json={"paths": [str(document)]})
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("MinerU 不支持", rejected.json()["detail"])
        self.assertEqual(self.client.get("/projects", headers=self.headers).json(), [])


if __name__ == "__main__":
    unittest.main()
