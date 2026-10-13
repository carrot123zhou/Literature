import { useEffect, useMemo, useState } from "react";
import type { ModelRole, ModelService } from "../types";
import type { ModelServicePayload, ModelServicesApi } from "../modelServices";

const PRESETS: Record<string, { label: string; base_url: string; model: string }> = {
  openai: { label: "OpenAI", base_url: "https://api.openai.com/v1", model: "gpt-5-mini" },
  deepseek: { label: "DeepSeek", base_url: "https://api.deepseek.com", model: "deepseek-chat" },
  glm: { label: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4.5" },
  qwen: { label: "Qwen / 阿里云百炼", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus" },
  custom: { label: "自定义中转站 / OpenAI 兼容", base_url: "", model: "" },
};

type Draft = ModelServicePayload & { service_id: string };

const REASONING_STYLES: { value: string; label: string }[] = [
  { value: "", label: "不设置（跟随服务默认）" },
  { value: "openai_effort", label: "OpenAI · reasoning_effort" },
  { value: "qwen_thinking", label: "Qwen · enable_thinking" },
  { value: "glm_thinking", label: "智谱 GLM · thinking" },
];

function emptyDraft(): Draft {
  return { service_id: "", name: "", preset: "custom", base_url: "", model: "", temperature: 0.2, max_output_tokens: 8192, input_price_per_million: null, output_price_per_million: null, reasoning_style: "", reasoning_effort: "", reasoning_budget: 0 };
}
function draftFrom(service: ModelService): Draft {
  return {
    service_id: service.service_id, name: service.name, preset: service.preset, base_url: service.base_url, model: service.model,
    temperature: service.temperature, max_output_tokens: service.max_output_tokens,
    input_price_per_million: service.input_price_per_million ?? null,
    output_price_per_million: service.output_price_per_million ?? null,
    reasoning_style: service.reasoning_style ?? "",
    reasoning_effort: service.reasoning_effort ?? "",
    reasoning_budget: service.reasoning_budget ?? 0,
  };
}

/** 模型服务库：管理多个服务（厂商预设或自定义中转站）并绑定到任务角色。 */
export default function ModelServicesDialog({ api, role, close, onMessage }: { api: ModelServicesApi; role: ModelRole; close: () => void; onMessage: (value: string) => void }) {
  const bound = api.serviceFor(role);
  const [draft, setDraft] = useState<Draft>(() => bound ? draftFrom(bound) : emptyDraft());
  const [keyInput, setKeyInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [listing, setListing] = useState(false);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [checkResult, setCheckResult] = useState("");

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") close(); };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [close]);

  const isNew = !draft.service_id;
  const selected = useMemo(() => api.services.find(item => item.service_id === draft.service_id), [api.services, draft.service_id]);

  const selectService = (service: ModelService) => { setDraft(draftFrom(service)); setKeyInput(""); setCheckResult(""); setModelOptions([]); };

  const changePreset = (preset: string) => {
    const presetValue = PRESETS[preset];
    setModelOptions([]);
    setDraft(current => ({
      ...current, preset,
      name: current.name || presetValue.label,
      base_url: preset === "custom" ? current.base_url : presetValue.base_url,
      model: preset === "custom" ? current.model : presetValue.model,
    }));
  };

  const save = async () => {
    if (!draft.name.trim() || !draft.model.trim() || !draft.base_url.trim()) { setCheckResult("请填写服务名称、Base URL 与模型 ID。"); return; }
    setBusy(true);
    try {
      const payload: ModelServicePayload = { ...draft };
      const saved = isNew ? await api.create(payload) : await api.update(draft.service_id, payload);
      setDraft(draftFrom(saved));
      await api.setRoles({ [role]: saved.service_id } as Partial<Record<ModelRole, string>>);
      onMessage(`模型服务已保存：${saved.name}`);
    } catch (error) { setCheckResult(String(error)); }
    finally { setBusy(false); }
  };

  const remove = async () => {
    if (isNew) { setDraft(emptyDraft()); return; }
    setBusy(true);
    try { await api.remove(draft.service_id); setDraft(emptyDraft()); onMessage("模型服务已删除"); }
    catch (error) { setCheckResult(String(error)); }
    finally { setBusy(false); }
  };

  const saveKey = async () => {
    if (isNew) { setCheckResult("请先保存服务，再填写 API Key。"); return; }
    if (!keyInput.trim()) return;
    setBusy(true);
    try { await api.saveKey(draft.service_id, keyInput); setKeyInput(""); onMessage("API Key 已保存到 macOS Keychain"); }
    catch (error) { setCheckResult(String(error)); }
    finally { setBusy(false); }
  };

  const fetchModels = async () => {
    if (isNew) { setCheckResult("请先保存服务，再拉取模型列表。"); return; }
    setListing(true);
    setCheckResult("");
    try {
      const list = await api.listModels(draft.service_id);
      setModelOptions(list);
      setCheckResult(`已获取 ${list.length} 个模型，点击模型 ID 输入框即可选择。`);
    } catch (error) { setCheckResult(String(error)); }
    finally { setListing(false); }
  };

  const check = async () => {
    if (isNew) { setCheckResult("请先保存服务，再测试连接。"); return; }
    setChecking(true);
    setCheckResult("");
    try {
      const result = await api.check(draft.service_id);
      setCheckResult(`连接成功 · ${result.model} · ${result.latency_ms}ms`);
    } catch (error) { setCheckResult(String(error)); }
    finally { setChecking(false); }
  };

  return <div className="modal-backdrop model-services-backdrop" onClick={close}>
    <section className="model-services-dialog" role="dialog" aria-modal="true" aria-label="模型服务" onClick={event => event.stopPropagation()}>
      <header>
        <div><h2>模型服务</h2><p>集中管理模型服务（厂商预设或自定义中转站），再分配给「结构校对」与「翻译」两个任务角色。API Key 只保存在本机 Keychain。</p></div>
        <button aria-label="关闭模型服务设置" onClick={close}>×</button>
      </header>

      <div className="model-role-row">
        {(["structure", "translation"] as ModelRole[]).map(item => <label key={item}>{item === "structure" ? "结构校对模型" : "翻译模型"}
          <select value={api.roles[item]} onChange={event => api.setRoles({ [item]: event.target.value } as Partial<Record<ModelRole, string>>).catch(error => onMessage(String(error)))}>
            <option value="">未绑定</option>
            {api.services.map(service => <option key={service.service_id} value={service.service_id}>{service.name} · {service.model}</option>)}
          </select>
        </label>)}
      </div>

      <div className="model-services-body">
        <aside className="model-service-list">
          <div className="model-service-list-head"><strong>服务列表</strong><button onClick={() => { setDraft(emptyDraft()); setKeyInput(""); setCheckResult(""); setModelOptions([]); }}>+ 新增</button></div>
          {api.services.length === 0 && <p className="empty-note">还没有模型服务。点击「+ 新增」开始配置。</p>}
          {api.services.map(service => <button key={service.service_id} className={`model-service-item${service.service_id === draft.service_id ? " selected" : ""}`} onClick={() => selectService(service)}>
            <span className={`model-dot${service.configured ? " configured" : ""}`}/>
            <span><strong>{service.name}</strong><small>{service.model}</small></span>
            <em>{Object.entries(api.roles).filter(([, id]) => id === service.service_id).length ? "已绑定" : ""}</em>
          </button>)}
        </aside>

        <form className="model-service-form" onSubmit={event => { event.preventDefault(); void save(); }}>
          <label>服务名称<input value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="例如：我的中转站"/></label>
          <label>类型<select value={draft.preset} onChange={event => changePreset(event.target.value)}>{Object.entries(PRESETS).map(([key, value]) => <option key={key} value={key}>{value.label}</option>)}</select></label>
          <label>Base URL<input value={draft.base_url} onChange={event => setDraft({ ...draft, base_url: event.target.value })} placeholder="https://your-relay.example.com/v1"/></label>
          <label>模型 ID
            <div className="model-input-row">
              <input list="model-id-options" value={draft.model} onChange={event => setDraft({ ...draft, model: event.target.value })} placeholder="gpt-4o-mini"/>
              <button type="button" onClick={fetchModels} disabled={listing || isNew}>{listing ? "拉取中…" : "拉取模型"}</button>
            </div>
            <datalist id="model-id-options">{modelOptions.map(id => <option key={id} value={id}/>)}</datalist>
          </label>
          <div className="model-form-row">
            <label>温度<input type="number" min="0" max="2" step="0.1" value={draft.temperature ?? 0.2} onChange={event => setDraft({ ...draft, temperature: Number(event.target.value) })}/></label>
            <label>最大输出 token<input type="number" min="256" step="256" value={draft.max_output_tokens ?? 8192} onChange={event => setDraft({ ...draft, max_output_tokens: Number(event.target.value) })}/></label>
          </div>
          <div className="model-form-row">
            <label>输入单价（USD/百万）<input type="number" min="0" step="0.01" value={draft.input_price_per_million ?? ""} onChange={event => setDraft({ ...draft, input_price_per_million: event.target.value ? Number(event.target.value) : null })}/></label>
            <label>输出单价（USD/百万）<input type="number" min="0" step="0.01" value={draft.output_price_per_million ?? ""} onChange={event => setDraft({ ...draft, output_price_per_million: event.target.value ? Number(event.target.value) : null })}/></label>
          </div>
          <label>思考模式协议
            <select value={draft.reasoning_style ?? ""} onChange={event => setDraft({ ...draft, reasoning_style: event.target.value, reasoning_effort: "", reasoning_budget: 0 })}>
              {REASONING_STYLES.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select>
          </label>
          {draft.reasoning_style === "openai_effort" && (
            <label>思考强度
              <select value={draft.reasoning_effort ?? ""} onChange={event => setDraft({ ...draft, reasoning_effort: event.target.value })}>
                <option value="">不设置</option>
                <option value="minimal">minimal</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
              </select>
            </label>
          )}
          {(draft.reasoning_style === "qwen_thinking" || draft.reasoning_style === "glm_thinking") && (
            <label>思考开关
              <select value={draft.reasoning_effort ?? ""} onChange={event => setDraft({ ...draft, reasoning_effort: event.target.value })}>
                <option value="">不设置</option>
                <option value="off">关闭（翻译任务推荐）</option>
                <option value="on">开启</option>
              </select>
            </label>
          )}
          {draft.reasoning_style === "qwen_thinking" && (
            <label>思考预算（token，0 为沿用服务默认）<input type="number" min="0" step="1024" value={draft.reasoning_budget ?? 0} onChange={event => setDraft({ ...draft, reasoning_budget: Math.max(0, Number(event.target.value) || 0) })}/></label>
          )}
          {draft.reasoning_style && <p className="empty-note">用量里 reasoning 占比很高时，关闭思考模式可能降低耗时与费用；请对照结果检查质量。</p>}
          <label>API Key<input type="password" value={keyInput} onChange={event => setKeyInput(event.target.value)} placeholder={selected?.configured ? "已保存，留空则不变" : "只保存到 macOS Keychain"}/></label>
          <div className="model-form-actions">
            <button type="button" onClick={saveKey} disabled={busy || isNew || !keyInput.trim()}>保存密钥</button>
            <button type="button" onClick={check} disabled={checking || isNew}>{checking ? "测试中…" : "测试连接"}</button>
            {!isNew && <button type="button" className="danger-text" onClick={remove} disabled={busy}>删除服务</button>}
            <button type="submit" className="primary" disabled={busy}>{busy ? "保存中…" : isNew ? "创建并绑定" : "保存"}</button>
          </div>
          {checkResult && <p className="model-form-status">{checkResult}</p>}
        </form>
      </div>
    </section>
  </div>;
}
