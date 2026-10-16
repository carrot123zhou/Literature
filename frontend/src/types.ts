export type Block = {
  block_id: string; type: string; order: number; page_index: number; source_text: string;
  bbox: number[]; level: number; parent_id?: string; items: string[];
  cells: { cell_id: string; row: number; column: number; text: string }[];
  resource_refs: string[]; caption: string[]; footnote: string[]; format: Record<string, unknown>; raw: Record<string, unknown>;
};

export type TocNode = { node_id: string; title: string; level: number; block_id: string; page_index: number; children: TocNode[] };
/** 跨页续接（只读派生）：承载整段的 block → 它在下一页的续行区域。 */
export type FlowRegion = { page_index: number; bbox: number[] };
export type FlowLink = { next_region?: FlowRegion };
export type FlowLinks = Record<string, FlowLink>;
/** layout.json pdf_info 提供的页面几何；width/height 为 PDF 点，0 表示未取得。 */
export type PageMeta = { page_index: number; width: number; height: number; rotation: number; source_file: string; source_page_index: number };
export type DocumentGraph = {
  project_id: string; title: string; source_language: string; source_language_mode: string; target_language: string;
  page_count: number; pages?: PageMeta[]; blocks: Block[]; toc: TocNode[]; resources: Record<string, { path: string }>;
  chapter_language_overrides: Record<string, string>;
  /** bbox 单位契约：mineru-1000 表示 0-1000 归一化坐标；none 表示没有可用坐标。 */
  bbox_space: string;
  /** 跨页续接（只读派生）：承载整段的 block → 它在下一页的续行区域。 */
  flow_links?: FlowLinks;
  schema_version?: string;
  updated_at?: string;
  budget_usd?: number;
};
export type ProjectReviewSummary = { state: "todo" | "confirm" | "partial" | "done"; pending: number; confirmed_at?: string; confirmed_scope?: string };
/** 全文解析覆盖（canonical 已合并源页）；state 未知时不代表完成。 */
export type ParseCoverage = {
  state: "unparsed" | "partial" | "full" | "unknown";
  source_page_count: number;
  covered_pages: number;
  ranges: number[][];
};
export type ProjectTranslateSummary = {
  state: "none" | "running" | "partial" | "done" | "failed";
  done: number; total: number; failed: number; stale: number; stale_source: number;
  percent: number; progress?: number | null; locale: string; glossary_frozen: boolean;
};
/** 提取之外的三条独立工作流状态；提取仍来自 /ocr/jobs。 */
export type ProjectPipeline = {
  review: ProjectReviewSummary;
  translate: ProjectTranslateSummary;
};
export type Project = {
  project_id: string; title: string; state: string; path?: string; source_name?: string; source_type?: string;
  source_language?: string; target_language?: string; source_size?: number; created_at?: string; updated_at: string;
  has_document?: boolean; asset_count?: number; translation?: TranslationStats | null;
  review?: ProjectReviewSummary; pipeline?: ProjectPipeline; parse_coverage?: ParseCoverage | null;
};
export type TranslationEntry = { block_id: string; text: string; status: string; locked: boolean; items: string[]; caption: string[]; footnote: string[]; cells: Record<string, string>; selection_translations: {source_text:string;translated_text:string;created_at:string}[]; candidates?: Record<string, unknown>[]; updated_at?: string; state?: string; stale?: boolean; stale_reason?: "source" | "structure" | null; structure_revision?: number; error?: string; quality_warnings?: string[] };
export type TranslationStats = {
  total: number; done: number; pending: number; percent: number;
  untranslated: number; translated: number; refined: number; edited: number;
  failed: number; locked: number; stale: number; stale_locked: number;
  stale_source: number; stale_structure: number; stale_block_ids: string[];
  stale_source_unlocked?: number;
  glossary_frozen?: boolean;
};
export type TranslationLayer = { project_id: string; locale: string; entries: Record<string, TranslationEntry>; glossary_snapshot: Record<string, string>; stats?: TranslationStats; stale_block_ids?: string[]; updated_at?: string };
export type GlossaryVersion = { version: number; entries: Record<string, string>; source: string; created_at: string };
export type TranslationEstimate = { block_count: number; source_chars: number; prompt_chars?: number; estimated_input_tokens: number; estimated_output_tokens: number; estimated_input_tokens_low?: number; estimated_input_tokens_high?: number; estimated_output_tokens_low?: number; estimated_output_tokens_high?: number; estimated_cost: number | null; estimated_cost_low?: number | null; estimated_cost_high?: number | null; chunk_count: number; refine?: boolean; polish?: boolean; has_pricing?: boolean };
export type ProviderConfig = { provider: string; model: string; base_url: string; temperature: number; max_output_tokens: number; input_price_per_million?: number; output_price_per_million?: number };
export type JobActivity = { at: string; level: string; text: string };
export type Job = {
  job_id: string; kind: string; status: string; progress: number; error: string; scope: Record<string, unknown>;
  message?: string; activity?: JobActivity[]; result?: Record<string, unknown>;
  created_at?: string; updated_at?: string;
};
export type ModelEntry = {
  /** 服务商内的模型 ID（唯一键，必填）。 */
  model: string;
  name?: string;
  context_window?: number;
  max_output_tokens?: number;
  temperature?: number;
  input_price_per_million?: number | null;
  output_price_per_million?: number | null;
  reasoning_style?: string;
  reasoning_effort?: string;
  reasoning_budget?: number;
};
/** v3：一个服务商（Base URL + 密钥）及其模型列表；模型嵌在服务商里。 */
export type ModelService = {
  service_id: string; name: string; preset: string; base_url: string;
  /** 是否已确认允许对该 http 地址明文发送 API Key（内网/自建服务）。 */
  allow_plaintext_http?: boolean;
  models: ModelEntry[];
  configured: boolean;
};
/** 任务角色绑定：服务商 + 该服务商内的模型 ID。 */
export type ModelRoleBinding = { service_id: string; model: string };
export type ModelRole = "structure" | "translation";
export type ModelRoles = { structure: ModelRoleBinding; translation: ModelRoleBinding };
/** `/v1/models` 返回的一条记录：ID + 可选的上下文窗口 / 输出上限 / 单价。 */
export type RemoteModel = {
  id: string; context_window: number; max_output_tokens: number;
  input_price_per_million: number | null; output_price_per_million: number | null;
};
export type StructureTocEntry = { title: string; level: number };
export type StructureSuggestion = { block_id: string; operation: "set_type" | "set_level" | "set_title"; before: unknown; after: unknown; confidence: number; reason: string; automatic: boolean; document_updated_at?: string; job_id?: string; suggestion_id?: string; parent_id?: string | null; parent_title?: string | null };
export type StructureBaseline = { toc: StructureTocEntry[]; source: string; updated_at: string };
export type StructurePatch = StructureSuggestion & { patch_id: string; source: "ai" | "user"; reverted_at?: string; created_at: string };
export type UsageRecord = { usage_id: string; job_id: string; provider: string; model: string; scope: string; input_tokens: number; output_tokens: number; cached_tokens: number; reasoning_tokens: number; estimated_cost?: number; request_id: string; created_at: string };
export type OcrSlice = {
  index: number; start_page: number; end_page: number; page_count: number; state: string;
  remote_state: string; attempts: number; error: string; source_pages?: number[];
};
export type ParseQueueEntry = {
  job_id: string; position: number; state?: string | null; project_id?: string; title?: string; source_name?: string;
};
export type ParseQueueState = { order: string[]; entries: ParseQueueEntry[]; running: string | null };
export type OcrJob = {
  job_id: string; project_id: string; title: string; project_state: string; state: string; error: string;
  source_name: string; source_type: string; model_version: string; language: string; is_ocr: boolean;
  enable_table: boolean; enable_formula: boolean; page_ranges: string; extra_formats: string[];
  page_count: number; page_limit: number; source_size: number; slice_count: number; completed_slices: number;
  downloaded_slices: number; failed_slices: number; submitted_pages: number; completed_pages: number;
  downloaded_pages: number; stage_progress: number | null; can_retry: boolean; slices: OcrSlice[];
  source_page_count?: number; requested_pages?: number[]; requested_ranges?: string; append?: boolean;
  this_task_pages?: number; can_resume?: boolean; can_start?: boolean;
  parse_coverage?: ParseCoverage | null; queue_position?: number | null;
  created_at: string; updated_at: string;
  /** 首次进入 completed 的时间；旧任务由 updated_at 回退。 */
  finished_at?: string; finished_at_estimated?: boolean;
  archived?: boolean; archived_at?: string; archive_reason?: string;
};

export type TaskCounts = { running: number; queued: number; resumable: number; failed: number; total: number; completed?: number };
export type TaskPage = { items: OcrJob[]; total: number; page: number; page_size: number; pages: number; counts: TaskCounts };
export type ArchiveSettings = { retention_days: number | null; max_records: number };

/* ----------------------------- 导出 ----------------------------- */
export type ExportFormatId = "epub" | "docx" | "markdown" | "html";
export type ExportModeId = "source" | "translation" | "bilingual";
export type ExportFormat = { id: ExportFormatId; label: string; single_file: boolean };
export type ExportMode = { id: ExportModeId; label: string };
export type ExportTocNode = {
  node_id: string; title: string; level: number; block_id: string; page_index: number;
  block_count: number; translated_count: number; missing_count: number; children: ExportTocNode[];
};
export type ExportFile = { name: string; rel_path: string; bytes: number; single_file: boolean };
export type ExportRecord = {
  export_id: string; created_at: string; mode: string; locale: string; formats: string[];
  path: string; files: ExportFile[]; bytes: number; warnings: string[];
};
export type ExportOptions = {
  project_id: string; title: string; source_language: string; target_language: string;
  locales: string[]; default_locale: string; formats: ExportFormat[]; modes: ExportMode[];
  block_count: number; image_count: number; toc: ExportTocNode[]; recent: ExportRecord[];
};
export type ExportRequest = {
  formats: ExportFormatId[]; mode: ExportModeId; locale: string; chapters: string[];
  block_ids?: string[]; filename?: string; missing_policy?: "fallback_source";
};
