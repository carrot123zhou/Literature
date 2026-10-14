from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import utc_now
from .providers import (
    PROVIDER_PRESETS, ProviderConfig, ProviderError,
    REASONING_EFFORTS, REASONING_STYLES, THINKING_TOGGLES, get_api_key,
)

# 密钥仍只保存在 macOS Keychain；模型服务的账户名用自己的 service_id，
# 预设厂商可回退到旧 providers Keychain，保持已有配置继续可用。
MODEL_KEYCHAIN_SERVICE = "local.literature.models"

logger = logging.getLogger(__name__)

# 探测缓存文件的读改写互斥：两个项目并发翻译同一服务时，避免丢掉对方的条目。
_POLICY_LOCK = threading.Lock()

ROLE_STRUCTURE = "structure"
ROLE_TRANSLATION = "translation"
MODEL_ROLES = (ROLE_STRUCTURE, ROLE_TRANSLATION)


def runtime_root() -> Path:
    configured = os.environ.get("LITERATURE_RUNTIME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / "Library" / "Application Support" / "Literature"


@dataclass(slots=True)
class ModelEntry:
    """服务商下的一个模型：上下文/输出上限/单价/思考配置都挂在模型上。

    与 Cindy 的「服务商 → 模型」层级一致：一个 Base URL + Key 就是一个服务商，
    下面可以挂多个模型；每个模型各自有上下文窗口与默认思考档位。
    """

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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def config(self, *, provider_id: str, base_url: str) -> ProviderConfig:
        return ProviderConfig(
            provider=provider_id, model=self.model, base_url=base_url,
            temperature=self.temperature, max_output_tokens=self.max_output_tokens,
            input_price_per_million=self.input_price_per_million,
            output_price_per_million=self.output_price_per_million,
            reasoning_style=self.reasoning_style or "", reasoning_effort=self.reasoning_effort or "",
            reasoning_budget=self.reasoning_budget or 0,
        )


@dataclass(slots=True)
class ModelProvider:
    """一个服务商：一个 Base URL + 独立密钥，下挂多个模型。"""

    service_id: str
    name: str
    preset: str = "custom"
    base_url: str = ""
    models: list[ModelEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "name": self.name,
            "preset": self.preset,
            "base_url": self.base_url,
            "models": [entry.to_dict() for entry in self.models],
        }


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


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _empty_role() -> dict[str, str]:
    return {"service_id": "", "model": ""}


class ModelServiceStore:
    """模型服务库与任务角色绑定的持久化（非机密部分，密钥仅进 Keychain）。

    磁盘结构 v2：
    ```json
    {
      "version": 2,
      "services": [{"service_id", "name", "preset", "base_url",
                    "models": [{"model", "context_window", "max_output_tokens", ...}]}],
      "roles": {"structure": {"service_id", "model"}, "translation": {...}}
    }
    ```
    v1 的扁平结构（一个服务 = 一个模型、roles 是字符串）在读取时自动迁移，无需人工处理。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        base = Path(root) if root is not None else runtime_root()
        self.path = base / "model-services.json"

    # ---- 读写与 v1→v2 迁移 ---------------------------------------------- #

    def _normalize_entry(self, item: dict[str, Any], *, legacy: bool = False) -> dict[str, Any]:
        entry = ModelEntry(
            model=_clean_text(item.get("model"), maximum=200),
            # v1 扁平服务上的 ``name`` 是“服务名”（旧流程一个服务就是一个模型），
            # 不是模型显示名；当成模型名会在 UI 里显示成服务商名（yellowbee）。
            name="" if legacy else _clean_text(item.get("name"), maximum=200),
            context_window=max(0, min(10_000_000, _as_int(item.get("context_window"), default=0))),
            max_output_tokens=max(256, min(128_000, _as_int(item.get("max_output_tokens"), default=16384))),
            temperature=max(0.0, min(2.0, _as_float(item.get("temperature"), default=0.2))),
            input_price_per_million=_clean_price(item.get("input_price_per_million")),
            output_price_per_million=_clean_price(item.get("output_price_per_million")),
            reasoning_style=_clean_text(item.get("reasoning_style"), maximum=40),
            reasoning_effort=_clean_text(item.get("reasoning_effort"), maximum=20),
            reasoning_budget=max(0, min(200_000, _as_int(item.get("reasoning_budget"), default=0))),
        )
        return entry.to_dict()

    def _normalize_provider(self, item: Any) -> dict[str, Any] | None:
        """归一化一个**已经是 v2 形态**的服务商（带 `models` 列表，不参与合并）。"""
        if not isinstance(item, dict) or not item.get("service_id"):
            return None
        preset = _clean_text(item.get("preset"), maximum=40) or "custom"
        raw_models = item.get("models")
        entries = [self._normalize_entry(entry) for entry in raw_models if isinstance(entry, dict)] if isinstance(raw_models, list) else []
        return {
            "service_id": str(item["service_id"]),
            "name": _clean_text(item.get("name"), maximum=120) or preset,
            "preset": preset,
            "base_url": _clean_text(item.get("base_url"), maximum=500),
            "models": [entry for entry in entries if entry["model"]],
        }

    def _normalize_role(self, value: Any, *, providers_by_id: dict[str, dict[str, Any]], legacy_model: dict[str, str]) -> dict[str, str]:
        if isinstance(value, dict):
            return {"service_id": _clean_text(value.get("service_id"), maximum=64), "model": _clean_text(value.get("model"), maximum=200)}
        if isinstance(value, str) and value:
            # v1：角色绑定到服务（当时服务=模型）。合并后取该服务贡献的那个模型，而不是合并组里的第一个。
            provider = providers_by_id.get(value)
            model = legacy_model.get(value) or (provider["models"][0]["model"] if provider and provider["models"] else "")
            return {"service_id": provider["service_id"] if provider else value, "model": model}
        return _empty_role()

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        raw_services = value.get("services") if isinstance(value.get("services"), list) else []
        raw_roles = value.get("roles") if isinstance(value.get("roles"), dict) else {}
        providers: list[dict[str, Any]] = []
        providers_by_id: dict[str, dict[str, Any]] = {}
        legacy_model: dict[str, str] = {}
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        for item in raw_services:
            if not isinstance(item, dict) or not item.get("service_id"):
                continue
            service_id = str(item["service_id"])
            if isinstance(item.get("models"), list):
                provider = self._normalize_provider(item)
                if provider is not None:
                    providers.append(provider)
                    providers_by_id[service_id] = provider
                continue
            # v1 扁平服务：同一个 Base URL + 预设的服务合并成一个服务商（Cindy 的「一个服务商挂多个模型」）。
            # 旧流程为每个模型建一条服务，所以历史配置会以 base_url 合并。
            preset = _clean_text(item.get("preset"), maximum=40) or "custom"
            base_url = _clean_text(item.get("base_url"), maximum=500)
            key = (base_url, preset)
            provider = groups.get(key)
            if provider is None:
                provider = {
                    "service_id": service_id,
                    "name": _clean_text(item.get("name"), maximum=120) or preset,
                    "preset": preset,
                    "base_url": base_url,
                    "models": [],
                }
                groups[key] = provider
                providers.append(provider)
            entry = self._normalize_entry(item, legacy=True)
            if entry["model"]:
                legacy_model[service_id] = entry["model"]
                if not any(existing["model"] == entry["model"] for existing in provider["models"]):
                    provider["models"].append(entry)
            providers_by_id[service_id] = provider
        return {
            "version": 2,
            "services": providers,
            "roles": {role: self._normalize_role(raw_roles.get(role), providers_by_id=providers_by_id, legacy_model=legacy_model) for role in MODEL_ROLES},
        }

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "services": value.get("services", []), "roles": value.get("roles", {role: _empty_role() for role in MODEL_ROLES})}
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    # ---- 关思考字段变体缓存（非机密，可重建） --------------------------- #
    #
    # 同一服务+模型只需探测一次：“哪个字段写法真的能关掉思考”是服务端能力，
    # 不会随任务变化。缓存命中后每次翻译启动零额外请求。
    #
    # 缓存文件与 services 清单分开：它只是可重建的性能提示，损坏时直接忽略，
    # 绝不能让一个写失败拖垮正在启动的翻译作业。

    def _policy_path(self) -> Path:
        return self.path.with_name("thinking-policy.json")

    @staticmethod
    def _policy_key(service_id: str, model: str) -> str:
        return f"{service_id}\u0000{model}"

    def _read_policy(self) -> dict[str, Any]:
        try:
            value = json.loads(self._policy_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_policy(self, value: dict[str, Any]) -> None:
        path = self._policy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def thinking_policy(self, service_id: str, model: str) -> dict[str, Any] | None:
        """返回已探测的关思考字段变体；`None` 表示尚未探测过。

        只有经 `remember_thinking_policy` 写入的条目（带 `probed_at`）才算“已探测”：
        残缺条目当作未探测，下次作业重新探测一次。
        """
        with _POLICY_LOCK:
            entry = self._read_policy().get(self._policy_key(service_id, model))
        if not isinstance(entry, dict) or not entry.get("probed_at"):
            return None
        return entry

    def remember_thinking_policy(self, service_id: str, model: str, policy: dict[str, Any]) -> dict[str, Any]:
        """持久化探测结果；`{}` 也写盘，表示“已探测且没有可用字段”，避免反复探测。

        写失败只记日志并返回内存中的值：探测是优化，不能因为缓存写不进去就阻断翻译。
        """
        entry = {
            "variant": str(policy.get("variant") or ""),
            "style": str(policy.get("style") or ""),
            "effort": str(policy.get("effort") or ""),
            "model": model,
            "probed_at": utc_now(),
        }
        with _POLICY_LOCK:
            value = self._read_policy()
            value[self._policy_key(service_id, model)] = entry
            try:
                self._write_policy(value)
            except OSError:
                logger.exception("思考探测缓存写入失败（不影响本次翻译）")
        return entry

    def forget_thinking_policy(self, service_id: str, model: str = "") -> None:
        """清掉某服务的探测缓存（改模型/配置后重新探测）。model 为空则清该服务全部模型。"""
        with _POLICY_LOCK:
            value = self._read_policy()
            prefix = f"{service_id}\u0000"
            kept = {
                key: entry for key, entry in value.items()
                if not (key.startswith(prefix) and (not model or key == self._policy_key(service_id, model)))
            }
            if kept == value:
                return
            try:
                self._write_policy(kept)
            except OSError:
                logger.exception("思考探测缓存清理失败")

    # ---- 构建与校验 ----------------------------------------------------- #

    def _build_entry(self, payload: dict[str, Any]) -> ModelEntry:
        model = _clean_text(payload.get("model"), maximum=200)
        if not model:
            raise ProviderError("请填写模型 ID。")
        reasoning_style = _clean_text(payload.get("reasoning_style"), maximum=40)
        if reasoning_style not in ("", *REASONING_STYLES):
            raise ProviderError("不支持的思考模式协议；请留空或选择已声明的协议。")
        reasoning_effort = _clean_text(payload.get("reasoning_effort"), maximum=20)
        allowed_effort = REASONING_EFFORTS if reasoning_style == "openai_effort" else THINKING_TOGGLES
        if reasoning_effort and reasoning_effort not in allowed_effort:
            raise ProviderError("思考强度/开关与所选协议不匹配。")
        try:
            reasoning_budget = int(payload.get("reasoning_budget", 0) or 0)
        except (TypeError, ValueError):
            reasoning_budget = 0
        max_output_tokens = _as_int(payload.get("max_output_tokens"), default=16384)
        context_window = _as_int(payload.get("context_window"), default=0)
        return ModelEntry(
            model=model,
            name=_clean_text(payload.get("name"), maximum=200),
            context_window=max(0, min(10_000_000, context_window)),
            max_output_tokens=max(256, min(128_000, max_output_tokens)),
            temperature=max(0.0, min(2.0, _as_float(payload.get("temperature"), default=0.2))),
            input_price_per_million=_clean_price(payload.get("input_price_per_million")),
            output_price_per_million=_clean_price(payload.get("output_price_per_million")),
            reasoning_style=reasoning_style,
            reasoning_effort=reasoning_effort,
            reasoning_budget=max(0, min(200_000, reasoning_budget)),
        )

    def _build_provider(self, payload: dict[str, Any], *, service_id: str) -> ModelProvider:
        preset = _clean_text(payload.get("preset"), maximum=40) or "custom"
        if preset not in PROVIDER_PRESETS:
            raise ProviderError(f"未知服务商预设：{preset}")
        defaults = PROVIDER_PRESETS[preset]
        base_url = _clean_text(payload.get("base_url"), maximum=500) or str(defaults.get("base_url") or "")
        if not base_url:
            raise ProviderError("请填写 Base URL。")
        raw_models = payload.get("models")
        models: list[ModelEntry] = []
        if isinstance(raw_models, list) and raw_models:
            models = [self._build_entry(entry) for entry in raw_models if isinstance(entry, dict) and _clean_text(entry.get("model"))]
        if not models:
            # 兼容 v1 扁平载荷：顶层 model 字段就是一个模型。
            # （``ModelServiceInput`` 的 models 默认是空列表，所以“空列表”不算是“显式清空”。）
            fallback = _clean_text(payload.get("model"), maximum=200)
            if fallback:
                models = [self._build_entry(payload)]
        if not models:
            default_model = _clean_text(defaults.get("model"), maximum=200)
            if default_model:
                models = [self._build_entry({**payload, "model": default_model})]
        return ModelProvider(
            service_id=service_id,
            name=_clean_text(payload.get("name"), maximum=120) or str(defaults.get("label") or preset),
            preset=preset,
            base_url=base_url,
            models=models,
        )

    # ---- 查询 ----------------------------------------------------------- #

    def list(self) -> list[dict[str, Any]]:
        return self._read()["services"]

    def roles(self) -> dict[str, dict[str, str]]:
        return self._read()["roles"]

    def role_binding(self, role: str) -> dict[str, str]:
        return self._read()["roles"].get(role, _empty_role())

    def get(self, service_id: str) -> dict[str, Any]:
        for item in self.list():
            if item.get("service_id") == service_id:
                return item
        raise ProviderError(f"模型服务不存在：{service_id}")

    def get_model(self, service_id: str, model: str) -> dict[str, Any]:
        provider = self.get(service_id)
        for entry in provider.get("models") or []:
            if entry.get("model") == model:
                return entry
        raise ProviderError(f"模型不存在：{model}")

    def resolve_model(self, service_id: str, model: str = "") -> str:
        """解析实际使用的模型：显式给出则校验存在，否则用该服务商的第一个模型。"""
        provider = self.get(service_id)
        models = provider.get("models") or []
        if model:
            if not any(entry.get("model") == model for entry in models):
                raise ProviderError(f"模型不存在：{model}")
            return model
        return str(models[0]["model"]) if models else ""

    def has_model(self, service_id: str, model: str) -> bool:
        try:
            return any(entry.get("model") == model for entry in (self.get(service_id).get("models") or []))
        except ProviderError:
            return False

    # ---- 写入 ----------------------------------------------------------- #

    def ensure_migrated(self) -> None:
        """把旧版 v1 扁平配置一次性落盘为 v2（同一个 Base URL 的服务合并成一个服务商）。
        失败不影响读取（读取时仍按迁移后视图）。"""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict) and raw.get("version") == 2:
            return
        try:
            self._write(self._read())
        except OSError:
            logger.exception("模型配置 v1→v2 迁移写盘失败（读取仍按迁移后视图）")

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = self._read()
        provider = self._build_provider(payload, service_id=uuid.uuid4().hex)
        value["services"].append(provider.to_dict())
        # 第一个服务商自动绑定到尚未设置的角色（绑到它的第一个模型），减少首次配置步骤。
        first_model = provider.models[0].model if provider.models else ""
        for role in MODEL_ROLES:
            if not value["roles"].get(role, {}).get("service_id"):
                value["roles"][role] = {"service_id": provider.service_id, "model": first_model}
        self._write(value)
        return provider.to_dict()

    def update(self, service_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        value = self._read()
        for index, item in enumerate(value["services"]):
            if item.get("service_id") != service_id:
                continue
            provider = self._build_provider({**item, **payload}, service_id=service_id)
            value["services"][index] = provider.to_dict()
            # 角色里引用的模型已不存在（被删/改名）时，回落到第一个模型，避免悬空绑定。
            for role in MODEL_ROLES:
                binding = value["roles"].get(role, _empty_role())
                if binding.get("service_id") == service_id and not self._model_in(provider, binding.get("model", "")):
                    value["roles"][role] = {"service_id": service_id, "model": provider.models[0].model if provider.models else ""}
            self._write(value)
            self._invalidate_policy(service_id, item, provider)
            return provider.to_dict()
        raise ProviderError(f"模型服务不存在：{service_id}")

    @staticmethod
    def _model_in(provider: ModelProvider, model: str) -> bool:
        return bool(model) and any(entry.model == model for entry in provider.models)

    def _invalidate_policy(self, service_id: str, previous: dict[str, Any], current: ModelProvider) -> None:
        """Base URL 变了清全部；否则只清思考协议变化的模型缓存。"""
        if str(previous.get("base_url") or "") != current.base_url:
            self.forget_thinking_policy(service_id)
            return
        old = {entry.get("model"): entry for entry in (previous.get("models") or [])}
        new = {entry.model: entry for entry in current.models}
        for model, entry in old.items():
            replacement = new.get(model)
            if replacement is None or str(entry.get("reasoning_style") or "") != replacement.reasoning_style:
                self.forget_thinking_policy(service_id, str(model))

    def delete(self, service_id: str) -> None:
        value = self._read()
        value["services"] = [item for item in value["services"] if item.get("service_id") != service_id]
        for role in MODEL_ROLES:
            if value["roles"].get(role, {}).get("service_id") == service_id:
                value["roles"][role] = _empty_role()
        self._write(value)
        delete_service_key(service_id)
        self.forget_thinking_policy(service_id)

    def set_roles(self, roles: dict[str, Any]) -> dict[str, dict[str, str]]:
        value = self._read()
        known = {item.get("service_id"): item for item in value["services"]}
        for role in MODEL_ROLES:
            if role not in roles:
                continue
            candidate = roles.get(role)
            if isinstance(candidate, str):
                candidate = {"service_id": candidate, "model": ""}
            if not isinstance(candidate, dict):
                continue
            service_id = _clean_text(candidate.get("service_id"), maximum=64)
            model = _clean_text(candidate.get("model"), maximum=200)
            if not service_id:
                value["roles"][role] = _empty_role()
                continue
            provider = known.get(service_id)
            if provider is None:
                raise ProviderError(f"模型服务不存在：{service_id}")
            models = provider.get("models") or []
            if model and not any(entry.get("model") == model for entry in models):
                raise ProviderError(f"模型不存在：{model}")
            value["roles"][role] = {"service_id": service_id, "model": model or (str(models[0]["model"]) if models else "")}
        self._write(value)
        return value["roles"]

    # ---- 密钥与 provider 构造 ------------------------------------------- #

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

    def config_for(self, service_id: str, model: str = "", *, timeout: float = 180.0):
        from .providers import OpenAICompatibleProvider
        provider = self.get(service_id)
        resolved = self.resolve_model(service_id, model)
        entry = self.get_model(service_id, resolved) if resolved else None
        if entry is None:
            raise ProviderError("该服务商还没有可用的模型，请先在模型配置里添加。")
        config = ModelEntry(**{key: entry.get(key) for key in ModelEntry.__dataclass_fields__}).config(
            provider_id=service_id, base_url=str(provider.get("base_url") or ""),
        )
        return OpenAICompatibleProvider(config, api_key=self.api_key(service_id), timeout=timeout)

    def list_models(self, service_id: str, *, timeout: float = 30.0) -> list[dict[str, Any]]:
        """用该服务已保存的 base URL 与密钥拉取远端模型列表（含上下文等元数据）。"""
        return self.probe_models(service_id=service_id, timeout=timeout)

    def probe_models(
        self, *, base_url: str = "", api_key: str = "", service_id: str = "", timeout: float = 30.0,
    ) -> list[dict[str, Any]]:
        """拉取远端 `/v1/models`：可只给 base URL + key（新建服务尚未保存），
        也可给 service_id 复用已保存的 base URL / Keychain 密钥，缺什么补什么。"""
        from .providers import list_remote_models
        resolved_base = _clean_text(base_url, maximum=500)
        resolved_key = (api_key or "").strip()
        if service_id:
            service = self.get(service_id)
            resolved_base = resolved_base or str(service.get("base_url") or "")
            resolved_key = resolved_key or self.api_key(service_id)
        return list_remote_models(resolved_base, resolved_key, timeout=timeout)


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
