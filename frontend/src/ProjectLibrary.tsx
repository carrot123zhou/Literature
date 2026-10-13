import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "./api";
import { isDesktop, revealPath } from "./desktop";
import type { WorkView } from "./HomeTopbar";
import type { OcrJob, Project, ProjectReviewSummary, ProjectTranslateSummary } from "./types";
import "./resource.css";

type Props = {
  projects: Project[];
  currentProjectId?: string;
  refreshProjects: () => Promise<void>;
  onOpenView: (view: WorkView, projectId: string) => void;
  onTranslateProject: (projectId: string, options: { scope: string; locale: string }) => Promise<boolean>;
  onGoHome: () => void;
  onMessage: (message: string) => void;
};

type ViewMode = "grid" | "list";
type SortKey = "updated_at" | "created_at" | "title" | "source_size";
type Tone = "done" | "run" | "act" | "fail" | "off";
type Stage = { tone: Tone; label: string; progress?: number | null };

const TINT: Record<string, string> = { pdf: "#b8503f", docx: "#3f6fb8", pptx: "#c47a2c", xlsx: "#3f8a5c", image: "#7a5cc0", html: "#5f6b64" };

const percent = (value?: number | null) => (value == null ? "" : `${Math.round(value * 100)}%`);

function typeOf(project: Project) {
  const name = project.source_name?.split(".").pop()?.toLowerCase();
  const raw = (name || project.source_type || "file").toLowerCase();
  if (raw === "jpeg" || raw === "jpg" || raw === "png" || raw === "gif" || raw === "bmp" || raw === "webp" || raw === "jp2") return "image";
  if (raw === "doc" || raw === "docx") return "docx";
  if (raw === "ppt" || raw === "pptx") return "pptx";
  if (raw === "xls" || raw === "xlsx") return "xlsx";
  return raw;
}
function extension(project: Project) { return typeOf(project).toUpperCase().slice(0, 4); }
function formatSize(bytes = 0) {
  if (!bytes) return "—";
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
function formatDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}

function reviewOf(project: Project): ProjectReviewSummary {
  return project.pipeline?.review || project.review || { state: "todo", pending: 0 };
}
function translateOf(project: Project): ProjectTranslateSummary {
  if (project.pipeline?.translate) return project.pipeline.translate;
  const stats = project.translation;
  const total = stats?.total || 0;
  const done = stats?.done || 0;
  const failed = stats?.failed || 0;
  const stale = stats?.stale || 0;
  const staleSource = stats?.stale_source || 0;
  const state: ProjectTranslateSummary["state"] = !project.has_document || total === 0 ? "none"
    : done >= total && !staleSource && !failed ? "done"
    : done === 0 && failed > 0 && !staleSource ? "failed" : "partial";
  return {
    state, done, total, failed, stale, stale_source: staleSource, percent: stats?.percent || 0,
    progress: total ? done / total : null,
    locale: project.target_language || "zh-CN",
    glossary_frozen: Boolean(stats?.glossary_frozen),
  };
}
function coverageSuffix(project: Project): string {
  const coverage = project.parse_coverage;
  if (!coverage || coverage.state !== "partial") return "";
  return ` · 已合并 ${coverage.covered_pages}/${coverage.source_page_count} 页`;
}

function extractStage(project: Project, job?: OcrJob): Stage {
  const state = job?.state || "";
  const suffix = coverageSuffix(project);
  if (state === "failed") return { tone: "fail", label: `提取失败${suffix}` };
  if (state === "waiting" || state === "interrupted") return { tone: "act", label: `${state === "interrupted" ? "已中断" : "等待恢复"}${suffix}` };
  if (state === "queued") return { tone: "run", label: `排队中${suffix}` };
  if (state && state !== "completed") return { tone: "run", label: `解析中 ${percent(job?.stage_progress)}${suffix}`, progress: job?.stage_progress ?? null };
  const coverage = project.parse_coverage;
  if (!coverage || coverage.state === "unknown") return project.has_document ? { tone: "act", label: "覆盖未知" } : { tone: "off", label: "未解析" };
  if (coverage.state === "full") return { tone: "done", label: `全部解析 ${coverage.source_page_count} 页` };
  if (coverage.state === "partial") return { tone: "act", label: `部分解析 ${coverage.covered_pages}/${coverage.source_page_count} 页`, progress: coverage.source_page_count ? coverage.covered_pages / coverage.source_page_count : null };
  return { tone: "off", label: "未解析" };
}

function coverageText(project: Project, job?: OcrJob): string {
  const active = Boolean(job && !["completed", "failed"].includes(job.state));
  if (!project.has_document) return active ? "解析中，完成后生成规范文档" : "尚未开始解析";
  const coverage = project.parse_coverage;
  if (!coverage || coverage.state === "unknown") return "全文覆盖未知（旧资源或非 PDF）";
  if (coverage.state === "unparsed") return "尚未合并任何页面";
  if (coverage.state === "full") return `已合并全部 ${coverage.source_page_count} 页`;
  if (coverage.state === "partial") {
    const ranges = (coverage.ranges || []).map(item => item[0] === item[1] ? `${item[0]}` : `${item[0]}-${item[1]}`).join("、");
    return `已合并 ${coverage.covered_pages}/${coverage.source_page_count} 页（${ranges}），剩余 ${coverage.source_page_count - coverage.covered_pages} 页`;
  }
  return "尚未合并任何页面";
}

function canContinueParse(project: Project): boolean {
  return Boolean(project.has_document) && project.parse_coverage?.state === "partial";
}

function needsFirstParse(project: Project): boolean {
  return !project.has_document;
}

function coverageUnknown(project: Project): boolean {
  return Boolean(project.has_document) && (!project.parse_coverage || project.parse_coverage.state === "unknown");
}

function migrateReasonText(reason: string): string {
  const map: Record<string, string> = {
    no_document: "该资源还没有规范文档",
    already_known: "解析范围已经明确",
    source_missing: "源文件已移动或缺失",
    source_changed: "源文件已被修改，历史结果不再对应",
    raw_manifest_missing: "缺少已封存的 MinerU 原文记录",
    raw_verification_failed: "已封存原文的哈希校验失败（证据可能被改动）",
    raw_slice_count_mismatch: "封存切片数与规范文档不一致",
    raw_hash_mismatch: "规范文档与封存原文的哈希对不上",
    canonical_page_count_mismatch: "规范文档页数与封存切片不一致",
    no_trusted_evidence: "没有任何已完成的解析任务",
    completed_but_unverifiable: "存在已完成任务，但缺少可核对的页码证据",
    pages_exceed_source: "记录页数超出源 PDF",
    page_mapping_incomplete: "部分页面无法映射",
    page_ranges_not_trusted_prefix: "历史页码范围不是可信的连续前缀 1-N",
    prefix_slice_count_mismatch: "历史前缀任务的切片数与封存结果不一致",
    prefix_slice_not_downloaded: "历史前缀任务的切片未完成下载",
    prefix_slice_range_mismatch: "历史前缀任务的切片范围不是 1-N",
    prefix_job_page_count_mismatch: "历史任务的页数与页码范围不一致",
    prefix_canonical_page_mismatch: "规范文档页数与历史前缀范围不一致",
    prefix_document_page_mismatch: "已解析页数与历史前缀范围不一致",
    prefix_pages_exceed_source: "历史前缀页数超出源 PDF",
    prefix_raw_slice_missing: "缺少对应的封存原文切片",
    prefix_content_missing: "封存切片中没有 content_list_v2",
    prefix_content_unreadable: "封存 content_list_v2 无法读取",
    prefix_content_page_mismatch: "封存 content_list_v2 页数与历史前缀不一致",
    prefix_layout_page_mismatch: "layout.json 页数与历史前缀不一致",
    prefix_origin_page_mismatch: "原始切片 PDF 页数与历史前缀不一致",
    prefix_no_page_evidence: "缺少可核对的页数证据（layout / origin PDF）",
  };
  return map[reason] || `无法核对（${reason || "未知原因"}）`;
}
function reviewStage(project: Project): Stage {
  const review = reviewOf(project);
  if (review.state === "confirm") return { tone: "act", label: `待确认 ${review.pending}` };
  if (review.state === "done") return { tone: "done", label: "已校对" };
  if (review.state === "partial") return { tone: "run", label: "部分校对" };
  return { tone: "off", label: project.has_document ? "未开始" : "—" };
}
function translateStage(project: Project): Stage {
  if (!project.has_document) return { tone: "off", label: "—" };
  const t = translateOf(project);
  const parsedOnly = project.parse_coverage?.state === "partial";
  if (t.state === "done") return { tone: "done", label: parsedOnly ? "已译已解析内容" : "已完成" };
  if (t.state === "failed") return { tone: "fail", label: "失败" };
  if (t.state === "running") return { tone: "run", label: `进行中 ${t.progress != null ? percent(t.progress) : `${Math.round(t.percent)}%`}`, progress: t.progress ?? t.percent / 100 };
  if (t.state === "partial") return { tone: "run", label: `部分 ${t.done}/${t.total}`, progress: t.progress };
  return { tone: "off", label: "未开始" };
}
function badge(project: Project, job?: OcrJob): { tone: Tone; label: string } {
  const extract = extractStage(project, job);
  const translate = translateOf(project);
  if (extract.tone === "fail") return { tone: "fail", label: "提取失败" };
  if (translate.state === "failed") return { tone: "fail", label: "翻译失败" };
  if (job && (job.state === "waiting" || job.state === "interrupted")) return { tone: "act", label: job.state === "interrupted" ? "解析已中断" : "解析等待恢复" };
  if (extract.tone === "run") return { tone: "run", label: job?.state === "queued" ? "排队中" : "解析中" };
  if (project.parse_coverage?.state === "partial") return { tone: "act", label: `部分解析 ${project.parse_coverage.covered_pages}/${project.parse_coverage.source_page_count}` };
  if (reviewOf(project).state === "confirm") return { tone: "act", label: "待确认" };
  if (translate.state === "running" || translate.state === "partial") return { tone: "run", label: "翻译中" };
  if (project.has_document && reviewOf(project).state !== "done") return { tone: "act", label: "待校对" };
  if (!project.has_document) return { tone: "off", label: "未解析" };
  return { tone: "done", label: "已完成" };
}

function StatusPill({ tone, label }: { tone: Tone; label: string }) {
  return <span className={`res-status ${tone}`}><i className="dot"/>{label}</span>;
}
function Strip({ name, stage }: { name: string; stage: Stage }) {
  return <div className={`res-strip ${stage.tone}`}><span className="k">{name}</span><span className="v"><i className="dot"/>{stage.label}</span></div>;
}
function Strips({ project, job }: { project: Project; job?: OcrJob }) {
  return <div className="res-strips">
    <Strip name="提取" stage={extractStage(project, job)}/>
    <Strip name="结构校对" stage={reviewStage(project)}/>
    <Strip name="翻译" stage={translateStage(project)}/>
  </div>;
}

export default function ProjectLibrary({ projects, currentProjectId, refreshProjects, onOpenView, onTranslateProject, onGoHome, onMessage }: Props) {
  const [jobs, setJobs] = useState<OcrJob[]>([]);
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewMode>(() => (window.localStorage.getItem("literature.library.view") as ViewMode) || "grid");
  const [filter, setFilter] = useState("all");
  const [type, setType] = useState("all");
  const [sortKey, setSortKey] = useState<SortKey>("updated_at");
  const [modal, setModal] = useState("");
  const [expanded, setExpanded] = useState("");
  const [slicesFor, setSlicesFor] = useState("");
  const [menu, setMenu] = useState<{ id: string; top: number; left: number } | null>(null);
  const [translating, setTranslating] = useState<Project | null>(null);
  const [renaming, setRenaming] = useState<Project | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deleting, setDeleting] = useState<Project | null>(null);
  const [continuing, setContinuing] = useState<Project | null>(null);
  const [starting, setStarting] = useState<Project | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshJobs = useCallback(async () => {
    try { setJobs(await api<OcrJob[]>("/ocr/jobs")); } catch { /* 轮询失败不打断当前页面 */ }
  }, []);

  useEffect(() => { window.localStorage.setItem("literature.library.view", view); }, [view]);
  useEffect(() => {
    let active = true;
    const refresh = () => api<OcrJob[]>("/ocr/jobs").then(value => { if (active) setJobs(value); }).catch(() => {});
    refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [menu]);

  const jobsByProject = useMemo(() => {
    const values = new Map<string, OcrJob>();
    for (const job of jobs) if (!values.has(job.project_id)) values.set(job.project_id, job);
    return values;
  }, [jobs]);

  const kinds = useMemo(() => [...new Set(projects.map(typeOf))].sort(), [projects]);
  const filters = useMemo(() => {
    const failed = (project: Project) => extractStage(project, jobsByProject.get(project.project_id)).tone === "fail" || translateOf(project).state === "failed";
    return [
      { key: "all", label: "全部资源", test: () => true },
      { key: "extract", label: "解析中", test: (project: Project) => extractStage(project, jobsByProject.get(project.project_id)).tone === "run" },
      { key: "review", label: "待校对", test: (project: Project) => Boolean(project.has_document) && reviewOf(project).state !== "done" },
      { key: "translate", label: "翻译中", test: (project: Project) => ["running", "partial"].includes(translateOf(project).state) },
      { key: "failed", label: "有失败", test: failed },
    ];
  }, [jobsByProject]);

  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    const activeFilter = filters.find(value => value.key === filter) || filters[0];
    const values = projects.filter(project => {
      const matchesQuery = !normalized || `${project.title} ${project.source_name || ""}`.toLocaleLowerCase().includes(normalized);
      const matchesType = type === "all" || typeOf(project) === type;
      return matchesQuery && matchesType && activeFilter.test(project);
    });
    return values.sort((left, right) => {
      const a = sortKey === "title" ? left.title.toLocaleLowerCase() : sortKey === "source_size" ? left.source_size || 0 : left[sortKey] || "";
      const b = sortKey === "title" ? right.title.toLocaleLowerCase() : sortKey === "source_size" ? right.source_size || 0 : right[sortKey] || "";
      if (typeof a === "number" && typeof b === "number") return b - a;
      return sortKey === "title" ? String(a).localeCompare(String(b), "zh-CN") : String(b).localeCompare(String(a), "zh-CN");
    });
  }, [projects, query, type, filter, filters, sortKey]);

  const openView = (target: WorkView, project: Project) => {
    setMenu(null);
    if (!project.has_document) { onMessage("提取完成后即可进入该工作流。"); return; }
    setModal(""); setExpanded("");
    onOpenView(target, project.project_id);
  };
  const startTranslate = (project: Project) => {
    setMenu(null);
    if (!project.has_document) { onMessage("提取完成后即可开始翻译。"); return; }
    setTranslating(project);
  };
  const openMenu = (event: React.MouseEvent<HTMLElement>, project: Project) => {
    event.stopPropagation();
    const rect = event.currentTarget.getBoundingClientRect();
    setMenu({
      id: project.project_id,
      top: Math.min(rect.bottom + 6, window.innerHeight - 250),
      left: Math.max(8, Math.min(rect.right - 178, window.innerWidth - 192)),
    });
  };

  const retryExtract = async (project: Project) => {
    const job = jobsByProject.get(project.project_id);
    if (!job) { onMessage("没有可重试的解析任务。"); return; }
    setBusy(true);
    try {
      await api(`/ocr/jobs/${job.job_id}/retry`, { method: "POST" });
      await refreshJobs();
      onMessage("已重新提交失败分段；已经成功的分段不会重复处理。");
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  const resumeExtract = async (project: Project) => {
    const job = jobsByProject.get(project.project_id);
    if (!job) { onMessage("没有可恢复的解析任务。"); return; }
    setBusy(true);
    try {
      await api(`/ocr/jobs/${job.job_id}/start`, { method: "POST" });
      await refreshJobs();
      onMessage("已恢复该解析任务；已上传的分段不会重复提交。");
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  const reorderExtract = async (project: Project, position: "top" | "up" | "down") => {
    const job = jobsByProject.get(project.project_id);
    if (!job) { onMessage("没有可调序的解析任务。"); return; }
    setBusy(true);
    try {
      await api(`/ocr/jobs/${job.job_id}/queue`, { method: "POST", body: JSON.stringify({ position }) });
      await refreshJobs();
      onMessage(position === "top" ? "已将该任务置顶，下一次优先执行。" : "已调整解析队列顺序，刷新后仍保留。");
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  const migrateExtract = async (project: Project) => {
    setBusy(true);
    try {
      const value = await api<{ migrated: boolean; reason: string; coverage?: { state?: string; covered_pages?: number; source_page_count?: number } | null }>(
        `/projects/${project.project_id}/parse/migrate`, { method: "POST" },
      );
      await refreshProjects();
      await refreshJobs();
      if (value.migrated && value.coverage) {
        const label = value.coverage.state === "full" ? "全部解析" : `已合并 ${value.coverage.covered_pages}/${value.coverage.source_page_count} 页`;
        onMessage(`已按历史证据恢复解析范围：${label}。现在可以继续解析剩余页。`);
      } else {
        onMessage(`无法自动恢复：${migrateReasonText(value.reason)}。请人工核对原 PDF 已解析的页码范围。`);
      }
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  const continueExtract = async (project: Project, pageRanges: string) => {
    setBusy(true);
    try {
      const value = await api<{ job: OcrJob; skipped_pages: number[] }>(`/projects/${project.project_id}/ocr/continue`, {
        method: "POST", body: JSON.stringify({ page_ranges: pageRanges }),
      });
      await refreshJobs();
      await refreshProjects();
      setContinuing(null);
      const skipped = value.skipped_pages.length ? `；已跳过已解析页 ${value.skipped_pages.join("、")}` : "";
      onMessage(`已创建继续解析任务${skipped}。`);
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  type StartOptions = { pageRanges: string; language: string; modelVersion: string; isOcr: boolean; enableTable: boolean; enableFormula: boolean };
  const startExtract = async (project: Project, options: StartOptions) => {
    setBusy(true);
    try {
      const job = await api<OcrJob>(`/projects/${project.project_id}/ocr/start`, {
        method: "POST",
        body: JSON.stringify({
          page_ranges: options.pageRanges.trim(),
          language: options.language, model_version: options.modelVersion,
          is_ocr: options.isOcr, enable_table: options.enableTable, enable_formula: options.enableFormula,
          page_limit: 200,
        }),
      });
      await refreshJobs();
      await refreshProjects();
      setStarting(null);
      const total = project.parse_coverage?.source_page_count || job.source_page_count || job.page_count || 0;
      const scope = options.pageRanges.trim() ? `范围 ${options.pageRanges.trim()}` : total ? `全部 ${total} 页` : "整份文件";
      onMessage(`已创建解析任务（${scope}）；完成后会生成规范文档。`);
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  const reveal = (project: Project) => {
    setMenu(null);
    if (isDesktop && project.path) void revealPath(project.path).catch(error => onMessage(String(error)));
    else onMessage(project.path || project.title);
  };
  const saveRename = async () => {
    if (!renaming || !renameValue.trim()) return;
    setBusy(true);
    try {
      await api(`/projects/${renaming.project_id}`, { method: "PATCH", body: JSON.stringify({ title: renameValue.trim() }) });
      await refreshProjects();
      setRenaming(null);
      onMessage("资源已重命名");
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await api(`/projects/${deleting.project_id}`, { method: "DELETE" });
      await refreshProjects();
      setDeleting(null);
      onMessage("资源已移出资源库，并保留在本地回收目录");
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(false); }
  };

  const activeProject = (id: string) => projects.find(project => project.project_id === id);

  return <>
    <main className="res-page" onClick={() => setMenu(null)}>
      <section className="res-head">
        <span className="res-eyebrow">FILE RESOURCES</span>
        <h1>文件资源</h1>
        <p>提取完成后，校对与翻译是两条独立的工作流。网格视图点击卡片弹出详情，列表视图就地展开。</p>
      </section>

      <div className="res-section">概览</div>
      <section className="res-stats" aria-label="文件资源状态概览">
        {filters.map(value => <button
          key={value.key}
          className={`res-stat${value.key === "failed" ? " is-fail" : ""}${filter === value.key ? " on" : ""}`}
          onClick={() => setFilter(value.key)}
        ><b>{projects.filter(value.test).length}</b><span>{value.label}</span></button>)}
      </section>

      <div className="res-section">资源</div>
      <section className="res-panel">
        <div className="res-toolbar">
          <label className="res-search"><span>⌕</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索资源名称或原始文件名"/></label>
          <select aria-label="文件类型筛选" value={type} onChange={event => setType(event.target.value)}><option value="all">全部类型</option>{kinds.map(value => <option key={value} value={value}>{value.toUpperCase()}</option>)}</select>
          <select aria-label="排序依据" value={sortKey} onChange={event => setSortKey(event.target.value as SortKey)}><option value="updated_at">最近修改</option><option value="created_at">创建时间</option><option value="title">资源名称</option><option value="source_size">文件大小</option></select>
          <div className="res-seg"><button className={view === "grid" ? "on" : ""} onClick={() => { setView("grid"); setExpanded(""); }}>网格</button><button className={view === "list" ? "on" : ""} onClick={() => { setView("list"); setModal(""); }}>列表</button></div>
        </div>
        <div className="res-summary"><span>{filtered.length} 项资源</span><span>{query || type !== "all" || filter !== "all" ? `已从 ${projects.length} 项资源中筛选` : "全部阶段"}</span></div>
        <div className="res-body">
          {!filtered.length
            ? <div className="res-empty"><div className="big">⌕</div><h2>{projects.length ? "没有符合条件的资源" : "还没有文件资源"}</h2><p>{projects.length ? "调整关键词、阶段或类型筛选后再试。" : "在首页上传并开始处理后，任务和生成的标准化文档资源会统一出现在这里。"}</p></div>
            : view === "grid"
              ? <div className="res-grid">{filtered.map(project => <Card key={project.project_id} project={project} job={jobsByProject.get(project.project_id)} current={project.project_id === currentProjectId} onOpen={() => setModal(project.project_id)} onMenu={event => openMenu(event, project)}/>)}</div>
              : <div className="res-list">
                  <div className="res-lhead"><span>资源名称</span><span>提取</span><span>结构校对</span><span>翻译</span><span>更新时间</span><span>状态</span></div>
                  {filtered.map(project => <Row
                    key={project.project_id}
                    project={project}
                    job={jobsByProject.get(project.project_id)}
                    current={project.project_id === currentProjectId}
                    expanded={expanded === project.project_id}
                    onToggle={() => { setMenu(null); setExpanded(expanded === project.project_id ? "" : project.project_id); }}
                    onMenu={event => openMenu(event, project)}
                    detail={<Workstreams project={project} job={jobsByProject.get(project.project_id)} slicesOpen={slicesFor === project.project_id} onToggleSlices={() => setSlicesFor(slicesFor === project.project_id ? "" : project.project_id)} onRetry={() => retryExtract(project)} onContinue={() => { setMenu(null); setExpanded(""); setContinuing(project); }} onResume={() => resumeExtract(project)} onReorder={position => reorderExtract(project, position)} onMigrate={() => migrateExtract(project)} onProgress={onGoHome} onStart={() => { setMenu(null); setExpanded(""); setStarting(project); }} onOpen={openView} onTranslate={startTranslate} busy={busy}/>}
                    footer={<>
                      <button onClick={() => reveal(project)}>在 Finder 中显示</button>
                      <button onClick={() => { setMenu(null); setRenameValue(project.title); setRenaming(project); }}>重命名</button>
                      <button className="danger-out" onClick={() => { setMenu(null); setDeleting(project); }}>移出资源库</button>
                      <span className="grow"/>
                      <span style={{ color: "var(--faint)", fontSize: 12 }}>{project.path}</span>
                    </>}
                  />)}
                </div>}
        </div>
      </section>

      {menu && activeProject(menu.id) && <div className="res-menu" style={{ top: menu.top, left: menu.left }} onClick={event => event.stopPropagation()}>
        {(() => { const project = activeProject(menu.id)!; return <>
          <button onClick={() => openView("review", project)}>结构校对</button>
          <button onClick={() => startTranslate(project)}>翻译…</button>
          <button onClick={() => reveal(project)}>在 Finder 中显示</button>
          <hr/>
          <button onClick={() => { setMenu(null); setRenameValue(project.title); setRenaming(project); }}>重命名</button>
          <button className="dz" onClick={() => { setMenu(null); setDeleting(project); }}>移出资源库</button>
        </>; })()}
      </div>}

      {modal && activeProject(modal) && <DetailDialog
        project={activeProject(modal)!}
        job={jobsByProject.get(modal)}
        slicesOpen={slicesFor === modal}
        onToggleSlices={() => setSlicesFor(slicesFor === modal ? "" : modal)}
        onRetry={() => retryExtract(activeProject(modal)!)}
        onContinue={() => { setModal(""); setContinuing(activeProject(modal)!); }}
        onResume={() => resumeExtract(activeProject(modal)!)}
        onReorder={position => reorderExtract(activeProject(modal)!, position)}
        onMigrate={() => migrateExtract(activeProject(modal)!)}
        onProgress={onGoHome}
        onStart={() => { setModal(""); setStarting(activeProject(modal)!); }}
        onOpen={openView}
        onTranslate={startTranslate}
        busy={busy}
        onClose={() => setModal("")}
        onReveal={reveal}
        onRename={project => { setModal(""); setRenameValue(project.title); setRenaming(project); }}
        onRemove={project => { setModal(""); setDeleting(project); }}
      />}

      {translating && <TranslateDialog
        project={translating}
        close={() => setTranslating(null)}
        onOpenReading={project => { setTranslating(null); openView("read", project); }}
        onSubmit={async options => {
          const ok = await onTranslateProject(translating.project_id, options);
          if (ok) setTranslating(null);
        }}
      />}

      {continuing && <ContinueDialog
        project={continuing}
        busy={busy}
        close={() => setContinuing(null)}
        onSubmit={pageRanges => continueExtract(continuing, pageRanges)}
      />}

      {starting && <StartParseDialog
        project={starting}
        busy={busy}
        close={() => setStarting(null)}
        onSubmit={options => startExtract(starting, options)}
      />}

      {renaming && <Dialog title="重命名资源" close={() => setRenaming(null)}>
        <p className="res-field"><label>资源名称<input autoFocus value={renameValue} maxLength={240} onChange={event => setRenameValue(event.target.value)} onKeyDown={event => { if (event.key === "Enter") void saveRename(); }}/></label></p>
        <div className="res-dialog-foot"><span className="grow"/><button onClick={() => setRenaming(null)}>取消</button><button className="primary" disabled={busy || !renameValue.trim()} onClick={saveRename}>{busy ? "保存中…" : "保存"}</button></div>
      </Dialog>}

      {deleting && <Dialog title="移出资源库" close={() => setDeleting(null)}>
        <p>确定移出“{deleting.title}”吗？资源不会立即永久删除；完整目录将保留在资源库的 <code>.trash</code> 回收目录中。</p>
        <div className="res-dialog-foot"><span className="grow"/><button onClick={() => setDeleting(null)}>取消</button><button className="danger-out" disabled={busy} onClick={remove}>{busy ? "正在移出…" : "移出资源库"}</button></div>
      </Dialog>}
    </main>
  </>;
}

function Card({ project, job, current, onOpen, onMenu }: { project: Project; job?: OcrJob; current?: boolean; onOpen: () => void; onMenu: (event: React.MouseEvent<HTMLElement>) => void }) {
  const value = badge(project, job);
  return <article className={`res-card${current ? " is-current" : ""}`} tabIndex={0} onClick={onOpen} onKeyDown={event => { if (event.key === "Enter") onOpen(); }}>
    <div className="res-chead">
      <span className="res-mono" style={{ background: TINT[typeOf(project)] || "#5f6b64" }}>{extension(project)}</span>
      <div className="res-ctitle"><h3 title={project.title}>{project.title}{current && <span className="res-cur-tag">当前任务</span>}</h3><p className="src" title={project.source_name}>{project.source_name || "本地文件"}</p></div>
      <div className="res-card-actions">
        <StatusPill tone={value.tone} label={value.label}/>
        <button className="res-menu-btn" aria-label={`${project.title} 的操作`} onClick={onMenu} onKeyDown={event => event.stopPropagation()}>•••</button>
      </div>
    </div>
    <Strips project={project} job={job}/>
  </article>;
}

function Row({ project, job, current, expanded, onToggle, onMenu, detail, footer }: {
  project: Project; job?: OcrJob; current?: boolean; expanded: boolean;
  onToggle: () => void; onMenu: (event: React.MouseEvent<HTMLElement>) => void; detail: ReactNode; footer: ReactNode;
}) {
  const value = badge(project, job);
  return <>
    <div className={`res-lrow${current ? " is-current" : ""}`} onClick={onToggle}>
      <div className="res-lcell res-lname">
        <span className="res-mono" style={{ background: TINT[typeOf(project)] || "#5f6b64" }}>{extension(project)}</span>
        <span className="tx"><b>{project.title}{current ? " · 当前" : ""}</b><small>{project.source_name || "本地文件"} · {formatSize(project.source_size)} · {job?.page_count ? `${job.page_count} 页` : project.has_document ? "已标准化" : "—"}</small></span>
      </div>
      <StageCell stage={extractStage(project, job)}/>
      <StageCell stage={reviewStage(project)}/>
      <StageCell stage={translateStage(project)}/>
      <span className="res-lcell" style={{ color: "var(--muted)", fontSize: 12 }}>{formatDate(project.updated_at)}</span>
      <span className="res-lcell" style={{ display: "flex", alignItems: "center", gap: 6, justifyContent: "space-between" }}>
        <StatusPill tone={value.tone} label={value.label}/>
        <button className="res-menu-btn" aria-label={`${project.title} 的操作`} onClick={event => { event.stopPropagation(); onMenu(event); }}>•••</button>
      </span>
    </div>
    {expanded && <div className="res-expand"><div className="res-wf" style={{ paddingTop: 6 }}>{detail}</div><div className="res-foot">{footer}</div></div>}
  </>;
}
function StageCell({ stage }: { stage: Stage }) {
  return <span className={`res-lcell res-stg ${stage.tone}`}><i className="dot"/>{stage.label}</span>;
}

function Slices({ job }: { job: OcrJob }) {
  return <div className="res-slices">
    {job.slices.length ? job.slices.map(slice => <div key={slice.index}>
      <span>第 {slice.index} 段 · {slice.source_pages && slice.source_pages.length ? `源页 ${slice.source_pages[0]}–${slice.source_pages[slice.source_pages.length - 1]}` : `${slice.start_page}–${slice.end_page} 页`}</span>
      <small>{slice.state}{slice.error ? ` · ${slice.error}` : ""}</small>
    </div>) : <div><span>该任务没有分段信息</span><small/></div>}
  </div>;
}

function Workstreams({ project, job, slicesOpen, onToggleSlices, onRetry, onContinue, onResume, onReorder, onMigrate, onProgress, onStart, onOpen, onTranslate, busy }: {
  project: Project; job?: OcrJob; slicesOpen: boolean; onToggleSlices: () => void;
  onRetry: () => void; onContinue: () => void; onResume: () => void;
  onReorder: (position: "top" | "up" | "down") => void;
  onMigrate: () => void; onProgress: () => void; onStart: () => void; onOpen: (view: WorkView, project: Project) => void;
  onTranslate: (project: Project) => void; busy: boolean;
}) {
  const gate = Boolean(project.has_document);
  const extract = extractStage(project, job);
  const review = reviewOf(project);
  const translate = translateOf(project);
  const extractKind: Tone = extract.tone === "fail" ? "fail" : extract.tone === "done" ? "done" : extract.tone === "act" ? "act" : "run";
  const extractStatus = extract.label;
  const extractDesc = [
    coverageText(project, job),
    job ? [job.model_version ? `${job.model_version} 模型` : "", job.slice_count > 1 ? `分段 ${job.completed_slices}/${job.slice_count}` : "", job.requested_ranges ? `本次范围 ${job.requested_ranges}` : ""].filter(Boolean).join(" · ") : "",
    job?.error || "",
  ].filter(Boolean).join(" · ");
  const queued = job?.state === "queued";
  const resumable = job?.state === "waiting" || job?.state === "interrupted";
  const extractActive = Boolean(job && !["completed", "failed"].includes(job.state));
  const partial = canContinueParse(project) && !extractActive;
  const reviewKind: Tone = !gate ? "off" : review.state === "done" ? "done" : review.state === "confirm" || review.state === "partial" ? "act" : "off";
  const translateKind: Tone = !gate ? "off" : translate.state === "done" ? "done" : translate.state === "failed" ? "fail" : ["running", "partial"].includes(translate.state) ? "run" : "off";
  return <div className="res-wf">
    <div className={`res-wfrow ${extractKind}`}>
      <span className="ico">{extractKind === "done" ? "✓" : extractKind === "fail" ? "!" : 1}</span>
      <div className="txt">
        <div className="t"><b>提取（MinerU）</b><span className="st">{extractStatus}</span></div>
        <p>{extractDesc}</p>
        {extract.tone === "run" && job?.stage_progress != null && <div className="res-mini-prog"><i style={{ width: `${Math.round(job.stage_progress * 100)}%` }}/></div>}
      </div>
      <div className="side">
        {extract.tone === "fail"
          ? <button className="warn" disabled={busy || !job?.can_retry} onClick={onRetry}>重试失败分段</button>
          : resumable
            ? <button className="primary" disabled={busy} onClick={onResume}>恢复解析</button>
            : <>{queued && <><button disabled={busy} onClick={() => onReorder("top")}>置顶</button><button disabled={busy} onClick={() => onReorder("up")}>上移</button><button disabled={busy} onClick={() => onReorder("down")}>下移</button></>}
                {partial && <button className="primary" disabled={busy} onClick={onContinue}>继续解析</button>}
                {coverageUnknown(project) && !extractActive && <button className="primary" disabled={busy} onClick={onMigrate}>检查解析范围</button>}
                {extract.tone === "done" && <button onClick={onToggleSlices}>{slicesOpen ? "收起分段" : "查看分段"}</button>}
                {!partial && !coverageUnknown(project) && extract.tone !== "done" && !extractActive && (needsFirstParse(project)
                  ? <button className="primary" disabled={busy} onClick={onStart}>开始解析</button>
                  : job ? <button onClick={onProgress}>查看进度</button> : null)}
                {extractActive && <button disabled title="当前任务正在执行，完成后才能继续解析剩余页">有任务进行中</button>}</>}
      </div>
    </div>
    {slicesOpen && job && <Slices job={job}/>}
    <div className={`res-wfrow ${reviewKind}`}>
      <span className="ico">{reviewKind === "done" ? "✓" : 2}</span>
      <div className="txt">
        <div className="t"><b>结构校对</b><span className="st">{!gate ? "等待提取" : review.state === "done" ? "已完成" : review.state === "confirm" ? `${review.pending} 项待确认` : review.state === "partial" ? "部分校对（未确认全量）" : "未开始"}</span></div>
        <p>{!gate ? "提取完成后即可校对，无需先翻译。" : review.state === "done" ? "目录与标题层级已确认；结构补丁历史可撤销。" : "AI 与本地校准给出的标题层级建议需人工确认后应用。"}</p>
      </div>
      <div className="side">{gate && <button className={review.state === "done" ? "" : "primary"} onClick={() => onOpen("review", project)}>{review.state === "done" ? "查看记录" : "进入校对"}</button>}</div>
    </div>
    <div className={`res-wfrow ${translateKind}`}>
      <span className="ico">{translateKind === "done" ? "✓" : translateKind === "fail" ? "!" : 3}</span>
      <div className="txt">
        <div className="t"><b>翻译</b><span className="st">{!gate ? "等待提取" : translate.state === "done" ? (project.parse_coverage?.state === "partial" ? "已译已解析内容" : "已完成") : translate.state === "failed" ? "失败" : translate.state === "running" ? `进行中 ${percent(translate.progress)}` : translate.state === "partial" ? "部分完成" : "未开始"}</span></div>
        <p>{!gate ? "提取完成后即可翻译，无需先校对。"
          : `目标语言：${translate.locale} · 已翻译 ${translate.done} / ${translate.total} 段${translate.failed ? ` · 失败 ${translate.failed} 段` : ""}${translate.stale ? ` · 待复核 ${translate.stale} 段` : ""} · 术语表${translate.glossary_frozen ? "已冻结" : "未创建"}${project.parse_coverage?.state === "partial" ? `（仅统计已解析的 ${project.parse_coverage.covered_pages}/${project.parse_coverage.source_page_count} 页；剩余未解析内容尚未翻译）` : ""}`}</p>
        {["running", "partial"].includes(translate.state) && translate.progress != null && <div className="res-mini-prog"><i style={{ width: `${Math.round(translate.progress * 100)}%` }}/></div>}
      </div>
      <div className="side">{gate && (translate.state === "done"
        ? <><button onClick={() => onOpen("read", project)}>阅读与翻译</button><button onClick={() => onTranslate(project)}>重译…</button></>
        : <button className={translate.state === "failed" ? "warn" : "primary"} onClick={() => onTranslate(project)}>{translate.state === "none" ? "开始翻译" : translate.state === "failed" ? "重试翻译" : "继续翻译"}</button>)}</div>
    </div>
    <div className="res-meta">
      <div><span>项目路径</span><br/><b>{project.path || "—"}</b></div>
      <div><span>文件大小</span><br/><b>{formatSize(project.source_size)}</b></div>
      <div><span>页数 / 更新时间</span><br/><b>{job?.page_count || (project.has_document ? "已标准化" : "—")} · {formatDate(project.updated_at)}</b></div>
    </div>
  </div>;
}

function DetailDialog({ project, job, slicesOpen, onToggleSlices, onRetry, onContinue, onResume, onReorder, onMigrate, onProgress, onStart, onOpen, onTranslate, busy, onClose, onReveal, onRename, onRemove }: {
  project: Project; job?: OcrJob; slicesOpen: boolean; onToggleSlices: () => void;
  onRetry: () => void; onContinue: () => void; onResume: () => void;
  onReorder: (position: "top" | "up" | "down") => void;
  onMigrate: () => void; onProgress: () => void; onStart: () => void; onOpen: (view: WorkView, project: Project) => void;
  onTranslate: (project: Project) => void; busy: boolean; onClose: () => void;
  onReveal: (project: Project) => void; onRename: (project: Project) => void; onRemove: (project: Project) => void;
}) {
  const value = badge(project, job);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return <div className="res-pop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="res-dialog" role="dialog" aria-modal="true" aria-label={`${project.title} 详情`}>
      <div className="res-dialog-head">
        <span className="res-mono" style={{ background: TINT[typeOf(project)] || "#5f6b64" }}>{extension(project)}</span>
        <div style={{ flex: 1, minWidth: 0 }}><h2>{project.title}</h2><p className="src">{project.source_name || "本地文件"}</p></div>
        <StatusPill tone={value.tone} label={value.label}/>
        <button className="res-iconbtn" aria-label="关闭" onClick={onClose}>✕</button>
      </div>
      <div className="res-dialog-body">
        <Workstreams project={project} job={job} slicesOpen={slicesOpen} onToggleSlices={onToggleSlices} onRetry={onRetry} onContinue={onContinue} onResume={onResume} onReorder={onReorder} onMigrate={onMigrate} onProgress={onProgress} onStart={onStart} onOpen={onOpen} onTranslate={onTranslate} busy={busy}/>
      </div>
      <div className="res-dialog-foot">
        <button onClick={() => onReveal(project)}>在 Finder 中显示</button>
        <button onClick={() => onRename(project)}>重命名</button>
        <button className="danger-out" onClick={() => onRemove(project)}>移出资源库</button>
        <span className="grow"/>
        <button onClick={onClose}>关闭</button>
      </div>
    </section>
  </div>;
}

const LOCALES = [
  { value: "zh-CN", label: "简体中文（zh-CN）" },
  { value: "en", label: "English (en)" },
  { value: "ja", label: "日本語 (ja)" },
];

function TranslateDialog({ project, close, onOpenReading, onSubmit }: {
  project: Project; close: () => void; onOpenReading: (project: Project) => void;
  onSubmit: (options: { scope: string; locale: string }) => Promise<void>;
}) {
  const translate = translateOf(project);
  const [locale, setLocale] = useState(translate.locale || project.target_language || "zh-CN");
  const [scope, setScope] = useState("book");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);
  const options = [
    { value: "book", title: "全书翻译", note: `翻译全部 ${translate.total || "可译"} 个段落结构，跳过已锁定的人工译文` },
    ...(translate.done > 0 ? [{ value: "untranslated", title: "仅未翻译段落", note: `继续剩余 ${Math.max(0, translate.total - translate.done)} 段，适合中断后接续` }] : []),
    ...(translate.stale > 0 ? [{ value: "stale", title: "仅待复核段落", note: `结构变化后需要复核的 ${translate.stale} 段，已锁定的人工译文保留` }] : []),
    ...(translate.failed > 0 ? [{ value: "failed", title: "仅重试失败段落", note: `重试 ${translate.failed} 个失败分块` }] : []),
    { value: "blocks", title: "选定章节", note: "先进入阅读与翻译页勾选章节后再启动" },
  ];
  const submit = async () => {
    if (scope === "blocks") { onOpenReading(project); return; }
    setBusy(true);
    await onSubmit({ scope, locale });
    setBusy(false);
  };
  return <div className="res-pop" onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <section className="res-dialog res-form" role="dialog" aria-modal="true" aria-label={`翻译 ${project.title}`}>
      <div className="res-dialog-head" style={{ alignItems: "center" }}>
        <div style={{ flex: 1 }}><h2 style={{ fontSize: 17 }}>翻译 · {project.title}</h2></div>
        <button className="res-iconbtn" aria-label="关闭" onClick={close}>✕</button>
      </div>
      <div className="res-dialog-body" style={{ padding: "16px 19px" }}>
        <div className="pc" style={{ padding: 0 }}>
          <div className="res-field"><label>目标语言</label><select value={locale} onChange={event => setLocale(event.target.value)}>{LOCALES.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></div>
          <div className="res-field"><label>翻译范围 · 每次都需要手动选择</label><div className="res-radio">
            {options.map(item => <label key={item.value} className={scope === item.value ? "on" : ""}>
              <input type="radio" name="scope" checked={scope === item.value} onChange={() => setScope(item.value)}/>
              <span>{item.title}<small>{item.note}</small></span>
            </label>)}
          </div></div>
          <div className="res-xnote">校对未完成不影响翻译；之后若调整了结构，新结构会自动同步，已锁定的人工译文保留。</div>
          <div className="res-field"><label>术语表</label><select disabled><option>{translate.glossary_frozen ? "使用已冻结术语表" : "未创建术语表（可直接开始）"}</option></select></div>
        </div>
      </div>
      <div className="res-dialog-foot"><span className="grow"/><button onClick={close}>取消</button><button className="primary" disabled={busy} onClick={submit}>{busy ? "提交中…" : scope === "blocks" ? "去选章节" : "开始翻译"}</button></div>
    </section>
  </div>;
}

function ContinueDialog({ project, busy, close, onSubmit }: {
  project: Project; busy: boolean; close: () => void; onSubmit: (pageRanges: string) => void;
}) {
  const coverage = project.parse_coverage;
  const [value, setValue] = useState("");
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);
  const total = coverage?.source_page_count || 0;
  const covered = coverage?.covered_pages || 0;
  const coveredText = (coverage?.ranges || []).map(item => item[0] === item[1] ? `${item[0]}` : `${item[0]}-${item[1]}`).join("、");
  return <div className="res-pop" onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <section className="res-dialog res-form" role="dialog" aria-modal="true" aria-label={`继续解析 ${project.title}`}>
      <div className="res-dialog-head" style={{ alignItems: "center" }}>
        <div style={{ flex: 1 }}><h2 style={{ fontSize: 17 }}>继续解析 · {project.title}</h2></div>
        <button className="res-iconbtn" aria-label="关闭" onClick={close}>✕</button>
      </div>
      <div className="res-dialog-body" style={{ padding: "16px 19px" }}>
        <div className="res-field"><label>当前已合并</label><p style={{ margin: 0 }}>{covered}/{total} 页{coveredText ? `（${coveredText}）` : ""}</p></div>
        <div className="res-field"><label>还要解析的页码</label>
          <input autoFocus value={value} maxLength={2000} onChange={event => setValue(event.target.value)} placeholder={`留空解析剩余 ${Math.max(0, total - covered)} 页，例如 101-300 或 5,7-9`}/>
        </div>
        <div className="res-xnote">Literature 只发送未合并的源页，并在本地生成只含所选页的子集；已经合并过的页面会自动跳过，不会覆盖已有原文、人工校对或译文。</div>
      </div>
      <div className="res-dialog-foot"><span className="grow"/><button onClick={close}>取消</button>
        <button className="primary" disabled={busy} onClick={() => onSubmit(value.trim())}>{busy ? "提交中…" : "开始继续解析"}</button>
      </div>
    </section>
  </div>;
}

type StartParseOptions = { pageRanges: string; language: string; modelVersion: string; isOcr: boolean; enableTable: boolean; enableFormula: boolean };

function StartParseDialog({ project, busy, close, onSubmit }: {
  project: Project; busy: boolean; close: () => void; onSubmit: (options: StartParseOptions) => void;
}) {
  const [value, setValue] = useState("");
  const [language, setLanguage] = useState("ch");
  const [modelVersion, setModelVersion] = useState("vlm");
  const [isOcr, setIsOcr] = useState(true);
  const [enableTable, setEnableTable] = useState(true);
  const [enableFormula, setEnableFormula] = useState(true);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);
  const isPdf = (project.source_type || "").toLowerCase() === "pdf";
  const total = project.parse_coverage?.source_page_count || 0;
  const pageText = isPdf
    ? (total ? `共 ${total} 页` : "页数待确认")
    : `${extension(project)} 文件（由 MinerU 转换后解析）`;
  return <div className="res-pop" onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <section className="res-dialog res-form" role="dialog" aria-modal="true" aria-label={`开始解析 ${project.title}`}>
      <div className="res-dialog-head" style={{ alignItems: "center" }}>
        <div style={{ flex: 1 }}><h2 style={{ fontSize: 17 }}>开始解析 · {project.title}</h2></div>
        <button className="res-iconbtn" aria-label="关闭" onClick={close}>✕</button>
      </div>
      <div className="res-dialog-body" style={{ padding: "16px 19px" }}>
        <div className="res-field"><label>源文件</label><p style={{ margin: 0 }}>{project.source_name || "本地文件"} · {pageText}</p></div>
        <div className="res-field"><label>要解析的页码</label>
          <input autoFocus value={value} maxLength={2000} disabled={!isPdf || !total} onChange={event => setValue(event.target.value)} placeholder={isPdf && total ? `留空解析全部 ${total} 页，例如 1-200 或 1,3-5` : "非 PDF 或不支持指定页码"}/>
        </div>
        <div className="res-field"><label>解析参数（本次任务）</label>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <select aria-label="解析模型" value={modelVersion} onChange={event => setModelVersion(event.target.value)}><option value="vlm">MinerU VLM（推荐）</option><option value="pipeline">MinerU Pipeline</option></select>
            <select aria-label="文档语言包" value={language} onChange={event => setLanguage(event.target.value)}><option value="ch">中英文（默认）</option><option value="ch_server">繁体／手写体</option><option value="en">纯英文</option><option value="japan">日文为主</option><option value="korean">韩文</option><option value="chinese_cht">繁体中文为主</option><option value="latin">拉丁语系</option><option value="cyrillic">西里尔语系</option></select>
          </div>
          <div className="res-checkline" style={{ display: "flex", gap: 12, marginTop: 6, fontSize: 12 }}>
            <label><input type="checkbox" checked={isOcr} onChange={event => setIsOcr(event.target.checked)}/> OCR</label>
            <label><input type="checkbox" checked={enableTable} onChange={event => setEnableTable(event.target.checked)}/> 表格</label>
            <label><input type="checkbox" checked={enableFormula} onChange={event => setEnableFormula(event.target.checked)}/> 公式</label>
          </div>
        </div>
        <div className="res-xnote">开始解析会为这个从未解析的资源创建首个任务并生成规范文档；已经合并的页、人工校对和译文不会被新建任务覆盖。</div>
      </div>
      <div className="res-dialog-foot"><span className="grow"/><button onClick={close}>取消</button>
        <button className="primary" disabled={busy} onClick={() => onSubmit({ pageRanges: value.trim(), language, modelVersion, isOcr, enableTable, enableFormula })}>{busy ? "提交中…" : "开始解析任务"}</button>
      </div>
    </section>
  </div>;
}

function Dialog({ title, close, children }: { title: string; close: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);
  return <div className="res-pop" onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <section className="res-dialog res-form" role="dialog" aria-modal="true" aria-label={title} onClick={event => event.stopPropagation()}>
      <div className="res-dialog-head"><div style={{ flex: 1 }}><h2 style={{ fontSize: 17 }}>{title}</h2></div><button className="res-iconbtn" aria-label="关闭" onClick={close}>✕</button></div>
      <div className="res-dialog-body" style={{ padding: "16px 19px" }}>{children}</div>
    </section>
  </div>;
}
