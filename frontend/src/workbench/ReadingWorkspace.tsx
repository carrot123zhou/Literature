import { useMemo, useState, useCallback } from "react";
import ReadingBlockCard, { draftFromEntry, type Draft } from "./ReadingBlockCard";
import { BlockPanel, GlossaryPanel, JobPanel, ScopeDialog, TocPanel, type ScopeConfirm, type ScopeKey } from "./ReadingPanels";
import { chapterOf } from "./readingModel";
import { useVirtualWindow } from "./useVirtualWindow";
import "./reading.css";
import type { ReasoningMode } from "./ThinkingLevelField";
import type { DocumentGraph, GlossaryVersion, Job, ModelRoleBinding, ModelService, TranslationEstimate, TranslationEntry, TranslationLayer, TranslationStats } from "../types";

type TranslateOptions = { refine?: boolean; force?: boolean; useGlossary?: boolean; instructions?: string; locale?: string; polish?: boolean; workers?: number; serviceId?: string; model?: string; reasoningMode?: ReasoningMode; reasoningBudget?: number };

type Props = {
  projectId: string;
  document: DocumentGraph;
  translation: TranslationLayer | null;
  stats: TranslationStats;
  usage: { input_tokens: number; output_tokens: number; cached_tokens?: number; reasoning_tokens?: number; estimated_cost: number };
  jobs: Job[];
  glossary: Record<string, string>;
  glossaryVersions: GlossaryVersion[];
  locale: string;
  checked: Set<string>;
  scopeBlockIds: string[];
  selected: string;
  estimate: TranslationEstimate | null;
  /** 翻译角色绑定的服务商与模型（用于侧栏展示与启动弹窗默认值）。 */
  providers: ModelService[];
  translationBinding: ModelRoleBinding;
  onSelectBlock: (id: string) => void;
  onToggle: (id: string) => void;
  onPause: (job: Job) => void;
  onResume: (job: Job) => void;
  onTranslate: (scope: string, blockIds: string[], options?: TranslateOptions) => void;
  onTranslateBlock: (blockId: string, options?: { refine?: boolean; force?: boolean }) => void;
  onEstimate: (options: ScopeConfirm) => void;
  onInvalidateEstimate: () => void;
  onFreezeGlossary: (entries: Record<string, string>) => void;
  onRollbackGlossary: (version: number) => void;
  onUnlockBlock: (blockId: string) => void;
  onRefresh: () => void;
  onInstant: (text: string, blockId: string) => void;
  onOpenReview: () => void;
  onConfigureModel: () => void;
  onReviewBlock: (blockId: string) => void;
  reviewCandidate: {
    project_id: string; locale: string; block_id: string; candidate_id: string;
    source_hash: string; base_revision: string; changed: boolean; complete: boolean;
    revised: Record<string, unknown>; original: Record<string, unknown>; source?: string; current?: string;
  } | null;
  onAdoptReview: () => void;
  onRejectReview: () => void;
};

const CHIPS: { key: keyof TranslationStats | "done"; label: string; cls: string }[] = [
  { key: "done", label: "已翻译", cls: "done" },
  { key: "locked", label: "人工锁定", cls: "lock" },
  { key: "stale_source", label: "待复核", cls: "stale" },
  { key: "stale_structure", label: "已对齐", cls: "synced" },
  { key: "failed", label: "失败", cls: "fail" },
  { key: "untranslated", label: "未翻译", cls: "idle" },
];

function summarizeCandidate(value: Record<string, unknown>): string {
  const text = typeof value?.text === "string" ? value.text : "";
  if (text.trim()) return text;
  const parts: string[] = [];
  const items = Array.isArray(value?.items) ? value.items : [];
  if (items.length) parts.push(items.map((item, index) => `${index + 1}. ${item}`).join(" "));
  const cells = value?.cells && typeof value.cells === "object" ? Object.entries(value.cells as Record<string, string>) : [];
  if (cells.length) parts.push(cells.map(([key, val]) => `${key}：${val}`).join(" | "));
  const caption = Array.isArray(value?.caption) ? value.caption : [];
  if (caption.length) parts.push(`图题：${caption.join(" ")}`);
  const footnote = Array.isArray(value?.footnote) ? value.footnote : [];
  if (footnote.length) parts.push(`脚注：${footnote.join(" ")}`);
  return parts.join(" · ") || "—";
}

export default function ReadingWorkspace(props: Props) {
  const {
    projectId, document, translation, stats, usage, jobs, glossary, glossaryVersions, locale, checked,
    scopeBlockIds, selected, estimate, providers, translationBinding,
    onSelectBlock, onToggle, onPause, onResume, onTranslate, onTranslateBlock, onEstimate,
    onFreezeGlossary, onRollbackGlossary, onUnlockBlock, onRefresh, onInstant, onOpenReview, onConfigureModel, onInvalidateEstimate,
    onReviewBlock, reviewCandidate, onAdoptReview, onRejectReview,
  } = props;
  const [sideTab, setSideTab] = useState<"toc" | "job" | "glossary" | "block">("toc");
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [editingIds, setEditingIds] = useState<Record<string, boolean>>({});
  const blockIds = useMemo(() => document.blocks.map(block => block.block_id), [document]);
  const pinned = useMemo(() => Object.keys(editingIds).filter(id => editingIds[id]), [editingIds]);
  const window_ = useVirtualWindow(blockIds, pinned);
  const draftFor = useCallback((blockId: string, entry?: TranslationEntry) => drafts[blockId] ?? draftFromEntry(entry), [drafts]);
  const changeDraft = useCallback((blockId: string, entry: TranslationEntry | undefined) => (updater: (value: Draft) => Draft) => {
    setDrafts(prev => ({ ...prev, [blockId]: updater(prev[blockId] ?? draftFromEntry(entry)) }));
  }, []);
  const changeEditing = useCallback((blockId: string, entry: TranslationEntry | undefined) => (editing: boolean) => {
    setEditingIds(prev => ({ ...prev, [blockId]: editing }));
    setDrafts(prev => {
      if (editing) return prev[blockId] ? prev : { ...prev, [blockId]: draftFromEntry(entry) };
      const next = { ...prev };
      delete next[blockId];
      return next;
    });
  }, []);
  const renderCard = (block: (typeof document.blocks)[number]) => {
    const entry = translation?.entries[block.block_id];
    return <ReadingBlockCard
      projectId={projectId}
      block={block}
      entry={entry}
      locale={locale}
      selected={selected === block.block_id}
      onSelect={() => onSelectBlock(block.block_id)}
      onTranslateBlock={options => onTranslateBlock(block.block_id, options)}
      onChanged={onRefresh}
      editing={Boolean(editingIds[block.block_id])}
      draft={draftFor(block.block_id, entry)}
      onEditingChange={changeEditing(block.block_id, entry)}
      onDraftChange={changeDraft(block.block_id, entry)}
      onReview={() => onReviewBlock(block.block_id)}
    />;
  };
  const [scopeOpen, setScopeOpen] = useState(false);
  const translationProvider = providers.find(item => item.service_id === translationBinding.service_id);
  const translationModel = translationBinding.model || translationProvider?.models[0]?.model || "";
  // 划选原文时只浮出按钮，点击后才发请求（F09）；不写 window.getSelection 无法判断的来源。
  const [selectionMenu, setSelectionMenu] = useState<{ x: number; y: number; text: string; blockId: string } | null>(null);
  const selectedBlock = document.blocks.find(block => block.block_id === selected);
  const activeJob = jobs.find(job => job.kind === "translate" || job.kind === "refine");
  const failed = document.blocks
    .filter(block => translation?.entries[block.block_id]?.state === "failed")
    .map(block => ({ block_id: block.block_id, type: block.type, error: translation?.entries[block.block_id]?.error || "" }));

  const confirmScope = (value: ScopeConfirm) => {
    const blockIds = value.scope === "blocks" ? scopeBlockIds : [];
    onTranslate(value.scope, blockIds, { refine: value.refine, force: value.force, polish: value.polish, workers: value.workers, useGlossary: value.useGlossary, instructions: value.instructions, locale: value.locale, serviceId: value.serviceId, model: value.model, reasoningMode: value.reasoningMode, reasoningBudget: value.reasoningBudget });
    setScopeOpen(false);
  };

  const cellOf = (node: Node | null) => {
    const element = node instanceof Element ? node : node?.parentElement || null;
    return element?.closest<HTMLElement>(".cell.src") || null;
  };
  const handleSelection = () => {
    const selection = window.getSelection();
    const text = selection?.toString().trim() || "";
    if (!selection || !text || selection.rangeCount === 0) { setSelectionMenu(null); return; }
    const range = selection.getRangeAt(0);
    const startCell = cellOf(range.startContainer);
    const endCell = cellOf(range.endContainer);
    const startBlock = startCell?.closest<HTMLElement>("[data-block-id]")?.dataset.blockId || "";
    const endBlock = endCell?.closest<HTMLElement>("[data-block-id]")?.dataset.blockId || "";
    // 只允许原文且同一 block 内的选择；划选译文或跨段不弹菜单。
    if (!startCell || !endCell || !startBlock || startBlock !== endBlock) { setSelectionMenu(null); return; }
    const rect = range.getBoundingClientRect();
    setSelectionMenu({ x: rect.left + rect.width / 2, y: rect.top, text, blockId: startBlock });
  };
  const fireInstant = () => {
    if (!selectionMenu) return;
    onInstant(selectionMenu.text, selectionMenu.blockId);
    setSelectionMenu(null);
    window.getSelection()?.removeAllRanges();
  };

  return <div className="rdg-root">
    <aside className="col side">
      <div className="side-tabs">
        <button className={sideTab === "toc" ? "on" : ""} onClick={() => setSideTab("toc")}><span className="ic">☰</span>目录</button>
        <button className={sideTab === "job" ? "on" : ""} onClick={() => setSideTab("job")}><span className="ic">◷</span>任务{stats.failed ? <span className="tabbadge on">{stats.failed}</span> : null}</button>
        <button className={sideTab === "glossary" ? "on" : ""} onClick={() => setSideTab("glossary")}><span className="ic">❖</span>术语表</button>
        <button className={sideTab === "block" ? "on" : ""} onClick={() => setSideTab("block")}><span className="ic">▤</span>当前块{selected ? <span className="tabdot on"/> : null}</button>
      </div>
      <div className="side-body">
        {sideTab === "toc" && <TocPanel document={document} translation={translation} checked={checked} onToggle={onToggle} onSelectBlock={id => { onSelectBlock(id); window_.scrollToBlock(id); }}/>}
        {sideTab === "job" && <JobPanel jobs={jobs} stats={stats} usage={usage} failed={failed} onPause={onPause} onResume={onResume} onRetryAll={() => onTranslate("failed", [], {})} onRetryOne={blockId => onTranslateBlock(blockId, { force: true })}/>}
        {sideTab === "glossary" && <GlossaryPanel glossary={glossary} versions={glossaryVersions} onFreeze={onFreezeGlossary} onRollback={onRollbackGlossary}/>}
        {sideTab === "block" && <BlockPanel block={selectedBlock} entry={selected ? translation?.entries[selected] : undefined} chapter={selectedBlock ? chapterOf(document, selectedBlock.block_id) : ""} onTranslateBlock={options => selectedBlock && onTranslateBlock(selectedBlock.block_id, options)} onUnlock={() => selectedBlock && onUnlockBlock(selectedBlock.block_id)}/>}
        <div className="panel"><h4>翻译模型</h4><div className="pbody">
          <div className="kv"><span>{translationProvider?.name || "未配置模型"}</span><b>{translationModel || "—"}</b></div>
          <button className="ghost" style={{ justifySelf: "start" }} onClick={onConfigureModel}>配置模型</button>
        </div></div>
      </div>
      <div className="side-foot">
        <div className="foot-prog">
          <div className="jobbar"><i style={{ width: `${stats.percent}%` }}/></div>
          <span className="txt"><b>{stats.percent}%</b> · {stats.done}/{stats.total}</span>
        </div>
        <div className="foot-acts">
          <button className="primary" onClick={() => setScopeOpen(true)}>开始翻译…</button>
          {activeJob?.status === "running"
            ? <button onClick={() => onPause(activeJob)}>⏸ 暂停</button>
            : <button disabled={!activeJob || !["paused", "budget_paused", "failed", "partial_failed"].includes(activeJob.status)} onClick={() => activeJob && onResume(activeJob)}>▶ 恢复</button>}
        </div>
        <div className="foot-note">暂停在分块边界生效 · 译文自动随结构对齐</div>
      </div>
    </aside>

    <section className="col center">
      <div className="colhead">
        <h3>原文 / 译文</h3>
        <span className="hint">左原文 · 右译文 · 双击译文可人工编辑 · 划选原文可即时翻译</span>
      </div>
      <div className="toolrow">
        <div className="chips">
          {CHIPS.map(chip => <span key={chip.key} className={`chip ${chip.cls}`}><i className="dot"/>{chip.label} {chip.key === "done" ? stats.done : (stats[chip.key] as number)}</span>)}
        </div>
        <div style={{ display: "flex", gap: 8, marginLeft: "auto" }}>
          <button className="ghost" onClick={onOpenReview}>打开原页</button>
        </div>
      </div>
      <div className="blank" ref={window_.scrollerRef} onMouseUp={handleSelection} onScroll={() => { setSelectionMenu(null); window_.onScroll(); }}>
        <div className="blocks" style={{ position: "relative", minHeight: window_.totalHeight }}>
          <div style={{ height: document.blocks[window_.rangeStart] ? window_.offsets[document.blocks[window_.rangeStart].block_id] : 0 }}/>
          {document.blocks.slice(window_.rangeStart, window_.rangeEnd).map(block => <div key={block.block_id} ref={window_.measure(block.block_id)}>{renderCard(block)}</div>)}
          <div style={{ height: Math.max(0, window_.totalHeight - (window_.rangeEnd > 0 ? window_.offsets[document.blocks[window_.rangeEnd - 1].block_id] + window_.heightOf(document.blocks[window_.rangeEnd - 1].block_id) : 0)) }}/>
          {window_.overlay.map(id => {
            const block = document.blocks.find(candidate => candidate.block_id === id);
            if (!block) return null;
            return <div key={`overlay-${id}`} style={{ position: "absolute", top: window_.offsets[id], left: 0, right: 0 }}>{renderCard(block)}</div>;
          })}
        </div>
      </div>
    </section>

    {reviewCandidate && <div className="review-candidate" role="dialog" aria-label="定向译审候选">
      <div className="rch">定向译审候选 · {reviewCandidate.block_id} · {reviewCandidate.changed ? "有修订" : "与现译一致"}</div>
      {reviewCandidate.complete
        ? <div className="rcmp">
            <div><span>原文</span><p>{reviewCandidate.source || "—"}</p></div>
            <div><span>现译</span><p>{summarizeCandidate(reviewCandidate.original)}</p></div>
            <div><span>建议</span><p>{summarizeCandidate(reviewCandidate.revised)}</p></div>
          </div>
        : <p>模型输出不完整，未提供可采纳候选；不会自动覆盖现有译文。</p>}
      <div style={{ display: "flex", gap: 8 }}>
        <button className="primary" disabled={!reviewCandidate.changed || !reviewCandidate.complete} onClick={onAdoptReview}>采纳并保存</button>
        <button onClick={onRejectReview}>拒绝</button>
      </div>
    </div>}

    {selectionMenu && <button
      className="instant-float"
      style={{ left: selectionMenu.x, top: selectionMenu.y }}
      onMouseDown={event => event.preventDefault()}
      onClick={fireInstant}
    >即时翻译</button>}

    {scopeOpen && <ScopeDialog
      projectTitle={document.title}
      locale={locale}
      stats={stats}
      selectedScopeCount={scopeBlockIds.length}
      glossaryVersions={glossaryVersions}
      providers={providers}
      binding={translationBinding}
      estimate={estimate}
      onEstimate={scope => onEstimate(scope)}
      onInvalidateEstimate={onInvalidateEstimate}
      onClose={() => setScopeOpen(false)}
      onConfirm={confirmScope}
    />}
  </div>;
}
