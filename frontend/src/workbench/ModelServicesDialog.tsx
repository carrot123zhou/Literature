import { useEffect, useMemo, useState } from "react";
import type { ModelEntry, ModelRole, ModelService, RemoteModel } from "../types";
import type { ModelServicePayload, ModelServicesApi } from "../modelServices";
import { inferReasoningStyleFromModel } from "./ThinkingLevelField";

const PRESETS: Record<string, { label: string; base_url: string }> = {
  openai: { label: "OpenAI", base_url: "https://api.openai.com/v1" },
  deepseek: { label: "DeepSeek", base_url: "https://api.deepseek.com" },
  glm: { label: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4" },
  qwen: { label: "Qwen / 阿里云百炼", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  custom: { label: "自定义中转站 / OpenAI 兼容", base_url: "" },
};

type Draft = { service_id: string; name: string; preset: string; base_url: string; allow_plaintext_http: boolean; models: ModelEntry[] };

/** 思考配置挂在单个模型上（v3：provider → model 两层）。 */
type ReasoningFields = { model?: string; reasoning_style?: string; reasoning_effort?: string; reasoning_budget?: number };

/** 服务级思考档位：比任务级多一个「自动识别」（对应后端 reasoning_style="auto"）。 */
type ServiceThinkingLevel = "inherit" | "auto" | "off" | "low" | "medium" | "high";

const SERVICE_LEVEL_OPTIONS: { value: ServiceThinkingLevel; label: string }[] = [
  { value: "inherit", label: "跟随默认（不发送思考字段）" },
  { value: "auto", label: "自动识别（推荐：翻译/结构任务自动关思考）" },
  { value: "off", label: "关闭思考" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

const LEVEL_HINTS: Record<ServiceThinkingLevel, string> = {
  inherit: "不发送任何思考字段，行为完全由服务端默认决定。",
  auto: "按模型名识别思考协议，翻译/结构等确定性任务会自动关闭思考（首次任务探测一次并按服务缓存）。",
  off: "始终关闭思考：OpenAI 协议发送 reasoning_effort=minimal；Qwen/GLM 发送关闭开关。",
  low: "低强度思考：OpenAI 协议映射 reasoning_effort=low；Qwen/GLM 开关型映射为开启。",
  medium: "中强度思考：OpenAI 协议映射 reasoning_effort=medium；Qwen/GLM 开关型映射为开启。",
  high: "高强度思考：OpenAI 协议映射 reasoning_effort=high；Qwen/GLM 开关型映射为开启。",
};

const PROTOCOL_OPTIONS: { value: string; label: string }[] = [
  { value: "auto", label: "自动识别（按模型名推断）" },
  { value: "openai_effort", label: "OpenAI · reasoning_effort" },
  { value: "qwen_thinking", label: "Qwen · enable_thinking" },
  { value: "glm_thinking", label: "智谱 GLM · thinking" },
];

const CONCRETE_PROTOCOLS = ["openai_effort", "qwen_thinking", "glm_thinking"];

/** 从已保存的 style/effort 反推 UI 档位；具体协议保留在 reasoning_style，保存往返不丢。 */
function levelFromEntry(entry: ReasoningFields): ServiceThinkingLevel {
  const style = entry.reasoning_style ?? "";
  const effort = entry.reasoning_effort ?? "";
  if (!style || style === "none") return "inherit"; // "none" 是后端负缓存标记；防御性归入默认档
  if (style === "auto") return "auto";
  if (style === "openai_effort") {
    if (effort === "minimal") return "off";
    if (["low", "medium", "high"].includes(effort)) return effort as ServiceThinkingLevel;
    return "auto";
  }
  // Qwen/GLM 开关型：off → 关闭；on（或空，等同跟随默认）→ 展示为自动识别语义。
  return effort === "off" ? "off" : "auto";
}

/** 把 UI 档位写回模型级 reasoning_* 字段；协议优先取显式选择，否则按模型名保守推断。 */
function applyLevelToEntry(entry: ReasoningFields, level: ServiceThinkingLevel): { style: string; effort: string; budget: number; warn?: string } {
  const budget = level === "inherit" ? 0 : entry.reasoning_budget ?? 0;
  if (level === "inherit") return { style: "", effort: "", budget: 0 };
  if (level === "auto") return { style: entry.reasoning_style || "auto", effort: "", budget };
  const style = CONCRETE_PROTOCOLS.includes(entry.reasoning_style ?? "") ? entry.reasoning_style : inferReasoningStyleFromModel(entry.model ?? "");
  if (!style) {
    return { style: "", effort: "", budget: 0, warn: "未能识别该模型的思考协议，已按「跟随默认」保存；可在展开编辑里显式指定思考协议后再选档位。" };
  }
  const effort = style === "openai_effort" ? (level === "off" ? "minimal" : level) : (level === "off" ? "off" : "on");
  return { style, effort, budget };
}

/** 协议切换时保留兼容的 effort，不兼容则重置为空（跟随默认）。 */
function effortForProtocol(current: string, protocol: string): string {
  const allowed = protocol === "openai_effort" ? ["", "minimal", "low", "medium", "high"] : ["", "on", "off"];
  return allowed.includes(current) ? current : "";
}

/** 把后端回显的原始错误按语义归一成用户能行动的提示（对齐 Cindy 的错误分类）。 */
function normalizeProviderError(raw: string): string {
  const text = String(raw || "");
  const lower = text.toLowerCase();
  // 明文 HTTP 拦截：这不是服务端错误，不能落到下面的“端点无法连接”里，否则用户会去查网络。
  if (/\u660e\u6587\s*http|plaintext\s*http/.test(lower) || text.includes("\u672c\u673a loopback")) {
    return "该地址是明文 HTTP，密钥会被明文传输，已默认拦截。若是自建/内网服务，请勾选「允许明文 HTTP」后重试；否则请改用 https://。";
  }
  if (/\b401\b|unauthorized|invalid[_ ]?api[_ ]?key/.test(lower)) return "密钥无效或已过期，请检查密钥后重试。";
  if (/\b403\b|forbidden|permission/.test(lower)) return "没有访问权限（403），请确认账号可用该资源。";
  if (/\b429\b|rate.?limit|too many requests/.test(lower)) return "触发限流（429），请稍后重试或调低并发。";
  if (/insufficient|balance|余额|欠费/.test(lower)) return "账户余额不足，请充值后重试。";
  if (/context length|maximum context|too long|too large|超长/.test(lower)) return "上下文超长：请减小输入或降低最大输出 tokens。";
  if (/model.{0,40}not.{0,10}(found|exist)|does not exist|model_not_found|无权使用/.test(lower)) return "模型不存在或无权使用，请检查模型 ID。";
  if (/\b404\b|not found|econnrefused|dns|timed? ?out|timeout|连接/.test(lower)) return "端点不存在或无法连接，请检查基础 URL。";
  return text;
}

/** 128000 → 128K；仅用于展示，不参与计算。 */
function formatTokens(value: number | undefined): string {
  if (!value || value <= 0) return "未知";
  if (value >= 1_000_000) return `${Math.round(value / 100_000) / 10}M`;
  if (value >= 1000) return `${Math.round(value / 1000)}K`;
  return String(value);
}
/** 单价展示按「/ 百万 tokens」；无报价时显示「无报价」。 */
function formatPrice(value: number | null | undefined, direction: "输入" | "输出"): string {
  return value == null ? `${direction}无报价` : `${direction} $${value} / 百万 tokens`;
}

function emptyDraft(): Draft {
  return { service_id: "", name: "", preset: "custom", base_url: "", allow_plaintext_http: false, models: [] };
}

/** 明文 HTTP 且不是本机 loopback：后端默认拦截，需要用户显式确认后才放行。
 * 与后端 providers._is_loopback_host 保持一致，只把精确的 localhost / 127.0.0.0\/8 / [::1] 视为本机。 */
function isPlaintextRemote(baseUrl: string): boolean {
  try {
    const parsed = new URL(baseUrl.trim());
    if (parsed.protocol !== "http:") return false;
    const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (host === "localhost" || host === "::1") return false;
    return !/^127\.(?:\d{1,3}\.){2}\d{1,3}$/.test(host);
  } catch { return false; }
}

function draftFrom(service: ModelService): Draft {
  return { service_id: service.service_id, name: service.name, preset: service.preset, base_url: service.base_url, allow_plaintext_http: Boolean(service.allow_plaintext_http), models: service.models.map(entry => ({ ...entry })) };
}
function remoteToEntry(remote: RemoteModel): ModelEntry {
  const context = remote.context_window || 0;
  return {
    model: remote.id,
    name: remote.id,
    context_window: context,
    // 服务没给输出上限时，按上下文 1/4 取一个保守值（不超过 16384 默认）。
    max_output_tokens: remote.max_output_tokens || (context > 0 ? Math.min(16384, Math.max(1024, Math.floor(context / 4))) : 16384),
    temperature: 0.2,
    input_price_per_million: remote.input_price_per_million,
    output_price_per_million: remote.output_price_per_million,
    reasoning_style: "",
    reasoning_effort: "",
    reasoning_budget: 0,
  };
}
function entryBadges(entry: ModelEntry): string[] {
  const badges: string[] = [];
  badges.push(`上下文 ${formatTokens(entry.context_window)}（tokens）`);
  badges.push(`最大输出 ${formatTokens(entry.max_output_tokens)}`);
  if (entry.input_price_per_million == null && entry.output_price_per_million == null) badges.push("该路由没有报价数据");
  else {
    if (entry.input_price_per_million != null) badges.push(formatPrice(entry.input_price_per_million, "输入"));
    if (entry.output_price_per_million != null) badges.push(formatPrice(entry.output_price_per_million, "输出"));
  }
  return badges;
}

/** 模型服务商库（v3 两层：服务商 → 模型）：左侧服务商列表，右侧服务商表单 + 内嵌模型列表。
 *
 * 作为「设置 → 模型服务」分区嵌入（不再是独立弹窗）。交互对齐 Cindy：
 * 「获取模型列表 → 选择器（可多选批量加）→ 追加进服务商并保存」；思考配置挂在每个
 * 模型上；顶部角色绑定为「服务商 + 模型」两级下拉。
 */
export default function ModelServicesPanel({ api, onMessage }: { api: ModelServicesApi; onMessage: (value: string) => void }) {
  const [draft, setDraft] = useState<Draft>(() => api.services[0] ? draftFrom(api.services[0]) : emptyDraft());
  const [keyInput, setKeyInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [listing, setListing] = useState(false);
  const [remoteOptions, setRemoteOptions] = useState<RemoteModel[]>([]);
  const [checkResult, setCheckResult] = useState("");
  const [statusError, setStatusError] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [modelQuery, setModelQuery] = useState("");
  const [pickedModels, setPickedModels] = useState<Set<string>>(new Set());
  const [expandedModel, setExpandedModel] = useState("");
  /** 用户点了「+ 新增」：不要被“自动选中第一个服务商”的效果覆盖。 */
  const [creating, setCreating] = useState(false);

  /** 状态行：默认成功色；失败/校验提示用 error=true 标红，避免「密钥无效」显示成绿色。 */
  const showStatus = (text: string, error = false) => { setCheckResult(text); setStatusError(error); };

  const isNew = !draft.service_id;
  const selected = useMemo(() => api.services.find(item => item.service_id === draft.service_id), [api.services, draft.service_id]);
  const filteredRemote = useMemo(() => {
    const query = modelQuery.trim().toLocaleLowerCase();
    if (!query) return remoteOptions;
    return remoteOptions.filter(entry => entry.id.toLocaleLowerCase().includes(query));
  }, [remoteOptions, modelQuery]);
  const existingIds = useMemo(() => new Set(draft.models.map(entry => entry.model)), [draft.models]);
  const freshRemoteCount = useMemo(() => remoteOptions.filter(entry => !existingIds.has(entry.id)).length, [remoteOptions, existingIds]);

  const resetTransient = () => { setKeyInput(""); setCheckResult(""); setRemoteOptions([]); setPickerOpen(false); setModelQuery(""); setPickedModels(new Set()); setExpandedModel(""); };

  // 首次加载（或删除后）自动选中第一个服务商；用户新建时不干扰。
  useEffect(() => {
    if (creating || draft.service_id || !api.services.length) return;
    setDraft(draftFrom(api.services[0]));
  }, [api.services, draft.service_id, creating]);

  const selectService = (service: ModelService) => {
    setCreating(false);
    setDraft(draftFrom(service));
    resetTransient();
  };

  const changePreset = (preset: string) => {
    setRemoteOptions([]);
    setPickerOpen(false);
    setPickedModels(new Set());
    setDraft(current => ({ ...current, preset, base_url: preset === "custom" ? current.base_url : PRESETS[preset].base_url, name: current.name || PRESETS[preset].label }));
  };

  const payloadOf = (current: Draft): ModelServicePayload => ({ name: current.name, preset: current.preset, base_url: current.base_url, allow_plaintext_http: current.allow_plaintext_http, models: current.models });

  const save = async () => {
    if (!draft.name.trim() || !draft.base_url.trim()) { showStatus("请填写显示名称与基础 URL。", true); return; }
    if (!draft.models.length) { showStatus("请至少添加一个模型：填好基础 URL 与 API Key 后点「获取模型列表」。", true); return; }
    setBusy(true);
    try {
      const saved = isNew ? await api.create(payloadOf(draft)) : await api.update(draft.service_id, payloadOf(draft));
      setCreating(false);
      setDraft(draftFrom(saved));
      // 未绑定的角色由后端在创建时自动绑定到服务商的第一个模型，这里不再改写用户手工设置。
      onMessage(`模型服务商已保存：${saved.name}（${saved.models.length} 个模型）`);
    } catch (error) { showStatus(normalizeProviderError(String(error)), true); }
    finally { setBusy(false); }
  };

  const remove = async () => {
    if (isNew) { setCreating(false); setDraft(emptyDraft()); resetTransient(); return; }
    setBusy(true);
    try { await api.remove(draft.service_id); setCreating(false); setDraft(emptyDraft()); resetTransient(); onMessage("模型服务商已删除"); }
    catch (error) { showStatus(normalizeProviderError(String(error)), true); }
    finally { setBusy(false); }
  };

  const saveKey = async () => {
    if (isNew) { showStatus("请先保存服务商，再填写 API Key。", true); return; }
    if (!keyInput.trim()) return;
    setBusy(true);
    try {
      await api.saveKey(draft.service_id, keyInput);
      setKeyInput("");
      onMessage("API Key 已保存到 macOS Keychain");
      await fetchModels();  // 密钥就绪后自动拉取模型，省去再点一次
    }
    catch (error) { showStatus(normalizeProviderError(String(error)), true); }
    finally { setBusy(false); }
  };

  const fetchModels = async () => {
    if (!draft.base_url.trim()) { showStatus("请先填写基础 URL", true); return; }
    if (isNew && !keyInput.trim()) { showStatus("请先填写 API Key（只保存到本机 Keychain），再获取模型列表。", true); return; }
    setListing(true);
    showStatus("");
    try {
      const list = await api.fetchModels({ base_url: draft.base_url.trim(), api_key: keyInput.trim(), service_id: isNew ? "" : draft.service_id, allow_plaintext_http: draft.allow_plaintext_http });
      const previousIds = new Set(remoteOptions.map(entry => entry.id));
      const fresh = list.filter(entry => !previousIds.has(entry.id)).length;
      setRemoteOptions(list);
      showStatus(!remoteOptions.length
        ? `已获取 ${list.length} 个模型，勾选后点「添加所选」。`
        : fresh > 0 ? `已发现 ${fresh} 个新模型。` : "已是最新，无新增。");
    } catch (error) { showStatus(`获取模型列表失败：${normalizeProviderError(String(error))}`, true); }
    finally { setListing(false); }
  };

  /** 基础 URL 失焦 / 打开选择器时静默补拉一次；已有列表时不重复请求。 */
  const ensureModels = async () => {
    if (listing || remoteOptions.length || !draft.base_url.trim()) return;
    if (isNew && !keyInput.trim()) return;
    await fetchModels();
  };

  /** 选择器「添加所选」：把勾选的远端模型追加进当前服务商（已存在的不重复），已保存则立即 PUT。 */
  const addSelected = async () => {
    const chosen = remoteOptions.filter(entry => pickedModels.has(entry.id) && !existingIds.has(entry.id));
    if (!chosen.length) return;
    const additions = chosen.map(remoteToEntry);
    const nextModels = [...draft.models, ...additions];
    setDraft(current => ({ ...current, models: nextModels }));
    setPickedModels(new Set());
    if (!isNew) {
      setBusy(true);
      try {
        // 只持久化模型列表：名称/基础 URL 等表单内未保存的改动仍留给「保存」按钮，
        // 避免勾选模型时把半成品表单一并写回。
        const saved = await api.update(draft.service_id, {
          name: selected?.name ?? draft.name, preset: selected?.preset ?? draft.preset,
          base_url: selected?.base_url ?? draft.base_url, models: nextModels,
          allow_plaintext_http: selected?.allow_plaintext_http ?? draft.allow_plaintext_http,
        });
        // 保留表单里未保存的改动（名称/地址/明文开关），不因为勾选模型而被回退。
        setDraft(current => ({
          ...draftFrom(saved),
          name: current.name, preset: current.preset, base_url: current.base_url,
          allow_plaintext_http: current.allow_plaintext_http,
        }));
        onMessage(`已添加 ${additions.length} 个模型到「${saved.name}」`);
      } catch (error) { showStatus(normalizeProviderError(String(error)), true); }
      finally { setBusy(false); }
    } else {
      onMessage(`已添加 ${additions.length} 个模型，点「创建服务商」保存`);
    }
  };

  const togglePicked = (id: string) => setPickedModels(current => {
    const next = new Set(current);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  /** 删除服务商下的一个模型（本地生效，点「保存」持久化）。 */
  const removeModel = (modelId: string) => {
    setDraft(current => ({ ...current, models: current.models.filter(entry => entry.model !== modelId) }));
    if (expandedModel === modelId) setExpandedModel("");
  };

  const updateModel = (modelId: string, patch: Partial<ModelEntry>) => {
    setDraft(current => ({ ...current, models: current.models.map(entry => entry.model === modelId ? { ...entry, ...patch } : entry) }));
  };

  const changeModelThinking = (entry: ModelEntry, level: ServiceThinkingLevel) => {
    const mapped = applyLevelToEntry(entry, level);
    updateModel(entry.model, { reasoning_style: mapped.style, reasoning_effort: mapped.effort, reasoning_budget: mapped.budget });
    if (mapped.warn) showStatus(mapped.warn, true);
  };

  const changeModelProtocol = (entry: ModelEntry, protocol: string) => {
    const style = protocol === "auto" ? "auto" : protocol;
    updateModel(entry.model, { reasoning_style: style, reasoning_effort: effortForProtocol(entry.reasoning_effort ?? "", style) });
  };

  const check = async () => {
    if (!draft.base_url.trim() || !draft.models.length) { showStatus("请先填写基础 URL 和至少一个模型 ID。", true); return; }
    if (isNew) { showStatus("请先保存服务商，再测试连接。", true); return; }
    setChecking(true);
    showStatus("");
    try {
      const result = await api.check(draft.service_id, expandedModel || draft.models[0]?.model);
      showStatus(`连接成功（${result.latency_ms}ms）`);
    } catch (error) { showStatus(normalizeProviderError(String(error)), true); }
    finally { setChecking(false); }
  };

  /** 角色绑定：服务商下拉 → 模型下拉（两级）。 */
  const roleValue = (item: ModelRole) => api.roles[item] ?? { service_id: "", model: "" };
  const changeRoleProvider = (item: ModelRole, serviceId: string) => {
    const provider = api.services.find(candidate => candidate.service_id === serviceId);
    api.setRoles({ [item]: { service_id: serviceId, model: provider?.models[0]?.model ?? "" } } as Partial<Record<ModelRole, { service_id: string; model: string }>>).catch(error => onMessage(String(error)));
  };
  const changeRoleModel = (item: ModelRole, model: string) => {
    const binding = roleValue(item);
    api.setRoles({ [item]: { service_id: binding.service_id, model } } as Partial<Record<ModelRole, { service_id: string; model: string }>>).catch(error => onMessage(String(error)));
  };

  return <div className="model-services-panel">
    <div className="model-role-row model-role-row-v3">
        {(["structure", "translation"] as ModelRole[]).map(item => {
          const binding = roleValue(item);
          const roleProvider = api.services.find(candidate => candidate.service_id === binding.service_id);
          return <label key={item}>{item === "structure" ? "结构校对模型" : "翻译模型"}
            <div className="model-form-row">
              <select aria-label={`${item === "structure" ? "结构校对" : "翻译"}服务商`} value={binding.service_id} onChange={event => changeRoleProvider(item, event.target.value)}>
                <option value="">未绑定</option>
                {api.services.map(provider => <option key={provider.service_id} value={provider.service_id}>{provider.name}</option>)}
              </select>
              <select aria-label={`${item === "structure" ? "结构校对" : "翻译"}模型`} value={binding.model || roleProvider?.models[0]?.model || ""} disabled={!roleProvider} onChange={event => changeRoleModel(item, event.target.value)}>
                {!roleProvider && <option value="">先选服务商</option>}
                {roleProvider?.models.map(entry => <option key={entry.model} value={entry.model}>{entry.name || entry.model}</option>)}
                {roleProvider && !roleProvider.models.length && <option value="">该服务商还没有模型</option>}
              </select>
            </div>
          </label>;
        })}
      </div>

      <div className="model-services-body">
        <aside className="model-service-list">
          <div className="model-service-list-head"><strong>服务商</strong><button onClick={() => { setCreating(true); setDraft(emptyDraft()); resetTransient(); }}>+ 新增</button></div>
          {api.services.length === 0 && <p className="empty-note">还没有模型服务商。点击「+ 新增」开始配置。</p>}
          {api.services.map(service => <button key={service.service_id} className={`model-service-item${service.service_id === draft.service_id ? " selected" : ""}`} onClick={() => selectService(service)}>
            <span className={`model-dot${service.configured ? " configured" : ""}`}/>
            <span><strong>{service.name}</strong><small>{service.base_url || "未填写基础 URL"}</small><small>{service.models.length} 个模型</small></span>
            <em>{Object.values(api.roles).some(binding => binding.service_id === service.service_id) ? "已绑定" : ""}</em>
          </button>)}
        </aside>

        <form className="model-service-form" onSubmit={event => { event.preventDefault(); void save(); }}>
          <div className="model-form-row">
            <label>显示名称<input value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="显示名称（默认用类型名）"/></label>
            <label>类型<select value={draft.preset} onChange={event => changePreset(event.target.value)}>{Object.entries(PRESETS).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}</select></label>
          </div>
          <label>基础 URL<input value={draft.base_url} onChange={event => setDraft({ ...draft, base_url: event.target.value })} onBlur={() => void ensureModels()} placeholder="https://your-relay.example.com/v1"/></label>
          {isPlaintextRemote(draft.base_url) && <div className={`model-plaintext-warning${draft.allow_plaintext_http ? " is-allowed" : ""}`} role="note">
            <p>{draft.allow_plaintext_http
              ? "已允许对该地址使用明文 HTTP：API Key 不加密传输，请确认这是可信的自建 / 内网服务。"
              : "该地址是明文 http://，不是本机 loopback。API Key 会以明文在网络上传输，默认已被拦下。"}</p>
            <label className="model-plaintext-check">
              <input type="checkbox" checked={draft.allow_plaintext_http} onChange={event => setDraft({ ...draft, allow_plaintext_http: event.target.checked })}/>
              允许明文 HTTP（仅限自建 / 内网服务；修改后需要点「保存」生效）
            </label>
          </div>}
          <label>API Key<input type="password" value={keyInput} onChange={event => setKeyInput(event.target.value)} placeholder={selected?.configured ? "已保存，留空则不变" : "只保存到 macOS Keychain"}/></label>
          <div className="model-form-actions">
            <button type="button" onClick={saveKey} disabled={busy || isNew || !keyInput.trim()}>保存密钥</button>
            <button type="button" onClick={() => { const next = !pickerOpen; setPickerOpen(next); if (next) void ensureModels(); }} disabled={listing}>{listing ? "正在获取模型…" : pickerOpen ? "收起列表" : "获取模型列表"}</button>
          </div>
          {pickerOpen && <div className="model-picker">
            <div className="model-picker-head">
              <strong>选择要添加的模型</strong>
              <span className="model-picker-count">已选 {pickedModels.size} / 共 {filteredRemote.length} 个{freshRemoteCount ? ` · ${freshRemoteCount} 个未添加` : ""}</span>
            </div>
            <div className="model-picker-head">
              <input autoFocus value={modelQuery} onChange={event => setModelQuery(event.target.value)} placeholder="搜索模型…"/>
              <button type="button" onClick={() => void fetchModels()} disabled={listing}>{listing ? "正在获取模型…" : "刷新"}</button>
            </div>
            <div className="model-picker-list">
              {filteredRemote.map(entry => {
                const exists = existingIds.has(entry.id);
                const picked = pickedModels.has(entry.id);
                return <button type="button" key={entry.id} className={`model-picker-item${picked ? " selected" : ""}${exists ? " exists" : ""}`} disabled={exists} title={exists ? "已在当前服务商中" : undefined} onClick={() => !exists && togglePicked(entry.id)}>
                  <span className="model-picker-check">{picked ? "✓" : exists ? "已添加" : ""}</span>
                  <span className="model-picker-main">
                    <strong>{entry.id}</strong>
                    <span className="model-picker-meta">
                      {entry.context_window > 0 && <em>上下文 {formatTokens(entry.context_window)}（tokens）</em>}
                      {entry.max_output_tokens > 0 && <em>最大输出 {formatTokens(entry.max_output_tokens)}</em>}
                      {entry.input_price_per_million == null && entry.output_price_per_million == null
                        ? <em>该路由没有报价数据</em>
                        : <>{entry.input_price_per_million != null && <em>{formatPrice(entry.input_price_per_million, "输入")}</em>}{entry.output_price_per_million != null && <em>{formatPrice(entry.output_price_per_million, "输出")}</em>}</>}
                    </span>
                  </span>
                </button>;
              })}
              {!listing && !filteredRemote.length && <p className="empty-note">{remoteOptions.length ? "没有匹配的模型。" : "还没有模型列表：填好基础 URL 与 API Key 后点「刷新」。"}</p>}
            </div>
            <div className="model-picker-foot">
              <button type="button" className="primary" disabled={!freshRemoteCount || busy} onClick={() => void addSelected()}>添加所选{freshRemoteCount ? `（${freshRemoteCount} 个新模型）` : ""}</button>
            </div>
          </div>}

          <div className="model-entry-list">
            <div className="model-service-list-head"><strong>模型列表（{draft.models.length}）</strong><span className="model-entry-hint">点模型行展开编辑；列表改动随「保存」或「添加所选」写回服务商</span></div>
            {!draft.models.length && <p className="empty-note">还没有模型：填好基础 URL 与 API Key 后点「获取模型列表」，或保存后手动添加。</p>}
            {draft.models.map(entry => {
              const expanded = expandedModel === entry.model;
              const level = levelFromEntry(entry);
              return <div key={entry.model} className={`model-entry${expanded ? " expanded" : ""}`}>
                <div className="model-entry-row">
                  <button type="button" className="model-entry-main" onClick={() => setExpandedModel(expanded ? "" : entry.model)}
                    title={entry.model}>
                    <strong>{entry.name || entry.model}</strong>
                    <span className="model-entry-id">{entry.model}</span>
                    <span className="model-picker-meta">
                      {entryBadges(entry).map(badge => <em key={badge}>{badge}</em>)}
                    </span>
                  </button>
                  <div className="model-entry-side">
                    <label className="model-entry-thinking">思考
                      <select aria-label={`${entry.model} 思考水平`} value={level} onChange={event => changeModelThinking(entry, event.target.value as ServiceThinkingLevel)}>
                        {SERVICE_LEVEL_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
                      </select>
                    </label>
                    <button type="button" className="danger-text" onClick={() => removeModel(entry.model)} disabled={busy}>删除</button>
                  </div>
                </div>
                {expanded && <div className="model-entry-editor">
                  <p className="empty-note">思考水平说明：{LEVEL_HINTS[level]}{entry.reasoning_style === "qwen_thinking" ? " 开关型协议下「低/中/高」都表示开启思考。" : ""}</p>
                  {(entry.reasoning_style === "qwen_thinking" || (entry.reasoning_budget ?? 0) > 0) && (
                    <label>思考预算（token，0 = 按服务默认）<input type="number" min="0" step="1024" value={entry.reasoning_budget ?? 0} onChange={event => updateModel(entry.model, { reasoning_budget: Math.max(0, Number(event.target.value) || 0) })}/></label>
                  )}
                  <div className="model-form-row">
                    <label>上下文（tokens）<input type="number" min="0" step="1024" value={entry.context_window ?? 0} onChange={event => updateModel(entry.model, { context_window: Math.max(0, Number(event.target.value) || 0) })}/></label>
                    <label>最大输出<input type="number" min="256" step="256" value={entry.max_output_tokens ?? 16384} onChange={event => updateModel(entry.model, { max_output_tokens: Math.max(256, Number(event.target.value) || 16384) })}/></label>
                  </div>
                  <p className="empty-note">上下文留空按 200K 保守默认；从模型列表选择会自动填入。</p>
                  <div className="model-form-row">
                    <label>温度<input type="number" min="0" max="2" step="0.1" value={entry.temperature ?? 0.2} onChange={event => updateModel(entry.model, { temperature: Number(event.target.value) })}/></label>
                    <label>思考协议
                      <select value={CONCRETE_PROTOCOLS.includes(entry.reasoning_style ?? "") ? entry.reasoning_style : "auto"} onChange={event => changeModelProtocol(entry, event.target.value)}>
                        {PROTOCOL_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
                      </select>
                    </label>
                  </div>
                  <div className="model-form-row">
                    <label>输入单价（USD / 百万 tokens）<input type="number" min="0" step="0.01" value={entry.input_price_per_million ?? ""} onChange={event => updateModel(entry.model, { input_price_per_million: event.target.value ? Number(event.target.value) : null })}/></label>
                    <label>输出单价（USD / 百万 tokens）<input type="number" min="0" step="0.01" value={entry.output_price_per_million ?? ""} onChange={event => updateModel(entry.model, { output_price_per_million: event.target.value ? Number(event.target.value) : null })}/></label>
                  </div>
                  <p className="empty-note">「自动识别」覆盖不到中转站改名模型时，在这里显式指定协议；一般情况不需要改。</p>
                </div>}
              </div>;
            })}
          </div>

          <div className="model-form-actions">
            {!isNew && <button type="button" className="danger-text" onClick={remove} disabled={busy}>删除服务商</button>}
            <button type="submit" className="primary" disabled={busy}>{busy ? "保存中…" : isNew ? "创建服务商" : "保存"}</button>
          </div>
          {checkResult && <p className={`model-form-status${statusError ? " is-error" : ""}`} role={statusError ? "alert" : "status"}>{checkResult}</p>}
        </form>
    </div>
  </div>;
}
