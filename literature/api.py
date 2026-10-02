from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from fastapi import File, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from .exporters import export_all
from .mineru import detect_language, import_mineru
from .mineru_capabilities import MAX_BATCH_FILES, validate_source
from .mineru_workflow import MinerUClient, WorkflowError
from .model_services import MODEL_ROLES, ROLE_STRUCTURE, ROLE_TRANSLATION, ModelServiceStore, save_service_key
from .models import UsageRecord
from .ocr import MinerUService, delete_mineru_token, get_mineru_token, save_mineru_token
from .providers import PROVIDER_PRESETS, OpenAICompatibleProvider, ProviderConfig, ProviderError, delete_api_key, get_api_key, save_api_key
from .runtime_revision import RUNTIME_REVISION
from .storage import ProjectStore, StorageError
from .structure import analyze_structure, analyze_structure_with_provider, apply_automatic, apply_patch, find_toc_page_candidates, plan_structure_analysis, revert_patch, run_structure_job, structure_model_payload
from .tbl import TBLBridge
from .translation import TranslationService, draft_glossary
from .uploads import UploadStore


logger = logging.getLogger(__name__)


def application_support() -> Path:
    configured = os.environ.get("LITERATURE_RUNTIME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / "Library" / "Application Support" / "Literature"


def session_token() -> str:
    configured = os.environ.get("LITERATURE_SESSION_TOKEN", "").strip()
    if configured:
        return configured
    path = application_support() / "session-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(32)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return value


class ProjectCreate(BaseModel):
    source_file: str = ""
    source_pdf: str = ""  # Backward-compatible alias for existing local clients.
    title: str = ""
    source_language: str = "auto"
    target_language: str = "zh-CN"


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=240)
    source_language: Literal["auto", "en", "ja", "fr", "de"] | None = None
    target_language: str | None = None
    chapter_language_overrides: dict[str, Literal["en", "ja", "fr", "de"]] | None = None
    budget_usd: float | None = Field(default=None, ge=0)


class MinerUImport(BaseModel):
    paths: list[str]


class MinerUJobCreate(BaseModel):
    language: str = "ch"
    model_version: str = "vlm"
    is_ocr: bool = True
    enable_table: bool = True
    enable_formula: bool = True
    page_ranges: str = Field(default="", max_length=2000)
    extra_formats: list[Literal["docx", "html", "latex"]] = Field(default_factory=list)
    page_limit: int = Field(default=200, ge=1, le=200)


class IntakeItemOptions(BaseModel):
    upload_id: str = ""
    path: str = ""
    page_ranges: str = Field(default="", max_length=2000)
    extra_formats: list[Literal["docx", "html", "latex"]] = Field(default_factory=list)


class IntakeJobCreate(MinerUJobCreate):
    model_config = ConfigDict(extra="forbid")

    items: list[IntakeItemOptions] = Field(default_factory=list, max_length=MAX_BATCH_FILES)
    upload_ids: list[str] = Field(default_factory=list, max_length=MAX_BATCH_FILES)
    paths: list[str] = Field(default_factory=list, max_length=MAX_BATCH_FILES)


class SecretInput(BaseModel):
    value: str


class StructureAnalyze(BaseModel):
    toc: list[dict[str, Any]]
    auto_apply: bool = False


class PatchInput(BaseModel):
    block_id: str
    operation: Literal["set_type", "set_level", "set_title"]
    after: str | int
    confidence: float = 1.0
    reason: str = ""
    automatic: bool = False


class ProviderInput(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 8192
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    def config(self) -> ProviderConfig:
        if self.provider not in PROVIDER_PRESETS:
            raise ValueError(f"未知服务商：{self.provider}")
        return ProviderConfig.preset(
            self.provider, model=self.model or PROVIDER_PRESETS[self.provider]["model"],
            base_url=self.base_url or PROVIDER_PRESETS[self.provider]["base_url"],
            temperature=self.temperature, max_output_tokens=self.max_output_tokens,
            input_price_per_million=self.input_price_per_million,
            output_price_per_million=self.output_price_per_million,
        )


class StructureAIAnalyze(BaseModel):
    toc: list[dict[str, Any]]
    provider: ProviderInput | None = None
    service_id: str = ""
    auto_apply: bool = False


class StructureJobInput(BaseModel):
    service_id: str = ""
    toc: list[dict[str, Any]] = Field(default_factory=list)
    extract_toc: bool = True
    proofread: bool = True
    auto_apply: bool = True
    toc_page_limit: int = Field(default=12, ge=1, le=40)


class StructurePlanInput(BaseModel):
    toc: list[dict[str, Any]] = Field(default_factory=list)
    extract_toc: bool = True
    proofread: bool = True
    toc_page_limit: int = Field(default=12, ge=1, le=40)


class ModelServiceInput(BaseModel):
    name: str = ""
    preset: str = "custom"
    base_url: str = ""
    model: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 8192
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None


class ModelRolesInput(BaseModel):
    structure: str | None = None
    translation: str | None = None


class TranslationJobInput(BaseModel):
    provider: ProviderInput | None = None
    service_id: str = ""
    block_ids: list[str] = Field(default_factory=list)
    locale: str = "zh-CN"
    glossary: dict[str, str] | None = None
    refine: bool = False
    force: bool = False
    instructions: str = Field(default="", max_length=8000)


class InstantInput(BaseModel):
    provider: ProviderInput | None = None
    service_id: str = ""
    text: str
    locale: str = "zh-CN"
    block_id: str | None = None
    save: bool = False


class TranslationEdit(BaseModel):
    text: str
    locale: str = "zh-CN"


class SelectionSave(BaseModel):
    source_text: str
    translated_text: str
    locale: str = "zh-CN"


class PatchBatchInput(BaseModel):
    patches: list[PatchInput]


class PatchRevertBatchInput(BaseModel):
    patch_ids: list[str]


class GlossaryInput(BaseModel):
    entries: dict[str, str]


class ExportInput(BaseModel):
    mode: Literal["source", "translation", "bilingual"] = "translation"
    locale: str = "zh-CN"
    formats: list[Literal["html", "md+json", "epub", "docx"]] = Field(default_factory=lambda: ["html", "md+json", "epub", "docx"])


class TBLPrepareInput(BaseModel):
    format: Literal["epub", "docx"] = "epub"
    mode: Literal["source", "translation", "bilingual"] = "source"


class TBLConfigInput(BaseModel):
    command: str = ""
    url: str = "http://127.0.0.1:5001"


class JobManager:
    def __init__(self, store: ProjectStore, ocr: MinerUService, models: ModelServiceStore | None = None) -> None:
        self.store = store
        self.ocr = ocr
        self.models = models or ModelServiceStore()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="literature-job")
        self.pause_events: dict[str, threading.Event] = {}
        self.ocr_running: set[str] = set()
        self.ocr_lock = threading.Lock()

    def provider_from(self, service_id: str, provider_input: ProviderInput | None = None) -> OpenAICompatibleProvider:
        """优先按模型服务库解析（密钥只从 Keychain 取）；否则兼容旧的随请求 ProviderInput。"""
        if service_id:
            return self.models.config_for(service_id)
        if provider_input is not None:
            return OpenAICompatibleProvider(provider_input.config())
        raise ProviderError("尚未选择模型服务，请先在当前模块配置模型。")

    def service_id_for(self, role: str, explicit: str = "") -> str:
        if explicit:
            return explicit
        if role not in MODEL_ROLES:
            raise ProviderError(f"未知任务角色：{role}")
        resolved = self.models.roles().get(role, "")
        if not resolved:
            raise ProviderError("尚未为当前模块绑定模型服务，请先配置模型。")
        return resolved

    def start_structure(self, project_id: str, request: "StructureJobInput") -> str:
        if not request.extract_toc and not request.proofread:
            raise ProviderError("至少需要勾选“提取目录”或“校对标题”中的一项。")
        service_id = self.service_id_for(ROLE_STRUCTURE, request.service_id)
        provider = self.provider_from(service_id)
        service = self.models.get(service_id)
        config_value = request.model_dump()
        config_value["service_id"] = service_id
        scope = {
            "toc_entries": len(request.toc),
            "extract_toc": request.extract_toc,
            "proofread": request.proofread,
            "body_text_included": False,
            "toc_page_text_included": request.extract_toc,
            "model": service.get("model", ""),
            "service_name": service.get("name", ""),
        }
        job_id = self.store.create_job(project_id, "structure_ai", scope, config_value)

        def run() -> None:
            try:
                run_structure_job(self.store, project_id, job_id, config_value, provider)
            except Exception:
                logger.exception("Structure job %s failed", job_id)

        self.executor.submit(run)
        return job_id

    def start_translation(self, project_id: str, request: TranslationJobInput, *, existing_job_id: str | None = None) -> str:
        scope = {"block_ids": request.block_ids, "locale": request.locale}
        config_value = request.model_dump()
        job_id = existing_job_id or self.store.create_job(project_id, "refine" if request.refine else "translate", scope, config_value)
        if existing_job_id:
            self.store.update_job(project_id, job_id, status="queued", error="")
        pause = threading.Event()
        self.pause_events[job_id] = pause

        def run() -> None:
            try:
                provider = self.provider_from(request.service_id, request.provider)
                service = TranslationService(self.store, provider)
                service.run(
                    project_id, block_ids=request.block_ids, locale=request.locale, glossary=request.glossary,
                    refine=request.refine, force=request.force, instructions=request.instructions,
                    job_id=job_id, should_pause=pause.is_set,
                )
            except Exception as error:
                self.store.update_job(project_id, job_id, status="failed", error=str(error))

        self.executor.submit(run)
        return job_id

    def pause(self, job_id: str) -> None:
        event = self.pause_events.get(job_id)
        if event:
            event.set()

    def start_ocr(self, job_id: str) -> bool:
        with self.ocr_lock:
            if job_id in self.ocr_running:
                return False
            self.ocr_running.add(job_id)

        def run() -> None:
            try:
                self.ocr.run(job_id)
            except Exception:
                # MinerUWorkflow and MinerUService persist the actionable error
                # for the UI; keep a traceback in the local API log as well.
                logger.exception("MinerU job %s failed", job_id)
            finally:
                with self.ocr_lock:
                    self.ocr_running.discard(job_id)

        try:
            self.executor.submit(run)
        except Exception:
            with self.ocr_lock:
                self.ocr_running.discard(job_id)
            raise
        return True

    def resume_ocr_jobs(self) -> int:
        if not get_mineru_token():
            return 0
        resumable = {
            "queued", "preparing", "uploading", "processing",
            "downloading", "merging", "waiting",
        }
        started = 0
        for job in self.ocr.list():
            if job["state"] in resumable and self.start_ocr(job["job_id"]):
                started += 1
        return started

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=False)


def create_app(store: ProjectStore | None = None):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, StreamingResponse
    except ImportError as error:
        raise RuntimeError("缺少 FastAPI，请安装 Literature Python 依赖。") from error

    store = store or ProjectStore()
    try:
        store.assert_accessible()
    except StorageError as error:
        # This happens before FastAPI can serve a useful error response.  Make
        # startup fail instead of running against a partially visible library.
        raise RuntimeError(str(error)) from error
    ocr = MinerUService(store)
    models = ModelServiceStore(application_support())
    manager = JobManager(store, ocr, models)
    uploads = UploadStore(application_support() / "uploads")
    tbl = TBLBridge(store)
    token = session_token()
    @asynccontextmanager
    async def lifespan(_app: Any):
        manager.resume_ocr_jobs()
        try:
            yield
        finally:
            manager.shutdown()

    app = FastAPI(title="Literature Local API", version="0.1.0", lifespan=lifespan)
    app.state.session_token = token
    app.state.store = store
    app.state.ocr_service = ocr
    app.state.upload_store = uploads
    app.state.job_manager = manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:1420", "http://127.0.0.1:1420", "tauri://localhost", "https://tauri.localhost"],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
    )

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if request.method != "OPTIONS" and request.url.path not in {"/health"} and request.headers.get("x-literature-token") != token:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "invalid local session token"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(StorageError)
    async def storage_error(_request: Request, error: StorageError):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(WorkflowError)
    async def workflow_error(_request: Request, error: WorkflowError):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.exception_handler(ProviderError)
    async def provider_error(_request: Request, error: ProviderError):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(error)}, status_code=502)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "runtime_revision": RUNTIME_REVISION,
            "capabilities": ["intake_items_v1", "structure_review_v2", "model_services_v1", "structure_job_v1"],
        }

    @app.get("/projects")
    def projects():
        return store.list_projects()

    @app.post("/projects")
    def create_project(request: ProjectCreate):
        source = request.source_file or request.source_pdf
        if not source:
            raise StorageError("请选择源文件。")
        return store.create_project(source, title=request.title, source_language=request.source_language, target_language=request.target_language)

    @app.get("/projects/{project_id}")
    def project(project_id: str):
        return {**store.describe_project(project_id), "jobs": store.list_jobs(project_id), "usage": store.usage_summary(project_id)}

    @app.patch("/projects/{project_id}")
    def update_project(project_id: str, request: ProjectUpdate):
        if request.title is not None:
            store.rename_project(project_id, request.title)

        project_path = store.project_path(project_id)
        has_document = (project_path / "document" / "document.json").is_file()
        document_changes_requested = any(value is not None for value in (
            request.source_language, request.target_language, request.chapter_language_overrides,
        ))
        if document_changes_requested and not has_document:
            raise StorageError("项目尚未生成规范文档，当前只能重命名或调整预算。")

        changes: dict[str, Any] = {}
        if request.budget_usd is not None:
            changes["budget_usd"] = request.budget_usd
        if has_document:
            document = store.load_document(project_id)
            if request.source_language is not None:
                document.source_language_mode = request.source_language
                document.source_language = detect_language(document.blocks) if request.source_language == "auto" else request.source_language
                changes.update(source_language=document.source_language, source_language_mode=request.source_language)
            if request.target_language is not None:
                document.target_language = request.target_language
                changes["target_language"] = request.target_language
            if request.chapter_language_overrides is not None:
                for block_id in request.chapter_language_overrides:
                    block = document.block(block_id)
                    if block.type != "title":
                        raise HTTPException(status_code=400, detail=f"章节语言覆盖必须挂接标题 block：{block_id}")
                document.chapter_language_overrides = dict(request.chapter_language_overrides)
                changes["chapter_language_overrides"] = document.chapter_language_overrides
            store.save_document(document)
            manifest = store.update_manifest(project_id, **changes) if changes else store.load_manifest(project_id)
            return {**document.to_dict(), "budget_usd": manifest.get("budget_usd", 0)}
        if changes:
            store.update_manifest(project_id, **changes)
        return store.describe_project(project_id)

    @app.delete("/projects/{project_id}")
    def delete_project(project_id: str):
        active_ocr_states = {"queued", "preparing", "uploading", "processing", "downloading", "merging", "waiting"}
        active_ocr = [job for job in ocr.list(project_id) if job["state"] in active_ocr_states]
        active_jobs = [job for job in store.list_jobs(project_id) if job["status"] in {"queued", "running"}]
        if active_ocr or active_jobs:
            raise HTTPException(status_code=409, detail="项目仍有运行或等待中的任务，请等待任务结束后再删除。")
        ocr.archive_project_jobs(project_id)
        return store.delete_project(project_id)

    @app.get("/projects/{project_id}/document")
    def document(project_id: str):
        return store.load_document(project_id).to_dict()

    @app.get("/projects/{project_id}/source.pdf")
    def source_pdf(project_id: str):
        source = store.source_path(project_id)
        if source.suffix.casefold() != ".pdf":
            raise HTTPException(status_code=404, detail="该项目的源文件不是 PDF。")
        return FileResponse(source, media_type="application/pdf")

    @app.get("/projects/{project_id}/source")
    def source_file(project_id: str):
        source = store.source_path(project_id)
        return FileResponse(source, filename=source.name)

    @app.get("/projects/{project_id}/assets/{name}")
    def asset(project_id: str, name: str):
        if Path(name).name != name:
            raise HTTPException(status_code=400, detail="invalid asset")
        path = store.project_path(project_id) / "assets" / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="asset not found")
        return FileResponse(path)

    @app.get("/projects/{project_id}/raw")
    def raw_files(project_id: str):
        """列出已封存的 MinerU 原始文件，供对照阅读核对逐格式输出。"""
        manifest_path = store.project_path(project_id) / "ocr" / "raw" / "manifest.json"
        if not manifest_path.is_file():
            return {"files": []}
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = [
            {"path": str(item.get("path", "")), "size": int(item.get("size", 0)), "sha256": str(item.get("sha256", ""))}
            for slice_info in value.get("slices", [])
            for item in slice_info.get("files", [])
        ]
        return {"files": files}

    @app.get("/projects/{project_id}/raw/{name:path}")
    def raw_file(project_id: str, name: str):
        """只读读取封存原始文件；路径必须落在 ocr/raw/mineru/ 之内。"""
        project = store.project_path(project_id).resolve()
        root = (project / "ocr" / "raw" / "mineru").resolve()
        candidate = (project / name).resolve()
        if candidate != root and root not in candidate.parents:
            raise HTTPException(status_code=400, detail="invalid raw path")
        if not candidate.is_file():
            raise HTTPException(status_code=404, detail="raw file not found")
        return FileResponse(candidate)

    @app.post("/projects/{project_id}/mineru/import")
    def mineru_import(project_id: str, request: MinerUImport):
        return import_mineru(store, project_id, request.paths).to_dict()

    @app.get("/projects/{project_id}/mineru/verify")
    def mineru_verify(project_id: str):
        errors = store.verify_raw(project_id)
        return {"valid": not errors, "errors": errors}

    @app.get("/mineru/token")
    def mineru_token_status():
        return {"configured": bool(get_mineru_token())}

    @app.post("/mineru/token/check")
    def mineru_token_check():
        client = MinerUClient(get_mineru_token(), max_attempts=2)
        try:
            client.check_token()
        finally:
            client.close()
        return {"configured": True, "connected": True}

    @app.put("/mineru/token")
    def mineru_token_save(request: SecretInput):
        save_mineru_token(request.value)
        return {"configured": True, "resumed_jobs": manager.resume_ocr_jobs()}

    @app.delete("/mineru/token")
    def mineru_token_delete():
        delete_mineru_token()
        return {"configured": False}

    @app.post("/projects/{project_id}/ocr/jobs")
    def create_ocr_job(project_id: str, request: MinerUJobCreate):
        return ocr.create(project_id, **request.model_dump())

    @app.post("/intake/uploads")
    def create_intake_uploads(files: list[UploadFile] = File(...)):
        if not files or len(files) > MAX_BATCH_FILES:
            raise StorageError(f"一次请选择 1–{MAX_BATCH_FILES} 个文件。")
        saved = []
        try:
            for item in files:
                saved.append(uploads.save(item.filename or "", item.file, content_type=item.content_type or ""))
        except Exception:
            for item in saved:
                uploads.discard(str(item["upload_id"]))
            raise
        return saved

    @app.delete("/intake/uploads/{upload_id}")
    def discard_intake_upload(upload_id: str):
        uploads.discard(upload_id)
        return {"discarded": True}

    @app.post("/intake/jobs")
    def create_intake_jobs(request: IntakeJobCreate):
        request_upload_ids = {
            item.upload_id for item in request.items if item.upload_id
        } | set(request.upload_ids)
        sources: list[tuple[Path, str | None, IntakeItemOptions | None]] = []
        created_project_ids: list[str] = []
        try:
            seen: set[Path] = set()
            if request.items and (request.upload_ids or request.paths):
                raise StorageError("逐文件设置不能与旧版批量文件字段同时使用。")
            if request.items:
                for item in request.items:
                    if bool(item.upload_id) == bool(item.path):
                        raise StorageError("每个文件设置必须且只能指定 upload_id 或 path。")
                    if item.upload_id:
                        source, _metadata = uploads.resolve(item.upload_id)
                        upload_id: str | None = item.upload_id
                    else:
                        try:
                            source = validate_source(item.path)
                        except ValueError as error:
                            raise StorageError(str(error)) from error
                        upload_id = None
                    resolved = source.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        sources.append((resolved, upload_id, item))
            else:
                for upload_id in request.upload_ids:
                    source, _metadata = uploads.resolve(upload_id)
                    resolved = source.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        sources.append((resolved, upload_id, None))
                for raw in request.paths:
                    try:
                        source = validate_source(raw)
                    except ValueError as error:
                        raise StorageError(str(error)) from error
                    if source in seen:
                        continue
                    seen.add(source)
                    sources.append((source, None, None))
            if not sources:
                raise StorageError("请选择至少一个 MinerU 支持的文件。")

            options = request.model_dump(exclude={"items", "paths", "upload_ids"})
            token_configured = bool(get_mineru_token())
            jobs = []
            # Build the whole local batch before starting any background work.
            # If item N fails, items 1..N-1 are moved out of the active indexes
            # so retrying the batch cannot create hidden duplicates.
            for source, _upload_id, item in sources:
                project = store.create_project(source, source_language="auto")
                project_id = str(project["project_id"])
                created_project_ids.append(project_id)
                file_options = dict(options)
                if item is not None:
                    file_options.update(page_ranges=item.page_ranges, extra_formats=item.extra_formats)
                job = ocr.create(project_id, **file_options)
                jobs.append(ocr.get(job["job_id"]))
        except Exception:
            for project_id in reversed(created_project_ids):
                try:
                    ocr.archive_project_jobs(project_id)
                    store.delete_project(project_id)
                except Exception:
                    logger.exception("Failed to roll back intake project %s", project_id)
            for upload_id in request_upload_ids:
                uploads.discard(upload_id)
            raise

        for upload_id in request_upload_ids:
            uploads.discard(upload_id)
        if token_configured:
            for job in jobs:
                try:
                    manager.start_ocr(job["job_id"])
                except Exception:
                    # The persisted queued job remains resumable on the next
                    # startup; do not turn an accepted batch into a false 400.
                    logger.exception("Could not start queued MinerU job %s", job["job_id"])
        return {"jobs": jobs, "token_configured": token_configured}

    @app.get("/ocr/jobs")
    def list_all_ocr_jobs():
        return ocr.list()

    @app.post("/ocr/jobs/{job_id}/start")
    def start_ocr_job(job_id: str):
        if not get_mineru_token():
            raise StorageError("请先配置 MinerU Token。")
        started = manager.start_ocr(job_id)
        return {"job_id": job_id, "state": "starting" if started else "already_running"}

    @app.post("/ocr/jobs/{job_id}/retry")
    def retry_ocr_job(job_id: str):
        if not get_mineru_token():
            raise StorageError("请先配置 MinerU Token。")
        ocr.retry(job_id)
        manager.start_ocr(job_id)
        return ocr.get(job_id)

    @app.get("/projects/{project_id}/ocr/jobs")
    def list_ocr_jobs(project_id: str):
        return ocr.list(project_id)

    @app.post("/projects/{project_id}/structure/analyze")
    def structure_analyze(project_id: str, request: StructureAnalyze):
        suggestions = analyze_structure(store.load_document(project_id), request.toc)
        applied = [asdict(value) for value in apply_automatic(store, project_id, suggestions)] if request.auto_apply else []
        return {"suggestions": suggestions, "applied": applied}

    @app.get("/projects/{project_id}/structure/toc-page-candidates")
    def structure_toc_candidates(project_id: str):
        return find_toc_page_candidates(store.load_document(project_id))

    @app.post("/projects/{project_id}/structure/audit-payload")
    def structure_audit_payload(project_id: str, request: StructureAnalyze):
        # v1 审计：只包含目录文本与标题候选，不含任何正文/目录页文本。
        return structure_model_payload(store.load_document(project_id), request.toc)

    @app.post("/projects/{project_id}/structure/plan")
    def structure_plan(project_id: str, request: StructurePlanInput):
        # v2 审计：与异步作业完全一致的输入，包含将要发送的目录页文本。
        return plan_structure_analysis(
            store.load_document(project_id), request.toc,
            extract_toc=request.extract_toc, proofread=request.proofread,
            toc_page_limit=request.toc_page_limit,
        )

    @app.post("/projects/{project_id}/structure/jobs")
    def structure_job_create(project_id: str, request: StructureJobInput):
        store.load_document(project_id)  # 提前校验文档存在，避免创建无效任务
        job_id = manager.start_structure(project_id, request)
        return store.get_job(project_id, job_id)

    @app.get("/projects/{project_id}/structure/jobs/{job_id}")
    def structure_job_get(project_id: str, job_id: str):
        return store.get_job(project_id, job_id)

    @app.post("/projects/{project_id}/structure/analyze-ai")
    def structure_analyze_ai(project_id: str, request: StructureAIAnalyze):
        scope = {"toc_entries": len(request.toc), "body_text_included": False}
        job_id = store.create_job(project_id, "structure_ai", scope, request.model_dump())
        store.update_job(project_id, job_id, status="running")
        try:
            provider = manager.provider_from(request.service_id, request.provider)
            suggestions, response = analyze_structure_with_provider(
                store.load_document(project_id), request.toc, provider,
            )
            applied = [asdict(value) for value in apply_automatic(store, project_id, suggestions)] if request.auto_apply else []
            store.record_usage(UsageRecord(
                usage_id=uuid.uuid4().hex, project_id=project_id, job_id=job_id,
                provider=response.provider, model=response.model, scope="structure_ai",
                input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                cached_tokens=response.cached_tokens, reasoning_tokens=response.reasoning_tokens,
                estimated_cost=response.estimated_cost, request_id=response.request_id,
            ))
            store.update_job(project_id, job_id, status="completed", progress=1)
            return {"suggestions": suggestions, "applied": applied, "job_id": job_id, "audit": scope}
        except Exception as error:
            store.update_job(project_id, job_id, status="failed", error=str(error))
            raise

    @app.post("/projects/{project_id}/structure/patches")
    def structure_patch(project_id: str, request: PatchInput):
        return asdict(apply_patch(store, project_id, request.model_dump(), source="user"))

    @app.post("/projects/{project_id}/structure/patches/batch")
    def structure_patch_batch(project_id: str, request: PatchBatchInput):
        return [
            asdict(apply_patch(store, project_id, patch.model_dump(), source="user"))
            for patch in request.patches
        ]

    @app.get("/projects/{project_id}/structure/patches")
    def structure_patches(project_id: str):
        return store.list_patches(project_id)

    @app.post("/projects/{project_id}/structure/patches/{patch_id}/revert")
    def structure_revert(project_id: str, patch_id: str):
        return asdict(revert_patch(store, project_id, patch_id))

    @app.post("/projects/{project_id}/structure/patches/revert-batch")
    def structure_revert_batch(project_id: str, request: PatchRevertBatchInput):
        return [asdict(revert_patch(store, project_id, patch_id)) for patch_id in request.patch_ids]

    @app.get("/projects/{project_id}/glossary/draft")
    def glossary_draft(project_id: str):
        return draft_glossary(store.load_document(project_id))

    @app.put("/projects/{project_id}/translations/{locale}/glossary")
    def glossary_freeze(project_id: str, locale: str, request: GlossaryInput):
        layer = store.load_translation(project_id, locale)
        layer.glossary_snapshot = {
            str(source).strip(): str(target).strip()
            for source, target in request.entries.items() if str(source).strip()
        }
        store.save_translation(layer)
        return {"entries": layer.glossary_snapshot, "updated_at": layer.updated_at}

    @app.get("/providers")
    def provider_presets():
        return [{**value, "provider": key, "configured": bool(get_api_key(key))} for key, value in PROVIDER_PRESETS.items()]

    @app.put("/providers/{provider}/key")
    def provider_key_save(provider: str, request: SecretInput):
        if provider not in PROVIDER_PRESETS:
            raise HTTPException(status_code=404, detail="unknown provider")
        save_api_key(provider, request.value)
        return {"configured": True}

    @app.delete("/providers/{provider}/key")
    def provider_key_delete(provider: str):
        if provider not in PROVIDER_PRESETS:
            raise HTTPException(status_code=404, detail="unknown provider")
        delete_api_key(provider)
        return {"configured": False}

    def _service_view() -> dict[str, Any]:
        return {
            "services": [{**item, "configured": models.configured(item["service_id"])} for item in models.list()],
            "roles": models.roles(),
        }

    @app.get("/model-services")
    def model_service_list():
        return _service_view()

    @app.post("/model-services")
    def model_service_create(request: ModelServiceInput):
        created = models.create(request.model_dump())
        return {**created, "configured": models.configured(created["service_id"])}

    @app.put("/model-services/roles")
    def model_service_roles(request: ModelRolesInput):
        return models.set_roles(request.model_dump(exclude_none=True))

    @app.put("/model-services/{service_id}")
    def model_service_update(service_id: str, request: ModelServiceInput):
        updated = models.update(service_id, request.model_dump())
        return {**updated, "configured": models.configured(service_id)}

    @app.delete("/model-services/{service_id}")
    def model_service_delete(service_id: str):
        models.delete(service_id)
        return {"deleted": service_id}

    @app.put("/model-services/{service_id}/key")
    def model_service_key(service_id: str, request: SecretInput):
        models.get(service_id)  # 校验存在
        save_service_key(service_id, request.value)
        return {"configured": True}

    @app.post("/model-services/{service_id}/check")
    def model_service_check(service_id: str):
        import time as _time
        started = _time.perf_counter()
        provider = models.config_for(service_id, timeout=30.0)
        response = provider.complete(
            system="你是连接性探测助手。只返回 JSON {\"ok\":true}。",
            user="ping",
        )
        return {
            "ok": True,
            "service_id": service_id,
            "model": response.model,
            "latency_ms": round((_time.perf_counter() - started) * 1000),
            "sample": response.content[:160],
        }

    @app.post("/projects/{project_id}/translation/jobs")
    def translation_job(project_id: str, request: TranslationJobInput):
        job_id = manager.start_translation(project_id, request)
        return store.get_job(project_id, job_id)

    @app.post("/projects/{project_id}/translation/jobs/{job_id}/pause")
    def translation_pause(project_id: str, job_id: str):
        manager.pause(job_id)
        return {"job_id": job_id, "state": "pause_requested"}

    @app.post("/projects/{project_id}/translation/jobs/{job_id}/resume")
    def translation_resume(project_id: str, job_id: str):
        job = store.get_job(project_id, job_id)
        request = TranslationJobInput.model_validate(job["config"])
        manager.start_translation(project_id, request, existing_job_id=job_id)
        return store.get_job(project_id, job_id)

    @app.get("/projects/{project_id}/translation/jobs")
    def translation_jobs(project_id: str):
        return store.list_jobs(project_id)

    @app.get("/projects/{project_id}/translations/{locale}")
    def translation_layer(project_id: str, locale: str):
        return store.load_translation(project_id, locale).to_dict()

    @app.post("/projects/{project_id}/translate/instant")
    def translate_instant(project_id: str, request: InstantInput):
        service = TranslationService(store, manager.provider_from(request.service_id, request.provider))
        return service.instant(project_id, request.text, locale=request.locale, block_id=request.block_id, save=request.save)

    @app.put("/projects/{project_id}/translations/{block_id}")
    def translation_edit(project_id: str, block_id: str, request: TranslationEdit):
        # Provider is irrelevant for a manual edit.
        from .providers import MockProvider
        return asdict(TranslationService(store, MockProvider()).edit(project_id, block_id, request.text, locale=request.locale))

    @app.post("/projects/{project_id}/translations/{block_id}/unlock")
    def translation_unlock(project_id: str, block_id: str, locale: str = "zh-CN"):
        from .providers import MockProvider
        return asdict(TranslationService(store, MockProvider()).unlock(project_id, block_id, locale=locale))

    @app.post("/projects/{project_id}/translations/{block_id}/selections")
    def translation_selection_save(project_id: str, block_id: str, request: SelectionSave):
        from .providers import MockProvider
        return asdict(TranslationService(store, MockProvider()).save_selection(
            project_id, block_id, request.source_text, request.translated_text, locale=request.locale,
        ))

    @app.get("/projects/{project_id}/usage")
    def usage(project_id: str):
        return store.usage_summary(project_id)

    @app.get("/projects/{project_id}/usage/records")
    def usage_records(project_id: str):
        return store.usage_records(project_id)

    @app.post("/projects/{project_id}/exports")
    def exports(project_id: str, request: ExportInput):
        return export_all(store, project_id, mode=request.mode, locale=request.locale, formats=list(request.formats))

    @app.post("/projects/{project_id}/tbl/prepare")
    def tbl_prepare(project_id: str, request: TBLPrepareInput):
        return tbl.prepare(project_id, format_name=request.format, mode=request.mode)

    @app.post("/tbl/start")
    def tbl_start():
        return tbl.start()

    @app.get("/tbl/config")
    def tbl_config():
        return tbl.config()

    @app.put("/tbl/config")
    def tbl_configure(request: TBLConfigInput):
        return tbl.configure(command=request.command, url=request.url)

    @app.get("/tbl/status")
    def tbl_status():
        return tbl.status()

    @app.get("/projects/{project_id}/events")
    async def events(project_id: str):
        async def stream():
            previous = ""
            while True:
                payload = json.dumps({"jobs": store.list_jobs(project_id), "ocr_jobs": ocr.list(project_id), "usage": store.usage_summary(project_id)}, ensure_ascii=False, sort_keys=True)
                if payload != previous:
                    yield f"event: state\ndata: {payload}\n\n"
                    previous = payload
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


try:
    app = create_app()
except RuntimeError:
    app = None


def main() -> None:
    try:
        import uvicorn
    except ImportError as error:
        raise SystemExit("缺少 uvicorn，请先安装 Literature 依赖。") from error
    if app is None:
        raise SystemExit("Literature API 初始化失败。")
    # Pass the object directly so a frozen Python sidecar does not need to
    # rediscover the package through an import string at runtime.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LITERATURE_PORT", "41731")), reload=False)


if __name__ == "__main__":
    main()
