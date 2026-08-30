# Literature

Literature 是一个本地优先的结构化文献处理工作台：以 MinerU 结果为不可变来源，保留页面、bbox、标题、表格、图片、脚注和阅读顺序；在稳定 block ID 上叠加目录校对、全书/章节翻译、即时翻译、版本与导出。

## 唯一数据主线

```text
原始 PDF / Office / 图片 / HTML
  → MinerU 原始输出（只读封存，仅作为溯源证据）
  → 规范化 + 目录结构校准
  → document.json + source.md + assets（唯一长期主数据）
  → 编辑 / 提取 / 原生结构化翻译层
  → EPUB / DOCX / HTML / MD+JSON 派生导出，或交给外部 TBL
```

EPUB、DOCX 和 HTML 成品绝不由源文件直接转换。`source.md` 是 `document.json` 的可读投影；编辑、翻译、检索准备和全部导出均以规范文档图谱及其资源为输入。

## 当前实现

- Web 多文件拖拽/系统文件选择、multipart 上传，以及真实后端任务创建与状态轮询
- 对齐 MinerU 精准解析的 PDF、Word、PPT、Excel、图片和 HTML 输入类型
- 完整呈现本地文件接口可用的模型、语言包、OCR、表格、公式、页码范围和额外输出设置
- 独立项目库与 SQLite 任务/修订/用量账本
- MinerU `content_list_v2.json`、`content_list.json` 与 Markdown 导入
- 原始 OCR 封存、哈希验证、规范化文档图谱与稳定 block/cell ID
- 可逆结构补丁、0.97 高置信自动修正边界
- 可审计的 AI 目录校对输入（目录、标题候选、bbox 与相邻类型；不含正文）
- OpenAI、DeepSeek、智谱 GLM、Qwen/百炼和自定义兼容端点
- 全书、章节、block、即时选段共用翻译引擎；人工译文自动锁定
- 术语表冻结、任务暂停/恢复/失败分块重试、逐次 token 明细与预算暂停
- HTML、Markdown+JSON、EPUB 3、DOCX 导出，以及 TBL 外部接力清单
- FastAPI 本地服务与 React/PDF.js 三工作区 WebUI；Tauri 桌面版源码暂时保留但暂停推进
- 大型 PDF 自动按最多 200 页分段；默认展示文档总状态，可展开查看各分段页码、状态和错误

开发状态与可接续事项见 [开发日志](docs/DEVELOPMENT_LOG.md)，数据不变量见 [架构说明](docs/ARCHITECTURE.md)，发布流程见 [macOS 打包](docs/PACKAGING.md)。

## 开发启动

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

当前优先开发 WebUI。后端与前端两个终端必须使用相同的开发会话令牌：

```bash
LITERATURE_SESSION_TOKEN=literature-dev .venv/bin/python -m literature.api
VITE_LITERATURE_TOKEN=literature-dev pnpm --dir frontend dev
```

默认 API 为 `http://127.0.0.1:41731`。项目数据默认保存在 `~/Documents/Literature/`；API key 保存在 macOS Keychain，不写入项目文件。

## 设计边界

- MinerU 是首版唯一 OCR 服务商；Literature 不实现 OCR 推理。
- `ocr/raw/mineru/` 永不被规范化或翻译流程修改。
- `document/document.json` 是长期结构主数据，Markdown 是派生视图。
- 所有导出器只读取规范文档图谱、译文层和 `assets/`；源文件只供 MinerU 输入与人工对照。
- TBL 仍以独立进程工作，只接收 Literature 导出的 EPUB/DOCX，不回写 block 图谱。
- 首版不生成 PDF，不翻译图片内部文字。
