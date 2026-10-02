import SplitPane from "../SplitPane";
import TocTree from "../TocTree";
import JobPanel from "./JobPanel";
import TranslationBlock from "./TranslationBlock";
import type { Block, DocumentGraph, Job, TranslationLayer } from "../types";

type Props = {
  projectId: string;
  document: DocumentGraph;
  translation: TranslationLayer | null;
  selected: string;
  checked: Set<string>;
  selectedScopeCount: number;
  jobs: Job[];
  glossary: Record<string, string>;
  onToggle: (id: string) => void;
  onSelectBlock: (id: string) => void;
  onPause: (job: Job) => void;
  onResume: (job: Job) => void;
  onOpenGlossary: () => void;
  onTranslate: (refine?: boolean, scope?: string[]) => void;
  onRefresh: () => void;
  onInstant: (event: React.MouseEvent<HTMLElement>) => void;
  onOpenReview: () => void;
};

/** 阅读与翻译工作区：翻译范围 / 原文 / 译文三栏，栏宽可拖动并记忆。 */
export default function ReadingWorkspace(props: Props) {
  const { projectId, document, translation, selected, checked, selectedScopeCount, jobs, glossary, onToggle, onSelectBlock, onPause, onResume, onOpenGlossary, onTranslate, onRefresh, onInstant, onOpenReview } = props;
  const selectedBlock = document.blocks.find(block => block.block_id === selected);

  return <SplitPane
    className="workspace reading-grid"
    storageKey="literature.split.reading"
    initial={[0.24, 0.38, 0.38]}
    minPx={[240, 320, 320]}
    handleLabels={["调整翻译范围栏宽度", "调整原文与译文宽度"]}
  >
    <aside className="sidebar reader-sidebar">
      <div className="panel-title"><strong>选择翻译范围</strong><span>{selectedScopeCount || "全书"} blocks</span></div>
      <div className="pane-scroll">
        <TocTree nodes={document.toc} checked={checked} onToggle={onToggle} onSelect={onSelectBlock}/>
        <button className="glossary-button" onClick={onOpenGlossary}>术语表 · {Object.keys(glossary).length} 项</button>
        <JobPanel jobs={jobs} onPause={onPause} onResume={onResume}/>
        <div className="current-block-action"><span>{selectedBlock ? `当前：${selectedBlock.block_id}` : "点选正文块"}</span><button disabled={!selectedBlock} onClick={() => selectedBlock && onTranslate(false, [selectedBlock.block_id])}>翻译当前 block</button></div>
      </div>
      <div className="sticky-actions"><button className="primary" onClick={() => onTranslate(false)}>开始初译</button><button onClick={() => onTranslate(true)}>按需润色</button></div>
    </aside>

    <section className="reader source-reader" onMouseUp={onInstant}>
      <div className="reader-heading">原文 <button onClick={onOpenReview}>打开原页</button></div>
      <div className="reader-scroll">
        {document.blocks.map(block => <article data-block-id={block.block_id} id={`source-${block.block_id}`} key={block.block_id} className={`reader-block ${selected === block.block_id ? "selected" : ""}`} onClick={() => onSelectBlock(block.block_id)}>{block.type === "title" ? <h3>{block.source_text}</h3> : block.type === "table" ? <div className="table-placeholder">表格 · {block.cells.length} cells</div> : <p>{block.source_text || block.items.join(" · ") || block.caption.join(" ")}</p>}</article>)}
      </div>
    </section>

    <section className="reader translation-reader">
      <div className="reader-heading">译文 <span>编辑后自动锁定</span></div>
      <div className="reader-scroll">
        {document.blocks.map(block => <TranslationBlock key={block.block_id} projectId={projectId} block={block} entry={translation?.entries[block.block_id]} onChanged={onRefresh}/>)}
      </div>
    </section>
  </SplitPane>;
}
