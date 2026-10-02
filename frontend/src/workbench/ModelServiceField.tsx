import type { ModelService } from "../types";

/** 功能页里的模型绑定行：显示当前服务、密钥状态，并打开模型服务设置。 */
export default function ModelServiceField({ label, service, onConfigure }: { label: string; service?: ModelService; onConfigure: () => void }) {
  return <div className="model-service-field">
    <span className={`model-dot${service?.configured ? " configured" : ""}`}/>
    <span className="model-service-field-text">
      <strong>{service ? service.name : "未配置模型"}</strong>
      <small>{service ? service.model : "点击右侧按钮添加模型服务"}</small>
    </span>
    <button className="link-button" onClick={onConfigure}>配置</button>
  </div>;
}
