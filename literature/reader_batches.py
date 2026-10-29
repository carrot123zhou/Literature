"""AIFold 有界批处理：batch 规划、长 unit 句窗、逐 unit 检查点与串行提交的并发请求。

设计边界（与 `docs/READER_BACKEND.md` §4.8 同步）：

1. **有界请求**：单次请求 ≤ `MAX_BATCH_UNITS` 个 unit，估算输入 ≤ `MAX_INPUT_TOKENS`、
   估算输出 ≤ `MAX_OUTPUT_TOKENS`。超长单 unit **不截断正文**：按原始**句子**边界切窗；
   单句自己就超上限时按**同一句内的 token 子句**切（父句 ID 保留），再由**一次模型归并**
   （`consolidation_prompt`）写出整 unit 的短层——绝不用本地拼接冒充模型短层。
2. **逐 unit 检查点**：`reader/fold_checkpoints.json`，每 unit 一条带指纹的短层投影，
   指纹 = lane + 服务/模型 + 提示词版本 + unit 内容 + **实际消费的上下文** + 术语表。
   **每批完成即原子落盘**（只合并本实例改动的条目，不覆盖其他 lane/instance 的更新）。
3. **串行提交**：请求最多 `MAX_CONCURRENCY` 路并发（每路独立 provider 与独立 doc 副本，
   只有可克隆 provider 或显式 provider_factory 才允许并发），结果回主线程**串行**写入 ladder。
4. **批边界暂停**：暂停只在批之间检查；在途批跑完并提交后才挂起。
5. **整节摘要只写一次**：多批节/部分复用节在全部 unit 短层齐备后发**一次**章节摘要请求
   （输入只有 unit 短层）；takeaway 太多时按 token 预算分组做**有界分层摘要**。
6. **装置内容（索引/参考文献等）**由 `reader_apparatus.py` 分类决定，收据落在检查点文件里。

本模块不导入 `literature.reader`（避免循环导入）。只依赖标准库与 `third_party.textfold`
（+ `textfold_adapter` 的语言/提示词工具）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from third_party.textfold import ladder as tf_ladder
from third_party.textfold import langs as tf_langs
from third_party.textfold import llm as tf_llm
from third_party.textfold import tokens as tf_tokens

from .textfold_adapter import SOURCE_LOCALE, section_ladder_safe, upstream_lang

# ---- 有界请求的硬上限 ------------------------------------------------------
MAX_BATCH_UNITS = 12
MAX_INPUT_TOKENS = 6000
MAX_OUTPUT_TOKENS = 4000
MAX_CONCURRENCY = 2
# 句窗/子句窗上限：窗内句子数量与窗口数量都不是无限的。
MAX_WINDOW_SENTENCES = 24
MAX_WINDOW_PARTS = 64
CHECKPOINT_VERSION = 1
# 检查点文件软上限（派生数据，不是事实层）。项目内单本书远小于此；超出只淘汰其他陈旧节。
MAX_CHECKPOINT_SECTIONS = 4000
MAX_CHECKPOINT_BYTES = 32_000_000

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
# 子句切分点：句内标点（中英文），用于「单句超过输入上限」时的窗口边界。
_CLAUSE_RE = re.compile(r"[，,；;：:、。！？!?]")

# 派生 lane 的译文长度保守系数（见 estimate_unit_output_tokens）
_TARGET_TEXT_RATIO = {"cjk": 2.0, "latin": 1.6}


# ---------------------------------------------------------------------------
# token 估算（只用于装箱边界，不用于计费）
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """粗略 token 估算：CJK 每字 1 token，其余 4 字符 1 token（只决定 batch 边界）。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def estimate_unit_output_tokens(
    unit: dict[str, Any], *, lane: str = "source", locale: str = SOURCE_LOCALE,
    atoms: dict[str, Any] | None = None,
) -> int:
    """单个 unit 的模型输出估算（只用于 batch 边界，不参与计费）。

    - 原文 lane：模型只写短层 + evidence 下标，**不输出整句** → 200 token/unit 足够；
    - 派生 lane：`ev_sentences` 要逐条译出被引证据句的**整段文本**（不是短层），按原文
      token × 保守目标语系数估算（CJK 2.0 / 拉丁 1.6）+ 短层与 JSON 开销。
    """
    if lane != "derive":
        return 200
    sentences = unit.get("sents") or []
    toks = unit.get("toks") or []
    lad = unit.get("lad") or {}
    slots = [
        int(value) for value in (lad.get("ev") or [])
        if isinstance(value, (int, float)) and 0 <= int(value) < len(sentences)
    ][:3] or [0]
    text_tokens = 0
    for slot in slots:
        if slot >= len(sentences):
            continue
        start, end = sentences[slot]
        text_tokens += estimate_tokens(tf_tokens.plain(toks[start:end], atoms or {}))
    cjk = tf_langs.cjk(upstream_lang(locale)) if locale != SOURCE_LOCALE else False
    return 120 + int(text_tokens * _TARGET_TEXT_RATIO["cjk" if cjk else "latin"])


def is_timeout_error(error: BaseException | str) -> bool:
    """请求超时（已计费/可能在途）：**不重试**——重发可能重复计费且大概率再超时。"""
    name = type(error).__name__ if isinstance(error, BaseException) else ""
    text = f"{name} {error}".lower()
    return "timeout" in text or "timed out" in text or "超时" in text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 活动日志（generate(activity=...) 的唯一出口；观测失败绝不影响折叠）
# ---------------------------------------------------------------------------

# 内部阶段名 → 归一化 stage（UI/runner 契约：source / derive / waiting / generating /
# validating / saving）。未列出的名字按原样给出，由消费方忽略。
_STAGE_ALIASES = {
    "plan": "waiting",
    "source_fold": "source",
    "target_derive": "derive",
    "section_summary": "generating",
    "consolidate": "generating",
    "apparatus": "validating",
    "finalize": "saving",
    "error": "error",
}


class ActivityLog:
    """阶段/节/批/请求四级事件出口；回调异常被吞掉（观测不是业务流程的一部分）。

    事件既保留内部审计名（`event`），也带**归一化字段**供 UI/runner 直接消费：
    `section_id`、`batch_index`、`batch_total`、`request_started_at`、`last_response_at`、
    `stage`（source/derive/waiting/generating/validating/saving）、`pending_units`。
    """

    def __init__(self, callback: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.callback = callback
        self._t0 = time.monotonic()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.callback is not None

    @staticmethod
    def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        if "section" in payload and "section_id" not in payload:
            normalized["section_id"] = payload["section"]
        if "started_at" in payload and "request_started_at" not in payload:
            normalized["request_started_at"] = payload["started_at"]
        if "finished_at" in payload and "last_response_at" not in payload:
            normalized["last_response_at"] = payload["finished_at"]
        stage = payload.get("stage")
        if isinstance(stage, str) and stage in _STAGE_ALIASES:
            normalized["stage"] = _STAGE_ALIASES[stage]
        if "batch_index" not in normalized and "batch_total" not in normalized:
            normalized.setdefault("batch_index", payload.get("batch_index", 0))
            normalized.setdefault("batch_total", payload.get("batch_total", 0))
        return normalized

    def emit(self, event: str, **fields: Any) -> None:
        if self.callback is None:
            return
        payload = self._normalize({
            "event": event, "ts": _now_iso(),
            "elapsed_seconds": round(time.monotonic() - self._t0, 3), **fields,
        })
        with self._lock:
            try:
                self.callback(payload)
            except Exception:  # noqa: BLE001 - 观测失败不影响折叠
                pass

    def stage(self, name: str, phase: str, **fields: Any) -> None:
        self.emit("stage", stage=name, internal_stage=name, phase=phase, **fields)

    def request(self, *, started_at: str, finished_at: str, duration_seconds: float,
                **fields: Any) -> None:
        self.emit("request", started_at=started_at, finished_at=finished_at,
                  duration_seconds=round(duration_seconds, 3), **fields)


# ---------------------------------------------------------------------------
# batch 计划
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchPlan:
    """一次模型请求的计划：prompt 已构建，边界（unit 数/token 估算）已核。"""

    units: tuple[str, ...]
    window: dict[str, Any] | None
    system: str
    user: str
    input_tokens: int
    output_tokens: int
    label: str = ""
    attempt: int = 1

    @property
    def unit_id(self) -> str:
        return str((self.window or {}).get("unit") or "")

    def describe(self, section_id: str = "", *, batch_index: int = 0,
                 batch_total: int = 0) -> dict[str, Any]:
        """活动日志字段：批次可观测（哪节、几个 unit、是否句窗、估算 token）。"""
        fields: dict[str, Any] = {
            "section": section_id, "section_id": section_id,
            "batch": self.label, "batch_index": batch_index, "batch_total": batch_total,
            "attempt": self.attempt, "units": len(self.units), "unit_ids": list(self.units),
            "input_tokens_est": self.input_tokens, "output_tokens_est": self.output_tokens,
        }
        if self.window:
            fields["window"] = dict(self.window)
        return fields


def _make_plan(units: Sequence[str], measure: tuple[str, str, int, int], *, label: str,
               window: dict[str, Any] | None = None, attempt: int = 1) -> BatchPlan:
    system, user, input_tokens, output_tokens = measure
    return BatchPlan(tuple(units), window, system, user, input_tokens, output_tokens,
                     label=label, attempt=attempt)


def plan_batches(
    unit_ids: Sequence[str],
    *,
    build_prompt: Callable[[Sequence[str]], tuple[str, str]],
    unit_output: Callable[[str], int] = estimate_unit_output_tokens,
    max_units: int = MAX_BATCH_UNITS,
    max_input_tokens: int = MAX_INPUT_TOKENS,
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    attempt: int = 1,
) -> list[BatchPlan]:
    """贪心装箱：≤ `max_units` 个 unit，估算输入/输出都在上限内。

    单个 unit 自己就超输入的批照常返回（`input_tokens > max_input_tokens`）——调用方用
    `plan_unit_windows` 换成句窗批，**不截断正文**。
    """

    def measure(units: Sequence[str]) -> tuple[str, str, int, int]:
        system, user = build_prompt(units)
        return (system, user, estimate_tokens(system) + estimate_tokens(user),
                sum(int(unit_output(uid)) for uid in units))

    plans: list[BatchPlan] = []
    current: list[str] = []
    current_measure: tuple[str, str, int, int] | None = None

    def flush() -> None:
        nonlocal current, current_measure
        if not current:
            return
        plans.append(_make_plan(current, current_measure or measure(current),
                                label=f"b{len(plans) + 1}", attempt=attempt))
        current, current_measure = [], None

    for uid in unit_ids:
        if not current:
            current = [uid]
            current_measure = None
            continue
        candidate = measure(current + [uid])
        _, _, input_tokens, output_tokens = candidate
        if (len(current) + 1 > max_units or input_tokens > max_input_tokens
                or output_tokens > max_output_tokens):
            flush()
            current = [uid]
            current_measure = None
            continue
        current.append(uid)
        current_measure = candidate
    flush()
    return plans


def plan_unit_windows(
    unit_id: str,
    *,
    sentence_count: int,
    build_prompt: Callable[[int, int], tuple[str, str]],
    output_tokens: int,
    max_input_tokens: int = MAX_INPUT_TOKENS,
    max_sentences: int = MAX_WINDOW_SENTENCES,
    attempt: int = 1,
) -> list[BatchPlan]:
    """超长单 unit 的**句窗**计划：窗口边界只落在原始句边界，合起来覆盖全部句子。

    `build_prompt(start, end)` 返回该窗口的 `(system, user)`。单句自己就超上限时返回一个
    覆盖该单句的窗口（`input_tokens > max_input_tokens`），由调用方改用
    `plan_clause_windows` 在同一句内按 token 子句切（父句 ID 保留，绝不截断丢字）。
    """
    plans: list[BatchPlan] = []
    start = 0
    total = max(1, int(sentence_count))
    while start < total:
        chosen: tuple[str, str, int, int] | None = None
        end = start + 1
        while end <= total and end - start <= max_sentences:
            system, user = build_prompt(start, end)
            tokens = estimate_tokens(system) + estimate_tokens(user)
            if tokens <= max_input_tokens:
                chosen = (system, user, tokens, end)
                end += 1
                continue
            if chosen is None:   # 单句就超上限：交给子句窗口
                chosen = (system, user, tokens, end)
            break
        if chosen is None:
            break
        system, user, tokens, end = chosen
        window = {"unit": unit_id, "sents_start": start, "sents_end": end}
        plans.append(BatchPlan((unit_id,), window, system, user, tokens, int(output_tokens),
                               label=f"w{start}-{end}", attempt=attempt))
        start = end
    return plans


def plan_clause_windows(
    unit_id: str,
    *,
    sentence_index: int,
    sentence_count: int,
    clause_tokens: Sequence[tuple[int, int]],
    build_prompt: Callable[[int, int], tuple[str, str]],
    output_tokens: int,
    max_input_tokens: int = MAX_INPUT_TOKENS,
    max_parts: int = MAX_WINDOW_PARTS,
    attempt: int = 1,
) -> list[BatchPlan]:
    """单句超上限时的**子句窗口**：窗口覆盖整句 token（不丢字），父句 ID 不变。

    窗口终点优先落在子句边界（句内标点）上，避免把半句话单独喂给模型；预算实在放不下
    一个完整子句时才在子句内部按 token 二分切点。每个窗口都标记 `parent_sent`，
    归并时按父句重建 evidence。
    """
    spans = [(int(a), int(b)) for a, b in clause_tokens if int(b) > int(a)]
    if not spans:
        return []
    boundaries = {end for _, end in spans}
    start_token, end_token = spans[0][0], spans[-1][1]
    plans: list[BatchPlan] = []
    cursor = start_token
    while cursor < end_token and len(plans) < max_parts:
        low, high = cursor + 1, end_token
        best: tuple[str, str, int, int] | None = None
        while low <= high:
            middle = (low + high) // 2
            system, user = build_prompt(cursor, middle)
            tokens = estimate_tokens(system) + estimate_tokens(user)
            if tokens <= max_input_tokens:
                best = (system, user, tokens, middle)
                low = middle + 1
            else:
                high = middle - 1
        if best is None:   # 单个 token 都超预算：不截断，单独成窗（活动日志可见）
            system, user = build_prompt(cursor, cursor + 1)
            best = (system, user, estimate_tokens(system) + estimate_tokens(user), cursor + 1)
        system, user, tokens, window_end = best
        inner = [boundary for boundary in boundaries if cursor < boundary < window_end]
        if inner:
            window_end = max(inner)
            system, user = build_prompt(cursor, window_end)
            tokens = estimate_tokens(system) + estimate_tokens(user)
        window = {
            "unit": unit_id, "parent_sent": int(sentence_index),
            "sents_start": int(sentence_index), "sents_end": int(sentence_index) + 1,
            "tok_start": cursor, "tok_end": window_end, "clause": True,
        }
        plans.append(BatchPlan((unit_id,), window, system, user, tokens, int(output_tokens),
                               label=f"c{cursor}-{window_end}", attempt=attempt))
        cursor = window_end
    return plans


def unit_clause_spans(unit: dict[str, Any], sentence_index: int) -> list[tuple[int, int]]:
    """一句内部的子句 token 区间（在句内标点处切；无标点时按 token 均分）。"""
    sentences = unit.get("sents") or []
    if not 0 <= sentence_index < len(sentences):
        return []
    start, end = int(sentences[sentence_index][0]), int(sentences[sentence_index][1])
    toks = unit.get("toks") or []
    spans: list[tuple[int, int]] = []
    cursor = start
    for index in range(start, min(end, len(toks))):
        if _CLAUSE_RE.search(str(toks[index].get("t") or "")) and index + 1 > cursor:
            spans.append((cursor, index + 1))
            cursor = index + 1
    if cursor < end:
        spans.append((cursor, end))
    # 极长无标点句：按约 MAX_INPUT_TOKENS/2 token 切片，保证窗口可装箱（不丢字）
    limit = max(64, MAX_INPUT_TOKENS // 2)
    pieces: list[tuple[int, int]] = []
    for a, b in spans:
        while b - a > limit:
            pieces.append((a, a + limit))
            a += limit
        pieces.append((a, b))
    return [(a, b) for a, b in pieces if b > a]


# ---------------------------------------------------------------------------
# doc 副本与句窗/子句窗代理（并发隔离 / 不截断正文）
# ---------------------------------------------------------------------------


def _clone_unit(unit: dict[str, Any]) -> dict[str, Any]:
    """unit 的可写副本：`toks`/`sents` 共享只读引用（折叠只读它们），`lad`/`tr` 深拷贝。"""
    cloned = dict(unit)
    cloned["lad"] = json.loads(json.dumps(unit.get("lad"))) if unit.get("lad") is not None else None
    if unit.get("tr"):
        cloned["tr"] = json.loads(json.dumps(unit["tr"]))
    return cloned


def make_proxy_doc(
    doc: dict[str, Any], chunk: dict[str, Any], unit_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """batch 局部 doc/chunk 副本：提示词里的 `[U{n}]` 按**本批**从 0 编号。

    - chunk 的 `units` 换成批内 unit；`tr`/`lad` 深拷贝，`apply_reply`/`apply_derived_reply`
      的写入不会漏进真实 ladder（真实写入只在主线程串行提交）；
    - `prompt_for` 的 abstract 上下文读 `doc["chunks"][0]` 的 unit，若它不是本节则保留这些
      unit 的只读引用（否则 abstract 文本随 batch 变化，甚至 KeyError）。
    """
    keep: dict[str, dict[str, Any]] = {}
    chunks = doc.get("chunks") or []
    if chunks:
        first = chunks[0]
        if first.get("id") != chunk.get("id"):
            for uid in first.get("units") or []:
                if uid in (doc.get("units") or {}):
                    keep[uid] = doc["units"][uid]     # 只读引用：本批不会写这些 unit
    for uid in unit_ids:
        unit = (doc.get("units") or {}).get(uid)
        if unit is not None:
            keep[uid] = _clone_unit(unit)
    proxy_doc = {**doc, "units": keep}
    proxy_chunk = {
        **chunk,
        "units": list(unit_ids),
        "tr": json.loads(json.dumps(chunk.get("tr") or {})),
        "lad": json.loads(json.dumps(chunk.get("lad"))) if chunk.get("lad") is not None else None,
    }
    return proxy_doc, proxy_chunk


def window_unit(unit: dict[str, Any], *, start: int, end: int) -> dict[str, Any]:
    """句窗代理：`toks`/`sents` 以窗口起点重新计数，`lad`/`tr` 的 ev 映射到窗口句空间。"""
    sentences = unit.get("sents") or []
    if not sentences:
        return dict(unit)
    start = max(0, min(int(start), len(sentences) - 1))
    end = max(start + 1, min(int(end), len(sentences)))
    tok_start = int(sentences[start][0])
    tok_end = int(sentences[end - 1][1])
    proxy = {key: value for key, value in unit.items() if key not in ("toks", "sents", "lad", "tr")}
    proxy["toks"] = [unit["toks"][index] for index in range(tok_start, min(tok_end, len(unit["toks"])))]
    proxy["sents"] = [[int(a) - tok_start, int(b) - tok_start] for a, b in sentences[start:end]]
    proxy["source_tok_start"] = tok_start
    proxy["sents_start"] = start
    proxy["sents_end"] = end
    proxy["lad"] = _window_lad(unit.get("lad"), start, end)
    if unit.get("tr"):
        proxy["tr"] = {
            lang: {**value, "lad": _window_lad(value.get("lad"), start, end)}
            for lang, value in unit["tr"].items() if isinstance(value, dict)
        }
    return proxy


def window_unit_clause(
    unit: dict[str, Any], *, sentence_index: int, tok_start: int, tok_end: int,
) -> dict[str, Any]:
    """子句窗代理：单句内的 token 子区间，父句 ID 保留（`parent_sent`）。"""
    proxy = {key: value for key, value in unit.items() if key not in ("toks", "sents", "lad", "tr")}
    toks = unit.get("toks") or []
    tok_start = max(0, int(tok_start))
    tok_end = max(tok_start + 1, min(int(tok_end), len(toks)))
    proxy["toks"] = [toks[index] for index in range(tok_start, tok_end)]
    proxy["sents"] = [[0, tok_end - tok_start]]
    proxy["source_tok_start"] = tok_start
    proxy["sents_start"] = int(sentence_index)
    proxy["sents_end"] = int(sentence_index) + 1
    proxy["parent_sent"] = int(sentence_index)
    proxy["clause"] = True
    if unit.get("lad") is not None:
        proxy["lad"] = {**(unit.get("lad") or {}), "ev": [0]}
    if unit.get("tr"):
        proxy["tr"] = {
            lang: {**value, "lad": ({**(value.get("lad") or {}), "ev": [0]}
                                    if isinstance(value.get("lad"), dict) else value.get("lad"))}
            for lang, value in unit["tr"].items() if isinstance(value, dict)
        }
    return proxy


def _window_lad(lad: Any, start: int, end: int) -> dict[str, Any] | None:
    """把整 unit 的 lad 映射到窗口：ev 只保留窗口内句并平移到 0 起；空则取窗口前 1-2 句。"""
    if not isinstance(lad, dict):
        return lad
    inner = [int(s) for s in (lad.get("ev") or []) if isinstance(s, (int, float))]
    local = [s for s in inner if start <= s < end]
    if not local:
        span = end - start
        local = [start] if span <= 1 else [start, start + 1]
    return {
        **lad,
        "ev": sorted({int(s) - start for s in local}),
        "brief": list(lad.get("brief") or []),
        "tn": int(lad.get("tn") or 0),
        "topic": list(lad.get("topic") or []),
    }


# ---------------------------------------------------------------------------
# 超长 unit 的模型归并（窗口短层 → 一个整 unit 短层；绝不用本地拼接冒充模型）
# ---------------------------------------------------------------------------


def _window_lines(doc: dict[str, Any], unit: dict[str, Any], windows: Sequence[dict[str, Any]],
                  *, limit: int) -> list[str]:
    """窗口短层的可读清单（归并请求的输入；超预算时只保留 takeaway）。"""
    atoms = doc.get("atoms") or {}
    lines: list[str] = []
    for index, part in enumerate(windows, 1):
        lad = part.get("lad") or {}
        brief = list(lad.get("brief") or [])
        tn = max(0, min(int(lad.get("tn") or 0), len(brief)))
        take = tf_tokens.plain(brief[:tn], atoms)
        reason = tf_tokens.plain(brief[tn + 1: len(brief) - 1], atoms) if len(brief) > tn + 1 else ""
        span = f"{part.get('sents_start')}-{part.get('sents_end')}"
        if lad.get("brief_sentences"):
            sentences = " / ".join(str(s) for s in lad.get("brief_sentences") or [])
            lines.append(f"[W{index}] sentences {span}: {take} | {reason} | backing: {sentences}"[:limit])
        else:
            lines.append(f"[W{index}] sentences {span}: {take} | {reason}"[:limit])
    return lines


def consolidation_prompt(
    doc: dict[str, Any], chunk: dict[str, Any], unit: dict[str, Any],
    windows: Sequence[dict[str, Any]], *, lane: str, locale: str = SOURCE_LOCALE,
) -> tuple[str, str]:
    """超长 unit 的**唯一一次**归并请求：输入是窗口短层 + 原句清单，输出一个整 unit 短层。

    这样既不违反短层预算（14 词/40 字）也不把本地拼接标成模型产物；evidence 必须选**原始
    句号**（子句窗沿用父句号），派生 lane 的 `ev_sentences` 由各窗口已译证据按父槽合并。
    """
    atoms = doc.get("atoms") or {}
    sentences = unit.get("sents") or []
    toks = unit.get("toks") or []
    is_derive = lane == "derive"
    code = upstream_lang(locale) if is_derive else "en"
    cjk = tf_langs.cjk(code) if is_derive else bool(doc.get("is_cjk"))
    budget = tf_langs.budget(code) if is_derive else tf_ladder.budgets_for(doc)
    unit_word = budget["unit"] if is_derive else ("characters" if cjk else "words")
    numbered = []
    for index, (start, end) in enumerate(sentences):
        text = tf_tokens.plain(toks[start:end], atoms)
        if text.strip():
            numbered.append(f"{index}: {text}"[:400])
    if not numbered:
        return "", ""
    lines = _window_lines(doc, unit, windows, limit=400)
    if is_derive:
        prompt = tf_ladder.kinds.adapt(f"""The short levels of one long unit were written in windows. Consolidate them into ONE set in {tf_langs.name(code)}. Do not translate the unit again.

Source sentences (numbered; evidence must point at these numbers):
{chr(10).join(numbered)}

Windows:
{chr(10).join(lines)}

Return JSON: {{"takeaway": "...", "reason": "...", "topic": "...", "evidence": [0]}} with:
- takeaway: ONE sentence of at most {budget['takeaway']} {unit_word} covering what the whole unit establishes (not a list of the windows);
- reason: ONE more sentence of at most {budget['reason']} {unit_word}, or "";
- topic: at most {budget['topic']} {unit_word}, verbatim inside the takeaway;
- evidence: 1 to 3 source sentence numbers that back the takeaway.""", doc.get("meta") or {})
    else:
        prompt = tf_ladder.kinds.adapt(f"""The short levels of one long unit were written in windows. Consolidate them into ONE set. The unit's text is unchanged.

Source sentences (numbered; evidence must point at these numbers):
{chr(10).join(numbered)}

Windows:
{chr(10).join(lines)}

Return JSON: {{"takeaway": "...", "role": "claim", "reason": "...", "topic": "...", "evidence": [0]}} with:
- takeaway: ONE sentence of at most {budget['takeaway']} {unit_word} stating what the whole unit establishes (not a list of the windows);
- role: one of context, gap, claim, method, setup, result, analysis, limitation;
- reason: ONE more sentence of at most {budget['reason']} {unit_word}, or "";
- topic: at most {budget['topic']} {unit_word}, verbatim inside the takeaway;
- evidence: 1 to 3 sentence numbers above that back the takeaway.""", doc.get("meta") or {})
    return tf_llm.system_for(doc.get("meta") or {}), prompt


def apply_consolidation(
    doc: dict[str, Any], unit: dict[str, Any], payload: Any, windows: Sequence[dict[str, Any]],
    *, lane: str, locale: str = SOURCE_LOCALE, model: str,
) -> dict[str, Any]:
    """把归并回复落成整 unit 的短层（source: lad；derive: tr lad）。

    派生 lane 的证据句译文**按父槽合并**窗口已译句（同一原句被切成多个子句窗时不重复翻译）。
    """
    if not isinstance(payload, dict):
        return {}
    atoms = doc.get("atoms") or {}
    sentences = unit.get("sents") or []
    take_text = tf_ladder.clean(payload.get("takeaway"))
    if not take_text:
        return {}
    raw_evidence = [int(value) for value in (payload.get("evidence") or [])
                    if isinstance(value, (int, float)) and 0 <= int(value) < len(sentences)]
    evidence = sorted(set(raw_evidence))[:3] or [0]
    reason_text = tf_ladder.clean(payload.get("reason"))
    if lane == "derive":
        code = upstream_lang(locale)
        cjk = tf_langs.cjk(code)
        dot = tf_langs.stop(code)
        take = tf_tokens.tokenize_text(take_text)
        brief = [dict(token) for token in take]
        if reason_text:
            brief.append({"t": dot} if cjk else {"t": dot, "s": 1})
            brief += tf_tokens.tokenize_text(reason_text)
        brief.append({"t": dot})
        if take:
            take[-1].pop("s", None)
        tn = len(take)
        # 每个父槽的已译证据句：按窗口顺序拼接同一父句的子句片段。
        by_slot: dict[int, list[str]] = {}
        for part in windows:
            lad = part.get("lad") or {}
            base = int(part.get("parent_sent") if part.get("parent_sent") is not None
                       else (part.get("sents_start") or 0))
            # 窗口 lad 的 ev 是**窗口局部**下标（`window_unit` 把 sents 重基到 0）：
            # 进归并前必须搬回父句坐标，否则第二窗的 ev=[0] 会被当成父句 0（evidence 丢失）。
            raw_slots = [int(s) for s in (lad.get("ev") or []) if isinstance(s, (int, float))]
            clause = bool(part.get("clause") or part.get("parent_sent") is not None)
            # 窗口上是局部坐标（有 < base 的下标或子句窗）→ 平移；已经是父坐标就别重复加 base。
            needs_shift = clause or (bool(raw_slots) and min(raw_slots) < base)
            slots = [base for _ in raw_slots] if clause else (
                [slot + base for slot in raw_slots] if needs_shift else raw_slots
            )
            texts = [str(x) for x in (lad.get("brief_sentences") or [])]
            for slot, text in zip(slots, texts):
                if text.strip():
                    by_slot.setdefault(slot, []).append(text.strip())
        translated = [" ".join(by_slot.get(slot, [])) for slot in evidence]
        if not all(translated):
            return {}
        topic = tf_ladder.topic_tokens(
            tf_ladder.clean(payload.get("topic")), brief, tn,
            {"toks": [], "sents": []}, atoms, [], tf_langs.budget(code)["topic"],
        )
        return {"brief": brief, "tn": tn, "topic": topic,
                "role": str(payload.get("role") or "context"), "model": model,
                "ev": evidence, "brief_sentences": translated, "brief_source": "derived"}
    cjk = bool(doc.get("is_cjk"))
    dot = "。" if cjk else "."
    take = tf_tokens.tokenize_text(take_text)
    brief = [dict(token) for token in take]
    if reason_text:
        brief.append({"t": dot} if cjk else {"t": dot, "s": 1})
        brief += tf_tokens.tokenize_text(reason_text)
    brief.append({"t": dot})
    if take:
        take[-1].pop("s", None)
    tn = len(take)
    role = str(payload.get("role") or unit.get("role") or "context")
    if role not in ("context", "gap", "claim", "method", "setup", "result", "analysis", "limitation"):
        role = "context"
    topic = tf_ladder.topic_tokens(
        tf_ladder.clean(payload.get("topic")), brief, tn, unit, atoms, evidence,
        (tf_ladder.CJK_BUDGET if cjk else tf_ladder.BUDGET)["topic"],
    )
    return {"role": role, "ev": evidence, "brief": brief, "tn": tn, "topic": topic, "model": model}


# ---------------------------------------------------------------------------
# 多批节的唯一一次章节摘要（输入只有 unit 短层，不含正文）
# ---------------------------------------------------------------------------


def section_summary_points(
    doc: dict[str, Any], chunk: dict[str, Any], *, locale: str = SOURCE_LOCALE,
) -> list[str]:
    """本节各 unit 的短层 takeaway（摘要请求的唯一输入；不含正文）。"""
    atoms = doc.get("atoms") or {}
    is_trans = locale != SOURCE_LOCALE
    points: list[str] = []
    for uid in chunk.get("units") or []:
        unit = (doc.get("units") or {}).get(uid) or {}
        if is_trans:
            lad = ((unit.get("tr") or {}).get(locale) or {}).get("lad") or {}
        else:
            lad = unit.get("lad") or {}
        take = tf_tokens.plain((lad.get("brief") or [])[: int(lad.get("tn") or 0)], atoms)
        if take.strip():
            points.append(take.strip())
    return points


def _summary_text(
    doc: dict[str, Any], chunk: dict[str, Any], points: Sequence[str], *, locale: str,
    group: bool = False,
) -> tuple[str, str]:
    is_trans = locale != SOURCE_LOCALE
    title = str(((chunk.get("tr") or {}).get(locale) or {}).get("title") or chunk.get("title") or "")
    body = "\n".join(f"- {point}" for point in points)
    if is_trans:
        code = upstream_lang(locale)
        name = tf_langs.name(code)
        budget = tf_langs.budget(code)
        word = budget["unit"]
        header = ("These are condensed summaries of consecutive groups of the section's units."
                  if group else "These are the section's unit takeaways in reading order.")
        prompt = tf_ladder.kinds.adapt(f"""A section's zoom levels are already written in {name}, unit by unit. {header}

Title: {title}

{body}

Write the section's own summary in {name}: the one line a reader sees when zoomed out of the whole section.
- takeaway: ONE sentence of at most {budget['section']} {word}, stating what the section establishes; no new facts.
- topic: at most {budget['topic']} {word}, copied VERBATIM from inside that takeaway.
- {tf_langs.punctuation_rule(code)}

Return JSON: {{"section": {{"takeaway": "...", "topic": "..."}}}}""", doc.get("meta") or {})
    else:
        budget = tf_ladder.budgets_for(doc)
        word = "characters" if budget is tf_ladder.CJK_BUDGET else "words"
        header = ("These are condensed summaries of consecutive groups of the section's units."
                  if group else "These are the section's unit takeaways in reading order.")
        prompt = tf_ladder.kinds.adapt(f"""A section's zoom levels are already written, unit by unit. {header}

Title: {title}

{body}

Write the section's own summary: the one line a reader sees when zoomed out of the whole section.
- takeaway: ONE sentence of at most {budget['section']} {word}, stating what this section establishes; no new facts.
- topic: at most {budget['topic']} {word}, copied VERBATIM from inside that takeaway.

Return JSON: {{"section": {{"takeaway": "...", "topic": "..."}}}}""", doc.get("meta") or {})
    return tf_llm.system_for(doc.get("meta") or {}), prompt


def section_summary_prompt(
    doc: dict[str, Any], chunk: dict[str, Any], *, locale: str = SOURCE_LOCALE,
) -> tuple[str, str]:
    """单次章节摘要请求（points 太多时由调用方改用 `plan_summary_steps` 分层）。"""
    points = section_summary_points(doc, chunk, locale=locale)
    if not points:
        return "", ""
    return _summary_text(doc, chunk, points, locale=locale)


def plan_summary_steps(
    doc: dict[str, Any], chunk: dict[str, Any], *,
    locale: str = SOURCE_LOCALE, max_input_tokens: int = MAX_INPUT_TOKENS,
) -> list[dict[str, Any]]:
    """章节摘要的分层计划：单次输入超预算时先按预算分组摘要，再汇总各组摘要。

    返回步骤列表：`[{"kind": "final"|"group", "points": [...], "system","user"}]`。
    `final` 步骤的输出写成章节短层；`group` 步骤的输出作为 final 的输入点。
    """
    points = section_summary_points(doc, chunk, locale=locale)
    if not points:
        return []
    system, user = _summary_text(doc, chunk, points, locale=locale)
    if estimate_tokens(system) + estimate_tokens(user) <= max_input_tokens:
        return [{"kind": "final", "points": list(points), "system": system, "user": user}]
    budget = max(800, max_input_tokens // 2)
    groups: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for point in points:
        tokens = estimate_tokens(point) + 8
        if current and current_tokens + tokens > budget:
            groups.append(current)
            current, current_tokens = [], 0
        current.append(point)
        current_tokens += tokens
    if current:
        groups.append(current)
    steps: list[dict[str, Any]] = []
    for group in groups:
        group_system, group_user = _summary_text(doc, chunk, group, locale=locale, group=True)
        steps.append({"kind": "group", "points": group, "system": group_system, "user": group_user})
    if len(groups) > 1:
        placeholder = [f"（第 {index} 组摘要）" for index in range(1, len(groups) + 1)]
        final_system, final_user = _summary_text(doc, chunk, placeholder, locale=locale, group=True)
        steps.append({"kind": "final", "points": placeholder, "system": final_system,
                      "user": final_user, "compose": True})
    return steps


def apply_section_summary(
    doc: dict[str, Any], chunk: dict[str, Any], payload: Any, *, locale: str = SOURCE_LOCALE,
    problems: list[str] | None = None,
) -> bool:
    """把章节摘要写进 `chunk["lad"]`（原文层）或 `chunk["tr"][locale]["lad"]`（译文层）。

    类型边界：`section.takeaway/topic` 非 str 时只当缺失（见 `textfold_adapter.section_ladder_safe`），
    不让畸形回复抛 TypeError 拖死整节/整批；单一缺陷记到 `problems`（可审计），不猜造内容。
    """
    if not isinstance(payload, dict):
        return False
    section = payload.get("section")
    raw = section if isinstance(section, (dict, str)) else payload
    if locale == SOURCE_LOCALE:
        lad, problem = section_ladder_safe(raw, 3, doc, chunk)
    else:
        lad, problem = section_ladder_safe(
            raw, tf_langs.budget(upstream_lang(locale))["topic"],
        )
    if problem and problems is not None:
        problems.append(problem)
    if not lad:
        return False
    if locale == SOURCE_LOCALE:
        chunk["lad"] = lad
        return True
    target = chunk.setdefault("tr", {}).setdefault(locale, {})
    if not isinstance(target, dict):
        target = {}
        chunk["tr"][locale] = target
    target["lad"] = lad
    return True


def finalize_chunk(doc: dict[str, Any], chunk: dict[str, Any]) -> None:
    """整节 unit 短层齐备后的**唯一一次**收尾（背景降级 + 断句承接）。

    多批节不能在批内做这两步：`fold_background` 的连续背景计数与 `dress` 的上一句承接
    都依赖整节邻居，只看子集会把正常行误降级、把句尾接错（subset dress damage）。
    """
    tf_ladder.fold_background(chunk, doc)
    tf_ladder.dress(chunk, doc)


# ---------------------------------------------------------------------------
# 逐 unit 检查点
# ---------------------------------------------------------------------------

_CHECKPOINT_ITEM_KEYS = (
    "id", "kind", "role", "takeaway", "key", "topic", "evidence", "brief", "brief_source",
    "source", "suppressed", "suppress_reason", "type", "page_index", "fallback", "error",
)


def checkpoint_item(stored_unit: dict[str, Any]) -> dict[str, Any]:
    """把存盘 unit 投影裁剪成检查点条目：去掉可由 toks 重算的 `full`/`sentences`。"""
    return {key: stored_unit[key] for key in _CHECKPOINT_ITEM_KEYS if key in stored_unit}


def checkpoint_unit_fingerprint(
    *, lane: str, service_id: str, model: str, prompt_version: int, uid: str,
    content: str, context: str, term: str = "",
) -> str:
    """unit 检查点指纹：lane + **服务/模型** + 提示词版本 + 内容 + 上下文 + 术语表。

    `service_id` 必须在内：同一模型名挂在不同服务上（自建/官方/不同网关）产物不同，
    只比 `model` 会把换服务的短层错误复用。
    """
    payload = {
        "version": CHECKPOINT_VERSION, "lane": lane,
        "service_id": service_id or "", "model": model or "",
        "prompt_version": int(prompt_version), "unit": uid,
        "content": content or "", "context": context or "", "term": term or "",
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


class UnitCheckpoints:
    """`reader/fold_checkpoints.json`：逐 unit 短层检查点 + 章节摘要 + 装置分类收据。

    写入采用**增量合并**：本实例只记录自己改过的条目（`_scratch`），`save()` 在文件锁内
    重读磁盘上的最新内容再合并写回——并发/多实例（原文 lane 与译文 lane 各自一个实例）
    不会互相覆盖对方已落盘的 unit。文件可删可重建（不是事实层）。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = _path_lock(self.path)
        self._scratch: dict[str, dict[str, dict[str, Any]]] = {}
        self._dirty = False
        self._cached: dict[str, Any] | None = None
        self._stamp: tuple[int, int, int] | None = None

    # -- 读 ---------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            stamp = None
        if self._cached is not None and stamp == self._stamp:
            return self._cached
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self._cached = {"version": CHECKPOINT_VERSION, "sections": {}}
            self._stamp = stamp
            return self._cached
        if not isinstance(raw, dict) or raw.get("version") != CHECKPOINT_VERSION:
            self._cached = {"version": CHECKPOINT_VERSION, "sections": {}}
            self._stamp = stamp
            return self._cached
        sections = raw.get("sections")
        self._cached = {"version": CHECKPOINT_VERSION,
                        "sections": sections if isinstance(sections, dict) else {}}
        self._stamp = stamp
        return self._cached

    def load(self) -> None:
        """兼容旧调用：真正的读取发生在 `_section()`/`save()` 的锁内重读。"""
        with self._lock:
            self._load()

    def _section(self, locale: str, section_id: str) -> dict[str, Any]:
        with self._lock:
            data = self._load()
        lane = (data.get("sections") or {}).get(locale) or {}
        entry = lane.get(section_id)
        return entry if isinstance(entry, dict) else {}

    def section(self, locale: str, section_id: str) -> dict[str, Any]:
        return self._section(locale, section_id)

    def unit(self, locale: str, section_id: str, uid: str, fingerprint: str) -> dict[str, Any] | None:
        """指纹匹配才返回条目；不匹配/不存在返回 None（调用方据此重发该 unit）。"""
        if not fingerprint:
            return None
        units = self._section(locale, section_id).get("units")
        item = (units or {}).get(uid) if isinstance(units, dict) else None
        if not isinstance(item, dict) or item.get("fp") != fingerprint:
            return None
        stored = item.get("item")
        return stored if isinstance(stored, dict) else None

    def summary(self, locale: str, section_id: str, fingerprint: str) -> dict[str, Any] | None:
        entry = self._section(locale, section_id)
        summary = entry.get("summary")
        if not isinstance(summary, dict) or not fingerprint or summary.get("fp") != fingerprint:
            return None
        return summary

    def apparatus(self, locale: str, section_id: str, fingerprint: str) -> dict[str, Any] | None:
        """装置分类收据（指纹不匹配视为无收据：事实/绑定/策略变了必须重新分类）。"""
        entry = self._section(locale, section_id)
        receipt = entry.get("apparatus")
        if not isinstance(receipt, dict) or not fingerprint or receipt.get("fp") != fingerprint:
            return None
        return receipt

    # -- 写 ---------------------------------------------------------------
    def _scratch_section(self, locale: str, section_id: str) -> dict[str, Any]:
        lane = self._scratch.setdefault(locale, {})
        return lane.setdefault(section_id, {"units": {}})

    def put_unit(self, locale: str, section_id: str, uid: str, fingerprint: str,
                 item: dict[str, Any]) -> None:
        with self._lock:
            entry = self._scratch_section(locale, section_id)
            entry.setdefault("units", {})[uid] = {"fp": fingerprint, "item": checkpoint_item(item)}
            entry["updated"] = _now_iso()
            self._dirty = True

    def put_summary(self, locale: str, section_id: str, *, fingerprint: str,
                    takeaway: str, topic: str) -> None:
        if not fingerprint:
            return
        with self._lock:
            entry = self._scratch_section(locale, section_id)
            entry["summary"] = {"fp": fingerprint, "takeaway": takeaway or "", "topic": topic or ""}
            entry["updated"] = _now_iso()
            self._dirty = True

    def put_apparatus(self, locale: str, section_id: str, *, fingerprint: str,
                      receipt: dict[str, Any]) -> None:
        if not fingerprint:
            return
        with self._lock:
            entry = self._scratch_section(locale, section_id)
            entry["apparatus"] = {"fp": fingerprint, **receipt}
            entry["updated"] = _now_iso()
            self._dirty = True

    def prune(self, locale: str, section_id: str, keep: Iterable[str]) -> None:
        keep_set = set(keep)
        with self._lock:
            current = self._section(locale, section_id).get("units")
            if isinstance(current, dict):
                for uid in [uid for uid in current if uid not in keep_set]:
                    current.pop(uid, None)
                    self._scratch_section(locale, section_id).setdefault("units", {})[uid] = None
                    self._dirty = True

    def save(self) -> None:
        """原子落盘：锁内重读最新文件，只合并本实例改过的条目（避免丢并发更新）。"""
        with self._lock:
            if not self._dirty:
                return
            data = self._load()
            sections = data.setdefault("sections", {})
            for locale, lane_scratch in self._scratch.items():
                lane = sections.setdefault(locale, {})
                if not isinstance(lane, dict):
                    lane = {}
                    sections[locale] = lane
                for section_id, patch in lane_scratch.items():
                    entry = lane.get(section_id)
                    if not isinstance(entry, dict):
                        entry = {"units": {}}
                        lane[section_id] = entry
                    units = entry.get("units")
                    if not isinstance(units, dict):
                        units = {}
                        entry["units"] = units
                    for uid, value in (patch.get("units") or {}).items():
                        if value is None:
                            units.pop(uid, None)
                        else:
                            units[uid] = value
                    if "summary" in patch:
                        entry["summary"] = patch["summary"]
                    if "apparatus" in patch:
                        entry["apparatus"] = patch["apparatus"]
                    entry["updated"] = patch.get("updated") or _now_iso()
            self._trim(sections)
            payload = json.dumps(data, ensure_ascii=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                temporary.write_text(payload, "utf-8")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self._cached = data
            stat = self.path.stat()
            self._stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            self._scratch = {}
            self._dirty = False

    @staticmethod
    def _trim(sections: dict[str, Any]) -> None:
        """软上限：极端情况下按最后更新时间淘汰最旧的节（单本书远达不到上限）。"""
        while True:
            entries: list[tuple[str, str, str]] = []
            for locale, lane in sections.items():
                if not isinstance(lane, dict):
                    continue
                for section_id, entry in lane.items():
                    if isinstance(entry, dict):
                        entries.append((locale, section_id, str(entry.get("updated") or "")))
            over_count = len(entries) > MAX_CHECKPOINT_SECTIONS
            over_bytes = len(json.dumps(sections, ensure_ascii=False)) > MAX_CHECKPOINT_BYTES
            if not entries or not (over_count or over_bytes):
                return
            locale, section_id, _ = min(entries, key=lambda row: row[2] or "")
            sections.get(locale, {}).pop(section_id, None)


# ---------------------------------------------------------------------------
# 请求执行：有限并发 + 串行提交 + 批边界暂停
# ---------------------------------------------------------------------------


@dataclass
class BatchOutcome:
    """一次批请求的结果：payload 与错误分开（错误批也可能带可用的部分 payload）。"""

    payload: dict[str, Any] | None = None
    error: str = ""
    timed_out: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0
    started_at: str = ""
    finished_at: str = ""
    extras: dict[str, Any] = field(default_factory=dict)


def run_plans(
    plans: Sequence[BatchPlan],
    *,
    execute: Callable[[BatchPlan], BatchOutcome],
    commit: Callable[[BatchPlan, BatchOutcome], None],
    concurrency: int = MAX_CONCURRENCY,
    should_pause: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """按并发上限发请求，**按计划顺序串行提交**；批边界检查暂停。

    返回 `{"paused": bool, "completed": [索引], "failed": {索引: 消息}, "submitted": int}`。
    """
    limit = max(1, min(int(concurrency or 1), MAX_CONCURRENCY))
    paused = False
    completed: list[int] = []
    failed: dict[int, str] = {}
    submitted = 0
    pending: list[tuple[int, Future]] = []
    next_index = 0
    pool = ThreadPoolExecutor(max_workers=limit, thread_name_prefix="reader-batch")
    try:
        while next_index < len(plans) or pending:
            while (not paused and next_index < len(plans) and len(pending) < limit):
                if should_pause is not None and should_pause():
                    paused = True
                    break
                pending.append((next_index, pool.submit(execute, plans[next_index])))
                next_index += 1
                submitted += 1
            if not pending:
                break
            index, future = pending.pop(0)
            try:
                outcome = future.result()
            except Exception as error:  # noqa: BLE001 - 单批失败不炸整节
                failed[index] = str(error)[:500]
                continue
            try:
                commit(plans[index], outcome)
            except Exception as error:  # noqa: BLE001 - 提交失败按批失败处理（已提交的保留）
                failed[index] = str(error)[:500]
                continue
            completed.append(index)
    finally:
        pool.shutdown(wait=True)
    return {"paused": paused, "completed": completed, "failed": failed, "submitted": submitted}


__all__ = [
    "MAX_BATCH_UNITS", "MAX_INPUT_TOKENS", "MAX_OUTPUT_TOKENS", "MAX_CONCURRENCY",
    "MAX_WINDOW_SENTENCES", "CHECKPOINT_VERSION", "ActivityLog", "BatchOutcome", "BatchPlan",
    "UnitCheckpoints", "apply_consolidation", "apply_section_summary", "checkpoint_item",
    "checkpoint_unit_fingerprint", "consolidation_prompt", "estimate_tokens",
    "estimate_unit_output_tokens", "finalize_chunk", "is_timeout_error", "make_proxy_doc",
    "plan_batches", "plan_clause_windows", "plan_summary_steps", "plan_unit_windows",
    "run_plans", "section_summary_points", "section_summary_prompt", "unit_clause_spans",
    "window_unit", "window_unit_clause",
]
