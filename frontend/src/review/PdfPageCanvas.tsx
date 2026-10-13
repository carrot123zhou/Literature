import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import "../pdfAssets";
import type { Block } from "../types";
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
  onSelect: (blockId: string) => void;
};

/**
 * 单页 PDF 渲染 + bbox 覆盖层。
 *
 * canvas 的像素尺寸乘 devicePixelRatio 保证清晰度，CSS 尺寸固定为 CSS 像素，
 * 因此覆盖层的百分比坐标与 canvas 完全对齐。
 */
export default function PdfPageCanvas({ pdf, pageIndex, renderWidth, active, pageRatio, blocks, selected, showBoxes, space, onSelect }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const taskRef = useRef<{ cancel: () => void; promise: Promise<unknown> } | null>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!active || renderWidth <= 0) return;
    let cancelled = false;
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
        canvas.width = Math.max(1, Math.floor(outputViewport.width));
        canvas.height = Math.max(1, Math.floor(outputViewport.height));
        canvas.style.width = `${cssViewport.width}px`;
        canvas.style.height = `${cssViewport.height}px`;
        setSize({ width: cssViewport.width, height: cssViewport.height });
        const context = canvas.getContext("2d");
        if (!context) throw new Error("无法获取 canvas 2d 上下文");
        const task = page.render({ canvas, canvasContext: context, viewport: outputViewport });
        taskRef.current = task;
        await task.promise;
        if (!cancelled) setFailed(false);
      } catch (error) {
        if (!cancelled) {
          console.error(`[PDF] 第 ${pageIndex + 1} 页渲染失败`, error);
          setFailed(true);
        }
      }
    };
    run();
    return () => { cancelled = true; taskRef.current?.cancel(); };
  }, [pdf, pageIndex, renderWidth, active]);

  const height = active && size.height > 0 ? size.height : estimatedPageHeight(undefined, renderWidth, pageRatio);
  const width = active && size.width > 0 ? size.width : renderWidth;

  return (
    <div className="pdf-page" data-page-index={pageIndex} style={{ width, height }}>
      {active && <canvas ref={canvasRef} />}
      {active && failed && <div className="pdf-page-error" style={{ position: "absolute", inset: 0 }}>第 {pageIndex + 1} 页渲染失败</div>}
      {!active && <div className="pdf-page-placeholder">{pageIndex + 1}</div>}
      <div className={`pdf-overlay${showBoxes ? "" : " hide-boxes"}`}>
        {blocks.filter(hasBBox).map(block => {
          const rect = bboxRect(block.bbox, space);
          const current = selected === block.block_id;
          return (
            <button
              key={block.block_id}
              type="button"
              data-block-id={block.block_id}
              className={`bbox bbox-${block.type}${current ? " selected" : ""}`}
              style={{ left: `${rect.left}%`, top: `${rect.top}%`, width: `${rect.width}%`, height: `${rect.height}%` }}
              title={`${block.block_id} · ${block.type}${block.level ? ` H${block.level}` : ""}`}
              aria-label={block.source_text || block.block_id}
              onClick={() => onSelect(block.block_id)}
            />
          );
        })}
      </div>
    </div>
  );
}
