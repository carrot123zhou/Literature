import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { ArchiveSettings, OcrJob, TaskPage } from "./types";

const PAGE_SIZE = 20;

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "全部状态" },
  { value: "active", label: "当前待处理" },
  { value: "running", label: "运行中" },
  { value: "queued", label: "排队中" },
  { value: "waiting", label: "等待恢复" },
  { value: "interrupted", label: "已中断" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "archived", label: "已归档" },
];

const RETENTION_OPTIONS: { value: string; label: string; days: number | null }[] = [
  { value: "30", label: "30 天", days: 30 },
  { value: "90", label: "90 天（默认）", days: 90 },
  { value: "180", label: "180 天", days: 180 },
  { value: "forever", label: "永久保留", days: null },
];

const STATE_LABEL: Record<string, string> = {
  queued: "排队中", preparing: "准备中", uploading: "上传中", processing: "解析中",
  downloading: "下载中", merging: "合并中", completed: "本次完成", failed: "失败",
  waiting: "等待恢复", interrupted: "已中断",
};

function formatTime(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
}
function formatSize(bytes?: number) {
  if (!bytes) return "";
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
}
function taskScope(job: OcrJob) {
  const ranges = (job.requested_ranges || "").trim();
  if (ranges) return ranges;
  if (job.requested_pages && job.requested_pages.length) return `${job.requested_pages.length} 页`;
  if (job.append) return "剩余未解析页";
  const pages = job.page_count || job.source_page_count || 0;
  return pages ? `全部 ${pages} 页` : "整份文件";
}
function taskPages(job: OcrJob) {
  return job.this_task_pages || job.requested_pages?.length || job.page_count || 0;
}
function coverageLabel(job: OcrJob) {
  const coverage = job.parse_coverage;
  if (!coverage || coverage.state === "unknown") return "覆盖未知";
  if (coverage.state === "unparsed") return "尚未解析";
  if (coverage.state === "full") return `全文 ${coverage.source_page_count} 页`;
  return `已解析 ${coverage.covered_pages}/${coverage.source_page_count} 页`;
}
function archiveReasonLabel(reason?: string) {
  if (reason === "retention") return "超期自动归档";
  if (reason === "capacity") return "超量自动归档";
  if (reason === "manual") return "手动归档";
  return "已归档";
}
function canArchive(job: OcrJob) { return job.state === "completed" || job.state === "failed"; }

export default function TaskHistory({ onOpenResources, onMessage }: {
  onOpenResources: (projectId: string) => void;
  onMessage: (value: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState("recent");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<TaskPage | null>(null);
  const [settings, setSettings] = useState<ArchiveSettings | null>(null);
  const [detail, setDetail] = useState<OcrJob | null>(null);
  const [busy, setBusy] = useState("");
  const requestRef = useRef(0);

  const load = useCallback(async () => {
    const token = ++requestRef.current;
    const params = new URLSearchParams({ scope: "history", page: String(page), page_size: String(PAGE_SIZE), sort });
    if (query.trim()) params.set("query", query.trim());
    if (status) params.set("status", status);
    const value = await api<TaskPage>(`/ocr/tasks?${params.toString()}`);
    if (token === requestRef.current) setData(value);
  }, [query, status, sort, page]);

  useEffect(() => { load().catch(error => onMessage(String(error))); }, [load, onMessage]);
  useEffect(() => { api<ArchiveSettings>("/ocr/archive-settings").then(setSettings).catch(() => {}); }, []);

  const act = async (job: OcrJob, action: "archive" | "unarchive" | "retry") => {
    setBusy(`${job.job_id}:${action}`);
    try {
      if (action === "archive") {
        await api(`/ocr/jobs/${job.job_id}/archive`, { method: "POST" });
        onMessage("已归档该记录；任务与证据仍完整保留，可随时取回。");
      } else if (action === "unarchive") {
        await api(`/ocr/jobs/${job.job_id}/unarchive`, { method: "POST" });
        onMessage("已取回该记录。");
      } else {
        await api(`/ocr/jobs/${job.job_id}/retry`, { method: "POST" });
        onMessage("只重试失败分段；已经成功的分段不会重复处理。");
      }
      await load();
      if (detail && detail.job_id === job.job_id) {
        setDetail(await api<OcrJob>(`/ocr/jobs/${job.job_id}`));
      }
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
    finally { setBusy(""); }
  };

  const saveRetention = async (days: number | null) => {
    try {
      const value = await api<ArchiveSettings>("/ocr/archive-settings", {
        method: "PUT", body: JSON.stringify({ retention_days: days, max_records: settings?.max_records ?? 1000 }),
      });
      setSettings(value);
      onMessage(days == null ? "已设为永久保留：不再按时间自动归档，但常驻成功记录仍上限 1000 条。" : `已设为保留 ${days} 天；超期成功记录会转为已归档，不删除任何文件。`);
      await load();
    } catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
  };

  const openDetail = async (job: OcrJob) => {
    try { setDetail(await api<OcrJob>(`/ocr/jobs/${job.job_id}`)); }
    catch (error) { onMessage(error instanceof Error ? error.message : String(error)); }
  };

  return <main className="history-page">
    <section className="history-head">
      <span className="eyebrow">PARSE HISTORY</span>
      <h1>解析历史</h1>
      <p>每次“首次解析 / 继续解析”都是独立记录。这里展示任务自身的完成范围，以及资源当前的全书覆盖；一次完成不等于全文完成。</p>
    </section>

    <section className="history-settings">
      <div>
        <strong>成功记录保留期</strong>
        <small>超期或常驻成功记录超过 1000 条（最老优先）会自动转为“已归档”。归档只改变展示，不删除任务、原文封存、规范文档或译文。</small>
      </div>
      <div className="history-retention">
        {RETENTION_OPTIONS.map(option => <button
          key={option.value}
          className={settings?.retention_days === option.days ? "on" : ""}
          onClick={() => saveRetention(option.days)}
        >{option.label}</button>)}
      </div>
    </section>

    <section className="history-toolbar">
      <label className="history-search"><span>⌕</span><input value={query} onChange={event => { setQuery(event.target.value); setPage(1); }} placeholder="搜索文件名或任务"/></label>
      <select aria-label="状态筛选" value={status} onChange={event => { setStatus(event.target.value); setPage(1); }}>{STATUS_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select>
      <select aria-label="排序" value={sort} onChange={event => { setSort(event.target.value); setPage(1); }}><option value="recent">最近完成/更新</option><option value="oldest">最早在前</option></select>
      <span className="grow"/>
      <span className="history-total">{data ? `${data.total} 条记录` : "载入中…"}</span>
    </section>

    <section className="history-list">
      {!data ? <div className="history-empty">正在载入解析历史…</div>
        : !data.items.length ? <div className="history-empty"><strong>没有符合条件的解析记录</strong><span>调整关键词、状态筛选或保留期后再试。</span></div>
        : data.items.map(job => <article key={job.job_id} className={`history-row ${job.archived ? "is-archived" : ""}`}>
            <span className={`history-state state-${job.state}`}>{STATE_LABEL[job.state] || job.state}</span>
            <div className="history-main">
              <strong title={job.source_name || job.title}>{job.source_name || job.title}</strong>
              <small>{job.archived ? `${archiveReasonLabel(job.archive_reason)} · ` : ""}本次范围 {taskScope(job)} · 解析 {taskPages(job)} 页 · 完成 {formatTime(job.finished_at)}{job.finished_at_estimated ? "（按最后更新时间）" : ""}</small>
              <small className="history-coverage">资源当前覆盖：{coverageLabel(job)}{job.error ? ` · ${job.error}` : ""}</small>
            </div>
            <div className="history-actions">
              <button onClick={() => openDetail(job)}>查看详情</button>
              <button onClick={() => onOpenResources(job.project_id)}>打开资源</button>
              {job.state === "failed" && !job.archived && <button onClick={() => act(job, "retry")} disabled={Boolean(busy)}>{busy === `${job.job_id}:retry` ? "重试中…" : "重试失败分段"}</button>}
              {canArchive(job) && !job.archived && <button onClick={() => act(job, "archive")} disabled={Boolean(busy)}>{busy === `${job.job_id}:archive` ? "归档中…" : "归档此记录"}</button>}
              {job.archived && <button onClick={() => act(job, "unarchive")} disabled={Boolean(busy)}>{busy === `${job.job_id}:unarchive` ? "取回中…" : "取消归档"}</button>}
            </div>
          </article>)}
    </section>

    {data && data.pages > 1 && <nav className="history-pager" aria-label="分页">
      <button disabled={data.page <= 1} onClick={() => setPage(current => Math.max(1, current - 1))}>上一页</button>
      <span>第 {data.page} / {data.pages} 页</span>
      <button disabled={data.page >= data.pages} onClick={() => setPage(current => Math.min(data.pages, current + 1))}>下一页</button>
    </nav>}

    {detail && <div className="res-pop" onClick={event => { if (event.target === event.currentTarget) setDetail(null); }}>
      <section className="res-dialog res-form" role="dialog" aria-modal="true" aria-label={`解析任务详情 ${detail.job_id}`}>
        <div className="res-dialog-head" style={{ alignItems: "center" }}>
          <div style={{ flex: 1 }}><h2 style={{ fontSize: 17 }}>解析任务详情</h2><p className="src">{detail.source_name || detail.title}</p></div>
          <button className="res-iconbtn" aria-label="关闭" onClick={() => setDetail(null)}>✕</button>
        </div>
        <div className="res-dialog-body" style={{ padding: "16px 19px" }}>
          <p className="res-field"><label>任务状态</label><span>{STATE_LABEL[detail.state] || detail.state}{detail.archived ? ` · ${archiveReasonLabel(detail.archive_reason)}` : ""}</span></p>
          <p className="res-field"><label>本次范围 / 解析页数</label><span>{taskScope(detail)} · {taskPages(detail)} 页{detail.slice_count > 1 ? `（${detail.slice_count} 段）` : ""}</span></p>
          <p className="res-field"><label>资源当前覆盖</label><span>{coverageLabel(detail)}</span></p>
          <p className="res-field"><label>创建 / 完成</label><span>{formatTime(detail.created_at)} · {formatTime(detail.finished_at)}{detail.finished_at_estimated ? "（旧记录按最后更新时间）" : ""}</span></p>
          {detail.error && <p className="res-field"><label>错误</label><span className="ocr-error">{detail.error}</span></p>}
          {detail.slices.length > 0 && <div className="history-slices">{detail.slices.map(slice => <div key={slice.index} className={`history-slice slice-${slice.state}`}>
            <span>第 {slice.index} 段 · {slice.source_pages && slice.source_pages.length ? `源页 ${slice.source_pages[0]}–${slice.source_pages[slice.source_pages.length - 1]}` : `第 ${slice.start_page}–${slice.end_page} 页`}</span>
            <strong>{STATE_LABEL[slice.state] || slice.state}</strong>
          </div>)}</div>}
        </div>
        <div className="res-dialog-foot">
          <button onClick={() => onOpenResources(detail.project_id)}>打开资源</button>
          {detail.state === "failed" && <button onClick={() => act(detail, "retry")} disabled={Boolean(busy)}>重试失败分段</button>}
          {canArchive(detail) && !detail.archived && <button onClick={() => act(detail, "archive")} disabled={Boolean(busy)}>归档此记录</button>}
          {detail.archived && <button onClick={() => act(detail, "unarchive")} disabled={Boolean(busy)}>取消归档</button>}
          <span className="grow"/>
          <button onClick={() => setDetail(null)}>关闭</button>
        </div>
      </section>
    </div>}
  </main>;
}
