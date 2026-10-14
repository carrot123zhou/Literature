import { memo, useEffect, useState } from "react";
import TaskModelPicker, { type TaskModelPatch } from "../workbench/TaskModelPicker";
import type { ReasoningMode } from "../workbench/ThinkingLevelField";
import type { DocumentGraph, Job, ModelRoleBinding, ModelService, StructurePatch, StructureSuggestion, StructureTocEntry, TocNode } from "../types";
import { flattenToc, formatTocDraft, parseTocDraft } from "./projection";

export type TocCandidate = { page_index: number; page_number: number; confidence: number; reasons: string[] };
export type StructureMode = "coarse" | "fine";
export type StructureOptions = { extractToc: boolean; proofread: boolean; autoApply: boolean; useVision: boolean; requestTimeoutSeconds: number; reviewMode: "sample" | "full" | "off"; tocExtraction: "auto" | "local" | "model"; proofreadConcurrency: number; planningMode: "global" | "adaptive" };
/** 档位派生的服务端 preset：主流程只传 mode；下列字段仅当高级选项里被显式改动时才随请求覆盖。 */
export const MODE_PRESET: Record<StructureMode, Omit<StructureOptions, "requestTimeoutSeconds" | "proofreadConcurrency">> = {
  coarse: { extractToc: true, proofread: true, autoApply: true, useVision: false, reviewMode: "off", tocExtraction: "auto", planningMode: "global" },
  fine: { extractToc: true, proofread: true, autoApply: true, useVision: false, reviewMode: "off", tocExtraction: "auto", planningMode: "global" },
};
export const MODE_HINTS: Record<StructureMode, string> = {
  coarse: "以原书目录为基准：剔除误标标题、补齐漏标标题、按目录父子关系校准层级。快、省 token。",
  fine: "包含粗校的全部内容，额外发现目录未收录的小节，并复核整棵标题树的父子关系。更慢、消耗更多 token。",
};
export type StructureAnalysis = { job: Job | null; running: boolean; paused: boolean; mode: StructureMode; options: StructureOptions; proposedToc: StructureTocEntry[] | null };
type Props = {
  document: DocumentGraph; tocText: string; onTocText: (value: string) => void;
  tocCandidates: TocCandidate[]; onFindTocPages: () => void; onJumpPage: (page: number) => void;
  pageText: string; onPageText: (value: string) => void;
  onAnalyze: () => void; onPreviewAudit: () => void; onReadLocalToc: () => void;
  onPreviewActualAudit: () => void;
  suggestions: StructureSuggestion[]; patches: StructurePatch[]; selected: string; activeBlock: string;
  onSelect: (blockId: string) => void; onConfirm: (suggestion: StructureSuggestion) => void;
  onDismiss: (suggestion: StructureSuggestion) => void; onConfirmAll: (values: StructureSuggestion[]) => void;
  onRevert: (patchId: string) => void; onRevertAll: () => void;
  busy: boolean;
  /** 全部服务商（含模型列表）与当前任务级选择；默认 = 角色绑定。 */
  providers: ModelService[]; modelValue: ModelRoleBinding; configured: boolean;
  onConfigureModel: () => void; analysis: StructureAnalysis;
  onOptions: (patch: Partial<StructureOptions>) => void; onMode: (mode: StructureMode) => void;
  reasoningMode: ReasoningMode; reasoningBudget: number;
  onModelChange: (patch: TaskModelPatch) => void;
  onRunModel: () => void; onPause: () => void;
  onAdoptProposed: (entries: StructureTocEntry[]) => void;
  onConfirmReview: () => void;
};
function TocRow({ node, selected, activeBlock, onSelect }: { node: TocNode; selected: string; activeBlock: string; onSelect: (id: string) => void }) {
  return <li>
    <div className={`toc-row${selected === node.block_id ? " selected" : ""}${activeBlock === node.block_id ? " active" : ""}`}>
      <span className="toc-level">H{node.level}</span>
      <button className="toc-title" onClick={() => onSelect(node.block_id)} title={node.title}>{node.title}</button>
      <span className="toc-page">{node.page_index + 1}</span>
    </div>
    {node.children.length > 0 && <ul>{node.children.map(child => <TocRow key={child.node_id} node={child} selected={selected} activeBlock={activeBlock} onSelect={onSelect}/>)}</ul>}
  </li>;
}
function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
}
const SCOPE_LABELS: Record<string, string> = { structure_extract: "识别目录", structure_outline: "规划标题树", structure_proofread: "复核标题" };
function stageLabel(scope?: string): string { return (scope && SCOPE_LABELS[scope]) || "模型请求"; }
function targetLabel(value: unknown): string {
  const labels: Record<string, string> = { title: "标题", paragraph: "正文", list: "列表", footnote: "脚注" };
  if (typeof value === "object" && value) {
    const after = value as { type?: string; level?: number };
    if (after.type === "title") return `H${after.level}`;
    return after.type ? (labels[after.type] || after.type) : "正文";
  }
  if (typeof value === "number") return `H${value}`;
  return labels[String(value)] || String(value);
}
export default memo(function OutlinePane(props: Props) {
  const { document, tocText, onTocText, tocCandidates, onFindTocPages, onJumpPage, pageText, onPageText, onAnalyze, onPreviewAudit, onReadLocalToc, suggestions, patches, selected, activeBlock, onSelect, onConfirm, onDismiss, onConfirmAll, onRevert, onRevertAll, busy, providers, modelValue, configured, onConfigureModel, analysis, onOptions, onMode, reasoningMode, reasoningBudget, onModelChange, onRunModel, onPause, onAdoptProposed, onConfirmReview } = props;
  const [queue, setQueue] = useState("all");
  const allPending = suggestions.filter(value => !value.automatic);
  const group = (value: StructureSuggestion) => value.operation === "set_type" ? "cleanup" : value.operation === "set_title" ? "promotion" : "level";
  const pending = allPending.filter(value => queue === "all" || group(value) === queue);
  const live = patches.filter(value => !value.reverted_at);
  const { job, running, paused, mode, options, proposedToc } = analysis;
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    setClock(Date.now());
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const [draftText, setDraftText] = useState("");
  useEffect(() => { setDraftText(proposedToc ? formatTocDraft(proposedToc) : ""); }, [proposedToc]);
  const draftEntries = parseTocDraft(draftText);
  const invalidDraftLines = draftText.split("\n").filter(line => line.trim() && !parseTocDraft(line).length).length;
  const blocked = busy || running;
  const textFor = (id: string) => document.blocks.find(block => block.block_id === id)?.source_text || "结构块";
  const reconciliation = (job?.result as { reconciliation?: Record<string, number> } | undefined)?.reconciliation;
  const skippedModel = (job?.result as { audit?: { skipped_model_suggestions?: number } } | undefined)?.audit?.skipped_model_suggestions ?? 0;
  const coverage = (job?.result as { review_coverage?: { reviewed: number; candidates: number; needs_review: number; reused?: number; second_pass?: number } } | undefined)?.review_coverage;
  const reviewSelection = (job?.result as { review_selection?: { review_mode?: string; qa?: { checked: number; agree: number; disagree: number; agreement?: number | null } | null } } | undefined)?.review_selection;
  const qa = reviewSelection?.qa || null;
  const tocExtraction = (job?.result as { toc_extraction?: string } | undefined)?.toc_extraction;
  const jobAudit = (job?.result as { audit?: { requests?: Array<{ scope?: string; status?: string; started_at?: string }>; planning?: { mode?: string; fallback?: boolean; fallback_reason?: string; framework?: { state?: string }; reliable_regions?: number; ambiguous_regions?: number; requests?: number; local_upgrades?: number; missing?: number; overlap_ignored?: number } } } | undefined)?.audit;
  const planning = jobAudit?.planning;
  const requests = jobAudit?.requests ?? [];
  const createdMs = job?.created_at ? Date.parse(job.created_at) : NaN;
  const totalElapsed = running && Number.isFinite(createdMs) ? Math.max(0, Math.round((clock - createdMs) / 1000)) : null;
  const inFlight = [...requests].reverse().find(item => item.status === "started" && item.started_at);
  const inFlightMs = inFlight?.started_at ? Date.parse(inFlight.started_at) : NaN;
  const inFlightElapsed = running && Number.isFinite(inFlightMs) ? Math.max(0, Math.round((clock - inFlightMs) / 1000)) : null;
  const hierarchy = (job?.result as { hierarchy_review?: { checked: number; headings: number }; hierarchy_plan?: { profile?: { toc_scope?: string; root_convention?: string } } } | undefined);
  const baselineCount = parseTocDraft(tocText).length;
  // 一行摘要：优先读 A/B/C 归类计数（result.categories），旧后端回落到 reconciliation 字段。
  const categories = (job?.result as { categories?: { removed?: number; added?: number; level?: number } } | undefined)?.categories;
  const summaryParts: string[] = [];
  if (reconciliation?.toc_entries !== undefined) summaryParts.push(`目录 ${reconciliation.toc_entries}`);
  if (categories) summaryParts.push(`剔除误标 ${categories.removed ?? 0}`, `补齐漏标 ${categories.added ?? 0}`, `层级修正 ${categories.level ?? 0}`);
  else if (reconciliation) {
    if (reconciliation.promotions !== undefined) summaryParts.push(`补齐漏标 ${reconciliation.promotions}`);
    if (reconciliation.level_corrections !== undefined) summaryParts.push(`层级修正 ${reconciliation.level_corrections}`);
  }
  const summary = summaryParts.length ? summaryParts.join(" · ") : null;
  return <aside className="sidebar mineru-control-column review-outline">
    <div className="panel-title"><strong>文档目录</strong><span>{flattenToc(document.toc).length} 个标题</span></div>
    <div className="pane-scroll">
      <details className="outline-section review-assistant" open>
        <summary>AI 校准</summary>
        <TaskModelPicker
          label="校对模型"
          providers={providers}
          value={modelValue}
          reasoningMode={reasoningMode}
          reasoningBudget={reasoningBudget}
          onConfigure={onConfigureModel}
          onChange={onModelChange}
        />
        <div className="calib-modes" role="radiogroup" aria-label="校准档位">
          {(["coarse", "fine"] as StructureMode[]).map(value =>
            <button key={value} type="button" role="radio" aria-checked={mode === value} className={mode === value ? "active" : ""} disabled={blocked || paused} onClick={() => onMode(value)}>{value === "coarse" ? "粗校" : "精校"}</button>)}
        </div>
        <p className="calib-mode-hint">{MODE_HINTS[mode]}</p>
        {/* 运行中主按钮变「暂停」必须保持可点；档位/基准约束只在未运行时拦截。 */}
        <button className="primary block-button" disabled={busy || (!running && (!configured || (!options.extractToc && !baselineCount)))}
          onClick={() => running ? onPause() : onRunModel()}>
          {running ? "暂停" : paused ? "继续校准" : "开始校准"}
        </button>
        {!configured && <small className="review-hint">先选择并配置校对模型；校准结果自动应用，可随时在修订历史撤销。</small>}
        {job && <div className={`structure-run-panel${job.status === "failed" ? " is-failed" : ""}${paused ? " is-paused" : ""}`} role="status">
          <strong>{running ? "正在校准" : paused ? "已暂停" : job.status === "failed" ? "校准失败" : "校准完成"}</strong>
          <progress max={1} value={job.progress || 0}/>
          {running && totalElapsed !== null && <p>已用时 {formatDuration(totalElapsed)}{inFlight && inFlightElapsed !== null ? ` · ${stageLabel(inFlight.scope)}进行中 ${formatDuration(inFlightElapsed)}` : ""}</p>}
          {paused && <p>已暂停：已完成的请求会保留，点「继续校准」只补剩余部分。</p>}
          {job.status === "failed"
            ? <p role="alert" className="review-error">{job.error || job.message}</p>
            : <p>{job.message}</p>}
          {summary && <p className="reconciliation-summary">{summary}</p>}
          <details><summary>详情</summary>
            {coverage && <p>候选 {coverage.candidates} 个 · 第一遍规划定案 {coverage.reused ?? 0} 个 · 第二遍必须定案 {coverage.second_pass ?? 0} 个{coverage.needs_review > 0 ? `，${coverage.needs_review} 个仍需核对` : ""}{qa && `，抽查 ${qa.checked} 项一致性 ${qa.agree}/${qa.checked}`}。</p>}
            {tocExtraction === "local" && <p className="review-hint">目录来自本地解析（有显式目录标记页），层级按编号与标题词推断。</p>}
            {planning?.mode === "adaptive" && <p className="review-hint">规划路径：{planning.fallback ? `回退整体串行（${planning.fallback_reason || "框架不可靠"}）` : `自适应分支 · 框架 ${planning.framework?.state ?? "?"} · 可靠区域 ${planning.reliable_regions ?? 0} · 歧义区域 ${planning.ambiguous_regions ?? 0} · 局部升级 ${planning.local_upgrades ?? 0} · 遗漏 ${planning.missing ?? 0} · 重叠只读 ${planning.overlap_ignored ?? 0}`}</p>}
            {planning?.mode === "global" && <p className="review-hint">规划路径：整体串行。</p>}
            {hierarchy?.hierarchy_review?.headings !== undefined && <p>父子关系检查通过 {hierarchy.hierarchy_review.checked}/{hierarchy.hierarchy_review.headings} 个标题。</p>}
            {skippedModel > 0 && <p className="review-hint">有 {skippedModel} 项未通过角色或父子关系检查，未应用。</p>}
            <ul className="structure-activity">{(job.activity ?? []).map((item, index) => <li key={index}>{item.text}</li>)}</ul>
          </details>
        </div>}
        <details className="review-advanced">
          <summary>高级选项</summary>
          <label className="review-baseline-mode">目录基准
            <select aria-label="目录基准来源" value={options.extractToc ? "source" : "confirmed"} disabled={blocked} onChange={event => onOptions({ extractToc: event.target.value === "source" })}>
              <option value="source">自动识别印刷目录</option>
              <option value="confirmed">使用已确认的目录</option>
            </select>
          </label>
          {!options.extractToc && <div className="review-page-picker review-manual-baseline">
            <label>已确认目录 · {baselineCount} 条<textarea aria-label="已确认的目录基准" value={tocText} disabled={blocked} onChange={event => onTocText(event.target.value)} rows={6} spellCheck={false} placeholder={"# 第一章\n## 第一节"}/></label>
            <small>每行用 # 至 ###### 表示层级。此模式不需要目录页；没有原书目录时可自行填写。</small>
            {tocText.trim() && !baselineCount && <small role="alert">尚未识别到目录条目，请按“# 标题”格式填写。</small>}
          </div>}
          {options.extractToc && <div className="review-page-picker">
            <label>限定目录页（留空自动识别）<input aria-label="源文件目录页" value={pageText} disabled={blocked} onChange={event => onPageText(event.target.value)} placeholder="例如 3-5, 7"/></label>
            <button className="link-button" disabled={blocked} onClick={onFindTocPages}>查找目录页</button>
            {tocCandidates.length > 0 && <div className="toc-candidates">{tocCandidates.slice(0, 8).map(candidate => <button key={candidate.page_index} disabled={blocked} title={candidate.reasons.join(" · ")} onClick={() => onJumpPage(candidate.page_number)}>查看第 {candidate.page_number} 页</button>)}</div>}
            <small>自动定位后有显式目录标记页就直接采信本地解析（省一次模型请求）。</small>
          </div>}
          <button className="link-button block-button" onClick={onReadLocalToc} disabled={blocked}>本地读取目录 · 0 费用</button>
          <button className="block-button" onClick={onAnalyze} disabled={blocked}>只做本地校准 · 无模型费用</button>
          <button className="link-button block-button" onClick={onPreviewAudit} disabled={blocked}>预览启动输入</button>
          <label className="structure-option"><input type="checkbox" checked={options.useVision} disabled={blocked} onChange={() => onOptions({ useVision: !options.useVision })}/>对照原页图片 <small>需视觉模型</small></label>
          <label className="structure-option"><input type="checkbox" checked={options.autoApply} disabled={blocked} onChange={() => onOptions({ autoApply: !options.autoApply })}/>自动采用高置信修正</label>
          <label>单次模型响应等待（秒）<input aria-label="单次模型响应等待（秒）" type="number" min={30} max={1800} value={options.requestTimeoutSeconds} disabled={blocked} onChange={event => onOptions({ requestTimeoutSeconds: Number(event.target.value) })}/></label>
          <label>复核模式<select aria-label="复核模式" value={options.reviewMode} disabled={blocked} onChange={event => onOptions({ reviewMode: event.target.value === "full" ? "full" : event.target.value === "off" ? "off" : "sample" })}><option value="off">只跑第一遍（默认）</option><option value="sample">定案 + 抽查</option><option value="full">全量双遍（更慢）</option></select></label>
          <label>目录来源<select aria-label="目录来源" value={options.tocExtraction} disabled={blocked} onChange={event => onOptions({ tocExtraction: event.target.value === "model" ? "model" : event.target.value === "local" ? "local" : "auto" })}><option value="auto">有目录标记时用本地（默认）</option><option value="local">总是用本地解析</option><option value="model">总是让模型核对</option></select></label>
          <label>规划策略<select aria-label="规划策略" value={options.planningMode} disabled={blocked} onChange={event => onOptions({ planningMode: event.target.value === "adaptive" ? "adaptive" : "global" })}><option value="global">整体串行（默认）</option><option value="adaptive">自适应分支（试用）</option></select></label>
          <label>复核并发（1–4 路）<input aria-label="复核并发" type="number" min={1} max={4} value={options.proofreadConcurrency} disabled={blocked} onChange={event => onOptions({ proofreadConcurrency: Math.max(1, Math.min(4, Number(event.target.value) || 1)) })}/></label>
          <button className="block-button" onClick={onConfirmReview} disabled={blocked}>确认当前审查范围（全书）</button>
          <small>高级选项改动后按当前档位覆盖预设；主流程只按「粗校/精校」运行。自适应分支质量未经真实 A/B，属试用。校准修改默认自动应用，全部可撤销。</small>
          {proposedToc?.length ? <details className="review-baseline" open><summary>待确认目录草稿 · {draftEntries.length} 条</summary><p className="review-hint">请对照原目录编辑文字与层级，每行用 # 至 ###### 表示层级；未定位条目也可能超出当前解析页范围。</p><textarea aria-label="待确认目录草稿" value={draftText} disabled={blocked} onChange={event => setDraftText(event.target.value)} rows={8} spellCheck={false}/>{invalidDraftLines > 0 && <p role="alert">有 {invalidDraftLines} 行格式无效，请按“# 标题”格式修改后确认。</p>}<button className="block-button" disabled={blocked || !draftEntries.length || invalidDraftLines > 0} onClick={() => onAdoptProposed(draftEntries)}>确认目录并保存基准</button></details> : null}
          {job?.result && <button className="link-button block-button" onClick={props.onPreviewActualAudit}>查看实际模型请求</button>}
        </details>
      </details>
      {allPending.length > 0 && <details className="outline-section structure-queue" open>
        <summary>待确认 · {allPending.length}</summary>
        <select aria-label="筛选校准建议" value={queue} onChange={event => setQueue(event.target.value)}>
          {[["all", "全部"], ["cleanup", "清理误标"], ["level", "调整层级"], ["promotion", "补回标题"]].map(([key, label]) => <option key={key} value={key}>{label} · {allPending.filter(value => key === "all" || group(value) === key).length}</option>)}
        </select>
        <button className="block-button" disabled={blocked || !pending.length} onClick={() => onConfirmAll(pending)}>采用当前 {pending.length} 项</button>
        {pending.map(value => {
          const block = document.blocks.find(item => item.block_id === value.block_id);
          const page = block ? `第 ${block.page_index + 1} 页 · ` : "";
          return <article key={`${value.block_id}-${value.operation}`}>
            <button className="suggestion-title" onClick={() => onSelect(value.block_id)}>{textFor(value.block_id)}</button>
            <strong>{page}{targetLabel(value.before)} → {targetLabel(value.after)}</strong>
            <p>{value.reason}</p>
            {value.parent_title && <small>所属标题：{value.parent_title}</small>}
            <div className="review-suggestion-actions"><button disabled={blocked} onClick={() => onConfirm(value)}>采用</button><button disabled={blocked} onClick={() => onDismiss(value)}>忽略</button></div>
          </article>;
        })}
      </details>}
      <details className="outline-section" open><summary>当前目录</summary>
        {document.toc.length ? <ul className="toc-tree">{document.toc.map(node => <TocRow key={node.node_id} node={node} selected={selected} activeBlock={activeBlock} onSelect={onSelect}/>)}</ul> : <p className="empty-note">还没有标题。选择右侧正文块，将其设为标题。</p>}
      </details>
      <details className="outline-section structure-queue"><summary>修订历史 · {live.length}</summary>
        {live.length > 1 && <button className="block-button" disabled={blocked} onClick={onRevertAll}>撤销全部修订</button>}
        {patches.slice(0, 40).map(value => <article key={value.patch_id} className={value.reverted_at ? "is-reverted" : ""}>
          <button className="suggestion-title" onClick={() => onSelect(value.block_id)}>{textFor(value.block_id)}</button>
          <p>{targetLabel(value.after)} · {value.automatic ? "自动采用" : value.source === "ai" ? "采用 AI" : "人工调整"}{value.reverted_at ? " · 已撤销" : ""}</p>
          {!value.reverted_at && <button disabled={blocked || live.find(item => item.block_id === value.block_id)?.patch_id !== value.patch_id} onClick={() => onRevert(value.patch_id)}>撤销</button>}
        </article>)}
        {!patches.length && <p className="empty-note">尚无修订。</p>}
      </details>
    </div>
  </aside>;
});
