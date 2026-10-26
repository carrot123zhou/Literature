# 阅读模块 · 现状设计（唯一现状文档）

> 状态：**产品前端已实现并通过隔离端到端验收（2026-10-25，`tmp/reader_e2e_check.py` + `tmp/reader-e2e.cjs` 24/24 项，隔离后端+真实 PDF 夹具+真实 reader API；此前原型阶段核心交互 38/38、浮窗 27/27）**。
> 生产代码：`frontend/src/reader/**`（ReaderWorkspace / ReaderSettings / ModelPicker / readerApi / types / reader.css / devEntry）；
> 入口 `#/p/<projectId>/reader`（App.tsx 由 developer 接线）、设置 → 阅读（ReaderSettings，roles `reader_fold`/`reader_chat`）。
> 后端契约：`literature/reader.py` + `/projects/{id}/reader/*`（developer 实现）；TextFold 折叠核心 vendored 于 `third_party/textfold/`（Apache-2.0，pin `2182d9a8`，见 VENDOR.md）。
> 交互原型（`opendesign-projects/73279d59-…/design.html`）保留为已批准的设计基准；下文交互规范两者一致，原型专有的演示边界（自编文档/示意版面/罐头回答）不再适用于生产实现。

## 0. 生产实现现状（2026-10-25）

- **数据流**：DocumentGraph + TranslationLayer 只读呈现；阅读交互（批注/偏好）落 `reader/overlay.json`（后端 CAS 乐观并发），五层派生落 `reader/layers.json`（textfold/doc v0，unit id = 稳定 block_id）；均不写回原始文件。
- **三栏**：原件=真实 PDF（复用 review/PdfPageCanvas + usePdfDocument + mineru-1000 bbox 契约；非 PDF/加载失败按格式降级并说明）；原文/译文=语义渲染（标题层级/段落/表格 cells/图片 assets 原位/脚注/公式/代码，缺译回退原文+标记）。
- **五层折叠**：首开只读；点击「简述」等短层即进入折叠，免费本地预览与当前阅读节的 AI 升级按作业真实完成状态衔接。章节选择、按需调度、缓存由程序处理，不让读者配置范围；只保留紧凑五档条，进度/未完成状态用小字，绑定与具体错误收进详情。原文 brief 为逐字原句；目标语 derived brief 为分层译句，须区别于已有译文逐字摘录。
- **批注**：右键整段与鼠标划词共用选区及操作条（5 色高亮/笔记/追问）；已有高亮右键进入编辑态（笔记/移除/换色/追问），与选区条互斥。左键不弹批注条，短层左键可局部展开；hover 仅段前小圆点轻动效，原件 bbox 轻描边联动。字符偏移+quote 锚定、overlay 防抖保存与 CAS 保持。
- **统一问答**：单一侧栏无 tab；一次性选段引用卡（✕ 关闭回全文，摘要/出处可回跳）；发送冻结快照到该条消息（问题在上、可折叠引用在下，含「回原文」）；持久化会话（GET/POST/stop/retry/clear，700ms 轮询，无伪 token 流式）；回答引用 chip（[#块ID]）可点击回跳；答案语言随问句；**未配置模型时如实报错，无罐头回答**。
- **模型选择**：输入区旁按服务商分组的两层 picker（搜索/键盘跳过不可用项/Enter 守卫/未配密钥禁用/空态提示），popover portal 到 body 并做视口定位（上/下翻转 + 四边夹取 + 滚动重定位），长模型名 ellipsis 不遮操作；每次请求冻结实际 service_id+model；设置 → 阅读两个独立默认角色（ReaderSettings + roles API），生效绑定/来源/问题用后端 role_states DTO 如实显示（bound/unset 继承翻译/cleared/invalid 不兜底），不把服务商第一个模型伪装成有效绑定。

## 1. 范围与边界（不混写）

- **原型阶段边界（历史，仅设计参考）**：原型使用自编双语演示文档、程序绘制的示意版面（显著标注「原件示意 · 非真实 PDF」）与标注的模拟回答；交互状态存 localStorage 命名空间 `literature-reader-proto:`。这些边界**不适用于生产实现**。
- **生产现状边界**：问答/折叠需绑定模型（`reader_chat`/`reader_fold` 角色）才可用，未绑定时 UI 如实禁用并提示；后端问答暂无 token 级流式（界面显示「正在基于原文组织回答…」直到真实回答到达，停止由后端 stop 语义保证迟到回答丢弃）；语义检索默认免下载（词面召回），未引入 embedding、无下载/启用按钮；真实模型端到端质量与费用尚未验收（见 §8）。

## 2. 三栏布局契约（2026-10-25 用户校准：三个独立选框，无组合预设）

- 顶栏【原件】【原文】【译文】三个独立 checkbox 胶囊（真实 `input[type=checkbox]`，可键盘 Tab+Space 切换，选中蓝底白字高亮，`aria` 可达）；取消勾选即隐藏对应栏，各自独立开关；**不设排列组合菜单**。
- 至少保留一栏：最后一个选中项取消时拒绝并 toast 提示，勾选状态与界面始终一致。
- 宽度模型：可见栏按像素制 `weights` 分配（Σ可见栏权重=可用宽度，flexGrow=权重 ⇒ 拖宽像素级一致），分隔条拖拽只改相邻两栏；隐藏中栏后相邻可见栏的分隔条仍接通可拖；隐藏栏权重保留。
- 窄屏（≤899px）单栏显示：三个选框表示**可用栏集合**（工具栏第二行居中，标签完整可读），底部 tab 只列已开启栏；关闭当前显示栏时自动切到另一个已开启栏；宽屏恢复时选框组合原样生效。无自动降档（不会静默改勾选）。
- 宽度断点实测无横向溢出：1440 / 768 / 390 / 320。

## 3. 锚定与联动（与产品契约一致）

- 一切定位以**稳定 block_id** 为锚；原件 bbox 按 `mineru-1000` 归一化坐标换算为页面百分比矩形（与 `frontend/src/review/geometry.ts` 的 `bboxRect` 契约一致）。
- 双向联动：文本栏滚动 → 驱动另一文本栏与原件 bbox 高亮；原件滚动 → 驱动文本栏；点击原件 bbox → 回跳文本栏；hover bbox → 文本块预览描边。
- 两个独立开关（可单独关闭）：**跟随滚动**（滚动联动）、**同步定位**（点击/选中联动）。关闭后确认无任何跨栏动作（浏览器已验证）。
- 滚动驱动只统计可见块（`getClientRects` 过滤），隐藏块不干扰滚动锚点；跳转前先展开目标（见 §5），避免跳进隐藏 DOM。
- 防反馈循环：程序化滚动持 guard 标志，rAF 后释放。

## 4. TextFold 五层（核心语义与上游对齐；呈现沿真实源码改编）

| UI 层 | 中文名 | 上游层（data-l / LEVELS 下标） | 每段/每节变成 | 内容字段 | 谁 |
|:-:|---|---|---|---|---|
| 1 | 全文 | 4 Full | 段落原样 | `unit.full`（逐字还原自 toks） | 作者 |
| 2 | 简述（关键句） | 3 Brief | 1–3 句逐字关键原句 | `unit.brief[]` / `brief_text`（由 `evidence` 下标 0 起在本地拼回，摘句逐字来自原文） | 作者 |
| 3 | 要点 | 2 Key | 要点+理由 | `unit.key`（含 takeaway+reason，模型写出，基于 evidence 句） | AI |
| 4 | 主线 | 1 Takeaway | 章节主线一句 | `section.takeaway` | AI |
| 5 | 主题 | 0 Topic | 少量词语主题卡/图谱 | `section.topic` / `unit.topic` | AI |

上游 i18n.js 中文层名 `['主题','要点','关键句','简述','全文']`（粗→细）与本 UI `['主题','主线','要点','简述','全文']` **名字不完全一样**：上游第 4 档叫「关键句」（Brief，逐字摘取 1–3 关键原句），本 UI 叫「简述」；上游第 3 档叫「要点」（Key，写出要点+理由），本 UI也叫「要点」但排序不同。字段语义以核心 worker 冻结的 DTO 为准，此处仅记录映射关系，不做语义偷换。
每 unit/section 另有 `source: model\|local\|suppressed` 与 `section.source: model\|local\|suppressed\|mixed`，界面据此诚实标注来源（见下方 AI 状态）。

- **底部悬浮五档条**替代顶部下拉：凹槽轨道+白色滑块（视觉与指针交互改编自上游 footer.zoom/.zoombar/.zthumb 与 measureBar/placeThumb/posAt/pointer 逻辑），role=slider + aria-valuetext；点击档位/直接拖动（>3px 阈值进入连续拖、松手吸附）/浮条上滚轮/←→/Home/End/数字键 1–5 均可切层；阅读区 ⌘/Ctrl+滚轮切层（不劫持普通滚动与浏览器缩放）。
- **原文与译文同一层级状态同步折叠**；**原件页不受折叠影响**；局部展开：L2/L3 左键段落展开全文，L4/L5 展开整节全文（含该节图表等透传块）。上游的词级对应、连续 morph/stage 尚未迁移；原型中的块级 FLIP 不得作为生产动画已完成的证据。
- **标题树折叠另设**：各节 ▾ 箭头独立于五层。
- **AI/本地身份诚实标注（生产）**：不在每段重复彩色徽章。分层来源按后端 provenance 与 unit/section 的 `source`（`model`/`local`/`suppressed`/`mixed`）如实标注：浮条提示按 provenance.content 显示「由模型生成（模型名）/混合来源/本地摘句（程序摘取，非 AI 概括）」；主题卡角标按 section.source 显示「AI / AI+本地 / 本地」。`reader_fold` 的**本地摘句不需要模型**（mode=local，不产生任何模型调用），不能笼统写「折叠需绑定才可用」；模型折叠（mode=auto）才需要 `reader_fold` 绑定。分层部分失败时 provenance.partial 如实提示「部分章节仍是旧内容/本地摘句」。
- 批注随层可找回：任何层级下有批注的段落显示页边黄条（改编自上游 .u.has-notes::after），点击打开笔记；批注列表回跳自动展开目标。

### 4.1 按阅读位置渐进折叠

- 空层点击短层先生成免费本地预览；本地请求受理不等于作业完成，须等状态确认后才启动 AI。
- 自动模型请求每次只含当前阅读节 `section_ids=[id]`；同一折叠产物供全部短层复用，无 force，切层不重复计费；切回全文不继续发起新节。
- 有译文时以目标语言节是否完成为准，不能以原文节已有 AI 为由跳过尚缺的目标短层；缺译按已有回退契约处理。
- 失败不自动死循环，小字提供重试，详情保留实际原因；混合来源不等于失败。模型未配置时仅提示设置并保留免费预览。
- 来源与模型信息仍可在详情查看；强制重折明确提示额外计费，并限定当前节。

## 5. 批注（高亮 / 笔记）

- 划词浮层：5 色高亮、笔记、AI 追问；**仅支持同一段落内划词**，跨段选择明确拒绝并提示，不产生残留记录；气泡位置双边界夹取（不跑出视口）。
- **字符偏移锚定**（改编自上游 textOffset/rangeAt/selectionAnchors 思路）：记录 `block_id + pane + start/end + quote(exact/prefix/suffix)`，替代旧的全文 indexOf 首匹配——同段重复词、跨 `<sup>/<b>` 内联节点的高亮在刷新恢复后位置仍准确；内容变化时按 quote 重定位并写回，失败回退段落级 tint。
- 跨语言规则：**原文栏逐字高亮；译文栏只做对应段落级标记**（tint 色条），无句级对齐证据，不伪装逐词对应；删除时 mark 与 tint 一并清理。
- **存储（生产）**：批注与阅读偏好保存在项目目录 `reader/overlay.json`（后端 `PUT /projects/{id}/reader/overlay`，带 revision 乐观并发 CAS），**不写回 document.json / translations / ocr/raw**；保存防抖、失败保草稿可重试、冲突（其他窗口已更新）时重载服务端状态并提示。批注结构（含 detached 纯笔记）由后端 `sanitize_overlay` 校验收敛。
- 笔记可编辑；批注抽屉列出全部条目，点击回跳（先展开再滚动+闪烁定位）；页边黄条点击直达笔记。
- **高亮操作浮窗（2026-10-25 最新用户校准）**：已有高亮改为右键打开编辑态，提供笔记、移除、换色、追问；与新建选区菜单互斥。换色改同一记录，追问引用当前记录的原文证据。Esc/关闭/外点退出，浮窗须夹在视口内。
- **移除高亮语义**：有笔记 → 仅摘除高亮，保留为纯笔记记录（`detached`，不再产生 mark/tint，页边条与列表保留并标注「已移除高亮 · 笔记保留」）；无笔记 → 删除整条记录。同段多条批注互不影响。

## 6. AI 侧栏（统一对话 + 一次性选段引用；2026-10-25 用户校准）

- **单一统一对话侧栏**，无全文/选段模式 tab；用户不需要选模式。顶部「AI 问答」只打开同一侧栏，不改变当前引用。
- **待发送引用卡**（输入框上方）：显示选段摘要（两行截断）、段落出处；选译文时标注「引用自译文 · 关联原文」，实际证据始终取对应原文 block（不伪装精确回译）。✕ 关闭即回到全文提问（草稿/高亮/笔记不受影响）；摘要与出处可点击回原位置。
- **引用一次性**（最新语义）：成功发送时冻结当时的引用快照 → 该条消息「问题在上、选段引用在下」（默认折叠 details，展开区分实际选中文字与关联原文段落证据，并可「回原文 ↗」；译文消息显示译句+关联原文标识）；发送后立即清空待发送引用，下一问回到全文，**不自动沿用**；继续追问请再点阅读器高亮/划词「追问」重新附引用。空问题提交不消耗引用；历史引用永久属于它那条消息，展开/回跳不会悄悄恢复待发送引用。
- 无引用占位「向全文提问…」，有引用「就这段内容提问…」；**不自动发送**，示例问题只填入。
- 回答来自**真实模型**（`POST /reader/chat/messages`，后端 ReaderChatRunner 持久化会话）：回答语言跟随提问语言；引用 chip（`§ x · 原文`）可点击回跳原文块。**未绑定 `reader_chat` 模型时如实报错并保留草稿/引用，不使用任何罐头回答**。生成期间显示「正在基于原文组织回答…」（后端暂无 token 级流式，界面不做伪逐字输出）；停止/重试走后端 stop/retry 端点（迟到回答由服务端丢弃）。
- **面板外观**：圆角浮动面板（桌面上/下/右各留 16px、圆角 18px、细边框+克制阴影；窄屏留 10px、圆角 16px、宽 calc(100% - 20px)），overflow 裁切使标题/聊天底色不穿圆角；收起动画位移超过面板宽+边距，无残留边框/阴影/横向溢出；内部 flex：历史滚动，引用区/输入固定底部。消息无可见角色行（左右对齐表达身份，DOM class 保留作逻辑钩子，aria-label 供读屏）；模型/绑定来源仅在面板顶部副标题一处说明（实际模型名，或「继承翻译绑定」的如实标注）。
- **question context 接口（已实现）**：每次发送携带冻结的引用快照（`quote: {block_id, pane, text}` + `locale` + `{service_id, model}` 覆写），与 UI 待发送状态解耦；历史与实际模型路由由后端按消息持久化（见 `readerChatClient.ts` 契约）。

## 7. 排版规则（Aa 面板）

- 外观：浅色 / 纸色 / 深色（CSS 变量主题层）；字号 13–20px；行距 1.7/1.9/2.1；衬线（Georgia + 宋体系）或无衬线；专注模式（F 键或按钮，隐藏工具栏/状态栏，浮动退出）。
- 正文规则：栏宽 ≤640px（约 38–42em）；中文正文两端对齐、段距独立于行距；标题按 1.45/1.18/1.05em 递减；图题居中弱化（0.76em 灰）；表格数字列右对齐 + tabular-nums，外层 `.tbl-scroll` 横向滚动防窄屏撑破；脚注 0.8em 上边线分隔，正文 `sup.fnref` 可点击回跳脚注、脚注 `↩` 返回正文；**缺译回退**：译文缺失的块显示原文 + 「未译 · 显示原文回退」标记（回退规则，任何译文层均适用，非演示专用）。

## 8. 验收证据

### 8.0 生产实现（2026-10-25，隔离端到端）

- 脚本：`tmp/reader_e2e_check.py` + `tmp/reader-e2e.cjs`（隔离 runtime/library 后端 create_app+uvicorn + vite dev + 真实 PDF 夹具 + 真实 reader API；**24/24 项通过**）。
- 覆盖：真实 PDF 页 canvas 渲染；正文/表格来自真实 document.json；本地摘句折叠（真实 job）+ 要点层；划词高亮 → overlay PUT 保存 → 刷新从后端恢复；未配置模型时问答如实报错（toast「阅读对话模型不可用…」）且草稿/引用保留、无罐头回答；模型选择器空态；圆角浮动面板几何；三 checkbox 开关；窄屏 390/320/768 单栏 tab/无溢出/问答与模型 picker 可达。
- 截图：`/tmp/reader-shots/prod-desktop.png`（真实 PDF+语义正文+圆角问答面板）、`prod-narrow-390.png`。
- 边界：隔离夹具为最小真实 PDF+手写 document.json（结构同 canonical 契约）；未验收项——真实模型问答/折叠质量与费用、真实译文层的双语折叠、tester 生产浏览器 20 项、打包客户端真机。

### 8.0.1 原型阶段验收（历史；以下为设计基准阶段的证据，使用演示数据与模拟回答，不构成生产验收）



- 当前权威脚本：`tmp/reader-final-check.cjs`（Playwright，ms-playwright-go 1.57.0 node + package，chromium headless，file:// 直载，失败退出码非 0）；**38/38 项通过，0 脚本错误**（在统一对话版基线上复跑通过）。
- **统一对话/一次性引用补充验收：`tmp/reader-unified-chat-check.cjs` 27/27 项通过，0 脚本错误**：无 tab 单一侧栏；无引用全文问答；原文划词 → 引用卡（不自动发）→ 空值提交不消耗 → 第1问带折叠引用 → 发送后待发送引用清空 → 第2问无引用 → 历史引用可展开/回原文且不恢复待发送引用 → 高亮追问第3问重新附引用且发送不变高亮；译文引用卡译句+关联原文标注、证据=原文；高亮追问不串上下文；关开侧栏保留引用与草稿；✕ 不清草稿/高亮；320/390 引用卡与发送可点不溢出。
- **圆角浮动面板 + 无角色行验收：`tmp/reader-rounded-check.cjs` 27/27 项通过，0 脚本错误**：1440×900/390×844/320×640 下面板圆角 18/16px、上下留白 ≥15/≥10px、overflow 裁切、无横向溢出；发送/关闭/引用✕（有引用时）可见可点；长聊天可滚；一次性引用发送后清空；关开侧栏保留草稿；无可见「你/AI」角色行（左右对齐表达身份，aria 保留）。
- **高亮操作浮窗补充验收：`tmp/reader-hl-actions-check.cjs` 27/27 项通过，0 脚本错误**：点击已有高亮无需重选即见四操作；笔记编辑保存；换色不丢草稿+刷新后同一记录同范围同色；同段两条批注仅改/仅删目标；有笔记移除 → 纯笔记保留（mark/tint 无残留、页边条仍在），无笔记移除 → 整条删除；追问上下文=高亮块原文且不受陈旧选择污染（原文/译文双侧演示，中英文问题+引用回跳）；320/390 浮窗完整在视口内且操作可点、正文可读。
- 截图：`/tmp/reader-shots/`（desktop-3pane、desktop-single-l3、desktop-dark-l3、desktop-l5、hlpop-desktop、hlpop-ask、hlpop-mobile、mobile-390-final、mobile-320-final）。
- 覆盖：三选框独立显隐与 o+t 拖宽精度（+72±8）、最后一栏保护、状态保留（高亮/折叠/进度）；浮条点击/拖动/键盘←→/数字键/Ctrl+滚轮切层、普通滚轮不切层、滑块随层移动；要点层黑体主张+灰理由正文流；局部展开双语同步与来源层身份；**重复词第二处与跨 `<sup>` 内联节点的高亮刷新恢复准确**；页边黄条随层可找回；AI 中文模拟回答+引用；底部浮条不遮脚注/tab；窄屏 390/320 无溢出、选框可读、浮条不出屏。
- 历史：第一轮 52/52（`tmp/reader-proto-check.cjs`，已被本轮取代）；第二轮 63/63（`tmp/reader-proto-check.cjs` 修订版，预设菜单路径已废弃，仅保留为历史检查记录，以 final 为准）。
- 截图：`/tmp/reader-shots/`（desktop-3pane 默认三栏、desktop-single-l3 单栏要点层≈用户参考图、desktop-dark-l3 深色、mobile-390-final / mobile-320-final、desktop-l5 主题卡）。

## 8.1 原型阶段边界（历史）

- 跟随滚动开启时，原件的任何滚动（包括浏览器原生滚动到可见区）都会驱动文本栏——设计行为；点击联动测试需排除该干扰后验证。
- 拖宽最小宽度 240px；批注锚定解决重复词/跨内联节点，但内容真被改写时回退段落级 tint（无真实编辑流，不会被触发）。
- 切层动画为块级 FLIP + 轻量淡入；**上游词级连续 morph（stage/keyframes）未移植**，不宣称词级；双指捏合未实现（WebView 可靠性不足，不宣称支持）；reduced-motion 下全部动画禁用。
- 旧版草稿中「写入 reader.overlay.json」的说法已废弃：批注与偏好只存 localStorage，无任何文件写入。

## 8.2 上游源码复用与许可

- 参考源码：`tmp/textfold-reference/{reader.js, reader.css, template.html, LICENSE, NOTICE}`（GitHub fe109727…/f423f7bb…，curl 原件）；结构格式参考 `tmp/textfold-reference/format.md`。
- **实际复用/改编**（HTML 头注释与 tmp/reader-three-toggle-TEXTFOLD-{LICENSE,NOTICE} 同步留痕）：① 底部五档条轨道/滑块与指针拖拽（reader.css 125–153 footer.zoom/.zoombar/.zthumb；reader.js 3896–3964 measureBar/placeThumb/posAt/pointer）——改为 1–5 层、Literature 蓝、统一适配版坐标系（上游本身使用视口坐标）；② 层级排版（reader.css 257–302/473–474 .u 单元层）——映射到本稿 block 结构；③ 批注字符偏移锚定与页边条（reader.js 1982–2045 textOffset/rangeAt/selectionAnchors；reader.css 575–605 .u.has-notes::after）——适配 block_id+pane 契约；④ 切层块级 FLIP 与锚点 settle（reader.js 774–830 morph/snapshot/settle 思路，块级简化）；⑤ ⌘/Ctrl+滚轮（reader.js 4306–4333）收窄到阅读区。
- **未移植**：词级连续 morph/stage 预渲染、D.units/atoms/chunks/i18n 架构、后端 API、上游字体（Inter/Source Serif4，用系统字体替代）。
- **未来后端迁移接口**（仅记录，不实现）：上游 textfold/server format（format.md）以稳定 id 锚定层内容；本稿以 block_id 为同等锚点，canonical block ↔ 上游 unit 的适配点在「层内容生成（L2 摘句 / L3–L5 派生）」与「批注 quote 锚定」两处，均已在原型数据结构中预留字段。
- 许可：TextFold 为 Apache-2.0（Copyright 2026 Xia Chen）。生产代码的折叠算法来自 `third_party/textfold/`（vendored，pin `2182d9a8`，完整 LICENSE/NOTICE/VENDOR.md 随目录保留，逐文件对照与补丁清单见 VENDOR.md）；前端浮条/层排版/批注锚定的改编片段在 `frontend/src/reader/reader.css` 与组件注释中标注来源。上游论文内容许可不随代码授权，阅读样本来自用户真实项目或测试夹具。

## 9. 尚未实现 / 待验收（生产）

- 上游词级连续 morph/stage 尚未迁移；当前为块级过渡，不能声称完整复刻 TextFold。
- 真实模型已验证原文单次折叠→目标短层派生及既有译文只读复用；整书质量、费用和稳定耗时仍未验收。真实 S3（12 段）约 3 分 50 秒，速度仍需优化；无价格配置时费用未知。
- 真实客户端已用鼠标验证操作入口；当前生产构建的隔离交互回归、包内后端验收与 native 点验证据以开发日志最新阅读器条目为准，历史原型检查不替代生产证据。
- 公式（LaTeX 排版渲染）、图片内文字、语义/embedding 检索与问答 token 级流式尚未实现。
