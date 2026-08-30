from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


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
    schema_version: str = "1.0"
    page_count: int = 0
    pages: list[Page] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    toc: list[TocNode] = field(default_factory=list)
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    chapter_language_overrides: dict[str, str] = field(default_factory=dict)
    source_files: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def block(self, block_id: str) -> Block:
        for item in self.blocks:
            if item.block_id == block_id:
                return item
        raise KeyError(block_id)

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
            page_count=int(value.get("page_count", 0)),
            pages=[Page(**item) for item in value.get("pages", [])], blocks=blocks,
            toc=[toc_node(item) for item in value.get("toc", [])],
            resources=value.get("resources", {}),
            chapter_language_overrides=value.get("chapter_language_overrides", {}),
            source_files=value.get("source_files", []),
            created_at=value.get("created_at", utc_now()), updated_at=value.get("updated_at", utc_now()),
        )


PatchOperation = Literal["set_type", "set_level"]


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
    locked: bool = False
    revisions: list[dict[str, Any]] = field(default_factory=list)
    selection_translations: list[dict[str, Any]] = field(default_factory=list)
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
