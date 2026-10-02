from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from literature.providers import ProviderResponse


def span(text: str) -> dict[str, str]:
    return {"type": "text", "content": text}


class StructureMockProvider:
    """按调用类型返回固定 JSON：先目录提取，后标题校对。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        self.calls.append(user)
        if "toc_pages" in user:
            content = json.dumps({"toc": [{"title": "Chapter One", "level": 1}, {"title": "Chapter Two", "level": 1}]})
        else:
            content = json.dumps({"suggestions": []})
        return ProviderResponse(content=content, provider="mock", model="mock", input_tokens=10, output_tokens=5, request_id="req")


class ModelServiceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        import fitz
        pdf = fitz.open()
        pdf.new_page(width=600, height=800)
        pdf.new_page(width=600, height=800)
        self.source = self.root / "book.pdf"
        pdf.save(self.source)
        pdf.close()
        self.mineru = self.root / "mineru"
        self.mineru.mkdir()
        pages = [
            [
                {"type": "title", "bbox": [40, 40, 560, 100], "content": {"level": 1, "title_content": [span("Contents")]}},
                {"type": "list", "bbox": [40, 140, 560, 360], "content": {"list_items": [span("Chapter One 1"), span("Chapter Two 5")]}},
            ],
            [
                {"type": "title", "bbox": [40, 40, 560, 100], "content": {"level": 1, "title_content": [span("Chapter One")]}},
                {"type": "paragraph", "bbox": [40, 140, 560, 400], "content": {"paragraph_content": [span("Body text for the structure job contract.")]}},
            ],
        ]
        (self.mineru / "book_content_list_v2.json").write_text(json.dumps(pages), encoding="utf-8")
        (self.mineru / "book_layout.json").write_text(json.dumps({"pdf_info": [
            {"page_idx": 0, "page_size": [600, 800]}, {"page_idx": 1, "page_size": [600, 800]},
        ]}), encoding="utf-8")

        from fastapi.testclient import TestClient
        from literature.api import create_app
        from literature.storage import ProjectStore
        self.client = TestClient(create_app(ProjectStore(self.root / "library")))
        self.headers = {"x-literature-token": self.client.app.state.session_token}
        created = self.client.post("/projects", headers=self.headers, json={"source_file": str(self.source)})
        self.assertEqual(created.status_code, 200, created.text)
        self.project_id = created.json()["project_id"]
        imported = self.client.post(f"/projects/{self.project_id}/mineru/import", headers=self.headers, json={"paths": [str(self.mineru)]})
        self.assertEqual(imported.status_code, 200, imported.text)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        os.environ.pop("LITERATURE_RUNTIME", None)
        os.environ.pop("LITERATURE_LIBRARY", None)

    def create_service(self, **overrides) -> dict:
        payload = {"name": "我的中转站", "preset": "custom", "base_url": "https://relay.example.com/v1", "model": "gpt-4o-mini", **overrides}
        response = self.client.post("/model-services", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_model_services_crud_and_role_binding(self) -> None:
        self.assertIn("model_services_v1", self.client.get("/health").json()["capabilities"])
        service = self.create_service()
        self.assertTrue(service["service_id"])
        listing = self.client.get("/model-services", headers=self.headers).json()
        self.assertEqual(len(listing["services"]), 1)
        # 第一个服务自动绑定到未设置的角色，减少首次配置步骤。
        self.assertEqual(listing["roles"]["structure"], service["service_id"])
        self.assertEqual(listing["roles"]["translation"], service["service_id"])

        second = self.create_service(name="DeepSeek 官方", preset="deepseek")
        roles = self.client.put("/model-services/roles", headers=self.headers, json={"structure": second["service_id"], "translation": service["service_id"]}).json()
        self.assertEqual(roles["structure"], second["service_id"])
        self.assertEqual(roles["translation"], service["service_id"])

        updated = self.client.put(f"/model-services/{service['service_id']}", headers=self.headers, json={**service, "model": "gpt-4o"}).json()
        self.assertEqual(updated["model"], "gpt-4o")

        with patch("literature.model_services.ModelServiceStore.api_key", return_value="sk-test"):
            listing = self.client.get("/model-services", headers=self.headers).json()
            self.assertTrue(all(item["configured"] for item in listing["services"]))

        deleted = self.client.delete(f"/model-services/{service['service_id']}", headers=self.headers)
        self.assertEqual(deleted.status_code, 200)
        listing = self.client.get("/model-services", headers=self.headers).json()
        self.assertEqual(len(listing["services"]), 1)
        # 删除后引用它的角色被清空，避免悬空绑定。
        self.assertEqual(listing["roles"]["translation"], "")

    def test_structure_plan_includes_toc_page_text_only_when_requested(self) -> None:
        plan = self.client.post(f"/projects/{self.project_id}/structure/plan", headers=self.headers, json={
            "toc": [], "extract_toc": True, "proofread": True,
        }).json()
        self.assertTrue(plan["toc_pages"])
        self.assertIn("Contents", json.dumps(plan["toc_pages"], ensure_ascii=False))
        self.assertTrue(plan["options"]["extract_toc"])

        without = self.client.post(f"/projects/{self.project_id}/structure/plan", headers=self.headers, json={
            "toc": [], "extract_toc": False, "proofread": True,
        }).json()
        self.assertEqual(without["toc_pages"], [])

    def test_structure_job_runs_in_background_with_activity(self) -> None:
        service = self.create_service()
        provider = StructureMockProvider()
        with patch("literature.model_services.ModelServiceStore.config_for", return_value=provider):
            created = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers, json={
                "service_id": service["service_id"], "toc": [], "extract_toc": True, "proofread": True, "auto_apply": True,
            })
            self.assertEqual(created.status_code, 200, created.text)
            job_id = created.json()["job_id"]
            job = None
            for _ in range(100):
                job = self.client.get(f"/projects/{self.project_id}/structure/jobs/{job_id}", headers=self.headers).json()
                if job["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.05)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "completed", job)
        self.assertTrue(job["activity"])
        self.assertEqual(len(job["result"]["proposed_toc"]), 2)
        self.assertTrue(job["result"]["audit"]["toc_page_text_included"])
        self.assertFalse(job["result"]["audit"]["body_text_included"])
        self.assertEqual(job["scope"]["extract_toc"], True)
        self.assertGreaterEqual(len(provider.calls), 2)


if __name__ == "__main__":
    unittest.main()
