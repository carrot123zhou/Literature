import { api, apiUrl } from "../api";
import type { ReaderLayers, ReaderOverlay, ReaderState } from "./types";

/**
 * 阅读层 API（契约冻结自 literature/api.py + literature/reader.py）。
 * 对话为服务端持久化会话：GET /reader/chat 投影 DTO，POST messages/stop/retry，
 * quote = {block_id, pane, text}，模型覆写 = {service_id, model}。
 */

export type ReaderChatWire = {
  conversation_id: string;
  messages: {
    id: string;
    role: "user" | "assistant";
    question?: string;
    answer?: string;
    citations?: { block_id: string; page_index: number; page: number; section: string; text: string; note: string }[];
    model?: string;
    binding?: { service_id: string; model: string };
    state: "sending" | "done" | "error" | "stopped";
    error?: string;
    created_at?: string;
    quote?: { block_id: string; pane: string; snippet: string; srcText: string; sec: string } | null;
  }[];
  updated_at?: string;
};

export const readerApi = {
  state: (projectId: string, locale: string) =>
    api<ReaderState>(`/projects/${projectId}/reader/state?locale=${encodeURIComponent(locale)}`),

  layers: (projectId: string, locale: string) =>
    api<ReaderLayers>(`/projects/${projectId}/reader/layers?locale=${encodeURIComponent(locale)}`),

  /** 触发五层折叠：mode=auto 用模型（需 service_id 或 reader_fold 角色绑定），mode=local 仅本地摘句。 */
  generateLayers: (projectId: string, body: { locale: string; service_id?: string; model?: string; mode?: "auto" | "local"; force?: boolean; section_ids?: string[] }) =>
    api<{ job_id: string; status: string; locale: string }>(`/projects/${projectId}/reader/layers`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  clearLayers: (projectId: string, locale: string) =>
    api<{ cleared: string }>(`/projects/${projectId}/reader/layers?locale=${encodeURIComponent(locale)}`, { method: "DELETE" }),

  /** 保存 overlay（批注+偏好）；base_revision 乐观并发，冲突时后端抛 StorageError。 */
  saveOverlay: (projectId: string, overlay: ReaderOverlay, baseRevision?: number) =>
    api<ReaderOverlay>(`/projects/${projectId}/reader/overlay`, {
      method: "PUT",
      body: JSON.stringify({ overlay, base_revision: baseRevision }),
    }),


};
