import ThinkingLevelField, { type ReasoningMode } from "./ThinkingLevelField";
import type { ModelRoleBinding, ModelService } from "../types";

export type TaskModelPatch = { serviceId?: string; model?: string; reasoningMode?: ReasoningMode; reasoningBudget?: number };

type Props = {
  label: string;
  /** 全部服务商（含各自模型列表），按「服务商 → 模型」两级渲染。 */
  providers: ModelService[];
  /** 当前绑定（默认值 = 角色绑定）。 */
  value: ModelRoleBinding;
  reasoningMode: ReasoningMode;
  reasoningBudget: number;
  /** 结构校准等零高级配置场景可关掉思考档位/预算（后端已按任务默认关思考）；翻译页不受影响。 */
  showThinking?: boolean;
  onChange: (patch: TaskModelPatch) => void;
  onConfigure?: () => void;
};

/** 任务界面的模型选择器：服务商下拉 → 模型下拉 + 思考水平（默认=角色绑定）。 */
export default function TaskModelPicker({ label, providers, value, reasoningMode, reasoningBudget, showThinking = true, onChange, onConfigure }: Props) {
  const provider = providers.find(item => item.service_id === value.service_id);
  // 绑定 model 为空时展示服务商第一个模型，与后端“空回退第一个”的语义保持一致。
  const model = value.model || provider?.models[0]?.model || "";
  const changeProvider = (serviceId: string) => {
    const next = providers.find(item => item.service_id === serviceId);
    onChange({ serviceId, model: next?.models[0]?.model ?? "" });
  };
  return <div className="task-model-picker">
    <div className="task-model-picker-head">
      <span className="task-model-picker-label">{label}</span>
      {onConfigure && <button className="link-button" onClick={onConfigure}>配置服务商与模型</button>}
    </div>
    {showThinking && <ThinkingLevelField
      value={reasoningMode}
      budget={reasoningBudget}
      service={provider}
      model={model}
      emptyHint={providers.length ? undefined : "还没有模型服务商，请先在「配置服务商与模型」中添加。"}
      onChange={patch => onChange({ reasoningMode: patch.reasoningMode, ...(patch.reasoningBudget !== undefined ? { reasoningBudget: patch.reasoningBudget } : {}) })}
    />}
    <div className="model-form-row task-model-picker-row">
      <label>服务商
        <select aria-label={`${label}服务商`} value={value.service_id} onChange={event => changeProvider(event.target.value)}>
          <option value="">未绑定</option>
          {providers.map(item => <option key={item.service_id} value={item.service_id}>{item.name}（{item.models.length} 个模型）</option>)}
        </select>
      </label>
      <label>模型
        <select aria-label={`${label}模型`} value={model} disabled={!provider} onChange={event => onChange({ model: event.target.value })}>
          {!provider && <option value="">先选服务商</option>}
          {provider?.models.map(entry => <option key={entry.model} value={entry.model}>{entry.name || entry.model}</option>)}
          {provider && !provider.models.length && <option value="">该服务商还没有模型</option>}
        </select>
      </label>
    </div>
  </div>;
}
