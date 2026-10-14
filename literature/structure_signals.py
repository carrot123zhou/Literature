"""结构校对的本地信号层：在调用模型之前穷尽零成本的几何/重复/文本证据。

本模块是 "先本地、后模型" 策略的基础，全部纯函数、可解释、可审计，不发起任何
网络请求。它回答三个问题：

1. 哪些块是页眉/页脚/页码/题注/脚注，而不是标题（``detect_page_furniture`` 等）；
2. 印刷目录页在哪里，条目与层级是什么（``extract_printed_toc``）；
3. 目录条目应该对齐到哪些文档块（``align_printed_toc``，带序动态规划）。

这些证据随后进入 :mod:`literature.structure` 的对账与模型批次构造。
"""
from __future__ import annotations

import re
import json
from functools import lru_cache
from pathlib import Path
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from typing import Any, Iterable

from .models import Block, Document, Page

# MinerU content_list 的 bbox 是 0-1000 归一化坐标，页眉通常落在上 9%、页脚在下 9%。
TOP_ZONE = 90.0
BOTTOM_ZONE = 910.0

CAPTION_RE = re.compile(
    r"^\s*(?:fig(?:ure)?\.?|tab(?:le)?\.?|plate|chart|map|diagram|scheme|"
    r"图|圖|表|図)\s*[0-9一二三四五六七八九十]",
    re.IGNORECASE,
)
RUNNING_HEADER_RE = re.compile(r"^\s*(?:[ivxlcdm]{1,7}|\d{1,4})\s*[-–—·:：]\s*\S", re.IGNORECASE)
RUNNING_HEADER_SUFFIX_RE = re.compile(r"\s*[-–—·:：]\s*(?:[ivxlcdm]{1,7}|\d{1,4})\s*$", re.IGNORECASE)
CHAPTER_WORD_RE = re.compile(
    r"^\s*(?:part|chapter|book|section|appendix|"
    r"第[一二三四五六七八九十百零〇0-9]+[章节篇部卷]|"
    r"chapter\s+[ivxlcdm0-9]+)\b",
    re.IGNORECASE,
)
NUMBERED_HEADING_RE = re.compile(r"^\s*\d+(?:[.\-]\d+)*[.)]?\s+\S")
TERMINAL_PUNCT_RE = re.compile(r"[。！？!?；;]$")
SOFT_PUNCT_RE = re.compile(r"[,，、:：]$")
FOOTNOTE_MARK_RE = re.compile(r"^\s*(?:\d{1,3}|[*†‡])\s+\S")

TOC_MARKER_RE = re.compile(
    r"^\s*(?:table\s+of\s+contents|content|contents|目\s*次|目\s*录|目\s*錄)\s*$",
    re.IGNORECASE,
)
# 目录条目必须像标题。尾注/参考书目页同样满页“编号 + 页码”，但条目是长句书目：
# 实测真实目录条目中位长度 26–38、最长 62；尾注页中位 89–101、最长 815。
TOC_ENTRY_MAX_TITLE = 100
TOC_ENTRY_SHAPE_RATIO = 0.6
# 索引页的条目也是“短词 + 页码”（实测 ratio 0.92，光看形态区分不了），但索引带
# 注释引用（24n、188n）与 See/See also 交叉引用；真实目录页这些几乎为 0。
INDEX_NOTE_REF_RATIO = 0.25
INDEX_SEE_RATIO = 0.15
INDEX_NOTE_REF_RE = re.compile(r"\b\d{1,4}[a-z]{1,2}\b")
INDEX_SEE_RE = re.compile(r"\bSee(?:\s+also)?\b")
BIBLIO_ENTRY_RE = re.compile(
    r"(?:University Press|Verlag|https?://|\bIbid\b|\beds?\.\s+[A-Z]|"
    r"\btrans\.\s+[A-Z]|\bvol\.\s*\d|\bpp?\.\s*\d{1,4}\s*$)"
)
ILLUSTRATION_LIST_RE = re.compile(r"^(?:list of )?(?:maps|tables|figures|plates|illustrations|图表目录|插图目录|表目录|図表一覧)$", re.I)
ANY_PAGE_RE = re.compile(r"(?:^|[\s.·\t])(\d{1,4}|[ivxlcdm]{1,7})(?=[\s.·]|$)", re.IGNORECASE)
LEADER_RE = re.compile(r"[.·]{2,}")
CHAPTER_RE = re.compile(
    r"^\s*(?:chapter|part|book|appendix|section)\b|"
    r"^\s*第[一二三四五六七八九十百零〇0-9]+[章节篇部卷]",
    re.IGNORECASE,
)
FRONT_BACK_RE = re.compile(
    r"^\s*(?:acknowledg(?:e)?ments?|abbreviations|prologue|epilogue|"
    r"introduction|preface|foreword|glossary|bibliography|index|contributors|"
    r"notes|appendi(?:x|ces)|references|前言|序言|绪论|后记|参考书目|索引)\b",
    re.IGNORECASE,
)

# MinerU 会把脚注/行内公式标记泄漏进标题文本，例如
# "THE EMERGENCE OF THE TRICKSTER equation_inline^{1}"，直接匹配会失败。
INLINE_MARKER_RE = re.compile(r"(?:[A-Za-z_]+_inline|equation|table|image)_?\s*\^?\{[^}]*\}")
SUPERSCRIPT_RE = re.compile(r"\^?\{[^}]{1,40}\}")

MATCH_FLOOR = 0.58
GAP_PENALTY = -0.32


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\u00ad", "").replace("\u2010", "-").replace("\u2011", "-")
    return re.sub(r"\s+", " ", value).strip()


def strip_inline_markers(value: str) -> str:
    """去掉 MinerU 泄漏进文本的脚注/公式标记；这些标记会破坏标题匹配。"""
    value = INLINE_MARKER_RE.sub(" ", value or "")
    value = SUPERSCRIPT_RE.sub(" ", value)
    return normalize_text(value)


def block_text(block: Block) -> str:
    return strip_inline_markers(block.source_text or "\n".join(block.items))


def _norm_key(value: str) -> str:
    key = strip_inline_markers(value).casefold()
    key = RUNNING_HEADER_RE.sub("", key)
    key = RUNNING_HEADER_SUFFIX_RE.sub("", key)
    return re.sub(r"[\s\W_]+", "", key)


@dataclass
class Signal:
    block_id: str
    page_index: int
    text: str
    block_type: str
    level: int
    role_hint: str = "unknown"
    heading_score: float = 0.0
    level_hint: int | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id, "page_index": self.page_index, "text": self.text,
            "type": self.block_type, "level": self.level, "role_hint": self.role_hint,
            "heading_score": round(self.heading_score, 3), "level_hint": self.level_hint,
            "reasons": list(self.reasons),
        }


def _zone(block: Block) -> str:
    if len(block.bbox) != 4:
        return "body"
    if block.bbox[1] <= TOP_ZONE:
        return "top"
    if block.bbox[3] >= BOTTOM_ZONE:
        return "bottom"
    return "body"


def _looks_like_page_number(text: str) -> bool:
    return bool(re.fullmatch(r"[ivxlcdm]{1,7}|\d{1,4}", normalize_text(text), re.IGNORECASE))


def enrich_furniture_evidence(document: Document, project_path: Path) -> Document:
    """从封存 JSON 补充被规范化过滤的页眉证据；新旧项目均可读，不改 raw 或正文。"""
    from .mineru import span_text
    evidence: dict[int, list[dict[str, Any]]] = {}
    root = (project_path / "ocr" / "raw" / "mineru").resolve()
    for source in document.source_files:
        path = (project_path / str(source.get("path", ""))).resolve()
        if root not in path.parents or not path.is_file() or source.get("representation") not in {"content_list_v2", "content_list"}:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, list):
            continue
        offset = int(source.get("page_offset", 0))
        recorded_pages = source.get("source_pages")
        recorded_pages = [int(page) for page in recorded_pages] if isinstance(recorded_pages, list) else []

        def evidence_page(local_page: int) -> int:
            if recorded_pages and 0 <= local_page < len(recorded_pages):
                return recorded_pages[local_page] - 1
            return offset + local_page

        groups = enumerate(data) if source["representation"] == "content_list_v2" else ((int(item.get("page_idx", 0)), [item]) for item in data if isinstance(item, dict))
        for local_page, group in groups:
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, dict) or item.get("type") not in {"page_header", "page_footer", "header", "footer"}:
                    continue
                content = item.get("content")
                text = span_text(content.get(f"{item['type']}_content") or content) if isinstance(content, dict) else str(item.get("text") or content or "")
                bbox = item.get("bbox", [])
                if text.strip() and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
                    evidence.setdefault(evidence_page(local_page), []).append({"text": text, "bbox": bbox, "type": item["type"]})
    pages = {page.page_index: page for page in document.pages}
    for page_index, items in evidence.items():
        page = pages.get(page_index) or Page(page_index)
        pages[page_index] = replace(page, raw={**page.raw, "structure_furniture": items})
    return replace(document, pages=[pages[key] for key in sorted(pages)])


def detect_page_furniture(document: Document) -> dict[str, dict[str, Any]]:
    """识别跨页重复的页眉/页脚（MinerU 常把书眉误标成 title）。

    判据：归一化文本（去掉前导/尾随页码）在同一纵向区域出现在至少 3 个不同页。正文区反复出现的短句（如各章的 "Notes"）不在此列。
    """
    # 书眉常只在所属章节内重复（每 2-4 页一次），按全书比例会漏；固定下限 3 页，
    # 并用"主导区域非正文"过滤掉各章重复出现的真实小节标题（如 Notes）。
    threshold = 3
    occurrences: dict[str, set[int]] = {}
    zones: dict[str, Counter] = {}
    by_key: dict[str, list[Block]] = {}
    # canonical 过滤掉的正确页眉仍提供重复证据，帮助识别偶尔错标为 title 的同名书眉。
    observed = list(document.blocks)
    if document.bbox_space == "mineru-1000":
        for page in document.pages:
            for index, item in enumerate(page.raw.get("structure_furniture", [])):
                observed.append(Block(block_id=f"evidence:{page.page_index}:{index}", type="paragraph", order=-1,
                    page_index=page.page_index, source_text=item["text"], bbox=item["bbox"]))
    actual_ids = {block.block_id for block in document.blocks}
    for block in observed:
        key = _norm_key(block_text(block))
        if not key or len(block_text(block)) > 80:
            continue
        occurrences.setdefault(key, set()).add(block.page_index)
        zones.setdefault(key, Counter())[_zone(block)] += 1
        by_key.setdefault(key, []).append(block)

    result: dict[str, dict[str, Any]] = {}
    for key, pages in occurrences.items():
        if len(pages) < threshold:
            continue
        margin_zone = max(zones[key], key=zones[key].get)
        if margin_zone == "body":
            continue
        for block in by_key[key]:
            if block.block_id in actual_ids and _zone(block) == margin_zone:
                result[block.block_id] = {
                    "kind": "running_header" if margin_zone == "top" else "running_footer",
                    "pages": len(pages), "text": block_text(block),
                }
    return result


def classify_block(block: Block, furniture: dict[str, dict[str, Any]]) -> Signal:
    text = block_text(block)
    signal = Signal(block_id=block.block_id, page_index=block.page_index, text=text,
                    block_type=block.type, level=int(block.level or 0))
    if not text:
        signal.role_hint = "paragraph"
        return signal
    if block.block_id in furniture:
        signal.role_hint = "furniture"
        signal.reasons.append("跨页重复的页眉/页脚")
        return signal
    raw_kind = block.format.get("mineru_type") or block.raw.get("type")
    if raw_kind in {"page_header", "page_footer", "page_number"}:
        signal.role_hint = "furniture"
        signal.reasons.append("MinerU 原始页边类型（仍需确认）")
        return signal
    if block.type == "footnote" or raw_kind == "page_footnote":
        signal.role_hint = "footnote"
        signal.reasons.append("原始脚注类型")
        return signal
    if _looks_like_page_number(text):
        signal.role_hint = "page_number"
        signal.reasons.append("纯页码/罗马数字")
        return signal
    if len(block.bbox) == 4 and (block.bbox[0] >= 910 or block.bbox[2] <= 90) and block.bbox[2] - block.bbox[0] <= 40 and block.bbox[3] - block.bbox[1] >= 100:
        signal.role_hint = "furniture"
        signal.reasons.append("页面侧边窄长文字，疑似页边标题")
        return signal
    if CAPTION_RE.match(text):
        signal.role_hint = "caption"
        signal.reasons.append("题注形态（图/表编号开头）")
        return signal

    in_top, in_bottom = _zone(block) == "top", _zone(block) == "bottom"
    if (in_top or in_bottom) and (RUNNING_HEADER_RE.match(text) or RUNNING_HEADER_SUFFIX_RE.search(text)):
        signal.role_hint = "furniture"
        signal.reasons.append("页边区域且带页码/书眉编号")
        return signal
    if in_bottom and FOOTNOTE_MARK_RE.match(text) and (len(text) > 60 or bool(re.search(r"[。.;；]$", text))):
        signal.role_hint = "footnote"
        signal.reasons.append("页脚区域且以注释编号开头")
        return signal

    score = 0.0
    if block.type == "title":
        score += 0.55
        signal.reasons.append("MinerU 已标为标题")
    length = len(text)
    if length <= 80:
        score += 0.08
    elif length <= 120:
        score += 0.02
    else:
        score -= 0.45
        signal.reasons.append("文本过长，难以是标题")
    if TERMINAL_PUNCT_RE.search(text):
        score -= 0.35
        signal.reasons.append("以句末标点结尾")
    elif SOFT_PUNCT_RE.search(text):
        score -= 0.12
        signal.reasons.append("以逗号/冒号结尾")
    if text.endswith(("?", "？")):
        score += 0.04
    if text and text.upper() == text and any(ch.isalpha() for ch in text):
        score += 0.16
        signal.reasons.append("全大写")
    if CHAPTER_WORD_RE.match(text):
        score += 0.22
        signal.reasons.append("含章节/部卷编号")
    elif NUMBERED_HEADING_RE.match(text):
        score += 0.12
        signal.reasons.append("以编号开头")
    if len(block.bbox) == 4:
        height = block.bbox[3] - block.bbox[1]
        if height <= 34:
            score += 0.05
        elif height >= 60:
            score -= 0.10
            signal.reasons.append("行高偏大，可能多行")
    if "\n" in block.source_text:
        score -= 0.30
        signal.reasons.append("块内包含换行，可能被合并")
    if in_top or in_bottom:
        score -= 0.20
        signal.reasons.append("位于页边区域")

    signal.heading_score = max(0.0, min(1.0, score))
    signal.role_hint = "heading" if signal.heading_score >= 0.5 else "paragraph"

    number = re.match(r"^\s*(\d+(?:\.\d+)*)", text)
    if number:
        signal.level_hint = min(6, number.group(1).count(".") + 1)
    elif CHAPTER_WORD_RE.match(text):
        signal.level_hint = 1
    return signal


def build_signals(document: Document) -> tuple[list[Signal], dict[str, dict[str, Any]]]:
    # 无几何或其他坐标契约时，不可把未知坐标当作页面比例。
    blocks = document.blocks if document.bbox_space == "mineru-1000" else [replace(block, bbox=[]) for block in document.blocks]
    view = replace(document, blocks=blocks)
    furniture = detect_page_furniture(view)
    return [classify_block(block, furniture) for block in blocks], furniture


# --------------------------------------------------------------------------- 目录


@dataclass
class TocEntry:
    title: str
    level: int
    printed_page: str = ""
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "level": self.level, "printed_page": self.printed_page, "level_source": "heuristic"}


def _entry_boundaries(text: str) -> list[re.Match]:
    """目录行里像"条目结束页码"的位置：后一个 token 应是新条目开头。

    紧跟 "." 或 ")" 的数字是列表编号（1. Introduction），不是页码；页码后若是大写字、
    引号、新的数字编号或行尾，才算条目边界。
    """
    valid: list[re.Match] = []
    for match in ANY_PAGE_RE.finditer(text):
        before = text[:match.start()].strip()
        if not before or re.search(r"(?:chapter|part|book|section|appendix)\s*$", before, re.I):
            continue
        if text[match.end():match.end() + 1] in {".", ")"}:
            continue
        after = text[match.end():].lstrip(" .·•\t")
        if after == "" or after[0].isupper() or after[0] in "“\"'（(" or re.match(r"^\d+[.)]\s", after):
            valid.append(match)
    return valid


def _split_collapsed(text: str) -> list[str]:
    """从被折叠成单行的目录文本中恢复条目边界。

    Literature 的规范化会把块内换行折成空格。只有当一块里存在 >=2 个条目边界时才拆分，
    避免把"标题 + 页码 + 作者"的单条目误拆；单条目原样返回，交给整行解析。
    """
    boundaries = _entry_boundaries(text)
    stripped = text.strip()
    if len(boundaries) < 2:
        return [stripped] if stripped else []
    segments: list[str] = []
    previous = 0
    for match in boundaries:
        segment = text[previous:match.end()].strip()
        if segment:
            segments.append(segment)
        previous = match.end()
    tail = text[previous:].strip()
    if tail:
        segments.append(tail)
    return segments


def _page_lines(document: Document, page_index: int) -> list[str]:
    from .mineru import span_text
    lines: list[str] = []
    illustration_list = False
    for block in document.blocks:
        if block.page_index != page_index:
            continue
        if block.type in {"footnote", "image", "table", "equation", "code"} or block.format.get("mineru_type") in {"page_header", "page_footer", "page_number"}:
            continue
        text = block.source_text or "\n".join(block.items)
        # canonical 会折叠换行；原始 span 可以恢复目录行，但不能覆盖人工修改的文本。
        content = block.raw.get("content")
        if isinstance(content, dict):
            raw_text = span_text(content.get("paragraph_content") or content.get("title_content") or [])
            if normalize_text(raw_text) == normalize_text(text):
                text = raw_text
        if TOC_MARKER_RE.match(normalize_text(text)):
            continue
        if ILLUSTRATION_LIST_RE.match(normalize_text(text)):
            illustration_list = True
            continue
        if illustration_list:
            continue
        if "\n" in text:
            lines.extend(strip_inline_markers(raw) for raw in text.splitlines() if raw.strip())
        else:
            lines.extend(_split_collapsed(strip_inline_markers(text)))
    return lines


def _toc_entry_shape(title: str, trailing: str) -> bool:
    """目录条目必须像标题：短、不以句读结尾、不是书目引用。"""
    title = title.strip(" .·•\t")
    if not title or len(title) > TOC_ENTRY_MAX_TITLE:
        return False
    if TERMINAL_PUNCT_RE.search(title):
        return False
    return not (BIBLIO_ENTRY_RE.search(title) or BIBLIO_ENTRY_RE.search(trailing))


def _page_looks_like_toc(document: Document, page_index: int) -> bool:
    lines = _page_lines(document, page_index)
    if len(lines) < 4:
        return False
    if _page_looks_like_index(lines):
        return False
    refs = shaped = 0
    for line in lines:
        parsed = _page_reference(line)
        if parsed is None:
            continue
        refs += 1
        if _toc_entry_shape(parsed[0], parsed[2]):
            shaped += 1
    if refs < 3 or refs / len(lines) < 0.4:
        return False
    # 尾注/参考书目页也满足“多行带页码”，但条目多数不像标题。
    return shaped / refs >= TOC_ENTRY_SHAPE_RATIO


def _page_looks_like_index(lines: list[str]) -> bool:
    """索引页：条目标注注释引用（24n）或 See/See also 交叉引用。目录页几乎不会。"""
    total = len(lines)
    if total < 4:
        return False
    notes = sum(1 for line in lines if INDEX_NOTE_REF_RE.search(line))
    if notes / total >= INDEX_NOTE_REF_RATIO:
        return True
    see = sum(1 for line in lines if INDEX_SEE_RE.search(line))
    return see / total >= INDEX_SEE_RATIO


def toc_marker_pages(document: Document) -> list[int]:
    """含显式目录标记（contents / content / 目录 / 目次）的页。

    显式标记是“这页真的是目录”的强证据：本命中的页可以直接采信本地解析结果，
    不必再花一次模型请求去“核对目录”。无标记（靠行扫描猜出来）的页不做此保证。
    """
    by_page: dict[int, list[Block]] = {}
    for block in document.blocks:
        by_page.setdefault(block.page_index, []).append(block)
    return [page for page in sorted(by_page)
            if any(TOC_MARKER_RE.match(normalize_text(block.source_text)) for block in by_page[page])]


def find_toc_pages(document: Document) -> list[int]:
    """定位印刷目录页：优先显式目录标记，其次连续多行带页码引用的页。"""
    by_page: dict[int, list[Block]] = {}
    for block in document.blocks:
        by_page.setdefault(block.page_index, []).append(block)
    marked = toc_marker_pages(document)
    if marked:
        pages = [marked[0]]
        cursor = marked[0] + 1
        while len(pages) < 8 and cursor < document.page_count and _page_looks_like_toc(document, cursor):
            pages.append(cursor)
            cursor += 1
        return pages
    ref_pages = [page for page in sorted(by_page) if _page_looks_like_toc(document, page)]
    runs: list[list[int]] = []
    for page in ref_pages:
        if runs and page == runs[-1][-1] + 1:
            runs[-1].append(page)
        else:
            runs.append([page])
    best = max(runs, key=len, default=[])
    return best if len(best) >= 2 else []


def _page_reference(line: str) -> tuple[str, str, str] | None:
    matches = list(ANY_PAGE_RE.finditer(line))
    if not matches:
        return None
    match = matches[-1]
    title = line[: match.start()].strip(" .·•\t")
    return title, match.group(1), line[match.end():].strip(" .·•\t")


def _looks_like_author(text: str) -> bool:
    if not text:
        return True
    words = re.findall(r"[A-Za-z][\w'’.-]*", text)
    if not words:
        return True
    connectors = {"and", "of", "the", "de", "van", "von", "jr", "sr"}
    named = [word for word in words if word.lower() not in connectors]
    return len(named) <= 6 and all(word[0].isupper() or word.isupper() for word in named)


def _infer_level(title: str) -> int:
    numbered = re.match(r"^(\d+(?:\.\d+)+)\.?\s", title)
    if numbered:
        return min(6, numbered.group(1).count(".") + 1)
    if re.match(r"^[IVXLCDM]+[.)]\s", title):
        return 1
    if CHAPTER_RE.match(title) or FRONT_BACK_RE.match(title):
        return 1
    if re.match(r"^\d+[.)]\s+\S", title) and not re.match(r"^\d+\.\d+", title):
        return 1
    return 2


def parse_toc_lines(lines: Iterable[str]) -> list[TocEntry]:
    entries: list[TocEntry] = []
    pending = ""
    for raw in lines:
        line = raw.strip()
        if not line or TOC_MARKER_RE.match(line):
            continue
        parsed = _page_reference(line)
        if parsed is None:
            if len(line) > 120 or re.search(r"[。.;；]$", line) or _looks_like_page_number(line):
                pending = ""
                continue
            pending = f"{pending} {line}".strip() if pending else line
            continue
        title, page, trailing = parsed
        if not _looks_like_author(trailing):
            pending = f"{pending} {line}".strip() if pending else line
            continue
        title = LEADER_RE.sub(" ", title).strip()
        if pending:
            title = f"{pending} {title}".strip()
            pending = ""
        title = title.strip(" .·•\t")
        if title and len(title) <= 300:
            entries.append(TocEntry(title=title, level=_infer_level(title), printed_page=page))
    # 没有页码的尾行可能是书眉或正文，不能直接造目录条目。
    for index, entry in enumerate(entries):
        entry.order = index
    return entries


def extract_printed_toc(document: Document, pages: Iterable[int] | None = None) -> tuple[list[TocEntry], list[int]]:
    chosen = sorted({int(page) for page in pages}) if pages is not None else find_toc_pages(document)
    lines: list[str] = []
    for page in chosen:
        lines.extend(_page_lines(document, page))
    return parse_toc_lines(lines), chosen


# --------------------------------------------------------------------------- 对齐


@lru_cache(maxsize=16384)
def heading_key(value: str) -> str:
    value = strip_inline_markers(value).casefold()
    value = re.sub(r"^\s*(?:chapter|part|book|section|appendix)\s+(?:[ivxlcdm]+|\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b[.:]?\s*", " ", value)
    value = re.sub(r"^\s*[ivxlcdm]+[.)]\s+", " ", value)
    value = re.sub(r"第[一二三四五六七八九十百零〇0-9]+[章节篇部卷]", " ", value)
    value = re.sub(r"^\s*\d+(?:[.)]\d+)*[.)]?\s*", " ", value)
    value = re.sub(r"^\s*(?:\d+|[ivxlcdm]+)\s*[-–—·:：]\s*", " ", value)
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


@lru_cache(maxsize=65536)
def heading_similarity(left: str, right: str) -> float:
    a, b = heading_key(left), heading_key(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # SequenceMatcher.ratio() 的上界是 2*min/(len_a+len_b)。低于匹配下限时
    # 调用方只会把它当成“不匹配”（对齐 DP 用 MATCH_FLOOR 做阈值），直接返回上界
    # 可以跳过昂贵的逐字符比较，且不改变任何决策。
    upper = 2.0 * min(len(a), len(b)) / (len(a) + len(b))
    if upper < MATCH_FLOOR:
        return upper
    score = SequenceMatcher(a=a, b=b).ratio()
    if (a in b or b in a) and min(len(a), len(b)) / max(len(a), len(b)) >= 0.7:
        score = max(score, 0.88)
    return score


def candidate_signals(signals: list[Signal]) -> list[Signal]:
    picked = []
    for signal in signals:
        if signal.role_hint in {"furniture", "page_number", "caption", "footnote"} or not signal.text:
            continue
        latent = (len(signal.text) <= 90 and signal.heading_score >= 0.15) or signal.heading_score >= 0.5
        if signal.block_type == "title" or signal.role_hint == "heading" or latent:
            picked.append(signal)
    return picked


@dataclass
class Alignment:
    toc_index: int
    block_id: str
    score: float
    level: int


def align_printed_toc(entries: list[TocEntry], candidates: list[Signal]) -> dict[str, Any]:
    """Needleman–Wunsch 全局对齐目录与标题候选，利用"目录顺序 = 正文顺序"约束。"""
    if not entries or not candidates:
        return {
            "matches": [],
            "level_targets": {},
            "missing": list(entries),
            "extra_ids": [candidate.block_id for candidate in candidates],
        }
    rows, cols = len(entries), len(candidates)
    sim = [[heading_similarity(entries[i].title, candidates[j].text) for j in range(cols)] for i in range(rows)]
    dp = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    back = [[0] * (cols + 1) for _ in range(rows + 1)]
    for i in range(1, rows + 1):
        dp[i][0] = dp[i - 1][0] + GAP_PENALTY
        back[i][0] = 1
    for j in range(1, cols + 1):
        dp[0][j] = dp[0][j - 1] + GAP_PENALTY
        back[0][j] = 2
    for i in range(1, rows + 1):
        for j in range(1, cols + 1):
            score = sim[i - 1][j - 1]
            match_gain = dp[i - 1][j - 1] + (score * 1.6 - 0.25 if score >= MATCH_FLOOR else -0.6)
            best = max(match_gain, dp[i - 1][j] + GAP_PENALTY, dp[i][j - 1] + GAP_PENALTY)
            dp[i][j] = best
            back[i][j] = 0 if best == match_gain else (1 if best == dp[i - 1][j] + GAP_PENALTY else 2)
    matches: list[Alignment] = []
    matched_toc: set[int] = set()
    matched_blocks: set[str] = set()
    i, j = rows, cols
    while i > 0 or j > 0:
        move = back[i][j]
        if i > 0 and j > 0 and move == 0:
            if sim[i - 1][j - 1] >= MATCH_FLOOR:
                matches.append(Alignment(i - 1, candidates[j - 1].block_id, sim[i - 1][j - 1], entries[i - 1].level))
                matched_toc.add(i - 1)
                matched_blocks.add(candidates[j - 1].block_id)
            i -= 1
            j -= 1
        elif move == 1:
            i -= 1
        else:
            j -= 1
    matches.reverse()
    return {
        "matches": matches,
        "level_targets": {item.block_id: item.level for item in matches},
        "missing": [entry for index, entry in enumerate(entries) if index not in matched_toc],
        "extra_ids": [candidate.block_id for candidate in candidates if candidate.block_id not in matched_blocks],
    }
