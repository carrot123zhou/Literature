#!/usr/bin/env python3
"""Persistent MinerU precision-API workflow migrated from Litera.

This module intentionally does not inspect document contents with an LLM.  It
splits PDFs locally, submits deterministic MinerU jobs, persists enough state
to resume after an application restart, downloads validated result archives,
and finally hands ordered result folders to :mod:`mineru_epub`.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol


SCHEMA_VERSION = 1
DEFAULT_PAGE_LIMIT = 200
DEFAULT_MAX_SLICE_BYTES = 200_000_000
DEFAULT_MAX_RESULT_BYTES = 2_000_000_000
DEFAULT_MAX_EXTRACTED_BYTES = 4_000_000_000
DEFAULT_DOWNLOAD_ROUNDS = 2
DEFAULT_API_BASE = "https://mineru.net"
MAX_PAGES_PER_FILE = 200
MAX_BATCH_FILES = 50
KEYCHAIN_SERVICE = "local.literature.mineru"
KEYCHAIN_ACCOUNT = "precision-api-token"
SUPPORTED_MODELS = {"vlm", "pipeline"}
SUPPORTED_LANGUAGES = {
    "ch", "ch_server", "en", "japan", "korean", "chinese_cht",
    "ta", "te", "ka", "el", "th", "latin", "arabic", "cyrillic",
    "east_slavic", "devanagari",
}
DEFAULT_OUTPUT_DIRECTORY_NAME = "MinerU"
PROJECT_MANIFEST_NAME = "project.json"
PROJECT_README_NAME = "README.md"
MAX_PROJECT_NAME_BYTES = 160
CONTENT_PATTERNS = (
    "*_content_list_v2.json", "content_list_v2.json",
    "*_content_list.json", "content_list.json", "full.md",
)


@dataclass(frozen=True)
class ProjectLayout:
    root: Path
    name: str
    epub: Path
    merged: Path
    mineru: Path


def default_output_root() -> Path:
    configured = os.environ.get("LITERATURE_LIBRARY", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Documents" / "Literature"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    encoded = encoded[:max_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8").rstrip(" .-_")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def friendly_project_name(source: str | Path) -> str:
    """Build a readable, portable project name from a source filename."""
    name = unicodedata.normalize("NFC", Path(source).stem)
    # Remove common acquisition-site suffixes without touching meaningful
    # parenthesised author or edition information earlier in the title.
    site_suffix = re.compile(
        r"\s*[\[(](?=[^\])]*(?:z[- ]?library|z-lib|1lib|libgen))[^\])]*[\])]\s*$",
        re.IGNORECASE,
    )
    while site_suffix.search(name):
        name = site_suffix.sub("", name)
    name = re.sub(r"[\x00-\x1f\x7f/:\\]+", " - ", name)
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    name = _truncate_utf8(name, MAX_PROJECT_NAME_BYTES)
    return name or "未命名文档"


def reserve_project_layout(output_root: str | Path, source: str | Path) -> ProjectLayout:
    """Atomically reserve a non-overwriting per-document project directory."""
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    base = friendly_project_name(source)
    for number in range(1, 10_000):
        name = base if number == 1 else f"{base} ({number})"
        project = root / name
        try:
            project.mkdir()
        except FileExistsError:
            continue
        except OSError as error:
            raise WorkflowError(f"无法创建 MinerU 项目目录：{project}：{error}") from error
        output = project / "output"
        return ProjectLayout(
            root=project,
            name=name,
            epub=output / f"{name}.epub",
            merged=output / "content",
            mineru=project / "mineru",
        )
    raise WorkflowError(f"无法为 {base} 分配唯一的项目目录。")


def epub_language_for_mineru(language: str) -> str:
    """Translate MinerU OCR-pack names into valid EPUB language tags."""
    return {
        "ch": "zh-CN",
        "ch_server": "zh-Hant",
        "chinese_cht": "zh-Hant",
        "en": "en",
        "japan": "ja",
        "korean": "ko",
        "ta": "ta",
        "te": "te",
        # MinerU calls its Kannada pack ``ka``; BCP 47 uses ``kn``.
        "ka": "kn",
        "el": "el",
        "th": "th",
        # Family packs do not identify one document language.
        "latin": "und",
        "arabic": "und",
        "cyrillic": "und",
        "east_slavic": "und",
        "devanagari": "und",
    }.get(language, "und")


class WorkflowError(RuntimeError):
    """The local workflow cannot safely continue."""


class MinerUAPIError(WorkflowError):
    """A structured MinerU API or transport failure."""

    def __init__(self, message: str, *, code: str = "", status_code: int = 0, retriable: bool = False) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status_code = status_code
        self.retriable = retriable


class JobState(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    UPLOADING = "uploading"
    PROCESSING = "processing"
    DOWNLOADING = "downloading"
    MERGING = "merging"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING = "waiting"


class SliceState(str, Enum):
    PREPARED = "prepared"
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    REMOTE_DONE = "remote_done"
    DOWNLOADED = "downloaded"
    FAILED = "failed"


@dataclass
class SliceJob:
    index: int
    start_page: int
    end_page: int
    data_id: str
    upload_name: str
    local_pdf: str
    state: SliceState = SliceState.PREPARED
    batch_id: str = ""
    remote_state: str = ""
    result_url: str = ""
    result_zip: str = ""
    result_directory: str = ""
    error: str = ""
    attempts: int = 0

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SliceJob":
        clean = dict(value)
        clean["state"] = SliceState(clean.get("state", SliceState.PREPARED.value))
        return cls(**clean)


@dataclass
class WorkflowJob:
    job_id: str
    source_pdf: str
    output_epub: str
    workspace: str
    raw_output: str
    source_size: int
    source_sha256: str
    project_root: str = ""
    project_name: str = ""
    merged_output: str = ""
    page_count: int = 0
    state: JobState = JobState.QUEUED
    model_version: str = "vlm"
    language: str = "ch"
    is_ocr: bool = False
    enable_table: bool = True
    enable_formula: bool = True
    page_limit: int = DEFAULT_PAGE_LIMIT
    max_slice_bytes: int = DEFAULT_MAX_SLICE_BYTES
    keep_original_pdf: bool = True
    slices: list[SliceJob] = field(default_factory=list)
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def job_file(self) -> Path:
        return Path(self.workspace) / "job.json"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["schema_version"] = SCHEMA_VERSION
        value["state"] = self.state.value
        value["slices"] = [item.to_dict() for item in self.slices]
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkflowJob":
        if value.get("schema_version") != SCHEMA_VERSION:
            raise WorkflowError(f"不支持的 MinerU 任务格式：{value.get('schema_version')!r}")
        clean = {key: item for key, item in value.items() if key != "schema_version"}
        clean["state"] = JobState(clean.get("state", JobState.QUEUED.value))
        clean["slices"] = [SliceJob.from_dict(item) for item in clean.get("slices", [])]
        return cls(**clean)


@dataclass(frozen=True)
class BatchSubmission:
    batch_id: str
    upload_urls: tuple[str, ...]


class HTTPResponse(Protocol):
    status_code: int

    def json(self) -> Any: ...
    def raise_for_status(self) -> None: ...
    def iter_bytes(self, chunk_size: int = ...) -> Iterable[bytes]: ...


ProgressCallback = Callable[[WorkflowJob, str], None]
MergeCallback = Callable[[WorkflowJob, list[str]], Any]
SleepCallback = Callable[[float], None]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _project_relative(path: str | Path, root: Path) -> str:
    candidate = Path(path).expanduser().resolve()
    if candidate != root and root not in candidate.parents:
        raise WorkflowError(f"项目资源路径超出项目目录：{candidate}")
    return candidate.relative_to(root).as_posix() or "."


def write_project_documents(job: WorkflowJob) -> None:
    """Keep human- and machine-readable project documents in sync."""
    if not job.project_root:
        return
    root = Path(job.project_root).expanduser().resolve()
    name = job.project_name or root.name
    paths = {
        "epub": _project_relative(job.output_epub, root),
        "structured_content": _project_relative(job.merged_output, root),
        "mineru_results": _project_relative(job.raw_output, root),
    }
    completed_slices = sum(item.state == SliceState.DOWNLOADED for item in job.slices)
    manifest = {
        "schema_version": 1,
        "project_name": name,
        "state": job.state.value,
        "source": {
            "filename": Path(job.source_pdf).name,
            "path": job.source_pdf,
            "size_bytes": job.source_size,
            "sha256": job.source_sha256,
        },
        "resources": paths,
        "progress": {
            "pages": job.page_count,
            "slices_total": len(job.slices),
            "slices_downloaded": completed_slices,
        },
        "options": {
            "model": job.model_version,
            "language": job.language,
            "ocr": job.is_ocr,
            "table": job.enable_table,
            "formula": job.enable_formula,
            "pages_per_slice": job.page_limit,
            "keep_original_pdf": job.keep_original_pdf,
        },
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "job_id": job.job_id,
    }
    _atomic_json(root / PROJECT_MANIFEST_NAME, manifest)
    state_labels = {
        JobState.QUEUED: "排队中", JobState.PREPARING: "正在拆分",
        JobState.UPLOADING: "正在上传", JobState.PROCESSING: "MinerU 解析中",
        JobState.DOWNLOADING: "正在下载", JobState.MERGING: "正在生成成品",
        JobState.COMPLETED: "已完成", JobState.FAILED: "失败",
        JobState.WAITING: "等待继续",
    }
    error_line = f"\n- 最近错误：{job.error}" if job.error else ""
    readme = f"""# {name}

这是 Litera 为本 PDF 建立的独立 MinerU 项目目录。此文件会随任务状态自动更新。

## 项目状态

- 状态：{state_labels.get(job.state, job.state.value)}
- 原始文件：{Path(job.source_pdf).name}
- 页数：{job.page_count or '尚未读取'}
- 切片：{completed_slices}/{len(job.slices)} 已下载
- 创建时间：{job.created_at}
- 更新时间：{job.updated_at}{error_line}

## 资源位置

- EPUB 成品：`{paths['epub']}`
- Markdown、JSON、图片与可选原 PDF：`{paths['structured_content']}/`
- MinerU 原始解析切片：`{paths['mineru_results']}/`
- 机器可读项目清单：`{PROJECT_MANIFEST_NAME}`

## 解析设置

- 模型：`{job.model_version}`
- 识别语言：`{job.language}`
- OCR：{'开启' if job.is_ocr else '关闭'}
- 表格识别：{'开启' if job.enable_table else '关闭'}
- 公式识别：{'开启' if job.enable_formula else '关闭'}
- 每片最多：{job.page_limit} 页
- 保留原始 PDF：{'是' if job.keep_original_pdf else '否'}

Token 只保存在 macOS Keychain，不会写入本目录。
"""
    _atomic_text(root / PROJECT_README_NAME, readme)


def plan_page_ranges(page_count: int, page_limit: int = DEFAULT_PAGE_LIMIT) -> list[tuple[int, int]]:
    """Return contiguous one-based, inclusive ranges without overlap."""
    if page_count < 1:
        raise WorkflowError("PDF 没有可解析页面。")
    if page_limit < 1:
        raise WorkflowError("每片页数必须大于 0。")
    return [(start, min(page_count, start + page_limit - 1)) for start in range(1, page_count + 1, page_limit)]


def _open_pdf_page_count(path: Path) -> int:
    try:
        import fitz
    except ImportError as error:
        raise WorkflowError("自动分片需要 PyMuPDF；请从 Litera 启动器重新安装 MinerU 工具。") from error
    try:
        document = fitz.open(path)
    except Exception as error:
        raise WorkflowError(f"无法打开 PDF：{error}") from error
    try:
        return int(document.page_count)
    finally:
        document.close()


def _write_pdf_range(source: Path, output: Path, start_page: int, end_page: int) -> None:
    try:
        import fitz
    except ImportError as error:
        raise WorkflowError("自动分片需要 PyMuPDF；请从 Litera 启动器重新安装 MinerU 工具。") from error
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source_document = fitz.open(source)
    target = fitz.open()
    try:
        target.insert_pdf(source_document, from_page=start_page - 1, to_page=end_page - 1)
        target.save(temporary, garbage=4, deflate=True)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        target.close()
        source_document.close()
    temporary.replace(output)


def split_pdf(
    source: str | Path,
    destination: str | Path,
    *,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_bytes: int = DEFAULT_MAX_SLICE_BYTES,
    page_count: int | None = None,
    writer: Callable[[Path, Path, int, int], None] = _write_pdf_range,
) -> list[tuple[int, int, Path]]:
    """Split a PDF by page count and recursively bisect oversized slices."""
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    destination_path.mkdir(parents=True, exist_ok=True)
    total_pages = page_count if page_count is not None else _open_pdf_page_count(source_path)
    pending = plan_page_ranges(total_pages, page_limit)
    completed: list[tuple[int, int, Path]] = []
    while pending:
        start_page, end_page = pending.pop(0)
        draft = destination_path / f"p{start_page:06d}-{end_page:06d}.pdf"
        writer(source_path, draft, start_page, end_page)
        if draft.stat().st_size <= max_bytes:
            completed.append((start_page, end_page, draft))
            continue
        draft.unlink(missing_ok=True)
        if start_page == end_page:
            raise WorkflowError(f"第 {start_page} 页单页文件已超过 MinerU 的 {max_bytes / 1_000_000:.0f}MB 限制。")
        midpoint = (start_page + end_page) // 2
        pending[0:0] = [(start_page, midpoint), (midpoint + 1, end_page)]
    completed.sort(key=lambda item: item[0])
    renamed: list[tuple[int, int, Path]] = []
    for index, (start_page, end_page, path) in enumerate(completed, 1):
        target = destination_path / f"{index:03d}_p{start_page:06d}-{end_page:06d}.pdf"
        if path != target:
            path.replace(target)
        renamed.append((start_page, end_page, target))
    return renamed


def _api_message(code: str, message: str) -> str:
    known = {
        "A0202": "MinerU Token 无效，请重新保存 Token。",
        "A0211": "MinerU Token 已过期，请更换 Token。",
        "-60005": "切片超过 MinerU 200MB 限制。",
        "-60006": "切片页数超过当前 MinerU 限制，请降低每片页数。",
        "-60009": "MinerU 任务队列已满，请稍后重试。",
        "-60012": "MinerU 已找不到该远端任务，需要重新提交对应切片。",
        "-60018": "今日 MinerU 解析任务数量已达上限。",
    }
    return known.get(str(code), message or f"MinerU API 返回错误 {code}")


class MinerUClient:
    """Small precision-API client with bounded retry and streaming downloads."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_API_BASE,
        http_client: Any | None = None,
        max_attempts: int = 5,
        sleep: SleepCallback = time.sleep,
    ) -> None:
        token = token.strip()
        if not token:
            raise MinerUAPIError("尚未配置 MinerU 精准解析 Token。", code="missing_token")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.max_attempts = max(1, max_attempts)
        self.sleep = sleep
        direct_download_client = None
        if http_client is None:
            try:
                import httpx
            except ImportError as error:
                raise MinerUAPIError(
                    "缺少 httpx；请从 Litera 启动器重新安装 MinerU 工具。",
                    code="missing_dependency",
                ) from error
            http_client = httpx.Client(
                timeout=httpx.Timeout(90.0, connect=20.0),
                follow_redirects=True,
                headers={"User-Agent": "Litera-MinerU/1.2"},
            )
            # API requests may legitimately need the user's configured proxy,
            # while MinerU result URLs point at a separate object-storage CDN.
            # Some macOS proxy/VPN combinations terminate the CDN TLS tunnel
            # during CONNECT. Prefer an isolated direct client for downloads,
            # then alternate with the normal client as a compatibility fallback.
            direct_download_client = httpx.Client(
                timeout=httpx.Timeout(90.0, connect=20.0),
                follow_redirects=True,
                trust_env=False,
                headers={"User-Agent": "Litera-MinerU/1.2"},
            )
        self.http = http_client
        self.download_http_clients = tuple(
            client for client in (direct_download_client, http_client) if client is not None
        )

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def close(self) -> None:
        closed: set[int] = set()
        for client in (self.http, *self.download_http_clients):
            if id(client) in closed:
                continue
            closed.add(id(client))
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _delay(self, attempt: int) -> None:
        self.sleep(min(16.0, 0.5 * (2 ** (attempt - 1))) + random.uniform(0, 0.15))

    def _request(self, method: str, url: str, *, rewind: Callable[[], None] | None = None, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                if rewind:
                    rewind()
                response = self.http.request(method, url, **kwargs)
            except Exception as error:
                last_error = error
                if attempt < self.max_attempts:
                    self._delay(attempt)
                    continue
                raise MinerUAPIError(f"连接 MinerU 失败：{error}", retriable=True) from error
            status = int(getattr(response, "status_code", 0))
            if status in (401, 403):
                raise MinerUAPIError("MinerU Token 无效或无权访问该任务。", code="auth", status_code=status)
            if status == 429 or status >= 500:
                if attempt < self.max_attempts:
                    self._delay(attempt)
                    continue
                raise MinerUAPIError(f"MinerU 暂时不可用（HTTP {status}）。", status_code=status, retriable=True)
            if status < 200 or status >= 300:
                raise MinerUAPIError(f"MinerU 请求失败（HTTP {status}）。", status_code=status)
            return response
        raise MinerUAPIError(f"连接 MinerU 失败：{last_error}", retriable=True)

    def _json(self, response: Any) -> dict[str, Any]:
        try:
            payload = response.json()
        except Exception as error:
            raise MinerUAPIError("MinerU 返回了无效 JSON。") from error
        if not isinstance(payload, dict):
            raise MinerUAPIError("MinerU 返回的数据结构无效。")
        code = str(payload.get("code", ""))
        if code not in ("0", "", "None"):
            message = _api_message(code, str(payload.get("msg", "")))
            retriable = code in {"-10001", "-60001", "-60007", "-60008", "-60009", "-60010", "-60022"}
            raise MinerUAPIError(message, code=code, retriable=retriable)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MinerUAPIError("MinerU 响应缺少 data。")
        return data

    def create_batch(self, slices: list[SliceJob], job: WorkflowJob) -> BatchSubmission:
        if not slices or len(slices) > MAX_BATCH_FILES:
            raise WorkflowError(f"单批 MinerU 文件数必须为 1–{MAX_BATCH_FILES}。")
        files = [
            {
                "name": item.upload_name,
                "data_id": item.data_id,
                "is_ocr": job.is_ocr,
            }
            for item in slices
        ]
        response = self._request(
            "POST",
            f"{self.base_url}/api/v4/file-urls/batch",
            headers={**self.auth_headers, "Content-Type": "application/json"},
            json={
                "files": files,
                "model_version": job.model_version,
                "language": job.language,
                "enable_table": job.enable_table,
                "enable_formula": job.enable_formula,
            },
        )
        data = self._json(response)
        urls = data.get("file_urls")
        batch_id = str(data.get("batch_id", ""))
        if not batch_id or not isinstance(urls, list) or len(urls) != len(slices):
            raise MinerUAPIError("MinerU 没有返回完整的批次上传地址。")
        return BatchSubmission(batch_id, tuple(str(item) for item in urls))

    def upload_file(self, upload_url: str, path: str | Path) -> None:
        source = Path(path)
        with source.open("rb") as stream:
            self._request(
                "PUT", upload_url, content=stream,
                headers={"Content-Length": str(source.stat().st_size)},
                rewind=lambda: stream.seek(0),
            )

    def get_batch(self, batch_id: str) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"{self.base_url}/api/v4/extract-results/batch/{batch_id}",
            headers=self.auth_headers,
        )
        data = self._json(response)
        results = data.get("extract_result", [])
        if not isinstance(results, list):
            raise MinerUAPIError("MinerU 批次结果格式无效。")
        return [item for item in results if isinstance(item, dict)]

    def check_token(self) -> None:
        """Use a side-effect-free lookup; a not-found batch still proves auth."""
        try:
            response = self._request(
                "GET",
                f"{self.base_url}/api/v4/extract-results/batch/litera-token-check",
                headers=self.auth_headers,
            )
            payload = response.json()
        except MinerUAPIError as error:
            if error.status_code == 404:
                return
            raise
        except Exception as error:
            raise MinerUAPIError(f"无法验证 MinerU Token：{error}") from error
        if isinstance(payload, dict) and str(payload.get("code")) in {"A0202", "A0211"}:
            code = str(payload["code"])
            raise MinerUAPIError(_api_message(code, str(payload.get("msg", ""))), code=code)

    def download_result(self, url: str, target: str | Path, *, max_bytes: int = DEFAULT_MAX_RESULT_BYTES) -> Path:
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep a partial file across transport retries (and even an app
        # restart). MinerU's object-storage CDN advertises byte ranges, so a
        # short-lived TLS EOF should resume instead of throwing away tens or
        # hundreds of megabytes and starting again from byte zero.
        temporary = target_path.with_suffix(target_path.suffix + ".part")
        target_path.with_suffix(target_path.suffix + ".tmp").unlink(missing_ok=True)
        if temporary.is_file() and temporary.stat().st_size > max_bytes:
            temporary.unlink(missing_ok=True)

        def validated_download() -> Path:
            try:
                with zipfile.ZipFile(temporary) as bundle:
                    if not bundle.infolist():
                        raise zipfile.BadZipFile("empty archive")
                    corrupt = bundle.testzip()
                    if corrupt:
                        raise zipfile.BadZipFile(f"corrupt member: {corrupt}")
            except (OSError, zipfile.BadZipFile) as error:
                raise MinerUAPIError(
                    f"MinerU ZIP 下载不完整或已损坏：{error}",
                    code="incomplete_download", retriable=True,
                ) from error
            temporary.replace(target_path)
            return target_path

        if temporary.is_file() and temporary.stat().st_size:
            try:
                return validated_download()
            except MinerUAPIError:
                # Expected for a genuinely partial archive; resume below.
                pass

        stream_methods = [
            method for method in (
                getattr(client, "stream", None) for client in self.download_http_clients
            )
            if callable(method)
        ]
        if stream_methods:
            last_error: Exception | None = None
            for attempt in range(1, self.max_attempts + 1):
                try:
                    stream_method = stream_methods[(attempt - 1) % len(stream_methods)]
                    offset = temporary.stat().st_size if temporary.is_file() else 0
                    headers = {"Accept-Encoding": "identity", "Connection": "close"}
                    if offset:
                        headers["Range"] = f"bytes={offset}-"
                    with stream_method("GET", url, headers=headers) as response:
                        status = int(getattr(response, "status_code", 0))
                        if status in (401, 403):
                            raise MinerUAPIError(
                                "MinerU 结果下载地址已过期或无权访问；重试时会刷新结果地址。",
                                code="download_forbidden", status_code=status,
                            )
                        if status == 416 and offset:
                            # The partial file may actually be complete (for
                            # example, the app stopped after the last network
                            # read but before the atomic rename). If it is not
                            # a valid ZIP, discard it and restart cleanly.
                            try:
                                return validated_download()
                            except MinerUAPIError:
                                temporary.unlink(missing_ok=True)
                                raise MinerUAPIError(
                                    "MinerU CDN 拒绝了断点位置，正在从头重试。",
                                    code="invalid_range", retriable=True,
                                )
                        if status == 429 or status >= 500:
                            raise MinerUAPIError(
                                f"MinerU 下载暂时不可用（HTTP {status}）。",
                                status_code=status, retriable=True,
                            )
                        if status < 200 or status >= 300:
                            raise MinerUAPIError(f"MinerU 下载失败（HTTP {status}）。", status_code=status)
                        response_headers = getattr(response, "headers", {}) or {}
                        if status == 206:
                            content_range = str(response_headers.get("Content-Range", ""))
                            match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
                            if not match or int(match.group(1)) != offset:
                                temporary.unlink(missing_ok=True)
                                raise MinerUAPIError(
                                    "MinerU CDN 返回了无效的断点续传范围。",
                                    code="invalid_range", retriable=True,
                                )
                            expected_total = None if match.group(3) == "*" else int(match.group(3))
                            mode = "ab"
                            written = offset
                        else:
                            # Some CDNs ignore Range and answer 200 with the
                            # complete object. Restart this attempt safely.
                            expected_header = str(response_headers.get("Content-Length", ""))
                            expected_total = int(expected_header) if expected_header.isdigit() else None
                            mode = "wb"
                            written = 0
                        with temporary.open(mode) as output:
                            for block in response.iter_bytes(chunk_size=1024 * 1024):
                                written += len(block)
                                if written > max_bytes:
                                    raise MinerUAPIError(
                                        "MinerU ZIP 超过 Litera 的 2GB 安全上限。",
                                        code="size_limit",
                                    )
                                if block:
                                    output.write(block)
                    if written == 0:
                        raise MinerUAPIError("MinerU 下载结果为空。")
                    if expected_total is not None and written != expected_total:
                        raise MinerUAPIError(
                            f"MinerU ZIP 下载未完成（{written}/{expected_total} 字节）。",
                            code="incomplete_download", retriable=True,
                        )
                    return validated_download()
                except MinerUAPIError as error:
                    last_error = error
                    if error.code == "size_limit":
                        temporary.unlink(missing_ok=True)
                    if not error.retriable or attempt >= self.max_attempts:
                        raise
                    self._delay(attempt)
                except Exception as error:
                    last_error = error
                    if attempt >= self.max_attempts:
                        raise MinerUAPIError(f"下载 MinerU ZIP 失败：{error}", retriable=True) from error
                    self._delay(attempt)
            raise MinerUAPIError(f"下载 MinerU ZIP 失败：{last_error}", retriable=True)

        response = self._request("GET", url, headers={})
        try:
            written = 0
            with temporary.open("wb") as stream:
                iterator = getattr(response, "iter_bytes", None)
                if callable(iterator):
                    for block in iterator(chunk_size=1024 * 1024):
                        written += len(block)
                        if written > max_bytes:
                            raise MinerUAPIError(
                                "MinerU ZIP 超过 Litera 的 2GB 安全上限。",
                                code="size_limit",
                            )
                        if block:
                            stream.write(block)
                else:
                    content = bytes(response.content)
                    written = len(content)
                    if written > max_bytes:
                        raise MinerUAPIError(
                            "MinerU ZIP 超过 Litera 的 2GB 安全上限。",
                            code="size_limit",
                        )
                    stream.write(content)
            if written == 0:
                raise MinerUAPIError("MinerU 下载结果为空。")
            return validated_download()
        except Exception as error:
            if isinstance(error, MinerUAPIError) and error.code == "size_limit":
                temporary.unlink(missing_ok=True)
            raise
        return target_path


def _read_mineru_token_from_keychain() -> str:
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def get_mineru_token() -> str:
    override = os.environ.get("MINERU_API_TOKEN", "").strip()
    return override or _read_mineru_token_from_keychain()


def save_mineru_token(token: str) -> None:
    token = token.strip()
    if not token:
        raise WorkflowError("Token 不能为空。")
    try:
        result = subprocess.run(
            ["/usr/bin/security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w", token],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkflowError("无法访问 macOS Keychain。") from error
    if result.returncode:
        raise WorkflowError(f"无法将 MinerU Token 保存到 macOS Keychain（状态 {result.returncode}）。")
    if _read_mineru_token_from_keychain() != token:
        raise WorkflowError("Token 已提交给 macOS Keychain，但读取验证失败；请检查钥匙串访问权限。")


def delete_mineru_token() -> None:
    try:
        result = subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkflowError("无法访问 macOS Keychain。") from error
    if result.returncode not in (0, 44):
        raise WorkflowError("无法从 macOS Keychain 删除 MinerU Token。")


def _is_result_directory(path: Path) -> bool:
    return any(any(path.glob(pattern)) for pattern in CONTENT_PATTERNS)


def safe_extract_result(archive: str | Path, destination: str | Path, *, max_extracted_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES) -> Path:
    archive_path = Path(archive)
    destination_path = Path(destination).resolve()
    temporary = destination_path.with_name(destination_path.name + ".extracting")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_path) as bundle:
            expanded_size = 0
            for member in bundle.infolist():
                target = (temporary / member.filename).resolve()
                if target != temporary and temporary not in target.parents:
                    raise WorkflowError("MinerU ZIP 包含不安全的路径。")
                expanded_size += max(0, member.file_size)
                if expanded_size > max_extracted_bytes:
                    raise WorkflowError("MinerU ZIP 解压后超过 Litera 的 4GB 安全上限。")
            bundle.extractall(temporary)
        candidates = [temporary] if _is_result_directory(temporary) else []
        candidates.extend(path for path in temporary.rglob("*") if path.is_dir() and _is_result_directory(path))
        unique = sorted(set(candidates), key=lambda item: (len(item.parts), str(item)))
        if not unique:
            raise WorkflowError("MinerU ZIP 缺少 content_list JSON 或 full.md。")
        selected = unique[0]
        if destination_path.exists():
            shutil.rmtree(destination_path)
        if selected == temporary:
            temporary.replace(destination_path)
            return destination_path
        destination_path.mkdir(parents=True)
        for child in selected.iterdir():
            shutil.move(str(child), destination_path / child.name)
        shutil.rmtree(temporary)
        return destination_path
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise


class MinerUWorkflow:
    """Create, run, resume, retry, and clean persistent MinerU jobs."""

    def __init__(
        self,
        jobs_root: str | Path,
        *,
        client_factory: Callable[[str], MinerUClient] = MinerUClient,
        merge_callback: MergeCallback | None = None,
        sleep: SleepCallback = time.sleep,
        poll_interval: float = 5.0,
    ) -> None:
        self.jobs_root = Path(jobs_root).expanduser().resolve()
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.client_factory = client_factory
        self.merge_callback = merge_callback or self._default_merge
        self.sleep = sleep
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()

    def _notify(self, job: WorkflowJob, progress: ProgressCallback | None, message: str) -> None:
        job.updated_at = _utcnow()
        self.save_job(job)
        if progress:
            progress(job, message)

    def save_job(self, job: WorkflowJob) -> None:
        job.updated_at = _utcnow()
        _atomic_json(job.job_file, job.to_dict())
        write_project_documents(job)

    def load_job(self, path: str | Path) -> WorkflowJob:
        candidate = Path(path)
        if candidate.is_dir():
            candidate = candidate / "job.json"
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WorkflowError(f"无法读取 MinerU 任务：{error}") from error
        if not isinstance(value, dict):
            raise WorkflowError("MinerU job.json 格式无效。")
        return WorkflowJob.from_dict(value)

    def list_jobs(self) -> list[WorkflowJob]:
        jobs: list[WorkflowJob] = []
        for path in self.jobs_root.glob("*/job.json"):
            try:
                jobs.append(self.load_job(path))
            except WorkflowError:
                continue
        return sorted(jobs, key=lambda item: item.created_at)

    def create_job(
        self,
        source_pdf: str | Path,
        output_epub: str | Path,
        *,
        model_version: str = "vlm",
        language: str = "ch",
        is_ocr: bool = False,
        enable_table: bool = True,
        enable_formula: bool = True,
        page_limit: int = DEFAULT_PAGE_LIMIT,
        max_slice_bytes: int = DEFAULT_MAX_SLICE_BYTES,
        keep_original_pdf: bool = True,
        project_layout: ProjectLayout | None = None,
    ) -> WorkflowJob:
        source = Path(source_pdf).expanduser().resolve()
        if not source.is_file() or source.suffix.casefold() != ".pdf":
            raise WorkflowError(f"请选择有效 PDF：{source}")
        if model_version not in SUPPORTED_MODELS:
            raise WorkflowError(f"不支持的 MinerU 模型：{model_version}")
        language = language.strip() or "ch"
        if language not in SUPPORTED_LANGUAGES:
            raise WorkflowError(f"不支持的 MinerU 识别语言：{language}")
        if page_limit < 1 or page_limit > MAX_PAGES_PER_FILE:
            raise WorkflowError(f"每片页数必须在 1–{MAX_PAGES_PER_FILE} 之间。")
        output = Path(output_epub).expanduser().resolve()
        if output.suffix.casefold() != ".epub":
            output = output.with_suffix(".epub")
        job_id = uuid.uuid4().hex
        workspace = self.jobs_root / job_id
        raw_output = project_layout.mineru if project_layout else output.parent / f"{output.stem}_mineru_job"
        job = WorkflowJob(
            job_id=job_id,
            source_pdf=str(source),
            output_epub=str(output),
            workspace=str(workspace),
            raw_output=str(raw_output),
            source_size=source.stat().st_size,
            source_sha256=_sha256(source),
            project_root=str(project_layout.root) if project_layout else "",
            project_name=project_layout.name if project_layout else "",
            merged_output=str(project_layout.merged) if project_layout else "",
            model_version=model_version,
            language=language,
            is_ocr=is_ocr,
            enable_table=enable_table,
            enable_formula=enable_formula,
            page_limit=page_limit,
            max_slice_bytes=max_slice_bytes,
            keep_original_pdf=keep_original_pdf,
        )
        self.save_job(job)
        return job

    def create_project_job(
        self,
        source_pdf: str | Path,
        output_root: str | Path,
        **options: Any,
    ) -> WorkflowJob:
        source = Path(source_pdf).expanduser().resolve()
        if not source.is_file() or source.suffix.casefold() != ".pdf":
            raise WorkflowError(f"请选择有效 PDF：{source}")
        layout = reserve_project_layout(output_root, source)
        try:
            return self.create_job(
                source,
                layout.epub,
                project_layout=layout,
                **options,
            )
        except Exception:
            # Reservation created exactly this empty directory.  Remove it only
            # when job creation failed before any project document was written.
            try:
                layout.root.rmdir()
            except OSError:
                pass
            raise

    def ensure_project_for_unstarted_job(
        self,
        job: WorkflowJob,
        output_root: str | Path,
    ) -> WorkflowJob:
        if job.project_root:
            return job
        if job.slices or job.state not in {JobState.QUEUED, JobState.FAILED}:
            raise WorkflowError("任务已经开始，不能再迁移到新的项目目录。")
        layout = reserve_project_layout(output_root, job.source_pdf)
        job.project_root = str(layout.root)
        job.project_name = layout.name
        job.output_epub = str(layout.epub)
        job.merged_output = str(layout.merged)
        job.raw_output = str(layout.mineru)
        self.save_job(job)
        return job

    def _verify_source(self, job: WorkflowJob) -> Path:
        source = Path(job.source_pdf)
        if not source.is_file():
            raise WorkflowError(f"原始 PDF 已移动或删除：{source}")
        if source.stat().st_size != job.source_size or _sha256(source) != job.source_sha256:
            raise WorkflowError("原始 PDF 自创建任务后已发生变化，请新建任务。")
        return source

    def _prepare(self, job: WorkflowJob, progress: ProgressCallback | None) -> None:
        if job.slices:
            return
        if job.page_limit < 1 or job.page_limit > MAX_PAGES_PER_FILE:
            raise WorkflowError(
                f"任务的分片上限为 {job.page_limit} 页，但 MinerU 当前每文件最多支持 "
                f"{MAX_PAGES_PER_FILE} 页；请新建任务。"
            )
        job.state = JobState.PREPARING
        job.error = ""
        self._notify(job, progress, "正在检查并拆分 PDF")
        source = self._verify_source(job)
        page_count = _open_pdf_page_count(source)
        split_dir = Path(job.workspace) / "split"
        pieces = split_pdf(
            source, split_dir, page_limit=job.page_limit,
            max_bytes=job.max_slice_bytes, page_count=page_count,
        )
        job.page_count = page_count
        job.slices = [
            SliceJob(
                index=index,
                start_page=start,
                end_page=end,
                data_id=f"{job.job_id}-{index:03d}",
                upload_name=path.name,
                local_pdf=str(path),
            )
            for index, (start, end, path) in enumerate(pieces, 1)
        ]
        self._notify(job, progress, f"已生成 {len(job.slices)} 个无重叠切片")

    def _upload(self, job: WorkflowJob, client: MinerUClient, progress: ProgressCallback | None) -> bool:
        pending = [item for item in job.slices if item.state == SliceState.PREPARED]
        if not pending:
            return True
        job.state = JobState.UPLOADING
        self._notify(job, progress, f"准备上传 {len(pending)} 个切片")
        failures: list[SliceJob] = []
        for offset in range(0, len(pending), MAX_BATCH_FILES):
            group = pending[offset:offset + MAX_BATCH_FILES]
            submission = client.create_batch(group, job)
            for item in group:
                item.batch_id = submission.batch_id
            self.save_job(job)
            for item, upload_url in zip(group, submission.upload_urls):
                if self._stop_event.is_set():
                    for untouched in group:
                        if untouched.state == SliceState.PREPARED:
                            untouched.batch_id = ""
                    job.state = JobState.WAITING
                    self._notify(job, progress, "已在安全边界停止上传；可稍后继续")
                    return False
                try:
                    item.attempts += 1
                    client.upload_file(upload_url, item.local_pdf)
                    item.state = SliceState.UPLOADED
                    item.error = ""
                    self._notify(job, progress, f"已上传切片 {item.index}/{len(job.slices)}（{item.start_page}–{item.end_page} 页）")
                except Exception as error:
                    item.state = SliceState.FAILED
                    item.error = str(error)
                    # A future retry obtains a fresh signed URL and batch.
                    item.batch_id = ""
                    failures.append(item)
                    self.save_job(job)
        if failures:
            raise WorkflowError(f"{len(failures)} 个切片上传失败；可使用“重试失败”继续。")
        return True

    @staticmethod
    def _match_results(slices: list[SliceJob], results: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        by_data = {str(item.get("data_id")): item for item in results if item.get("data_id")}
        by_name = {str(item.get("file_name")): item for item in results if item.get("file_name")}
        return {
            item.index: by_data.get(item.data_id) or by_name.get(item.upload_name)
            for item in slices
            if by_data.get(item.data_id) or by_name.get(item.upload_name)
        }

    def _poll(self, job: WorkflowJob, client: MinerUClient, progress: ProgressCallback | None) -> bool:
        job.state = JobState.PROCESSING
        self._notify(job, progress, "MinerU 正在解析切片")
        while True:
            if self._stop_event.is_set():
                job.state = JobState.WAITING
                self._notify(job, progress, "已停止本地等待；远端任务仍会继续，可稍后恢复")
                return False
            active = [item for item in job.slices if item.state in {SliceState.UPLOADED, SliceState.PROCESSING}]
            for batch_id in sorted({item.batch_id for item in active if item.batch_id}):
                members = [item for item in active if item.batch_id == batch_id]
                try:
                    results = client.get_batch(batch_id)
                except MinerUAPIError as error:
                    if error.code in {"-60012", "-60013"}:
                        for item in members:
                            item.state = SliceState.FAILED
                            item.batch_id = ""
                            item.error = str(error)
                        self.save_job(job)
                    raise
                matched = self._match_results(members, results)
                for item in members:
                    result = matched.get(item.index)
                    if not result:
                        continue
                    remote_state = str(result.get("state", ""))
                    item.remote_state = remote_state
                    if remote_state == "done":
                        result_url = str(result.get("full_zip_url", ""))
                        if not result_url:
                            item.state = SliceState.FAILED
                            item.error = "MinerU 返回完成状态但没有 ZIP 地址。"
                        else:
                            item.state = SliceState.REMOTE_DONE
                            item.result_url = result_url
                            item.error = ""
                    elif remote_state == "failed":
                        item.state = SliceState.FAILED
                        item.error = str(result.get("err_msg") or "MinerU 解析失败")
                    else:
                        item.state = SliceState.PROCESSING
                self.save_job(job)
            failed = [item for item in job.slices if item.state == SliceState.FAILED]
            if failed:
                raise WorkflowError(f"{len(failed)} 个切片解析失败；可使用“重试失败”继续。")
            remaining = [item for item in job.slices if item.state in {SliceState.UPLOADED, SliceState.PROCESSING}]
            done = len([item for item in job.slices if item.state in {SliceState.REMOTE_DONE, SliceState.DOWNLOADED}])
            self._notify(job, progress, f"MinerU 已完成 {done}/{len(job.slices)} 个切片")
            if not remaining:
                return True
            self.sleep(self.poll_interval)

    def _download(self, job: WorkflowJob, client: MinerUClient, progress: ProgressCallback | None) -> bool:
        pending = [item for item in job.slices if item.state == SliceState.REMOTE_DONE]
        if not pending:
            return True
        job.state = JobState.DOWNLOADING
        self._notify(job, progress, f"正在下载 {len(pending)} 个 MinerU 结果")
        slices_root = Path(job.raw_output) / "slices"
        downloads = Path(job.workspace) / "downloads"
        for item in pending:
            if self._stop_event.is_set():
                job.state = JobState.WAITING
                self._notify(job, progress, "已在安全边界停止下载；可稍后继续")
                return False
            archive = downloads / f"{item.index:03d}.zip"
            result_dir = slices_root / f"{item.index:03d}_p{item.start_page:06d}-{item.end_page:06d}"
            try:
                for download_round in range(1, DEFAULT_DOWNLOAD_ROUNDS + 1):
                    try:
                        if not archive.is_file():
                            client.download_result(item.result_url, archive)
                        break
                    except MinerUAPIError as error:
                        recoverable = error.retriable or error.code == "download_forbidden"
                        if not recoverable or download_round >= DEFAULT_DOWNLOAD_ROUNDS or not item.batch_id:
                            raise
                        results = client.get_batch(item.batch_id)
                        refreshed = self._match_results([item], results).get(item.index)
                        refreshed_url = str((refreshed or {}).get("full_zip_url", ""))
                        if str((refreshed or {}).get("state", "")) != "done" or not refreshed_url:
                            raise
                        item.result_url = refreshed_url
                        item.error = ""
                        self._notify(
                            job, progress,
                            f"切片 {item.index} 下载中断，已保留进度并刷新地址自动续传",
                        )
                safe_extract_result(archive, result_dir)
                item.result_zip = str(archive)
                item.result_directory = str(result_dir)
                item.result_url = ""
                item.state = SliceState.DOWNLOADED
                item.error = ""
                self._notify(job, progress, f"已下载并校验切片 {item.index}/{len(job.slices)}")
            except Exception as error:
                item.error = str(error)
                if not isinstance(error, MinerUAPIError) or not (
                    error.retriable or error.code in {"download_forbidden", "incomplete_download"}
                ):
                    item.state = SliceState.FAILED
                self.save_job(job)
                raise
        return True

    def _default_merge(self, job: WorkflowJob, inputs: list[str]) -> Any:
        try:
            import mineru_epub
        except ImportError as error:
            raise WorkflowError("无法加载 Litera MinerU 合并核心。") from error
        return mineru_epub.convert_mineru(
            inputs,
            job.output_epub,
            title=job.project_name or friendly_project_name(job.source_pdf),
            language=epub_language_for_mineru(job.language),
            merged_directory=job.merged_output or None,
            auto_order=False,
            dedupe_boundaries=False,
            merge_origin_pdf=False,
            source_pdf=job.source_pdf if job.keep_original_pdf else None,
        )

    def _merge(self, job: WorkflowJob, progress: ProgressCallback | None) -> None:
        missing = [item.index for item in job.slices if item.state != SliceState.DOWNLOADED or not item.result_directory]
        if missing:
            raise WorkflowError(f"切片结果尚未完整下载：{missing}")
        job.state = JobState.MERGING
        self._notify(job, progress, "正在按原始页码顺序合并并校验结果")
        inputs = [item.result_directory for item in sorted(job.slices, key=lambda item: item.start_page)]
        self.merge_callback(job, inputs)

    def _snapshot(self, job: WorkflowJob) -> None:
        target = Path(job.raw_output) / "job.json"
        _atomic_json(target, job.to_dict())

    def _cleanup_temporaries(self, job: WorkflowJob) -> None:
        workspace = Path(job.workspace).resolve()
        for name in ("split", "downloads"):
            target = (workspace / name).resolve()
            if workspace in target.parents and target.exists():
                shutil.rmtree(target)
        for item in job.slices:
            if item.result_zip and not Path(item.result_zip).is_file():
                item.result_zip = ""

    def run_or_resume(
        self,
        job_or_path: WorkflowJob | str | Path,
        *,
        token: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> WorkflowJob:
        job = job_or_path if isinstance(job_or_path, WorkflowJob) else self.load_job(job_or_path)
        self._stop_event.clear()
        if job.state == JobState.COMPLETED:
            return job
        if any(item.state == SliceState.FAILED for item in job.slices):
            raise WorkflowError("任务含失败切片，请先使用“重试失败”。")
        client: MinerUClient | None = None
        try:
            client = self.client_factory((token if token is not None else get_mineru_token()).strip())
            self._prepare(job, progress)
            if not self._upload(job, client, progress):
                return job
            if not self._poll(job, client, progress):
                return job
            if not self._download(job, client, progress):
                return job
            self._merge(job, progress)
            job.state = JobState.COMPLETED
            job.error = ""
            self._notify(job, progress, f"完成：{job.output_epub}")
            self._cleanup_temporaries(job)
            self.save_job(job)
            self._snapshot(job)
            return job
        except Exception as error:
            if job.state != JobState.WAITING:
                job.state = JobState.FAILED
                job.error = str(error)
                self._notify(job, progress, f"失败：{error}")
            raise
        finally:
            if client:
                client.close()

    def retry_failed(self, job_or_path: WorkflowJob | str | Path) -> WorkflowJob:
        job = job_or_path if isinstance(job_or_path, WorkflowJob) else self.load_job(job_or_path)
        failed = [item for item in job.slices if item.state == SliceState.FAILED]
        if not failed:
            if job.state == JobState.FAILED:
                # Failures before splitting (for example a missing local HTTP
                # dependency or an unavailable Keychain) have no failed slice
                # to reset, but the whole job is still safe to retry.
                job.state = JobState.QUEUED
                job.error = ""
                self.save_job(job)
                return job
            raise WorkflowError("任务没有失败切片。")
        for item in failed:
            if item.result_directory and _is_result_directory(Path(item.result_directory)):
                item.state = SliceState.DOWNLOADED
            elif item.result_url and item.batch_id:
                # Query the batch again so an expired or rejected CDN URL is
                # refreshed before the next download attempt.
                item.state = SliceState.PROCESSING
                item.result_url = ""
            else:
                local = Path(item.local_pdf)
                if not local.is_file():
                    source = self._verify_source(job)
                    _write_pdf_range(source, local, item.start_page, item.end_page)
                    if local.stat().st_size > job.max_slice_bytes:
                        local.unlink(missing_ok=True)
                        raise WorkflowError(
                            f"重建的 {item.start_page}–{item.end_page} 页切片超过当前文件大小限制，请新建任务并降低每片页数。"
                        )
                item.state = SliceState.PREPARED
                item.batch_id = ""
                item.remote_state = ""
            item.error = ""
        job.state = JobState.QUEUED
        job.error = ""
        self.save_job(job)
        return job

    def stop_waiting(self) -> None:
        self._stop_event.set()

    def cleanup(self, job_or_path: WorkflowJob | str | Path, *, remove_results: bool = False) -> WorkflowJob:
        job = job_or_path if isinstance(job_or_path, WorkflowJob) else self.load_job(job_or_path)
        self._cleanup_temporaries(job)
        if remove_results:
            raw = Path(job.raw_output).resolve()
            if job.project_root:
                project = Path(job.project_root).resolve()
                if raw != project / "mineru":
                    raise WorkflowError("拒绝清理任务输出：MinerU 资源目录不符合项目结构。")
            else:
                output_parent = Path(job.output_epub).resolve().parent
                if raw.parent != output_parent:
                    raise WorkflowError("拒绝清理任务输出：目录不在所选输出位置。")
            if raw.exists():
                shutil.rmtree(raw)
            for item in job.slices:
                item.result_directory = ""
                item.result_zip = ""
        self.save_job(job)
        return job
