import { useCallback, useEffect, useMemo, useState } from "react";
import { isDesktop, revealPath, saveCopy } from "../desktop";
import type { ExportFormatId, ExportOptions, ExportRecord, ExportTocNode, Project } from "../types";
import { createExport, deleteExport, downloadExportFile, fetchExportOptions, renameExport } from "./exportApi";
import "./export.css";

/* ----------------------------- 工具 ----------------------------- */

const MODE_DESC: Record<string, string> = {
  source: "只导出原文，不改写版面元数据。",
  translation: "导出译文；尚未翻译的段落自动回退显示原文。",
  bilingual: "逐段交替对照：原文段后紧跟译文段，译文以弱化样式区分；缺译段落只显示原文。",
};

const LOCALE_LABEL: Record<string, string> = {
  "zh-CN": "简体中文", "zh-TW": "繁體中文", en: "English", ja: "日本語", fr: "Français", de: "Deutsch",
};

function localeLabel(value: string) {
  return LOCALE_LABEL[value] ? `${LOCALE_LABEL[value]}（${value}）` : value;
}

const MODE_LABEL: Record<string, string> = { source: "原文", translation: "译文", bilingual: "对照版" };

function formatSize(bytes = 0) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
}

function flatten(nodes: ExportTocNode[]): ExportTocNode[] {
  const out: ExportTocNode[] = [];
  const walk = (list: ExportTocNode[]) => { for (const node of list) { out.push(node); walk(node.children || []); } };
  walk(nodes);
  return out;
}

/** 只统计“没有已勾选祖先”的目录项，避免父章与其子章重复计入段落数。 */
function topMostSelected(nodes: ExportTocNode[], checked: Set<string>): ExportTocNode[] {
  const out: ExportTocNode[] = [];
  const walk = (list: ExportTocNode[], ancestorChecked: boolean) => {
    for (const node of list) {
      const on = checked.has(node.block_id);
      if (on && !ancestorChecked) out.push(node);
      walk(node.children || [], ancestorChecked || on);
    }
  };
  walk(nodes, false);
  return out;
}

function collectMissing(nodes: ExportTocNode[]): Set<string> {
  const out = new Set<string>();
  const visit = (list: ExportTocNode[]): boolean => {
    let any = false;
    for (const node of list) {
      const childAny = visit(node.children || []);
      if (node.missing_count > 0 || childAny) { out.add(node.node_id); any = true; }
    }
    return any;
  };
  visit(nodes);
  return out;
}

type VisibleNode = { node: ExportTocNode; depth: number };

function collectVisible(nodes: ExportTocNode[], expanded: Set<string>, keep: (node: ExportTocNode) => boolean): VisibleNode[] {
  const out: VisibleNode[] = [];
  const walk = (list: ExportTocNode[], depth: number) => {
    for (const node of list) {
      if (!keep(node)) continue;
      out.push({ node, depth });
      if ((node.children || []).length && expanded.has(node.node_id)) walk(node.children, depth + 1);
    }
  };
  walk(nodes, 0);
  return out;
}

function pageLabel(node: ExportTocNode) {
  return node.page_index >= 0 ? `第 ${node.page_index + 1} 页` : "";
}

/* ----------------------------- 组件 ----------------------------- */

export default function ExportDialog({ project, close, onMessage }: {
  project: Project;
  close: () => void;
  onMessage: (message: string) => void;
}) {
  const [options, setOptions] = useState<ExportOptions | null>(null);
  const [loadError, setLoadError] = useState("");
  const [mode, setMode] = useState<"source" | "translation" | "bilingual">("source");
  const [locale, setLocale] = useState("");
  const [formats, setFormats] = useState<Set<ExportFormatId>>(new Set());
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [onlyMissing, setOnlyMissing] = useState(false);
  const [filename, setFilename] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ExportRecord | null>(null);
  const [history, setHistory] = useState<ExportRecord[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [openRecord, setOpenRecord] = useState("");
  const [renamingId, setRenamingId] = useState("");
  const [renameValue, setRenameValue] = useState("");
  const [deletingId, setDeletingId] = useState("");

  const initialLocale = project.target_language || "zh-CN";

  const load = useCallback(async () => {
    setLoadError("");
    try {
      const value = await fetchExportOptions(project.project_id, initialLocale);
      setOptions(value);
      setHistory(value.recent || []);
      // 默认：EPUB 对照版；无译文时由下方 mode 逻辑回退原文。
      const available = value.formats.map(item => item.id);
      const preferred = (["epub"] as ExportFormatId[]).filter(id => available.includes(id));
      setFormats(new Set(preferred.length ? preferred : available.slice(0, 1)));
      setLocale(value.default_locale || initialLocale);
      setFilename(value.title);
      const all = flatten(value.toc);
      setChecked(new Set(all.map(node => node.block_id)));
      setExpanded(new Set(value.toc.map(node => node.node_id)));
      setMode(value.locales.length ? "bilingual" : "source");
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error));
    }
  }, [initialLocale, project.project_id]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, close]);

  const allNodes = useMemo(() => (options ? flatten(options.toc) : []), [options]);
  const missingNodes = useMemo(() => (options ? collectMissing(options.toc) : new Set<string>()), [options]);
  const keep = useCallback((node: ExportTocNode) => !onlyMissing || missingNodes.has(node.node_id), [onlyMissing, missingNodes]);
  const visible = useMemo(() => (options ? collectVisible(options.toc, expanded, keep) : []), [options, expanded, keep]);

  const selectedTop = useMemo(() => (options ? topMostSelected(options.toc, checked) : []), [options, checked]);
  const selectedBlocks = useMemo(() => selectedTop.reduce((sum, node) => sum + node.block_count, 0), [selectedTop]);
  const selectedMissing = useMemo(() => selectedTop.reduce((sum, node) => sum + node.missing_count, 0), [selectedTop]);
  const hasToc = Boolean(options && options.toc.length);
  const hasTranslation = Boolean(options && options.locales.length);
  const needsTranslation = mode === "translation" || mode === "bilingual";
  const canSubmit = Boolean(options) && formats.size > 0 && (!hasToc || checked.size > 0) && (!needsTranslation || hasTranslation) && !busy;

  const toggleFormat = (id: ExportFormatId) => setFormats(previous => {
    const next = new Set(previous);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const toggleChecked = (blockId: string) => setChecked(previous => {
    const next = new Set(previous);
    if (next.has(blockId)) next.delete(blockId); else next.add(blockId);
    return next;
  });
  const toggleExpanded = (nodeId: string) => setExpanded(previous => {
    const next = new Set(previous);
    if (next.has(nodeId)) next.delete(nodeId); else next.add(nodeId);
    return next;
  });
  const selectAll = () => setChecked(new Set(allNodes.map(node => node.block_id)));
  const clearAll = () => setChecked(new Set());
  const invert = () => setChecked(previous => new Set(allNodes.filter(node => !previous.has(node.block_id)).map(node => node.block_id)));

  const submit = async () => {
    if (!options || !canSubmit) return;
    setBusy(true);
    try {
      const record = await createExport(project.project_id, {
        formats: [...formats],
        mode,
        locale,
        chapters: hasToc ? [...checked] : [],
        filename: filename.trim(),
        missing_policy: "fallback_source",
      });
      setResult(record);
      setHistory(previous => [record, ...previous.filter(item => item.export_id !== record.export_id)].slice(0, 50));
      for (const warning of record.warnings || []) onMessage(warning);
      onMessage(`导出完成：${record.files.filter(file => file.single_file).length} 个文件`);
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const revealRecord = (record: ExportRecord) => {
    if (isDesktop) void revealPath(record.path).catch(error => onMessage(String(error)));
    else onMessage(record.path);
  };

  const recordTitle = (record: ExportRecord) => record.display_name || MODE_LABEL[record.mode] || record.mode;
  const startRename = (record: ExportRecord) => {
    setDeletingId("");
    setRenamingId(record.export_id);
    setRenameValue(recordTitle(record));
  };
  const saveRename = async (record: ExportRecord) => {
    const value = renameValue.trim();
    if (!value) return;
    try {
      const updated = await renameExport(project.project_id, record.export_id, value);
      setHistory(previous => previous.map(item => item.export_id === updated.export_id ? { ...item, ...updated } : item));
      setResult(previous => previous && previous.export_id === updated.export_id ? { ...previous, ...updated } : previous);
      setRenamingId("");
      onMessage("导出记录已重命名");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const removeRecord = async (record: ExportRecord) => {
    try {
      await deleteExport(project.project_id, record.export_id);
      setHistory(previous => previous.filter(item => item.export_id !== record.export_id));
      setResult(previous => previous && previous.export_id === record.export_id ? null : previous);
      if (openRecord === record.export_id) setOpenRecord("");
      setDeletingId("");
      onMessage("已删除该次导出的产物与记录");
    } catch (error) {
      onMessage(error instanceof Error ? error.message : String(error));
    }
  };
  const saveFile = (record: ExportRecord, relPath: string) => {
    if (isDesktop) {
      void saveCopy(`${record.path}/${relPath}`)
        .then(value => { if (value) onMessage(`已另存为 ${value}`); })
        .catch(error => onMessage(String(error)));
      return;
    }
    void downloadExportFile(project.project_id, record.export_id, relPath).catch(error => onMessage(String(error)));
  };

  const renderFiles = (record: ExportRecord) => <div className="export-files">
    {record.files.map(file => <div className="export-file" key={file.rel_path}>
      <span className={`export-file-kind${file.single_file ? "" : " is-folder"}`}>{file.single_file ? (file.name.split(".").pop() || "file").toUpperCase().slice(0, 4) : "DIR"}</span>
      <span className="export-file-name" title={file.rel_path}>{file.rel_path}</span>
      <span className="export-file-size">{file.single_file ? formatSize(file.bytes) : "文件夹"}</span>
      <span className="export-file-acts">
        {isDesktop
          ? <>
              <button onClick={() => revealRecord(record)}>显示位置</button>
              {file.single_file && <button onClick={() => saveFile(record, file.rel_path)}>另存为…</button>}
            </>
          : <button onClick={() => saveFile(record, file.rel_path)}>下载</button>}
      </span>
    </div>)}
  </div>;

  return <div className="res-pop export-backdrop u-backdrop" onClick={event => { if (event.target === event.currentTarget && !busy) close(); }}>
    <section className="res-dialog export-dialog u-modal u-modal--lg" role="dialog" aria-modal="true" aria-label={`导出 ${project.title}`}>
      <div className="res-dialog-head u-modal__head">
        <span className="res-mono">EX</span>
        <div className="export-header-copy">
          <h2>导出 · {project.title}</h2>
          <p className="src">选择格式、内容版本与章节；产物写入项目 exports/ 目录，也可另存到外部位置。</p>
        </div>
        <button className="res-iconbtn" aria-label="关闭" disabled={busy} onClick={close}>✕</button>
      </div>

      <div className="res-dialog-body export-body u-modal__body">
        {loadError
          ? <div className="export-state is-error"><b>无法读取导出选项</b><p>{loadError}</p><button onClick={() => void load()}>重试</button></div>
          : !options
            ? <div className="export-state"><div className="u-spinner"/><div className="u-skeleton"/>正在读取目录与译文状态…</div>
            : <div className="export-grid">
                {/* 左：章节选择 */}
                <aside className="export-side">
                  <div className="export-side-head">
                    <b>章节</b>
                    <span>{checked.size} / {allNodes.length || 1} 已选</span>
                  </div>
                  <div className="export-chapter-actions">
                    <button onClick={selectAll}>全选</button>
                    <button onClick={clearAll}>清空</button>
                    <button onClick={invert}>反选</button>
                    <label className="export-filter"><input type="checkbox" checked={onlyMissing} disabled={!hasToc} onChange={event => setOnlyMissing(event.target.checked)}/>只看有缺译</label>
                  </div>
                  {!hasToc
                    ? <div className="export-notoc">该书还没有确认的目录结构，将作为整本导出（{options.block_count} 段）。</div>
                    : <div className="export-tree">
                        {visible.map(({ node, depth }) => {
                          const ratio = node.block_count ? node.translated_count / node.block_count : 0;
                          return <div className="export-node" key={node.node_id} style={{ ["--export-indent" as string]: `calc(${depth} * var(--sp-4))` }}>
                            {(node.children || []).length
                              ? <button className={`export-caret${expanded.has(node.node_id) ? " open" : ""}`} aria-label={expanded.has(node.node_id) ? "收起" : "展开"} onClick={() => toggleExpanded(node.node_id)}>▸</button>
                              : <span className="export-caret is-empty"/>}
                            <input type="checkbox" checked={checked.has(node.block_id)} onChange={() => toggleChecked(node.block_id)} aria-label={`选择 ${node.title}`}/>
                            <span className="export-node-title" title={node.title}>{node.title || "（无标题）"}</span>
                            <span className="export-node-meta">
                              {pageLabel(node) && <small>{pageLabel(node)}</small>}
                              <small>{node.translated_count}/{node.block_count} 段</small>
                              {node.missing_count > 0 && <i className="export-node-warn" title={`${node.missing_count} 段缺译，导出时回退原文`}>{node.missing_count}</i>}
                              <i className="export-node-bar"><b style={{ width: `${Math.round(ratio * 100)}%` }}/></i>
                            </span>
                          </div>;
                        })}
                        {onlyMissing && !visible.length && <div className="export-notoc">当前目录中没有缺译的章节。</div>}
                      </div>}
                </aside>

                {/* 右：配置 + 摘要 + 结果 */}
                <div className="export-main">
                  <div className="export-block">
                    <h4>格式</h4>
                    <div className="export-formats">
                      {options.formats.map(item => <label key={item.id} className={`export-chip${formats.has(item.id) ? " on" : ""}`}>
                        <input type="checkbox" checked={formats.has(item.id)} onChange={() => toggleFormat(item.id)}/>
                        <span><b>{item.label}</b><small>{item.id === "html" ? "每章一个页面 + 目录页" : item.id === "markdown" ? "单文件 + assets" : item.single_file ? "单文件" : "文件夹"}</small></span>
                      </label>)}
                    </div>
                  </div>

                  <div className="export-block">
                    <h4>内容版本</h4>
                    <div className="radio">
                      {options.modes.map(item => {
                        const disabled = item.id !== "source" && !hasTranslation;
                        return <label key={item.id} className={`${mode === item.id ? "on" : ""}${disabled ? " dis" : ""}`}>
                          <input type="radio" name="export-mode" checked={mode === item.id} disabled={disabled} onChange={() => setMode(item.id)}/>
                          <span className="n"><b>{item.label}</b><small>{MODE_DESC[item.id]}</small></span>
                        </label>;
                      })}
                    </div>
                    {!hasTranslation && <div className="export-warn">该书还没有译文，暂只能导出原文。先在「对照翻译」中生成译文后再导出译文或对照版。</div>}
                    {needsTranslation && <div className="export-inline">
                      <label>目标语言
                        <select value={locale} onChange={event => setLocale(event.target.value)}>
                          {options.locales.map(value => <option key={value} value={value}>{localeLabel(value)}</option>)}
                        </select>
                      </label>
                      <label className="grow">文件名（可留空用书名）
                        <input value={filename} maxLength={200} placeholder={options.title} onChange={event => setFilename(event.target.value)}/>
                      </label>
                    </div>}
                    {!needsTranslation && <div className="export-inline">
                      <label className="grow">文件名（可留空用书名）
                        <input value={filename} maxLength={200} placeholder={options.title} onChange={event => setFilename(event.target.value)}/>
                      </label>
                    </div>}
                  </div>

                  <div className="export-block export-summary">
                    <h4>导出摘要</h4>
                    <div className="export-kv"><span>格式</span><b>{formats.size ? [...formats].join(" · ").toUpperCase() : "未选择"}</b></div>
                    <div className="export-kv"><span>内容</span><b>{options.modes.find(item => item.id === mode)?.label || mode}{needsTranslation ? ` · ${localeLabel(locale)}` : ""}</b></div>
                    <div className="export-kv"><span>章节 / 段落</span><b>{hasToc ? `${checked.size} 项 / ${selectedBlocks} 段` : `整本 / ${options.block_count} 段`}</b></div>
                    <div className="export-kv"><span>图片</span><b>{options.image_count} 张（全书）</b></div>
                    {needsTranslation && hasToc && <div className="export-kv"><span>译文覆盖</span><b>{selectedBlocks ? `${selectedBlocks - selectedMissing} 已译 · ` : ""}{selectedMissing ? `${selectedMissing} 缺译（回退原文）` : "无缺译"}</b></div>}
                    {needsTranslation && !hasToc && <div className="export-kv"><span>缺译处理</span><b>缺失处回退原文</b></div>}
                  </div>

                  {result && <div className="export-block export-result">
                    <h4>本次导出<span className="grow"/><span className="export-ok">完成</span></h4>
                    {result.warnings.length > 0 && <div className="export-warn">{result.warnings.map(w => <div key={w}>{w}</div>)}</div>}
                    {renderFiles(result)}
                  </div>}

                  <div className="export-block export-history">
                    <h4>导出历史<span className="grow"/><button className="export-toggle" onClick={() => setHistoryOpen(open => !open)}>{historyOpen ? "收起" : `展开（${history.length}）`}</button></h4>
                    {!historyOpen
                      ? <div className="export-hint">共 {history.length} 次导出记录，最近一次：{history[0] ? new Date(history[0].created_at).toLocaleString() : "无"}</div>
                      : !history.length
                        ? <div className="export-hint">还没有导出记录。</div>
                        : history.map(record => <div className="export-hist" key={record.export_id}>
                            <div className="export-hist-bar">
                              <button className="export-hist-head" onClick={() => setOpenRecord(openRecord === record.export_id ? "" : record.export_id)}>
                                <b>{recordTitle(record)}</b>
                                <span>{record.formats.join(" · ").toUpperCase()}{record.locale ? ` · ${record.locale}` : ""}</span>
                                <small>{new Date(record.created_at).toLocaleString()} · {formatSize(record.bytes)}</small>
                              </button>
                              <span className="export-hist-acts">
                                {isDesktop && <button title="在 Finder 中显示产物" onClick={() => revealRecord(record)}>显示位置</button>}
                                <button title="重命名这次导出" onClick={() => startRename(record)}>重命名</button>
                                {deletingId === record.export_id
                                  ? <>
                                      <button className="danger" onClick={() => void removeRecord(record)}>确认删除</button>
                                      <button onClick={() => setDeletingId("")}>取消</button>
                                    </>
                                  : <button className="danger" title="删除产物目录与记录" onClick={() => setDeletingId(record.export_id)}>删除</button>}
                              </span>
                            </div>
                            {renamingId === record.export_id && <div className="export-hist-rename">
                              <input autoFocus value={renameValue} maxLength={200} aria-label="导出名称"
                                onChange={event => setRenameValue(event.target.value)}
                                onKeyDown={event => { if (event.key === "Enter") void saveRename(record); }}/>
                              <button className="primary" onClick={() => void saveRename(record)}>保存</button>
                              <button onClick={() => setRenamingId("")}>取消</button>
                            </div>}
                            {openRecord === record.export_id && renderFiles(record)}
                          </div>)}
                  </div>
                </div>
              </div>}
      </div>

      <div className="res-dialog-foot u-modal__foot">
        <span className="grow"/>
        <button disabled={busy} onClick={close}>取消</button>
        <button className="primary" disabled={!canSubmit} onClick={() => void submit()}>{busy ? "正在导出…" : "开始导出"}</button>
      </div>
    </section>
  </div>;
}
