/**
 * foldApi —— 整篇折叠作业客户端（严格对齐 backend 实际 JSON）：
 * - POST /projects/{id}/reader/fold-runs            body {locale, force?} → { run_id, ... }
 * - GET  /projects/{id}/reader/fold-run             → { status, progress: {ratio,...}, ready?, errors: [...], run_id?, activity? }
 * - POST /projects/{id}/reader/fold-runs/{run_id}/pause | /resume
 * - GET  /projects/{id}/reader/layers/native?locale=<l> → 上游 paper-data 形状
 *
 * 实际状态枚举：running / pausing / paused / ready / failed（含 idle=无 run）。
 * progress 是对象 {ratio,...}，不是数字；没有 paused 布尔（status===paused 即暂停态）；
 * errors 是数组。字段猜错就 NaN/永远等不到 ready——这里逐字段显式解析。
 * `activity` 是**可选**证据（旧 run/旧后端没有）：必须原样保留，不能当未知字段丢掉。
 * 生产禁止 fixture/模拟（lead 2026-10-25）；错误如实抛出，宿主显示可操作错误。
 */
import { api } from "../api";
import type { ReaderFoldActivity } from "./types";

export type FoldRunStatus = "idle" | "running" | "pausing" | "paused" | "ready" | "failed";
export type FoldProgress = { ratio: number } & Record<string, unknown>;
export type FoldJobState = {
  status: FoldRunStatus;
  /** 后端 progress.ratio 0..1；缺失/非法时 null（宿主不得当 0 显示假进度）。 */
  progress: FoldProgress | null;
  /** 后端显式 ready 布尔：仅 status=ready 且 ready=true 才可启动。 */
  ready: boolean;
  /**
   * 已有可进入成果（部分完成也算）：主按钮「进入折叠」的依据。
   * 旧后端缺 readable 时回退 ready===true；两字段都缺视为不可进入。
   */
  readable: boolean;
  /** 成果可用性摘要（各层 pending 计数等，shape 由后端契约冻结）；缺失为 null，不猜数值。 */
  availability: Record<string, unknown> | null;
  errors: string[];
  runId: string;
  /** 实时活动（阶段/节/批次/计时/待补），可选；缺失=没有可展示的证据，不是 0。 */
  activity: ReaderFoldActivity | null;
};

const asRecord = (value: unknown): Record<string, unknown> =>
  (value && typeof value === "object" ? value : {}) as Record<string, unknown>;

/**
 * `activity` 解析（可选）：字段名/类型逐项显式，变化不会静默变成 0。
 * 后端契约：stage 公开值 waiting/generating/validating/saving/source/derive/apparatus；
 * batch_index **0 起**；pending_units 是当前 section/lane 仍待补的 unit 数；
 * 时间戳可能是 ISO 或 epoch（秒/毫秒都由展示层 foldTimeMs 归一）。
 * 老后端无该字段时返回 null；整个 dict 为空（{}）也返回 null，不造阶段。
 */
export function parseFoldActivity(raw: unknown): ReaderFoldActivity | null {
  if (!raw || typeof raw !== "object") return null;
  const rec = asRecord(raw);
  const text = (key: string) => (typeof rec[key] === "string" ? String(rec[key]).trim() : "");
  const num = (key: string) => (typeof rec[key] === "number" && Number.isFinite(rec[key]) ? Number(rec[key]) : undefined);
  const activity: ReaderFoldActivity = {
    stage: text("stage"),
    state: text("state"),
    section_id: text("section_id"),
    batch_index: num("batch_index"),
    batch_total: num("batch_total"),
    request_started_at: typeof rec.request_started_at === "string" || typeof rec.request_started_at === "number"
      ? rec.request_started_at as string | number : null,
    last_response_at: typeof rec.last_response_at === "string" || typeof rec.last_response_at === "number"
      ? rec.last_response_at as string | number : null,
    elapsed_seconds: num("elapsed_seconds"),
    pending_units: num("pending_units"),
    retry_reason: text("retry_reason"),
  };
  const empty = !activity.stage && !activity.state && !activity.section_id && !activity.retry_reason
    && activity.elapsed_seconds === undefined && activity.pending_units === undefined
    && activity.batch_index === undefined && activity.batch_total === undefined;
  return empty ? null : activity;
}

/** 严格解析 fold-run 响应；与后端字段漂移在这里显式显形，不被 Number()/Boolean() 静默成 NaN/false。 */
export function parseFoldRun(value: unknown): FoldJobState {
  const rec = asRecord(value);
  const rawStatus = typeof rec.status === "string" ? rec.status : "idle";
  const status = (["idle", "running", "pausing", "paused", "ready", "failed"] as FoldRunStatus[]).includes(rawStatus as FoldRunStatus)
    ? rawStatus as FoldRunStatus : "failed";
  const progressRec = asRecord(rec.progress);
  const ratio = Number(progressRec.ratio);
  const availability = rec.availability && typeof rec.availability === "object" && !Array.isArray(rec.availability)
    ? { ...(rec.availability as Record<string, unknown>) } : null;
  return {
    status,
    progress: rec.progress && typeof rec.progress === "object" && Number.isFinite(ratio) ? { ...progressRec, ratio } : null,
    ready: rec.ready === true,
    // readable 优先；旧后端没有该字段时回退 ready（无 ready 字段即 false，不猜）。
    readable: typeof rec.readable === "boolean" ? rec.readable : rec.ready === true,
    availability,
    errors: Array.isArray(rec.errors) ? rec.errors.map(item => (typeof item === "string" ? item : String(asRecord(item).message ?? asRecord(item).error ?? "折叠未完成，请重试"))) : [],
    runId: typeof rec.run_id === "string" ? rec.run_id : typeof rec.id === "string" ? rec.id : "",
    activity: parseFoldActivity(rec.activity),
  };
}

const base = (projectId: string) => `/projects/${projectId}/reader/fold-runs`;

export const foldJobApi = {
  /** 发起整篇折叠（用户点 AIFold 即明确授权）。 */
  async start(projectId: string, locale: string, force = false): Promise<string> {
    const value = await api<Record<string, unknown>>(base(projectId), {
      method: "POST",
      body: JSON.stringify({ locale, force }),
    });
    const runId = typeof value.run_id === "string" ? value.run_id : typeof value.id === "string" ? value.id : "";
    if (!runId) throw new Error("fold-runs 响应缺少 run_id");
    return runId;
  },
  async state(projectId: string): Promise<FoldJobState> {
    return parseFoldRun(await api<unknown>(`/projects/${projectId}/reader/fold-run`));
  },
  pause: (projectId: string, runId: string) =>
    api(`${base(projectId)}/${encodeURIComponent(runId)}/pause`, { method: "POST" }),
  resume: (projectId: string, runId: string) =>
    api(`${base(projectId)}/${encodeURIComponent(runId)}/resume`, { method: "POST" }),
  /** 上游 paper-data 产物（locale 由宿主按 targetLocale 传）。 */
  native: <T>(projectId: string, locale: string): Promise<T> =>
    api<T>(`/projects/${projectId}/reader/layers/native?locale=${encodeURIComponent(locale)}`),
};
