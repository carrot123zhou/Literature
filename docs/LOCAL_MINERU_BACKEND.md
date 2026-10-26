# 解析引擎接入（MinerU：云端 / 本地引擎 / 已有安装 / 已有服务）

日期：2026-10-25。状态：**已实现并隔离验收；本机已有引擎（TextFold 布局）与自建 V1 服务已完成真实端到端解析验收**（详见 `docs/DEVELOPMENT_LOG.md` 2026-10-25 条目）。
本文件是解析引擎接入的**唯一现状文档**；取代 2026-10-13 的「本地 MinerU 后端接入口径（预留）」。

## 1. 四类接入方式

| 来源（kind） | 设置入口 | 说明 | 可管理操作 |
|---|---|---|---|
| `mineru_remote`（云端 API） | Token 设置 | MinerU 精准解析 API，默认方式，原有全部能力 | 连接配置/Token |
| `managed`（独立安装） | 设置 → 文档解析 → 下载安装 | Literature 自管：uv 引导托管 Python 3.12 venv → `mineru==4.0.10`（pinned）→ `mineru-kit models download --tier standard`；安装位置 `<runtime>/mineru-local/` | 安装/取消/修复（续装）/卸载 |
| `external_cli`（已有安装） | 设置 → 文档解析 → 使用已有安装 | 通用路径探测（TextFold/PaperFold 仅是自动发现候选，不是特判）：目录含 `venv/bin/mineru-kit`（或直接指向 kit 二进制） | 连接/检测/断开；**不升级、不 pip 安装、不修复、不清理、不卸载** |
| `http_service`（已有服务） | 设置 → 文档解析 → 连接已有服务 | 官方 mineru-api 的 **/v1 自托管协议**（`/v1/health` `/v1/tiers` `/v1/uploads` `/v1/parse/jobs` `/v1/files`）；与官方云端 API 严格区分，不声称任意版本兼容 | 连接/检测/断开；不部署、不管理远端 |

- 来源选择持久化在 `<runtime>/parse-engine.json`；`GET /parse/backends` 的 `mineru_local` 能力按当前来源真实探测结果动态返回，验证不通过即明确不可用并给出原因，不提供伪入口。
- 设置界面三入口分别为「下载安装 / 使用已有安装 / 连接已有服务」；`external_cli` 与 `http_service` 不提供卸载/修复按钮。
- 兼容判定：CLI 引擎仅接受 **MinerU 4.0.x**（与已验证的 4.0.10 行为一致），版本读不到或模型缺失时拒绝并说明；HTTP 服务须 `/v1/health` 返回 version 且 `/v1/tiers` 可读。

## 2. 执行与结果契约（四类来源统一）

- 解析方式在**任务创建时固定**并落盘：`backend_id` + `backend_config`（含 `engine_source` 来源 kind 与 `engine_source_config` 完整身份快照：HTTP 规范化 URL / CLI 引擎+模型路径与版本 / 独立安装引擎根目录，不含密钥）。运行时逐片校验快照：kind 与身份任一变化（同 kind 换地址/换路径也算）→ 明确失败，绝不静默切换；执行一律使用冻结配置，不重读全局 selection（消除 TOCTOU）。外部应用升级/卸载导致冻结 kit 消失 → 明确失败并提示重新检测或改用独立安装。
- 后端实现 `literature/mineru_local.py` 的 `LocalMinerUBackend`（CLI 引擎与 HTTP 服务共用外壳），接入既有 `prepare/execute/status/result` 协议与工作流工厂；云端仍走 `RemoteMinerUBackend`。
- 切片复用共享逻辑（PDF 按页/字节自动分段，source_pages 由本地决定）；CLI 引擎逐片调 `mineru-kit parse --format zip --tier standard --disable-image-analysis`，取消为真取消（终止进程）；HTTP 服务走 uploads → parse/jobs → 轮询 → zip 下载，停止时尽力取消远端任务。
- **结果适配（关键差异）**：MinerU 4 kit 的 ZIP 输出为 `markdown.md` / `middle_json.json` / `structured_content.json` / `images/`，与远端 API 的 content_list_v2 不同构。适配层**原样保留 kit 输出**并派生两个文件：
  - `content_list_v2.json`：CLI 引擎在引擎 venv 内跑官方 renderer（`mineru.render.render_content_list_v2`，表格 html 补 `table_content` 别名）；HTTP 来源无引擎 venv，用内置适配器 `middle_json_to_content_list` 从 middle_json 派生（span 结构、视觉注记、列表/目录展平、bbox 0-1000，与官方输出对齐）。
  - `layout.json`：页几何优先取 `structured_content.json` 的官方 `extensions.docvortex_layout`（每页 width_pt/height_pt），缺失时用 PyMuPDF 读切片 PDF 兜底。
- **tier 按格式**：仅 PDF 传 `--tier standard`（HTTP 任务同理）；非 PDF/图片由 kit/服务按格式自定（flash-only 格式强制 flash），不强行传 tier。云端专属参数中 `is_ocr` 映射为 `ocr_mode`；语言包/表格开关/公式开关/额外输出格式对本机来源不适用（前端已禁用并说明），不静默忽略。
- 派生完成后走既有同一套 `seal_raw_mineru`（raw 原样封存+哈希）→ `merge_imported_project`（canonical、稳定 block/cell ID、source_pages 映射、coverage）；**绝不绕过 canonical，不覆盖 raw**。派生文件是加法，不改 kit 原始输出。

## 3. 资源与安全边界（实测依据）

- **独立安装（managed）**：安装位置 `<runtime>/mineru-local/`（默认 `~/Library/Application Support/Literature/mineru-local/`），不进仓库、不进文献库；uv 缺失时从 astral-sh/uv GitHub Release 下载**固定版本 0.13.5 并做 sha256 校验**（四个平台哈希内置）；模型与 uv 缓存全部收在该目录；`installed.txt` 最后写入，没有标记一律视为未完成安装；步骤幂等，取消/失败后可从断点续装；**修复/重装**（已就绪时 force 重跑幂等步骤）与卸载均与本地解析任务互斥；卸载只删该目录，不碰 raw/canonical/译文。安装取消只终止安装进程，绝不误杀正在执行的解析进程（进程按 install/parse 标签隔离）；进程日志记录运行中的推理进程，应用重启时清理遗留进程。
- **已有安装（external_cli）**：启动独立子进程；输出/临时文件/日志/doclib 数据库全部在 Literature 目录（`MINERU_HOME` 指向我们的目录）。模型目录**只读共享**（`MINERU_CONFIG` 自建 config 指定 `model.base_dir` + `source: local`），实测不会改写外部 `config.yaml`。实测发现 kit 运行会向其托管 Python 写 `__pycache__/*.pyc` 字节码，已用 `PYTHONPYCACHEPREFIX` 重定向到 Literature 目录隔离；`models/.locks` 可能因并发下载锁产生微小写入（纯推理未触发）。对外表述为「只读调用 + 字节码缓存已隔离」，不承诺绝对零写入。
- **已有服务（http_service）**：服务地址默认要求 HTTPS；明文 `http://` 仅限真正 loopback（复用 `providers._is_loopback_host`：精确 localhost、127.0.0.0/8、::1；`0.0.0.0` 与 `*.local` 不豁免），或用户显式勾选「允许明文 HTTP」；URL 内 userinfo/query/fragment 拒绝；probe 与每次执行前都重新校验，且**不跟随重定向**（防 HTTPS→HTTP 绕过）。服务密钥存 macOS Keychain（service `local.literature.mineru-http`，account 按**规范化地址**摘要绑定）——换地址不会读到旧服务密钥；连接验证成功才保存 selection 与密钥，失败不破坏原配置；select 支持一次提交新密钥（先验证后保存）。HTTP 客户端 `trust_env=False` 直连。**断点恢复边界（如实声明）**：远端 job_id 只存活在本次执行内；应用重启后进行中的切片会重新提交（已完成切片保留、不重复合并），不做远端任务续接。
- Token 只约束云端后端：本地 CLI/HTTP 任务不读 MinerU Token，调度器按任务后端判定，不再被「无 Token」阻塞。
- 并行使用：默认 Literature 内部串行执行本地任务；不承诺跨应用全局互斥（与 TextFold 同时解析大文档可能争用内存）。

## 4. 验收状态（2026-10-25 第二轮复核修复后）

- **隔离自动测试**：`tests/test_mineru_local.py`（安装成功/失败/取消/断点续装/互斥/状态；外部引擎探测/选用/不兼容拒绝/冻结 kit 消失拒绝；明文 HTTP 契约（0.0.0.0、*.local、userinfo/query/fragment、IPv6、127/8）；密钥按地址绑定与 A→B 隔离；select 验证失败不落配置不存密钥；任务来源快照防切换（同 kind 换地址）；安装取消不杀解析进程；kit ZIP 解包与校验；官方 renderer 派生脚本；HTTP v1 协议执行链（MockTransport）；内置适配器与 normalize 消费）；全量 pytest 673 passed；tsc --noEmit 与 Vite 生产构建通过。
- **隔离真实浏览器验收**（`tests/manual/engine_ui_browser_check.py` + `.cjs`，14/14 PASS、0 脚本错误）：初始 managed 未安装提示与安装按钮；切换面板展示本机发现候选并一键选用 TextFold 引擎（版本显示）；首次连接带密钥假 v1 服务一次成功（select 内先验证后保存）；断开恢复并清密钥；无效路径内联明确原因；external/http 无卸载修复按钮；managed 已安装态提供修复（重跑幂等步骤）与卸载。
- **真实验收（本机，用户已授权调用 TextFold 引擎）**：
  - 外部 CLI 引擎：`~/Library/Application Support/TextFold/engine`（mineru 4.0.10，模型 2.0GB）→ 探测通过 → 完整服务链路（创建项目 → start_parse 本地后端 → run）→ 任务完成，canonical 9 blocks/3 pages/coverage=full，页几何 595×842，raw 封存校验无错误，source.md 生成。复现脚本 `tests/manual/local_engine_real_check.py`。
  - 自建 HTTP 服务：同一引擎 `mineru-kit api-server`（v1 协议）→ 探测通过 → 完整链路（上传→任务→轮询→zip→适配→合并）→ canonical 4 blocks/2 pages/coverage=full。
- **未验收 / 限制**：
  - managed 独立下载安装（~3.4GB）的真实端到端未在本轮执行（安装器逻辑有隔离测试覆盖）；真实「干净机器全新安装」待验。
  - 自建 HTTP 服务的鉴权（API key）路径、断点恢复、远端取消后的任务状态未真实演练；其他 MinerU 版本/协议不承诺兼容。
  - 客户端真机 UI（设置三入口、导入页来源选择）未由用户点验；未打包安装。
  - 结构校对/翻译基于稳定 block/cell ID 的契约未受影响（走同一 merge），但本地解析结果的下游真实校对/翻译未复跑。
