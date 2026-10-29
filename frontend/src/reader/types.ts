import type { Job } from "../types";

/** 阅读模块 DTO：与 literature/reader.py + /projects/{id}/reader/* API 一一对应。 */

export type ReaderColor = "y" | "g" | "b" | "p" | "o";
export type ReaderPane = "source" | "trans";

/** 一条批注：字符偏移 + quote 三元组锚定（后端 sanitize_overlay 同构）。 */
export type ReaderAnnotation = {
  id: string;
  /** `range`：有精确字符范围；`block`：整段/整节锚点（AI 摘要没有逐字范围时用它，不伪造区间）。 */
  anchor?: "range" | "block";
  block_id: string;
  pane: ReaderPane;
  start: number;
  end: number;
  quote: { exact: string; prefix: string; suffix: string };
  color: ReaderColor;
  note: string;
  /** 已移除高亮但保留笔记的纯笔记记录（不再产生 mark/tint，页边条保留）。 */
  detached?: boolean;
  /**
   * 上游内核原生锚（textfold/notes v1 anchor 的逐字快照，含 shown/level/tokens/sents/lang/quote），
   * 用于同产物同 locale 重开时精确复原短层写行/派生空间划词。结构：
   * { version: 1, locale, space: "full"|"brief"|"derived", digest, thread?, anchor }。
   * digest 为该锚索引空间 token 文本指纹：产物重折后不一致 → 锚失效（保留笔记回退 block+quote），不错标其他词。
   */
  engine_anchor?: {
    version: 1;
    locale: string;
    /** token 空间（数组名非 UI 层名）：full=全文 toks；brief=lad.brief 写行；derived=derived_brief；
     *  summary=章节摘要写行（chunk.lad.take）；title=章节标题纯文本（无 token 流）。 */
    space: "full" | "brief" | "derived" | "summary" | "title";
    digest: string;
    thread?: unknown[];
    anchor: Record<string, unknown>;
  };
  created_at: string;
  updated_at: string;
};

export type ReaderPrefs = {
  panes: { origin: boolean; source: boolean; trans: boolean };
  /** 三栏像素权重（origin/source/trans），拖宽只改相邻两栏。 */
  weights: number[];
  /** TextFold 层级：1 全文 · 2 简述(作者原句) · 3 要点(AI) · 4 主线(AI) · 5 主题(AI)。 */
  level: number;
  appearance: "light" | "paper" | "dark";
  font: "serif" | "sans";
  fontSize: number;
  lineHeight: "1.7" | "1.9" | "2.1";
  follow: boolean;
  sync: boolean;
};

export type ReaderOverlay = {
  version: number;
  revision: number;
  updated_at: string;
  annotations: ReaderAnnotation[];
  prefs: ReaderPrefs;
};

/** 五层折叠章节（TextFold textfold/doc version 0 子集；unit id = 稳定 block_id）。 */
export type ReaderFoldUnit = {
  id: string;
  block_id: string;
  full: string;
  sentences: string[];
  /** 句子下标（0 起），brief 由它在后端本地拼回（摘句逐字来自原文）。 */
  evidence?: number[];
  brief?: string[];
  brief_text?: string;
  takeaway?: string;
  key?: string;
  topic?: string;
  role?: string;
  /** 本层内容来源：model=模型写出；local=历史摘句缓存；pending=AI未生成；suppressed=确定性抑制。 */
  source?: "model" | "local" | "pending" | "suppressed";
  suppressed?: boolean;
  /** 译文缺失回退原文时为 true。 */
  fallback?: boolean;
  /**
   * 该单元（本语言 lane）AI 短层未生成（部分失败）：所有层保留全文/已有全文译文，
   * 仅加「未生成」标记；native 产物同名字段由内核补丁消费。缺失/false = 正常。
   */
  lit_pending?: boolean;
  /**
   * 短层（brief/takeaway）的真实来源，adapter 逐 unit 写入：
   * - `derived`：由原文 AI 层派生的分层译句（不是已存译文的逐字摘录）；
   * - `translation-local`：从用户已有译文里本地摘句（未调模型）；
   * - `source-fallback`：该段无译文，短层回退到原文。
   * 顶层 `provenance.strategy === "derive"` 只说明调用类型，不能代表每个 unit。
   */
  brief_source?: "derived" | "translation-local" | "source-fallback";
};

export type ReaderFoldSection = {
  id: string;
  title: string;
  level: number;
  block_id: string;
  page_index: number;
  takeaway?: string;
  topic?: string;
  /** 本节聚合来源：model / local / suppressed / mixed（混合时不能整体标 AI）。 */
  source?: "model" | "local" | "pending" | "suppressed" | "mixed";
  /** 本章摘要 AI 未生成（lit_summary_pending）：标题保留，摘要行不显示；段落全文/要点照常可用。 */
  lit_summary_pending?: boolean;
  units: ReaderFoldUnit[];
};

export type ReaderProvenance = {
  made_with?: string;
  textfold?: string;
  textfold_commit?: string;
  exported?: string;
  ai_generated?: boolean;
  model?: string;
  local?: boolean;
  /** local=本次没走模型；model=全部模型写出；mixed=两者都有。 */
  content?: "local" | "model" | "mixed";
  ai_units?: number;
  local_units?: number;
  prompt_version?: number;
  partial?: boolean;
  levels?: string[];
};

export type ReaderLayers = {
  failed?: Array<{ id: string; error: string }>;
  format: string;
  version: number;
  project_id: string;
  locale: string;
  title: string;
  levels: string[];
  written: string[];
  sections: ReaderFoldSection[];
  provenance: ReaderProvenance;
  /** 章节摘要与当前 document/translation 不一致（补译/校对后需要重折叠）。 */
  stale: boolean;
  generated: boolean;
};

/**
 * 整篇折叠 run 的可选实时活动（`GET /projects/{id}/reader/fold-run` 的 `activity`）。
 *
 * 后端只在**模型请求边界**写这份证据（stage/section/batch/时间戳）；旧 run、刚启动、
 * 或 run 已结束时该字段可能缺失，界面必须当作「没有证据」：只显示状态文本，
 * 不编造阶段、不把缺失的 elapsed/pending 当 0。时间戳后端写 ISO 字符串或 epoch。
 */
export type ReaderFoldActivity = {
  /** 公开阶段值：`waiting` 等首个字节 / `generating` 模型生成中 / `validating` 结构校验 /
   *  `saving` 落盘 / `source` 原文折叠 / `derive` 译文派生 / `apparatus` 书目索引等装置识别。
   *  内部别名由后端归一，不会再出现；未知值仍按原样显示（不吞成空白）。 */
  stage?: string;
  /** provider 事件里的同义字段（`waiting`/`generating`）：stage 缺失时回退使用，不单独成阶段。 */
  state?: string;
  /** 当前节 id（与 section_index / 折叠层同一来源）。 */
  section_id?: string;
  /** 当前节内请求批次：`batch_index` **0 起**（界面按「第 1 批」呈现），`batch_total` 为该节批数。 */
  batch_index?: number;
  batch_total?: number;
  /** 在途请求发出时刻：界面据它本地计时，等待期间也不冻结。 */
  request_started_at?: string | number | null;
  /** 最近一次模型响应时刻：请求之间用它显示「已等」。 */
  last_response_at?: string | number | null;
  /** 后端自报的已用秒数（时间戳缺失时的回退证据）。 */
  elapsed_seconds?: number;
  /** **当前节/当前语言 lane 仍待补**的 unit 数（不是全库剩余量，也不是批大小）。 */
  pending_units?: number;
  /** 非空表示该 run 内发生过重试（如超时/格式错误），原因如实显示为「曾重试」。 */
  retry_reason?: string;
};

/** 阅读角色的有效绑定与来源（后端 role_state；不要靠空 dict 推断）。 */
export type ReaderRoleResolution = {
  role: string;
  origin: "bound" | "unset" | "cleared" | "invalid";
  source: string;
  binding: { service_id: string; model: string };
  problem: string;
};

/**
 * 全局角色真状态（后端 `GET /model-services` 的 `role_states`，或 `GET /model-services/roles/state`）。
 * 四个角色都有：structure / translation / reader_fold / reader_chat。
 */
export type RoleState = {
  role: string;
  /** `bound` 自有绑定 / `unset` 配置里从没写过 / `cleared` 显式清空 / `invalid` 服务或模型已失效。 */
  origin: "bound" | "unset" | "cleared" | "invalid";
  /**
   * `role`：用自己的绑定；
   * `inherited_translation`：**只有阅读角色且 origin=unset** 时回退翻译默认；
   * `unset` / `cleared` / `invalid`：不可用（cleared/invalid **不会**回退翻译）。
   */
  source: "role" | "inherited_translation" | "unset" | "cleared" | "invalid";
  /** 配置里真实存的绑定（不做首模型替换）。 */
  binding: { service_id: string; model: string };
  /** 实际会用的绑定（模型留空时按该服务第一个模型兜底）。 */
  effective: { service_id: string; model: string };
  /** `binding`（显式指定）/ `first_model`（未指定模型，用该服务第一个）/ 空（不可用）。 */
  model_source: "binding" | "first_model" | "";
  problem: string;
};
export type RoleStateMap = Record<string, RoleState>;

/** 阅读角色状态（与全局 RoleState 同构；保留旧名以免引用处大改）。 */
export type ReaderRoleState = RoleState;

/**
 * 章节索引（`/reader/state.section_index`）：由 core `build_sections` 从文档标题结构生成，
 * **不依赖模型、不依赖分层**。`id` / `unit_ids` 与折叠层完全同源，界面据此提供
 * 「当前章」折叠范围与目录，首次折叠不必先整书。
 */
export type ReaderSectionIndexItem = {
  id: string;
  num: string;
  title: string;
  level: number;
  /** 章节标题块（有真实 bbox，可作锚点）。 */
  block_id: string;
  page_index: number;
  /** 本节可折叠的正文块 id（paragraph/list）。 */
  unit_ids: string[];
  foldable: number;
};

export type ReaderState = {
  project_id: string;
  title: string;
  source_language: string;
  target_language: string;
  bbox_space: string;
  page_count: number;
  overlay: ReaderOverlay;
  layers: { [locale: string]: unknown };
  layers_locale: string;
  layer_job: Job | null;
  summary: Record<string, unknown>;
  /** 折叠/对话两个角色的有效绑定（含继承翻译默认的来源）。 */
  roles?: { fold: ReaderRoleState; chat: ReaderRoleState };
  /** 章节索引（无 AI 也可用；为空表示这本书确实没有标题结构）。 */
  section_index?: ReaderSectionIndexItem[];
  /** 关思考探测结果：null = 未探测/未知（不得宣称已关闭思考）。 */
  thinking?: { fold: Record<string, unknown> | null; chat: Record<string, unknown> | null };
};

export type ReaderCitation = {
  block_id: string;
  page_index: number;
  page: number;
  section: string;
  text: string;
  note: string;
};

export type ReaderAskResult = {
  answer: string;
  citations: ReaderCitation[];
  context_blocks: number;
  model: string;
  provider: string;
  usage: {
    input_tokens: number;
    output_tokens: number;
    cached_tokens: number;
    reasoning_tokens: number;
    estimated_cost: number | null;
  };
  job_id: string;
};

/** 统一聊天的一条消息；quote 是发送时冻结的一次性引用快照。 */
export type ReaderChatMessage = {
  id: string;
  role: "user" | "assistant";
  question?: string;
  /** 冻结的选段引用（原文证据 + 来源）。 */
  quote?: { block_id: string; pane: ReaderPane; snippet: string; srcText: string; sec: string } | null;
  answer?: string;
  citations?: ReaderCitation[];
  model?: string;
  state: "sending" | "done" | "error" | "stopped";
  error?: string;
};

export type ReaderModelBinding = { service_id: string; model: string };

/** 设置中阅读角色的默认模型（折叠/对话各自独立；roles API 由 developer 接线）。 */
export type ReaderModelRoles = { fold: ReaderModelBinding; chat: ReaderModelBinding };
