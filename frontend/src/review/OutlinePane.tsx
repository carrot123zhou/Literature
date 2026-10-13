import { memo, useEffect, useState } from "react";
import ModelServiceField from "../workbench/ModelServiceField";
import type { DocumentGraph, Job, ModelService, StructurePatch, StructureSuggestion, StructureTocEntry, TocNode } from "../types";
import { flattenToc, formatTocDraft, parseTocDraft } from "./projection";

export type TocCandidate = { page_index: number; page_number: number; confidence: number; reasons: string[] };
export type StructureOptions = { extractToc: boolean; proofread: boolean; autoApply: boolean; useVision: boolean; requestTimeoutSeconds: number; proofreadFull: boolean; proofreadConcurrency: number };
export type StructureAnalysis = { job: Job | null; running: boolean; options: StructureOptions; proposedToc: StructureTocEntry[] | null };
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
  modelService?: ModelService; onConfigureModel: () => void; analysis: StructureAnalysis;
  onOptions: (patch: Partial<StructureOptions>) => void; onRunModel: () => void;
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
const SCOPE_LABELS: Record<string, string> = { structure_extract: "目录核对", structure_outline: "整体标题树规划", structure_proofread: "标题角色复核" };
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
  const { document, tocText, onTocText, tocCandidates, onFindTocPages, onJumpPage, pageText, onPageText, onAnalyze, onPreviewAudit, onReadLocalToc, suggestions, patches, selected, activeBlock, onSelect, onConfirm, onDismiss, onConfirmAll, onRevert, onRevertAll, busy, modelService, onConfigureModel, analysis, onOptions, onRunModel, onAdoptProposed, onConfirmReview } = props;
  const [queue, setQueue] = useState("all");
  const allPending = suggestions.filter(value => !value.automatic);
  const group = (value: StructureSuggestion) => value.operation === "set_type" ? "cleanup" : value.operation === "set_title" ? "promotion" : "level";
  const pending = allPending.filter(value => queue === "all" || group(value) === queue);
  const live = patches.filter(value => !value.reverted_at);
  const { options, running, job, proposedToc } = analysis;
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
  const auditRequests = (job?.result as { audit?: { requests?: Array<{ scope?: string; status?: string; started_at?: string }> } } | undefined)?.audit?.requests ?? [];
  const createdMs = job?.created_at ? Date.parse(job.created_at) : NaN;
  const totalElapsed = running && Number.isFinite(createdMs) ? Math.max(0, Math.round((clock - createdMs) / 1000)) : null;
  const inFlight = [...auditRequests].reverse().find(item => item.status === "started" && item.started_at);
  const inFlightMs = inFlight?.started_at ? Date.parse(inFlight.started_at) : NaN;
  const inFlightElapsed = running && Number.isFinite(inFlightMs) ? Math.max(0, Math.round((clock - inFlightMs) / 1000)) : null;
  const hierarchy = (job?.result as { hierarchy_review?: { checked: number; headings: number }; hierarchy_plan?: { profile?: { toc_scope?: string; root_convention?: string } } } | undefined);
  const baselineCount = parseTocDraft(tocText).length;
  return <aside className="sidebar mineru-control-column review-outline">
    <div className="panel-title"><strong>文档目录</strong><span>{flattenToc(document.toc).length} 个标题</span></div>
    <div className="pane-scroll">
      <details className="outline-section review-assistant" open>
        <summary>AI 校准</summary>
        <ModelServiceField label="校对模型" service={modelService} onConfigure={onConfigureModel}/>
        <p className="review-hint">AI 先理解目录收录范围和整体标题树，再结合父标题与相邻标题复核。目录未收录的小节也会检查层级；建议由你确认，修改可撤销。</p>
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
        <button className="primary block-button" onClick={onRunModel} disabled={blocked || !modelService?.configured || (!options.extractToc && !baselineCount)}>{running ? "正在校准…" : options.extractToc ? "AI 校准结构" : "AI 校对标题"}</button>
        {options.extractToc && <button className="link-button block-button" onClick={onReadLocalToc} disabled={blocked}>本地读取目录 · 0 费用</button>}
        {!modelService?.configured && <small className="review-hint">先选择并配置校对模型；只想用本地校对可不配置模型。</small>}
        {job && <div className={`structure-run-panel${job.status === "failed" ? " is-failed" : ""}`} role="status">
          <strong>{running ? "正在处理" : job.status === "failed" ? "校准失败" : "校准完成"}</strong>
          <progress max={1} value={job.progress || 0}/>
          {running && totalElapsed !== null && <p>已运行 {formatDuration(totalElapsed)}{inFlight && inFlightElapsed !== null ? ` · ${stageLabel(inFlight.scope)}进行中 ${formatDuration(inFlightElapsed)}` : ""}</p>}
          {job.status === "failed"
            ? <p role="alert" className="review-error">{job.error || job.message}</p>
            : <p>{job.message}</p>}
          {job.status === "failed" && <small>再次校准时，已完成且文档、模型与输入一致的请求会复用。</small>}
          {coverage && <p>候选 {coverage.candidates} 个 · 第一遍复用 {coverage.reused ?? 0} 个 · 第二遍复核 {coverage.second_pass ?? 0} 个{coverage.needs_review > 0 ? `，${coverage.needs_review} 个仍需核对` : ""}；复用按角色、父子关系与目录一致性检查，冲突项一律复核。</p>}
          {hierarchy?.hierarchy_review?.headings !== undefined && <p>父子关系检查通过 {hierarchy.hierarchy_review.checked}/{hierarchy.hierarchy_review.headings} 个标题；未通过的层级修改不进入建议。</p>}
          {hierarchy?.hierarchy_plan?.profile?.toc_scope && <p className="review-hint">目录范围：{hierarchy.hierarchy_plan.profile.toc_scope}<br/>层级约定：{hierarchy.hierarchy_plan.profile.root_convention}</p>}
          {reconciliation && <p className="reconciliation-summary">目录 {reconciliation.toc_entries ?? 0} · 已定位 {reconciliation.matched ?? 0} · 层级修正 {reconciliation.level_corrections ?? 0} · 疑似漏标 {reconciliation.promotions ?? 0} · 未定位 {reconciliation.missing ?? 0} · 未收录标题 {reconciliation.unlisted_titles ?? 0}</p>}
          {skippedModel > 0 && <p className="review-hint">有 {skippedModel} 项未通过角色或父子关系检查，仍需复核；详情见过程和请求记录。</p>}
          {job.result && <button className="link-button block-button" onClick={props.onPreviewActualAudit}>查看实际模型请求</button>}
          <details><summary>查看过程</summary><ul className="structure-activity">{(job.activity ?? []).map((item, index) => <li key={index}>{item.text}</li>)}</ul></details>
        </div>}
        {proposedToc?.length ? <details className="review-baseline" open><summary>待确认目录草稿 · {draftEntries.length} 条</summary><p className="review-hint">请对照原目录编辑文字与层级，每行用 # 至 ###### 表示层级；未定位条目也可能超出当前解析页范围。</p><textarea aria-label="待确认目录草稿" value={draftText} disabled={blocked} onChange={event => setDraftText(event.target.value)} rows={8} spellCheck={false}/>{invalidDraftLines > 0 && <p role="alert">有 {invalidDraftLines} 行格式无效，请按“# 标题”格式修改后确认。</p>}<button className="block-button" disabled={blocked || !draftEntries.length || invalidDraftLines > 0} onClick={() => onAdoptProposed(draftEntries)}>确认目录并保存基准</button></details> : null}
        <details className="review-advanced"><summary>校对选项</summary>
          <label>单次模型响应等待（秒）<input aria-label="单次模型响应等待（秒）" type="number" min={30} max={1800} value={options.requestTimeoutSeconds} disabled={blocked} onChange={event => onOptions({ requestTimeoutSeconds: Number(event.target.value) })}/></label>
          {options.extractToc && <div className="review-page-picker">
            <label>限定目录页（留空自动识别）<input aria-label="源文件目录页" value={pageText} disabled={blocked} onChange={event => onPageText(event.target.value)} placeholder="例如 3-5, 7"/></label>
            <button className="link-button" disabled={blocked} onClick={onFindTocPages}>查找目录页</button>
            {tocCandidates.length > 0 && <div className="toc-candidates">{tocCandidates.slice(0, 8).map(candidate => <button key={candidate.page_index} disabled={blocked} title={candidate.reasons.join(" · ")} onClick={() => onJumpPage(candidate.page_number)}>查看第 {candidate.page_number} 页</button>)}</div>}
            <small>自动定位后由 AI 核对源目录，区分作者、标题和收录范围。最多 6 页。</small>
          </div>}
          <label className="structure-option"><input type="checkbox" checked={options.useVision} disabled={blocked} onChange={() => onOptions({ useVision: !options.useVision })}/>对照原页图片 <small>需视觉模型</small></label>
          <label className="structure-option"><input type="checkbox" checked={options.autoApply} disabled={blocked} onChange={() => onOptions({ autoApply: !options.autoApply })}/>自动采用高置信修正</label>
          <label className="structure-option"><input type="checkbox" checked={options.proofreadFull} disabled={blocked} onChange={() => onOptions({ proofreadFull: !options.proofreadFull })}/>全量复核每个候选（更慢，回退用）</label>
          <label>复核并发（1–4 路）<input aria-label="复核并发" type="number" min={1} max={4} value={options.proofreadConcurrency} disabled={blocked} onChange={event => onOptions({ proofreadConcurrency: Math.max(1, Math.min(4, Number(event.target.value) || 1)) })}/></label>
          <small>默认只复核第一遍规划中缺失、不确定、冲突或证据不足的候选；全量复核会重新判断每个候选，仅作回退。自动采用仅用于本地校准，AI 整体推理的修改均待确认。</small>
          <button className="block-button" onClick={onAnalyze} disabled={blocked}>只做本地校准 · 无模型费用</button>
          <button className="link-button block-button" onClick={onPreviewAudit} disabled={blocked}>预览启动输入</button>
        </details>
      </details>
      {allPending.length > 0 && <details className="outline-section structure-queue" open>
        <summary>待确认 · {allPending.length}</summary>
        <select aria-label="筛选校准建议" value={queue} onChange={event => setQueue(event.target.value)}>
          {[["all", "全部"], ["cleanup", "清理误标"], ["level", "调整层级"], ["promotion", "补回标题"]].map(([key, label]) => <option key={key} value={key}>{label} · {allPending.filter(value => key === "all" || group(value) === key).length}</option>)}
        </select>
        <button className="block-button" disabled={blocked || !pending.length} onClick={() => onConfirmAll(pending)}>采用当前 {pending.length} 项</button>
        <button className="block-button" disabled={busy} onClick={onConfirmReview}>确认当前审查范围（全书）</button>
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
