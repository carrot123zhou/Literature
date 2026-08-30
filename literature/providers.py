from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


KEYCHAIN_SERVICE = "local.literature.providers"


class ProviderError(RuntimeError):
    pass


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
    max_output_tokens: int = 8192
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    extra_headers: dict[str, str] = field(default_factory=dict)

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
    raw: dict[str, Any] = field(default_factory=dict)


class TextProvider(Protocol):
    config: ProviderConfig

    def complete(self, *, system: str, user: str) -> ProviderResponse: ...


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


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig, *, api_key: str | None = None, timeout: float = 180.0) -> None:
        self.config = config
        self.api_key = (api_key if api_key is not None else get_api_key(config.provider)).strip()
        self.timeout = timeout
        if not self.api_key:
            raise ProviderError(f"尚未配置 {config.provider} API key。")
        if not config.base_url.strip():
            raise ProviderError("兼容服务需要 base URL。")

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        try:
            import httpx
        except ImportError as error:
            raise ProviderError("缺少 httpx，请安装 Literature Python 依赖。") from error
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", **self.config.extra_headers}
        body = {
            "model": self.config.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
        }
        try:
            response = httpx.post(url, headers=headers, json=body, timeout=self.timeout)
            response.raise_for_status()
            value = response.json()
        except Exception as error:
            detail = getattr(getattr(error, "response", None), "text", "")
            raise ProviderError(f"模型请求失败：{detail or error}") from error
        try:
            content = value["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise ProviderError("模型响应缺少 choices[0].message.content。") from error
        if isinstance(content, list):
            content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        completion_details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cost = None
        if self.config.input_price_per_million is not None and self.config.output_price_per_million is not None:
            cost = round((input_tokens * self.config.input_price_per_million + output_tokens * self.config.output_price_per_million) / 1_000_000, 8)
        return ProviderResponse(
            content=str(content), provider=self.config.provider, model=str(value.get("model") or self.config.model),
            request_id=str(response.headers.get("x-request-id") or value.get("id") or ""),
            input_tokens=input_tokens, output_tokens=output_tokens,
            cached_tokens=int(prompt_details.get("cached_tokens") or usage.get("cached_tokens") or 0),
            reasoning_tokens=int(completion_details.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0),
            estimated_cost=cost, raw=value,
        )


class MockProvider:
    """Deterministic provider used by tests and the UI demo project."""
    def __init__(self, translations: dict[str, str] | None = None) -> None:
        self.config = ProviderConfig.preset("custom", model="mock", base_url="mock://local")
        self.translations = translations or {}

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        try:
            payload = json.loads(user)
        except ValueError:
            content = f"译：{user}"
            return ProviderResponse(content=content, provider="mock", model="mock", request_id=uuid.uuid4().hex, input_tokens=len(user) // 4, output_tokens=len(content) // 4)
        if not isinstance(payload, dict) or "blocks" not in payload:
            content = f"译：{user}"
            return ProviderResponse(content=content, provider="mock", model="mock", request_id=uuid.uuid4().hex, input_tokens=len(user) // 4, output_tokens=len(content) // 4)
        output = []
        for item in payload.get("blocks", []):
            block_id = item["block_id"]
            output.append({
                "block_id": block_id,
                "text": self.translations.get(block_id, f"译：{item.get('text', '')}"),
                "items": [f"译：{value}" for value in item.get("items", [])],
                "caption": [f"译：{value}" for value in item.get("caption", [])],
                "footnote": [f"译：{value}" for value in item.get("footnote", [])],
                "cells": {cell["cell_id"]: f"译：{cell['text']}" for cell in item.get("cells", [])},
            })
        content = json.dumps({"translations": output}, ensure_ascii=False)
        return ProviderResponse(content=content, provider="mock", model="mock", request_id=uuid.uuid4().hex, input_tokens=len(user) // 4, output_tokens=len(content) // 4)
