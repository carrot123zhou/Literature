import { useCallback, useEffect, useMemo, useRef, useState } from "react";

/**
 * 可变高度列表窗口化（阅读/结构结果共用）。
 *
 * - 只渲染滚动可视区附近的一小段，DOM 数量有界（1000 块也不会全量挂载）。
 * - 固定块（正在编辑/明确 pin 的块）若在窗口外，使用绝对定位悬浮渲染，保证草稿不丢。
 * - 跳转到未渲染块：先按已测高度估算偏移设置 scrollTop，再让窗口覆盖目标。
 */

const ESTIMATED_HEIGHT = 170;
const OVERSCAN_PX = 600;

export type VirtualWindow = {
  scrollerRef: React.RefObject<HTMLDivElement | null>;
  onScroll: () => void;
  rangeStart: number;
  rangeEnd: number;
  overlay: string[];
  measure: (id: string) => (node: HTMLElement | null) => void;
  scrollToBlock: (id: string) => void;
  offsets: Record<string, number>;
  heightOf: (id: string) => number;
  totalHeight: number;
};

export function useVirtualWindow(ids: string[], pinned: string[]): VirtualWindow {
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const [heights, setHeights] = useState<Record<string, number>>({});
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(900);
  const pinnedKey = pinned.join("|");
  const pinnedSet = useMemo(() => new Set(pinned), [pinnedKey]);

  const { offsets, totalHeight } = useMemo(() => {
    const map: Record<string, number> = {};
    let total = 0;
    for (const id of ids) {
      map[id] = total;
      total += heights[id] ?? ESTIMATED_HEIGHT;
    }
    return { offsets: map, totalHeight: total };
  }, [ids, heights]);

  const heightOf = useCallback((id: string) => heights[id] ?? ESTIMATED_HEIGHT, [heights]);

  const onScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    setScrollTop(el.scrollTop);
    setViewportHeight(el.clientHeight);
  }, []);

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    // 与真实 DOM 同步（挂载/重入/外部滚动后，state 不能停在旧值）。
    if (Math.abs(el.scrollTop - scrollTop) > 1) setScrollTop(el.scrollTop);
    if (el.clientHeight && Math.abs(el.clientHeight - viewportHeight) > 1) setViewportHeight(el.clientHeight);
  });

  const { rangeStart, rangeEnd, overlay } = useMemo(() => {
    const top = Math.max(0, scrollTop - OVERSCAN_PX);
    const bottom = scrollTop + viewportHeight + OVERSCAN_PX;
    let start = 0;
    let end = ids.length;
    for (let index = 0; index < ids.length; index++) {
      const id = ids[index];
      if (offsets[id] + heightOf(id) < top) continue;
      start = index;
      break;
    }
    end = start;
    for (let index = start; index < ids.length; index++) {
      const id = ids[index];
      if (offsets[id] > bottom) break;
      end = index + 1;
    }
    const inRange = new Set(ids.slice(start, end));
    const overlayIds = [...pinnedSet].filter(id => !inRange.has(id) && id in offsets);
    return { rangeStart: start, rangeEnd: end, overlay: overlayIds };
  }, [ids, offsets, scrollTop, viewportHeight, heightOf, pinnedKey]);

  const measure = useCallback((id: string) => (node: HTMLElement | null) => {
    if (!node) return;
    const height = Math.round(node.getBoundingClientRect().height);
    if (height <= 0) return;
    setHeights(current => (Math.abs((current[id] ?? 0) - height) > 1 ? { ...current, [id]: height } : current));
  }, []);

  const scrollToBlock = useCallback((id: string) => {
    const el = scrollerRef.current;
    if (!el || !(id in offsets)) return;
    el.scrollTop = Math.max(0, offsets[id] - 8);
    setScrollTop(el.scrollTop);
  }, [offsets]);

  return { scrollerRef, onScroll, rangeStart, rangeEnd, overlay, measure, scrollToBlock, offsets, heightOf, totalHeight };
}
