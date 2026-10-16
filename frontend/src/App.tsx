import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, initializeApi } from "./api";
import HomeTopbar, { type WorkView } from "./HomeTopbar";
import ImportWorkspace from "./ImportWorkspace";
import ProjectLibrary from "./ProjectLibrary";
import TaskHistory from "./TaskHistory";
import ReviewWorkspace from "./review/ReviewWorkspace";
import { flattenToc } from "./review/projection";
import { useModelServices } from "./modelServices";
import { InstantPanel, ReadingWorkspace, Settings, TaskPicker, type SettingsSection } from "./workbench";
import type { ScopeConfirm } from "./workbench/ReadingPanels";
import type { ReasoningMode } from "./workbench/ThinkingLevelField";
import { isWorkView, readLastProject, useHashRoute, writeLastProject } from "./viewState";
import type { DocumentGraph, GlossaryVersion, Job, Project, StructurePatch, StructureSuggestion, TranslationEstimate, TranslationLayer, TranslationStats, UsageRecord } from "./types";

const JOB_KIND: Record<string, string> = { translate: "翻译", refine: "润色", structure: "结构校对", proofread: "结构校对" };
const JOB_STATUS: Record<string, string> = { queued: "排队中", running: "进行中", paused: "已暂停", budget_paused: "预算暂停", completed: "已完成", failed: "失败", partial_failed: "部分失败", cancelled: "已取消" };
function savedInstructions(): string { try { return window.localStorage.getItem("literature.translation.instructions") || "" } catch { return "" } }

export default function App() {
  const [startup, setStartup] = useState("loading");
  const [route, navigate] = useHashRoute();
  const models = useModelServices();
  const [projects, setProjects] = useState<Project[]>([]);
  const [lastProject, setLastProject] = useState(readLastProject);
  const [document, setDocument] = useState<DocumentGraph | null>(null);
  const [translation, setTranslation] = useState<TranslationLayer | null>(null);
  const [selected, setSelected] = useState("");
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [suggestions, setSuggestions] = useState<StructureSuggestion[]>([]);
  const [patches, setPatches] = useState<StructurePatch[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [usage, setUsage] = useState({ input_tokens: 0, output_tokens: 0, cached_tokens: 0, reasoning_tokens: 0, estimated_cost: 0, unknown_cost_records: 0, pending_cost_records: 0 });
  const [instructions, setInstructions] = useState(savedInstructions);
  const [settings, setSettings] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general");
  const [message, setMessage] = useState("");
  const [instantResult, setInstantResult] = useState<{ source: string; text: string; blockId: string } | null>(null);
  const [reviewCandidate, setReviewCandidate] = useState<{ job_id?: string; project_id: string; locale: string; block_id: string; candidate_id: string; source_hash: string; base_revision: string; changed: boolean; complete: boolean; revised: Record<string, unknown>; original: Record<string, unknown>; source?: string; current?: string } | null>(null);
  const [glossary, setGlossary] = useState<Record<string, string>>({});
  const [glossaryVersions, setGlossaryVersions] = useState<GlossaryVersion[]>([]);
  const [locale, setLocale] = useState(() => { try { return window.localStorage.getItem("literature.translation.locale") || "zh-CN"; } catch { return "zh-CN"; } });
  const [estimate, setEstimate] = useState<TranslationEstimate | null>(null);
  const [usageRecords, setUsageRecords] = useState<UsageRecord[]>([]);
  const [projectLoadError, setProjectLoadError] = useState("");
  // 同步可见性：轮询出错不当成“没事发生”，也不能拿旧数据显示成当前状态（F08）。
  const [lastSyncAt, setLastSyncAt] = useState<number | null>(null);
  const [syncError, setSyncError] = useState("");
  const seenTranslationJobRef = useRef("");
  const estimateSeqRef = useRef(0);
  const reviewSeqRef = useRef(0);

  // 项目上下文独立于当前视图：在首页/文件资源浏览时仍保留最近打开的任务。
  const activeProjectId = route.projectId || lastProject;
  const activeProject = projects.find(project => project.project_id === activeProjectId);
  const view = route.view;

  const refreshProjects = useCallback(async () => setProjects(await api<Project[]>("/projects")), []);
  // 防止项目切换后旧的异步响应回来污染当前项目状态（F08）。
  const activeProjectRef = useRef(activeProjectId);
  const loadSeqRef = useRef(0);
  const localeSeqRef = useRef(0);
  const translationRevisionRef = useRef<{ key: string; revision: string }>({ key: "", revision: "" });
  // 目标语言只影响译文层：用 ref 读取，避免「切换语言 → refreshProject 重建 → 项目加载 effect 重跑 → 整个工作区被清空」。
  const localeRef = useRef(locale);
  const previousLocaleRef = useRef(locale);
  useEffect(() => { activeProjectRef.current = activeProjectId; }, [activeProjectId]);
  useEffect(() => { localeRef.current = locale; }, [locale]);
  const loadTranslationLayer = useCallback(async (id: string, target: string) => {
    const [layer, versions] = await Promise.all([
      api<TranslationLayer>(`/projects/${id}/translations/${target}`),
      api<GlossaryVersion[]>(`/projects/${id}/translations/${target}/glossary/versions`),
    ]);
    const frozen = Object.keys(layer.glossary_snapshot || {}).length;
    const glossaryValue = frozen ? layer.glossary_snapshot : await api<Record<string, string>>(`/projects/${id}/glossary/draft`);
    return { layer, versions, glossaryValue };
  }, []);
  const refreshProject = useCallback(async (id: string) => {
    // 请求代次：project 切换后，旧的迟到响应不能覆盖新状态。
    const seq = ++loadSeqRef.current;
    const target = localeRef.current;
    const [graph, translationLayer, patchesValue, usageRecordsValue, state] = await Promise.all([
      api<DocumentGraph>(`/projects/${id}/document`),
      loadTranslationLayer(id, target),
      api<StructurePatch[]>(`/projects/${id}/structure/patches`),
      api<UsageRecord[]>(`/projects/${id}/usage/records`),
      api<any>(`/projects/${id}`),
    ]);
    const { layer, versions, glossaryValue } = translationLayer;
    if (activeProjectRef.current !== id || seq !== loadSeqRef.current) return;
    translationRevisionRef.current = {
      key: `${id}:${target}`,
      revision: (layer as TranslationLayer & { revision?: string }).revision || layer.updated_at || "",
    };
    setTranslation(layer);
    setGlossary(glossaryValue);
    setGlossaryVersions(versions);
    setPatches(patchesValue);
    setUsageRecords(usageRecordsValue);
    setDocument({ ...graph, budget_usd: state.budget_usd || 0 }); setJobs(state.jobs); setUsage(state.usage);
  }, [loadTranslationLayer]);
  // 切换目标语言：只换译文层与术语表，不重置文档/任务/建议（否则整块工作区会闪空）。
  const refreshTranslationLocale = useCallback(async (id: string) => {
    const seq = ++localeSeqRef.current;
    const target = localeRef.current;
    const { layer, versions, glossaryValue } = await loadTranslationLayer(id, target);
    if (activeProjectRef.current !== id || localeRef.current !== target || seq !== localeSeqRef.current) return;
    translationRevisionRef.current = {
      key: `${id}:${target}`,
      revision: (layer as TranslationLayer & { revision?: string }).revision || layer.updated_at || "",
    };
    setTranslation(layer);
    setGlossary(glossaryValue);
    setGlossaryVersions(versions);
  }, [loadTranslationLayer]);
  const resetProjectState = useCallback(() => {
    setDocument(null); setTranslation(null); setSelected(""); setChecked(new Set());
    setSuggestions([]); setPatches([]); setJobs([]); setGlossary({}); setUsageRecords([]);
    setUsage({ input_tokens: 0, output_tokens: 0, cached_tokens: 0, reasoning_tokens: 0, estimated_cost: 0, unknown_cost_records: 0, pending_cost_records: 0 });
  }, []);
  // 统一错误反馈：所有直接操作失败都要落到顶部提示，而不是变成未处理的 Promise。
  const reportError = useCallback((error: unknown, fallback: string) => {
    setMessage(error instanceof Error && error.message ? error.message : fallback);
  }, []);

  useEffect(() => { initializeApi().then(refreshProjects).then(() => setStartup("ready")).catch(error => setStartup(String(error))); }, [refreshProjects]);
  useEffect(() => { window.localStorage.setItem("literature.translation.instructions", instructions); }, [instructions]);
  useEffect(() => {
    try { window.localStorage.setItem("literature.translation.locale", locale); } catch { /* ignore */ }
    setEstimate(null);
    // 仅当目标语言真的变化时刷新译文层；项目切换由 refreshProject 负责，避免重复请求。
    const changed = previousLocaleRef.current !== locale;
    previousLocaleRef.current = locale;
    if (!changed || !activeProjectRef.current) return;
    refreshTranslationLocale(activeProjectRef.current).catch(error => setMessage(error instanceof Error ? error.message : String(error)));
  }, [locale, refreshTranslationLocale]);
  useEffect(() => { if (route.projectId) { setLastProject(route.projectId); writeLastProject(route.projectId); } }, [route.projectId]);
  useEffect(() => {
    if (!lastProject || !projects.length) return;
    if (!projects.some(project => project.project_id === lastProject)) { setLastProject(""); writeLastProject(""); }
  }, [projects, lastProject]);
  useEffect(() => {
    if (!activeProjectId) { resetProjectState(); setProjectLoadError(""); return; }
    let cancelled = false;
    resetProjectState();
    setProjectLoadError("");
    refreshProject(activeProjectId).catch(error => { if (!cancelled) setProjectLoadError(String(error instanceof Error ? error.message : error)); });
    return () => { cancelled = true; };
  }, [activeProjectId, refreshProject, resetProjectState]);
  useEffect(() => {
    if (!activeProjectId) return;
    let cancelled = false;
    let timer = 0;
    const schedule = (delay: number) => { timer = window.setTimeout(() => void tick(), delay); };
    const tick = async () => {
      // 隐藏窗口时不做无意义轮询；回到前台立即补一次。（`document` 在此是项目状态，需用 window.document）
      if (window.document.hidden) { schedule(10000); return; }
      // 收敛轮询：在跑 1.5s；任务结束后退到 8s；无任何任务退到 15s。
      let delay = 15000;
      try {
        const value = await api<any>(`/projects/${activeProjectId}/status`);
        if (cancelled) return;
        setJobs(value.jobs); setUsage(value.usage);
        setSyncError(""); setLastSyncAt(Date.now());
        const translateJobs = value.jobs.filter((job: Job) => job.kind === "translate" || job.kind === "refine");
        const running = translateJobs.some((job: Job) => ["queued", "running"].includes(job.status));
        const busy = value.jobs.some((job: Job) => ["queued", "running", "pause_requested"].includes(job.status));
        const signature = translateJobs[0] ? `${translateJobs[0].job_id}:${translateJobs[0].updated_at}` : "";
        // 只有“真正在执行/排队”或“任务结果签名变化”时才拉整层译文；暂停/闲置不再反复拉全量。
        if (running || signature !== seenTranslationJobRef.current) {
          seenTranslationJobRef.current = signature;
          const revKey = `${activeProjectId}:${locale}`;
          const since = translationRevisionRef.current.key === revKey ? translationRevisionRef.current.revision : "";
          const layer = await api<TranslationLayer & { unchanged?: boolean; revision?: string }>(`/projects/${activeProjectId}/translations/${locale}?since=${encodeURIComponent(since)}`);
          if (!cancelled && !layer.unchanged) {
            translationRevisionRef.current = { key: revKey, revision: layer.revision || (layer as { updated_at?: string }).updated_at || "" };
            setTranslation(layer);
          }
        }
        delay = busy ? 1500 : value.jobs.length ? 8000 : 15000;
      } catch (error) {
        if (!cancelled) setSyncError(error instanceof Error ? error.message : String(error));
      } finally {
        if (!cancelled) schedule(delay);
      }
    };
    const onVisibility = () => { if (!window.document.hidden) { window.clearTimeout(timer); void tick(); } };
    window.document.addEventListener("visibilitychange", onVisibility);
    void tick();
    return () => { cancelled = true; window.clearTimeout(timer); window.document.removeEventListener("visibilitychange", onVisibility); };
  }, [activeProjectId, locale]);
  // 切项目/语言时丢弃候选，避免按旧项目/语言采纳。
  useEffect(() => { reviewSeqRef.current += 1; setReviewCandidate(null); }, [activeProjectId, locale]);
  useEffect(() => {
    if (view !== "resources") return;
    // 进入文件资源页立即刷新一次，并按 3 秒轮询，让校对/翻译状态及时回显；窗口隐藏时暂停。
    const run = () => { if (!window.document.hidden) refreshProjects().catch(() => {}); };
    run();
    const timer = window.setInterval(run, 3000);
    window.document.addEventListener("visibilitychange", run);
    return () => { window.clearInterval(timer); window.document.removeEventListener("visibilitychange", run); };
  }, [view, refreshProjects]);
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(""), /失败|错误|无法|不可用|Error|error/.test(message) ? 15000 : 6000);
    return () => window.clearTimeout(timer);
  }, [message]);

  const selectedScope = useMemo(() => {
    if (!document || checked.size === 0) return [];
    const headings = flattenToc(document.toc);
    const ids = new Set<string>();
    for (const titleId of checked) {
      const node = headings.find(value => value.block_id === titleId); if (!node) continue;
      const start = document.blocks.findIndex(block => block.block_id === titleId);
      for (let index = start; index < document.blocks.length; index++) {
        const block = document.blocks[index];
        if (index > start && block.type === "title" && block.level <= node.level) break;
        ids.add(block.block_id);
      }
    }
    return [...ids];
  }, [document, checked]);

  const openHome = () => navigate("home", "");
  const openResources = () => navigate("resources", "");
  // 工作区可以不带项目进入：没有项目时显示该模块的空白页与任务选择列表。
  const openWork = (target: WorkView) => navigate(target, activeProjectId || "");
  const openProjectFor = (projectId: string) => {
    setLastProject(projectId); writeLastProject(projectId);
    navigate(isWorkView(view) ? view : "review", projectId);
  };
  const openProjectView = (target: WorkView, projectId: string) => {
    setLastProject(projectId); writeLastProject(projectId);
    navigate(target, projectId);
  };
  // 文件资源卡片可以就地启动翻译：范围与目标语言来自弹窗，不依赖当前已打开的任务。
  const translateProject = async (projectId: string, options: { scope: string; locale: string }) => {
    if (!models.roles.translation.service_id) { openSettings("models"); setMessage("请先配置翻译模型"); return false; }
    const binding = models.roles.translation;
    try {
      await api(`/projects/${projectId}/translation/jobs`, { method: "POST", body: JSON.stringify({
        service_id: binding.service_id, model: binding.model, scope: options.scope, block_ids: [], locale: options.locale,
        refine: false, force: false, polish: false, parallel_workers: 8,
        reasoning_mode: "inherit", reasoning_budget: 0,
      }) });
      if (options.locale !== locale) setLocale(options.locale);
      setMessage("翻译任务已进入本地队列");
      await refreshProjects();
      return true;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
      return false;
    }
  };
  const closeProject = () => {
    setLastProject(""); writeLastProject("");
    resetProjectState(); setProjectLoadError("");
    navigate(view, "");
    setMessage("已关闭当前任务");
  };

  const selectBlock = (id: string) => setSelected(id);
  const toggle = (id: string) => setChecked(previous => { const next = new Set(previous); next.has(id) ? next.delete(id) : next.add(id); return next; });
  const translate = async (taskScope: string, blockIds: string[] = [], options: { refine?: boolean; force?: boolean; polish?: boolean; workers?: number; useGlossary?: boolean; instructions?: string; locale?: string; serviceId?: string; model?: string; reasoningMode?: ReasoningMode; reasoningBudget?: number } = {}) => {
    const binding = models.roles.translation;
    // 弹窗里可以直接选服务商/模型：守卫按“实际将使用的 service_id”判断，而不是只看角色绑定。
    const effectiveServiceId = options.serviceId || binding.service_id;
    if (!effectiveServiceId) { openSettings("models"); setMessage("请先配置翻译模型"); return; }
    if (taskScope === "blocks" && !blockIds.length) { setMessage("请先在目录中勾选要翻译的章节或段落"); return; }
    const targetLocale = options.locale || locale;
    if (options.locale && options.locale !== locale) setLocale(options.locale);
    const payload = {
      service_id: effectiveServiceId,
      model: options.model || binding.model,
      scope: taskScope, block_ids: blockIds, locale: targetLocale,
      glossary: options.useGlossary === false ? {} : glossary,
      refine: options.refine || false, force: options.force || false,
      polish: options.polish || false, parallel_workers: options.workers ?? 8,
      instructions: options.instructions ?? instructions,
      // 契约：budget>0 仅在低/中/高档生效；inherit 时必须归零，避免陈旧预算覆盖服务配置。
      reasoning_mode: options.reasoningMode ?? "inherit",
      reasoning_budget: options.reasoningMode && options.reasoningMode !== "inherit" ? options.reasoningBudget ?? 0 : 0,
    };
    try {
      await api(`/projects/${activeProjectId}/translation/jobs`, { method: "POST", body: JSON.stringify(payload) });
      setMessage(options.refine ? "润色任务已进入本地队列" : options.polish ? "翻译 + 润色任务已进入本地队列" : "翻译任务已进入本地队列");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const translateBlock = (blockId: string, options: { refine?: boolean; force?: boolean } = {}) => translate("blocks", [blockId], options);
  const estimateScope = async (options: ScopeConfirm) => {
    const blockIds = options.scope === "blocks" ? selectedScope : [];
    // 请求代次：选项快速变化时，旧请求的迟到响应不能覆盖新选项的估算。
    const seq = ++estimateSeqRef.current;
    try {
      const value = await api<TranslationEstimate>(`/projects/${activeProjectId}/translation/estimate`, {
        method: "POST",
        body: JSON.stringify({
          service_id: options.serviceId || models.roles.translation.service_id, model: options.model || models.roles.translation.model, scope: options.scope, block_ids: blockIds,
          locale: options.locale || locale, refine: options.refine, force: options.force,
          polish: options.polish, glossary: options.useGlossary === false ? {} : glossary,
          instructions: options.instructions ?? instructions,
        }),
      });
      if (seq === estimateSeqRef.current) setEstimate(value);
    } catch (error) {
      if (seq === estimateSeqRef.current) reportError(error, "预估失败");
    }
  };
  const invalidateEstimate = useCallback(() => {
    // 稳定引用：选项真正变化时才失效，不能让每次 render 都清估算（否则刚显示的估算立即消失）。
    estimateSeqRef.current += 1;
    setEstimate(null);
  }, []);
  const unlockBlock = async (blockId: string) => {
    if (!activeProjectId) return;
    try {
      await api(`/projects/${activeProjectId}/translations/${blockId}/unlock?locale=${encodeURIComponent(locale)}`, { method: "POST" });
      await refreshProject(activeProjectId);
      setMessage("已解锁该段译文");
    } catch (error) {
      reportError(error, "解锁失败");
    }
  };
  const pauseJob = async (job: Job) => {
    if (!activeProjectId) return;
    try {
      await api(`/projects/${activeProjectId}/translation/jobs/${job.job_id}/pause`, { method: "POST" });
      setMessage("已请求暂停；当前分块完成后停止");
    } catch (error) {
      reportError(error, "暂停失败");
    }
  };
  const resumeJob = async (job: Job) => {
    if (!activeProjectId) return;
    try {
      await api(`/projects/${activeProjectId}/translation/jobs/${job.job_id}/resume`, { method: "POST" });
      setMessage(job.status === "failed" || job.status === "partial_failed" ? "正在重试失败分块" : "翻译任务已恢复");
    } catch (error) {
      reportError(error, "恢复任务失败");
    }
  };
  const freezeGlossary = async (entries: Record<string, string>) => {
    if (!activeProjectId) return;
    try {
      // 冻结以服务端成功为准：写入成功后再回读快照与版本，不做乐观写入。
      await api(`/projects/${activeProjectId}/translations/${locale}/glossary`, { method: "PUT", body: JSON.stringify({ entries }) });
      await refreshTranslationLocale(activeProjectId);
      setMessage(`术语表已冻结：${Object.keys(entries).length} 项`);
    } catch (error) {
      reportError(error, "冻结术语表失败");
    }
  };
  const rollbackGlossary = async (version: number) => {
    if (!activeProjectId) return;
    try {
      const value = await api<{ entries: Record<string, string>; version: number }>(`/projects/${activeProjectId}/translations/${locale}/glossary/rollback`, { method: "POST", body: JSON.stringify({ version }) });
      await refreshTranslationLocale(activeProjectId);
      setMessage(`术语表已回滚为 v${version} 的内容（新版本 v${value.version}）`);
    } catch (error) {
      reportError(error, "回滚术语表失败");
    }
  };
  const reviewBlock = async (blockId: string) => {
    if (!models.roles.translation.service_id) { openSettings("models"); setMessage("请先配置翻译模型"); return; }
    const projectId = activeProjectId;
    const requestedLocale = locale;
    const seq = ++reviewSeqRef.current;
    try {
      const value = await api<any>(`/projects/${projectId}/translations/${blockId}/review`, {
        method: "POST", body: JSON.stringify({ service_id: models.roles.translation.service_id, model: models.roles.translation.model, locale: requestedLocale, instructions }),
      });
      // 迟到响应：项目/语言/代次不一致就不回写。
      if (seq !== reviewSeqRef.current || activeProjectRef.current !== projectId) return;
      setReviewCandidate(value);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const adoptReview = async () => {
    if (!reviewCandidate) return;
    const candidate = reviewCandidate;
    try {
      await api(`/projects/${candidate.project_id}/translations/${candidate.block_id}/review/adopt`, {
        method: "POST",
        body: JSON.stringify({
          locale: candidate.locale, candidate_id: candidate.candidate_id,
          source_hash: candidate.source_hash, base_revision: candidate.base_revision,
          revised: candidate.revised,
        }),
      });
      setReviewCandidate(null);
      await refreshProject(candidate.project_id);
      setMessage("已采纳并保存译审候选");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const rejectReview = () => { setReviewCandidate(null); setMessage("已拒绝译审候选，未改动译文"); };

  const instant = async (text: string, blockId: string) => {
    const selection = text.trim();
    if (!selection) return;
    if (!models.roles.translation.service_id) { openSettings("models"); setMessage("请先配置翻译模型"); return; }
    try {
      const value = await api<any>(`/projects/${activeProjectId}/translate/instant`, { method: "POST", body: JSON.stringify({ service_id: models.roles.translation.service_id, model: models.roles.translation.model, text: selection, locale, block_id: blockId || null }) });
      setInstantResult({ source: selection, text: value.text, blockId });
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const usageBadge = document ? <span className="usage">{(usage.input_tokens + usage.output_tokens).toLocaleString()} Token · ${usage.estimated_cost.toFixed(4)}{document.budget_usd ? ` / $${document.budget_usd.toFixed(2)}` : ""}{(usage.unknown_cost_records + usage.pending_cost_records) ? ` · ${usage.unknown_cost_records + usage.pending_cost_records} 个请求费用未知/待对账` : ""}</span> : undefined;
  const stats: TranslationStats = translation?.stats || { total: 0, done: 0, pending: 0, percent: 0, untranslated: 0, translated: 0, refined: 0, edited: 0, failed: 0, locked: 0, stale: 0, stale_locked: 0, stale_source: 0, stale_source_unlocked: 0, stale_structure: 0, stale_block_ids: [] };
  const openSettings = (section: SettingsSection = "general") => { setSettingsSection(section); setSettings(true); };
  const projectPlaceholder = (hint: string) => activeProjectId
    ? <WorkspaceState title={projectLoadError ? "无法打开任务" : "正在载入任务…"} body={projectLoadError || "正在读取规范文档。"} action={projectLoadError ? openResources : undefined} label={projectLoadError ? "查看文件资源" : undefined}/>
    : <TaskPicker projects={projects} onOpen={openProjectFor} onUpload={openHome} hint={hint}/>;

  if (startup !== "ready") return <main className="client-startup"><h1>Literature</h1><p role="status">{startup === "loading" ? "正在启动本地工作台…" : startup}</p>{startup !== "loading" && <button onClick={() => window.location.reload()}>重试连接</button>}</main>;
  return <div className="app-shell">
    <HomeTopbar
      active={view}
      openHome={openHome}
      openResources={openResources}
      openWork={openWork}
      closeProject={closeProject}
      openSettings={() => openSettings(settingsSection)}
      project={activeProject ? { title: document?.title || activeProject.title, subtitle: document ? `${document.source_language} → ${document.target_language}` : activeProject.state, usage: usageBadge } : undefined}
    />

    {view === "home" && <div className="page-scroll"><ImportWorkspace refreshProjects={refreshProjects} openResources={openResources} openHistory={() => navigate("history", "")}/></div>}
    {view === "resources" && <div className="page-scroll"><ProjectLibrary
      projects={projects}
      currentProjectId={lastProject}
      refreshProjects={refreshProjects}
      onOpenView={openProjectView}
      onTranslateProject={translateProject}
      onGoHome={openHome}
      onMessage={setMessage}
    /></div>}
    {view === "history" && <div className="page-scroll"><TaskHistory
      onOpenResources={projectId => { if (projectId) { setLastProject(projectId); writeLastProject(projectId); } navigate("resources", ""); }}
      onMessage={setMessage}
    /></div>}

    {view === "review" && (document
      ? <ReviewWorkspace
          key={activeProjectId}
          projectId={activeProjectId}
          document={document}
          sourceType={activeProject?.source_type || "pdf"}
          sourceName={activeProject?.source_name || document.title}
          patches={patches}
          suggestions={suggestions}
          jobs={jobs}
          onSuggestions={setSuggestions}
          selected={selected}
          onSelect={selectBlock}
          refresh={() => refreshProject(activeProjectId)}
          setMessage={setMessage}
          modelService={models.serviceFor("structure")}
          boundModel={models.roles.structure?.model || ""}
          providers={models.services}
          onConfigureModel={() => openSettings("models")}
        />
      : projectPlaceholder("选择一个已完成处理的文档，进入结构校对；或去首页上传新文件。"))}
    {view === "read" && (document
      ? <ReadingWorkspace
          projectId={activeProjectId}
          document={document}
          translation={translation}
          stats={stats}
          usage={usage}
          selected={selected}
          checked={checked}
          scopeBlockIds={selectedScope}
          jobs={jobs}
          glossary={glossary}
          glossaryVersions={glossaryVersions}
          locale={locale}
          estimate={estimate}
          onToggle={toggle}
          onSelectBlock={selectBlock}
          onPause={pauseJob}
          onResume={resumeJob}
          onTranslate={translate}
          onTranslateBlock={translateBlock}
          onEstimate={estimateScope}
          onInvalidateEstimate={invalidateEstimate}
          onReviewBlock={reviewBlock}
          reviewCandidate={reviewCandidate}
          onAdoptReview={adoptReview}
          onRejectReview={rejectReview}
          onFreezeGlossary={freezeGlossary}
          onRollbackGlossary={rollbackGlossary}
          onUnlockBlock={unlockBlock}
          onRefresh={() => refreshProject(activeProjectId)}
          onInstant={instant}
          onOpenReview={() => navigate("review", activeProjectId)}
          providers={models.services}
          translationBinding={models.roles.translation}
          onConfigureModel={() => openSettings("models")}
        />
      : projectPlaceholder("选择一个已完成处理的文档，进入对照翻译；或去首页上传新文件。"))}
    {isWorkView(view) && <footer className="statusbar"><span>{syncError ? <b className="statusbar-fail">同步失败：{syncError}</b> : lastSyncAt ? `已同步 ${new Date(lastSyncAt).toLocaleTimeString()}` : "本地项目 · 已保存"}</span><span>{jobs[0] ? `${JOB_KIND[jobs[0].kind] || jobs[0].kind} · ${JOB_STATUS[jobs[0].status] || jobs[0].status} · ${Math.round(jobs[0].progress * 100)}%` : "无运行任务"}</span>{view === "review" && <span>{suggestions.filter(value => !value.automatic).length} 项待确认</span>}</footer>}

    {settings && <Settings
      projectId={activeProjectId || undefined}
      document={document}
      selected={selected}
      instructions={instructions}
      setInstructions={setInstructions}
      usageRecords={usageRecords}
      models={models}
      section={settingsSection}
      onSectionChange={setSettingsSection}
      close={() => setSettings(false)}
      onUpdated={setDocument}
      onMessage={setMessage}
    />}
    {instantResult && <InstantPanel projectId={activeProjectId} locale={locale} value={instantResult} close={() => setInstantResult(null)} onSaved={() => refreshProject(activeProjectId)}/>}
    {message && <button className="toast" onClick={() => setMessage("")}>{message}</button>}
  </div>;
}

function WorkspaceState({ title, body, action, label }: { title: string; body: string; action?: () => void; label?: string }) {
  return <div className="workspace-state"><h2>{title}</h2><p>{body}</p>{action && <button className="primary" onClick={action}>{label}</button>}</div>;
}
