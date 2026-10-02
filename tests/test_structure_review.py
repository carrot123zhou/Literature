from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literature.mineru import import_mineru
from literature.models import BBOX_SPACE_MINERU_1000, BBOX_SPACE_NONE, SCHEMA_VERSION
from literature.storage import ProjectStore
from literature.structure import apply_patch, revert_patch


def span(text: str) -> dict[str, str]:
    return {"type": "text", "content": text}


class StructureReviewTests(unittest.TestCase):
    """对照阅读与结构调整模块依赖的数据契约。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        pdf = self.root / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% fixture")
        self.project_id = self.store.create_project(pdf, title="Review Contract")["project_id"]
        self.mineru = self.root / "mineru"
        self.mineru.mkdir(parents=True)
        pages = [[
            {"type": "title", "bbox": [100, 80, 900, 120], "content": {"level": 1, "title_content": [span("Introduction")]}},
            {"type": "paragraph", "bbox": [100, 140, 900, 220], "content": {"paragraph_content": [span("A body paragraph used for the review contract.")]}},
            {"type": "paragraph", "bbox": [100, 240, 100, 260], "content": {"paragraph_content": [span("Degenerate box.")]}},
        ]]
        (self.mineru / "book_content_list_v2.json").write_text(json.dumps(pages), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_layout(self, name: str = "layout.json", page_size: list[int] | None = None) -> None:
        (self.mineru / name).write_text(json.dumps({"pdf_info": [{
            "page_idx": 0, "page_size": page_size or [500, 700],
            "preproc_blocks": [{"large": "sealed only"}],
        }]}), encoding="utf-8")

    def test_layout_json_supplies_page_geometry(self):
        """精准解析输出没有 middle.json，页面几何来自 layout.json。"""
        self.write_layout("book_layout.json")
        document = import_mineru(self.store, self.project_id, [self.mineru])
        self.assertEqual((document.pages[0].width, document.pages[0].height), (500, 700))
        self.assertEqual(document.pages[0].source_page_index, 0)
        self.assertTrue(document.pages[0].source_file.endswith("book_layout.json"))
        self.assertNotIn("preproc_blocks", document.pages[0].raw)

    def test_bbox_space_is_explicit_and_backward_compatible(self):
        self.write_layout()
        document = import_mineru(self.store, self.project_id, [self.mineru])
        self.assertEqual(document.bbox_space, BBOX_SPACE_MINERU_1000)
        self.assertEqual(document.schema_version, SCHEMA_VERSION)
        self.assertTrue(document.bbox_available)
        self.assertEqual(document.blocks[0].format["bbox_space"], BBOX_SPACE_MINERU_1000)

        # 老文档没有 bbox_space 字段时仍按 0-1000 解释，无需迁移。
        legacy = document.to_dict()
        legacy.pop("bbox_space")
        from literature.models import Document

        self.assertEqual(Document.from_dict(legacy).bbox_space, BBOX_SPACE_MINERU_1000)

    def test_degenerate_bbox_is_dropped(self):
        self.write_layout()
        document = import_mineru(self.store, self.project_id, [self.mineru])
        degenerate = document.blocks[2]
        self.assertEqual(degenerate.bbox, [])
        self.assertNotIn("bbox_space", degenerate.format)

    def test_block_position_matches_reading_order(self):
        self.write_layout()
        document = import_mineru(self.store, self.project_id, [self.mineru])
        self.assertEqual(document.block_position(document.blocks[1].block_id), 1)
        self.assertIs(document.page(0), document.pages[0])
        self.assertIsNone(document.page(9))

    def test_set_title_is_reversible(self):
        self.write_layout()
        document = import_mineru(self.store, self.project_id, [self.mineru])
        paragraph = document.blocks[1]
        patch = apply_patch(self.store, self.project_id, {
            "block_id": paragraph.block_id, "operation": "set_title", "after": 3,
            "reason": "人工确认为三级标题",
        })
        promoted = self.store.load_document(self.project_id).block(paragraph.block_id)
        self.assertEqual((promoted.type, promoted.level), ("title", 3))
        self.assertEqual(patch.before, {"type": "paragraph", "level": 0})

        revert_patch(self.store, self.project_id, patch.patch_id)
        restored = self.store.load_document(self.project_id).block(paragraph.block_id)
        self.assertEqual((restored.type, restored.level), ("paragraph", 0))

    def test_set_title_cannot_be_applied_automatically(self):
        self.write_layout()
        document = import_mineru(self.store, self.project_id, [self.mineru])
        with self.assertRaises(Exception):
            apply_patch(self.store, self.project_id, {
                "block_id": document.blocks[1].block_id, "operation": "set_title", "after": 2,
                "confidence": 1.0, "automatic": True,
            })

    def test_unknown_operation_is_rejected(self):
        self.write_layout()
        document = import_mineru(self.store, self.project_id, [self.mineru])
        with self.assertRaises(Exception):
            apply_patch(self.store, self.project_id, {
                "block_id": document.blocks[0].block_id, "operation": "delete_block", "after": "",
            })


class ReviewApiTests(unittest.TestCase):
    """对照阅读模块依赖的 API 契约：页面几何、bbox 单位、封存原始文件读取。"""

    def setUp(self) -> None:
        import os

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
        (self.mineru / "scan_content_list_v2.json").write_text(json.dumps([[
            {"type": "title", "bbox": [40, 40, 600, 100], "content": {"level": 1, "title_content": [span("Chapter One")]}},
            {"type": "paragraph", "bbox": [40, 140, 600, 400], "content": {"paragraph_content": [span("Body text for the review contract.")]}},
        ]]), encoding="utf-8")
        (self.mineru / "scan_layout.json").write_text(json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [600, 800]}]}), encoding="utf-8")

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
        import os

        self.client.close()
        self.temporary.cleanup()
        os.environ.pop("LITERATURE_RUNTIME", None)
        os.environ.pop("LITERATURE_LIBRARY", None)

    def test_health_declares_review_capability(self) -> None:
        self.assertIn("structure_review_v2", self.client.get("/health").json()["capabilities"])

    def test_document_exposes_bbox_space_and_page_geometry(self) -> None:
        document = self.client.get(f"/projects/{self.project_id}/document", headers=self.headers).json()
        self.assertEqual(document["bbox_space"], BBOX_SPACE_MINERU_1000)
        self.assertEqual((document["pages"][0]["width"], document["pages"][0]["height"]), (600, 800))
        self.assertEqual(document["blocks"][0]["bbox"], [40.0, 40.0, 600.0, 100.0])

    def test_set_title_patch_is_recorded_and_revertible(self) -> None:
        document = self.client.get(f"/projects/{self.project_id}/document", headers=self.headers).json()
        paragraph = next(block for block in document["blocks"] if block["type"] == "paragraph")
        created = self.client.post(f"/projects/{self.project_id}/structure/patches", headers=self.headers, json={
            "block_id": paragraph["block_id"], "operation": "set_title", "after": 2, "reason": "人工确认标题",
        })
        self.assertEqual(created.status_code, 200, created.text)
        patch = created.json()
        updated = self.client.get(f"/projects/{self.project_id}/document", headers=self.headers).json()
        promoted = next(block for block in updated["blocks"] if block["block_id"] == paragraph["block_id"])
        self.assertEqual((promoted["type"], promoted["level"]), ("title", 2))
        def flatten(nodes: list[dict]) -> list[dict]:
            return [node for item in nodes for node in [item, *flatten(item["children"])]]
        self.assertEqual(len(flatten(updated["toc"])), 2)

        reverted = self.client.post(f"/projects/{self.project_id}/structure/patches/{patch['patch_id']}/revert", headers=self.headers)
        self.assertEqual(reverted.status_code, 200, reverted.text)
        restored = self.client.get(f"/projects/{self.project_id}/document", headers=self.headers).json()
        back = next(block for block in restored["blocks"] if block["block_id"] == paragraph["block_id"])
        self.assertEqual((back["type"], back["level"]), ("paragraph", 0))

    def test_raw_files_are_listed_and_paths_are_confined(self) -> None:
        listing = self.client.get(f"/projects/{self.project_id}/raw", headers=self.headers).json()
        paths = [item["path"] for item in listing["files"]]
        self.assertTrue(any(path.endswith("scan_content_list_v2.json") for path in paths))
        served = self.client.get(f"/projects/{self.project_id}/raw/{paths[0]}", headers=self.headers)
        self.assertEqual(served.status_code, 200, served.text)
        outside = self.client.get(f"/projects/{self.project_id}/raw/source/origin.pdf", headers=self.headers)
        self.assertEqual(outside.status_code, 400)
        missing = self.client.get(f"/projects/{self.project_id}/raw/ocr/raw/mineru/nope.json", headers=self.headers)
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
