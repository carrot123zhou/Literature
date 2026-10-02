import { useState } from "react";
import { api } from "../api";

export default function ExportPanel({ projectId, onMessage }: { projectId: string; onMessage: (value: string) => void }) {
  const [mode, setMode] = useState("translation");
  const [formats, setFormats] = useState(new Set(["html", "md+json", "epub", "docx"]));
  const run = async () => { const value = await api<Record<string, string>>(`/projects/${projectId}/exports`, { method: "POST", body: JSON.stringify({ mode, locale: "zh-CN", formats: [...formats] }) }); onMessage(`已导出：${Object.keys(value).join("、")}`); };
  const tbl = async () => { const value = await api<any>(`/projects/${projectId}/tbl/prepare`, { method: "POST", body: JSON.stringify({ format: "epub", mode: "source" }) }); onMessage(`TBL 接力文件：${value.input}`); };
  const startTbl = async () => { const value = await api<any>("/tbl/start", { method: "POST" }); onMessage(value.state === "running" ? `TBL 已启动：${value.url}` : value.message || `TBL 状态：${value.state}`); };
  return <main className="export-center"><section><span className="eyebrow">EXPORT CENTER</span><h2>从同一份文档图谱生成成品</h2><p>导出不会修改原始 OCR、校对补丁或译文版本。</p></section><div className="export-options"><label>内容<select value={mode} onChange={e => setMode(e.target.value)}><option value="translation">纯译文</option><option value="bilingual">逐段双语</option><option value="source">校对原文</option></select></label><div className="format-grid">{["epub", "docx", "html", "md+json"].map(value => <label key={value}><input type="checkbox" checked={formats.has(value)} onChange={() => setFormats(old => { const next = new Set(old); next.has(value) ? next.delete(value) : next.add(value); return next; })}/><strong>{value.toUpperCase()}</strong><small>{value === "epub" ? "电子书目录与资源" : value === "docx" ? "可继续编辑" : "结构化离线输出"}</small></label>)}</div><button className="primary" onClick={run}>生成所选格式</button><button onClick={tbl}>准备 EPUB 并交给外部 TBL</button><button onClick={startTbl}>启动 / 查看外部 TBL</button></div></main>;
}
