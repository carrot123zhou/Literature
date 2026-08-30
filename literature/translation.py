from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from typing import Any, Iterable
from collections.abc import Callable

from .models import Block, TranslationEntry, UsageRecord, block_source_digest, utc_now
from .providers import ProviderError, TextProvider, extract_json
from .storage import ProjectStore, StorageError


TRANSLATABLE_TYPES = {"title", "paragraph", "list", "footnote", "table", "image", "code"}


def draft_glossary(document: Any) -> dict[str, str]:
    """Create a conservative editable draft without spending model tokens."""
    terms: dict[str, str] = {}
    for block in document.blocks:
        if block.type == "title" and 2 <= len(block.source_text) <= 100:
            terms.setdefault(block.source_text, "")
        if document.source_language in {"en", "fr", "de"}:
            for match in re.findall(r"\b(?:[A-Z][\wÀ-ÖØ-öø-ÿ'’-]+(?:\s+|$)){1,4}", block.source_text):
                term = match.strip()
                if 3 <= len(term) <= 80:
                    terms.setdefault(term, "")
        if len(terms) >= 200:
            break
    return terms


def _payload(block: Block, current: TranslationEntry | None = None, source_language: str | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {
        "block_id": block.block_id, "type": block.type, "text": block.source_text,
        "items": block.items, "caption": block.caption, "footnote": block.footnote,
        "cells": [{"cell_id": cell.cell_id, "text": cell.text} for cell in block.cells],
    }
    if source_language:
        value["source_language"] = source_language
    if current:
        value["current_translation"] = {
            "text": current.text, "items": current.items, "caption": current.caption,
            "footnote": current.footnote, "cells": current.cells,
        }
    return value


def _chunks(blocks: list[Block], limit: int = 12000) -> list[list[Block]]:
    chunks: list[list[Block]] = []
    current: list[Block] = []
    size = 0
    for block in blocks:
        block_size = len(block.source_text) + sum(map(len, block.items + block.caption + block.footnote)) + sum(len(cell.text) for cell in block.cells)
        if current and size + block_size > limit:
            chunks.append(current)
            current = []
            size = 0
        current.append(block)
        size += block_size
    if current:
        chunks.append(current)
    return chunks


def _system_prompt(source_language: str, target_language: str, glossary: dict[str, str], *, refine: bool, instructions: str = "") -> str:
    task = "润色已有译文" if refine else "翻译原文"
    return (
        f"你是严谨的人文社科学术翻译。请从 {source_language} 到 {target_language}{task}。"
        "严格保持 block_id 和 cell_id；不得增加、删除、合并或拆分 block。公式、资源引用和结构元数据不翻译。"
        "返回且只返回 JSON：{\"translations\":[{\"block_id\":\"...\",\"text\":\"...\","
        "\"items\":[],\"caption\":[],\"footnote\":[],\"cells\":{\"cell-id\":\"...\"}}]}。"
        f"术语表：{json.dumps(glossary, ensure_ascii=False)}"
        + (f"\n用户附加要求（不得覆盖 ID 与结构约束）：{instructions.strip()}" if instructions.strip() else "")
    )


def _validate_response(expected: list[Block], value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    translations = value.get("translations")
    if not isinstance(translations, list):
        raise ProviderError("模型响应缺少 translations 数组。")
    expected_ids = {block.block_id for block in expected}
    result: dict[str, dict[str, Any]] = {}
    for item in translations:
        if not isinstance(item, dict) or not isinstance(item.get("block_id"), str):
            raise ProviderError("翻译条目缺少 block_id。")
        block_id = item["block_id"]
        if block_id in result:
            raise ProviderError(f"模型重复返回 block：{block_id}")
        result[block_id] = item
    missing, unknown = expected_ids - result.keys(), result.keys() - expected_ids
    if missing or unknown:
        raise ProviderError(f"block ID 校验失败；缺少 {sorted(missing)}，未知 {sorted(unknown)}")
    for block in expected:
        expected_cells = {cell.cell_id for cell in block.cells if cell.text}
        cells = result[block.block_id].get("cells") or {}
        if not isinstance(cells, dict):
            raise ProviderError(f"{block.block_id} 的 cells 必须是对象。")
        if set(cells) != expected_cells:
            raise ProviderError(f"{block.block_id} 的表格 cell ID 不完整。")
    return result


class TranslationService:
    def __init__(self, store: ProjectStore, provider: TextProvider) -> None:
        self.store = store
        self.provider = provider

    def run(
        self, project_id: str, *, block_ids: Iterable[str] | None = None, locale: str = "zh-CN",
        glossary: dict[str, str] | None = None, refine: bool = False, force: bool = False,
        instructions: str = "", job_id: str | None = None, should_pause: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        document = self.store.load_document(project_id)
        layer = self.store.load_translation(project_id, locale)
        block_languages: dict[str, str] = {}
        active_language = document.source_language
        for candidate in document.blocks:
            if candidate.type == "title" and candidate.block_id in document.chapter_language_overrides:
                active_language = document.chapter_language_overrides[candidate.block_id]
            block_languages[candidate.block_id] = active_language
        if glossary is not None:
            layer.glossary_snapshot = dict(glossary)
        selected = set(block_ids or [])
        blocks = []
        for block in document.blocks:
            if selected and block.block_id not in selected:
                continue
            if block.type not in TRANSLATABLE_TYPES:
                continue
            if not (block.source_text or block.items or block.caption or block.footnote or any(cell.text for cell in block.cells)):
                continue
            current = layer.entries.get(block.block_id)
            if current and current.locked:
                continue
            if refine and not current:
                continue
            if not refine and current and not force and current.source_hash == block_source_digest(block) and current.status in {"translated", "refined", "edited"}:
                continue
            blocks.append(block)
        scope = {"block_ids": [block.block_id for block in blocks], "locale": locale}
        config = {"provider": self.provider.config.provider, "model": self.provider.config.model, "refine": refine, "force": force, "instructions": instructions}
        if job_id is None:
            job_id = self.store.create_job(project_id, "refine" if refine else "translate", scope, config)
        if not blocks:
            self.store.update_job(project_id, job_id, status="completed", progress=1)
            return self.store.get_job(project_id, job_id)
        chunks = _chunks(blocks)
        self.store.update_job(project_id, job_id, status="running")
        active_chunk: list[Block] = []
        try:
            for index, chunk in enumerate(chunks):
                active_chunk = chunk
                budget = float(self.store.load_manifest(project_id).get("budget_usd") or 0)
                spent = float(self.store.usage_summary(project_id)["estimated_cost"] or 0)
                if budget > 0 and spent >= budget:
                    self.store.update_job(project_id, job_id, status="budget_paused", progress=index / len(chunks))
                    return self.store.get_job(project_id, job_id)
                if should_pause and should_pause():
                    self.store.update_job(project_id, job_id, status="paused", progress=index / len(chunks))
                    return self.store.get_job(project_id, job_id)
                user = json.dumps({
                    "context_before": [item.source_text for item in document.blocks[max(0, chunk[0].order - 3):chunk[0].order - 1]],
                    "blocks": [_payload(
                        block, layer.entries.get(block.block_id) if refine else None,
                        block_languages.get(block.block_id),
                    ) for block in chunk],
                }, ensure_ascii=False)
                chunk_languages = {block_languages.get(block.block_id, document.source_language) for block in chunk}
                source_language = next(iter(chunk_languages)) if len(chunk_languages) == 1 else "mixed (see each block.source_language)"
                response = self.provider.complete(
                    system=_system_prompt(source_language, locale, layer.glossary_snapshot, refine=refine, instructions=instructions), user=user,
                )
                translated = _validate_response(chunk, extract_json(response.content))
                run_id = response.request_id or uuid.uuid4().hex
                for block in chunk:
                    value = translated[block.block_id]
                    previous = layer.entries.get(block.block_id)
                    revisions = list(previous.revisions) if previous else []
                    if previous:
                        revisions.append({
                            "text": previous.text, "cells": previous.cells, "items": previous.items,
                            "caption": previous.caption, "footnote": previous.footnote,
                            "status": previous.status, "model_run_id": previous.model_run_id, "updated_at": previous.updated_at,
                        })
                    layer.entries[block.block_id] = TranslationEntry(
                        block_id=block.block_id, text=str(value.get("text") or ""),
                        cells={str(key): str(text) for key, text in (value.get("cells") or {}).items()},
                        items=[str(item) for item in value.get("items") or []],
                        caption=[str(item) for item in value.get("caption") or []],
                        footnote=[str(item) for item in value.get("footnote") or []],
                        status="refined" if refine else "translated", source_hash=block_source_digest(block),
                        model_run_id=run_id, locked=False, revisions=revisions,
                        selection_translations=list(previous.selection_translations) if previous else [],
                    )
                self.store.save_translation(layer)
                self.store.record_usage(UsageRecord(
                    usage_id=uuid.uuid4().hex, project_id=project_id, job_id=job_id,
                    provider=response.provider, model=response.model, scope="refine" if refine else "translate",
                    input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                    cached_tokens=response.cached_tokens, reasoning_tokens=response.reasoning_tokens,
                    estimated_cost=response.estimated_cost, request_id=response.request_id,
                ))
                self.store.update_job(project_id, job_id, progress=(index + 1) / len(chunks))
            self.store.update_job(project_id, job_id, status="completed", progress=1)
        except Exception as error:
            for block in active_chunk:
                previous = layer.entries.get(block.block_id)
                if previous and previous.locked:
                    continue
                layer.entries[block.block_id] = TranslationEntry(
                    block_id=block.block_id,
                    text=previous.text if previous else "",
                    cells=dict(previous.cells) if previous else {},
                    items=list(previous.items) if previous else [],
                    caption=list(previous.caption) if previous else [],
                    footnote=list(previous.footnote) if previous else [],
                    status="failed",
                    source_hash=block_source_digest(block),
                    model_run_id=previous.model_run_id if previous else "",
                    locked=False,
                    revisions=list(previous.revisions) if previous else [],
                    selection_translations=list(previous.selection_translations) if previous else [],
                )
            if active_chunk:
                self.store.save_translation(layer)
            self.store.update_job(project_id, job_id, status="failed", error=str(error))
            raise
        return self.store.get_job(project_id, job_id)

    def instant(self, project_id: str, text: str, *, locale: str = "zh-CN", block_id: str | None = None, save: bool = False) -> dict[str, Any]:
        document = self.store.load_document(project_id)
        budget = float(self.store.load_manifest(project_id).get("budget_usd") or 0)
        spent = float(self.store.usage_summary(project_id)["estimated_cost"] or 0)
        if budget > 0 and spent >= budget:
            raise StorageError("项目预算已经用尽；请提高预算或将预算设为 0 后继续。")
        response = self.provider.complete(
            system=f"将以下 {document.source_language} 人文社科文本准确翻译为 {locale}。只返回译文，不要解释。",
            user=text,
        )
        job_id = self.store.create_job(project_id, "instant", {"block_id": block_id, "locale": locale}, {"provider": response.provider, "model": response.model})
        self.store.update_job(project_id, job_id, status="completed", progress=1)
        self.store.record_usage(UsageRecord(
            usage_id=uuid.uuid4().hex, project_id=project_id, job_id=job_id,
            provider=response.provider, model=response.model, scope="instant",
            input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            cached_tokens=response.cached_tokens, reasoning_tokens=response.reasoning_tokens,
            estimated_cost=response.estimated_cost, request_id=response.request_id,
        ))
        if save:
            if not block_id:
                raise StorageError("保存即时翻译需要 block_id。")
            block = document.block(block_id)
            layer = self.store.load_translation(project_id, locale)
            previous = layer.entries.get(block_id)
            layer.entries[block_id] = TranslationEntry(
                block_id=block_id, text=response.content.strip(), status="translated",
                source_hash=block_source_digest(block), model_run_id=response.request_id,
                revisions=list(previous.revisions) if previous else [],
                selection_translations=list(previous.selection_translations) if previous else [],
            )
            self.store.save_translation(layer)
        return {"text": response.content.strip(), "job_id": job_id}

    def edit(self, project_id: str, block_id: str, text: str, *, locale: str = "zh-CN") -> TranslationEntry:
        document = self.store.load_document(project_id)
        block = document.block(block_id)
        layer = self.store.load_translation(project_id, locale)
        previous = layer.entries.get(block_id)
        revisions = list(previous.revisions) if previous else []
        if previous:
            revisions.append(asdict(previous))
        entry = TranslationEntry(
            block_id=block_id, text=text, cells=dict(previous.cells) if previous else {},
            items=list(previous.items) if previous else [], caption=list(previous.caption) if previous else [],
            footnote=list(previous.footnote) if previous else [], status="edited",
            source_hash=block_source_digest(block), model_run_id=previous.model_run_id if previous else "",
            locked=True, revisions=revisions, updated_at=utc_now(),
            selection_translations=list(previous.selection_translations) if previous else [],
        )
        layer.entries[block_id] = entry
        self.store.save_translation(layer)
        return entry

    def unlock(self, project_id: str, block_id: str, *, locale: str = "zh-CN") -> TranslationEntry:
        layer = self.store.load_translation(project_id, locale)
        if block_id not in layer.entries:
            raise StorageError("译文不存在。")
        layer.entries[block_id].locked = False
        layer.entries[block_id].updated_at = utc_now()
        self.store.save_translation(layer)
        return layer.entries[block_id]

    def save_selection(
        self, project_id: str, block_id: str, source_text: str, translated_text: str, *, locale: str = "zh-CN",
    ) -> TranslationEntry:
        document = self.store.load_document(project_id)
        block = document.block(block_id)
        layer = self.store.load_translation(project_id, locale)
        entry = layer.entries.get(block_id) or TranslationEntry(block_id=block_id, source_hash=block_source_digest(block))
        entry.selection_translations.append({
            "source_text": source_text, "translated_text": translated_text,
            "source_hash": block_source_digest(block), "created_at": utc_now(),
        })
        entry.updated_at = utc_now()
        layer.entries[block_id] = entry
        self.store.save_translation(layer)
        return entry
