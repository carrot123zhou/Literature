# Literature 总体计划

> 本文件是新对话、上下文压缩或新 agent 接手后的第一入口。最后更新：2026-08-31。

## 1. 产品定义

Literature 是一个 WebUI 优先的结构化文献处理工作台。它把成熟的 MinerU 文档解析、目录结构校对、原文阅读、结构化 AI 翻译和多格式导出串成一条可恢复、可追溯的工作流。

目标用户不需要阅读代码来验收功能。每个模块都应尽早提供可交互页面、清晰状态和可复现的测试样本。

当前技术方向：

- React + TypeScript + Vite 提供 WebUI。
- Python 3.12 + FastAPI 提供本地 Web API 与后台任务。
- SQLite 和项目目录保存任务、修订、用量、原始解析结果与规范文档。
- 当前暂停 Tauri/桌面打包；保留已有 `src-tauri`，但不得让桌面能力阻塞 Web 功能。
- 旧 Litera 保持不变；Literature 是独立仓库和升级版产品。

## 2. 已确认的核心原则

### 2.1 数据分层

1. `ocr/raw/mineru/`：MinerU 返回的 Markdown、JSON、图片、布局数据等原样封存，并记录哈希；后续步骤不得覆盖。
2. `document/document.json`：规范文档图谱，保留稳定 block/cell ID、页码、bbox、阅读顺序、类型、层级、格式、资源引用和 MinerU 原始字段。
3. `translations/<locale>/translation.json`：译文按 block/cell ID 挂接，不复制或改写原文版面元数据。
4. EPUB、DOCX、HTML、MD+JSON 都是从规范图谱和译文层生成的视图，不是新的事实来源。

这样可以为后续检索、问答、双语引用和定位回原页保留基础。

### 2.2 MinerU 边界

- MinerU 是首要且当前唯一的文档解析服务；Literature 只负责输入、自动分段、上传、轮询、下载、校验、合并和规范化。
- WebUI 从一开始对齐 MinerU 精准解析 API 支持的文件类型：PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX、PNG/JPG/JPEG/JP2/WEBP/GIF/BMP、HTML。
- PDF 超过 MinerU 单文件限制时自动分段，默认每段不超过 200 页；分段进度采用渐进式展示。
- 非 PDF 由 MinerU 执行格式转换；若远端报告页数或转换限制，保留任务和明确错误，不伪装成本地已支持的转换能力。
- 远端解析不能真正暂停或取消。界面不提供误导性按钮；应用重启后恢复轮询，失败时只重试失败分段。

MinerU 官方精准解析 API 当前公开的本地批量上传设置：

- `model_version`：`vlm`、`pipeline`；HTML 自动使用 `MinerU-HTML`。
- `language`：官方语言包，不使用含糊的“自动检测”冒充 `ch`。
- `is_ocr`、`enable_table`、`enable_formula`。
- 文件级 `page_ranges`。
- `extra_formats`：`docx`、`html`、`latex`；Markdown 和 JSON 默认返回。
- `data_id` 由 Literature 自动生成，用于可靠匹配分段结果。
- 本地 WebUI 使用轮询，因此不要求用户填写公网 callback/seed；URL 专属的缓存参数不出现在本地文件上传设置中。

官方依据：[MinerU 文档解析接口](https://mineru.net/apiManage/docs)；[MinerU 输出文件说明](https://opendatalab.github.io/MinerU/reference/output_files/)。

### 2.3 结构校对

- 目录树、原页 bbox 和结构块双向定位。
- AI 只读取目录内容、标题候选和少量相邻结构元数据，不读取全文正文来做目录校对。
- 置信度至少 0.97 时，只可自动执行“标题降为正文/调整标题级别”等可逆补丁。
- 删除、合并或改写正文永不自动执行；所有修改有历史并可撤销。

### 2.4 翻译

- 全书、选章、block 和即时选段翻译共用同一结构化引擎。
- 表格按 cell 翻译；标题、图题、图注和脚注翻译；公式、图片及图片内部文字首版保持原样。
- 翻译前可草拟并冻结术语表；任务保存术语表快照。
- 人工编辑的译文自动锁定，批量重译和润色默认跳过。
- 支持 OpenAI、DeepSeek、智谱 GLM、Qwen/百炼和自定义 OpenAI-compatible 服务；记录真实 usage 和估算费用。
- TBL 作为外部独立工具通过 EPUB/DOCX 接力，不把其 AGPL 内核复制进 Literature 核心。

### 2.5 导出

- 首版输出 EPUB 3、DOCX、HTML、MD+JSON，支持校对原文、纯译文和逐段双语。
- 保持章节锚点、图片、表格和脚注；首版不生成 PDF。

## 3. 目标用户流程

1. 在 Web 页面拖入或通过系统文件选择器批量选择文档。
2. 查看文件类型、大小和校验结果，设置 MinerU 参数。
3. 点击“开始解析”，页面先显示浏览器到 Literature 的上传状态，再显示 MinerU 总进度。
4. 多分段文档默认显示总进度，需要时展开每段页码、状态与错误。
5. 解析完成后进入结构校对，确认目录和标题层级。
6. 在阅读与翻译页浏览原文、选择章节/block、冻结术语表并启动翻译。
7. 人工检查或局部润色后，从同一文档图谱导出所需格式。

## 4. 当前架构

```text
Web 浏览器
  └─ React/Vite WebUI
       └─ FastAPI（loopback + 会话令牌）
            ├─ Web 上传暂存区
            ├─ MinerU 任务管理器
            │    └─ MinerU 精准解析 API
            ├─ ProjectStore / SQLite
            ├─ raw → canonical 规范化
            ├─ 结构补丁与翻译任务
            └─ EPUB/DOCX/HTML/MD+JSON 导出
```

项目目录目标形态：

```text
~/Documents/Literature/<项目名>/
  project.json
  source/origin.<原扩展名>
  ocr/raw/mineru/
  ocr/raw/manifest.json
  document/document.json
  document/source.md
  translations/zh-CN/translation.json
  assets/
  exports/
  literature.sqlite
```

## 5. 当前开发状态

已形成的基础：

- 项目存储、SQLite、不可变 MinerU raw 层和 SHA-256 清单。
- MinerU Token 的安全保存和 Web 设置入口，不回显完整密钥。
- PDF 自动 200 页分段、批量提交、轮询、下载、断点恢复、失败分段重试和结果合并。
- 处理进度采用总进度 + 可展开分段详情。
- MinerU 输出规范化为稳定 block/cell ID、页码、bbox、目录和资源引用。
- 结构校对、可逆补丁、翻译层、术语表、用量账本以及四种导出已有首个纵向切片。

本轮已完成编码与隔离验证：

- 用标准 Web 文件输入替换 Tauri 本地路径选择器，打通点击选择和拖拽上传。
- 从 PDF-only 扩展到 MinerU 官方精准解析支持的全部本地文件类型。
- 将所有适用于本地批量上传的 MinerU 设置呈现在前端并保存到任务快照。
- 把项目源文件从固定 `origin.pdf` 改为 `origin.<原扩展名>`。
- 新项目按通用源文件工作；旧版 PDF 项目仍可继续打开和预览。
- 自动测试和 Web 浏览器交互验收已通过；尚未消耗用户 MinerU 额度做真实服务请求。

尚未完成或尚未真实验收：

- 使用真实 MinerU Token 对多种格式进行在线 smoke test。
- 非 PDF 完成后的原件/解析结果对照界面；PDF 已有 bbox 原页对照基础。
- 大型非 PDF 的本地自动切分；当前依赖 MinerU 转换并反馈限制。
- 真实模型上的目录校对、全书翻译与费用验收。
- 完整响应式设计、部署、账号体系和远程多用户隔离。

## 6. 分阶段路线图

### M1：Web 导入与 MinerU 全能力接入（当前）

- 标准多文件选择、拖拽、类型/大小校验和上传暂存。
- 官方文件类型与适用设置完整接入。
- 任务快照、进度、恢复、失败重试和 raw 封存。
- PDF 大文件自动分片；非 PDF 限制给出明确反馈。
- 使用短 PDF、图片、DOCX、PPTX、XLSX、HTML 做真实服务验收。

### M2：结构校对可用版

- PDF/bbox/结构块联动稳定化。
- 非 PDF 的可用预览策略。
- 目录页候选、AI 建议、批量确认、历史与撤销。
- 保证正文未发送给目录校对模型的审计视图。

### M3：结构化翻译可用版

- 模型设置与密钥、术语表冻结、全书/选章/block 队列。
- 校验漏块、重复块、未知 ID；失败块重试和重启恢复。
- 人工锁定、即时翻译和章节润色。
- usage/token/费用账本。

### M4：导出与交付

- EPUB、DOCX、HTML、MD+JSON 的内容和资源验证。
- 原文、译文、双语预览。
- 外部 TBL 接力。
- Web 部署与数据安全方案；桌面版是否恢复开发届时再决定。

## 7. 验收标准

- Web 文件按钮真实打开系统文件选择窗口；拖拽可加入同一队列。
- 支持的扩展名与 MinerU 官方文档一致；不支持格式在上传前明确拒绝。
- 单文件超过 200MB、批次数过多、空文件和错误页码范围有可理解反馈。
- 前端设置值进入持久化任务，并在 MinerU 请求测试中逐项可见。
- 500 页以上 PDF 分段连续、无重叠，重启后可恢复，成功分段不重复提交。
- raw 文件哈希不变，canonical/translation/export 不修改 raw。
- API 密钥不进入项目、日志、Git 或导出。
- 每项外部能力分别记录“已编码、自动测试、隔离联调、真实服务验收”四种状态。

## 8. agent 接手检查表

1. 阅读本文件与 `docs/DEVELOPMENT_LOG.md`。
2. 查看 `git status`，保留用户已有改动。
3. 确定正在推进的里程碑，不跨模块做未经授权的重构。
4. 优先用可交互 Web 页面让用户验收。
5. 修改代码后运行对应测试、类型检查和生产构建。
6. 更新本文件的“当前开发状态”和开发日志，再提交 Git。
