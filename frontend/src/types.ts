export type Block = {
  block_id: string; type: string; order: number; page_index: number; source_text: string;
  bbox: number[]; level: number; parent_id?: string; items: string[];
  cells: { cell_id: string; row: number; column: number; text: string }[];
  resource_refs: string[]; caption: string[]; footnote: string[]; format: Record<string, unknown>; raw: Record<string, unknown>;
};

export type TocNode = { node_id: string; title: string; level: number; block_id: string; page_index: number; children: TocNode[] };
/** layout.json pdf_info 提供的页面几何；width/height 为 PDF 点，0 表示未取得。 */
export type PageMeta = { page_index: number; width: number; height: number; rotation: number; source_file: string; source_page_index: number };
export type DocumentGraph = {
  project_id: string; title: string; source_language: string; source_language_mode: string; target_language: string;
  page_count: number; pages?: PageMeta[]; blocks: Block[]; toc: TocNode[]; resources: Record<string, { path: string }>;
  chapter_language_overrides: Record<string, string>;
  /** bbox 单位契约：mineru-1000 表示 0-1000 归一化坐标；none 表示没有可用坐标。 */
  bbox_space: string;
  schema_version?: string;
  updated_at?: string;
  budget_usd?: number;
};
export type Project = {
  project_id: string; title: string; state: string; path?: string; source_name?: string; source_type?: string;
  source_language?: string; target_language?: string; source_size?: number; created_at?: string; updated_at: string;
  has_document?: boolean; asset_count?: number; export_count?: number;
};
export type TranslationEntry = { block_id: string; text: string; status: string; locked: boolean; items: string[]; caption: string[]; footnote: string[]; cells: Record<string, string>; selection_translations: {source_text:string;translated_text:string;created_at:string}[] };
export type TranslationLayer = { project_id: string; locale: string; entries: Record<string, TranslationEntry>; glossary_snapshot: Record<string, string> };
export type ProviderConfig = { provider: string; model: string; base_url: string; temperature: number; max_output_tokens: number; input_price_per_million?: number; output_price_per_million?: number };
export type JobActivity = { at: string; level: string; text: string };
export type Job = {
  job_id: string; kind: string; status: string; progress: number; error: string; scope: Record<string, unknown>;
  message?: string; activity?: JobActivity[]; result?: Record<string, unknown>;
};
export type ModelService = {
  service_id: string; name: string; preset: string; base_url: string; model: string;
  temperature: number; max_output_tokens: number;
  input_price_per_million?: number | null; output_price_per_million?: number | null;
  configured: boolean;
};
export type ModelRole = "structure" | "translation";
export type ModelRoles = { structure: string; translation: string };
export type StructureTocEntry = { title: string; level: number };
export type StructureSuggestion = { block_id: string; operation: "set_type" | "set_level" | "set_title"; before: unknown; after: unknown; confidence: number; reason: string; automatic: boolean };
export type StructurePatch = StructureSuggestion & { patch_id: string; source: "ai" | "user"; reverted_at?: string; created_at: string };
export type UsageRecord = { usage_id: string; job_id: string; provider: string; model: string; scope: string; input_tokens: number; output_tokens: number; cached_tokens: number; reasoning_tokens: number; estimated_cost?: number; request_id: string; created_at: string };
export type OcrSlice = {
  index: number; start_page: number; end_page: number; page_count: number; state: string;
  remote_state: string; attempts: number; error: string;
};
export type OcrJob = {
  job_id: string; project_id: string; title: string; project_state: string; state: string; error: string;
  source_name: string; source_type: string; model_version: string; language: string; is_ocr: boolean;
  enable_table: boolean; enable_formula: boolean; page_ranges: string; extra_formats: string[];
  page_count: number; page_limit: number; source_size: number; slice_count: number; completed_slices: number;
  downloaded_slices: number; failed_slices: number; submitted_pages: number; completed_pages: number;
  downloaded_pages: number; stage_progress: number | null; can_retry: boolean; slices: OcrSlice[];
  created_at: string; updated_at: string;
};
