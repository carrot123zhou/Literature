from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable
from collections.abc import Callable

from .models import Block, TranslationEntry, UsageRecord, block_source_digest, utc_now
from .providers import ProviderConfig, ProviderError, ProviderResponse, ProviderTimeoutError, TextProvider
from .request_coordinator import BudgetExhausted, RequestCoordinator, prompt_cost_estimator
from .storage import ProjectStore, StorageError

logger = logging.getLogger(__name__)


TRANSLATABLE_TYPES = {"title", "paragraph", "list", "footnote", "table", "image", "code"}

# 供界面使用的段落级状态机。queued/translating 由翻译作业表达，不落库到条目。
ENTRY_STATES = ("untranslated", "translated", "refined", "edited", "failed", "locked", "stale")

# 分块与并发策略（对齐 TranslateBooksWithLLMs 的思路：小块 + 前后文 + 并发 + 失败切分）。
# 分块预算按估算 token 计量（不是字符）：拉丁文约 4 字/token、CJK 约 1.6 字/token。
# 同一 token 预算下英文块更大（减少请求数）、中文块更小（降低输出截断率），
# 两边都更接近模型的实际输出长度，减少 finish_reason=length 引发的切分重试往返。
DEFAULT_CHUNK_TOKENS = 900
# 除 token 预算外，限制单批字段数：短段落多时，按 token 打包会把几十个字段塞进一个请求，
# 一旦响应超时/漏段就整批失败。先取 12 作试验起点（失败再沿 _split_units 缩批）。
DEFAULT_CHUNK_FIELDS = 12
# 单个字段（段落/单元格/图题/脚注）超过这个字符数时，预先沿句子边界拆成临时子段，
# 让它在失败重试时可以继续缩批，而不是「只有一个 segment，不可再分」直接失败。
MAX_SEGMENT_CHARS = 1200
DEFAULT_PARALLEL_WORKERS = 8
MAX_PARALLEL_WORKERS = 16
# 注：旧作业的并发数从冻结快照读回（不会被新默认值改写），恢复不会变快变慢。
# AIMD 并发自适应（additive increase / multiplicative decrease）：
# 连续成功若干块后并发 +1 探测回用户设定的上限；收到 429 后并发减半。
# 服务级信号量（RequestCoordinator）负责多作业共享同一服务的总量上限，
# 本控制器只在单次作业内部逼近/回退，两者叠加后 429 不再需要整作业退避等待。
AIMD_PROBE_SUCCESS_STREAK = 3
# 一块失败时最多向下切分几层；越深越小，最终退化为逐段重试。
MAX_SPLIT_DEPTH = 3
# 连续多少个分块彻底失败且没有任何成功时，判定模型服务不可用，停止后续请求避免无谓计费。
GLOBAL_FAILURE_LIMIT = 3
# prompt 组装契约版本：冻结进任务快照，方便审计“同一次生成用的是哪版 prompt 语义”。
# v3：术语表从按块过滤的 user prompt 改为按稳定排序注入 system prompt 前缀（prefix caching 友好）。
PROMPT_VERSION = "translation-v3"
GLOSSARY_MAX_ENTRIES = 80
CONTEXT_BEFORE_CHARS = 700
CONTEXT_AFTER_CHARS = 500
PREVIOUS_TRANSLATION_CHARS = 400

# 模型必须按 <seg id="..."> 标签回话。用标签而不是整块 JSON：长中文文本放进 JSON
# 字符串极易被模型转义/截断搞坏，一旦 JSON 解析失败整批就白跑；标签协议可以逐段
# 宽松解析，缺哪段只重试哪段。
_SEGMENT_RE = re.compile(r'<seg\s+id\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</seg\s*>', re.S | re.I)

_CJK_CLASS = "\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef"


def _has_source(block: Block) -> bool:
    return bool(block.source_text or block.items or block.caption or block.footnote or any(cell.text for cell in block.cells))


def is_translatable(block: Block) -> bool:
    return block.type in TRANSLATABLE_TYPES and _has_source(block)


def _entry_has_content(entry: TranslationEntry | None) -> bool:
    if entry is None:
        return False
    return bool(entry.text or entry.items or entry.caption or entry.footnote or any(str(value) for value in entry.cells.values()))


def _revised_has_field(revised: dict[str, Any], key: str) -> bool:
    """候选 `revised` 是否按稳定字段提供了该源字段的非空内容。"""
    if key == "text":
        return bool(str(revised.get("text") or "").strip())
    items = revised.get("items")
    caption = revised.get("caption")
    footnote = revised.get("footnote")
    cells = revised.get("cells")
    if key.startswith("item:"):
        index = int(key.split(":", 1)[1])
        return isinstance(items, list) and index < len(items) and bool(str(items[index]).strip())
    if key.startswith("caption:"):
        index = int(key.split(":", 1)[1])
        return isinstance(caption, list) and index < len(caption) and bool(str(caption[index]).strip())
    if key.startswith("footnote:"):
        index = int(key.split(":", 1)[1])
        return isinstance(footnote, list) and index < len(footnote) and bool(str(footnote[index]).strip())
    if key.startswith("cell:"):
        cell_id = key.split(":", 1)[1]
        return isinstance(cells, dict) and bool(str(cells.get(cell_id) or "").strip())
    return False


def _entry_matches_revised(entry: TranslationEntry, revised: dict[str, Any]) -> bool:
    """当前条目是否已是该候选（用于重复采纳的幂等判定）。"""
    if "text" in revised and str(revised.get("text") or "") != (entry.text or ""):
        return False
    for field_name, stored in (("items", entry.items), ("caption", entry.caption), ("footnote", entry.footnote)):
        values = revised.get(field_name)
        if isinstance(values, list) and [str(value) for value in values] != [str(value) for value in stored]:
            return False
    cells = revised.get("cells")
    if isinstance(cells, dict):
        for cell_id, value in cells.items():
            if str(value) != str(entry.cells.get(cell_id) or ""):
                return False
    return True


def _entry_snapshot(entry: TranslationEntry, *, recorded_at: str | None = None) -> dict[str, Any]:
    """平面内容快照：只保留译文内容与依据元数据。

    历史版本过去用 ``asdict(previous)``，而 previous 自带完整 ``revisions`` /
    ``candidates`` / ``selection_translations``，于是每次重译/编辑都把整段历史再嵌一层，
    translation.json 呈指数式膨胀。版本历史与候选只存内容快照，不嵌历史容器。
    """
    return {
        "block_id": entry.block_id,
        "text": entry.text,
        "cells": dict(entry.cells),
        "items": list(entry.items),
        "caption": list(entry.caption),
        "footnote": list(entry.footnote),
        "status": entry.status,
        "source_hash": entry.source_hash,
        "model_run_id": entry.model_run_id,
        "run_token": entry.run_token,
        "locked": entry.locked,
        "partial": entry.partial,
        "structure_revision": entry.structure_revision,
        "error": entry.error,
        "updated_at": entry.updated_at,
        "recorded_at": recorded_at or utc_now(),
    }


def entry_stale_reason(block: Block, entry: TranslationEntry | None) -> str | None:
    """译文落后的原因：`source` 原文内容已变需复核；`structure` 仅层级/类型调整已自动对齐。"""
    if entry is None or entry.status not in {"translated", "refined", "edited"}:
        return None
    if not _entry_has_content(entry):
        return None
    if entry.source_hash and entry.source_hash != block_source_digest(block):
        return "source"
    # 旧译文可能没有 source_hash。结构编辑记录最后一次原文变动的版本，
    # 防止这类旧译文在合并、拆分或改字后仅被当成「层级已对齐」。
    if not entry.source_hash:
        try:
            content_revision = int((block.raw or {}).get("structure_content_revision") or 0)
        except (TypeError, ValueError):
            content_revision = 0
        if content_revision > int(entry.structure_revision or 0):
            return "source"
    if int(entry.structure_revision or 0) != int(getattr(block, "structure_revision", 0) or 0):
        return "structure"
    return None


def entry_is_stale(block: Block, entry: TranslationEntry | None) -> bool:
    """译文是否落后于当前结构/文本：结构版本变化或源文摘要变化即为 stale。"""
    return entry_stale_reason(block, entry) is not None


_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?%?")
_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_CITATION_RE = re.compile(r"\[\d+(?:\s*[,\u2013-]\s*\d+)*\]")


def entry_quality_warnings(block: Block, entry: TranslationEntry | None) -> list[str]:
    """本地启发式质量告警：数字/URL/引用缺失、译文原样/异常偏短。

    这些只是“供人工复核的提示”，**不是语义判定**，也不会自动改译文或阻止保存；
    只用于把复核成本集中到风险段落，避免默认对全书再加一遍模型请求。
    """
    if entry is None or entry.status not in {"translated", "refined", "edited"} or entry.partial:
        return []
    pairs: list[tuple[str, str]] = []
    if block.source_text and entry.text:
        pairs.append((block.source_text, entry.text))
    for index, item in enumerate(block.items):
        if item and index < len(entry.items) and entry.items[index]:
            pairs.append((item, entry.items[index]))
    for index, item in enumerate(block.caption):
        if item and index < len(entry.caption) and entry.caption[index]:
            pairs.append((item, entry.caption[index]))
    for index, item in enumerate(block.footnote):
        if item and index < len(entry.footnote) and entry.footnote[index]:
            pairs.append((item, entry.footnote[index]))
    for cell in block.cells:
        translated = entry.cells.get(cell.cell_id, "")
        if cell.text and translated:
            pairs.append((cell.text, translated))
    warnings: list[str] = []
    for source, translated in pairs:
        # token 化比较数字：避免 12 被 120 这类子串误判为“已出现”。
        source_numbers = set(_NUMBER_RE.findall(source))
        translated_numbers = set(_NUMBER_RE.findall(translated))
        missing = [number for number in sorted(source_numbers) if number not in translated_numbers]
        if missing:
            warnings.append("数字可能缺失：" + "、".join(missing[:6]))
        for url in dict.fromkeys(_URL_RE.findall(source)):
            if url not in translated:
                warnings.append(f"URL 缺失：{url[:80]}")
        for citation in dict.fromkeys(_CITATION_RE.findall(source)):
            if citation not in translated:
                warnings.append(f"引用标记缺失：{citation}")
        if source.strip() == translated.strip():
            warnings.append("译文与原文完全相同")
        elif len(source) >= 40 and len(translated) < len(source) * 0.3:
            warnings.append("译文明显偏短，可能漏译")
    return list(dict.fromkeys(warnings))


def align_structure_only(document: Any, layer: TranslationLayer) -> list[str]:
    """把仅结构变化的条目对齐到当前 structure_revision，不修改内容、不发模型请求。

    返回被对齐的 block_id；调用方持写锁后自行保存。
    """
    touched: list[str] = []
    for block in document.blocks:
        entry = layer.entries.get(block.block_id)
        if entry is None:
            continue
        if entry_stale_reason(block, entry) == "structure":
            entry.structure_revision = int(getattr(block, "structure_revision", 0) or 0)
            entry.updated_at = utc_now()
            touched.append(block.block_id)
    return touched


def entry_state(block: Block, entry: TranslationEntry | None) -> str:
    """单一显示状态：未译 / 失败 / 锁定 / 待复核(stale) / 已译(含润色、编辑)。"""
    if entry is None or not _entry_has_content(entry):
        return "failed" if (entry is not None and entry.status == "failed") else "untranslated"
    if entry.status == "failed":
        return "failed"
    if entry.locked:
        return "locked"
    if entry_is_stale(block, entry):
        return "stale"
    return entry.status if entry.status in {"translated", "refined", "edited"} else "translated"


def translation_stats(document: Any, layer: TranslationLayer) -> dict[str, Any]:
    """可译块的状态计数与进度；stale 条目不计入已完成，用于提示复核。

    `stale` 是全部待复核条目（含已锁定），与 `stale_block_ids` 保持一致；
    `stale_locked` 是其中被人工锁定、只会提示不会自动重译的部分。
    """
    blocks = [block for block in document.blocks if is_translatable(block)]
    counts = {name: 0 for name in ENTRY_STATES}
    stale_block_ids: list[str] = []
    stale_locked = 0
    stale_source = 0
    stale_source_locked = 0
    stale_structure = 0
    done = 0
    for block in blocks:
        entry = layer.entries.get(block.block_id)
        state = entry_state(block, entry)
        counts[state] = counts.get(state, 0) + 1
        reason = entry_stale_reason(block, entry)
        if reason is not None:
            stale_block_ids.append(block.block_id)
            if reason == "source":
                stale_source += 1
            else:
                stale_structure += 1
            if entry is not None and entry.locked:
                stale_locked += 1
                if reason == "source":
                    stale_source_locked += 1
        if state in {"translated", "refined", "edited", "locked"}:
            done += 1
    total = len(blocks)
    return {
        **counts,
        # 锁定条目的 state 仍是 locked；这里让 stale 覆盖全部待复核块，避免界面漏报。
        "stale": len(stale_block_ids),
        "stale_locked": stale_locked,
        "stale_source": stale_source,
        # 会被“仅待复核”范围实际重译的数量：原文已变且未被人工锁定。
        "stale_source_unlocked": max(0, stale_source - stale_source_locked),
        "stale_structure": stale_structure,
        "total": total,
        "done": done,
        "pending": max(0, total - done),
        "percent": round(done / total * 100, 1) if total else 0.0,
        "stale_block_ids": stale_block_ids,
        # 项目列表摘要需要显示术语表是否已冻结；snapshot 一冻结即视为已创建。
        "glossary_frozen": bool(layer.glossary_snapshot),
    }


def resolve_scope(document: Any, layer: TranslationLayer, scope: str | None, block_ids: Iterable[str] | None) -> set[str] | None:
    """把范围语义解析为 block id 集合；返回 None 表示全部可译块。"""
    identifiers = list(block_ids or [])
    name = (scope or "").strip() or ("blocks" if identifiers else "book")
    if name == "auto":
        name = "blocks" if identifiers else "book"
    if name == "blocks":
        return set(identifiers)
    if name == "book":
        return None
    blocks = [block for block in document.blocks if is_translatable(block)]
    if name == "untranslated":
        return {block.block_id for block in blocks if entry_state(block, layer.entries.get(block.block_id)) == "untranslated"}
    if name == "stale":
        # 只有原文内容变化（source stale）才需要重译；纯层级/类型对齐（structure stale）
        # 已在运行前自动对齐到新版本，不会发送模型请求。已锁定的人工译文仍留给用户确认。
        return {
            block.block_id for block in blocks
            if entry_stale_reason(block, layer.entries.get(block.block_id)) == "source"
            and not (layer.entries.get(block.block_id) and layer.entries[block.block_id].locked)
        }
    if name == "failed":
        return {block.block_id for block in blocks if layer.entries.get(block.block_id) is not None and layer.entries[block.block_id].status == "failed"}
    raise StorageError(f"未知的翻译范围：{scope}")


# payload 版本协议：任何改变派生 payload（locale/译文/文档结构/质量协议）的因素都必须使 revision 失效。
REVISION_PROTOCOL = "translation-payload-v2"


def translation_payload(document: Any, layer: TranslationLayer, *, revision: str = "") -> dict[str, Any]:
    """在译文层原始结构上补充每个 block 的显示状态、stale 标记与统计。"""
    value = layer.to_dict()
    for block in document.blocks:
        entry = layer.entries.get(block.block_id)
        if entry is None:
            continue
        value["entries"][block.block_id] = {
            **value["entries"][block.block_id],
            "state": entry_state(block, entry),
            "stale": entry_is_stale(block, entry),
            "stale_reason": entry_stale_reason(block, entry),
            "quality_warnings": entry_quality_warnings(block, entry),
        }
    stats = translation_stats(document, layer)
    value["stats"] = stats
    value["stale_block_ids"] = stats["stale_block_ids"]
    # revision：覆盖 locale/译文/文档内容结构的版本指纹（同一次 payload 口径）。
    value["revision"] = revision or f"{layer.locale}|{layer.updated_at}|{getattr(document, 'updated_at', '')}|{REVISION_PROTOCOL}"
    return value


# --------------------------------------------------------------------------- #
# 段落（segment）协议
# --------------------------------------------------------------------------- #

def _seg_id(block_id: str, key: str) -> str:
    return f"{block_id}#{key}"


def _source_segments(block: Block) -> list[tuple[str, str]]:
    """一个 block 拆成可独立翻译的字段，键与 block 的字段一一对应。"""
    segments: list[tuple[str, str]] = []
    if block.source_text:
        segments.append(("text", block.source_text))
    for index, item in enumerate(block.items):
        if item:
            segments.append((f"item:{index}", item))
    for index, item in enumerate(block.caption):
        if item:
            segments.append((f"caption:{index}", item))
    for index, item in enumerate(block.footnote):
        if item:
            segments.append((f"footnote:{index}", item))
    for cell in block.cells:
        if cell.text:
            segments.append((f"cell:{cell.cell_id}", cell.text))
    return segments


_SENTENCE_ENDINGS = "。！？!?；;\n"


def _split_long_text(text: str, limit: int = MAX_SEGMENT_CHARS) -> list[str]:
    """把超长文本沿句子/空白边界切成不超过 ``limit`` 的片段（尽可能不切断句子）。"""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max((window.rfind(mark) for mark in _SENTENCE_ENDINGS), default=-1)
        if cut < limit // 3:
            cut = window.rfind(" ")
        if cut < limit // 3:
            cut = limit - 1
        pieces.append(remaining[: cut + 1])
        remaining = remaining[cut + 1:]
    if remaining:
        pieces.append(remaining)
    return pieces


def _expand_segments(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """把超长字段拆成 ``key~i`` 子段；子段拼回同一个稳定字段，不改 canonical 身份。"""
    expanded: list[tuple[str, str]] = []
    for key, text in segments:
        pieces = _split_long_text(text)
        if len(pieces) == 1:
            expanded.append((key, text))
        else:
            expanded.extend((f"{key}~{index}", piece) for index, piece in enumerate(pieces))
    return expanded


def _unit_segments(block: Block) -> list[tuple[str, str]]:
    """翻译输入字段；超长单字段预先拆成可缩批的子段。"""
    return _expand_segments(_source_segments(block))


def _entry_segments(block: Block, entry: TranslationEntry) -> list[tuple[str, str]]:
    """润色 pass 的输入：字段键与原文一致，但取现有译文内容。"""
    segments: list[tuple[str, str]] = []
    if block.source_text and entry.text:
        segments.append(("text", entry.text))
    for index, item in enumerate(block.items):
        if item and index < len(entry.items) and entry.items[index]:
            segments.append((f"item:{index}", entry.items[index]))
    for index, item in enumerate(block.caption):
        if item and index < len(entry.caption) and entry.caption[index]:
            segments.append((f"caption:{index}", entry.caption[index]))
    for index, item in enumerate(block.footnote):
        if item and index < len(entry.footnote) and entry.footnote[index]:
            segments.append((f"footnote:{index}", entry.footnote[index]))
    for cell in block.cells:
        translated = entry.cells.get(cell.cell_id, "")
        if cell.text and translated:
            segments.append((f"cell:{cell.cell_id}", translated))
    return _expand_segments(segments)


def _reusable_previous(block: Block, entry: TranslationEntry | None) -> bool:
    """旧内容是否可作为当前源文的可靠局部结果被复用。

    只有“已校验且对应当前源 hash”的旧内容才能被补齐/覆盖；失败尝试留下的
    未校验片段（partial=True）或源文已变的旧译文一律不得复用。
    """
    return (
        entry is not None
        and not entry.partial
        and bool(entry.source_hash)
        and entry.source_hash == block_source_digest(block)
    )


def _parse_segments(content: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for match in _SEGMENT_RE.finditer(content or ""):
        parsed[match.group(1).strip()] = match.group(2)
    return parsed


def _parse_segment_counts(content: str) -> dict[str, int]:
    """统计每个 <seg id> 出现次数，用于识别模型重复吐同一个 id 的协议错误。"""
    counts: dict[str, int] = {}
    for match in _SEGMENT_RE.finditer(content or ""):
        key = match.group(1).strip()
        counts[key] = counts.get(key, 0) + 1
    return counts


def _merge_segment_maps(*maps: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """按 segment id 深合并，而不是按 block_id 整块覆盖。

    切分重试会把同一个 block 的不同字段分到左右两半，如果按 block_id 直接
    `{**left, **right}`，后到的一半会盖掉另一半已成功的字段（F02 的根因）。
    """
    merged: dict[str, dict[str, str]] = {}
    for mapping in maps:
        for block_id, segments in (mapping or {}).items():
            merged.setdefault(block_id, {}).update(segments)
    return merged


# 模型以输出长度上限结束时，哪怕标签刚好闭合也不能当作完整结果。
_TRUNCATED_FINISH_REASONS = {"length", "max_tokens", "incomplete_stream"}

# 输出 token 里思考（reasoning）占比过高时，截断更可能是“预算被思考吃掉”，
# 而不是真的译文太长；失败提示据此给出可执行的建议。
_REASONING_SQUEEZE_RATIO = 0.6


def _reasoning_squeezed(response: ProviderResponse) -> bool:
    reasoning = int(getattr(response, "reasoning_tokens", 0) or 0)
    output = int(getattr(response, "output_tokens", 0) or 0)
    if reasoning <= 0:
        return False
    if output <= 0:
        return True
    return reasoning >= output * _REASONING_SQUEEZE_RATIO


def _summarize_failures(reasons: dict[str, str], *, limit: int = 240) -> str:
    """把逐块失败原因归类计数，生成一句可放进任务错误栏的摘要。"""
    counts: dict[str, int] = {}
    for raw in reasons.values():
        text = (str(raw or "").strip() or "未知原因")
        counts[text] = counts.get(text, 0) + 1
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    parts = [f"{text}（{count} 段）" for text, count in ordered[:3]]
    if len(ordered) > 3:
        parts.append(f"另有 {len(ordered) - 3} 类原因")
    summary = "；".join(parts)
    return summary if len(summary) <= limit else summary[: limit - 1] + "…"


def _clean_translated_text(text: str) -> str:
    """清理模型输出：去掉残留协议标签、代码围栏、多余空白，并修 CJK 标点周围空格。"""
    if not text:
        return ""
    value = re.sub(r"</?(?:seg|src)\b[^>]*>", "", text, flags=re.I)
    value = value.strip()
    value = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    value = value.strip()
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(rf"(?<=[{_CJK_CLASS}])[ \t]+(?=[{_CJK_CLASS}])", "", value)
    value = re.sub(rf"(?<=[{_CJK_CLASS}])[ \t]+(?=[，。、；：！？）】」』》〕］])", "", value)
    value = re.sub(rf"(?<=[（【「『《〔［])[ \t]+(?=[{_CJK_CLASS}])", "", value)
    return value.strip()


def _filter_glossary(glossary: dict[str, str], text: str) -> dict[str, str]:
    """按块过滤术语表（保留供其它调用方/未来策略使用；主链路已改为稳定前缀注入）。"""
    if not glossary:
        return {}
    haystack = text.lower()
    selected: dict[str, str] = {}
    for source, target in glossary.items():
        term = str(source or "").strip()
        if not term or not str(target or "").strip():
            continue
        if term.lower() in haystack:
            selected[term] = str(target).strip()
        if len(selected) >= GLOSSARY_MAX_ENTRIES:
            break
    return selected


def _stable_glossary(glossary: dict[str, str] | None) -> dict[str, str]:
    """把术语表整理成稳定字节序的前缀（去空项、按源词排序、截到上限）。

    Prefix caching（DeepSeek/Qwen/OpenAI 兼容服务的自动前缀缓存）要求跨请求的
    前缀逐字节一致：系统提示 + 全量排序术语表在整次任务内不变，命中缓存后
    输入费用和 TTFT 都大幅下降。按块过滤会让前缀每块都不同，缓存全废。
    """
    cleaned = {
        str(source or "").strip(): str(target or "").strip()
        for source, target in (glossary or {}).items()
        if str(source or "").strip() and str(target or "").strip()
    }
    return dict(sorted(cleaned.items(), key=lambda item: item[0])[:GLOSSARY_MAX_ENTRIES])


def _render_glossary(glossary: dict[str, str]) -> str:
    if not glossary:
        return ""
    lines = "\n".join(f"{source} → {target}" for source, target in glossary.items())
    return f"# 术语表（整本书统一，必须采用下列译法）\n{lines}\n\n"


def _system_prompt(
    source_language: str, target_language: str, *, refine: bool, instructions: str = "",
    glossary: dict[str, str] | None = None,
) -> str:
    # 术语表注入 system prompt 尾部且按稳定排序：同一任务内所有请求的 system 前缀
    # 逐字节一致，OpenAI 兼容服务的自动前缀缓存才能命中（省输入费与 TTFT）。
    # 代价是不再按块过滤（部分术语当前块未出现），但上限 GLOSSARY_MAX_ENTRIES
    # 的额外 token 在缓存命中后接近免费，且代词指代处也能拿到正确译法。
    extra = f"\n\n# 用户附加要求（不得覆盖上面的完整性与 id 约束）\n{instructions.strip()}" if instructions.strip() else ""
    glossary_block = _render_glossary(_stable_glossary(glossary))
    if refine:
        return (
            f"你是{target_language}文学编辑与学术译审，负责在不改变事实、术语、专名和段落结构的前提下，"
            f"把下面的{target_language}初译稿打磨成流畅、地道的定稿。\n\n"
            "# 润色要求\n"
            f"1. 修正生硬直译、语序不顺、重复用词与标点问题，让它读起来像{target_language}母语作者写的。\n"
            "2. 保持原意与信息完整，不增删、不概括。\n"
            "3. 术语与专名沿用初译；不得把已经中译的专名改回原文。\n"
            "4. 公式、代码、URL、数字与引用标记原样保留。\n"
            "5. 输入中每个 <src id=\"...\"> 是一个独立单元；输出必须为每个 id 生成一个 "
            "<seg id=\"...\">，id、数量与顺序完全一致，不得合并或拆分。\n\n"
            "# 输出格式\n"
            "只输出 <seg id=\"...\">定稿</seg> 序列，不要任何解释、标题或 Markdown 代码块。"
            + extra + (f"\n\n{glossary_block.rstrip()}" if glossary_block else "")
        )
    return (
        f"你是资深的{target_language}学术翻译与母语写作者，负责把{source_language}的人文社科文献完整、准确地译成{target_language}。\n\n"
        "# 翻译原则（按优先级，从高到低）\n"
        "1. 完整：逐句翻译，不省略、不概括、不合并；注释、表头、图注、脚注、参考文献条目都要译。\n"
        "2. 准确：忠实原意、论证结构与语气；不添加原文没有的内容。\n"
        f"3. 自然：用{target_language}学术写作的惯常表达，避免逐字硬译，必要时调整语序。\n"
        "4. 术语一致：同一术语保持同一译法，优先采用给定的术语表。\n"
        f"5. 专名：人名、地名、机构名、出版社与期刊名等一律译为{target_language}，采用学界通行译名；"
        "首次出现时可写作「中译（原文）」。不得把整句原文语言留在译文里。\n"
        "6. 保留：公式、代码、变量、URL、数字与年份、引用标记原样保留，不翻译。\n"
        "7. 结构：输入中每个 <src id=\"...\"> 是一个独立翻译单元；输出必须为每个 id 生成一个 "
        "<seg id=\"...\">，id、数量与顺序与输入完全一致，不得合并、拆分或调换。\n\n"
        "# 输出格式\n"
        "只输出 <seg id=\"...\">译文</seg> 序列，不要任何解释、标题或 Markdown 代码块。"
        + extra + (f"\n\n{glossary_block.rstrip()}" if glossary_block else "")
    )


def _render_units(units: list[tuple[Block, list[tuple[str, str]]]]) -> str:
    lines: list[str] = []
    for block, segments in units:
        for key, text in segments:
            lines.append(f'<src id="{_seg_id(block.block_id, key)}">{text}</src>')
    return "\n".join(lines)


def _user_prompt(
    units: list[tuple[Block, list[tuple[str, str]]]], *,
    context_before: str = "", context_after: str = "", previous_translation: str = "",
) -> str:
    # 术语表已迁入 system prompt 稳定前缀（见 _system_prompt）；user prompt 只保留
    # 跨块变化的部分，让前缀缓存能命中。
    parts: list[str] = []
    if context_before:
        parts.append(f"# 前文（仅供理解语境，不要翻译）\n{context_before}\n")
    if context_after:
        parts.append(f"# 后文（仅供理解语境，不要翻译）\n{context_after}\n")
    if previous_translation:
        parts.append(f"# 上一段已定译文（用于衔接语气，不要重复翻译）\n{previous_translation}\n")
    parts.append("# 待翻译内容\n" + _render_units(units))
    parts.append("\n请为上面每一个 id 输出一个 <seg id=\"...\">译文</seg>，不要输出其他内容。")
    return "\n".join(parts).strip()


# --------------------------------------------------------------------------- #
# 分块与上下文
# --------------------------------------------------------------------------- #

def _chars_per_token(language: str) -> float:
    """按源语言的字符→token 粗比率估计分块体积（对齐 context_optimizer 的经验值）。"""
    value = (language or "").strip().lower()
    if value.startswith(("zh", "ja")):
        return 1.6
    if value.startswith("ko"):
        return 2.2
    if value.startswith("ru"):
        return 3.2
    if value.startswith(("en", "fr", "de", "es", "it", "pt")):
        return 4.0
    return 3.5


def _estimate_tokens(chars: int, language: str) -> int:
    ratio = _chars_per_token(language)
    return max(1, int(chars / ratio)) if chars > 0 else 0


def _block_char_size(block: Block) -> int:
    return sum(len(text) for _, text in _source_segments(block))


def _block_token_size(block: Block, language: str) -> int:
    return _estimate_tokens(_block_char_size(block), language)


def _chunks(
    blocks: list[Block], limit: int = DEFAULT_CHUNK_TOKENS, max_fields: int = DEFAULT_CHUNK_FIELDS,
    *, languages: dict[str, str] | None = None, language: str = "en",
) -> list[list[Block]]:
    """按估算 token/字段预算把 block 切成分块；不跨章节/语言边界，超大单块独占一块。"""
    chunks: list[list[Block]] = []
    current: list[Block] = []
    size = 0
    fields = 0

    def flush() -> None:
        nonlocal current, size, fields
        if current:
            chunks.append(current)
        current, size, fields = [], 0, 0

    for block in blocks:
        segments = _unit_segments(block)
        block_size = _block_token_size(block, language)
        block_fields = len(segments)
        # 章节边界：标题开启新章，不与上一章正文混在同一个请求。
        chapter_break = bool(current) and block.type == "title"
        # 语言边界：章语言覆盖变化时不混在一个请求。
        language_break = False
        if languages is not None and current:
            if languages.get(block.block_id) != languages.get(current[-1].block_id):
                language_break = True
        if current and (chapter_break or language_break or size + block_size > limit or fields + block_fields > max_fields):
            flush()
        current.append(block)
        size += block_size
        fields += block_fields
    if current:
        chunks.append(current)
    return chunks


def _split_units(units: list[tuple[Block, list[tuple[str, str]]]]) -> tuple[list[tuple[Block, list[tuple[str, str]]]], list[tuple[Block, list[tuple[str, str]]]]] | tuple[None, None]:
    """把失败的一批单元沿 segment 中点切成两半；只有 1 个 segment 时不再切。"""
    flat: list[tuple[int, tuple[str, str]]] = []
    for index, (_, segments) in enumerate(units):
        for segment in segments:
            flat.append((index, segment))
    if len(flat) < 2:
        return None, None
    middle = len(flat) // 2
    left_map: dict[int, list[tuple[str, str]]] = {}
    right_map: dict[int, list[tuple[str, str]]] = {}
    for position, (index, segment) in enumerate(flat):
        (left_map if position < middle else right_map).setdefault(index, []).append(segment)
    left = [(units[index][0], left_map[index]) for index in range(len(units)) if index in left_map]
    right = [(units[index][0], right_map[index]) for index in range(len(units)) if index in right_map]
    return left, right


def _batch_units(
    units: list[tuple[Block, list[tuple[str, str]]]],
    limit: int = DEFAULT_CHUNK_TOKENS, max_fields: int = DEFAULT_CHUNK_FIELDS,
    *, language: str = "en",
) -> list[list[tuple[Block, list[tuple[str, str]]]]]:
    """把一批单元按 token/字段预算真正拆成多个请求。

    与 ``_chunks`` 不同：``_chunks`` 只在 block 之间分组（超大单块独占一块，整块仍在一个请求里），
    本函数在**子段级**打包，因此超长单字段会被拆成多次 HTTP，而不是只加多个协议标签。
    同一个 block 的子段可以跨多个请求；调用方聚合完整子段证据后再拼回稳定字段。
    """
    batches: list[list[tuple[Block, list[tuple[str, str]]]]] = []
    current: list[tuple[Block, list[tuple[str, str]]]] = []
    size = 0
    fields = 0
    for block, segments in units:
        for key, text in segments:
            segment_size = _estimate_tokens(len(text), language)
            if current and (fields + 1 > max_fields or size + segment_size > limit):
                batches.append(current)
                current, size, fields = [], 0, 0
            if current and current[-1][0] is block:
                current[-1][1].append((key, text))
            else:
                current.append((block, [(key, text)]))
            size += segment_size
            fields += 1
    if current:
        batches.append(current)
    return batches if batches else ([units] if units else [])


def _chunk_context(
    document: Any, positions: dict[str, int], chunk: list[Block], *, languages: dict[str, str] | None = None,
) -> tuple[str, str]:
    """取前后文，但不跨章节/语言边界；run 与 estimate 共用同一规则。"""
    first = positions.get(chunk[0].block_id, 0)
    last = positions.get(chunk[-1].block_id, first)
    chunk_language = languages.get(chunk[0].block_id) if languages is not None else None
    before_parts: list[str] = []
    total = 0
    for index in range(first - 1, -1, -1):
        block = document.blocks[index]
        # 章节边界：标题开启新章，不再往外取上一章内容。
        if block.type == "title":
            break
        if languages is not None and languages.get(block.block_id) != chunk_language:
            break
        if not is_translatable(block):
            continue
        text = block.source_text or " ".join(block.items)
        if not text:
            continue
        before_parts.append(text)
        total += len(text)
        if total >= CONTEXT_BEFORE_CHARS or len(before_parts) >= 3:
            break
    before = " ".join(reversed(before_parts))[-CONTEXT_BEFORE_CHARS:]
    after_parts: list[str] = []
    total = 0
    for index in range(last + 1, len(document.blocks)):
        block = document.blocks[index]
        if block.type == "title":
            break
        if languages is not None and languages.get(block.block_id) != chunk_language:
            break
        if not is_translatable(block):
            continue
        text = block.source_text or " ".join(block.items)
        if not text:
            continue
        after_parts.append(text)
        total += len(text)
        if total >= CONTEXT_AFTER_CHARS or len(after_parts) >= 3:
            break
    after = " ".join(after_parts)[:CONTEXT_AFTER_CHARS]
    return before, after


# --------------------------------------------------------------------------- #
# 可译块筛选
# --------------------------------------------------------------------------- #

def _eligible_blocks(
    document: Any, layer: TranslationLayer, selected: set[str] | None, *, refine: bool, force: bool,
) -> list[Block]:
    blocks: list[Block] = []
    for block in document.blocks:
        if not is_translatable(block):
            continue
        if selected is not None and block.block_id not in selected:
            continue
        current = layer.entries.get(block.block_id)
        if current and current.locked:
            continue
        if refine:
            if not current or not _entry_has_content(current) or current.status not in {"translated", "refined", "edited"}:
                continue
            blocks.append(block)
            continue
        # 内容与结构都已对齐（含仅层级调整）时跳过；只有 source stale 才重新发送模型。
        if (
            current
            and not force
            and current.status in {"translated", "refined", "edited"}
            and _entry_has_content(current)
            and current.source_hash == block_source_digest(block)
        ):
            continue
        blocks.append(block)
    return blocks


def estimate_translation(
    document: Any, layer: TranslationLayer, *, scope: str | None, block_ids: Iterable[str] | None,
    refine: bool, config: ProviderConfig, force: bool = False, glossary: dict[str, str] | None = None,
    polish: bool = False, instructions: str = "", locale: str = "zh-CN",
) -> dict[str, Any]:
    """预估：复用真实请求组装（分块/前后文/术语/system prompt），给出 token/费用区间。

    不再只用“正文字符/2”，而是按实际会发出的每个请求计算 prompt 字符数（含 system、
    前后文、术语、协议标签），并单独标记润色 pass。价格未知时 cost 为 None，不冒充零成本。
    """
    selected = resolve_scope(document, layer, scope, block_ids)
    candidates = _eligible_blocks(document, layer, selected, refine=refine, force=force)
    positions = {block.block_id: index for index, block in enumerate(document.blocks)}
    # 与真实执行一致：章节/语言边界同样影响分块，预估必须用同一规则。
    block_languages: dict[str, str] = {}
    active_language = document.source_language
    for candidate in document.blocks:
        if candidate.type == "title" and candidate.block_id in document.chapter_language_overrides:
            active_language = document.chapter_language_overrides[candidate.block_id]
        block_languages[candidate.block_id] = active_language
    passes: list[tuple[list[Block], bool]] = [(candidates, refine)]
    if polish and not refine:
        passes.append((candidates, True))

    prompt_chars = 0
    output_chars = 0
    chunk_count = 0
    source_language = document.source_language
    for blocks, pass_refine in passes:
        for chunk in _chunks(blocks, languages=block_languages, language=source_language):
            units: list[tuple[Block, list[tuple[str, str]]]] = []
            for block in chunk:
                entry = layer.entries.get(block.block_id)
                if pass_refine and entry is not None:
                    segments = _entry_segments(block, entry)
                else:
                    segments = _unit_segments(block)
                if segments:
                    units.append((block, segments))
            if not units:
                continue
            context_before, context_after = _chunk_context(document, positions, chunk, languages=block_languages)
            # 与真实执行一致：超预算的单元会被 _batch_units 真正拆成多次请求。
            batched = _batch_units(units, language=source_language)
            chunk_count += len(batched)
            for batch in batched:
                # 与真实执行一致：术语表稳定注入 system prompt（不再按块过滤），
                # 预估的 prompt 字符数含同一份 system 前缀。
                system = _system_prompt(source_language, locale, refine=pass_refine, instructions=instructions, glossary=glossary)
                user = _user_prompt(
                    batch, context_before=context_before, context_after=context_after,
                    previous_translation="",
                )
                prompt_chars += len(system) + len(user)
                output_chars += sum(len(text) for _, segs in batch for _, text in segs)

    # 字符→token 的粗略区间：拉丁 ~4 字/token、CJK ~1.5 字/token，取 1/2 与 1/4 作为区间。
    def tokens(chars: int) -> tuple[int, int]:
        if chars <= 0:
            return 0, 0
        low = chars // 4
        high = max(low, chars // 2)
        return low, high

    input_low, input_high = tokens(prompt_chars)
    output_low, output_high = tokens(output_chars)

    def cost(input_tokens: int, output_tokens: int) -> float | None:
        if config.input_price_per_million is None or config.output_price_per_million is None:
            return None
        return round(
            (input_tokens * config.input_price_per_million + output_tokens * config.output_price_per_million) / 1_000_000, 6,
        )

    return {
        "block_count": len(candidates),
        "source_chars": sum(_block_char_size(block) for block in candidates),
        "prompt_chars": prompt_chars,
        "chunk_count": chunk_count,
        "estimated_input_tokens": (input_low + input_high) // 2,
        "estimated_output_tokens": (output_low + output_high) // 2,
        "estimated_input_tokens_low": input_low,
        "estimated_input_tokens_high": input_high,
        "estimated_output_tokens_low": output_low,
        "estimated_output_tokens_high": output_high,
        "estimated_cost": cost((input_low + input_high) // 2, (output_low + output_high) // 2),
        "estimated_cost_low": cost(input_low, output_low),
        "estimated_cost_high": cost(input_high, output_high),
        "has_pricing": config.input_price_per_million is not None and config.output_price_per_million is not None,
        "polish": bool(polish and not refine),
        "refine": bool(refine),
    }


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


# --------------------------------------------------------------------------- #
# 翻译引擎
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class _ChunkOutcome:
    # block_id -> {完整 seg id: 译文}（仅整块完整通过的块）
    segments: dict[str, dict[str, str]]
    # block_id -> 失败原因（缺段 / 截断 / 模型错误）
    failed: dict[str, str]
    usages: list[ProviderResponse] = field(default_factory=list)
    error: Exception | None = None
    model_run_id: str = ""
    # block_id -> {已解析成功的 seg id: 译文}；失败块的部分字段也在这里，供只重试缺失字段。
    resolved: dict[str, dict[str, str]] = field(default_factory=dict)
    # 预算不足导致本块中止：已解析的子段作为本任务中间证据落盘，恢复只补剩余子段。
    budget_paused: bool = False
    # 本块的任一子请求收到 429（限流）：调度层据此 AIMD 减半并发。
    rate_limited: bool = False

    @property
    def fully_failed(self) -> bool:
        return not self.segments and bool(self.failed)


class TranslationService:
    def __init__(
        self, store: ProjectStore, provider: TextProvider, *,
        coordinator: RequestCoordinator | None = None, service_id: str = "",
    ) -> None:
        self.store = store
        self.provider = provider
        # 可选的共用请求协调层（服务级并发/预算预留）；未提供时也建一个实例级的，
        # 保证预算守卫始终生效（已花从持久账本 seed，服务级并发在跨作业时由 JobManager 共享）。
        self.coordinator = coordinator or RequestCoordinator(
            spent_loader=lambda pid: float(store.usage_summary(pid)["estimated_cost"] or 0),
        )
        self.service_id = service_id or ""
        # 作业内的 canonical 源快照缓存（按 document.json mtime+size 失效）：
        # 每次提交都重新核对 mtime，但只在文件真的变了时才重新解析整份文档。
        self._doc_cache: tuple[tuple[int, int], dict[str, tuple[str, int]]] | None = None
        self._active_project = ""
        self._active_job_id = ""
        self._active_refine = False
        self._active_scope = "translate"
        # 本任务已付费并落盘的中间子段（不是完成译文）：预算暂停/崩溃恢复时只补剩余子段。
        self._partial_segments: dict[str, dict[str, Any]] = {}
        self._partial_lock = threading.Lock()
        self._budget_cache: float | None = None
        self._cache_lock = threading.Lock()

    # ---- 请求协调（服务并发 + 预算预留 + 未知费用） --------------------- #

    def _estimate_request_cost(self, prompt_chars: int) -> float:
        # 与 request_coordinator.prompt_cost_estimator 保持单一实现，避免三处公式漂移。
        return prompt_cost_estimator(self.provider.config)(prompt_chars)

    def _record_response(self, response: ProviderResponse) -> None:
        self.store.record_usage(UsageRecord(
            usage_id=uuid.uuid4().hex, project_id=self._active_project, job_id=self._active_job_id,
            provider=response.provider, model=response.model,
            scope=self._active_scope,
            input_tokens=response.input_tokens, output_tokens=response.output_tokens,
            cached_tokens=response.cached_tokens, reasoning_tokens=response.reasoning_tokens,
            estimated_cost=response.estimated_cost, request_id=response.request_id,
        ))

    def _complete(self, system: str, user: str) -> ProviderResponse:
        if not self._active_project:
            try:
                response = self.provider.complete(system=system, user=user)
            except ProviderError as error:
                # 无协调层时也要保留错误响应已拿到的 usage，不当作零消耗。
                captured = getattr(error, "response", None)
                if self._active_project and isinstance(captured, ProviderResponse):
                    self._record_response(captured)
                raise
            if self._active_project:
                self._record_response(response)
            return response
        project_id = self._active_project
        # 真实 provider 若已在每次 attempt 预留预算（hooks_include_budget），就不再外层预留；
        # 其他 provider（Mock/fake）仍由外层预留 + 服务额度。
        hooks_budget = bool(getattr(self.provider, "_hooks_include_budget", False))
        has_hooks = getattr(self.provider, "attempt_hooks", None) is not None
        attempt_state = getattr(self.provider, "_attempt_state", None)
        reservation = None
        if not hooks_budget:
            reservation = self.coordinator.reserve(
                project_id, cost=self._estimate_request_cost(len(system) + len(user)),
                budget=self._budget_value(project_id),
            )
            if reservation is None:
                raise BudgetExhausted("项目预算不足，已停止提交新的模型请求")

        def settle(actual_cost: float | None) -> None:
            """在写账后同一处对账：优先 attempt 预留，其次外层预留，否则直接计入已花。"""
            attempt_reservation = getattr(attempt_state, "handle", None) if attempt_state is not None else None
            if attempt_state is not None:
                attempt_state.handle = None
            if hooks_budget and attempt_reservation is not None:
                self.coordinator.reconcile(attempt_reservation, actual_cost=actual_cost)
            elif reservation is not None:
                self.coordinator.reconcile(reservation, actual_cost=actual_cost)

        try:
            if has_hooks:
                response = self.provider.complete(system=system, user=user)
            else:
                # 无 attempt hooks 的 provider 也要共享服务级并发。
                with self.coordinator.limit(self.service_id):
                    response = self.provider.complete(system=system, user=user)
            actual_cost = response.estimated_cost
            try:
                self._record_response(response)
            except Exception:
                # 响应已获得但写账失败：写持久待对账证据（含已知 usage/cost），
                # 仍按已知费用对账，绝不能释放成零费用后被当成从未花钱而重发。
                self.coordinator.note_unknown_cost(project_id)
                self.store.record_pending_cost(
                    project_id, job_id=self._active_job_id, reason="usage-write-failed",
                    provider=response.provider, model=response.model, request_id=response.request_id,
                    input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                    estimated_cost=actual_cost,
                )
            settle(actual_cost)
            return response
        except ProviderError as error:
            captured = getattr(error, "response", None)
            actual_cost: float | None = None
            if isinstance(captured, ProviderResponse):
                self._record_response(captured)
                actual_cost = captured.estimated_cost
            else:
                self.store.record_pending_cost(
                    project_id, job_id=self._active_job_id, reason="timeout-or-no-response",
                    provider=getattr(self.provider.config, "provider", ""), model=getattr(self.provider.config, "model", ""),
                )
            settle(actual_cost)
            raise
        except BaseException:
            # 崩溃/其他异常：把在途 attempt 预留按未知结算，避免静默变零。
            settle(None)
            raise
        finally:
            if reservation is not None:
                self.coordinator.release(reservation)

    def _record_validated_segments(self, block_id: str, segments: dict[str, str], source_hash: str) -> None:
        """每个协议校验通过的子响应立即原子落盘为中间证据，崩溃/暂停只补缺失子段。"""
        if not segments or not source_hash or not self._active_project or not self._active_job_id:
            return
        with self._partial_lock:
            existing = self._partial_segments.get(block_id)
            merged = dict(existing.get("segments")) if isinstance(existing, dict) else {}
            merged.update(segments)
            self._partial_segments[block_id] = {
                "segments": merged, "source_hash": source_hash, "refine": bool(self._active_refine),
            }
            self._save_checkpoint(self._active_project, self._active_job_id, {
                "partial_segments": {bid: dict(record) for bid, record in self._partial_segments.items()},
                "partial_meta": {"run_token": self._active_job_id, "refine": bool(self._active_refine), "recorded_at": utc_now()},
            })

    def _partial_snapshot(self) -> dict[str, dict[str, Any]]:
        """在锁内取一份中间子段快照：工作线程不能再直接引用会变化的主字典。"""
        with self._partial_lock:
            return {bid: dict(record) for bid, record in self._partial_segments.items()}

    def _forget_partials(self, block_ids: Iterable[str]) -> None:
        """在锁内清除已完成的中间子段，避免主线程 pop 与工作线程迭代并发。"""
        with self._partial_lock:
            for block_id in block_ids:
                self._partial_segments.pop(block_id, None)

    def _persist_partial_segments(self, project_id: str, job_id: str, outcome: _ChunkOutcome, chunk: list[Block]) -> None:
        """把已付费但未完成的子段落盘为本任务中间证据（同源、同阶段才可使用）。"""
        for block in chunk:
            if block.block_id in outcome.segments:
                continue
            segments = outcome.resolved.get(block.block_id) or {}
            if segments:
                self._record_validated_segments(block.block_id, dict(segments), block_source_digest(block))

    def _budget_value(self, project_id: str) -> float:
        if self._budget_cache is None:
            with self._cache_lock:
                if self._budget_cache is None:
                    self._budget_cache = float(self.store.load_manifest(project_id).get("budget_usd") or 0)
        return self._budget_cache

    def _canonical_digests(self, project_id: str) -> dict[str, tuple[str, int]]:
        path = self.store.project_path(project_id) / "document" / "document.json"
        try:
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return {}
        if self._doc_cache is not None and self._doc_cache[0] == key:
            return self._doc_cache[1]
        document = self.store.load_document(project_id)
        digests = {
            block.block_id: (block_source_digest(block), int(getattr(block, "structure_revision", 0) or 0))
            for block in document.blocks
        }
        self._doc_cache = (key, digests)
        return digests

    # ---- 对外入口 ------------------------------------------------------- #

    def run(
        self, project_id: str, *, block_ids: Iterable[str] | None = None, locale: str = "zh-CN",
        glossary: dict[str, str] | None = None, refine: bool = False, force: bool = False,
        instructions: str = "", scope: str | None = None, polish: bool = False,
        parallel_workers: int | None = None,
        job_id: str | None = None, should_pause: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        document = self.store.load_document(project_id)
        layer = self.store.load_translation(project_id, locale)
        # 请求协调上下文：本次请求走哪个项目/作业/阶段，供预算预留与用量写账使用。
        self._active_project = project_id
        self._active_refine = bool(refine)
        self._active_scope = "refine" if refine else "translate"
        self._budget_cache = None
        self._active_job_id = job_id or ""
        # 真实 provider 的每次 HTTP attempt 也进入公共协调层（服务并发/退避 + 项目预算）。
        self.coordinator.attempt_hooks_for(
            self.service_id, self.provider,
            project_id=self._active_project, budget=self._budget_value(self._active_project),
            cost_of=self._estimate_request_cost,
        )
        # 恢复已有作业：先读 checkpoint，本次任务的术语/附加要求/模型参数以“冻结快照”为准，
        # 不允许用户后来改服务或术语表静默改变同一次生成；密钥仍运行时从当前服务读，不进快照。
        resume_checkpoint = self._load_checkpoint(project_id, job_id) if job_id else None
        frozen_manifest = (resume_checkpoint or {}).get("frozen") if resume_checkpoint else None
        if isinstance(frozen_manifest, dict):
            frozen_glossary = frozen_manifest.get("glossary")
            if isinstance(frozen_glossary, dict):
                glossary = dict(frozen_glossary)
            if isinstance(frozen_manifest.get("instructions"), str):
                instructions = frozen_manifest["instructions"]
            if isinstance(frozen_manifest.get("model"), dict):
                self._apply_frozen_model(frozen_manifest["model"])
            # 任务级执行标志也按冻结值恢复，不让恢复请求静默改变本轮语义。
            force = bool(frozen_manifest.get("force", force))
            polish = bool(frozen_manifest.get("polish", polish))
            if frozen_manifest.get("prompt_version") not in (None, PROMPT_VERSION):
                logger.warning(
                    "Translation job %s was frozen with prompt version %s; current is %s",
                    job_id, frozen_manifest.get("prompt_version"), PROMPT_VERSION,
                )
        # 运行前快照：提交时用它识别“运行期间发生的人工编辑/附属记录”，避免整层旧快照覆盖（F01）。
        started_at = utc_now()
        baseline = {block_id: self._entry_signature(entry) for block_id, entry in layer.entries.items()}
        baseline_glossary = dict(layer.glossary_snapshot)
        block_languages: dict[str, str] = {}
        active_language = document.source_language
        for candidate in document.blocks:
            if candidate.type == "title" and candidate.block_id in document.chapter_language_overrides:
                active_language = document.chapter_language_overrides[candidate.block_id]
            block_languages[candidate.block_id] = active_language
        if glossary is not None:
            layer.glossary_snapshot = dict(glossary)
        # 纯层级/类型对齐不重译：先把仅结构变化的条目对齐到当前版本（F04）。
        self._align_structure_only_stale(project_id, locale, document, layer, baseline, baseline_glossary, started_at)
        scope_value = {"scope": (scope or ("blocks" if block_ids else "book")), "block_ids": list(block_ids or []), "locale": locale}
        config = {
            "provider": self.provider.config.provider, "model": self.provider.config.model,
            "refine": refine, "force": force, "instructions": instructions,
            "polish": polish, "parallel_workers": self._worker_count(parallel_workers),
        }
        if job_id is None:
            job_id = self.store.create_job(project_id, "refine" if refine else "translate", scope_value, config)
        self._active_job_id = job_id
        # 恢复本任务的中间子段证据（绑定 run_token/pass）；不匹配则丢弃，不冒充本次结果。
        partial_meta = (resume_checkpoint or {}).get("partial_meta") or {}
        if partial_meta.get("run_token") == job_id:
            raw_partials = (resume_checkpoint or {}).get("partial_segments") or {}
            self._partial_segments = {
                str(bid): dict(segs) for bid, segs in raw_partials.items() if isinstance(segs, dict)
            }
        else:
            self._partial_segments = {}
        workers = self._worker_count(parallel_workers)
        polish_pending = polish and not refine

        # 恢复契约：existing job 读持久化 checkpoint（本次目标 block ids + 当前 pass + 完成清单），
        # 不再用当前 entries 重新按 scope 筛选。否则 scope=untranslated/force 在恢复时会丢掉
        # 原任务范围：untranslated 恢复后为空、force 会把已完成块重新初译（重复付费）。
        checkpoint = resume_checkpoint
        if checkpoint is not None:
            target_ids = list(checkpoint.get("target_ids") or [])
            polish_target_ids = list(checkpoint.get("polish_target_ids") or [])
            polish_completed = list(checkpoint.get("polish_completed") or [])
            translate_failed = dict(checkpoint.get("translate_failed") or {})
            polish_failed = dict(checkpoint.get("polish_failed") or {})
        else:
            blocks = _eligible_blocks(document, layer, resolve_scope(document, layer, scope, block_ids), refine=refine, force=force)
            target_ids = [block.block_id for block in blocks]
            polish_target_ids = []
            polish_completed = []
            translate_failed = {}
            polish_failed = {}

        if not target_ids:
            counts, _reasons = self._task_accounting(
                layer, target_ids=[], translate_completed=[], polish_target_ids=[], polish_completed=[],
                translate_failed={}, polish_failed={}, source_changed_ids=set(),
            )
            self.store.update_job(
                project_id, job_id, status="completed", progress=1,
                message="没有需要处理的段落。", result=self._merged_result(project_id, job_id, counts),
            )
            return self.store.get_job(project_id, job_id)

        # 以“真正保存下来的有效结果”决定还要初译哪些块：checkpoint.pass 只记录阶段，
        # 不能据此跳过仍未初译成功的目标（否则 partially failed 的初译失败段恢复后永远不再重试）。
        source_changed_ids = self._source_changed_ids(document, layer, target_ids)
        run_token = job_id
        # 本次任务的完成证据 = 磁盘上带本次 run_token 的结果 ∪ 本任务自己写的 checkpoint。
        # 绝不能用“磁盘上所有历史有效译文”：那会把 force 前的旧译文当成“本次已完成”，
        # 崩溃恢复后跳过从未重译的块并假报完成（Lead 复现：resume_calls=0）。
        task_done = set(self._task_done_ids(layer, target_ids, refine=refine, run_token=run_token)) - source_changed_ids
        task_done |= self._checkpoint_subset(checkpoint, "translate_completed", target_ids) - source_changed_ids
        # `frozen` 记录本次任务的非敏感模型配置、源文摘要和术语版本；只在首次写入，
        # 恢复时不因用户后来改了服务/术语表而被覆盖，保证审计与复现语义稳定。
        frozen = (checkpoint or {}).get("frozen") or self._freeze_manifest(
            document, layer, target_ids, locale, refine, instructions, force=force, polish=polish,
        )
        self._save_checkpoint(project_id, job_id, {
            "pass": "translate", "target_ids": target_ids, "frozen": frozen,
            "translate_completed": [bid for bid in target_ids if bid in task_done],
            "polish_target_ids": polish_target_ids, "polish_completed": polish_completed,
            "translate_failed": translate_failed, "polish_failed": polish_failed,
        })
        # 全新任务按 target_ids 全部处理（force/未译范围必须真正执行）；
        # 只有恢复已有 checkpoint 的任务才跳过“本次任务”已完成的块，避免重复付费。
        if checkpoint is None:
            translate_pending = list(target_ids)
        else:
            translate_pending = [bid for bid in target_ids if bid not in task_done]
        blocks = self._blocks_for_ids(document, layer, translate_pending)

        translate_span = 0.5 if polish_pending else 1.0
        summary = self._empty_summary()
        polish_stage_stopped = False
        if blocks:
            summary = self._process_blocks(
                project_id, job_id, document, layer, blocks,
                refine=refine, instructions=instructions, locale=locale, block_languages=block_languages,
                workers=workers, should_pause=should_pause, progress_base=0.0, progress_span=translate_span,
                started_at=started_at, baseline=baseline, baseline_glossary=baseline_glossary,
                # 强制重译必须全字段重发，不复用任何旧内容。
                reuse_previous=not force, run_token=run_token,
            )
        # 处理完后以真实保存结果重算：源文在任务期间变化的旧译不算完成，不计入完成清单。
        source_changed_ids = self._source_changed_ids(self.store.load_document(project_id), layer, target_ids)
        # “仍失败”过滤用当前有效译文（全局）；写入 checkpoint 与终态计数用“本次任务完成”来源。
        valid_now = [bid for bid in self._done_ids(layer, target_ids, refine=refine) if bid not in source_changed_ids]
        task_done |= set(self._task_done_ids(layer, target_ids, refine=refine, run_token=run_token))
        task_done -= source_changed_ids
        translate_completed = [bid for bid in target_ids if bid in task_done]
        # 只保留“当前仍失败”的块：本轮新失败并集，去掉后来重试成功的块，避免历史失败重复计数。
        translate_failed.update(summary["failed_reasons"])
        translate_failed = {bid: reason for bid, reason in translate_failed.items() if bid not in set(valid_now)}
        # checkpoint 只记录“本次任务完成”，不把全局有效译文写成本次完成。
        self._save_checkpoint(project_id, job_id, {
            "pass": "translate", "target_ids": target_ids,
            "translate_completed": translate_completed,
            "polish_target_ids": polish_target_ids, "polish_completed": polish_completed,
            "translate_failed": translate_failed, "polish_failed": polish_failed,
            "partial_segments": {bid: dict(segs) for bid, segs in self._partial_snapshot().items() if bid not in task_done},
        })

        if polish_pending and summary["stop_reason"] is None:
            # 每次都用当前真实结果重算可润色范围：包含恢复后新初译成功、尚未润色的段，
            # 同时排除已源变、已锁定或未校验的块；已在 polish_completed 的不会重复计费。
            polish_target_ids = [
                bid for bid in target_ids
                if bid not in source_changed_ids
                and (entry := layer.entries.get(bid)) is not None
                and entry.status in {"translated", "refined"} and not entry.locked and not entry.partial
            ]
            polish_task_done = set(self._task_done_ids(layer, polish_target_ids, refine=True, run_token=run_token))
            polish_task_done |= self._checkpoint_subset(checkpoint, "polish_completed", polish_target_ids)
            polish_ids = [
                bid for bid in polish_target_ids
                if bid not in polish_task_done
                and (entry := layer.entries.get(bid)) is not None
                and entry.status == "translated" and not entry.locked and not entry.partial
            ]
            polish_blocks = self._blocks_for_ids(document, layer, polish_ids)
            self._save_checkpoint(project_id, job_id, {
                "pass": "polish", "target_ids": target_ids, "translate_completed": [bid for bid in target_ids if bid in task_done],
                "polish_target_ids": polish_target_ids,
                "polish_completed": [bid for bid in polish_target_ids if bid in polish_task_done],
                "translate_failed": translate_failed, "polish_failed": polish_failed,
            })
            if polish_blocks:
                polish_summary = self._process_blocks(
                    project_id, job_id, document, layer, polish_blocks,
                    refine=True, instructions=instructions, locale=locale, block_languages=block_languages,
                    workers=workers, should_pause=should_pause, progress_base=0.5, progress_span=0.5,
                    started_at=started_at, baseline=baseline, baseline_glossary=baseline_glossary,
                    run_token=run_token,
                )
                summary["failed_block_ids"] |= polish_summary["failed_block_ids"]
                summary["failed_reasons"].update(polish_summary["failed_reasons"])
                summary["success_blocks"] += polish_summary["success_blocks"]
                summary["total_blocks"] += polish_summary["total_blocks"]
                # 润色阶段被暂停/预算中止时，必须把状态带出来，不能显示为全部完成（F05）。
                if polish_summary["stop_reason"]:
                    summary["stop_reason"] = polish_summary["stop_reason"]
                    polish_stage_stopped = True
                if summary["first_error"] is None:
                    summary["first_error"] = polish_summary["first_error"]
                valid_polish = self._done_ids(layer, polish_target_ids, refine=True)
                polish_task_done |= set(self._task_done_ids(layer, polish_target_ids, refine=True, run_token=run_token))
                polish_completed = [bid for bid in polish_target_ids if bid in polish_task_done]
                polish_failed.update(polish_summary["failed_reasons"])
                polish_failed = {bid: reason for bid, reason in polish_failed.items() if bid not in set(valid_polish)}
                self._save_checkpoint(project_id, job_id, {
                    "pass": "polish", "target_ids": target_ids, "translate_completed": [bid for bid in target_ids if bid in task_done],
                    "polish_target_ids": polish_target_ids,
                    "polish_completed": [bid for bid in polish_target_ids if bid in polish_task_done],
                    "translate_failed": translate_failed, "polish_failed": polish_failed,
                })

        return self._finalize_job(
            project_id, job_id, layer, summary, polish_stage=polish_stage_stopped,
            target_ids=target_ids, translate_completed=translate_completed,
            polish_target_ids=polish_target_ids, polish_completed=polish_completed,
            translate_failed=translate_failed, polish_failed=polish_failed,
            source_changed_ids=source_changed_ids,
        )

    # ---- 恢复 checkpoint ------------------------------------------------ #

    def _load_checkpoint(self, project_id: str, job_id: str) -> dict[str, Any] | None:
        try:
            job = self.store.get_job(project_id, job_id)
        except StorageError:
            return None
        result = job.get("result") or {}
        if isinstance(result, dict) and result.get("checkpoint_version"):
            return result
        return None

    def _apply_frozen_model(self, frozen: dict[str, Any]) -> None:
        """按冻结快照恢复非敏感模型参数（模型/端点/温度/输出上限）；密钥不在快照内。

        密钥是**运行时从当前服务**读的，因此只有能确认「冻结时与现在指向同一服务地址」
        才能应用；否则会把新服务的密钥发往旧地址（安全与计费风险）。
        - 冻结 service_id 与当前不一致 → 拒绝。
        - 冻结 base_url 与当前不一致 → 拒绝（即使 service_id 相同：地址已被改过，
          旧地址可能已属于另一个服务，不得拿当前密钥去访问）。
        - 两者都能确认一致时才应用。
        """
        config = self.provider.config
        frozen_service = str(frozen.get("service_id") or "")
        current_service = str(self.service_id or "")
        frozen_base = str(frozen.get("base_url") or "").rstrip("/")
        current_base = str(getattr(config, "base_url", "") or "").rstrip("/")
        if frozen_service and current_service and frozen_service != current_service:
            raise ProviderError(
                f"该任务由模型服务 {frozen_service} 创建，当前为 {current_service}；"
                "为避免把当前服务的密钥发往旧地址，已拒绝恢复。请改回原服务商，或另建任务。"
            )
        if frozen_base and current_base and frozen_base != current_base:
            raise ProviderError(
                "该任务的模型地址在当前服务上已改变；为避免把当前密钥发往旧地址，已拒绝恢复。"
                "请改回原地址，或另建任务。"
            )
        for name in ("model", "base_url", "temperature", "max_output_tokens"):
            value = frozen.get(name)
            if value is not None:
                setattr(config, name, value)

    def _freeze_manifest(
        self, document: Any, layer: TranslationLayer, target_ids: list[str], locale: str, refine: bool,
        instructions: str = "", *, force: bool = False, polish: bool = False,
    ) -> dict[str, Any]:
        """冻结本次任务的目标、源文摘要、非敏感模型配置、附加要求与完整术语快照。"""
        wanted = set(target_ids)
        digests = sorted(
            (block.block_id, block_source_digest(block))
            for block in document.blocks if block.block_id in wanted
        )
        source_digest = hashlib.sha256(
            "\n".join(f"{block_id}:{digest}" for block_id, digest in digests).encode("utf-8")
        ).hexdigest()[:32]
        glossary = dict(layer.glossary_snapshot or {})
        glossary_digest = hashlib.sha256(
            "\n".join(f"{key}\t{value}" for key, value in sorted(glossary.items())).encode("utf-8")
        ).hexdigest()[:32]
        config = self.provider.config
        return {
            "frozen_at": utc_now(),
            "locale": locale,
            "refine": bool(refine),
            "force": bool(force),
            "polish": bool(polish),
            "target_count": len(target_ids),
            "source_digest": source_digest,
            "prompt_version": PROMPT_VERSION,
            # 服务身份：恢复时据此确认密钥与地址仍属同一服务，不把当前密钥发往旧地址。
            "service_id": str(self.service_id or ""),
            # 完整术语快照 + 摘要：恢复时按快照重新组装 prompt，不因用户后来改术语表而变。
            "glossary": glossary,
            "glossary_digest": glossary_digest,
            "glossary_size": len(glossary),
            "instructions": instructions,
            # 非敏感模型参数：恢复时按冻结值执行（密钥仍运行时从当前服务读取，不落快照）。
            "model": {
                "provider": config.provider,
                "model": config.model,
                "base_url": config.base_url,
                "temperature": config.temperature,
                "max_output_tokens": config.max_output_tokens,
            },
        }

    def _save_checkpoint(self, project_id: str, job_id: str, value: dict[str, Any]) -> None:
        # 合并而不是整体覆盖 result：终态 counts 与以后新增的字段不能被 checkpoint 刷掉；
        # checkpoint 自己的键（目标/阶段/完成清单）以本次传入的 value 为准。
        try:
            existing = self.store.get_job(project_id, job_id).get("result") or {}
        except StorageError:
            existing = {}
        self.store.update_job(project_id, job_id, result={**existing, **value, "checkpoint_version": 1})

    @staticmethod
    def _done_ids(layer: TranslationLayer, ids: list[str], *, refine: bool) -> list[str]:
        done: list[str] = []
        for block_id in ids:
            entry = layer.entries.get(block_id)
            if entry is None or entry.partial:
                continue
            if refine:
                if entry.status == "refined":
                    done.append(block_id)
            elif entry.status in {"translated", "refined"}:
                done.append(block_id)
        return done

    @staticmethod
    def _task_done_ids(layer: TranslationLayer, ids: list[str], *, refine: bool, run_token: str) -> list[str]:
        """本次任务真正处理过且仍然有效的块。

        磁盘上的旧译文即使 status/source_hash 都“有效”，也不代表它由本次 job 产生；
        只有带本次 ``run_token`` 的结果才能算“本次已完成”。这是 force 重译、重新润色
        在崩溃恢复时不漏块的关键证据。
        """
        if not run_token:
            return []
        done: list[str] = []
        for block_id in ids:
            entry = layer.entries.get(block_id)
            if entry is None or entry.partial or entry.run_token != run_token:
                continue
            if refine:
                if entry.status == "refined":
                    done.append(block_id)
            elif entry.status in {"translated", "refined"}:
                done.append(block_id)
        return done

    @staticmethod
    def _checkpoint_subset(checkpoint: dict[str, Any] | None, key: str, ids: list[str]) -> set[str]:
        """checkpoint 中属于本次目标范围的完成清单（由本任务自己写入，不会含旧译文）。"""
        if not checkpoint:
            return set()
        wanted = {str(value) for value in ids}
        return {str(value) for value in (checkpoint.get(key) or []) if str(value) in wanted}

    @staticmethod
    def _blocks_for_ids(document: Any, layer: TranslationLayer, ids: list[str]) -> list[Block]:
        wanted = set(ids)
        result: list[Block] = []
        for block in document.blocks:
            if block.block_id not in wanted or not is_translatable(block):
                continue
            entry = layer.entries.get(block.block_id)
            if entry is not None and entry.locked:
                continue
            result.append(block)
        return result

    # ---- 并发安全提交（F01） ------------------------------------------- #

    @staticmethod
    def _empty_summary() -> dict[str, Any]:
        return {
            "stop_reason": None, "first_error": None, "success_blocks": 0,
            "failed_block_ids": set(), "failed_reasons": {}, "total_blocks": 0, "completed_chunks": 0,
            "total_chunks": 0, "unhandled": [],
        }

    @staticmethod
    def _entry_signature(entry: TranslationEntry | None) -> tuple[Any, ...]:
        if entry is None:
            return ()
        return (
            entry.text, tuple(entry.items), tuple(entry.caption), tuple(entry.footnote),
            tuple(sorted(entry.cells.items())), entry.status, bool(entry.locked),
            entry.source_hash, int(entry.structure_revision or 0),
            len(entry.selection_translations), len(entry.revisions),
        )

    def _align_structure_only_stale(
        self, project_id: str, locale: str, document: Any, layer: TranslationLayer,
        baseline: dict[str, tuple[Any, ...]], baseline_glossary: dict[str, str], started_at: str,
    ) -> None:
        """把仅结构变化的译文对齐到当前结构版本，不发送模型请求。"""
        touched = align_structure_only(document, layer)
        if touched:
            self._commit_translation(project_id, locale, layer, set(touched), baseline, baseline_glossary, started_at)

    def _commit_translation(
        self, project_id: str, locale: str, layer: TranslationLayer, touched: set[str],
        baseline: dict[str, tuple[Any, ...]], baseline_glossary: dict[str, str], started_at: str,
    ) -> None:
        """按项目+语言串行提交，只合并本次触碰的条目，并保留运行期间的人工内容。"""
        if not touched:
            return
        lock = self.store.translation_lock(project_id, locale)
        with lock:
            disk = self.store.load_translation(project_id, locale)
            # 提交时复查 canonical 源：作业处理期间源文可能已被改（无需长期锁住 HTTP 期间的文档）。
            canonical = self._canonical_digests(project_id)
            for block_id in touched:
                memo = layer.entries.get(block_id)
                current = disk.entries.get(block_id)
                info = canonical.get(block_id)
                if memo is not None and info is not None and memo.source_hash != info[0]:
                    # 当前源已变：AI 结果只存候选，绝不把旧源的结果写成当前完成。
                    candidate = {**_entry_snapshot(memo), "kind": "ai_candidate", "created_at": utc_now()}
                    if current is not None:
                        current.candidates = [*current.candidates, candidate]
                        disk.entries[block_id] = current
                    else:
                        disk.entries[block_id] = TranslationEntry(
                            block_id=block_id, status="failed", source_hash=memo.source_hash,
                            candidates=[candidate], partial=True, structure_revision=info[1],
                            error="原文内容在任务期间已变化，本次结果只保存为候选，需复核",
                        )
                    continue
                merged = self._merge_entry(current, memo, baseline.get(block_id), started_at)
                if merged is None:
                    disk.entries.pop(block_id, None)
                else:
                    disk.entries[block_id] = merged
            # 术语表：只有磁盘仍是本作业启动时的快照时才写回，避免覆盖运行期间的人工冻结。
            if (
                layer.glossary_snapshot
                and layer.glossary_snapshot != baseline_glossary
                and disk.glossary_snapshot == baseline_glossary
            ):
                disk.glossary_snapshot = dict(layer.glossary_snapshot)
            self.store.save_translation(disk)
            for block_id in touched:
                if block_id in disk.entries:
                    layer.entries[block_id] = disk.entries[block_id]
                else:
                    layer.entries.pop(block_id, None)
            layer.glossary_snapshot = disk.glossary_snapshot

    @staticmethod
    def _merge_entry(
        current: TranslationEntry | None, memo: TranslationEntry | None,
        base_signature: tuple[Any, ...] | None, started_at: str,
    ) -> TranslationEntry | None:
        if current is None:
            return memo
        if memo is None:
            return current
        # 只看“内容签名是否与作业基线不同”：不再叠加 updated_at 时间窗，避免
        # 微秒级竞态和旧文件缺少 updated_at 时被 default_factory 当成刚编辑。
        manual_now = current.locked or current.status == "edited"
        if manual_now and TranslationService._entry_signature(current) != base_signature:
            # 运行期间用户保存了人工译文：保留人工内容，把模型结果作为候选（绝不覆盖）。
            preserved = TranslationEntry(**asdict(current))
            if _entry_has_content(memo):
                candidate = _entry_snapshot(memo)
                candidate["kind"] = "ai_candidate"
                candidate["created_at"] = utc_now()
                preserved.candidates = [*preserved.candidates, candidate]
            return preserved
        merged = TranslationEntry(**asdict(memo))
        # 保留运行期间追加的即时翻译记录与版本历史，不让旧快照覆盖。
        if len(current.selection_translations) > len(merged.selection_translations):
            merged.selection_translations = list(current.selection_translations)
        if len(current.revisions) > len(merged.revisions):
            merged.revisions = list(current.revisions)
        if len(current.candidates) > len(merged.candidates):
            merged.candidates = list(current.candidates)
        return merged

    # ---- 并发调度 ------------------------------------------------------- #

    def _worker_count(self, value: int | None) -> int:
        if value is None:
            value = DEFAULT_PARALLEL_WORKERS
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = DEFAULT_PARALLEL_WORKERS
        return max(1, min(MAX_PARALLEL_WORKERS, number))

    def _budget_exceeded(self, project_id: str, reserved: float = 0.0) -> bool:
        budget = float(self.store.load_manifest(project_id).get("budget_usd") or 0)
        if budget <= 0:
            return False
        spent = float(self.store.usage_summary(project_id)["estimated_cost"] or 0)
        # 预留在途请求的估算费用：并发下不能只看“已经花掉”，否则会超出预算。
        return spent + reserved >= budget

    def _estimate_chunk_cost(self, units: list[tuple[Block, list[tuple[str, str]]]]) -> float:
        """在途请求的保守费用估算；与单次请求估算共用同一公式（价格未知时返回 0）。"""
        chars = sum(len(text) for _, segments in units for _, text in segments)
        return prompt_cost_estimator(self.provider.config)(chars)

    def _process_blocks(
        self, project_id: str, job_id: str, document: Any, layer: TranslationLayer, blocks: list[Block], *,
        refine: bool, instructions: str, locale: str, block_languages: dict[str, str],
        workers: int, should_pause: Callable[[], bool] | None,
        progress_base: float, progress_span: float,
        started_at: str, baseline: dict[str, tuple[Any, ...]], baseline_glossary: dict[str, str],
        reuse_previous: bool = True, run_token: str = "",
    ) -> dict[str, Any]:
        # 当前阶段（初译/润色）：用量 scope 与中间子段证据都要按 pass 区分。
        self._active_refine = bool(refine)
        self._active_scope = "refine" if refine else "translate"
        chunks = _chunks(blocks, languages=block_languages, language=document.source_language)
        total_chunks = len(chunks)
        positions = {block.block_id: index for index, block in enumerate(document.blocks)}
        context_map = {index: _chunk_context(document, positions, chunk, languages=block_languages) for index, chunk in enumerate(chunks)}
        # 单元快照在主线程完成：工作线程只做 HTTP + 解析，绝不触碰共享的 layer。
        units_map = {index: self._units_for(chunk, layer, refine=refine) for index, chunk in enumerate(chunks)}
        glossary = layer.glossary_snapshot or {}
        self.store.update_job(project_id, job_id, status="running", message=f"共 {len(blocks)} 段 / {total_chunks} 个分块，{workers} 路并发。")

        completed = 0
        success_blocks = 0
        failed_block_ids: set[str] = set()
        failed_reasons: dict[str, str] = {}
        first_error: Exception | None = None
        stop_reason: str | None = None
        provider_error_streak = 0
        handled: set[str] = set()
        previous_translation = ""
        previous_chunk_language: str | None = None
        submit_index = 0
        # AIMD 并发自适应：effective_workers 只会在 [1, workers] 内波动。
        # 429 → 减半（退避由 provider 内部重试 + 协调层冷却叠加完成）；
        # 连续成功 → +1 探测回升。用户设定的 workers 是上限，不额外放大。
        effective_workers = workers
        aimd_success_streak = 0

        # 小块并发会产生很多分块；每块都整层重写 translation.json 会变成 O(n²)。
        # 这里按块数/时间节流，崩溃时最多损失几块进度，但大幅减少磁盘写入。
        dirty = 0
        last_save = time.monotonic()
        pending_touched: set[str] = set()

        def commit(force: bool = False) -> None:
            nonlocal dirty, last_save
            dirty += 1
            if force or dirty >= 5 or time.monotonic() - last_save > 3.0:
                if pending_touched:
                    self._commit_translation(
                        project_id, locale, layer, set(pending_touched), baseline, baseline_glossary, started_at,
                    )
                    pending_touched.clear()
                dirty = 0
                last_save = time.monotonic()

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="literature-translate") as pool:
            running: dict[Future, tuple[int, float]] = {}
            reserved = 0.0
            while submit_index < total_chunks or running:
                while submit_index < total_chunks and len(running) < effective_workers and stop_reason is None:
                    if should_pause and should_pause():
                        stop_reason = "paused"
                        break
                    if self._budget_exceeded(project_id, reserved):
                        stop_reason = "budget_paused"
                        break
                    if provider_error_streak >= GLOBAL_FAILURE_LIMIT:
                        stop_reason = "global_failure"
                        break
                    chunk = chunks[submit_index]
                    context_before, context_after = context_map[submit_index]
                    # 串行衔接也遵守章节/语言边界：新章或换语言时清空上一块译文。
                    if workers == 1:
                        chunk_language = block_languages.get(chunk[0].block_id)
                        if chunk[0].type == "title" or chunk_language != previous_chunk_language:
                            previous_translation = ""
                        previous_chunk_language = chunk_language
                    chunk_cost = self._estimate_chunk_cost(units_map[submit_index])
                    reserved += chunk_cost
                    running[pool.submit(
                        self._run_chunk, units_map[submit_index], chunk, refine=refine, instructions=instructions, locale=locale,
                        block_languages=block_languages, glossary=glossary,
                        context_before=context_before, context_after=context_after,
                        previous_translation=previous_translation,
                        partial_map=self._partial_snapshot(),
                    )] = (submit_index, chunk_cost)
                    submit_index += 1
                if not running:
                    break
                done, _ = wait(list(running.keys()), return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: running[item][0]):
                    chunk_index, chunk_cost = running.pop(future)
                    reserved = max(0.0, reserved - chunk_cost)
                    chunk = chunks[chunk_index]
                    try:
                        outcome = future.result()
                    except BudgetExhausted as exhausted:
                        # 预算不足：中止整个作业进入 budget_paused；绝不能标记为块失败而清空旧译文。
                        stop_reason = "budget_paused"
                        first_error = exhausted
                        handled.update(block.block_id for block in chunk)
                        continue
                    except Exception as error:  # noqa: BLE001 - 任何未预期错误都降级为该块失败，不拖垮整本书
                        outcome = _ChunkOutcome({}, {block.block_id: str(error) for block in chunk}, [], error)
                    if outcome.budget_paused:
                        # 已完整解析的块照常提交；未完成的子段作为本任务的中间证据落盘（不是译文完成），
                        # 恢复时只补剩余子段，已付费的子段不重发。
                        if outcome.segments:
                            self._apply_outcome(layer, chunk, outcome, refine=refine, model_run_id=outcome.model_run_id, reuse_previous=reuse_previous, run_token=run_token)
                            pending_touched.update(block.block_id for block in chunk if block.block_id in outcome.segments)
                            self._forget_partials([block.block_id for block in chunk])
                            commit(force=True)
                        self._persist_partial_segments(project_id, job_id, outcome, chunk)
                        stop_reason = "budget_paused"
                        first_error = first_error or BudgetExhausted("项目预算不足，已停止提交新的模型请求")
                        handled.update(block.block_id for block in chunk)
                        continue
                    # 用量已在 _complete 中随每次 HTTP 实时写入（含子批/重试），此处不再重复记账。
                    self._apply_outcome(layer, chunk, outcome, refine=refine, model_run_id=outcome.model_run_id, reuse_previous=reuse_previous, run_token=run_token)
                    # 完成的块要清除对应 phase 的中间结果，避免后续阶段错误复用。
                    self._forget_partials([block.block_id for block in chunk if block.block_id in outcome.segments])
                    pending_touched.update(block.block_id for block in chunk)
                    commit()
                    handled.update(block.block_id for block in chunk)
                    success_blocks += sum(1 for block in chunk if block.block_id in outcome.segments)
                    failed_block_ids.update(outcome.failed.keys())
                    failed_reasons.update(outcome.failed)
                    if outcome.rate_limited:
                        # 429 是临时限流：无论该块是否同时完全失败，都只做 AIMD 减半，
                        # 不计入全局熔断——服务很快恢复时不能把剩余整本书误判为失败。
                        # 退避由 provider 内部重试与协调层冷却叠加完成。
                        effective_workers = max(1, effective_workers // 2)
                        aimd_success_streak = 0
                        provider_error_streak = 0
                        if outcome.fully_failed and first_error is None:
                            first_error = outcome.error or ProviderError("模型请求被限流（429），该分块待重试。")
                    elif outcome.fully_failed:
                        if first_error is None:
                            first_error = outcome.error or ProviderError("模型输出缺少该分块的译文。")
                        if isinstance(outcome.error, ProviderError) or outcome.error is None:
                            provider_error_streak += 1
                        aimd_success_streak = 0
                    else:
                        provider_error_streak = 0
                        # AIMD：连续成功后 +1 探测回升（不超过用户设定的上限）。
                        if effective_workers < workers:
                            aimd_success_streak += 1
                            if aimd_success_streak >= AIMD_PROBE_SUCCESS_STREAK:
                                effective_workers += 1
                                aimd_success_streak = 0
                        else:
                            aimd_success_streak = 0
                    # 顺序模式下把上一块末段译文带进下一块，保证跨块语气衔接。
                    if workers == 1 and outcome.segments:
                        last_block = chunk[-1]
                        last_seg = outcome.segments.get(last_block.block_id, {})
                        tail = next(reversed(list(last_seg.values())), "") if last_seg else ""
                        if tail:
                            previous_translation = tail[-PREVIOUS_TRANSLATION_CHARS:]
                    completed += 1
                    self.store.update_job(
                        project_id, job_id,
                        progress=min(1.0, progress_base + progress_span * completed / max(1, total_chunks)),
                    )

        if stop_reason == "global_failure":
            reason = "已中止：模型服务连续失败"
            for chunk in chunks[submit_index:]:
                self._mark_failed(layer, chunk, reason)
                failed_block_ids.update(block.block_id for block in chunk)
                failed_reasons.update({block.block_id: reason for block in chunk})
                handled.update(block.block_id for block in chunk)
                pending_touched.update(block.block_id for block in chunk)
            commit(force=True)
        elif pending_touched:
            commit(force=True)

        return {
            "stop_reason": stop_reason,
            "first_error": first_error,
            "success_blocks": success_blocks,
            "failed_block_ids": failed_block_ids,
            "failed_reasons": failed_reasons,
            "total_blocks": len(blocks),
            "completed_chunks": completed,
            "total_chunks": total_chunks,
            "unhandled": [block.block_id for block in blocks if block.block_id not in handled],
        }

    def _finalize_job(
        self, project_id: str, job_id: str, layer: TranslationLayer, summary: dict[str, Any], *,
        target_ids: list[str], translate_completed: list[str],
        polish_target_ids: list[str], polish_completed: list[str],
        translate_failed: dict[str, str], polish_failed: dict[str, str],
        source_changed_ids: set[str], polish_stage: bool = False,
    ) -> dict[str, Any]:
        stop_reason = summary["stop_reason"]
        counts, failure_reasons = self._task_accounting(
            layer, target_ids=target_ids, translate_completed=translate_completed,
            polish_target_ids=polish_target_ids, polish_completed=polish_completed,
            translate_failed=translate_failed, polish_failed=polish_failed,
            source_changed_ids=source_changed_ids,
        )
        failed_count = len(failure_reasons)
        result = self._merged_result(project_id, job_id, counts)
        if stop_reason == "paused":
            stage = "润色" if polish_stage else "翻译"
            self.store.update_job(project_id, job_id, status="paused", message=f"{stage}已暂停；当前分块已完成，已完成部分已保留，可继续。", result=result)
        elif stop_reason == "budget_paused":
            self.store.update_job(project_id, job_id, status="budget_paused", message="项目预算已用尽。", result=result)
        elif summary["success_blocks"] == 0 and summary["total_blocks"]:
            # 整批没有任何成功分块：无论是否触发了连续失败熔断，都算整体失败。
            # 反过来，熔断时若已有成功分块，则落到下面的 partial_failed，保留已完成结果，
            # 不再因为“后面持续失败”就把前面成功的整本译文标记为 failed。
            error = summary["first_error"] or ProviderError("所有分块翻译失败；请检查模型服务后重试。")
            self.store.update_job(project_id, job_id, status="failed", error=str(error), message="翻译失败。", result=result)
            raise error if isinstance(error, Exception) else ProviderError(str(error))
        elif failed_count:
            # 有失败段落时不再冒充“完成”：部分失败用独立状态，并把失败原因写进错误栏，
            # 让用户知道为什么失败、能不能重试，而不是看到一个空错误栏的 completed。
            detail = _summarize_failures(failure_reasons)
            error = f"{failed_count} 段失败：{detail}" if detail else f"{failed_count} 段失败，可重试。"
            self.store.update_job(
                project_id, job_id, status="partial_failed", progress=1,
                error=error, message=f"翻译完成，{failed_count} 段失败可重试。", result=result,
            )
        else:
            self.store.update_job(project_id, job_id, status="completed", progress=1, error="", message="翻译完成。", result=result)
        return self.store.get_job(project_id, job_id)

    @staticmethod
    def _source_changed_ids(document: Any, layer: TranslationLayer, target_ids: list[str]) -> set[str]:
        """目标里“依据的源文与当前 canonical 源不一致”的块：含未完成但因源变只存了候选的块。"""
        block_map = {block.block_id: block for block in document.blocks}
        changed: set[str] = set()
        for bid in dict.fromkeys(str(value) for value in target_ids):
            entry = layer.entries.get(bid)
            block = block_map.get(bid)
            if entry is not None and entry.source_hash and block is not None:
                if entry.source_hash != block_source_digest(block):
                    changed.add(bid)
        return changed

    @staticmethod
    def _task_accounting(
        layer: TranslationLayer, *,
        target_ids: list[str], translate_completed: list[str],
        polish_target_ids: list[str], polish_completed: list[str],
        translate_failed: dict[str, str], polish_failed: dict[str, str],
        source_changed_ids: set[str],
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """以 checkpoint 的原始 target_ids 定义任务目标，按唯一 block 统计，初译/润色阶段分开。

        口径（避免同一 block 重复计数）：
        - `target_blocks` 是任务目标的唯一 block 数（去重）；不是本次执行剩余范围，恢复后不会漏已完成段。
        - `translate.completed/failed/locked/unhandled` 四类互斥且恰好划分 target（和恒等于 target）；
          优先级 completed > locked > failed > unhandled：
          * completed 是任务已成功完成的目标，且依据的源 hash 与当前源一致（源变旧译不算完成）；
          * locked 只统计“尚未完成但因人工锁定而被跳过”的目标，已完成的锁定块仍归 completed；
          * failed 只统计既未完成也未被锁定分类的目标；剩余为 unhandled。
          `translate.source_changed` 是叠加标记（原文内容在任务期间变化的块），它与 failed 重叠但不重复计入划分。
        - `polish.target` 是允许润色的唯一 block 子集；`polish.completed` 以 status=refined 计；
          `polish.failed` 只含仍未润色成功的块。两个阶段各自计数，不再相加同一 block。
        - `failed` 取“当前仍失败”的块：本轮新失败并集去掉后来重试成功的块；已保存但未通过的条目也会以真实原因归入
          failed，而不是静默当作 unhandled。
        """
        order = list(dict.fromkeys(str(value) for value in target_ids))
        target = set(order)
        changed = {bid for bid in source_changed_ids if bid in target}
        completed = {bid for bid in translate_completed if bid in target} - changed
        locked: set[str] = set()
        for bid in target:
            entry = layer.entries.get(bid)
            # 已完成优先归 completed；locked 只表示“因人工锁定而跳过、尚未完成”的目标。
            if entry is not None and entry.locked and bid not in completed:
                locked.add(bid)
        failed = {bid: reason for bid, reason in translate_failed.items() if bid in target}
        failed = {bid: reason for bid, reason in failed.items() if bid not in completed and bid not in locked}
        # 源变但未完成的块必须归因到 failed（需复核），不能静默当作完成或未处理。
        for bid in changed:
            if bid not in completed and bid not in locked:
                failed[bid] = "原文内容在任务期间已变化，本次结果只保存为候选，需复核"
        # 已保存但未通过完整校验的条目（含提交守卫降级的失败）也归 failed，不当作未处理；
        # 但“旧任务留下的已校验译文”只是本次未处理，归 unhandled，不能报失败。
        for bid in target:
            if bid in completed or bid in locked or bid in failed:
                continue
            entry = layer.entries.get(bid)
            if entry is not None and (entry.partial or entry.status not in {"translated", "refined"}):
                failed[bid] = entry.error or "模型输出缺少该段译文"
        unhandled = target - completed - set(failed) - locked

        polish_set = {bid for bid in dict.fromkeys(str(value) for value in polish_target_ids) if bid in target}
        polish_done = {bid for bid in polish_completed if bid in polish_set}
        polish_fail = {
            bid: reason for bid, reason in polish_failed.items()
            if bid in polish_set and bid not in polish_done
        }
        counts: dict[str, Any] = {
            "target_blocks": len(order),
            "translate": {
                "target": len(order),
                "completed": len(completed),
                "failed": len(failed),
                "locked": len(locked),
                "unhandled": len(unhandled),
                "source_changed": len(changed),
            },
            "polish": {
                "target": len(polish_set),
                "completed": len(polish_done),
                "failed": len(polish_fail),
            },
        }
        return counts, {**failed, **polish_fail}

    def _merged_result(self, project_id: str, job_id: str, counts: dict[str, Any]) -> dict[str, Any]:
        """把任务计数并进已有 result，而不是覆盖 checkpoint（否则恢复契约会失效）。"""
        try:
            existing = self.store.get_job(project_id, job_id).get("result") or {}
        except StorageError:
            existing = {}
        return {**existing, "counts": counts}

    # ---- 单个分块（含失败切分重试） ------------------------------------- #

    def _run_chunk(
        self, units: list[tuple[Block, list[tuple[str, str]]]], chunk: list[Block], *, refine: bool, instructions: str, locale: str,
        block_languages: dict[str, str], glossary: dict[str, str],
        context_before: str, context_after: str, previous_translation: str,
        partial_map: dict[str, dict[str, str]] | None = None,
    ) -> _ChunkOutcome:
        # 术语表整份稳定注入 system prompt（prefix caching 友好，见 _system_prompt）；
        # 不再按块过滤：前缀逐字节一致才能命中缓存，且代词指代处也能拿到正确译法。
        languages = {block_languages.get(block.block_id) for block in chunk}
        source_language = next(iter(languages)) if len(languages) == 1 else "原文（可能混合多种语言）"
        usages: list[ProviderResponse] = []
        partial_map = partial_map or {}
        # 已经付费并落盘的子段不再重发：先种入 resolved，只发缺失子段。
        # 中间证据必须同源、同阶段才可用：源文变或阶段不符一律丢弃，不能混用旧新源文。
        resolved: dict[str, dict[str, str]] = {}
        for block, _segments in units:
            record = partial_map.get(block.block_id)
            if not isinstance(record, dict):
                continue
            if bool(record.get("refine")) != bool(refine):
                continue
            if record.get("source_hash") != block_source_digest(block):
                continue
            segments = record.get("segments")
            if isinstance(segments, dict) and segments:
                resolved[block.block_id] = dict(segments)
        send_units: list[tuple[Block, list[tuple[str, str]]]] = []
        for block, segments in units:
            known = resolved.get(block.block_id, {})
            remaining = [(key, text) for key, text in segments if _seg_id(block.block_id, key) not in known]
            if remaining:
                send_units.append((block, remaining))
        # 请求级预拆：超过 token/字段预算时拆成多个 HTTP，而不是把整块压在首个请求里。
        reasons: dict[str, str] = {}
        run_id = ""
        budget_paused = False
        rate_limited = False
        chunk_error: ProviderError | None = None
        # 切分子批里的异常被 _translate_units_isolated 吞掉转成 reasons；
        # 429 状态码通过 error_sink 上浮，否则子批限流不会触发 AIMD 减半。
        error_sink: list[ProviderError] = []
        for batch in _batch_units(send_units, language=source_language or "en"):
            try:
                batch_resolved, batch_reasons, batch_run_id = self._translate_units(
                    batch, refine=refine, instructions=instructions, locale=locale, source_language=source_language or "",
                    glossary=glossary, context_before=context_before, context_after=context_after,
                    previous_translation=previous_translation, depth=0, usages=usages,
                    error_sink=error_sink,
                )
            except BudgetExhausted:
                # 预算不足：已解析的子段随 outcome 返回并落为中间证据，不能让它们被重发。
                budget_paused = True
                break
            except ProviderTimeoutError as error:
                # 超时（流式下即“长时间无输出”）：与协议失败一样沿 segment 中点切分重试。
                # 小批生成更快，超时概率骤降；单 segment 仍超时才真正失败。
                batch_resolved, batch_reasons = self._retry_batch_after_timeout(
                    batch, error, refine=refine, instructions=instructions, locale=locale,
                    source_language=source_language or "", glossary=glossary,
                    context_before=context_before, context_after=context_after,
                    previous_translation=previous_translation, usages=usages,
                    error_sink=error_sink,
                )
                batch_run_id = ""
            except ProviderError as error:
                # 单个子请求失败要隔离，不能作废同一次作业里其他子请求已解析的字段。
                response = getattr(error, "response", None)
                if isinstance(response, ProviderResponse):
                    usages.append(response)
                batch_resolved = {}
                batch_reasons = {block.block_id: str(error) for block, _ in batch}
                batch_run_id = ""
                rate_limited = rate_limited or getattr(error, "status_code", 0) == 429
                chunk_error = chunk_error or error
            # 子批内（切分/超时重试）捕获的 429 也算：AIMD 据此减半并发。
            if any(getattr(error, "status_code", 0) == 429 for error in error_sink):
                rate_limited = True
            if chunk_error is None and error_sink:
                chunk_error = error_sink[0]
            resolved = _merge_segment_maps(resolved, batch_resolved)
            reasons.update(batch_reasons)
            run_id = run_id or batch_run_id
            # 每个协议校验通过（未被截断/缺段）的子响应立即落盘为中间证据。
            for block, _segs in batch:
                if block.block_id in batch_reasons:
                    continue
                segments = batch_resolved.get(block.block_id)
                if segments:
                    self._record_validated_segments(block.block_id, dict(segments), block_source_digest(block))
        # 整块完整性检查：任一预期字段（含子段）缺失/为空就不算完成（F02/F03）。
        segments: dict[str, dict[str, str]] = {}
        failed: dict[str, str] = {}
        for block, segs in units:
            expected = {_seg_id(block.block_id, key) for key, _ in segs}
            got = resolved.get(block.block_id, {})
            if expected and expected <= set(got) and block.block_id not in reasons:
                segments[block.block_id] = {segment_id: got[segment_id] for segment_id in expected}
            else:
                failed[block.block_id] = reasons.get(block.block_id, "模型输出缺少该段译文")
        return _ChunkOutcome(segments, failed, usages, chunk_error, run_id, resolved=resolved, budget_paused=budget_paused, rate_limited=rate_limited)

    def _retry_batch_after_timeout(
        self, batch: list[tuple[Block, list[tuple[str, str]]]], error: ProviderTimeoutError,
        *, refine: bool, instructions: str, locale: str, source_language: str, glossary: dict[str, str],
        context_before: str, context_after: str, previous_translation: str,
        usages: list[ProviderResponse], depth: int = 0, error_sink: list[ProviderError] | None = None,
    ) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
        """超时后的降级重试：把批次沿 segment 中点切半，两半分别隔离重试。

        单 segment 无法再切、或递归超过 MAX_SPLIT_DEPTH 时才把该批判为失败。
        每次切分后的生成时间大约减半，超时概率随之骤降；子批内协议层面的
        进一步切分仍由 _translate_units 自己处理。
        """
        flat = sum(len(segments) for _, segments in batch)
        if flat < 2 or depth >= MAX_SPLIT_DEPTH:
            return {}, {block.block_id: f"请求超时：{error}" for block, _ in batch}
        left, right = _split_units(batch)
        if not left or not right:
            return {}, {block.block_id: f"请求超时：{error}" for block, _ in batch}
        kwargs = dict(
            refine=refine, instructions=instructions, locale=locale, source_language=source_language,
            glossary=glossary, context_before=context_before, context_after=context_after,
            previous_translation=previous_translation, usages=usages,
        )
        sink: list[ProviderError] = []
        resolved: dict[str, dict[str, str]] = {}
        reasons: dict[str, str] = {}
        for half in (left, right):
            try:
                half_resolved, half_reasons, _run_id = self._translate_units(half, depth=0, error_sink=sink, **kwargs)
            except ProviderTimeoutError as sub_error:
                half_resolved, half_reasons = self._retry_batch_after_timeout(half, sub_error, depth=depth + 1, error_sink=sink, **kwargs)
            except ProviderError as sub_error:
                response = getattr(sub_error, "response", None)
                if isinstance(response, ProviderResponse):
                    usages.append(response)
                half_resolved, half_reasons = {}, {block.block_id: str(sub_error) for block, _ in half}
                sink.append(sub_error)
            resolved = _merge_segment_maps(resolved, half_resolved)
            reasons.update(half_reasons)
        if error_sink is not None:
            error_sink.extend(sink)
        return resolved, reasons

    def _units_for(self, blocks: list[Block], layer: TranslationLayer, *, refine: bool) -> list[tuple[Block, list[tuple[str, str]]]]:
        units: list[tuple[Block, list[tuple[str, str]]]] = []
        for block in blocks:
            entry = layer.entries.get(block.block_id)
            if refine and entry is not None:
                segments = _entry_segments(block, entry)
            else:
                # 失败/待重译的块一律全字段重发：未校验的局部片段（partial）与源文已变的
                # 旧内容都不能作为“只补缺字段”的依据，否则会把过期译文伪装成当前结果。
                # 超长单字段在这里预先拆成可缩批的子段（最终仍拼回同一稳定字段）。
                segments = _unit_segments(block)
            if segments:
                units.append((block, segments))
        return units

    def _translate_units(
        self, units: list[tuple[Block, list[tuple[str, str]]]], *, refine: bool, instructions: str, locale: str,
        source_language: str, glossary: dict[str, str], context_before: str, context_after: str,
        previous_translation: str, depth: int, usages: list[ProviderResponse],
        error_sink: list[ProviderError] | None = None,
    ) -> tuple[dict[str, dict[str, str]], dict[str, str], str]:
        """返回 (已解析的字段级结果, 仍未完整的块与原因, run_id)。

        完整性判定交给调用方按“整块预期字段”做，不允许半个切分认为整块完成（F02）。
        """
        prompt = _user_prompt(
            units, context_before=context_before, context_after=context_after,
            previous_translation=previous_translation,
        )
        response = self._complete(
            _system_prompt(source_language, locale, refine=refine, instructions=instructions, glossary=glossary), prompt,
        )
        usages.append(response)
        parsed = _parse_segments(response.content)
        counts = _parse_segment_counts(response.content)
        truncated = str(getattr(response, "finish_reason", "") or "") in _TRUNCATED_FINISH_REASONS
        reasoning_squeezed = _reasoning_squeezed(response)
        resolved: dict[str, dict[str, str]] = {}
        for block, segments in units:
            block_resolved: dict[str, str] = {}
            for key, _ in segments:
                segment_id = _seg_id(block.block_id, key)
                # 重复 id 说明模型把同一字段吐了多次，内容有歧义，不能算完成（F03）。
                if counts.get(segment_id, 0) != 1:
                    continue
                value = _clean_translated_text(parsed.get(segment_id, ""))
                if value:
                    block_resolved[segment_id] = value
            if block_resolved:
                resolved[block.block_id] = block_resolved
        incomplete: list[tuple[Block, list[tuple[str, str]]]] = []
        for block, segments in units:
            expected = {_seg_id(block.block_id, key) for key, _ in segments}
            # 输出被长度截断时，即使标签刚好闭合也不能当作完整结果（F03）。
            if not truncated and expected <= set(resolved.get(block.block_id, {})):
                continue
            incomplete.append((block, segments))
        if not incomplete:
            return resolved, {}, response.request_id
        # 整份响应一个 <seg> 都没有，且不是被截断：说明模型没遵守协议，切分也无用，
        # 直接判该批失败，避免为同一个坏模型发出指数级请求。
        if not parsed and not truncated:
            return resolved, {block.block_id: "模型没有按 <seg id=\"...\"> 格式返回译文" for block, _ in incomplete}, response.request_id
        if depth < MAX_SPLIT_DEPTH:
            left, right = _split_units(incomplete)
            if left and right:
                # 子请求异常必须隔离：一侧超时/报错不能作废父请求和兄弟批里已解析出的字段。
                # 被吞掉的 ProviderError 通过 error_sink 上浮（429 触发 AIMD，见 _run_chunk）。
                sink: list[ProviderError] = []
                left_resolved, left_reasons = self._translate_units_isolated(
                    left, refine=refine, instructions=instructions, locale=locale, source_language=source_language,
                    glossary=glossary, context_before=context_before, context_after=context_after,
                    previous_translation=previous_translation, depth=depth + 1, usages=usages,
                    error_sink=sink,
                )
                right_resolved, right_reasons = self._translate_units_isolated(
                    right, refine=refine, instructions=instructions, locale=locale, source_language=source_language,
                    glossary=glossary, context_before=context_before, context_after=context_after,
                    previous_translation=previous_translation, depth=depth + 1, usages=usages,
                    error_sink=sink,
                )
                if error_sink is not None:
                    error_sink.extend(sink)
                # 按 segment id 深合并，不能让后到的一半整块覆盖前一半已成功的字段（F02）。
                return (
                    _merge_segment_maps(resolved, left_resolved, right_resolved),
                    {**left_reasons, **right_reasons},
                    response.request_id,
                )
        if truncated:
            reason = (
                "模型把输出预算用在了思考过程（reasoning）上，译文被截断；"
                "请换用非思考模型、降低思考强度，或减小分块后重试"
                if reasoning_squeezed else "模型输出被截断，切分后仍不完整"
            )
        else:
            reason = "模型输出缺少该段译文"
        return resolved, {block.block_id: reason for block, _ in incomplete}, response.request_id

    # ---- 落库 ----------------------------------------------------------- #

    def _translate_units_isolated(
        self, units: list[tuple[Block, list[tuple[str, str]]]], **kwargs: Any,
    ) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
        """子批翻译的异常隔离：把 ProviderError 转成该子批的失败原因，不拖垮兄弟批。

        错误响应若已带 usage（例如只返回思考内容），把它登记进 usages，避免漏记账；
        错误本身追加进 ``error_sink``（若调用方提供），供调度层识别 429 限流。
        """
        error_sink = kwargs.get("error_sink")
        try:
            resolved, reasons, _run_id = self._translate_units(units, **kwargs)
            return resolved, reasons
        except ProviderError as error:
            response = getattr(error, "response", None)
            usages = kwargs.get("usages")
            if isinstance(response, ProviderResponse) and isinstance(usages, list):
                usages.append(response)
            if isinstance(error_sink, list):
                error_sink.append(error)
            return {}, {block.block_id: str(error) for block, _ in units}

    def _assign_fields(self, block: Block, segments: dict[str, str]) -> dict[str, Any]:
        def take(key: str) -> str:
            direct = segments.get(_seg_id(block.block_id, key))
            if isinstance(direct, str) and direct:
                return _clean_translated_text(direct)
            # 超长字段被拆成 key~0..n 子段：按序拼回同一个稳定字段；缺任何一段都算未完成。
            pieces: list[str] = []
            index = 0
            while True:
                piece = segments.get(_seg_id(block.block_id, f"{key}~{index}"))
                if not isinstance(piece, str) or not piece:
                    break
                pieces.append(piece)
                index += 1
            return _clean_translated_text("".join(pieces)) if pieces else ""

        text = take("text") if block.source_text else ""
        items = [take(f"item:{index}") if item else "" for index, item in enumerate(block.items)]
        caption = [take(f"caption:{index}") if item else "" for index, item in enumerate(block.caption)]
        footnote = [take(f"footnote:{index}") if item else "" for index, item in enumerate(block.footnote)]
        cells = {cell.cell_id: take(f"cell:{cell.cell_id}") for cell in block.cells if cell.text}
        return {"text": text, "items": items, "caption": caption, "footnote": footnote, "cells": cells}

    @staticmethod
    def _overlay_previous(fields: dict[str, Any], previous: TranslationEntry | None) -> dict[str, Any]:
        """用已校验旧内容补齐本次未返回的字段（仅 previous 已通过 _reusable_previous 校验时传入）。"""
        if previous is None:
            return fields
        fields["text"] = fields.get("text") or previous.text
        for name, stored in (("items", previous.items), ("caption", previous.caption), ("footnote", previous.footnote)):
            values = list(fields.get(name) or [])
            fields[name] = [
                value or (stored[index] if index < len(stored) else "")
                for index, value in enumerate(values)
            ]
        for cell_id, value in (fields.get("cells") or {}).items():
            if not value:
                fields["cells"][cell_id] = previous.cells.get(cell_id, "")
        return fields

    def _apply_outcome(
        self, layer: TranslationLayer, chunk: list[Block], outcome: _ChunkOutcome, *,
        refine: bool, model_run_id: str, reuse_previous: bool = True, run_token: str = "",
    ) -> None:
        for block in chunk:
            previous = layer.entries.get(block.block_id)
            # 只有“已校验且对应当前源文”的旧内容才能被复用；force 运行不复用。
            reusable = previous if (reuse_previous and _reusable_previous(block, previous)) else None
            candidates = list(previous.candidates) if previous else []
            if block.block_id in outcome.segments:
                fields = self._assign_fields(block, outcome.resolved.get(block.block_id, outcome.segments[block.block_id]))
                fields = self._overlay_previous(fields, reusable)
                revisions = list(previous.revisions) if previous else []
                if previous:
                    revisions.append(_entry_snapshot(previous))
                layer.entries[block.block_id] = TranslationEntry(
                    block_id=block.block_id, status="refined" if refine else "translated",
                    source_hash=block_source_digest(block),
                    model_run_id=model_run_id or (previous.model_run_id if previous else ""),
                    run_token=run_token,
                    locked=False, revisions=revisions, updated_at=utc_now(),
                    selection_translations=list(previous.selection_translations) if previous else [],
                    candidates=candidates,
                    structure_revision=int(getattr(block, "structure_revision", 0) or 0),
                    partial=False,
                    error="",
                    **fields,
                )
            elif block.block_id in outcome.failed:
                if refine:
                    # 润色失败时保留原始译文，不把人工/已译内容降级为失败。
                    continue
                resolved = outcome.resolved.get(block.block_id, {})
                # 未通过整块校验的字段不得写入内容，只存为候选。
                # 但“对应当前源文”的旧译文要保留：否则一次失败的 force 重译会把已有译文清空；
                # 保留内容 ≠ 本轮完成，partial 仍按 reusable 判定（force 下为 True）。
                fallback = previous if _reusable_previous(block, previous) else None
                fields = self._overlay_previous(self._assign_fields(block, {}), fallback)
                if resolved:
                    candidates.append({
                        **self._assign_fields(block, resolved), "kind": "ai_candidate",
                        "created_at": utc_now(), "reason": outcome.failed.get(block.block_id, ""),
                    })
                layer.entries[block.block_id] = TranslationEntry(
                    block_id=block.block_id, status="failed",
                    source_hash=block_source_digest(block),
                    model_run_id=previous.model_run_id if previous else "",
                    locked=False,
                    revisions=list(previous.revisions) if previous else [],
                    selection_translations=list(previous.selection_translations) if previous else [],
                    candidates=candidates,
                    structure_revision=int(getattr(block, "structure_revision", 0) or 0),
                    partial=reusable is None,
                    error=outcome.failed.get(block.block_id, ""),
                    **fields,
                )

    def _mark_failed(self, layer: TranslationLayer, chunk: list[Block], reason: str = "") -> None:
        for block in chunk:
            previous = layer.entries.get(block.block_id)
            if previous and previous.locked:
                continue
            reusable = previous if _reusable_previous(block, previous) else None
            layer.entries[block.block_id] = TranslationEntry(
                block_id=block.block_id,
                text=reusable.text if reusable else "",
                cells=dict(reusable.cells) if reusable else {},
                items=list(reusable.items) if reusable else [],
                caption=list(reusable.caption) if reusable else [],
                footnote=list(reusable.footnote) if reusable else [],
                status="failed", source_hash=block_source_digest(block),
                model_run_id=previous.model_run_id if previous else "", locked=False,
                revisions=list(previous.revisions) if previous else [],
                selection_translations=list(previous.selection_translations) if previous else [],
                candidates=list(previous.candidates) if previous else [],
                structure_revision=int(getattr(block, "structure_revision", 0) or 0),
                partial=reusable is None,
                error=reason,
            )

    # ---- 即时翻译 / 编辑 / 锁定 ----------------------------------------- #

    def review(
        self, project_id: str, block_id: str, *, locale: str = "zh-CN", instructions: str = "",
    ) -> dict[str, Any]:
        """定向译审：对照原文与当前译文产出修订候选；不自动保存（用户显式采纳）。"""
        document = self.store.load_document(project_id)
        block = document.block(block_id)
        layer = self.store.load_translation(project_id, locale)
        entry = layer.entries.get(block_id)
        if entry is None or not _entry_has_content(entry):
            raise StorageError("该段落还没有译文可供审查。")
        source_segments = _source_segments(block)
        current = _entry_segments(block, entry)
        current_map = {_seg_id(block_id, key): text for key, text in current}
        if not source_segments or not current_map:
            raise StorageError("该段没有可对照的字段。")
        job_id = self.store.create_job(project_id, "review", {"block_id": block_id, "locale": locale}, {})
        self._active_project = project_id
        self._active_job_id = job_id
        self._active_refine = False
        self._active_scope = "review"
        self._budget_cache = None
        self.store.update_job(project_id, job_id, status="running", progress=0.1)
        self.coordinator.attempt_hooks_for(
            self.service_id, self.provider, project_id=project_id,
            budget=self._budget_value(project_id), cost_of=self._estimate_request_cost,
        )
        source_block = "\n".join(
            f'<src id="{_seg_id(block_id, key)}">{text}</src>' for key, text in source_segments
        )
        current_block = "\n".join(
            f'<cur id="{segment_id}">{text}</cur>' for segment_id, text in current_map.items()
        )
        system = (
            f"你是资深双语译审。对照原文逐字段审查当前{locale}译文：修正误译、漏译、数字/引用与术语错误；"
            "当前译文正确时原样返回。只输出 <seg id=\"...\">修订后译文</seg>，不要解释。"
        )
        user = (
            (f"# 附加要求\n{instructions.strip()}\n" if instructions.strip() else "")
            + f"# 原文\n{source_block}\n\n# 当前译文\n{current_block}\n\n"
            "请为每个 id 输出一个 <seg id=\"...\">修订后译文</seg>。"
        )
        try:
            response = self._complete(system=system, user=user)
        except BudgetExhausted as error:
            self.store.update_job(project_id, job_id, status="budget_paused", error=str(error))
            raise
        except Exception as error:
            self.store.update_job(project_id, job_id, status="failed", error=str(error))
            raise
        parsed = _parse_segments(response.content)
        counts = _parse_segment_counts(response.content)
        revised_segments: dict[str, str] = {}
        for segment_id in current_map:
            if counts.get(segment_id) != 1:
                continue
            value = _clean_translated_text(parsed.get(segment_id, ""))
            if value:
                revised_segments[segment_id] = value
        truncated = str(getattr(response, "finish_reason", "") or "") in _TRUNCATED_FINISH_REASONS
        revised = self._assign_fields(block, revised_segments)
        original = {
            "text": entry.text, "items": list(entry.items), "caption": list(entry.caption),
            "footnote": list(entry.footnote), "cells": dict(entry.cells),
        }
        # 完整性必须按源文所需字段校验：缺字段不能 complete，也不能被采纳。
        required = {_seg_id(block_id, key) for key, _ in source_segments}
        complete = (not truncated) and required <= set(revised_segments)
        changed = complete and any(
            revised.get(key) != original.get(key) for key in ("text", "items", "caption", "footnote", "cells")
        )
        if not complete:
            self.store.update_job(project_id, job_id, status="partial_failed", progress=1, error="译审输出不完整，未提供可采纳候选。")
        else:
            self.store.update_job(project_id, job_id, status="completed", progress=1)
        return {
            "job_id": job_id, "project_id": project_id, "locale": locale, "block_id": block_id,
            "candidate_id": uuid.uuid4().hex,
            "source_hash": block_source_digest(block),
            "base_revision": entry.updated_at,
            "changed": bool(changed), "complete": bool(complete),
            "revised": revised, "original": original,
            "source": source_block, "current": current_block,
        }

    def adopt_review(
        self, project_id: str, block_id: str, *, locale: str, revised: dict[str, Any],
        source_hash: str, base_revision: str, candidate_id: str = "",
    ) -> TranslationEntry:
        """采纳译审候选：源/基线/完整性校验与写入在**同一临界区**内完成，不可分割。

        锁序统一为 document_lock → translation_lock；`edit` 在临界区内重入翻译锁，
        不会释放后再写，因此并发人工编辑无法被旧候选覆盖。
        """
        if not source_hash or not base_revision:
            raise StorageError("译审候选缺少源/译文版本依据，已拒绝采纳。")
        with self.store.document_lock(project_id):
            document = self.store.load_document(project_id)
            block = document.block(block_id)
            if block_source_digest(block) != source_hash:
                raise StorageError("原文在审查期间已变化，候选已作废；请重新审查。")
            source_segments = _source_segments(block)
            missing = [key for key, _ in source_segments if not _revised_has_field(revised, key)]
            if missing:
                raise StorageError("译审候选缺少字段，已拒绝采纳；请重新审查。")
            with self.store.translation_lock(project_id, locale):
                layer = self.store.load_translation(project_id, locale)
                entry = layer.entries.get(block_id)
                if entry is None:
                    raise StorageError("译文已被移除，候选已作废；请重新审查。")
                if entry.updated_at != base_revision:
                    # 幂等：若已是同一候选内容则视为已采纳；否则拒绝并保留最新人工内容。
                    if entry.locked and _entry_matches_revised(entry, revised):
                        return entry
                    raise StorageError("译文在审查期间已被修改（可能是人工编辑），候选已作废；请重新审查。")
                # 仍在同一翻译锁临界区内写入（edit 重入本锁，不释放）。
                text = revised.get("text")
                return self.edit(
                    project_id, block_id, locale=locale,
                    text=text if isinstance(text, str) and text else None,
                    cells=revised.get("cells") if isinstance(revised.get("cells"), dict) else None,
                    items=revised.get("items") if isinstance(revised.get("items"), list) else None,
                    caption=revised.get("caption") if isinstance(revised.get("caption"), list) else None,
                    footnote=revised.get("footnote") if isinstance(revised.get("footnote"), list) else None,
                )

    def instant(self, project_id: str, text: str, *, locale: str = "zh-CN", block_id: str | None = None, save: bool = False) -> dict[str, Any]:
        document = self.store.load_document(project_id)
        job_id = self.store.create_job(project_id, "instant", {"block_id": block_id, "locale": locale}, {})
        # 即时请求也进入公共协调层（预算/并发），用独立（非翻译作业）上下文。
        self._active_project = project_id
        self._active_job_id = job_id
        self._active_refine = False
        self._active_scope = "instant"
        self._budget_cache = None
        self.store.update_job(project_id, job_id, status="running", progress=0.1)
        try:
            response = self._complete(
                system=(
                    f"你是资深的{locale}学术翻译。将下面的{document.source_language}文本准确、自然地译成{locale}；"
                    "人名、机构名等专名给出通行中译，不要保留整句原文。只返回译文，不要解释。"
                ),
                user=text,
            )
        except BudgetExhausted as error:
            self.store.update_job(project_id, job_id, status="budget_paused", error=str(error))
            raise
        except Exception as error:
            self.store.update_job(project_id, job_id, status="failed", error=str(error))
            raise
        truncated = str(getattr(response, "finish_reason", "") or "") in _TRUNCATED_FINISH_REASONS
        saved_text = _clean_translated_text(response.content.strip())
        try:
            if save:
                if not block_id:
                    raise StorageError("保存即时翻译需要 block_id。")
                # 即时翻译只产出单个 text 字段；被截断或面向表格/列表等字段级块时不能冒充完整译文。
                if truncated:
                    raise ProviderError("即时翻译被截断，未保存；请缩小选区或稍后重试。")
                if not saved_text:
                    raise ProviderError("即时翻译返回空内容，未保存。")
                block = document.block(block_id)
                if not block.source_text or block.items or block.caption or block.footnote or block.cells:
                    raise StorageError("该段落使用字段级译文，请改用划选保存，不要覆盖整块。")
                lock = self.store.translation_lock(project_id, locale)
                with lock:
                    layer = self.store.load_translation(project_id, locale)
                    previous = layer.entries.get(block_id)
                    # 人工编辑会自动锁定；即时保存不得静默覆盖锁定/人工译文。
                    if previous is not None and (previous.locked or previous.status == "edited"):
                        raise StorageError("该段落已有锁定或人工译文，即时翻译未覆盖；如需替换请先解锁。")
                    revisions = list(previous.revisions) if previous else []
                    if previous is not None:
                        # 把被替换的旧内容写进修订历史，保证可追溯、可恢复。
                        revisions.append(_entry_snapshot(previous))
                    layer.entries[block_id] = TranslationEntry(
                        block_id=block_id, text=saved_text, status="translated",
                        source_hash=block_source_digest(block), model_run_id=response.request_id,
                        revisions=revisions,
                        selection_translations=list(previous.selection_translations) if previous else [],
                        candidates=list(previous.candidates) if previous else [],
                        structure_revision=int(getattr(block, "structure_revision", 0) or 0),
                    )
                    self.store.save_translation(layer)
        except Exception as error:
            self.store.update_job(project_id, job_id, status="failed", error=str(error))
            raise
        self.store.update_job(project_id, job_id, status="completed", progress=1, result={"provider": response.provider, "model": response.model})
        return {"text": saved_text, "job_id": job_id}

    def edit(
        self, project_id: str, block_id: str, text: str | None = None, *, locale: str = "zh-CN",
        cells: dict[str, str] | None = None, items: list[str] | None = None,
        caption: list[str] | None = None, footnote: list[str] | None = None,
    ) -> TranslationEntry:
        """人工编辑：支持整块 text 与按字段（表格 cell / 列表 item / 图题 / 脚注）分别保存（F10）。"""
        document = self.store.load_document(project_id)
        block = document.block(block_id)
        lock = self.store.translation_lock(project_id, locale)
        with lock:
            layer = self.store.load_translation(project_id, locale)
            previous = layer.entries.get(block_id)
            revisions = list(previous.revisions) if previous else []
            if previous:
                revisions.append(_entry_snapshot(previous))
            entry = TranslationEntry(
                block_id=block_id,
                text=text if text is not None else (previous.text if previous else ""),
                cells={**(previous.cells if previous else {}), **(cells or {})},
                items=list(items) if items is not None else (list(previous.items) if previous else []),
                caption=list(caption) if caption is not None else (list(previous.caption) if previous else []),
                footnote=list(footnote) if footnote is not None else (list(previous.footnote) if previous else []),
                status="edited",
                source_hash=block_source_digest(block), model_run_id=previous.model_run_id if previous else "",
                locked=True, revisions=revisions, updated_at=utc_now(),
                selection_translations=list(previous.selection_translations) if previous else [],
                candidates=list(previous.candidates) if previous else [],
                structure_revision=int(getattr(block, "structure_revision", 0) or 0),
                error="",
            )
            layer.entries[block_id] = entry
            self.store.save_translation(layer)
            return entry

    def unlock(self, project_id: str, block_id: str, *, locale: str = "zh-CN") -> TranslationEntry:
        lock = self.store.translation_lock(project_id, locale)
        with lock:
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
        lock = self.store.translation_lock(project_id, locale)
        with lock:
            layer = self.store.load_translation(project_id, locale)
            entry = layer.entries.get(block_id) or TranslationEntry(block_id=block_id, source_hash=block_source_digest(block), structure_revision=int(getattr(block, "structure_revision", 0) or 0))
            entry.selection_translations.append({
                "source_text": source_text, "translated_text": translated_text,
                "source_hash": block_source_digest(block), "created_at": utc_now(),
            })
            entry.updated_at = utc_now()
            layer.entries[block_id] = entry
            self.store.save_translation(layer)
            return entry
