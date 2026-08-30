from __future__ import annotations

import html
import json
import mimetypes
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

from .models import Block, Document, TranslationEntry, TranslationLayer, utc_now
from .storage import ProjectStore, StorageError, atomic_json, safe_name


ExportMode = Literal["source", "translation", "bilingual"]


def _entry(layer: TranslationLayer | None, block: Block) -> TranslationEntry | None:
    return layer.entries.get(block.block_id) if layer else None


def _text(block: Block, entry: TranslationEntry | None, mode: ExportMode) -> list[tuple[str, str]]:
    source = block.source_text
    translated = entry.text if entry and entry.text else source
    if mode == "source":
        return [("source", source)]
    if mode == "translation":
        return [("translation", translated)]
    return [("source", source), ("translation", translated)] if source else [("translation", translated)]


def _lists(block: Block, entry: TranslationEntry | None, mode: ExportMode) -> list[tuple[str, list[str]]]:
    translated = entry.items if entry and entry.items else block.items
    if mode == "source":
        return [("source", block.items)]
    if mode == "translation":
        return [("translation", translated)]
    return [("source", block.items), ("translation", translated)]


def _block_markdown(block: Block, entry: TranslationEntry | None, mode: ExportMode) -> list[str]:
    lines: list[str] = []
    if block.type == "title":
        for _, value in _text(block, entry, mode):
            lines.extend([f"{'#' * max(1, block.level)} {value}", ""])
    elif block.type in {"paragraph", "code", "footnote"}:
        for role, value in _text(block, entry, mode):
            if not value:
                continue
            prefix = "> [页下注] " if block.type == "footnote" else ""
            if mode == "bilingual" and role == "translation":
                prefix += "> "
            lines.extend([prefix + value, ""])
    elif block.type == "list":
        for role, values in _lists(block, entry, mode):
            if mode == "bilingual" and role == "translation":
                lines.extend([*(f"> - {value}" for value in values), ""])
            else:
                lines.extend([*(f"- {value}" for value in values), ""])
    elif block.type == "equation":
        lines.extend(["$$", block.source_text, "$$", ""])
    elif block.type == "table":
        if block.cells:
            values = entry.cells if entry and entry.cells else {}
            rows = max(cell.row for cell in block.cells) + 1
            columns = max(cell.column for cell in block.cells) + 1
            for role in (["source", "translation"] if mode == "bilingual" else [mode]):
                matrix = [["" for _ in range(columns)] for _ in range(rows)]
                for cell in block.cells:
                    matrix[cell.row][cell.column] = values.get(cell.cell_id, cell.text) if role == "translation" else cell.text
                if matrix:
                    lines.append("| " + " | ".join(matrix[0]) + " |")
                    lines.append("| " + " | ".join("---" for _ in matrix[0]) + " |")
                    lines.extend("| " + " | ".join(row) + " |" for row in matrix[1:])
                    lines.append("")
        else:
            lines.extend([block.format.get("table_html", ""), ""])
    elif block.type == "image":
        for resource in block.resource_refs:
            lines.append(f"![]({resource})")
        captions = entry.caption if entry and entry.caption and mode != "source" else block.caption
        lines.extend([*captions, ""])
    return lines


def render_markdown(document: Document, layer: TranslationLayer | None, mode: ExportMode) -> str:
    lines: list[str] = []
    for block in document.blocks:
        lines.extend(_block_markdown(block, _entry(layer, block), mode))
    value = "\n".join(lines).rstrip() + "\n"
    for resource_id, resource in document.resources.items():
        value = value.replace(f"({resource_id})", f"(assets/{Path(resource['path']).name})")
    return value


def _copy_assets(project: Path, target: Path, document: Document) -> dict[str, str]:
    target.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for resource_id, value in document.resources.items():
        source = project / value["path"]
        if source.is_file():
            destination = target / source.name
            if not destination.exists():
                shutil.copy2(source, destination)
            mapping[resource_id] = destination.name
    return mapping


def _table_html(block: Block, entry: TranslationEntry | None, role: str) -> str:
    if not block.cells:
        return block.format.get("table_html", "")
    values = entry.cells if entry and entry.cells and role == "translation" else {}
    rows = max(cell.row for cell in block.cells) + 1
    by_row: list[list[Any]] = [[] for _ in range(rows)]
    for cell in block.cells:
        by_row[cell.row].append(cell)
    output = ["<table>"]
    for row in by_row:
        output.append("<tr>")
        for cell in sorted(row, key=lambda item: item.column):
            tag = "th" if cell.header else "td"
            attrs = ""
            if cell.rowspan > 1:
                attrs += f' rowspan="{cell.rowspan}"'
            if cell.colspan > 1:
                attrs += f' colspan="{cell.colspan}"'
            output.append(f"<{tag}{attrs}>{html.escape(values.get(cell.cell_id, cell.text))}</{tag}>")
        output.append("</tr>")
    output.append("</table>")
    return "".join(output)


def _block_html(block: Block, entry: TranslationEntry | None, mode: ExportMode, assets: dict[str, str]) -> str:
    output: list[str] = [f'<section class="block block-{html.escape(block.type)}" data-block-id="{block.block_id}" data-page="{block.page_index + 1}">']
    if block.type == "title":
        tag = f"h{max(1, min(6, block.level or 1))}"
        for role, text in _text(block, entry, mode):
            output.append(f'<{tag} class="{role}">{html.escape(text)}</{tag}>')
    elif block.type in {"paragraph", "footnote", "code"}:
        tag = "pre" if block.type == "code" else "p"
        for role, text in _text(block, entry, mode):
            output.append(f'<{tag} class="{role}">{html.escape(text)}</{tag}>')
    elif block.type == "list":
        for role, items in _lists(block, entry, mode):
            output.append(f'<ul class="{role}">' + "".join(f"<li>{html.escape(value)}</li>" for value in items) + "</ul>")
    elif block.type == "equation":
        output.append(f'<pre class="equation">{html.escape(block.source_text)}</pre>')
    elif block.type == "table":
        roles = ["source", "translation"] if mode == "bilingual" else [mode]
        for role in roles:
            output.append(f'<div class="{role}">{_table_html(block, entry, role)}</div>')
    elif block.type == "image":
        for resource in block.resource_refs:
            if resource in assets:
                output.append(f'<img src="assets/{html.escape(assets[resource])}" alt=""/>')
        captions = block.caption if mode == "source" or not entry or not entry.caption else entry.caption
        output.extend(f"<figcaption>{html.escape(value)}</figcaption>" for value in captions)
    output.append("</section>")
    return "".join(output)


BASE_CSS = """
:root{color:#20322b;background:#f6f5ef;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans CJK SC",sans-serif}
body{max-width:50rem;margin:3rem auto;padding:0 1.5rem;line-height:1.72}.translation{color:#2f5142}.source+.translation{margin-top:.35rem;padding-left:1rem;border-left:3px solid #c8e569}
img{max-width:100%;height:auto}table{border-collapse:collapse;width:100%;margin:1rem 0}td,th{border:1px solid #bac5bd;padding:.45rem;text-align:left}figcaption{color:#63716b;font-size:.92em}.block{margin:.7rem 0}pre{white-space:pre-wrap}
"""


def export_html(store: ProjectStore, project_id: str, *, mode: ExportMode = "translation", locale: str = "zh-CN") -> Path:
    project = store.project_path(project_id)
    document = store.load_document(project_id)
    layer = None if mode == "source" else store.load_translation(project_id, locale)
    target = project / "exports" / f"{safe_name(document.title)}-{mode}-html"
    target.mkdir(parents=True, exist_ok=True)
    assets = _copy_assets(project, target / "assets", document)
    body = "".join(_block_html(block, _entry(layer, block), mode, assets) for block in document.blocks)
    page = f'<!doctype html><html lang="{html.escape(locale if mode != "source" else document.source_language)}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(document.title)}</title><link rel="stylesheet" href="style.css"></head><body><main>{body}</main></body></html>'
    (target / "index.html").write_text(page, encoding="utf-8")
    (target / "style.css").write_text(BASE_CSS.strip() + "\n", encoding="utf-8")
    return target / "index.html"


def export_package(store: ProjectStore, project_id: str, *, mode: ExportMode = "translation", locale: str = "zh-CN") -> Path:
    project = store.project_path(project_id)
    document = store.load_document(project_id)
    layer = None if mode == "source" else store.load_translation(project_id, locale)
    target = project / "exports" / f"{safe_name(document.title)}-{mode}-md-json"
    target.mkdir(parents=True, exist_ok=True)
    (target / "full.md").write_text(render_markdown(document, layer, mode), encoding="utf-8")
    atomic_json(target / "document.json", document.to_dict())
    if layer:
        atomic_json(target / "translation.json", layer.to_dict())
    _copy_assets(project, target / "assets", document)
    atomic_json(target / "export_report.json", {"format": "md+json", "mode": mode, "locale": locale, "created_at": utc_now(), "blocks": len(document.blocks)})
    return target


def _xhtml(title: str, language: str, body: str) -> str:
    return f'''<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="{html.escape(language)}" xml:lang="{html.escape(language)}"><head><title>{html.escape(title)}</title><link rel="stylesheet" type="text/css" href="../styles/book.css"/></head><body>{body}</body></html>'''


def export_epub(store: ProjectStore, project_id: str, *, mode: ExportMode = "translation", locale: str = "zh-CN") -> Path:
    project = store.project_path(project_id)
    document = store.load_document(project_id)
    layer = None if mode == "source" else store.load_translation(project_id, locale)
    output = project / "exports" / f"{safe_name(document.title)}-{mode}.epub"
    language = document.source_language if mode == "source" else locale
    assets = {key: Path(value["path"]).name for key, value in document.resources.items()}
    sections: list[tuple[str, list[Block]]] = []
    current_title = document.title
    current: list[Block] = []
    for block in document.blocks:
        if block.type == "title" and block.level <= 2 and current:
            sections.append((current_title, current))
            current = []
            current_title = block.source_text
        current.append(block)
    if current:
        sections.append((current_title, current))
    if not sections:
        sections = [(document.title, document.blocks)]
    book_id = f"urn:uuid:{uuid.uuid4()}"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("EPUB/styles/book.css", BASE_CSS)
        manifest_items = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>', '<item id="css" href="styles/book.css" media-type="text/css"/>']
        spine = []
        nav_links = []
        for index, (title, blocks) in enumerate(sections, 1):
            filename = f"text/section-{index:04d}.xhtml"
            body = "".join(_block_html(block, _entry(layer, block), mode, assets) for block in blocks)
            archive.writestr(f"EPUB/{filename}", _xhtml(title, language, body))
            manifest_items.append(f'<item id="section-{index}" href="{filename}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="section-{index}"/>')
            nav_links.append(f'<li><a href="{filename}">{html.escape(title)}</a></li>')
        for resource_id, value in document.resources.items():
            source = project / value["path"]
            if not source.is_file():
                continue
            name = source.name
            media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
            archive.write(source, f"EPUB/text/assets/{name}")
            manifest_items.append(f'<item id="asset-{len(manifest_items)}" href="text/assets/{html.escape(name)}" media-type="{media_type}"/>')
        archive.writestr("EPUB/nav.xhtml", _xhtml("目录", language, f'<nav xmlns:epub="http://www.idpf.org/2007/ops" epub:type="toc"><h1>目录</h1><ol>{"".join(nav_links)}</ol></nav>'))
        package = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="book-id" version="3.0" xml:lang="{html.escape(language)}"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">{book_id}</dc:identifier><dc:title>{html.escape(document.title)}</dc:title><dc:language>{html.escape(language)}</dc:language><meta property="dcterms:modified">{utc_now().split('.')[0]}Z</meta></metadata><manifest>{''.join(manifest_items)}</manifest><spine>{''.join(spine)}</spine></package>'''
        archive.writestr("EPUB/package.opf", package)
    validate_epub(output)
    return output


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


def export_docx(store: ProjectStore, project_id: str, *, mode: ExportMode = "translation", locale: str = "zh-CN") -> Path:
    try:
        from docx import Document as WordDocument
        from docx.shared import Inches
    except ImportError as error:
        raise StorageError("导出 DOCX 需要 python-docx。") from error
    project = store.project_path(project_id)
    document = store.load_document(project_id)
    layer = None if mode == "source" else store.load_translation(project_id, locale)
    output = project / "exports" / f"{safe_name(document.title)}-{mode}.docx"
    word = WordDocument()
    word.core_properties.title = document.title
    for block in document.blocks:
        entry = _entry(layer, block)
        if block.type == "title":
            for _, value in _text(block, entry, mode):
                word.add_heading(value, level=max(1, min(9, block.level or 1)))
        elif block.type in {"paragraph", "footnote", "code"}:
            for role, value in _text(block, entry, mode):
                paragraph = word.add_paragraph(value)
                if mode == "bilingual" and role == "translation":
                    paragraph.style = word.styles["Quote"]
        elif block.type == "list":
            for _, values in _lists(block, entry, mode):
                for value in values:
                    word.add_paragraph(value, style="List Bullet")
        elif block.type == "equation":
            word.add_paragraph(block.source_text)
        elif block.type == "table" and block.cells:
            rows = max(cell.row for cell in block.cells) + 1
            columns = max(cell.column for cell in block.cells) + 1
            for role in (["source", "translation"] if mode == "bilingual" else [mode]):
                table = word.add_table(rows=rows, cols=columns)
                values = entry.cells if entry and role == "translation" else {}
                for cell in block.cells:
                    table.cell(cell.row, cell.column).text = values.get(cell.cell_id, cell.text)
        elif block.type == "image":
            for resource_id in block.resource_refs:
                value = document.resources.get(resource_id)
                if value and (project / value["path"]).is_file():
                    try:
                        word.add_picture(str(project / value["path"]), width=Inches(5.8))
                    except Exception:
                        word.add_paragraph(f"[图片：{Path(value['path']).name}]")
            captions = entry.caption if entry and mode != "source" else block.caption
            for caption in captions:
                word.add_paragraph(caption, style="Caption")
    word.save(output)
    return output


def export_all(store: ProjectStore, project_id: str, *, mode: ExportMode, locale: str = "zh-CN", formats: list[str] | None = None) -> dict[str, str]:
    formats = formats or ["html", "md+json", "epub", "docx"]
    outputs: dict[str, str] = {}
    for format_name in formats:
        if format_name == "html":
            outputs[format_name] = str(export_html(store, project_id, mode=mode, locale=locale))
        elif format_name == "md+json":
            outputs[format_name] = str(export_package(store, project_id, mode=mode, locale=locale))
        elif format_name == "epub":
            outputs[format_name] = str(export_epub(store, project_id, mode=mode, locale=locale))
        elif format_name == "docx":
            outputs[format_name] = str(export_docx(store, project_id, mode=mode, locale=locale))
        else:
            raise StorageError(f"不支持的导出格式：{format_name}")
    return outputs
