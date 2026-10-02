import { memo, useEffect, useMemo, useRef } from "react";
import type { Block } from "../types";
import { hasBBox } from "./geometry";

export type ResultTab = "blocks" | "markdown" | "json";
export type StructureOperation = "set_type" | "set_level" | "set_title";
export type PatchProposal = { block_id: string; operation: StructureOperation; after: string | number; reason: string };

type Props = {
  blocks: Block[];
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

const BlockCard = memo(function BlockCard({ block, selected, active, checked, space, onSelect, onCheck, onPatch }: {
  block: Block;
  selected: boolean;
  active: boolean;
  checked: boolean;
  space: string;
  onSelect: (blockId: string) => void;
  onCheck: (blockId: string, range: boolean) => void;
  onPatch: (proposal: PatchProposal) => void;
}) {
  const isTitle = block.type === "title";
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
        <span className={`type type-${block.type}`}>{block.type}{isTitle && block.level ? ` H${block.level}` : ""}</span>
        <span className="block-card-meta">
          <em>p{block.page_index + 1}</em>
          {hasBBox(block) ? <em>bbox</em> : <em className="muted">无 bbox</em>}
          <small>{block.block_id}</small>
        </span>
      </div>
      <p>{summary(block)}</p>
      <div className="card-actions" onClick={event => event.stopPropagation()}>
        {[1, 2, 3, 4, 5, 6].map(level => (
          <button
            key={level}
            className={`level-button${isTitle && block.level === level ? " current" : ""}`}
            title={isTitle ? `改为 H${level}` : `设为 H${level} 标题`}
            onClick={() => onPatch({
              block_id: block.block_id,
              operation: isTitle ? "set_level" : "set_title",
              after: level,
              reason: isTitle ? `人工改为 H${level}` : `人工设为 H${level} 标题`,
            })}
          >H{level}</button>
        ))}
        {isTitle
          ? <button className="demote-button" onClick={() => onPatch({ block_id: block.block_id, operation: "set_type", after: "paragraph", reason: "人工降为正文" })}>降为正文</button>
          : <button className="promote-button" onClick={() => onPatch({ block_id: block.block_id, operation: "set_title", after: 2, reason: "人工设为标题" })}>设为标题</button>}
      </div>
    </article>
  );
});

/** 右侧解析结果栏：结构块 / Markdown / JSON 三个视图，负责与左侧双向定位。 */
export default memo(function ResultPane(props: Props) {
  const { blocks, space, selected, activeBlock, checked, tab, onTab, onSelect, onCheck, onClearChecks, onPatch, onBatchPatch, markdown, jsonValue, follow, onFollow } = props;
  const listRef = useRef<HTMLDivElement>(null);
  const scrolledTo = useRef("");

  // 跟随阅读位置：PDF 滚动到新块时同步滚动结果栏，但不改变显式选中。
  useEffect(() => {
    const target = follow ? activeBlock : selected;
    if (!target || tab !== "blocks" || scrolledTo.current === target) return;
    const element = listRef.current?.querySelector<HTMLElement>(`.block-card[data-block-id="${target}"]`);
    if (!element) return;
    scrolledTo.current = target;
    element.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [follow, activeBlock, selected, tab]);

  const grouped = useMemo(() => {
    const counts = new Map<string, number>();
    for (const block of blocks) counts.set(block.type, (counts.get(block.type) ?? 0) + 1);
    return [...counts.entries()].map(([type, count]) => `${type} ${count}`).join(" · ");
  }, [blocks]);

  return (
    <aside className="inspector mineru-result-pane">
      <div className="result-tabs">
        <button className={tab === "blocks" ? "active" : ""} onClick={() => onTab("blocks")}>结构块</button>
        <button className={tab === "markdown" ? "active" : ""} onClick={() => onTab("markdown")}>Markdown</button>
        <button className={tab === "json" ? "active" : ""} onClick={() => onTab("json")}>JSON</button>
        {tab === "blocks" && <label className="follow-toggle" title="PDF 滚动时自动跟随当前块"><input type="checkbox" checked={follow} onChange={event => onFollow(event.target.checked)}/>跟随</label>}
      </div>
      {tab === "blocks" && <>
        <div className="result-toolbar">
          <span>{blocks.length} blocks · {grouped}</span>
          {checked.size > 0 && <div className="batch-actions">
            <strong>已选 {checked.size}</strong>
            {[1, 2, 3, 4, 5, 6].map(level => <button key={level} onClick={() => onBatchPatch("set_title", level)}>H{level}</button>)}
            <button onClick={() => onBatchPatch("set_type", "paragraph")}>降为正文</button>
            <button onClick={onClearChecks}>清空</button>
          </div>}
        </div>
        <div className="block-list" ref={listRef}>
          {blocks.map(block => (
            <BlockCard
              key={block.block_id}
              block={block}
              space={space}
              selected={selected === block.block_id}
              active={activeBlock === block.block_id}
              checked={checked.has(block.block_id)}
              onSelect={onSelect}
              onCheck={onCheck}
              onPatch={onPatch}
            />
          ))}
        </div>
      </>}
      {tab === "markdown" && <pre className="result-code markdown-code">{markdown}</pre>}
      {tab === "json" && <pre className="result-code">{JSON.stringify(jsonValue, null, 2)}</pre>}
    </aside>
  );
});
