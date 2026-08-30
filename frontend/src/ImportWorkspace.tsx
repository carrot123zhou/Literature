import { useCallback, useEffect, useMemo, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { api } from "./api";
import type { OcrJob, OcrSlice, Project } from "./types";

type Props = {
  projects: Project[];
  refreshProjects: () => Promise<void>;
  openProject: (projectId: string) => void;
};

const FINAL_STATES = new Set(["completed", "failed"]);
const stateLabel: Record<string, string> = {
  queued: "等待开始", preparing: "正在检查并自动分段", uploading: "正在上传分段",
  processing: "MinerU 解析中", downloading: "正在下载解析结果", merging: "正在合并与规范化",
  completed: "已完成", failed: "处理失败", waiting: "等待恢复",
};
const sliceLabel: Record<string, string> = {
  prepared: "等待上传", uploaded: "已上传，等待解析", processing: "MinerU 解析中",
  remote_done: "解析完成，等待下载", downloaded: "已下载并校验", failed: "处理失败",
};

function basename(path: string) { return path.split(/[\\/]/).pop() || path; }
function formatSize(bytes: number) {
  if (!bytes) return "本地文件";
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function jobDescription(job: OcrJob) {
  if (job.state === "queued") return "任务已经保存在本机，等待开始";
  if (job.state === "preparing") return "正在读取页数；超过 200 页时将自动分段";
  if (job.state === "uploading") return `已提交 ${job.submitted_pages}/${job.page_count || "?"} 页`;
  if (job.state === "processing") return `MinerU 已完成 ${job.completed_pages}/${job.page_count} 页 · ${job.completed_slices}/${job.slice_count} 段`;
  if (job.state === "downloading") return `已下载 ${job.downloaded_pages}/${job.page_count} 页 · ${job.downloaded_slices}/${job.slice_count} 段`;
  if (job.state === "merging") return `${job.slice_count} 个分段已经完成，正在按原页码合并`;
  if (job.state === "completed") return `已生成 document.json、source.md 和资源文件`;
  if (job.state === "waiting") return "远端任务可能仍在继续；Literature 会在启动后恢复检查";
  return job.error || "请查看失败分段并重试";
}

export default function ImportWorkspace({ projects, refreshProjects, openProject }: Props) {
  const [paths, setPaths] = useState<string[]>([]);
  const [jobs, setJobs] = useState<OcrJob[]>([]);
  const [language, setLanguage] = useState("auto");
  const [ocr, setOcr] = useState(true);
  const [tables, setTables] = useState(true);
  const [formulas, setFormulas] = useState(true);
  const [tokenConfigured, setTokenConfigured] = useState(false);
  const [tokenDialog, setTokenDialog] = useState(false);
  const [tokenEditing, setTokenEditing] = useState(false);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [dragging, setDragging] = useState(false);

  const refreshJobs = useCallback(async () => setJobs(await api<OcrJob[]>("/ocr/jobs")), []);
  useEffect(() => {
    Promise.all([
      refreshJobs(),
      api<{configured:boolean}>("/mineru/token").then(value => setTokenConfigured(value.configured)),
    ]).catch(error => setMessage(String(error)));
  }, [refreshJobs]);
  useEffect(() => {
    if (!jobs.some(job => !FINAL_STATES.has(job.state))) return;
    const timer = window.setInterval(() => refreshJobs().catch(() => {}), 1500);
    return () => window.clearInterval(timer);
  }, [jobs, refreshJobs]);
  useEffect(() => {
    if (!("__TAURI_INTERNALS__" in window)) return;
    let unlisten: undefined | (() => void);
    import("@tauri-apps/api/webview").then(({ getCurrentWebview }) =>
      getCurrentWebview().onDragDropEvent(event => {
        if (event.payload.type === "enter") setDragging(true);
        if (event.payload.type === "leave") setDragging(false);
        if (event.payload.type === "drop") {
          setDragging(false);
          addPaths(event.payload.paths);
        }
      }).then(value => { unlisten = value; })
    ).catch(() => {});
    return () => unlisten?.();
  }, []);

  const addPaths = (incoming: string[]) => {
    const unsupported = incoming.filter(path => !path.toLowerCase().endsWith(".pdf"));
    const accepted = incoming.filter(path => path.toLowerCase().endsWith(".pdf"));
    setPaths(current => [...new Set([...current, ...accepted])]);
    if (unsupported.length) setMessage(`第一阶段仅加入 PDF；已忽略 ${unsupported.length} 个其他格式文件。`);
  };
  const chooseFiles = async () => {
    let selected: string[] = [];
    if ("__TAURI_INTERNALS__" in window) {
      const value = await open({ multiple: true, filters: [{ name: "PDF 文档", extensions: ["pdf"] }] });
      selected = Array.isArray(value) ? value : value ? [value] : [];
    } else {
      selected = (window.prompt("浏览器开发模式：请输入 PDF 绝对路径，多个路径用逗号分隔") || "")
        .split(",").map(value => value.trim()).filter(Boolean);
    }
    addPaths(selected);
  };
  const startJobs = async () => {
    if (!paths.length) return;
    if (!tokenConfigured) { setTokenDialog(true); return; }
    setBusy(true);
    try {
      const value = await api<{jobs:OcrJob[];token_configured:boolean}>("/intake/jobs", {
        method: "POST",
        body: JSON.stringify({
          paths, language, model_version: "vlm", is_ocr: ocr,
          enable_table: tables, enable_formula: formulas, page_limit: 200,
        }),
      });
      setPaths([]);
      setJobs(current => [...value.jobs, ...current]);
      await refreshProjects();
      setMessage(`已创建 ${value.jobs.length} 个解析任务；Literature 将自动分段并轮询 MinerU。`);
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };
  const saveToken = async () => {
    if (!token.trim()) return;
    setBusy(true);
    try {
      await api("/mineru/token", { method: "PUT", body: JSON.stringify({ value: token }) });
      setToken(""); setTokenConfigured(true); setTokenEditing(false);
      setMessage("MinerU Token 已保存到 macOS Keychain。现在可以开始解析。");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };
  const openTokenSettings = () => {
    setToken("");
    setTokenEditing(!tokenConfigured);
    setTokenDialog(true);
  };
  const retry = async (job: OcrJob) => {
    setBusy(true);
    try {
      await api(`/ocr/jobs/${job.job_id}/retry`, { method: "POST" });
      await refreshJobs();
      setMessage("只重试失败分段；已经成功的分段不会重复处理。");
    } catch (error) { setMessage(String(error)); }
    finally { setBusy(false); }
  };
  const jobProjectIds = useMemo(() => new Set(jobs.map(job => job.project_id)), [jobs]);
  const standaloneProjects = projects.filter(project => !jobProjectIds.has(project.project_id));
  const waiting = jobs.filter(job => job.state === "queued" || job.state === "waiting").length;
  const working = jobs.filter(job => !FINAL_STATES.has(job.state) && job.state !== "queued" && job.state !== "waiting").length;
  const completed = jobs.filter(job => job.state === "completed").length;

  return <div className="intake-shell">
    <header className="intake-topbar">
      <button className="intake-brand">Literature</button>
      <nav><button className="active">导入与解析</button><button disabled>结构校对</button><button disabled>阅读与翻译</button><button disabled>导出</button></nav>
      <button onClick={openTokenSettings}>⚙ 设置</button>
    </header>
    <main className="intake-content">
      <section className="intake-heading"><div><span className="eyebrow">DOCUMENT INTAKE</span><h1>导入与解析</h1><p>添加 PDF 文献，使用 MinerU 生成结构化解析数据。</p></div><span className={`token-state ${tokenConfigured ? "configured" : ""}`}>● {tokenConfigured ? "MinerU 已连接" : "MinerU 未配置"}</span></section>
      <section className="intake-card">
        <div className="intake-main-column">
          <div className={`intake-dropzone ${dragging ? "dragging" : ""}`} onDragOver={event => { event.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); if (!("__TAURI_INTERNALS__" in window)) setMessage("浏览器无法读取拖入文件的本地路径；请点击选择文件。桌面应用支持直接拖入。"); }}>
            <div className="intake-file-symbols"><i>PDF</i><i>PDF</i><i>PDF</i></div>
            <strong>将 PDF 拖到此处</strong><span>或通过系统文件选择器批量添加</span>
            <button className="primary" onClick={chooseFiles}>＋ 选择文件</button><small>第一阶段支持 PDF · 单本超过 200 页会自动分段</small>
          </div>
          <div className="intake-queue-heading"><strong>待处理文件</strong><span>{paths.length} 个文件</span></div>
          <div className="intake-queue">{paths.length ? paths.map(path => <article key={path}><span className="file-kind">PDF</span><div><strong>{basename(path)}</strong><small>{path}</small></div><button aria-label="移除文件" onClick={() => setPaths(current => current.filter(value => value !== path))}>×</button></article>) : <div className="intake-empty">尚未选择文件</div>}</div>
        </div>
        <aside className="intake-options">
          <div className="intake-option-title"><strong>解析设置</strong><button className={tokenConfigured ? "token-button configured" : "token-button"} onClick={openTokenSettings}>🔑 {tokenConfigured ? "Token 已保存" : "设置 Token"}</button></div>
          <label>文档语言<select value={language} onChange={event => setLanguage(event.target.value)}><option value="auto">自动检测</option><option value="en">英语</option><option value="ja">日语</option><option value="fr">法语</option><option value="de">德语</option></select></label>
          <label className="option-toggle"><span><strong>OCR</strong><small>扫描件文字识别</small></span><input type="checkbox" checked={ocr} onChange={event => setOcr(event.target.checked)}/></label>
          <label className="option-toggle"><span><strong>表格识别</strong><small>保留行列结构</small></span><input type="checkbox" checked={tables} onChange={event => setTables(event.target.checked)}/></label>
          <label className="option-toggle"><span><strong>公式识别</strong><small>保留公式结构</small></span><input type="checkbox" checked={formulas} onChange={event => setFormulas(event.target.checked)}/></label>
          <button className="primary intake-start" disabled={!paths.length || busy} onClick={startJobs}>{busy ? "正在创建任务…" : `开始解析${paths.length ? `（${paths.length}）` : ""}`}</button>
          <small className="intake-privacy">文件与解析结果保存在本机；只有解析任务会发送至 MinerU。</small>
        </aside>
      </section>
      <section className="intake-progress-card">
        <header><div><h2>处理进度</h2><span>共 {jobs.length} 个任务 · {waiting} 个等待 · {working} 个处理中 · {completed} 个已完成</span></div></header>
        <div className="ocr-job-list">{jobs.length ? jobs.map(job => <OcrJobRow key={job.job_id} job={job} retry={retry} openProject={openProject}/>) : <div className="intake-empty">开始解析后，任务状态会显示在这里。</div>}</div>
      </section>
      {standaloneProjects.length > 0 && <section className="existing-projects"><h2>已有项目</h2><div>{standaloneProjects.map(project => <button key={project.project_id} onClick={() => openProject(project.project_id)}><strong>{project.title}</strong><span>{project.state}</span></button>)}</div></section>}
    </main>
    {tokenDialog && <div className="modal-backdrop" onClick={() => setTokenDialog(false)}><section className="token-dialog" onClick={event => event.stopPropagation()}><div className="panel-title"><h2>MinerU API Token</h2><button onClick={() => setTokenDialog(false)}>关闭</button></div><p>Token 只会保存到 macOS Keychain，不写入项目、日志或导出文件。</p>{tokenConfigured && !tokenEditing ? <div className="token-saved-state"><i>✓</i><div><strong>Token 已安全保存</strong><span>为保护密钥安全，Literature 不回显完整 Token。</span></div><button onClick={() => setTokenEditing(true)}>替换 Token</button></div> : <><label>MinerU Token<input autoFocus type="password" value={token} onChange={event => setToken(event.target.value)} placeholder="请输入 MinerU Token"/></label><button className="primary" disabled={!token.trim() || busy} onClick={saveToken}>{busy ? "正在保存…" : tokenConfigured ? "保存新 Token" : "保存 Token"}</button>{tokenConfigured && <button className="token-cancel-edit" onClick={() => { setToken(""); setTokenEditing(false); }}>取消替换</button>}</>}</section></div>}
    {message && <button className="toast" onClick={() => setMessage("")}>{message}</button>}
  </div>;
}

function OcrJobRow({ job, retry, openProject }: { job: OcrJob; retry: (job: OcrJob) => void; openProject: (id: string) => void }) {
  const showProgress = job.stage_progress != null && job.state !== "completed";
  return <article className={`ocr-job ocr-${job.state}`}>
    <span className="file-kind">PDF</span>
    <div className="ocr-job-content">
      <div className="ocr-job-title"><strong>{job.title}.pdf</strong><span>{stateLabel[job.state] || job.state}</span></div>
      <p>{job.page_count ? `${job.page_count} 页${job.slice_count > 1 ? ` · 已自动分为 ${job.slice_count} 段` : ""}` : formatSize(job.source_size)} · {jobDescription(job)}</p>
      {showProgress && <progress max={1} value={job.stage_progress || 0}/>}
      {job.slice_count > 1 && <details open={job.failed_slices > 0}><summary>{job.failed_slices ? `${job.failed_slices} 个分段失败` : `查看 ${job.slice_count} 个分段的进度`}</summary><div className="ocr-slices">{job.slices.map(slice => <SliceRow key={slice.index} slice={slice}/>)}</div></details>}
      {job.error && <small className="ocr-error">{job.error}</small>}
    </div>
    <div className="ocr-job-actions">{job.can_retry && <button className="primary" onClick={() => retry(job)}>重试失败分段</button>}{job.state === "completed" && <button className="primary" onClick={() => openProject(job.project_id)}>查看解析结果</button>}</div>
  </article>;
}

function SliceRow({ slice }: { slice: OcrSlice }) {
  return <div className={`ocr-slice slice-${slice.state}`}><i/>
    <span>第 {slice.index} 段 · 第 {slice.start_page}–{slice.end_page} 页</span>
    <strong>{sliceLabel[slice.state] || slice.state}</strong>
    {slice.error && <small>{slice.error}</small>}
  </div>;
}
