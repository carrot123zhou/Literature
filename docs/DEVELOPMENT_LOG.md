# Literature 开发日志

> 本日志记录可验证事实、架构决策和仍需外部条件完成的验收。更新代码时应同步更新本文，避免把“已编码”“已测试”和“已用真实服务验证”混为一谈。

> 条目为「最新在上」。2026-10-14 前的手工「第N轮」编号是遗留标记（历史上出现过重复，已用 `·` 后缀消歧），不再续编；新条目用 `## YYYY-MM-DD · 主题`，引用时以上文日期与主题为准。

## 2026-10-14 · 模型配置 v3：服务商 → 模型两层 + 任务内选模型（已编码/全量测试/已重打包安装；未真机验收、未 commit）

状态：**已编码 / 已全量自动测试 / 前端已构建 / 已重打包并安装**；**未真机验收**（真实服务拉取/批量追加/真实模型端到端），未启动客户端、未 commit。

背景：上一轮已能读 `/v1/models` 自动回填，但每个模型仍是一个独立「服务」条目、与服务商并列；用户要求像 Cindy 一样「服务商 → 模型」层级，并能在任务界面直接选模型、调思考强度。

### 数据与后端（lead）

- 磁盘结构升级到 v2：`services[]` 是一个服务商（`service_id`/`name`/`preset`/`base_url` + `models[]`），每个模型自带 `context_window`/`max_output_tokens`/`temperature`/单价/思考配置；`roles` 由字符串改为 `{service_id, model}` 绑定对象。旧 v1 扁平结构（服务=模型、roles 字符串）在读取时自动迁移（`_normalize_provider`/`_normalize_role`），无需人工处理；`_write` 统一落 v2。
- 新增 `ModelEntry`/`ModelProvider` 数据类；`ModelServiceStore` 增 `role_binding`/`get_model`/`resolve_model`/`has_model`；`config_for(service_id, model="")` 按服务商+模型构造 provider（model 留空用第一个模型，无模型时报明确错误）。
- 任务级模型选择：`TranslationJobInput`/`StructureJobInput`/`StructureAIAnalyze`/`InstantInput`/`ReviewInput` 新增 `model`；`JobManager` 增 `model_for(role, service_id, explicit)`；翻译/结构/即时/译审四条路径都先解析 `service_id + model` 再建 provider，模型随任务快照冻结（暂停/恢复沿用）。
- 接口：`ModelServiceInput` 改收 `models[]`（保留 flat 兼容字段）；`ModelRolesInput` 接受绑定对象或字符串；`check` 收可选 `model`。
- 能力标记增 `model_services_v3`（后端 `/health` + 前端 `requiredCapabilities`），旧 sidecar 会被前端拦下。

### 前端（Orca worker 实现，lead 复核构建）

- `types.ts`/`modelServices.ts`：`ModelService`=服务商（含 `models[]`），roles 为绑定对象并兼容旧字符串；新增 `entryFor`/`roleBinding`；`check(serviceId, model?)`；payload 改 `{name,preset,base_url,models}`；旧 localStorage 迁移为 1 服务商 + 1 模型。
- `ModelServicesDialog.tsx` 重写：左侧服务商列表（名称/基础 URL/N 个模型/已绑定），右侧表单 + **内嵌模型列表**（每行模型 ID + 上下文/输出/单价徽标 + 思考水平 + 删除，可展开编辑协议/预算）；「获取模型列表」选择器多选追加、跳过已存在 ID。
- 新增 `TaskModelPicker.tsx`（服务商→模型分组 + 思考水平）；接入翻译 `ScopeDialog` 与结构「AI 校准」；`App.tsx`/`ReviewWorkspace.tsx` 启动请求带 `service_id + model + reasoning_mode + reasoning_budget`。

### 验证

- 全量 `pytest` **531 passed**。新增用例：v1→v2 自动迁移；服务商多模型 CRUD + 角色绑定（对象/字符串两种形态）；任务级 model 覆盖（`config_for` 与请求体都拿到显式模型，不传则用角色绑定）。
- `tsc --noEmit` 通过；Vite 构建 `frontend/dist/assets/index-CvCfA0qc.js` 835.05 kB / `index-BdX3qNuA.css` 86.61 kB。

### 打包与安装

- PyInstaller sidecar：revision `7bc59bd7e095f6011c59`，sha256 **`8b52da841b5c795dacb12ef7a2d7d80dc8a5b71a85a40c0a01ed3324c3dcccdc`**，size 42968080；`dist/sidecar/`、`src-tauri/binaries/`、`.app/Contents/MacOS/` 三处逐字节相同。
- Tauri：`node scripts/tauri-run.cjs build ...` → `Literature.app` 55 MiB（54.59 MiB）；adhoc codesign 验证通过。
- 冻结冒烟：`tests/manual/frozen_sidecar_check.py` **15/15 PASS**（本轮新增 `model_services_v3` 与「roles 是绑定对象」两项）。
- 安装：旧版（sidecar `2a6f107a…`）备份到 `launcher/.rollback-Literature-2a6f107a.app`，`zsh scripts/setup_macos_launcher.sh --app-only` 装入 `launcher/Literature.app`；安装后与构建包 `diff -rq` 完全一致、codesign 通过；客户端未启动。

### 未验收

- 真实服务「获取模型列表 → 批量追加 → PUT」「`check` 带 model」「角色绑定读写」未真机验收；真实模型端到端（结构+翻译）未做。
- 未启动客户端、未 commit。

## 2026-10-14 · 模型配置重设计（/v1/models 自动回填）+ 任务级思考档位

状态：**已编码 / 已全量自动测试 / 前端已构建 / 已重打包并安装**；**未真机验收**（真实外部服务拉取、Keychain 链路、真实模型端到端联调未做），未启动客户端、未 commit。

用户诉求：读 Base URL 的 `/v1/models` 自动带出模型名与上下文，不再手填；参照 Cindy 的模型配置交互重做界面；并支持任务开始前临时调整思考水平。

### 后端（lead）

- `providers.list_remote_models` 改为返回结构化元数据：`id` / `context_window` / `max_output_tokens` / 输入输出单价。字段别名收敛：`context_length`、`context_window`、`max_context_length`、`max_model_len`、`max_input_tokens`、`n_ctx`、`top_provider.context_length`；输出上限 `max_output_tokens` / `max_completion_tokens` / `max_tokens`；OpenRouter `pricing.prompt/completion`（每 token）自动换算为「每百万」。Base URL 容错：已含 `/v1` 直接请求 `/v1/models`，否则先 `/v1/models` 再 `/models`。
- 新增 `POST /model-services/models`（`ModelProbeInput`：`base_url` / `api_key` / `service_id`）：**新建服务未保存时即可拉取**，无需先落盘；旧 `POST /model-services/{id}/models` 保留兼容。
- 模型服务新增 `context_window` 字段（持久化、clamp 0–10M），供回填与展示。
- 新增能力标记 `model_services_v2`（`/health` 与前端 `requiredCapabilities` 同步），旧 sidecar 会被前端「前后端版本不一致」拦下。
- **任务级思考档位**：`TranslationJobInput` / `StructureJobInput` / `StructureAIAnalyze` 新增 `reasoning_mode`（`inherit`/`off`/`low`/`medium`/`high`）与 `reasoning_budget`（0=按服务）。`providers.apply_reasoning_mode` 把档位翻成协议字段（openai_effort 直接映射；qwen/glm 开关型 low/medium/high→开启；off→各协议关思考取值；推断不出协议则不改），由 `JobManager._apply_task_reasoning` 在 `apply_thinking_policy`（探测/缓存）之前落到 provider；档位随任务快照冻结，暂停/恢复沿用。
- 修 `with_thinking_disabled` 缺陷：显式 `openai_effort` 下 `low/medium` 会被默认关思考改写成 `minimal`；现改为「显式协议 + 显式非空强度」一律尊重（`high` 的旧行为不变）。

### 前端（Orca worker 实现，lead 复核构建）

- 新增 `workbench/ThinkingLevelField.tsx`：任务前思考水平共用组件（档位下拉 + 低/中/高时显示预算）。
- 重写 `ModelServicesDialog.tsx`：对齐 Cindy——「获取模型列表」按钮、选择器（已选 X / 共 Y、多选批量添加）、条目徽标（模型 ID / 上下文 / 最大输出 / 单价，无报价显示「该路由没有报价数据」）、Base URL 失焦或保存密钥后自动拉取、刷新提示「已发现 N 个新模型 / 已是最新」、测试连接「连接成功（{{ms}}ms）」、错误按 401/403/404/429/余额/上下文超长/模型不存在归一；表单分层为基础区 + 折叠高级区（上下文「留空按 200K 保守默认」、输出上限、温度、单价 / 百万 tokens、思考协议）。
- `ScopeDialog`（翻译启动）与结构校对「AI 校准」区增设思考水平选择；`App.tsx` 两条翻译启动路径与 `ReviewWorkspace.tsx` 结构作业请求都发送 `reasoning_mode` / `reasoning_budget`（inherit 时预算强制归零）。
- 样式：`mineru.css` 增 `model-picker` / `thinking-level` / `model-advanced` 等。

### 验证

- 全量 `pytest` 两次 exit 0。新增 `tests/test_model_services.py`：元数据解析（含 OpenRouter 风格）、未保存拉取、任务级档位映射、`openai_effort` low/medium 不被改写、结构作业端到端请求体带 `reasoning_effort=high`、作业快照冻结。顺手修掉 `test_structure_job_probes_thinking_fields_once_then_uses_cache` 的 teardown 竞态（等作业终态再退出）。
- `tsc --noEmit` 通过；Vite 生产构建 `frontend/dist/assets/index-zQLoj23n.js` 830.83 kB / `index-DWoiT7ke.css` 85.35 kB。

### 打包与安装

- PyInstaller sidecar：sha256 **`2a6f107aabf3a089991d069e9ef6c05c1428ecedb6c06a433cb2347c50e6d951`** / size **42960016** / revision `7fbfa572a60325c73196`，与 `src-tauri/binaries/manifest.json` 一致；`dist/sidecar/`、`src-tauri/binaries/`、`.app/Contents/MacOS/` 三处逐字节相同。
- Tauri 打包：`node scripts/tauri-run.cjs build --config src-tauri/tauri.bundle.conf.json --config '{"build":{"beforeBuildCommand":""}}'` → `Literature.app` 55 MiB（54.58 MiB）；`codesign --force --deep --sign -` 后 `codesign --verify --deep --strict` 通过（adhoc）。
- 冻结 sidecar 隔离冒烟：`tests/manual/frozen_sidecar_check.py` **13/13 PASS**（新增「能力包含 `model_services_v2`」与「冻结产物含 `POST /model-services/models` 免保存拉取端点」两项）。
- 安装：旧版（sidecar `258845a2…`）`ditto` 备份到 `launcher/.rollback-Literature-258845a2.app`（已验证含旧 sidecar，不提交），再 `zsh scripts/setup_macos_launcher.sh --app-only`。安装后：内置 sidecar sha/size 与 manifest 一致、与构建包 `diff -rq` **完全一致**、codesign 通过；客户端未启动。

### 未验收

- 真实外部服务 `/v1/models` 拉取、批量添加、Keychain 保存链路未真机验收（本机 Keychain 读取被凭据访问策略拦下，未能实拉 yellowbee 网关）。
- 真实模型端到端联调（结构 + 翻译各一）未做；错误归一依赖后端回显文本关键词，极端中转站文案可能原样透传。
- 已安装但未启动、未 commit；真实耗时/质量对比未做。

## 2026-10-14 · 翻译提速验收 + 重打包安装（worker 验收轮：真实 provider 探测已通过，已安装，未启动/未 commit）

状态：**已真实验收（provider 探测）/ 已重打包 / 已安装**；未启动客户端、未 commit；关思考后的真实翻译耗时/质量对比未做。

承接同日「翻译提速：真正关掉思考」条目的遗留验收项与安装。

### 验收结果

- 全量 `pytest` **522 passed**（复跑）。
- `tsc --noEmit` 通过；Vite 生产构建产物与上轮记录逐字一致（`index-hceY4Yzm.js` 820.60 kB / `index-DtKO1_xS.css` 82.81 kB）。本机仍无 PATH node，借用 Playwright 内置 node（v24.18.1）。
- **真实 provider 探测（上轮未验收项，本轮补上）**：`.venv/bin/python tests/manual/thinking_probe_check.py` 对翻译角色服务 `yellowbee`（qwen3.8-flash-next）实测：生效写法为 `qwen_chat_template_kwargs`（`chat_template_kwargs.enable_thinking=false`），确认请求 reasoning=0 / output=8 / 占比 **0%**（阈值 25%），退出码 0。Keychain 读取在 worker 会话可用（上轮被安全策略拦下）。注：探测脚本只验证不落缓存；缓存仍由客户端首个作业按同一生产代码探测后写入，属预期行为。
- 未跑同书开/关思考 A/B 与译文质量抽检（需要启动客户端后由用户观察）。

### 打包与安装

- PyInstaller sidecar：sha256 **`258845a224e7e0f15e60506998d4198197198665eea6f7812243d2c6bcd06b88`** / size **42954896** / revision `d17a3834dc4746073211`，与 `src-tauri/binaries/manifest.json` 一致；`dist/sidecar/`、`src-tauri/binaries/`、`.app/Contents/MacOS/` 三处逐字节相同。
- Tauri 打包：`node scripts/tauri-run.cjs build --config src-tauri/tauri.bundle.conf.json --config '{"build":{"beforeBuildCommand":""}}'` → `Literature.app` 54.58 MiB；`codesign --force --deep --sign -` 后 `codesign --verify --deep --strict` 通过（adhoc）。
- 冻结 sidecar 隔离冒烟：`tests/manual/frozen_sidecar_check.py` **11/11 PASS**（revision 与 manifest 逐字符一致）。
- 安装：按第二十五轮同口径处理——先结束构建目录孤儿 sidecar（PID 33326/33327，非已安装客户端），旧版（sidecar `61bbe834…`）`ditto` 备份到 `launcher/.rollback-Literature-61bbe834.app`（已验证含旧 sidecar，不提交），再 `zsh scripts/setup_macos_launcher.sh --app-only`（脚本为 zsh；首次调用因审批超时未执行，重试成功）。安装后：内置 sidecar sha/size 与 manifest 及构建包一致、与构建包 `diff -rq` **完全一致**、codesign 通过；客户端未启动。
- 包内容：本轮翻译提速全部改动 + 工作树中既有的结构校对粗校/精校等未提交改动（自最新工作树重建，无丢失）。

## 2026-10-14 · 翻译提速：真正关掉思考（自动推断 + 字段探测 + 400 安全网）（已编码/全量自动测试/前端构建；未真实 provider 验收、未重打包/安装/commit）

用户提出「如何大幅度提升目前的翻译速率」。结论：瓶颈不是并发/分块/磁盘，而是**思考没被真正关掉**。

### 定位依据（本机真实库实测，`library/*/literature.sqlite`）

- `sincerity and authenticity` job `8631b4bf…`：77 请求 / 661 目标块 / 679 s（16 并发）；平均输入 1,362 token、平均输出 **9,824** token，其中 **reasoning 9,202（93.7%）**；反推单请求平均延迟 ≈152 s。
- `Between Cultures`（130 请求 / 27 min）、`Once upon a future time`（5,741 s 只译完 13/2564 块）同型，reasoning 占比均约 93%。
- 根因：`model-services.json` 里该服务的 `reasoning_style` 为空（`preset: custom`），而 `with_thinking_disabled()` 当时的规定是「只在用户显式声明协议时才改写字段」，因此整条链路调用它等于 no-op。

### 已实现

- **自动推断**（`literature/providers.py`）：新增 `infer_reasoning_style` / `resolve_reasoning_style` / `disabled_reasoning_effort`。未声明协议（`""` 或新增的 `auto`）时按模型名前缀保守推断（`qwen3*`/`qwq` → qwen，`glm-4*` → glm，`gpt-5*`/`o*` → openai_effort）；推断不出则不发送任何字段（与改造前一致）。`REASONING_STYLES` 增加 `auto`。显式 `on`/`high` 仍被尊重。
- **字段写法探测**（`probe_thinking_off` + `_settle_streaming_support` + `_probe_attempt`）：不同 OpenAI 兼容服务认不同的关思考写法（`enable_thinking` / `chat_template_kwargs.enable_thinking` / `reasoning_effort=minimal` / GLM `thinking`）。先用一个**不带思考字段**的 `max_tokens=96` 请求把「服务是否支持流式」定下来（拒流式则把 `_stream_disabled` 提升到父 provider），再逐候选探测，按 `reasoning_tokens/output_tokens ≤ 25%` 判定真正生效的那个。三态返回：成功 dict / `{}`（都不生效）/ `None`（临时故障——限流、5xx、超时、401/403/404、无 usage、已挂协调钩子——**不写缓存**）。探测子 provider 自建的连接池会释放，不误关作业连接池。
- **探测缓存**（`literature/model_services.py`）：新增 `thinking-policy.json`（`_POLICY_LOCK` 保护、tmp+replace 写入），按 `service_id\0model` 记录 `variant`。`thinking_policy` 只认带 `probed_at` 的条目；改模型/Base URL/协议或换密钥（`PUT /model-services/{id}/key`）会失效缓存。负缓存语义为 `NO_REASONING_STYLE="none"`——**明确不发任何字段**（含 `thinking_budget`），避免严格服务每个作业白付一次 400。
- **400 安全网**（`_complete`）：请求体带思考字段且收到 400 时，先按 `_EXPLICIT_REASONING_FIELD_HINT` 判断错误体是否**点名**了思考字段——点名则先摘字段（保住流式），否则先回退非流式；两个闸门单调，最多 3 次请求收敛，不会死循环。这保证「离线/未探测」或「服务不认字段」都不会让整本书在第一个分块就失败。
- **接入四路**（`literature/api.py::JobManager.apply_thinking_policy`）：翻译作业与结构作业（`allow_probe=True`，探测前把「正在探测关思考字段」写入任务 message）、单块译审、即时翻译（只读缓存不探测）；`/structure/analyze-ai` 旧端点一并接入。随请求传入的临时 provider（无 service_id）不做缓存/探测。
  - 结构路径的 `allow_probe=True` 是 2026-10-14 补的：真实网关拒收顶层 `enable_thinking`（只认 `chat_template_kwargs`），若结构路径只读缓存，则**只用结构校对的用户永远预热不了缓存**，会一直发被拒的默认字段、靠安全网摘掉 → 思考始终开着。回归测试 `test_structure_job_probes_thinking_fields_once_then_uses_cache`（已用禁用探测实验验证：去掉 `allow_probe=True` 该用例会失败）。
- **并发默认 4 → 8**：`TranslationJobInput.parallel_workers`、`App.tsx`、`ScopeConfirm` 默认值与 `DEFAULT_PARALLEL_WORKERS` 同步；上限仍 16，AIMD 在 429 时减半。
- **前端**（`ModelServicesDialog.tsx`）：思考模式协议下拉新增「自动识别（推荐）」并解释探测与缓存行为。

### 验证

- 全量 `pytest` **523 passed**（新增 `tests/test_thinking_policy.py` 29 项 + `tests/test_model_services.py` 1 项结构路径探测缓存回归：推断、字段变体、探测成功/静默忽略/只回思维链/临时故障中止/401 不写缓存/无 usage 不写缓存/已挂钩子不探测/连接池释放、400 安全网四种组合、缓存三态与失效、`NO_REASONING_STYLE` 负缓存、并发默认值）。
- `tsc --noEmit` 通过；Vite 生产构建 `frontend/dist/assets/index-hceY4Yzm.js`（820.60 kB）/ `index-DtKO1_xS.css`（82.81 kB）。
- 三轮独立 reviewer 复核：第 1 轮指出并修掉「400 分支把流式回退误判成字段被拒」「只回思维链被当瞬时故障」；第 2 轮指出并修掉「401/403/404 被当字段被拒并永久负缓存」「探测与流式回退耦合」「负缓存仍漏发 `thinking_budget`」；第 3 轮全部复核通过，并补上「无 usage 不写缓存」「已挂协调钩子不探测」「通用 400 优先保关思考」三项。
- 新增人工脚本 `tests/manual/thinking_probe_check.py`：对真实服务跑探测并打印生效写法与实测 reasoning 占比（结论直接复用生产 `probe_thinking_off`，与客户端行为一致）。

### 未验收（不混写）

- 关思考后的**真实翻译耗时对比与译文质量抽检**未做（需启动客户端跑一本书）；「6–10×」仍是推理值，不是实测值。
- 未做真实视觉模型 / 真实 MinerU Token 多格式验收（与本轮无关，沿用上一条目的遗留项）。
- **已安装包不含本条目最后的「结构路径也探测」修复**：reviewer 打包的 sidecar revision `d17a3834dc4746073211` 在我补上 `_claim_structure_job` 的 `allow_probe=True` 之前构建。翻译链路不受影响；只用结构校对的用户需要重新打包才能预热缓存。

> 真实验收与安装由 Orca reviewer worker（`m978l6kooeglwjn8cwsfbpiw`）于同日补完：
> 实测 `thinking_probe_check.py` 对 yellowbee（qwen3.8-flash-next）确认生效写法为
> `qwen_chat_template_kwargs`（`chat_template_kwargs.enable_thinking=false`），
> 请求 reasoning=0 / output=8（占比 0%，阈值 25%），退出码 0；sidecar
> sha256 `258845a2…` / revision `d17a3834dc4746073211`，冻结冒烟 11/11 PASS，
> Tauri 包 54.58 MiB，adhoc 签名校验通过，已安装（客户端未启动）。
> 这同时证明「顶层 `enable_thinking` 被网关拒收、靠探测换到第二候选」的主链路有效。
> 改动仍**未 commit**。

## 2026-10-14（第二十五轮）：打包并安装客户端（用户授权“打包安装”，未启动、未 commit）

把第二十四轮的粗校/精校结构校对打进可交付 macOS 客户端并安装。本轮**只做构建与安装验证**，未启动客户端、未调真实 MinerU/provider、未提交 Git。

### 构建输入与产物（安装前可复核）

- 冻结 revision：`literature/runtime_revision.py::source_revision()` = **`784fe85c3b42b3a2f86e`**（工作树含第二十四轮未提交改动）。
- 前端产物：`tsc --noEmit` 通过；Vite 生产构建 `frontend/dist/assets/index-CC7R9OzN.js`（820105 B）、`index-DtKO1_xS.css`（82807 B）。构建用 `~/Library/Caches/ms-playwright-go/1.57.0/node`（v24.11.1）——ChatGPT.app 内置 node 因 code signature Team ID 与 rollup 原生模块不一致，无法 `dlopen`。
- PyInstaller sidecar：`.venv/bin/python scripts/build_sidecar.py` → sha256 **`61bbe8346b055931172ac30e34989b3887fd977711bab7c32b4bce6828f87def`** / size **42944912**，与 `src-tauri/binaries/manifest.json` 一致；`dist/sidecar/literature-sidecar`、`src-tauri/binaries/literature-sidecar-aarch64-apple-darwin`、`.app/Contents/MacOS/literature-sidecar` **三处逐字节相同**。
- Tauri 打包：`node scripts/tauri-run.cjs build --config src-tauri/tauri.bundle.conf.json --config '{"build":{"beforeBuildCommand":""}}'`（`NAPI_RS_NATIVE_LIBRARY_PATH` 指向 `@tauri-apps/cli-darwin-arm64@2.12.1` 的 `.node`）；`src-tauri/target/release/bundle/macos/Literature.app` **54.57 MiB**；`codesign --force --deep --sign -` 后 `codesign --verify --deep --strict` 通过（adhoc）。
- 版本确认：Tauri v2 把前端资源压缩编进 `Contents/MacOS/literature`，`strings -a` 取出的资源名为 **`assets/index-CC7R9OzN.js`**，与 `frontend/dist/assets/` 一致。

### 验证

- **冻结 sidecar 隔离冒烟**（临时库/runtime、假 Token、不联网）：`tests/manual/frozen_sidecar_check.py` **11/11 PASS**（`/health` revision 与 manifest 逐字符一致、能力含 `parse_lifecycle_v1`/`task_history_v1`、未解析资源覆盖、任务中心、旧前缀迁移 3/6）。
- 额外实测冻结二进制 `/health`：revision `784fe85c3b42b3a2f86e` 与 manifest 一致，能力含 **`structure_modes_v1`** / **`structure_pause_v1`**。
- 安装：`scripts/setup_macos_launcher.sh --app-only`。安装前确认 `launcher/Literature.app` 及其内置后端未运行，并结束早前会话遗留的**构建目录孤儿 sidecar**（PID 21063/21064，指向 `src-tauri/target/release/bundle/macos/…`，非已安装客户端）；旧版（sidecar `b05922f76a9dcd62c62a0e839ef9d1d8cb44b307ba907c0fc74cd09162e6c4eb`）用 `ditto` 备份到 `launcher/.rollback-Literature-b05922f7.app`（已验证含旧 sidecar，不提交）。安装后：内置 sidecar sha256 与 manifest 一致、与构建包 `diff -rq` **完全一致**、`codesign --verify --deep --strict` 通过。

### 未验收 / 未做（不混写）

- 安装过程**未启动客户端**（安装后客户端于 11:42 由用户启动，当前运行新版本）；本轮未做系统性的原生窗口点击/交互验收（结构 UI 已在第二十四轮隔离浏览器 36/36 验收）。
- 未调真实 MinerU/付费模型；**粗校/精校的真实模型 A/B 仍未验证**，不能声称准确率/token 收益。
- 无 Developer ID 签名、无公证、无 Intel/通用包，不能把本地 adhoc 包当作对外发行包。
- 未提交 Git。仍遗留：早期会话的开发态 smoke sidecar（PID 4098，`/tmp/lit-smoke`）与 Vite 开发服务器（PID 4105）仍在运行，不影响安装，本轮未动。

## 2026-10-14（第二十四轮）：AI 结构校对重定义为「目录锚定的差分校准」（Lead 实施后端 + 收口，前端由 Orca designer worker 实施；已编码/全量自动测试/前端构建/隔离真实浏览器；未真实模型 A/B、未重打包/安装/提交）

用户提出三点重构：①目标不是全面结构校准，而是「以原书目录为基准，剔除误标 + 补齐漏标 + 按目录自身父子层级对齐」；②给出粗校/精校两档（多花 token/时间换质量）；③UI 只留「选档 + 开始/暂停/继续」，其余决策由程序完成。经与用户确认：**精校范围收窄为「粗校 + 未收录小节发现 + 整树父子复核」，不做全量配对复核、默认关视觉**（用户明确否掉了 full 第二遍）；**修正建议全部自动应用**（推翻旧原则「AI 修改全部待确认」，改为依赖可逆补丁 + 修订历史撤销）；**后端与前端一次做到位**。用户逐定：前端交现有 Orca `designer` worker，后端由 Lead 直接实施（不开新 worker）。

### 后端已实现（Lead）

- **档位**：`literature/structure.py` 新增 `STRUCTURE_MODES = ("coarse", "fine")` 与 `structure_mode_config(mode, advanced, base)`——把档位展开为内部执行参数（两档同为：本地优先、`toc_extraction=auto`、`proofread_mode=off`、`planning_mode=global`、关视觉、`auto_apply=True`；精校额外 `discover_unlisted=True`）。`advanced` 仅作诊断/兼容覆盖，显式给出才生效。`api.py::StructureJobInput` 新增 `mode: Literal["coarse","fine"] | None = None`（`None` 保持旧行为，兼容既有调用/测试）与 `advanced: dict`；`_claim_structure_job` 在服务端展开预置（用户不再传复核模式/规划策略/目录来源/并发/视觉/超时）。
- **候选收窄（粗校）**：`scope_calibration_candidates(payload)` 只保留「目录命中块 ∪ 本地建议涉及块」，丢弃看起来正常的未收录标题（那是精校的未收录小节发现）；形态明显可疑的未收录标题仍保留。仅在 `mode ∈ {coarse,fine}` 且存在目录且非精校时生效；目录为空的书自动回退「无目录模式」（本地信号 + 一次有界模型请求），不弹窗打断。
- **全部自动应用**：`apply_all_suggestions` 以 `automatic_policy="all"` 放开 strict 自动边界（不再限制 `set_title` 与 `confidence < AUTO_THRESHOLD`），patch 仍标 `automatic=True`（审计可追溯），仍只走可逆补丁；旧 `auto_apply` 高级路径保持 strict 语义不变。档位下**本地证据先落建议、模型结论覆盖同块**（模型对「保持不变」不产建议，因此本地误标结论不会被静默丢掉）。
- **暂停/继续**：新增 `_StructurePaused` 与 `check_pause()`；`run_structure_job(..., should_pause=...)` 在模型请求边界（`AuditedProvider._call`、目录提取前、规划前、每个复核批前/提交前）响应暂停。暂停时 job `status="paused"`（不是 failed），已完成响应已在审计中；继续 = `reuse_job_id` 指向暂停作业，复用判据从 `failed` 扩展到 `{failed,paused}`，只补剩余请求。`api.py` 新增 `POST /projects/{id}/structure/jobs/{id}/pause` 与 `/resume`；`_claim_structure_job` 建 `pause_event`、`run` 传 `should_pause`、`finally` 清理。
- **结果**：`result` 新增 `mode`、`categories`（扁平单行摘要：`removed`/`added`/`level`）与 `calibration`（详细诊断：含 `applied`/`candidate_scope`），供单一主控卡展示一行摘要；`/health` 新增能力 `structure_modes_v1`、`structure_pause_v1`。
- **自动采用已确认目录基准**：主流程只传 `mode` 时，`_claim_structure_job` 若发现项目存有 `structure/baseline` 且调用方未显式给 `toc`/`extract_toc`，则自动以其为基准并置 `extract_toc=False`（不重跑目录提取）；显式给出时不覆盖。这样前端只需传 `mode`，仍能尊重用户已确认/修订过的原书目录。

### 验证

- 全量 `pytest` **485 passed**（本轮新增 15 项）。新增 `tests/test_structure_modes_v2.py` 11 项：档位预置、精校与粗校仅差 mode 标志、`advanced` 覆盖但 mode 不变、未知档拒绝、候选收窄（保留目录相关+可疑、丢弃正常未收录）、strict 拒绝模型式 `set_title` 而 all 落地 `set_title`+`set_type` 且可撤销、粗校只向模型发送目录相关候选并全部自动应用（无待确认，`categories` 为 `{removed:0,added:2,level:1}`）、请求边界暂停后继续复用已完成请求（`reused_requests ≥ 1`）、请求前暂停后继续、旧参数无 `mode` 保持原行为且建议仍待确认。`tests/test_model_services.py` 新增 4 项：`mode` 解析 + 全自动应用后 pending 为空且含 `categories`、`/resume` 由 paused 配置重建并完成、主流程只传 mode 时自动采用已确认基准（`extract_toc=False`、`baseline_source=confirmed`）、**顶层显式字段覆盖档位预设**（`proofread_mode=full`/`planning_mode=adaptive`/`proofread_concurrency=3`/`toc_extraction=model` 均生效）。
- 兼容回归：既有 470 项全部通过（`run_structure_job` 新增参数全为默认值；`_prepare_patch`/`apply_patch_batch` 新增 `automatic_policy` 默认 `strict`；`StructureJobInput` 新字段默认 `None`/`{}`）。
- **前端**（designer worker；Lead 收口）：`OutlinePane.tsx` 重构为「模型字段 → 分段控件（粗校/精校）→ 单一主按钮（开始/暂停/继续校准）→ 运行面板（进度+阶段+已用时+一行摘要）→ 高级选项折叠」；`ReviewWorkspace.tsx` 状态收敛为 `mode` + `options`，主流程只传 `{service_id, mode}`，高级改动才随顶层字段覆盖；`review.css` 配套。
- **前后端字段对齐（Lead）**：新增 `result.categories`（`removed`/`added`/`level`）作界面单行摘要，保留 `calibration` 作详细诊断；`toc_entries` 继续在 `reconciliation` 里。发现并修复两处集成缺陷：(1) 前端把高级覆盖发在**顶层字段**，而原后端只读 `advanced` 容器，导致覆盖被档位预设静默盖掉 → 改用 pydantic `model_fields_set` 识别显式字段，只有显式字段才覆盖预设（新增回归）；(2) `runModel` 的 `.then` 返回了轮询 Promise，使 `posting.current` 直到轮询结束才释放，**暂停按钮被永久拦截** → 改为不返回轮询 Promise（`void pollStructureJob(...)`）。
- `tsc --noEmit` **通过**（exit 0）；Vite 生产构建通过（`index-CC7R9OzN.js` 820105 B、`index-DtKO1_xS.css` 82807 B）。两处均用 `~/Library/Caches/ms-playwright-go/1.57.0/node`（v24.11.1）执行（ChatGPT.app 内置 node 因 code signature Team ID 不匹配，无法 dlopen rollup 原生模块）。
- **隔离真实浏览器**（路由 stub，不联网/不读 Keychain/不调付费模型）：`tests/manual/structure_ui_browser_check.py` **36/36 PASS、0 脚本错误**。重写后的 `structure_ui_check.cjs` 覆盖：分段控件与默认主按钮、主流程只传 `mode`、高级覆盖下发、运行中主按钮变可点「暂停」且 POST pause、暂停态变「继续校准」且继续带 `reuse_job_id`、耗时真实递增、一行摘要 `categories`（目录 312 · 剔除误标 8 · 补齐漏标 5 · 层级修正 21）、详情内覆盖口径/规划路径/本地目录来源、思考配置保存。原解析全流程浏览器验收 `parse_ui_browser_check.py` 仍全部通过（仅保存失败用例刻意 abort 的 `net::ERR_FAILED`，0 脚本错误）。

### 未验收 / 未做（不混写）

- 未重打包 sidecar/Tauri、未安装/启动客户端；未做原生窗口点击验收。
- **未用真实模型做粗校/精校 A/B**，不能声称准确率、真实耗时或 token 收益；「粗校候选收窄」与「全部自动应用」的质量影响均未经真实样本验证。
- 未改真实库 `raw`/`document`/`translations`；未提交 Git。

## 2026-10-14（第二十三轮）：重新打包并安装客户端（用户授权安装，未启动、未 commit）

把前两轮的结构校对提速 + 项目列表轮询提速 + 资源清理一起打进可交付的 macOS 客户端并安装。本轮**只做了构建与安装验证**，未启动客户端、未调真实 MinerU/provider、未提交 Git。

### 环境与构建输入

- 本机 PATH 里**没有 node/npm/pnpm**（只有 cargo/rustc），且 `frontend/node_modules` 处于 pnpm 虚拟店完整但**链接不全**的状态：`@tauri-apps/cli` 在 `frontend/node_modules/@tauri-apps/cli/node_modules/` 下没有指向 `@tauri-apps/cli-darwin-arm64` 的链接，`require` 直接失败（`frontend/node_modules/.pnpm/...` 里的 `cli.darwin-arm64.node` 实体是在的）。用 CLI 自己的 `NAPI_RS_NATIVE_LIBRARY_PATH` 指向该 `.node` 绕过，未重装依赖。处置步骤与验收要点已写进 `docs/PACKAGING.md` 的“环境回退”。
- 借用 agent 运行时自带的 node `v24.19.0`（`~/.cache/codex-runtimes/.../dependencies/node/bin/node`，不在仓库内、不入包）。
- 冻结 revision：`literature/runtime_revision.py::source_revision()` = **`f01105482311f04ce692`**（构建前后一致）。

### 验证（均为安装前可复核的事实）

- 全量 `pytest` **470 passed**（合并前两轮：结构校对默认路径瘦身 + 项目列表轮询/`source.md`）。
- `tsc --noEmit` **通过**（exit 0）——上一轮因本机无 node 未能执行，本轮补上。
- Vite 生产构建通过：`frontend/dist/assets/index-44088ZuV.js`（818.97 kB / gzip 247.52 kB）、`index-DwNVqgKi.css`（82.10 kB）、`pdf.worker.min-iDqQPrd3.mjs`。
- PyInstaller sidecar：`src-tauri/binaries/literature-sidecar-aarch64-apple-darwin`，sha256 **`b05922f76a9dcd62c62a0e839ef9d1d8cb44b307ba907c0fc74cd09162e6c4eb`**，size **42937856**，均与 `src-tauri/binaries/manifest.json` 一致；`dist/sidecar/literature-sidecar`、`src-tauri/binaries/…`、`.app/Contents/MacOS/literature-sidecar` **三处逐字节相同**。
- Tauri 打包：`src-tauri/target/release/bundle/macos/Literature.app`（**54.56 MiB**）；`codesign --force --deep --sign -` 后 `codesign --verify --deep --strict` 通过（adhoc，无 Developer ID）。
- **冻结 sidecar 隔离冒烟**（临时 `LITERATURE_LIBRARY`/`LITERATURE_RUNTIME`、自带 session token、不触真实库）：`/health` 返回 `runtime_revision: f01105482311f04ce692`（与 manifest 逐字符一致）；把 `On Being Authentic` 的**副本**放进临时库后 `/projects` 返回 `review=partial`、`translate=done 615/615`，与改造前该项目的 pipeline 输出一致；`project_state` 缓存表在**既有数据库**上自动补建并回填（旧库迁移路径验证）。
- 安装：`scripts/setup_macos_launcher.sh --app-only`。安装前用与脚本相同的判据（`ps -axo command=` 匹配 `launcher/Literature.app/Contents/MacOS/`）确认目标客户端/后端未运行；旧版（sidecar `dc682466…` / revision `5368566b3b43788b28ba`）备份到 `launcher/.rollback-Literature-dc682466.app`（已验证含旧 sidecar，不提交）。安装后：内置 sidecar 与 manifest 及 `src-tauri/binaries` 一致、与构建包 `diff -rq` **完全一致**、`codesign --verify --deep --strict` 通过。
- 前端版本确认：Tauri v2 把前端资源**压缩后编进 `Contents/MacOS/literature`**，中文文案无法 grep；用 `strings -a` 取出的资源名为 **`assets/index-44088ZuV.js`**，与本轮 `frontend/dist/assets/` 的文件名一致，证明安装包内是本轮前端产物（而不是旧 dist）。

### 未验收 / 未做（不混写）

- **未启动客户端**，所以未经原生窗口点击/交互验收；未做真实浏览器端到端（本轮前端只改了选项下拉与结果栏文案，已过 `tsc` 与构建）。
- 未调真实 MinerU、未调真实付费模型；未验证新默（`proofread_mode=sample`、`toc_extraction=auto`、结构关思考）在真实模型上的准确率——这一点仍是上两轮遗留的核心未验收项。
- 无 Developer ID 签名、无公证、无 Intel/通用包，不能把本地 adhoc 包当作对外发行包。
- 观察到但未处理：PID 4098 有一个早期会话留下的开发态 sidecar（`.venv/bin/python -m literature.sidecar`，`/tmp/lit-smoke` 库，端口 41731）仍在运行。它不使用 `launcher/Literature.app` 下的二进制，不阻塞安装，与已安装客户端互不影响；本轮未动它。

## 2026-10-14（第二十二轮）：结构校对默认路径瘦身 + 导出时代暂存副本清理（已编码/自动测试/离线基准，未真实模型 A/B、未重打包客户端）

起因：对同一本 `On Being Authentic` 做“库内真实作业”与“人工 agent 直接校对”的对照——真实作业 288.5 s / 99,006 token（其中 `structure_proofread` 两次共 184 s），人工 agent 1 分 19 秒 / 约 1.2 万 token。逐项定位到三处可确定的浪费：第二遍全量复核（64% 墙钟、历史两遍零分歧）、模型被要求“每个候选都必须返回一次角色判断”、以及本地目录检测失败后降级到模型提取目录。本轮落地其中的确定性部分，并额外提供了目录页负样本闸门与离线基准。

### 已实现

- **复核模式 `proofread_mode`（`sample` 默认 / `full` / `off`）**（`structure_reasoning.py`、`structure.py`、`api.py`）：`select_review_targets` 新增 `review_mode` 参数（默认 `full`，历史行为逐位不变）。`sample`/`off` 下安全规则（漏标提升、标题降级、目录冲突、无锚点改层级、本地仍像标题/非散文短块、父子关系问题、置信度不足）不再阻止采纳，只写入该决定的 `review_flags` 供人工确认时查看；第二遍只剩两类候选：**硬阻塞**（规划缺失/角色非法/置信度非法，上限 `MAX_MUST_REVIEW_CANDIDATES=80`，超出部分保持原状并计入 `needs_review`）与**抽查**（`QA_REASON` 标记，按 `block_id` 的 SHA-256 排序取固定比例，确定性可复现）。抽查结果只经 `compare_qa_sample` 生成 `review_selection.qa` 一致性指标，**强制不进 decisions/suggestions**（回归：抽查故意全部反着答，仍零建议、零补丁）。`proofread_full=true` 仍是 `full` 的兼容入口，`proofread_mode` 优先。
- **抽查用极简输出契约**（`QA_PROOFREAD_INSTRUCTION`）：抽查批只要求 `block_id/role/parent_id/confidence`，明确禁止 `reason`；`batch["review_purpose"]="qa"` 触发，`_proofread_batch(..., purpose=)` 透传，二分拆批保持目的。审计 `batch_tag` 由 `proofread-N` 改为 `{purpose}-N`，`apply_batch_audit` 同步。
- **印刷目录检测：补充正样本、新增两类负样本**（`structure_signals.py`）：`TOC_MARKER_RE` 增加单数 `content`（真实书常见 `Content`）；新增 `TOC_ENTRY_MAX_TITLE=100` + `_toc_entry_shape`（长度、句读结尾、书目引用）作为页级 `TOC_ENTRY_SHAPE_RATIO=0.6` 闸门；新增 `_page_looks_like_index`（注释引用 `\d+[a-z]` 密度 ≥0.25 或 `See/See also` 密度 ≥0.15）排除索引页。实测分辨度：真实目录页形态比 0.97–1.00，尾注页 0.18–0.30，参考书目页 0.36，索引页 0.92（由索引闸门拦下）。
- **本地目录直接采信 `toc_extraction`（`auto`/`local`/`model`，默认 `auto`）**（`structure.py`）：`signals.toc_marker_pages` 抽为独立函数；有显式目录标记页、本地能解析出 ≥2 条时不再发 `structure_extract` 请求，`proposed_toc` 带 `level_source="local"`，结论仍只是待确认草稿。无标记/解析为空/显式 `model` 时保持原路径。结果新增 `toc_extraction` 与 `local_toc_marker_pages`，`baseline_source` 在本地采信时为 `local`。
- **结构路径默认关思考**（`structure.py`）：`run_structure_job` 在 `with_read_timeout` 后调用 `with_thinking_disabled()`；语义与翻译路径一致（只改已声明协议的字段，显式 `on`/`high` 时尊重用户配置）。
- **前端**（`OutlinePane.tsx`、`ReviewWorkspace.tsx`）：`StructureOptions.proofreadFull` 换成 `reviewMode` + `tocExtraction`，新增两个下拉；结果栏文案改为“第一遍规划定案 / 第二遍必须定案 / 抽查一致性”，并在地目录被采信时提示层级来源；帮助文案与审计口径同步。
- **导出时代暂存副本清理**（新增 `scripts/prune_mineru_workflow_staging.py`）：2026-10-13 移除导出/TBL 后，旧项目仍留着封存前的下载目录 `exports/.mineru-workflow_mineru_job/slices/**`，与 `ocr/raw/mineru/**` 逐字节相同。脚本默认只预览，`--apply` 才删；只删 sha256 命中 `ocr/raw/manifest.json` 已封存哈希的文件，任何不匹配文件（如记录原始作业路径的 `job.json`）与空目录清理分开处理。执行结果：6 个项目共 **474 个文件 / 181.4 MiB**；`library` **594 MB → 416 MB**；`verify_raw` 全部通过；用户 EPUB、raw、document、translations、`library/.trash` 未动。
- **离线基准**（新增 `tests/manual/structure_review_mode_benchmark.py`）：只读真实库，把 document 复制到临时 `ProjectStore`，用 mock provider 跑**真实代码路径** `run_structure_job`，不调用模型、不写真实库。指标为请求数、输入字符数、模型必须逐条作答的条数（均为代理指标）。

### 与既有工作的关系

- 与本轮前两轮的关系：`d7a0cbd` 已做“本地过滤 + 选择性复核 + 有界并发”，本轮在其上把“选择性复核”从“安全规则决定哪些候选要再问一遍”改为“第一遍定案 + 有界抽查”，并把“目录核对”从“总是问模型”改为“有标记就本地采信”。工作树里既有的 `compact_context`（上下文压缩 V1）仍保持默认关闭、未被本轮启用。
- 未动：`structure_adaptive.py`（自适应分支规划，仍为可选试用、默认 `global`）、`apply_automatic` 的本地 0.97 阈值与全部补丁/撤销路径。

### 验证

- 全量 `pytest` **463 passed**（本轮前 439）。新增两个测试文件：`tests/test_structure_toc_detection.py`（6 项：单数 `Content` 标记、尾注页、参考书目长 run 不压过真目录、索引页、真目录通过索引闸门、长条目标题不被误杀）、`tests/test_structure_review_modes.py`（17 项：模式解析与兼容入口、抽查尺寸/确定性、`sample`/`off` 标注语义、硬阻塞仍必须定案、抽查与硬阻塞不重叠、`compare_qa_sample` 不改输入、端到端“抽查全部反着答也不产生建议”、`off` 不发第二遍请求）。
- **改写 4 处旧断言以反映新默认**（不是“测试通过即兼容”）：`test_structure_reasoning.py` 重试用例 4→3 次调用（默认 `sample`）、续跑用例 3→2 次调用与 usage 40→30；`test_structure_speed.py` 三个并发用例显式传 `proofread_mode="full"`（它们验证的是多批并发机器本身）；`test_model_services.py` 后台作业用例显式传 `toc_extraction="model"` 以继续覆盖模型核对目录路径；`test_structure_calibration.py` 把“可解析的目录仍须由 AI 核对”改为“有显式标记时本地直接采信”，并保留“无标记时仍走模型”的覆盖。
- 离线基准（5 份真实文档，改造前 `full`+`model` → 改造后 `sample`+`auto`）：

| 文档 | 候选 | 请求 | 输入字符 | 逐条作答 |
|---|---:|---:|---:|---:|
| White Lotus Teachings | 348 | 16 → 7 (-56%) | 443,113 → 247,200 (-44%) | 761 → 366 (-52%) |
| Metropolitan Journal | 230 | 11 → 5 (-55%) | 267,628 → 143,468 (-46%) | 474 → 242 (-49%) |
| On Being Authentic | 58 | 4 → 2 (-50%) | 56,766 → 27,043 (-52%) | 127 → 66 (-48%) |
| Writing and Authority | 529 | 24 → 10 (-58%) | 654,865 → 375,232 (-43%) | 1,114 → 556 (-50%) |
| Making Transcendents | 489 | 22 → 9 (-59%) | 539,340 → 277,662 (-49%) | 991 → 514 (-48%) |

  报告：`docs/verification/structure-review-mode-2026-10-14.md`；原始 JSON：`docs/verification/structure-review-mode-2026-10-14.json`。
- 本地目录检测在 7 个真实项目上的前后对照：`On Being Authentic` p97–104（尾注，80 条垃圾）→ **p5（11 条，与印刷目录逐条一致）**；`Once upon a future time` 参考书目 p314–326（13 页 run）→ **真目录 p7–9（91 条）**；White Lotus p6–7、Making Transcendents p8、MetJournal p3、Writing and Authority p2–3 不变；White Lotus War 仍为空（无目录）。
- 资源清理后 `verify_raw` 对全部 7 个项目无错；`library` 594 MB→416 MB。

### 未验收 / 未做

- **质量未证**：离线基准用 mock provider，第一遍规划由文档自身标题层级派生，只度量请求形状；**不能**声称准确率、真实耗时或真实费用等价。抽查只回答“两遍是否分歧”，不回答“谁对”。
- 未用真实模型跑同书 A/B（`full` vs `sample`，人工比对采纳率）；建议在留出文档上补做后再决定是否把 `sample` 长期作为默认。
- 未重打包/安装客户端（本轮只改 Python 与前端源码）；本机**无 Node/npm/pnpm**，`tsc --noEmit` 与 Vite 构建**本轮未执行**，前端改动只经过人工审读。
- 未改真实库的 raw、document、translations；未调用付费服务；未提交 Git。
- 已知但本轮未处理：`structure_adaptive.py` 的 `planning_mode=adaptive` 仍未做真实 A/B；`compact_context`（上下文压缩 V1）仍默认关闭。

### 追加：项目列表轮询与 `source.md`（同日追写）

上一条里列为“已知但未处理”的两项已逐一定量并处理。

- **`/projects` 聚合的热点不是 manifest，而是两处“为了一个字段解码整份文件”**（实测 7 项目共 175 ms，前端每 3 秒轮询）：
  - `structure_review_summary` 调 `load_document` 只是为了比一个 `updated_at`，实测该项 **131 ms**，其中 **122 ms** 是 JSON 解码（`document.json` 单份 1.4–8.2 MB，整库约 28 MB）。新增 `ProjectStore.document_updated_at()`：以 `(size, mtime_ns)` 为缓存键存在新增的 `project_state` 表（`CREATE TABLE IF NOT EXISTS`，随 `_init_database` 对旧库自动补建），命中直接返回，未命中才读一次文件并回填；`save_document` 写完后顺手刷新缓存。缓存键取自真实文件，**不存在“缓存说未变、文件其实已变”的假有效**，且不受写入顺序影响（崩在两次写之间时签名不匹配→重新读）。单项目（8.2 MB 文档）**57.5 ms → 1.8 ms**；整库 7 项目 **164.5 ms → 6.3 ms**。
  - `project_pipeline` 在列表路径上默认调 `list_jobs`（`SELECT *` + 解码 `result_json`/`activity_json` 审计，单项目可达 1 MB+），实测 **42 ms**。改为默认 `list_job_summaries`（只投影状态字段）；`translate_stage` 只用到 `kind`/`status`，语义不变。`GET /projects/{id}` 仍传完整 jobs（它本来就要审计）。
  - 回归：新增 `tests/test_document_state_cache.py` 6 项（值与 `load_document` 一致且每次 save 后跟随、外部写入使缓存失效、文件缺失返回空、`project_state` 表存在、确认状态机 `todo→done→partial` 不变、绕过 store 的外部写入仍使确认失效）。
- **`document/source.md` 量下来不值得删，但它确实是“非原子写”**：实测渲染 + 写入仅 **0.0–1.9 ms**、全库合计约 6 MB，且 `tests/test_pipeline.py` 与 `docs/ARCHITECTURE.md` 都把它当可读投影，所以**不删**。但它原本用 `write_text` 直接覆盖（与 `document.json` 的原子写入不一致，崩溃或并发会读到被截断的 Markdown），已改为新增 `mineru.write_source_markdown()` + `storage.atomic_text()`（临时文件 + `os.replace`），4 处写入全部收口。回归：`tests/test_pipeline.py` 新增 1 项（写入后内容与 `render_source_markdown` 一致且不残留临时文件），原导入用例也断言无临时文件残留。

**验证（追加）**：全量 `pytest` **470 passed**；两项均在真实库上量了前后耗时（只读，未改真实库业务数据；`project_state` 是派生缓存，可重建）。**未验收（不混写）**：未重新打包/安装客户端；本机无 Node，前端本轮无改动也未重跑 `tsc`；未跑真实浏览器端到端。

## 2026-10-14（第二十一轮·翻译提速）：翻译提速四项（对齐 TBL 策略，已编码/自动测试，未真实验收）

承接与 TranslateBooksWithLLMs 的比对结论（token 计量分块、AIMD 并发、prefix caching、思考控制），四项全部落在翻译主链路。未调用真实模型、未重打包客户端、未改库。

### 已实现

- **翻译任务默认关思考**（`providers.py` + `api.py`）：新增 `OpenAICompatibleProvider.with_thinking_disabled()`——已声明协议时按协议改写（`qwen_thinking`/`glm_thinking` → 开关 `off`，`openai_effort` → `minimal`），整本书翻译、单块译审与即时翻译三条链路默认调用；用户显式要求开思考（开关 `on` / effort `high`）时不覆盖，未声明协议的服务不发送未知字段（显式协议原则不变，此类服务需在模型服务设置里选协议才能关）。依据：实测 `qwen3.8-flash-next` reasoning 占输出 85–90%，对译文质量近零贡献。
- **AIMD 并发自适应 + 上限 8→16**（`translation.py` + `request_coordinator.py` + `api.py`）：`MAX_PARALLEL_WORKERS=16`、`TranslationJobInput.parallel_workers le=16`，前端范围弹窗增加 12/16 档；`_process_blocks` 内滑窗提交门改用 `effective_workers`，块级 `rate_limited`（新增 `_ChunkOutcome.rate_limited`，`ProviderError` 新增 `status_code`）触发并发减半，连续 3 块成功后 +1 探测回升，不超过用户设定上限。同时修复隐性瓶颈：`RequestCoordinator.default_service_limit=4` 从未被调高，workers>4 时在途请求仍被压到 4——新增 `raise_service_limit()`（只升不降，避免重建信号量伤及运行中作业），翻译作业启动时上调到 `max(现值, workers)`。
- **AIMD 限流修复（评审盲区补齐，同日）**：评审复现「整块因 429 完全失败」走 `fully_failed` 分支——既不减并发又计入全局熔断（3 次后服务已恢复仍熔断，12 块误败 8 块）。修复：`rate_limited` 提到 `fully_failed` 之前判断，429 减半并发且 `provider_error_streak` 归零（不计熔断）；`_run_chunk` 把捕获的 `ProviderError` 传入 `_ChunkOutcome.error`；切分子批被 `_translate_units_isolated` 吞掉的异常经新增 `error_sink` 上传 429 状态（含超时切分路径）。修复后复现：串行 10/12 译完、并发 4 下 9/12 译完，失败仅限真正撞上限流的块，无「已中止」误报（`tmp/aimd_repro.py`）。
- **Prefix caching 友好布局**（prompt 契约 `translation-v3`）：术语表从“按块过滤后插 user prompt”改为整份按源词稳定排序注入 system prompt 尾部（新增 `_stable_glossary`，上限 `GLOSSARY_MAX_ENTRIES=80` 不变）；同一任务内所有请求的 system 前缀逐字节一致，可命中 DeepSeek/Qwen/OpenAI 兼容服务的自动前缀缓存；user prompt 只留跨块变化部分。代价是不再按块过滤（未出现术语也注入），但缓存命中后这部分输入接近免费，且代词指代处也能拿到正确译法。
- **分块预算从字符改 token**（`translation.py`）：新增 `_chars_per_token`（en/fr/de/es/it/pt≈4.0、zh/ja≈1.6、ko≈2.2、ru≈3.2）与 `_estimate_tokens`；`DEFAULT_CHUNK_TOKENS=900` 取代 `DEFAULT_CHUNK_CHARS=2400`，`_chunks`/`_batch_units`/`estimate_translation` 共用同一计量（预估与真实执行仍同规则）。效果：英文块变大（减少请求数）、中文块变小（降低 `finish_reason=length` 截断与切分重试往返）。

### 已验证

- Python 全套 `433 passed`（含评审后修复与回归）。新增/更新：token 分块按语言差异、`with_thinking_disabled` 七种配置矩阵（含不修改原对象、未声明协议原样返回）、AIMD 单次 429 后继续提交、AIMD 限流突发（连续 3 次 429 后恢复）不触发全局熔断（并发 4 下 ≥9/12 译完、失败 ≤3、无「已中止」）、术语表稳定 system 前缀契约（排序、user 无术语表）；更新两处旧契约断言（英文批上限 3200→3700、冻结术语表断言改到 system）。评审独立复检 432 passed + 本轮修复后全量重跑通过。
- `tsc --noEmit` 与 Vite 生产构建通过（Node v22.11.0，仓库 `.tools/node`）。

### 尚未验收

- 未用真实 provider 验证：关思考后 `reasoning_tokens` 应降至≈0（中转站可能剥掉 `enable_thinking` 字段，需用 usage 账本核实）；prefix caching 命中率（`cached_tokens`）与实际提速幅度；AIMD 在真实 429 下的降速/回升曲线。
- 未重建冻结 sidecar 与 Tauri 包，已安装客户端仍是旧引擎；`polish`/串行衔接等旧行为回归未逐项手测。
- prompt 契约升为 `translation-v3`：恢复 v2 冻结任务会打 warning 并沿用冻结语义（现有逻辑，未新增迁移）。

## 2026-10-14（第二十一轮·安装自适应）：在最新完整版本上安装自适应结构校对更新（用户授权安装，未 commit）

用户明确授权“在最新版本的程序之上安装这个更新”，要求不得用旧包覆盖其他模块的新改动。本轮未调真实模型/MinerU、未改真实库/raw、未启动客户端、未 commit/push。

### 安装前核实（只读）

- HEAD `b40b9ae`（源 revision `4c4ffbcfb83f0c774fc7`）；工作树 revision `5368566b3b43788b28ba`。
- 已安装旧版 `launcher/Literature.app`：sidecar sha `a5a99eba…` / size 42892592 / manifest revision `d383569f0222a29435f1`，02:58 构建、03:02 安装（`.rollback-Literature-7aec1108.app` 是它替换掉的我的 7aec1108 版本）。它早于当前工作树，不是更晚/不同来源，工作树包含其他模块的最新未提交改动（`translation.py` +196、`providers.py` +36、`request_coordinator.py` +15、`ReadingPanels.tsx` +4 等），因此在其基础上重建不会丢失新模块。
- 无 Literature / sidecar 进程运行；最近 5 小时无源码改动（冻结可建）。

### 冻结、测试与构建

- 构建前 `source_revision()` = `5368566b…`；前端 dist `index-VaYJ_Nxx.js` sha256 `2bf3bffc…`。
- 全量 `pytest` **433 passed**；`tsc --noEmit` 与 Vite 生产构建通过。
- 重建 sidecar：revision `5368566b3b43788b28ba`、sha256 `dc682466f982ad0d846a491640fa227efb928b6cff3002c19497c71a6d663f46`、size 42927360。
- 重建 Tauri：`src-tauri/target/release/bundle/macos/Literature.app` 54.55 MiB；包内 sidecar 与 `src-tauri/binaries`、`dist/sidecar` 逐字节一致；`codesign --verify --deep --strict` 通过（adhoc）。
- 构建后 `source_revision()` 仍为 `5368566b…`（无构建期源码变化）。

### 备份与安装

- 备份旧版到 `launcher/.rollback-Literature-a5a99eba.app`（sidecar 仍 `a5a99eba…`，codesign 验证通过；不提交）。
- `scripts/setup_macos_launcher.sh --app-only` 安装到 `launcher/Literature.app`。
- 安装后：内置 sidecar 与构建包/binaries 逐字节一致（`dc682466…` / 42927360）；`Contents` 与构建包 `diff -rq` 完全一致；`codesign --verify --deep --strict` 通过；客户端仍未运行。

### 包含模块与未验收

- 包含：自适应分支结构规划（可选试用，global 默认不变，未因安装默认启用）+ 其他 agent 的 translation SSE/超时切分/读超时可配、providers/request_coordinator、ReadingPanels 等最新改动；sidecar revision 覆盖全部 `literature/*.py`，前端 dist 含 `planning_mode`/`自适应分支`。
- **未验收**（不混写）：真实模型 A/B、真实 MinerU/付费 provider、原生窗口逐项点击、公证与干净机安装均未做；本轮仅安装授权，未 commit/push。

## 2026-10-14（第二十轮）：自适应分支结构规划（可选试用，已实现/离线验收/未部署）

用户授权优化并实现由简到繁的分支校对策略。新增 `literature/structure_adaptive.py`，作为**明确可选试用模式**（`planning_mode="adaptive"`）；生产默认仍是整体串行 + 选择性复核，不静默替换。未调用模型、未安装/启动、未 commit/push、未改真实库/raw。

### 分支规则与数据契约

- 数据仍只用 `structure_model_payload` 的候选/目录/对账字段；规划输出与整体规划同形（`profile/nodes/issues/planned`），下游选择性复核、`validate_hierarchy`、建议、人工确认与撤销全部复用。
- 可靠锚点：正文匹配 ≥0.92、唯一领先 ≥0.06、目录无重名、正文层级（现有层级或编号前缀 ±1）佐证、`level_source∈{confirmed,model,vision}`、阅读顺序 LIS 一致；印刷页码仅辅助（≥3 锚点单调才算一致）。AI 自报置信度/单一模糊匹配/单个页码不能独自授权边界。
- 区域与预检门控：最浅层可靠锚点定章；章内候选按 `REGION_WINDOW=64` 切；发送前先构造全部请求并计算成本代理（请求数/字符/串行关键路径/上下文），若自适应请求数超全局则先做小区域装箱合并（`MERGED_MAX_PRIMARY=128`，保留各章 anchors 与边界标记）；门控要求 请求≤全局 且 字符<全局 且 轮数≤全局，否则精确回退。每请求都带精简 `root_convention`（章锚点层级+一行约定），`overview` 仅首个请求；可靠区域默认 `MAX_PARALLEL_REGIONS=2` 有界并行，头部/重名/歧义区域串行。
- 合并只接受主候选，重叠只读；每个候选（含未收录小节、疑似漏标短块）恰好一次主判断；目录不覆盖正文标题文字。
- 升级/回退：局部升级触发覆盖遗漏/uncertain/非法角色、与最终标题树冲突、与已确认目录层级矛盾（不只“不在 nodes 里”）；局部升级（扩到 `REGION_CONTEXT×2`）与全局补查各限一轮且合计受 `MAX_ADDITIONAL_PLANNING_REQUESTS=4` 硬上限，超限/仍异常留第二遍复核 + `validate_hierarchy` + 人工；框架 `unreliable`、候选 <`ADAPTIVE_MIN_CANDIDATES=120` 或预检门控不通过回退整体串行；`proofread_full` 全量复核仍可选。

### 集成与 UI

- `select_review_targets` 的 `strict_local` 保留为默认 True 的可选参数；默认调用与显式 True 逐位一致（White Lotus 87/261），新增等价单测。
- 新增 `StructureJobInput/StructureAIAnalyze/StructurePlanInput.planning_mode`；前端“校对选项”新增「规划策略」，结果栏显示实际路径（自适应分支的框架状态/可靠/歧义区域/局部升级/遗漏，或回退原因）。

### 离线验收（无模型调用）

- 脚本 `tests/manual/experiments/structure_adaptive_replay.py`，输出 `docs/verification/structure-adaptive-replay.json`。
- White Lotus（历史规划回放，候选 ID 逐一致）：348/348 覆盖不变、遗漏 0、fallback 否；规划 6 请求/183,923 字符 → 6 请求/97,443（**-47.0%**，小区域合并生效）；端到端（规划+未改的 strict_local 第二遍）13/363,728 → 13/273,138（**-24.9%**），可见第二遍复核限制了端到端收益。
- Journal（机械覆盖）：主 4 请求但机械计划的人工 `parent=None` 触发 3 次局部升级，总 7 请求/108,740（只问可疑 ID）；端到端 8/191,924 → 11/201,846（**更差**）；真实模型未验证，**不宣称 Journal 有收益**。
- Authentic（机械覆盖）：58 候选 <120 → **回退整体串行**（输出与全局等价），覆盖不变。
- 说明：字符/请求为精确测量；这不是新模型准确率/耗时证明，也不使用历史 usage 当作新请求用量。

### 测试 / 构建 / 未验收

- 结构相关 `pytest` **38 项自适应回归**（`tests/test_structure_adaptive.py`：框架有效/部分/无效/无目录、跨章/跨批、重名、页码偏移、错误目录层级、未收录无编号小节、升降级、遗漏/非法响应、`_apply_rows` 严格校验〔unhashable/重复 ID 整项拒绝/非法或后置父标题/未知 ID〕、root_convention、预检门控与合并、局部升级只问可疑 ID、补查按阅读顺序 ≤`ADAPTIVE_SECONDARY_WINDOW=64` 切批且受 `MAX_ADDITIONAL_PLANNING_REQUESTS=4`、failure/BudgetExhausted 停止新请求、模式隔离与缓存、小样本回退）。当前工作树全量 `pytest` **432 passed**（含并行未提交的翻译/SSE 改动；本轮结构改动未触碰 translation/providers/request_coordinator）。
- `tsc --noEmit` 与 Vite 生产构建通过；隔离真实浏览器 `tests/manual/structure_ui_browser_check.py` **21/21 PASS、0 脚本错误**（新增 `planning_mode=adaptive` POST 与路径/边界显示）；原 `parse_ui_browser_check.py` 全流程仍通过。
- **未验收**：无真实模型 A/B，阈值（0.92/0.06/0.6/120/64/6/2）未用真实样本校准；不能声称准确率/漏标率/真实总耗时已改善；未安装/未提交 Git/未改真实库/raw。

## 2026-10-14（离线实验，未部署）：结构校对上下文压缩与复核范围收紧

目标：回应用户“现有方案准确率高但耗时/token 多、拒绝仅按目录校准导致未收录小节被忽视”。以 `d7a0cbd` 选择性第二遍为 baseline（不是早期全量双遍），测两个变体：V1 仅压缩重复上下文（复核选择不变）；V2 压缩上下文 + 仅异常/高风险补查。只读真实库/历史审计，用 Lead 只读快照，未调模型、未安装/启动、未改真实库/raw、未提交。

### 可复现性与数据

- White Lotus 快照 348 候选；历史作业 `17e682665d914a0d82948c3594c18141`（2026-10-13，6 规划 + 9 复核）请求内嵌候选 ID 与当前重建 **完全一致（对称差 0）**，可做忠实两遍回放。
- Journal（228）/ Authentic（58）历史作业候选逻辑版本不同，**不做回放**，只测规划阶段输入开销；Journal 未收录标题 63、Authentic 12，风险画像不同。
- 快照为 2026-10-14 只读状态，可能已含人工采纳；涉及“相对当前文档”的建议集合为近似，非逐步 gold。

### 输入开销（精确字符/字节；无 tokenizer，token 为估算）

- White Lotus 请求拆为 6 规划窗（64/窗）+ 复核批（40/批）：历史全量双遍 15 请求/409,603 字符；**baseline 选择性 13 请求/363,728**；**V1 13 请求/279,275（-23.2%）**，其中规划 -41.5%（去后续窗口重复 overview/整本目录/过长 previous_outline，保留近邻目录/祖先/相邻标题）、复核 -4.5%（去每候选重复 reasons 与 layout，heading_context 截 120 字符）；**V2 7 请求/146,177（-59.8%）**，复核字符 -78.6%。
- Journal 规划 101,352→68,691（-32.2%）；Authentic 单窗口 15,080→15,080（0%）。
- 历史真实 usage（White Lotus 全量双遍，仅供参考）：规划 input 95,606 / output 59,602；复核 input 109,177 / output 61,124；非新请求计量。

### 两遍分歧与省略风险（White Lotus）

- 规划节点 348、复核答案 348；**role/level/parent 完全一致（role_changed=level_changed=parent_changed=0）**。V2 相比 baseline 省略复核 241 项，历史两遍分歧为 0；省略项构成 paragraph 238 + footnote 3，规划角色 body 232/furniture 6/footnote 3，本地“仍像标题”仅 4 项、非散文短块 240 项。
- 未收录标题 5 个仍保留在目录树；其中 `Analysis` 进入 V2 复核，其余第一遍即为标题且层级不变，未被删除或降级。
- 两遍共有 2 个父子关系错误（`1.1 Defining the problem`、`1.2 Defining some terminology` 挂为根），**两遍都没修**；说明两遍一致≠真实正确。
- 人工采纳：该历史作业 43 条 applied；落在 V2 省略集的仅 2 条本地 `set_type→paragraph` 归一（规划/复核一致，非模型复核被省略而丢失），无“人工采纳项存在两遍分歧”。

### 结论 / 未验收

- **V1 有低风险离线收益证据**（候选与复核选择不变，机制明确）；**V2 收益明确但质量未证**：单样本单模型、两遍一致非真值、Journal/Authentic 无法回放、token 为估算。均 **不能声称可部署或准确率/真实耗时已验证**。
- 生产默认路径未变：`select_review_targets` 仅新增默认 `strict_local=True` 的可选参数（默认行为同 `d7a0cbd`）。
- 产物：`tests/manual/experiments/structure_context_experiment.py`、`tests/test_structure_context_experiment.py`（11 项边界回归，全量 `pytest` 390 passed 含既有 379）、`docs/verification/structure-context-experiment-2026-10-14.md` 与同名 JSON。
- 下一步（交 Lead/用户决策）：真实小样本 A/B（3 样本×2 模型×3 分支，≤120 请求，硬预算；人工小规模 gold 覆盖全部未收录标题与历史分歧项）；V2 只要漏掉任一 gold 未收录小节或省略任一人工采纳修正即不通过，保留 baseline+V1。

## 2026-10-14（第十九轮）：翻译链路流式传输、超时切分重试与读超时可配（Lead 直接实施，已编码/自动测试/tsc/生产前端构建，未重建 sidecar/未安装/未真实 provider 验收）

### 诊断背景（只读检查真实库，未改数据）

- 《The White Lotus Teachings》300 段译文中 **16 段 failed**，错误全部为「等待模型响应超时（等待上限 180 秒）」；该书 Qwen（中转）用量 815,668 输出 token 中 **721,697（88.5%）为 reasoning**；《On Being Authentic》同型（958,431 输出中 859,784 为 reasoning）。
- 三个叠加缺口：翻译链路从未调用 `with_read_timeout`（吃 provider 默认 180s，只有结构用 600s）；超时后不沿 segment 切分重试（只有协议失败才切），整批 12 字段一起判死，连续 3 块全失败触发 `GLOBAL_FAILURE_LIMIT` 熔断；思考模式开关后端协议已落地但此前前端不可达（前端入口已在第十八轮提交）。

### 实现

1. **SSE 流式请求**（`literature/providers.py`）：`_complete` 默认 `stream=true + stream_options.include_usage`；新增 `_read_stream` 聚合 `delta.content`（`delta.reasoning_content` 只参与保活、不计入译文），usage/finish_reason/model/id 取自事件流，聚合成与非流式一致的响应 JSON 后走同一下游提取。读超时语义变为「两个数据块之间的空闲时限」。三重回退：①服务端 400 拒绝流式 → 置 `_stream_disabled` 本会话永久回退非流式，同一 attempt 预算内重发（hooks `end(final=False)` 释放本次预留，不重复预留）；②空事件流（无内容/无 usage/无 finish_reason）→ 同上回退，不伪造成空译文；③中转站忽略 `stream` 返回完整 JSON（content-type 非 event-stream）→ 按非流式解析，不关流式。流中网络中断仍映射 `ProviderTimeoutError(read)`；`_HTTPStatusError`/`_StreamUnsupported` 为模块内私有异常；429/5xx 退避重试与 attempt 预算钩子语义不变。
2. **超时切分重试**（`literature/translation.py`）：`_run_chunk` 捕获 `ProviderTimeoutError` 调 `_retry_batch_after_timeout`：批次沿 segment 中点切半（复用 `_split_units`），两半分别隔离重试，递归受 `MAX_SPLIT_DEPTH=3` 限制；单 segment 无法再切或超深时才把该批标失败（错误带「请求超时：」前缀）。每个成功子响应仍立即落盘中间证据；超时分支 `batch_run_id` 置空不影响 run_id 归属。
3. **读超时可配**（`literature/api.py`）：`TranslationJobInput.request_timeout_seconds`（默认 300，范围 30–1800），启动翻译作业时用 `with_read_timeout` 应用（`hasattr` 守卫：测试/自定义 provider 未实现该方法时保持原超时）。冻结快照不含超时，恢复沿用创建时的配置值。
4. **reviewer 只读复验后的修复（E/F 回归）**：复验发现非流式分支的 `response.json()` 解码失败不再归成 `ProviderError`（HEAD 原实现包在 `except Exception`），导致①错误文案变成原始 JSONDecodeError、②`provider_error_streak` 不自增 → 连续失败熔断对「200+门户 HTML」失效。修复：非流式与 relay-JSON 分支的解码失败统一经 `_HTTPStatusError`（status=200 不在重试集，外层转 `ProviderError` 且 hooks `end(final=True)`）。**reviewer 第二轮定向复验证实 E 有效且熔断恢复（200+HTML 连续 3 次即触发 GLOBAL_FAILURE_LIMIT），但发现 F 修复在真实 httpx 流下无效**：非 event-stream 分支在未 `read()` 的流式响应上访问 `.json()/.text` 抛 `httpx.ResponseNotRead`（它不是 ValueError/HTTPError，不会被捕获），relay-JSON 回退与 200+HTML 在真实流下全部失效（MockTransport 预置 `_content`，故单测未暴露）。修复：该分支解析前先 `response.read()`；另在 `_complete` 增加 `except httpx.StreamError` 兼底（任何流读取异常都归 `ProviderError`，熔断语义不破口）。新增真实本机 HTTP 服务（127.0.0.1 随机端口）回归 3 项：relay 返回 JSON、SSE 体标错 content-type、流式 200+HTML → ProviderError 而非 ResponseNotRead。SSE 聚合抽为模块级 `_aggregate_sse_events` 复用。另按复验意见：`_stream_disabled` 语义表述修正为「本 provider 实例」（每作业新建 provider，新作业仍会先试一次流式）；确认流式覆盖所有 `_complete` 调用方（结构/即时/译审/vision）属有意设计。复验观察项 3（超时切分块 `model_run_id` 为空、`run_token` 正常）与 4（跨半块成功半不落 partial、存为 candidates 后恢复重发，既有行为）记录在案，均不阻塞。

### 测试与验证

- 新增 `tests/test_translation_streaming.py` **12 项**：SSE 聚合含 usage/finish_reason/request_id、思考增量不计入内容、请求体含 `stream_options.include_usage`、400 拒流式回退（非流式重发成功且本实例关闭流式）、中转站忽略 stream 返回 JSON 正常解析、空事件流回退、流中 ReadTimeout 映射 `ProviderTimeoutError(kind=read)`、qwen_thinking 字段在流式路径照发、200+HTML 归 `ProviderError`、relay 分支非法 JSON 归 `ProviderError`、无 content-type 的 SSE 体照常解析、超时切分后全部块译出、单段超时任务 failed 且条目错误含「超时」、`request_timeout_seconds` 默认/自定义。
- 既有测试适配：`test_structure_calibration.py` 三个 patch `httpx.Client.post` 的用例与 `test_structure_speed.py` 预算用例显式置 `_stream_disabled = True`（它们验证的是非流式路径的请求体组装/超时语义）；images 用例的 Mock 响应补 `status_code = 200`。
- 全量 `pytest` **379 passed**（364 + 新增 15，含 3 项真实本机 HTTP 流回归）；`tsc --noEmit`、Vite 生产构建通过。reviewer 两轮复验：基线三项全绿；定向检查（回退路径预算峰值=1 无双重预留、流中超时 final=True、超时切分中间证据落盘 spy 证据、with_read_timeout 保留 `_stream_disabled`、E 修复后熔断恢复）全部 PASS；第三轮待复验 ③ 真实流三项。

### 边界与未验收

- 未重建 sidecar/Tauri 包、未安装客户端（当前已安装客户端仍为第十八轮 revision `21664b1e108399c8335c`，不含本轮流式改动）。
- 未用真实中转/真实 provider 验证：流式兼容性（尤其各家中转对 `stream_options` 的支持）、真实耗时/费用/失败率改善。中转站若对 `include_usage` 静默忽略，usage 将记为未知费用（既有 `unknown_cost_records`/待对账路径承接）。
- 真实 MinerU/付费模型、原生交互、公证仍未验收（未变）。

## 2026-10-14（第十八轮）：AI 结构校对提速保质（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离浏览器，未真实模型 A/B、未安装）

目标：在不牺牲漏标召回的前提下加快 AI 结构校对。不改数据分层、不改真实库/raw、不调付费服务。

### A. 本地候选构建（语义不变）

- `structure_model_payload` 对 `TEXT_STRUCTURE_TYPES` 先按 `>120` 字符、`reconcile_toc` 补充匹配先按 `>140` 字符跳过，均在逐条目录相似度之前；`normalize_heading`/`_heading_similarity`（structure.py）与 `heading_key`/`heading_similarity`（structure_signals.py）加有界 `lru_cache`，`heading_similarity` 用 `2*min/(len_a+len_b)` 上界在低于 `MATCH_FLOOR` 时直接返回（对齐 DP 只按该阈值判断）。
- **语义一致**：只读快照重跑完整 payload（含 `local.toc_score` 等）SHA-256 与修改前逐字一致，候选数不变。
- 自然 cold 本地耗时：White Lotus **37.744503→3.263567s**（348）、Metropolitan Journal **12.810176→1.177188s**（228）、On Being Authentic **2.556754→0.343734s**（58）。只代表本地候选构建，不是 AI 总时长。
- 手动可选基准 `tests/manual/structure_selection_benchmark.py`（cold/warm、payload SHA、复核统计），不进全量 pytest、不依赖真实库。

### B. 选择性第二遍复核

- 新增 `structure_reasoning.select_review_targets`：第一遍规划只在“有节点、角色合法、置信度有限且在 [0,1]、父子关系成立（含 `validate_hierarchy` 预检）、与确认目录锚点不冲突、父标题已复用”时复用。
- 必复核：疑似漏标、现有标题降为非标题、与确认目录层级冲突、目录已定位但判非标题、未收录小标题判非标题、改层级但无确认目录锚点（锚点要求 `score≥0.92` 且 `level_source=="confirmed"`）、本地仍像标题或非散文短块。
- `proofread_full=true` 可回退全量。历史规划响应回放（不是新模型 A/B）：White Lotus 348 候选复用 87、复核 261（7 批 vs 原 9 批），其中 241 本地形态保守、18 置信不足、2 树冲突。

### C. 并发复核（默认 2 路，最多 4 路）

- 全局规划保持顺序依赖；第二遍固定整体标题树上下文、40 条一批并发。**有界在途调度**：在途 future 不超过并发上限，成功才提交下一批；失败即 `stop_event`、取消未开始批次、等待在途收尾，`_call` 发送前检查 `stop_event`，失败后不再付费。
- 线程安全：审计/缓存/写账/进度用 `RLock`；每批独立 `decisions`/`skipped` 按批次顺序稳定合并；`rejected_count/reasons` 按 `batch_tag` 归属；`_attempt_state` 线程本地；服务并发/退避/预算仍由公共 `RequestCoordinator` 封顶。
- `BudgetExhausted` 发送前拦截：不记未知费用、不留待对账、不 commit，审计标 `status=blocked/sent=false`；写账异常 `finally` 仍用已知 usage 对账句柄并保留 `record_pending_cost`。

### D. 思考模式/预算（显式协议，默认关闭）

- 模型服务新增 `reasoning_style`（`openai_effort`/`qwen_thinking`/`glm_thinking`）+ `reasoning_effort`/`reasoning_budget`；`reasoning_request_fields` 仅在显式选择且值合法时拼字段，默认全空、向后兼容。
- 规划/复核 `reason` 上限收紧到 30 字；不缩小 `max_tokens`。
- 同时移除未充分验证的全局默认 SSE 扩展（恢复非流式 `stream:false`，删除 `_read_stream`/`_StreamUnsupported`/`_HTTPStatusError`/`_stream_disabled`）与翻译模块新增的超时二分自动重发（越界且可能重复付费）；`ProviderTimeoutError` 改为条件提示。

### E. 进度与 UI

- 规划 70%→80%、复核 82%→92%、校验 93%、建议 94%；活动逐批输出候选数、批数、实时完成数与已用时。
- 结构结果栏显示“候选 N · 第一遍复用 X · 第二遍复核 Y”；运行中显示总耗时与当前阶段（含进行中秒数）；高级选项可切全量复核与 1–4 并发并真实 POST。思考预算 0 标注“沿用服务默认”，关闭思考提示不承诺准确率。

### 验证 / 未验收

- 全量 `pytest` **364 passed**；新增 `tests/test_structure_speed.py`（复用边界、可移植合成夹具、并发并行度/顺序、实时进度、失败停止付费、全量回退、MockTransport 级结构预算集成、思考字段）。`tsc --noEmit` 与 Vite 生产构建通过。
- 新增隔离真实浏览器 `tests/manual/structure_ui_browser_check.py`（阻断非本地网络与业务 Keychain 读取、fitz 生成有效 PDF）：**19/19 PASS、0 脚本错误**，覆盖 POST `proofread_full`/`proofread_concurrency`、运行时总/阶段耗时真实递增、覆盖口径、思考配置保存与兼容默认；原有 `parse_ui_browser_check.py` 全流程仍全部通过。
- **构建**：重建自包含 Python sidecar（revision `21664b1e108399c8335c`，sha256 `7aec110812917c52c8fcee552a7b8ee60c6404e42ec0beeb2708c2b003364052`，size 42888192），并用 `node scripts/tauri-run.cjs build --config src-tauri/tauri.bundle.conf.json --config '{"build":{"beforeBuildCommand":""}}'`（前端 dist 已单独构建，避免总脚本安装）生成 `src-tauri/target/release/bundle/macos/Literature.app`（54.52 MiB）；包内 sidecar 与 `src-tauri/binaries/manifest.json` 及源二进制 SHA-256/大小逐字一致，`codesign --force --deep --sign -` 后 `codesign --verify --deep --strict` 通过（adhoc，TeamIdentifier 不设）。**未安装/未启动客户端。**
- **Lead 独立复验**：禁用真实凭据及外部请求的隔离全量测试 **364 passed**（13.22 秒）；TypeScript 检查、结构浏览器 **19/19**（0 脚本错误）通过。三份只读文献快照的新旧完整 payload 一致；两路复核的预算拦截无未知费用或残留预留；包内 sidecar 的源码 revision、manifest SHA-256/大小及严格签名检查通过。原有解析浏览器全流程通过为 Orca Worker 报告，本轮 Lead 未重复执行。
- **安装/提交**（用户 2026-10-14 授权）：安装前目标客户端未运行；先把上一版 `launcher/Literature.app`（sidecar sha256 `6915c3a4…`）备份到 `launcher/.rollback-Literature-6915c3a4.app`（不提交），再用 `scripts/setup_macos_launcher.sh --app-only` 安装到 `launcher/Literature.app`。安装后内置 sidecar 与源包及 `src-tauri/binaries` 逐字节一致（sha256 `7aec1108…`，size 42888192，revision `21664b1e108399c8335c`），`codesign --verify --deep --strict` 通过（adhoc）。**未启动客户端**。本轮创建本地 Git 提交，仅含结构优化相关源码/测试/手动验收脚本与两份文档，不含产物/缓存/真实库与既有未跟踪文件；只本地 commit，不 push。
- **未验收**：无真实模型 A/B，不能声称真实准确率或 AI 总时长提升；未启动客户端、未改真实库/raw、未调付费服务。真实 MinerU/付费模型、原生交互、公证安装仍未验收。

## 2026-10-14（第十七轮）：客户端安装与本地 Git 提交（用户授权）

用户明确授权“安装新版 app，提交 Git”。本轮仅安装已验收包并做本地提交，不新增功能、不重建、不 push。

- 源包：`src-tauri/target/release/bundle/macos/Literature.app`，sidecar revision `613a128fc4c0146af0e2`、sha256 `6915c3a4112c42354ca5469ba837b745d03beeb97edccae11e3e6b342a3c90f0`、size 42873728。
- 安装前：`launcher/Literature.app` 及内置 sidecar 未运行；已停止本轮遗留的 `/tmp/lit-sidecar` 开发 sidecar；旧客户端备份到 `launcher/.rollback-Literature-0e308e43.app`（不提交）。
- 用仓库既有 `scripts/setup_macos_launcher.sh --app-only` 安装到 `launcher/Literature.app`。安装后：内置 sidecar sha256/size 与已验收包/`manifest.json` 一致，且与源包 sidecar 字节相同；`codesign --verify --deep --strict` 通过。**未启动客户端**（启动可能自动读取真实 MinerU Token）。
- 本地提交包含本轮及作为该版本依赖的此前未提交实现（导出/TBL 移除、本地 MinerU 边界、任务来源/冻结契约、可靠恢复、请求协调/预算、revision/窗口化、结构确认/缓存、定向译审原子采纳）及对应测试/打包脚本/文档/必要源码资源。
- 未提交（有意排除，文件保留）：`opendesign-projects/`、`docs/prototypes/`、`docs/verification/` 与大型截图、`launcher/`（含备份 app）、构建产物与缓存、真实资源库/运行数据；已扫描提交范围无真实 Token/API key。仅本地 commit，不改身份/远端/分支，不 amend。
- 仍未验收（未变）：真实 MinerU、真实付费 provider、原生窗口逐项点击、公证与干净机器安装；结构裁片/目录磁盘缓存未实现。

## 2026-10-14：Lead 独立收口验收（Orca Worker developer 实施结果）

- 在临时 library/runtime 下独立执行 Python 全量测试：**336 passed / 0 failed**；禁用真实 Keychain 读取与非本地 httpx 外网请求。TypeScript `--noEmit` 通过。
- 独立执行生产前端 + 临时项目库 + fake MinerU 的浏览器全流程：**103/103 PASS**，包括长书窗口化、编辑草稿往返、保存失败保留、候选展示/采纳与项目切换迟到响应。无脚本错误；唯一 `net::ERR_FAILED` 来自刻意 abort 的保存失败用例。审校响应使用 stub，采纳使用真实本地 API 的版本校验。
- 独立重跑人工编辑竞态：人工写入在采纳临界区内被阻挡，随后最新人工内容保留，已采纳候选留在平面修订历史。另重跑源文写入竞态：源文写入被阻挡至采纳结束；源文随后变化时旧候选被拒绝，译文不被覆盖。
- 独立核对自包含客户端：源码与 manifest revision 均为 `613a128fc4c0146af0e2`；包内 sidecar SHA-256 为 `6915c3a4112c42354ca5469ba837b745d03beeb97edccae11e3e6b342a3c90f0`，大小 **42873728**，均与 manifest 一致；`codesign --verify --deep --strict` 通过。
- 本轮验收限于已实现的部分重构与优化。译文 revision 是 full/no-change 条件请求，条目级差量与结构裁片/目录磁盘缓存未实现。真实 MinerU/付费模型、原生交互、公证与干净机器安装未验收，未测量真实文献的速度/token/费用改善。未提交 Git、未安装/启动客户端、未替换 launcher、未修改真实文献库。

## 2026-10-14（第十六轮）：采纳译审候选的原子性与服务端校验（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离浏览器，已构建未安装）

### A. 采纳原子化

- `adopt_review` 把“源 hash 校验 + 译文 `updated_at` 基线校验 + 按源文所需字段的完整性校验 + `edit` 读改写落盘”放在**同一临界区**：锁序统一为 `document_lock → translation_lock`，`edit` 在临界区内重入翻译锁（RLock），不再验证后释放锁再写。保留 `edit` 的锁定与平面修订历史。
- 回归 `test_adopt_review_is_atomic_against_concurrent_manual_edit`：在采纳调用 `edit` 前启动另一线程人工编辑；**先临时去掉临界区验证旧实现失败（final=旧候选覆盖内容）**，修复后 final=人工最新译文且旧候选可见于修订历史。

### B. 服务端校验与必填

- `source_hash`/`base_revision` 必填（`ReviewAdoptInput` 与服务层双重校验，缺版本依据 → 拒绝/422）；服务端按源文所需字段校验候选完整性，缺字段拒绝且不改库；`candidate_id` 明确为客户端幂等/去重凭证（候选不落库，不是服务端证据）；重复采纳同内容时幂等返回，否则 409 并保留最新人工内容。
- 回归：`test_adopt_review_rejects_missing_basis_and_incomplete_candidate`（缺依据/表格缺 cell 均拒，库不变）。

### C. 浏览器 stub 候选提供有效版本依据

- `parse_ui_flow_check.cjs` 在审校候选验收前从真实 API 读取 `big-p0` 的 `source_hash`/`updated_at`，stub 候选带上它们，使采纳走服务端真实校验。文档明确候选身份是客户端凭证。

### 验证 / 产物

- 全量 `pytest` **336 passed / 0 failed**；`tsc --noEmit` 通过；Vite 生产构建；隔离真实浏览器 **103/103 PASS、0 脚本错误**。
- 自包含 Tauri：sidecar revision `613a128fc4c0146af0e2`（与源码 `RUNTIME_REVISION` 一致）/ sha256 `6915c3a4…` / size 42873728；`Literature.app` 54.50 MiB；bundle 内 sidecar 哈希与 manifest 一致；adhoc 签名 `codesign --verify --deep --strict` 通过。**未安装/未启动客户端**。未调真实服务、未读真实 Keychain、未改真实库、未提交 Git。
- 外部未验收：真实 MinerU、真实付费 provider、原生窗口点击、公证；结构裁片/目录磁盘缓存未实现。

## 2026-10-13（第十五轮）：草稿父状态化、候选身份/基线校验、review 生命周期与真实浏览器验收（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离浏览器，已构建未安装）

### 1. 窗口化编辑草稿不丢

- `ReadingBlockCard` 的 `editing`/`draft` 改为父级控制：`ReadingWorkspace` 按 `block_id` 持有 `editingIds`/`drafts`，range↔overlay 切换重挂载不再丢草稿；可多块同时编辑；保存失败保持编辑与草稿（成功才 `onEditingChange(false)` 清草稿）；焦点只在进入编辑那一刻拉取，滚动重挂载不拉回。`useVirtualWindow` 每次 render 与真实 DOM 同步 `scrollTop`/`clientHeight`（重入/外部滚动不再停在旧值）。
- 浏览器验收（生产 `frontend/dist` + 隔离库 + 1000+ blocks）：`窗口化往返编辑草稿不丢`、`保存失败不丢草稿`（route.abort 拦截 PUT）、`结构结果栏 DOM 有界/滚到底渲染尾部块`。

### 2. 候选身份/基线校验

- `review` 返回 `job_id/project_id/locale/block_id/candidate_id/source_hash/base_revision`，并含 `source/current`；完整性按源文所需字段校验（缺字段 `complete=false`，不可采纳）。
- 新增 `POST /projects/{id}/translations/{block_id}/review/adopt`：在译文写锁内校验源 hash 与译文 `updated_at` 基线，不符 409 并保留最新人工内容；采纳走既有 `edit`（锁定 + 修订历史）。前端候选按 `project:locale` 隔离、切项目/语言清候选并用代次丢弃迟到响应，面板展示原文/现译/建议。
- 回归：`test_review_adopt_validates_source_and_translation_baseline`（源变/译文被人工改均拒绝、正确基线采纳且保留修订历史）。浏览器：`审校候选展示原文/现译/建议`、`采纳后候选面板关闭`、`切项目后迟到审校响应被丢弃`。

### 3. review/instant 生命周期

- `review`/`instant` 均有真实终态：请求前 `running`，成功 `completed`，预算不足 `budget_paused`，异常 `failed`；返回 `job_id`；API 在 `finally` 关闭 provider；候选拒绝/取消不等于请求未结束。预算不足尚未发送不记未知付费。
- 回归：`test_review_budget_paused_sets_terminal_status`、`test_targeted_review_returns_candidate_without_auto_save`（断言 review 作业 completed）。

### 4. 文档范围诚实

- 本轮新增的真实浏览器交互为草稿往返/保存失败/审校候选与拒绝采纳/切项目迟到/结构窗口化；旧 95 用例不再被用来声称这些已验收。外部仍未验收：真实 MinerU、真实付费 provider、原生窗口点击、公证；结构裁片/目录**磁盘**缓存未实现。

### 验证 / 产物

- 全量 `pytest` **334 passed / 0 failed**；`tsc --noEmit` 通过；Vite 生产构建。隔离真实浏览器 **103/103 PASS、0 脚本错误**（故意 abort 的 net::ERR_FAILED 已排除并标注）。
- 自包含 Tauri：sidecar revision `dd731bb60a4216298d4f`（与源码 `RUNTIME_REVISION` 一致）/ sha256 `8e2e2b7f…` / size 42869472；`Literature.app` 54.42 MiB；bundle 内 sidecar 哈希与 manifest 一致；adhoc 签名 `codesign --verify --deep --strict` 通过。**未安装/未启动客户端**。未调真实服务、未读真实 Keychain、未改真实库、未提交 Git。

## 2026-10-13（第十四轮）：attempt 预算持有到对账、结构逐响应、revision 源指纹、定向译审与窗口化（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离浏览器，已构建未安装）

### 1–2. attempt 预算与结构逐响应（真实 provider 路径）

- provider 每次 HTTP attempt 的预算预留在 `final=True` 时**不在 end 释放**，写入线程本地句柄；调用方写 usage 后 `_complete` 原子 `reconcile`（成功/ProviderError/超时/退避 attempt 一致且幂等），消除 release-before-commit 重复花钱窗口。无 hooks 的 provider 仍走外层预留 + `coordinator.limit` 服务额度。
- 结构响应在 `record(response, scope)` 后逐请求 `reconcile`（不再作业结束用项目总 usage delta）；结构作业不再提交整项目 delta。
- 回归：`test_attempt_reservation_prevents_overspend_under_concurrency`（真实 provider + `httpx.MockTransport`，budget=.0003 两路并发只花一笔、spent=.000202）、`test_per_attempt_budget_blocks_before_http`。

### 3. revision 覆盖源文版本

- 译文 payload/`?since=` 的 revision = `locale|layer.updated_at|document.json mtime+size|REVISION_PROTOCOL`；源文/结构/译文/术语变更均使其失效。空译文层用稳定 `updated_at=""` 避免每次调用变化。前端 revision 按 `project:locale` 隔离，过期响应不回写。
- 回归：`test_translation_revision_invalidates_on_source_change`（改源后 `since` 不再 unchanged）。

### 4. 待对账费用

- `record_pending_cost` 带 `input_tokens/output_tokens/estimated_cost/request_id` 等可对账身份；超时/无响应/写账失败写 `usage_pending.jsonl`，有响应但缺 usage/单价只计 `unknown_cost_records`（NULL 行），不重复写 pending；前端徽标显示“未知/待对账”合计。

### 5. 上下文边界

- `_chunk_context` 不再跨章节/语言取前后文（遇标题或语言变化即停止）；串行 `previous_translation` 在新章/换语言时清空；run/estimate 共用。
- 回归：`test_chunks_break_on_chapter_and_language_boundaries`。

### A. 阅读/结构列表窗口化

- 新增 `frontend/src/workbench/useVirtualWindow.ts`（可变高度、测量缓存、固定块悬浮不卸载、按估算偏移跳转），接入阅读页与结构结果栏；随目录跳转调用 `scrollToBlock`。
- 隔离浏览器新增 1000+ blocks 大书验收（`parse_ui_harness` 种子 Big Book）：初始 DOM 有界、滚到底仍有限、尾部块可渲染；**95/95 PASS、0 脚本错误**。

### B. 定向译审（候选 → 采纳/拒绝）

- `TranslationService.review`：对照原文+现译产出修订候选（走同一公共预算层），不自动保存；`POST /projects/{id}/translations/{block_id}/review`；前端在阅读页展示候选，“采纳并保存”走既有编辑接口、“拒绝”不改译文。锁定人工内容不自动覆盖。
- 回归：`test_targeted_review_returns_candidate_without_auto_save`。

### 验证 / 产物

- 全量 `pytest` **332 passed / 0 failed**；`tsc --noEmit` 通过；Vite 生产构建；隔离浏览器 **95/95 PASS、0 脚本错误**。
- 自包含 Tauri：sidecar revision `9e9aafbafc49e7c55d77` / sha256 `1e61ed4f…` / size 42870144；`Literature.app` 54.50 MiB；bundle 内 sidecar 哈希与 manifest 一致；adhoc 签名 `codesign --verify --deep --strict` 通过。**未安装/未启动客户端**。未调真实 MinerU/provider、未读真实 Keychain、未改真实库、未提交 Git。
- 仍未外部验收：真实服务端到端、原生窗口点击、公证；结构性裁片/目录磁盘缓存未实现（请求缓存已实现）。

## 2026-10-13（第十三轮）：中间证据同源/同阶段、崩溃窗口、预估失效与公共预算收口（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器，未安装/未真实服务验收）

### 中间子段证据（Lead 复现的源变混用）

- `partial_segments` 改为每块记录 `{segments, source_hash, refine}`；`_run_chunk` 只种入“本块当前 source_hash 相同且阶段一致”的子段。源文变或阶段不符直接丢弃，重发全部子段，不把旧源结果拼成当前完整。
- 回归：`test_partial_segments_are_discarded_when_source_changes`（前 15 字等长改为 CHANGED SOURCE：旧中间段全部丢弃，新源实际重发并出现在译文）。

### 崩溃窗口：每个有效子响应立即落盘

- `_run_chunk` 每个协议校验通过的批次（未被截断/缺段）立即调 `_record_validated_segments` 原子写 checkpoint（带 source_hash/refine，加锁合并）；`_persist_partial_segments` 只在预算暂停时补录；完成块会清除对应中间证据。
- 回归：`test_crash_mid_stage_persists_paid_subsegments`（第 2 请求 KeyboardInterrupt 绕过收尾：第 1 请求已付费子段已在 checkpoint，恢复不重发）。

### 预估 UI 失效与摘要 scope

- `onInvalidateEstimate` 改为稳定 `useCallback`（空依赖），选项变化或挂载时才失效，不再每次 render 清空估算。`list_job_summaries` 保留小 `scope` 投影。

### 未知/待对账费用持久化

- `usage_summary` 新增 `pending_cost_records`（`usage_pending.jsonl` 行数），超时/无 usage/写账失败均写待对账证据；重启仍统计。
- 协调层 `_spent_for` 账本不可读时返回 None，`reserve` 在 budget>0 时保守拒绝（不 seed 0 继续花）。
- 回归：`test_pending_cost_persists_unknown_across_restart`、`test_budget_seed_failure_blocks_spending`。

### 每 attempt 预算 + 结构/即时共用额度

- provider 每次 HTTP attempt 先调 `attempt_hooks`（传入 prompt 字符数）；协调层 `attempt_hooks_for(..., project_id, budget, cost_of)` 在每次 attempt 原子 `reserve`，成功后由调用方 `commit` 实际费用（未知计 unknown）。翻译/即时/结构均接入（结构作业完成后把实际花费 delta `commit` 入协调层）。
- 回归：`test_per_attempt_budget_blocks_before_http`（真实 provider + `httpx.MockTransport`：预算不足时 0 HTTP）、`test_429_retry_calls_attempt_hooks_and_records_cooldown`。

### R08 章节/语言边界

- `_chunks` 在标题（新章）或章语言覆盖变化处断开；`run`/`_process_blocks`/`estimate_translation` 用同一规则。回归：`test_chunks_break_on_chapter_and_language_boundaries`。

### R11 revision 增量 / R12 结构请求缓存

- 译文层下发 `revision`（`updated_at`）且 `GET /translations/{locale}?since=` 未变则返回 `unchanged` 小载荷；前端轮询带 revision。
- 结构进程内请求缓存（key=`context_key:request_key`，context_key 含模型配置+文档版本；TTL 1h、上限 400），失败/不合法响应不入缓存；只在能确定上下文时启用。回归 `tests/test_structure_cache.py`。
- backend_config 校验扩展到 model_version/language_pack/page_ranges（回归 `test_backend_config_model_language_page_ranges_validated`）。

### 验证

- 全量 `pytest` **329 passed / 0 failed**；`tsc --noEmit` 通过；Vite 生产构建与隔离浏览器 **92/92 PASS、0 脚本错误**。本轮**未重建 sidecar/Tauri**（按 Lead 要求等全部完成后再一次构建）；当前已构建产物 revision `9720d7cf2648f8008a46` 早于本轮改动。
- 仍待：R11 结构/阅读 block 窗口化；R12 定向译审的 UI 采纳链与裁片/目录缓存。

## 2026-10-13（第十二轮）：预算一致性、已付费子段、未知费用与解析结果消费（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器，未安装/未真实服务验收）

### A. 预算事务单一一致性边界

- `RequestCoordinator` 自己维护 `spent`（进程内唯一来源，首次从持久账本 seed），`reserve` 在锁内一次完成“取最新已花 + 在途 + 本次估算”判定；`reconcile` 在锁内释放预留并累计已花。不再由各 `TranslationService` 缓存自己的已花金额（那是 Lead 复现的 stale 标量竞态）。
- `TranslationService` 只传协调器（不再传 spent 标量）；默认协调器绑定 `store.usage_summary` 作 seed。回归：`test_shared_coordinator_budget_is_atomic_under_stale_read`（顺序 + 8 线程并发不超卖）。

### B. 已付费子段作为中间证据

- `_ChunkOutcome` 新增 `budget_paused`；`_run_chunk` 捕获 `BudgetExhausted` 后返回已解析子段（不再丢弃）；`_process_blocks` 将未完成块的已付费子段写入 checkpoint（`partial_segments` + `partial_meta{run_token,refine,recorded_at}`），完整块照常提交。
- 恢复时按 `run_token` 读回中间子段，只发缺失子段；不把半句写成 translated。回归：`test_budget_pause_keeps_paid_subsegments_and_resume_does_not_resend`（已付费段不重发、最终拼接无丢段）。

### C. 公共请求契约

- `OpenAICompatibleProvider` 新增 `attempt_hooks`，每次真实 HTTP attempt 前后进入公共层；`RequestCoordinator.attempt_hooks_for` 挂上服务级并发/退避（429/5xx 记冷却）。翻译在 `run()` 一次挂载（避免每调用赋值竞态），`_complete` 检测到 hooks 时不再重复持有外层额度。即时翻译经同一 `_complete`（预算+并发）。结构作业也挂 attempt hooks（服务级并发/退避）。
- 回归：`tests/test_request_coordinator.py` 用 `httpx.MockTransport` 验证 429→重试→成功且冷却被记录、服务额度满时阻塞。**未做**：结构作业的预算预留（仍由目标计费路径为主）、内部重试的逐 attempt 预算预留（预算按逻辑请求预留）。

### D. 未知费用持久化与可见

- `usage_summary` 新增 `unknown_cost_records`（`estimated_cost IS NULL` 计数，持久）；`/status`/项目详情下发；前端用量徽标显示“N 个请求费用未知”。写账失败时保留待对账证据（计 unknown）并按已知费用对账，不 release 后当零。

### E. 预估失效与摘要 scope

- `ScopeDialog` 任一选项（scope/target/glossary/instructions/refine/force/polish/workers）变化即调 `onInvalidateEstimate`（递增代次 + 清空），无需再点预估；迟到响应被代次校验丢弃。
- `list_job_summaries` 补回小 `scope_json` 投影并解码 `scope`，阅读面板不再把选章任务显示为全书。

### F. 解析结果真实消费

- `MergeCallback` 改为 `(WorkflowJob, ParseResult)`；`ocr._finish_import` 消费 `ParseResult`（raw 目录 + source_pages，旧任务回退切片推导）；`_seal_and_merge` 不再强制 `SliceState.DOWNLOADED`，校验目录存在即可。
- 回归：`test_fake_local_parse_result_reaches_canonical_with_noncontiguous_pages`（fake local-like 无 SliceJob、无 Token，raw 规范文件 + 非连续源页 3/5 进入真实 seal/normalize/merge，0-based page_index=0/2/4）。`prepare` 校验 `backend_config` 布尔开关；**未做**：模型/语言/页范围的更严校验。

### 验证

- 全量 `pytest` **319 passed / 0 failed**；`tsc --noEmit` 通过；Vite 生产构建与隔离真实浏览器 **92/92 PASS、0 脚本错误**。自包含 Tauri 已重建（未安装）：sidecar manifest revision `9720d7cf2648f8008a46` / sha256 `b3be3324…` / size 42858208；`Literature.app` 54.49 MiB；bundle 内 sidecar 哈希与 manifest 一致，adhoc 签名验证通过。**未安装/未启动客户端、未调真实 MinerU/provider、未读真实 Keychain、未改真实库、未提交 Git。**
- 仍待继续：结构预算预留与更严 backend_config 校验、R11 译文 ETag/block 窗口化、R12 结构目录/裁片缓存与带原文定向译审、R08 章节/语言边界。

## 2026-10-13（第十一轮）：请求协调/预算、解析契约、轻量状态与结构确认（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器，未安装/未真实服务验收）

### R09 请求公共层与预算

- 新增 `literature/request_coordinator.py`：服务级有限并发（`service_id` 信号量）、429/5xx 退避冷却、项目预算在途预留 `reserve/release`、未知费用计数。
- `TranslationService` 接入：每次真实 HTTP （含子批拆分/重试）在 `_complete` 先原子 `reserve(spent+reserved+本次估算)`，不足即 `BudgetExhausted`（作业进 `budget_paused`，不标记为漏译失败、不清旧译）；响应拿到后**立即写 usage 再释放预留**（避免 release 后等主线程晚写 usage 的重复花钱窗口）；无 usage/无单价/超时计入 unknown 而非 0。`JobManager` 持有共享 coordinator 并注入翻译作业。
- 回归：`test_budget_guard_rejects_before_any_request`（余额不足 → 0 请求 + budget_paused）、`test_unknown_cost_is_marked_not_zero`、`test_inflight_budget_reservation_stops_before_overspending`。**未做**：结构/即时请求接入共享并发额度（仅翻译路径接入）、服务级限流的真实 429 验证。

### R09 预估契约

- `TranslationEstimateInput`/`estimate_translation` 新增 `force`（已有译文时 force 预估非零），预估复用真实请求组装（含 `_batch_units` 后的实际请求数、前后文/术语/system/润色）并返回 token/费用区间与 `has_pricing`。
- 前端 `ScopeDialog` 的“预估”改为传递当前弹窗实际选项（target locale/useGlossary/instructions/refine/force/polish），`App.estimateScope` 按选项传参并加请求代次防止迟到响应覆盖；面板展示区间与未知价格状态。

### 解析契约与本地口径（Task 4）

- `parse_backends.py` 新增统一 `ParseResult`（raw 目录 + source_pages）；删除与实际签名不符的旧 `Mapping→Mapping` Protocol。
- `mineru_workflow.py` 定义与实际一致的 `ParseBackend` 协议（`prepare/execute/status` 操作 `WorkflowJob`，`execute` 返回 bool，`result` 返回 `ParseResult`）；`RemoteMinerUBackend.result` 返回 `ParseResult`，共享协调层 `_seal_and_merge` 统一调用 merge 回调（seal/normalize/merge）。`prepare` 校验 `backend_config` 关键开关；`retry_failed` 与 `run_or_resume` 先 `ensure_available`（local/未知不得建 client/读 Token）。
- 回归：spy 适配器调用链、fake local-like 适配器无需 Token 产出 `ParseResult` 进入同一 merge、`backend_config` 不一致报错。

### R11 轻量状态与请求代次

- `ProjectStore.list_job_summaries` 只投影状态列（不 `SELECT *`、不解码 result/activity）；`GET /projects/{id}/status` 用它。前端轮询改走 `/status`；`ReviewWorkspace` 进入已完成结构作业时按 job_id 拉 `GET /structure/jobs/{id}` 完整详情后再 `applyJobResult`，避免摘要缺 `result/proposed_toc`。
- `App.refreshProject` 改为独立读取 `Promise.all` 并行，并加 `loadSeqRef` 代次 + `activeProjectRef` 双重校验，语言/项目切换的迟到响应不会覆盖新状态。
- **未做**：译文 ETag/revision 增量拉取、结构/阅读 block 窗口化。

### R12 可见质量与结构审查确认

- 译文载荷 `quality_warnings` 现于 `ReadingBlockCard` 可见（带“带原文重译本段”入口，仅提示不自动改）；数字改为 token 化比较（12 不被 120 子串误判）。
- **结构 done 改为显式审查确认**：`ProjectStore.confirm_structure_review` 记录 `{scope, document_updated_at, confirmed_at}`；`structure_review_summary` 新增 `partial` 状态，`done` 仅当存在对应当前文档版本的 full 确认，文档改动后自动失效。新增 `POST /projects/{id}/structure/confirm` 与前端“确认当前审查范围（全书）”按钮；资源卡片显示 partial。
- **未做**：结构目录/裁片/精确请求缓存、带原文定向译审。

### 验证

- 全量 `pytest` **313 passed / 0 failed**；`tsc --noEmit` 通过；Vite 生产构建通过；隔离真实浏览器 **92/92 PASS、0 脚本错误**。自包含 Tauri 已重建（未安装）：sidecar manifest revision `1de73602c1ea369e57f2` / sha256 `3f0f41be…` / size 42853808；`Literature.app` 54.48 MiB；bundle 内 sidecar 哈希与 manifest 一致，adhoc 签名 `codesign --verify --deep --strict` 通过。**未安装/未启动客户端、未调真实 MinerU/provider、未读真实 Keychain、未改真实库、未提交 Git。** 结构/阅读 block 窗口化、ETag 增量、结构缓存/定向译审、结构/即时共用限流仍需继续。

## 2026-10-13（第十轮）：R02 任务来源契约、冻结契约、remote 真实 adapter、预估与质量告警（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器，未安装/未真实服务验收）

### R02 任务来源契约（修正第九轮的不完整修复）

- Lead 复现：6 块已有旧译文 → force → 前 5 块写盘、第 6 块绕过收尾中断 → 同 job 恢复 **0 请求假 completed**。根因是“磁盘所有有效旧结果”被当成本次完成。
- 修复（`literature/translation.py` + `models.py`）：`TranslationEntry` 新增任务级 `run_token`；本次完成 = 磁盘上带本次 `run_token` 且通过校验的块（∪ 本任务自己写入的 checkpoint 清单）。预写 checkpoint 的 `translate_completed` 不再包含旧译文（Lead 场景下为 `[]`）。
- 分块真实提交时 `_apply_outcome` 给成功项打本次 `run_token`，崩溃窗口靠磁盘来源恢复。counts 改用“本轮来源”：force 暂停 1 块后 `translate.completed=1 / unhandled=5`（旧实现会算 6 完成）；已校验旧译文不再被计为 failed（归 unhandled）。
- 回归（先在旧代码失败）：`test_force_resume_treats_only_this_task_results_as_done`（resume_calls=1，预写清单为 `[]`）、`test_force_paused_counts_reflect_this_round_only`、`test_refine_resume_does_not_skip_already_refined`、`test_polish_crash_resume_does_not_fake_complete`；scope 不缩水/锁定/源变由既有点检覆盖。

### 冻结契约（实施，不只是记账）

- `frozen` 快照现包含：完整术语快照（非仅摘要）、附加要求 `instructions`、`prompt_version`、非敏感模型参数（model/base_url/temperature/max_output_tokens，密钥不落快照）、`force/polish` 任务标志。
- 恢复时 `run()` 实际应用冻结值：术语/附加要求/模型参数/执行标志都按快照，不被恢复请求改动；密钥仍运行时从当前服务读取。新增观测点：若代码 `PROMPT_VERSION` 与冻结值不同会记 warning。
- 回归：`test_resume_uses_frozen_model_glossary_and_instructions`（model-after→实际 model-before、prompt 用 OLD 而非 NEW、附加要求被冻结）。服务被删除时 `provider_from` 在 `run()` 前抛出，明确阻止（不静默换模型）。

### remote 真实 adapter（不再只是 Protocol 声明）

- `literature/mineru_workflow.py` 新增 `RemoteMinerUBackend`（`capabilities/prepare/execute/status/result`）；`MinerUWorkflow` 新增 `backend_factory`，`run_or_resume` 先 `ensure_available(backend.job.backend_id)`，再经适配器推进；Token/client 只在 `execute` 内创建。
- local/未知 `backend_id` 的持久化 job 在 `run_or_resume` 入口即被拒（不建远端 client、不读 Token），旧任务缺字段默认 remote。原有 `client_factory`/fake 流程兼容。
- 回归：spy 适配器证明 `run_or_resume` 真实调用 `prepare→execute→status→result→merge`；local/unknown 恢复断言 `client`/Token 均未读取。

### 其余 C（部分完成）

- **R08**：单字段超过 1200 字符沿句子/空白边界预拆为 `key~i` 子段；新增 `_batch_units` 在**请求级**按字符（2400）/字段（12）预算把子段真正拆成多次 HTTP，再聚合完整子段证据拼回同一稳定字段（不再只加协议标签）。大表/多 cell 块同样首发预拆。回归：10000 字符单字段断言发出多个请求且每次 `<src>` 待翻内容 ≤ 3200 字符；缺/截断不拼半句；拼接无 `~` 残留。**未做**：按章节/语言边界组织请求。
- **R09（部分）**：`estimate_translation` 复用真实请求组装（含 `_batch_units` 后的实际请求数），返回 token/费用区间与 `has_pricing`；新增 `_estimate_chunk_cost` + 在途费用预留，并发下 `spent + reserved >= budget` 即 `budget_paused`（回归：4 路并发小预算只发出 ≤ 1 块）。**未做**：服务级共用并发额度/限流、辗转重试/子请求预算入账。
- **R11（部分）**：新增 `GET /projects/{id}/status` 轻量接口（作业去掉 `result`/`activity` 审计 + usage），前端高频轮询改用它；`/projects/{id}` 仍保留完整审计。**未做**：ETag/revision 增量译文、结构/阅读 block 窗口化。
- **R12（部分）**：新增 `entry_quality_warnings`（数字/URL/引用缺失、译文原样/异常偏短）随 `translation_payload` 下发，仅作人工复核提示，不自动改译文。**未做**：结构 done 绑定文档版本/审查范围、结构增量候选/缓存、带原文定向译审。
- 另：force 重译失败时保留“对应当前源文”的旧译文内容（不清空），但 `partial=True` 不计为本轮完成；`_task_accounting` 区分“本轮未处理（unhandled）”与“本轮明确失败（failed）”，旧译不再被误计 failed（Lead 3 块场景回归）。

### 验证

- 全量 `pytest` **309 passed / 0 failed**；`tsc --noEmit` 通过；Vite 生产构建通过；隔离真实浏览器 **92/92 PASS、0 脚本错误**。
- **自包含 Tauri 产物已构建（未安装）**：重建 `scripts/build_sidecar.py`（manifest revision `cfe0e3d46a4d7a36dea4` / sha256 `7194c984…` / size 42845232），`tauri build` 产出 `src-tauri/target/release/bundle/macos/Literature.app`（54.47 MiB），bundle 内 sidecar sha256/size 与 manifest 一致；adhoc 重新签名后 `codesign --verify --deep --strict` 通过。**未替换 `launcher/Literature.app`、未启动/未安装**。
- 未调真实 MinerU/provider、未读真实 Keychain、未改真实库、未提交 Git。
- 第十轮条目中“R02 已关闭”的描述以本轮为准；后续进展（R09 在途预算/协调层、R11 轻量状态、R12 质量告警与结构确认）见第十一轮条目；R11 块窗口化/ETag、R12 结构缓存/定向译审仍未实施，不能宣称整体优化完成。

## 2026-10-13（第九轮）：移除导出/TBL、MinerU 本地后端口径与重复费用/读写膨胀修复（Orca Worker 实施，已编码/自动测试/tsc/临时 Vite 构建，未安装/未真实服务验收）

### 背景与范围

用户授权“完成项目的部分重构和整体优化，去除导出与外部 TBL 模块，并为以后增加本地处理的 MinerU 模块留下口径”，并说明按“任务/恢复/重复读写优化、移除导出/TBL、预留本地 MinerU 接入契约”拆分实施，不推倒重写。本轮保留 Tauri macOS + 内置 sidecar、不可变 raw、canonical 文档图谱、稳定 block/cell ID、人工锁定与可撤销修订。

### 1. 正确性与重复费用（评审 R01/R02/R07/R10 类）

- **翻译/结构启动原子化**：`literature/api.py` 新增项目级 `_project_start_lock`；“检查活动任务 + 创建/恢复作业”抽为 `_claim_translation_job`，`start_structure` 抽为 `_claim_structure_job` 并新增“同项目仅一个活动结构 AI 作业”守卫。并发新建/resume/双 resume/双击只会领取一次。
- **崩溃恢复不重复翻译（含 Lead 第二轮复现的 force 边界）**：`literature/translation.py` 在发出首个模型请求前写 checkpoint（冻结 `target_ids`、`pass`、源文摘要、非敏感模型配置、术语表摘要 `frozen`）。`TranslationEntry` 新增任务级 `run_token`：磁盘上带本次 `run_token` 的结果汇总 checkpoint 里本次写入的完成清单，**只把“本次任务真正处理过”的块当成本次完成**；旧译文即使 source_hash/status 有效也不算。修复 Lead 复现的“6 块均有旧译文 + force + 第 6 块绕过收尾中断”场景：旧实现预写 checkpoint 把 6 块旧译文全算完成，恢复 0 请求假 completed；现在只重发第 6 块（resume_calls=1）。覆盖旧译 force、旧 refined 再 refine、polish 中途崩溃、scope=untranslated 目标不缩水；`frozen` 只在首次写入。
- **R08 超长单字段预拆**：`_unit_segments` 把超过 1200 字符的单字段沿句子/空白边界拆成 `key~i` 子段，`_assign_fields` 按序拼回同一稳定字段（不改 block/cell 身份）；失败重试时 `_split_units` 现在可继续缩批。新增回归：单字段被拆成多子段且拼接无 `~` 残留；若模型始终截断仍失败且不保存半句。
- **平面版本历史**：`revisions`/`candidates` 由 `asdict(previous)` 改为 `_entry_snapshot`（内容 + 依据元数据，不嵌 `revisions`/`candidates`/`selection_translations`），消除 translation.json 递归膨胀；旧文件里已存在的嵌套历史仍可读、可审阅。
- **项目路径索引**：`literature/storage.py` 新增可重建的 `project_id→path` 索引，`list_projects` 顺带刷新，`create_project`/`rename_project`/`delete_project` 增删；缓存未命中或目录失效时重建，仍以磁盘为准。

### 2. 整体优化（评审 §4 阶段 3–5 部分项）

- **全局熔断覆盖“有成功、后面持续失败”**：`_process_blocks` 连续 3 个分块失败即停止提交新分块（不再要求 `success_blocks==0`）；`_finalize_job` 只有“零成功分块”才整体 `failed`，已有成功分块保留并按 `partial_failed` 呈现。
- **provider 生命周期**：翻译/结构作业 `finally` 显式 `provider.close()`，并清理 `pause_events` 句柄。
- **前端轮询降频**：`frontend/src/App.tsx` 只在真正 queued/running 或有新任务签名时拉整层译文，暂停/闲置不再反复拉；轮询间隔闲置降到 5s、繁忙保持 1.5s。
- **表格占用网格**：`literature/mineru.py` 的 HTML 表格解析维护 `(行,列)` 占用网格，rowspan 后的下一行单元格不再错位到第 0 列；仅影响新解析，旧 cell ID/译文不变。

### 3. 移除导出与外部 TBL

- 后端：删除 `literature/exporters.py`、`literature/tbl.py`、CLI `export` 命令、`_describe_project` 的 `export_count`、项目 `exports/` 目录创建、`python-docx` 依赖与 `scripts/build_sidecar.py` 的 `--collect-all docx`；移除 `exporters`/`tbl` 的 import 与 JobManager 中的 `TBLBridge`。`/projects/{id}/exports`、`/export/readiness`、`/tbl/prepare|start|config|status` 保留路径但返回 **410**，旧客户端/旧书签得到明确提示。`import_mineru` 的 `extra_formats`（MinerU 返回的原始证据）保留。
- 前端：删除 `frontend/src/workbench/ExportPanel.tsx`；移除导出导航标签、`#/export` 视图、资源卡片导出阶段/筛选/菜单操作与工作流行、`ReadingWorkspace` 的“导出…”按钮、设置中的 TBL 段、`export_readiness_v1` 能力校验。旧 `#/export` 与 `#/p/<id>/export` 统一回退到“阅读与翻译”，不白屏。
- **数据保留**：已存在的用户 `exports/` 文件、`source/origin.*`、`ocr/raw/mineru`、`document/document.json`、`translations/` 均保留；`ProjectStore.create_project` 不再新建 `exports/`，但不会删除旧目录，也不运行任何导出相关清理。旧项目里若已有 `exports/`，仅 `export_count` 不再下发。

### 4. 本地 MinerU 扩展口径（只预留，不伪装可用）

- 新增 `literature/parse_backends.py`：定义 `mineru_remote`（已实现、默认，切片/上传 URL/轮询/不可真正取消/可断点续跑）与 `mineru_local`（预留，`available=false`，上传 URL/轮询为否，带明确原因）。
- `literature/mineru_workflow.py`：`WorkflowJob` 新增 `backend_id`/`backend_config`，创建任务时校验后端可用性并落盘参数快照；`from_dict` 对旧任务缺字段默认 `mineru_remote`。`literature/ocr.py` 创建任务时固定 remote 并记录后端参数快照，`describe` 下发 `backend_id`/`backend_config`。
- API：新增 `GET /parse/backends` 只读能力清单；`/health` 新增 `parse_backends_v1`。
- 明确未做：本版本**不安装本地 MinerU 模型/依赖**，local 不显示为可点击功能，未来作为独立进程/服务通过同一 `prepare/execute/status/result` 边界接入，再走同一 normalize/merge。文档与架构说明见 `docs/ARCHITECTURE.md`、`PROJECT_PLAN.md` §2.2/§6。

### 5. 验证与未验收边界

- 全量 `pytest` **298 passed**（原 283 项移除导出测试并新增 21 项：并发启动/新建+resume/双 resume/结构原子领取/exec.submit 失败可恢复、崩溃恢复不重发、force 旧译不假完成、旧 refined 再 refine、polish 崩溃、冻结清单、平面历史体积、项目索引缓存/重建、持续失败熔断、rowspan/colspan 占用网格、长字段预拆与拼接、解析后端可用性与旧任务默认、旧 exports 文件不被触动）；`tsc --noEmit` 通过；Vite 生产构建到 `/tmp/literature-opt-build` 与 `frontend/dist`（主 JS 804.28 kB / gzip 242.52 kB；Node 22.11.0 版本告警如实记录，未安装依赖掩盖）。
- **隔离真实浏览器验收**：`tests/manual/parse_ui_browser_check.py`（假 MinerU、不读 Keychain、不发外部请求，真实点击生产构建前端）**92/92 PASS、0 脚本错误**，覆盖首页/资源/校对/阅读、开始/继续/恢复解析、旧 `#/p/<id>/export` 与 `#/export` 回退不白屏、主导航不再暴露导出、`/tbl/config` 与 `/projects/{id}/exports` 返回 410；仍断言真实 PDF canvas 已绘制内容。
- **隔离 sidecar 构建**：用与 `scripts/build_sidecar.py` 相同的参数（去掉 `--collect-all docx`）构建到 `/tmp/lit-sidecar/dist` 成功（约 42.8 MiB，小于旧包，符合移除 python-docx）；启动后 `/health` 能力包含 `parse_backends_v1`、不再包含 `export_readiness_v1`。**未安装/未替换已安装客户端**。
- 测试使用临时库与 mock，未读真实 Keychain、未调用真实 MinerU/provider。
- **未做**：未安装/未替换/未启动真实客户端（未 `tauri build`、未安装 `.app`），未做原生窗口点击与公证验收，未做真实 MinerU/provider 质量/速度/费用验收，未提交 Git，未改真实文献库。隔离浏览器与临时 sidecar 构建不能替代真实服务与原生验收。
- **兼容/迁移限制**：已移除的导出接口按 410 废止（不是 404，便于旧前端定位）；旧项目 `exports/` 目录与文件保留；表格占用网格修复只对**新解析**生效，旧 canonical cell 坐标不改、不迁移 cell ID；`backend_id` 缺省按 remote。

### 6. 尚未实施（评审 §4 阶段 2/4/5）

分级预算预留与在途/重试费用、预估复用实际请求组装、结构 AI 增量候选与缓存、结构 done 口径（版本/范围/实际审查 + 明确确认）、分块按语言/章节约束、本地质量检查告警、全块列表窗口化与状态/审计分离接口。

## 2026-10-13：全项目运行逻辑与效率/token/质量评审（未修改业务代码）

- 交付 `docs/PROJECT_REVIEW_2026-10-13.md`：按客户端、导入/资源、MinerU、规范化/存储、原页/人工结构、结构 AI、翻译、provider/账本、导出/TBL 介绍运行逻辑与实现路径，给出优先级、证据和优化验收顺序。依据当前工作区，保留已有修改，不将 2026-10-12 评审已修问题重复报为未修。
- 本轮全量 Python **274 项通过**：临时 library/runtime，屏蔽真实 Keychain 命令与 httpx 非本地网络。TypeScript `--noEmit` 通过；Vite 构建到 `/private/tmp/literature-review-build-20261013` 通过（主 JS 810.83 kB/gzip 244.57 kB）。本机 Node 22.11.0 仍有 Vite 要求 20.19+/22.12+ 的版本告警，构建成功不等于兼容风险消失。
- 临时样例复现：并发 `start_translation` 创建 2 job/提交 2 worker；6 分块 force 作业在第 6 请求模拟绕过正常收尾，已保存 5 条但无 checkpoint，同 job 恢复发送 6 请求、重复 5 块（崩溃边界模拟，不是实际 kill 验收）；一个短段人工编辑 1/5/10 次 translation.json 为 699/11012/446645 字节；10000 字符单字段独占一块且无法再次切分。
- 导出复现：图片附注/表格标题/表格附注在 Markdown/HTML 缺失（EPUB 共用 HTML 正文）；partial 50/359 页当前可译块全译后 readiness 仍 `ready=true`；zh-CN 与 ja MD+JSON 导出目录相同，后者替换前者的 translation.json；DOCX colspan=2 未生成 gridSpan；rowspan=2 下次行 C 被规范化到 (1,0)，应为 (1,1)。raw/canonical 在真实库均未改动。
- 大库开销测量（读取次数，不是耗时）：10/50 个合成项目单次 `GET /projects` 分别读取 project.json **305/6525 次**，响应均 200。定位到 project_path 扫描与逐项目阶段聚合叠加；同时确认多路定时轮询、暂停拉整层译文、全块渲染、预估未覆盖实际 prompt/polish/force、在途预算未预留等代码缺口。
- **上述业务缺陷尚未修复**。本轮只新增评审并同步计划/日志；未提交 Git、未打包安装/启动客户端、未改真实文献库、未调用真实 MinerU/provider、未执行本轮原生/浏览器点击验收。历史已有验收不扩大到这些新边界。

## 2026-10-13（第八轮）：解析任务历史、首页分区与自动归档（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器 87/87/冻结 API 11/11/已安装）

### 背景与设计

用户要求解释首页只显示最近 3 条的规则，并参照主流模式制定列表/历史/保留与自动清理方案。产品参考（不是行业统一标准）：Safari 下载列表把已完成从当前列表移出、GitHub Actions 运行历史按保留期归档；**90 天 / 1000 条是本产品自选值**。完整契约见 `docs/PARSING_TASK_HISTORY_DESIGN.md`。

### 生产修复

- `literature/mineru_workflow.py`：`WorkflowJob` 新增 `finished_at`，只在**首次**进入 `completed` 时写入，后续保存/清理/快照不覆盖；旧任务缺失时展示回退 `updated_at` 并标注 `finished_at_estimated`（绝不用 `created_at`）。
- `literature/task_history.py`（新增）：`JobArchiveStore` 独立持久化 `{runtime}/ocr-jobs/archive.json`（归档记录 + `settings` + `last_auto_run_at`，原子写、串行锁，**不写 job.json**）；纯函数分区过滤/排序/分页与 `select_auto_archive`。
- `literature/ocr.py`：`list`/`get` 附加归档元数据；`task_center`（scope=active/recent_completed/history）；`archive_job`/`unarchive_job`（仅 completed/failed，活动状态 400，幂等）；`auto_archive`（保留期+条数上限，最老优先）与完成后 `capacity_only` 检查；启动一次 + 调度器每日最多一次（持久化 `last_auto_run_at`，重启幂等）；重试自动清除归档标记。
- `literature/api.py`：新增 `GET /ocr/tasks`、`GET /ocr/jobs/{id}`、`POST /ocr/jobs/{id}/archive`、`/unarchive`、`GET/PUT /ocr/archive-settings`；`/health` 新增能力 `task_history_v1`。**保留 `/ocr/jobs` 全量契约**（含已归档，资源页最新 job 不丢失）。
- 前端：`types.ts`（finished_at/archived/TaskPage/ArchiveSettings）、`viewState.ts`+`HomeTopbar.tsx`（`#/history` 路由）、新增 `TaskHistory.tsx`（搜索/状态筛选/日期排序/每页 20/详情/归档找回/保留期设置）、`ImportWorkspace.tsx` 拆成「当前任务」（运行置前、queued 按真实 queue_position、失败不受 3 条限制、每页 10、精确计数与分页）与「最近完成」（24h 内最新 3 条、按 finished_at + created_at + job_id 稳定排序、无满格进度条、分段按需展开、「收起已完成」持久化隐藏当前 job_ids）、`App.tsx` 增加历史路由。

### 并发与原子性收口（Lead 复核）

- **归档/取消归档与 retry/start 原子互斥**：旧 `archive_job` 先 `get` 判状态再 `archive_one`，无 schedule/job 锁；并发 `retry` 可在判定之后先把任务变 `queued` 并清标记，归档随后给活动任务加标记。现 `archive_job`/`unarchive_job` 与 `retry_job`/`start_job` 共用锁序 `schedule_lock → job_lock` 并在锁内重查状态；`auto_archive` 也在 `schedule_lock` 内完成“快照→选择→写入”，避免与手动归档/取消/重试交错。`completed` 不能被 `start`/`retry` 变为活动状态，候选不会在写入前变成活动任务。
- **归档元数据写失败不污染内存**：`JobArchiveStore` 的 `archive_many`/`unarchive`/`update_settings`/`mark_auto_run` 改为 copy-on-write（先原子落盘成功再发布 `self._data`），写失败时内存与磁盘均保持旧值，请求会真实报错且当前会话不会错误隐藏任务，后续重试成功。

### 自动归档规则

- 仅 `completed` 参与自动归档；**未解决 failed 与所有活动/可恢复任务绝不自动归档**。
- 默认保留 90 天；常驻（未归档）成功记录上限 1000，超期或超量（最老优先）转已归档，原因 `retention`/`capacity`。设置可选 30/90/180/永久；永久取消时间限制但 1000 上限仍在。
- 归档只改展示/保留期元数据：不改 `job.state`，不删 job/bridge/raw/canonical/translation/exports；`migrate_project` 继续按完整历史证据工作。

### 回归

- 全量 `pytest`：**279 passed**（上轮 274 + 本轮新增 5 项）：新增真实线程/事件并发回归（archived 与 retry 竞争后任务必须 queued 且无归档、queue 正确）、`retry → archive` 被拒、`auto_archive` 不标记任何活动任务、`completed` 不能被 start/retry 改变、**JobArchiveStore 写失败前后内存/磁盘不变且后续重试成功**。
- `tsc --noEmit`、Vite 生产构建通过。
- 隔离真实浏览器全流程：**87/87 PASS**、0 脚本错误（上轮 84 + 本轮 3）。新增：首页同时存在当前任务与最近完成两区、最近完成无满格进度条、窄屏首页无横向裁切。**首页任务区完整截图** `docs/verification/home-sections.png`（full page，含 10 条失败/分页「第 1 / 2 页 · 共 12 条」与 3 条最近完成、无进度条；已人工检视）、`home-sections-narrow.png`、`history-page.png`、`history-narrow.png`。旧有部分→全文、源页 5/7 定位+canvas 像素、失败重试、中断恢复、双击防重复、旧资源检查、队列调序、历史搜索/筛选/分页/归档找回/保留期/收起持久化均保持通过。
- 冻结 sidecar 隔离 API：**11/11 PASS**（新增 `task_history_v1` 能力、默认保留期 90/1000、任务中心可用）。

### 构建与安装

- 新 sidecar：revision `0e308e432be3a38ee358`，sha256 `b0f85f4af1a4f1a38d883db360cfba63e52c419e63e92391fbd5c5a9f122444b`，size `47709968`。
- Tauri 产物重建 `Literature.app`（59.11 MiB），adhoc `codesign --verify --deep --strict` 通过；包内 sidecar 哈希与 manifest 一致。
- 安装前只读确认：无 `launcher/Literature.app` 进程、无进程打开真实库、runtime `queue.json` 为空。已备份上一版到 `launcher/.rollback-Literature-521e540c.app`，用 `scripts/setup_macos_launcher.sh --app-only` 安全安装；安装后签名与哈希校验通过。**未启动客户端、未修改真实库**。
- 备注：真实库某项目 `jobs` 表存在 2 条较早遗留的 `running`（structure_ai/translate）行，但**没有对应进程**（`lsof` 未打开该库）；按设计下次启动会被 `recover_interrupted_jobs` 标为失败，本轮未触碰。

### 尚未验收

- 真实 MinerU / 真实 provider / 原生窗口逐项点击 / Developer ID 公证均未做；历史与归档仅在隔离 fake MinerU + 真实 Chromium 生产构建与冻结 sidecar 上验收。


## 2026-10-13（第七轮）：未解析已有资源「开始解析」入口与总状态修正（Orca Worker 实施，已编码/自动测试/tsc/build/隔离浏览器 52/52/冻结 API 8/8/已安装）

### 用户现象与只读根因

用户截图反馈一个 338 页 PDF 资源显示“未解析”，但详情里只有“查看进度”，而右上总状态却是绿色“已完成”。只读检查（未改真实库）：

- 真实项目 `fdbd7397a632432ab7496835fdde6e82`：`project.json` `state=created`、`source/origin.pdf` **338 页 / 11,536,253 字节**、`document/document.json` **不存在**、`ocr/raw/mineru/` 空；runtime `ocr-jobs` 中**没有**该 project_id 的 bridge；项目内 `jobs` 表 0 行。即：从未解析、也没有任何任务。
- `ocr.project_coverage` 对“从未生成规范文档”和“旧已解析资源无 coverage 证据”都返回 `unknown`，无法区分。
- `frontend/src/ProjectLibrary.tsx`：`canContinueParse`（要求 `has_document && partial`）与 `coverageUnknown`（要求 `has_document`）对无文档资源都为假，`Workstreams` 的兜底分支因此只渲染“查看进度”；而 `onProgress` 只是 `openHome`（切到首页），不是进度面板。
- `badge()` 缺少“无规范文档”分支，最终无条件 `return { tone: "done", label: "已完成" }`，导致“提取=未解析 / 总状态=已完成”自相矛盾。

只读复核 `project_coverage('fdbd7397…')` 在本轮修复后返回 `{state: 'unparsed', source_page_count: 338, covered_pages: 0, ranges: []}`（读取操作，未创建任务、未触达 MinerU）。

### 生产修复

- `literature/ocr.py`
  - `project_coverage`：无 `document/document.json` 时，若 `source_page_count` 已知或源 PDF 页数可读，返回 `unparsed + source_page_count`；**有规范文档但无 coverage 证据仍返回 `unknown`**（旧资源保守口径不变）。
  - 新增 `start_parse` / `_start_parse_locked`：语义独立于“继续解析”，是一个完整的单次服务端首次解析操作。在 `schedule_lock → project_lock` 锁序内依次完成 **Token 校验、无规范文档检查、活动作业检查、源文件存在且 SHA-256 与创建时一致校验、`create(enforce_single=True)` 与调度**（`schedule_lock` 为可重入锁，内层 `start_job` 再取不会死锁）。**无 Token 时不创建任何作业**；**创建成功但调度失败时任务保留为可恢复的 queued 状态并报出带 job_id 的具体错误**。`page_ranges` 经 `parse_page_ranges` 转为**本地压缩子集** `requested_pages`；非 PDF 不支持指定页码。绝不据现有页推断覆盖，也不会把 `unknown` 合并进旧文档。
  - `_continue_parse_locked`：无 `document/document.json` 时明确拒绝，提示改用“开始解析”。
- `literature/api.py`：新增 `ParseStart` 与 `POST /projects/{project_id}/ocr/start`，直接委托服务层；**不改变既有 `POST /projects/{project_id}/ocr/jobs` 的仅创建契约**。Token 校验、创建与调度均在服务层锁内。
- `frontend/src/ProjectLibrary.tsx`：
  - `badge()`：无规范文档时显示“未解析”，不再误报“已完成”。
  - `coverageText()`：无文档按是否有活动任务区分“尚未开始解析 / 解析中，完成后生成规范文档”；旧资源未知文案保留。
  - 新增 `needsFirstParse`；无文档且无活动任务时用“开始解析”主按钮替代兜底“查看进度”。
  - 新增 `StartParseDialog`：显示源文件与**总页数**、可选页码范围、模型/语言包/OCR/表格/公式参数；`startExtract` 调 `POST /projects/{id}/ocr/start`，提交期间禁用防双击。
  - 既有 `queued/waiting/interrupted/failed` 的入队/恢复/重试分支未改，开始解析仅在没有活动任务时出现。

### 回归

- 全量 `pytest`：**256 passed**（上一轮 243 + 本轮 13）。新增 `StartParseTests`（11 项）：338 页 created+无 job 报告 `unparsed/338`；legacy 有文档无 coverage 仍 `unknown`；开始解析创建全量首个作业、入队且不产生文档；**338 页源经 `_prepare` 自动切成 200+138 两片且累计页 1–338 无重叠无遗漏**；**并发两个 `start_parse` 只建一个作业**；**Token 缺失零创建且不留下队列条目**；**调度失败保留可恢复的 queued 任务并报带 job_id 的具体错误**；页码范围 `3-5,8 → [3,4,5,8]`；已有文档 / 源文件缺失 / 源文件被改（hash 不符）均拒绝且不留下任务；`continue_parse` 对无文档资源拒绝。新增 API 2 项：无 Token 时 `POST /ocr/start` 返回 400 且 `/ocr/jobs`、队列为空；有 Token 时创建 1 个 job、立即二次提交冲突且仍只有 1 个。
- `tsc --noEmit`、Vite 生产构建通过。
- 隔离真实浏览器全流程：**52/52 PASS**、0 脚本错误。新增：“未解析资源提供开始解析入口 / 不再提供查看进度 / 不显示已完成 / 总状态为未解析 / 弹窗显示共 260 页与页码范围 / **开始解析 260 页自动切成 200+60 两片** / fake 完成并生成规范文档 260/260 / 只创建一个任务 / 刷新后显示全部解析 260 页”。旧有部分→全文、源页 5/7 定位与 canvas 像素、失败重试（fake 改为“切片源页集合精确匹配”触发一次性失败）、中断恢复、双击防重复、旧资源检查、队列调序均保持通过。
- 冻结 sidecar 隔离 API：**8/8 PASS**（新增“未解析资源报告 unparsed + 页数”）。

### 构建与安装

- 新 sidecar：revision `d632fe3c3f105812d64a`，sha256 `e5f88b9410ae1df828fde3518006b4b8bb52479feeb2bcde10bb7e9680d39c60`，size `47694096`。
- Tauri 产物重建 `Literature.app`（59.10 MiB），adhoc `codesign --verify --deep --strict` 通过；包内 sidecar 哈希 `e5f88b94…` 与 manifest 一致。
- 安装前只读确认：无 `launcher/Literature.app` 进程；真实 runtime `ocr-jobs/queue.json` 为空、10 个 job 全部为 `completed`/`failed`（无 queued/waiting/interrupted/active）；真实库各项目 `jobs` 表无 `queued/running`。已备份上一版安装包到 `launcher/.rollback-Literature-058f5a85.app`（sha256 `9a417378…`，更早的正式版仍在 `.rollback-Literature-5010db9b.app`），用 `scripts/setup_macos_launcher.sh --app-only` 安全安装；安装后签名校验通过、内置 sidecar 哈希与 manifest 一致。**未启动客户端、未修改真实库**。

### 尚未验收

- 真实 MinerU 解析（用户真实 338 页资源需在安装版点击“开始解析”后自行发起并计费）、真实 provider、原生窗口逐项点击、Developer ID/公证均未做。本轮“开始解析”仅在隔离 fake MinerU + 真实 Chromium 生产构建与冻结 sidecar 上验收；真实库 `fdbd7397…` 的覆盖仍为 unknown → 现仅显示为 `unparsed`，未写入任何 coverage 或文档。


## 2026-10-13（第六轮）：旧 page_ranges=1-N 可信前缀迁移 + 部分翻译口径（Orca Worker 实施）

### 用户现象与只读根因

用户截图反馈“The White Lotus Teachings”显示“覆盖未知”且“检查解析范围”失败。只读检查（未改真实库）：

- 项目 `bdb72fda156045379ab8181f32cc16f3`；`source/origin.pdf` 实隖359 页；`manifest.state=ready`、`page_count=50`、无 `parse_coverage`。
- canonical 50 页 / 242 blocks；`source_files` 单片 `content_list_v2` sha256 `10a620cf…`。
- 历史 job `99335abf9b8042beb68a4c0127028580`：`completed`、`page_ranges='1-50'`、`page_count=50`、单 slice `start=1/end=50 downloaded`，无 `source_pages/source_page_count`。
- raw `slice-001`：`content_list_v2` 长度 50（45 非空）、`layout.json` 的 `pdf_info` 长度 50、`*_origin.pdf` 50 页；源/任务二方 hash 均为 `1fc85b93…`。
- 旧 `migrate_project` 遇到 `page_ranges.strip()` 直接跳过 → `no_trusted_evidence`，与实际存在 completed 任务矛盾。

### 修复规则（严格、可审计）

`migrate_project` 新增可信连续前缀 `1-N` 路径，必须同时满足，否则保守 `unknown` 并给出具体原因码：

- 源文件 hash 与创建时一致；`verify_raw` 无错。
- `job.page_count`、`slice.start=1`、`slice.end=N`、`len(document.pages)`、`source_files[0].page_count` 均为 N；单片片且与 `source_files` 数量一致。
- 封存切片可核对页数证据：`content_list_v2` 长度 = N；`layout.json` 的 `pdf_info` 长度 = N；`*_origin.pdf` 存在时页数 = N。
- 前缀映射：局部第 i 页 = 源页 i+1；不依赖文字或文件名猜页序。
- 非前缀（如 `2-5`）、非连续、远端原始页号不明确的旧作业仍保守 unknown。

迁移沿用原有原子应用：同时修正 `page.page_index/source_page_index` 与 `block.page_index`；**block/cell ID、人工结构修订、译文与锁定、raw 均不变**。

### UI

- 翻译阶段在覆盖为 `partial` 时显示“已译已解析内容”，描述注明“仅统计已解析的 N/M 页；剩余未解析内容尚未翻译”；总状态仍优先“部分解析 N/M”，避免把当前可译块的全译误认为全书完成。
- “检查解析范围”失败文案改为按具体原因码（如 `raw_verification_failed`、`source_changed`、`prefix_layout_page_mismatch`、`completed_but_unverifiable`）说明缺失的证据，不再笼统“没有已完成任务”。

### 回归

- 全量 `pytest`：**243 passed**（新增 6 项 `LegacyPrefixMigrationTests`）：359 页源 + 50 页前缀（含 5 空白页）迁移为 `partial 50/359` 且保留旧 ID/结构修订/人工锁定译文；缺/篡改证据失败（删 layout → `raw_verification_failed`；layout 页数不符 → `prefix_layout_page_mismatch`；源变 → `source_changed`）；非前缀 `2-5` → `page_ranges_not_trusted_prefix`；迁移后 `continue` selected 51..359 自动切分 **200+109**。
- 隔离真实浏览器全流程：**44/44 PASS**（新增“旧 1-10 前缀迁移 10/30 → 继续解析 → fake 完成 30/30”），0 脚本错误。
- `tsc --noEmit`、Vite 生产构建通过。

### 构建与安装

- 新 sidecar：revision `5010db9b018a80003d21`，sha256 `1ce0ece4b81b52d7170ebf8434573e30f5f2c2adc3af76154c24b02d52846b6d`，size `47692064`。
- Tauri 产物重建 `Literature.app`（59.10 MiB），adhoc 签名验证通过；包内 sidecar 哈希与 manifest 一致。冻结隔离 API **7/7 PASS**（含新前缀迁移 `mode=legacy_prefix`、部分 3/6）。
- 安装前无客户端进程、真实库活动 OCR 任务 0；已备份上一版到 `launcher/.rollback-Literature-546812b8.app`，用 `scripts/setup_macos_launcher.sh --app-only` 安全安装；安装后 `codesign --verify --deep --strict` 通过、内置 sidecar 哈希与 manifest 一致。**未修改真实书 coverage**：用户在新版本点击“检查解析范围”后自行迁移。

### 尚未验收

- 真实 MinerU / 真实 provider / 原生窗口逐页点击 / Developer ID 公证；真实书的迁移结果需用户在安装版中点击后确认。


## 2026-10-13（第五轮）：客户端安装与原生预览验收（Orca Worker 实施）

### 安装前只读检查

- 无 `launcher/Literature.app` 进程。
- 真实库 7 个项目：`created` 2、`ready` 5；runtime 8 个 OCR job：`completed` 6、`failed` 2；**活动 OCR 任务 = 0**（无 queued/waiting/interrupted/active），因此启动不会自动恢复外部调用。
- 旧安装包 sidecar sha256 `015e5d83…`（与上一版一致）；已备份到 `launcher/.rollback-Literature.app/`（安装脚本会在退出时删除自己的 previous.app，故单独备份）。

### 安装

- 用 `./scripts/setup_macos_launcher.sh --app-only`，不动桌面替身。
- 安装后：`codesign --verify --deep --strict launcher/Literature.app` 通过；内置 sidecar sha256 `dd63b2cb37cdc9f39f48117121b079da472b9b164883f385d38449c56e195c88`，size `47690256`，与 `src-tauri/binaries/manifest.json` 一致（revision `546812b8e04ec5d6d433`）。

### 隔离原生验收（不碰真实库、不读 Keychain、不发外部请求）

- 启动环境：`LITERATURE_LIBRARY=/tmp/lit-native/library`、`LITERATURE_RUNTIME=/tmp/lit-native/runtime`、`MINERU_API_TOKEN=fake-native-token`、`HTTPS_PROXY=http://127.0.0.1:9`（本地黑洞，阻断外部）。首页显示“MinerU 连接检查失败”，证明未触达真实服务。
- 真实 WKWebView 窗口截图 `docs/verification/native-review.png`：结构校对页的**源文件预览实际渲染出 “PAGE 1”**、bbox 覆盖层、页码 `1 / 9`、原页缩放与结构框；右侧结构编辑列出 p1–p4。这证实上一轮 pdfjs `getOrInsertComputed` 修复在原生 WKWebView 中生效。
- 首页/文件资源原生渲染正常：资源卡片显示“部分解析 4/9 页”“覆盖未知”“待校对”等；MinerU 状态如实显示。

### 限制与未验收

- 原生窗口的 AX/坐标点击在顶栏导航上不稳定（`STALE_SNAPSHOT`/点击未生效），因此**原生“阅读与翻译”页点击未完成**；该页已在真实 Chromium + 生产构建下强断言验证（`docs/verification/reading-narrow.png`）。不能把浏览器通过当作原生通过。
- 仅 adhoc 签名，**未做 Developer ID/公证**；未替换桌面替身；未用真实 MinerU/模型；未修改真实库。
- 如需完整原生逐页点击，建议在允许前台交互且 AX 稳定的环境重跑，或补一个原生 E2E。


## 2026-10-13：Lead 最终独立复核

- 独立复跑全量 `pytest tests/ -q`：237 项全部通过；两个解析生命周期测试文件单独运行：41 项全部通过。
- 独立复跑 `tests/manual/parse_ui_browser_check.py`：实际输出 37 条 PASS，全部通过，0 浏览器脚本错误。包括部分解析到全文、刷新回显、源页 5/7 定位与真实画布像素、125% 缩放、失败重试、中断恢复、防重复提交、旧资源检查成功/失败、UI 调序后 fake 实际顺序 A→C→B。
- 实际查看校对与阅读窄窗截图：源 PDF 的 PAGE 1/2 可见，没有前轮的渲染失败。核验最终 Tauri 产物 `codesign --verify --deep --strict` 通过，内置 sidecar SHA-256 与 manifest 一致（`dd63b2cb…`）。
- 浏览器复跑需要沙箱外本地回环监听权限；获得执行许可后完成。全程使用临时库与假 MinerU，不调用真实解析/模型服务，不安装或替换现有客户端。真实 MinerU、原生窗口交互仍待验收。

## 2026-10-13（第四轮）：原文预览真实渲染修复（Lead 肉眼截图发现）

前一轮的浏览器验收有实质遗漏：仅断言页面容器可见/当前页约等于目标，而 `PdfPageCanvas` 的 `.catch` 将真实错误吞成“渲染失败”，所以“0 脚本错误 + 容器定位通过”仍然交付了一个看不到原文的预览。Lead 用 view_image 查看 `docs/verification/reading-narrow.png` 发现：实际是结构校对页，中间橙字“第 1 页渲染失败”，却仍有 bbox。

### 根因

- pdfjs-dist **5.7.284** 在 `OptionalContentConfig` 中调用 TC39 提案方法 `Map/WeakMap.prototype.getOrInsertComputed`；当前 Chromium 与 macOS WKWebView 尚未实现，`page.render` 直接抛 `TypeError: this[#e].getOrInsertComputed is not a function`，所有页面渲染失败。这是真实生产缺陷（WKWebView 同样缺该方法）。
- 次级缺陷：渲染失败后组件移除 `<canvas>`，且 effect 依赖不含 `failed`，无法重试；取消/尺寸变更时在新渲染前未等待上一次 render task 结束，pdfjs 会因“同一 canvas 并发渲染”再抛错。

### 生产修复

- `frontend/src/pdfAssets.ts`：在 pdfjs 使用前 polyfill `Map/WeakMap.prototype.getOrInsertComputed`；`PdfPageCanvas` 显式 `import "../pdfAssets"` 保证先执行。
- `frontend/src/review/PdfPageCanvas.tsx`：不再静默吞错（`console.error` 保留真实原因）；canvas 常驻（失败显示覆盖层而非卸载）；新渲染前 await 上一次 render task 结算再复用 canvas；取消不判失败。

### 有效验收（不再用弱断言）

- `parse_ui_flow_check.cjs` 新增 `canvasRendered`：目标页 canvas 存在、宽高 >0、`getImageData` 深色像素 > 50；缩放 125% 后重查；断言无 `.pdf-page-error`。
- `tests/manual/parse_ui_harness.py` 的测试 PDF 写入“PAGE N”文字与矩形，使像素与截图可辨。
- **实际检视截图**：`docs/verification/review-narrow.png` 源文件预览显示 PAGE 1/PAGE 2、bbox 覆盖层与右侧结构一致；`docs/verification/reading-narrow.png` 为“阅读与翻译”双语布局。
- 结果：隔离浏览器全流程 **36/36 PASS**（含 canvas 像素、缩放、双页定位、双页校对、阅读页）；0 脚本错误；全量 `pytest` **237 passed**；`tsc --noEmit`、Vite 生产构建通过。

### 重新构建（未安装）

- 重新 `tauri build --config src-tauri/tauri.ci.conf.json` → `Literature.app`（59.09 MiB），adhoc `codesign --verify --deep --strict` 通过；polyfill 已进入 `frontend/dist` 与包内前端产物。
- 后端源码未变，sidecar 不重建：revision `546812b8e04ec5d6d433`、sha256 `dd63b2cb37cdc9f39f48117121b079da472b9b164883f385d38449c56e195c88`；包内 sidecar 哈希一致。

### 尚未验收

- 真实 MinerU、真实 provider 译文、原生窗口实际点击与公证未验收；本轮修复只证明生产组件能正确渲染真实 PDF（已在真实 Chromium + 可辨内容 PDF 上验证）。


## 2026-10-13（第三轮）：Lead 复跑缺口收口 + 强断言浏览器全流程（Orca Worker 实施）

Lead 第二轮复跑发现 33pass/1fail 与多处实际不可用/弱断言。本轮逐项收口，仍保持隔离 + fake、不 Keychain/外网/真实库/安装/git。

### 缺陷与修复

1. **测试契约与凭据隔离（实证失败）**：`/ocr/jobs/{id}/start`、`/retry` 改为**先校验任务存在（不存在统一 404）再检查 Token**，消除“无 Token 时 400 掩盖 404”；`ApiLifecycleTests.setUp` 显式 patch `api/ocr.get_mineru_token`，两个测试文件可独立跑而不依赖全量测试副作用或 Keychain。
2. **调度临界区（P0）**：新增 `_schedule_lock` 统一保护“选择候选→领取→从队列移除”与 enqueue/reorder/retry 状态修改；调度器锁外初选、锁内重选并领取，领取前重查队首/停止标志，领取后从等待队列移除（等待序号真实）；`retry_job` 对已排队作业去重。锁序固定 schedule→job/project。回归：阻塞初选后调序 → 领取重查 → fake 执行顺序符合调序；shutdown 到 claim 边界不领取/不运行；并发 retry 只入队一次。
3. **迁移证据校验（P0）**：`migrate_project` 现在必须同时满足：源文件 hash 与创建时一致；canonical `source_files` 与不可变 raw manifest 一致且 `verify_raw` 无错；存在已完成、无 `page_ranges` 的旧作业且切片按顺序与 `source_files` 页数一一对应。迁移同时修正 page/block 原页索引，修复旧 normalize 每片局部页号重复导致 >200 页书页号重复。回归：401 页旧 normalize 文档迁移为 0..400 唯一且 full；切片数不符/源变不得迁移。
4. **迁移入口（实际不可用）**：资源详情为未知覆盖资源新增“检查解析范围”按钮，接 `POST /projects/{id}/parse/migrate`，成功刷新后可继续，失败显示真实原因（如源文件已修改/原始证据校验失败）。删除了 `continue` 错误中“重新解析全部页建立可信覆盖”的虚假承诺（当时无对应按钮/API 且未知合并仍保持 unknown）。
5. **封存 reconcile 事务**：只要 reconcile 改变了切片集合（含“manifest 有记录但目录缺失”）就回写 manifest，修复“全部复用时 verify_raw 永久失败”；保留已记录哈希，不把新算哈希冒充原证据。回归：缺失记录被剔除且 verify_raw 通过；被篡改证书文件仍报哈希变化且记录不变。
6. **强断言浏览器全流程**：新增 fake 确认完成后真实跑完 create/upload/poll/download/merge；点击校对块后断言目标源页真的落在预览可视区（`data-page-index`）且 `data-current-page≈N`；刷新后资源页显示“全部解析 9 页”；双击继续解析只建 1 个作业；未知资源迁移成功与失败反馈；队列 UI 置顶后 `fake` 实际执行顺序为 A→C→B；窄窗口阅读截图。

### 验证证据

- 全量 `pytest`：**237 passed**；两个测试文件单独跑 `test_parsing_lifecycle.py test_parsing_review_fixes.py` 共 **41 passed**（无 Keychain 依赖）。
- `tsc --noEmit` 通过；Vite 生产构建通过。
- 隔离真实浏览器全流程：**29/29 PASS**（含上述强断言）；0 脚本错误；截图 `docs/verification/reading-narrow.png`、`docs/verification/resources-narrow.png`。

### 构建产物（已准备，未安装/未替换运行中的客户端）

- 冻结 sidecar：`.venv/bin/python scripts/build_sidecar.py` → `src-tauri/binaries/literature-sidecar-aarch64-apple-darwin`，revision **`546812b8e04ec5d6d433`**，sha256 **`dd63b2cb37cdc9f39f48117121b079da472b9b164883f385d38449c56e195c88`**，size **47690256**（与 `manifest.json` 一致）。
- Tauri 产物：`frontend/node_modules/.bin/tauri build --config src-tauri/tauri.ci.conf.json` → `src-tauri/target/release/bundle/macos/Literature.app`（**59.09 MiB**）；`codesign --force --deep --sign -` + `codesign --verify --deep --strict` 通过；包内 `Contents/MacOS/literature-sidecar` sha256 与 manifest 一致。
- 冻结隔离 API 验收：`tests/manual/frozen_sidecar_check.py` **6/6 PASS**（`/health` revision 与 manifest 一致、能力含 `parse_lifecycle_v1`、`/projects` 可用、冻结进程按证据迁移旧资源为 3/4），用 `MINERU_API_TOKEN` 假值避免 Keychain、不发外部请求。
- 新增 `src-tauri/tauri.ci.conf.json`（跳过 `pnpm` beforeBuildCommand，供本机无 pnpm 时构建）。**未安装 / 未替换** `launcher/Literature.app`，也未触碰运行中的客户端。

### 尚未验收 / 限制

- 真实 MinerU、真实 provider 译文、原生客户端点击与公证未验收；浏览器外部解析服务仍为 fake。
- 冻结 sidecar/Tauri 产物仅为源码收口后的构建证据，不等于原生窗口交互与公证完成。


## 2026-10-13（第二轮）：Lead 复查缺陷修复 + 全流程真实浏览器验收（Orca Worker 实施）

Lead 独立复查发现上一轮存在严重边界，第一轮不能验收。本轮在同一 A–D 范围内修复 8 类缺陷，并把浏览器验收从“只到入队”扩展为**真实跑完整条流水线**。仍保留全部安全边界：隔离库 + fake MinerU、不读 Keychain、不发外部付费请求、不碰真实 library、不安装、不提交 git。

### 缺陷与修复

1. **源页定位错（P0）**：上一轮把 `page_index` 压缩成 canonical 顺序，导致第 5/7 页被当成第 1/2 页（`ReviewWorkspace` 按 `page.page_index` 建原 PDF metadata、`PdfPane` 覆盖层/跳页按 `block.page_index`）。现改为 **`page_index` 恒等于源文件页号（0-based，稀疏）**，`document.page_count` 为源总页数；`_parse_slice` 与 `merge_imported_project` 不再压缩索引，旧文档按页的 `source_page_index` 还原；`enrich_furniture_evidence` 用切片的 `source_pages` 映射证据页。回归：`make_pdf(9)+[5,7]` 得 `block.page_index=[4,4,6,6]`、`metadata=(4,4)/(6,6)`；补前 4 页后仍为 4/6。
2. **并发/原子性（P0）**：`continue_parse` 改为项目锁内“检查活动作业 + 创建”，`start/retry/reorder` 改为 job 锁内原子操作；`/start` 只允许 `queued/waiting/interrupted`（不存在 404、运行中/非法 409），`/queue` 只允许等待中任务。回归：Barrier/ThreadPool 并发 continue 得 1 成功 1 冲突；completed 任务 start/reorder 拒绝。
3. **封存中断不可恢复（P0）**：`seal_raw_mineru` 改为“复制并算哈希 → **先写 manifest** → 再移动目录”的事务；失败重试不重复复制；`_reconcile_raw_slices` 丢掉目录缺失记录、收养孤儿切片；重复 source 去重。回归：注入 `atomic_json` OSError 后重试成功、无阻塞孤儿；重复 source 只封存一次；孤儿被收养。
4. **旧任务/旧资源迁移（P1）**：`_finish_import` 对旧切片按“无 page_ranges 的连续 start/end”恢复 `source_pages`，`_source_page_count` 从源 PDF 推总页数；新增 `MinerUService.migrate_project` 与 `POST /projects/{id}/parse/migrate`，只在可信历史作业上恢复 coverage 与原页映射；不可信的 `page_ranges` 历史作业保持 unknown，错误文案给出明确下一步。
5. **document 写锁（P1）**：新增 `ProjectStore.document_lock`，追加合并与 `apply_patch/apply_patch_batch/revert_patch` 共用；合并后回调整理翻译摘要（新页计入总数、老条目保留）。回归：合并与结构补丁并发时两者都保留。
6. **完整性校验（P1）**：`_parse_slice` 在提供 `source_pages` 时校验结果页数/页码；v2 页数不符、legacy `page_idx` 越界/非法、markdown 多页均报错中止；空白页（v2 空列表）仍计为已覆盖。
7. **队列/恢复健壮性（P1）**：`resume_all` 跳过无 bridge/无 job.json 的孤儿；`running` 任务的 `queue_position` 置空并单列 `running`；`shutdown` join 调度线程、停止后不再领取新任务；调度器领取前重查停止标志。
8. **UI 一致性（P1）**：`extractStage` 在续跑/失败时同时显示“本次状态 + 已合并 N/M 页”；提取进行中时“继续解析”禁用并给出原因；首页只展示最近 3 个已结束任务，不无限堆历史。

### 自动测试

- 新增 `tests/test_parsing_review_fixes.py`（17 项）：源页定位契约、并发 continue、start/reorder 状态限制、封存恢复/去重/收养、旧任务推导、迁移可/不可信、document 写锁并发、完整性校验与空白页、resume 跳过孤儿、调序驱动真实执行顺序、队列重启保持。
- 全量 `pytest`：**230 passed**；`tsc --noEmit` 通过；Vite 生产构建通过。

### 隔离真实浏览器全流程（已完成，15/15 PASS）

`tests/manual/parse_ui_browser_check.py` 使用 `tests/manual/fake_mineru.py`（能真正跑完 create/upload/poll/download/merge 的假客户端）在隔离库上起本地 API + 生产构建前端，用本机 ms-playwright-go 随包 Playwright 真实点击：

- 部分解析 4/9 → 详情“继续解析”弹窗 → 提交 → 等到全文 9/9；
- 第 5/7 页定位：API 得 `block.page_index=4/6`，校对页 DOM 显示 `p5/p7`；
- 注入切片失败 → 首页/详情如实显示失败 → 点击“重试失败分段” → 完成全文；
- 中断任务 → 首页“恢复解析” → 完成；
- 窄窗口截图 `docs/verification/resources-narrow.png`；浏览器 **0 脚本错误**。

### 修正前轮过强陈述

- 前轮日志说“启动/重试的 400/409 原子守卫”与“全部完成”过强：实际当时只是先检查后操作、无锁；本轮才引入项目/job/document/raw 四类锁与**指令**级事务，且已实测并发冲突。
- 前轮“页面索引压缩”与项目源 PDF 定位契约冲突，已回退为源页索引。

### 尚未验收

- 真实 MinerU（压缩子集 PDF 远端回传、>200 页真实切分与计费、真实表格/公式）未测；真实 provider 译文、原生客户端点击与公证未验收。
- 浏览器验收是真实浏览器 + 真实本地 API + fake 解析服务，**不等于**真实 MinerU 端到端。


## 2026-10-13：解析任务生命周期、全文覆盖与可调序队列（Orca Worker 实施，已编码/自动测试/tsc/生产构建/隔离真实浏览器点击 8/8）

按 `docs/PARSING_RESOURCE_LIFECYCLE_DESIGN.md` 落地用户需求 1–3 与 Lead 的 A–D。本轮未提交 Git、未调用真实 MinerU/付费模型、未读 Keychain 启动外部服务、未改真实 library、未替换运行中的客户端。

### 三层状态分离

- 任务生命周期：`queued → preparing → uploading → processing → downloading → merging → completed | failed | waiting | interrupted`。新增 `interrupted`：上次进程退出时仍在执行的任务落为可恢复状态，保留切片进度；远端任务可能仍在，恢复时优先沿用已上传切片。
- 全文覆盖（canonical 已合并源页）：`unparsed | partial | full | unknown`，只统计已成功合并进 `document/document.json` 的源页，不把上传页/远端 done 页算作已覆盖。`project.json.parse_coverage` 与 `document.coverage` 双向一致，`/projects`、`/ocr/jobs` 直接下发。非 PDF / 旧资源为 `unknown`，不误报“全文完成”。
- 交付完整度（校对/翻译/导出）保持既有 `pipeline` 契约不变。

### 追加解析（不可变分批 raw + 只追加未覆盖源页）

- 新增 `store.append_raw_mineru`：继续 `slice-NNN` 编号、只追加 manifest、旧切片与哈希/只读位不变。
- 新增 `mineru.merge_imported_project`：只规范化本次新封存目录；新 block 使用独立命名空间 `blk-p{源页:06d}-{页内序号:04d}`，旧 `block_id`/`order`/`structure_revision`/人工校对/译文与锁定/资产引用一律不改；按 `source_page_index` 重排阅读顺序并重算 `page_index`；重叠源页被过滤，绝不覆盖已有页；只有全部成功后才写 `parse_coverage` 与 `state=ready`。
- `ocr._finish_import` 改为“首次导入 / 继续解析”共用合并路径；旧的 `import_raw_mineru`+全量 `normalize_imported_project` 只保留给手工导入入口（覆盖按 unknown 保守处理）。
- 修复 `_prepare` 的选页映射缺陷：选页改为在本地生成**只含所选页的压缩子集 PDF**（`split_selected_pages`），`SliceJob.source_pages` 记录真实源页，不再依赖远端 `page_ranges` 的页码语义；超过 `page_limit`/字节上限自动二次切分，支持任意非连续范围与 >200 页剩余，且 `create_batch` 对子集切片不再下发 `page_ranges`。`retry_failed` 重建切片时对非连续范围使用 `_write_pdf_pages`。
- 新增 `POST /projects/{id}/ocr/continue`：计算未覆盖页，支持“解析全部剩余”或指定范围，返回被过滤的 `skipped_pages`；无剩余页/页数未知时明确拒绝。

### 可调序持久化队列与原子守卫

- 新增 `literature/parse_queue.py`（`ParseQueue` + `select_next_runnable`）：`queue.json` 持久化顺序，支持置顶/上移/下移，重启保留；调度器单文档串行执行，运行任务不可抢占。
- `MinerUService` 新增调度线程：启动时把活动任务标为 `interrupted`，按队列顺序恢复；无 Token 时不轮询，保存 Token 后 `resume_all` 唤醒。
- `/ocr/jobs/{id}/start`：任务不存在返回 400 且不污染 running 集合；运行中返回 409。`/ocr/jobs/{id}/retry`：不存在 404、运行中 409，先重置失败切片再入队。`GET /ocr/queue`、`POST /ocr/jobs/{id}/queue` 提供队列视图与调序。`/health` 新增能力 `parse_lifecycle_v1`。

### 自动测试

- 新增 `tests/test_parsing_lifecycle.py`（17 项）：队列调序与持久化、`select_next_runnable`、非连续选页本地子集、>200 页切分、子集切片不下发 page_ranges、首次部分解析→追加到全量时旧 ID/译文锁定/raw 哈希不变、非连续补页按源页排序、重叠过滤、continue 过滤已覆盖页、覆盖未知时拒绝继续、封存幂等与重复合并不重复写入、旧资源无 coverage 保持 unknown、interrupted 恢复、以及隔离 API 的队列/400/404 守卫与 continue 端点。
- 复跑全量 `pytest`：213 passed；`tsc --noEmit` 通过；Vite 生产构建通过。

### 隔离真实浏览器逐项点击验收（已完成）

- `tests/manual/parse_ui_browser_check.py`：自建隔离 library/runtime/collector + 种子“部分解析 4/9 页”资源与 3 个排队任务 + 后台线程起 FastAPI（假 MinerU 客户端，不读 Keychain、不发任何外部请求）+ 复制生产构建前端并把内联 API 端口改到本次空闲端口；优先用 Playwright（Python 或本机 ms-playwright-go 随包 Node 版）真实点击，缺少时退回 headless Chromium `--dump-dom` 只读断言；`tests/manual/parse_ui_harness.py` 为手动/Cindy 浏览器验收用的常驻服务脚本。
- 实际结果（模式 playwright-node，8/8 PASS）：资源页显示“部分解析 4/9 页”；点击资源卡片打开详情弹窗并显示“继续解析”；打开继续解析弹窗；提交后创建继续解析任务且 `parse_coverage.covered_pages` 仍为 4（未覆盖已合并页）；首页显示“当前处理任务”与“置顶”；置顶后队列顺序仍包含全部任务。
- 这些均为“已编码 + 自动测试 + 隔离真实 API + 真实浏览器点击”，**不等于**真实 MinerU/真实模型的端到端验收。

### 尚未验收（必须与“已编码”区分）

- 真实 MinerU（含压缩子集 PDF 的远端页码回传、>200 页真实计费）与真实 provider 译文未验收；本轮全程使用假 MinerU 客户端与隔离库，未读 Keychain、未发外部请求。
- 原生客户端点击、Developer ID/公证未验收；未重建或替换运行中的客户端与 sidecar。
- 旧资源（无 `coverage`）按 `unknown` 保守处理，未做自动迁移推断；手工导入入口仍走全量规范化。


## 2026-10-12：恢复语义与提交终态修复 + 客户端重建安装（Orca Worker 实施）

用户在 Lead 隔离复现两个 P0/P1 边界后授权：深入测试、修复、重建并安全更新 Literature 客户端。本轮未提交 Git、未动旧 Litera、未改 canonical 稳定 ID 或 raw、未调用真实付费模型/MinerU、未改真实库译文。

### 修复后的恢复语义与提交终态

- **恢复不再漏补初译（Lead 复现 ①）**：原逻辑按 `checkpoint.pass == "polish"` 直接跳过初译阶段，导致 `polish=True` 部分失败（b1 未初译）恢复后 b1 永远 failed。现在按“真正保存下来的有效结果”决定待初译集合：`translate_pending = target 未在 checkpoint.translate_completed` 或“层里并非有效完成（含源变）”；`checkpoint.pass` 只记录阶段，不再作为跳过依据。恢复后先补初译，再对当前可润色范围（`translated/refined` 且未锁定、未 partial、未源变，且不在 `polish_completed`）补润色。
- **已完成不重复计费**：`polish_target_ids` 每次按当前真实结果重算；已在 `translate_completed`/`polish_completed` 的块不会被重新初译/润色。回归：部分失败恢复只发 2 次请求（b1 初译 + b1 润色），b2 不重复。
- **提交后以真实保存结果核对终态（Lead 复现 ②）**：新增 `_source_changed_ids`（entry 的 source_hash 与当前 canonical 源不一致，含未完成但只存候选的块），`_task_accounting` 改为接收该集合：源变的旧译不算 completed，归入 failed（原因“原文内容在任务期间已变化，本次结果只保存为候选，需复核”），`source_changed` 作为叠加计数；已保存但未通过完整校验的条目也归 failed，不再静默当作 unhandled。提交守卫创建的新失败条目也带上该 error。最终 `completed/failed/locked/unhandled` 四类互斥且和恒等于 target，job 不再伪报 completed。
- 快出口（无目标）仍走计数；target 非空但本次无块可处理时不再直接写 completed。

### 故障注入回归（新增 8 项，共 196 项 Python 测试通过）

- 阶段错位恢复：polish 部分失败后恢复补初译再润色，已完成不重复（provider.calls=2）。
- 源变终态：任务期间改 source_text，job=partial_failed，`source_changed=1`、`completed=0`、`failed=1`、四类之和=target；源稳定后恢复重译成功 → completed。
- 截断父批 + 截断左子批：不得因标签闭合变成伪完整（b1 failed 且 text 为空）。
- 15 字段表格首轮漏一格 + 超 12 字段上限：切分重试后 15 格全部保留。
- 超长单 text 字段被截断：单块失败且不保存半句；恢复后成功。
- 润色失败恢复：只补润色 1 次请求，不重新初译。
- 真实 API（TestClient + 隔离库 + 本地 mock provider）：任务先 partial_failed，再 resume 同 job → completed，两条均为 refined。

### 构建与安装（冻结端到端）

- 冻结 sidecar：`src-tauri/binaries/literature-sidecar-aarch64-apple-darwin`，revision `97186a2ca9645671c822`，sha256 `015e5d83c445ec3117ab69ae135886826ad3c185c728f1d2dacc61d2d680695d`，size `47650512`（manifest 与源码指纹一致）。
- 安装前用冻结后端隔离验证（`tmp/verify_frozen.py` + 本地回环 mock OpenAI 服务 + 临时库与 runtime）：`/health` revision 与 manifest 一致；能力含 `translation_engine_v2/translation_safety_v1/resource_pipeline_v1`；真实 HTTP API 任务 `partial_failed → resume → completed`，counts 正确。
- Tauri 客户端 `src-tauri/target/release/bundle/macos/Literature.app`（59.06 MiB）内置 sidecar hash 与 manifest 一致；`codesign --verify --deep --strict` 通过。
- 安装到 `launcher/Literature.app`（`setup_macos_launcher.sh --app-only`）；安装前确认无 `launcher/Literature.app` 进程、真实库无活动任务（`active_total=0`）。安装后 sha256 与签名验证通过；启动后 `/health` revision=`97186a2ca9645671c822`、能力齐全，sidecar 日志显示前端持续调用 `/projects` 等接口（前端连接正常）。

### 尚未验收

- 真实 provider（yellowbee/qwen3.8-flash-next）的译文质量/速度/费用未复测；本轮 mock 只证明协议与状态机。真实库 590 已完成译文保持，未做全书强制重译。
- 超长单字段的临时子片段预切分（按字段预算提前拆分单个 block/超长 text）未实现：单个 15 字段表格仍会先整块发送，靠失败切分（`_split_units`）补救；已用测试确认不丢字段、不伪完整。
- 按服务能力配置的思考开关/预算仍未实现（待小样本确认中转是否透传）。
- 原生窗口逐项点击、Developer ID 签名/公证、干净机器安装未验收。

## 2026-10-12：翻译失败审计与子请求隔离（Orca Worker 实施）

依据 `docs/TRANSLATION_TASK_DIAGNOSIS_2026-10-12.md` 的推荐顺序第 1–2 步实施；保留工作区既有未提交改动，未提交 Git、未打包/安装客户端、未调用真实模型或 MinerU，未修改真实文献库、canonical 或 `ocr/raw/mineru`。

### 已实现（后端）

- 部分完成语义：`_finalize_job` 在仍有失败段落时改为终态 `partial_failed`（progress=1），错误栏写入归类后的失败原因，不再冒充 completed + 空 error。
- 任务计数（收口后的最终契约）：`_task_accounting` 以 checkpoint 原始 `target_ids` 定义任务目标（去重），初译/润色阶段分开计数，避免“恢复后只剩本次剩余范围”漏计已完成段和“润色把初译+润色 success 相加”重复计同一 block。结果结构与口径：
  - `counts.target_blocks`：任务目标唯一 block 数（非本次执行剩余范围）。
  - `counts.translate.{target,completed,failed,locked,unhandled}`：四类互斥且恰好划分 target（completed+failed+locked+unhandled=target），优先级 completed > locked > failed > unhandled；已完成的目标保持 completed，`locked` 只统计尚未完成但因人工锁定而被跳过的目标，不再与 completed 重叠；`translate.source_changed` 是叠加标记（任务期间原文内容变化的块，通常为 completed 子集），不计入划分。
  - `counts.polish.{target,completed,failed}`：`target` 是允许润色的唯一 block 子集，`completed` 以 status=refined 计；两阶段各自计数，不再相加同一 block。
  - `failed` 只含“当前仍失败”的块：本轮新失败并集去掉后来重试成功/锁定的块；跨恢复用 checkpoint 的 `translate_failed`/`polish_failed` 累计并在成功/完成时剔除。
- 持久化兼容：`_save_checkpoint` 改为合并已有 `result` 而不是整体覆盖，避免 checkpoint 写盘丢掉已写入的 `counts`；checkpoint 自身的键仍以本次传入为准。
- 失败原因持久化：`TranslationEntry` 新增 `error` 字段；`_apply_outcome` 失败分支与 `_mark_failed` 写入具体原因，成功与人工编辑清空。API `translations/{locale}` 载荷直接带出 `error`。
- 子请求异常隔离：`_translate_units` 的左右子批改走 `_translate_units_isolated`，任一侧 `ProviderError` 只判该子批失败，父请求与兄弟批已解析的字段照常合并保留（复现诊断 3.2 的场景不再丢失已成功结果）。
- 用量不丢失：`ProviderError` 可携带 `ProviderResponse`；只返回思考内容、子批超时等错误路径仍把已获取的 usage/request_id 登记入账，不再当零消耗。
- 字段数预算：`_chunks` 除 2400 字符外增加 `DEFAULT_CHUNK_FIELDS = 12`，短段多时不再把 20+ 字段塞进一个请求；失败仍沿 `_split_units` 继续缩批（缩到单字段为止）。
- 思考截断归因：响应 `finish_reason` 为 length/max_tokens 且 reasoning 占输出 ≥60% 时，失败原因写成“输出预算用在思考过程”，指向换非思考模型/降低思考强度/减小分块。

### 已实现（前端）

- `TranslationEntry.error` 类型；失败列表与当前块失败提示显示真实原因；任务面板识别 `partial_failed`（“部分失败”），恢复/重试按钮覆盖该状态；用量面板显示 reasoning tokens；项目库翻译状态仍由统计推导（`failed>0` → partial）。

### 已验证

- `pytest tests` 共 189 项通过；新增回归：部分失败记录原因并在重试成功后清空、reasoning 预算被吃掉时的可执行原因、父批部分成功+子批超时时保留兄弟/父结果、错误响应携带 usage 仍记账、25 个单字段块按 12 字段上限分成 3 批；另新增计数契约回归：暂停后 `target_blocks` 保持完整 3 段且恢复后 completed=3、润色阶段计数不与初译相加、润色失败只计 `polish.failed` 不误记初译失败、`_save_checkpoint` 不覆盖已有 counts；收口复查补：已完成+人工锁定的重叠边界（四类之和恒等于 target，completed 优先、locked=0）、未完成锁定目标单独归 locked、暂停后已完成目标被锁定再恢复不重复计数。
- `tsc --noEmit` 通过；`vite build` 通过（Node 22.11.0 低于 Vite 要求的环境警告仍在，产物写入临时目录）。

### 未完成 / 未验收

- 逐请求审计（阶段、耗时、request_id、finish_reason、usage 是否可得的结构化流水）未落库；当前只在 job 级保存失败原因归类与计数。
- 超长单 text 字段的临时子片段 id 与聚合回填未实现（仍只在字段边界切分）。
- 按服务能力配置的思考开关/预算未实现：中转是否透传 `enable_thinking` 等参数尚未用小样本确认，未擅自新增会被静默忽略的配置项。
- 未用真实 provider 重试真实 25 段；未重建/停机更新已安装客户端；3.1/3.2/3.3 的修复尚未进入 `launcher/Literature.app` 的冻结 sidecar。

## 2026-10-12：最近翻译任务错误诊断（未修改业务代码）

- 只读 SQLite、译文层、canonical、客户端日志及已安装 sidecar，定位 `On Being Authentic` 的最新作业 `ec29fb0801544ebd8d1bf4e66ba35a85`（22:48:33–22:51:33，UTC+8）。本次目标 48 段，23 成功、25 失败；真实译文层共 590 translated / 25 failed。任务却保存 completed/100%/空 error。
- 25 失败段位于第 101–102 页的注释，重建为同批 2302 字符/25 字段。耗时 180.159 秒与 Provider 180 秒响应上限吻合；超时是较高可能性推断，旧版未保存该批错误，不能确认 ReadTimeout、限流或截断。
- 最近/上次重试/首次未译任务的已记账输出中 reasoning 占比约 88.0%/89.2%/90.1%；未返回或未记账请求的用量不可视为零。当前服务绑定 yellowbee/qwen3.8-flash-next，响应账本模型名为 Qwen，实际后端模型版本未知。
- 临时目录故障注入复现当前源码缺口：父批 3 段先返回 1 段完整译文、子批 timeout 后 3 段全失败且成功结果未保留；FakeClient 仅思考响应在 usage 返回前被 ProviderError 中断。相关既有 44 项自动测试通过，不能替代这些缺口的修复。
- 已安装 sidecar SHA-256 `0734660b…` 与 manifest revision `95fe704229347b9c3c75` 一致；只读 PYZ 代码对象确认仍为旧翻译实现。今天源码修复尚未进入安装包。
- 交付 `docs/TRANSLATION_TASK_DIAGNOSIS_2026-10-12.md`，给出失败审计/部分完成语义、子请求异常隔离、字符+字段组批、思考能力配置、用量保留、停机更新与小样本验收顺序。本轮未修改业务代码、真实文献/译文、安装包，未调用模型或 MinerU；方案尚未实施。

## 2026-10-12：F05 恢复契约（持久化 checkpoint，Orca Worker 实施）

Lead 隔离诊断发现：仅靠恢复时重新按 `scope` 筛选 current entries 不满足目标——`scope=untranslated` 恢复后重新计算为空（零请求却 completed）、`scope=book, force=True` 恢复会把已完成块重新初译（重复付费）。本轮只修此恢复边界，不展开其他模块。

### 恢复契约（实现）

- 启动时把“本次任务实际目标”写进 job `result_json`：`checkpoint_version=1`、`pass`（`translate`/`polish`）、`target_ids`、`translate_completed`、`polish_target_ids`、`polish_completed`。
- 恢复（existing `job_id` 且带 checkpoint）时**读 checkpoint**，不再 `resolve_scope`/`_eligible_blocks` 重筛：`blocks = target_ids − 已完成 − 已润色`（过滤不可译与已锁定）；`pass=polish` 时跳过初译阶段。
- `force` 只影响“首次如何解析目标”：目标存进 `target_ids` 后，已完成的块不会再被重译。
- 润色只针对本次任务自己翻译成功的范围内条目（`polish_target_ids`），不会误润色历史 translated；润色中途暂停按 `polish_completed` 只补剩余。
- 源变化/锁定仍由提交安全守卫（F01–F02 收口）处理；checkpoint 只决定“处理哪些、跳到哪个 pass”。
- 无 checkpoint 的旧 job 或新建 job 仍走原有重筛逻辑（向后兼容）。

### 已验证

- Python `177 passed`（+5：`test_resume_keeps_scope_targets_across_ranges`（untranslated/failed/blocks/stale）、`test_resume_force_book_polish_does_not_retranslate`、`test_resume_force_translate_midway_skips_completed`、`test_resume_polish_midway_only_processes_remaining`、`test_api_resume_uses_persisted_checkpoint`（HTTP resume 端到端））。
- 同步修正上一节 F05 “天然从润色继续”的过强描述。

### 仍未验收

- 真实 provider/MinerU/客户端端到端；checkpoint 只在每次 `_process_blocks` 返回后写盘（暂停/异常路径正确），未做逐块持久化的崩溃级恢复。

## 2026-10-12：第一轮收口（完整性/状态/兼容性，Orca Worker 实施）

在第一轮基础上按 Lead 复查意见收口；同样未提交、未打包/安装、未调用真实服务、未动真实库。

- **P0 源 hash 变化后不可复用旧译文**：新增 `TranslationEntry.partial`；`_reusable_previous(block, entry)` 要求“已校验且 source_hash 等于当前 canonical 摘要”才可复用。`_units_for` 对失败/待重译块一律全字段重发（不再用未校验片段做补缺）；`_apply_outcome` 失败时把本次已解析字段只存为 `candidates`，内容只在可复用时才带过来；`force=True` 全程 `reuse_previous=False`；导出 `_entry_complete` 拒绝 `partial`。回归：`test_source_changed_partial_retry_uses_current_source`、`test_force_partial_retry_ignores_previous_partial`、`test_f02_unverified_partial_is_candidate_not_content`。
- **P0 提交前复查 canonical 源**：`_commit_translation` 写盘前重新核对当前 canonical 源 hash（`_canonical_digests` 按 document.json mtime+size 失效，避免每块重解析）；不相同时 AI 结果只存候选、条目落为 failed/partial，不把旧源结果写成当前完成。回归：`test_commit_rechecks_canonical_source_hash`。
- **卡片状态与阅读一致**：新增 `align_structure_only(document, layer)`，`JobManager.refresh_translation_summary` 在结构补丁/撤销后直接写回纯结构对齐（无需再跑翻译作业）；`translate_stage` 改用 `stale_source` 判定 done/partial 并返回 `stale_source`，`ProjectLibrary.translateOf` 同步。回归：`test_structure_only_patch_aligns_card_state`、`test_project_pipeline_summarizes_independent_workflows`（纯层级对齐不产生 stale，正文→列表的 source 变化才 partial）。
- **兼容性门**：`/health` 新增 `translation_safety_v1`、`export_readiness_v1`，前端 `requiredCapabilities` 同步强制校验，旧 sidecar 会被提示退出重启更新。回归：`test_health_capabilities_include_safety_markers`。
- **导出策略与状态**：`ExportStrategy` 改为 `keep_source`（缺译标记并保留原文）/ `mark_only`（缺译处仅留标记），命名与行为一致（都是可识别草稿，不会静默回退）；`readiness.ready` 现在还要求 `missing_resources == 0`；ExportPanel 在项目/默认语言变化时重置语言与产物路径，source 模式不再显示“全书未翻译”警告。回归：`test_export_readiness_requires_resources`、`test_f06_mark_only_strategy_omits_untranslated_source`。
- 上一节 F02 描述中的“失败块只补缺字段”已被本节取代：未通过整块校验的片段一律只存候选。**性能取舍**：为保正确，失败块重试采用整块全字段重发（不再只补缺字段），代价是失败重试可能重复翻译同一块内已成功的字段；后续若要在保证源 hash/校验前提下恢复字段级补缺，需要为每个字段持久化“已验证”依据，本轮不做。

### 已验证

- Python `172 passed`（本轮新增 6 项边界回归，另更新 2 处既有断言）。
- `tsc --noEmit` 与 Vite 生产构建通过（/private/tmp/literature-safety-build-20261012）。

### 仍未验收

- 真实 provider/MinerU/客户端端到端、原生交互；`instant(save)` 仍只支持纯 text 段落；F07/F11 大范围/F12 未展开。

## 2026-10-12：第一轮翻译与导出闭环加固（Orca Worker 实施）

依据 `docs/PROJECT_REVIEW_2026-10-12.md` 的第一轮范围（F01–F06、F08–F10）实施；保留工作区既有未提交改动，未提交 Git、未打包、未安装/替换正在运行的客户端，未调用真实模型或 MinerU，未触碰用户真实文献库、`ocr/raw/mineru` 或 canonical 稳定 ID。

### 已实现

- **F01 并发写入安全**：`ProjectStore.translation_lock(project_id, locale)` 提供按项目+语言的写锁，翻译作业、人工编辑、解锁、即时翻译保存与术语表冻结/回滚共用。翻译作业不再整层重写：`TranslationService._commit_translation` 每次只重载磁盘最新层、只合并本次触碰的条目；`_merge_entry` 在发现运行期间出现的人工内容（锁定或 `edited`、且内容签名不同于作业基线）时保留人工内容并把模型结果存为 `candidates`，同时保留运行期间追加的 `selection_translations` / `revisions` 与人工冻结的术语表；`TranslationEntry` 新增 `candidates` 字段。
- **F02 字段级切分合并**：`_translate_units` 现在只返回字段级结果，`_run_chunk` 用“整块预期 segment 集合 ⊆ 已解析集合”做最终完整性判定；递归切分用 `_merge_segment_maps` 按 segment id 深合并，杜绝后到的一半按 block 覆盖前一半。未通过整块校验的局部字段只存 `candidates`，不写入内容、不作为补缺依据（见下一节收口）。
- **F03 质量门槛**：空 `<seg>`、重复 id、未知 id 均不进入已解析集合；`finish_reason ∈ {length, max_tokens}` 时即使标签闭合也强制切分/重试，切片耗尽仍不完整则只保留候选并记 `failed`，不再判 `translated`。
- **F04 纯层级变化不重译**：`resolve_scope("stale")` 只返回 source stale 且未锁定的块；`_eligible_blocks` 跳过内容与结构都已对齐的条目；作业开始前 `_align_structure_only_stale` 把仅结构变化的条目对齐到当前 `structure_revision` 且不发请求。`translation_stats` 新增 `stale_source_unlocked`。
- **F05 润色 checkpoint**：润色阶段的 `stop_reason` 会带回作业总状态，暂停落为 `paused` 且进度保持在中途；`_finalize_job` 用真实停止阶段标注“翻译/润色已暂停”。（早期描述曾说“恢复时天然从润色 pass 继续”，已过强：仅靠重新筛选 current entries 在 `scope=untranslated/force` 下会丢掉原任务范围，现由下面的持久化 checkpoint 取代。）
- **F06 导出语言与完整性**：`exporters.py` 新增导出视图：绑定真实 `locale`、`_entry_complete` 按整块字段与源 hash 判定完整，缺译处显式输出 `[未完成译文]`（`strategy=draft` 保留原文，`strategy=complete` 只留标记），HTML/EPUB 的 `lang`、导出报告与资源均用真实语言；新增 `GET /projects/{id}/export/readiness` 返回完整/缺译/失败/原文已变/资源缺失；`ExportInput` 新增 `strategy`。
- **F10 字段级编辑**：`TranslationService.edit` 支持按 `cells` / `items` / `caption` / `footnote` 分别保存（缺省沿用旧值），`TranslationEdit` 同步扩展；整块仍按第一轮约定锁定。
- **F08 前端状态**：`App.tsx` 改为按“是否有活动翻译作业或最新作业版本变化”增量拉取整层，首次运行即可看到新译文、闲置不再重复拉取；轮询错误与最后同步时间在状态栏可见；项目切换后旧响应不再污染（`activeProjectRef` 守卫）；`ReadingBlockCard` 编辑草稿独立于轮询，新版本到达显示冲突提示。
- **F09 划选确认**：`ReadingWorkspace` 仅在原文栏同一 block 内划选时浮出“即时翻译”按钮，点击才发请求；划选译文或跨段不弹菜单，翻译过程可关闭。
- **F10 前端**：表格逐 cell、列表逐 item、图注/脚注分别编辑；保存失败在卡片内可见。`ExportPanel` 接收真实目标语言、展示完整性报告、提供缺译策略选择。

### 已验证

- Python `166 passed`（基线 151 + 新增 15 项边界回归 `tests/test_translation_safety.py`）：直接复现评审六项（人工编辑被覆盖并解除锁定、两 cell 切分丢字段、空 seg 判完成、length+闭合标签判完成、纯层级变化入列、润色暂停误报完成）并断言已修复；另覆盖术语表冻结保留、失败块只补缺字段、导出语言/缺译标记/完整性预检、API 字段级编辑与 readiness、即时翻译保存守卫。
- `tsc --noEmit` 通过；Vite 生产构建通过，产物写入 `/private/tmp/literature-safety-build-20261012`。
- 因行为按 F04 变更，更新了两处既有用例的预期：`test_stale_scope_retranslates_and_clears` 与 `test_locked_stale_is_counted_and_excluded_from_stale_scope` 改用“正文→列表”的 source 变化来验证 stale 范围（纯层级变化现在是自动对齐而非重译）。

### 尚未

- 未用真实 provider / MinerU / 客户端做端到端质量、速度与原生交互验收；“自动测试通过”不等于真实质量或速度结论。
- 报告后续清单（F07 统一导出图题/表注/脚注、DOCX 合并 cell、嵌套目录与稳定锚点/EPUBCheck；F11 校对完成语义与多语言摘要；F12 项目索引/分页/虚拟化；回收站、译文历史恢复、搜索筛选、raw 重建 ID 映射、导出预览/历史、初始化向导、目标预设、全局 provider 调度/RPM/TPM/费用预留/token 分块/真实客户端验收）未实施。
- 已知待改进（本轮审查发现，未纳入第一轮）：结构校对完成后项目卡片的 `translate.state` 仍以合并 `stale` 判定，纯层级变化的卡片可能停在 `partial`（后续应改用 `stale_source` 或把对齐并入摘要刷新）；人工编辑整块锁定尚未按字段演进；即时翻译的 `save` 仅支持纯 text 段落。

## 2026-10-12：全项目产品与工程评审（未修改业务代码）

- 交付 `docs/PROJECT_REVIEW_2026-10-12.md`，覆盖四个用户目标、12 项主要发现、全流程功能补齐、用户介入点、三轮建议及验收指标；对照当前计划、日志、文件资源原型/设计和结构校对设计。
- 当前工作区 Python `151 passed`、TypeScript `--noEmit` 通过；Vite 构建到 `/private/tmp/literature-review-build-20261012` 成功。最初终端无 node，改用现有 `.tools/node/bin/node`；其 22.11.0 版本被 Vite 提示低于支持版本，主 JS 包亦有体积警告。
- 临时目录边界诊断复现六项：任务保存覆盖运行中人工编辑并解除锁定；表格字段切分重试后先成功的 cell 丢失；闭合标签伴随 length 仍判 completed；空 seg 条目仍 translated；纯层级变化仍成为翻译候选；润色开始时暂停最终误报 completed/100%。闭合标签诊断仅证明截断信号被忽略，不宣称该模拟文本存在实际语义截断。
- 其余静态发现包括导出语言固定 zh-CN、缺译回退原文未提示、图表注释/目录锚点等导出契约缺口、前端全量刷新与编辑草稿覆盖、划选即自动请求、结构化字段人工编辑不匹配、阶段完成语义与大库扫描风险。具体位置和验收建议见报告。
- 评审这一轮只新增评审文档和状态记录，未实施这些修复（第一轮修复见上一节）；保留用户既有未提交改动。未操作真实文献库、未调用外部模型/MinerU、未安装客户端或进行原生点击验收；自动测试通过不能替代真实质量和速度结论。

## 2026-10-11：文件资源管理模块打通（按已确认设计稿改造前端 + 后端补齐阶段摘要）

### 已实现（依据 `docs/FILE_RESOURCES_DESIGN.md` 与原型 `docs/prototypes/file-resources-v4.html`）

- 产品模型：提取是唯一前置门槛；提取完成后「结构校对 / 翻译 / 导出」三条工作流相互独立。卡片用四个并列阶段格表达，不做线性流水线。
- 后端 `literature/storage.py`：新增 `structure_review_summary(project_id)`，由 `structure_suggestions` 待确认数与已处理/补丁历史推导 `review.state ∈ todo|confirm|done` 与 `pending`；`_describe_project` 的 `export_count` 现在把导出目录（html、md+json）计为 1 个产物，不再只数文件。
- 后端 `literature/api.py`：新增 `translate_stage()` 与 `project_pipeline()`，`GET /projects`、`GET /projects/{id}` 返回 `review` 与 `pipeline`（`review` / `translate` / `export`）。`translate.state` 由译文摘要 + 该项目翻译 job 运行态推导（running 优先，其次 done/failed/partial/none），**不把项目归纳成单一 state**。`/health` 新增能力 `resource_pipeline_v1`，前端 `api.ts` 同步强制校验，避免旧后端被静默复用。
- 翻译摘要刷新：`JobManager.refresh_translation_summary()` 在结构建议采用/忽略、批量决策、结构补丁新增与撤销、本地校准、结构 AI 作业结束后调用，使「先翻译后校对」产生的 stale 能立即回到项目列表；`translation_stats()` 增加 `glossary_frozen`。
- 前端：`ProjectLibrary.tsx` 按原型重写（网格卡片 + 四格阶段栏 + 概览六维筛选 + 表格化列表就地展开 + 居中详情弹窗 + 翻译范围弹窗 + 卡片 ••• 菜单），新增 `resource.css`（设计 token 与组件样式），移除 `mineru.css` 中旧的资源库规则；`types.ts` 增加 `ProjectPipeline`/`ProjectReviewSummary`/`ProjectTranslateSummary`。翻译范围按状态动态出现：全书 / 仅未翻译 / 仅待复核（stale）/ 仅重试失败 / 选定章节。
- 接线 `App.tsx`：新增 `openProjectView(view, id)` 与 `translateProject(id, {scope, locale})`（可就地入队，不依赖当前已打开任务）；文件资源页进入即刷新并按 3 秒轮询（不再因存在「最近项目」而停止轮询）；详情动作分别进入 校对/阅读与翻译/导出，失败分段走既有 `POST /ocr/jobs/{id}/retry`。

### 已验证

- Python `151 passed`；新增 `test_project_pipeline_summarizes_independent_workflows`（校对 todo→confirm→done、`glossary_frozen`、结构补丁后 stale 刷新、导出计数）。
- `tsc --noEmit` 与 Vite 生产构建通过。
- 真实本地 API + 隔离种子库 + 浏览器（外置自动化浏览器，非模拟）逐项点击：卡片四格阶段、概览筛选计数、详情弹窗四条工作流行与进度条、列表就地展开、翻译弹窗的目标语言/动态范围（含「仅待复核段落」）、「查看记录」跳转结构校对工作区，均按设计渲染；控制台 0 报错。
- stale 传播端到端：对已完成翻译的项目施加结构补丁后，`GET /projects` 立即返回 `translate.stale=1`、`state=partial`，列表页在 3 秒轮询内把该行更新为「部分 3/3 · 翻译中」，翻译弹窗出现「仅待复核段落」。
- 冻结 sidecar：`src-tauri/binaries/manifest.json` revision `95fe704229347b9c3c75`，与源码 `/health` 指纹一致；sidecar sha256 `0734660b757660ea32be7dd14e6cb845febe0396441077b9907ace90805642a3`。重建 Tauri 包（59.04 MiB）并 `codesign --verify --deep --strict` 通过。

### 尚未验收 / 阻塞

- 已安装的 `launcher/Literature.app` 仍在运行，安装脚本按既有安全约定拒绝在运行中替换程序包；新版客户端尚未装到 `launcher/Literature.app`，需要用户完全退出 Literature 后执行安装并启动验证。
- 未做原生窗口（Tauri）逐项点击验收；未调用真实翻译 provider；翻译弹窗的「术语表」下拉目前仅回显冻结状态（实际术语表由既有译文层快照在后端生效），未做术语表选择/草稿入口。
- 未在真实大库上评估 `/projects` 每 3 秒聚合（逐项目读 `structure_suggestions`、`jobs`）的开销。

## 2026-10-11：翻译引擎 v2（对齐 TranslateBooksWithLLMs 思路，修复慢与质量差）

### 触发问题（实测复现）

- 用户实测项目 “Making Transcendents” 的翻译角色绑定 `yellowbee / qwen3.8-flash-next`（temperature 0.7）。`blk-00000040`（1032 字符）的译文在 700 字处断在 “...including the American” 半句上，却被记为 `translated` 正常落盘。
- 旧实现的根因：① 12000 字符/块 + 整块严格 JSON 校验，模型漏一个 block_id 或一个 cell_id 就整批 `ProviderError`，没有部分成功；② 无 `finish_reason` 截断检测；③ prompt 只有“不得增删 block”，没有翻译原则、专名处理、上下文衔接；④ 串行请求且每次 `httpx.post` 新建连接；⑤ 术语表整表进每块 system prompt。

### 已实现（后端 `literature/translation.py`、`literature/providers.py`、`literature/api.py`）

- 分块：按字符预算 2400（`DEFAULT_CHUNK_CHARS`）切块，block 之间不拆分；每块附带前文（≤700 字，取尾部）与后文（≤500 字，取头部）；顺序模式（workers=1）额外把上一块末段译文尾部（≤400 字）作为衔接上下文。
- 输出协议：从“整块 JSON”改为 `<src id="block#field">` / `<seg id="block#field">` 段落标签协议，字段级拆分为 `text`、`item:N`、`caption:N`、`footnote:N`、`cell:<id>`。解析用正则宽松提取，支持代码围栏与前后杂讯；缺哪段只重试/失败哪段。
- 失败处理：缺段或 `finish_reason ∈ {length,max_tokens}` 时按 segment 中点切分重试（`MAX_SPLIT_DEPTH=3`）；整份响应无 `<seg>` 且未截断（模型不守协议）立即判失败，避免指数级请求；连续 3 个分块彻底失败且零成功时熔断停止后续请求（`GLOBAL_FAILURE_LIMIT`）。部分成功仍落库，仅失败块进入 `failed` 供重试。
- 并发与连接：`ThreadPoolExecutor` 滑动窗口并发（默认 4，可调 1–8），主线程独占落库与 usage 记录，工作线程只做 HTTP+解析；`OpenAICompatibleProvider` 惰性复用单个 `httpx.Client`（keep-alive），对 429/5xx 做指数退避（尊重 `Retry-After`），超时不自动重发以免重复计费。
- 质量：system prompt 重写为学术翻译原则（完整性优先、准确性、自然表达、术语一致、专名一律中译并首次附原文、公式/代码/URL 保留、id 结构约束），润色 pass 用“文学编辑”prompt。术语表按块过滤（`GLOSSARY_MAX_ENTRIES=80`）注入 user prompt。输出做 CJK 后处理（去协议标签/围栏、修中日韩标点周围空格）。
- 两遍制：新增 `polish` 选项，翻译完成后对本次成功块自动跑一遍润色（进度 0–0.5 / 0.5–1）。
- 写盘：小块并发下按“5 块或 3 秒”节流整层写盘，避免 O(n²)。
- 接口：`TranslationJobInput` 新增 `polish: bool=False`、`parallel_workers: int=4(1–8)`；`/health` 能力新增 `translation_engine_v2`（前端 `api.ts` 同步强制校验）。`MockProvider` 改写为按 `<src>` 回吐 `<seg>`，即时翻译与连通性探测仍走纯文本分支。
- 前端：范围弹窗新增“并发请求数”（1–8）、“翻译后自动润色”与“强制重译已有译文”（忽略已译状态重跑，锁定段除外；换模型/换引擎后整本重跑用）。
- 保留不变：对照翻译、编辑锁定、单块翻译/重译/润色、stale 语义、范围选择、术语表版本、usage 账本。

### 已验证

- Python 全套 `150 passed`。新增 6 项：部分分块失败只标记缺段块、未闭合截断输出不落库、术语表按块过滤、并发 4 路全部译完、`polish` 后条目为 `refined`、CJK 后处理。另因 provider 改用 `httpx.Client`，把 3 项结构校验收据从 patch `httpx.post` 改为 `httpx.Client.post`（超时仍按 `connect=20/write=60/read=配置值` 断言）。
- `tsc --noEmit` 与 Vite 生产构建通过（`index-D-CgjgVH.js`）。
- 真实本地 API 进程 + 本地 mock OpenAI 兼容模型：41 块 / 2 分块，4 路并发 `completed`、41/41 完成、usage 与估算费用落账；随后把模型切成“不遵守协议”，作业 `failed`，全程只有 2 次模型请求（无指数重试）。
- 冻结 sidecar（非源码）隔离验证：`/health` revision `28201d29c74fe4916f8e` 与 `src-tauri/binaries/manifest.json` 一致，能力含 `translation_engine_v2`。
- 已安装到 `launcher/Literature.app` 并启动验证：`/health` revision `28201d29c74fe4916f8e`、能力含 `translation_engine_v2`，日志显示前端成功调用 `/translations/...`（能力门通过）。
- 重建 `src-tauri/target/release/bundle/macos/Literature.app`（59.02 MiB），包内 sidecar sha256 `83b01f9121afe190466f7cd042d750ebbea02f6d5dc2ee93665b2098e807f3a0` 与 manifest 一致，`codesign --verify --deep --strict` 通过。

### 尚未验收

- 未用真实 provider（`yellowbee / qwen3.8-flash-next`）跑整本书；真实译文质量、速度提升幅度与并发下的限流/费用仍未验收（本地 mock 只证明链路与并发，不证明模型质量）。
- 未做原生窗口逐项点击验收（范围弹窗的并发数/自动润色/强制重译、失败块重试、对照编辑）。
- `polish` 两遍制会使费用与耗时约翻倍，默认关闭；是否默认开启待真实费用数据。
- 旧项目已存的截断译文不会自动重译（source_hash 未变）；用户需用“强制重译已有译文”或单块“重译”重新生成。

## 2026-10-11：文件资源管理模块设计定稿（原型，未实现）

### 已确认（产品 + 前端设计）

- 提取（MinerU）是唯一前置门槛；提取完成后「结构校对 / 翻译 / 导出」三条工作流**相互独立**，可任意顺序与组合（可不校对直接导出、只校对不翻译、先翻译后校对）。先翻译后校对时结构改动同步到译文，`locked` 人工译文保留只提示复核。卡片不使用线性流水线。
- 卡片：类型色实底 + 白字的文件图标；四格独立阶段栏（提取/结构校对/翻译/导出）带独立底色；右上状态胶囊不显示「待确认」数量；**去掉底部进度条**。
- 网格视图点击卡片 → 居中详情弹窗（避免展开导致位置跳动）；列表视图 → 表格化栅格 + 行下通栏就地展开。
- 翻译必须在弹窗中**每次手动选范围**（全书 / 仅未翻译 / 仅待复核 / 仅重试失败 / 选定章节）。
- 概览统计按独立维度筛选（解析中/待校对/翻译中/有失败/已导出）。

### 交付物

- 交互原型：`docs/prototypes/file-resources-v4.html`（模拟数据、可点击）。
- 落地依据：`docs/FILE_RESOURCES_DESIGN.md`（含设计 token、组件规范、与现有代码映射、后端数据契约、实施 checklist）。
- OpenDesign 稿件：`opendesign-projects/2542d83c-2b70-43a2-ad66-e09274e12513/design.html`。

### 尚未实现

- 本轮**未改动任何业务代码**；前端 `ProjectLibrary.tsx`/`styles.css` 与后端 `/projects` 校对摘要均未改造。
- 后端已有可复用能力：`translations/<locale>/summary.json` 翻译摘要（`translation_stats`）、`resolve_scope` 的 `stale` 范围、结构补丁与建议决策接口；仅缺 `/projects` 的校对待确认数聚合。

## 2026-10-07：翻译模块前后端打通与客户端重打包（stale 语义、并发保护）

### 根因

- 已安装 `launcher/Literature.app` 内置后端 revision 仍是 `7557bb0a476f8584a731`（2026-10-04），早于 2026-10-06 的翻译模块；前端 `api.ts` 强制要求 `translation_module_v1`，因此打开「阅读与翻译」会直接报“前后端版本不一致（缺少能力：translation_module_v1）”。前端代码是新的、内置后端是旧的，即“前后端没打通”的实际原因。

### 已实现

- `entry_stale_reason` 区分译文落后的原因：`source`=原文内容已变（待复核、可重译）；`structure`=仅层级/标题类型调整（译文自动对齐、无需重译）。`entry_is_stale` 改为基于该判定。
- `translation_payload` 每个条目新增 `stale_reason`；`translation_stats` 新增 `stale_source`、`stale_structure`、`stale_locked`，并让 `stale` 覆盖全部待复核块（含已锁定），与 `stale_block_ids` 一致，修复锁定块被结构改动却不计数的漏报。
- `resolve_scope("stale")` 排除已锁定块（锁定条目不会被自动重译，需先解锁）。前端范围弹窗补上此前缺失的“仅待复核段落”入口，计数排除锁定块。
- 前端状态 chip 拆分为“待复核/已对齐”；块卡片按 `stale_reason` 显示不同提示与“重译本段”，锁定块也显示 stale 提示。
- `JobManager.start_translation` 增加单项目并发保护：已有 queued/running 翻译作业时拒绝新作业（返回 400），重复 resume 也拒绝，避免重复计费与并发读写互相覆盖译文条目。
- `App.translate` 失败改为 toast 提示，不再静默。

### 已验证

- Python 全套 `144 passed`（新增 3 项：stale 原因区分、锁定 stale 计数与范围排除、并发拒绝），`tsc --noEmit` 与 Vite 生产构建通过。
- 真实本地 API 进程 + 本机 mock OpenAI 兼容模型：全书翻译、预估、术语表冻结/版本、失败→resume 恢复、stale 范围、并发拒绝均按预期。
- 冻结 sidecar（非源码）隔离验证：`/health` revision `124e1fe574069b18c210` 与 manifest 一致、能力含 `translation_module_v1`，全书翻译作业完成、译文正确落盘。
- 重建并安装 `launcher/Literature.app`（59.01 MiB）；包内 `literature-sidecar` sha256 `f15debdc66cd12a2dfaba9a5f1a43ece0a52ff591c88f826232b496473e94f0e` 与 `src-tauri/binaries/manifest.json` 一致，`codesign --verify --deep --strict` 通过。启动客户端后前端成功调用 `/translations/...` 接口，说明能力校验已通过。

### 尚未验收

- 未调用真实翻译模型；真实 provider 的分块、费用与译文质量仍未验收。
- 未做原生窗口逐项点击验收（范围切换、目标语言、锁定块待复核提示、术语表回滚）。
- 并发/速率限制、任务花费硬预检仍未实现。

## 2026-10-06：翻译模块 M3 可用版（范围/状态机/stale 传播/术语表版本/预估）

### 已实现（后端）

- 数据模型：`Block` 与 `TranslationEntry` 新增 `structure_revision`（int，旧数据默认 0），`SCHEMA_VERSION` 升至 1.2。结构补丁 `_prepare_patch` 与 `revert_patch` 改到哪个块就给该块 +1。
- 段落级状态机与统计：`translation.py` 新增 `entry_is_stale` / `entry_state` / `translation_stats` / `resolve_scope` / `translation_payload` / `estimate_translation`。stale 判定 = 结构版本不一致（结构性改到该块）或 source_hash 与当前源文不一致；locked 条目保持“已锁定”但计入 `stale_block_ids` 供复核提示。
- 范围语义：`TranslationJobInput.scope` 支持 `auto|book|untranslated|stale|failed|blocks`；`auto` 保持旧行为（有 block_ids 即选段，否则全书）。stale 范围会绕过“已被翻译即跳过”的逻辑，强制重译被结构改动影响的块。
- `GET /projects/{id}/translations/{locale}` 现返回每个条目的 `state`/`stale`、`stats` 与 `stale_block_ids`；`POST /projects/{id}/translation/estimate` 在不发请求前给出块数/字符数/token/费用预估。
- 术语表版本：`translations/{locale}/glossary_versions.json` 追加不可变版本；`GET .../glossary/versions`、`POST .../glossary/rollback`（回滚也记为新版本）。
- 项目列表：`_describe_project` 附带 `translation` 摘要（读取 `translations/{locale}/summary.json`），作业完成、编辑、解锁与译文层读取时刷新摘要。

### 已实现（前端）

- `ReadingWorkspace`：目标语言选择（zh-CN/zh-TW/en/ja/fr/de，localStorage 记忆）、范围选择器（未译/待复核/失败/全书/选段，带计数）、进度与统计条、预估按钮与预估结果、单块操作。保留原三栏布局，范围不弹窗。
- `TranslationBlock`：状态 chip（未译/已译/已润色/已编辑/已锁定/失败/待复核）、双击或按钮编辑、保存自动锁定、解锁、单块翻译/重译/润色、stale 与锁定提示。
- `GlossaryEditor`：版本历史与回滚。`ProjectLibrary` 卡片显示翻译进度摘要。
- `api.ts` 增加 `translation_module_v1` 能力校验；`types.ts` 补齐 TranslationStats/GlossaryVersion/TranslationEstimate。

### 已验证

- Python 全套 `141 passed`（新增 `tests/test_translation_module.py` 9 项）：统计与 payload、结构补丁只标记目标块 stale、locked 仍 stale 标记、stale 范围重译并清除、范围解析（book/auto/untranslated/blocks/failed/非法值）、失败范围重试恢复、预估（有/无单价、完成后为 0）、术语表冻结/版本/回滚经 API、译文层 stats 与 estimate 端点。
- TypeScript `--noEmit` 与 Vite 生产构建通过（Node v22.11.0，仅 chunk 体积既有警告）。
- 真实 `python -m literature.api` 本地进程 + 隔离资源库冒烟：`/health` 含 `translation_module_v1`；导入离线 MinerU 夹具后 `/translations/zh-CN` 返回 stats、`/translation/estimate` 返回预估、术语表冻结/版本、`/projects` 返回 translation 摘要均 200 且内容正确。

### 尚未验收

- 未调用真实翻译模型；真实 provider 的分块、费用与译文质量仍未验收。
- 未做浏览器内点击验收（范围切换、目标语言、单块操作、术语表回滚、stale 提示），未重打包/重装 `launcher/Literature.app`，原生窗口仍未验收。
- 并发/速率限制、任务花费预检与真实费用对账仍未实现。

## 2026-10-04：结构规划响应超时与失败重试恢复

### 任务事实

- 只读检查 Journal 作业 `f309c6d2f67c4461a92a703812c38964`：231 个候选，使用 14 条确认目录，关闭视觉与自动采用；第一整体规划请求为 160 个候选+48 个 overview、44,749 字节 user 输入。16:32:53 UTC 请求开始，16:35:53 失败。栈位于 httpcore `_receive_response_headers`，明确为 ReadTimeout；无法从本机断言服务端排队/推理状态。没有模型响应或本轮结构补丁。

### 已实现

- 规划窗口 160→64，所有候选仍逐窗口覆盖，保持目录/profile、48 条整体 overview、边界上下文、当前祖先与最近 24 结果；跨窗口上下文补足原标题文字而不重复长理由。模型每条依据要求简短，界面逐规划窗口显示候选数/进度。
- 结构专用 provider 副本默认响应等待 600 秒，可在校对选项调整 30–1800；翻译 provider 默认仍为原 180 秒。httpx 连接/发送/池等待上限 20/60/30 秒（短响应配置取较小值）。分类报连接/响应/发送/池超时，结构错误补阶段；不因网络失败自动重试或拆批。
- 审计保存请求起止时间、elapsed_seconds、错误类型/等待上限、响应元数据及输入摘要键。用户再次运行失败任务传入 reuse_job_id；同项目 failed 任务才可作为复用来源。文档版本+模型配置摘要匹配后，再按阶段/system/user/图片摘要精确匹配。仅契约可解析的 responded/reused 响应可复用；旧审计缺字段、模型/文档/输入变化或坏 JSON 不复用。复用结果重新进入当前解析与父子校验，来源可追溯，不重复写 usage，可跨连续失败保留已完成请求。

### 验证与交付

- 全套 `132 passed`（新增 7 项），TypeScript 与 Vite 生产构建、diff 检查通过。新增回归验证响应/连接超时分离、无自动重发、64 条窗口覆盖/边界祖先文字与进度、连续失败恢复/用量去重、模型/文档/输入改变拒绝复用、坏响应不复用、API 等待范围及已完成任务拒绝复用。
- 实际新版冻结后端+生产 Chrome，在隔离库/运行配置、假 Key、127.0.0.1 模型桩上：目录与规划完成后模拟复核 HTTP 503；浏览器再点 AI 校准，POST 携带失败作业 ID 和 600 秒配置，复用 2 个响应，只重新调用复核。模型桩调用为目录 1/规划 1/复核 2（第一次失败），用量仅 3 条成功请求；角色/父子关系 4/4，3 个 pending、raw 哈希有效、0 脚本错误。报告 `/private/tmp/model-timeout-qa/report.json`，截图同目录 `ui.png`。此联调验证恢复路径；ReadTimeout 本身由注入异常回归验证，没有等待真实 600 秒。
- 已重建 sidecar、Tauri 包；检查原库无 queued/running 后正常退出、安装并重开客户端，严格签名通过。revision `7557bb0a476f8584a731`，sidecar SHA `580dfef1a5765d1d438ea6faaf961af5ac30b5c1e7a2fa43f77634956825fda9`，47604144 bytes。
- 本轮未发真实模型/MinerU 请求，原文献、旧失败作业与证据保持；未重复原生点击校准。窗口缩小增加请求数/重复上下文，长等待不能保证远端网关或服务不会超时；真实延迟、费用、效果仍待用户重试验收。原本超时请求的远端处理/费用未知。

## 2026-10-04：运行中安装替换导致 PyInstaller 解压失败

- 用户报告 `Error -3 while decompressing data: incorrect header check`。只读侧车日志定位到 Journal 作业 `0168700080b141e29a7d7bfec5058c14` 的 `plan_structure_analysis → visual_evidence → import fitz → pyimod01_archive.extract`，在本地规划阶段、任何模型请求之前失败。数据库保留该 failed 状态。
- 原客户端 PID 26831 / 后端 26845 启动于 10 月 3 日 22:57，安装包侧车修改于 10 月 4 日 00:06，报错任务于 00:26 启动。PyInstaller 本地加载器源码确认每次导入重开启动路径读取 PYZ，仍用启动时的 TOC/偏移；运行中替换归档导致新旧不一致。已安装侧车 SHA 与 manifest 完全一致，新进程可正常导入，排除当前包损坏。
- `setup_macos_launcher.sh` 增加两次目标进程检查（复制前/替换前）；检查失败或客户端/侧车仍在运行则退出且不替换。真实运行中验证退出码 2 与清晰错误；正常退出客户端后安装成功、严格签名校验通过，再打开客户端，确认新 PID 30547 / 后端 30570 及文献 API 读取成功。关闭前只读检查库内无 queued/running 项目作业。
- 使用已安装冻结侧车、隔离 runtime/library、合成两页 PDF 和假 MinerU 凭据，仅通过 loopback API 执行导入及 `/structure/plan` 的视觉路径：2 个 PNG（目录整页+标题裁片）、raw 校验通过，revision `0a907b0dbf2a480470b9`。报告 `/private/tmp/literature-visual-package-nslf54_7/report.json`；没有模型/MinerU 网络请求，未自动重试原文献 AI 任务。
- shell 语法检查与 diff 检查通过；本轮只改安装脚本与文档，未改后端/前端业务，也未重跑无关 Python 全套测试。实际 Journal 的真实模型复跑仍由用户重试验证。

## 2026-10-04：修订历史区分人工调整与采用 AI

- 修复显示统一为“人工确认”和建议采用接口错误保存 `source=user` 的两个问题。历史现在区分“人工调整”“采用 AI”“自动采用”，已撤销记录仍保留原来源。
- 单项/批量采用持久化建议保存 `source=ai, automatic=false`；直接编辑默认 user。兼容无 suggestion_id 的前端采用分支显式传递 ai 来源，批量成功提示改为“已采用…项结构建议”。
- 扩展现有 API 回归，验证单项建议采用、批量采用、直接手动编辑、历史读取及两个兼容入口的来源和 automatic 字段；手动批量回归也验证 user 来源。全套 `125 passed`，TypeScript、Vite 生产构建和 diff 空白检查通过。
- 已重建 PyInstaller sidecar、Tauri `.app` 并安装到 `launcher/Literature.app`；严格签名校验通过，revision `0a907b0dbf2a480470b9`，sidecar SHA `a7eaa01b0301513ffc2665c69c6856cf14d76f9539b320c14de41c109e80534d`、47599376 bytes。
- 本轮未调用模型/MinerU，未重复原生窗口点击验收。旧版已采用但保存为 user 的补丁缺少可靠关联，不能准确反推来源，因此未猜测回填或修改原项目记录。

## 2026-10-03（本轮最新）：Journal 复盘与 AI 整体标题树推理

### 已查明的任务事实

- 只读检查项目 `31f1beb79c884e618334463cdbfbd6dc`、已完成作业 `e6a172aba0cd45869ab7a6f7159961e7` 的 config/result/activity/请求审计与确认基准。147/147 是逐角色有效回答数；回答为 caption 70、h2 66、h1 7、body 3、h3 1。模型理由多次明确说“子标题/小节，保留 h2”，证明角色识别和层级恢复被混为一谈。
- 本地目录产生 14 条均为 heuristic H2，且作者姓名串入下一条文章标题，随后被用户保存为确认基准。4 个固定 40 条批次未携带完整父子/篇章上下文，前批降文章为 H2，后批保留文章为 H1；自报置信度合并不能解决全局一致性。
- 原项目、原 PDF、raw、确认基准、旧作业与人工决策均未修改。详见 `JOURNAL_STRUCTURE_REVIEW.md`。

### 已实现

- 新增 `structure_reasoning.py`：模型先判断文献类型、目录收录范围和根层级约定，按阅读顺序给出 role + parent_id 的整体标题树。每窗口最多 160 个短候选，48 条均匀抽样只作 overview；所有候选逐窗口覆盖，携带边界上下文、之前规划的当前祖先链与最近 24 条结果，目录和约定贯穿全任务。
- AI 主流程总是核对源目录页，本地目录只作草稿；作者与印刷页码独立字段。免费本地读取/校准入口保持。现有标题不因本地精确匹配而被跳过；增加无编号紧凑短段召回。长正文和图片/表格/代码不作为文字候选；竖向窄页边仍排除。
- `hierarchy-v2` 逐项角色复核携带全局规划、本批祖先和邻近标题；允许 uncertain，不强制维持无目录时的原层级。文档文本只视为内容。AI 模式不预先应用弱规则结论，也不再按本地高 confidence 抢占模型结论；所有模型修改待人工确认。
- 形式校验检查最终 level 栈生成的树是否与 parent_id 一致，包括同级父子、指向未来、跨新篇章与无效祖先；不通过的标题调整不进入建议，角色回答覆盖率与关系检查分别报告。兼容旧 suggestions 响应的标题修改必须有有效关系才可在新版作业中提出，旧同步 `/structure/analyze-ai` 接口也统一走整体推理/审计/持久化管线，不再绕过关系检查或自动应用边界。
- 包内联调发现建议父标题信息在原有存储固定字段中丢失；新增幂等 metadata_json 迁移、读写及旧决策保持测试，所属标题在重开后仍可见。
- 前端显示目录范围、根级约定、角色回答数、父子检查数和建议所属标题；说明回答完整不等于层级准确。实际审计保存每个阶段的输入、响应、usage，网络失败不通过拆批放大。

### 已验证与交付

- `125 passed`（原 111 + 新 14）；TypeScript `--noEmit` 通过；Node v24.21.0 Vite 生产构建通过（既有 bundle 大小提示）。新测试覆盖四级无编号标题树、同级父子与无效后代、跨篇挂接、缺失关系、规划和最终角色冲突、350 候选全窗口覆盖、跨批祖先、重复/uncertain/NaN、网络不重试、已确认匹配也复核、无编号候选与长正文隔离，以及作业入库/人工采用/撤销后文字/稳定 ID/顺序/raw 保持。
- 以上使用离线桩模型，证明调用契约、形式校验与存储闭环，不证明真实模型准确率。
- 已重建 PyInstaller sidecar、Tauri 发布包并安装到 `launcher/Literature.app`（58.99 MiB）；`codesign --verify --deep --strict` 与实际包内 sidecar 的 SHA/大小匹配通过。revision `66851b90aca4cd887701`，SHA `deba7d6b1768aefd0b0e093aa1b3621d625cdaf9af76aeaf841151bf1daaa0e2`，47598368 bytes。
- 实际包内冻结后端连接仅监听 127.0.0.1 的离线桩模型，隔离 library/runtime、假模型/MinerU 凭据：目录核对→整体规划→逐项复核三个阶段真实 HTTP 跑通；4/4 角色、4/4 父子关系、3 条 pending、自动采用开启仍不应用模型修改、raw 哈希有效。原 Journal 源 PDF SHA 与封存 raw 清单只读校验通过。
- Chrome headless 加载生产前端，连接上述实际冻结 API：角色计数、关系计数、目录范围、建议所属标题均可见，0 页面脚本错误；截图 `structure-hierarchy-v2-qa.png`，报告 `/private/tmp/mmj-hierarchy-qa/report.json`。原生 Tauri 窗口本轮没有逐项点击验收。
- 逐批输入去掉重复本地建议和标题全文，只保留相关目录锚点及祖先；尚未以真实 token 衡量节省。

### 真实服务与边界

- 新版未调用真实模型或 MinerU。已准备 `/private/tmp/mmj-hierarchy-v2-start-input.json`（Journal 第 4 页目录、231 个短候选及元数据，不含 PDF/图片/正文全文），向用户请求发往既有 yellowbee / glm-5.3-flash 的隔离试跑许可；当前未收到答复、没有发送。
- “有效父子关系”只是形式一致性；模型可以给出形式合法但语义错误的树。仍需多文献人工基准和真实模型评测，不声称全书校对完成。新增整体规划与目录核对会增加请求/token；尚未衡量成本收益。MinerU 没有提供字体信息时不伪造字体，几何仅作证据。超大文献通过窗口规划，边界语义一致性仍需实测。

## 2026-10-03（本轮最新）：独立样本探索与逐候选角色契约落地

### 实验与已实现

- 使用 `mineru-structure-calibration` skill，在 `/private/tmp/structure-lab-20261003` 复制两份既有真实 MinerU 输出：White Lotus 前 50 页（335 raw 块）与 Making Transcendents 前 20 页（106 raw 块）。先执行独立 skill 的零模型基线，再用实验适配器比较 Literature 改进版；没有改旧 Litera、原文献库或封存 raw，没有新 MinerU 提交。实验产物及方法见 `STRUCTURE_CALIBRATION_EXPERIMENTS.md`。
- 统一 `analyze_structure` 与 `reconcile_toc`：有序对齐直接决定实际建议，不再一项目录独立匹配多个块；同名项按顺序处理，歧义保留；已有页边/题注/脚注证据不能因目录同名而重新提升。
- 修复 TOC 行解析：尽可能恢复原始 spans 换行（仅在 raw 与当前文本一致时使用），保留已有换行、去项目符号、排除 MAPS/TABLES，拒绝无页码尾部、短 Chapter 前缀、句中年份/罗马词误切；罗马章号与多级数字正确推断层级，显式保留 heuristic/model 来源。White Lotus 图表目录 11 项不再混入章节目录；地图侧边的窄竖文字不再误提升。
- `enrich_furniture_evidence` 只读既有 raw 文件，补充已正确过滤的 page_header/footer 证据；新导入在页级元数据记录，旧项目分析时补齐。White Lotus 单个误标的 INTRODUCTION 因其他正确页眉提供重复证据被识别。
- 生产 AI 改为 `roles-v1`：每候选返回 `h1…h6/body/furniture/caption/footnote`（包括不变项）；校验合法 ID、有限 confidence、冲突重复与遗漏，显示 review_coverage。所有新角色模型建议人工确认，兼容旧 suggestions 响应但不计逐项复核。每批 40 条、文字最多 240 字符；仅响应解析/结构失败拆批，网络/鉴权错误立即报告，避免重试放大。
- 目录页选择贯穿本地抽取与作业计划；没目录时仍运行角色清理而不臆推层级。本地读取只产生可编辑待确认草稿，非法行阻止确认，服务端保存成功后才切换为项目确认基准；保存失败保留草稿并报告。视觉默认关闭。本地 analyze 建立作业并持久化 pending 建议，自动应用只标记实际应用项。
- 前端建议分组为全部/误标清理/层级调整/疑似漏标，批量只采用可见组，其他组保留；完整快照新鲜度检查包含列表 items，列表升标题与撤销保留文字。

### 已验证

- Python 全套 `111 passed`；TypeScript `--noEmit` 与 Vite 生产构建通过。新增回归覆盖全局建议匹配、图表目录、原行/罗马词/年份、推断层级不可自动、选择范围、角色不变与漏答、重复冲突、网络不拆批、旧 raw 页眉证据只读、窄侧边文字不提升和列表推广可逆。
- 独立副本 White Lotus 9 条建议/9 处 MD 修改（3 清理+6 层级）、Making 2 条/2 处。原文字/bbox/顺序与原始文件 SHA 保持；输出只是本地候选修订，不代表人工确认成品。
- 隔离 API：White Lotus 本地 65 条目录、9 条 pending；采用 6 条层级组后另 3 条 pending 保留，文本/items/bbox/顺序不变，撤销精确还原，重开 ProjectStore pending 保持，raw 验证有效。
- 本机 in-process 模型模拟：White Lotus 14/14、Making 4/4 返回角色、0 未复核，实际请求审计为 roles-v1；没有副本外写入、没有第三方请求。只验证管线与交互，不验证真实模型效果。
- 嵌入浏览器实际跑过本地目录草稿、模拟角色任务、覆盖率和层级组筛选；后端重启后回环 API 被浏览器报 `ERR_BLOCKED_BY_CLIENT`，其余用 API 验收。未宣称最终全部浏览器或原生点击通过。
- 使用已有 PyInstaller/Rust 与临时 Node v24 重建 sidecar、生产前端和 Tauri 发布包（约 58.98 MiB），更新 `launcher/Literature.app`。`codesign --verify --deep --strict` 与包内 sidecar SHA/大小匹配通过；runtime_revision `0c1a6853c2a0cd6ec397`，SHA `b27b7dbf781656dbc93fb2b02dd2beb5e278ad7dc17b98169f6bee7b933708a7`，大小 47590560 bytes。
- 实际冻结二进制用隔离 library/runtime 和假 MinerU Token 启动，未调用外部模型/MinerU：health 与 revision 一致、真实旧 raw 导入并补入页眉证据、本地目录 65 条、结构建议 9 条入库、raw 哈希校验有效。

### 外部验收与尚未

- 本轮真实模型请求未执行。首次访问 Keychain 受沙箱限制；随后用于试跑的提权请求被自动审批拒绝，明确理由为具体本地文档内容和外部 AI 目的地未获明确授权。未通过其他路径重试；已向用户展示固定目录+15 候选快照并请求许可，目标是已配置 yellowbee `maas.home.hgh.cn.akafeng.me` 的 `glm-5.3-flash`，不含 PDF、图片或正文全文。许可未回复前，效果/token/费用均保持未验收。
- 本轮新版角色契约不能用历史真实模型验收替代。缺少代表性题注/脚注错误、全书人工基准、印刷页码偏移、跨块标题合并、无目录无编号漏标、视觉缓存、完整原生交互与干净机器/Developer ID 公证验收。

## 2026-10-03（续）：前端与端到端流程验收（运行中的开发环境）

### 环境

- 本机 `PATH` 无 Node.js；下载解压独立运行时到仓库 `.tools/node`（已加入 `.gitignore`），并新增 `scripts/dev_frontend.sh` 一键启动本地 API + Vite（系统有 node 时优先用系统 node）。
- 开发环境：API `http://127.0.0.1:41731`（`LITERATURE_SESSION_TOKEN=literature-dev`，`LITERATURE_LIBRARY=.e2e/library` 隔离库），前端 `http://127.0.0.1:1420/`。

### 已验证（可复现）

- `frontend` 生产构建通过：`tsc --noEmit` + `vite build`（用 `.tools/node`，输出到 `frontend/dist`）。
- 真实后端 API 端到端（Test A，导入已有 MinerU 输出）：创建项目 → `/mineru/import`（等于 `_finish_import` 的 `import_raw_mineru` + `normalize_imported_project` 路径）→ 898 块/92 标题 → `GET /structure/local-toc` 104 条 → 手动 `set_type` 降级并 `revert` 精确还原 → 本地 `analyze`（18 条书眉降级 / 5 层级修正 / 3 漏标）→ 真实模型结构作业完成（104 条目录、44 条建议、0 条被拒）→ 采用建议写补丁。
- 真实浏览器（Playwright + Chrome，连真实后端）结构校对：本地读取目录 104 条、手动 H2→H1 生效、真实 AI 校准完成（对账摘要：目录 104 · 已定位 59 · 层级修正 4 · 疑似漏标 3）。
- 离线浏览器交互：一键「AI 校准结构（自动识别目录）」在目录页留空时发送 `extract_toc=true, toc_page_indexes=[]`；「本地读取目录 · 0 费用」回填基准并切到已确认模式；目录页输入默认收进「校对选项」；块结构下拉 9 项；Shift 多选跳过图片；批量后清除旧建议；页面 0 脚本错误。
- 真实前端导入点击路径：选择文件 → 逐文件设置对话框 → 开始解析 → 任务出现，页面 0 脚本错误。

### 未完成 / 外部依赖

- MinerU 云端往返：提交与轮询正常，但两个真实任务（22 页 vlm、3 页 pipeline）在本轮长时间停留在远端 `state=pending`，是 MinerU 服务队列而非本地问题；本地监视器继续轮询，完成后会自动落 `document.json`。Test A 已覆盖“云端完成后导入”的同一代码路径。
- 原生 Tauri 窗口与 `.app` 重打包未做（本轮以浏览器开发前端为交付面）。

## 2026-10-03：AI 结构校准升级——实验 → skill → Literature 落地（分层信号 + 有序对账）

### 背景与实验

- 在 Literature 之外建独立实验台，对两个真实 MinerU 输出做端到端校准：The Rhetoric of Immediacy（200 页 / 1293 块，书眉被大量误标 title）与 Unruly Gods（301 页 / 2554 块）。
- 结论：只“对照印刷目录”不够。真正的收益来自分层——本地几何/重复/文本信号先定案，目录用于全局对齐，模型只处理疑难项，且输入有界、可审计。
- 方法已沉淀为独立 skill `~/.agents/skills/mineru-structure-calibration/`（SKILL.md + scripts/run.py，支持 `--no-model` 零费用本地模式）。

### 发现的真实缺陷（已在 Literature 修复）

- **书眉误标不能只靠模型**：新增 `literature/structure_signals.py` 的跨页重复检测（归一化去页码 + 同一纵向区域跨 ≥3 页重复 + 重复主导区不是正文），`analyze_structure` 对其自动降级（`automatic=True`）。Rhetoric 样本本地即降 18 条书眉，Unruly 降 6 条 Glossary/Index 书眉；各章真实重复小节（Notes 等）因主导区为正文被正确放行。
- **编号前缀误配**：`_heading_similarity` 原先只凭“都以 2 开头”把脚注 `2. See also…` 匹配成第二章。现要求编号后的正文相似度 ≥0.5 才补偿；Unruly 的 23 条脚注误提升归零。
- **标记污染**：MinerU 把脚注/公式标记混入标题（`… equation_inline^{1}`）导致匹配失败。新增 `strip_inline_markers`，匹配与显示前剥离 `*_inline^{…}`、`^{…}`。
- **输出触顶**：真实模型（glm-5.3-flash）在 74 条候选时输出超过 8192 tokens、返回 `None`，整次校对失败。现默认每批 40 条，`max_output_tokens` 默认 16384（本机服务调至 32768），解析失败时对半拆分重试；小批次（<8）仍按真实失败抛出。
- **越批建议**：批次携带了不在本批候选中的 `local_suggestions/unlisted_titles` 的 block_id，模型据其提建议后被判“未知 block”丢弃（实测 21 条）。现按本批 block_id 过滤，实测降为 0。

### 新能力

- `structure_signals.py`：页眉/页脚/页码/题注/脚注识别、印刷目录抽取（含把被折叠成单行的目录按“页码 + 大写开头”恢复条目）、Needleman–Wunsch 有序对账。
- `reconcile_toc` 改为全局有序对齐；`structure_model_payload` 给每个候选附带本地信号（role / heading_score / y_top / reasons）。
- 本地读取目录：`local_printed_toc` + `GET /projects/{id}/structure/local-toc`；作业在 `extract_toc` 时优先本地解析印刷目录，解析失败才回退模型提取。
- 前端：主按钮改为“AI 校准结构（自动识别目录）”，目录页输入移入高级选项（留空自动识别），新增“本地读取目录 · 0 费用”。

### 已验证

- Python 全套 92 项通过（新增书眉自动降级、同名目录有序对齐、本地目录抽取、标记不阻塞匹配、作业本地抽目录不调模型）。
- Literature 完整作业链路真实模型验收（临时项目 + Rhetoric 样本）：本地抽目录 104 条（未调模型）→ 32 条候选单批 → 0 条被拒 → 45 条建议、自动应用 31 条，usage 8970/9214 tokens。
- 独立 skill：`scripts/run.py --no-model` 在 Rhetoric 样本跑通。
- 未做：前端 `tsc`/构建（本机当前 PATH 无 node）；原生窗口点击与打包验收。

## 2026-10-03：结构校对 AI/手动校准管线修复与 P0 加固（执行者）

### 已修复（跑通 AI 校准的关键缺陷）

- 既有项目的 `literature.sqlite` 只在 `create_project` 建库时迁移；上一提交新增的 `jobs.message/result_json/activity_json` 对旧库缺失，结构 AI 作业在第一次 `update_job(status="running", message=...)` 就抛 `no such column: message`，作业永远停在 `queued`，界面无限重连。真实文献库中的 4 个结构 AI 作业全部卡在 `queued` 且 `error` 为空，即由此产生。现在新增 `ProjectStore.project_database()`，在读写项目任务/修订/用量前按项目幂等建表并补齐新增列；10 处访问统一走它。
- 新增 `JobManager.recover_interrupted_jobs()`：应用启动时把上次进程遗留的 `queued/running` 项目作业（结构、翻译、即时）标为失败并写明“应用上次退出时该任务被中断”，避免界面把僵尸任务当成进行中任务无限重连。MinerU 作业仍由 `resume_ocr_jobs` 依据远端状态续跑。

### 已实现（设计师验收文档 P0/P1）

- P0 批量原子性：`apply_patch` 拆出 `_prepare_patch`（只校验并改内存文档）与 `_persist_structure`；新增 `apply_patch_batch` 先校验全部再一次性保存文档/source.md 并批量写修订；`apply_automatic` 也改走批量。`/structure/patches/batch` 不再逐项提交，任一项非法则整批不写。
- P0 陈旧建议：建议携带生成时的块状态 `before`（AI 作业另带 `document_updated_at`）。采用时 `_assert_block_fresh` 按块校验类型/层级/文本，不一致返回 400；按块而非整份文档判断，采用同批兄弟建议不会互相判为过期。`PatchInput` 新增 `before` 字段以便该状态经 API 传入。
- P0 手动提交锁：`ReviewWorkspace` 用 `submitting` ref 串行化手动结构动作，快速连点/快捷键/双击同帧只产生一份补丁。
- P0 建议决策持久化：新增项目级 `structure_suggestions` 表与决策，AI 作业完成时持久化 pending/已采用/已忽略；采用时服务端原子应用补丁并记录，新一次分析取代旧 pending 但不复活已决定项。重启后未处理建议从项目数据库恢复，不再依赖 WebView 偏好。
- P1 失败原因持续可见：作业面板失败时显示 `job.error` 而不是只靠短暂提示；建议卡片显示“第 N 页 · 原文类型 → 目标类型”。
- P1 目录基准入库：新增 `document/toc_baseline.json`、`ProjectStore.load/save_structure_baseline` 与 `GET/PUT /projects/{id}/structure/baseline`；前端优先加载项目基准，AI 提取采用、本地校准与 AI 校对时写回项目，不再只依赖 localStorage。

### 已验证

- 87 项 Python 测试通过（新增：旧库缺列迁移、启动中断作业恢复、批量原子、兄弟建议同批采用、陈旧建议拒绝、项目基准读写与 API 往返、建议 pending/已采用/已忽略持久化与决策 API）。
- TypeScript `--noEmit` 通过（借用系统内可用的 node v24 运行 `tsc`；未安装新依赖）。
- 重新构建 PyInstaller sidecar（`runtime_revision` c85e4dc0…）并在隔离库实测冻结二进制：`/health` 能力与版本一致；旧库自动补齐列并把遗留 queued 作业标为失败；基线 PUT/GET；本地校准返回带版本的 `set_level` 建议；带视觉的 AI 作业跑完（提取目录→本地对账→模型校对），逐批图片隔离正确；建议以 pending 入库并可单独采用、批量忽略，重启后 pending/已采用/已忽略状态均正确保留；陈旧建议采用返回 400 且不覆盖；有效+非法批量返回 400 且零写入；同批兄弟建议两条都生效。
- 上述 AI 作业验证使用本机回环桩模型（OpenAI 兼容），只验证链路、持久化与审计，不代表真实模型准确率或费用。
- 前端已重建：用临时 adhoc 签名的 node 副本与重签的 rollup/tauri native `.node` 绕过签名 Team ID 不匹配，`vite build` 与 `tauri build` 均成功；`launcher/Literature.app` 已重装为新包（前端资源 `index-JADs8ymS.js`、sidecar sha256 b52ff247…），`codesign --verify --deep --strict` 通过，启动后 `/health` 返回 c85e4dc0…。
- 为在 node 可执行文件名不是 `node` 的环境运行 Tauri CLI，新增 `scripts/tauri-run.cjs`（绕过 `tauri.js` 的 binStem 判断）。

### 尚未

- 未调用真实模型或 MinerU，未做扫描书准确率与费用验收；未做 Developer ID 签名、公证、Intel/通用构建或干净机器安装验收。
- 建议决策已入库（`structure_suggestions`），但前端建议卡片的显示与批量按钮仅在 `tsc --noEmit` 与打包层面验证，本轮未做原生窗口逐项点击验收。

## 2026-10-03：结构校对产品核查与基准入口修复

### 已实现

- AI 面板新增明确的目录基准来源选择。用户切换为“使用已确认的目录”后直接编辑基准，不再被源目录页校验阻断；没有原书目录时可自行填入标题。条目数量来自实际解析，空标题被过滤、缩进被接受，无有效条目时校准禁用并显示格式提示。
- 手动范围选择和修饰键选择只计入可修改的文本结构块；跳过图片、表格、公式、代码。批量修订保存成功后删除目标块的旧 AI 建议，避免再次覆盖人工调整。已有标题的结构下拉移除重复当前层级项。
- 新增 `docs/STRUCTURE_REVIEW_ACCEPTANCE.md`，列出具体问题、已修复项、建议持久化 / 批量原子性 / 陈旧采用 / 手动提交锁等剩余 P0 与最终客户端验收步骤。

### 已验证

- TypeScript 类型检查与 Vite 生产构建通过，产物写入 `/private/tmp/literature-design-validation-dist`，未覆盖现有客户端。
- Python 结构模块回归 41 项通过；本轮未修改服务端。
- Chrome headless 加载生产前端、所有本地 API 请求由离线模拟替代：基准来源切换、手动模式不要求目录页、非法格式提示、AI 请求参数、标题选项无重复、Shift 范围排除图片、批量修改清除旧建议通过，页面脚本错误为 0。该验证覆盖前端交互和请求契约，不等同于服务端事务或真实 AI 验收。

### 尚未验收

- 未调用真实模型或 MinerU，未改动文献库，也未重打包或替换 `launcher/Literature.app`。原生客户端最终整合打包、待确认决策重启恢复、批量失败一致性和跨窗口陈旧采用仍待执行者验证与修复。

## 2026-10-03：结构校对文本结构类型与本地优先 AI 对账

### 已实现

- 可修改结构类型对齐 MinerU 归一化后的文本结构：新增 `TEXT_STRUCTURE_TYPES = (paragraph, list, footnote)` 与 `EDITABLE_STRUCTURE_TYPES = (title, ...)`；图片、表格、公式、代码作为“由内容推导的类型”保持只读。`_apply_set_type` 支持文本类型互转，转列表时把正文搬入 `items`，转回时还原 `source_text`；补丁 before 记录 `{type, level, source_text, items}` 完整快照，`revert_patch` 精确还原且向后兼容旧的字符串 before。
- 自动应用边界收紧：`apply_patch` 仅在 已有标题 → 正文 时允许 `automatic`；`StructureJobInput.auto_apply` 默认改为 False；置信度必须是 0–1 的有限数值（拒绝 NaN、Infinity、布尔、越界、不可转换类型），层级必须是 1–6 的整数（拒绝小数与越界）。
- AI 校对重建为本地优先两阶段：新增 `reconcile_toc` 把已确认目录逐条对账到文档结构块，产出 `summary{已定位, 层级修正, 疑似漏标, 未定位, 未收录标题}`、`local_suggestions`、`missing`、`unlisted_titles`。`structure_model_payload` 只把本地未高置信定案的疑难候选（含疑似漏标短文本、未收录标题）发给模型，并携带 `unresolved_toc`／`unlisted_titles`；本地自动定案的块不再重复发送。
- 相似度改用带编号前缀补偿的 `_heading_similarity`；本地模糊匹配（非精确）一律 `automatic=False`，只有 `score == 1.0` 的精确匹配才可能自动改级；目录同名条目层级冲突时层级视为不可信。
- 明确选择目录页时 `excluded_pages` 只使用所选页（未选定时才回退到本地 `find_toc_page_candidates`），避免仅凭候选把真正的正文页误排除；`proofread=False` 时清空标题候选与标题裁片，`extract-only` 只发送目录页图片。
- 模型输出健壮化：编造或重复的 `block_id`、非法操作、非法置信度／层级一律跳过而非让整次作业失败，且**显式留痕**：`analyze_structure_with_provider(..., skipped=...)` 返回被丢弃条目及原因；`run_structure_job` 写入 warning 活动、结果 `skipped_suggestions` 与 `audit.skipped_model_suggestions`，界面同步提示，不默认为“全部建议已验证”。同批候选最多 80 个，标题裁片只附带属于本批候选的块（新增 `_batch_payload`）。本地与模型对同一块的冲突结论由 `_merge_suggestion` 处理：本地自动定案优先，否则取置信度更高者。
- 源目录提取后按动态疑难候选重新生成视觉裁片；`run_structure_job` 结果新增 `reconciliation` 摘要并写入进度活动。
- 前端：结构下拉框扩展为 正文／列表／脚注／H1–H6（单块与批量），快捷键同步；AI 面板合并“提取目录＋校对”为单一主按钮、移除冗余的“从源目录建立基准”勾选、显示目录对账摘要、自动应用／视觉／手动基准收进选项。
- 前端任务恢复：同帧防重复 POST（`posting` ref）；离开后重连运行中作业；状态读取失败时保留当前任务并 3 秒重连；已完成作业仅在 `localStorage.literature.review.consumedJob.<projectId>` 未记录时回填建议与源目录，并在作业保存的 `document_updated_at` 与当前文档不一致时不回填、提示重新校准。**注意：这个消费标记只是防重复回填，不等同于完整的人工决策持久化——应用重启后尚待处理（未采用/未忽略）的建议不会自动恢复。**后台失败错误保留在任务面板。

### 已验证

- 77 项 Python 测试通过（新增 15 项）：结构类型互转与精确撤销、自动应用仅限降为正文、非法置信度／层级拒绝、provider 跳过非法值并留痕、模糊匹配保持人工、明确目录页不误排正文、proofread-only 仍排除识别出的目录页、对账摘要、失败 JSON 仍记录 usage 与审计、批次候选与图片隔离、extract-only 无标题图、跳过建议在批次审计（rejected_count/reasons）与活动中显式告警、结果记录 document_updated_at 且后续改结构能被检测、本地／模型冲突合并。
- TypeScript `--noEmit` 通过；Vite 生产构建通过（本机 shell 无 node，用现有可用运行时并把临时副本 adhoc 重签以解决 rollup 原生模块的签名不匹配；未安装新依赖）。
- 重新构建 PyInstaller sidecar 与 Tauri 发布客户端并更新 `launcher/Literature.app`（约 59 MiB，Apple Silicon，最低 macOS 15）；`codesign --verify --deep --strict` 通过。包内 `Contents/MacOS/literature-sidecar` 与 `src-tauri/binaries/manifest.json` 的 sha256（05da37f9…）与大小（47.55 MiB）一致。
- 冻结 sidecar 在隔离库实际运行（`LITERATURE_LIBRARY`/`LITERATURE_RUNTIME` 指向临时目录、显式 token、不调用模型、禁用真实凭据）：`/health` 能力齐全且 `runtime_revision` 47b98b95… 与构建清单一致；离线 MinerU `content_list_v2` 导入、`/document`、`/structure/analyze`（本地 set_level）与 `/structure/plan`（对账摘要）均返回 200。
- 打包链路用已有 cargo/rustc（离线 `CARGO_NET_OFFLINE=true`）与已安装 PyInstaller，未安装新依赖；因本机无 `pnpm`，以 `-c '{"build":{"beforeBuildCommand":""}}'` 跳过 Tauri 的 pnpm 构建步骤，前端 `dist` 已先行构建。

### 尚未验收与外部服务事实

- 未调用真实视觉／文本模型，未做扫描书准确率与费用验收；服务端视觉缓存、无目录且无编号的漏标识别、人工决策持久化仍是下一步。
- 原生窗口 AX 自动化不可靠，未做原生文件选择/拖放/另存的实际点击与视觉验收；未做 Developer ID 签名、公证、Intel／通用构建或干净机器安装。

## 2026-10-03：WebUI 转为独立 macOS 客户端与资源整理

### 已实现

- 根据用户本轮要求，当前交付优先级从 WebUI 切换为 Tauri macOS 客户端；同步更新 AGENTS.md、PROJECT_PLAN.md、README、架构与打包文档。React 工作区复用，浏览器保留为开发调试入口，旧 Litera 未改动。
- Rust 内置服务管理改为动态 loopback 端口、随机会话令牌、单实例聚焦、启动错误反馈、日志、退出进程组回收；Python 监测 App PID 处理父进程结束。发布只使用同包 sidecar，开发态可使用 .venv/显式解释器。
- 前端初始化共享就绪 Promise，实际业务请求等待连接，检查能力与本会话鉴权后才展示工作区，避免子组件抢先连接旧端口。新增 desktop_resources_v1 标记。
- 原生多选与拖放接入路径队列，认证元数据接口返回文件大小及 PDF 页数，保留逐文件设置。确认解析时直接创建项目原件副本，跳过 multipart 暂存；浏览器调试仍有上传回退。
- 当前机器继续使用既有 library；新机器默认使用 Application Support/Literature/library。library-location.json 保存位置；设置可打开资源库/运行数据/日志并选择其他独立文件夹，重启生效，旧数据不自动搬移。选择时检查可写并禁止选现有项目内部目录。
- 文件资源提供 Finder 定位，导出显示实际路径，EPUB/DOCX 与原件可通过原生对话框另存。Rust 校验真实路径，拒绝越界与符号链接逃逸，另存不覆盖应用管理数据。
- 实际接通 PyInstaller 与 Tauri 发布链；后端附带源码指纹和目标二进制哈希清单；本机构建约 59 MiB、Apple Silicon、最低 macOS 15（与打包 Python 库要求一致）。补上应用 adhoc 签名和严格校验。launcher/Literature.app 已替换为独立客户端。
- PDF worker/WASM/ICC/CMap/标准字体与许可证改为从锁定 pdfjs-dist 自动生成，移除旧静态副本。删除未引用 PdfPage.tsx、旧 JXA 与 start_webui.sh 浏览器启动链；保留现有其余组件和历史文档。
- 新增 clean_resources.py 固定允许列表与预览功能。本次删除 2909.5 MiB（约 2.84 GiB）的 PyInstaller/Vite/Rust 中间产物、schema 和源码缓存；保留客户端、release bundle、开发依赖、sidecar 输入、library、回收站和应用配置。库外旧源码目录未清理。

### 已验证

- 57 项 Python 测试通过，新增认证本地元数据、重复路径/错误文件、无上传副本、资源位置及 Tauri Origin CORS 测试。
- 2 项 Rust 测试通过：管理路径与符号链接逃逸限制、后端进程组回收；TypeScript 和最终 Vite/Tauri release 构建通过。最后补充资源库选择/初始化错误处理均已编入最终包。
- 自包含 sidecar 在 /private/tmp 隔离资源库实际运行，鉴权、源码指纹、3 页 PDF 元数据、离线 raw 规范化、四种导出和 raw 哈希校验通过；不依赖源码 Python 启动后端。
- 系统记录新 bundle ID 的客户端进程与本地 API 请求。实际重复打开保留同一个 App 及两级 frozen sidecar；结束 App 后 5 秒内两级后端都退出。
- 安装后的应用 codesign --verify --deep --strict 通过。缓存删除后，launcher 与 release bundle 仍各约 59 MiB，原 library 仍约 324 MiB。现有 6 个项目保留，其中 4 个有 raw manifest 的项目哈希复核无错误；另 2 个没有 manifest，未尝试补写或将其报告为证据已验收。

### 尚未验收与外部服务事实

- 原生窗口的自动化 AX 读取多次超时；不能把进程存活或 API 请求成功当作原生对话框、拖放、Finder/另存按钮及 PDF 视觉验收。上述实际点击与视觉项目仍待手工验收。
- 首页初始化曾读取既有 Keychain Token 并成功执行 MinerU 连通性检查。本轮没有上传真实文献、创建远端解析批次或调用视觉/翻译模型，真实服务能力验收状态未提升。
- 未做 Developer ID 签名、公证、Intel/通用构建、干净机器安装或真实局域网模型服务在新 bundle 身份下的验收。本地 adhoc 包是当前机器可运行交付，不代表已完成对外发行。

## 2026-10-03：结构校对简化、源页视觉与分层校准

### 已实现

- 左侧改为文档目录与 AI 校准，移除翻译范围勾选、技术状态步骤和数据文件名；高级目录文本、本地校准和审计按需展开。右侧当前块用一个正文／H1–H6 下拉框编辑，图片、表格等不显示标题编辑；可见复选框与批量下拉框降低多选操作负担。
- 建立独立源目录基准：要求查看并填写 PDF 目录页；不再把 MinerU 解析目录自动当作可靠基准。目录基准按项目保存在本浏览器 localStorage，文档修订不覆盖它。建议默认待确认，可以采用／忽略；历史按同块逆序撤销，整体撤销按时间逆序处理。
- PDF 预览不依赖 bbox 是否存在，按源文件真实页数补全页面，新增页码跳转、缩放、结构框开关。SplitPane 的最小宽度现在同时应用于窗口布局，并修正小于 1 的 fr 比例造成窄窗留白；窄窗顶栏不显示被挤成竖排的标题，预览工具栏可完整换行。
- 视觉校准接入现有模型服务：选定源目录页文本／图片（最多 6 页）用于提取独立目录，少量标题 bbox 裁片用于排版判断（最多 6 个）。兼容 /chat/completions 的图像 content parts，沿用 usage 账本；输入计划可实际预览将发送的图片，不输出 Base64 长串。
- 本地校准处理明确目录层级差异；正确标题跳过模型；短正文与目录匹配生成漏标 set_title 建议，紧凑短编号标题作为模型候选。疑难标题最多 80 个一批。未入目录的小标题保持原类型，本地长文形态判断不再授权自动降级；重名目录层级冲突不自动匹配。
- 关闭自动应用时所有未应用建议保留在队列；补回标题不自动应用；分析期间结构变化则拒绝陈旧结果。结构操作限定标题与正文，防止图片／表格被快捷键或批量操作改成标题。健康能力增加 structure_vision_v1。
- OpenDesign 在本会话创建三栏交互稿，包含层级调整、目录定位、AI 演示建议、采用／忽略、批量修改与撤销；演示不调用外部模型。

### 已验证

- Python：55 passed（新增 10 项），覆盖漏标、编号候选、视觉请求、正文输入边界、重复目录冲突、陈旧结果与撤销顺序；旧测试调整为明确的目录层级自动修正及跳过冗余模型调用。
- TypeScript --noEmit 与 Vite 生产构建通过，产物仅写 /private/tmp。已有 PDF worker 带来的包体积提示仍存在。
- 使用 /private/tmp 隔离项目库、禁用真实 MinerU／模型服务的本机 API + Chrome：PDF 渲染、正文升标题、Markdown 同步、单项撤销、bbox 选中、150% 缩放、页码跳转、目录图片输入预览与两块批量调整均通过，页面脚本错误为零。
- 桌面与 1024px 截图确认三栏和源文件可见，窄窗三栏宽度为 260／364／364px，顶栏仅出现一次。示例截图保存在 docs/structure-review-20261003.png。

### 尚未验收及边界

- 未调用真实视觉模型，不能把图片输入单元测试或演示建议表述为实际扫描书校准准确率／费用已验收。Office 视觉预览、无目录无编号漏标标题、长作业恢复、视觉缓存、预算硬上限和完整逐批实际输入审计尚未实现。
- 输入计划是初始目录／候选／图片集合，目录提取后会动态生成新候选；普通正文不全文发送，但疑似标题的短正文候选可能实际属于正文。完整设计和性价比策略见 docs/STRUCTURE_REVIEW_DESIGN.md。

## 2026-10-03：模型列表直读、代理环境规范化与局域网模型服务直连修复

### 已实现

- 模型服务「拉取模型」：新增 `providers.list_remote_models(base_url, api_key)` 读取 OpenAI 兼容的 `GET {base_url}/models`，`ModelServiceStore.list_models(service_id)` 用该服务已保存的 base URL + Keychain 密钥调用，API 增加 `POST /model-services/{service_id}/models`。前端 `workbench/ModelServicesDialog.tsx` 在「模型 ID」旁新增「拉取模型」按钮，结果通过 `<datalist>` 提供给输入框下拉选择；`mineru.css` 新增 `.model-input-row` 样式。
- 代理环境规范化：`providers.normalize_proxy_environment()` 把 `NO_PROXY`/`no_proxy` 中带方括号的 IPv6（如 `[::1]`）规范为 httpx 可解析的形式（`::1`），并在 `OpenAICompatibleProvider.complete()` 与 API 启动时（`main()`）调用。
- 局域网模型服务直连（macOS 本地网络隐私）：`scripts/Literature.js` 改用 `NSTask` + `waitUntilExit` 启动 `scripts/start_webui.sh`，让启动器 App 常驻；`scripts/start_webui.sh` 末尾 `wait "$api_pid"`（未新起 API 时 wait `web_pid`）保持脚本存活；`scripts/setup_macos_launcher.sh` 与新增的 `src-tauri/Info.plist` 增加 `NSLocalNetworkUsageDescription`。

### 已验证

- 后端 `pytest tests/test_model_services.py` 4 passed（新增「模型列表端点」用例，含服务不存在返回 502）。
- `tsc --noEmit` 通过。
- 真实局域网 relay（`http://maas.home.hgh.cn.akafeng.me/v1` → `192.168.21.3:80`）连通性：替换为有效 Key 后，`POST /model-services/{id}/models` 返回真实模型列表（`deepseek-v4-flash` / `glm-5.3-flash` / `qwen3.8-flash-next` 等 5 条）。
- 进程树核对：`Literature.app applet → zsh start_webui.sh → python -m literature.api`，后端不再以孤儿进程（PPID=1）运行。
- 启动器生命周期：退出 App 后 6 秒内 api/vite 自动退出、`launch.lock` 自动释放；退出后再次启动可正常拉起；重复启动不会产生重复进程。

### 本轮发现并修正

- `NO_PROXY=...,[::1]` 会让 httpx 0.28 的 `is_ipv6_hostname` 拼出 `all://*[::1]` 并抛 `InvalidURL: Invalid port: ':1]'`，被包装成「模型请求失败」，请求根本没发出。已在 providers 层规范化。
- 应用内访问局域网模型服务报 `[Errno 65] No route to host`，而终端直连正常。根因是 macOS 14+ 的本地网络授权按 App 归属：旧启动器 `doShellScript("... &")` 启动后立刻退出，后端成为孤儿进程，系统日志出现 `Got local network blocked notification ... bundle_id: (null)`，且「系统设置 → 隐私与安全性 → 本地网络」中看不到 Literature。修正为启动器常驻持有进程树。
- 常驻化带来的生命周期缺陷：结束时 App 被强杀后，它拉起的 zsh/API/vite 不会跟着退出（父死子不灭），`launch.lock` 也留在原地；再点快捷方式时脚本看到锁就 `exit 0`，表现为“启动没反应”。修正：锁内记录脚本 PID 与启动它的 App PID，仅当两者都活着才算在运行，否则视为陈旧锁并接管；并在 `start_webui.sh` 末尾加 watchdog 轮询 App PID，App 一死就停掉服务并释放锁；陈旧锁接管时先停掉旧进程树再以当前 App 为父重新拉起。

### 未验证

- 「拉取模型」下拉控件的肉眼验收（后端接口已多次返回真实列表）。
- 启动器 watchdog 在 App 收到 SIGKILL 时的清理时机（已用 SIGTERM 场景验证）。

## 2026-10-02：任务生命周期、模块空白页、可收起目录与模型服务库

### 已实现

- 任务生命周期：顶栏项目区新增「关闭任务」(✕)，关闭后清空项目上下文并停留在当前模块；三个工作区（结构校对 / 阅读与翻译 / 导出）都支持无项目进入并显示空白页，内含可搜索的已有文档简列表与「去首页上传新文件」（`frontend/src/workbench/TaskPicker.tsx`）。
- hash 路由支持无项目工作区：`#/review`、`#/read`、`#/export`；带项目仍为 `#/p/<id>/<view>`。
- 结构栏「目录」改为可收起的 `<details>`，与「确认原书目录基准」「修订历史」一致。
- 模型服务库（`literature/model_services.py` + `frontend/src/modelServices.ts` + `workbench/ModelServicesDialog.tsx`）：集中管理多个模型服务（OpenAI/DeepSeek/GLM/Qwen 预设或自定义中转站 base_url + key + model），并绑定到 `structure` / `translation` 两个任务角色；密钥只进 macOS Keychain（account = service_id，预设可回退旧 providers Keychain）。API：`GET/POST /model-services`、`PUT/DELETE /model-services/{id}`、`PUT /model-services/{id}/key`、`POST /model-services/{id}/check`、`PUT /model-services/roles`。
- 结构基准交给模型：新增异步作业 `POST /projects/{id}/structure/jobs`（后台线程 + SQLite `jobs` 新增 `message/result_json/activity_json` 列，幂等迁移），步骤为 本地识别目录页 → 模型从目录页提取目录条目 → 模型校对标题层级（0.97 自动应用）。界面在「确认原书目录基准」显示进度、当前步骤与精简活动摘要，并可「采用模型提取的目录」。
- 审计边界：`POST /projects/{id}/structure/plan` 返回与实际作业完全一致的输入；开启「从目录页提取」会把选中目录页文本发送给模型，预览里明确展示，正文永不发送（`body_text_included: false`）。
- 翻译与即时翻译改为按 `service_id`（翻译角色）调用；全局设置不再配置模型，改为入口链接。
- `/health` 新增能力标记 `model_services_v1`、`structure_job_v1`。

### 已验证

- 后端 `pytest` 44 passed（新增 `tests/test_model_services.py` 3 项：服务 CRUD 与角色绑定、plan 的目录页文本开关、后台作业活动与结果）。
- `tsc --noEmit` 与 Vite 生产构建通过。
- Chrome headless 截图核对：`#/review` 无项目时显示任务选择空白页；`#/p/<id>/review` 显示模型字段、可收起目录、作业入口与关闭按钮；`#/p/<id>/read` 布局与模型字段正常。

### 未验证

- 模型服务弹窗、真实模型调用与目录提取、拖动分隔条、窄视口均未经肉眼或真实凭据验收。

## 2026-10-02：可拖拽三栏、面板内滚动与跨页任务导航

### 已实现

- `frontend/src/viewState.ts`：零依赖 hash 路由（`#/home`、`#/resources`、`#/p/<id>/review|read|export`）。刷新、后退/前进、复制地址均保持同一项目与视图；`location.hash` 是唯一写入点，`hashchange` 是状态唯一来源。
- 项目上下文与视图解耦：`literature.lastProject` 记忆最近打开的项目，浏览首页/文件资源时顶栏仍显示项目与三个工作区标签；被移出资源库的项目会自动清理记忆。
- `HomeTopbar` 工作区标签始终可点：未打开项目时跳转文件资源并提示（关闭 PLAN G1）；设置入口在全部页面可用，未打开项目时也能配置模型密钥、单价、附加要求、MinerU Token 与 TBL（关闭 G2）。
- 五个页面并入同一 `.app-shell` 外壳与滚动模型，不再切换 `.intake-shell`/`.app-shell`（关闭 G4）。
- 修复左「结构与任务」栏无法滚动：`.review-grid > .sidebar{overflow:hidden}`（0,2,0）压过 `.mineru-control-column{overflow-y:auto}`（0,1,0）。改为常驻标题 + `.pane-scroll` 独立滚动；阅读页同上。
- `frontend/src/SplitPane.tsx`：结构校对与阅读翻译三栏可拖动调宽，带最小宽度夹取、双击/Home 复位、`←/→` 微调，比例持久化到 localStorage。
- 抽出 `frontend/src/workbench/`（Settings、ExportPanel、GlossaryEditor、JobPanel、TranslationBlock、InstantPanel、ReadingWorkspace），`App.tsx` 内联组件清零（关闭 P1-6 结构部分）。
- 修正 `styles.css` 的 `.project-topbar nav{min-width:560px}` 为 `.home-topbar.project-topbar nav`（特异性 0,2,0）并让工作区标签在 ≤900px 保持可见。
- toast 6 秒自动消失；设置抽屉 Esc 关闭；资源卡片标记「当前任务」并提供「继续任务 →」。

### 已验证

- `frontend`：`tsc --noEmit` 通过；Vite 生产构建通过。
- Chrome headless 对 `#/home`、`#/resources`、`#/p/<id>/review`、`#/p/<id>/read` 截图核对：顶栏统一、未开项目时标签为提示态、左栏可滚动、三栏分隔条可见、阅读页左栏固定底栏。

### 未验证

- 真实鼠标拖动分隔条、双击复位与键盘微调未经手工操作验收。
- ≤900px 窄视口只做了 CSS 特异性修正，未肉眼回归。
- 本轮未改动后端；`pytest` 41 passed 无回归。

## 2026-09-30：统一主导航外壳（校对 / 翻译 / 导出整合为主 WebUI 标签子页面）

### 背景

- 首页与文件资源页共用 `HomeTopbar` 的 `intake-topbar` 主导航（首页 → 文件资源 → 结构校对 → 阅读与翻译 → 导出，未打开项目时后三个标签禁用）；但打开处理好的资源后，工作区切换到 `styles.css` 里另一套 `app-shell` + `topbar` 顶栏（独立品牌栏与样式），视觉与交互上像跳转到了独立 Web UI。

### 已实现（前端）

- `HomeTopbar.tsx` 扩展为唯一页面外壳：新增 `project` 导航参数；打开项目时「结构校对 / 阅读与翻译 / 导出」成为同一主导航下的可激活标签子页面（active 态沿用 `intake-topbar nav button.active`），未打开项目时保持禁用；`ImportWorkspace` 与 `ProjectLibrary` 现有调用不变。
- `App.tsx` 打开项目后（含 document 未生成的「资源仍在处理中」空态）改用统一 HomeTopbar：导航「首页」回到上传页、「文件资源」回到资源页；用量（Token · 费用 / 预算）与设置入口保留在顶栏右侧；项目标题与语言对移到品牌旁；底部 statusbar 与三块工作区内容不变。
- 移除旧的独立 `.topbar` 顶栏渲染；`styles.css` 中旧顶栏样式暂保留未清理。
- `styles.css` 新增 `project-topbar` 修饰样式：品牌+标题组、顶栏右侧用量+设置、nav 最小宽度；其中 `.home-topbar.project-topbar nav button:nth-last-child(-n+3)` 的特异性（0,3,2）高于 `mineru.css` 900px 媒体查询里的隐藏规则（0,2,2），项目工作区在窄视口下 5 个标签仍然全部可见（媒体查询不改变特异性，与源顺序无关）。`mineru.css` 未改动，首页/资源页窄屏隐藏禁用标签的原设计保持不变。

### 已验证

- `tsc --noEmit` 通过；Vite 生产构建通过（`dist/pdfjs` wasm/icc 资源随构建生成）；pytest `41 passed` 无回归。
- 构建产物 `dist/assets/index-Cq7q3FqB.css` 中两条 nth-last-child 规则分别在 offset 6759 与 39640，项目顶栏覆盖规则特异性更高，窄视口下仍然胜出。
- 运行中的 Vite dev server（127.0.0.1:1423）已热更新并返回新 `HomeTopbar` 模块（HTTP 200）。
- 浏览器交互验收未执行：主导航切换、标签 active 态与空态页需用户刷新后确认；窄视口标签可见性建议把窗口拖到 900px 以下在项目工作区内数标签验收。

## 2026-09-30：重建「对照阅读 + 结构调整」模块

### 背景：两项被证实的坐标错误

- `content_list.json` 与 `content_list_v2.json` 的 bbox 是 **0-1000 归一化坐标**（x 按页宽、y 按页高分别缩放），但 `PdfPage.tsx` 拿 PDF.js 的 **点单位**视口去除它。实测 `Making Transcendents` 首页标题块 bbox `[362,267,637,297]`，页面 499×709pt；`layout.json` 中同一块为 `[181,190,318,211]`，两边的比例正好是 1000/499 与 1000/709。旧覆盖层因此偏离原页。
- `_page_metadata()` 只找 `*_middle.json`，而真实精准解析输出里没有该文件，页面几何在 `layout.json` 的 `pdf_info[].page_size`。三个 library 项目的 `pages[].width/height` 因此全部是 0。

### 已实现（后端）

- `Document.bbox_space` 显式声明 bbox 单位（`mineru-1000` | `none`），旧文档缺字段时按 `mineru-1000` 解释，**不重写已有 document.json**；schema_version 从 1.1 起写入该字段。
- `_page_metadata()` 改为「优先 middle.json、回退 layout.json」；`Page.source_file` 记录实际几何来源。
- `_valid_bbox()` 只接受 4 个有限数字且 x1>x0、y1>y0 的 bbox，退化框不再进入规范图谱；块级 `format.bbox_space` 标记坐标来源。
- 结构调整新增 `set_title` 操作（正文→标题并指定层级），`apply_patch` 改为操作注册表；`set_type`/`set_title` 的 `before` 记录完整 `{type, level}`，使正文↔标题可逆。`set_level` 撤销同时恢复 `type=title`。自动应用白名单仍只有 `set_type`/`set_level`。
- 新增只读 API：`GET /projects/{id}/raw` 列出封存原始文件，`GET /projects/{id}/raw/{path}` 读取单个文件，路径强制落在 `ocr/raw/mineru/` 内。
- `/health` 增加 `structure_review_v2` 能力标记。

### 已实现（前端 `frontend/src/review/`）

- `geometry.ts`：读取 `bbox_space` 后把 bbox 换算成百分比矩形，并提供渲染前的页面高度估算。
- `usePdfDocument.ts`：一份文档只加载一次 PDF，切换项目销毁加载任务。
- `pdfAssets.ts`：统一设置 `pdfjs.GlobalWorkerOptions.workerSrc`，并给所有 `getDocument` 传入 `wasmUrl`/`iccUrl`；配套 `frontend/public/pdfjs/wasm`（JBIG2 / OpenJPEG / QCMS 解码器）与 `frontend/public/pdfjs/iccs`。修复了扫描件只显示白页的问题。
- `PdfPageCanvas.tsx`：单页 canvas（像素乘 devicePixelRatio）+ 独立绝对定位覆盖层，`pointer-events` 分层保证覆盖层可点、不挡选区。
- `PdfPane.tsx`：连续滚动、只渲染当前页附近的 canvas（默认前后 2/3 页），其余页用页面几何占位；滚动停止 160ms 后才上报当前阅读块。
- `ResultPane.tsx`：结构块 / Markdown / JSON 三个视图，块卡支持 H1-H6、降为正文、快速设为标题、Cmd/Shift 多选批量操作，可按阅读位置跟随。
- `OutlinePane.tsx`：目录树内直接升降层级与降为正文（都走可逆补丁）、目录基准、目录页候选、建议队列、修订历史（逐项与批量撤销）。
- `ReviewWorkspace.tsx`：三栏共享同一 active block；点目录/结构块/bbox 双向定位并高亮；↑↓/jk 按阅读顺序跳转；1-6 设为对应层级；在输入控件内不拦截按键。
- `SourcePreview.tsx`：非 PDF 对照策略——图片与 HTML 用带令牌的 fetch 取回后转 blob URL 内嵌；Office 类明确说明没有浏览器内渲染器并降级为文本级对照；附带 MinerU 原始输出列表与只读预览。
- `App.tsx` 只保留项目/译文/导出/设置状态，review 分支不再内联。

### 修复：扫描件原页预览白页（WASM 解码器缺失）

- **症状**：打开《The White Lotus Teachings in Chinese Religious History》的「源文件预览」，页码与 bbox 覆盖层正常，但页面本身是空白，且界面不报错。
- **根因**：该 PDF 的扫描页全部是 JBIG2（`pdfimages -list` 统计 342 个 jbig2 stencil），另一个项目「Once upon a future time」是 jbig2 + jpx。PDF.js v5.7 的 JBIG2 / JPEG2000 / QCMS 解码器已改为 WASM，必须显式提供 `wasmUrl`；旧代码只设置 `workerSrc`，`wasmUrl` 为空时 worker 拼出 `nulljbig2.wasm`，回退脚本 `nulljbig2_nowasm_fallback.js` 同样取不到，解码失败只写 `console.warn`，不会让 `getDocument` 报错，因此表现为「没有报错的白页」。解码器本就在 `node_modules/pdfjs-dist/wasm/`，但没有进入 Vite 产物；Vite 的 SPA 回退还会把不存在的 `/wasm/*` 请求返回 `index.html`，`WebAssembly.instantiate` 拿到 HTML 后必然失败。
- **修复**：新增 `frontend/public/pdfjs/`（`wasm/` 与 `iccs/`，构建时原样拷贝到站点根目录）；新增 `frontend/src/pdfAssets.ts` 统一 worker 与 `pdfAssetOptions`（`wasmUrl`/`iccUrl`，目录末尾斜杠必需，否则 `getFactoryUrlProp()` 抛 `Invalid factory url`）；`usePdfDocument.ts`、`PdfPage.tsx`、`ImportWorkspace.tsx` 改为复用该模块。

### 已验证

- Python 自动化测试：`41 passed`（新增 11 项：layout.json 页面几何、bbox_space 向后兼容、退化 bbox、阅读顺序定位、set_title 可逆与自动边界、未知操作拒绝、review API 能力标记、document bbox_space/页面几何、set_title 补丁与撤销、raw 列表与路径限制）。
- `tsc --noEmit` 通过；Vite 生产构建通过（49 modules，`dist/` 被 Git 忽略），`dist/pdfjs/wasm` 与 `dist/pdfjs/iccs` 随构建生成。
- WASM 资源在 Vite 开发服务器上实测返回正确 MIME：`/pdfjs/wasm/jbig2.wasm` → `200 application/wasm`（104852 B），`/pdfjs/wasm/openjpeg.wasm` → `200 application/wasm`（252032 B），ICC → `200`（8464 B）。浏览器内实际解码效果仍待用户刷新后确认。
- 用真实项目库启动本地 API 探测：`/health` 返回 `intake_v1` + `structure_review_v2`；某 ready 项目的 `/document` 返回 `bbox_space=mineru-1000`、81 blocks；`/raw` 列出 7 个封存文件；`/raw/<file>` 返回 200 且字节数与清单一致；`/raw/source/origin.pdf` 与 `/raw/project.json` 返回 400，缺失文件返回 404，无令牌返回 401。

### 未完成

- 本轮**没有**做浏览器交互验收，也**没有**启动完整 WebUI；新模块的交互行为只能在下次启动时由用户验收。
- 扫描件白页修复只做到「资源可达 + 构建产物存在 + `tsc`/build 通过」，尚未在浏览器里肉眼确认 JBIG2 页面真正渲染出图像。
- 旧项目的 `pages[].width/height` 仍为 0（历史数据），PdfPane 退回 PDF 首页宽高比占位；重建规范文档的入口本轮未实现。
- 表格/公式等可视化块的独立截图对照、块级滚动像素同步、非 PDF 的 Office 内嵌渲染仍未实现。

### 外部服务事实

- 本轮只对本地 API 做只读 GET 探测，没有向 MinerU 上传任何文档，也没有调用任何翻译模型。

## 2026-09-02：独立仓库与项目库迁移

### 目标与数据边界

- Literature 的唯一仓库根目录迁移为 `/Users/jiangwei/CdoexProject/Literature/`，保留原 `.git`、分支和全部未提交工作区改动，不重新初始化或覆盖历史。
- 程序源码、测试、启动脚本、计划和开发文档继续由 Git 跟踪；长期文档项目统一放入仓库内的 `library/`，并通过根 `.gitignore` 排除，避免原文、MinerU raw、译文和导出被误提交。
- 上传暂存、任务恢复状态和启动器状态继续使用 `~/Library/Application Support/Literature/`，日志继续使用 `~/Library/Logs/Literature/`，Token 继续使用 macOS Keychain。

### 已实现

- Python 默认项目库、独立 MinerU 工作流默认输出和 WebUI 启动器统一指向 `~/CdoexProject/Literature/library/`；`LITERATURE_LIBRARY` 仍可显式覆盖。
- 启动器显式把项目库路径传给 API，并移除“文稿”目录专属错误提示；项目库的读、写、枚举预检仍保留。
- README 和总体计划补充仓库/项目数据分层；macOS 应用包改为存放在新仓库的 `launcher/` 下，并通过自身位置解析仓库根目录。桌面只保留 Finder 替身，不再依赖旧源码位置或 Applications 文件夹；设置脚本支持安全重复构建并重建会跟随旧 bundle inode 的 Finder 替身。
- 迁移后发现 `.venv/bin/python`、`pyvenv.cfg`、editable 元数据和命令 shebang 仍分别绑定旧 Documents 运行时及旧源码位置，这会在 Literature 加载源码前触发 macOS“文稿”权限。旧 `.venv` 已替换；新环境使用 `~/.cache/codex-runtimes/` 的 Python 3.12.13 以 `--copies` 在新仓库重建并重新安装依赖，启动器同时增加 Documents 绑定检测作为防回归保护。
- 桌面旧实体应用和 `/Users/jiangwei/codexprogram/literature/` 包装目录已移除；活动入口只剩桌面 Finder 替身和仓库内应用包。迁移时曾将这些生成物移动到废纸篓，但最终核验时废纸篓已为空，因此不把它们记录为可恢复备份；`library/` 项目数据未受影响。
- 4 个已有 Literature 项目连同完整目录迁移到新 `library/`；21 个无活动任务引用的历史上传暂存目录移动到新项目库 `.trash/`，未永久删除。

### 已验证

- Python 自动化测试：`30 passed`。
- TypeScript 检查、Vite 生产构建和 macOS 启动脚本语法检查通过。
- 仓库内 `Literature.app` 的 `Info.plist`、JXA 反编译结果、adhoc 签名和 Finder 替身目标检查通过；从 Finder 启动后 API 运行于新仓库本地 `.venv/bin/python`，WebUI 运行于非 Documents 的 Node 解释器。
- 新 API 进程的打开文件中不存在 `/Users/jiangwei/Documents`；健康响应返回匹配的源码指纹和 `intake_items_v1`，WebUI 返回 `<title>Literature</title>`。
- 默认库路径与独立 MinerU 输出路径均解析为 `/Users/jiangwei/CdoexProject/Literature/library`；迁移后 Git 仍识别原仓库，`library/` 不出现在 Git 状态中。

### 外部服务事实

- 已保存 MinerU Token 的无副作用连接检查返回 `connected: true`。
- 本轮没有把任何用户文档发送给 MinerU；真实 PDF smoke test 仍需用户对具体文件的外发授权。

## 2026-09-01：修复 WebUI 与旧 API 进程不一致

### 根因

- 新版首页已经按文件发送 `/intake/jobs.items`，但 macOS 启动器只用固定版本号 `0.1.0` 判断 API 是否可复用，继续连接了前一天启动、尚未加载 `items` 模型的旧 Python 进程。
- 旧 Pydantic 模型忽略未知的 `items` 后，只看到空的 `upload_ids/paths`，因而返回“请选择至少一个 MinerU 支持的文件”。请求日志证明 `/intake/uploads` 已成功，400 发生在本地任务创建阶段，尚未请求 MinerU。

### 已实现

- 后端启动时对 `literature/**/*.py` 计算稳定源码指纹，`/health` 返回该指纹以及 `intake_items_v1` 能力标记。
- 启动器用当前源码计算期望指纹，只复用指纹和能力均匹配的 API；状态文件增加 API/WebUI PID 与指纹。
- 源码指纹变化时，启动器先核对已保存 PID、监听端口和进程命令，只终止自己管理的旧 Literature 服务，再在原端口启动新版 API 与 Vite。
- GUI 启动环境会实际执行候选 Python 来计算指纹；仓库 `.venv` 因 macOS 隐私边界不能运行时，自动回退到可用的 Codex Python 运行时。
- WebUI 初始化时检查 `intake_items_v1`，旧 API 会显示明确的前后端版本不一致提示。`IntakeJobCreate` 同时禁止未知字段，避免未来协议不匹配再次落入误导性的空文件错误。

### 已验证

- Python 自动化测试：`26 passed`；新增健康指纹/能力和未知 intake 字段拒绝测试。
- TypeScript 检查与 Vite 生产构建通过。
- 清理了 3 个旧 Literature API 与 4 个旧 Vite 进程；正式 `Literature.app` 已在 `41731/1423` 启动最终源码。
- 三次失败上传留下的 9 个无任务引用暂存目录（约 40MB）已移入 Literature 的 `.trash/orphaned-uploads-20260901-2037/`，未永久删除，活动上传暂存区已清空。
- 运行 API 的 OpenAPI 已确认包含 `items`，并声明 `additionalProperties: false`；健康指纹与启动器状态一致。
- 用临时后端源文件改变源码指纹后，正式启动器自动替换 API/WebUI PID；删除探针后再次自动替换回最终源码，证明旧后端不会再被复用。临时探针未保留。
- 浏览器刷新后的首页正常显示“MinerU 已连接”，无请求错误和控制台警告；本轮未选择文件、未创建解析任务，也未调用真实 MinerU。

## 2026-09-01：逐文件 MinerU 设置

### 已实现

- 从右侧批次级“更多 MinerU 设置”中移除 PDF 页码范围和额外输出格式；解析模型、语言、OCR、表格和公式仍是批次设置。
- 选择或拖入文件后自动打开逐文件设置弹窗。每行显示文件名、大小、PDF 本地读取的总页数、独立页码范围，以及 DOCX/HTML/LaTeX 额外输出选项。
- 每个 PDF 页码范围默认为空，并在界面中明确解释为空即解析全部；非 PDF 禁用页码输入，HTML 使用 MinerU-HTML 默认 Markdown/JSON 输出。
- 待处理队列显示每个文件自己的页码和额外格式摘要，并可随时重新打开设置弹窗修改。
- `/intake/jobs` 增加逐文件 `items` 请求结构，每个上传项分别携带 `page_ranges` 和 `extra_formats`。后端逐项目创建独立 MinerU 任务快照，同时保留旧版 `upload_ids`/`paths` 批量调用兼容性。

### 已验证

- Python 自动化测试：`25 passed`。新增验证同批两个 PDF 分别保存不同页码范围和不同额外输出格式；原有 HTML 自动切换 MinerU-HTML 与格式清理测试继续通过。
- TypeScript 类型检查和 Vite 生产构建通过。
- 隔离浏览器选择 3 页与 5 页 PDF 后，弹窗自动出现并正确显示总页数；分别设置 `1-2 + DOCX` 和 `2,4-5 + HTML/LATEX`，队列摘要和重新打开后的值保持独立，浏览器控制台无错误。
- 浏览器验收 API 显式禁用 Keychain Token，未点击“开始解析”，本轮没有调用真实 MinerU 服务。

## 2026-09-01：首页与文件资源流程校正

### 产品边界

- 文件上传、MinerU 参数设置和当前处理任务属于首页，是 Literature 工作流的第一步。
- “文件资源”是第二步，统一列出等待中、执行中、失败和已完成的文档任务与标准化资源；资源页本身不提供导入入口。
- 单个资源的后续处理手段留到下一轮，不在本轮提前设计或实现。

### 已实现

- WebUI 默认入口从原“文件项目”页改回首页，并将首页标题和叙述统一为“上传与处理”。
- 主导航改为“首页 → 文件资源 → 结构校对 → 阅读与翻译 → 导出”；点击 Literature 品牌返回首页，从已完成资源工作区退出则返回文件资源页。
- 首页只显示当前等待、处理中和失败待处理任务；已完成历史不再作为“已有项目”重复堆叠，提供“查看全部文件资源”入口。
- 文件资源页移除页头和空状态中的导入按钮，补充全部/处理中/等待中/失败/已完成状态概览，并关联 OCR 任务展示实时状态与阶段进度。
- 完成资源可进入结构校对；尚未生成 `document.json` 的资源只展示任务状态，不再跳回导入页。页面文案由“项目库/文件项目”统一为“文件资源/资源库”。

### 已验证

- TypeScript 类型检查和 Vite 生产构建通过。
- Python 自动化测试：`24 passed`。
- 隔离浏览器验收通过：默认首页、首页到文件资源导航、资源页无导入入口、全量状态概览、完成资源卡片、网格/列表切换、资源搜索、返回首页和浏览器控制台无警告/错误。

### 外部服务事实

- 隔离验收 API 首次启动时读取到了本机 Keychain 中已有的 MinerU Token；一份 66 字节的本地合成 HTML 验收文件在进程停止前完成一次真实 MinerU-HTML 批次（远端状态 `done`），结果仅保存在 `/private/tmp` 隔离目录。
- 发现后立即停止该 API，并以禁用 Token 的方式完成后续验收。本次只能证明该极小 HTML 夹具的上传、远端完成、下载和规范化链路，不能替代计划中的 PDF、图片、DOCX、PPTX、XLSX、HTML 代表样本 smoke test，也不把其他格式标记为真实验收。

## 2026-08-31：macOS 一键 WebUI 启动入口

### 已实现

- 增加 `scripts/start_webui.sh`：自动检查并复用 Literature API/WebUI，端口占用时在受限端口范围内选择空闲端口，等待两个服务就绪后打开默认浏览器。
- 增加并发启动锁、端口状态文件、独立日志、启动失败提示和重复点击复用；服务识别同时校验 Literature 健康响应与 HTML 标题，避免误用其他本地服务。
- 增加 macOS 启动器构建脚本和 JXA 入口，使用仓库现有 Literature 图标生成可点击应用包，保持旧 Litera 项目不变。

### 已验证

- 临时 `.app` 编译、Literature 图标、bundle 元数据和 adhoc 签名检查通过。
- 从 `.app` 启动后，API 健康检查返回 Literature `0.1.0`，WebUI 返回 `<title>Literature</title>`，浏览器打开项目库；本轮未调用 MinerU，也未上传任何文档。
- 再次点击 `.app` 后 WebUI 仍由同一个 Node PID 提供服务，证明重复点击会复用服务而不是重复启动进程。

## 2026-08-31：独立文件项目库

### 参考产品结论

- 只读检查了本机 MinerU 应用。`My Collections` 是收藏/摘录分类，不是文件项目管理器；真正对应 Literature 需求的是 `Tasks` 页面。
- MinerU `Tasks` 提供任务名搜索、模型/类型/状态/创建时间筛选、表格列、打开位置、删除和分页。Literature 复用这套信息架构，但按长期文档项目而不是一次性解析任务组织，并补充网格视图和重命名。

### 已实现

- WebUI 默认进入独立“文件项目”页面；今天完成的“导入与解析”与旧的“结构校对/阅读与翻译/导出”已接到统一主导航，不再表现为两套彼此隔离的前端。
- 项目库支持网格/列表切换、搜索项目名和原始文件名、状态和文件类型筛选，以及按修改时间、创建时间、名称或文件大小排序。
- 增加分页、每页数量和本地视图偏好记忆；项目卡片/行显示原始文件名、类型、大小、处理状态、修改时间、资源数和导出数。
- 已生成 `document.json` 的项目进入现有查看/校对工作区；尚未生成规范文档的项目回到导入任务页，避免用不完整数据打开编辑器。
- `PATCH /projects/{id}` 支持解析前后重命名。后端安全移动项目目录，处理同名冲突，并同步 `project.json` 与已有 `document.json` 标题。
- `DELETE /projects/{id}` 采用软删除：项目完整目录移动到项目库 `.trash/`，完成的 MinerU 运行目录也移出活动索引；有运行中或等待中的解析/翻译任务时返回 409，避免任务与目录竞态。
- 项目列表/详情增加 `has_document`、`asset_count`、`export_count`；本地开发 CORS 允许任意 loopback 端口，正式访问仍要求会话令牌。

### 已验证

- Python 自动化测试：`24 passed`。新增覆盖项目描述字段、解析前重命名、解析后标题同步、可恢复删除和删除后活动项目不可见。
- TypeScript 类型检查和 Vite 生产构建通过。
- 隔离项目库与本地 WebUI 浏览器验收通过：验证默认项目库、三项目网格、列表切换、关键词实时筛选、操作菜单、重命名保存、可恢复删除确认弹窗，以及项目库/导入页双向导航。
- 浏览器验收未执行最终删除按钮，也未调用真实 MinerU 服务；重命名只作用于 `/private/tmp` 中的隔离演示项目。

### 后续

- 回收站目前是数据层可恢复，尚未提供“回收站/恢复/永久删除”页面。
- 三栏查看编辑页的“原始文件—解析内容—翻译内容”重构属于后续结构校对阶段，本轮只保留既有查看页并建立正确入口。

## 2026-08-31：WebUI 优先与 MinerU 全格式导入

### 已实现

- 开发优先级从 Tauri 桌面端调整为 WebUI；已有桌面源码保留，但当前模块不再依赖 Tauri 文件路径能力。
- 使用标准 `<input type="file" multiple>` 与浏览器拖放事件建立待处理队列；点击会打开系统文件选择窗口。
- 新增 FastAPI multipart 流式上传暂存接口，项目创建后保存为 `source/origin.<原扩展名>`，不再固定为 `origin.pdf`。
- 输入格式对齐 MinerU 精准解析本地文件接口：PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX、PNG/JPG/JPEG/JP2/WEBP/GIF/BMP、HTML。
- PDF 继续自动按 200 页及 200MB 分片；非 PDF 作为完整原件交给 MinerU 转换，远端限制会保留为可操作错误。
- 前端展示并持久化 MinerU VLM/Pipeline（HTML 自动 MinerU-HTML）、全部官方语言包、OCR、表格、公式、PDF 页码范围和 DOCX/HTML/LaTeX 额外输出。
- `data_id`、轮询和结果匹配由 Literature 托管；本地文件流程不要求用户配置公网 callback/seed，也不展示只适用于 URL 的缓存参数。
- 新增根目录 `PROJECT_PLAN.md` 和 `AGENTS.md`，规定新 agent 或上下文恢复后先读取总体计划与开发日志。
- 非 PDF 项目完成后不会错误加载 PDF.js；结构校对页先提供结构块/Markdown/JSON，并明确标记原件预览仍待完善。
- 旧项目没有 `source_type` 时按旧版 PDF 项目处理，避免迁移后丢失原页预览。

### 已验证

- Python 自动化测试：`23 passed`，覆盖 Web multipart 上传、DOCX/HTML 项目、HTML 模型自动选择、任务设置快照、官方 MinerU 请求参数与非 PDF 准备流程。
- TypeScript 类型检查和 Vite 生产构建通过。
- 浏览器中实际触发系统文件选择器，确认支持多选；选择 DOCX 后文件名、大小、类型进入队列并启用“开始解析”。
- 浏览器验收完整语言包、模型、OCR/表格/公式、页码范围和额外输出设置；未点击“开始解析”，未把测试文件发送给 MinerU。

### 尚待真实服务验收

- 需要用户选取可公开测试的短 PDF、图片、DOCX、PPTX、XLSX 和 HTML，逐类验证 MinerU 在线上传、转换、轮询、下载与规范化结果。
- 真实多格式 smoke test 前不把“官方接口允许”表述为“所有格式已由真实服务跑通”。

## 2026-08-30：PDF 导入与 MinerU 分段进度工作台

### 已实现

- 将首页改为实际连接 FastAPI 的 PDF 导入与解析工作台；Tauri 支持多选和文件拖入，浏览器开发模式保留本地路径输入回退。
- 新增批量 intake API：一次提交多份 PDF，每份 PDF 独立创建 Literature 项目和持久化 MinerU 任务。
- 大型 PDF 继续使用既有 200 页上限自动分段、轮询、下载和按原始页码合并流程。
- OCR 状态接口现在返回真实总页数、已完成页数、分段总数、完成/下载/失败分段数，以及每段页码、状态、重试次数和错误。
- 前端采用渐进式披露：默认显示文档总状态；多段文档可展开详情；失败分段自动展开并提供“重试失败分段”。
- 删除误导性的 OCR 暂停 API。MinerU 远端解析阶段不显示暂停或取消；应用启动后在 Token 可用时自动恢复未完成任务的轮询和下载。
- “解析设置”标题区和页面设置按钮都可打开 MinerU Token 弹窗；Token 保存到 macOS Keychain，前端只显示“已保存”状态并允许替换，不回显完整密钥。未配置 Token 时不会把排队任务标记成正在处理。
- 第一阶段只对 PDF 声明真实支持；DOCX、PPTX 和图片需确定转换/主数据策略后再开放。

### 已验证

- Python 自动化测试：`20 passed`。
- 新增 401 页真实 PDF 回归，验证生成 200、200、1 页三个有效且无重叠的 PDF 分段。
- 新增批量 intake、格式边界、分段完成页数和“不暴露 OCR pause”接口测试。
- TypeScript 类型检查和 Vite 生产构建通过。
- `cargo check` 通过。
- 隔离环境中启动真实 FastAPI 与前端，前端成功读取后端排队任务并显示 Token、等待和任务状态。
- 浏览器验收了“设置 Token”入口、密码输入弹窗、默认禁用的空值提交按钮和页面无控制台错误；未写入假 Token，也未触发真实 MinerU 请求。

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
