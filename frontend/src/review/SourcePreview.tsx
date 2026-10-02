import { useEffect, useState } from "react";
import { api, apiHeaders, apiUrl } from "../api";

export type RawFile = { path: string; size: number; sha256: string };

const IMAGE_TYPES = new Set(["png", "jpg", "jpeg", "jp2", "webp", "gif", "bmp"]);
const TEXT_TYPES = new Set(["md", "json", "txt", "html", "tex", "csv", "tsv"]);

function extension(name: string): string {
  const index = name.lastIndexOf(".");
  return index >= 0 ? name.slice(index + 1).toLowerCase() : "";
}

/** 带会话令牌取回受保护文件并转成可嵌入的 blob URL；<img>/<iframe> 无法自带头部。 */
function useBlobUrl(projectId: string, path: string | null, enabled: boolean) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    if (!enabled || !path) { setUrl(""); return; }
    let cancelled = false;
    let created = "";
    fetch(apiUrl(path), { headers: apiHeaders() })
      .then(response => (response.ok ? response.blob() : Promise.reject(new Error(String(response.status)))))
      .then(blob => {
        if (cancelled) return;
        created = URL.createObjectURL(blob);
        setUrl(created);
      })
      .catch(() => { if (!cancelled) setUrl(""); });
    return () => { cancelled = true; if (created) URL.revokeObjectURL(created); };
  }, [projectId, path, enabled]);
  return url;
}

type Props = {
  projectId: string;
  sourceType: string;
  sourceName: string;
  bboxAvailable: boolean;
  onJumpPage: (page: number) => void;
};

/**
 * 非 PDF 的原件对照策略。
 *
 * 浏览器能安全渲染的（图片、HTML）直接内嵌；Office 类没有本地渲染器，
 * 因此把“对照”落在右侧结构块与 MinerU 原始输出上，并明确说明降级原因。
 */
export default function SourcePreview({ projectId, sourceType, sourceName, bboxAvailable, onJumpPage }: Props) {
  const kind = extension(sourceName) || sourceType;
  const inline = IMAGE_TYPES.has(kind) || kind === "html";
  const blobUrl = useBlobUrl(projectId, `/projects/${projectId}/source`, inline);
  const [rawFiles, setRawFiles] = useState<RawFile[]>([]);
  const [preview, setPreview] = useState<{ path: string; text: string } | null>(null);
  const [previewError, setPreviewError] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => {
      api<{ files: RawFile[] }>(`/projects/${projectId}/raw`).then(value => setRawFiles(value.files)).catch(() => setRawFiles([]));
    }, 400);
    return () => window.clearTimeout(timer);
  }, [projectId]);

  const openRaw = async (path: string) => {
    setPreviewError("");
    try {
      const response = await fetch(apiUrl(`/projects/${projectId}/raw/${path}`), { headers: apiHeaders() });
      if (!response.ok) throw new Error(String(response.status));
      const text = await response.text();
      setPreview({ path, text: text.length > 200_000 ? `${text.slice(0, 200_000)}\n…（已截断）` : text });
    } catch (reason) {
      setPreviewError(String(reason));
    }
  };

  return (
    <section className="pdf-panel mineru-preview-pane">
      <div className="viewer-toolbar">
        <strong>源文件预览</strong>
        <div>
          <a className="download-link" href={apiUrl(`/projects/${projectId}/source`)} onClick={event => { event.preventDefault(); void fetch(apiUrl(`/projects/${projectId}/source`), { headers: apiHeaders() }).then(r => r.blob()).then(blob => { const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = sourceName; link.click(); }); }}>下载原件</a>
          <span>{kind.toUpperCase()}</span>
        </div>
      </div>
      <div className="source-preview-body">
        {!bboxAvailable && <div className="preview-notice" role="status">
          <strong>该格式没有 bbox</strong>
          <p>MinerU 只对 PDF 与图片输出坐标，Office/HTML 等格式只能做文本级对照：右侧结构块与 Markdown 是唯一可定位的视图，原页联动已自动关闭。</p>
        </div>}
        {inline && blobUrl && kind !== "html" && <img className="source-image" src={blobUrl} alt={sourceName}/>}
        {kind === "html" && blobUrl && <iframe className="source-frame" title={sourceName} sandbox="" src={blobUrl}/>}
        {inline && !blobUrl && <div className="empty">正在加载原件…</div>}
        {!inline && <div className="source-preview-placeholder">
          <strong>{sourceName}</strong>
          <span>{kind.toUpperCase()}</span>
          <p>浏览器内没有可靠的 {kind.toUpperCase()} 渲染器，因此本模块不做像素级原件对照。请使用右侧「结构块 / Markdown」核对解析结果，或在「MinerU 原始输出」中查看逐格式结果。</p>
        </div>}

        <details className="raw-files">
          <summary>MinerU 原始输出 · {rawFiles.length} 个文件</summary>
          <p className="audit-note">原始输出在封存后只读，可通过哈希复核；这里是只读预览。</p>
          {rawFiles.filter(file => !file.path.includes("/images/")).slice(0, 40).map(file => (
            <div className="raw-file-row" key={file.path}>
              <span title={file.path}>{file.path.split("/").slice(-1)[0]}</span>
              <small>{Math.max(1, Math.round(file.size / 1024))} KB</small>
              {TEXT_TYPES.has(extension(file.path)) && <button onClick={() => openRaw(file.path)}>预览</button>}
            </div>
          ))}
          {rawFiles.length === 0 && <p className="empty-note">没有可读取的封存文件。</p>}
        </details>

        {previewError && <div className="preview-notice"><strong>无法读取</strong><p>{previewError}</p></div>}
        {preview && <div className="raw-preview">
          <div className="panel-title"><strong>{preview.path.split("/").slice(-1)[0]}</strong><button onClick={() => setPreview(null)}>关闭</button></div>
          <pre>{preview.text}</pre>
        </div>}
        <button className="link-button" onClick={() => onJumpPage(1)}>回到第 1 页定位</button>
      </div>
    </section>
  );
}
