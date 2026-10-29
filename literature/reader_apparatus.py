"""AIFold 装置内容（索引 / 参考文献 / 书目类）的**有界 AI 分类**与抑制收据。

用户 2026-10-27 裁决：印刷书目/索引/backmatter 在 AI 确认其类型后保留 Full，**跳过短层生成**
（不为索引的 233/617 段写 takeaway，省下最多调用与 token）。要点：

1. **候选召回只看显式标题**（Index / References / Bibliography / Primary Sources /
   Secondary Sources / Monograph Series 等），不做通用启发式自动抑制——正文里叫
   “Archives” 或 “Sources” 的章节不会被误判成装置内容。
2. **AI 分类器**只看到：节标题、unit 数量与类型分布、节内**代表性样本**（首/中/尾，
   ≤`MAX_BATCH_UNITS` unit、≤`MAX_INPUT_TOKENS` token）与相邻节标题。它回答
   `{"kind": index|bibliography|catalog|body, "mixed": bool, "reason": str}`。
3. 只有 `kind ∈ 装置类` 且 `mixed=false` 才抑制：该节全部 unit 标 suppressed +
   `suppress_reason="apparatus"`，并写入**分类收据**（指纹 = 节 canonical digest + 服务/模型 +
   策略版本）。Full 不动；译文层由源层抑制权威传播，**零派生调用**。
4. 分类请求失败（异常/不可解析）→ 该节保持 pending，不猜、不抑制。
5. 收据让就绪判定能区分「AI 分类为装置」与「确定性短段抑制」：带 apparatus 标记的节
   必须有匹配指纹的收据，否则不算就绪（事实/绑定/策略变化后必须重新分类）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from third_party.textfold import ladder as tf_ladder
from third_party.textfold import llm as tf_llm
from third_party.textfold import tokens as tf_tokens

from .reader_batches import (
    MAX_BATCH_UNITS,
    MAX_INPUT_TOKENS,
    estimate_tokens,
)
from .textfold_adapter import SOURCE_LOCALE, upstream_lang
from .providers import ProviderError

# 策略版本：候选词表/样本策略/判定规则变化时必须 +1，让旧收据自动失效（重新分类）。
POLICY_VERSION = 1
APPARATUS_KINDS = ("index", "bibliography", "catalog")
SUPPRESS_REASON = "apparatus"

# 显式装置标题（大小写不敏感，允许前后缀修饰词）。**不做**语义/规模启发式：
# 只有标题本身就是这些明确的 backmatter 名称才是候选。
_TITLE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("index", r"^(?:general |name |names |subject |author |authors |place |places |title )?index$"),
    ("index", r"^index(?: of (?:names|subjects|places|authors|titles|terms))$"),
    ("index", r"^(?:人名|地名|主题|名词|事项|书名|作者)?索引$"),
    ("bibliography", r"^(?:(?:selected|general|annotated|critical|classified|extended) )?bibliography$"),
    ("bibliography", r"^(?:works? cited|literature cited|references?|reference list|bibliographic references)$"),
    ("bibliography", r"^(?:primary sources?|secondary sources?|archival sources?|sources consulted|"
                   r"sources and (?:abbreviations|archives)|abbreviations and sources)$"),
    ("bibliography", r"^(?:参考文献|引用文献|参考书目|文献目录|征引书目|引用书目|参考文献目录|主要参考文献)$"),
    ("catalog", r"^(?:monograph series|series list|list of (?:publications|volumes|titles)|"
                r"publisher'?s? (?:list|catalog(?:ue)?)|catalog(?:ue)? of (?:publications|titles)|"
                r"also available|other volumes in|publications in (?:this|the) series)$"),
    ("catalog", r"^(?:丛书目录|丛书书目|出版书目|图书目录)$"),
)


def normalize_title(title: str) -> str:
    """标题归一化：去编号前缀/序号/首尾标点，压空白。"""
    value = str(title or "").strip().lower()
    value = value.replace("’", "'")
    value = value.strip(" .,:;·—-()[]【】")
    value = " ".join(value.split())
    return value


def candidate_kind(title: str) -> str | None:
    """显式装置标题 → 候选类型；不匹配返回 None（不猜）。"""
    import re as _re

    value = normalize_title(title)
    if not value or len(value) > 200:
        return None
    # 去掉常见章节编号（"Chapter 7", "7", "七、"）
    value = _re.sub(r"^第[一二三四五六七八九十百0-9]+[章节篇部]\s*", "", value)
    value = _re.sub(r"^(?:(?:chapter|part|appendix)\s+)?[0-9ivxlcdm一二三四五六七八九十百]+(?:\s+|[.、:：]\s*)",
                    "", value).strip() or value
    if _re.search(r"\bmonograph series\b", value):
        return "catalog"
    for kind, pattern in _TITLE_PATTERNS:
        if _re.match(pattern, value):
            return kind
    return None


def apparatus_fingerprint(*, section_digest: str, service_id: str, model: str) -> str:
    """收据指纹：节 canonical digest + 冻结的服务/模型 + 策略版本。"""
    payload = {
        "policy": POLICY_VERSION,
        "section": section_digest or "",
        "service_id": service_id or "",
        "model": model or "",
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


def _representative_units(
    doc: dict[str, Any], chunk: dict[str, Any], *, limit_units: int, limit_tokens: int,
) -> list[tuple[str, str]]:
    """首/中/尾代表性样本（≤ limit_units 个 unit、≤ limit_tokens token）。"""
    atoms = doc.get("atoms") or {}
    ids = [uid for uid in chunk.get("units") or [] if uid in (doc.get("units") or {})]
    if not ids:
        return []
    picks: list[int] = []
    if len(ids) <= limit_units:
        picks = list(range(len(ids)))
    else:
        head = limit_units // 3
        tail = limit_units // 3
        middle = limit_units - head - tail
        picks = list(range(head))
        center = len(ids) // 2
        picks += list(range(max(head, center - middle // 2),
                            max(head, center - middle // 2) + middle))
        picks += list(range(len(ids) - tail, len(ids)))
    samples: list[tuple[str, str]] = []
    used = 0
    for index in picks[:limit_units]:
        unit = doc["units"][ids[index]]
        toks = unit.get("toks") or []
        text = tf_tokens.plain(toks, atoms).strip()
        if not text:
            continue
        tokens = estimate_tokens(text)
        budget = max(0, (limit_tokens - used) // max(1, len(picks) - len(samples)))
        if not budget:
            break
        if tokens > budget:
            low, high = 0, len(text)
            while low < high:
                middle = (low + high + 1) // 2
                if estimate_tokens(text[:middle] + " …") <= budget:
                    low = middle
                else:
                    high = middle - 1
            text = text[:low] + " …"
            tokens = estimate_tokens(text)
        if used + tokens > limit_tokens:
            break
        used += tokens
        samples.append((ids[index], text))
    return samples


def classifier_prompt(
    doc: dict[str, Any],
    chunk: dict[str, Any],
    *,
    neighbor_titles: Sequence[str] = (),
    locale: str = SOURCE_LOCALE,
    limit_units: int = MAX_BATCH_UNITS,
    limit_tokens: int = MAX_INPUT_TOKENS,
) -> tuple[str, str]:
    """有界分类提示词：标题 + 规模/类型分布 + 代表性样本 + 相邻节标题。"""
    samples = _representative_units(doc, chunk, limit_units=limit_units,
                                    limit_tokens=max(0, limit_tokens - 1600))
    kinds: dict[str, int] = {}
    for uid in chunk.get("units") or []:
        unit = (doc.get("units") or {}).get(uid) or {}
        key = str(unit.get("type") or "paragraph")
        kinds[key] = kinds.get(key, 0) + 1
    title = str(((chunk.get("tr") or {}).get(locale) or {}).get("title") or chunk.get("title") or "")
    neighbors = "\n".join(f"- {name}" for name in neighbor_titles if str(name or "").strip())
    body = "\n\n".join(f"[{uid}]\n{text}" for uid, text in samples)
    prompt = f"""You are classifying one section of a book by what it IS, not by what it is about. Decide whether this section is back matter that is a list rather than prose: a name/subject index, a bibliography/reference list, or a catalogue of other publications in a series.

Section title: {title}
Units in this section: {sum(kinds.values())} (types: {", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))})
Neighbouring section titles:
{neighbors or "- (none)"}

Representative units (first/middle/last; each is one paragraph or list item):
{body}

Rules:
- "body" when the units read as connected prose making claims, even if the section is called "Sources" or "Archives" and even if it quotes many titles.
- "index" when units are index entries (terms or names followed by page numbers/locators).
- "bibliography" when units are reference entries (author/year/title/publisher), with or without commentary.
- "catalog" when units list other books/publications of a series or publisher, or library metadata.
- mixed=true when the section mixes apparatus units with real prose units; mixed sections must NOT be suppressed.
- reason: one short sentence for the operator log.

Return JSON: {{"kind": "index|bibliography|catalog|body", "mixed": true|false, "reason": "..."}}"""
    system = tf_llm.system_for(doc.get("meta") or {})
    user = tf_ladder.kinds.adapt(prompt, doc.get("meta") or {})
    if estimate_tokens(system) + estimate_tokens(user) > limit_tokens:
        raise ProviderError("书后材料分类上下文超过请求上限，已保留全文；缩小上下文后可继续。", possible_charge=False)
    return system, user


def parse_classifier_reply(payload: Any) -> dict[str, Any]:
    """解析分类回复；不可解析时返回 `{"ok": False}`（调用方保持 pending，不猜）。"""
    if not isinstance(payload, dict):
        return {"ok": False}
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in ("index", "bibliography", "catalog", "body"):
        return {"ok": False}
    mixed = payload.get("mixed")
    if not isinstance(mixed, bool):
        return {"ok": False}
    reason = str(payload.get("reason") or "").strip()[:400]
    return {
        "ok": True,
        "kind": kind,
        "mixed": mixed,
        "reason": reason,
        "affirmative": kind in APPARATUS_KINDS and not mixed,
    }


def apply_suppression(doc: dict[str, Any], chunk: dict[str, Any], *, reason: str = SUPPRESS_REASON) -> list[str]:
    """把本节全部 unit 标成装置抑制（Full 保留），返回受影响的 unit id。"""
    touched: list[str] = []
    for uid in chunk.get("units") or []:
        unit = (doc.get("units") or {}).get(uid)
        if unit is None:
            continue
        unit["lad"] = tf_ladder.suppressed()
        unit["suppressed"] = True
        unit["source"] = "suppressed"
        unit["suppress_reason"] = reason
        unit.pop("error", None)
        touched.append(uid)
    return touched


def propagate_to_target(doc: dict[str, Any], chunk: dict[str, Any], *, locale: str) -> int:
    """把源层装置抑制权威传播到译文层（零派生调用；已译 Full 不动）。"""
    count = 0
    for uid in chunk.get("units") or []:
        unit = (doc.get("units") or {}).get(uid)
        if unit is None:
            continue
        tr = (unit.get("tr") or {}).get(locale)
        if tr is None:
            continue
        tr["lad"] = tf_ladder.suppressed()
        tr["suppressed"] = True
        count += 1
    return count


__all__ = [
    "POLICY_VERSION", "APPARATUS_KINDS", "SUPPRESS_REASON", "apparatus_fingerprint",
    "apply_suppression", "candidate_kind", "classifier_prompt", "normalize_title",
    "parse_classifier_reply", "propagate_to_target", "upstream_lang",
]
