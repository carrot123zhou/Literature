"""实验辅助函数的边界回归：压缩不改候选语义、选择性复核边界、缺失/异常响应重建。"""
from __future__ import annotations

import json
import unittest

from literature.structure import _batch_payload
from literature.structure_reasoning import outline_inputs, select_review_targets
from tests.manual.experiments.structure_context_experiment import (
    compact_outline_request,
    compact_proofread_batch,
    reconstruct_decisions,
    reconstruct_plan,
)


def candidate(bid, *, kind="title", level=2, text="", heading_score=0.0):
    return {"block_id": bid, "text": text or bid, "type": kind, "level": level,
            "page_index": 1, "bbox": [100, 300, 700, 325],
            "local": {"role": "heading" if kind == "title" else "paragraph",
                      "heading_score": heading_score, "toc_score": 0.0, "y_top": 300,
                      "reasons": ["本地判据 A", "本地判据 B"]},
            "layout": {"font_size": 12, "font_name": "Test", "bold": True, "italic": False},
            "previous": {"type": "paragraph", "page_index": 1},
            "next": {"type": "paragraph", "page_index": 1}}


def node(role, parent=None, confidence=0.9):
    return {"role": role, "parent_id": parent, "confidence": confidence, "reason": "r"}


class CompactOutlineTests(unittest.TestCase):
    def test_window1_keeps_full_context(self):
        payload = {"title_candidates": [candidate(f"b{i}") for i in range(70)]}
        request = outline_inputs(payload)[0]
        request = dict(request)
        request["stage"] = "outline-plan-v2"
        request["toc"] = [{"title": "A", "level": 1}, {"title": "B", "level": 1}]
        compacted = compact_outline_request(request)
        self.assertIn("overview", compacted)
        self.assertEqual(compacted["toc"], request["toc"])
        self.assertEqual([c["block_id"] for c in compacted["candidates"]],
                         [c["block_id"] for c in request["candidates"]])

    def test_later_window_drops_overview_and_keeps_nearby_toc(self):
        payload = {"title_candidates": [candidate(f"b{i}") for i in range(130)]}
        request = dict(outline_inputs(payload)[1])
        request["stage"] = "outline-plan-v2"
        request["toc"] = [{"title": f"Chapter {i}", "level": 1} for i in range(40)]
        compacted = compact_outline_request(request)
        self.assertNotIn("overview", compacted)
        self.assertLess(len(compacted["toc"]), len(request["toc"]))
        self.assertEqual([c["block_id"] for c in compacted["candidates"]],
                         [c["block_id"] for c in request["candidates"]])

    def test_previous_outline_and_context_are_trimmed_not_dropped(self):
        request = {"window": 2, "candidates": [], "context": [],
                   "overview": [1], "toc": [], "previous_outline": [{"i": i} for i in range(40)]}
        compacted = compact_outline_request(request)
        self.assertEqual(len(compacted["previous_outline"]), 14)
        self.assertEqual(compacted["previous_outline"][0], {"i": 0})
        self.assertEqual(compacted["previous_outline"][-1], {"i": 39})


class CompactProofreadTests(unittest.TestCase):
    def test_candidate_ids_and_anchors_preserved_reasons_dropped(self):
        candidates = [candidate(f"b{i}") for i in range(45)]
        model_payload = {"document_title": "T", "bbox_space": "mineru-1000",
                         "authoritative_toc": [{"title": "Chapter 0", "level": 1}],
                         "reconciliation": {}, "unresolved_toc": [], "matched_titles": [],
                         "hierarchy_plan": {"profile": {}, "nodes": {}}, "visual_evidence": []}
        batch = _batch_payload(model_payload, candidates[40:45], 0)
        compacted = compact_proofread_batch(batch)
        self.assertEqual([c["block_id"] for c in compacted["title_candidates"]],
                         [c["block_id"] for c in batch["title_candidates"]])
        for item in compacted["title_candidates"]:
            self.assertNotIn("reasons", item["local"])
            self.assertIn("heading_score", item["local"])
            self.assertNotIn("layout", item)
            self.assertIn("bbox", item)
        self.assertEqual(compacted["authoritative_toc"], batch["authoritative_toc"])
        self.assertEqual(compacted["hierarchy_plan"], batch["hierarchy_plan"])


class StrictLocalVariantTests(unittest.TestCase):
    def select(self, candidates, nodes, strict):
        return select_review_targets(candidates, {"nodes": nodes}, {}, strict_local=strict)

    def test_strict_local_default_equals_explicit_true(self):
        # 默认参数与显式 strict_local=True 必须逐位一致（生产默认行为不变的证据）。
        candidates = [candidate("s", kind="paragraph", text="Emergent Trickster"),
                      candidate("t", level=2), candidate("p", kind="paragraph", text="Promoted")]
        nodes = {"s": node("body"), "t": node("h2"), "p": node("h2")}
        default = select_review_targets(candidates, {"nodes": nodes}, {})
        explicit = select_review_targets(candidates, {"nodes": nodes}, {}, strict_local=True)
        self.assertEqual(default, explicit)

    def test_strict_local_reviews_ambiguous_short_body(self):
        candidates = [candidate("s", kind="paragraph", text="Emergent Trickster", heading_score=0.0)]
        reused, review, _reasons, _stats = self.select(candidates, {"s": node("body")}, strict=True)
        self.assertEqual(reused, {})
        self.assertEqual(review, ["s"])

    def test_non_strict_reuses_unchanged_short_body_but_still_reviews_risk(self):
        candidates = [candidate("s", kind="paragraph", text="Emergent Trickster"),
                      candidate("p", kind="paragraph", text="Promoted subsection"),
                      candidate("t", kind="title", level=3)]
        nodes = {"s": node("body"), "p": node("h2"), "t": node("body")}
        reused, review, _reasons, stats = self.select(candidates, nodes, strict=False)
        self.assertEqual(list(reused), ["s"])
        self.assertEqual(set(review), {"p", "t"})  # 漏标提升与标题降级仍复核
        self.assertEqual(stats["promotions"], 1)
        self.assertEqual(stats["demotions"], 1)

    def test_missing_uncertain_conflict_still_reviewed_non_strict(self):
        candidates = [candidate("a", level=2), candidate("b", level=2), candidate("c", level=2)]
        nodes = {"b": node("uncertain"), "c": node("h4")}
        matched = [{"block_id": "c", "score": 0.99, "level": 3, "level_source": "confirmed"}]
        reused, review, _reasons, _stats = select_review_targets(
            candidates, {"nodes": nodes}, {"matched": matched}, strict_local=False)
        self.assertEqual(reused, {})
        self.assertEqual(set(review), {"a", "b", "c"})

    def test_duplicate_titles_are_handled_independently(self):
        candidates = [candidate("dup1", text="Introduction", level=2),
                      candidate("dup2", text="Introduction", level=2)]
        nodes = {"dup1": node("h2"), "dup2": node("h3")}
        reused, review, _reasons, _stats = self.select(candidates, nodes, strict=False)
        self.assertEqual(set(reused), {"dup1"})
        self.assertEqual(review, ["dup2"])

    def test_cross_chapter_parent_conflict_triggers_review(self):
        candidates = [candidate("a", level=1), candidate("b", level=1), candidate("s", level=2)]
        nodes = {"a": node("h1"), "b": node("h1"), "s": node("h2", parent="a")}
        reused, review, _reasons, stats = self.select(candidates, nodes, strict=False)
        self.assertIn("s", review)
        self.assertIn("a", reused)
        self.assertEqual(stats["tree_conflicts"], 1)


class ReconstructBoundaryTests(unittest.TestCase):
    def test_missing_and_abnormal_responses_are_recorded_not_guessed(self):
        requests = [
            {"status": "responded", "response_content": '{"outline": [{"block_id": "a", "role": "h1", "parent_id": null, "confidence": 0.9}]}'},
            {"status": "responded", "response_content": "not json"},
            {"status": "responded", "response_content": '{"outline": [{"block_id": "b", "role": "bogus", "parent_id": null, "confidence": 0.9}, {"block_id": "c", "role": "h2"}]}'},
            {"status": "failed", "response_content": ""},
        ]
        plan = reconstruct_plan(requests)
        self.assertEqual(set(plan["nodes"]), {"a"})
        self.assertEqual(plan["unparsed"], 1)
        self.assertEqual(plan["issues"], 2)

    def test_duplicate_decisions_are_conflict_flagged(self):
        requests = [
            {"status": "responded", "response_content": '{"decisions": [{"block_id": "a", "role": "h2", "parent_id": null, "confidence": 0.9}]}'},
            {"status": "responded", "response_content": '{"decisions": [{"block_id": "a", "role": "h3", "parent_id": null, "confidence": 0.9}]}'},
        ]
        decisions, conflicts, unparsed = reconstruct_decisions(requests)
        self.assertEqual(decisions["a"]["role"], "h3")
        self.assertEqual(conflicts, {"a"})
        self.assertEqual(unparsed, 0)


if __name__ == "__main__":
    unittest.main()
