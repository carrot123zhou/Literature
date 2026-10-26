/**
 * foldApi —— 整篇折叠作业客户端（严格对齐 backend 实际 JSON）：
 * - POST /projects/{id}/reader/fold-runs            body {locale, force?} → { run_id, ... }
 * - GET  /projects/{id}/reader/fold-run             → { status, progress: {ratio,...}, ready?, errors: [...], run_id? }
 * - POST /projects/{id}/reader/fold-runs/{run_id}/pause | /resume
 * - GET  /projects/{id}/reader/layers/native?locale=<l> → 上游 paper-data 形状
 *
 * 实际状态枚举：running / pausing / paused / ready / failed（含 idle=无 run）。
 * progress 是对象 {ratio,...}，不是数字；没有 paused 布尔（status===paused 即暂停态）；
 * errors 是数组。字段猜错就 NaN/永远等不到 ready——这里逐字段显式解析。
 * 生产禁止 fixture/模拟（lead 2026-10-25）；错误如实抛出，宿主显示可操作错误。
 */
import { api } from "../api";

export type FoldRunStatus = "idle" | "running" | "pausing" | "paused" | "ready" | "failed";
export type FoldProgress = { ratio: number } & Record<string, unknown>;
export type FoldJobState = {
  status: FoldRunStatus;
  /** 后端 progress.ratio 0..1；缺失/非法时 null（宿主不得当 0 显示假进度）。 */
  progress: FoldProgress | null;
  /** 后端显式 ready 布尔：仅 status=ready 且 ready=true 才可启动。 */
  ready: boolean;
  errors: string[];
  runId: string;
};

const asRecord = (value: unknown): Record<string, unknown> =>
  (value && typeof value === "object" ? value : {}) as Record<string, unknown>;

/** 严格解析 fold-run 响应；与后端字段漂移在这里显式显形，不被 Number()/Boolean() 静默成 NaN/false。 */
export function parseFoldRun(value: unknown): FoldJobState {
  const rec = asRecord(value);
  const rawStatus = typeof rec.status === "string" ? rec.status : "idle";
  const status = (["idle", "running", "pausing", "paused", "ready", "failed"] as FoldRunStatus[]).includes(rawStatus as FoldRunStatus)
    ? rawStatus as FoldRunStatus : "failed";
  const progressRec = asRecord(rec.progress);
  const ratio = Number(progressRec.ratio);
  return {
    status,
    progress: rec.progress && typeof rec.progress === "object" && Number.isFinite(ratio) ? { ...progressRec, ratio } : null,
    ready: rec.ready === true,
    errors: Array.isArray(rec.errors) ? rec.errors.map(item => (typeof item === "string" ? item : String(asRecord(item).message ?? asRecord(item).error ?? "折叠未完成，请重试"))) : [],
    runId: typeof rec.run_id === "string" ? rec.run_id : typeof rec.id === "string" ? rec.id : "",
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
