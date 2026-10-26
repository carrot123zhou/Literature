/** 阅读模块（产品前端）：三栏对照 + TextFold 五层 + 独立批注层 + 统一问答。 */
export { default as ReaderWorkspace } from "./ReaderWorkspace";
export { default as ReaderSettings } from "./ReaderSettings";
export { default as ModelPicker } from "./ModelPicker";
export { readerApi } from "./readerApi";
export type {
  ReaderAnnotation, ReaderAskResult, ReaderChatMessage, ReaderCitation, ReaderColor, ReaderFoldSection,
  ReaderFoldUnit, ReaderLayers, ReaderModelBinding, ReaderModelRoles, ReaderOverlay, ReaderPane,
  ReaderPrefs, ReaderState,
} from "./types";
