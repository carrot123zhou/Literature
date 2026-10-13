import { useEffect, useState } from "react";
import { api } from "../api";
import LocalResources from "./LocalResources";
import type { DocumentGraph, UsageRecord } from "../types";

type Props = {
  /** 未打开项目时为 undefined；此时仅展示全局可用的设置项。 */
  projectId?: string;
  document?: DocumentGraph | null;
  selected: string;
  instructions: string;
  setInstructions: (value: string) => void;
  usageRecords: UsageRecord[];
  close: () => void;
  onUpdated: (value: DocumentGraph) => void;
  onMessage: (value: string) => void;
  /** 打开模型服务库（任务角色模型在功能页各自绑定）。 */
  onOpenModels: () => void;
};

/** 全局设置抽屉：语言、预算、附加要求、用量与 MinerU；模型配置在模型服务库。 */
export default function Settings({ projectId, document, selected, instructions, setInstructions, usageRecords, close, onUpdated, onMessage, onOpenModels }: Props) {
  const [mineru, setMineru] = useState("");

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [close]);

  const updateLanguage = async (source: string, chapterLanguage?: string) => {
    if (!projectId || !document) return;
    const payload: any = { source_language: source };
    if (chapterLanguage !== undefined && selected) {
      const overrides = { ...document.chapter_language_overrides };
      if (chapterLanguage) overrides[selected] = chapterLanguage; else delete overrides[selected];
      payload.chapter_language_overrides = overrides;
    }
    const graph = await api<DocumentGraph>(`/projects/${projectId}`, { method: "PATCH", body: JSON.stringify(payload) });
    onUpdated(graph);
    onMessage("语言设置已更新");
  };
  const updateBudget = async (value: number) => {
    if (!projectId) return;
    const graph = await api<DocumentGraph>(`/projects/${projectId}`, { method: "PATCH", body: JSON.stringify({ budget_usd: Math.max(0, value || 0) }) });
    onUpdated(graph);
    onMessage(value > 0 ? `项目预算设为 $${value.toFixed(2)}` : "项目预算限制已关闭");
  };
  const selectedTitle = document?.blocks.find(block => block.block_id === selected && block.type === "title");

  return <div className="modal-backdrop" onClick={close}>
    <section className="settings" role="dialog" aria-modal="true" aria-label="设置" onClick={e => e.stopPropagation()}>
      <div className="panel-title"><h2>设置</h2><button onClick={close}>关闭</button></div>
      {document && projectId ? <>
        <h3>语言</h3>
        <label>全局源语言<select value={document.source_language_mode || document.source_language} onChange={e => updateLanguage(e.target.value)}><option value="auto">自动检测（当前 {document.source_language}）</option><option value="en">英语</option><option value="ja">日语</option><option value="fr">法语</option><option value="de">德语</option></select></label>
        {selectedTitle && <label>当前章节覆盖<select value={document.chapter_language_overrides[selected] || ""} onChange={e => updateLanguage(document.source_language_mode || document.source_language, e.target.value)}><option value="">继承全局</option><option value="en">英语</option><option value="ja">日语</option><option value="fr">法语</option><option value="de">德语</option></select></label>}
        <hr/>
      </> : <p className="settings-hint">当前没有打开项目。MinerU Token 属于全局设置，会一直可用。</p>}
      <LocalResources onMessage={onMessage}/><hr/><h3>模型</h3>
      <p className="settings-hint">结构校对与翻译各自在对应功能页绑定模型服务；密钥只保存在本机 Keychain。</p>
      <button onClick={onOpenModels}>打开模型服务设置</button>
      {document && projectId && <label>项目预算（USD，0 为不限）<input type="number" min="0" step="0.1" defaultValue={document.budget_usd || 0} onBlur={e => updateBudget(Number(e.target.value))}/></label>}
      <label>翻译附加要求<textarea rows={4} value={instructions} onChange={e => setInstructions(e.target.value)} placeholder="例如：人名保留原文括注；采用学术中文。结构与 ID 约束始终优先。"/></label>
      {projectId && <>
        <hr/><h3>用量明细</h3>
        <div className="usage-ledger">{usageRecords.length === 0 ? <p>尚无模型调用。</p> : usageRecords.slice(0, 20).map(record => <article key={record.usage_id}><strong>{record.provider} · {record.model}</strong><span>{(record.input_tokens + record.output_tokens).toLocaleString()} token</span><small>{record.scope} · {record.estimated_cost == null ? "服务商未返回可估价信息" : `$${record.estimated_cost.toFixed(5)}`}</small></article>)}</div>
      </>}
      <hr/><h3>MinerU 精准解析</h3>
      <label>Token<input type="password" value={mineru} onChange={e => setMineru(e.target.value)}/></label>
      <button onClick={async () => { await api("/mineru/token", { method: "PUT", body: JSON.stringify({ value: mineru }) }); setMineru(""); onMessage("MinerU Token 已保存"); }}>保存 MinerU Token</button>
    </section>
  </div>;
}
