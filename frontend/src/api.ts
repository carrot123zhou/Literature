import { isDesktop, runtimeInfo } from "./desktop";

let base = import.meta.env.VITE_API_URL || "http://127.0.0.1:41731";
let token = import.meta.env.VITE_LITERATURE_TOKEN || "";
let ready: Promise<void> | undefined;
const requiredCapabilities = ["intake_items_v1", "structure_review_v2", "model_services_v1", "model_services_v2", "model_services_v3", "structure_job_v1", "structure_modes_v1", "structure_pause_v1", "structure_vision_v1", "parse_backends_v1", "desktop_resources_v1", "translation_module_v1", "translation_engine_v2", "resource_pipeline_v1", "translation_safety_v1", "parse_lifecycle_v1", "task_history_v1"];

export function initializeApi(): Promise<void> {
  return ready ||= connect();
}
async function connect() {
  if (isDesktop) { const info = await runtimeInfo(); base = info.apiUrl; token = info.token; }
  const deadline = Date.now() + 30_000;
  for (let attempt = 0; attempt < 120 && Date.now() < deadline; attempt += 1) {
    try {
      const response = await fetch(`${base}/health`, { signal: AbortSignal.timeout(1500) });
      if (response.ok) {
        const health = await response.json() as { capabilities?: string[] };
        const missing = requiredCapabilities.filter(name => !health.capabilities?.includes(name));
        if (missing.length) throw new Error(`Literature 前后端版本不一致（缺少能力：${missing.join("、")}），请完全退出后重新打开 Literature。`);
        // Verify the process belongs to this session, even if a port was claimed during startup.
        const authenticated = await fetch(`${base}/desktop/resources`, { headers: { "x-literature-token": token }, signal: AbortSignal.timeout(1500) });
        if (!authenticated.ok) throw new Error("Literature 本地服务会话校验失败，请完全退出后重新打开。");
        return;
      }
    } catch (error) {
      if (error instanceof Error && (error.message.includes("前后端版本不一致") || error.message.includes("会话校验失败"))) throw error;
      // 重连时端口与令牌都可能已轮换：必须写回新的 runtimeInfo，否则会一直拿旧地址/旧令牌重试。
      if (isDesktop) { const info = await runtimeInfo(); base = info.apiUrl; token = info.token; }
    }
    await new Promise(resolve => window.setTimeout(resolve, 250));
  }
  throw new Error("Literature 本地服务启动超时，请检查设置中的运行日志并重新启动应用。");
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  await initializeApi();
  const headers = new Headers(init.headers);
  headers.set("x-literature-token", token);
  if (!(init.body instanceof FormData) && !headers.has("content-type")) headers.set("content-type", "application/json");
  const response = await fetch(`${base}${path}`, { ...init, headers });
  if (!response.ok) {
    const value = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(typeof value.detail === "string" ? value.detail : `请求失败：${response.status}`);
  }
  return response.json();
}
export function apiUrl(path: string) { return `${base}${path}`; }
export function apiHeaders() { return { "x-literature-token": token }; }
