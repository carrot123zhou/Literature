"""可阅读 / 全部完成分离契约（冻结版：tmp/reader-partial-contract.md，2026-10-27）。

冻结契约要点（全部零付费、临时 runtime/library、Keychain 只用替身）：
- run 快照 additive 新增 `readable: boolean` 与 `availability`；旧字段（ready/status/...）
  语义不变。`readable = availability.units_valid >= 1`，**不按 status 强制 false**——
  running/pausing/paused/failed 都可 readable；`ready` 仍严格「全部完成」。
- availability 槽位口径：每 unit 在源/目标 lane 各一槽位；
  `units_total = units_valid + units_pending + units_suppressed` 恒成立；source 语 locale
  无目标 lane（target_* = 0）。无译文槽位走 source-fallback，不计 target pending。
- pending 标记 lane 固定：unit 顶层 `lit_pending` / chunk 顶层 `lit_summary_pending` 恒指
  **原文 lane**；`tr[locale].lit_pending` / `tr[locale].lit_summary_pending` 恒指
  **目标 lane**，不随 payload.locale 变形。
- native 根 `lit`：{partial, readable, full_text, pending_units, pending_summaries,
  rebuilt, checked_at, facts_fingerprint, availability}；`lit.readable` 与 run 同判据。
- GET /reader/layers/native：文件缺失也可从「当前事实 + 校验通过的缓存/检查点」安全重建
  （200，rebuilt=true）；仅项目无 document 时 404。事实变化后离开校验的旧短层一律丢弃，
  Full 恒为当前事实层全文——不复活过期短层。
- 全部失败：readable=false（units_valid=0）、full_text=true（普通全文可读）、不假称 AI 就绪；
  派生失败：中文全文不回退英文。源/派生/摘要独立降级；重试 = run resume；成功缓存 0 新调用。

后端落地前，冻结字段断言失败是预期信号；冻结后最终运行必须全部通过（不得删断言假绿）。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from literature.models import Block, Document, Page
from literature.reader import ReaderJobRunner, ReaderLayerService, ReaderStore
from literature.storage import ProjectStore
from tests.test_reader_fold_run import (
    derive_reply_for,
    make_document,
    make_translation,
    reply_for,
    wait_for,
)


# ---------------------------------------------------------------- fixture ----

def make_partial_document(project_id: str) -> Document:
    """三节文档：S1 Introduction（2 段）/ S2 Conclusion（列表 + 公式）/ S3 Epilogue（1 段）。"""
    base = make_document(project_id)
    blocks = list(base.blocks)
    blocks.append(Block(block_id="b-title-3", type="title", order=7, page_index=1,
                        source_text="Epilogue", level=1, bbox=[10, 10, 300, 40]))
    blocks.append(Block(
        block_id="b-p-3", type="paragraph", order=8, page_index=1, bbox=[10, 50, 300, 200],
        source_text="Silver hoards faded from the coastal trade. Later excavations found only traces of the old routes.",
    ))
    return Document(project_id=project_id, title=base.title, source_language="en",
                    page_count=base.page_count, pages=list(base.pages), blocks=blocks)


SUMMARY_MARKERS = (
    "unit takeaways in reading order",
    "condensed summaries of consecutive groups",
)

AVAILABILITY_KEYS = (
    "full_text",
    "units_total", "units_valid", "units_pending", "units_suppressed",
    "source_units_total", "source_units_valid", "source_units_pending", "source_units_suppressed",
    "target_units_total", "target_units_valid", "target_units_pending", "target_units_suppressed",
    "summaries_total", "summaries_pending", "source_summaries_pending", "target_summaries_pending",
    "basis", "checked_at",
)

LIT_KEYS = (
    "partial", "readable", "full_text", "pending_units", "pending_summaries",
    "rebuilt", "checked_at", "facts_fingerprint", "availability",
)


class PartialProvider:
    """source / derive / summary 三类请求可分别注入失败；可省略回复中的 section 摘要
    （迫使「章节摘要缺失」路径）；首个请求可被 gate 阻塞以观察在途暂停边界。

    识别：derive 提示词含 "source takeaway:"；章节摘要提示词含 SUMMARY_MARKERS 之一；
    其余为 source 折叠。节识别沿用 ScriptedProvider 规则（This section: N / 标题）。"""

    def __init__(
        self,
        *,
        fail_source: set[str] | None = None,
        fail_derive: set[str] | None = None,
        fail_summary: set[str] | None = None,
        omit_section: set[str] | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        import re as _re
        self._re = _re
        self.calls: list[tuple[str, str]] = []
        self.fail_source = set(fail_source or ())
        self.fail_derive = set(fail_derive or ())
        self.fail_summary = set(fail_summary or ())
        self.omit_section = set(omit_section or ())
        self.gate = gate
        from literature.providers import MockProvider
        self.config = MockProvider().config

    def _section_of(self, user: str) -> str:
        match = self._re.search(r"This section: (\d+)", user)
        if match:
            return f"S{match.group(1)}"
        if "Introduction" in user:
            return "S1"
        if "Conclusion" in user:
            return "S2"
        if "Epilogue" in user:
            return "S3"
        return "?"

    def complete(self, *, system: str, user: str) -> object:
        from literature.providers import ProviderResponse
        if "source takeaway:" in user:
            kind = "derive"
        elif any(marker in user for marker in SUMMARY_MARKERS):
            kind = "summary"
        else:
            kind = "source"
        section = self._section_of(user)
        self.calls.append((kind, section))
        if self.gate is not None and len(self.calls) == 1:
            # 模拟在途请求：等待测试侧先发暂停，再放行（请求边界暂停契约）。
            self.gate.wait(timeout=30)
        if kind == "source" and section in self.fail_source:
            raise RuntimeError(f"模拟 {section} 原文折叠失败")
        if kind == "derive" and section in self.fail_derive:
            raise RuntimeError(f"模拟 {section} 目标派生失败")
        if kind == "summary" and section in self.fail_summary:
            raise RuntimeError(f"模拟 {section} 章节摘要失败")
        payload = derive_reply_for(user) if kind == "derive" else reply_for(user)
        if kind != "summary" and section in self.omit_section:
            payload = {"units": payload.get("units") or []}
        return ProviderResponse(
            content=json.dumps(payload, ensure_ascii=False), provider="fake", model="fake-model",
            request_id=f"req-{len(self.calls)}", input_tokens=100, output_tokens=50, estimated_cost=0.001,
        )


def calls_of(provider: PartialProvider, kind: str, section: str) -> int:
    return len([item for item in provider.calls if item[0] == kind and item[1] == section])


def effective_lad(unit: dict, locale: str = "source") -> dict | None:
    """unit 的展示层短层：原文层看 lad；译文层看 tr[locale].lad。
    无译文槽位（tr[locale] 缺失）沿 source-fallback 看原文层；有译文槽位但派生
    未生成 → None（pending），不借用原文短层冒充目标已生成。"""
    if locale != "source":
        tr = (unit.get("tr") or {}).get(locale)
        if tr is None:
            return unit.get("lad")
        lad = tr.get("lad")
        if lad and (lad.get("brief") or lad.get("topic")):
            return lad
        return None
    return unit.get("lad")


def chunk_of(native: dict, chunk_id: str) -> dict | None:
    return next((c for c in native.get("chunks") or [] if c.get("id") == chunk_id), None)


def pending_units_of(native: dict, chunk_id: str, locale: str = "source") -> list[dict]:
    """某节里「未生成」的 unit：无有效短层（lad 缺失或 brief/topic 为空）。"""
    chunk = chunk_of(native, chunk_id)
    if chunk is None:
        return []
    out = []
    for uid in chunk.get("units") or []:
        unit = (native.get("units") or {}).get(uid) or {}
        lad = effective_lad(unit, locale)
        if not lad or not (lad.get("brief") or lad.get("topic")):
            out.append(unit)
    return out


def model_units_of(native: dict, chunk_id: str, locale: str = "source") -> list[dict]:
    chunk = chunk_of(native, chunk_id)
    if chunk is None:
        return []
    out = []
    for uid in chunk.get("units") or []:
        unit = (native.get("units") or {}).get(uid) or {}
        lad = effective_lad(unit, locale)
        if lad and (lad.get("brief") or lad.get("topic")):
            out.append(unit)
    return out


def plain_text(native: dict, toks: list) -> str:
    from third_party.textfold import tokens as tf_tokens
    return tf_tokens.plain(toks or [], native.get("atoms") or {})


def unit_full_text(native: dict, unit: dict, locale: str = "source") -> str:
    if locale != "source":
        tr = (unit.get("tr") or {}).get(locale) or {}
        if tr.get("toks"):
            return plain_text(native, tr.get("toks"))
    return plain_text(native, unit.get("toks"))


# ------------------------------------------------------------ 契约断言工具 ----

def assert_availability_shape(testcase: unittest.TestCase, avail) -> dict:
    testcase.assertIsInstance(avail, dict, "run 快照缺少冻结字段 availability")
    for key in AVAILABILITY_KEYS:
        testcase.assertIn(key, avail, f"availability 缺少 {key}: {avail}")
    testcase.assertIsInstance(avail["full_text"], bool)
    testcase.assertIsInstance(avail["basis"], str)
    testcase.assertIsInstance(avail["checked_at"], str)
    int_keys = [k for k in AVAILABILITY_KEYS if k not in ("full_text", "basis", "checked_at")]
    for key in int_keys:
        testcase.assertIsInstance(avail[key], int, f"availability.{key} 应为整数: {avail}")
    # 冻结恒等式：units_total = units_valid + units_pending + units_suppressed。
    testcase.assertEqual(
        avail["units_total"],
        avail["units_valid"] + avail["units_pending"] + avail["units_suppressed"],
        f"槽位恒等式不成立: {avail}",
    )
    testcase.assertEqual(
        avail["target_units_total"],
        avail["target_units_valid"] + avail["target_units_pending"] + avail["target_units_suppressed"],
        f"target 槽位恒等式不成立: {avail}",
    )
    return avail


def assert_readable_matches(testcase: unittest.TestCase, readable, avail: dict) -> None:
    testcase.assertIsInstance(readable, bool, f"readable 应为布尔: {readable!r}")
    testcase.assertEqual(readable, avail["units_valid"] >= 1,
                         f"readable 必须等于 units_valid>=1: readable={readable}, {avail}")


def assert_lit_shape(testcase: unittest.TestCase, native: dict) -> dict:
    lit = native.get("lit")
    testcase.assertIsInstance(lit, dict, "native 缺少冻结字段 lit")
    for key in LIT_KEYS:
        testcase.assertIn(key, lit, f"lit 缺少 {key}: {lit.keys()}")
    for key in ("partial", "readable", "full_text"):
        testcase.assertIsInstance(lit[key], bool, f"lit.{key} 应为布尔: {lit}")
    avail = assert_availability_shape(testcase, lit.get("availability"))
    assert_readable_matches(testcase, lit["readable"], avail)
    testcase.assertEqual(int(lit["pending_units"]), avail["units_pending"],
                         f"lit.pending_units 必须等于 availability.units_pending: {lit}")
    testcase.assertEqual(int(lit["pending_summaries"]), avail["summaries_pending"],
                         f"lit.pending_summaries 必须等于 availability.summaries_pending: {lit}")
    return lit


# ------------------------------------------------- runner 级（service 层）----

class PartialFoldRunnerContract(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% partial fixture")
        project = self.store.create_project(source, title="Silk Road Silver")
        self.project_id = project["project_id"]
        document = make_partial_document(self.project_id)
        self.store.save_document(document)
        self.store.save_translation(make_translation(document))
        self.runner = ReaderJobRunner(self.store)
        self.reader = ReaderStore(self.store)
        self.service = ReaderLayerService(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _facts_digests(self) -> dict[str, str]:
        base = self.store.project_path(self.project_id)
        values = {}
        for rel in ("document/document.json", "translations/zh-CN/translation.json"):
            path = base / rel
            if path.is_file():
                values[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        return values

    def _start(self, provider: PartialProvider, locale: str = "zh-CN") -> None:
        self.runner.start_run(
            self.project_id, locale=locale, provider_factory=lambda: provider,
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )

    # 1. 少量源失败：pending + 全文保留 + ready 严格 + resume 只补 ------------

    def test_source_failure_keeps_full_text_and_ready_stays_strict(self) -> None:
        facts_before = self._facts_digests()
        provider = PartialProvider(fail_source={"S2"})
        self._start(provider)
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "failed", final)
        self.assertFalse(final.get("ready"))
        self.assertIn("S2", [item.get("section_id") for item in final.get("errors") or []], final)
        # readable 不绑 status：部分失败仍有已验证成果可读。
        avail = assert_availability_shape(self, final.get("availability"))
        assert_readable_matches(self, final.get("readable"), avail)
        self.assertTrue(final.get("readable"), final)
        self.assertTrue(avail["full_text"])
        self.assertGreaterEqual(avail["source_units_pending"], 1, avail)

        # readiness：S2 未就绪（pending 不能冒充 AI 完成），S1/S3 就绪。
        readiness = self.service.fold_run_readiness(
            self.project_id, locale="zh-CN", service_id="svc", model="fake-model")
        by_id = {item["section_id"]: item for item in readiness["sections"]}
        self.assertTrue(readiness["sections"], readiness)
        self.assertFalse(readiness["ready"])
        self.assertFalse(by_id["S2"]["ready"])
        self.assertIn("未折叠", by_id["S2"]["reason"])
        for sid in ("S1", "S3"):
            self.assertTrue(by_id[sid]["ready"], by_id[sid])

        # 源层：S2 unit 全部 pending（空短层），全文 toks 仍在。
        native_src = self.reader.load_native(self.project_id, "source")
        self.assertIsNotNone(native_src)
        pend = pending_units_of(native_src, "S2")
        self.assertTrue(pend, "S2 应存在未生成 unit")
        full_text = "".join(unit_full_text(native_src, unit) for unit in pend)
        self.assertIn("Hoards cluster in port cities.", full_text)

        # resume 修复后：S1/S3 缓存 0 新调用，全部就绪。
        provider.fail_source.clear()
        before = len(provider.calls)
        s1_source, s3_source = calls_of(provider, "source", "S1"), calls_of(provider, "source", "S3")
        self.runner.resume_run(self.project_id, final["run_id"], provider_factory=lambda: provider)
        done = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(done["status"], "ready", done)
        self.assertEqual(calls_of(provider, "source", "S1"), s1_source)
        self.assertEqual(calls_of(provider, "source", "S3"), s3_source)
        self.assertGreater(len(provider.calls), before)

        # 事实层不变：document/translation 字节未动。
        self.assertEqual(self._facts_digests(), facts_before)

    # 2. 仅目标派生失败：源/派生独立降级 + 中文全文不回退英文 ----------------

    def test_derive_failure_language_and_layer_independence(self) -> None:
        provider = PartialProvider(fail_derive={"S2"})
        self._start(provider)
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "failed", final)
        avail = assert_availability_shape(self, final.get("availability"))
        assert_readable_matches(self, final.get("readable"), avail)
        self.assertTrue(final.get("readable"), final)
        self.assertEqual(avail["source_units_pending"], 0, avail)
        self.assertGreaterEqual(avail["target_units_pending"], 1, avail)

        # 源层已完整（源/派生独立）：source readiness 全就绪。
        src_readiness = self.service.fold_run_readiness(
            self.project_id, locale="source", service_id="svc", model="fake-model")
        self.assertTrue(src_readiness["ready"], src_readiness)

        # 目标层：S2 派生未就绪，其余就绪。
        readiness = self.service.fold_run_readiness(
            self.project_id, locale="zh-CN", service_id="svc", model="fake-model")
        by_id = {item["section_id"]: item for item in readiness["sections"]}
        self.assertFalse(readiness["ready"])
        self.assertFalse(by_id["S2"]["ready"])
        self.assertIn("派生未就绪", by_id["S2"]["reason"])
        for sid in ("S1", "S3"):
            self.assertTrue(by_id[sid]["ready"], by_id[sid])

        # 中文全文来自 translation.json，不回退英文：S2 pending unit 的 tr toks 是译文。
        native_zh = self.reader.load_native(self.project_id, "zh-CN")
        self.assertIsNotNone(native_zh)
        pend = pending_units_of(native_zh, "S2", locale="zh-CN")
        self.assertTrue(pend, "S2 派生失败应存在未生成 unit")
        for unit in pend:
            text = unit_full_text(native_zh, unit, locale="zh-CN")
            self.assertIn("[译]", text, f"S2 全文投影必须是中文译文，实际：{text[:80]}")
            self.assertNotIn("Silver vessels", text)
        # 已派生节的中文短层正常（对照）。
        self.assertTrue(model_units_of(native_zh, "S3", locale="zh-CN"),
                        "S3 派生成功应有有效短层")

        # resume 补齐后 ready，S2 源层 0 新调用（源层独立、不重折）。
        provider.fail_derive.clear()
        s2_source = calls_of(provider, "source", "S2")
        self.runner.resume_run(self.project_id, final["run_id"], provider_factory=lambda: provider)
        done = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(done["status"], "ready", done)
        self.assertEqual(calls_of(provider, "source", "S2"), s2_source)

    # 3. 仅章节摘要失败：unit 短层保留，resume 只补摘要 ---------------------

    def test_summary_failure_resume_only_refills_summary(self) -> None:
        provider = PartialProvider(omit_section={"S1"}, fail_summary={"S1"})
        self._start(provider, locale="source")
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "failed", final)
        avail = assert_availability_shape(self, final.get("availability"))
        self.assertGreaterEqual(avail["source_summaries_pending"], 1, avail)

        readiness = self.service.fold_run_readiness(
            self.project_id, locale="source", service_id="svc", model="fake-model")
        by_id = {item["section_id"]: item for item in readiness["sections"]}
        self.assertFalse(by_id["S1"]["ready"], by_id)
        self.assertIn("章节摘要", by_id["S1"]["reason"])

        # unit 短层仍在（只有摘要缺）：S1 unit 投影有有效 brief。
        native_src = self.reader.load_native(self.project_id, "source")
        self.assertTrue(model_units_of(native_src, "S1"), "unit 短层不应因摘要失败丢失")

        # resume：只补摘要（S1 无新 source 调用），ready 后章节短层写入。
        provider.fail_summary.clear()
        s1_source, s1_summary = calls_of(provider, "source", "S1"), calls_of(provider, "summary", "S1")
        self.runner.resume_run(self.project_id, final["run_id"], provider_factory=lambda: provider)
        done = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(done["status"], "ready", done)
        self.assertEqual(calls_of(provider, "source", "S1"), s1_source, "resume 不应重折 unit")
        self.assertEqual(calls_of(provider, "summary", "S1"), s1_summary + 1)
        native_after = self.reader.load_native(self.project_id, "source")
        chunk = chunk_of(native_after, "S1")
        self.assertIsNotNone(chunk)
        self.assertTrue(chunk.get("lad"), "resume 后章节摘要应写入")

    # 4. 事实变化：旧短层不得复活，未变 unit 保留，resume 精准补 ------------

    def test_stale_facts_old_short_layers_not_resurrected(self) -> None:
        """逐 unit 冻结语义：fixture 只改 b-p-1 → b-p-1 源/目标 pending 且短层空；
        b-p-2 内容与实际消费上下文未变（_unit_context_text 刻意不含整节 digest）
        → 逐 unit 检查点仍有效，短层逐字保留（不重复计费）；S1 章节摘要清空，
        S2/S3 摘要保留。用检查点 fp 与短层逐字对照证明 b-p-2 未变。"""
        provider = PartialProvider()
        self._start(provider)
        ready = wait_for(self.runner, self.project_id, ("ready",))
        self.assertTrue(ready["ready"])

        # 预变更基线：b-p-2 原短层（安全投影）+ 检查点 fp。
        baseline = self.service.reading_native(self.project_id, locale="zh-CN")
        self.assertIsNotNone(baseline)
        baseline_units = baseline.get("units") or {}
        b2_before = ((((baseline_units.get("b-p-2") or {}).get("tr") or {}).get("zh-CN")
                      or {}).get("lad") or {}).get("brief")
        self.assertTrue(b2_before, "预变更基线：b-p-2 应有有效短层")
        ckpt_path = self.store.project_path(self.project_id) / "reader" / "fold_checkpoints.json"
        ckpt_before = ""
        if ckpt_path.is_file():
            raw = json.loads(ckpt_path.read_text("utf-8"))
            ckpt_before = str((((raw.get("sections") or {}).get("zh-CN") or {})
                               .get("S1") or {}).get("units", {}).get("b-p-2", {}).get("fp") or "")
        self.assertTrue(ckpt_before, "预变更基线：b-p-2 应有逐 unit 检查点 fp")

        # 事实变化：只改 b-p-1 正文（b-p-2/S2/S3 不动）。
        document = self.store.load_document(self.project_id)
        for block in document.blocks:
            if block.block_id == "b-p-1":
                block.source_text = (
                    "Silver vessels travelled along the Silk Road. Workshops copied shapes "
                    "for decades. Demand fell after the ninth century. New evidence emerged later on."
                )
        self.store.save_document(document)

        # ready 诚实失效（failed/partial 非 ready 也做事实变更检查）。
        invalidated = self.runner.current_run(self.project_id)
        self.assertEqual(invalidated["status"], "failed", invalidated)
        self.assertFalse(invalidated.get("ready"))
        # readable 不绑 status：S2/S3 未变仍有效 → 仍可读。
        avail = assert_availability_shape(self, invalidated.get("availability"))
        assert_readable_matches(self, invalidated.get("readable"), avail)

        # 安全投影（权威面 = reading_native，与 GET /reader/layers/native 同一入口）：
        # 逐 unit 语义——S1 章节摘要清空；b-p-1（真过期）源/目标 pending 且短层空；
        # b-p-2（未变）短层逐字保留且检查点 fp 不变；S2/S3 摘要保留；新全文进入投影。
        native = self.service.reading_native(self.project_id, locale="zh-CN")
        self.assertIsNotNone(native)
        lit = native.get("lit") or {}
        self.assertIs(lit.get("rebuilt"), True, lit)
        units = native.get("units") or {}
        s1 = chunk_of(native, "S1")
        s2 = chunk_of(native, "S2")
        s3 = chunk_of(native, "S3")
        s1_take = (((s1.get("tr") or {}).get("zh-CN") or {}).get("lad") or {}).get("take")
        self.assertFalse(s1_take, "S1 过期章节摘要应清空（安全投影丢弃）")
        for sid in ("S2", "S3"):
            take = (((chunk_of(native, sid).get("tr") or {}).get("zh-CN") or {})
                    .get("lad") or {}).get("take")
            self.assertTrue(take, f"未变化节 {sid} 章节摘要应保留")
        b1 = (units.get("b-p-1") or {}).get("tr", {}).get("zh-CN") or {}
        self.assertTrue(b1.get("lit_pending"), "真过期 b-p-1 应重置为待生成")
        self.assertFalse((b1.get("lad") or {}).get("brief"), "真过期 b-p-1 短层应为空")
        b2 = (units.get("b-p-2") or {}).get("tr", {}).get("zh-CN") or {}
        self.assertFalse(b2.get("lit_pending"), "未变 b-p-2 不得标 pending")
        self.assertEqual(b2.get("lad", {}).get("brief"), b2_before,
                         "未变 b-p-2 短层应逐字保留（匹配原检查点）")
        if ckpt_path.is_file():
            raw = json.loads(ckpt_path.read_text("utf-8"))
            ckpt_after = str((((raw.get("sections") or {}).get("zh-CN") or {})
                              .get("S1") or {}).get("units", {}).get("b-p-2", {}).get("fp") or "")
            self.assertEqual(ckpt_after, ckpt_before,
                             "未变 b-p-2 的检查点 fp 不应变化（消费上下文未变）")
        native_blob = json.dumps(native, ensure_ascii=False)
        self.assertIn("New evidence emerged later on.", native_blob, "全文投影应反映当前事实")

        # 读侧 view（document()）：stale 标志在位（S3 保留性由上面 native 面断言）。
        view = self.service.document(self.project_id, locale="zh-CN")
        self.assertTrue(view.get("stale"), view.get("stale"))
        sections = {item["id"]: item for item in view.get("sections") or []}
        self.assertTrue(any(u.get("brief") or u.get("brief_text") for u in sections["S3"]["units"]),
                        "未变化的 S3 短层应保留（读侧扁平投影）")
        s3_source, s3_derive = calls_of(provider, "source", "S3"), calls_of(provider, "derive", "S3")
        self.runner.resume_run(self.project_id, ready["run_id"], provider_factory=lambda: provider)
        done = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(done["status"], "ready", done)
        self.assertEqual(calls_of(provider, "source", "S3"), s3_source)
        self.assertEqual(calls_of(provider, "derive", "S3"), s3_derive)
        view2 = self.service.document(self.project_id, locale="zh-CN")
        self.assertFalse(view2.get("stale"))
        native_src = self.reader.load_native(self.project_id, "source")
        s1_units = model_units_of(native_src, "S1") + pending_units_of(native_src, "S1")
        s1_text = "".join(unit_full_text(native_src, u) for u in s1_units)
        self.assertIn("New evidence emerged later on.", s1_text, "全文投影应反映当前事实")


# ------------------------------------------------- API DTO 级（冻结面）------

class PartialRunApiContract(unittest.TestCase):
    """API 契约：/reader/fold-run 快照（readable/availability/ready）与
    /reader/layers/native 投影（lit / lit_pending=source / tr[locale].lit_pending=target）。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        os.environ["LITERATURE_RUNTIME"] = str(self.base / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.base / "library")
        os.environ["LITERATURE_SESSION_TOKEN"] = ""
        (self.base / "runtime").mkdir(parents=True, exist_ok=True)
        (self.base / "runtime/session-token").write_text("")
        import literature.model_services as ms
        self._ms = ms
        self._real_api_key = ms.ModelServiceStore.api_key
        self._real_save = getattr(ms, "save_service_key", None)
        ms.ModelServiceStore.api_key = lambda store, sid: "partial-not-a-real-key"
        if self._real_save is not None:
            ms.save_service_key = lambda *a, **k: None
        self.store = ProjectStore(self.base / "library")

    def tearDown(self) -> None:
        self._ms.ModelServiceStore.api_key = self._real_api_key
        if self._real_save is not None:
            self._ms.save_service_key = self._real_save
        os.environ.pop("LITERATURE_RUNTIME", None)
        os.environ.pop("LITERATURE_LIBRARY", None)
        os.environ.pop("LITERATURE_SESSION_TOKEN", None)
        self.temporary.cleanup()

    def _make_project(self, title: str) -> str:
        source = self.base / f"{title}.pdf"
        source.write_bytes(b"%PDF-1.4\n% partial api fixture")
        project = self.store.create_project(source, title=title)
        pid = str(project["project_id"])
        document = make_partial_document(pid)
        self.store.save_document(document)
        self.store.save_translation(make_translation(document))
        return pid

    def _app(self, provider: PartialProvider):
        from fastapi.testclient import TestClient
        from literature.api import create_app
        app = create_app(self.store)
        app.state.job_manager.provider_from = lambda *a, **k: provider
        return app, TestClient(app)

    def _bind_service(self, client, app) -> None:
        headers = {"x-literature-token": app.state.session_token}
        svc = client.post("/model-services", json={
            "name": "partial-fake", "preset": "custom",
            "base_url": "http://127.0.0.1:1/v1", "models": [{"model": "fake-model"}],
        }, headers=headers)
        assert svc.status_code == 200, svc.text
        roles = client.put("/model-services/roles", json={
            "reader_fold": {"service_id": svc.json()["service_id"], "model": "fake-model"},
        }, headers=headers)
        assert roles.status_code == 200, roles.text

    def _start(self, client, pid: str, app, locale: str = "zh-CN") -> dict:
        headers = {"x-literature-token": app.state.session_token}
        run = client.post(f"/projects/{pid}/reader/fold-runs", json={"locale": locale}, headers=headers)
        assert run.status_code == 200, run.text
        return run.json()["run"]

    def _snapshot(self, client, pid: str, app) -> dict:
        headers = {"x-literature-token": app.state.session_token}
        snap: dict = {}
        for _ in range(600):
            snap = client.get(f"/projects/{pid}/reader/fold-run", headers=headers).json()
            if snap.get("status") in ("ready", "failed"):
                return snap
            time.sleep(0.05)
        raise AssertionError(f"run 未在时限内结束：{snap.get('status')}")

    def _native(self, client, pid: str, app, locale: str = "zh-CN"):
        headers = {"x-literature-token": app.state.session_token}
        return client.get(f"/projects/{pid}/reader/layers/native",
                          params={"locale": locale}, headers=headers)

    # A. 少量源失败：readable + availability + lit_pending（source lane）---

    def test_partial_source_failure_snapshot_and_native_contract(self) -> None:
        pid = self._make_project("PartialSrc")
        provider = PartialProvider(fail_source={"S2"})
        app, client = self._app(provider)
        self._bind_service(client, app)
        self._start(client, pid, app)
        snap = self._snapshot(client, pid, app)
        self.assertEqual(snap["status"], "failed", snap)
        self.assertFalse(snap.get("ready"), snap)
        self.assertIn("S2", [item.get("section_id") for item in snap.get("errors") or []], snap)
        avail = assert_availability_shape(self, snap.get("availability"))
        assert_readable_matches(self, snap.get("readable"), avail)
        self.assertIs(snap.get("readable"), True, snap)
        self.assertTrue(avail["full_text"])
        self.assertGreaterEqual(avail["source_units_pending"], 1, avail)
        self.assertGreaterEqual(avail["source_summaries_pending"], 1, avail)

        native = self._native(client, pid, app, locale="source").json()
        lit = assert_lit_shape(self, native)
        self.assertTrue(lit["readable"], lit)
        self.assertTrue(lit["full_text"], lit)
        self.assertTrue(lit["partial"], lit)

        # lit_pending 恒指 source lane：S2 未生成 unit 顶层 lit_pending=True。
        pend = pending_units_of(native, "S2")
        self.assertTrue(pend)
        for unit in pend:
            self.assertIs(unit.get("lit_pending"), True,
                          f"source pending unit 顶层缺 lit_pending=True：{unit.get('id')}")
        for unit in model_units_of(native, "S1"):
            self.assertIs(unit.get("lit_pending"), False, "已生成 unit 顶层 lit_pending 应为 False")
        # 全文保留：pending unit 的 toks 仍携带原句（5 层全文可见的数据基础）。
        self.assertIn("Hoards cluster in port cities.",
                      "".join(unit_full_text(native, u) for u in pend))
        # 章节摘要 pending：S2 顶层 lit_summary_pending=True（source 摘要缺）。
        chunk_s2 = chunk_of(native, "S2")
        self.assertIsNotNone(chunk_s2)
        self.assertIs(chunk_s2.get("lit_summary_pending"), True, "摘要缺失的节顶层缺 lit_summary_pending")
        chunk_s1 = chunk_of(native, "S1")
        self.assertIsNotNone(chunk_s1)
        self.assertIs(chunk_s1.get("lit_summary_pending"), False, "已就绪节顶层 lit_summary_pending 应为 False")

    # B. 仅目标派生失败：tr[locale] 恒 target、不随 payload.locale 改变 ------

    def test_derive_failure_target_pending_marks_chinese_full_text(self) -> None:
        pid = self._make_project("PartialDerive")
        provider = PartialProvider(fail_derive={"S2"})
        app, client = self._app(provider)
        self._bind_service(client, app)
        self._start(client, pid, app)
        snap = self._snapshot(client, pid, app)
        self.assertEqual(snap["status"], "failed", snap)
        avail = assert_availability_shape(self, snap.get("availability"))
        self.assertEqual(avail["source_units_pending"], 0, avail)
        self.assertGreaterEqual(avail["target_units_pending"], 1, avail)

        native = self._native(client, pid, app, locale="zh-CN").json()
        assert_lit_shape(self, native)
        # S2：源层已折好 → 顶层 lit_pending/lit_summary_pending 必须为 False；
        # 目标派生缺 → tr["zh-CN"].lit_pending / lit_summary_pending = True。
        chunk = chunk_of(native, "S2")
        self.assertIsNotNone(chunk)
        self.assertIs(chunk.get("lit_summary_pending"), False,
                      "lit_summary_pending 恒指 source：源层已折好不得标 pending")
        tr_chunk = (chunk.get("tr") or {}).get("zh-CN") or {}
        self.assertIs(tr_chunk.get("lit_summary_pending"), True,
                      "target 摘要缺应有 tr[zh-CN].lit_summary_pending=True")
        target_pending = 0
        for uid in chunk.get("units") or []:
            unit = (native.get("units") or {}).get(uid) or {}
            self.assertIs(unit.get("lit_pending"), False,
                          "lit_pending 恒指 source lane，不得随 payload.locale 改变")
            tr = (unit.get("tr") or {}).get("zh-CN") or {}
            if tr.get("lit_pending"):
                target_pending += 1
                text = plain_text(native, tr.get("toks"))
                self.assertIn("[译]", text, f"target 全文投影必须是中文译文，实际：{text[:80]}")
                self.assertNotIn("Silver vessels", text, "派生失败不得回退英文全文")
        self.assertGreaterEqual(target_pending, 1, "S2 应存在 target lit_pending unit")
        # 已派生节短层正常（对照）。
        self.assertTrue(model_units_of(native, "S3", locale="zh-CN"), "S3 应有有效中文短层")

        # 同一事实的 source locale native：S2 源 lane 全部有效（lit_pending=False）。
        native_src = self._native(client, pid, app, locale="source").json()
        chunk_src = chunk_of(native_src, "S2")
        self.assertIsNotNone(chunk_src)
        self.assertIs(chunk_src.get("lit_summary_pending"), False, chunk_src)

    # C. 全部失败：readable=false、不假称 AI 就绪、普通全文仍可读 ----------

    def test_all_failed_readable_false_and_honest(self) -> None:
        pid = self._make_project("PartialAll")
        provider = PartialProvider(fail_source={"S1", "S2", "S3"})
        app, client = self._app(provider)
        self._bind_service(client, app)
        self._start(client, pid, app)
        snap = self._snapshot(client, pid, app)
        self.assertEqual(snap["status"], "failed", snap)
        avail = assert_availability_shape(self, snap.get("availability"))
        assert_readable_matches(self, snap.get("readable"), avail)
        self.assertIs(snap.get("readable"), False, "全部失败（units_valid=0）不得声称可阅读")
        self.assertFalse(snap.get("ready"), snap)
        # 普通全文仍可读：full_text=true；全部槽位 pending。
        self.assertTrue(avail["full_text"], avail)
        self.assertGreater(avail["units_pending"], 0, avail)
        self.assertEqual(avail["units_valid"], 0, avail)

        base = self.store.project_path(pid)
        document_hash = hashlib.sha256((base / "document/document.json").read_bytes()).hexdigest()
        resp = self._native(client, pid, app, locale="source")
        self.assertEqual(resp.status_code, 200, resp.text)
        native = resp.json()
        lit = assert_lit_shape(self, native)
        self.assertFalse(lit["readable"], lit)
        self.assertTrue(lit["full_text"], lit)
        full_text = "".join(unit_full_text(native, u) for u in (native.get("units") or {}).values())
        self.assertIn("Silver vessels travelled along the Silk Road.", full_text)
        self.assertEqual(
            hashlib.sha256((base / "document/document.json").read_bytes()).hexdigest(),
            document_hash,
        )

    # D. 从未运行：GET native 安全重建 200（不再 404；无 document 才 404） --

    def test_native_rebuild_without_any_run(self) -> None:
        pid = self._make_project("PartialFresh")
        provider = PartialProvider()
        app, client = self._app(provider)
        headers = {"x-literature-token": app.state.session_token}
        idle = client.get(f"/projects/{pid}/reader/fold-run", headers=headers).json()
        self.assertEqual(idle.get("status"), "idle", idle)
        resp = self._native(client, pid, app, locale="zh-CN")
        self.assertEqual(resp.status_code, 200, resp.text)
        native = resp.json()
        lit = assert_lit_shape(self, native)
        self.assertFalse(lit["readable"], "从未生成（units_valid=0）不得声称可阅读")
        self.assertTrue(lit["full_text"], "从未生成也应可读普通全文")
        self.assertIs(lit.get("rebuilt"), True, lit)
        # 全部未抑制 unit 顶层 lit_pending=True + 中文全文投影（来自 translation.json）。
        units = native.get("units") or {}
        self.assertTrue(units)
        for unit in units.values():
            if unit.get("suppressed"):
                continue
            self.assertIs(unit.get("lit_pending"), True,
                          f"从未生成 unit 应标顶层 lit_pending：{unit.get('id')}")
        some_text = "".join(
            plain_text(native, ((u.get("tr") or {}).get("zh-CN") or {}).get("toks") or [])
            for u in units.values() if not u.get("suppressed")
        )
        self.assertIn("[译]", some_text, "zh native 全文投影应来自译文层")

    # E. 暂停 → 恢复 → 重启恢复 → 项目隔离 ---------------------------------

    def test_pause_resume_recover_and_project_isolation(self) -> None:
        pid_a = self._make_project("PartialPauseA")
        pid_b = self._make_project("PartialPauseB")
        provider = PartialProvider(gate=threading.Event())
        app, client = self._app(provider)
        self._bind_service(client, app)
        headers = {"x-literature-token": app.state.session_token}
        self._start(client, pid_a, app)
        # 在途请求期间发暂停。
        for _ in range(100):
            if provider.calls:
                break
            time.sleep(0.05)
        self.assertTrue(provider.calls, "provider 未收到请求")
        snap = client.get(f"/projects/{pid_a}/reader/fold-run", headers=headers).json()
        run_id = str(snap.get("run_id") or "")
        self.assertTrue(run_id, snap)
        pause = client.post(f"/projects/{pid_a}/reader/fold-runs/{run_id}/pause", headers=headers)
        self.assertEqual(pause.status_code, 200, pause.text)
        provider.gate.set()
        for _ in range(600):
            snap = client.get(f"/projects/{pid_a}/reader/fold-run", headers=headers).json()
            if snap.get("status") == "paused":
                break
            time.sleep(0.05)
        self.assertEqual(snap.get("status"), "paused", snap)
        # readable 不绑 status：paused 且有已验证成果（在途请求已落盘/检查点）→ 可读。
        avail = assert_availability_shape(self, snap.get("availability"))
        assert_readable_matches(self, snap.get("readable"), avail)

        # 重启恢复：新 runner 实例从盘读取，recover() 不破坏 paused 状态。
        fresh_runner = ReaderJobRunner(self.store)
        fresh_runner.recover()
        after = fresh_runner.current_run(pid_a)
        self.assertEqual(after.get("status"), "paused", after)

        # 项目隔离：B 独立 idle。
        idle_b = client.get(f"/projects/{pid_b}/reader/fold-run", headers=headers).json()
        self.assertEqual(idle_b.get("status"), "idle", idle_b)

        # resume（重试沿 run resume）后 ready；成功节源折叠只发生一次。
        resume = client.post(f"/projects/{pid_a}/reader/fold-runs/{run_id}/resume", headers=headers)
        self.assertEqual(resume.status_code, 200, resume.text)
        done = self._snapshot(client, pid_a, app)
        self.assertEqual(done["status"], "ready", done)
        self.assertLessEqual(calls_of(provider, "source", "S1"), 1, provider.calls)

        # B 的 run 独立就绪，A 状态不被覆盖。
        self._start(client, pid_b, app)
        done_b = self._snapshot(client, pid_b, app)
        self.assertEqual(done_b["status"], "ready", done_b)
        snap_a = client.get(f"/projects/{pid_a}/reader/fold-run", headers=headers).json()
        self.assertEqual(snap_a.get("status"), "ready", snap_a)
        self.assertEqual(snap_a.get("run_id"), done.get("run_id"), snap_a)

    # F. 事实变化：GET native 安全重建，旧短层不得复活 ----------------------

    def test_stale_facts_native_safe_rebuild(self) -> None:
        pid = self._make_project("PartialStale")
        provider = PartialProvider()
        app, client = self._app(provider)
        self._bind_service(client, app)
        self._start(client, pid, app)
        ready = self._snapshot(client, pid, app)
        self.assertEqual(ready["status"], "ready", ready)
        # 预变更基线：b-p-2 原短层（逐 unit 语义下未变单元应保留）。
        baseline = self._native(client, pid, app, locale="zh-CN").json()
        b2_before = ((((baseline.get("units") or {}).get("b-p-2") or {}).get("tr") or {})
                     .get("zh-CN") or {}).get("lad", {}).get("brief")
        self.assertTrue(b2_before, "预变更基线：b-p-2 应有有效短层")
        old_brief = "本节总结白银传播。"
        headers = {"x-literature-token": app.state.session_token}

        document = self.store.load_document(pid)
        for block in document.blocks:
            if block.block_id == "b-p-1":
                block.source_text = (
                    "Silver vessels travelled along the Silk Road. Workshops copied shapes "
                    "for decades. Demand fell after the ninth century. New evidence emerged later on."
                )
        self.store.save_document(document)

        # ready 诚实失效（事实变更检查不绑 ready）。
        snap = client.get(f"/projects/{pid}/reader/fold-run", headers=headers).json()
        self.assertEqual(snap.get("status"), "failed", snap)
        self.assertFalse(snap.get("ready"), snap)

        # GET native 安全重建：S1 旧派生短层不得复活；S2/S3 未变短层保留；新全文进入投影。
        native = self._native(client, pid, app, locale="zh-CN").json()
        lit = assert_lit_shape(self, native)
        self.assertTrue(lit.get("rebuilt"), lit)
        # S2/S3 未变仍有效 → 安全投影存在，readable 应保持 True（独立于 status）。
        self.assertIs(lit["readable"], True, lit)
        # 逐 unit 定位（三节摘要同文，不能全局 assertNotIn 误伤 S2/S3）：
        # S1 章节摘要清空；真过期 b-p-1 短层空 + lit_pending；未变 b-p-2 短层逐字保留。
        units = native.get("units") or {}
        s1_chunk = chunk_of(native, "S1")
        self.assertIsNotNone(s1_chunk)
        s1_take = (((s1_chunk.get("tr") or {}).get("zh-CN") or {}).get("lad") or {}).get("take")
        self.assertFalse(s1_take, "S1 过期章节摘要应清空")
        for sid in ("S2", "S3"):
            take = (((chunk_of(native, sid).get("tr") or {}).get("zh-CN") or {})
                    .get("lad") or {}).get("take")
            self.assertTrue(take, f"未变化节 {sid} 章节摘要应保留")
        b1 = (units.get("b-p-1") or {}).get("tr", {}).get("zh-CN") or {}
        self.assertTrue(b1.get("lit_pending"), "真过期 b-p-1 应重置为待生成")
        self.assertFalse((b1.get("lad") or {}).get("brief"), "真过期 b-p-1 短层应为空")
        b2 = (units.get("b-p-2") or {}).get("tr", {}).get("zh-CN") or {}
        self.assertFalse(b2.get("lit_pending"), "未变 b-p-2 不得标 pending")
        self.assertEqual(b2.get("lad", {}).get("brief"), b2_before,
                         "未变 b-p-2 短层应逐字保留")
        native_blob = json.dumps(native, ensure_ascii=False)
        self.assertIn("New evidence emerged later on.", native_blob, "全文投影应反映当前事实")

        # resume：只补 S1，S2/S3 缓存 0 调用，回到 ready。
        s2_source, s3_source = calls_of(provider, "source", "S2"), calls_of(provider, "source", "S3")
        s2_derive, s3_derive = calls_of(provider, "derive", "S2"), calls_of(provider, "derive", "S3")
        resume = client.post(f"/projects/{pid}/reader/fold-runs/{ready['run_id']}/resume", headers=headers)
        self.assertEqual(resume.status_code, 200, resume.text)
        done = self._snapshot(client, pid, app)
        self.assertEqual(done["status"], "ready", done)
        self.assertEqual(calls_of(provider, "source", "S2"), s2_source)
        self.assertEqual(calls_of(provider, "source", "S3"), s3_source)
        self.assertEqual(calls_of(provider, "derive", "S2"), s2_derive)
        self.assertEqual(calls_of(provider, "derive", "S3"), s3_derive)

    # G. ready 后重开：缓存复用 0 新调用 -----------------------------------

    def test_ready_run_restart_reuses_cache_zero_calls(self) -> None:
        pid = self._make_project("PartialCache")
        provider = PartialProvider()
        app, client = self._app(provider)
        self._bind_service(client, app)
        self._start(client, pid, app)
        first = self._snapshot(client, pid, app)
        self.assertEqual(first["status"], "ready", first)
        avail = assert_availability_shape(self, first.get("availability"))
        self.assertEqual(avail["units_pending"], 0, avail)
        total_before = len(provider.calls)
        self.assertGreater(total_before, 0)
        # 再次启动（新 run）：全部节有效缓存复用，0 新模型调用。
        self._start(client, pid, app)
        second = self._snapshot(client, pid, app)
        self.assertEqual(second["status"], "ready", second)
        self.assertEqual(len(provider.calls), total_before, provider.calls)
        self.assertEqual(int((second.get("progress") or {}).get("reused") or 0),
                         int((second.get("progress") or {}).get("total") or 0), second)


if __name__ == "__main__":
    unittest.main()
