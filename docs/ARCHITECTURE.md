# Literature 架构与不变量

## 分层

```text
PDF 输入
  |
  v
MinerU 服务适配层 --------> ocr/raw/mineru/（只读证据）
  |
  v
规范化器 -----------------> document.json（主数据）
  |                           source.md（投影）
  |                           assets/（资源）
  +----------+---------------+----------------+
             |                                |
             v                                v
      StructurePatch                    TranslationLayer
        可逆叠加                         block/cell ID 挂接
             |                                |
             +---------------+----------------+
                             v
                    阅读 / 校对 / 翻译工作区
```

> 2026-10-13：Literature 已整体移除内置多格式导出与外部 TBL 接力。原有的
> `exports/` 成品目录、原件、raw、document、translations 均保留在用户库中，
> 不会被自动清理；产品只保留“解析 → 结构校对 → 翻译”三个工作流。

## 必须长期保持的不变量

1. MinerU raw 文件在封存后不得修改；通过 `ocr/raw/manifest.json` 的 SHA-256 复核。
2. `document.json` 保存 MinerU 原始字段和资源映射，不以 Markdown 作为唯一事实来源。
3. block/cell ID 在同一份封存 raw 上重复规范化时保持稳定。
4. 结构校准只写可逆补丁；正文删除、合并和改写不得自动执行。
5. 翻译通过 block/cell ID 挂接，不把译文写回 source block。
6. 人工编辑的译文自动锁定；批量任务默认跳过锁定项。
7. 解析方式（remote/local）在任务创建时固定并落盘；不可用的解析后端不得伪装可用。
8. API key、MinerU Token 和会话令牌不得进入项目、日志或其他文件。
9. 本地 API 只绑定 loopback；除健康检查外均需会话令牌。

## 解析后端契约（2026-10-13）

`literature/parse_backends.py` 定义解析方式与工作流的边界：

- `mineru_remote`：当前唯一已实现、默认使用的解析方式，走 MinerU 精准解析 API（切片 → 预签名上传 → 远端轮询 → 下载校验 → 封存 → 规范化）。
- `mineru_local`：仅预留接入点，本版本**不安装、不内置**本地 MinerU 模型与依赖，明确标记 `available=false` 与原因，不提供可点击的伪功能。

后端能力描述区分远端与本地差异（是否切片、是否轮询、是否需要上传 URL、能否真正取消、能否断点续跑）。远端实际执行经 `RemoteMinerUBackend`（`mineru_workflow.py`）的 `prepare/execute/status/result` 边界：`run_or_resume` 先按任务持久化的 `backend_id` 校验可用性，local/未知任务在入口即被拒（不创建远端 client、不读 Token）。适配器统一产出「可封存 raw 结果 + source_pages 映射」，再走 `literature/mineru.py` 的同一 normalize/merge；旧任务缺 `backend_id` 时按 remote 读取。接入口径、部署/日志/隔离与未来验收见 `docs/LOCAL_MINERU_BACKEND.md`。

## UI 工作区

- **结构校对**：MinerU 式左/中/右联动。左侧章节与任务，中间 PDF 页及 bbox，右侧结构块、Markdown 投影和 JSON/raw。
- **阅读与翻译**：章节范围、原文、译文三栏；原页可返回；选段即时翻译可选择保存。
- **文件资源**：解析/校对/翻译状态与操作；已移除的导出不再出现在导航、卡片阶段或工作区中。
- 旧的 `#/export`、`#/p/<id>/export` 地址会兼容回退到「阅读与翻译」，不白屏。

## 客户端边界（2026-10-03）

Tauri 单实例外壳加载内置前端并启动同包 Python sidecar；动态端口与随机令牌只经环境/受限 IPC 交换。前端核对后端能力及本会话鉴权后再呈现工作区。发布态不回退开发解释器，原生导入读取本地元数据并只在确认任务时创建耐久原件副本。

Finder 定位限定资源库/运行数据/日志的真实路径，拒绝符号链接逃逸。另存使用原生对话框并禁止覆盖资源库、运行数据、日志及符号链接。退出时清理后端进程组；后端同时监测 App PID。资源库选择不自动移动文献，也不能把已有项目内部目录选为新库。资源库、raw、译文与回收站不参加构建缓存清理。
