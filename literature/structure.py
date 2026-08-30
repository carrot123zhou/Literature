from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from .mineru import build_toc, render_source_markdown
from .models import Document, StructurePatch, utc_now
from .providers import ProviderError, ProviderResponse, TextProvider, extract_json
from .storage import ProjectStore, StorageError


AUTO_THRESHOLD = 0.97
ALLOWED_AUTOMATIC_OPERATIONS = {"set_type", "set_level"}
TOC_MARKERS = (
    "table of contents", "contents", "sommaire", "table des matières",
    "inhaltsverzeichnis", "inhalt", "目次", "もくじ",
)


def find_toc_page_candidates(document: Document) -> list[dict[str, Any]]:
    """Rank likely TOC pages locally; no text leaves the project."""
    by_page: dict[int, list[Any]] = {}
    for block in document.blocks:
        by_page.setdefault(block.page_index, []).append(block)
    output = []
    for page_index, blocks in by_page.items():
        texts = [block.source_text.strip() for block in blocks if block.source_text.strip()]
        lowered = "\n".join(texts).casefold()
        markers = [marker for marker in TOC_MARKERS if marker in lowered]
        short_lines = sum(1 for text in texts if 2 <= len(text) <= 120)
        page_refs = sum(1 for text in texts if re.search(r"(?:\.{2,}|\s)\d{1,4}\s*$", text))
        structural = sum(1 for block in blocks if block.type in {"title", "list"})
        density = (short_lines + page_refs * 2 + structural) / max(1, len(blocks) * 4)
        confidence = min(0.99, (0.72 if markers else 0) + min(0.25, density * 0.35))
        if confidence >= 0.45:
            reasons = []
            if markers:
                reasons.append("检测到目录标记：" + "、".join(markers))
            if page_refs:
                reasons.append(f"{page_refs} 行疑似页码条目")
            if structural:
                reasons.append(f"{structural} 个标题/列表 block")
            output.append({
                "page_index": page_index, "page_number": page_index + 1,
                "confidence": round(confidence, 3), "reasons": reasons,
                "block_ids": [block.block_id for block in blocks],
            })
    return sorted(output, key=lambda item: (-item["confidence"], item["page_index"]))


def structure_model_payload(document: Document, authoritative_toc: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build the complete auditable model input without any body-block text."""
    toc = [{"title": str(item.get("title", "")), "level": int(item.get("level", 1))} for item in authoritative_toc]
    candidates = []
    for index, block in enumerate(document.blocks):
        if block.type != "title" or not block.source_text:
            continue
        previous = document.blocks[index - 1] if index else None
        following = document.blocks[index + 1] if index + 1 < len(document.blocks) else None
        candidates.append({
            "block_id": block.block_id,
            "text": block.source_text,
            "level": block.level,
            "page_index": block.page_index,
            "bbox": block.bbox,
            "previous": {"type": previous.type, "page_index": previous.page_index} if previous else None,
            "next": {"type": following.type, "page_index": following.page_index} if following else None,
        })
    return {"authoritative_toc": toc, "title_candidates": candidates}


def analyze_structure_with_provider(
    document: Document, authoritative_toc: Iterable[dict[str, Any]], provider: TextProvider,
) -> tuple[list[dict[str, Any]], ProviderResponse]:
    payload = structure_model_payload(document, authoritative_toc)
    response = provider.complete(
        system=(
            "你只校对 OCR 标题结构。根据确认目录和标题候选，判断误识别标题或错误层级。"
            "不得要求、推断或改写正文。只返回 JSON："
            '{"suggestions":[{"block_id":"...","operation":"set_type|set_level",'
            '"after":"paragraph|1-6","confidence":0.0,"reason":"..."}]}。'
        ),
        user=json.dumps(payload, ensure_ascii=False),
    )
    value = extract_json(response.content)
    raw_suggestions = value.get("suggestions")
    if not isinstance(raw_suggestions, list):
        raise ProviderError("结构模型响应缺少 suggestions 数组。")
    candidates = {item["block_id"]: item for item in payload["title_candidates"]}
    suggestions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_suggestions:
        if not isinstance(raw, dict):
            raise ProviderError("结构建议必须是对象。")
        block_id = str(raw.get("block_id", ""))
        operation = str(raw.get("operation", ""))
        if block_id not in candidates or block_id in seen:
            raise ProviderError(f"结构模型返回未知或重复 block：{block_id}")
        if operation not in ALLOWED_AUTOMATIC_OPERATIONS:
            raise ProviderError(f"结构模型返回不允许的操作：{operation}")
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
        before: Any
        after: Any
        if operation == "set_type":
            before, after = "title", str(raw.get("after", ""))
            if after != "paragraph":
                raise ProviderError("AI set_type 只允许将标题降为正文。")
        else:
            before = int(candidates[block_id]["level"])
            after = int(raw.get("after", 0))
            if not 1 <= after <= 6:
                raise ProviderError("AI 标题层级必须在 1 到 6 之间。")
        suggestions.append({
            "block_id": block_id, "operation": operation, "before": before, "after": after,
            "confidence": confidence, "reason": str(raw.get("reason", "")),
            "automatic": confidence >= AUTO_THRESHOLD,
        })
        seen.add(block_id)
    return suggestions, response


def normalize_heading(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE)
    return value


def _toc_matches(title: str, authoritative: list[dict[str, Any]]) -> tuple[float, int | None]:
    target = normalize_heading(title)
    best_score = 0.0
    best_level = None
    for item in authoritative:
        candidate = normalize_heading(str(item.get("title", "")))
        if not target or not candidate:
            continue
        score = SequenceMatcher(a=target, b=candidate).ratio()
        if score > best_score:
            best_score = score
            try:
                best_level = int(item.get("level"))
            except (TypeError, ValueError):
                best_level = None
    return best_score, best_level


def analyze_structure(document: Document, authoritative_toc: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Generate conservative heading-only suggestions without sending body text anywhere."""
    authoritative = list(authoritative_toc)
    suggestions: list[dict[str, Any]] = []
    for block in document.blocks:
        if block.type != "title" or not block.source_text:
            continue
        score, expected_level = _toc_matches(block.source_text, authoritative)
        terminal_punctuation = bool(re.search(r"[。！？.!?;；,:，：]$", block.source_text))
        body_like = len(block.source_text) > 100 or terminal_punctuation
        if score >= 0.92 and expected_level and expected_level != block.level:
            confidence = min(0.995, 0.94 + score * 0.05)
            suggestions.append({
                "block_id": block.block_id, "operation": "set_level", "before": block.level,
                "after": max(1, min(6, expected_level)), "confidence": confidence,
                "reason": "与确认目录条目匹配，但标题层级不同。", "automatic": confidence >= AUTO_THRESHOLD,
            })
        elif score < 0.55 and body_like:
            confidence = 0.98 if len(block.source_text) > 160 else 0.97
            suggestions.append({
                "block_id": block.block_id, "operation": "set_type", "before": "title",
                "after": "paragraph", "confidence": confidence,
                "reason": "未匹配确认目录，且文本形态更接近连续正文。", "automatic": True,
            })
        elif score < 0.55:
            suggestions.append({
                "block_id": block.block_id, "operation": "set_type", "before": "title",
                "after": "paragraph", "confidence": 0.72,
                "reason": "未匹配确认目录，需要人工核对。", "automatic": False,
            })
    return suggestions


def apply_patch(store: ProjectStore, project_id: str, proposal: dict[str, Any], *, source: str = "user") -> StructurePatch:
    document = store.load_document(project_id)
    block = document.block(str(proposal["block_id"]))
    operation = str(proposal["operation"])
    confidence = float(proposal.get("confidence", 1.0))
    automatic = bool(proposal.get("automatic", False))
    if automatic and (operation not in ALLOWED_AUTOMATIC_OPERATIONS or confidence < AUTO_THRESHOLD):
        raise StorageError("该变更不符合自动应用边界。")
    if operation == "set_type":
        before = {"type": block.type, "level": block.level}
        after = str(proposal["after"])
        if after not in {"title", "paragraph"}:
            raise StorageError("结构校对只允许标题与正文之间变更。")
        block.type = after
        if after != "title":
            block.level = 0
        after_value: Any = {"type": block.type, "level": block.level}
    elif operation == "set_level":
        before = block.level
        after = max(1, min(6, int(proposal["after"])))
        block.type = "title"
        block.level = after
        after_value = after
    else:
        raise StorageError(f"不支持的结构变更：{operation}")
    patch = StructurePatch(
        patch_id=uuid.uuid4().hex, block_id=block.block_id, operation=operation,
        before=before, after=after_value, confidence=confidence,
        source="ai" if source == "ai" else "user", reason=str(proposal.get("reason", "")), automatic=automatic,
    )
    document.toc = build_toc(document.blocks)
    store.save_document(document)
    (store.project_path(project_id) / "document" / "source.md").write_text(render_source_markdown(document), encoding="utf-8")
    store.record_patch(project_id, patch)
    return patch


def apply_automatic(store: ProjectStore, project_id: str, proposals: Iterable[dict[str, Any]]) -> list[StructurePatch]:
    applied = []
    for proposal in proposals:
        if proposal.get("automatic") and float(proposal.get("confidence", 0)) >= AUTO_THRESHOLD:
            applied.append(apply_patch(store, project_id, proposal, source="ai"))
    return applied


def revert_patch(store: ProjectStore, project_id: str, patch_id: str) -> StructurePatch:
    patches = store.list_patches(project_id)
    value = next((item for item in patches if item["patch_id"] == patch_id), None)
    if not value:
        raise StorageError("结构补丁不存在。")
    if value.get("reverted_at"):
        raise StorageError("结构补丁已经撤销。")
    document = store.load_document(project_id)
    block = document.block(value["block_id"])
    if value["operation"] == "set_type":
        before = value["before"]
        if isinstance(before, dict):
            block.type = str(before["type"])
            block.level = int(before.get("level", 0))
        else:
            block.type = str(before)
            if block.type == "title" and block.level == 0:
                block.level = 1
    elif value["operation"] == "set_level":
        block.level = int(value["before"])
    else:
        raise StorageError("结构补丁类型不可撤销。")
    document.toc = build_toc(document.blocks)
    store.save_document(document)
    (store.project_path(project_id) / "document" / "source.md").write_text(render_source_markdown(document), encoding="utf-8")
    reverted_at = utc_now()
    store.mark_patch_reverted(project_id, patch_id, reverted_at)
    return StructurePatch(
        patch_id=value["patch_id"], block_id=value["block_id"], operation=value["operation"],
        before=value["before"], after=value["after"], confidence=value["confidence"],
        source=value["source"], reason=value["reason"], automatic=value["automatic"],
        created_at=value["created_at"], reverted_at=reverted_at,
    )
