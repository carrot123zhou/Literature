from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


SCHEMA_VERSION = "1.2"

# bbox 单位契约。MinerU 的 content_list/content_list_v2 在 0-1000 归一化坐标系中
# 给出 x/y，分别按页宽和页高缩放；document.json 原样保留该数值，不做换算。
# 消费方必须先读取 Document.bbox_space 才能解释 bbox。
BBOX_SPACE_MINERU_1000 = "mineru-1000"
BBOX_SPACE_NONE = "none"
BBOX_SPACE_VALUES = (BBOX_SPACE_MINERU_1000, BBOX_SPACE_NONE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class Cell:
    cell_id: str
    row: int
    column: int
    text: str
    rowspan: int = 1
    colspan: int = 1
    header: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Block:
    block_id: str
    type: str
    order: int
    page_index: int
    source_text: str = ""
    bbox: list[float] = field(default_factory=list)
    level: int = 0
    parent_id: str | None = None
    cells: list[Cell] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    resource_refs: list[str] = field(default_factory=list)
    caption: list[str] = field(default_factory=list)
    footnote: list[str] = field(default_factory=list)
    format: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    # 结构版本：每次结构校对改到该块（改类型/层级/升标题）就 +1。译文条目记录
    # 自己依据的版本，两者不一致即 stale，用于「先翻译、后校对」后的复核提示。
    structure_revision: int = 0


@dataclass(slots=True)
class TocNode:
    node_id: str
    title: str
    level: int
    block_id: str
    page_index: int
    children: list["TocNode"] = field(default_factory=list)


@dataclass(slots=True)
class Page:
    page_index: int
    width: float = 0
    height: float = 0
    rotation: int = 0
    source_file: str = ""
    source_page_index: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Document:
    project_id: str
    title: str
    source_language: str
    source_language_mode: str = "auto"
    target_language: str = "zh-CN"
    schema_version: str = SCHEMA_VERSION
    # bbox 的解释方式；旧文档没有该字段时按 MinerU 0-1000 处理，因此无需数据迁移。
    bbox_space: str = BBOX_SPACE_MINERU_1000
    page_count: int = 0
    pages: list[Page] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    toc: list[TocNode] = field(default_factory=list)
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    chapter_language_overrides: dict[str, str] = field(default_factory=dict)
    source_files: list[dict[str, Any]] = field(default_factory=list)
    # 全文解析覆盖：{"source_page_count": int, "ranges": [[a,b],...], "state": "..."}。
    # 只记录已经成功合并进本 document 的源页；旧文档缺失时视为“覆盖未知”。
    coverage: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def bbox_available(self) -> bool:
        """是否存在可用于原页对照的 bbox；非 PDF 或纯 Markdown 导入时为 False。"""
        return self.bbox_space == BBOX_SPACE_MINERU_1000 and any(len(block.bbox) == 4 for block in self.blocks)

    def block(self, block_id: str) -> Block:
        for item in self.blocks:
            if item.block_id == block_id:
                return item
        raise KeyError(block_id)

    def block_position(self, block_id: str) -> int:
        """返回 block 在阅读顺序中的下标，供对照阅读的前后跳转使用。"""
        for index, item in enumerate(self.blocks):
            if item.block_id == block_id:
                return index
        raise KeyError(block_id)

    def page(self, page_index: int) -> Page | None:
        for item in self.pages:
            if item.page_index == page_index:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Document":
        def toc_node(item: dict[str, Any]) -> TocNode:
            return TocNode(
                node_id=item["node_id"], title=item["title"], level=int(item["level"]),
                block_id=item["block_id"], page_index=int(item["page_index"]),
                children=[toc_node(child) for child in item.get("children", [])],
            )

        blocks = []
        for item in value.get("blocks", []):
            data = dict(item)
            data["cells"] = [Cell(**cell) for cell in data.get("cells", [])]
            blocks.append(Block(**data))
        return cls(
            project_id=value["project_id"], title=value["title"],
            source_language=value.get("source_language", "auto"),
            source_language_mode=value.get("source_language_mode", value.get("source_language", "auto")),
            target_language=value.get("target_language", "zh-CN"),
            schema_version=value.get("schema_version", "1.0"),
            bbox_space=value.get("bbox_space", BBOX_SPACE_MINERU_1000),
            page_count=int(value.get("page_count", 0)),
            pages=[Page(**item) for item in value.get("pages", [])], blocks=blocks,
            toc=[toc_node(item) for item in value.get("toc", [])],
            resources=value.get("resources", {}),
            chapter_language_overrides=value.get("chapter_language_overrides", {}),
            source_files=value.get("source_files", []),
            coverage=value.get("coverage", {}) if isinstance(value.get("coverage"), dict) else {},
            created_at=value.get("created_at", utc_now()), updated_at=value.get("updated_at", utc_now()),
        )


PatchOperation = Literal["set_type", "set_level", "set_title"]


@dataclass(slots=True)
class StructurePatch:
    patch_id: str
    block_id: str
    operation: PatchOperation
    before: Any
    after: Any
    confidence: float
    source: Literal["ai", "user"]
    reason: str = ""
    automatic: bool = False
    reverted_at: str | None = None
    created_at: str = field(default_factory=utc_now)


TranslationStatus = Literal["pending", "translated", "refined", "edited", "failed"]


@dataclass(slots=True)
class TranslationEntry:
    block_id: str
    text: str = ""
    cells: dict[str, str] = field(default_factory=dict)
    items: list[str] = field(default_factory=list)
    caption: list[str] = field(default_factory=list)
    footnote: list[str] = field(default_factory=list)
    status: TranslationStatus = "pending"
    source_hash: str = ""
    model_run_id: str = ""
    # 本次任务（job_id）成功处理过该块的证据。旧文件缺此字段时为空，
    # 表示结果来自更早的任务，不能算作“本次任务已完成”。
    run_token: str = ""
    locked: bool = False
    revisions: list[dict[str, Any]] = field(default_factory=list)
    selection_translations: list[dict[str, Any]] = field(default_factory=list)
    # 并发冲突时保留人工内容，把模型刚生成的结果作为候选存这里，供用户对比采用。
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # True 表示当前内容并非对当前源文的完整译文（例如失败尝试留下的未校验片段），
    # 不得被后续补缺复用、不得计入当前完成；复用时必须先校验源 hash 与校验结果。
    partial: bool = False
    # 生成该译文时所依据的 block 结构版本；与 block 当前版本不一致即 stale。
    structure_revision: int = 0
    # 最近一次未能译完的原因（截断 / 协议不合法 / 模型错误）。成功或人工编辑后清空，
    # 让用户在失败列表里能看到“为什么失败”，而不是只有空白的错误栏。
    error: str = ""
    updated_at: str = field(default_factory=utc_now)


@dataclass(slots=True)
class TranslationLayer:
    project_id: str
    locale: str
    entries: dict[str, TranslationEntry] = field(default_factory=dict)
    glossary_snapshot: dict[str, str] = field(default_factory=dict)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "locale": self.locale,
            "entries": {key: asdict(value) for key, value in self.entries.items()},
            "glossary_snapshot": self.glossary_snapshot,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TranslationLayer":
        return cls(
            project_id=value["project_id"], locale=value.get("locale", "zh-CN"),
            entries={key: TranslationEntry(**entry) for key, entry in value.get("entries", {}).items()},
            glossary_snapshot=value.get("glossary_snapshot", {}),
            updated_at=value.get("updated_at", utc_now()),
        )


@dataclass(slots=True)
class UsageRecord:
    usage_id: str
    project_id: str
    job_id: str
    provider: str
    model: str
    scope: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost: float | None = None
    currency: str = "USD"
    request_id: str = ""
    created_at: str = field(default_factory=utc_now)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def block_source_digest(block: Block) -> str:
    """Hash every translatable source field, not just paragraph text."""
    return source_digest(stable_json({
        "text": block.source_text,
        "items": block.items,
        "caption": block.caption,
        "footnote": block.footnote,
        "cells": [{"cell_id": cell.cell_id, "text": cell.text} for cell in block.cells],
    }))
