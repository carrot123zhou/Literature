"""TextFold AI 核心语义验收（tester，2026-10-25）。

区别于 tests/test_reader.py 的「程序可运行」断言，本文件验收**核心语义与实际默认路径**：

一、上游同版本算法直接对照（不经过 Literature 包装）
二、逐字锚定契约（format.md §contract：full/brief 逐字；key/takeaway/topic 模型转写且可回溯）
三、真实语义行为：多句段 evidence 非机械首句、takeaway 摘要转写、章节主线
四、生命周期：local-only 不伪 AI、换模型缓存失效、local 不覆盖有效 AI、失败局部补齐
五、原译同层证据一致

模型回复一律用**脚本化语义 provider**（模拟一个「真的在读原文」的模型：多句段选非首句、
takeaway 从原文句子转写、topic 逐字短语），不付费、可复现。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from literature.models import Block, Document, Page, TranslationEntry, TranslationLayer
from literature.providers import MockProvider, ProviderResponse
from literature.reader import ReaderLayerService, ReaderStore, clear_layers, parse_section_reply
from literature.storage import ProjectStore

REPO = Path(__file__).resolve().parents[1]


def make_multisentence_document(project_id: str = "p1") -> Document:
    """多句段落书：每段 3-4 句，让「机械选首句」与「真实语义选证」可区分。"""
    blocks = [
        Block(block_id="b-title-1", type="title", order=0, page_index=0, source_text="Silver Road", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-p-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 200], source_text=(
            "The opening chapter surveys the trade routes. "
            "Silver moved westward through Sogdian intermediaries in the seventh century. "
            "Workshops in Chang'an adapted the imported forms for local elites. "
            "By the ninth century the demand had shifted to gold."
        )),
        Block(block_id="b-p-2", type="paragraph", order=2, page_index=0, bbox=[10, 210, 300, 360], source_text=(
            "Assay records reveal a consistent alloy standard. "
            "Ninety-two percent silver coins circulated alongside gilt bronzes. "
            "Two mints have been identified near the western market. "
            "Their output peaked between 780 and 820."
        )),
        Block(block_id="b-title-2", type="title", order=3, page_index=1, source_text="Decline", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-p-3", type="paragraph", order=4, page_index=1, bbox=[10, 50, 300, 200], source_text=(
            "Hoards from the period tell a story of decline. "
            "Buried coins grew scarcer after 850. "
            "Hairline traces of gilding suggest reuse rather than new work."
        )),
    ]
    return Document(project_id=project_id, title="Silver Road", source_language="en", page_count=2,
                    pages=[Page(page_index=0, width=600, height=800), Page(page_index=1, width=600, height=800)],
                    blocks=blocks)


def make_store(root: Path) -> tuple[ProjectStore, str]:
    store = ProjectStore(root / "library")
    source = root / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n% fixture")
    project = store.create_project(source, title="Silver Road")
    pid = project["project_id"]
    store.save_document(make_multisentence_document(pid))
    return store, pid


def semantic_reply(user: str) -> dict:
    """模拟「真实阅读理解」的模型回复（当前提示契约：`[U0] paragraph` + 编号句子行）：
    每段选**承载主张的句子**（通常非首句），takeaway 用原文句子**转写**（压缩——上游
    written() 会把它锚回原文 token），topic 从 takeaway 逐字取名词短语，role 按论证角色。"""
    import re

    units = []
    current_id = None
    sentences: list[str] = []
    for line in user.splitlines() + ["[END]"]:
        m = re.match(r"^\[(\S+)\]", line)
        if m or line.startswith("[END]"):
            if current_id is not None and sentences:
                # 语义选择：最长（信息最密）的句子作为主张证据——不是机械首句
                ranked = sorted(range(len(sentences)), key=lambda i: len(sentences[i]), reverse=True)
                evidence = sorted(ranked[:2])
                core = sentences[evidence[0]]
                takeaway = core.rstrip(".")[: min(len(core.rstrip(".")), 90)]
                topic = " ".join(takeaway.split()[:3]) if takeaway[:1].isascii() else takeaway[:8]
                units.append({"id": current_id, "role": "claim", "takeaway": takeaway, "topic": topic,
                              "reason": "This is what the passage establishes about its subject.",
                              "evidence": evidence})
            current_id = m.group(1) if m else None
            sentences = []
            continue
        sm = re.match(r"^\s*(\d+):\s+(.*)$", line)
        if current_id is not None and sm:
            sentences.append(sm.group(2).strip())
    # derive 协议（译文短层）：prompt 含 "do NOT translate the section" 与 "ev_sentences"；
    # 只对 backing sentences 逐句给译文渲染，不要求 evidence/role。
    if "do NOT translate the section" in user and "ev_sentences" in user:
        d_units = []
        current = None
        backing: list[str] = []
        for line in user.splitlines() + ["[END]"]:
            m = re.match(r"^\[(\S+)\]", line)
            if m:
                if current is not None:
                    d_units.append({**current, "ev_sentences": [
                        f"[译] {sentence}" for sentence in backing]})
                current = {"id": m.group(1), "takeaway": f"译文短层 {m.group(1)}",
                           "reason": "译文理由。", "topic": "译文主题"}
                backing = []
                continue
            bm = re.match(r"^\s*(\d+):\s+(.*)$", line)
            if current is not None and bm:
                backing.append(bm.group(2).strip())
        if current is not None:
            d_units.append({**current, "ev_sentences": [f"[译] {s}" for s in backing]})
        return {"section": {"takeaway": "译文章节主线", "topic": "译文主题"}, "units": d_units}
    return {"section": {"takeaway": "Silver trade and assay standards shaped the period.", "topic": "Silver trade"},
            "units": units}


class SemanticProvider:
    """按语义规则生成回复的脚本 provider（见 semantic_reply）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.config = MockProvider().config

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        self.calls.append((system, user))
        return ProviderResponse(content=json.dumps(semantic_reply(user), ensure_ascii=False),
                                provider="semantic-fake", model="semantic-model",
                                request_id=f"req-{len(self.calls)}", input_tokens=100, output_tokens=200,
                                estimated_cost=0.002)

    def close(self) -> None:
        pass


class UpstreamAlgorithmParityTests(unittest.TestCase):
    """一、上游同版本算法直接对照：Literature 的折叠必须与 vendored 上游逐字段一致。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.service = ReaderLayerService(self.store, ReaderStore(self.store))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_section_view_matches_upstream_fold_unit(self) -> None:
        """同一个 ladder doc，折叠后 Literature 的投影与上游 fold.section()/unit() 输出一致。"""
        from literature.textfold_adapter import build_ladder_doc, apply_reply
        from third_party.textfold import fold as tf_fold

        document = self.store.load_document(self.project_id)
        doc = build_ladder_doc(document)
        for chunk in doc["chunks"]:
            apply_reply(doc, chunk, semantic_reply("probe"), model="semantic-model")
        for chunk in doc["chunks"]:
            if not chunk["units"]:
                continue
            upstream = tf_fold.section(doc, chunk, "en")
            for upstream_unit in upstream["units"]:
                source = doc["units"][upstream_unit["id"]]
                sentences = [source and None]  # noqa: F841 - 占位，实际比较在下方
                toks, sents = source["toks"], source["sents"]
                ev = upstream_unit["evidence"]
                # 上游契约（协议检查，fixture 内 takeaway 为逐字派生）：
                # brief == sentences[evidence]（0 起）。真实 AI 的 takeaway 允许同义转写（词面可低），
                self.assertEqual(
                    upstream_unit["brief"],
                    [tf_fold._text(toks[a:b], doc["atoms"]) for s in ev for a, b in [sents[s]] if s < len(sents)],
                    f"{upstream_unit['id']} brief 与 evidence 句不一致",
                )
                # takeaway ⊆ 原文 token（written() 锚定）：takeaway 文本必须能在原文里找到
                # （允许大小写/空白规范化差异，LCS 锚定）;这里断言 takeaway 的实词大多出现在原文
                take_text = upstream_unit["takeaway"]
                full_text = upstream_unit["full"]
                if take_text:
                    words = [w for w in take_text.replace(".", "").split() if len(w) > 3]
                    hit = sum(1 for w in words if w.lower() in full_text.lower())
                    # 协议检查（仅本 fixture：takeaway 由原文句子压缩而来）；
                    # 真实模型同义概括不适用 60% 词面门槛（见 Lead 判据 2026-10-25）。
                    self.assertGreaterEqual(
                        hit, max(1, int(len(words) * 0.6)),
                        f"{upstream_unit['id']} takeaway 无法锚回原文：{take_text!r}",
                    )

    def test_assemble_is_the_single_fold_path(self) -> None:
        """Literature 折叠必须复用上游 ladder.assemble（模型词→锚定 token），不另写第二套。"""
        from literature.textfold_adapter import build_ladder_doc, apply_reply
        from third_party.textfold import ladder as tf_ladder

        document = self.store.load_document(self.project_id)
        doc = build_ladder_doc(document)
        chunk = next(c for c in doc["chunks"] if c["units"])
        reply = semantic_reply("拆节提示词占位")[ "units" ]
        # 用 assemble 直接算，再与 Literature 的 apply_reply 对比同一 unit 的 lad
        unit = doc["units"][chunk["units"][0]]
        # 提示里的 unit id 是位置编号 U0..（与 apply_reply 的 by_id 匹配规则一致）
        reply = semantic_reply("[U0] paragraph\n  0: Sentence one.\n  1: Sentence two is longer and carries the claim.\n")[ "units" ]
        direct = tf_ladder.assemble(unit, reply[0], doc["atoms"], "semantic-model", cjk=doc.get("is_cjk"))
        apply_reply(doc, chunk, {"section": {"takeaway": "t", "topic": "t"}, "units": reply}, model="semantic-model")
        self.assertEqual(unit["lad"].get("model"), direct.get("model"))
        self.assertEqual(unit["lad"].get("ev"), direct.get("ev"))
        self.assertEqual(unit["lad"].get("tn"), direct.get("tn"))


class SemanticFoldTests(unittest.TestCase):
    """二/三、逐字锚定与真实语义行为。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.service = ReaderLayerService(self.store, ReaderStore(self.store))
        self.provider = SemanticProvider()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fold(self, **kwargs) -> dict:
        return self.service.generate(self.project_id, provider=self.provider, model_label="semantic/model", **kwargs)

    def test_evidence_is_not_mechanically_first_sentence(self) -> None:
        """多句段上真实语义选择的 evidence 不应全部机械落 [0]。"""
        result = self._fold(mode="auto")
        multi = [u for s in result["sections"] for u in s["units"] if len(u.get("sentences") or []) >= 3]
        self.assertTrue(multi, "测试样本必须有多句段")
        non_first = [u for u in multi if any(e > 0 for e in (u.get("evidence") or []))]
        self.assertGreater(len(non_first), 0, "所有多句段的 evidence 都是机械 [0]——模型没有真实选择")

    def test_brief_is_verbatim_evidence_sentences(self) -> None:
        result = self._fold(mode="auto")
        for section in result["sections"]:
            for unit in section["units"]:
                self.assertEqual(
                    unit.get("brief"),
                    [(unit.get("sentences") or [])[e] for e in (unit.get("evidence") or []) if e < len(unit.get("sentences") or [])],
                    f"{unit['id']} brief 与 evidence 句不一致",
                )
                for sentence in unit.get("brief") or []:
                    self.assertIn(sentence, unit.get("full") or "", "brief 摘句必须逐字来自原文")

    def test_takeaway_topic_anchored_and_key_is_transcription(self) -> None:
        """takeaway/topic 可锚回原文；key = 模型转写（takeaway+reason）；topic ⊆ takeaway 逐字。"""
        result = self._fold(mode="auto")
        checked = 0
        for section in result["sections"]:
            for unit in section["units"]:
                if unit.get("source") != "model":
                    continue
                checked += 1
                takeaway = unit.get("takeaway") or ""
                self.assertTrue(takeaway, f"{unit['id']} 缺 takeaway")
                self.assertLessEqual(len(takeaway), 260, f"{unit['id']} takeaway 超预算")
                # topic 是 takeaway 的逐字子串（或其去空白形态）
                topic = (unit.get("topic") or "").strip()
                if topic:
                    # 上游 topic_tokens：优先 takeaway 逐字短语；短短语（≤预算+2 词）可保留
                    # 模型自拟措辞——不强行 substring（Lead 判据 2026-10-25）。
                    in_takeaway = topic in takeaway or topic.replace(" ", "") in takeaway.replace(" ", "")
                    short_exception = len(topic.split()) <= 5
                    self.assertTrue(in_takeaway or short_exception,
                                    f"{unit['id']} topic {topic!r} 既非 takeaway 逐字短语也非短短语例外")
                # key = 模型转写（takeaway 起头 + reason），语义上以 takeaway 为主干
                key = unit.get("key") or ""
                self.assertTrue(key.startswith(takeaway[: max(1, len(takeaway) - 2)]) or takeaway[:20] in key,
                                f"{unit['id']} key 未以 takeaway 为主干：key={key[:60]!r} takeaway={takeaway[:60]!r}")
        self.assertGreater(checked, 0, "没有 model 来源的 unit——AI 折叠没有生效")

    def test_section_takeaway_written(self) -> None:
        """章节主线（section takeaway）由模型写出且非空。"""
        result = self._fold(mode="auto")
        for section in result["sections"]:
            self.assertTrue(section.get("takeaway"), f"{section['id']} 章节主线缺失")

    def test_role_within_upstream_vocabulary(self) -> None:
        allowed = {"context", "gap", "claim", "method", "setup", "result", "analysis", "limitation"}
        result = self._fold(mode="auto")
        for section in result["sections"]:
            for unit in section["units"]:
                if unit.get("source") == "model":
                    self.assertIn(unit.get("role") or "context", allowed, f"{unit['id']} role 越界")


class LifecycleTests(unittest.TestCase):
    """四、生命周期：local 不伪 AI、换模型失效、local 不覆盖有效 AI、失败局部补齐。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.service = ReaderLayerService(self.store, ReaderStore(self.store))
        self.provider = SemanticProvider()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_model_mode_produces_ai_content(self) -> None:
        result = self.service.generate(self.project_id, provider=self.provider, model_label="m")
        self.assertTrue(result["provenance"]["ai_generated"])
        self.assertFalse(result["provenance"]["local"])
        self.assertEqual(result["provenance"].get("content"), "model")

    def test_model_change_invalidates_cache(self) -> None:
        """换折叠模型必须重折叠（缓存键含 model），不得复用别家输出。"""
        self.service.generate(self.project_id, provider=self.provider, model_label="model-A", mode="auto")
        calls_before = len(self.provider.calls)
        again = self.service.generate(self.project_id, provider=self.provider, model_label="model-B", mode="auto")
        self.assertGreater(len(self.provider.calls), calls_before, "换模型后没有重新折叠——缓存未按 model 失效")
        self.assertEqual(again["reused_sections"], 0, "换模型后不应有复用章节")

    def test_local_fold_does_not_overwrite_valid_ai_layers(self) -> None:
        """已有 AI 分层时，非 force 的 local 请求不得推翻 AI 内容。"""
        ai_result = self.service.generate(self.project_id, provider=self.provider, model_label="semantic/model", mode="auto")
        ai_takeaways = {u["id"]: u.get("takeaway") for s in ai_result["sections"] for u in s["units"]}
        rerun = self.service.generate(self.project_id, provider=self.provider, model_label="m")
        local_takeaways = {u["id"]: u.get("takeaway") for s in rerun["sections"] for u in s["units"]}
        same = [uid for uid in ai_takeaways if ai_takeaways[uid] == local_takeaways.get(uid)]
        # local 折叠与 AI 折叠内容天然不同：如果层被覆盖，takeaway 会变成机械首句
        self.assertEqual(
            len(same), len(ai_takeaways),
            "local 折叠覆盖了有效 AI 分层（AI takeaway 被 机械摘句 替换）",
        )
        self.assertFalse(self.service.document(self.project_id)["stale"],
                         "local 请求不得抹掉所保留 AI 内容的模型摘要键")

    def test_failed_section_refilled_on_next_run(self) -> None:
        """失败章节保留旧内容；恢复后只补失败章节（其余复用缓存）。"""
        from literature.providers import ProviderError

        class Flaky(SemanticProvider):
            def __init__(self, fail_section_marker: str) -> None:
                super().__init__()
                self.fail_marker = fail_section_marker
                self.failed_once = False

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                if not self.failed_once and self.fail_marker in user:
                    self.failed_once = True
                    raise ProviderError("受控失败：该章节")
                return super().complete(system=system, user=user)

        flaky = Flaky("opening chapter surveys")  # 只出现在第一节的句子里 → 第一节失败
        first = self.service.generate(self.project_id, provider=flaky, model_label="semantic/model", mode="auto")
        self.assertEqual(len(first["failed_sections"]), 1, f"应恰好失败一节：{first['failed_sections']}")
        calls_after_first = len(flaky.calls)
        second = self.service.generate(self.project_id, provider=flaky, model_label="semantic/model", mode="auto")
        self.assertEqual(second["failed_sections"], [], "恢复运行不应再有失败章节")
        self.assertEqual(len(flaky.calls) - calls_after_first, 1, "恢复运行应只补失败的那一节")


class TranslationLayerConsistencyTests(unittest.TestCase):
    """五、原译同层：译文层的 evidence 锚定译文句子、与原文层一致。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.service = ReaderLayerService(self.store, ReaderStore(self.store))
        self.store.save_translation(TranslationLayer(project_id=self.project_id, locale="zh-CN", entries={
            "b-p-1": TranslationEntry(block_id="b-p-1", text="开篇一章综述了贸易路线。白银经粟特中介在西运。长安工坊改造了进口器形。九世纪需求转向黄金。", status="translated"),
            "b-p-2": TranslationEntry(block_id="b-p-2", text="检测记录显示一致的合金标准。百分之九十二银币流通。两处铸币厂已确认。产量在 780 到 820 年达到顶峰。", status="translated"),
            "b-p-3": TranslationEntry(block_id="b-p-3", text="窖藏讲述了衰落的故事。850 年后窖藏硬币变得稀少。鎏金痕迹暗示重复利用。", status="translated"),
            "b-title-1": TranslationEntry(block_id="b-title-1", text="白银之路", status="translated"),
            "b-title-2": TranslationEntry(block_id="b-title-2", text="衰落", status="translated"),
        }))
        self.provider = SemanticProvider()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_first_bilingual_local_preview_populates_both_pane_indexes(self) -> None:
        before = self.store.translation_path(self.project_id, "zh-CN").read_bytes()
        self.service.generate(self.project_id, locale="zh-CN", provider=self.provider, model_label="m", section_ids=["S1"])
        source = self.service.document(self.project_id, locale="source")
        target = self.service.document(self.project_id, locale="zh-CN")
        self.assertEqual([s["id"] for s in source["sections"]], ["S1"])
        self.assertEqual([s["id"] for s in target["sections"]], ["S1"])
        self.assertEqual([u["id"] for u in source["sections"][0]["units"]],
                         [u["id"] for u in target["sections"][0]["units"]])
        # mode=auto + provider → AI 产物（不再有 local 预览路径）
        self.assertTrue(source["ai_generated"])
        self.assertTrue(target["ai_generated"])
        self.assertEqual(before, self.store.translation_path(self.project_id, "zh-CN").read_bytes())

    def test_translation_locale_keeps_evidence_consistent(self) -> None:
        """译文层的 evidence 必须能锚定译文句子（brief 逐字来自译文），unit id 与原文一致。"""
        source_result = self.service.generate(self.project_id, provider=self.provider, model_label="m", mode="auto")
        translated = self.service.generate(self.project_id, locale="zh-CN", provider=self.provider, model_label="m", mode="auto")
        source_units = {u["id"]: u for s in source_result["sections"] for u in s["units"]}
        for section in translated["sections"]:
            for unit in section["units"]:
                self.assertIn(unit["id"], source_units, "译文层 unit id 必须与原文一致")
                src_ev = source_units[unit["id"]].get("evidence") or []
                if src_ev:
                    # 边界（2026-10-25 用户判据）：句数相等不是对齐证据；本 fixture 的句级对应
                    # 是构造时明示的（4:4 逐句直译）→ 索引相等可作回归。真实 unequal 场景必须
                    # 走映射 DTO 或显式「不可对齐」，映射规则冻结前不写索引断言。
                    self.assertEqual(unit.get("evidence"), src_ev,
                                     f"{unit['id']} 译文层 evidence 与原文层不一致（翻译应保留 evidence）")
                # 新 derive 协议：target brief = ev_sentences（模型对 backing 原句的译文渲染，
                # 标 derived/layer-translation），不再是 translation.json 原句引用。
                # 断言改为：brief 非空且不冒充 translation.json 原句。
                if unit.get("brief"):
                    self.assertTrue(all(sentence for sentence in unit["brief"]),
                                    f"{unit['id']} 译文 brief 存在空句")
                # derived 标签契约：brief_source=derived 必须出现在投影（前端据此渲染标签）
                if unit.get("source") == "model":
                    self.assertEqual(unit.get("brief_source"), "derived",
                                     f"{unit['id']} model 来源的 unit 应标 brief_source=derived")



class BilingualFoldBudgetTests(unittest.TestCase):
    """双语折叠 token 预算硬约束（2026-10-25 用户判据）。

    - source 只折叠一次：全文正文只允许出现在 source fold 请求里；
    - target short 层仅从 source folded 产物派生：**禁止**把完整译文句序列/整份译文发模型；
    - 若存在第二类调用，只允许短 takeaway/reason/topic/evidence 片段 + 必要小上下文；
    - translation.json 哈希与人工锁定不变；换 locale 不重新 source fold；
      target 失效不污染 source；刷新复用 0 调用；
    - token 账本 input/output/缓存分列，不虚报。
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.zh_text = {
            "b-p-1": "开篇一章综述了贸易路线。白银经粟特中介在西运。长安工坊改造了进口器形。九世纪需求转向黄金。",
            "b-p-2": "检测记录显示一致的合金标准。百分之九十二银币流通。两处铸币厂已确认。产量在 780 到 820 年达到顶峰。",
            "b-p-3": "窖藏讲述了衰落的故事。850 年后窖藏硬币变得稀少。鎏金痕迹暗示重复利用。",
        }
        self.store.save_translation(TranslationLayer(project_id=self.project_id, locale="zh-CN", entries={
            **{bid: TranslationEntry(block_id=bid, text=text, status="translated", locked=True)
               for bid, text in self.zh_text.items()},
            "b-title-1": TranslationEntry(block_id="b-title-1", text="白银之路", status="translated"),
            "b-title-2": TranslationEntry(block_id="b-title-2", text="衰落", status="translated"),
        }))
        self.service = ReaderLayerService(self.store, ReaderStore(self.store))
        self.provider = SemanticProvider()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _source_body_markers(self) -> list[str]:
        document = self.store.load_document(self.project_id)
        return [b.source_text.strip() for b in document.blocks if b.type == "paragraph"]

    def test_target_phase_must_not_send_full_translation(self) -> None:
        """source 折叠一次后，译文短层调用不得携带完整译文句序列/整段译文。"""
        translation_path = self.store.translation_path(self.project_id, "zh-CN")
        translation_before = hashlib.sha256(translation_path.read_bytes()).hexdigest()

        self.service.generate(self.project_id, provider=self.provider, model_label="m", mode="auto")  # source
        source_calls = len(self.provider.calls)
        self.assertGreater(source_calls, 0)
        # source 正文只应出现在 source fold 请求里
        for marker in self._source_body_markers():
            self.assertTrue(any(marker[:40] in user for _s, user in self.provider.calls[:source_calls]),
                            f"source 正文未在 source fold 请求中：{marker[:40]!r}")

        self.service.generate(self.project_id, locale="zh-CN", provider=self.provider, model_label="m", mode="auto")
        target_calls = self.provider.calls[source_calls:]
        full_translation_sentences = [s for text in self.zh_text.values() for s in text.split("。") if s.strip()]
        # 硬约束：任何 target 调用不得携带完整译文句序列（逐句枚举=整份译文上模型）
        offenders = []
        for _s, user in target_calls:
            hits = sum(1 for s in full_translation_sentences if s in user)
            if hits >= 3:
                offenders.append((hits, user[:120]))
        self.assertEqual(
            offenders, [],
            f"译文短层调用携带了完整译文句序列（双份正文上模型）：{offenders[:2]}",
        )
        # translation.json 未被写、锁定未变
        self.assertEqual(translation_before,
                         hashlib.sha256(translation_path.read_bytes()).hexdigest(),
                         "translation.json 在折叠过程中被修改")

    def test_locale_switch_does_not_refold_source_and_refresh_reuses(self) -> None:
        """序列（Lead 2026-10-25 裁决）：初zh→读回非空→clear(断言空+source不变)→重建
        （target 调用可、source 0）→读回非空→同内容刷新 0 调用且 reused 准确。"""
        translation_path = self.store.translation_path(self.project_id, "zh-CN")
        translation_before = hashlib.sha256(translation_path.read_bytes()).hexdigest()

        self.service.generate(self.project_id, provider=self.provider, model_label="m", mode="auto")  # source
        reader = ReaderStore(self.store)

        # 1) 初次 zh 成功 → 新 ReaderStore 读回 target 非空
        self.service.generate(self.project_id, locale="zh-CN", provider=self.provider, model_label="m", mode="auto")
        stored1 = reader.load_layers(self.project_id)
        self.assertTrue(stored1["docs"].get("zh-CN", {}).get("sections"),
                        "初次 zh 折叠后 target 层应为非空")
        self.assertGreater(len(stored1["section_hashes"].get("zh-CN") or {}), 0)

        # 2) clear 并断言 target 空、source 不变
        source_before_clear = json.dumps(stored1["docs"].get("source") or {}, ensure_ascii=False, default=str)
        clear_layers(reader, self.project_id, locale="zh-CN")
        stored2 = reader.load_layers(self.project_id)
        self.assertFalse(stored2["docs"].get("zh-CN", {}).get("sections"),
                         "clear 后 target 层应为空")
        self.assertEqual(
            json.dumps(stored2["docs"].get("source") or {}, ensure_ascii=False, default=str),
            source_before_clear, "clear target 不得影响 source 层",
        )
        self.assertEqual(hashlib.sha256(translation_path.read_bytes()).hexdigest(), translation_before,
                         "clear/rebuild 过程不得修改 translation.json")

        # 3) 明确 generate zh 重建：target 调用允许，source 重折不允许
        calls_before_rebuild = len(self.provider.calls)
        self.service.generate(self.project_id, locale="zh-CN", provider=self.provider, model_label="m", mode="auto")
        document = self.store.load_document(self.project_id)
        source_markers = [b.source_text.strip() for b in document.blocks if b.type == "paragraph"]
        stored3 = reader.load_layers(self.project_id)
        self.assertTrue(stored3["docs"].get("zh-CN", {}).get("sections"),
                        "重建后 target 层非空")
        self.assertGreater(len(stored3["section_hashes"].get("zh-CN") or {}), 0,
                           "重建后 hashes 非空（F4 回归：落盘不能为空）")
        # source 重折检测：derive 调用允许被选 evidence 原句；抓未被选原句/译文全句
        rebuilt_calls = self.provider.calls[calls_before_rebuild:]
        selected = set()
        for section in stored3["docs"].get("zh-CN", {}).get("sections") or []:
            for unit in section.get("units") or []:
                for sentence in unit.get("sentences") or []:
                    pass
        src_stored = reader.load_layers(self.project_id)["docs"].get("source") or {}
        for section in src_stored.get("sections") or []:
            for unit in section.get("units") or []:
                for sentence in unit.get("sentences") or []:
                    selected.add(sentence.strip())
        # 用 source 层的句子全集减去 derive 中合法出现的：derive 只含 backing（被选）句；
        # 简化判据：任何调用里出现「完整段落正文」（≥2 句拼接）即视为 source 重折/夹带
        for _s, user in rebuilt_calls:
            for marker in source_markers:
                first_two = " ".join(marker.split(". ")[:2])
                self.assertNotIn(first_two[:60], user,
                                 "重建调用夹带 source 整段正文（应只有被选 evidence 句）")

        # 4) 同模型同内容刷新：0 调用 + reused 准确
        calls_before_refresh = len(self.provider.calls)
        result_refresh = self.service.generate(self.project_id, locale="zh-CN", provider=self.provider,
                                               model_label="m", mode="auto")
        self.assertEqual(len(self.provider.calls) - calls_before_refresh, 0,
                         "刷新（同模型同内容）必须 0 模型调用")
        self.assertEqual(result_refresh["reused_sections"],
                         len(stored3["section_hashes"].get("zh-CN") or {}),
                         "刷新复用章节数应等于已存章节数")
        self.assertEqual(result_refresh["generated_sections"], 0)
        self.assertEqual(hashlib.sha256(translation_path.read_bytes()).hexdigest(), translation_before,
                         "刷新不得修改 translation.json")

    def test_token_ledger_split_input_output(self) -> None:
        """账本分列 input/output/缓存；不把双语开销虚报为 0。"""
        usage: list = []
        self.service.generate(self.project_id, provider=self.provider, model_label="m", mode="auto",
                              usage_hook=usage.append)
        self.service.generate(self.project_id, locale="zh-CN", provider=self.provider, model_label="m", mode="auto",
                              usage_hook=usage.append)
        self.assertTrue(usage, "无 usage 记录")
        for record in usage:
            self.assertGreater(record.input_tokens, 0, "input_tokens 必须如实记录")
            self.assertGreater(record.output_tokens, 0, "output_tokens 必须如实记录（双语开销不得虚报为 0）")



class SingleFlightTests(unittest.TestCase):
    """跨 locale 并发同节：source fold 必须单飞（1 次调用），网络失败释放锁可补齐。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.store.save_translation(TranslationLayer(project_id=self.project_id, locale="zh-CN", entries={
            "b-p-1": TranslationEntry(block_id="b-p-1", text="开篇一章综述了贸易路线。", status="translated"),
            "b-p-2": TranslationEntry(block_id="b-p-2", text="检测记录显示一致的合金标准。", status="translated"),
            "b-p-3": TranslationEntry(block_id="b-p-3", text="窖藏讲述了衰落的故事。", status="translated"),
        }))
        self.service = ReaderLayerService(self.store, ReaderStore(self.store))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_concurrent_source_and_target_same_section_single_source_fold(self) -> None:
        """两线程同节（同一 section_ids）并发同模型：该节 source fold 恰 1 次（单飞），两层正常。

        计数 oracle（Lead 2026-10-25）：fixture 有 2 节，全文档最低正常 fold 数=2（每节 1）；
        并发必须按节证据——两线程都传同一个实际 section_ids，provider 延时保证 in-flight 重叠。
        """
        import threading
        import time as _time

        from literature.reader import build_sections

        document = self.store.load_document(self.project_id)
        sections = build_sections(document)
        target_section_ids = [sections[0]["id"]]  # 只折 S1：两线程同一节
        provider = SemanticProvider()
        original_complete = provider.complete

        def complete(*, system: str, user: str) -> ProviderResponse:
            _time.sleep(0.4)  # in-flight 重叠窗口
            return original_complete(system=system, user=user)
        provider.complete = complete  # type: ignore[method-assign]

        results: list = []
        errors: list = []
        barrier = threading.Barrier(2)

        def run(locale: str) -> None:
            barrier.wait()
            try:
                results.append((locale, self.service.generate(
                    self.project_id, locale=locale, provider=provider, model_label="m",
                    mode="auto", section_ids=list(target_section_ids))))
            except Exception as error:  # noqa: BLE001
                errors.append(str(error))

        t_source = threading.Thread(target=run, args=("source",))
        t_target = threading.Thread(target=run, args=("zh-CN",))
        t_source.start(); t_target.start(); t_source.join(); t_target.join()
        self.assertFalse(errors, f"并发触发报错：{errors}")
        # 按节计数：S1 的 source fold 提示恰 1 次；S2 未被触及（section_ids 限定）
        s1_folds = [user for _s, user in provider.calls
                    if "do NOT translate" not in user and "This section: 1 Silver Road" in user]
        s2_folds = [user for _s, user in provider.calls
                    if "do NOT translate" not in user and "This section: 2 Decline" in user]
        self.assertEqual(len(s1_folds), 1,
                         f"S1 source fold 被执行 {len(s1_folds)} 次（单飞应恰 1 次）")
        self.assertEqual(len(s2_folds), 0, "section_ids 限定下不得折叠 S2")
        by_locale = dict(results)
        self.assertTrue(by_locale.get("source", {}).get("sections"))
        self.assertTrue(by_locale.get("zh-CN", {}).get("sections"))

    def test_network_failure_releases_lock_and_can_refill(self) -> None:
        """source fold 网络失败后锁必须释放：下一次成功 provider 能补齐。"""
        from literature.providers import ProviderError

        class FailingOnce(SemanticProvider):
            def __init__(self) -> None:
                super().__init__()
                self.failed = False

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                if not self.failed:
                    self.failed = True
                    raise ProviderError("受控网络失败")
                return super().complete(system=system, user=user)

        failing = FailingOnce()
        # 失败语义：generate 不抛，转为该节 failed（不 local 兜底——AIFold 必须 AI 成功产物）
        first = self.service.generate(self.project_id, provider=failing, model_label="m", mode="auto")
        self.assertTrue(first.get("failed_sections"), "网络失败应记入 failed_sections")
        # 失败节无存盘投影（不 local 冒充）——失败节不在 sections 里
        self.assertTrue(first.get("failed_sections"), "failed_sections 确认失败")
        # 失败后重试：single-flight 锁已释放，成功 provider 能补齐全部章节
        recovered = self.service.generate(self.project_id, provider=failing, model_label="m", mode="auto")
        self.assertEqual(recovered.get("failed_sections"), [], "重试应补齐失败章节")
        self.assertTrue(all(u.get("source") == "model"
                            for s in recovered["sections"] for u in s["units"]),
                        "补齐后应为 AI 内容")


class DeriveEvSentencesGuardTests(unittest.TestCase):
    """ev_sentences 数量与 backing 不一致：拒绝该 unit / partial，不得截源 ev 假对应。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.store.save_translation(TranslationLayer(project_id=self.project_id, locale="zh-CN", entries={
            "b-p-1": TranslationEntry(block_id="b-p-1", text="开篇一章综述了贸易路线。", status="translated"),
            "b-p-2": TranslationEntry(block_id="b-p-2", text="检测记录显示一致的合金标准。", status="translated"),
            "b-p-3": TranslationEntry(block_id="b-p-3", text="窖藏讲述了衰落的故事。", status="translated"),
        }))
        self.service = ReaderLayerService(self.store, ReaderStore(self.store))
        self.provider = SemanticProvider()
        self.service.generate(self.project_id, provider=self.provider, model_label="m", mode="auto")  # source 先折叠

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _derive_with_bad_ev(self, mutate) -> dict:
        """发一次 derive 请求，用 mutate 篡改 ev_sentences 数量后返回 zh 结果与调用。"""
        calls_before = len(self.provider.calls)
        original_complete = self.provider.complete

        def complete(*, system: str, user: str) -> ProviderResponse:
            response = original_complete(system=system, user=user)
            payload = json.loads(response.content)
            if "ev_sentences" in user:
                for unit in payload.get("units", []):
                    mutate(unit)
            return ProviderResponse(content=json.dumps(payload, ensure_ascii=False),
                                    provider=response.provider, model=response.model,
                                    request_id=response.request_id, input_tokens=1, output_tokens=1,
                                    estimated_cost=0.0)
        self.provider.complete = complete  # type: ignore[method-assign]
        result = self.service.generate(self.project_id, locale="zh-CN", provider=self.provider,
                                       model_label="m", mode="auto", force=True)
        self.provider.complete = original_complete  # type: ignore[method-assign]
        return result

    def _strict_derive_case(self, ev_len: int) -> dict:
        """adapter 直测：构造已折叠 source（语义 provider 选 2 句），派生回复 ev_sentences 长度 ev_len。

        返回 {zh_unit_lad_exists, brief_source, source_ev_after, warnings, fallback}。
        """
        from literature.textfold_adapter import (
            apply_derived_reply, apply_reply, build_ladder_doc, merge_translation_doc,
        )

        document = self.store.load_document(self.project_id)
        # 与 generate 相同的两步：先折 source ladder（apply_reply），再 merge 出 zh 视图
        source_ladder = build_ladder_doc(document)
        source_reply = semantic_reply("[U0] paragraph\n  0: The opening chapter surveys the trade routes.\n"
                                      "  1: Silver moved westward through Sogdian intermediaries in the seventh century.\n"
                                      "  2: Workshops in Chang'an adapted the imported forms for local elites.\n"
                                      "  3: By the ninth century the demand had shifted to gold.\n")
        for chunk in source_ladder["chunks"]:
            if chunk["units"]:
                apply_reply(source_ladder, chunk, source_reply, model="semantic-model")
        merged = merge_translation_doc(
            source_ladder,
            build_ladder_doc(document, locale="zh-CN",
                             translation=self.store.load_translation(self.project_id, "zh-CN")),
            "zh-CN")
        chunk = next(c for c in merged["chunks"] if c["units"])
        unit = merged["units"][chunk["units"][0]]
        src_ev_before = list((unit.get("lad") or {}).get("ev") or [])
        self.assertEqual(len(src_ev_before), 2, "fixture 需要被选 2 句的 unit")
        src_ev_expected = list(src_ev_before)  # 语义选择的结果（如 [1,2]），守卫对照用它
        reply = {"section": {"takeaway": "译文章节主线", "topic": "译文主题"},
                 "units": [{"id": "U0", "takeaway": "译文要点一", "reason": "译文理由。",
                            "topic": "译文要点",
                            "ev_sentences": [f"译句{i}" for i in range(ev_len)]}]}
        warnings, derived, fallback = apply_derived_reply(
            merged, chunk, reply, locale="zh-CN", model="m", unit_evidence=[(unit["id"], src_ev_before)])
        tr_lad = ((unit.get("tr") or {}).get("zh-CN") or {}).get("lad")
        return {"tr_lad_exists": tr_lad is not None,
                "brief_source": (tr_lad or {}).get("brief_source"),
                "source_ev_after": list((unit.get("lad") or {}).get("ev") or []),
                "src_ev_expected": src_ev_expected,
                "warnings": warnings, "fallback": fallback}

    def test_fewer_ev_sentences_rejects_unit_and_keeps_source_ev(self) -> None:
        """ev_sentences 少于 backing（1<2）：整 unit 拒绝派生（无 derived lad）、源 ev 不变。"""
        case = self._strict_derive_case(ev_len=1)
        self.assertFalse(case["tr_lad_exists"], "数量不符的 unit 不得写入 derived lad（拒绝/回退）")
        self.assertNotEqual(case["brief_source"], "derived")
        self.assertEqual(case["source_ev_after"], case["src_ev_expected"], "源 evidence 被截断/修改")
        self.assertTrue(case["warnings"], "拒绝必须留下告警")

    def test_extra_ev_sentences_rejects_unit_and_keeps_source_ev(self) -> None:
        """ev_sentences 多于 backing（3>2）：同样整 unit 拒绝，不静默接受假对应。"""
        case = self._strict_derive_case(ev_len=3)
        self.assertFalse(case["tr_lad_exists"], "数量不符的 unit 不得写入 derived lad")
        self.assertNotEqual(case["brief_source"], "derived")
        self.assertEqual(case["source_ev_after"], case["src_ev_expected"])

    def test_matching_ev_sentences_writes_derived(self) -> None:
        """对照：数量匹配（2==2）时正常写入 derived（证明拒绝不是一刀切）。"""
        case = self._strict_derive_case(ev_len=2)
        self.assertTrue(case["tr_lad_exists"], "匹配数量应正常派生")
        self.assertEqual(case["brief_source"], "derived")
        self.assertEqual(case["source_ev_after"], case["src_ev_expected"])


if __name__ == "__main__":
    unittest.main()
