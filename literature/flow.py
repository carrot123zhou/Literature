"""跨页段落续接（只读派生）。

MinerU 在 ``layout.json`` 里把跨页段落的续行并回上一页的块，并在续页顶部留下一个
``lines_deleted`` 的空块；``content_list_v2.json`` 是同一件事的规范化投影——续页顶部留下
一个空 ``paragraph``（bbox 就是续行区域），上一页承载整段的块则保留了续行的文字。规范化
``document.json`` 会丢弃那个空块，于是对照视图只剩上一页的框，看不到续页的续行区域，也没
有任何跨页标记。

本模块直接读回不可变的 ``content_list_v2``，用 MinerU 自己的空块作为“续页区域”，把它挂到
上一页承载整段的 block 上：

    {block_id: {"next_region": {"page_index": int, "bbox": [x0, y0, x1, y1]}}}

判定是只读派生，不写回 ``document.json``，也不改变阅读顺序；没有 MinerU 原始输出的项目
（纯 Markdown 导入）自然没有续接关系。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import BBOX_SPACE_MINERU_1000, Document

# document.source_files[].representation 只处理 content_list_v2（当前 MinerU 精准解析输出）。
_CONTENT_LIST_V2 = "content_list_v2"


def _paragraph_text(item: dict[str, Any]) -> str | None:
    """正文块的纯文本：非正文返回 ``None``，空正文返回空串。"""
    if item.get("type") != "paragraph":
        return None
    content = item.get("content")
    if not isinstance(content, dict):
        return ""
    parts = []
    for span in content.get("paragraph_content") or []:
        if isinstance(span, dict) and span.get("content"):
            parts.append(str(span["content"]))
    return "".join(parts).strip()


@lru_cache(maxsize=8)
def _read_content_list(path: str, size: int, mtime_ns: int) -> list[Any] | None:
    """按 (路径, 大小, mtime) 缓存不可变的 content_list_v2；raw 层约定写入后不再变更。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, list) else None


def _load_content_list(project: Path, relative: str) -> list[Any] | None:
    path = project / relative
    try:
        stat = path.stat()
    except OSError:
        return None
    return _read_content_list(str(path), stat.st_size, stat.st_mtime_ns)


def _bbox_key(page_index: int, bbox: Any) -> tuple[int, tuple[float, float, float, float]] | None:
    """(页, bbox) 作为块的身份键；两处数值都来自同一份 content_list，取整避免浮点噪声。"""
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    try:
        values = tuple(round(float(value), 3) for value in bbox)
    except (TypeError, ValueError):
        return None
    if values[2] <= values[0] or values[3] <= values[1]:
        return None
    return page_index, values  # type: ignore[return-value]


def _nearest_paragraph(stream: list[tuple[int, dict[str, Any]]], position: int) -> tuple[int, dict[str, Any]] | None:
    """从续页空块向前找最近的非空正文块（跳过页眉/页码等非正文与连续空块）。"""
    for candidate in range(position - 1, -1, -1):
        if _paragraph_text(stream[candidate][1]):
            return stream[candidate]
    return None


def detect_cross_page_flow(document: Document, project: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """返回跨页续接区域：``{block_id: {"next_region": {"page_index", "bbox"}}}``。

    ``project`` 是项目根目录（用于读回 ``ocr/raw`` 里的 content_list_v2）；缺失或非
    ``mineru-1000`` 文档时返回空字典。
    """
    if project is None or document.bbox_space != BBOX_SPACE_MINERU_1000:
        return {}
    project_path = Path(project)

    # 用 (page_index, bbox) 唯一定位承载整段的正文块。
    index: dict[tuple[int, tuple[float, float, float, float]], str] = {}
    for block in document.blocks:
        if block.type != "paragraph":
            continue
        key = _bbox_key(block.page_index, block.bbox)
        if key is not None:
            index.setdefault(key, block.block_id)

    links: dict[str, dict[str, Any]] = {}
    for record in document.source_files:
        if record.get("representation") != _CONTENT_LIST_V2:
            continue
        data = _load_content_list(project_path, str(record.get("path", "")))
        if data is None:
            continue
        offset = int(record.get("page_offset", 0) or 0)
        count = int(record.get("page_count", len(data)) or len(data))
        stream: list[tuple[int, dict[str, Any]]] = []
        for local_page in range(min(count, len(data))):
            page = data[local_page]
            if not isinstance(page, list):
                continue
            for item in page:
                if isinstance(item, dict):
                    stream.append((offset + local_page, item))
        for position, (page_index, item) in enumerate(stream):
            if _paragraph_text(item) != "":
                continue  # 只认 MinerU 在续页留下的空正文占位块
            region = _bbox_key(page_index, item.get("bbox"))
            if region is None:
                continue
            source = _nearest_paragraph(stream, position)
            if source is None:
                continue
            source_key = _bbox_key(source[0], source[1].get("bbox"))
            block_id = index.get(source_key) if source_key else None
            if not block_id:
                continue
            links.setdefault(block_id, {})["next_region"] = {
                "page_index": page_index,
                "bbox": [float(value) for value in item["bbox"]],
            }
    return links


def flow_statistics(links: dict[str, Any]) -> dict[str, int]:
    """给审计/测试用的计数：按续接关系（而非 key）统计。"""
    pairs = sum(1 for value in links.values() if isinstance(value, dict) and "next_region" in value)
    return {"links": pairs, "blocks": len(links)}
