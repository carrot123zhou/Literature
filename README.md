# Literature

<div align="center">
  <img src="docs/showcase/literature-poster.jpg" alt="Literature 宣传海报：解析、校对、翻译、导出一条可恢复、可追溯的工作流" width="720"/>
</div>

Literature 是本地优先的结构化文献客户端：MinerU 解析文档，Literature 保留不可变证据、规范文档与稳定 block/cell ID，并提供目录校对、阅读翻译、多格式导出与人工修订。

当前交付是 **Tauri macOS 客户端**。React/PDF.js 工作区和自包含 Python/FastAPI 后端都装在 `.app` 内，运行不需要浏览器、Vite、Node、Python 或源码仓库。当前本机构建为 Apple Silicon，最低 macOS 15。

## 下载

- **Windows**（x64）：从 [Releases](https://github.com/carrot123zhou/Literature/releases) 下载 `Literature_<版本>_x64-setup.exe`（NSIS 安装器，按当前用户安装，不需要管理员权限；内置 Python 后端与全部前端资源）。由 GitHub Actions 在 `windows-latest` 上构建（见 `.github/workflows/windows.yml`），打 `v*` 标签或在 Actions 页手动触发均可重新构建。
- **macOS**（Apple Silicon）：当前尚未在 Release 发布，按下方「开发与构建」在本机构建并安装到 `launcher/Literature.app`。

## 界面

| | |
|---|---|
| ![四模块总览](docs/showcase/literature-overview.jpg) | ![导入与解析](docs/showcase/literature-intro.jpg) |

左：导入、解析、结构校对、对照翻译四页合一总览；右：导入与解析设置。

## 使用

打开 `launcher/Literature.app`。首页通过系统文件选择器或拖入文件加入队列，确认逐文件设置后导入资源库并交给 MinerU。支持 PDF、Office、图片与 HTML；大型 PDF 自动分段。文件资源页可搜索、重命名、可恢复移出和在 Finder 中显示。

设置中的「本地资源与存储」可打开资源库、运行数据和日志，也可选择另一资源库（重新启动生效，原库不自动移动）。项目内原件可另存到外部位置。

## 数据与资源

```text
原件 → MinerU raw（只读封存、SHA-256）
     → document/document.json（长期主数据）+ source.md + assets/
     → translations/<locale>/translation.json（稳定 ID 挂接）
```

本机现有 `library/` 继续使用，不搬迁或清除文献。新机器默认使用 `~/Library/Application Support/Literature/library/`。资源库位置记录在应用数据目录的 `library-location.json`。应用数据与模型服务配置继续使用 `~/Library/Application Support/Literature/`，日志位于 `~/Library/Logs/Literature/`；密钥只进 Keychain。应用包和源码之外的资源不受构建缓存清理影响。

目录与清理规则见 [资源管理](docs/RESOURCE_MANAGEMENT.md)。当前验收事实见 [开发日志](docs/DEVELOPMENT_LOG.md)，数据约束见 [架构](docs/ARCHITECTURE.md)。

## 开发与构建

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,packaging]'
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend desktop:dev
```

开发客户端自动启动 `.venv` 后端；发布客户端只启动内置后端。API 使用动态 loopback 端口和每次启动的随机令牌，单实例运行，退出时回收子进程。

```bash
./scripts/build_desktop.sh
```

该脚本生成后端、构建前端与 Tauri、做本地 adhoc 签名，最后安装到 `launcher/Literature.app`。可选运行 `./scripts/setup_macos_launcher.sh` 更新桌面替身。详细流程与发布限制见 [macOS 打包](docs/PACKAGING.md)。

浏览器仅用于调试，可分别启动 API 与前端并使用同一开发令牌：

```bash
LITERATURE_SESSION_TOKEN=literature-dev .venv/bin/python -m literature.api
VITE_LITERATURE_TOKEN=literature-dev pnpm --dir frontend dev
```

## 致谢

- [MinerU](https://github.com/opendatalab/MinerU)（OpenDataLab）——Literature 的文档解析引擎。MinerU 负责将 PDF / Office / 图片 / HTML 转为带版面结构的 Markdown 与 JSON，Literature 在其不可变输出之上构建规范文档、结构校对与翻译工作流。感谢 MinerU 项目与社区的出色工作。

## 边界

- MinerU 是唯一文档解析主服务，不重复实现 OCR 模型。默认走远端 API（`mineru_remote`）；本地 MinerU 仅预留后端接入点，本版本未安装、未实现，不会显示为可点击功能。
- raw 永不被规范化或翻译改写；译文按稳定 ID 挂接。
- 内置导出（2026-10-17 重新引入）：EPUB / Word DOCX / Markdown / 分页 HTML，支持原文、译文与对照版三种内容版本，产物写入项目 `exports/` 并可在导出弹窗中管理（重命名/删除）；外部 TBL 接力保持移除。
- 旧 Litera 项目保持独立。
