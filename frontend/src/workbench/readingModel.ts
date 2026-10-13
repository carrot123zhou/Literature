import type { Block, TranslationEntry } from "../types";

/** 段落显示状态 → 标签与颜色类（与原型状态机一致）。 */
export const STATE_META: Record<string, { label: string; cls: string }> = {
  untranslated: { label: "未翻译", cls: "idle" },
  queued: { label: "排队中", cls: "act" },
  translating: { label: "翻译中", cls: "run" },
  translated: { label: "已翻译", cls: "done" },
  refined: { label: "已润色", cls: "done" },
  edited: { label: "已编辑", cls: "lock" },
  locked: { label: "人工锁定", cls: "lock" },
  failed: { label: "失败", cls: "fail" },
  stale: { label: "待复核", cls: "stale" },
  synced: { label: "已对齐", cls: "synced" },
  keep: { label: "保持原样", cls: "idle" },
};

export const TYPE_LABEL: Record<string, string> = {
  h1: "H1", h2: "H2", h3: "H3", p: "正文", list: "列表", footnote: "脚注",
  table: "表格", figure: "图片", formula: "公式", code: "代码",
};

export function blockKind(block: Block): string {
  if (block.type === "title") return block.level <= 1 ? "h1" : block.level === 2 ? "h2" : "h3";
  if (block.type === "paragraph") return "p";
  if (block.type === "list") return "list";
  if (block.type === "footnote") return "footnote";
  if (block.type === "table") return "table";
  if (block.type === "image") return "figure";
  if (block.type === "code") return "code";
  return "p";
}

export function hasSource(block: Block): boolean {
  return Boolean(block.source_text || block.items.length || block.caption.length || block.footnote.length || block.cells.some(cell => cell.text));
}

/** 图片/公式且没有任何可译文本时首版保持原样。 */
export function isKeep(block: Block, entry?: TranslationEntry): boolean {
  if (entry && (entry.text || entry.items.length || entry.caption.length || Object.keys(entry.cells).length)) return false;
  if (block.type === "image") return !block.caption.length && !block.source_text;
  return !hasSource(block);
}

export function entryState(block: Block, entry?: TranslationEntry): string {
  if (entry?.state) return entry.state;
  if (!entry) return "untranslated";
  if (entry.locked) return "locked";
  return entry.status || "untranslated";
}

export function entryText(entry?: TranslationEntry): string {
  if (!entry) return "";
  return entry.text || entry.caption?.join(" ") || entry.items?.join(" ") || Object.values(entry.cells || {}).join(" ") || "";
}

/** 从表格 block 的 cell 结构按行还原，译文缺失时回退空白。 */
export function tableRows(block: Block, entry?: TranslationEntry): string[][] {
  const rows: Record<number, { column: number; text: string }[]> = {};
  for (const cell of block.cells) {
    (rows[cell.row] ||= []).push({ column: cell.column, text: entry?.cells?.[cell.cell_id] || "" });
  }
  return Object.keys(rows).map(Number).sort((a, b) => a - b)
    .map(row => rows[row].sort((a, b) => a.column - b.column).map(item => item.text || "—"));
}

export function sourceTableRows(block: Block): string[][] {
  const rows: Record<number, { column: number; text: string }[]> = {};
  for (const cell of block.cells) (rows[cell.row] ||= []).push({ column: cell.column, text: cell.text });
  return Object.keys(rows).map(Number).sort((a, b) => a - b)
    .map(row => rows[row].sort((a, b) => a.column - b.column).map(item => item.text));
}

export function chapterOf(document: { blocks: Block[]; toc: { block_id: string; title: string }[] }, blockId: string): string {
  const index = document.blocks.findIndex(block => block.block_id === blockId);
  if (index < 0) return "";
  for (let i = index; i >= 0; i -= 1) {
    const b = document.blocks[i];
    if (b.type === "title" && b.level <= 2) return b.source_text || b.block_id;
  }
  return document.toc[0]?.title || "";
}
