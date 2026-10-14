# Literature 总体计划

> 本文件是新对话、上下文压缩或新 agent 接手后的第一入口。最后更新：2026-10-14。

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
- 默认建议待人工确认；自动应用是高级选项。漏标标题补回必须确认，目录未收录的小标题不能仅凭未匹配而降为正文。
- 本地校准在置信度至少 0.97 且满足既有证据边界时可自动执行可逆补丁；AI 整体推理的修改全部待确认，自报置信度不授权自动采用。
- 尾注页、参考书目页、索引页不得当作印刷目录（证据见 `docs/verification/structure-review-mode-2026-10-14.md`）。
- 删除、合并或改写正文永不自动执行；所有修改有历史并可撤销。

### 2.4 翻译

- 全书、选章、block 和即时选段翻译共用同一结构化引擎。
- 表格按 cell 翻译；标题、图题、图注和脚注翻译；公式、图片及图片内部文字首版保持原样。
- 翻译前可草拟并冻结术语表；任务保存术语表快照。
- 人工编辑的译文自动锁定，批量重译和润色默认跳过。
- 支持 OpenAI、DeepSeek、智谱 GLM、Qwen/百炼和自定义 OpenAI-compatible 服务；记录真实 usage 和估算费用。

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

2026-10-14（第二十三轮）重新打包并安装客户端（用户授权安装，**未启动**、未 commit）：把前两轮的结构校对提速 + 项目列表轮询提速 + 资源清理一起打进可交付包。冻结 revision `f01105482311f04ce692`（构建前后一致）。本机 PATH 里没有 node/npm/pnpm，且 `frontend/node_modules` 的 pnpm 链接不全（`@tauri-apps/cli` 找不到 `cli-darwin-arm64` native 模块）——用 CLI 自身的 `NAPI_RS_NATIVE_LIBRARY_PATH` 指向 `.node` 实体绕过，未重装依赖；步骤已补进 `docs/PACKAGING.md`。验证：全量 `pytest` **470 passed**；`tsc --noEmit` 通过（补齐上一轮因无 node 未跑的类型检查）；Vite 生产构建 `index-44088ZuV.js`；sidecar sha256 `b05922f76a9dcd62c62a0e839ef9d1d8cb44b307ba907c0fc74cd09162e6c4eb` / size 42937856，三处（`dist/sidecar`、`src-tauri/binaries`、`.app` 内置）逐字节相同且与 `manifest.json` 一致；`.app` 54.56 MiB，adhoc 签名校验通过。**冻结 sidecar 隔离冒烟**（临时库/runtime）：`/health` revision 与 manifest 一致；真实项目副本下 `/projects` 输出与改造前一致，`project_state` 缓存表在既有库上自动补建。安装前确认目标客户端未运行，旧版（sidecar `dc682466…`）备份到 `launcher/.rollback-Literature-dc682466.app`，用 `scripts/setup_macos_launcher.sh --app-only` 安装；安装后内置 sidecar 与 manifest 一致、与构建包 `diff -rq` 完全一致、签名校验通过，且用 `strings -a` 确认包内前端资源名为本轮构建产物。

**未验收（不混写）**：**未启动客户端**，未经原生交互与真实浏览器端到端；未调真实 MinerU/付费模型，**新默认（`sample` 复核、本地目录采信、结构关思考）的真实模型准确率仍未验证**（这是第二十二轮遗留的核心未验收项）；无 Developer ID/公证/Intel 包。另：PID 4098 有一个早期会话遗留的开发态 sidecar（`/tmp/lit-smoke` 库，端口 41731）仍在运行，不阻塞安装也不影响已安装客户端，本轮未动。

2026-10-14（第二十二轮）结构校对默认路径瘦身：复核降为有界抽查、本地目录直接采信、结构路径关思考，并清理导出时代遗留暂存副本（已编码/自动测试/离线基准，未真实模型 A/B、未重打包客户端）：起因是实测“同书两种做法”（`On Being Authentic` 库内真实作业 288.5 s / 99,006 token，其中第二遍 184 s；人工 agent 同书 1 分 19 秒 / 约 1.2 万 token）。**①复核模式**：新增 `proofread_mode`（`sample` 默认 / `full` 历史回退 / `off`），`proofread_full=true` 仍等价 `full`；`select_review_targets` 新增 `review_mode`，`sample`/`off` 下安全规则命中只写 `review_flags` 待确认标注，第二遍仅剩“规划缺失/非法”的硬阻塞（上限 80 条）+ 一批 5%（8–40 条）确定性抽查，抽查结论只进 `review_selection.qa` 一致性指标、强制不写回 decisions/suggestions。**②印刷目录**：`TOC_MARKER_RE` 认单数 `Content`；新增目录条目形态判据（≤100 字符、无句读结尾、非书目引用）与索引页判据（注释引用 `24n`、`See/See also` 密度），`find_toc_pages` 不再把尾注/参考书目/索引页当目录——修复 `On Being Authentic` 误选尾注页 p97–104（80 条垃圾条目）与 `Once upon a future time` 误选参考书目页；有显式标记且可解析时本地目录直接采信（本库 5/7 命中），省掉 `structure_extract` 请求。**③思考控制**：结构作业默认 `with_thinking_disabled()`。**④资源清理**：新增 `scripts/prune_mineru_workflow_staging.py`（默认预览、`--apply` 才删、只删 sha256 与封存 manifest 一致的文件、保留 `job.json`），清理 6 个项目共 474 个文件 / **181.4 MiB**，`verify_raw` 全部通过，用户 EPUB/raw/document/translations 未动，`.trash` 未动；`library` 594 MB→416 MB。**验证**：全量 `pytest` **463 passed**（新增 `tests/test_structure_toc_detection.py` 6 项、`tests/test_structure_review_modes.py` 17 项；改写 4 处旧断言以反映新默认）；离线基准 `tests/manual/structure_review_mode_benchmark.py`（只读真实库 + mock provider 跑真实 `run_structure_job`，无模型调用）：改造前后请求 **-50%~-59%**、输入字符 **-43%~-52%**、必须逐条作答条数 **-48%~-52%**（5 份真实文档，报告 `docs/verification/structure-review-mode-2026-10-14.md`）。**未验收（不混写）**：**质量未证**——离线基准用 mock provider，第一遍规划由文档自身层级派生，不能声称准确率或真实耗时等价；“第一遍定案 + 抽查”只有“历史两遍零分歧”这一个支持证据。未用真实模型做同书 A/B、未重打包/安装客户端、未改真实库 raw/document/translations。

同日追加：项目列表轮询与 `source.md`。**① `GET /projects` 的两处隐性开销已消除**：`structure_review_summary` 原来为了一个 `updated_at` 就解码整份 `document.json`（实测 131 ms/轮，其中 122 ms 是 JSON 解码；`document.json` 单份 1.4–8.2 MB），改为新增 `ProjectStore.document_updated_at()` ——以 `(size, mtime_ns)` 为键缓存到新增的 `project_state` 表（旧库自动补建），单项目 **57.5 ms → 1.8 ms**、整库 7 项目 **164.5 ms → 6.3 ms**；列表路径不再调 `list_jobs`（原来要解码 `result_json`/`activity_json` 审计，实测 42 ms），改默认 `list_job_summaries`。`GET /projects/{id}` 仍返回完整 jobs。**② `document/source.md` 实测后决定保留**：渲染 + 写入仅 0.0–1.9 ms、全库约 6 MB，且 `tests/test_pipeline.py` 与 `docs/ARCHITECTURE.md` 都把它当可读投影，删掉是负收益；但它原是非原子 `write_text`，已改为 `storage.atomic_text()` + `mineru.write_source_markdown()`（4 处写入全部收口）。追加验证：全量 `pytest` **470 passed**（新增 `tests/test_document_state_cache.py` 6 项、`tests/test_pipeline.py` 1 项）；未重新打包/安装客户端，本机无 Node 未重跑 `tsc`（前端本轮无改动）。

2026-10-14（第二十一轮）在最新完整版本上安装自适应结构校对更新（用户授权安装，未 commit）：安装前只读核实：HEAD `b40b9ae`（源 revision `4c4ffbcfb83f0c774fc7`）；工作树 revision `5368566b3b43788b28ba`（晚于旧安装包，含其他 agent 最新未提交的 translation/provider 改动）；**已安装**旧版 launcher app 的 manifest revision `d383569f0222a29435f1` / sidecar sha `a5a99eba…` / 42892592（02:58 构建、03:02 安装），早于当前工作树，不是更新的外部来源，因此在其基础上重建不会丢新模块。冻结构建输入：构建前后 `source_revision()` 均为 `5368566b…`，前端 dist `index-VaYJ_Nxx.js` sha256 `2bf3bffc…`。验证：全量 `pytest` **433 passed**、`tsc --noEmit` 与 Vite 生产构建通过（前端本轮无新改，未重复浏览器）。重建自包含 sidecar（revision `5368566b3b43788b28ba`、sha256 `dc682466f982ad0d846a491640fa227efb928b6cff3002c19497c71a6d663f46`、size 42927360）与 Tauri 包 `src-tauri/target/release/bundle/macos/Literature.app`（54.55 MiB）；包内 sidecar 与 `src-tauri/binaries`、`dist/sidecar` 逐字节一致，`codesign --verify --deep --strict` 通过（adhoc）。旧版备份到 `launcher/.rollback-Literature-a5a99eba.app`（sidecar 仍为 `a5a99eba…`，已验证；不提交），再用 `scripts/setup_macos_launcher.sh --app-only` 安装到 `launcher/Literature.app`；安装后内置 sidecar 与构建包、binaries 逐字节一致，`Contents` 目录与构建包 `diff -rq` 完全一致，`codesign --verify --deep --strict` 通过，**未启动客户端**。包含模块：自适应分支结构规划（可选试用，global 默认不变）+ 其他 agent 的 translation SSE/超时切分/读超时可配、providers/request_coordinator、ReadingPanels 等最新改动（sidecar revision 覆盖全部 `literature/*.py`，前端 dist 含 `planning_mode`/`自适应分支`）。**未验收（不混写）**：真实模型 A/B、真实 MinerU/付费 provider、原生窗口点击、公证与干净机安装均未做；本轮仅安装授权，未 commit/push、未改真实库/raw。

2026-10-14（第二十轮）自适应分支结构规划（可选试用模式，已实现/离线验收/未部署）：新增 `literature/structure_adaptive.py`，落地“用户确认目录优先 → 本地/视觉目录 → 框架验证 → 可靠区域并行 + 歧义区域串行 + 局部升级/回退”。**框架**：正文匹配 ≥0.92、唯一领先 ≥0.06、目录无重名、正文层级佐证、阅读顺序 LIS 一致；印刷页码需 ≥3 锚点单调才作辅助，否则降 partial。**区域与预检门控**：最浅层可靠锚点定章，章内候选 + 只读相邻上下文（`REGION_CONTEXT=6`，大章按 `REGION_WINDOW=64` 再切，未合并区域主候选 ≤64）；发送前先构造全部请求并计算成本代理（请求数/字符/串行关键路径/上下文），若自适应请求数超全局则先做小区域装箱合并（`MERGED_MAX_PRIMARY=128`，保留各章 anchors 与边界；合并后单请求主候选 ≤128，会改变单请求输出大小），门控要求请求≤全局且字符<全局且轮数≤全局，否则精确回退；每个请求都带精简 `root_convention`，`overview` 仅首个请求；可靠区域默认 2 路有界并行（仍走 `RequestCoordinator`），头部/重名/歧义区域串行；合并只接受主候选，重叠只读，每个候选恰好一次主判断。**升级/回退**：局部升级触发覆盖遗漏/uncertain/非法、树冲突、已确认目录层级矛盾（不只“不在 nodes 里”），局部升级只问可疑 ID、全局补查按阅读顺序 ≤64 切批，两者合计受 `MAX_ADDITIONAL_PLANNING_REQUESTS=4` 硬上限；`_apply_rows` 与原 `plan_outline` 同等严格（重类型/重复 ID 整项拒绝/父标题必须在给定集合且靠前）；超限或仍异常写入 issues，交由第二遍复核+`validate_hierarchy`+人工确认；框架 `unreliable`、候选 <120 或预检门控不通过回退整体串行；`proofread_full` 全量复核仍可选；未收录小节/疑似漏标短块全部参与，不因未收录丢失。**集成**：`select_review_targets` 的 `strict_local` 保留为默认 True 的可选参数（默认行为同 `d7a0cbd`）；新增 API `planning_mode`、UI「规划策略」选择与“实际路径/升级原因”显示、策略文档 `docs/ADAPTIVE_STRUCTURE_PLANNING.md`。**离线验收**（无模型调用，规划与第二遍分开报告）：White Lotus 348 候选覆盖不变（348/348）、规划 6 请求/183,923 字符 → 6 请求/97,443（**-47.0%**，合并生效）、端到端 13/363,728 → 13/273,138（**-24.9%**，限制来自未改的第二遍复核）；Journal 机械计划因人工构造的 parent=None 触发 3 次局部升级（只问可疑 ID），主 4 但总 7/108,740、端到端 11/201,846 反而更差（真实模型未验证，不宣称收益）；Authentic 58<120 回退整体（等价）。**验证**：结构相关 `tests/test_structure_adaptive.py` 38 项回归；当前工作树全量 `pytest` **432 passed**（含并行未提交的翻译/SSE 改动；本轮结构改动未触碰 translation/providers/request_coordinator）；`tsc`、Vite 生产构建、隔离真实浏览器 **21/21 PASS、0 脚本错误**（新增规划策略 POST 与路径/边界显示），原解析浏览器全流程仍通过。**未验收**：无真实模型 A/B，不能声称准确率/真实耗时；阈值未用真实样本校准；未安装/未提交 Git/未改真实库/raw。

2026-10-14（离线实验，未部署）：AI 结构校对“压缩上下文 / 收紧复核范围”。以 `d7a0cbd` 选择性复核为 baseline。White Lotus 只读快照 348 候选，历史作业候选 ID 与当前重建**完全一致**，可做忠实两遍回放：**V1 仅压缩上下文**（复核范围不变）输入字符 -23.2%（规划 -41.5%，去后续窗口重复的全局 overview/整本目录/过长 previous_outline，保留近邻目录/祖先/相邻标题），Journal 规划 -32.2%，Authentic 单窗口 0%；**V2 压缩+仅异常/高风险复核**输入 -59.8%、请求 13→7（复核 261→20）。历史两遍在 role/level/parent 上**零分歧**，V2 省略的 241 项历史分歧也为 0，建议集合不变；但两遍一致≠正确（两遍共有 2 个父子关系错误）、单样本单模型、Journal/Authentic 候选逻辑版本不同无法回放、无 tokenizer（token 为估算）。结论：**V1 有低风险离线收益证据；V2 收益明确但质量未证**，不能声称准确率/真实耗时已验证或可部署。生产默认路径未变（`select_review_targets` 仅增加默认 `strict_local=True` 的可选参数）。实验脚本 `tests/manual/experiments/structure_context_experiment.py`、边界回归 `tests/test_structure_context_experiment.py`、报告 `docs/verification/structure-context-experiment-2026-10-14.md`；下一步真实小样本 A/B（3 样本×2 模型×3 分支，≤120 请求，硬预算）与最小验收见报告。未安装/未启动/未改真实库/未提交。

2026-10-14（第十九轮）翻译链路流式传输、超时切分重试与读超时可配（Lead 直接实施，已编码/自动测试/tsc/生产前端构建，未重建 sidecar/未安装/未真实 provider 验收）：针对真实库诊断（White Lotus 16 段译文全部因「等待模型响应超时（180 秒）」失败；Qwen 思考模型 reasoning 占输出 token 约 85–90%）落地四项修复：**①SSE 流式请求**（`providers.py::_read_stream`）：`OpenAICompatibleProvider._complete` 默认 `stream=true + stream_options.include_usage`，聚合 `delta.content`（`delta.reasoning_content` 只参与保活不计入译文），usage/finish_reason/request_id 取自事件流；httpx 读超时语义从「等待整份响应的总时限」变成「两个数据块之间的空闲时限」，思考型模型长时间生成不再整单超时作废。三重回退：服务端 400 拒绝流式、空事件流、中转站忽略 stream 返回完整 JSON，前两种置 `_stream_disabled` 本会话永久回退非流式（同一 attempt 预算内重发，不重复预留）；流中断仍映射 `ProviderTimeoutError(read)`；attempt 预算钩子/退避重试语义不变。**②超时切分重试**（`translation.py::_retry_batch_after_timeout`）：`_run_chunk` 捕获 `ProviderTimeoutError` 后沿 segment 中点切半、两半隔离重试（递归受 `MAX_SPLIT_DEPTH` 限制，单 segment 仍超时才真失败），不再整批直接判死。**③读超时可配**：`TranslationJobInput.request_timeout_seconds`（默认 300，30–1800），启动翻译作业时 `with_read_timeout` 应用（用 `hasattr` 守卫，测试/自定义 provider 缺省保持原超时）。**④**思考模式控制的前端入口已在第十八轮落地（`qwen_thinking` 的 `enable_thinking=false` 是对 Qwen 思考模型最大的提速+降费+减失败杠杆）。**⑤reviewer 复验后修复 E/F 回归**：非流式与 relay-JSON 分支的非法响应体（如 200+门户 HTML）现在也归成 `ProviderError`（经 `_HTTPStatusError` 统一路径，外层 `end(final=True)` 语义保持），恢复连续失败熔断对这类响应的有效性；content-type 缺失/标错但 body 为 SSE 的响应按文本再解析一次，不误报错误（SSE 聚合逻辑抽为模块级 `_aggregate_sse_events` 复用）。另按复验意见把「本会话永久回退」的表述修正为「本 provider 实例」（每个新作业新建 provider，仍会先试一次流式）。流式作用面覆盖所有 `_complete` 调用方（结构 AI/即时/译审/vision），属有意设计（读超时语义同样受益），已在文档标注。**验证**：全量 `pytest` **379 passed**（新增 `tests/test_translation_streaming.py` 15 项：SSE 聚合/usage/finish_reason、400 拒流式回退、中转站忽略 stream、空事件流回退、流中读超时映射、思考字段照发、200+HTML 与 relay 非法 JSON 归为 ProviderError、无 content-type 的 SSE 体照常解析、3 项真实本机 HTTP 流回归（relay-JSON/SSE 体标错 content-type/流式 200+HTML→ProviderError 而非 ResponseNotRead）、超时切分后全部译出、单段超时报可行动错误、读超时可配）；`tsc --noEmit` 与 Vite 生产构建通过。**未验收**：未重建 Tauri 包/未安装客户端；未用真实中转/provider 验证流式兼容性与真实耗时/费用改善；真实 MinerU/付费模型、原生交互、公证仍未验收。

2026-10-14（第十八轮）AI 结构校对提速保质（Orca Worker 实施，已编码/自动测试/tsc/生产前端构建，未真实模型 A/B、未安装）：**①本地候选构建**：`structure_model_payload`/`reconcile_toc` 的长正文在相似度计算前按长度过滤，目录与标题归一化加进程内有界 `lru_cache`，`heading_similarity` 加长度上界预判；语义完全不变（三份真实文献快照完整 payload SHA-256 与修改前一致，候选数不变），自然 cold 本地耗时 White Lotus **37.744503→3.263567s**（348 候选）、Journal **12.810176→1.177188s**（228）、Authentic **2.556754→0.343734s**（58）。**②选择性第二遍复核**：新增 `structure_reasoning.select_review_targets`，第一遍规划经“完整/角色合法/有限置信度/父子关系（含 `validate_hierarchy` 预检）/确认目录锚点”检查后复用，第二遍只复核缺失、uncertain、冲突、证据不足、漏标提升、非标题降级、无目录锚点改层级、本地仍像标题/非散文短块，以及父标题仍需复核的子项；`proofread_full=true` 可回退全量。历史响应回放（不是新模型 A/B）White Lotus 348 候选复用 87、复核 261（7 批 vs 原 9 批）。**③并发复核**：第二遍固定整体标题树上下文，按 40 条一批、默认 2 路（最多 4 路）并发；有界在途调度（失败即 `stop_event` 停止提交、取消未开始批次、等待在途收尾）、每批独立 decisions/skipped 并按批次顺序稳定合并、审计按 `batch_tag` 归属、`AuditedProvider` 审计/缓存/写账/进度加锁、`BudgetExhausted` 在发送前拦截不记未知费用/不留待对账、写账异常 `finally` 仍对账预算句柄。仍统一走公共 `RequestCoordinator` 服务并发/退避/预算。**④思考参数**：模型服务新增显式 `reasoning_style`（`openai_effort`/`qwen_thinking`/`glm_thinking`）+ `reasoning_effort`/`reasoning_budget`，默认全空不发送任何字段，只有显式选择才按协议拼请求体；reason 上限收紧到 30 字；不砍 `max_tokens`。**⑤进度/UI**：规划进度 70%→80%、复核 82%→92%，活动显示候选数、规划/复核批数、实时完成数与已用时；结构结果栏展示候选/复用/第二遍数量。**验证**：全量 `pytest` **364 passed**（新增 `tests/test_structure_speed.py` 27 项：复用边界、可移植合成夹具、并发并行度/顺序、实时进度、失败停止付费、全量回退、预算 MockTransport 集成、思考字段）；`tsc --noEmit` 与 Vite 生产构建通过；`tests/manual/structure_selection_benchmark.py` 为只读手动基准（历史回放+本地耗时），不进全量回归、不依赖真实库；新增隔离真实浏览器 `tests/manual/structure_ui_browser_check.py` **19/19 PASS、0 脚本错误**（阻断非本地网络与业务 Keychain、fitz 有效 PDF，覆盖复核参数 POST、运行总/阶段耗时递增、覆盖口径、思考配置保存与兼容默认），原有 `parse_ui_browser_check.py` 全流程仍全部通过。**构建**：重建自包含 Python sidecar（revision `21664b1e108399c8335c`，sha256 `7aec1108…`，size 42888192）与 Tauri 包 `src-tauri/target/release/bundle/macos/Literature.app`（54.52 MiB）；包内 sidecar 与 `src-tauri/binaries/manifest.json` 及源二进制 SHA-256/大小一致，`codesign --force --deep --sign -` 后 `codesign --verify --deep --strict` 通过（adhoc）。**安装/提交**（用户 2026-10-14 授权）：安装前确认目标客户端与内置 sidecar 未运行；先把上一版 `launcher/Literature.app`（sidecar sha256 `6915c3a4…`）备份到 `launcher/.rollback-Literature-6915c3a4.app`（不提交），再用 `scripts/setup_macos_launcher.sh --app-only` 安装到 `launcher/Literature.app`。安装后内置 sidecar 与源包及 `src-tauri/binaries` 逐字节一致（sha256 `7aec110812917c52c8fcee552a7b8ee60c6404e42ec0beeb2708c2b003364052`，size 42888192，源码 revision `21664b1e108399c8335c`），`codesign --verify --deep --strict` 通过（adhoc）。**未启动客户端**。本轮创建本地 Git 提交，仅含结构优化相关源码/测试/手动验收脚本与两份文档，不含构建产物/缓存/真实库及既有未跟踪文件；只本地 commit，不 push。**未验收**：本轮没有真实模型 A/B，不能声称真实准确率或 AI 总时长提升；未改真实库/raw、未调付费服务；真实 MinerU/付费模型、原生交互、公证安装仍未验收。

Lead 独立复验：隔离全量测试 364 passed、TypeScript 检查、结构浏览器 19/19 均通过；三份只读快照的新旧完整 payload 一致；包内 sidecar 与源码 revision、manifest 的 SHA-256/大小一致，严格签名检查通过。以上不替代真实模型 A/B 或原生客户端验收。

2026-10-14（第十七轮）客户端安装与本地 Git 提交（用户授权）：用户授权“安装新版 app，提交 Git”。用已验收包 `src-tauri/target/release/bundle/macos/Literature.app`（sidecar revision `613a128fc4c0146af0e2` / sha256 `6915c3a4…` / size 42873728）经仓库既有 `scripts/setup_macos_launcher.sh --app-only` 安装到 `launcher/Literature.app`；安装后内置 sidecar 哈希/大小与 manifest 及源包字节一致，`codesign --verify --deep --strict` 通过；旧客户端备份到 `launcher/.rollback-Literature-0e308e43.app`（不提交）。安装前确认目标客户端与 sidecar 未运行。**未启动客户端**。同轮创建本地 Git 提交，包含本轮及作为该版本依赖的此前未提交实现与测试/脚本/文档；有意未提交 `opendesign-projects/`、`docs/prototypes/`、`docs/verification/`、大型截图、`launcher/`、构建产物与缓存、真实库/运行数据（均保留在磁盘）。仅本地 commit，不 push/不 amend。外部未验收（未变）：真实 MinerU/付费模型、原生窗口逐项点击、公证与干净机器安装；结构裁片/目录磁盘缓存未实现。

2026-10-14 Lead 独立收口复核：Orca Worker developer 的本轮部分重构与优化通过离线验收。独立执行全量 Python 测试 **336 passed**、TypeScript 类型检查与隔离生产前端浏览器全流程 **103/103 PASS**；浏览器仅出现保存失败用例刻意 abort 的 `net::ERR_FAILED`，无脚本错误。另用临时项目复现人工编辑与源文写入竞态，确认采纳校验和保存期间写入被阻挡、后续人工译文保留、过期源文候选拒绝。源码 revision `613a128fc4c0146af0e2`、manifest 与包内 sidecar 的完整 SHA-256/大小一致，签名验证通过。译文轮询目前为 revision 条件请求（未变化返回小载荷，变化返回全量），不是条目级差量；结构裁片/目录磁盘缓存仍列后续。未提交 Git、未安装/启动新客户端；真实 MinerU/付费模型、原生交互、公证与干净机器安装未验收，尚无真实文献的节省 token/费用基准。

2026-10-14（第十六轮）采纳译审候选原子性与服务端校验（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离浏览器，已构建未安装）：**A**：`adopt_review` 把源 hash/译文基线/字段完整性校验与 `edit` 读改写落盘放入同一临界区（锁序 `document_lock → translation_lock`，`edit` 重入），不再验证后释放锁再写；回归先证旧实现会被旧候选覆盖，修复后人工最新译文胜出。**B**：`source_hash`/`base_revision` 必填（API 422 + 服务层双重），服务端按源字段校验完整性，缺字段拒绝不改库，重复采纳同内容幂等、否则 409；`candidate_id` 为客户端去重凭证（非服务端证据）。**C**：浏览器 stub 候选从真实 API 取 `source_hash`/`updated_at`。**验证**：`pytest` **336 passed**、`tsc` 通过、隔离浏览器 **103/103**；自包含 Tauri revision `613a128fc4c0146af0e2`（与源码一致）/ sha256 `6915c3a4…` / size 42873728，bundle 哈希与 manifest 一致、adhoc 签名通过，**未安装**。未调真实服务/未提交 Git。外部未验收：真实 MinerU/provider、原生点击、公证；结构裁片/目录磁盘缓存未实现。

2026-10-13（第十五轮）草稿父状态化、候选身份/基线校验、review 生命周期与真实浏览器验收（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离浏览器，已构建未安装）：①阅读页 `editing/draft` 提升到父级按 block_id 持有，range↔overlay 重挂载不丢草稿，保存失败保留草稿，焦点不拉回；`useVirtualWindow` 与真实 DOM 同步 scrollTop。②`review` 返回 project/locale/block_id/candidate_id/source_hash/base_revision，完整性按源所需字段；新增 `/review/adopt` 在写锁内校验源 hash 与译文基线（不符 409），前端候选按 project:locale 隔离、代次丢弃迟到响应、展示原文/现译/建议。③`review`/`instant` 有 running/completed/budget_paused/failed 真实终态并返回 job_id，API finally 关 provider。**真实浏览器 103/103 PASS**（新增草稿往返、保存失败、审校候选采纳/拒绝、切项目迟到丢弃、结构结果栏窗口化；不拿旧 95 用例声称已验收）。**验证**：`pytest` **334 passed**、`tsc` 通过；自包含 Tauri revision `dd731bb60a4216298d4f` / sha256 `8e2e2b7f…` / size 42869472，bundle 哈希与 manifest 一致、adhoc 签名通过，**未安装**。未调真实服务/未提交 Git。外部未验收：真实 MinerU/provider、原生点击、公证；结构裁片/目录磁盘缓存未实现。

2026-10-13（第十四轮）attempt 预算持有到对账、结构逐响应、revision 源指纹、定向译审与列表窗口化（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离浏览器，已构建未安装）：**①attempt 预算**：预留在 final attempt 不再提前释放，调用方写账后原子 reconcile（真实 provider 并发只花一笔，回归）；**②结构**：逐结构响应 reconcile，不再作业结束用项目总 delta；**③revision**：覆盖 locale/译文/文档 mtime+size/协议版本，源变即失效（回归）；**④待对账**：pending 带可对账身份，NULL usage 与 pending 去重，前端徽标显示；**⑤上下文**：`_chunk_context`/串行衔接不跨章节/语言；**A 窗口化**：新增 `useVirtualWindow`，阅读页与结构结果栏接入，1000+ blocks 大书浏览器验收 DOM 有界、滚到底/跳转可渲染；**B 定向译审**：`review` 产出候选（走公共预算层、不自动保存），前端可采纳/拒绝，锁定内容不自动覆盖。**验证**：`pytest` **332 passed**、`tsc` 通过、隔离浏览器 **95/95**；自包含 Tauri revision `9e9aafbafc49e7c55d77` / sha256 `1e61ed4f…`，bundle 哈希与 manifest 一致、adhoc 签名通过，**未安装**。未调真实服务/未提交 Git。仍待：真实服务/原生/公证外部验收；结构裁片/目录磁盘缓存。

2026-10-13（第十三轮）中间证据同源/同阶段、崩溃窗口与公共预算收口（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器，未安装/未真实服务验收）：**①中间证据**：`partial_segments` 每块带 `source_hash/refine`，源变或阶段不符直接丢弃（回归：等长改源不混用旧中段）；**②崩溃窗口**：每个协议校验通过的批次立即加锁原子落盘（回归：第 2 请求中断后恢复不重发第 1 批）；**③预估 UI**：`onInvalidateEstimate` 稳定 `useCallback`，选项变化才失效，不再每 render 清空；`list_job_summaries` 保留 `scope`；**④未知费用**：`pending_cost_records` 持久（`usage_pending.jsonl`），重启仍统计；账本不可读时保守拒付；**⑤每 attempt 预算**：provider 每次 HTTP 前调 `attempt_hooks(prompt_chars)`，协调层原子 reserve，实际费用由调用方 commit；翻译/即时/结构均接入（结构完成后 commit delta）；**⑥R08**：`_chunks` 按章节/语言边界断开，run/estimate 同规则；**⑦R11/R12**：译文 `revision` + `?since=` 增量；结构进程内请求缓存（TTL/上限有界、上下文含模型+文档版本）；backend_config 校验扩展到模型/语言/页范围。**验证**：`pytest` **329 passed**、`tsc` 通过、隔离浏览器 **92/92**；本轮未重建 sidecar/Tauri（等全部完成）。**仍待**：R11 结构/阅读 block 窗口化；R12 定向译审 UI 采纳链与裁片/目录缓存。

2026-10-13（第十二轮）预算一致性/已付费子段/未知费用/解析结果消费（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器，未安装/未真实服务验收）：按 Lead 复核收口 A–F。**A**：`RequestCoordinator` 自持 `spent`（单一一致性边界），`reserve` 锁内一次判定，不再由各 service 缓存过期标量。**B**：`_run_chunk` 预算暂停时返回已解析子段，`_process_blocks` 将其写为 `partial_segments`（绑 run_token）；恢复只补缺失子段，不重发已付费段。**C**：真实 provider 每次 HTTP attempt 经 `attempt_hooks` 进入公共层（服务并发/退避）；翻译/即时走同一 `_complete`，结构挂 attempt hooks。**D**：`usage_summary` 持久化 `unknown_cost_records` 并下发/展示；写账失败保留待对账证据。**E**：`ScopeDialog` 选项变化即失效旧估算；`list_job_summaries` 保留小 `scope`。**F**：`MergeCallback` 改为消费 `ParseResult`（含 source_pages），fake local-like 无 SliceJob 进入真实 seal/normalize/merge。**验证**：`pytest` **319 passed**、`tsc` 通过、隔离浏览器 **92/92**；未安装/未调真实服务/未提交 Git。**仍待**：结构预算预留、R11 ETag/窗口化、R12 结构缓存/定向译审、R08 章节语言边界。

2026-10-13（第十一轮）请求协调/预算、解析契约与结构确认（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器，未安装/未真实服务验收）：①**R09**：新增 `literature/request_coordinator.py`（服务级并发信号量 + 退避冷却 + 项目预算在途预留 + 未知费用计数）；翻译每次真实 HTTP（含子批/重试）在 `_complete` 先原子 reserve、不足即 budget_paused、响应后立即写 usage 再 release；预估加 `force` 并复用真实请求组装；前端 ScopeDialog 估传当前实际选项（locale/术语/附加要求/refine/force/polish）与区间/未知价格。②**解析契约**：统一 `ParseResult`（raw 目录 + source_pages）；`ParseBackend` 协议与实际签名对齐；`RemoteMinerUBackend.result` 返回结果并由协调层统一 seal/normalize/merge；`backend_config` 执行前校验；`retry`/`run_or_resume` 先 `ensure_available`；新增 fake local-like 适配器进入同一 merge 的回归。③**R11（部分）**：`list_job_summaries` 只投影状态列 + `/projects/{id}/status` 轻量轮询；进入已完成结构作业按 job_id 拉完整详情；`refreshProject` 并行 + 代次防串。④**R12（部分）**：质量告警在 `ReadingBlockCard` 可见且数字 token 化比较；结构 `done` 改为**显式审查确认**（绑定文档版本，改动后失效），新增 `/structure/confirm` 与 UI 按钮，资源卡片展示 partial。**未做**：结构/即时共用并发额度、译文 ETag 增量、block 窗口化、结构缓存/定向译审。**验证**：`pytest` **313 passed**、`tsc --noEmit` 通过、Vite 构建与隔离浏览器 **92/92**；未安装/未调真实服务/未提交 Git。

2026-10-13（第十轮）R02 来源契约、冻结契约、remote 真实 adapter 与部分 C 收尾（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器，未安装/未真实服务验收）：Lead 第二轮复现指出第九轮 R02 修复不完整：6 块旧译文 + force + 第 6 块绕过收尾中断，恢复 0 请求假 completed。本轮：①**任务来源契约**：`TranslationEntry` 新增任务级 `run_token`，预写 checkpoint 不再把旧译文算成本次完成（Lead 场景为 `[]`），恢复只跳过“本次带 run_token 且通过校验”的块 ∪ 本任务已写清单；counts 改按本轮来源（force 暂停 1 块 → completed=1/unhandled=5）；已校验旧译文不再计 failed。②**冻结契约实际生效**：`frozen` 存完整术语快照/附加要求/`prompt_version`/非敏感模型参数（model/base_url/temperature/max_output_tokens）/force/polish；恢复时实际应用（Lead 场景 model-before+OLD 生效，密钥仍运行时读）；服务删除时明确阻止。③**remote 真实 adapter**：新增 `RemoteMinerUBackend` 与 `backend_factory`，`run_or_resume` 先校验 `backend_id` 再经 `prepare/execute/status/result` 推进；local/未知持久化 job 在入口被拒（不建 client、不读 Token）；spy 适配器证明调用顺序。④**部分 C**：R08 在请求级按字符/字段预算真正拆多次 HTTP（大表/长段首发预拆）再聚合拼回稳定字段；R09 预估复用真实请求组装并给出 token/费用区间，新增在途费用预留（小预算并发只发 ≤ 1 块）；R11 新增 `GET /projects/{id}/status` 轻量接口并接入前端高频轮询；R12 新增本地质量告警（数字/URL/引用）；force 失败保留旧译文内容但不计本轮完成，counts 区分 unhandled/failed。**未做**：R09 服务级共用并发/限流、R11 ETag 增量与 block 窗口化、R12 结构 done 版本绑定/增量缓存/定向译审。**验证**：`pytest` **309 passed**、`tsc --noEmit` 通过、Vite 构建通过、隔离浏览器 **92/92 PASS、0 脚本错误**；自包含 Tauri 产物（`src-tauri/target/release/bundle/macos/Literature.app` 54.47 MiB，sidecar revision `cfe0e3d46a4d7a36dea4` / sha256 `7194c984…`，bundle 哈希与 manifest 一致，adhoc 签名验证通过）已构建但**未安装**；未调真实服务、未改真实库、未提交 Git。第九轮“R02 已关闭”的表述以本轮为准；整体优化未完成。

2026-10-13（第九轮）架构整理：移除导出/TBL、MinerU 本地后端口径与重复费用/读写膨胀修复（Orca Worker 实施，已编码/自动测试/tsc/临时 Vite 构建，未安装/未真实服务验收）：用户授权“完成部分重构和整体优化，去除导出与外部 TBL 模块，并为以后增加本地处理的 MinerU 模块留下口径”，本轮不推倒重写。①**正确性与费用**：`JobManager.start_translation`（新建与 resume）与 `start_structure` 改为项目级原子领取，并发启动/双击只创建一次；翻译 checkpoint 在发出首个请求前冻结 `target_ids/pass/源文摘要/非敏感模型配置/术语表摘要`，并给译文条目加任务级 `run_token`：恢复只跳过“本次任务真正处理过”的块，修复“6 块旧译文 + force + 第 6 块绕过收尾中断 → 恢复 0 请求假 completed”（现在只重发第 6 块）；超长单字段沿句子边界预拆为 `key~i` 子段再拼回稳定字段，失败重试可继续缩批；译文 `revisions`/`candidates` 改为平面内容快照，消除 `asdict(previous)` 递归嵌套导致的 translation.json 指数膨胀；`ProjectStore` 新增可重建/可失效的 `project_id→path` 索引，`/projects` 与逐项目查询不再二次扫描全库。②**整体优化**：全局熔断改为“连续 3 个分块失败即停止提交新分块”，已有成功分块保留并按 `partial_failed` 呈现；翻译/结构作业 `finally` 显式 `provider.close()` 并清理暂停句柄；`exec.submit` 失败时把任务标为可恢复的 failed（不卡在 queued）；前端项目轮询闲置降到 5s、暂停不再反复拉整层译文；规范化 HTML 表格维护占用网格，修复 rowspan 后续行坐标错位（仅新解析生效，旧 cell ID 不变）。③**移除导出/TBL**：见 §2.5；前端删除 `ExportPanel`、导出导航/路由/阶段/筛选/操作与设置中的 TBL，旧导出地址回退阅读页。④**本地 MinerU 口径**：新增 `literature/parse_backends.py` 描述 remote/local 身份、版本、能力（格式、切片、轮询、上传 URL、远端取消、断点续跑）与不可用原因；任务持久化 `backend_id/backend_config`，旧任务缺字段默认 remote；local 仅预留且 `available=false`，`GET /parse/backends` 只读暴露，`/health` 新增 `parse_backends_v1`。回归：全量 `pytest` **298 passed**（新增并发启动/新建+resume/双 resume/结构原子领取/exec.submit 失败可恢复/崩溃恢复不重发/force 旧译不假完成/旧 refined 再 refine/polish 崩溃/冻结清单/平面历史/项目索引/持续失败熔断/表格占用网格/长字段预拆/后端契约/旧 exports 保留，更新移除导出后的测试）；`tsc --noEmit` 通过；Vite 生产构建到 `/tmp/literature-opt-build`（主 JS 804.28 kB / gzip 242.52 kB；本机 Node 22.11.0 仍有 Vite 版本告警）；隔离真实浏览器 **92/92 PASS、0 脚本错误**（含旧导出路由回退与废止接口 410）；与构建脚本同参数（去掉 `--collect-all docx`）的隔离 sidecar 构建到 `/tmp/lit-sidecar/dist` 成功，`/health` 含 `parse_backends_v1`、不含 `export_readiness_v1`。**未安装/未启动客户端、未调用真实 MinerU/provider、未改真实库、未提交 Git、未做原生窗口与真实服务验收。** 尚未实施的优化见 `docs/PROJECT_REVIEW_2026-10-13.md` §4（预算预留与在途费用、预估复用、结构增量候选/缓存、结构 done 口径、分块按语言/章节约束、本地质量告警、全块窗口化与状态/审计分离）。

2026-10-13 全项目运行逻辑与效率/token/质量评审（文档交付，未修改业务代码）：新增 `docs/PROJECT_REVIEW_2026-10-13.md`，逐模块核对当前工作区并区分旧评审已修项与剩余缺口。本轮隔离验证 Python **274 项通过**、TypeScript 与临时目录 Vite 构建通过；另复现翻译并发启动创建两任务、阶段收尾前中断缺 checkpoint 导致 force 恢复重发已保存块、递归译文历史膨胀、超长单字段不可切分、图表导出字段遗漏、部分覆盖导出误报 ready、不同 locale 成品覆盖、rowspan 坐标/DOCX colspan 缺口，并测量 `/projects` 重复读取（10/50 项目分别 305/6525 次 manifest 读取）。上述发现均**尚未修复**；未调用真实服务、未读取真实 Keychain、未改真实库/安装客户端。详细证据、优化顺序和验收建议见评审与开发日志。

2026-10-13（第八轮）解析任务历史、首页分区与自动归档（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器 87/87/冻结 API 11/11/已安装）：用户要求解释首页只展示最近 3 条的规则并参照主流模式定方案。新方案（详见 `docs/PARSING_TASK_HISTORY_DESIGN.md`，90 天/1000 条为本产品自选值，非行业统一标准）：①首页「当前任务」= 运行/queued/waiting/interrupted/未归档 failed，运行置前、queued 按真实 queue_position、**失败不受 3 条限制**，每页 10、精确计数与空态；②独立「最近完成」= 过去 24h completed 最新 3 条，按首次进入 completed 的 `finished_at`（新写入且不被覆盖；旧任务回退 `updated_at` 并标注，绝不用 `created_at`）稳定排序，紧凑展示、无满格进度条、分段按需展开、「收起已完成」持久化隐藏当前 job_ids；③独立 `#/history` 路由（首页任务区按钮，可刷新恢复）：搜索/状态筛选/日期排序/每页 20/详情/关联资源；④自动归档：默认 90 天 + 常驻成功记录上限 1000，超期/超量最老优先，设置 30/90/180/永久（永久取消时间限制但仍有 1000 上限）；启动一次 + 运行中每天最多一次（持久化、重启幂等）+ 完成后超量检查；⑤未解决 failed 与所有活动/可恢复任务不得自动归档；failed 可手动归档/取消归档/重试，queued/waiting/interrupted/running 拒绝；归档不改 `job.state`、不删 job/bridge/raw/canonical/translation/exports，`migrate_project` 继续完整可用；⑥本次范围/解析页数与资源当前覆盖分开展示，不把一次 completed 写成全文完成。实现：新增 `literature/task_history.py`（独立 `archive.json` 持久化，**不污染 job.json 排序**）与 `WorkflowJob.finished_at`；`ocr.task_center/archive_job/unarchive_job/auto_archive`；API 新增 `/ocr/tasks`、`/ocr/jobs/{id}`、`archive`/`unarchive`、`archive-settings`，`/health` 加 `task_history_v1`，**保留 `/ocr/jobs` 全量契约**；前端新增 `TaskHistory.tsx` 与 `#/history` 路由，首页拆两分区并分页。Lead 复核收口：`archive_job`/`unarchive_job` 与 `retry_job`/`start_job` 共用锁序 `schedule_lock → job_lock` 并在锁内重查状态，`auto_archive` 也在 `schedule_lock` 内快照+写入，修复“并发 retry 把任务变 queued 后归档仍给活动任务加标记”的竞态；`JobArchiveStore` 改为 copy-on-write（落盘成功后才发布内存），写失败时内存/磁盘均不变且可重试。回归：全量 `pytest` **279 passed**（新增 `tests/test_task_history.py` 23 项：24h 边界、同时间稳定排序、失败可翻页、90天/1000超量/永久、每日幂等重启、活动拒归档、手动归档取消重试、旧无 finished_at、归档后证据不变、history 过滤分页；以及 Lead 复核新增的真实线程并发（archive vs retry）不变量、retry→archive 拒绝、auto_archive 不标活动、completed 不可 start/retry、archive 存储写失败回滚与重试）；隔离真实浏览器 **87/87**、0 脚本错误（含首页两区/分页/最近完成无进度条/窄屏不裁切、历史入口刷新恢复、搜索筛选分页、超期归档、手动归档查找/取消、保留期、收起持久化），首页完整截图 `docs/verification/home-sections.png`（两区+分页）与 `home-sections-narrow.png` 已人工检视；冻结 sidecar **11/11**。新 sidecar revision `0e308e432be3a38ee358` / sha256 `b0f85f4a…` / size `47709968`；Tauri 59.11 MiB 签名通过；安装前无客户端、无进程打开真实库、OCR 队列空，已备份到 `launcher/.rollback-Literature-521e540c.app` 并安全安装（真实库有 2 条无进程的遗留 running 行，下次启动会被标记失败，本轮未触碰）；**未启动客户端、未改真实库**。**未做真实 MinerU/provider/原生点击/公证验收**。详见 `docs/DEVELOPMENT_LOG.md` 同日第八轮条目。

2026-10-13（第七轮）未解析已有资源「开始解析」入口与总状态修正（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器 52/52/冻结 API 8/8/已安装）：用户截图反馈 338 页 PDF 显示“未解析”却只有“查看进度”，总状态却是绿色“已完成”。只读根因：真实项目 fdbd7397… `state=created`、无 `document/document.json`、无关联 job；`ocr.project_coverage` 对“从未解析”与“旧资源未知”都返回 `unknown`，前端 `canContinueParse`/`coverageUnknown` 均要求 `has_document`，于是落到兜底“查看进度”（`onProgress` 只是回首页）；`badge()` 缺无文档分支，最终无条件返回“已完成”。修复：`project_coverage` 对“无文档但页数已知的 PDF”返回 `unparsed+source_page_count`（有文档但无 coverage 仍 `unknown`）；新增 `MinerUService.start_parse` 与 `POST /projects/{id}/ocr/start`（这是独立的单次“首次解析”操作，**不改变既有 `POST /projects/{id}/ocr/jobs` 的仅创建契约**；在 `schedule_lock → project_lock` 锁序内完成 **Token 校验 + 活动作业/无文档/源存在且 hash 未变校验 + 创建 + 调度**，**无 Token 不创建任何任务**，创建成功但调度失败则保留可恢复的 queued 任务并报出具体错误；`page_ranges`→本地子集 `requested_pages`，语义独立于 continue）；`continue_parse` 对无文档资源明确拒绝；前端资源详情新增“开始解析”弹窗（源文件总页数、可选页码范围、模型/语言/OCR/表格/公式），无文档且无活动任务时以此替代“查看进度”；`badge`/`coverageText` 区分未开始与旧资源未知。回归：全量 `pytest` **256 passed**（新增 338 页源跨 200 页切片 200+138、338 页 created+无 job 报告 unparsed/338、并发 start 只建一个、Token 缺失零创建、调度失败保留可恢复 queued、范围解析、已有文档/源文件缺失或变化拒绝、legacy 有文档无 coverage 仍 unknown）；隔离真实浏览器 **52/52**、0 脚本错误（新增“未解析资源→开始解析 260 页→fake 自动切 200+60 两片→完成 260/260→规范文档、只建一个任务、刷新显示全部解析”）；冻结 sidecar **8/8**。新 sidecar revision `d632fe3c3f105812d64a` / sha256 `e5f88b94…` / size `47694096`；Tauri 59.10 MiB 签名通过；安装前无客户端、真实队列空且无活动任务，已备份到 `launcher/.rollback-Literature-058f5a85.app`（及更早的 `.rollback-Literature-5010db9b.app`）并安全安装；**未启动客户端、未改真实库 coverage**。**未做真实 MinerU/真实 provider/原生窗口点击/公证验收**。详见 `docs/DEVELOPMENT_LOG.md` 同日第七轮条目。

2026-10-13（第六轮）旧 page_ranges=1-N 可信前缀迁移 + 部分翻译口径（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器 44/44/冻结 API 7/7/已安装）：用户反馈 “The White Lotus Teachings” 显示“覆盖未知”且检查解析范围失败。只读根因：真实项目 bdb72fda… 源 PDF 359 页、canonical 50 页、旧 job `99335abf…` completed 且 `page_ranges='1-50'`（单 slice 1-50），但旧 `migrate_project` 对任何 page_ranges 直接跳过。本轮新增**可信连续前缀 1-N 恢复**：需源 hash 一致、`verify_raw` 无错、job/slice/document/source_files 页数均为 N，且封存切片有 `content_list_v2`/`layout.json`/`*_origin.pdf` 页数 N 的证据，才按局部第 i 页=源页 i+1 恢复（不改 block/cell ID、人工修订、译文锁定、raw）；非前缀/非连续仍保守 unknown 并给具体原因码。UI：partial 覆盖下翻译显示“已译已解析内容”并注明仅统计已解析 N/M 页，总状态仍优先“部分解析 N/M”；迁移失败文案具体化。回归：全量 `pytest` 243 passed（含 359 页源 + 50 页前缀含 5 空白页 → partial 50/359、保留旧 ID/修订/锁定译文、缺/篡改证据失败、非前缀失败、continue 51-359 切 200+109）；隔离浏览器 44/44（含旧 1-10 前缀迁移 10/30 → 继续 → fake 完成 30/30）。新 sidecar revision `5010db9b018a80003d21` / sha256 `1ce0ece4…` / size `47692064`；Tauri 59.10 MiB 签名通过、冻结 API 7/7；安装前无客户端进程、活动任务 0，已备份上一版到 `launcher/.rollback-Literature-546812b8.app` 并安全安装；**未改真实书 coverage**。详见 `docs/DEVELOPMENT_LOG.md` 同日第六轮条目。

2026-10-13（第五轮）客户端安装与原生预览验收（Orca Worker 实施，已安装/已启动/原生截图）：安装前只读确认无客户端进程、真实库活动 OCR 任务 0（7 项目：2 created/5 ready；8 job：6 completed/2 failed），旧包 sidecar sha256 `015e5d83…` 已备份到 `launcher/.rollback-Literature.app`；用 `scripts/setup_macos_launcher.sh --app-only` 安装新包（不动桌面替身），安装后 `codesign --verify --deep --strict` 通过、内置 sidecar sha256 `dd63b2cb…` 与 `manifest.json` 一致（revision `546812b8e04ec5d6d433`）。以隔离库/runtime + 假 token + 本地代理黑洞启动已安装客户端做原生 WKWebView 验收：结构校对页源文件预览**实际渲染出 PAGE 1**（截图 `docs/verification/native-review.png`），首页/资源页与覆盖状态均正常，首页显示“MinerU 连接检查失败”证明未触达外部服务。**限制**：原生顶栏导航的 AX/坐标点击不稳定，“阅读与翻译”页原生点击未完成（该页已在 Chromium+生产构建验证）；仅 adhoc 签名、未公证。详见 `docs/DEVELOPMENT_LOG.md` 同日第五轮条目。

2026-10-13 Lead 最终独立复核：全量 Python 237 项、解析关键回归单独 41 项全部通过；最终隔离浏览器脚本实际 37 条 PASS、0 脚本错误，包含真实 PDF 画布内容与缩放、部分到全文、恢复/重试、旧资源检查及 UI 调序后的实际执行顺序。已肉眼核对窄窗校对/阅读截图，并核验最终 Tauri 签名与 sidecar 哈希一致。新客户端产物已准备，尚未安装；真实 MinerU 与原生窗口交互尚未验收。

2026-10-13（第四轮）原文预览真实渲染修复（Orca Worker 实施，已编码/自动测试/tsc/构建/隔离真实浏览器 36/36 + 人工检视截图）：Lead 用 view_image 发现前三轮截图实际是结构校对页且中间显示“第 1 页渲染失败”，证明“0 脚本错误 + 容器定位通过”验收不足。根因是 pdfjs-dist 5.7.284 使用 TC39 提案方法 `Map/WeakMap.prototype.getOrInsertComputed`，当前 Chromium 与 macOS WKWebView 尚未实现，`page.render` 整页抛 `TypeError`；组件又吞错、失败后卸载 canvas、取消/重尺寸竞态导致无法恢复。已在生产代码修复：`pdfAssets.ts` 在使用 pdfjs 前 polyfill `getOrInsertComputed`，`PdfPageCanvas.tsx` 保留真实错误、canvas 常驻（失败显示覆盖层）、新渲染前 await 上一次 render task 结算且取消不判失败。验收改为强断言：目标页 canvas 存在且尺寸有效、`getImageData` 深色像素 > 0，缩放 125% 后仍绘制、无 `.pdf-page-error`；测试 PDF 写入“PAGE N”文字；实际检视 `docs/verification/review-narrow.png`（源文件预览显示 PAGE 1/2）与 `reading-narrow.png`（阅读与翻译页）。隔离浏览器 36/36、0 脚本错误；全量 `pytest` 237 passed；`tsc`/Vite 构建通过；重新 `tauri build` 59.09 MiB、签名验证通过，后端未变 sidecar 不重建（revision `546812b8e04ec5d6d433`，sha256 `dd63b2cb…`）。**尚未**：真实 MinerU/ provider/ 原生窗口点击与公证。详见 `docs/DEVELOPMENT_LOG.md` 同日第四轮条目。

2026-10-13（第三轮）Lead 复跑缺口收口（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离真实浏览器 29/29）：修复 Lead 第二轮复跑实证失败与不可用/弱断言项：①`/start`、`/retry` 先校验任务存在（不存在 404）再查 Token，测试 fixture 显式 patch 凭据，两个测试文件可独立跑；②新增 `_schedule_lock` 统一保护调度选择/领取/入队/调序/重试，调度器锁外初选、锁内重选并领取（领取前重查队首/停止标志），领取后从等待队列移除，`retry_job` 去重；③`migrate_project` 改为严格证据（源 hash、canonical source_files 对不可变 raw manifest、`verify_raw` 无错、旧作业切片与 source_files 页数一一对应），修正 page+block 原页索引，修复旧 normalize 每片局部页号重复导致 >200 页书页号重复；④资源详情新增“检查解析范围”入口接 `POST /projects/{id}/parse/migrate`，成功后可继续、失败显真实原因，删除 `continue` 中不存在的“重新解析全部页”承诺；⑤封存 reconcile 只要切片集合变化（含目录缺失）就回写 manifest，保留旧哈希不冒充新证据；⑥浏览器改为强断言：真实跑完流水线、点击校对块断言目标源页在预览可视区、刷新后 UI 显示全文、双击只建一个作业、迁移成功/失败反馈、队列 UI 调序后 fake 实际执行顺序 A→C→B、窄窗口阅读截图。全量 `pytest` 237 passed，两个测试文件单独 41 passed；`tsc --noEmit`、Vite 生产构建通过；隔离浏览器 29/29、0 脚本错误。**尚未**：真实 MinerU/ provider/ 原生客户端/公证。详见 `docs/DEVELOPMENT_LOG.md` 同日第三轮条目。

2026-10-13（第二轮）Lead 复查缺陷修复 + 全流程真实浏览器验收（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离真实浏览器 15/15）：Lead 独立复查发现前一轮存在严重边界，本轮在同一 A–D 范围内修复：①**源页定位契约**——`page_index` 回退为恒等于源文件页号（0-based、稀疏，`document.page_count` 为源总页数），修复第 5/7 页被压缩到第 1/2 页的阅读/校对/裁片错位；②**并发与原子性**——新增项目锁/job 锁/document 写锁/raw 封存锁，`continue_parse` 在项目锁内检查+创建，`start/retry/reorder` 原子化并限制合法状态；③**raw 封存事务**——改为“复制并算哈希→先写 manifest→再移动目录”，失败重试不重复、收养孤儿、重复 source 去重；④**旧任务/旧资源迁移**——`_finish_import` 按可信旧切片恢复 `source_pages/source_page_count`，新增 `POST /projects/{id}/parse/migrate`，仅按证据恢复，不可信 `page_ranges` 历史作业保持 unknown；⑤**document 写锁**——追加合并与结构补丁/撤销共用，合并后刷新翻译摘要；⑥**_parse_slice 完整性校验**——结果页数/页码不符、markdown 多页明确报错，空白页仍计覆盖；⑦**队列/恢复健壮性**——跳过无 bridge 孤儿、running 不计等待序号、shutdown join；⑧**UI 一致性**——续跑/失败同时显示本次状态与已合并 N/M 页、进行中禁继续解析、首页限制历史数量。新增 `tests/test_parsing_review_fixes.py` 17 项，全量 `pytest` 230 passed、`tsc --noEmit`、Vite 生产构建通过。隔离验收用能真正跑完整条流水线的 fake MinerU + 本机 Playwright 真实点击生产构建前端，15/15 PASS（部分 4/9→继续→全文 9/9、第 5/7 页定位 p5/p7、失败切片重试完成、中断恢复完成、窄窗口截图 `docs/verification/resources-narrow.png`、0 脚本错误）。**修正前轮陈述**：上一条中“400/409 原子守卫”当时只是先检查后操作，本轮才引入锁与指令级事务；“全部完成”也应理解为隔离 mock 与隔离浏览器验收。**尚未**：真实 MinerU、真实 provider 译文、原生客户端点击与公证。详见 `docs/DEVELOPMENT_LOG.md` 同日第二轮条目。

2026-10-13 解析任务生命周期与全文解析覆盖 + 可调序队列（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离真实 API + 真实浏览器点击 8/8）：按 `docs/PARSING_RESOURCE_LIFECYCLE_DESIGN.md` 分离“本次任务是否完成”（queued/preparing/…/completed/failed/waiting/interrupted）与“全文是否解析”（unparsed/partial/full/unknown，只计已合并进 document 的源页）。新增持久化 `ocr-jobs/queue.json` 队列（置顶/上移/下移、重启保留、单文档串行不抢占）、`interrupted` 中断恢复、启动/重试的 400/409 原子守卫；继续解析以“不可变分批 raw 封存 + 仅追加未覆盖源页合并”实现，旧 raw 哈希、block/cell ID、人工校对、译文与锁定、资产引用不变；选页改为本地压缩子集 PDF 并记录 `SliceJob.source_pages`，修复原 `page_ranges` 切片把源页错映射为 1..N 的缺陷，支持非连续范围与 >200 页自动二次切分，重叠页过滤且不覆盖已有页。新增 `POST /projects/{id}/ocr/continue`、`GET /ocr/queue`、`POST /ocr/jobs/{id}/queue`，`/health` 新增 `parse_lifecycle_v1`；`/projects` 与 `/ocr/jobs` 下发 `parse_coverage`/`requested_pages`/`queue_position` 等。新增 `tests/test_parsing_lifecycle.py` 17 项，全量 `pytest` 213 passed、`tsc --noEmit`、Vite 生产构建通过。隔离验收用 `tests/manual/parse_ui_browser_check.py`（假 MinerU、不读 Keychain、不发外部请求）以 Playwright（Node）真实点击生产构建前端，8/8 PASS：部分解析展示、详情继续解析弹窗、提交后创建继续任务且不覆盖已合并页、首页队列与置顶调序。**尚未**：真实 MinerU（压缩子集远端页码回传、>200 页真实计费）、真实 provider 译文、原生客户端点击与公证。详见 `docs/DEVELOPMENT_LOG.md` 同日条目。

2026-10-14 翻译提速四项（已编码/自动测试，未真实验收）：承接与 TBL 的比对结论。①翻译任务默认关思考：`with_thinking_disabled()` 按所声明协议改写（qwen/glm→off、openai_effort→minimal，显式 on/high 尊重用户，未声明协议不发未知字段），整本书/单块译审/即时翻译三链路默认生效；②AIMD 并发自适应 + 上限 8→16：429 减半、连续 3 块成功 +1 探测回升（不超过用户上限，整块 429 完全失败也不计入熔断、子批 429 经 error_sink 上传），并修复 `RequestCoordinator.default_service_limit=4` 从未调高的隐性瓶颈（`raise_service_limit` 只升不降）；③prefix caching：术语表从按块过滤的 user prompt 改为整份稳定排序注入 system 前缀（prompt 契约 `translation-v3`），同一任务内 system 逐字节一致可命中自动前缀缓存；④分块预算字符→token（`DEFAULT_CHUNK_TOKENS=900`，按源语言比率 en≈4/zh≈1.6 字/token），英文块变宽减少请求数、中文块变小降截断。已验证 433 项 Python 测试（含评审复现回归：限流突发不熔断）、`tsc --noEmit`、Vite 生产构建。尚未：真实 provider 验收（关思考后 reasoning_tokens 应≈0、cached_tokens 命中率、AIMD 真实 429 曲线）、sidecar/Tauri 重建安装。详见 `docs/DEVELOPMENT_LOG.md` 同日第二十一轮条目。

2026-10-12 恢复语义与提交终态修复 + 客户端重建安装（Orca Worker 实施，已编码/自动测试/冻结端到端/安装启动，未做真实服务验收）：修复 Lead 隔离复现的两个边界——①按 checkpoint.pass 跳过初译导致 polish 部分失败恢复后不再补初译；②任务期间源文变化时旧结果只存候选但 job 伪报 completed、counts 漏计源变。现在待初译集合按“真实保存的有效结果”判定，恢复先补初译再补润色且已完成不重复计费；`_source_changed_ids` 使源变旧译不计完成并归入 failed，`completed/failed/locked/unhandled` 四类互斥且和恒等于 target。新增 8 项故障注入回归（阶段错位恢复、源变终态、截断父子不伪完整、15 字段表格不丢格、超长单字段不存半句、润色失败恢复、真实 API partial_failed→resume→completed），共 196 项 Python 测试通过。冻结 sidecar revision `97186a2ca9645671c822`、sha256 `015e5d83…`、size `47650512`；安装前用冻结后端 + 本地回环 mock 服务隔离验证 revision/能力/翻译流程；Tauri 包 59.06 MiB，签名验证通过；已安全安装到 `launcher/Literature.app`（安装前无客户端进程、真实库无活动任务），启动后 `/health` revision 与 manifest 一致、能力齐全、前端连接正常。尚未：真实 provider 质量/速度/费用、超长单字段预切分、思考开关/预算、原生点击与公证验收。详见 `docs/DEVELOPMENT_LOG.md` 同日条目。

2026-10-12 翻译失败审计与子请求隔离（Orca Worker 实施，已编码/自动测试/前端构建，未做真实服务验收）：按 `docs/TRANSLATION_TASK_DIAGNOSIS_2026-10-12.md` 推荐顺序第 1–2 步修复。要点：部分失败改终态 `partial_failed` 并在错误栏归类失败原因（不再 completed+空 error）；`TranslationEntry` 新增 `error` 字段并随 API 载荷下发，失败列表/当前块显示原因，成功与人工编辑清空；子批请求改走异常隔离，一侧超时/报错不再作废父请求与兄弟批已解析的字段；`ProviderError` 可携带响应，只返回思考内容等错误路径仍记账真实 usage；`_chunks` 增加 12 字段上限（失败仍沿字段边界缩批）；reasoning 占输出 ≥60% 的截断给出可执行原因。

收口后的任务计数契约：`_task_accounting` 以 checkpoint 原始 `target_ids` 定义任务目标（去重），初译/润色阶段分开计数：`counts.target_blocks` 为任务目标唯一 block 数，`counts.translate.{target,completed,failed,locked,unhandled}` 四类互斥且恰好划分 target（优先级 completed > locked > failed > unhandled；已完成目标保持 completed，locked 只计尚未完成而被跳过的目标），`counts.translate.source_changed` 为叠加标记；`counts.polish.{target,completed,failed}` 独立计数，不与初译相加（修正“恢复只剩剩余范围漏计”与“润色重复计同一 block”）。`failed` 只含当前仍失败（本轮新失败去掉后来成功/锁定），跨恢复用 checkpoint 的 `translate_failed`/`polish_failed` 累计；`_save_checkpoint` 改为合并 result，不覆盖已写入的 counts。已验证 189 项 Python 测试（含计数契约回归：暂停后目标保持完整、恢复后 completed 全覆盖、润色不算入初译、润色失败只计 polish、checkpoint 不丢 counts、已完成+锁定不重叠且四类之和等于 target）、`tsc --noEmit`、Vite 生产构建。尚未：逐请求审计流水、超长单字段临时子片段、思考开关/预算配置（待小样本确认中转是否透传）、真实 provider 重试 25 段、重建并停机更新客户端。详见 `docs/DEVELOPMENT_LOG.md` 同日条目。

2026-10-12 最近翻译任务诊断（只读检查与隔离模拟，未修改业务代码/真实库/安装包）：`On Being Authentic` 最近失败范围作业 `ec29fb0…` 重试 48 段，23 成功、25 失败，耗时约 180 秒但仍记 completed/100% 且 error 为空。25 段集中在第 101–102 页注释，同批 2302 字符/25 字段；已记账响应中 reasoning 占输出约 88%–90%。响应超时为较高可能性推断，因旧版未保存失败原因而无法确认。隔离复现当前源码的子请求异常连带丢失已成功结果、仅思考响应 usage 遗漏；已安装 sidecar SHA 对应 10 月 11 日 revision `95fe704229347b9c3c75`，今天源码修复尚未进入安装包。相关 44 项自动测试通过；未重发外部请求。证据、方案和验收顺序见 `docs/TRANSLATION_TASK_DIAGNOSIS_2026-10-12.md`。

2026-10-12 F05 恢复契约（Orca Worker 实施，已编码/自动测试，未做真实服务验收）：新增持久化 checkpoint（`target_ids`/`pass`/`translate_completed`/`polish_target_ids`/`polish_completed`），恢复时读 checkpoint 而不重按 scope 筛选；修正 `scope=untranslated` 恢复后零请求却 completed、`scope=book, force=True` 恢复重译已完成块两个边界；润色只覆盖本次任务范围内条目。已验证 177 项 Python 测试（+5 项恢复回归，含 HTTP resume 端到端）。F02 为保正确已暂退为失败块整块全字段重发（放弃只补缺字段的性能优化）。详见 `docs/DEVELOPMENT_LOG.md` 同日 F05 条目。

2026-10-12 第一轮收口（Orca Worker 实施，已编码/自动测试/前端构建，未做真实服务验收）：按 Lead 复查补齐完整性与兼容性——源 hash 变化/force 不再复用旧译文（失败块未校验片段只存候选，`TranslationEntry.partial` 标记）；`_commit_translation` 写盘前复查 canonical 源 hash，不一致只存候选不冒充当前完成；新增 `align_structure_only` 并在结构补丁/撤销后即时写回对齐，`translate_stage` 改用 `stale_source` 判定，卡片与阅读一致；`/health` 新增 `translation_safety_v1`、`export_readiness_v1` 且前端强制校验；导出策略更名为 `keep_source`/`mark_only`，`readiness.ready` 要求资源齐全，ExportPanel 换项目重置语言与产物。已验证：172 项 Python 测试（含本轮新增 6 项边界回归）、`tsc --noEmit`、Vite 生产构建。详见 `docs/DEVELOPMENT_LOG.md` 同日“第一轮收口”条目。尚未：真实 provider/MinerU/原生客户端验收；F07/F11 大范围/F12 未展开。

2026-10-12 第一轮翻译与导出闭环加固（Orca Worker 实施，已编码/自动测试/前端构建，未做真实服务验收）：承接评审报告第一轮（F01–F06、F08–F10），详见 `docs/DEVELOPMENT_LOG.md` 同日条目。要点：译文层按项目+语言加写锁并改成“只合并本次触碰条目”的提交，保留运行中人工编辑（模型结果存为候选）以及即时附属记录与术语表；字段级切分结果按 segment id 深合并并做整块完整性校验，空/重复/未知 id 与 `finish_reason=length` 不再判完成；纯层级变化自动对齐、零模型请求；润色阶段暂停正确落为 paused 并从润色继续；导出绑定真实语言、缺译显式标记并可预检完整性；前端改为按活动任务/版本增量刷新、编辑草稿独立并提示冲突、划选原文只浮出按钮且点击才请求、按 cell/item/caption/footnote 编辑。后续收口见上一条（失败块补缺语义已改为只存候选）。尚未：真实 provider/MinerU 端到端质量与速度、原生客户端验收；F07/F11/F12 及报告其余后续清单未实施。

2026-10-12 全项目产品与工程评审（评审交付，未修改业务代码）：按 AI 速度/质量、全流程可用性、用户参与和自动化四项目标检查当前工作区，报告见 `docs/PROJECT_REVIEW_2026-10-12.md`。151 项 Python 测试、TypeScript 检查与 Vite 生产构建通过；隔离诊断复现翻译覆盖运行中人工编辑、字段切分重试丢单元格、空 seg/截断信号被误判完成、纯层级变化仍入重译范围、润色暂停误报完成等边界。提出先修完整性与提交安全、再优化 AI 调度、最后简化配置和补齐交付的建议；第一轮修复见上一条。未调用真实 MinerU/模型，未操作用户文献库或安装客户端，真实质量/速度与原生交互仍待验收。

2026-10-11 文件资源管理模块前后端打通（按已确认设计稿）：按 `docs/FILE_RESOURCES_DESIGN.md` 与原型 `docs/prototypes/file-resources-v4.html` 重写文件资源前端，并补齐后端阶段摘要。后端 `GET /projects` 与 `GET /projects/{id}` 新增 `review`（待确认数）与 `pipeline`（校对/翻译/导出的独立状态）；新增 `structure_review_summary`、`translate_stage`；`/health` 新增能力 `resource_pipeline_v1`；结构建议决策、补丁新增/撤销、本地校准与结构 AI 作业完成后都会刷新译文摘要，使「先翻译后校对」的 stale 立即回到列表；导出计数改为把 html/md+json 目录也计入。前端按原型实现网格卡片（类型实底图标 + 四格阶段栏 + 概览六维筛选 + 卡片 ••• 菜单）、表格化列表就地展开、居中详情弹窗、翻译范围弹窗（全书/仅未翻译/仅待复核/仅重试失败/选定章节），并把详情动作接到既有校对/阅读/导出工作流与 `POST /translation/jobs`。已验证：151 项 Python 测试、`tsc --noEmit`、Vite 生产构建；真实本地 API + 隔离种子库 + 真实浏览器逐项点击（卡片、筛选、详情弹窗、列表展开、翻译弹窗含 stale 范围、跳转结构校对、结构补丁后 stale 回显）0 控制台报错。已重建冻结 sidecar（revision `95fe704229347b9c3c75`，sha256 `0734660b…`）与 Tauri 包（59.04 MiB，签名校验通过）。尚未：已安装的 `launcher/Literature.app` 仍在运行，按安全约定未在运行中替换，需要用户退出后安装并启动；原生窗口逐项点击与真实 provider 翻译仍待验收。

2026-10-11 文件资源管理模块设计定稿（原型，未实现）：确认「提取完成后 校对/翻译/导出 三条独立工作流」的产品模型，以及网格弹窗 + 列表栅格就地展开的交互与视觉规范。交付 `docs/prototypes/file-resources-v4.html` 与 `docs/FILE_RESOURCES_DESIGN.md`（含设计 token、组件规范、代码映射、后端数据契约、实施 checklist）。本轮未改动业务代码；打通时后端只需在 `/projects` 补校对待确认数聚合，翻译摘要与 stale 范围接口已存在。
2026-10-11 翻译引擎 v2（对齐 TranslateBooksWithLLMs 思路）：用户实测发现旧翻译模块又慢又差，且实测项目里有一段译文在 700 字处断在 “...including the American” 半句上却被记为已翻译。根因是 12000 字符/块的整块严格 JSON 校验（错一个 id 整批失败）、无截断检测、prompt 无翻译原则、串行请求且每请求新建连接。本轮重写引擎：2400 字符小块 + 前后文 + 上一段译文衔接；输出改为 `<src>/<seg>` 段落标签协议，宽松解析、缺段只重试/失败该段、截断（`finish_reason`）切分重试、连续失败熔断；滑动窗口并发（默认 4，可调 1–8）+ `httpx.Client` 长连接复用 + 429/5xx 退避；术语表按块过滤；学术翻译原则 prompt（专名一律中译、完整性优先）；可选两遍翻译+润色（`polish`，默认关）；CJK 后处理；整层写盘节流。保留对照翻译、编辑锁定、单块重译/润色、stale、范围选择、术语表版本与 usage 账本。新增能力标记 `translation_engine_v2`、`TranslationJobInput.polish/parallel_workers`。已验证 150 项 Python 测试、`tsc --noEmit`、Vite 生产构建、真实本地 API + mock 模型（41 块 / 2 分块并发完成；不守协议的模型 2 次请求即判失败）与冻结 sidecar（revision `28201d29c74fe4916f8e`，能力含 `translation_engine_v2`）；已重建、签名并安装到 `launcher/Literature.app`（59.02 MiB，sidecar sha256 `83b01f91…`），启动后 `/health` 与日志确认新能力生效。前端范围弹窗新增并发数、自动润色与“强制重译已有译文”。尚未用真实 provider（yellowbee/qwen3.8-flash-next）跑整本书，真实质量/速度/限流与费用、原生窗口逐项点击仍待验收。

2026-10-07 翻译模块前后端打通与客户端重打包：已安装客户端的旧内置后端（`7557bb0a`，2026-10-04）缺 `translation_module_v1`，前端打开阅读与翻译报“前后端版本不一致”，这是先前“没打通”的真实原因。本轮修复 stale 语义（`source` 原文已变为待复核可重译 / `structure` 仅层级调整为自动对齐无需重译）、锁定块 stale 漏报、补齐“仅待复核段落”范围入口、增加单项目翻译并发保护；重建 sidecar 与 Tauri 包并安装，revision `124e1fe574069b18c210`，sidecar sha256 `f15debdc…` 与 manifest 一致，签名校验通过，启动后前端成功调用翻译接口。已验证 144 项 Python 测试、TypeScript 与生产构建、真实本地 API + mock 模型流程、冻结 sidecar 翻译作业。未调用真实翻译模型，未做原生窗口逐项点击验收，并发/速率限制与花费硬预检仍未实现。

2026-10-06 翻译模块 M3 可用版（后端 + 前端打通）：新增 block 级 `structure_revision`，结构校对改到某个块就让该块 +1，译文条目记录依据版本，不一致即 stale（locked 条目保留但进入待复核列表）。翻译范围支持 全书/未译/待复核/失败/选段；译文层接口返回每块 state/stale 与统计；新增预估算接口、术语表版本与回滚、项目列表翻译摘要。前端保留原三栏布局，新增目标语言选择、范围选择器与计数、进度与统计、单块翻译/重译/润色/锁定、术语表版本回滚、stale 提示。已验证 141 项 Python 测试（新增 9 项）、TypeScript 与生产构建通过，并用真实本地 API 进程 + 隔离库冒烟了统计/预估/术语表版本/项目摘要接口。未调用真实翻译模型，未做浏览器点击验收，未重打包客户端；真实 provider 的费用与译文质量、并发/速率限制仍未验收。

2026-10-04 结构模型超时修复：Journal 作业 `f309c6…` 第一规划窗口包含 160 个候选、44,749 字节文字输入，无图片；服务未返回响应头，触发 180 秒 ReadTimeout。现规划窗口最多 64，保留整体 overview/目录/跨批祖先与原标题文字，并逐批显示进度。结构请求响应等待默认 600 秒、用户可设 30–1800 秒；连接等待仍短限时，错误显示阶段与类型，审计保存耗时。再次校准失败任务可复用文档/模型/输入一致的有效已完成响应，不重复 usage，不自动重发超时。132 项 Python 测试、类型检查与生产构建通过；冻结后端+生产浏览器离线验证失败→用户重试→复用 2 项→仅重做复核、用量无重复、0 脚本错误。已停机安装并重开客户端，revision `7557bb0a476f8584a731`。未重发真实模型任务；较小窗口和长等待能否改善该服务仍待实跑，不保证远端不会超时。

2026-10-04 运行中更新导致的解压错误修复：Journal 作业 `016870…` 在本地视觉模块 import 阶段失败，未进入模型请求。日志与进程时间证实客户端 22:57 启动后仍运行，00:06 安装替换了 PyInstaller 可执行归档，旧进程使用旧偏移读取新包。安装脚本现于复制前和替换前检查目标客户端/后端，运行中拒绝更新。真实验证运行中拒绝、完全退出后安装成功；已正常重启客户端。已安装冻结后端在隔离合成 PDF 上通过目录整页和标题裁片渲染（2 个 PNG、raw 校验有效），无外部请求。原任务保持失败供用户重试，不自动重发 AI。

2026-10-04 修订来源显示修正：修订历史按实际来源区分“人工调整”“采用 AI”，自动采用保持“自动采用”。单项/批量采用建议记录 `source=ai, automatic=false`，手动编辑默认 `source=user`；无 suggestion_id 的兼容采用入口也传递来源。125 项 Python 测试（扩展 API 回归）、TypeScript 和生产构建通过；自包含客户端已重建安装，签名校验通过，revision `0a907b0dbf2a480470b9`。旧记录若曾将采用来源保存为 user，无法可靠还原，本轮不猜测迁移。未调用外部服务，未重复原生窗口点击验收。

2026-10-03 Journal 任务复盘与 AI 整体结构推理（本轮当前状态，覆盖旧管线描述）：只读检查 `The_Metropolitan_Museum_Journal_v_45_2010` 已完成作业 e6a172…，发现 147/147 角色回答中 66 个 h2、只有 1 个 h3；本地目录把作者串入下一篇标题、14 篇全推断 H2，固定 40 条独立复核缺少篇章与父子上下文。现改为 `hierarchy-v2`：AI 核对源目录（本地提取仅为草稿）→有界整体标题树规划（文献类型、目录范围、根层级约定）→保留跨批父标题/相邻标题的逐项复核→检查最终阅读顺序下的父子关系。全部现有标题参与模型审查，无目录也可推理；弱本地规则和置信度不再抢占模型结论。免费本地入口保留。模型修改全部人工确认，非法/缺失/跨篇关系不产出层级建议；界面分别展示角色回答与父子关系检查数，并显示建议的所属标题。

- 已编码与自动验证：125 项 Python 测试、TypeScript、Node v24 生产构建通过；新增四级无编号层级、同级父子、跨篇挂接、跨批上下文、规划覆盖率/重复/非法值、无目录召回、持久化采用/撤销与证据不变等验证。关系检查是形式一致性，不是语义准确率。
- 真实服务验收：本轮未调用模型或 MinerU；新版真实模型效果、费用和原生交互仍待验收。已准备第 4 页目录 + 231 个短候选快照，请求用户授权隔离试跑；未收到授权前不发送、不修改原项目。
- 已交付客户端包：`launcher/Literature.app` 已重建并安装，签名/sidecar 哈希通过，revision `66851b90aca4cd887701`；实际冻结后端与生产 Chrome 在隔离离线模型上跑通三个阶段、关系计数和建议父标题显示，0 脚本错误。原生窗口点击未验收。详细诊断见 `docs/JOURNAL_STRUCTURE_REVIEW.md`。原文献与旧作业只读保留。


2026-10-03 结构校准二次探索与前一轮交付（历史阶段记录）：在 Literature 之外用 White Lotus 前 50 页和 Making Transcendents 前 20 页的真实 `md + content_list_v2.json` 副本比较策略，发现“有序统计却独立出建议”、图表目录混入章节目录、目录行边界丢失、推断层级冒充确认基准、被过滤页眉无法提供重复证据等问题。现本地建议统一来自有序对齐，排除已识别题注/脚注/页边文本误提升，旧项目只读 raw 补充页眉证据；目录抽取结果为草稿，用户确认后才能保存确认基准。AI 改为 `roles-v1` 逐候选返回角色（含不变项），每批 40 条短候选、漏答/非法结果显式报告覆盖率，网络/鉴权失败不拆批，所有新角色模型建议待人工采用。界面支持误标清理/层级/漏标分组，批量只采用当前组，其余建议保留；本地建议也持久化。

- 已编码与自动验证：111 项 Python 测试、TypeScript、Vite 生产构建通过；独立副本产出 White Lotus 9 处、Making 2 处结构修改，原文字/顺序/bbox 和 raw 哈希不变。
- 已隔离联调：真实 API 本地校准、6 条层级组采用后 3 条清理组保留、精确撤销与待处理决策重开保持；离线模型模拟复核 14/14 与 4/4 候选（不证明真实模型准确率）。浏览器已部分跑过目录草稿、模拟任务与建议分组，之后嵌入浏览器阻断回环请求，未完成最终全部交互验收。
- 已交付客户端包：重新构建自包含 sidecar 与 `launcher/Literature.app`，签名/哈希核对通过；实际包内冻结后端离线导入、65 条目录抽取、9 条建议入库与 raw 校验通过，revision `0c1a6853c2a0cd6ec397`。
- 尚未真实验收：本轮具体文档候选发送到 yellowbee 服务的试跑被自动审批拒绝，用户对准备好的 15 条候选快照/目的地许可尚待回复；没有发送，模型准确率与费用未知。原生窗口逐项点击、全书人工基准、多样题注/脚注误标与无目录漏标评测仍待完成。历史真实模型结果不能替代新版角色契约验收。详情见 `docs/STRUCTURE_CALIBRATION_EXPERIMENTS.md` 与 `docs/STRUCTURE_REVIEW_DESIGN.md`。

2026-10-03 AI 结构校准升级（本轮，方案探索 + 落地）：先在 Literature 之外用两个真实 MinerU 样本做端到端实验，确定“本地信号优先、目录全局有序对齐、模型只处理疑难项”的分层策略，并沉淀为独立 skill `~/.agents/skills/mineru-structure-calibration/`。落地到 Literature：新增 `literature/structure_signals.py`（跨页书眉/页脚/页码/题注/脚注识别 + 印刷目录抽取 + Needleman–Wunsch 有序对账），`analyze_structure` 自动降级书眉，`reconcile_toc` 改为全局有序对齐，模型候选附带本地信号，作业优先本地解析印刷目录（无模型费用）并把每批压到 40 条、输出截断时对半重试，新增 `GET /structure/local-toc` 与前端“本地读取目录 · 0 费用”。已修复编号前缀误配脚注、MinerU 标记污染标题、越批建议等真实缺陷。已验证：92 项 Python 测试、完整作业链路真实模型验收（Rhetoric 样本本地抽 104 条目录、32 条候选单批、0 条被拒、自动应用 31 条，usage 8970/9214）；前端类型检查/构建因本机 PATH 无 node 未跑，原生窗口与打包验收待做。详见 `docs/DEVELOPMENT_LOG.md`。

2026-10-03 结构校对 AI/手动校准管线修复（执行者，本轮当前状态）：已定位并修复阻塞 AI 校准的根因——既有项目的 `literature.sqlite` 缺少上次提交新增的任务列，结构 AI 作业一创建就卡在 `queued`；`ProjectStore.project_database()` 现在按项目幂等迁移，启动时 `recover_interrupted_jobs()` 把上次进程遗留的 queued/running 项目作业标为失败，不再无限重连。同时完成 `docs/STRUCTURE_REVIEW_ACCEPTANCE.md` 的 P0：批量结构修改与自动应用改为“先全部校验、再一次性保存”的原子提交；结构建议携带生成时块状态，采用时按块拒绝陈旧覆盖；手动结构动作加提交锁；已确认目录基准与建议的采用/忽略决策均按项目存入项目数据库。已验证：87 项 Python 测试、`tsc --noEmit`、重新构建的冻结 sidecar 在隔离库跑通带视觉的 AI 作业、本地校准、批量原子、陈旧拒绝、基准往返与建议决策重启保持；前端已重建并随完整打包重装至 `launcher/Literature.app`（启动后 `/health` 返回 c85e4dc0…）。尚未用真实模型/MinerU 验收，原生窗口逐项点击与 Developer ID/公证验收仍待进行。详见 `docs/DEVELOPMENT_LOG.md`。

2026-10-03 结构校对产品核查：已修复手动目录基准无法切换的入口、目录格式反馈、多选只读块计数、批量修改后旧 AI 建议残留与重复标题选项。类型检查、生产构建、41 项结构模块回归及离线 API 模拟的 Chrome 交互通过；未提升真实模型或原生客户端验收状态，未重打包客户端。待处理建议持久化、批量原子性、人工采用时版本校验与手动提交锁仍需继续，具体执行与交付标准见 `docs/STRUCTURE_REVIEW_ACCEPTANCE.md`。

2026-10-03 客户端交付转换（本轮当前状态）：

- 已恢复并完成 Tauri 发布构建，`launcher/Literature.app` 内置 React/PDF.js 工作区和 PyInstaller 后端；当前约 59 MiB，Apple Silicon，最低 macOS 15，运行不依赖 Python/Node/Vite/源码。
- 独立动态端口、随机会话令牌、能力与鉴权检查、单实例聚焦、退出进程组清理和 App PID watchdog 已接入。启动失败有持续可见的错误与重试入口。
- 原生多文件选择/拖入通过元数据读取后直接按本地路径导入，省去浏览器上传副本；逐文件 PDF 页码和额外格式保留。
- 设置可查看/定位资源库、运行数据和日志，选择另一资源库后重启使用。当前机器保留原 `library/`，新机器默认应用数据目录；不自动搬移旧文献。资源卡片可 Finder 定位，导出有结果路径与定位，EPUB/DOCX 及原件可另存到库外。
- PDF 解码器、ICC、CMap、标准字体和许可证由锁定依赖自动进入构建，清除了手动静态副本、未引用 PdfPage 和旧浏览器启动链。新增固定允许列表缓存清理工具，已清理 2909.5 MiB（约 2.84 GiB）可重建产物；文献库/回收站/密钥配置不作为冗余资源。
- 已验证：57 项 Python 测试、2 项 Rust 测试、类型检查及生产构建；真实打包后端的鉴权、PDF 元数据、离线规范化、四种导出与 raw 哈希；实际客户端单实例及 App 结束后 5 秒内两级 sidecar 回收；adhoc 签名校验。
- 尚未：原生窗口 AX 自动化持续超时，文件选择、拖放、另存、Finder 按钮的真实点击和客户端 PDF 解码视觉验收尚未完成；未做 Developer ID、公证、Intel/干净机器安装。本轮未提交真实文献到 MinerU 或调用模型；首页初始化曾成功检查已有 MinerU Token 连通性。
- 下面的 WebUI/启动器段落为历史实施记录，以本段的客户端安排为准。具体目录与清理规则见 `docs/RESOURCE_MANAGEMENT.md`。


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

2026-10-03 结构校对简化与视觉校准（M2）：

- 三栏明确为文档目录／源文件预览／结构编辑，移除翻译范围勾选和技术处理状态；层级选择集中到当前块的正文／H1–H6 下拉框，可见多选框支持批量调整。建议展示标题与修改目标，可采用或忽略，历史按逆序安全撤销。
- 源 PDF 按实际页数预览，缺 bbox 时也可浏览原件；新增页码跳转、适应宽度／缩放、结构框开关。三栏在窄窗口遵守最小宽度并用完可用空间。
- AI 从明确选择的源目录页建立独立基准，当前浏览器按项目保存该基准，不再从可能错标的解析目录生成基准。用户可复用目录或执行免费本地校准。
- 新增 PDF 视觉输入：限定目录原页与代表性标题裁片；OpenAI 兼容模型服务支持图像内容项。目录匹配先本地解决，正确标题跳过模型，疑难候选按 80 个分批；支持疑似漏标短标题并保持人工确认。
- 修复关闭自动应用后高置信建议不显示、旧修订撤销覆盖新修订、分析期间文档改变仍回写等问题；限制图片／表格被标题操作误改。增加 structure_vision_v1 能力标记。
- 已验证：55 项 Python 测试通过，TypeScript 类型检查与 Vite 生产构建通过；隔离 PDF 的真实 Chrome 交互与桌面／1024px 截图通过，没有页面脚本错误。OpenDesign 交互稿使用演示数据。
- 尚未：真实视觉模型、实际扫描书准确率及费用验收；服务端视觉缓存、无目录且无编号标题识别、完整逐批输入审计和任务恢复。当前输入审计是启动计划，目录提取后的动态批次仍需进一步记录。详情见 docs/STRUCTURE_REVIEW_DESIGN.md。

2026-10-03 结构校对：MinerU 文本结构类型与本地优先 AI 对账（本轮）：

- 可修改结构类型对齐 MinerU 归一化后的文本结构：标题 H1–H6、正文、列表、脚注（对应 MinerU paragraph / list / page_footnote 等）；图片、表格、公式、代码保持只读。“设为列表”会把正文搬入 items，补丁记录完整内容快照，撤销精确还原。
- AI 自动校对重建为“本地优先两阶段”：`reconcile_toc` 先把已确认目录逐条对账到文档结构块，产出 已定位／层级修正／疑似漏标／未定位／未收录标题 摘要；只有本地未解决的疑难项才进入模型，模型只处理候选并按 80 条分批，图片只带本批标题裁片。
- 冲突与校验：本地精确自动定案优先，其余取置信度更高者；模型编造／重复 block、非法置信度（NaN／Infinity／布尔／越界）与非法层级（小数／越界／类型错误）一律跳过或拒绝；被跳过的建议会写入警告与审计（`skipped_model_suggestions`），不默认为“全部建议已验证”。
- 明确选择目录页时只排除所选页，不再仅凭本地候选把正文页误排为目录；`proofread=False` 不生成任何标题候选／裁片；源目录提取后按动态疑难候选重新生成裁片。
- 自动应用边界不变：只自动执行精确匹配的标题层级修正与“标题降为正文”，补回标题始终人工确认；`StructureJobInput.auto_apply` 默认 False。
- 前端：结构下拉框扩展到 正文／列表／脚注／H1–H6；AI 面板把“提取目录＋校对”合并为单一主按钮，移除冗余的“从源目录建立基准”勾选，自动应用、视觉与手动基准收进选项，并显示目录对账摘要。
- 任务恢复加固：同帧防重复 POST；离开后重连运行中作业，读取失败保留当前任务并重连；已完成作业仅在从未回填且作业记录的 `document_updated_at` 与当前文档一致时恢复建议（不一致则提示重新校准），避免已忽略／已采用建议复活或回填过时建议。该消费标记只防重复回填，不等同于完整人工决策持久化：应用重启后尚待处理的建议不会自动恢复。后台失败错误保留在任务面板。
- 已验证（本轮）：77 项 Python 测试通过（新增结构类型可逆、置信度／层级校验、模糊匹配人工、失败 JSON 仍记 usage／审计、批次输入隔离、extract-only 无标题图、明确目录页不误排正文、对账推广保留裁片、冲突合并、跳过建议本批 rejected_count/reasons 显式告警、结果记录 document_updated_at 且后续改结构能被检测）；TypeScript `--noEmit` 与 Vite 生产构建通过（用本机可用 node 运行时，未装新依赖）。
- 已验证（本轮）：重新构建 Python sidecar 与 Tauri 发布客户端并更新 `launcher/Literature.app`（约 59 MiB，Apple Silicon，最低 macOS 15）；`codesign --verify --deep --strict` 通过；包内 sidecar 与 `src-tauri/binaries/manifest.json` 的 sha256/大小一致（05da37f9…，47.55 MiB）。冻结 sidecar 在隔离库（禁用真实凭据、不调用模型）实际跑通：`/health` 能力与 `runtime_revision` 47b98b95… 与构建清单一致，离线 MinerU content_list_v2 导入、document、`/structure/analyze`（本地 set_level）与 `/structure/plan`（对账摘要）均 200。
- 尚未（本轮）：真实视觉模型与实际扫描书准确率／费用验收；服务端视觉缓存；无目录且无编号的漏标识别；人工决策持久化仍是最小实现。原生窗口 AX 自动化仍不可靠，未做原生对话框/拖放/另存的实际点击与视觉验收。

尚未完成或尚未真实验收：

- 真实浏览器交互验收本轮未执行：新模块只通过 `tsc --noEmit`、Vite 生产构建、pytest 与真实 API 只读探测；JBIG2/JPEG2000 扫描件的原页渲染同样只做到资源可达，尚未肉眼验收。
- 迁移前生成的旧项目虽然 bbox 显示已正确，但 `pages[].width/height` 仍为 0，PdfPane 退回用 PDF 自身首页宽高比占位；需要一次「从封存 raw 重建规范文档」的入口才能真正补齐（本轮未实现）。
- 使用真实 MinerU Token 对多种格式进行在线 smoke test。
- 大型非 PDF 的本地自动切分；当前依赖 MinerU 转换并反馈限制。
- 真实模型上的目录校对、全书翻译与费用验收。
- 项目回收站浏览与一键恢复；当前删除数据可恢复，但恢复需在本地目录中手工完成。
- 完整响应式设计、部署、账号体系和远程多用户隔离。

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
