import { memo, useEffect, useMemo, useRef } from "react";
import type { Block, FlowLink, FlowLinks } from "../types";
import { useVirtualWindow } from "../workbench/useVirtualWindow";
import { blockMarkdown } from "./projection";

const EDITABLE_TYPES = ["title", "paragraph", "list", "footnote"];
const TYPE_LABELS: Record<string, string> = { paragraph: "正文", list: "列表", footnote: "脚注" };

export type ResultTab = "blocks" | "markdown" | "json";
export type StructureOperation = "set_type" | "set_level" | "set_title";
export type PatchProposal = { block_id: string; operation: StructureOperation; after: string | number; reason: string };

type Props = {
  blocks: Block[];
  busy: boolean;
  space: string;
  selected: string;
  activeBlock: string;
  checked: Set<string>;
  tab: ResultTab;
  onTab: (tab: ResultTab) => void;
  onSelect: (blockId: string) => void;
  onCheck: (blockId: string, range: boolean) => void;
  onClearChecks: () => void;
  onPatch: (proposal: PatchProposal) => void;
  onBatchPatch: (operation: "set_type" | "set_title", after: string | number) => void;
  markdown: string;
  jsonValue: unknown;
  follow: boolean;
  onFollow: (value: boolean) => void;
  flowLinks: FlowLinks;
};

function summary(block: Block): string {
  if (block.source_text) return block.source_text;
  if (block.cells.length) return `表格 · ${block.cells.length} cells`;
  if (block.items.length) return block.items.join(" · ");
  if (block.caption.length) return block.caption.join(" ");
  if (block.resource_refs.length) return `图片 · ${block.resource_refs.length} 个资源`;
  if (block.footnote.length) return block.footnote.join(" ");
  return "(空块)";
}

const BlockCard = memo(function BlockCard({ block, selected, active, checked, busy, flow, onSelect, onCheck, onPatch }: {
  block: Block;
  selected: boolean;
  active: boolean;
  checked: boolean;
  busy: boolean;
  flow?: FlowLink;
  onSelect: (blockId: string) => void;
  onCheck: (blockId: string, range: boolean) => void;
  onPatch: (proposal: PatchProposal) => void;
}) {
  const isTitle = block.type === "title";
  const editable = EDITABLE_TYPES.includes(block.type);
  const currentValue = isTitle ? String(block.level) : block.type;
  const flowChip = flow?.next_region ? "续下页" : "";
  return (
    <article
      className={`block-card${selected ? " selected" : ""}${active && !selected ? " active" : ""}${checked ? " checked" : ""}`}
      data-block-id={block.block_id}
      onClick={event => {
        if (event.metaKey || event.ctrlKey || event.shiftKey) { onCheck(block.block_id, event.shiftKey); return; }
        onSelect(block.block_id);
      }}
    >
      <div className="block-card-head">
        <span className={`type type-${block.type}`}>{isTitle ? `标题 H${block.level}` : ({ paragraph: "正文", table: "表格", image: "图片", list: "列表", equation: "公式", footnote: "脚注", code: "代码" } as Record<string, string>)[block.type] || block.type}</span>
        <span className="block-card-meta">
          <em>p{block.page_index + 1}</em>
          {flowChip && <em className="flow-chip" title="跨页续接：与相邻页的同一段落">{flowChip}</em>}
          {editable && <input type="checkbox" aria-label={`选择第 ${block.page_index + 1} 页的${isTitle ? "标题" : "正文"}块`} checked={checked} onClick={event => event.stopPropagation()} onChange={() => onCheck(block.block_id, false)}/>}
        </span>
      </div>
      <p>{summary(block)}</p>
      {selected && editable && <div className="card-actions" onClick={event => event.stopPropagation()}>
        <label>结构 <select aria-label="当前块结构" disabled={busy} value={currentValue} onChange={event => {
          const raw = event.target.value;
          if (raw in TYPE_LABELS) {
            onPatch({ block_id: block.block_id, operation: "set_type", after: raw, reason: `人工设为${TYPE_LABELS[raw]}` });
          } else {
            const level = Number(raw);
            onPatch({ block_id: block.block_id, operation: isTitle ? "set_level" : "set_title", after: level, reason: `人工设为 H${level}` });
          }
        }}>
          <option value="paragraph">正文</option>
          <option value="list">列表</option>
          <option value="footnote">脚注</option>
          {[1, 2, 3, 4, 5, 6].map(level => <option key={level} value={level}>H{level} 标题</option>)}
        </select></label><small>标题 1–6 · 正文/列表/脚注</small>
      </div>}

    </article>
  );
});

/** 右侧解析结果栏：结构块 / Markdown / JSON 三个视图，负责与左侧双向定位。 */
export default memo(function ResultPane(props: Props) {
  const { blocks, busy, selected, activeBlock, checked, tab, onTab, onSelect, onCheck, onClearChecks, onPatch, onBatchPatch, markdown, jsonValue, follow, onFollow, flowLinks } = props;
  const listRef = useRef<HTMLDivElement>(null);
  const markdownRef = useRef<HTMLDivElement>(null);
  const scrolledTo = useRef("");
  const blockIds = useMemo(() => blocks.map(block => block.block_id), [blocks]);
  const pinned = useMemo(() => [selected, activeBlock].filter(Boolean), [selected, activeBlock]);
  const vw = useVirtualWindow(blockIds, pinned);

  // 跟随阅读位置：PDF 滚动到新块时同步滚动结果栏（窗口化后直接定位到未渲染块）。
  useEffect(() => {
    const target = follow ? activeBlock : selected;
    if (!target || tab !== "blocks" || scrolledTo.current === target) return;
    scrolledTo.current = target;
    vw.scrollToBlock(target);
  }, [follow, activeBlock, selected, tab, vw.scrollToBlock]);

  // Markdown 页签与 PDF 双向：选中的块自动滚到可视区，强调其位置。
  useEffect(() => {
    if (tab !== "markdown") return;
    const target = selected || activeBlock;
    if (!target) return;
    markdownRef.current?.querySelector<HTMLElement>(`[data-md-block-id="${target}"]`)?.scrollIntoView({ block: "center" });
  }, [tab, selected, activeBlock]);


  const audit = jsonValue as { visual_evidence?: { image_url: string; kind: string; block_id?: string; page_index: number }[] } | null;
  const images = audit?.visual_evidence ?? [];
  const detail = images.length ? { ...audit, visual_evidence: images.map(({ image_url, ...meta }) => ({ ...meta, image: "见上方原页图片" })) } : jsonValue;
  return (
    <aside className="inspector mineru-result-pane">
      <div className="result-tabs">
        <button className={tab === "blocks" ? "active" : ""} onClick={() => onTab("blocks")}>结构编辑</button>
        <button className={tab === "markdown" ? "active" : ""} onClick={() => onTab("markdown")}>Markdown</button>
        <button className={tab === "json" ? "active" : ""} onClick={() => onTab("json")}>输入与详情</button>
        {tab === "blocks" && <label className="follow-toggle" title="PDF 滚动时自动跟随当前块"><input type="checkbox" checked={follow} onChange={event => onFollow(event.target.checked)}/>跟随</label>}
      </div>
      {tab === "blocks" && <>
        {checked.size > 0 && <div className="result-toolbar">
          <div className="batch-actions">
            <strong>已选 {checked.size}</strong>
            <select aria-label="批量调整结构" value="" disabled={busy} onChange={event => {
              const raw = event.target.value;
              if (raw in TYPE_LABELS) onBatchPatch("set_type", raw);
              else onBatchPatch("set_title", Number(raw));
            }}>
              <option value="" disabled>调整为…</option><option value="paragraph">正文</option><option value="list">列表</option><option value="footnote">脚注</option>{[1, 2, 3, 4, 5, 6].map(level => <option key={level} value={level}>H{level} 标题</option>)}
            </select>
            <button onClick={onClearChecks}>清空</button>
          </div>
        </div>}
        <div className="block-list" ref={node => { listRef.current = node; vw.scrollerRef.current = node; }} onScroll={vw.onScroll}>
          <div style={{ position: "relative", minHeight: vw.totalHeight }}>
            <div style={{ height: blocks[vw.rangeStart] ? vw.offsets[blocks[vw.rangeStart].block_id] : 0 }}/>
            {blocks.slice(vw.rangeStart, vw.rangeEnd).map(block => <div key={block.block_id} ref={vw.measure(block.block_id)}>
              <BlockCard
                block={block}
                busy={busy}
                selected={selected === block.block_id}
                active={activeBlock === block.block_id}
                checked={checked.has(block.block_id)}
                flow={flowLinks[block.block_id]}
                onSelect={onSelect}
                onCheck={onCheck}
                onPatch={onPatch}
              />
            </div>)}
            <div style={{ height: Math.max(0, vw.totalHeight - (vw.rangeEnd > 0 ? vw.offsets[blocks[vw.rangeEnd - 1].block_id] + vw.heightOf(blocks[vw.rangeEnd - 1].block_id) : 0)) }}/>
            {vw.overlay.map(id => {
              const block = blocks.find(candidate => candidate.block_id === id);
              if (!block) return null;
              return <div key={`overlay-${id}`} style={{ position: "absolute", top: vw.offsets[id], left: 0, right: 0 }}>
                <BlockCard
                  block={block}
                  busy={busy}
                  selected={selected === block.block_id}
                  active={activeBlock === block.block_id}
                  checked={checked.has(block.block_id)}
                  flow={flowLinks[block.block_id]}
                  onSelect={onSelect}
                  onCheck={onCheck}
                  onPatch={onPatch}
                />
              </div>;
            })}
          </div>
        </div>
      </>}
      {tab === "markdown" && <div className="result-code markdown-code" ref={markdownRef}>
        {blocks.map(block => {
          const text = blockMarkdown(block);
          if (!text) return null;
          return <span
            key={block.block_id}
            data-md-block-id={block.block_id}
            className={`md-block${selected === block.block_id ? " selected" : activeBlock === block.block_id ? " active" : ""}`}
            title={`定位到第 ${block.page_index + 1} 页`}
            onClick={() => onSelect(block.block_id)}
          >{text}{"\n\n"}</span>;
        })}
      </div>}
      {tab === "json" && <div className="review-audit"><div className="review-evidence">{images.map((item, index) => <figure key={index}><figcaption>{item.kind === "toc" ? "目录原页" : "标题裁片"} · 第 {item.page_index + 1} 页</figcaption><img src={item.image_url} alt={`将发送的第 ${item.page_index + 1} 页${item.kind === "toc" ? "目录" : "标题"}`}/></figure>)}</div><pre className="result-code">{JSON.stringify(detail, null, 2)}</pre></div>}
    </aside>
  );
});
