import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Block, TranslationEntry } from "../types";

const STATE_LABEL: Record<string, string> = {
  untranslated: "未译", translated: "已译", refined: "已润色", edited: "已编辑",
  locked: "已锁定", failed: "失败", stale: "待复核",
};

type Props = {
  projectId: string;
  block: Block;
  entry?: TranslationEntry;
  locale: string;
  selected: boolean;
  onSelect: () => void;
  onTranslateBlock: (options?: { refine?: boolean; force?: boolean }) => void;
  onChanged: () => void;
};

/** 单块译文：状态标记、人工编辑锁定、单块翻译/重译/解锁。 */
export default function TranslationBlock({ projectId, block, entry, locale, selected, onSelect, onTranslateBlock, onChanged }: Props) {
  const fallback = entry?.text || Object.values(entry?.cells || {}).join(" · ") || entry?.items?.join(" · ") || entry?.caption?.join(" ") || "";
  const [text, setText] = useState(fallback);
  const [editing, setEditing] = useState(false);
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => setText(fallback), [fallback]);
  useEffect(() => { if (editing) area.current?.focus(); }, [editing]);

  const state = entry?.state || (fallback ? "translated" : "untranslated");
  const save = async () => {
    setEditing(false);
    if (text === fallback) return;
    await api(`/projects/${projectId}/translations/${block.block_id}`, { method: "PUT", body: JSON.stringify({ text, locale }) });
    onChanged();
  };
  const unlock = async () => { await api(`/projects/${projectId}/translations/${block.block_id}/unlock?locale=${encodeURIComponent(locale)}`, { method: "POST" }); onChanged(); };

  return <article
    data-block-id={block.block_id}
    className={`reader-block translation-edit state-${state} ${selected ? "selected" : ""} ${entry?.locked ? "locked" : ""}`}
    onClick={onSelect}
  >
    {editing
      ? <textarea
          ref={area}
          aria-label={`${block.block_id} 译文`}
          value={text}
          placeholder="输入人工译文，保存后自动锁定，批量重译将跳过"
          onChange={event => setText(event.target.value)}
          onBlur={save}
        />
      : <div className="translation-text" onDoubleClick={() => setEditing(true)}>
          {fallback || <span className="untranslated">等待翻译；双击可手动输入</span>}
        </div>}
    <div className="translation-meta">
      <span className={`bstate s-${state}`}>{STATE_LABEL[state] || state}</span>
      {entry?.selection_translations?.length ? <span title="划词即时翻译记录">{entry.selection_translations.length} 条选段</span> : null}
      <span className="grow"/>
      {state === "untranslated" || state === "failed" ? <button onClick={event => { event.stopPropagation(); onTranslateBlock(); }}>翻译本段</button> : null}
      {state === "stale" ? <button onClick={event => { event.stopPropagation(); onTranslateBlock({ force: true }); }}>重译</button> : null}
      {state === "translated" || state === "refined" || state === "edited" ? <button onClick={event => { event.stopPropagation(); onTranslateBlock({ refine: true, force: true }); }}>润色</button> : null}
      {entry?.locked ? <button onClick={event => { event.stopPropagation(); unlock(); }}>解锁</button> : <button onClick={event => { event.stopPropagation(); setEditing(true); }}>编辑</button>}
    </div>
    {state === "stale" ? <div className="stalenote">结构校对调整了该块的角色或层级，译文可能需复核；可直接「重译」或保留。</div> : null}
    {entry?.locked && state !== "stale" ? <div className="locknote">人工编辑已锁定，批量重译与润色默认跳过。</div> : null}
  </article>;
}
