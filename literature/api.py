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
from typing import Any, Callable, Literal

from fastapi import File, UploadFile
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .exporters import create_export, delete_export, export_options, list_exports, rename_export, resolve_export_file
from .flow import detect_cross_page_flow
from .mineru import detect_language, import_mineru
from .mineru_capabilities import MAX_BATCH_FILES, validate_source
from .mineru_workflow import MinerUAPIError, MinerUClient, WorkflowError
from .mineru_local import EngineBusyError, local_engine_ready, shared_engine
from .model_services import (
    MODEL_ROLES, ROLE_READER_CHAT, ROLE_READER_FOLD, ROLE_STRUCTURE, ROLE_TRANSLATION,
    ModelServiceStore, save_service_key,
)
from .models import UsageRecord
from .reader import (
    ReaderChatRunner, ReaderConflict, ReaderJobRunner, ReaderLayerService, ReaderStore,
    answer_question, build_sections, clear_layers, layer_state, layers_markdown,
    local_fold_section, summarize_reader, usage_recorder,
)
from .ocr import MinerUService, delete_mineru_token, get_mineru_token, save_mineru_token
from .parse_backends import LOCAL_BACKEND_ID, describe_backends, resolve_job_backend
from .providers import PROVIDER_PRESETS, NO_REASONING_STYLE, PROBE_MAX_TOKENS, OpenAICompatibleProvider, ProviderConfig, ProviderError, apply_reasoning_mode, delete_api_key, disabled_reasoning_effort, get_api_key, normalize_proxy_environment, normalize_tls_environment, probe_thinking_off, resolve_reasoning_style, save_api_key
from .request_coordinator import BudgetExhausted, RequestCoordinator, ServiceBusyError, prompt_cost_estimator
from .runtime_revision import RUNTIME_REVISION
from .storage import ProjectStore, StorageError
from .structure_signals import enrich_furniture_evidence
from .structure import analyze_structure, apply_automatic, apply_patch, apply_patch_batch, find_toc_page_candidates, local_printed_toc, plan_structure_analysis, reconcile_toc, revert_patch, run_structure_job, structure_mode_config, structure_model_payload
from .translation import TranslationService, align_structure_only, draft_glossary, estimate_translation, translation_payload, translation_stats, REVISION_PROTOCOL
from .uploads import UploadStore


logger = logging.getLogger(__name__)

# 探测请求在“价格未知”时的保守预算占额：仅用于避免“未知= 0 免费”地逐次放行，
# 不是对真实费用的上限承诺。有限预算下遇到一次不可量化费用后会直接停止后续探测。
PROBE_UNKNOWN_COST_USD = 0.001


def probe_prompt_cost_estimator(config: Any) -> Callable[[int], float]:
    """探测专用的保守成本估算：输出按**子 provider** 的 max_output_tokens（探测已缩为
    PROBE_MAX_TOKENS）计，而不是父 provider 的全输出上限。价格未知时返回 0
    （由调用方决定是否用保守占额，不能用 0 冒充“免费”）。
    """
    input_price = getattr(config, "input_price_per_million", None)
    output_price = getattr(config, "output_price_per_million", None)
    max_output = max(1, int(getattr(config, "max_output_tokens", 0) or PROBE_MAX_TOKENS))

    def estimate(prompt_chars: int) -> float:
        if input_price is None or output_price is None:
            return 0.0
        input_tokens = prompt_chars // 2 + 200
        return (input_tokens * input_price + max_output * output_price) / 1_000_000
    return estimate


def probe_cost_is_unknown(response: Any, error: Exception | None) -> bool:
    """本次探测是否“费用无法量化”（需要记未知/阻止继续探测）。

    - 有 usage（token>0）且算出 estimated_cost → 已知。
    - 有 usage 但无单价（estimated_cost=None）→ 未知。
    - **无 usage（token=0）→ 未知**：即使有单价也会算出 0，但那是“没量到”，不能当零成本。
    - 无响应：显式 possible_charge 优先；否则 status=0 或非 4xx（除 408）保守按可能计费；
      4xx（除 408）确定不计费。
    """
    if response is not None:
        tokens = int(getattr(response, "input_tokens", 0) or 0) + int(getattr(response, "output_tokens", 0) or 0)
        if tokens <= 0:
            return True
        return getattr(response, "estimated_cost", None) is None
    if error is None:
        return True
    explicit = getattr(error, "possible_charge", None)
    if explicit is not None:
        return bool(explicit)
    status = int(getattr(error, "status_code", 0) or 0)
    return not (400 <= status < 500 and status != 408)


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


class ExportRequest(BaseModel):
    formats: list[Literal["epub", "docx", "markdown", "html"]] = Field(default_factory=lambda: ["epub"])
    mode: Literal["source", "translation", "bilingual"] = "translation"
    locale: str = "zh-CN"
    chapters: list[str] = Field(default_factory=list)
    block_ids: list[str] = Field(default_factory=list)
    filename: str = Field(default="", max_length=240)
    missing_policy: Literal["fallback_source"] = "fallback_source"


class ExportRenameRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)


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
    # 解析后端：mineru_remote（默认，云端）或 mineru_local（本地引擎，需已安装）。
    backend_id: Literal["mineru_remote", "mineru_local"] = "mineru_remote"


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
    backend_id: Literal["mineru_remote", "mineru_local", ""] = ""


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
    backend_id: Literal["mineru_remote", "mineru_local"] = "mineru_remote"


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


class EngineInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["install"] = "install"
    # force=True 为「修复/重装」：已就绪时也重跑幂等安装步骤；与解析任务互斥。
    force: bool = False


class EngineActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["cancel", "uninstall"]


class EngineHttpKeyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    http_url: str = Field(min_length=1, max_length=2000)
    value: str


class EngineSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["managed", "external_cli", "http_service"]
    external_dir: str = Field(default="", max_length=2000)
    http_url: str = Field(default="", max_length=2000)
    allow_plaintext_http: bool = False
    # 本次显式输入的服务密钥：连接验证成功后才按规范化地址绑定保存；
    # 验证失败不落 selection、不动已有密钥。
    http_key: str = Field(default="", max_length=2000)


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
    # 向后兼容的随请求 ProviderInput（主路径已改为模型服务库）：同样默认拦截明文 HTTP。
    allow_plaintext_http: bool = False

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
            allow_plaintext_http=self.allow_plaintext_http,
        )


# 任务级思考档位：inherit=沿用模型服务配置；其余为本次任务的覆盖。
ReasoningMode = Literal["inherit", "off", "low", "medium", "high"]


class StructureAIAnalyze(BaseModel):
    toc: list[dict[str, Any]]
    provider: ProviderInput | None = None
    service_id: str = ""
    # 服务商内的模型 ID；留空则用角色绑定/服务商默认（第一个）模型。
    model: str = ""
    auto_apply: bool = False
    # 复核模式：sample=第一遍定案 + 固定比例抽查（默认）；full=全量双遍；off=不抽查。
    # proofread_full=True 是 full 的兼容入口。
    proofread_mode: Literal["full", "sample", "off"] | None = None
    proofread_full: bool = False
    proofread_concurrency: int = Field(default=2, ge=1, le=4)
    planning_mode: Literal["global", "adaptive"] = "global"
    # 任务级思考档位（覆盖服务级配置）；见 ReasoningMode。
    reasoning_mode: ReasoningMode = "inherit"
    reasoning_budget: int = Field(default=0, ge=0, le=200000)


class StructureJobInput(BaseModel):
    service_id: str = ""
    # 服务商内的模型 ID；留空则用角色绑定/服务商默认（第一个）模型。
    model: str = ""
    # 任务级思考档位：inherit=沿用服务配置（默认）；off=本次强制关思考；
    # low/medium/high=本次设定思考强度。budget>0 时对 qwen 思考预算生效。
    reasoning_mode: ReasoningMode = "inherit"
    reasoning_budget: int = Field(default=0, ge=0, le=200000)
    toc: list[dict[str, Any]] = Field(default_factory=list)
    # 目录基准来源元数据（只读展示用）：confirmed=人工确认；auto_extract=上次校准自动提取。
    # 由 start_structure 载入已保存基准时写入；客户端无需传，不影响执行参数。
    toc_source: str = ""
    # 用户档位：coarse=粗校（只校准与目录相关的候选，默认）；fine=精校（额外未收录小节发现）。
    # 设为 None 时保持旧行为（各字段显式生效），用于兼容既有调用。
    mode: Literal["coarse", "fine"] | None = None
    # 诊断/兼容覆盖：仅在需要时显式给出，覆盖档位预置值。
    advanced: dict[str, Any] = Field(default_factory=dict)
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


class ModelEntryInput(BaseModel):
    """服务商下的一个模型（上下文/输出上限/单价/思考配置都挂在模型上）。"""
    model: str = ""
    name: str = ""
    context_window: int = 0
    max_output_tokens: int = 16384
    temperature: float = 0.2
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    reasoning_style: str = ""
    reasoning_effort: str = ""
    reasoning_budget: int = 0


class ModelServiceInput(BaseModel):
    """一个服务商（Base URL + 密钥）及其模型列表。

    `models` 为空时兼容旧的扁平载荷：顶层 `model`/单价/思考字段会被当作单个模型。
    """
    name: str = ""
    preset: str = "custom"
    base_url: str = ""
    # 用户显式确认：该地址是内网/自建服务，允许用明文 HTTP 携带 API Key。
    # 默认 False，公网 http 地址会被 providers.assert_key_transport_safe 拦截。
    allow_plaintext_http: bool = False
    models: list[ModelEntryInput] = Field(default_factory=list)
    # v1 兼容字段（仅在 models 为空时生效）。
    model: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 16384
    context_window: int = 0
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    reasoning_style: str = ""
    reasoning_effort: str = ""
    reasoning_budget: int = 0


class ModelRoleBinding(BaseModel):
    service_id: str = ""
    model: str = ""


class ModelRolesInput(BaseModel):
    # 新形态是 {service_id, model}；也接受纯字符串（v1 只绑服务），由存储层归一。
    # `extra="forbid"`：把 `{roles: {...}}` 这类错误包裹显式拒绝，而不是静默忽略后“保存成功”。
    model_config = ConfigDict(extra="forbid")

    structure: ModelRoleBinding | str | None = None
    translation: ModelRoleBinding | str | None = None
    # 阅读折叠 / 阅读对话（各自独立；缺省表示不改这一项）
    reader_fold: ModelRoleBinding | str | None = None
    reader_chat: ModelRoleBinding | str | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_roles_wrapper(cls, value: Any) -> Any:
        if isinstance(value, dict) and "roles" in value and not (set(value) & set(MODEL_ROLES)):
            raise ValueError(
                "请求体不要包一层 roles：直接传 {\"structure\": {…}, \"translation\": {…}, "
                "\"reader_fold\": {…}, \"reader_chat\": {…}}（只传要改的角色即可）。"
            )
        return value


class ModelCheckInput(BaseModel):
    model: str = ""


class ModelProbeInput(BaseModel):
    """拉取远端模型列表的输入：新建服务只需 base_url + api_key，已保存服务可只给 service_id。"""
    base_url: str = ""
    api_key: str = ""
    service_id: str = ""
    # 新建服务时随表单提交的“允许明文 HTTP”选项（已保存服务缺省用服务上的配置）。
    allow_plaintext_http: bool | None = None


class TranslationJobInput(BaseModel):
    provider: ProviderInput | None = None
    service_id: str = ""
    # 服务商内的模型 ID；留空则用角色绑定/服务商默认（第一个）模型。
    model: str = ""
    # 任务级思考档位（覆盖服务级配置）：见 StructureJobInput 的同名字段。
    reasoning_mode: ReasoningMode = "inherit"
    reasoning_budget: int = Field(default=0, ge=0, le=200000)
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
    parallel_workers: int = Field(default=8, ge=1, le=16)
    # 单次模型请求的读超时。流式下是「两个数据块之间的空闲时限」，
    # 非流式回退时才是等待整份响应的总时限；供不支持流式的中转站调大。
    request_timeout_seconds: int = Field(default=300, ge=30, le=1800)


class InstantInput(BaseModel):
    provider: ProviderInput | None = None
    service_id: str = ""
    model: str = ""
    text: str
    locale: str = "zh-CN"
    block_id: str | None = None
    save: bool = False


class ReaderQuoteInput(BaseModel):
    block_id: str = ""
    pane: str = "source"
    text: str = ""


class ReaderLayersInput(BaseModel):
    locale: str = "source"
    service_id: str = ""
    model: str = ""
    # auto：有模型就用模型折叠；local：只用本地摘句（不产生任何模型调用）。
    mode: Literal["auto", "local"] = "auto"
    force: bool = False
    section_ids: list[str] | None = None


class FoldRunInput(BaseModel):
    """整篇折叠 run 的启动参数（AIFold：默认关闭→点击后整篇处理→全部完成才 ready）。

    - locale 缺省由服务端解析：有译文=译文 locale（单一目标 controller），否则 source。
    - force=True 忽略缓存重新采样（仍走 single-flight，不双倍计费）。
    """

    model_config = ConfigDict(extra="forbid")

    locale: str = ""
    force: bool = False


class ReaderOverlayInput(BaseModel):
    overlay: dict[str, Any]
    # 乐观并发：提交时若服务端 revision 已变，拒绝覆盖（前端刷新后重试）。
    base_revision: int | None = None


class ReaderChatInput(BaseModel):
    # `extra="forbid"`：客户端/后端字段漂移必须立刻报错，而不是被静默忽略后回落到默认角色
    # （真实缺陷 D1：前端发 `binding:{...}`，pydantic 丢掉未知字段，请求实际走了默认模型）。
    model_config = ConfigDict(extra="forbid")

    question: str = Field(default="", max_length=4000)
    locale: str = "source"
    # 本次提问使用的服务商 + 模型；留空走「阅读对话」角色绑定。
    # 同一个 model id 可能出现在不同服务商下，所以两者必须一起传、一起记录。
    service_id: str = ""
    model: str = ""
    quote: ReaderQuoteInput | None = None


class ReaderChatRetryInput(BaseModel):
    locale: str = "source"
    service_id: str = ""
    model: str = ""


class ReaderAskInput(BaseModel):
    question: str = Field(default="", max_length=4000)
    locale: str = "source"
    service_id: str = ""
    model: str = ""
    quote: ReaderQuoteInput | None = None
    block_ids: list[str] | None = None


class ReviewInput(BaseModel):
    service_id: str = ""
    model: str = ""
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
        # 阅读模块的五层折叠后台作业（与解析/翻译作业并列，不共用线程池）。
        self.reader_jobs = ReaderJobRunner(store)
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

    def provider_from(self, service_id: str, provider_input: ProviderInput | None = None, model: str = "") -> OpenAICompatibleProvider:
        """优先按模型服务库解析（密钥只从 Keychain 取）；否则兼容旧的随请求 ProviderInput。

        `model` 是服务商内的模型 ID；留空则用该服务商的第一个模型。
        """
        if service_id:
            return self.models.config_for(service_id, model)
        if provider_input is not None:
            return OpenAICompatibleProvider(provider_input.config())
        raise ProviderError("尚未选择模型服务，请先在当前模块配置模型。")

    def service_id_for(self, role: str, explicit: str = "") -> str:
        if explicit:
            return explicit
        if role not in MODEL_ROLES:
            raise ProviderError(f"未知任务角色：{role}")
        resolved = self.models.roles().get(role, {}).get("service_id", "")
        if not resolved:
            raise ProviderError("尚未为当前模块绑定模型服务，请先配置模型。")
        return resolved

    def model_for(self, role: str, service_id: str, explicit: str = "") -> str:
        """解析角色/任务实际使用的模型：显式优先，其次角色绑定（仅当绑定同一服务商），
        否则交给存储层回落到服务商的第一个模型。"""
        if explicit:
            return explicit
        if role not in MODEL_ROLES:
            return ""
        binding = self.models.roles().get(role, {})
        if binding.get("service_id") == service_id:
            return str(binding.get("model") or "")
        return ""

    @staticmethod
    def _apply_task_reasoning(provider: Any, mode: str, budget: int) -> Any:
        """把任务级思考档位落到 provider 配置上；必须在 apply_thinking_policy 之前调用
        （后者会根据最终 effort 决定是否探测“关思考”字段写法）。inherit 时零改动。"""
        config = getattr(provider, "config", None)
        if config is None:
            return provider
        updated = apply_reasoning_mode(config, mode, budget)
        if updated is not config:
            provider.config = updated
        return provider

    def apply_thinking_policy(
        self, provider: Any, service_id: str, *, allow_probe: bool = False,
        on_probe: Callable[[], None] | None = None,
        on_probe_usage: Callable[[Any, Exception | None], None] | None = None,
        project_id: str = "", budget: float = 0.0,
    ) -> Any:
        """把“确定性任务关思考”落到 provider 上，返回可能被替换的 provider。

        三步（顺序有含义）：
        1. `with_thinking_disabled()` 决定 style/effort——用户显式开思考时它直接返回原对象。
        2. 仅当最终处于“关思考”状态时才查/写“哪个字段写法真的生效”的缓存。
        3. `allow_probe` 为真且无缓存时，用一次极小请求探测（见 providers.probe_thinking_off）；
           结果按 服务+模型 持久化，后续作业零额外开销。`on_probe` 用于在探测前把
           “正在探测”写进任务面板，避免作业在排队状态里静默几百毫秒到数十秒。

        未声明协议且模型名推断不出的服务保持原样（不发送任何未声明字段）。
        """
        if hasattr(provider, "with_thinking_disabled"):
            provider = provider.with_thinking_disabled()
        config = getattr(provider, "config", None)
        if config is None or not service_id:
            # 没有服务标识（例如随请求传入的临时 provider）：不做缓存/探测，只用推断出的默认字段。
            return provider
        style = resolve_reasoning_style(config)
        effort = str(getattr(config, "reasoning_effort", "") or "")
        if not style or effort != disabled_reasoning_effort(style):
            return provider  # 推断不出协议，或用户显式要求不关思考：维持原样。
        model = str(getattr(config, "model", "") or "")
        cached = self.models.thinking_policy(service_id, model)
        if cached is None:
            if not allow_probe or not hasattr(provider, "with_reasoning"):
                return provider
            if budget > 0 and project_id:
                # 探测也是花钱的真实请求：预算已用尽、账本不可读、或已有未知/待对账费用时
                # 不再探测（不写缓存）。未知费用不能被当成 0 反复放行。
                try:
                    summary = self.store.usage_summary(project_id)
                except Exception:
                    logger.info("用量账本不可读，跳过关思考探测：service=%s model=%s", service_id, model)
                    return provider
                if (float(summary.get("estimated_cost") or 0) >= budget
                        or int(summary.get("unknown_cost_records") or 0) > 0
                        or int(summary.get("pending_cost_records") or 0) > 0):
                    logger.info("项目预算已用尽或已有未知费用，跳过关思考探测：service=%s model=%s", service_id, model)
                    return provider
            if on_probe is not None:
                try:
                    on_probe()
                except Exception:
                    logger.exception("探测提示写入失败（不影响探测）")
            probe_state = {"stop": False}

            def prepare_child(child: Any) -> None:
                # 用子 provider 的 config（max_output_tokens 已缩为 PROBE_MAX_TOKENS）估算，
                # 不按父 provider 的全输出上限预留；价格未知时用保守占额而不是 0。
                estimator = probe_prompt_cost_estimator(getattr(child, "config", config))

                def probe_cost_of(chars: int) -> float:
                    value = estimator(chars)
                    return value if value > 0 else (PROBE_UNKNOWN_COST_USD if budget > 0 else 0.0)

                self.coordinator.attempt_hooks_for(
                    service_id, child, project_id=project_id, budget=budget, cost_of=probe_cost_of,
                )

            def settle_child(child: Any, response: Any) -> None:
                state = getattr(child, "_attempt_state", None)
                handle = getattr(state, "handle", None) if state is not None else None
                if state is not None:
                    state.handle = None
                if handle is not None:
                    self.coordinator.reconcile(
                        handle, actual_cost=(getattr(response, "estimated_cost", None) if response is not None else None),
                    )

            def probe_on_attempt(response: Any, error: Exception | None) -> None:
                if on_probe_usage is not None:
                    try:
                        on_probe_usage(response, error)
                    except Exception:
                        logger.exception("探测用量回调失败（不影响探测判断）")
                if budget > 0 and probe_cost_is_unknown(response, error):
                    # 有限预算下出现不可量化费用后停止后续探测（unknown 不能被当成 0 反复放行）。
                    probe_state["stop"] = True

            def probe_should_continue() -> bool:
                return not probe_state["stop"]

            probed = probe_thinking_off(
                provider, on_attempt=probe_on_attempt,
                prepare_child=prepare_child, settle=settle_child,
                should_continue=probe_should_continue,
            )
            if probed is None:
                # 探测被临时故障打断：不写缓存，保持默认字段，下次作业再试。
                logger.info("关思考探测未完成（临时故障）：service=%s model=%s", service_id, model)
                return provider
            cached = self.models.remember_thinking_policy(service_id, model, probed)
            logger.info(
                "关思考探测：service=%s model=%s variant=%s",
                service_id, model, cached.get("variant") or "(无可用字段)",
            )
        variant = str(cached.get("variant") or "")
        if variant and hasattr(provider, "with_reasoning"):
            return provider.with_reasoning(style=style, effort=effort, variant=variant)
        if cached.get("probed_at") and hasattr(provider, "with_reasoning"):
            # 已探测且没有任何写法生效：明确不发思考字段（NO_REASONING_STYLE，不会因模型名
            # 再被推断回去）。继续发默认字段只会让严格服务在每个新作业实例上白付一次 400。
            return provider.with_reasoning(style=NO_REASONING_STYLE, effort="", variant="")
        return provider

    def record_probe_outcome(
        self, project_id: str, job_id: str, response: Any, error: Exception | None,
        *, provider_name: str = "", model_name: str = "",
    ) -> None:
        """把关思考探测请求纳入用量账本：探测也是真实的付费请求，不能当零成本。

        有 usage 就写实际 token/费用；确实可能已计费但没有 usage（成功但服务不回 usage、
        超时/流已开始/5xx）才持久记“费用未知”；400/401/403/404 等确定不计费的失败不记。
        """
        try:
            tokens = int(getattr(response, "input_tokens", 0) or 0) + int(getattr(response, "output_tokens", 0) or 0)
            if response is not None and tokens > 0:
                self.store.record_usage(UsageRecord(
                    usage_id=uuid.uuid4().hex, project_id=project_id, job_id=job_id,
                    provider=str(getattr(response, "provider", "") or provider_name),
                    model=str(getattr(response, "model", "") or model_name),
                    scope="probe",
                    input_tokens=int(getattr(response, "input_tokens", 0) or 0),
                    output_tokens=int(getattr(response, "output_tokens", 0) or 0),
                    cached_tokens=int(getattr(response, "cached_tokens", 0) or 0),
                    reasoning_tokens=int(getattr(response, "reasoning_tokens", 0) or 0),
                    estimated_cost=getattr(response, "estimated_cost", None),
                    request_id=str(getattr(response, "request_id", "") or ""),
                ))
            else:
                # 预算/并发未放行、请求根本没发出：不可能计费，也不写未知。
                if isinstance(error, (BudgetExhausted, ServiceBusyError)):
                    return
                # 成功但无 usage、读/写超时、已开始的流 → 记“费用未知”；
                # 连接失败与 4xx（除 408）确定不计费 → 不记；status=0 的未知原因保守按可能计费。
                if probe_cost_is_unknown(response, error):
                    self.store.record_pending_cost(
                        project_id, job_id=job_id, reason="probe-no-usage",
                        provider=provider_name, model=model_name,
                    )
        except Exception:
            logger.exception("探测用量记账失败（不影响探测结论）")

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
            if previous["kind"] != "structure_ai" or previous["status"] not in {"failed", "paused"}:
                raise StorageError("只能复用当前项目中失败或已暂停的结构校对任务。")
        active_structure = [
            job for job in self.store.list_jobs(project_id)
            if job.get("kind") == "structure_ai" and job.get("status") in {"queued", "running"}
        ]
        if active_structure:
            raise StorageError("已有结构校对任务正在运行；请等待完成后再启动新的校对。")
        service_id = self.service_id_for(ROLE_STRUCTURE, request.service_id)
        model = self.model_for(ROLE_STRUCTURE, service_id, request.model)
        provider = self.provider_from(service_id, model=model)
        # 任务级思考档位优先于服务配置；在 apply_thinking_policy 之前落到 provider 上。
        provider = self._apply_task_reasoning(provider, request.reasoning_mode, request.reasoning_budget)
        service = self.models.get(service_id)
        config_value = request.model_dump()
        config_value.pop("advanced", None)
        config_value["model"] = model
        # 显式字段 = 调用方真正给出的字段（pydantic ``model_fields_set``）+ ``advanced`` 容器；
        # 只有这些才覆盖档位预设，其余一律由 ``mode`` 派生。
        explicit = {key: getattr(request, key) for key in request.model_fields_set
                    if key not in {"service_id", "mode", "advanced", "reasoning_mode", "reasoning_budget", "model", "toc_source"}}
        explicit.update({key: value for key, value in request.advanced.items() if value is not None})
        if request.mode:
            # 界面档位：由服务端展开内部参数，用户不需要选择复核模式/规划策略/视觉等。
            config_value = structure_mode_config(request.mode, explicit, base=config_value)
            # 项目已确认的目录基准就是“原书目录”：主流程只传 mode 时，后端自动采用它，
            # 不再重跑目录提取。优先级：显式目录/目录来源 > 显式纠错目录页 > 已保存基准 >
            # 自动定位目录页；用户在纠错入口指定了真实目录页时必须重新提取
            # （覆盖旧基准），不能被已保存基准静默截住。
            if (not config_value.get("toc") and "toc" not in explicit
                    and "extract_toc" not in explicit and "toc_page_indexes" not in explicit):
                stored = self.store.load_structure_baseline(project_id)
                stored_toc = stored.get("toc") if isinstance(stored, dict) else None
                if stored_toc:
                    config_value["toc"] = stored_toc
                    config_value["extract_toc"] = False
                    # 携带来源元数据供只读摘要区分“人工确认”与“上次自动提取”，不把自动基准称已确认。
                    config_value["toc_source"] = str(stored.get("source") or "confirmed")
        else:
            config_value.update(explicit)
            config_value.pop("mode", None)
        config_value["service_id"] = service_id
        scope = {
            "toc_entries": len(request.toc),
            "extract_toc": bool(config_value.get("extract_toc", request.extract_toc)),
            "proofread": bool(config_value.get("proofread", request.proofread)),
            "body_text_included": False,
            "toc_page_text_included": bool(config_value.get("extract_toc", request.extract_toc)),
            "mode": request.mode or "",
            "model": model,
            "service_name": service.get("name", ""),
        }
        job_id = self.store.create_job(project_id, "structure_ai", scope, config_value)
        # 暂停事件：结构作业在模型请求边界响应暂停，已完成的响应写入审计可在继续时复用。
        pause = threading.Event()
        try:
            # 结构校对同样默认关思考（reasoning 对“按几何/目录证据定层级”贡献有限）。
            # 这里也允许探测：只用结构校对的用户不会先跑翻译，若只读缓存就永远预热不了，
            # 会一直发被合法拒收的默认字段、靠安全网摘掉→思考始终开着（真实网关就是这种）。
            # 结构请求同样进入公共协调层（服务并发/退避 + 项目预算，按每次 attempt 预留）。
            structure_budget = float(self.store.load_manifest(project_id).get("budget_usd") or 0)
            provider = self.apply_thinking_policy(
                provider, service_id, allow_probe=True,
                on_probe=lambda: self.store.update_job(
                    project_id, job_id, status="running",
                    message="正在探测该服务的关思考字段（只做一次，之后走缓存）…",
                ),
                on_probe_usage=lambda response, error: self.record_probe_outcome(
                    project_id, job_id, response, error,
                    provider_name=str(getattr(provider.config, "provider", "") or ""),
                    model_name=str(getattr(provider.config, "model", "") or ""),
                ),
                project_id=project_id, budget=structure_budget,
            )
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
            self.pause_events[job_id] = pause

            def run() -> None:
                try:
                    run_structure_job(self.store, project_id, job_id, config_value, provider, should_pause=pause.is_set)
                except Exception:
                    logger.exception("Structure job %s failed", job_id)
                finally:
                    # 同样只清自己那一个 Event，避免误删 resume 后的新 Event。
                    if self.pause_events.get(job_id) is pause:
                        self.pause_events.pop(job_id, None)
                    try:
                        provider.close()
                    except Exception:
                        logger.exception("Provider close failed for structure job %s", job_id)
                    self.refresh_translation_summary(project_id)

            self.executor.submit(run)
        except Exception as error:
            # 建作业后到真正提交前的任何失败（探测/挂钩子/线程池关闭）都必须落到终态，
            # 否则作业卡在 queued/running：界面一直“运行中”，后续启动也被“已有任务运行”挡住。
            if self.pause_events.get(job_id) is pause:
                self.pause_events.pop(job_id, None)
            try:
                provider.close()
            except Exception:
                logger.debug("结构作业启动失败后关闭 provider 失败", exc_info=True)
            self.store.update_job(project_id, job_id, status="failed", error=f"无法启动结构校对作业：{error}")
            raise StorageError(f"无法启动结构校对作业：{error}") from error
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
                # 角色绑定给出默认服务商+模型；任务可用 service_id/model 显式覆盖。
                binding = self.models.roles().get(ROLE_TRANSLATION, {}) if self.models else {}
                resolved_service = request.service_id or str(binding.get("service_id") or "")
                resolved_model = request.model or (str(binding.get("model") or "") if binding.get("service_id") == resolved_service else "")
                provider = self.provider_from(resolved_service, request.provider, model=resolved_model)
                # 翻译请求的读超时可按作业配置（冻结快照不含超时，恢复沿用创建时的值）。
                # 测试/自定义 provider 可能不实现该方法，缺省时保持原超时。
                if hasattr(provider, "with_read_timeout"):
                    provider = provider.with_read_timeout(float(request.request_timeout_seconds))
                # 任务级思考档位优先于服务配置；在 apply_thinking_policy 之前落到 provider 上。
                provider = self._apply_task_reasoning(provider, request.reasoning_mode, request.reasoning_budget)
                # 翻译是确定性任务：reasoning 对译文质量近乎零贡献却占掉大部分输出延迟
                # （本机实测 reasoning 占输出 93.7%，单请求平均 152 秒）。默认关思考，
                # 并在首次任务时探测该服务真正接受的字段写法（结果按 服务+模型 缓存）。
                provider = self.apply_thinking_policy(
                    provider, resolved_service, allow_probe=True,
                    on_probe=lambda: self.store.update_job(
                        project_id, job_id, status="running",
                        message="正在探测该服务的关思考字段（只做一次，之后走缓存）…",
                    ),
                    on_probe_usage=lambda response, error: self.record_probe_outcome(
                        project_id, job_id, response, error,
                        provider_name=str(getattr(provider.config, "provider", "") or ""),
                        model_name=str(getattr(provider.config, "model", "") or ""),
                    ),
                    project_id=project_id,
                    budget=float(self.store.load_manifest(project_id).get("budget_usd") or 0),
                )
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
                # 只清自己那一个 Event：暂停后立即 resume 会用同一 job_id 换上新 Event，
                # 旧线程的 finally 若按 job_id 盲删，会把新 Event 删掉，导致继续后无法再暂停。
                if self.pause_events.get(job_id) is pause:
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


# ---- 章节索引（无模型、无 AI 依赖） -----------------------------------------
# 「当前章 / 目录 / 折叠范围」必须在**没有任何分层**时也可用，否则首次折叠只能整书。
# 这里直接复用 core 的 `build_sections`（与折叠作业同一个 section/unit id 生成器），
# 只投影成轻量索引，不重建一套前端 id。
_SECTION_INDEX_CACHE: dict[tuple[str, float, float, str], list[dict]] = {}


def _section_index_for(store, project_id: str, locale: str = "source") -> list[dict]:
    """按文档标题结构给出章节索引（id 与折叠层完全一致）。按 mtime 缓存，避免轮询重算。"""
    project_dir = store.project_path(project_id)
    document_path = project_dir / "document" / "document.json"
    try:
        document_mtime = document_path.stat().st_mtime
    except OSError:
        document_mtime = 0.0
    translation = None
    translation_mtime = 0.0
    if locale and locale != "source":
        try:
            translation_mtime = (project_dir / "translations" / locale / "translation.json").stat().st_mtime
        except OSError:
            translation_mtime = 0.0
        try:
            translation = store.load_translation(project_id, locale)
        except Exception:  # noqa: BLE001 - 译文缺失不影响章节索引
            translation = None
    key = (project_id, document_mtime, translation_mtime, locale or "source")
    cached = _SECTION_INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    document = store.load_document(project_id)
    sections = build_sections(document, locale=locale or "source", translation=translation)
    index = [
        {
            "id": str(section.get("id") or ""),
            "num": str(section.get("num") or ""),
            "title": str(section.get("title") or ""),
            "level": int(section.get("level") or 1),
            "block_id": str(section.get("block_id") or ""),
            "page_index": int(section.get("page_index") or 0),
            "unit_ids": [str(unit.get("block_id") or unit.get("id") or "") for unit in section.get("units") or []],
            "foldable": len([unit for unit in section.get("units") or [] if not unit.get("fallback")]),
        }
        for section in sections
    ]
    if len(_SECTION_INDEX_CACHE) > 32:
        _SECTION_INDEX_CACHE.clear()
    _SECTION_INDEX_CACHE[key] = index
    return index


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
    # 修正 NO_PROXY 里 httpx 无法解析的 [::1] 条目（此前只在 serve() 入口做，
    # 浏览器开发/测试直接 create_app 时首页 token 检查会 InvalidURL 500）。
    normalize_proxy_environment()
    # 清理指向不存在路径的 TLS 信任库环境变量（SSL_CERT_FILE/DIR 等）：
    # 否则 ssl.create_default_context 在冻结包里会 FileNotFoundError，所有 HTTPS 请求失败。
    # 只剔除不可用路径，保留存在的自定义 CA；证书校验始终开启。
    removed_tls_env = normalize_tls_environment()
    if removed_tls_env:
        logger.warning("Ignored unusable TLS trust-store env vars: %s", ", ".join(removed_tls_env))
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

    @app.exception_handler(ReaderConflict)
    async def reader_conflict(_request: Request, error: ReaderConflict):
        # 乐观并发冲突：可重试（刷新 revision 后再提交），与「数据非法」区分开。
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(error), "code": "reader_revision_conflict"}, status_code=409)

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
            "capabilities": ["intake_items_v1", "structure_review_v2", "model_services_v1", "model_services_v2", "model_services_v3", "model_services_v4", "structure_job_v1", "structure_modes_v1", "structure_pause_v1", "structure_vision_v1", "structure_flow_v1", "desktop_resources_v1", "translation_module_v1", "translation_engine_v2", "resource_pipeline_v1", "translation_safety_v1", "parse_lifecycle_v1", "task_history_v1", "parse_backends_v1", "parse_engine_v1", "export_v1", "export_manage_v1",
                    "reader_v1", "reader_v2", "reader_chat_v1", "reader_roles_v1",
                    "reader_fold_run_v1", "reader_native_v1"],
        }

    @app.get("/parse/backends")
    def parse_backends():
        # 只读能力清单：remote 已实现可用；local 按本地引擎真实安装状态动态返回。
        return {"backends": describe_backends()}

    @app.get("/parse/engine")
    def parse_engine_status():
        # 本地引擎状态：安装状态/版本/占用/进度/错误，供设置卡与导入页共用。
        return shared_engine().status()

    @app.post("/parse/engine/install")
    def parse_engine_install(request: EngineInstallRequest):
        # 幂等：已就绪直接返回状态；中断后再次调用会从断点继续；
        # force=True 为「修复/重装」（已就绪时重跑幂等步骤，与解析任务互斥）。
        try:
            return shared_engine().install(force=request.force)
        except EngineBusyError as error:
            raise HTTPException(status_code=409, detail=str(error))

    @app.post("/parse/engine/cancel")
    def parse_engine_cancel(_request: EngineActionRequest):
        return shared_engine().cancel()

    @app.post("/parse/engine/uninstall")
    def parse_engine_uninstall(_request: EngineActionRequest):
        # 卸载只删除独立引擎目录；与安装/本地解析任务互斥。
        try:
            return shared_engine().uninstall()
        except EngineBusyError as error:
            raise HTTPException(status_code=409, detail=str(error))

    @app.get("/parse/engine/sources")
    def parse_engine_sources():
        # 引擎来源：当前选择 + 可用的外部 CLI 发现结果（TextFold/PaperFold 仅为候选）。
        from .mineru_local import discover_external_engines, get_engine_selection, get_http_service_key
        selection = get_engine_selection()
        http_url = str(selection.get("http_url") or "")
        return {
            "selection": selection,
            "discovered": discover_external_engines(),
            "http_key_configured": bool(http_url and get_http_service_key(http_url)),
            "http_key_url": http_url,
        }

    @app.post("/parse/engine/select")
    def parse_engine_select(request: EngineSelectRequest):
        # 外部 CLI / HTTP 服务必须先通过探测验证才允许选用；
        # HTTP 支持在连接时一并校验新密钥（验证成功才保存并绑定到该地址）。
        from .mineru_local import set_engine_selection
        try:
            value = set_engine_selection(
                request.kind, external_dir=request.external_dir,
                http_url=request.http_url, allow_plaintext_http=request.allow_plaintext_http,
                http_key=request.http_key,
            )
        except WorkflowError as error:
            raise HTTPException(status_code=400, detail=str(error))
        return {"selection": value, "status": shared_engine().status(), "capabilities": describe_backends()}

    @app.get("/parse/engine/probe-external")
    def parse_engine_probe(external_dir: str):
        from .mineru_local import probe_external_engine
        return probe_external_engine(external_dir)

    @app.put("/parse/engine/http/key")
    def parse_engine_http_key_save(request: EngineHttpKeyInput):
        # 密钥按规范化服务地址绑定；换地址不继承旧密钥。
        from .mineru_local import save_http_service_key
        try:
            save_http_service_key(request.http_url, request.value)
        except WorkflowError as error:
            raise HTTPException(status_code=400, detail=str(error))
        return {"configured": True, "http_url": request.http_url}

    @app.delete("/parse/engine/http/key")
    def parse_engine_http_key_delete(http_url: str = ""):
        from .mineru_local import delete_http_service_key
        if not http_url:
            raise HTTPException(status_code=400, detail="请提供服务地址。")
        delete_http_service_key(http_url)
        return {"configured": False, "http_url": http_url}

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

    ACTIVE_JOB_STATUSES = {"queued", "running"}
    ACTIVE_OCR_STATES = {"queued", "preparing", "uploading", "processing", "downloading", "merging", "waiting", "interrupted"}

    def active_job_blockers(project_id: str) -> list[str]:
        """列出仍在运行/等待中的项目作业（翻译、结构、即时）与 MinerU 解析作业。"""
        blockers = [str(job.get("job_id")) for job in store.list_jobs(project_id) if job.get("status") in ACTIVE_JOB_STATUSES]
        blockers += [str(job.get("job_id")) for job in ocr.list(project_id) if job.get("state") in ACTIVE_OCR_STATES]
        return blockers

    @app.patch("/projects/{project_id}")
    def update_project(project_id: str, request: ProjectUpdate):
        # 重命名、改语言/章节都会移动项目目录或整体重写 document.json。
        # 正在跑的作业仍按旧路径/旧文档写盘，会在旧路径下重建幽灵目录或丢掉并发修订，
        # 因此有活动作业时直接拒绝，而不是“写完再发现不一致”。
        touches_document = any(value is not None for value in (
            request.title, request.source_language, request.target_language, request.chapter_language_overrides,
        ))
        if touches_document:
            blockers = active_job_blockers(project_id)
            if blockers:
                raise HTTPException(status_code=409, detail="项目仍有运行或等待中的任务，请等待任务结束后再修改项目信息。")
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
            # document.json 的读改写必须与结构补丁/撤销共用同一把锁，否则并发写会互相覆盖。
            with store.document_lock(project_id):
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
            return {**document.to_dict(), "budget_usd": manifest.get("budget_usd", 0), "flow_links": detect_cross_page_flow(document, project_path)}
        if changes:
            store.update_manifest(project_id, **changes)
        return store.describe_project(project_id)

    @app.delete("/projects/{project_id}")
    def delete_project(project_id: str):
        if active_job_blockers(project_id):
            raise HTTPException(status_code=409, detail="项目仍有运行或等待中的任务，请等待任务结束后再删除。")
        ocr.archive_project_jobs(project_id)
        return store.delete_project(project_id)

    @app.get("/projects/{project_id}/document")
    def document(project_id: str):
        value = store.load_document(project_id)
        # flow_links 是只读派生视图元数据（跨页续接），不写回 document.json。
        return {**value.to_dict(), "flow_links": detect_cross_page_flow(value, store.project_path(project_id))}

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
        value = import_mineru(store, project_id, request.paths)
        return {**value.to_dict(), "flow_links": detect_cross_page_flow(value, store.project_path(project_id))}

    @app.get("/projects/{project_id}/mineru/verify")
    def mineru_verify(project_id: str):
        errors = store.verify_raw(project_id)
        return {"valid": not errors, "errors": errors}

    @app.get("/mineru/token")
    def mineru_token_status():
        return {"configured": bool(get_mineru_token())}

    @app.post("/mineru/token/check")
    def mineru_token_check():
        # 连接检查失败必须返回可理解、可操作的信息（4xx/502），不能 500 把原因埋在日志。
        from .providers import tls_context_error

        try:
            client = MinerUClient(get_mineru_token(), max_attempts=2)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise StorageError(tls_context_error(error)) from error
        except WorkflowError as error:
            raise StorageError(str(error)) from error
        try:
            client.check_token()
        except MinerUAPIError as error:
            raise HTTPException(status_code=502, detail=f"MinerU 连接检查失败：{error}") from error
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
        payload = request.model_dump()
        backend_id = payload.pop("backend_id", "mineru_remote")
        return ocr.create(project_id, enforce_single=True, backend_id=backend_id, **payload)

    @app.post("/projects/{project_id}/ocr/start")
    def start_project_parse(project_id: str, request: ParseStart):
        # Token/引擎校验、活动作业/文档/源文件校验、创建与调度都在服务层的锁内完成；
        # 本地后端要求引擎就绪，远端后端要求 Token；创建成功但调度失败会报出具体错误与可恢复状态。
        return ocr.start_parse(project_id, backend_id=request.backend_id, **request.model_dump(exclude={"backend_id"}))

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
            backend_id = options.pop("backend_id", "mineru_remote")
            local_backend = resolve_job_backend(backend_id).backend_id == LOCAL_BACKEND_ID
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
                job = ocr.create(project_id, backend_id=backend_id, **file_options)
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
        # 远端后端需要 Token 才启动；本地后端只需引擎就绪（执行入口会再校验）。
        can_start = token_configured or (local_backend and local_engine_ready())
        if can_start:
            for job in jobs:
                try:
                    manager.start_ocr(job["job_id"])
                except Exception:
                    # The persisted queued job remains resumable on the next
                    # startup; do not turn an accepted batch into a false 400.
                    logger.exception("Could not start queued MinerU job %s", job["job_id"])
        return {"jobs": jobs, "token_configured": token_configured, "engine_ready": local_engine_ready()}

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
        # Token 只约束远端后端；本地后端改为要求引擎就绪（执行入口会再校验）。
        if ocr._job_needs_token(job_id):
            if not get_mineru_token():
                raise StorageError("请先配置 MinerU Token。")
        elif not local_engine_ready():
            raise StorageError("本地解析来源尚未就绪；请在设置 → 文档解析里安装引擎或检查已有安装/服务连接。")
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
        # Token 只约束远端后端；本地后端要求引擎就绪。
        if ocr._job_needs_token(job_id):
            if not get_mineru_token():
                raise StorageError("请先配置 MinerU Token。")
        elif not local_engine_ready():
            raise StorageError("本地解析来源尚未就绪；请在设置 → 文档解析里安装引擎或检查已有安装/服务连接。")
        try:
            return ocr.retry_job(job_id)
        except (StorageError, WorkflowError) as error:
            raise HTTPException(status_code=409, detail=str(error))

    @app.post("/projects/{project_id}/parse/migrate")
    def migrate_parse_coverage(project_id: str):
        return ocr.migrate_project(project_id)

    @app.post("/projects/{project_id}/ocr/continue")
    def continue_ocr_job(project_id: str, request: ParseContinue):
        payload = request.model_dump()
        backend_id = payload.pop("backend_id") or ""
        if not backend_id:
            # 未显式指定时延续原任务后端：从既有任务里取 append 模板的后端。
            template = next((job for job in ocr.list(project_id) if job.get("append")), None)
            backend_id = str((template or {}).get("backend_id") or "")
        # Token 只约束远端后端；本地后端要求引擎就绪。
        if backend_id == LOCAL_BACKEND_ID:
            if not local_engine_ready():
                raise StorageError("本地解析来源尚未就绪；请在设置 → 文档解析里安装引擎或检查已有安装/服务连接。")
        elif not get_mineru_token():
            raise StorageError("请先配置 MinerU Token。")
        job, skipped = ocr.continue_parse(
            project_id, page_ranges=request.page_ranges, language=request.language,
            model_version=request.model_version, page_limit=request.page_limit,
            extra_formats=request.extra_formats or None,
            backend_id=backend_id if backend_id == LOCAL_BACKEND_ID else "",
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

    @app.post("/projects/{project_id}/structure/jobs/{job_id}/pause")
    def structure_job_pause(project_id: str, job_id: str):
        job = store.get_job(project_id, job_id)
        if job.get("kind") != "structure_ai":
            raise StorageError("该任务不是结构校对任务。")
        manager.pause(job_id)
        return {"job_id": job_id, "state": "pause_requested"}

    @app.post("/projects/{project_id}/structure/jobs/{job_id}/resume")
    def structure_job_resume(project_id: str, job_id: str):
        # 继续 = 用已暂停/失败作业的审计做请求级复用，只补剩余部分（新建作业指向旧作业）。
        job = store.get_job(project_id, job_id)
        if job.get("kind") != "structure_ai" or job.get("status") not in {"paused", "failed"}:
            raise StorageError("只能继续已暂停或失败的结构校对任务。")
        config = dict(job.get("config") or {})
        config["reuse_job_id"] = job_id
        request = StructureJobInput.model_validate(config)
        new_job_id = manager.start_structure(project_id, request)
        return store.get_job(project_id, new_job_id)

    @app.post("/projects/{project_id}/structure/analyze-ai")
    def structure_analyze_ai(project_id: str, request: StructureAIAnalyze):
        scope = {"toc_entries": len(request.toc), "body_text_included": False}
        job_id = store.create_job(project_id, "structure_ai", scope, request.model_dump())
        store.update_job(project_id, job_id, status="running")
        try:
            binding = manager.models.roles().get(ROLE_STRUCTURE, {}) if manager.models else {}
            resolved = request.service_id or str(binding.get("service_id") or "")
            model = request.model or (str(binding.get("model") or "") if binding.get("service_id") == resolved else "")
            provider = manager.provider_from(resolved, request.provider, model=model)
            # 与主结构作业路径一致：默认关思考（走已有探测缓存，不在这里探测）。
            # 临时 ProviderInput（无 service_id）不查缓存，避免写到角色服务的键上。
            provider = manager.apply_thinking_policy(provider, resolved or "")
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
        models.ensure_migrated()
        return {
            "services": [{**item, "configured": models.configured(item["service_id"])} for item in models.list()],
            "roles": models.roles(),
        }

    def _role_states() -> dict[str, Any]:
        """四个角色的**全局**真状态（不依赖项目；不含任何密钥）。

        供设置页/聊天面板显示「实际在用哪家模型」：
        - `origin`：`bound` 自有绑定 / `unset` 配置里从没有这个键 / `cleared` 显式清空 / `invalid` 服务或模型已失效；
        - `source`：`role`（自己的绑定）/ `inherited_translation`（**仅阅读角色且 origin=unset** 时回退翻译默认）/
          `unset` / `cleared` / `invalid`；
        - `binding`：配置里真实存的 `{service_id, model}`（不做首模型替换）；
        - `effective`：实际会用的 `{service_id, model}`（模型留空时按服务第一个模型兜底，并标 `model_source=first_model`）；
        - `problem`：不可用时的可读原因。
        `cleared` / `invalid` **不会**回退到 translation（避免“看起来继承、其实用了别家”）。
        """
        states: dict[str, Any] = {}
        for role in MODEL_ROLES:
            state = _role_state(role)
            origin = str(state.get("origin") or "unset")
            binding = dict(state.get("binding") or _empty_model_binding())
            effective = dict(binding)
            source = "role" if origin == "bound" else origin
            problem = str(state.get("problem") or "")
            if origin == "unset" and role in (ROLE_READER_FOLD, ROLE_READER_CHAT):
                inherited = _role_state(ROLE_TRANSLATION)
                if inherited.get("origin") == "bound":
                    effective = dict(inherited.get("binding") or _empty_model_binding())
                    source = "inherited_translation"
            if source in ("cleared", "invalid", "unset"):
                effective = _empty_model_binding() if source != "inherited_translation" else effective
            model_source = ""
            service_id = str(effective.get("service_id") or "")
            if service_id:
                try:
                    service = manager.models.get(service_id) if manager.models else {}
                    available = [str(entry.get("model") or "") for entry in (service or {}).get("models") or []]
                    if not effective.get("model") and available:
                        effective["model"] = available[0]
                        model_source = "first_model"
                    elif effective.get("model"):
                        model_source = "binding"
                except Exception:  # noqa: BLE001 - 展示层读不到服务不影响返回
                    pass
            states[role] = {
                "role": role, "origin": origin, "source": source,
                "binding": binding, "effective": effective,
                "model_source": model_source, "problem": problem,
            }
        return states

    @app.get("/model-services")
    def model_service_list():
        # 向后兼容：老前端只读 services/roles；新增 role_states 供设置页与聊天面板显示真实来源。
        return {**_service_view(), "role_states": _role_states()}

    @app.get("/model-services/roles/state")
    def model_service_role_state():
        """独立只读端点：全局四角色状态（无项目也能用，不含密钥）。"""
        return {"roles": _role_states()}

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
        # 换密钥后重新探测：上一次探测可能是因认证失败而中断，不该留下旧结论。
        models.forget_thinking_policy(service_id)
        return {"configured": True}

    @app.post("/model-services/models")
    def model_service_probe_models(request: ModelProbeInput):
        return {"models": models.probe_models(
            base_url=request.base_url, api_key=request.api_key, service_id=request.service_id,
            allow_plaintext_http=request.allow_plaintext_http,
        )}

    @app.post("/model-services/{service_id}/models")
    def model_service_models(service_id: str):
        models.get(service_id)  # 校验存在
        return {"models": models.list_models(service_id)}

    @app.post("/model-services/{service_id}/check")
    def model_service_check(service_id: str, request: ModelCheckInput | None = None):
        import time as _time
        started = _time.perf_counter()
        provider = models.config_for(service_id, request.model if request else "", timeout=30.0)
        try:
            response = provider.complete(
                system="你是连接性探测助手。只返回 JSON {\"ok\":true}。",
                user="ping",
            )
        finally:
            # 每次检查都新建 provider；不关连接池会每次泄漏一个 httpx.Client。
            try:
                provider.close()
            except Exception:
                logger.debug("探测 provider 关闭失败", exc_info=True)
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
        binding = manager.models.roles().get(ROLE_TRANSLATION, {}) if manager.models else {}
        resolved = request.service_id or str(binding.get("service_id") or "")
        model = request.model or (str(binding.get("model") or "") if binding.get("service_id") == resolved else "")
        service_id = resolved or "default"
        provider = manager.provider_from(resolved, None, model=model)
        # 单块译审同样是翻译任务：默认关思考，走已有探测缓存（不在这里再探测，避免交互延迟）。
        provider = manager.apply_thinking_policy(provider, service_id)
        # 注：预算/并发的 attempt hooks 由 TranslationService.review() 自己挂（已在服务层），
        # 这里不再重复挂一次，避免同一 provider 被两次 attempt_hooks_for 覆盖。
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
        binding = manager.models.roles().get(ROLE_TRANSLATION, {}) if manager.models else {}
        resolved = request.service_id or str(binding.get("service_id") or "")
        model = request.model or (str(binding.get("model") or "") if binding.get("service_id") == resolved else "")
        service_id = resolved or "default"
        provider = manager.provider_from(resolved, request.provider, model=model)
        # 即时翻译同样是翻译任务：默认关思考，只读缓存不探测（划选要即时响应）。
        # 走临时 ProviderInput（无 service_id）时不查缓存：它的键属于角色绑定的服务，会串味。
        policy_key = "" if (request.provider is not None and not request.service_id) else service_id
        provider = manager.apply_thinking_policy(provider, policy_key)
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

    # ---- 阅读模块（三栏对照 + TextFold 五层 + 阅读层交互） -------------------
    # 阅读层的写入全部落在项目目录 reader/ 下，绝不写回 document.json /
    # translation.json / ocr/raw：批注、笔记、折叠内容都是可丢弃、可重建的交互数据。

    def _thinking_policy_for(binding: dict[str, str]) -> dict[str, Any] | None:
        """该服务+模型已探测到的「关思考」字段写法；None 表示尚未探测（未知，不是“已关闭”）。"""
        service_id = str(binding.get("service_id") or "")
        model = str(binding.get("model") or "")
        if not service_id or manager.models is None:
            return None
        try:
            return manager.models.thinking_policy(service_id, model)
        except Exception:  # noqa: BLE001 - 探测缓存读不到不影响阅读功能
            return None

    def _role_state(role: str) -> dict[str, Any]:
        """角色状态；真实 store 用 `role_state()`，只提供 `roles()` 的调用方按「空绑定=未设置」降级。

        真实 `ModelServiceStore` 一定能给准确答案（uq 分 unset/cleared/invalid）；这里保留降级分支，
        是为了让只 mock `roles()` 的旧测试替身不至于 500，而不是为了在真实路径上猜。
        """
        resolver = getattr(manager.models, "role_state", None)
        if callable(resolver):
            return resolver(role)
        binding = dict((manager.models.roles() or {}).get(role) or {"service_id": "", "model": ""})
        return {
            "role": role, "origin": "bound" if binding.get("service_id") else "unset",
            "binding": binding, "problem": "",
        }

    def _reader_role_resolution(role: str) -> dict[str, Any]:
        """阅读角色的有效绑定与来源（`reader_role` / `inherited_translation` / `unset`）。

        兼容规则只有一条：**旧配置里从没写过这个角色**（`origin == "unset"`）才回退翻译默认。
        显式清空（`cleared`）或绑定的服务/模型已被删除（`invalid`）都**不**偷偷换别家，
        而是返回可操作的错误，并让界面显示真实来源。
        """
        if manager.models is None:
            return {"role": role, "source": "unset", "binding": _empty_model_binding(), "problem": "尚未配置模型服务。"}
        state = _role_state(role)
        if state["origin"] == "bound":
            return {"role": role, "origin": state["origin"], "source": "reader_role", "binding": state["binding"], "problem": ""}
        if state["origin"] == "unset":
            inherited = _role_state(ROLE_TRANSLATION)
            if inherited["origin"] == "bound":
                return {
                    "role": role, "origin": state["origin"], "source": "inherited_translation",
                    "binding": inherited["binding"], "problem": "",
                }
            return {
                "role": role, "origin": state["origin"], "source": "unset", "binding": _empty_model_binding(),
                "problem": "尚未绑定阅读模型，翻译默认模型也不可用。",
            }
        return {
            "role": role, "origin": state["origin"], "source": state["origin"],
            "binding": _empty_model_binding(), "problem": state["problem"],
        }

    def _empty_model_binding() -> dict[str, str]:
        return {"service_id": "", "model": ""}

    def _effective_of(binding: dict[str, str]) -> tuple[dict[str, str], str]:
        """`binding`（原值）→ `(effective, model_source)`：模型留空时按该服务第一个模型兜底。

        契约：**binding 永远保留用户写入的原值**（含空 model）；首模型兜底只出现在 effective 里，
        并用 `model_source="first_model"` 标出来，避免 UI 把“未指定”说成“你选了这个模型”。
        """
        effective = dict(binding)
        source = ""
        service_id = str(effective.get("service_id") or "")
        if not service_id:
            return effective, source
        try:
            service = manager.models.get(service_id) if manager.models else {}
        except ProviderError:
            return effective, source
        models = [str(entry.get("model") or "") for entry in (service or {}).get("models") or []]
        if effective.get("model"):
            source = "binding"
        elif models:
            effective["model"] = models[0]
            source = "first_model"
        return effective, source

    def _reader_require(role: str, label: str, explicit_service: str = "", explicit_model: str = "") -> dict[str, Any]:
        """解析一次阅读请求实际使用的绑定；不可用时抛 400（可操作文案），绝不静默改用别家模型。

        返回 `{role, origin, source, binding, effective, model_source, problem}`：
        - `binding`：存储/请求里的**原值**（可能 model 为空）；
        - `effective`：真正要发给服务的 `{service_id, model}`（空 model 已按首模型兜底）；
        - `source`：`role` / `inherited_translation` / `explicit` / `unset` / `cleared` / `invalid`（后三者直接 400）。
        """
        if explicit_service:
            try:
                service = manager.models.get(explicit_service) if manager.models else {}
            except ProviderError:
                raise HTTPException(status_code=400, detail=f"指定的服务商不存在：{explicit_service}。")
            models = [str(entry.get("model") or "") for entry in (service or {}).get("models") or []]
            chosen = explicit_model or ""
            if not services_has(service) or (chosen and chosen not in models):
                raise HTTPException(status_code=400, detail=f"指定的模型不可用：{explicit_service} / {chosen or '(未指定)'}。")
            binding = {"service_id": explicit_service, "model": chosen}
            effective, model_source = _effective_of(binding)
            return {
                "role": role, "origin": "bound", "source": "explicit",
                "binding": binding, "effective": effective, "model_source": model_source, "problem": "",
            }
        resolution = _reader_role_resolution(role)
        if not resolution["binding"].get("service_id"):
            raise HTTPException(
                status_code=400,
                detail=f"{label}不可用：{resolution['problem']} 请到「设置 → 阅读」选择模型服务。",
            )
        binding = dict(resolution["binding"])
        if explicit_model:
            binding["model"] = explicit_model
        effective, model_source = _effective_of(binding)
        return {**resolution, "binding": binding, "effective": effective, "model_source": model_source}

    def services_has(service: Any) -> bool:
        return bool(service) and bool(service.get("models"))

    def _provider_factory_for(project_id: str, binding: dict[str, str]) -> Callable[[], Any]:
        """按已解析的绑定给出「按需新建 provider」的工厂（预算/服务并发钩子照旧挂上）。"""
        resolved = str(binding.get("service_id") or "")
        resolved_model = str(binding.get("model") or "")

        def factory() -> Any:
            provider = manager.provider_from(resolved, None, model=resolved_model)
            # 折叠/阅读问答都是「按已给文本作答」的任务：与翻译、结构校对走**同一套**关思考策略，
            # 包括「首次按 服务+模型 探测真正生效的字段写法」——本机实测该网关只认
            # chat_template_kwargs 变体，只发默认 enable_thinking 时 reasoning 仍占 73–90%。
            # 探测必须发生在挂请求协调钩子之前（probe 拒绝在钩子已挂时发请求）；结果按服务+模型缓存。
            budget = 0.0
            if resolved:
                try:
                    budget = float(store.load_manifest(project_id).get("budget_usd") or 0)
                except Exception:  # noqa: BLE001 - 预算读不到就不做预算门控探测
                    budget = 0.0
            provider = manager.apply_thinking_policy(
                provider, resolved, allow_probe=True,
                on_probe_usage=lambda response, error: manager.record_probe_outcome(
                    project_id, "", response, error,
                    provider_name=str(getattr(provider.config, "provider", "") or ""),
                    model_name=str(getattr(provider.config, "model", "") or ""),
                ),
                project_id=project_id, budget=budget,
            )
            if resolved:
                manager.coordinator.attempt_hooks_for(
                    resolved, provider, project_id=project_id, budget=budget,
                    cost_of=prompt_cost_estimator(provider.config),
                )
            return provider

        return factory

    @app.get("/projects/{project_id}/reader/state")
    def reader_state(project_id: str, locale: str = "source"):
        reader = ReaderStore(store)
        document = store.load_document(project_id)
        return {
            "project_id": project_id,
            "title": document.title,
            "source_language": document.source_language,
            "target_language": document.target_language,
            "bbox_space": document.bbox_space,
            "page_count": document.page_count,
            "overlay": reader.load_overlay(project_id),
            "layers": layer_state(project_id, reader),
            "layers_locale": locale,
            "layer_job": manager.reader_jobs.latest_job(project_id),
            # 整篇 fold-run（AIFold 生命周期）：idle/running/pausing/paused/ready/failed，
            # 前端据此决定「AIFold 默认关闭→点击整篇处理→ready 才显示「启动」」。
            "fold_run": manager.reader_jobs.current_run(project_id),
            "summary": summarize_reader(project_id, reader),
            # 章节索引：不依赖模型/分层，供「当前章范围」、目录与本机折叠入口使用，
            # id/unit_ids 与折叠作业同源（core build_sections）。
            "section_index": _section_index_for(store, project_id, locale),
            # 阅读两个角色的**真状态**：与全局 `/model-services.role_states` 同一份 DTO
            # （origin/source/binding/effective/model_source/problem），界面据此显示实际服务商/模型
            # 与「继承翻译默认」，不再靠空 dict 或 translationBinding 推断。
            # `thinking` 是该 服务+模型 的关思考探测缓存（null = 未探测/未知），界面据此如实标注。
            "roles": {
                "fold": _role_states()[ROLE_READER_FOLD],
                "chat": _role_states()[ROLE_READER_CHAT],
            },
            "thinking": {
                "fold": _thinking_policy_for(_reader_role_resolution(ROLE_READER_FOLD)["binding"]),
                "chat": _thinking_policy_for(_reader_role_resolution(ROLE_READER_CHAT)["binding"]),
            },
        }

    @app.get("/projects/{project_id}/reader/layers")
    def reader_layers(project_id: str, locale: str = "source"):
        # 只读：缺分层时返回空 sections + stale，由前端决定是否触发折叠（不隐式调模型）。
        return ReaderLayerService(store).document(project_id, locale=locale)

    @app.post("/projects/{project_id}/reader/layers")
    def reader_layers_generate(project_id: str, request: ReaderLayersInput):
        locale = request.locale or "source"
        store.load_document(project_id)
        if request.mode == "local":
            resolution = {"role": ROLE_READER_FOLD, "source": "local", "binding": _empty_model_binding(), "problem": ""}
        else:
            # 门控按**折叠角色**判断（reader_fold，未设置时回退翻译默认）：只配折叠、没配翻译也能折叠。
            resolution = _reader_require(ROLE_READER_FOLD, "折叠模型", request.service_id, request.model)
        binding = resolution["binding"]
        effective = resolution.get("effective") or binding
        job_id, created = manager.reader_jobs.start(
            project_id, locale=locale, provider_factory=_provider_factory_for(project_id, effective),
            model_label=str(effective.get("model") or ""), service_id=str(effective.get("service_id") or ""),
            section_ids=request.section_ids, force=request.force, mode=request.mode,
            binding_source=resolution["source"],
            on_response=usage_recorder(store, project_id, "", "reader_layers"),
        )
        # created=False 表示同一项目已有折叠作业在跑：返回既有作业，不重复提交、不重复计费。
        return {"job_id": job_id, "status": "running", "created": created, "locale": locale}

    @app.post("/projects/{project_id}/reader/layers/stop")
    def reader_layers_stop(project_id: str, locale: str = ""):
        # 在章节边界生效：当前在途请求跑完即停，已完成章节保留（远端模型请求不可中断，如实声明）。
        job_ids = manager.reader_jobs.stop(project_id, locale or None)
        if not job_ids:
            raise HTTPException(status_code=409, detail="当前没有正在运行的折叠作业。")
        return {"job_ids": job_ids, "status": "stopping"}

    # ---- 整篇 fold-run：AIFold 默认关闭→点击后整篇处理→全部完成才 ready ----

    def _run_locale(project_id: str, requested: str) -> str:
        """run 的目标 locale：显式指定必须存在译文；缺省=有译文则译文 locale，否则 source。
        整篇 run 同一时刻只驱动一个目标 controller（原文层作为 prerequisite 在其内部补齐）。"""
        if requested:
            if requested != "source" and store.load_translation(project_id, requested) is None:
                raise HTTPException(status_code=400, detail=f"该项目还没有 {requested} 译文层，无法作为折叠目标。")
            return requested
        document = store.load_document(project_id)
        target = str(document.target_language or "")
        if target and target != "source" and store.load_translation(project_id, target) is not None:
            return target
        return "source"

    def _require_reader_project(project_id: str) -> None:
        """fold-run 路由的缺项目映射：404（而不是 StorageError→400/500），前端轮询可干净处理。"""
        try:
            project_dir = store.project_path(project_id)
        except StorageError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if not (project_dir / "project.json").is_file():
            raise HTTPException(status_code=404, detail=f"项目不存在：{project_id}")

    @app.post("/projects/{project_id}/reader/fold-runs")
    def reader_fold_run_start(project_id: str, request: FoldRunInput):
        _require_reader_project(project_id)
        store.load_document(project_id)
        locale = _run_locale(project_id, request.locale.strip())
        resolution = _reader_require(ROLE_READER_FOLD, "折叠模型", "", "")
        binding = resolution["binding"]
        effective = resolution.get("effective") or binding
        try:
            run, created = manager.reader_jobs.start_run(
                project_id, locale=locale,
                provider_factory=_provider_factory_for(project_id, effective),
                model_label=str(effective.get("model") or ""),
                service_id=str(effective.get("service_id") or ""),
                force=request.force, binding_source=resolution["source"],
                on_response=usage_recorder(store, project_id, "", "reader_fold_run"),
            )
        except StorageError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"run_id": run.get("run_id", ""), "created": created, "run": run, "locale": locale}

    @app.get("/projects/{project_id}/reader/fold-run")
    def reader_fold_run_get(project_id: str):
        _require_reader_project(project_id)
        return manager.reader_jobs.current_run(project_id)

    @app.post("/projects/{project_id}/reader/fold-runs/{run_id}/pause")
    def reader_fold_run_pause(project_id: str, run_id: str):
        _require_reader_project(project_id)
        run = manager.reader_jobs.pause_run(project_id, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="折叠 run 不存在。")
        if run.get("status") in ("ready", "failed", "paused", "idle"):
            return {"run": run, "accepted": False,
                    "detail": "run 已结束或已暂停，无需暂停。"}
        return {"run": run, "accepted": run.get("status") == "pausing"}

    @app.post("/projects/{project_id}/reader/fold-runs/{run_id}/resume")
    def reader_fold_run_resume(project_id: str, run_id: str, request: FoldRunInput | None = None):
        # body 可省（UI「继续」发无 body POST）：resume 沿冻结 run 继续，locale 冻结在
        # run 记录里、绑定冻结、force 缺省 False——空 body 等价默认输入。
        # 传了 body 时仍走 FoldRunInput 校验（extra=forbid：不支持字段 422）。
        request = request or FoldRunInput()
        _require_reader_project(project_id)
        store.load_document(project_id)
        current = manager.reader_jobs.current_run(project_id)
        if current.get("status") == "idle" or current.get("run_id") != run_id:
            raise HTTPException(status_code=404, detail="折叠 run 不存在。")
        if current.get("status") not in ("paused", "failed"):
            raise HTTPException(status_code=409,
                                detail=f"当前 run 状态为 {current.get('status')}，只有 paused/failed 可以继续。")
        locale = _run_locale(project_id, (request.locale or str(current.get("locale") or "")).strip())
        if locale != current.get("locale"):
            raise HTTPException(status_code=400,
                                detail=f"run 的目标语言是 {current.get('locale')}，不能改成 {locale}；请重新启动一个 run。")
        # 绑定冻结：resume 沿用 run 记录里的 service/model（缓存键与已完成节一致，不重复计费）。
        # 不重新解析角色——换模型必须显式重启新 run。
        frozen = dict(current.get("binding") or {})
        effective = {"service_id": str(frozen.get("service_id") or ""), "model": str(frozen.get("model") or "")}
        if not effective["service_id"]:
            raise HTTPException(status_code=400, detail="run 记录缺少折叠模型绑定，无法继续；请重新启动一个 run。")
        try:
            run, created = manager.reader_jobs.resume_run(
                project_id, run_id,
                provider_factory=_provider_factory_for(project_id, effective),
                force=request.force,
                on_response=usage_recorder(store, project_id, "", "reader_fold_run"),
            )
        except StorageError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if run is None:
            raise HTTPException(status_code=404, detail="折叠 run 不存在。")
        return {"run_id": run.get("run_id", ""), "created": created, "run": run, "locale": locale}

    @app.get("/projects/{project_id}/reader/layers/native")
    def reader_layers_native(project_id: str, locale: str = "source", section: str = ""):
        """词级原生结构（toks/sents/lad/atoms，brief 词 m、topic 词 k）：前端词级 stage/morph 数据源。

        只读已落盘的派生层（不发模型）；`section=S3` 可按节过滤 chunks（units 仍全量返回，
        词级定位与跨节词身份需要）。"""
        payload = ReaderStore(store).load_native(project_id, locale)
        if payload is None:
            raise HTTPException(status_code=404,
                                detail=f"还没有 {locale} 的词级折叠结构；请先运行 AIFold 或生成分层。")
        if section:
            chunks = [chunk for chunk in payload.get("chunks") or [] if chunk.get("id") == section]
            if not chunks:
                raise HTTPException(status_code=404, detail=f"词级结构里没有章节 {section}。")
            payload = {**payload, "chunks": chunks}
        return payload

    @app.delete("/projects/{project_id}/reader/layers")
    def reader_layers_clear(project_id: str, locale: str = ""):
        clear_layers(ReaderStore(store), project_id, locale or None)
        return {"cleared": locale or "all"}

    @app.put("/projects/{project_id}/reader/overlay")
    def reader_overlay_save(project_id: str, request: ReaderOverlayInput):
        return ReaderStore(store).save_overlay(project_id, request.overlay, base_revision=request.base_revision)

    # ---- 阅读对话：统一侧栏 + 一次性引用 + 按条冻结模型 -------------------
    # 会话历史落在项目目录 reader/chat.json；一条消息的模型选择在创建时冻结，
    # 之后改设置不影响在途/历史；回答落地时再记录实际响应的模型。

    def _chat_binding(service_id: str, model: str) -> dict[str, str]:
        """对话本次使用的绑定：显式选择优先，否则 reader_chat → 未设置时继承翻译默认。

        `source` 会随消息落库并在 wire DTO 里返回，界面据此显示「继承翻译默认」，
        不让用户以为专用阅读对话模型在生效。
        """
        resolution = _reader_require(ROLE_READER_CHAT, "阅读对话模型", service_id, model)
        binding = dict(resolution["binding"])
        binding["source"] = resolution["source"]
        resolved = binding["service_id"]
        try:
            service = manager.models.get(resolved) if resolved else {}
            binding["name"] = str(service.get("name") or "")
            entry = next((item for item in service.get("models") or [] if item.get("model") == binding["model"]), None)
            binding["label"] = str((entry or {}).get("name") or binding["model"])
        except Exception:  # noqa: BLE001 - 展示名缺失不影响调用
            pass
        return binding

    def _chat_wire(project_id: str, chat: dict[str, Any]) -> dict[str, Any]:
        """把落盘的消息投影成前端 DTO（`frontend/src/reader/types.ts` 的 ReaderChatMessage）。

        - `question`/`answer` 按角色拆分；`state` 把 running→sending 对齐前端枚举。
        - `quote.snippet` 是读者选中的文字；`srcText` **永远取原文 block**，译文引用也只给原文证据。
        - `model` 是给人看的展示名，`binding` 是重试/复用所需的真实 {service_id, model}。
        """
        try:
            document = store.load_document(project_id)
            blocks = {block.block_id: block for block in document.blocks}
        except Exception:  # noqa: BLE001 - 文档缺失时仍可读历史
            blocks = {}
        out = []
        for item in chat.get("messages") or []:
            quote = item.get("quote")
            wire_quote = None
            if quote:
                block = blocks.get(quote.get("block_id"))
                src = (getattr(block, "source_text", "") or "") if block is not None else ""
                wire_quote = {
                    "block_id": quote.get("block_id", ""),
                    "pane": quote.get("pane", "source"),
                    "snippet": quote.get("text", ""),
                    "srcText": src or quote.get("text", ""),
                    "sec": quote.get("section", ""),
                }
            binding = item.get("model") or {}
            # 展示名优先用服务端实际回报的模型名；路由身份仍是 binding.service_id + binding.model。
            shown_model = binding.get("actual") or binding.get("label") or binding.get("model") or ""
            label = " · ".join(part for part in (binding.get("name"), shown_model) if part)
            source = binding.get("source") or ""
            if source == "inherited_translation":
                label = f"{label}（继承翻译默认）" if label else "继承翻译默认"
            state = {"running": "sending", "ok": "done", "error": "error", "stopped": "stopped"}.get(
                item.get("status", "ok"), "done",
            )
            wire = {
                "id": item["id"],
                "role": item["role"],
                "citations": item.get("citations") or [],
                "model": label,
                "binding": {
                    "service_id": binding.get("service_id", ""), "model": binding.get("model", ""),
                    "source": source, "actual": binding.get("actual", ""),
                },
                "state": state,
                "error": item.get("error", ""),
                "created_at": item.get("created_at", ""),
            }
            if item["role"] == "user":
                wire["question"] = item.get("text", "")
            else:
                wire["answer"] = item.get("text", "")
                wire["question"] = next(
                    (prev.get("text", "") for prev in reversed(out) if prev["role"] == "user"), "",
                )
            if wire_quote:
                wire["quote"] = wire_quote
            out.append(wire)
        return {"conversation_id": chat.get("conversation_id", ""), "messages": out, "updated_at": chat.get("updated_at", "")}

    @app.get("/projects/{project_id}/reader/chat")
    def reader_chat(project_id: str):
        chat = ReaderChatRunner(store).conversation(project_id)
        return _chat_wire(project_id, chat)

    @app.post("/projects/{project_id}/reader/chat/messages")
    def reader_chat_send(project_id: str, request: ReaderChatInput):
        store.load_document(project_id)
        binding = _chat_binding(request.service_id, request.model)
        provider_factory = _provider_factory_for(project_id, binding)
        runner = ReaderChatRunner(store)
        started = runner.start(
            project_id, question=request.question, quote=request.quote.model_dump() if request.quote else None,
            locale=request.locale or "source", binding=binding, provider_factory=provider_factory,
            usage_hook=usage_recorder(store, project_id, "", "reader_chat"),
        )
        return {
            **started, "binding": binding, "status": "running",
            "chat": _chat_wire(project_id, runner.conversation(project_id)),
        }

    @app.post("/projects/{project_id}/reader/chat/messages/{message_id}/stop")
    def reader_chat_stop(project_id: str, message_id: str):
        chat = ReaderChatRunner(store).stop(project_id, message_id)
        return _chat_wire(project_id, chat)

    @app.post("/projects/{project_id}/reader/chat/messages/{message_id}/retry")
    def reader_chat_retry(project_id: str, message_id: str, request: ReaderChatRetryInput):
        # 重试默认**沿用该条消息自己的模型**（用户当时选的那家/那个），而不是重新解析默认角色：
        # 否则「当次换成 svc-other 提问 → 失败 → 重试」会悄悄换回默认模型，路由与用户在界面上看到的不一致。
        stored_service, stored_model = "", ""
        if not request.service_id:
            chat = ReaderChatRunner(store).conversation(project_id)
            target = next((item for item in chat.get("messages") or [] if item["id"] == message_id), None)
            if target is not None:
                stored = target.get("model") or {}
                stored_service = str(stored.get("service_id") or "")
                stored_model = str(stored.get("model") or "")
        binding = _chat_binding(request.service_id or stored_service, request.model or stored_model)
        provider_factory = _provider_factory_for(project_id, binding)
        runner = ReaderChatRunner(store)
        started = runner.retry(
            project_id, message_id, locale=request.locale or "source", binding=binding,
            provider_factory=provider_factory,
            usage_hook=usage_recorder(store, project_id, "", "reader_chat"),
        )
        return {
            **started, "binding": binding, "status": "running",
            "chat": _chat_wire(project_id, runner.conversation(project_id)),
        }

    @app.delete("/projects/{project_id}/reader/chat")
    def reader_chat_clear(project_id: str):
        return _chat_wire(project_id, ReaderChatRunner(store).clear(project_id))

    @app.get("/projects/{project_id}/reader/layers/markdown")
    def reader_layers_markdown(project_id: str, locale: str = "source", level: str = "full", section: str = ""):
        # 复用上游 `fold.markdown()`：把某一层导出为 Markdown（只读投影，不写文件）。
        from fastapi.responses import PlainTextResponse

        text = layers_markdown(ReaderStore(store), project_id, locale=locale, level=level, section=section or None)
        return PlainTextResponse(text)

    @app.post("/projects/{project_id}/reader/ask")
    def reader_ask(project_id: str, request: ReaderAskInput):
        question = (request.question or "").strip()
        if not question:
            raise HTTPException(status_code=400, detail="问题不能为空。")
        document = store.load_document(project_id)
        locale = request.locale or "source"
        # 显示语言章节（写「每节一句话主线」）+ 原文章节（检索与引用证据），见 reader._sections_for_ask。
        from .reader import _sections_for_ask

        sections, source_sections = _sections_for_ask(store, project_id, locale)
        resolution = _reader_require(ROLE_READER_CHAT, "阅读对话模型", request.service_id, request.model)
        binding = resolution["binding"]
        resolved, model = binding["service_id"], binding.get("model", "")
        provider_factory = _provider_factory_for(project_id, binding)
        job_id = store.create_job(
            project_id, "reader_ask",
            {"quoted": bool(request.quote), "block_ids": len(request.block_ids or [])},
            {"service_id": resolved, "model": model, "binding_source": resolution["source"]},
        )
        store.update_job(project_id, job_id, status="running")
        provider = provider_factory()
        try:
            result = answer_question(
                document, sections, provider, question=question,
                quote=request.quote.model_dump() if request.quote else None,
                block_ids=request.block_ids or None,
                locale=locale, source_sections=source_sections,
                usage_hook=usage_recorder(store, project_id, job_id, "reader_ask"),
            )
            store.update_job(project_id, job_id, status="completed", progress=1,
                             result={"model": result.get("model", ""), "citations": len(result.get("citations") or [])})
            return {**result, "job_id": job_id, "binding": binding, "binding_source": resolution["source"]}
        except Exception as error:
            store.update_job(project_id, job_id, status="failed", error=str(error))
            raise
        finally:
            try:
                provider.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响已拿到的回答
                pass

    @app.get("/projects/{project_id}/usage/records")
    def usage_records(project_id: str):
        return store.usage_records(project_id)

    @app.get("/projects/{project_id}/export/options")
    def export_options_endpoint(project_id: str, locale: str = "zh-CN"):
        return export_options(store, project_id, locale)

    @app.post("/projects/{project_id}/export")
    def export_create_endpoint(project_id: str, request: ExportRequest):
        return create_export(
            store, project_id,
            formats=request.formats, mode=request.mode, locale=request.locale,
            chapters=request.chapters, block_ids=request.block_ids,
            filename=request.filename, missing_policy=request.missing_policy,
        )

    @app.get("/projects/{project_id}/exports")
    def exports_list_endpoint(project_id: str):
        return list_exports(store, project_id)

    @app.patch("/projects/{project_id}/exports/{export_id}")
    def export_rename_endpoint(project_id: str, export_id: str, request: ExportRenameRequest):
        try:
            return rename_export(store, project_id, export_id, request.display_name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="导出记录不存在。")

    @app.delete("/projects/{project_id}/exports/{export_id}")
    def export_delete_endpoint(project_id: str, export_id: str):
        try:
            return delete_export(store, project_id, export_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="导出记录不存在。")

    @app.get("/projects/{project_id}/exports/{export_id}/file")
    def export_file_endpoint(project_id: str, export_id: str, name: str):
        from fastapi.responses import FileResponse
        try:
            path, media_type = resolve_export_file(store, project_id, export_id, name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="导出文件不存在。")
        return FileResponse(path, media_type=media_type, filename=path.name)

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


def _default_app():
    """惰性构造默认应用。

    不模块级调用：否则任何 `import literature.api`（包括测试收集）都会读真实
    LITERATURE_LIBRARY / LITERATURE_RUNTIME、创建 session-token与 uploads 目录。
    """
    try:
        return create_app()
    except RuntimeError:
        return None


def __getattr__(name: str):
    # `from literature.api import app` / `uvicorn literature.api:app` 首次访问时才构造。
    if name == "app":
        value = _default_app()
        globals()["app"] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    try:
        import uvicorn
    except ImportError as error:
        raise SystemExit("缺少 uvicorn，请先安装 Literature 依赖。") from error
    app = _default_app()
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
