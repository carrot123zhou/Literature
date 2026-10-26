import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Block, DocumentGraph, ModelService, TranslationLayer } from "../types";
import { apiUrl } from "../api";
import PdfPageCanvas from "../review/PdfPageCanvas";
import { usePdfDocument } from "../review/usePdfDocument";
import { bboxRect, estimatedPageHeight, hasBBox } from "../review/geometry";
import "./reader.css";
import "./reader-interactions.css";
import ModelPicker from "./ModelPicker";
import { readerApi } from "./readerApi";
import { useReaderChat, type ReaderChatWireQuote } from "../readerChatClient";
import { useTextFoldEngine, type EngineEvent, type PaperData, type TextFoldPaneHandle } from "./TextFoldPane";
import { foldJobApi, type FoldJobState } from "./foldApi";
import { buildTokenMap, toksTextLike, tokenSpaceDigest } from "./tokenMap";
import type {
  ReaderAnnotation, ReaderFoldSection, ReaderFoldUnit, ReaderLayers,
  ReaderModelBinding, ReaderOverlay, ReaderPane, ReaderState,
} from "./types";

type Props = {
  projectId: string;
  document: DocumentGraph;
  translation: TranslationLayer | null;
  /** 目标语言（翻译层 locale）；无译文时传 ""。 */
  targetLocale: string;
  /** 全部已配置服务商（来自共享 useModelServices，不另建状态）。 */
  providers: ModelService[];
  /** 阅读角色绑定（reader_fold / reader_chat，来自共享 ModelServicesApi.roles）。 */
  defaultFold?: ReaderModelBinding;
  defaultChat?: ReaderModelBinding;
  /** 翻译角色绑定：reader_chat 未绑定时后端回退它；UI 如实显示继承来源（developer 传入）。 */
  translationBinding?: ReaderModelBinding;
  onOpenSettings?: () => void;
};

const LEVEL_NAMES: Record<number, string> = { 1: "全文", 2: "简述", 3: "要点", 4: "主线", 5: "主题" };
const COLORS: ReaderColor2[] = ["y", "g", "b", "p", "o"];
type ReaderColor2 = "y" | "g" | "b" | "p" | "o";

/** 分层来源的诚实标注：mixed/local/suppressed 不能整体标成 AI；partial 不能说“全部完成”。 */
function provenanceLabel(layers: ReaderLayers | null, contentHint = "", partialHint = false): string {
  if (!layers || !layers.generated) return "尚未生成分层：当前显示全文";
  const content = (contentHint || layers.provenance?.content || "local") as string;
  const partial = (partialHint || Boolean(layers.provenance?.partial)) ? "（部分章节失败，仍是旧内容/本地摘句）" : "";
  if (content === "model") return `要点/主线/主题由模型生成（${layers.provenance.model || "已绑定模型"}）` + partial;
  if (content === "mixed") return "混合来源：部分段落由模型生成，部分为本地摘句" + partial;
  return "本地摘句（本次未调模型；要点/主线/主题为程序摘取，非 AI 概括）" + partial;
}

const DEFAULT_PREFS = (): ReaderOverlay["prefs"] => ({
  panes: { origin: true, source: true, trans: true },
  weights: [340, 460, 460],
  level: 1,
  appearance: "light",
  font: "serif",
  fontSize: 17,
  lineHeight: "1.9",
  follow: true,
  sync: true,
});

function uid(): string { return "h" + Date.now().toString(36) + Math.floor(Math.random() * 1e4); }
function textOffset(root: HTMLElement, node: Node, off: number): number {
  const r = document.createRange(); r.selectNodeContents(root); r.setEnd(node, off);
  return r.toString().length;
}
function clamp(n: number, lo: number, hi: number): number { return Math.max(lo, Math.min(hi, n)); }

/** 把段落纯文本按批注偏移切段渲染（重叠区间跳过，越界时按 quote 重定位）。
 *  左键不响应（阅读态）；已有标记的唯一编辑入口是右键（见 contextmenu → data-ann-id）。 */
function annotatedSegments(text: string, anns: ReaderAnnotation[]) {
  const parts: React.ReactNode[] = [];
  let cursor = 0;
  const list = anns.filter(a => !a.detached).sort((a, b) => a.start - b.start);
  list.forEach((a, index) => {
    let { start, end } = a;
    if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return;
    // 后端 quote.exact 只保留前 500 字：校验只比对可得前缀；位置失效时用前缀重定位，
    // 并保留原选区全长（end-start）——不能把用户长选区截短成 500 字。
    const exact = a.quote.exact || "";
    const span = end - start;
    const probe = exact.slice(0, Math.min(exact.length, span));
    const at = start >= 0 && start + probe.length <= text.length && probe.length > 0
      && text.slice(start, start + probe.length) === probe;
    if (!at) {
      const idx = probe ? text.indexOf(probe) : -1;
      if (idx < 0) return;
      start = idx;
      end = Math.min(idx + span, text.length);
    }
    if (end > text.length) end = text.length;
    if (start < cursor) return;
    if (start > cursor) parts.push(<span key={"t" + index}>{text.slice(cursor, start)}</span>);
    parts.push(
      <mark key={a.id} className={"hl c-" + a.color} data-ann-id={a.id} title={a.note ? `笔记：${a.note}` : "高亮（右键编辑）"}>
        {text.slice(start, end)}
      </mark>,
    );
    cursor = end;
  });
  if (cursor < text.length) parts.push(<span key="tail">{text.slice(cursor)}</span>);
  return parts;
}

/* ================================ 主工作区 ================================ */

export default function ReaderWorkspace({ projectId, document: doc, translation, targetLocale, providers, defaultFold, defaultChat, translationBinding, onOpenSettings }: Props) {
  /* ---------- 远端数据 ---------- */
  const [state, setState] = useState<ReaderState | null>(null);
  const [overlay, setOverlay] = useState<ReaderOverlay | null>(null);
  const [layersSrc, setLayersSrc] = useState<ReaderLayers | null>(null);
  const [layersDst, setLayersDst] = useState<ReaderLayers | null>(null);
  const [layersError, setLayersError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const noticeTimer = useRef<number>(0);
  const toast = useCallback((message: string) => {
    setNotice(message);
    window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(""), 3200);
  }, []);

  const hasTrans = Boolean(translation && targetLocale);
  const sourceLocale = "source";

  const loadAll = useCallback(async () => {
    try {
      const value = await readerApi.state(projectId, targetLocale || "source");
      setState(value);
      // 合并而非覆盖：轮询/刷新期间用户改过的本地偏好（层级/布局/外观）不能被服务端旧值打回；
      // 批注与 revision 以服务端为准（本地未保存的批注由 scheduleSave 的防抖队列负责）。
      setOverlay(prev => value.overlay
        ? { ...value.overlay, prefs: prev?.prefs ?? value.overlay.prefs }
        : value.overlay);
      const [src, dst] = await Promise.all([
        readerApi.layers(projectId, "source"),
        hasTrans ? readerApi.layers(projectId, targetLocale).catch(() => null) : Promise.resolve(null),
      ]);
      setLayersSrc(src); setLayersDst(dst);
      setLayersError("");
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error));
    }
  }, [projectId, targetLocale, hasTrans]);

  useEffect(() => { setLoadError(""); void loadAll(); }, [loadAll]);

  /**
   * AIFold 状态机（严格对齐后端 JSON：status=running/pausing/paused/ready/failed、
   * progress={ratio}、errors[]、无 paused 布尔）。统一会话代次：所有异步在发起前捕获
   * {project, locale, gen}，返回核验；cleanup/切 project/切 locale 均使代次失效。
   * 单飞轮询；恢复 running/pausing 接续轮询；paused 不自动恢复生成，resume 后轮询至 ready；
   * ready 且 ready=true 才可「启动」；失败/网络错误保留可恢复状态。
   */
  const foldLocale = hasTrans ? (targetLocale || "source") : "source";
  const [foldStage, setFoldStage] = useState<"off" | "processing" | "ready" | "on">("off");
  const [foldJob, setFoldJob] = useState<FoldJobState | null>(null);
  const [foldError, setFoldError] = useState("");
  const [foldData, setFoldData] = useState<PaperData | null>(null);
  /** 会话代次：projectId/locale 变化或 cleanup 时 ++；旧异步返回一律丢弃。 */
  const foldGen = useRef(0);
  const foldSession = useRef("");
  const foldPolling = useRef(0);   // 持有当前轮询的会话代次（0=空闲；旧晚归响应不得翻转新会话锁）
  const foldTimerRef = useRef(0);
  const foldApplyState = useCallback((value: FoldJobState) => {
    setFoldJob(value);
    if (value.status === "ready" && value.ready) setFoldStage("ready");
    else if (value.status === "failed") { setFoldStage("off"); setFoldError(value.errors.join("；") || "整篇折叠失败"); }
  }, []);
  /** 单飞轮询：running/pausing 期间持续；paused/ready/failed 停轮询（paused 不自动恢复）。 */
  const foldPoll = useCallback((projectIdAt: string, localeAt: string) => {
    const gen = foldGen.current;
    if (foldPolling.current === gen) return;   // 本代次已在轮询
    if (foldPolling.current !== 0) return;     // 旧代次轮询未结束：等它自然退出，不抢锁
    foldPolling.current = gen;
    const tick = () => {
      if (gen !== foldGen.current || projectIdAt !== projectId || localeAt !== foldLocale) {
        if (foldPolling.current === gen) foldPolling.current = 0; return;
      }
      void foldJobApi.state(projectIdAt).then(value => {
        if (gen !== foldGen.current || projectIdAt !== projectId) { if (foldPolling.current === gen) foldPolling.current = 0; return; }
        foldApplyState(value);
        if (foldPolling.current === gen) foldPolling.current = 0;
        // 800ms 间隔轮询（不能紧递归：立即重入会把 running 瞬间轮成 ready，进度环与暂停都失去意义）
        if (value.status === "running" || value.status === "pausing") {
          window.clearTimeout(foldTimerRef.current);
          foldTimerRef.current = window.setTimeout(() => foldPoll(projectIdAt, localeAt), 800);
        }
      }).catch(error => {
        if (foldPolling.current === gen) foldPolling.current = 0;
        if (gen !== foldGen.current || projectIdAt !== projectId) return;
        // 网络/接口错误：保留可恢复状态（停在 processing，可暂停/重试），错误如实显示
        setFoldError(error instanceof Error ? error.message : String(error));
      });
    };
    tick();
  }, [foldApplyState, foldLocale, projectId]);   // eslint-disable-line react-hooks/exhaustive-deps
  /** 打开/切项目/切 locale：代次失效 + 恢复现有 run 状态（done/ready→ready，running/pausing 接续轮询）。 */
  useEffect(() => {
    const gen = ++foldGen.current;
    foldPolling.current = 0; window.clearTimeout(foldTimerRef.current);   // 新会话：清旧轮询锁与定时器
    setFoldStage("off"); setFoldData(null); setFoldJob(null); setFoldError("");
    void foldJobApi.state(projectId).then(value => {
      if (gen !== foldGen.current) return;
      setFoldJob(value);
      if (value.status === "ready" && value.ready) setFoldStage("ready");
      else if (value.status === "running" || value.status === "pausing") { setFoldStage("processing"); foldPoll(projectId, foldLocale); }
      else if (value.status === "paused") setFoldStage("processing");   // 暂停态：显示「继续」，不自动恢复生成
      else setFoldStage("off");
    }).catch((error) => { console.error("[fold-recovery] state GET 失败", String(error).slice(0, 160)); });
    return () => { foldGen.current++; window.clearTimeout(foldTimerRef.current); };
  }, [projectId, foldLocale, foldPoll]);   // eslint-disable-line react-hooks/exhaustive-deps
  const startAIFold = () => {
    const gen = ++foldGen.current;
    const projectIdAt = projectId, localeAt = foldLocale;
    setFoldError("");
    setFoldStage("processing");
    void (async () => {
      try {
        await foldJobApi.start(projectIdAt, localeAt);
        if (gen !== foldGen.current || projectIdAt !== projectId) return;
        foldPoll(projectIdAt, localeAt);
      } catch (error) {
        if (gen !== foldGen.current) return;
        setFoldStage("off");
        setFoldError(error instanceof Error ? error.message : String(error));
      }
    })();
  };
  const foldPauseResume = () => {
    const gen = foldGen.current;
    const projectIdAt = projectId, localeAt = foldLocale;
    const runId = foldJob?.runId;
    if (!runId) { setFoldError("没有可暂停/继续的折叠作业"); return; }
    const wasPaused = foldJob?.status === "paused";
    const action = wasPaused ? foldJobApi.resume : foldJobApi.pause;
    void action(projectIdAt, runId).then(() => {
      if (gen !== foldGen.current || projectIdAt !== projectId) return;   // 旧会话/旧项目的响应不写当前状态
      if (wasPaused) { setFoldStage("processing"); foldPoll(projectIdAt, localeAt); }
      else return foldJobApi.state(projectIdAt).then(value => { if (gen === foldGen.current && projectIdAt === projectId) foldApplyState(value); });
    }).catch(error => {
      if (gen !== foldGen.current || projectIdAt !== projectId) return;
      setFoldError(error instanceof Error ? error.message : String(error));
    });
  };
  const launchFold = () => {
    const gen = foldGen.current;
    const projectIdAt = projectId, localeAt = foldLocale;
    setFoldError("");
    setFoldStage("on");   // 乐观进入 fold 视图；产物未到前按钮禁用（防双击误退），载入失败回 ready
    void (async () => {
      try {
        const data = await foldJobApi.native<PaperData>(projectIdAt, localeAt);
        if (gen !== foldGen.current || projectIdAt !== projectId || localeAt !== foldLocale) return;
        setFoldData(data);
      } catch (error) {
        if (gen !== foldGen.current || projectIdAt !== projectId) return;
        setFoldError(error instanceof Error ? error.message : String(error));
        setFoldStage("ready");
      }
    })();
  };
  /** 退出 fold 回普通阅读：保留 ready 可再启动，不重新生成。 */
  const exitFold = () => { setFoldStage(foldJob?.status === "ready" && foldJob.ready ? "ready" : "off"); setFoldData(null); };
  const foldStageRef = useRef(foldStage);
  foldStageRef.current = foldStage;


  /* ---------- overlay 保存（防抖 + 乐观并发） ---------- */
  const saveTimer = useRef<number>(0);
  const pendingOverlay = useRef<ReaderOverlay | null>(null);
  const scheduleSave = useCallback((next: ReaderOverlay) => {
    pendingOverlay.current = next;
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => {
      const payload = pendingOverlay.current;
      if (!payload) return;
      pendingOverlay.current = null;
      void persistOverlayNow(payload).catch(error => {
        const message = error instanceof Error ? error.message : String(error);
        if (message.includes("已被其他窗口更新")) toast("阅读层已在其他窗口更新，已重新加载");
        else toast(`${message}（草稿保留，可重试）`);
      });
    }, 600);
  }, [projectId, targetLocale, toast]);

  const patchPrefs = useCallback((patch: Partial<ReaderOverlay["prefs"]>) => {
    setOverlay(current => {
      if (!current) return current;
      const next: ReaderOverlay = { ...current, prefs: { ...current.prefs, ...patch } };
      scheduleSave(next);
      return next;
    });
  }, [scheduleSave]);

  const prefs = overlay?.prefs ?? DEFAULT_PREFS();

  /* ---------- 分层索引 ---------- */
  /**
   * 章节索引：`/reader/state.section_index`（core build_sections 投影，零模型）。
   * 有它才能在「还没有任何分层/没调过模型」时选当前章、开目录、只折一章。
   */
  const sectionIndex = state?.section_index ?? [];
  const sectionIndexById = useMemo(() => {
    const map = new Map<string, (typeof sectionIndex)[number]>();
    sectionIndex.forEach(item => map.set(item.id, item));
    return map;
  }, [sectionIndex]);
  const blockToSectionId = useMemo(() => {
    const map = new Map<string, string>();
    sectionIndex.forEach(item => {
      if (item.block_id) map.set(item.block_id, item.id);
      (item.unit_ids || []).forEach(blockId => map.set(blockId, item.id));
    });
    return map;
  }, [sectionIndex]);
  const dstUnitIndex = useMemo(() => {
    const map = new Map<string, ReaderFoldUnit>();
    (layersDst?.sections ?? []).forEach(section => section.units.forEach(unit => map.set(unit.block_id || unit.id, unit)));
    return map;
  }, [layersDst]);
  const unitIndex = useMemo(() => {
    const map = new Map<string, { unit: ReaderFoldUnit; section: ReaderFoldSection }>();
    const src = layersSrc?.sections ?? [];
    // 后端投影里 unit 的稳定 id 在 `id`（=block_id），`block_id` 字段可能缺省
    src.forEach(section => section.units.forEach(unit => map.set(unit.block_id || unit.id, { unit, section })));
    return map;
  }, [layersSrc]);

  /** 文档顺序的 block → section 归组（含图/表/公式等透传块，不要求是 unit）。
   *  「展开本节」据此把整节按原文顺序完整渲染：标题块切节，标题之间的所有块都归属当前节；
   *  前言（block_id 为空的首节）由首个标题之前的块构成。 */
  const blockSectionByOrder = useMemo(() => {
    const anchorToSection = new Map<string, string>();
    (layersSrc?.sections ?? []).forEach(section => { if (section.block_id) anchorToSection.set(section.block_id, section.id); });
    sectionIndex.forEach(item => { if (item.block_id && !anchorToSection.has(item.block_id)) anchorToSection.set(item.block_id, item.id); });
    const first = layersSrc?.sections?.[0] ?? sectionIndex[0];
    const map = new Map<string, string>();
    let current = first && !first.block_id ? first.id : "";
    doc.blocks.forEach(block => {
      if (block.type === "title") {
        // 锚定标题切节；未锚定标题（后端丢弃的纯标题节，如只有子标题的过渡页）切为无归属，
        // 避免后续块错挂到上一节（展开上一节时渲染出子节内容）。
        current = anchorToSection.get(block.block_id) ?? "";
      }
      if (current) map.set(block.block_id, current);
    });
    return map;
  }, [doc.blocks, layersSrc, sectionIndex]);

  /* ---------- 选中 / 定位 ---------- */
  const [currentBlock, setCurrentBlock] = useState("");
  const [selected, setSelected] = useState("");
  const [tocOpen, setTocOpen] = useState(false);
  const [aaOpen, setAaOpen] = useState(false);
  const [visiblePage, setVisiblePage] = useState(0);
  const guardRef = useRef(0);
  const bodyRefs: { source: React.RefObject<HTMLDivElement | null>; trans: React.RefObject<HTMLDivElement | null>; origin: React.RefObject<HTMLDivElement | null> } = {
  source: useRef<HTMLDivElement>(null), trans: useRef<HTMLDivElement>(null), origin: useRef<HTMLDivElement>(null),
};
  const paneMounted = (pane: "source" | "trans" | "origin") => {
    const el = pane === "origin" ? bodyRefs.origin.current : bodyRefs[pane].current;
    return Boolean(el && el.clientWidth > 0);
  };
  const blkTop = (container: HTMLElement | null, node: Element) =>
    node.getBoundingClientRect().top - (container?.getBoundingClientRect().top ?? 0) + (container?.scrollTop ?? 0);

  const blockEl = useCallback((pane: ReaderPane, id: string) =>
    (pane === "source" ? bodyRefs.source.current : bodyRefs.trans.current)?.querySelector(`[data-block-id="${CSS.escape(id)}"]`) as HTMLElement | null,
    []); // eslint-disable-line react-hooks/exhaustive-deps

  /** 是否为可绘制原件页的 PDF（后面原件栏与定位都用它）。 */
  const isPdf = (doc.pages?.length ?? 0) > 0;
  const pdf = usePdfDocument(projectId, isPdf && prefs.panes.origin);
  /** PDF 解码失败（或页图不可得）但文档带 mineru bbox：降级为「版面框」视图，仍可定位与联动。 */
  const showBboxOnly = isPdf && Boolean(pdf.error) && doc.bbox_space !== "none"
    && (doc.pages?.length ?? 0) > 0;

  /**
   * 跨栏定位的抑制窗口。
   *
   * 双向跟随 + 点击居中都会触发对方的 scroll 事件；用时间窗而不是布尔锁，
   * 因为平滑滚动是异步的（几十到几百毫秒），布尔锁会在滚动结束前就释放并产生回弹。
   */
  const suppressUntil = useRef(0);
  const suppress = (ms = 300) => { suppressUntil.current = Date.now() + ms; };
  const suppressed = () => Date.now() < suppressUntil.current;

  /** 屏幕上某块的锚点：优先真实 bbox，其次跨页续接区域，都没有就退回页号（明确降级）。 */
  const anchorOf = useCallback((blockId: string): { page: number; bbox: number[] | null } | null => {
    const block = doc.blocks.find(b => b.block_id === blockId);
    if (!block) return null;
    const valid = (bbox?: number[]) => Array.isArray(bbox) && bbox.length === 4 && bbox[2] > bbox[0] && bbox[3] > bbox[1];
    if (valid(block.bbox)) return { page: block.page_index, bbox: block.bbox };
    const region = doc.flow_links?.[blockId]?.next_region;
    if (region && valid(region.bbox)) return { page: region.page_index, bbox: region.bbox };
    return { page: block.page_index, bbox: null };
  }, [doc.blocks, doc.flow_links]);

  /** 章节/主题锚点：用标题块（有真实 bbox），而不是给 AI 摘要编一个逐字范围。 */
  const sectionAnchor = useCallback((sectionId: string): string => {
    const section = (layersSrc?.sections ?? []).find(item => item.id === sectionId);
    if (section?.block_id) return section.block_id;
    const first = section?.units?.[0];
    if (first) return first.block_id || first.id;
    // 还没有分层：用章节索引里的标题块（与折叠层同一个 block_id 来源）
    return sectionIndexById.get(sectionId)?.block_id ?? "";
  }, [layersSrc, sectionIndexById]);

  useEffect(() => () => { suppressUntil.current = 0; }, []);

  /* ---------- 原件：bbox 定位（不再用估算页高/页号滚动代替） ---------- */
  const originBody = () => bodyRefs.origin.current;

  /** 等目标 bbox 真的渲染出来（虚拟页未激活时先切到该页，等 canvas/overlay 出现）。 */
  const waitForOriginBox = useCallback((blockId: string, timeoutMs = 900): Promise<HTMLElement | null> => {
    const deadline = Date.now() + timeoutMs;
    return new Promise(resolve => {
      const tick = () => {
        const body = originBody();
        const el = body?.querySelector(`[data-block-id="${CSS.escape(blockId)}"]`) as HTMLElement | null;
        if (el && el.getClientRects().length) { resolve(el); return; }
        if (!body || Date.now() > deadline) { resolve(null); return; }
        window.requestAnimationFrame(tick);
      };
      window.requestAnimationFrame(tick);
    });
  }, []);

  /** 把某块的原件 bbox 居中到原件滚动容器；返回是否命中了真实 bbox。 */
  const centerOriginOn = useCallback(async (blockId: string): Promise<boolean> => {
    const body = originBody();
    // 原件栏被关闭（窄屏切到原文/译文、或用户取消勾选）时不做任何定位，也不弹「无 bbox」提示。
    if (!body || !isPdf || !paneMounted("origin")) return false;
    const anchor = anchorOf(blockId);
    if (!anchor) return false;
    suppress(420);
    setVisiblePage(anchor.page);
    // 目标页不在活动窗口内时，先切页再等一帧，让 PdfPageCanvas 真正渲染。
    const el = await waitForOriginBox(blockId);
    const container = originBody();
    if (!container) return false;
    if (!el) {
      // 明确降级：没有 bbox（或该页渲染失败）时只滚到页首，并如实提示。
      const pageEl = container.querySelector(`.pdf-page[data-page-index="${anchor.page}"]`) as HTMLElement | null;
      if (pageEl) {
        const cRect = container.getBoundingClientRect();
        const pRect = pageEl.getBoundingClientRect();
        container.scrollTop += pRect.top - cRect.top - 8;
      }
      if (!anchor.bbox) toast("该段没有可用版面坐标：已定位到所在页（无 bbox 可居中）");
      return false;
    }
    const cRect = container.getBoundingClientRect();
    const eRect = el.getBoundingClientRect();
    const target = container.scrollTop + (eRect.top + eRect.height / 2) - (cRect.top + cRect.height / 2);
    container.scrollTop = Math.max(0, target);
    return true;
  }, [anchorOf, isPdf, toast]); // eslint-disable-line react-hooks/exhaustive-deps

  /** 文本栏内定位：优先精确块，其次同节的第一个已渲染块（L4/L5 只渲染节卡片）。 */
  const scrollTextPaneTo = useCallback((pane: ReaderPane, blockId: string) => {
    const container = pane === "source" ? bodyRefs.source.current : bodyRefs.trans.current;
    if (!container || !paneMounted(pane)) return;
    let el = blockEl(pane, blockId);
    if (!el) {
      // L4/L5 只渲染「节卡片」：它的 data-block-id 就是节标题块（sectionAnchor）。
      const sectionId = unitIndex.get(blockId)?.section.id
        ?? (layersSrc?.sections ?? []).find(item => item.block_id === blockId)?.id
        ?? "";
      const anchorId = sectionId ? sectionAnchor(sectionId) : "";
      if (anchorId) el = blockEl(pane, anchorId);
      if (!el && sectionId) {
        for (const unit of (layersSrc?.sections ?? []).find(item => item.id === sectionId)?.units ?? []) {
          el = blockEl(pane, unit.block_id || unit.id);
          if (el) break;
        }
      }
    }
    if (!el) return;
    suppress(220);
    container.scrollTop = Math.max(0, blkTop(container, el) - 90);
  }, [blockEl, unitIndex, layersSrc, sectionAnchor, paneMounted]); // eslint-disable-line react-hooks/exhaustive-deps

  /**
   * 点击/选中：一次跨栏定位（原件 bbox 居中 + 对侧文本栏定位），
   * 与「跟随滚动」开关无关；再次点击同一块也会重新居中。
   */
  const activateBlock = useCallback((blockId: string, pane: "source" | "trans" | "origin") => {
    if (!blockId) return;
    setSelected(blockId);
    setCurrentBlock(blockId);
    suppress(420);
    void centerOriginOn(blockId);
    // fold 模式：原件/目录/对侧点击 → 双栏内核定位到同 unit（id=稳定 block_id）
    if (foldStageRef.current === "on") {
      srcEngineRef.current?.gotoUnit(blockId);
      dstEngineRef.current?.gotoUnit(blockId);
    }
    if (pane !== "trans") scrollTextPaneTo("trans", blockId);
    if (pane !== "source") scrollTextPaneTo("source", blockId);
  }, [centerOriginOn, scrollTextPaneTo]);

  /* ---------- 窄屏：单栏 + tab ---------- */
  const [isNarrow, setIsNarrow] = useState(() => window.matchMedia("(max-width:899px)").matches);
  const [narrowPane, setNarrowPane] = useState<"origin" | "source" | "trans">("source");
  useEffect(() => {
    const mq = window.matchMedia("(max-width:899px)");
    const onChange = () => setIsNarrow(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  useEffect(() => {
    if (isNarrow && !prefs.panes[narrowPane]) {
      const alt = (["origin", "source", "trans"] as const).find(k => prefs.panes[k]);
      if (alt) setNarrowPane(alt);
    }
  }, [isNarrow, narrowPane, prefs.panes]);

  /* ---------- 层级（仅 fold 模式；普通阅读恒为全文，五档 bar 仅在 fold 内出现） ---------- */
  const level = foldStage === "on" ? prefs.level : 1;
  const [openBlocks, setOpenBlocks] = useState<Set<string>>(new Set());
  const [openSections, setOpenSections] = useState<Set<string>>(new Set());
  /** 跨栏小蓝点：译文栏 hover uid → 原文栏对应 unit 同亮；反之亦然（同 unit id = 同一阅读对象）。 */
  const [litForSrc, setLitForSrc] = useState("");
  const [litForDst, setLitForDst] = useState("");
  const makeEngineOnEvent = useCallback((from: "src" | "dst") => (ev: EngineEvent) => {
    if (ev.type === "hover") {
      if (from === "src") setLitForDst(ev.uid || ""); else setLitForSrc(ev.uid || "");
      return;
    }
    if (ev.type === "level") {
      // 引擎内部层变化（⌘/Ctrl+滚轮等）回同步宿主状态与滑块
      const product = clamp(5 - ev.level, 1, 5);   // 上游 0..4(Topic..Full) → 产品 1..5
      if (product !== prefs.level) patchPrefs({ level: product });
    }
  }, [patchPrefs, prefs.level]);   // eslint-disable-line react-hooks/exhaustive-deps
  /** 上游内核实例：原文栏 en（或原文语）、译文栏 zh（setLang 由数据驱动，内核原生支持）。 */
  /** 内核 RPC：notes 读写映射到阅读层 overlay（CAS 持久化/刷新恢复），ask 路由到统一问答面板。 */
  const foldDataRef = useRef<PaperData | null>(null);
  foldDataRef.current = foldData;
  type NotesHandler = (a: ReaderAnnotation) => Record<string, unknown>;
  const toEngineNoteRef = useRef<NotesHandler | null>(null);
  const putEngineNoteRef = useRef<((note: Record<string, unknown>, pane: ReaderPane) => { ok: boolean; persisted: Promise<void> }) | null>(null);
  const deleteEngineNoteRef = useRef<((noteId: string) => { ok: boolean; persisted: Promise<void> }) | null>(null);
  const engineRpc = useCallback(async (path: string, method: string, body: unknown, pane: ReaderPane): Promise<unknown> => {
    const payload = typeof body === "string" ? (JSON.parse(body || "{}") as Record<string, unknown>) : (body ?? {}) as Record<string, unknown>;
    if (path.startsWith("/api/notes/") && method === "GET") {
      // 只回本栏批注：源词 token 偏移绝不挂到译文栏（pane 归属是批注事实的一部分）
      return { notes: (foldDataRef.current ? overlayLive.current?.annotations ?? [] : []).filter(a => !a.detached && a.pane === pane).map(a => toEngineNoteRef.current ? toEngineNoteRef.current(a) : a) };
    }
    if (path.startsWith("/api/notes/") && method === "POST") {
      const note = payload.note as Record<string, unknown> | undefined;
      if (payload.op === "put" && note) {
        const result = putEngineNote(note, pane) as { ok: boolean; persisted?: Promise<void> };
        await result.persisted;   // 真实 CAS 落盘后才 200；失败如实回执内核
        return { ok: true };
      }
      if (payload.op === "delete" && typeof payload.id === "string") {
        const result = deleteEngineNote(payload.id) as { ok: boolean; persisted?: Promise<void> };
        await result.persisted;
        return { ok: true };
      }
      throw new Error(`未知 notes 操作：${String(payload.op)}`);
    }
    if (path.startsWith("/api/ask")) {
      // 统一问答：引用与问题进宿主聊天面板；内核侧收到如实错误（不吞、不伪造回答）
      const note = (payload.note ?? {}) as Record<string, unknown>;
      const anchor = (note.anchor ?? {}) as Record<string, unknown>;
      const quote = (anchor.quote ?? {}) as { exact?: string };
      setQuote(String(anchor.unit || ""), pane, String(quote.exact || ""));
      setChatDraft(String(payload.question || ""));
      setChatOpen(true);
      throw new Error("已在宿主「AI 问答」面板继续此问题");
    }
    throw new Error(`引擎请求未接通：${method} ${path}`);
  }, [overlay]);   // eslint-disable-line react-hooks/exhaustive-deps
  const tokenMapForRef = useRef<((blockId: string, pane: ReaderPane) => ReturnType<typeof buildTokenMap> | null) | null>(null);
  const makeRpc = useCallback((pane: ReaderPane) =>
    (path: string, method: string, body: unknown) => engineRpc(path, method, body, pane), [engineRpc]);
  /** 内核 token↔字符映射：源栏用 unit.toks 重建 vs 原文全文；译文栏用 tr[locale].toks vs 译文文本。
   *  重建不一致（分词有损）返回 null → 回退 block 锚 + quote，不伪造对应。 */
  const tokenMapFor = useCallback((blockId: string, pane: ReaderPane) => {
    if (!foldData) return null;
    const units = foldData.units as Record<string, { toks?: unknown[]; tr?: Record<string, { toks?: unknown[] }> }> | undefined;
    const unit = units?.[blockId];
    if (!unit) return null;
    const atoms = (foldData.atoms ?? {}) as Record<string, { kind?: string; alt?: string; text?: string }>;
    const block = doc.blocks.find(b => b.block_id === blockId);
    if (pane === "source") {
      const text = block?.source_text ?? "";
      // 首选 React 渲染的同一文本；重建失败再试 trims 变体；都失败 → null（不伪造）
      return buildTokenMap(unit.toks as never, atoms, text)
        ?? buildTokenMap(unit.toks as never, atoms, (blockEl("source", blockId)?.textContent ?? "").trim());
    }
    const trKey = unit.tr?.[targetLocale] ? targetLocale : Object.keys(unit.tr || {})[0] ?? "";
    const tr = unit.tr?.[trKey];
    const text = translation?.entries[blockId]?.text ?? block?.source_text ?? "";
    return tr ? buildTokenMap(tr.toks as never, atoms, text) : null;
  }, [foldData, doc.blocks, targetLocale, translation, blockEl]);   // eslint-disable-line react-hooks/exhaustive-deps


  /** 内核锚索引空间的 token 流（供指纹与复原校验）：full/brief/derived 三种，按 anchor.lang 选 lane。 */
  const anchorSpaceToks = useCallback((unitId: string, space: "full" | "brief" | "derived", locale: string): { toks?: unknown[] } | null => {
    const units = foldData?.units as Record<string, {
      toks?: unknown[]; lad?: Record<string, unknown>; tr?: Record<string, { toks?: unknown[]; lad?: Record<string, unknown> }>;
    } | undefined> | undefined;
    const unit = units?.[unitId];
    if (!unit) return null;
    const isTarget = Boolean(locale && locale !== "source" && unit.tr?.[locale]);
    const z = isTarget ? unit.tr?.[locale] : undefined;
    if (space === "derived") {
      const db = (z?.lad as { derived_brief?: { toks?: unknown[] } } | undefined)?.derived_brief;
      return db && Array.isArray(db.toks) ? { toks: db.toks } : null;
    }
    if (space === "brief") {
      // brief 在 z.lad（z 是整个 tr lane，不是 lad 本身）
      const lad = (z ? z.lad : unit.lad) as { brief?: unknown[] } | undefined;
      return lad && Array.isArray(lad.brief) ? { toks: lad.brief } : null;
    }
    return { toks: (isTarget ? z?.toks : unit.toks) as unknown[] | undefined };
  }, [foldData, targetLocale]);

  /** lane locale：源 = 实际源语言码（meta.lang，如 en）或 "source"；译文 = targetLocale。 */
  const laneLocale = useCallback((pane: ReaderPane) =>
    pane === "trans" ? (targetLocale || "source") : (String((foldData?.meta as { lang?: string } | undefined)?.lang) || "source"), [targetLocale, foldData]);
  /** 保存内核原生锚快照：{version, locale, space, digest, thread, anchor}；空间 token 流不存在则不保存（回退确定性映射）。
   *  brief/derived 空间剥离 anchor.sents（那是全文句下标，backend 契约禁止随短层空间保存）；其余字段逐字保留。 */
  const makeEngineAnchor = (note: Record<string, unknown>, pane: ReaderPane) => {
    const anchor = note.anchor as Record<string, unknown> | undefined;
    if (!anchor) return undefined;
    const locale = laneLocale(pane);
    const anchorOn = String(anchor.on || "unit");
    // 章节级锚（on=summary/title，unit:null）：space=summary/title，digest 取该节写行/标题
    // 的 token 流（§4.9：summary→chunk.lad.take（译文 lane=tr[locale].lad.take）；
    // title→[{t:标题文本}]）。annotation.block_id 由 putEngineNote 映射到稳定标题块。
    if (anchorOn === "summary" || anchorOn === "title") {
      const sectionId = String(anchor.section || "");
      const chunk = (foldData?.chunks ?? [] as Array<Record<string, unknown>>).find(
        c => (c as { id?: string }).id === sectionId) as
        { id: string; title?: string; lad?: { take?: unknown[] }; tr?: Record<string, { title?: string; lad?: { take?: unknown[] } }> } | undefined;
      if (!chunk) return undefined;
      const trLane = anchorOn === "summary" && locale !== "source" ? (chunk.tr?.[locale] ?? null) : null;
      const atoms = (foldData?.atoms ?? {}) as Record<string, { kind?: string; alt?: string; text?: string }>;
      let digest: string | null;
      if (anchorOn === "summary") {
        const take = ((trLane?.lad ?? chunk.lad ?? {}).take ?? []) as unknown[];
        digest = tokenSpaceDigest(take as never, atoms as never);
      } else {
        const title = String(trLane?.title ?? chunk.title ?? "");
        digest = title ? tokenSpaceDigest([{ t: title }] as never, atoms as never) : null;
      }
      if (!digest) return undefined;
      const anchorCopy = JSON.parse(JSON.stringify(anchor)) as Record<string, unknown>;
      anchorCopy.lang = locale;
      return {
        version: 1 as const, locale, space: anchorOn, digest,
        thread: Array.isArray(note.thread) ? note.thread : undefined,
        anchor: anchorCopy,
      };
    }
    const unitId = String(anchor.unit || "");
    const level = Number(anchor.level ?? 4);
    const shown = anchor.shown as { words?: number[] } | undefined;
    // 空间分派按 actual lane 语义（backend §4.8b），不只 level：
    // - 写行选词（shown）→ brief（words 索引该 lane 的 lad.brief）；
    // - L3 且目标 lane brief_source==='derived' 且有 derived_brief → derived（独立 token 空间）；
    //   源 Brief / translation-local Brief / L2/L1 无 shown → full（tokens/sents 索引全文）。
    const units = foldData?.units as Record<string, { tr?: Record<string, { lad?: { brief_source?: string; derived_brief?: { toks?: unknown[] } } }> }> | undefined;
    const laneLad = pane === "trans" && targetLocale ? units?.[unitId]?.tr?.[targetLocale]?.lad : undefined;
    const isDerivedLane = laneLad?.brief_source === "derived" && Array.isArray(laneLad?.derived_brief?.toks) && (laneLad?.derived_brief?.toks as unknown[]).length > 0;
    const space: "full" | "brief" | "derived" =
      shown ? "brief"
        : level === 3 && isDerivedLane ? "derived"
          : "full";
    const atoms = (foldData?.atoms ?? {}) as Record<string, { kind?: string; alt?: string; text?: string }>;
    const spaceToks = anchorSpaceToks(unitId, space, locale);
    const toks = spaceToks && "toks" in spaceToks ? (spaceToks.toks as unknown[]) : undefined;
    const digest = toks ? tokenSpaceDigest(toks as never, atoms) : null;
    if (!digest) return undefined;
    const anchorCopy = JSON.parse(JSON.stringify(anchor)) as Record<string, unknown>;
    // 自然锚逐字保留（backend eedf20d0 接受自然 brief+sents——sents 可选 Full 来源，不剥离）；
    // anchor.lang 是内核可见语言（marksFor 比较 a.lang===lang）：源=meta.lang，译=targetLocale
    anchorCopy.lang = pane === "trans" ? (targetLocale || "source") : (String((foldData?.meta as { lang?: string } | undefined)?.lang) || "source");
    return {
      version: 1 as const, locale, space, digest,
      thread: Array.isArray(note.thread) ? note.thread : undefined,
      anchor: anchorCopy,
    };
  };
  /** 恢复校验：同 locale + 同空间 token 指纹一致才允许原生锚复用（产物重折/换 locale 即失效，保留笔记不错标）。 */
  const engineAnchorUsable = (a: ReaderAnnotation): Record<string, unknown> | null => {
    const ea = a.engine_anchor;
    if (!ea || ea.version !== 1 || !ea.anchor) return null;
    if (ea.locale !== laneLocale(a.pane)) return null;
    const atoms = (foldData?.atoms ?? {}) as Record<string, { kind?: string; alt?: string; text?: string }>;
    const nAnchor = ea.anchor as {
      on?: string; unit?: string; section?: string; lang?: string;
      tokens?: number[]; sents?: number[]; shown?: { words?: number[]; exact?: string }; quote?: { exact?: string };
    };
    const anchorOn = String(nAnchor.on || "unit");
    // 章节级锚（summary/title）：digest 对该节写行/标题 token 流；tokens 范围与 quote 逐字校验；
    // block_id 必须是该节稳定标题块（与 backend chunk.block_id 核验同语义）。
    if (ea.space === "summary" || ea.space === "title") {
      const sectionId = String(nAnchor.section || "");
      const chunk = (foldData?.chunks ?? [] as Array<Record<string, unknown>>).find(
        c => (c as { block_id?: string }).block_id === a.block_id) as
        { id: string; title?: string; lad?: { take?: unknown[] }; tr?: Record<string, { title?: string; lad?: { take?: unknown[] } }> } | undefined;
      if (!chunk || chunk.id !== sectionId) return null;
      const trLane = ea.space === "summary" && ea.locale !== "source" ? (chunk.tr?.[ea.locale] ?? null) : null;
      if (ea.space === "summary") {
        const take = ((trLane?.lad ?? chunk.lad ?? {}).take ?? []) as unknown[];
        const digest = tokenSpaceDigest(take as never, atoms as never);
        if (digest !== ea.digest) return null;
        const [x, y] = Array.isArray(nAnchor.tokens) ? nAnchor.tokens : [-1, -1];
        if (x < 0 || y >= take.length) return null;
        const claimed = String(nAnchor.quote?.exact ?? "");
        if (claimed && toksTextLike(take as never, x, y, atoms as never) !== claimed) return null;
        return ea.anchor;
      }
      // title：digest 对标题文本；quote 必须在标题文本内
      const title = String(trLane?.title ?? chunk.title ?? "");
      const digest = title ? tokenSpaceDigest([{ t: title }] as never, atoms as never) : null;
      if (!title || digest !== ea.digest) return null;
      const claimed = String(nAnchor.quote?.exact ?? "");
      if (claimed && !title.includes(claimed)) return null;
      return ea.anchor;
    }
    const spaceToks = anchorSpaceToks(a.block_id, ea.space, laneLocale(a.pane));
    const toks = spaceToks && "toks" in spaceToks ? (spaceToks.toks as unknown[]) : undefined;
    if (!toks) return null;
    if (tokenSpaceDigest(toks as never, atoms) !== ea.digest) return null;   // 产物已变更：锚失效
    // 区间精确校验：锚 tokens/shown.words 必须与其声称的引文逐字一致（相同短语重复出现也不接受错位）
    const toksArr = toks as never[];
    const rangeText = (x: number, y: number) => toksTextLike(toks as never, Math.max(0, x), Math.min(y, toksArr.length - 1), atoms);
    let intervalOk = false;
    if (nAnchor.shown && Array.isArray(nAnchor.shown.words)) {
      const [x, y] = nAnchor.shown.words;
      intervalOk = rangeText(x, y) === nAnchor.shown.exact;
    } else if (Array.isArray(nAnchor.tokens) && nAnchor.tokens.length === 2) {
      const [x, y] = nAnchor.tokens;
      const claimed = (nAnchor.quote?.exact ?? a.quote?.exact ?? "").trim();
      // backend 存储上限 500 截断：严格一致 = rangeText === claimed（backend 已截，前端不补）
      intervalOk = !claimed || rangeText(x, y) === claimed;
    } else {
      intervalOk = false;
    }
    if (!intervalOk) return null;   // 失效：保留笔记，回退 block 呈现，不伪精确重标
    return ea.anchor;
  };

  /** 章节标题块映射：摘要锚（on:'summary'）的 block_id 用该节真实标题块（有稳定 id/bbox）。 */
  const sectionTitleBlock = useCallback((sectionId: string): string => {
    const sec = sectionIndexById.get(sectionId);
    if (sec?.block_id) return sec.block_id;
    return (layersSrc?.sections ?? []).find(s => s.id === sectionId)?.block_id ?? "";
  }, [sectionIndexById, layersSrc]);

  /** 内核 note ↔ 阅读层批注映射（textfold/notes v1）。摘要锚（on:'summary'）映射到标题 block_id。 */
  const toEngineNote = (a: ReaderAnnotation) => {
    // 原生锚优先：同产物同 locale（digest 校验）→ 逐字恢复 shown/level/tokens/sents/lang/quote/on
    const nativeAnchor = engineAnchorUsable(a);
    if (nativeAnchor) {
      return {
        id: a.id, kind: (a.note ?? "").trim() ? "note" : "highlight",
        created: a.created_at, updated: a.updated_at,
        anchor: nativeAnchor,
        text: a.note ?? "",
        thread: (a.engine_anchor?.thread as unknown[] | undefined) ?? [],
        color: a.color, shade: 2,
      };
    }
    // 失效锚（含 summary/title）：统一回退 block+quote 引用锚（内核 margin/list 呈现，
    // tokens/sents 恒 null——不把旧短层引文伪成精确 token 重锚，不误标）。
    return {
    id: a.id, kind: (a.note ?? "").trim() ? "note" : "highlight",
    created: a.created_at, updated: a.updated_at,
    anchor: (() => {
      // 跨模式锚定：React 字符偏移 → 内核 token 下标（确定性映射；不可信则 null+quote 由内核重锚）。
      // level：range=4（Full，token 下标与字符偏移互转）；block 锚无逐字范围 → tokens null，
      // 内核以 margin/列表呈现整段标记，不伪造全文偏移。
      const tokens = a.anchor === "range" ? ((tokenMapForRef.current?.(a.block_id, a.pane))?.charToToken(a.start, a.end) ?? null) : null;
      const lang = a.pane === "trans" ? (targetLocale || "source") : (String((foldData?.meta as { lang?: string } | undefined)?.lang) || "source");
      return {
        unit: a.block_id, section: null, lang,
        level: a.anchor === "range" ? 4 : 1, tokens, sents: null,
        // 失效回退：保留 quote 作为笔记引用；tokens/sents 恒 null，不把旧短层引文伪成 Full token 重锚
        quote: { exact: a.quote.exact, prefix: a.quote.prefix, suffix: a.quote.suffix },
      };
    })(),
    text: a.note ?? "", thread: [], color: a.color, shade: 2,
    } as { id: string; kind: string; created: string; updated: string; anchor: Record<string, unknown>; text: string; thread: unknown[]; color: string; shade: number };
  };
  /** 实时 overlay：连续内核 RPC（put→delete）不能读渲染闭包——旧闭包会把已删条目写回。 */
  const overlayLive = useRef<ReaderOverlay | null>(null);
  overlayLive.current = overlay;
  const putEngineNote = (note: Record<string, unknown>, pane: ReaderPane) => {
    const overlay = overlayLive.current;
    if (!overlay) throw new Error("阅读层尚未加载，暂不能保存批注");
    const anchor = (note.anchor ?? {}) as Record<string, unknown>;
    const quote = (anchor.quote ?? {}) as { exact?: string; prefix?: string; suffix?: string };
    const kernelTokens = Array.isArray(anchor.tokens) ? (anchor.tokens as number[]) : null;
    const kernelLevel = Number(anchor.level ?? 4);
    // 仅 Full 层（level=4）的 token 下标映射回字符偏移；派生/短层空间独立，落 block 锚+quote
    // 摘要锚（on:'summary'）：block_id 映射到章节标题块（真实稳定 id）；普通 unit 锚不变
    const anchorOn = String(anchor.on || "unit");
    const anchorSection = String(anchor.section || "");
    const isSummary = anchorOn === "summary" || anchorOn === "title";
    const blockId = isSummary && anchorSection
      ? sectionTitleBlock(anchorSection) || anchorSection
      : String(anchor.unit || "");
    const charRange = kernelTokens && kernelLevel === 4 && !isSummary ? (tokenMapForRef.current?.(String(anchor.unit || ""), pane))?.tokenToChar(kernelTokens[0], kernelTokens[1]) ?? null : null;
    const tokens = charRange ? kernelTokens : null;
    const id = typeof note.id === "string" ? note.id : uid();
    // 短层/派生原生锚持久化：逐字快照内核 anchor + 空间指纹（重开同产物/locale 精确复原）
    const engineAnchor = makeEngineAnchor(note, pane);
    const existing = overlay.annotations.find(a => a.id === id);
    const next: ReaderOverlay = existing
      ? { ...overlay, annotations: overlay.annotations.map(a => {
          if (a.id !== id) return a;
          return { ...a, note: String(note.text ?? ""), color: (typeof note.color === "string" ? note.color : a.color), engine_anchor: engineAnchor ?? a.engine_anchor, updated_at: new Date().toISOString() } as ReaderAnnotation;
        }) }
      : { ...overlay, annotations: [...overlay.annotations, {
          id, block_id: blockId, pane,
          anchor: isSummary ? "block" : charRange ? "range" : "block", start: charRange ? charRange[0] : 0, end: charRange ? charRange[1] : 0,
          quote: { exact: String(quote.exact ?? ""), prefix: String(quote.prefix ?? ""), suffix: String(quote.suffix ?? "") },
          color: (typeof note.color === "string" ? note.color : "y") as ReaderColor2,
          note: String(note.text ?? ""), detached: false,
          engine_anchor: engineAnchor,
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        } as ReaderAnnotation] };
    overlayLive.current = next;
    setOverlay(next);
    // 取消防抖队列：旧排队 payload 不得覆盖本次写入；回执等真实 CAS 落盘
    window.clearTimeout(saveTimer.current);
    pendingOverlay.current = null;
    return { ok: true, persisted: persistOverlayNow(next) };
  };
  /** 立即持久化（绕过防抖）：CAS 200 才算成功；冲突重载并以错误回执内核（不显示已保存）。 */
  /** 保存串行队列：所有 PUT 依序执行，比较代次丢弃旧 payload，杜绝防抖旧数据覆盖新删除/写入。 */
  const saveQueueRef = useRef<{ seq: number; promise: Promise<void> }>({ seq: 0, promise: Promise.resolve() });
  const saveSeqRef = useRef(0);
  const enqueueOverlaySave = useCallback((next: ReaderOverlay, seq: number) => {
    const entry = { seq, promise: Promise.resolve() };
    entry.promise = saveQueueRef.current.promise.then(async () => {
      if (seq < saveQueueRef.current.seq) return;   // 已有更新 payload 排队：跳过旧者
      const saved = await readerApi.saveOverlay(projectId, next, next.revision);
      if (seq < saveQueueRef.current.seq) return;
      overlayLive.current = saved;
      setOverlay(saved);
      pendingOverlay.current = null;
    }).catch((error) => {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("已被另一个窗口更新")) {
        void readerApi.state(projectId, targetLocale || "source").then(value => {
          if (value) { overlayLive.current = value.overlay; setOverlay(value.overlay); setState(value); }
        });
        throw new Error("阅读层已被其他窗口更新（CAS），本次批注未保存，请重试");
      }
      throw new Error(`阅读层保存失败：${message}`);
    });
    saveQueueRef.current = entry;
    return entry.promise;
  }, [projectId, targetLocale]);   // eslint-disable-line react-hooks/exhaustive-deps
  const persistOverlayNow = (next: ReaderOverlay): Promise<void> => {
    return enqueueOverlaySave(next, ++saveSeqRef.current);
  };
  const persistOverlayNowImpl = async (next: ReaderOverlay): Promise<void> => {
    try {
      const saved = await readerApi.saveOverlay(projectId, next, next.revision);
      overlayLive.current = saved;
      setOverlay(saved);
      pendingOverlay.current = null;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("已被另一个窗口更新")) {
        const value = await readerApi.state(projectId, targetLocale || "source").catch(() => null);
        if (value) { setOverlay(value.overlay); setState(value); }
        throw new Error("阅读层已被其他窗口更新（CAS），本次批注未保存，请重试");
      }
      throw new Error(`阅读层保存失败：${message}`);
    }
  };
  const deleteEngineNote = (noteId: string) => {
    const overlay = overlayLive.current;
    if (!overlay) throw new Error("阅读层尚未加载");
    // 用户语义「删除笔记」= 整条移除（detached 只表示锚失效，不是回收站）；与普通阅读删除一致
    const next: ReaderOverlay = { ...overlay, annotations: overlay.annotations.filter(a => a.id !== noteId) };
    overlayLive.current = next;
    setOverlay(next);
    window.clearTimeout(saveTimer.current);
    pendingOverlay.current = null;
    return { ok: true, persisted: persistOverlayNow(next) };
  };
  toEngineNoteRef.current = toEngineNote;
  putEngineNoteRef.current = putEngineNote;
  deleteEngineNoteRef.current = deleteEngineNote;
  const srcEngine = useTextFoldEngine({
    data: foldStage === "on" ? foldData : null,
    initialLevel: 5 - clamp(prefs.level, 1, 5),   // 产品 1..5(全文..主题) → 上游 4..0(Topic..Full)
    onEvent: makeEngineOnEvent("src"),
    litUid: litForSrc,
    theme: prefs.appearance,
    rpc: makeRpc("source"),
    onActivate: uid => activateBlock(uid, "source"),
  });
  const dstEngine = useTextFoldEngine({
    data: foldStage === "on" && hasTrans ? foldData : null,
    initialLevel: 5 - clamp(prefs.level, 1, 5),   // 产品 1..5(全文..主题) → 上游 4..0(Topic..Full)
    onEvent: makeEngineOnEvent("dst"),
    litUid: litForDst,
    theme: prefs.appearance,
    rpc: makeRpc("trans"),
    locale: targetLocale || undefined,   // 实际 locale，不 hardcode
    onActivate: uid => activateBlock(uid, "trans"),
  });
  const setLevel = (value: number) => {
    const next = clamp(value, 1, 5);
    patchPrefs({ level: next });
    setOpenBlocks(new Set());
    setOpenSections(new Set());
    srcEngine.handle.current?.setLevel(5 - next);
    dstEngine.handle.current?.setLevel(5 - next);
  };
  const srcEngineRef = useRef<TextFoldPaneHandle | null>(null);
  srcEngineRef.current = srcEngine.handle.current;
  useEffect(() => { srcEngineRef.current = srcEngine.handle.current; }, [srcEngine.ready]);
  const dstEngineRef = useRef<TextFoldPaneHandle | null>(null);
  dstEngineRef.current = dstEngine.handle.current;
  useEffect(() => { dstEngineRef.current = dstEngine.handle.current; }, [dstEngine.ready]);

  // 分层重折/重载后，openSections 里可能残留已不存在的 section id：过滤掉，避免「展开」渲染成空壳。
  useEffect(() => {
    const known = new Set<string>([
      ...((layersSrc?.sections ?? []).map(section => section.id)),
      ...sectionIndex.map(item => item.id),
    ]);
    if (!known.size) return;
    setOpenSections(prev => {
      const next = new Set([...prev].filter(id => known.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [layersSrc, sectionIndex]);

  /* ---------- 三栏几何 ---------- */
  const effectiveNarrowPane: "origin" | "source" | "trans" =
    (["origin", "source", "trans"] as const).find(k => prefs.panes[k] && k === narrowPane)
    ?? (["origin", "source", "trans"] as const).find(k => prefs.panes[k])
    ?? "source";
  /** 窄屏：三个选框=开启集合，实际只显示当前 tab 指向栏；宽屏：各自独立显隐。 */
  const paneVisible = (key: "origin" | "source" | "trans") =>
    isNarrow ? key === effectiveNarrowPane : prefs.panes[key];
  const paneKeys = useMemo<(("origin" | "source" | "trans"))[]>(() => {
    const keys = (["origin", "source", "trans"] as const).filter(paneVisible);
    return keys.length ? [...keys] : ["source"];
  }, [prefs.panes, isNarrow, effectiveNarrowPane]);
  useEffect(() => {
    // 宽度按像素权重归一（拖宽像素级一致）；面板可见性变化时重算。
    const main = document.getElementById("rdrr-main");
    if (!main || paneKeys.length === 0) return;
    if (isNarrow) {
      paneKeys.forEach(k => {
        const el = document.getElementById("rdrr-pane-" + k);
        if (el) { el.style.flexGrow = "1"; el.style.flexBasis = "0%"; }
      });
      return;
    }
    const dvCount = paneKeys.length - 1;
    const available = Math.max(320, main.clientWidth - dvCount * 6);
    const sum = paneKeys.reduce((acc, k) => acc + (prefs.weights[paneKeys.indexOf(k)] || 1), 0);
    paneKeys.forEach(k => {
      const el = document.getElementById("rdrr-pane-" + k);
      if (el) {
        el.style.flexGrow = String((prefs.weights[paneKeys.indexOf(k)] || 1) / sum * available);
        el.style.flexBasis = "0%";
      }
    });
  }, [paneKeys, prefs.weights, isNarrow]);

  const onDividerDown = (leftKey: "origin" | "source" | "trans") => (event: React.PointerEvent) => {
    const divider = event.currentTarget as HTMLElement;
    const left = document.getElementById("rdrr-pane-" + leftKey);
    const leftIndex = paneKeys.indexOf(leftKey);
    const rightKey = paneKeys[leftIndex + 1];
    if (!left || !rightKey) return;
    const right = document.getElementById("rdrr-pane-" + rightKey);
    if (!right) return;
    divider.setPointerCapture(event.pointerId);
    const startL = left.getBoundingClientRect().width, startR = right.getBoundingClientRect().width;
    const total = startL + startR, startX = event.clientX, MINW = 240;
    if (total < MINW * 2) return;
    const move = (ev: PointerEvent) => {
      const nl = clamp(startL + (ev.clientX - startX), MINW, total - MINW);
      const li = paneKeys.indexOf(leftKey), ri = paneKeys.indexOf(rightKey);
      const nextWeights = [...prefs.weights];
      nextWeights[li] = nl; nextWeights[ri] = total - nl;
      patchPrefs({ weights: nextWeights });
      left.style.flexGrow = String(nl); right.style.flexGrow = String(total - nl);
    };
    const up = () => {
      divider.removeEventListener("pointermove", move);
      divider.removeEventListener("pointerup", up);
    };
    divider.addEventListener("pointermove", move);
    divider.addEventListener("pointerup", up);
  };

  /* ---------- 跟随滚动：文本栏驱动对侧文本 + 原件 bbox ---------- */
  const followFrame = useRef(0);
  const onTextScroll = (srcKey: ReaderPane) => () => {
    if (!prefs.follow || suppressed()) return;
    if (followFrame.current) return;            // 每帧只处理一次，避免长列表滚动时逐块触发
    followFrame.current = window.requestAnimationFrame(() => {
      followFrame.current = 0;
      const body = (srcKey === "source" ? bodyRefs.source : bodyRefs.trans).current;
      if (!body) return;
      const top = body.scrollTop + 110;
      const seen: HTMLElement[] = [];
      body.querySelectorAll<HTMLElement>("[data-block-id]").forEach(el => {
        if (el.getClientRects().length && blkTop(body, el) <= top) seen.push(el);
      });
      const id = seen.length ? (seen[seen.length - 1].dataset.blockId || "") : "";
      if (!id) return;
      setCurrentBlock(id);
      // 对侧文本栏同步（与「同步定位」开关无关：跟随滚动就是对侧跟随）
      const other: ReaderPane = srcKey === "source" ? "trans" : "source";
      if (paneMounted(other)) {
        const t = blockEl(other, id);
        const otherBody = other === "source" ? bodyRefs.source.current : bodyRefs.trans.current;
        if (t && otherBody) {
          suppress(180);
          otherBody.scrollTop = blkTop(otherBody, t) - 100;
        }
      }
      // 原件：按**真实 bbox** 居中（不按页号滚动）
      void centerOriginOn(id);
    });
  };
  useEffect(() => () => { if (followFrame.current) window.cancelAnimationFrame(followFrame.current); }, []);

  // 切层后把原件同步到当前块：L4/L5 只渲染节卡片，文本栏常常不可滚；
  // 若不在这里同步，用户切到主线/主题层时原件会停在上一层的位置。
  useEffect(() => {
    if (!prefs.follow) return;
    const id = currentBlock || selected;
    if (!id) return;
    const timer = window.setTimeout(() => { void centerOriginOn(id); }, 60);
    return () => window.clearTimeout(timer);
  }, [level]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ---------- 跟随滚动：原件驱动文本栏（bbox 中心最近的块） ---------- */
  const originFollowFrame = useRef(0);
  const onOriginScroll = useCallback(() => {
    if (!prefs.follow || suppressed()) return;
    if (originFollowFrame.current) return;
    originFollowFrame.current = window.requestAnimationFrame(() => {
      originFollowFrame.current = 0;
      const body = bodyRefs.origin.current;
      if (!body) return;
      const cRect = body.getBoundingClientRect();
      const mid = cRect.top + cRect.height / 2;
      let bestId = "", bestDist = Number.POSITIVE_INFINITY;
      body.querySelectorAll(".pdf-overlay [data-block-id]").forEach(node => {
        const el = node as HTMLElement;
        if (!el.getClientRects().length) return;
        const r = el.getBoundingClientRect();
        const dist = Math.abs(r.top + r.height / 2 - mid);
        if (dist < bestDist) { bestDist = dist; bestId = el.dataset.blockId || ""; }
      });
      if (!bestId) return;
      setCurrentBlock(bestId);
      scrollTextPaneTo("source", bestId);
      scrollTextPaneTo("trans", bestId);
    });
  }, [prefs.follow, scrollTextPaneTo]);
  useEffect(() => () => { if (originFollowFrame.current) window.cancelAnimationFrame(originFollowFrame.current); }, []);

  /* ---------- hover：只做视觉联动，绝不滚动 ---------- */
  const [hoverBlock, setHoverBlock] = useState("");
  const hoverProps = (blockId: string) => ({
    onMouseEnter: () => setHoverBlock(blockId),
    onMouseLeave: () => setHoverBlock(prev => (prev === blockId ? "" : prev)),
  });
  const hoverToken = hoverBlock + "|" + level + "|" + paneKeys.join(",") + "|" + (layersSrc?.sections.length ?? 0);
  useEffect(() => {
    // 文本栏 hover 不做整块高亮：双栏对应段前小点同时亮（原译同步），跨栏原件 bbox 轻描边保留。
    const roots: [HTMLElement | null, string][] = [
      [bodyRefs.source.current, "rdrr-dlit"],
      [bodyRefs.trans.current, "rdrr-dlit"],
      [bodyRefs.origin.current, "rdrr-bbox-hover"],
    ];
    roots.forEach(([root, cls]) => root?.querySelectorAll("." + cls).forEach(node => node.classList.remove(cls)));
    if (!hoverBlock) return;
    roots.forEach(([root, cls]) => {
      if (!root) return;
      root.querySelectorAll(`[data-block-id="${CSS.escape(hoverBlock)}"]`).forEach(node => node.classList.add(cls));
    });
  }, [hoverToken, hoverBlock]);

  /* ---------- 统一选段操作：右键整段与划词同一入口（参考 TextFold selbar，Apache-2.0） ----------
   * - 右键段落 = 像划词一样选中整段，弹出与划词相同的操作条；左键不再弹任何 bar（只定位/局部展开）。
   * - 已有标记的段落右键直接进入编辑态（笔记/换色/移除/追问），同一入口，绝不同时出两个 bar。 */
  type SelectionTarget = {
    x: number; y: number;
    blockId: string; pane: ReaderPane;
    /** range=真实字符范围（划词/右键整段）；block=无逐字范围块（标题/摘要/图表）的整段锚点标记。 */
    mode: "range" | "block";
    text: string; start: number; end: number;
  };
  const [selectionPop, setSelectionPop] = useState<SelectionTarget | null>(null);
  /** 上次使用的高亮颜色（上游 lastShade 习惯：点色点直接套用，选择被记住）。 */
  const [lastColor, setLastColor] = useState<ReaderColor2>(() => {
    try {
      const saved = localStorage.getItem("rdrr-hl-color");
      return (COLORS as string[]).includes(saved ?? "") ? (saved as ReaderColor2) : "y";
    } catch { return "y"; }
  });

  const blockVerbatimText = (blockId: string, pane: ReaderPane): string => {
    const block = doc.blocks.find(b => b.block_id === blockId);
    if (!block) return "";
    if (block.type === "list") {
      const items = pane === "trans" ? (translation?.entries[blockId]?.items ?? []) : block.items;
      return (items ?? []).filter(Boolean).join(" ");
    }
    if (block.type === "paragraph" || block.type === "code" || block.type === "footnote") {
      const entry = translation?.entries[blockId];
      const text = pane === "trans" && entry?.text ? entry.text : block.source_text;
      return (text ?? "").trim();
    }
    return "";   // 标题/节锚点、图/表/公式：没有可逐字标记的正文
  };

  /** 已存在的「整段/整节」标记（block 锚点，或覆盖整段的 range）。 */
  const wholeAnnotationOf = (blockId: string, pane: ReaderPane): ReaderAnnotation | null => {
    if (!overlay) return null;
    const text = blockVerbatimText(blockId, pane);
    return overlay.annotations.find(a => a.block_id === blockId && a.pane === pane && !a.detached &&
      (a.anchor === "block" || (text.length > 0 && a.start === 0 && a.end === text.length))) ?? null;
  };

  /** 右键入口：已标记段落 → 直接进编辑态；可逐字段落 → 选中整段（真实 range）；
   *  其它（标题/摘要/图表）→ 整段锚点 bar，不伪造字符区间。 */
  const openWholeSelection = (pane: ReaderPane, blockId: string, el: HTMLElement | null, point?: { clientX: number; clientY: number }) => {
    const rect = el?.getBoundingClientRect();
    const existing = wholeAnnotationOf(blockId, pane);
    if (existing) {
      openHighlightPop(existing, rect ?? new DOMRect(point?.clientX ?? 0, point?.clientY ?? 0, 0, 0));
      return;
    }
    const text = blockVerbatimText(blockId, pane);
    if (text && el && el.dataset.annotatable && rect) {
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel?.removeAllRanges(); sel?.addRange(range);
      setHlPop(null);
      setSelectionPop({
        x: clamp(rect.left + rect.width / 2 - 90, 6, Math.max(6, innerWidth - 190)),
        y: clamp(rect.top - 46, 6, Math.max(6, innerHeight - 90)),
        blockId, pane, mode: "range", text, start: 0, end: text.length,
      });
      return;
    }
    setHlPop(null);
    setSelectionPop({
      x: clamp((point?.clientX ?? (rect ? rect.left + rect.width / 2 : innerWidth / 2)) - 90, 6, Math.max(6, innerWidth - 190)),
      y: clamp((point?.clientY ?? (rect ? rect.top : innerHeight / 2)) - 46, 6, Math.max(6, innerHeight - 90)),
      blockId, pane, mode: "block", text: "", start: 0, end: 0,
    });
  };

  /** 由当前选段目标构造标记（不提交；提交由调用方完成）。已存在的整段标记直接复用。 */
  const createAnnotationFromSelection = (color: ReaderColor2): { annotation: ReaderAnnotation; existed: boolean } | null => {
    const target = selectionPop;
    if (!target || !overlay) return null;
    if (target.mode === "block") {
      const existing = wholeAnnotationOf(target.blockId, target.pane);
      if (existing) return { annotation: existing, existed: true };
      return {
        annotation: {
          id: uid(), block_id: target.blockId, pane: target.pane,
          anchor: "block", start: 0, end: 0,
          quote: { exact: "", prefix: "", suffix: "" },
          color, note: "", detached: false,
          created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
        },
        existed: false,
      };
    }
    const blkText = (blockEl(target.pane, target.blockId)?.textContent ?? "").slice(0, target.end + 400);
    return {
      annotation: {
        id: uid(), block_id: target.blockId, pane: target.pane,
        start: target.start, end: target.end,
        quote: {
          exact: blkText.slice(target.start, target.end),
          prefix: blkText.slice(Math.max(0, target.start - 16), target.start),
          suffix: blkText.slice(target.end, target.end + 16),
        },
        color, note: "", detached: false,
        created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
      },
      existed: false,
    };
  };

  const noteFromSelection = () => {
    const target = selectionPop;
    if (!target || !overlay) return;
    const made = createAnnotationFromSelection(lastColor);
    if (!made) return;
    let working = overlay;
    if (!made.existed) {
      working = { ...overlay, annotations: [...overlay.annotations, made.annotation] };
      setOverlay(working); scheduleSave(working);
    }
    const rect = blockEl(target.pane, target.blockId)?.getBoundingClientRect()
      ?? new DOMRect(target.x, target.y, 0, 0);
    openHighlightPop(made.annotation, rect);
  };

  const askFromSelection = () => {
    const target = selectionPop;
    if (!target) return;
    const sourceText = doc.blocks.find(b => b.block_id === target.blockId)?.source_text ?? "";
    setQuote(target.blockId, target.pane,
      (target.mode === "range" && target.text ? target.text : (sourceText || blockVerbatimText(target.blockId, target.pane))).slice(0, 180));
    setSelectionPop(null);
    setChatOpen(true);
  };

  /* ---------- 划词 / 高亮 ---------- */
  const [hlPop, setHlPop] = useState<{ hid: string; x: number; y: number } | null>(null);
  const [hlDraft, setHlDraft] = useState("");

  // 划词收集：原生 mouseup 监听（原型同款）。React 合成 onMouseUp 在该子树不稳定，原生事件可靠。
  const handleMouseUp = useCallback((pane: ReaderPane, event: MouseEvent) => {
    if (event.button === 2) return;   // 右键走 contextmenu（整段选中），mouseup 不重复处理
    const target = event.target as HTMLElement;
    if (target.closest("mark.hl, button, #hlPop, #selPop")) return;
    window.setTimeout(() => {
      const selection = window.getSelection();
      if (!selection || selection.isCollapsed || !selection.rangeCount) { setSelectionPop(null); return; }
      const text = selection.toString().trim();
      if (!text) { setSelectionPop(null); return; }
      const range = selection.getRangeAt(0);
      const startEl = ((range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement) as Element | null)?.closest("[data-block-id]") as HTMLElement | null;
      const endEl = ((range.endContainer.nodeType === 1 ? range.endContainer : range.endContainer.parentElement) as Element | null)?.closest("[data-block-id]") as HTMLElement | null;
      if (!startEl || startEl !== endEl) { setSelectionPop(null); toast("划词仅支持同一段落内；跨段请分次选择"); return; }
      const blockId = startEl.dataset.blockId || "";
      const from = textOffset(startEl, range.startContainer, range.startOffset);
      const to = textOffset(startEl, range.endContainer, range.endOffset);
      if (to <= from) { setSelectionPop(null); return; }
      const rect = range.getBoundingClientRect();
      setHlPop(null);
      setSelectionPop({
        x: clamp(rect.left + rect.width / 2 - 90, 6, innerWidth - 190),
        y: clamp(rect.top - 46, 6, innerHeight - 90),
        blockId, pane, mode: "range", text, start: from, end: to,
      });
    }, 10);
  }, [toast]);

  useEffect(() => {
    const entries: [HTMLElement | null, ReaderPane][] = [
      [bodyRefs.source.current, "source"],
      [bodyRefs.trans.current, "trans"],
    ];
    const cleanups: (() => void)[] = [];
    entries.forEach(([el, pane]) => {
      if (!el) return;
      const handler = (event: MouseEvent) => handleMouseUp(pane, event);
      el.addEventListener("mouseup", handler);
      cleanups.push(() => el.removeEventListener("mouseup", handler));
    });
    return () => cleanups.forEach(fn => fn());
  }, [handleMouseUp, projectId, overlay !== null, loadError, prefs.panes.origin, prefs.panes.source, prefs.panes.trans]);

  // 右键 = 像划词一样选中整段，弹同一操作条（原生监听；经 ref 转发到最新实现，避免闭包过期）。
  const openWholeSelectionRef = useRef(openWholeSelection);
  openWholeSelectionRef.current = openWholeSelection;
  const overlayRef = useRef<ReaderOverlay | null>(null);
  overlayRef.current = overlay;
  const openHighlightPopRef = useRef<(a: ReaderAnnotation, rect: DOMRect) => void>(() => {});
  openHighlightPopRef.current = (a, rect) => openHighlightPop(a, rect);
  useEffect(() => {
    const entries: [HTMLElement | null, ReaderPane][] = [
      [bodyRefs.source.current, "source"],
      [bodyRefs.trans.current, "trans"],
    ];
    const cleanups: (() => void)[] = [];
    entries.forEach(([el, pane]) => {
      if (!el) return;
      const onContext = (event: MouseEvent) => {
        const target = event.target as HTMLElement;
        if (target.closest("button, a, input, textarea, #hlPop, #selPop")) return;
        // 右键点在已有高亮上 = 直接进入同一入口的编辑态
        const mark = target.closest("mark.hl") as HTMLElement | null;
        if (mark?.dataset.annId) {
          const ann = (overlayRef.current?.annotations ?? []).find(a => a.id === mark.dataset.annId);
          if (ann) { event.preventDefault(); openHighlightPopRef.current(ann, mark.getBoundingClientRect()); return; }
        }
        const el2 = target.closest("[data-block-id]") as HTMLElement | null;
        if (!el2) return;
        event.preventDefault();
        openWholeSelectionRef.current(pane, el2.dataset.blockId || "", el2, event);
      };
      el.addEventListener("contextmenu", onContext);
      cleanups.push(() => el.removeEventListener("contextmenu", onContext));
    });
    return () => cleanups.forEach(fn => fn());
  }, [projectId, overlay !== null, loadError, prefs.panes.origin, prefs.panes.source, prefs.panes.trans]);

  // 划词选区塔缩（点击别处）时收起 range 态操作条；block 态由 Esc / 外点关闭。
  useEffect(() => {
    const onSelChange = () => {
      setSelectionPop(prev => {
        if (!prev || prev.mode !== "range") return prev;
        const sel = window.getSelection();
        return !sel || sel.isCollapsed ? null : prev;
      });
    };
    document.addEventListener("selectionchange", onSelChange);
    return () => document.removeEventListener("selectionchange", onSelChange);
  }, []);

  const annotationsOf = (blockId: string, pane: ReaderPane) =>
    (overlay?.annotations ?? []).filter(a => a.block_id === blockId && a.pane === pane);

  const addAnnotation = (color: ReaderColor2) => {
    const target = selectionPop;
    if (!target || !overlay) return;
    setLastColor(color);
    try { localStorage.setItem("rdrr-hl-color", color); } catch { /* 私密窗口 */ }
    const made = createAnnotationFromSelection(color);
    if (!made) return;
    let next: ReaderOverlay;
    let message: string;
    if (made.existed) {
      if (made.annotation.color === color) { toast("这一段已经用该颜色标记"); return; }
      // 一个段落一个标记：换色改同一条，而不是叠出第二条
      next = { ...overlay, annotations: overlay.annotations.map(a => a.id === made.annotation.id
        ? { ...a, color, updated_at: new Date().toISOString() } : a) };
      message = "已更换标记颜色";
    } else {
      next = { ...overlay, annotations: [...overlay.annotations, made.annotation] };
      message = target.mode === "block"
        ? "已标记（该块没有逐字范围，只标到原文锚点；不写回原始文档）"
        : "已保存到项目阅读层（不写回原始文档）";
    }
    setOverlay(next); scheduleSave(next);
    setSelectionPop(null);
    window.getSelection()?.removeAllRanges();
    toast(message);
  };

  const openHighlightPop = (annotation: ReaderAnnotation, rect: DOMRect) => {
    setSelectionPop(null);
    setHlDraft(annotation.note ?? "");
    setHlPop({ hid: annotation.id, x: clamp(rect.left, 6, innerWidth - 296), y: clamp(rect.bottom + 8, 6, Math.max(6, innerHeight - 240)) });
  };

  const activeAnnotation = hlPop ? (overlay?.annotations ?? []).find(a => a.id === hlPop.hid) : null;

  const patchAnnotation = (id: string, patch: Partial<ReaderAnnotation>) => {
    if (!overlay) return;
    const next: ReaderOverlay = {
      ...overlay,
      annotations: overlay.annotations.map(a => a.id === id ? { ...a, ...patch, updated_at: new Date().toISOString() } : a),
    };
    setOverlay(next); scheduleSave(next);
  };

  const removeHighlight = () => {
    const a = activeAnnotation;
    if (!a || !overlay) return;
    let next: ReaderOverlay;
    let message: string;
    if ((a.note ?? "").trim()) {
      next = { ...overlay, annotations: overlay.annotations.map(x => x.id === a.id ? { ...x, detached: true, updated_at: new Date().toISOString() } : x) };
      message = "已移除高亮；笔记保留（纯笔记记录）";
    } else {
      next = { ...overlay, annotations: overlay.annotations.filter(x => x.id !== a.id) };
      message = "已移除高亮（原始文档从未被改动）";
    }
    setOverlay(next); scheduleSave(next);
    setHlPop(null);
    toast(message);
  };

  /* ---------- 统一对话（一次性引用 + 真实问答） ---------- */
  const [chatOpen, setChatOpen] = useState(false);
  const [chatDraft, setChatDraft] = useState("");
  const [activeQuote, setActiveQuote] = useState<ReaderChatWireQuote | null>(null);
  const [chatBinding, setChatBinding] = useState<ReaderModelBinding>(defaultChat ?? { service_id: "", model: "" });
  useEffect(() => { if (defaultChat?.service_id) setChatBinding(defaultChat); }, [defaultChat?.service_id, defaultChat?.model]); // eslint-disable-line react-hooks/exhaustive-deps
  // 切项目：清掉上一项目的引用卡/草稿/弹层（chat 历史由 useReaderChat 随 projectId 重载）
  useEffect(() => {
    setActiveQuote(null); setChatDraft(""); setSelectionPop(null); setHlPop(null);
  }, [projectId]);
  // 持久化对话（developer 的 readerChatClient）：历史/轮询/停止/重试都真实落地
  const chat = useReaderChat(projectId, true);
  const chatMessages = chat.messages;
  const chatBusy = chat.busy || chat.pending;

  /**
   * 聊天面板的模型来源展示：**以服务端 `/reader/state.roles.chat` 为准**。
   *
   * 之前只看 `translationBinding`：即使 reader_chat 被显式清空/绑定失效，也会显示成「继承翻译绑定」，
   * 把「不可用」说成「继承」。现在：
   * - source=role → 阅读对话自有绑定；
   * - source=inherited_translation → 继承翻译默认（明确写出来）；
   * - cleared/invalid/unset(且翻译也不可用) → 未配置/不可用 + 原因，不伪装成继承；
   * - 聊天输入区临时选的模型只影响下一条消息，单独标注「本条」，不改来源叙述。
   */
  const roleOf = (serviceId: string, model: string) => {
    const service = providers.find(item => item.service_id === serviceId);
    const entry = service?.models.find(item => item.model === model);
    // 服务列表还没加载完时退回 id，也要能说清“到底用的哪一个”，不能只剩空白。
    return [service?.name || serviceId, entry?.name || model].filter(Boolean).join(" · ");
  };
  const chatRole = state?.roles?.chat;
  const effectiveChat = chatRole?.effective;
  const effectiveChatLabel = effectiveChat?.service_id && effectiveChat.model ? roleOf(effectiveChat.service_id, effectiveChat.model) : "";
  // 只有**用户真的改选了不同于已保存绑定的服务/模型**才算临时覆盖：
  // - 「绑定了服务但没指定模型」是服务端首模型兜底；
  // - 「绑定的服务已删除」picker 会回显旧值，但那不是用户新选的，必须按不可用说明。
  const storedChat = chatRole?.binding;
  const sameAsStored = chatBinding.service_id === (storedChat?.service_id || "")
    && (chatBinding.model || "") === (storedChat?.model || "");
  const tempOverride = Boolean(chatBinding.service_id) && !sameAsStored;
  const chatRoleLabel = tempOverride
    ? `本条使用 ${roleOf(chatBinding.service_id, chatBinding.model) || chatBinding.model}（临时，不影响默认）`
    : !chatRole
      ? (translationBinding?.service_id ? `继承翻译默认 · ${translationBinding.model || "未指定模型"}` : "未配置阅读对话模型")
      : chatRole.source === "inherited_translation"
        ? `基于原文 · 继承翻译默认 · ${effectiveChatLabel || "未指定模型"}`
        : chatRole.source === "role"
          ? `基于原文 · 阅读对话模型 · ${effectiveChatLabel || "未指定模型"}${chatRole.model_source === "first_model" ? "（未指定，用该服务第一个模型）" : ""}`
          : chatRole.source === "cleared"
            ? "基于原文 · 未配置阅读对话模型（已显式清空，不会回退翻译）"
            : chatRole.source === "invalid"
              ? "基于原文 · 阅读对话模型不可用（绑定的服务/模型已失效）"
              : "基于原文 · 未配置阅读对话模型";
  const chatRoleTitle = chatRole?.problem || (chatRole ? `来源：${chatRole.source}` : "");

  const sectionLabel = (blockId: string) => {
    const info = unitIndex.get(blockId);
    return info ? info.section.title : "";
  };

  const setQuote = (blockId: string, pane: ReaderPane, snippet: string) => {
    const srcText = doc.blocks.find(b => b.block_id === blockId)?.source_text ?? "";
    setActiveQuote({ block_id: blockId, pane, snippet, srcText, sec: sectionLabel(blockId) });
  };

  // 一次性引用：只有 send 成功返回才消耗；失败保草稿/引用（useReaderChat 抛错时不清）
  const sendQuestion = async () => {
    const question = chatDraft.trim();
    if (!question) { toast("请输入问题"); return; }
    const frozen = activeQuote;
    try {
      await chat.send({
        question,
        locale: frozen && frozen.pane === "trans" ? (targetLocale || "source") : "source",
        binding: { service_id: chatBinding.service_id, model: chatBinding.model },
        quote: frozen ? { block_id: frozen.block_id, pane: "source", text: frozen.srcText } : null,
      });
      setActiveQuote(null);
      setChatDraft("");
      setChatOpen(true);
    } catch (error) {
      toast(error instanceof Error ? error.message : String(error));
      // 草稿与引用保留，可直接重试
    }
  };

  const stopChat = async () => {
    const last = [...chatMessages].reverse().find(m => m.role === "assistant" && m.state === "sending");
    if (!last) return;
    try { await chat.stop(last.id); } catch (error) { toast(error instanceof Error ? error.message : String(error)); }
  };

  const retryChat = async (messageId: string) => {
    const target = chatMessages.find(m => m.id === messageId);
    try {
      await chat.retry(messageId, target?.binding ?? chatBinding, "source");
    } catch (error) { toast(error instanceof Error ? error.message : String(error)); }
  };


  /* ---------- 原件（真实 PDF，复用 review 组件） ---------- */
  const [originWidth, setOriginWidth] = useState(0);
  useEffect(() => {
    const el = bodyRefs.origin.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setOriginWidth(el.clientWidth));
    observer.observe(el);
    setOriginWidth(el.clientWidth);
    const onScroll = () => {
      // 用**已渲染页元素**的真实偏移算当前页（估算页高在有图/表格时不准）。
      const pages = [...el.querySelectorAll(".pdf-page[data-page-index]")] as HTMLElement[];
      const mid = el.scrollTop + el.clientHeight / 2;
      let page = 0;
      for (const node of pages) {
        const index = Number(node.dataset.pageIndex || 0);
        // node.offsetTop 相对每个 relative 页容器恒为 0，会让所有页都命中、页号永远停在最后一页；
        // 必须用视口坐标换算回滚动内容坐标（blkTop）再比较。
        if (blkTop(el, node) <= mid) page = index;
      }
      setVisiblePage(clamp(page, 0, Math.max(0, pdf.pageCount - 1)));
      onOriginScroll();
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => { observer.disconnect(); el.removeEventListener("scroll", onScroll); };
  }, [prefs.panes.origin, overlay !== null, pdf.pageCount, onOriginScroll]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ---------- 渲染辅助 ---------- */
  const blockText = (block: Block, pane: ReaderPane): { text: string; fallback: boolean } => {
    if (pane === "source") return { text: block.source_text || "", fallback: false };
    const entry = translation?.entries[block.block_id];
    const value = (entry?.text || "").trim();
    return value ? { text: value, fallback: false } : { text: block.source_text || "", fallback: true };
  };

  const renderPassthrough = (block: Block, pane: ReaderPane) => {
    if (block.type === "image") {
      const refs = block.resource_refs ?? [];
      return (
        <figure className="rdrr-fig" data-block-id={block.block_id}>
          {refs.map(name => (
            <img key={name} src={apiUrl(`/projects/${projectId}/assets/${encodeURIComponent(name)}`)} alt={block.caption?.join(" ") || "插图"}
              loading="lazy" onError={event => { (event.target as HTMLImageElement).style.display = "none"; }} />
          ))}
          {!refs.length && <div className="rdrr-fig-fallback">图片资源缺失（{block.caption?.join(" ") || block.block_id}）</div>}
          {Boolean(block.caption?.length) && <figcaption>{block.caption.join(" ")}</figcaption>}
        </figure>
      );
    }
    if (block.type === "table") {
      const rows = new Map<number, string[]>();
      (block.cells ?? []).forEach(cell => {
        const list = rows.get(cell.row) ?? [];
        list[cell.column] = pane === "source" ? cell.text : (translation?.entries[block.block_id]?.cells?.[cell.cell_id] || cell.text || "—");
        rows.set(cell.row, list);
      });
      const ordered = [...rows.keys()].sort((a, b) => a - b).map(row => (rows.get(row) ?? []).map(v => v || "—"));
      return (
        <div className="rdrr-tblwrap" data-block-id={block.block_id}>
          <table className="rdrr-tbl">
            {Boolean(block.caption?.length) && <caption>{block.caption.join(" ")}</caption>}
            <tbody>
              {ordered.map((cells, index) => (
                <tr key={index}>{cells.map((value, col) => index === 0
                  ? <th key={col}>{value}</th>
                  : <td key={col}>{value}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    if (block.type === "equation") {
      return <div className="rdrr-formula" data-block-id={block.block_id}>{block.source_text}</div>;
    }
    if (block.type === "footnote") {
      const { text } = blockText(block, pane);
      return <aside className="rdrr-fn" data-block-id={block.block_id}>{text || block.source_text}</aside>;
    }
    const { text, fallback } = blockText(block, pane);
    if (block.type === "code") return <pre className="rdrr-code" data-block-id={block.block_id}>{text || block.source_text}</pre>;
    // 无分层（或后端未收录）时段落也走这里：保持与 renderAnnotatedText 同一套标注/右键能力，
    // 但缺译回退（带前缀，textContent 与 verbatim 文本不对齐）不参与逐字标注。
    const anns = annotationsOf(block.block_id, pane);
    const annotatable = !fallback && block.type === "paragraph";
    const blockAnn = blockAnnotationOf(block.block_id, pane);
    const body = annotatable && anns.some(a => !a.detached)
      ? annotatedSegments(text, anns)
      : (fallback ? `[未译 · 显示原文] ${text}` : text);
    return <p className={"rdrr-blk rdrr-p" + (blockAnn ? " rdrr-blockmark" : "")}
      style={blockAnn ? ({ "--rdrr-blockmark-color": `var(--rdrr-c-${blockAnn.color})` } as React.CSSProperties) : undefined}
      data-block-id={block.block_id} {...(annotatable ? { "data-annotatable": "1" } : {})} {...hoverProps(block.block_id)}>{body}</p>;
  };

  const blockAnnotationOf = (blockId: string, pane: ReaderPane) =>
    (overlay?.annotations ?? []).find(a => a.block_id === blockId && a.pane === pane && a.anchor === "block" && !a.detached);

  const renderAnnotatedText = (block: Block, pane: ReaderPane, className: string, text?: string) => {
    const info = blockText(block, pane);
    const value = text ?? info.text;
    const anns = annotationsOf(block.block_id, pane);
    const hasMarks = anns.some(a => !a.detached);
    // block 锚批注（fold 短层/整段右键创建）：普通阅读渲染为段首色条（与内核 margin 互认）
    const blockAnn = blockAnnotationOf(block.block_id, pane);
    const blockMarkStyle = blockAnn ? ({ "--rdrr-blockmark-color": `var(--rdrr-c-${blockAnn.color})` } as React.CSSProperties) : undefined;
    // 缺译回退：徽章放在 p 之外（同级），p 的 textContent 保持纯正文，字符偏移锚定不受影响
    const missing = pane === "trans" && info.fallback;
    const body = (
      <p className={className + (blockAnn ? " rdrr-blockmark" : "")} style={blockMarkStyle}
        data-block-id={block.block_id} data-annotatable="1" {...hoverProps(block.block_id)}
        onClick={event => {
          if ((event.target as HTMLElement).closest("button, a, input, .rdrr-pexpand, .rdrr-loctl")) return;
          activateBlock(block.block_id, pane);
        }}>
        {hasMarks ? annotatedSegments(value, anns) : value}
      </p>
    );
    return missing ? (
      <div className="rdrr-missing">
        <span className="rdrr-mflag">未译 · 显示原文回退</span>
        {body}
      </div>
    ) : body;
  };

  const renderUnitByLevel = (block: Block, sourceUnit: ReaderFoldUnit | undefined, pane: ReaderPane) => {
    // 译文栏必须用 **译文层的 unit**：它的 brief/key 是中文（而不是原文 brief），
    // 且带本段真实的 brief_source / source（derived / translation-local / source-fallback）。
    // 之前一律用原文 unit，导致「分层译句」这类来源标注张冠李戴、译文栏显示原文短句。
    const unit = pane === "trans" ? (dstUnitIndex.get(block.block_id) ?? sourceUnit) : sourceUnit;
    const locallyOpen = openBlocks.has(block.block_id);
    if (level === 1 || locallyOpen || !unit) {
      const node = renderAnnotatedText(block, pane, "rdrr-blk rdrr-p");
      if (locallyOpen && level > 1) {
        return (
          <div className="rdrr-localopen" key={block.block_id + "-open"}>
            <span className="rdrr-lotag">展开自{LEVEL_NAMES[level]}层</span>
            <button className="rdrr-loctl" onClick={() => setOpenBlocks(prev => { const next = new Set(prev); next.delete(block.block_id); return next; })}>收起</button>
            {node}
          </div>
        );
      }
      return node;
    }
    // 短层来源按 **unit.brief_source** 判定（adapter 每 unit 都写：derived / translation-local /
    // source-fallback）。整份 provenance.strategy=derive 只说明这次调用是派生，不代表每段都真的
    // 被模型重写——本地译文摘句与未译回退不能冒充“模型译句”。
    const briefTag = (() => {
      if (pane === "trans") {
        if (unit.fallback || unit.brief_source === "source-fallback") {
          return <span className="rdrr-fbtag" title="该段没有译文，短层回退到原文">未译 · 回退原文</span>;
        }
        if (unit.brief_source === "derived") {
          return <span className="rdrr-derivedtag" title="由原文 AI 层派生的短句，不是从已有译文的逐字摘录">分层译句 · 非译文逐字摘录</span>;
        }
        if (unit.brief_source === "translation-local") {
          return <span className="rdrr-localtag" title="从已有译文逐字摘句（本次未调模型），不是模型译句">本地译文摘句</span>;
        }
        return null;
      }
      return null;
    })();
    // L3 分层概括：按 unit.source 诚实标注（model / local / suppressed），与 brief 来源分开说
    const summaryTag = level >= 3
      ? unit.source === "model"
        ? <span className="rdrr-modeltag" title="要点/主线由模型生成">模型概括</span>
        : unit.source === "suppressed"
          ? <span className="rdrr-localtag" title="该段被判为不需折叠，显示原文">未折叠</span>
          : <span className="rdrr-localtag" title="程序摘取（本次未调模型）">本地摘取</span>
      : null;

    if (level === 2) {
      const brief = (unit.brief_text || unit.brief?.join(" ") || unit.sentences.slice(0, 1).join(" "));
      return (
        <p className="rdrr-pbrief" data-block-id={block.block_id} title="点击展开本段原文；右键标记/笔记/追问"
          {...hoverProps(block.block_id)}
          onClick={event => {
            if ((event.target as HTMLElement).closest(".rdrr-pexpand")) return;
            activateBlock(block.block_id, pane);
            setOpenBlocks(prev => new Set(prev).add(block.block_id));
          }}>
          {brief}{briefTag}{summaryTag}
          <button className="rdrr-pexpand" onClick={event => { event.stopPropagation(); setOpenBlocks(prev => new Set(prev).add(block.block_id)); }}>展开原文</button>
        </p>
      );
    }
    // L3 要点
    return (
      <p className="rdrr-pbrief" data-block-id={block.block_id} title="点击展开本段原文；右键标记/笔记/追问"
        {...hoverProps(block.block_id)}
        onClick={event => {
          if ((event.target as HTMLElement).closest(".rdrr-pexpand")) return;
          activateBlock(block.block_id, pane);
          setOpenBlocks(prev => new Set(prev).add(block.block_id));
        }}>
        {unit.key || unit.takeaway || unit.brief_text || unit.sentences[0]}{briefTag}{summaryTag}
        <button className="rdrr-pexpand" onClick={event => { event.stopPropagation(); setOpenBlocks(prev => new Set(prev).add(block.block_id)); }}>展开原文</button>
      </p>
    );
  };

  const articleItems = useMemo(() => {
    // 文档顺序 → 渲染项：标题/单元/透传块。
    return doc.blocks.map(block => {
      if (block.type === "title") return { kind: "heading" as const, block, sectionId: "" };
      const info = unitIndex.get(block.block_id);
      if (info) return { kind: "unit" as const, block, unit: info.unit, section: info.section, sectionId: info.section.id };
      return { kind: "block" as const, block };
    });
  }, [doc.blocks, unitIndex]);

  /** 整节展开：按文档顺序渲染本节**全文**（unit 全文 + 图/表/公式透传），而不是短层摘要。
   *  之前 L4「展开本节」只渲染节内第一个 unit、L5 展开后仍显示逐段要点，都是旧实现造成的。 */
  const renderSectionExpanded = (sectionId: string, pane: ReaderPane): React.ReactNode[] => {
    const out: React.ReactNode[] = [];
    articleItems.forEach(item => {
      if (blockSectionByOrder.get(item.block.block_id) !== sectionId) return;
      if (item.kind === "heading") return;   // 节标题已在脊线/主题卡展示，不重复
      if (item.kind === "unit") {
        out.push(<div key={"u" + item.block.block_id}>{renderAnnotatedText(item.block, pane, "rdrr-blk rdrr-p")}</div>);
        return;
      }
      if (item.block.type === "footnote") return;   // 与 L3+ 一致：脚注不进正文流
      out.push(
        <div key={"b" + item.block.block_id} {...hoverProps(item.block.block_id)}
          onClick={event => {
            if ((event.target as HTMLElement).closest("button, a, mark.hl, input")) return;
            activateBlock(item.block.block_id, pane);
          }}>
          {renderPassthrough(item.block, pane)}
        </div>,
      );
    });
    return out;
  };

  /** 展开整节的容器：节标题标注 + 全文 + 收起按钮（展开内容在主题层排在卡片后，需要标题归属）。 */
  const renderSectionOpen = (sectionId: string, pane: ReaderPane) => {
    const title = (layersSrc?.sections ?? []).find(section => section.id === sectionId)?.title
      ?? sectionIndexById.get(sectionId)?.title ?? "";
    return (
      <div key={"secopen" + sectionId + pane} className="rdrr-secopen">
        {title && <div className="rdrr-sectag">§ {title}</div>}
        {renderSectionExpanded(sectionId, pane)}
        <button className="rdrr-loctl" aria-label="收起本节"
          onClick={() => setOpenSections(prev => { const next = new Set(prev); next.delete(sectionId); return next; })}>
          ▲ 收起本节
        </button>
      </div>
    );
  };

  const renderArticle = (pane: ReaderPane) => {
    const items = articleItems;
    const out: React.ReactNode[] = [];
    if (level >= 5) {
      // 主题卡：分节 + 前言
      const cards: React.ReactNode[] = [];
      const seen = new Set<string>();
      items.forEach(item => {
        if (item.kind === "unit") {
          const sec = item.section;
          if (seen.has(sec.id)) return;
          seen.add(sec.id);
          const sectionLayers = (pane === "source" ? layersSrc : layersDst)?.sections.find(s => s.id === sec.id);
          const secSource = sectionLayers?.source ?? "local";
          const anchorId = sectionAnchor(sec.id) || sec.block_id || "";
          cards.push(
            <div key={"card" + sec.id} className="rdrr-tcard" data-block-id={anchorId} data-section-id={sec.id}
              {...hoverProps(anchorId)}
              onClick={event => {
                if ((event.target as HTMLElement).closest(".rdrr-topen")) return;
                if (anchorId) activateBlock(anchorId, pane);
              }}>
              <div className="rdrr-tcard-topic">{sectionLayers?.topic || sectionLayers?.takeaway || sec.title}</div>
              <div className="rdrr-tcard-title">{sec.title}</div>
              <div className="rdrr-tcard-meta">
                <span className="rdrr-mini-ai" title={secSource === "model" ? "由模型生成" : secSource === "mixed" ? "混合来源：部分模型、部分本地摘句" : "本地摘句（未调模型）"}>
                  {secSource === "model" ? "AI" : secSource === "mixed" ? "AI+本地" : "本地"}
                </span>
                <button className="rdrr-topen" onClick={event => { event.stopPropagation(); setOpenSections(prev => new Set(prev).add(sec.id)); }}>展开本节 →</button>
              </div>
            </div>,
          );
        }
      });
      out.push(<div key="topicmap" className="rdrr-topicmap">{cards}</div>);
      // 已展开节：按原文顺序渲染**整节全文**（此前逐段走短层，展开后仍是要点）
      const seenSec = new Set<string>();
      items.forEach(item => {
        if (item.kind !== "unit") return;
        if (!openSections.has(item.sectionId) || seenSec.has(item.sectionId)) return;
        seenSec.add(item.sectionId);
        out.push(renderSectionOpen(item.sectionId, pane));
      });
      return out;
    }
    if (level === 4) {
      // 主线脊线：每节一行 takeaaway，展开的节渲染全文
      const rendered = new Set<string>();
      items.forEach(item => {
        if (item.kind === "heading") {
          out.push(<h2 key={item.block.block_id} className="rdrr-blk rdrr-h2" data-block-id={item.block.block_id}
            {...hoverProps(item.block.block_id)}
            onClick={event => { activateBlock(item.block.block_id, pane); }}>{blockText(item.block, pane).text}</h2>);
          return;
        }
        if (item.kind === "unit") {
          if (rendered.has(item.sectionId)) return;
          rendered.add(item.sectionId);
          const secLayers = (pane === "source" ? layersSrc : layersDst)?.sections.find(s => s.id === item.sectionId);
          const open = openSections.has(item.sectionId);
          const anchorId = sectionAnchor(item.sectionId) || item.block.block_id;
          out.push(
            <div key={"spine" + item.sectionId} className={"rdrr-spine-item" + (currentBlock && sectionOfBlock(currentBlock) === item.sectionId ? " cur" : "")}
              data-block-id={anchorId} data-section-id={item.sectionId}
              {...hoverProps(anchorId)}
              onClick={event => {
                if ((event.target as HTMLElement).closest(".rdrr-openbtn")) return;
                activateBlock(anchorId, pane);
              }}>
              <span className="rdrr-sw">{secLayers?.takeaway || item.unit.takeaway || item.unit.sentences[0]}</span>
              <button className="rdrr-openbtn" onClick={event => {
                event.stopPropagation();
                if (!open) setOpenSections(prev => new Set(prev).add(item.sectionId));
                else setOpenSections(prev => { const next = new Set(prev); next.delete(item.sectionId); return next; });
              }}>{open ? "▲ 收起本节" : "展开本节 →"}</button>
            </div>,
          );
          // 展开的节：按原文顺序渲染整节全文（此前只渲染节内第一个 unit）
          if (open) out.push(renderSectionOpen(item.sectionId, pane));
          return;
        }
        // 透传块在主线层隐藏（与原型一致）
      });
      return out;
    }
    // L1/L2/L3：连续正文流
    items.forEach(item => {
      if (item.kind === "heading") {
        const { text, fallback } = blockText(item.block, pane);
        const Tag = (["h1", "h2", "h3", "h4"][clamp(item.block.level || 1, 1, 4)] ?? "h4") as "h1" | "h2" | "h3" | "h4";
        out.push(
          <Tag key={item.block.block_id} className={"rdrr-blk rdrr-h" + clamp(item.block.level || 1, 1, 4)} data-block-id={item.block.block_id}
            {...hoverProps(item.block.block_id)}
            onClick={event => {
              activateBlock(item.block.block_id, pane);
            }}>
            {fallback && pane === "trans" ? `[未译] ${text}` : text}
          </Tag>,
        );
        return;
      }
      if (item.kind === "unit") {
        if (level >= 3) {
          const fnCount = 0; // 脚注块独立渲染；正文层 L3 起不重复显示脚注
          void fnCount;
        }
        out.push(<div key={"u" + item.block.block_id}>{renderUnitByLevel(item.block, item.unit, pane)}</div>);
        return;
      }
      if (item.block.type === "footnote" && level >= 3) return;
      out.push(
        <div key={"b" + item.block.block_id} {...hoverProps(item.block.block_id)}
          onClick={event => {
            if ((event.target as HTMLElement).closest("button, a, mark.hl, input")) return;
            activateBlock(item.block.block_id, pane);
          }}>
          {renderPassthrough(item.block, pane)}
        </div>,
      );
    });
    return out;
  };

  const sectionOfBlock = (blockId: string) =>
    unitIndex.get(blockId)?.section.id ?? blockToSectionId.get(blockId) ?? "";

  /* ---------- 层级浮条（拖动/滚轮/键盘） ---------- */
  const barRef = useRef<HTMLDivElement>(null);
  const thumbRef = useRef<HTMLSpanElement>(null);
  const barGeom = useRef<{ left: number; width: number }[]>([]);
  const barButtons = () => [...(barRef.current?.querySelectorAll("button") ?? [])] as HTMLButtonElement[];
  const measureBar = useCallback(() => {
    const br = barRef.current?.getBoundingClientRect();
    if (!br) return;
    barGeom.current = barButtons().map(button => {
      const r = button.getBoundingClientRect();
      return { left: r.left - br.left, width: r.width };
    });
  }, []);
  const placeThumb = useCallback(() => {
    if (!barGeom.current.length) measureBar();
    const g = barGeom.current[clamp(level, 1, 5) - 1];
    if (!g || !thumbRef.current) return;
    thumbRef.current.style.transform = `translateX(${g.left}px)`;
    thumbRef.current.style.width = `${g.width}px`;
    barButtons().forEach((button, index) => button.classList.toggle("on", index + 1 === level));
  }, [level, measureBar]);
  useEffect(() => { placeThumb(); }, [level, placeThumb, paneKeys]);
  const posAt = (clientX: number) => {
    const br = barRef.current?.getBoundingClientRect();
    if (!br || !barGeom.current.length) return level;
    const x = clientX - br.left;
    const centers = barGeom.current.map(g => g.left + g.width / 2);
    if (x <= centers[0]) return 1;
    if (x >= centers[centers.length - 1]) return 5;
    for (let i = 0; i < centers.length - 1; i++) {
      if (x >= centers[i] && x <= centers[i + 1]) return 1 + i + (x - centers[i]) / (centers[i + 1] - centers[i]);
    }
    return level;
  };
  const barDrag = useRef<{ x0: number; moved: boolean } | null>(null);
  const onBarPointerDown = (event: React.PointerEvent) => {
    if (event.button !== 0) return;
    (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
    barRef.current?.classList.add("dragging");
    measureBar();
    barDrag.current = { x0: event.clientX, moved: false };
  };
  const onBarPointerMove = (event: React.PointerEvent) => {
    if (!barDrag.current) return;
    if (!barDrag.current.moved && Math.abs(event.clientX - barDrag.current.x0) > 3) barDrag.current.moved = true;
    if (barDrag.current.moved) setLevel(Math.round(posAt(event.clientX)));
  };
  const onBarPointerUp = (event: React.PointerEvent) => {
    if (!barDrag.current) return;
    const moved = barDrag.current.moved;
    barDrag.current = null;
    barRef.current?.classList.remove("dragging");
    if (!moved) setLevel(Math.round(posAt(event.clientX)));
  };
  const wheelLock = useRef(0);
  const wheelLevel = (delta: number) => {
    const now = Date.now();
    if (now - wheelLock.current < 160) return;
    wheelLock.current = now;
    setLevel(level + (delta > 0 ? 1 : -1));
  };

  // 键盘：数字键 1–5 切层、F 开关问答、Esc 关闭浮层。
  // 用 window 级监听——原来挂在阅读器根节点上，焦点在 body 时（刚打开、点过栏外）完全不触发。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing = Boolean(target?.closest("input, textarea, select, [contenteditable='true']"));
      if (event.key === "Escape") {
        setSelectionPop(null); setHlPop(null);
        return;
      }
      if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
      if (foldStageRef.current === "on" && "12345".includes(event.key)) { event.preventDefault(); setLevel(Number(event.key)); return; }
      if (event.key === "f" || event.key === "F") { event.preventDefault(); setChatOpen(value => !value); }
    };
    const onDown = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("#selPop, #hlPop, .rdrr-lvldetail, .rdrr-lvlnote")) return;
      setSelectionPop(null); setHlPop(null);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("mousedown", onDown); };
  }, []);

  /* ---------- 渲染 ---------- */
  if (loadError) {
    return <div className="rdrr-root"><div className="rdrr-loaderror">阅读层加载失败：{loadError}<button onClick={() => void loadAll()}>重试</button></div></div>;
  }
  if (!overlay) {
    return <div className="rdrr-root"><div className="rdrr-loading">正在加载阅读层…</div></div>;
  }

  // 当前章（仅用于目录/锚点推断；折叠范围已不再暴露给用户）
  const firstSectionId = layersSrc?.sections?.[0]?.id ?? layersDst?.sections?.[0]?.id ?? sectionIndex[0]?.id ?? "";
  /** 目标语短层若由「原文 AI 层派生」，必须标明它不是已有译文的逐字摘录。 */
  const appearance = prefs.appearance;
  const foldStageLabel = ({ off: "普通阅读（折叠默认关闭）", processing: "整篇折叠处理中…", ready: "折叠就绪 · 点「启动」进入", on: "折叠阅读" } as Record<string, string>)[foldStage];
  const foldErrorLine = foldError || (foldJob?.errors.length ? foldJob.errors.join("；") : "");
  const stale = layersSrc?.stale || (hasTrans && layersDst?.stale === true);

  return (
    <div id="rdrr-root" className={"rdrr-root" + (prefs.font === "sans" ? " sans" : "")} data-appearance={appearance}
      style={{ "--rdrr-fs": `${prefs.fontSize}px`, "--rdrr-lh": prefs.lineHeight } as React.CSSProperties}

    >
      {/* 顶栏：目录 / 书名 / 三选框 / Aa / 笔记 / 问答 */}
      <header className="rdrr-topbar">
        <button className="rdrr-tbtn" onClick={() => setTocOpen(value => !value)}>☰ 目录</button>
        <div className="rdrr-book">
          <div className="rdrr-book-title">{state?.title || doc.title}</div>
          <div className="rdrr-book-sub">对照阅读 · {foldStageLabel}</div>
        </div>
        <div className="rdrr-spacer" />
        <div className="rdrr-toggles" role="group" aria-label="显示栏开关">
          {(["origin", "source", "trans"] as const).map(key => (
            <label key={key} className="rdrr-ptoggle">
              <input type="checkbox" checked={prefs.panes[key]} aria-label={key}
                onChange={event => {
                  const on = event.target.checked;
                  if (!on && paneKeys.length <= 1) { event.target.checked = true; toast("至少保留一栏"); return; }
                  patchPrefs({ panes: { ...prefs.panes, [key]: on } });
                }} />
              <span>{{ origin: "原件", source: "原文", trans: "译文" }[key]}</span>
            </label>
          ))}
        </div>
        {isNarrow && (
          <div className="rdrr-narrowtabs" role="tablist" aria-label="阅读栏切换">
            {(["origin", "source", "trans"] as const).filter(k => prefs.panes[k]).map(k => (
              <button key={k} className={effectiveNarrowPane === k ? "on" : ""} onClick={() => setNarrowPane(k)}>
                {{ origin: "原件", source: "原文", trans: "译文" }[k]}
              </button>
            ))}
          </div>
        )}
        <button className="rdrr-tbtn" onClick={() => setAaOpen(value => !value)}>Aa</button>
        <div className="rdrr-aifoldwrap">
          <button className="rdrr-tbtn rdrr-aifold" id="btn-aifold" data-stage={foldStage}
            title={{ off: "处理整篇并生成五层折叠（点击即明确授权，可能调用模型）", processing: "整篇折叠中…可暂停/继续", ready: "启动折叠阅读", on: "退出折叠，回到普通阅读" }[foldStage]}
            disabled={foldStage === "on" && !foldData}
            onClick={() => {
              if (foldStage === "off") startAIFold();
              else if (foldStage === "ready") launchFold();
              else if (foldStage === "on" && foldData) exitFold();   // 产物未载入前不响应退出（防双击误退）
            }}>
            {foldStage === "processing" && (
              <svg className="rdrr-ring" viewBox="0 0 36 36" aria-hidden="true">
                <circle className="rdrr-ring-bg" cx="18" cy="18" r="15.5" />
                <circle className="rdrr-ring-fg" cx="18" cy="18" r="15.5"
                  strokeDasharray={`${Math.round(Math.max(0, Math.min(1, Number(foldJob?.progress?.ratio ?? 0))) * 97.4)} 97.4`} />
              </svg>
            )}
            {{ off: "AIFold", processing: "AIFold", ready: "启动", on: foldData ? "退出折叠" : "载入中…" }[foldStage]}
          </button>
          {foldStage === "processing" && (
            <button className="rdrr-tbtn" id="btn-foldpause" data-paused={foldJob?.status === "paused" ? "1" : "0"}
              disabled={foldJob?.status === "pausing"}
              title={foldJob?.status === "paused" ? "继续整篇折叠" : "暂停整篇折叠（模型请求边界生效）"}
              onClick={foldPauseResume}>
              {foldJob?.status === "paused" ? "继续" : foldJob?.status === "pausing" ? "正在暂停…" : "暂停"}
            </button>
          )}
        </div>
        <button className="rdrr-tbtn" id="btn-ai" onClick={() => { setChatOpen(value => !value); }}>AI 问答</button>
      </header>
      {foldErrorLine && foldStage !== "on" && (
        <div className="rdrr-fixturetag rdrr-layerfail" role="alert"
          style={{ position: "absolute", top: 54, left: "50%", transform: "translateX(-50%)", zIndex: 65, maxWidth: "72%" }}>
          {foldErrorLine}
        </div>
      )}

      {/* 目录抽屉 */}
      {tocOpen && (
        <div className="rdrr-toc" role="dialog" aria-label="目录">
          <div className="rdrr-toc-head">目录<button onClick={() => setTocOpen(false)}>✕</button></div>
          <div className="rdrr-toc-body">
            {(layersSrc?.sections?.length
              ? (layersSrc.sections ?? []).map(section => ({ id: section.id, title: section.title, block_id: section.block_id, level: 1 }))
              : sectionIndex.map(item => ({ id: item.id, title: item.title || item.num || item.id, block_id: item.block_id, level: item.level }))
            ).map(section => (
              <button key={section.id} className={"rdrr-toc-row lv" + clamp(section.level || 1, 1, 4)}
                data-section-id={section.id}
                onClick={() => {
                  setTocOpen(false);
                  // The TOC is outside both text panes: position both of them.
                  if (section.block_id) activateBlock(section.block_id, "origin");
                }}>{section.title || section.id}</button>
            ))}
            {!layersSrc?.sections?.length && !sectionIndex.length && (
              <div className="rdrr-toc-empty">这本书没有可识别的标题结构，无法拆分章节；可按全文折叠。</div>
            )}
          </div>
        </div>
      )}

      {/* Aa 面板 */}
      {aaOpen && (
        <div className="rdrr-aa" role="dialog" aria-label="排版与外观">
          <div className="rdrr-aa-seg" role="group" aria-label="外观">
            {(["light", "paper", "dark"] as const).map(v => (
              <button key={v} className={prefs.appearance === v ? "on" : ""} onClick={() => patchPrefs({ appearance: v })}>
                {{ light: "浅色", paper: "纸色", dark: "深色" }[v]}
              </button>
            ))}
          </div>
          <div className="rdrr-aa-row">
            <span>字号</span>
            <button onClick={() => patchPrefs({ fontSize: clamp(prefs.fontSize - 1, 13, 20) })}>A−</button>
            <span className="rdrr-aa-val">{prefs.fontSize}px</span>
            <button onClick={() => patchPrefs({ fontSize: clamp(prefs.fontSize + 1, 13, 20) })}>A+</button>
          </div>
          <div className="rdrr-aa-seg" role="group" aria-label="行距">
            {(["1.7", "1.9", "2.1"] as const).map(v => (
              <button key={v} className={prefs.lineHeight === v ? "on" : ""} onClick={() => patchPrefs({ lineHeight: v })}>
                {{ "1.7": "紧凑", "1.9": "标准", "2.1": "宽松" }[v]}
              </button>
            ))}
          </div>
          <div className="rdrr-aa-seg" role="group" aria-label="字体">
            <button className={prefs.font === "serif" ? "on" : ""} onClick={() => patchPrefs({ font: "serif" })}>衬线</button>
            <button className={prefs.font === "sans" ? "on" : ""} onClick={() => patchPrefs({ font: "sans" })}>无衬线</button>
          </div>
          <label className="rdrr-aa-check"><input type="checkbox" checked={prefs.follow} onChange={e => patchPrefs({ follow: e.target.checked })} />跟随滚动</label>
          <label className="rdrr-aa-check"><input type="checkbox" checked={prefs.sync} onChange={e => patchPrefs({ sync: e.target.checked })} />同步定位</label>
          <button className="rdrr-aa-wide" onClick={() => setChatOpen(value => !value)}>{chatOpen ? "收起问答" : "专注问答"}</button>
        </div>
      )}

      {/* 三栏 */}
      <div id="rdrr-main" className="rdrr-main">
        {paneVisible("origin") && (
          <>
            <section className="rdrr-pane rdrr-origin" id="rdrr-pane-origin">
              <div className="rdrr-pane-head"><span className="rdrr-tag rdrr-tag-pdf">原件</span>
                <span className="rdrr-tag rdrr-tag-demo">{isPdf ? "PDF 原件" : "非 PDF · 无页图"}</span></div>
              <div className="rdrr-pane-body" ref={bodyRefs.origin}>
                {showBboxOnly ? (
                  <div className="rdrr-pages">
                    <div className="rdrr-origin-note" role="status">
                      页图不可用（{pdf.error || "未加载"}）：仅显示版面框，位置来自 document.json 的 bbox，定位与联动仍可用。
                    </div>
                    {Array.from({ length: pdf.pageCount || doc.page_count || 0 }, (_, index) => {
                      const pageMeta = doc.pages?.[index];
                      const height = estimatedPageHeight(pageMeta, originWidth * 0.86);
                      return (
                        <div key={index} className="rdrr-page" style={{ height }}>
                          <div className="pdf-page" data-page-index={index} style={{ width: originWidth * 0.86, height }}>
                            <div className="pdf-overlay">
                              {doc.blocks.filter(block => block.page_index === index && hasBBox(block)).map(block => {
                                const rect = bboxRect(block.bbox, doc.bbox_space);
                                const current = (selected || currentBlock) === block.block_id;
                                return (
                                  <button key={block.block_id} type="button" data-block-id={block.block_id}
                                    className={`bbox bbox-${block.type}${current ? " selected" : ""}`}
                                    style={{ left: `${rect.left}%`, top: `${rect.top}%`, width: `${rect.width}%`, height: `${rect.height}%` }}
                                    title={`${block.block_id} · ${block.type}`}
                                    aria-label={block.source_text || block.block_id}
                                    onClick={() => { activateBlock(block.block_id, "origin"); }}>
                                    <span className="bbox-label" aria-hidden="true">{block.type === "title" ? `H${block.level}` : " "}</span>
                                  </button>
                                );
                              })}
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : isPdf && pdf.document ? (() => {
                  const pdfDoc = pdf.document;
                  return (
                  <div className="rdrr-pages">
                    {Array.from({ length: pdf.pageCount }, (_, index) => (
                      <div key={index} className="rdrr-page" style={{ height: estimatedPageHeight(doc.pages?.[index], originWidth * 0.86) }}>
                        <PdfPageCanvas
                          pdf={pdfDoc}
                          pageIndex={index}
                          renderWidth={originWidth > 100 ? Math.round(originWidth * 0.86) : 0}
                          active={Math.abs(index - visiblePage) <= 1 && originWidth > 100}
                          pageRatio={pageRatio(index)}
                          blocks={doc.blocks.filter(b => b.page_index === index)}
                          selected={selected || currentBlock}
                          showBoxes
                          space={doc.bbox_space}
                          flowLinks={doc.flow_links ?? {}}
                          onSelect={id => { activateBlock(id, "origin"); }}
                        />
                      </div>
                    ))}
                  </div>
                  );
                })() : (
                  <div className="rdrr-origin-fallback">
                    {pdf.error ? `原件加载失败：${pdf.error}` :
                      !isPdf ? "该文档不是 PDF：原件页图不可用，请在「原文/译文」栏阅读解析结果（图片/表格已按 block 原位渲染）。" :
                        "正在加载 PDF…"}
                  </div>
                )}
              </div>
            </section>
            <div className="rdrr-divider" onPointerDown={onDividerDown("origin")} />
          </>
        )}
        <section className={"rdrr-pane" + (paneVisible("source") ? "" : " hidden")} id="rdrr-pane-source">
          <div className="rdrr-pane-head"><span className="rdrr-tag rdrr-tag-md">原文</span></div>
          <div className="rdrr-pane-body" ref={bodyRefs.source} onScroll={onTextScroll("source")}>
            {foldStage === "on" && !foldData
              ? <div className="rdrr-engineerror" role="status">正在载入折叠产物…</div>
              : foldStage === "on" && foldData
              ? (srcEngine.error
                ? <div className="rdrr-engineerror" role="alert">内核加载失败：{srcEngine.error}<button onClick={srcEngine.retry}>重试</button></div>
                : <div ref={srcEngine.containerRef} className="rdrr-tfhost" />)
              : <article className="rdrr-article">{renderArticle("source")}</article>}
          </div>
        </section>
        {paneVisible("source") && paneVisible("trans") && <div className="rdrr-divider" onPointerDown={onDividerDown("source")} />}
        <section className={"rdrr-pane rdrr-trans" + (paneVisible("trans") ? "" : " hidden")} id="rdrr-pane-trans">
          <div className="rdrr-pane-head"><span className="rdrr-tag rdrr-tag-zh">译文</span>
            {!hasTrans && <span className="rdrr-tag rdrr-tag-demo">未生成译文</span>}</div>
          <div className="rdrr-pane-body" ref={bodyRefs.trans} onScroll={onTextScroll("trans")}>
            {foldStage === "on" && !foldData
              ? <div className="rdrr-engineerror" role="status">正在载入折叠产物…</div>
              : foldStage === "on" && foldData
              ? (hasTrans
                ? (dstEngine.error
                  ? <div className="rdrr-engineerror" role="alert">内核加载失败：{dstEngine.error}<button onClick={dstEngine.retry}>重试</button></div>
                  : <div ref={dstEngine.containerRef} className="rdrr-tfhost" />)
                : <div className="rdrr-empty-pane">该项目还没有译文，折叠对照不可用；可先退出折叠阅读原文。</div>)
              : (hasTrans || layersDst
                ? <article className="rdrr-article">{renderArticle("trans")}</article>
                : <div className="rdrr-empty-pane">该项目还没有译文。请先在「对照翻译」中生成译文；原文仍可在左侧完整阅读。</div>)}
          </div>
        </section>
      </div>

      {/* fold 模式：纯五档 bar（上游 footer.zoom 交互语义；上下无任何信息/操作栏）；普通阅读无 bar */}
      {foldStage === "on" && (
        <div className="rdrr-lvlwrap">
          {foldErrorLine && <div className="rdrr-fixturetag rdrr-layerfail" role="alert">{foldErrorLine}</div>}
          <div id="lvlbar" className="rdrr-lvlbar" role="slider" aria-label="折叠层级" aria-valuemin={1} aria-valuemax={5} aria-valuenow={level}
            aria-valuetext={LEVEL_NAMES[level]} tabIndex={0} ref={barRef}
            onPointerDown={onBarPointerDown} onPointerMove={onBarPointerMove} onPointerUp={onBarPointerUp}
            onKeyDown={event => {
              if (event.key === "ArrowLeft") { event.preventDefault(); setLevel(level - 1); }
              if (event.key === "ArrowRight") { event.preventDefault(); setLevel(level + 1); }
            }}
            onWheel={event => { event.preventDefault(); wheelLevel(event.deltaY); }}>
            <span className="rdrr-zthumb" ref={thumbRef} aria-hidden="true" />
            {[1, 2, 3, 4, 5].map(value => (
              <button key={value} data-l={value} className={level === value ? "on" : ""} aria-pressed={level === value}
                title={String(value)} onClick={() => setLevel(value)}>
                {{ 1: "全文", 2: "简述", 3: "要点", 4: "主线", 5: "主题" }[value]}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* 统一选段操作条：划词与右键整段同一入口（墨色胶囊，参考 TextFold selbar）；已有标记的编辑态（hlPop）是它的另一状态，两者不同时出现 */}
      {selectionPop && (
        <div id="selPop" className="rdrr-selpop themed" style={{ left: selectionPop.x, top: selectionPop.y }}
          onMouseDown={event => event.preventDefault()}
          onMouseUp={event => event.stopPropagation()}>
          <span className="rdrr-sp-label">高光</span>
          {COLORS.map(color => (
            <span key={color} className={"rdrr-swatch sw-" + color + (lastColor === color ? " cur" : "")} role="button" aria-label={`高亮（${color}）`}
              onClick={() => addAnnotation(color)} />
          ))}
          <span className="rdrr-sep" />
          <button className="rdrr-act" onClick={() => noteFromSelection()}>笔记</button>
          <button className="rdrr-act" onClick={() => askFromSelection()}>追问</button>
          {selectionPop.mode === "block" && <span className="rdrr-sp-hint" title="该块没有逐字范围：标记锚定到整段/整节">整段</span>}
        </div>
      )}

      {/* 高亮操作浮窗 */}
      {hlPop && activeAnnotation && (
        <div id="hlPop" className="rdrr-hlpop" role="dialog" aria-label="高亮操作"
          style={{ left: hlPop.x, top: hlPop.y }}>
          <div className="rdrr-hl-head">
            <span className="rdrr-hl-t">笔记</span>
            <span className="rdrr-hl-ref">· {sectionLabel(activeAnnotation.block_id).slice(0, 18)}</span>
            <button className="rdrr-hl-x" aria-label="关闭" onClick={() => setHlPop(null)}>✕</button>
          </div>
          <div className="rdrr-hl-swatches">
            {COLORS.map(color => (
              <span key={color} className={"rdrr-swatch sw-" + color + (activeAnnotation.color === color ? " cur" : "")}
                role="button" aria-label={`替换为颜色 ${color}`} tabIndex={0}
                onClick={() => patchAnnotation(activeAnnotation.id, { color })}
                onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); patchAnnotation(activeAnnotation.id, { color }); } }} />
            ))}
          </div>
          <textarea value={hlDraft} aria-label="笔记内容" placeholder="写点什么…"
            onChange={event => setHlDraft(event.target.value)} />
          <div className="rdrr-hl-acts">
            <button className="rdrr-danger" onClick={removeHighlight}>移除高亮</button>
            <button onClick={() => {
              setQuote(activeAnnotation.block_id, activeAnnotation.pane, activeAnnotation.quote.exact);
              setHlPop(null); setChatOpen(true);
            }}>AI 追问</button>
            <span className="rdrr-spacer" />
            <button className="rdrr-primary" onClick={() => {
              patchAnnotation(activeAnnotation.id, { note: hlDraft });
              setHlPop(null);
              toast("笔记已保存到阅读层");
            }}>保存</button>
          </div>
        </div>
      )}

      {/* 统一问答侧栏（圆角浮动） */}
      <aside id="ai" className={"rdrr-ai" + (chatOpen ? " show" : "")} aria-label="AI 问答">
        <div className="rdrr-ai-head">
          <span className="rdrr-ai-title">AI 问答</span>
          <span className="rdrr-ai-sub" title={chatRoleTitle}>
            {chatRoleLabel}
          </span>
          <button className="rdrr-ai-close" id="aiClose" onClick={() => setChatOpen(false)} aria-label="关闭">✕</button>
        </div>
        <div className="rdrr-ai-body" id="aiBody">
          {!chatMessages.length && <div className="rdrr-ai-empty">向全文提问，或划词/点高亮「追问」引用某一段。<br />回答一律给出原文依据（[#块ID] 可点击回跳）。</div>}
          {chatMessages.map(message => message.role === "user" ? (
            <div key={message.id} className="rdrr-msg user" aria-label="你的提问">
              <div className="rdrr-msg-q">{message.question}</div>
              {message.quote && (
                <details className="rdrr-qref">
                  <summary>{message.quote.pane === "trans" ? "译句：" : ""}{message.quote.snippet.slice(0, 60)}{(message.quote.snippet.length > 60 ? "…" : "")} · §{message.quote.sec}</summary>
                  {message.quote.pane === "trans" && <div className="rdrr-qline"><span className="rdrr-qtag">选中译文</span>{message.quote.snippet}</div>}
                  <div className="rdrr-qline"><span className="rdrr-qtag">原文</span>{message.quote.srcText}</div>
                  <button className="rdrr-cite" onClick={() => { activateBlock(message.quote!.block_id, "source"); }}>{message.quote.pane === "trans" ? "回原文 ↗" : "回原文 ↗"}</button>
                </details>
              )}
            </div>
          ) : (
            <div key={message.id} className="rdrr-msg ai" aria-label="AI 回答">
              {message.state === "sending" && <div className="rdrr-ai-wait">正在基于原文组织回答…</div>}
              {(message.state === "error" || message.state === "stopped") &&
  <div className={message.state === "error" ? "rdrr-ai-error" : "rdrr-ai-wait"}>
    {message.state === "error" ? message.error : "已停止"}
    <button onClick={() => void retryChat(message.id)}>重试</button>
  </div>}
              {message.state === "stopped" && <div className="rdrr-ai-wait">已停止</div>}
              {message.answer && <div className="rdrr-msg-a">{message.answer}</div>}
              {Boolean(message.citations?.length) && (
                <div className="rdrr-cites">
                  {message.citations!.map(c => (
                    <button key={c.block_id} className="rdrr-cite" onClick={() => activateBlock(c.block_id, "source")}>
                      § {c.section || c.block_id} · 原文
                    </button>
                  ))}
                </div>
              )}
              {message.model && <div className="rdrr-msg-model">{message.model}</div>}
            </div>
          ))}
        </div>
        {activeQuote && (
          <div className="rdrr-quote-card" id="aiQuote">
            <div className="rdrr-quote-head">
              <span className="rdrr-quote-tag">当前引用</span>
              <span className="rdrr-quote-sec" id="qSec">§ {activeQuote.sec}</span>
              <button className="rdrr-quote-x" id="qClose" aria-label="关闭引用" onClick={() => { setActiveQuote(null); toast("已关闭引用，后续提问面向全文"); }}>✕</button>
            </div>
            <div className="rdrr-quote-text" id="qText" title="点击回到原文位置" onClick={() => activateBlock(activeQuote.block_id, activeQuote.pane)}>
              {activeQuote.snippet}
            </div>
            <div className="rdrr-quote-rel" id="qRel">{activeQuote.pane === "trans" ? "引用自译文 · 关联原文；证据取原文段落" : "引用自原文"}</div>
          </div>
        )}
        <div className="rdrr-ai-inputrow">
          <ModelPicker
            label="对话模型"
            providers={providers}
            value={chatBinding}
            align="right"
            compact
            onChange={binding => { setChatBinding(binding); toast(`下一条消息将使用 ${binding.model}`); }}
          />
          <textarea
            id="aiInput"
            value={chatDraft}
            placeholder={activeQuote ? "就这段内容提问…" : "向全文提问…"}
            aria-label="问题"
            onChange={event => setChatDraft(event.target.value)}
            onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void sendQuestion(); } }}
          />
          {chatBusy
            ? <button className="rdrr-ai-send" onClick={() => void stopChat()}>停止</button>
            : <button className="rdrr-ai-send rdrr-primary" id="aiSend" onClick={() => void sendQuestion()}>发送</button>}
        </div>
      </aside>

      {notice && <div className="rdrr-toast">{notice}</div>}
    </div>
  );

  /* ---------- 局部工具（放在 return 之后仅作类型收口） ---------- */
  function pageRatio(index: number): number {
    const page = doc.pages?.[index];
    return page && page.width > 0 ? page.height / page.width : 1.414;
  }
}
