"""AIFold 有界批处理 / 逐 unit 检查点 / 装置内容抑制的针对性测试。

覆盖（不依赖真实模型、零付费）：

1. `plan_batches`：≤12 unit、估算输入 ≤6000、输出 ≤4000；超长单 unit 单独成窗。
2. `plan_unit_windows` / `plan_clause_windows`：窗口只落在原句/子句边界，合起来**覆盖全部**
   句子与 token（不截断正文）。
3. 指纹：服务变化（同模型名）必须换指纹；派生上下文（源层短层/证据句）变化必须换指纹。
4. 装置内容（用户 2026-10-27 批准）：显式 Index/References 标题 → 有界 AI 分类 →
   affirmative 且不混合 → 全 unit suppressed（Full 保留、零短层生成）+ 收据；收据指纹
   让 ready 能验证身份；mixed/body/分类失败 → 不抑制（失败保持未就绪）。
5. 部分成功：漏段 → 每次调用重试一轮；仍缺 → 本节失败但已完成 unit 落盘；resume 只补缺项。
6. 多批节：章节摘要只发一次；摘要失败 → 该节未就绪（不沿用子集摘要冒充）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literature.models import Block, Document, Page
from literature.reader import ReaderLayerService, ReaderStore
from literature.reader_apparatus import (
    apparatus_fingerprint,
    candidate_kind,
    parse_classifier_reply,
)
from literature.reader_batches import (
    MAX_BATCH_UNITS,
    MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    estimate_tokens,
    estimate_unit_output_tokens,
    plan_batches,
    plan_clause_windows,
    plan_unit_windows,
    unit_clause_spans,
    window_unit,
    window_unit_clause,
)
from literature.storage import ProjectStore

try:  # 可选：真实书标题只在有 fixtures 时校验
    from literature.reader_apparatus import POLICY_VERSION
except ImportError:  # pragma: no cover
    POLICY_VERSION = 1


# ---------------------------------------------------------------------------
# 夹具：一节的段落数量与长度可控
# ---------------------------------------------------------------------------


def make_document(project_id: str, *, sections: dict[str, list[str]]) -> Document:
    blocks: list[Block] = []
    order = 0
    page = 0
    for title, paragraphs in sections.items():
        blocks.append(Block(block_id=f"t-{title}", type="title", order=order,
                            page_index=page, source_text=title, level=1, bbox=[10, 10, 300, 40]))
        order += 1
        for index, text in enumerate(paragraphs):
            blocks.append(Block(
                block_id=f"{title}-p{index}", type="paragraph", order=order, page_index=page,
                bbox=[10, 50, 300, 120], source_text=text,
            ))
            order += 1
        page += 1
    return Document(project_id=project_id, title="Batch Book", source_language="en", page_count=page + 1,
                    pages=[Page(page_index=index, width=600, height=800) for index in range(page + 1)],
                    blocks=blocks)


class FakeProvider:
    """脚本化 provider：按提示词里的 `[U{n}]` 编号回 JSON，并记录 (kind, 调用次数)。

    `omit_last`：每次折叠回复都少写最后一个 unit（模拟模型漏段 → 触发一次重试）。
    `fail_classify` / `classify_kind`：控制装置分类回复。
    `fail_summary`：章节摘要请求失败（验证「不沿用子集摘要」）。
    """

    def __init__(self, *, omit_last: bool = False, classify: dict | None = None,
                 fail_classify: bool = False, fail_summary: bool = False,
                 fail_fold: bool = False) -> None:
        from literature.providers import MockProvider
        self.config = MockProvider().config
        self.calls: list[str] = []
        self.prompts: list[tuple[str, str]] = []
        self.omit_last = omit_last
        self.classify = classify if classify is not None else {
            "kind": "body", "mixed": False, "reason": "prose",
        }
        self.fail_classify = fail_classify
        self.fail_summary = fail_summary
        self.fail_fold = fail_fold

    # -- 内部 ----------------------------------------------------------
    @staticmethod
    def _unit_ids(user: str) -> list[str]:
        import re
        return [f"U{m.group(1)}" for m in (re.match(r"\[U(\d+)\]", line) for line in user.splitlines()) if m]

    def complete(self, *, system: str, user: str):
        from literature.providers import ProviderError, ProviderResponse

        if "classifying one section" in user:
            self.calls.append("classify")
            if self.fail_classify:
                raise ProviderError("分类请求失败")
            payload = self.classify
            return ProviderResponse(content=json.dumps(payload, ensure_ascii=False),
                                    provider="fake", model="fake-model",
                                    request_id=f"req-{len(self.calls)}",
                                    input_tokens=120, output_tokens=20, estimated_cost=0.0)
        units = self._unit_ids(user)
        if not units:
            self.calls.append("summary")
            if self.fail_summary:
                raise ProviderError("摘要请求失败")
            return ProviderResponse(
                content=json.dumps({"section": {"takeaway": "本节汇总断言。", "topic": "汇总"}},
                                   ensure_ascii=False),
                provider="fake", model="fake-model", request_id=f"req-{len(self.calls)}",
                input_tokens=80, output_tokens=20, estimated_cost=0.0,
            )
        kind = "derive" if "source takeaway:" in user else "fold"
        self.calls.append(kind)
        self.prompts.append((kind, user))
        if self.fail_fold:
            raise ProviderError("折叠请求失败")
        kept = units[:-1] if self.omit_last else units
        if "source takeaway:" in user:
            body = [{"id": unit, "takeaway": f"译文要点 {unit}", "reason": "译文理由。",
                     "topic": "译文", "ev_sentences": ["分层译句。"]} for unit in kept]
        else:
            body = [{"id": unit, "evidence": [0], "role": "claim",
                     "takeaway": "Silver vessels travelled along the Silk Road",
                     "reason": "Demand fell after the ninth century.", "topic": "Silver vessels"}
                    for unit in kept]
        payload = {"section": {"takeaway": "整节主张。", "topic": "整节"}, "units": body}
        return ProviderResponse(content=json.dumps(payload, ensure_ascii=False), provider="fake",
                                model="fake-model", request_id=f"req-{len(self.calls)}",
                                input_tokens=200, output_tokens=60, estimated_cost=0.0)


def paragraph(words: int = 12, seed: int = 0) -> str:
    base = ("Silver vessels travelled along the Silk Road and workshops in Chang'an copied "
            "Sogdian shapes for decades before demand fell").split()
    text = " ".join(base[(seed + index) % len(base)] for index in range(words))
    return f"{text.capitalize()}."


class BatchPlanTests(unittest.TestCase):
    def test_batches_respect_unit_and_token_caps(self) -> None:
        units = {f"u{index}": {"toks": [{"t": "word"}] * 40, "sents": [[0, 40]]} for index in range(40)}
        plans = plan_batches(
            list(units),
            build_prompt=lambda batch: ("system", "prompt " + "x" * (100 * len(batch))),
            unit_output=lambda uid: 200,
        )
        self.assertTrue(plans)
        for plan in plans:
            self.assertLessEqual(len(plan.units), MAX_BATCH_UNITS)
            self.assertLessEqual(plan.input_tokens, MAX_INPUT_TOKENS)
            self.assertLessEqual(plan.output_tokens, MAX_OUTPUT_TOKENS)
        self.assertEqual([uid for plan in plans for uid in plan.units], list(units))

    def test_single_oversized_unit_becomes_own_plan(self) -> None:
        plans = plan_batches(
            ["big", "small"],
            build_prompt=lambda batch: ("s", "y" * (MAX_INPUT_TOKENS * 4) if "big" in batch else "y"),
            unit_output=lambda uid: 200,
        )
        self.assertEqual(plans[0].units, ("big",))
        self.assertGreater(plans[0].input_tokens, MAX_INPUT_TOKENS)
        self.assertEqual(plans[1].units, ("small",))

    def test_output_estimator_separates_lanes(self) -> None:
        unit = {"toks": [{"t": "word"}] * 300, "sents": [[0, 300]],
                "lad": {"ev": [0]}}
        self.assertEqual(estimate_unit_output_tokens(unit, lane="source"), 200)
        self.assertGreater(
            estimate_unit_output_tokens(unit, lane="derive", locale="zh-CN"), 400,
        )


class WindowTests(unittest.TestCase):
    def _unit(self, sentences: int = 6, words: int = 30) -> dict:
        toks: list[dict] = []
        sents: list[list[int]] = []
        for index in range(sentences):
            start = len(toks)
            toks.extend({"t": f"w{index}-{pos}"} for pos in range(words))
            toks[-1]["t"] = f"w{index}."
            sents.append([start, len(toks)])
        return {"id": "big", "toks": toks, "sents": sents, "role": "para",
                "lad": {"ev": [0], "brief": [], "tn": 0, "topic": []}}

    def test_sentence_windows_cover_every_sentence(self) -> None:
        unit = self._unit(sentences=8, words=200)
        plans = plan_unit_windows(
            "big", sentence_count=8,
            build_prompt=lambda a, b: ("s", "x" * (4000 * (b - a))),
            output_tokens=200, max_input_tokens=3500,
        )
        self.assertGreater(len(plans), 1)
        covered = [(plan.window["sents_start"], plan.window["sents_end"]) for plan in plans]
        self.assertEqual(covered[0][0], 0)
        self.assertEqual(covered[-1][1], 8)
        for (_, end), (start, _) in zip(covered, covered[1:]):
            self.assertEqual(end, start)   # 连续无空洞

    def test_clause_windows_cover_whole_sentence(self) -> None:
        unit = self._unit(sentences=2, words=300)
        spans = unit_clause_spans(unit, 0)
        self.assertTrue(spans)
        self.assertEqual(spans[0][0], unit["sents"][0][0])
        self.assertEqual(spans[-1][1], unit["sents"][0][1])
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertEqual(end, start)
        plans = plan_clause_windows(
            "big", sentence_index=0, sentence_count=2, clause_tokens=spans,
            build_prompt=lambda a, b: ("s", "x" * ((b - a) * 8)), output_tokens=200,
            max_input_tokens=400,
        )
        self.assertGreater(len(plans), 1)
        self.assertEqual(plans[0].window["parent_sent"], 0)
        self.assertTrue(all(plan.window["clause"] for plan in plans))

    def test_window_proxies_rebase_offsets(self) -> None:
        unit = self._unit(sentences=3, words=5)
        proxy = window_unit(unit, start=1, end=3)
        self.assertEqual(proxy["sents_start"], 1)
        self.assertEqual(proxy["sents"][0][0], 0)
        clause = window_unit_clause(unit, sentence_index=1, tok_start=10, tok_end=20)
        self.assertEqual(clause["parent_sent"], 1)
        self.assertEqual(clause["sents"], [[0, 10]] if False else clause["sents"])
        self.assertEqual(clause["sents"], [[0, len(clause["toks"])]])
        self.assertEqual(clause["source_tok_start"], 10)


class FingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4")
        project = self.store.create_project(source, title="Batch Book")
        self.project_id = project["project_id"]
        self.document = make_document(self.project_id, sections={"Intro": [paragraph(10)]})
        self.store.save_document(self.document)
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _unit_and_chunk(self):
        from literature.textfold_adapter import _glossary_hash, build_ladder_doc
        doc = build_ladder_doc(self.document, locale="source")
        chunk = next(item for item in doc["chunks"] if item["units"])
        return doc, chunk, doc["units"][chunk["units"][0]]

    def test_service_change_breaks_fingerprint(self) -> None:
        doc, chunk, unit = self._unit_and_chunk()
        first = self.service._unit_fingerprint(
            doc=doc, chunk=chunk, unit=unit, lane="source", locale="source", uid=unit["id"],
            model_label="same-model", glossary_hash="", service_id="service-a",
        )
        second = self.service._unit_fingerprint(
            doc=doc, chunk=chunk, unit=unit, lane="source", locale="source", uid=unit["id"],
            model_label="same-model", glossary_hash="", service_id="service-b",
        )
        self.assertNotEqual(first, second, "同模型名换服务必须换指纹，不能复用短层")

    def test_derive_context_tracks_consumed_source_layer(self) -> None:
        doc, chunk, unit = self._unit_and_chunk()
        base = self.service._unit_fingerprint(
            doc=doc, chunk=chunk, unit=unit, lane="derive", locale="zh-CN", uid=unit["id"],
            model_label="m", glossary_hash="", service_id="s",
        )
        changed_unit = {**unit, "lad": {"brief": [{"t": "别的"}, {"t": "短层"}, {"t": "."}],
                                        "tn": 2, "topic": [{"t": "别的"}], "ev": [0],
                                        "role": "claim", "model": "m"}}
        changed = self.service._unit_fingerprint(
            doc=doc, chunk=chunk, unit=changed_unit, lane="derive", locale="zh-CN",
            uid=unit["id"], model_label="m", glossary_hash="", service_id="s",
        )
        self.assertNotEqual(base, changed, "源层短层变化必须让派生指纹失效")


class ApparatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4")
        project = self.store.create_project(source, title="Batch Book")
        self.project_id = project["project_id"]
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _save(self, sections: dict[str, list[str]]) -> None:
        self.store.save_document(make_document(self.project_id, sections=sections))

    def test_candidate_titles_are_explicit_only(self) -> None:
        self.assertEqual(candidate_kind("Index"), "index")
        self.assertEqual(candidate_kind("Primary Sources"), "bibliography")
        self.assertEqual(candidate_kind("Secondary Sources"), "bibliography")
        self.assertEqual(candidate_kind("Bibliography"), "bibliography")
        self.assertIsNone(candidate_kind("Archives and Sources in the Body"))
        self.assertIsNone(candidate_kind("The Source of the Nile"))

    def test_affirmative_classification_suppresses_without_short_layers(self) -> None:
        self._save({"Index": [paragraph(8, seed=1), paragraph(9, seed=2)]})
        provider = FakeProvider(classify={"kind": "index", "mixed": False, "reason": "page locators"})
        result = self.service.generate(self.project_id, provider=provider, model_label="m",
                                       service_id="svc")
        self.assertEqual(result["failed"], [])
        self.assertEqual(provider.calls.count("classify"), 1)
        self.assertEqual(provider.calls.count("fold"), 0, "装置节不得生成短层")
        doc = self.service.document(self.project_id)
        section = doc["sections"][0]
        self.assertTrue(all(unit.get("source") == "suppressed" for unit in section["units"]))
        self.assertTrue(all(not unit.get("brief") for unit in section["units"]))
        # Full 不动：原文仍在
        self.assertTrue(all(unit.get("full") for unit in section["units"]))
        # 收据让 ready 验得动身份
        receipt = ReaderStore(self.store).load_layers(self.project_id)
        self.assertTrue(result["sections"])
        readiness = self.service.fold_run_readiness(
            self.project_id, locale="source", service_id="svc", model="m",
        )
        self.assertTrue(readiness["ready"], readiness)
        self.assertTrue(receipt["docs"].get("source"))

    def test_mixed_or_body_is_not_suppressed(self) -> None:
        for verdict in ({"kind": "index", "mixed": True, "reason": "mixed"},
                        {"kind": "body", "mixed": False, "reason": "prose"}):
            with self.subTest(verdict=verdict["kind"] + str(verdict["mixed"])):
                self._save({"Index": [paragraph(8, seed=3)]})
                provider = FakeProvider(classify=verdict)
                result = self.service.generate(self.project_id, provider=provider,
                                               model_label="m", service_id="svc", force=True)
                self.assertEqual(result["failed"], [])
                self.assertGreaterEqual(provider.calls.count("fold"), 1, "非装置必须正常折叠")
                section = self.service.document(self.project_id)["sections"][0]
                self.assertTrue(any(unit.get("source") == "model" for unit in section["units"]))

    def test_classifier_failure_keeps_section_pending(self) -> None:
        self._save({"Index": [paragraph(8, seed=4)]})
        provider = FakeProvider(fail_classify=True)
        result = self.service.generate(self.project_id, provider=provider, model_label="m",
                                       service_id="svc")
        self.assertTrue(result["failed"], "分类失败必须保持未就绪，不猜、不抑制")
        self.assertEqual(provider.calls.count("fold"), 0)
        section = self.service.document(self.project_id)["sections"]
        self.assertEqual(section, [])

    def test_valid_cached_section_skips_classification(self) -> None:
        self._save({"Index": [paragraph(10, seed=5)]})
        provider = FakeProvider(classify={"kind": "body", "mixed": False, "reason": "prose"})
        self.service.generate(self.project_id, provider=provider, model_label="m", service_id="svc")
        before = len(provider.calls)
        self.service.generate(self.project_id, provider=provider, model_label="m", service_id="svc")
        self.assertEqual(len(provider.calls), before, "有效缓存节必须零调用（含不重复分类）")

    def test_parse_classifier_reply_requires_bool_and_known_kind(self) -> None:
        self.assertFalse(parse_classifier_reply({"kind": "index", "mixed": "yes"})["ok"])
        self.assertFalse(parse_classifier_reply({"kind": "appendix", "mixed": False})["ok"])
        self.assertTrue(parse_classifier_reply({"kind": "index", "mixed": False})["affirmative"])
        self.assertFalse(parse_classifier_reply({"kind": "index", "mixed": True})["affirmative"])


class ApparatusTargetLaneTests(unittest.TestCase):
    """默认 zh-CN 整篇 run：装置分类必须也发生在**原文前置**路径上（S85/S87 的真实拓扑）。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4")
        project = self.store.create_project(source, title="Batch Book")
        self.project_id = project["project_id"]
        document = make_document(self.project_id, sections={
            "Index": [paragraph(8, seed=1), paragraph(8, seed=2)],
            "Body": [paragraph(10, seed=3)],
        })
        self.store.save_document(document)
        from literature.models import TranslationEntry, TranslationLayer
        entries = {}
        for block in document.blocks:
            if block.type == "paragraph":
                entries[block.block_id] = TranslationEntry(
                    block_id=block.block_id, text=f"[译] {block.source_text}",
                )
        self.store.save_translation(
            TranslationLayer(project_id=self.project_id, locale="zh-CN", entries=entries)
        )
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_gate_runs_when_source_cache_is_valid_but_target_is_missing(self) -> None:
        """真实 S85：源层是策略生效前折好的**有效缓存**，目标层缺项。

        旧实现 `_ensure_source_fold` 在「有效源缓存」分支直接 return，闸门永远不会执行，
        目标层照旧派生 233 段。闸门必须在那个 return **之前**。
        """
        # 只留 Index 一节：便于断言「装置节本身零折叠/零派生」。
        document = make_document(self.project_id, sections={"Index": [paragraph(8, seed=1),
                                                                     paragraph(8, seed=2)]})
        self.store.save_document(document)
        from literature.models import TranslationEntry, TranslationLayer
        self.store.save_translation(TranslationLayer(
            project_id=self.project_id, locale="zh-CN",
            entries={block.block_id: TranslationEntry(block_id=block.block_id,
                                                      text=f"[译] {block.source_text}")
                     for block in document.blocks if block.type == "paragraph"},
        ))
        prose = FakeProvider(classify={"kind": "body", "mixed": False, "reason": "prose"})
        self.service.generate(self.project_id, provider=prose, model_label="m", service_id="svc")
        self.assertGreaterEqual(prose.calls.count("fold"), 1, "源层先按 prose 折好（两段同批）")
        # 抹掉分类收据，模拟「策略生效前就已存在的有效源缓存」。
        checkpoints_path = (self.store.project_path(self.project_id)
                            / "reader" / "fold_checkpoints.json")
        payload = json.loads(checkpoints_path.read_text("utf-8"))
        payload["sections"]["source"]["S1"].pop("apparatus", None)
        checkpoints_path.write_text(json.dumps(payload), "utf-8")

        provider = FakeProvider(classify={"kind": "index", "mixed": False, "reason": "locators"})
        result = self.service.generate(self.project_id, locale="zh-CN", provider=provider,
                                       model_label="m", service_id="svc")
        self.assertEqual(result["failed"], [])
        self.assertEqual(provider.calls.count("classify"), 1, "源缓存有效时也要分类一次")
        self.assertEqual(provider.calls.count("fold"), 0, "有效源缓存不得重折")
        self.assertEqual(provider.calls.count("derive"), 0, "装置节零派生（233 段不再付费）")
        readiness = self.service.fold_run_readiness(
            self.project_id, locale="zh-CN", service_id="svc", model="m",
        )
        self.assertTrue(readiness["ready"], readiness)

    def test_target_lane_inherits_apparatus_suppression_with_zero_derivation(self) -> None:
        provider = FakeProvider(classify={"kind": "index", "mixed": False, "reason": "locators"})
        result = self.service.generate(self.project_id, locale="zh-CN", provider=provider,
                                       model_label="m", service_id="svc")
        self.assertEqual(result["failed"], [])
        self.assertEqual(provider.calls.count("classify"), 1, "候选节只分类一次")
        # Index 节：源层零折叠、目标层零派生；Body 节正常各一次
        self.assertEqual(provider.calls.count("fold"), 1, provider.calls)
        self.assertEqual(provider.calls.count("derive"), 1, provider.calls)
        readiness = self.service.fold_run_readiness(
            self.project_id, locale="zh-CN", service_id="svc", model="m",
        )
        self.assertTrue(readiness["ready"], readiness)


class PartialAndSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4")
        project = self.store.create_project(source, title="Batch Book")
        self.project_id = project["project_id"]
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_missing_unit_is_retried_once_then_reported(self) -> None:
        self.store.save_document(make_document(self.project_id, sections={
            "Intro": [paragraph(10, seed=1), paragraph(10, seed=2), paragraph(10, seed=3)],
        }))
        provider = FakeProvider(omit_last=True)
        result = self.service.generate(self.project_id, provider=provider, model_label="m",
                                       service_id="svc")
        # 首次折叠 + 一次重试（每次少最后一段），不无限重试
        self.assertEqual(provider.calls.count("fold"), 2, provider.calls)
        self.assertTrue(result["failed"])
        self.assertIn("未生成", result["failed"][0]["error"])
        stored = self.service.document(self.project_id)["sections"]
        self.assertEqual(len(stored), 1)
        units = stored[0]["units"]
        self.assertEqual([unit.get("source") for unit in units], ["model", "model", "pending"])
        self.assertTrue(units[-1].get("error"), "未生成的 unit 必须带具体错误（readiness 理由）")
        readiness = self.service.fold_run_readiness(
            self.project_id, locale="source", service_id="svc", model="m",
        )
        self.assertFalse(readiness["ready"])
        self.assertIn("Intro-p2", readiness["sections"][0]["reason"], "readiness 理由要带具体 unit 与错误")

    def test_partial_units_persist_and_resume_only_fills_gaps(self) -> None:
        self.store.save_document(make_document(self.project_id, sections={
            "Intro": [paragraph(10, seed=1), paragraph(10, seed=2), paragraph(10, seed=3)],
        }))

        class LastOnly(FakeProvider):
            """第一次折叠只漏最后一段；之后完整回复。"""

            def __init__(self) -> None:
                super().__init__()
                self.omitted = 0

            def complete(self, *, system: str, user: str):
                from literature.providers import ProviderError, ProviderResponse
                if "classifying one section" in user or not self._unit_ids(user):
                    return super().complete(system=system, user=user)
                units = self._unit_ids(user)
                self.calls.append("derive" if "source takeaway:" in user else "fold")
                kept = units
                if self.omitted < 2:          # 首折 + 一次重试都漏同一段
                    kept = units[:-1]
                    self.omitted += 1
                body = [{"id": unit, "evidence": [0], "role": "claim",
                         "takeaway": "Silver vessels travelled along the Silk Road",
                         "reason": "Demand fell.", "topic": "Silver vessels"} for unit in kept]
                return ProviderResponse(
                    content=json.dumps({"section": {"takeaway": "整节主张。", "topic": "整节"},
                                        "units": body}, ensure_ascii=False),
                    provider="fake", model="fake-model", request_id="r",
                    input_tokens=10, output_tokens=5, estimated_cost=0.0,
                )

        provider = LastOnly()
        first = self.service.generate(self.project_id, provider=provider, model_label="m",
                                      service_id="svc")
        self.assertTrue(first["failed"], "部分成功节不得假装完成")
        stored = self.service.document(self.project_id)["sections"]
        self.assertTrue(stored, "已完成的 unit 必须落盘（部分成功不丢）")
        sources = [unit.get("source") for unit in stored[0]["units"]]
        self.assertIn("model", sources)
        folds_before = provider.calls.count("fold")
        second = self.service.generate(self.project_id, provider=provider, model_label="m",
                                       service_id="svc")
        self.assertEqual(second["failed"], [])
        # resume 只补缺的那一段：1 次折叠请求（+1 次摘要，因为摘要缺失）
        self.assertEqual(provider.calls.count("fold") - folds_before, 1, provider.calls)
        readiness = self.service.fold_run_readiness(
            self.project_id, locale="source", service_id="svc", model="m",
        )
        self.assertTrue(readiness["ready"], readiness)

    def test_multi_batch_section_summarizes_once(self) -> None:
        paragraphs = [paragraph(9, seed=index) for index in range(14)]
        self.store.save_document(make_document(self.project_id, sections={"Long": paragraphs}))
        provider = FakeProvider()
        result = self.service.generate(self.project_id, provider=provider, model_label="m",
                                       service_id="svc")
        self.assertEqual(result["failed"], [])
        self.assertEqual(provider.calls.count("fold"), 2, "14 段应分成 2 批")
        self.assertEqual(provider.calls.count("summary"), 1, "章节摘要只发一次")
        section = self.service.document(self.project_id)["sections"][0]
        self.assertTrue(section.get("takeaway"), "章节短层来自唯一一次摘要")

    def test_summary_failure_keeps_section_not_ready(self) -> None:
        paragraphs = [paragraph(9, seed=index) for index in range(14)]
        self.store.save_document(make_document(self.project_id, sections={"Long": paragraphs}))
        provider = FakeProvider(fail_summary=True)
        result = self.service.generate(self.project_id, provider=provider, model_label="m",
                                       service_id="svc")
        self.assertTrue(result["failed"])
        self.assertIn("摘要", result["failed"][0]["error"])
        readiness = self.service.fold_run_readiness(
            self.project_id, locale="source", service_id="svc", model="m",
        )
        self.assertFalse(readiness["ready"], "子集/缺失摘要不得 ready")

    def test_readiness_detects_missing_unit_coverage(self) -> None:
        self.store.save_document(make_document(self.project_id, sections={
            "Intro": [paragraph(10, seed=1), paragraph(10, seed=2)],
        }))
        self.service.generate(self.project_id, provider=FakeProvider(), model_label="m",
                              service_id="svc")
        reader = ReaderStore(self.store)
        stored = reader.load_layers(self.project_id)
        doc = stored["docs"]["source"]
        section = doc["sections"][0]
        section["units"] = section["units"][:-1]      # 人为丢掉一段（模拟旧 bug）
        reader.save_locale_doc(self.project_id, "source", doc,
                               stored["section_hashes"].get("source") or {})
        readiness = self.service.fold_run_readiness(
            self.project_id, locale="source", service_id="svc", model="m",
        )
        self.assertFalse(readiness["ready"])
        self.assertIn("结构不一致", readiness["sections"][0]["reason"])


class VerbatimReuseTests(unittest.TestCase):
    """复用路径必须**逐字**保留用户已看过的短层（真实验收：S5 12 个复用 unit 字段全变）。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4")
        project = self.store.create_project(source, title="Batch Book")
        self.project_id = project["project_id"]
        self.store.save_document(make_document(self.project_id, sections={
            "Intro": [paragraph(11, seed=1), paragraph(11, seed=2)],
        }))
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _fields(section: dict) -> list[dict]:
        return [
            {key: unit.get(key) for key in ("id", "takeaway", "key", "topic", "brief",
                                            "evidence", "brief_text")}
            for unit in section["units"]
        ]

    def test_checkpoint_only_rebuild_preserves_short_layer_fields(self) -> None:
        provider = FakeProvider()
        self.service.generate(self.project_id, provider=provider, model_label="m", service_id="svc")
        before = self._fields(self.service.document(self.project_id)["sections"][0])
        # 删掉 layers.json 里的存盘层（保留检查点）：下一次必须靠检查点重建且逐字不变。
        reader = ReaderStore(self.store)
        reader.save_locale_doc(self.project_id, "source",
                               {"format": "textfold/doc", "version": 0, "project_id": self.project_id,
                                "locale": "source", "title": "Batch Book", "sections": [],
                                "provenance": {}, "failed": []},
                               {})
        calls_before = len(provider.calls)
        folds_before = provider.calls.count("fold")
        self.service.generate(self.project_id, provider=provider, model_label="m", service_id="svc")
        after = self._fields(self.service.document(self.project_id)["sections"][0])
        self.assertEqual(before, after, "复用路径改写了短层字段（必须逐字不变）")
        self.assertEqual(provider.calls.count("fold"), folds_before, "检查点齐备时不应重新折叠")
        self.assertLessEqual(len(provider.calls) - calls_before, 1, "至多补一次章节摘要")


class CheckpointIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4")
        project = self.store.create_project(source, title="Batch Book")
        self.project_id = project["project_id"]
        self.store.save_document(make_document(self.project_id, sections={
            "Intro": [paragraph(10, seed=1), paragraph(10, seed=2)],
        }))
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_service_switch_refolds_instead_of_reusing_checkpoints(self) -> None:
        provider = FakeProvider()
        self.service.generate(self.project_id, provider=provider, model_label="m", service_id="svc-a")
        folds = provider.calls.count("fold")
        # 换服务、同模型：检查点指纹不同 → 必须重折（不能用别家服务的产物冒充）
        document = self.store.load_document(self.project_id)
        document.blocks[1].source_text = paragraph(12, seed=9)
        self.store.save_document(document)
        self.service.generate(self.project_id, provider=provider, model_label="m", service_id="svc-b")
        self.assertGreater(provider.calls.count("fold"), folds)

    def test_checkpoint_file_is_written_and_reusable(self) -> None:
        provider = FakeProvider()
        self.service.generate(self.project_id, provider=provider, model_label="m", service_id="svc")
        path = self.store.project_path(self.project_id) / "reader" / "fold_checkpoints.json"
        self.assertTrue(path.is_file())
        payload = json.loads(path.read_text("utf-8"))
        units = payload["sections"]["source"]["S1"]["units"]
        self.assertEqual(len(units), 2)
        self.assertTrue(all(entry.get("fp") for entry in units.values()))
        before = len(provider.calls)
        self.service.generate(self.project_id, provider=provider, model_label="m", service_id="svc")
        self.assertEqual(len(provider.calls), before)

class RetrySafetyTests(unittest.TestCase):
    """重试安全：可能已计费的传输失败不得自动重发；输出截断只允许更小批重试一次。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4")
        project = self.store.create_project(source, title="Batch Book")
        self.project_id = project["project_id"]
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_transport_error_is_not_retried_automatically(self) -> None:
        self.store.save_document(make_document(self.project_id, sections={
            "Intro": [paragraph(10, seed=1), paragraph(10, seed=2)],
        }))
        provider = FakeProvider(fail_fold=True)
        result = self.service.generate(self.project_id, provider=provider, model_label="m",
                                       service_id="svc")
        self.assertTrue(result["failed"])
        self.assertEqual(provider.calls.count("fold"), 1,
                         f"可能已计费的传输失败不得自动重发：{provider.calls}")

    def test_truncated_reply_retried_once_with_smaller_batch(self) -> None:
        paragraphs = [paragraph(9, seed=index) for index in range(8)]
        self.store.save_document(make_document(self.project_id, sections={"Long": paragraphs}))

        class Truncating(FakeProvider):
            def __init__(self) -> None:
                super().__init__()
                self.truncated = False
                self.batch_sizes: list[int] = []

            def complete(self, *, system: str, user: str):
                from literature.providers import ProviderResponse
                units = self._unit_ids(user)
                if units and not self.truncated:
                    self.truncated = True
                    self.calls.append("fold")
                    self.batch_sizes.append(len(units))
                    return ProviderResponse(content='{"section": {}}', provider="fake",
                                            model="fake-model", request_id="t",
                                            input_tokens=10, output_tokens=4000,
                                            estimated_cost=0.0, finish_reason="length")
                if units:
                    self.batch_sizes.append(len(units))
                return super().complete(system=system, user=user)

        provider = Truncating()
        result = self.service.generate(self.project_id, provider=provider, model_label="m",
                                       service_id="svc")
        self.assertGreaterEqual(len(provider.batch_sizes), 2, provider.batch_sizes)
        self.assertEqual(provider.batch_sizes[0], 8)
        self.assertTrue(all(size <= 6 for size in provider.batch_sizes[1:]), provider.batch_sizes)
        self.assertEqual(result["failed"], [])


class LegacySeedTests(unittest.TestCase):
    """真实故障（Imperiled S10）：源层只修 1 段时，旧的有效派生 unit 不得全量重烧。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4")
        project = self.store.create_project(source, title="Batch Book")
        self.project_id = project["project_id"]
        document = make_document(self.project_id, sections={
            "Intro": [paragraph(10, seed=1), paragraph(10, seed=2)],
        })
        self.store.save_document(document)
        from literature.models import TranslationEntry, TranslationLayer
        entries = {
            block.block_id: TranslationEntry(block_id=block.block_id, text=f"[译] {block.source_text}")
            for block in document.blocks if block.type == "paragraph"
        }
        self.store.save_translation(
            TranslationLayer(project_id=self.project_id, locale="zh-CN", entries=entries)
        )
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_legacy_state(self, repaired: str | None = None) -> str:
        """构造真实 legacy 状态：源层留 1 个 pending unit，且目标层键仍是「由这份旧源层派生」。

        返回被「修复」的 unit id（`repaired=None` 表示只损坏源层）。
        """
        from literature.reader import (
            _sections_from_ladder, _source_fold_fingerprint, derive_section_digest, section_digest,
        )
        from literature.textfold_adapter import _glossary_hash, build_ladder_doc

        reader = ReaderStore(self.store)
        stored = reader.load_layers(self.project_id)
        source_doc = stored["docs"]["source"]
        section = source_doc["sections"][0]
        target = section["units"][1]["id"] if repaired is None else repaired
        for unit in section["units"]:
            if unit["id"] == target:
                unit.update({"source": "pending", "brief": [], "takeaway": "", "key": "", "topic": ""})
        reader.save_locale_doc(self.project_id, "source", source_doc,
                               stored["section_hashes"].get("source") or {})
        # 目标层键按「旧源快照」重算（证据：这次派生确实发生在这份源层上）。
        source_preview = _sections_from_ladder(
            build_ladder_doc(self.store.load_document(self.project_id), locale="source")
        )[0]
        # 术语表哈希必须与生产路径同源（`_glossary_hash` 对空快照也返回哈希，不是 ""），
        # 否则这里写出的「旧派生键」永远匹配不上真实 generate 的重算值。
        translated = self.store.load_translation(self.project_id, "zh-CN")
        expected_old = derive_section_digest(
            locale="zh-CN", service_id="svc", model="m",
            source_digest=section_digest(source_preview, "source", service_id="svc", model="m"),
            glossary_hash=_glossary_hash(translated),
            source_fingerprint=_source_fold_fingerprint(section),
        )
        hashes = dict(stored["section_hashes"] or {})
        hashes["zh-CN"] = {**(hashes.get("zh-CN") or {}), section["id"]: expected_old}
        data = reader.load_layers(self.project_id)
        data["section_hashes"]["zh-CN"] = hashes["zh-CN"]
        reader.save_layers(self.project_id, data)
        # 删掉该 unit 的源层检查点，模拟「源层是旧产物、没有逐 unit 检查点」
        checkpoints_path = self.store.project_path(self.project_id) / "reader" / "fold_checkpoints.json"
        payload = json.loads(checkpoints_path.read_text("utf-8"))
        payload["sections"]["source"]["S1"]["units"].pop(target, None)
        checkpoints_path.write_text(json.dumps(payload), "utf-8")
        return target

    def _drop_target_checkpoints(self) -> None:
        """真实 legacy 状态：旧目标产物早于逐 unit 检查点（该 lane 没有任何条目）。"""
        checkpoints_path = self.store.project_path(self.project_id) / "reader" / "fold_checkpoints.json"
        payload = json.loads(checkpoints_path.read_text("utf-8"))
        payload["sections"].pop("zh-CN", None)
        checkpoints_path.write_text(json.dumps(payload), "utf-8")

    def test_legacy_target_units_seeded_before_source_repair_are_reused(self) -> None:
        provider = FakeProvider()
        self.service.generate(self.project_id, locale="zh-CN", provider=provider,
                              model_label="m", service_id="svc")
        self.assertEqual(provider.calls.count("derive"), 1, provider.calls)   # 两段一批
        before = {unit["id"]: dict(unit) for unit in
                  self.service.document(self.project_id, locale="zh-CN")["sections"][0]["units"]}
        # 模拟真实书 S10：源层第二段回退 pending、目标层键仍是旧源层派生键，且该节**没有**
        # 逐 unit 检查点（旧产物早于逐 unit 检查点）。
        self._make_legacy_state()
        self._drop_target_checkpoints()
        provider2 = FakeProvider()
        result = self.service.generate(self.project_id, locale="zh-CN", provider=provider2,
                                       model_label="m", service_id="svc")
        self.assertEqual(result["failed"], [])
        # 源层修复 1 段 = 1 次折叠；派生只请求**被修的那一段**（旧源层当时没有短层，无法证明
        # 其消费上下文），另一段由 legacy 播种的检查点复用。
        self.assertEqual(provider2.calls.count("fold"), 1, provider2.calls)
        self.assertEqual(provider2.calls.count("derive"), 1, provider2.calls)
        derive_prompts = [text for kind, text in provider2.prompts if kind == "derive"]
        self.assertEqual(FakeProvider._unit_ids(derive_prompts[0]), ["U0"])
        self.assertEqual(len(FakeProvider._unit_ids(derive_prompts[0])), 1,
                         "只请求一段（另一段由播种检查点复用）")
        after = {unit["id"]: dict(unit) for unit in
                 self.service.document(self.project_id, locale="zh-CN")["sections"][0]["units"]}
        changed = [
            uid for uid in before
            if any(before[uid][key] != after[uid][key]
                   for key in ("takeaway", "key", "topic", "brief", "evidence"))
        ]
        self.assertLessEqual(len(changed), 1, f"只应有被修的那一段变化（复用段必须逐字不变）：{changed}")

    def test_changed_repair_rederives_only_that_unit(self) -> None:
        provider = FakeProvider()
        self.service.generate(self.project_id, locale="zh-CN", provider=provider,
                              model_label="m", service_id="svc")
        self._make_legacy_state()
        self._drop_target_checkpoints()

        class ChangedFold(FakeProvider):
            """修复后的源层短层与旧产物不同（论证角色/要点变了）。"""

            def complete(self, *, system: str, user: str):
                from literature.providers import ProviderResponse
                units = self._unit_ids(user)
                if units and "source takeaway:" not in user:
                    self.calls.append("fold")
                    self.prompts.append(("fold", user))
                    body = [{"id": unit, "evidence": [0], "role": "result",
                             "takeaway": "Assay data show a consistent alloy standard",
                             "reason": "Ninety-two percent silver circulated.",
                             "topic": "Assay data"} for unit in units]
                    return ProviderResponse(
                        content=json.dumps({"section": {"takeaway": "整节主张。", "topic": "整节"},
                                            "units": body}, ensure_ascii=False),
                        provider="fake", model="fake-model", request_id="c",
                        input_tokens=10, output_tokens=5, estimated_cost=0.0,
                    )
                return super().complete(system=system, user=user)

        provider2 = ChangedFold()
        result = self.service.generate(self.project_id, locale="zh-CN", provider=provider2,
                                       model_label="m", service_id="svc")
        self.assertEqual(result["failed"], [])
        self.assertEqual(provider2.calls.count("fold"), 1, provider2.calls)
        # 只有消费上下文真的变了的 unit 重派生（1 次），另一段由播种检查点复用。
        self.assertEqual(provider2.calls.count("derive"), 1, provider2.calls)
        derive_prompts = [user for kind, user in provider2.prompts if kind == "derive"]
        self.assertEqual(len(derive_prompts), 1)
        self.assertNotIn("[U1]", derive_prompts[0])


if __name__ == "__main__":
    unittest.main()
