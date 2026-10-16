import { useMemo, useState } from "react";
import type { Project } from "../types";

type Props = {
  projects: Project[];
  onOpen: (projectId: string) => void;
  onUpload: () => void;
  hint?: string;
};

/** 模块空白页：未打开任务时提供一个简单的已有资源列表与导入入口。 */
export default function TaskPicker({ projects, onOpen, onUpload, hint }: Props) {
  const [query, setQuery] = useState("");
  const ready = useMemo(() => projects.filter(project => project.has_document), [projects]);
  const processing = projects.length - ready.length;
  const filtered = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase();
    if (!normalized) return ready;
    return ready.filter(project => `${project.title} ${project.source_name || ""}`.toLocaleLowerCase().includes(normalized));
  }, [ready, query]);

  return <div className="workspace-state task-picker task-picker-v3 u-card">
    <span className="eyebrow">WORKSPACE</span>
    <h2>还没有打开任务</h2>
    <p>{hint || "从下面的资源里选择一个已完成处理的文档，或去首页导入新文件。"}</p>
    <label className="task-picker-search"><span>⌕</span><input autoFocus value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索已有文档"/></label>
    <div className="task-picker-list">
      {filtered.length
        ? filtered.map(project => <button key={project.project_id} className="task-picker-item" onClick={() => onOpen(project.project_id)}>
            <strong>{project.title}</strong>
            <small>{project.source_name || "本地文件"}</small>
          </button>)
        : <p className="empty-note">{ready.length ? "没有匹配的文档。" : "资源里还没有已完成处理的文档。"}</p>}
    </div>
    <div className="task-picker-actions">
      <button className="primary" onClick={onUpload}>去首页导入新文件</button>
      {processing > 0 && <span className="empty-note">另有 {processing} 个文档正在处理中。</span>}
    </div>
  </div>;
}
