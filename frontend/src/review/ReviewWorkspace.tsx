import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import SplitPane from "../SplitPane";
import type { Block, DocumentGraph, PageMeta, ProviderConfig, StructurePatch, StructureSuggestion } from "../types";
import OutlinePane, { type TocCandidate } from "./OutlinePane";import PdfPane, { type FocusRequest } from "./PdfPane";
import ResultPane, { type PatchProposal, type ResultTab } from "./ResultPane";
import SourcePreview from "./SourcePreview";
import { documentProjection, parseTocDraft, tocDraft } from "./projection";
import { usePdfDocument } from "./usePdfDocument";

type Props = {
  projectId: string;
  document: DocumentGraph;
  sourceType: string;
  sourceName: string;
  patches: StructurePatch[];
  suggestions: StructureSuggestion[];
  onSuggestions: (value: StructureSuggestion[]) => void;
  scope: Set<string>;
  onToggleScope: (blockId: string) => void;
  provider: ProviderConfig;
  selected: string;
  onSelect: (blockId: string) => void;
  refresh: () => Promise<void>;
  setMessage: (value: string) => void;
  onOpenRead: () => void;
};

function isTyping(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element) return false;
  return element.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(element.tagName);
}

/**
 * 对照阅读 + 结构调整工作区。
 *
 * 三栏共享同一个 active block 语义：PDF 覆盖层、结构块列表、目录树都通过
 * block_id 互相定位；结构调整只写可逆补丁，不改变阅读顺序。
 */
export default function ReviewWorkspace(props: Props) {
  const { projectId, document, sourceType, sourceName, patches, suggestions, onSuggestions, scope, onToggleScope, provider, selected, onSelect, refresh, setMessage, onOpenRead } = props;
  const bboxAvailable = document.bbox_space === "mineru-1000" && document.blocks.some(block => block.bbox.length === 4);

  const [tocText, setTocText] = useState(() => tocDraft(document.toc));
  const [tocCandidates, setTocCandidates] = useState<TocCandidate[]>([]);
  const [auditPayload, setAuditPayload] = useState<unknown>(null);
  const [tab, setTab] = useState<ResultTab>("blocks");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [activeBlock, setActiveBlock] = useState("");
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const [follow, setFollow] = useState(true);
  const [busy, setBusy] = useState(false);
  const nonce = useRef(0);

  useEffect(() => { setTocText(tocDraft(document.toc)); }, [document.updated_at, document.toc]);

  const pdf = usePdfDocument(projectId, bboxAvailable);
  const markdown = useMemo(() => documentProjection(document), [document]);
  const pages = useMemo<PageMeta[]>(() => (
    document.pages?.length
      ? document.pages
      : Array.from({ length: document.page_count || 0 }, (_, index) => ({ page_index: index, width: 0, height: 0, rotation: 0, source_file: "", source_page_index: index }))
  ), [document.pages, document.page_count]);
  const selectedBlock = useMemo(() => document.blocks.find(block => block.block_id === selected), [document.blocks, selected]);
  const jsonValue = auditPayload ?? selectedBlock?.raw ?? document;

  const locate = useCallback((blockId: string) => {
    nonce.current += 1;
    onSelect(blockId);
    setFocus({ blockId, nonce: nonce.current });
    setActiveBlock(blockId);
  }, [onSelect]);

  const run = useCallback(async (action: () => Promise<void>) => {
    setBusy(true);
    try { await action(); } catch (error) { setMessage(String(error instanceof Error ? error.message : error)); } finally { setBusy(false); }
  }, [setMessage]);

  const applyPatch = useCallback((proposal: PatchProposal) => void run(async () => {
    await api(`/projects/${projectId}/structure/patches`, { method: "POST", body: JSON.stringify(proposal) });
    await refresh();
    locate(proposal.block_id);
    setMessage(`结构修订已记录：${proposal.block_id}`);
  }), [projectId, refresh, locate, run, setMessage]);

  const applyBatch = (operation: "set_type" | "set_title", after: string | number) => void run(async () => {
    const targets = [...picked];
    if (!targets.length) return;
    await api(`/projects/${projectId}/structure/patches/batch`, {
      method: "POST",
      body: JSON.stringify({ patches: targets.map(block_id => ({ block_id, operation, after, reason: "批量结构调整", automatic: false })) }),
    });
    setPicked(new Set());
    await refresh();
    setMessage(`已批量调整 ${targets.length} 个块`);
  });

  const confirm = (suggestion: StructureSuggestion) => void run(async () => {
    await api(`/projects/${projectId}/structure/patches`, { method: "POST", body: JSON.stringify({ ...suggestion, automatic: false }) });
    onSuggestions(suggestions.filter(value => value !== suggestion));
    await refresh();
  });

  const confirmAll = () => void run(async () => {
    const pending = suggestions.filter(value => !value.automatic);
    if (!pending.length) return;
    await api(`/projects/${projectId}/structure/patches/batch`, { method: "POST", body: JSON.stringify({ patches: pending.map(value => ({ ...value, automatic: false })) }) });
    onSuggestions([]);
    await refresh();
    setMessage(`已确认 ${pending.length} 项结构修正`);
  });

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

  const analyze = () => void run(async () => {
    const value = await api<{ suggestions: StructureSuggestion[]; applied: unknown[] }>(`/projects/${projectId}/structure/analyze`, { method: "POST", body: JSON.stringify({ toc: parseTocDraft(tocText), auto_apply: true }) });
    onSuggestions(value.suggestions);
    await refresh();
    setMessage(`自动应用 ${value.applied.length} 项，另有 ${value.suggestions.length - value.applied.length} 项待核对`);
  });

  const analyzeAI = () => void run(async () => {
    const value = await api<{ suggestions: StructureSuggestion[]; applied: unknown[] }>(`/projects/${projectId}/structure/analyze-ai`, { method: "POST", body: JSON.stringify({ toc: parseTocDraft(tocText), provider, auto_apply: true }) });
    onSuggestions(value.suggestions);
    await refresh();
    setMessage(`AI 结构分析完成；自动应用 ${value.applied.length} 项`);
  });

  const previewAudit = () => void run(async () => {
    const value = await api<unknown>(`/projects/${projectId}/structure/audit-payload`, { method: "POST", body: JSON.stringify({ toc: parseTocDraft(tocText), auto_apply: false }) });
    setAuditPayload(value);
    setTab("json");
    setMessage("JSON 视图正在显示发送给结构模型的完整数据（不含正文）");
  });

  const findTocPages = () => void run(async () => {
    const value = await api<TocCandidate[]>(`/projects/${projectId}/structure/toc-page-candidates`);
    setTocCandidates(value);
    setMessage(value.length ? `找到 ${value.length} 个目录页候选` : "没有找到高置信目录页，请手工定位");
  });

  const togglePick = useCallback((blockId: string, range: boolean) => {
    setPicked(previous => {
      const next = new Set(previous);
      if (range && selected) {
        const from = document.blocks.findIndex(block => block.block_id === selected);
        const to = document.blocks.findIndex(block => block.block_id === blockId);
        if (from >= 0 && to >= 0) {
          for (let index = Math.min(from, to); index <= Math.max(from, to); index += 1) next.add(document.blocks[index].block_id);
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
        if (!block) return;
        event.preventDefault();
        applyPatch({ block_id: block.block_id, operation: block.type === "title" ? "set_level" : "set_title", after: Number(event.key), reason: `键盘设为 H${event.key}` });
      } else if (event.key === "0" || event.key === "Backspace") {
        const block = document.blocks[index];
        if (!block || block.type !== "title") return;
        event.preventDefault();
        applyPatch({ block_id: block.block_id, operation: "set_type", after: "paragraph", reason: "键盘降为正文" });
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [document.blocks, selected, locate, applyPatch]);

  const jumpToPage = useCallback((page: number) => {
    const block = document.blocks.find(item => item.page_index === page - 1);
    if (block) locate(block.block_id);
  }, [document.blocks, locate]);

  return (
    <SplitPane
      className="workspace review-grid"
      storageKey="literature.split.review"
      initial={[0.22, 0.41, 0.37]}
      minPx={[260, 320, 300]}
      handleLabels={["调整结构与任务栏宽度", "调整文档预览与解析结果宽度"]}
    >
      <OutlinePane
        document={document}
        tocText={tocText}
        onTocText={setTocText}
        tocCandidates={tocCandidates}
        onFindTocPages={findTocPages}
        onJumpPage={jumpToPage}
        onAnalyze={analyze}
        onAnalyzeAI={analyzeAI}
        onPreviewAudit={previewAudit}
        suggestions={suggestions}
        patches={patches}
        selected={selected}
        activeBlock={activeBlock}
        checked={scope}
        onSelect={locate}
        onToggleCheck={onToggleScope}
        onConfirm={confirm}
        onConfirmAll={confirmAll}
        onRevert={revert}
        onRevertAll={revertAll}
        onPatch={applyPatch}
        busy={busy}
      />
      {bboxAvailable
        ? <PdfPane
            pdf={pdf}
            pages={pages}
            blocks={document.blocks}
            space={document.bbox_space}
            selected={selected}
            onSelect={locate}
            onActiveBlock={setActiveBlock}
            focus={focus}
            action={<button className="link-button" onClick={onOpenRead}>去翻译</button>}
          />
        : <SourcePreview projectId={projectId} sourceType={sourceType} sourceName={sourceName} bboxAvailable={bboxAvailable} onJumpPage={jumpToPage}/>}
      <ResultPane
        blocks={document.blocks}
        space={document.bbox_space}
        selected={selected}
        activeBlock={activeBlock}
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
