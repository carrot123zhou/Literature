# Literature agent 启动约定

开始任何开发、诊断或产品设计工作前，按顺序：

1. **完整读** `PROJECT_PLAN.md`（已瘦身，约 5k token）：产品目标、已确认原则、当前状态快照、路线图和验收标准。
2. **只读最新条目**：`docs/DEVELOPMENT_LOG.md` 顶部**最新 15 个 `## ` 小节**（约 10k token）。其余历史**不要通读**；需要溯源时用 `grep`/`read` 按模块或日期定位。
3. 与本次模块直接相关的代码和测试。
4. `docs/archive/` 全是历史快照，**不是当前事实来源**；只在需要溯源时查询。

工作约定：

- 当前阶段以 Tauri macOS 客户端为优先交付面；React 工作区内嵌于客户端，浏览器仅作为开发调试入口。打包必须包含 Python sidecar，不依赖源码目录、Python、Node 或 Vite。
- MinerU 是文档解析主服务，不在 Literature 内重复实现 OCR 模型。
- `ocr/raw/mineru` 是不可变证据层；`document/document.json` 是长期主数据；译文只按稳定 block/cell ID 挂接。
- 不得把“代码已写”“自动化测试通过”和“真实外部服务已验收”混写成同一状态。

文档纪律（防止文档再次膨胀）：

- **留痕只写一处**：逐轮事实、证据与验收边界写入 `docs/DEVELOPMENT_LOG.md`；`PROJECT_PLAN.md` §5「当前开发状态」只保留 **≤30 行快照**，禁止写逐轮叙事或与日志重复的第二份副本。
- 日志新条目格式：`## YYYY-MM-DD · 主题`；正文第一行写状态（已编码 / 已自动测试 / 已隔离联调 / 已真实验收 / 未验收）。不再使用手工「第N轮」编号。
- **同一模块同一时刻只允许一份现状文档**。新设计**覆盖**旧文档：旧的移入 `docs/archive/` 并加 `STALE` 头，写明被什么取代，不得并排新增。
- 完成实质变更后，同步更新 `PROJECT_PLAN.md` 的当前状态快照和 `docs/DEVELOPMENT_LOG.md`。
- 不修改或覆盖旧 Litera 项目；Literature 独立演进。
