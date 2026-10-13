# 解析任务历史、首页分区与自动归档设计

> 2026-10-13（第七轮之后）。本文件定义 Literature「解析任务」在首页、独立历史入口与
> 长期保留上的产品状态、数据契约、归档规则与验收矩阵。
> 适用代码：`literature/mineru_workflow.py`、`literature/ocr.py`、
> `literature/task_history.py`、`literature/api.py`、`frontend/src/ImportWorkspace.tsx`、
> `frontend/src/TaskHistory.tsx`、`frontend/src/types.ts`、`frontend/src/viewState.ts`。
>
> 产品参考（不是行业统一标准）：Safari 下载列表把“已完成”从当前列表移出、GitHub Actions
> 运行历史按保留期归档。90 天 / 1000 条是本产品自选值。

## 1. 概念拆分

1. **任务生命周期**（本次请求）：`queued → preparing → uploading → processing →
   downloading → merging → completed | failed | waiting | interrupted`。`completed`
   只表示**本次请求的页面范围**成功合并。
2. **解析覆盖**（canonical）：`unparsed | partial | full | unknown`，只统计已合并进
   `document/document.json` 的源页。一次 `completed` 不等于全文完成。
3. **展示分区**（本文件新增，不改变 job 生命周期）：
   - 首页「当前任务」：运行中 / queued / waiting / interrupted / **未归档的 failed**。
   - 首页「最近完成」：过去 24 小时内 `completed` 的最新 3 条。
   - **解析历史**：全部任务（活动 + 已完成 + 失败 + 已归档），可搜索/筛选/分页。
4. **归档（archive）**：仅是**展示/保留期元数据**，不是状态变更，也不删除任何数据。

## 2. 数据契约

### 2.1 `WorkflowJob`（`{runtime}/ocr-jobs/<job_id>/job.json`）

新增字段（向后兼容，缺失时取默认值；`SCHEMA_VERSION` 不变）：

```jsonc
{
  "finished_at": "2026-10-13T12:00:00+00:00"   // 首次进入 completed 的时间；"" 表示未知
}
```

- `finished_at` 只在任务**首次**进入 `COMPLETED` 时写入，之后任何 `save_job`
  （包括完成后的清理、快照）都不得覆盖。
- 旧任务没有 `finished_at`：展示与排序回退到 `updated_at`，并在 API 输出
  `finished_at_estimated: true` 标注“兼容依据”，绝不把 `created_at` 当完成时间。
- 归档**不写 job.json**，避免污染 `updated_at`/`finished_at`。

### 2.2 归档元数据 `{runtime}/ocr-jobs/archive.json`

独立持久化，与 job.json 解耦：

```jsonc
{
  "settings": { "retention_days": 90, "max_records": 1000 },  // retention_days=null 表示永久
  "last_auto_run_at": "2026-10-13T12:00:00+00:00",
  "records": {
    "<job_id>": { "archived_at": "2026-10-13T12:00:00+00:00", "reason": "retention", "auto": true }
  }
}
```

- `reason ∈ retention | capacity | manual`；`auto` 表示是否由自动规则触发。
- 记录写入幂等：同一 job 重复归档保留首次 `archived_at`。
- 取消归档只移除 records 条目，不动 job/bridge/raw/canonical/translation/exports。

### 2.3 API 输出附加字段

`GET /ocr/jobs`、`GET /ocr/tasks`、`GET /ocr/jobs/{id}` 的每个 job 附带：

```jsonc
{
  "finished_at": "...", "finished_at_estimated": false,
  "archived": false, "archived_at": "", "archive_reason": ""
}
```

保留既有 `/ocr/jobs` 全量契约（含已归档，资源页“最新 job”不因归档丢失）。

## 3. 分区与分页规则

### 3.1 首页「当前任务」（scope=active）

- 包含：`queued/preparing/uploading/processing/downloading/merging/waiting/interrupted`
  与 `failed && !archived`。
- 排序：运行中优先（`updated_at` 倒序）→ `queued` 按真实 `queue_position` 升序
  （无位置最后，再按 `created_at`）→ `waiting/interrupted`（`updated_at` 倒序）→
  `failed`（`updated_at` 倒序）。
- **失败不受 3 条限制**；每页 10 条；返回精确计数
  `{running, queued, resumable, failed, total}`。
- 空态：无任何当前任务时显示明确空态。

### 3.2 首页「最近完成」（scope=recent_completed）

- 包含：`completed && !archived && finished_at >= now - 24h`。
- 排序：`finished_at` 倒序，同时间用 `created_at` 倒序、再 `job_id` 倒序保证稳定。
- 最多 3 条；展示紧凑文件名 / 本次范围 / 完成时间 / 「查看资源」；
  **不再渲染满格进度条**；分段按需展开（`<details>`）。
- 「收起已完成」把**当前已显示的 job_ids** 持久化到 localStorage 并隐藏它们；
  未来新完成的任务仍会出现（不是“永久隐藏已完成”）。

### 3.3 解析历史（scope=history）

- 路由 `#/history`，可刷新恢复；首页任务区按钮与「查看全部文件资源 →」并存。
- 支持：名称搜索（title / source_name 子串、大小写不敏感）、状态筛选
  （全部/活动/排队/运行/待恢复/已完成/失败/已归档）、日期排序（最近/最早）、
  每页 20 条。
- 每行显示：文件名、状态、完成/更新时间、**本次范围与解析页数**、
  **资源当前覆盖**、动作（查看详情 / 打开资源 / 归档或取消归档 / 失败重试）。
- 首次解析与继续解析是**不同的 job**，都各自记录。

## 4. 归档规则

### 4.1 自动归档

- 成功记录 = `completed`。默认保留期 90 天；常驻（未归档）成功记录上限 1000 条。
- 超期或超量（**最老优先**）转已归档，原因 `retention` / `capacity`。
- 保留期可选 30 / 90 / 180 / 永久。永久取消时间限制，但 1000 条上限仍在。
- 触发：应用启动一次；运行中每天最多一次（持久化 `last_auto_run_at`，重启幂等）；
  每次有新任务完成后额外执行**超量**检查。
- **未解决 failed 及所有活动/可恢复任务绝不自动归档。**
- 已归档仍可搜索/查看；任务与证据（job/bridge/raw/迁移证据）永久保留，不做物理删除。

### 4.2 手动归档 / 取消

- `failed` 可手动「归档此记录」从首页待处理移出；可「取消归档」找回并重试。
- `completed` 可手动归档，行为与自动归档一致（`reason=manual`）。
- `queued/waiting/interrupted/running` **不能**手动归档（返回 400）。
- 归档不改变 `job.state`。重试/恢复不要求已归档任务先取消归档，但取消归档会清标记；
  对已归档 failed 触发重试时自动清除归档标记，保证状态一致。
- 归档永不删除 runtime job / bridge / raw / canonical / translation / exports；
  `migrate_project` 继续按完整历史证据工作。

## 5. API 契约

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/ocr/tasks` | `scope=active\|recent_completed\|history`，`status`、`query`、`sort`、`page`、`page_size`；返回 `{items,total,page,page_size,counts}` |
| GET | `/ocr/jobs/{id}` | 单任务详情（含归档/完成元数据） |
| POST | `/ocr/jobs/{id}/archive` | 手动归档；仅 completed / failed；活动状态 400；幂等 |
| POST | `/ocr/jobs/{id}/unarchive` | 取消归档；幂等 |
| GET | `/ocr/archive-settings` | 返回 `{retention_days, max_records}` |
| PUT | `/ocr/archive-settings` | 设置保留期（30/90/180/null）；`max_records` 默认 1000 |
| GET | `/ocr/jobs`（既有） | 全量任务，附加归档/完成元数据（消费契约不变） |

自动归档只在启动 / 每日一次 / 完成后触发，**不放进每次轮询的 GET**。

## 6. 验收矩阵

| # | 场景 | 断言 |
| --- | --- | --- |
| H1 | 首页只展示最近 3 条 completed | `recent_completed` 最多 3 条，按 finished_at 倒序 |
| H2 | 24h 边界 | `finished_at = now-24h+ε` 在内；`now-24h-ε` 在外 |
| H3 | 同完成时间稳定排序 | 相同 finished_at 时 created_at/job_id 决定稳定顺序 |
| H4 | 失败超过 3 条全部可达 | active 分区 failed 无 3 条限制，分页可翻到全部 |
| H5 | 90 天超期归档 | 老于 90 天的 completed 自动 `retention` 归档 |
| H6 | 1000 超量归档 | 未归档 completed > 1000 时最老 `capacity` 归档 |
| H7 | 永久保留 | `retention_days=null` 不按时间归档，但超量仍归档 |
| H8 | 启动/每日幂等 | 同一天第二次 `auto_archive` 跳过；重启后读持久化时间 |
| H9 | 活动拒绝归档 | queued/running/waiting/interrupted 手动归档 400 |
| H10 | 手动归档/取消/重试 | failed 手动归档后从 active 移出、history 可见；unarchive 找回；重试清标记 |
| H11 | 旧无 finished_at 兼容 | completed 无 finished_at → 用 updated_at 且 `finished_at_estimated=true` |
| H12 | 归档不破坏证据 | 归档后 `verify_raw`、raw 哈希、canonical、`migrate_project` 结果不变 |
| H13 | history 分页过滤 | query/status 过滤后 total 正确、分页边界正确 |
| H14 | 本次 vs 全书 | 记录 `requested_ranges`/`this_task_pages` 与 `parse_coverage`，不写成全文完成 |

UI 验收：隔离真实 API + fake MinerU + 真实浏览器点击首页 / 历史 / 设置 / 失败重试 /
归档找回 / 最近完成收起并刷新 / 分页 / 窄屏；外部服务一律 fake。

## 7. 已知边界

- `finished_at` 只对新完成的任务准确；旧任务按 `updated_at` 近似并标注。
- 自动归档不删除任何文件；磁盘占用仍由 raw/canonical 决定（另行设计）。
- 真实 MinerU / 真实 provider / 原生窗口点击 / 公证不在本文件验收范围。
