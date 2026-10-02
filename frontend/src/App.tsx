import { useCallback, useEffect, useMemo, useState } from "react";
import { api, initializeApi } from "./api";
import HomeTopbar, { type WorkView } from "./HomeTopbar";
import ImportWorkspace from "./ImportWorkspace";
import ProjectLibrary from "./ProjectLibrary";
import ReviewWorkspace from "./review/ReviewWorkspace";
import { flattenToc } from "./review/projection";
import { useModelServices } from "./modelServices";
import { ExportPanel, GlossaryEditor, InstantPanel, ModelServicesDialog, ReadingWorkspace, Settings, TaskPicker } from "./workbench";
import { isWorkView, readLastProject, useHashRoute, writeLastProject } from "./viewState";
import type { DocumentGraph, Job, ModelRole, Project, StructurePatch, StructureSuggestion, TranslationLayer, UsageRecord } from "./types";

function savedInstructions(): string { try { return window.localStorage.getItem("literature.translation.instructions") || "" } catch { return "" } }

export default function App() {
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
  const [usage, setUsage] = useState({ input_tokens: 0, output_tokens: 0, estimated_cost: 0 });
  const [instructions, setInstructions] = useState(savedInstructions);
  const [settings, setSettings] = useState(false);
  const [modelRole, setModelRole] = useState<ModelRole | null>(null);
  const [message, setMessage] = useState("");
  const [instantResult, setInstantResult] = useState<{ source: string; text: string; blockId: string } | null>(null);
  const [glossary, setGlossary] = useState<Record<string, string>>({});
  const [glossaryOpen, setGlossaryOpen] = useState(false);
  const [usageRecords, setUsageRecords] = useState<UsageRecord[]>([]);
  const [projectLoadError, setProjectLoadError] = useState("");

  // 项目上下文独立于当前视图：在首页/文件资源浏览时仍保留最近打开的任务。
  const activeProjectId = route.projectId || lastProject;
  const activeProject = projects.find(project => project.project_id === activeProjectId);
  const view = route.view;

  const refreshProjects = useCallback(async () => setProjects(await api<Project[]>("/projects")), []);
  const refreshProject = useCallback(async (id: string) => {
    const graph = await api<DocumentGraph>(`/projects/${id}/document`);
    const layer = await api<TranslationLayer>(`/projects/${id}/translations/zh-CN`);
    setTranslation(layer); setGlossary(layer.glossary_snapshot);
    setPatches(await api<StructurePatch[]>(`/projects/${id}/structure/patches`));
    setUsageRecords(await api<UsageRecord[]>(`/projects/${id}/usage/records`));
    const state = await api<any>(`/projects/${id}`); setDocument({ ...graph, budget_usd: state.budget_usd || 0 }); setJobs(state.jobs); setUsage(state.usage);
  }, []);
  const resetProjectState = useCallback(() => {
    setDocument(null); setTranslation(null); setSelected(""); setChecked(new Set());
    setSuggestions([]); setPatches([]); setJobs([]); setGlossary({}); setUsageRecords([]);
    setUsage({ input_tokens: 0, output_tokens: 0, estimated_cost: 0 });
  }, []);

  useEffect(() => { initializeApi().then(refreshProjects).catch(error => setMessage(String(error))); }, [refreshProjects]);
  useEffect(() => { window.localStorage.setItem("literature.translation.instructions", instructions); }, [instructions]);
  useEffect(() => { if (route.projectId) { setLastProject(route.projectId); writeLastProject(route.projectId); } }, [route.projectId]);
  useEffect(() => {
    if (!lastProject || !projects.length) return;
    if (!projects.some(project => project.project_id === lastProject)) { setLastProject(""); writeLastProject(""); }
  }, [projects, lastProject]);
  useEffect(() => {
    if (!activeProjectId) { resetProjectState(); setProjectLoadError(""); return; }
    resetProjectState();
    setProjectLoadError("");
    refreshProject(activeProjectId).catch(error => setProjectLoadError(String(error instanceof Error ? error.message : error)));
  }, [activeProjectId, refreshProject, resetProjectState]);
  useEffect(() => {
    if (!activeProjectId) return;
    const timer = window.setInterval(() => api<any>(`/projects/${activeProjectId}`).then(async value => {
      setJobs(value.jobs); setUsage(value.usage);
      if (value.jobs.some((job: Job) => job.status === "completed")) setTranslation(await api<TranslationLayer>(`/projects/${activeProjectId}/translations/zh-CN`));
    }).catch(() => {}), 1500);
    return () => clearInterval(timer);
  }, [activeProjectId]);
  useEffect(() => {
    if (activeProjectId || view !== "resources") return;
    const timer = window.setInterval(() => refreshProjects().catch(() => {}), 3000);
    return () => window.clearInterval(timer);
  }, [activeProjectId, view, refreshProjects]);
  useEffect(() => {
    if (!message) return;
    const timer = window.setTimeout(() => setMessage(""), 6000);
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
  const openProject = (project: Project) => {
    if (!project.has_document) { setMessage("该任务仍在解析中，生成规范文档后即可进入结构校对。"); return; }
    setLastProject(project.project_id); writeLastProject(project.project_id);
    navigate("review", project.project_id);
  };
  const openProjectFor = (projectId: string) => {
    setLastProject(projectId); writeLastProject(projectId);
    navigate(isWorkView(view) ? view : "review", projectId);
  };
  const closeProject = () => {
    setLastProject(""); writeLastProject("");
    resetProjectState(); setProjectLoadError("");
    navigate(view, "");
    setMessage("已关闭当前任务");
  };

  const selectBlock = (id: string) => setSelected(id);
  const toggle = (id: string) => setChecked(previous => { const next = new Set(previous); next.has(id) ? next.delete(id) : next.add(id); return next; });
  const translate = async (refine = false, scope: string[] = selectedScope) => {
    if (!models.roles.translation) { setModelRole("translation"); setMessage("请先配置翻译模型"); return; }
    await api(`/projects/${activeProjectId}/translation/jobs`, { method: "POST", body: JSON.stringify({ service_id: models.roles.translation, block_ids: scope, locale: "zh-CN", glossary, refine, force: false, instructions }) });
    setMessage("翻译任务已进入本地队列");
  };
  const pauseJob = async (job: Job) => { await api(`/projects/${activeProjectId}/translation/jobs/${job.job_id}/pause`, { method: "POST" }); setMessage("已请求暂停；当前分块完成后停止"); };
  const resumeJob = async (job: Job) => { await api(`/projects/${activeProjectId}/translation/jobs/${job.job_id}/resume`, { method: "POST" }); setMessage(job.status === "failed" ? "正在重试失败分块" : "翻译任务已恢复"); };
  const openGlossary = async () => { if (!Object.keys(glossary).length) setGlossary(await api<Record<string, string>>(`/projects/${activeProjectId}/glossary/draft`)); setGlossaryOpen(true); };
  const freezeGlossary = async (entries = glossary) => { setGlossary(entries); await api(`/projects/${activeProjectId}/translations/zh-CN/glossary`, { method: "PUT", body: JSON.stringify({ entries }) }); setGlossaryOpen(false); setMessage(`术语表已冻结：${Object.keys(entries).length} 项`); };
  const instant = async (event: React.MouseEvent<HTMLElement>) => {
    const selection = window.getSelection()?.toString().trim(); if (!selection) return;
    if (!models.roles.translation) { setModelRole("translation"); setMessage("请先配置翻译模型"); return; }
    const blockId = (event.target as HTMLElement).closest<HTMLElement>("[data-block-id]")?.dataset.blockId || selected;
    const value = await api<any>(`/projects/${activeProjectId}/translate/instant`, { method: "POST", body: JSON.stringify({ service_id: models.roles.translation, text: selection, locale: "zh-CN", block_id: blockId || null }) });
    setInstantResult({ source: selection, text: value.text, blockId });
  };

  const usageBadge = document ? <span className="usage">{(usage.input_tokens + usage.output_tokens).toLocaleString()} Token · ${usage.estimated_cost.toFixed(4)}{document.budget_usd ? ` / $${document.budget_usd.toFixed(2)}` : ""}</span> : undefined;
  const openModelsForView = () => { setSettings(false); setModelRole(view === "read" ? "translation" : "structure"); };
  const projectPlaceholder = (hint: string) => activeProjectId
    ? <WorkspaceState title={projectLoadError ? "无法打开任务" : "正在载入任务…"} body={projectLoadError || "正在读取规范文档。"} action={projectLoadError ? openResources : undefined} label={projectLoadError ? "查看文件资源" : undefined}/>
    : <TaskPicker projects={projects} onOpen={openProjectFor} onUpload={openHome} hint={hint}/>;

  return <div className="app-shell">
    <HomeTopbar
      active={view}
      openHome={openHome}
      openResources={openResources}
      openWork={openWork}
      closeProject={closeProject}
      openSettings={() => setSettings(true)}
      project={activeProject ? { title: document?.title || activeProject.title, subtitle: document ? `${document.source_language} → ${document.target_language}` : activeProject.state, usage: usageBadge } : undefined}
    />

    {view === "home" && <div className="page-scroll"><ImportWorkspace refreshProjects={refreshProjects} openResources={openResources}/></div>}
    {view === "resources" && <div className="page-scroll"><ProjectLibrary projects={projects} refreshProjects={refreshProjects} openProject={openProject} currentProjectId={lastProject}/></div>}

    {view === "review" && (document
      ? <ReviewWorkspace
          projectId={activeProjectId}
          document={document}
          sourceType={activeProject?.source_type || "pdf"}
          sourceName={activeProject?.source_name || document.title}
          patches={patches}
          suggestions={suggestions}
          onSuggestions={setSuggestions}
          scope={checked}
          onToggleScope={toggle}
          selected={selected}
          onSelect={selectBlock}
          refresh={() => refreshProject(activeProjectId)}
          setMessage={setMessage}
          onOpenRead={() => navigate("read", activeProjectId)}
          modelService={models.serviceFor("structure")}
          onConfigureModel={() => setModelRole("structure")}
        />
      : projectPlaceholder("选择一个已完成处理的文档，进入结构校对；或去首页上传新文件。"))}
    {view === "read" && (document
      ? <ReadingWorkspace
          projectId={activeProjectId}
          document={document}
          translation={translation}
          selected={selected}
          checked={checked}
          selectedScopeCount={selectedScope.length}
          jobs={jobs}
          glossary={glossary}
          onToggle={toggle}
          onSelectBlock={selectBlock}
          onPause={pauseJob}
          onResume={resumeJob}
          onOpenGlossary={openGlossary}
          onTranslate={translate}
          onRefresh={() => refreshProject(activeProjectId)}
          onInstant={instant}
          onOpenReview={() => navigate("review", activeProjectId)}
          translationService={models.serviceFor("translation")}
          onConfigureModel={() => setModelRole("translation")}
        />
      : projectPlaceholder("选择一个已完成处理的文档，进入阅读与翻译；或去首页上传新文件。"))}
    {view === "export" && (activeProjectId && document
      ? <div className="page-scroll"><ExportPanel projectId={activeProjectId} onMessage={setMessage}/></div>
      : projectPlaceholder("选择一个已完成处理的文档，进入导出；或去首页上传新文件。"))}
    {isWorkView(view) && <footer className="statusbar"><span>本地项目 · 已保存</span><span>{jobs[0] ? `${jobs[0].kind} · ${jobs[0].status} · ${Math.round(jobs[0].progress * 100)}%` : "无运行任务"}</span><span>{suggestions.filter(value => !value.automatic).length} 项待确认</span></footer>}

    {settings && <Settings
      projectId={activeProjectId || undefined}
      document={document}
      selected={selected}
      instructions={instructions}
      setInstructions={setInstructions}
      usageRecords={usageRecords}
      close={() => setSettings(false)}
      onUpdated={setDocument}
      onMessage={setMessage}
      onOpenModels={openModelsForView}
    />}
    {modelRole && <ModelServicesDialog api={models} role={modelRole} close={() => setModelRole(null)} onMessage={setMessage}/>}
    {glossaryOpen && <GlossaryEditor entries={glossary} setEntries={setGlossary} close={() => setGlossaryOpen(false)} freeze={freezeGlossary}/>}
    {instantResult && <InstantPanel projectId={activeProjectId} value={instantResult} close={() => setInstantResult(null)} onSaved={() => refreshProject(activeProjectId)}/>}
    {message && <button className="toast" onClick={() => setMessage("")}>{message}</button>}
  </div>;
}

function WorkspaceState({ title, body, action, label }: { title: string; body: string; action?: () => void; label?: string }) {
  return <div className="workspace-state"><h2>{title}</h2><p>{body}</p>{action && <button className="primary" onClick={action}>{label}</button>}</div>;
}
