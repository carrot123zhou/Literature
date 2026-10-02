import { useEffect, useRef, useState } from "react";
import * as pdfjs from "pdfjs-dist";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { apiHeaders, apiUrl } from "../api";
import { pdfAssetOptions } from "../pdfAssets";

export type PdfState = { document: PDFDocumentProxy | null; pageCount: number; error: string };

/**
 * 一份项目只加载一次 PDF 文档，供连续滚动预览的所有页面共享。
 * 切换项目或关闭预览时销毁加载任务，避免 worker 泄漏。
 */
export function usePdfDocument(projectId: string, enabled: boolean): PdfState {
  const [state, setState] = useState<PdfState>({ document: null, pageCount: 0, error: "" });
  const taskRef = useRef<ReturnType<typeof pdfjs.getDocument> | null>(null);
  useEffect(() => {
    if (!enabled || !projectId) {
      setState({ document: null, pageCount: 0, error: "" });
      return;
    }
    let cancelled = false;
    const task = pdfjs.getDocument({ url: apiUrl(`/projects/${projectId}/source.pdf`), httpHeaders: apiHeaders(), ...pdfAssetOptions });
    taskRef.current = task;
    task.promise.then(
      document => { if (!cancelled) setState({ document, pageCount: document.numPages, error: "" }); },
      reason => { if (!cancelled) setState({ document: null, pageCount: 0, error: String(reason) }); },
    );
    return () => {
      cancelled = true;
      taskRef.current = null;
      task.destroy().catch(() => {});
    };
  }, [projectId, enabled]);
  return state;
}
