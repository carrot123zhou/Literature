import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import LocalResources from "./LocalResources";
import ModelServicesPanel from "./ModelServicesDialog";
import type { ModelServicesApi } from "../modelServices";
import type { DocumentGraph, UsageRecord } from "../types";

export type SettingsSection = "general" | "models" | "translation" | "structure" | "parsing" | "usage" | "about";

const SECTIONS: { id: SettingsSection; label: string; hint: string }[] = [
  { id: "general", label: "通用", hint: "语言与全局行为" },
  { id: "models", label: "模型服务", hint: "服务商与模型" },
  { id: "translation", label: "翻译", hint: "目标语言与附加要求" },
  { id: "structure", label: "结构校对", hint: "默认校对档位" },
  { id: "parsing", label: "文档解析", hint: "MinerU 与本地资源" },
  { id: "usage", label: "用量与预算", hint: "调用明细与费用上限" },
  { id: "about", label: "关于", hint: "版本与运行信息" },
];

type Props = {
  /** 未打开项目时为 undefined；此时仅展示全局可用的设置项。 */
  projectId?: string;
  document?: DocumentGraph | null;
  selected: string;
  instructions: string;
  setInstructions: (value: string) => void;
  usageRecords: UsageRecord[];
  models: ModelServicesApi;
  section: SettingsSection;
  onSectionChange: (value: SettingsSection) => void;
  close: () => void;
  onUpdated: (value: DocumentGraph) => void;
  onMessage: (value: string) => void;
};

/** 全局设置：左侧分类导航 + 右侧内容（模型配置归口在「模型服务」）。 */
export default function Settings({ projectId, document, selected, instructions, setInstructions, usageRecords, models, section, onSectionChange, close, onUpdated, onMessage }: Props) {
  const [mineru, setMineru] = useState("");
  const [health, setHealth] = useState<{ runtime_revision?: string; capabilities?: string[] } | null>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [close]);

  useEffect(() => {
    if (section !== "about" || health) return;
    api<{ runtime_revision?: string; capabilities?: string[] }>("/health").then(setHealth).catch(() => setHealth({}));
  }, [section, health]);

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
  const needsProject = (body: ReactNode) => projectId
    ? body
    : <p className="settings-hint">这一项属于当前任务：请先在「文件资源」打开一个任务，再回来调整。</p>;

  return <div className="modal-backdrop settings-backdrop" onClick={close}>
    <section className="settings settings-v2" role="dialog" aria-modal="true" aria-label="设置" onClick={e => e.stopPropagation()}>
      <header className="settings-head">
        <h2>设置</h2>
        <button onClick={close}>关闭</button>
      </header>
      <div className="settings-body">
        <nav className="settings-nav" aria-label="设置分类">
          {SECTIONS.map(item => <button key={item.id} className={section === item.id ? "active" : ""} onClick={() => onSectionChange(item.id)}>
            <strong>{item.label}</strong><small>{item.hint}</small>
          </button>)}
        </nav>
        <div className="settings-content">
          {section === "general" && <>
            <h3>语言</h3>
            {needsProject(<>
              <label>全局源语言<select value={document!.source_language_mode || document!.source_language} onChange={e => updateLanguage(e.target.value)}><option value="auto">自动检测（当前 {document!.source_language}）</option><option value="en">英语</option><option value="ja">日语</option><option value="fr">法语</option><option value="de">德语</option></select></label>
              {selectedTitle && <label>当前章节覆盖<select value={document!.chapter_language_overrides[selected] || ""} onChange={e => updateLanguage(document!.source_language_mode || document!.source_language, e.target.value)}><option value="">继承全局</option><option value="en">英语</option><option value="ja">日语</option><option value="fr">法语</option><option value="de">德语</option></select></label>}
            </>)}
            <p className="settings-hint">源语言只影响解析与翻译提示，不改动原文。目标语言在每次「开始翻译」时选择。</p>
          </>}

          {section === "models" && <>
            <h3>模型服务商</h3>
            <p className="settings-hint">一个服务商 = 一个基础 URL + 一个 API Key，下面挂多个模型。模型与上下文从服务商的 /v1/models 自动读取；API Key 只保存在本机 Keychain。结构校对与翻译的默认模型在下方绑定，单个任务仍可临时改。</p>
            <ModelServicesPanel api={models} onMessage={onMessage}/>
          </>}

          {section === "translation" && <>
            <h3>翻译</h3>
            <label>附加要求<textarea rows={5} value={instructions} onChange={e => setInstructions(e.target.value)} placeholder="例如：人名保留原文括注；采用学术中文。结构与 ID 约束始终优先。"/></label>
            <p className="settings-hint">附加要求会进入每个翻译任务快照；默认目标语言、范围与并发在「开始翻译」弹窗里按次选择。</p>
          </>}

          {section === "structure" && <>
            <h3>结构校对</h3>
            <p className="settings-hint">校对档位（粗校 / 精校）、是否只跑一遍、是否使用视觉，都在「结构校对」页启动时选择，不入全局设置。这里只保留默认模型绑定。</p>
            <p className="settings-hint">默认结构校对模型：{models.entryFor("structure") ? `${models.serviceFor("structure")?.name ?? ""} · ${models.entryFor("structure")?.name || models.entryFor("structure")?.model}` : "未绑定（可到「模型服务」里设置）"}。</p>
          </>}

          {section === "parsing" && <>
            <h3>文档解析</h3>
            <LocalResources onMessage={onMessage}/>
            <hr/>
            <h3>MinerU 精准解析</h3>
            <p className="settings-hint">用于扫描版 PDF 的版面/公式/表格识别；Token 属于全局设置，一直可用。</p>
            <label>Token<input type="password" value={mineru} onChange={e => setMineru(e.target.value)}/></label>
            <button onClick={async () => { await api("/mineru/token", { method: "PUT", body: JSON.stringify({ value: mineru }) }); setMineru(""); onMessage("MinerU Token 已保存"); }}>保存 MinerU Token</button>
          </>}

          {section === "usage" && <>
            <h3>用量与预算</h3>
            {needsProject(<label>任务预算（USD，0 为不限）<input type="number" min="0" step="0.1" defaultValue={document?.budget_usd || 0} onBlur={e => updateBudget(Number(e.target.value))}/></label>)}
            <hr/>
            <h3>用量明细</h3>
            <div className="usage-ledger">{usageRecords.length === 0 ? <p>尚无模型调用。</p> : usageRecords.slice(0, 50).map(record => <article key={record.usage_id}><strong>{record.provider} · {record.model}</strong><span>{(record.input_tokens + record.output_tokens).toLocaleString()} token</span><small>{record.scope} · {record.estimated_cost == null ? "服务商未返回可估价信息" : `$${record.estimated_cost.toFixed(5)}`}</small></article>)}</div>
          </>}

          {section === "about" && <>
            <h3>关于 Literature</h3>
            <p className="settings-hint">本地优先的书籍结构化与翻译工作台。解析由本地 MinerU 服务完成，译文与结构修改全部保存在本机。</p>
            <label>后端修订<input readOnly value={health?.runtime_revision || "读取中…"}/></label>
            <p className="settings-hint">能力标记：{(health?.capabilities || []).join("、") || "读取中…"}</p>
            <p className="settings-hint">运行日志目录：~/Library/Application Support/Literature/logs</p>
          </>}
        </div>
      </div>
    </section>
  </div>;
}
