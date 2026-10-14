import type { ModelService } from "../types";

/** 任务级思考档位；取值与后端 REASONING_MODES 一致，字段名与后端契约对齐，不要改。 */
export type ReasoningMode = "inherit" | "off" | "low" | "medium" | "high";

export const REASONING_MODE_OPTIONS: { value: ReasoningMode; label: string }[] = [
  { value: "inherit", label: "跟随模型配置" },
  { value: "off", label: "关闭思考" },
  { value: "low", label: "低" },
  { value: "medium", label: "中" },
  { value: "high", label: "高" },
];

/** 与后端 infer_reasoning_style 相同的保守前缀匹配，仅用于前端提示与档位回退。 */
export function inferReasoningStyleFromModel(model: string): string {
  const name = String(model || "").trim().toLowerCase();
  if (!name) return "";
  const token = name.includes("/") ? name.slice(name.lastIndexOf("/") + 1) : name;
  if (token.startsWith("qwen3") || token.startsWith("qwq") || token.includes("qwen3")) return "qwen_thinking";
  if (token.startsWith("glm-4") || token.startsWith("glm4") || token.startsWith("glm-z") || token.startsWith("glm-z1")) return "glm_thinking";
  if (token.startsWith("gpt-5") || token.startsWith("gpt-6") || token.startsWith("o1") || token.startsWith("o3") || token.startsWith("o4")) return "openai_effort";
  return "";
}

export type ReasoningPatch = { reasoningMode: ReasoningMode; reasoningBudget?: number };

type Props = {
  value: ReasoningMode;
  budget: number;
  onChange: (patch: ReasoningPatch) => void;
  /** 当前服务商（徽标行，Cindy 风格）；不传则不显示徽标行。 */
  service?: ModelService;
  /** 绑定的模型 ID；显示「服务商名 · 模型ID」。 */
  model?: string;
  /** 服务未配置时的提示文案。 */
  emptyHint?: string;
};

/** 思考水平选择（任务启动处共用）：当前模型徽标行 + 档位下拉 + 思考预算。 */
export default function ThinkingLevelField({ value, budget, onChange, service, model, emptyHint }: Props) {
  const showBudget = value === "low" || value === "medium" || value === "high";
  const modelLabel = model || (service?.models.length === 1 ? service.models[0].model : "") || "";
  return <div className="thinking-level">
    {service
      ? <div className="thinking-model-badge" title={`${service.name} · ${modelLabel || "未选择模型"}`}>
          <span className={`model-dot${service.configured ? " configured" : ""}`}/>
          <strong>{service.name}</strong>
          <small>{modelLabel || "未选择模型"}</small>
        </div>
      : (emptyHint ? <p className="thinking-level-hint">{emptyHint}</p> : null)}
    <label className="thinking-level-row">思考水平
      <select aria-label="思考水平" value={value} onChange={event => onChange({ reasoningMode: event.target.value as ReasoningMode })}>
        {REASONING_MODE_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
    </label>
    {showBudget && <label className="thinking-level-row">思考预算（token，0 = 按服务默认）
      <input aria-label="思考预算（token）" type="number" min={0} step={1024} value={budget} onChange={event => onChange({ reasoningMode: value, reasoningBudget: Math.max(0, Number(event.target.value) || 0) })}/>
    </label>}
  </div>;
}
