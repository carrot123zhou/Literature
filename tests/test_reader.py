from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from literature.models import Block, Document, Page, TranslationEntry, TranslationLayer
from literature.providers import MockProvider, ProviderResponse
from tests.conftest import local_fold_section
from literature.reader import (
    ReaderLayerService,
    ReaderStore,
    answer_question,
    build_ask_context,
    build_sections,
    clear_layers,
    parse_section_reply,
    sanitize_overlay,
    section_digest,
    split_sentences,
)
from literature.storage import ProjectStore, StorageError


def make_document(project_id: str = "p1") -> Document:
    blocks = [
        Block(block_id="b-title-1", type="title", order=0, page_index=0, source_text="Introduction", level=1, bbox=[10, 10, 300, 40]),
        Block(
            block_id="b-p-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 120],
            source_text="Silver vessels travelled along the Silk Road. Workshops in Chang'an copied Sogdian shapes for decades. Demand fell after the ninth century.",
        ),
        Block(
            block_id="b-p-2", type="paragraph", order=2, page_index=0, bbox=[10, 130, 300, 200],
            source_text="Assay data show a consistent alloy of 92 percent silver.",
        ),
        Block(block_id="b-table", type="table", order=3, page_index=0, source_text="", bbox=[10, 210, 300, 300]),
        Block(block_id="b-title-2", type="title", order=4, page_index=1, source_text="Conclusion", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-list", type="list", order=5, page_index=1, bbox=[10, 50, 300, 120], items=["Hoards cluster in port cities.", "Finds thin out inland."]),
    ]
    return Document(
        project_id=project_id, title="Silk Road Silver", source_language="en",
        page_count=2,
        pages=[Page(page_index=0, width=600, height=800), Page(page_index=1, width=600, height=800)],
        blocks=blocks,
    )


def make_store(root: Path) -> tuple[ProjectStore, str]:
    store = ProjectStore(root / "library")
    source = root / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n% fixture")
    project = store.create_project(source, title="Silk Road Silver")
    store.save_document(make_document(project["project_id"]))
    return store, project["project_id"]


class SplitSentenceTests(unittest.TestCase):
    def test_cjk_and_latin_boundaries(self) -> None:
        # 语义来自上游 tokens.split_sentences：分号不是句末，成对省略号也不切句
        # （中文省略号后面通常还是同一句）；这条契约与 TextFold 完全一致，不按直觉改。
        self.assertEqual(split_sentences("这是第一句。这是第二句！还有第三句？"), ["这是第一句。", "这是第二句！", "还有第三句？"])
        self.assertEqual(split_sentences("One. Two? Three!"), ["One.", "Two?", "Three!"])
        self.assertEqual(split_sentences("等等……他真的来了。下一句。"), ["等等……他真的来了。", "下一句。"])
        self.assertEqual(split_sentences("第一句。第二句；第三句。"), ["第一句。", "第二句；第三句。"])

    def test_abbreviations_and_decimals_do_not_split(self) -> None:
        self.assertEqual(
            split_sentences("Fig. 3 shows the result. Values rise to 3.5 units in 2020."),
            ["Fig. 3 shows the result.", "Values rise to 3.5 units in 2020."],
        )
        self.assertEqual(split_sentences("Smith et al. reported this. It held."), ["Smith et al. reported this.", "It held."])

    def test_sentences_are_verbatim(self) -> None:
        text = "第一句。第二句；第三句。"
        joined = "".join(split_sentences(text))
        self.assertEqual(joined, text)


class SectionTests(unittest.TestCase):
    def test_sections_follow_headings_and_use_block_ids(self) -> None:
        document = make_document()
        sections = build_sections(document)
        self.assertEqual([section["id"] for section in sections], ["S1", "S2"])
        self.assertEqual(sections[0]["title"], "Introduction")
        # 表格不是 unit：只有 paragraph / list 参与折叠。
        self.assertEqual([unit["id"] for unit in sections[0]["units"]], ["b-p-1", "b-p-2"])
        self.assertEqual([unit["id"] for unit in sections[1]["units"]], ["b-list"])
        self.assertEqual(sections[1]["units"][0]["kind"], "list item")
        self.assertEqual(sections[1]["units"][0]["sentences"],
                         ["Hoards cluster in port cities.", "Finds thin out inland."])

    def test_preamble_before_first_heading_is_section_zero(self) -> None:
        document = make_document()
        document.blocks.insert(0, Block(block_id="b-pre", type="paragraph", order=-1, page_index=0,
                                        source_text="A short preamble paragraph."))
        for index, block in enumerate(document.blocks):
            block.order = index
        sections = build_sections(document)
        self.assertEqual(sections[0]["id"], "S0")
        self.assertEqual([unit["id"] for unit in sections[0]["units"]], ["b-pre"])

    def test_translation_locale_marks_missing_entries_as_fallback(self) -> None:
        document = make_document()
        layer = TranslationLayer(project_id="p1", locale="zh-CN", entries={
            "b-p-1": TranslationEntry(block_id="b-p-1", text="银器沿丝绸之路流动。长安工坊模仿粟特器形数十年。", status="translated"),
        })
        sections = build_sections(document, locale="zh-CN", translation=layer)
        units = sections[0]["units"]
        self.assertEqual(units[0]["full"], "银器沿丝绸之路流动。长安工坊模仿粟特器形数十年。")
        self.assertFalse(units[0]["fallback"])
        # 没有译文的段落回退原文并明确标记，界面据此显示「未译 · 显示原文」。
        self.assertTrue(units[1]["fallback"])
        self.assertEqual(units[1]["full"], document.blocks[2].source_text)


class LocalFoldTests(unittest.TestCase):
    def test_local_fold_is_verbatim_and_anchored(self) -> None:
        sections = build_sections(make_document())
        folded = local_fold_section(sections[0])
        unit = folded["units"][0]
        self.assertTrue(all(sentence in unit["sentences"] for sentence in unit["brief"]))
        self.assertTrue(all(0 <= number < len(unit["sentences"]) for number in unit["evidence"]))
        self.assertTrue(unit["takeaway"])
        self.assertTrue(folded["takeaway"])

    def test_local_fold_keeps_long_first_sentence_alone(self) -> None:
        document = make_document()
        document.blocks[1].source_text = (
            "This opening sentence is deliberately long enough that adding the next sentence would overrun a brief line of text. "
            "A second sentence follows it."
        )
        sections = build_sections(document)
        unit = local_fold_section(sections[0])["units"][0]
        # 英文本地折叠取首句（上游 head_words），句子下标 0 起。
        self.assertEqual(unit["evidence"], [0])


class ParseReplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sections = build_sections(make_document())

    def test_brief_is_rebuilt_from_evidence(self) -> None:
        payload = {
            "section": {"takeaway": "Silver shapes were copied locally.", "topic": "Silver shapes"},
            "units": [
                {"id": "U0", "evidence": [0], "takeaway": "Copying happened for decades.",
                 "reason": "Workshops in Chang'an copied Sogdian shapes.", "topic": "decades", "role": "claim"},
                {"id": "U1", "evidence": [0], "takeaway": "Alloy is consistent.",
                 "reason": "Assay data show 92 percent silver.", "topic": "Alloy", "role": "result"},
            ],
        }
        folded, warnings = parse_section_reply(payload, self.sections[0])
        self.assertEqual(warnings, [])
        unit = folded["units"][0]
        # evidence 是句子下标（0 起，与上游 fold.unit 一致）；brief 由它在本地拼回。
        self.assertEqual(unit["evidence"], [0])
        self.assertEqual(unit["brief"], self.sections[0]["units"][0]["sentences"][:1])
        self.assertEqual(unit["brief_text"], " ".join(unit["brief"]))
        self.assertEqual(unit["role"], "claim")
        # 上游语义：key = takeaway + 一句理由；takeaway 是 key 的前缀；
        # topic 在 takeaway 里出现时按词序列锚定（`topic_tokens` 优先 locate）。
        self.assertTrue(unit["key"].startswith(unit["takeaway"]))
        self.assertIn(unit["topic"], unit["takeaway"])
        self.assertTrue(unit["takeaway"].startswith("Copying happened for decades"))
        # 上游 clean() 会去掉行尾标点。
        self.assertEqual(folded["takeaway"], "Silver shapes were copied locally")
        self.assertEqual(folded["topic"], "Silver shapes")

    def test_invalid_evidence_falls_back_with_warning(self) -> None:
        payload = {"units": [{"id": "U0", "evidence": [9], "takeaway": "Made up."}]}
        folded, warnings = parse_section_reply(payload, self.sections[0])
        # 越界 evidence 被过滤后为空 → 该段不可追溯：按未生成处理（pending 占位，空短层），
        # 不回退本地摘句、不编句子（AIFold 必须 AI 成功产物）。
        unit = folded["units"][0]
        self.assertTrue(any("evidence 全部无效" in w for w in warnings))
        self.assertEqual(unit["source"], "pending")
        self.assertEqual(unit["brief"], [])
        self.assertEqual(unit["takeaway"], "")   # 不采用模型伪造行

    def test_unknown_ids_and_overlong_fields_are_bounded(self) -> None:
        payload = {"units": [{"id": "U9", "evidence": [0], "takeaway": "x"}]}
        folded, warnings = parse_section_reply(payload, self.sections[0])
        self.assertTrue(warnings)
        self.assertEqual([unit["id"] for unit in folded["units"]], ["b-p-1", "b-p-2"])

        payload = {"units": [{"id": "U0", "evidence": [0, 1, 2, 3], "takeaway": "长" * 400,
                              "reason": "k" * 900, "topic": "t" * 200, "role": "not-a-role"}]}
        folded, _ = parse_section_reply(payload, self.sections[0])
        unit = folded["units"][0]
        # 上游 assemble 只取前 3 条合法 evidence；超长字段按预算夹取后仍满足 takeaway ⊆ key。
        self.assertLessEqual(len(unit["evidence"]), 3)
        self.assertTrue(unit["key"].startswith(unit["takeaway"]))
        self.assertEqual(unit["role"], "context")   # 非法 role 落回上游默认值

    def test_missing_unit_and_empty_takeaway_stay_pending(self) -> None:
        """模型漏段 / 空 takeaway：该 unit 落 pending 未生成占位（空短层），不本地摘句。"""
        # 场景 1：模型漏段（只回 U0，U1 缺失）
        payload = {
            "section": {"takeaway": "Partial.", "topic": "Partial"},
            "units": [
                {"id": "U0", "evidence": [0], "takeaway": "Real takeaway.",
                 "reason": "Grounded.", "topic": "Real", "role": "claim"},
            ],
        }
        folded, warnings = parse_section_reply(payload, self.sections[0])
        sources = {u["id"]: u["source"] for u in folded["units"]}
        self.assertEqual(sources.get("b-p-1"), "model")
        self.assertEqual(sources.get("b-p-2"), "pending")
        self.assertTrue(any("未返回" in w for w in warnings))
        # 场景 2：空 takeaway（预检拦截，不进 assemble，不落 local）
        payload2 = {
            "section": {"takeaway": "Partial.", "topic": "Partial"},
            "units": [
                {"id": "U0", "evidence": [0], "takeaway": "Real takeaway.",
                 "reason": "Grounded.", "topic": "Real", "role": "claim"},
                {"id": "U1", "evidence": [0], "takeaway": "", "reason": "", "topic": "", "role": "claim"},
            ],
        }
        folded2, warnings2 = parse_section_reply(payload2, self.sections[0])
        sources2 = {u["id"]: u["source"] for u in folded2["units"]}
        self.assertEqual(sources2.get("b-p-2"), "pending")
        self.assertTrue(any("takeaway" in w for w in warnings2))
        pending_unit = next(u for u in folded2["units"] if u["source"] == "pending")
        self.assertEqual(pending_unit["brief"], [])
        self.assertEqual(pending_unit["takeaway"], "")


class OverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.reader = ReaderStore(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_overlay_roundtrip_and_revision(self) -> None:
        first = self.reader.load_overlay(self.project_id)
        self.assertEqual(first["revision"], 0)
        saved = self.reader.save_overlay(self.project_id, {
            "annotations": [{
                "id": "a1", "block_id": "b-p-1", "pane": "source", "start": 0, "end": 6,
                "quote": {"exact": "Silver", "prefix": "", "suffix": " vessels"},
                "color": "b", "note": "关键术语",
            }],
            "prefs": {"fontSize": 18, "level": 3, "panes": {"origin": True, "source": True, "trans": True}},
        }, base_revision=0)
        self.assertEqual(saved["revision"], 1)
        loaded = self.reader.load_overlay(self.project_id)
        self.assertEqual(len(loaded["annotations"]), 1)
        self.assertEqual(loaded["annotations"][0]["quote"]["exact"], "Silver")
        self.assertEqual(loaded["prefs"]["fontSize"], 18)
        self.assertEqual(loaded["prefs"]["level"], 3)

    def test_overlay_rejects_stale_base_revision(self) -> None:
        self.reader.save_overlay(self.project_id, {"annotations": []}, base_revision=0)
        with self.assertRaises(StorageError):
            self.reader.save_overlay(self.project_id, {"annotations": []}, base_revision=0)

    def test_overlay_sanitizes_unknown_and_out_of_range_values(self) -> None:
        values = sanitize_overlay({
            "annotations": [
                {"block_id": "b-p-1", "start": 5, "end": 2},
                {"block_id": "", "start": 0, "end": 3},
                {"block_id": "b-p-2", "start": 0, "end": 3, "pane": "evil", "color": "rainbow",
                 "note": "n" * 9000, "junk": "dropped"},
            ],
            "prefs": {"fontSize": 99, "level": 0, "weights": [-1, 0, 0], "appearance": "neon"},
            "unexpected": {"huge": "x"},
        })
        self.assertEqual(len(values["annotations"]), 1)
        annotation = values["annotations"][0]
        self.assertEqual(annotation["pane"], "source")
        self.assertEqual(annotation["color"], "y")
        self.assertLessEqual(len(annotation["note"]), 4000)
        self.assertNotIn("junk", annotation)
        self.assertNotIn("unexpected", values)
        self.assertEqual(values["prefs"]["fontSize"], 20)
        self.assertEqual(values["prefs"]["level"], 1)
        self.assertEqual(values["prefs"]["appearance"], "light")
        self.assertEqual(values["prefs"]["weights"], [1.0, 1.0, 1.0])

    def test_overlay_write_never_touches_document_or_translation(self) -> None:
        project = self.store.project_path(self.project_id)
        document_path = project / "document" / "document.json"
        layer = TranslationLayer(project_id=self.project_id, locale="zh-CN", entries={
            "b-p-1": TranslationEntry(block_id="b-p-1", text="银器沿丝绸之路流动。"),
        })
        self.store.save_translation(layer)
        translation_path = self.store.translation_path(self.project_id, "zh-CN")
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (document_path, translation_path)}
        self.reader.save_overlay(self.project_id, {"annotations": [
            {"id": "a1", "block_id": "b-p-1", "pane": "source", "start": 0, "end": 5, "note": "备注"},
        ]})
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (document_path, translation_path)}
        self.assertEqual(before, after)
        self.assertTrue((project / "reader" / "overlay.json").is_file())


class CountingProvider:
    """按调用序号返回预置 JSON，并记录每次请求，用于验证「只重折叠变化的章节」。"""

    def __init__(self, reply) -> None:
        self.calls: list[tuple[str, str]] = []
        self._reply = reply
        self.config = MockProvider().config

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        self.calls.append((system, user))
        # 派生路径（derive_context 提示含 source takeaway）需要 ev_sentences。
        if "source takeaway:" in user:
            payload = self._reply(user)
            for unit in payload.get("units") or []:
                unit["ev_sentences"] = [f"分层译句 {unit.get('id', 'U')}。"]
        else:
            payload = self._reply(user)
        return ProviderResponse(
            content=json.dumps(payload, ensure_ascii=False), provider="fake", model="fake-model",
            request_id=f"req-{len(self.calls)}", input_tokens=100, output_tokens=50, estimated_cost=0.001,
        )


def reply_for(user: str) -> dict:
    """按上游提示词格式（`[U0] paragraph` + 编号句子）回复一个章节。"""
    import re as _re

    units = [f"U{m.group(1)}" for m in (_re.match(r"\[U(\d+)\]", line) for line in user.splitlines()) if m]
    return {
        "section": {"takeaway": "这一节说明白银器物的传播与合金特征。", "topic": "白银器物"},
        "units": [
            {"id": unit_id, "evidence": [0], "takeaway": f"要点 {unit_id}",
             "reason": "依据第一句。", "topic": "要点", "role": "claim"}
            for unit_id in units
        ],
    }


class LayerGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.service = ReaderLayerService(self.store)
        self.reader = ReaderStore(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_model_layers_are_persisted_with_provenance(self) -> None:
        provider = CountingProvider(reply_for)
        usage: list[ProviderResponse] = []
        result = self.service.generate(self.project_id, provider=provider, model_label="fake-model",
                                       usage_hook=usage.append)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(result["generated_sections"], 2)
        self.assertTrue(result["provenance"]["ai_generated"])
        self.assertFalse(result["provenance"]["local"])
        self.assertEqual(len(usage), 2)
        # 落盘位置是独立交互层，不是 document.json。
        self.assertTrue((self.store.project_path(self.project_id) / "reader" / "layers.json").is_file())
        stored = self.service.document(self.project_id)
        self.assertFalse(stored["stale"])
        self.assertTrue(stored["generated"])
        section = stored["sections"][0]
        self.assertEqual([unit["id"] for unit in section["units"]], ["b-p-1", "b-p-2"])

    def test_second_run_reuses_cached_sections(self) -> None:
        provider = CountingProvider(reply_for)
        self.service.generate(self.project_id, provider=provider, model_label="fake-model")
        self.assertEqual(len(provider.calls), 2)
        again = self.service.generate(self.project_id, provider=provider, model_label="fake-model")
        self.assertEqual(len(provider.calls), 2, "内容未变的章节不应重新调用模型")
        self.assertEqual(again["reused_sections"], 2)
        self.assertEqual(again["generated_sections"], 0)

    def test_only_changed_section_is_regenerated(self) -> None:
        provider = CountingProvider(reply_for)
        self.service.generate(self.project_id, provider=provider, model_label="fake-model")
        provider.calls.clear()
        document = self.store.load_document(self.project_id)
        document.blocks[1].source_text = "Silver vessels travelled along the Silk Road in both directions."
        self.store.save_document(document)
        result = self.service.generate(self.project_id, provider=provider, model_label="fake-model")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(result["generated_sections"], 1)
        self.assertEqual(result["reused_sections"], 1)
        self.assertTrue(self.service.document(self.project_id)["stale"] is False)

    def test_stale_flag_detects_structure_change(self) -> None:
        self.service.generate(self.project_id, provider=CountingProvider(reply_for), model_label="fake")
        document = self.store.load_document(self.project_id)
        document.blocks[1].source_text = "Completely rewritten paragraph."
        self.store.save_document(document)
        self.assertTrue(self.service.document(self.project_id)["stale"])

    def test_auto_mode_uses_model_and_produces_ai_content(self) -> None:
        provider = CountingProvider(reply_for)
        result = self.service.generate(self.project_id, provider=provider, model_label="fake")
        self.assertTrue(len(provider.calls) > 0)
        self.assertTrue(result["provenance"]["ai_generated"])
        self.assertFalse(result["provenance"]["local"])
        self.assertTrue(result["generated"])

    def test_failed_section_no_local_fallback(self) -> None:
        class Boom(CountingProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                from literature.providers import ProviderError
                raise ProviderError("boom")

        result = self.service.generate(self.project_id, provider=Boom(reply_for), model_label="fake")
        self.assertEqual(len(result["failed_sections"]), 2)
        # AIFold 失败不 local 兜底：失败节的 unit 无 brief/takeaway（Full 层仍渲染）
        for section in result["sections"]:
            for unit in section.get("units") or []:
                self.assertFalse(unit.get("brief"), f"{unit['id']} 不应有 local brief")

    def test_clear_layers_removes_only_derived_content(self) -> None:
        self.service.generate(self.project_id, provider=CountingProvider(reply_for), model_label="fake")
        clear_layers(self.reader, self.project_id)
        self.assertFalse(self.service.document(self.project_id)["generated"])
        self.assertTrue(self.store.load_document(self.project_id).blocks)

    def test_translation_locale_layers_use_translated_text(self) -> None:
        layer = TranslationLayer(project_id=self.project_id, locale="zh-CN", entries={
            "b-p-1": TranslationEntry(block_id="b-p-1", text="银器沿丝绸之路流动。长安工坊模仿粟特器形数十年。", status="translated"),
            "b-p-2": TranslationEntry(block_id="b-p-2", text="检测数据显示合金含银百分之九十二。", status="translated"),
            "b-title-1": TranslationEntry(block_id="b-title-1", text="导论", status="translated"),
            "b-list": TranslationEntry(block_id="b-list", items=["窖藏集中在港口城市。", "内陆发现变少。"], status="translated"),
            "b-title-2": TranslationEntry(block_id="b-title-2", text="结论", status="translated"),
        })
        self.store.save_translation(layer)
        provider = CountingProvider(reply_for)
        result = self.service.generate(self.project_id, locale="zh-CN", provider=provider, model_label="fake")
        self.assertEqual(result["sections"][0]["title"], "导论")
        self.assertEqual(result["sections"][0]["units"][0]["full"], "银器沿丝绸之路流动。长安工坊模仿粟特器形数十年。")
        # 2026-10-25 两相流契约：locale≠source 先折叠原文（phase1 提示是原文句子），
        # 再用 trans_prompt 写译文短层（phase2 提示含用户译文与原文 source takeaway）。
        # 旧断言「calls[0] 含译文」对应的是已废弃的单相折叠，按新契约改判两相。
        # 2026-10-25 derive 协议契约（收紧）：prompt 只含被选 evidence 的 backing 原句，
        # 不含用户译文全文/整段译文序列；要求 ev_sentences（backing 句译文渲染，
        # 标 layer-translation，translation.json 永不被引用或修改）。
        trans_calls = [user for _system, user in provider.calls if "source takeaway" in user]
        self.assertTrue(trans_calls, "两相流缺失：没有找到译文短层的 trans_prompt 请求")
        self.assertIn("backing sentences", trans_calls[0])
        self.assertIn("ev_sentences", trans_calls[0])
        self.assertIn("do NOT translate the section", trans_calls[0])
        self.assertNotIn("银器沿丝绸之路流动。长安工坊模仿粟特器形数十年。", trans_calls[0],
                         "derive prompt 不得夹带用户译文全文")


class AskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.document = self.store.load_document(self.project_id)
        self.sections = [local_fold_section(item) for item in build_sections(self.document)]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_quote_context_is_bounded_to_the_neighbourhood(self) -> None:
        system, user, citations = build_ask_context(
            self.document, self.sections, question="这块银器说明了什么？",
            quote={"block_id": "b-p-1", "pane": "source", "text": "Silver vessels travelled"},
        )
        self.assertIn("the selection is here", user)
        ids = [item["block_id"] for item in citations]
        self.assertIn("b-p-1", ids)
        # 章节标题不是 unit，不会出现在邻域上下文里；远端章节也不会被拉进来。
        self.assertNotIn("b-list", ids)
        # 上游 SYSTEM_ASK + 注入边界；语言跟随问句写在同一处 system 里。
        self.assertIn("You help a reader understand a research paper", system)
        self.assertIn("never followed", system)

    def test_document_context_includes_toc_and_ranks_blocks(self) -> None:
        _system, user, citations = build_ask_context(
            self.document, self.sections, question="alloy data 合金",
        )
        self.assertIn("one line per section", user)
        # 全篇地图带章节标题；正文按上游 retrieve() 词面打分取前 k（标题块不再硬塞进引用）。
        self.assertIn("Introduction", user)
        ids = [item["block_id"] for item in citations]
        self.assertIn("b-p-2", ids)

    def test_answer_filters_citations_to_known_blocks(self) -> None:
        class Provider:
            config = MockProvider().config

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                return ProviderResponse(
                    content="银器沿丝路流通 [#b-p-1]。合金稳定 [#b-p-2]。编造的引用 [#zzz]。",
                    provider="fake", model="fake", request_id="r1",
                    input_tokens=10, output_tokens=10, estimated_cost=0.0,
                )

        result = answer_question(
            self.document, self.sections, Provider(), question="银器怎么流通的？",
            quote={"block_id": "b-p-1", "pane": "source", "text": ""},
        )
        ids = [item["block_id"] for item in result["citations"]]
        self.assertIn("b-p-1", ids)
        self.assertNotIn("zzz", ids)

    def test_empty_question_is_rejected(self) -> None:
        with self.assertRaises(StorageError):
            answer_question(self.document, self.sections, MockProvider(), question="   ")

    def test_unknown_quote_block_is_rejected(self) -> None:
        with self.assertRaises(StorageError):
            build_ask_context(self.document, self.sections, question="?", quote={"block_id": "missing"})


class SectionDigestTests(unittest.TestCase):
    def test_digest_tracks_locale_and_content(self) -> None:
        sections = build_sections(make_document())
        first = section_digest(sections[0], "source")
        self.assertEqual(first, section_digest(sections[0], "source"))
        self.assertNotEqual(first, section_digest(sections[0], "zh-CN"))
        sections[0]["units"][0]["full"] += " changed"
        self.assertNotEqual(first, section_digest(sections[0], "source"))


class ReaderApiTests(unittest.TestCase):
    """HTTP 契约：阅读层独立存储、后台折叠作业、问答需模型。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # 模型服务库（model-services.json）在运行目录里；必须按测试隔离，
        # 否则前面用例写入的角色绑定会让「未配置模型」的断言随执行顺序漂移。
        self._previous_runtime = os.environ.get("LITERATURE_RUNTIME")
        self._previous_library = os.environ.get("LITERATURE_LIBRARY")
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        self.store, self.project_id = make_store(self.root)
        from fastapi.testclient import TestClient
        from literature.api import create_app
        self.client = TestClient(create_app(self.store))
        self.headers = {"x-literature-token": self.client.app.state.session_token}

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        for key, value in (("LITERATURE_RUNTIME", self._previous_runtime), ("LITERATURE_LIBRARY", self._previous_library)):
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def url(self, suffix: str) -> str:
        return f"/projects/{self.project_id}/reader{suffix}"

    def test_health_advertises_reader_capability(self) -> None:
        value = self.client.get("/health").json()
        self.assertIn("reader_v1", value["capabilities"])

    def test_state_and_overlay_roundtrip(self) -> None:
        self.assertEqual(self.client.get(self.url("/state")).status_code, 401)
        state = self.client.get(self.url("/state"), headers=self.headers).json()
        self.assertEqual(state["overlay"]["revision"], 0)
        self.assertEqual(state["layers"]["locales"], {})
        self.assertEqual(state["bbox_space"], "mineru-1000")

        saved = self.client.put(self.url("/overlay"), headers=self.headers, json={
            "overlay": {"annotations": [{"id": "a1", "block_id": "b-p-1", "start": 0, "end": 6, "note": "hi"}],
                        "prefs": {"level": 3}},
            "base_revision": 0,
        }).json()
        self.assertEqual(saved["revision"], 1)
        conflict = self.client.put(self.url("/overlay"), headers=self.headers, json={
            "overlay": {"annotations": []}, "base_revision": 0,
        })
        # 乐观并发冲突用 409（可重试），与「数据非法」的 400 区分。
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("revision", conflict.json()["detail"])

    def test_local_layer_job_completes_and_is_readable(self) -> None:
        # mode=local 已移除——改用 fold-run + mock provider 验证 API 层完整链路
        from tests.test_reader_fold_run import ScriptedProvider
        provider = ScriptedProvider()
        manager = self.client.app.state.job_manager
        manager.provider_from = lambda *a, **k: provider
        svc = manager.models.create({"name": "svc-test", "preset": "custom",
                                     "base_url": "http://127.0.0.1:1/v1", "models": [{"model": "fake-model"}]})
        manager.models.set_roles({"reader_fold": {"service_id": svc["service_id"], "model": "fake-model"}})
        started = self.client.post(self.url('/fold-runs'), headers=self.headers, json={'locale': 'source'})
        self.assertEqual(started.status_code, 200, started.text)
        run_id = started.json()['run_id']
        status = ''
        for _ in range(100):
            run = self.client.get(self.url('/fold-run'), headers=self.headers).json()
            status = run.get('status', '')
            if status in ('ready', 'failed'): break
            import time; time.sleep(0.05)
        self.assertEqual(status, 'ready', run)
        layers = self.client.get(self.url('/layers'), headers=self.headers).json()
        self.assertTrue(layers['generated'])
        # 删除后重新空
        cleared = self.client.delete(self.url("/layers"), headers=self.headers, params={"locale": "source"})
        self.assertEqual(cleared.status_code, 200)
        empty = self.client.get(self.url("/layers"), headers=self.headers).json()
        self.assertFalse(empty["generated"])

    def test_ask_requires_model_and_reports_missing_binding(self) -> None:
        response = self.client.post(self.url("/ask"), headers=self.headers, json={"question": "这本书讲了什么？"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("模型服务", response.json()["detail"])

    def test_ask_rejects_empty_question(self) -> None:
        response = self.client.post(self.url("/ask"), headers=self.headers, json={"question": "  "})
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# 阅读器验收补充（tester，2026-10-25）
#
# 覆盖矩阵里后端可独立验证的部分：独立层只读不变量、并发保存、失败/超时语义、
# 请求期模型冻结、译文引用的原文证据、真实书目（library/ 只读副本）的逐字性。
# 前端交互（三栏/五层/统一对话）尚未交付，浏览器验收不在此文件中代偿。
# ---------------------------------------------------------------------------

READER_LIBRARY = Path(__file__).resolve().parents[1] / "library"


def _real_projects() -> list[Path]:
    """本机文献库里已生成规范文档的真实项目（只读引用，测试中一律先复制再写）。"""
    if not READER_LIBRARY.is_dir():
        return []
    return sorted(
        path for path in READER_LIBRARY.iterdir()
        if (path / "document" / "document.json").is_file()
    )


def copy_real_project(root: Path, source: Path) -> tuple[ProjectStore, str, Path, Path]:
    """把真实项目的 document.json / zh-CN 译文复制进隔离项目，原件只读。"""
    import shutil

    store = ProjectStore(root / "library")
    origin = root / "sample.pdf"
    origin.write_bytes(b"%PDF-1.4\n% fixture")
    project = store.create_project(origin, title=source.name[:80])
    project_id = project["project_id"]
    document_path = store.project_path(project_id) / "document" / "document.json"
    document_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "document" / "document.json", document_path)
    translation_source = source / "translations" / "zh-CN" / "translation.json"
    if translation_source.is_file():
        target = store.translation_path(project_id, "zh-CN")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(translation_source, target)
    return store, project_id, document_path, translation_source


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_sentences_verbatim(case: unittest.TestCase, full: str, sentences: list[str]) -> None:
    """摘句必须逐字来自原文：去掉空白后拼回去应与原文完全一致。"""
    import re

    compact = lambda value: re.sub(r"\s+", "", value or "")
    case.assertTrue(sentences, "单元必须至少切出一句")
    for sentence in sentences:
        case.assertIn(sentence, full)
    case.assertEqual(compact("".join(sentences)), compact(full))


class ReaderInvariantTests(unittest.TestCase):
    """阅读层的任何写入都只能落在 reader/ 下；事实来源文件哈希必须不变。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.project = self.store.project_path(self.project_id)
        self.store.save_translation(TranslationLayer(project_id=self.project_id, locale="zh-CN", entries={
            "b-p-1": TranslationEntry(block_id="b-p-1", text="银器沿丝绸之路流动。", status="translated"),
            "b-title-1": TranslationEntry(block_id="b-title-1", text="导论", status="translated"),
        }))
        self.document_path = self.project / "document" / "document.json"
        self.translation_path = self.store.translation_path(self.project_id, "zh-CN")
        raw = self.project / "ocr" / "raw" / "mineru" / "full.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("# raw evidence\n", encoding="utf-8")
        self.raw_path = raw
        self.paths = [self.document_path, self.translation_path, self.raw_path]
        self.before = {path: sha256_of(path) for path in self.paths}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_overlay_layers_and_ask_never_write_back_facts(self) -> None:
        reader = ReaderStore(self.store)
        reader.save_overlay(self.project_id, {
            "annotations": [{
                "id": "a1", "block_id": "b-p-1", "pane": "source", "start": 0, "end": 6,
                "quote": {"exact": "Silver", "prefix": "", "suffix": " vessels"}, "note": "术语",
            }],
            "prefs": {"level": 3},
        }, base_revision=0)
        service = ReaderLayerService(self.store)
        service.generate(self.project_id, provider=CountingProvider(reply_for), model_label="fake")
        service.generate(self.project_id, provider=CountingProvider(reply_for), model_label="fake/model")
        document = self.store.load_document(self.project_id)
        answer_question(document, [local_fold_section(item) for item in build_sections(document)],
                        CountingProvider(reply_for), question="银器说明了什么？")

        for path in self.paths:
            self.assertEqual(self.before[path], sha256_of(path), f"{path.name} 被阅读层写坏")
        self.assertTrue((self.project / "reader" / "overlay.json").is_file())
        self.assertTrue((self.project / "reader" / "layers.json").is_file())


class OverlayConcurrencyTests(unittest.TestCase):
    """批注写入的并发与隔离（同一段两条记录、移除保笔记）。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id = make_store(self.root)
        self.reader = ReaderStore(self.store)
        self.path = f"/projects/{self.project_id}/reader/overlay"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_two_annotations_on_same_block_are_isolated(self) -> None:
        first = self.reader.save_overlay(self.project_id, {"annotations": [
            {"id": "a1", "block_id": "b-p-1", "start": 0, "end": 6, "color": "y", "note": "第一条"},
            {"id": "a2", "block_id": "b-p-1", "start": 19, "end": 30, "color": "b", "note": "第二条"},
        ]}, base_revision=0)
        self.assertEqual(len(first["annotations"]), 2)

        revised = [dict(item) for item in first["annotations"]]
        revised[0]["color"] = "p"
        revised[0]["note"] = "第一条（改色）"
        saved = self.reader.save_overlay(self.project_id, {"annotations": revised}, base_revision=first["revision"])
        by_id = {item["id"]: item for item in saved["annotations"]}
        self.assertEqual(by_id["a1"]["color"], "p")
        self.assertEqual(by_id["a2"]["color"], "b")
        self.assertEqual(by_id["a2"]["start"], 19)
        self.assertEqual(by_id["a2"]["note"], "第二条")

        remaining = self.reader.save_overlay(
            self.project_id, {"annotations": [by_id["a2"]]}, base_revision=saved["revision"],
        )
        self.assertEqual([item["id"] for item in remaining["annotations"]], ["a2"])

    def test_removed_highlight_keeps_note_as_detached_record(self) -> None:
        saved = self.reader.save_overlay(self.project_id, {"annotations": [{
            "id": "a1", "block_id": "b-p-1", "start": 0, "end": 6, "color": "y",
            "note": "只有笔记", "detached": True,
        }]}, base_revision=0)
        annotation = saved["annotations"][0]
        self.assertTrue(annotation["detached"])
        self.assertEqual(annotation["note"], "只有笔记")
        self.assertEqual((annotation["start"], annotation["end"]), (0, 6))
        reloaded = self.reader.load_overlay(self.project_id)
        self.assertEqual(reloaded["annotations"][0]["detached"], True)

    def test_overlay_size_limit_is_rejected_not_truncated(self) -> None:
        from literature.reader import MAX_OVERLAY_BYTES

        huge = [{
            "id": f"a{index}", "block_id": "b-p-1", "start": 0, "end": 5,
            "note": "注" * 4000,
        } for index in range(600)]
        with self.assertRaises(StorageError):
            self.reader.save_overlay(self.project_id, {"annotations": huge}, base_revision=0)
        # 失败后不能留下半份覆盖：原层仍是空层且 revision 未推进。
        self.assertEqual(self.reader.load_overlay(self.project_id)["revision"], 0)
        self.assertGreater(MAX_OVERLAY_BYTES, 0)

    def test_concurrent_overlay_saves_have_exactly_one_winner(self) -> None:
        """回归守护：并发同一 base_revision 的保存只允许一个成功。

        历史（2026-10-25 上午）：API 每请求新建 ReaderStore，实例级 RLock 跨请求无效，
        8 个并发 PUT 实测 5 个返回 200、最终 revision 仍为 1（丢更新）。
        现实现已改为 _path_lock(path)（路径级锁）+ 校验后写入，本测试保持常态运行。
        """
        import threading

        from fastapi.testclient import TestClient

        from literature.api import create_app

        client = TestClient(create_app(self.store))
        headers = {"x-literature-token": client.app.state.session_token}
        barrier = threading.Barrier(8)
        codes: list[int] = []
        lock = threading.Lock()

        def save(index: int) -> None:
            barrier.wait()
            response = client.put(self.path, headers=headers, json={
                "overlay": {"annotations": [{
                    "id": f"a{index}", "block_id": "b-p-1", "start": 0, "end": 5, "note": "n",
                }]},
                "base_revision": 0,
            })
            with lock:
                codes.append(response.status_code)

        threads = [threading.Thread(target=save, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(codes).count(200), 1, f"并发保存结果：{sorted(codes)}")
        self.assertEqual(self.reader.load_overlay(self.project_id)["revision"], 1)


class ReaderModelContractTests(unittest.TestCase):
    """隔离 HTTP 端到端：折叠作业冻结模型、usage 入账、问答失败不留伪成功。"""

    def setUp(self) -> None:
        import types

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._previous = {key: os.environ.get(key) for key in ("LITERATURE_RUNTIME", "LITERATURE_LIBRARY")}
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        os.environ["LITERATURE_SESSION_TOKEN"] = ""
        self.store, self.project_id = make_store(self.root)
        self.store.save_translation(TranslationLayer(project_id=self.project_id, locale="zh-CN", entries={
            "b-p-1": TranslationEntry(block_id="b-p-1", text="银器沿丝绸之路流动。", status="translated"),
            "b-p-2": TranslationEntry(block_id="b-p-2", text="检测数据显示合金含银百分之九十二。", status="translated"),
            "b-title-1": TranslationEntry(block_id="b-title-1", text="导论", status="translated"),
            "b-title-2": TranslationEntry(block_id="b-title-2", text="结论", status="translated"),
        }))

        from fastapi.testclient import TestClient

        from literature.api import create_app

        self.client = TestClient(create_app(self.store), raise_server_exceptions=False)
        self.manager = self.client.app.state.job_manager
        self.headers = {"x-literature-token": self.client.app.state.session_token}
        self.manager.models = types.SimpleNamespace(
            roles=lambda: {"translation": {"service_id": "svc-a", "model": "model-a"}},
            role_binding=lambda role: {"service_id": "svc-a", "model": "model-a"},
            get=lambda service_id: {
                "service_id": service_id, "name": "A", "preset": "custom", "base_url": "https://example.invalid",
                "allow_plaintext_http": False, "models": [{"model": "model-a"}],
            },
        )
        self._provider_class: type = CountingProvider
        self._reply = reply_for
        self.manager.provider_from = lambda service_id, provider_input=None, model="": self._provider_class(self._reply)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def url(self, suffix: str) -> str:
        return f"/projects/{self.project_id}/reader{suffix}"

    def _wait_job(self, timeout: float = 15.0) -> dict:
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            job = self.client.get(self.url("/state"), headers=self.headers).json()["layer_job"]
            if job and job["status"] in ("completed", "failed"):
                return job
            _time.sleep(0.05)
        self.fail("折叠作业未在超时内结束")

    def test_layers_job_freezes_model_and_records_usage(self) -> None:
        started = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto", "service_id": "svc-a", "model": "model-a",
        })
        self.assertEqual(started.status_code, 200)
        job = self._wait_job()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["config"]["model"], "model-a")
        self.assertEqual(job["result"]["failed_sections"], [])
        layers = self.client.get(self.url("/layers"), headers=self.headers).json()
        self.assertTrue(layers["generated"])
        self.assertEqual(layers["provenance"]["model"], "model-a")
        self.assertTrue(layers["provenance"]["ai_generated"])
        scopes = [record["scope"] for record in self.store.usage_records(self.project_id)]
        self.assertIn("reader_layers", scopes)
        self.assertTrue(all(record["model"] == "fake-model" for record in self.store.usage_records(self.project_id)))

    def test_translated_quote_asks_with_source_evidence(self) -> None:
        answer = self.client.post(self.url("/ask"), headers=self.headers, json={
            "question": "银器怎么流通的？",
            "locale": "zh-CN",
            "quote": {"block_id": "b-p-1", "pane": "trans", "text": "银器沿丝绸之路流动。"},
        })
        self.assertEqual(answer.status_code, 200, answer.text)
        payload = answer.json()
        self.assertTrue(payload["answer"])
        self.assertIn("b-p-1", [item["block_id"] for item in payload["citations"]])
        # 证据始终取原文：引文文本里必须有原文英文，而不是译文。
        quote_citation = next(item for item in payload["citations"] if item["block_id"] == "b-p-1")
        self.assertIn("Silver vessels", quote_citation["text"])
        self.assertNotIn("银器沿丝绸之路流动。", quote_citation["text"])
        job = self.store.list_jobs(self.project_id)[0]
        self.assertEqual(job["kind"], "reader_ask")
        self.assertEqual(job["status"], "completed")
        # 配置快照记录实际绑定与来源（reader_role / inherited_translation / explicit）。
        self.assertEqual(
            {key: job["config"][key] for key in ("service_id", "model")},
            {"service_id": "svc-a", "model": "model-a"},
        )
        self.assertIn("binding_source", job["config"])
        self.assertIn("reader_ask", [record["scope"] for record in self.store.usage_records(self.project_id)])

    def test_ask_provider_failure_is_reported_and_job_marked_failed(self) -> None:
        from literature.providers import ProviderError

        class Timeout(CountingProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                raise ProviderError("读超时：60s")

        self._provider_class = Timeout
        response = self.client.post(self.url("/ask"), headers=self.headers, json={"question": "这本书讲了什么？"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("读超时", response.json()["detail"])
        jobs = [job for job in self.store.list_jobs(self.project_id) if job["kind"] == "reader_ask"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertIn("读超时", jobs[0]["error"])

    def test_provider_error_in_fold_is_reported_not_silently_successful(self) -> None:
        from literature.providers import ProviderError

        class Boom(CountingProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                raise ProviderError("鉴权失败")

        self._provider_class = Boom
        started = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto", "service_id": "svc-a", "model": "model-a",
        })
        self.assertEqual(started.status_code, 200)
        job = self._wait_job()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(job["result"]["failed_sections"]), 2)
        # 2026-10-26：模型失败不再 local 兜底——失败节无存盘投影（诚实失败），
        # resume 重折这些节。layers 不应标注成 AI 生成。
        layers = self.client.get(self.url("/layers"), headers=self.headers).json()
        self.assertFalse(layers["provenance"]["ai_generated"])
        self.assertFalse(layers["generated"])
        self.assertFalse(layers["provenance"]["local"])
        self.assertTrue(all(section["units"] for section in layers["sections"]))

    def test_empty_units_reply_is_not_advertised_as_ai_generated(self) -> None:
        """回归守护：模型返回可解析 JSON 但没有任何有效 unit 时，不得宣称 AI 生成。

        历史（2026-10-25）：provenance.ai_generated 按「发过请求且未抛异常」计数，
        全部段回退本地折叠后仍标 AI。现实现按 ai_units>0 判定，并给出
        content=local/model/mixed 三态；本测试保持常态运行。
        """
        def empty_units(_user: str) -> dict:
            return {"takeaway": "", "topic": "", "units": []}

        self._reply = empty_units
        started = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto", "service_id": "svc-a", "model": "model-a",
        })
        self.assertEqual(started.status_code, 200)
        job = self._wait_job()
        self.assertTrue(job["result"]["warnings"])
        layers = self.client.get(self.url("/layers"), headers=self.headers).json()
        self.assertFalse(layers["provenance"]["ai_generated"])

    def test_question_without_token_overlap_still_gets_bounded_context(self) -> None:
        document = self.store.load_document(self.project_id)
        sections = [local_fold_section(item) for item in build_sections(document)]
        _system, user, citations = build_ask_context(document, sections, question="zzz qqq")
        self.assertTrue(citations)
        self.assertIn("Passages from the paper", user)
        self.assertIn("Only the opening of the paper could be retrieved", user)
        self.assertLessEqual(len(user), 18000)

    def test_cross_language_quote_keeps_source_evidence(self) -> None:
        """免 embedding 边界：中文问题/英文原文、译文选段提问，证据必须是原文 block。"""
        document = self.store.load_document(self.project_id)
        sections = [local_fold_section(item) for item in build_sections(document)]
        _system, user, citations = build_ask_context(
            document, sections, question="这本书关于什么主题？",
            quote={"block_id": "b-p-2", "pane": "trans", "text": "检测数据显示合金含银百分之九十二。"},
        )
        ids = [item["block_id"] for item in citations]
        self.assertIn("b-p-2", ids)
        quote_citation = next(item for item in citations if item["block_id"] == "b-p-2")
        self.assertIn("92 percent silver", quote_citation["text"])

    def test_unknown_citation_marker_is_removed_from_answer_text(self) -> None:
        """悬空引用标记：citations 已过滤 [#zzz] 后，answer 文本里的同名标记也必须去掉，
        否则前端会渲染出指向不存在段落的引用标签。已在 answer_question 内清理。"""
        class Marker:
            config = MockProvider().config

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                return ProviderResponse(
                    content="结论 [#zzz]。", provider="fake", model="m", request_id="r",
                    input_tokens=1, output_tokens=1, estimated_cost=0.0,
                )

        document = self.store.load_document(self.project_id)
        sections = [local_fold_section(item) for item in build_sections(document)]
        result = answer_question(document, sections, Marker(), question="?")
        self.assertNotIn("[#zzz]", result["answer"])


@unittest.skipUnless(_real_projects(), "本机 library/ 没有已解析的真实书目")
class RealLibraryReaderTests(unittest.TestCase):
    """真实书目（只读副本）：逐字摘句、稳定 block 映射、译文回退、写边界、规模上限。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sources = _real_projects()[:2]

    def test_real_sections_and_local_fold_stay_verbatim(self) -> None:
        for source in self.sources:
            with self.subTest(book=source.name[:40]):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                store, project_id, document_path, translation_source = copy_real_project(Path(temporary.name), source)
                document = store.load_document(project_id)
                sections = build_sections(document)
                blocks = {block.block_id: block for block in document.blocks}
                self.assertTrue(sections, "真实书目必须能切出章节")
                for section in sections:
                    for unit in section["units"]:
                        self.assertIn(unit["id"], blocks)
                        self.assertFalse(unit["fallback"], "原文层不应标记为译文回退")
                        assert_sentences_verbatim(self, unit["full"], unit["sentences"])

                folded = [local_fold_section(section) for section in sections]
                for section in folded:
                    for unit in section["units"]:
                        if unit.get("suppressed"):
                            # 版式管道（页码、"* * *" 分隔符、目录行）：不折叠，全文层照原样显示，
                            # 也不允许编出 takeaway/topic。
                            self.assertEqual(unit["brief"], [])
                            self.assertEqual(unit["takeaway"], "")
                            self.assertEqual(unit["topic"], "")
                            continue
                        # brief 是原文句子子集（不是整段重排）：每句都必须逐字存在于原文。
                        self.assertTrue(unit["brief"])
                        for sentence in unit["brief"]:
                            self.assertIn(sentence, unit["full"])
                            self.assertIn(sentence, unit["sentences"])
                        self.assertEqual(
                            unit["brief"],
                            [unit["sentences"][number] for number in unit["evidence"]],
                        )
                        # 本地兜底的主题是 takeaway 的前缀（超长时截断加省略号），
                        # 不是模型那种「逐字名词短语」；只要求不引入正文以外的词。
                        if unit["topic"]:
                            self.assertTrue(unit["takeaway"].startswith(unit["topic"].rstrip("…")))
                        else:
                            # 已知边界（2026-10-25）：takeaway 以「(」开头时 _local_topic 切出空主题。
                            self.assertTrue(unit["takeaway"].lstrip().startswith(("(", "（")), unit["id"])
                        self.assertTrue(set(unit["evidence"]) <= set(range(len(unit["sentences"]))))

                # 译文层：unit id 与原文一致；缺译必须显式标记回退而不是当译文。
                translation = store.load_translation(project_id, "zh-CN") if translation_source.is_file() else None
                if translation is not None:
                    translated = build_sections(document, locale="zh-CN", translation=translation)
                    self.assertEqual(
                        [unit["id"] for section in translated for unit in section["units"]],
                        [unit["id"] for section in sections for unit in section["units"]],
                    )
                    for section in translated:
                        for unit in section["units"]:
                            block = blocks[unit["id"]]
                            entry = translation.entries.get(unit["id"])
                            # 列表/注文的译文放在 items 里，正文块放在 text 里。
                            available = entry is not None and (
                                bool(entry.items) if block.type == "list" else bool((entry.text or "").strip())
                            )
                            if not available:
                                self.assertTrue(unit["fallback"], f"{unit['id']} 缺译未标记回退")
                                self.assertTrue(unit["full"])
                            else:
                                self.assertFalse(unit["fallback"], f"{unit['id']} 有译文却标记回退")

                self.assertTrue(document_path.is_file())

    def test_real_book_write_boundary_and_context_bound(self) -> None:
        for source in self.sources:
            with self.subTest(book=source.name[:40]):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                store, project_id, document_path, translation_source = copy_real_project(root, source)
                watched = [document_path] + ([translation_source] if translation_source.is_file() else [])
                # 真实库原件（library/ 下）全程只读：单独记哈希，最后再校验一次。
                library_files = [source / "document" / "document.json"]
                if translation_source.is_file():
                    library_files.append(translation_source)
                library_before = {path: sha256_of(path) for path in library_files}
                before = {path: sha256_of(path) for path in watched}

                ReaderStore(store).save_overlay(project_id, {"annotations": [
                    {"id": "real-1", "block_id": store.load_document(project_id).blocks[0].block_id,
                     "start": 0, "end": 4, "note": "真实样本批注"},
                ]}, base_revision=0)
                ReaderLayerService(store).generate(project_id, provider=CountingProvider(reply_for), model_label="fake")

                document = store.load_document(project_id)
                sections = [local_fold_section(item) for item in build_sections(document)]
                _system, user, _citations = build_ask_context(document, sections, question="这本书的结构是什么？")
                self.assertLessEqual(len(user), 18000)

                for path in watched:
                    self.assertEqual(before[path], sha256_of(path), f"真实副本被写坏：{path.name}")
                # 真实库原件同样只读：测试全程不应改到 library/。
                for path, digest in library_before.items():
                    self.assertEqual(digest, sha256_of(path), f"真实库原件被写入：{path}")
                self.assertTrue((store.project_path(project_id) / "reader" / "layers.json").is_file())

    def test_real_book_section_build_is_fast_enough(self) -> None:
        import time as _time

        for source in self.sources:
            with self.subTest(book=source.name[:40]):
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                store, project_id, _document, _translation = copy_real_project(Path(temporary.name), source)
                document = store.load_document(project_id)
                started = _time.monotonic()
                sections = build_sections(document)
                elapsed = _time.monotonic() - started
                self.assertTrue(sections)
                self.assertLess(elapsed, 10.0, f"build_sections 用时 {elapsed:.1f}s")


class ReaderRoleGatingTests(unittest.TestCase):
    """角色门控回归（2026-10-25 Lead 指定）。

    - reader_fold / reader_chat 是独立角色：只配其一不得被「翻译角色」的门槛错挡；
    - 同名模型必须按 (service_id, model) 路由到对应服务商，不能只看模型名；
    - 请求级覆写（本次换模型）优先于角色绑定，并在消息/作业里冻结。
    """

    def setUp(self) -> None:
        import types

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._previous = {key: os.environ.get(key) for key in ("LITERATURE_RUNTIME", "LITERATURE_LIBRARY")}
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        self.store, self.project_id = make_store(self.root)
        from fastapi.testclient import TestClient

        from literature.api import create_app

        self.client = TestClient(create_app(self.store))
        self.manager = self.client.app.state.job_manager
        self.headers = {"x-literature-token": self.client.app.state.session_token}
        self.roles: dict[str, dict[str, str]] = {
            "structure": {}, "translation": {}, "reader_fold": {}, "reader_chat": {},
        }
        self.routes: list[tuple[str, str]] = []

        class Fake:
            config = MockProvider().config

            def complete(_self, *, system: str, user: str) -> ProviderResponse:
                return ProviderResponse(
                    content='{"takeaway":"主线","topic":"主题","units":[]}',
                    provider="fake", model="actual-model", request_id="r",
                    input_tokens=10, output_tokens=5, estimated_cost=0.0,
                )

            def close(_self) -> None:
                pass

        def provider_from(service_id: str, provider_input=None, model: str = "") -> Fake:
            self.routes.append((service_id, model))
            return Fake()

        self.manager.models = types.SimpleNamespace(
            roles=lambda: dict(self.roles),
            role_binding=lambda role: dict(self.roles.get(role) or {}),
            get=lambda service_id: {
                "service_id": service_id, "name": f"服务 {service_id}", "preset": "custom",
                "base_url": "https://example.invalid", "allow_plaintext_http": False,
                "models": [{"model": "shared-model", "name": "同名模型"}, {"model": "m2", "name": "M2"}],
            },
        )
        self.manager.provider_from = provider_from

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def url(self, suffix: str) -> str:
        return f"/projects/{self.project_id}/reader{suffix}"

    def _wait_job(self, timeout: float = 10.0) -> dict:
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            job = self.client.get(self.url("/state"), headers=self.headers).json()["layer_job"]
            if job and job["status"] in ("completed", "failed"):
                return job
            _time.sleep(0.05)
        self.fail("折叠作业未在超时内结束")

    def test_fold_role_alone_is_not_blocked_by_translation_gate(self) -> None:
        """只绑定 reader_fold（未配 translation）时，折叠必须可用。

        门槛已改为按 reader_fold 判断（旧配置回退 translation），不再误挡 fold-only 配置。
        """
        self.roles["reader_fold"] = {"service_id": "svc-fold", "model": "shared-model"}
        response = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto",
        })
        self.assertEqual(response.status_code, 200, response.text)
        job = self._wait_job()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.routes[-1], ("svc-fold", "shared-model"))

    def test_missing_fold_binding_error_names_the_fold_role(self) -> None:
        """完全没绑折叠模型时，错误信息应指向「折叠模型」，而不是让人去配翻译模型。"""
        response = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("折叠", response.json()["detail"])

    def test_same_name_models_route_by_service_id(self) -> None:
        """两个服务商都有同名模型时，必须按 (service_id, model) 各自路由。

        断言折叠真正走 reader_fold 的 svc-a，而不是回落到 translation 的 svc-t。
        """
        self.roles["translation"] = {"service_id": "svc-t", "model": "shared-model"}
        self.roles["reader_fold"] = {"service_id": "svc-a", "model": "shared-model"}
        self.roles["reader_chat"] = {"service_id": "svc-b", "model": "shared-model"}
        started = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto",
        })
        self.assertEqual(started.status_code, 200, started.text)
        self._wait_job()
        self.assertIn(("svc-a", "shared-model"), self.routes)
        self.assertNotIn(("svc-t", "shared-model"), self.routes, "折叠不得回落到 translation 服务商")
        sent = self.client.post(self.url("/chat/messages"), headers=self.headers, json={"question": "?"})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(self.routes.count(("svc-b", "shared-model")), 1, f"对话路由：{self.routes}")

    def test_request_override_wins_and_freezes_binding(self) -> None:
        """当次换模型：请求级 service_id/model 优先于角色绑定，并冻结在消息里。"""
        self.roles["reader_chat"] = {"service_id": "svc-default", "model": "m2"}
        sent = self.client.post(self.url("/chat/messages"), headers=self.headers, json={
            "question": "这次用别的模型", "service_id": "svc-override", "model": "m2",
        })
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["binding"]["service_id"], "svc-override")
        self.assertIn(("svc-override", "m2"), self.routes)
        self.assertNotIn(("svc-default", "m2"), self.routes)
        chat = self.client.get(self.url("/chat"), headers=self.headers).json()
        user_message = next(item for item in chat["messages"] if item["role"] == "user")
        # 冻结的是真实路由；`source` 记录这次是显式指定（不是角色绑定/继承）。
        self.assertEqual(
            {key: user_message["binding"][key] for key in ("service_id", "model")},
            {"service_id": "svc-override", "model": "m2"},
        )
        self.assertEqual(user_message["binding"]["source"], "explicit")

    def test_chat_with_unbound_chat_role_names_chat_not_translation(self) -> None:
        """只配 reader_fold、未配 reader_chat 时，对话错误应指向「阅读对话」角色。"""
        self.roles["reader_fold"] = {"service_id": "svc-fold", "model": "shared-model"}
        sent = self.client.post(self.url("/chat/messages"), headers=self.headers, json={"question": "?"})
        self.assertEqual(sent.status_code, 400)
        self.assertIn("阅读对话", sent.json()["detail"])


class ReaderRoleCompatTests(unittest.TestCase):
    """旧配置角色兼容（真实 ModelServiceStore 文件 + 真实 API，不用 stub roles）。

    冻结契约（2026-10-25）：显式 service+model → 该 reader 角色 → 旧配置继承 translation；
    显式解绑/服务失效不得静默改用别家。
    """

    def setUp(self) -> None:
        import types

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._previous = {key: os.environ.get(key) for key in ("LITERATURE_RUNTIME", "LITERATURE_LIBRARY")}
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / "session-token").write_text("", encoding="utf-8")
        self.store, self.project_id = make_store(self.root)
        # 真实旧 v2 配置：只有 structure/translation 角色，没有 reader_* 键（老用户升级形态）。
        self.old_config = {
            "version": 2,
            "services": [{
                "service_id": "svc-t", "name": "旧服务", "preset": "custom",
                "base_url": "https://old.example", "allow_plaintext_http": False,
                "models": [{"model": "model-t", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2}],
            }],
            "roles": {
                "structure": {"service_id": "svc-t", "model": "model-t"},
                "translation": {"service_id": "svc-t", "model": "model-t"},
            },
        }
        (self.runtime / "model-services.json").write_text(json.dumps(self.old_config), encoding="utf-8")

        from fastapi.testclient import TestClient

        from literature.api import create_app

        self.client = TestClient(create_app(self.store))
        self.manager = self.client.app.state.job_manager
        self.headers = {"x-literature-token": self.client.app.state.session_token}
        self.routes: list[tuple[str, str]] = []

        class Fake:
            config = MockProvider().config

            def complete(_self, *, system: str, user: str) -> ProviderResponse:
                return ProviderResponse(
                    content='{"takeaway":"主线","topic":"主题","units":[]}', provider="fake",
                    model="model-t", request_id="r", input_tokens=5, output_tokens=5, estimated_cost=0.0,
                )

            def close(_self) -> None:
                pass

        def provider_from(service_id: str, provider_input=None, model: str = "") -> Fake:
            # 服务存在性以真实 ModelServiceStore 文件为准（与 config_for 一致）；
            # 只 fake 掉 Keychain 取密钥与 HTTP 这一层。
            try:
                self.manager.models.get(service_id)
            except Exception as error:
                self.routes.append((service_id, model))
                from literature.providers import ProviderError

                raise ProviderError(str(error)) from error
            self.routes.append((service_id, model))
            return Fake()

        self.manager.provider_from = provider_from

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def url(self, suffix: str) -> str:
        return f"/projects/{self.project_id}/reader{suffix}"

    def _wait_job(self, timeout: float = 10.0) -> dict:
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            job = self.client.get(self.url("/state"), headers=self.headers).json()["layer_job"]
            if job and job["status"] in ("completed", "failed"):
                return job
            _time.sleep(0.05)
        self.fail("折叠作业未在超时内结束")

    def test_old_config_without_reader_keys_inherits_translation(self) -> None:
        """旧 v2 配置（文件里没有 reader_* 键）必须继承 translation 绑定。

        已修：`roles.get(role) or translation` 会被空 dict（truthy）骗过去；现在用
        `ModelServiceStore.role_state()` 区分 `unset`（从没写过 → 继承）/`cleared`/`invalid`（→ 明确失败）。
        """
        response = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto",
        })
        self.assertEqual(response.status_code, 200, response.text)
        job = self._wait_job()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["config"].get("model"), "model-t")
        self.assertEqual(self.routes[-1], ("svc-t", "model-t"))

    def test_explicitly_empty_role_does_not_silently_use_other_service(self) -> None:
        """显式清空（文件里 reader_fold 键存在但 service_id 为空）不得静默改用 translation。"""
        config = json.loads((self.runtime / "model-services.json").read_text())
        config["roles"]["reader_fold"] = {"service_id": "", "model": ""}
        (self.runtime / "model-services.json").write_text(json.dumps(config), encoding="utf-8")
        response = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("折叠", response.json()["detail"])
        self.assertEqual(self.routes, [], "不得发起任何模型请求")

    def test_deleted_service_fails_loudly_not_cross_service(self) -> None:
        """reader_fold 指向已删除/失效的服务：在提交期就明确失败，不静默改用 translation。

        契约（2026-10-25 冻结）：`cleared` / `invalid` 都返回 400 且文案指名角色；
        比「先建作业再失败」更早、更省，也不会产生一次无效作业记录。
        """
        config = json.loads((self.runtime / "model-services.json").read_text())
        config["roles"]["reader_fold"] = {"service_id": "svc-gone", "model": "m"}
        (self.runtime / "model-services.json").write_text(json.dumps(config), encoding="utf-8")
        started = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto",
        })
        self.assertEqual(started.status_code, 400, started.text)
        self.assertIn("折叠", started.json()["detail"])
        self.assertIn("svc-gone", started.json()["detail"])
        self.assertEqual(self.routes, [], "不得发起任何模型请求，也不得改用 translation 服务商")


    def test_explicit_binding_routes_to_named_service_and_model(self) -> None:
        """显式绑定（reader_chat=svc-b/model-b）→ 请求路由到该服务，source=explicit。"""
        config = json.loads((self.runtime / "model-services.json").read_text())
        config["services"].append({
            "service_id": "svc-b", "name": "服务B", "preset": "custom", "base_url": "https://b.invalid",
            "allow_plaintext_http": False,
            "models": [{"model": "model-b", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2}],
        })
        config["roles"]["reader_chat"] = {"service_id": "svc-b", "model": "model-b"}
        (self.runtime / "model-services.json").write_text(json.dumps(config), encoding="utf-8")
        self.roles_on_disk = self.manager.models.roles()
        sent = self.client.post(f"/projects/{self.project_id}/reader/chat/messages", headers=self.headers, json={
            "question": "显式绑定路由验证", "service_id": "svc-b", "model": "model-b"})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["binding"]["source"], "explicit")
        self.assertEqual(sent.json()["binding"]["model"], "model-b")
        self.assertIn(("svc-b", "model-b"), self.routes)

    def test_cleared_role_returns_400_without_provider_request(self) -> None:
        """显式清空（cleared）→ 400 指名角色，绝不静默回退 translation。"""
        config = json.loads((self.runtime / "model-services.json").read_text())
        config["roles"]["reader_chat"] = {"service_id": "", "model": ""}
        (self.runtime / "model-services.json").write_text(json.dumps(config), encoding="utf-8")
        response = self.client.post(f"/projects/{self.project_id}/reader/chat/messages", headers=self.headers, json={
            "question": "cleared 场景"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("阅读对话", response.json()["detail"])
        self.assertEqual(self.routes, [], "cleared 后不得发起任何模型请求")

    def test_invalid_model_in_role_returns_400(self) -> None:
        """角色指向的 model 已从服务商删除（invalid）→ 400 指名不可用模型，不回退。"""
        config = json.loads((self.runtime / "model-services.json").read_text())
        config["roles"]["reader_chat"] = {"service_id": "svc-t", "model": "deleted-model"}
        (self.runtime / "model-services.json").write_text(json.dumps(config), encoding="utf-8")
        response = self.client.post(f"/projects/{self.project_id}/reader/chat/messages", headers=self.headers, json={
            "question": "invalid 场景"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("不可用", response.json()["detail"])
        self.assertEqual(self.routes, [])


if __name__ == "__main__":
    unittest.main()


class ReaderChatTests(unittest.TestCase):
    """统一对话：一次性引用快照、按条冻结模型、停止不落库、失败可重试、历史可清空。"""

    def setUp(self) -> None:
        import types

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._previous = {key: os.environ.get(key) for key in ("LITERATURE_RUNTIME", "LITERATURE_LIBRARY")}
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        self.store, self.project_id = make_store(self.root)
        from fastapi.testclient import TestClient

        from literature.api import create_app

        self.client = TestClient(create_app(self.store), raise_server_exceptions=False)
        self.manager = self.client.app.state.job_manager
        self.headers = {"x-literature-token": self.client.app.state.session_token}
        self._reply_text = "银器沿丝路流通 [#b-p-1]。"
        self._delay = 0.0
        self._error = ""
        self._seen: list[tuple[str, str]] = []

        class Fake:
            config = MockProvider().config

            def complete(_self, *, system: str, user: str) -> ProviderResponse:
                import time as _time

                outer._seen.append((system, user))
                if outer._delay:
                    _time.sleep(outer._delay)
                if outer._error:
                    from literature.providers import ProviderError

                    raise ProviderError(outer._error)
                return ProviderResponse(
                    content=outer._reply_text, provider="fake", model="actual-model-1",
                    request_id="r", input_tokens=10, output_tokens=5, estimated_cost=0.0,
                )

            def close(_self) -> None:
                pass

        outer = self
        bindings = {
            "structure": {"service_id": "svc-a", "model": "model-a"},
            "translation": {"service_id": "svc-a", "model": "model-a"},
            "reader_fold": {"service_id": "svc-a", "model": "model-a"},
            "reader_chat": {"service_id": "svc-b", "model": "model-b"},
        }

        def service_of(service_id: str) -> dict:
            return {
                "service_id": service_id, "name": f"服务 {service_id}", "preset": "custom",
                "base_url": "https://example.invalid", "allow_plaintext_http": False,
                "models": [{"model": "model-a", "name": "模型 A"}, {"model": "model-b", "name": "模型 B"}],
            }

        self.manager.models = types.SimpleNamespace(
            roles=lambda: dict(bindings),
            role_binding=lambda role: dict(bindings.get(role) or {"service_id": "", "model": ""}),
            role_state=lambda role: {
                "role": role, "origin": "bound" if bindings.get(role, {}).get("service_id") else "unset",
                "binding": dict(bindings.get(role) or {"service_id": "", "model": ""}), "problem": "",
            },
            get=service_of,
        )
        self.manager.provider_from = lambda service_id, provider_input=None, model="": Fake()

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        for key, value in self._previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def url(self, suffix: str) -> str:
        return f"/projects/{self.project_id}/reader{suffix}"

    def _wait(self, assistant_id: str, timeout: float = 10.0) -> dict:
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            chat = self.client.get(self.url("/chat"), headers=self.headers).json()
            message = next(item for item in chat["messages"] if item["id"] == assistant_id)
            if message["state"] != "sending":
                return message
            _time.sleep(0.05)
        self.fail("回答未在超时内结束")

    def test_quote_is_frozen_per_message_and_model_is_recorded(self) -> None:
        sent = self.client.post(self.url("/chat/messages"), headers=self.headers, json={
            "question": "这段说明什么？",
            "service_id": "svc-a", "model": "model-a",
            "quote": {"block_id": "b-p-1", "pane": "trans", "text": "银器沿丝绸之路流动。"},
        })
        self.assertEqual(sent.status_code, 200, sent.text)
        assistant_id = sent.json()["assistant_message_id"]
        message = self._wait(assistant_id)
        self.assertEqual(message["state"], "done")
        self.assertEqual(message["answer"], self._reply_text)
        # 契约（2026-10-25 修正）：`binding.model` 是**路由身份**（重试/复用要用它），
        # 服务端回报的实际模型名放在 `binding.actual` 仅作展示。
        # 证据：真实服务把 binding 覆盖成响应里的 `Qwen` 后，重试会发一个服务商下不存在的 model → 400。
        self.assertEqual(message["binding"]["model"], "model-a")
        self.assertEqual(message["binding"]["service_id"], "svc-a")
        self.assertEqual(message["binding"]["actual"], "actual-model-1")
        self.assertIn("actual-model-1", message["model"])
        self.assertIn("b-p-1", [item["block_id"] for item in message["citations"]])

        chat = self.client.get(self.url("/chat"), headers=self.headers).json()
        user = chat["messages"][0]
        self.assertEqual(user["quote"]["pane"], "trans")
        self.assertEqual(user["quote"]["block_id"], "b-p-1")
        self.assertEqual(user["quote"]["snippet"], "银器沿丝绸之路流动。")
        # 引用证据永远是原文：译文引用也给出原文 block 文本。
        self.assertIn("Silver vessels", user["quote"]["srcText"])
        # 引用随这条消息冻结：它的上下文里必须有选中文字与原文证据。
        self.assertIn("The selected text", self._seen[-1][1])
        self.assertIn("Silver vessels", self._seen[-1][1])

        below = self.client.post(self.url("/chat/messages"), headers=self.headers, json={
            "question": "全书结构如何？", "service_id": "svc-b", "model": "model-b",
        })
        self.assertEqual(below.status_code, 200)
        message = self._wait(below.json()["assistant_message_id"])
        self.assertEqual(message["binding"]["service_id"], "svc-b")
        self.assertNotIn("The selected text", self._seen[-1][1])
        self.assertIn("one line per section", self._seen[-1][1])

    def test_retry_after_failure_reuses_the_same_question(self) -> None:
        self._error = "鉴权失败"
        sent = self.client.post(self.url("/chat/messages"), headers=self.headers, json={
            "question": "这本书讲了什么？",
        })
        assistant_id = sent.json()["assistant_message_id"]
        message = self._wait(assistant_id)
        self.assertEqual(message["state"], "error")
        self.assertIn("鉴权失败", message["error"])

        self._error = ""
        retried = self.client.post(self.url(f"/chat/messages/{assistant_id}/retry"), headers=self.headers, json={
            "locale": "source",
        })
        self.assertEqual(retried.status_code, 200, retried.text)
        message = self._wait(retried.json()["assistant_message_id"])
        self.assertEqual(message["state"], "done")
        chat = self.client.get(self.url("/chat"), headers=self.headers).json()
        self.assertEqual([item["role"] for item in chat["messages"]],
                         ["user", "assistant", "user", "assistant"])

    def test_stop_discards_a_late_answer(self) -> None:
        self._delay = 0.6
        sent = self.client.post(self.url("/chat/messages"), headers=self.headers, json={
            "question": "慢慢想。",
        })
        assistant_id = sent.json()["assistant_message_id"]
        stopped = self.client.post(self.url(f"/chat/messages/{assistant_id}/stop"), headers=self.headers)
        self.assertEqual(stopped.status_code, 200)
        import time as _time

        _time.sleep(0.9)
        chat = self.client.get(self.url("/chat"), headers=self.headers).json()
        message = next(item for item in chat["messages"] if item["id"] == assistant_id)
        self.assertEqual(message["state"], "stopped")
        # 迟到的回答被丢弃：不写文本、不冒充成功。
        self.assertEqual(message["answer"], "")
        self.assertFalse(message.get("citations"))

    def test_chat_history_can_be_cleared_and_is_persisted(self) -> None:
        self.client.post(self.url("/chat/messages"), headers=self.headers, json={"question": "第一问"})
        chat = self.client.get(self.url("/chat"), headers=self.headers).json()
        self.assertTrue(chat["messages"])
        self.assertTrue((self.store.project_path(self.project_id) / "reader" / "chat.json").is_file())
        cleared = self.client.delete(self.url("/chat"), headers=self.headers)
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(self.client.get(self.url("/chat"), headers=self.headers).json()["messages"], [])

    def test_chat_requires_a_chat_model_binding(self) -> None:
        self.manager.models = type("Empty", (), {
            "roles": staticmethod(lambda: {}),
            "role_binding": staticmethod(lambda role: {}),
            "role_state": staticmethod(lambda role: {
                "role": role, "origin": "unset", "binding": {"service_id": "", "model": ""}, "problem": "",
            }),
            "get": staticmethod(lambda service_id: {}),
        })()
        response = self.client.post(self.url("/chat/messages"), headers=self.headers, json={"question": "?"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("阅读对话", response.json()["detail"])

    def test_fold_role_and_chat_role_are_independent(self) -> None:
        # 折叠走 reader_fold（svc-a/model-a），对话走 reader_chat（svc-b/model-b）：
        # 两条链路各自的默认模型互不影响。
        started = self.client.post(self.url("/layers"), headers=self.headers, json={
            "locale": "source", "mode": "auto",
        })
        self.assertEqual(started.status_code, 200, started.text)
        import time as _time

        deadline = _time.monotonic() + 10
        job = None
        while _time.monotonic() < deadline:
            job = self.client.get(self.url("/state"), headers=self.headers).json()["layer_job"]
            if job and job["status"] in ("completed", "failed"):
                break
            _time.sleep(0.05)
        self.assertEqual(job["config"]["model"], "model-a")
        sent = self.client.post(self.url("/chat/messages"), headers=self.headers, json={"question": "?"})
        self.assertEqual(sent.json()["binding"]["model"], "model-b")


if __name__ == "__main__":
    unittest.main()
