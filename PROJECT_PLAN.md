# Literature 总体计划

> 本文件是新对话、上下文压缩或新 agent 接手后的第一入口。最后更新：2026-09-30。

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

1. 在首页拖入或通过系统文件选择器批量选择文档。
2. 查看文件类型、大小和校验结果；添加文件后在弹窗中逐个设置 PDF 页码范围和额外输出格式，页码留空默认解析全部。
3. 首页先显示浏览器到 Literature 的上传状态，再显示当前 MinerU 任务总进度。
4. 在第二页“文件资源”统一查看等待中、执行中、失败和已完成的全部文档任务与资源。
5. 多分段文档默认显示总进度，需要时展开每段页码、状态与错误。
6. 标准化资源生成后进入结构校对，确认目录和标题层级。
7. 在阅读与翻译页浏览原文、选择章节/block、冻结术语表并启动翻译。
8. 人工检查或局部润色后，从同一文档图谱导出所需格式。

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

独立仓库与项目数据目标形态：

```text
~/CdoexProject/Literature/
  literature/、frontend/、docs/、tests/  # Git 跟踪的程序与文档
  launcher/Literature.app               # Git 忽略的可重建 macOS 启动器
  library/                               # Git 忽略的长期文档项目库
    <项目名>/
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

- 独立仓库和长期项目库统一迁移到 `~/CdoexProject/Literature/`；程序源码与开发文档由 Git 跟踪，`library/` 保存原文、MinerU raw、规范文档、译文和导出并被 Git 永久忽略，默认运行不再访问 macOS“文稿”目录。
- macOS 一键 WebUI 启动器位于仓库的 `launcher/Literature.app`，桌面只保留 Finder 替身；应用包按自身位置解析仓库根目录，不依赖 `/Applications`、用户名或旧源码路径。启动器会复用已运行的本地服务，或自动启动 API 与 Vite，等待就绪后在默认浏览器打开首页。
- Python 虚拟环境已在新仓库中用非 Documents 的基础解释器重建；启动器会跳过 `pyvenv.cfg` 仍绑定 `~/Documents/` 的旧环境，防止迁移残留再次触发无关的 macOS“文稿”权限。
- API 健康响应携带启动时后端源码指纹与能力标记；macOS 启动器只复用指纹完全一致的 API，并保存、校验和替换自己管理的旧 API/WebUI 进程，避免前端热更新后继续连接旧 Python 进程。
- WebUI 启动时验证逐文件 intake 能力，前后端不匹配时直接提示重新启动，不再把未知请求字段误报为“未选择支持的文件”。
- 默认首页承担文件上传、MinerU 参数设置和当前处理任务；第二页“文件资源”统一汇总执行中与已完成的标准化文档资源，资源页不提供导入入口。
- 文件资源页支持状态概览、网格/列表、资源名或原始文件名搜索、状态/类型筛选、名称/创建时间/修改时间/大小排序、分页和视图偏好保存。
- 项目支持解析前后重命名；名称同步到项目目录、`project.json` 和已有 `document.json`。删除采用可恢复移动到项目库 `.trash/`，不会直接永久抹除。
- 项目描述接口补充 `has_document`、资源数和导出数；解析未完成的项目进入任务页，存在规范文档的项目才进入查看/校对页。
- 用标准 Web 文件输入替换 Tauri 本地路径选择器，打通点击选择和拖拽上传。
- 从 PDF-only 扩展到 MinerU 官方精准解析支持的全部本地文件类型。
- 将 MinerU 的模型、语言、OCR、表格和公式作为批次设置；PDF 页码范围与额外输出格式作为逐文件设置，并分别保存到对应项目任务快照。
- 把项目源文件从固定 `origin.pdf` 改为 `origin.<原扩展名>`。
- 新项目按通用源文件工作；旧版 PDF 项目仍可继续打开和预览。
- 自动测试和 Web 浏览器交互验收已通过；除开发日志明确记录的极小 HTML 意外调用外，尚未执行计划中的真实多格式 MinerU 验收。

2026-09-30 重建「对照阅读 + 结构调整」模块（对应 M2 前半段）：

- 修正 bbox 坐标契约错误：MinerU `content_list` / `content_list_v2` 的 bbox 是 0-1000 归一化坐标，旧实现按 PDF 点单位换算，覆盖层偏离原页。`Document.bbox_space` 现在显式声明单位，旧文档缺字段时按 0-1000 解释，无需数据迁移。
- 修正页面几何来源：真实精准解析输出没有 `*_middle.json`，页面尺寸在 `layout.json` 的 `pdf_info[].page_size`。规范化现在优先 middle.json、回退 layout.json，`pages[].width/height` 不再恒为 0。
- 原页预览从单页 canvas 升级为连续滚动 + 附近页虚拟化：未渲染页用页面几何占位撑住滚动条，长文档滚动位置稳定；覆盖层用独立绝对定位层，canvas 按 devicePixelRatio 渲染。
- 三栏共享同一 active block 语义：点目录/结构块/bbox 双向定位并高亮；PDF 滚动停止后判定当前阅读块，结果栏可跟随；↑↓/jk 按阅读顺序跳转。
- 结构调整补 `set_title` 操作，修复「正文→标题」不可逆的问题（`set_level` 撤销只还原层级、不还原类型）；前端支持每块 H1-H6、降为正文、多选批量、修订历史逐项与批量撤销。
- 非 PDF 对照策略：图片与 HTML 经会话令牌取回后内嵌预览；Office 类明确说明没有浏览器内渲染器并降级为结构块/Markdown 对照；新增只读的 MinerU 原始输出列表与预览（路径限制在 `ocr/raw/mineru/` 内）。
- 前端拆出 `frontend/src/review/`（geometry / usePdfDocument / PdfPageCanvas / PdfPane / ResultPane / OutlinePane / SourcePreview / ReviewWorkspace），`App.tsx` 只保留项目、译文、导出与设置状态。
- `/health` 增加 `structure_review_v2` 能力标记，前端启动时同时校验 `intake_items_v1` 与该标记，避免旧后端被静默复用。
- 修复扫描件原页预览白页：PDF.js v5 的 JBIG2 / JPEG2000 / QCMS 解码器是 WASM，旧代码没有传 `wasmUrl`，导致 JBIG2 扫描书（如《The White Lotus Teachings》）只渲染白页且不报错。新增 `frontend/public/pdfjs/{wasm,iccs}` 与 `frontend/src/pdfAssets.ts`，统一 worker 和解码器资源路径，`usePdfDocument`、`PdfPage`、`ImportWorkspace` 复用。

2026-09-30 统一主导航外壳：结构校对 / 阅读与翻译 / 导出 并入主 WebUI 标签子页面：

- `HomeTopbar.tsx` 成为唯一页面顶栏。打开项目时「结构校对 / 阅读与翻译 / 导出」变为主导航内可激活的标签子页面（active 态沿用 `intake-topbar nav button.active`），未打开项目时保持禁用；旧的独立 `.topbar` 顶栏渲染已移除。`App.tsx` 打开项目后（含 document 未生成的「资源仍在处理中」空态）改用统一顶栏，首页 / 文件资源返回、项目标题与语言对、Token·费用/预算、设置入口、底部状态栏与三块工作区内容全部保留。
- `styles.css` 新增 `project-topbar` 修饰样式。`.home-topbar.project-topbar nav button:nth-last-child(-n+3)`（特异性 0,3,2）覆盖 `mineru.css` 900px 断点的隐藏规则（0,2,2），项目工作区在窄视口下 5 个标签仍可见；`mineru.css` 未改动，首页 / 资源页窄屏隐藏禁用标签的原设计保持不变。
- 已验证（已编码 + 自动检查）：`tsc --noEmit` 通过、Vite 生产构建通过、`pytest` 41 passed 无回归、dev server `127.0.0.1:1423` 已热更新。
- 尚未真实浏览器验收，且遗留拆解见 `docs/PLAN-工作区整合到主WebUI.md`：窄视口第二条冲突未修——`mineru.css:415` 的 `.home-topbar nav{min-width:250px}` 与 `styles.css` 的 `.project-topbar nav{min-width:560px}` 特异性相同（均 0,1,1）且源顺序靠后（产物 offset 6620 vs 39698），≤900px 时项目态 nav 保底宽度被压回 250px，5 个标签是否挤压换行未实测；另有未打开项目时标签无提示（G1）、资源页无全局设置入口（G2）、无 URL 导致刷新回首页与后退退出项目（G3）、首页与资源页同工作区仍是两套高度模型（G4）、`App.tsx` 仍内联 6 个工作区组件（P1-6）。

2026-10-02 交互整合第二轮：可拖拽三栏、面板内滚动与跨页任务导航：

- 新增 `frontend/src/viewState.ts`：零依赖 hash 路由（`#/home`、`#/resources`、`#/p/<id>/review|read|export`）。刷新、浏览器后退/前进、复制地址都能保持同一项目同一视图；路由只写 `location.hash`，`hashchange` 是状态唯一来源，不做 state/URL 双写。
- 项目上下文与视图解耦：`literature.lastProject` 记忆最近打开的项目。在首页/文件资源浏览时顶栏仍显示项目名、语言对、Token·费用与「结构校对/阅读与翻译/导出」标签，随时点回刚打开的任务；被移出资源库的项目会自动清理记忆。
- `HomeTopbar` 是唯一顶栏（首页 → 文件资源 → 三个工作区），工作区标签始终可点：未打开项目时点击跳转文件资源并给出提示，不再渲染灰色死按钮（关闭 PLAN 的 G1）。
- 三个工作区并入同一 `.app-shell` 外壳：首页、文件资源、结构校对、阅读与翻译、导出共用同一高度模型与滚动容器，不再切换 `.intake-shell` / `.app-shell` 两套外壳（关闭 G4）。全局设置抽屉在任意页面可用；未打开项目时可配置模型密钥、单价、附加要求、MinerU Token 与 TBL（关闭 G2）。
- 左「结构与任务」栏修复被截断的缺陷：`.review-grid > .sidebar{overflow:hidden}`（0,2,0）压过 `.mineru-control-column{overflow-y:auto}`（0,1,0），导致整列无法滚动。现改为「常驻标题 + `.pane-scroll` 独立滚动」，目录、建议队列、修订历史与处理状态都能滚到底；阅读页同一处理。
- 新增 `frontend/src/SplitPane.tsx`：三栏可拖动调宽，相邻两栏有最小像素宽度夹取，双击分隔条或聚焦后按 Home 恢复默认，`←/→` 微调；比例写入 localStorage（`literature.split.review` / `literature.split.reading`），窗口缩放按比例自适应。
- 交互细节：toast 6 秒自动消失；设置抽屉支持 Esc 关闭；资源卡片标出「当前任务」并可「继续任务 →」。
- 抽出 `frontend/src/workbench/`（Settings / ExportPanel / GlossaryEditor / JobPanel / TranslationBlock / InstantPanel / ReadingWorkspace），`App.tsx` 内联组件清零，只剩统一外壳、路由与共享状态（关闭 P1-6 的结构性部分）。
- 修正 `styles.css` 的 `.project-topbar nav{min-width:560px}` 为 `.home-topbar.project-topbar nav`（特异性 0,2,0），压过 900px 断点的 250px；工作区标签在窄视口下不再被隐藏。
- 已验证：`tsc --noEmit` 通过；Vite 生产构建通过；Chrome headless 对首页、文件资源、结构校对、阅读与翻译四页截图核对，顶栏统一、左栏可滚、三栏分隔条可见。
- 尚未：真实鼠标拖动分隔条的手工验收、窄视口（≤900px）肉眼回归、后端零改动的 pytest 复跑。本轮同时关闭 PLAN 的 G1–G4、§3.2 与 P1-6；`docs/PLAN-工作区整合到主WebUI.md` 的缺口表随之失效。

2026-10-02 任务生命周期、模块空白页与模型服务库：

- 顶栏可「关闭任务」；结构校对 / 阅读与翻译 / 导出三个工作区都能在未打开任务时进入空白页，并从已有文档简列表里选择任务或去首页上传。hash 路由支持 `#/review` 等无项目工作区。
- 结构栏「目录」与另外两栏一致，改为可收起。
- 新增模型服务库：可维护多个模型服务（厂商预设或自定义中转站 base_url + key + model），并分别绑定到结构校对与翻译两个任务角色；密钥只进 Keychain。功能页内直接显示/配置本模块模型，全局设置不再配置模型。
- 结构基准可由模型完成：新增异步结构作业（本地识别目录页 → 模型提取目录条目 → 模型校对标题），界面显示进度与精简活动摘要，可一键采用模型提取的目录；`/structure/plan` 提供与实际发送一致的审计预览，正文永不发送。

尚未完成或尚未真实验收：

- 真实浏览器交互验收本轮未执行：新模块只通过 `tsc --noEmit`、Vite 生产构建、pytest 与真实 API 只读探测；JBIG2/JPEG2000 扫描件的原页渲染同样只做到资源可达，尚未肉眼验收。
- 迁移前生成的旧项目虽然 bbox 显示已正确，但 `pages[].width/height` 仍为 0，PdfPane 退回用 PDF 自身首页宽高比占位；需要一次「从封存 raw 重建规范文档」的入口才能真正补齐（本轮未实现）。
- 使用真实 MinerU Token 对多种格式进行在线 smoke test。
- 大型非 PDF 的本地自动切分；当前依赖 MinerU 转换并反馈限制。
- 真实模型上的目录校对、全书翻译与费用验收。
- 项目回收站浏览与一键恢复；当前删除数据可恢复，但恢复需在本地目录中手工完成。
- 完整响应式设计、部署、账号体系和远程多用户隔离。

## 6. 分阶段路线图

### M1：Web 导入与 MinerU 全能力接入（当前）

- 独立文件资源页：列表/网格、任务状态概览、搜索、筛选、排序、分页、重命名和可恢复删除；上传与处理入口只在首页。
- 标准多文件选择、拖拽、类型/大小校验、上传暂存和逐文件 MinerU 设置弹窗。
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
- 批次设置与每个文件独立的页码范围/额外格式分别进入持久化任务，并在 MinerU 请求测试中逐项可见。
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
