import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { ModelEntry, ModelRole, ModelRoleBinding, ModelRoles, ModelService, RemoteModel } from "./types";

/** 服务商创建/更新载荷：models 整套替换（PUT 传新数组即增/删/改）。 */
export type ModelServicePayload = {
  name: string;
  preset: string;
  base_url: string;
  models: ModelEntry[];
};

export type ModelServicesApi = {
  services: ModelService[];
  roles: ModelRoles;
  ready: boolean;
  /** 角色绑定的服务商；未绑定或服务商已删除时返回 undefined。 */
  serviceFor: (role: ModelRole) => ModelService | undefined;
  /** 角色绑定的模型条目；模型未绑定/已删除时回退到该服务商第一个模型。 */
  entryFor: (role: ModelRole) => ModelEntry | undefined;
  /** 角色绑定的 {service_id, model}；用于任务界面默认选中。 */
  roleBinding: (role: ModelRole) => ModelRoleBinding;
  refresh: () => Promise<void>;
  create: (payload: ModelServicePayload) => Promise<ModelService>;
  update: (serviceId: string, payload: ModelServicePayload) => Promise<ModelService>;
  remove: (serviceId: string) => Promise<void>;
  saveKey: (serviceId: string, value: string) => Promise<void>;
  check: (serviceId: string, model?: string) => Promise<{ ok: boolean; model: string; latency_ms: number; sample: string }>;
  /** 拉取远端 `/v1/models`；新建服务商尚未保存时传 base_url + api_key 即可。 */
  fetchModels: (params: { base_url?: string; api_key?: string; service_id?: string }) => Promise<RemoteModel[]>;
  setRoles: (roles: Partial<ModelRoles>) => Promise<void>;
};

const emptyBinding = (): ModelRoleBinding => ({ service_id: "", model: "" });
const EMPTY_ROLES: ModelRoles = { structure: emptyBinding(), translation: emptyBinding() };

function legacyProvider(): Record<string, unknown> | null {
  try { return JSON.parse(window.localStorage.getItem("literature.provider") || "null"); } catch { return null; }
}

function normalizeRoles(value: unknown): ModelRoles {
  const source = (value ?? {}) as Partial<Record<ModelRole, unknown>>;
  const pick = (raw: unknown): ModelRoleBinding => {
    if (typeof raw === "string") return { service_id: raw, model: "" };
    const item = (raw ?? {}) as Partial<ModelRoleBinding>;
    return { service_id: String(item.service_id ?? ""), model: String(item.model ?? "") };
  };
  return { structure: pick(source.structure), translation: pick(source.translation) };
}

/** 服务商库（provider → models 两层）+ 任务角色绑定；密钥只存在后端 Keychain，前端从不读取。 */
export function useModelServices(): ModelServicesApi {
  const [services, setServices] = useState<ModelService[]>([]);
  const [roles, setRoleState] = useState<ModelRoles>(EMPTY_ROLES);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    const value = await api<{ services: ModelService[]; roles: ModelRoles }>("/model-services");
    setServices(value.services);
    setRoleState(normalizeRoles(value.roles));
  }, []);

  // 首次使用时把旧的单一 provider 配置迁移成一个服务商 + 一个模型，避免用户重录。
  const migrateLegacy = useCallback(async () => {
    const current = await api<{ services: ModelService[] }>("/model-services");
    if (current.services.length) return;
    const legacy = legacyProvider();
    if (!legacy || !legacy.model || !legacy.base_url) return;
    const name = String(legacy.provider || "custom");
    const preset = ["openai", "deepseek", "glm", "qwen"].includes(name) ? name : "custom";
    await api("/model-services", {
      method: "POST",
      body: JSON.stringify({
        name: preset === "custom" ? "自定义中转站" : name,
        preset,
        base_url: legacy.base_url,
        models: [{
          model: String(legacy.model),
          name: String(legacy.model),
          temperature: legacy.temperature ?? 0.2,
          max_output_tokens: legacy.max_output_tokens ?? 8192,
          input_price_per_million: legacy.input_price_per_million ?? null,
          output_price_per_million: legacy.output_price_per_million ?? null,
        }],
      }),
    });
  }, []);

  useEffect(() => {
    refresh()
      .then(migrateLegacy)
      .then(refresh)
      .catch(() => {})
      .finally(() => setReady(true));
  }, [refresh, migrateLegacy]);

  const create = useCallback(async (payload: ModelServicePayload) => {
    const created = await api<ModelService>("/model-services", { method: "POST", body: JSON.stringify(payload) });
    await refresh();
    return created;
  }, [refresh]);

  const update = useCallback(async (serviceId: string, payload: ModelServicePayload) => {
    const updated = await api<ModelService>(`/model-services/${serviceId}`, { method: "PUT", body: JSON.stringify(payload) });
    await refresh();
    return updated;
  }, [refresh]);

  const remove = useCallback(async (serviceId: string) => {
    await api(`/model-services/${serviceId}`, { method: "DELETE" });
    await refresh();
  }, [refresh]);

  const saveKey = useCallback(async (serviceId: string, value: string) => {
    await api(`/model-services/${serviceId}/key`, { method: "PUT", body: JSON.stringify({ value }) });
    await refresh();
  }, [refresh]);

  const check = useCallback(async (serviceId: string, model?: string) =>
    api<{ ok: boolean; model: string; latency_ms: number; sample: string }>(`/model-services/${serviceId}/check`, { method: "POST", body: JSON.stringify(model ? { model } : {}) }), []);

  const fetchModels = useCallback(async (params: { base_url?: string; api_key?: string; service_id?: string }) => {
    const value = await api<{ models: RemoteModel[] }>("/model-services/models", { method: "POST", body: JSON.stringify(params) });
    return value.models;
  }, []);

  const setRoles = useCallback(async (patch: Partial<ModelRoles>) => {
    const value = await api<ModelRoles>("/model-services/roles", { method: "PUT", body: JSON.stringify(patch) });
    setRoleState(normalizeRoles(value));
  }, []);

  const serviceFor = useCallback((role: ModelRole) => {
    const binding = roles[role] ?? emptyBinding();
    return services.find(item => item.service_id === binding.service_id);
  }, [services, roles]);

  const entryFor = useCallback((role: ModelRole) => {
    const provider = serviceFor(role);
    if (!provider) return undefined;
    const binding = roles[role] ?? emptyBinding();
    return provider.models.find(item => item.model === binding.model) ?? provider.models[0];
  }, [serviceFor, roles]);

  const roleBinding = useCallback((role: ModelRole) => roles[role] ?? emptyBinding(), [roles]);

  return { services, roles, ready, serviceFor, entryFor, roleBinding, refresh, create, update, remove, saveKey, check, fetchModels, setRoles };
}
