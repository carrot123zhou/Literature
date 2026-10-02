import { memo } from "react";
import type { DocumentGraph, StructurePatch, StructureSuggestion, TocNode } from "../types";
import type { PatchProposal } from "./ResultPane";

export type TocCandidate = { page_index: number; page_number: number; confidence: number; reasons: string[] };

type Props = {
  document: DocumentGraph;
  tocText: string;
  onTocText: (value: string) => void;
  tocCandidates: TocCandidate[];
  onFindTocPages: () => void;
  onJumpPage: (page: number) => void;
  onAnalyze: () => void;
  onAnalyzeAI: () => void;
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

/** 左侧结构栏：目录基准、目录页候选、可编辑目录树、建议队列与修订历史。 */
export default memo(function OutlinePane(props: Props) {
  const { document, tocText, onTocText, tocCandidates, onFindTocPages, onJumpPage, onAnalyze, onAnalyzeAI, onPreviewAudit, suggestions, patches, selected, activeBlock, checked, onSelect, onToggleCheck, onConfirm, onConfirmAll, onRevert, onRevertAll, onPatch, busy } = props;
  const pending = suggestions.filter(value => !value.automatic);
  const live = patches.filter(value => !value.reverted_at);

  return (
    <aside className="sidebar mineru-control-column">
      <div className="panel-title"><strong>结构与任务</strong><span>规范主数据</span></div>
      <div className="pane-scroll">
      <details className="outline-section">
        <summary>确认原书目录基准</summary>
        <button className="block-button" onClick={onFindTocPages} disabled={busy}>寻找目录页候选</button>
        {tocCandidates.length > 0 && <div className="toc-candidates">
          {tocCandidates.map(candidate => (
            <button key={candidate.page_index} onClick={() => onJumpPage(candidate.page_number)}>
              第 {candidate.page_number} 页 <span>{Math.round(candidate.confidence * 100)}%</span>
              <small>{candidate.reasons.join(" · ")}</small>
            </button>
          ))}
        </div>}
        <textarea value={tocText} onChange={event => onTocText(event.target.value)} rows={8} spellCheck={false}/>
        <button className="primary block-button" onClick={onAnalyze} disabled={busy}>本地保守分析</button>
        <button className="block-button" onClick={onAnalyzeAI} disabled={busy}>AI 辅助分析</button>
        <button className="link-button block-button" onClick={onPreviewAudit} disabled={busy}>预览发送给 AI 的数据</button>
        <small className="audit-note">目录页候选在本地计算；AI 结构分析只使用目录文本、标题候选、bbox 与相邻 block 类型，不发送正文。</small>
      </details>

      <div className="outline-section">
        <div className="section-head"><strong>目录（{live.length} 项已校对）</strong><span>← → 调层级 · ✕ 降正文</span></div>
        {document.toc.length === 0
          ? <p className="empty-note">当前没有标题级 block。可在右侧把正文块设为标题。</p>
          : <ul className="toc-tree">{document.toc.map(node => <TocRow key={node.node_id} node={node} depth={0} selected={selected} activeBlock={activeBlock} checked={checked} onSelect={onSelect} onToggleCheck={onToggleCheck} onPatch={onPatch} busy={busy}/>)}</ul>}
      </div>

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
