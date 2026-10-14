import type { ModelService } from "../types";

/** 功能页里的模型绑定行：显示「服务商名 · 模型ID」、密钥状态，并打开模型服务设置。 */
export default function ModelServiceField({ label, service, model, onConfigure }: { label: string; service?: ModelService; model?: string; onConfigure: () => void }) {
  const modelLabel = model || (service?.models.length === 1 ? service.models[0].model : "");
  return <div className="model-service-field">
    <span className={`model-dot${service?.configured ? " configured" : ""}`}/>
    <span className="model-service-field-text">
      <strong>{service ? service.name : "未配置模型"}</strong>
      <small>{service ? (modelLabel || "未选择模型，点击右侧按钮配置") : "点击右侧按钮添加模型服务商"}</small>
    </span>
    <button className="link-button" onClick={onConfigure}>配置</button>
  </div>;
}
