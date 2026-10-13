import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { ModelRole, ModelRoles, ModelService } from "./types";

export type ModelServicePayload = {
  name: string;
  preset: string;
  base_url: string;
  model: string;
  temperature?: number;
  max_output_tokens?: number;
  input_price_per_million?: number | null;
  output_price_per_million?: number | null;
  reasoning_style?: string;
  reasoning_effort?: string;
  reasoning_budget?: number;
};

export type ModelServicesApi = {
  services: ModelService[];
  roles: ModelRoles;
  ready: boolean;
  serviceFor: (role: ModelRole) => ModelService | undefined;
  refresh: () => Promise<void>;
  create: (payload: ModelServicePayload) => Promise<ModelService>;
  update: (serviceId: string, payload: ModelServicePayload) => Promise<ModelService>;
  remove: (serviceId: string) => Promise<void>;
  saveKey: (serviceId: string, value: string) => Promise<void>;
  check: (serviceId: string) => Promise<{ ok: boolean; model: string; latency_ms: number; sample: string }>;
  listModels: (serviceId: string) => Promise<string[]>;
  setRoles: (roles: Partial<ModelRoles>) => Promise<void>;
};

const EMPTY_ROLES: ModelRoles = { structure: "", translation: "" };

function legacyProvider(): Record<string, unknown> | null {
  try { return JSON.parse(window.localStorage.getItem("literature.provider") || "null"); } catch { return null; }
}

/** 模型服务库 + 任务角色绑定；密钥只存在后端 Keychain，前端从不读取。 */
export function useModelServices(): ModelServicesApi {
  const [services, setServices] = useState<ModelService[]>([]);
  const [roles, setRoleState] = useState<ModelRoles>(EMPTY_ROLES);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    const value = await api<{ services: ModelService[]; roles: ModelRoles }>("/model-services");
    setServices(value.services);
    setRoleState({ ...EMPTY_ROLES, ...value.roles });
  }, []);

  // 首次使用时把旧的单一 provider 配置迁移成一个模型服务，避免用户重录。
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
        model: legacy.model,
        temperature: legacy.temperature ?? 0.2,
        max_output_tokens: legacy.max_output_tokens ?? 8192,
        input_price_per_million: legacy.input_price_per_million ?? null,
        output_price_per_million: legacy.output_price_per_million ?? null,
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

  const check = useCallback(async (serviceId: string) => api<{ ok: boolean; model: string; latency_ms: number; sample: string }>(`/model-services/${serviceId}/check`, { method: "POST" }), []);

  const listModels = useCallback(async (serviceId: string) => {
    const value = await api<{ models: string[] }>(`/model-services/${serviceId}/models`, { method: "POST" });
    return value.models;
  }, []);

  const setRoles = useCallback(async (patch: Partial<ModelRoles>) => {
    const value = await api<ModelRoles>("/model-services/roles", { method: "PUT", body: JSON.stringify(patch) });
    setRoleState({ ...EMPTY_ROLES, ...value });
  }, []);

  const serviceFor = useCallback((role: ModelRole) => services.find(item => item.service_id === roles[role]), [services, roles]);

  return { services, roles, ready, serviceFor, refresh, create, update, remove, saveKey, check, listModels, setRoles };
}
