from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literature.flow import detect_cross_page_flow, flow_statistics
from literature.models import BBOX_SPACE_MINERU_1000, BBOX_SPACE_NONE, Block, Document, Page


MERGED_TEXT = (
    "The gradual/sudden paradigm, which functions as the matrix of the Chan/Zen tradition, "
    "can be seen as a dialectical tension between mediate and immediate understanding"
)


def paragraph(bbox: list[float], text: str) -> dict:
    return {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": text}]}, "bbox": bbox}


def empty_paragraph(bbox: list[float]) -> dict:
    """MinerU 在续页顶部留下的空块：bbox 就是续行区域。"""
    return {"type": "paragraph", "content": {"paragraph_content": []}, "bbox": bbox}


class CrossPageFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.project = Path(self._temporary.name)
        raw = self.project / "ocr" / "raw" / "mineru" / "slice-001"
        raw.mkdir(parents=True, exist_ok=True)
        self.relative = "ocr/raw/mineru/slice-001/book_content_list_v2.json"
        (self.project / self.relative).write_text(json.dumps([
            # 第 1 页：承载整段的正文块 + 页眉。
            [paragraph([100, 700, 900, 960], MERGED_TEXT), {"type": "page_header", "content": {}, "bbox": [300, 30, 600, 50]}],
            # 第 2 页：顶部空占位块（续行区域）+ 下一段正文。
            [empty_paragraph([100, 80, 900, 380]), paragraph([100, 380, 900, 700], "When society comes to be seen as a product of contract.")],
        ]), encoding="utf-8")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _document(self, *, space: str = BBOX_SPACE_MINERU_1000, blocks: list[Block] | None = None, source_files: list[dict] | None = None) -> Document:
        if blocks is None:
            blocks = [
                Block(block_id="p0", type="paragraph", order=1, page_index=0, source_text=MERGED_TEXT, bbox=[100, 700, 900, 960]),
                Block(block_id="p1", type="paragraph", order=2, page_index=1, source_text="When society comes to be seen as a product of contract.", bbox=[100, 380, 900, 700]),
            ]
        return Document(
            project_id="flow", title="Flow", source_language="en", bbox_space=space, page_count=2,
            pages=[Page(page_index=index, width=600, height=800, source_page_index=index) for index in range(2)],
            blocks=blocks,
            source_files=source_files if source_files is not None else [
                {"path": self.relative, "representation": "content_list_v2", "sha256": "", "page_offset": 0, "page_count": 2},
            ],
        )

    def test_region_is_attached_to_the_merged_block(self) -> None:
        links = detect_cross_page_flow(self._document(), self.project)
        self.assertEqual(links, {"p0": {"next_region": {"page_index": 1, "bbox": [100.0, 80.0, 900.0, 380.0]}}})
        self.assertEqual(flow_statistics(links), {"links": 1, "blocks": 1})

    def test_non_empty_paragraphs_are_not_treated_as_regions(self) -> None:
        # 没有任何空占位块时不会产生续接。
        (self.project / self.relative).write_text(json.dumps([
            [paragraph([100, 700, 900, 960], MERGED_TEXT)],
            [paragraph([100, 80, 900, 380], "A separate paragraph.")],
        ]), encoding="utf-8")
        self.assertEqual(detect_cross_page_flow(self._document(), self.project), {})

    def test_without_project_or_bbox_space_there_is_no_flow(self) -> None:
        self.assertEqual(detect_cross_page_flow(self._document()), {})
        self.assertEqual(detect_cross_page_flow(self._document(space=BBOX_SPACE_NONE), self.project), {})

    def test_missing_raw_file_is_tolerated(self) -> None:
        document = self._document(source_files=[{
            "path": "ocr/raw/mineru/slice-001/missing.json", "representation": "content_list_v2",
            "sha256": "", "page_offset": 0, "page_count": 2,
        }])
        self.assertEqual(detect_cross_page_flow(document, self.project), {})

    def test_non_content_list_representation_is_ignored(self) -> None:
        document = self._document(source_files=[{
            "path": self.relative, "representation": "markdown", "sha256": "", "page_offset": 0, "page_count": 2,
        }])
        self.assertEqual(detect_cross_page_flow(document, self.project), {})

    def test_region_without_matching_block_is_skipped(self) -> None:
        # 续页空块找不到上一页的承载块时宁可不连，也不挂到错误的块上。
        document = self._document(blocks=[
            Block(block_id="p1", type="paragraph", order=1, page_index=1, source_text="next", bbox=[100, 380, 900, 700]),
        ])
        self.assertEqual(detect_cross_page_flow(document, self.project), {})


if __name__ == "__main__":
    unittest.main()
