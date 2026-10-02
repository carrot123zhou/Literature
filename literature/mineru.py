from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from html.parser import HTMLParser
from math import isfinite
from pathlib import Path
from typing import Any, Iterable

from .models import BBOX_SPACE_MINERU_1000, BBOX_SPACE_NONE, Block, Cell, Document, Page, TocNode, utc_now
from .storage import ProjectStore, StorageError, atomic_json, sha256_file


IGNORED_TYPES = {"header", "footer", "page_header", "page_footer", "page_number"}
IMAGE_TYPES = {"image", "chart"}
NOTE_TYPES = {"aside_text", "page_aside_text", "page_footnote"}

# MinerU 在不同版本里把页面几何写在 middle.json 或 layout.json 的 pdf_info 中。
PAGE_GEOMETRY_SOURCES = (("*_middle.json", "middle.json"), ("*_layout.json", "layout.json"))


class MinerUImportError(RuntimeError):
    pass


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\u00ad", "").split())


def span_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(span_text(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("content"), str):
            return value["content"]
        for key in (
            "children", "text", "value", "item_content", "paragraph_content", "text_content",
            "title_content", "page_footnote_content", "page_aside_text_content", "aside_text_content", "list_items",
        ):
            if key in value:
                return span_text(value[key])
    return ""


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    return [text for item in values if (text := clean_text(span_text(item) if isinstance(item, (dict, list)) else item))]


def find_one(directory: Path, patterns: Iterable[str]) -> Path | None:
    candidates: set[Path] = set()
    for pattern in patterns:
        candidates.update(item.resolve() for item in directory.glob(pattern) if item.is_file())
    return sorted(candidates, key=lambda item: item.name.casefold())[0] if candidates else None


def discover_results(root: str | Path) -> list[Path]:
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise MinerUImportError(f"MinerU 结果目录不存在：{root}")

    def valid(path: Path) -> bool:
        return bool(
            find_one(path, ("*_content_list_v2.json", "content_list_v2.json"))
            or find_one(path, ("*_content_list.json", "content_list.json"))
            or (path / "full.md").is_file()
        )

    if valid(root):
        return [root]
    found = set()
    for pattern in ("*_content_list_v2.json", "content_list_v2.json", "*_content_list.json", "content_list.json", "full.md"):
        found.update(item.parent.resolve() for item in root.rglob(pattern) if item.is_file())
    results = [path for path in found if valid(path) and not (path / "merge_report.json").is_file()]
    if not results:
        raise MinerUImportError(f"目录中没有可识别的 MinerU 输出：{root}")
    return sorted(results, key=lambda item: [int(piece) if piece.isdigit() else piece.casefold() for piece in re.split(r"(\d+)", str(item))])


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[dict[str, Any]] = []
        self.row = -1
        self.column = 0
        self.current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row += 1
            self.column = 0
        if tag in {"td", "th"}:
            values = dict(attrs)
            self.current = {
                "row": max(self.row, 0), "column": self.column, "text": "",
                "rowspan": int(values.get("rowspan") or 1), "colspan": int(values.get("colspan") or 1),
                "header": tag == "th", "raw": {"attrs": values},
            }

    def handle_data(self, data: str) -> None:
        if self.current is not None:
            self.current["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.current is not None:
            self.current["text"] = clean_text(self.current["text"])
            self.cells.append(self.current)
            self.column += max(1, int(self.current["colspan"]))
            self.current = None


def _table_cells(block_id: str, table_html: str) -> list[Cell]:
    if not table_html:
        return []
    parser = _TableParser()
    try:
        parser.feed(table_html)
    except (ValueError, TypeError):
        return []
    return [Cell(cell_id=f"{block_id}:cell:{item['row']}:{item['column']}", **item) for item in parser.cells]


def _asset_path(item: dict[str, Any], content: dict[str, Any], directory: Path) -> Path | None:
    source = content.get("image_source") if isinstance(content.get("image_source"), dict) else {}
    value = source.get("path") or content.get("img_path") or content.get("image_path") or item.get("img_path") or item.get("image_path")
    if not value:
        return None
    path = directory / str(value)
    if not path.is_file():
        path = directory / "images" / Path(str(value)).name
    return path if path.is_file() else None


def _valid_bbox(value: Any) -> list[float]:
    """只接受 4 个有限数字且 x1>x0、y1>y0 的 bbox；其他情况视为没有 bbox。"""
    if not isinstance(value, list) or len(value) != 4:
        return []
    try:
        bbox = [float(number) for number in value]
    except (TypeError, ValueError):
        return []
    if not all(isfinite(number) for number in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return []
    return bbox


def _normalized_block(
    item: dict[str, Any], *, project_id: str, order: int, page_index: int, source_file: str,
    source_directory: Path, assets_directory: Path, resources: dict[str, dict[str, Any]], legacy: bool = False,
) -> Block | None:
    raw_kind = str(item.get("type", "paragraph"))
    if raw_kind in IGNORED_TYPES:
        return None
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    bbox = _valid_bbox(item.get("bbox"))
    block_id = f"blk-{order:08d}"
    kind = raw_kind
    level = 0
    text = ""
    items: list[str] = []
    caption: list[str] = []
    footnote: list[str] = []
    resource_refs: list[str] = []
    format_data: dict[str, Any] = {"mineru_type": raw_kind, "source_file": source_file}
    if bbox:
        # MinerU content_list/content_list_v2 的 bbox 是 0-1000 归一化坐标。
        format_data["bbox_space"] = BBOX_SPACE_MINERU_1000

    if raw_kind == "title" or (legacy and raw_kind == "text" and item.get("text_level")):
        kind = "title"
        try:
            level = max(1, min(6, int(content.get("level") if not legacy else item.get("text_level") or 1)))
        except (TypeError, ValueError):
            level = 1
        text = clean_text(span_text(content.get("title_content")) if not legacy else item.get("text"))
    elif raw_kind in {"paragraph", "text"}:
        kind = "paragraph"
        text = clean_text(span_text(content.get("paragraph_content") or content.get("text_content") or content) if not legacy else item.get("text"))
    elif raw_kind in IMAGE_TYPES:
        kind = "image"
        text = clean_text(content.get("content") if not legacy else item.get("content"))
        caption = string_list((content if not legacy else item).get("image_caption") or (content if not legacy else item).get("chart_caption"))
        footnote = string_list((content if not legacy else item).get("image_footnote") or (content if not legacy else item).get("chart_footnote"))
    elif raw_kind == "table":
        kind = "table"
        table_html = str((content.get("table_content") or content.get("content") or content.get("table_body")) if not legacy else (item.get("table_body") or item.get("content") or ""))
        format_data["table_html"] = table_html
        caption = string_list((content if not legacy else item).get("table_caption"))
        footnote = string_list((content if not legacy else item).get("table_footnote"))
    elif raw_kind in {"equation", "equation_interline"}:
        kind = "equation"
        text = str((content.get("math_content") or content.get("content")) if not legacy else (item.get("text") or item.get("content") or "")).strip()
    elif raw_kind in {"list", "index"}:
        kind = "list"
        items = string_list((content if not legacy else item).get("list_items"))
    elif raw_kind in NOTE_TYPES:
        kind = "footnote"
        text = clean_text(span_text(content.get(f"{raw_kind}_content") or content) if not legacy else (item.get("text") or item.get("content")))
    elif raw_kind in {"code", "algorithm"}:
        kind = "code"
        text = str((content.get("code_content") or content.get("code_body") or content.get("content")) if not legacy else (item.get("code_body") or item.get("content") or "")).strip()
    else:
        kind = "paragraph"
        text = clean_text(span_text(content) if not legacy else (item.get("text") or item.get("content")))

    image = _asset_path(item, content, source_directory)
    if image:
        data_hash = sha256_file(image)
        asset_name = f"{data_hash[:24]}{image.suffix.casefold()}"
        destination = assets_directory / asset_name
        if not destination.exists():
            shutil.copy2(image, destination)
        resource_id = f"asset:{data_hash[:24]}"
        resources[resource_id] = {
            "path": f"assets/{asset_name}", "sha256": data_hash, "size": destination.stat().st_size,
            "source": str(image), "media_type": image.suffix.casefold().lstrip("."),
        }
        resource_refs.append(resource_id)

    block = Block(
        block_id=block_id, type=kind, order=order, page_index=page_index, source_text=text,
        bbox=bbox, level=level, items=items,
        resource_refs=resource_refs, caption=caption, footnote=footnote,
        format=format_data, raw=item,
    )
    if kind == "table":
        block.cells = _table_cells(block_id, format_data.get("table_html", ""))
    if not (text or items or caption or footnote or resource_refs or block.cells or format_data.get("table_html")):
        return None
    return block


def _markdown_blocks(path: Path, start_order: int) -> tuple[list[Block], int]:
    blocks: list[Block] = []
    order = start_order
    paragraph: list[str] = []

    def flush() -> None:
        nonlocal order
        if paragraph:
            text = clean_text(" ".join(paragraph))
            if text:
                blocks.append(Block(f"blk-{order:08d}", "paragraph", order, 0, source_text=text, format={"source_file": str(path)}, raw={"markdown": text}))
                order += 1
            paragraph.clear()

    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if heading:
            flush()
            blocks.append(Block(f"blk-{order:08d}", "title", order, 0, source_text=clean_text(heading.group(2)), level=len(heading.group(1)), format={"source_file": str(path)}, raw={"markdown": line}))
            order += 1
        elif not line.strip():
            flush()
        elif not line.lstrip().startswith("!["):
            paragraph.append(line.strip())
    flush()
    return blocks, order


def detect_language(blocks: Iterable[Block]) -> str:
    sample = " ".join(block.source_text for block in blocks if block.source_text)[:30000]
    if re.search(r"[\u3040-\u30ff]", sample):
        return "ja"
    lowered = f" {sample.casefold()} "
    german = len(re.findall(r"\b(der|die|das|und|nicht|eine|einer|ist|mit|für)\b", lowered)) + len(re.findall(r"[äöüß]", lowered)) * 2
    french = len(re.findall(r"\b(le|la|les|des|une|est|dans|pour|avec|que)\b", lowered)) + len(re.findall(r"[àâçéèêëîïôùûüÿœ]", lowered)) * 2
    if german > french and german >= 4:
        return "de"
    if french >= 4:
        return "fr"
    return "en"


def build_toc(blocks: list[Block]) -> list[TocNode]:
    roots: list[TocNode] = []
    stack: list[TocNode] = []
    for block in blocks:
        if block.type != "title" or not block.source_text:
            continue
        node = TocNode(
            node_id=f"toc:{block.block_id}", title=block.source_text, level=block.level or 1,
            block_id=block.block_id, page_index=block.page_index,
        )
        while stack and stack[-1].level >= node.level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
            block.parent_id = stack[-1].block_id
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _page_geometry(directory: Path) -> tuple[Path | None, list[dict[str, Any]]]:
    """读取页面几何：优先 middle.json，其次 layout.json（当前精准解析实际返回的文件）。"""
    for patterns in PAGE_GEOMETRY_SOURCES:
        path = find_one(directory, patterns)
        if not path:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        candidate = payload.get("pdf_info") if isinstance(payload, dict) else None
        if isinstance(candidate, list):
            return path, [item for item in candidate if isinstance(item, dict)]
    return None, []


def _page_metadata(directory: Path, project: Path, page_offset: int, page_count: int) -> list[Page]:
    geometry_file, values = _page_geometry(directory)
    pages = []
    for local_index in range(page_count):
        raw = values[local_index] if local_index < len(values) else {}
        size = raw.get("page_size") if isinstance(raw.get("page_size"), list) else []
        width = float(size[0]) if len(size) > 1 and isinstance(size[0], (int, float)) else float(raw.get("width") or 0)
        height = float(size[1]) if len(size) > 1 and isinstance(size[1], (int, float)) else float(raw.get("height") or 0)
        try:
            source_page_index = int(raw.get("page_idx", local_index))
        except (TypeError, ValueError):
            source_page_index = local_index
        pages.append(Page(
            page_index=page_offset + local_index, width=width, height=height,
            rotation=int(raw.get("rotation") or 0),
            source_file=str(geometry_file.relative_to(project)) if geometry_file else "",
            source_page_index=source_page_index,
            raw={key: value for key, value in raw.items() if key not in {"preproc_blocks", "para_blocks", "discarded_blocks"}},
        ))
    return pages


def render_source_markdown(document: Document) -> str:
    lines = []
    for block in document.blocks:
        if block.type == "title":
            lines.extend([f"{'#' * max(1, block.level)} {block.source_text}", ""])
        elif block.type == "paragraph":
            lines.extend([block.source_text, ""])
        elif block.type == "list":
            lines.extend([*(f"- {item}" for item in block.items), ""])
        elif block.type == "equation":
            lines.extend(["$$", block.source_text, "$$", ""])
        elif block.type == "footnote":
            lines.extend([f"> [页下注] {block.source_text}", ""])
        elif block.type == "table":
            lines.extend([block.format.get("table_html", ""), ""])
        elif block.type == "image":
            for resource in block.resource_refs:
                path = document.resources.get(resource, {}).get("path", "")
                if path:
                    lines.append(f"![]({path})")
            lines.extend(block.caption)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def normalize_imported_project(store: ProjectStore, project_id: str) -> Document:
    project = store.project_path(project_id)
    raw_root = project / "ocr" / "raw" / "mineru"
    result_dirs = []
    for child in sorted(raw_root.iterdir()):
        if child.is_dir():
            result_dirs.extend(discover_results(child))
    if not result_dirs:
        raise MinerUImportError("封存目录中没有可规范化的 MinerU 输出。")

    manifest = store.load_manifest(project_id)
    resources: dict[str, dict[str, Any]] = {}
    blocks: list[Block] = []
    pages: list[Page] = []
    source_files = []
    page_offset = 0
    order = 1
    for directory in result_dirs:
        v2 = find_one(directory, ("*_content_list_v2.json", "content_list_v2.json"))
        legacy = find_one(directory, ("*_content_list.json", "content_list.json"))
        if v2:
            value = json.loads(v2.read_text(encoding="utf-8"))
            if not isinstance(value, list) or any(not isinstance(page, list) for page in value):
                raise MinerUImportError(f"content_list_v2 顶层格式错误：{v2}")
            for local_page, page in enumerate(value):
                for item in page:
                    if isinstance(item, dict):
                        block = _normalized_block(
                            item, project_id=project_id, order=order, page_index=page_offset + local_page,
                            source_file=str(v2.relative_to(project)), source_directory=directory,
                            assets_directory=project / "assets", resources=resources,
                        )
                        if block:
                            blocks.append(block)
                            order += 1
            page_count = len(value)
            source = v2
            representation = "content_list_v2"
        elif legacy:
            value = json.loads(legacy.read_text(encoding="utf-8"))
            if not isinstance(value, list):
                raise MinerUImportError(f"content_list 顶层格式错误：{legacy}")
            max_page = 0
            for item in value:
                if not isinstance(item, dict):
                    continue
                try:
                    local_page = max(0, int(item.get("page_idx", 0)))
                except (TypeError, ValueError):
                    local_page = 0
                max_page = max(max_page, local_page)
                block = _normalized_block(
                    item, project_id=project_id, order=order, page_index=page_offset + local_page,
                    source_file=str(legacy.relative_to(project)), source_directory=directory,
                    assets_directory=project / "assets", resources=resources, legacy=True,
                )
                if block:
                    blocks.append(block)
                    order += 1
            page_count = max_page + 1
            source = legacy
            representation = "content_list"
        else:
            source = directory / "full.md"
            new_blocks, order = _markdown_blocks(source, order)
            for block in new_blocks:
                block.page_index += page_offset
            blocks.extend(new_blocks)
            page_count = 1
            representation = "markdown"
        source_files.append({
            "path": str(source.relative_to(project)), "representation": representation,
            "sha256": sha256_file(source), "page_offset": page_offset, "page_count": page_count,
        })
        pages.extend(_page_metadata(directory, project, page_offset, page_count))
        page_offset += page_count

    source_language_mode = manifest.get("source_language_mode", manifest.get("source_language", "auto"))
    source_language = source_language_mode
    if source_language_mode == "auto":
        source_language = detect_language(blocks)
    document = Document(
        project_id=project_id, title=manifest["title"], source_language=source_language,
        source_language_mode=source_language_mode,
        target_language=manifest.get("target_language", "zh-CN"), page_count=page_offset, pages=pages,
        bbox_space=BBOX_SPACE_MINERU_1000 if any(block.bbox for block in blocks) else BBOX_SPACE_NONE,
        blocks=blocks, resources=resources, source_files=source_files,
    )
    document.toc = build_toc(document.blocks)
    store.save_document(document)
    (project / "document" / "source.md").write_text(render_source_markdown(document), encoding="utf-8")
    store.update_manifest(
        project_id, state="ready", source_language=source_language,
        source_language_mode=source_language_mode, detected_source_language=detect_language(blocks),
        page_count=page_offset, block_count=len(blocks),
    )
    return document


def import_mineru(store: ProjectStore, project_id: str, sources: Iterable[str | Path]) -> Document:
    resolved = []
    for raw in sources:
        resolved.extend(discover_results(raw))
    store.import_raw_mineru(project_id, resolved)
    return normalize_imported_project(store, project_id)
