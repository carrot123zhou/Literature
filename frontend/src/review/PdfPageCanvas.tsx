import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
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
  space: string;
  onSelect: (blockId: string) => void;
};

/**
 * 单页 PDF 渲染 + bbox 覆盖层。
 *
 * canvas 的像素尺寸乘 devicePixelRatio 保证清晰度，CSS 尺寸固定为 CSS 像素，
 * 因此覆盖层的百分比坐标与 canvas 完全对齐。
 */
export default function PdfPageCanvas({ pdf, pageIndex, renderWidth, active, pageRatio, blocks, selected, space, onSelect }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!active || renderWidth <= 0) return;
    let cancelled = false;
    let task: { cancel: () => void; promise: Promise<unknown> } | null = null;
    (async () => {
      const page = await pdf.getPage(pageIndex + 1);
      if (cancelled) return;
      const base = page.getViewport({ scale: 1 });
      const scale = renderWidth / base.width;
      const cssViewport = page.getViewport({ scale });
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const outputViewport = page.getViewport({ scale: scale * ratio });
      const canvas = canvasRef.current;
      if (cancelled || !canvas) return;
      canvas.width = Math.max(1, Math.floor(outputViewport.width));
      canvas.height = Math.max(1, Math.floor(outputViewport.height));
      canvas.style.width = `${cssViewport.width}px`;
      canvas.style.height = `${cssViewport.height}px`;
      setSize({ width: cssViewport.width, height: cssViewport.height });
      setFailed(false);
      task = page.render({ canvas, canvasContext: canvas.getContext("2d")!, viewport: outputViewport });
      await task.promise;
    })().catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; task?.cancel(); };
  }, [pdf, pageIndex, renderWidth, active]);

  const height = active && size.height > 0 ? size.height : estimatedPageHeight(undefined, renderWidth, pageRatio);
  const width = active && size.width > 0 ? size.width : renderWidth;

  return (
    <div className="pdf-page" data-page-index={pageIndex} style={{ width, height }}>
      {active && !failed && <canvas ref={canvasRef} />}
      {failed && <div className="pdf-page-error">第 {pageIndex + 1} 页渲染失败</div>}
      {!active && <div className="pdf-page-placeholder">{pageIndex + 1}</div>}
      <div className="pdf-overlay">
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
