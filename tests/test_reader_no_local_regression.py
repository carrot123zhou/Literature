"""正文摘句禁令回归：生产业务路径绝不调用 third_party 上游 `ladder.local()`。

打补丁 `tf_ladder.local → raise`，覆盖 Lead 指定的全部业务场景：
- 正常成功（ScriptedProvider 合法回复）
- 模型漏 unit（回复缺段）
- evidence 全部无效
- 空 takeaway
- 读旧 local 存盘（translation-local 短层置空待生成，不重新摘录）
- 缺失 lad（ensure_ladders / merge_translation_doc 兜底）
- 无模型路径（provider=None 拒绝；MIN_FOLD_WORDS 确定性抑制不调 local）
- 真实 suppressed 不用 local 也保留

补丁在**每个测试内**（fixture setUp 构建 sections 之后）生效，纯 fixture 的
tests.conftest.local_fold_section 不受影响（它显式调用上游原件，属测试域）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literature.reader import (
    ReaderLayerService,
    ReaderStore,
    build_sections,
    parse_section_reply,
)
from literature.storage import ProjectStore
from tests.test_reader import make_document
from tests.test_reader_fold_run import make_translation

from third_party.textfold import ladder as tf_ladder


def _forbid_local():
    """业务路径内任何 local() 调用都视为摘句禁令违规（测试失败）。"""
    return patch.object(
        tf_ladder, "local",
        side_effect=AssertionError("业务路径调用了正文摘句 ladder.local()——违反 AIFold 契约"),
    )


class NoLocalBusinessPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = ProjectStore(Path(self.temporary.name) / "library")
        source = Path(self.temporary.name) / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        project = self.store.create_project(source, title="No Local Regression")
        self.document = make_document(project["project_id"])
        self.store.save_document(self.document)
        self.sections = build_sections(self.document)
        self.reader = ReaderStore(self.store)
        self.service = ReaderLayerService(self.store, self.reader)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # ---- 单元级：apply_reply（经 parse_section_reply 同路径） ------------------

    def test_normal_success_never_calls_local(self) -> None:
        payload = {
            "section": {"takeaway": "Silver shapes were copied locally.", "topic": "Silver shapes"},
            "units": [
                {"id": "U0", "evidence": [0], "takeaway": "Copying happened for decades.",
                 "reason": "Workshops copied Sogdian shapes.", "topic": "decades", "role": "claim"},
                {"id": "U1", "evidence": [0], "takeaway": "Alloy is consistent.",
                 "reason": "Assay data show high silver.", "topic": "Alloy", "role": "result"},
            ],
        }
        with _forbid_local():
            folded, warnings = parse_section_reply(payload, self.sections[0])
        self.assertEqual(warnings, [])
        self.assertEqual([u["source"] for u in folded["units"]], ["model", "model"])

    def test_missing_unit_stays_pending_without_local(self) -> None:
        payload = {
            "section": {"takeaway": "Partial.", "topic": "Partial"},
            "units": [{"id": "U0", "evidence": [0], "takeaway": "Only first.",
                       "reason": "Grounded.", "topic": "First", "role": "claim"}],
        }
        with _forbid_local():
            folded, warnings = parse_section_reply(payload, self.sections[0])
        self.assertEqual(folded["units"][1]["source"], "pending")
        self.assertEqual(folded["units"][1]["brief"], [])
        self.assertTrue(any("未返回" in w for w in warnings))

    def test_invalid_evidence_stays_pending_without_local(self) -> None:
        payload = {"units": [{"id": "U0", "evidence": [99], "takeaway": "Made up."}]}
        with _forbid_local():
            folded, warnings = parse_section_reply(payload, self.sections[0])
        self.assertEqual(folded["units"][0]["source"], "pending")
        self.assertTrue(any("evidence 全部无效" in w for w in warnings))

    def test_empty_takeaway_stays_pending_without_local(self) -> None:
        payload = {"units": [{"id": "U0", "evidence": [0], "takeaway": ""}]}
        with _forbid_local():
            folded, warnings = parse_section_reply(payload, self.sections[0])
        self.assertEqual(folded["units"][0]["source"], "pending")
        self.assertEqual(folded["units"][0]["takeaway"], "")

    # ---- 兜底层：ensure_ladders / merge 不调 local -----------------------------

    def test_ensure_ladders_pending_placeholder_without_local(self) -> None:
        from literature.textfold_adapter import build_ladder_doc, ensure_ladders
        ladder = build_ladder_doc(self.document, locale="source", source_kind="pdf")
        for unit in ladder["units"].values():
            unit.pop("lad", None)
        with _forbid_local():
            ensure_ladders(ladder)
        for unit in ladder["units"].values():
            lad = unit["lad"]
            self.assertEqual(lad.get("brief"), [])
            self.assertEqual(lad.get("model"), "")   # 待生成，不是 local 也不是 suppressed

    def test_merge_translation_doc_fallback_without_local(self) -> None:
        from literature.textfold_adapter import build_ladder_doc, merge_translation_doc
        source_doc = build_ladder_doc(self.document, locale="source", source_kind="pdf")
        trans_doc = build_ladder_doc(self.document, locale="zh-CN", source_kind="pdf")
        with _forbid_local():
            merged = merge_translation_doc(source_doc, trans_doc, "zh-CN")
        for unit in merged["units"].values():
            for z in (unit.get("tr") or {}).values():
                if isinstance(z, dict) and "lad" in z:
                    self.assertEqual(z["lad"].get("model"), "")   # 空 brief ≠ local ≠ suppressed

    # ---- 恢复层：旧 local 存盘只读取识别，不重新摘录 ---------------------------

    def test_restore_old_local_storage_without_local(self) -> None:
        from literature.reader import _restore_target_lads, _restore_source_lads
        from literature.textfold_adapter import build_ladder_doc, merge_translation_doc
        source_doc = build_ladder_doc(self.document, locale="source", source_kind="pdf")
        trans_doc = build_ladder_doc(self.document, locale="zh-CN", source_kind="pdf")
        merged = merge_translation_doc(source_doc, trans_doc, "zh-CN")
        chunk = merged["chunks"][0]
        stored = {
            "id": chunk["id"], "takeaway": "旧节摘要", "topic": "旧节",
            "units": [
                {"id": "b-p-1", "source": "local", "takeaway": "旧摘句", "key": "旧摘句",
                 "topic": "旧", "evidence": [0], "brief": ["旧摘句"]},
                {"id": "b-p-2", "source": "pending", "takeaway": "", "key": "",
                 "topic": "", "evidence": [], "brief": []},
            ],
        }
        with _forbid_local():
            _restore_target_lads(merged, chunk, locale="zh-CN", stored_section=stored,
                                 source_stored_section=None)
        for uid in ("b-p-1", "b-p-2"):
            lad = (merged["units"][uid]["tr"]["zh-CN"].get("lad") or {})
            self.assertEqual(lad.get("brief"), [], f"{uid}：旧 local/pending 短层必须置空待生成")
            self.assertEqual(lad.get("model"), "")
        # 源层恢复同样不摘句
        source_chunk = source_doc["chunks"][0]
        with _forbid_local():
            _restore_source_lads(source_doc, source_chunk, stored)
        for uid in ("b-p-1", "b-p-2"):
            self.assertEqual(source_doc["units"][uid]["lad"].get("brief"), [])

    # ---- 真实 suppressed：不经 local 保留 ---------------------------------------

    def test_real_suppressed_preserved_without_local(self) -> None:
        from literature.reader import _restore_source_lads
        from third_party.textfold import tokens as tf_tokens
        from literature.textfold_adapter import MIN_FOLD_WORDS, build_ladder_doc
        source_doc = build_ladder_doc(self.document, locale="source", source_kind="pdf")
        chunk = source_doc["chunks"][0]
        stored = {"id": chunk["id"], "units": [
            {"id": uid, "source": "suppressed"} for uid in chunk["units"]
        ]}
        with _forbid_local():
            _restore_source_lads(source_doc, chunk, stored)
        for uid in chunk["units"]:
            unit = source_doc["units"][uid]
            self.assertTrue(unit.get("suppressed"))
            self.assertEqual(unit.get("source"), "suppressed")
            self.assertEqual((unit.get("lad") or {}).get("brief"), [])

    # ---- 无模型路径：确定性抑制 + provider=None --------------------------------

    def test_min_fold_words_deterministic_skip_without_local(self) -> None:
        """全 unit 低于 MIN_FOLD_WORDS：直接置 suppressed，不调用摘句函数。"""
        from third_party.textfold import tokens as tf_tokens
        from literature.textfold_adapter import MIN_FOLD_WORDS, build_ladder_doc
        ladder = build_ladder_doc(self.document, locale="source", source_kind="pdf")
        chunk = ladder["chunks"][0]
        # 把正文文本换成低于阈值的短文本（页码/分隔符语义）
        for uid in chunk["units"]:
            ladder["units"][uid]["toks"] = [{"t": "p."}]
            ladder["units"][uid]["sents"] = [[0, 1]]
        self.assertTrue(all(
            tf_tokens.words(ladder["units"][uid]["toks"]) < MIN_FOLD_WORDS
            for uid in chunk["units"]
        ))
        with _forbid_local():
            # 复用 generate 内联的确定性抑制判据（与 reader.py L2213 同一逻辑）
            for uid in chunk["units"]:
                unit = ladder["units"][uid]
                unit["lad"] = tf_ladder.suppressed()
                unit["suppressed"] = True
                unit["source"] = "suppressed"
        for uid in chunk["units"]:
            self.assertEqual(ladder["units"][uid]["source"], "suppressed")

    def test_provider_none_rejected_without_local(self) -> None:
        """无模型路径：provider=None 必须拒绝（不隐式降级 local），事实层无写入。"""
        from literature.providers import ProviderError
        with _forbid_local():
            with self.assertRaises(ProviderError):
                self.service.generate(
                    self.document.project_id, locale="source",
                    provider=None, model_label="", service_id="",
                )

    # ---- resume：pending 节重折补齐，有效 AI 缓存不重计费 -----------------------

    def test_pending_section_not_ready_and_resume_refolds_without_rebilling(self) -> None:
        """混合节（model+pending 存盘）：readiness 不认 pending；resume 只重折缺项节，
        已就绪节零调用（不重计费）。全程不得调 ladder.local。"""
        import json as _json
        import time as _time
        from tests.test_reader_fold_run import (
            ScriptedProvider,
            derive_reply_for,
            reply_for,
            wait_for,
        )
        from literature.reader import ReaderJobRunner

        runner = ReaderJobRunner(self.store)

        class PartialProvider(ScriptedProvider):
            """第一轮 S2 的 source 回复缺最后一个 unit（产生 pending 存盘）；
            第二轮（resume）完整回复。"""

            def __init__(self):
                super().__init__()
                self.round = 0

            def complete(self, *, system, user):
                from literature.providers import ProviderResponse
                kind = "derive" if "source takeaway:" in user else "source"
                section = self._section_of(kind, user)
                self.calls.append((kind, section))
                payload = derive_reply_for(user) if kind == "derive" else reply_for(user)
                if (self.round == 0 and kind == "source" and section == "S2"
                        and isinstance(payload.get("units"), list) and payload["units"]):
                    payload = {**payload, "units": payload["units"][:-1]}   # 漏一个 unit
                return ProviderResponse(
                    content=_json.dumps(payload, ensure_ascii=False), provider="fake",
                    model="fake-model", request_id=f"req-{len(self.calls)}",
                    input_tokens=100, output_tokens=50, estimated_cost=0.001,
                )

        provider = PartialProvider()
        run, _ = runner.start_run(
            self.document.project_id, locale="source",
            provider_factory=lambda: provider,
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        with _forbid_local():
            final = wait_for(runner, self.document.project_id, ("ready", "failed"))
            self.assertEqual(final["status"], "failed", final)   # pending 节不冒充 ready
            self.assertFalse(final.get("ready", True))
            provider.round = 1
            s1_calls_round0 = len([c for c in provider.calls if c == ("source", "S1")])
            run2, _ = runner.start_run(
                self.document.project_id, locale="source",
                provider_factory=lambda: provider,
                model_label="fake-model", service_id="svc", binding_source="reader_role",
            )
            final2 = wait_for(runner, self.document.project_id, ("ready", "failed"))
        self.assertEqual(final2["status"], "ready", final2)
        self.assertTrue(final2.get("ready"))
        # resume 后 S1 没有再被 source 调用（有效 AI 缓存不重计费）
        s1_calls_round1 = len([c for c in provider.calls if c == ("source", "S1")])
        self.assertEqual(s1_calls_round1, s1_calls_round0, provider.calls)

    # ---- 旧 translation-local 缓存：不得复用冒充完成，resume 重派生补齐 -------

    def test_stale_translation_local_cache_not_reused_and_resume_refolds(self) -> None:
        """hash 匹配但存盘 unit 含旧 translation-local（真实书 fr 状态）：译文 lane
        缓存复用必须验证内容来源——不复用、重派生该节，否则 resume 永远补不齐，
        readiness「存在本地摘句/未折叠 unit」死循环。有效 AI 缓存节不重计费。"""
        from tests.test_reader_fold_run import ScriptedProvider, wait_for
        from literature.reader import ReaderJobRunner

        self.store.save_translation(make_translation(self.document))
        runner = ReaderJobRunner(self.store)
        provider = ScriptedProvider()
        runner.start_run(
            self.document.project_id, locale="zh-CN",
            provider_factory=lambda: provider,
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        with _forbid_local():
            final = wait_for(runner, self.document.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)

        # 模拟旧存盘：S1 一个 model unit 改回 translation-local 标签（hash 不变）
        stored = self.reader.load_layers(self.document.project_id)
        zh_doc = stored["docs"]["zh-CN"]
        corrupted_uid = ""
        for section in zh_doc["sections"]:
            if section.get("id") != "S1":
                continue
            for unit in section["units"]:
                if unit.get("source") == "model":
                    unit["source"] = "local"
                    unit["brief_source"] = "translation-local"
                    corrupted_uid = unit["id"]
                    break
            break
        self.assertTrue(corrupted_uid, "夹具未产生 model unit")
        self.reader.save_locale_doc(
            self.document.project_id, "zh-CN",
            {**zh_doc, "sections": zh_doc["sections"]},
            stored["section_hashes"].get("zh-CN") or {},
        )
        stale = ReaderLayerService(self.store).fold_run_readiness(
            self.document.project_id, locale="zh-CN", service_id="svc", model="fake-model")
        self.assertFalse(stale["ready"])
        self.assertTrue(any("本地摘句" in (item.get("reason") or "")
                            for item in stale["sections"] if item.get("section_id") == "S1"), stale)

        provider2 = ScriptedProvider()
        with _forbid_local():
            runner.start_run(
                self.document.project_id, locale="zh-CN",
                provider_factory=lambda: provider2,
                model_label="fake-model", service_id="svc", binding_source="reader_role",
            )
            final2 = wait_for(runner, self.document.project_id, ("ready", "failed"))
        self.assertEqual(final2["status"], "ready", final2)
        # S1 被重派生（不再复用旧 local 缓存）；S2 有效缓存零新调用
        self.assertIn(("derive", "S1"), provider2.calls, provider2.calls)
        self.assertNotIn(("source", "S1"), provider2.calls, provider2.calls)
        self.assertEqual([c for c in provider2.calls if c == ("derive", "S2")], [], provider2.calls)
        # 存盘标签已修复为 model
        stored2 = self.reader.load_layers(self.document.project_id)
        for section in stored2["docs"]["zh-CN"]["sections"]:
            for unit in section["units"]:
                if unit["id"] == corrupted_uid:
                    self.assertEqual(unit.get("source"), "model")


if __name__ == "__main__":
    unittest.main()
