import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { api } from "../api";
import type { Block, TranslationEntry } from "../types";
import { STATE_META, TYPE_LABEL, blockKind, entryText, isKeep, sourceTableRows, tableRows } from "./readingModel";

type Draft = { text: string; items: string[]; caption: string[]; footnote: string[]; cells: Record<string, string> };

type Props = {
  projectId: string;
  block: Block;
  entry?: TranslationEntry;
  locale: string;
  selected: boolean;
  onSelect: () => void;
  onTranslateBlock: (options?: { refine?: boolean; force?: boolean }) => void;
  onChanged: () => void;
  editing: boolean;
  draft: Draft;
  onEditingChange: (editing: boolean) => void;
  onDraftChange: (updater: (value: Draft) => Draft) => void;
  onReview?: () => void;
};

export type { Draft };

export function draftFromEntry(entry?: TranslationEntry): Draft {
  return {
    text: entry?.text || "",
    items: [...(entry?.items || [])],
    caption: [...(entry?.caption || [])],
    footnote: [...(entry?.footnote || [])],
    cells: { ...(entry?.cells || {}) },
  };
}

/** 单段落卡片：左原文 / 右译文的逐段双语，含状态、编辑与重译。 */
export default function ReadingBlockCard({ projectId, block, entry, locale, selected, onSelect, onTranslateBlock, onChanged, editing, draft, onEditingChange, onDraftChange, onReview }: Props) {
  const kind = blockKind(block);
  const keep = isKeep(block, entry);
  const state = keep ? "keep" : (entry?.state || entry?.status || "untranslated");
  const staleReason = entry?.stale_reason;
  const meta = STATE_META[state] || STATE_META.translated;
  const badgeLabel = state === "stale" ? (staleReason === "structure" ? "已对齐" : "待复核") : meta.label;
  const setDraft = onDraftChange;
  const [conflict, setConflict] = useState(false);
  const [saveError, setSaveError] = useState("");
  const editBaseline = useRef<string>("");
  const area = useRef<HTMLTextAreaElement>(null);
  const wasEditing = useRef(editing);

  // 编辑中的草稿由父级持有（keyed by block），窗口化上下切换不丢草稿；
  // 新版本到达时只提示冲突，不覆盖草稿（F08）。
  useEffect(() => {
    if (!editing) return;
    const updated = entry?.updated_at || "";
    if (editBaseline.current && updated && updated !== editBaseline.current) setConflict(true);
  }, [entry, editing]);
  // 只在“进入编辑”那一刻拉焦点；窗口重挂载（editing 已为 true）不拉回。
  useEffect(() => {
    if (editing && !wasEditing.current) area.current?.focus();
    wasEditing.current = editing;
  }, [editing]);

  const beginEdit = () => {
    editBaseline.current = entry?.updated_at || "";
    setConflict(false);
    onEditingChange(true);
  };
  const payloadFor = () => ({
    text: draft.text, cells: draft.cells, items: draft.items, caption: draft.caption, footnote: draft.footnote,
  });
  const save = async () => {
    setSaveError("");
    try {
      await api(`/projects/${projectId}/translations/${block.block_id}`, { method: "PUT", body: JSON.stringify({ ...payloadFor(), locale }) });
      onEditingChange(false);
      onChanged();
    } catch (error) {
      // 失败保持编辑与草稿（不提前 onEditingChange(false) 清掉草稿）。
      setSaveError(error instanceof Error ? error.message : String(error));
    }
  };
  const unlock = async () => { await api(`/projects/${projectId}/translations/${block.block_id}/unlock?locale=${encodeURIComponent(locale)}`, { method: "POST" }); onChanged(); };

  let actions: ReactNode[] = [];
  if (keep) actions = [];
  else if (editing) actions = [
    <button key="save" className="primary" onClick={() => save()}>保存</button>,
    <button key="cancel" onClick={() => onEditingChange(false)}>取消</button>,
  ];
  else if (state === "untranslated" || state === "failed") actions = [
    <button key="one" onClick={() => onTranslateBlock()}>翻译本段</button>,
    <button key="edit" onClick={beginEdit}>编辑</button>,
  ];
  else if (state === "locked") actions = [
    <button key="edit" onClick={beginEdit}>编辑</button>,
    <button key="retrans" onClick={() => onTranslateBlock({ force: true })}>重译</button>,
    <button key="review" className="ghost" onClick={onReview}>审校</button>,
    <button key="unlock" className="ghost" onClick={unlock}>解锁</button>,
  ];
  else actions = [
    <button key="edit" onClick={beginEdit}>编辑</button>,
    <button key="retrans" onClick={() => onTranslateBlock({ force: true })}>重译</button>,
    <button key="review" className="ghost" onClick={onReview}>审校</button>,
  ];

  return <article className={`block b-${state}${selected ? " sel" : ""}`} data-block-id={block.block_id} onClick={onSelect}>
    <div className="bfoot">
      <span className={`btype t-${kind}`}>{TYPE_LABEL[kind] || block.type}</span>
      <span className={`bstate s-${staleReason === "structure" ? "synced" : meta.cls}`}><i className="dot"/>{badgeLabel}</span>
      <span className="bmeta">#{block.block_id} · 依据 r{entry?.structure_revision ?? 0}</span>
      <span className="bacts">{actions}</span>
    </div>
    <div className="bgrid">
      <div className="cell src">{renderSource(block, kind)}</div>
      <div className="cell dst">{renderDest(block, entry, kind, keep, editing, draft, setDraft, area)}</div>
    </div>
    {conflict && editing ? <div className="stalenote need">! 该段译文在编辑期间被其他任务更新，草稿已保留。保存将覆盖最新版本，取消则保留对方结果。</div> : null}
    {saveError ? <div className="stalenote need">! 保存失败：{saveError}</div> : null}
    {staleReason === "source" ? <div className="stalenote need">! 原文内容已变，建议重译该段以保持译文与原文一致<button className="ghost" onClick={event => { event.stopPropagation(); onTranslateBlock({ force: true }); }}>重译本段</button></div> : null}
    {staleReason === "structure" ? <div className="stalenote">✓ 结构校对调整了层级；译文已自动对齐到新的内容层级，无需重新翻译。</div> : null}
    {state === "failed" ? <div className="failnote">✕ {entry?.error || "该段落翻译失败或已被中断"}<button className="ghost" onClick={event => { event.stopPropagation(); onTranslateBlock(); }}>仅重试本段</button></div> : null}
    {entry?.quality_warnings?.length ? <div className="stalenote need">⚠ 本地复核提示（启发式，不是语义结论）：{entry.quality_warnings.join("；")}<button className="ghost" onClick={event => { event.stopPropagation(); (onReview || (() => onTranslateBlock({ force: true })))(); }}>带原文+现译审查</button></div> : null}
    {state === "locked" && !editing ? <div className="locknote">▣ 人工编辑已锁定，批量重译与润色默认跳过</div> : null}
  </article>;
}

function renderSource(block: Block, kind: string) {
  if (kind === "table") return renderTable(sourceTableRows(block), true);
  if (kind === "h1" || kind === "h2" || kind === "h3") return <h4>{block.source_text}</h4>;
  if (kind === "list") return <ul>{block.items.map((item, index) => <li key={index}>{item}</li>)}</ul>;
  if (kind === "figure") return <span className="keepbadge">▨ {block.source_text || "插图"} —— 首版保持原样</span>;
  if (kind === "footnote") return <p>{block.source_text || block.footnote.join(" ") || "（脚注）"}</p>;
  return <p>{block.source_text || block.caption.join(" ") || "（无文本）"}</p>;
}

function renderDest(
  block: Block, entry: TranslationEntry | undefined, kind: string, keep: boolean, editing: boolean,
  draft: Draft, setDraft: (updater: (value: Draft) => Draft) => void, area: RefObject<HTMLTextAreaElement | null>,
) {
  if (keep && !editing) return <span className="keepbadge">▨ 图片 / 公式 —— 首版保持原样</span>;
  if (editing) return renderEditor(block, kind, draft, setDraft, area);
  if (kind === "table") {
    const rows = tableRows(block, entry);
    const has = rows.some(row => row.some(text => text && text !== "—"));
    return has ? renderTable(rows, false) : <span className="empty">（尚未翻译）</span>;
  }
  const text = entry?.text || "";
  const items = entry?.items || [];
  if ((kind === "h1" || kind === "h2" || kind === "h3") && text) return <h4>{text}</h4>;
  if (kind === "list" && items.length) return <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul>;
  if (kind === "figure") return <span className="keepbadge">▨ 图片 —— 首版保持原样</span>;
  const body = text || entryText(entry);
  if (!body) return <span className="empty">（尚未翻译）</span>;
  return <p>{body}</p>;
}

function renderEditor(
  block: Block, kind: string, draft: Draft,
  setDraft: (updater: (value: Draft) => Draft) => void, area: RefObject<HTMLTextAreaElement | null>,
) {
  if (kind === "table" && block.cells.length) {
    const rows: Record<number, typeof block.cells> = {};
    for (const cell of block.cells) (rows[cell.row] ||= []).push(cell);
    return <div className="feldit">
      {Object.keys(rows).map(Number).sort((a, b) => a - b).map(row => <div className="fe-row" key={row}>
        {rows[row].sort((a, b) => a.column - b.column).map(cell => <input
          key={cell.cell_id}
          aria-label={`${block.block_id} ${cell.cell_id}`}
          value={draft.cells[cell.cell_id] ?? ""}
          placeholder={cell.text}
          onChange={event => setDraft(value => ({ ...value, cells: { ...value.cells, [cell.cell_id]: event.target.value } }))}
        />)}
      </div>)}
    </div>;
  }
  if (kind === "list" && block.items.length) {
    return <div className="feldit">
      {block.items.map((item, index) => <input
        key={index}
        aria-label={`${block.block_id} item ${index}`}
        value={draft.items[index] ?? ""}
        placeholder={item}
        onChange={event => setDraft(value => { const items = [...value.items]; items[index] = event.target.value; return { ...value, items }; })}
      />)}
    </div>;
  }
  if (kind === "figure") {
    const captions = block.caption.length ? block.caption : [""];
    return <div className="feldit">
      {captions.map((item, index) => <input
        key={index}
        aria-label={`${block.block_id} caption ${index}`}
        value={draft.caption[index] ?? ""}
        placeholder={item || "图注译文"}
        onChange={event => setDraft(value => { const caption = [...value.caption]; caption[index] = event.target.value; return { ...value, caption }; })}
      />)}
    </div>;
  }
  return <div className="feldit">
    <textarea
      ref={area}
      aria-label={`${block.block_id} 译文`}
      value={draft.text}
      placeholder="输入人工译文，保存后自动锁定，批量重译将跳过"
      onChange={event => setDraft(value => ({ ...value, text: event.target.value }))}
    />
    {block.footnote.length ? block.footnote.map((item, index) => <input
      key={index}
      aria-label={`${block.block_id} footnote ${index}`}
      value={draft.footnote[index] ?? ""}
      placeholder={item || "脚注译文"}
      onChange={event => setDraft(value => { const footnote = [...value.footnote]; footnote[index] = event.target.value; return { ...value, footnote }; })}
    />) : null}
  </div>;
}

function renderTable(rows: string[][], header: boolean) {
  return <div className="tblwrap"><table className="mini-tbl"><tbody>
    {rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => header && rowIndex === 0
      ? <th key={cellIndex}>{cell}</th> : <td key={cellIndex}>{cell}</td>)}</tr>)}
  </tbody></table></div>;
}
