import { useEffect, useState } from "react";
import { api } from "../api";
import type { Block, TranslationEntry } from "../types";

export default function TranslationBlock({ projectId, block, entry, onChanged }: { projectId: string; block: Block; entry?: TranslationEntry; onChanged: () => void }) {
  const fallback = entry?.text || Object.values(entry?.cells || {}).join(" · ") || entry?.items?.join(" · ") || entry?.caption?.join(" ") || "";
  const [text, setText] = useState(fallback);
  useEffect(() => setText(fallback), [fallback]);
  const save = async () => { if (text === fallback) return; await api(`/projects/${projectId}/translations/${block.block_id}`, { method: "PUT", body: JSON.stringify({ text, locale: "zh-CN" }) }); onChanged(); };
  const unlock = async () => { await api(`/projects/${projectId}/translations/${block.block_id}/unlock?locale=zh-CN`, { method: "POST" }); onChanged(); };
  return <article className={`reader-block translation-edit ${entry?.locked ? "locked" : ""}`}><textarea aria-label={`${block.block_id} 译文`} value={text} placeholder="等待翻译；也可直接输入译文" onChange={event => setText(event.target.value)} onBlur={save}/><div className="translation-meta"><span>{entry?.status || "pending"}{entry?.selection_translations?.length ? ` · ${entry.selection_translations.length} 条选段` : ""}</span>{entry?.locked && <button onClick={unlock}>解锁</button>}</div></article>;
}
