import { useEffect, useRef, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import { apiHeaders, apiUrl } from "./api";
import { pdfAssetOptions } from "./pdfAssets";
import type { Block } from "./types";

type Props = { projectId: string; page: number; blocks: Block[]; selected?: string; onSelect: (id: string) => void; onPageCount: (count: number) => void };

export default function PdfPage({ projectId, page, blocks, selected, onSelect, onPageCount }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ width: 1, height: 1, sourceWidth: 1, sourceHeight: 1 });
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const task = pdfjs.getDocument({ url: apiUrl(`/projects/${projectId}/source.pdf`), httpHeaders: apiHeaders(), ...pdfAssetOptions });
        const pdf = await task.promise;
        onPageCount(pdf.numPages);
        const pdfPage = await pdf.getPage(Math.max(1, Math.min(page, pdf.numPages)));
        const viewport = pdfPage.getViewport({ scale: 1.25 });
        const source = pdfPage.getViewport({ scale: 1 });
        if (cancelled || !canvas.current) return;
        canvas.current.width = viewport.width;
        canvas.current.height = viewport.height;
        setSize({ width: viewport.width, height: viewport.height, sourceWidth: source.width, sourceHeight: source.height });
        await pdfPage.render({ canvas: canvas.current, canvasContext: canvas.current.getContext("2d")!, viewport }).promise;
      } catch (reason) { if (!cancelled) setError(String(reason)); }
    })();
    return () => { cancelled = true; };
  }, [projectId, page, onPageCount]);
  return <div className="pdf-stage" style={{ width: size.width, height: size.height }}>
    {error ? <div className="empty">无法加载 PDF：{error}</div> : <canvas ref={canvas}/>}
    {blocks.filter(block => block.bbox.length === 4).map(block => {
      const [x0,y0,x1,y1] = block.bbox;
      return <button key={block.block_id} className={`bbox ${selected === block.block_id ? "selected" : ""}`} onClick={() => onSelect(block.block_id)} style={{ left: `${x0/size.sourceWidth*100}%`, top: `${y0/size.sourceHeight*100}%`, width: `${(x1-x0)/size.sourceWidth*100}%`, height: `${(y1-y0)/size.sourceHeight*100}%` }} aria-label={block.source_text}/>;
    })}
  </div>;
}
