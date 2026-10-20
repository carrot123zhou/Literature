import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "./review.css";
import { api } from "../api";
import SplitPane from "../SplitPane";
import type { DocumentGraph, FlowLinks, Job, ModelRoleBinding, ModelService, PageMeta, StructureBaseline, StructurePatch, StructureSuggestion, StructureTocEntry } from "../types";
import type { TaskModelPatch } from "../workbench/TaskModelPicker";
import OutlinePane, { MODE_PRESET, type StructureAnalysis, type StructureMode, type StructureOptions, type TocCandidate } from "./OutlinePane";
import type { ReasoningMode } from "../workbench/ThinkingLevelField";
import PdfPane, { type FocusRequest } from "./PdfPane";
import ResultPane, { type PatchProposal, type ResultTab } from "./ResultPane";
import SourcePreview from "./SourcePreview";
import { documentProjection, formatTocDraft, parseTocDraft } from "./projection";
import { usePdfDocument } from "./usePdfDocument";

type Props = {
  projectId: string;
  document: DocumentGraph;
  sourceType: string;
  sourceName: string;
  patches: StructurePatch[];
  suggestions: StructureSuggestion[];
  jobs: Job[];
  onSuggestions: (value: StructureSuggestion[]) => void;
  selected: string;
  onSelect: (blockId: string) => void;
  refresh: () => Promise<void>;
  setMessage: (value: string) => void;
  /** 结构校对角色绑定的服务商。 */
  modelService?: ModelService;
  /** 结构校对角色绑定的模型 ID。 */
  boundModel?: string;
  /** 全部服务商（含模型列表），供任务界面「服务商 → 模型」选择。 */
  providers: ModelService[];
  onConfigureModel: () => void;
};

function isTyping(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element) return false;
  return element.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName);
}

/** MinerU 文本结构类型中可以在结构校对里修改的集合。 */
const EDITABLE_TYPES = ["title", "paragraph", "list", "footnote"];

/** 建议是否仍适用当前块；按块而不是整份文档判断，避免采用兄弟建议后其它建议一起失效。 */
function suggestionFresh(document: DocumentGraph, value: StructureSuggestion): boolean {
  const block = document.blocks.find(item => item.block_id === value.block_id);
  if (!block) return false;
  if (value.operation === "set_level") return block.type === "title" && block.level === Number(value.before);
  const expected = value.before && typeof value.before === "object" ? value.before as { type?: string; level?: number; source_text?: string; items?: string[] } : { type: String(value.before) };
  if (block.type !== (expected.type ?? block.type)) return false;
  if (block.type === "title" && Number(expected.level ?? block.level) !== block.level) return false;
  if ("source_text" in expected && expected.source_text !== block.source_text) return false;
  if ("items" in expected && JSON.stringify(expected.items) !== JSON.stringify(block.items)) return false;
  return true;
}

/**
 * 对照阅读 + 结构调整工作区。
 *
 * 三栏共享同一个 active block 语义：PDF 覆盖层、结构块列表、目录树都通过
 * block_id 互相定位；结构调整只写可逆补丁，不改变阅读顺序。
 */
export default function ReviewWorkspace(props: Props) {
  const { projectId, document, sourceType, sourceName, patches, suggestions, onSuggestions, selected, onSelect, refresh, setMessage, modelService, boundModel, providers, onConfigureModel } = props;
  const bboxAvailable = document.bbox_space === "mineru-1000" && document.blocks.some(block => block.bbox.length === 4);
  const isPdf = sourceType.toLowerCase() === "pdf";
  // 后端只读派生：跨页续接双向链接，供原页虚线框/连线与结果栏提示使用。
  const flowLinks: FlowLinks = document.flow_links ?? {};

  const [tocText, setTocText] = useState(() => { try { return localStorage.getItem(`literature.review.baseline.${projectId}`) || ""; } catch { return ""; } });
  const [pageText, setPageText] = useState("");
  const [tocCandidates, setTocCandidates] = useState<TocCandidate[]>([]);
  const [auditPayload, setAuditPayload] = useState<unknown>(null);
  const [tab, setTab] = useState<ResultTab>("blocks");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [activeBlock, setActiveBlock] = useState("");
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const [follow, setFollow] = useState(true);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<StructureMode>("coarse");
  // 任务级服务商/模型/思考覆盖：默认 = 角色绑定；随结构作业请求发送。
  const [taskModel, setTaskModel] = useState<ModelRoleBinding>(() => ({ service_id: modelService?.service_id || "", model: boundModel || modelService?.models[0]?.model || "" }));
  // 仅在角色绑定本身变化时回填默认值；服务商列表普通刷新（存密钥等）不打断用户已选的任务级覆盖。
  const boundServiceId = modelService?.service_id || "";
  useEffect(() => {
    setTaskModel({ service_id: boundServiceId, model: boundModel || modelService?.models[0]?.model || "" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boundServiceId, boundModel]);
  const [reasoningMode, setReasoningMode] = useState<ReasoningMode>("inherit");
  const [reasoningBudget, setReasoningBudget] = useState(0);
  // 主流程只传 mode：初始选项直接取档位预设，只有高级选项里被显式改动的字段才随请求覆盖。
  const [options, setOptions] = useState<StructureOptions>(() => ({ ...MODE_PRESET.coarse, requestTimeoutSeconds: 600, proofreadConcurrency: 2 }));
  const [structureJob, setStructureJob] = useState<Job | null>(null);
  const [structureRunning, setStructureRunning] = useState(false);
  const [proposedToc, setProposedToc] = useState<StructureTocEntry[] | null>(null);
  const nonce = useRef(0);
  // 防止双击/同帧重入造成重复 POST 结构分析作业。
  const posting = useRef(false);
  // 手动结构修改的提交锁：React busy 是异步状态，同帧连续事件会绕过它。
  const submitting = useRef(false);

  useEffect(() => { try { localStorage.setItem(`literature.review.baseline.${projectId}`, tocText); } catch { /* optional preference storage */ } }, [projectId, tocText]);

  // 项目基准优先于本机偏好：换客户端或清空 WebView 偏好后仍可复用已确认目录。
  // 只回填目录文本供「高级 · 使用已确认的目录」取用；不改变主流程的档位预设（extract_toc=true）。
  useEffect(() => {
    let cancelled = false;
    api<StructureBaseline>(`/projects/${projectId}/structure/baseline`).then(value => {
      if (cancelled || !value.toc?.length) return;
      setTocText(formatTocDraft(value.toc));
    }).catch(() => { /* 旧后端或尚未保存基准时保留本机偏好 */ });
    return () => { cancelled = true; };
  }, [projectId]);

  const persistBaseline = useCallback((entries: StructureTocEntry[], source: string) => {
    void api(`/projects/${projectId}/structure/baseline`, { method: "PUT", body: JSON.stringify({ toc: entries, source }) })
      .catch(() => { /* 基准保存失败不应阻断当前校准 */ });
  }, [projectId]);
  const mounted = useRef(true);
  const trackedJob = useRef("");
  const documentRef = useRef(document);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { documentRef.current = document; }, [document]);

  /** 从服务端读取仍待处理的建议；已采用/已忽略的结论存在项目数据库，重启后不复活。 */
  const refreshSuggestions = useCallback(async (): Promise<StructureSuggestion[]> => {
    try {
      const value = await api<StructureSuggestion[]>(`/projects/${projectId}/structure/suggestions?decision=pending`);
      const fresh = value.filter(item => suggestionFresh(documentRef.current, item)).map(item => ({ ...item, automatic: Boolean(item.automatic) }));
      onSuggestions(fresh);
      return fresh;
    } catch { return []; }
  }, [projectId, onSuggestions]);

  /** 回填一次已完成作业的源目录基准；建议本身从服务端待处理列表读取。 */
  const applyJobResult = useCallback((job: Job) => {
    const result = (job.result ?? {}) as { proposed_toc?: StructureTocEntry[] };
    setProposedToc(result.proposed_toc?.length ? result.proposed_toc : null);
  }, []);

  // 进入工作区时恢复项目内仍待处理的建议。
  useEffect(() => { void refreshSuggestions(); }, [projectId]);
  const selectedPages = () => {
    if (!options.extractToc) return [];
    // 留空表示交给后端自动识别目录页；不强制用户先选页。
    if (!pageText.trim()) return [];
    const indexes = new Set<number>();
    for (const part of pageText.replaceAll("，", ",").split(",")) {
      const match = part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
      if (!match) throw new Error("目录页格式应为 3-5, 7。");
      const from = Number(match[1]), to = Number(match[2] || match[1]);
      if (from < 1 || to < from || to > (pdf.pageCount || document.page_count)) throw new Error("目录页超出源文件范围。");
      if (to - from >= 6) throw new Error("一次最多选择 6 页目录。");
      for (let page = from; page <= to; page++) indexes.add(page - 1);
    }
    if (indexes.size > 6) throw new Error("一次最多选择 6 页目录。");
    return [...indexes].sort((a, b) => a - b);
  };

  const pdf = usePdfDocument(projectId, isPdf);
  const markdown = useMemo(() => documentProjection(document), [document]);
  const pages = useMemo<PageMeta[]>(() => {
    const metadata = new Map((document.pages ?? []).map(page => [page.page_index, page]));
    return Array.from({ length: pdf.pageCount || document.page_count || 0 }, (_, index) => metadata.get(index) ?? {
      page_index: index, width: 0, height: 0, rotation: 0, source_file: "", source_page_index: index,
    });
  }, [document.pages, document.page_count, pdf.pageCount]);
  const selectedBlock = useMemo(() => document.blocks.find(block => block.block_id === selected), [document.blocks, selected]);
  const jsonValue = auditPayload ?? selectedBlock?.raw ?? document;

  const locate = useCallback((blockId: string) => {
    nonce.current += 1;
    onSelect(blockId);
    setFocus({ blockId, nonce: nonce.current });
    setActiveBlock(blockId);
  }, [onSelect]);

  const run = useCallback(async (action: () => Promise<void>) => {
    if (submitting.current || structureRunning) return;
    submitting.current = true;
    setBusy(true);
    try { await action(); } catch (error) { setMessage(String(error instanceof Error ? error.message : error)); } finally { submitting.current = false; setBusy(false); }
  }, [setMessage, structureRunning]);

  const applyPatch = useCallback((proposal: PatchProposal) => void run(async () => {
    await api(`/projects/${projectId}/structure/patches`, { method: "POST", body: JSON.stringify(proposal) });
    await refresh();
    locate(proposal.block_id);
    onSuggestions(suggestions.filter(value => value.block_id !== proposal.block_id));
    setMessage("结构已更新，可在修订历史中撤销。");
  }), [projectId, refresh, locate, run, setMessage, suggestions, onSuggestions]);

  const applyBatch = (operation: "set_type" | "set_title", after: string | number) => void run(async () => {
    const targets = [...picked].filter(id => document.blocks.some(block => block.block_id === id && EDITABLE_TYPES.includes(block.type)));
    if (!targets.length) return;
    await api(`/projects/${projectId}/structure/patches/batch`, {
      method: "POST",
      body: JSON.stringify({ patches: targets.map(block_id => ({ block_id, operation, after, reason: "批量结构调整", automatic: false })) }),
    });
    setPicked(new Set());
    onSuggestions(suggestions.filter(value => !targets.includes(value.block_id)));
    await refresh();
    setMessage(`已批量调整 ${targets.length} 个块`);
  });

  // 采用建议：带 suggestion_id 的走服务端决策（项目内原子应用并记录），本地分析结果退回补丁接口。
  const confirm = (suggestion: StructureSuggestion) => void run(async () => {
    if (suggestion.suggestion_id) {
      await api(`/projects/${projectId}/structure/suggestions/${suggestion.suggestion_id}/decision`, { method: "POST", body: JSON.stringify({ decision: "applied" }) });
      await refresh();
      await refreshSuggestions();
    } else {
      await api(`/projects/${projectId}/structure/patches`, { method: "POST", body: JSON.stringify({ ...suggestion, automatic: false, source: "ai" }) });
      onSuggestions(suggestions.filter(value => value !== suggestion));
      await refresh();
    }
  });

  const confirmAll = (values: StructureSuggestion[]) => void run(async () => {
    const pending = values.filter(value => !value.automatic);
    if (!pending.length) return;
    if (pending.every(value => value.suggestion_id)) {
      await api(`/projects/${projectId}/structure/suggestions/decide-batch`, { method: "POST", body: JSON.stringify({ suggestion_ids: pending.map(value => value.suggestion_id), decision: "applied" }) });
      await refresh();
      await refreshSuggestions();
    } else {
      await api(`/projects/${projectId}/structure/patches/batch`, { method: "POST", body: JSON.stringify({ patches: pending.map(value => ({ ...value, automatic: false })), source: "ai" }) });
      onSuggestions(suggestions.filter(value => !pending.includes(value)));
      await refresh();
    }
    setMessage(`已采用 ${pending.length} 项结构建议`);
  });

  const dismiss = (suggestion: StructureSuggestion) => {
    if (suggestion.suggestion_id) {
      void run(async () => {
        await api(`/projects/${projectId}/structure/suggestions/${suggestion.suggestion_id}/decision`, { method: "POST", body: JSON.stringify({ decision: "dismissed" }) });
        await refreshSuggestions();
      });
    } else {
      onSuggestions(suggestions.filter(value => value !== suggestion));
    }
  };

  const revert = (patchId: string) => void run(async () => {
    await api(`/projects/${projectId}/structure/patches/${patchId}/revert`, { method: "POST" });
    await refresh();
    setMessage("结构修订已撤销");
  });

  const revertAll = () => void run(async () => {
    const ids = patches.filter(value => !value.reverted_at).map(value => value.patch_id);
    if (!ids.length) return;
    await api(`/projects/${projectId}/structure/patches/revert-batch`, { method: "POST", body: JSON.stringify({ patch_ids: ids }) });
    await refresh();
    setMessage(`已撤销 ${ids.length} 项结构修订`);
  });

  // 稳定引用：OutlinePane 是 memo 组件，轮询引发的父级重渲染不应击穿它。
  const onTaskModelChange = useCallback((patch: TaskModelPatch) => {
    if (patch.serviceId !== undefined || patch.model !== undefined) setTaskModel(current => ({ service_id: patch.serviceId ?? current.service_id, model: patch.model ?? current.model }));
    if (patch.reasoningMode !== undefined) setReasoningMode(patch.reasoningMode);
    if (patch.reasoningBudget !== undefined) setReasoningBudget(patch.reasoningBudget);
  }, []);

  const analyze = () => void run(async () => {
    const draft = options.extractToc ? [] : parseTocDraft(tocText);
    const value = await api<{ suggestions: StructureSuggestion[]; applied: unknown[]; proposed_toc: StructureTocEntry[] }>(`/projects/${projectId}/structure/analyze`, { method: "POST", body: JSON.stringify({ toc: draft, toc_page_indexes: options.extractToc ? selectedPages() : [], auto_apply: options.autoApply }) });
    if (draft.length) persistBaseline(draft, "confirmed");
    if (value.proposed_toc?.length) setProposedToc(value.proposed_toc);
    onSuggestions(value.suggestions.filter(item => !item.automatic && suggestionFresh(document, item)));
    await refresh();
    setMessage(`自动应用 ${value.applied.length} 项，另有 ${value.suggestions.length - value.applied.length} 项待核对`);
  });

  // 显式确认当前审查范围：绑定文档结构版本；文档改动后确认自动失效。
  const confirmReview = () => void run(async () => {
    await api(`/projects/${projectId}/structure/confirm`, { method: "POST" });
    await refresh();
    setMessage("已确认当前校对范围为已审查；文档之后改动会自动失效");
  });

  // 异步结构分析：后台作业 + 轮询状态，界面展示进度与活动摘要。
  // 主流程只传 mode（服务端按档位派生 preset）；高级选项里被显式改动的字段才随请求覆盖。
  const runModel = () => {
    if (posting.current || structureRunning || busy) return;
    const resumable = structureJob && structureJob.status === "paused" ? structureJob.job_id : "";
    // 注意：只有「暂停」才走续跑契约（后端复用旧作业快照）。失败的任务必须新建作业：
    // 否则用户改完高级设置（如重新指定目录页）点「开始校准」，实际会静默沿用失败
    // 作业的旧参数重新报同样的错。新建时指向旧作业，已完成请求仍然可复用。
    const resuming = Boolean(resumable);
    const preset = MODE_PRESET[mode];
    const body: Record<string, unknown> = { service_id: taskModel.service_id, model: taskModel.model, mode, reasoning_mode: reasoningMode, reasoning_budget: reasoningMode === "inherit" ? 0 : reasoningBudget };
    if (!resumable) {
      // 只有新建任务才在客户端校验/拼装参数；续跑交给后端 resume 契约复用已存配置，避免与暂停时快照不一致。
      let tocPages: number[];
      try {
        if (!Number.isInteger(options.requestTimeoutSeconds) || options.requestTimeoutSeconds < 30 || options.requestTimeoutSeconds > 1800) throw new Error("模型响应等待时间应为 30 至 1800 秒。");
        tocPages = options.extractToc ? selectedPages() : [];
        if (!options.extractToc && !parseTocDraft(tocText).length) throw new Error("请填写已确认目录，每行按“# 标题”格式标记层级。");
      } catch (error) { setMessage((error as Error).message); return; }
      if (!options.extractToc) persistBaseline(parseTocDraft(tocText), "confirmed");
      if (!options.extractToc) { body.extract_toc = false; body.toc = parseTocDraft(tocText); }
      if (tocPages.length) body.toc_page_indexes = tocPages;
      if (options.requestTimeoutSeconds !== 600) body.request_timeout_seconds = options.requestTimeoutSeconds;
      if (options.useVision !== preset.useVision) body.use_vision = options.useVision;
      if (options.autoApply !== preset.autoApply) body.auto_apply = options.autoApply;
      if (options.reviewMode !== preset.reviewMode) body.proofread_mode = options.reviewMode;
      if (options.tocExtraction !== preset.tocExtraction) body.toc_extraction = options.tocExtraction;
      if (options.proofreadConcurrency !== 2) body.proofread_concurrency = options.proofreadConcurrency;
      if (options.planningMode !== preset.planningMode) body.planning_mode = options.planningMode;
      if (structureJob?.status === "failed" && structureJob.job_id) body.reuse_job_id = structureJob.job_id;
    }
    posting.current = true;
    setStructureRunning(true);
    if (!resuming) setProposedToc(null);
    setStructureJob(null);
    setMessage(resuming ? "继续结构校准：已完成的部分直接复用" : "结构校准任务已启动");
    const request = resumable
      // 续跑真正复用后端 resume 契约：服务端用暂停/失败作业的审计做请求级复用，只补剩余请求。
      ? api<Job>(`/projects/${projectId}/structure/jobs/${resumable}/resume`, { method: "POST" })
      : api<Job>(`/projects/${projectId}/structure/jobs`, { method: "POST", body: JSON.stringify(body) });
    request
      .then(job => {
        if (!mounted.current) return;
        trackedJob.current = job.job_id;
        setStructureJob(job);
        // 不返回轮询 Promise：否则 .finally 会等轮询结束才释放 posting，暂停按钮被永久拦截。
        void pollStructureJob(job.job_id);
      }).catch(error => { if (mounted.current) { setStructureRunning(false); setMessage(String(error instanceof Error ? error.message : error)); } })
      .finally(() => { posting.current = false; });
  };

  // 暂停：请求后端在下一个模型请求边界挂起；当前在途请求跑完即暂停。
  const pauseStructure = () => {
    if (!structureJob || !structureRunning || posting.current) return;
    posting.current = true;
    api<{ job_id: string; state: string }>(`/projects/${projectId}/structure/jobs/${structureJob.job_id}/pause`, { method: "POST" })
      .then(() => { if (mounted.current) setMessage("已请求暂停：当前请求完成后挂起"); })
      .catch(error => { if (mounted.current) setMessage(String(error instanceof Error ? error.message : error)); })
      .finally(() => { posting.current = false; });
  };

  const pollingJob = useRef("");
  const pollStructureJob = async (jobId: string) => {
    // 防重入：POST 尚未返回时 props.jobs 可能先带回同一作业，避免两个轮询循环并发。
    if (pollingJob.current === jobId) return;
    pollingJob.current = jobId;
    let failures = 0;
    try {
    while (mounted.current && trackedJob.current === jobId) {
      let job: Job;
      try {
        job = await api<Job>(`/projects/${projectId}/structure/jobs/${jobId}`);
        failures = 0;
      } catch (error) {
        if (!mounted.current) return;
        failures += 1;
        if (failures >= 10) {
          // 任务可能已被清理或项目已切换：停止轮询，让用户重新发起。
          trackedJob.current = "";
          setStructureRunning(false);
          setMessage(`无法读取任务状态，已停止跟进：${error instanceof Error ? error.message : String(error)}`);
          return;
        }
        setStructureJob(current => current ? { ...current, message: `暂时无法读取任务状态，正在重连：${error instanceof Error ? error.message : String(error)}` } : current);
        await new Promise(resolve => window.setTimeout(resolve, 3000));
        continue;
      }
      if (!mounted.current) return;
      setStructureJob(job);
      if (job.status === "paused") {
        setStructureRunning(false);
        setMessage("结构校准已暂停，可点「继续校准」补完剩余部分");
        return;
      }
      if (job.status === "completed" || job.status === "failed") {
        setStructureRunning(false);
        if (job.status === "failed") { setMessage(`结构校准失败：${job.error}`); return; }
        applyJobResult(job);
        await refresh();
        const pending = await refreshSuggestions();
        setMessage(`结构校准完成：自动应用 ${(job.result as { applied?: unknown[] } | undefined)?.applied?.length ?? 0} 项，可在修订历史撤销${pending.length ? `；另有 ${pending.length} 项待核对` : ""}`);
        return;
      }
      await new Promise(resolve => window.setTimeout(resolve, 900));
    }
    } finally { if (pollingJob.current === jobId) pollingJob.current = ""; }
  };

  // Re-entering the workspace reconnects to a running backend task rather than starting another.
  useEffect(() => {
    if (trackedJob.current) return;
    const running = props.jobs.find(value => value.kind === "structure_ai" && ["queued", "running", "pause_requested"].includes(value.status));
    if (running) {
      trackedJob.current = running.job_id;
      setStructureJob(running);
      setStructureRunning(true);
      void pollStructureJob(running.job_id);
      return;
    }
    // 已完成的任务：按当前块状态与已保存的采用/忽略决定恢复仍待处理的建议。
    const latest = props.jobs.find(value => value.kind === "structure_ai");
    if (!latest) return;
    trackedJob.current = latest.job_id;
    setStructureJob(latest);
    // 轻量轮询只给摘要（无 result/audit）；进入已完成作业时按 job_id 拉完整详情再应用。
    void api<Job>(`/projects/${projectId}/structure/jobs/${latest.job_id}`).then(full => {
      if (trackedJob.current !== full.job_id) return;
      setStructureJob(full);
      if (full.status === "completed") { applyJobResult(full); void refreshSuggestions(); }
    }).catch(() => { /* 详情读取失败时保留摘要，不阻塞工作区 */ });
  }, [props.jobs, projectId, applyJobResult, refreshSuggestions]);

  const previewActualAudit = () => {
    if (!structureJob?.result) return;
    setAuditPayload((structureJob.result as { audit?: unknown }).audit ?? structureJob.result);
    setTab("json");
    setMessage("已展开实际请求记录；图片记录页码、块 ID 和摘要，不重复保存图片数据。");
  };

  const adoptProposed = (entries: StructureTocEntry[]) => void run(async () => {
    if (!entries.length) return;
    await api(`/projects/${projectId}/structure/baseline`, { method: "PUT", body: JSON.stringify({ toc: entries, source: "confirmed" }) });
    setTocText(formatTocDraft(entries));
    setProposedToc(null);
    setMessage("已保存源目录基准到项目，下次校准可直接复用。");
  });

  const previewAudit = () => void run(async () => {
    const value = await api<unknown>(`/projects/${projectId}/structure/plan`, { method: "POST", body: JSON.stringify({ toc: parseTocDraft(tocText), extract_toc: options.extractToc, proofread: options.proofread, toc_page_indexes: selectedPages(), use_vision: options.useVision }) });
    setAuditPayload(value);
    setTab("json");
    setMessage("模型输入已展开，visual_evidence 包含将发送的目录页与标题裁片。");
  });

  const findTocPages = () => void run(async () => {
    const value = await api<TocCandidate[]>(`/projects/${projectId}/structure/toc-page-candidates`);
    setTocCandidates(value);
    setMessage(value.length ? `找到 ${value.length} 个目录页候选` : "没有找到高置信目录页，请手工定位");
  });

  // 零成本：直接从规范文档里解析印刷目录，回填为已确认基准，不调用模型。
  const readLocalToc = () => void run(async () => {
    const value = await api<{ pages: number[]; entries: StructureTocEntry[] }>(`/projects/${projectId}/structure/local-toc?pages=${selectedPages().join(",")}`);
    if (!value.entries.length) {
      setMessage("正文里没有找到可解析的印刷目录，可手工填写基准，或从源目录页提取。");
      return;
    }
    setProposedToc(value.entries);
    const pages = value.pages.map(page => page + 1).join("、");
    setMessage(`本地读取目录 ${value.entries.length} 条（第 ${pages} 页），未调用模型。请核对下方目录草稿的层级后确认。`);
  });

  const togglePick = useCallback((blockId: string, range: boolean) => {
    if (!document.blocks.some(block => block.block_id === blockId && EDITABLE_TYPES.includes(block.type))) return;
    setPicked(previous => {
      const next = new Set(previous);
      if (range && selected) {
        const from = document.blocks.findIndex(block => block.block_id === selected);
        const to = document.blocks.findIndex(block => block.block_id === blockId);
        if (from >= 0 && to >= 0) {
          for (let index = Math.min(from, to); index <= Math.max(from, to); index += 1) {
            if (EDITABLE_TYPES.includes(document.blocks[index].type)) next.add(document.blocks[index].block_id);
          }
          return next;
        }
      }
      if (next.has(blockId)) next.delete(blockId); else next.add(blockId);
      return next;
    });
  }, [document.blocks, selected]);

  // 阅读顺序导航与层级快捷键；在输入控件里不拦截按键。
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isTyping(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      const index = document.blocks.findIndex(block => block.block_id === selected);
      if (event.key === "ArrowDown" || event.key === "j") {
        const next = document.blocks[Math.min(document.blocks.length - 1, Math.max(0, index) + 1)];
        if (next) { event.preventDefault(); locate(next.block_id); }
      } else if (event.key === "ArrowUp" || event.key === "k") {
        const previous = document.blocks[Math.max(0, index - 1)];
        if (previous) { event.preventDefault(); locate(previous.block_id); }
      } else if (/^[1-6]$/.test(event.key)) {
        const block = document.blocks[index];
        if (!block || !EDITABLE_TYPES.includes(block.type) || busy || structureRunning) return;
        event.preventDefault();
        applyPatch({ block_id: block.block_id, operation: block.type === "title" ? "set_level" : "set_title", after: Number(event.key), reason: `键盘设为 H${event.key}` });
      } else if (event.key === "0" || event.key === "Backspace") {
        const block = document.blocks[index];
        if (!block || !EDITABLE_TYPES.includes(block.type) || block.type === "paragraph" || busy || structureRunning) return;
        event.preventDefault();
        applyPatch({ block_id: block.block_id, operation: "set_type", after: "paragraph", reason: "键盘降为正文" });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [document.blocks, selected, locate, applyPatch, busy, structureRunning]);

  const jumpToPage = useCallback((page: number) => {
    const block = document.blocks.find(item => item.page_index === page - 1);
    if (block) locate(block.block_id);
    else { nonce.current += 1; setFocus({ blockId: "", pageIndex: page - 1, nonce: nonce.current }); }
  }, [document.blocks, locate]);

  const analysis = useMemo<StructureAnalysis>(() => ({ job: structureJob, running: structureRunning, paused: !structureRunning && structureJob?.status === "paused", mode, options, proposedToc }), [structureJob, structureRunning, mode, options, proposedToc]);
  const onOptions = (patch: Partial<StructureOptions>) => setOptions(current => ({ ...current, ...patch }));
  // 切换档位即回到该档位的预设；高级选项可在切换后再次调整。
  const onMode = (next: StructureMode) => {
    setMode(next);
    setOptions(current => ({ ...current, ...MODE_PRESET[next] }));
  };

  return (
    <SplitPane
      className="workspace review-grid"
      storageKey="literature.split.review"
      initial={[0.2, 0.4, 0.4]}
      minPx={[200, 360, 280]}
      handleLabels={["调整结构与任务栏宽度", "调整文档预览与解析结果宽度"]}
    >
      <OutlinePane
        document={document}
        tocText={tocText}
        onTocText={setTocText}
        tocCandidates={tocCandidates}
        pageText={pageText}
        onPageText={setPageText}
        onFindTocPages={findTocPages}
        onReadLocalToc={readLocalToc}
        onJumpPage={jumpToPage}
        onAnalyze={analyze}
        onPreviewAudit={previewAudit}
        onPreviewActualAudit={previewActualAudit}
        suggestions={suggestions}
        patches={patches}
        selected={selected}
        activeBlock={activeBlock}
        onSelect={locate}
        onConfirm={confirm}
        onDismiss={dismiss}
        onConfirmAll={confirmAll}
        onRevert={revert}
        onRevertAll={revertAll}
        busy={busy}
        providers={providers}
        modelValue={taskModel}
        // 按任务实际选中的服务商判断密钥状态，而不是角色绑定的那家。
        configured={Boolean(providers.find(item => item.service_id === taskModel.service_id)?.configured)}
        onConfigureModel={onConfigureModel}
        analysis={analysis}
        onOptions={onOptions}
        onMode={onMode}
        reasoningMode={reasoningMode}
        reasoningBudget={reasoningBudget}
        onModelChange={onTaskModelChange}
        onRunModel={runModel}
        onPause={pauseStructure}
        onAdoptProposed={adoptProposed}
        onConfirmReview={confirmReview}
      />
      {isPdf
        ? <PdfPane
            pdf={pdf}
            pages={pages}
            blocks={document.blocks}
            space={document.bbox_space}
            selected={selected}
            flowLinks={flowLinks}
            onSelect={locate}
            onActiveBlock={setActiveBlock}
            focus={focus}
          />
        : <SourcePreview projectId={projectId} sourceType={sourceType} sourceName={sourceName} bboxAvailable={bboxAvailable} onJumpPage={jumpToPage}/>}
      <ResultPane
        blocks={document.blocks}
        busy={busy || structureRunning}
        space={document.bbox_space}
        selected={selected}
        activeBlock={activeBlock}
        flowLinks={flowLinks}
        checked={picked}
        tab={tab}
        onTab={setTab}
        onSelect={locate}
        onCheck={togglePick}
        onClearChecks={() => setPicked(new Set())}
        onPatch={applyPatch}
        onBatchPatch={applyBatch}
        markdown={markdown}
        jsonValue={jsonValue}
        follow={follow}
        onFollow={setFollow}
      />
    </SplitPane>
  );
}
