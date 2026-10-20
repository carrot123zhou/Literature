"""页下注 → 目标文档真实脚注（EPUB3 noteref/aside、DOCX w:footnoteReference）。

只做纯函数计算，不写文件、不碰 raw/document/translations。输入是按阅读顺序排列的
``Block`` 列表，输出一个 :class:`FootnotePlan`：哪些页下注块绑到了正文里的哪个
上标标记（MinerU 输出正文里的 ``^{N}`` 形态），供导出器改写渲染。

匹配规则（先精确、后回退，宁缺毋滥）：
- 页下注编号：``^{N} …``、``¹…``（Unicode 上标）、``N. …``、``N␣…``（数字+空格+
  文字）、``*†‡§¶`` 记号。
- 正文标记：``^{N}``、``^{N,M}``（纯数字、逗号/顿号分隔）；equation/code/footnote
  块不扫描（公式上标与注释自身不是引用）。
- 精确绑定：按阅读顺序扫描，遇到页下注块时绑到**所有**仍未绑定的同号标记
  （同号重复引用共享同一注文；逐页编号时先前的标记早已各自绑定，不会误绑）。
- 续行合并：无编号、且明显接续上一条注文的块（小写开头/引号开头，或上一条注
  没有以句末标点收尾）并入上一条，不再单独成段。
- 页位回退锚定：有编号、但 MinerU 丢了正文上标（全书无 ``^{N}`` 的书很常见），
  绑到该注之前**最近的正文块**（paragraph/list/text）末尾，由导出器在锚点补回
  印刷编号的上标；找不到正文块时保持原样。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator

from .models import Block


MARKER_RE = re.compile(r"\^\{([0-9]{1,4}(?:[,,、][0-9]{1,4})*)\}")
# 任意 ``^{…}`` 上标（含分数 ``^{1/2}``、序数 ``^{th}``、指数 ``^{2}``）：未绑到脚注时
# 仍按印刷上标渲染，不把 MinerU 的 LaTeX 形态原样留在成品里。
SUPERSCRIPT_RE = re.compile(r"\^\{((?:[^{}\n]|\{[^{}\n]{1,24}\}){1,64})\}")

_SUP_LATEX_WRAP_RE = re.compile(r"\\(?:text|mathrm|rm|textrm|operatorname)\s*\{([^{}]*)\}")
_NOTE_LEADING_BRACE_RE = re.compile(r"^\^\{([0-9]{1,4})\}\s*")
_NOTE_LEADING_NUM_RE = re.compile(r"^([0-9]{1,4})[.．]\s*")
# ``15 On the dates…``：数字 + 空白 + 文字（排除 ``13 (Rome…``、``80–31`` 这类续行）。
_NOTE_LEADING_BARE_RE = re.compile(r"^([0-9]{1,3})\s+(?=[0-9A-Za-z\u4e00-\u9fff\u201c\"'‘“])")
_UNICODE_SUP_CHARS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_UNICODE_SUP_RE = re.compile(f"^([{_UNICODE_SUP_CHARS}]{{1,4}})\\s*")
_SYMBOL_LEAD_RE = re.compile(r"^([*†‡§¶])\s*")

# 参与标记扫描的正文块类型；回退锚定不含 title（注不挂在标题上）。
_HOST_TYPES = frozenset({"paragraph", "text", "list", "title"})
_FALLBACK_HOST_TYPES = frozenset({"paragraph", "text", "list"})

# 续行判定：这些开头的非编号注块视为上一条的续行（引号/括号收尾、破折号等）。
_CONTINUATION_PUNCT = frozenset("\"'”’）)]】»…—–-")
# 上一条注文以这些句末标点收尾时，下一条非编号注块不再视为续行。
_SENTENCE_END = (".", "。", "!", "?", "！", "？", "…", "”", "’", '"', "'", ")")


def superscript_content(raw: str) -> str:
    """上标内部文本：去掉 ``\\text{th}`` 这类 LaTeX 包装，只留可读内容。"""
    value = _SUP_LATEX_WRAP_RE.sub(r"\1", raw or "")
    return value.replace("\\circ", "°").strip()


@dataclass(slots=True)
class Ref:
    """正文里的一处上标标记（note_block_id 绑定后回填，故不可 frozen）。

    ``start == -1`` 表示回退锚点：正文里找不到标记（MinerU 丢失上标），由导出器
    把引用插到宿主块文本末尾。
    """

    number: str
    host_block_id: str
    start: int
    end: int
    note_block_id: str = ""  # 绑定后回填

    @property
    def is_fallback(self) -> bool:
        return self.start < 0


@dataclass(slots=True)
class NoteLink:
    """一个页下注块及其绑定的正文标记。"""

    block: Block
    number: str
    refs: list[Ref] = field(default_factory=list)
    merged: str = ""  # 吸收续行块后的完整注文（空串表示没有续行）

    @property
    def block_id(self) -> str:
        return self.block.block_id

    @property
    def raw_text(self) -> str:
        """合并续行后的注文原文（含编号前缀）。"""
        return self.merged or (self.block.source_text or "")

    @property
    def text(self) -> str:
        """去掉引导性 ``^{N}``/上标标记后的注文（保留 ``N.``/``N␣`` 前缀与全部内容）。"""
        return _strip_leading_label(self.raw_text)

    @property
    def body(self) -> str:
        """纯注文：连印刷编号前缀（``^{N}``/``¹``/``N.``/``N␣``/记号）也去掉，
        Word 用自定义标记单独显示编号，不重复。"""
        label, strip = _leading_label(self.raw_text)
        if strip:
            return _strip_leading_label(self.raw_text).lstrip()
        if label and self.text.startswith(label):
            rest = self.text[len(label):].lstrip()
            if label.isdigit() and rest[:1] in (".", "．", "。"):
                rest = rest[1:].lstrip()
            return rest
        return self.text


@dataclass(slots=True)
class FootnotePlan:
    links: dict[str, NoteLink] = field(default_factory=dict)  # 注块 block_id → NoteLink
    refs_by_host: dict[str, list[Ref]] = field(default_factory=dict)  # 正文块 block_id → refs（按出现顺序）
    absorbed: dict[str, str] = field(default_factory=dict)  # 续行块 block_id → 被并入的注块 block_id
    absorbed_blocks: dict[str, Block] = field(default_factory=dict)  # 续行块 block_id → Block

    @property
    def linked_note_ids(self) -> set[str]:
        return set(self.links)

    def host_first_note_ids(self) -> dict[str, list[str]]:
        """正文块 block_id → 该块锚定的注块 block_id（只挂每个注文的首锚，避免重复）。"""
        result: dict[str, list[str]] = {}
        for note_id, link in self.links.items():
            if not link.refs:
                continue
            result.setdefault(link.refs[0].host_block_id, []).append(note_id)
        return result

    def section_note_ids(self, blocks: list[Block]) -> list[str]:
        """一组正文块（通常是一个章节）锚定的注块 id，按首锚出现顺序去重。

        只收录首锚在该组块内的注文：同一注文被多章同号标记共享时，aside 只
        放在首锚所在章节，后续章节的引用走跨文件链接，避免重复 id。
        """
        first_host: dict[str, list[str]] = {}
        for note_id, link in self.links.items():
            if link.refs:
                first_host.setdefault(link.refs[0].host_block_id, []).append(note_id)
        ordered: list[str] = []
        seen: set[str] = set()
        for block in blocks:
            for note_id in first_host.get(block.block_id, []):
                if note_id not in seen:
                    seen.add(note_id)
                    ordered.append(note_id)
        return ordered

    def numbers_for_host(self, block_id: str) -> set[str]:
        return {ref.number for ref in self.refs_by_host.get(block_id, []) if ref.note_block_id}


def _iter_host_texts(block: Block) -> Iterator[tuple[str, int, int]]:
    """产出 (文本, 项内偏移基线, 结束)；list 块逐项扫描，其余块整段扫描。"""
    if block.type == "list":
        base = 0
        for item in block.items or []:
            yield item, base, base + len(item)
            base += len(item) + 1
    elif block.source_text:
        yield block.source_text, 0, len(block.source_text)


def _leading_label(source_text: str) -> tuple[str, int]:
    """注块的 (编号/记号, 引导标记剥离长度)；剥 0 表示前缀是注文的一部分，保留。"""
    text = source_text or ""
    match = _NOTE_LEADING_BRACE_RE.match(text)
    if match:
        return match.group(1), match.end()
    match = _UNICODE_SUP_RE.match(text)
    if match:
        return match.group(1).translate(str.maketrans(_UNICODE_SUP_CHARS, "0123456789")), match.end()
    match = _NOTE_LEADING_NUM_RE.match(text)
    if match:
        return match.group(1), 0  # ``N.`` 是注文自身的一部分，不删
    match = _NOTE_LEADING_BARE_RE.match(text)
    if match:
        return match.group(1), 0  # ``N␣`` 同样保留前缀
    match = _SYMBOL_LEAD_RE.match(text)
    if match:
        return match.group(1), match.end()
    return "", 0


def _strip_leading_label(source_text: str) -> str:
    label, strip = _leading_label(source_text)
    return source_text[strip:] if strip else source_text


def note_number(source_text: str) -> tuple[str, int]:
    """兼容入口：注块编号与 ``^{N}`` 引导标记长度；否则 ("", 0)。"""
    label, strip = _leading_label(source_text)
    if _NOTE_LEADING_BRACE_RE.match(source_text or ""):
        return label, strip
    return label, 0


def _looks_like_continuation(text: str, previous: str) -> bool:
    """非编号注块是否是上一条注文的续行。"""
    stripped = text.strip()
    if not stripped:
        return True
    first = stripped[0]
    if first.isalnum():
        if first.islower():
            return True
    elif first in _CONTINUATION_PUNCT:
        return True
    return bool(previous) and not previous.rstrip().endswith(_SENTENCE_END)


def build_plan(blocks: list[Block]) -> FootnotePlan:
    plan = FootnotePlan()
    open_refs: list[Ref] = []
    merged: dict[str, str] = {}
    last_note_id: str | None = None
    by_id = {block.block_id: block for block in blocks}

    def note_text(note_id: str) -> str:
        block = by_id.get(note_id)
        return merged.get(note_id) or (block.source_text or "" if block else "")

    for block in blocks:
        if block.type == "footnote":
            label, _ = _leading_label(block.source_text or "")
            if not label and last_note_id:
                text = (block.source_text or "").strip()
                if _looks_like_continuation(text, note_text(last_note_id)):
                    merged[last_note_id] = (note_text(last_note_id).rstrip() + " " + text).strip()
                    plan.absorbed[block.block_id] = last_note_id
                    plan.absorbed_blocks[block.block_id] = block
                    continue
            if label:
                bound = [ref for ref in open_refs if ref.number == label and not ref.note_block_id]
                if bound:
                    for ref in bound:
                        ref.note_block_id = block.block_id
                        plan.refs_by_host.setdefault(ref.host_block_id, []).append(ref)
                    plan.links[block.block_id] = NoteLink(
                        block=block, number=label, refs=bound, merged=merged.get(block.block_id, ""))
                last_note_id = block.block_id
            continue
        if block.type not in _HOST_TYPES:
            continue
        for text, base, _end in _iter_host_texts(block):
            for match in MARKER_RE.finditer(text):
                for part in re.split(r"[,,、]", match.group(1)):
                    open_refs.append(Ref(
                        number=part, host_block_id=block.block_id,
                        start=base + match.start(), end=base + match.end()))

    # 页位回退：有编号但正文标记丢失（或已被同号前注占用）的注，锚到最近的正文块末尾。
    body_positions = [i for i, item in enumerate(blocks) if item.type in _FALLBACK_HOST_TYPES]
    for index, block in enumerate(blocks):
        if block.type != "footnote" or block.block_id in plan.links or block.block_id in plan.absorbed:
            continue
        label, _ = _leading_label(merged.get(block.block_id) or (block.source_text or ""))
        if not label:
            continue
        host = next((blocks[i] for i in reversed(body_positions) if i < index), None)
        if host is None:
            continue
        ref = Ref(number=label, host_block_id=host.block_id, start=-1, end=-1, note_block_id=block.block_id)
        plan.refs_by_host.setdefault(host.block_id, []).append(ref)
        plan.links[block.block_id] = NoteLink(
            block=block, number=label, refs=[ref], merged=merged.get(block.block_id, ""))

    # 续行合并晚于部分 NoteLink 创建，统一回填合并后的注文。
    for note_id, link in plan.links.items():
        link.merged = merged.get(note_id, "")

    def sort_key(ref: Ref) -> tuple[int, int, int]:
        if ref.start < 0:
            return (1, 0, 0)  # 回退锚点永远排在该宿主块所有精确标记之后
        return (0, ref.start, ref.end)

    for refs in plan.refs_by_host.values():
        refs.sort(key=sort_key)
    return plan


def annotate(text: str, plan: FootnotePlan, block: Block, role: str,
             by_offset: bool | None = None) -> list[tuple[str, str, str]]:
    """把文本切成渲染段：``("text", 片段, "")`` 或 ``("ref", 编号, 注块id)``。

    - ``by_offset=True``（默认 source 角色）：按计划里的偏移精确替换
      （含 ``^{N,M}`` 逐号拆分），要求 text 与扫描时同源同长；回退锚点（start=-1）
      追加在文本末尾。
    - ``by_offset=False``（译文、list 逐项等非同源文本）：按编号匹配该块已绑定的
      注，匹配不到就原样保留。
    """
    if not text:
        return []
    if by_offset is None:
        by_offset = role == "source"
    if by_offset:
        refs = plan.refs_by_host.get(block.block_id) or []
        if not refs:
            return [("text", text, "")]
        segments: list[tuple[str, str, str]] = []
        cursor = 0
        fallbacks: list[Ref] = []
        for ref in sorted(refs, key=lambda r: (r.start < 0, max(r.start, 0), r.end)):
            if ref.start < 0:
                fallbacks.append(ref)
                continue
            if ref.start < cursor or ref.end > len(text):
                continue
            if ref.start > cursor:
                segments.append(("text", text[cursor:ref.start], ""))
            segments.append(("ref", ref.number, ref.note_block_id))
            cursor = ref.end
        if cursor < len(text):
            segments.append(("text", text[cursor:], ""))
        for ref in fallbacks:
            segments.append(("ref", ref.number, ref.note_block_id))
        return segments
    numbers = plan.numbers_for_host(block.block_id)
    if not numbers:
        return [("text", text, "")]
    queue: dict[str, list[str]] = {}
    for ref in plan.refs_by_host.get(block.block_id) or []:
        if ref.note_block_id:
            queue.setdefault(ref.number, []).append(ref.note_block_id)
    segments = []
    cursor = 0
    for match in MARKER_RE.finditer(text):
        parts = re.split(r"[,,、]", match.group(1))
        usable = [number for number in parts if number in numbers and queue.get(number)]
        if not usable:
            continue
        if match.start() > cursor:
            segments.append(("text", text[cursor:match.start()], ""))
        for number in usable:
            note_id = queue[number].pop(0)
            segments.append(("ref", number, note_id))
        cursor = match.end()
    if cursor < len(text):
        segments.append(("text", text[cursor:], ""))
    return segments
