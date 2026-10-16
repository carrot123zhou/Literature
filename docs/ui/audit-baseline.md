# Literature UI v3 · 审计基线（stage 1）

> 状态：**阶段性基线**（2026-10-15，stage 1）。
> 四个屏幕 worker 仍在改造中，本文件记录审计脚本在**首次运行**时的确定性结果；收口阶段
> （stage 2，等 Lead 指令）用同一脚本重跑，产出 `docs/ui/audit-final.md` 做回归对照。
> 唯一设计权威是 `docs/UI_DESIGN.md`：**本文件不定义规则，只记录执行结果**，供收口时对照。

---

## 0. 运行方式

```bash
# 人类可读清单
python3 scripts/ui_audit.py

# 机器可读摘要
python3 scripts/ui_audit.py --json /tmp/ui-audit.json

# 只要 JSON
python3 scripts/ui_audit.py --format json | jq .metrics

# 指定仓库根 / 调整清单长度
python3 scripts/ui_audit.py --root /path/to/repo --max-list 200
```

- 脚本：`scripts/ui_audit.py`（纯标准库、无第三方依赖、可重复运行）。
- 只读 `frontend/src`，**不修改任何源码**；输出全部排序，结果确定。
- 覆盖文件：`styles.css`、`mineru.css`、`shell.css`、`intake.css`、`app-settings.css`、
  `resource.css`、`review/review.css`、`workbench/reading.css`、`workbench/export.css`、
  `theme.css`、`base.css`；TSX 侧扫描 `frontend/src/**/*.tsx|ts`。

---

## 1. 基线数字（stage 1，2026-10-15）

| # | 检查项 | 结果 | 备注 |
|---|---|---|---|
| A.1 | 硬编码颜色（`theme.css` 之外） | **519 处 / 214 个不同 hex** | 513 处在 `styles.css`+`mineru.css` |
| A.2 | 圆角 ∉ {6,10,14,999px,50%} | **79 处** | 78 处在 `styles.css`+`mineru.css`；`base.css` 1 处为滚动条 8px |
| A.3 | `font-size` < 11px | **21 处** | 全部在 `styles.css`(4) + `mineru.css`(17) |
| A.4 | 间距非 4pt 栅格 | **325 处** | `styles.css`(73) + `mineru.css`(237) 占 310；新层多为 1–2px |
| A.5a | `grid-template-columns` 固定 px 合计 ≥ 700px | **3 条** | 全部在 legacy 层（见 §3.1） |
| A.5b | `overflow:hidden` | 99 处，其中 **86 处候选**「子树无 auto/scroll」 | 文本启发式，含省略号容器假阳性 |
| A.6 | 层叠冲突（同一选择器跨 legacy × 覆盖层） | **375 个选择器** | 309 个跨 2 文件、64 个跨 3 文件、2 个跨 4 文件 |
| A.7 | 死 CSS 候选（class 在任何 TSX 字面量中不出现） | **88 个** | 约 30 个 `u-*` 未消费、约 30 个 legacy 遗留 |
| A.8 | `!important` | **14 处** | `base.css` 4 处为 `prefers-reduced-motion` 兜底 |
| A.8 | `prefers-reduced-motion` | **存在**（`base.css`） | ✅ |

### 1.1 A.1 硬编码颜色 · 逐文件

| 文件 | 归属 | 处数 |
|---|---|---|
| `mineru.css` | legacy（Lead 收口） | 386 |
| `styles.css` | ui-shell | 127 |
| `base.css` | Lead | 4 |
| `shell.css` | ui-shell | 1 |
| `resource.css` | ui-intake | 1 |
| `theme.css` | Lead | —（唯一允许硬编码色，已排除） |
| `intake.css` / `app-settings.css` / `review.css` / `reading.css` / `export.css` | 各屏 | **0** ✅ |

> `theme.css` 自身注释里的 hex 与 RGBA 不计入；`base.css` 的 4 处是 `--shadow-overlay` 等未走令牌的
> `rgba()`（Lead 所有，收口时决定）。

### 1.2 A.2 圆角违规取值分布

| 取值 | 处数 | 取值 | 处数 |
|---|---|---|---|
| 8px | 25 | 4px | 4 |
| 7px | 25 | 2px | 2 |
| 12px | 12 | 11px | 1 |
| 9px | 7 | 13px | 2 |
| 5px | 1 | | |

### 1.3 A.4 间距非 4pt · Top 取值

| 取值 | 处数 | 取值 | 处数 | 取值 | 处数 |
|---|---|---|---|---|---|
| 10px | 60 | 5px | 21 | 1px | 8 |
| 6px | 47 | 14px | 17 | 13px | 6 |
| 9px | 30 | 3px | 15 | 22px | 5 |
| 7px | 27 | 11px | 11 | 15px / 17px | 各 5 |
| 2px | 24 | 18px | 22 | | |

> 1–2px 出现在 `base.css`（`.u-segmented` 2px、`.u-badge` 2px）、`shell.css`（`.project-topbar-title` 2px）、
> `app-settings.css`（多枚 1–2px）——属于发丝线 / 药丸内边距，是「严格 4pt 网格」的**边界策略问题**，
> 不是错位缺陷，收口时统一裁定即可。

---

## 2. 逐文件状态（新层 vs legacy）

| 文件 | 硬编码色 | 圆角违规 | <11px | 非栅格间距 |
|---|---|---|---|---|
| `styles.css`（ui-shell，待收口） | 127 | 10 | 4 | 73 |
| `mineru.css`（legacy，Lead 收口） | 386 | 68 | 17 | 237 |
| `shell.css` | 1 | 0 | 0 | 1 |
| `intake.css` | 0 | 0 | 0 | 0 |
| `app-settings.css` | 0 | 0 | 0 | 11 |
| `resource.css` | 1 | 0 | 0 | 0 |
| `review/review.css` | 0 | 0 | 0 | 0 |
| `workbench/reading.css` | 0 | 0 | 0 | 0 |
| `workbench/export.css` | 0 | 0 | 0 | 0 |
| `base.css`（Lead） | 4 | 1 | 0 | 3 |

**结论：四个屏幕覆盖层已基本完成令牌化（颜色/圆角/字号 0 违规）**；剩余技术债集中在
`styles.css` + `mineru.css` 两张 legacy 表，以及少量的发丝线间距与 `base.css` 收尾。

---

## 3. Top 问题（按风险排序）

### 3.1 高风险（结构性，收口必清）— legacy 固定 px 网格仍在

| 文件 | 选择器 | 固定 px 合计 | 现状 |
|---|---|---|---|
| `styles.css` L1 | `.review-grid` | 970px | 被 `SplitPane` **内联** `gridTemplateColumns` 覆盖 |
| `mineru.css` L18 | `.review-grid` | 980px | 同上（≥1181px 时它是 CSS 级有效规则） |
| `mineru.css` L205 | `.file-settings-head, .file-settings-table article` | 760px | 被 `intake.css` 的 `minmax(0,…)` 弹性列覆盖 |

- 这正是 `docs/UI_DESIGN.md` §1 记录过的根因规则。今天因为「内联样式 / 后置覆盖层」没有实际裁切，
  但**只要覆盖层或断点变化，就会撞上 `.app-shell{overflow:hidden}` 造成窄窗裁切**。
- 收口动作：删除/改写这 3 条 legacy 声明，而不是继续依赖覆盖。

### 3.2 高风险（一致性）— legacy 硬编码色仍在

- 519 处硬编码色中 513 处在两张 legacy 表；79 处圆角违规中 78 处同样。任何**没有被覆盖层命中的
  legacy 规则**都会以旧配色渲染，形成「同一界面两种灰/两种圆角」的观感。收口以「删 legacy 重复规则」
  为主，而非继续叠加覆盖。

### 3.3 中风险 — 同一屏三份实现（`.history-*` 等）

- A.6 中 64 个选择器跨 **3 个文件**；最集中的是解析历史页 `.history-*`（30+ 选择器）同时存在于
  `intake.css`、`app-settings.css`、`mineru.css`。加载顺序决定 `app-settings.css` 生效，`intake.css`
  与 `mineru.css` 的副本是死规则。同形态还有 `.pdf-scroll`、`.primary`、`.app-shell`、`.eyebrow`。
- 收口动作：每屏只保留一个权威文件，删除其余副本。

### 3.4 中风险 — 死 CSS 候选 88 个

- **预期未消费**（约 30 个）：`base.css` 的 `u-*` 基元（`u-btn--danger`、`u-panel`、`u-modal--sm`…），
  等各屏迁移后使用，暂不算问题。
- **真实遗留**（legacy 层）：`styles.css` 的 `.library-shell/.library-grid/.project-card/.reading-grid/
  .reader*/.pdf-stage/.progress-track/.status-dot/.sticky-actions/.export-center/.export-options/.format-grid/
  .export-report`，`mineru.css` 的 `.intake-shell/.muted/.status-step*/.current-block-action/.glossary-button`
  等。
- **组件内已无引用**：`.activity-error/-model/-success/-warning`（4 个）、`.structure-run-message/-status/
  -options`、`.model-advanced`、`.model-input-row`、`.existing-projects`。
- 脚本只输出候选，**不自动删除**；动态拼接的类名（`bbox-${type}`、`b-${state}`、`ocr-${state}`、
  `slice-${state}`、`state-${state}`、`s-${state}`、`t-${n}`、`res-*`、`export-*` 等）已通过
  前缀启发式排除。

### 3.5 低风险 — 4pt 网格的 1–2px 边界

见 §1.3；建议收口时明确「发丝线/药丸内边距豁免 1–2px」，或把它们并入 `--sp-1`。

---

## 4. 已被 `theme.css` / `base.css` 解决的历史问题（对照用）

以下是改造前的实测问题，现已由令牌层与基础层解决，**stage 2 不应再出现在 final 清单里**：

| 历史问题 | 现状 | 证据 |
|---|---|---|
| 全局只有 1 处 `:focus-visible`，键盘焦点不可见 | 统一焦点环 `--focus-ring`（Apple 蓝） | `base.css` `:focus-visible { box-shadow: var(--focus-ring) }` |
| 滚动条样式只覆盖部分面板，其余系统默认 | 全量统一细滚动条 | `base.css` `*::-webkit-scrollbar*` + `scrollbar-width` |
| `mineru.css` 的橙色径向渐晕底 | 已移除，统一 `var(--bg)` | `base.css` `body { background-image: none }` |
| `body min-width` 两处矛盾（1024px vs 860px） | 统一 `var(--min-app-width)` = 860px | `base.css` `body { min-width: var(--min-app-width) }` |
| 橄榄绿 / MinerU 橙强调色并存 | 收敛到 Apple system blue `--accent:#007AFF` | `theme.css` |
| 界面标题混用 Georgia 衬线 | 界面统一 `--font-ui`（SF/系统无衬线），衬线仅书稿正文 | `theme.css`、`base.css` `h1..h6` |
| 无 `prefers-reduced-motion` | 已加动效降级 | `base.css`（4 处 `!important`，属预期） |
| 无统一选中色 / 等宽数字 | `::selection`、`.u-num` | `base.css` |

> 注：`≤900px` 媒体查询**不是死代码**——它覆盖 `--min-app-width:860px` 到 900px 的区间，收口时保留。

---

## 5. 脚本局限（读结果前必看）

1. **A.5b「overflow:hidden 无内滚动」是文本启发式**：只看选择器字符串。BEM 子元素
   （`.u-modal` 与 `.u-modal__body`）不会互相匹配，圆角裁剪用的 `.res-panel`/`.rdg-root .block`
   也会被列为候选；结果只作人工排查入口，不可据此删除。
2. **A.7 死 CSS 是候选**：动态拼接类名、通过变量间接使用的类名无法被字面量覆盖；脚本已用
   「`${` 前缀」启发式排除常见动态类（见 §3.4），但仍可能有漏网与误收。
3. **A.6 只比较选择器字符串完全相等**：`.a` 与 `.a.b` 不算冲突；`@media` 内的同名选择器会一并计入。
4. **A.2/A.3/A.4 只解析能静态求值的字面量**：`var()` / `calc()` / 百分比一律视为合规。
5. 颜色统计按「出现次数」而非「唯一值」；唯一 hex 数为 214（`theme.css` 之外）。
