import { useCallback, useEffect, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import { api } from "./api";
import { isDesktop, pickDocuments } from "./desktop";
import { getCurrentWebview } from "@tauri-apps/api/webview";
import "./pdfAssets";
import type { OcrJob, OcrSlice, TaskPage } from "./types";

type Props = {
  refreshProjects: () => Promise<void>;
  openResources: () => void;
  openHistory: () => void;
};

const ACTIVE_PAGE_SIZE = 10;
const ACCEPTED_EXTENSIONS = ["pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "png", "jpg", "jpeg", "jp2", "webp", "gif", "bmp", "html"];
const ACCEPT = ACCEPTED_EXTENSIONS.map(value => `.${value}`).join(",");
const MAX_BATCH_FILES = 50;
const MAX_OFFICE_BYTES = 200_000_000;
const EXTRA_FORMATS = ["docx", "html", "latex"] as const;
type ExtraFormat = typeof EXTRA_FORMATS[number];
type PendingFile = {
  key: string; file: File | { name: string; size: number; lastModified: number }; path?: string; pageRanges: string; extraFormats: Set<ExtraFormat>;
  pageCount?: number; pageCountLoading?: boolean;
};
type UploadedFile = { upload_id: string; name: string; size: number; extension: string; source_kind: string };
const stateLabel: Record<string, string> = {
  queued: "等待开始", preparing: "正在检查并自动分段", uploading: "正在上传分段",
  processing: "MinerU 解析中", downloading: "正在下载解析结果", merging: "正在合并与规范化",
  completed: "本次解析完成", failed: "处理失败", waiting: "已暂停等待恢复", interrupted: "已中断，可恢复",
};
const sliceLabel: Record<string, string> = {
  prepared: "等待上传", uploaded: "已上传，等待解析", processing: "MinerU 解析中",
  remote_done: "解析完成，等待下载", downloaded: "已下载并校验", failed: "处理失败",
};

function extension(name: string) { return name.split(".").pop()?.toUpperCase() || "FILE"; }
function formatSize(bytes: number) {
  if (!bytes) return "本地文件";
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
function isPdf(file: { name: string }) { return file.name.toLowerCase().endsWith(".pdf"); }
async function readPdfPageCount(file: File) {
  const task = pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) });
  try { return (await task.promise).numPages; }
  finally { await task.destroy(); }
}
function fileSettingsSummary(item: PendingFile) {
  const range = isPdf(item.file) ? `页码：${item.pageRanges.trim() || "全部"}` : "完整文件";
  const formats = item.extraFormats.size ? `额外输出：${[...item.extraFormats].map(value => value.toUpperCase()).join("、")}` : "默认输出";
  return `${range} · ${formats}`;
}

function jobDescription(job: OcrJob) {
  if (job.state === "queued") return "任务已经保存在本机，等待按队列顺序开始";
  if (job.state === "preparing") return "正在读取页数并准备解析切片";
  if (job.state === "uploading") return `已提交 ${job.submitted_pages}/${job.page_count || "?"} 页`;
  if (job.state === "processing") return `MinerU 已完成 ${job.completed_pages}/${job.page_count} 页 · ${job.completed_slices}/${job.slice_count} 段`;
  if (job.state === "downloading") return `已下载 ${job.downloaded_pages}/${job.page_count} 页 · ${job.downloaded_slices}/${job.slice_count} 段`;
  if (job.state === "merging") return `${job.slice_count} 个分段已经完成，正在按原页码合并`;
  if (job.state === "completed") return "本次请求的页面已生成规范文档资源";
  if (job.state === "waiting") return "已在安全边界停止本地等待；远端任务可能仍在继续，可恢复";
  if (job.state === "interrupted") return "应用上次退出时该任务被中断；可恢复继续（远端任务可能仍在处理）";
  return job.error || "请查看失败分段并重试";
}

function queueHint(job: OcrJob) {
  return typeof job.queue_position === "number" ? `队列第 ${job.queue_position + 1} 位` : "";
}

function coverageHint(job: OcrJob) {
  if (job.state === "completed") return job.requested_ranges ? `本次完成：${job.requested_ranges}` : "本次解析完成";
  return "";
}

export default function ImportWorkspace({ refreshProjects, openResources, openHistory }: Props) {
  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [activeData, setActiveData] = useState<TaskPage | null>(null);
  const [recentData, setRecentData] = useState<TaskPage | null>(null);
  const [activePage, setActivePage] = useState(1);
  const [collapsedCompleted, setCollapsedCompleted] = useState<string[]>(() => {
    try { return JSON.parse(window.localStorage.getItem("literature.home.collapsedCompleted") || "[]"); } catch { return []; }
  });
  const [language, setLanguage] = useState("ch");
  const [modelVersion, setModelVersion] = useState("vlm");
  const [ocr, setOcr] = useState(true);
  const [tables, setTables] = useState(true);
  const [formulas, setFormulas] = useState(true);
  const [fileSettingsOpen, setFileSettingsOpen] = useState(false);
  const [tokenConfigured, setTokenConfigured] = useState(false);
  const [mineruConnected, setMineruConnected] = useState<boolean | null>(null);
  const [tokenDialog, setTokenDialog] = useState(false);
  const [tokenEditing, setTokenEditing] = useState(false);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [dragging, setDragging] = useState(false);
  const [busyStage, setBusyStage] = useState("");
  const [busyJob, setBusyJob] = useState("");

  const refreshTasks = useCallback(async (page: number) => {
    const [active, recent] = await Promise.all([
      api<TaskPage>(`/ocr/tasks?scope=active&page=${page}&page_size=${ACTIVE_PAGE_SIZE}`),
      api<TaskPage>("/ocr/tasks?scope=recent_completed"),
    ]);
    setActiveData(active);
    setRecentData(recent);
  }, []);
  const refreshMineruConnection = useCallback(async () => {
    const value = await api<{configured:boolean}>("/mineru/token");
    setTokenConfigured(value.configured);
    if (!value.configured) { setMineruConnected(null); return; }
    setMineruConnected(null);
    try {
      await api("/mineru/token/check", { method: "POST" });
      setMineruConnected(true);
    } catch {
      setMineruConnected(false);
    }
  }, []);
  useEffect(() => {
    Promise.all([
      refreshTasks(activePage),
      refreshMineruConnection(),
    ]).catch(error => setMessage(String(error)));
  }, [refreshTasks, refreshMineruConnection, activePage]);
  useEffect(() => {
    if (!activeData?.counts.total) return;
    const timer = window.setInterval(() => refreshTasks(activePage).catch(() => {}), 1500);
    return () => window.clearInterval(timer);
  }, [activeData?.counts.total, refreshTasks, activePage]);
  const addFiles = (incoming: FileList | File[]) => {
    const selected = Array.from(incoming);
    const unsupported = selected.filter(file => !ACCEPTED_EXTENSIONS.includes(file.name.split(".").pop()?.toLowerCase() || ""));
    const oversized = selected.filter(file => !file.name.toLowerCase().endsWith(".pdf") && file.size > MAX_OFFICE_BYTES);
    const accepted = selected.filter(file => !unsupported.includes(file) && !oversized.includes(file));
    const known = new Set(pendingFiles.map(item => item.key));
    const additions: PendingFile[] = accepted
      .map(file => ({
        key: `${file.name}:${file.size}:${file.lastModified}`, file,
        pageRanges: "", extraFormats: new Set<ExtraFormat>(), pageCountLoading: isPdf(file),
      }))
      .filter(item => !known.has(item.key));
    setPendingFiles(current => [...current, ...additions].slice(0, MAX_BATCH_FILES));
    if (additions.length) {
      for (const item of additions.filter(value => isPdf(value.file))) {
        if (!(item.file instanceof File)) continue;
        readPdfPageCount(item.file)
          .then(pageCount => setPendingFiles(current => current.map(value => value.key === item.key ? { ...value, pageCount, pageCountLoading: false } : value)))
          .catch(() => setPendingFiles(current => current.map(value => value.key === item.key ? { ...value, pageCountLoading: false } : value)));
      }
    }
    if (unsupported.length || oversized.length || selected.length > MAX_BATCH_FILES) {
      setMessage(`已忽略 ${unsupported.length} 个不支持格式、${oversized.length} 个超过 200MB 的非 PDF 文件；单批最多 ${MAX_BATCH_FILES} 个。`);
    }
  };
  const addLocalFiles = useCallback(async (paths: string[]) => {
    if (!paths.length) return;
    try {
      const value = await api<{ files: { path: string; name: string; size: number; page_count: number | null }[]; errors: { message: string }[] }>("/intake/files/inspect", {
        method: "POST", body: JSON.stringify({ paths: paths.slice(0, MAX_BATCH_FILES) }),
      });
      const additions: PendingFile[] = value.files.map(file => ({
        key: file.path, path: file.path, file: { name: file.name, size: file.size, lastModified: 0 },
        pageRanges: "", extraFormats: new Set<ExtraFormat>(), pageCount: file.page_count ?? undefined,
      }));
      setPendingFiles(current => {
        const known = new Set(current.map(item => item.key));
        return [...current, ...additions.filter(item => !known.has(item.key))].slice(0, MAX_BATCH_FILES);
      });
      if (value.errors.length || paths.length > MAX_BATCH_FILES) setMessage([...value.errors.map(error => error.message), `单批最多 ${MAX_BATCH_FILES} 个文件。`].join("；"));
    } catch (error) { setMessage(String(error)); }
  }, []);
  useEffect(() => {
    if (!isDesktop) return;
    let disposed = false;
    let cleanup: (() => void) | undefined;
    getCurrentWebview().onDragDropEvent(event => {
      if (busy) return;
      if (event.payload.type === "enter" || event.payload.type === "over") setDragging(true);
      else { setDragging(false); if (event.payload.type === "drop") void addLocalFiles(event.payload.paths); }
    }).then(unlisten => { if (disposed) unlisten(); else cleanup = unlisten; }).catch(error => setMessage(String(error)));
    return () => { disposed = true; cleanup?.(); };
  }, [addLocalFiles, busy]);
  const chooseFiles = async () => {
    try { await addLocalFiles(await pickDocuments(ACCEPTED_EXTENSIONS)); }
    catch (error) { setMessage(String(error)); }
  };
  const updatePageRanges = (key: string, pageRanges: string) => setPendingFiles(current => current.map(item => item.key === key ? { ...item, pageRanges } : item));
  const toggleExtraFormat = (key: string, format: ExtraFormat) => setPendingFiles(current => current.map(item => {
    if (item.key !== key) return item;
    const extraFormats = new Set(item.extraFormats);
    extraFormats.has(format) ? extraFormats.delete(format) : extraFormats.add(format);
    return { ...item, extraFormats };
  }));
  const startJobs = async () => {
    if (!pendingFiles.length) return;
    if (!tokenConfigured) { openTokenSettings(); return; }
    setBusy(true);
    let uploaded: UploadedFile[] = [];
    try {
      if (!isDesktop) {
        setBusyStage("正在上传到 Literature…");
        const form = new FormData();
        pendingFiles.forEach(item => { if (item.file instanceof File) form.append("files", item.file, item.file.name); });
        uploaded = await api<UploadedFile[]>("/intake/uploads", { method: "POST", body: form });
      } else setBusyStage("正在导入本地文件…");
      setBusyStage("正在创建 MinerU 任务…");
      const value = await api<{jobs:OcrJob[];token_configured:boolean}>("/intake/jobs", {
        method: "POST",
        body: JSON.stringify({
          items: pendingFiles.map((item, index) => ({
            ...(item.path ? { path: item.path } : { upload_id: uploaded[index]?.upload_id }),
            page_ranges: item.pageRanges.trim() || "",
            extra_formats: [...item.extraFormats],
          })),
          language, model_version: modelVersion,
          is_ocr: ocr, enable_table: tables, enable_formula: formulas,
          page_limit: 200,
        }),
      });
      setPendingFiles([]);
      await refreshTasks(1);
      setActivePage(1);
      await refreshProjects();
      setMessage(`已创建 ${value.jobs.length} 个解析任务；Literature 将自动分段并轮询 MinerU。`);
    } catch (error) {
      await Promise.allSettled(uploaded.map(item => api(`/intake/uploads/${item.upload_id}`, { method: "DELETE" })));
      setMessage(String(error));
    }
    finally { setBusy(false); setBusyStage(""); }
  };
  const saveToken = async () => {
    if (!token.trim()) return;
    setBusy(true);
    try {
      await api("/mineru/token", { method: "PUT", body: JSON.stringify({ value: token }) });
      setToken(""); setTokenConfigured(true); setTokenEditing(false);
      setMineruConnected(null);
      try {
        await api("/mineru/token/check", { method: "POST" });
        setMineruConnected(true);
        setMessage("MinerU Token 已保存并通过连接验证。现在可以开始解析。");
      } catch (error) {
        setMineruConnected(false);
        setMessage(`Token 已保存，但 MinerU 连接验证失败：${String(error)}`);
      }
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };
  const openTokenSettings = () => {
    setToken("");
    setTokenEditing(!tokenConfigured);
    setTokenDialog(true);
  };
  const retry = async (job: OcrJob) => {
    setBusyJob(job.job_id);
    try {
      await api(`/ocr/jobs/${job.job_id}/retry`, { method: "POST" });
      await refreshTasks(activePage);
      setMessage("只重试失败分段；已经成功的分段不会重复处理。");
    } catch (error) { setMessage(String(error)); }
    finally { setBusyJob(""); }
  };
  const archiveFailed = async (job: OcrJob) => {
    setBusyJob(job.job_id);
    try {
      await api(`/ocr/jobs/${job.job_id}/archive`, { method: "POST" });
      await refreshTasks(activePage);
      setMessage("已归档该失败记录，可在解析历史中取消归档找回或重试。");
    } catch (error) { setMessage(String(error)); }
    finally { setBusyJob(""); }
  };
  const reorder = async (job: OcrJob, position: "top" | "up" | "down") => {
    setBusyJob(job.job_id);
    try {
      await api(`/ocr/jobs/${job.job_id}/queue`, { method: "POST", body: JSON.stringify({ position }) });
      await refreshTasks(activePage);
      setMessage(position === "top" ? "已把该任务置顶，下一次将优先执行。" : "已调整解析队列顺序。");
    } catch (error) { setMessage(String(error)); }
    finally { setBusyJob(""); }
  };
  const resume = async (job: OcrJob) => {
    setBusyJob(job.job_id);
    try {
      await api(`/ocr/jobs/${job.job_id}/start`, { method: "POST" });
      await refreshTasks(activePage);
      setMessage("已恢复该解析任务；已上传的分段不会重复提交。");
    } catch (error) { setMessage(String(error)); }
    finally { setBusyJob(""); }
  };
  const counts = activeData?.counts;
  const recentItems = recentData?.items || [];
  const toggleCollapsed = (jobIds: string[]) => {
    const next = collapsedCompleted.length ? [] : jobIds;
    setCollapsedCompleted(next);
    try { window.localStorage.setItem("literature.home.collapsedCompleted", JSON.stringify(next)); } catch { /* ignore */ }
  };
  const visibleRecent = recentItems.filter(job => !collapsedCompleted.includes(job.job_id));

  return <>
    <main className="intake-content">
      <section className="intake-heading"><div><span className="eyebrow">DOCUMENT INTAKE</span><h1>导入与处理</h1><p>从这里导入文献，并通过 MinerU 与 Literature 流程生成标准化文档资源。</p></div><span className={`token-state ${mineruConnected ? "configured" : ""}`}>● {!tokenConfigured ? "MinerU 未配置" : mineruConnected === true ? "MinerU 已连接" : mineruConnected === false ? "MinerU 连接检查失败" : "正在检查 MinerU"}</span></section>
      <section className="intake-card">
        <div className="intake-main-column">
          <div className={`intake-dropzone ${dragging ? "dragging" : ""}`} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); if (!isDesktop && !busy) addFiles(event.dataTransfer.files); }}>
            <div className="intake-file-symbols"><i>DOC</i><i>PDF</i><i>IMG</i></div>
            <strong>将文件拖到此处</strong><span>或点击后从系统文件选择窗口批量添加</span>
            {isDesktop ? <button className="primary" disabled={busy} onClick={chooseFiles}>选择本地文件</button> : <input disabled={busy} aria-label="选择文件" className="intake-file-input" type="file" multiple accept={ACCEPT} onChange={event => { if (event.target.files) addFiles(event.target.files); event.target.value = ""; }}/>}<small>PDF · Office · 图片 · HTML · PDF 超过 200 页自动分段</small>
          </div>
          <div className="intake-queue-heading"><strong>待处理文件</strong><span>{pendingFiles.length} 个文件</span></div>
          <div className="intake-queue">{pendingFiles.length ? pendingFiles.map(item => <article key={item.key}><span className="file-kind">{extension(item.file.name)}</span><div><strong>{item.file.name}</strong><small>{formatSize(item.file.size)} · {fileSettingsSummary(item)}</small></div><div className="intake-file-actions"><button onClick={() => setFileSettingsOpen(true)}>设置</button><button aria-label={`移除 ${item.file.name}`} onClick={() => setPendingFiles(current => current.filter(value => value.key !== item.key))}>×</button></div></article>) : <div className="intake-empty">尚未选择文件</div>}</div>
        </div>
        <aside className="intake-options">
          <div className="intake-option-title"><strong>解析设置</strong><button className={tokenConfigured ? "token-button configured" : "token-button"} onClick={openTokenSettings}>🔑 {tokenConfigured ? "Token 已保存" : "设置 Token"}</button></div>
          <label>解析模型<select value={modelVersion} onChange={event => setModelVersion(event.target.value)}><option value="vlm">MinerU VLM（推荐）</option><option value="pipeline">MinerU Pipeline</option></select><small>HTML 文件会自动切换到 MinerU-HTML。</small></label>
          <label>文档语言包<select value={language} onChange={event => setLanguage(event.target.value)}><option value="ch">中英文（默认）</option><option value="ch_server">繁体／手写体</option><option value="en">纯英文</option><option value="japan">日文为主</option><option value="korean">韩文</option><option value="chinese_cht">繁体中文为主</option><option value="ta">泰米尔文</option><option value="te">泰卢固文</option><option value="ka">卡纳达文</option><option value="el">希腊文</option><option value="th">泰文</option><option value="latin">拉丁语系</option><option value="arabic">阿拉伯语系</option><option value="cyrillic">西里尔语系</option><option value="east_slavic">东斯拉夫语系</option><option value="devanagari">天城文语系</option></select></label>
          <label className="option-toggle"><span><strong>OCR</strong><small>扫描件文字识别</small></span><input type="checkbox" checked={ocr} onChange={event => setOcr(event.target.checked)}/></label>
          <label className="option-toggle"><span><strong>表格识别</strong><small>保留行列结构</small></span><input type="checkbox" checked={tables} onChange={event => setTables(event.target.checked)}/></label>
          <label className="option-toggle"><span><strong>公式识别</strong><small>保留公式结构</small></span><input type="checkbox" checked={formulas} onChange={event => setFormulas(event.target.checked)}/></label>
          <button className="file-settings-trigger" disabled={!pendingFiles.length} onClick={() => setFileSettingsOpen(true)}><span><strong>逐文件 MinerU 设置</strong><small>页码范围与额外输出格式分别保存</small></span><b>{pendingFiles.length || "—"} ›</b></button>
          <button className="primary intake-start" disabled={!pendingFiles.length || busy} onClick={startJobs}>{busy ? busyStage || "正在处理…" : `开始解析${pendingFiles.length ? `（${pendingFiles.length}）` : ""}`}</button>
          <small className="intake-privacy">{isDesktop ? "确认解析后，原件复制到本地资源库，再由后台发送给 MinerU；密钥保存在本机 Keychain。" : "文件先上传到本机 Literature，再由后台发送给 MinerU；密钥保存在本机 Keychain。"}</small>
        </aside>
      </section>
      <section className="intake-progress-card">
        <header><div><h2>当前处理任务</h2><span>{counts ? `${counts.running} 个处理中 · ${counts.queued} 个排队 · ${counts.resumable} 个等待恢复 · ${counts.failed} 个失败待处理` : "载入中…"}</span></div><div className="intake-head-actions"><button onClick={openHistory}>解析历史 →</button><button onClick={openResources}>查看全部文件资源 →</button></div></header>
        <div className="ocr-job-list">{!activeData ? <div className="intake-empty">正在载入任务…</div>
          : !activeData.items.length ? <div className="intake-empty">当前没有正在处理或待处理的解析任务。开始解析后，进度会显示在这里；完成结果进入文件资源页，更早的完成记录在解析历史中。</div>
          : activeData.items.map(job => <ActiveJobRow key={job.job_id} job={job} busy={busyJob === job.job_id} retry={retry} archive={archiveFailed} reorder={reorder} resume={resume}/>)}</div>
        {activeData && activeData.pages > 1 && <nav className="intake-pager" aria-label="当前任务分页">
          <button disabled={activeData.page <= 1} onClick={() => setActivePage(current => Math.max(1, current - 1))}>上一页</button>
          <span>第 {activeData.page} / {activeData.pages} 页 · 共 {activeData.total} 条</span>
          <button disabled={activeData.page >= activeData.pages} onClick={() => setActivePage(current => Math.min(activeData.pages, current + 1))}>下一页</button>
        </nav>}
      </section>
      <section className="intake-progress-card recent-completed-card">
        <header><div><h2>最近完成</h2><span>过去 24 小时完成的最新 3 条；一次完成只代表本次请求的页面范围</span></div>
          <div className="intake-head-actions">{recentItems.length > 0 && <button onClick={() => toggleCollapsed(recentItems.map(job => job.job_id))}>{collapsedCompleted.length ? "展开已完成" : "收起已完成"}</button>}</div>
        </header>
        <div className="ocr-job-list">{!recentData ? <div className="intake-empty">正在载入完成记录…</div>
          : !recentItems.length ? <div className="intake-empty">过去 24 小时没有新的完成记录。更早的完成请到解析历史查看。</div>
          : !visibleRecent.length ? <div className="intake-empty">已收起本页显示的完成记录；未来新完成的任务仍会出现在这里。</div>
          : visibleRecent.map(job => <RecentJobRow key={job.job_id} job={job} openResources={openResources}/>)}</div>
      </section>
    </main>
    {fileSettingsOpen && <FileSettingsDialog files={pendingFiles} close={() => setFileSettingsOpen(false)} updatePageRanges={updatePageRanges} toggleExtraFormat={toggleExtraFormat}/>}
    {tokenDialog && <div className="modal-backdrop" onClick={() => setTokenDialog(false)}><section className="token-dialog" onClick={event => event.stopPropagation()}><div className="panel-title"><h2>MinerU API Token</h2><button onClick={() => setTokenDialog(false)}>关闭</button></div><p>Token 只会保存到 macOS Keychain，不写入项目、日志或其他文件。</p>{tokenConfigured && !tokenEditing ? <div className="token-saved-state"><i>✓</i><div><strong>Token 已安全保存</strong><span>为保护密钥安全，Literature 不回显完整 Token。</span></div><button onClick={() => setTokenEditing(true)}>替换 Token</button></div> : <><label>MinerU Token<input autoFocus type="password" value={token} onChange={event => setToken(event.target.value)} placeholder="请输入 MinerU Token"/></label><button className="primary" disabled={!token.trim() || busy} onClick={saveToken}>{busy ? "正在保存…" : tokenConfigured ? "保存新 Token" : "保存 Token"}</button>{tokenConfigured && <button className="token-cancel-edit" onClick={() => { setToken(""); setTokenEditing(false); }}>取消替换</button>}</>}</section></div>}
    {message && <button className="toast" onClick={() => setMessage("")}>{message}</button>}
  </>;
}

function FileSettingsDialog({ files, close, updatePageRanges, toggleExtraFormat }: {
  files: PendingFile[]; close: () => void;
  updatePageRanges: (key: string, value: string) => void;
  toggleExtraFormat: (key: string, format: ExtraFormat) => void;
}) {
  return <div className="modal-backdrop file-settings-backdrop" onClick={close}><section className="file-settings-dialog" role="dialog" aria-modal="true" aria-label="逐文件 MinerU 设置" onClick={event => event.stopPropagation()}>
    <header><div><h2>设置每个文件的解析范围</h2><p>以下设置会分别写入对应文件资源的 MinerU 任务；PDF 页码范围留空时解析全部页。</p></div><button aria-label="关闭逐文件设置" onClick={close}>×</button></header>
    <div className="file-settings-table">
      <div className="file-settings-head"><span>文件</span><span>总页数</span><span>PDF 解析页码范围</span><span>额外输出格式</span></div>
      {files.map(item => {
        const pdf = isPdf(item.file);
        const html = item.file.name.toLowerCase().endsWith(".html");
        return <article key={item.key}>
          <div className="file-settings-name"><span className="file-kind">{extension(item.file.name)}</span><span><strong>{item.file.name}</strong><small>{formatSize(item.file.size)}</small></span></div>
          <span className="file-page-count">{pdf ? item.pageCountLoading ? "读取中…" : item.pageCount ? `${item.pageCount} 页` : "—" : "不适用"}</span>
          <label><span className="sr-only">{item.file.name} 的 PDF 解析页码范围</span><input type="text" disabled={!pdf} value={item.pageRanges} onChange={event => updatePageRanges(item.key, event.target.value)} placeholder={pdf ? "留空解析全部，例如 1-100,105" : "仅适用于 PDF"}/></label>
          <fieldset disabled={html}><legend className="sr-only">{item.file.name} 的额外输出格式</legend>{EXTRA_FORMATS.map(format => <label key={format}><input type="checkbox" checked={item.extraFormats.has(format)} onChange={() => toggleExtraFormat(item.key, format)}/>{format.toUpperCase()}</label>)}</fieldset>
          {html && <small className="html-format-note">MinerU-HTML 使用默认 Markdown/JSON 输出</small>}
        </article>;
      })}
    </div>
    <footer><small>Markdown 和 JSON 始终默认返回；每个 PDF 未填写页码时均解析全部。</small><button className="primary" onClick={close}>保存并返回</button></footer>
  </section></div>;
}

function ActiveJobRow({ job, busy, retry, archive, reorder, resume }: {
  job: OcrJob; busy: boolean;
  retry: (job: OcrJob) => void;
  archive: (job: OcrJob) => void;
  reorder: (job: OcrJob, position: "top" | "up" | "down") => void;
  resume: (job: OcrJob) => void;
}) {
  const showProgress = job.stage_progress != null;
  const queued = job.state === "queued";
  const resumable = job.state === "waiting" || job.state === "interrupted";
  const failed = job.state === "failed";
  const hint = queueHint(job);
  return <article className={`ocr-job ocr-${job.state}`}>
    <span className="file-kind">{extension(job.source_name || job.title)}</span>
    <div className="ocr-job-content">
      <div className="ocr-job-title"><strong>{job.source_name || job.title}</strong><span>{stateLabel[job.state] || job.state}{hint ? ` · ${hint}` : ""}</span></div>
      <p>{job.page_count ? `${job.page_count} 页${job.slice_count > 1 ? ` · 已自动分为 ${job.slice_count} 段` : ""}` : formatSize(job.source_size)} · {jobDescription(job)}</p>
      {coverageHint(job) && <small className="ocr-coverage">{coverageHint(job)}</small>}
      {showProgress && <progress max={1} value={job.stage_progress || 0}/>}
      {job.slice_count > 1 && <details open={job.failed_slices > 0}><summary>{job.failed_slices ? `${job.failed_slices} 个分段失败` : `查看 ${job.slice_count} 个分段的进度`}</summary><div className="ocr-slices">{job.slices.map(slice => <SliceRow key={slice.index} slice={slice}/>)}</div></details>}
      {job.error && <small className="ocr-error">{job.error}</small>}
    </div>
    <div className="ocr-job-actions">
      {queued && <><button disabled={busy} onClick={() => reorder(job, "top")}>置顶</button><button disabled={busy} onClick={() => reorder(job, "up")}>↑ 上移</button><button disabled={busy} onClick={() => reorder(job, "down")}>↓ 下移</button></>}
      {resumable && <button className="primary" disabled={busy} onClick={() => resume(job)}>{busy ? "恢复中…" : "恢复解析"}</button>}
      {job.can_retry && <button className="primary" disabled={busy} onClick={() => retry(job)}>{busy ? "重试中…" : "重试失败分段"}</button>}
      {failed && <button onClick={() => archive(job)} disabled={busy} title="从首页待处理移出，可在解析历史取消归档">归档此记录</button>}
    </div>
  </article>;
}

function recentScope(job: OcrJob) {
  const ranges = (job.requested_ranges || "").trim();
  if (ranges) return `本次范围 ${ranges}`;
  if (job.requested_pages?.length) return `本次范围 ${job.requested_pages.length} 页`;
  if (job.append) return "本次范围 剩余未解析页";
  const pages = job.page_count || job.source_page_count || 0;
  return pages ? `本次范围 全部 ${pages} 页` : "本次范围 整份文件";
}
function recentCoverage(job: OcrJob) {
  const coverage = job.parse_coverage;
  if (!coverage || coverage.state === "unknown") return "资源覆盖未知";
  if (coverage.state === "unparsed") return "资源尚未解析";
  if (coverage.state === "full") return `资源已全文解析 ${coverage.source_page_count} 页`;
  return `资源已解析 ${coverage.covered_pages}/${coverage.source_page_count} 页`;
}

function RecentJobRow({ job, openResources }: { job: OcrJob; openResources: () => void }) {
  return <article className="ocr-job ocr-recent" data-job-id={job.job_id}>
    <span className="file-kind">{extension(job.source_name || job.title)}</span>
    <div className="ocr-job-content">
      <div className="ocr-job-title"><strong>{job.source_name || job.title}</strong><span>{formatFinished(job.finished_at)}{job.finished_at_estimated ? "（旧记录按最后更新时间）" : ""}</span></div>
      <p>{recentScope(job)} · 解析 {job.this_task_pages || job.requested_pages?.length || job.page_count || 0} 页{job.slice_count > 1 ? ` · ${job.slice_count} 段` : ""}</p>
      <small className="ocr-coverage">{recentCoverage(job)}</small>
      {job.slice_count > 1 && <details><summary>查看 {job.slice_count} 个分段</summary><div className="ocr-slices">{job.slices.map(slice => <SliceRow key={slice.index} slice={slice}/>)}</div></details>}
    </div>
    <div className="ocr-job-actions"><button onClick={openResources}>查看资源</button></div>
  </article>;
}

function formatFinished(value?: string) {
  if (!value) return "完成时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return `完成 ${new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date)}`;
}

function SliceRow({ slice }: { slice: OcrSlice }) {
  const scope = slice.source_pages && slice.source_pages.length
    ? `源页 ${slice.source_pages[0]}–${slice.source_pages[slice.source_pages.length - 1]}${slice.source_pages.length !== slice.source_pages[slice.source_pages.length - 1] - slice.source_pages[0] + 1 ? `（${slice.source_pages.length} 页）` : ""}`
    : slice.start_page > 0 ? `第 ${slice.start_page}–${slice.end_page} 页` : "完整文件";
  return <div className={`ocr-slice slice-${slice.state}`}><i/>
    <span>第 {slice.index} 段 · {scope}</span>
    <strong>{sliceLabel[slice.state] || slice.state}</strong>
    {slice.error && <small>{slice.error}</small>}
  </div>;
}
