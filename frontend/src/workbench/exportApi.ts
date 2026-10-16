import { api, apiHeaders, apiUrl } from "../api";
import type { ExportOptions, ExportRecord, ExportRequest } from "../types";

/** 导出能力的前端入口：选项、创建、历史与文件下载。 */

export function fetchExportOptions(projectId: string, locale: string) {
  return api<ExportOptions>(`/projects/${projectId}/export/options?locale=${encodeURIComponent(locale)}`);
}

export function createExport(projectId: string, body: ExportRequest) {
  return api<ExportRecord>(`/projects/${projectId}/export`, { method: "POST", body: JSON.stringify(body) });
}

export function fetchExports(projectId: string) {
  return api<ExportRecord[]>(`/projects/${projectId}/exports`);
}

export function renameExport(projectId: string, exportId: string, displayName: string) {
  return api<ExportRecord>(`/projects/${projectId}/exports/${encodeURIComponent(exportId)}`, {
    method: "PATCH", body: JSON.stringify({ display_name: displayName }),
  });
}

export function deleteExport(projectId: string, exportId: string) {
  return api<{ export_id: string; deleted: boolean }>(`/projects/${projectId}/exports/${encodeURIComponent(exportId)}`, { method: "DELETE" });
}

export function exportFileUrl(projectId: string, exportId: string, relPath: string) {
  return apiUrl(`/projects/${projectId}/exports/${encodeURIComponent(exportId)}/file?name=${encodeURIComponent(relPath)}`);
}

/** 浏览器开发态下载：本地服务需要会话令牌，不能直接用 <a href>。 */
export async function downloadExportFile(projectId: string, exportId: string, relPath: string) {
  const response = await fetch(exportFileUrl(projectId, exportId, relPath), { headers: apiHeaders() });
  if (!response.ok) {
    const value = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(typeof value.detail === "string" ? value.detail : `下载失败：${response.status}`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = relPath.split("/").pop() || "export";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
