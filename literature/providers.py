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
from typing import Any, Protocol


KEYCHAIN_SERVICE = "local.literature.providers"

# 这些 HTTP 状态码代表临时性故障（限流/网关/服务端），值得退避后重试；
# 401/400 等确定性错误不重试，直接报告给用户。
RETRY_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    # 有些错误（例如只返回思考内容、截断）发生时其实已经拿到了服务端响应与 usage；
    # 把它挂在异常上，上层仍能将真实用量记账，而不是把已产生的费用当成零。
    def __init__(self, message: str, *, response: "ProviderResponse | None" = None) -> None:
        super().__init__(message)
        self.response = response


class ProviderTimeoutError(ProviderError):
    def __init__(self, kind: str, seconds: float) -> None:
        self.kind, self.seconds = kind, seconds
        action = {"read": "等待模型响应", "connect": "连接模型服务", "write": "发送模型输入", "pool": "等待模型连接"}[kind]
        hint = (
            "若本任务用量里 reasoning 占比很高，可在模型服务里关闭思考模式或换用非思考模型。"
            if kind == "read" else ""
        )
        super().__init__(f"{action}超时（等待上限 {seconds:g} 秒）。可稍后重试或更换模型服务。{hint}")


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

    def _complete(self, *, system: str, content: str | list[dict[str, Any]]) -> ProviderResponse:
        try:
            import httpx
        except ImportError as error:
            raise ProviderError("缺少 httpx，请安装 Literature Python 依赖。") from error
        # 代理环境变量在 httpx 创建 Client 时解析，必须先规范化再发请求。
        normalize_proxy_environment()
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", **self.config.extra_headers}
        body = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
        }
        body.update(reasoning_request_fields(self.config))
        timeouts = httpx.Timeout(self.timeout, connect=min(20.0, self.timeout),
                                 write=min(60.0, self.timeout), pool=min(30.0, self.timeout))
        client = self._client_instance(httpx)
        prompt_chars = len(system) + (len(content) if isinstance(content, str) else 0)
        response = None
        value: dict[str, Any] = {}
        for attempt in range(self.max_retries + 1):
            handle = self.attempt_hooks[0](prompt_chars) if self.attempt_hooks else None
            try:
                response = client.post(url, headers=headers, json=body, timeout=timeouts)
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
                    continue
                raise ProviderError(f"模型请求失败：{error}") from error
            if response.status_code in RETRY_STATUS_CODES and attempt < self.max_retries:
                # 429/5xx 且还会重试：释放本次 attempt 的预留并记退避冷却。
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, response, None, False)
                time.sleep(self._retry_delay(response, attempt))
                continue
            try:
                response.raise_for_status()
                value = response.json()
            except Exception as error:
                if self.attempt_hooks:
                    self.attempt_hooks[1](handle, response, error, True)
                detail = getattr(getattr(error, "response", None), "text", "")
                raise ProviderError(f"模型请求失败：{detail or error}") from error
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
