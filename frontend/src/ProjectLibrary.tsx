import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type { OcrJob, Project } from "./types";

type Props = {
  projects: Project[];
  refreshProjects: () => Promise<void>;
  openProject: (project: Project) => void;
  /** 当前仍打开的项目的 id，用于在列表里标出「当前任务」并可一键返回。 */
  currentProjectId?: string;
};
type ViewMode = "grid" | "list";
type SortKey = "updated_at" | "created_at" | "title" | "source_size";

const statusLabel: Record<string, string> = {
  created: "等待解析", ocr_queued: "等待解析", ocr_preparing: "准备中", ocr_uploading: "上传中",
  ocr_processing: "MinerU 解析中", ocr_downloading: "下载结果", ocr_merging: "生成规范文档",
  ocr_waiting: "等待恢复", ocr_failed: "解析失败", ocr_imported: "处理完成", ready: "处理完成",
  queued: "等待解析", preparing: "准备中", uploading: "上传中", processing: "MinerU 解析中",
  downloading: "下载结果", merging: "生成规范文档", waiting: "等待恢复", failed: "解析失败",
  completed: "处理完成",
};
const statusTone: Record<string, string> = {
  created: "waiting", ocr_queued: "waiting", ocr_preparing: "working", ocr_uploading: "working",
  ocr_processing: "working", ocr_downloading: "working", ocr_merging: "working", ocr_waiting: "waiting",
  ocr_failed: "failed", ocr_imported: "ready", ready: "ready",
  queued: "waiting", preparing: "working", uploading: "working", processing: "working",
  downloading: "working", merging: "working", waiting: "waiting", failed: "failed", completed: "ready",
};

function extension(project: Project) {
  const fromName = project.source_name?.split(".").pop();
  return (fromName || project.source_type || "FILE").toUpperCase();
}
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

export default function ProjectLibrary({ projects, refreshProjects, openProject, currentProjectId }: Props) {
  const [jobs, setJobs] = useState<OcrJob[]>([]);
  const [query, setQuery] = useState("");
  const [view, setView] = useState<ViewMode>(() => (window.localStorage.getItem("literature.library.view") as ViewMode) || "grid");
  const [sortKey, setSortKey] = useState<SortKey>("updated_at");
  const [descending, setDescending] = useState(true);
  const [status, setStatus] = useState("all");
  const [kind, setKind] = useState("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(12);
  const [menu, setMenu] = useState("");
  const [renaming, setRenaming] = useState<Project | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [deleting, setDeleting] = useState<Project | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    let active = true;
    const refresh = () => api<OcrJob[]>("/ocr/jobs").then(value => { if (active) setJobs(value); }).catch(error => { if (active) setMessage(String(error)); });
    refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  useEffect(() => { window.localStorage.setItem("literature.library.view", view); }, [view]);
  useEffect(() => { setPage(1); }, [query, status, kind, sortKey, descending, pageSize]);

  const jobsByProject = useMemo(() => {
    const values = new Map<string, OcrJob>();
    for (const job of jobs) if (!values.has(job.project_id)) values.set(job.project_id, job);
    return values;
  }, [jobs]);
  const toneFor = (project: Project) => statusTone[jobsByProject.get(project.project_id)?.state || project.state] || "waiting";
  const kinds = useMemo(() => [...new Set(projects.map(project => project.source_type || "file"))].sort(), [projects]);
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    const values = projects.filter(project => {
      const matchesQuery = !normalized || `${project.title} ${project.source_name || ""}`.toLocaleLowerCase().includes(normalized);
      const matchesStatus = status === "all" || toneFor(project) === status;
      const matchesKind = kind === "all" || (project.source_type || "file") === kind;
      return matchesQuery && matchesStatus && matchesKind;
    });
    return values.sort((left, right) => {
      const a = sortKey === "title" ? left.title.toLocaleLowerCase() : sortKey === "source_size" ? left.source_size || 0 : left[sortKey] || "";
      const b = sortKey === "title" ? right.title.toLocaleLowerCase() : sortKey === "source_size" ? right.source_size || 0 : right[sortKey] || "";
      const compared = typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b), "zh-CN");
      return descending ? -compared : compared;
    });
  }, [projects, query, status, kind, sortKey, descending, jobsByProject]);
  const counts = useMemo(() => ({
    waiting: projects.filter(project => toneFor(project) === "waiting").length,
    working: projects.filter(project => toneFor(project) === "working").length,
    failed: projects.filter(project => toneFor(project) === "failed").length,
    ready: projects.filter(project => toneFor(project) === "ready").length,
  }), [projects, jobsByProject]);
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const visible = filtered.slice((page - 1) * pageSize, page * pageSize);
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);

  const startRename = (project: Project) => { setMenu(""); setRenameValue(project.title); setRenaming(project); };
  const saveRename = async () => {
    if (!renaming || !renameValue.trim()) return;
    setBusy(true);
    try {
      await api(`/projects/${renaming.project_id}`, { method: "PATCH", body: JSON.stringify({ title: renameValue.trim() }) });
      await refreshProjects(); setRenaming(null); setMessage("资源已重命名");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await api(`/projects/${deleting.project_id}`, { method: "DELETE" });
      await refreshProjects(); setDeleting(null); setMessage("资源已移出资源库，并保留在本地回收目录");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };
  const action = (project: Project) => { if (project.has_document) openProject(project); };

  return <>
    <main className="library-content" onClick={() => setMenu("")}>
      <section className="library-heading">
        <div><span className="eyebrow">FILE RESOURCES</span><h1>文件资源</h1><p>统一查看所有文档任务及资源状态；上传与处理从首页开始。打开已完成的任务即可回到结构校对。</p></div>
      </section>

      <section className="resource-overview" aria-label="文件资源状态概览">
        <div><strong>{projects.length}</strong><span>全部资源</span></div><div><strong>{counts.working}</strong><span>处理中</span></div><div><strong>{counts.waiting}</strong><span>等待中</span></div><div><strong>{counts.failed}</strong><span>处理失败</span></div><div><strong>{counts.ready}</strong><span>已完成</span></div>
      </section>

      <section className="library-toolbar" aria-label="资源筛选和显示选项">
        <label className="library-search"><span>⌕</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索资源名称或原始文件名"/></label>
        <select aria-label="状态筛选" value={status} onChange={event => setStatus(event.target.value)}><option value="all">全部状态</option><option value="ready">已完成</option><option value="working">处理中</option><option value="waiting">等待中</option><option value="failed">失败</option></select>
        <select aria-label="文件类型筛选" value={kind} onChange={event => setKind(event.target.value)}><option value="all">全部类型</option>{kinds.map(value => <option key={value} value={value}>{value.toUpperCase()}</option>)}</select>
        <span className="toolbar-divider"/>
        <select aria-label="排序依据" value={sortKey} onChange={event => setSortKey(event.target.value as SortKey)}><option value="updated_at">最近修改</option><option value="created_at">创建时间</option><option value="title">资源名称</option><option value="source_size">文件大小</option></select>
        <button className="sort-direction" aria-label={descending ? "当前降序，点击切换升序" : "当前升序，点击切换降序"} onClick={() => setDescending(value => !value)}>{descending ? "↓" : "↑"}</button>
        <div className="view-switch"><button aria-label="网格视图" className={view === "grid" ? "active" : ""} onClick={() => setView("grid")}>▦</button><button aria-label="列表视图" className={view === "list" ? "active" : ""} onClick={() => setView("list")}>☷</button></div>
      </section>

      <div className="library-summary"><strong>{filtered.length} 项资源</strong><span>{query || status !== "all" || kind !== "all" ? `已从 ${projects.length} 项资源中筛选` : "包含执行中与已完成任务"}</span></div>
      {visible.length ? view === "grid" ? <div className="project-grid">{visible.map(project => <ProjectCard key={project.project_id} project={project} job={jobsByProject.get(project.project_id)} current={project.project_id === currentProjectId} menu={menu} setMenu={setMenu} open={() => action(project)} rename={() => startRename(project)} remove={() => { setMenu(""); setDeleting(project); }}/>)}</div> : <ProjectTable projects={visible} jobsByProject={jobsByProject} menu={menu} setMenu={setMenu} open={action} rename={startRename} remove={project => { setMenu(""); setDeleting(project); }}/> : <div className="library-empty"><div>⌕</div><h2>{projects.length ? "没有符合条件的资源" : "还没有文件资源"}</h2><p>{projects.length ? "请调整关键词或筛选条件。" : "在首页上传并开始处理后，任务和生成的标准化文档资源会统一出现在这里。"}</p></div>}

      {filtered.length > pageSize && <footer className="library-pagination"><span>第 {page} / {pageCount} 页</span><div><button disabled={page <= 1} onClick={() => setPage(value => value - 1)}>←</button><button disabled={page >= pageCount} onClick={() => setPage(value => value + 1)}>→</button></div><label>每页 <select value={pageSize} onChange={event => setPageSize(Number(event.target.value))}><option value={12}>12</option><option value={24}>24</option><option value={48}>48</option></select></label></footer>}
    </main>
    {renaming && <Dialog title="重命名资源" close={() => setRenaming(null)}><label className="library-dialog-label">资源名称<input autoFocus value={renameValue} maxLength={240} onChange={event => setRenameValue(event.target.value)} onKeyDown={event => { if (event.key === "Enter") saveRename(); }}/></label><div className="dialog-actions"><button onClick={() => setRenaming(null)}>取消</button><button className="primary" disabled={busy || !renameValue.trim()} onClick={saveRename}>{busy ? "保存中…" : "保存"}</button></div></Dialog>}
    {deleting && <Dialog title="移出资源库" close={() => setDeleting(null)}><p>确定移出“{deleting.title}”吗？资源不会立即永久删除；完整目录将保留在资源库的 <code>.trash</code> 回收目录中。</p><div className="dialog-actions"><button onClick={() => setDeleting(null)}>取消</button><button className="danger" disabled={busy} onClick={remove}>{busy ? "正在移出…" : "移出资源库"}</button></div></Dialog>}
    {message && <button className="toast" onClick={() => setMessage("")}>{message}</button>}
  </>;
}

function ProjectCard({ project, job, current, menu, setMenu, open, rename, remove }: { project: Project; job?: OcrJob; current?: boolean; menu: string; setMenu: (id: string) => void; open: () => void; rename: () => void; remove: () => void }) {
  const state = job?.state || project.state;
  const progress = job?.stage_progress;
  return <article className={`project-card ${project.has_document ? "is-openable" : "is-processing"}${current ? " is-current" : ""}`} tabIndex={project.has_document ? 0 : undefined} onClick={project.has_document ? open : undefined} onKeyDown={event => { if (project.has_document && event.key === "Enter") open(); }}>
    <div className="project-card-preview"><FileSymbol project={project}/><span className={`project-status ${statusTone[state] || "waiting"}`}><i/>{statusLabel[state] || state}</span>{current && <span className="project-current-badge">当前任务</span>}<Menu project={project} open={menu === project.project_id} setMenu={setMenu} rename={rename} remove={remove}/>{progress != null && state !== "completed" && <progress className="resource-card-progress" max={1} value={progress}/>}</div>
    <div className="project-card-body"><h2 title={project.title}>{project.title}</h2><p title={project.source_name}>{project.source_name || "本地文件"}</p><div><span>{formatSize(project.source_size)}</span><span>{formatDate(project.updated_at)}</span></div></div>
    <footer><span>{project.has_document ? `${project.asset_count || 0} 个关联资源 · ${project.export_count || 0} 个导出` : progress != null ? `当前阶段 ${Math.round(progress * 100)}%` : "等待生成标准化文档"}</span>{project.has_document && <button onClick={event => { event.stopPropagation(); open(); }}>{current ? "继续任务 →" : "打开资源 →"}</button>}</footer>
  </article>;
}

function ProjectTable({ projects, jobsByProject, menu, setMenu, open, rename, remove }: { projects: Project[]; jobsByProject: Map<string, OcrJob>; menu: string; setMenu: (id: string) => void; open: (project: Project) => void; rename: (project: Project) => void; remove: (project: Project) => void }) {
  return <div className="project-table"><div className="project-table-head"><span>资源名称</span><span>任务状态</span><span>类型</span><span>大小</span><span>修改时间</span><span>操作</span></div>{projects.map(project => {
    const job = jobsByProject.get(project.project_id);
    const state = job?.state || project.state;
    const label = statusLabel[state] || state;
    return <article key={project.project_id} className={project.has_document ? "is-openable" : "is-processing"} onClick={project.has_document ? () => open(project) : undefined}><div className="table-project-name"><FileSymbol project={project}/><span><strong>{project.title}</strong><small>{project.source_name || "本地文件"}</small></span></div><span className={`project-status ${statusTone[state] || "waiting"}`}><i/>{job?.stage_progress != null && state !== "completed" ? `${label} · ${Math.round(job.stage_progress * 100)}%` : label}</span><span>{extension(project)}</span><span>{formatSize(project.source_size)}</span><span>{formatDate(project.updated_at)}</span><Menu project={project} open={menu === project.project_id} setMenu={setMenu} rename={() => rename(project)} remove={() => remove(project)}/></article>;
  })}</div>;
}

function FileSymbol({ project }: { project: Project }) { return <span className={`project-file-symbol file-${project.source_type || "file"}`}><i>{extension(project)}</i><b>≡</b></span>; }
function Menu({ project, open, setMenu, rename, remove }: { project: Project; open: boolean; setMenu: (id: string) => void; rename: () => void; remove: () => void }) {
  return <div className="project-menu"><button aria-label={`${project.title} 的操作`} onClick={event => { event.stopPropagation(); setMenu(open ? "" : project.project_id); }}>•••</button>{open && <div onClick={event => event.stopPropagation()}><button onClick={rename}>重命名</button><button className="danger-text" onClick={remove}>移出资源库</button></div>}</div>;
}
function Dialog({ title, close, children }: { title: string; close: () => void; children: React.ReactNode }) {
  return <div className="modal-backdrop" onClick={close}><section className="library-dialog" role="dialog" aria-modal="true" aria-label={title} onClick={event => event.stopPropagation()}><div className="panel-title"><h2>{title}</h2><button onClick={close}>关闭</button></div>{children}</section></div>;
}
