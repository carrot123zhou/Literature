import { useEffect, useMemo, useState } from "react";
import type { Block, GlossaryVersion, Job, ModelRoleBinding, ModelService, TocNode, TranslationEntry, TranslationEstimate, TranslationStats, UsageRecord } from "../types";
import TaskModelPicker, { type TaskModelPatch } from "./TaskModelPicker";
import type { ReasoningMode } from "./ThinkingLevelField";
import { STATE_META, TYPE_LABEL, blockKind, entryText } from "./readingModel";

/* ----------------------------- 目录 ----------------------------- */

function rangeForNode(document: { blocks: Block[]; toc: TocNode[] }, node: TocNode): Block[] {
  const start = document.blocks.findIndex(block => block.block_id === node.block_id);
  if (start < 0) return [];
  const result: Block[] = [];
  for (let index = start; index < document.blocks.length; index += 1) {
    const block = document.blocks[index];
    if (index > start && block.type === "title" && block.level <= node.level) break;
    if (block.type !== "title" || index === start) result.push(block);
  }
  return result;
}

export function TocPanel({ document, translation, checked, onToggle, onSelectBlock }: {
  document: { blocks: Block[]; toc: TocNode[] };
  translation: { entries: Record<string, TranslationEntry> } | null;
  checked: Set<string>;
  onToggle: (id: string) => void;
  onSelectBlock: (id: string) => void;
}) {
  const rows: { node: TocNode; depth: number; done: number; total: number }[] = [];
  const walk = (nodes: TocNode[], depth: number) => {
    for (const node of nodes) {
      const blocks = rangeForNode(document, node);
      const done = blocks.filter(block => {
        const state = translation?.entries[block.block_id]?.state;
        return state === "translated" || state === "refined" || state === "edited" || state === "locked";
      }).length;
      rows.push({ node, depth, done, total: blocks.length });
      walk(node.children || [], depth + 1);
    }
  };
  walk(document.toc, 0);
  if (!rows.length) return <div className="panel"><h4>目录</h4><div className="pbody"><div className="hint">该文档还没有确认的目录结构，可先在「结构校对」中整理标题。</div></div></div>;
  return <div className="panel"><h4>目录<span className="grow"/><span style={{ fontSize: 11, color: "var(--rdg-faint)" }}>{rows.length} 项</span></h4>
    <div className="pbody" style={{ gap: 3, padding: 9 }}>
      {rows.map(({ node, depth, done, total }) => {
        const pct = total ? Math.round(done / total * 100) : 0;
        return <div key={node.node_id} className={`tocitem${checked.has(node.block_id) ? " on" : ""}${depth > 0 ? " tocchild" : ""}`}>
          <input type="checkbox" checked={checked.has(node.block_id)} onChange={() => onToggle(node.block_id)} onClick={event => event.stopPropagation()} aria-label={`选择 ${node.title}`}/>
          <div className="ti" onClick={() => onSelectBlock(node.block_id)} title={node.title}>
            <b>{node.title || "（无标题）"}</b>
            <small>第 {node.page_index + 1} 页 · {done}/{total} 段</small>
          </div>
          {depth === 0 ? <div className="miniring" style={{ ["--p" as string]: `${pct}%` }}><i>{pct}%</i></div> : <span/>}
        </div>;
      })}
    </div>
  </div>;
}

/* ----------------------------- 任务 ----------------------------- */

const SCOPE_LABEL: Record<string, string> = {
  book: "全书翻译", untranslated: "仅未翻译段落", stale: "仅待复核段落",
  failed: "仅重试失败段落", blocks: "选定章节 / 段落", auto: "全书翻译",
};

export function JobPanel({ jobs, stats, usage, failed, onPause, onResume, onRetryAll, onRetryOne }: {
  jobs: Job[];
  stats: TranslationStats;
  usage: { input_tokens: number; output_tokens: number; cached_tokens?: number; reasoning_tokens?: number; estimated_cost: number };
  failed: { block_id: string; type: string; error?: string }[];
  onPause: (job: Job) => void;
  onResume: (job: Job) => void;
  onRetryAll: () => void;
  onRetryOne: (blockId: string) => void;
}) {
  const relevant = jobs.filter(job => job.kind === "translate" || job.kind === "refine");
  const job = relevant[0];
  const running = job?.status === "running";
  const paused = job?.status === "paused" || job?.status === "budget_paused";
  const scopeValue = (job?.scope as { scope?: string } | undefined)?.scope || "book";
  const stateLabel = !job ? "未启动"
    : running ? "进行中"
    : paused ? "已暂停"
    : job.status === "completed" ? "已完成"
    : job.status === "partial_failed" ? "部分失败"
    : job.status === "failed" ? "失败"
    : job.status;
  return <>
    <div className="panel"><h4>当前翻译任务<span className="grow"/><span style={{ fontSize: 11, color: running ? "var(--rdg-run)" : "var(--rdg-faint)" }}>{stateLabel}</span></h4>
      <div className="pbody">
        <div className="kv"><span>范围</span><b>{job ? (SCOPE_LABEL[scopeValue] || scopeValue) : "—"}</b></div>
        <div className="jobbar"><i style={{ width: `${job ? Math.round((job.progress || 0) * 100) : stats.percent}%` }}/></div>
        <div className="kv"><span>整书进度</span><b>{stats.done} / {stats.total} 段 · {stats.percent}%</b></div>
        <div className="kv"><span>失败段落</span><b style={{ color: stats.failed ? "var(--rdg-fail)" : "var(--rdg-ink)" }}>{stats.failed}</b></div>
        <div className="kv"><span>已用 tokens</span><b>{(usage.input_tokens + usage.output_tokens).toLocaleString()}</b></div>
        {usage.reasoning_tokens ? <div className="kv"><span>其中思考 tokens</span><b>{usage.reasoning_tokens.toLocaleString()}</b></div> : null}
        <div className="kv"><span>估算费用</span><b>${(usage.estimated_cost || 0).toFixed(4)}</b></div>
        {job?.error ? <div className="hint" style={{ color: "var(--rdg-fail)" }}>{job.error}</div> : null}
        <div className="acts">
          <button disabled={!running} onClick={() => job && onPause(job)}>⏸ 暂停</button>
          <button disabled={!paused && job?.status !== "failed" && job?.status !== "partial_failed"} onClick={() => job && onResume(job)}>{job?.status === "failed" || job?.status === "partial_failed" ? "▶ 重试失败" : "▶ 恢复"}</button>
        </div>
      </div>
    </div>
    <div className="panel"><h4>失败分块<span className="grow"/><button className="ghost" style={{ fontSize: 11 }} disabled={!stats.failed} onClick={onRetryAll}>重试全部失败</button></h4>
      <div className="pbody" style={{ gap: 0 }}>
        {!failed.length ? <div className="hint">暂无失败分块。</div> : failed.map(item => <div className="failrow" key={item.block_id}>
          <i className="fdot"/><div className="grow"><b>#{item.block_id} {TYPE_LABEL[blockKind({ type: item.type } as Block)] || item.type}</b><small>{item.error || "翻译失败或已中断"}</small></div>
          <button onClick={() => onRetryOne(item.block_id)}>重试</button>
        </div>)}
      </div>
    </div>
    <div className="panel"><h4>段落状态统计</h4><div className="pbody" style={{ gap: 7 }}>
      <div className="kv"><span>已翻译</span><b>{stats.translated + stats.refined + stats.edited}</b></div>
      <div className="kv"><span>人工锁定</span><b>{stats.locked}</b></div>
      <div className="kv"><span>待复核（原文已变）</span><b>{stats.stale_source}</b></div>
      <div className="kv"><span>已对齐（仅结构变化）</span><b>{stats.stale_structure}</b></div>
      <div className="kv"><span>失败</span><b>{stats.failed}</b></div>
      <div className="kv"><span>未翻译</span><b>{stats.untranslated}</b></div>
    </div></div>
  </>;
}

/* ----------------------------- 术语表 ----------------------------- */

export function GlossaryPanel({ glossary, versions, onFreeze, onRollback }: {
  glossary: Record<string, string>;
  versions: GlossaryVersion[];
  onFreeze: (entries: Record<string, string>) => void;
  onRollback: (version: number) => void;
}) {
  const [text, setText] = useState(() => Object.entries(glossary).map(([source, target]) => `${source}\t${target}`).join("\n"));
  useEffect(() => { setText(Object.entries(glossary).map(([source, target]) => `${source}\t${target}`).join("\n")); }, [glossary]);
  const parse = () => {
    const parsed: Record<string, string> = {};
    for (const line of text.split(/\n/)) {
      const [source, ...target] = line.split(/\t|\s*=>\s*/);
      if (source?.trim()) parsed[source.trim()] = target.join(" ").trim();
    }
    return parsed;
  };
  const currentVersion = versions.length ? versions[versions.length - 1].version : 0;
  return <>
    <div className="panel"><h4>术语表<span className="grow"/><span style={{ fontSize: 11, color: "var(--rdg-faint)" }}>草稿 → 冻结快照</span></h4>
      <div className="pbody">
        <div className="ghead">原文词条 → 目标译法（每行一条，Tab 或 =&gt; 分隔）</div>
        <textarea style={{ minHeight: 160 }} value={text} onChange={event => setText(event.target.value)} placeholder={"state of nature\t自然状态"}/>
        <div className="xnote">术语表会随任务保存为冻结快照；之后编辑不影响已运行的任务，需要时可切换到新版本。</div>
        <button className="primary" onClick={() => onFreeze(parse())}>冻结为新快照 v{currentVersion + 1}</button>
      </div>
    </div>
    <div className="panel"><h4>版本与回滚</h4><div className="pbody" style={{ gap: 0 }}>
      {!versions.length ? <div className="hint">尚未冻结任何版本。</div> : [...versions].reverse().map(item => <div className="vrow" key={item.version}>
        <b>v{item.version}</b>
        <span style={{ color: "var(--rdg-faint)" }}>{new Date(item.created_at).toLocaleString()} · {Object.keys(item.entries).length} 词条</span>
        {item.version === currentVersion ? <span className="curtag">当前使用</span> : <button onClick={() => onRollback(item.version)}>回滚</button>}
      </div>)}
    </div></div>
  </>;
}

/* ----------------------------- 当前块 ----------------------------- */

export function BlockPanel({ block, entry, chapter, onTranslateBlock, onUnlock }: {
  block?: Block;
  entry?: TranslationEntry;
  chapter: string;
  onTranslateBlock: (options?: { refine?: boolean; force?: boolean }) => void;
  onUnlock: () => void;
}) {
  if (!block) return <div className="panel"><h4>当前块</h4><div className="pbody"><div className="hint">在中间栏点选一个段落，可查看其译文依据、状态与操作，并可使用「即时翻译」（在原文中划选文字）。</div></div></div>;
  const kind = blockKind(block);
  const state = entry?.state || entry?.status || "untranslated";
  const meta = STATE_META[state] || STATE_META.translated;
  return <>
    <div className="panel"><h4>当前块</h4><div className="pbody blockinfo">
      <p className="bt">#{block.block_id} · {TYPE_LABEL[kind] || block.type}</p>
      <p className="bs">{chapter} · 依据结构 revision r{entry?.structure_revision ?? 0}</p>
      <span className={`bstate s-${meta.cls}`} style={{ justifySelf: "start" }}><i className="dot"/>{meta.label}</span>
      <div className="txt">{entryText(entry) || "（尚未翻译）"}</div>
      <div className="acts">
        {state !== "locked" && state !== "translated" && state !== "refined" && state !== "edited" ? <button className="primary" onClick={() => onTranslateBlock()}>翻译本段</button> : null}
        {state === "translated" || state === "refined" || state === "edited" || state === "stale" ? <button onClick={() => onTranslateBlock({ force: true })}>重译</button> : null}
        {state === "locked" ? <><button onClick={() => onTranslateBlock({ force: true })}>重译</button><button onClick={onUnlock}>解锁</button></> : null}
      </div>
    </div></div>
    <div className="panel"><h4>即时翻译</h4><div className="pbody"><div className="hint">在原文栏中划选任意词语或句子，点击浮出的「即时翻译」；结果作为该段附属记录保存，不覆盖整段译文。</div></div></div>
  </>;
}

/* ----------------------------- 范围弹窗 ----------------------------- */

export type ScopeKey = "book" | "untranslated" | "stale" | "failed" | "blocks";
export type ScopeConfirm = { scope: ScopeKey; useGlossary: boolean; instructions: string; refine: boolean; force: boolean; polish: boolean; workers: number; locale: string; serviceId: string; model: string; reasoningMode: ReasoningMode; reasoningBudget: number };

export function ScopeDialog({ projectTitle, locale, stats, selectedScopeCount, glossaryVersions, providers, binding, estimate, onEstimate, onInvalidateEstimate, onClose, onConfirm }: {
  projectTitle: string;
  locale: string;
  stats: TranslationStats;
  selectedScopeCount: number;
  glossaryVersions: GlossaryVersion[];
  /** 全部服务商（含模型列表），供「服务商 → 模型」两级选择。 */
  providers: ModelService[];
  /** 默认绑定 = 翻译角色当前绑定。 */
  binding: ModelRoleBinding;
  estimate: TranslationEstimate | null;
  onEstimate: (value: ScopeConfirm) => void;
  onInvalidateEstimate: () => void;
  onClose: () => void;
  onConfirm: (value: ScopeConfirm) => void;
}) {
  const [scope, setScope] = useState<ScopeKey>(stats.untranslated > 0 && stats.untranslated < stats.total ? "untranslated" : "book");
  const [target, setTarget] = useState(locale);
  const [glossaryMode, setGlossaryMode] = useState<"frozen" | "none">("frozen");
  const [instructions, setInstructions] = useState("");
  const [refine, setRefine] = useState(false);
  const [force, setForce] = useState(false);
  const [polish, setPolish] = useState(false);
  const [workers, setWorkers] = useState(8);
  const [modelValue, setModelValue] = useState<ModelRoleBinding>(binding);
  const [reasoningMode, setReasoningMode] = useState<ReasoningMode>("inherit");
  const [reasoningBudget, setReasoningBudget] = useState(0);
  const frozen = glossaryVersions.length ? glossaryVersions[glossaryVersions.length - 1].version : 0;
  // 选项变化立即失效旧估算（不需要再点一次预估）；迟到响应由 App 的代次校验丢弃。
  useEffect(() => {
    onInvalidateEstimate();
  }, [scope, target, glossaryMode, instructions, refine, force, polish, workers, modelValue.service_id, modelValue.model, reasoningMode, onInvalidateEstimate]);
  // 仅“原文已变且未锁定”的段落会真正重译；纯层级变化已自动对齐，不再入列（F04）。
  const retranslatableStale = stats.stale_source_unlocked ?? Math.max(0, stats.stale_source - stats.stale_locked);
  const options: { key: ScopeKey; title: string; desc: string; count: number | string; disabled?: boolean }[] = [
    { key: "book", title: "全书翻译", desc: `翻译全部 ${stats.total} 个可译段落，跳过已锁定的人工译文`, count: stats.total },
    { key: "untranslated", title: "仅未翻译段落", desc: `只处理尚未翻译的 ${stats.untranslated} 段，适合中断后接续`, count: stats.untranslated, disabled: !stats.untranslated },
    { key: "stale", title: "仅待复核段落", desc: `只重译原文内容已变的 ${retranslatableStale} 段；已锁定的人工译文不会被自动覆盖`, count: retranslatableStale, disabled: !retranslatableStale },
    { key: "failed", title: "仅重试失败段落", desc: `重试 ${stats.failed} 个失败分块，不重发已成功的段落`, count: stats.failed, disabled: !stats.failed },
    { key: "blocks", title: "选定章节 / 段落", desc: "先在目录中勾选范围，再启动任务", count: selectedScopeCount, disabled: !selectedScopeCount },
  ];
  const canGo = scope !== "blocks" || selectedScopeCount > 0;
  // 预估必须按当前弹窗选项（目标语言/术语/附加要求/refine/force/polish）计算，而不是只传 scope。
  const confirmValue: ScopeConfirm = { scope, useGlossary: glossaryMode === "frozen", instructions, refine, force: force && !refine, polish: polish && !refine, workers, locale: target, serviceId: modelValue.service_id, model: modelValue.model, reasoningMode, reasoningBudget };
  return <div className="rdg-pop" onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="dialog" role="dialog" aria-modal="true" aria-label="选择翻译范围">
      <div className="dialog-head">
        <span className="mono">FR</span>
        <div style={{ flex: 1 }}><h2>翻译 · {projectTitle}</h2><p className="src">选择范围与模型后开始</p></div>
        <button className="x" onClick={onClose} aria-label="关闭">✕</button>
      </div>
      <div className="dialog-body">
        <div className="grid2">
          <div className="field"><label>目标语言</label>
            <select value={target} onChange={event => setTarget(event.target.value)}>
              {[["zh-CN", "简体中文"], ["zh-TW", "繁體中文"], ["en", "English"], ["ja", "日本語"], ["fr", "Français"], ["de", "Deutsch"]].map(([value, label]) => <option key={value} value={value}>{label}（{value}）</option>)}
            </select>
          </div>
          <div className="field"><label>术语表</label>
            <select value={glossaryMode} onChange={event => setGlossaryMode(event.target.value as "frozen" | "none")}>
              <option value="frozen">{frozen ? `使用已冻结术语表 v${frozen}` : "无已冻结术语表"}</option>
              <option value="none">不使用术语表（直接开始）</option>
            </select>
          </div>
        </div>
        <div className="field"><label>翻译模型与思考水平</label>
          <TaskModelPicker
            label="翻译模型"
            providers={providers}
            value={modelValue}
            reasoningMode={reasoningMode}
            reasoningBudget={reasoningBudget}
            onChange={(patch: TaskModelPatch) => {
              if (patch.serviceId !== undefined || patch.model !== undefined) setModelValue(current => ({ service_id: patch.serviceId ?? current.service_id, model: patch.model ?? current.model }));
              if (patch.reasoningMode !== undefined) setReasoningMode(patch.reasoningMode);
              if (patch.reasoningBudget !== undefined) setReasoningBudget(patch.reasoningBudget);
            }}
          />
        </div>
        <div className="field"><label>翻译范围</label>
          <div className="radio">
            {options.map(option => <label key={option.key} className={`${scope === option.key ? "on" : ""}${option.disabled ? " dis" : ""}`}>
              <input type="radio" name="scope" checked={scope === option.key} disabled={option.disabled} onChange={() => setScope(option.key)}/>
              <span className="n"><b>{option.title}</b><small>{option.desc}</small></span>
              <span className="cnt">{option.count}</span>
            </label>)}
          </div>
        </div>
        <details className="scope-more"><summary style={{ cursor: "pointer", margin: "4px 0 12px", fontWeight: 600 }}>更多选项（附加要求、润色、重译、并发）</summary>
        <div className="field"><label>附加要求（进入任务快照）</label>
          <input value={instructions} onChange={event => setInstructions(event.target.value)} placeholder="如：保留原文人名，不译"/>
        </div>
        <label className="radio" style={{ marginBottom: 12 }}>
          <label className={refine ? "on" : ""} style={{ width: "100%" }}><input type="checkbox" checked={refine} onChange={event => setRefine(event.target.checked)}/><span className="n"><b>仅润色已有译文</b><small>跳过未翻译段落，只对已有译文做润色</small></span></label>
        </label>
        <label className="radio" style={{ marginBottom: 12 }}>
          <label className={force ? "on" : ""} style={{ width: "100%" }}><input type="checkbox" checked={force} disabled={refine} onChange={event => setForce(event.target.checked)}/><span className="n"><b>强制重译已有译文</b><small>忽略“已翻译”状态重新生成（人工锁定段落仍会跳过）；换模型或换引擎后整本重跑时勾选</small></span></label>
        </label>
        <div className="grid2">
          <div className="field"><label>并发请求数</label>
            <select value={workers} onChange={event => setWorkers(Number(event.target.value))}>
              <option value={1}>1（串行，最稳）</option>
              <option value={2}>2</option>
              <option value={4}>4</option>
              <option value={6}>6</option>
              <option value={8}>8（推荐）</option>
              <option value={12}>12</option>
              <option value={16}>16（最快，限流时自动降速）</option>
            </select>
          </div>
          <label className="radio" style={{ alignSelf: "end", marginBottom: 12 }}>
            <label className={polish && !refine ? "on" : ""} style={{ width: "100%" }}><input type="checkbox" checked={polish} disabled={refine} onChange={event => setPolish(event.target.checked)}/><span className="n"><b>翻译后自动润色</b><small>两遍制：先翻译再文学润色，质量更高，耗时与费用约翻倍</small></span></label>
          </label>
        </div>
        </details>
        <div className="xnote">校对未完成不影响翻译；翻译完成后再调整结构时，译文会<b>自动对齐到新的内容层级</b>，无需重新翻译。翻译按小块并发发送，单段失败只重试该段，不重复已成功的 usage。</div>
        {estimate ? <div className="xnote">将处理 <b>{estimate.block_count}</b> 块 · 约 {estimate.estimated_input_tokens.toLocaleString()}{(estimate.estimated_input_tokens_low != null && estimate.estimated_input_tokens_high != null) ? `（区间 ${estimate.estimated_input_tokens_low.toLocaleString()}–${estimate.estimated_input_tokens_high.toLocaleString()}）` : ""} 输入 token · {estimate.chunk_count} 个请求{estimate.has_pricing ? (estimate.estimated_cost != null ? ` · 估费 ${estimate.estimated_cost_low != null && estimate.estimated_cost_high != null ? `$${estimate.estimated_cost_low.toFixed(4)}–$${estimate.estimated_cost_high.toFixed(4)}（约 $${estimate.estimated_cost.toFixed(4)}）` : `约 $${estimate.estimated_cost.toFixed(4)}`}` : " · 价格未知，无法给出硬预算") : <span> · 单价比未配置，无法估算费用</span>}</div> : null}
      </div>
      <div className="dialog-foot">
        <button onClick={() => onEstimate(confirmValue)}>预估</button>
        <span className="grow"/>
        <button onClick={onClose}>取消</button>
        <button className="primary" disabled={!canGo} onClick={() => onConfirm(confirmValue)}>开始翻译</button>
      </div>
    </div>
  </div>;
}
