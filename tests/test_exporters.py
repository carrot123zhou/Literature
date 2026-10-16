"""内置多格式导出：渲染层单测 + API 契约测试。

覆盖：三模式 × 四格式、章节子集展开（含嵌套层级边界）、缺译回退与不重复、
CJK/emoji 往返、表格 cells + rowspan/colspan、图片 basename 冲突去重、
EPUB 结构校验、DOCX 可打开且含 document.xml、空章节/空文档、export_v1 能力标记、
路径穿越被拒、TBL 四个端点仍为 410。
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from literature.exporters import (
    FORMAT_IDS,
    asset_plan,
    create_export,
    export_options,
    render_markdown,
    select_blocks,
    split_sections,
    validate_epub,
)
from literature.models import Block, Cell, Document, TocNode, TranslationEntry, TranslationLayer
from literature.storage import ProjectStore


CJK = "中文标题😀"
EMOJI_PARAGRAPH = "Emoji 😀 与 CJK 中文混排。"


def _blocks() -> list[Block]:
    return [
        Block("t1", "title", 0, 0, source_text="Chapter One", level=1),
        Block("t1a", "title", 1, 0, source_text="Section One A", level=2),
        Block("p1", "paragraph", 2, 0, source_text="Hello world."),
        Block("p2", "paragraph", 3, 0, source_text="No translation here."),
        Block("l1", "list", 4, 0, items=["alpha", "beta"]),
        Block("tb", "table", 5, 0, cells=[
            Cell("c1", 0, 0, "A", header=True),
            Cell("c2", 0, 1, "B"),
            Cell("c3", 1, 0, "C", colspan=2),
        ]),
        Block("img", "image", 6, 0, resource_refs=["r1", "r2", "missing"], caption=["Figure 1"]),
        Block("eq", "equation", 7, 0, source_text="E=mc^2"),
        Block("fn", "footnote", 8, 0, source_text="A note."),
        Block("t2", "title", 9, 0, source_text="Chapter Two", level=1),
        Block("p3", "paragraph", 10, 0, source_text="Second chapter."),
        Block("cjk", "paragraph", 11, 0, source_text=EMOJI_PARAGRAPH),
    ]


def _document(project_root: Path) -> Document:
    assets = project_root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n1")
    (assets / "other").mkdir(parents=True, exist_ok=True)
    (assets / "other" / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n2")
    return Document(
        project_id="placeholder",
        title="Book",
        source_language="en",
        target_language="zh-CN",
        blocks=_blocks(),
        toc=[
            TocNode("n1", "Chapter One", 1, "t1", 0, [TocNode("n1a", "Section One A", 2, "t1a", 0, [])]),
            TocNode("n2", "Chapter Two", 1, "t2", 0, []),
        ],
        resources={
            "r1": {"path": "assets/pic.png"},
            "r2": {"path": "assets/other/pic.png"},
            "missing": {"path": "assets/none.png"},
        },
    )


def _layer(project_id: str) -> TranslationLayer:
    return TranslationLayer(
        project_id=project_id,
        locale="zh-CN",
        entries={
            "t1": TranslationEntry("t1", text="第一章"),
            "t1a": TranslationEntry("t1a", text="第一节"),
            "p1": TranslationEntry("p1", text="你好，世界。"),
            "l1": TranslationEntry("l1", items=["甲", "乙"]),
            "tb": TranslationEntry("tb", cells={"c1": "甲", "c2": "乙", "c3": "丙"}),
            "img": TranslationEntry("img", caption=["图 1"]),
            "t2": TranslationEntry("t2", text="第二章"),
            "cjk": TranslationEntry("cjk", text="译文：" + EMOJI_PARAGRAPH),
        },
    )


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_chapter_expansion_includes_nested_and_stops_at_next_title(self) -> None:
        document = _document(self.root)
        ids = [b.block_id for b in select_blocks(document, chapters=["t1"])]
        self.assertEqual(ids, ["t1", "t1a", "p1", "p2", "l1", "tb", "img", "eq", "fn"])
        nested = [b.block_id for b in select_blocks(document, chapters=["t1a"])]
        self.assertEqual(nested, ["t1a", "p1", "p2", "l1", "tb", "img", "eq", "fn"])
        second = [b.block_id for b in select_blocks(document, chapters=["t2"])]
        self.assertEqual(second, ["t2", "p3", "cjk"])

    def test_chapter_union_merges_ranges(self) -> None:
        document = _document(self.root)
        ids = [b.block_id for b in select_blocks(document, chapters=["t1", "t2"])]
        self.assertEqual(ids, [b.block_id for b in document.blocks])

    def test_empty_chapters_means_whole_book(self) -> None:
        document = _document(self.root)
        self.assertEqual(len(select_blocks(document)), len(document.blocks))
        self.assertEqual(len(select_blocks(document, chapters=[])), len(document.blocks))

    def test_block_ids_override_keeps_reading_order(self) -> None:
        document = _document(self.root)
        ids = [b.block_id for b in select_blocks(document, block_ids=["p3", "p1", "p1"])]
        self.assertEqual(ids, ["p1", "p3"])

    def test_split_sections_uses_toc_hierarchy(self) -> None:
        document = _document(self.root)
        sections = split_sections(document, document.blocks)
        self.assertEqual([(s.title, s.level, s.nav) for s in sections], [
            ("Chapter One", 1, True),
            ("Section One A", 2, True),
            ("Chapter Two", 1, True),
        ])
        self.assertEqual([b.block_id for b in sections[0].blocks], ["t1"])
        self.assertEqual([b.block_id for b in sections[1].blocks], ["t1a", "p1", "p2", "l1", "tb", "img", "eq", "fn"])

    def test_empty_document_sections(self) -> None:
        document = _document(self.root)
        document.blocks = []
        document.toc = []
        self.assertEqual(split_sections(document, []), [])


class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.document = _document(self.root)
        self.layer = _layer("p")
        self.assets = asset_plan(self.document, self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_source_mode_only_emits_source(self) -> None:
        text = render_markdown(self.document, self.document.blocks, self.layer, "source", self.assets)
        self.assertIn("# Chapter One", text)
        self.assertIn("Hello world.", text)
        self.assertNotIn("你好，世界。", text)

    def test_translation_mode_falls_back_to_source(self) -> None:
        text = render_markdown(self.document, self.document.blocks, self.layer, "translation", self.assets)
        self.assertIn("你好，世界。", text)
        self.assertIn("No translation here.", text)
        self.assertNotIn("Hello world.", text)

    def test_bilingual_interleaves_without_duplicate_fallback(self) -> None:
        text = render_markdown(self.document, self.document.blocks, self.layer, "bilingual", self.assets)
        self.assertIn("Hello world.", text)
        self.assertIn("> 你好，世界。", text)
        self.assertIn("No translation here.", text)
        self.assertNotIn("> No translation here.", text)
        self.assertIn("- alpha", text)
        self.assertIn("> - 甲", text)

    def test_table_cells_and_spans(self) -> None:
        text = render_markdown(self.document, self.document.blocks, self.layer, "translation", self.assets)
        self.assertIn("甲", text)
        self.assertIn("丙", text)
        from literature.exporters import _table_html

        table = self.document.block("tb")
        entry = self.layer.entries["tb"]
        markup = _table_html(table, entry, "translation")
        self.assertIn('colspan="2"', markup)
        self.assertIn("<th>甲</th>", markup)
        self.assertIn("<td colspan=\"2\">丙</td>", markup)

    def test_asset_basename_conflict_is_deduplicated(self) -> None:
        names = sorted(self.assets.values())
        self.assertEqual(len(names), 2)
        self.assertEqual(len(set(names)), 2)
        self.assertIn("pic.png", names)
        text = render_markdown(self.document, self.document.blocks, self.layer, "source", self.assets)
        for name in names:
            self.assertIn(f"assets/{name}", text)
        self.assertNotIn("assets/none.png", text)

    def test_cjk_and_emoji_roundtrip_in_markdown(self) -> None:
        text = render_markdown(self.document, self.document.blocks, self.layer, "translation", self.assets)
        self.assertIn(EMOJI_PARAGRAPH, text)
        self.assertIn("译文：" + EMOJI_PARAGRAPH, text)


class ApiExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source.html"
        source.write_text("<h1>Book</h1>", encoding="utf-8")
        self.store = ProjectStore(self.root / "library")
        created = self.store.create_project(source, title="Book")
        self.project_id = created["project_id"]
        self.project_dir = Path(created["path"])
        self.document = _document(self.project_dir)
        self.document.project_id = self.project_id
        self.store.save_document(self.document)
        self.store.save_translation(_layer(self.project_id))

        from fastapi.testclient import TestClient

        from literature.api import create_app

        self.client = TestClient(create_app(self.store))
        self.headers = {"x-literature-token": self.client.app.state.session_token}

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def _export(self, **payload):
        return self.client.post(f"/projects/{self.project_id}/export", headers=self.headers, json=payload)

    def test_capability_marker_and_tbl_still_gone(self) -> None:
        health = self.client.get("/health").json()
        self.assertIn("export_v1", health["capabilities"])
        for method, path in (
            ("post", f"/projects/{self.project_id}/tbl/prepare"), ("post", "/tbl/start"),
            ("get", "/tbl/config"), ("put", "/tbl/config"), ("get", "/tbl/status"),
        ):
            response = getattr(self.client, method)(path, headers=self.headers)
            self.assertEqual(response.status_code, 410, response.text)

    def test_options_reports_locales_formats_modes_and_counts(self) -> None:
        response = self.client.get(f"/projects/{self.project_id}/export/options", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual({item["id"] for item in value["formats"]}, FORMAT_IDS)
        self.assertEqual({item["id"] for item in value["modes"]}, {"source", "translation", "bilingual"})
        self.assertEqual(value["locales"], ["zh-CN"])
        self.assertEqual(value["default_locale"], "zh-CN")
        self.assertEqual(value["block_count"], len(self.document.blocks))
        first = value["toc"][0]
        self.assertEqual(first["title"], "Chapter One")
        self.assertEqual(first["block_count"], 9)
        self.assertEqual(first["translated_count"], 6)
        self.assertEqual(first["missing_count"], 3)
        self.assertEqual(first["children"][0]["title"], "Section One A")

    def test_all_formats_and_modes_produce_files(self) -> None:
        for mode in ("source", "translation", "bilingual"):
            response = self._export(formats=["epub", "docx", "markdown", "html"], mode=mode, locale="zh-CN")
            self.assertEqual(response.status_code, 200, response.text)
            value = response.json()
            rels = {item["rel_path"] for item in value["files"]}
            self.assertIn("epub/Book.epub", rels)
            self.assertIn("docx/Book.docx", rels)
            self.assertIn("markdown/full.md", rels)
            self.assertIn("html/index.html", rels)
            self.assertTrue(any(rel.startswith("html/chapter-") for rel in rels))
            for item in value["files"]:
                self.assertTrue((Path(value["path"]) / item["rel_path"]).is_file())
            markdown = (Path(value["path"]) / "markdown" / "full.md").read_text(encoding="utf-8")
            if mode == "source":
                self.assertIn("Hello world.", markdown)
                self.assertNotIn("你好，世界。", markdown)
            else:
                self.assertIn("你好，世界。", markdown)
                self.assertIn("No translation here.", markdown)

    def test_single_format_is_flat_and_uses_filename(self) -> None:
        response = self._export(formats=["epub"], mode="source", filename="custom name")
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual([item["rel_path"] for item in value["files"]], ["custom name.epub"])
        self.assertEqual(value["files"][0]["single_file"], True)

    def test_chapter_subset_excludes_other_chapters(self) -> None:
        response = self._export(formats=["markdown"], mode="source", chapters=["t2"])
        self.assertEqual(response.status_code, 200, response.text)
        path = Path(response.json()["path"]) / "full.md"
        markdown = path.read_text(encoding="utf-8")
        self.assertIn("Second chapter.", markdown)
        self.assertNotIn("Hello world.", markdown)

    def test_block_ids_override_chapters(self) -> None:
        response = self._export(formats=["markdown"], mode="source", chapters=["t1"], block_ids=["p3"])
        self.assertEqual(response.status_code, 200, response.text)
        markdown = (Path(response.json()["path"]) / "full.md").read_text(encoding="utf-8")
        self.assertIn("Second chapter.", markdown)
        self.assertNotIn("Hello world.", markdown)

    def test_unknown_chapter_yields_warning_and_empty_selection_still_exports(self) -> None:
        response = self._export(formats=["markdown"], mode="source", chapters=["t2", "nope"])
        self.assertEqual(response.status_code, 200, response.text)
        # 选到 t2 有效内容 + 一个不存在章节 → 有告警但成功。
        self.assertTrue(any("不存在" in item for item in response.json()["warnings"]))
        empty = self._export(formats=["markdown"], mode="source", chapters=["nope"])
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertTrue(any("无任何可导出内容" in item for item in empty.json()["warnings"]))
        self.assertEqual(empty.json()["block_count"], 0)

    def test_missing_translation_warning(self) -> None:
        response = self._export(formats=["markdown"], mode="translation")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(any("译文缺失" in item for item in response.json()["warnings"]))

    def test_docx_is_openable_zip_and_contains_document_xml(self) -> None:
        response = self._export(formats=["docx"], mode="translation")
        self.assertEqual(response.status_code, 200, response.text)
        path = Path(response.json()["path"]) / "Book.docx"
        self.assertTrue(zipfile.is_zipfile(path))
        with zipfile.ZipFile(path) as archive:
            self.assertIn("word/document.xml", archive.namelist())
        from docx import Document as WordDocument

        word = WordDocument(str(path))
        text = "\n".join(paragraph.text for paragraph in word.paragraphs)
        self.assertIn("中文", text)
        self.assertIn("😀", text)
        self.assertTrue(word.tables)
        cell_texts = {cell.text for table in word.tables for row in table.rows for cell in row.cells}
        self.assertIn("甲", cell_texts)

    def test_epub_structure_is_valid(self) -> None:
        response = self._export(formats=["epub"], mode="bilingual")
        self.assertEqual(response.status_code, 200, response.text)
        path = Path(response.json()["path"]) / "Book.epub"
        validate_epub(path)
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            self.assertEqual(infos[0].filename, "mimetype")
            self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
            for name in ("META-INF/container.xml", "EPUB/package.opf", "EPUB/nav.xhtml"):
                self.assertIn(name, archive.namelist())
                ET.fromstring(archive.read(name))
            self.assertTrue(any(name.endswith(".xhtml") and "section" in name for name in archive.namelist()))
            css = archive.read("EPUB/styles/book.css").decode("utf-8")
            self.assertIn("text-align:justify", css)
            self.assertNotIn("border-left", css)

    def test_epub_nav_bilingual_and_translation_modes(self) -> None:
        # 对照版：导航目录为「原文 · 译文」。
        response = self._export(formats=["epub"], mode="bilingual")
        path = Path(response.json()["path"]) / "Book.epub"
        with zipfile.ZipFile(path) as archive:
            nav = archive.read("EPUB/nav.xhtml").decode("utf-8")
        self.assertIn("Chapter One · 第一章", nav)
        self.assertIn("Section One A · 第一节", nav)

        # 译文版：导航目录用纯译文标题；缺译回退原文。
        response = self._export(formats=["epub"], mode="translation")
        path = Path(response.json()["path"]) / "Book.epub"
        with zipfile.ZipFile(path) as archive:
            nav = archive.read("EPUB/nav.xhtml").decode("utf-8")
        self.assertIn("第一章", nav)
        self.assertIn("第二章", nav)
        self.assertNotIn("Chapter One", nav)

    def test_epub_cover_from_source_pdf(self) -> None:
        import fitz

        pdf = self.project_dir / "source" / "origin.pdf"
        with fitz.open() as doc:
            page = doc.new_page(width=400, height=600)
            page.insert_text((40, 80), "COVER", fontsize=32)
            # 填充一些像素，避免空白页被压缩成极小文件。
            page.draw_rect(fitz.Rect(20, 20, 380, 580), color=(0.2, 0.3, 0.2), fill=None)
            doc.save(pdf)
        response = self._export(formats=["epub"], mode="bilingual")
        self.assertEqual(response.status_code, 200, response.text)
        path = Path(response.json()["path"]) / "Book.epub"
        validate_epub(path)
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assertIn("EPUB/cover.jpg", names)
            self.assertIn("EPUB/cover.xhtml", names)
            self.assertGreater(archive.getinfo("EPUB/cover.jpg").file_size, 1000)
            opf = archive.read("EPUB/package.opf").decode("utf-8")
            self.assertIn('properties="cover-image"', opf)
            self.assertIn('<meta name="cover" content="cover-image"/>', opf)
            self.assertIn('<itemref idref="cover-page"', opf)
            spine_first = opf.split("<spine>", 1)[1].split("</spine>", 1)[0]
            self.assertTrue(spine_first.index('idref="cover-page"') < spine_first.index("section-1"))

    def test_epub_without_pdf_source_has_no_cover(self) -> None:
        response = self._export(formats=["epub"], mode="bilingual")
        path = Path(response.json()["path"]) / "Book.epub"
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
        self.assertNotIn("EPUB/cover.jpg", names)
        self.assertNotIn("EPUB/cover.xhtml", names)
        with zipfile.ZipFile(path) as archive:
            opf = archive.read("EPUB/package.opf").decode("utf-8")
        self.assertNotIn("cover-image", opf)

    def test_html_is_multipage_with_nav_and_css(self) -> None:
        response = self._export(formats=["html"], mode="source")
        self.assertEqual(response.status_code, 200, response.text)
        root = Path(response.json()["path"])
        index = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn("Chapter One", index)
        self.assertIn("chapter-0001.html", index)
        self.assertTrue((root / "style.css").is_file())

    def test_download_and_path_traversal(self) -> None:
        response = self._export(formats=["epub"], mode="source")
        value = response.json()
        export_id = value["export_id"]
        rel = value["files"][0]["rel_path"]
        ok = self.client.get(
            f"/projects/{self.project_id}/exports/{export_id}/file", headers=self.headers, params={"name": rel},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.headers["content-type"].split(";")[0], "application/epub+zip")
        self.assertGreater(len(ok.content), 0)
        for bad in ("../../project.json", "../export.json", "/etc/passwd"):
            blocked = self.client.get(
                f"/projects/{self.project_id}/exports/{export_id}/file", headers=self.headers, params={"name": bad},
            )
            self.assertIn(blocked.status_code, (400, 404), f"{bad} -> {blocked.status_code}")
        missing = self.client.get(
            f"/projects/{self.project_id}/exports/{export_id}/file", headers=self.headers, params={"name": "nope.txt"},
        )
        self.assertEqual(missing.status_code, 404, missing.text)

    def test_exports_list_is_newest_first_and_export_does_not_mutate_sources(self) -> None:
        first = self._export(formats=["markdown"], mode="source")
        second = self._export(formats=["markdown"], mode="translation")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        listing = self.client.get(f"/projects/{self.project_id}/exports", headers=self.headers)
        self.assertEqual(listing.status_code, 200, listing.text)
        values = listing.json()
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0]["export_id"], second.json()["export_id"])
        # 导出是视图：document / translation 内容不变。
        self.assertEqual(self.store.load_document(self.project_id).blocks[2].source_text, "Hello world.")
        self.assertEqual(self.store.load_translation(self.project_id, "zh-CN").entries["p1"].text, "你好，世界。")

    def test_export_options_requires_document(self) -> None:
        source = self.root / "empty.html"
        source.write_text("<h1>x</h1>", encoding="utf-8")
        created = self.store.create_project(source, title="No Doc")
        response = self.client.get(f"/projects/{created['project_id']}/export/options", headers=self.headers)
        self.assertEqual(response.status_code, 400, response.text)


if __name__ == "__main__":
    unittest.main()
