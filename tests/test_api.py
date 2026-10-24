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

    def test_native_intake_inspection_is_authenticated_and_has_no_copies(self) -> None:
        self.assertEqual(self.client.post("/intake/files/inspect", json={"paths": [str(self.source)]}).status_code, 401)
        missing = self.root / "missing.pdf"
        unsupported = self.root / "notes.txt"
        unsupported.write_text("notes")
        before = self.source.read_bytes()
        response = self.client.post("/intake/files/inspect", headers=self.headers, json={
            "paths": [str(self.source), str(self.source), str(missing), str(unsupported)],
        })
        self.assertEqual(response.status_code, 200)
        value = response.json()
        self.assertEqual(len(value["files"]), 1)
        self.assertEqual(value["files"][0]["page_count"], 1)
        self.assertEqual(value["files"][0]["path"], str(self.source.resolve()))
        self.assertEqual(len(value["errors"]), 2)
        self.assertEqual(self.store.list_projects(), [])
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list((self.root / "runtime" / "uploads").iterdir()), [])
        self.assertEqual(self.client.post("/intake/files/inspect", headers=self.headers, json={"paths": []}).status_code, 422)

    def test_desktop_resource_locations_and_webview_cors(self) -> None:
        self.assertEqual(self.client.get("/desktop/resources").status_code, 401)
        response = self.client.get("/desktop/resources", headers=self.headers)
        self.assertEqual(response.json()["library"], str(self.store.root))
        self.assertIn("desktop_resources_v1", self.client.get("/health").json()["capabilities"])
        preflight = self.client.options("/projects", headers={"origin": "tauri://localhost", "access-control-request-method": "GET", "access-control-request-headers": "x-literature-token"})
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers["access-control-allow-origin"], "tauri://localhost")

    def test_document_endpoint_exposes_cross_page_flow(self) -> None:
        from literature.models import BBOX_SPACE_MINERU_1000, Block, Document, Page

        created = self.client.post("/projects", headers=self.headers, json={
            "source_pdf": str(self.source), "source_language": "en", "target_language": "zh-CN",
        })
        project_id = created.json()["project_id"]
        project = self.store.project_path(project_id)
        raw = project / "ocr" / "raw" / "mineru" / "slice-001"
        raw.mkdir(parents=True, exist_ok=True)
        relative = "ocr/raw/mineru/slice-001/book_content_list_v2.json"
        merged = "The legacy of the Chan/Zen tradition can be seen as a dialectical tension between mediate and immediate understanding"
        (project / relative).write_text(json.dumps([
            [{"type": "paragraph", "content": {"paragraph_content": [{"content": merged}]}, "bbox": [50, 760, 550, 960]}],
            [
                {"type": "paragraph", "content": {"paragraph_content": []}, "bbox": [50, 80, 550, 380]},
                {"type": "paragraph", "content": {"paragraph_content": [{"content": "When society is seen as a product of contract."}]}, "bbox": [50, 380, 550, 700]},
            ],
        ]), encoding="utf-8")
        document = Document(
            project_id=project_id, title="Flow", source_language="en", bbox_space=BBOX_SPACE_MINERU_1000,
            page_count=2, pages=[Page(page_index=index, width=600, height=800, source_page_index=index) for index in range(2)],
            source_files=[{"path": relative, "representation": "content_list_v2", "sha256": "", "page_offset": 0, "page_count": 2}],
        )
        document.blocks = [
            Block(block_id="p0", type="paragraph", order=1, page_index=0, bbox=[50, 760, 550, 960], source_text=merged),
            Block(block_id="p1", type="paragraph", order=2, page_index=1, bbox=[50, 380, 550, 700],
                  source_text="When society is seen as a product of contract."),
        ]
        self.store.save_document(document)
        payload = self.client.get(f"/projects/{project_id}/document", headers=self.headers).json()
        # MinerU 把跨页整段并入上一页的块，续页只留空占位块；派生视图给出续行区域。
        self.assertEqual(payload["flow_links"], {"p0": {"next_region": {"page_index": 1, "bbox": [50.0, 80.0, 550.0, 380.0]}}})

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
        # 跨页续接是只读派生字段，单页样例应为空但仍必须存在。
        self.assertEqual(graph["flow_links"], {})
        self.assertEqual(self.client.get(f"/projects/{project_id}/document", headers=self.headers).json()["flow_links"], {})
        title_id = graph["blocks"][0]["block_id"]
        language = self.client.patch(f"/projects/{project_id}", headers=self.headers, json={
            "source_language": "fr", "chapter_language_overrides": {title_id: "ja"}, "budget_usd": 12.5,
        })
        self.assertEqual(language.status_code, 200, language.text)
        self.assertEqual(language.json()["source_language"], "fr")
        self.assertEqual(language.json()["chapter_language_overrides"][title_id], "ja")
        self.assertEqual(language.json()["budget_usd"], 12.5)
        self.assertIn("flow_links", language.json())
        patched = self.client.post(
            f"/projects/{project_id}/structure/patches/batch", headers=self.headers,
            json={"patches": [{
                "block_id": title_id, "operation": "set_level", "after": 2,
                "confidence": 0.8, "reason": "人工批量确认", "automatic": False,
            }]},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()[0]["source"], "user")
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
        verified = self.client.get(f"/projects/{project_id}/mineru/verify", headers=self.headers)
        self.assertEqual(verified.json(), {"valid": True, "errors": []})
        # 导出与外部 TBL：TBL 继续废弃，内置导出已恢复为真实实现。
        for method, path in (("post", "tbl/start"), ("get", "tbl/config"), ("get", "tbl/status")):
            response = getattr(self.client, method)(f"/{path}", headers=self.headers)
            self.assertEqual(response.status_code, 410, response.text)
        self.assertIn("export_v1", health.json()["capabilities"])
        options = self.client.get(f"/projects/{project_id}/export/options", headers=self.headers)
        self.assertEqual(options.status_code, 200, options.text)
        self.assertEqual({i["id"] for i in options.json()["formats"]}, {"epub", "docx", "markdown", "html"})
        exported = self.client.post(
            f"/projects/{project_id}/export", headers=self.headers,
            json={"formats": ["markdown"], "mode": "source", "locale": "zh-CN"},
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertTrue(any(i["rel_path"] == "scan.md" for i in exported.json()["files"]))

    def test_export_preserves_existing_files(self) -> None:
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.source), "title": "Keep Exports"})
        project_id = created.json()["project_id"]
        imported = self.client.post(
            f"/projects/{project_id}/mineru/import", headers=self.headers, json={"paths": [str(self.mineru)]},
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        exports = Path(created.json()["path"]) / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        marker = exports / "legacy-note.md"
        marker.write_text("kept by user", encoding="utf-8")
        exported = self.client.post(
            f"/projects/{project_id}/export", headers=self.headers,
            json={"formats": ["markdown"], "mode": "source"},
        )
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), "kept by user")
        self.assertTrue(any(exports.glob("*/export.json")))
        other = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.source), "title": "No Exports"})
        self.assertFalse((Path(other.json()["path"]) / "exports").exists())

    def test_project_status_is_lightweight(self) -> None:
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.source), "title": "Status"})
        project_id = created.json()["project_id"]
        job_id = self.store.create_job(project_id, "translate", {"scope": "book"}, {})
        self.store.update_job(project_id, job_id, result={"counts": {"target_blocks": 1}})
        status = self.client.get(f"/projects/{project_id}/status", headers=self.headers)
        self.assertEqual(status.status_code, 200, status.text)
        jobs = status.json()["jobs"]
        self.assertEqual(len(jobs), 1)
        # 高频轮询接口不下发重型审计。
        self.assertNotIn("result", jobs[0])
        self.assertNotIn("activity", jobs[0])
        self.assertIn("usage", status.json())
        # 完整详情仍保留审计，供需要时读取。
        full = self.client.get(f"/projects/{project_id}", headers=self.headers).json()["jobs"][0]
        self.assertIn("result", full)

    def test_translation_revision_invalidates_on_source_change(self) -> None:
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.source), "title": "Revision"})
        project_id = created.json()["project_id"]
        imported = self.client.post(f"/projects/{project_id}/mineru/import", headers=self.headers, json={"paths": [str(self.mineru)]})
        self.assertEqual(imported.status_code, 200, imported.text)
        first = self.client.get(f"/projects/{project_id}/translations/zh-CN", headers=self.headers).json()
        revision = first["revision"]
        same = self.client.get(f"/projects/{project_id}/translations/zh-CN?since={revision}", headers=self.headers).json()
        self.assertTrue(same.get("unchanged"))
        # 改源文（document 保存）后，revision 必须失效，不能返回 unchanged。
        document = self.store.load_document(project_id)
        document.block(document.blocks[0].block_id).source_text = "Changed source text"
        self.store.save_document(document)
        changed = self.client.get(f"/projects/{project_id}/translations/zh-CN?since={revision}", headers=self.headers).json()
        self.assertFalse(changed.get("unchanged", False))
        self.assertNotEqual(changed["revision"], revision)

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
        self.assertNotIn("export_count", descriptor)
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

    def test_startup_marks_interrupted_project_jobs_failed(self) -> None:
        """上次进程遗留的 queued/running 项目作业必须被标为失败。

        否则结构校对页的恢复逻辑会把僵尸任务当成进行中任务无限重连。
        """
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.source)})
        self.assertEqual(created.status_code, 200, created.text)
        project_id = created.json()["project_id"]
        job_id = self.store.create_job(project_id, "structure_ai", {}, {})
        recovered = self.client.app.state.job_manager.recover_interrupted_jobs()
        self.assertGreaterEqual(recovered, 1)
        job = self.store.get_job(project_id, job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("中断", job["error"])
        # 已完成的任务不会被改写。
        self.store.update_job(project_id, job_id, status="completed")
        self.client.app.state.job_manager.recover_interrupted_jobs()
        self.assertEqual(self.store.get_job(project_id, job_id)["status"], "completed")

    def test_structure_baseline_round_trip(self) -> None:
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.source)})
        self.assertEqual(created.status_code, 200, created.text)
        project_id = created.json()["project_id"]
        self.assertEqual(self.client.get(f"/projects/{project_id}/structure/baseline").status_code, 401)
        saved = self.client.put(
            f"/projects/{project_id}/structure/baseline", headers=self.headers,
            json={"toc": [{"title": "Intro", "level": 1}], "source": "confirmed"},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        loaded = self.client.get(f"/projects/{project_id}/structure/baseline", headers=self.headers)
        self.assertEqual(loaded.json()["toc"], [{"title": "Intro", "level": 1}])
        self.assertEqual(loaded.json()["source"], "confirmed")

    def test_local_printed_toc_endpoint_returns_entries_without_model(self) -> None:
        directory = self.root / "with_toc"
        directory.mkdir()
        toc_text = "\n".join(f"Chapter {index}. Title {index} {index * 10}" for index in range(1, 10))
        content = [[
            {"type": "title", "bbox": [20, 20, 300, 50], "content": {"level": 1, "title_content": [{"content": "Contents"}]}},
            {"type": "paragraph", "bbox": [20, 70, 560, 700], "content": {"paragraph_content": [{"content": toc_text}]}},
        ]]
        (directory / "book_content_list_v2.json").write_text(json.dumps(content), encoding="utf-8")
        (directory / "book_middle.json").write_text(json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [600, 800]}]}), encoding="utf-8")
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.source)})
        project_id = created.json()["project_id"]
        imported = self.client.post(f"/projects/{project_id}/mineru/import", headers=self.headers, json={"paths": [str(directory)]})
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(self.client.get(f"/projects/{project_id}/structure/local-toc").status_code, 401)
        response = self.client.get(f"/projects/{project_id}/structure/local-toc", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertGreaterEqual(len(value["entries"]), 8)
        self.assertEqual(value["entries"][0]["title"], "Chapter 1. Title 1")
        self.assertEqual(value["entries"][0]["printed_page"], "10")

    def test_structure_suggestion_decision_endpoint_applies_and_records(self) -> None:
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.source)})
        project_id = created.json()["project_id"]
        imported = self.client.post(f"/projects/{project_id}/mineru/import", headers=self.headers, json={"paths": [str(self.mineru)]})
        self.assertEqual(imported.status_code, 200, imported.text)
        title_id = imported.json()["blocks"][0]["block_id"]
        self.store.save_structure_suggestions(project_id, "job-api", [{
            "block_id": title_id, "operation": "set_level", "before": 1, "after": 3, "confidence": 0.9, "reason": "test",
        }], document_updated_at="")
        pending = self.client.get(f"/projects/{project_id}/structure/suggestions", headers=self.headers).json()
        self.assertEqual(len(pending), 1)
        suggestion_id = pending[0]["suggestion_id"]
        applied = self.client.post(
            f"/projects/{project_id}/structure/suggestions/{suggestion_id}/decision",
            headers=self.headers, json={"decision": "applied"},
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        self.assertIsNotNone(applied.json()["patch"])
        self.assertEqual(applied.json()["patch"]["source"], "ai")
        self.assertFalse(applied.json()["patch"]["automatic"])
        document = self.client.get(f"/projects/{project_id}/document", headers=self.headers).json()
        block = next(item for item in document["blocks"] if item["block_id"] == title_id)
        self.assertEqual(block["level"], 3)
        self.assertEqual(self.client.get(f"/projects/{project_id}/structure/suggestions", headers=self.headers).json(), [])
        again = self.client.post(
            f"/projects/{project_id}/structure/suggestions/{suggestion_id}/decision",
            headers=self.headers, json={"decision": "applied"},
        )
        self.assertEqual(again.status_code, 400)

        manual = self.client.post(
            f"/projects/{project_id}/structure/patches", headers=self.headers,
            json={"block_id": title_id, "operation": "set_level", "before": 3, "after": 2},
        )
        self.assertEqual(manual.status_code, 200, manual.text)
        self.assertEqual(manual.json()["source"], "user")
        self.store.save_structure_suggestions(project_id, "job-api-batch", [{
            "block_id": title_id, "operation": "set_level", "before": 2, "after": 1,
            "confidence": 0.9, "reason": "batch test",
        }])
        pending = self.client.get(f"/projects/{project_id}/structure/suggestions", headers=self.headers).json()
        batch = self.client.post(
            f"/projects/{project_id}/structure/suggestions/decide-batch", headers=self.headers,
            json={"suggestion_ids": [pending[0]["suggestion_id"]], "decision": "applied"},
        )
        self.assertEqual(batch.status_code, 200, batch.text)
        self.assertEqual(batch.json()["patches"][0]["source"], "ai")
        self.assertFalse(batch.json()["patches"][0]["automatic"])
        history = self.client.get(f"/projects/{project_id}/structure/patches", headers=self.headers).json()
        by_id = {value["patch_id"]: value for value in history}
        for value in (applied.json()["patch"], manual.json(), batch.json()["patches"][0]):
            self.assertEqual(by_id[value["patch_id"]]["source"], value["source"])

        # 旧版未持久化建议的兼容入口也保留采用来源。
        for endpoint, payload in (
            ("patches", {"block_id": title_id, "operation": "set_level", "after": 2, "source": "ai"}),
            ("patches/batch", {"patches": [{"block_id": title_id, "operation": "set_level", "after": 1}], "source": "ai"}),
        ):
            response = self.client.post(f"/projects/{project_id}/structure/{endpoint}", headers=self.headers, json=payload)
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()[0] if endpoint.endswith("batch") else response.json()
            self.assertEqual(value["source"], "ai")
            self.assertFalse(value["automatic"])

    def test_project_pipeline_summarizes_independent_workflows(self) -> None:
        created = self.client.post("/projects", headers=self.headers, json={"source_pdf": str(self.source)})
        project_id = created.json()["project_id"]
        imported = self.client.post(f"/projects/{project_id}/mineru/import", headers=self.headers, json={"paths": [str(self.mineru)]})
        self.assertEqual(imported.status_code, 200, imported.text)
        graph = imported.json()
        title_id = graph["blocks"][0]["block_id"]

        descriptor = self.client.get("/projects", headers=self.headers).json()[0]
        self.assertEqual(descriptor["review"]["state"], "todo")
        self.assertEqual(descriptor["review"]["pending"], 0)
        self.assertEqual(descriptor["pipeline"]["review"]["state"], "todo")
        self.assertEqual(descriptor["pipeline"]["translate"]["state"], "none")
        self.assertNotIn("export", descriptor["pipeline"])

        self.store.save_structure_suggestions(project_id, "job-pipeline", [{
            "block_id": title_id, "operation": "set_level", "before": 1, "after": 2,
            "confidence": 0.9, "reason": "pipeline test",
        }], document_updated_at="")
        confirm = self.client.get("/projects", headers=self.headers).json()[0]
        self.assertEqual(confirm["review"]["state"], "confirm")
        self.assertEqual(confirm["review"]["pending"], 1)

        suggestion_id = self.client.get(
            f"/projects/{project_id}/structure/suggestions", headers=self.headers,
        ).json()[0]["suggestion_id"]
        applied = self.client.post(
            f"/projects/{project_id}/structure/suggestions/{suggestion_id}/decision",
            headers=self.headers, json={"decision": "applied"},
        )
        self.assertEqual(applied.status_code, 200, applied.text)
        after_apply = self.client.get("/projects", headers=self.headers).json()[0]
        # 单个补丁/采用不等于全书已校对：现在是 partial，需显式确认。
        self.assertEqual(after_apply["review"]["state"], "partial")
        confirmed = self.client.post(f"/projects/{project_id}/structure/confirm", headers=self.headers)
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        done = self.client.get("/projects", headers=self.headers).json()[0]
        self.assertEqual(done["review"]["state"], "done")
        # 确认绑定文档结构版本：文档改变后确认失效。
        patched = self.client.post(
            f"/projects/{project_id}/structure/patches", headers=self.headers,
            json={"block_id": title_id, "operation": "set_level", "after": 3, "source": "user"},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        stale = self.client.get("/projects", headers=self.headers).json()[0]
        self.assertEqual(stale["review"]["state"], "partial")

        draft = self.client.get(f"/projects/{project_id}/glossary/draft", headers=self.headers).json()
        frozen = self.client.put(
            f"/projects/{project_id}/translations/zh-CN/glossary", headers=self.headers,
            json={"entries": {**draft, "Paradigm": "范式"}},
        )
        self.assertEqual(frozen.status_code, 200, frozen.text)
        # 冻结术语表后，只要译文层被读取一次，项目摘要应立即反映 glossary_frozen。
        self.client.get(f"/projects/{project_id}/translations/zh-CN", headers=self.headers)
        frozen_summary = self.client.get("/projects", headers=self.headers).json()[0]
        self.assertTrue(frozen_summary["pipeline"]["translate"]["glossary_frozen"])

        # 写入一段人工译文并让摘要落盘，再修正标题层级：纯层级变化应自动对齐，不产生 stale。
        self.client.put(
            f"/projects/{project_id}/translations/{title_id}", headers=self.headers,
            json={"text": "第一章", "locale": "zh-CN"},
        )
        self.client.get(f"/projects/{project_id}/translations/zh-CN", headers=self.headers)
        patched = self.client.post(
            f"/projects/{project_id}/structure/patches", headers=self.headers,
            json={"block_id": title_id, "operation": "set_level", "after": 3, "source": "user"},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        aligned = self.client.get("/projects", headers=self.headers).json()[0]
        self.assertEqual(aligned["pipeline"]["translate"]["stale"], 0)
        self.assertEqual(aligned["pipeline"]["translate"]["stale_source"], 0)

        # 正文转列表会改变源字段布局：这才是需要人工复核的 source stale。
        paragraph_id = next(item["block_id"] for item in graph["blocks"] if item["type"] == "paragraph")
        self.client.put(
            f"/projects/{project_id}/translations/{paragraph_id}", headers=self.headers,
            json={"text": "人工段落", "locale": "zh-CN"},
        )
        source_patch = self.client.post(
            f"/projects/{project_id}/structure/patches", headers=self.headers,
            json={"block_id": paragraph_id, "operation": "set_type", "after": "list", "source": "user"},
        )
        self.assertEqual(source_patch.status_code, 200, source_patch.text)
        stale = self.client.get("/projects", headers=self.headers).json()[0]
        self.assertGreaterEqual(stale["pipeline"]["translate"]["stale"], 1)
        self.assertEqual(stale["pipeline"]["translate"]["state"], "partial")

        exported = self.client.post(
            f"/projects/{project_id}/export", headers=self.headers,
            json={"mode": "source", "locale": "zh-CN", "formats": ["html"]},
        )
        # 导出只是视图：产物落 exports/，不进入流水线统计。
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertNotIn("export", self.client.get("/projects", headers=self.headers).json()[0]["pipeline"])

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
