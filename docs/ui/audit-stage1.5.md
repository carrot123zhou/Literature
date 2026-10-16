# Literature UI v3 · 审计 stage 1.5（收紧启发式 + legacy 覆盖工单）

> 状态：**阶段性快照**（2026-10-15）。本轮只改 `scripts/ui_audit.py` 并新增本文件，未触碰 `frontend/src`。
> ⚠️ 采数时 `ui-shell2` 仍在重写 `shell.css` / `app-settings.css`（`styles.css` 也随之变动），因此下面所有
> **数字是本次运行的快照**；stage 2 会用同一脚本重跑并以 `audit-final.md` 为准。判定规则（脚本逻辑）是稳定的。
> 唯一设计权威：`docs/UI_DESIGN.md`。

运行：

```bash
python3 scripts/ui_audit.py                      # 人类清单（legacy 工单默认只打 actionable）
python3 scripts/ui_audit.py --worklist all       # legacy 工单连 keep 一起打
python3 scripts/ui_audit.py --json /tmp/ui-audit.json   # 完整机器摘要
```

---

## 1. 收紧后的 overflow:hidden 判定（A.5b）

之前「子树无 auto/scroll」直接列 86 个候选，绝大多数是有意为之。现在按以下**有序**规则分类
（每个 `overflow:hidden` 生效容器只落一类）：

| 顺序 | 类别 | 判定条件 | 处理 |
|---|---|---|---|
| 1 | `truncation` | 规则含 `text-overflow:ellipsis` 或 `white-space:nowrap`，或类名含 `u-truncate` | 单行省略，排除 |
| 2 | `utility` | `.u-clip` / `.u-scroll*` 设计基元 | 与滚动区配套，排除 |
| 3 | `indicator_clip` | `progress` 元素、类名以 `progress/prog/bar` 结尾，或 `height/max-height ≤ 24px` | 进度条/指标条，排除 |
| 4 | `clip_risk` | 有界容器（声明了 `height`/`max-height`/`inset` 或 `position:fixed`）**且**无任何滚动兜底 | **列入风险** |
| 5 | `rounded_clip` | 有 `border-radius`、无高度约束、无滚动兜底 | 仅圆角裁边，排除 |
| 6 | `self_healing` | 自身可纵向滚动，或子树存在滚动区 | 有兜底，排除 |
| 7 | `clip_risk` | 以上都不满足（无高度约束、无圆角、无滚动兜底） | **列入风险** |

「子树存在滚动区」的识别：选择器文本前缀匹配（`S + " " / ">" / "+" / "~"`）**或** BEM 子元素
（`.u-modal` → `.u-modal__body`）。每条 `clip_risk` 都会输出：`file:line`、选择器、命中的**原因**、
以及识别到的**兜底滚动区**列表。

### 1.1 结果对照

| | 收紧前（stage 1） | 收紧后（stage 1.5） |
|---|---|---|
| `overflow:hidden` 生效容器 | 99 | **93** |
| 候选 / 风险（§5.2） | 86（未分类） | **11**（`clip_risk`） |
| truncation | — | 49 |
| rounded_clip | — | 15 |
| indicator_clip | — | 7 |
| utility | — | 1 |
| self_healing | — | 10 |
| **clip_risk** | — | **11** |

---

## 2. 本次快照的其它数字

| # | 检查项 | stage 1 | stage 1.5（本快照） |
|---|---|---|---|
| A.1 | 硬编码色（theme.css 之外） | 519 / 214 hex | **485 / 207 hex**（styles 93、mineru 386、base 4、shell 1、resource 1） |
| A.2 | 圆角违规 | 79 | **76**（+ 滚动条圆角 2 处已标为预期保留） |
| A.3 | 字号 < 11px | 21 | **21**（styles 4、mineru 17） |
| A.4 | 间距非 4pt | 325 | **286**（Top：10px×55、6px×43、9px×29、7px×27、18px×19…） |
| A.5a | grid 固定 px ≥700px | 3 | **3**（全为 legacy） |
| A.6 | 层叠冲突选择器 | 375 | **375** |
| A.7 | 死 CSS 候选 | 88 | **72** |
| A.8 | `!important` | 14 | **8**（其中 reduced-motion 兜底 **4 处标为预期保留**） |

> 预期保留项（**不再报成违规**）：
> - `base.css` 的 4 处 `!important` —— `@media (prefers-reduced-motion: reduce)` 动效降级兜底；
> - 2 处滚动条 `::-webkit-scrollbar-thumb` 的 8px 圆角 —— 非令牌值但属预期。

---

## 3. `clip_risk` 最终清单与判断

共 11 条；经人工判断，**真正需要盯的只有 3 条**，其余是「内部滚动兜底」或「legacy 待删」。

| 选择器 | 位置 | 脚本原因 | 人工判断 |
|---|---|---|---|
| `.app-shell` | `shell.css:136` | 有界容器（`height:100vh`）无文本级内滚动 | **需盯**：主区靠子层 `.page-scroll` / `SplitPane` 内部滚动；`shell.css` 已给子项 `min-width:0` 且 `.page-scroll` `overflow-x:hidden`。收口确认四档宽度无横向即可 |
| `.review-grid` | `review/review.css:1` | 无高度约束、无内滚动 | **需盯（结构性，必改）**：列宽实际由 `SplitPane` 内联 `gridTemplateColumns` 注入，CSS 规则是 latent；legacy 970/980px 两条必须删/改 |
| `.settings.settings-v2` | `app-settings.css:14` | 有界容器（`height:min(780px,92vh)`）无文本级内滚动 | **需盯**：滚动由 `.settings-content{overflow:auto}` 承担；`app-settings.css` 已在 ≤1180 / ≤899 折叠侧栏，确认窄窗表现即可 |
| `.res-dialog` | `resource.css:656` | 有界容器无文本级内滚动 | 低：对话框主体自带滚动，内部滚动区未被文本启发式捕获 |
| `.res-dialog.export-dialog` | `workbench/export.css:3` | 同上 | 低：同上（导出对话框） |
| `.rdg-pop .dialog` | `workbench/reading.css:238` | 有界容器无文本级内滚动 | 低：`.dialog-body` 自带滚动 |
| `.rdg-root .col` | `workbench/reading.css:54` | 无高度约束、无内滚动 | 低：flex 脚手架，`min-height:0`，内容在 `.side-body` / `.blank` 滚动 |
| `.rdg-root .tblwrap` | `workbench/reading.css:203` | 无高度约束、无内滚动 | 低：表格横向包装，非纵向裁切 |
| `.model-services-dialog` | `mineru.css:418` | 有界容器无文本级内滚动 | **legacy**：已被 `app-settings.css` 覆盖，随工单删除 |
| `.settings-v2 .settings-nav` | `mineru.css:46` | 无高度约束、无内滚动 | **legacy**：已被 `app-settings.css` 覆盖，随工单删除 |
| `.reader` | `mineru.css:19` | 无高度约束、无内滚动 | **legacy 且无 TSX 引用**：随工单删除（同族 `.reader-sidebar` 已归入 truncation/待删） |

**结论：唯一的结构性裁切路径仍是 legacy 固定 px 网格三项（见 §4.2），今天靠内联/覆盖层兜住；
`.app-shell` 与 `.settings.settings-v2` 需在四档宽度截图确认；其余均为低风险或待删。**

---

## 4. legacy 覆盖图 / 可删工单（B）

脚本对 `styles.css` 与 `mineru.css` 的**每条选择器**输出：是否被后加载层覆盖、覆盖文件、
覆盖是否完整、仍在生效的属性、以及结论。

### 4.1 结论统计（本快照）

| 文件 | 可安全删除 | 部分可删 | 不可删 |
|---|---|---|---|
| `styles.css` | 21 | 20 | 56 |
| `mineru.css` | 285 | 87 | 144 |
| **合计** | **306** | **107** | **200** |

判定规则：

- 覆盖来源 = `resource → review → reading → export → theme → base → shell → intake → app-settings` 中
  声明了**同一属性**的规则；
- `override_kind = exact`：后层有**同名选择器**；`higher`：后层选择器包含本选择器的全部简单片段
  （如 `.block-card` 被 `.block-card.selected`、`.bbox` 被 `.pdf-overlay .bbox` 覆盖），即更高优先权；
- `safe_delete` = 该选择器在 legacy 里声明的属性**全部**被后层重写；
  `partial_delete` = 部分属性仍生效（工单里逐条列出）；`keep` = 后层没有覆盖。

> ⚠️ `higher` 是**文本近似**：不做完整 CSS 优先权求解。`safe_delete` 条目在工单里都附了
> 逐属性的 `覆盖 <prop> <- <file> (exact|higher)`，收口时以该行为准，可快速复核。

导出完整工单（含 `keep`）：

```bash
python3 scripts/ui_audit.py --json /tmp/ui-audit.json
# 取 details.legacy_worklist["styles.css"|"mineru.css"]
# 每条含 selector / line / verdict / declared / covered_by / override_kind / remaining / notes
```

### 4.2 Lead 已拍板的特例（已按裁定归位）

| 特例 | 归属 | 工单标记 |
|---|---|---|
| `.history-*`（30+ 选择器）同时在 `intake.css` + `app-settings.css` + `mineru.css` | **保留 `app-settings.css`** | `intake.css` 与 `mineru.css` 两份在工单 `notes` 里标「Lead 已裁定：保留 app-settings.css，本份待删」 |
| `styles.css` `.review-grid`(970px)、`mineru.css` `.review-grid`(980px)、`mineru.css` `.file-settings-head, .file-settings-table article`(760px) | 结构性裁切路径 | 工单 `notes` 标「结构性裁切路径（必删/必改）」 |
| `base.css` 4 处 `!important`（reduced-motion 兜底）、2 处滚动条 8px 圆角 | 预期保留 | 不计入 A.2 / A.8 违规（脚本内 `expected` 标记） |

注：`.file-settings-head` / `.file-settings-table article` 在本快照里已显示为 `safe_delete`
（被 `intake.css` 的 `minmax(0,…)` 弹性列覆盖），但**仍按结构性裁切路径要求删除/改写**，
不能只依赖覆盖层。

---

## 5. 脚本局限（更新）

1. `clip_risk` 的「内部滚动区」是**文本启发式**（选择器前缀 + BEM 子元素）。用变量/运行时类名
   拼出的内部滚动区、以及跨组件（如 `.settings` 与 `.settings-content`）的滚动关系不会被识别，
   所以 `clip_risk` 里的多条实际是「低风险/已兜底」，需人工判断（见 §3）。
2. legacy 工单的 `higher` 覆盖为**文本近似**，不求解完整 CSS 优先权；`safe_delete` 必须结合
   工单里的逐属性覆盖明细复核。
3. 死 CSS（A.7）仍是候选：动态类名用 `${` 前缀启发式排除（`bbox-`、`b-`、`ocr-`、`slice-`、
   `state-`、`s-`、`t-`、`res-*`…），但仍可能有漏网/误收。
4. 只解析能静态求值的字面量：`var()` / `calc()` / 百分比一律视为合规（这也让 `height:var(--sp-2)`
   这类小进度条无法按高度识别，改用类名后缀 `bar` 兜底）。
5. 颜色按出现次数统计；唯一 hex 数为 207（`theme.css` 之外）。
