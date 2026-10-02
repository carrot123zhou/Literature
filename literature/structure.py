from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from .mineru import build_toc, render_source_markdown
from .models import Block, Document, StructurePatch, UsageRecord, utc_now
from .providers import ProviderError, ProviderResponse, TextProvider, extract_json
from .storage import ProjectStore, StorageError


AUTO_THRESHOLD = 0.97
ALLOWED_AUTOMATIC_OPERATIONS = {"set_type", "set_level"}
MANUAL_OPERATIONS = ("set_type", "set_level", "set_title")
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


DEFAULT_TOC_PAGE_LIMIT = 12
TOC_PAGE_MIN_CONFIDENCE = 0.5


def toc_page_text(document: Document, page_indexes: Iterable[int]) -> list[dict[str, Any]]:
    """按页取出目录页的可读文本；这是唯一会把目录页正文发送给模型的入口。"""
    wanted = sorted({int(value) for value in page_indexes})
    wanted_set = set(wanted)
    by_page: dict[int, list[dict[str, str]]] = {}
    for block in document.blocks:
        if block.page_index not in wanted_set:
            continue
        text = (block.source_text or "").strip()
        if not text and block.type == "table":
            text = f"[表格 {len(block.cells)} cells]"
        if text:
            by_page.setdefault(block.page_index, []).append({"type": block.type, "text": text})
    return [
        {"page_index": page_index, "page_number": page_index + 1, "blocks": by_page.get(page_index, [])}
        for page_index in wanted
    ]


def plan_structure_analysis(
    document: Document,
    authoritative_toc: Iterable[dict[str, Any]],
    *,
    extract_toc: bool,
    proofread: bool,
    toc_page_limit: int = DEFAULT_TOC_PAGE_LIMIT,
) -> dict[str, Any]:
    """构建结构分析的完整可审计输入；审计预览与实际作业共用同一函数。"""
    payload = structure_model_payload(document, authoritative_toc)
    payload["options"] = {
        "extract_toc": bool(extract_toc),
        "proofread": bool(proofread),
        "toc_page_limit": max(1, min(40, int(toc_page_limit))),
    }
    payload["toc_pages"] = []
    if extract_toc:
        candidates = find_toc_page_candidates(document)
        pages = sorted({
            int(candidate["page_index"])
            for candidate in candidates
            if float(candidate.get("confidence", 0)) >= TOC_PAGE_MIN_CONFIDENCE
        })[: max(1, min(40, int(toc_page_limit)))]
        payload["toc_pages"] = toc_page_text(document, pages)
    return payload


def extract_toc_with_provider(payload: dict[str, Any], provider: TextProvider) -> tuple[list[dict[str, Any]], ProviderResponse]:
    """让模型从确认的目录页文本提取目录条目（标题 + 层级）。"""
    user = json.dumps(
        {"toc_pages": payload.get("toc_pages", []), "existing_toc": payload.get("authoritative_toc", [])},
        ensure_ascii=False,
    )
    response = provider.complete(
        system=(
            "你从扫描书的目录页 OCR 文本中提取结构化目录。只依据给定的目录页文本，"
            "不要推测正文，不要输出解释。只返回 JSON："
            '{"toc":[{"title":"...","level":1}]}。level 为 1-6 的层级整数，'
            "保持目录原有顺序，去掉页码、点线与页眉页脚。"
        ),
        user=user,
    )
    value = extract_json(response.content)
    raw_entries = value.get("toc")
    if not isinstance(raw_entries, list):
        raise ProviderError("目录提取模型响应缺少 toc 数组。")
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise ProviderError("目录条目必须是对象。")
        title = str(raw.get("title", "")).strip()
        if not title:
            continue
        try:
            level = int(raw.get("level", 1))
        except (TypeError, ValueError):
            level = 1
        entries.append({"title": title[:300], "level": max(1, min(6, level))})
    if not entries:
        raise ProviderError("模型没有提取到任何目录条目。")
    return entries, response


def run_structure_job(
    store: ProjectStore,
    project_id: str,
    job_id: str,
    options: dict[str, Any],
    provider: TextProvider,
) -> dict[str, Any]:
    """异步结构分析：本地识别目录页 → （可选）模型提取目录 → 模型校对标题。

    每一步都写入 job.activity，供界面展示“模型正在做什么”，不必暴露完整对话。
    """
    activity: list[dict[str, Any]] = []

    def emit(text: str, *, level: str = "info", progress: float | None = None) -> None:
        activity.append({"at": utc_now(), "level": level, "text": text})
        store.update_job(project_id, job_id, activity=activity, message=text, progress=progress)

    def record(response: ProviderResponse, scope: str) -> None:
        store.record_usage(UsageRecord(
            usage_id=uuid.uuid4().hex, project_id=project_id, job_id=job_id,
            provider=response.provider, model=response.model, scope=scope,
            input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            cached_tokens=response.cached_tokens, reasoning_tokens=response.reasoning_tokens,
            estimated_cost=response.estimated_cost, request_id=response.request_id,
        ))

    extract_toc = bool(options.get("extract_toc"))
    proofread = bool(options.get("proofread"))
    auto_apply = bool(options.get("auto_apply"))
    try:
        store.update_job(project_id, job_id, status="running", progress=0.02, message="载入规范文档")
        document = store.load_document(project_id)
        emit(f"载入规范文档：{len(document.blocks)} 个 block", progress=0.08)

        payload = plan_structure_analysis(
            document, options.get("toc") or [],
            extract_toc=extract_toc, proofread=proofread,
            toc_page_limit=int(options.get("toc_page_limit") or DEFAULT_TOC_PAGE_LIMIT),
        )
        title_count = len(payload["title_candidates"])
        emit(f"采集标题候选：{title_count} 个标题 block（不发送正文）", progress=0.2)

        proposed_toc: list[dict[str, Any]] = []
        if extract_toc:
            pages = [item["page_index"] + 1 for item in payload["toc_pages"]]
            line_count = sum(len(item["blocks"]) for item in payload["toc_pages"])
            if not payload["toc_pages"]:
                emit("没有找到可信的目录页候选，跳过目录提取；可手工指定目录页后重试。", level="warning", progress=0.35)
            else:
                emit(f"采集目录页：第 {'、'.join(map(str, pages))} 页，共 {line_count} 行文本（将发送给模型）", level="model", progress=0.35)
                proposed_toc, response = extract_toc_with_provider(payload, provider)
                record(response, "structure_extract")
                emit(f"模型提取到 {len(proposed_toc)} 条目录条目", level="success", progress=0.55)

        baseline = proposed_toc or payload["authoritative_toc"]
        suggestions: list[dict[str, Any]] = []
        if proofread:
            if not baseline:
                emit("目录基准为空，无法校对标题层级。先提取目录或粘贴目录文本。", level="warning", progress=0.7)
            else:
                emit(f"模型开始校对 {title_count} 个标题候选与 {len(baseline)} 条目录（不发送正文）", level="model", progress=0.7)
                suggestions, response = analyze_structure_with_provider(document, baseline, provider)
                record(response, "structure_proofread")
                emit(f"模型返回 {len(suggestions)} 条结构建议", level="success", progress=0.85)

        applied = [asdict(value) for value in apply_automatic(store, project_id, suggestions)] if auto_apply else []
        if applied:
            emit(f"自动应用 {len(applied)} 条高置信修正（阈值 {AUTO_THRESHOLD}）", level="success", progress=0.95)
        pending = [value for value in suggestions if not value.get("automatic")]
        if pending:
            emit(f"另有 {len(pending)} 条建议待人工确认", progress=0.97)

        result = {
            "proposed_toc": proposed_toc,
            "suggestions": suggestions,
            "applied": applied,
            "audit": {
                "options": payload["options"],
                "toc_pages": [
                    {"page_index": item["page_index"], "page_number": item["page_number"], "block_count": len(item["blocks"])}
                    for item in payload["toc_pages"]
                ],
                "title_candidates": title_count,
                "body_text_included": False,
                "toc_page_text_included": bool(payload["toc_pages"]),
            },
        }
        emit("结构分析完成", level="success", progress=1.0)
        store.update_job(project_id, job_id, status="completed", progress=1.0, result=result)
        return result
    except Exception as error:
        activity.append({"at": utc_now(), "level": "error", "text": str(error)})
        store.update_job(project_id, job_id, status="failed", error=str(error), activity=activity, message=str(error))
        raise


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


def _normalize_level(value: Any) -> int:
    return max(1, min(6, int(value)))


def _apply_set_type(block: Block, after: Any) -> tuple[Any, Any]:
    """标题与正文互转；before 记录 type 与 level，保证撤销能完整还原。"""
    target = str(after)
    if target not in {"title", "paragraph"}:
        raise StorageError("结构校对只允许标题与正文之间变更。")
    before = {"type": block.type, "level": block.level}
    block.type = target
    if target != "title":
        block.level = 0
    elif block.level < 1:
        block.level = 1
    return before, {"type": block.type, "level": block.level}


def _apply_set_level(block: Block, after: Any) -> tuple[Any, Any]:
    """调整已有标题的层级；不改变 type。"""
    before = block.level
    block.level = _normalize_level(after)
    return before, block.level


def _apply_set_title(block: Block, after: Any) -> tuple[Any, Any]:
    """正文升为标题或修正标题层级；before 记录完整状态，使正文↔标题可逆。"""
    before = {"type": block.type, "level": block.level}
    block.type = "title"
    block.level = _normalize_level(after)
    return before, {"type": block.type, "level": block.level}


_OPERATIONS = {
    "set_type": _apply_set_type,
    "set_level": _apply_set_level,
    "set_title": _apply_set_title,
}


def apply_patch(store: ProjectStore, project_id: str, proposal: dict[str, Any], *, source: str = "user") -> StructurePatch:
    document = store.load_document(project_id)
    block = document.block(str(proposal["block_id"]))
    operation = str(proposal["operation"])
    confidence = float(proposal.get("confidence", 1.0))
    automatic = bool(proposal.get("automatic", False))
    handler = _OPERATIONS.get(operation)
    if handler is None:
        raise StorageError(f"不支持的结构变更：{operation}")
    if automatic and (operation not in ALLOWED_AUTOMATIC_OPERATIONS or confidence < AUTO_THRESHOLD):
        raise StorageError("该变更不符合自动应用边界。")
    before, after_value = handler(block, proposal["after"])
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
    if value["operation"] in {"set_type", "set_title"}:
        before = value["before"]
        if isinstance(before, dict):
            block.type = str(before.get("type", block.type))
            block.level = int(before.get("level", 0) or 0)
        else:
            # 历史补丁（schema 1.0）只记录了 type 字符串，没有完整状态。
            block.type = str(before)
        if block.type == "title" and block.level < 1:
            block.level = 1
    elif value["operation"] == "set_level":
        block.type = "title"
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
