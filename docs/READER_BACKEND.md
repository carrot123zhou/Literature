# 阅读模块后端 · 现状（唯一现状文档）

> 本文件描述后端契约；构建、真实模型与客户端交互的最新验收状态分别记录在 `docs/DEVELOPMENT_LOG.md`，不得互相代替。
> 本文只描述**后端与集成契约**；阅读器界面设计与交互契约见 `docs/ui/reader-design.md`（由 designer 维护，唯一现状）。
> 前端 UI 代码归 `frontend/src/reader/**`（designer）；本文的 DTO 与端点是双方的合同。

最后更新：2026-10-27。

## 1. 范围与边界

- 三栏对照（原件 / 原文 / 译文）、TextFold 五层折叠、批注与笔记、统一阅读问答。
- **只读事实来源**：`document/document.json`、`translations/<locale>/translation.json`、`ocr/raw/mineru/**` 一律不写。
- 阅读层是独立、可丢弃、可重建的：
  - `reader/overlay.json`：高亮/笔记/阅读偏好（带 revision，乐观并发）
  - `reader/layers.json`：五层折叠派生内容（`textfold/doc` v0 子集）
  - `reader/chat.json`：问答会话历史（按条冻结模型与引用快照）
- 删除 `reader/` 目录等于「清空阅读痕迹」，原文与译文不受影响。

**2026-10-27：移除本地摘句折叠。** `POST /reader/layers` 只接受 `mode="auto"`，`local` 返回 422；未配置模型不建作业、不写分层。模型失败或派生未覆盖的单元不补本地摘句，保留有效模型缓存及原全文，按未完成章节继续处理。旧 `local` / `translation-local` 标签仅用于识别旧缓存，不允许被当成 AI 成功；确定性 suppressed、分词及词锚计算保留。问答无折叠缓存时只用全文结构检索，不临时生成摘句。AIFold 默认关闭，用户显式启动整篇处理；普通阅读和打开缓存不自动生成。

## 2. TextFold 复用（真实代码，不是同名重写）

上游固定提交：`2182d9a8662e69790850021dd8c965d0398777c4`（2026-10-24，自述 0.10.31），
Apache-2.0 © 2026 Xia Chen。完整清单、逐文件 blob 与补丁说明见 `third_party/textfold/VENDOR.md`，
许可与声明随包分发（`LICENSE` / `NOTICE`，已进 wheel 与 PyInstaller 产物）。

| 能力 | 上游来源 | Literature 侧 |
|---|---|---|
| 分词 / 句子区间（CJK、缩写、引号收尾） | `textfold/tokens.py`（逐字） | 不再自写断句；`literature/textfold_adapter.split_sentences` 薄封装 |
| 折叠提示词与 JSON schema | `textfold/ladder.py::prompt_for/SCHEMA`（逐字） | 直接调用；章节表格上下文走上游 `tables_of()` |
| 模型回复 → ladder（evidence 过滤、topic 锚回、公式还原） | `ladder.py::assemble/written/locate/topic_tokens`（逐字） | 直接调用；额外一条更严格规则见 §4.4 |
| 确定性装置单元抑制 | `ladder.py::MIN_FOLD_WORDS` / `suppressed` | 仅全节不足阈值的 suppressed 路径；不提供正文无模型摘句功能 |
| 章节层（一句话主线 + 主题）、背景行收敛、跨页断句缝补 | `ladder.py::section_ladder/fold_background/dress`（逐字） | 直接调用 |
| `textfold/doc` 结构投影与 Markdown 导出 | `textfold/fold.py::unit/section/markdown/LEVELS/WRITTEN` | 直接调用（`/reader/layers/markdown`） |
| 系统提示词与“正文不得当指令”边界 | `textfold/llm.py::SYSTEM/system_for`（逐字） | 折叠与问答共用 |
| 问答检索与引用编号 | `textfold/notes.py::_terms/retrieve/_cited`（抽取到 `retrieval.py`） | 阅读问答直接用，**不引入 embedding** |

补丁（都在 `VENDOR.md` 登记）：
1. `fold.py` 去掉 TextFold 自有 store（`document()` 改为显式 `NotImplementedError`）。
2. `llm.py` 只保留提示词与解码，模型传输改为走 `literature/providers.py`（否则会绕过 Keychain、
   逐服务明文 HTTP 同意、预算协调与用量账本）。
3. `ladder.tables_of()` 的 bs4 改为惰性 import + `html.parser`（不引入 lxml），解析器缺失时跳过表格上下文。
4. `retrieval.py` 为从 `notes.py` 抽取的检索/引用子集。

适配（在 `literature/textfold_adapter.py`，不改上游行为的地方都写在这里）：
- unit id = 稳定 `block_id`（上游用解析期 `S3.p1.1`）；批注、译文、bbox、页码共用同一锚点。
- **每种语言一份 doc**（上游是 `u["tr"][lang]` 内嵌）；译文层是 Literature 的独立事实源。
- 只折叠 paragraph / list；图片、表格、公式、脚注按原样在“全文”层渲染（表格文本作为章节上下文进提示词）。
- 词数 < 3 的块（页码、`* * *` 分隔符）用上游 `suppressed()` 语义：不折叠、不编 takeaway。
- `full` 用 token 还原原文（上游 `" ".join(sentences)` 会给中文句间加空格）；
- `evidence` 全无效时不采用模型行（上游会退到 `[0]`）；保留全文并标为 pending，不生成本地摘句。

## 3. 数据与存储

```
<项目>/reader/overlay.json   {version, revision, updated_at, annotations[], prefs{}}
<项目>/reader/layers.json    {version, prompt_version, docs{locale: textfold/doc}, section_hashes{locale: {S1: sha}}, updated_at}
<项目>/reader/chat.json      {version, conversation_id, messages[], updated_at}
```

- 全部原子写（同目录临时文件 + `os.replace`），失败不留半份 JSON。
- overlay 写入是「读 revision → 校验 → 写」的读改写：进程内按**文件路径**共享 `RLock`（不是实例锁，
  API 每个请求都新建 `ReaderStore`）。过期 `base_revision` → **HTTP 409** + `code: reader_revision_conflict`。
- 批注规则：`id` 重复整次拒绝（避免改色/移除命中错记录）；`end <= start` 但**有笔记**时保留为
  `anchor: "block"` 的段落级笔记（不画 mark，不静默丢）；既无区间又无笔记则丢弃。
- 上限：overlay 1.5MB / chat 2MB / 单条笔记 4000 字 / 消息 20000 字；超限是**拒绝**，不是截断。

## 4. 折叠管线

### 4.1 章节与 unit

- 章节按标题块切分：`S1..Sn`（文档顺序），首个标题前的前言为 `S0`。
- `unit` = paragraph / list 块；`kind` 取上游 `KINDS`（`paragraph` / `list item`）。
- unit 字段：`id`(=block_id) `block_id` `kind` `type` `page_index` `full` `sentences[]` `fallback`
  `suppressed` `source` `evidence[]` `brief[]` `brief_text` `key` `takeaway` `topic` `role`。
- 章节字段：`id` `num` `title` `level` `block_id` `page_index` `takeaway` `topic` `source` `units[]`。

### 4.2 五层语义（与上游 `fold.unit` 完全一致）

| 层 | UI | 来源 | 不变量 |
|---|---|---|---|
| `full` | 1 全文 | 作者原话 | 与 `document.json` 正文逐字一致 |
| `brief` | 2 简述 | 作者原句（模型选 evidence） | `brief[i] == sentences[evidence[i]]`，逐字 |
| `key` | 3 要点 | 模型写出（takeaway + reason） | `key` 以 `takeaway` 开头 |
| `takeaway` | 4 主线 | 模型写出 | 章节层同样由模型写（`section_ladder`） |
| `topic` | 5 主题 | 从 takeaway 逐字取出 | 在 takeaway 里出现时按 token 序列锚定；不在时保留短词短语（上游同此） |

**`evidence` 是 0 起的句子下标**（上游 `fold.unit()` 的约定）。旧原型/旧测试里的 1 起写法已废弃。

### 4.3 译文层（target locale）：source-only 派生，不二次翻译

**用户硬约束（2026-10-25）：原文正文只在 source fold 入模一次；译文层只派生短层，不把整章原文或译文再给模型，
不重新全文翻译。** 实现路径：

1. `ReaderLayerService.generate(locale≠source)` 先 `_ensure_source_fold()`：对应原文节已有 AI 存盘投影时
   从投影重建 lad（**不发请求**）；缺失时才先折该原文节并同步写入 source 层存盘（与上游「先英文后翻译」
   次序一致）。
2. `textfold_adapter.derive_context()` 构建派生请求：**只含 fold 产物**——原文层的 role/takeaway/reason/topic、
   被选中证据句（逐字、短）、目标语章节标题（取自 translation.json 事实层）、截断到 40 条的术语表。
   不含整章原文、不含译文全文。
3. 回复 `apply_derived_reply()`：evidence 下标**原样继承**（不重新挑句）；`takeaway/reason/topic` 由模型
   用目标语重写（`written()`/`topic_tokens()` 锚定）；`ev_sentences`（**schema required**）逐条对应 evidence，
   作为译文层的 brief，标 `brief_source="derived"`（分层译句，非已存译文逐字摘录，可回溯：brief[i] ↔ evidence[i]）。
   Full 永远来自 translation.json，人工译文不被覆写。
4. 派生失败或遗漏单元不补 `translation-local`。章节如实未完成；Full 仍来自既有原文/译文全文。显式继续时补齐未完成部分，有效原文模型缓存不重计费。

译文层 DTO 新增：unit `brief_source: "derived" | "translation-local" | "source-fallback"`；
provenance 新增 `target_derived: true`、`strategy: "derive"`、`calls`（本层请求数）。

缓存键（`derive_section_digest`）：`(source 节 digest（同 service/model）, locale, 术语表哈希, service_id+model,
prompt_version)`——**不含译文文本**：译文文字编辑不重烧派生层（短层锚在原文），术语表变化才重建；
原文重折叠 → source digest 变 → 派生层精确失效。

**brief 与 full 的来源差异（必须区分）**：译文层 `full`/Key 句来自 `translation.json`（用户/既有译文，
可能逐字摘录）；`brief`（`brief_source="derived"`）是**模型对证据句的分层译句**，不是已存译文的逐字摘录，
不能当 exact quote 渲染，必须可回溯到原文 evidence 下标；`translation-local` 仅是旧缓存兼容来源标记，新作业不再生成。

**驱动与并发边界（不夸大）**：UI 契约是**单 AI driver**——有译文只调 targetLocale 的 startModelFold
（`generate(target)` 内部 `_ensure_source_fold` 补齐并同步 source 层）；无译文才调 source。API 级去重键是
`(project_id, locale)`，跨 locale 不互斥：source 与 target 作业同时跑时，`_ensure_source_fold` 会重读
最新存盘 source 层（逐节落盘后即可复用）。此外有 **section 级 single-flight**：同一「项目存储路径 +
原文节内容 digest + 服务/模型」的折叠在进程内只发一次请求，owner 在「结果验证 + 本节原子落盘」后才
signal；等待者醒来重读缓存（digest+来源校验）命中则 0 调用复用，未命中（首跑失败）则重新竞争 owner 重试。
不同书/章节互不阻塞（不持全局 reader 文件锁跨网络调用）；`force` 只跳过缓存预检（重新采样），仍走
single-flight，并发同 key 的 force 合并为一次新采样、不双倍计费。

**费用与 thinking 边界**：折叠/派生走与翻译相同的 `apply_thinking_policy`（探测一次后按 服务+模型
缓存复用，后续请求零探测开销）；实测 S9（关思考后）source fold in=4317/out=1141、derive in=2581/out=1510、
reasoning=0。整书费用靠显式点击 + 按节缓存 + 失败只补失败节控制，无固定「省半」比率承诺。

### 4.4 缓存键与失效

`section_digest = sha256(prompt_version, textfold_commit, locale, service_id, model, 章节标题, [(unit_id, 全文, 是否回退原文)])`

因此换模型、换服务商、换提示词、补译/重译、结构校正后的正文变化，都会让对应章节失效并只重折叠该章节；
未变化的章节复用，不发请求、不计费。`stale` 用**落盘时**的 digest 元信息判定，且**逐节按其存盘内容的真实来源**
选键（AI 节用模型键、本地/缺席节用 local 键），混合层不会假 stale；调用方不传 model 也不会永远 stale。

### 4.5 来源标注（不把本地生成伪装成 AI）

每个 unit 带 `source`（`model` / `pending` / `suppressed`；`local` 仅旧缓存兼容），每节按实际单元聚合来源。新作业不运行本地摘句。
`provenance` 字段：

| 字段 | 含义 |
|---|---|
| `ai_generated` | **真的有 unit 由模型写出**才为 true（空 units 回复 → false） |
| `local` | 历史来源兼容字段；新作业不提供本地摘句模式 |
| `content` | 内容实际来源：`model` / `local` / `mixed` |
| `ai_units` / `local_units` | 逐 unit 计数 |
| `partial` | 本次存在未完成项；展示保留当前全文及经校验有效的 AI 短层，不生成本地摘句 |
| `model` / `service_id` / `prompt_version` / `textfold_commit` | 可追溯元信息 |

作业 `result` 同步给出 `ai_generated / ai_units / local_units / content / partial / failed_sections / warnings / stopped`。

### 4.7 作业、停止与重启

- `POST /reader/layers` 立刻返回 `job_id`，折叠在后台线程跑（章节边界更新进度）。
- **去重键是 `(project_id, locale)`**，不是「整个项目只允许一个」：同一项目**同一语言**重复提交会返回
  既有 `job_id`（`created=false`，不重复计费）；**原文层与译文层是两份不同的工作，可以并行**（各自
  独立落盘、独立计费）。早期实现按 `project_id` 去重，导致 UI 连发 source + zh-CN 时第二种语言被当成
  重复提交挡掉（已修，tester 的浏览器验收曾因此红一项）。
- `POST /reader/layers/stop?locale=`：按项目停（可只停某一语言），在章节边界生效（当前在途请求跑完即停），
  已完成章节保留；没有在跑作业时返回 409。
- **逐节 checkpoint**：每节成功后立即原子落盘（`_write_json_atomic`），再发下一节请求；崩溃/停止最多丢
  「正在途中的那一节」，已完成章节全部可从新 `ReaderStore` 读回。写失败向上抛真实错误（作业 failed，
  不静默）。
- 重启恢复边界：进程重启时 `ReaderJobRunner.recover()` 把遗留 `running/queued` 折叠作业标为
  `failed` 并说明「已完成章节保留，重新折叠只补未完成的章节」——不宣称能续跑线程。
  （reader_fold_run 作业不同：重启后标 `paused` 可继续，见 §4.8。）
- **僵尸守护（2026-10-27）**：`start_run`/`resume_run`/`pause_run` 对磁盘快照为 `running/pausing`
  但无活线程的 run，先落盘纠偏为 `paused` 再续跑/暂停（与 `current_run` 的呈现一致）。
  启动时 `JobManager.recover_interrupted_jobs()` 会调用 `reader_jobs.recover()`，且通用恢复
  **跳过** `reader_layers`/`reader_fold_run` 两类作业（历史上只标 job、不修 run 快照，产生
  「继续无响应」的僵尸；见 2026-10-27 日志）。

### 4.8 整篇 fold-run（AIFold 生命周期，2026-10-25 新增）

**用户决策（取代“按可见节自动生成”）**：AIFold 默认关闭；点击顶部 AIFold 后自动处理整篇文章，
可暂停继续；全部应处理节齐备才 `ready`，`readable` 独立表示有效成果可供阅读。部分失败不再阻塞进入已有成果，但不能冒充全部完成。

- **单一目标 controller**：一个 run 只驱动一个 locale（有译文=译文 locale，否则 source）。
  有译文时 generate 内部先保原文层（缓存命中 0 调用）再派生短层——维持「原文一次 fold→
  目标短层派生、已有译文只读、不双全文投喂」预算硬约束。run 与按节作业同 `(project, locale)`
  互斥（复用 `_running` 键）；跨 locale 不互斥但同一时刻一个项目只有一个 fold-run
  （`fold_run.json` 单文件承载）。
- **状态机**：`idle → running →（pausing→paused | ready | failed）`；paused/failed 可 resume
  （同 `run_id`，`resume_count` 递增）。pausing 是中间态：暂停在**模型请求边界**生效，在途节跑完
  并落盘后转 paused；在途节完成后全书正好折完则直接 ready（不假装 paused）。
- **持久化**：`<项目>/reader/fold_run.json`（原子写）。run 记录含 `run_id/locale/status/binding/
  progress{total,completed,reused,failed,pending,ratio,current_section}/ready/ready_basis/errors/
  resume_count/job_id`。**暂停不产生空 id 的 failed 项**（与 stop 语义分离）；重启时
  `recover()` 把 running 的 run 标为 paused（reader_layers 作业仍标 failed），resume 只补未完成节。
- **ready 判据（零模型，`ReaderLayerService.fold_run_readiness`）**：逐节判定——存盘投影存在、
  每 unit 真实 `source=model`（suppressed 算已处理；**全 suppressed 节也算就绪**——无可折内容
  不算未完成，不死锁；local/混合不冒充）且存盘 hash 与按 run 绑定重算的期望键一致。
  译文层额外要求**源前置同样有效**（源节 model/suppressed 且源 digest 与 run 绑定一致）；
  无译文的节沿用原文层，不阻塞 ready。全部节 ready 且无 errors 且 **native 在盘** →
  `ready=true` + `ready_basis` 写明依据。**ready 不是终态豁免**：`current_run` 用廉价事实指纹
  （绑定 + layers/document/translation/native 的 mtime+size）门控完整重算，指纹变化即重校，
  变化即失效（状态转 failed，已完成章节保留，可 resume 只补失效节）。
- **pending 语义**：暂停时的 `pending_sections` 是「resume 需要再过的节」，不是「会发模型的节」——
  resume 重扫时有效缓存 0 调用复用，不重计费。
- **force 语义（2026-10-25 修正）**：`force=true` 跳过**所有**缓存预检、重新采样——包括目标层的
  派生调用**和源前置折叠**（此前 `_ensure_source_fold` 的 digest 复用不看 force，导致 force 只重折
  派生、源层走缓存，已修）；并发同 key 的 force 仍经 section single-flight 合并为一次新采样，
  不双倍计费。force 不产生「无暂停窗口的瞬间 ready」——真实重折有真实的请求窗口。
- **resume body 可省（2026-10-26）**：resume 沿冻结 run 继续——locale 冻结在 run 记录、
  绑定冻结、force 缺省 False，空 body 等价默认输入（UI「继续」发无 body POST 不再 422）。
  传 body 时仍走 `FoldRunInput` 校验（extra=forbid）；`locale` 与 run 记录不一致 400/409，
  **不能因此换绑定**。
- **绑定契约（如实陈述，不夸大）**：
  - run 的 binding 在启动时冻结并写入 `fold_run.json`；**resume 沿用冻结绑定**（API 按冻结绑定
    构建 provider 工厂，不重新解析角色）；换模型要换产物必须**显式重启新 run**（新 run 用当时的
    角色绑定解析结果）。
  - **全局换角色绑定不会自动让旧 run 的 ready 失效**：ready 产物按其冻结绑定自洽（缓存键含
    service/model，指纹里的绑定是 run 自己的冻结值）。旧产物继续可读；用户要新绑定产物就显式
    发起新 run。不要宣称「换模型自动失效旧 run」。
  - 缺失冻结绑定（绑定被删）的 resume 返回 400，不静默换绑。
- **确定性 suppressed 跳过（费用边界，2026-10-25 新增）**：节内**全部** unit 低于上游确定性抑制
  阈值（`tf_tokens.words < MIN_FOLD_WORDS=3`，与 `apply_reply`/`local()` 同一判据，非语义猜测）
  时，源前置与派生请求都不发（零模型费用），直接 suppressed 落盘；本地内容持 local digest
  （`_merge_source_section`/readiness 对称）。含真实正文内容的节**不会**被跳过。

### 4.8a 有界请求、逐单元恢复与书后材料（2026-10-27）

- 每批最多 12 个 unit，估算输入 ≤6000 token、输出 ≤4000 token；超长段按句/子句分窗后由模型归并，不截掉正文。节摘要在全部短层齐备后生成，过长时分层汇总；整篇完成后统一 finalize。
- 同服务跨项目最多两路请求；429 后暂降一路。每个请求独立连接与预算预留，首包/块间空闲 90 秒、总时限 180 秒；套接字看门狗中止卡住的读取。输出同时按语义流字节与实际 usage 校验。可能已计费的超时、5xx、断流不自动重发；明确未计费的连接失败/429 和协议兼容回退另处理。
- `reader/fold_checkpoints.json` 按 unit 保存已验证结果，键含原文、实际消费的上下文、服务/模型和提示词版本；批间落盘，只补缺失或失效项。旧目标层通过旧源指纹证明后迁入逐单元缓存，避免修一段而重烧全节。旧源段落缺短层时不播种该项，避免以空消费上下文覆盖已有有效目标检查点。恢复 ladder 精确保留原有短层文本，不追加标点。
- 索引/参考文献/丛书目录仅通过显式标题召回，再用首中尾有界样本交 AI 确认类型及非混合内容。确认后保留 Full、跳过短层；分类收据绑定原文/服务/模型/策略。缺收据或收据失效即重新校验，混有正文不抑制。目标层同步源层决定。
- `progress` 从 section_states 去重计算，completed+reused ≤ total；失败与暂停不累加旧次数。`activity` 只含阶段、节、零起批次、本节 lane 待补量、请求开始/最近响应时间、重试原因等元数据。流式回调实时刷新 waiting/generating；前端每秒推进请求计时，暂停停表。
- 真实 usage 与未知费用分开记录：带 response 的错误仍记已知用量；未拿到 response 的可能计费请求保留未知账项。未配置价格时 cost=null，不表示免费。恢复、迁入或缓存命中不能额外生成一份模型结果。

### 4.8b 词级 native 结构（上游 reader.js stage/morph 的数据源，2026-10-25）

- `GET /projects/{id}/reader/layers/native?locale=&section=` 返回与 **upstream reader.js
  textfold/doc 载荷同构**的原生词级结构（已对照真实 gallery fixture 与 `reader.js`
  `view()/pieces()/build()` 逐字段核实）：
  - 根字段：`meta`（id/title/source/lang/cjk + `langs` 可用语言）· `atoms` · `units`（dict）·
    `chunks` · **`blocks`（原文顺序）** · `edges` · `alias` · `app:true` · `langs`（显示名表）·
    `katex:"inside"` · `pdfAt:null` · `bib:null` · `gone:null` · `notes:null`；
  - `blocks` 是 reader.js `build()` 遍历的原序形状：`{k:"h",chunk}`（章节标题）·
    `{k:"u",u,chunk}`（unit）· `{k:"fig",id,kind:"table"|"image",html,src?,cap,chunk}`（图/表；
    图片带 `src` = asset 名列表，前端用 `/projects/{id}/assets/{name}` 拉取）·
    `{k:"eq",id,chunk,html}`（公式，转义纯文本，前端可用 katex 渲染）；
  - `units` 以稳定 block_id 为键：`{id,role(para/item/abstract),toks[{t}/{a}/{s}/{f}],sents[[起,止]],
    chunk,lad,tr}` + Literature 适配字段（`block_id/page_index/type/fallback/suppressed`；列表项带
    `list` 分组键）；译文 `tr[locale].{toks,sents,lad}`；`chunks` 含
    `{id,num,title,html,level,units,lad:{take,topic},tr:{lang:{title,lad}},block_id,page_index}`
    （章节级 `lad.take`/`topic` 由折叠管线写入，reader.js `headEls/stake` 直接可用）。
- **五层语义（目标 lane，勿混淆——上游同构）**：
  - `lad.brief` + `lad.tn` + `lad.topic`（词 token）是 **L2 Key / L1 Takeaway / L0 Topic** 的
    written line；`topic` 词带 `k`（锚进 brief 词序，真实锚定）；
  - **L3 Brief 不是 `lad.brief`**：derived 层的 L3 事实是 `lad.brief_sentences`（模型分层译句，
    与源 `ev` 1:1）；native 额外给确定性 DTO **`lad.derived_brief = {toks, sents, source_ev}`**
    ——由 brief_sentences 逐句 `tokenize_text` 分词、`sents` 为 toks 内句区间、`source_ev[i]`
    对应 `sents[i]`（=继承的源 evidence 下标）；**不带指向全文的 `m`**（派生译句非译文逐字，
    无对齐不伪造）；
  - `lad.ev_space`：`"source"`（derived：ev 是原文句下标，只用于回跳原文证据，**禁止索引
    z.sents**）或 `"target"`（translation-local：ev 由上游 `local()` 对译文文本算出，可索引
    z.sents）；上游 `view()` 的 `z.sents[ev]` 捷径只在 `ev_space=="target"` 时成立；
  - `tr[locale].fallback=true`（无译文 unit）：toks 即原文 token，全部层回退渲染原文，
    不套目标 sents、无派生；
  - 词级 morph：`m`/`k` 只在词真实共享时存在；derived 层 brief/derived_brief 大多无 `m`
    （模型自拟译文），pieces() 走无 `m` 的 `w{j}` 淡入路径——不伪造对齐、不改已有译文、
    不增加全文模型调用。
- **resume/缓存节的 lad 重建**：缓存命中的节在 merged doc 里只有 `ensure_ladders` 兜底层；
  `generate()` 在收尾前按存盘投影确定性重建（`_restore_target_lads`/`_restore_source_lads`）：
  derived 从 takeaway/reason/brief_sentences 字符串重建（同一构造，topic 用 `topic_tokens`
  真实锚 `k`）；translation-local 走 `local()`；suppressed 传播 `suppressed()`。新折节不动
  （其 lad 是 assemble 的真实产物，重建反而会丢 `m` 锚）。
- **suppressed 传播**：源层 suppressed unit（页码/分隔符等装置内容）在目标层同步标
  `suppressed`（`apply_derived_reply` + `_sync_source_lads` + `section_view`），不再被投影成
  `local`——带装置内容的节不会因此永不 ready。
- **消毒**：所有 html 字段（chunk.html/表格 raw table_html/eq html/atom html）统一过
  `_sanitize_html`：剔除 script/style/iframe/object/embed/link/meta 标签、`on*` 事件属性与
  `javascript:`/`vbscript:` 伪协议；转义后的惰性文本（如 `&lt;script&gt;`）不是执行风险。
- **存储**：fold 过程同步写 `reader/native_<locale>.json`（展示 locale 必写；译文 run 同步写
  原文层；写失败只记 warning 且 **该 run 不能 ready**——`current_run` 复验含 native 在盘校验）。
  `?section=S3` 按节过滤 chunks（units 全量返回保词身份）。缺失时 404。
- 现有 `GET /reader/layers`（string 投影）不变，旧 UI 兼容。

### 4.8c 可阅读与全部完成分离（2026-10-27）

- `ready` 保留严格的全部完成语义；run 新增 `readable` 和 `availability`。`readable` 独立于 running/pausing/paused/failed/ready：只要当前事实与冻结绑定能验证至少一个 AI 短层槽位，即可进入已有成果；全合法 suppressed 且无待补内容也可读。全部失败无有效成果时不假称 AI 可读，普通全文一直可用。
- `availability` 计数以源+目标语言的短层槽位为单位：`units_total = units_valid + units_pending + units_suppressed`。同时给 `source_units_*` / `target_units_*`、`summaries_total/pending`、`source_summaries_pending` / `target_summaries_pending`，以及 `full_text`、`basis`、`checked_at`。缺译的原文回退不是目标派生失败。界面不得将「可读」写成「全部完成」。
- `ReaderLayerService.reading_projection()` 以当前 canonical/translation 全文为底，只还原匹配当前事实/冻结绑定的节缓存或逐单元检查点；过期短层不展示，未变的有效单元独立保留。原文部分失败不再整节堵住有效单元的目标语派生；章节摘要缺失不阻塞段落阅读。事实变化时旧节摘要不直接复活；继续生成时，摘要检查点仍按实际消费的全部单元上下文及冻结绑定校验，输入未变可复用，不要求为变化过的整节无条件重新调用模型。没有 local 摘句或默认首句补位。
- native 单元 `lit_pending` 与章节 `lit_summary_pending` **恒指原文**；各自 `tr[locale]` 内同名键恒指目标语，不能随请求 locale 改变顶层含义。失败单元在所有五层以 Full 词空间渲染当前全文/已有全文译文，批注也使用 Full 锚，不伪造短层对应。
- `GET /reader/layers/native` 调用 `reading_native()`。事实文件、缓存、检查点、冻结绑定及投影版本的廉价指纹一致时复用安全 native；不一致或文件缺失时从安全投影重建，零模型调用。未生成的项目也可返回纯全文投影（`lit.readable=false`），无规范文档才返回 404。
- 根 `native.lit` 含 `partial/readable/full_text/pending_units/pending_summaries/rebuilt/checked_at/facts_fingerprint/availability`。run 对各作业状态都按事实指纹刷新可用性，running 最多每 15 秒重算、终态立即刷新，指纹未变不反复构造全书；读路径不重新解析全局角色来覆盖 run 冻结绑定。
- 生成路径原始 native 一律不写可信事实指纹，首次 GET 必经安全重建后缓存，避免仅凭有效数量误信旧内容。字符串恢复的源层与原始词级产物可能存在 token 细节差异，导致个别派生检查点失配：此时标 pending，继续时重新派生，不冒险显示未验证短层。
- 模型回复仍按上游 `units_of` 兼容可确定归一的容器形式；单元字段异常只使该单元 pending，合法同批单元继续接收；章节字段异常只影响摘要。不以裁切译句、默认第一句或推测对应关系修复内容错误。

### 4.9 短层笔记原生锚（`engine_anchor`，2026-10-26，**单一契约**）

批注（overlay annotation）可选字段 `engine_anchor`：上游 textfold/notes v1 anchor 的版本化持久化，
用于同产物同 locale 重开 fold 时精确复原短层写行/派生空间划词（block+quote 只是普通阅读回退）。

**信封（与前端 types.ts 对齐，后端为权威 schema）**：

```json
{"version": 1,
 "locale": "zh-CN",                       // 锚所在语言（pane=trans→目标 locale；source→源语言）
 "space": "full|brief|derived",           // token 空间（数组名，非 UI 层名）：
                                          //   full=unit 全文 toks（译文 lane=tr[locale].toks）
                                          //   brief=lad.brief 写行（Key/Takeaway，shown 所在空间）
                                          //   derived=lad.derived_brief.toks（L3 分层译句）
 "digest": "djb2 8hex",                   // 空间 token 流指纹（见下），产物变化即失配
 "thread": [],                            // 可选：会话线程信息（≤64 项）
 "anchor": {                              // 上游原生 anchor 逐字快照（白名单字段）：
   "on": "unit|summary|title|paper", "lang": "...", "level": 0..4,
   "unit": "<block_id>", "section": "S3", "tokens": [lo,hi], "sents": [k,…],
   "quote": {"exact","prefix","suffix"}, "shown": {"words":[x,y], "exact": "词行文本"}
 }}
```

- **digest 算法（前后端同一实现，含跨语言 golden 测试）**：`tokenSpaceDigest` —— 逐 token 纯文本
  （`tokPlain`：atom math→`$alt$`、其他 atom→text、`x`→`$x$`、否则 `t`）按字符 djb2
  （`((h<<5)+h+code)>>>0`），**每 token 后 `h=(h+1)>>>0`**（词界敏感：同字符不同分词即不同），
  32 位无符号 → 8 位小写 hex。**字符码取 JS charCodeAt 语义（UTF-16 码元，非 BMP 字符拆
  代理对）**——Python 端 `_js_char_codes` 同一拆分，emoji/古文字笔记不因语言差异失效。
  后端 `token_space_digest()` 同实现；**保存时重算**，与 envelope.digest 不一致 = 产物已重折/重建
  → 锚失效。跨语言 golden 值（ASCII/CJK/emoji/古文字/数学 atom/行内公式/空格/词界，真实 JS
  运行取值）钉在 `tests/test_reader_engine_anchor.py::test_golden_digest_cross_language_with_js`。
- **一致性校验（违反即丢弃扩展）**：`anchor.on=="unit"` 时 `anchor.unit` 必须 == annotation.block_id；
  `shown` 存在 → space 必须 `brief`；`space=="brief"` → 必带 shown；`space=="derived"` →
  `anchor.level==3`、无 shown、**无 sents**（derived 不得冒充 Full sents）。**`space=="full"`
  不限 level**：full 指 token 空间——源 Brief/translation-local Brief 的原生 tokens 同样引用
  Full toks（level3、无 shown，合法）。
- **写行锚的三字段共存（2026-10-26 依真实上游 selbar 修正）**：自然 Key 锚实际为
  `{unit, section, lang, level:2, tokens:[5,6], sents:[0], shown:{words:[4,14], exact:…}}`——
  `shown.words` 是**写行位置**（对 `lad.brief` 校验），`tokens`/`sents` 是对应 **Full 来源范围**
  （对 unit/tr 的 toks/sents 校验），三者共存且分别验证，不互斥、不剥离。
  **前端接线状态（2026-10-26，A/B 端到端已验；目标栏布局修复后 7 空间场景 80 断言全绿）**：
  ⚠️ 验收脚本历史假阴性两处（已修）：① fold 视图高亮是 `span.tk.hl`（非 `<mark>`）；
  ② CJK 逐字 span 连接带空格（命中判定需去空格比较）。
  另：`sanitize_overlay` 的 `prefs.panes.trans` 缺省曾为 False（origin/source 为 True 的
  不对称）——空 overlay 首读即目标栏 hidden、rects 全 0、目标 lane 选区全失效；已改 True
  （三栏对称，缺省全开）。`makeEngineAnchor` 支持 on=summary/title
  （digest 分别取该节 take 流/标题文本，anchor 逐字快照 + lang=lane locale）；`engineAnchorUsable`
  章节校验（chunk.block_id==annotation.block_id + digest + tokens 范围 + quote 逐字/包含）；
  失效 summary 绕过已移除——失效锚统一回退 block+quote（不误标）。端到端（受控 provider 隔离库）：
  源 Key 写行 marks 刷新恢复（span.tk.hl hlk 6 词）、章节 summary stake marks 恢复（hit=true）、
  title 存续不误标。⚠️ 验收脚本注意：内核 fold 视图高亮是 `span.tk.hl`（非 `<mark>`），逐词
  span——断言需按词序列连接匹配。
  **校验目标分空间（token 索引空间跟随 space，2026-10-26 修正）**：
  `full`/`brief` 的原生 `tokens`/`sents` 是 **Full 来源范围** → 对 Full lane（unit/tr toks/sents）
  验范围；**`derived` 的 `tokens` 是 `derived_brief` 独立空间（不是 Full）** → tokens/quote/digest
  都对 derived_brief.toks 验（长于/短于 Full 都按自身长度验，sents 已拒绝）。`digest`/`shown`
  对**所在空间**的 token 流验。**quote 按真实上游规则全量一致**：`quote.exact == 所在空间区间
  toksText`（full/derived/summary 分别用所在空间核验；受 500 字存储上限截断比较，不擅改截断点；
  brief 的 quote 是写行选段文本、已由 shown.exact 全等覆盖，不重复验）——不是 120 前缀包含。
- **章节级锚（`space:"summary"|"title"`，2026-10-26 新增——上游 stakeMarks/selectionAnchors 语义）**：
  自然形态 `{on:"summary", unit:null, section:"S1", lang, level:1, tokens:[0,2], quote:{exact:…}}`——
  `tokens` 索引 **`chunk.lad.take`（译文 lane=tr[locale].lad.take）**，quote.exact=take 区间 toksText
  （全量一致）；`title` 无 token 流（tokens 必须缺省），quote.exact 对标题文本包含核验，digest 对
  `[{"t":标题文本}]` 计算。**归属用真实稳定标题 block_id**：annotation.block_id 必须等于 native
  `chunk.block_id`（不用 unit=null 造假锚；缺章节/tokens 越界/digest 失配/block_id 归属不符 →
  失效保留批注）。summary/title 锚不进 unit Full marksFor（内核 stakeMarks 消费）。
- **索引/长度/类型**：tokens `[lo,hi]` 0≤lo≤hi≤200000；sents ≤256 项（0..100000）；shown/quote
  文本 ≤500（prefix/suffix ≤120）；字符串拒绝 NUL/控制字符——**按纯文本存储，前端必须用
  textContent 渲染，不得 innerHTML**（不接受任意 HTML）；thread ≤64 项。
- **失效语义（不伪恢复，native 缺失不放行）**：保存时（`_validate_overlay_blocks`）按锚的 lane
  加载 native（源语言码/`"source"` 归一到 `native_source.json`；译文 locale 用 `native_<locale>.json`
  ——**找不到即失效**，不跳过校验放行）；重算空间 digest、索引范围、shown 词行文本、**quote 必须落
  在实际所选 token 区间文本内**、**sents 不得超出实际句数组**（校验对象都是实际所选空间，不是整流
  包含前缀）。任一不符 → **丢弃 engine_anchor、保留批注**（回退 block+quote 展示，笔记不丢失）；
  重锚由前端在新产物上带新 digest 重新提交。lane 越界（pane=source 带译文 locale、反之）同样失效。
- **thread 有界白名单**：turn 仅 `{q(≤2000,必填非空), a(≤20000), lang(≤12), error(≤200),
  cites(≤32×120)}`——`pending`/`demo` 是瞬态字段不持久化，未知/深层字段丢弃；≤64 项；
  非法 turn（无 q/非 dict/cites 超限）整个锚丢弃。
- **兼容**：旧记录无 `engine_anchor` 原样保留；不合法扩展丢弃不报错（normalize 不迁坏）。

## 5. 模型角色

`MODEL_ROLES = (structure, translation, reader_fold, reader_chat)`；旧配置缺新角色时为空绑定。

- 解析顺序：请求显式 `service_id + model` → 该角色绑定 → **`translation` 绑定兜底**（升级后立即可用）。
- **折叠门控看 `reader_fold`**（不是 translation）：只配折叠模型、没配翻译模型时折叠照常可用；
  未绑定时 400 文案指向「设置 → 阅读 → 折叠默认模型」，并提示可改用本地摘句。
- 对话每次请求都可带 `service_id + model`（聊天输入区的模型选择器）：**只影响下一次请求**；
  在途请求用它创建时冻结的绑定；消息落库时记录**实际响应**的模型（展示名 + 真实 `service_id/model`）。
- 同名模型在不同服务商下靠 `service_id` 区分；等价性/回退绝不跨服务商静默替换。

## 6. API 契约

能力标记（`/health`）：`reader_v1`、`reader_v2`、`reader_chat_v1`、`reader_roles_v1`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/projects/{id}/reader/state?locale=` | overlay + layers 摘要 + 最近折叠作业 + 文档元信息 |
| GET | `/projects/{id}/reader/layers?locale=` | `textfold/doc`（`sections[]`）+ `stale/generated/section_count/unit_count/digest` |
| POST | `/projects/{id}/reader/layers` | `{locale, mode: "auto", service_id?, model?, force?, section_ids?}` → `{job_id, created}` |
| POST | `/projects/{id}/reader/layers/stop` | 章节边界停止（无在跑作业时 409） |
| POST | `/projects/{id}/reader/fold-runs` | 整篇折叠 run：`{locale?, force?}` → `{run_id, created, run}`（幂等；paused 即 resume） |
| GET | `/projects/{id}/reader/fold-run` | 当前 run 快照（无记录时 `{status:"idle"}`） |
| POST | `/projects/{id}/reader/fold-runs/{run_id}/pause` | 请求暂停（边界生效）；不存在 404；已结束返回 `accepted:false` |
| POST | `/projects/{id}/reader/fold-runs/{run_id}/resume` | 继续只有 paused/failed 可用（否则 409）；**body 可省**（无 body = 默认输入；传 body 仍 extra=forbid，不能换 locale/绑定） |
| GET | `/projects/{id}/reader/layers/native` | 词级原生结构（见 §4.8b）；缺失 404 |
| DELETE | `/projects/{id}/reader/layers?locale=` | 删除派生层（可重建） |
| GET | `/projects/{id}/reader/layers/markdown?level=&section=` | 上游 `fold.markdown()` 只读投影 |
| GET/PUT | `/projects/{id}/reader/overlay` | PUT body `{overlay, base_revision}`；冲突 409 |
| POST | `/projects/{id}/reader/ask` | 同步问答：`{question, locale?, service_id?, model?, quote?, block_ids?}` |
| GET | `/projects/{id}/reader/chat` | 会话历史（wire DTO，见下） |
| POST | `/projects/{id}/reader/chat/messages` | `{question, locale?, service_id?, model?, quote?}` → `{user_message_id, assistant_message_id, chat}` |
| POST | `/projects/{id}/reader/chat/messages/{mid}/stop` | 停止等待并丢弃迟到回答 |
| POST | `/projects/{id}/reader/chat/messages/{mid}/retry` | `{locale?, service_id?, model?}` → 新回答（同一提问/引用） |
| DELETE | `/projects/{id}/reader/chat` | 清空会话 |

### 6.1 `quote`（一次性引用快照）

```json
{"block_id": "blk-00001234", "pane": "source|trans", "text": "读者选中的文字"}
```

- 证据永远取**原文** block；`pane="trans"` 只表示用户在译文栏选中，服务端不伪装精确回译。
- 引用只随这一条消息持久化；发送后前端清空待发送引用；下一条默认面向全文。

### 6.2a 显式模型路径（缺陷 D1 已修，2026-10-25）

- 前端 `useReaderChat.send()` 必须把选中模型解包成**顶层** `service_id`/`model`（后端 DTO 只认顶层）；
  早期版本把 `{binding:{...}}` 整体发出去，pydantic 静默丢弃未知字段 → 实际回落到默认角色模型。
- 后端 `ReaderChatInput` 现在 `extra="forbid"`：字段漂移直接 **422**，不会再有「选了 A 实际跑 B」的静默错误。
- `binding.model` 是**路由身份**（重试/复用用它），服务端回报的实际模型名放在 `binding.actual`（仅展示）。
  早期实现把 `binding.model` 覆盖成响应里的 `Qwen`，导致重试发一个服务商下不存在的 model → 400。
- 重试默认**沿用该条消息自己的模型**（不重新解析默认角色），除非请求显式指定。
- 回归证据：`scripts/reader_model_picker_check.py`（拦截 `POST /reader/chat/messages`，不调模型）断言
  请求体为 `{question, locale, quote, service_id, model}` 且无嵌套 `binding`。

### 6.2 聊天 wire DTO（`frontend/src/reader/types.ts` 的 `ReaderChatMessage` + `binding`）

```json
{
  "id": "...", "role": "user|assistant",
  "question": "（user 的消息；assistant 也带上它对应的提问）",
  "answer": "（assistant 的消息）",
  "quote": {"block_id","pane","snippet","srcText","sec"},
  "citations": [{"block_id","page","section","text"}],
  "model": "服务名 · 模型名（实际响应模型）",
  "binding": {"service_id","model"},
  "state": "sending|done|error|stopped",
  "error": "", "created_at": ""
}
```

`state`：`sending` = 服务端生成中（前端轮询 `/reader/chat`，默认 700ms）。没有 token 级流式，
界面**不得**声称流式输出。

### 6.3 问答结果（`/reader/ask`）

`{answer, citations[], context_blocks, model, provider, usage{...}, job_id}`。
`answer` 内的引用标记统一为 `[#block_id]`（前端渲染可点击 chip）；不存在的 id 从正文中清除，
`[[?]]`（模型自己的推断）被移除。`citations[]` 只含真实存在的 block。

### 6.4 UI 接线清单（designer 侧需要读的字段）

| 需求 | 字段 |
|---|---|
| 显示「实际在用哪家模型」+「继承翻译默认」 | `GET /reader/state` → `roles.fold` / `roles.chat`：`{role, source, binding:{service_id,model}, problem}`；`source ∈ reader_role \| inherited_translation \| unset \| cleared \| invalid` |
| 折叠/对话的来源提示（历史消息也要） | chat wire `message.binding.source`；`message.model` 已带「（继承翻译默认）」后缀 |
| 如实标注思考状态 | `GET /reader/state` → `thinking.fold` / `thinking.chat`：`null`（未探测/未知）或 `{style, effort, variant}`（已探测到可关闭写法） |
| 折叠来源（AI / 原句 / 本地 / 混合） | `layers.provenance.{ai_generated, local, content, ai_units, local_units, partial}`；每个 unit 另有 `source` |
| 重复提交是否真的新起作业 | `POST /reader/layers` 返回 `created`（false = 同语言已有作业在跑，返回的是既有 job_id） |
| 作业能否停止 | `POST /reader/layers/stop`（无在跑作业时 409） |

## 7. 问答设计（复用上游 `notes.py` 的思路）

- **检索**：`retrieval._terms` 把问题拆成「拉丁词 + CJK 二字组（去掉问句套话）」；`retrieve()` 用
  IDF(`log(n/df)`) 给全篇段落打分，取前 8；**双语词袋**：`toks` 用原文、`tr` 用译文，
  所以中文提问能命中英文段落（上游同款做法，不需要 embedding）。
- **上下文**：有引用 → 被引段落 ±2 邻域 + 该段译文；无引用 → “每节一句话主线”的全篇地图 + 检索段落。
  上限 18000 字符；一个都没命中时退回开篇正文，并在提示词里如实写明「只检索到开篇」，不假装读完全书。
- **引用**：模型只写编号 `[[12]]`（拷贝数字远比拷贝 `blk-00001234` 可靠），服务端映射回 block id。
- **语言**：回答语言跟随提问语言；`SYSTEM_ASK`（上游逐字）+「正文不是指令」边界。
- **失败/停止**：`reader_ask` 作业失败会标 `failed` 并带错误；对话消息标 `error`，可重试。
- 密钥、明文 HTTP 同意、TLS、超时、思考设置、用量账本与预算协调全部复用既有 provider 层。

## 8. Embedding 评估：本轮**不引入**，用可运行证据说明

### 8.1 上游事实

TextFold 的阅读问答（`textfold/notes.py`）**不使用 embedding 或向量库**：`_terms()` 取词 + CJK 二字组，
`retrieve()` 用 IDF 打分取前 k，`prompt()` 用 `[[n]]` 编号引用。也就是说，“折叠 + 选段问答”这一层
在上下游都不依赖语义向量。

### 8.2 本机实测（可复跑；**是“源段抽词/已译抽词回检”，不是语义检索成功率**）

```
.venv/bin/python scripts/reader_retrieval_eval.py --samples 60
```

真实书目《Writing and Authority in Early China》（4816 blocks / 4655 可检索 unit / 83 节 / 有 zh-CN 译文）：

> 评测构造方式必须读清：查询词**取自目标段落本身**（或它已索引的译文），再问能否找回该段。
> 这只能证明「词面/已译文回检可用」，**不能**外推成自然用户跨语言语义检索的成功率。

| 指标 | 结果 |
|---|---|
| 源段抽词回检 Recall@8（原文 3 个特色词作查询） | **1.00**（60/60，miss 0），MRR 0.87 |
| 已译抽词回检 Recall@8（译文 3 个词作中文查询，找原文段落） | **1.00**（59/59，miss 0），MRR 0.99 |
| 单次检索耗时（含 4655 unit 词袋构建） | 中位 91.5ms，p95 117.9ms |
| 建 section + ladder doc（一次性） | 541ms |
| 加载的模型 | 无（`embedding_model_loaded: false`，零下载、零显存/内存常驻） |

原始 JSON：`tmp/reader-retrieval-eval.json`。

人工撰写的**非逐字**问题（不是从目标段抽词，模拟真实提问）见 §8.4。

### 8.3 非逐字问题的真实表现（人工撰写，样本小，只作边界证据）

`.venv/bin/python scripts/reader_retrieval_eval.py --questions` → 9 条人工问题（中/英混排；先读段落再用自己的话提问，
不复用该段特色词），逐条记录「期望段落是否进 top-8」：

| 结果 | 说明 |
|---|---|
| Hit@8 = **0.778**（7/9） | 中文跨语言 4/4 命中；英文改写 3/4 |
| 漏检 1 | EN「Why was Confucius suspicious of words…」——正文用 master/language/problem 等词，问句措辞不重合 |
| 漏检 2 | ZH「为什么要同时具备文与武的能力…」——跨语言 + 改写叠加 |

清单：`tmp/reader-retrieval-questions.json`。这就是「本轮不声称语义检索已解决」的直接证据；
榜单里 1.00 的那两行只说明**抽词回检**可用，不能替代这两个漏检。

### 8.4 与 tester 那份 0/4 的分歧：**待独立复核**，我不断言其结论错误

`tests/manual/reader_natural_query_eval.py`（tester）在《Between Cultures》上报 `natural_questions 0/4`、
`synonym_paraphrase 0/4`。**这是待复核的分歧项，不是已定论**。以下只列可复现的事实，判定权在 tester / Lead：

1. 该脚本用 `find_block(r"Burton", min_len=120)` 把问题钉到「第一个含 Burton 且 ≥120 字」的段落，
   实测得到的是 **`blk-00000033`**。该段正文是**美国国会图书馆主题词表**：
   `"6. Burton, Richard Francis, Sir, 1821–1890. 7. Lawrence, T. E. ..."` —— 它是书目元数据，
   既没有书名也没有「去麦加后写了什么」的答案。
2. 同一问题的 `retrieve(..., k=8)` 结果里**包含 `blk-00000040`**，该段正文含
   `"Early in September 1853, Richard Burton arrived in Mecca... Personal Narrative of a Pilgrimage to
   Al-Medinah and Meccah, the work that made his reputation as ..."` —— 即它**直接给出了书名**。
3. 因此「0/4」至少有一部分来自**目标段选取**（钉在元数据块上），而不是检索没找到能回答的段落。
   但这仍不足以宣布 tester 的结论无效：`blk-00000040` 是否「足以回答问题」需要按问答口径独立判断，
   而且另外 3 条用例的目标段我尚未逐一核对。

口径纪律（本条不放松）：
- **不把「命中同章节」或「关键词同章」算作命中**；只认「能回答该问题的段落进入 top-k」。
- 我自己的 9 条人工改写问题（§8.3，`--questions`）按同一纪律统计，得 7/9，两条漏检如实保留。
- 无论 §8.4 如何裁定，§8.2 的 1.00 仍只代表「抽词回检」，§8.5 的「本轮不引入 embedding + 语义漏检边界」不变。

### 8.5 结论与边界

- **普通阅读、五层折叠、选段问答不依赖 embedding**：现有词面 + IDF + 双语词袋已满足“找到能回答的那几段”，
  且延迟与体积成本为零。
- **已知限制**（不掩饰，且不被 §8.2 的 1.00 掩盖）：同义改写/纯语义相似会漏（见 §8.3 的非逐字问题集）；
  没有译文的书（或尚未翻译的章节）跨语言检索等于单语。
- 因此：「本轮不引入 embedding」是对**当前可用路径**的技术选择，不等于「未来不需要语义检索」；
  用户反馈出现真实语义漏检时应按 §8.5 末尾的条件重新评估。
- 因此本轮**不下载、不实现** embedding 索引；`reader/` 保持“删除即可重建”的纯文本派生层。
- 若将来采用（触发条件：真实用户反馈出现语义漏检，且译文覆盖不足），建议的落地约束：
  1. 只做**可选**本地索引，保留现在的零下载检索路径为默认；
  2. 候选模型需按模型卡核对许可证/维度/体积（例如多语小模型 multilingual-e5-small、bge-m3 一类，
     具体版本与大小以模型卡为准，本文不写未核实的数字）；
  3. 索引与模型放 `reader/index/`（可重建），索引入口绑定「模型版本 + 维度 + 文本版本」，任一变化即重建；
  4. 用户显式点击才下载，真实进度/取消/完整性校验/失败重试，半下载不得显示已就绪；
  5. 禁止 `trust_remote_code`，不执行下载仓库里的代码；下载只出网取权重，不携带任何项目文档。

## 8.6 关思考策略：探测后真实生效（本机实测）

阅读折叠与翻译/结构校对走**同一套**关思考策略（`apply_thinking_policy` + 按 `服务+模型` 缓存的字段写法探测），
不在阅读模块里另造字段。开启 `allow_probe=True` 后，本机真实网关（`9b83b387…/qwen3.8-flash-next`）实测：

| 阶段 | 请求数 | output token | reasoning token | 占比 |
|---|---|---|---|---|
| 关思考探测（3 个候选写法，极小请求） | 3 | 201 | 192 | 95.5%（候选未生效的那几次） |
| **探测命中后的折叠（1 节 25 unit）** | 1 | 2073 | **0** | **0.0%** |
| 探测前同一服务的折叠（历史两轮） | 2 节 | 12719 / 11752 | 10178 / 8306 | 80% / 71% |

即：该网关只认 `chat_template_kwargs` 变体；只发默认 `enable_thinking:false` 时思考照旧全开。
探测结论按服务+模型缓存，后续作业零额外成本；探测请求本身也进用量账本（`scope=probe`，单价缺失时费用如实为未知，不写 0）。
`/reader/state.thinking.{fold,chat}` 返回**已探测到的**写法（null = 未探测/未知）：界面只能据此标注，
不得默认宣称「已关闭思考」。用户显式设置的思考档位始终优先（`with_thinking_disabled()` 会直接返回原对象）。

## 9. 冻结与验收证据

`runtime_revision` 是源码指纹的近似标记，不能代替包内字节身份。每次实质变更应重新记录生产源码与实际 served 前端资产 SHA256，再构建 sidecar/客户端，核对包内 sidecar 与 manifest 的 SHA/size，以及内嵌前端文件名。已安装包与候选包分别记录，不把候选包自动视为已安装。

逐轮版本和验收边界只写 `docs/DEVELOPMENT_LOG.md`，本处不再维护易过期的第二份源码哈希表。本轮证据为 `tmp/reader-partial-final-pytest.log`、`tmp/reader-partial-result.json`、`tmp/reader-partial-package-check.json`；`tmp/` 是本机验收产物，复验需重新生成。当前冻结可执行契约使用 `tests/manual/reader_frozen_contract_check.py --mode binary --binary <sidecar>`，不沿用旧 local 折叠脚本作为放行门。

### 9.1b 引用证据来源（早前 `quote_evidence_is_source:false` 的复核结论）

早前 `/tmp` 里的临时脚本用硬编码 `"Silver" in citation.text` 判定来源，在《Between Cultures》上必然为 false，
**这是脚本判据写错，不是后端把译文当证据**。正确判据与证据（`scripts/reader_real_chat_check.py`，默认离线、不调模型）：

- 判据：`citation.text`（去掉摘要省略号）必须逐字出现在对应 block 的 `source_text`；不得等于该 block 的译文；
  引用正文不应出现中文（除非原文本身是中文）。
- 实测（引用来自**译文栏** `blk-00000040`，5 条 citation）：`all_source_derived=true`、
  `any_translation_as_evidence=false`、citations 全英文、提示词里引文段为原文
  （`Early in September 1853…`）且译文另附并明确标注语言。

### 9.1 模型选择器缺陷的可复跑证据

Lead 实查 `ModelPicker.tsx` 两处缺陷，我已写成可跑的浏览器验收 `scripts/reader_model_picker_check.py`
（真实生产构建前端 + 真实本地 API + 隔离库；只读复制生产 `model-services.json`，再追加一个「有模型、无密钥」
的合成服务来构造 disabled 项；不调用任何外部模型）：

- **缺陷 1 已复现**：输入服务商名「无密钥服务」后，选择器里**可见项为 0**——`flat`（键盘索引）把
  `service.name` 纳入匹配，而分组渲染的过滤没有纳入，两者不一致，于是「搜得到却看不见」。
- **缺陷 2 的断言已就位**：`data-active` 落在 `disabled` 项时按 Enter，选择必须不变；修复前因可见项为 0
  该分支跳过，修复后会自动生效。
- 脚本还有一条正例：键盘 Enter 选中一个可用项后弹层关闭且按钮文案更新（当前 **PASS**）。

### 9.2 最终审查产物（2026-10-25 09:02 构建，唯一最终口径）

冻结树：`ReaderWorkspace.tsx` sha256 `ae70b928…`（60299 B，08:53）——designer 明确不再改。

| 项 | 值 |
|---|---|
| app | `src-tauri/target/release/bundle/macos/Literature.app`（60 MB；`Contents/MacOS/literature` sha256 `1f7601953a1bf6dc0f7f6a27793d9a5292a1db4ce6b6cc2a301a3f0572a5abe9`，13,209,952 字节） |
| 包内 sidecar | sha256 `241927c4cacb1d60598e88cb1b8931e2a510f3fef0d8fc4baa7bfc5f39bdf692`，48,744,720 字节（== `src-tauri/binaries/manifest.json`；`runtime_revision 9089521e5ebe7bed8f50`） |
| 内嵌前端 | `assets/index-jZBi_Goc.js`（`6bb1306f2ee3429f7c658c6d67fadfe163f2a67e1b7210c73e1c22ca263e4b65`）+ `assets/index-DS-1nv-q.css`（`bb475e8d9a15ec9d9a92a720e80b25519a0435e74397deb2ac6f380d110ca632`），与 `frontend/dist/assets` 同名文件一致 |
| 签名 | adhoc（`codesign --verify --deep --strict` 通过；`Identifier=local.literature.workbench`，`TeamIdentifier` 未设置） |
| 冻结验收（包内 sidecar） | `scripts/reader_frozen_check.py` → **18/18 PASS** |
| 选择器验收 | `scripts/reader_model_picker_check.py` → **6/6 PASS**（含 D1 回归：请求体顶层 `service_id/model`、无嵌套 `binding`） |
| 溢出验收 | `scripts/app_shell_overflow_check.py` → 5 路由 × 320/390/768/1024/1440，页面级与阅读器内部**全 0** |
| 设计端联调 | tester 独立生产 dist 连续两轮 **40/40**（D1 真实 svc-other 路由、D2 stopped 重试、D3 缺译徽章），本包基于同一冻结树构建 |

**注意（字节身份 vs revision）**：`runtime_revision` 只反映源码指纹，不能代替字节身份；PyInstaller onefile
会嵌入构建时间，同一份 Python 源码重建也会得到不同的 sidecar 字节。复验必须比对
「包内 sidecar 的 sha256/size == 当时的 `src-tauri/binaries/manifest.json`」。


## 9. 验收状态（分开陈述）

| 项 | 状态 |
|---|---|
| 代码 | 已编码：`literature/reader.py`、`literature/textfold_adapter.py`、`third_party/textfold/**`、API/DTO、前端接线（roles/route/settings/chat client） |
| 自动测试 | 全量 `pytest` **747 passed**（其中 `tests/test_reader.py` 61 项：契约、并发、CAS、来源标注、角色组合、真实书目、真实 provider 契约） |
| 类型/构建 | 后端与我接线的前端文件通过；2026-10-25 收尾时 `frontend/src/reader/ReaderWorkspace.tsx` 仍在 designer 手上（含调试日志），`tsc` 报 1 处 `TS2339`（line 904 `e.target.tagName`），由其修掉后需复跑 `tsc` + `vite build` |
| 隔离端到端 | 冻结 sidecar `scripts/reader_frozen_check.py` **18/18 PASS**（真实书目副本、本地折叠、overlay/CAS 409、markdown、chat、事实文件哈希不变） |
| 全 app 横向溢出（我负责的 shell） | 新脚本 `scripts/app_shell_overflow_check.py`：首页/文件资源/结构校对/对照翻译/阅读 × 320/390/768/1024/1440 **页面级溢出全部为 0**，五个导航标签在 390 下真实点击均切换路由；截图 `tmp/shell-overflow-shots/`。修法：`body{min-width:860px}` → 320，≤899 顶栏两行（导航独立横向滚动，不裁剪可用内容），≤560 品牌只留图标；桌面 ≥1024 视觉不变（顶栏仍 60px 单行） |
| 浏览器联调（tester 脚本） | `tests/manual/reader_ui_browser_check.py` **20/20 PASS**（真实生产构建前端 + 真实本地 API + 隔离库；本地摘句分层、三选框、层浮条、缺译回退、overlay 持久化、未配模型时明确报错） |
| 真实外部服务 | `tests/manual/reader_real_provider_check.py`（0 起 evidence 版）**PASS**：2 节真实折叠（模型 44 unit，18 local/suppressed）+ 1 次选段问答；brief 逐字、evidence 合法、citations 合法、事实文件未变 |
| 真实外部服务（译文层） | 我在 /tmp 用隔离脚本跑 **zh-CN 2 节真实折叠 PASS**：`ai_units=58 / local_units=4`，brief 逐字、evidence 合法、`key` 以 takeaway 开头、0 违规（`tmp/reader-zh-layers-result.json`） |
| 真实外部服务（关思考） | 探测命中后折叠 reasoning 占比 **0.0%**（此前 71–80%），见 §8.6 |
| 真实外部服务（对话） | 隔离脚本单次提问 **PASS**：`state=done`、中文回答、引用 `blk-00000040/41`（原文证据，即使引用来自译文栏）、`reasoning_share 0.0`、input 1992 / output 351、费用未知记为 null（不写 0）、`reader/chat.json` 已落盘（`tmp/reader-real-chat.json`） |
| 打包（审查产物） | 见 §9 冻结基线与最终包标识；**未替换 launcher、未启动、未由用户真机点验** |
| 冻结验收（打包产物本身） | 对 **bundle 内** sidecar 复跑 `scripts/reader_frozen_check.py` → **18/18 PASS**（不是只看 pyi 列表；每次重建后重跑） |
| 未验收 | 用户真机点验；长书全书折叠（>50 节）真实模型耗时/费用；Tauri 安装与启动；tester 的 320 窄屏全交互与「A 在途→切 B」自动化；designer 修完 320px 后的最终包重建（等信号） |
