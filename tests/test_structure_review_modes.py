"""复核模式（sample / full / off）回归：第一遍定案 + 有界抽查。

背景（2026-10-14）：实测第二遍全量复核占墙钟 64%，而历史两遍在 role/level/parent 上
分歧为 0。默认改为 sample：第一遍规划直接定案，第二遍只抽查固定比例做一致性指标，
抽查结论绝不写回建议；proofread_full=True 仍是 full 的兼容入口。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literature.models import Block, Document
from literature.providers import ProviderResponse
from literature.storage import ProjectStore
from literature.structure import _review_mode, run_structure_job
from literature.structure_reasoning import (
    DEFAULT_REVIEW_MODE, QA_REASON, REVIEW_MODES, qa_sample_ids, qa_sample_size,
    select_review_targets,
)
from literature.structure_reasoning import compare_qa_sample


def candidate(bid, *, kind="title", level=2, text="", heading_score=0.0, toc_score=0.0):
    return {"block_id": bid, "text": text or bid, "type": kind, "level": level,
            "page_index": 1, "bbox": [100, 300, 700, 325],
            "local": {"heading_score": heading_score, "toc_score": toc_score}}


def node(role, parent=None, confidence=0.9):
    return {"role": role, "parent_id": parent, "confidence": confidence, "reason": "r"}


class ReviewModeResolutionTests(unittest.TestCase):
    def test_default_is_sample(self):
        self.assertEqual(DEFAULT_REVIEW_MODE, "sample")
        self.assertEqual(_review_mode({}), "sample")

    def test_proofread_full_is_the_legacy_full_entry(self):
        self.assertEqual(_review_mode({"proofread_full": True}), "full")
        self.assertEqual(_review_mode({"proofread_full": False}), "sample")

    def test_explicit_mode_wins_over_legacy_flag(self):
        self.assertEqual(_review_mode({"proofread_mode": "off", "proofread_full": True}), "off")
        self.assertEqual(_review_mode({"proofread_mode": "FULL"}), "full")

    def test_unknown_mode_falls_back_to_default(self):
        self.assertEqual(_review_mode({"proofread_mode": "nonsense"}), "sample")
        self.assertEqual(set(REVIEW_MODES), {"full", "sample", "off"})


class QaSampleTests(unittest.TestCase):
    def test_sample_size_is_bounded_to_one_batch(self):
        self.assertEqual(qa_sample_size(0), 0)
        self.assertEqual(qa_sample_size(1), 8)
        self.assertEqual(qa_sample_size(60), 8)
        self.assertEqual(qa_sample_size(348), 18)
        self.assertEqual(qa_sample_size(10_000), 40)

    def test_sample_is_deterministic_and_order_independent(self):
        ids = [f"b{i:03d}" for i in range(200)]
        first = qa_sample_ids(ids, 10)
        self.assertEqual(first, qa_sample_ids(list(reversed(ids)), 10))
        self.assertEqual(first, qa_sample_ids(ids + ids, 10))
        self.assertEqual(len(first), 10)


class SelectReviewTargetsModeTests(unittest.TestCase):
    def select(self, candidates, nodes, mode, **kwargs):
        return select_review_targets(candidates, {"nodes": nodes}, {}, review_mode=mode, **kwargs)

    def test_default_mode_equals_explicit_full(self):
        candidates = [candidate("p", kind="paragraph", text="Promoted"), candidate("t", level=2)]
        nodes = {"p": node("h2"), "t": node("h2")}
        self.assertEqual(select_review_targets(candidates, {"nodes": nodes}, {}),
                         select_review_targets(candidates, {"nodes": nodes}, {}, review_mode="full"))

    def test_new_modes_decide_on_the_first_pass_and_annotate_risk(self):
        # p 是“散文形态的短块被判标题”，隔离 promotion 规则；t 是“现有标题被判非标题”。
        prose = "Promoted subsection heading text goes here, written as a full sentence."
        candidates = [candidate("p", kind="paragraph", text=prose), candidate("t", level=2)]
        nodes = {"p": node("h2"), "t": node("body")}
        reused, review, reasons, stats = self.select(candidates, nodes, "off")
        # 安全规则命中不再阻止复用：决定照样产出，只带待确认标注。
        self.assertEqual(set(reused), {"p", "t"})
        self.assertEqual(reasons, {})
        self.assertEqual(stats["promotions"], 1)
        self.assertEqual(stats["demotions"], 1)
        self.assertEqual(stats["annotated"], 2)
        self.assertEqual(reused["p"]["review_flags"], ["疑似漏标标题，需人工确认"])
        self.assertEqual(reused["t"]["review_flags"], ["现有标题被判为非标题，需人工确认"])

    def test_short_non_prose_block_is_still_annotated_in_new_modes(self):
        # 无编号的几何短块即使规划判为正文也要标出来（旧行为是进第二遍）。
        candidates = [candidate("s", kind="paragraph", text="Emergent Trickster")]
        reused, review, reasons, stats = self.select(candidates, {"s": node("body")}, "off")
        self.assertEqual(set(reused), {"s"})
        self.assertEqual(review, [])
        self.assertEqual(stats["local_heading"], 1)
        self.assertIn("本地证据仍支持标题", reused["s"]["review_flags"][0])

    def test_hard_blockers_still_require_the_second_pass(self):
        candidates = [candidate("a"), candidate("b"), candidate("c")]
        nodes = {"b": node("uncertain")}  # a 缺规划，b 角色不确定，c 非法置信度
        nodes["c"] = {"role": "h2", "parent_id": None, "confidence": 1.5}
        reused, review, reasons, stats = self.select(candidates, nodes, "off")
        self.assertEqual(reused, {})
        self.assertEqual(set(review), {"a", "b", "c"})
        self.assertEqual(stats["missing_plan"], 1)
        self.assertEqual(stats["invalid_plan"], 2)

    def test_sample_mode_marks_qa_ids_and_keeps_review_bounded(self):
        candidates = [candidate(f"t{i}", level=2) for i in range(60)]
        nodes = {f"t{i}": node("h2") for i in range(60)}
        reused, review, reasons, stats = self.select(candidates, nodes, "sample")
        self.assertEqual(len(reused), 60)
        self.assertEqual(stats["reused"], 60)
        self.assertEqual(stats["annotated"], 0)  # 无安全规则命中
        self.assertEqual(len(review), 8)         # 一把抽查批
        self.assertTrue(all(reasons[bid] == QA_REASON for bid in review))
        self.assertEqual(stats["qa"], 8)
        # 抽查项本身就是已定案的候选（reused 的子集），所以两者有意重叠。
        self.assertTrue(set(review) <= set(reused))

    def test_off_mode_emits_no_second_pass_at_all(self):
        candidates = [candidate(f"t{i}", level=2) for i in range(60)]
        nodes = {f"t{i}": node("h2") for i in range(60)}
        reused, review, reasons, stats = self.select(candidates, nodes, "off")
        self.assertEqual(len(reused), 60)
        self.assertEqual(review, [])
        self.assertEqual(stats["qa"], 0)

    def test_qa_sample_never_overlaps_hard_blockers(self):
        candidates = [candidate(f"t{i}", level=2) for i in range(30)]
        nodes = {f"t{i}": node("h2") for i in range(30)}
        nodes["t5"] = node("uncertain")
        reused, review, reasons, stats = self.select(candidates, nodes, "sample")
        self.assertEqual(len(review), len(set(review)))
        self.assertIn("t5", review)
        self.assertIn(QA_REASON, reasons.values())
        self.assertNotEqual(reasons["t5"], QA_REASON)


class CompareQaSampleTests(unittest.TestCase):
    def test_reports_agreement_without_mutating_inputs(self):
        stage1 = {"a": {"role": "h2", "parent_id": None}, "b": {"role": "h2", "parent_id": "a"}}
        sample = {"a": {"role": "h2", "parent_id": None}, "b": {"role": "h1", "parent_id": None}}
        report = compare_qa_sample(stage1, sample, [candidate("a"), candidate("b", text="B")])
        self.assertEqual(report["checked"], 2)
        self.assertEqual(report["agree"], 1)
        self.assertEqual(report["disagree"], 1)
        self.assertEqual(report["agreement"], 0.5)
        self.assertEqual([row["block_id"] for row in report["disagreements"]], ["b"])
        self.assertEqual(stage1["b"]["role"], "h2")  # 抽查不盖回决定

    def test_items_without_a_first_pass_answer_are_not_counted(self):
        stage1 = {"a": {"role": "h2", "parent_id": None}}
        sample = {"a": {"role": "h2", "parent_id": None}, "z": {"role": "h1", "parent_id": None}}
        report = compare_qa_sample(stage1, sample, [candidate("a"), candidate("z")])
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["agree"], 1)


class QaBatchDoesNotProduceSuggestionsTests(unittest.TestCase):
    def build(self, root: Path, count: int) -> tuple[ProjectStore, str]:
        source = root / "source.html"
        source.write_text("fixture")
        store = ProjectStore(root / "library")
        project = store.create_project(source)["project_id"]
        store.save_document(Document(project_id=project, title="F", source_language="en", page_count=2,
                                     blocks=[Block(block_id=f"t{i:03d}", type="title", order=i,
                                                   page_index=1, source_text=f"Topic {i} heading", level=2)
                                             for i in range(count)]))
        return store, project

    def test_contradicting_sample_decisions_do_not_change_suggestions(self):
        class Provider:
            def __init__(self):
                self.qa_calls = 0
            def complete(self, *, system, user):
                payload = json.loads(user)
                if payload.get("review_purpose") == "qa":
                    self.qa_calls += 1
                    rows = [{"block_id": row["block_id"], "role": "h1", "parent_id": None,
                             "confidence": 0.99} for row in payload["title_candidates"]]
                    return ProviderResponse(content=json.dumps({"decisions": rows}), provider="mock", model="mock")
                rows = [{"block_id": row["block_id"], "role": "h2", "parent_id": None,
                         "confidence": 0.95, "reason": "kept"} for row in payload["candidates"]]
                return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project = self.build(root, 20)
            provider = Provider()
            job = store.create_job(project, "structure_ai", {}, {})
            result = run_structure_job(store, project, job,
                                       {"proofread": True, "extract_toc": False}, provider)
            self.assertEqual(provider.qa_calls, 1)
            self.assertEqual(result["review_coverage"]["second_pass"], 0)
            qa = result["review_selection"]["qa"]
            self.assertEqual(qa["checked"], 8)
            self.assertEqual(qa["disagree"], 8)          # 抽查故意全部反着答
            self.assertEqual(result["suggestions"], [])  # 但一条建议都没多
            self.assertEqual(store.list_patches(project), [])

    def test_off_mode_sends_no_second_pass_request(self):
        class Provider:
            def __init__(self):
                self.review_calls = 0
            def complete(self, *, system, user):
                payload = json.loads(user)
                if "title_candidates" in payload:
                    self.review_calls += 1
                rows = [{"block_id": row["block_id"], "role": "h2", "parent_id": None,
                         "confidence": 0.95, "reason": "kept"} for row in payload["candidates"]]
                return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project = self.build(root, 20)
            provider = Provider()
            job = store.create_job(project, "structure_ai", {}, {})
            result = run_structure_job(store, project, job,
                                       {"proofread": True, "extract_toc": False,
                                        "proofread_mode": "off"}, provider)
            self.assertEqual(provider.review_calls, 0)
            self.assertIsNone(result["review_selection"]["qa"])
            self.assertEqual(result["review_coverage"]["reviewed"], 20)


if __name__ == "__main__":
    unittest.main()
