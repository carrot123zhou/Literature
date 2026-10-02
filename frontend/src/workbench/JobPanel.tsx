import type { Job } from "../types";

export default function JobPanel({ jobs, onPause, onResume }: { jobs: Job[]; onPause: (job: Job) => void; onResume: (job: Job) => void }) {
  const relevant = jobs.filter(job => job.kind === "translate" || job.kind === "refine").slice(0, 4);
  if (!relevant.length) return null;
  return <details className="job-panel" open><summary>翻译任务 · {relevant.length}</summary>{relevant.map(job => <article key={job.job_id}><div><strong>{job.kind === "refine" ? "润色" : "初译"}</strong><span>{job.status} · {Math.round(job.progress * 100)}%</span></div><progress max={1} value={job.progress}/>{job.error && <small>{job.error}</small>}<div>{job.status === "running" && <button onClick={() => onPause(job)}>暂停</button>}{(job.status === "paused" || job.status === "failed" || job.status === "budget_paused") && <button className="primary" onClick={() => onResume(job)}>{job.status === "failed" ? "重试失败分块" : job.status === "budget_paused" ? "提高预算后恢复" : "恢复"}</button>}</div></article>)}</details>;
}
