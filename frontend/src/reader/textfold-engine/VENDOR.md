# TextFold 阅读器内核 vendor（逐字移植）

来源：https://github.com/chenxiachan/textfold
Pin：`2182d9a8662e69790850021dd8c965d0398777c4`（与 third_party/textfold/VENDOR.md 的 Python 内核 pin 一致）
许可证：Apache-2.0（上游 LICENSE，见 third_party/textfold/LICENSE）；上游 NOTICE 同目录。

## 文件（逐字副本，未改一行内核逻辑）

| 文件 | 上游路径 | 字节数 | 说明 |
|---|---|---|---|
| `upstream-reader.js` | textfold/web/reader.js | 270,423 | 阅读器内核（五层 zoom/词级 morph/stage/anchor/selbar/notes/peek/topic map） |
| `upstream-reader.css` | textfold/web/reader.css | 94,058 | 阅读器样式 |
| `upstream-i18n.js` | textfold/web/i18n.js | 243,764 | 界面语言（window.DR_I18N；CODES/t/uiFor/NATIVE/guess/htmlLang） |

校验：三者 git blob sha 与 pin 处 GitHub 内容一致（reader.js `fe109727…`、reader.css `f423f7bb…`；i18n.js 自同 commit 下载）。

## 兼容补丁（可追踪；原件不动）

`apply_patches.py` 从 `upstream-reader.js` 生成 `engine-patched.js`（锚点精确替换，锚变即报错）：
1. view()：目标 lane 分派——source-fallback 原文回退；ev_space=source + derived_brief 时
   L3 Brief 用派生独立 token 流（briefOverride，不进全文、不伪造 m）；
2. pieces(L3)：briefOverride 存在时句选择/切分/渲染全在派生空间（`db` token id 前缀独立身份）。
backend 契约：docs/READER_BACKEND.md §4.8b（tr[locale].lad.ev_space / derived_brief）。

## 集成方式（外壳适配桥 + 可追踪兼容补丁）

**诚实声明：运行内核不是“一行未改”**——`upstream-reader.js` 保存逐字原件（可校验），生产加载的是
`apply_patches.py` 生成的 `engine-patched.js`（原件 + 以下最小兼容补丁，每段有精确锚、锚变即报错）：

1. **view() 目标 lane 分派**：source-fallback 原文回退；ev_space=source + derived_brief 时 L3 Brief
   使用派生独立 token 流（briefOverride，不进全文、不伪造 m）；ev_space=target（translation-local）
   时 ev 用目标 lane 自己的 lad.ev（原文 ev 只用于回跳原文）；
2. **derived 独立索引贯穿**：L3 派生 pieces 携带 `di`（derived 空间下标）→ DOM `s._di` →
   selectionAnchors 收集 `_di` 产出 `tokens=[dlo,dhi]`（派生空间下标，持久化为
   engine_anchor.space='derived'）→ marksFor 仅在派生 lane 消费（dexact，不进 Full exact/soft）
   → pieces 着色走 dexact。Full 层不消费 derived 锚，防错标；
3. **srcdoc history 守卫**：setLang 的 history.replaceState 在 srcdoc iframe 内 try/catch。

其余集成（paper-data 注入、template 脚手架、fetch RPC 路由、KaTeX）均为外壳行为，不改内核逻辑。

上游 reader.js 是整页 IIFE：吃 `#paper-data` JSON，接管 `document`（topbar/footer.zoom/#doc/#ghosts）。
Literature 三栏客户端里由 `TextFoldPane.tsx` 以 **iframe(srcdoc)** 承载：每栏（原文/译文）一个引擎实例，
srcdoc = template.html 脚手架 + i18n + paper-data + engine-patched（内核+兼容补丁）内联。宿主通过
`contentWindow.__reader`（setLevel/zoomBy/scrubTo/letGo/level/lang…）下发命令，注入的
`bridge.js`（宿主代码，非上游）把引擎内部层变化 postMessage 回宿主，并转发跨栏 hover/批注事件。

上游网络钩子 `/api/notes/*`、`/api/ask` 由 bridge 在 iframe 内重写 `window.fetch` 路由到
Literature 阅读层（overlay/统一问答）；不可用项如实禁用，不伪造。

KaTeX：完整离线资源已 vendor（`public/textfold-vendor/katex/`：katex.min.css/js + 60 fonts 0.16.11，
LICENSE=MIT/Khan Academy，来源 cdn.jsdelivr.net/npm/katex@0.16.11），`D.katex` 指向本地路径
（`/textfold-vendor/katex/`），行内公式离线渲染。

## pin 之后未跟进的上游提交（可追溯）

- `1b5f41bc` ⌘F 文内查找（2026-10-24）
- `f4367dc8` / `c4b592c0` 高亮颜色体系重构（颜色随层 + 深浅档）
- v0.10.32（fa5f9768）Mermaid 图渲染

以上不阻塞本轮；升级需整体重 pin 并重跑对照清单。
