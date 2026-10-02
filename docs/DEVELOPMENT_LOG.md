# Literature 开发日志

> 本日志记录可验证事实、架构决策和仍需外部条件完成的验收。更新代码时应同步更新本文，避免把“已编码”“已测试”和“已用真实服务验证”混为一谈。

## 2026-10-02：可拖拽三栏、面板内滚动与跨页任务导航

### 已实现

- `frontend/src/viewState.ts`：零依赖 hash 路由（`#/home`、`#/resources`、`#/p/<id>/review|read|export`）。刷新、后退/前进、复制地址均保持同一项目与视图；`location.hash` 是唯一写入点，`hashchange` 是状态唯一来源。
- 项目上下文与视图解耦：`literature.lastProject` 记忆最近打开的项目，浏览首页/文件资源时顶栏仍显示项目与三个工作区标签；被移出资源库的项目会自动清理记忆。
- `HomeTopbar` 工作区标签始终可点：未打开项目时跳转文件资源并提示（关闭 PLAN G1）；设置入口在全部页面可用，未打开项目时也能配置模型密钥、单价、附加要求、MinerU Token 与 TBL（关闭 G2）。
- 五个页面并入同一 `.app-shell` 外壳与滚动模型，不再切换 `.intake-shell`/`.app-shell`（关闭 G4）。
- 修复左「结构与任务」栏无法滚动：`.review-grid > .sidebar{overflow:hidden}`（0,2,0）压过 `.mineru-control-column{overflow-y:auto}`（0,1,0）。改为常驻标题 + `.pane-scroll` 独立滚动；阅读页同上。
- `frontend/src/SplitPane.tsx`：结构校对与阅读翻译三栏可拖动调宽，带最小宽度夹取、双击/Home 复位、`←/→` 微调，比例持久化到 localStorage。
- 抽出 `frontend/src/workbench/`（Settings、ExportPanel、GlossaryEditor、JobPanel、TranslationBlock、InstantPanel、ReadingWorkspace），`App.tsx` 内联组件清零（关闭 P1-6 结构部分）。
- 修正 `styles.css` 的 `.project-topbar nav{min-width:560px}` 为 `.home-topbar.project-topbar nav`（特异性 0,2,0）并让工作区标签在 ≤900px 保持可见。
- toast 6 秒自动消失；设置抽屉 Esc 关闭；资源卡片标记「当前任务」并提供「继续任务 →」。

### 已验证

- `frontend`：`tsc --noEmit` 通过；Vite 生产构建通过。
- Chrome headless 对 `#/home`、`#/resources`、`#/p/<id>/review`、`#/p/<id>/read` 截图核对：顶栏统一、未开项目时标签为提示态、左栏可滚动、三栏分隔条可见、阅读页左栏固定底栏。

### 未验证

- 真实鼠标拖动分隔条、双击复位与键盘微调未经手工操作验收。
- ≤900px 窄视口只做了 CSS 特异性修正，未肉眼回归。
- 本轮未改动后端；`pytest` 41 passed 无回归。

## 2026-09-30：统一主导航外壳（校对 / 翻译 / 导出整合为主 WebUI 标签子页面）

### 背景

- 首页与文件资源页共用 `HomeTopbar` 的 `intake-topbar` 主导航（首页 → 文件资源 → 结构校对 → 阅读与翻译 → 导出，未打开项目时后三个标签禁用）；但打开处理好的资源后，工作区切换到 `styles.css` 里另一套 `app-shell` + `topbar` 顶栏（独立品牌栏与样式），视觉与交互上像跳转到了独立 Web UI。

### 已实现（前端）

- `HomeTopbar.tsx` 扩展为唯一页面外壳：新增 `project` 导航参数；打开项目时「结构校对 / 阅读与翻译 / 导出」成为同一主导航下的可激活标签子页面（active 态沿用 `intake-topbar nav button.active`），未打开项目时保持禁用；`ImportWorkspace` 与 `ProjectLibrary` 现有调用不变。
- `App.tsx` 打开项目后（含 document 未生成的「资源仍在处理中」空态）改用统一 HomeTopbar：导航「首页」回到上传页、「文件资源」回到资源页；用量（Token · 费用 / 预算）与设置入口保留在顶栏右侧；项目标题与语言对移到品牌旁；底部 statusbar 与三块工作区内容不变。
- 移除旧的独立 `.topbar` 顶栏渲染；`styles.css` 中旧顶栏样式暂保留未清理。
- `styles.css` 新增 `project-topbar` 修饰样式：品牌+标题组、顶栏右侧用量+设置、nav 最小宽度；其中 `.home-topbar.project-topbar nav button:nth-last-child(-n+3)` 的特异性（0,3,2）高于 `mineru.css` 900px 媒体查询里的隐藏规则（0,2,2），项目工作区在窄视口下 5 个标签仍然全部可见（媒体查询不改变特异性，与源顺序无关）。`mineru.css` 未改动，首页/资源页窄屏隐藏禁用标签的原设计保持不变。

### 已验证

- `tsc --noEmit` 通过；Vite 生产构建通过（`dist/pdfjs` wasm/icc 资源随构建生成）；pytest `41 passed` 无回归。
- 构建产物 `dist/assets/index-Cq7q3FqB.css` 中两条 nth-last-child 规则分别在 offset 6759 与 39640，项目顶栏覆盖规则特异性更高，窄视口下仍然胜出。
- 运行中的 Vite dev server（127.0.0.1:1423）已热更新并返回新 `HomeTopbar` 模块（HTTP 200）。
- 浏览器交互验收未执行：主导航切换、标签 active 态与空态页需用户刷新后确认；窄视口标签可见性建议把窗口拖到 900px 以下在项目工作区内数标签验收。

## 2026-09-30：重建「对照阅读 + 结构调整」模块

### 背景：两项被证实的坐标错误

- `content_list.json` 与 `content_list_v2.json` 的 bbox 是 **0-1000 归一化坐标**（x 按页宽、y 按页高分别缩放），但 `PdfPage.tsx` 拿 PDF.js 的 **点单位**视口去除它。实测 `Making Transcendents` 首页标题块 bbox `[362,267,637,297]`，页面 499×709pt；`layout.json` 中同一块为 `[181,190,318,211]`，两边的比例正好是 1000/499 与 1000/709。旧覆盖层因此偏离原页。
- `_page_metadata()` 只找 `*_middle.json`，而真实精准解析输出里没有该文件，页面几何在 `layout.json` 的 `pdf_info[].page_size`。三个 library 项目的 `pages[].width/height` 因此全部是 0。

### 已实现（后端）

- `Document.bbox_space` 显式声明 bbox 单位（`mineru-1000` | `none`），旧文档缺字段时按 `mineru-1000` 解释，**不重写已有 document.json**；schema_version 从 1.1 起写入该字段。
- `_page_metadata()` 改为「优先 middle.json、回退 layout.json」；`Page.source_file` 记录实际几何来源。
- `_valid_bbox()` 只接受 4 个有限数字且 x1>x0、y1>y0 的 bbox，退化框不再进入规范图谱；块级 `format.bbox_space` 标记坐标来源。
- 结构调整新增 `set_title` 操作（正文→标题并指定层级），`apply_patch` 改为操作注册表；`set_type`/`set_title` 的 `before` 记录完整 `{type, level}`，使正文↔标题可逆。`set_level` 撤销同时恢复 `type=title`。自动应用白名单仍只有 `set_type`/`set_level`。
- 新增只读 API：`GET /projects/{id}/raw` 列出封存原始文件，`GET /projects/{id}/raw/{path}` 读取单个文件，路径强制落在 `ocr/raw/mineru/` 内。
- `/health` 增加 `structure_review_v2` 能力标记。

### 已实现（前端 `frontend/src/review/`）

- `geometry.ts`：读取 `bbox_space` 后把 bbox 换算成百分比矩形，并提供渲染前的页面高度估算。
- `usePdfDocument.ts`：一份文档只加载一次 PDF，切换项目销毁加载任务。
- `pdfAssets.ts`：统一设置 `pdfjs.GlobalWorkerOptions.workerSrc`，并给所有 `getDocument` 传入 `wasmUrl`/`iccUrl`；配套 `frontend/public/pdfjs/wasm`（JBIG2 / OpenJPEG / QCMS 解码器）与 `frontend/public/pdfjs/iccs`。修复了扫描件只显示白页的问题。
- `PdfPageCanvas.tsx`：单页 canvas（像素乘 devicePixelRatio）+ 独立绝对定位覆盖层，`pointer-events` 分层保证覆盖层可点、不挡选区。
- `PdfPane.tsx`：连续滚动、只渲染当前页附近的 canvas（默认前后 2/3 页），其余页用页面几何占位；滚动停止 160ms 后才上报当前阅读块。
- `ResultPane.tsx`：结构块 / Markdown / JSON 三个视图，块卡支持 H1-H6、降为正文、快速设为标题、Cmd/Shift 多选批量操作，可按阅读位置跟随。
- `OutlinePane.tsx`：目录树内直接升降层级与降为正文（都走可逆补丁）、目录基准、目录页候选、建议队列、修订历史（逐项与批量撤销）。
- `ReviewWorkspace.tsx`：三栏共享同一 active block；点目录/结构块/bbox 双向定位并高亮；↑↓/jk 按阅读顺序跳转；1-6 设为对应层级；在输入控件内不拦截按键。
- `SourcePreview.tsx`：非 PDF 对照策略——图片与 HTML 用带令牌的 fetch 取回后转 blob URL 内嵌；Office 类明确说明没有浏览器内渲染器并降级为文本级对照；附带 MinerU 原始输出列表与只读预览。
- `App.tsx` 只保留项目/译文/导出/设置状态，review 分支不再内联。

### 修复：扫描件原页预览白页（WASM 解码器缺失）

- **症状**：打开《The White Lotus Teachings in Chinese Religious History》的「源文件预览」，页码与 bbox 覆盖层正常，但页面本身是空白，且界面不报错。
- **根因**：该 PDF 的扫描页全部是 JBIG2（`pdfimages -list` 统计 342 个 jbig2 stencil），另一个项目「Once upon a future time」是 jbig2 + jpx。PDF.js v5.7 的 JBIG2 / JPEG2000 / QCMS 解码器已改为 WASM，必须显式提供 `wasmUrl`；旧代码只设置 `workerSrc`，`wasmUrl` 为空时 worker 拼出 `nulljbig2.wasm`，回退脚本 `nulljbig2_nowasm_fallback.js` 同样取不到，解码失败只写 `console.warn`，不会让 `getDocument` 报错，因此表现为「没有报错的白页」。解码器本就在 `node_modules/pdfjs-dist/wasm/`，但没有进入 Vite 产物；Vite 的 SPA 回退还会把不存在的 `/wasm/*` 请求返回 `index.html`，`WebAssembly.instantiate` 拿到 HTML 后必然失败。
- **修复**：新增 `frontend/public/pdfjs/`（`wasm/` 与 `iccs/`，构建时原样拷贝到站点根目录）；新增 `frontend/src/pdfAssets.ts` 统一 worker 与 `pdfAssetOptions`（`wasmUrl`/`iccUrl`，目录末尾斜杠必需，否则 `getFactoryUrlProp()` 抛 `Invalid factory url`）；`usePdfDocument.ts`、`PdfPage.tsx`、`ImportWorkspace.tsx` 改为复用该模块。

### 已验证

- Python 自动化测试：`41 passed`（新增 11 项：layout.json 页面几何、bbox_space 向后兼容、退化 bbox、阅读顺序定位、set_title 可逆与自动边界、未知操作拒绝、review API 能力标记、document bbox_space/页面几何、set_title 补丁与撤销、raw 列表与路径限制）。
- `tsc --noEmit` 通过；Vite 生产构建通过（49 modules，`dist/` 被 Git 忽略），`dist/pdfjs/wasm` 与 `dist/pdfjs/iccs` 随构建生成。
- WASM 资源在 Vite 开发服务器上实测返回正确 MIME：`/pdfjs/wasm/jbig2.wasm` → `200 application/wasm`（104852 B），`/pdfjs/wasm/openjpeg.wasm` → `200 application/wasm`（252032 B），ICC → `200`（8464 B）。浏览器内实际解码效果仍待用户刷新后确认。
- 用真实项目库启动本地 API 探测：`/health` 返回 `intake_v1` + `structure_review_v2`；某 ready 项目的 `/document` 返回 `bbox_space=mineru-1000`、81 blocks；`/raw` 列出 7 个封存文件；`/raw/<file>` 返回 200 且字节数与清单一致；`/raw/source/origin.pdf` 与 `/raw/project.json` 返回 400，缺失文件返回 404，无令牌返回 401。

### 未完成

- 本轮**没有**做浏览器交互验收，也**没有**启动完整 WebUI；新模块的交互行为只能在下次启动时由用户验收。
- 扫描件白页修复只做到「资源可达 + 构建产物存在 + `tsc`/build 通过」，尚未在浏览器里肉眼确认 JBIG2 页面真正渲染出图像。
- 旧项目的 `pages[].width/height` 仍为 0（历史数据），PdfPane 退回 PDF 首页宽高比占位；重建规范文档的入口本轮未实现。
- 表格/公式等可视化块的独立截图对照、块级滚动像素同步、非 PDF 的 Office 内嵌渲染仍未实现。

### 外部服务事实

- 本轮只对本地 API 做只读 GET 探测，没有向 MinerU 上传任何文档，也没有调用任何翻译模型。

## 2026-09-02：独立仓库与项目库迁移

### 目标与数据边界

- Literature 的唯一仓库根目录迁移为 `/Users/jiangwei/CdoexProject/Literature/`，保留原 `.git`、分支和全部未提交工作区改动，不重新初始化或覆盖历史。
- 程序源码、测试、启动脚本、计划和开发文档继续由 Git 跟踪；长期文档项目统一放入仓库内的 `library/`，并通过根 `.gitignore` 排除，避免原文、MinerU raw、译文和导出被误提交。
- 上传暂存、任务恢复状态和启动器状态继续使用 `~/Library/Application Support/Literature/`，日志继续使用 `~/Library/Logs/Literature/`，Token 继续使用 macOS Keychain。

### 已实现

- Python 默认项目库、独立 MinerU 工作流默认输出和 WebUI 启动器统一指向 `~/CdoexProject/Literature/library/`；`LITERATURE_LIBRARY` 仍可显式覆盖。
- 启动器显式把项目库路径传给 API，并移除“文稿”目录专属错误提示；项目库的读、写、枚举预检仍保留。
- README 和总体计划补充仓库/项目数据分层；macOS 应用包改为存放在新仓库的 `launcher/` 下，并通过自身位置解析仓库根目录。桌面只保留 Finder 替身，不再依赖旧源码位置或 Applications 文件夹；设置脚本支持安全重复构建并重建会跟随旧 bundle inode 的 Finder 替身。
- 迁移后发现 `.venv/bin/python`、`pyvenv.cfg`、editable 元数据和命令 shebang 仍分别绑定旧 Documents 运行时及旧源码位置，这会在 Literature 加载源码前触发 macOS“文稿”权限。旧 `.venv` 已替换；新环境使用 `~/.cache/codex-runtimes/` 的 Python 3.12.13 以 `--copies` 在新仓库重建并重新安装依赖，启动器同时增加 Documents 绑定检测作为防回归保护。
- 桌面旧实体应用和 `/Users/jiangwei/codexprogram/literature/` 包装目录已移除；活动入口只剩桌面 Finder 替身和仓库内应用包。迁移时曾将这些生成物移动到废纸篓，但最终核验时废纸篓已为空，因此不把它们记录为可恢复备份；`library/` 项目数据未受影响。
- 4 个已有 Literature 项目连同完整目录迁移到新 `library/`；21 个无活动任务引用的历史上传暂存目录移动到新项目库 `.trash/`，未永久删除。

### 已验证

- Python 自动化测试：`30 passed`。
- TypeScript 检查、Vite 生产构建和 macOS 启动脚本语法检查通过。
- 仓库内 `Literature.app` 的 `Info.plist`、JXA 反编译结果、adhoc 签名和 Finder 替身目标检查通过；从 Finder 启动后 API 运行于新仓库本地 `.venv/bin/python`，WebUI 运行于非 Documents 的 Node 解释器。
- 新 API 进程的打开文件中不存在 `/Users/jiangwei/Documents`；健康响应返回匹配的源码指纹和 `intake_items_v1`，WebUI 返回 `<title>Literature</title>`。
- 默认库路径与独立 MinerU 输出路径均解析为 `/Users/jiangwei/CdoexProject/Literature/library`；迁移后 Git 仍识别原仓库，`library/` 不出现在 Git 状态中。

### 外部服务事实

- 已保存 MinerU Token 的无副作用连接检查返回 `connected: true`。
- 本轮没有把任何用户文档发送给 MinerU；真实 PDF smoke test 仍需用户对具体文件的外发授权。

## 2026-09-01：修复 WebUI 与旧 API 进程不一致

### 根因

- 新版首页已经按文件发送 `/intake/jobs.items`，但 macOS 启动器只用固定版本号 `0.1.0` 判断 API 是否可复用，继续连接了前一天启动、尚未加载 `items` 模型的旧 Python 进程。
- 旧 Pydantic 模型忽略未知的 `items` 后，只看到空的 `upload_ids/paths`，因而返回“请选择至少一个 MinerU 支持的文件”。请求日志证明 `/intake/uploads` 已成功，400 发生在本地任务创建阶段，尚未请求 MinerU。

### 已实现

- 后端启动时对 `literature/**/*.py` 计算稳定源码指纹，`/health` 返回该指纹以及 `intake_items_v1` 能力标记。
- 启动器用当前源码计算期望指纹，只复用指纹和能力均匹配的 API；状态文件增加 API/WebUI PID 与指纹。
- 源码指纹变化时，启动器先核对已保存 PID、监听端口和进程命令，只终止自己管理的旧 Literature 服务，再在原端口启动新版 API 与 Vite。
- GUI 启动环境会实际执行候选 Python 来计算指纹；仓库 `.venv` 因 macOS 隐私边界不能运行时，自动回退到可用的 Codex Python 运行时。
- WebUI 初始化时检查 `intake_items_v1`，旧 API 会显示明确的前后端版本不一致提示。`IntakeJobCreate` 同时禁止未知字段，避免未来协议不匹配再次落入误导性的空文件错误。

### 已验证

- Python 自动化测试：`26 passed`；新增健康指纹/能力和未知 intake 字段拒绝测试。
- TypeScript 检查与 Vite 生产构建通过。
- 清理了 3 个旧 Literature API 与 4 个旧 Vite 进程；正式 `Literature.app` 已在 `41731/1423` 启动最终源码。
- 三次失败上传留下的 9 个无任务引用暂存目录（约 40MB）已移入 Literature 的 `.trash/orphaned-uploads-20260901-2037/`，未永久删除，活动上传暂存区已清空。
- 运行 API 的 OpenAPI 已确认包含 `items`，并声明 `additionalProperties: false`；健康指纹与启动器状态一致。
- 用临时后端源文件改变源码指纹后，正式启动器自动替换 API/WebUI PID；删除探针后再次自动替换回最终源码，证明旧后端不会再被复用。临时探针未保留。
- 浏览器刷新后的首页正常显示“MinerU 已连接”，无请求错误和控制台警告；本轮未选择文件、未创建解析任务，也未调用真实 MinerU。

## 2026-09-01：逐文件 MinerU 设置

### 已实现

- 从右侧批次级“更多 MinerU 设置”中移除 PDF 页码范围和额外输出格式；解析模型、语言、OCR、表格和公式仍是批次设置。
- 选择或拖入文件后自动打开逐文件设置弹窗。每行显示文件名、大小、PDF 本地读取的总页数、独立页码范围，以及 DOCX/HTML/LaTeX 额外输出选项。
- 每个 PDF 页码范围默认为空，并在界面中明确解释为空即解析全部；非 PDF 禁用页码输入，HTML 使用 MinerU-HTML 默认 Markdown/JSON 输出。
- 待处理队列显示每个文件自己的页码和额外格式摘要，并可随时重新打开设置弹窗修改。
- `/intake/jobs` 增加逐文件 `items` 请求结构，每个上传项分别携带 `page_ranges` 和 `extra_formats`。后端逐项目创建独立 MinerU 任务快照，同时保留旧版 `upload_ids`/`paths` 批量调用兼容性。

### 已验证

- Python 自动化测试：`25 passed`。新增验证同批两个 PDF 分别保存不同页码范围和不同额外输出格式；原有 HTML 自动切换 MinerU-HTML 与格式清理测试继续通过。
- TypeScript 类型检查和 Vite 生产构建通过。
- 隔离浏览器选择 3 页与 5 页 PDF 后，弹窗自动出现并正确显示总页数；分别设置 `1-2 + DOCX` 和 `2,4-5 + HTML/LATEX`，队列摘要和重新打开后的值保持独立，浏览器控制台无错误。
- 浏览器验收 API 显式禁用 Keychain Token，未点击“开始解析”，本轮没有调用真实 MinerU 服务。

## 2026-09-01：首页与文件资源流程校正

### 产品边界

- 文件上传、MinerU 参数设置和当前处理任务属于首页，是 Literature 工作流的第一步。
- “文件资源”是第二步，统一列出等待中、执行中、失败和已完成的文档任务与标准化资源；资源页本身不提供导入入口。
- 单个资源的后续处理手段留到下一轮，不在本轮提前设计或实现。

### 已实现

- WebUI 默认入口从原“文件项目”页改回首页，并将首页标题和叙述统一为“上传与处理”。
- 主导航改为“首页 → 文件资源 → 结构校对 → 阅读与翻译 → 导出”；点击 Literature 品牌返回首页，从已完成资源工作区退出则返回文件资源页。
- 首页只显示当前等待、处理中和失败待处理任务；已完成历史不再作为“已有项目”重复堆叠，提供“查看全部文件资源”入口。
- 文件资源页移除页头和空状态中的导入按钮，补充全部/处理中/等待中/失败/已完成状态概览，并关联 OCR 任务展示实时状态与阶段进度。
- 完成资源可进入结构校对；尚未生成 `document.json` 的资源只展示任务状态，不再跳回导入页。页面文案由“项目库/文件项目”统一为“文件资源/资源库”。

### 已验证

- TypeScript 类型检查和 Vite 生产构建通过。
- Python 自动化测试：`24 passed`。
- 隔离浏览器验收通过：默认首页、首页到文件资源导航、资源页无导入入口、全量状态概览、完成资源卡片、网格/列表切换、资源搜索、返回首页和浏览器控制台无警告/错误。

### 外部服务事实

- 隔离验收 API 首次启动时读取到了本机 Keychain 中已有的 MinerU Token；一份 66 字节的本地合成 HTML 验收文件在进程停止前完成一次真实 MinerU-HTML 批次（远端状态 `done`），结果仅保存在 `/private/tmp` 隔离目录。
- 发现后立即停止该 API，并以禁用 Token 的方式完成后续验收。本次只能证明该极小 HTML 夹具的上传、远端完成、下载和规范化链路，不能替代计划中的 PDF、图片、DOCX、PPTX、XLSX、HTML 代表样本 smoke test，也不把其他格式标记为真实验收。

## 2026-08-31：macOS 一键 WebUI 启动入口

### 已实现

- 增加 `scripts/start_webui.sh`：自动检查并复用 Literature API/WebUI，端口占用时在受限端口范围内选择空闲端口，等待两个服务就绪后打开默认浏览器。
- 增加并发启动锁、端口状态文件、独立日志、启动失败提示和重复点击复用；服务识别同时校验 Literature 健康响应与 HTML 标题，避免误用其他本地服务。
- 增加 macOS 启动器构建脚本和 JXA 入口，使用仓库现有 Literature 图标生成可点击应用包，保持旧 Litera 项目不变。

### 已验证

- 临时 `.app` 编译、Literature 图标、bundle 元数据和 adhoc 签名检查通过。
- 从 `.app` 启动后，API 健康检查返回 Literature `0.1.0`，WebUI 返回 `<title>Literature</title>`，浏览器打开项目库；本轮未调用 MinerU，也未上传任何文档。
- 再次点击 `.app` 后 WebUI 仍由同一个 Node PID 提供服务，证明重复点击会复用服务而不是重复启动进程。

## 2026-08-31：独立文件项目库

### 参考产品结论

- 只读检查了本机 MinerU 应用。`My Collections` 是收藏/摘录分类，不是文件项目管理器；真正对应 Literature 需求的是 `Tasks` 页面。
- MinerU `Tasks` 提供任务名搜索、模型/类型/状态/创建时间筛选、表格列、打开位置、删除和分页。Literature 复用这套信息架构，但按长期文档项目而不是一次性解析任务组织，并补充网格视图和重命名。

### 已实现

- WebUI 默认进入独立“文件项目”页面；今天完成的“导入与解析”与旧的“结构校对/阅读与翻译/导出”已接到统一主导航，不再表现为两套彼此隔离的前端。
- 项目库支持网格/列表切换、搜索项目名和原始文件名、状态和文件类型筛选，以及按修改时间、创建时间、名称或文件大小排序。
- 增加分页、每页数量和本地视图偏好记忆；项目卡片/行显示原始文件名、类型、大小、处理状态、修改时间、资源数和导出数。
- 已生成 `document.json` 的项目进入现有查看/校对工作区；尚未生成规范文档的项目回到导入任务页，避免用不完整数据打开编辑器。
- `PATCH /projects/{id}` 支持解析前后重命名。后端安全移动项目目录，处理同名冲突，并同步 `project.json` 与已有 `document.json` 标题。
- `DELETE /projects/{id}` 采用软删除：项目完整目录移动到项目库 `.trash/`，完成的 MinerU 运行目录也移出活动索引；有运行中或等待中的解析/翻译任务时返回 409，避免任务与目录竞态。
- 项目列表/详情增加 `has_document`、`asset_count`、`export_count`；本地开发 CORS 允许任意 loopback 端口，正式访问仍要求会话令牌。

### 已验证

- Python 自动化测试：`24 passed`。新增覆盖项目描述字段、解析前重命名、解析后标题同步、可恢复删除和删除后活动项目不可见。
- TypeScript 类型检查和 Vite 生产构建通过。
- 隔离项目库与本地 WebUI 浏览器验收通过：验证默认项目库、三项目网格、列表切换、关键词实时筛选、操作菜单、重命名保存、可恢复删除确认弹窗，以及项目库/导入页双向导航。
- 浏览器验收未执行最终删除按钮，也未调用真实 MinerU 服务；重命名只作用于 `/private/tmp` 中的隔离演示项目。

### 后续

- 回收站目前是数据层可恢复，尚未提供“回收站/恢复/永久删除”页面。
- 三栏查看编辑页的“原始文件—解析内容—翻译内容”重构属于后续结构校对阶段，本轮只保留既有查看页并建立正确入口。

## 2026-08-31：WebUI 优先与 MinerU 全格式导入

### 已实现

- 开发优先级从 Tauri 桌面端调整为 WebUI；已有桌面源码保留，但当前模块不再依赖 Tauri 文件路径能力。
- 使用标准 `<input type="file" multiple>` 与浏览器拖放事件建立待处理队列；点击会打开系统文件选择窗口。
- 新增 FastAPI multipart 流式上传暂存接口，项目创建后保存为 `source/origin.<原扩展名>`，不再固定为 `origin.pdf`。
- 输入格式对齐 MinerU 精准解析本地文件接口：PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX、PNG/JPG/JPEG/JP2/WEBP/GIF/BMP、HTML。
- PDF 继续自动按 200 页及 200MB 分片；非 PDF 作为完整原件交给 MinerU 转换，远端限制会保留为可操作错误。
- 前端展示并持久化 MinerU VLM/Pipeline（HTML 自动 MinerU-HTML）、全部官方语言包、OCR、表格、公式、PDF 页码范围和 DOCX/HTML/LaTeX 额外输出。
- `data_id`、轮询和结果匹配由 Literature 托管；本地文件流程不要求用户配置公网 callback/seed，也不展示只适用于 URL 的缓存参数。
- 新增根目录 `PROJECT_PLAN.md` 和 `AGENTS.md`，规定新 agent 或上下文恢复后先读取总体计划与开发日志。
- 非 PDF 项目完成后不会错误加载 PDF.js；结构校对页先提供结构块/Markdown/JSON，并明确标记原件预览仍待完善。
- 旧项目没有 `source_type` 时按旧版 PDF 项目处理，避免迁移后丢失原页预览。

### 已验证

- Python 自动化测试：`23 passed`，覆盖 Web multipart 上传、DOCX/HTML 项目、HTML 模型自动选择、任务设置快照、官方 MinerU 请求参数与非 PDF 准备流程。
- TypeScript 类型检查和 Vite 生产构建通过。
- 浏览器中实际触发系统文件选择器，确认支持多选；选择 DOCX 后文件名、大小、类型进入队列并启用“开始解析”。
- 浏览器验收完整语言包、模型、OCR/表格/公式、页码范围和额外输出设置；未点击“开始解析”，未把测试文件发送给 MinerU。

### 尚待真实服务验收

- 需要用户选取可公开测试的短 PDF、图片、DOCX、PPTX、XLSX 和 HTML，逐类验证 MinerU 在线上传、转换、轮询、下载与规范化结果。
- 真实多格式 smoke test 前不把“官方接口允许”表述为“所有格式已由真实服务跑通”。

## 2026-08-30：PDF 导入与 MinerU 分段进度工作台

### 已实现

- 将首页改为实际连接 FastAPI 的 PDF 导入与解析工作台；Tauri 支持多选和文件拖入，浏览器开发模式保留本地路径输入回退。
- 新增批量 intake API：一次提交多份 PDF，每份 PDF 独立创建 Literature 项目和持久化 MinerU 任务。
- 大型 PDF 继续使用既有 200 页上限自动分段、轮询、下载和按原始页码合并流程。
- OCR 状态接口现在返回真实总页数、已完成页数、分段总数、完成/下载/失败分段数，以及每段页码、状态、重试次数和错误。
- 前端采用渐进式披露：默认显示文档总状态；多段文档可展开详情；失败分段自动展开并提供“重试失败分段”。
- 删除误导性的 OCR 暂停 API。MinerU 远端解析阶段不显示暂停或取消；应用启动后在 Token 可用时自动恢复未完成任务的轮询和下载。
- “解析设置”标题区和页面设置按钮都可打开 MinerU Token 弹窗；Token 保存到 macOS Keychain，前端只显示“已保存”状态并允许替换，不回显完整密钥。未配置 Token 时不会把排队任务标记成正在处理。
- 第一阶段只对 PDF 声明真实支持；DOCX、PPTX 和图片需确定转换/主数据策略后再开放。

### 已验证

- Python 自动化测试：`20 passed`。
- 新增 401 页真实 PDF 回归，验证生成 200、200、1 页三个有效且无重叠的 PDF 分段。
- 新增批量 intake、格式边界、分段完成页数和“不暴露 OCR pause”接口测试。
- TypeScript 类型检查和 Vite 生产构建通过。
- `cargo check` 通过。
- 隔离环境中启动真实 FastAPI 与前端，前端成功读取后端排队任务并显示 Token、等待和任务状态。
- 浏览器验收了“设置 Token”入口、密码输入弹窗、默认禁用的空值提交按钮和页面无控制台错误；未写入假 Token，也未触发真实 MinerU 请求。

### 尚待真实服务验收

- 当前机器尚未配置 MinerU Token，因此本轮没有向 MinerU 上传文件；在线上传、远端轮询、结果下载和合并仍需使用真实 Token 做 smoke test。

## 2026-08-30：首个可运行纵向切片

### 已确定的数据主线

Literature 只有一条主数据路径：

```text
原始 PDF
  -> MinerU 原始输出（只读、带哈希、用于溯源）
  -> 规范化与目录结构校准
  -> document/document.json + document/source.md + assets/
  -> 编辑、提取、原生结构化翻译层
  -> EPUB / DOCX / HTML / MD+JSON 派生导出，或外部 TBL 接力
```

`document.json` 是长期主数据；`source.md` 是可读投影。任何导出器都不得直接读取原始 PDF 生成成品。原 PDF 只用于 MinerU 输入和人工对照。

### 已实现

- 创建了与 Litera 平行的独立仓库 `/Users/jiangwei/Documents/ChatGPT/literature`，没有让 Literature 依赖或回写 Litera。
- 迁入并隔离了 Litera 中已验证的 MinerU 上传、分页切片、轮询、下载、断点恢复与结果合并工作流。
- 建立本地项目目录、SQLite 任务/结构修订/token 用量账本，以及不可变 MinerU raw 层和 SHA-256 校验清单。
- 支持导入 MinerU `content_list_v2.json`、旧 `content_list.json` 与 Markdown，规范化为稳定 block/cell ID、页码、bbox、阅读顺序、资源引用和保留的 raw 字段。
- 建立 `Document`、`Block`、`Cell`、`TocNode`、`StructurePatch`、`TranslationLayer`、`UsageRecord` 等核心模型。
- 支持英语、日语、法语、德语自动检测，以及项目级和章节级人工覆盖。
- 实现可逆标题降级/层级补丁。自动修改仅允许 `set_type` 与 `set_level`，阈值固定为 0.97；不自动删除、合并或改写正文。
- 实现 OpenAI、DeepSeek、智谱 GLM、Qwen/百炼和自定义 OpenAI-compatible provider；密钥通过 macOS Keychain 保存。
- 全书、选章、block、即时选段共用结构化翻译层；模型必须按 block/cell ID 回写，人工编辑自动锁定，重译默认跳过锁定项。
- 实现 HTML、MD+JSON、EPUB 3、DOCX 的原文/译文/双语派生导出；表格按 cell，图片、图注、脚注和章节锚点随图谱输出。
- TBL 仅作为外部接力：Literature 从规范图谱导出 EPUB/DOCX，再启动独立 TBL；TBL 不写入 Literature 的译文层。
- 建立 FastAPI loopback sidecar、会话令牌、CORS 边界、Tauri 外壳与 React/PDF.js 桌面界面。
- 结构校对界面采用 MinerU 当前 WebUI 的设计语言：左侧结构/任务、中间文档预览、右侧解析结果，橙色主操作、半透明白色面板、Markdown/JSON/结构块标签页和处理步骤状态。
- 阅读翻译界面支持章节复选、原译双栏、当前 block 翻译、划词/选段即时翻译、保存到译文层、人工编辑锁定与解锁。
- 结构 AI 提供完整输入审计预览，只发送确认目录、标题候选、bbox 和相邻 block 类型；服务端再次验证 block ID、操作白名单和 0.97 阈值。
- 本地目录页候选支持英、日、法、德常见目录标记、页码行密度和标题/列表结构评分；候选必须由用户确认。
- 低置信结构建议支持逐项/批量确认，补丁历史支持逐项撤销。
- 术语表可由本地规则草拟、人工编辑并冻结为翻译层快照。
- 翻译任务面板支持暂停、恢复和失败分块重试；失败 chunk 会在译文层留下 `failed` 状态。
- 用量面板支持逐次 provider/model/token/估价记录，单价可编辑；项目预算达到后会在下一分块调用前暂停。
- 非敏感 provider 设置和翻译附加要求保存在本地 WebView；附加要求会进入任务快照，但不能覆盖 block/cell ID 与结构约束。
- 增加 PyInstaller sidecar 构建脚本、Tauri `externalBin` 覆盖配置和发布态 sidecar 发现逻辑。
- TBL 命令与本地 URL 可在设置中心持久化，导出页可生成结构化交接文件并启动/查看独立 TBL；配置保存在应用支持目录而非项目主数据。
- 前端会等待本地 sidecar 健康检查，降低桌面应用启动时的 API 竞态。

### 已验证

- Python 自动化测试：`16 passed`。
- 测试覆盖 raw 哈希、稳定 ID、语言识别与覆盖、结构补丁/撤销、严格 block/cell 回写、人工锁定、即时选段保存、四种导出、API 会话令牌和 CORS。
- 特别验证：测试会先删除 `source/origin.pdf`，再成功生成四种导出，证明导出链只依赖规范文档图谱、译文层和资源。
- TypeScript 类型检查通过。
- Vite 生产构建通过；受限环境下将验证产物写入 `/private/tmp`，未覆盖仓库既有 `frontend/dist`。
- `cargo check` 通过。
- 本地浏览器对三工作区、PDF/bbox 选择、结构块/Markdown/JSON 切换和 MinerU 风格界面做过交互验收。

### 本轮发现并修正

- 修正文档叙述，明确“PDF -> EPUB/DOCX/HTML”不是 Literature 的数据流。
- 将语言设置补齐为自动检测 + 英/日/法/德人工选择，并支持标题 block 上的章节覆盖。
- 将即时选段翻译保存为 block 译文层中的附属记录，而不是破坏整块译文。
- 导出器测试新增“原 PDF 已移除仍可导出”的硬性约束。
- 结构校对页改造成更接近 MinerU WebUI 的 2:4:4 桌面三栏及解析结果标签页。
- 增加 1024px 附近窗口的响应式三栏收缩规则。

## 与原计划相比仍未完成

以下项目不能标记为真实完成：

1. 术语表已可冻结，但尚无独立版本浏览/回滚界面。
2. 翻译任务可暂停、恢复和重试失败 chunk，尚未实现可配置的 provider 并发与速率限制。
3. 用量明细、手工单价和预算暂停已完成；自动更新价格表和“预计本任务花费”预检尚未实现。
4. sidecar 构建与 Tauri bundle 路径已编码，但尚未实际安装 PyInstaller 生成二进制，也未做签名、公证和干净机器安装测试。
5. 尚未用真实 500+ 页扫描书、真实 MinerU Token 和四家真实模型密钥执行端到端验收。
6. 日文竖排、复杂跨页表格、脚注回流和超大图片资源仍需要真实样本回归。

## 下一步顺序

1. 增加术语表版本回滚、provider 并发/速率限制和任务花费预估。
2. 为 MinerU 和模型服务写可注入的 HTTP 集成测试夹具，再用用户提供的真实样本与凭据做 smoke test。
3. 实际生成 PyInstaller sidecar 与 `.app` bundle，完成签名/公证和干净 macOS 验收。

## 外部验收所需输入

- 一份代表性的 500 页以上扫描 PDF，以及英、日、法、德各至少一份小样。
- MinerU Token。
- 至少一个翻译模型 API key；四家 provider 的最终兼容性测试需要各自 key。
- TBL 的实际可执行文件路径和启动参数。
- 若发布给其他 Mac：Apple Developer ID 与公证凭据。
