/**
 * TextFoldPane —— 上游 TextFold 阅读器内核宿主桥（逐字 vendor，见 textfold-engine/VENDOR.md）。
 *
 * 架构：上游 reader.js 是整页 IIFE，在每栏以 iframe(srcdoc) 承载一个内核实例；srcdoc =
 * template.html 脚手架 + DR_I18N + paper-data + 内核 JS 逐字内联 + bridge 注入脚本（宿主所有）。
 *
 * 桥 v2（真宿主请求，无外部假地址）：
 * - 内核 /api/notes/*（GET/put/delete）与 /api/ask 由桥转成 RPC（reqId）发回宿主；
 *   宿主映射到阅读层 overlay（CAS 持久化/刷新恢复）与统一问答面板，再回信内核。
 * - 内核初始化成功（window.__reader 存在）→ 握手 {type:"ready"}；失败 → {type:"error"}；
 *   宿主不再用定时器假 ready，异常可见可重试。
 * - 所有 message 双向校验 event.source（parent↔contentWindow）与实例 id。
 * - 右键 .u = 选中整段 + 触发内核 mouseup → 内核原生 selbar（划词/右键同菜单）。
 * - KaTeX：D.katex 指向本地 /textfold-vendor/katex/（离线完整资源）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import readerCss from "./textfold-engine/upstream-reader.css?raw";
// CSP 外链化（§4.8b）：内联执行脚本全部改为同源静态资源（?url 单源引用，无拷贝无漂移）。
// 小文件若被 Vite 内联成 data: 会被 script-src 'self' 阻断——assetsInlineLimit 已在 vite.config 按路径关闭。
import i18nUrl from "./textfold-engine/upstream-i18n.js?url";
import engineUrl from "./textfold-engine/engine-patched.js?url";
import bridgeUrl from "./textfold-engine/bridge.js?url";
import afterUrl from "./textfold-engine/after.js?url";
import bootstrapUrl from "./textfold-engine/bootstrap.js?url";

export type PaperData = Record<string, unknown> & {
  meta: { id: string; title?: string };
  units: Record<string, unknown>;
  chunks: unknown[];
  app?: boolean;
};

export type EngineEvent =
  | { type: "ready" }
  | { type: "error"; message: string }
  | { type: "level"; level: number }
  | { type: "hover"; uid: string | null }
  | { type: "activate"; uid: string }
  | { type: "notes-get"; reqId: string }
  | { type: "notes-put"; reqId: string; note: Record<string, unknown> }
  | { type: "notes-delete"; reqId: string; noteId: string }
  | { type: "ask"; reqId: string; question: string; note: Record<string, unknown> };

/** 桥实例标识：message 校验用（防串扰/防伪造）。 */
let bridgeSeq = 0;

function absSrc(scriptOrigin: string, url: string): string {
  return scriptOrigin + (url.startsWith("/") ? url : `/${url}`);
}
function buildSrcdoc(data: PaperData, level0to4: number, katexBase: string, inst: string, scriptOrigin: string): string {
  const payload = JSON.stringify({ ...data, app: true, katex: katexBase })
    .replace(/<\/script/gi, "<\\/script");
  // CSP：所有可执行脚本均为同源外链（script-src 'self'）；JSON 块是 application/json 非执行载荷。
  // srcdoc 内相对 URL 会解析到 about:srcdoc——必须用宿主 origin+BASE_URL 绝对地址（Tauri 协议同源自允）。
  const config = JSON.stringify({ inst, initialLevel: level0to4 })
    .replace(/<\/script/gi, "<\\/script");
  return `<!doctype html>
<html lang="zh-CN" class="app">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>${readerCss}</style>
<style>
  /* 宿主外壳适配（非上游，逐条可追踪）：栏内不渲染上游自带 topbar/footer（五档 bar 由宿主渲染） */
  .topbar, footer.zoom, .madewith { display: none !important; }   /* #ghosts 是上游词级动画层，必须保留 */
  html, body { background: transparent; }
  #doc { padding-top: 8px; }
  /* 桥点亮态：只作用段前小点（借上游 dr-beat 关键帧），不给整段背景 */
  .u.tf-lit > .dot { --beat: var(--accent); opacity: 1 !important; animation: dr-beat 1.6s ease-out infinite; }
  /* 宿主主题覆盖（用户 4c：颜色跟随阅读器主题）——上游 selbar 用 --ink 深底反差设计，
     宿主改为 --card 底 + --ink 字 + --line 边框 + --pop 阴影，与阅读器面板同色。
     shade 色点（hl/hlk 等）有专有颜色，不受此覆盖影响。 */
  .selbar { background: var(--card); border: 1px solid var(--line); box-shadow: var(--pop); }
  .selbar button { color: var(--ink); }
  .selbar button:hover { background: var(--line-2); }
</style>
<script type="application/json" id="tf-config">${config}<\/script>
<script src="${absSrc(scriptOrigin, bridgeUrl)}"><\/script>
<script src="${absSrc(scriptOrigin, i18nUrl)}"><\/script>
</head>
<body>
<header class="topbar"><div class="brand"><span>TextFold</span><a id="arxiv-link"></a></div><div class="legend" id="legend"></div><div class="tagline"></div><div class="langpick"><button type="button" id="langbtn" class="langbtn"></button><div id="langmenu" class="langmenu" role="menu" hidden></div></div></header>
<main id="doc" class="doc" data-level="4"></main>
<div id="ghosts" aria-hidden="true"></div>
<footer class="zoom"><button type="button" id="back" class="backpill" hidden></button><nav class="zoombar" id="zoombar"></nav><div class="hint" id="hint"></div></footer>
<script type="application/json" id="paper-data">${payload}<\/script>
<script src="${absSrc(scriptOrigin, engineUrl)}"><\/script>
<script src="${absSrc(scriptOrigin, afterUrl)}"><\/script>
<script src="${absSrc(scriptOrigin, bootstrapUrl)}"><\/script>
</body></html>`;
}

export type TextFoldPaneHandle = {
  setLevel: (level0to4: number) => void;
  lightUnit: (uid: string | null) => void;
  gotoUnit: (uid: string) => void;
  /** 引擎内语言切换（译文栏用实际 locale；内核原生支持 langs/tr）。 */
  setLang: (locale: string) => void;
};

export function useTextFoldEngine(args: {
  data: PaperData | null;
  initialLevel: number;
  onEvent?: (ev: EngineEvent) => void;
  litUid: string | null;
  theme: string;
  /** 译文栏的实际 locale（不 hardcode）；空串=源文栏（用数据默认语言）。 */
  locale?: string;
  /** RPC 处理（宿主真实执行 overlay/问答），返回值回信内核。 */
  rpc: (path: string, method: string, body: unknown) => Promise<unknown>;
  onActivate?: (uid: string) => void;
}): { containerRef: React.RefObject<HTMLDivElement | null>; handle: React.MutableRefObject<TextFoldPaneHandle | null>; ready: boolean; error: string; retry: () => void } {
  const { data, initialLevel, onEvent, litUid, theme, locale, rpc, onActivate } = args;
  const containerRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  const handle = useRef<TextFoldPaneHandle | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [nonce, setNonce] = useState(0);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const rpcRef = useRef(rpc);
  rpcRef.current = rpc;
  const onActivateRef = useRef(onActivate);
  onActivateRef.current = onActivate;
  const inst = useMemo(() => `tf${++bridgeSeq}`, []);   // eslint-disable-line react-hooks/exhaustive-deps
  const katexBase = useMemo(() => `${window.location.origin}${import.meta.env.BASE_URL || "/"}textfold-vendor/katex/`, []);
  // CSP 外链脚本基址：宿主 origin（srcdoc 内相对 URL 会解析到 about:srcdoc）。
  // ?url 产物形如 /assets/<name>-<hash>.js（BASE_URL 前缀含内）→ 拼 origin 成绝对地址（Tauri 协议同源自允）。
  const scriptOrigin = useMemo(() => window.location.origin, []);
  // initialLevel 只用于首次挂载：内核 setLevel 后宿主 prefs.level 变化不能重建 srcdoc
  //（否则 iframe 整个重挂、执行环境销毁、词级动画与批注状态全丢）。
  const initialLevelRef = useRef(initialLevel);
  initialLevelRef.current = initialLevel;
  const srcdoc = useMemo(
    () => (data ? buildSrcdoc(data, initialLevelRef.current, katexBase, inst, scriptOrigin) : ""),
    [data, katexBase, inst, scriptOrigin],   // eslint-disable-line react-hooks/exhaustive-deps
  );

  useEffect(() => {
    if (!srcdoc || !containerRef.current) { setReady(false); return; }
    setReady(false); setError("");
    const frame = document.createElement("iframe");
    frame.setAttribute("title", "TextFold reader");
    frame.style.cssText = "width:100%;height:100%;border:0;display:block;background:transparent";
    frame.srcdoc = srcdoc;
    containerRef.current.replaceChildren(frame);
    frameRef.current = frame;
    const onMessage = (event: MessageEvent) => {
      // 严格校验：只信本实例 iframe 的 contentWindow，且实例 id 匹配
      if (!frameRef.current || event.source !== frameRef.current.contentWindow) return;
      const d = event.data as { __tf?: number; inst?: string; type?: string; message?: string; path?: string; method?: string; body?: unknown; reqId?: string; uid?: string } | null;
      if (!d || !d.__tf || d.inst !== inst) return;
      if (d.type === "ready") { setReady(true); onEventRef.current?.({ type: "ready" }); return; }
      if (d.type === "error") { setError(String(d.message || "内核初始化失败")); onEventRef.current?.(d as EngineEvent); return; }
      if (d.type === "rpc") {
        void rpcRef.current(String(d.path || ""), String(d.method || "GET"), d.body).then(
          (result) => frame.contentWindow?.postMessage({ __tfresp: 1, reqId: d.reqId, data: result }, "*"),
          (err) => frame.contentWindow?.postMessage({ __tfresp: 1, reqId: d.reqId, error: err instanceof Error ? err.message : String(err) }, "*"),
        );
        return;
      }
      if (d.type === "activate" && typeof d.uid === "string") { onActivateRef.current?.(d.uid); return; }
      onEventRef.current?.(d as unknown as EngineEvent);
    };
    window.addEventListener("message", onMessage);
    return () => {
      window.removeEventListener("message", onMessage);
      frame.remove();
      frameRef.current = null;
      handle.current = null;
      setReady(false);
    };
  }, [srcdoc, inst]);

  // 引擎语言：译文栏按实际 locale 切（内核原生 langs/tr；数据没有该语言时内核如实忽略）
  useEffect(() => {
    if (!ready || !locale) return;
    try { (frameRef.current?.contentWindow as unknown as { __reader?: { setLang: (l: string) => void } })?.__reader?.setLang(locale); } catch { /* 引擎未就绪 */ }
  }, [ready, locale]);

  useEffect(() => {
    if (!ready) { handle.current = null; return; }
    const frame = frameRef.current;
    const win = frame?.contentWindow as unknown as { __reader?: Record<string, unknown> } | null;
    const r = win?.__reader;
    if (!r) { handle.current = null; return; }
    handle.current = {
      setLevel: (l) => { try { (r.setLevel as (n: number) => void)(l); } catch { /* 引擎未就绪 */ } },
      lightUnit: (uid) => { try { frame?.contentWindow?.postMessage({ __lit: "light", uid, on: Boolean(uid) }, "*"); } catch { /* ignore */ } },
      gotoUnit: (uid) => { try { frame?.contentWindow?.postMessage({ __lit: "goto", uid }, "*"); } catch { /* ignore */ } },
      setLang: (l) => { try { (r.setLang as (c: string) => void)(l); } catch { /* ignore */ } },
    };
  }, [ready]);

  useEffect(() => {
    if (!ready) return;
    handle.current?.lightUnit(litUid);
  }, [litUid, ready]);

  // 主题映射（§4.9 上游 CSS 契约）：宿主三档 → 上游 data-style/data-theme。
  // light→data-theme=light；paper→data-style=paper + data-theme=light（暖纸色令牌）；
  // dark→data-theme=dark。不重建 iframe（dataset 直改，CSS 变量即时生效）。
  useEffect(() => {
    const doc = frameRef.current?.contentDocument;
    if (!doc?.documentElement) return;
    const root = doc.documentElement;
    if (theme === "paper") {
      root.dataset.style = "paper";
      root.dataset.theme = "light";
    } else if (theme === "dark") {
      delete root.dataset.style;
      root.dataset.theme = "dark";
    } else {
      delete root.dataset.style;
      root.dataset.theme = "light";
    }
  }, [theme, ready]);

  const retry = useCallback(() => setNonce(value => value + 1), []);
  useEffect(() => { if (nonce > 0) setReady(false); }, [nonce]);

  return { containerRef, handle, ready, error, retry };
}
