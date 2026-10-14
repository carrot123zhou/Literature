"""compact_context（V1/V2 压缩上下文）的边界回归。

保证：
- plan_outline / _proofread_batch 默认（不传或 compact_context=False）行为逐字节不变；
- compact_context=True 时只压缩上下文（overview/目录/previous_outline/context、
  local.reasons/layout/heading_context），候选与决策协议字段不动。
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from literature.models import Document
from literature.providers import ProviderResponse
from literature.structure import _proofread_batch, structure_model_payload
from literature.structure_reasoning import (  # noqa: E402
    compact_candidate, compact_outline_request, compact_proofread_batch, plan_outline,
)


def candidate(bid, *, kind="title", level=2, text=""):
    return {"block_id": bid, "text": text or bid, "type": kind, "level": level,
            "page_index": 1, "bbox": [100, 300, 700, 325],
            "local": {"heading_score": 0.5, "toc_score": 0.5}}


def toc_entry(index, title, level):
    return {"index": index, "title": title, "level": level}


class OutlineSpyProvider:
    """记录完整请求并返回合法规划的假 provider。"""

    def __init__(self):
        self.requests: list[dict] = []
        self.config = None

    def complete(self, *, system, user):
        request = json.loads(user)
        self.requests.append(request)
        outline = [{"block_id": item["block_id"], "role": "h1" if i % 3 == 0 else "h2",
                    "parent_id": None, "confidence": 0.9, "reason": "r"}
                   for i, item in enumerate(request["candidates"])]
        content = json.dumps({"outline": outline, "profile": {"document_kind": "book"}}, ensure_ascii=False)
        return ProviderResponse(content=content, provider="spy", model="spy")


def outline_payload(count=150):
    blocks = [candidate(f"b{i:03d}", text=f"Section {i}") for i in range(count)]
    toc = [toc_entry(i, f"Section {i}", 1) for i in range(0, count, 3)]
    document = Document(project_id="p", title="T", source_language="en",
                        page_count=2, blocks=[])
    payload = structure_model_payload(document, toc, excluded_pages=set())
    payload["title_candidates"] = blocks
    return payload


class PlanOutlineDefaultUnchangedTests(unittest.TestCase):
    def test_default_equals_explicit_false_byte_for_byte(self):
        payload = outline_payload()
        default_provider, explicit_provider = OutlineSpyProvider(), OutlineSpyProvider()
        default_plan = plan_outline(payload, default_provider)
        explicit_plan = plan_outline(payload, explicit_provider, compact_context=False)
        self.assertEqual(default_plan, explicit_plan)
        self.assertEqual(
            [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in default_provider.requests],
            [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in explicit_provider.requests])

    def test_compact_only_changes_context_not_candidates(self):
        payload = outline_payload()
        spy = OutlineSpyProvider()
        plan = plan_outline(payload, spy, compact_context=True)
        self.assertEqual(len(spy.requests), 3)
        first, later = spy.requests[0], spy.requests[1]
        # 第 1 窗不变：仍有 overview 与完整目录。
        self.assertIn("overview", first)
        self.assertEqual(len(first["toc"]), len(payload["authoritative_toc"]))
        # 后续窗口：无 overview，目录只保留近邻条目（非整本）。
        self.assertNotIn("overview", later)
        self.assertLess(len(later["toc"]), len(payload["authoritative_toc"]))
        # 候选本身逐字节不动（outline_inputs 的 compact_candidate 投影不变）。
        self.assertEqual([json.dumps(c, sort_keys=True) for c in later["candidates"]],
                         [json.dumps(compact_candidate(item), sort_keys=True) for item in
                          payload["title_candidates"][64:128]])
        # previous_outline 有界、context 有界。
        self.assertLessEqual(len(later["previous_outline"]), 14)
        self.assertLessEqual(len(later["context"]), 8)
        # 决策不受压缩影响（spy 对相同候选返回相同规划）。
        self.assertEqual(plan["candidates"], 150)


class CompactFunctionTests(unittest.TestCase):
    def test_compact_outline_request_keeps_window1(self):
        request = {"window": 1, "overview": [{"block_id": "a"}], "toc": [{"index": 0}],
                   "previous_outline": list(range(20)), "context": list(range(20))}
        output = compact_outline_request(request)
        self.assertIn("overview", output)
        self.assertEqual(output["toc"], request["toc"])
        # previous_outline/context 仍会被压缩（窗口 1 也压缩这两个可再生的字段）。
        self.assertLessEqual(len(output["previous_outline"]), 14)
        self.assertLessEqual(len(output["context"]), 8)

    def test_compact_proofread_batch_strips_local_noise(self):
        batch = {
            "title_candidates": [{**candidate("a"), "layout": {"font": "bold"},
                                  "local": {"heading_score": 0.5, "reasons": ["中文理由"]}}],
            "heading_context": [{"block_id": "p", "text": "x" * 300}],
        }
        output = compact_proofread_batch(batch)
        item = output["title_candidates"][0]
        self.assertNotIn("layout", item)
        self.assertNotIn("reasons", item["local"])
        self.assertEqual(item["local"]["heading_score"], 0.5)
        context = output["heading_context"][0]
        self.assertEqual(len(context["text"]), 120)
        self.assertTrue(context["text_truncated"])


class ProofreadBatchHookTests(unittest.TestCase):
    def _run(self, compact):
        captured = {}

        def fake_analyze(document, baseline, provider, *, payload, skipped, decisions):
            captured["payload"] = payload
            return [], None

        model_payload = {
            "title_candidates": [{**candidate(f"b{i:03d}"), "layout": {"font": "bold"},
                                  "local": {"heading_score": 0.5, "reasons": ["r"]}} for i in range(4)],
            "authoritative_toc": [toc_entry(0, "b000", 2)],
            "hierarchy_plan": {"profile": {}, "nodes": {"b000": {"role": "h2", "parent_id": None}}},
            "heading_context_seed": True,
        }
        document = Document(project_id="p", title="T", source_language="en", page_count=1, blocks=[])
        with mock.patch("literature.structure.analyze_structure_with_provider", side_effect=fake_analyze):
            _proofread_batch(document, model_payload["authoritative_toc"], object(),
                             model_payload, model_payload["title_candidates"][:2],
                             [], compact_context=compact)
        return captured["payload"]

    def test_default_batch_is_uncompacted(self):
        payload = self._run(False)
        item = payload["title_candidates"][0]
        self.assertIn("layout", item)
        self.assertIn("reasons", item["local"])

    def test_compact_batch_strips_local_noise(self):
        payload = self._run(True)
        item = payload["title_candidates"][0]
        self.assertNotIn("layout", item)
        self.assertNotIn("reasons", item["local"])


if __name__ == "__main__":
    unittest.main()
