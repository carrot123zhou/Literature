from __future__ import annotations

from pathlib import Path


SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".html",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp"}
SUPPORTED_MODELS = {"vlm", "pipeline", "MinerU-HTML"}
SUPPORTED_EXTRA_FORMATS = {"docx", "html", "latex"}
SUPPORTED_LANGUAGES = {
    "ch", "ch_server", "en", "japan", "korean", "chinese_cht",
    "ta", "te", "ka", "el", "th", "latin", "arabic", "cyrillic",
    "east_slavic", "devanagari",
}
MAX_FILE_BYTES = 200_000_000
MAX_PDF_UPLOAD_BYTES = 2_000_000_000
MAX_BATCH_FILES = 50


def source_kind(path: str | Path) -> str:
    suffix = Path(path).suffix.casefold()
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".doc", ".docx"}:
        return "word"
    if suffix in {".ppt", ".pptx"}:
        return "presentation"
    if suffix in {".xls", ".xlsx"}:
        return "spreadsheet"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix == ".html":
        return "html"
    return "unknown"


def validate_source(path: str | Path) -> Path:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"文件不存在：{source}")
    if source.suffix.casefold() not in SUPPORTED_EXTENSIONS:
        formats = "、".join(sorted(value.removeprefix(".").upper() for value in SUPPORTED_EXTENSIONS))
        raise ValueError(f"MinerU 不支持 {source.suffix or '无扩展名'} 文件；可用格式：{formats}")
    size = source.stat().st_size
    if size <= 0:
        raise ValueError(f"文件为空：{source.name}")
    maximum = MAX_PDF_UPLOAD_BYTES if source.suffix.casefold() == ".pdf" else MAX_FILE_BYTES
    if size > maximum:
        if source.suffix.casefold() == ".pdf":
            raise ValueError(f"{source.name} 超过 Literature 当前 2GB Web 上传限制。")
        raise ValueError(f"{source.name} 超过 MinerU 单文件 200MB 限制。")
    return source
