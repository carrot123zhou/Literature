# 客户端资源管理

| 资源 | 位置 | 管理规则 |
|---|---|---|
| 程序与文档 | `literature/`、`frontend/src/`、`src-tauri/`、`docs/`、`tests/`、`scripts/` | Git 跟踪；用户已有未提交改动保留 |
| 可运行客户端 | `launcher/Literature.app` | Git 忽略；内置前端、PDF 资源与 Python 后端；可单独复制 |
| 文献项目库 | 本机现有 `library/`；新机器为 Application Support/Literature/library | 原件、raw、canonical、译文、assets、exports 和 SQLite 都属用户数据，不作为缓存删除 |
| 项目回收站 | 资源库 `.trash/` | 只做可恢复移出；不自动清空；恢复目前通过本地目录操作 |
| 全局运行数据 | `~/Library/Application Support/Literature/` | 模型服务、OCR 恢复状态等；不得整体删除（旧 `tbl.json` 已无产品用途，保留不影响运行） |
| 资源库位置 | 运行数据 `library-location.json` | 客户端首次选择兼容库并持久化；设置可选新位置，重启生效，不自动迁移 |
| 运行日志 | `~/Library/Logs/Literature/` | 可在设置中打开；不包含密钥或会话令牌 |
| API 密钥 | macOS Keychain | 不进入项目库、Git、日志或其他文件 |
| 构建依赖 | `.venv/`、`frontend/node_modules/`、lockfiles | 开发/重建使用，客户端运行不依赖；保留依赖便于后续开发 |
| 构建后端 | `src-tauri/binaries/` | Git 忽略；保留当前目标二进制及哈希清单供重建 |
| PDF 解码资源 | 锁定的 `pdfjs-dist` → Vite/Tauri 产物 | 依赖是唯一来源；WASM、ICC、字体、CMap 和许可一并生成 |

## 可重建资源清理

```bash
.venv/bin/python scripts/clean_resources.py          # 预览路径和大小
.venv/bin/python scripts/clean_resources.py --apply  # 删除确认列表
```

固定允许列表只包含 PyInstaller 临时 build/dist、Vite dist、Rust debug 与 release 中间缓存、Tauri schema、pytest 与 Python 源码缓存。保留可运行客户端、release bundle、依赖和后端构建输入。扫描排除文献库、虚拟环境、node_modules、Git、launcher；不跟随符号链接。

删除 Vite dist 后，需要先重新构建再运行 release Cargo 构建；`scripts/build_desktop.sh` 自动完成完整顺序。重建 Rust 中间产物可能增加下一次构建时间。

本次已移除未引用的 `PdfPage.tsx`、浏览器启动脚本/JXA，以及手动复制的 PDF 资源。历史开发日志和阶段设计文档保留为决策记录，不用旧状态替代当前规划。没有删除旧 Litera 或另一份旧源码目录。
