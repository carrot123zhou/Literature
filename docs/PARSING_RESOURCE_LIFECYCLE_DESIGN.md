# 解析任务生命周期与资源覆盖设计

> 2026-10-13。本文件定义 Literature「文档解析 / 文件资源」模块的产品状态、数据契约、追加解析合并规则与验收矩阵。
>
> **更新（2026-10-13）**：导出与外部 TBL 模块已整体移除，文中「校对 / 翻译 / 导出三条工作流」现为「校对 / 翻译两条独立工作流」。
> 适用代码：`literature/mineru_workflow.py`、`literature/ocr.py`、`literature/parse_queue.py`、`literature/mineru.py`、`literature/storage.py`、`literature/api.py`、`frontend/src/ImportWorkspace.tsx`、`frontend/src/ProjectLibrary.tsx`。

## 1. 三个必须分开的概念

行业模式（后台作业）要求把「任务是否运行/成功」和「业务数据是否完整」分开；MinerU 远端不提供 pause/cancel，因此本地只能提供「安全边界停止等待 + 恢复」，不能伪装抢占。

1. **任务生命周期（本次任务）**：`queued → preparing → uploading → processing → downloading → merging → completed | failed | waiting | interrupted`
   - `waiting`：用户主动在安全边界停止本地等待，远端可能仍在跑，可恢复。
   - `interrupted`：上次进程退出时任务仍在执行，本地取消等待；远端工作可能仍在，可恢复轮询/下载。
   - `completed` 只表示**这一次请求的页面范围**成功合并。
2. **全文解析覆盖（canonical 覆盖）**：`unparsed | partial | full | unknown`
   - 只统计**已经成功合并进 `document/document.json` 的源页**，不统计上传页或远端 `done` 页。
   - 非 PDF 且页数未知时为 `unknown`，不得报「全文完成」。
3. **交付完整度**：结构校对 / 翻译 / 导出三条独立工作流（既有 `pipeline` 契约），本轮不改。

## 2. 数据契约

### 2.1 `project.json`（manifest）

```jsonc
{
  "state": "created | ocr_queued | ocr_processing | ocr_waiting | ocr_failed | ready",
  "source_page_count": 300,          // 源文件总页数；null/缺失 = 未知（非 PDF 或未打开）
  "parse_coverage": {
    "state": "unparsed | partial | full | unknown",
    "source_page_count": 300,
    "covered_pages": 100,
    "ranges": [[1, 100]]             // 已合并源页区间，1-based 闭区间，升序不重叠
  }
}
```

`parse_coverage` 由合并成功后的 `document.coverage` 投影而来，`/projects` 与 `/ocr/jobs` 都直接读它。

### 2.2 `document/document.json`（长期主数据）

```jsonc
{
  "page_count": 100,                 // canonical 页数（已合并页数）
  "coverage": {
    "source_page_count": 300,
    "ranges": [[1, 100]],
    "state": "partial"
  },
  "pages": [{ "page_index": 0, "source_page_index": 0, ... }],
  "blocks": [...],                   // 顺序即阅读顺序；旧 block_id 永不改写
  "source_files": [{ "path": "ocr/raw/mineru/slice-001/...", "source_pages": [1, 100] }]
}
```

- `Page.page_index`：**等于源文件页号（0-based）**，即稀疏存在、不压缩；解释层必须同时提供 `source_page_count`。`Page.source_page_index` 与 `page_index` 一致。
- 追加解析**只新增** block：新 block_id 形如 `blk-p{源页0基:06d}-{页内序号:04d}`，与旧 `blk-{order:08d}` 命名空间不冲突；旧 block 的 `block_id`、`order`、`structure_revision`、类型/层级一律不改。

### 2.3 `ocr/raw/`（不可变证据层）

- 首次封存：`slice-001…`，`manifest.json.slices[]`。
- 追加封存：`store.append_raw_mineru` 继续 `slice-NNN` 编号，**只追加** manifest 条目；旧文件哈希与只读位不变。
- `verify_raw` 仍然校验所有历史切片，任何旧哈希变化都报错。
- `source_files[].source_pages` 记录该切片覆盖的源页，供覆盖计算与证据追溯。

### 2.4 OCR 队列 `{runtime}/ocr-jobs/queue.json`

```jsonc
{ "order": ["job-b", "job-a"] }      // 队首先执行
```

- 队列顺序即真实执行顺序（默认单文档串行执行；保留既有内部分片能力）。
- 等待任务支持置顶 / 上移 / 下移；运行任务不可被抢占。
- 每次变更原子写盘；应用重启后按该顺序恢复。
- 不在队列中的 `queued/waiting/interrupted` 任务按 `created_at` 升序补到队尾。

## 3. 继续 / 追加解析规则

1. 只有 `source_page_count` 已知（PDF）且 `coverage.state != full` 时可继续。
2. 用户可指定剩余范围（如 `101-300`、`5,7-9`），也可「解析全部剩余」。
3. 未解析页 = `[1..N] - 已覆盖区间`。请求中与已覆盖重叠的页会被**过滤**，并返回 `skipped_pages` 供前端解释；过滤后为空则报错，不创建作业。
4. 为避免远端 `page_ranges` 语义歧义并保证源页映射：解析指定/剩余页时，Literature 在本地生成只含所选页的**压缩子集 PDF**（按页序），超过 `page_limit`（默认 200）或 `max_slice_bytes` 时按页/字节二次切分，每个子集切片记录 `source_pages`。上传后 MinerU 按文件完整解析，合并时用 `source_pages` 还原真实源页号。
5. 合并（`merge_imported_project`）流程：
   - 只规范化**本次新封存的切片**；
   - 载入既有 document（无则新建）；
   - 新 block 按 `source_page_index` 插入到正确阅读位置，旧 block 原样保留；
   - 合并资源表（旧资源不丢，新资源去重）；
   - 更新 `coverage.ranges`（并集）、`page_count`、`source.md`、`toc`（由合并后 blocks 重建，层级/类型等人工结构改动随 block 保留）；
   - **只有全部成功后才写 `parse_coverage` / `state=ready`**；失败时保留原 document 与旧覆盖。
6. 同一资源永远只有一个项目；继续解析不复制成新资源。

## 4. 并发 / 幂等 / 恢复

- 统一调度锁 `_schedule_lock` 保护“选择候选 → 领取 → 从队列移除”与入队/调序/重试状态修改；调度器在锁外初选、在锁内重选并领取（领取前重查队首与停止标志），领取后从等待队列移除，不持锁跑网络。锁序固定为 `schedule_lock → job_lock/project_lock`，避免死锁。
- 同一 project 同时至多一个**活动**解析作业：`continue_parse` 在项目锁内完成“检查活动作业 + 创建”，两并发 continue 只有一个成功，另一个明确冲突。
- 生命周期操作（start/retry/reorder）在 job 锁内原子完成；start 只允许 `queued/waiting/interrupted`（不存在 404、运行中/非法状态 409）；reorder 只允许等待中的任务。
- `document.json` 的读改写只有一个写入者：追加合并与结构补丁/撤销共用 `store.document_lock(project_id)`。
- raw 封存是“复制到暂存并算哈希 → **先写 manifest** → 再移动目录”的事务；崩溃在写前无副作用，写后断开重试会因清单有记录但目录缺失而重新封存；孤儿目录会被收养，重复 source 去重。
- 共享 `_stop_event`：调度器在启动每个任务前清空；`shutdown` 设置停止并在安全边界 join 调度线程，停止后不再领取新任务。
- 旧资源迁移：`migrate_project` 有两条可信路径：①已完成、无 `page_ranges`、切片按顺序与 `source_files` 页数一一对应；②**旧 `page_ranges=1-N` 连续前缀**，需同时满足：源 hash 与创建时一致、`verify_raw` 无错、单片片与 `source_files` 页数一致、`job.page_count`/`slice.start=1,end=N`/`document.pages`/`source_files[0].page_count` 均为 N，且封存切片可核对页数证据（`content_list_v2` 长度=N、`layout.json` 的 `pdf_info` 长度=N、`*_origin.pdf` 存在时页数=N）。前缀映射下局部第 i 页即源页 i+1，不需要猜；迁移同时修正 page/block 原页索引（block_id/人工校对/译文锁定/raw 不变）。非前缀、非连续或远端原始页号不明确的旧 `page_ranges` 作业仍保守 unknown，并返回具体缺失证据的原因码。
- 无 Token：任务保持 `queued`，不消耗轮询；保存 Token 后 `resume_ocr_jobs` 按队列顺序续跑。
- 应用重启：活动中任务标记 `interrupted`（保留切片进度），队列顺序保留，Token 存在时按序恢复。

## 5. API 契约（本轮新增/变更）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/ocr/queue` | 返回 `{order:[job_id], entries:[{job_id,position,state,project_id,title,...}]}` |
| POST | `/ocr/jobs/{id}/queue` | body `{position:"top"\|"up"\|"down"}`，调序；运行中/不存在 → 409/400 |
| POST | `/ocr/jobs/{id}/start` | 入队并唤醒；不存在 → 400；运行/已排队 → 409 |
| POST | `/ocr/jobs/{id}/retry` | 运行中 → 409；否则重置失败切片并入队 |
| POST | `/projects/{id}/ocr/continue` | body `{page_ranges?:str, language?, model_version?, page_limit?}`，返回新 job + `skipped_pages` |
| GET | `/ocr/jobs`、`/projects` | 附加 `parse_coverage` / `requested_pages` / `requested_ranges` / `queue_position` / `running` |
| POST | `/projects/{id}/parse/migrate` | 按可信历史证据恢复旧资源 coverage 与原页映射；无法证实返回 reason |

## 6. 前端一致性

- 首页进度列表、资源页网格/列表/详情使用同一套状态文案：
  - 本次任务：`排队中 / 正在解析 / 等待恢复 / 已中断 / 失败 / 本次完成`
  - 全文覆盖：`未解析 / 部分解析 N/M 页 / 全部解析 N 页 / 页数未知`
- 首页进度列表：显示队列位置、上移/下移/置顶（仅等待任务）、等待/中断恢复、失败重试；`queued/failed/waiting/interrupted/completed` 都在列表中可见（已完成显示「本次完成」而非笼统「已完成」）。
- 资源页详情：解析行显示覆盖与本次进度，主按钮按状态为「继续解析 / 恢复解析 / 重试失败分段 / 检查解析范围 / 查看分段」；部分解析时阅读/校对入口可用；未知覆盖提供「检查解析范围」（接 `POST /projects/{id}/parse/migrate`），成功刷新后可继续，失败给出真实原因与人工核对下一步。
- 所有异步按钮请求期间禁用（防双击），失败保留上下文并在原位显示可执行错误；刷新后由 `/ocr/jobs` + `/ocr/queue` 恢复。

## 7. 验收矩阵

| # | 场景 | 断言 |
| --- | --- | --- |
| B1 | `_prepare` 指定非连续页 `1-3,7-9` | 生成压缩子集切片，`source_pages` 正确，无 `page_ranges` 远端字段 |
| B2 | 剩余 >200 页 | 自动切分成多个 ≤page_limit 子集，累计源页无重叠无遗漏 |
| B3 | 部分解析 100/300 | `parse_coverage.state=partial`，`covered_pages=100` |
| B4 | 继续剩余到全覆盖 | 旧 block_id / structure_revision / raw 哈希不变，新增页合并，`state=full` |
| B5 | 非连续补页 | 旧 block 未被改写，新 block 按源页插入阅读顺序 |
| B6 | 重叠请求 | 已覆盖页被过滤并回报 `skipped_pages`；无剩余时拒绝 |
| B7 | 追加 + 人工编辑/译文 | 旧译文条目、锁、结构补丁仍然有效 |
| B8 | 失败切片重试 | 只重发失败切片，成功切片不重复上传 |
| B9 | 中断上传/轮询/下载恢复 | `interrupted`→恢复后从断点继续，不重复提交已上传 |
| B10 | 队列调序 + 重启 | 顺序持久化、重启后保留、队首先执行 |
| B11 | 无 Token | 保持 queued；保存后按序恢复 |
| B12 | 防重复 | 运行中 start/retry → 409；不存在 job → 400 且不污染 running |
| B13 | 旧库未知覆盖 | 无 `coverage` 的旧 document 按「未知覆盖」处理，不误报全文完成 |
| B15 | 源页定位 | 第 5/7 页解析后 `block.page_index == 4/6`，前端校对/阅读按 source page 定位 |
| B16 | 并发 continue | 两个并发 continue 只创建一个作业，另一个明确冲突 |
| B17 | 封存恢复 | manifest 写入失败后重试成功、无阻塞孤儿、重复 source 去重、孤儿收养 |
| B18 | 旧任务/旧资源迁移 | 可信历史作业恢复映射；旧 `1-N` 前缀按页数证据恢复，其余 `page_ranges` 保守 unknown |
| B19 | document 写锁 | 合并与结构补丁并发时两者都保留 |
| B20 | 完整性校验 | 结果页数/页码不匹配、markdown 多页均明确报错，不静默接纳 |
| B21 | 队列执行顺序 | 调序后真实执行顺序一致；队列重启保留 |
| B14 | 合并失败 | 不提前写覆盖，原 document 与 raw 完整保留 |

UI 验收：真实本地 API + 隔离库 + 浏览器逐项点击（外部服务用 fake）。原生窗口 / 真实 MinerU / 真实模型另行记录，不冒充。

## 8. 已知边界

- MinerU 远端不能暂停/取消：`waiting`/`interrupted` 只是本地停止等待，远端可能继续并计费；恢复时优先沿用已上传切片，不重复提交。
- 压缩子集 PDF 会逐页 `insert_pdf`，超大文档首轮准备耗时较长，按字节/页数二次切分以控制单文件大小。
- 旧任务（无 `source_pages` / `coverage`）按保守策略：不推断覆盖为 full，除非存在明确的全量证据。
