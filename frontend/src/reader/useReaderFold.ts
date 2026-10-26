import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { readerApi } from "./readerApi";
import type { ReaderLayers, ReaderRoleState, ReaderState } from "./types";

/**
 * useReaderFold —— 阅读器五层折叠的生成状态机（接管 autoLocal / AI 生成 / 停止 / 失败重折）。
 *
 * 契约（与 literature/reader.py ReaderLayerService + /reader/layers* 一一对应）：
 *
 * - **local 不是成品**：后端 `content` 字段（"local" | "model" | "mixed"）是唯一来源真相；
 *   本地摘句后 `generated=true` 但 `content="local"`，`phase` 仍是 "local"，升级入口常驻。
 * - **模型绑定只读 state.roles.fold**：本 hook 从不读 chatBinding/defaultFold，也**不向后端
 *   传显式 service_id/model**——由后端按 reader_fold → 继承翻译默认 解析（api._reader_require）。
 *   这消除了「折叠误用对话模型」的接线缺陷。
 * - **原文/译文两个作业分别跟踪**：`state.layers.jobs[locale]`（layer_state 新增）按 locale
 *   给出各自最新作业；没有该字段时回退 state.layer_job（旧后端，单作业视图）。
 * - **不默认烧整书费用**：打开阅读器只读 layers，绝不自动触发生成；本地预览也由宿主显式
 *   调 startLocalPreview()（迁移期可保留原 autoLocal 行为，但必须用它不能标 AI 完成）。
 */

export type FoldContent = "local" | "model" | "mixed" | "";
/** 折叠状态机：empty=从未生成；local=只有本地摘句；ai=模型层；partial=有失败节/混合；generating=作业中。 */
export type FoldPhase = "empty" | "local" | "ai" | "partial" | "generating";

export type FoldJobView = {
  job_id: string;
  status: string;
  progress: number;
  message: string;
  error: string;
};

export type FailedSection = { id: string; error: string };

export type ReaderFoldController = {
  /** 折叠状态机（见 FoldPhase）。UI 常驻入口据此渲染，不再用 generated/stale 推断。 */
  phase: FoldPhase;
  content: FoldContent;
  generated: boolean;
  stale: boolean;
  aiGenerated: boolean;
  partial: boolean;
  aiUnits: number;
  localUnits: number;
  failedSections: FailedSection[];
  /** 可升级到模型：有本地/混合内容，或已过期，或存在失败节，且模型绑定可用。 */
  canUpgradeToModel: boolean;
  /** 模型层已就绪（content==="model" 且无失败节）。 */
  aiReady: boolean;

  /** 折叠角色的有效绑定与来源（state.roles.fold）。`effective` 才是实际生效模型；
   *  `binding` 是保存值（unset 继承时可能为空）；`model_source` 说明 effective 的来源。 */
  binding: { service_id: string; model: string };
  bindingSource: string;
  modelSource: string;
  roleOrigin: string;
  bindingLabel: string;
  bindingProblem: string;
  modelReady: boolean;

  /** 按 locale 的折叠作业（本 locale 的在 jobs[locale]，另一栏的在 jobs 全表）。 */
  jobs: Record<string, FoldJobView>;
  job: FoldJobView | null;
  generating: boolean;
  anyGenerating: boolean;
  /** 0..1，无作业时 0。 */
  progress: number;

  lastError: string;
  clearError: () => void;

  /** 用模型折叠（scope：全文或指定章节；force=强制重折，忽略缓存）。 */
  startModelFold: (opts?: { sectionIds?: string[]; force?: boolean }) => Promise<void>;
  /** 本地摘句预览（不调模型；不覆盖已有 AI 层——后端契约）。 */
  startLocalPreview: () => Promise<void>;
  /** 停止本 locale 的折叠作业（章节边界生效，已完成章节保留）。 */
  stop: () => Promise<void>;
  /** 失败节补齐：只重折 failedSections 涉及的章节。 */
  retryFailed: () => Promise<void>;
  /** 刷新 layers/state（宿主 loadAll）。 */
  refresh: () => Promise<void>;
};

type LayersJobsShape = {
  jobs?: Record<string, FoldJobView>;
  locales?: Record<string, { job?: FoldJobView | null; failed?: FailedSection[] }>;
};

type FoldLayersShape = ReaderLayers & {
  content?: FoldContent;
  ai_generated?: boolean;
  partial?: boolean;
  failed?: FailedSection[];
  provenance?: {
    content?: FoldContent;
    ai_generated?: boolean;
    ai_units?: number;
    local_units?: number;
    partial?: boolean;
  };
};

export function useReaderFold(args: {
  projectId: string;
  /** 生效 locale（原文层传 "source"，译文层传 targetLocale）。 */
  locale: string;
  /** ReaderWorkspace 已有的 state（reader/state 响应）。 */
  state: ReaderState | null;
  /** ReaderWorkspace 已有的本 locale layers（reader/layers 响应）。 */
  layers: FoldLayersShape | null;
  /** 宿主刷新回调（loadAll）；作业轮询由本 hook 接管。 */
  refresh: () => Promise<void>;
}): ReaderFoldController {
  const { projectId, locale, state, layers, refresh } = args;
  const [lastError, setLastError] = useState("");
  const [tick, setTick] = useState(0);
  const refreshing = useRef(false);

  const clearError = useCallback(() => setLastError(""), []);

  // —— 模型绑定：只读 state.roles.fold ——
  const role: ReaderRoleState | undefined = state?.roles?.fold;
  // 生效模型以 role.effective 为准（binding 是保存值，unset 继承时可能为空）。
  // origin/source/model_source 原样透出给界面；cleared/invalid 阻止发起（后端同样会 400）。
  const binding = useMemo(
    () => ({
      service_id: role?.effective?.service_id ?? role?.binding?.service_id ?? "",
      model: role?.effective?.model ?? role?.binding?.model ?? "",
    }),
    [role],
  );
  const bindingSource = role?.source ?? "";
  const modelSource = role?.model_source ?? "";
  const roleOrigin = role?.origin ?? "";
  const bindingProblem = role?.problem ?? "";
  const bindingLabel = useMemo(
    () => (binding.service_id ? (binding.model ? `${binding.service_id} · ${binding.model}` : binding.service_id) : ""),
    [binding],
  );
  const modelReady =
    Boolean(binding.service_id) &&
    roleOrigin !== "cleared" &&
    roleOrigin !== "invalid" &&
    bindingSource !== "cleared" &&
    bindingSource !== "invalid";

  // —— 作业：按 locale 跟踪（layers.jobs 新契约，回退 layer_job） ——
  const jobs: Record<string, FoldJobView> = useMemo(() => {
    const layersState = (state?.layers ?? {}) as unknown as LayersJobsShape;
    if (layersState.jobs && Object.keys(layersState.jobs).length > 0) return layersState.jobs;
    const legacy = state?.layer_job;
    if (legacy && legacy.kind === "reader_layers") {
      return {
        [locale]: {
          job_id: String(legacy.job_id ?? ""),
          status: String(legacy.status ?? ""),
          progress: Number(legacy.progress ?? 0),
          message: String(legacy.message ?? ""),
          error: String(legacy.error ?? ""),
        },
      };
    }
    return {};
  }, [state, locale]);

  const job = jobs[locale] ?? null;
  const generating = job?.status === "running" || job?.status === "queued";
  const anyGenerating = Object.values(jobs).some(item => item.status === "running" || item.status === "queued");

  // —— 作业轮询：running 期间每 2s 刷新一次（接管 ReaderWorkspace 原轮询 effect） ——
  useEffect(() => {
    if (!anyGenerating) return;
    const timer = window.setInterval(() => { setTick(value => value + 1); }, 2000);
    return () => window.clearInterval(timer);
  }, [anyGenerating]);

  useEffect(() => {
    if (tick === 0 || refreshing.current) return;
    refreshing.current = true;
    void refresh().finally(() => { refreshing.current = false; });
  }, [tick, refresh]);

  // —— 内容状态（全部来自后端，不在前端推断） ——
  const content: FoldContent = (layers?.content ?? layers?.provenance?.content ?? "") as FoldContent;
  const generated = Boolean(layers?.generated);
  const stale = Boolean(layers?.stale);
  const aiGenerated = Boolean(layers?.ai_generated ?? layers?.provenance?.ai_generated);
  const partial = Boolean(layers?.partial ?? layers?.provenance?.partial);
  // Empty ids are document-level notices (for example an intentional stop),
  // not retryable chapter failures.
  const failedSections: FailedSection[] = (layers?.failed ?? []).filter(item => Boolean(item.id));
  const aiUnits = Number(layers?.provenance?.ai_units ?? 0);
  const localUnits = Number(layers?.provenance?.local_units ?? 0);

  const phase: FoldPhase = generating
    ? "generating"
    : !generated
      ? "empty"
      : content === "model"
        ? "ai"
        : content === "mixed" || partial || failedSections.length > 0
          ? "partial"
          : "local";

  const canUpgradeToModel = modelReady && (phase === "local" || phase === "partial" || stale);
  const aiReady = phase === "ai" && !stale;

  const startModelFold = useCallback(async (opts?: { sectionIds?: string[]; force?: boolean }) => {
    setLastError("");
    if (!modelReady) {
      setLastError(bindingProblem || "尚未绑定阅读折叠模型：请到「设置 → 阅读」选择模型服务。");
      return;
    }
    try {
      // 关键：不传 service_id/model —— 后端按 reader_fold 角色解析（显式传值会绕过角色绑定，
      // 这正是此前折叠误用对话模型的根因）。
      await readerApi.generateLayers(projectId, {
        locale,
        mode: "auto",
        force: Boolean(opts?.force),
        section_ids: opts?.sectionIds,
      });
      await refresh();
    } catch (error) {
      setLastError(error instanceof Error ? error.message : String(error));
    }
  }, [projectId, locale, modelReady, bindingProblem, refresh]);

  const startLocalPreview = useCallback(async () => {
    setLastError("");
    try {
      await readerApi.generateLayers(projectId, { locale, mode: "local" });
      await refresh();
    } catch (error) {
      setLastError(error instanceof Error ? error.message : String(error));
    }
  }, [projectId, locale, refresh]);

  const stop = useCallback(async () => {
    setLastError("");
    try {
      await api(`/projects/${projectId}/reader/layers/stop?locale=${encodeURIComponent(locale)}`, { method: "POST" });
      await refresh();
    } catch (error) {
      // 409 = 当前没有在跑的作业：视为已停止，不报错。
      const message = error instanceof Error ? error.message : String(error);
      if (!message.includes("没有正在运行")) setLastError(message);
    }
  }, [projectId, locale, refresh]);

  const retryFailed = useCallback(async () => {
    const ids = failedSections.map(item => item.id).filter(Boolean);
    // 预算边界：没有已知失败节时【绝不】回落整书重折（静默无操作即可，避免误触发全量付费）
    if (ids.length === 0) { setLastError(""); return; }
    await startModelFold({ sectionIds: ids });
  }, [failedSections, startModelFold]);

  return {
    phase, content, generated, stale, aiGenerated, partial,
    aiUnits, localUnits, failedSections,
    canUpgradeToModel, aiReady,
    binding, bindingSource, modelSource, roleOrigin, bindingLabel, bindingProblem, modelReady,
    jobs, job, generating, anyGenerating, progress: Number(job?.progress ?? 0),
    lastError, clearError,
    startModelFold, startLocalPreview, stop, retryFailed, refresh,
  };
}
