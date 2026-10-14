"""document_updated_at 快速读取的回归：值与文件一致，且缓存不会假命中。

背景（2026-10-14）：structure_review_summary 原来为了拿一个时间戳就 load_document
解码整份 document.json（单份可达 8 MB），而 /projects 每 3 秒逐项目调用一次；
实测整库每轮多花约 130 ms。改为以 (size, mtime_ns) 为键的 project_state 缓存后
warm 约 6 ms，本文件锁定“值正确 + 失效正确 + 确认状态机不变”。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from literature.models import Block, Document
from literature.storage import ProjectStore


def build(store: ProjectStore, tmp: Path, blocks: int = 20) -> str:
    source = tmp / "source.html"
    source.write_text("fixture")
    project = store.create_project(source)["project_id"]
    store.save_document(Document(
        project_id=project, title="F", source_language="en", page_count=2,
        blocks=[Block(block_id=f"b{i}", type="title", order=i, page_index=1,
                      source_text=f"Heading {i}", level=2) for i in range(blocks)]))
    return project


class DocumentUpdatedAtTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root / "library")
        self.project = build(self.store, self.root)

    def document_path(self) -> Path:
        return self.store.project_path(self.project) / "document" / "document.json"

    def test_matches_the_document_and_follows_every_save(self):
        self.assertEqual(self.store.document_updated_at(self.project),
                         self.store.load_document(self.project).updated_at)
        for index in range(3):
            document = self.store.load_document(self.project)
            document.blocks[0].source_text = f"Heading {index}"
            self.store.save_document(document)
            self.assertEqual(self.store.document_updated_at(self.project), document.updated_at)

    def test_external_write_invalidates_the_cache(self):
        first = self.store.document_updated_at(self.project)
        # 模拟另一个写入方（不走 save_document）：缓存键来自真实文件，必须失效后重读。
        value = json.loads(self.document_path().read_text(encoding="utf-8"))
        value["updated_at"] = "2030-01-01T00:00:00+00:00"
        value["blocks"] = value["blocks"][:1]  # 同时改变文件大小
        self.document_path().write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertNotEqual(first, self.store.document_updated_at(self.project))
        self.assertEqual(self.store.document_updated_at(self.project), "2030-01-01T00:00:00+00:00")

    def test_missing_document_returns_empty_without_raising(self):
        self.document_path().unlink()
        self.assertEqual(self.store.document_updated_at(self.project), "")

    def test_project_state_table_exists_for_existing_databases(self):
        database = self.store.project_database(self.project)
        with sqlite3.connect(database) as connection:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("project_state", names)


class StructureReviewStateWithCheapTimestampTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ProjectStore(self.root / "library")
        self.project = build(self.store, self.root)

    def test_state_machine_is_unchanged(self):
        self.assertEqual(self.store.structure_review_summary(self.project)["state"], "todo")
        self.store.confirm_structure_review(self.project, scope="full")
        summary = self.store.structure_review_summary(self.project)
        self.assertEqual(summary["state"], "done")
        self.assertEqual(summary["confirmed_scope"], "full")
        # 文档改动后确认自动失效：快速读取必须跟着变，否则会假报 done。
        document = self.store.load_document(self.project)
        document.blocks[0].level = 1
        self.store.save_document(document)
        self.assertEqual(self.store.structure_review_summary(self.project)["state"], "partial")

    def test_document_change_written_around_the_store_still_invalidates(self):
        self.store.confirm_structure_review(self.project, scope="full")
        path = self.store.project_path(self.project) / "document" / "document.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["updated_at"] = "2031-05-05T05:05:05+00:00"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.store.structure_review_summary(self.project)["state"], "partial")


if __name__ == "__main__":
    unittest.main()
