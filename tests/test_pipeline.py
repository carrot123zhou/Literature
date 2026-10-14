from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from literature.mineru import import_mineru
from literature.models import UsageRecord
from literature.providers import MockProvider, ProviderResponse, extract_json
from literature.storage import ProjectStore
from literature.structure import analyze_structure, analyze_structure_with_provider, apply_automatic, find_toc_page_candidates, revert_patch, structure_model_payload
from literature.translation import TranslationService, draft_glossary


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl1pAAAAABJRU5ErkJggg==")


def span(text: str) -> dict[str, str]:
    return {"type": "text", "content": text}


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        pdf = self.root / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% fixture")
        self.project = self.store.create_project(pdf, title="Structure and History")
        self.project_id = self.project["project_id"]
        self.mineru = self.root / "mineru"
        (self.mineru / "images").mkdir(parents=True)
        (self.mineru / "images" / "figure.png").write_bytes(PNG)
        pages = [[
            {"type": "title", "bbox": [10, 10, 300, 35], "content": {"level": 1, "title_content": [span("Introduction")]}},
            {"type": "paragraph", "bbox": [10, 45, 300, 90], "content": {"paragraph_content": [span("The history of science is not a linear accumulation of facts.")]}},
            {"type": "title", "bbox": [10, 95, 300, 140], "content": {"level": 2, "title_content": [span("This sentence ends with punctuation and is long enough to be ordinary body prose rather than a genuine heading in the confirmed table of contents.")]}},
            {"type": "table", "bbox": [10, 150, 300, 240], "content": {"table_content": "<table><tr><th>Term</th><th>Meaning</th></tr><tr><td>Paradigm</td><td>Example</td></tr></table>", "table_caption": [span("Table one")]}},
            {"type": "image", "bbox": [10, 250, 300, 420], "content": {"image_source": {"path": "images/figure.png"}, "image_caption": [span("Figure one")]}},
            {"type": "page_footnote", "bbox": [10, 430, 300, 450], "content": {"page_footnote_content": [span("A source note.")]}},
        ]]
        (self.mineru / "book_content_list_v2.json").write_text(json.dumps(pages), encoding="utf-8")
        (self.mineru / "book_middle.json").write_text(json.dumps({"pdf_info": [{
            "page_idx": 0, "page_size": [1200, 1800],
            "preproc_blocks": [{"large": "kept only in sealed raw output"}],
        }]}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def import_document(self):
        return import_mineru(self.store, self.project_id, [self.mineru])

    def test_import_preserves_raw_structure_and_assets(self):
        document = self.import_document()
        self.assertEqual(document.page_count, 1)
        self.assertEqual((document.pages[0].width, document.pages[0].height), (1200, 1800))
        self.assertNotIn("preproc_blocks", document.pages[0].raw)
        self.assertEqual(document.source_language, "en")
        self.assertEqual([block.type for block in document.blocks], ["title", "paragraph", "title", "table", "image", "footnote"])
        self.assertEqual(document.blocks[0].bbox, [10.0, 10.0, 300.0, 35.0])
        self.assertEqual(document.blocks[3].cells[0].text, "Term")
        self.assertTrue(document.blocks[3].cells[0].cell_id.startswith(document.blocks[3].block_id))
        self.assertEqual(document.blocks[0].raw["type"], "title")
        self.assertTrue(document.resources)
        self.assertEqual(self.store.verify_raw(self.project_id), [])
        source_markdown = (self.store.project_path(self.project_id) / "document" / "source.md").read_text(encoding="utf-8")
        self.assertIn("# Introduction", source_markdown)
        # source.md 是投影，不是事实来源；但它会被导入/补丁/撤销三处重写，
        # 所以必须像 document.json 一样原子写入：不留下临时文件。
        self.assertEqual(list((self.store.project_path(self.project_id) / "document").glob(".source.md-*")), [])

    def test_source_markdown_is_written_atomically_and_matches_the_document(self):
        from literature.mineru import render_source_markdown, write_source_markdown
        document = self.import_document()
        document.blocks[0].source_text = "Rewritten heading"
        self.store.save_document(document)
        write_source_markdown(self.store.project_path(self.project_id), document)
        path = self.store.project_path(self.project_id) / "document" / "source.md"
        self.assertEqual(path.read_text(encoding="utf-8"), render_source_markdown(document))
        self.assertIn("Rewritten heading", path.read_text(encoding="utf-8"))
        self.assertEqual(list(path.parent.glob(".source.md-*")), [])

    def test_table_rowspan_and_colspan_occupancy(self):
        from literature.mineru import _table_cells

        cells = _table_cells("t1", "<table><tr><td rowspan='2'>A</td><td>B</td></tr><tr><td>C</td></tr></table>")
        positions = {cell.text: (cell.row, cell.column) for cell in cells}
        self.assertEqual(positions["A"], (0, 0))
        self.assertEqual(positions["B"], (0, 1))
        # rowspan=2 占住了第 0 列，第二行的 C 必须落在第 1 列而不是 (1, 0)。
        self.assertEqual(positions["C"], (1, 1))

        spanned = _table_cells("t2", "<table><tr><td colspan='2'>X</td></tr><tr><td>Y</td><td>Z</td></tr></table>")
        layout = {cell.text: (cell.row, cell.column, cell.colspan) for cell in spanned}
        self.assertEqual(layout["X"], (0, 0, 2))
        self.assertEqual(layout["Y"][:2], (1, 0))
        self.assertEqual(layout["Z"][:2], (1, 1))

    def test_structure_autofix_is_reversible(self):
        document = self.import_document()
        proposals = analyze_structure(document, [{"title": "Introduction", "level": 2}])
        automatic = [value for value in proposals if value["automatic"]]
        self.assertEqual(len(automatic), 1)
        self.assertEqual(automatic[0]["operation"], "set_level")
        # Text length alone cannot authorize automatic demotion.
        self.assertTrue(any(value["operation"] == "set_type" and not value["automatic"] for value in proposals))
        patches = apply_automatic(self.store, self.project_id, proposals)
        changed = self.store.load_document(self.project_id).block(automatic[0]["block_id"])
        self.assertEqual((changed.type, changed.level), ("title", 2))
        revert_patch(self.store, self.project_id, patches[0].patch_id)
        restored = self.store.load_document(self.project_id).block(automatic[0]["block_id"])
        self.assertEqual((restored.type, restored.level), ("title", 1))

    def test_structure_ai_payload_excludes_body_text(self):
        document = self.import_document()
        toc = [{"title": "Introduction", "level": 1}]
        payload = structure_model_payload(document, toc)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("The history of science is not a linear accumulation of facts.", serialized)
        self.assertIn("Introduction", serialized)

        title = document.blocks[0]

        class StructureProvider(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.last_user = user
                return ProviderResponse(
                    content=json.dumps({"suggestions": [{
                        "block_id": title.block_id, "operation": "set_level",
                        "after": 2, "confidence": 0.99, "reason": "目录层级不一致",
                    }]}), provider="mock", model="structure-mock",
                )

        provider = StructureProvider()
        suggestions, _response = analyze_structure_with_provider(document, toc, provider)
        self.assertTrue(suggestions[0]["automatic"])
        self.assertNotIn("The history of science is not a linear accumulation of facts.", provider.last_user)

    def test_toc_page_candidates_are_local_and_multilingual(self):
        document = self.import_document()
        document.blocks[0].source_text = "Table of Contents"
        candidates = find_toc_page_candidates(document)
        self.assertEqual(candidates[0]["page_number"], 1)
        self.assertGreaterEqual(candidates[0]["confidence"], 0.7)

    def test_translation_lock_and_usage(self):
        document = self.import_document()
        service = TranslationService(self.store, MockProvider())
        job = service.run(self.project_id, glossary=draft_glossary(document))
        self.assertEqual(job["status"], "completed")
        layer = self.store.load_translation(self.project_id)
        self.assertEqual(len(layer.entries), 6)
        table = next(block for block in document.blocks if block.type == "table")
        self.assertEqual(set(layer.entries[table.block_id].cells), {cell.cell_id for cell in table.cells})
        paragraph = next(block for block in document.blocks if block.type == "paragraph")
        service.edit(self.project_id, paragraph.block_id, "人工译文")
        service.run(self.project_id, force=True)
        locked = self.store.load_translation(self.project_id).entries[paragraph.block_id]
        self.assertEqual(locked.text, "人工译文")
        self.assertTrue(locked.locked)
        service.unlock(self.project_id, paragraph.block_id)
        service.run(self.project_id, block_ids=[paragraph.block_id], refine=True)
        refined = self.store.load_translation(self.project_id).entries[paragraph.block_id]
        self.assertEqual(refined.status, "refined")
        self.assertTrue(any(revision.get("text") == "人工译文" for revision in refined.revisions))
        service.edit(self.project_id, paragraph.block_id, "人工译文")
        self.assertIn("译：", service.instant(self.project_id, "A selected phrase")["text"])
        selection = service.save_selection(self.project_id, paragraph.block_id, "history of science", "科学史")
        self.assertEqual(selection.selection_translations[-1]["translated_text"], "科学史")
        self.assertGreater(self.store.usage_summary(self.project_id)["input_tokens"], 0)

    def test_tampered_raw_is_detected(self):
        self.import_document()
        raw_file = next((self.store.project_path(self.project_id) / "ocr" / "raw" / "mineru").rglob("*_content_list_v2.json"))
        raw_file.chmod(0o644)
        raw_file.write_text("[]", encoding="utf-8")
        self.assertTrue(any("哈希变化" in value for value in self.store.verify_raw(self.project_id)))

    def test_json_extraction_accepts_fences_and_rejects_arrays(self):
        self.assertEqual(extract_json("```json\n{\"ok\": true}\n```"), {"ok": True})
        with self.assertRaises(Exception):
            extract_json("[]")

    def test_failed_chunk_is_recorded_and_can_be_resumed(self):
        document = self.import_document()

        class InvalidProvider(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                return ProviderResponse(content='{"translations": []}', provider="mock", model="broken")

        service = TranslationService(self.store, InvalidProvider())
        with self.assertRaises(Exception):
            service.run(self.project_id)
        failed = self.store.load_translation(self.project_id)
        self.assertTrue(failed.entries)
        self.assertTrue(all(entry.status == "failed" for entry in failed.entries.values()))

        resumed = TranslationService(self.store, MockProvider()).run(
            self.project_id, block_ids=list(failed.entries)
        )
        self.assertEqual(resumed["status"], "completed")
        translated = self.store.load_translation(self.project_id)
        self.assertTrue(all(entry.status == "translated" for entry in translated.entries.values()))

    def test_budget_pauses_before_another_model_call(self):
        self.import_document()
        self.store.update_manifest(self.project_id, budget_usd=0.5)
        self.store.record_usage(UsageRecord(
            usage_id="spent", project_id=self.project_id, job_id="previous",
            provider="mock", model="mock", scope="translate", estimated_cost=0.5,
        ))
        service = TranslationService(self.store, MockProvider())
        job = service.run(self.project_id)
        self.assertEqual(job["status"], "budget_paused")
        with self.assertRaises(Exception):
            service.instant(self.project_id, "No more spending")


if __name__ == "__main__":
    unittest.main()
