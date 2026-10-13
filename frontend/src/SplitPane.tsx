import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 可拖动调整宽度的分栏容器。
 *
 * 所有栅格用 fr 比例保存，拖动分隔条只调整相邻两栏的比例和，因此窗口缩放时
 * 布局按比例自适应。比例写入 localStorage；双击分隔条或按 Home 键恢复默认。
 */

const HANDLE_PX = 8;
const KEYBOARD_STEP = 0.02;

type Props = {
  /** localStorage 键；不同工作区各自记忆宽度。 */
  storageKey: string;
  /** 默认比例，长度需与 children 相同，合计为 1。 */
  initial: number[];
  /** 每栏最小像素宽度，拖动时用于夹取。 */
  minPx?: number[];
  /** 分隔条的 aria-label，便于读屏与自动化定位。 */
  handleLabels?: string[];
  children: React.ReactNode[];
  className?: string;
};

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.max(minimum, Math.min(maximum, value));
}

function useStoredSizes(storageKey: string, count: number, initial: number[]) {
  const [sizes, setSizes] = useState<number[]>(() => {
    try {
      const raw = JSON.parse(window.localStorage.getItem(storageKey) || "[]");
      if (Array.isArray(raw) && raw.length === count && raw.every(value => typeof value === "number" && value > 0)) {
        const total = raw.reduce((sum, value) => sum + value, 0);
        if (total > 0) return raw.map(value => value / total);
      }
    } catch { /* 损坏的记忆就退回默认比例。 */ }
    return initial;
  });
  useEffect(() => {
    try { window.localStorage.setItem(storageKey, JSON.stringify(sizes)); } catch { /* 忽略无痕模式。 */ }
  }, [storageKey, sizes]);
  return [sizes, setSizes] as const;
}

export default function SplitPane({ storageKey, initial, minPx = [], handleLabels = [], children, className = "" }: Props) {
  const count = children.length;
  const containerRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ index: number; startX: number; start: number[]; usablePx: number } | null>(null);
  const [sizes, setSizes] = useStoredSizes(storageKey, count, initial);

  const applyDrag = useCallback((index: number, deltaPx: number) => {
    const state = drag.current;
    if (!state) return;
    const total = state.start[index] + state.start[index + 1];
    const usable = Math.max(1, state.usablePx);
    const minimumLeft = (minPx[index] ?? 0) / usable;
    const minimumRight = (minPx[index + 1] ?? 0) / usable;
    const left = clamp(state.start[index] + deltaPx / usable, minimumLeft, total - minimumRight);
    setSizes(previous => {
      const next = [...previous];
      next[index] = left;
      next[index + 1] = total - left;
      return next;
    });
  }, [minPx, setSizes]);

  const onPointerDown = (index: number) => (event: React.PointerEvent<HTMLDivElement>) => {
    const width = containerRef.current?.clientWidth || 0;
    if (!width) return;
    event.preventDefault();
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    drag.current = {
      index,
      startX: event.clientX,
      start: [...sizes],
      usablePx: Math.max(1, width - HANDLE_PX * (count - 1)),
    };
    document.body.classList.add("is-resizing");
  };

  const endDrag = () => {
    drag.current = null;
    document.body.classList.remove("is-resizing");
  };

  const nudge = (index: number, direction: -1 | 1) => {
    const state = { index, startX: 0, start: [...sizes], usablePx: Math.max(1, (containerRef.current?.clientWidth || 0) - HANDLE_PX * (count - 1)) };
    drag.current = state;
    applyDrag(index, direction * KEYBOARD_STEP * state.usablePx);
    drag.current = null;
  };

  const template = sizes
    .flatMap((size, index) => (index < count - 1 ? [`minmax(${minPx[index] ?? 0}px,${size * 100}fr)`, `${HANDLE_PX}px`] : [`minmax(${minPx[index] ?? 0}px,${size * 100}fr)`]))
    .join(" ");

  return (
    <div ref={containerRef} className={`split-pane ${className}`.trim()} style={{ gridTemplateColumns: template, columnGap: 0 }}>
      {children.map((child, index) => (
        <FragmentRow key={index}>
          {child}
          {index < count - 1 && (
            <div
              className="split-handle"
              role="separator"
              aria-orientation="vertical"
              aria-label={handleLabels[index] ?? `调整第 ${index + 1} 栏宽度`}
              tabIndex={0}
              onPointerDown={onPointerDown(index)}
              onPointerMove={event => { if (drag.current) applyDrag(drag.current.index, event.clientX - drag.current.startX); }}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
              onDoubleClick={() => setSizes(initial)}
              onKeyDown={event => {
                if (event.key === "ArrowLeft") { event.preventDefault(); nudge(index, -1); }
                else if (event.key === "ArrowRight") { event.preventDefault(); nudge(index, 1); }
                else if (event.key === "Home") { event.preventDefault(); setSizes(initial); }
              }}
            />
          )}
        </FragmentRow>
      ))}
    </div>
  );
}

// 用 Fragment 包住「面板 + 分隔条」，避免额外 DOM 层级破坏 grid 子项语义。
function FragmentRow({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}
