import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Block, FlowLinks, PageMeta } from "../types";
import PdfPageCanvas from "./PdfPageCanvas";
import type { PdfState } from "./usePdfDocument";
import { estimatedPageHeight } from "./geometry";
import { blockSources } from "./blockSources";

export type FocusRequest = { blockId: string; nonce: number; pageIndex?: number; sourceIndex?: number };

type Props = {
  pdf: PdfState;
  pages: PageMeta[];
  blocks: Block[];
  space: string;
  selected: string;
  flowLinks: FlowLinks;
  onSelect: (blockId: string, pageIndex?: number, sourceIndex?: number) => void;
  onActiveBlock: (blockId: string) => void;
  onPageChange?: (page: number) => void;
  focus: FocusRequest | null;
  action?: React.ReactNode;
};

const WINDOW_BEFORE = 2;
const WINDOW_AFTER = 3;
const PAGE_GAP = 24;

/** 图例：与框/角标配色一一对应。跨页续接单独列出，因为它不是块类型而是关系。 */
const LEGEND: { key: string; label: string }[] = [
  { key: "title", label: "标题" },
  { key: "paragraph", label: "正文" },
  { key: "table", label: "表格" },
  { key: "image", label: "图片" },
  { key: "equation", label: "公式" },
  { key: "footnote", label: "脚注" },
  { key: "flow", label: "跨页续接" },
];

type Connector = { id: string; x1: number; y1: number; x2: number; y2: number; gapY: number };

/** 连续滚动的原页预览：只渲染当前页附近的 canvas，其余页面用几何占位撑住滚动条。 */
export default function PdfPane({ pdf, pages, blocks, space, selected, flowLinks, onSelect, onPageChange, onActiveBlock, focus, action }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stackRef = useRef<HTMLDivElement>(null);
  const currentRef = useRef(0);
  const activeRef = useRef("");
  const [renderWidth, setRenderWidth] = useState(0);
  const [zoom, setZoom] = useState(1);
  const [showBoxes, setShowBoxes] = useState(true);
  const [currentPage, setCurrentPage] = useState(0);
  const [pinnedPage, setPinnedPage] = useState<number | null>(null);
  const [defaultRatio, setDefaultRatio] = useState(1.414);
  const [naturalWidth, setNaturalWidth] = useState(0);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [legendOpen, setLegendOpen] = useState(false);
  const legendRef = useRef<HTMLDivElement>(null);

  const byPage = useMemo(() => {
    const map = new Map<number, Block[]>();
    for (const block of blocks) {
      const sources = blockSources(block);
      const locations = sources.length > 1 ? sources.map(source => ({ ...block, page_index: source.page_index, bbox: source.bbox, sourceIndex: source.index })) : [block];
      for (const location of locations) {
        const list = map.get(location.page_index);
        if (list) list.push(location);
        else map.set(location.page_index, [location]);
      }
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
        if (!cancelled && viewport.width > 0) { setDefaultRatio(viewport.height / viewport.width); setNaturalWidth(viewport.width); }
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

  const gotoPage = useCallback((index: number) => {
    if (index < 0 || index >= pages.length) return;
    setPinnedPage(index);
    window.setTimeout(() => containerRef.current?.querySelector<HTMLElement>(`.pdf-page[data-page-index="${index}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" }), 80);
  }, [pages.length]);

  // 外部选中（结果栏、目录）→ 先把目标页纳入渲染窗口，再滚动到 bbox。
  useEffect(() => {
    if (!focus || !pdf.document) return;
    const block = blocks.find(item => item.block_id === focus.blockId);
    const targetPage = focus.pageIndex ?? block?.page_index;
    if (targetPage === undefined) return;
    setPinnedPage(targetPage);
    const timer = window.setTimeout(() => {
      const container = containerRef.current;
      if (!container) return;
      const page = container.querySelector<HTMLElement>(`.pdf-page[data-page-index="${targetPage}"]`);
      const source = focus.sourceIndex === undefined ? "" : `[data-source-index="${focus.sourceIndex}"]`;
      const target = page?.querySelector<HTMLElement>(`.bbox[data-block-id="${block?.block_id}"]${source}`) ?? page;
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

  // 跨页续接连线：与 MinerU 一致，从源块左下角沿空白折到续页区域右上角，不穿过正文。
  const measureConnectors = useCallback(() => {
    const stack = stackRef.current;
    if (!stack || !showBoxes) { setConnectors([]); return; }
    const base = stack.getBoundingClientRect();
    const next: Connector[] = [];
    for (const [blockId, value] of Object.entries(flowLinks)) {
      if (!value.next_region) continue;
      const from = stack.querySelector<HTMLElement>(`.bbox[data-block-id="${blockId}"]`);
      const to = stack.querySelector<HTMLElement>(`.bbox[data-flow-region="${blockId}"]`);
      if (!from || !to) continue;
      const a = from.getBoundingClientRect();
      const b = to.getBoundingClientRect();
      // 与页间空白对齐的折线：两段竖线分别贴着源块左缘与续页区域右缘，横线落在页缝里。
      const fromPage = from.closest<HTMLElement>(".pdf-page")?.getBoundingClientRect();
      const toPage = to.closest<HTMLElement>(".pdf-page")?.getBoundingClientRect();
      const gapTop = fromPage ? fromPage.bottom : a.bottom;
      const gapBottom = toPage ? toPage.top : b.top;
      const gapY = gapTop + Math.max(0, gapBottom - gapTop) / 2;
      next.push({
        id: `${blockId}->region`,
        x1: a.left - base.left,
        y1: a.bottom - base.top,
        x2: b.right - base.left,
        y2: b.top - base.top,
        gapY: gapY - base.top,
      });
    }
    setConnectors(next);
  }, [flowLinks, showBoxes]);

  useEffect(() => {
    const stack = stackRef.current;
    if (!stack) return;
    const run = () => window.requestAnimationFrame(measureConnectors);
    run();
    // canvas 从占位高度变为真实高度会改变 stack 尺寸；观察它即可覆盖页面懒渲染。
    const observer = new ResizeObserver(run);
    observer.observe(stack);
    return () => observer.disconnect();
  }, [measureConnectors, renderWidth, windowRange.from, windowRange.to]);

  // 图例浮层：点外部或 Esc 关闭，避免在工具栏里常驻一条横幅。
  useEffect(() => {
    if (!legendOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!legendRef.current?.contains(event.target as Node)) setLegendOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") setLegendOpen(false); };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [legendOpen]);

  const zoomPercent = naturalWidth > 0 && renderWidth > 0 ? Math.round((renderWidth / naturalWidth) * 100) : 0;

  if (pdf.error) {
    return <section className="pdf-panel mineru-preview-pane"><div className="viewer-toolbar"><strong>源文件预览</strong><div>{action}</div></div><div className="empty">无法加载 PDF：{pdf.error}</div></section>;
  }

  return (
    <section className="pdf-panel mineru-preview-pane" data-current-page={currentPage + 1} data-page-count={pages.length}>
      <div className="viewer-toolbar">
        <strong>源文件预览</strong>
        <div className="review-viewer-controls">{action}
          <div className="page-stepper">
            <button type="button" aria-label="上一页" disabled={currentPage <= 0} onClick={() => gotoPage(currentPage - 1)}>‹</button>
            <input aria-label="跳转源文件页" type="number" min={1} max={pages.length} key={currentPage} defaultValue={currentPage + 1} onKeyDown={event => {
              if (event.key !== "Enter") return;
              gotoPage(Number(event.currentTarget.value) - 1);
            }}/><span>/ {pages.length}</span>
            <button type="button" aria-label="下一页" disabled={currentPage >= pages.length - 1} onClick={() => gotoPage(currentPage + 1)}>›</button>
          </div>
          <label>缩放 <select aria-label="原页缩放" value={zoom} onChange={event => setZoom(Number(event.target.value))}><option value={1}>适应宽度</option><option value={1.25}>125%</option><option value={1.5}>150%</option><option value={2}>200%</option></select></label>
          {zoomPercent > 0 && <span className="zoom-readout" title="当前渲染宽度占 PDF 原始宽度的比例">{zoomPercent}%</span>}
          <button className={showBoxes ? "is-active" : ""} aria-pressed={showBoxes} title="显示或隐藏结构框" onClick={() => setShowBoxes(value => !value)}>结构框</button>
        </div>
        {/* 图例放在滚动区之外，浮层不会被 overflow 裁剪 */}
        <div className="legend-menu" ref={legendRef}>
          <button type="button" className={legendOpen ? "is-active" : ""} aria-haspopup="true" aria-expanded={legendOpen} title="结构框配色图例" onClick={() => setLegendOpen(value => !value)}>图例</button>
          {legendOpen && <div className="legend-panel" role="group" aria-label="结构框图例">
            {LEGEND.map(item => <span key={item.key} className={`legend-item legend-${item.key}`}><i/>{item.label}</span>)}
          </div>}
        </div>
      </div>
      <div className="pdf-scroll" ref={containerRef} onScroll={locate}>
        <div className="pdf-stack" ref={stackRef}>
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
              flowLinks={flowLinks}
              onSelect={onSelect}
            />
          ) : (
            <div className="pdf-page pdf-page-placeholder" key={page.page_index} data-page-index={page.page_index} style={{ width: renderWidth || undefined, height: renderWidth > 0 ? estimatedPageHeight(undefined, renderWidth, ratios[index] ?? defaultRatio) : undefined }}>{page.page_index + 1}</div>
          )))}
          {showBoxes && connectors.length > 0 && <svg className="flow-overlay" aria-hidden="true" width="100%" height="100%">
            {connectors.map(item => <g key={item.id} className="flow-group">
              <path className="flow-line" d={`M ${item.x1} ${item.y1} V ${item.gapY} H ${item.x2} V ${item.y2}`}/>
              <circle className="flow-dot" cx={item.x1} cy={item.y1} r={3.2}/>
              <circle className="flow-dot" cx={item.x2} cy={item.y2} r={3.2}/>
            </g>)}
          </svg>}
        </div>
      </div>
    </section>
  );
}
