# Literature agent 启动约定

开始任何开发、诊断或产品设计工作前，必须按顺序阅读：

1. `PROJECT_PLAN.md`：产品目标、已确认原则、当前状态、路线图和验收标准。
2. `docs/DEVELOPMENT_LOG.md`：已经实现、已经验证和尚未验证的事实。
3. 与本次模块直接相关的代码和测试。

工作约定：

- 当前阶段以 Tauri macOS 客户端为优先交付面；React 工作区内嵌于客户端，浏览器仅作为开发调试入口。打包必须包含 Python sidecar，不依赖源码目录、Python、Node 或 Vite。
- MinerU 是文档解析主服务，不在 Literature 内重复实现 OCR 模型。
- `ocr/raw/mineru` 是不可变证据层；`document/document.json` 是长期主数据；译文只按稳定 block/cell ID 挂接。
- 不得把“代码已写”“自动化测试通过”和“真实外部服务已验收”混写成同一状态。
- 完成实质变更后同步更新 `PROJECT_PLAN.md` 的当前状态和 `docs/DEVELOPMENT_LOG.md`。
- 不修改或覆盖旧 Litera 项目；Literature 独立演进。

