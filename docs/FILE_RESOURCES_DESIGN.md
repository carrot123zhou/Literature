# 文件资源管理模块设计（前端 + 产品）

> 状态：**已实现（前后端打通，2026-10-11）**。本文是「文件资源管理」改造的落地依据，§8 的 checklist 已完成；实现与验收证据见 `docs/DEVELOPMENT_LOG.md` 与 `PROJECT_PLAN.md` 的 2026-10-11 条目。
>
> **更新（2026-10-13）**：导出与外部 TBL 模块已整体移除。下文凡涉及「导出」阶段/卡牌/菜单/筛选的描述已不再适用于当前产品；资源页现只有「提取 / 结构校对 / 翻译」三个阶段，旧导出入口回退到阅读与翻译。
>
> - 可交互原型：`docs/prototypes/file-resources-v4.html`（用浏览器直接打开，含模拟数据与全部交互）。
> - OpenDesign 稿件：`opendesign-projects/2542d83c-2b70-43a2-ad66-e09274e12513/design.html`。
> - 相关记忆：`project_file-resource-module-redesign.md`、`project_translation-module-build-plan.md`。
>
> 实现映射：`frontend/src/ProjectLibrary.tsx` + `frontend/src/resource.css`（按本文 §4 重写）；`literature/storage.py` 的 `structure_review_summary`；`literature/api.py` 的 `translate_stage`/`project_pipeline` 与 `/health` 的 `resource_pipeline_v1`；结构变更后经 `JobManager.refresh_translation_summary` 刷新译文摘要。以下 §7 的 `ProjectPipeline` 形状按实现微调：`pipeline` 只含 `review`/`translate`/`export`，`extract` 仍由前端轮询 `/ocr/jobs` 得到。

---

## 1. 目标

统一查看每篇文档从 **MinerU 提取 → 结构校对 → 翻译 → 导出** 的完整进度，并**直接就地推进任意一条下游工作流**，不必先进入某个工作区。

要解决的三类现状问题：

1. 卡片视觉凌乱、文件图标单薄；
2. 卡片信息太少，只显示解析状态；
3. 交互单一：点击只跳结构校对，无法在卡片把文档投入翻译，也看不到校对/翻译/导出的进展。

---

## 2. 产品模型：独立工作流（核心）

**提取（MinerU）是唯一前置门槛。** 提取完成（生成规范文档）后，下游三条工作流**相互独立**，可任意顺序与组合：

- 不校对直接导出；
- 只校对不翻译；
- 只翻译不导出；
- **先翻译后校对**：校对修改结构后，结构改动要同步到译文；已人工锁定（`locked`）的译文保留，只提示复核。

因此卡片**不使用线性流水线**，而是四个**并列的独立阶段状态**。

依赖规则：

| 阶段 | 前置条件 | 与其他阶段的关系 |
|---|---|---|
| 提取 | 无 | 门槛 |
| 结构校对 | 提取完成 | 独立 |
| 翻译 | 提取完成 | 独立，可先于校对 |
| 导出 | 提取完成 | 独立，与翻译进度无关；翻译未完成可先导出原文 |

---

## 3. 状态定义（前后端唯一事实）

每张卡的四个阶段都要能独立回答「进行到哪一步、有没有需要我处理的」。

### 3.1 提取（MinerU）

- 数据来源：`GET /ocr/jobs`（现有；前端 `ProjectLibrary.tsx` 已在轮询）。
- 状态：`queued`（排队中）/ `running`（进行中 + `stage_progress`）/ `done`（已完成）/ `failed`（失败 + 失败分段数）。
- 备注文案：页数、模型版本、分段数、耗时、失败分段原因。

### 3.2 结构校对

- 数据来源：待确认结构建议数 = `GET /projects/{id}/structure/suggestions?decision=pending` 的长度（当前 `/projects` 未聚合，见 §7 待补）。
- 状态：`todo`（未开始）/ `confirm`（待确认 N 项）/ `done`（已校对）。

### 3.3 翻译

- 数据来源：`GET /projects` 的 `translation` 摘要（已存在，来自 `translations/<locale>/summary.json`，由 `translation_stats` 生成）。
- 现有字段：`total` / `done` / `pending` / `percent` / `stale` / `stale_locked` / `stale_source` / `stale_structure` / `stale_block_ids`，以及各 `state`（`untranslated|queued|translating|translated|refined|edited|locked|failed|stale`）计数。
- 卡片状态：`none`（未开始）/ `running`（进行中 + `percent`）/ `partial`（部分完成）/ `done`（已完成）/ `failed`（失败）。**`stale` 要在卡片上显式提示**（结构变更后待复核）。
- 详情文案：目标语言、已翻译/总数、失败段数、待复核段数、术语表是否已冻结。

### 3.4 导出

- 数据来源：`project.export_count`（现有）。
- 状态：`none`（未生成）/ `done`（已生成 N 个）。

### 3.5 卡片右上「关注点」标签（派生）

按优先级取一个：`提取失败` > `翻译失败` > `解析中` > `待确认`（**不显示数量**，数量在阶段栏）> `翻译中` > `待校对` > `已完成`。

---

## 4. 前端设计规范

### 4.1 设计 token（与 Literature 现有气质一致）

```
--bg:#f3f2eb  --paper:#fffefa  --ink:#1f312a  --muted:#65716a  --faint:#93a099
--line:#dde2da  --line-strong:#ccd3c9  --soft:#f6f7f2  --soft2:#fafbf6
语义色：完成 #3f8a56 / 进行 #b97c1d / 待办 #2f6fa3 / 失败 #b8452f / 未开始 #a7b0a9
主操作按钮：底 #eaf2d6 边 #adc474 字 #3a4f1d；选中卡片边 #a9c474
标题字体 Georgia,"Noto Serif CJK SC",serif；正文系统无衬线
圆角：卡片 14 / 按钮 9 / 文件图标 11 / 弹窗 16
```

**文件类型色（图标实底 + 白字）**：`pdf #b8503f`、`docx #3f6fb8`、`pptx #c47a2c`、`xlsx #3f8a5c`、`image #7a5cc0`、`html #5f6b64`。

> 注意：文件图标是**类型色实底 + 白色字母**（不是浅底彩字）；卡片**没有底部进度条**。

### 4.2 页面结构

```
[标题区] eyebrow FILE RESOURCES + 衬线标题 + 说明
[分区标题] 概览 ────────────
[概览] 6 个可点统计卡：全部 / 解析中 / 待校对 / 翻译中 / 有失败 / 已导出
[分区标题] 资源 ────────────
[面板]
  ├ 工具栏：搜索框 · 类型筛选 · 排序 · 网格/列表切换
  ├ 摘要行：N 项资源 …… 筛选说明
  └ 内容：网格 或 列表
```

概览统计**按独立维度**筛选，一项资源可命中多个维度（因为阶段是并行的）。

### 4.3 卡片（网格）

- 头部：类型色文件图标（实底白字）+ 标题（衬线，单行省略，可挂「当前任务」小标）+ 原始文件名 + 右上状态胶囊（中性底 + 语义圆点 + 文字）。
- **阶段栏**：四格等分，带独立底色与细分隔线，每格「阶段名（小字）+ 圆点 + 值」。
- 运行中**不再画底部进度条**（已确认去掉）；进度用阶段格里的百分比表达。
- 悬停轻微抬升；当前任务卡片用 `#a9c474` 描边。

网格布局：`repeat(3,1fr)`，gap 18，≤1080px 变 2 列。

### 4.4 列表

- 使用**表格化 CSS Grid**，列头与数据行共用同一 `grid-template-columns`，严格对齐：
  `minmax(0,2.4fr) 96px 104px 92px 84px 132px 122px`
  列 = 资源名称 / 提取 / 结构校对 / 翻译 / 导出 / 更新时间 / 状态。
- 每个阶段是独立列：圆点 + 值，左侧竖线分隔。
- 行点击 → **就地展开**详情（通栏）。

### 4.5 详情：网格用弹窗，列表用就地展开（已确认）

- **网格视图点击卡片 → 居中详情弹窗**（宽 760px，max-height 86vh）：
  - 头部：图标 + 标题 + 文件名 + 状态胶囊 + 关闭；
  - 主体：四条**纵向工作流行**（图标 + 阶段名 + 状态 + 说明 + 右侧动作按钮），下面接「项目路径 / 文件大小 / 页数·更新时间」；
  - 底部：在 Finder 中显示 / 重命名 / 移出资源库 / 关闭；
  - 点遮罩、✕、Esc 关闭。
- **列表视图点击行 → 行下通栏就地展开**（内容同上，无头部/底部外壳）。
- 选择网格/列表弹窗两种呈现，是为了**网格下展开会导致位置跳动**的问题（已确认）。

### 4.6 工作流行（四条，顺序固定）

`提取（MinerU） · 结构校对 · 翻译 · 导出`

每行内容与动作（`gate = 提取已完成`）：

| 行 | 状态文案 | 说明 | 动作 |
|---|---|---|---|
| 提取 | 已完成/失败/等待中/进行中 x% | 页数、模型、分段、失败原因 | 失败→重试失败分段；进行中→查看进度；完成→查看分段 |
| 结构校对 | 等待提取 / 已完成 / N 项待确认 / 未开始 | 目录与层级；`gate=false` 时提示「提取完成后即可校对」 | 进入校对 / 查看记录 |
| 翻译 | 等待提取 / 已完成 / 失败 / 进行中 x% / 部分完成 / 未开始 | 目标语言、已翻译/总数、失败数、待复核数、术语表 | 开始/继续/重试翻译（**弹窗**）/ 阅读与翻译 / 重译… |
| 导出 | 等待提取 / 已生成 N 个 / 未生成 | 可导出原文/纯译文/双语，与翻译无关 | 进入导出 / 查看导出 |

`gate=false` 时下游三行置灰并给出「等待提取」说明。

### 4.7 翻译弹窗（每次必弹，已确认）

- 尺寸 440px；字段：目标语言、**翻译范围（每次手动选择）**、术语表。
- 范围选项按状态动态出现：全书 / 仅未翻译 / **仅待复核（stale）** / 仅重试失败 / 选定章节。
- 一行提示：校对未完成不影响翻译；结构变更会自动同步，已锁定人工译文保留。

### 4.8 其他操作

- 卡片「•••」菜单：结构校对 / 翻译… / 导出… / 在 Finder 中显示 / 重命名 / 移出资源库（危险色）。
- 移出资源库提示保留在 `.trash` 回收目录（沿用现有文案与后端 `DELETE /projects/{id}`）。

---

## 5. 交互清单

1. 概览统计卡点击 → 按该维度筛选（单选）。
2. 搜索：匹配「资源名称 + 原始文件名」。
3. 类型 / 排序筛选；网格↔列表切换（切换时收起弹窗/展开）。
4. 点卡片（网格）→ 打开详情弹窗；点行（列表）→ 就地展开。
5. 详情动作：重试失败分段、查看分段/进度、进入校对、**翻译（弹窗选范围）**、阅读与翻译、进入导出、Finder、重命名、移出。
6. 翻译弹窗：选范围 → 确认 → 入队（toast）。
7. 当前任务卡片有「当前任务」标记与描边。

---

## 6. 与现有代码的映射（改造点）

| 文件 | 现状 | 改造 |
|---|---|---|
| `frontend/src/ProjectLibrary.tsx` | `ProjectCard`/`ProjectTable`/`FileSymbol`/`Menu`/`Dialog`；点击即 `openProject` 进结构校对 | 按本文重写卡片、阶段栏、列表栅格、详情弹窗、翻译弹窗；卡片点击改为开详情，动作走回调 |
| `frontend/src/styles.css` | 压缩成一行的旧样式（`.project-card`、`.project-grid`、`.project-table`…） | 移除旧规则，按 §4 token 重写；建议独立成 `resource.css` 再 import |
| `frontend/src/types.ts` | `Project`（`state/has_document/asset_count/export_count`） | 增加 `translation` 摘要类型、`review_pending`（或 `pipeline`）字段 |
| `frontend/src/App.tsx` | `openProject` → `navigate("review", id)`；`translate` 已存在；`openWork`/`openProjectFor` | 把详情/翻译动作接到 `openProjectFor(view)`、`translate(...)`；项目列表刷新 |
| `frontend/src/viewState.ts` | hash 路由 `#/p/{id}/review|read|export` | 复用，不动 |
| `literature/storage.py` | `_describe_project` 返回 `has_document/asset_count/export_count/translation` | 见 §7 补 `review` 摘要 |
| `literature/api.py` | `GET /projects`、`/ocr/jobs`、`/structure/suggestions`、`/translations/{locale}` 均已有 | 补 `/projects` 的校对待确认数 |

**复用已有能力，不要重复造：**
- 翻译范围接口 `POST /projects/{id}/translation/jobs`（`scope` 已支持 `auto|book|untranslated|stale|failed|blocks`）；
- 译文统计 `translation_stats`（已含 stale 细项）；
- 单项目翻译并发保护（已有，重复提交返回 400）；
- 结构建议决策 `.../suggestions/decide-batch` 与补丁 `.../structure/patches`。

---

## 7. 后端数据契约（打通时需补）

`GET /projects` 目前每个项目已返回：`has_document`、`asset_count`、`export_count`、`translation`（摘要，可能为 `null`）。需要补：

1. **校对摘要**：`review: { state: "todo"|"confirm"|"done", pending: number }`
   - 由 `list_structure_suggestions(project_id, "pending")` 的长度得出；为列表性能考虑，可持久化到项目库或 `project.json`，避免逐项扫描。
2. **提取摘要（可选聚合）**：目前提取状态只能从 `GET /ocr/jobs` 拿；若希望 `/projects` 一次返回，可加 `extract: { state, progress, failed_slices, page_count }`。前端目前已在轮询 `/ocr/jobs`，可继续沿用。
3. `translation` 摘要要**在每次译文/结构变更后刷新**（`_persist_translation_summary` 已有调用点，注意补全新增路径）。

前端拿到的 `Project` 建议形状：

```ts
type ProjectPipeline = {
  extract:   { state: "queued"|"running"|"done"|"failed"; progress?: number; failed_slices?: number; note?: string };
  review:    { state: "todo"|"confirm"|"done"; pending: number };
  translate: { state: "none"|"running"|"partial"|"done"|"failed"; done: number; total: number;
               failed: number; stale: number; percent: number; locale: string; glossary_frozen: boolean };
  export:    { count: number };
};
```

> 各阶段是**独立状态**，不要在后端把项目归纳成单一 `state`（现有 `manifest.state` 仍保留为 MinerU 自身状态，不要复用为全局阶段）。

---

## 8. 实施步骤（前后端打通 checklist）

1. 后端：`/projects` 补 `review` 摘要；核对 `translation` 摘要刷新路径。
2. 前端类型：`Project` 增加 `review` / `translation` 摘要字段。
3. 前端：按 §4 重写 `ProjectLibrary.tsx` + 样式（卡片、阶段栏、列表栅格、详情弹窗、翻译弹窗）。
4. 接线：详情动作 → `openProjectFor('review'|'read'|'export')`；翻译 → 现有 `POST /translation/jobs`（范围来自弹窗）。
5. 联调：真实本地 API 用 mock 模型跑通「范围选择 → 入队 → 状态回显」；stale 提示。
6. 验收：`tsc --noEmit` + Vite 生产构建 + pytest；浏览器点击验收；再考虑重打包客户端。
7. 完成后同步 `PROJECT_PLAN.md` 当前状态与 `docs/DEVELOPMENT_LOG.md`。

---

## 9. 已知取舍 / 待定

- 概览筛选为单选（简化）；如需多选，统计卡需支持叠加。
- 目标语言：原型固定「简体中文」，实现时需接实际 locale 选择（后端 `translation` 摘要按 `target_language` 生成）。
- 列表在窄窗口下列较多，需要响应式降级策略（可隐藏「更新时间」列）。
- 「结构变更 → stale」的传播已在后端实现（`structure_revision`），前端只需正确显示 `stale`，不在本轮新增语义。
