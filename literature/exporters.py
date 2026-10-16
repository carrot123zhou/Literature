"""内置多格式导出（纯渲染层，无全局状态）。

数据源只有两个：``document/document.json``（规范文档图谱）与
``translations/<locale>/translation.json``（按 block_id 挂接的译文层）。
本模块只读取它们，绝不修改 raw / document / translations；产物一律写到
项目目录下的 ``exports/<export_id>/``。

格式：
- ``epub``：EPUB3 单文件。
- ``docx``：Word 单文件（需要 python-docx）。
- ``markdown``：单文件 ``full.md`` + 同级 ``assets/``。
- ``html``：分章节多页 ``index.html`` + ``chapter-0001.html`` … + ``style.css`` + ``assets/``。

模式：``source``（原文）/ ``translation``（译文，缺译回退原文）/
``bilingual``（原文段后紧跟译文段，缺译只出原文段，不重复）。
"""

from __future__ import annotations

import html
import json
import mimetypes
import re
import shutil
import zipfile
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal
from xml.etree import ElementTree as ET

from .models import Block, Document, TranslationEntry, TranslationLayer, utc_now
from .storage import ProjectStore, StorageError, atomic_json, safe_locale, safe_name


ExportMode = Literal["source", "translation", "bilingual"]

FORMATS: list[dict[str, Any]] = [
    {"id": "epub", "label": "EPUB", "single_file": True},
    {"id": "docx", "label": "Word DOCX", "single_file": True},
    {"id": "markdown", "label": "Markdown", "single_file": True},
    {"id": "html", "label": "分页 HTML", "single_file": False},
]
MODES: list[dict[str, str]] = [
    {"id": "source", "label": "原文"},
    {"id": "translation", "label": "译文"},
    {"id": "bilingual", "label": "对照版"},
]
FORMAT_IDS = {item["id"] for item in FORMATS}
MODE_IDS = {item["id"] for item in MODES}

MEDIA_TYPES = {
    ".epub": "application/epub+zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}

_EXPORT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")

BASE_CSS = """
:root{color:#20322b;background:#f6f5ef;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC","Songti SC",sans-serif}
body{max-width:50rem;margin:3rem auto;padding:0 1.5rem;line-height:1.72;color:#20322b}
a{color:#2f5142}
h1,h2,h3,h4,h5,h6{line-height:1.3}
/* 浏览器阅读页：译文用更弱的墨色区分，不再用左侧竖线 */
.translation{color:#52625a}
.source+.translation{margin-top:.35rem}
p{margin:.7rem 0}
img{max-width:100%;height:auto}
table{border-collapse:collapse;width:100%;margin:1rem 0}
td,th{border:1px solid #bac5bd;padding:.45rem;text-align:left;vertical-align:top}
figcaption{color:#63716b;font-size:.92em}
.block{margin:.7rem 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eeeee6;padding:.6rem;border-radius:.3rem}
.footnote{color:#63716b;font-size:.94em}
""".strip() + "\n"

_INDEX_CSS_EXTRA = """
.toc{background:#fff;border:1px solid #d7ddd7;border-radius:.6rem;padding:1rem 1.4rem}
.toc ol{margin:.2rem 0 .2rem 1.1rem;padding:0}
.toc li{margin:.25rem 0}
.meta{color:#63716b}
""".strip()


# EPUB 专用排版（book.css）：不设背景色/前景色（不干扰阅读器夜间模式），
# 全部用相对单位；serif 正文 + 两端对齐 + 首行缩进，对齐 Apple Books 商店书的排版惯例。
EPUB_CSS = """
body{
  margin:0;
  line-height:1.65;
  font-family:Georgia,"Palatino","Times New Roman","Songti SC","Noto Serif CJK SC","Source Han Serif SC",serif;
  text-align:justify;
  widows:2;
  orphans:2;
}

h1,h2,h3,h4,h5,h6{
  font-family:"Avenir Next","Helvetica Neue",Helvetica,"PingFang SC","Noto Sans CJK SC","Source Han Sans SC",sans-serif;
  font-weight:700;
  line-height:1.3;
  text-align:left;
  hyphens:none;
  -webkit-hyphens:none;
  page-break-after:avoid;
  break-after:avoid;
}

/* 章节内小标题 */
h1{font-size:1.7em;margin:2em 0 .8em}
h2{font-size:1.4em;margin:1.8em 0 .7em}
h3{font-size:1.2em;margin:1.5em 0 .6em}
h4,h5,h6{font-size:1.05em;margin:1.2em 0 .5em}

/* 标题的译文：弱化为常规字重副题（原文标题之下） */
.block-title .translation{
  font-size:.55em;
  font-weight:400;
  margin:.55em 0 0;
  line-height:1.4;
}

/* 章首标题（每章第一个标题块） */
body > section.block-title:first-child{margin:2.2em 0 1.4em}
body > section.block-title:first-child h1,
body > section.block-title:first-child h2{font-size:1.7em;margin:0}
body > section.block-title:first-child h3,
body > section.block-title:first-child h4,
body > section.block-title:first-child h5,
body > section.block-title:first-child h6{font-size:1.45em;margin:0}

p{
  margin:0 0 .5em;
  text-indent:1.5em;
  hyphens:auto;
  -webkit-hyphens:auto;
}
/* 译文以中文为主，按中文排版惯例缩进两字 */
p.translation{text-indent:2em}

/* 标题后的第一段不缩进（出版惯例） */
section.block-title + section.block-paragraph > p:first-child{text-indent:0}

/* 封面页 */
body.cover-page{margin:0;text-align:center}
body.cover-page .cover{margin:0;padding:0}
body.cover-page img{max-width:100%;height:auto}

.footnote{font-size:.88em;margin:.35em 0 .5em;text-indent:0}

ul,ol{margin:.6em 0;padding-left:1.5em}
li{margin:.28em 0;text-align:justify;hyphens:auto;-webkit-hyphens:auto}

pre,code{font-family:"SF Mono",Menlo,Consolas,"Courier New",monospace}
pre{
  font-size:.88em;
  white-space:pre-wrap;
  overflow-wrap:anywhere;
  margin:.8em 0;
  text-indent:0;
  text-align:left;
  page-break-inside:avoid;
  break-inside:avoid;
}

img{max-width:100%;height:auto}
.block-image{text-align:center;margin:1em 0}
figcaption{font-size:.88em;margin:.4em 0 1em;text-indent:0;text-align:center;hyphens:none;-webkit-hyphens:none}

table{
  border-collapse:collapse;
  margin:.9em auto;
  font-size:.92em;
  page-break-inside:avoid;
  break-inside:avoid;
}
td,th{border:1px solid;padding:.4em .55em;text-align:left;vertical-align:top}

a{color:inherit}

/* 目录页 */
nav > h1{margin:2em 0 1em}
nav ol{list-style:none;padding-left:0;margin:0}
nav ol ol{padding-left:1.4em}
nav li{margin:.5em 0}
""".strip() + "\n"


# ---------------------------------------------------------------------------
# 译文/内容取值
# ---------------------------------------------------------------------------

def entry_for(layer: TranslationLayer | None, block: Block) -> TranslationEntry | None:
    return layer.entries.get(block.block_id) if layer else None


def has_translation(block: Block, entry: TranslationEntry | None) -> bool:
    """该 block 在当前译文层里是否真的有一段可用译文。"""
    if entry is None:
        return False
    if block.type == "list":
        return bool(entry.items)
    if block.type == "table":
        return bool(entry.cells)
    if block.type == "image":
        return bool(entry.caption)
    return bool((entry.text or "").strip())


def _text_roles(block: Block, entry: TranslationEntry | None, mode: ExportMode) -> list[tuple[str, str]]:
    source = block.source_text or ""
    translated = entry.text if entry and entry.text else ""
    if mode == "source":
        return [("source", source)]
    if mode == "translation":
        return [("translation", translated or source)]
    roles = [("source", source)]
    if translated:
        roles.append(("translation", translated))
    return roles


def _list_roles(block: Block, entry: TranslationEntry | None, mode: ExportMode) -> list[tuple[str, list[str]]]:
    source = list(block.items or [])
    translated = list(entry.items) if entry and entry.items else []
    if mode == "source":
        return [("source", source)]
    if mode == "translation":
        return [("translation", translated or source)]
    roles = [("source", source)]
    if translated:
        roles.append(("translation", translated))
    return roles


def _caption_roles(block: Block, entry: TranslationEntry | None, mode: ExportMode) -> list[tuple[str, list[str]]]:
    source = list(block.caption or [])
    translated = list(entry.caption) if entry and entry.caption else []
    if mode == "source":
        return [("source", source)]
    if mode == "translation":
        return [("translation", translated or source)]
    roles = [("source", source)]
    if translated:
        roles.append(("translation", translated))
    return roles


def _table_roles(block: Block, entry: TranslationEntry | None, mode: ExportMode) -> list[str]:
    if mode == "source":
        return ["source"]
    if mode == "translation":
        return ["translation"]
    roles = ["source"]
    if entry and entry.cells:
        roles.append("translation")
    return roles


def _table_matrix(block: Block, entry: TranslationEntry | None, role: str) -> list[list[str]]:
    if not block.cells:
        return []
    rows = max(cell.row for cell in block.cells) + 1
    columns = max(cell.column for cell in block.cells) + 1
    values = entry.cells if entry and entry.cells and role == "translation" else {}
    matrix = [["" for _ in range(columns)] for _ in range(rows)]
    for cell in block.cells:
        row = max(0, min(rows - 1, cell.row))
        column = max(0, min(columns - 1, cell.column))
        matrix[row][column] = values.get(cell.cell_id, cell.text)
    return matrix


def _table_html(block: Block, entry: TranslationEntry | None, role: str) -> str:
    if not block.cells:
        return str(block.format.get("table_html", "") or "")
    values = entry.cells if entry and entry.cells and role == "translation" else {}
    rows = max(cell.row for cell in block.cells) + 1
    by_row: dict[int, list[Any]] = {}
    for cell in block.cells:
        by_row.setdefault(cell.row, []).append(cell)
    output = ["<table>"]
    for row in range(rows):
        output.append("<tr>")
        for cell in sorted(by_row.get(row, []), key=lambda item: item.column):
            tag = "th" if cell.header else "td"
            attributes = ""
            if cell.rowspan > 1:
                attributes += f' rowspan="{int(cell.rowspan)}"'
            if cell.colspan > 1:
                attributes += f' colspan="{int(cell.colspan)}"'
            output.append(f"<{tag}{attributes}>{html.escape(values.get(cell.cell_id, cell.text))}</{tag}>")
        output.append("</tr>")
    output.append("</table>")
    return "".join(output)


# ---------------------------------------------------------------------------
# 章节选择与切分
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Section:
    title: str
    level: int
    blocks: list[Block]
    nav: bool = True


def flatten_toc(nodes: Iterable[Any]) -> Iterator[Any]:
    for node in nodes:
        yield node
        yield from flatten_toc(getattr(node, "children", []) or [])


def toc_levels(document: Document) -> dict[str, int]:
    """TOC 节点 block_id → 规范化 level（1..6）。"""
    levels: dict[str, int] = {}
    for node in flatten_toc(document.toc):
        levels[str(node.block_id)] = max(1, min(6, int(node.level or 1)))
    return levels


def _title_positions(blocks: list[Block]) -> dict[int, list[int]]:
    """by_level[L] = 所有「type==title 且 level<=L」block 的下标（升序）。"""
    by_level: dict[int, list[int]] = {level: [] for level in range(1, 7)}
    for index, block in enumerate(blocks):
        if block.type != "title":
            continue
        level = max(1, min(6, int(block.level or 1)))
        for container in range(level, 7):
            by_level[container].append(index)
    return by_level


def _chapter_end(by_level: dict[int, list[int]], start: int, level: int, total: int) -> int:
    positions = by_level[max(1, min(6, int(level or 1)))]
    index = bisect_right(positions, start)
    return positions[index] if index < len(positions) else total


def _block_positions(blocks: list[Block]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, block in enumerate(blocks):
        positions.setdefault(block.block_id, index)
    return positions


def select_blocks(
    document: Document,
    chapters: Iterable[str] | None = None,
    block_ids: Iterable[str] | None = None,
) -> list[Block]:
    """按冻结契约展开所选内容。

    - ``block_ids`` 非空时优先：精确取这些 block（保持阅读顺序）。
    - 否则 ``chapters`` 为 TOC 节点 block_id 列表：从该 title block 开始向后收集，
      遇到下一个 ``type=="title" && level<=该节点 level`` 的 block 时停止（不含它），
      起始 title 自身包含在内；空列表 = 全书。
    """
    blocks = list(document.blocks)
    positions = _block_positions(blocks)

    chosen_ids = [str(value) for value in (block_ids or [])]
    if chosen_ids:
        return [blocks[index] for index in sorted({positions[value] for value in chosen_ids if value in positions})]

    chapter_values = [str(value) for value in (chapters or [])]
    if not chapter_values:
        return blocks

    levels = toc_levels(document)
    by_level = _title_positions(blocks)
    ranges: list[tuple[int, int]] = []
    for value in chapter_values:
        start = positions.get(value)
        if start is None:
            continue
        block = blocks[start]
        level = levels.get(value) or max(1, min(6, int(block.level or 1)))
        ranges.append((start, _chapter_end(by_level, start, level, len(blocks))))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [block for start, end in merged for block in blocks[start:end]]


def split_sections(document: Document, blocks: list[Block]) -> list[Section]:
    """把已选 block 切成若干个「章节」，供 EPUB / 分页 HTML 的分页与目录使用。

    优先按 TOC 节点切分（保留层级）；无可用 TOC 节点时回退到 ``level<=2`` 的 title。
    """
    if not blocks:
        return []
    positions = _block_positions(blocks)
    starts: list[tuple[int, str, int]] = []
    for node in flatten_toc(document.toc):
        block_id = str(node.block_id)
        if block_id not in positions:
            continue
        index = positions[block_id]
        title = str(node.title or "") or blocks[index].source_text or ""
        starts.append((index, title, max(1, min(6, int(node.level or 1)))))
    if not starts:
        for index, block in enumerate(blocks):
            if block.type == "title" and 1 <= int(block.level or 0) <= 2:
                starts.append((index, block.source_text or "", max(1, min(6, int(block.level or 1)))))
    starts.sort()
    unique: list[tuple[int, str, int]] = []
    seen: set[int] = set()
    for item in starts:
        if item[0] in seen:
            continue
        seen.add(item[0])
        unique.append(item)

    sections: list[Section] = []
    if not unique or unique[0][0] > 0:
        end = unique[0][0] if unique else len(blocks)
        sections.append(Section(document.title, 1, blocks[:end], nav=False))
    for index, (start, title, level) in enumerate(unique):
        end = unique[index + 1][0] if index + 1 < len(unique) else len(blocks)
        sections.append(Section(title, level, blocks[start:end], nav=True))
    return sections


def _section_nav_title(section: Section, layer: TranslationLayer | None, mode: ExportMode) -> str:
    """导航条目标题：译文模式用纯译文标题；对照模式拼「原文 · 译文」；缺失回退原文。"""
    if not section.nav or not section.blocks:
        return section.title
    first = section.blocks[0]
    if first.type != "title":
        return section.title
    entry = entry_for(layer, first)
    translated = (entry.text or "").strip() if entry else ""
    if not translated or translated == (first.source_text or "").strip():
        return section.title
    if mode == "translation":
        return translated
    if mode == "bilingual":
        return f"{section.title} · {translated}"
    return section.title


def _build_nav_tree(items: list[tuple[str, str, int]]) -> list[dict[str, Any]]:
    root: dict[str, Any] = {"level": 0, "children": []}
    stack = [root]
    for title, href, level in items:
        node: dict[str, Any] = {"title": title, "href": href, "level": level, "children": []}
        while len(stack) > 1 and stack[-1]["level"] >= level:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)
    return root["children"]


def _render_nav(nodes: list[dict[str, Any]]) -> str:
    if not nodes:
        return ""
    parts = ["<ol>"]
    for node in nodes:
        parts.append(f'<li><a href="{html.escape(node["href"])}">{html.escape(node["title"])}</a>')
        parts.append(_render_nav(node["children"]))
        parts.append("</li>")
    parts.append("</ol>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 资源（图片）映射与复制
# ---------------------------------------------------------------------------

def asset_plan(document: Document, project: Path) -> dict[str, str]:
    """resource_id → 输出文件名（basename 冲突用 ``<resource_id>-`` 前缀去重）。"""
    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    for resource_id, value in document.resources.items():
        relative = str((value or {}).get("path") or "")
        if not relative:
            continue
        source = project / relative
        if not source.is_file():
            continue
        base = Path(relative).name
        candidate = base
        if candidate in used:
            candidate = f"{resource_id}-{base}"
        suffix = 2
        while candidate in used:
            candidate = f"{resource_id}-{suffix}-{base}"
            suffix += 1
        used[candidate] = resource_id
        mapping[resource_id] = candidate
    return mapping


def _copy_assets(project: Path, target: Path, document: Document, mapping: dict[str, str]) -> list[Path]:
    files: list[Path] = []
    if not mapping:
        return files
    target.mkdir(parents=True, exist_ok=True)
    for resource_id, filename in mapping.items():
        relative = str((document.resources.get(resource_id) or {}).get("path") or "")
        if not relative:
            continue
        source = project / relative
        if not source.is_file():
            continue
        destination = target / filename
        if not destination.exists():
            shutil.copy2(source, destination)
        files.append(destination)
    return files


# ---------------------------------------------------------------------------
# Markdown 渲染
# ---------------------------------------------------------------------------

def _markdown_block(block: Block, entry: TranslationEntry | None, mode: ExportMode, assets: dict[str, str]) -> list[str]:
    lines: list[str] = []
    kind = block.type
    if kind == "title":
        level = max(1, min(6, int(block.level or 1)))
        for role, text in _text_roles(block, entry, mode):
            if not text:
                continue
            lines.extend([f"{'#' * level} {text}", ""])
    elif kind in {"paragraph", "code", "footnote"}:
        for role, text in _text_roles(block, entry, mode):
            if not text:
                continue
            prefix = "> [页下注] " if kind == "footnote" else ""
            if mode == "bilingual" and role == "translation":
                prefix += "> "
            lines.extend([prefix + text, ""])
    elif kind == "list":
        for role, values in _list_roles(block, entry, mode):
            for value in values:
                prefix = "> " if (mode == "bilingual" and role == "translation") else ""
                lines.append(f"{prefix}- {value}")
            lines.append("")
    elif kind == "equation":
        lines.extend(["$$", block.source_text, "$$", ""])
    elif kind == "table":
        for role in _table_roles(block, entry, mode):
            matrix = _table_matrix(block, entry, role)
            if not matrix:
                fallback = str(block.format.get("table_html", "") or "")
                if fallback:
                    lines.extend([fallback, ""])
                continue
            lines.append("| " + " | ".join(matrix[0]) + " |")
            lines.append("| " + " | ".join("---" for _ in matrix[0]) + " |")
            lines.extend("| " + " | ".join(row) + " |" for row in matrix[1:])
            lines.append("")
    elif kind == "image":
        for resource_id in block.resource_refs:
            filename = assets.get(resource_id)
            if filename:
                lines.append(f"![](assets/{filename})")
        for role, captions in _caption_roles(block, entry, mode):
            for caption in captions:
                prefix = "> " if (mode == "bilingual" and role == "translation") else ""
                lines.append(prefix + caption)
        lines.append("")
    else:
        if block.source_text:
            lines.extend([block.source_text, ""])
    return lines


def render_markdown(
    document: Document, blocks: list[Block], layer: TranslationLayer | None, mode: ExportMode,
    assets: dict[str, str] | None = None,
) -> str:
    mapping = assets if assets is not None else asset_plan(document, Path("."))
    lines: list[str] = []
    for block in blocks:
        lines.extend(_markdown_block(block, entry_for(layer, block), mode, mapping))
    text = "\n".join(lines).rstrip() + "\n"
    for resource_id, filename in mapping.items():
        text = text.replace(f"({resource_id})", f"(assets/{filename})")
    return text


# ---------------------------------------------------------------------------
# HTML / XHTML 渲染
# ---------------------------------------------------------------------------

def _lang_attr(role: str, mode: ExportMode, source_language: str) -> str:
    """对照模式下给原文标 lang，让西文在两端对齐时能正确连字断行。"""
    if mode == "bilingual" and role == "source" and source_language:
        value = html.escape(source_language)
        return f' xml:lang="{value}" lang="{value}"'
    return ""


def _block_html(block: Block, entry: TranslationEntry | None, mode: ExportMode, assets: dict[str, str],
                source_language: str = "") -> str:
    kind = block.type
    output = [
        f'<section class="block block-{html.escape(kind)}"'
        f' data-block-id="{html.escape(block.block_id)}" data-page="{block.page_index + 1}">'
    ]
    if kind == "title":
        tag = f"h{max(1, min(6, int(block.level or 1)))}"
        for role, text in _text_roles(block, entry, mode):
            output.append(f'<{tag} class="{role}"{_lang_attr(role, mode, source_language)}>{html.escape(text)}</{tag}>')
    elif kind in {"paragraph", "code"}:
        tag = "pre" if kind == "code" else "p"
        for role, text in _text_roles(block, entry, mode):
            output.append(f'<{tag} class="{role}"{_lang_attr(role, mode, source_language)}>{html.escape(text)}</{tag}>')
    elif kind == "footnote":
        for role, text in _text_roles(block, entry, mode):
            output.append(f'<p class="footnote {role}"{_lang_attr(role, mode, source_language)}>{html.escape(text)}</p>')
    elif kind == "list":
        for role, values in _list_roles(block, entry, mode):
            attr = _lang_attr(role, mode, source_language)
            output.append(f'<ul class="{role}"{attr}>' + "".join(f"<li>{html.escape(value)}</li>" for value in values) + "</ul>")
    elif kind == "equation":
        output.append(f'<pre class="equation source">{html.escape(block.source_text)}</pre>')
    elif kind == "table":
        if block.cells:
            for role in _table_roles(block, entry, mode):
                output.append(f'<div class="table-{role}">{_table_html(block, entry, role)}</div>')
        else:
            output.append(str(block.format.get("table_html", "") or ""))
    elif kind == "image":
        for resource_id in block.resource_refs:
            filename = assets.get(resource_id)
            if filename:
                output.append(f'<img src="assets/{html.escape(filename)}" alt=""/>')
        for role, captions in _caption_roles(block, entry, mode):
            for caption in captions:
                output.append(f'<figcaption class="{role}"{_lang_attr(role, mode, source_language)}>{html.escape(caption)}</figcaption>')
    else:
        if block.source_text:
            output.append(f'<p class="source">{html.escape(block.source_text)}</p>')
    output.append("</section>")
    return "".join(output)


def _page_html(title: str, language: str, body: str, *, stylesheet: str) -> str:
    return (
        "<!doctype html>\n"
        f'<html lang="{html.escape(language)}"><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width,initial-scale=1"/>'
        f"<title>{html.escape(title)}</title>"
        f'<link rel="stylesheet" href="{html.escape(stylesheet)}"/></head>'
        f"<body><main>{body}</main></body></html>\n"
    )


def _xhtml(title: str, language: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        f'lang="{html.escape(language)}" xml:lang="{html.escape(language)}">'
        f"<head><title>{html.escape(title)}</title>"
        '<link rel="stylesheet" type="text/css" href="../styles/book.css"/></head>'
        f"<body>{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# 各格式写入器（返回 (Path, single_file) 列表）
# ---------------------------------------------------------------------------

def _write_markdown(project: Path, root: Path, document: Document, blocks: list[Block], layer: TranslationLayer | None,
                    mode: ExportMode, assets: dict[str, str]) -> list[tuple[Path, bool]]:
    root.mkdir(parents=True, exist_ok=True)
    target = root / "full.md"
    target.write_text(render_markdown(document, blocks, layer, mode, assets), encoding="utf-8")
    files: list[tuple[Path, bool]] = [(target, True)]
    for path in _copy_assets(project, root / "assets", document, assets):
        files.append((path, True))
    return files


def _write_html(project: Path, root: Path, document: Document, blocks: list[Block], layer: TranslationLayer | None,
                mode: ExportMode, assets: dict[str, str]) -> list[tuple[Path, bool]]:
    root.mkdir(parents=True, exist_ok=True)
    sections = split_sections(document, blocks) or [Section(document.title, 1, blocks, nav=False)]
    language = _display_language(document, layer, mode)
    files: list[tuple[Path, bool]] = []
    nav_items: list[tuple[str, str, int]] = []
    for index, section in enumerate(sections, 1):
        filename = f"chapter-{index:04d}.html"
        body = "".join(_block_html(block, entry_for(layer, block), mode, assets) for block in section.blocks)
        (root / filename).write_text(_page_html(section.title, language, body, stylesheet="style.css"), encoding="utf-8")
        files.append((root / filename, False))
        if section.nav:
            nav_items.append((section.title, filename, section.level))
    if not nav_items:
        nav_items = [(section.title, f"chapter-{i:04d}.html", section.level) for i, section in enumerate(sections, 1)]
    nav_html = _render_nav(_build_nav_tree([(t, h, lv) for t, h, lv in nav_items]))
    index_body = (
        f'<h1 class="book-title">{html.escape(document.title)}</h1>'
        f'<p class="meta">{html.escape(language)} · {len(blocks)} blocks</p>'
        f'<nav class="toc">{nav_html or "<ol></ol>"}</nav>'
    )
    (root / "index.html").write_text(_page_html(document.title, language, index_body, stylesheet="style.css"), encoding="utf-8")
    files.append((root / "index.html", False))
    (root / "style.css").write_text(BASE_CSS + _INDEX_CSS_EXTRA + "\n", encoding="utf-8")
    files.append((root / "style.css", False))
    for path in _copy_assets(project, root / "assets", document, assets):
        files.append((path, False))
    return files


def _cover_jpeg(project: Path) -> tuple[str, bytes] | None:
    """用原书 PDF 首页渲染 EPUB 封面（JPEG）；非 PDF 源或渲染失败返回 None。"""
    source_dir = project / "source"
    if not source_dir.is_dir():
        return None
    pdf_path = next((child for child in sorted(source_dir.iterdir())
                     if child.is_file() and child.suffix.casefold() == ".pdf"), None)
    if pdf_path is None:
        return None
    try:
        import fitz

        with fitz.open(pdf_path) as pdf:
            if pdf.page_count < 1:
                return None
            page = pdf.load_page(0)
            zoom = max(1.0, 1600.0 / max(1.0, float(page.rect.width)))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            if pix.width > 2400:
                scale = 2400.0 / pix.width
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom * scale, zoom * scale), alpha=False)
            return "cover.jpg", pix.tobytes("jpg", jpg_quality=88)
    except Exception:  # noqa: BLE001 - 封面是增强项，任何失败都不阻断导出
        return None


def validate_epub(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not infos or infos[0].filename != "mimetype" or infos[0].compress_type != zipfile.ZIP_STORED:
            raise StorageError("EPUB mimetype 必须是首个且未压缩条目。")
        names = set(archive.namelist())
        required = {"mimetype", "META-INF/container.xml", "EPUB/package.opf", "EPUB/nav.xhtml"}
        if missing := required - names:
            raise StorageError(f"EPUB 缺少文件：{sorted(missing)}")
        for name in names:
            if name.endswith((".xml", ".opf", ".xhtml")):
                ET.fromstring(archive.read(name))


def _write_epub(project: Path, root: Path, document: Document, blocks: list[Block], layer: TranslationLayer | None,
                mode: ExportMode, assets: dict[str, str], name: str) -> list[tuple[Path, bool]]:
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"{name}.epub"
    sections = split_sections(document, blocks) or [Section(document.title, 1, blocks, nav=False)]
    language = _display_language(document, layer, mode)
    source_language = str(document.source_language or "")
    if language == source_language:
        source_language = ""  # 已由根元素声明，不必逐段重复
    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="css" href="styles/book.css" media-type="text/css"/>',
    ]
    cover = _cover_jpeg(project)
    if cover is not None:
        manifest_items.append('<item id="cover-image" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
        manifest_items.append('<item id="cover-page" href="cover.xhtml" media-type="application/xhtml+xml"/>')
    spine: list[str] = []
    if cover is not None:
        spine.append('<itemref idref="cover-page" linear="yes"/>')
    nav_items: list[tuple[str, str, int]] = []
    bodies: list[tuple[str, str]] = []
    for index, section in enumerate(sections, 1):
        filename = f"text/section-{index:04d}.xhtml"
        body = "".join(
            _block_html(block, entry_for(layer, block), mode, assets, source_language)
            for block in section.blocks
        )
        bodies.append((filename, _xhtml(section.title, language, body)))
        manifest_items.append(f'<item id="section-{index}" href="{filename}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="section-{index}"/>')
        if section.nav:
            nav_items.append((_section_nav_title(section, layer, mode), filename, section.level))
    if not nav_items:
        nav_items = [(_section_nav_title(section, layer, mode), f"text/section-{i:04d}.xhtml", section.level)
                     for i, section in enumerate(sections, 1)]
    nav_tree = _build_nav_tree(nav_items)
    nav_body = (
        '<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc">'
        f"<h1>目录</h1>{_render_nav(nav_tree)}</nav>"
    )
    book_id = f"urn:uuid:{_uuid_like(document)}"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        if cover is not None:
            cover_filename, cover_bytes = cover
            archive.writestr("EPUB/cover.jpg", cover_bytes, compress_type=zipfile.ZIP_STORED)
            archive.writestr(
                "EPUB/cover.xhtml",
                _xhtml("Cover", language, f'<div class="cover"><img src="{cover_filename}" alt="{html.escape(document.title)}"/></div>'),
            )
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        archive.writestr("EPUB/styles/book.css", EPUB_CSS)
        for filename, body in bodies:
            archive.writestr(f"EPUB/{filename}", body)
        for resource_id, filename in assets.items():
            relative = str((document.resources.get(resource_id) or {}).get("path") or "")
            source = project / relative if relative else None
            if source is None or not source.is_file():
                continue
            media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            archive.write(source, f"EPUB/text/assets/{filename}")
            manifest_items.append(
                f'<item id="asset-{len(manifest_items)}" href="text/assets/{html.escape(filename)}" media-type="{media_type}"/>'
            )
        archive.writestr("EPUB/nav.xhtml", _xhtml("目录", language, nav_body))
        package = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            f'<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="book-id" version="3.0" xml:lang="{html.escape(language)}">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:identifier id="book-id">{book_id}</dc:identifier>'
            f"<dc:title>{html.escape(document.title)}</dc:title>"
            f"<dc:language>{html.escape(language)}</dc:language>"
            f'<meta property="dcterms:modified">{utc_now().split(".")[0]}Z</meta>'
            + (f'<meta name="cover" content="cover-image"/>' if cover is not None else "")
            + f"</metadata><manifest>{''.join(manifest_items)}</manifest>"
            f"<spine>{''.join(spine)}</spine></package>"
        )
        archive.writestr("EPUB/package.opf", package)
    validate_epub(output)
    return [(output, True)]


def _uuid_like(document: Document) -> str:
    import uuid as _uuid

    seed = f"{document.project_id}:{document.title}:{document.updated_at}"
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, seed))


def _docx_quote(paragraph: Any) -> None:
    from docx.shared import Inches, RGBColor

    paragraph.paragraph_format.left_indent = Inches(0.3)
    for run in paragraph.runs:
        run.italic = True
        run.font.color.rgb = RGBColor(0x2F, 0x51, 0x42)


def _docx_default_font(word: Any) -> None:
    try:
        from docx.oxml.ns import qn

        style = word.styles["Normal"]
        style.font.name = "Times New Roman"
        rpr = style.element.get_or_add_rPr()
        rfonts = rpr.get_or_add_rFonts()
        rfonts.set(qn("w:eastAsia"), "Songti SC")
    except Exception:  # noqa: BLE001 - 字体仅为观感，失败不影响内容
        pass


def _docx_table(word: Any, block: Block, entry: TranslationEntry | None, role: str) -> None:
    matrix = _table_matrix(block, entry, role)
    if not matrix:
        fallback = str(block.format.get("table_html", "") or "")
        if fallback:
            word.add_paragraph(fallback)
        return
    rows = len(matrix)
    columns = len(matrix[0])
    table = word.add_table(rows=rows, cols=columns)
    try:
        table.style = "Table Grid"
    except KeyError:
        pass
    values = entry.cells if entry and entry.cells and role == "translation" else {}
    for cell in block.cells:
        row = max(0, min(rows - 1, cell.row))
        column = max(0, min(columns - 1, cell.column))
        table.cell(row, column).text = values.get(cell.cell_id, cell.text)
    for cell in block.cells:
        if cell.rowspan <= 1 and cell.colspan <= 1:
            continue
        row_end = max(0, min(rows - 1, cell.row + max(1, cell.rowspan) - 1))
        column_end = max(0, min(columns - 1, cell.column + max(1, cell.colspan) - 1))
        try:
            anchor = table.cell(max(0, min(rows - 1, cell.row)), max(0, min(columns - 1, cell.column)))
            other = table.cell(row_end, column_end)
            if anchor is not other:
                anchor.merge(other)
        except Exception:  # noqa: BLE001 - 合并失败不阻断导出
            continue


def _write_docx(project: Path, root: Path, document: Document, blocks: list[Block], layer: TranslationLayer | None,
                mode: ExportMode, assets: dict[str, str], name: str) -> list[tuple[Path, bool]]:
    try:
        from docx import Document as WordDocument
        from docx.shared import Inches
    except ImportError as error:  # pragma: no cover - 环境缺少依赖时的明确报错
        raise StorageError("导出 DOCX 需要 python-docx。") from error
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"{name}.docx"
    word = WordDocument()
    word.core_properties.title = document.title
    _docx_default_font(word)
    for block in blocks:
        entry = entry_for(layer, block)
        kind = block.type
        if kind == "title":
            level = max(1, min(9, int(block.level or 1)))
            for role, text in _text_roles(block, entry, mode):
                if not text:
                    continue
                heading = word.add_heading(text, level=level)
                if mode == "bilingual" and role == "translation":
                    _docx_quote(heading)
        elif kind in {"paragraph", "footnote", "code"}:
            for role, text in _text_roles(block, entry, mode):
                if not text:
                    continue
                paragraph = word.add_paragraph(text)
                if role == "translation" and mode != "source":
                    _docx_quote(paragraph)
        elif kind == "list":
            for role, values in _list_roles(block, entry, mode):
                for value in values:
                    paragraph = word.add_paragraph(value, style="List Bullet")
                    if mode == "bilingual" and role == "translation":
                        _docx_quote(paragraph)
        elif kind == "equation":
            word.add_paragraph(block.source_text)
        elif kind == "table":
            for role in _table_roles(block, entry, mode):
                _docx_table(word, block, entry, role)
        elif kind == "image":
            for resource_id in block.resource_refs:
                relative = str((document.resources.get(resource_id) or {}).get("path") or "")
                source = project / relative if relative else None
                if source is not None and source.is_file():
                    try:
                        word.add_picture(str(source), width=Inches(5.8))
                    except Exception:  # noqa: BLE001 - 个别图片格式失败时留占位说明
                        word.add_paragraph(f"[图片：{source.name}]")
                else:
                    filename = assets.get(resource_id) or resource_id
                    word.add_paragraph(f"[图片：{filename}]")
            for _role, captions in _caption_roles(block, entry, mode):
                for caption in captions:
                    word.add_paragraph(caption, style="Caption")
        else:
            if block.source_text:
                word.add_paragraph(block.source_text)
    word.save(output)
    return [(output, True)]


def _display_language(document: Document, layer: TranslationLayer | None, mode: ExportMode) -> str:
    if mode == "source":
        return document.source_language or "und"
    if layer is not None and layer.locale:
        return layer.locale
    return document.target_language or document.source_language or "und"


# ---------------------------------------------------------------------------
# 对外 API
# ---------------------------------------------------------------------------

def available_locales(project: Path) -> list[str]:
    root = project / "translations"
    values: list[str] = []
    if not root.is_dir():
        return values
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / "translation.json").is_file():
            continue
        try:
            if safe_locale(child.name) == child.name:
                values.append(child.name)
        except StorageError:
            continue
    return values


def export_options(store: ProjectStore, project_id: str, locale: str = "") -> dict[str, Any]:
    project = store.project_path(project_id)
    document = store.load_document(project_id)
    locales = available_locales(project)
    requested = str(locale or "").strip()
    if requested in locales:
        default_locale = requested
    elif document.target_language in locales:
        default_locale = document.target_language
    elif locales:
        default_locale = locales[0]
    else:
        default_locale = requested or document.target_language or ""
    layer = store.load_translation(project_id, default_locale) if default_locale else None

    blocks = document.blocks
    positions = _block_positions(blocks)
    by_level = _title_positions(blocks)
    levels = toc_levels(document)
    prefix = [0]
    for block in blocks:
        prefix.append(prefix[-1] + (1 if has_translation(block, entry_for(layer, block)) else 0))

    def node_json(node: Any) -> dict[str, Any]:
        block_id = str(node.block_id)
        start = positions.get(block_id)
        if start is None:
            count = translated = 0
        else:
            level = levels.get(block_id) or 1
            end = _chapter_end(by_level, start, level, len(blocks))
            count = end - start
            translated = prefix[end] - prefix[start]
        return {
            "node_id": str(node.node_id),
            "title": str(node.title or ""),
            "level": max(1, min(6, int(node.level or 1))),
            "block_id": block_id,
            "page_index": int(getattr(node, "page_index", 0) or 0),
            "block_count": count,
            "translated_count": translated,
            "missing_count": max(0, count - translated),
            "children": [node_json(child) for child in (getattr(node, "children", []) or [])],
        }

    return {
        "project_id": project_id,
        "title": document.title,
        "source_language": document.source_language,
        "target_language": document.target_language,
        "locales": locales,
        "default_locale": default_locale,
        "formats": FORMATS,
        "modes": MODES,
        "block_count": len(blocks),
        "image_count": len(document.resources),
        "toc": [node_json(node) for node in document.toc],
        "recent": list_exports(store, project_id),
    }


def _unique_export_id(project: Path, mode: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}-{mode}"
    candidate = base
    index = 1
    while (project / "exports" / candidate).exists():
        index += 1
        candidate = f"{base}-{index}"
    return candidate


def _format_files(
    fmt: str,
    project: Path,
    root: Path,
    document: Document,
    blocks: list[Block],
    layer: TranslationLayer | None,
    mode: ExportMode,
    assets: dict[str, str],
    name: str,
) -> list[tuple[Path, bool]]:
    if fmt == "epub":
        return _write_epub(project, root, document, blocks, layer, mode, assets, name)
    if fmt == "docx":
        return _write_docx(project, root, document, blocks, layer, mode, assets, name)
    if fmt == "markdown":
        return _write_markdown(project, root, document, blocks, layer, mode, assets)
    if fmt == "html":
        return _write_html(project, root, document, blocks, layer, mode, assets)
    raise StorageError(f"不支持的导出格式：{fmt}")


def create_export(
    store: ProjectStore,
    project_id: str,
    *,
    formats: Iterable[str] | None = None,
    mode: str = "translation",
    locale: str = "zh-CN",
    chapters: Iterable[str] | None = None,
    block_ids: Iterable[str] | None = None,
    filename: str = "",
    missing_policy: str = "fallback_source",
) -> dict[str, Any]:
    project = store.project_path(project_id)
    document = store.load_document(project_id)

    requested = list(dict.fromkeys(str(value) for value in (formats or [])))
    if not requested:
        requested = ["epub"]
    unknown_formats = [value for value in requested if value not in FORMAT_IDS]
    if unknown_formats:
        raise StorageError(f"不支持的导出格式：{'、'.join(unknown_formats)}")
    if mode not in MODE_IDS:
        raise StorageError(f"不支持的导出模式：{mode}")
    if missing_policy != "fallback_source":
        raise StorageError(f"不支持的缺失译文策略：{missing_policy}")

    layer = None if mode == "source" else store.load_translation(project_id, locale)
    chapter_values = [str(value) for value in (chapters or [])]
    block_values = [str(value) for value in (block_ids or [])]
    known = {block.block_id for block in document.blocks}
    warnings: list[str] = []
    if block_values:
        ignored = [value for value in block_values if value not in known]
        if ignored:
            warnings.append(f"忽略 {len(ignored)} 个不存在的段落。")
    elif chapter_values:
        ignored = [value for value in chapter_values if value not in known]
        if ignored:
            warnings.append(f"忽略 {len(ignored)} 个不存在的章节。")

    blocks = select_blocks(document, chapter_values, block_values)
    if not blocks:
        warnings.append("所选章节无任何可导出内容。")
    if mode != "source" and blocks:
        missing = sum(1 for block in blocks if not has_translation(block, entry_for(layer, block)))
        if missing:
            warnings.append(f"译文缺失 {missing} 段，已回退原文。")

    export_id = _unique_export_id(project, mode)
    export_dir = project / "exports" / export_id
    export_dir.mkdir(parents=True, exist_ok=True)
    name = safe_name(str(filename or "").strip() or document.title)
    assets = asset_plan(document, project)
    flat = len(requested) == 1
    written: list[tuple[Path, bool]] = []
    for fmt in requested:
        root = export_dir if flat else export_dir / fmt
        written.extend(_format_files(fmt, project, root, document, blocks, layer, mode, assets, name))

    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path, single in written:
        try:
            relative = path.relative_to(export_dir).as_posix()
        except ValueError:
            continue
        if relative in seen:
            continue
        seen.add(relative)
        files.append({
            "name": path.name,
            "rel_path": relative,
            "bytes": path.stat().st_size if path.is_file() else 0,
            "single_file": bool(single),
        })
    files.sort(key=lambda item: item["rel_path"])
    total_bytes = sum(int(item["bytes"]) for item in files)
    manifest = {
        "export_id": export_id,
        "created_at": utc_now(),
        "mode": mode,
        "locale": "" if mode == "source" else locale,
        "formats": requested,
        "path": str(export_dir),
        "files": files,
        "bytes": total_bytes,
        "warnings": warnings,
        "title": document.title,
        "chapters": chapter_values,
        "block_ids": block_values,
        "missing_policy": missing_policy,
        "block_count": len(blocks),
    }
    atomic_json(export_dir / "export.json", manifest)
    return manifest


def _recent_projection(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "export_id": str(value.get("export_id", "")),
        "created_at": str(value.get("created_at", "")),
        "mode": str(value.get("mode", "")),
        "locale": str(value.get("locale", "")),
        "formats": list(value.get("formats") or []),
        "path": str(value.get("path", "")),
        "files": list(value.get("files") or []),
        "bytes": int(value.get("bytes") or 0),
        "warnings": list(value.get("warnings") or []),
    }


def list_exports(store: ProjectStore, project_id: str) -> list[dict[str, Any]]:
    project = store.project_path(project_id)
    root = project / "exports"
    values: list[dict[str, Any]] = []
    if root.is_dir():
        for manifest_path in root.glob("*/export.json"):
            try:
                value = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(value, dict):
                values.append(_recent_projection(value))
    values.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return values[:50]


def resolve_export_file(store: ProjectStore, project_id: str, export_id: str, name: str) -> tuple[Path, str]:
    """把 ``(export_id, name)`` 解析成导出目录内的真实文件，并拒绝目录穿越。

    非法输入抛 ``StorageError``（API 层 400），文件不存在抛 ``FileNotFoundError``（API 层 404）。
    """
    if not _EXPORT_ID_RE.match(str(export_id or "")):
        raise StorageError("非法的导出标识。")
    if not name or "\x00" in name:
        raise StorageError("非法的文件名。")
    project = store.project_path(project_id)
    exports_root = (project / "exports").resolve()
    base = (exports_root / export_id).resolve()
    if base == exports_root or exports_root not in base.parents:
        raise StorageError("非法的导出标识。")
    candidate = (base / name).resolve()
    if candidate != base and base not in candidate.parents:
        raise StorageError("文件超出导出目录范围。")
    if candidate == base or not candidate.is_file():
        raise FileNotFoundError(name)
    extension = candidate.suffix.casefold()
    media_type = MEDIA_TYPES.get(extension) or mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return candidate, media_type
