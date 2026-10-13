# 本地 MinerU 后端接入口径（预留）

日期：2026-10-13。状态：**仅预留接入点，本版本未安装、未实现本地 MinerU**。
本文件说明未来如何把本地解析接入 Literature，以及本轮已经落地的契约。它不改变
「MinerU 是文档解析主服务、raw 是不可变证据、canonical 是长期主数据」这些原则。

## 1. 本轮已落地的事实

- `literature/parse_backends.py` 定义解析后端的身份、版本、能力与不可用原因：
  - `mineru_remote`：已实现、默认。切片 → 预签名上传地址 → 远端轮询 → 下载校验 → 封存 → 规范化。
  - `mineru_local`：预留。`available=false`，`upload_url=false`、`polling=false`，
    并给出「本版本未内置本地执行环境（未安装模型、推理依赖或运行时）」的具体原因。
- `literature/mineru_workflow.py` 的 `WorkflowJob` 持久化 `backend_id` 与
  `backend_config`（不含密钥的参数快照）。旧任务缺这两个字段时按 `mineru_remote` 读取。
- `GET /parse/backends` 只读返回后端清单；`/health` 能力 `parse_backends_v1`。
- 本地后端在 `create_job` 入口即被拒绝（`ParseBackendUnavailable`），不会创建半成品任务目录，
  界面也不存在指向本地的可点击伪功能。

## 2. 未来本地实现的契约

后端需要满足 `literature/parse_backends.py` 中的 `ParseBackend` 协议：

| 阶段 | 职责 | 远端现状 | 本地应做什么 |
|---|---|---|---|
| `prepare` | 校验输入、生成后端参数快照，不发起执行 | 校验格式/语言/页码，生成切片计划 | 校验本机运行时与模型是否就绪，生成本地命令/参数快照 |
| `execute` | 开始一次解析，返回可持久化句柄 | 申请上传 URL、上传、轮询远端 | 启动本地进程/服务，返回本机句柄（不需要上传 URL） |
| `status` | 查询句柄进度 | 轮询远端任务状态 | 读取本地进程状态/日志，可即时返回 |
| `result` | 返回「可封存 raw 结果目录 + `source_pages` 映射」 | 下载并校验结果目录，按源页映射 | 输出同构结果目录与相同的 `source_pages` 映射 |

关键约束：

- 本地实现**不需要**实现远端专属的上传 URL、远端轮询语义；`polling`/`upload_url` 能力为 `false`。
- 无论本地还是远端，最终都必须产出「可封存的 MinerU 结果目录 + 明确的源页映射」，
  再走 `literature/mineru.py` 的同一套 normalize/merge，绝不绕过 canonical 与稳定 block/cell ID。
- 本地**可以**支持真正取消/暂停（`remote_job_control=true` 的差异），但不得改变任务恢复契约。
- 不得把本地结果写进 `ocr/raw/mineru` 之外的证据层，也不得覆盖既有 raw 哈希。

## 3. 部署、资源与隔离（规划口径，本轮未实现）

- **安装位置**：本地 MinerU 应作为独立进程/服务随 Literature 客户端分发或由用户单独安装，
  其模型权重与运行时放在应用数据目录（如 `~/Library/Application Support/Literature/mineru-local/`），
  **不放入 Git 仓库、不放入用户文献库**。
- **资源与日志**：模型文件、临时工作目录与日志需独立于项目库；日志进入
  `~/Library/Logs/Literature/`；推理临时文件可安全清理，raw 证据不可清理。
- **运行依赖与隔离**：本地运行时（Python/CUDA/MLX 等）与 Literature 主 sidecar 隔离，
  避免污染主进程依赖；通过明确的进程/端口契约通信，只经 loopback 与每次启动的会话令牌。
- **文档与密钥**：本地解析不需要 MinerU Token；远端 Token 仍只进 Keychain。
- **可卸载**：卸载本地后端不得删除用户 raw/canonical/译文。

## 4. 未来验收（本轮未做）

- 在干净机器上安装本地后端 → `GET /parse/backends` 中 `mineru_local.available=true`。
- 用同一份代表样本分别跑远端与本地，比较规范化后的 block/cell ID 稳定性与页映射一致性。
- 中断/重启后本地任务的恢复、取消与资源回收。
- 本地与远端结果都能通过 `literature.mineru` 的 normalize/merge 与 raw 哈希校验。

在以上验收完成前，产品状态应保持：**远端可用、本地仅预留且未安装**。
