import type { Block, DocumentGraph, TocNode } from "../types";

/** 单块到 Markdown 的可读投影，供对照阅读的 Markdown 视图使用。 */
export function blockMarkdown(block: Block): string {
  if (block.type === "title") return `${"#".repeat(Math.max(1, block.level))} ${block.source_text}`;
  if (block.type === "list") return block.items.map(item => `- ${item}`).join("\n");
  if (block.type === "table") return block.format?.table_html ? String(block.format.table_html) : `[table · ${block.cells.length} cells]`;
  if (block.type === "image") return `${block.resource_refs.map(ref => `![](${ref})`).join("\n")}\n${block.caption.join("\n")}`.trim();
  if (block.type === "equation") return `$$\n${block.source_text}\n$$`;
  return block.source_text || block.footnote.join("\n");
}

export function documentProjection(document: DocumentGraph | null): string {
  return document?.blocks.map(blockMarkdown).filter(Boolean).join("\n\n") ?? "";
}

export function flattenToc(nodes: TocNode[]): TocNode[] {
  return nodes.flatMap(node => [node, ...flattenToc(node.children)]);
}

export function tocDraft(nodes: TocNode[]): string {
  return flattenToc(nodes).map(node => `${"#".repeat(Math.max(1, Math.min(6, node.level)))} ${node.title}`).join("\n");
}

export function parseTocDraft(value: string): { level: number; title: string }[] {
  return value.split(/\n/)
    .map(line => line.trim().match(/^(#{1,6})\s+(.+)$/))
    .filter((match): match is RegExpMatchArray => Boolean(match))
    .map(match => ({ level: match[1].length, title: match[2].trim() }))
    .filter(entry => Boolean(entry.title));
}

/** 把目录条目转回可编辑的 Markdown 草稿，供项目基准回填。 */
export function formatTocDraft(entries: { level: number; title: string }[]): string {
  return entries.map(entry => `${"#".repeat(Math.max(1, Math.min(6, entry.level)))} ${entry.title}`).join("\n");
}
