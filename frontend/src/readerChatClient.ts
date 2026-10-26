/**
 * 阅读对话的持久化客户端（后端 `ReaderChatRunner` 的配对实现）。
 *
 * 为什么不是「前端 useState 里放两条消息」：需求要求会话历史、按条冻结模型、停止与重试都真实落地。
 * 后端把每一轮落在项目目录 `reader/chat.json`（独立交互层，不碰 document/translation），前端只做投影：
 *
 * - 发送：POST /reader/chat/messages → 立即返回 user/assistant 两条消息（assistant 状态 sending）；
 *   生成本身在服务端线程里跑，页面刷新/关掉侧栏都不会丢。
 * - 停止：POST .../stop → 消息标记 stopped，迟到的回答被服务端丢弃（不写文本、不伪装成功）。
 * - 重试：POST .../retry → 用同一条提问、同一份冻结引用重新生成（模型可用新绑定）。
 * - 引用一次性：quote 只随这一条用户消息持久化；发送后清空前端待发送引用即可，后端不会沿用。
 *
 * 传给 designer 的 `ReaderWorkspace` 时，只需把本地 messages state 换成这里的 `messages`，
 * 并把 send/stop/retry/clear 接到输入区与消息操作上（类型见 `ReaderChatWireMessage`）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

export type ReaderChatBinding = { service_id: string; model: string };

export type ReaderChatWireQuote = {
  block_id: string;
  pane: "source" | "trans";
  /** 读者选中的文字（译文引用时是译文）。 */
  snippet: string;
  /** 关联的**原文**段落文本：证据永远取原文，不做精确回译。 */
  srcText: string;
  /** 段落出处（章节名）。 */
  sec: string;
};

export type ReaderChatWireCitation = {
  block_id: string;
  page: number;
  section: string;
  text: string;
};

export type ReaderChatWireMessage = {
  id: string;
  role: "user" | "assistant";
  question?: string;
  answer?: string;
  quote?: ReaderChatWireQuote | null;
  citations?: ReaderChatWireCitation[];
  /** 展示名（服务名 · 模型名）；历史标记的是**实际**响应的模型。 */
  model?: string;
  /** 真实路由：`service_id + model`（同名模型靠 service_id 区分；重试用它）。 */
  binding: ReaderChatBinding;
  state: "sending" | "done" | "error" | "stopped";
  error?: string;
  created_at?: string;
};

type ChatPayload = { conversation_id: string; messages: ReaderChatWireMessage[]; updated_at?: string };

type SendInput = {
  question: string;
  locale: string;
  quote?: { block_id: string; pane: string; text: string } | null;
  binding: ReaderChatBinding;
};

const POLL_MS = 700;

export function useReaderChat(projectId: string, enabled = true) {
  const [messages, setMessages] = useState<ReaderChatWireMessage[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const timer = useRef(0);
  const alive = useRef(true);
  /**
   * 项目代次：切项目时 +1。
   *
   * 竞态（2026-10-25 修复）：A 项目有请求在途 → 用户切到 B → 组件没有 key 重挂时 `alive` 又被置回 true，
   * A 的迟到响应会把 A 的历史写进 B 的界面，甚至 stop/retry 作用到 B（错项目）。
   * 现在每个异步操作开始时抄下 `epoch`，回来时代次不一致就整段丢弃。
   */
  const epoch = useRef(0);

  const apply = useCallback((payload: ChatPayload | null, at?: number) => {
    if (!payload) return;
    if (at !== undefined && at !== epoch.current) return;
    setMessages(payload.messages || []);
  }, []);

  const refresh = useCallback(async () => {
    if (!projectId) return;
    const at = epoch.current;
    try {
      const payload = await api<ChatPayload>(`/projects/${projectId}/reader/chat`);
      if (at !== epoch.current) return;
      apply(payload, at);
      setError("");
    } catch (reason) {
      if (at !== epoch.current) return;
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [projectId, apply]);

  useEffect(() => {
    alive.current = true;
    epoch.current += 1;
    window.clearTimeout(timer.current);
    if (!enabled || !projectId) { setMessages([]); return; }
    // 切项目先清空：宁可短暂空，也不把上一本文的对话留在界面上。
    setMessages([]);
    void refresh();
    return () => { alive.current = false; window.clearTimeout(timer.current); };
  }, [enabled, projectId, refresh]);

  // 有消息在生成中时轮询：后端线程生成，前端只等结果（没有 token 级流式，不假装有）。
  const pending = messages.some(message => message.state === "sending");
  useEffect(() => {
    window.clearTimeout(timer.current);
    if (!pending || !projectId) return;
    timer.current = window.setTimeout(() => { if (alive.current) void refresh(); }, POLL_MS);
    return () => window.clearTimeout(timer.current);
  }, [pending, messages, projectId, refresh]);

  const send = useCallback(async (input: SendInput) => {
    if (!input.question.trim()) return null;
    const at = epoch.current;
    setBusy(true);
    // 后端 `ReaderChatInput` 只认**顶层** service_id/model；直接把 {binding:{...}} 发出去会被 pydantic
    // 当未知字段丢掉，实际回落到角色绑定（缺陷 D1：选择器选了 svc-other，请求仍打 svc-chat）。
    // 这里显式解包；binding 为空时不传键，保持「未绑定 → 角色绑定 → 旧配置继承 translation」。
    const payload: Record<string, unknown> = {
      question: input.question,
      locale: input.locale,
      quote: input.quote ?? null,
    };
    if (input.binding?.service_id) payload.service_id = input.binding.service_id;
    if (input.binding?.model) payload.model = input.binding.model;
    try {
      const value = await api<{ chat: ChatPayload; user_message_id: string; assistant_message_id: string }>(
        `/projects/${projectId}/reader/chat/messages`,
        { method: "POST", body: JSON.stringify(payload) },
      );
      if (at !== epoch.current) return null;   // 已切到别的书：这条结果只属于旧书
      apply(value.chat, at);
      setError("");
      return value;
    } catch (reason) {
      if (at === epoch.current) setError(reason instanceof Error ? reason.message : String(reason));
      throw reason;
    } finally {
      if (at === epoch.current) setBusy(false);
    }
  }, [projectId, apply]);

  const stop = useCallback(async (messageId: string) => {
    const at = epoch.current;
    const value = await api<ChatPayload>(`/projects/${projectId}/reader/chat/messages/${messageId}/stop`, { method: "POST" });
    apply(value, at);
  }, [projectId, apply]);

  const retry = useCallback(async (messageId: string, binding: ReaderChatBinding, locale: string) => {
    const at = epoch.current;
    const value = await api<{ chat: ChatPayload; assistant_message_id: string }>(
      `/projects/${projectId}/reader/chat/messages/${messageId}/retry`,
      { method: "POST", body: JSON.stringify({ service_id: binding.service_id, model: binding.model, locale }) },
    );
    apply(value.chat, at);
    return value;
  }, [projectId, apply]);

  const clear = useCallback(async () => {
    const at = epoch.current;
    apply(await api<ChatPayload>(`/projects/${projectId}/reader/chat`, { method: "DELETE" }), at);
  }, [projectId, apply]);

  return { messages, error, busy, pending, send, stop, retry, clear, refresh };
}
