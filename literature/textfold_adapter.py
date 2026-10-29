"""canonical 文档 ↔ TextFold 内部结构 的薄适配层。

这里只做**搬运与投影**，折叠算法本身来自 `third_party/textfold/`（上游 Apache-2.0 快照）：

- 分词与断句：`tokens.tokenize_text` / `tokens.split_sentences`
- 章节折叠提示词与 schema：`ladder.prompt_for` / `ladder.SCHEMA`
- 模型回复 → ladder：`ladder.assemble`（evidence 越界过滤、topic 锚回 takeaway、公式原子还原）
- 无模型降级：`ladder.local` + `ladder.is_apparatus` + `ladder.cjk_*`
- 章节层：`ladder.section_ladder` / `ladder.fold_background` / `ladder.dress`
- doc 格式投影：`fold.LEVELS` / `fold.unit` / `fold.section` / `fold.markdown`

Literature 侧的差异（有意为之，逐条写在这里，避免「同名但不是同一套东西」）：

1. **unit id = 稳定 block_id**。上游用 `S3.p1.1` 一类解析期 id；Literature 的锚点已经是
   `document.json` 的稳定 block_id，批注、译文、bbox、页码都挂在它上面，所以直接复用它。
2. **每种语言一份 doc**，而不是上游的 `unit["tr"][lang]` 内嵌译文。原因是 Literature 的译文层是
   独立事实来源（`translations/<locale>/translation.json`），折叠内容按 locale 各存一份，
   摘要缓存才能按「原文/模型/提示词版本」独立失效。
3. **只折叠有连续正文的块**（paragraph / list）。图片、表格、公式、脚注按原样在「全文」层渲染，
   不交给模型概括；脚注的版式管道判定仍复用 `ladder.is_apparatus` 处理列表与图表题文本。
4. **页面断句补全**只在 CJK 文档生效（上游同样只在 CJK 生效），由 `ladder.dress` 完成。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from third_party.textfold import fold as tf_fold
from third_party.textfold import ladder as tf_ladder
from third_party.textfold import langs as tf_langs
from third_party.textfold import llm as tf_llm
from third_party.textfold import tokens as tf_tokens

from .models import Block, Document, TranslationLayer

SOURCE_LOCALE = "source"
UNIT_TYPES = ("paragraph", "list")
# `fold.unit()` 用 `KINDS.get(u["role"])` 决定 kind，所以 unit["role"] 在装配期是「种类」，
# 折叠后真正的论证角色在 `u["lad"]["role"]`（上游同此约定）。
KIND_ROLE = {"list item": "item", "paragraph": "para"}
# 少于这个词数的块（页码、"* * *" 分隔符、空行残留）不折叠：上游对 CJK 的 `is_apparatus` 同理，
# 但英文侧没有这条判断，由 Literature 在适配层补上（否则会折叠出空 takeaway，界面出现空行）。
MIN_FOLD_WORDS = 3


_SANITIZE_TAG_RE = None  # 延迟编译（见 _sanitize_html）


def _sanitize_html(html: str) -> str:
    """词级 native 载荷里所有 html 字段的消毒（防御纵深；前端渲染前还应再校验）。

    - 剔除 <script>/<style>/<iframe>/<object>/<embed>/<link>/<meta> 整段；
    - 剔除 on* 事件属性与 javascript:/vbscript: URL；
    - 其余标签/属性保留：表格结构与 upstream reader.js 的 innerHTML 渲染需要它们。
    解析层带回的 raw table_html 属外部输入，必须过这里；我们自拼的转义文本过这里是冗余保险。
    """
    global _SANITIZE_TAG_RE
    import re as _re
    if _SANITIZE_TAG_RE is None:
        _SANITIZE_TAG_RE = _re.compile(
            r"<\s*(script|style|iframe|object|embed|link|meta)\b[^>]*>.*?<\s*/\s*\1\s*>"
            r"|<\s*(script|style|iframe|object|embed|link|meta)\b[^>]*/?>\s*",
            _re.IGNORECASE | _re.DOTALL,
        )
    value = str(html or "")
    value = _SANITIZE_TAG_RE.sub("", value)
    value = _re.sub(r"\s+on\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", value, flags=_re.IGNORECASE)
    value = _re.sub(
        r"(href|src)\s*=\s*(\"|')?\s*(javascript|vbscript):[^\"'>\s]*",
        r"\1=\2#",
        value,
        flags=_re.IGNORECASE,
    )
    return value


def _escape_text(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _table_html(block: Block) -> str:
    """把 canonical 表格还原成最小 HTML，喂给上游 `ladder.tables_of()`（章节摘要要看得到表格）。

    优先用解析时保留的原始 table_html；没有就按 cells 的行列重建（rowspan/colspan 不还原，
    只求行文本可读）。任何异常都退化为空串：表格上下文是加分项，不能因此让折叠失败。
    """
    raw = str((block.format or {}).get("table_html") or "")
    if "<table" in raw.casefold():
        return _sanitize_html(raw)
    try:
        rows: dict[int, list[tuple[int, str]]] = {}
        for cell in block.cells or []:
            rows.setdefault(int(cell.row), []).append((int(cell.column), str(cell.text or "")))
        if not rows:
            return ""
        lines = ["<table>"]
        for row_index in sorted(rows):
            lines.append("<tr>")
            for _, text in sorted(rows[row_index]):
                safe = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                lines.append(f"<td>{safe}</td>")
            lines.append("</tr>")
        lines.append("</table>")
        return "".join(lines)
    except Exception:  # noqa: BLE001 - 表格上下文是可选增强
        return ""


def _block_text(block: Block) -> str:
    if block.type == "list":
        return " ".join(item.strip() for item in block.items if item.strip())
    return (block.source_text or "").strip()


def _entry_text(entry: Any, block: Block) -> tuple[str, bool]:
    """返回 (正文, 是否回退原文)。译文缺失/为空/失败都按回退处理，不冒充已译。"""
    if entry is None:
        return _block_text(block), True
    if block.type == "list":
        if entry.items:
            return " ".join(item.strip() for item in entry.items if item.strip()), False
        return _block_text(block), True
    text = (entry.text or "").strip()
    if not text:
        return _block_text(block), True
    return text, False


def split_sentences(text: str) -> list[str]:
    """沿用上游 tokenizer 切句（CJK、缩写、引号收尾都在 upstream 里处理）。"""
    toks = tf_tokens.tokenize_text(text or "")
    return [tf_tokens.plain(toks[start:end], {}) for start, end in tf_tokens.split_sentences(toks)]


def build_ladder_doc(
    document: Document,
    *,
    locale: str = SOURCE_LOCALE,
    translation: TranslationLayer | None = None,
    source_kind: str = "",
) -> dict[str, Any]:
    """把 canonical document（+ 译文层）装配成 TextFold 内部 doc 结构。"""
    meta = {
        "title": document.title,
        # kinds.adapt 依据 source.kind 决定用「论文」还是「文本」的说法；
        # 缺省时 is_paper 会按 arXiv id 形状兜底，这里显式给出更可靠。
        "source": {"kind": source_kind or "pdf"},
        "id": document.project_id,
        "lang": document.source_language,
    }
    atoms: dict[str, Any] = {}
    units: dict[str, dict[str, Any]] = {}
    # 原生文档顺序（upstream reader.js `build()` 遍历的形状）：h=章节标题、u=unit、
    # fig=图/表、eq=公式。这不再是 tables_of() 专用的 fig 列表——native 载荷直接可用；
    # tables_of() 对无 html 的 h/u 条目原样跳过，行为不变。
    blocks: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    chunk: dict[str, Any] = {"id": "S0", "num": "", "title": "前言", "level": 2, "units": [],
                             "block_id": "", "page_index": 0}
    counter = 0
    for block in document.blocks:
        if block.type == "title":
            if chunk["units"] or chunk["block_id"]:
                chunks.append(chunk)
            counter += 1
            entry = translation.entries.get(block.block_id) if translation else None
            title = _block_text(block)
            if locale != SOURCE_LOCALE and entry is not None and (entry.text or "").strip():
                title = entry.text.strip()
            chunk = {
                "id": f"S{counter}", "num": str(counter), "title": title or f"第 {counter} 节",
                "level": max(2, int(block.level or 1) + 1), "units": [],
                "block_id": block.block_id, "page_index": block.page_index,
                "source_level": int(block.level or 1),
            }
            # h 条目在节开始处（先于其 units）；前言 S0 无真实标题块，不发 h（reader.js
            # 对首 h 之前的 u 落在顶层 front，语义一致）。
            blocks.append({"k": "h", "chunk": chunk["id"]})
            continue
        if block.type == "table":
            blocks.append({"id": block.block_id, "k": "fig", "kind": "table", "cap": None,
                           "html": _table_html(block), "chunk": chunk["id"]})
            continue
        if block.type == "image":
            # native 渲染需要图的原位：资源名随块带给前端（src 是 asset 名，不是 URL；
            # 前端用 /projects/{id}/assets/{name} 拉取）。无 html——上游 fig.html 可为空。
            blocks.append({"id": block.block_id, "k": "fig", "kind": "image", "cap": None,
                           "html": "", "src": list(block.resource_refs or []), "chunk": chunk["id"]})
            continue
        if block.type == "equation":
            # 公式以转义纯文本给出（上游 eq.html 是 MathML；Literature 不产 MathML，
            # 前端可用 katex 渲染 source_text）。esc 过，无脚本/事件属性。
            blocks.append({"id": block.block_id, "k": "eq", "chunk": chunk["id"],
                           "html": _escape_text(_block_text(block))})
            continue
        if block.type not in UNIT_TYPES:
            continue
        entry = translation.entries.get(block.block_id) if translation else None
        if locale == SOURCE_LOCALE:
            full, fallback = _block_text(block), False
        else:
            full, fallback = _entry_text(entry, block)
        if not full.strip():
            continue
        toks = tf_tokens.tokenize_text(full)
        sents = tf_tokens.split_sentences(toks)
        if not sents:
            continue
        kind_role = KIND_ROLE["list item" if block.type == "list" else "paragraph"]
        units[block.block_id] = {
            "id": block.block_id, "role": kind_role, "toks": toks, "sents": sents,
            "chunk": chunk["id"],
            **({"list": block.block_id} if block.type == "list" else {}),
            "block_id": block.block_id, "page_index": block.page_index,
            "type": block.type, "fallback": fallback,
        }
        # 旧版这里给每个 unit 塞过一个 `k:"fig"` 兼容条目（tables_of 的 cap 查找用）；
        # 现在有了真正的 {k:"u"} 原生顺序条目，非表格 unit 不再产 fig（空 html 的 fig
        # 会被前端渲染成空图）。tables_of 只认 kind=table/raw，行为不变。
        chunk["units"].append(block.block_id)
        # unit 的原生顺序条目（upstream reader.js 的 {k:"u"} 形状；role/list 供列表分组）
        blocks.append({"k": "u", "u": block.block_id, "chunk": chunk["id"]})
    if chunk["units"] or chunk["block_id"]:
        chunks.append(chunk)
    return {
        "meta": meta,
        "atoms": atoms,
        "units": units,
        "chunks": chunks,
        "blocks": blocks,
        "locale": locale,
        "is_cjk": tf_ladder.cjk_doc({"units": units, "chunks": chunks, "atoms": atoms}),
    }


def upstream_lang(locale: str) -> str:
    """Literature 的 locale（如 `zh-CN`）→ 上游 `langs.LANGS` 的语言码。

    上游 `translate.prompt_for`/`langs.budget` 只认 LANGS 里的码；未收录的语言按基础码
    匹配，再退回英文（预算按词计，不会错，只是预算值不同）。
    """
    code = (locale or "").strip()
    if not code or code == SOURCE_LOCALE:
        return "en"
    if code in tf_langs.LANGS:
        return code
    base = code.split("-")[0]
    return base if base in tf_langs.LANGS else "en"


def merge_translation_doc(
    source_doc: dict[str, Any], trans_doc: dict[str, Any], locale: str,
) -> dict[str, Any]:
    """把原文 ladder doc 与译文 doc 合并成上游同构的一份 doc：unit 带原文 `toks`，
    译文放在 `u["tr"][locale]`（上游 `fold._view` 的同一条路径），章节标题同理。

    上游是「一份 ladder + 各语言 tr」；Literature 此前是每语言一份独立 doc，两层各自的
    evidence/topic 无对齐保证。合并 doc 让译文层投影直接走上游 `fold.section(doc, c,
    locale)`：Key 逐字来自译文全文、evidence 下标继承原文 ladder、role 沿用原文论证角色。
    不改动两个入参 doc（新 dict 复用旧 token 列表，都是只读）。
    """
    trans_units = trans_doc["units"]
    trans_chunks = {chunk["id"]: chunk for chunk in trans_doc["chunks"]}
    units: dict[str, dict[str, Any]] = {}
    for uid, unit in source_doc["units"].items():
        merged_unit = {**unit, "tr": dict(unit.get("tr") or {})}
        trans_unit = trans_units.get(uid)
        if trans_unit is not None:
            merged_unit["tr"][locale] = {
                "toks": trans_unit["toks"], "sents": trans_unit["sents"],
                "fallback": bool(trans_unit.get("fallback")),
            }
        units[uid] = merged_unit
    chunks: list[dict[str, Any]] = []
    for chunk in source_doc["chunks"]:
        trans_chunk = trans_chunks.get(chunk["id"])
        merged_chunk = {**chunk, "tr": dict(chunk.get("tr") or {})}
        if trans_chunk is not None:
            merged_chunk["tr"][locale] = {
                "title": trans_chunk.get("title", ""), "lad": None,
            }
        chunks.append(merged_chunk)
    meta = {**source_doc["meta"], "titles": {**(source_doc["meta"].get("titles") or {}), locale: trans_doc["meta"].get("title", "")}}
    merged = {
        "meta": meta,
        "atoms": source_doc["atoms"],
        "units": units,
        "chunks": chunks,
        "blocks": source_doc["blocks"],
        "locale": locale,
        "is_cjk": source_doc.get("is_cjk", False),
    }
    # `fold._view` 对有 tr 的 unit 直接读 `u["lad"]` 与 `z["lad"]`：上游 build() 结尾给全部 unit
    # 兒底 local ladder。Literature 不执行正文摘句：兒底用**纯 Full 占位**（空短层、无来源、
    # 不标 suppressed 不标 model），后续 AI 折叠/存盘重建覆盖；投影不炸，Full 照常显示。
    ensure_ladders(merged)
    # 源层 brief 为空（suppressed/pending）的 unit 不进派生，但它的 tr 仍存在：
    # 给 `z["lad"]` 兒底空层（同样不折叠，Full 照常显示译文），否则投影 KeyError。
    # 注意 model=""（待生成）而非 "local"——空 brief 不等于 suppressed，也不冒充已折叠，
    # readiness 按存盘 unit source 判定，这里只是投影兒底。
    for unit in merged["units"].values():
        lad = unit.get("lad") or {}
        if not (lad.get("brief") or []):
            for z in (unit.get("tr") or {}).values():
                if isinstance(z, dict) and "lad" not in z:
                    z["lad"] = {"brief": [], "tn": 0, "topic": [],
                                "role": lad.get("role") or "context", "model": ""}
    return merged


def trans_reply_schema() -> dict[str, Any]:
    """译文层折叠的回复 schema：只重写短层（takeaway/reason/topic）+ 章节主线。

    与上游 `translate.SCHEMA` 的差别（逐条有因）：不需要 `sentences`——Literature 的
    Full/Key 文本来自用户的 `translations/<locale>/translation.json`（人工译文不得被
    模型覆写），模型只写短层；不需要 `evidence`——下标从原文 ladder 继承，这是
    「原译同步、证据可追溯」的契约本体。
    """
    return {
        "type": "object",
        "properties": {
            "section": {"type": "object", "properties": {"takeaway": {"type": "string"}, "topic": {"type": "string"}},
                        "required": ["takeaway", "topic"]},
            "units": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "takeaway": {"type": "string"}, "reason": {"type": "string"},
                "topic": {"type": "string"}},
                "required": ["id", "takeaway", "reason", "topic"]}},
        },
        "required": ["section", "units"],
    }


def trans_units_in_section(merged_doc: dict[str, Any], chunk: dict[str, Any], locale: str) -> list[str]:
    """本节里**有译文**、需要重写短层的 unit（无译文的 unit 由 `fold._view` 回退原文层）。"""
    return [uid for uid in chunk["units"]
            if (merged_doc["units"].get(uid, {}).get("tr") or {}).get(locale) is not None]


def _glossary_hash(translation: dict[str, Any] | None) -> str:
    """术语表快照的摘要：术语变化只重建派生短层，不动原文层。"""
    if not translation:
        return ""
    snapshot = getattr(translation, "glossary_snapshot", None) or {}
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def ensure_trans_lads(merged_doc: dict[str, Any], chunk: dict[str, Any], *, locale: str) -> None:
    """投影前兒底：源层 brief 为空（suppressed/pending）的 unit 不进派生，
    但它的 tr 仍存在，`fold._view` 要求 `z["lad"]` 存在。给这类 tr 补空层（同样
    不折叠，Full 照常显示译文），否则投影 KeyError。model=""（待生成）——
    空 brief 不冒充 suppressed 也不冒充 local，readiness 按存盘 source 判定。"""
    for uid in chunk["units"]:
        unit = merged_doc["units"].get(uid)
        if unit is None:
            continue
        z = (unit.get("tr") or {}).get(locale)
        if z is None or "lad" in z:
            continue
        lad = unit.get("lad") or {}
        if lad.get("brief"):
            continue
        z["lad"] = {"brief": [], "tn": 0, "topic": [],
                    "role": lad.get("role") or "context", "model": ""}


def derive_reply_schema() -> dict[str, Any]:
    """译文层「派生」调用的回复 schema。

    与上游 `translate.SCHEMA` 的本质区别：不翻译全文、不返回 `sentences`（Full 来自
    用户的 translation.json，永不被覆写），也不返回 `evidence`（下标从原文层继承，
    不重新挑句）。只重写短层 + 逐条给出证据句的分层译句（`ev_sentences`，与 evidence
    下标一一对应，标记为分层译句而非已存译文逐字摘录）。
    """
    return {
        "type": "object",
        "properties": {
            "section": {"type": "object", "properties": {"takeaway": {"type": "string"}, "topic": {"type": "string"}},
                        "required": ["takeaway", "topic"]},
            "units": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"}, "takeaway": {"type": "string"}, "reason": {"type": "string"},
                "topic": {"type": "string"}, "ev_sentences": {"type": "array", "items": {"type": "string"}}},
                "required": ["id", "takeaway", "reason", "topic", "ev_sentences"]}},
        },
        "required": ["section", "units"],
    }


def derive_context(
    merged_doc: dict[str, Any], chunk: dict[str, Any], *, locale: str,
    translation: Any | None = None,
) -> tuple[str, str, list[tuple[str, list[int]]]]:
    """译文层派生调用的上下文：**只含 fold 产物，不含任何整章正文**。

    输入（每 unit）：原文层的 role / takeaway / reason / topic + 被选中的证据句（逐字、短）；
    加目标语章节标题（来自 translation.json 事实层）与截断后的术语表。不投喂原文全文、
    不投喂译文全文，因此不存在二次全文翻译或双份全文 token。

    返回 `(system, user, unit_evidence)`，`unit_evidence` 是 `(uid, evidence 下标)` 列表
    （与提示词里的 [U{n}] 顺序一致，供回复校验与 evidence 继承）。
    """
    code = upstream_lang(locale)
    L, b = tf_langs.name(code), tf_langs.budget(code)
    unit_word, acro = b["unit"], " (an English acronym counts as one)" if tf_langs.cjk(code) else ""
    A = merged_doc["atoms"]
    tr_chunk = (chunk.get("tr") or {}).get(locale) or {}
    sec = chunk.get("lad") or {}
    blocks: list[str] = []
    unit_evidence: list[tuple[str, list[int]]] = []
    for uid in chunk["units"]:
        unit = merged_doc["units"].get(uid)
        if unit is None or (unit.get("tr") or {}).get(locale) is None:
            continue                       # 无译文：本节派生不含它（投影回退原文层）
        lad = unit.get("lad") or {}
        if not (lad.get("brief") or []):
            continue                       # 无原文短层（如纯 suppressed）：没有可派生的东西
        n = len(unit_evidence)
        ev = [int(s) for s in (lad.get("ev") or []) if isinstance(s, (int, float))]
        sentences = [tf_tokens.plain(unit["toks"][s:e], A) for s, e in unit["sents"]]
        reason = tf_tokens.plain(lad["brief"][lad["tn"] + 1:-1], A) if len(lad["brief"]) > lad["tn"] + 1 else ""
        lines = [
            f"[U{n}] {tf_ladder.KIND.get(unit.get('role', 'para'), 'paragraph')} (argument role: {lad.get('role') or 'context'})",
            f"  source takeaway: {tf_tokens.plain(lad['brief'][:lad['tn']], A)}",
            f"  source reason: {reason}",
            f"  source topic: {tf_tokens.plain(lad.get('topic') or [], A)}",
            "  backing sentences as numbered evidence slots (verbatim source; translate each numbered slot into " + L + " as ONE array item, same order):",
        ]
        for i, s in enumerate(ev):
            if s < len(sentences):
                lines.append(f"    {i}: {sentences[s]}")
        blocks.append("\n".join(lines))
        unit_evidence.append((uid, ev))
    target_title = str(tr_chunk.get("title") or chunk.get("title") or "")
    sec_take = sec.get("take") or []
    sec_topic = tf_tokens.plain([sec_take[i] for i in (sec.get("topic") or []) if isinstance(i, int) and i < len(sec_take)], A)
    glossary = ""
    if translation is not None:
        snapshot = getattr(translation, "glossary_snapshot", None) or {}
        if snapshot:
            terms = [f"{src} = {dst}" for src, dst in list(snapshot.items())[:40]]
            glossary = "\nGlossary (keep these {L} renderings):\n".replace("{L}", L) + "\n".join(terms[:40])
    prompt = tf_ladder.kinds.adapt(f"""A reader is reading this section in {L}. The section's full text already exists as a {L} translation; do NOT translate the section. Below are the zoom levels already written for the source text, with the exact source sentences that back each unit. Write the same zoom levels in {L}.

Section: {target_title}
Source section takeaway: {tf_tokens.plain(sec_take, A)}
Source section topic: {sec_topic}
{glossary}

{chr(10).join(blocks)}

Rules:
- takeaway: the source takeaway as natural {L} of at most {b['takeaway']} {unit_word}{acro}, same information, conclusion first, no new facts, not a word-by-word translation.
- reason: the source reason as natural {L} of at most {b['reason']} {unit_word}{acro}; "" when the source reason is empty.
- topic: at most {b['topic']} {unit_word}{acro}, copied VERBATIM from inside the {L} takeaway.
- ev_sentences: provide exactly ONE array item per numbered evidence slot (0,1,2,…), in slot order; len(ev_sentences) MUST equal the number of slots. A slot may contain several real sentences (superscript footnote marks can hide a sentence boundary from the tokenizer); translate ALL sentences of that slot together inside its single item, in order. Never split one slot into multiple items, never merge slots, never return fewer or more items than slots. These items are shown as the unit's evidence in the {L} pane and are marked as layer translations (the reader's translation file is never quoted verbatim for them and never modified).
- section: takeaway (at most {b['section']} {unit_word}{acro}) and topic (at most {b['topic']} {unit_word}{acro}, verbatim from that takeaway) in {L}.
- {tf_langs.punctuation_rule(code)}

Return JSON: {{"section": {{"takeaway": "...", "topic": "..."}}, "units": [{{"id": "U0", "takeaway": "...", "reason": "...", "topic": "...", "ev_sentences": ["..."]}}, ...]}} with exactly one entry per unit, in order.""", merged_doc["meta"])
    return tf_llm.system_for(merged_doc["meta"]), prompt, unit_evidence


def apply_derived_reply(
    merged_doc: dict[str, Any],
    chunk: dict[str, Any],
    payload: dict[str, Any],
    *,
    locale: str,
    model: str,
    unit_evidence: list[tuple[str, list[int]]],
) -> tuple[list[str], int, int]:
    """派生回复落 ladder。返回 (告警, 派生 unit 数, 未生成 unit 数)。

    - evidence 下标**原样继承**（不重新挑句）；brief = ev_sentences（分层译句，与
      evidence 1:1），标 `brief_source="derived"`；
    - takeaway/reason/topic 由 `written()`/`topic_tokens()` 锚定（无原子时的普通文本路径）；
    - 原文层已被踩到 Brief（tn=0）的 unit，译文层同样 tn=0；
    - 回复缺失/ev_sentences 不可用的 unit：不动它的短层，落 per-unit 错误元数据
      `tr[locale]["error"] = {code, message}`（code 见 PENDING_ERROR_CODES），投影经
      section_view 透出——**没有本地预览回退路径**，这些 unit 保持未生成、留待补齐。
    """
    by_id = reply_units(payload)
    warnings: list[str] = []
    derived = fallback = 0
    # 章节字段先过类型边界：畸形 section 只让**摘要保持未生成**，不能抛 TypeError 拖死整批。
    section, section_problem = section_ladder_safe(
        payload.get("section"), tf_langs.budget(upstream_lang(locale))["topic"],
    )
    if section_problem:
        warnings.append(f"派生章节摘要：{section_problem}")
    if section:
        chunk.setdefault("tr", {})[locale] = {**((chunk.get("tr") or {}).get(locale) or {}), "lad": section}
    # 源层 suppressed 的 unit（页码/分隔符等装置内容）不在 unit_evidence 里（无可派生短层），
    # 但目标层必须同步 suppressed——否则兜底 lad 会被投影成 local，带装置内容的节永不 ready。
    for uid in trans_units_in_section(merged_doc, chunk, locale):
        src_unit = merged_doc["units"].get(uid) or {}
        # 判据用显式标记（suppressed 标志/来源），不用 lad.model=="local" 猜测——
        # pending（待生成）与真实 suppressed 必须区分，空 brief 不当 suppressed。
        if src_unit.get("suppressed") or src_unit.get("source") == "suppressed":
            tr = ((merged_doc["units"][uid].get("tr") or {}).get(locale) or {})
            tr["lad"] = tf_ladder.suppressed()
            tr["suppressed"] = True
    for n, (uid, ev) in enumerate(unit_evidence):
        unit = merged_doc["units"].get(uid)
        if unit is None:
            warnings.append(f"{uid}：派生目标 unit 不在当前 ladder，跳过")
            fallback += 1
            continue
        # 逐 unit 隔离：单段畸形回复/非预期异常只能让**该段**保持未生成，
        # 不能把同批其他合法段一起丢掉（批内串行提交，异常会丢掉整批）。
        try:
            ok, message = _apply_unit_derived(
                merged_doc, unit, by_id.get(f"U{n}"), ev, locale=locale, model=model,
            )
        except Exception as error:  # noqa: BLE001 - 边界隔离，不猜造内容
            ok = False
            message = (
                f"{uid}：应用该段派生回复时异常（{type(error).__name__}: {str(error)[:120]}），"
                "保持未生成（留待补齐）"
            )
            _set_tr_error(unit, locale, "unit_apply_error", message)
        if not ok:
            warnings.append(message)
            fallback += 1
            continue
        derived += 1
        if message:
            warnings.append(message)
    return warnings, derived, fallback


def _apply_unit_derived(
    merged_doc: dict[str, Any], unit: dict[str, Any], reply: dict[str, Any] | None,
    inherited_ev: list[int], *, locale: str, model: str,
) -> tuple[bool, str]:
    """把本批派生回复里的**一段**落到 `tr[locale]`；返回 (是否成功, 告警/错误文本)。

    字段类型边界：`takeaway` 非 str → `field_type_invalid`（不 str() 强转）；`reason`/`topic`
    非 str → 按缺失处理（与模型没写等价，可审计）；`ev_sentences` 元素非 str →
    `bad_ev_sentences`（不可采信，不猜对应）。
    """
    uid = str(unit.get("id") or "")
    if reply is None:
        message = f"{uid}：派生回复缺少该段，保持未生成（留待补齐）"
        _set_tr_error(unit, locale, "missing_reply", message)
        return False, message
    takeaway = reply.get("takeaway")
    if takeaway is not None and not isinstance(takeaway, str):
        message = (
            f"{uid}：派生 takeaway 类型非法（{type(takeaway).__name__}），"
            "保持未生成（不猜造内容，留待补齐）"
        )
        _set_tr_error(unit, locale, "field_type_invalid", message)
        return False, message
    take_text = tf_ladder.clean(takeaway)
    if not take_text:
        message = f"{uid}：派生 takeaway 为空，保持未生成（留待补齐）"
        _set_tr_error(unit, locale, "empty_takeaway", message)
        return False, message
    src_lad = unit.get("lad") or {}
    ev = [int(s) for s in (src_lad.get("ev") or inherited_ev)
          if isinstance(s, (int, float)) and not isinstance(s, bool)]
    raw_sentences = reply.get("ev_sentences")
    if raw_sentences is not None and not isinstance(raw_sentences, (list, tuple)):
        message = f"{uid}：ev_sentences 类型非法（{type(raw_sentences).__name__}），保持未生成（留待补齐）"
        _set_tr_error(unit, locale, "bad_ev_sentences", message)
        return False, message
    items = list(raw_sentences or [])
    if any(not isinstance(item, str) for item in items):
        bad = next(type(item).__name__ for item in items if not isinstance(item, str))
        message = f"{uid}：ev_sentences 含非字符串项（{bad}），不可采信，保持未生成（留待补齐）"
        _set_tr_error(unit, locale, "bad_ev_sentences", message)
        return False, message
    derived_sentences = [item.strip() for item in items if item.strip()]
    if len(derived_sentences) != len(ev):
        # 数量不匹配 ≠ 可猜对应关系：不前缀截断、不改原 evidence，整 unit 拒绝为
        # 派生失败（保持未生成/留待补齐），不伪精确对应。提示词已要求每个编号
        # evidence 槽位恰好一条数组项（槽内可能含多个真实句子，要整体译在一条里），
        # 不分裂槽位、不合并槽位。
        message = (
            f"{uid}：ev_sentences {len(derived_sentences)} 条与 evidence 槽位 {len(ev)} 个不一致，"
            "整 unit 拒绝派生（不猜对应关系），保持未生成（留待补齐）"
        )
        _set_tr_error(unit, locale, "ev_sentences_mismatch", message)
        return False, message
    if not derived_sentences:
        message = f"{uid}：ev_sentences 为空，保持未生成（留待补齐）"
        _set_tr_error(unit, locale, "empty_ev_sentences", message)
        return False, message
    payload, problems = _normalize_optional_fields(reply)
    # brief tokens：takeaway/reason 是模型写出的短层；ev_sentences 逐条 token 化，
    # sents 区间与 evidence 下标一一对应（brief[i] ↔ evidence[i]）。
    code = upstream_lang(locale)
    dot = tf_langs.stop(code)
    take = tf_tokens.tokenize_text(take_text)
    reason_text = tf_ladder.clean(payload.get("reason"))
    brief = [dict(tk) for tk in take]
    if reason_text:
        brief.append({"t": dot} if tf_langs.cjk(code) else {"t": dot, "s": 1})
        brief += tf_tokens.tokenize_text(reason_text)
    brief.append({"t": dot})
    if take:
        take[-1].pop("s", None)
    tn = len(take)
    topic = tf_ladder.topic_tokens(
        tf_ladder.clean(payload.get("topic")), brief, tn, {"toks": [], "sents": []},
        merged_doc["atoms"], [], tf_langs.budget(code)["topic"],
    )
    if src_lad and int(src_lad.get("tn") or 0) == 0:
        tn, topic = 0, []
    tr = (unit.get("tr") or {}).get(locale)
    if not isinstance(tr, dict):
        message = f"{uid}：该段没有译文槽位，无法派生（保持未生成）"
        _set_tr_error(unit, locale, "missing_reply", message)
        return False, message
    tr.pop("error", None)
    tr["lad"] = {
        "brief": brief, "tn": tn, "topic": topic,
        "role": (src_lad or {}).get("role") or "context",
        "model": model,
        "ev": list(ev),
        "brief_sentences": derived_sentences,
        "brief_source": "derived",
    }
    return True, (f"{uid}：" + "；".join(problems)) if problems else ""
def unit_view(doc: dict[str, Any], block_id: str) -> dict[str, Any] | None:
    """一个 unit 的格式投影（`fold.unit` 同款字段），含逐字 brief 与 0 起的 evidence。"""
    if block_id not in doc["units"]:
        return None
    value = tf_fold.unit(doc, block_id, "en")
    unit = doc["units"][block_id]
    value["fallback"] = bool(unit.get("fallback"))
    value["type"] = unit.get("type", "paragraph")
    value["page_index"] = unit.get("page_index", 0)
    value["block_id"] = block_id
    return value


def section_view(
    doc: dict[str, Any], chunk: dict[str, Any], *, lang: str = "en",
) -> dict[str, Any]:
    """`fold.section()` 的投影 + 两处 Literature 适配：

    - 上游 `fold.unit()` 用 `" ".join(sentences)` 拼 `full`（面向 Markdown 导出）；中文段落会在句间
      多出空格。这里改回按 token 还原原文，`full` 必须与 `document.json` 的正文逐字一致。
      译文层（lang ≠ "en"）同理：`full` 与 `translation.json` 的译文逐字一致。
    - `brief_text` 同理：CJK 文档直接连写，拉丁文文档用空格。
    """
    value = tf_fold.section(doc, chunk, lang)
    value["block_id"] = chunk.get("block_id", "")
    value["page_index"] = chunk.get("page_index", 0)
    is_trans = lang != "en"
    joiner = "" if (doc.get("is_cjk") if not is_trans else _cjk_display(doc, chunk, lang)) else " "
    for unit in value["units"]:
        source = doc["units"].get(unit["id"])
        if is_trans:
            tr = ((source or {}).get("tr") or {}).get(lang)
            # 译文层的 full 永远来自 translation.json 事实层（fold.unit 的英文空格拼接不适用；
            # 派生短层也不改写 Full）。
            if source is not None and tr is not None:
                unit["full"] = tf_tokens.plain(tr["toks"], doc["atoms"])
                unit["fallback"] = bool(tr.get("fallback"))
            elif source is not None:
                unit["full"] = tf_tokens.plain(source["toks"], doc["atoms"])
                unit["fallback"] = True
            if source is not None:
                unit["type"] = source.get("type", "paragraph")
                unit["page_index"] = int(source.get("page_index", 0))
            lad = (((source or {}).get("tr") or {}).get(lang) or {}).get("lad") or (source or {}).get("lad") or {}
            joiner2 = "" if tf_langs.cjk(upstream_lang(lang)) else " "
            brief_sentences = lad.get("brief_sentences")
            if brief_sentences is not None:
                # 派生层：brief = 分层译句（与 evidence 下标 1:1，非已存译文逐字摘录，
                # 可回溯到原文证据句）；evidence 原样继承，不按译文句序硬拷。
                unit["brief"] = list(brief_sentences)
                unit["brief_text"] = joiner2.join(brief_sentences)
                unit["evidence"] = list(lad.get("ev") or [])
                unit["brief_source"] = str(lad.get("brief_source") or "derived")
            else:
                # 无派生短层的 unit：空 brief（纯 Full），标 pending——不再有 translation-local
                # 摘句路径（旧存盘 local 短层已在 _restore_target_lads 置空待生成）。
                unit["brief_text"] = joiner2.join(unit.get("brief") or [])
                unit["brief_source"] = "pending"
            # 源层 suppressed（页码/分隔符等装置内容）同步传播：目标层不是 AI 内容也不是本地摘句，
            # 标 suppressed——否则带装置内容的节在 ready 判定里永远差一个 local unit（死锁）。
            unit["suppressed"] = bool((source or {}).get("suppressed") or (tr or {}).get("suppressed"))
            if unit["suppressed"]:
                unit["source"] = "suppressed"
            else:
                # 来源按 lad.model 精确区分：AI 产物 / 旧存盘 translation-local（摘句，
                # readiness 不认）/ pending（空层待生成）。空 model 不冒充 local。
                model_label = str((lad.get("model") or ""))
                if model_label == "local":
                    unit["source"] = "local"
                elif model_label == "":
                    unit["source"] = "pending"
                else:
                    unit["source"] = "model"
            # per-unit 错误元数据透出（apply_derived_reply 落在 tr[locale]["error"]）。
            unit_error = tr.get("error") if isinstance(tr, dict) else None
            if unit_error:
                unit["error"] = dict(unit_error)
            if not unit.get("topic") and unit.get("takeaway"):
                unit["topic"] = unit["takeaway"][:12]
            continue
        if source is not None:
            unit["full"] = tf_tokens.plain(source["toks"], doc["atoms"])
        unit["brief_text"] = joiner.join(unit.get("brief") or [])
        unit["suppressed"] = bool((source or {}).get("suppressed"))
        unit["source"] = (source or {}).get("source") or ("suppressed" if unit["suppressed"] else "local")
        # 原文层的 per-unit 错误元数据透出（apply_reply 落在 unit["error"]）。
        source_error = (source or {}).get("error")
        if source_error:
            unit["error"] = dict(source_error)
        # 空 topic 但确有 takeaway：退回首几个字（上游 local() 在极短标题上可能给空 topic，
        # 界面会出现空主题卡）。这是适配层兜底，不改上游算法。
        if not unit.get("topic") and unit.get("takeaway"):
            unit["topic"] = unit["takeaway"][:12]
    sources = {unit["source"] for unit in value["units"]}
    value["source"] = (
        "model" if sources == {"model"} else
        "suppressed" if sources == {"suppressed"} else
        "local" if "model" not in sources else
        "mixed"
    )
    return value


def _cjk_display(doc: dict[str, Any], chunk: dict[str, Any], lang: str) -> bool:
    """译文层的显示语言是否 CJK（决定 brief_text 是否加空格）：按该节译文文本判。"""
    texts = []
    for uid in chunk["units"]:
        tr = ((doc["units"].get(uid) or {}).get("tr") or {}).get(lang)
        if tr is not None:
            texts.append(tf_tokens.plain(tr["toks"], doc["atoms"]))
    text = "".join(texts)[:4000]
    words = [c for c in text if c.isalnum() or tf_ladder.is_cjk(c)]
    return bool(text) and (sum(tf_ladder.is_cjk(c) for c in words) / len(words)) > 0.2 if words else False


def format_doc(
    doc: dict[str, Any],
    *,
    project_id: str,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """整份 doc 投影成 `textfold/doc`（version 0）。字段与上游 `fold.document()` 一致，
    只少了 Paper 元数据（authors/date/source.url），Literature 没有对应事实。"""
    return {
        "format": "textfold/doc",
        "version": 0,
        "project_id": project_id,
        "lang": doc["meta"].get("lang", ""),
        "title": doc["meta"].get("title", ""),
        "levels": list(tf_fold.LEVELS),
        "written": [level for level in tf_fold.LEVELS if tf_fold.WRITTEN[level]],
        "provenance": provenance,
        "sections": [section_view(doc, chunk) for chunk in doc["chunks"] if chunk["units"]],
    }


def section_prompt(doc: dict[str, Any], chunk: dict[str, Any]) -> tuple[str, str]:
    """上游折叠提示词 + 上游系统提示词（含「正文不得覆盖指令」的边界）。"""
    system = tf_llm.system_for(doc["meta"])
    prompt = tf_ladder.prompt_for(doc, chunk)
    # Footnote/citation numbers are document text, never sentence identities.
    bounds = "; ".join(f"U{i}: 0..{len(doc['units'][uid].get('sents') or []) - 1}"
                       for i, uid in enumerate(chunk['units']))
    prompt += ("\nEvidence indexing: use ONLY the zero-based sentence labels printed at the start "
               "of each sentence in that unit. Footnote superscripts, citation numbers, years and "
               "page numbers inside the sentence are NOT evidence IDs. Valid evidence ranges: "
               + bounds + ". Return 1-3 valid sentence indices within that unit's range.")
    return system, prompt


def prompt_schema() -> dict[str, Any]:
    return tf_ladder.SCHEMA


def unit_ids_in_prompt_order(chunk: dict[str, Any]) -> list[str]:
    """提示词里的 unit 以 `[U0] [U1] …` 编号，回复也按这个编号引用。"""
    return [f"U{index}" for index in range(len(chunk["units"]))]


def reply_units(out: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把模型回复里的 units 按提示词编号索引（容忍上游 `units_of` 认得的各种写法）。"""
    return {str(item.get("id") or ""): item for item in tf_llm.units_of(out)}


def apply_reply(
    doc: dict[str, Any],
    chunk: dict[str, Any],
    out: dict[str, Any],
    *,
    model: str,
    finalize: bool = True,
) -> list[str]:
    """把一次章节回复落到 ladder 上，返回告警文本（缺段 / 超预算）。

    逐条复用上游 `assemble`：evidence 越界过滤、takeaway 必填、topic 必须能在 takeaway 里
    找到逐字片段（找不到时按上游规则退回头几个词），公式与原子由 `written()` 还原。

    Literature 严格化（无本地摘句回退，2026-10-27）：模型漏段 / evidence 全无效 /
    takeaway 为空的 unit 一律落 `pending_lad()` 未生成占位（空短层、无来源、
    不标 suppressed 不标 model）——readiness 必须视为未完成，resume 只补这些段。
    绝不调用 `tf_ladder.local()`（正文摘句执行禁止出现在生产业务路径），也不在
    预检失败时调用 `assemble()`（上游 assemble 在 takeaway 为空时会自己退回 local）。
    每个 pending unit 带具体的 `unit["error"] = {code, message}` 元数据（见
    PENDING_ERROR_CODES），成功 unit 不带 error 键；投影经 section_view 透出。

    `finalize=False`（可选）：跳过本节的 `fold_background` / `dress` 收尾——这两步会
    改写**同章节内其他 unit** 的短层（后台降层 tn→0、跨页句缝合 glue），逐节收尾会让
    暂停边界上已落盘的节投影与最终全文不一致。core 在**全部章节回复落完后**调用一次
    `finalize_doc(doc)` 统一收尾（幂等）；`finalize=True`（默认）保持原有逐节收尾行为。
    """
    by_id = reply_units(out)
    warnings: list[str] = []
    cjk = doc.get("is_cjk", False)
    budget = tf_ladder.budgets_for({"units": doc["units"]})["takeaway"]
    for index, block_id in enumerate(chunk["units"]):
        unit = doc["units"][block_id]
        # 逐 unit 隔离：单段畸形回复（含非预期异常）只能让**该段**保持未生成，
        # 不能把同批其他合法 unit 一起拖死（批内主线程串行提交，异常会丢掉整批）。
        try:
            message = _apply_unit_reply(
                doc, unit, by_id.get(f"U{index}"), model=model, cjk=cjk, budget=budget,
            )
        except Exception as error:  # noqa: BLE001 - 边界隔离，不猜造内容
            _mark_pending(
                unit, "unit_apply_error",
                f"{block_id}：应用该段回复时异常（{type(error).__name__}: {str(error)[:120]}），"
                "保持未生成（resume 补齐）",
            )
            message = unit["error"]["message"]
        if message:
            warnings.append(message)
    section, section_problem = section_ladder_safe(out.get("section"), 3, doc, chunk)
    if section_problem:
        # 章节字段类型坏只影响摘要（保持未生成），不影响已经落好的逐 unit 短层。
        warnings.append(section_problem)
    if section:
        chunk["lad"] = section
    if finalize:
        tf_ladder.fold_background(chunk, doc)
        tf_ladder.dress(chunk, doc)
    return warnings


# pending unit 的错误元数据 code 集合（apply_reply / apply_derived_reply 共用口径）。
PENDING_ERROR_CODES = (
    "missing_reply",       # 模型没返回该段
    "invalid_evidence",    # evidence 全部越界/无效，不可追溯
    "empty_takeaway",      # takeaway 为空
    "assemble_local",      # 防御分支：assemble 异常落到本地折叠
    "field_type_invalid",  # 必填字段类型非法（如 takeaway 是 list/dict），不 str() 强转
    "unit_apply_error",    # 该段应用时抛异常（逐 unit 隔离兜底，具体原因在 message）
    "empty_ev_sentences",  # 派生回复 ev_sentences 为空
    "bad_ev_sentences",    # 派生回复 ev_sentences 元素不是字符串（不可采信）
    "ev_sentences_mismatch",  # 派生 ev_sentences 数量与 evidence 槽位不一致（不猜对应）
)


def _apply_unit_reply(
    doc: dict[str, Any], unit: dict[str, Any], reply: dict[str, Any] | None, *,
    model: str, cjk: bool, budget: int,
) -> str:
    """把本批回复里的**一段**落到 unit 上；返回告警文本（无告警返回空串）。

    只处理这一段：任何校验失败都落 `pending_lad()` + 具体 error code 并返回告警，
    绝不抛给批提交（批是串行提交的，一个畸形段不能污染同批其他合法段）。
    字段类型边界：`takeaway` 非 str（list/dict/数字…）按 `field_type_invalid` 未生成；
    `reason`/`topic` 非 str 按缺失处理（上游对缺失的兜底是确定性的，不猜造内容）。
    """
    block_id = str(unit.get("id") or "")
    if tf_tokens.words(unit["toks"]) < MIN_FOLD_WORDS:
        unit["lad"] = tf_ladder.suppressed()
        unit["suppressed"] = True
        unit["source"] = "suppressed"
        unit.pop("error", None)
        return ""
    if not reply:
        _mark_pending(unit, "missing_reply", f"{block_id}：模型未返回该段，保持未生成（resume 补齐）")
        return unit["error"]["message"]
    takeaway = reply.get("takeaway")
    if takeaway is not None and not isinstance(takeaway, str):
        _mark_pending(
            unit, "field_type_invalid",
            f"{block_id}：takeaway 类型非法（{type(takeaway).__name__}），保持未生成（不猜造内容，resume 补齐）",
        )
        return unit["error"]["message"]
    # 上游 `assemble()` 在 evidence 全无效时会退到 `[0]`（容忍模型漏引）。Literature 要求
    # 「写出来的每一行都能被点回原文句子」，所以这里更严格：evidence 全无效 = 这段不可追溯，
    # 按未生成处理（不回退本地折叠），留待 resume 补齐。
    sentences_count = len(unit["sents"])
    valid = [int(value) for value in (reply.get("evidence") or [])
             if isinstance(value, (int, float)) and not isinstance(value, bool)
             and 0 <= int(value) < sentences_count]
    if not valid:
        _mark_pending(unit, "invalid_evidence",
                      f"{block_id}：evidence 全部无效，保持未生成（不可追溯的模型行不采用，resume 补齐）")
        return unit["error"]["message"]
    # 预检 takeaway 必须非空：上游 `assemble()` 在 takeaway 为空时会自己退回 local，
    # 所以空 takeaway 在进 assemble 之前就拦截为未生成。
    if not tf_ladder.clean(takeaway):
        _mark_pending(unit, "empty_takeaway", f"{block_id}：缺少 takeaway，保持未生成（resume 补齐）")
        return unit["error"]["message"]
    payload, problems = _normalize_optional_fields(reply)
    unit["lad"] = tf_ladder.assemble(unit, payload, doc["atoms"], model, cjk)
    # 防御：预检后 assemble 不应再落到 local；万一出现按未生成处理（不展示摘句）。
    if unit["lad"].get("model") == "local":
        _mark_pending(unit, "assemble_local", f"{block_id}：assemble 落到本地折叠（异常），按未生成处理")
        return unit["error"]["message"]
    unit["source"] = "model"
    unit.pop("error", None)
    notes: list[str] = []
    used = tf_tokens.words(unit["lad"]["brief"][: unit["lad"]["tn"]])
    if used > budget + (15 if cjk else 4):
        notes.append(f"{block_id}：takeaway 超预算（{used} {'字' if cjk else '词'}）")
    if problems:
        notes.append(f"{block_id}：" + "；".join(problems))
    return "；".join(notes)


def _normalize_optional_fields(reply: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """可选字段类型归一：`reason`/`topic` 非 str 按**缺失**处理（不 str() 强转、不猜造）。

    上游对缺失 reason（不追加理由）与缺失 topic（从 takeaway 头部确定性定位）都有既存语义，
    所以剔除非法类型与「模型没写」等价，可追溯且不会伪造内容。返回 (归一后的回复, 问题描述)。
    """
    payload = dict(reply)
    problems: list[str] = []
    for key in ("reason", "topic"):
        raw = payload.get(key)
        if raw is None or isinstance(raw, str):
            continue
        payload.pop(key, None)
        problems.append(f"{key} 类型非法（{type(raw).__name__}），按缺失处理")
    return payload, problems


def section_ladder_safe(
    value: Any, topic_budget: int, doc: dict[str, Any] | None = None, chunk: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """章节短层的类型边界：畸形 `section` 只让**摘要保持未生成**，不抛异常、不猜造。

    上游 `section_ladder` 只做 `isinstance(o, str)`/`isinstance(o, dict)` 判断，dict 里的
    `takeaway`/`topic` 若是 list/dict 会在 `clean()` 里抛 TypeError——那会连带整批 unit
    提交失败。这里在调用上游之前把非法**类型**归一为缺失（缺失语义上游已有），并对上游
    自身可能抛出的异常兜底。返回 `(lad 或 None, 问题描述)`。
    """
    payload: Any
    if isinstance(value, str) or value is None:
        payload = value
        problem = ""
    elif isinstance(value, dict):
        payload = dict(value)
        problems: list[str] = []
        for key in ("takeaway", "topic"):
            raw = payload.get(key)
            if raw is None or isinstance(raw, str):
                continue
            payload.pop(key, None)
            problems.append(f"section.{key} 类型非法（{type(raw).__name__}），按缺失处理")
        problem = "；".join(problems)
    else:
        payload = None
        problem = f"section 类型非法（{type(value).__name__}），摘要保持未生成"
    if payload is None or payload == "":
        return None, problem
    try:
        lad = tf_ladder.section_ladder(payload, topic_budget, doc, chunk)
    except Exception as error:  # noqa: BLE001 - 畸形输入只影响摘要，不能让整批提交失败
        detail = f"章节摘要解析失败（{type(error).__name__}: {str(error)[:120]}）"
        return None, "；".join(part for part in (problem, detail) if part)
    return lad, problem


def _mark_pending(unit: dict[str, Any], code: str, message: str) -> None:
    """落未生成占位 + 具体 per-unit 错误元数据（不冒充成功，也不冒充 suppressed）。"""
    unit["lad"] = pending_lad(unit)
    unit["source"] = "pending"
    unit["error"] = {"code": code, "message": message}


def _set_tr_error(unit: dict[str, Any], locale: str, code: str, message: str) -> None:
    """译文层 per-unit 错误元数据（只在真实 tr 字典上写；无 tr 时仅丢失元数据不致崩）。"""
    tr = (unit.get("tr") or {}).get(locale)
    if isinstance(tr, dict):
        tr["error"] = {"code": code, "message": message}


def finalize_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """全部章节回复落完后统一收尾（对每个 chunk 执行 fold_background + dress）。

    与「每个章节 apply_reply 时各自收尾」的区别：后台降层（tn→0）与跨页句缝合（glue）
    会改写同章节内其他 unit 的短层；core 用 apply_reply(..., finalize=False) 逐节落
    回复后，在全部章节完成（含暂停前的收尾点）调用本函数一次，保证整份 doc 的收尾
    语义一致。幂等：重复调用不叠加（两步都是按当前 lad 状态重算，不累积副作用）。"""
    for chunk in doc["chunks"]:
        tf_ladder.fold_background(chunk, doc)
        tf_ladder.dress(chunk, doc)
    return doc


def pending_lad(unit: dict[str, Any] | None = None) -> dict[str, Any]:
    """未生成占位层：空短层、无来源（model=""），投影只渲染 Full，不伪造高亮/摘句。

    与 `tf_ladder.suppressed()` 的语义区别：suppressed 是确定性判定（装置内容，
    MIN_FOLD_WORDS 判据），readiness 视为已处理；pending 是「模型没写出来 /
    不可追溯 / 待补」，readiness 必须视为未完成（resume 只补缺项，有效 AI 缓存不重计费）。"""
    return {
        "role": (unit or {}).get("role", "para") if isinstance(unit, dict) else "context",
        "ev": [], "brief": [], "tn": 0, "topic": [], "model": "",
    }


def ensure_ladders(doc: dict[str, Any]) -> None:
    """补齐缺失的 ladder：纯 Full 占位（空短层、无来源），不是本地摘句。

    上游 build() 结尾给全部 unit 兒底 local ladder；Literature 不执行正文摘句，
    缺 lad 的 unit 只投影 Full。注意不能用 `setdefault(local(...))`——那会连已有 lad
    的 unit 也先求值生成一次 local（纯浪费且违反禁令），所以显式判 key。"""
    for unit in doc["units"].values():
        if "lad" not in unit:
            unit["lad"] = pending_lad(unit)


def markdown_view(layers_doc: dict[str, Any], level: str = "full", section: str | None = None) -> str:
    """把已生成的分层内容渲染成 Markdown（`fold.markdown` 原样复用）。"""
    if level not in tf_fold.LEVELS:
        raise ValueError(f"level is one of {', '.join(tf_fold.LEVELS)}")
    d = {
        "title": layers_doc.get("title", ""),
        "authors": [],
        "source": {"label": "", "url": ""},
        "provenance": {
            "model": (layers_doc.get("provenance") or {}).get("model", ""),
            "local": (layers_doc.get("provenance") or {}).get("local", False),
        },
        "sections": layers_doc.get("sections") or [],
    }
    return tf_fold.markdown(d, level=level, which=section)


def citation_guard(prompt: str, *, max_chars: int) -> str:
    """上下文上限：TextFold 的 `plain()` 会把公式还原成 `$…$`，字符数会略多于原文，这里只做最终夹取。"""
    if len(prompt) <= max_chars:
        return prompt
    return prompt[:max_chars] + "\n…（上下文已按上限截断）"


__all__ = [
    "SOURCE_LOCALE", "UNIT_TYPES",
    "build_ladder_doc", "unit_view", "section_view", "format_doc",
    "section_prompt", "prompt_schema", "unit_ids_in_prompt_order", "reply_units",
    "apply_reply", "pending_lad", "ensure_ladders", "markdown_view", "split_sentences",
    "citation_guard", "tf_fold", "tf_ladder", "tf_tokens", "tf_llm", "tf_langs",
    "upstream_lang", "merge_translation_doc", "trans_reply_schema", "trans_units_in_section",
    "derive_context", "derive_reply_schema", "apply_derived_reply", "_glossary_hash", "ensure_trans_lads",
    "finalize_doc", "PENDING_ERROR_CODES", "section_ladder_safe",
]
