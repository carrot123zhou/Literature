from __future__ import annotations

import json
import math
import tempfile
import threading
import time
import unittest
from pathlib import Path

from literature.models import Block, Document
from literature.providers import ProviderConfig, ProviderError, ProviderResponse, reasoning_request_fields
from literature.storage import ProjectStore
from literature.structure import (
    STRUCTURE_BATCH_SIZE, _batch_payload, run_structure_job, structure_model_payload,
)
from literature.structure_reasoning import select_review_targets


def candidate(bid, *, kind="title", level=2, text="", heading_score=0.0, toc_score=0.0):
    return {"block_id": bid, "text": text or bid, "type": kind, "level": level,
            "page_index": 1, "bbox": [100, 300, 700, 325],
            "local": {"heading_score": heading_score, "toc_score": toc_score}}


def node(role, parent=None, confidence=0.9):
    return {"role": role, "parent_id": parent, "confidence": confidence, "reason": "r"}


class SelectReviewTargetsTests(unittest.TestCase):
    def select(self, candidates, nodes, matched=None, unlisted=None):
        plan = {"nodes": nodes}
        reconciliation = {"matched": matched or [], "unlisted_titles": unlisted or []}
        return select_review_targets(candidates, plan, reconciliation)

    def test_unchanged_confident_title_is_reused(self):
        reused, review, _reasons, stats = self.select([candidate("a", level=2)], {"a": node("h2")})
        self.assertEqual(list(reused), ["a"])
        self.assertEqual(review, [])
        self.assertEqual(stats["reused"], 1)

    def test_missing_and_uncertain_plan_are_reviewed(self):
        reused, review, reasons, stats = self.select(
            [candidate("a"), candidate("b")], {"b": node("uncertain")})
        self.assertEqual(reused, {})
        self.assertEqual(set(review), {"a", "b"})
        self.assertEqual(stats["missing_plan"], 1)

    def test_promotion_and_demotion_always_reviewed(self):
        reused, review, _reasons, stats = self.select(
            [candidate("p", kind="paragraph", heading_score=0.1),
             candidate("d", kind="title", level=2)],
            {"p": node("h2"), "d": node("body")})
        self.assertEqual(reused, {})
        self.assertEqual(set(review), {"p", "d"})
        self.assertEqual(stats["promotions"], 1)
        self.assertEqual(stats["demotions"], 1)

    def test_non_finite_or_out_of_range_confidence_is_reviewed(self):
        for bad in (float("nan"), float("inf"), 1.5, -0.1, True, "high", None):
            with self.subTest(confidence=bad):
                reused, review, _reasons, _stats = self.select([candidate("a", level=2)], {"a": node("h2", confidence=bad)})
                self.assertEqual(reused, {})
                self.assertEqual(review, ["a"])

    def test_missing_parent_id_is_reviewed(self):
        nodes = {"a": {"role": "h2", "confidence": 0.9, "reason": "r"}}
        reused, review, _reasons, _stats = self.select([candidate("a", level=2)], nodes)
        self.assertEqual(reused, {})
        self.assertEqual(review, ["a"])

    def test_weak_or_local_toc_match_is_not_an_anchor(self):
        # 模糊命中（score<0.92）不能当作已确认目录锚点：改层级仍要复核。
        reused, review, _reasons, stats = self.select(
            [candidate("a", level=2)],
            {"a": node("h3")},
            matched=[{"block_id": "a", "score": 0.7, "level": 3, "level_source": "confirmed"}])
        self.assertEqual(reused, {})
        self.assertEqual(review, ["a"])
        self.assertEqual(stats["conflicts"], 0)

    def test_heuristic_level_source_is_not_an_anchor(self):
        reused, review, _reasons, _stats = self.select(
            [candidate("a", level=2)],
            {"a": node("h3")},
            matched=[{"block_id": "a", "score": 0.99, "level": 3, "level_source": "heuristic"}])
        self.assertEqual(reused, {})
        self.assertEqual(review, ["a"])

    def test_confirmed_strong_anchor_allows_level_change_reuse(self):
        reused, review, _reasons, _stats = self.select(
            [candidate("a", level=2)],
            {"a": node("h3", confidence=0.85)},
            matched=[{"block_id": "a", "score": 0.97, "level": 3, "level_source": "confirmed"}])
        self.assertEqual(list(reused), ["a"])
        self.assertEqual(review, [])

    def test_confirmed_toc_level_conflict_is_reviewed(self):
        reused, review, _reasons, stats = self.select(
            [candidate("a", level=2)],
            {"a": node("h4")},
            matched=[{"block_id": "a", "score": 0.99, "level": 3, "level_source": "confirmed"}])
        self.assertEqual(reused, {})
        self.assertEqual(review, ["a"])
        self.assertEqual(stats["conflicts"], 1)

    def test_tree_conflict_triggers_review_not_late_drop(self):
        # b 的规划父标题为 None，但按最终标题树它应挂在 a 下：冲突必须复核。
        reused, review, _reasons, stats = self.select(
            [candidate("a", level=1), candidate("b", level=2)],
            {"a": node("h1"), "b": node("h2", parent=None)})
        self.assertEqual(list(reused), ["a"])
        self.assertEqual(review, ["b"])
        self.assertEqual(stats["tree_conflicts"], 1)

    def test_child_of_reviewed_parent_is_also_reviewed(self):
        reused, review, reasons, _stats = self.select(
            [candidate("a", level=1), candidate("b", level=2)],
            {"a": node("h1", confidence=0.4), "b": node("h2", parent="a")})
        self.assertEqual(reused, {})
        self.assertEqual(set(review), {"a", "b"})
        self.assertIn("父标题仍需复核", reasons["b"])

    def test_unlisted_title_turned_body_is_reviewed(self):
        reused, review, _reasons, _stats = self.select(
            [candidate("a", kind="title")], {"a": node("body")}, unlisted=[{"block_id": "a"}])
        self.assertEqual(reused, {})
        self.assertEqual(review, ["a"])

    def test_non_heading_reuse_requires_prose_like_text(self):
        # 无编号几何短块（短且无句读）不能只凭 local 低分就复用。
        reused, review, _reasons, _stats = self.select(
            [candidate("short", kind="paragraph", text="Emergent Trickster", heading_score=0.1)],
            {"short": node("body", confidence=0.95)})
        self.assertEqual(reused, {})
        self.assertEqual(review, ["short"])
        # 正常散文正文可以复用。
        prose = candidate("prose", kind="paragraph",
                          text="This is a sufficiently long paragraph of ordinary body prose.", heading_score=0.05)
        reused, review, _reasons, _stats = self.select([prose], {"prose": node("body", confidence=0.95)})
        self.assertEqual(list(reused), ["prose"])
        self.assertEqual(review, [])

    def test_local_heading_signal_forces_review(self):
        reused, review, _reasons, _stats = self.select(
            [candidate("n", kind="paragraph", text="2.1 An unlisted section", heading_score=0.2)],
            {"n": node("body", confidence=0.95)})
        self.assertEqual(reused, {})
        self.assertEqual(review, ["n"])

    def test_portable_large_fixture_reuse_boundary(self):
        # 可移植合成夹具：一批已有标题（规划提升为 h1）与一批散文正文（规划为 body）。
        candidates = []
        nodes = {}
        for index in range(120):
            bid = f"h{index:03d}"
            candidates.append(candidate(bid, kind="title", level=2, text=f"Chapter {index}"))
            nodes[bid] = node("h1")
        for index in range(60):
            bid = f"p{index:03d}"
            candidates.append(candidate(bid, kind="paragraph",
                                        text="This is an ordinary body paragraph with enough prose to read.",
                                        heading_score=0.05))
            nodes[bid] = node("body", confidence=0.95)
        reused, review, _reasons, stats = select_review_targets(candidates, {"nodes": nodes}, {})
        # 层级改变但无目录锚点的 120 个标题必须复核；散文正文可复用。
        self.assertEqual(stats["promotions"], 0)
        self.assertEqual(stats["demotions"], 0)
        self.assertEqual(len(reused), 60)
        self.assertEqual(len(review), 120)


class ReasoningRequestFieldsTests(unittest.TestCase):
    def config(self, **values):
        base = ProviderConfig.preset("custom", base_url="mock://x", model="m")
        for key, value in values.items():
            setattr(base, key, value)
        return base

    def test_default_sends_no_fields(self):
        self.assertEqual(reasoning_request_fields(self.config()), {})

    def test_openai_effort_only_for_valid_values(self):
        self.assertEqual(reasoning_request_fields(self.config(reasoning_style="openai_effort", reasoning_effort="low")),
                         {"reasoning_effort": "low"})
        self.assertEqual(reasoning_request_fields(self.config(reasoning_style="openai_effort", reasoning_effort="")), {})

    def test_qwen_thinking_toggle_and_budget(self):
        value = reasoning_request_fields(self.config(reasoning_style="qwen_thinking", reasoning_effort="off", reasoning_budget=512))
        self.assertEqual(value, {"enable_thinking": False, "thinking_budget": 512})

    def test_glm_thinking_toggle(self):
        self.assertEqual(reasoning_request_fields(self.config(reasoning_style="glm_thinking", reasoning_effort="on")),
                         {"thinking": {"type": "enabled"}})

    def test_malformed_values_are_ignored(self):
        self.assertEqual(reasoning_request_fields(self.config(reasoning_style="openai_effort", reasoning_effort="ultra")), {})
        self.assertEqual(reasoning_request_fields(self.config(reasoning_style="qwen_thinking", reasoning_budget="bad")), {})


def build_store(root: Path, blocks: list[Block]) -> tuple[ProjectStore, str]:
    source = root / "source.html"
    source.write_text("fixture")
    store = ProjectStore(root / "library")
    project = store.create_project(source)["project_id"]
    store.save_document(Document(project_id=project, title="Fixture", source_language="en",
                                 page_count=2, blocks=blocks))
    return store, project


class ConcurrentProofreadTests(unittest.TestCase):
    def make_doc(self):
        # 全部为已有标题但规划提升为 h1：触发第二遍；90 个候选 = 3 批。
        return [Block(block_id=f"b{i:03d}", type="title", order=i, page_index=1,
                      source_text=f"Topic {i} heading", level=2) for i in range(90)]

    def run_proofread(self, provider, *, options=None, blocks=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project = build_store(root, blocks or self.make_doc())
            job = store.create_job(project, "structure_ai", {}, {})
            merged = {"proofread": True, "extract_toc": False, "auto_apply": False,
                      "proofread_concurrency": 3, **(options or {})}
            result = run_structure_job(store, project, job, merged, provider)
            return result, store, project

    def test_concurrent_batches_run_in_parallel_and_keep_order(self):
        class Provider:
            def __init__(self):
                self.lock = threading.Lock()
                self.active = 0
                self.max_active = 0
                self.review_calls = 0
            def complete(self, *, system, user):
                payload = json.loads(user)
                rows = [{"block_id": row["block_id"], "role": "h1", "parent_id": None,
                         "confidence": 0.9, "reason": "root"} for row in payload.get("candidates", payload.get("title_candidates", []))]
                if payload.get("stage"):
                    return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
                with self.lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.review_calls += 1
                time.sleep(0.05)
                with self.lock:
                    self.active -= 1
                return ProviderResponse(content=json.dumps({"decisions": rows}), provider="mock", model="mock")
        provider = Provider()
        result, _store, _project = self.run_proofread(provider)
        self.assertEqual(provider.review_calls, 3)
        self.assertGreaterEqual(provider.max_active, 2)
        self.assertEqual(result["review_selection"]["stats"]["review"], 90)
        self.assertEqual(result["hierarchy_review"]["checked"], 90)
        # 并发完成顺序不影响建议顺序：仍按候选阅读顺序。
        self.assertEqual([item["block_id"] for item in result["suggestions"]], [f"b{i:03d}" for i in range(90)])

    def test_progress_advances_before_pool_exits(self):
        class Provider:
            def complete(self, *, system, user):
                payload = json.loads(user)
                rows = [{"block_id": row["block_id"], "role": "h1", "parent_id": None,
                         "confidence": 0.9, "reason": "root"} for row in payload.get("candidates", payload.get("title_candidates", []))]
                if payload.get("stage"):
                    return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
                time.sleep(0.03)
                return ProviderResponse(content=json.dumps({"decisions": rows}), provider="mock", model="mock")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project = build_store(root, self.make_doc())
            job = store.create_job(project, "structure_ai", {}, {})
            run_structure_job(store, project, job, {"proofread": True, "extract_toc": False,
                                                    "proofread_concurrency": 2}, Provider())
            completed = [row for row in store.get_job(project, job)["activity"]
                         if "标题角色复核完成" in row["text"]]
            self.assertGreaterEqual(len(completed), 2)
            self.assertIn("已处理 90/90", completed[-1]["text"])

    def test_failure_cancels_pending_batches_and_records_completed(self):
        class Provider:
            def __init__(self):
                self.lock = threading.Lock()
                self.review_calls = 0
            def complete(self, *, system, user):
                payload = json.loads(user)
                rows = [{"block_id": row["block_id"], "role": "h1", "parent_id": None,
                         "confidence": 0.9, "reason": "root"} for row in payload.get("candidates", payload.get("title_candidates", []))]
                if payload.get("stage"):
                    return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
                with self.lock:
                    self.review_calls += 1
                    current = self.review_calls
                if current >= 2:
                    raise ProviderError("second batch failed")
                time.sleep(0.02)
                return ProviderResponse(content=json.dumps({"decisions": rows}), provider="mock", model="mock")
        provider = Provider()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project = build_store(root, self.make_doc())
            job = store.create_job(project, "structure_ai", {}, {})
            with self.assertRaises(ProviderError):
                run_structure_job(store, project, job, {"proofread": True, "extract_toc": False,
                                                        "proofread_concurrency": 2}, provider)
            # 第二批失败后不再提交第三批；在途/已完成的请求已持久化。
            self.assertEqual(provider.review_calls, 2)
            job_value = store.get_job(project, job)
            self.assertEqual(job_value["status"], "failed")
            failed = [item for item in job_value["result"]["audit"]["requests"] if item.get("status") == "failed"]
            self.assertEqual(len(failed), 1)

    def test_full_review_fallback_reviews_every_candidate(self):
        class Provider:
            def __init__(self):
                self.review_calls = 0
            def complete(self, *, system, user):
                payload = json.loads(user)
                rows = [{"block_id": row["block_id"], "role": "h2", "parent_id": None,
                         "confidence": 0.95, "reason": "kept"} for row in payload.get("candidates", payload.get("title_candidates", []))]
                if payload.get("stage"):
                    return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
                self.review_calls += 1
                return ProviderResponse(content=json.dumps({"decisions": rows}), provider="mock", model="mock")
        provider = Provider()
        result, _store, _project = self.run_proofread(provider, options={"proofread_full": True, "proofread_concurrency": 2})
        self.assertEqual(provider.review_calls, 3)
        self.assertTrue(result["review_selection"]["full_review"])
        self.assertEqual(result["review_selection"]["stats"]["reused"], 0)
        self.assertEqual(result["review_coverage"]["second_pass"], 90)


class LocalPerformanceSemanticsTests(unittest.TestCase):
    def test_repeated_payload_is_stable_and_length_filter_preserves_candidates(self):
        blocks = [
            Block(block_id="title", type="title", order=0, page_index=1, source_text="Article", level=1,
                  bbox=[100, 300, 700, 325]),
            Block(block_id="short", type="paragraph", order=1, page_index=1, source_text="Missed heading",
                  bbox=[100, 340, 700, 360]),
            Block(block_id="long", type="paragraph", order=2, page_index=1,
                  source_text="Ordinary body text " * 20, bbox=[100, 400, 700, 600]),
        ]
        document = Document(project_id="p", title="T", source_language="en", page_count=2, blocks=blocks)
        toc = [{"title": "Article", "level": 1}]
        first = json.dumps(structure_model_payload(document, toc, excluded_pages=set()), sort_keys=True, ensure_ascii=False)
        second = json.dumps(structure_model_payload(document, toc, excluded_pages=set()), sort_keys=True, ensure_ascii=False)
        self.assertEqual(first, second)
        payload = json.loads(first)
        ids = {item["block_id"] for item in payload["title_candidates"]}
        self.assertIn("short", ids)
        self.assertEqual(len(ids), len(payload["title_candidates"]))

    def test_batch_size_constant_and_batch_payload_isolation(self):
        self.assertEqual(STRUCTURE_BATCH_SIZE, 40)
        candidates = [candidate(f"b{i}") for i in range(45)]
        batch = _batch_payload({"title_candidates": candidates, "hierarchy_plan": {"nodes": {}}}, candidates, 40, 5)
        self.assertEqual([row["block_id"] for row in batch["title_candidates"]], ["b40", "b41", "b42", "b43", "b44"])


class StructureBudgetIntegrationTests(unittest.TestCase):
    def test_budget_blocked_request_is_not_counted_as_unknown_or_pending(self):
        from unittest.mock import Mock, patch

        from literature.providers import OpenAICompatibleProvider
        from literature.request_coordinator import BudgetExhausted, RequestCoordinator

        def handler(url, **kwargs):
            user = kwargs["json"]["messages"][1]["content"]
            payload = json.loads(user)
            rows = [{"block_id": row["block_id"], "role": "h1", "parent_id": None, "confidence": 0.9}
                    for row in payload.get("candidates", payload.get("title_candidates", []))]
            response = Mock()
            response.status_code = 200
            response.headers = {}
            response.raise_for_status = lambda: None
            response.json.return_value = {
                "choices": [{"message": {"content": json.dumps({"outline": rows, "decisions": rows})},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "model": "m",
            }
            return response

        blocks = [Block(block_id=f"b{i:03d}", type="title", order=i, page_index=1,
                        source_text=f"Topic {i} heading", level=2) for i in range(90)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project = build_store(root, blocks)
            job = store.create_job(project, "structure_ai", {}, {})
            coordinator = RequestCoordinator()
            provider = OpenAICompatibleProvider(
                ProviderConfig.preset("custom", base_url="https://relay.example.invalid/v1", model="m",
                                      input_price_per_million=100.0, output_price_per_million=100.0),
                api_key="fixture")
            budget = 0.00061
            coordinator.attempt_hooks_for("svc", provider, project_id=project, budget=budget,
                                          cost_of=lambda chars: 0.0002)
            provider._coordinator = coordinator
            provider._budget_project = project
            with patch("httpx.Client.post", side_effect=handler):
                with self.assertRaises(BudgetExhausted):
                    run_structure_job(store, project, job, {
                        "proofread": True, "extract_toc": False, "proofread_full": True,
                        "proofread_concurrency": 1,
                    }, provider)
            snapshot = coordinator.snapshot(project)
            # 预算拦截发生在发送前：不算未知费用、不留待对账记录、不残留预留。
            self.assertEqual(snapshot["unknown_cost_requests"], 0)
            self.assertEqual(snapshot["reserved_cost"], 0.0)
            self.assertLessEqual(snapshot["spent_cost"], budget + 1e-9)
            self.assertEqual(store.pending_cost_records(project), 0)
            job_value = store.get_job(project, job)
            self.assertEqual(job_value["status"], "failed")
            blocked = [item for item in job_value["result"]["audit"]["requests"]
                       if item.get("status") == "blocked"]
            self.assertTrue(blocked)
            self.assertFalse(blocked[-1]["sent"])


if __name__ == "__main__":
    unittest.main()
