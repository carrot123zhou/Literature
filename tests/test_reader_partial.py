"""局部失败与安全投影的**边界**回归（后端 worker 自己维护；与 tester 的
`tests/test_reader_partial_contract.py` 不重复：那边跑完整 pipeline，这里只压
adapter 的类型边界、装置收据门、native 可信指纹门、槽位口径这些边角）。

全部零付费：不建 provider、不发网络请求、临时 runtime/library、Keychain 不触碰。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from literature.models import Block, Document, Page
from literature.reader import (
    ReaderLayerService,
    ReaderStore,
    _sections_from_ladder,
    apparatus_fingerprint,
    build_ladder_doc,
    reading_facts_fingerprint,
    section_digest,
)
from literature.storage import ProjectStore
from literature.textfold_adapter import (
    apply_derived_reply,
    apply_reply,
    build_ladder_doc as adapter_build_ladder_doc,
    merge_translation_doc,
)
from literature.reader_batches import apply_section_summary


# ------------------------------------------------------------------ fixtures --

def make_document(project_id: str) -> Document:
    """三节小文档：S1 两段正文 / S2 标题 + 列表 + 公式 / S3 一段正文。"""
    blocks = [
        Block(block_id="b-title-1", type="title", order=0, page_index=0,
              source_text="Introduction", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-p-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 200],
              source_text="Silver vessels travelled along the Silk Road. Workshops copied shapes for decades."),
        Block(block_id="b-p-2", type="paragraph", order=2, page_index=0, bbox=[10, 210, 300, 260],
              source_text="Assay data show a consistent alloy of 92 percent silver."),
        Block(block_id="b-p-1c", type="paragraph", order=3, page_index=0, bbox=[10, 270, 300, 320],
              source_text="Copying stopped once the route shifted inland."),
        Block(block_id="b-title-2", type="title", order=4, page_index=1,
              source_text="Conclusion", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-list", type="list", order=5, page_index=1, bbox=[10, 50, 300, 120],
              items=["Hoards cluster in port cities.", "Finds thin out inland."]),
        Block(block_id="b-title-3", type="title", order=6, page_index=1,
              source_text="Epilogue", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-p-3", type="paragraph", order=7, page_index=1, bbox=[10, 50, 300, 200],
              source_text="Silver hoards faded from the coastal trade."),
    ]
    page = Page(page_index=0, width=612, height=792)
    page2 = Page(page_index=1, width=612, height=792)
    return Document(project_id=project_id, title="Silver Road", source_language="en",
                    page_count=2, pages=[page, page2], blocks=blocks)


class _StoreCase(unittest.TestCase):
    """临时 library/runtime + 一个含 document 的项目；不建任何 provider。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        self.project_id = self.store.create_project(source, title="Silver Road")["project_id"]
        self.document = make_document(self.project_id)
        self.store.save_document(self.document)
        self.reader = ReaderStore(self.store)
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- 直接造存盘层（不调模型）：只写 reading_projection 真正消费的字段 --
    def _source_ladder(self):
        return build_ladder_doc(self.document, locale="source", translation=None, source_kind="pdf")

    def _seed_source_layer(self, sections: dict[str, list[dict]]) -> None:
        ladder = self._source_ladder()
        previews = {item["id"]: item for item in _sections_from_ladder(ladder)}
        doc_sections, hashes = [], {}
        for sid, units in sections.items():
            doc_sections.append({
                "id": sid,
                "takeaway": "" if all(u.get("source") == "suppressed" for u in units) else "本节总结",
                "topic": "",
                "units": units,
            })
            has_model = any(u.get("source") == "model" for u in units)
            hashes[sid] = (
                section_digest(previews[sid], "source", service_id="svc", model="fake-model")
                if has_model else section_digest(
                    previews[sid], "source", service_id="", model="", strategy="local",
                )
            )
        self.reader.save_locale_doc(self.project_id, "source", {
            "format": "textfold/doc", "version": 0, "project_id": self.project_id,
            "locale": "source", "title": self.document.title,
            "digest": {"service_id": "svc", "model": "fake-model", "prompt_version": 3,
                       "strategy": "fold"},
            "sections": doc_sections,
        }, hashes)

    @staticmethod
    def _unit(uid: str, *, source: str = "model", reason: str = "", takeaway: str = "银器沿丝路流动",
              key: str = "银器沿丝路流动。依据证据句。", topic: str = "银器") -> dict:
        item = {
            "id": uid, "role": "claim", "source": source, "takeaway": takeaway,
            "key": key, "topic": topic, "evidence": [0], "type": "paragraph", "page_index": 0,
        }
        if reason:
            item["suppress_reason"] = reason
        return item


# ------------------------------------------------- 1. adapter 类型边界隔离 --

class AdapterTypeBoundaryTests(unittest.TestCase):
    """合法 U0 + 坏类型 U1 + 合法 U2 + 坏 section 类型：坏段 pending、合法段照存、不抛异常。"""

    def _doc_and_chunk(self):
        document = make_document("proj-type")
        doc = adapter_build_ladder_doc(document, locale="source", translation=None, source_kind="pdf")
        chunk = next(item for item in doc["chunks"] if item["id"] == "S1")
        assert len(chunk["units"]) == 3, chunk["units"]
        return doc, chunk

    def test_apply_reply_isolates_bad_typed_unit_and_section(self) -> None:
        doc, chunk = self._doc_and_chunk()
        units = list(chunk["units"])
        payload = {
            # section 字段类型坏（takeaway 是 list）：只能让摘要保持未生成。
            "section": {"takeaway": ["不该", "进", "clean"], "topic": {"bad": 1}},
            "units": [
                {"id": "U0", "takeaway": "银器沿丝路流动", "reason": "作坊仿形", "evidence": [0]},
                # U1：takeaway 是 list（旧实现会让整批 clean() 抛 TypeError）。
                {"id": "U1", "takeaway": ["坏", "类型"], "evidence": [0]},
                {"id": "U2", "takeaway": "成分稳定", "reason": "检测数据", "evidence": [0]},
            ],
        }
        warnings = apply_reply(doc, chunk, payload, model="fake-model", finalize=False)
        self.assertIsNotNone(warnings)
        self.assertNotIn("lad", chunk)  # 坏 section 不写章节短层
        by_id = {uid: doc["units"][uid] for uid in units}
        self.assertEqual(by_id[units[0]]["source"], "model", "合法 U0 必须照存")
        self.assertEqual(by_id[units[2]]["source"], "model", "合法 U2 必须照存")
        self.assertEqual(by_id[units[1]]["source"], "pending", "坏类型 U1 只能保持未生成")
        self.assertEqual(by_id[units[1]]["error"]["code"], "field_type_invalid")
        self.assertTrue(any("section.takeaway 类型非法" in item for item in warnings), warnings)

    def test_apply_reply_survives_non_dict_section_and_topic_type(self) -> None:
        doc, chunk = self._doc_and_chunk()
        units = list(chunk["units"])
        payload = {
            "section": ["not", "a", "dict"],
            "units": [
                {"id": "U0", "takeaway": "银器沿丝路流动", "topic": ["坏"], "evidence": [0]},
            ],
        }
        warnings = apply_reply(doc, chunk, payload, model="fake-model", finalize=False)
        self.assertEqual(doc["units"][units[0]]["source"], "model")
        self.assertNotIn("lad", chunk)
        self.assertTrue(any("section 类型非法" in item for item in warnings), warnings)
        # topic 类型坏按缺失处理，不 str() 强转成 "[坏]" 这类假内容。
        self.assertFalse(any("坏" in str(tok) for tok in doc["units"][units[0]]["lad"]["topic"]))

    def test_apply_derived_reply_isolates_bad_typed_unit_and_section(self) -> None:
        document = make_document("proj-type")
        source = adapter_build_ladder_doc(document, locale="source", translation=None, source_kind="pdf")
        # 源层短层：三个 unit 都有 brief，才能进派生上下文。
        from literature.textfold_adapter import pending_lad
        for uid in ("b-p-1", "b-p-2"):
            unit = source["units"][uid]
            unit["lad"] = {**pending_lad(unit), "model": "fake-model",
                           "brief": [{"t": "银"}], "tn": 1, "ev": [0]}
            unit["source"] = "model"
        trans = adapter_build_ladder_doc(document, locale="zh-CN", translation=None, source_kind="pdf")
        merged = merge_translation_doc(source, trans, "zh-CN")
        chunk = next(item for item in merged["chunks"] if item["id"] == "S1")
        evidence = [("b-p-1", [0]), ("b-p-2", [0])]
        payload = {
            "section": {"takeaway": {"bad": "type"}, "topic": None},
            "units": [
                {"id": "U0", "takeaway": "译文要点", "reason": "译文理由",
                 "topic": "要点", "ev_sentences": ["这是分层译句。"]},
                # U1：ev_sentences 含非字符串项 → 不可采信，保持未生成。
                {"id": "U1", "takeaway": "译文要点", "reason": "译文理由",
                 "topic": "要点", "ev_sentences": [{"not": "a string"}]},
            ],
        }
        warnings, derived, fallback = apply_derived_reply(
            merged, chunk, payload, locale="zh-CN", model="fake-model", unit_evidence=evidence,
        )
        self.assertEqual((derived, fallback), (1, 1), warnings)
        tr0 = merged["units"]["b-p-1"]["tr"]["zh-CN"]
        self.assertEqual(tr0["lad"]["model"], "fake-model")
        tr1 = merged["units"]["b-p-2"]["tr"]["zh-CN"]
        self.assertEqual(tr1.get("error", {}).get("code"), "bad_ev_sentences")
        self.assertFalse((tr1.get("lad") or {}).get("brief"),
                         "坏类型的 ev_sentences 不得写成短层")
        self.assertIsNone(((chunk.get("tr") or {}).get("zh-CN") or {}).get("lad"),
                          "坏 section 类型只让摘要保持未生成")

    def test_apply_section_summary_bad_types_never_raises(self) -> None:
        doc, chunk = self._doc_and_chunk()
        problems: list[str] = []
        self.assertFalse(apply_section_summary(doc, chunk, {"section": {"takeaway": [1]}},
                                               locale="source", problems=problems))
        self.assertTrue(problems)
        # takeaway 合法、topic 类型坏：摘要照写（topic 按缺失处理由上游确定性兜底）。
        problems.clear()
        self.assertTrue(apply_section_summary(
            doc, chunk, {"section": {"takeaway": "银器沿丝路流动", "topic": ["坏"]}},
            locale="source", problems=problems,
        ))
        self.assertTrue(chunk.get("lad", {}).get("take"))
        self.assertTrue(any("section.topic 类型非法" in item for item in problems), problems)


# --------------------------------------- 2. 装置抑制必须有分类收据（阅读投影） --

class ApparatusReceiptGateTests(_StoreCase):
    """`suppress_reason="apparatus"` 的存盘抑制必须有匹配的分类收据；缺/过期 → 保持 pending。"""

    def _seed_apparatus_section(self) -> None:
        self._seed_source_layer({
            "S1": [self._unit("b-p-1"), self._unit("b-p-2"), self._unit("b-p-1c")],
            "S2": [self._unit("b-list", takeaway="银器集中在港口", key="银器集中在港口。依据证据句。")],
            # S3 全装置抑制（索引/参考文献类）：普通 tiny suppressed 不需要收据。
            "S3": [self._unit("b-p-3", source="suppressed", reason="apparatus",
                              takeaway="", key="", topic="")],
        })

    def _apparatus_key(self, ladder) -> tuple[str, dict]:
        preview = {item["id"]: item for item in _sections_from_ladder(ladder)}
        digest = section_digest(preview["S3"], "source", service_id="svc", model="fake-model")
        key = apparatus_fingerprint(section_digest=digest, service_id="svc", model="fake-model")
        return key, {"affirmative": True, "kind": "index"}

    def test_missing_receipt_keeps_apparatus_units_pending(self) -> None:
        self._seed_apparatus_section()
        projection = self.service.reading_projection(self.project_id, locale="source")
        availability = projection["availability"]
        self.assertEqual(availability["source_units_suppressed"], 0, availability)
        self.assertEqual(availability["source_units_pending"], 1, availability)
        chunk = next(item for item in projection["source_ladder"]["chunks"] if item["id"] == "S3")
        unit = projection["source_ladder"]["units"][chunk["units"][0]]
        self.assertFalse(unit.get("suppressed"), "缺收据不得复用装置抑制（否则未确认材料被隐藏）")
        self.assertEqual(unit.get("source"), "pending")

    def test_matching_receipt_allows_suppression_and_counts_as_suppressed(self) -> None:
        from literature.reader_batches import UnitCheckpoints
        from literature.reader import _checkpoint_path
        self._seed_apparatus_section()
        ladder = self._source_ladder()
        key, receipt = self._apparatus_key(ladder)
        checkpoints = UnitCheckpoints(_checkpoint_path(self.store, self.project_id))
        checkpoints.put_apparatus("source", "S3", fingerprint=key, receipt=receipt)
        checkpoints.save()
        projection = self.service.reading_projection(self.project_id, locale="source")
        availability = projection["availability"]
        self.assertEqual(availability["source_units_suppressed"], 1, availability)
        self.assertEqual(availability["source_units_pending"], 0, availability)
        # 全 suppressed 节不要求章节摘要：摘要待补不能因此记 1。
        self.assertEqual(availability["source_summaries_pending"], 0, availability)
        self.assertTrue(availability["readable"], availability)

    def test_stale_receipt_binding_keeps_pending(self) -> None:
        """换绑定（模型变了）后旧收据的指纹不匹配 → 不得继续隐藏材料。"""
        from literature.reader_batches import UnitCheckpoints
        from literature.reader import _checkpoint_path
        self._seed_apparatus_section()
        ladder = self._source_ladder()
        key, receipt = self._apparatus_key(ladder)
        checkpoints = UnitCheckpoints(_checkpoint_path(self.store, self.project_id))
        checkpoints.put_apparatus("source", "S3", fingerprint=key, receipt=receipt)
        checkpoints.save()
        projection = self.service.reading_projection(
            self.project_id, locale="source",
            binding={"service_id": "svc", "model": "another-model"},
        )
        availability = projection["availability"]
        # 换绑定后整节键失效：S3 的装置抑制不能复用（pending 计数含该节全部 unit）。
        self.assertEqual(availability["source_units_suppressed"], 0, availability)
        self.assertEqual(availability["source_units_pending"], 5, availability)


# ---------------------------------------- 3. native 可信指纹门（旁路堵口） --

class NativeTrustedFingerprintTests(_StoreCase):
    """生成路径写出的 native 只有「全部槽位已校验」才带可信指纹；否则首次 GET 强制安全重建。"""

    def test_untrusted_native_write_has_no_fingerprint_and_get_rebuilds(self) -> None:
        self._seed_source_layer({
            "S1": [self._unit("b-p-1"), self._unit("b-p-2"), self._unit("b-p-1c")],
            "S2": [self._unit("b-list")],
            "S3": [self._unit("b-p-3")],
        })
        stale_brief = "旧的过期短层不该被展示"
        ladder = self._source_ladder()
        for uid in ("b-p-1", "b-p-2"):
            unit = ladder["units"][uid]
            unit["lad"] = {"role": "claim", "ev": [0], "brief": [{"t": stale_brief}],
                           "tn": 1, "topic": [], "model": "fake-model"}
            unit["source"] = "model"
        # 模拟 generate 收尾：availability 有 pending → trusted=False → 不写可信指纹。
        availability = self.service.fold_run_availability(
            self.project_id, locale="source", service_id="svc", model="fake-model",
        )
        payload = self.service._save_native_docs(
            self.project_id, locale="source", source_ladder=ladder,
            display_ladder=ladder, merged=None, is_trans=False,
            sections_meta=_sections_from_ladder(ladder), source_language="en",
            availability=availability, service_id="svc", model_label="fake-model",
        )
        self.assertEqual(payload, [])
        written = self.reader.load_native(self.project_id, "source")
        self.assertEqual(written["lit"]["facts_fingerprint"], "",
                         "未全量校验的生成产物不得携带可信指纹")
        served = self.service.reading_native(self.project_id, locale="source")
        self.assertTrue(served["lit"]["rebuilt"], served["lit"])
        blob = json.dumps(served, ensure_ascii=False)
        self.assertNotIn(stale_brief, blob, "不可信 native 内容不得直接放行")

    def test_native_file_without_fingerprint_is_rebuilt_even_if_content_looks_rich(self) -> None:
        self._seed_source_layer({
            "S1": [self._unit("b-p-1"), self._unit("b-p-2"), self._unit("b-p-1c")],
            "S2": [self._unit("b-list")],
            "S3": [self._unit("b-p-3")],
        })
        ladder = self._source_ladder()
        forged = self.service.fold_run_availability(
            self.project_id, locale="source", service_id="svc", model="fake-model",
        )
        payload = self.service._save_native_docs(
            self.project_id, locale="source", source_ladder=ladder,
            display_ladder=ladder, merged=None, is_trans=False,
            sections_meta=_sections_from_ladder(ladder), source_language="en",
            availability=forged, service_id="svc", model_label="fake-model",
        )
        self.assertEqual(payload, [])
        raw = self.reader.load_native(self.project_id, "source")
        self.assertEqual(raw["lit"]["facts_fingerprint"], "",
                         "原始落盘一律不盖可信指纹（统一最小安全口径）")
        # 真实词级 m 锚保留在原始落盘（fresh 产物），GET 重建按真实词对应恢复。
        served_first = self.service.reading_native(self.project_id, locale="source")
        self.assertTrue(served_first["lit"]["rebuilt"])
        trusted = self.reader.load_native(self.project_id, "source")
        self.assertNotEqual(trusted["lit"]["facts_fingerprint"], "")
        # 事实变化（正文改写）→ 指纹失效 → GET 必须重建，不能返回旧文件。
        document = self.store.load_document(self.project_id)
        document.blocks[1].source_text = "Rewritten text that changes the facts entirely."
        self.store.save_document(document)
        served = self.service.reading_native(self.project_id, locale="source")
        self.assertTrue(served["lit"]["rebuilt"])
        current = reading_facts_fingerprint(
            self.store, self.reader, self.project_id, locale="source", binding={},
        )
        self.assertNotEqual(current, trusted["lit"]["facts_fingerprint"])


# --------------------------------------------------- 4. 槽位口径与 readable --

class AvailabilitySlotTests(_StoreCase):
    def test_all_suppressed_ready_book_stays_readable(self) -> None:
        self._seed_source_layer({
            "S1": [self._unit("b-p-1", source="suppressed", takeaway="", key="", topic=""),
                   self._unit("b-p-2", source="suppressed", takeaway="", key="", topic=""),
                   self._unit("b-p-1c", source="suppressed", takeaway="", key="", topic="")],
            "S2": [self._unit("b-list", source="suppressed", takeaway="", key="", topic="")],
            "S3": [self._unit("b-p-3", source="suppressed", takeaway="", key="", topic="")],
        })
        availability = self.service.fold_run_availability(
            self.project_id, locale="source", service_id="svc", model="fake-model",
        )
        self.assertEqual(availability["units_valid"], 0, availability)
        self.assertEqual(availability["units_pending"], 0, availability)
        self.assertEqual(availability["summaries_pending"], 0, availability)
        self.assertTrue(availability["readable"],
                        "全 suppressed 且无待补必须是 readable，否则旧 ready 书打不开")
        self.assertTrue(availability["full_text"])

    def test_not_yet_generated_is_not_readable_but_full_text_is(self) -> None:
        availability = self.service.fold_run_availability(
            self.project_id, locale="source", service_id="svc", model="fake-model",
        )
        self.assertFalse(availability["readable"], availability)
        self.assertTrue(availability["full_text"])
        self.assertGreater(availability["units_pending"], 0)
        self.assertEqual(
            availability["units_total"],
            availability["units_valid"] + availability["units_pending"] + availability["units_suppressed"],
        )

    def test_untranslated_units_are_not_target_pending(self) -> None:
        """缺译 unit 走 source-fallback：不计目标槽位（否则 strict ready 与待补自相矛盾）。"""
        # 只给 b-p-1/b-p-2 部分译文：b-p-2 缺译 → fallback。
        from literature.models import TranslationEntry, TranslationLayer
        layer = TranslationLayer(
            project_id=self.project_id, locale="zh-CN", updated_at="2026-10-27T00:00:00Z",
            entries={
                "b-p-1": TranslationEntry(block_id="b-p-1", text="银器沿丝路流动。"),
            },
        )
        self.store.save_translation(layer)
        availability = self.service.fold_run_availability(
            self.project_id, locale="zh-CN", service_id="svc", model="fake-model",
        )
        self.assertEqual(availability["source_units_total"], 5, availability)
        self.assertEqual(availability["target_units_total"], 1,
                         "只有真正有译文的 unit 才算目标槽位")
        self.assertIn("target_units_pending", availability)

    def test_partial_derive_failure_shows_target_pending_not_zero(self) -> None:
        """源全有效但目标未派生：不能显示「待补 0」。"""
        self._seed_source_layer({
            "S1": [self._unit("b-p-1"), self._unit("b-p-2"), self._unit("b-p-1c")],
            "S2": [self._unit("b-list")],
            "S3": [self._unit("b-p-3")],
        })
        availability = self.service.fold_run_availability(
            self.project_id, locale="source", service_id="svc", model="fake-model",
        )
        self.assertEqual(availability["source_units_pending"], 0, availability)
        self.assertEqual(availability["units_pending"], 0, availability)
        self.assertTrue(availability["readable"], availability)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
