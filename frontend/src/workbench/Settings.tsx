import { useEffect, useState } from "react";
import { api } from "../api";
import type { DocumentGraph, ProviderConfig, UsageRecord } from "../types";

type Props = {
  /** 未打开项目时为 undefined；此时仅展示全局可用的设置项。 */
  projectId?: string;
  document?: DocumentGraph | null;
  selected: string;
  provider: ProviderConfig;
  setProvider: (value: ProviderConfig) => void;
  instructions: string;
  setInstructions: (value: string) => void;
  usageRecords: UsageRecord[];
  close: () => void;
  onUpdated: (value: DocumentGraph) => void;
  onMessage: (value: string) => void;
};

const presets: Record<string, { label: string; model: string; url: string }> = {
  openai: { label: "OpenAI", model: "gpt-5-mini", url: "https://api.openai.com/v1" },
  deepseek: { label: "DeepSeek", model: "deepseek-chat", url: "https://api.deepseek.com" },
  glm: { label: "智谱 GLM", model: "glm-4.5", url: "https://open.bigmodel.cn/api/paas/v4" },
  qwen: { label: "Qwen / 阿里云百炼", model: "qwen-plus", url: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  custom: { label: "OpenAI-compatible", model: "", url: "" },
};

/** 全局设置抽屉：未打开项目也能配置模型密钥、单价、附加要求与 TBL。 */
export default function Settings({ projectId, document, selected, provider, setProvider, instructions, setInstructions, usageRecords, close, onUpdated, onMessage }: Props) {
  const [key, setKey] = useState("");
  const [mineru, setMineru] = useState("");
  const [tblCommand, setTblCommand] = useState("");
  const [tblUrl, setTblUrl] = useState("http://127.0.0.1:5001");

  useEffect(() => { api<any>("/tbl/config").then(value => { setTblCommand(value.command || ""); setTblUrl(value.url || "http://127.0.0.1:5001"); }).catch(() => {}); }, []);
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [close]);

  const choose = (name: string) => setProvider({ ...provider, provider: name, model: presets[name].model, base_url: presets[name].url });
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
      </> : <p className="settings-hint">当前没有打开项目。模型密钥、MinerU Token 与 TBL 属于全局设置，会一直可用。</p>}
      <h3>翻译模型</h3>
      <label>服务商<select value={provider.provider} onChange={e => choose(e.target.value)}>{Object.keys(presets).map(name => <option value={name} key={name}>{presets[name].label}</option>)}</select></label>
      <label>Model ID<input value={provider.model} onChange={e => setProvider({ ...provider, model: e.target.value })}/></label>
      <label>Base URL<input value={provider.base_url} onChange={e => setProvider({ ...provider, base_url: e.target.value })}/></label>
      <label>输入单价（USD / 百万 token）<input type="number" min="0" step="0.01" value={provider.input_price_per_million ?? ""} onChange={e => setProvider({ ...provider, input_price_per_million: e.target.value ? Number(e.target.value) : undefined })}/></label>
      <label>输出单价（USD / 百万 token）<input type="number" min="0" step="0.01" value={provider.output_price_per_million ?? ""} onChange={e => setProvider({ ...provider, output_price_per_million: e.target.value ? Number(e.target.value) : undefined })}/></label>
      {document && projectId && <label>项目预算（USD，0 为不限）<input type="number" min="0" step="0.1" defaultValue={document.budget_usd || 0} onBlur={e => updateBudget(Number(e.target.value))}/></label>}
      <label>翻译附加要求<textarea rows={4} value={instructions} onChange={e => setInstructions(e.target.value)} placeholder="例如：人名保留原文括注；采用学术中文。结构与 ID 约束始终优先。"/></label>
      <label>API Key<input type="password" value={key} onChange={e => setKey(e.target.value)} placeholder="只保存到 macOS Keychain"/></label>
      <button className="primary" onClick={async () => { await api(`/providers/${provider.provider}/key`, { method: "PUT", body: JSON.stringify({ value: key }) }); setKey(""); onMessage("模型密钥已保存到 Keychain"); }}>保存模型密钥</button>
      {projectId && <>
        <hr/><h3>用量明细</h3>
        <div className="usage-ledger">{usageRecords.length === 0 ? <p>尚无模型调用。</p> : usageRecords.slice(0, 20).map(record => <article key={record.usage_id}><strong>{record.provider} · {record.model}</strong><span>{(record.input_tokens + record.output_tokens).toLocaleString()} token</span><small>{record.scope} · {record.estimated_cost == null ? "服务商未返回可估价信息" : `$${record.estimated_cost.toFixed(5)}`}</small></article>)}</div>
      </>}
      <hr/><h3>MinerU 精准解析</h3>
      <label>Token<input type="password" value={mineru} onChange={e => setMineru(e.target.value)}/></label>
      <button onClick={async () => { await api("/mineru/token", { method: "PUT", body: JSON.stringify({ value: mineru }) }); setMineru(""); onMessage("MinerU Token 已保存"); }}>保存 MinerU Token</button>
      <hr/><h3>外部 TBL</h3>
      <label>启动命令<input value={tblCommand} onChange={e => setTblCommand(e.target.value)} placeholder="/Applications/TBL.app/Contents/MacOS/TBL"/></label>
      <label>本地 URL<input value={tblUrl} onChange={e => setTblUrl(e.target.value)}/></label>
      <button onClick={async () => { await api("/tbl/config", { method: "PUT", body: JSON.stringify({ command: tblCommand, url: tblUrl }) }); onMessage("TBL 外部工具设置已保存"); }}>保存 TBL 设置</button>
    </section>
  </div>;
}
