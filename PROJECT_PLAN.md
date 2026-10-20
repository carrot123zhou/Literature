# Literature 总体计划

> 本文件是新对话、上下文压缩或新 agent 接手后的第一入口。最后更新：2026-10-15。

## 1. 产品定义

Literature 是一个以 macOS 客户端为优先交付面的结构化文献处理工作台。它是把成熟的 MinerU 文档解析、目录结构校对、原文阅读与结构化 AI 翻译串成一条可恢复、可追溯的工作流；2026-10-13 起不再内置多格式导出与外部 TBL 接力（见 §2.5）。

目标用户不需要阅读代码来验收功能。每个模块都应尽早提供可交互页面、清晰状态和可复现的测试样本。

当前技术方向：

- React + TypeScript + Vite 提供内嵌工作区，浏览器仅用于开发调试。
- Python 3.12 + FastAPI 提供本地 Web API 与后台任务。
- SQLite 和项目目录保存任务、修订、用量、原始解析结果与规范文档。
- 2026-10-03 用户要求转为客户端：Tauri macOS 外壳和自包含 Python sidecar 成为当前交付路径，不依赖外部浏览器或开发服务器。
- 旧 Litera 保持不变；Literature 是独立仓库和升级版产品。

## 2. 已确认的核心原则

### 2.1 数据分层

1. `ocr/raw/mineru/`：MinerU 返回的 Markdown、JSON、图片、布局数据等原样封存，并记录哈希；后续步骤不得覆盖。
2. `document/document.json`：规范文档图谱，保留稳定 block/cell ID、页码、bbox、阅读顺序、类型、层级、格式、资源引用和 MinerU 原始字段。
3. `translations/<locale>/translation.json`：译文按 block/cell ID 挂接，不复制或改写原文版面元数据。
4. ~~EPUB、DOCX、HTML、MD+JSON 都是从规范图谱和译文层生成的视图，不是新的事实来源。~~（**历史**：内置多格式导出与外部 TBL 已于 2026-10-13 整体移除；上述原则不再描述当前功能，但 canonical/译文为唯一事实源的原则仍成立。）

这样可以为后续检索、问答、双语引用和定位回原页保留基础。

### 2.2 MinerU 边界

- MinerU 是首要且当前唯一的文档解析服务；Literature 只负责输入、自动分段、上传、轮询、下载、校验、合并和规范化。
- WebUI 从一开始对齐 MinerU 精准解析 API 支持的文件类型：PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX、PNG/JPG/JPEG/JP2/WEBP/GIF/BMP、HTML。
- PDF 超过 MinerU 单文件限制时自动分段，默认每段不超过 200 页；分段进度采用渐进式展示。
- 非 PDF 由 MinerU 执行格式转换；若远端报告页数或转换限制，保留任务和明确错误，不伪装成本地已支持的转换能力。
- 远端解析不能真正暂停或取消。界面不提供误导性按钮；应用重启后恢复轮询，失败时只重试失败分段。
- 解析方式由 `literature/parse_backends.py` 的契约描述：默认 `mineru_remote`（已实现）；`mineru_local` 仅预留接入点，本版本未安装本地模型与依赖，明确不可用，不伪装成可点击功能。任务在创建时固定解析方式与参数快照，旧任务缺字段按 remote 读取。本地接入的部署/日志/隔离与未来验收口径见 `docs/LOCAL_MINERU_BACKEND.md`。

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
- AI 使用选定源目录页、全部现有标题和有界的疑似漏标短块，先理解目录收录范围和整体标题树，再结合跨批父标题与相邻标题复核。普通正文不全文发送；短块的几何/编号/匹配仅作候选召回证据，不决定语义角色。视觉最多 6 页目录与 6 个代表性标题裁片。
- **复核默认“第一遍定案 + 有界抽查”**（2026-10-14）：第一遍整体规划直接定案，提示词里的安全规则命中只写成待确认标注，不再触发逐条二次判断；第二遍只抽查候选数的 5%（下限 8、上限 40，最多一批），结论只作一致性指标，绝不写回建议。`proofread_mode="full"` 是历史全量双遍的回退入口。依据：历史两遍在 role/level/parent 上分歧为 0，而第二遍占墙钟 64%。“两遍一致≠正确”，抽查报告只陈述分歧，不声称准确率。
- **印刷目录优先本地解析**（2026-10-14）：有显式目录标记页（`Contents`/`Content`/`目录`/`目次`）且能解析出 ≥2 条时直接采信本地结果，不再为“核对目录”发一次模型请求；无标记、解析为空或用户显式选择时仍由模型读目录页文本。`toc_extraction` 可取 `auto`（默认）/`local`/`model`。
- **结构路径默认关思考**：与翻译一致，只按已声明协议改字段，用户显式开思考时尊重配置。依据：真实作业 reasoning 占全部 token 的 31%，对“按几何/目录证据选角色”贡献有限。
- 默认建议待人工确认；**2026-10-14 起用户档位（粗校/精校）改为全部自动应用**（见下），安全网是可逆补丁 + 修订历史 + 撤销，删除/合并正文仍永不自动执行。漏标标题补回必须确认，目录未收录的小标题不能仅凭未匹配而降为正文。
- **粗校/精校两档（2026-10-14，后端 + 前端均已实现并联调验收）**：用户只选档位 + 开始/暂停/继续，其余由程序决定。两档都本地优先、默认关思考、不做第二遍逐条复核、默认关视觉、默认全部自动应用。**粗校**候选收窄为「与目录相关或本地可疑的块」（目录命中 + 本地误标/精确层级 + 目录命中但未标标题），未收录的正常标题不发送；**精校**额外做「未收录小节发现」（候选不按目录收窄）+「整树父子复核」。目录为空的书自动回退「无目录模式」（本地信号 + 一次有界模型请求），不弹窗打断。内部统一 `mode` 参数；`proofread_mode`/`planning_mode`/`toc_extraction`/`proofread_concurrency`/`use_vision`/`request_timeout_seconds` 收进高级/诊断覆盖。
- **暂停/继续（2026-10-14）**：结构作业在模型请求边界响应暂停（当前在途请求跑完即挂起），已完成响应落审计；继续时以 `reuse_job_id` 指向已暂停作业做请求级复用，只补剩余请求。
- 本地校准在置信度至少 0.97 且满足既有证据边界时可自动执行可逆补丁；AI 整体推理的修改全部待确认，自报置信度不授权自动采用。
- 尾注页、参考书目页、索引页不得当作印刷目录（证据见 `docs/verification/structure-review-mode-2026-10-14.md`）。
- 删除、合并或改写正文永不自动执行；所有修改有历史并可撤销。

### 2.4 翻译

- 全书、选章、block 和即时选段翻译共用同一结构化引擎。
- 表格按 cell 翻译；标题、图题、图注和脚注翻译；公式、图片及图片内部文字首版保持原样。
- 翻译前可草拟并冻结术语表；任务保存术语表快照。
- 人工编辑的译文自动锁定，批量重译和润色默认跳过。
- 支持 OpenAI、DeepSeek、智谱 GLM、Qwen/百炼和自定义 OpenAI-compatible 服务；记录真实 usage 和估算费用。
- **明文 HTTP 只能逐服务显式放行**（2026-10-15）：Base URL 用 `http://` 且非本机 loopback 时默认拒绝携带 API Key（防止密钥在链路上明文暴露）；内网/自建 MaaS 可在「设置 → 模型服务」勾选「允许明文 HTTP」后使用，放行会写 warning 日志，公网地址默认仍拦截。

### 2.5 导出（已于 2026-10-13 移除）

- 用户 2026-10-13 授权整体移除“导出与外部 TBL”模块，不只是隐藏 TBL。
- 后端删除 `literature/exporters.py`、`literature/tbl.py`、CLI `export` 命令与 `export_count`；`/projects/{id}/exports`、`/export/readiness`、`/tbl/*` 保留路径但返回 **410** 明确废止；MinerU 的 `extra_formats` 是解析服务返回的原始证据，不属于 Literature 导出器，继续保留。
- 前端移除导出导航/工作区/卡片阶段/操作/设置与 TBL；旧 `#/export`、`#/p/<id>/export` 回退到“阅读与翻译”。
- **已存在的用户 `exports/` 文件、原件、raw、document、translations 一律保留**，不做自动清理或迁移删除。

## 3. 目标用户流程

1. 在首页拖入或通过系统文件选择器批量选择文档。
2. 查看文件类型、大小和校验结果；添加文件后在弹窗中逐个设置 PDF 页码范围和额外输出格式，页码留空默认解析全部。
3. 首页先显示浏览器到 Literature 的上传状态，再显示当前 MinerU 任务总进度。
4. 在第二页“文件资源”统一查看等待中、执行中、失败和已完成的全部文档任务与资源。
5. 多分段文档默认显示总进度，需要时展开每段页码、状态与错误。
6. 标准化资源生成后进入结构校对，确认目录和标题层级。
7. 在阅读与翻译页浏览原文、选择章节/block、冻结术语表并启动翻译，人工检查或局部润色。

## 4. 当前架构

```text
Tauri macOS 客户端（单实例）
  └─ 内嵌 React 工作区及 PDF 解码资源
       └─ 内置 FastAPI sidecar（动态 loopback 端口 + 每次启动的会话令牌）
            ├─ 原生路径导入（开发浏览器保留上传暂存）
            ├─ MinerU 任务管理器
            │    └─ MinerU 精准解析 API
            ├─ ProjectStore / SQLite
            ├─ raw → canonical 规范化
            ├─ 结构补丁与翻译任务
            └─ 阅读 / 校对 / 翻译工作区
```

独立仓库与项目数据目标形态：

```text
~/CdoexProject/Literature/
  literature/、frontend/、docs/、tests/  # Git 跟踪的程序与文档
  launcher/Literature.app               # Git 忽略的独立 macOS 客户端
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
      exports/          # 历史遗留：已移除内置导出；仅在原项目已有时保留，不再新建。
                        # 其中 `.mineru-workflow_mineru_job/slices/**` 是封存前的重复下载目录，
                        # 已用 scripts/prune_mineru_workflow_staging.py 清理（保留 job.json）；
                        # 用户 EPUB 与 raw/document/translations 一律不动。
      literature.sqlite
```

## 5. 当前开发状态

> 本段只保留**快照**（上限 30 行），不写逐轮叙事。逐轮事实、证据与验收边界一律写入 `docs/DEVELOPMENT_LOG.md`（唯一正本）；本段与其重复等于第二份副本，禁止。

**当前里程碑**：M1 客户端导入与 MinerU 全能力接入。M2 结构校对（粗校/精校）与 M3 结构化翻译已具备可用纵向切片，见 §6。

**当前交付（2026-10-15）**：后端安全/数据保护、探测预算与前端交互优化，以及「逐服务显式确认的明文 HTTP」（`model_services_v4`）已打包安装到 `launcher/Literature.app`（sidecar `9934ab4b55881b2e9148`）。全量 pytest 598 passed；tsc/Vite 通过；冻结冒烟 15/15，安装内容与构建包一致。真实服务与原生交互待用户验收；交付证据见开发日志。

**前端交付（2026-10-16）**：设置空白页、导入页待处理队列对齐与内滚动、资源页翻译行去导出、校对状态统一「已校对」、模块更名「对照翻译」、目录进度环溢出、翻译单键切换、任务选择列表标题被 grid 压扁不可见——已随本轮重建打包安装到 `launcher/Literature.app`（sidecar `9934ab4b55881b2e9148`，内嵌前端 `index-BSNt2f9U.js` / `index-D2bDGs6A.css`）；冻结冒烟 15/15，旧包备份至 `launcher/.rollback-Literature-B5rqP2lv-20261016.app`。未启动客户端，待真机验收。

**结构校对·对照观看（2026-10-17，已打包安装）**：跨页续接按 **MinerU 原生标记**只读派生 `flow_links`——读回不可变 `content_list_v2`，用 MinerU 在续页留下的空 `paragraph` 占位块作为续行区域，挂到上一页承载整段的 block（`{block_id: {next_region: {page_index, bbox}}}`；`literature/flow.py`；`/projects/{id}/document`、`PATCH /projects/{id}`、`mineru/import` 三处返回，已导入项目无需重新解析）。原页承载块与续页区域都按普通正文块同样式渲染（无额外颜色/角标），两块间用 MinerU 式**虚线折线 + 两端圆点**连接：从源块左下角沿页缝空白折到续页区域右上角，不穿过正文。**框体尺寸修复**：`.pdf-overlay .bbox` 显式 `min-height:0`，不再被全局 `button{min-height:32px}` 撑高（目录/脚注矮行不再堆叠）；三栏 `SplitPane` 设 `minPx=[200,360,280]`，分栏不可被拖到 0 而遮挡。Markdown 页签按块渲染并与 PDF 双向同步高亮；类型图例改为「图例」按需浮层；右侧块列表已移除常驻「N 个内容块」提示行。**顶部三栏头部统一**：`.panel-title`/`.viewer-toolbar`/`.result-tabs` 统一 52px 高、同一底边，一级文字统一 14px；中栏工具栏 `flex-wrap: nowrap`，控制区 `overflow-x:auto` 横向滚动、图例钉在行尾（浮层不被裁剪），拉窄不再换行。全量 pytest 605 passed；tsc/Vite 通过；隔离浏览器 23 + 11 项验收、0 脚本错误（截图 `tmp/structure-flow-review.png`、`tmp/toc-overlay-wide.png`、`tmp/toc-overlay.png`）。已打包安装到 `launcher/Literature.app`（内嵌前端 `index-__nO-9dI.js` / `index-Cz8XKgc8.css`；sidecar `8e505fd1c6e616dd8a93` / sha256 `315945c9…`；旧包备份 `launcher/.rollback-Literature-20261017015852.app`）。**应用图标**换成定稿 monogram（粗体「L」+ 波浪书口的书形底 + 两点）：源图存 `assets/brand/monogram.png`，`scripts/build_icons.py` 新增 `mark` 渲染器（位图墨迹→alpha 蒙版→macOS squircle），默认打包该套，`cards`/矢量 `monogram` 作为备选。未启动客户端、未由用户真机验收、未提交；legacy `content_list` 与纯 Markdown 导入不产出续接。

**导出管理与交互修复（2026-10-17，已打包安装）**：导出记录可管理——`DELETE/PATCH /projects/{id}/exports/{export_id}`（能力 `export_manage_v1`），导出弹窗历史区每条记录可删除（两步确认）与重命名（同步改单文件产物名），不碰 raw/document/translations；翻译确认后自动打开对照翻译页；顶栏 60px + 主导航加宽 + 品牌字前加应用图标；导出结果行限制在卡片内；导出默认 EPUB + 对照版。全量 pytest 612 passed；隔离浏览器验收 14 + 5 项全 PASS；冻结冒烟 15/15。已打包安装到 `launcher/Literature.app`（sidecar `600164e1…` / sha256 `94c8b3b5…`，内嵌前端 `index-DCZcdqbt.js`；旧包备份 `launcher/.rollback-Literature-20261017031104.app`）。未启动客户端、待真机验收。

**开源发布（2026-10-17）**：仓库已推送 `https://github.com/carrot123zhou/Literature.git`（main），README 首页含宣传海报与界面截图；**Release v0.1.0** 提供 Windows x64 NSIS 安装包（GitHub Actions `windows.yml`：PyInstaller + Vite + Tauri/NSIS，`workflow_dispatch`/`v*` 标签触发），包内含 Windows sidecar 与同版本前端；Rust 外壳/密钥存储/sidecar 构建脚本已做 Windows 适配（macOS 行为不变）。未在真机 Windows 运行验收；macOS 仍按本机构建流程交付。

**导出真脚注（2026-10-20，已编码/真实验收，未打包安装）**：EPUB 与 DOCX 导出把 `footnote` 块按「同页/阅读顺序 + 同编号」绑到正文 `^{N}` 上标，分别产出 EPUB3 `noteref`+`aside` 与 Word `word/footnotes.xml` 真脚注（`w:customMarkFollows` 保留印刷编号；同一脚注只允许引用一次，重复引用降级为上标）；未绑定注文与 `^{…}` 一律降级为上标/页末注，不再留 LaTeX 字面量。库内 8 本书绑定率 0–94%（Making 721/767、Once upon 401/830、White Lotus 531/815）；Word 16.114 实开 721 注 docx 无修复、PDF 目视确认；全量 pytest 620 passed。第二轮按用户反馈调整：注文统一章末（aside 首锚章节唯一，跨章引用跨文件链接 + ↩ 回链）、续行注合并、编号格式扩充（`¹`/`N␣`/记号）、页位回退锚定（MinerU 丢正文上标时锚到页内最近正文块并补回上标）；绑定率 Making 754/767、White Lotus 707/815、Once upon 554/830。证据见开发日志同日第二轮条目；重新打包安装与 Apple Books 真机验收待做。

**能力标记**：以 `/health` 的 `capabilities` 为唯一事实来源（`literature/api.py`）——当前含 `intake_items_v1`、`structure_review_v2`、`model_services_v1/v2/v3/v4`、`structure_modes_v1`、`structure_pause_v1`、`structure_flow_v1`、`translation_engine_v2`、`translation_safety_v1`、`parse_lifecycle_v1`、`task_history_v1`、`export_v1`、`export_manage_v1` 等。前端启动时校验关键标记，不静默复用旧后端。注：导出模块（`exporters.py`、`export_v1`）为重新引入版本（与 §2.5 的「已移除」历史决定冲突，待归档计划文本），已于 2026-10-17 随工作区一并提交（`13f3bfd`）；2026-10-17 已重做 EPUB 排版（出版级 CSS：两端对齐+首行缩进+段间距+serif 栈、对照去竖线、导航目录原文·译文/译文纯译文、原书 PDF 首页封面），已重新打包安装（sidecar `4441cacd…`，快照 `.rollback-Literature-20261017024354.app`），未真机验收。

**尚未完成或尚未真实验收（不混写）**：

- 真实模型验收：粗校/精校真实模型 A/B（准确率/耗时/token 收益均未验证）；真实视觉模型准确率与费用；真实 MinerU Token 多格式在线 smoke test。
- 模型配置真实验收：真实外部服务 `/v1/models` 拉取、批量添加、Keychain 保存链路与真实模型端到端（结构 + 翻译）联调未做。
- 翻译关思考的提速量化：探测已实测通过（reasoning 0%），但**同一本书开/关思考的耗时对比与译文质量抽检仍未做**。
- 当前安装包已包含 2026-10-15 源码改动；保留旧版回滚包，尚未由用户完成真实窗口验收。
- 原生客户端：系统性的原生窗口点击/拖放/另存/Finder 与 PDF 解码视觉验收（AX 自动化不可靠）；无 Developer ID / 公证 / Intel 包。
- 迁移前旧项目 `pages[].width/height` 为 0，缺「从封存 raw 重建规范文档」入口。
- 大型非 PDF 的本地自动切分；当前依赖 MinerU 转换并反馈限制。
- 项目回收站浏览与一键恢复（删除数据可恢复，但需在本地目录手工完成）。
- 完整响应式设计、部署、账号体系与远程多用户隔离。

**下一步**：按 §6 路线图推进；实质变更后同步更新本段（≤30 行）与 `docs/DEVELOPMENT_LOG.md`。

## 6. 分阶段路线图

### M1：客户端导入与 MinerU 全能力接入（当前）

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
- 小块并发 + 前后文衔接 + 段落标签协议（v2，2026-10-11）；失败只重试受影响段落，截断不落库；可选两遍翻译+润色。

### M4：交付与本地解析扩展（2026-10-13 调整）

- 内置多格式导出与外部 TBL 已移除（见 §2.5）；历史 `exports/` 文件保留但不作为产品阶段。
- 完整客户端构建、资源管理、签名/公证和干净机器安装验收；浏览器调试入口保留。
- 本地 MinerU 作为未来独立进程/服务接入：后端契约与能力描述已完成，后续再实现本地执行与真实验收。

## 7. 验收标准

- 客户端按钮真实打开原生文件选择窗口；原生拖放可加入同一队列。发布应用内置后端与前端，退出后无残留服务。
- 支持的扩展名与 MinerU 官方文档一致；不支持格式在上传前明确拒绝。
- 单文件超过 200MB、批次数过多、空文件和错误页码范围有可理解反馈。
- 批次设置与每个文件独立的页码范围/额外格式分别进入持久化任务，并在 MinerU 请求测试中逐项可见。
- 500 页以上 PDF 分段连续、无重叠，重启后可恢复，成功分段不重复提交。
- raw 文件哈希不变，canonical/translation 不修改 raw。
- API 密钥不进入项目、日志、Git 或导出。
- 每项外部能力分别记录“已编码、自动测试、隔离联调、真实服务验收”四种状态。

## 8. agent 接手检查表

1. 阅读本文件与 `docs/DEVELOPMENT_LOG.md`。
2. 查看 `git status`，保留用户已有改动。
3. 确定正在推进的里程碑，不跨模块做未经授权的重构。
4. 优先用可运行客户端让用户验收；浏览器只作开发调试。
5. 修改代码后运行对应测试、类型检查和生产构建。
6. 更新本文件的“当前开发状态”和开发日志，再提交 Git。
