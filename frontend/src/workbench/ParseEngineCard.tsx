import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";

/** 引擎状态/来源类型（/parse/engine 与 /parse/engine/sources）。 */
export type EngineStatus = {
  backend_id: string; installed: boolean; version: string; engine_root: string; bytes: number;
  installing: boolean; phase: string; step_index: number; steps: string[]; step_labels: Record<string, string>;
  progress: number | null; message: string; error: string; cancel_requested: boolean;
  active_jobs: string[]; supported: boolean;
};
export type EngineSelection = { kind: "managed" | "external_cli" | "http_service"; external_dir?: string; http_url?: string; allow_plaintext_http?: boolean };
export type ExternalProbe = {
  available: boolean; compatible: boolean; kind?: string; path: string; kit_binary?: string;
  version?: string; models_ready?: boolean; reason: string;
};
export type EngineSources = {
  selection: EngineSelection; discovered: ExternalProbe[];
  http_key_configured: boolean; http_key_url: string;
};

export function formatEngineBytes(bytes: number) {
  if (!bytes) return "";
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
  return `${(bytes / 1024 / 1024).toFixed(0)} MB`;
}

const KIND_LABEL: Record<string, string> = {
  managed: "Literature 独立安装",
  external_cli: "已有本机安装",
  http_service: "已有解析服务",
};

async function loadEngine(): Promise<{ status: EngineStatus; sources: EngineSources; localAvailable: boolean; localLabel: string; localReason: string }> {
  const [status, sources, backends] = await Promise.all([
    api<EngineStatus>("/parse/engine"),
    api<EngineSources>("/parse/engine/sources"),
    api<{ backends: Record<string, unknown>[] }>("/parse/backends"),
  ]);
  const local = backends.backends.find(item => item.backend_id === "mineru_local") || {};
  return {
    status, sources,
    localAvailable: Boolean(local.available),
    localLabel: String(local.display_name || "本地解析"),
    localReason: String(local.unavailable_reason || ""),
  };
}

type Props = {
  onMessage: (value: string) => void;
  /** 导入页内嵌时用紧凑展示。 */
  compact?: boolean;
};

/** 解析引擎管理卡：下载安装 / 使用已有安装 / 连接已有服务。 */
export default function ParseEngineCard({ onMessage, compact = false }: Props) {
  const [data, setData] = useState<Awaited<ReturnType<typeof loadEngine>> | null>(null);
  const [loadError, setLoadError] = useState("");
  // 草稿表单：初始化自服务端一次；用户编辑后刷新轮询不再覆盖。
  const [externalDir, setExternalDir] = useState("");
  const [probe, setProbe] = useState<ExternalProbe | null>(null);
  const [httpUrl, setHttpUrl] = useState("");
  const [httpKey, setHttpKey] = useState("");
  const [allowPlaintext, setAllowPlaintext] = useState(false);
  const dirty = useRef({ external: false, http: false });
  const initialized = useRef(false);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const value = await loadEngine();
      setData(value);
      setLoadError("");
      if (!initialized.current) {
        initialized.current = true;
        setExternalDir(value.sources.selection.external_dir || "");
        setHttpUrl(value.sources.selection.http_url || "");
        setAllowPlaintext(Boolean(value.sources.selection.allow_plaintext_http));
      } else {
        // 只回填未在编辑的字段（服务端状态变化不冲掉用户输入）。
        if (!dirty.current.external && !externalDir) setExternalDir(value.sources.selection.external_dir || "");
        if (!dirty.current.http) {
          if (!httpUrl) setHttpUrl(value.sources.selection.http_url || "");
          if (!httpUrl && value.sources.selection.http_url) setAllowPlaintext(Boolean(value.sources.selection.allow_plaintext_http));
        }
      }
    } catch (error) {
      setLoadError(String(error));
    }
  }, [externalDir, httpUrl]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (!data?.status.installing) return;
    const timer = window.setInterval(() => { void refresh(); }, 1500);
    return () => window.clearInterval(timer);
  }, [data?.status.installing, refresh]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try { await fn(); initialized.current = false; await refresh(); onMessage(message); }
    catch (error) { onMessage(String(error)); }
    finally { setBusy(false); }
  };
  const install = (force = false) => act(
    () => api("/parse/engine/install", { method: "POST", body: JSON.stringify({ action: "install", force }) }),
    force ? "开始修复引擎（重跑安装步骤，已完成的步骤会跳过）。" : "开始下载安装本地 MinerU 引擎。",
  );
  const cancel = () => act(() => api("/parse/engine/cancel", { method: "POST", body: JSON.stringify({ action: "cancel" }) }), "已请求取消安装；已下载内容保留，可继续安装。");
  const uninstall = () => act(() => api("/parse/engine/uninstall", { method: "POST", body: JSON.stringify({ action: "uninstall" }) }), "已卸载独立引擎（不影响文献库与已有解析结果）。");
  const selectManaged = () => act(() => api("/parse/engine/select", { method: "POST", body: JSON.stringify({ kind: "managed" }) }), "已切回 Literature 独立安装。");
  const probeExternal = async () => {
    if (!externalDir.trim()) { onMessage("请先填写引擎目录。"); return; }
    setBusy(true);
    try { setProbe(await api<ExternalProbe>(`/parse/engine/probe-external?external_dir=${encodeURIComponent(externalDir.trim())}`)); }
    catch (error) { onMessage(String(error)); }
    finally { setBusy(false); }
  };
  const selectExternal = (dir = externalDir.trim()) => act(async () => {
    if (!dir) throw new Error("请先填写引擎目录。");
    await api("/parse/engine/select", { method: "POST", body: JSON.stringify({ kind: "external_cli", external_dir: dir }) });
  }, "已选用本机已有安装（只读调用，不修改外部环境）。");
  const selectHttp = () => act(async () => {
    if (!httpUrl.trim()) throw new Error("请先填写服务地址。");
    // 一次调用完成「新密钥校验 + 连接」：验证成功才保存密钥与选择；失败不破坏原配置。
    await api("/parse/engine/select", {
      method: "POST",
      body: JSON.stringify({ kind: "http_service", http_url: httpUrl.trim(), allow_plaintext_http: allowPlaintext, http_key: httpKey.trim() }),
    });
    setHttpKey("");
  }, "已连接解析服务（连接仅探测与调用，不管理远端）。");
  const disconnectHttp = () => act(async () => {
    if (httpUrl.trim()) await api(`/parse/engine/http/key?http_url=${encodeURIComponent(httpUrl.trim())}`, { method: "DELETE" });
    await api("/parse/engine/select", { method: "POST", body: JSON.stringify({ kind: "managed" }) });
  }, "已断开解析服务并清除该地址的密钥。");

  if (loadError) return <div className="engine-card">
    <small className="ocr-error">解析引擎状态读取失败：{loadError}</small>
    <div className="engine-actions"><button disabled={busy} onClick={() => { setLoadError(""); void refresh(); }}>重试</button></div>
  </div>;
  if (!data) return <p className="settings-hint">正在读取解析引擎状态…</p>;
  const { status, sources, localAvailable, localLabel, localReason } = data;
  const kind = sources.selection.kind;

  return <div className={`engine-card${compact ? " engine-card-compact" : ""}`}>
    <p className="settings-hint">
      当前来源：<strong>{KIND_LABEL[kind] || kind}</strong>
      {localAvailable ? <> · <strong>{localLabel}</strong></> : <> · 不可用：{localReason}</>}
    </p>

    {kind === "managed" && <>
      <p className="settings-hint">
        {status.installed
          ? <>已安装 MinerU {status.version} · {formatEngineBytes(status.bytes) || "计算中"} · 位置 {status.engine_root}</>
          : status.installing ? status.message || "正在安装…" : "未安装。下载约 3.4GB（含模型），安装后可离线解析。"}
      </p>
      {status.installing && <>
        <progress max={1} value={status.progress ?? (status.step_index / Math.max(1, status.steps.length))}/>
        <small className="settings-hint">{status.step_labels[status.phase] || status.phase}（第 {status.step_index + 1}/{status.steps.length} 步）</small>
      </>}
      {status.error && <small className="ocr-error">{status.error}</small>}
      {status.active_jobs.length > 0 && <small className="settings-hint">正在被 {status.active_jobs.length} 个本地解析任务使用。</small>}
      <div className="engine-actions">
        {status.installing
          ? <button disabled={busy} onClick={cancel}>{status.cancel_requested ? "正在取消…" : "取消安装"}</button>
          : status.installed
            ? <><button disabled={busy} onClick={() => install(true)} title="重跑安装步骤（幂等），用于引擎损坏时修复">修复（重跑安装步骤）</button><button disabled={busy} onClick={uninstall}>卸载引擎</button></>
            : <button className="primary" disabled={busy || !status.supported} onClick={() => install(false)}>{status.phase === "failed" || status.phase === "cancelled" ? "继续安装" : "下载并安装"}</button>}
      </div>
    </>}

    {kind === "external_cli" && <>
      <p className="settings-hint">只读调用外部引擎：不升级、不修复、不清理外部环境；模型目录只读共享，运行缓存全部写入 Literature 目录。外部应用升级/卸载后需重新检测。</p>
      <label>引擎目录（含 venv/bin/mineru-kit，或直接指向 mineru-kit 可执行文件）
        <input type="text" value={externalDir} onChange={event => { dirty.current.external = true; setExternalDir(event.target.value); }} placeholder="例如 ~/Library/Application Support/TextFold/engine"/>
      </label>
      <div className="engine-actions">
        <button disabled={busy} onClick={probeExternal}>检测</button>
        <button className="primary" disabled={busy || !(probe?.available || localAvailable)} onClick={() => selectExternal()}>重新检测并选用</button>
        <button disabled={busy} onClick={selectManaged}>改用独立安装</button>
      </div>
      {probe && <small className="settings-hint">{probe.available ? `检测通过：MinerU ${probe.version}，模型就绪。` : probe.reason}</small>}
    </>}

    {kind === "http_service" && <>
      <p className="settings-hint">连接自建 MinerU 解析服务（官方 mineru-api 的 /v1 协议）。Literature 只调用与检测，不部署、不升级远端。</p>
      <label>服务地址<input type="text" value={httpUrl} onChange={event => { dirty.current.http = true; setHttpUrl(event.target.value); }} placeholder="例如 http://127.0.0.1:8000"/></label>
      <label>{sources.http_key_configured && sources.http_key_url === httpUrl.trim() ? "服务密钥（该地址已保存；输入可替换）" : "服务密钥（服务要求时填写，仅存 Keychain）"}
        <input type="password" value={httpKey} onChange={event => setHttpKey(event.target.value)} placeholder={sources.http_key_configured && sources.http_key_url === httpUrl.trim() ? "留空使用已保存密钥" : "选填"}/>
      </label>
      {(httpUrl.startsWith("http://") || allowPlaintext) && <label className="option-toggle"><span><strong>允许明文 HTTP</strong><small>仅限内网/自建；公网请用 HTTPS</small></span><input type="checkbox" checked={allowPlaintext} onChange={event => setAllowPlaintext(event.target.checked)}/></label>}
      <div className="engine-actions">
        <button className="primary" disabled={busy || !httpUrl.trim()} onClick={selectHttp}>{httpKey.trim() ? "用新密钥检测并连接" : "重新检测并连接"}</button>
        <button disabled={busy} onClick={disconnectHttp}>断开</button>
      </div>
    </>}

    {!compact && <hr/>}
    {!compact && <details><summary>切换其他来源（可首次配置）</summary>
      <div className="engine-actions">
        {kind !== "managed" && <button disabled={busy} onClick={selectManaged}>下载安装（独立安装）</button>}
        {kind !== "external_cli" && <button disabled={busy || !(probe?.available)} onClick={() => selectExternal()}>使用已有安装</button>}
        {kind !== "http_service" && <button className="primary" disabled={busy || !httpUrl.trim()} onClick={selectHttp}>连接已有服务</button>}
      </div>
      <label>已有安装目录（首次可直接粘贴后检测/选用）
        <input type="text" value={externalDir} onChange={event => { dirty.current.external = true; setExternalDir(event.target.value); }} placeholder="例如 ~/Library/Application Support/TextFold/engine"/>
      </label>
      <div className="engine-actions"><button disabled={busy || !externalDir.trim()} onClick={probeExternal}>检测该目录</button></div>
      {probe && <small className="settings-hint">{probe.available ? `检测通过：MinerU ${probe.version}，可选用。` : probe.reason}</small>}
      {sources.discovered.length > 0 && <div>
        <small className="settings-hint">本机发现的已有引擎：</small>
        {sources.discovered.map(item => <div key={item.path} className="engine-discovered">
          <small>{item.path} · MinerU {item.version}{item.available ? " · 可一键选用" : ` · ${item.reason}`}</small>
          {item.available && <button disabled={busy} onClick={() => { dirty.current.external = true; setExternalDir(item.path); setProbe(item); selectExternal(item.path); }}>选用</button>}
        </div>)}
      </div>}
      <label>解析服务地址（首次连接可在下方直接填密钥）
        <input type="text" value={httpUrl} onChange={event => { dirty.current.http = true; setHttpUrl(event.target.value); }} placeholder="http://127.0.0.1:8000"/>
      </label>
      <label>服务密钥<input type="password" value={httpKey} onChange={event => setHttpKey(event.target.value)} placeholder="服务要求时填写"/></label>
      {(httpUrl.startsWith("http://") || allowPlaintext) && <label className="option-toggle"><span><strong>允许明文 HTTP</strong><small>仅限内网/自建</small></span><input type="checkbox" checked={allowPlaintext} onChange={event => setAllowPlaintext(event.target.checked)}/></label>}
    </details>}
  </div>;
}
