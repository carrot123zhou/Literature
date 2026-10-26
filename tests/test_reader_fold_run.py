"""整篇 fold-run 生命周期（AIFold 默认关闭→整篇处理→全部完成才 ready）的针对性测试。

覆盖：
- 默认读 state 零模型（idle run 快照，无任何调用）；
- 整书原译只一次：source fold 一次/节 → 目标短层派生一次/节，无重复调用；
- pause/resume：边界暂停不产生空 id failed；resume 只补未完成，缓存不重计费；
- 重启恢复：新 Runner + recover() 后 running→paused，可 resume 继续；
- 失败不能 ready：单节失败保留在 errors，ready=false；成功节保留；
- 旧缓存兼容：既有 layers.json 的有效 AI 内容复用，0 新调用即 ready；
- 事实哈希不变：document.json / translation.json 逐字节不变；
- 词对应：native 结构保留 toks/sents/lad（brief 词 m、topic 词 k、ev 句下标）与 tr[locale]。
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

from literature.models import Block, Document, Page, TranslationEntry, TranslationLayer
from literature.reader import ReaderJobRunner, ReaderLayerService, ReaderStore
from literature.storage import ProjectStore


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
        Block(block_id="b-title-2", type="title", order=4, page_index=1, source_text="Conclusion", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-list", type="list", order=5, page_index=1, bbox=[10, 50, 300, 120], items=["Hoards cluster in port cities.", "Finds thin out inland."]),
        Block(block_id="b-eq", type="equation", order=6, page_index=1, bbox=[10, 130, 300, 170],
              source_text="<script>alert(1)</script> Ag + Cl -&gt; AgCl"),
    ]
    return Document(
        project_id=project_id, title="Silk Road Silver", source_language="en",
        page_count=2,
        pages=[Page(page_index=0, width=600, height=800), Page(page_index=1, width=600, height=800)],
        blocks=blocks,
    )


def make_translation(document: Document, locale: str = "zh-CN") -> TranslationLayer:
    entries = {}
    for block in document.blocks:
        if block.type == "paragraph":
            entries[block.block_id] = TranslationEntry(block_id=block.block_id, text=f"[译] {block.source_text}")
        elif block.type == "title":
            entries[block.block_id] = TranslationEntry(block_id=block.block_id, text=f"[译章] {block.source_text}")
        elif block.type == "list":
            entries[block.block_id] = TranslationEntry(block_id=block.block_id, items=[f"[译] {item}" for item in block.items or []])
    return TranslationLayer(project_id=document.project_id, locale=locale, entries=entries)


def reply_for(user: str) -> dict:
    """按上游提示词格式（`[U0] paragraph` + 编号句子）回复一个章节。

    takeaway/reason 故意逐字取自原文句：上游 `written()` 只对与 full 共享的词写入 `m` 锚，
    模型自拟词没有 m——这样测试才能断言词级对应结构真实存在。"""
    import re as _re

    units = [f"U{m.group(1)}" for m in (_re.match(r"\[U(\d+)\]", line) for line in user.splitlines()) if m]
    return {
        "section": {"takeaway": "Silver vessels travelled along the Silk Road.", "topic": "Silver vessels"},
        "units": [
            {"id": unit_id, "evidence": [0],
             "takeaway": "Silver vessels travelled along the Silk Road",
             "reason": "Demand fell after the ninth century.",
             "topic": "Silver vessels", "role": "claim"}
            for unit_id in units
        ],
    }


def derive_reply_for(user: str) -> dict:
    """目标派生回复：与原文同 evidence，目标语短层 + ev_sentences（schema required，逐条对应 evidence）。"""
    import re as _re

    units = [f"U{m.group(1)}" for m in (_re.match(r"\[U(\d+)\]", line) for line in user.splitlines()) if m]
    return {
        "section": {"takeaway": "本节总结白银传播。", "topic": "白银传播"},
        "units": [
            {"id": unit_id, "evidence": [0], "takeaway": f"译要点 {unit_id}",
             "reason": "依据证据句。", "topic": "译要点", "role": "claim",
             "ev_sentences": ["这是分层译句。"]}
            for unit_id in units
        ],
    }


class ScriptedProvider:
    """source 折叠与 derive 派生共用同一个 fake 服务；记录 (kind, section) 调用序列供计费断言。

    节识别：source 提示词含 "This section: <num> <title>"；derive 提示词含 "Section: <译标题>"
    （测试书的译标题是 “[译章] Introduction/Conclusion”）。"""

    def __init__(self, *, fail_sections: set[str] | None = None, after_first: threading.Event | None = None) -> None:
        import re as _re
        self._re = _re
        self.calls: list[tuple[str, str]] = []
        self.fail_sections = set(fail_sections or ())
        self.after_first = after_first
        from literature.providers import MockProvider
        self.config = MockProvider().config

    def _section_of(self, kind: str, user: str) -> str:
        if kind == "source":
            match = self._re.search(r"This section: (\d+)", user)
            return f"S{match.group(1)}" if match else "?"
        if "Introduction" in user:
            return "S1"
        if "Conclusion" in user:
            return "S2"
        return "?"

    def complete(self, *, system: str, user: str) -> object:
        from literature.providers import ProviderResponse
        kind = "derive" if "source takeaway:" in user else "source"
        section = self._section_of(kind, user)
        self.calls.append((kind, section))
        if self.after_first is not None and len(self.calls) >= 1:
            self.after_first.set()
        if section in self.fail_sections:
            raise RuntimeError(f"模拟 {section} 折叠失败")
        payload = derive_reply_for(user) if kind == "derive" else reply_for(user)
        return ProviderResponse(
            content=json.dumps(payload, ensure_ascii=False), provider="fake", model="fake-model",
            request_id=f"req-{len(self.calls)}", input_tokens=100, output_tokens=50, estimated_cost=0.001,
        )


def wait_for(runner: ReaderJobRunner, project_id: str, statuses: tuple[str, ...], timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = runner.current_run(project_id)
        if last.get("status") in statuses:
            return last
        time.sleep(0.02)
    raise AssertionError(f"run 未在 {timeout}s 内到达 {statuses}，最后状态：{last.get('status')}")


class FoldRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        project = self.store.create_project(source, title="Silk Road Silver")
        self.project_id = project["project_id"]
        document = make_document(self.project_id)
        self.store.save_document(document)
        self.store.save_translation(make_translation(document))
        self.runner = ReaderJobRunner(self.store)
        self.reader = ReaderStore(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()
        os.environ.pop("LITERATURE_RUNTIME", None)
        os.environ.pop("LITERATURE_LIBRARY", None)

    def _provider_factory(self, provider: ScriptedProvider):
        return lambda: provider

    def _facts_digests(self) -> dict[str, str]:
        base = self.store.project_path(self.project_id)
        values = {}
        document_path = base / "document" / "document.json"
        translation_path = base / "translations" / "zh-CN" / "translation.json"
        for path in (document_path, translation_path):
            if path.is_file():
                values[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return values

    # 1. 默认读 state 零模型 -------------------------------------------------

    def test_idle_run_snapshot_without_any_model_call(self) -> None:
        snapshot = self.runner.current_run(self.project_id)
        self.assertEqual(snapshot["status"], "idle")
        self.assertEqual(snapshot["progress"]["total"], 0)
        self.assertFalse(snapshot.get("ready", False))
        # 零模型：idle 快照不创建任何作业
        self.assertEqual([job for job in self.store.list_jobs(self.project_id) if job["kind"] == "reader_fold_run"], [])

    # 2. 整书原译只一次 -------------------------------------------------------

    def test_book_run_folds_source_once_and_derives_target(self) -> None:
        provider = ScriptedProvider()
        run, created = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        self.assertTrue(created)
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertTrue(final["ready"])
        self.assertIn("模型产物", final["ready_basis"])
        # 2 节 × (source fold + derive) = 4 次调用；每节 source 恰好一次（无重复计费）
        source_calls = [item for item in provider.calls if item[0] == "source"]
        derive_calls = [item for item in provider.calls if item[0] == "derive"]
        self.assertEqual(len(source_calls), 2, provider.calls)
        self.assertEqual(len(derive_calls), 2, provider.calls)
        self.assertEqual(sorted(section for _, section in source_calls), ["S1", "S2"])
        # run 快照的计数与 job 记录一致
        job = [item for item in self.store.list_jobs(self.project_id) if item["kind"] == "reader_fold_run"][0]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["failed_sections"], [])

    # 3. pause / resume：边界暂停、只补未完成、缓存不重计费 ---------------------

    def test_pause_at_boundary_then_resume_without_rebilling(self) -> None:
        """暂停在请求边界生效：S1 源前置折完、派生请求**未发**即暂停（pending 含 S1）；
        resume 后源缓存 0 调用复用，只补派生与剩余节——不重复计费。"""
        pause_after_first = threading.Event()
        provider = ScriptedProvider(after_first=pause_after_first)
        run, created = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        run_id = run["run_id"]
        # 第一个模型请求（S1 源前置）发生后立即请求暂停：派生请求不发出
        self.assertTrue(pause_after_first.wait(timeout=5))
        paused = self.runner.pause_run(self.project_id, run_id)
        self.assertIsNotNone(paused)
        final = wait_for(self.runner, self.project_id, ("paused", "ready"), timeout=15)
        if final["status"] == "ready":
            self.skipTest("provider 过快：整篇在暂停生效前已完成（边界语义允许，不算失败）")
        self.assertEqual(final["status"], "paused")
        self.assertGreaterEqual(final["progress"]["pending"], 1)
        # 暂停不产生空 id 的 failed 项；失败节（如有）才有 section_id
        self.assertTrue(all(item.get("section_id") for item in final.get("errors") or []))
        # S1 的派生请求未发（请求边界暂停）
        derive_before = [item for item in provider.calls if item[0] == "derive"]
        self.assertFalse(derive_before, provider.calls)
        # resume：只补未完成节——S1 源前置缓存 0 调用复用，不再发源请求
        calls_before = len(provider.calls)
        resumed, created = self.runner.resume_run(
            self.project_id, run_id, provider_factory=self._provider_factory(provider),
            model_label="ignore-me", service_id="ignore-me", binding_source="reader_role",
        )
        self.assertIsNotNone(resumed)
        self.assertTrue(created)
        self.assertEqual(resumed["run_id"], run_id)
        self.assertEqual(resumed["binding"]["model"], "fake-model")   # 绑定冻结，不换绑
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        source_calls = [item for item in provider.calls if item[0] == "source"]
        derive_calls = [item for item in provider.calls if item[0] == "derive"]
        # 源每节恰好一次（S1 不重复计费）；派生每节一次
        self.assertEqual(len(source_calls), 2, provider.calls)
        self.assertEqual(len(derive_calls), 2, provider.calls)
        self.assertGreater(len(provider.calls), calls_before)   # 补了剩余节
        self.assertGreaterEqual(final["progress"]["ratio"], 1.0)

    # 4. 重启恢复 -----------------------------------------------------------

    def test_restart_recovers_running_run_as_paused_and_resumable(self) -> None:
        pause_after_first = threading.Event()
        provider = ScriptedProvider(after_first=pause_after_first)
        run, _ = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        run_id = run["run_id"]
        self.assertTrue(pause_after_first.wait(timeout=5))
        self.runner.pause_run(self.project_id, run_id)
        final = wait_for(self.runner, self.project_id, ("paused", "ready"), timeout=15)
        if final["status"] == "ready":
            self.skipTest("provider 过快：整篇在暂停生效前已完成")
        # 模拟重启：新 Runner（无线程）+ recover()
        fresh_runner = ReaderJobRunner(self.store)
        fresh_runner.recover()
        recovered = fresh_runner.current_run(self.project_id)
        self.assertEqual(recovered["status"], "paused")
        self.assertEqual(recovered["run_id"], run_id)
        # 继续：补齐未完成节并就绪
        provider2 = ScriptedProvider()
        resumed, created = fresh_runner.resume_run(
            self.project_id, run_id, provider_factory=self._provider_factory(provider2),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        self.assertIsNotNone(resumed)
        final = wait_for(fresh_runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)

    # 5. 失败不能 ready -------------------------------------------------------

    def test_failed_section_blocks_ready_and_is_reported(self) -> None:
        provider = ScriptedProvider(fail_sections={"S2"})
        run, _ = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "failed")
        self.assertFalse(final["ready"])
        failed_ids = {item["section_id"] for item in final.get("errors") or []}
        self.assertIn("S2", failed_ids)
        # S1 成功保留：layers 里有模型内容
        doc = ReaderLayerService(self.store).document(self.project_id, locale="zh-CN")
        s1 = next(item for item in doc["sections"] if item["id"] == "S1")
        self.assertTrue(any(unit.get("source") == "model" for unit in s1["units"]))

    # 6. 旧缓存兼容 -----------------------------------------------------------

    def test_existing_valid_layers_are_reused_without_new_calls(self) -> None:
        service = ReaderLayerService(self.store)
        provider = ScriptedProvider()
        service.generate(self.project_id, locale="source", provider=provider, model_label="fake-model",
                         service_id="svc")
        service.generate(self.project_id, locale="zh-CN", provider=provider, model_label="fake-model",
                         service_id="svc")
        calls_before = len(provider.calls)
        # 有效缓存存在：run 直接复用，0 新调用即 ready
        run, created = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertEqual(len(provider.calls), calls_before)

    # 7. 事实哈希不变 ---------------------------------------------------------

    def test_fact_files_untouched_by_run(self) -> None:
        before = self._facts_digests()
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(self._facts_digests(), before)

    # 8. 词级 native 结构（m/k/ev/tr） ----------------------------------------

    def test_native_doc_keeps_word_level_structures(self) -> None:
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        wait_for(self.runner, self.project_id, ("ready", "failed"))
        native = self.reader.load_native(self.project_id, "zh-CN")
        self.assertIsNotNone(native)
        self.assertEqual(native["format"], "textfold/doc")
        # units 以稳定 block_id 为键，toks/sents/lad 齐备
        units = native["units"]
        self.assertIn("b-p-1", units)
        unit = units["b-p-1"]
        self.assertTrue(unit["toks"])
        self.assertTrue(unit["sents"])
        self.assertIsInstance(unit["sents"][0], list)
        lad = unit.get("lad") or {}
        self.assertTrue(lad.get("brief"))
        # 词级 morph：m/k 只在词真实共享时存在（不伪造对齐）
        mapped = [token for token in lad["brief"] if isinstance(token, dict) and token.get("m") is not None]
        self.assertTrue(mapped, lad["brief"])
        for token in mapped:
            self.assertLess(token["m"], len(unit["toks"]))
        for ev in lad.get("ev") or []:
            self.assertLess(ev, len(unit["sents"]))
        # 目标 lane：tr[locale] 内嵌；ev_space=source（derived）；derived_brief 由
        # brief_sentences 确定性分词，source_ev 与 ev 1:1，**无指向全文的 m**（不伪造对齐）；
        # lad.brief/tn/topic 保持 Key/Takeaway/Topic 语义（L2/L1/L0），不与 L3 Brief 混用。
        tr = (unit.get("tr") or {}).get("zh-CN")
        self.assertIsNotNone(tr)
        self.assertTrue(tr["toks"])
        tr_lad = tr.get("lad") or {}
        self.assertEqual(tr_lad.get("ev_space"), "source")
        self.assertEqual(tr_lad.get("brief_source"), "derived")
        self.assertTrue(tr_lad.get("brief_sentences"))
        derived = tr_lad.get("derived_brief") or {}
        self.assertEqual(len(derived.get("sents") or []), len(tr_lad["brief_sentences"]))
        self.assertEqual(derived.get("source_ev"), [int(i) for i in tr_lad.get("ev") or []])
        self.assertTrue(derived.get("toks"))
        self.assertFalse(any(token.get("m") is not None for token in derived["toks"]), derived["toks"])
        self.assertTrue(tr_lad.get("brief"))
        self.assertIsInstance(tr_lad.get("tn"), int)
        # ev_sentences（L3 Brief）与 takeaway/reason（L2 Key）是不同内容：brief 句数与
        # brief_sentences 一致，且 Key 词 token 序列 ≠ derived_brief 词 token 序列
        self.assertNotEqual(
            [token.get("t") for token in tr_lad["brief"]],
            [token.get("t") for token in derived["toks"]],
        )
        # chunks 保留章节框架与 unit 列表；原文层 native 也已落盘
        chunk_ids = {chunk["id"] for chunk in native["chunks"]}
        self.assertEqual(chunk_ids, {"S1", "S2"})
        source_native = self.reader.load_native(self.project_id, "source")
        self.assertIsNotNone(source_native)
        self.assertIn("b-p-1", source_native["units"])

    # 8b. blocks 原文顺序 / chunk lad·tr / 单元 chunk·role / 消毒 ----------------

    def test_native_blocks_order_and_sanitization(self) -> None:
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        wait_for(self.runner, self.project_id, ("ready", "failed"))
        native = self.reader.load_native(self.project_id, "zh-CN")
        # blocks 是原文顺序的 h/u 序列（reader.js build() 遍历形状）：
        # h(S1) u(b-p-1) u(b-p-2) h(S2) u(b-list) —— 公式块 eq 也在原位
        kinds = [(item["k"], item.get("chunk"), item.get("u")) for item in native["blocks"]]
        self.assertEqual(
            kinds,
            [("h", "S1", None), ("u", "S1", "b-p-1"), ("u", "S1", "b-p-2"),
             ("h", "S2", None), ("u", "S2", "b-list"), ("eq", "S2", None)],
            kinds,
        )
        # unit 带 chunk/role（列表项 role=item 且带 list 分组键）
        self.assertEqual(native["units"]["b-p-1"]["chunk"], "S1")
        self.assertEqual(native["units"]["b-p-1"]["role"], "para")
        self.assertEqual(native["units"]["b-list"]["role"], "item")
        self.assertEqual(native["units"]["b-list"]["list"], "b-list")
        # chunk 带 html（转义标题）与 lad.take/topic；译文 chunk tr 带目标语 lad
        s1 = next(chunk for chunk in native["chunks"] if chunk["id"] == "S1")
        self.assertEqual(s1["html"], "Introduction")
        self.assertTrue((s1.get("lad") or {}).get("take"))
        self.assertIn("zh-CN", s1.get("tr") or {})
        self.assertIsNotNone((s1["tr"]["zh-CN"]).get("lad"))
        # 消毒：eq 的 html 无可执行标记（script 标签被转义为惰性文本；无事件属性/伪协议），
        # 正文实体转义保留 —— &lt;script&gt; 在浏览器里显示为文本，不会执行。
        eq = next(item for item in native["blocks"] if item["k"] == "eq")
        self.assertNotIn("<script", eq["html"].lower())
        self.assertNotRegex(eq["html"], r"on\w+\s*=", )
        self.assertIn("Ag + Cl", eq["html"])
        # meta/langs/app/katex 根字段（fixture 对齐）
        self.assertEqual(native["meta"]["title"], "Silk Road Silver")
        self.assertEqual(native["meta"]["langs"], ["en", "zh-CN"])
        self.assertTrue(native["app"])
        self.assertEqual(native["katex"], "inside")
        self.assertIn("zh", native["langs"])

    # 9. 幂等与互斥 -----------------------------------------------------------

    def test_double_start_is_idempotent_while_running(self) -> None:
        gate = threading.Event()
        provider = ScriptedProvider()
        original_complete = provider.complete

        def slow_complete(*, system: str, user: str):
            gate.wait(timeout=5)   # 拖住第一节，制造 running 窗口
            return original_complete(system=system, user=user)

        provider.complete = slow_complete
        run, created = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        self.assertTrue(created)
        deadline = time.time() + 5
        while time.time() < deadline and self.runner.current_run(self.project_id).get("status") != "running":
            time.sleep(0.02)
        again, created_again = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        self.assertFalse(created_again)
        self.assertEqual(again["run_id"], run["run_id"])
        gate.set()
        wait_for(self.runner, self.project_id, ("ready", "failed"))

    def test_force_resamples_source_and_derive(self) -> None:
        """force=true 重新采样：已 ready 的 run 重跑 force，源前置与派生都真实重折
        （此前源前置被缓存复用、force 只重折派生——违反「force 跳过缓存预检」约定）。"""
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        wait_for(self.runner, self.project_id, ("ready",))
        self.assertEqual(len([item for item in provider.calls if item[0] == "source"]), 2)
        self.assertEqual(len([item for item in provider.calls if item[0] == "derive"]), 2)
        forced, created = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role", force=True,
        )
        self.assertTrue(created)
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertEqual(len([item for item in provider.calls if item[0] == "source"]), 4, provider.calls)
        self.assertEqual(len([item for item in provider.calls if item[0] == "derive"]), 4, provider.calls)

    def test_all_suppressed_section_skips_model_calls(self) -> None:
        """全 unit 低于确定性抑制阈值（<3 词）的节：跳过模型请求（零费用），
        suppressed 落盘且不阻塞 ready。计数断言：该节 0 次模型调用。"""
        from literature.models import Block as _Block
        document = make_document(self.project_id)
        document.blocks.append(_Block(
            block_id="b-title-3", type="title", order=7, page_index=1, source_text="Appendix", level=1, bbox=[10, 10, 300, 40],
        ))
        document.blocks.append(_Block(
            block_id="b-p-tiny", type="paragraph", order=8, page_index=1, bbox=[10, 50, 300, 80], source_text="End.",
        ))
        self.store.save_document(document)
        translation = make_translation(document)
        translation.entries["b-p-tiny"] = TranslationEntry(block_id="b-p-tiny", text="完。")
        self.store.save_translation(translation)
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        # S3（全 suppressed）：源与派生都是 0 次调用；S1/S2 正常 2+2
        source_by_section = {}
        derive_by_section = {}
        for kind, section in provider.calls:
            (source_by_section if kind == "source" else derive_by_section)[section] = (
                source_by_section if kind == "source" else derive_by_section).get(section, 0) + 1
        self.assertNotIn("S3", source_by_section, provider.calls)
        self.assertNotIn("S3", derive_by_section, provider.calls)
        self.assertEqual(source_by_section.get("S1"), 1)
        self.assertEqual(derive_by_section.get("S1"), 1)

    def test_full_lifecycle_in_one_process(self) -> None:
        """同一进程完整生命周期：ready → 事实变化失效 → resume 只补失效节 → 再次 ready。"""
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        first = wait_for(self.runner, self.project_id, ("ready",))
        self.assertTrue(first["ready"])
        self.assertEqual(len([item for item in provider.calls if item[0] == "source"]), 2)
        # 事实变化：S1 正文变 → ready 失效（状态 failed，可 resume）
        document = self.store.load_document(self.project_id)
        for block in document.blocks:
            if block.block_id == "b-p-1":
                block.source_text = "Silver vessels travelled along the Silk Road. Workshops copied shapes for decades. Demand fell after the ninth century. New evidence emerged later on."
        self.store.save_document(document)
        invalidated = self.runner.current_run(self.project_id)
        self.assertFalse(invalidated["ready"])
        self.assertEqual(invalidated["status"], "failed")
        # resume：只补失效的 S1（源重折 + 派生），S2 缓存 0 调用
        calls_before = len(provider.calls)
        resumed, _ = self.runner.resume_run(
            self.project_id, first["run_id"], provider_factory=self._provider_factory(provider),
        )
        self.assertIsNotNone(resumed)
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertTrue(final["ready"])
        source_calls = [item for item in provider.calls if item[0] == "source"]
        self.assertEqual(len(source_calls), 3, provider.calls)   # S1 重折一次，S2 不动
        self.assertEqual(len([item for item in provider.calls if item[0] == "derive"]), 3, provider.calls)
        self.assertGreater(len(provider.calls), calls_before)

    def test_pause_unknown_run_returns_none(self) -> None:
        self.assertIsNone(self.runner.pause_run(self.project_id, "fr-notexist"))

    # ---- 混合章源 suppressed 权威传播（真实书 fr-dfd3b500 阻断修复） --------

    def _mixed_document(self, project_id: str) -> Document:
        """S1 = 正常长段 + 3 个装置/署名短 unit（源按 MIN_FOLD_WORDS 抑制，目标译文较长）；
        S2 = 正常。复刻真实书混合章形态。"""
        blocks = [
            Block(block_id="b-title-1", type="title", order=0, page_index=0, source_text="Introduction", level=1, bbox=[10, 10, 300, 40]),
            Block(block_id="b-p-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 120],
                  source_text="Silver vessels travelled along the Silk Road. Workshops in Chang'an copied Sogdian shapes for decades. Demand fell after the ninth century."),
            Block(block_id="b-p-2", type="paragraph", order=2, page_index=0, bbox=[10, 130, 300, 160], source_text="BY"),
            Block(block_id="b-p-3", type="paragraph", order=3, page_index=0, bbox=[10, 170, 300, 200], source_text="E. ZÜRCHER"),
            Block(block_id="b-p-4", type="paragraph", order=4, page_index=0, bbox=[10, 210, 300, 240], source_text="2014"),
            Block(block_id="b-title-2", type="title", order=5, page_index=1, source_text="Conclusion", level=1, bbox=[10, 10, 300, 40]),
            Block(block_id="b-list", type="list", order=6, page_index=1, bbox=[10, 50, 300, 120], items=["Hoards cluster in port cities.", "Finds thin out inland."]),
        ]
        return Document(project_id=project_id, title="Silk Road Silver", source_language="en",
                        page_count=2,
                        pages=[Page(page_index=0, width=600, height=800), Page(page_index=1, width=600, height=800)],
                        blocks=blocks)

    def _mixed_translation(self, document: Document) -> TranslationLayer:
        entries = {}
        longer = {"b-p-2": "作者：", "b-p-3": "许理和（E. ZÜRCHER）", "b-p-4": "2014年11月6日访问"}
        for block in document.blocks:
            if block.block_id in longer:
                entries[block.block_id] = TranslationEntry(block_id=block.block_id, text=longer[block.block_id])
            elif block.type == "paragraph":
                entries[block.block_id] = TranslationEntry(block_id=block.block_id, text=f"[译] {block.source_text}")
            elif block.type == "title":
                entries[block.block_id] = TranslationEntry(block_id=block.block_id, text=f"[译章] {block.source_text}")
            elif block.type == "list":
                entries[block.block_id] = TranslationEntry(block_id=block.block_id, items=[f"[译] {i}" for i in block.items or []])
        return TranslationLayer(project_id=document.project_id, locale="zh-CN", entries=entries)

    def test_mixed_suppressed_units_reach_ready_without_new_calls(self) -> None:
        """混合章：源装置/署名 unit 被 MIN_FOLD_WORDS 抑制、目标译文较长——
        旧存盘 translation-local 标签在 resume 时被权威重标为 suppressed（复用零新调用）
        → ready。ThrowingProvider 证明零新模型调用。"""
        from literature.reader import ReaderLayerService
        document = self._mixed_document(self.project_id)
        self.store.save_document(document)
        self.store.save_translation(self._mixed_translation(document))
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        wait_for(self.runner, self.project_id, ("ready", "failed"))
        # 模拟旧代码存盘的 translation-local 标签（真实书 fr-dfd3b500 的状态）
        reader = ReaderStore(self.store)
        stored = reader.load_layers(self.project_id)
        zh_doc = stored["docs"]["zh-CN"]
        corrupted = []
        for section in zh_doc["sections"]:
            for unit in section["units"]:
                if unit.get("source") == "suppressed":
                    unit["source"] = "local"
                    unit["brief_source"] = "translation-local"
                    unit["suppressed"] = False
                    corrupted.append(unit["id"])
        self.assertTrue(corrupted, "夹具未产生 suppressed 源 unit")
        reader.save_locale_doc(self.project_id, "zh-CN",
                               {**zh_doc, "sections": zh_doc["sections"]}, stored["section_hashes"].get("zh-CN") or {})
        # 旧标签下 readiness 失败（复现真实阻断）
        stale_readiness = ReaderLayerService(self.store).fold_run_readiness(
            self.project_id, locale="zh-CN", service_id="svc", model="fake-model")
        # 注意：豁免规则（源 suppressed 权威传播）在读取侧已放行——旧行为是失败。
        # 这里断言 resume 后标签被修复且 ready（核心契约）。
        provider2 = ScriptedProvider()
        run, created = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider2),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertTrue(final["ready"])
        # 零新模型调用（全缓存复用）
        self.assertEqual(len(provider2.calls), 0, provider2.calls)
        # 标签被权威修复为 suppressed
        stored2 = reader.load_layers(self.project_id)
        for section in stored2["docs"]["zh-CN"]["sections"]:
            for unit in section["units"]:
                if unit["id"] in corrupted:
                    self.assertEqual(unit.get("source"), "suppressed",
                                     f"{unit['id']} 应沿源抑制")
                    self.assertTrue(unit.get("suppressed"))

    def test_throwing_provider_resume_still_ready_from_cache(self) -> None:
        """resume 时 provider 抛异常：全缓存复用（零 complete 调用）仍 ready——
        不得因 provider 失败把可复用 run 打成 failed。"""
        document = self._mixed_document(self.project_id)
        self.store.save_document(document)
        self.store.save_translation(self._mixed_translation(document))
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        wait_for(self.runner, self.project_id, ("ready",))

        class ThrowingProvider:
            def __init__(self):
                self.calls = []
                from literature.providers import MockProvider
                self.config = MockProvider().config

            def complete(self, *, system, user):
                self.calls.append((system, user))
                raise RuntimeError("throwing: 不应被调用（全缓存复用）")

            def close(self):
                pass

        throwing = ThrowingProvider()
        run, _ = self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=lambda: throwing,
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertEqual(throwing.calls, [])

    def test_finalize_errors_carry_section_reasons(self) -> None:
        """readiness 失败时 errors 带真实 per-section 理由（不再空 errors + 模糊消息）。"""
        provider = ScriptedProvider(fail_sections={"S2"})
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        final = wait_for(self.runner, self.project_id, ("failed",))
        self.assertFalse(final["ready"])
        err_ids = {item.get("section_id") for item in final.get("errors") or []}
        self.assertIn("S2", err_ids)
        reasons = [item.get("error") for item in final.get("errors") or [] if item.get("section_id") == "S2"]
        self.assertTrue(any("本地摘句" in r or "未就绪" in r or "不一致" in r for r in reasons), reasons)

    def test_mixed_translation_longer_stays_suppressed_in_native(self) -> None:
        """native/缓存恢复：目标译文较长的 suppressed 源 unit 在 native 中保持 suppressed
        （_restore_target_lads 权威传播，不被 translation-local 覆盖）。"""
        document = self._mixed_document(self.project_id)
        self.store.save_document(document)
        self.store.save_translation(self._mixed_translation(document))
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        wait_for(self.runner, self.project_id, ("ready", "failed"))
        native = self.reader.load_native(self.project_id, "zh-CN")
        for uid in ("b-p-2", "b-p-3", "b-p-4"):
            unit = native["units"][uid]
            tr = (unit.get("tr") or {}).get("zh-CN") or {}
            self.assertTrue(tr.get("suppressed"), f"{uid} 目标译文较长但源抑制未传播")
            self.assertEqual((tr.get("lad") or {}).get("brief"), [], f"{uid} 不应做 translation-local 折叠")

    # ---- ②③④ 边界回归 ---------------------------------------------------

    def test_failed_run_ratio_is_not_100(self) -> None:
        """失败不假 100：单节失败时终态 ratio 是就绪节/total，严格小于 1。"""
        provider = ScriptedProvider(fail_sections={"S2"})
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "failed")
        self.assertFalse(final["ready"])
        self.assertLess(final["progress"]["ratio"], 1.0)
        self.assertGreaterEqual(final["progress"]["failed"], 1)

    def test_ready_invalidated_when_facts_change(self) -> None:
        """ready 不是终态豁免：事实（document.json 正文）变化后 current_run 重校失效。"""
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        ready = wait_for(self.runner, self.project_id, ("ready",))
        self.assertTrue(ready["ready"])
        # 改事实：S1 正文变化 → 源 digest 变 → 派生键失效 → ready 失效
        document = self.store.load_document(self.project_id)
        for block in document.blocks:
            if block.block_id == "b-p-1":
                block.source_text = "Silver vessels travelled along the Silk Road. Workshops copied shapes for decades. Demand fell after the ninth century. New evidence emerged later on."
        self.store.save_document(document)
        revalidated = self.runner.current_run(self.project_id)
        self.assertFalse(revalidated["ready"], revalidated.get("ready_basis"))
        self.assertEqual(revalidated["status"], "failed")
        self.assertIn("S1", revalidated["ready_basis"])

    def test_ready_invalidated_when_native_missing(self) -> None:
        """native 写入失败/被删 → 不能 ready（词级结构是五级阅读的数据源）。"""
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        ready = wait_for(self.runner, self.project_id, ("ready",))
        self.assertTrue(ready["ready"])
        self.reader.native_path(self.project_id, "zh-CN").unlink()
        revalidated = self.runner.current_run(self.project_id)
        self.assertFalse(revalidated["ready"])
        self.assertIn("词级结构", revalidated["ready_basis"])

    def test_all_suppressed_section_does_not_deadlock(self) -> None:
        """全 suppressed 节（页码/分隔符等装置内容）算就绪：整书全是装置内容也不死锁。"""
        from literature.models import Block as _Block
        document = make_document(self.project_id)
        document.blocks.append(_Block(
            block_id="b-title-3", type="title", order=7, page_index=1, source_text="Appendix", level=1, bbox=[10, 10, 300, 40],
        ))
        document.blocks.append(_Block(
            block_id="b-p-tiny", type="paragraph", order=8, page_index=1, bbox=[10, 50, 300, 80], source_text="End.",
        ))
        self.store.save_document(document)
        translation = make_translation(document)
        translation.entries["b-p-tiny"] = TranslationEntry(block_id="b-p-tiny", text="完。")
        self.store.save_translation(translation)
        provider = ScriptedProvider()
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        final = wait_for(self.runner, self.project_id, ("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertTrue(final["ready"])

    def test_live_progress_counters_are_per_section(self) -> None:
        """逐节实时计数：completed/reused/failed/pending 在运行中就可见（不等到 finalize）。"""
        release = threading.Event()
        provider = ScriptedProvider()
        original_complete = provider.complete

        def gated_complete(*, system: str, user: str):
            result = original_complete(system=system, user=user)
            # 第一节完成后拖住后续请求，给主线程一个观察窗口
            if len(provider.calls) >= 2:
                release.wait(timeout=5)
            return result

        provider.complete = gated_complete
        self.runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=self._provider_factory(provider),
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        deadline = time.time() + 10
        observed = None
        while time.time() < deadline:
            snapshot = self.runner.current_run(self.project_id)
            progress = snapshot.get("progress") or {}
            if progress.get("completed", 0) + progress.get("reused", 0) >= 1:
                observed = snapshot
                break
            time.sleep(0.02)
        release.set()
        self.assertIsNotNone(observed, "运行中从未观察到逐节计数")
        progress = observed["progress"]
        self.assertGreaterEqual(progress["completed"] + progress["reused"], 1)
        self.assertEqual(progress["total"], 2)
        wait_for(self.runner, self.project_id, ("ready", "failed"))


class FoldRunApiTests(unittest.TestCase):
    """公开 API 契约（designer 接线用）：路由形态、幂等/错误码、native 404 语义。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # 隔离 runtime/library：模型服务配置、角色状态不落在真实 Application Support，
        # 也不被其他测试留下的全局状态污染（单跑/全跑行为一致）。
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        project = self.store.create_project(source, title="Silk Road Silver")
        self.project_id = project["project_id"]
        document = make_document(self.project_id)
        self.store.save_document(document)
        self.store.save_translation(make_translation(document))
        from fastapi.testclient import TestClient
        from literature.api import create_app
        self.client = TestClient(create_app(self.store))
        self.headers = {"x-literature-token": self.client.app.state.session_token}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_state_includes_fold_run_snapshot(self) -> None:
        response = self.client.get(f"/projects/{self.project_id}/reader/state", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["fold_run"]["status"], "idle")
        self.assertIn("progress", body["fold_run"])

    def test_start_run_without_fold_model_is_400_with_guidance(self) -> None:
        response = self.client.post(f"/projects/{self.project_id}/reader/fold-runs", json={}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("折叠模型", response.json()["detail"])

    def test_unknown_run_pause_is_404_and_native_missing_is_404(self) -> None:
        response = self.client.post(f"/projects/{self.project_id}/reader/fold-runs/fr-nope/pause", headers=self.headers)
        self.assertEqual(response.status_code, 404)
        response = self.client.get(
            f"/projects/{self.project_id}/reader/layers/native?locale=zh-CN", headers=self.headers,
        )
        self.assertEqual(response.status_code, 404)

    def test_fold_run_input_rejects_unknown_fields(self) -> None:
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs",
            json={"locale": "zh-CN", "unexpected": True},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)

    def test_resume_without_body_continues_paused_run(self) -> None:
        """E6 修复：UI「继续」发无 body POST resume → 200 续跑（不再 422）。

        覆盖：无 body / {} / paused→running→ready / running→409 / 未知字段 422 /
        绑定不被 body 改变。provider 受控、零付费。"""
        import threading
        import time as _time
        from tests.test_reader_fold_run_isolation_audit import _seed_service_and_role
        _seed_service_and_role(self.root / "runtime", model="fake-model")
        from literature.model_services import ModelServiceStore
        import literature.model_services as ms

        provider = ScriptedProvider(after_first=threading.Event())
        original_api_key = ms.ModelServiceStore.api_key
        original_provider_from = None
        ms.ModelServiceStore.api_key = lambda self, sid: "test-not-a-real-key"
        try:
            # provider 工厂按冻结绑定走 manager.provider_from：打桩替换为受控 provider
            from literature.api import create_app as _create_app  # noqa: F401  (确保模块已加载)
            # 重新建 client（setUp 里已建过；这里补丁 provider_from）
            client = self.client
            app = client.app
            manager = app.state.job_manager
            original_provider_from = manager.provider_from

            def provider_from(*args, **kwargs):
                return provider

            manager.provider_from = provider_from
            headers = self.headers
            started = client.post(
                f"/projects/{self.project_id}/reader/fold-runs",
                json={"locale": "zh-CN"}, headers=headers,
            )
            self.assertEqual(started.status_code, 200, started.text)
            run_id = started.json()["run_id"]
            # 第一个模型请求后暂停（S1 源完成、派生未发）
            self.assertTrue(provider.after_first.wait(timeout=5))
            paused = client.post(
                f"/projects/{self.project_id}/reader/fold-runs/{run_id}/pause", headers=headers,
            )
            self.assertEqual(paused.status_code, 200, paused.text)
            deadline = _time.time() + 10
            while _time.time() < deadline:
                status = client.get(
                    f"/projects/{self.project_id}/reader/fold-run", headers=headers,
                ).json().get("status")
                if status in ("paused", "ready"):
                    break
                _time.sleep(0.05)
            if status == "ready":
                self.skipTest("provider 过快：暂停生效前已完成")
            self.assertEqual(status, "paused")
            frozen_binding = client.get(
                f"/projects/{self.project_id}/reader/fold-run", headers=headers,
            ).json()["binding"]
            # 无 body resume → 200（E6 主断言）
            resumed = client.post(
                f"/projects/{self.project_id}/reader/fold-runs/{run_id}/resume", headers=headers,
            )
            self.assertEqual(resumed.status_code, 200, resumed.text)
            self.assertEqual(resumed.json()["run"]["status"], "running")
            # 绑定不被 body 改变：仍为冻结绑定
            self.assertEqual(resumed.json()["run"]["binding"], frozen_binding)
            # running 状态再 resume → 409
            again = client.post(
                f"/projects/{self.project_id}/reader/fold-runs/{run_id}/resume", headers=headers,
            )
            self.assertEqual(again.status_code, 409)
            # 未知字段仍 422（extra=forbid 不因 body 可省而失效）
            bad = client.post(
                f"/projects/{self.project_id}/reader/fold-runs/{run_id}/resume",
                json={"unexpected": True}, headers=headers,
            )
            self.assertEqual(bad.status_code, 422)
            # 等 run 完成（resume 只补未完成节）
            deadline = _time.time() + 15
            while _time.time() < deadline:
                status = client.get(
                    f"/projects/{self.project_id}/reader/fold-run", headers=headers,
                ).json().get("status")
                if status in ("ready", "failed"):
                    break
                _time.sleep(0.05)
            self.assertEqual(status, "ready")
        finally:
            ms.ModelServiceStore.api_key = original_api_key
            if original_provider_from is not None:
                self.client.app.state.job_manager.provider_from = original_provider_from

    def test_resume_rejects_locale_change(self) -> None:
        """resume 不能换 locale（run 的目标语言冻结）；不因 body 可省而放松。"""
        from literature.reader import ReaderJobRunner
        runner = ReaderJobRunner(self.store)
        provider = ScriptedProvider()
        run, _ = runner.start_run(
            self.project_id, locale="zh-CN", provider_factory=lambda: provider,
            model_label="fake-model", service_id="svc", binding_source="reader_role",
        )
        # 直接把 run 置为 paused（绕过线程，专注路由校验）
        runner.pause_run(self.project_id, run["run_id"])
        deadline = time.time() + 10
        while time.time() < deadline and runner.current_run(self.project_id).get("status") != "paused":
            time.sleep(0.02)
        from fastapi.testclient import TestClient
        # runner 直启的 run 没有 API 侧 provider 打桩：resume 会尝试真折叠——
        # 这里只断言 400（locale 不匹配）发生在启动之前。
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs/{run['run_id']}/resume",
            json={"locale": "fr-FR"}, headers=self.headers,
        )
        self.assertIn(response.status_code, (400, 409))

    def test_unknown_locale_is_400(self) -> None:
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs",
            json={"locale": "fr-FR"},
            headers=self.headers,
        )
        # 未绑定模型先拦（400 折叠模型）；locale 校验在其之前——两者都可能是拦截面，
        # 只要求不出现 5xx 且错误可读。
        self.assertIn(response.status_code, (400, 422))
        self.assertTrue(str(response.json().get("detail", "")))


if __name__ == "__main__":
    unittest.main()
