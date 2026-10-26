import "./reader.css";
import ModelPicker from "./ModelPicker";
import type { ModelService, ModelRoleBinding } from "../types";
import type { ReaderModelBinding, ReaderModelRoles } from "./types";
import type { RoleStateMap } from "../types";

type Props = {
  providers: ModelService[];
  /** 折叠默认模型与对话默认模型（独立；roles 持久化由 developer 接 Settings/roles API）。 */
  roles: ReaderModelRoles;
  onChange: (patch: Partial<ReaderModelRoles>) => void;
  /** 翻译角色绑定：阅读两角色未绑定时后端按它回退；UI 如实显示继承来源。 */
  translationBinding?: ModelRoleBinding;
  /** 后端解析的全局真实角色状态（origin/source/binding/effective/model_source/problem），来自 /model-services/roles/state。 */
  roleStates?: RoleStateMap;
  /** 模型服务 hook 是否已完成首次加载。 */
  modelsReady?: boolean;
  onConfigure?: () => void;
};

/**
 * 设置 → 阅读：折叠默认模型与对话默认模型，各自独立，用同一款按服务商分组的 picker。
 * 后端 reader 折叠/问答的显式 service_id+model 优先；未显式传时回退翻译角色绑定（见
 * literature/api.py `_reader_provider_factory`）。这里的绑定通过 developer 的 roles API 持久化。
 */
export default function ReaderSettings({ providers, roles, translationBinding, roleStates, modelsReady, onChange, onConfigure }: Props) {
  /** 按钮标签：从 roleState origin/source/effective 导出，cleared 明确已清空、invalid 失效、unset 才继承。 */
  const unboundLabelFor = (role: "reader_fold" | "reader_chat"): string => {
    const rs = roleStates?.[role];
    if (rs) {
      if (rs.origin === "cleared") return "已清空";
      if (rs.origin === "invalid") return "绑定已失效";
      if (rs.origin === "unset" && rs.source === "inherited_translation" && rs.effective.model) {
        return `继承翻译默认（${rs.effective.model}）`;
      }
      if (rs.origin === "unset") return "未单独绑定";
    }
    if (modelsReady === false) return "正在读取…";
    return "配置状态未就绪";
  };

  /** 生效来源说明：用后端 /model-services/roles/state 的真实 DTO（origin/source/effective/model_source/problem）。
   *  不凭空推断继承、不把 effective 冒充专用绑定、不把第一个模型当有效绑定。 */
  const effectiveHint = (role: "reader_fold" | "reader_chat"): string => {
    const rs = roleStates?.[role];
    if (modelsReady === false) return "正在读取模型配置…";
    if (!rs) return "暂时无法读取生效模型，请刷新重试";
    const svcName = providers.find(p => p.service_id === rs.effective.service_id)?.name ?? "";
    const eff = rs.effective.model ? `${rs.effective.model}${svcName ? ` · ${svcName}` : ""}` : "";
    if (rs.origin === "bound") {
      const src = rs.model_source === "first_model" ? "（默认取服务商第一个模型）" : "";
      return `已绑定：${eff}${src}`;
    }
    if (rs.origin === "unset" && rs.source === "inherited_translation") {
      return `继承翻译默认：${eff || "翻译绑定也不可用"}`;
    }
    if (rs.origin === "cleared") return "已显式清空：阅读不使用模型（可重新绑定）";
    if (rs.origin === "invalid") return `绑定已失效：${rs.problem || "服务或模型已删除"}（请重新绑定）`;
    return rs.problem || "未知状态";
  };
  return (
    <section className="rdrr-settings" aria-label="阅读设置">
      <div className="rdrr-settings-row">
        <div className="rdrr-settings-info">
          <div className="rdrr-settings-title">折叠默认模型</div>
          <div className="rdrr-settings-desc">TextFold 五层折叠（要点/主线/主题）使用的默认模型；也可用「本地摘句」不调模型。</div>
        </div>
        <ModelPicker
          label="折叠默认模型"
          providers={providers}
          value={roles.fold}
          unboundLabel={unboundLabelFor("reader_fold")}
          onChange={binding => onChange({ fold: binding })}
        />
        <div className="rdrr-settings-effective">{effectiveHint("reader_fold")}</div>
      </div>
      <div className="rdrr-settings-row">
        <div className="rdrr-settings-info">
          <div className="rdrr-settings-title">对话默认模型</div>
          <div className="rdrr-settings-desc">阅读问答的默认模型；聊天输入区旁可临时切换，仅影响下一条消息。</div>
        </div>
        <ModelPicker
          label="对话默认模型"
          providers={providers}
          value={roles.chat}
          unboundLabel={unboundLabelFor("reader_chat")}
          onChange={binding => onChange({ chat: binding })}
        />
        <div className="rdrr-settings-effective">{effectiveHint("reader_chat")}</div>
      </div>
      {onConfigure && (
        <button type="button" className="rdrr-settings-config" onClick={onConfigure}>
          配置服务商与模型
        </button>
      )}
      {!providers.length && (
        <div className="rdrr-settings-empty">尚未配置任何模型服务商：阅读问答与 AI 折叠在绑定前不可用，本地摘句折叠不受影响。</div>
      )}
      <div className="rdrr-settings-note">
        阅读折叠与问答共用后端密钥链与网络安全策略（逐服务 allow_plaintext_http、TLS 校验）；API Key 不进入前端。
      </div>
    </section>
  );
}

export type { ReaderModelBinding };
