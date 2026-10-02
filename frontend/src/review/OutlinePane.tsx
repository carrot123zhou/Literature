import { memo } from "react";
import ModelServiceField from "../workbench/ModelServiceField";
import type { DocumentGraph, Job, ModelService, StructurePatch, StructureSuggestion, StructureTocEntry, TocNode } from "../types";
import type { PatchProposal } from "./ResultPane";

export type TocCandidate = { page_index: number; page_number: number; confidence: number; reasons: string[] };
export type StructureOptions = { extractToc: boolean; proofread: boolean; autoApply: boolean };
export type StructureAnalysis = {
  job: Job | null;
  running: boolean;
  options: StructureOptions;
  proposedToc: StructureTocEntry[] | null;
};

type Props = {
  document: DocumentGraph;
  tocText: string;
  onTocText: (value: string) => void;
  tocCandidates: TocCandidate[];
  onFindTocPages: () => void;
  onJumpPage: (page: number) => void;
  onAnalyze: () => void;
  onPreviewAudit: () => void;
  suggestions: StructureSuggestion[];
  patches: StructurePatch[];
  selected: string;
  activeBlock: string;
  checked: Set<string>;
  onSelect: (blockId: string) => void;
  onToggleCheck: (blockId: string) => void;
  onConfirm: (suggestion: StructureSuggestion) => void;
  onConfirmAll: () => void;
  onRevert: (patchId: string) => void;
  onRevertAll: () => void;
  onPatch: (proposal: PatchProposal) => void;
  busy: boolean;
  modelService?: ModelService;
  onConfigureModel: () => void;
  analysis: StructureAnalysis;
  onOptions: (patch: Partial<StructureOptions>) => void;
  onRunModel: () => void;
  onAdoptProposed: () => void;
};

const OPERATION_LABEL: Record<string, string> = {
  set_type: "改类型",
  set_level: "改层级",
  set_title: "设为标题",
};

function TocRow({ node, depth, selected, activeBlock, checked, onSelect, onToggleCheck, onPatch, busy }: {
  node: TocNode;
  depth: number;
  selected: string;
  activeBlock: string;
  checked: Set<string>;
  onSelect: (blockId: string) => void;
  onToggleCheck: (blockId: string) => void;
  onPatch: (proposal: PatchProposal) => void;
  busy: boolean;
}) {
  const current = node.block_id === selected;
  return (
    <li>
      <div className={`toc-row${current ? " selected" : ""}${node.block_id === activeBlock ? " active" : ""}`} style={{ paddingLeft: `${8 + depth * 12}px` }}>
        <input type="checkbox" checked={checked.has(node.block_id)} onChange={() => onToggleCheck(node.block_id)} title="加入翻译范围"/>
        <button className="toc-title" onClick={() => onSelect(node.block_id)} title={node.title}>{node.title}</button>
        <span className="toc-page">p{node.page_index + 1}</span>
        <span className="toc-level">H{node.level}</span>
        <span className="toc-actions">
          <button disabled={busy || node.level <= 1} title="提升一级" onClick={() => onPatch({ block_id: node.block_id, operation: "set_level", after: Math.max(1, node.level - 1), reason: "目录内提升层级" })}>←</button>
          <button disabled={busy || node.level >= 6} title="降低一级" onClick={() => onPatch({ block_id: node.block_id, operation: "set_level", after: Math.min(6, node.level + 1), reason: "目录内降低层级" })}>→</button>
          <button disabled={busy} title="降为正文（从目录移除）" onClick={() => onPatch({ block_id: node.block_id, operation: "set_type", after: "paragraph", reason: "目录内降为正文" })}>✕</button>
        </span>
      </div>
      {node.children.length > 0 && (
        <ul>{node.children.map(child => <TocRow key={child.node_id} node={child} depth={depth + 1} selected={selected} activeBlock={activeBlock} checked={checked} onSelect={onSelect} onToggleCheck={onToggleCheck} onPatch={onPatch} busy={busy}/>)}</ul>
      )}
    </li>
  );
}

/** 模型运行状态：进度、当前步骤与精简活动摘要，不展示完整对话。 */
function StructureRunPanel({ analysis, onAdoptProposed }: { analysis: StructureAnalysis; onAdoptProposed: () => void }) {
  const { job, running, proposedToc } = analysis;
  const activity = job?.activity ?? [];
  const recent = activity.slice(-6);
  const failed = job?.status === "failed";
  if (!job && !proposedToc) return null;
  return <div className={`structure-run-panel${running ? " is-running" : ""}${failed ? " is-failed" : ""}`}>
    <div className="structure-run-status">
      <span className={`run-dot${running ? " running" : failed ? " failed" : " done"}`}/>
      <strong>{running ? "模型运行中" : failed ? "运行失败" : "运行完成"}</strong>
      <span>{job ? `${Math.round((job.progress || 0) * 100)}%` : ""}</span>
    </div>
    {job && job.progress > 0 && <progress max={1} value={job.progress}/>}
    {job?.message && <p className="structure-run-message">{job.message}</p>}
    {recent.length > 0 && <ul className="structure-activity">
      {recent.map((item, index) => <li key={`${item.at}-${index}`} className={`activity-${item.level}`}><span>{item.text}</span></li>)}
    </ul>}
    {!running && proposedToc && proposedToc.length > 0 && <button className="primary block-button" onClick={onAdoptProposed}>采用模型提取的目录（{proposedToc.length} 条）</button>}
  </div>;
}

/** 左侧结构栏：目录基准（含模型运行）、可编辑目录树、建议队列与修订历史。 */
export default memo(function OutlinePane(props: Props) {
  const { document, tocText, onTocText, tocCandidates, onFindTocPages, onJumpPage, onAnalyze, onPreviewAudit, suggestions, patches, selected, activeBlock, checked, onSelect, onToggleCheck, onConfirm, onConfirmAll, onRevert, onRevertAll, onPatch, busy, modelService, onConfigureModel, analysis, onOptions, onRunModel, onAdoptProposed } = props;
  const pending = suggestions.filter(value => !value.automatic);
  const live = patches.filter(value => !value.reverted_at);
  const { options, running } = analysis;

  return (
    <aside className="sidebar mineru-control-column">
      <div className="panel-title"><strong>结构与任务</strong><span>规范主数据</span></div>
      <div className="pane-scroll">
      <details className="outline-section" open>
        <summary>确认原书目录基准</summary>
        <ModelServiceField label="目录模型" service={modelService} onConfigure={onConfigureModel}/>
        <button className="block-button" onClick={onFindTocPages} disabled={busy || running}>寻找目录页候选</button>
        {tocCandidates.length > 0 && <div className="toc-candidates">
          {tocCandidates.map(candidate => (
            <button key={candidate.page_index} onClick={() => onJumpPage(candidate.page_number)}>
              第 {candidate.page_number} 页 <span>{Math.round(candidate.confidence * 100)}%</span>
              <small>{candidate.reasons.join(" · ")}</small>
            </button>
          ))}
        </div>}
        <textarea value={tocText} onChange={event => onTocText(event.target.value)} rows={8} spellCheck={false}/>
        <div className="structure-options">
          <label title="把选中目录页的文本发送给模型，由模型提取目录条目"><input type="checkbox" checked={options.extractToc} onChange={() => onOptions({ extractToc: !options.extractToc })}/>从目录页提取</label>
          <label title="把标题候选与目录基准发送给模型，校对误识别标题与层级"><input type="checkbox" checked={options.proofread} onChange={() => onOptions({ proofread: !options.proofread })}/>校对标题</label>
          <label title="置信度达到 0.97 的建议自动应用，可随时撤销"><input type="checkbox" checked={options.autoApply} onChange={() => onOptions({ autoApply: !options.autoApply })}/>自动应用高置信</label>
        </div>
        <button className="primary block-button" onClick={onRunModel} disabled={busy || running}>{running ? "模型运行中…" : "识别并校对目录"}</button>
        <button className="block-button" onClick={onAnalyze} disabled={busy || running}>本地保守分析</button>
        <button className="link-button block-button" onClick={onPreviewAudit} disabled={busy || running}>预览发送给模型的数据</button>
        <small className="audit-note">本地分析只使用确认目录与标题候选。开启「从目录页提取」会把选中目录页的文本发送给模型，可在预览中核对；正文永不发送。</small>
        <StructureRunPanel analysis={analysis} onAdoptProposed={onAdoptProposed}/>
      </details>

      <details className="outline-section" open>
        <summary>目录 · {document.toc.length} 项标题</summary>
        <div className="section-head"><strong>{live.length} 项已校对</strong><span>← → 调层级 · ✕ 降正文</span></div>
        {document.toc.length === 0
          ? <p className="empty-note">当前没有标题级 block。可在右侧把正文块设为标题。</p>
          : <ul className="toc-tree">{document.toc.map(node => <TocRow key={node.node_id} node={node} depth={0} selected={selected} activeBlock={activeBlock} checked={checked} onSelect={onSelect} onToggleCheck={onToggleCheck} onPatch={onPatch} busy={busy}/>)}</ul>}
      </details>

      {pending.length > 0 && <div className="structure-queue">
        <details open>
          <summary>待确认建议 · {pending.length}</summary>
          <button className="primary block-button" onClick={onConfirmAll} disabled={busy}>确认全部低风险修正</button>
          {pending.map(value => (
            <article key={`${value.block_id}-${value.operation}`} onClick={() => onSelect(value.block_id)}>
              <strong>{value.operation === "set_type" ? "降为正文" : `改为 H${value.after}`}</strong>
              <span>{Math.round(value.confidence * 100)}%</span>
              <p>{value.reason}</p>
              <button onClick={event => { event.stopPropagation(); onConfirm(value); }} disabled={busy}>确认此项</button>
            </article>
          ))}
        </details>
      </div>}

      <div className="structure-queue">
        <details>
          <summary>修订历史 · {live.length}{patches.length !== live.length ? `（${patches.length - live.length} 已撤销）` : ""}</summary>
          {live.length > 1 && <button className="block-button" onClick={onRevertAll} disabled={busy}>撤销全部未撤销修订</button>}
          {patches.slice(0, 40).map(value => (
            <article className={value.reverted_at ? "is-reverted" : ""} key={value.patch_id} onClick={() => onSelect(value.block_id)}>
              <strong>{value.source === "ai" ? "AI 自动" : "人工"} · {OPERATION_LABEL[value.operation] ?? value.operation}</strong>
              <span>{value.reverted_at ? "已撤销" : `${Math.round(value.confidence * 100)}%`}</span>
              <p>{value.block_id} · {typeof value.after === "object" ? JSON.stringify(value.after) : String(value.after)}</p>
              {!value.reverted_at && <button onClick={event => { event.stopPropagation(); onRevert(value.patch_id); }} disabled={busy}>撤销</button>}
            </article>
          ))}
          {patches.length === 0 && <p className="empty-note">尚无结构修订。</p>}
        </details>
      </div>

      <div className="status-steps-panel">
        <strong>处理状态</strong>
        <div className="status-step is-done"><i/>MinerU raw 已封存</div>
        <div className="status-step is-done"><i/>规范图谱已生成（{document.blocks.length} blocks）</div>
        <div className={`status-step ${pending.length ? "is-active" : "is-done"}`}><i/>目录校准 {pending.length ? "待确认" : "已同步"}</div>
        <div className="status-latest">document.json + source.md + assets</div>
      </div>
      </div>
    </aside>
  );
});
