from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, NamedTuple, Protocol


KEYCHAIN_SERVICE = "local.literature.providers"

logger = logging.getLogger(__name__)

# 这些 HTTP 状态码代表临时性故障（限流/网关/服务端），值得退避后重试；
# 401/400 等确定性错误不重试，直接报告给用户。
RETRY_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    # 有些错误（例如只返回思考内容、截断）发生时其实已经拿到了服务端响应与 usage；
    # 把它挂在异常上，上层仍能将真实用量记账，而不是把已产生的费用当成零。
    # status_code：429/5xx 等临时故障的状态码。调度层（AIMD 并发自适应）据此
    # 降低并发，而不是把 429 和确定性失败混在一起。
    def __init__(self, message: str, *, response: "ProviderResponse | None" = None, status_code: int = 0) -> None:
        super().__init__(message)
        self.response = response
        self.status_code = int(status_code or 0)


class ProviderTimeoutError(ProviderError):
    def __init__(self, kind: str, seconds: float) -> None:
        self.kind, self.seconds = kind, seconds
        action = {"read": "等待模型响应", "connect": "连接模型服务", "write": "发送模型输入", "pool": "等待模型连接"}[kind]
        hint = (
            "若本任务用量里 reasoning 占比很高，可在模型服务里关闭思考模式或换用非思考模型。"
            if kind == "read" else ""
        )
        super().__init__(f"{action}超时（等待上限 {seconds:g} 秒）。可稍后重试或更换模型服务。{hint}")


class _StreamUnsupported(Exception):
    """流式请求没有得到可用输出（空事件流），应回退非流式重试。"""


def _aggregate_sse_events(lines: Iterable[Any]) -> dict[str, Any] | None:
    """把 SSE 行流聚合成与非流式一致的响应 JSON；无任何有效事件时返回 None。

    思考增量（delta.reasoning_content）只参与保活，不计入译文内容；
    usage/finish_reason/model/id 以事件流为准（include_usage 时 usage 在末尾事件）。
    """
    content_parts: list[str] = []
    finish_reason = ""
    usage: dict[str, Any] = {}
    model = ""
    request_id = ""
    for raw in lines:
        line = (raw or "").strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        model = str(event.get("model") or model)
        request_id = str(event.get("id") or request_id)
        event_usage = event.get("usage")
        if isinstance(event_usage, dict) and event_usage:
            usage = event_usage
        choices = event.get("choices") or []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        piece = delta.get("content")
        if isinstance(piece, str):
            content_parts.append(piece)
        elif isinstance(piece, list):
            content_parts.extend(
                str(item.get("text", "")) for item in piece if isinstance(item, dict)
            )
        reason = choice.get("finish_reason")
        if reason:
            finish_reason = str(reason)
    if not any(part.strip() for part in content_parts) and not usage and not finish_reason:
        return None
    return {
        "model": model,
        "id": request_id,
        "choices": [{"message": {"content": "".join(content_parts)}, "finish_reason": finish_reason}],
        "usage": usage,
    }


class _HTTPStatusError(Exception):
    """携带 httpx 响应的错误，便于外层统一处理重试/报错。"""

    def __init__(self, response: Any, detail: str) -> None:
        super().__init__(detail)
        self.response = response
        self.detail = detail
        self.status_code = int(getattr(response, "status_code", 0) or 0)


def normalize_proxy_environment() -> None:
    """把 NO_PROXY / no_proxy 里的带括号 IPv6 条目规范化为 httpx 可解析的形式。

    httpx 0.28 的 is_ipv6_hostname 不会去掉方括号，导致形如 [::1] 的条目被当成
    普通域名拼成 all://*[::1]，解析时抛 InvalidURL("Invalid port: ':1]'")，
    直接让所有模型请求失败。这里保留代理能力，只修掉无法解析的写法。
    """
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        hosts: list[str] = []
        changed = False
        for host in raw.split(","):
            value = host.strip()
            if not value:
                continue
            if value != "*" and value.startswith("[") and value.endswith("]"):
                value = value[1:-1]
                changed = True
            hosts.append(value)
        if changed:
            os.environ[name] = ",".join(hosts)


PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-5-mini"},
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "glm": {"label": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4.5"},
    "qwen": {"label": "Qwen / 阿里云百炼", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "custom": {"label": "OpenAI-compatible", "base_url": "", "model": ""},
}


@dataclass(slots=True)
class ProviderConfig:
    provider: str
    model: str
    base_url: str
    temperature: float = 0.2
    max_output_tokens: int = 16384
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)
    # 思考模式/预算：`""` 表示“未声明协议”，翻译/结构任务会按模型名自动推断（见
    # infer_reasoning_style），用户显式选择具体协议时以用户选择为准。
    reasoning_style: str = ""
    reasoning_effort: str = ""
    reasoning_budget: int = 0
    # 关思考字段的写法变体（探测得到，见 THINKING_FIELD_VARIANTS / probe_thinking_off）。
    # 不同 OpenAI 兼容服务接受不同写法（顶层 enable_thinking / chat_template_kwargs 等），
    # 空值表示用该风格的默认写法。仅在“关思考”时生效，开思考时始终走标准协议。
    reasoning_field_variant: str = ""

    @classmethod
    def preset(cls, provider: str, **overrides: Any) -> "ProviderConfig":
        if provider not in PROVIDER_PRESETS:
            raise ProviderError(f"未知服务商：{provider}")
        value = PROVIDER_PRESETS[provider]
        return cls(provider=provider, model=overrides.pop("model", value["model"]), base_url=overrides.pop("base_url", value["base_url"]), **overrides)


@dataclass(slots=True)
class ProviderResponse:
    content: str
    provider: str
    model: str
    request_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    estimated_cost: float | None = None
    # 模型停止原因："stop" 正常结束；"length"/"max_tokens" 表示输出被截断。
    # 翻译链路据此判断要不要把整块判为失败并切小块重试，而不是把半句存成完成。
    finish_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class TextProvider(Protocol):
    config: ProviderConfig

    def complete(self, *, system: str, user: str) -> ProviderResponse: ...


# 显式、可审计的思考参数协议。
# `auto`（或留空）表示按模型名推断；只有翻译/结构这类确定性任务才会自动关闭思考，
# 且必须经过 probe_thinking_off 验证字段真的被服务接受（见 api 的翻译启动路径）。
REASONING_STYLES = ("auto", "openai_effort", "qwen_thinking", "glm_thinking")
REASONING_EFFORTS = ("minimal", "low", "medium", "high")
THINKING_TOGGLES = ("on", "off")
# 任务级思考档位（面向用户，可覆盖服务级配置）：
#   inherit = 沿用模型服务上的 reasoning_*；off = 本次强制关思考；
#   low/medium/high = 本次设定思考强度。
REASONING_MODES = ("inherit", "off", "low", "medium", "high")
# “关思考”在同一风格下可能有多种写法，不同 OpenAI 兼容服务只认其中一种：
#   qwen_top_level              → {"enable_thinking": false}（DashScope 兼容模式、部分 vLLM）
#   qwen_chat_template_kwargs   → {"chat_template_kwargs": {"enable_thinking": false}}（vLLM/SGLang）
#   openai_effort_minimal       → {"reasoning_effort": "minimal"}（OpenAI 及同语义网关）
#   glm_toggle                  → {"thinking": {"type": "disabled"}}
THINKING_FIELD_VARIANTS: dict[str, dict[str, Any]] = {
    "qwen_top_level": {"enable_thinking": False},
    "qwen_chat_template_kwargs": {"chat_template_kwargs": {"enable_thinking": False}},
    "openai_effort_minimal": {"reasoning_effort": "minimal"},
    "glm_toggle": {"thinking": {"type": "disabled"}},
}
# “明确不发任何思考字段”的显式状态。与空串不同：空串/`auto` 还会按模型名推断，
# 本值直接停在“不发字段”，供“已探测到所有写法都不生效”的负缓存使用。
NO_REASONING_STYLE = "none"
# 能归因为“请求体字段非法”的状态码。401/403/404/405 等是认证或路由问题，
# 绝不能当成“这个关思考写法不被服务接受”，否则一次密钥过期就写成永久负缓存。
FIELD_REJECTION_STATUS = frozenset({400, 422})
# 400 错误体里**点名**了思考字段的关键词。只用来决定两种 400 回退的先后顺序：
# 点名了字段 → 先摘字段（保住流式，流式对慢模型很重要）；只说“不支持某参数/未知字段”
# 而不点名 → 先回退非流式（这种事更常见于不支持流式，与字段无关）。
# 不参与最终判定：两个闸门依次收敛，两种原因都能兜住。
_EXPLICIT_REASONING_FIELD_HINT = re.compile(
    r"(enable_thinking|thinking_budget|chat_template|reasoning_effort|reasoning_budget|\bthinking\b)",
    re.IGNORECASE,
)

# 每种风格的候选顺序：先试最标准/最省事的写法，被拒或无效再换下一个。
THINKING_OFF_CANDIDATES: dict[str, tuple[str, ...]] = {
    "qwen_thinking": ("qwen_top_level", "qwen_chat_template_kwargs", "openai_effort_minimal"),
    "glm_thinking": ("glm_toggle", "qwen_top_level"),
    "openai_effort": ("openai_effort_minimal",),
}


def infer_reasoning_style(model: str) -> str:
    """按模型名推断思考协议；无法判断时返回空串（保持“不发未声明字段”）。

    只做保守的前缀匹配，宁可漏判也不要把 `enable_thinking` 发给不认识它的服务：
    即使推断错了，字段失效也只是白发一个字段，不会改变译文语义。
    """
    name = str(model or "").strip().lower()
    if not name:
        return ""
    token = name.rsplit("/", 1)[-1]
    if token.startswith("qwen3") or token.startswith("qwq") or "qwen3" in token:
        return "qwen_thinking"
    if token.startswith(("glm-4", "glm4", "glm-z", "glm-z1")):
        return "glm_thinking"
    if token.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
        return "openai_effort"
    return ""


def resolve_reasoning_style(config: Any) -> str:
    """配置里的有效思考协议：NO_REASONING_STYLE 表示明确不发字段；
    显式协议优先；空串/`auto` 则按模型名推断。"""
    style = str(getattr(config, "reasoning_style", "") or "").strip()
    if style == NO_REASONING_STYLE:
        return ""
    if style and style != "auto":
        return style
    return infer_reasoning_style(str(getattr(config, "model", "") or ""))


def disabled_reasoning_effort(style: str) -> str:
    """该协议下“关思考”对应的 effort 取值。"""
    return "off" if style in ("qwen_thinking", "glm_thinking") else "minimal"


def apply_reasoning_mode(config: Any, mode: str, budget: int = 0) -> Any:
    """把任务级思考档位翻译成具体协议字段，返回一个**新**配置；`inherit` 原样返回。

    - inherit：不覆盖，沿用模型服务上的 reasoning_style/effort/budget。
    - off：本次强制关思考（各协议取 disabled_reasoning_effort），优先级高于服务级“显式开”。
    - low/medium/high：openai_effort 直接映射；qwen/glm 这类开关型在三个档位都映射为
      开启（“低”不含“关”之意，避免用户选低却完全没思考）。
    - 协议无法推断（未声明且模型名推断不出）时保持原样：不发未声明字段，与现状一致。
    """
    key = str(mode or "inherit").strip().lower()
    if key not in REASONING_MODES or key == "inherit":
        return config
    style = resolve_reasoning_style(config)
    if not style:
        return config
    updated = copy(config)
    updated.reasoning_style = style
    if key == "off":
        updated.reasoning_effort = disabled_reasoning_effort(style)
    elif style == "openai_effort":
        updated.reasoning_effort = key
    else:
        updated.reasoning_effort = "on"
    try:
        amount = int(budget or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount > 0:
        updated.reasoning_budget = max(0, min(200_000, amount))
    return updated


def reasoning_request_fields(config: Any) -> dict[str, Any]:
    """按有效协议返回请求体附加字段；未声明且无法推断、或取值不合法时返回空。"""
    style = resolve_reasoning_style(config)
    effort = str(getattr(config, "reasoning_effort", "") or "").strip()
    variant = str(getattr(config, "reasoning_field_variant", "") or "").strip()
    try:
        budget = int(getattr(config, "reasoning_budget", 0) or 0)
    except (TypeError, ValueError):
        budget = 0
    # 探测出的变体只在“关思考”时生效；开思考仍走标准协议，避免把变体字段
    # 带进一个语义完全不同的请求。
    variant_fields = THINKING_FIELD_VARIANTS.get(variant) if variant else None
    if style == "openai_effort":
        if effort in REASONING_EFFORTS:
            return {"reasoning_effort": effort}
        if effort == "" and variant_fields is not None:
            return dict(variant_fields)
        return {}
    if style == "qwen_thinking":
        if effort == "on":
            return {"enable_thinking": True, **({"thinking_budget": budget} if budget > 0 else {})}
        if variant_fields is not None and effort != "on":
            return dict(variant_fields)
        fields: dict[str, Any] = {}
        if effort in THINKING_TOGGLES:
            fields["enable_thinking"] = False
        if budget > 0:
            fields["thinking_budget"] = budget
        return fields
    if style == "glm_thinking":
        if effort == "on":
            return {"thinking": {"type": "enabled"}}
        if variant_fields is not None and effort != "on":
            return dict(variant_fields)
        if effort in THINKING_TOGGLES:
            return {"thinking": {"type": "disabled"}}
        return {}
    return {}


def save_api_key(provider: str, api_key: str) -> None:
    api_key = api_key.strip()
    if not api_key:
        raise ProviderError("API key 不能为空。")
    result = subprocess.run(
        ["/usr/bin/security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", provider, "-w", api_key],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ProviderError(result.stderr.strip() or "无法保存 API key。")


def get_api_key(provider: str) -> str:
    environment = os.environ.get(f"LITERATURE_{provider.upper()}_API_KEY", "").strip()
    if environment:
        return environment
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider, "-w"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def delete_api_key(provider: str) -> None:
    subprocess.run(
        ["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider],
        capture_output=True, text=True, check=False,
    )


def extract_json(content: str) -> dict[str, Any]:
    stripped = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, flags=re.S | re.I)
    if fenced:
        stripped = fenced.group(1)
    try:
        value = json.loads(stripped)
    except ValueError as error:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise ProviderError("模型没有返回可解析的 JSON。") from error
        try:
            value = json.loads(stripped[start:end + 1])
        except ValueError as nested:
            raise ProviderError("模型返回的 JSON 格式错误。") from nested
    if not isinstance(value, dict):
        raise ProviderError("模型响应 JSON 顶层必须是对象。")
    return value


# `/v1/models` 的元数据字段各服务写法不一：把常见别名收敛成
# 「上下文窗口 / 最大输出 / 单价」三组，能解析多少算多少，解析不到就留空。
_CONTEXT_FIELD_KEYS = ("context_length", "context_window", "max_context_length", "max_model_len", "max_input_tokens", "n_ctx")
_OUTPUT_FIELD_KEYS = ("max_output_tokens", "max_completion_tokens", "max_tokens", "max_output")
_VERSION_SEGMENT_RE = re.compile(r"/v\d+[a-z0-9]*$", re.I)


def _positive_int(value: Any) -> int:
    """把 `128000` / `"128000"` / `"128k"` 之类的写法收敛成正整数；解析不到返回 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        match = re.search(r"\d[\d_]*", value)
        if match:
            return int(match.group(0).replace("_", ""))
    return 0


def _first_int(sources: Iterable[Any], keys: tuple[str, ...]) -> int:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            number = _positive_int(source.get(key))
            if number:
                return number
    return 0


def _per_million(value: Any, *, per_token: bool = False) -> float | None:
    """OpenRouter 约定 `pricing.prompt/completion` 是「每 token」美元字符串，本应用按「每百万」计价。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    if per_token:
        number *= 1_000_000
    return number


def _empty_metadata(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "context_window": 0,
        "max_output_tokens": 0,
        "input_price_per_million": None,
        "output_price_per_million": None,
    }


def _model_metadata(item: dict[str, Any]) -> dict[str, Any]:
    """从一条 `/v1/models` 记录里提取上下文、输出上限与单价，缺失即为空/0。"""
    top_provider = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
    pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
    input_price = _per_million(pricing.get("prompt"), per_token=True)
    if input_price is None:
        input_price = _per_million(pricing.get("input"))
    if input_price is None:
        input_price = _per_million(item.get("input_price_per_million"))
    output_price = _per_million(pricing.get("completion"), per_token=True)
    if output_price is None:
        output_price = _per_million(pricing.get("output"))
    if output_price is None:
        output_price = _per_million(item.get("output_price_per_million"))
    return {
        "id": str(item.get("id") or item.get("model") or "").strip(),
        "context_window": _first_int((item, top_provider), _CONTEXT_FIELD_KEYS),
        "max_output_tokens": _first_int((item, top_provider), _OUTPUT_FIELD_KEYS),
        "input_price_per_million": input_price,
        "output_price_per_million": output_price,
    }

def _model_list_urls(base_url: str) -> list[str]:
    """Base URL 可能已含 `/v1`（厂商文档常见），也可能只到域名；两种都按 `/v1/models` 优先尝试。"""
    base = (base_url or "").strip().rstrip("/")
    if _VERSION_SEGMENT_RE.search(base):
        return [f"{base}/models"]
    return [f"{base}/v1/models", f"{base}/models"]


def list_remote_models(base_url: str, api_key: str, *, timeout: float = 30.0) -> list[dict[str, Any]]:
    """拉取 OpenAI 兼容的 `/v1/models`，并解析出模型 ID、上下文窗口、输出上限与单价。"""
    base_url = (base_url or "").strip()
    if not base_url:
        raise ProviderError("请填写 Base URL。")
    if not (api_key or "").strip():
        raise ProviderError("请先填写 API Key。")
    try:
        import httpx
    except ImportError as error:
        raise ProviderError("缺少 httpx，请安装 Literature Python 依赖。") from error
    normalize_proxy_environment()
    value: Any = None
    failure: Exception | None = None
    detail = ""
    for url in _model_list_urls(base_url):
        try:
            response = httpx.get(url, headers={"Authorization": f"Bearer {api_key.strip()}"}, timeout=timeout)
            response.raise_for_status()
            value = response.json()
            failure = None
            break
        except Exception as error:  # 404 等按候选 URL 试下一个，最终报最后一个错误
            failure = error
            detail = getattr(getattr(error, "response", None), "text", "")
    if failure is not None:
        raise ProviderError(f"获取模型列表失败：{detail or failure}") from failure
    items = value.get("data") if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise ProviderError("模型列表响应格式无法识别。")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        entry = _model_metadata(item) if isinstance(item, dict) else (_empty_metadata(str(item).strip()) if isinstance(item, str) else None)
        if not entry or not entry["id"] or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        models.append(entry)
    models.sort(key=lambda entry: entry["id"])
    if not models:
        raise ProviderError("该服务没有返回任何模型。")
    return models


class OpenAICompatibleProvider:
    # 标记：本 provider 会在每次 HTTP attempt 调用 attempt_hooks（控制层据此决定是否挂预算钩子）。
    supports_attempt_hooks = True

    def __init__(
        self, config: ProviderConfig, *, api_key: str | None = None, timeout: float = 180.0,
        max_retries: int = 2,
    ) -> None:
        self.config = config
        self.api_key = (api_key if api_key is not None else get_api_key(config.provider)).strip()
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        # 一次任务内复用同一个 httpx.Client：keep-alive 省掉每块的 TCP/TLS 握手，
        # 这是把整本书翻译从「每段都重新建连接」提速下来的关键之一。
        self._client = None
        self._client_lock = threading.Lock()
        # 流式（SSE）请求：默认开启。流式把「读超时」从「等待整份响应的总时限」变成
        # 「两个数据块之间的空闲时限」，思考型模型长时间生成不再整单超时作废。
        # 服务端不支持时自动回退非流式（_stream_disabled 置位，本 provider 实例
        # 不再重试流式；ModelServiceStore 每个作业新建实例，因此新作业会重试一次流式）。
        self._stream_disabled = False
        # 服务端拒绝思考字段时的安全网：置位后本次 HTTP 不再携带 reasoning 字段重发一次。
        # 探测（probe_thinking_off）是主路径；这里保证即使探测被跳过（离线/测试），
        # 一个不认 enable_thinking 的服务也不会把整本书翻译直接 400 掉。
        self._drop_reasoning_fields = False
        # 可选：每次真实 HTTP attempt 进入公共协调层（预算预留/并发/退避）。
        # begin(prompt_chars) 在发请求前调用并返回句柄；end(handle, response, error, final) 在 attempt 后处理。
        # final=True 时预算预留不在 end 释放，而是留在线程本地，由调用方写账后原子 reconcile。
        self.attempt_hooks: tuple[Any, Any] | None = None
        self._attempt_state = threading.local()
        if not self.api_key:
            raise ProviderError(f"尚未配置 {config.provider} API key。")
        if not config.base_url.strip():
            raise ProviderError("兼容服务需要 base URL。")

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        return self._complete(system=system, content=user)

    def with_read_timeout(self, seconds: float) -> "OpenAICompatibleProvider":
        provider = copy(self)
        provider.timeout = seconds
        # 新超时意味着新 Client 配置；不要复用原对象的连接池。
        provider._client = None
        provider._client_lock = threading.Lock()
        return provider

    def with_thinking_disabled(self) -> "OpenAICompatibleProvider":
        """返回一个“思考已尽量关闭”的 provider 副本（不修改原对象）。

        翻译/结构是确定性任务，reasoning 对译文质量近乎零贡献却占掉大部分输出延迟
        （本机实测思考型模型 reasoning 占输出 93.7%，单请求平均 152 秒），因此这两条
        链路默认调用本方法。

        规则：
        - 用户显式声明协议（reasoning_style 为具体风格）且指定了非“关思考”的强度
          （开关=on / openai_effort 的 low/medium/high）时，尊重用户配置，不覆盖。
        - 未声明协议（`""` 或 `auto`）时按模型名推断（infer_reasoning_style）；推断不出
          或用户显式要求开思考时，不发送任何字段，行为与改造前完全一致。
        - 推断出的字段是否被服务接受，由 probe_thinking_off 在实际任务前验证；
          服务返回 400 时 _complete 也会自动摘掉这些字段重发（安全网）。
        """
        declared = (self.config.reasoning_style or "").strip()
        effort = (self.config.reasoning_effort or "").strip()
        style = resolve_reasoning_style(self.config)
        if not style:
            return self
        if effort == "on":
            # 用户显式要求开思考（开关型协议）：不改写配置。报文仍由 reasoning_request_fields
            # 按所声明（或按模型名推断出的）协议拼装。
            return self
        if declared not in ("", "auto") and effort:
            # 显式协议 + 显式非空强度：openai_effort 的 low/medium/high 视为有意开启，
            # 不改写配置（报文仍由 reasoning_request_fields 按所声明协议拼装）。
            return self
        target = disabled_reasoning_effort(style)
        provider = copy(self)
        provider.config = copy(self.config)
        provider.config.reasoning_style = style
        provider.config.reasoning_effort = target
        return provider

    def with_reasoning(self, *, style: str = "", effort: str = "", variant: str = "") -> "OpenAICompatibleProvider":
        """返回一个显式指定思考协议/字段变体的 provider 副本（探测与缓存复用）。"""
        provider = copy(self)
        provider.config = copy(self.config)
        provider.config.reasoning_style = style
        provider.config.reasoning_effort = effort
        provider.config.reasoning_field_variant = variant
        return provider

    def with_max_output_tokens(self, value: int) -> "OpenAICompatibleProvider":
        """返回一个受限于更小输出上限的 provider 副本（用于极小的思考探测请求）。"""
        provider = copy(self)
        provider.config = copy(self.config)
        provider.config.max_output_tokens = max(1, int(value))
        return provider

    def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _client_instance(self, httpx: Any):
        client = self._client
        if client is not None:
            return client
        with self._client_lock:
            if self._client is None:
                timeouts = httpx.Timeout(
                    self.timeout, connect=min(20.0, self.timeout),
                    write=min(60.0, self.timeout), pool=min(30.0, self.timeout),
                )
                self._client = httpx.Client(
                    timeout=timeouts, follow_redirects=True,
                    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                )
            return self._client

    def _retry_delay(self, response: Any, attempt: int) -> float:
        header = response.headers.get("retry-after") if response is not None else None
        if header:
            try:
                return min(30.0, max(0.5, float(header)))
            except (TypeError, ValueError):
                pass
        return min(8.0, 0.8 * (2 ** attempt))

    def complete_vision(self, *, system: str, user: str, images: list[dict[str, Any]]) -> ProviderResponse:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for item in images:
            content.append({"type": "text", "text": f"{item['kind']} page={item['page_index'] + 1} block={item.get('block_id', '')}"})
            content.append({"type": "image_url", "image_url": {"url": item["image_url"], "detail": "high"}})
        return self._complete(system=system, content=content)

    def _read_stream(self, client: Any, url: str, headers: dict[str, str], body: dict[str, Any], timeouts: Any) -> tuple[Any, dict[str, Any]]:
        """发起一次 SSE 流式请求，聚合成与非流式一致的响应 JSON 后返回。

        流式把「读超时」的语义从「等待整份响应的总时限」变成「两个数据块之间的
        空闲时限」：只要模型还在持续吐 token（包括思考型模型的 reasoning 增量），
        长时间生成不会再在总时限处整单超时作废——这是降低翻译失败率的关键。
        思考增量（delta.reasoning_content）只参与保活，不计入译文内容。
        """
        with client.stream("POST", url, headers=headers, json=body, timeout=timeouts) as response:
            if response.status_code >= 400:
                response.read()
                raise _HTTPStatusError(response, response.text)
            content_type = str(response.headers.get("content-type") or "").lower()
            if "text/event-stream" not in content_type:
                # 必须先 read()：真实流式响应未读取前访问 .json()/.text 会抛
                # httpx.ResponseNotRead（它不是 ValueError/HTTPError，不会被下捕）。
                response.read()
                # 中转服务忽略 stream 参数、直接返回完整 JSON：按非流式解析。
                # 解码失败也要归成 _HTTPStatusError（外层转 ProviderError），
                # 否则连续失败熔断对「200+门户 HTML」这类响应失效。
                try:
                    return response, response.json()
                except ValueError as error:
                    # 少数中转返回 SSE 体却标错/缺失 content-type：按文本再试一次 SSE 解析。
                    text = response.text
                    if "data:" in text:
                        value = _aggregate_sse_events(text.splitlines())
                        if value is not None:
                            return response, value
                    raise _HTTPStatusError(response, f"响应不是有效 JSON：{error}") from error
            value = _aggregate_sse_events(response.iter_lines())
            if value is None:
                # 连一个有效事件都没有：视为协议不兼容，回退非流式，而不是伪造成空译文。
                raise _StreamUnsupported("空事件流")
            return response, value

    def _complete(self, *, system: str, content: str | list[dict[str, Any]]) -> ProviderResponse:
        try:
            import httpx
        except ImportError as error:
            raise ProviderError("缺少 httpx，请安装 Literature Python 依赖。") from error
        # 代理环境变量在 httpx 创建 Client 时解析，必须先规范化再发请求。
        normalize_proxy_environment()
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", **self.config.extra_headers}
        base_body = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        timeouts = httpx.Timeout(self.timeout, connect=min(20.0, self.timeout),
                                 write=min(60.0, self.timeout), pool=min(30.0, self.timeout))
        client = self._client_instance(httpx)
        prompt_chars = len(system) + (len(content) if isinstance(content, str) else 0)
        response = None
        value: dict[str, Any] = {}
        attempt = 0
        while True:
            use_stream = not self._stream_disabled
            body = dict(base_body)
            # 思考字段在循环内合并：400 安全网置位 _drop_reasoning_fields 后，
            # 同一次 complete 的重发才能真正摘掉字段（循环外合并就无法生效）。
            if not self._drop_reasoning_fields:
                body.update(reasoning_request_fields(self.config))
            if use_stream:
                body["stream"] = True
                body["stream_options"] = {"include_usage": True}
            handle = self.attempt_hooks[0](prompt_chars) if self.attempt_hooks else None
            try:
                if use_stream:
                    response, value = self._read_stream(client, url, headers, body, timeouts)
                else:
                    response = client.post(url, headers=headers, json=body, timeout=timeouts)
                    if response.status_code >= 400:
                        raise _HTTPStatusError(response, response.text)
                    # 解码失败也归成 _HTTPStatusError（外层转 ProviderError）：否则
                    # 「200+门户 HTML」不触发连续失败熔断，且调用方捕获不到 ProviderError。
                    try:
                        value = response.json()
                    except ValueError as error:
                        raise _HTTPStatusError(response, f"响应不是有效 JSON：{error}") from error
            except httpx.TimeoutException as error:
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, error, True)
                kind = "connect" if isinstance(error, httpx.ConnectTimeout) else "write" if isinstance(error, httpx.WriteTimeout) else "pool" if isinstance(error, httpx.PoolTimeout) else "read"
                # 超时可能已经产生费用，不自动重发；由上层按分块重试。
                raise ProviderTimeoutError(kind, getattr(timeouts, kind)) from error
            except httpx.HTTPError as error:
                final = attempt >= self.max_retries
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, error, final)
                if not final:
                    time.sleep(self._retry_delay(None, attempt))
                    attempt += 1
                    continue
                raise ProviderError(f"模型请求失败：{error}") from error
            except _StreamUnsupported:
                # 空事件流：服务端可能不兼容流式协议，关闭流式后同一 attempt 预算内重发。
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, None, False)
                self._stream_disabled = True
                continue
            except httpx.StreamError as error:
                # 流式读取异常（含 ResponseNotRead 等）：它不是 httpx.HTTPError，
                # 不萘既有的 HTTPError/ValueError 分支；必须归成 ProviderError，
                # 否则连续失败熔断对这类响应失效（与「200+门户 HTML」同型缺口）。
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, error, True)
                raise ProviderError(f"模型请求失败：{error}") from error
            except _HTTPStatusError as error:
                if error.status_code == 400:
                    # 400 有两种常见原因：服务不支持流式、服务不认思考字段。两者都不能只凭
                    # “带不带思考字段”区分，因此先用 error.detail 里的关键词定优先级，
                    # 再用两个单调闸门（_stream_disabled / _drop_reasoning_fields）依次收敛：
                    #   点名了思考字段 → 先摘字段（保住流式，流式对慢模型很重要）
                    #   其余（含 “stream not supported”“unknown field”等不点名的情况）→
                    #   先回退非流式。这样优先保住的总是“关思考”这个主要收益。
                    # 无论哪种先试，另一个后试，因此两种原因都能兜住，不会死循环。
                    field_suspect = bool(_EXPLICIT_REASONING_FIELD_HINT.search(error.detail or ""))
                    try_fields_first = field_suspect and not self._drop_reasoning_fields and bool(reasoning_request_fields(self.config))
                    if try_fields_first:
                        if self.attempt_hooks:
                            self.attempt_hooks[1](handle, error.response, None, False)
                        self._drop_reasoning_fields = True
                        continue
                    if use_stream:
                        # 服务端拒绝流式请求（不支持 stream/stream_options 等）：
                        # 同一 attempt 的预算预留不浪费，关闭流式后立即重发一次。
                        if self.attempt_hooks:
                            self.attempt_hooks[1](handle, error.response, None, False)
                        self._stream_disabled = True
                        continue
                    if not self._drop_reasoning_fields and reasoning_request_fields(self.config):
                        # 非流式仍然 400：这时才能真正把原因归给思考字段。
                        if self.attempt_hooks:
                            self.attempt_hooks[1](handle, error.response, None, False)
                        self._drop_reasoning_fields = True
                        continue
                if error.status_code in RETRY_STATUS_CODES and attempt < self.max_retries:
                    # 429/5xx 且还会重试：释放本次 attempt 的预留并记退避冷却。
                    if self.attempt_hooks:
                        self.attempt_hooks[1](handle, error.response, None, False)
                    time.sleep(self._retry_delay(error.response, attempt))
                    attempt += 1
                    continue
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, error.response, error, True)
                raise ProviderError(
                    f"模型请求失败：{error.detail or error}", status_code=error.status_code,
                ) from error
            if self.attempt_hooks:
                self.attempt_hooks[1](handle, response, None, True)
            break
        try:
            choice = value["choices"][0]
            message = choice["message"]
            raw_content = message["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError("模型响应缺少 choices[0].message.content。") from error
        finish_reason = str(choice.get("finish_reason") or "")
        if isinstance(raw_content, list):
            content = "".join(str(item.get("text", "")) for item in raw_content if isinstance(item, dict))
        else:
            content = raw_content
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cost = None
        if self.config.input_price_per_million is not None and self.config.output_price_per_million is not None:
            cost = round((input_tokens * self.config.input_price_per_million + output_tokens * self.config.output_price_per_million) / 1_000_000, 8)
        result = ProviderResponse(
            content=str(content), provider=self.config.provider, model=str(value.get("model") or self.config.model),
            request_id=str((response.headers.get("x-request-id") if response is not None else "") or value.get("id") or ""),
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=int(prompt_details.get("cached_tokens") or usage.get("cached_tokens") or 0),
            reasoning_tokens=int(completion_details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0),
            estimated_cost=cost, finish_reason=finish_reason, raw=value,
        )
        if not str(content or "").strip():
            reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
            if reasoning:
                # 仍把已获取的响应/usage 交给上层记账。
                raise ProviderError("模型只返回了思维链内容，没有给出译文；请更换模型或关闭思考模式后重试。", response=result)
        return result


# 关思考探测：一个极小的真实请求，只用来判断“服务是否真的接受了这个字段”。
# 用 max_tokens 截断思考型模型的输出：即使思考开着，探测本身也只需要几秒。
PROBE_MAX_TOKENS = 96
PROBE_SYSTEM = "你是翻译引擎，只输出译文，不要解释、不要 Markdown。"
PROBE_USER = "Translate into Chinese: The quick brown fox jumps over the lazy dog."
# 探测单请求的等待上限：探测只是一次正确性检查，不能把作业启动卡在服务的长尾上。
PROBE_TIMEOUT_SECONDS = 60.0
# reasoning 占输出的比例超过这个值就认为“思考没关掉”。
PROBE_REASONING_SHARE = 0.25


def _close_probe_client(provider: Any, child: Any) -> None:
    """关闭探测子 provider 自建、且不与父 provider 共享的 HTTP 连接池。

    `copy()` 会连 `_client` 一起复制：父对象已有连接池时不能关（会把作业的连接池关掉），
    只有子对象自己新建的（父对象为 None）才需要在此释放，否则每次探测都漏一个客户端。
    """
    if child is None:
        return
    client = getattr(child, "_client", None)
    if client is None or client is getattr(provider, "_client", None):
        return
    try:
        client.close()
    except Exception:  # noqa: BLE001 - 释放失败不影响探测结论
        logger.debug("探测子连接池关闭失败", exc_info=True)


class _ProbeResult(NamedTuple):
    """一次探测请求的观察结果。

    - ``response``：响应（可能是从 ProviderError 上取回的——“只返回思维链”也带完整 usage）。
    - ``field_rejected``：本次请求因 400 被安全网摘掉思考字段后重发过 → 该写法不被接受。
    - ``stream_disabled``：为了绕过“不支持流式”才关掉的流式（不能归咎于思考字段）。
    """

    response: Any
    field_rejected: bool
    stream_disabled: bool


def _probe_attempt(provider: Any, *, style: str, effort: str, variant: str) -> _ProbeResult:
    """发一次极小探测请求；子 provider 的超时与自建连接池都在此收敛。"""
    child = provider.with_reasoning(style=style, effort=effort, variant=variant)
    child = child.with_max_output_tokens(PROBE_MAX_TOKENS)
    if hasattr(child, "timeout"):
        # 只收窄单次请求的等待上限，不重建连接池（探测应复用作业的 keep-alive 连接）。
        child.timeout = min(float(child.timeout or PROBE_TIMEOUT_SECONDS), PROBE_TIMEOUT_SECONDS)
    try:
        response = child.complete(system=PROBE_SYSTEM, user=PROBE_USER)
    except ProviderError as error:
        _close_probe_client(provider, child)
        attached = getattr(error, "response", None)
        if attached is None:
            raise
        # “只返回了思维链”这类错误带着完整 usage，仍可作为判定依据。
        return _ProbeResult(
            response=attached,
            field_rejected=bool(getattr(child, "_drop_reasoning_fields", False)),
            stream_disabled=bool(getattr(child, "_stream_disabled", False)),
        )
    except BaseException:
        _close_probe_client(provider, child)
        raise
    result = _ProbeResult(
        response=response,
        field_rejected=bool(getattr(child, "_drop_reasoning_fields", False)),
        stream_disabled=bool(getattr(child, "_stream_disabled", False)),
    )
    _close_probe_client(provider, child)
    return result


def _settle_streaming_support(provider: Any) -> bool:
    """先把“服务是否支持流式”定下来，返回是否可继续探测。

    用一个**不带任何思考字段**的极小请求：这样任何 400 都不可能归因于字段，而只能来自
    流式协议（provider 的安全网会自动关流式重发）。把“关过流式”提升到父 provider 上，
    后续候选就在同一个（非流式）前提下比较，字段结论才干净——这修复了“非流式服务返回
    通用 400 文案导致思考字段被误摘、候选被误判为被拒”的组合缺陷。

    认证/路由/限流/网络故障一律返回 False：探测放弃，不写缓存。
    """
    if getattr(provider, "_stream_disabled", False):
        return True
    try:
        result = _probe_attempt(provider, style=NO_REASONING_STYLE, effort="", variant="")
    except ProviderError as error:
        logger.warning("关思考探测前置检查失败：%s", error)
        return False
    except Exception as error:  # noqa: BLE001 - 前置检查失败不能拖垮作业
        logger.warning("关思考探测前置检查失败：%s", error)
        return False
    if result.stream_disabled:
        # 真实作业也会落到同样的非流式路径，先替它定下来，省掉一个 400 往返。
        provider._stream_disabled = True
        logger.info("该服务不接受流式请求：关思考探测与后续请求改为非流式")
    return True


def probe_thinking_off(provider: Any) -> dict[str, Any] | None:
    """探测该服务上真正能关闭思考的字段写法，返回可供 ``with_reasoning`` 的参数。

    返回值有三种含义，调用方必须区分：
    - ``{"style", "effort", "variant"}``：探测到生效写法。
    - ``{}``：所有候选都试过了且都不生效（包括被 400 拒收、或被服务静默忽略）。
    - ``None``：探测被临时故障（限流/5xx/网络/认证）打断，结论未知，**不能写进缓存**，
      否则一次服务抖动就把这个服务永久钉在“开关不掉”的结论上。

    - 只探测已知风格的候选；推断不出风格时返回 ``{}``（无需探测）。
    - 先定下流式支持状态，再逐候选发 ``PROBE_MAX_TOKENS`` 的极小请求；
      这样任何 400 都能干净地归因到“字段”，不会与流式协议混淆。
    - 拿到响应后看 ``reasoning_tokens`` 占比：接近 0 才算真的关掉了思考。
    - “只返回了思维链”这类错误带着完整 usage，同样参与判定，而不是当瞬时故障中止。
    - 有候选根本拿不到 usage（服务不回 usage）时返回 ``None``：量不出来就不能下结论，
      否则会把“无 usage”误固化成“这个服务开关不掉思考”的永久负缓存。
    """
    if getattr(provider, "attempt_hooks", None) is not None:
        # 协调层钩子已挂：探测请求会占用服务信号量/项目预算，且与真实作业的 attempt
        # 状态互相污染（同一个 _attempt_state）。此时不探测，退回默认字段。
        logger.warning("provider 已挂请求协调钩子，跳过关思考探测")
        return None
    style = resolve_reasoning_style(getattr(provider, "config", None))
    candidates = THINKING_OFF_CANDIDATES.get(style)
    if not candidates:
        return {}
    if not _settle_streaming_support(provider):
        return None
    effort = disabled_reasoning_effort(style)
    undecided = False
    for variant in candidates:
        try:
            result = _probe_attempt(provider, style=style, effort=effort, variant=variant)
        except ProviderError as error:
            status = int(getattr(error, "status_code", 0) or 0)
            if status in FIELD_REJECTION_STATUS:
                continue  # 该字段被服务拒绝，试下一个写法。
            # 429/5xx/超时/认证失败/路由错误：结论未知，不写缓存，下次作业再试。
            logger.warning("关思考探测中断（%s，status=%s）：%s", variant, status, error)
            return None
        except Exception as error:  # noqa: BLE001 - 探测失败不能拖垮翻译作业
            logger.warning("关思考探测中断（%s）：%s", variant, error)
            return None
        if result.field_rejected:
            # provider 的安全网已因 400 摘掉字段并重发成功：这个写法不被服务接受。
            # 不能把它当成生效写法，否则缓存值会在每个新 provider 实例上白发一次 400。
            continue
        output = int(getattr(result.response, "output_tokens", 0) or 0)
        reasoning = int(getattr(result.response, "reasoning_tokens", 0) or 0)
        if output <= 0:
            undecided = True  # 服务不返回 usage：量不出 reasoning 占比，不能下结论。
            continue
        if reasoning / output <= PROBE_REASONING_SHARE:
            return {"style": style, "effort": effort, "variant": variant}
    if undecided:
        logger.warning("关思考探测无法判定（该服务不返回 usage）：不写缓存")
        return None
    return {}


class MockProvider:
    """Deterministic provider used by tests and the UI demo project.

    同时兼容两种调用：结构化翻译（输入是一串 ``<src id="...">`` 段）与
    即时/连通性探测（输入是普通文本）。前者按段回吐 ``<seg id="...">``。
    """

    def __init__(self, translations: dict[str, str] | None = None) -> None:
        self.config = ProviderConfig.preset("custom", model="mock", base_url="mock://local")
        self.translations = translations or {}

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
        if segments:
            parts = []
            for seg_id, text in segments:
                translated = self.translations.get(seg_id, f"译：{text.strip()}")
                parts.append(f'<seg id="{seg_id}">{translated}</seg>')
            content = "\n".join(parts)
            return ProviderResponse(
                content=content, provider="mock", model="mock", request_id=uuid.uuid4().hex,
                input_tokens=len(user) // 4, output_tokens=len(content) // 4,
            )
        content = f"译：{user}"
        return ProviderResponse(content=content, provider="mock", model="mock", request_id=uuid.uuid4().hex, input_tokens=len(user) // 4, output_tokens=len(content) // 4)
