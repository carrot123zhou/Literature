# 归档文档

> 本目录只放**已被取代或已收尾**的历史文档。它们不是当前事实来源。
> 当前事实顺序：`PROJECT_PLAN.md`（目标/原则/快照/路线图）→ `docs/DEVELOPMENT_LOG.md`（逐轮事实与验收边界）→ 代码与本目录之外的模块现状文档。

归档规则（见 `AGENTS.md`）：

- 同一模块同一时刻只允许一份「现状文档」。新设计**覆盖**旧文档，不再并排新增；被覆盖的一份移入本目录并加 `STALE` 头。
- 移入时必须写清楚「被什么取代」。仅因时间过去但结论仍有效、仍在被引用的文档**不归档**。

## 清单（归档于 2026-10-14）

| 文件 | 原主题 | 归档原因 / 取代者 |
|---|---|---|
| `PROJECT.md` | 2026-09-30 标准项目文档 | 自称历史快照、仍描述已移除的导出/TBL；当前见 `PROJECT_PLAN.md` |
| `PLAN-工作区整合到主WebUI.md` | 工作区并入主 WebUI 的拆解 | 目标已收尾，缺口表自认失效；现状见代码与开发日志 |
| `STRUCTURE_REVIEW_DESIGN.md` | 2026-10-04 结构校对角色管线 | 已被 2026-10-14「目录锚定差分校准（粗校/精校）」取代 |
| `STRUCTURE_REVIEW_ACCEPTANCE.md` | 2026-10-03 结构校对验收要求 | 同上；现行验收见 `PROJECT_PLAN.md` §7 |
| `STRUCTURE_CALIBRATION_EXPERIMENTS.md` | 2026-10-03 结构校准实验 | 实验结论已沉淀为现行方案；现行见 `PROJECT_PLAN.md` §2.3 |
| `JOURNAL_STRUCTURE_REVIEW.md` | 2026-10-03 单个 Journal 作业复盘 | 一次性复盘，结论已进入通用功能与开发日志 |
| `PARSING_RESOURCE_LIFECYCLE_DESIGN.md` | 解析资源生命周期设计 | 已被解析任务历史设计取代；现行见 `PARSING_TASK_HISTORY_DESIGN.md` |
| `TRANSLATION_TASK_DIAGNOSIS_2026-10-12.md` | 翻译失败诊断 | 修复已落地；现行见 `PROJECT_PLAN.md` §2.4 与开发日志后续轮次 |
| `PROJECT_REVIEW_2026-10-12.md` | 全项目评审（时点） | 发现项已由后续修复轮次消化；后续评审见 `PROJECT_REVIEW_2026-10-13.md` |

> 注：`docs/DEVELOPMENT_LOG.md` 中的历史条目仍可能按旧路径引用本目录文件。日志是 append-only 证据层，不改写；如需查证请在本目录内按文件名查找。
