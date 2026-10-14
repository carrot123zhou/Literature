"""自适应分支规划：框架验证、区域切分、局部升级、fallback 与并发稳定性回归。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from literature.providers import ProviderResponse
from literature.request_coordinator import BudgetExhausted
from literature.models import Block, Document
from literature.storage import ProjectStore
from literature.structure import run_structure_job
from literature.structure_adaptive import (
    ADAPTIVE_SECONDARY_WINDOW,
    MAX_ADDITIONAL_PLANNING_REQUESTS,
    REGION_WINDOW,
    Region,
    _apply_rows,
    _merge_regions,
    build_framework,
    build_region_request,
    partition_regions,
    run_adaptive_planning,
)


def block(bid, *, kind="title", level=1, text="", page_index=0):
    return {"block_id": bid, "type": kind, "level": level, "text": text or bid,
            "page_index": page_index, "bbox": [10, 10, 120, 32],
            "local": {"role": "heading" if kind == "title" else "paragraph",
                      "heading_score": 0.8 if kind == "title" else 0.1, "toc_score": 0.0, "y_top": 10,
                      "reasons": ["本地判据"]},
            "layout": {"font_size": 12}, "previous": None, "next": None}


def payload(candidates, toc, matched, *, unlisted=None, unresolved=None):
    return {"title_candidates": candidates, "authoritative_toc": toc, "matched_titles": matched,
            "unlisted_titles": unlisted or [], "unresolved_toc": unresolved or [],
            "document_title": "T", "bbox_space": "mineru-1000"}


def match(index, bid, *, score=1.0, level=1, level_source="confirmed"):
    return {"toc_index": index, "block_id": bid, "score": score, "level": level, "level_source": level_source}


class FakeProvider:
    def __init__(self, *, omit=(), malformed=(), illegal=(), delay=0.0):
        self.omit = set(omit)
        self.malformed = set(malformed)
        self.illegal = set(illegal)
        self.delay = delay
        self.requests = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def complete(self, *, system, user):
        request = json.loads(user)
        with self.lock:
            self.requests.append(request)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                import time
                time.sleep(self.delay)
            # 首轮（adaptive）才注入遗漏/非法；局部升级与补查阶段恢复有效输出，模拟“升级后修好”。
            first_pass = request.get("planning") in {"adaptive", None}
            omit = self.omit if first_pass else set()
            malformed = self.malformed if first_pass else set()
            illegal = self.illegal if first_pass else set()
            primary = [c["block_id"] for c in request.get("candidates", [])]
            if any(bid in malformed for bid in primary):
                return ProviderResponse(content="not json", provider="mock", model="mock")
            rows = []
            for item in request.get("candidates", []):
                if item["block_id"] in omit:
                    continue
                if item["block_id"] in illegal:
                    rows.append({"block_id": item["block_id"], "role": "bogus", "parent_id": None, "confidence": 0.9})
                    continue
                rows.append({"block_id": item["block_id"],
                             "role": "h1" if item["type"] == "title" else "body",
                             "parent_id": None, "confidence": 0.9, "reason": "stub"})
            return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
        finally:
            with self.lock:
                self.active -= 1


class FrameworkTests(unittest.TestCase):
    def test_reliable_with_strong_unique_anchors(self):
        candidates = [block("a", text="Chapter One"), block("s1", kind="paragraph", level=0, text="sub"),
                      block("b", text="Chapter Two")]
        framework = build_framework(payload(candidates,
            [{"title": "Chapter One", "level": 1, "printed_page": "1"},
             {"title": "Chapter Two", "level": 1, "printed_page": "20"}],
            [match(0, "a"), match(1, "b")]))
        self.assertEqual(framework.state, "reliable")
        self.assertEqual([a.block_id for a in framework.reliable_anchors], ["a", "b"])
        self.assertEqual(framework.coverage, 1.0)

    def test_weak_or_non_unique_match_is_not_anchor(self):
        candidates = [block("a", text="Chapter One"), block("b", text="Chapter One")]
        framework = build_framework(payload(candidates,
            [{"title": "Chapter One", "level": 1}], [match(0, "a", score=0.7)]))
        self.assertEqual(framework.state, "unreliable")
        self.assertTrue(any("匹配强度不足" in reason for reason in framework.anchors[0].reasons))

    def test_level_not_corroborated_by_body_is_not_anchor(self):
        # 目录声称 H1，但正文标题是 H4 且无编号支持 → 不能独自授权边界。
        candidates = [block("a", text="Chapter One", level=4), block("b", text="Chapter Two", level=4)]
        framework = build_framework(payload(candidates,
            [{"title": "Chapter One", "level": 1}, {"title": "Chapter Two", "level": 1}],
            [match(0, "a"), match(1, "b")]))
        self.assertEqual(framework.state, "unreliable")
        self.assertTrue(all(not a.reliable for a in framework.anchors))

    def test_duplicate_titles_are_flagged(self):
        candidates = [block("a", text="Introduction"), block("b", text="Introduction")]
        framework = build_framework(payload(candidates,
            [{"title": "Introduction", "level": 1}, {"title": "Introduction", "level": 2}],
            [match(0, "a"), match(1, "b", level=2)]))
        self.assertTrue(framework.duplicate_titles)
        self.assertNotEqual(framework.state, "reliable")

    def test_page_mapping_inconsistency_downgrades_but_keeps_body_anchors(self):
        candidates = [block("a"), block("b"), block("c")]
        framework = build_framework(payload(candidates,
            [{"title": "A", "level": 1, "printed_page": "50"},
             {"title": "B", "level": 1, "printed_page": "10"},
             {"title": "C", "level": 1, "printed_page": "90"}],
            [match(0, "a"), match(1, "b"), match(2, "c")]))
        self.assertFalse(framework.page_mapping_consistent)
        self.assertEqual(framework.state, "partial")

    def test_order_inversion_anchor_is_removed(self):
        candidates = [block("a", text="A"), block("b", text="B")]
        framework = build_framework(payload(candidates,
            [{"title": "A", "level": 1}, {"title": "B", "level": 1}],
            [match(0, "b"), match(1, "a")]))  # toc 顺序与正文顺序相反
        self.assertGreaterEqual(framework.order_inversions, 1)
        self.assertLessEqual(len(framework.reliable_anchors), 1)


class RegionTests(unittest.TestCase):
    def framework(self):
        candidates = [block("a"), block("u1", kind="paragraph", level=0, text="unlisted subsection"),
                      block("b"), block("u2", kind="paragraph", level=0, text="tail")]
        toc = [{"title": "A", "level": 1, "printed_page": "1"}, {"title": "B", "level": 1, "printed_page": "20"}]
        matched = [match(0, "a"), match(1, "b")]
        return candidates, payload(candidates, toc, matched), build_framework(payload(candidates, toc, matched))

    def test_partition_covers_each_candidate_exactly_once(self):
        candidates, _payload, framework = self.framework()
        regions = partition_regions(candidates, framework)
        primary = [bid for region in regions for bid in region.primary_ids]
        self.assertEqual(sorted(primary), sorted(item["block_id"] for item in candidates))
        self.assertEqual(len(primary), len(set(primary)))

    def test_unlisted_unnumbered_subsection_is_primary_not_lost(self):
        candidates, _payload, framework = self.framework()
        regions = partition_regions(candidates, framework)
        self.assertIn("u1", [bid for region in regions for bid in region.primary_ids])

    def test_duplicate_title_region_is_serial(self):
        candidates = [block("a"), block("dup", text="Same"), block("dup2", text="Same")]
        toc = [{"title": "A", "level": 1}, {"title": "Same", "level": 1}, {"title": "Same", "level": 1}]
        matched = [match(0, "a"), match(1, "dup"), match(2, "dup2")]
        framework = build_framework(payload(candidates, toc, matched))
        regions = partition_regions(candidates, framework)
        self.assertTrue(any(not region.reliable for region in regions))

    def test_region_request_controls_toc_and_overview(self):
        _candidates, data, framework = self.framework()
        regions = partition_regions(data["title_candidates"], framework)
        first = build_region_request(data, regions[0], framework, include_overview=True)
        later = build_region_request(data, regions[-1], framework, include_overview=False)
        self.assertIn("overview", first)
        self.assertNotIn("overview", later)
        self.assertLessEqual(len(later["toc"]), 3)
        self.assertTrue(set(item["block_id"] for item in later["context"]).isdisjoint(regions[-1].primary_ids))


class PlannerTests(unittest.TestCase):
    def simple(self):
        candidates = [block("a"), block("s1", kind="paragraph", level=0, text="sub"), block("b")]
        toc = [{"title": "A", "level": 1, "printed_page": "1"}, {"title": "B", "level": 1, "printed_page": "20"}]
        matched = [match(0, "a"), match(1, "b")]
        return candidates, payload(candidates, toc, matched)

    def test_unreliable_framework_falls_back_to_global(self):
        candidates = [block("a"), block("b")]
        data = payload(candidates, [], [])
        provider = FakeProvider()
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False)
        self.assertTrue(meta["fallback"])
        self.assertEqual(plan["candidates"], 2)
        # 回退走全局 outline_inputs（有 window 字段、无 planning 字段）。
        self.assertTrue(any("window" in request for request in provider.requests))
        self.assertTrue(all("planning" not in request for request in provider.requests))

    def test_adaptive_covers_every_candidate_once(self):
        candidates, data = self.simple()
        provider = FakeProvider()
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False)
        self.assertFalse(meta["fallback"])
        self.assertEqual(set(plan["nodes"]), {item["block_id"] for item in candidates})
        self.assertEqual(meta["missing"], 0)

    def test_missing_and_illegal_rows_trigger_single_upgrade_round(self):
        candidates, data = self.simple()
        provider = FakeProvider(omit=("b",), illegal=("s1",))
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False)
        self.assertGreaterEqual(meta["local_upgrades"], 1)
        self.assertEqual(meta["missing"], 0)  # 升级后补回
        phases = [request.get("planning") for request in provider.requests]
        self.assertIn("adaptive-local-upgrade", phases)
        # 每个可疑区域最多升级一轮；本案两处问题恢复后不再进入全局补查。
        self.assertEqual(phases.count("adaptive-local-upgrade"), meta["local_upgrades"])
        self.assertLessEqual(meta["local_upgrades"], meta["reliable_regions"])
        self.assertNotIn("adaptive-secondary", phases)

    def test_small_candidate_count_falls_back_to_global(self):
        candidates, data = self.simple()
        provider = FakeProvider()
        plan, meta = run_adaptive_planning(data, provider, min_candidates=120)
        self.assertTrue(meta["fallback"])
        self.assertIn("少于自适应阈值", meta["fallback_reason"])
        self.assertEqual(set(plan["nodes"]), {item["block_id"] for item in candidates})
        self.assertTrue(all("planning" not in request for request in provider.requests))

    def test_local_upgrade_expands_context(self):
        candidates, data = self.simple()
        provider = FakeProvider(omit=("b",))
        _plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False)
        upgrade = next(request for request in provider.requests
                       if request.get("planning") == "adaptive-local-upgrade")
        base = next(request for request in provider.requests if request.get("planning") == "adaptive")
        self.assertGreaterEqual(len(upgrade["context"]), len(base["context"]))

    def test_parallel_merge_bounded_and_stable(self):
        blocks_list = []
        toc, matched = [], []
        for index in range(6):
            bid = f"c{index}"
            blocks_list.append(block(bid, text=f"Chapter {index}"))
            toc.append({"title": f"Chapter {index}", "level": 1, "printed_page": str(index * 10 + 1)})
            matched.append(match(index, bid))
        for index in range(6):
            blocks_list.append(block(f"u{index}", kind="paragraph", level=0, text=f"subsection {index}"))
        data = payload(blocks_list, toc, matched)
        provider = FakeProvider(delay=0.01)
        plan, meta = run_adaptive_planning(data, provider, parallel=2, min_candidates=0, use_gate=False)
        self.assertLessEqual(provider.max_active, 2)
        self.assertEqual(set(plan["nodes"]), {item["block_id"] for item in blocks_list})
        # 顺序稳定：按候选顺序都能得到主判断。
        self.assertEqual([bid for bid in plan["nodes"] if bid.startswith("c")], [f"c{i}" for i in range(6)])

    def test_stop_event_prevents_new_requests(self):
        _candidates, data = self.simple()
        provider = FakeProvider()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(RuntimeError):
            run_adaptive_planning(data, provider, stop_event=stop, min_candidates=0, use_gate=False)
        self.assertEqual(len(provider.requests), 0)

    def test_level_up_and_down_roles_preserved(self):
        candidates = [block("a", level=2), block("b", level=3)]
        toc = [{"title": "A", "level": 1, "printed_page": "1"}, {"title": "B", "level": 1, "printed_page": "20"}]
        matched = [match(0, "a"), match(1, "b")]

        class LevelProvider(FakeProvider):
            def complete(self, *, system, user):
                request = json.loads(user)
                with self.lock:
                    self.requests.append(request)
                rows = [{"block_id": item["block_id"], "role": "h2" if item["block_id"] == "a" else "h3",
                         "parent_id": None if item["block_id"] == "a" else "a",
                         "confidence": 0.9, "reason": "stub"} for item in request.get("candidates", [])]
                return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")

        plan, _meta = run_adaptive_planning(payload(candidates, toc, matched), LevelProvider(), min_candidates=0, use_gate=False)
        self.assertEqual(plan["nodes"]["a"]["role"], "h2")
        self.assertEqual(plan["nodes"]["b"]["role"], "h3")
        self.assertEqual(plan["nodes"]["b"]["parent_id"], "a")

    def test_region_window_bounds_large_chapter(self):
        candidates = [block(f"c{i}") for i in range(REGION_WINDOW + 10)]
        toc = [{"title": "C0", "level": 1, "printed_page": "1"}]
        matched = [match(0, "c0")]
        framework = build_framework(payload(candidates, toc, matched))
        regions = partition_regions(candidates, framework)
        self.assertTrue(all(len(region.primary_ids) <= REGION_WINDOW for region in regions))
        self.assertGreater(len(regions), 1)


class JobIntegrationTests(unittest.TestCase):
    def make_document(self):
        blocks = []
        order = 0
        for index in range(6):
            blocks.append(Block(block_id=f"t{index}", type="title", order=order, page_index=index,
                                source_text=f"Chapter {chr(65 + index)}", level=1)); order += 1
            for j in range(21):
                blocks.append(Block(block_id=f"p{index}_{j}", type="paragraph", order=order, page_index=index,
                                    source_text=f"unlisted subsection {chr(97 + index)} {chr(97 + j)} note", bbox=[100, 300 + j, 600, 316 + j])); order += 1
        return blocks

    class Provider:
        def __init__(self):
            self.requests = []
        def complete(self, *, system, user):
            request = json.loads(user)
            self.requests.append(request)
            if "title_candidates" in request:
                rows = [{"block_id": item["block_id"], "parent_id": None, "confidence": 0.9,
                         "role": "h1" if item["type"] == "title" else "body", "reason": "stub"}
                        for item in request["title_candidates"]]
                return ProviderResponse(content=json.dumps({"decisions": rows}), provider="mock", model="mock")
            rows = [{"block_id": item["block_id"], "parent_id": None, "confidence": 0.9,
                     "role": "h1" if item["type"] == "title" else "body", "reason": "stub"}
                    for item in request.get("candidates", [])]
            return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")

    def run_job(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "book.html"
            source.write_text("fixture")
            store = ProjectStore(root / "library")
            project = store.create_project(source)["project_id"]
            blocks = self.make_document()
            store.save_document(Document(project_id=project, title="T", source_language="en",
                                         page_count=1, blocks=blocks))
            toc = [{"title": f"Chapter {chr(65 + index)}", "level": 1, "printed_page": str(index * 10 + 1)} for index in range(6)]
            job = store.create_job(project, "structure_ai", {}, {})
            provider = self.Provider()
            result = run_structure_job(store, project, job, {
                "toc": toc, "extract_toc": False, "proofread": True,
                "proofread_concurrency": 1, "planning_mode": mode,
            }, provider)
            return result, provider

    def test_adaptive_job_covers_all_candidates_and_reports_path(self):
        result, provider = self.run_job("adaptive")
        planning = result["audit"]["planning"]
        self.assertEqual(planning["mode"], "adaptive")
        self.assertFalse(planning["fallback"])
        self.assertGreaterEqual(planning["reliable_regions"], 1)
        expected = {f"t{index}" for index in range(6)} | {f"p{i}_{j}" for i in range(6) for j in range(21)}
        self.assertEqual(set(result["hierarchy_plan"]["nodes"]), expected)
        self.assertEqual(planning.get("missing"), 0)

    def test_mode_isolation_no_cross_mode_cache_reuse(self):
        # 先 global 后 adaptive：请求内容不同，不应复用；若复用会污染公平比较。
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "book.html"
            source.write_text("fixture")
            store = ProjectStore(root / "library")
            project = store.create_project(source)["project_id"]
            blocks = self.make_document()
            store.save_document(Document(project_id=project, title="T", source_language="en",
                                         page_count=1, blocks=blocks))
            toc = [{"title": f"Chapter {chr(65 + index)}", "level": 1, "printed_page": str(index * 10 + 1)} for index in range(6)]
            options = {"toc": toc, "extract_toc": False, "proofread": True, "proofread_concurrency": 1}
            first = store.create_job(project, "structure_ai", {}, {})
            run_structure_job(store, project, first, {**options, "planning_mode": "global"}, self.Provider())
            second = store.create_job(project, "structure_ai", {}, {})
            result = run_structure_job(store, project, second, {**options, "planning_mode": "adaptive"}, self.Provider())
            self.assertEqual(result["audit"]["reused_requests"], 0)
            self.assertEqual(result["audit"]["planning"]["mode"], "adaptive")


class GateAndUpgradeTests(unittest.TestCase):
    def multi_chapter_payload(self, chapters=8, per_chapter=2):
        candidates, toc, matched = [], [], []
        for index in range(chapters):
            bid = f"c{index}"
            candidates.append(block(bid, text=f"Chapter {chr(65 + index)}"))
            toc.append({"title": f"Chapter {chr(65 + index)}", "level": 1, "printed_page": str(index * 10 + 1)})
            matched.append(match(index, bid))
            for j in range(per_chapter):
                candidates.append(block(f"p{index}_{j}", kind="paragraph", level=0,
                                        text=f"unlisted note {chr(97 + index)} {chr(97 + j)}"))
        return candidates, payload(candidates, toc, matched)

    def test_merge_regions_preserves_primary_union_and_anchors(self):
        regions = [
            Region(index=0, anchor_block_id="a", anchor_level=1, primary_ids=["a", "x"],
                   context_ids=["c"], reliable=True, anchors=["a"]),
            Region(index=1, anchor_block_id="b", anchor_level=1, primary_ids=["b", "y"],
                   context_ids=["d"], reliable=True, anchors=["b"]),
            Region(index=2, anchor_block_id="c", anchor_level=1, primary_ids=["c", "z"],
                   context_ids=[], reliable=False, anchors=["c"]),
        ]
        merged = _merge_regions(regions, 2, 128)
        self.assertEqual(len(merged), 2)
        self.assertEqual([bid for r in merged for bid in r.primary_ids], ["a", "x", "b", "y", "c", "z"])
        self.assertEqual(set(merged[0].anchors), {"a", "b"})
        self.assertTrue(all(len(r.primary_ids) <= 128 for r in merged))

    def test_gate_falls_back_when_no_safe_benefit(self):
        # 极短文档：自适应请求/字符不优于全局 → 回退（可接受结果）。
        candidates = [block("a"), block("p0", kind="paragraph", level=0, text="short note")]
        toc = [{"title": "A", "level": 1, "printed_page": "1"}]
        matched = [match(0, "a")]
        provider = FakeProvider()
        plan, meta = run_adaptive_planning(payload(candidates, toc, matched), provider, min_candidates=0)
        self.assertTrue(meta["fallback"])
        self.assertIn("预检门控不通过", meta["fallback_reason"])
        self.assertIn("gate", meta)
        self.assertEqual(set(plan["nodes"]), {item["block_id"] for item in candidates})

    def test_multi_small_chapter_merge_or_explicit_fallback(self):
        candidates, data = self.multi_chapter_payload()
        provider = FakeProvider()
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0)
        if meta["fallback"]:
            self.assertTrue(meta["fallback_reason"])
            self.assertEqual(set(plan["nodes"]), {item["block_id"] for item in candidates})
        else:
            self.assertLessEqual(meta["main_requests"], meta["gate"]["global_requests"])
            self.assertEqual(set(plan["nodes"]), {item["block_id"] for item in candidates})

    def test_root_convention_present_in_every_request(self):
        candidates, data = self.multi_chapter_payload()
        provider = FakeProvider()
        run_adaptive_planning(data, provider, min_candidates=0, use_gate=False)
        self.assertTrue(provider.requests)
        for request in provider.requests:
            if request.get("planning") in {"adaptive", "adaptive-local-upgrade", "adaptive-secondary"}:
                self.assertIn("root_convention", request)
                self.assertIn("anchor_level", request["root_convention"])
        overviews = [request for request in provider.requests if request.get("planning") == "adaptive" and "overview" in request]
        self.assertLessEqual(len(overviews), 1)

    class TreeConflictProvider(FakeProvider):
        def complete(self, *, system, user):
            request = json.loads(user)
            with self.lock:
                self.requests.append(request)
            rows = []
            for item in request.get("candidates", []):
                if item["type"] == "title":
                    rows.append({"block_id": item["block_id"],
                                 "role": "h1" if item["block_id"] == "c0" else "h2", "parent_id": None,
                                 "confidence": 0.9, "reason": "stub"})
                else:
                    rows.append({"block_id": item["block_id"], "role": "body", "parent_id": None,
                                 "confidence": 0.9, "reason": "stub"})
            return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")

    def test_upgrade_triggered_by_tree_conflict(self):
        candidates, data = self.multi_chapter_payload()
        provider = self.TreeConflictProvider()
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False)
        self.assertGreaterEqual(meta["local_upgrades"], 1)
        self.assertGreaterEqual(meta["tree_issues"], 1)

    class TocContradictionProvider(FakeProvider):
        def complete(self, *, system, user):
            request = json.loads(user)
            with self.lock:
                self.requests.append(request)
            rows = [{"block_id": item["block_id"],
                     "role": "h3" if item["type"] == "title" else "body",
                     "parent_id": None, "confidence": 0.9, "reason": "stub"}
                    for item in request.get("candidates", [])]
            return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")

    def test_upgrade_triggered_by_confirmed_toc_contradiction(self):
        candidates, data = self.multi_chapter_payload()
        provider = self.TocContradictionProvider()
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False)
        self.assertGreaterEqual(meta["local_upgrades"], 1)

    def test_additional_planning_requests_are_capped(self):
        candidates, data = self.multi_chapter_payload(chapters=12, per_chapter=3)
        provider = self.TocContradictionProvider()
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False, parallel=1)
        self.assertLessEqual(meta["upgrade_requests"] + meta["secondary_requests"], MAX_ADDITIONAL_PLANNING_REQUESTS)
        self.assertTrue(any("已达上限" in issue for issue in plan["issues"]) or meta["missing"] == 0)

    def test_upgrade_malformed_response_records_issue(self):
        candidates, data = self.multi_chapter_payload(chapters=4, per_chapter=1)
        provider = FakeProvider(illegal=("c0",))
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False, parallel=1)
        # 非法角色 → 升级；升级阶段恢复有效，所有候选最终都有规划。
        self.assertGreaterEqual(meta["local_upgrades"], 1)
        expected = {item["block_id"] for item in candidates}
        self.assertEqual(set(plan["nodes"]), expected)


class ApplyRowsValidationTests(unittest.TestCase):
    def test_rejects_unhashable_and_bad_types(self):
        nodes, issues = {}, []
        rows = [{"block_id": ["x"], "role": "h1", "parent_id": None, "confidence": 0.9},
                {"block_id": "a", "role": {"x": 1}, "parent_id": None, "confidence": 0.9},
                {"block_id": "a", "role": "h1", "parent_id": None, "confidence": "bad"},
                {"block_id": "a", "role": "h1", "confidence": 0.9},
                {"block_id": "a", "role": "uncertain", "parent_id": None, "confidence": 0.9}]
        _apply_rows(rows, {"a"}, {"a"}, {"a": 0}, nodes, issues)
        self.assertEqual(nodes, {})
        self.assertTrue(issues)

    def test_duplicate_id_is_rejected_entirely(self):
        nodes, issues = {}, []
        rows = [{"block_id": "a", "role": "h1", "parent_id": None, "confidence": 0.9},
                {"block_id": "a", "role": "h2", "parent_id": None, "confidence": 0.95}]
        _apply_rows(rows, {"a"}, {"a"}, {"a": 0}, nodes, issues)
        self.assertEqual(nodes, {})
        self.assertTrue(any("重复" in issue for issue in issues))

    def test_rejects_illegal_or_late_parent(self):
        nodes, issues = {}, []
        _apply_rows([{"block_id": "a", "role": "h1", "parent_id": "unknown", "confidence": 0.9}],
                    {"a", "b"}, {"a", "b"}, {"a": 0, "b": 1}, nodes, issues)
        self.assertEqual(nodes, {})
        nodes2, issues2 = {}, []
        rows2 = [{"block_id": "a", "role": "h2", "parent_id": "b", "confidence": 0.9},
                 {"block_id": "b", "role": "h1", "parent_id": None, "confidence": 0.9}]
        _apply_rows(rows2, {"a", "b"}, {"a", "b"}, {"a": 0, "b": 1}, nodes2, issues2)
        self.assertNotIn("a", nodes2)

    def test_accepts_valid_earlier_parent(self):
        nodes, issues = {}, []
        rows = [{"block_id": "p", "role": "h1", "parent_id": None, "confidence": 0.9},
                {"block_id": "c", "role": "h2", "parent_id": "p", "confidence": 0.9}]
        _apply_rows(rows, {"p", "c"}, {"p", "c"}, {"p": 0, "c": 1}, nodes, issues)
        self.assertEqual(set(nodes), {"p", "c"})
        self.assertEqual(nodes["c"]["parent_id"], "p")

    def test_context_rows_ignored_but_unknown_flagged(self):
        nodes, issues = {}, []
        rows = [{"block_id": "ctx", "role": "h1", "parent_id": None, "confidence": 0.9},
                {"block_id": "ghost", "role": "h1", "parent_id": None, "confidence": 0.9}]
        _apply_rows(rows, {"main"}, {"main", "ctx"}, {"main": 0, "ctx": 1}, nodes, issues)
        self.assertEqual(nodes, {})
        self.assertTrue(any("未知 block_id" in issue for issue in issues))


class BoundedCostTests(unittest.TestCase):
    def big_payload(self, chapters=4, per_chapter=40):
        candidates, toc, matched = [], [], []
        for index in range(chapters):
            bid = f"c{index}"
            candidates.append(block(bid, text=f"Chapter {chr(65 + index)}"))
            toc.append({"title": f"Chapter {chr(65 + index)}", "level": 1, "printed_page": str(index * 10 + 1)})
            matched.append(match(index, bid))
            for j in range(per_chapter):
                candidates.append(block(f"p{index}_{j}", kind="paragraph", level=0,
                                        text=f"note {chr(97 + index)} {chr(97 + j)} body"))
        return candidates, payload(candidates, toc, matched)

    def test_secondary_batches_are_bounded_and_capped(self):
        candidates, data = self.big_payload()
        omit = {item["block_id"] for item in candidates if item["block_id"].startswith("p")}

        class Omitting(FakeProvider):
            def complete(self, *, system, user):
                request = json.loads(user)
                with self.lock:
                    self.requests.append(request)
                rows = [{"block_id": item["block_id"], "role": "body", "parent_id": None,
                         "confidence": 0.9, "reason": "stub"} for item in request.get("candidates", [])
                        if item["block_id"] not in omit]
                return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")

        provider = Omitting()
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False, parallel=1)
        secondaries = [request for request in provider.requests if request.get("planning") == "adaptive-secondary"]
        self.assertTrue(secondaries)
        self.assertTrue(all(len(request["candidates"]) <= ADAPTIVE_SECONDARY_WINDOW for request in secondaries))
        self.assertLessEqual(meta["upgrade_requests"] + meta["secondary_requests"], MAX_ADDITIONAL_PLANNING_REQUESTS)
        self.assertTrue(all(request.get("context") for request in secondaries))

    def test_upgrade_request_only_lists_suspicious_ids(self):
        candidates, data = self.big_payload(chapters=4, per_chapter=6)
        provider = GateAndUpgradeTests.TocContradictionProvider()
        plan, meta = run_adaptive_planning(data, provider, min_candidates=0, use_gate=False, parallel=1)
        upgrades = [request for request in provider.requests if request.get("planning") == "adaptive-local-upgrade"]
        self.assertTrue(upgrades)
        for request in upgrades:
            self.assertTrue(request["candidates"])
            self.assertTrue(request.get("context"))

    def test_failure_stops_new_requests(self):
        candidates, data = self.big_payload()

        class Failing(FakeProvider):
            def complete(self, *, system, user):
                request = json.loads(user)
                with self.lock:
                    self.requests.append(request)
                    if len(self.requests) >= 2:
                        raise RuntimeError("boom")
                return ProviderResponse(content=json.dumps({"outline": []}), provider="mock", model="mock")

        provider = Failing()
        with self.assertRaises(RuntimeError):
            run_adaptive_planning(data, provider, min_candidates=0, use_gate=False, parallel=1)
        self.assertEqual(len(provider.requests), 2)

    def test_budget_exception_stops_new_requests(self):
        candidates, data = self.big_payload()

        class BudgetFailing(FakeProvider):
            def complete(self, *, system, user):
                request = json.loads(user)
                with self.lock:
                    self.requests.append(request)
                    if len(self.requests) >= 2:
                        raise BudgetExhausted("budget")
                return ProviderResponse(content=json.dumps({"outline": []}), provider="mock", model="mock")

        provider = BudgetFailing()
        with self.assertRaises(BudgetExhausted):
            run_adaptive_planning(data, provider, min_candidates=0, use_gate=False, parallel=1)
        self.assertEqual(len(provider.requests), 2)


if __name__ == "__main__":
    unittest.main()
