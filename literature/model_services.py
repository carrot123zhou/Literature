from __future__ import annotations

import json
import os
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .providers import PROVIDER_PRESETS, ProviderConfig, ProviderError, get_api_key

# 密钥仍只保存在 macOS Keychain；模型服务的账户名用自己的 service_id，
# 预设厂商可回退到旧 providers Keychain，保持已有配置继续可用。
MODEL_KEYCHAIN_SERVICE = "local.literature.models"

ROLE_STRUCTURE = "structure"
ROLE_TRANSLATION = "translation"
MODEL_ROLES = (ROLE_STRUCTURE, ROLE_TRANSLATION)


def runtime_root() -> Path:
    configured = os.environ.get("LITERATURE_RUNTIME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / "Library" / "Application Support" / "Literature"


@dataclass(slots=True)
class ModelService:
    """一个可复用的模型服务：预设厂商或自定义中转站。"""

    service_id: str
    name: str
    preset: str = "custom"
    base_url: str = ""
    model: str = ""
    temperature: float = 0.2
    max_output_tokens: int = 8192
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config(self) -> ProviderConfig:
        return ProviderConfig(
            provider=self.service_id, model=self.model, base_url=self.base_url,
            temperature=self.temperature, max_output_tokens=self.max_output_tokens,
            input_price_per_million=self.input_price_per_million,
            output_price_per_million=self.output_price_per_million,
        )


def _clean_text(value: Any, *, maximum: int = 400) -> str:
    return str(value or "").strip()[:maximum]


def _clean_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


class ModelServiceStore:
    """模型服务库与任务角色绑定的持久化（非机密部分，密钥仅进 Keychain）。"""

    def __init__(self, root: str | Path | None = None) -> None:
        base = Path(root) if root is not None else runtime_root()
        self.path = base / "model-services.json"

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        services = value.get("services")
        roles = value.get("roles")
        return {
            "version": 1,
            "services": services if isinstance(services, list) else [],
            "roles": {key: str(roles.get(key) or "") for key in MODEL_ROLES} if isinstance(roles, dict) else {key: "" for key in MODEL_ROLES},
        }

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def _build(self, payload: dict[str, Any], *, service_id: str) -> ModelService:
        preset = _clean_text(payload.get("preset"), maximum=40) or "custom"
        if preset not in PROVIDER_PRESETS:
            raise ProviderError(f"未知服务商预设：{preset}")
        defaults = PROVIDER_PRESETS[preset]
        base_url = _clean_text(payload.get("base_url"), maximum=500) or str(defaults.get("base_url") or "")
        model = _clean_text(payload.get("model"), maximum=200) or str(defaults.get("model") or "")
        if not model:
            raise ProviderError("请填写模型 ID。")
        if not base_url:
            raise ProviderError("请填写 Base URL。")
        return ModelService(
            service_id=service_id,
            name=_clean_text(payload.get("name"), maximum=120) or str(defaults.get("label") or model),
            preset=preset,
            base_url=base_url,
            model=model,
            temperature=max(0.0, min(2.0, float(payload.get("temperature", 0.2) or 0.2))),
            max_output_tokens=max(256, min(128_000, int(payload.get("max_output_tokens", 8192) or 8192))),
            input_price_per_million=_clean_price(payload.get("input_price_per_million")),
            output_price_per_million=_clean_price(payload.get("output_price_per_million")),
        )

    def list(self) -> list[dict[str, Any]]:
        return self._read()["services"]

    def roles(self) -> dict[str, str]:
        return self._read()["roles"]

    def get(self, service_id: str) -> dict[str, Any]:
        for item in self.list():
            if item.get("service_id") == service_id:
                return item
        raise ProviderError(f"模型服务不存在：{service_id}")

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = self._read()
        service = self._build(payload, service_id=uuid.uuid4().hex)
        value["services"].append(service.to_dict())
        # 第一个服务自动绑定到尚未设置的角色，减少首次配置步骤。
        for role in MODEL_ROLES:
            if not value["roles"].get(role):
                value["roles"][role] = service.service_id
        self._write(value)
        return service.to_dict()

    def update(self, service_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        value = self._read()
        for index, item in enumerate(value["services"]):
            if item.get("service_id") != service_id:
                continue
            service = self._build({**item, **payload}, service_id=service_id)
            value["services"][index] = service.to_dict()
            self._write(value)
            return service.to_dict()
        raise ProviderError(f"模型服务不存在：{service_id}")

    def delete(self, service_id: str) -> None:
        value = self._read()
        value["services"] = [item for item in value["services"] if item.get("service_id") != service_id]
        for role in MODEL_ROLES:
            if value["roles"].get(role) == service_id:
                value["roles"][role] = ""
        self._write(value)
        delete_service_key(service_id)

    def set_roles(self, roles: dict[str, Any]) -> dict[str, str]:
        value = self._read()
        known = {item.get("service_id") for item in value["services"]}
        for role in MODEL_ROLES:
            if role not in roles:
                continue
            candidate = _clean_text(roles.get(role), maximum=64)
            if candidate and candidate not in known:
                raise ProviderError(f"模型服务不存在：{candidate}")
            value["roles"][role] = candidate
        self._write(value)
        return value["roles"]

    def api_key(self, service_id: str) -> str:
        environment = os.environ.get(f"LITERATURE_SERVICE_{service_id.upper()}_API_KEY", "").strip()
        if environment:
            return environment
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", MODEL_KEYCHAIN_SERVICE, "-a", service_id, "-w"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        # 预设厂商回退到旧 providers Keychain，已有配置不需要重录。
        try:
            preset = str(self.get(service_id).get("preset") or "")
        except ProviderError:
            return ""
        if preset and preset != "custom":
            return get_api_key(preset)
        return ""

    def configured(self, service_id: str) -> bool:
        return bool(self.api_key(service_id))

    def config_for(self, service_id: str, *, timeout: float = 180.0):
        from .providers import OpenAICompatibleProvider
        service = self.get(service_id)
        model = ModelService(**{key: service.get(key) for key in ModelService.__dataclass_fields__})
        return OpenAICompatibleProvider(model.config(), api_key=self.api_key(service_id), timeout=timeout)


def save_service_key(service_id: str, api_key: str) -> None:
    api_key = api_key.strip()
    if not api_key:
        raise ProviderError("API key 不能为空。")
    result = subprocess.run(
        ["/usr/bin/security", "add-generic-password", "-U", "-s", MODEL_KEYCHAIN_SERVICE, "-a", service_id, "-w", api_key],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ProviderError(result.stderr.strip() or "无法保存 API key。")


def delete_service_key(service_id: str) -> None:
    subprocess.run(
        ["/usr/bin/security", "delete-generic-password", "-s", MODEL_KEYCHAIN_SERVICE, "-a", service_id],
        capture_output=True, text=True, check=False,
    )
