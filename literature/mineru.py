from __future__ import annotations

import hashlib
import html
import itertools
import json
import re
import shutil
from html.parser import HTMLParser
from math import isfinite
from pathlib import Path
from typing import Any, Iterable

from .models import BBOX_SPACE_MINERU_1000, BBOX_SPACE_NONE, Block, Cell, Document, Page, TocNode, utc_now
from .storage import ProjectStore, StorageError, atomic_json, atomic_text, sha256_file


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
        # 已被上一行 rowspan 占用的 (行, 列)；跨行单元格让后续行的列号从右往下顺延，
        # 否则 rowspan 后面的单元格会错位到左边（与 HTML/Word 实际合并语义不符）。
        self.occupied: set[tuple[int, int]] = set()

    def _skip_occupied(self) -> None:
        while (max(self.row, 0), self.column) in self.occupied:
            self.column += 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row += 1
            self.column = 0
            self._skip_occupied()
        if tag in {"td", "th"}:
            self._skip_occupied()
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
            rowspan = max(1, int(self.current["rowspan"]))
            colspan = max(1, int(self.current["colspan"]))
            for row in range(self.row, self.row + rowspan):
                for column in range(self.column, self.column + colspan):
                    self.occupied.add((row, column))
            self.column += colspan
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
    block_id: str | None = None,
) -> Block | None:
    raw_kind = str(item.get("type", "paragraph"))
    if raw_kind in IGNORED_TYPES:
        return None
    content = item.get("content") if isinstance(item.get("content"), dict) else {}
    bbox = _valid_bbox(item.get("bbox"))
    block_id = block_id or f"blk-{order:08d}"
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


def write_source_markdown(project: Path, document: Document) -> None:
    """原子写入可读投影 ``document/source.md``。

    source.md 是 ``document.json`` 的人类可读投影，不是事实来源；它会在导入、结构补丁、
    撤销三处重写，原本用 ``write_text`` 直接覆盖，崩溃或并发读到的是被截断的 Markdown。
    实测渲染 + 写入仅 1–3 ms（最大 8 MB 文档），无需删掉这个投影，但必须用原子写法。
    """
    atomic_text(project / "document" / "source.md", render_source_markdown(document))


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
    from .structure_signals import enrich_furniture_evidence
    document = enrich_furniture_evidence(document, project)
    document.toc = build_toc(document.blocks)
    store.save_document(document)
    write_source_markdown(project, document)
    store.update_manifest(
        project_id, state="ready", source_language=source_language,
        source_language_mode=source_language_mode, detected_source_language=detect_language(blocks),
        page_count=page_offset, block_count=len(blocks),
    )
    return document


def coverage_pages(document: Document) -> set[int] | None:
    """返回 document 已覆盖的源页（0-based）集合；缺失 coverage 时返回 None（未知）。"""
    coverage = document.coverage if isinstance(document.coverage, dict) else {}
    ranges = coverage.get("ranges")
    if not isinstance(ranges, list) or not ranges:
        return None
    pages: set[int] = set()
    for item in ranges:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                start, end = int(item[0]), int(item[1])
            except (TypeError, ValueError):
                continue
            if 1 <= start <= end:
                pages.update(range(start - 1, end))
    return pages


def merge_ranges(pages: Iterable[int]) -> list[list[int]]:
    """把源页号（1-based）合并为升序、不重叠的闭区间。"""
    ordered = sorted({int(page) for page in pages})
    ranges: list[list[int]] = []
    for page in ordered:
        if ranges and page == ranges[-1][1] + 1:
            ranges[-1][1] = page
        else:
            ranges.append([page, page])
    return ranges


def coverage_payload(source_page_count: int, covered: Iterable[int] | None) -> dict[str, Any]:
    """构造 manifest / document 共用的 parse_coverage。covered 为 0-based 源页集合。"""
    total = max(0, int(source_page_count or 0))
    if covered is None or total <= 0:
        pages: set[int] = set(covered or [])
        return {"source_page_count": total, "covered_pages": len(pages), "ranges": merge_ranges(page + 1 for page in pages), "state": "unknown"}
    pages = {int(page) for page in covered if 0 <= int(page) < total}
    if not pages:
        state = "unparsed"
    elif all(page in pages for page in range(total)):
        state = "full"
    else:
        state = "partial"
    return {
        "source_page_count": total,
        "covered_pages": len(pages),
        "ranges": merge_ranges(page + 1 for page in pages),
        "state": state,
    }


def _parse_slice(
    directory: Path, project: Path, project_id: str, resources: dict[str, dict[str, Any]],
    *, source_pages: list[int], block_ids: Any,
) -> tuple[list[Block], list[Page], dict[str, Any]]:
    """解析一个 MinerU 结果目录，把本地页号映射为真实源页（0-based）。

    当提供了 ``source_pages``（继续/指定范围解析）时，结果页数与源页映射必须一致；
    不匹配则抛错中止，绝不静默接纳（避免把结果错误归到别的源页）。
    """
    v2 = find_one(directory, ("*_content_list_v2.json", "content_list_v2.json"))
    legacy = find_one(directory, ("*_content_list.json", "content_list.json"))
    blocks: list[Block] = []
    per_page_seq: dict[int, int] = {}
    requested = [int(page) for page in source_pages]
    if requested and any(page < 1 for page in requested):
        raise MinerUImportError(f"切片源页号必须是 1-based 正整数：{source_pages}")

    def source_index(local_page: int) -> int:
        if requested:
            if local_page < 0 or local_page >= len(requested):
                raise MinerUImportError(
                    f"MinerU 结果页号 {local_page} 超出本次请求的 {len(requested)} 页范围：{directory.name}"
                )
            return requested[local_page] - 1
        return local_page

    if v2:
        value = json.loads(v2.read_text(encoding="utf-8"))
        if not isinstance(value, list) or any(not isinstance(page, list) for page in value):
            raise MinerUImportError(f"content_list_v2 顶层格式错误：{v2}")
        page_count = len(value)
        if requested and page_count != len(requested):
            raise MinerUImportError(
                f"MinerU 返回 {page_count} 页，但本次请求解析 {len(requested)} 页"
                f"（{source_page_count_text(requested)}）；为避免页码错位已中止。"
            )
        for local_page, page in enumerate(value):
            index = source_index(local_page)
            for item in page:
                if not isinstance(item, dict):
                    continue
                seq = per_page_seq.get(index, 0)
                block = _normalized_block(
                    item, project_id=project_id, order=0, page_index=index,
                    source_file=str(v2.relative_to(project)), source_directory=directory,
                    assets_directory=project / "assets", resources=resources,
                    block_id=block_ids(index, seq),
                )
                if block:
                    per_page_seq[index] = seq + 1
                    blocks.append(block)
        source = v2
        representation = "content_list_v2"
    elif legacy:
        value = json.loads(legacy.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise MinerUImportError(f"content_list 顶层格式错误：{legacy}")
        max_page = -1
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                local_page = int(item.get("page_idx", 0))
            except (TypeError, ValueError) as error:
                raise MinerUImportError(f"content_list 的 page_idx 非法：{item.get('page_idx')!r}") from error
            if local_page < 0:
                raise MinerUImportError(f"content_list 的 page_idx 为负数：{local_page}")
            if requested and local_page >= len(requested):
                raise MinerUImportError(
                    f"content_list 的 page_idx {local_page} 超出本次请求的 {len(requested)} 页范围。"
                )
            max_page = max(max_page, local_page)
            index = source_index(local_page)
            seq = per_page_seq.get(index, 0)
            block = _normalized_block(
                item, project_id=project_id, order=0, page_index=index,
                source_file=str(legacy.relative_to(project)), source_directory=directory,
                assets_directory=project / "assets", resources=resources, legacy=True,
                block_id=block_ids(index, seq),
            )
            if block:
                per_page_seq[index] = seq + 1
                blocks.append(block)
        page_count = max_page + 1
        source = legacy
        representation = "content_list"
    else:
        if requested and len(requested) != 1:
            raise MinerUImportError(
                "Markdown 结果无法对应多个源页；为避免页码错位已中止。请使用 MinerU 的 JSON 输出。"
            )
        source = directory / "full.md"
        index = source_index(0)
        new_blocks, _next = _markdown_blocks(source, 0)
        for seq, block in enumerate(new_blocks):
            block.block_id = block_ids(index, seq)
            block.page_index = index
            blocks.append(block)
        page_count = 1
        representation = "markdown"
    pages = _page_metadata(directory, project, 0, page_count)
    for local_page, page in enumerate(pages):
        page.source_page_index = source_index(local_page)
        page.page_index = page.source_page_index
    record = {
        "path": str(source.relative_to(project)), "representation": representation,
        "sha256": sha256_file(source), "page_count": page_count,
        "source_pages": list(requested), "page_offset": source_index(0) if page_count else 0,
    }
    return blocks, pages, record


def source_page_count_text(pages: list[int]) -> str:
    ordered = sorted({int(page) for page in pages})
    if not ordered:
        return ""
    return f"{ordered[0]}–{ordered[-1]}" if ordered[0] != ordered[-1] else str(ordered[0])


def merge_imported_project(
    store: ProjectStore, project_id: str, entries: Iterable[dict[str, Any]],
    *, source_page_count: int = 0, append: bool = False,
) -> Document:
    """把本次新封存的切片规范化并合并进 document（首次导入 / 继续解析共用）。

    - 只处理 ``entries`` 指定的新目录；旧 raw 哈希与旧 block 一律不改。
    - 新 block 使用独立 ID 命名空间，旧 block_id / structure_revision / 人工校对保留。
    - 已有覆盖页会被过滤，绝不覆盖；只有全部成功后才写 parse_coverage。
    - **page_index 积分就是源 PDF 页索引（0-based）**：稀疏存在，不压缩。
    - 与结构补丁共用 ``store.document_lock(project_id)``，不丢并发结构编辑。
    """
    entries = list(entries)
    lock = store.document_lock(project_id)
    with lock:
        return _merge_imported_project_locked(
            store, project_id, entries, source_page_count=source_page_count, append=append,
        )


def _merge_imported_project_locked(
    store: ProjectStore, project_id: str, entries: list[dict[str, Any]],
    *, source_page_count: int = 0, append: bool = False,
) -> Document:
    project = store.project_path(project_id)
    manifest = store.load_manifest(project_id)
    document_path = project / "document" / "document.json"
    existing: Document | None = store.load_document(project_id) if (append and document_path.is_file()) else None
    resources: dict[str, dict[str, Any]] = dict(existing.resources) if existing else {}
    counter = itertools.count(1)

    def fresh_id(_index: int, _seq: int) -> str:
        return f"blk-{next(counter):08d}"

    def appended_id(index: int, seq: int) -> str:
        return f"blk-p{index:06d}-{seq:04d}"

    block_ids = appended_id if existing is not None else fresh_id
    new_blocks: list[Block] = []
    new_pages: list[Page] = []
    new_source_files: list[dict[str, Any]] = []
    seen_directories: set[str] = set()
    for entry in entries:
        directory = Path(entry["directory"])
        key = str(directory.resolve())
        if key in seen_directories:
            # 同一目录不得在一个事务里重复合并（否则会重复写 block）。
            continue
        seen_directories.add(key)
        source_pages = [int(page) for page in (entry.get("source_pages") or [])]
        blocks, pages, record = _parse_slice(
            directory, project, project_id, resources,
            source_pages=source_pages, block_ids=block_ids,
        )
        new_blocks.extend(blocks)
        new_pages.extend(pages)
        new_source_files.append(record)

    existing_covered = coverage_pages(existing) if existing is not None else set()
    coverage_known = existing is None or existing_covered is not None
    if existing is None:
        covered_filter: set[int] = set()
    elif coverage_known:
        covered_filter = set(existing_covered or set())
    else:
        # 旧资源缺少 coverage：用现有页作“防重复”证据，但绝不据此声明覆盖状态。
        covered_filter = {page.source_page_index for page in existing.pages}
    kept_blocks = [block for block in new_blocks if block.page_index not in covered_filter]
    kept_pages = [page for page in new_pages if page.source_page_index not in covered_filter]
    new_covered = {page.source_page_index for page in kept_pages}

    if existing is None:
        all_blocks = kept_blocks
        all_pages = kept_pages
        source_files = new_source_files
    else:
        # 统一到“源页索引”坐标系：旧文档可能是历史压缩索引，按页的 source_page_index 还原。
        old_index_to_source = {page.page_index: page.source_page_index for page in existing.pages}
        for page in existing.pages:
            page.page_index = page.source_page_index
        existing_blocks = list(existing.blocks)
        for block in existing_blocks:
            block.page_index = old_index_to_source.get(block.page_index, block.page_index)
        all_blocks = existing_blocks + kept_blocks
        all_pages = list(existing.pages) + kept_pages
        source_files = list(existing.source_files) + new_source_files

    # 阅读顺序：按源页排序；page_index 保持等于源页索引，供 PdfPane/裁片直接定位。
    all_pages = [page for _orig, page in sorted(enumerate(all_pages), key=lambda item: (item[1].source_page_index, item[0]))]
    for page in all_pages:
        page.page_index = page.source_page_index
    all_blocks = [block for _orig, block in sorted(enumerate(all_blocks), key=lambda item: (item[1].page_index, item[0]))]
    for order, block in enumerate(all_blocks, 1):
        block.order = order

    total_source_pages = max(0, int(source_page_count or 0))
    if total_source_pages <= 0 and existing is not None:
        old_coverage = existing.coverage if isinstance(existing.coverage, dict) else {}
        total_source_pages = max(0, int(old_coverage.get("source_page_count") or 0))
    if total_source_pages <= 0:
        manifest_total = int(manifest.get("source_page_count") or 0)
        total_source_pages = max(0, manifest_total)
    covered_union = set(existing_covered or set()) | new_covered
    if not coverage_known:
        payload = {"source_page_count": total_source_pages, "covered_pages": 0, "ranges": [], "state": "unknown"}
    else:
        payload = coverage_payload(total_source_pages, None if total_source_pages <= 0 else covered_union)

    source_language_mode = manifest.get("source_language_mode", manifest.get("source_language", "auto"))
    if existing is not None:
        source_language = existing.source_language
        source_language_mode = existing.source_language_mode
    elif source_language_mode == "auto":
        source_language = detect_language(all_blocks)
    else:
        source_language = source_language_mode

    document = Document(
        project_id=project_id, title=manifest["title"], source_language=source_language,
        source_language_mode=source_language_mode,
        target_language=manifest.get("target_language", "zh-CN"),
        page_count=total_source_pages or len(all_pages), pages=all_pages,
        bbox_space=BBOX_SPACE_MINERU_1000 if any(block.bbox for block in all_blocks) else BBOX_SPACE_NONE,
        blocks=all_blocks, resources=resources, source_files=source_files, coverage=payload,
    )
    if existing is not None:
        document.chapter_language_overrides = existing.chapter_language_overrides
        document.created_at = existing.created_at
    else:
        from .structure_signals import enrich_furniture_evidence
        document = enrich_furniture_evidence(document, project)
        document.coverage = payload
    document.toc = build_toc(document.blocks)
    store.save_document(document)
    # document 是事实来源；manifest 是其投影。先写 document 再写 manifest；source.md
    # 只是派生展示，失败不回滚 coverage。崩溃在两者之间时以 document.coverage 为准自愈。
    store.update_manifest(
        project_id, state="ready", source_language=document.source_language,
        source_language_mode=document.source_language_mode,
        detected_source_language=detect_language(document.blocks),
        page_count=len(document.pages), block_count=len(document.blocks),
        source_page_count=total_source_pages or None, parse_coverage=document.coverage,
    )
    try:
        write_source_markdown(project, document)
    except OSError:
        pass
    return document


def import_mineru(store: ProjectStore, project_id: str, sources: Iterable[str | Path]) -> Document:
    resolved = []
    for raw in sources:
        resolved.extend(discover_results(raw))
    store.import_raw_mineru(project_id, resolved)
    return normalize_imported_project(store, project_id)
