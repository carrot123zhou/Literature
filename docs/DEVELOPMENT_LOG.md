# Literature 开发日志

> 本日志记录可验证事实、架构决策和仍需外部条件完成的验收。更新代码时应同步更新本文，避免把“已编码”“已测试”和“已用真实服务验证”混为一谈。

## 2026-08-30：PDF 导入与 MinerU 分段进度工作台

### 已实现

- 将首页改为实际连接 FastAPI 的 PDF 导入与解析工作台；Tauri 支持多选和文件拖入，浏览器开发模式保留本地路径输入回退。
- 新增批量 intake API：一次提交多份 PDF，每份 PDF 独立创建 Literature 项目和持久化 MinerU 任务。
- 大型 PDF 继续使用既有 200 页上限自动分段、轮询、下载和按原始页码合并流程。
- OCR 状态接口现在返回真实总页数、已完成页数、分段总数、完成/下载/失败分段数，以及每段页码、状态、重试次数和错误。
- 前端采用渐进式披露：默认显示文档总状态；多段文档可展开详情；失败分段自动展开并提供“重试失败分段”。
- 删除误导性的 OCR 暂停 API。MinerU 远端解析阶段不显示暂停或取消；应用启动后在 Token 可用时自动恢复未完成任务的轮询和下载。
- MinerU Token 可在导入页保存到 macOS Keychain；未配置 Token 时不会把排队任务标记成正在处理。
- 第一阶段只对 PDF 声明真实支持；DOCX、PPTX 和图片需确定转换/主数据策略后再开放。

### 已验证

- Python 自动化测试：`20 passed`。
- 新增 401 页真实 PDF 回归，验证生成 200、200、1 页三个有效且无重叠的 PDF 分段。
- 新增批量 intake、格式边界、分段完成页数和“不暴露 OCR pause”接口测试。
- TypeScript 类型检查和 Vite 生产构建通过。
- `cargo check` 通过。
- 隔离环境中启动真实 FastAPI 与前端，前端成功读取后端排队任务并显示 Token、等待和任务状态。

### 尚待真实服务验收

- 当前机器尚未配置 MinerU Token，因此本轮没有向 MinerU 上传文件；在线上传、远端轮询、结果下载和合并仍需使用真实 Token 做 smoke test。

## 2026-08-30：首个可运行纵向切片

### 已确定的数据主线

Literature 只有一条主数据路径：

```text
原始 PDF
  -> MinerU 原始输出（只读、带哈希、用于溯源）
  -> 规范化与目录结构校准
  -> document/document.json + document/source.md + assets/
  -> 编辑、提取、原生结构化翻译层
  -> EPUB / DOCX / HTML / MD+JSON 派生导出，或外部 TBL 接力
```

`document.json` 是长期主数据；`source.md` 是可读投影。任何导出器都不得直接读取原始 PDF 生成成品。原 PDF 只用于 MinerU 输入和人工对照。

### 已实现

- 创建了与 Litera 平行的独立仓库 `/Users/jiangwei/Documents/ChatGPT/literature`，没有让 Literature 依赖或回写 Litera。
- 迁入并隔离了 Litera 中已验证的 MinerU 上传、分页切片、轮询、下载、断点恢复与结果合并工作流。
- 建立本地项目目录、SQLite 任务/结构修订/token 用量账本，以及不可变 MinerU raw 层和 SHA-256 校验清单。
- 支持导入 MinerU `content_list_v2.json`、旧 `content_list.json` 与 Markdown，规范化为稳定 block/cell ID、页码、bbox、阅读顺序、资源引用和保留的 raw 字段。
- 建立 `Document`、`Block`、`Cell`、`TocNode`、`StructurePatch`、`TranslationLayer`、`UsageRecord` 等核心模型。
- 支持英语、日语、法语、德语自动检测，以及项目级和章节级人工覆盖。
- 实现可逆标题降级/层级补丁。自动修改仅允许 `set_type` 与 `set_level`，阈值固定为 0.97；不自动删除、合并或改写正文。
- 实现 OpenAI、DeepSeek、智谱 GLM、Qwen/百炼和自定义 OpenAI-compatible provider；密钥通过 macOS Keychain 保存。
- 全书、选章、block、即时选段共用结构化翻译层；模型必须按 block/cell ID 回写，人工编辑自动锁定，重译默认跳过锁定项。
- 实现 HTML、MD+JSON、EPUB 3、DOCX 的原文/译文/双语派生导出；表格按 cell，图片、图注、脚注和章节锚点随图谱输出。
- TBL 仅作为外部接力：Literature 从规范图谱导出 EPUB/DOCX，再启动独立 TBL；TBL 不写入 Literature 的译文层。
- 建立 FastAPI loopback sidecar、会话令牌、CORS 边界、Tauri 外壳与 React/PDF.js 桌面界面。
- 结构校对界面采用 MinerU 当前 WebUI 的设计语言：左侧结构/任务、中间文档预览、右侧解析结果，橙色主操作、半透明白色面板、Markdown/JSON/结构块标签页和处理步骤状态。
- 阅读翻译界面支持章节复选、原译双栏、当前 block 翻译、划词/选段即时翻译、保存到译文层、人工编辑锁定与解锁。
- 结构 AI 提供完整输入审计预览，只发送确认目录、标题候选、bbox 和相邻 block 类型；服务端再次验证 block ID、操作白名单和 0.97 阈值。
- 本地目录页候选支持英、日、法、德常见目录标记、页码行密度和标题/列表结构评分；候选必须由用户确认。
- 低置信结构建议支持逐项/批量确认，补丁历史支持逐项撤销。
- 术语表可由本地规则草拟、人工编辑并冻结为翻译层快照。
- 翻译任务面板支持暂停、恢复和失败分块重试；失败 chunk 会在译文层留下 `failed` 状态。
- 用量面板支持逐次 provider/model/token/估价记录，单价可编辑；项目预算达到后会在下一分块调用前暂停。
- 非敏感 provider 设置和翻译附加要求保存在本地 WebView；附加要求会进入任务快照，但不能覆盖 block/cell ID 与结构约束。
- 增加 PyInstaller sidecar 构建脚本、Tauri `externalBin` 覆盖配置和发布态 sidecar 发现逻辑。
- TBL 命令与本地 URL 可在设置中心持久化，导出页可生成结构化交接文件并启动/查看独立 TBL；配置保存在应用支持目录而非项目主数据。
- 前端会等待本地 sidecar 健康检查，降低桌面应用启动时的 API 竞态。

### 已验证

- Python 自动化测试：`16 passed`。
- 测试覆盖 raw 哈希、稳定 ID、语言识别与覆盖、结构补丁/撤销、严格 block/cell 回写、人工锁定、即时选段保存、四种导出、API 会话令牌和 CORS。
- 特别验证：测试会先删除 `source/origin.pdf`，再成功生成四种导出，证明导出链只依赖规范文档图谱、译文层和资源。
- TypeScript 类型检查通过。
- Vite 生产构建通过；受限环境下将验证产物写入 `/private/tmp`，未覆盖仓库既有 `frontend/dist`。
- `cargo check` 通过。
- 本地浏览器对三工作区、PDF/bbox 选择、结构块/Markdown/JSON 切换和 MinerU 风格界面做过交互验收。

### 本轮发现并修正

- 修正文档叙述，明确“PDF -> EPUB/DOCX/HTML”不是 Literature 的数据流。
- 将语言设置补齐为自动检测 + 英/日/法/德人工选择，并支持标题 block 上的章节覆盖。
- 将即时选段翻译保存为 block 译文层中的附属记录，而不是破坏整块译文。
- 导出器测试新增“原 PDF 已移除仍可导出”的硬性约束。
- 结构校对页改造成更接近 MinerU WebUI 的 2:4:4 桌面三栏及解析结果标签页。
- 增加 1024px 附近窗口的响应式三栏收缩规则。

## 与原计划相比仍未完成

以下项目不能标记为真实完成：

1. 术语表已可冻结，但尚无独立版本浏览/回滚界面。
2. 翻译任务可暂停、恢复和重试失败 chunk，尚未实现可配置的 provider 并发与速率限制。
3. 用量明细、手工单价和预算暂停已完成；自动更新价格表和“预计本任务花费”预检尚未实现。
4. sidecar 构建与 Tauri bundle 路径已编码，但尚未实际安装 PyInstaller 生成二进制，也未做签名、公证和干净机器安装测试。
5. 尚未用真实 500+ 页扫描书、真实 MinerU Token 和四家真实模型密钥执行端到端验收。
6. 日文竖排、复杂跨页表格、脚注回流和超大图片资源仍需要真实样本回归。

## 下一步顺序

1. 增加术语表版本回滚、provider 并发/速率限制和任务花费预估。
2. 为 MinerU 和模型服务写可注入的 HTTP 集成测试夹具，再用用户提供的真实样本与凭据做 smoke test。
3. 实际生成 PyInstaller sidecar 与 `.app` bundle，完成签名/公证和干净 macOS 验收。

## 外部验收所需输入

- 一份代表性的 500 页以上扫描 PDF，以及英、日、法、德各至少一份小样。
- MinerU Token。
- 至少一个翻译模型 API key；四家 provider 的最终兼容性测试需要各自 key。
- TBL 的实际可执行文件路径和启动参数。
- 若发布给其他 Mac：Apple Developer ID 与公证凭据。
