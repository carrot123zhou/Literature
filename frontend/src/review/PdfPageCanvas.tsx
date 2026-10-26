import { useEffect, useMemo, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import "../pdfAssets";
import type { Block, FlowLinks } from "../types";
import { bboxRect, estimatedPageHeight, hasBBox } from "./geometry";

type Props = {
  pdf: PDFDocumentProxy;
  pageIndex: number;
  /** 目标渲染宽度（CSS 像素），由预览栏容器宽度决定。 */
  renderWidth: number;
  /** 是否真正渲染 canvas；未激活的页面只保留占位高度。 */
  active: boolean;
  pageRatio: number;
  blocks: Block[];
  selected: string;
  showBoxes: boolean;
  space: string;
  flowLinks: FlowLinks;
  onSelect: (blockId: string) => void;
};

/** 块类型的中文短标签，供框上的角标直接显示。 */
const TYPE_LABELS: Record<string, string> = {
  title: "标题", paragraph: "正文", table: "表格", image: "图片",
  list: "列表", equation: "公式", footnote: "脚注", code: "代码",
};

function typeLabel(block: Block): string {
  if (block.type === "title") return `H${block.level}`;
  return TYPE_LABELS[block.type] ?? block.type;
}

/**
 * 单页 PDF 渲染 + bbox 覆盖层。
 *
 * canvas 的像素尺寸乘 devicePixelRatio 保证清晰度，CSS 尺寸固定为 CSS 像素，
 * 因此覆盖层的百分比坐标与 canvas 完全对齐。
 */
export default function PdfPageCanvas({ pdf, pageIndex, renderWidth, active, pageRatio, blocks, selected, showBoxes, space, flowLinks, onSelect }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const taskRef = useRef<{ cancel: () => void; promise: Promise<unknown> } | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [failed, setFailed] = useState(false);
  const [painted, setPainted] = useState(false);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!active || renderWidth <= 0) {
      setPainted(false);
      return;
    }
    let cancelled = false;
    setFailed(false);
    setPainted(false);
    const run = async () => {
      // 等待上一次渲染真正结束（取消也是异步的），否则 pdfjs 会因同一 canvas
      // 并发渲染直接抛错，导致页面永久显示“渲染失败”。
      const previous = taskRef.current;
      if (previous) {
        try { await previous.promise; } catch { /* 已取消或失败 */ }
        if (taskRef.current === previous) taskRef.current = null;
      }
      if (cancelled) return;
      let page;
      try {
        page = await pdf.getPage(pageIndex + 1);
      } catch (error) {
        if (!cancelled) { console.error(`[PDF] 第 ${pageIndex + 1} 页获取失败`, error); setFailed(true); }
        return;
      }
      if (cancelled) return;
      const canvas = canvasRef.current;
      if (!canvas) return;
      try {
        const base = page.getViewport({ scale: 1 });
        const scale = renderWidth / base.width;
        const cssViewport = page.getViewport({ scale });
        const ratio = Math.min(window.devicePixelRatio || 1, 2);
        const outputViewport = page.getViewport({ scale: scale * ratio });
        // Decode into a private canvas, then publish the completed page in one
        // paint. Cancelling/resizing must not erase the visible page mid-render.
        const scratch = document.createElement("canvas");
        scratch.width = Math.max(1, Math.floor(outputViewport.width));
        scratch.height = Math.max(1, Math.floor(outputViewport.height));
        setSize({ width: cssViewport.width, height: cssViewport.height });
        const context = scratch.getContext("2d");
        if (!context) throw new Error("无法获取 canvas 2d 上下文");
        const task = page.render({ canvas: scratch, canvasContext: context, viewport: outputViewport });
        taskRef.current = task;
        await task.promise;
        if (!cancelled && canvasRef.current === canvas) {
          canvas.width = scratch.width;
          canvas.height = scratch.height;
          canvas.style.width = `${cssViewport.width}px`;
          canvas.style.height = `${cssViewport.height}px`;
          const visible = canvas.getContext("2d");
          if (!visible) throw new Error("无法获取 canvas 2d 上下文");
          visible.drawImage(scratch, 0, 0);
          setPainted(true);
          setFailed(false);
        }
        scratch.width = scratch.height = 0;
      } catch (error) {
        if (!cancelled) {
          console.error(`[PDF] 第 ${pageIndex + 1} 页渲染失败`, error);
          setFailed(true);
        }
      }
    };
    run();
    return () => { cancelled = true; taskRef.current?.cancel(); };
  }, [pdf, pageIndex, renderWidth, active, attempt]);

  const height = active && size.height > 0 ? size.height : estimatedPageHeight(undefined, renderWidth, pageRatio);
  const width = active && size.width > 0 ? size.width : renderWidth;

  // 本页上的续上页区域：源块在上一页，规范化时那个空占位块已丢弃，这里补画出来。
  const regions = useMemo(
    () => Object.entries(flowLinks).filter(([, value]) => value.next_region?.page_index === pageIndex),
    [flowLinks, pageIndex],
  );

  return (
    <div className="pdf-page" data-page-index={pageIndex} data-rendered={active && painted ? "true" : "false"} style={{ width, height }}>
      {active && <canvas ref={canvasRef} />}
      {active && !painted && !failed && <div role="status" style={{ position: "absolute", top: 12, left: 12 }}>正在渲染第 {pageIndex + 1} 页…</div>}
      {active && failed && <div className="pdf-page-error" style={{ position: "absolute", inset: 0, zIndex: 3 }}>第 {pageIndex + 1} 页渲染失败 <button onClick={() => setAttempt(value => value + 1)}>重试</button></div>}
      {!active && <div className="pdf-page-placeholder">{pageIndex + 1}</div>}
      <div className={`pdf-overlay${showBoxes ? "" : " hide-boxes"}`}>
        {blocks.filter(hasBBox).map(block => {
          const rect = bboxRect(block.bbox, space);
          const current = selected === block.block_id;
          const continues = Boolean(flowLinks[block.block_id]?.next_region);
          return (
            <button
              key={block.block_id}
              type="button"
              data-block-id={block.block_id}
              className={`bbox bbox-${block.type}${current ? " selected" : ""}`}
              style={{ left: `${rect.left}%`, top: `${rect.top}%`, width: `${rect.width}%`, height: `${rect.height}%` }}
              title={`${block.block_id} · ${block.type}${block.level ? ` H${block.level}` : ""}${continues ? " · 跨页续接（续下页）" : ""}`}
              aria-label={block.source_text || block.block_id}
              onClick={() => onSelect(block.block_id)}
            >
              <span className="bbox-label" aria-hidden="true">{typeLabel(block)}</span>
            </button>
          );
        })}
        {regions.map(([sourceId, value]) => {
          const region = value.next_region!;
          const rect = bboxRect(region.bbox, space);
          const current = selected === sourceId;
          // 续页区域不是独立块，但视觉上与普通正文块完全一致；只有虚线连线暗示跨页关系。
          return (
            <button
              key={`flow-region-${sourceId}`}
              type="button"
              data-flow-region={sourceId}
              className={`bbox bbox-paragraph${current ? " selected" : ""}`}
              style={{ left: `${rect.left}%`, top: `${rect.top}%`, width: `${rect.width}%`, height: `${rect.height}%` }}
              title={`${sourceId} · 跨页续接（续上页）`}
              aria-label={`第 ${region.page_index + 1} 页的跨页续上页区域`}
              onClick={() => onSelect(sourceId)}
            >
              <span className="bbox-label" aria-hidden="true">正文</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
