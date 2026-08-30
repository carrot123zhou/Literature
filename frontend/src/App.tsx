import { useCallback, useEffect, useMemo, useState } from "react";
import { open } from "@tauri-apps/plugin-dialog";
import { api, initializeApi } from "./api";
import PdfPage from "./PdfPage";
import TocTree from "./TocTree";
import ImportWorkspace from "./ImportWorkspace";
import type { Block, DocumentGraph, Job, Project, ProviderConfig, StructurePatch, StructureSuggestion, TocNode, TranslationEntry, TranslationLayer, UsageRecord } from "./types";

type Mode = "review" | "read" | "export";
type ResultTab = "blocks" | "markdown" | "json";
const defaultProvider: ProviderConfig = { provider: "deepseek", model: "deepseek-chat", base_url: "https://api.deepseek.com", temperature: .2, max_output_tokens: 8192 };
function savedProvider(): ProviderConfig { try { return {...defaultProvider,...JSON.parse(window.localStorage.getItem("literature.provider")||"{}")} } catch { return defaultProvider } }
function savedInstructions(): string { try { return window.localStorage.getItem("literature.translation.instructions")||"" } catch { return "" } }

function flatten(nodes: TocNode[]): TocNode[] { return nodes.flatMap(node => [node, ...flatten(node.children)]); }
function tocDraft(nodes: TocNode[]) { return flatten(nodes).map(node => `${"#".repeat(node.level)} ${node.title}`).join("\n"); }
function parseToc(value: string) { return value.split(/\n/).map(line => line.match(/^(#{1,6})\s+(.+)$/)).filter(Boolean).map(match => ({ level: match![1].length, title: match![2].trim() })); }
function blockMarkdown(block: Block) { if(block.type==="title")return `${"#".repeat(Math.max(1,block.level))} ${block.source_text}`;if(block.type==="list")return block.items.map(item=>`- ${item}`).join("\n");if(block.type==="table")return `[table · ${block.cells.length} cells]`;if(block.type==="image")return `${block.resource_refs.map(ref=>`![](${ref})`).join("\n")}\n${block.caption.join("\n")}`;return block.source_text||block.footnote.join("\n"); }

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [document, setDocument] = useState<DocumentGraph | null>(null);
  const [translation, setTranslation] = useState<TranslationLayer | null>(null);
  const [mode, setMode] = useState<Mode>("review");
  const [selected, setSelected] = useState("");
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState(1);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [tocText, setTocText] = useState("");
  const [suggestions, setSuggestions] = useState<StructureSuggestion[]>([]);
  const [patches, setPatches] = useState<StructurePatch[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [usage, setUsage] = useState({ input_tokens: 0, output_tokens: 0, estimated_cost: 0 });
  const [provider, setProvider] = useState(savedProvider);
  const [instructions,setInstructions] = useState(savedInstructions);
  const [settings, setSettings] = useState(false);
  const [message, setMessage] = useState("");
  const [instantResult, setInstantResult] = useState<{source:string;text:string;blockId:string}|null>(null);
  const [resultTab,setResultTab] = useState<ResultTab>("blocks");
  const [glossary,setGlossary] = useState<Record<string,string>>({});
  const [glossaryOpen,setGlossaryOpen] = useState(false);
  const [usageRecords,setUsageRecords] = useState<UsageRecord[]>([]);
  const [auditPayload,setAuditPayload] = useState<Record<string,unknown>|null>(null);
  const [tocCandidates,setTocCandidates] = useState<{page_index:number;page_number:number;confidence:number;reasons:string[]}[]>([]);

  const refreshProjects = useCallback(async () => setProjects(await api<Project[]>("/projects")), []);
  const refreshProject = useCallback(async (id: string) => {
    const graph = await api<DocumentGraph>(`/projects/${id}/document`);
    setTocText(tocDraft(graph.toc));
    const layer = await api<TranslationLayer>(`/projects/${id}/translations/zh-CN`);
    setTranslation(layer); setGlossary(layer.glossary_snapshot);
    setPatches(await api<StructurePatch[]>(`/projects/${id}/structure/patches`));
    setUsageRecords(await api<UsageRecord[]>(`/projects/${id}/usage/records`));
    const state = await api<any>(`/projects/${id}`); setDocument({...graph,budget_usd:state.budget_usd||0}); setJobs(state.jobs); setUsage(state.usage);
  }, []);
  useEffect(() => { initializeApi().then(refreshProjects).catch(error => setMessage(String(error))); }, [refreshProjects]);
  useEffect(() => { window.localStorage.setItem("literature.provider",JSON.stringify(provider)); }, [provider]);
  useEffect(() => { window.localStorage.setItem("literature.translation.instructions",instructions); }, [instructions]);
  useEffect(() => { if (projectId) refreshProject(projectId).catch(error => setMessage(String(error))); }, [projectId, refreshProject]);
  useEffect(() => {
    if (!projectId) return;
    const timer = window.setInterval(() => api<any>(`/projects/${projectId}`).then(async value => {
      setJobs(value.jobs); setUsage(value.usage);
      if (value.jobs.some((job: Job)=>job.status==="completed")) setTranslation(await api<TranslationLayer>(`/projects/${projectId}/translations/zh-CN`));
    }).catch(() => {}), 1500);
    return () => clearInterval(timer);
  }, [projectId]);

  const selectedBlock = document?.blocks.find(block => block.block_id === selected);
  const pageBlocks = document?.blocks.filter(block => block.page_index === page - 1) || [];
  const sourceProjection = useMemo(()=>document?.blocks.map(blockMarkdown).filter(Boolean).join("\n\n")||"",[document]);
  const selectedScope = useMemo(() => {
    if (!document || checked.size === 0) return [];
    const headings = flatten(document.toc);
    const ids = new Set<string>();
    for (const titleId of checked) {
      const node = headings.find(value => value.block_id === titleId); if (!node) continue;
      const start = document.blocks.findIndex(block => block.block_id === titleId);
      for (let index=start; index<document.blocks.length; index++) {
        const block = document.blocks[index];
        if (index>start && block.type === "title" && block.level <= node.level) break;
        ids.add(block.block_id);
      }
    }
    return [...ids];
  }, [document, checked]);

  const selectBlock = (id: string) => { setSelected(id); const block=document?.blocks.find(value=>value.block_id===id); if (block) setPage(block.page_index+1); };
  const toggle = (id: string) => setChecked(previous => { const next=new Set(previous); next.has(id)?next.delete(id):next.add(id); return next; });
  const importMineru = async () => {
    let paths: string[] = [];
    if ("__TAURI_INTERNALS__" in window) { const value=await open({directory:true,multiple:true}); paths = Array.isArray(value)?value:value?[value]:[]; }
    if (!paths.length) paths=(window.prompt("MinerU 结果目录；多个路径用逗号分隔")||"").split(",").map(v=>v.trim()).filter(Boolean);
    if (!paths.length) return;
    await api(`/projects/${projectId}/mineru/import`, {method:"POST",body:JSON.stringify({paths})}); await refreshProject(projectId);
  };
  const startMineru = async () => {
    const job=await api<any>(`/projects/${projectId}/ocr/jobs`,{method:"POST",body:JSON.stringify({language:"auto",model_version:"vlm",is_ocr:true,enable_table:true,enable_formula:true,page_limit:200})});
    await api(`/ocr/jobs/${job.job_id}/start`,{method:"POST"}); setMessage("MinerU 精准解析已在后台启动");
  };
  const analyze = async () => {
    const value=await api<any>(`/projects/${projectId}/structure/analyze`,{method:"POST",body:JSON.stringify({toc:parseToc(tocText),auto_apply:true})});
    setSuggestions(value.suggestions); await refreshProject(projectId); setMessage(`自动应用 ${value.applied.length} 项，另有 ${value.suggestions.length-value.applied.length} 项待核对`);
  };
  const findTocPages = async () => { const value=await api<typeof tocCandidates>(`/projects/${projectId}/structure/toc-page-candidates`);setTocCandidates(value);setMessage(value.length?`找到 ${value.length} 个目录页候选`:`没有找到高置信目录页，请手工定位`); };
  const analyzeAI = async () => {
    const value=await api<any>(`/projects/${projectId}/structure/analyze-ai`,{method:"POST",body:JSON.stringify({toc:parseToc(tocText),provider,auto_apply:true})});
    setSuggestions(value.suggestions);await refreshProject(projectId);setMessage(`AI 结构分析完成；自动应用 ${value.applied.length} 项`);
  };
  const previewStructureAudit = async () => {
    const value=await api<Record<string,unknown>>(`/projects/${projectId}/structure/audit-payload`,{method:"POST",body:JSON.stringify({toc:parseToc(tocText),auto_apply:false})});
    setAuditPayload(value);setResultTab("json");setMessage("右侧 JSON 正在显示结构模型的完整发送数据");
  };
  const confirmSuggestion = async (suggestion: StructureSuggestion) => {
    await api(`/projects/${projectId}/structure/patches`,{method:"POST",body:JSON.stringify({...suggestion,automatic:false})});
    setSuggestions(current=>current.filter(value=>value!==suggestion)); await refreshProject(projectId);
  };
  const confirmAllSuggestions = async () => {
    const pending=suggestions.filter(value=>!value.automatic); if(!pending.length)return;
    await api(`/projects/${projectId}/structure/patches/batch`,{method:"POST",body:JSON.stringify({patches:pending.map(value=>({...value,automatic:false}))})});
    setSuggestions([]); await refreshProject(projectId); setMessage(`已确认 ${pending.length} 项结构修正`);
  };
  const revertStructurePatch = async (patchId: string) => {
    await api(`/projects/${projectId}/structure/patches/${patchId}/revert`,{method:"POST"}); await refreshProject(projectId); setMessage("结构修改已撤销");
  };
  const demote = async (block: Block) => { await api(`/projects/${projectId}/structure/patches`,{method:"POST",body:JSON.stringify({block_id:block.block_id,operation:"set_type",after:"paragraph",reason:"人工确认"})}); await refreshProject(projectId); };
  const translate = async (refine=false, scope=selectedScope) => { await api(`/projects/${projectId}/translation/jobs`,{method:"POST",body:JSON.stringify({provider,block_ids:scope,locale:"zh-CN",glossary,refine,force:false,instructions})}); setMessage("翻译任务已进入本地队列"); };
  const pauseJob = async (job: Job) => { await api(`/projects/${projectId}/translation/jobs/${job.job_id}/pause`,{method:"POST"}); setMessage("已请求暂停；当前分块完成后停止"); };
  const resumeJob = async (job: Job) => { await api(`/projects/${projectId}/translation/jobs/${job.job_id}/resume`,{method:"POST"}); setMessage(job.status==="failed"?"正在重试失败分块":"翻译任务已恢复"); };
  const openGlossary = async () => { if(!Object.keys(glossary).length)setGlossary(await api<Record<string,string>>(`/projects/${projectId}/glossary/draft`));setGlossaryOpen(true); };
  const freezeGlossary = async (entries=glossary) => { setGlossary(entries);await api(`/projects/${projectId}/translations/zh-CN/glossary`,{method:"PUT",body:JSON.stringify({entries})});setGlossaryOpen(false);setMessage(`术语表已冻结：${Object.keys(entries).length} 项`); };
  const instant = async (event: React.MouseEvent<HTMLElement>) => {
    const selection=window.getSelection()?.toString().trim(); if(!selection)return;
    const blockId=(event.target as HTMLElement).closest<HTMLElement>("[data-block-id]")?.dataset.blockId||selected;
    const value=await api<any>(`/projects/${projectId}/translate/instant`,{method:"POST",body:JSON.stringify({provider,text:selection,locale:"zh-CN",block_id:blockId||null})});
    setInstantResult({source:selection,text:value.text,blockId});
  };

  if (!projectId) return <ImportWorkspace projects={projects} refreshProjects={refreshProjects} openProject={setProjectId}/>;
  if (!document) return <div className="empty full"><button onClick={()=>setProjectId("")}>← 项目库</button><h2>项目尚未生成文档图谱</h2><p>先导入 MinerU 结果，或启动 MinerU 精准解析。</p><button className="primary" onClick={startMineru}>启动 MinerU OCR</button><button onClick={importMineru}>导入已有 MinerU 结果</button>{message&&<div className="toast">{message}</div>}</div>;

  return <div className="app-shell">
    <header className="topbar"><button className="brand" onClick={()=>setProjectId("")}>Literature</button><div className="project-title">{document.title}<small>{document.source_language} → {document.target_language}</small></div><nav><button className={mode==="review"?"active":""} onClick={()=>setMode("review")}>结构校对</button><button className={mode==="read"?"active":""} onClick={()=>setMode("read")}>阅读与翻译</button><button className={mode==="export"?"active":""} onClick={()=>setMode("export")}>导出</button></nav><div className="usage">{(usage.input_tokens+usage.output_tokens).toLocaleString()} Token · ${usage.estimated_cost.toFixed(4)}{document.budget_usd?` / $${document.budget_usd.toFixed(2)}`:""}</div><button className="icon-button" onClick={()=>setSettings(true)}>设置</button></header>
    {mode==="review"&&<main className="workspace review-grid">
      <aside className="sidebar mineru-control-column"><div className="panel-title"><strong>结构与任务</strong><span>规范主数据</span></div><details><summary>确认原书目录基准</summary><button className="block-button" onClick={findTocPages}>寻找目录页候选</button>{tocCandidates.length>0&&<div className="toc-candidates">{tocCandidates.map(candidate=><button key={candidate.page_index} onClick={()=>setPage(candidate.page_number)}>第 {candidate.page_number} 页 <span>{Math.round(candidate.confidence*100)}%</span><small>{candidate.reasons.join(" · ")}</small></button>)}</div>}<textarea value={tocText} onChange={event=>setTocText(event.target.value)} rows={10}/><button className="primary block-button" onClick={analyze}>本地保守分析</button><button className="block-button" onClick={analyzeAI}>AI 辅助分析</button><button className="link-button block-button" onClick={previewStructureAudit}>预览发送给 AI 的数据</button><small className="audit-note">目录页候选在本地计算；AI 结构分析只使用目录文本、标题候选和相邻 block 类型，不发送正文。</small></details><TocTree nodes={document.toc} checked={checked} onToggle={toggle} onSelect={selectBlock}/><StructureQueue suggestions={suggestions} patches={patches} onSelect={selectBlock} onConfirm={confirmSuggestion} onConfirmAll={confirmAllSuggestions} onRevert={revertStructurePatch}/><div className="status-steps-panel"><strong>处理状态</strong><div className="status-step is-done"><i/>MinerU raw 已封存</div><div className="status-step is-done"><i/>规范图谱已生成</div><div className={`status-step ${suggestions.some(value=>!value.automatic)?"is-active":"is-done"}`}><i/>目录校准 {suggestions.some(value=>!value.automatic)?"待确认":"已同步"}</div><div className="status-latest">document.json + source.md + assets</div></div></aside>
      <section className="pdf-panel mineru-preview-pane"><div className="viewer-toolbar"><strong>文档预览</strong><div><button onClick={()=>setPage(Math.max(1,page-1))}>←</button><span>{page} / {pageCount}</span><button onClick={()=>setPage(Math.min(pageCount,page+1))}>→</button></div></div><div className="pdf-scroll"><PdfPage projectId={projectId} page={page} blocks={pageBlocks} selected={selected} onSelect={selectBlock} onPageCount={setPageCount}/></div></section>
      <aside className="inspector mineru-result-pane"><div className="result-tabs"><button className={resultTab==="blocks"?"active":""} onClick={()=>setResultTab("blocks")}>结构块</button><button className={resultTab==="markdown"?"active":""} onClick={()=>setResultTab("markdown")}>Markdown</button><button className={resultTab==="json"?"active":""} onClick={()=>setResultTab("json")}>JSON</button></div>{resultTab==="blocks"&&<><div className="panel-title"><strong>解析结果</strong><span>{pageBlocks.length} blocks</span></div>{pageBlocks.map(block=><article key={block.block_id} className={`block-card ${selected===block.block_id?"selected":""}`} onClick={()=>selectBlock(block.block_id)}><div><span className={`type type-${block.type}`}>{block.type}{block.level?` H${block.level}`:""}</span><small>{block.block_id}</small></div><p>{block.source_text||block.caption.join(" ")||`表格 ${block.cells.length} cells`}</p>{block.type==="title"&&<div className="card-actions"><button className="primary" onClick={event=>{event.stopPropagation();demote(block)}}>降为正文</button><button>保留标题</button></div>}</article>)}</>}{resultTab==="markdown"&&<pre className="result-code markdown-code">{sourceProjection}</pre>}{resultTab==="json"&&<pre className="result-code">{JSON.stringify(auditPayload||selectedBlock?.raw||document,null,2)}</pre>}</aside>
    </main>}
    {mode==="read"&&<main className="workspace reading-grid"><aside className="sidebar"><div className="panel-title"><strong>选择翻译范围</strong><span>{selectedScope.length||"全书"} blocks</span></div><TocTree nodes={document.toc} checked={checked} onToggle={toggle} onSelect={selectBlock}/><button className="glossary-button" onClick={openGlossary}>术语表 · {Object.keys(glossary).length} 项</button><JobPanel jobs={jobs} onPause={pauseJob} onResume={resumeJob}/><div className="current-block-action"><span>{selectedBlock?`当前：${selectedBlock.block_id}`:"点选正文块"}</span><button disabled={!selectedBlock} onClick={()=>selectedBlock&&translate(false,[selectedBlock.block_id])}>翻译当前 block</button></div><div className="sticky-actions"><button className="primary" onClick={()=>translate(false)}>开始初译</button><button onClick={()=>translate(true)}>按需润色</button></div></aside><section className="reader source-reader" onMouseUp={instant}><div className="reader-heading">原文 <button onClick={()=>setMode("review")}>打开原页</button></div>{document.blocks.map(block=><article data-block-id={block.block_id} id={`source-${block.block_id}`} key={block.block_id} className={`reader-block ${selected===block.block_id?"selected":""}`} onClick={()=>selectBlock(block.block_id)}>{block.type==="title"?<h3>{block.source_text}</h3>:block.type==="table"?<div className="table-placeholder">表格 · {block.cells.length} cells</div>:<p>{block.source_text||block.items.join(" · ")||block.caption.join(" ")}</p>}</article>)}</section><section className="reader translation-reader"><div className="reader-heading">译文 <span>编辑后自动锁定</span></div>{document.blocks.map(block=><TranslationBlock key={block.block_id} projectId={projectId} block={block} entry={translation?.entries[block.block_id]} onChanged={()=>refreshProject(projectId)}/>)}</section></main>}
    {mode==="export"&&<ExportPanel projectId={projectId} onMessage={setMessage}/>}
    <footer className="statusbar"><span>本地项目 · 已保存</span><span>{jobs[0]?`${jobs[0].kind} · ${jobs[0].status} · ${Math.round(jobs[0].progress*100)}%`:"无运行任务"}</span><span>{suggestions.filter(value=>!value.automatic).length} 项待确认</span></footer>
    {settings&&<Settings projectId={projectId} document={document} selected={selected} provider={provider} setProvider={setProvider} instructions={instructions} setInstructions={setInstructions} usageRecords={usageRecords} close={()=>setSettings(false)} onUpdated={setDocument} onMessage={setMessage}/>} {glossaryOpen&&<GlossaryEditor entries={glossary} setEntries={setGlossary} close={()=>setGlossaryOpen(false)} freeze={freezeGlossary}/>} {instantResult&&<InstantPanel projectId={projectId} value={instantResult} close={()=>setInstantResult(null)} onSaved={()=>refreshProject(projectId)}/>} {message&&<button className="toast" onClick={()=>setMessage("")}>{message}</button>}
  </div>;
}

function StructureQueue({suggestions,patches,onSelect,onConfirm,onConfirmAll,onRevert}:{suggestions:StructureSuggestion[];patches:StructurePatch[];onSelect:(id:string)=>void;onConfirm:(value:StructureSuggestion)=>void;onConfirmAll:()=>void;onRevert:(id:string)=>void}) {
  const pending=suggestions.filter(value=>!value.automatic);
  return <div className="structure-queue">{pending.length>0&&<details open><summary>待确认建议 · {pending.length}</summary><button className="primary block-button" onClick={onConfirmAll}>确认全部低风险修正</button>{pending.map(value=><article key={`${value.block_id}-${value.operation}`} onClick={()=>onSelect(value.block_id)}><strong>{value.operation==="set_type"?"降为正文":`改为 H${value.after}`}</strong><span>{Math.round(value.confidence*100)}%</span><p>{value.reason}</p><button onClick={event=>{event.stopPropagation();onConfirm(value)}}>确认此项</button></article>)}</details>}{patches.length>0&&<details><summary>修订历史 · {patches.filter(value=>!value.reverted_at).length}</summary>{patches.slice(0,12).map(value=><article className={value.reverted_at?"is-reverted":""} key={value.patch_id}><strong>{value.source==="ai"?"AI 自动":"人工"} · {value.operation}</strong><span>{value.reverted_at?"已撤销":Math.round(value.confidence*100)+"%"}</span><p>{value.block_id}</p>{!value.reverted_at&&<button onClick={()=>onRevert(value.patch_id)}>撤销</button>}</article>)}</details>}</div>;
}

function JobPanel({jobs,onPause,onResume}:{jobs:Job[];onPause:(job:Job)=>void;onResume:(job:Job)=>void}) {
  const relevant=jobs.filter(job=>job.kind==="translate"||job.kind==="refine").slice(0,4);
  if(!relevant.length)return null;
  return <details className="job-panel" open><summary>翻译任务 · {relevant.length}</summary>{relevant.map(job=><article key={job.job_id}><div><strong>{job.kind==="refine"?"润色":"初译"}</strong><span>{job.status} · {Math.round(job.progress*100)}%</span></div><progress max={1} value={job.progress}/>{job.error&&<small>{job.error}</small>}<div>{job.status==="running"&&<button onClick={()=>onPause(job)}>暂停</button>}{(job.status==="paused"||job.status==="failed"||job.status==="budget_paused")&&<button className="primary" onClick={()=>onResume(job)}>{job.status==="failed"?"重试失败分块":job.status==="budget_paused"?"提高预算后恢复":"恢复"}</button>}</div></article>)}</details>;
}

function GlossaryEditor({entries,setEntries,close,freeze}:{entries:Record<string,string>;setEntries:(value:Record<string,string>)=>void;close:()=>void;freeze:(value:Record<string,string>)=>void}) {
  const [text,setText]=useState(()=>Object.entries(entries).map(([source,target])=>`${source}\t${target}`).join("\n"));
  const save=()=>{const parsed:Record<string,string>={};for(const line of text.split(/\n/)){const [source,...target]=line.split(/\t|\s*=>\s*/);if(source?.trim())parsed[source.trim()]=target.join(" ").trim()}setEntries(parsed);freeze(parsed)};
  return <div className="modal-backdrop" onClick={close}><section className="settings glossary-editor" onClick={event=>event.stopPropagation()}><div className="panel-title"><h2>冻结术语表</h2><button onClick={close}>关闭</button></div><p>每行一个术语，原文和译文用 Tab 或 <code>=&gt;</code> 分隔。冻结后随翻译任务保存快照。</p><textarea value={text} onChange={event=>setText(event.target.value)} placeholder={'Paradigm\t范式'} /><button className="primary" onClick={save}>确认并冻结</button></section></div>;
}

function ExportPanel({projectId,onMessage}:{projectId:string;onMessage:(value:string)=>void}) {
  const [mode,setMode]=useState("translation"); const [formats,setFormats]=useState(new Set(["html","md+json","epub","docx"]));
  const run=async()=>{const value=await api<Record<string,string>>(`/projects/${projectId}/exports`,{method:"POST",body:JSON.stringify({mode,locale:"zh-CN",formats:[...formats]})});onMessage(`已导出：${Object.keys(value).join("、")}`)};
  const tbl=async()=>{const value=await api<any>(`/projects/${projectId}/tbl/prepare`,{method:"POST",body:JSON.stringify({format:"epub",mode:"source"})});onMessage(`TBL 接力文件：${value.input}`)};
  const startTbl=async()=>{const value=await api<any>("/tbl/start",{method:"POST"});onMessage(value.state==="running"?`TBL 已启动：${value.url}`:value.message||`TBL 状态：${value.state}`)};
  return <main className="export-center"><section><span className="eyebrow">EXPORT CENTER</span><h2>从同一份文档图谱生成成品</h2><p>导出不会修改原始 OCR、校对补丁或译文版本。</p></section><div className="export-options"><label>内容<select value={mode} onChange={e=>setMode(e.target.value)}><option value="translation">纯译文</option><option value="bilingual">逐段双语</option><option value="source">校对原文</option></select></label><div className="format-grid">{["epub","docx","html","md+json"].map(value=><label key={value}><input type="checkbox" checked={formats.has(value)} onChange={()=>setFormats(old=>{const next=new Set(old);next.has(value)?next.delete(value):next.add(value);return next})}/><strong>{value.toUpperCase()}</strong><small>{value==="epub"?"电子书目录与资源":value==="docx"?"可继续编辑":"结构化离线输出"}</small></label>)}</div><button className="primary" onClick={run}>生成所选格式</button><button onClick={tbl}>准备 EPUB 并交给外部 TBL</button><button onClick={startTbl}>启动 / 查看外部 TBL</button></div></main>;
}

function TranslationBlock({projectId,block,entry,onChanged}:{projectId:string;block:Block;entry?:TranslationEntry;onChanged:()=>void}) {
  const fallback=entry?.text||Object.values(entry?.cells||{}).join(" · ")||entry?.items?.join(" · ")||entry?.caption?.join(" ")||"";
  const [text,setText]=useState(fallback);
  useEffect(()=>setText(fallback),[fallback]);
  const save=async()=>{if(text===fallback)return;await api(`/projects/${projectId}/translations/${block.block_id}`,{method:"PUT",body:JSON.stringify({text,locale:"zh-CN"})});onChanged()};
  const unlock=async()=>{await api(`/projects/${projectId}/translations/${block.block_id}/unlock?locale=zh-CN`,{method:"POST"});onChanged()};
  return <article className={`reader-block translation-edit ${entry?.locked?"locked":""}`}><textarea aria-label={`${block.block_id} 译文`} value={text} placeholder="等待翻译；也可直接输入译文" onChange={event=>setText(event.target.value)} onBlur={save}/><div className="translation-meta"><span>{entry?.status||"pending"}{entry?.selection_translations?.length?` · ${entry.selection_translations.length} 条选段`:""}</span>{entry?.locked&&<button onClick={unlock}>解锁</button>}</div></article>;
}

function InstantPanel({projectId,value,close,onSaved}:{projectId:string;value:{source:string;text:string;blockId:string};close:()=>void;onSaved:()=>void}) {
  const save=async()=>{if(!value.blockId)return;await api(`/projects/${projectId}/translations/${value.blockId}/selections`,{method:"POST",body:JSON.stringify({source_text:value.source,translated_text:value.text,locale:"zh-CN"})});await onSaved();close()};
  return <div className="instant-card"><div className="panel-title"><strong>即时翻译</strong><button onClick={close}>关闭</button></div><small>{value.source}</small><p>{value.text}</p><button disabled={!value.blockId} className="primary" onClick={save}>保存到当前 block 的译文层</button></div>;
}

function Settings({projectId,document,selected,provider,setProvider,instructions,setInstructions,usageRecords,close,onUpdated,onMessage}:{projectId:string;document:DocumentGraph;selected:string;provider:ProviderConfig;setProvider:(value:ProviderConfig)=>void;instructions:string;setInstructions:(value:string)=>void;usageRecords:UsageRecord[];close:()=>void;onUpdated:(value:DocumentGraph)=>void;onMessage:(value:string)=>void}) {
  const [key,setKey]=useState(""); const [mineru,setMineru]=useState("");
  const [tblCommand,setTblCommand]=useState(""); const [tblUrl,setTblUrl]=useState("http://127.0.0.1:5001");
  useEffect(()=>{api<any>("/tbl/config").then(value=>{setTblCommand(value.command||"");setTblUrl(value.url||"http://127.0.0.1:5001")}).catch(()=>{})},[]);
  const presets:any={openai:{label:"OpenAI",model:"gpt-5-mini",url:"https://api.openai.com/v1"},deepseek:{label:"DeepSeek",model:"deepseek-chat",url:"https://api.deepseek.com"},glm:{label:"智谱 GLM",model:"glm-4.5",url:"https://open.bigmodel.cn/api/paas/v4"},qwen:{label:"Qwen / 阿里云百炼",model:"qwen-plus",url:"https://dashscope.aliyuncs.com/compatible-mode/v1"},custom:{label:"OpenAI-compatible",model:"",url:""}};
  const choose=(name:string)=>setProvider({...provider,provider:name,model:presets[name].model,base_url:presets[name].url});
  const updateLanguage=async(source:string,chapterLanguage?:string)=>{const payload:any={source_language:source};if(chapterLanguage!==undefined&&selected){const overrides={...document.chapter_language_overrides};if(chapterLanguage)overrides[selected]=chapterLanguage;else delete overrides[selected];payload.chapter_language_overrides=overrides}const graph=await api<DocumentGraph>(`/projects/${projectId}`,{method:"PATCH",body:JSON.stringify(payload)});onUpdated(graph);onMessage("语言设置已更新")};
  const updateBudget=async(value:number)=>{const graph=await api<DocumentGraph>(`/projects/${projectId}`,{method:"PATCH",body:JSON.stringify({budget_usd:Math.max(0,value||0)})});onUpdated(graph);onMessage(value>0?`项目预算设为 $${value.toFixed(2)}`:"项目预算限制已关闭")};
  const selectedTitle=document.blocks.find(block=>block.block_id===selected&&block.type==="title");
  return <div className="modal-backdrop" onClick={close}>
    <section className="settings" onClick={e=>e.stopPropagation()}>
      <div className="panel-title"><h2>设置</h2><button onClick={close}>关闭</button></div>
      <h3>语言</h3>
      <label>全局源语言<select value={document.source_language_mode||document.source_language} onChange={e=>updateLanguage(e.target.value)}><option value="auto">自动检测（当前 {document.source_language}）</option><option value="en">英语</option><option value="ja">日语</option><option value="fr">法语</option><option value="de">德语</option></select></label>
      {selectedTitle&&<label>当前章节覆盖<select value={document.chapter_language_overrides[selected]||""} onChange={e=>updateLanguage(document.source_language_mode||document.source_language,e.target.value)}><option value="">继承全局</option><option value="en">英语</option><option value="ja">日语</option><option value="fr">法语</option><option value="de">德语</option></select></label>}
      <hr/><h3>翻译模型</h3>
      <label>服务商<select value={provider.provider} onChange={e=>choose(e.target.value)}>{Object.keys(presets).map(name=><option value={name} key={name}>{presets[name].label}</option>)}</select></label>
      <label>Model ID<input value={provider.model} onChange={e=>setProvider({...provider,model:e.target.value})}/></label>
      <label>Base URL<input value={provider.base_url} onChange={e=>setProvider({...provider,base_url:e.target.value})}/></label>
      <label>输入单价（USD / 百万 token）<input type="number" min="0" step="0.01" value={provider.input_price_per_million??""} onChange={e=>setProvider({...provider,input_price_per_million:e.target.value?Number(e.target.value):undefined})}/></label>
      <label>输出单价（USD / 百万 token）<input type="number" min="0" step="0.01" value={provider.output_price_per_million??""} onChange={e=>setProvider({...provider,output_price_per_million:e.target.value?Number(e.target.value):undefined})}/></label>
      <label>项目预算（USD，0 为不限）<input type="number" min="0" step="0.1" defaultValue={document.budget_usd||0} onBlur={e=>updateBudget(Number(e.target.value))}/></label>
      <label>翻译附加要求<textarea rows={4} value={instructions} onChange={e=>setInstructions(e.target.value)} placeholder="例如：人名保留原文括注；采用学术中文。结构与 ID 约束始终优先。"/></label>
      <label>API Key<input type="password" value={key} onChange={e=>setKey(e.target.value)} placeholder="只保存到 macOS Keychain"/></label>
      <button className="primary" onClick={async()=>{await api(`/providers/${provider.provider}/key`,{method:"PUT",body:JSON.stringify({value:key})});setKey("");onMessage("模型密钥已保存到 Keychain")}}>保存模型密钥</button>
      <hr/><h3>用量明细</h3>
      <div className="usage-ledger">{usageRecords.length===0?<p>尚无模型调用。</p>:usageRecords.slice(0,20).map(record=><article key={record.usage_id}><strong>{record.provider} · {record.model}</strong><span>{(record.input_tokens+record.output_tokens).toLocaleString()} token</span><small>{record.scope} · {record.estimated_cost==null?"服务商未返回可估价信息":`$${record.estimated_cost.toFixed(5)}`}</small></article>)}</div>
      <hr/><h3>MinerU 精准解析</h3>
      <label>Token<input type="password" value={mineru} onChange={e=>setMineru(e.target.value)}/></label>
      <button onClick={async()=>{await api("/mineru/token",{method:"PUT",body:JSON.stringify({value:mineru})});setMineru("");onMessage("MinerU Token 已保存")}}>保存 MinerU Token</button>
      <hr/><h3>外部 TBL</h3>
      <label>启动命令<input value={tblCommand} onChange={e=>setTblCommand(e.target.value)} placeholder="/Applications/TBL.app/Contents/MacOS/TBL"/></label>
      <label>本地 URL<input value={tblUrl} onChange={e=>setTblUrl(e.target.value)}/></label>
      <button onClick={async()=>{await api("/tbl/config",{method:"PUT",body:JSON.stringify({command:tblCommand,url:tblUrl})});onMessage("TBL 外部工具设置已保存")}}>保存 TBL 设置</button>
    </section>
  </div>;
}
