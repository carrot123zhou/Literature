"""印刷目录页定位的负样本回归：尾注 / 参考书目 / 索引页都不能被当成目录。

背景（2026-10-14）：`find_toc_pages` 只用“多行带页码引用”定位目录，把 On Being Authentic
的尾注页 p97–104 当成了目录（抽出 80 条垃圾条目），于是结构作业降级到模型提取目录；
同库的 Once upon a future time 则长期把参考书目页当目录。本文件锁定新的形态判据。
"""
from __future__ import annotations

import unittest

from literature import structure_signals as signals
from literature.models import Block, Document


class PrintedTocPageDetectionTests(unittest.TestCase):
    def document(self, pages: list[list[Block]]) -> Document:
        blocks: list[Block] = []
        order = 0
        for page_index, page_blocks in enumerate(pages):
            for block in page_blocks:
                block.page_index = page_index
                block.order = order
                order += 1
                blocks.append(block)
        return Document(project_id="p", title="F", source_language="en",
                        page_count=len(pages), blocks=blocks)

    def paragraph(self, bid: str, text: str, page: int = 0) -> Block:
        return Block(block_id=bid, type="paragraph", page_index=page, order=0,
                     source_text=text, bbox=[100, 120, 900, 200])

    def marker(self, bid: str = "mark", text: str = "Contents") -> Block:
        return Block(block_id=bid, type="title", page_index=0, order=0,
                     source_text=text, level=1, bbox=[100, 50, 800, 100])

    def test_singular_content_marker_marks_the_toc_page(self):
        # 真实书里 "Content"（单数）很常见；旧正则只认 "Contents"，会退回行扫描并选错页。
        doc = self.document([[
            self.marker(text="Content"),
            self.paragraph("e0", "Preface vii"),
            self.paragraph("e1", "One The Culture of Authenticity 1"),
            self.paragraph("e2", "Two The Enchanted Garden 7"),
            self.paragraph("e3", "Index 95"),
        ]])
        self.assertEqual(signals.find_toc_pages(doc), [0])

    def test_endnote_pages_are_not_toc_pages(self):
        # 尾注页满页“编号 + 末尾页码”，但条目是长句书目：形态比例达标才算目录。
        notes = [
            f"{i} See, for example, the essays in Mark Wrathall and Jeff Malpas, eds., Heidegger "
            f"and Modernity (Cambridge, MA: MIT Press, 2000), or my own essays, pp. 119-32, note {100 + i}."
            for i in range(1, 13)
        ]
        doc = self.document([[self.paragraph(f"n{i}", line) for i, line in enumerate(notes)]])
        self.assertEqual(signals.find_toc_pages(doc), [])
        entries, pages = signals.extract_printed_toc(doc)
        self.assertEqual(entries, [])
        self.assertEqual(pages, [])

    def test_bibliography_run_does_not_beat_a_real_toc_page(self):
        toc_page = [
            self.marker(),
            self.paragraph("e0", "Acknowledgements xi"),
            self.paragraph("e1", "Introduction xiii"),
            self.paragraph("e2", "Chapter 1 Bringing Transcendents Down to Earth 1"),
            self.paragraph("e3", "Index 291"),
        ]
        biblio_pages = [
            [self.paragraph(f"b{page}-{i}",
                            "APTE, Vaman Shivaram. The Practical Sanskrit-English Dictionary, "
                            f"revised edition. Delhi: Motilal Banarsidass, {1970 + i}.")
             for i in range(12)]
            for page in range(1, 4)
        ]
        doc = self.document([toc_page, *biblio_pages])
        # 参考书目连成 3 页的长 run，旧实现会因“最长 run”选中它。
        self.assertEqual(signals.find_toc_pages(doc), [0])

    def test_index_page_is_not_a_toc_page(self):
        index_lines = [
            "Kong-co. See Chin-ch'eng Kung-chu",
            "Ksanti qilmaq nom bitig 24n",
            "Kuan-fo san-mei hai ching 88n",
            "karma 12, 33n, 74",
            "Lankavatara-sutra 280n",
            "Mahaprajnaparamita-sutra 44n, 91",
            "meditation 3n, 17, 55",
            "nirvana 62n",
            "Sakyamuni 5, 19n",
        ]
        doc = self.document([[self.paragraph(f"i{i}", line) for i, line in enumerate(index_lines)]])
        self.assertTrue(signals._page_looks_like_index(signals._page_lines(doc, 0)))
        self.assertEqual(signals.find_toc_pages(doc), [])

    def test_real_toc_page_passes_the_index_guard(self):
        lines = ["Preface vii", "One The Culture of Authenticity 1", "Two The Enchanted Garden 7",
                 "Notes 87", "Index 95"]
        doc = self.document([[self.paragraph(f"e{i}", line) for i, line in enumerate(lines)]])
        self.assertFalse(signals._page_looks_like_index(signals._page_lines(doc, 0)))
        self.assertTrue(signals._page_looks_like_toc(doc, 0))

    def test_long_toc_entry_is_still_accepted(self):
        # 学术书的长条目标题（≤100 字符）不能被形态判据误杀。
        lines = [
            "Chapter 5 Verbal Self-Presentation and Audience Response in Early Medieval Hagiography 130",
            "Notes 285",
            "Bibliography 300",
            "Index 320",
            "Preface xiii",
        ]
        doc = self.document([[self.paragraph(f"e{i}", line) for i, line in enumerate(lines)]])
        self.assertTrue(signals._page_looks_like_toc(doc, 0))
