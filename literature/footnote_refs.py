"""页下注 → 目标文档真实脚注（EPUB3 noteref/aside、DOCX w:footnoteReference）。

只做纯函数计算，不写文件、不碰 raw/document/translations。输入是按阅读顺序排列的
``Block`` 列表，输出一个 :class:`FootnotePlan`：哪些页下注块绑到了正文里的哪个
上标标记（MinerU 输出正文里的 ``^{N}`` 形态），供导出器改写渲染。

匹配规则（保守，宁缺毋滥）：
- 页下注块编号：``^{N} …`` 或 ``N. …``（N 为 1-4 位数字）。
- 正文标记：``^{N}``、``^{N,M}``（纯数字、逗号/顿号分隔）；equation/code/footnote
  块不扫描（公式上标与注释自身不是引用）。
- 绑定：按阅读顺序扫描，遇到页下注块时绑到**所有**仍未绑定的同号标记
  （同号重复引用共享同一注文；逐页编号时先前的标记早已各自绑定，不会误绑）；
  无编号、或正文没有对应标记的页下注保持原样渲染。
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


def superscript_content(raw: str) -> str:
    """上标内部文本：去掉 ``\\text{th}`` 这类 LaTeX 包装，只留可读内容。"""
    value = _SUP_LATEX_WRAP_RE.sub(r"\1", raw or "")
    return value.replace("\\circ", "°").strip()
_NOTE_LEADING_BRACE_RE = re.compile(r"^\^\{([0-9]{1,4})\}\s*")
_NOTE_LEADING_NUM_RE = re.compile(r"^([0-9]{1,4})[.．]\s*")

# 参与标记扫描/可承载脚注锚点的正文块类型。
_HOST_TYPES = frozenset({"paragraph", "text", "list", "title"})


@dataclass(slots=True)
class Ref:
    """正文里的一处上标标记（note_block_id 绑定后回填，故不可 frozen）。"""

    number: str
    host_block_id: str
    start: int
    end: int
    note_block_id: str = ""  # 绑定后回填


@dataclass(slots=True)
class NoteLink:
    """一个页下注块及其绑定的正文标记。"""

    block: Block
    number: str
    refs: list[Ref] = field(default_factory=list)

    @property
    def block_id(self) -> str:
        return self.block.block_id

    @property
    def text(self) -> str:
        """去掉引导性 ``^{N}`` 标记后的注文（保留原有 ``N.`` 前缀与全部内容）。"""
        return _NOTE_LEADING_BRACE_RE.sub("", self.block.source_text or "", count=1)

    @property
    def body(self) -> str:
        """纯注文：连印刷编号前缀也去掉（Word 用自定义标记单独显示编号，不重复）。"""
        return _NOTE_LEADING_NUM_RE.sub("", self.text, count=1)


@dataclass(slots=True)
class FootnotePlan:
    links: dict[str, NoteLink] = field(default_factory=dict)  # 注块 block_id → NoteLink
    refs_by_host: dict[str, list[Ref]] = field(default_factory=dict)  # 正文块 block_id → refs（按出现顺序）

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


def note_number(source_text: str) -> tuple[str, int]:
    """注块编号与引导标记长度；``^{N} …`` / ``N. …``，否则 ("", 0)。"""
    text = source_text or ""
    match = _NOTE_LEADING_BRACE_RE.match(text)
    if match:
        return match.group(1), match.end()
    match = _NOTE_LEADING_NUM_RE.match(text)
    if match:
        return match.group(1), 0  # ``N.`` 是注文自身的一部分，不删
    return "", 0


def build_plan(blocks: list[Block]) -> FootnotePlan:
    plan = FootnotePlan()
    open_refs: list[Ref] = []

    def record(number: str, block_id: str, start: int, end: int) -> None:
        open_refs.append(Ref(number=number, host_block_id=block_id, start=start, end=end))

    for block in blocks:
        if block.type == "footnote":
            number, _ = note_number(block.source_text)
            if number:
                bound = [ref for ref in open_refs if ref.number == number and not ref.note_block_id]
                if bound:
                    for ref in bound:
                        ref.note_block_id = block.block_id
                        plan.refs_by_host.setdefault(ref.host_block_id, []).append(ref)
                    plan.links[block.block_id] = NoteLink(block=block, number=number, refs=bound)
            continue
        if block.type not in _HOST_TYPES:
            continue
        for text, base, _end in _iter_host_texts(block):
            for match in MARKER_RE.finditer(text):
                for part in re.split(r"[,,、]", match.group(1)):
                    record(part, block.block_id, base + match.start(), base + match.end())
    for refs in plan.refs_by_host.values():
        refs.sort(key=lambda ref: (ref.start, ref.end))
    return plan


def annotate(text: str, plan: FootnotePlan, block: Block, role: str,
             by_offset: bool | None = None) -> list[tuple[str, str, str]]:
    """把文本切成渲染段：``("text", 片段, "")`` 或 ``("ref", 编号, 注块id)``。

    - ``by_offset=True``（默认 source 角色）：按计划里的偏移精确替换
      （含 ``^{N,M}`` 逐号拆分），要求 text 与扫描时同源同长。
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
        for ref in refs:
            if ref.start < cursor or ref.end > len(text):
                continue
            if ref.start > cursor:
                segments.append(("text", text[cursor:ref.start], ""))
            segments.append(("ref", ref.number, ref.note_block_id))
            cursor = ref.end
        if cursor < len(text):
            segments.append(("text", text[cursor:], ""))
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
