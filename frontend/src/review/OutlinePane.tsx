import { memo, useEffect, useState } from "react";
import TaskModelPicker, { type TaskModelPatch } from "../workbench/TaskModelPicker";
import type { ReasoningMode } from "../workbench/ThinkingLevelField";
import type { DocumentGraph, Job, ModelRoleBinding, ModelService, StructurePatch, StructureSuggestion, TocNode } from "../types";
import { flattenToc } from "./projection";

export type TocCandidate = { page_index: number; page_number: number; confidence: number; reasons: string[] };
export type StructureMode = "coarse" | "fine";
/** 用户只选档位 + 开始/暂停/继续：目录准备、执行参数与视觉策略全部由后端档位预设与自动流程决定。 */
export const MODE_HINTS: Record<StructureMode, string> = {
  coarse: "有目录的书按目录差分校准（剔除误标、补齐漏标、校准层级）；无目录的论文按标题群与版面推断结构。快、省 token。",
  fine: "包含粗校的全部内容，额外发现未收录的小节，并复核整棵标题树的父子关系。更慢、消耗更多 token。",
};
export type StructureAnalysis = { job: Job | null; running: boolean; paused: boolean; mode: StructureMode };
type Props = {
  document: DocumentGraph; tocCandidates: TocCandidate[]; onFindTocPages: () => void; onJumpPage: (page: number) => void;
  pageText: string; onPageText: (value: string) => void;
  onPreviewActualAudit: () => void;
  suggestions: StructureSuggestion[]; patches: StructurePatch[]; selected: string; activeBlock: string;
  onSelect: (blockId: string) => void; onConfirm: (suggestion: StructureSuggestion) => void;
  onDismiss: (suggestion: StructureSuggestion) => void; onConfirmAll: (values: StructureSuggestion[]) => void;
  onRevert: (patchId: string) => void; onRevertAll: () => void;
  busy: boolean;
  /** 全部服务商（含模型列表）与当前任务级选择；默认 = 角色绑定。 */
  providers: ModelService[]; modelValue: ModelRoleBinding; configured: boolean;
  onConfigureModel: () => void; analysis: StructureAnalysis;
  onMode: (mode: StructureMode) => void;
  reasoningMode: ReasoningMode; reasoningBudget: number;
  onModelChange: (patch: TaskModelPatch) => void;
  onRunModel: () => void; onPause: () => void;
};
function TocRow({ node, selected, activeBlock, onSelect }: { node: TocNode; selected: string; activeBlock: string; onSelect: (id: string) => void }) {
  return <li>
    <div className={`toc-row${selected === node.block_id ? " selected" : ""}${activeBlock === node.block_id ? " active" : ""}`}>
      <span className="toc-level">H{node.level}</span>
      <button className="toc-title u-truncate" onClick={() => onSelect(node.block_id)} title={node.title}>{node.title}</button>
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
// 目录基准来源的只读摘要：无目录是正常路径，不提示用户去修复。
const BASELINE_LABELS: Record<string, string> = {
  local: "目录基准：原书目录（本地解析目录页，未调用模型提取）",
  model: "目录基准：原书目录（AI 从目录页提取，已存为项目基准供下次复用）",
  confirmed: "目录基准：已确认目录",
  auto_extract: "目录基准：原书目录（上次校准自动提取，未经人工确认；可在纠错入口重新指定目录页）",
  local_draft: "目录线索：本地解析的疑似目录（无显式目录标记，层级由 AI 复核）",
  none: "无目录模式：未找到印刷目录，AI 依据标题群与版面推断结构（正常路径，证据不足的项保留待复核）",
};
export default memo(function OutlinePane(props: Props) {
  const { document, tocCandidates, onFindTocPages, onJumpPage, pageText, onPageText, onPreviewActualAudit, suggestions, patches, selected, activeBlock, onSelect, onConfirm, onDismiss, onConfirmAll, onRevert, onRevertAll, busy, providers, modelValue, configured, onConfigureModel, analysis, onMode, reasoningMode, reasoningBudget, onModelChange, onRunModel, onPause } = props;
  const [queue, setQueue] = useState("all");
  const allPending = suggestions.filter(value => !value.automatic);
  const group = (value: StructureSuggestion) => value.operation === "set_type" ? "cleanup" : value.operation === "set_title" ? "promotion" : "level";
  const pending = allPending.filter(value => queue === "all" || group(value) === queue);
  const live = patches.filter(value => !value.reverted_at);
  const { job, running, paused, mode } = analysis;
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    setClock(Date.now());
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
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
  // 只读的目录基准摘要（含无目录模式与回退原因），来自作业真实结果。
  const baselineSource = (job?.result as { baseline_source?: string } | undefined)?.baseline_source || "";
  const baselineEntries = (job?.result as { baseline_entries?: number } | undefined)?.baseline_entries;
  const boundProvider = providers.find(item => item.service_id === modelValue.service_id);
  const boundModelLabel = modelValue.model || boundProvider?.models[0]?.model || "";
  return <aside className="sidebar mineru-control-column review-outline">
    <div className="panel-title"><strong>文档目录</strong><span>{flattenToc(document.toc).length} 个标题</span></div>
    <div className="pane-scroll">
      <details className="outline-section" open><summary>当前目录</summary>
        {document.toc.length ? <ul className="toc-tree">{document.toc.map(node => <TocRow key={node.node_id} node={node} selected={selected} activeBlock={activeBlock} onSelect={onSelect}/>)}</ul> : <p className="empty-note">还没有标题。选择右侧正文块，将其设为标题。</p>}
      </details>
      <details className="outline-section review-assistant">
        <summary>AI 校准</summary>
        <details className="task-model-details" open={!configured}>
          <summary>校对模型{configured && boundProvider ? `：${boundProvider.name}${boundModelLabel ? ` · ${boundModelLabel}` : ""}` : "（未配置）"}</summary>
          <TaskModelPicker
            label="校对模型"
            providers={providers}
            value={modelValue}
            reasoningMode={reasoningMode}
            reasoningBudget={reasoningBudget}
            showThinking={false}
            onConfigure={onConfigureModel}
            onChange={onModelChange}
          />
        </details>
        <div className="calib-modes u-segmented u-segmented--fill" role="radiogroup" aria-label="校准档位">
          {(["coarse", "fine"] as StructureMode[]).map(value =>
            <button key={value} type="button" role="radio" aria-checked={mode === value} className={mode === value ? "active is-active" : ""} disabled={blocked || paused} onClick={() => onMode(value)}>{value === "coarse" ? "粗校" : "精校"}</button>)}
        </div>
        <p className="calib-mode-hint">{MODE_HINTS[mode]}</p>
        {/* 运行中主按钮变「暂停」必须保持可点；模型未配置时才拦截。 */}
        <button className="primary block-button u-btn u-btn--primary u-btn--block" disabled={busy || (!running && !configured)}
          onClick={() => running ? onPause() : onRunModel()}>
          {running ? "暂停" : paused ? "继续校准" : "开始校准"}
        </button>
        <small className="review-hint">目录准备与执行参数由 AI 与档位自动决定：有已确认基准则复用，否则自动定位目录页并本地优先提取；没有目录的书按无目录模式推断。校准结果自动应用，可随时在修订历史撤销。</small>
        {!configured && <small className="review-hint">先在上方选择并配置校对模型。</small>}
        {job && <div className={`structure-run-panel${running ? " is-running" : ""}${job.status === "failed" ? " is-failed" : ""}${paused ? " is-paused" : ""}`} role="status">
          <strong><span className={`run-dot${running ? " running" : job.status === "failed" ? " failed" : " done"}`} aria-hidden="true"/> {running ? "正在校准" : paused ? "已暂停" : job.status === "failed" ? "校准失败" : "校准完成"}</strong>
          <progress max={1} value={job.progress || 0}/>
          {running && totalElapsed !== null && <p>已用时 {formatDuration(totalElapsed)}{inFlight && inFlightElapsed !== null ? ` · ${stageLabel(inFlight.scope)}进行中 ${formatDuration(inFlightElapsed)}` : ""}</p>}
          {paused && <p>已暂停：已完成的请求会保留，点「继续校准」只补剩余部分。</p>}
          {job.status === "failed"
            ? <p role="alert" className="review-error">{job.error || job.message}</p>
            : <p>{job.message}</p>}
          {baselineSource && <p className="review-hint">{BASELINE_LABELS[baselineSource] || `目录基准：${baselineSource}`}{typeof baselineEntries === "number" && baselineEntries > 0 ? ` · ${baselineEntries} 条` : ""}</p>}
          {summary && <p className="reconciliation-summary">{summary}</p>}
          <details><summary>诊断详情</summary>
            {coverage && <p>候选 {coverage.candidates} 个 · 第一遍规划定案 {coverage.reused ?? 0} 个 · 第二遍必须定案 {coverage.second_pass ?? 0} 个{coverage.needs_review > 0 ? `，${coverage.needs_review} 个仍需核对` : ""}{qa && `，抽查 ${qa.checked} 项一致性 ${qa.agree}/${qa.checked}`}。</p>}
            {tocExtraction === "local" && <p className="review-hint">目录来自本地解析（有显式目录标记页），层级按编号与标题词推断。</p>}
            {planning?.mode === "adaptive" && <p className="review-hint">规划路径：{planning.fallback ? `回退整体串行（${planning.fallback_reason || "框架不可靠"}）` : `自适应分支 · 框架 ${planning.framework?.state ?? "?"} · 可靠区域 ${planning.reliable_regions ?? 0} · 歧义区域 ${planning.ambiguous_regions ?? 0} · 局部升级 ${planning.local_upgrades ?? 0} · 遗漏 ${planning.missing ?? 0} · 重叠只读 ${planning.overlap_ignored ?? 0}`}</p>}
            {planning?.mode === "global" && <p className="review-hint">规划路径：整体串行。</p>}
            {hierarchy?.hierarchy_review?.headings !== undefined && <p>父子关系检查通过 {hierarchy.hierarchy_review.checked}/{hierarchy.hierarchy_review.headings} 个标题。</p>}
            {skippedModel > 0 && <p className="review-hint">有 {skippedModel} 项未通过角色或父子关系检查，未应用。</p>}
            <ul className="structure-activity">{(job.activity ?? []).map((item, index) => <li key={index}>{item.text}</li>)}</ul>
          </details>
        </div>}
        {/* 按需纠错入口：仅当目录识别不准时才指定真实 PDF 目录页；留空即自动识别。 */}
        {!running && <details className="calib-correct">
          <summary>目录识别不准？指定真实目录页</summary>
          <div className="review-page-picker">
            <label>目录页（留空自动识别）<input aria-label="源文件目录页" value={pageText} disabled={blocked} onChange={event => onPageText(event.target.value)} placeholder="例如 3-5, 7"/></label>
            <button className="link-button" disabled={blocked} onClick={onFindTocPages}>查找目录页</button>
            {tocCandidates.length > 0 && <div className="toc-candidates">{tocCandidates.slice(0, 8).map(candidate => <button key={candidate.page_index} disabled={blocked} title={candidate.reasons.join(" · ")} onClick={() => onJumpPage(candidate.page_number)}>查看第 {candidate.page_number} 页</button>)}</div>}
            <small>指定后点「开始校准」会以这些页重新提取目录并覆盖已保存基准；清空输入即恢复自动识别。</small>
          </div>
        </details>}
        {job?.result && <button className="link-button block-button" onClick={onPreviewActualAudit}>查看实际模型请求</button>}
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
