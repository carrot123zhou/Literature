from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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

from .mineru import detect_language, import_mineru
from .mineru_capabilities import MAX_BATCH_FILES, validate_source
from .mineru_workflow import MinerUClient, WorkflowError
from .model_services import MODEL_ROLES, ROLE_STRUCTURE, ROLE_TRANSLATION, ModelServiceStore, save_service_key
from .models import UsageRecord
from .ocr import MinerUService, delete_mineru_token, get_mineru_token, save_mineru_token
from .parse_backends import describe_backends
from .providers import PROVIDER_PRESETS, OpenAICompatibleProvider, ProviderConfig, ProviderError, delete_api_key, get_api_key, normalize_proxy_environment, save_api_key
from .request_coordinator import BudgetExhausted, RequestCoordinator, prompt_cost_estimator
from .runtime_revision import RUNTIME_REVISION
from .storage import ProjectStore, StorageError
from .structure_signals import enrich_furniture_evidence
from .structure import analyze_structure, apply_automatic, apply_patch, apply_patch_batch, find_toc_page_candidates, local_printed_toc, plan_structure_analysis, reconcile_toc, revert_patch, run_structure_job, structure_model_payload
from .translation import TranslationService, align_structure_only, draft_glossary, estimate_translation, translation_payload, translation_stats, REVISION_PROTOCOL
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


class LocalFilesInspect(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=MAX_BATCH_FILES)


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


class ParseQueueMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: Literal["top", "up", "down", "end"]


class ParseContinue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_ranges: str = Field(default="", max_length=2000)
    language: str = ""
    model_version: str = ""
    page_limit: int = Field(default=200, ge=1, le=200)
    extra_formats: list[Literal["docx", "html", "latex"]] = Field(default_factory=list)


class ParseStart(BaseModel):
    """首次解析（无规范文档、无活动任务）的参数。"""
    model_config = ConfigDict(extra="forbid")

    page_ranges: str = Field(default="", max_length=2000)
    language: str = "ch"
    model_version: str = "vlm"
    is_ocr: bool = True
    enable_table: bool = True
    enable_formula: bool = True
    page_limit: int = Field(default=200, ge=1, le=200)
    extra_formats: list[Literal["docx", "html", "latex"]] = Field(default_factory=list)


class ArchiveSettingsInput(BaseModel):
    """解析历史保留期设置；retention_days=null 表示永久保留（条数上限仍在）。"""
    model_config = ConfigDict(extra="forbid")

    retention_days: int | None = None
    max_records: int = Field(default=1000, ge=1, le=100000)


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
    toc_page_indexes: list[int] | None = None
    toc: list[dict[str, Any]]
    auto_apply: bool = False


class PatchInput(BaseModel):
    block_id: str
    operation: Literal["set_type", "set_level", "set_title"]
    after: str | int
    confidence: float = 1.0
    reason: str = ""
    automatic: bool = False
    source: Literal["ai", "user"] = "user"
    # 建议生成时的块状态；确认时后端据此拒绝陈旧采用。
    before: Any = None
    # 建议生成时的文档版本；供调用方审计与将来的版本级冲突检测。
    expected_document_updated_at: str = ""


class ProviderInput(BaseModel):
    provider: str
    model: str = ""
    base_url: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 16384
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    reasoning_style: str = ""
    reasoning_effort: str = ""
    reasoning_budget: int = 0

    def config(self) -> ProviderConfig:
        if self.provider not in PROVIDER_PRESETS:
            raise ValueError(f"未知服务商：{self.provider}")
        return ProviderConfig.preset(
            self.provider, model=self.model or PROVIDER_PRESETS[self.provider]["model"],
            base_url=self.base_url or PROVIDER_PRESETS[self.provider]["base_url"],
            temperature=self.temperature, max_output_tokens=self.max_output_tokens,
            input_price_per_million=self.input_price_per_million,
            output_price_per_million=self.output_price_per_million,
            reasoning_style=self.reasoning_style, reasoning_effort=self.reasoning_effort,
            reasoning_budget=self.reasoning_budget,
        )


class StructureAIAnalyze(BaseModel):
    toc: list[dict[str, Any]]
    provider: ProviderInput | None = None
    service_id: str = ""
    auto_apply: bool = False
    # 复核模式：sample=第一遍定案 + 固定比例抽查（默认）；full=全量双遍；off=不抽查。
    # proofread_full=True 是 full 的兼容入口。
    proofread_mode: Literal["full", "sample", "off"] | None = None
    proofread_full: bool = False
    proofread_concurrency: int = Field(default=2, ge=1, le=4)
    planning_mode: Literal["global", "adaptive"] = "global"


class StructureJobInput(BaseModel):
    service_id: str = ""
    toc: list[dict[str, Any]] = Field(default_factory=list)
    extract_toc: bool = True
    # 目录来源：auto=有显式目录标记页且本地可解析时直接采信（默认，省一次模型请求）；
    # local=总是采信本地；model=总是让模型核对目录页文本。
    toc_extraction: Literal["auto", "local", "model"] = "auto"
    proofread: bool = True
    auto_apply: bool = False
    toc_page_limit: int = Field(default=6, ge=1, le=6)
    toc_page_indexes: list[int] | None = None
    use_vision: bool = False
    request_timeout_seconds: int = Field(default=600, ge=30, le=1800)
    reuse_job_id: str = ""
    # 复核模式：sample=第一遍规划定案 + 固定比例抽查（默认，最多 1 批抽查请求）；
    # full=全量双遍（历史行为，最贵）；off=只跑第一遍，不抽查。
    # proofread_full=True 是 full 的兼容入口；两者同时给出以 proofread_mode 为准。
    proofread_mode: Literal["full", "sample", "off"] | None = None
    proofread_full: bool = False
    proofread_concurrency: int = Field(default=2, ge=1, le=4)
    # 规划策略：global=整体串行（默认，质量已用过）；adaptive=框架验证后的分支规划（试用，未真实 A/B）。
    planning_mode: Literal["global", "adaptive"] = "global"


class StructurePlanInput(BaseModel):
    toc: list[dict[str, Any]] = Field(default_factory=list)
    extract_toc: bool = True
    proofread: bool = True
    toc_page_limit: int = Field(default=6, ge=1, le=6)
    toc_page_indexes: list[int] | None = None
    use_vision: bool = False
    planning_mode: Literal["global", "adaptive"] = "global"


class ModelServiceInput(BaseModel):
    name: str = ""
    preset: str = "custom"
    base_url: str = ""
    model: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 16384
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    reasoning_style: str = ""
    reasoning_effort: str = ""
    reasoning_budget: int = 0


class ModelRolesInput(BaseModel):
    structure: str | None = None
    translation: str | None = None


class TranslationJobInput(BaseModel):
    provider: ProviderInput | None = None
    service_id: str = ""
    scope: Literal["auto", "book", "untranslated", "stale", "failed", "blocks"] = "auto"
    block_ids: list[str] = Field(default_factory=list)
    locale: str = "zh-CN"
    glossary: dict[str, str] | None = None
    refine: bool = False
    force: bool = False
    instructions: str = Field(default="", max_length=8000)
    # 翻译后是否自动再做一遍文学润色（两遍制，质量更高但更慢，默认关闭）。
    polish: bool = False
    # 同一作业内并发请求数。小块 + 并发是整本书提速的主要来源；
    # AIMD 自适应会在 429 时自动降速，因此上限可以开得比以前高。
    parallel_workers: int = Field(default=4, ge=1, le=16)
    # 单次模型请求的读超时。流式下是「两个数据块之间的空闲时限」，
    # 非流式回退时才是等待整份响应的总时限；供不支持流式的中转站调大。
    request_timeout_seconds: int = Field(default=300, ge=30, le=1800)


class InstantInput(BaseModel):
    provider: ProviderInput | None = None
    service_id: str = ""
    text: str
    locale: str = "zh-CN"
    block_id: str | None = None
    save: bool = False


class ReviewInput(BaseModel):
    service_id: str = ""
    locale: str = "zh-CN"
    instructions: str = ""


class ReviewAdoptInput(BaseModel):
    locale: str = "zh-CN"
    # candidate_id 是客户端幂等/去重凭证，不是服务端保存的证据（候选不落库）。
    candidate_id: str = ""
    # 版本依据必填：缺失直接 422，服务层也会再验一次。
    source_hash: str
    base_revision: str
    revised: dict[str, Any]


class TranslationEdit(BaseModel):
    text: str | None = None
    locale: str = "zh-CN"
    # 按字段编辑：表格逐 cell、列表逐 item、图题/脚注分开输入（F10）。
    cells: dict[str, str] | None = None
    items: list[str] | None = None
    caption: list[str] | None = None
    footnote: list[str] | None = None


class SelectionSave(BaseModel):
    source_text: str
    translated_text: str
    locale: str = "zh-CN"


class PatchBatchInput(BaseModel):
    patches: list[PatchInput]
    source: Literal["ai", "user"] = "user"


class StructureBaselineInput(BaseModel):
    toc: list[dict[str, Any]] = Field(default_factory=list)
    source: str = ""


class SuggestionDecisionInput(BaseModel):
    decision: Literal["applied", "dismissed"] = "applied"


class SuggestionBatchDecisionInput(BaseModel):
    suggestion_ids: list[str] = Field(default_factory=list)
    decision: Literal["applied", "dismissed"] = "applied"


class PatchRevertBatchInput(BaseModel):
    patch_ids: list[str]


class GlossaryInput(BaseModel):
    entries: dict[str, str]


class GlossaryRollbackInput(BaseModel):
    version: int


class TranslationEstimateInput(BaseModel):
    service_id: str = ""
    scope: Literal["auto", "book", "untranslated", "stale", "failed", "blocks"] = "book"
    block_ids: list[str] = Field(default_factory=list)
    locale: str = "zh-CN"
    refine: bool = False
    force: bool = False
    polish: bool = False
    glossary: dict[str, str] = Field(default_factory=dict)
    instructions: str = ""


_TRANSLATE_ACTIVE_STATES = {"queued", "running"}


def translate_stage(project: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """翻译是独立工作流：用自己的 job 与译文摘要推导状态，不依赖校对。"""
    stats = project.get("translation") or {}
    total = int(stats.get("total") or 0)
    done = int(stats.get("done") or 0)
    failed = int(stats.get("failed") or 0)
    stale = int(stats.get("stale") or 0)
    # 纯层级变化已自动对齐，不应让卡片误判 partial；只有“原文内容已变”才算内容缺口。
    stale_source = int(stats.get("stale_source") or 0)
    active = [
        job for job in jobs
        if job.get("kind") in {"translate", "refine"} and job.get("status") in _TRANSLATE_ACTIVE_STATES
    ]
    if not project.get("has_document"):
        state = "none"
    elif active:
        state = "running"
    elif total == 0:
        state = "none"
    elif done >= total and stale_source == 0 and failed == 0:
        state = "done"
    elif done == 0 and failed > 0 and stale_source == 0:
        state = "failed"
    else:
        state = "partial"
    if active:
        progress: float | None = active[0].get("progress")
    elif total:
        progress = round(done / total, 4)
    else:
        progress = None
    return {
        "state": state,
        "done": done,
        "total": total,
        "failed": failed,
        "stale": stale,
        "stale_source": stale_source,
        "percent": float(stats.get("percent") or 0.0),
        "progress": progress,
        "locale": stats.get("locale") or project.get("target_language") or "zh-CN",
        "glossary_frozen": bool(stats.get("glossary_frozen")),
    }


def project_pipeline(store: ProjectStore, project: dict[str, Any], jobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """聚合「提取 / 结构校对 / 翻译 / 导出」四条独立工作流状态，供文件资源卡片直接渲染。

    提取状态仍由 `/ocr/jobs` 提供（前端已在轮询）；这里只补项目库里没有的
    校对待确认数与翻译运行态，避免把项目错误归纳成单一 state。
    """
    project_id = str(project.get("project_id") or "")
    if jobs is None:
        # 列表视图只需要 kind/status/progress；不要用 list_jobs 解码 result/activity 审计
        # （单项目审计可达 1 MB+，而 /projects 每 3 秒被轮询一次）。
        jobs = store.list_job_summaries(project_id)
    if project.get("has_document"):
        try:
            review = store.structure_review_summary(project_id)
        except StorageError:
            review = {"state": "todo", "pending": 0}
    else:
        review = {"state": "todo", "pending": 0}
    return {
        "review": review,
        "translate": translate_stage(project, jobs),
    }


class JobManager:
    def __init__(self, store: ProjectStore, ocr: MinerUService, models: ModelServiceStore | None = None) -> None:
        self.store = store
        self.ocr = ocr
        self.models = models or ModelServiceStore()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="literature-job")
        # 跨模块共用的请求协调层：服务级并发/退避与项目预算预留。已花金额由它维护，
        # 首次用时从持久账本 seed（进程内唯一来源，避免各 service 缓存过期标量）。
        self.coordinator = RequestCoordinator(
            spent_loader=lambda pid: float(store.usage_summary(pid)["estimated_cost"] or 0),
        )
        self.pause_events: dict[str, threading.Event] = {}
        self.ocr_running: set[str] = set()
        self.ocr_lock = threading.Lock()
        self._start_locks: dict[str, threading.Lock] = {}
        self._start_locks_guard = threading.Lock()

    def _project_start_lock(self, project_id: str) -> threading.Lock:
        """项目级“领取启动”锁：把“检查是否已有活动任务 + 创建/恢复作业”变成一个原子操作。

        否则两个并发请求都可能先读到“没有活动翻译任务”，再各自创建作业，导致同一项目
        重复调用模型、重复计费，并在同一译文层上读改写（R01）。
        """
        with self._start_locks_guard:
            lock = self._start_locks.get(project_id)
            if lock is None:
                lock = threading.Lock()
                self._start_locks[project_id] = lock
            return lock

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
        # 与翻译启动同理：检查活动任务与创建/提交必须在同一临界区内完成，否则并发/双击
        # 会为同一项目创建多个结构 AI 作业，重复调用模型并重复计费（R01 同类）。
        with self._project_start_lock(project_id):
            return self._claim_structure_job(project_id, request)

    def _claim_structure_job(self, project_id: str, request: "StructureJobInput") -> str:
        if not request.extract_toc and not request.proofread:
            raise ProviderError("至少需要勾选“提取目录”或“校对标题”中的一项。")
        if request.reuse_job_id:
            previous = self.store.get_job(project_id, request.reuse_job_id)
            if previous["kind"] != "structure_ai" or previous["status"] != "failed":
                raise StorageError("只能复用当前项目中失败的结构校对任务。")
        active_structure = [
            job for job in self.store.list_jobs(project_id)
            if job.get("kind") == "structure_ai" and job.get("status") in {"queued", "running"}
        ]
        if active_structure:
            raise StorageError("已有结构校对任务正在运行；请等待完成后再启动新的校对。")
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
        # 结构请求同样进入公共协调层（服务并发/退避 + 项目预算，按每次 attempt 预留）。
        structure_budget = float(self.store.load_manifest(project_id).get("budget_usd") or 0)
        self.coordinator.attempt_hooks_for(
            service_id, provider, project_id=project_id, budget=structure_budget,
            cost_of=prompt_cost_estimator(provider.config) if hasattr(provider, "config") else None,
        )
        # 让结构响应在写 usage 后逐请求对账（不是作业结束用项目总额 delta）。
        try:
            provider._coordinator = self.coordinator
            provider._budget_project = project_id
        except Exception:
            pass

        def run() -> None:
            try:
                run_structure_job(self.store, project_id, job_id, config_value, provider)
            except Exception:
                logger.exception("Structure job %s failed", job_id)
            finally:
                try:
                    provider.close()
                except Exception:
                    logger.exception("Provider close failed for structure job %s", job_id)
                self.refresh_translation_summary(project_id)

        try:
            self.executor.submit(run)
        except Exception as error:
            # 线程池不可用（例如已关闭）：任务不能卡在 queued（既无 worker 执行，又会被 resume 以“正在运行”拒绝）。
            self.store.update_job(project_id, job_id, status="failed", error=f"无法提交作业线程：{error}")
            raise StorageError(f"无法提交作业线程：{error}") from error
        return job_id

    def start_translation(self, project_id: str, request: TranslationJobInput, *, existing_job_id: str | None = None) -> str:
        # “检查活动任务”和“创建/恢复任务”必须在同一临界区内完成，否则并发启动会各建一个作业。
        with self._project_start_lock(project_id):
            return self._claim_translation_job(project_id, request, existing_job_id=existing_job_id)

    def _claim_translation_job(
        self, project_id: str, request: TranslationJobInput, *, existing_job_id: str | None = None,
    ) -> str:
        # 同一项目同时只允许一个翻译/润色作业：否则两个线程会在同一译文层上读改写，
        # 既会重复产生模型费用，也可能互相覆盖 entries。
        if existing_job_id:
            previous = self.store.get_job(project_id, existing_job_id)
            if previous.get("status") in {"queued", "running"}:
                raise StorageError("该翻译任务正在运行中，无需重复恢复。")
        active = [
            job for job in self.store.list_jobs(project_id)
            if job.get("kind") in {"translate", "refine"} and job.get("status") in {"queued", "running"}
            and job.get("job_id") != existing_job_id
        ]
        if active:
            raise StorageError("已有翻译任务正在运行；请等待完成，或先暂停后再启动新的范围。")
        scope = {"scope": request.scope, "block_ids": request.block_ids, "locale": request.locale}
        config_value = request.model_dump()
        job_id = existing_job_id or self.store.create_job(project_id, "refine" if request.refine else "translate", scope, config_value)
        if existing_job_id:
            self.store.update_job(project_id, job_id, status="queued", error="")
        pause = threading.Event()
        self.pause_events[job_id] = pause

        def run() -> None:
            provider = None
            try:
                provider = self.provider_from(request.service_id, request.provider)
                # 翻译请求的读超时可按作业配置（冻结快照不含超时，恢复沿用创建时的值）。
                # 测试/自定义 provider 可能不实现该方法，缺省时保持原超时。
                if hasattr(provider, "with_read_timeout"):
                    provider = provider.with_read_timeout(float(request.request_timeout_seconds))
                # 翻译是确定性任务：reasoning 对译文质量近乎零贡献却占掉大部分输出延迟
                # （实测思考型模型 reasoning 占输出 85–90%）。默认按所声明协议关思考；
                # 用户显式要求开思考（开关=on / effort=high）时 provider 内部会尊重原配置。
                if hasattr(provider, "with_thinking_disabled"):
                    provider = provider.with_thinking_disabled()
                # 用实际 resolved 的 service_id 做限流键，避免空串把多个默认服务混在一起。
                resolved_service = request.service_id or (self.models.roles().get(ROLE_TRANSLATION, "") if self.models else "")
                # AIMD 需要能探速到用户设定的并发上限：服务级信号量只升不降到 max(默认, workers)，
                # 作为跨作业共享同一服务的总量上限；单次作业内部由 AIMD 自适应控制。
                # 只升不降：避免另一本书的运行中作业额度被压小（信号量重建会让在途持有者与
                # 新信号量脱钩，短暂超发，因此不降）。
                try:
                    self.coordinator.raise_service_limit(resolved_service or "default", request.parallel_workers)
                except Exception:
                    pass
                service = TranslationService(
                    self.store, provider, coordinator=self.coordinator, service_id=resolved_service or "default",
                )
                service.run(
                    project_id, block_ids=request.block_ids, locale=request.locale, glossary=request.glossary,
                    refine=request.refine, force=request.force, instructions=request.instructions,
                    scope=request.scope, job_id=job_id, should_pause=pause.is_set,
                    polish=request.polish, parallel_workers=request.parallel_workers,
                )
                self._persist_translation_summary(project_id, request.locale)
            except Exception as error:
                self.store.update_job(project_id, job_id, status="failed", error=str(error))
                try:
                    self._persist_translation_summary(project_id, request.locale)
                except Exception:
                    logger.exception("Translation summary refresh failed for %s", project_id)
            finally:
                # 作业结束后显式释放 HTTP 连接池，并清掉暂停句柄，避免长期驻留任务对象。
                if provider is not None:
                    try:
                        provider.close()
                    except Exception:
                        logger.exception("Provider close failed for %s", project_id)
                self.pause_events.pop(job_id, None)

        try:
            self.executor.submit(run)
        except Exception as error:
            # 线程池不可用（例如已关闭）：任务不能卡在 queued（既无 worker 执行，又会被 resume 以“正在运行”拒绝）。
            self.pause_events.pop(job_id, None)
            self.store.update_job(project_id, job_id, status="failed", error=f"无法提交作业线程：{error}")
            raise StorageError(f"无法提交作业线程：{error}") from error
        return job_id

    def _persist_translation_summary(self, project_id: str, locale: str) -> None:
        document = self.store.load_document(project_id)
        layer = self.store.load_translation(project_id, locale)
        self.store.save_translation_summary(project_id, locale, translation_stats(document, layer))

    def refresh_translation_summary(self, project_id: str) -> None:
        """结构变更后刷新项目列表的译文摘要，让卡片能立即看到 stale 待复核数。

        纯层级/类型对齐（structure stale）在这里直接写回依据版本，不需要用户额外跑一个
        翻译作业；只有“原文内容已变”（source stale）才需要人工复核。没有译文层时不凭空创建。
        """
        try:
            manifest = self.store.load_manifest(project_id)
            locale = str(manifest.get("target_language") or "zh-CN")
            if not self.store.translation_path(project_id, locale).is_file():
                return
            lock = self.store.translation_lock(project_id, locale)
            with lock:
                document = self.store.load_document(project_id)
                layer = self.store.load_translation(project_id, locale)
                if align_structure_only(document, layer):
                    self.store.save_translation(layer)
                self.store.save_translation_summary(project_id, locale, translation_stats(document, layer))
        except Exception:
            logger.exception("Translation summary refresh failed for %s", project_id)

    def pause(self, job_id: str) -> None:
        event = self.pause_events.get(job_id)
        if event:
            event.set()

    def start_ocr(self, job_id: str) -> str:
        """把 job 入队并唤醒调度器（原子；委托给 MinerUService.start_job）。"""
        return self.ocr.start_job(job_id)

    def recover_interrupted_jobs(self) -> int:
        """把上次进程遗留、无法自动续跑的项目作业标为失败。

        结构、翻译与即时翻译作业运行在本地线程里，应用退出后必然已经中断；
        若不清理，界面恢复逻辑会把仍标记为 queued/running 的僵尸任务当作
        进行中任务无限重连。MinerU 作业使用独立存储并由 resume_ocr_jobs
        依据远端状态续跑，不在这里处理。
        """
        recovered = 0
        for project in self.store.list_projects():
            project_id = project.get("project_id")
            if not project_id:
                continue
            for job in self.store.list_jobs(project_id):
                if job.get("status") not in {"queued", "running"}:
                    continue
                self.store.update_job(
                    project_id, job["job_id"], status="failed",
                    error="应用上次退出时该任务被中断，请重新运行。",
                    message="任务被中断，请重新运行。",
                )
                recovered += 1
        # MinerU 解析作业单独存储：上次退出时的活动任务落为可恢复的 interrupted，
        # 保留切片进度并继续留在队列中（由 resume_ocr_jobs 按序唤醒）。
        try:
            recovered += self.ocr.recover_interrupted()
        except Exception:
            logger.exception("Failed to recover interrupted MinerU jobs")
        return recovered

    def resume_ocr_jobs(self) -> int:
        if not get_mineru_token():
            return 0
        return self.ocr.resume_all()

    def shutdown(self) -> None:
        self.ocr.stop_scheduler()
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
    # 追加解析合并后刷新翻译摘要，保证新页计入总数、老条目保留。
    ocr.on_document_changed = manager.refresh_translation_summary
    uploads = UploadStore(application_support() / "uploads")
    token = session_token()
    @asynccontextmanager
    async def lifespan(_app: Any):
        manager.recover_interrupted_jobs()
        manager.resume_ocr_jobs()
        try:
            ocr.auto_archive()
        except Exception:
            logger.exception("Startup auto archive failed")
        ocr.start_scheduler()
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
            "capabilities": ["intake_items_v1", "structure_review_v2", "model_services_v1", "structure_job_v1", "structure_vision_v1", "desktop_resources_v1", "translation_module_v1", "translation_engine_v2", "resource_pipeline_v1", "translation_safety_v1", "parse_lifecycle_v1", "task_history_v1", "parse_backends_v1"],
        }

    @app.get("/parse/backends")
    def parse_backends():
        # 只读能力清单：remote 已实现可用；local 仅预留且明确 unavailable，不提供伪入口。
        return {"backends": describe_backends()}

    @app.get("/desktop/resources")
    def desktop_resources():
        return {"library": str(store.root), "runtime": str(application_support().resolve())}

    @app.post("/intake/files/inspect")
    def inspect_local_files(request: LocalFilesInspect):
        # Read metadata only. Import creates one durable source copy after confirmation.
        files, errors = [], []
        for raw in dict.fromkeys(request.paths):
            try:
                source = validate_source(raw)
                page_count = None
                if source.suffix.casefold() == ".pdf":
                    import fitz
                    with fitz.open(source) as pdf:
                        page_count = len(pdf)
                files.append({"path": str(source), "name": source.name, "size": source.stat().st_size, "page_count": page_count})
            except (ValueError, OSError, RuntimeError) as error:
                errors.append({"path": raw, "message": str(error)})
        return {"files": files, "errors": errors}

    @app.get("/projects")
    def projects():
        values = store.list_projects()
        for value in values:
            value["pipeline"] = project_pipeline(store, value)
            value["review"] = value["pipeline"]["review"]
            value["parse_coverage"] = ocr.project_coverage(value["project_id"])
        return values

    @app.post("/projects")
    def create_project(request: ProjectCreate):
        source = request.source_file or request.source_pdf
        if not source:
            raise StorageError("请选择源文件。")
        return store.create_project(source, title=request.title, source_language=request.source_language, target_language=request.target_language)

    @app.get("/projects/{project_id}/status")
    def project_status(project_id: str):
        """轻量状态：只投影作业状态字段（不读/不解码 result/activity 审计）与用量，供前端高频轮询。"""
        jobs = store.list_job_summaries(project_id)
        return {"project_id": project_id, "jobs": jobs, "usage": store.usage_summary(project_id)}

    @app.get("/projects/{project_id}")
    def project(project_id: str):
        value = store.describe_project(project_id)
        jobs = store.list_jobs(project_id)
        pipeline = project_pipeline(store, value, jobs)
        return {
            **value,
            "jobs": jobs,
            "usage": store.usage_summary(project_id),
            "review": pipeline["review"],
            "pipeline": pipeline,
            "parse_coverage": ocr.project_coverage(project_id),
        }

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
        active_ocr_states = {"queued", "preparing", "uploading", "processing", "downloading", "merging", "waiting", "interrupted"}
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
        return ocr.create(project_id, enforce_single=True, **request.model_dump())

    @app.post("/projects/{project_id}/ocr/start")
    def start_project_parse(project_id: str, request: ParseStart):
        # Token 校验、活动作业/文档/源文件校验、创建与调度都在服务层的锁内完成；
        # 无 Token 时不创建任何任务，创建成功但调度失败会报出具体错误与可恢复状态。
        return ocr.start_parse(project_id, **request.model_dump())

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

    @app.get("/ocr/tasks")
    def ocr_task_center(
        scope: str = "active", status: str = "", query: str = "",
        sort: str = "recent", page: int = 1, page_size: int = 0,
    ):
        return ocr.task_center(
            scope=scope, status=status, query=query, sort=sort, page=page, page_size=page_size,
        )

    @app.get("/ocr/jobs/{job_id}")
    def get_ocr_job(job_id: str):
        if not ocr.job_exists(job_id):
            raise HTTPException(status_code=404, detail="解析任务不存在。")
        return ocr.get(job_id)

    @app.post("/ocr/jobs/{job_id}/archive")
    def archive_ocr_job(job_id: str):
        if not ocr.job_exists(job_id):
            raise HTTPException(status_code=404, detail="解析任务不存在。")
        return ocr.archive_job(job_id, reason="manual", auto=False)

    @app.post("/ocr/jobs/{job_id}/unarchive")
    def unarchive_ocr_job(job_id: str):
        if not ocr.job_exists(job_id):
            raise HTTPException(status_code=404, detail="解析任务不存在。")
        return ocr.unarchive_job(job_id)

    @app.get("/ocr/archive-settings")
    def get_archive_settings():
        return ocr.archive_settings()

    @app.put("/ocr/archive-settings")
    def put_archive_settings(request: ArchiveSettingsInput):
        try:
            return ocr.set_archive_settings(
                retention_days=request.retention_days, max_records=request.max_records,
            )
        except ValueError as error:
            raise StorageError(str(error)) from error

    @app.get("/ocr/queue")
    def parse_queue_status():
        return ocr.queue_snapshot()

    @app.post("/ocr/jobs/{job_id}/start")
    def start_ocr_job(job_id: str):
        # 先检查存在性：不存在的任务统一返回 404，与是否配置 Token 无关。
        if not ocr.job_exists(job_id):
            raise HTTPException(status_code=404, detail="解析任务不存在。")
        if not get_mineru_token():
            raise StorageError("请先配置 MinerU Token。")
        try:
            result = ocr.start_job(job_id)
        except (StorageError, WorkflowError) as error:
            raise HTTPException(status_code=409, detail=str(error))
        if result == "already_running":
            raise HTTPException(status_code=409, detail="该解析任务正在执行，无需重复启动。")
        return {"job_id": job_id, "state": "queued"}

    @app.post("/ocr/jobs/{job_id}/queue")
    def move_ocr_job(job_id: str, request: ParseQueueMove):
        if not ocr.job_exists(job_id):
            raise HTTPException(status_code=404, detail="解析任务不存在。")
        try:
            position = ocr.reorder_job(job_id, request.position)
        except (StorageError, WorkflowError) as error:
            raise HTTPException(status_code=409, detail=str(error))
        return {"job_id": job_id, "position": position, "queue": ocr.queue_snapshot()}

    @app.post("/ocr/jobs/{job_id}/retry")
    def retry_ocr_job(job_id: str):
        if not ocr.job_exists(job_id):
            raise HTTPException(status_code=404, detail="解析任务不存在。")
        if not get_mineru_token():
            raise StorageError("请先配置 MinerU Token。")
        try:
            return ocr.retry_job(job_id)
        except (StorageError, WorkflowError) as error:
            raise HTTPException(status_code=409, detail=str(error))

    @app.post("/projects/{project_id}/parse/migrate")
    def migrate_parse_coverage(project_id: str):
        return ocr.migrate_project(project_id)

    @app.post("/projects/{project_id}/ocr/continue")
    def continue_ocr_job(project_id: str, request: ParseContinue):
        if not get_mineru_token():
            raise StorageError("请先配置 MinerU Token。")
        job, skipped = ocr.continue_parse(
            project_id, page_ranges=request.page_ranges, language=request.language,
            model_version=request.model_version, page_limit=request.page_limit,
            extra_formats=request.extra_formats or None,
        )
        ocr.start_job(job["job_id"])
        return {"job": ocr.get(job["job_id"]), "skipped_pages": skipped}

    @app.get("/projects/{project_id}/ocr/jobs")
    def list_ocr_jobs(project_id: str):
        return ocr.list(project_id)

    @app.post("/projects/{project_id}/structure/analyze")
    def structure_analyze(project_id: str, request: StructureAnalyze):
        document = enrich_furniture_evidence(store.load_document(project_id), store.project_path(project_id))
        local = local_printed_toc(document, pages=request.toc_page_indexes or None) if not request.toc else {"entries": [], "pages": []}
        baseline = request.toc or local["entries"]
        reconciliation = reconcile_toc(document, baseline, excluded_pages=set(request.toc_page_indexes or local["pages"]) or None)
        suggestions = reconciliation["suggestions"]
        for value in suggestions:
            value["document_updated_at"] = document.updated_at
        job_id = store.create_job(project_id, "structure_local", {}, request.model_dump())
        store.save_structure_suggestions(project_id, job_id, suggestions, document.updated_at)
        applied = [asdict(value) for value in apply_automatic(store, project_id, suggestions)] if request.auto_apply else []
        applied_ids = {item["block_id"] for item in applied}
        for value in suggestions:
            value["automatic"] = value["block_id"] in applied_ids
        if applied_ids:
            store.decide_structure_suggestions(project_id, [item["suggestion_id"] for item in suggestions if item["block_id"] in applied_ids], "applied")
        result = {"suggestions": suggestions, "applied": applied, "proposed_toc": local["entries"], "reconciliation": reconciliation["summary"], "job_id": job_id}
        store.update_job(project_id, job_id, status="completed", progress=1, result=result)
        manager.refresh_translation_summary(project_id)
        return result

    @app.get("/projects/{project_id}/structure/toc-page-candidates")
    def structure_toc_candidates(project_id: str):
        return find_toc_page_candidates(store.load_document(project_id))

    @app.get("/projects/{project_id}/structure/local-toc")
    def structure_local_toc(project_id: str, pages: str = ""):
        # 不调用模型的印刷目录抽取：无目录页参数时自动定位并抽取。
        requested = [int(value) for value in re.findall(r"\d+", pages)] if pages.strip() else None
        return local_printed_toc(store.load_document(project_id), pages=requested)

    @app.post("/projects/{project_id}/structure/audit-payload")
    def structure_audit_payload(project_id: str, request: StructureAnalyze):
        # v1 审计：只包含目录文本与标题候选，不含任何正文/目录页文本。
        return structure_model_payload(store.load_document(project_id), request.toc)

    @app.post("/projects/{project_id}/structure/plan")
    def structure_plan(project_id: str, request: StructurePlanInput):
        # v2 审计：与异步作业完全一致的输入，包含将要发送的目录页文本。
        return plan_structure_analysis(
            enrich_furniture_evidence(store.load_document(project_id), store.project_path(project_id)), request.toc,
            extract_toc=request.extract_toc, proofread=request.proofread,
            toc_page_limit=request.toc_page_limit,
            toc_page_indexes=request.toc_page_indexes, use_vision=request.use_vision,
            source_path=store.source_path(project_id), planning_mode=request.planning_mode,
        )

    @app.get("/projects/{project_id}/structure/baseline")
    def structure_baseline_get(project_id: str):
        return store.load_structure_baseline(project_id)

    @app.put("/projects/{project_id}/structure/baseline")
    def structure_baseline_put(project_id: str, request: StructureBaselineInput):
        return store.save_structure_baseline(project_id, request.toc, request.source)

    @app.get("/projects/{project_id}/structure/suggestions")
    def structure_suggestions_list(project_id: str, decision: str = "pending"):
        return store.list_structure_suggestions(project_id, decision or None)

    @app.post("/projects/{project_id}/structure/suggestions/decide-batch")
    def structure_suggestions_decide_batch(project_id: str, request: SuggestionBatchDecisionInput):
        if request.decision == "dismissed":
            store.decide_structure_suggestions(project_id, request.suggestion_ids, "dismissed")
            return {"suggestions": [store.get_structure_suggestion(project_id, sid) for sid in request.suggestion_ids], "patches": []}
        values = [store.get_structure_suggestion(project_id, sid) for sid in request.suggestion_ids]
        # 先校验全部再一次性写入：任一项陈旧或非法则整批不提交，也不标记为已采用。
        patches = apply_patch_batch(store, project_id, [{
            "block_id": value["block_id"], "operation": value["operation"], "after": value["after"],
            "before": value["before"], "confidence": value["confidence"], "reason": value["reason"], "automatic": False,
        } for value in values], source="ai")
        store.decide_structure_suggestions(project_id, request.suggestion_ids, "applied")
        manager.refresh_translation_summary(project_id)
        return {
            "suggestions": [store.get_structure_suggestion(project_id, sid) for sid in request.suggestion_ids],
            "patches": [asdict(patch) for patch in patches],
        }

    @app.post("/projects/{project_id}/structure/suggestions/{suggestion_id}/decision")
    def structure_suggestion_decide(project_id: str, suggestion_id: str, request: SuggestionDecisionInput):
        value = store.get_structure_suggestion(project_id, suggestion_id)
        if value["decision"] != "pending":
            raise StorageError("该建议已处理。")
        patch = None
        if request.decision == "applied":
            patch = asdict(apply_patch(store, project_id, {
                "block_id": value["block_id"], "operation": value["operation"], "after": value["after"],
                "before": value["before"], "confidence": value["confidence"], "reason": value["reason"], "automatic": False,
            }, source="ai"))
        store.decide_structure_suggestion(project_id, suggestion_id, request.decision)
        manager.refresh_translation_summary(project_id)
        return {"suggestion": store.get_structure_suggestion(project_id, suggestion_id), "patch": patch}

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
            result = run_structure_job(store, project_id, job_id,
                {**request.model_dump(), "extract_toc": False, "proofread": True}, provider)
            return {**result, "job_id": job_id}
        except Exception as error:
            store.update_job(project_id, job_id, status="failed", error=str(error))
            raise

    @app.post("/projects/{project_id}/structure/patches")
    def structure_patch(project_id: str, request: PatchInput):
        patch = asdict(apply_patch(store, project_id, request.model_dump(), source=request.source))
        manager.refresh_translation_summary(project_id)
        return patch

    @app.post("/projects/{project_id}/structure/patches/batch")
    def structure_patch_batch(project_id: str, request: PatchBatchInput):
        # 先校验全部条目，任一项非法则整批不写入，避免部分提交。
        patches = [asdict(value) for value in apply_patch_batch(
            store, project_id, [patch.model_dump() for patch in request.patches], source=request.source,
        )]
        manager.refresh_translation_summary(project_id)
        return patches

    @app.get("/projects/{project_id}/structure/patches")
    def structure_patches(project_id: str):
        return store.list_patches(project_id)

    @app.post("/projects/{project_id}/structure/confirm")
    def structure_confirm(project_id: str, scope: str = "full"):
        # 显式确认当前审查范围：绑定文档结构版本；文档改后确认自动失效。
        return store.confirm_structure_review(project_id, scope=scope)

    @app.post("/projects/{project_id}/structure/patches/{patch_id}/revert")
    def structure_revert(project_id: str, patch_id: str):
        patch = asdict(revert_patch(store, project_id, patch_id))
        manager.refresh_translation_summary(project_id)
        return patch

    @app.post("/projects/{project_id}/structure/patches/revert-batch")
    def structure_revert_batch(project_id: str, request: PatchRevertBatchInput):
        wanted = set(request.patch_ids)
        ordered = [item["patch_id"] for item in store.list_patches(project_id) if item["patch_id"] in wanted]
        if len(ordered) != len(wanted):
            raise StorageError("结构补丁不存在。")
        patches = [asdict(revert_patch(store, project_id, patch_id)) for patch_id in ordered]
        manager.refresh_translation_summary(project_id)
        return patches

    @app.get("/projects/{project_id}/glossary/draft")
    def glossary_draft(project_id: str):
        return draft_glossary(store.load_document(project_id))

    @app.put("/projects/{project_id}/translations/{locale}/glossary")
    def glossary_freeze(project_id: str, locale: str, request: GlossaryInput):
        lock = store.translation_lock(project_id, locale)
        with lock:
            layer = store.load_translation(project_id, locale)
            layer.glossary_snapshot = {
                str(source).strip(): str(target).strip()
                for source, target in request.entries.items() if str(source).strip()
            }
            store.save_translation(layer)
            record = store.save_glossary_version(project_id, locale, layer.glossary_snapshot, source="freeze")
        return {"entries": layer.glossary_snapshot, "updated_at": layer.updated_at, "version": record["version"]}

    @app.get("/projects/{project_id}/translations/{locale}/glossary/versions")
    def glossary_versions(project_id: str, locale: str):
        return store.list_glossary_versions(project_id, locale)

    @app.post("/projects/{project_id}/translations/{locale}/glossary/rollback")
    def glossary_rollback(project_id: str, locale: str, request: GlossaryRollbackInput):
        record = store.load_glossary_version(project_id, locale, request.version)
        lock = store.translation_lock(project_id, locale)
        with lock:
            layer = store.load_translation(project_id, locale)
            layer.glossary_snapshot = dict(record["entries"])
            store.save_translation(layer)
            # 回滚也记为新版本，保留完整的不可变历史链，而不是改写旧版本。
            created = store.save_glossary_version(project_id, locale, layer.glossary_snapshot, source=f"rollback:{request.version}")
        return {"entries": layer.glossary_snapshot, "updated_at": layer.updated_at, "version": created["version"]}

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

    @app.post("/model-services/{service_id}/models")
    def model_service_models(service_id: str):
        models.get(service_id)  # 校验存在
        return {"models": models.list_models(service_id)}

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
    def translation_layer(project_id: str, locale: str, since: str = ""):
        layer = store.load_translation(project_id, locale)
        # revision 必须覆盖源文/结构版本：用 document.json 的 mtime+size 作为源指纹（廉价），
        # 加上 locale、译文版本与协议版本；任何一项变化都使 since 失效。
        document_path = store.project_path(project_id) / "document" / "document.json"
        try:
            stat = document_path.stat()
            document_fingerprint = f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            document_fingerprint = ""
        revision = f"{locale}|{layer.updated_at}|{document_fingerprint}|{REVISION_PROTOCOL}"
        if since and since == revision:
            return {"unchanged": True, "revision": revision}
        document = store.load_document(project_id)
        payload = translation_payload(document, layer, revision=revision)
        store.save_translation_summary(project_id, locale, payload["stats"])
        return payload

    @app.post("/projects/{project_id}/translation/estimate")
    def translation_estimate(project_id: str, request: TranslationEstimateInput):
        document = store.load_document(project_id)
        layer = store.load_translation(project_id, request.locale)
        config = ProviderConfig(provider="estimate", model="", base_url="")
        try:
            service_id = manager.service_id_for(ROLE_TRANSLATION, request.service_id)
            config = manager.provider_from(service_id).config
        except ProviderError:
            pass
        result = estimate_translation(
            document, layer, scope=request.scope, block_ids=request.block_ids, refine=request.refine, config=config,
            force=request.force, glossary=request.glossary, polish=request.polish,
            instructions=request.instructions, locale=request.locale,
        )
        return result

    @app.post("/projects/{project_id}/translations/{block_id}/review")
    def translation_review(project_id: str, block_id: str, request: ReviewInput):
        service_id = request.service_id or (manager.models.roles().get(ROLE_TRANSLATION, "") if manager.models else "") or "default"
        provider = manager.provider_from(request.service_id, None)
        # 单块译审同样是翻译任务：默认按所声明协议关思考（与整本书翻译一致）。
        if hasattr(provider, "with_thinking_disabled"):
            provider = provider.with_thinking_disabled()
        manager.coordinator.attempt_hooks_for(
            service_id, provider, project_id=project_id,
            budget=float(store.load_manifest(project_id).get("budget_usd") or 0),
            cost_of=prompt_cost_estimator(provider.config),
        )
        service = TranslationService(store, provider, coordinator=manager.coordinator, service_id=service_id)
        try:
            return service.review(project_id, block_id, locale=request.locale, instructions=request.instructions)
        except BudgetExhausted:
            raise HTTPException(status_code=409, detail="项目预算不足，已停止定向译审。")
        finally:
            try:
                provider.close()
            except Exception:
                logger.exception("Provider close failed for review %s", block_id)

    @app.post("/projects/{project_id}/translations/{block_id}/review/adopt")
    def translation_review_adopt(project_id: str, block_id: str, request: ReviewAdoptInput):
        # 采纳只需要写库（不调模型/不读密钥）；在写锁内校验源与译文基线。
        service = TranslationService(store, None, coordinator=manager.coordinator)
        try:
            entry = service.adopt_review(
                project_id, block_id, locale=request.locale, revised=request.revised,
                source_hash=request.source_hash, base_revision=request.base_revision,
                candidate_id=request.candidate_id,
            )
        except StorageError as error:
            # 源/译文基线不符：409，保留最新人工内容供重新审查。
            raise HTTPException(status_code=409, detail=str(error))
        manager.refresh_translation_summary(project_id)
        return {"entry": asdict(entry)}

    @app.post("/projects/{project_id}/translate/instant")
    def translate_instant(project_id: str, request: InstantInput):
        service_id = request.service_id or (manager.models.roles().get(ROLE_TRANSLATION, "") if manager.models else "") or "default"
        provider = manager.provider_from(request.service_id, request.provider)
        # 即时翻译同样是翻译任务：默认按所声明协议关思考。
        if hasattr(provider, "with_thinking_disabled"):
            provider = provider.with_thinking_disabled()
        manager.coordinator.attempt_hooks_for(
            service_id, provider, project_id=project_id,
            budget=float(store.load_manifest(project_id).get("budget_usd") or 0),
            cost_of=prompt_cost_estimator(provider.config),
        )
        service = TranslationService(store, provider, coordinator=manager.coordinator, service_id=service_id)
        try:
            return service.instant(project_id, request.text, locale=request.locale, block_id=request.block_id, save=request.save)
        except BudgetExhausted:
            raise HTTPException(status_code=409, detail="项目预算不足，已停止即时翻译。")
        finally:
            try:
                provider.close()
            except Exception:
                logger.exception("Provider close failed for instant")

    @app.put("/projects/{project_id}/translations/{block_id}")
    def translation_edit(project_id: str, block_id: str, request: TranslationEdit):
        # Provider is irrelevant for a manual edit.
        from .providers import MockProvider
        entry = TranslationService(store, MockProvider()).edit(
            project_id, block_id, request.text, locale=request.locale,
            cells=request.cells, items=request.items, caption=request.caption, footnote=request.footnote,
        )
        manager._persist_translation_summary(project_id, request.locale)
        return asdict(entry)

    @app.post("/projects/{project_id}/translations/{block_id}/unlock")
    def translation_unlock(project_id: str, block_id: str, locale: str = "zh-CN"):
        from .providers import MockProvider
        entry = TranslationService(store, MockProvider()).unlock(project_id, block_id, locale=locale)
        manager._persist_translation_summary(project_id, locale)
        return asdict(entry)

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
    def exports(project_id: str):
        # 导出模块已整体移除；保留路径返回 410，让旧前端/旧书签得到明确提示而不是裸 404。
        raise HTTPException(status_code=410, detail="Literature 已移除内置导出；原文/解析证据/译文仍保留在项目中。")

    @app.get("/projects/{project_id}/export/readiness")
    def export_readiness_endpoint(project_id: str, locale: str = "zh-CN"):
        raise HTTPException(status_code=410, detail="Literature 已移除内置导出与完整性预检。")

    @app.post("/projects/{project_id}/tbl/prepare")
    def tbl_prepare(project_id: str):
        raise HTTPException(status_code=410, detail="Literature 已移除外部 TBL 接力。")

    @app.post("/tbl/start")
    def tbl_start():
        raise HTTPException(status_code=410, detail="Literature 已移除外部 TBL 接力。")

    @app.get("/tbl/config")
    def tbl_config():
        raise HTTPException(status_code=410, detail="Literature 已移除外部 TBL 接力。")

    @app.put("/tbl/config")
    def tbl_configure():
        raise HTTPException(status_code=410, detail="Literature 已移除外部 TBL 接力。")

    @app.get("/tbl/status")
    def tbl_status():
        raise HTTPException(status_code=410, detail="Literature 已移除外部 TBL 接力。")

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
    # 修正 NO_PROXY 中 httpx 无法解析的条目，避免模型与 MinerU 请求因代理环境直接失败。
    normalize_proxy_environment()
    # A frozen sidecar has its own bootloader process. Also watch the actual App
    # so SIGKILL/crashes cannot leave a backend polling forever.
    parent = os.environ.get("LITERATURE_PARENT_PID", "")
    if parent:
        def watch_parent():
            import time
            parent_pid = int(parent)
            while True:
                time.sleep(1)
                try:
                    os.kill(parent_pid, 0)
                except ProcessLookupError:
                    os._exit(0)
        threading.Thread(target=watch_parent, daemon=True).start()
    # Pass the object directly so a frozen Python sidecar does not need to
    # rediscover the package through an import string at runtime.
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("LITERATURE_PORT", "41731")), reload=False)


if __name__ == "__main__":
    main()
