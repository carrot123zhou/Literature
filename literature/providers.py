from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from copy import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol


KEYCHAIN_SERVICE = "local.literature.providers"

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
    # 思考模式/预算：默认全空，不向服务发送任何未声明支持的字段。
    # 只有用户显式选择 reasoning_style 后才按该风格的显式协议拼请求体。
    reasoning_style: str = ""
    reasoning_effort: str = ""
    reasoning_budget: int = 0

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


# 显式、可审计的思考参数协议。默认不选任何风格就不发送额外字段，
# 避免把未声明支持的参数发给中转站被静默忽略或直接报错。
REASONING_STYLES = ("openai_effort", "qwen_thinking", "glm_thinking")
REASONING_EFFORTS = ("minimal", "low", "medium", "high")
THINKING_TOGGLES = ("on", "off")


def reasoning_request_fields(config: Any) -> dict[str, Any]:
    """按用户显式选择的协议返回请求体附加字段；未选择或不合法则返回空。"""
    style = str(getattr(config, "reasoning_style", "") or "").strip()
    effort = str(getattr(config, "reasoning_effort", "") or "").strip()
    try:
        budget = int(getattr(config, "reasoning_budget", 0) or 0)
    except (TypeError, ValueError):
        budget = 0
    if style == "openai_effort" and effort in REASONING_EFFORTS:
        return {"reasoning_effort": effort}
    if style == "qwen_thinking":
        fields: dict[str, Any] = {}
        if effort in THINKING_TOGGLES:
            fields["enable_thinking"] = effort == "on"
        if budget > 0:
            fields["thinking_budget"] = budget
        return fields
    if style == "glm_thinking" and effort in THINKING_TOGGLES:
        return {"thinking": {"type": "enabled" if effort == "on" else "disabled"}}
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


def list_remote_models(base_url: str, api_key: str, *, timeout: float = 30.0) -> list[str]:
    """用服务自己的 base URL 与密钥拉取 OpenAI 兼容的 /v1/models 列表。"""
    base_url = (base_url or "").strip()
    if not base_url:
        raise ProviderError("请填写 Base URL。")
    if not (api_key or "").strip():
        raise ProviderError("请先保存 API Key。")
    try:
        import httpx
    except ImportError as error:
        raise ProviderError("缺少 httpx，请安装 Literature Python 依赖。") from error
    normalize_proxy_environment()
    url = f"{base_url.rstrip('/')}/models"
    try:
        response = httpx.get(url, headers={"Authorization": f"Bearer {api_key.strip()}"}, timeout=timeout)
        response.raise_for_status()
        value = response.json()
    except Exception as error:
        detail = getattr(getattr(error, "response", None), "text", "")
        raise ProviderError(f"获取模型列表失败：{detail or error}") from error
    items = value.get("data") if isinstance(value, dict) else None
    if not isinstance(items, list):
        raise ProviderError("模型列表响应格式无法识别。")
    models: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("id"):
            models.append(str(item["id"]))
        elif isinstance(item, str):
            models.append(item)
    models = sorted(set(models))
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
        """返回一个“思考已按所声明协议尽量关闭”的 provider 副本（不修改原对象）。

        翻译是确定性任务，reasoning 对译文质量近乎零贡献却占掉大部分输出延迟
        （实测思考型模型 reasoning 占输出的 85–90%），因此翻译作业默认调用本方法。

        规则：只在用户已显式声明协议（reasoning_style 非空）时才改写字段——
        从未声明协议的服务不发送任何未声明字段（providers 的显式协议原则），
        此时想关思考需在模型服务设置里选择协议并把开关设为 off。
        用户显式要求开思考（开关=on / effort=high）时尊重用户配置，不覆盖。
        """
        style = (self.config.reasoning_style or "").strip()
        effort = (self.config.reasoning_effort or "").strip()
        if not style:
            return self
        if effort in THINKING_TOGGLES and effort == "on":
            return self  # 用户显式要求开思考：尊重配置。
        if effort == "high":
            return self  # openai_effort 协议下显式选了 high：视为有意开启。
        target = "off" if style in ("qwen_thinking", "glm_thinking") else "minimal"
        if effort == target:
            return self
        provider = copy(self)
        provider.config = copy(self.config)
        provider.config.reasoning_effort = target
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
        base_body.update(reasoning_request_fields(self.config))
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
                if use_stream and error.status_code == 400:
                    # 服务端拒绝流式请求（不支持 stream/stream_options 等）：
                    # 同一 attempt 的预算预留不浪费，关闭流式后立即重发一次。
                    if self.attempt_hooks:
                        self.attempt_hooks[1](handle, error.response, None, False)
                    self._stream_disabled = True
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
