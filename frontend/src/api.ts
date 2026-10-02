import { invoke } from "@tauri-apps/api/core";

const base = import.meta.env.VITE_API_URL || "http://127.0.0.1:41731";
let token = import.meta.env.VITE_LITERATURE_TOKEN || "";
const requiredCapabilities = ["intake_items_v1", "structure_review_v2", "model_services_v1", "structure_job_v1"];

export async function initializeApi() {
  if (!token && "__TAURI_INTERNALS__" in window) token = await invoke<string>("session_token");
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch(`${base}/health`);
      if (response.ok) {
        const health = await response.json() as { capabilities?: string[] };
        const missing = requiredCapabilities.filter(name => !health.capabilities?.includes(name));
        if (missing.length === 0) return;
        throw new Error(`Literature 前后端版本不一致（缺少能力：${missing.join("、")}），请完全退出后重新打开 Literature。`);
      }
    } catch (error) {
      if (error instanceof Error && error.message.includes("前后端版本不一致")) throw error;
      /* The sidecar is still starting. */
    }
    await new Promise(resolve => window.setTimeout(resolve, 250));
  }
  throw new Error("Literature 本地服务未能在 10 秒内启动。");
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("x-literature-token", token);
  if (!(init.body instanceof FormData) && !headers.has("content-type")) headers.set("content-type", "application/json");
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers,
  });
  if (!response.ok) {
    const value = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(value.detail || `请求失败：${response.status}`);
  }
  return response.json();
}

export function apiUrl(path: string) { return `${base}${path}`; }
export function apiHeaders() { return { "x-literature-token": token }; }
