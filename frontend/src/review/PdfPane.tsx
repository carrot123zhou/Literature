import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Block, PageMeta } from "../types";
import PdfPageCanvas from "./PdfPageCanvas";
import type { PdfState } from "./usePdfDocument";
import { estimatedPageHeight } from "./geometry";

export type FocusRequest = { blockId: string; nonce: number; pageIndex?: number };

type Props = {
  pdf: PdfState;
  pages: PageMeta[];
  blocks: Block[];
  space: string;
  selected: string;
  onSelect: (blockId: string) => void;
  onActiveBlock: (blockId: string) => void;
  onPageChange?: (page: number) => void;
  focus: FocusRequest | null;
  action?: React.ReactNode;
};

const WINDOW_BEFORE = 2;
const WINDOW_AFTER = 3;
const PAGE_GAP = 24;

/** 连续滚动的原页预览：只渲染当前页附近的 canvas，其余页面用几何占位撑住滚动条。 */
export default function PdfPane({ pdf, pages, blocks, space, selected, onSelect, onPageChange, onActiveBlock, focus, action }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const currentRef = useRef(0);
  const activeRef = useRef("");
  const [renderWidth, setRenderWidth] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [showBoxes, setShowBoxes] = useState(true);
  const [currentPage, setCurrentPage] = useState(0);
  const [pinnedPage, setPinnedPage] = useState<number | null>(null);
  const [defaultRatio, setDefaultRatio] = useState(1.414);

  const byPage = useMemo(() => {
    const map = new Map<number, Block[]>();
    for (const block of blocks) {
      const list = map.get(block.page_index);
      if (list) list.push(block);
      else map.set(block.page_index, [block]);
    }
    return map;
  }, [blocks]);

  const ratios = useMemo(() => pages.map(page => (page.width > 0 && page.height > 0 ? page.height / page.width : defaultRatio)), [pages, defaultRatio]);

  // 页面几何优先取 layout.json 的 page_size；缺失时用首页宽高比兜底。
  useEffect(() => {
    const documentProxy = pdf.document;
    if (!documentProxy) return;
    let cancelled = false;
    documentProxy.getPage(1)
      .then(page => {
        const viewport = page.getViewport({ scale: 1 });
        if (!cancelled && viewport.width > 0) setDefaultRatio(viewport.height / viewport.width);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [pdf.document]);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const measure = () => setRenderWidth(Math.max(0, Math.floor((element.clientWidth - 48) * zoom)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [zoom]);

  const pageHeight = useCallback((index: number) => {
    const ratio = ratios[index] ?? defaultRatio;
    return estimatedPageHeight(undefined, renderWidth, ratio) + PAGE_GAP;
  }, [ratios, defaultRatio, renderWidth]);

  const locate = useCallback(() => {
    const element = containerRef.current;
    if (!element || renderWidth <= 0 || pages.length === 0) return;
    const middle = element.scrollTop + element.clientHeight * 0.42;
    let offset = PAGE_GAP;
    let found = 0;
    for (let index = 0; index < pages.length; index += 1) {
      const height = pageHeight(index);
      if (middle < offset + height) { found = index; break; }
      offset += height;
      found = index;
    }
    if (found !== currentRef.current) {
      currentRef.current = found;
      setCurrentPage(found);
      setPinnedPage(null);
    }
  }, [pages.length, renderWidth, pageHeight]);

  useEffect(() => { window.requestAnimationFrame(locate); }, [locate]);
  useEffect(() => { onPageChange?.(currentPage + 1); }, [currentPage, onPageChange]);

  // 外部选中（结果栏、目录）→ 先把目标页纳入渲染窗口，再滚动到 bbox。
  useEffect(() => {
    if (!focus || !pdf.document) return;
    const block = blocks.find(item => item.block_id === focus.blockId);
    const targetPage = block?.page_index ?? focus.pageIndex;
    if (targetPage === undefined) return;
    setPinnedPage(targetPage);
    const timer = window.setTimeout(() => {
      const container = containerRef.current;
      if (!container) return;
      const target = container.querySelector<HTMLElement>(`.bbox[data-block-id="${block?.block_id}"]`)
        ?? container.querySelector<HTMLElement>(`.pdf-page[data-page-index="${targetPage}"]`);
      target?.scrollIntoView({ block: "center", behavior: "smooth" });
    }, 80);
    return () => window.clearTimeout(timer);
  }, [focus, pdf.document, blocks, renderWidth]);

  // 滚动停止后才判定当前阅读块，避免滚动过程中反复触发上层重渲染。
  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    let timer = 0;
    const detect = () => {
      const container = element.getBoundingClientRect();
      const middle = container.top + container.height * 0.42;
      let best = "";
      let bestDistance = Number.POSITIVE_INFINITY;
      element.querySelectorAll<HTMLElement>(".bbox").forEach(candidate => {
        const rect = candidate.getBoundingClientRect();
        const distance = Math.abs(rect.top + rect.height / 2 - middle);
        if (distance < bestDistance) { bestDistance = distance; best = candidate.dataset.blockId ?? ""; }
      });
      if (best && best !== activeRef.current) { activeRef.current = best; onActiveBlock(best); }
    };
    const onScroll = () => { window.clearTimeout(timer); timer = window.setTimeout(detect, 160); };
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => { element.removeEventListener("scroll", onScroll); window.clearTimeout(timer); };
  }, [onActiveBlock]);

  const windowRange = useMemo(() => {
    const anchor = pinnedPage ?? currentPage;
    return { from: Math.max(0, anchor - WINDOW_BEFORE), to: Math.min(pages.length - 1, anchor + WINDOW_AFTER) };
  }, [pinnedPage, currentPage, pages.length]);

  if (pdf.error) {
    return <section className="pdf-panel mineru-preview-pane"><div className="viewer-toolbar"><strong>源文件预览</strong><div>{action}</div></div><div className="empty">无法加载 PDF：{pdf.error}</div></section>;
  }

  return (
    <section className="pdf-panel mineru-preview-pane" data-current-page={currentPage + 1} data-page-count={pages.length}>
      <div className="viewer-toolbar">
        <strong>源文件预览</strong>
        <div className="review-viewer-controls">{action}
          <label>页 <input aria-label="跳转源文件页" type="number" min={1} max={pages.length} key={currentPage} defaultValue={currentPage + 1} onKeyDown={event => {
            if (event.key !== "Enter") return;
            const index = Number(event.currentTarget.value) - 1;
            if (index < 0 || index >= pages.length) return;
            setPinnedPage(index);
            window.setTimeout(() => containerRef.current?.querySelector<HTMLElement>(`.pdf-page[data-page-index="${index}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" }), 80);
          }}/><span>/ {pages.length}</span></label>
          <select aria-label="原页缩放" value={zoom} onChange={event => setZoom(Number(event.target.value))}><option value={1}>适应宽度</option><option value={1.25}>125%</option><option value={1.5}>150%</option><option value={2}>200%</option></select>
          <button className={showBoxes ? "is-active" : ""} aria-pressed={showBoxes} title="显示或隐藏结构框" onClick={() => setShowBoxes(value => !value)}>结构框</button>
        </div>
      </div>
      <div className="pdf-scroll" ref={containerRef} onScroll={locate}>
        <div className="pdf-stack">
          {pages.map((page, index) => (pdf.document ? (
            <PdfPageCanvas
              key={page.page_index}
              pdf={pdf.document}
              pageIndex={page.page_index}
              renderWidth={renderWidth}
              active={renderWidth > 0 && index >= windowRange.from && index <= windowRange.to}
              pageRatio={ratios[index] ?? defaultRatio}
              blocks={byPage.get(page.page_index) ?? []}
              selected={selected}
              showBoxes={showBoxes}
              space={space}
              onSelect={onSelect}
            />
          ) : (
            <div className="pdf-page pdf-page-placeholder" key={page.page_index} data-page-index={page.page_index} style={{ width: renderWidth || undefined, height: renderWidth > 0 ? estimatedPageHeight(undefined, renderWidth, ratios[index] ?? defaultRatio) : undefined }}>{page.page_index + 1}</div>
          )))}
        </div>
      </div>
    </section>
  );
}
