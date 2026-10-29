from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
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
    # possible_charge：本次失败是否“可能已经产生费用”。None = 未知（外部按保守口径处理）；
    # True = 可能已计费（读/写超时、流已开始、5xx）；False = 确定不计费（连接失败、4xx 等）。
    possible_charge: bool | None = None

    def __init__(
        self, message: str, *,
        response: "ProviderResponse | None" = None, status_code: int = 0,
        possible_charge: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.response = response
        self.status_code = int(status_code or 0)
        if possible_charge is not None:
            self.possible_charge = bool(possible_charge)


class ProviderTimeoutError(ProviderError):
    def __init__(self, kind: str, seconds: float) -> None:
        self.kind, self.seconds = kind, seconds
        # 读/写超时、总时限越线都可能已经产生费用（流里已收到数据）；
        # 连接/连接池超时不可能计费。
        self.possible_charge = kind in {"read", "write", "total"}
        action = {
            "read": "等待模型响应", "connect": "连接模型服务", "write": "发送模型输入",
            "pool": "等待模型连接", "total": "模型请求总时限",
        }[kind]
        hint = (
            "若本任务用量里 reasoning 占比很高，可在模型服务里关闭思考模式或换用非思考模型。"
            if kind == "read" else ""
        )
        super().__init__(f"{action}超时（等待上限 {seconds:g} 秒）。可稍后重试或更换模型服务。{hint}")


class _StreamUnsupported(Exception):
    """流式请求没有得到可用输出（空事件流），应回退非流式重试。"""


class _TotalDeadlineExceeded(Exception):
    """单请求总时限（with_request_limits 的 total_seconds）越线。

    与 socket 层空闲超时（ProviderTimeoutError kind="read"）分开：total 只在流里
    已收到数据后才可能触发（空闲等待先被 read 超时兜住），因此按「可能已计费」处理。
    """

    def __init__(self, seconds: float) -> None:
        super().__init__(f"总时限 {seconds:g} 秒")
        self.seconds = seconds


class _OutputCapExceeded(Exception):
    """传输途中输出字节数已超单请求上限（服务端忽略 max_tokens 的失控输出）。

    在收到数据时就中止读取，不能等整份响应读完才复核——48k token 的失控输出
    会白白拉满总时限并产生大额费用。费用未知（按 possible_charge=True 上报）。
    """

    def __init__(self, bytes_seen: int, cap_bytes: int) -> None:
        super().__init__(f"已收 {bytes_seen} 字节 ≥ 上限 {cap_bytes} 字节")
        self.bytes_seen = bytes_seen
        self.cap_bytes = cap_bytes


class _StreamInterrupted(ProviderError):
    """流式已读到部分数据后断开：可能已经产生费用，不能当作“没花钱”自动重发。"""

    possible_charge = True


class _SSEErrorEvent(ProviderError):
    """服务端在 HTTP 200 的 SSE 流里回了一个 error 事件（限流/配额/上游故障）。"""

    possible_charge = True


def _sse_error_status(error_value: Any) -> int:
    """把 SSE error 事件里的文本归一到更接近真实原因的 HTTP 状态码（只作分类，不冒充证据）。"""
    if isinstance(error_value, str):
        text = error_value
    else:
        try:
            text = json.dumps(error_value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(error_value)
    lowered = text.lower()
    if "rate" in lowered and "limit" in lowered:
        return 429
    if any(token in lowered for token in ("insufficient", "quota", "balance", "exceeded") ):
        return 402
    if any(token in lowered for token in ("unauthorized", "invalid api key", "authentication", "auth failed")):
        return 401
    if "not found" in lowered or "no such model" in lowered:
        return 404
    return 502


def _sse_error_message(error_value: Any) -> str:
    if isinstance(error_value, dict):
        message = error_value.get("message") or error_value.get("type") or error_value.get("code") or ""
        if message:
            return redact_error_text(message)
    return redact_error_text(error_value)


# 上游错误原文往往是一整页 HTML，甚至可能回显密钥，不能原样写进长期数据文件。
_ERROR_TEXT_LIMIT = 300
_SECRET_TEXT_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+[A-Za-z0-9._\-]{6,}|[A-Za-z0-9_\-]{32,})",
    re.IGNORECASE,
)


def redact_error_text(value: Any) -> str:
    """把上游错误文本限长并脱敏（掩掉疑似密钥/长 token），供写入日志与数据文件。"""
    text = str(value if value is not None else "")
    text = _SECRET_TEXT_RE.sub("[REDACTED]", text)
    if len(text) > _ERROR_TEXT_LIMIT:
        text = text[:_ERROR_TEXT_LIMIT] + "…(已截断)"
    return text


def _aggregate_sse_events(lines: Iterable[Any], on_output: Any = None) -> dict[str, Any] | None:
    """把 SSE 行流聚合成与非流式一致的响应 JSON；无任何有效事件时返回 None。

    思考增量（delta.reasoning_content）只参与保活，不计入译文内容；
    usage/finish_reason/model/id 以事件流为准（include_usage 时 usage 在末尾事件）。

    ``on_output(piece)``：每个语义输出增量（delta.content / delta.reasoning_content）
    回调一次，供 with_request_limits 的输出上限按**解码后文本字节数**计量
    （SSE/JSON 框架字节不计入语义上限，只计入宽松的原始封套安全上限）。回调抛出
    的异常原样上抛（上限越界即中止读取）。
    """
    content_parts: list[str] = []
    finish_reason = ""
    usage: dict[str, Any] = {}
    model = ""
    request_id = ""
    saw_done = False
    for raw in lines:
        line = (raw or "").strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            saw_done = True
            break
        try:
            event = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            # 部分中转在 HTTP 200 的流里用 error 事件报限流/配额/上游故障；
            # 忽略它会把被截断的输出当成完整结果，或把限流误判成“流式不兼容”而重发。
            raise _SSEErrorEvent(_sse_error_message(event["error"]), status_code=_sse_error_status(event["error"]))
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
            if on_output is not None and piece:
                on_output(piece)
        elif isinstance(piece, list):
            for item in (str(x.get("text", "")) for x in piece if isinstance(x, dict)):
                content_parts.append(item)
                if on_output is not None and item:
                    on_output(item)
        reasoning_piece = delta.get("reasoning_content")
        if on_output is not None and isinstance(reasoning_piece, str) and reasoning_piece:
            on_output(reasoning_piece)   # 思考增量也是真实输出，计入输出上限
        reason = choice.get("finish_reason")
        if reason:
            finish_reason = str(reason)
    if not any(part.strip() for part in content_parts) and not usage and not finish_reason:
        return None
    if not finish_reason and not saw_done:
        # usage 不等于正常终止：有些中转只在末尾回 usage，却没有 [DONE] / finish_reason。
        # 缺终止标记的流按“不完整”上报（可能被截断），宁可多一次小批重试，不能当完整结果。
        finish_reason = "incomplete_stream"
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


def _is_loopback_host(host: str) -> bool:
    """仅把精确的 localhost 与真正的回环 IP（127.0.0.0/8、::1）视为本机。

    不能用前缀匹配：`127.attacker.example` 这类公网域名会以前缀 `127.` 误判为本机。
    也不把 `0.0.0.0` 当作 loopback（它是“所有接口”通配地址，保守拒绝携带密钥）。
    """
    if host == "localhost":
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def plaintext_http_blocked(base_url: str, *, allow_plaintext: bool = False) -> bool:
    """地址是明文 ``http://`` 且主机不是本机 loopback 时返回 True。

    API Key 走 ``Authorization`` 头；对远程主机用明文 HTTP 会把密钥暴露在链路上。
    本地网关/开发服务（localhost、127.0.0.0/8、[::1]）保留 http 契约，不受限制。

    ``allow_plaintext=True`` 表示用户已经对这个服务显式确认过风险
    （内网/自建 MaaS 常常没有 https），此时不再拦截；调用方负责在日志里留痕。
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(str(base_url or ""))
    except ValueError:
        return False
    if (parsed.scheme or "").lower() != "http":
        return False
    if allow_plaintext:
        return False
    return not _is_loopback_host((parsed.hostname or "").lower())


def assert_key_transport_safe(base_url: str, api_key: str, *, allow_plaintext: bool = False) -> None:
    """在携带密钥发请求前调用；阻止把密钥发往明文 HTTP 的远程地址。

    默认拦截（安全默认）；只有用户在服务配置里显式允许明文 HTTP 时才放行。
    """
    if (api_key or "").strip() and plaintext_http_blocked(base_url, allow_plaintext=allow_plaintext):
        raise ProviderError(
            "为避免 API Key 经明文 HTTP 泄露，远程服务必须使用 https:// 地址；"
            "本机 loopback（localhost / 127.0.0.1 / [::1]）不受限制。"
            "若这是自建/内网服务，请在「设置 → 模型服务」里勾选「允许明文 HTTP」后重试。"
        )


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


def normalize_tls_environment() -> list[str]:
    """清理指向不存在文件的 TLS 信任库环境变量（SSL_CERT_FILE / SSL_CERT_DIR 等）。

    httpx/requests 默认 ``trust_env=True``：env 里若有 ``SSL_CERT_FILE``，会用它
    代替内置 CA 包（certifi）；路径失效（例如指向已删除的临时目录、或另一个
    应用的私有证书包）时，``ssl.create_default_context`` 在发请求前就抛
    ``FileNotFoundError``，表现为「所有 HTTPS 请求莫名失败」。这里只剔除**不可用**
    的路径，保留存在的自定义信任库（企业自签场景），且**绝不关闭证书校验**。
    返回被清理的变量名（供调用方记日志）。
    """
    removed: list[str] = []
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_DIR"):
        raw = os.environ.get(name)
        if not raw:
            continue
        candidate = os.path.expanduser(raw.strip())
        usable = os.path.exists(candidate) if name != "SSL_CERT_DIR" else os.path.isdir(candidate)
        if usable:
            continue
        os.environ.pop(name, None)
        removed.append(name)
    return removed


def tls_context_error(error: BaseException) -> str:
    """把 TLS 初始化失败翻译成可操作的中文说明（不泄漏任何密钥）。"""
    detail = str(error) or error.__class__.__name__
    return (
        f"无法建立 HTTPS 连接（TLS 证书链不可用：{detail}）。"
        "通常是运行环境的证书配置（SSL_CERT_FILE/SSL_CERT_DIR 或内置 CA 包）异常；"
        "证书校验不会被关闭。请重启 Literature 后重试；若仍失败，请把这段错误反馈给开发者。"
    )


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
    # 用户显式确认「该服务是内网/自建服务，允许用明文 HTTP 发送 API Key」。
    # 默认 False：公网明文 http 依旧被 assert_key_transport_safe 拦截，
    # 否则一次误填就会把密钥暴露在链路上。
    allow_plaintext_http: bool = False

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


def _key_store_file() -> Path:
    """非 macOS 平台的密钥文件（用户目录默认仅当前用户可读）。"""
    configured = os.environ.get("LITERATURE_RUNTIME", "").strip()
    if configured:
        root = Path(configured).expanduser()
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        root = Path(base) / "Literature"
    else:
        root = Path.home() / "Library" / "Application Support" / "Literature"
    return root / "api-keys.json"


_KEY_STORE_LOCK = threading.Lock()


def _read_file_keys() -> dict[str, str]:
    path = _key_store_file()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _write_file_keys(keys: dict[str, str]) -> None:
    path = _key_store_file()
    if not keys:
        # 全部删除后把文件也清掉，不留空密钥文件。
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(keys, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def save_api_key(provider: str, api_key: str) -> None:
    api_key = api_key.strip()
    if not api_key:
        raise ProviderError("API key 不能为空。")
    if sys.platform != "darwin":
        # Windows/Linux 没有封装好的系统钥匙串：落到运行目录的用户专属文件。
        with _KEY_STORE_LOCK:
            keys = _read_file_keys()
            keys[provider] = api_key
            _write_file_keys(keys)
        return
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
    if sys.platform != "darwin":
        with _KEY_STORE_LOCK:
            return _read_file_keys().get(provider, "")
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", provider, "-w"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def delete_api_key(provider: str) -> None:
    if sys.platform != "darwin":
        with _KEY_STORE_LOCK:
            keys = _read_file_keys()
            if keys.pop(provider, None) is not None:
                _write_file_keys(keys)
        return
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


def list_remote_models(
    base_url: str, api_key: str, *, timeout: float = 30.0, allow_plaintext_http: bool = False,
) -> list[dict[str, Any]]:
    """拉取 OpenAI 兼容的 `/v1/models`，并解析出模型 ID、上下文窗口、输出上限与单价。"""
    base_url = (base_url or "").strip()
    if not base_url:
        raise ProviderError("请填写 Base URL。")
    if not (api_key or "").strip():
        raise ProviderError("请先填写 API Key。")
    assert_key_transport_safe(base_url, api_key, allow_plaintext=allow_plaintext_http)
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
            detail = redact_error_text(getattr(getattr(error, "response", None), "text", ""))
    if failure is not None:
        raise ProviderError(f"获取模型列表失败：{redact_error_text(detail or failure)}") from failure
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
        # 明文 HTTP 放行的日志只打一次（按用户显式配置放行，仍要留痕）。
        self._plaintext_warned = False
        # 单请求传输时限 + 输出上限 + 活动回调（仅 with_request_limits 副本非 None）。
        # 默认 None：其余请求方（翻译/结构/探测等）的默认行为与限额完全不变。
        self._request_limits: dict[str, float] | None = None
        self._on_activity: Any = None
        # 可自动重试的状态码（with_request_limits 副本收窄为 {429}：
        # 5xx/408 等可能已计费，不能隐藏重发）。
        self._retry_status_codes: frozenset[int] = frozenset(RETRY_STATUS_CODES)
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

    def with_request_limits(
        self, *, idle_seconds: float = 90.0, total_seconds: float = 180.0,
        max_output_tokens: int = 4000, on_activity: Any = None,
    ) -> "OpenAICompatibleProvider":
        """返回一个**单请求专用**的 provider 副本：传输时限 + 输出上限 + 活动回调。

        时限语义（SSE 与非流式回退都覆盖）：
        - connect ≤ 20s（与全库一致的上限，不随 total 拉长）。
        - idle_seconds：空闲上限。SSE 下 = 两个数据块之间的最大间隔（httpx read 超时，
          思考增量也保活）；非流式回退下 = 等待响应字节的读超时（服务端整段静默时同样
          受它约束）。超时抛 ``ProviderTimeoutError(kind="read")``，按「可能已计费」。
        - total_seconds：单请求总时限。SSE 在逐事件处检查绝对 deadline（连续吐 token
          的流不会绕过它）；非流式回退同样在逐字节读取处检查。越线抛
          ``ProviderTimeoutError(kind="total")``，按「可能已计费」。
        - 全程只用 socket 超时、读取循环内的 deadline 检查，以及一个到点 close 流、
          事后必被 cancel 的看门狗 Timer（兑住「阻塞在 read 上」的越线）；请求结束
          （成功/失败/回退）后看门狗已 cancel，无悬挂定时器。
        - 总时限按**整个 complete**（含 400 协议回退后的重发）计：deadline 在进入
          循环前起算，不因回退重发而重新拉满；每次真实传输的等待也被看门狗兑在
          剩余时限内。
        - 重试口径：副本对 5xx/408 等可能已计费的状态**不自动重发**，只允许
          明确未计费的 429 与连接失败重试，以及显式 400 协议回退。

        隔离：副本有独立连接池与独立配置（max_output_tokens 取 max_output_tokens），
        调用方用完应 ``close()`` 副本；原 provider 与其他请求方的默认行为完全不变。
        流式/思考字段回退状态（``_stream_disabled``/``_drop_reasoning_fields``）**从原
        provider 继承**：那是能力探测结论（服务不支持流式/不认思考字段），继承可避免
        每个请求副本都重复一次已被拒绝的请求；副本内新学到的回退不写回原对象。

        输出上限：**传输途中**按已收字节数复核（上限 ≈ max_output_tokens×12 字节 +
        响应封套余量）：服务端若忽略 max_tokens 参数持续生成，读取在越界当下即中止
        （``ProviderError``、``possible_charge=True``，费用未知、不自动重发）；响应完整
        拿到后再按真实 usage（缺失时按内容保守估算）复核一次。两种路径都不改写上游
        ``finish_reason``（原样保留），超限信号只体现在错误本身与 ``error.response``
        上供调用方记账。

        活动回调：``on_activity(dict)``，请求发出时收到一次
        ``{"state"/"stage": "waiting", ...}``，收到数据后收到
        ``{"state"/"stage": "generating", ...}``（每 0.5s 节流），字段：
        ``request_started_at`` / ``last_response_at``（ISO 8601 UTC 字符串，另附
        ``*_epoch`` Unix 秒浮点）、``elapsed_seconds``。回调异常只记 debug，不影响请求。"""
        provider = copy(self)
        provider.config = copy(self.config)
        cap = max(1, int(max_output_tokens))
        provider.config.max_output_tokens = cap
        provider.timeout = float(total_seconds)
        # 完全隔离：独立连接池，本请求内的连接不与共享实例共用。
        # _stream_disabled/_drop_reasoning_fields（能力回退结论）随 copy() 从原 provider
        # 继承——不重置，避免每个副本都重复一次已被服务拒绝的请求。
        provider._client = None
        provider._client_lock = threading.Lock()
        # 限流/网关类 5xx 的自动重发可能已计费：副本只重试明确未计费的 429。
        provider._retry_status_codes = frozenset({429})
        provider._request_limits = {
            "idle_seconds": max(0.05, float(idle_seconds)),
            "total_seconds": max(0.1, float(total_seconds)),
            "max_output_tokens": float(cap),
        }
        provider._on_activity = on_activity
        return provider

    # 语义输出上限（传输中，按解码后的 delta 文本字节计）：token×4 字节 + 余量。
    # 英文 token ≈4 字符、CJK ≈3 UTF-8 字节；4 字节/token + 事件余量对合法输出宽松，
    # 对真正的失控输出（数万 token）能及早中止。
    OUTPUT_SEMANTIC_BYTE_PER_TOKEN = 4
    OUTPUT_SEMANTIC_ENVELOPE = 8192
    # 原始封套安全上限（宽松）：SSE/JSON 框架、usage、role 等不计入语义上限，
    # 但整条流也不能无限吃内存/带宽——取 max(1 MiB, 语义上限×64)。
    OUTPUT_RAW_MIN_BYTES = 1024 * 1024
    OUTPUT_RAW_MULTIPLE = 64

    @staticmethod
    def _estimate_output_tokens(text: str) -> int:
        """无 usage 时的输出 token 保守估算：CJK 按字计，其余按 4 字符/token。

        只用于 with_request_limits 的输出上限复核；宁严勿漏（高估最多让一个确实
        超限界的请求被判失败，低估会让截断内容混成完成结果）。"""
        value = str(text or "")
        cjk = sum(
            1 for ch in value
            if "\u2e80" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"
        )
        return cjk + (len(value) - cjk + 3) // 4

    def _activity_emitters(self) -> tuple[Any, Any]:
        """构造 (on_waiting, on_data) 活动发射器；未挂 on_activity 时返回 (None, None)。"""
        callback = self._on_activity
        if callback is None:
            return None, None
        started = time.time()
        state = {"waiting_sent": False, "generating": False, "last_emit": 0.0}

        def send(kind: str) -> None:
            now = time.time()
            try:
                callback({
                    "state": kind,
                    "stage": kind,
                    "request_started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
                    "last_response_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                    "request_started_at_epoch": started,
                    "last_response_at_epoch": now,
                    "elapsed_seconds": round(now - started, 3),
                })
            except Exception:  # noqa: BLE001 - 活动回调失败不影响请求
                logger.debug("on_activity 回调失败", exc_info=True)

        def on_waiting() -> None:
            if not state["waiting_sent"]:
                state["waiting_sent"] = True
                send("waiting")

        def on_data() -> None:
            now = time.monotonic()
            if not state["generating"]:
                state["generating"] = True
                state["last_emit"] = now
                send("generating")
            elif now - state["last_emit"] >= 0.5:
                state["last_emit"] = now
                send("generating")

        return on_waiting, on_data

    @staticmethod
    def _guarded_lines(lines: Iterable[Any], *, deadline: float | None, total: float, on_data: Any,
                       byte_cap: int = 0) -> Any:
        """逐行包一层总时限检查 + 活动发射（仅 with_request_limits 副本启用）。

        总时限在每行 SSE 事件处检查绝对 deadline：只要流还在持续吐事件，任何时刻越线
        都立刻中止，连续 SSE 不会绕过 total_seconds；两块之间的空闲由 socket 层 read
        超时（idle_seconds）兑住，阻塞中的读取由看门狗（_read_stream 内）到点 close。
        byte_cap > 0 时按已收字节数（含 SSE 框架）拦截失控输出。"""
        next_emit = 0.0
        seen = 0
        for line in lines:
            if deadline is not None and time.monotonic() > deadline:
                raise _TotalDeadlineExceeded(total)
            if byte_cap > 0:
                seen += len(line.encode("utf-8", errors="replace")) + 1
                if seen > byte_cap:
                    raise _OutputCapExceeded(seen, byte_cap)
            if on_data is not None:
                now = time.monotonic()
                if now >= next_emit:
                    next_emit = now + 0.5
                    on_data()
            yield line

    @staticmethod
    def _guarded_bytes(chunks: Iterable[Any], *, deadline: float | None, total: float, on_data: Any,
                       byte_cap: int = 0) -> Any:
        """逐字节块包一层总时限检查 + 输出上限 + 活动发射（非流式回退的等价护栏）。"""
        next_emit = 0.0
        seen = 0
        for chunk in chunks:
            if deadline is not None and time.monotonic() > deadline:
                raise _TotalDeadlineExceeded(total)
            if byte_cap > 0:
                seen += len(chunk)
                if seen > byte_cap:
                    raise _OutputCapExceeded(seen, byte_cap)
            if on_data is not None:
                now = time.monotonic()
                if now >= next_emit:
                    next_emit = now + 0.5
                    on_data()
            yield chunk

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
                try:
                    self._client = httpx.Client(
                        timeout=timeouts, follow_redirects=True,
                        limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                    )
                except (FileNotFoundError, OSError, ValueError) as error:
                    # TLS 信任库不可用：给出可操作说明，绝不降级为不校验证书。
                    raise ProviderError(tls_context_error(error)) from error
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

    def _read_stream(
        self, client: Any, url: str, headers: dict[str, str], body: dict[str, Any], timeouts: Any,
        progress: dict[str, bool] | None = None,
        *, deadline: float | None = None, total: float = 0.0, on_data: Any = None,
        raw_cap: int = 0, semantic_cap: int = 0,
    ) -> tuple[Any, dict[str, Any]]:
        """发起一次 SSE 流式请求，聚合成与非流式一致的响应 JSON 后返回。

        流式把「读超时」的语义从「等待整份响应的总时限」变成「两个数据块之间的
        空闲时限」：只要模型还在持续吐 token（包括思考型模型的 reasoning 增量），
        长时间生成不会再在总时限处整单超时作废——这是降低翻译失败率的关键。
        思考增量（delta.reasoning_content）只参与保活，不计入译文内容。

        ``deadline``/``total``/``on_data``/``byte_cap`` 仅由 with_request_limits 副本传入：
        逐事件/逐字节检查单请求总时限与输出字节上限，并对每次收到数据发射活动回调。
        另挂一个到点 close 流的看门狗 Timer：兑住「阻塞在 read 上、等不到下一个事件」
        的越线（否则总时限最多会被拉长一个 idle）；Timer 在 finally 中必被 cancel。
        """
        guarded = deadline is not None or on_data is not None
        watchdog_fired = threading.Event()
        with client.stream("POST", url, headers=headers, json=body, timeout=timeouts) as response:
            watchdog: threading.Timer | None = None
            try:
                if deadline is not None:
                    # 看门狗：到点强关流，让阻塞在 read 上的本线程立刻醒来报总时限越线。
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise _TotalDeadlineExceeded(total)
                    watchdog = threading.Timer(remaining, self._fire_watchdog, args=(watchdog_fired, response))
                    watchdog.daemon = True
                    watchdog.start()
                return self._consume_response(
                    response, progress, deadline=deadline, total=total,
                    on_data=on_data, raw_cap=raw_cap, semantic_cap=semantic_cap, guarded=guarded,
                )
            except BaseException as error:
                # 看门狗已开火时，任何从被强关的流里冒出来的异常都归因为总时限越线。
                if watchdog_fired.is_set() and not isinstance(
                        error, (_TotalDeadlineExceeded, _HTTPStatusError, _StreamUnsupported, _OutputCapExceeded)):
                    raise _TotalDeadlineExceeded(total) from error
                raise
            finally:
                if watchdog is not None:
                    watchdog.cancel()   # 无悬挂定时器：成功/失败/回退都立即撤销
                    watchdog.join(timeout=1.0)   # 已开火的回调若有阻塞也只在有限时间内等

    @staticmethod
    def _fire_watchdog(fired: threading.Event, response: Any) -> None:
        """到点中止传输：先 best-effort shutdown socket 再 close 流。

        macOS 上另一线程的 close 不一定唤醒阻塞中的 recv（要等 socket 级 read 超时）；
        显式 shutdown(SHUT_RDWR) 能让 recv 立刻返回错误。全程 best effort：
        任何失败都由读取层的逐块 deadline 检查兑底。
        """
        fired.set()
        try:
            network_stream = (response.extensions or {}).get("network_stream")
            sock = network_stream.get_extra_info("socket") if network_stream is not None else None
            if sock is not None:
                import socket as _socket
                try:
                    sock.shutdown(_socket.SHUT_RDWR)
                except OSError:
                    pass  # 可能已被对端/连接池关闭
        except Exception:  # noqa: BLE001 - shutdown 失败不影响 close 兑底
            logger.debug("看门狗 shutdown socket 失败", exc_info=True)
        try:
            response.close()
        except Exception:  # noqa: BLE001 - 关流失败时读取层仍有逐块 deadline 检查兑底
            logger.debug("看门狗关闭流失败", exc_info=True)

    def _consume_response(
        self, response: Any, progress: dict[str, bool] | None = None,
        *, deadline: float | None = None, total: float = 0.0, on_data: Any = None,
        raw_cap: int = 0, semantic_cap: int = 0, guarded: bool,
    ) -> tuple[Any, dict[str, Any]]:
        # 语义输出计数器：只计解码后的 delta.content / reasoning_content 字节数
        # （SSE/JSON 框架字节不算语义输出，只受宽松的 raw_cap 封套安全上限约束）。
        semantic = {"bytes": 0}

        def on_output(piece: str) -> None:
            semantic["bytes"] += len(piece.encode("utf-8", errors="replace"))
            if semantic_cap > 0 and semantic["bytes"] > semantic_cap:
                raise _OutputCapExceeded(semantic["bytes"], semantic_cap)

        if response.status_code >= 400:
            response.read()
            raise _HTTPStatusError(response, redact_error_text(response.text))
        content_type = str(response.headers.get("content-type") or "").lower()
        if "text/event-stream" not in content_type:
            if not guarded:
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
                    raise _HTTPStatusError(response, f"响应不是有效 JSON：{redact_error_text(error)}") from error
            # 限额副本：非流式回退同样逐字节受总时限/活动护栏约束（不整块 read）。
            pieces = [chunk for chunk in self._guarded_bytes(
                response.iter_bytes(), deadline=deadline, total=total, on_data=on_data,
                byte_cap=raw_cap,
            )]
            raw = b"".join(pieces)
            try:
                text = raw.decode(response.encoding or "utf-8")
            except (UnicodeDecodeError, LookupError):
                text = raw.decode("utf-8", errors="replace")
            try:
                return response, json.loads(text)
            except ValueError as error:
                if "data:" in text:
                    value = _aggregate_sse_events(text.splitlines())
                    if value is not None:
                        return response, value
                raise _HTTPStatusError(response, f"响应不是有效 JSON：{redact_error_text(error)}") from error
        lines: Iterable[Any] = response.iter_lines()
        if guarded:
            lines = self._guarded_lines(lines, deadline=deadline, total=total, on_data=on_data,
                                        byte_cap=raw_cap)
        value = self._aggregate_stream_lines(lines, progress, on_output=on_output if semantic_cap > 0 else None)
        if value is None:
            # 连一个有效事件都没有：视为协议不兼容，回退非流式，而不是伪造成空译文。
            raise _StreamUnsupported("空事件流")
        return response, value

    @staticmethod
    def _aggregate_stream_lines(lines: Iterable[Any], progress: dict[str, bool] | None = None,
                                on_output: Any = None) -> dict[str, Any] | None:
        """聚合 SSE 行，并区分“接收到数据后才断开”与“一个字节都没拿到”。

        已读到内容的断流会抛 _StreamInterrupted：内容可能已经被服务端生成并计费，
        不能按“本次无实际费用”自动重发（会双重扣费且漏账）。
        ``progress`` 为调用方提供的可变标记（用于异常兜底判断是否可能已计费）；
        ``on_output`` 为语义输出增量回调（见 _aggregate_sse_events）。
        """
        try:
            import httpx
        except ImportError as error:
            raise ProviderError("缺少 httpx，请安装 Literature Python 依赖。") from error
        received = False

        def marked() -> Iterable[Any]:
            nonlocal received
            for line in lines:
                received = True
                if progress is not None:
                    progress["received"] = True
                yield line

        try:
            return _aggregate_sse_events(marked(), on_output=on_output)
        except httpx.TimeoutException:
            # 读超时保持原类型：外层会映射成 ProviderTimeoutError 并已按“可能已计费”处理。
            raise
        except httpx.HTTPError as error:
            if received:
                raise _StreamInterrupted(redact_error_text(f"流式响应在传输中中断：{error}")) from error
            raise

    def _complete(self, *, system: str, content: str | list[dict[str, Any]]) -> ProviderResponse:
        try:
            import httpx
        except ImportError as error:
            raise ProviderError("缺少 httpx，请安装 Literature Python 依赖。") from error
        # 代理环境变量在 httpx 创建 Client 时解析，必须先规范化再发请求。
        normalize_proxy_environment()
        # 密钥只在 https（或本机 loopback http，或用户显式允许明文的内网服务）上发送，
        # 不允许明文 HTTP 静默发往远程主机。
        assert_key_transport_safe(
            self.config.base_url, self.api_key, allow_plaintext=self.config.allow_plaintext_http,
        )
        if (
            self.config.allow_plaintext_http
            and plaintext_http_blocked(self.config.base_url)
            and not self._plaintext_warned
        ):
            # 每次任务只提示一次，避免翻译几千块时日志刷屏；但必须留痕。
            self._plaintext_warned = True
            logger.warning(
                "按服务配置允许明文 HTTP 发送 API Key：%s（链路上不加密，请仅在可信内网使用）",
                self.config.base_url,
            )
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", **self.config.extra_headers}
        base_body = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        limits = self._request_limits
        # 默认超时（无限额时用）；限额副本在循环内按剩余时限动态收窄（见下）。
        timeouts = httpx.Timeout(self.timeout, connect=min(20.0, self.timeout),
                                 write=min(60.0, self.timeout), pool=min(30.0, self.timeout))
        client = self._client_instance(httpx)
        prompt_chars = len(system) + (len(content) if isinstance(content, str) else 0)
        on_waiting, on_data = self._activity_emitters()
        # 总时限按整个 complete（含 400 协议回退后的重发）计：deadline 进入循环前起算，
        # 回退重发不重新拉满；每次 attempt 的 connect/read/write/pool 都按剩余时限收窄，
        # 看门狗再兑住阻塞中的读取（见 _read_stream）。
        complete_deadline = time.monotonic() + float(limits["total_seconds"]) if limits else None
        # 语义输出上限（解码后 delta 文本字节）与宽松的原始封套安全上限。
        semantic_cap = 0
        raw_cap = 0
        if limits:
            cap_tokens = float(limits["max_output_tokens"])
            semantic_cap = int(cap_tokens * self.OUTPUT_SEMANTIC_BYTE_PER_TOKEN) + self.OUTPUT_SEMANTIC_ENVELOPE
            raw_cap = max(self.OUTPUT_RAW_MIN_BYTES, int(semantic_cap * self.OUTPUT_RAW_MULTIPLE))
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
            if on_waiting is not None:
                on_waiting()
            handle = self.attempt_hooks[0](prompt_chars) if self.attempt_hooks else None
            # 本次 attempt 是否已经读到流式数据：决定异常时是否按“可能已计费”对账，
            # 而不是一律当成“没花钱”释放。
            stream_progress = {"received": False}
            if limits:
                # 本 attempt 的传输超时按**剩余总时限**收窄：兼容回退之间的间隙不豁免，
                # total_seconds 不会被 400 回退逐次拉满。
                remaining = (complete_deadline or 0.0) - time.monotonic()
                if remaining < 0.05:
                    # 预算名额归还（未发出任何请求，无费用）；不再发起新 attempt。
                    if self.attempt_hooks:
                        self.attempt_hooks[1](handle, None, None, False)
                    raise ProviderTimeoutError("total", float(limits["total_seconds"]))
                total_seconds = float(limits["total_seconds"])
                timeouts = httpx.Timeout(
                    total_seconds,
                    connect=min(20.0, total_seconds, remaining),
                    read=min(float(limits["idle_seconds"]), total_seconds, remaining),
                    write=min(60.0, total_seconds, remaining),
                    pool=min(30.0, total_seconds, remaining),
                )
            try:
                if use_stream or limits:
                    # 限额副本统一走流式读取函数：非流式回退（body 无 stream 字段）也
                    # 受剩余时限/输出上限/活动护栏约束，而不是无护栏的 client.post。
                    response, value = self._read_stream(
                        client, url, headers, body, timeouts, stream_progress,
                        deadline=complete_deadline,
                        total=float(limits["total_seconds"]) if limits else 0.0,
                        on_data=on_data, raw_cap=raw_cap, semantic_cap=semantic_cap,
                    )
                else:
                    response = client.post(url, headers=headers, json=body, timeout=timeouts)
                    if response.status_code >= 400:
                        raise _HTTPStatusError(response, redact_error_text(response.text))
                    # 解码失败也归成 _HTTPStatusError（外层转 ProviderError）：否则
                    # 「200+门户 HTML」不触发连续失败熔断，且调用方捕获不到 ProviderError。
                    try:
                        value = response.json()
                    except ValueError as error:
                        raise _HTTPStatusError(response, f"响应不是有效 JSON：{redact_error_text(error)}") from error
            except _TotalDeadlineExceeded as error:
                # 单请求总时限越线：可能已计费，不自动重发；按超时上报（kind="total"）。
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, error, True)
                raise ProviderTimeoutError("total", error.seconds) from error
            except _OutputCapExceeded as error:
                # 传输途中已收字节超输出上限（服务端忽略 max_tokens 的失控输出）：
                # 立即中止读取，不再收完 48k token；可能已计费，不自动重发。
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, error, True)
                partial = ProviderResponse(
                    content="", provider=self.config.provider, model=self.config.model,
                    request_id="", finish_reason="",   # 上游未结束，不伪造 finish_reason
                    raw={"output_cap_exceeded": {"bytes_seen": error.bytes_seen, "cap_bytes": error.cap_bytes}},
                )
                raise ProviderError(
                    f"模型输出在传输中超过单请求上限（已收 {error.bytes_seen} 字节 ≥ 约 "
                    f"{error.cap_bytes} 字节；服务端可能忽略了 max_tokens 参数）。已中止读取，"
                    "费用按未知口径记账，不自动重发；请缩小输入范围或降低输出预算。",
                    response=partial, possible_charge=True,
                ) from error
            except (_StreamInterrupted, _SSEErrorEvent) as error:
                # 已读到内容后才断开 / 服务端在流里回了 error：可能已经计费，也不应自动重发。
                # final=True 把预留交给调用方按“费用未知”对账（而不是释放成零成本）。
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, error, True)
                raise
            except httpx.TimeoutException as error:
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, error, True)
                kind = "connect" if isinstance(error, httpx.ConnectTimeout) else "write" if isinstance(error, httpx.WriteTimeout) else "pool" if isinstance(error, httpx.PoolTimeout) else "read"
                # 限额副本：read 超时按剩余总时限收窄，若在总时限处触发，应按总时限越线
                # 上报（kind="total"），不冒充空闲超时（空闲超时的语义是“两块间隔超 idle”）。
                if (complete_deadline is not None and kind == "read" and limits
                        and time.monotonic() >= complete_deadline - 0.05):
                    raise ProviderTimeoutError("total", float(limits["total_seconds"])) from error
                # 超时可能已经产生费用，不自动重发；由上层按分块重试。
                raise ProviderTimeoutError(kind, getattr(timeouts, kind)) from error
            except httpx.HTTPError as error:
                # 已读到流式数据后的任意 HTTP 异常都不能按“无费用”重发（读流内部通常已转
                # _StreamInterrupted；这里是兜底，防上下文进出等路径漏网）。
                final = attempt >= self.max_retries or bool(stream_progress["received"])
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, error, final)
                if not final:
                    time.sleep(self._retry_delay(None, attempt))
                    attempt += 1
                    continue
                # 连接未建立（ConnectError）= 确定不计费；其余（读/写/协议错误）保守按“未知”。
                raise ProviderError(
                    f"模型请求失败：{redact_error_text(str(error))}",
                    possible_charge=False if isinstance(error, httpx.ConnectError) else None,
                ) from error
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
                raise ProviderError(f"模型请求失败：{redact_error_text(str(error))}", possible_charge=True) from error
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
                if error.status_code in self._retry_status_codes and attempt < self.max_retries:
                    # 429/5xx 且还会重试：释放本次 attempt 的预留并记退避冷却。
                    if self.attempt_hooks:
                        self.attempt_hooks[1](handle, error.response, None, False)
                    time.sleep(self._retry_delay(error.response, attempt))
                    attempt += 1
                    continue
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, error.response, error, True)
                raise ProviderError(
                    f"模型请求失败：{redact_error_text(error.detail or error)}", status_code=error.status_code,
                ) from error
            except BaseException as error:
                # 未预期异常（非 httpx、非本模块自定义）：必须归还服务名额与预算预留后原样抛出。
                # 若流已经开始并读到数据，按“可能已计费”（final=True）交调用方按未知费用对账；
                # 否则释放成零成本（final=False）。
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, None, error, bool(stream_progress["received"]))
                raise
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
        if limits:
            # 输出上限复核：服务端若忽略 max_tokens 参数继续生成，这里按真实 usage
            # （缺失时按内容保守估算）强制收口。超限不重发（重发会重复计费）：把
            # 含真实 usage 的响应挂在 error.response 上交调用方记账后按失败处理。
            cap = int(limits.get("max_output_tokens") or 0)
            if cap > 0:
                reported = result.output_tokens
                estimate = self._estimate_output_tokens(result.content)
                tokens = max(reported, estimate)
                if tokens > cap:
                    # 上游 finish_reason 原样保留（不伪造 length）；超限信号只在本错误上。
                    raise ProviderError(
                        f"模型输出超过本请求上限（约 {tokens} > {cap} tokens；"
                        "服务端可能忽略了 max_tokens 参数）。本次真实用量已保留，不自动重发"
                        "（避免重复计费）；请缩小输入范围或降低输出预算。",
                        response=result,
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


def _probe_attempt(
    provider: Any, *, style: str, effort: str, variant: str,
    on_attempt: Any = None, prepare_child: Any = None, settle: Any = None,
) -> _ProbeResult:
    """发一次极小探测请求；子 provider 的超时与自建连接池都在此收敛。

    - ``prepare_child(child)``：为子 provider 挂共享协调层钩子（逐 attempt 预算/并发）。
    - ``settle(child, response)``：把子 provider 上本次 attempt 的预算预留对账掉。
    - ``on_attempt(response, error)``：把探测请求纳入用量账本（有 usage 记实际成本，
      无 usage 且可能已计费记“费用未知”）。**先关闭子 provider 再回调**，
      回调抛异常也不会漏关连接池。
    """
    child = provider.with_reasoning(style=style, effort=effort, variant=variant)
    child = child.with_max_output_tokens(PROBE_MAX_TOKENS)
    if hasattr(child, "timeout"):
        # 只收窄单次请求的等待上限，不重建连接池（探测应复用作业的 keep-alive 连接）。
        child.timeout = min(float(child.timeout or PROBE_TIMEOUT_SECONDS), PROBE_TIMEOUT_SECONDS)
    attempted = False
    handled_response: Any = None
    error_to_raise: BaseException | None = None
    try:
        if prepare_child is not None:
            prepare_child(child)
        attempted = True
        try:
            handled_response = child.complete(system=PROBE_SYSTEM, user=PROBE_USER)
        except ProviderError as error:
            attached = getattr(error, "response", None)
            if attached is None:
                error_to_raise = error
            else:
                # “只返回了思维链”这类错误带着完整 usage，仍可作为判定依据。
                handled_response = attached
        except BaseException as error:
            error_to_raise = error
    finally:
        # 子 provider 的生命周期与 prepare/settle/callback 全包在同一 finally 内：
        # prepare 失败、complete 异常、callback 抛错都不会漏关连接池。
        _close_probe_client(provider, child)
        if attempted:
            if settle is not None:
                try:
                    settle(child, handled_response)
                except Exception:
                    logger.debug("探测对账失败", exc_info=True)
            if on_attempt is not None:
                try:
                    on_attempt(handled_response, error_to_raise)
                except Exception as error:  # noqa: BLE001 - 回调失败不拖垮探测
                    logger.warning("探测用量回调失败：%s", redact_error_text(error))
    if error_to_raise is not None:
        raise error_to_raise
    return _ProbeResult(
        response=handled_response,
        field_rejected=bool(getattr(child, "_drop_reasoning_fields", False)),
        stream_disabled=bool(getattr(child, "_stream_disabled", False)),
    )


def _settle_streaming_support(provider: Any, on_attempt: Any = None, prepare_child: Any = None, settle: Any = None) -> bool:
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
        result = _probe_attempt(provider, style=NO_REASONING_STYLE, effort="", variant="", on_attempt=on_attempt, prepare_child=prepare_child, settle=settle)
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


def probe_thinking_off(
    provider: Any, *, on_attempt: Any = None, prepare_child: Any = None, settle: Any = None,
    should_continue: Any = None,
) -> dict[str, Any] | None:
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
    if should_continue is not None and not should_continue():
        # 有限预算下已经出现不可量化的费用（unknown）：停止后续探测，不写策略缓存。
        logger.info("探测预算/未知费用已阻止继续探测（前置）")
        return None
    if not _settle_streaming_support(provider, on_attempt=on_attempt, prepare_child=prepare_child, settle=settle):
        return None
    effort = disabled_reasoning_effort(style)
    undecided = False
    for variant in candidates:
        if should_continue is not None and not should_continue():
            logger.info("探测预算/未知费用已阻止继续探测（候选之间）")
            return None
        try:
            result = _probe_attempt(provider, style=style, effort=effort, variant=variant, on_attempt=on_attempt, prepare_child=prepare_child, settle=settle)
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
        if should_continue is not None and not should_continue():
            # 本次 attempt 产生了不可量化费用（probe_state.stop 已置位）：不采纳、不缓存。
            # 否则“前置已计量、候选未计量”时仍会把结果写进策略缓存。
            logger.info("探测预算/未知费用已阻止采纳本次结果（%s）", variant)
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
    if should_continue is not None and not should_continue():
        # 最后一步也要挡：不能把“已因未知费用停止”的轮次写成“所有写法都不生效”的负缓存。
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
