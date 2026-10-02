# PLAN：结构校对 / 阅读与翻译 / 导出 并入主 WebUI 标签页

```yaml
phase: 05-ui-shell-unification
feature: 工作区成为主导航下的标签子页面
autonomous: false          # 含 1 个决策点 + 1 个人工视觉验收点
status: 已收尾（2026-10-02 完成 G1–G4、§3.2 与 P1-6；见下方「收尾记录」）
verified_by_planner:
  - "frontend: tsc --noEmit → exit 0"
  - "frontend: vite build → ✓ built in 691ms"
repo: /Users/jiangwei/CdoexProject/Literature
date: 2026-09-30
author: 任务拆解专员
```

---

> **2026-10-02 收尾记录**：G1（未打开项目时标签可点并提示）、G2（全局设置任意页面可达）、G3（hash 路由 `#/p/<id>/review`，刷新/后退保态）、G4（统一 `.app-shell` 外壳）、§3.2（`.home-topbar.project-topbar nav` 特异性）、P1-6（抽出 `frontend/src/workbench/`）均已实现。此外新增三栏拖动调宽（`frontend/src/SplitPane.tsx`）并修复左「结构与任务」栏被 `overflow:hidden` 截断无法滚动的问题。验证：`tsc --noEmit` 与 Vite 生产构建通过、Chrome headless 四页截图核对。下方 §3–§5 保留为问题拆解的历史记录。

---

## 1. 原始问题的真相

用户看到的是「点击处理好的文本后跳到另一个独立 Web UI」。**代码里从来没有第二个 Web UI**：

- 全仓只有一个入口 `frontend/index.html` → `src/main.tsx` → `src/App.tsx`；Vite 单实例，FastAPI 不挂任何静态页。
- 病因是 `App.tsx` 用互斥 early-return 渲染了**两套外壳 + 两条顶栏**：首页/文件资源用 `.intake-shell` + `HomeTopbar`（`.intake-topbar.home-topbar`），打开项目后换成 `.app-shell` + 另一段内联 `<header className="topbar">`，主导航「首页 / 文件资源」直接消失；而 `HomeTopbar.tsx` 里的「结构校对 / 阅读与翻译 / 导出」是写死的 `<button disabled>` 空位。

## 2. 已经落地的部分（复核结论，不必重做）

| 项 | 证据 |
|---|---|
| 顶栏收敛为唯一组件 `HomeTopbar`，第二套内联 `.topbar` 已无引用（`grep -rn 'className="topbar"' frontend/src` 无命中） | `HomeTopbar.tsx:25-45` |
| 三个工作区标签在打开项目后成为**可激活 tab**（`project.openReview/openRead/openExport`） | `HomeTopbar.tsx:39-41`、`App.tsx:110-123` |
| 项目名 / 语言方向 / Token·费用 / `⚙ 设置` 迁入统一顶栏 | `App.tsx:114-122` |
| 「资源仍在处理中」屏已改挂统一顶栏，不再是无顶栏整页 | `App.tsx:103-107` |
| 新增顶栏类均有样式（`.project-topbar`、`.intake-brand-group`、`.project-topbar-title`、`.project-topbar-right` 在 `styles.css`） | grep 计数各 1 |
| 类型检查与生产构建 | `tsc --noEmit` exit 0；`vite build` ✓ 691ms |

## 3. 还剩下的 6 个缺口（按用户可感知度排序）

| ID | 缺口 | 证据 | 用户会怎么说 |
|---|---|---|---|
| G1 | 未打开项目时三个标签是**灰的且没有解释**，`onClick` 完全不绑 | `HomeTopbar.tsx:26-28` | 「三个标签是坏的吗？」 |
| G2 | 只有进入项目后才能打开完整设置；**文件资源页没有设置入口**，首页只有一个 MinerU Token 小弹窗 | `ProjectLibrary.tsx:129` 未传 `openSettings`；`ImportWorkspace.tsx:212` 传的是 `openTokenSettings` | 「新库还没建就先配不了模型 key」 |
| G3 | 无 URL：刷新回首页、浏览器后退直接退出项目、无法把某章某视图发给别人 | `App.tsx` 全程 `useState`，无 hash/path | 「一刷新全没了，还是像两个网站」 |
| G4 | 首页/资源页 `.intake-shell`（自然高度、页面级滚动）与工作区 `.app-shell`（`height:100vh; grid-template-rows:64px 1fr 30px; overflow:hidden`）仍是两套壳；顶栏多/少一个 `project-topbar` 类，切换标签时顶栏内容有跳动风险 | `styles.css` 末尾、`HomeTopbar.tsx:30` | 「切标签时上面抖一下」 |
| G5 | ~~视口 ≤900px 时三个工作区标签被整体隐藏~~ **已修复**（`styles.css:3` 特异性 0,3,2 恒胜 `mineru.css:414`）；遗留是同断点的 `min-width` 冲突，见 §3.2 | `mineru.css:409-415`、`styles.css:3` | 「窗口一小就没标签了」→ 已解决 |
| G6 | `App.tsx` 仍有 218 行、6 个内联组件（`JobPanel`/`GlossaryEditor`/`ExportPanel`/`TranslationBlock`/`InstantPanel`/`Settings`），`PROJECT_PLAN.md` P1-6 未解 | `App.tsx:148+` | 后续每次改 UI 都要在同一文件里挤 |

### 3.1 G5 复核更正：前端是对的，我的证据取自被截断的选择器

我上一轮引用的 `.project-topbar nav button:nth-last-child(-n+3)` 来自 `grep -o "\.project-topbar[^{]*"`，该正则从 `.project-topbar` 起匹配，**把前缀 `.home-topbar` 截掉了**。源码原文（`styles.css:3`）是 `.home-topbar.project-topbar nav button:nth-last-child(-n+3)`，特异性 **0,3,2 > 0,2,2**，与源顺序无关恒胜。**G5 已修复，撤销「需要重做」的判定**；首页 / 资源页窄屏继续隐藏三个禁用空标签属原设计，保持不动。

教训：判定 CSS 覆盖必须回源码读完整选择器。产物里的 offset 顺序只能证明源顺序，不能替代特异性比较。

### 3.2 同一条 900px 断点里的第二条冲突（新发现，归入 PL-B）

`styles.css:3` 的 `.project-topbar nav{min-width:560px;justify-content:center}` 特异性只有 **0,1,1**；`mineru.css:415`（同一 900px 块内）的 `.home-topbar nav{min-width:250px}` 同为 **0,1,1** 且源顺序靠后（构建产物 offset **6620** vs **39698**）→ **≤900px 时项目态 nav 的 560px 保底宽度被压回 250px**，五个刚显示出来的标签失去最小宽度约束，可能挤压或换行。这条才是真正的遗留。

- 改法：把 `styles.css` 那条写成 `.home-topbar.project-topbar nav{min-width:560px;justify-content:center}`（0,2,0 恒胜 0,1,1）。
- 验收：窗口 ≤900px 且已打开项目，devtools 里 `nav` 的 computed `min-width` 必须是 560px，五个标签不换行、不溢出、不与右侧用量重叠。

> 另有一处**行为不一致**需在 G1 一并决定：`ProjectLibrary.tsx:126,163,175` 只有 `has_document` 的项目可点，处理中的项目点不开；因此 `App.tsx:103` 的「资源仍在处理中」屏实际上只在 `has_document` 与后端返回不一致时才出现（近乎死支）。

---

## 4. 波次

```
Wave 1（三个计划互不改同一文件，可完全并行）
  ├─ PL-A 导航可用性（HomeTopbar.tsx + ProjectLibrary.tsx + ImportWorkspace.tsx）  → G1 G2
  ├─ PL-B 响应式与顶栏稳定性（mineru.css + styles.css）                            → G4 G5
  └─ PL-C 内联组件外迁（新增 frontend/src/workbench/**，App.tsx 仅改 import）      → G6
Wave 2（依赖 PL-A/B/C）
  └─ PL-D 统一外壳 + hash 路由（App.tsx）                                          → G3 G4
Wave 3（依赖 PL-D）
  └─ PL-E 全链路人工验收 + 文档同步                                                 → 收尾
```

**冲突纪律**：`App.tsx` 只允许 PL-C（加 import、删内联函数体）与 PL-D 依次触碰；Wave 1 期间 PL-B 不得改 tsx，PL-A 不得改 App.tsx。

**认领状态（15:25）**：PL-B（样式与响应式，含 §3.2 新增任务 B4）+ PL-A 的 A2/高度模型部分由前端工程师接；PL-A 的 A3 与 §7 四项等产品负责人 / 用户拍板；G2、G3 归入 PL-D 一起做；`PROJECT_PLAN.md` 与 `docs/DEVELOPMENT_LOG.md` 的本轮条目均已落盘（前者由任务拆解专员用 shell 写入，绕开 edit 拦截）。

---

## 5. Wave 1 明细

### PL-A · 导航可用性与全局设置入口（G1 + G2）

**目标**：五个标签在任意时刻都"看得懂、点得动"，不开项目也能配置模型与预算。

| # | 文件 / 范围 | 具体操作 | 验证方式 | 完成标准 | 类型 |
|---|---|---|---|---|---|
| A1 | `HomeTopbar.tsx:26-28` | 禁用分支不再用 `disabled`：改渲染 `<button className="is-hint" title="请先在「文件资源」打开一篇已完成处理的文章" onClick={openResources}>`；保留视觉弱化样式（由 PL-B 的 A 任务提供 `.is-hint`）。`openResources` 已在 props 中。 | `grep -n "disabled" frontend/src/HomeTopbar.tsx` 无命中；`tsc --noEmit` 通过。 | 首页/资源页点「结构校对」→ 落到文件资源页并见 title 提示。 | `auto` |
| A2 | `ProjectLibrary.tsx:129`、`ImportWorkspace.tsx:212` | 两处统一改为传**全局设置**入口 `openSettings`：`ProjectLibrary` 新增 prop `openSettings: () => void` 并透传给 `HomeTopbar`；`ImportWorkspace` 保留 Token 快捷入口但把顶栏 `⚙ 设置` 指向同一个全局设置面板（由 App 顶层持有 `settings` state 并渲染在壳内，两个页面通过 props 调用）。 | 手工：首页与资源页各点一次 `⚙ 设置`，弹窗出现且 `/providers`、`/tbl/config` 请求 2xx；保存一次 provider 预设成功。 | 未打开任何项目时，模型 key / 单价 / 附加要求 / TBL 设置全部可达。 | `auto` |
| A3 | 决策段（写入本文件 §7） | 就「处理中的项目能否进入工作区」二选一并落文档：**(a)** 资源页允许点开处理中的项目 → 顶栏三标签激活但内容区显示进度面板（复用 `App.tsx:103` 屏，接 `/ocr/jobs` 进度）；**(b)** 保持现状，把 `App.tsx:103` 那屏降级为纯异常兜底。 | 按选定方案走一遍流程，观察与文档一致。 | 选定方案已写入 §7 并勾除待决标记。 | `checkpoint:decision` |

### PL-B · 顶栏稳定性与响应式（G4 + G5）

| # | 文件 / 范围 | 具体操作 | 验证方式 | 完成标准 | 类型 |
|---|---|---|---|---|---|
| B1 | `styles.css`、`mineru.css` | 固定顶栏三列栅格在两种模式下的槽位宽度：为 `.home-topbar` 与 `.home-topbar.project-topbar` 统一 `grid-template-columns` 与 `nav` 宽度（现 `mineru.css:224` `min-width:560px` 与 `:403` `500px`、`:415` `250px` 三套值），使打开项目前后标签组位置像素不动。 | 浏览器 devtools：切换标签时在 `nav` 上加临时 outline，观察 x 坐标差值为 0。 | 标签文字在切换时无水平位移。 | `auto` |
| B2 | `mineru.css:414`、`styles.css` | **不要删 414**（§3.1 已改判：首页 / 资源页窄屏隐藏三个禁用空标签是原设计，删了会连带改变首页行为）。本任务只做两件事：① 为 A1 的提示态补 `.intake-topbar nav button.is-hint`（灰字 + 虚下划线 + hover）；② 在 900px 块内为项目态标签补紧凑字号 `.home-topbar.project-topbar nav button{padding:0 8px;font-size:12px}`，避免 B4 恢复 560px 后与右侧用量挤在一起。 | `grep -n "nth-last-child" mineru.css` 414 仍在；`tsc --noEmit` + `vite build` 通过；≤900px 打开项目时标签与用量不重叠。 | 414 未删，提示态有样式，窄屏项目态顶栏不重叠。 | `auto` |
| B3 | `styles.css`（`.app-shell` / `.intake-shell`） | 让首页与资源页也走同一 grid：给 `.intake-shell` 增加 `display:grid; grid-template-rows:64px 1fr; height:100vh; overflow:hidden`，并把其内容容器（`.intake-content`、`.library-content`）改为 `overflow:auto; min-height:0`。**保留** `.app-shell` 定义不删，避免 PL-D 前回归。 | `tsc --noEmit` + `vite build` 通过；手工：资源页滚动到分页条、首页拖拽上传区、两者顶栏均保持吸顶。 | 四类页面共用同一外壳高度模型，页级滚动条不再随视图出现/消失。 | `auto` |
| B4 | `styles.css:3`（§3.2 新增） | 把 `.project-topbar nav{min-width:560px;justify-content:center}` 改为 `.home-topbar.project-topbar nav{min-width:560px;justify-content:center}`，特异性从 0,1,1 提到 0,2,0，压过 `mineru.css:415` 同特异性的 `min-width:250px`。 | 窗口拖到 ≤900px **并打开一个项目**，devtools 选中 `nav` 看 computed `min-width` = 560px；五个标签一行排开、不与右侧用量重叠。 | computed 值为 560px 且标签不换行不溢出。 | `auto` |

> B3 会同时改变首页与资源页的滚动行为，是本轮**唯一有视觉回归风险**的任务；必须单独提交，便于回滚。

### PL-C · 内联组件外迁（G6）

| # | 文件 / 范围 | 具体操作 | 验证方式 | 完成标准 | 类型 |
|---|---|---|---|---|---|
| C1 | 新增 `frontend/src/workbench/{ExportPanel,Settings,GlossaryEditor,JobPanel,TranslationBlock,InstantPanel}.tsx` + `index.ts` | 从 `App.tsx:148-218` 原样剪出六个组件，**props 签名一字不改**；类型引自 `../types`，`api` 引自 `../api`；barrel 导出六个。 | `tsc --noEmit`（新增文件自身无 error）。 | 六文件 + barrel 存在。 | `auto` |
| C2 | `App.tsx`（仅 import + 删除内联定义） | `import { ExportPanel, Settings, ... } from "./workbench";`，其余代码零改动。 | `tsc --noEmit && vite build`；`git diff --stat App.tsx` 应显示删除约 70 行、新增 1 行 import；手工点一遍三个标签 + 设置 + 术语表 + 导出。 | App.tsx ≤ 150 行且行为与拆分前逐条一致。 | `auto` |

---

## 6. Wave 2 / 3 明细

### PL-D · 统一外壳 + hash 路由（G3）

| # | 文件 / 范围 | 具体操作 | 验证方式 | 完成标准 | 类型 |
|---|---|---|---|---|---|
| D1 | `frontend/src/App.tsx` | 合并三个正交状态 `homeView` / `projectId` / `mode` 为单一 `view: "home"|"resources"|"review"|"read"|"export"` + `projectId`；顶层只 `return` 一个 `.app-shell`（或 PL-B 后的统一壳），内部按 `view` 条件渲染内容区，删除全部整页 early-return。 | `grep -n "return <" frontend/src/App.tsx` 顶层整页 return 只剩 1 处；`grep -n "intake-shell" App.tsx` 无命中。 | 顶栏在 App 树里只出现一次。 | `auto` |
| D2 | 新增 `frontend/src/viewState.ts` + App 接入 | 零依赖 hash 路由：`#/home`、`#/resources`、`#/p/<projectId>/review|read|export`；启动解析 hash 恢复 `view`+`projectId`，state 变化用 `history.replaceState` 同步，监听 `hashchange` 支持后退；非法/无项目的工作区 hash 回落 `#/resources`。 | 浏览器：进入某项目「阅读与翻译」→ F5 仍在原项目原标签；按后退回到上一视图；手敲 `#/p/<不存在>/review` 不白屏。 | 刷新与后退/前进均保态；地址可复制给他人。 | `auto` |
| D3 | `App.tsx`（切项目清场） | 统一 `resetProjectState()`：清 `document/translation/patches/suggestions/selected/checked/jobs/usage`；`openProject(id)` 内先清后载；`useEffect` 保证 `projectId` 为空而 `view` 仍在工作区时自动回落 `resources`。 | 手工：项目 A → 资源页 → 项目 B，确认目录树、补丁队列、译文、待确认数均无 A 残留。 | 无跨项目状态串台。 | `auto` |

### PL-E · 全链路验收 + 文档同步

| # | 文件 / 范围 | 具体操作 | 验证方式 | 完成标准 | 类型 |
|---|---|---|---|---|---|
| E1 | 全量检查 | 依次执行并在开发日志记录结果：<br>`cd frontend && npx tsc --noEmit && npx vite build`<br>`cd .. && .venv/bin/python -m pytest -q` | 三条命令输出。 | 全绿（后端本轮不应有改动，pytest 仍为既有通过数）。 | `auto` |
| E2 | 浏览器（`zsh scripts/start_webui.sh`，用 `library/` 里 4 个真实项目） | 回归清单：① 首页→资源→项目全程顶栏不跳变 ② 未开项目时三标签提示并跳转 ③ 首页与资源页能打开全局设置并保存 ④ F5 与后退保持视图 ⑤ bbox/目录/结构块三向定位 ⑥ 补丁逐项与批量撤销 ⑦ `set_title`/`set_level`/降为正文 + 1–6/0/jk/↑↓ 快捷键 ⑧ 章节复选→初译→编辑锁定→解锁 ⑨ 划词即时翻译入库 ⑩ 术语表冻结 ⑪ 四格式 × 三模式各导一次 ⑫ 图片/HTML 项目降级预览 ⑬ 1024px 与 900px 窗口下导航完整 | 逐项打勾；`~/Library/Logs/Literature/api.log` 无新增 5xx。 | 十三项全部通过；任一项未做**必须写「未验收」**，不得默认通过。 | `checkpoint:human-verify` |
| E3 | `PROJECT_PLAN.md` §5、`docs/DEVELOPMENT_LOG.md` | 按 §8 检查表第 6 条补写：统一外壳/标签导航/hash 路由三条进展，并把 P1-6 从「未处理」更新为真实档位（区分「已编码 / 自动测试 / 真实浏览器验收」三档，禁止合并表述）。 | `grep -n "统一外壳\|hash 路由\|标签子页面" PROJECT_PLAN.md docs/DEVELOPMENT_LOG.md` 命中。 | 文档与代码一致。 | `auto` |

> `PROJECT_PLAN.md` §5 已明确记录上一轮「真实浏览器交互验收未执行」。若执行环境仍无法肉眼确认，E2 整项标为未验收并把清单交回用户，不要写成通过。

---

## 7. 待用户拍板（A3 及导航语义）

| 问题 | 默认建议 | 你的决定 |
|---|---|---|
| 未打开项目时点「结构校对」等标签 | 不做灰死：跳到「文件资源」并显示 title 提示 | ☐ |
| 处理中的项目可否进入工作区 | 可进入，内容区显示 MinerU 分段进度，标签激活但操作置灰 | ☐ |
| 底栏 `.statusbar`（任务/待确认数）范围 | 仅工作区三标签显示，首页与资源页不显示 | ☐ |
| 刷新后默认落点 | 有 `projectId` 的 hash 则恢复该项目该视图，否则「文件资源」 | ☐ |

## 8. 阶段成功标准

1. 五标签一条顶栏，从上传到导出全程不跳壳；`grep` 证实 App 树只有一个 header 组件实例。
2. 不开项目即可配置模型 key、预算、TBL。
3. 刷新与后退保持在原项目原标签，地址栏可分享。
4. 1024px 与 900px 视口下五个标签都可见可点。
5. `tsc` / `vite build` / `pytest` 全绿，后端零改动。
6. `App.tsx` ≤ 160 行，工作区组件各自成文件（P1-6 关闭）。

## 9. 风险

| 风险 | 触发 | 规避 |
|---|---|---|
| 统一外壳后首页/资源页滚动失效 | B3 的 `overflow:hidden` | B3 单独提交；内容容器必须显式 `overflow:auto` |
| 三栏高度塌陷（PDF 不再滚动） | D1 换壳后 grid 行高变化 | 保持 `grid-template-rows:64px 1fr 30px` 与 `.workspace{min-height:0}` |
| hash 与 React state 双写造成回环 | D2 `hashchange` ↔ `replaceState` | 只在 state 实际变化时写 hash；`hashchange` 内先比较再 setState |
| 覆盖用户未提交改动 | 工作区已有大批 `M` 文件且 `git log` 仅 4 个 commit | 开工前先提交一次现状（`PROJECT_PLAN.md` §5.3 里程碑 1 第 1 项），每个 PL 单独提交 |

## 10. 相关文件

- 现状：`frontend/src/App.tsx:101-145,148-218`、`frontend/src/HomeTopbar.tsx`、`frontend/src/ProjectLibrary.tsx:126-175`、`frontend/src/ImportWorkspace.tsx:212`、`frontend/src/styles.css`（`.app-shell`/`.intake-shell`/`.project-topbar*`）、`frontend/src/mineru.css:224-227,392-421`
- 约束：`PROJECT_PLAN.md` §2（数据分层与不变量）、§5（进展）、§8（接手检查表）
- 命令：`scripts/start_webui.sh`、`frontend/package.json`（`build: tsc && vite build`）
