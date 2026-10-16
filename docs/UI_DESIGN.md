# Literature UI v3 设计规范

> 状态：**已冻结共享契约**（2026-10-15）。本文件是界面的唯一设计权威。
> 任何界面改动先读本文件；与本文冲突的旧样式视为待迁移的技术债。
> 目标：苹果式设计美感与简洁性（Apple HIG 对齐），**零视觉错位、零边界溢出**。

---

## 0. 本次改造的既定决策

| 项 | 决定 | 依据 |
|---|---|---|
| 设计语言 | Apple HIG 对齐，界面统一 SF / 系统无衬线 | 用户 2026-10-15 |
| 强调色 | **Apple system blue `#007AFF`**（暗色 `#0A84FF`）；橄榄绿与 MinerU 橙全部废弃 | 用户 2026-10-15 |
| 衬线字体 | **只用于书稿正文**，界面标题一律无衬线 | Apple 的界面语言是单一 SF |
| 范围 | 全量落地：设计规范 → 基础设施 → 逐屏改造 → 验收 | 用户 2026-10-15 |
| 暗色模式 | 令牌已预置（`<html data-theme="dark">`），本轮不启用、不验收 | 避免半成品 |

---

## 1. 改造前的实测问题（作为验收基线）

1. **三套并存的视觉体系**：`styles.css`（橄榄绿）、`mineru.css`（橙）、`resource/reading/export.css`（第三套）。实测 **294 个不同 hex**、**18 种 border-radius**、4 处 `!important`。
2. **层叠顺序反了**：`main.tsx` 把 `import App` 放在 CSS 之前，导致组件级 CSS 先于 `styles.css`/`mineru.css` 载入，`mineru.css` 反向覆盖较新的界面样式。**已修复**（见 §2）。
3. **溢出/错位**：`.review-grid` 三列最小宽合计 ≈1020px，而外壳 `overflow:hidden` → 窄窗被裁且无法滚动；`body min-width` 两处互相矛盾（1024px vs 860px）；`.home-topbar nav` 固定 `min-width:560px`；`.res-lrow` 固定列合计 630px。
4. **可读性**：界面大量 9px / 9.5px / 10px 字号；Georgia 衬线用于品牌名、h1/h2、卡片与对话框标题。
5. **交互一致性**：全局仅 1 处 `:focus-visible`；按钮 ≥3 种风格；弹层 4 套实现；设置面板 2 套实现；动效 6 种时长、无 `prefers-reduced-motion`。

---

## 2. 共享契约（已由 Lead 冻结，worker 不得修改）

### 2.1 CSS 载入顺序（`frontend/src/main.tsx`，**任何人不得改动**）

```
styles → mineru → resource → review → reading → export
       → theme → base → shell → intake → app-settings
```

顺序即层叠优先级。**新增覆盖一律写进 `shell.css` / `intake.css` / `app-settings.css`，
不要在 `styles.css`、`mineru.css` 里追加规则。** 组件内的重复 `import "./x.css"` 由 bundler 去重，位置以 `main.tsx` 为准。

### 2.2 文件所有权（禁止跨文件改动）

| 文件 | 归属 worker |
|---|---|
| `theme.css`、`base.css`、`main.tsx`、本文件、`PROJECT_PLAN.md`、`docs/DEVELOPMENT_LOG.md` | **Lead（冻结，有问题回报 Lead）** |
| `styles.css`、`shell.css`、`app-settings.css` | `ui-shell` |
| `intake.css`、`resource.css` | `ui-intake` |
| `review/review.css` | `ui-review` |
| `workbench/reading.css`、`workbench/export.css` | `ui-reading` |

TSX 归属同上表对应的屏幕组件（见各 worker 任务书）。**`App.tsx` / `HomeTopbar.tsx` / `SplitPane.tsx` 归 `ui-shell` 独占**，其他 worker 需要改这三个文件时回报 Lead。

---

## 3. 令牌（`theme.css`）——只允许用这些值

- **颜色**：`--accent` `--accent-hover` `--accent-press` `--accent-tint` `--accent-tint-strong` `--accent-ring` `--on-accent`；文本 `--label / --label-2 / --label-3 / --label-4`；背景 `--bg / --bg-elevated / --bg-secondary / --bg-tertiary / --bg-sunken`；填充 `--fill-1/2/3`；分隔 `--separator / --separator-strong`；状态 `--system-*` 与浅底 `--tint-* / --on-*`（green/orange/red/blue/purple/teal/gray）。
- **字体**：`--font-ui`（界面）、`--font-text`（**仅**书稿正文）、`--font-mono`。
- **字阶**：`--fs-caption2 11` `--fs-caption1 12` `--fs-footnote 13` `--fs-subhead 14` `--fs-body 15` `--fs-title3 17` `--fs-title2 20` `--fs-title1 24` `--fs-large 30`。**界面禁止 < 11px**。
- **间距**：`--sp-1..9` = 4/8/12/16/20/24/32/40/48。**禁止出现其它间距值。**
- **圆角**：`--r-control 6` `--r-card 10` `--r-sheet 14` `--r-pill 999`。**禁止出现其它圆角值。**
- **阴影**：只有 `--shadow-card` 与 `--shadow-overlay` 两档 + 1px 发丝线。**禁止彩色描边与硬边框**（除发丝线与表单控件边框）。
- **材质**：`--material-bg` + `--material-blur`（顶栏 / 侧栏 / 工具栏）。
- **动效**：只有 `--dur-fast .14s` / `--dur .2s` / `--dur-slow .3s` + `--ease`。

## 4. 组件基元（`base.css`，新代码一律使用 `u-*` 类）

`u-btn`（`--primary` `--tinted` `--plain` `--danger` `--sm` `--lg` `--block` `--icon`）、`u-input` / `u-select` / `u-textarea` / `u-field` / `u-check`、`u-segmented`（tab 与档位统一用它）、`u-card` / `u-panel` / `u-panel__head` / `u-panel__body`、`u-toolbar`、`u-backdrop` / `u-modal`（`--sm` `--lg`）/ `u-modal__head|body|foot|close`、`u-sheet`（右侧抽屉）、`u-badge`（`--done --run --act --fail --stale --lock --plain`）、`u-empty`、`u-skeleton`、`u-spinner`、`u-toast`、`u-scroll` / `u-scroll-y` / `u-clip` / `u-truncate` / `u-num` / `u-row` / `u-col` / `u-grow` / `u-spacer`。

> 迁移策略：**不强制改类名**。优先在你的覆盖层里把旧选择器的取值换成令牌；
> 当某块本该复用基元时（按钮/输入/弹层/分段控件/徽标），可以给 TSX 追加 `u-*` 类，
> 但**不得改变 DOM 结构、事件、文案与业务行为**。

---

## 5. 防溢出硬规则（验收必查）

1. 所有多列 grid 的**每一列都要能收缩**：用 `minmax(0, …)`，禁止 `1fr` 与固定 px 混排把容器撑破。
2. 任何 `overflow: hidden` 的容器**必须**内含可滚动区（`u-scroll` / `u-scroll-y`）；否则改为 `auto`。
   这是 `.review-grid` 在窄窗被裁死的根因。
3. 文本容器一律 `min-width: 0` + `u-truncate` / `overflow-wrap: anywhere`。
4. 外壳内**不允许出现横向滚动条**（除明确设计的横向列表）。窄窗靠布局塌陷解决，不靠裁剪。
5. 断点只保留 3 档，且与 `--min-app-width: 860px` 一致：

| 断点 | 行为 |
|---|---|
| `≥ 1180px` | 完整三栏 |
| `900–1180px` | 侧栏收窄；必要时折叠为可切换抽屉 |
| `< 900px` | 单列/主栏优先，侧栏改抽屉或隐藏（**不得**靠 `overflow:hidden` 裁掉） |

6. 固定定位元素（`.u-toast`、即时翻译浮层）必须避开状态栏与安全区，不得压住可点控件。

---

## 6. 验收标准

**每屏（worker 自检）**
- `export PATH="$REPO/.tools/node/bin:$PATH"; cd frontend && ./node_modules/.bin/tsc --noEmit -p tsconfig.json`
- `./node_modules/.bin/vite build --outDir dist-check-<label> --emptyOutDir` 后再 `rm -rf dist-check-<label>`（避免并发写 `dist/`）
- 自检 860 / 1024 / 1280 / 1440 四档宽度下**无横向滚动、无元素被裁、无文字溢出**。
- 本文件 §3 的令牌约束 100% 满足：该屏 CSS 中不新增任何硬编码 hex / 圆角 / 间距。

**全局（Lead 收口）**
- 删除被覆盖的历史规则（`styles.css` / `mineru.css` 瘦身），全仓 hex 数与圆角种类显著下降。
- 生产构建通过；客户端重新打包并安装。
- 真实窗口截图对照四档宽度。

---

## 7. 本轮不做

- 不做暗色模式 UI（只保留令牌）。
- 不改任何业务流程、API 调用、数据模型与文案语义。
- 不引入新依赖（不装 UI 框架、不装 Tailwind）。
- 不做签名/公证与发布流程改动。
