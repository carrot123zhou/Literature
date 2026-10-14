"""自适应分支结构校对：先验证目录/正文锚点框架，再按可靠区域局部并行规划。

设计目标（对应产品决策）：

1. **目录优先但不独占**：用户确认的目录优先；模型/视觉目录只有在正文匹配强度、
   唯一性、阅读顺序与多锚点页码一致性都满足时才被当作可靠锚点。AI 自报置信度、
   单一模糊匹配或单个印刷页码都不能独自授权边界。
2. **可靠区域**固定“目录祖先 + 章内候选 + 边界重叠（只读）”，默认 2 路有界并行；
   每个候选（含未收录小节、疑似漏标短块）恰好得到一次主判断，重叠只读、不重复计入。
3. **歧义区域**（无锚点、重复标题、边界歧义）扩大相邻章上下文并串行处理；整份目录
   无法建立可靠框架时回退原来的整体串行规划（`reasoning.plan_outline`）。
4. 本地统一树检查，异常最多补查一轮，仍异常留人工；目录注释不覆盖正文标题文字。

本模块只做“规划”阶段；复核阶段仍由 `structure.run_structure_job` 的选择性/全量复核
与 `validate_hierarchy` 处理。本模块不导入 `structure`，避免循环依赖。
"""
from __future__ import annotations

import json
import math
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

from . import structure_signals as signals
from .structure_reasoning import (
    OUTLINE_PROMPT,
    ROLES,
    compact_candidate,
    outline_inputs,
    plan_outline,
    validate_hierarchy,
)

# 锚点可靠性阈值（保守：正文匹配 + 唯一性 + 顺序 + 层级佐证）。
ANCHOR_MIN_SCORE = 0.92
ANCHOR_MIN_MARGIN = 0.06
MIN_RELIABLE_ANCHORS = 2
RELIABLE_COVERAGE = 0.6
REGION_WINDOW = 64
REGION_CONTEXT = 6
OVERVIEW_SAMPLE = 24
MAX_PARALLEL_REGIONS = 2
# 预检门控：小区域装箱合并的上限（主候选数）。
MERGED_MAX_PRIMARY = 2 * REGION_WINDOW
# 全作业追加规划请求（局部升级 + 全局补查）的硬上限，防止逐区一轮+全局一轮膨胀。
MAX_ADDITIONAL_PLANNING_REQUESTS = 4
# 缺失补查批大小：按阅读顺序切批，避免把所有 missing 塞进单请求。
ADAPTIVE_SECONDARY_WINDOW = 64
# 候选太少时整体串行更省（局部请求各自重复 system prompt 与上下文）；阈值供 Lead 复核。
ADAPTIVE_MIN_CANDIDATES = 120
# 印刷页码一致性至少需要这么多可靠锚点才作为证据。
PAGE_MAPPING_MIN_ANCHORS = 3


@dataclass
class Anchor:
    toc_index: int
    title: str
    level: int
    block_id: str
    score: float
    margin: float
    level_source: str
    printed_page: str
    reliable: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "toc_index": self.toc_index, "title": self.title, "level": self.level,
            "block_id": self.block_id, "score": round(self.score, 3), "margin": round(self.margin, 3),
            "level_source": self.level_source, "printed_page": self.printed_page,
            "reliable": self.reliable, "reasons": list(self.reasons),
        }


@dataclass
class Region:
    index: int
    anchor_block_id: str | None
    anchor_level: int | None
    primary_ids: list[str]
    context_ids: list[str]
    reliable: bool
    reasons: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index, "anchor_block_id": self.anchor_block_id, "anchor_level": self.anchor_level,
            "anchors": list(self.anchors),
            "primary": len(self.primary_ids), "context": len(self.context_ids),
            "reliable": self.reliable, "reasons": list(self.reasons),
        }


@dataclass
class Framework:
    state: str  # reliable | partial | unreliable
    anchors: list[Anchor]
    reliable_anchors: list[Anchor]
    duplicate_titles: list[str]
    order_inversions: int
    page_mapping_consistent: bool
    coverage: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state, "anchor_count": len(self.anchors),
            "reliable_anchors": len(self.reliable_anchors),
            "reliable_anchor_details": [a.to_dict() for a in self.reliable_anchors][:100],
            "duplicate_titles": list(self.duplicate_titles),
            "order_inversions": self.order_inversions, "page_mapping_consistent": self.page_mapping_consistent,
            "coverage": round(self.coverage, 3), "reasons": list(self.reasons),
        }


def _role_level(role: Any) -> int | None:
    if isinstance(role, str) and len(role) == 2 and role[0] == "h" and role[1] in "123456":
        return int(role[1])
    return None


def _valid_confidence(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0)


def _level_corroborated(candidate: dict[str, Any], level: int) -> str:
    """正文层级是否佐证目录层级；返回空串表示不能佐证。"""
    if candidate.get("type") != "title":
        return "正文块当前不是标题，层级无从佐证"
    current = int(candidate.get("level") or 0)
    text = str(candidate.get("text") or "")
    numbered = None
    import re
    match = re.match(r"^\s*(\d+(?:\.\d+)*)", text)
    if match:
        numbered = min(6, match.group(1).count(".") + 1)
    if current == level or (numbered is not None and numbered == level):
        return ""
    if abs(current - level) <= 1:
        return ""
    return f"目录层级 H{level} 与正文现有层级 H{current} 相差较大且编号不支持"


def _lis_keep(sequence: list[int]) -> list[int]:
    """返回最长严格递增子序列的下标，用于剔除破坏阅读顺序的锚点。"""
    if not sequence:
        return []
    tails: list[int] = []       # values of the smallest tail for each length
    tails_index: list[int] = []  # index in sequence
    previous = [-1] * len(sequence)
    import bisect
    for index, value in enumerate(sequence):
        position = bisect.bisect_left(tails, value)
        if position == len(tails):
            tails.append(value)
            tails_index.append(index)
        else:
            tails[position] = value
            tails_index[position] = index
        previous[index] = tails_index[position - 1] if position else -1
    keep: list[int] = []
    cursor = tails_index[-1] if tails_index else -1
    while cursor >= 0:
        keep.append(cursor)
        cursor = previous[cursor]
    return sorted(keep)


def build_framework(payload: dict[str, Any]) -> Framework:
    """从对账结果与正文候选建立锚点框架；只读，不调用模型。"""
    candidates = payload.get("title_candidates") or []
    by_id = {item["block_id"]: item for item in candidates}
    position = {item["block_id"]: index for index, item in enumerate(candidates)}
    toc = payload.get("authoritative_toc") or []
    matched = payload.get("matched_titles") or []

    toc_counts: dict[str, int] = {}
    for index, entry in enumerate(toc):
        key = signals.heading_key(str(entry.get("title", "")))
        if key:
            toc_counts[key] = toc_counts.get(key, 0) + 1
    duplicate_titles = sorted({str(entry.get("title", "")) for index, entry in enumerate(toc)
                               if toc_counts.get(signals.heading_key(str(entry.get("title", "")))) and
                               toc_counts[signals.heading_key(str(entry.get("title", "")))] > 1})
    # 一个正文块被多个目录条目命中，或一个目录条目命中多个块：都不能算唯一锚点。
    block_hits: dict[str, int] = {}
    entry_hits: dict[int, int] = {}
    score_by_entry: dict[int, list[tuple[float, str]]] = {}
    for item in matched:
        block_id = str(item.get("block_id") or "")
        if block_id not in by_id:
            continue
        block_hits[block_id] = block_hits.get(block_id, 0) + 1
        toc_index = int(item.get("toc_index") or 0)
        entry_hits[toc_index] = entry_hits.get(toc_index, 0) + 1
        score_by_entry.setdefault(toc_index, []).append((float(item.get("score") or 0.0), block_id))

    anchors: list[Anchor] = []
    for item in matched:
        block_id = str(item.get("block_id") or "")
        candidate = by_id.get(block_id)
        if candidate is None:
            continue
        toc_index = int(item.get("toc_index") or 0)
        entry = toc[toc_index] if 0 <= toc_index < len(toc) else {}
        title = str(entry.get("title", "") or item.get("title", ""))
        score = float(item.get("score") or 0.0)
        level = int(item.get("level") or entry.get("level") or 1)
        level_source = str(item.get("level_source") or entry.get("level_source") or "confirmed")
        printed_page = str(entry.get("printed_page") or "")
        scores = sorted({value for value, _ in score_by_entry.get(toc_index, [])}, reverse=True)
        margin = scores[0] - (scores[1] if len(scores) > 1 else 0.0) if scores else score
        reasons: list[str] = []
        reliable = True
        if score < ANCHOR_MIN_SCORE:
            reliable = False
            reasons.append(f"正文匹配强度不足（{score:.3f} < {ANCHOR_MIN_SCORE}）")
        if margin < ANCHOR_MIN_MARGIN:
            reliable = False
            reasons.append(f"匹配不唯一（领先幅度 {margin:.3f} < {ANCHOR_MIN_MARGIN}）")
        if block_hits.get(block_id, 0) > 1 or entry_hits.get(toc_index, 0) > 1:
            reliable = False
            reasons.append("同一块/同一目录条目存在多个匹配，唯一性不足")
        key = signals.heading_key(title)
        if key and toc_counts.get(key, 0) > 1:
            reliable = False
            reasons.append("目录中存在重名标题，不能单独定边界")
        level_note = _level_corroborated(candidate, level)
        if level_note:
            reliable = False
            reasons.append(level_note)
        if level_source not in {"confirmed", "model", "vision"}:
            reliable = False
            reasons.append(f"目录层级来源为 {level_source}，不能单独授权边界")
        anchors.append(Anchor(toc_index=toc_index, title=title, level=level, block_id=block_id,
                              score=score, margin=margin, level_source=level_source, printed_page=printed_page,
                              reliable=reliable, reasons=reasons))

    # 阅读顺序一致性：保留最长递增子序列，其余剔除。
    ordered = sorted([a for a in anchors if a.reliable], key=lambda a: a.toc_index)
    sequence = [position.get(a.block_id, -1) for a in ordered]
    keep = _lis_keep([value for value in sequence if value >= 0])
    kept_ids = {ordered[index].block_id for index in keep if index < len(ordered)}
    order_inversions = len(ordered) - len(kept_ids)
    for anchor in ordered:
        if anchor.block_id not in kept_ids and anchor.reliable:
            anchor.reliable = False
            anchor.reasons.append("与其它锚点的阅读顺序不一致（疑似目录/正文错位）")

    reliable_anchors = [a for a in sorted(anchors, key=lambda a: position.get(a.block_id, 10 ** 9))
                        if a.reliable]

    # 多锚点页码一致性：至少 3 个带印刷页码的可靠锚点才作为证据。
    paginated = [a for a in reliable_anchors if str(a.printed_page).strip().isdigit()]
    page_mapping_consistent = True
    if len(paginated) >= PAGE_MAPPING_MIN_ANCHORS:
        pages = [int(a.printed_page) for a in paginated]
        if any(pages[i] > pages[i + 1] for i in range(len(pages) - 1)):
            page_mapping_consistent = False

    coverage = _reliable_coverage(candidates, reliable_anchors)
    reasons: list[str] = []
    if not reliable_anchors:
        state = "unreliable"
        reasons.append("没有可靠的正文锚点，回退整体串行规划")
    else:
        state = "reliable"
        if len(reliable_anchors) < MIN_RELIABLE_ANCHORS:
            state = "partial"
            reasons.append(f"可靠锚点仅 {len(reliable_anchors)} 个，少于 {MIN_RELIABLE_ANCHORS}")
        if coverage < RELIABLE_COVERAGE:
            state = "partial"
            reasons.append(f"可靠区域覆盖率 {coverage:.0%} 低于 {RELIABLE_COVERAGE:.0%}")
        if len(paginated) >= PAGE_MAPPING_MIN_ANCHORS and not page_mapping_consistent:
            state = "partial"
            reasons.append("多锚点印刷页码不单调，页码不作为边界证据")
    if duplicate_titles:
        reasons.append(f"目录存在 {len(duplicate_titles)} 个重名标题，相关区域升级为串行")
    if order_inversions:
        reasons.append(f"剔除 {order_inversions} 个阅读顺序不一致的锚点")
    return Framework(state=state, anchors=anchors, reliable_anchors=reliable_anchors,
                     duplicate_titles=duplicate_titles, order_inversions=order_inversions,
                     page_mapping_consistent=page_mapping_consistent, coverage=coverage, reasons=reasons)


def _chapter_level_anchors(reliable_anchors: list[Anchor]) -> list[Anchor]:
    """用最浅层的可靠锚点定章；更深的可靠锚点只作为章内祖先，不再切新章。"""
    if not reliable_anchors:
        return []
    min_level = min(anchor.level for anchor in reliable_anchors)
    return [anchor for anchor in reliable_anchors if anchor.level <= min_level]


def _reliable_coverage(candidates: list[dict[str, Any]], reliable_anchors: list[Anchor]) -> float:
    if not candidates or not reliable_anchors:
        return 0.0
    regions = _partition_by_anchors(candidates, _chapter_level_anchors(reliable_anchors))
    covered = sum(len(region) for region, _ in regions)
    return covered / len(candidates)


def _partition_by_anchors(candidates: list[dict[str, Any]],
                          reliable_anchors: list[Anchor]) -> list[tuple[list[str], list[str]]]:
    """按可靠锚点切分候选阅读顺序，返回 [(primary_ids, context_ids)]。"""
    if not candidates:
        return []
    order = [item["block_id"] for item in candidates]
    anchors = [a for a in reliable_anchors if a.block_id in set(order)]
    anchors.sort(key=lambda a: order.index(a.block_id))
    boundaries = [order.index(a.block_id) for a in anchors]
    primary: list[list[str]] = []
    start = 0
    for boundary in boundaries + [len(order)]:
        primary.append(order[start:boundary])
        start = boundary
    # 第一个 primary 可能为空（第一个锚点就在开头之前）；去掉空段。
    primary = [segment for segment in primary if segment]
    output: list[tuple[list[str], list[str]]] = []
    for index, segment in enumerate(primary):
        head = primary[index - 1][-REGION_CONTEXT:] if index > 0 else []
        tail = primary[index + 1][:REGION_CONTEXT] if index + 1 < len(primary) else []
        context = list(dict.fromkeys(head + tail))
        output.append((segment, [bid for bid in context if bid not in set(segment)]))
    return output


def partition_regions(candidates: list[dict[str, Any]], framework: Framework) -> list[Region]:
    """把候选按可靠锚点切成主判断区域；无锚点/重名/歧义区域标记为不可靠（串行）。"""
    order = [item["block_id"] for item in candidates]
    by_id = {item["block_id"]: item for item in candidates}
    chapter_anchors = _chapter_level_anchors(framework.reliable_anchors)
    reliable_positions = {a.block_id: order.index(a.block_id) for a in chapter_anchors
                          if a.block_id in set(order)}
    regions: list[Region] = []
    index = 0
    if not reliable_positions:
        return [Region(index=0, anchor_block_id=None, anchor_level=None,
                       primary_ids=list(order), context_ids=[], reliable=False,
                       reasons=["没有可靠锚点"])]
    sorted_anchors = sorted(reliable_positions.items(), key=lambda item: item[1])
    cursor = 0
    # 头部无锚点区域。
    if sorted_anchors[0][1] > 0:
        head = order[:sorted_anchors[0][1]]
        context = order[sorted_anchors[0][1]: sorted_anchors[0][1] + REGION_CONTEXT]
        regions.append(Region(index=index, anchor_block_id=None, anchor_level=None,
                              primary_ids=head, context_ids=context, reliable=False,
                              anchors=[], reasons=["首个可靠锚点之前的头部区域，无目录锚点"]))
        index += 1
    for position, (block_id, boundary) in enumerate(sorted_anchors):
        next_boundary = sorted_anchors[position + 1][1] if position + 1 < len(sorted_anchors) else len(order)
        # 大区域按 REGION_WINDOW 再切分，保证单次请求有界。
        for offset in range(boundary, next_boundary, REGION_WINDOW):
            segment = order[offset:min(offset + REGION_WINDOW, next_boundary)]
            context = order[max(0, offset - REGION_CONTEXT):offset] + \
                order[min(offset + REGION_WINDOW, next_boundary):min(offset + REGION_WINDOW, next_boundary) + REGION_CONTEXT]
            anchor = by_id.get(block_id, {})
            reasons: list[str] = []
            reliable = True
            if offset != boundary:
                # 同一章内的后续窗口仍以该章锚点为祖先。
                reasons.append("同一章的后续窗口，复用章锚点")
            if any(signals.heading_key(str(by_id.get(bid, {}).get("text") or "")) in
                   {signals.heading_key(title) for title in framework.duplicate_titles}
                   for bid in segment):
                reliable = False
                reasons.append("区域内含重名标题，升级为串行")
            regions.append(Region(index=index, anchor_block_id=block_id,
                                  anchor_level=int(anchor.get("level") or 0) or None,
                                  primary_ids=segment,
                                  context_ids=[bid for bid in dict.fromkeys(context) if bid not in set(segment)],
                                  reliable=reliable, anchors=[block_id], reasons=reasons))
            index += 1
    return regions


def build_region_request(payload: dict[str, Any], region: Region, framework: Framework, *,
                         include_overview: bool, context_span: int = REGION_CONTEXT,
                         phase: str = "adaptive", only_ids: list[str] | None = None) -> dict[str, Any]:
    """构造局部规划请求：固定目录祖先 + 章内候选 + 只读重叠上下文；不重复整本目录/overview。

    ``context_span`` 可放大相邻章上下文（供局部升级/歧义区域串行处理使用）。
    ``only_ids`` 仅用于局部升级：只把可疑 ID 作为主候选，原区其余候选与相邻块作为只读上下文。"""
    by_id = {item["block_id"]: item for item in payload.get("title_candidates") or []}
    order = [item["block_id"] for item in payload.get("title_candidates") or []]
    position = {bid: index for index, bid in enumerate(order)}
    toc = payload.get("authoritative_toc") or []
    anchor_ids = set(region.anchors or ([region.anchor_block_id] if region.anchor_block_id else []))
    anchor_list = sorted((a for a in framework.anchors if a.block_id in anchor_ids), key=lambda a: a.toc_index)
    entries: dict[int, dict[str, Any]] = {}
    for anchor in anchor_list:
        current_level = anchor.level
        chain: list[dict[str, Any]] = []
        for entry_index in range(anchor.toc_index - 1, -1, -1):
            entry = toc[entry_index] if 0 <= entry_index < len(toc) else {}
            entry_level = int(entry.get("level") or 1)
            if entry_level < current_level:
                chain.append({"title": str(entry.get("title", "")), "level": entry_level,
                              "printed_page": str(entry.get("printed_page") or "")})
                current_level = entry_level
        chain.reverse()
        for offset, item in enumerate(chain):
            entries.setdefault(anchor.toc_index - len(chain) + offset, item)
        entries.setdefault(anchor.toc_index, {"title": anchor.title, "level": anchor.level,
                                              "printed_page": anchor.printed_page, "anchor_block_id": anchor.block_id})
    ancestors = [entries[key] for key in sorted(entries)]
    chapter_level = min((a.level for a in framework.reliable_anchors), default=None)
    root_convention = {
        "anchor_level": chapter_level,
        "note": (f"可靠章锚点为 H{chapter_level}，章内小节按阅读顺序、相邻标题与层级递推判断；"
                 "未收录小节不得仅因未收录而降级。" if chapter_level else
                 "无可靠章锚点：按阅读顺序与相邻标题推断；证据不足用 uncertain。"),
    }
    request: dict[str, Any] = {
        "stage": "outline-plan-v2",
        "planning": phase,
        "document_title": payload.get("document_title", ""),
        "bbox_space": payload.get("bbox_space", "none"),
        "root_convention": root_convention,
        "region": {"index": region.index, "reliable": region.reliable,
                   "anchors": list(region.anchors),
                   "anchor_block_id": region.anchor_block_id, "anchor_level": region.anchor_level,
                   "reasons": list(region.reasons)},
        "toc": ancestors,
        "candidates": [compact_candidate(by_id[bid]) for bid in (only_ids if only_ids is not None else region.primary_ids)
                       if bid in by_id],
        "context": [],
        "profile": {},
        "previous_outline": [],
    }
    primary_ids = list(only_ids) if only_ids is not None else list(region.primary_ids)
    primary = set(primary_ids)
    # 相邻章上下文：按阅读顺序向两端各取 context_span 个候选（只读）。
    if primary_ids:
        first = position.get(primary_ids[0], 0)
        last = position.get(primary_ids[-1], 0)
        neighbors = order[max(0, first - context_span):first] + order[last + 1:last + 1 + context_span]
        read_only = list(region.context_ids) + neighbors
        if only_ids is not None:
            # 局部升级：原区其余候选作为只读上下文，帮助模型判断可疑项。
            read_only = [bid for bid in region.primary_ids if bid not in primary] + read_only
        request["context"] = [compact_candidate(by_id[bid]) for bid in dict.fromkeys(read_only)
                              if bid in by_id and bid not in primary]
    if include_overview:
        headings = [item for item in (payload.get("title_candidates") or []) if item["type"] == "title"]
        step = max(1, len(headings) // OVERVIEW_SAMPLE)
        request["overview"] = [compact_candidate(item) for item in headings[::step][:OVERVIEW_SAMPLE]]
    return request


def _parse_outline(content: str) -> tuple[list[dict[str, Any]], str]:
    try:
        value = json.loads(content or "{}")
    except ValueError:
        return [], "响应不是合法 JSON"
    rows = value.get("outline")
    if not isinstance(rows, list):
        return [], "响应缺少 outline 数组"
    return rows, ""


def _apply_rows(rows: list[dict[str, Any]], primary: set[str], allowed_ids: set[str],
                order: dict[str, int], nodes: dict[str, dict[str, Any]], issues: list[str]) -> None:
    """严格校验规划行；与 ``plan_outline`` 同等严格，不静默让后值覆盖前值。

    - ``bid`` 必须是字符串；同一主批重复 ID 整项拒绝（弹出已存节点）。
    - 主候选之外：合法 context/anchor ID 视为只读忽略；未知 ID 记 issue。
    - ``role`` 必须是字符串且属于 ROLES；``confidence`` 有限且 [0,1]；uncertain 记 issue。
    - ``parent_id`` 必须是 null 或给定 ID，且在该候选之前出现；层级关系交给 ``validate_hierarchy``。
    """
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            issues.append("规划行不是对象")
            continue
        bid = row.get("block_id")
        if not isinstance(bid, str):
            issues.append("规划行的 block_id 不是字符串")
            continue
        if bid not in allowed_ids:
            issues.append(f"规划行包含未知 block_id：{bid}")
            continue
        if bid not in primary:
            continue  # 只读上下文/锚点，忽略
        if bid in seen:
            nodes.pop(bid, None)
            issues.append(f"{bid} 在同一主批重复出现，整项拒绝")
            continue
        seen.add(bid)
        role = row.get("role")
        confidence = row.get("confidence")
        if not isinstance(role, str) or role not in ROLES:
            nodes.pop(bid, None)
            issues.append(f"{bid} 的角色不合法")
            continue
        if not _valid_confidence(confidence):
            nodes.pop(bid, None)
            issues.append(f"{bid} 的置信度不合法")
            continue
        if role == "uncertain":
            nodes.pop(bid, None)
            issues.append(f"{bid} 证据不足（uncertain），留人工")
            continue
        if "parent_id" not in row:
            nodes.pop(bid, None)
            issues.append(f"{bid} 缺少 parent_id")
            continue
        parent = row.get("parent_id")
        if parent is not None and (not isinstance(parent, str) or parent not in allowed_ids):
            nodes.pop(bid, None)
            issues.append(f"{bid} 的 parent_id 不合法：{parent!r}")
            continue
        if isinstance(parent, str) and order.get(parent, 10 ** 9) >= order.get(bid, -1):
            nodes.pop(bid, None)
            issues.append(f"{bid} 的父标题未在其之前出现")
            continue
        nodes[bid] = {"block_id": bid, "role": role, "parent_id": parent,
                      "confidence": float(confidence), "reason": str(row.get("reason", ""))[:350]}


def _merge_regions(regions: list[Region], target_count: int, max_primary: int) -> list[Region]:
    """把相邻小区域装箱合并到 target_count 以内（保留各章 anchors 与边界标记）。

    合并可靠区域仍为可靠；合并含歧义区域则整体转为串行。超过 max_primary 不合并。
    """
    merged = [Region(index=r.index, anchor_block_id=r.anchor_block_id, anchor_level=r.anchor_level,
                     primary_ids=list(r.primary_ids), context_ids=list(r.context_ids),
                     reliable=r.reliable, anchors=list(r.anchors), reasons=list(r.reasons)) for r in regions]
    while len(merged) > target_count:
        best: tuple[int, int] | None = None
        for position in range(len(merged) - 1):
            combined = len(merged[position].primary_ids) + len(merged[position + 1].primary_ids)
            if combined <= max_primary and (best is None or combined < best[0]):
                best = (combined, position)
        if best is None:
            break
        _, position = best
        left, right = merged[position], merged[position + 1]
        merged[position] = Region(
            index=left.index, anchor_block_id=left.anchor_block_id, anchor_level=left.anchor_level,
            primary_ids=left.primary_ids + right.primary_ids,
            context_ids=list(dict.fromkeys(left.context_ids + right.context_ids)),
            reliable=left.reliable and right.reliable,
            anchors=list(dict.fromkeys(left.anchors + right.anchors)),
            reasons=left.reasons + right.reasons + [f"与相邻区域合并以控制请求数（{left.index}+{right.index}）"],
        )
        del merged[position + 1]
    for index, region in enumerate(merged):
        region.index = index
    return merged


def run_adaptive_planning(payload: dict[str, Any], provider: Any, *,
                          on_event: Callable[[str, dict[str, Any]], None] | None = None,
                          stop_event: threading.Event | None = None,
                          parallel: int = MAX_PARALLEL_REGIONS,
                          min_candidates: int = ADAPTIVE_MIN_CANDIDATES,
                          use_gate: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    """自适应规划；返回 (plan, meta)。可靠性不足/预检门控不通过时回退整体串行。

    ``use_gate=False`` 仅用于单元测试分支机制；生产默认启用预检门控。
    未传入 ``stop_event`` 时创建内部事件，失败时置位以避免未发的在途批继续发送。"""
    if stop_event is None:
        stop_event = threading.Event()
    if stop_event.is_set():
        raise RuntimeError("自适应规划已中止：不发送新请求")
    def event(kind: str, **values: Any) -> None:
        if on_event is not None:
            on_event(kind, values)

    framework = build_framework(payload)
    candidates = payload.get("title_candidates") or []
    if framework.state == "unreliable" or len(candidates) < min_candidates:
        if framework.state == "unreliable":
            reason = "；".join(framework.reasons) or "没有可靠锚点"
        else:
            reason = f"候选数 {len(candidates)} 少于自适应阈值 {min_candidates}，整体串行更省"
        plan = plan_outline(payload, provider,
                            on_window=lambda number, total, count: event("outline_window", number=number,
                                                                         total=total, count=count))
        meta = {"mode": "adaptive", "framework": framework.to_dict(), "fallback": True,
                "fallback_reason": reason, "regions": [],
                "second_round": 0, "overlap_ignored": 0, "local_upgrades": 0}
        event("adaptive_fallback", reason=reason)
        return plan, meta

    regions = partition_regions(candidates, framework)
    global_requests = len(outline_inputs(payload))
    global_chars = sum(len(OUTLINE_PROMPT) + len(json.dumps({**request, "stage": "outline-plan-v2"}, ensure_ascii=False))
                       for request in outline_inputs(payload))

    def proxy(counted: list[Region]) -> tuple[dict[int, dict[str, Any]], int, int]:
        built = {region.index: build_region_request(payload, region, framework, include_overview=(index == 0))
                 for index, region in enumerate(counted)}
        chars = sum(len(OUTLINE_PROMPT) + len(json.dumps(request, ensure_ascii=False)) for request in built.values())
        reliable_count = sum(1 for region in counted if region.reliable)
        ambiguous_count = len(counted) - reliable_count
        rounds = (math.ceil(reliable_count / max(1, parallel)) if reliable_count else 0) + ambiguous_count
        return built, chars, rounds

    merged = False
    region_requests, adaptive_chars, parallel_rounds = proxy(regions)
    if len(regions) > global_requests:
        candidate_regions = _merge_regions(regions, global_requests, MERGED_MAX_PRIMARY)
        if len(candidate_regions) < len(regions):
            regions = candidate_regions
            region_requests, adaptive_chars, parallel_rounds = proxy(regions)
            merged = True
    gate_ok = len(regions) <= global_requests and adaptive_chars < global_chars and parallel_rounds <= global_requests
    gate = {"adaptive_requests": len(regions), "adaptive_chars": adaptive_chars, "parallel_rounds": parallel_rounds,
            "global_requests": global_requests, "global_chars": global_chars, "merged": merged, "passed": gate_ok}
    if use_gate and not gate_ok:
        reason = (f"预检门控不通过：自适应 {len(regions)} 请求/{adaptive_chars} 字符/关键路径 {parallel_rounds} 轮 vs "
                  f"全局 {global_requests} 请求/{global_chars} 字符；回退整体串行")
        plan = plan_outline(payload, provider,
                            on_window=lambda number, total, count: event("outline_window", number=number,
                                                                         total=total, count=count))
        meta = {"mode": "adaptive", "framework": framework.to_dict(), "fallback": True,
                "fallback_reason": reason, "regions": [r.to_dict() for r in regions], "gate": gate,
                "second_round": 0, "overlap_ignored": 0, "local_upgrades": 0}
        event("adaptive_fallback", reason=reason)
        return plan, meta

    reliable_regions = [r for r in regions if r.reliable]
    ambiguous_regions = [r for r in regions if not r.reliable]
    primary_by_region = {r.index: set(r.primary_ids) for r in regions}
    order = {item["block_id"]: index for index, item in enumerate(candidates)}
    anchor_ids = {a.block_id for a in framework.anchors}
    allowed_by_region = {r.index: set(r.primary_ids) | set(r.context_ids) | set(r.anchors) | anchor_ids
                         for r in regions}
    nodes: dict[str, dict[str, Any]] = {}
    issues: list[str] = []
    overlap_ignored = 0
    requests_made = 0
    upgrade_requests = 0
    secondary_requests = 0

    def call_region(region: Region) -> tuple[int, list[dict[str, Any]], str, int]:
        if stop_event.is_set():
            raise RuntimeError("自适应规划已中止：不发送新请求")
        request = region_requests[region.index]
        response = provider.complete(system=OUTLINE_PROMPT, user=json.dumps(request, ensure_ascii=False))
        rows, error = _parse_outline(response.content)
        ignored = sum(1 for row in rows if isinstance(row, dict) and row.get("block_id") not in primary_by_region[region.index])
        return region.index, rows, error, ignored

    def run_reliable() -> None:
        nonlocal overlap_ignored, requests_made
        if not reliable_regions:
            return
        workers = max(1, min(int(parallel), MAX_PARALLEL_REGIONS, len(reliable_regions)))
        event("adaptive_parallel", regions=len(reliable_regions), workers=workers)
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="literature-adaptive")
        running: dict[Future, int] = {}
        try:
            next_index = 0
            while next_index < len(reliable_regions) or running:
                while next_index < len(reliable_regions) and len(running) < workers:
                    region = reliable_regions[next_index]
                    running[pool.submit(call_region, region)] = region.index
                    next_index += 1
                done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                for future in done:
                    running.pop(future)
                    index, rows, error, ignored = future.result()
                    requests_made += 1
                    overlap_ignored += ignored
                    if error:
                        issues.append(f"区域 {index}: {error}")
                        continue
                    _apply_rows(rows, primary_by_region[index], allowed_by_region[index], order, nodes, issues)
                    event("adaptive_region_done", index=index, nodes=len(nodes))
        except BaseException:
            # 失败/预算异常：置位共享 stop_event，阻止尚未真正 HTTP 的在途批继续发送；
            # 已开始的在途请求由 shutdown(wait=True) 收尾。
            stop_event.set()
            for pending in running:
                pending.cancel()
            raise
        finally:
            pool.shutdown(wait=True)

    run_reliable()
    for region in ambiguous_regions:
        if stop_event.is_set():
            raise RuntimeError("自适应规划已中止：不发送新请求")
        index, rows, error, ignored = call_region(region)
        requests_made += 1
        overlap_ignored += ignored
        if error:
            issues.append(f"区域 {index}: {error}")
            continue
        _apply_rows(rows, primary_by_region[index], allowed_by_region[index], order, nodes, issues)

    # 统一树检查（同时作为局部升级的触发依据）。
    provisional = {bid: {"block_id": bid, "role": node["role"], "parent_id": node.get("parent_id")}
                   for bid, node in nodes.items() if _role_level(node["role"]) is not None}
    tree = validate_hierarchy(candidates, provisional, {"nodes": nodes})
    tree_conflict_ids = {item["block_id"] for item in tree["issues"]}
    # “已确认目录”层级锚点：用于识别目录矛盾。
    confirmed_toc: dict[str, int] = {}
    for item in payload.get("matched_titles") or []:
        if item.get("level_source") == "confirmed" and isinstance(item.get("level"), int) and isinstance(item.get("block_id"), str):
            confirmed_toc[item["block_id"]] = int(item["level"])

    def region_suspicious(region: Region) -> list[str]:
        """升级触发：遗漏/uncertain/非法、树冲突、目录矛盾，而不只是“不在 nodes 里”。"""
        suspicious: list[str] = []
        for bid in region.primary_ids:
            node = nodes.get(bid)
            if node is None or node["role"] == "uncertain":
                suspicious.append(bid)
                continue
            if bid in tree_conflict_ids:
                suspicious.append(bid)
                continue
            expected = confirmed_toc.get(bid)
            level = _role_level(node["role"])
            if expected is not None and level is not None and level != expected:
                suspicious.append(bid)
                continue
            if expected is not None and level is None:
                suspicious.append(bid)
        return suspicious

    # 局部升级：只问可疑 ID（不整区重问），带原区只读上下文；受全局追加上限约束。
    upgrade_targets = [(region, region_suspicious(region)) for region in reliable_regions]
    upgrade_targets = [(region, suspicious) for region, suspicious in upgrade_targets if suspicious]
    local_upgrades = 0
    for region, suspicious in upgrade_targets:
        if stop_event.is_set():
            break
        if upgrade_requests + secondary_requests >= MAX_ADDITIONAL_PLANNING_REQUESTS:
            issues.append("追加规划请求已达上限，剩余异常交由第二遍复核与人工确认")
            break
        upgrade_requests += 1
        local_upgrades += 1
        event("adaptive_local_upgrade", index=region.index, missing=len(suspicious))
        request = build_region_request(payload, region, framework, include_overview=False,
                                       context_span=REGION_CONTEXT * 2, phase="adaptive-local-upgrade",
                                       only_ids=suspicious)
        allowed = set(suspicious) | {item["block_id"] for item in request["context"]} | set(region.anchors) | anchor_ids
        response = provider.complete(system=OUTLINE_PROMPT, user=json.dumps(request, ensure_ascii=False))
        rows, error = _parse_outline(response.content)
        requests_made += 1
        if error:
            issues.append(f"升级区域 {region.index}: {error}")
        else:
            _apply_rows(rows, set(suspicious), allowed, order, nodes, issues)

    # 遗漏/非法补查：按阅读顺序 <= ADAPTIVE_SECONDARY_WINDOW 切批，带相邻祖先/邻居，
    # 且只在追加上限内发送能承受的批次，其余留第二遍复核。
    all_primary = {item["block_id"] for item in candidates}
    order_list = [item["block_id"] for item in candidates]
    missing = sorted(all_primary - set(nodes), key=lambda bid: order.get(bid, 10 ** 9))
    second_round = 0
    if missing and not stop_event.is_set():
        remaining = MAX_ADDITIONAL_PLANNING_REQUESTS - (upgrade_requests + secondary_requests)
        batches = [missing[index:index + ADAPTIVE_SECONDARY_WINDOW]
                   for index in range(0, len(missing), ADAPTIVE_SECONDARY_WINDOW)]
        if remaining <= 0:
            issues.append(f"追加规划请求已达上限（{MAX_ADDITIONAL_PLANNING_REQUESTS}），{len(missing)} 个缺失候选交由第二遍复核")
        else:
            by_id = {item["block_id"]: item for item in candidates}
            chapter_anchors = _chapter_level_anchors(framework.reliable_anchors)
            for batch in batches[:remaining]:
                if stop_event.is_set():
                    break
                secondary_requests += 1
                second_round += 1
                event("adaptive_secondary", missing=len(batch))
                first_position = order.get(batch[0], 0)
                preceding = [a for a in chapter_anchors if order.get(a.block_id, 10 ** 9) < first_position]
                anchor = preceding[-1] if preceding else None
                if anchor is not None:
                    temp = Region(index=-1, anchor_block_id=anchor.block_id, anchor_level=anchor.level,
                                  primary_ids=list(batch), context_ids=[], reliable=False, anchors=[anchor.block_id])
                    request = build_region_request(payload, temp, framework, include_overview=False,
                                                   context_span=REGION_CONTEXT * 2, phase="adaptive-secondary")
                else:
                    request = {
                        "stage": "outline-plan-v2", "planning": "adaptive-secondary",
                        "document_title": payload.get("document_title", ""), "bbox_space": payload.get("bbox_space", "none"),
                        "root_convention": {"anchor_level": min((a.level for a in framework.reliable_anchors), default=None),
                                            "note": "补查：保持全局层级约定，只回答本批候选。"},
                        "region": {"index": -1, "reliable": False, "anchors": [], "anchor_block_id": None,
                                   "anchor_level": None, "reasons": ["补查：第一轮遗漏或非法"]},
                        "toc": [a.to_dict() for a in framework.reliable_anchors],
                        "candidates": [compact_candidate(by_id[bid]) for bid in batch],
                        "context": [compact_candidate(by_id[bid]) for bid in
                                    order_list[max(0, first_position - REGION_CONTEXT):first_position]],
                        "profile": {}, "previous_outline": [],
                    }
                allowed = set(batch) | {item["block_id"] for item in request.get("context", [])} | anchor_ids
                response = provider.complete(system=OUTLINE_PROMPT, user=json.dumps(request, ensure_ascii=False))
                rows, error = _parse_outline(response.content)
                requests_made += 1
                if error:
                    issues.append(f"补查批次 {second_round}: {error}")
                else:
                    _apply_rows(rows, set(batch), allowed, order, nodes, issues)
            still_missing = sorted(all_primary - set(nodes), key=lambda bid: order.get(bid, 10 ** 9))
            if still_missing:
                issues.append(f"补查后仍缺少 {len(still_missing)} 个候选的有效规划（交由第二遍复核与人工确认）")
    elif missing:
        issues.append(f"有 {len(missing)} 个候选未得到有效规划（已停止新请求或达追加上限）")

    provisional = {bid: {"block_id": bid, "role": node["role"], "parent_id": node.get("parent_id")}
                   for bid, node in nodes.items() if _role_level(node["role"]) is not None}
    tree = validate_hierarchy(candidates, provisional, {"nodes": nodes})
    for item in tree["issues"]:
        issues.append(f"{item['block_id']} 父子关系冲突：{item['reason']}")

    plan = {"profile": {}, "nodes": {bid: nodes[bid] for bid in (item["block_id"] for item in candidates) if bid in nodes},
            "issues": issues,
            "candidates": len(all_primary), "planned": sum(1 for node in nodes.values() if node["role"] != "uncertain")}
    meta = {
        "mode": "adaptive", "framework": framework.to_dict(), "fallback": False, "fallback_reason": "", "gate": gate,
        "regions": [region.to_dict() for region in regions],
        "reliable_regions": len(reliable_regions), "ambiguous_regions": len(ambiguous_regions),
        "main_requests": len(region_requests), "upgrade_requests": upgrade_requests,
        "secondary_requests": secondary_requests, "requests": requests_made,
        "additional_cap": MAX_ADDITIONAL_PLANNING_REQUESTS,
        "overlap_ignored": overlap_ignored, "second_round": second_round,
        "local_upgrades": local_upgrades,
        "estimated_chars": {"adaptive": adaptive_chars, "global": global_chars},
        "missing": len(all_primary - set(nodes)), "tree_issues": len(tree["issues"]),
    }
    event("adaptive_done", regions=len(regions), nodes=len(nodes), issues=len(issues))
    return plan, meta
