import type { Block } from "../types";

export type BlockSource = { page_index: number; bbox: number[]; index: number };

/** 人工合并保留的原页来源；无有效来源时沿用块自己的位置。 */
export function blockSources(block: Block): BlockSource[] {
  const raw = block.raw?.structure_edit_sources;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((value, index) => {
    if (!value || typeof value !== "object") return [];
    const source = value as { page_index?: unknown; bbox?: unknown };
    if (!Number.isInteger(source.page_index) || (source.page_index as number) < 0) return [];
    const bbox = Array.isArray(source.bbox) && source.bbox.length === 4 && source.bbox.every(Number.isFinite) ? source.bbox as number[] : [];
    return [{ page_index: source.page_index as number, bbox, index }];
  });
}
