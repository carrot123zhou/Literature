# UI v3 legacy 瘦身候选清单（只出清单，未改动任何外观）

> 状态：**候选清单 / 未执行**。2026-10-15 由 Lead 依据 `scripts/ui_audit.py` 与
> `docs/ui/audit-stage1.5.md` 整理。本文件只做分类与判定，**不含任何代码改动**。
> 复现命令：`python3 scripts/ui_audit.py --json /tmp/ui-audit.json`（详情见 `details.legacy_worklist`）

## 0. 当前基线（2026-10-15，含五屏改造完成后）

| 指标 | 改造前 | 现在 |
|---|---|---|
| 硬编码颜色（theme.css 之外） | 589 处 / 263 hex | 485 处 / 207 hex |
| 圆角违规（非 4 档） | 93 | 76 |
| 字号 < 11px | 24 | 21 |
| 间距非 4pt 栅格 | 402 | 286 |
| `!important` | 4 | 8（其中 4 处为 base.css 的 reduced-motion 兜底，预期保留） |
| 层叠冲突选择器 | 375 | 375（未处理） |
| 新层（shell/intake/app-settings/resource/review/reading/export） | — | 颜色/圆角/字号违规 **0** |

**残留全部集中在 `mineru.css`（386 处硬编码色）与 `styles.css`（127 处）这两张历史表。**

---

## A. 纯删除 —— 外观 100% 不变（推荐第一批执行）

判定口径：**被后加载层完整覆盖**（同一批属性全部被重写），或**已无任何 TSX 引用**。
这两类删除在 CSS 层叠上不改变任何计算结果。

### A.1 `styles.css`
- `safe_delete`：**21 条**（含 `.library-shell`、`.library-grid`、`.project-card`、`.status-dot`、`.brand`、`.project-title`、`.sticky-actions`、`.reading-grid`、`.reader*`、`.export-center`、`.export-options`、`.format-grid`、`.table-placeholder`、`.icon-button`、`.pdf-stage`、`.reader-field`、`.scope-picker`、`.estimate-note`、`.stats-inline`、`.progress-track`、`.export-report`、`.reader-heading`）
  - 注：`ui-shell2` 本轮已把 16 组"确认无引用"的规则删掉（`styles.css` 45 → 30 行）。上述 21 条是重新跑审计后**仍然存在**的项，其性质是"被覆盖"而非"无引用"，需按本口径（覆盖完整）核对后才删。

### A.2 `mineru.css`
- `safe_delete`：**285 条**（占该文件 516 条选择器的 55%）

### A.3 跨层重复里的"纯副本"（零视觉变化，已裁定归属）
| 选择器组 | 保留 | 删除 | 依据 |
|---|---|---|---|
| `.history-*`（30+ 条） | `app-settings.css` | `intake.css` + `mineru.css` 各一份 | 只由 `TaskHistory.tsx` 渲染，该文件归 `ui-shell` |
| `.pdf-scroll` / `.primary` / `.app-shell` / `.eyebrow` 等 | 覆盖层 | `styles.css` / `mineru.css` 旧份 | 已在覆盖层生效 |

### A.4 结构性裁切路径（必删，删除后外观不变）
三条 legacy 固定 px 网格**当前已被覆盖**，但一旦覆盖层或断点变化就会撞上 `.app-shell{overflow:hidden}`：

| 位置 | 内容 | 处理 |
|---|---|---|
| `styles.css` L1 | `.review-grid` → `970px`（230+440+300） | 必删 |
| `mineru.css` L18 | `.review-grid` → `980px`（260+360+360） | 必删 |
| `mineru.css` L205 | `.file-settings-head, .file-settings-table article` → `760px` | 必删 |

### A.5 已无 TSX 引用的死类（脚本只列候选，逐条确认后删）
`.model-advanced`、`.model-input-row`、`.structure-options`（复数，已被 `review.css` 的 `.structure-option` 取代）、`.structure-run-status`、`.structure-run-message`、`.activity-*`（4 条）、`.existing-projects`
⚠️ **反例（不要删）**：`.type` / `.type-title` —— 由 `ResultPane` 动态生成 `type type-${block.type}`，仍在使用。

---

## B. 部分删除 —— 需逐属性核对（第二批，逐条小步）

判定口径：**部分属性仍在本层生效**，属于"覆盖层尚未接管的尾巴"。

- `styles.css`：**20 条**
- `mineru.css`：**87 条**
- 合计 **107 条**

处理方式：对每一条，把"仍在生效的属性"迁进对应覆盖层（shell / intake / app-settings / resource / review / reading / export），确认该属性在两处取值一致后再删旧声明。
**因为取值必须一致，这一步本身不改变外观**；但如果迁移时顺手换成令牌值，就会改外观 —— 那属于设计改动，不算瘦身，须单独走截图对比。

---

## C. 需你决策 —— 会改变外观（**不属于瘦身**，单独排期）

这两组是"同一选择器在两层各写了一份、生效的是后加载的那层"，切归属会改外观：

| 选择器组 | 现在生效 | 归属建议 | 切换后的可见差异 |
|---|---|---|---|
| `.structure-run-panel` / `.run-dot` / `.structure-activity` / `.structure-queue` | `app-settings.css` | `review.css`（这些类只由 `review/OutlinePane.tsx` 渲染） | 运行/暂停/失败的描边与底色（`is-paused` / `is-failed` 的红橙描边等少量差异） |

**两个选项：**
1. **保持现状**：以 `app-settings.css` 为唯一来源，删掉 `review.css` 那份 → **外观不变**，但把"结构校对的样式"留在设置层，长期是隐患。
2. **归位到 `review.css`**：删掉 `app-settings.css` 那份 → **外观会变**，必须先截图对比、确认新的观感可接受再合入。

建议走选项 2，但排在瘦身之后、单独一次改动 + 前后截图对照。

---

## D. 不可删 —— 仍在生效（**200 条**）

- `styles.css`：56 条
- `mineru.css`：144 条
- 其中 `styles.css` 仍带有 `<11px` 字号（`.bstate 10.5px`、`.lock 10px`、`.toc-row span 10px`、`.glossary-versions li small 10.5px`）—— 这些**不是瘦身项，是待修的规范违规**，要改就会改外观，须与 C 一起走截图对比。

---

## E. 顺带要修的缺陷（不是瘦身，独立列项）

五屏真实窗口走查（1024 / 1277 档）发现的遗留问题：

1. **列表表头与单元格左内边距不一致**：`文件资源` 列表所有列表头比行内内容**稳定偏左约 10px**（表头 `提取` x=598 vs 行 x=608；表头 `状态` x=992 vs 行 x=1004；表头 `资源名称` x=246 vs 行 PDF 徽标 x=254）。
2. **`文件资源`「提取」列在 1024 下截断**：`全部解析 208 页` → `全部解析 20…`，是列宽偏窄造成的信息损失。
3. **`--min-app-width: 860px` 与 `tauri.conf.json` 的 `minWidth: 1024` 不一致**：`<900` 断点（两侧栏 `display:none`）在已装客户端里**物理不可达**，属死分支；要么放宽 `minWidth`，要么把设计最小宽度改为 1024。
4. **遮罩色未走令牌**：`.modal-backdrop` 等处的 `rgba(0,0,0,.32)` 可直接换成 `theme.css` 的 `--scrim`（外观不变，属纯瘦身）。

---

## F. 建议执行顺序（每步都带验证闸门）

| 批次 | 内容 | 外观 | 验证 |
|---|---|---|---|
| 1 | A 类全部（`styles.css` 21 + `mineru.css` 285 + 副本 + 三条裁切路径 + 死类 + E.4） | **不变** | 跑 `tsc` + `vite build` 通过；`ui_audit.py` 违规数显著下降；**对同一批截图做像素级 diff（应为 0）** |
| 2 | B 类 107 条逐属性迁移 | **不变** | 同上，且逐条确认取值一致 |
| 3 | C 类归属归位 + D 类 `<11px` 修正 + E.1/E.2 | **会变** | 需你确认新观感；前后截图对照 |
| 4 | E.3 `minWidth` 决策 | 会变（窄窗可达） | 补 900 档真实窗口验收 |

**第 1 批的关键保障**：因为判定是"完整覆盖"，理论上像素 diff 应为 0；用同一窗口尺寸、同一屏幕状态下前后各截一次图，**diff 必须为 0** 才允许继续 —— 这样"删了 300 条规则"这件事本身就是可证的。
