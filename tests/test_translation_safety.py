from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from literature.models import Block, Cell, Document, TranslationEntry, block_source_digest
from literature.providers import MockProvider, ProviderConfig, ProviderError, ProviderResponse
from literature.storage import ProjectStore
from literature.structure import apply_patch
from literature.translation import TranslationService, entry_quality_warnings, entry_stale_reason, entry_state, translation_payload, translation_stats


def block(block_id: str, type: str, text: str = "", *, order: int = 0, cells: list[Cell] | None = None) -> Block:
    return Block(block_id=block_id, type=type, order=order, page_index=0, source_text=text, cells=cells or [])


def make_project(root: Path, blocks: list[Block], title: str = "Safety Fixture") -> tuple[ProjectStore, str]:
    store = ProjectStore(root / "library")
    pdf = root / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% fixture")
    project = store.create_project(pdf, title=title)
    project_id = project["project_id"]
    document = Document(project_id=project_id, title=title, source_language="en", target_language="zh-CN")
    document.blocks = blocks
    document.page_count = 1
    store.save_document(document)
    return store, project_id


class ConcurrentEditProvider(MockProvider):
    """首次响应前，模拟另一个写入者保存人工译文与即时翻译记录。"""

    def __init__(self, store: ProjectStore, project_id: str, *, edit_block: str, edit_text: str, selection_block: str) -> None:
        super().__init__()
        self.store = store
        self.project_id = project_id
        self.edit_block = edit_block
        self.edit_text = edit_text
        self.selection_block = selection_block
        self.done = False

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        if not self.done:
            self.done = True
            writer = TranslationService(self.store, MockProvider())
            writer.edit(self.project_id, self.edit_block, self.edit_text)
            writer.save_selection(self.project_id, self.selection_block, "a word", "一个词")
        return super().complete(system=system, user=user)


class SplitProvider(MockProvider):
    """整块请求只返回 c2（缺 c1），切分后的子请求各返回自己的那一格。"""

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
        ids = [segment_id for segment_id, _ in segments]
        if len(ids) > 1:
            return ProviderResponse(
                content=f'<seg id="{ids[-1]}">译：{segments[-1][1].strip()}</seg>',
                provider="mock", model="mock", request_id="req", finish_reason="stop",
            )
        return super().complete(system=system, user=user)


class EmptySegProvider(MockProvider):
    def complete(self, *, system: str, user: str) -> ProviderResponse:
        segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
        content = "\n".join(f'<seg id="{segment_id}"></seg>' for segment_id, _ in segments)
        return ProviderResponse(content=content, provider="mock", model="mock", request_id="req", finish_reason="stop")


class LengthProvider(MockProvider):
    def complete(self, *, system: str, user: str) -> ProviderResponse:
        response = super().complete(system=system, user=user)
        return ProviderResponse(
            content=response.content, provider=response.provider, model=response.model,
            request_id=response.request_id, finish_reason="length",
        )


class CountingProvider(MockProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        self.calls += 1
        return super().complete(system=system, user=user)


class ExplodingProvider(MockProvider):
    def complete(self, *, system: str, user: str) -> ProviderResponse:
        raise AssertionError("纯层级变化不应发送模型请求")


class TranslationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_f01_manual_edit_and_selection_survive_running_job(self):
        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "The first paragraph."),
            block("b2", "paragraph", "The second paragraph."),
        ])
        provider = ConcurrentEditProvider(store, project_id, edit_block="b2", edit_text="人工确认译文", selection_block="b1")
        job = TranslationService(store, provider).run(project_id, parallel_workers=1)
        self.assertEqual(job["status"], "completed")
        layer = store.load_translation(project_id)
        # 运行期间保存的人工译文绝不能被作业的旧快照覆盖，锁定状态也必须保留。
        self.assertEqual(layer.entries["b2"].text, "人工确认译文")
        self.assertTrue(layer.entries["b2"].locked)
        # 模型结果作为候选保留，供用户对比采用。
        self.assertTrue(layer.entries["b2"].candidates)
        # 即时翻译附属记录也不能丢。
        self.assertEqual(layer.entries["b1"].selection_translations[0]["translated_text"], "一个词")
        self.assertEqual(layer.entries["b1"].status, "translated")

    def test_f01_glossary_frozen_during_run_is_kept(self):
        store, project_id = make_project(self.root, [block("b1", "paragraph", "The first paragraph.")])

        class FreezeProvider(MockProvider):
            def complete(self, *, system, user):
                if not getattr(self, "done", False):
                    self.done = True
                    layer = store.load_translation(project_id)
                    layer.glossary_snapshot = {"Paradigm": "范式"}
                    store.save_translation(layer)
                return super().complete(system=system, user=user)

        TranslationService(store, FreezeProvider()).run(project_id, glossary={"Old": "旧"})
        layer = store.load_translation(project_id)
        self.assertEqual(layer.glossary_snapshot, {"Paradigm": "范式"})

    def test_f02_split_retry_merges_all_cells(self):
        table = block("t1", "table", cells=[Cell("c1", 0, 0, "first"), Cell("c2", 0, 1, "second")])
        store, project_id = make_project(self.root, [table])
        job = TranslationService(store, SplitProvider()).run(project_id)
        self.assertEqual(job["status"], "completed")
        entry = store.load_translation(project_id).entries["t1"]
        self.assertEqual(entry.status, "translated")
        # 两个 cell 都必须有内容：后到的一半不能覆盖前一半已成功的字段。
        self.assertTrue(entry.cells["c1"])
        self.assertTrue(entry.cells["c2"])

    def test_f03_empty_segments_never_marked_translated(self):
        blocks = [block(f"b{index}", "paragraph", f"Paragraph {index}.") for index in range(4)]
        store, project_id = make_project(self.root, blocks)
        with self.assertRaises(ProviderError):
            TranslationService(store, EmptySegProvider()).run(project_id)
        layer = store.load_translation(project_id)
        for value in blocks:
            self.assertNotEqual(layer.entries[value.block_id].status, "translated")
            self.assertEqual(layer.entries[value.block_id].text, "")

    def test_f03_truncated_but_closed_tags_never_completed(self):
        blocks = [block(f"b{index}", "paragraph", f"Paragraph {index}.") for index in range(4)]
        store, project_id = make_project(self.root, blocks)
        with self.assertRaises(ProviderError):
            TranslationService(store, LengthProvider()).run(project_id)
        layer = store.load_translation(project_id)
        for value in blocks:
            self.assertNotEqual(layer.entries[value.block_id].status, "translated")

    def test_f03_duplicate_segment_id_not_counted_as_complete(self):
        class DuplicateProvider(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
                parts = []
                for segment_id, text in segments:
                    parts.append(f'<seg id="{segment_id}">译：{text.strip()}</seg>')
                    parts.append(f'<seg id="{segment_id}">另一份</seg>')
                return ProviderResponse(content="\n".join(parts), provider="mock", model="mock", request_id="req", finish_reason="stop")

        store, project_id = make_project(self.root, [block("b1", "paragraph", "Only paragraph.")])
        with self.assertRaises(ProviderError):
            TranslationService(store, DuplicateProvider()).run(project_id)
        self.assertNotEqual(store.load_translation(project_id).entries["b1"].status, "translated")

    def test_failure_reason_is_recorded_and_cleared_after_retry(self):
        class PartialProvider(MockProvider):
            """丢掉 b1 的字段，模拟模型漏段（其余块正常）。"""

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
                parts = [f'<seg id="{sid}">译：{text.strip()}</seg>' for sid, text in segments if not sid.startswith("b1#")]
                return ProviderResponse(content="\n".join(parts), provider="mock", model="mock", request_id="req", finish_reason="stop")

        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
        ])
        job = TranslationService(store, PartialProvider()).run(project_id)
        # 有失败段落时，状态与错误栏必须如实反映失败，而不是completed+空错误。
        self.assertEqual(job["status"], "partial_failed")
        self.assertIn("1 段失败", job["error"])
        entry = store.load_translation(project_id).entries["b1"]
        self.assertEqual(entry.status, "failed")
        self.assertTrue(entry.error)
        # 重试成功后清除失败原因，任务回到 completed。
        retried = TranslationService(store, MockProvider()).run(project_id, block_ids=["b1"], force=True)
        self.assertEqual(retried["status"], "completed")
        cleared = store.load_translation(project_id).entries["b1"]
        self.assertEqual(cleared.status, "translated")
        self.assertEqual(cleared.error, "")

    def test_reasoning_squeeze_truncation_reports_actionable_reason(self):
        class ReasoningProvider(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                response = super().complete(system=system, user=user)
                # 输出预算几乎全被思考过程吃掉，译文被长度截断。
                return ProviderResponse(
                    content=response.content, provider="mock", model="mock", request_id="req",
                    input_tokens=100, output_tokens=1000, reasoning_tokens=900, finish_reason="length",
                )

        store, project_id = make_project(self.root, [block("b1", "paragraph", "Only paragraph.")])
        with self.assertRaises(ProviderError):
            TranslationService(store, ReasoningProvider()).run(project_id)
        entry = store.load_translation(project_id).entries["b1"]
        self.assertEqual(entry.status, "failed")
        # 错误信息要指向可执行的修复方向（换非思考模型 / 降低思考强度 / 减小分块）。
        self.assertIn("思考", entry.error)

    def test_sub_request_failure_keeps_sibling_and_parent_results(self):
        class SplitFaultProvider(MockProvider):
            """父请求先返回第一段；左子请求超时，右子请求正常。"""

            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
                if self.calls == 1:
                    segment_id, text = segments[0]
                    return ProviderResponse(content=f'<seg id="{segment_id}">译：{text.strip()}</seg>', provider="mock", model="mock", request_id="req", finish_reason="stop")
                if self.calls == 2:
                    raise ProviderError("read timeout")
                return super().complete(system=system, user=user)

        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
            block("b3", "paragraph", "Third paragraph."),
        ])
        job = TranslationService(store, SplitFaultProvider()).run(project_id, parallel_workers=1)
        # 子请求超时不能作废父请求与兄弟批里已解析出的字段。
        self.assertEqual(job["status"], "partial_failed")
        layer = store.load_translation(project_id)
        self.assertEqual(layer.entries["b1"].status, "translated")
        self.assertTrue(layer.entries["b1"].text)
        self.assertEqual(layer.entries["b3"].status, "translated")
        self.assertEqual(layer.entries["b2"].status, "failed")
        self.assertIn("timeout", layer.entries["b2"].error)

    def test_field_count_budget_limits_batch_size(self):
        class CallCountingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls: list[int] = []

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls.append(len(re.findall(r'<src\s+id="', user)))
                return super().complete(system=system, user=user)

        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"Paragraph {index}.") for index in range(25)
        ])
        provider = CallCountingProvider()
        TranslationService(store, provider).run(project_id, parallel_workers=1)
        # 25 个单字段块按 12 字段上限切成 3 批，而不是按字符塞进一个大批。
        self.assertEqual(len(provider.calls), 3)
        self.assertTrue(all(count <= 12 for count in provider.calls))

    def test_usage_is_recorded_when_error_carries_response(self):
        class ReasonOnlyProvider(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                response = ProviderResponse(
                    content="", provider="mock", model="mock", request_id="req",
                    input_tokens=120, output_tokens=500, reasoning_tokens=480, finish_reason="stop",
                )
                raise ProviderError("模型只返回了思考内容", response=response)

        store, project_id = make_project(self.root, [block("b1", "paragraph", "Only paragraph.")])
        with self.assertRaises(ProviderError):
            TranslationService(store, ReasonOnlyProvider()).run(project_id)
        summary = store.usage_summary(project_id)
        # 错误响应已拿到的用量不能被当成零消耗。
        self.assertEqual(summary["output_tokens"], 500)
        self.assertEqual(summary["reasoning_tokens"], 480)

    def test_counts_target_is_full_checkpoint_range_after_resume(self):
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(3)
        ])
        provider = CountingProvider()
        service = TranslationService(store, provider)
        paused = service.run(project_id, parallel_workers=1, should_pause=lambda: provider.calls >= 1)
        self.assertEqual(paused["status"], "paused")
        paused_counts = store.get_job(project_id, paused["job_id"])["result"]["counts"]
        self.assertEqual(paused_counts["target_blocks"], 3)
        self.assertEqual(paused_counts["translate"]["target"], 3)
        self.assertEqual(paused_counts["translate"]["completed"], 1)
        self.assertEqual(paused_counts["translate"]["unhandled"], 2)
        resumed = service.run(project_id, parallel_workers=1, job_id=paused["job_id"])
        self.assertEqual(resumed["status"], "completed")
        counts = store.get_job(project_id, resumed["job_id"])["result"]["counts"]
        # 任务目标以 checkpoint 原始 target_ids 为准，不因恢复只剩“本次剩余范围”而变少/漏计。
        self.assertEqual(counts["target_blocks"], 3)
        self.assertEqual(counts["translate"]["target"], 3)
        self.assertEqual(counts["translate"]["completed"], 3)
        self.assertEqual(counts["translate"]["unhandled"], 0)

    def test_polish_counts_are_per_phase_not_summed(self):
        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
        ])
        job = TranslationService(store, MockProvider()).run(project_id, polish=True, parallel_workers=1)
        self.assertEqual(job["status"], "completed")
        counts = store.get_job(project_id, job["job_id"])["result"]["counts"]
        # 任务目标与初译完成都是唯一 block 数；润色是独立阶段，不与初译相加。
        self.assertEqual(counts["target_blocks"], 2)
        self.assertEqual(counts["translate"]["completed"], 2)
        self.assertEqual(counts["polish"]["target"], 2)
        self.assertEqual(counts["polish"]["completed"], 2)
        # 若误把两阶段 success_blocks 相加，target_blocks 会被重复计成 4。
        self.assertNotEqual(counts["target_blocks"], counts["translate"]["completed"] + counts["polish"]["completed"])

    def test_polish_failure_counted_in_polish_phase_only(self):
        class PolishFailProvider(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                if "润色要求" in system:
                    return ProviderResponse(content="", provider="mock", model="mock", request_id="req", finish_reason="stop")
                return super().complete(system=system, user=user)

        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
        ])
        job = TranslationService(store, PolishFailProvider()).run(project_id, polish=True, parallel_workers=1)
        self.assertEqual(job["status"], "partial_failed")
        counts = store.get_job(project_id, job["job_id"])["result"]["counts"]
        self.assertEqual(counts["translate"]["completed"], 2)
        self.assertEqual(counts["polish"]["target"], 2)
        self.assertEqual(counts["polish"]["failed"], 2)
        # 润色失败不把已成功的初译算成失败。
        self.assertEqual(counts["translate"]["failed"], 0)

    def test_checkpoint_save_keeps_existing_counts(self):
        store, project_id = make_project(self.root, [block("b1", "paragraph", "Only paragraph.")])
        service = TranslationService(store, MockProvider())
        job_id = store.create_job(project_id, "translate", {"scope": "book"}, {})
        store.update_job(project_id, job_id, result={"counts": {"target_blocks": 7}})
        service._save_checkpoint(project_id, job_id, {"pass": "polish", "target_ids": ["b1"]})
        result = store.get_job(project_id, job_id)["result"]
        # checkpoint 写盘不能覆盖已写入的 counts（否则同一次任务的持久化结果自相矛盾）。
        self.assertEqual(result["counts"], {"target_blocks": 7})
        self.assertEqual(result["target_ids"], ["b1"])
        self.assertEqual(result["checkpoint_version"], 1)

    def test_checkpoint_freezes_source_model_and_glossary(self):
        store, project_id = make_project(self.root, [block("b1", "paragraph", "Hello.")])
        TranslationService(store, MockProvider()).run(project_id)
        job = store.list_jobs(project_id)[0]
        frozen = job["result"]["frozen"]
        # 请求前就固定了源文摘要、非敏感模型配置与目标范围。
        self.assertTrue(frozen["source_digest"])
        self.assertEqual(frozen["target_count"], 1)
        self.assertEqual(frozen["locale"], "zh-CN")
        self.assertEqual(frozen["model"]["provider"], "custom")
        self.assertEqual(frozen["model"]["model"], "mock")
        self.assertEqual(frozen["glossary_size"], 0)
        # 恢复同一次任务不得覆盖已冻结的配置（即使后来改了服务/术语表）。
        TranslationService(store, MockProvider()).run(project_id, job_id=job["job_id"])
        again = store.get_job(project_id, job["job_id"])["result"]["frozen"]
        self.assertEqual(again, frozen)

    def test_revision_history_is_flat_and_grows_linearly(self):
        store, project_id = make_project(self.root, [block("b1", "paragraph", "Hello.")])
        service = TranslationService(store, MockProvider())
        service.run(project_id)
        for index in range(10):
            service.edit(project_id, "b1", f"人工译文 {index}")
        revisions = store.load_translation(project_id).entries["b1"].revisions
        self.assertEqual(len(revisions), 10)
        for revision in revisions:
            # 旧实现用 asdict(previous) 把整段历史再嵌一层，体积指数增长。
            self.assertNotIn("revisions", revision)
            self.assertNotIn("candidates", revision)
            self.assertNotIn("selection_translations", revision)
        path = store.project_path(project_id) / "translations" / "zh-CN" / "translation.json"
        # 10 次编辑在旧递归实现下约 447 KB；平面快照应保持在小数量级。
        self.assertLess(path.stat().st_size, 40000)

    def test_sustained_failures_stop_even_after_successes(self):
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(12)
        ])

        class FlakyProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                if self.calls >= 3:
                    raise ProviderError("服务持续失败")
                return super().complete(system=system, user=user)

        provider = FlakyProvider()
        job = TranslationService(store, provider).run(project_id, parallel_workers=1)
        # 连续 3 次失败就熔断：2 次成功 + 3 次失败 = 5 次请求，不再把剩余 7 段全部打出。
        self.assertEqual(provider.calls, 5)
        # 已有成功分块时不能被整本报 failed，否则用户会以为前面的译文作废。
        self.assertEqual(job["status"], "partial_failed")
        self.assertEqual(job["result"]["counts"]["translate"]["completed"], 2)
        self.assertGreaterEqual(job["result"]["counts"]["translate"]["failed"], 10)

    def test_long_single_field_is_split_into_subsegments(self):
        from literature.translation import _split_units, _unit_segments

        text = "word " * 600  # ≈3000 字符，单个字段超过 1200 字符上限
        store, project_id = make_project(self.root, [block("b1", "paragraph", text)])
        prompts: list[str] = []

        class Recording(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                prompts.append(user)
                return super().complete(system=system, user=user)

        TranslationService(store, Recording()).run(project_id, parallel_workers=1)
        entry = store.load_translation(project_id).entries["b1"]
        self.assertEqual(entry.status, "translated")
        # 单字段被预先拆成可独立缩批的子段，而不是一个巨大的不可分 segment。
        self.assertIn("b1#text~0", prompts[0])
        self.assertIn("b1#text~1", prompts[0])
        # 子段结果按序拼回同一个稳定字段。
        self.assertGreaterEqual(entry.text.count("译："), 2)
        self.assertNotIn("~", entry.text)
        # 失败重试时现在可以继续缩批；旧实现只有一个 segment 时 _split_units 返回 (None, None)。
        target = store.load_document(project_id).block("b1")
        left, right = _split_units([(target, _unit_segments(target))])
        self.assertIsNotNone(left)
        self.assertIsNotNone(right)

    def test_long_single_field_is_split_across_multiple_http_requests(self):
        """10000 字符单字段必须首发就拆成多次 HTTP，而不是只加多个协议标签。"""
        import re as _re

        text = "word " * 2000  # ≈10000 字符
        store, project_id = make_project(self.root, [block("b1", "paragraph", text)])
        prompts: list[str] = []

        class Recording(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                prompts.append(user)
                return super().complete(system=system, user=user)

        TranslationService(store, Recording()).run(project_id, parallel_workers=1)
        entry = store.load_translation(project_id).entries["b1"]
        self.assertEqual(entry.status, "translated")
        # 真正拆成多个请求（首个请求不再是 10000 字符）。
        self.assertGreater(len(prompts), 1)
        for prompt in prompts:
            source_chars = sum(len(value) for value in _re.findall(r'<src[^>]*>(.*?)</src>', prompt, flags=_re.S))
            # v3 起 _batch_units 按估算 token（900 token ≈ 英文 3600 字符）计量，
            # 英文批次从旧字符预算的 3200 变宽是预期行为。
            self.assertLessEqual(source_chars, 3700, prompts)
        # 所有子段都拼回同一稳定字段。
        self.assertGreaterEqual(entry.text.count("译："), 5)
        self.assertNotIn("~", entry.text)

    def test_budget_guard_rejects_before_any_request(self):
        """Lead 场景：余额不足时必须在发出任何请求前 budget_paused，而不是先发再完成。"""
        from literature.request_coordinator import RequestCoordinator

        store, project_id = make_project(self.root, [block("b1", "paragraph", "Hello.")])
        store.update_manifest(project_id, budget_usd=0.000001)

        class Costly(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                response = super().complete(system=system, user=user)
                return ProviderResponse(
                    content=response.content, provider="mock", model="mock", request_id="req",
                    input_tokens=10, output_tokens=5, estimated_cost=0.001,
                )

        provider = Costly()
        provider.config.input_price_per_million = 1.0
        provider.config.output_price_per_million = 1.0
        coordinator = RequestCoordinator()
        job = TranslationService(store, provider, coordinator=coordinator, service_id="svc").run(
            project_id, parallel_workers=1,
        )
        self.assertEqual(job["status"], "budget_paused")
        self.assertEqual(provider.calls, 0)
        self.assertEqual(store.usage_summary(project_id)["estimated_cost"], 0)

    def test_unknown_cost_is_marked_not_zero(self):
        """价格/usage 未知时明确记为 unknown，不冒充零成本。"""
        from literature.request_coordinator import RequestCoordinator

        store, project_id = make_project(self.root, [block("b1", "paragraph", "Hello.")])

        class NoUsage(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                response = super().complete(system=system, user=user)
                return ProviderResponse(content=response.content, provider="mock", model="mock", request_id="req")

        coordinator = RequestCoordinator()
        TranslationService(store, NoUsage(), coordinator=coordinator, service_id="svc").run(project_id, parallel_workers=1)
        self.assertGreaterEqual(coordinator.unknown_cost_requests(project_id), 1)
        # 持久账本也表达“有请求费用未知”，不能只显示 $0 当作全确定。
        summary = store.usage_summary(project_id)
        self.assertGreaterEqual(summary["unknown_cost_records"], 1)
        self.assertEqual(summary["estimated_cost"], 0)

    def test_budget_pause_keeps_paid_subsegments_and_resume_does_not_resend(self):
        """已付费子段落为中间证据；预算暂停后恢复只补剩余子段，不重发已付费的。"""
        from literature.request_coordinator import RequestCoordinator
        from literature.translation import _unit_segments

        text = "word " * 2000  # ≈10000 字符 → 多个子段
        store, project_id = make_project(self.root, [block("b1", "paragraph", text)])
        store.update_manifest(project_id, budget_usd=0.01)

        class Costly(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0
                self.prompts: list[str] = []

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                self.prompts.append(user)
                response = super().complete(system=system, user=user)
                return ProviderResponse(
                    content=response.content, provider="mock", model="mock", request_id=f"req{self.calls}",
                    input_tokens=10, output_tokens=5, estimated_cost=0.009,
                )

        coordinator = RequestCoordinator()
        first_provider = Costly()
        first_provider.config.input_price_per_million = 1.0
        first_provider.config.output_price_per_million = 1.0
        job_id = store.create_job(project_id, "translate", {"scope": "book"}, {})
        paused = TranslationService(store, first_provider, coordinator=coordinator, service_id="svc").run(
            project_id, job_id=job_id, parallel_workers=1,
        )
        self.assertEqual(paused["status"], "budget_paused")
        self.assertEqual(first_provider.calls, 1)
        checkpoint = store.get_job(project_id, job_id)["result"]
        paid_record = checkpoint.get("partial_segments", {}).get("b1") or {}
        paid = paid_record.get("segments") or {}
        self.assertTrue(paid, "已付费子段应作为中间证据落盘")
        self.assertEqual(paid_record["source_hash"], block_source_digest(store.load_document(project_id).block("b1")))
        self.assertEqual(checkpoint["partial_meta"]["run_token"], job_id)
        # 未完成不得冒充完成译文。
        self.assertNotIn("b1", {bid for bid, entry in store.load_translation(project_id).entries.items() if entry.status == "translated"})

        # 提高预算，同 job 恢复：不得重发已付费子段。
        store.update_manifest(project_id, budget_usd=1.0)
        resume_provider = Costly()
        resume_provider.config.input_price_per_million = 1.0
        resume_provider.config.output_price_per_million = 1.0
        resumed = TranslationService(store, resume_provider, coordinator=coordinator, service_id="svc").run(
            project_id, job_id=job_id, parallel_workers=1,
        )
        self.assertEqual(resumed["status"], "completed")
        for seg_id in paid:
            for prompt in resume_provider.prompts:
                self.assertNotIn(seg_id, prompt, f"已付费子段 {seg_id} 被重发")
        entry = store.load_translation(project_id).entries["b1"]
        self.assertEqual(entry.status, "translated")
        # 所有子段都进入最终拼接，不丢半句。
        self.assertEqual(entry.text.count("译："), len(_unit_segments(store.load_document(project_id).block("b1"))))

    def test_partial_segments_are_discarded_when_source_changes(self):
        """源文变化后旧中间子段必须丢弃，不能拼成当前完整结果。"""
        from literature.request_coordinator import RequestCoordinator

        text = "word " * 2000
        store, project_id = make_project(self.root, [block("b1", "paragraph", text)])
        store.update_manifest(project_id, budget_usd=0.01)

        class Costly(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0
                self.prompts: list[str] = []

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                self.prompts.append(user)
                response = super().complete(system=system, user=user)
                return ProviderResponse(
                    content=response.content, provider="mock", model="mock", request_id=f"req{self.calls}",
                    input_tokens=10, output_tokens=5, estimated_cost=0.009,
                )

        coordinator = RequestCoordinator()
        first = Costly()
        first.config.input_price_per_million = 1.0
        first.config.output_price_per_million = 1.0
        job_id = store.create_job(project_id, "translate", {"scope": "book"}, {})
        paused = TranslationService(store, first, coordinator=coordinator, service_id="svc").run(
            project_id, job_id=job_id, parallel_workers=1,
        )
        self.assertEqual(paused["status"], "budget_paused")
        paid = (store.get_job(project_id, job_id)["result"]["partial_segments"]["b1"]["segments"])

        # 修改源文前 15 字（长度保持不变）：旧中间子段源 hash 不再匹配。
        document = store.load_document(project_id)
        changed = "CHANGED SOURCE " + text[15:]
        self.assertEqual(len(changed), len(text))
        document.block("b1").source_text = changed
        store.save_document(document)

        store.update_manifest(project_id, budget_usd=1.0)
        resume_provider = Costly()
        resume_provider.config.input_price_per_million = 1.0
        resume_provider.config.output_price_per_million = 1.0
        resumed = TranslationService(store, resume_provider, coordinator=coordinator, service_id="svc").run(
            project_id, job_id=job_id, parallel_workers=1,
        )
        self.assertEqual(resumed["status"], "completed")
        entry = store.load_translation(project_id).entries["b1"]
        # 新源文必须被实际重发/重译（旧中间子段已因源 hash 不匹配被丢弃）。
        self.assertTrue(any("CHANGED SOURCE" in prompt for prompt in resume_provider.prompts), resume_provider.prompts)
        self.assertIn("CHANGED SOURCE", entry.text)

    def test_crash_mid_stage_persists_paid_subsegments(self):
        """未设预算时第 2 个请求崩溃：第 1 个已付费子段必须先落盘，恢复不重发。"""
        text = "word " * 2000
        store, project_id = make_project(self.root, [block("b1", "paragraph", text)])

        class CrashSecond(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                if self.calls >= 2:
                    raise KeyboardInterrupt("模拟崩溃")
                return super().complete(system=system, user=user)

        job_id = store.create_job(project_id, "translate", {"scope": "book"}, {})
        with self.assertRaises(KeyboardInterrupt):
            TranslationService(store, CrashSecond()).run(project_id, job_id=job_id, parallel_workers=1)
        checkpoint = store.get_job(project_id, job_id)["result"]
        paid = (checkpoint.get("partial_segments", {}).get("b1") or {}).get("segments") or {}
        self.assertTrue(paid, "崩溃前已付费子段必须先落盘")

        class Counting(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.prompts: list[str] = []

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.prompts.append(user)
                return super().complete(system=system, user=user)

        resume_provider = Counting()
        resumed = TranslationService(store, resume_provider).run(project_id, job_id=job_id, parallel_workers=1)
        self.assertEqual(resumed["status"], "completed")
        for seg_id in paid:
            for prompt in resume_provider.prompts:
                self.assertNotIn(seg_id, prompt, f"崩溃前已付费子段 {seg_id} 被重发")

    def test_pending_cost_persists_unknown_across_restart(self):
        """超时/无价/无 usage 的费用未知必须持久，重启后仍明确 unknown。"""
        store, project_id = make_project(self.root, [block("b1", "paragraph", "Hello.")])

        class NoUsage(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                response = super().complete(system=system, user=user)
                return ProviderResponse(content=response.content, provider="mock", model="mock", request_id="req")

        TranslationService(store, NoUsage()).run(project_id, parallel_workers=1)
        summary = store.usage_summary(project_id)
        # 有响应但缺 usage/单价：计入持久 unknown_cost_records（NULL 行），不重复写 pending。
        self.assertGreaterEqual(summary["unknown_cost_records"], 1)
        self.assertEqual(summary["pending_cost_records"], 0)
        # 重启（新 store 实例）后仍可统计。
        from literature.storage import ProjectStore
        fresh = ProjectStore(store.root)
        self.assertGreaterEqual(fresh.usage_summary(project_id)["unknown_cost_records"], 1)

        # 超时（无响应）：也要有持久待对账证据。
        store2, project2 = make_project(self.root / "timeout", [block("b1", "paragraph", "Hello.")])

        class Timeout(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                raise ProviderError("模型请求超时")

        with self.assertRaises(ProviderError):
            TranslationService(store2, Timeout()).run(project2, parallel_workers=1)
        self.assertGreaterEqual(store2.usage_summary(project2)["pending_cost_records"], 1)

    def test_budget_seed_failure_blocks_spending(self):
        from literature.request_coordinator import RequestCoordinator

        def bad_loader(pid: str) -> float:
            raise RuntimeError("账本不可读")

        coordinator = RequestCoordinator(spent_loader=bad_loader)
        # 预算不明时不得 seed 为 0 继续花；保守拒绝。
        self.assertIsNone(coordinator.reserve("p", cost=0.1, budget=1.0))
        # 无预算限制时允许（不依赖账本）。
        self.assertIsNotNone(coordinator.reserve("p", cost=0.1, budget=0.0))

    def test_chunks_break_on_chapter_and_language_boundaries(self):
        from literature.translation import _chunks

        h1 = block("h1", "title", "Chapter One")
        p1 = block("p1", "paragraph", "Body one.")
        h2 = block("h2", "title", "Chapter Two")
        # 章节边界：标题开启新块，不与上一章正文混。
        chunks = _chunks([h1, p1, h2])
        self.assertEqual([[b.block_id for b in chunk] for chunk in chunks], [["h1", "p1"], ["h2"]])
        # 语言边界（无标题分隔时也要拆）。
        chunks2 = _chunks([h1, p1, h2], languages={"h1": "en", "p1": "en", "h2": "ja"})
        self.assertEqual([[b.block_id for b in chunk] for chunk in chunks2], [["h1", "p1"], ["h2"]])
        p2 = block("p2", "paragraph", "Body two.")
        chunks3 = _chunks([h1, p1, p2], languages={"h1": "en", "p1": "en", "p2": "ja"})
        self.assertEqual([[b.block_id for b in chunk] for chunk in chunks3], [["h1", "p1"], ["p2"]])

    def test_targeted_review_returns_candidate_without_auto_save(self):
        """定向译审对照原文+现译产出候选，不自动覆盖；锁定内容由用户显式采纳。"""
        import re as _re

        store, project_id = make_project(self.root, [block("b1", "paragraph", "Original sentence.")])
        TranslationService(store, MockProvider()).run(project_id, parallel_workers=1)
        before = store.load_translation(project_id).entries["b1"].text

        class Revising(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                ids = _re.findall(r'<cur id="([^"]+)"', user)
                return ProviderResponse(
                    content="\n".join(f'<seg id="{item}">修订：{item}</seg>' for item in ids),
                    provider="mock", model="mock", request_id="req",
                )

        result = TranslationService(store, Revising()).review(project_id, "b1", locale="zh-CN")
        self.assertTrue(result["changed"])
        self.assertTrue(result["complete"])
        self.assertTrue(result["revised"]["text"].startswith("修订："))
        # 候选身份/版本必须返回，供切项目/语言与并发校验。
        self.assertEqual(result["project_id"], project_id)
        self.assertEqual(result["locale"], "zh-CN")
        self.assertEqual(result["block_id"], "b1")
        self.assertTrue(result["source_hash"] and result["base_revision"] and result["candidate_id"])
        # 不自动保存：原条目不变。
        self.assertEqual(store.load_translation(project_id).entries["b1"].text, before)
        # 生命周期：review 作业有真实终态，不是一直 queued。
        review_jobs = [job for job in store.list_jobs(project_id) if job["kind"] == "review"]
        self.assertEqual(review_jobs[0]["status"], "completed")

    def test_review_adopt_validates_source_and_translation_baseline(self):
        import re as _re

        store, project_id = make_project(self.root, [block("b1", "paragraph", "Original sentence.")])
        TranslationService(store, MockProvider()).run(project_id, parallel_workers=1)

        class Revising(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                ids = _re.findall(r'<cur id="([^"]+)"', user)
                return ProviderResponse(
                    content="\n".join(f'<seg id="{item}">修订：{item}</seg>' for item in ids),
                    provider="mock", model="mock", request_id="req",
                )

        service = TranslationService(store, Revising())
        candidate = service.review(project_id, "b1", locale="zh-CN")
        # 源变 → 拒绝。
        document = store.load_document(project_id)
        document.block("b1").source_text = "Changed source text"
        store.save_document(document)
        with self.assertRaises(Exception):
            service.adopt_review(project_id, "b1", locale="zh-CN", revised=candidate["revised"],
                                 source_hash=candidate["source_hash"], base_revision=candidate["base_revision"])
        # 源改回，但译文被人工修改 → 基线不符，拒绝并保留人工内容。
        document.block("b1").source_text = "Original sentence."
        store.save_document(document)
        TranslationService(store, MockProvider()).edit(project_id, "b1", "人工最新译文")
        with self.assertRaises(Exception):
            service.adopt_review(project_id, "b1", locale="zh-CN", revised=candidate["revised"],
                                 source_hash=candidate["source_hash"], base_revision=candidate["base_revision"])
        self.assertEqual(store.load_translation(project_id).entries["b1"].text, "人工最新译文")
        # 用当前基线重新采纳 → 成功，且修订历史保留采纳前版本。
        current = store.load_translation(project_id).entries["b1"]
        adopted = service.adopt_review(project_id, "b1", locale="zh-CN", revised=candidate["revised"],
                                       source_hash=candidate["source_hash"], base_revision=current.updated_at)
        self.assertTrue(adopted.locked)
        self.assertTrue(any(revision.get("text") == "人工最新译文" for revision in adopted.revisions))

    def test_review_budget_paused_sets_terminal_status(self):
        store, project_id = make_project(self.root, [block("b1", "paragraph", "Original sentence.")])
        TranslationService(store, MockProvider()).run(project_id, parallel_workers=1)
        store.update_manifest(project_id, budget_usd=0.000001)

        class Costly(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                response = super().complete(system=system, user=user)
                return ProviderResponse(content=response.content, provider="mock", model="mock", request_id="req", estimated_cost=0.001)

        provider = Costly()
        provider.config.input_price_per_million = 1.0
        provider.config.output_price_per_million = 1.0
        with self.assertRaises(Exception):
            TranslationService(store, provider).review(project_id, "b1", locale="zh-CN")
        review_jobs = [job for job in store.list_jobs(project_id) if job["kind"] == "review"]
        self.assertEqual(review_jobs[0]["status"], "budget_paused")

    def test_adopt_review_is_atomic_against_concurrent_manual_edit(self):
        """Lead 复现：采纳校验后/写入前并发人工编辑，不能被旧候选覆盖。"""
        import threading

        store, project_id = make_project(self.root, [block("b1", "paragraph", "Original sentence.")])
        TranslationService(store, MockProvider()).run(project_id, parallel_workers=1)
        service = TranslationService(store, MockProvider())
        service.edit(project_id, "b1", "审校前译文")
        entry = store.load_translation(project_id).entries["b1"]
        revised = {"text": "旧候选覆盖内容", "items": [], "caption": [], "footnote": [], "cells": {}}

        done = threading.Event()
        started = threading.Event()
        original_edit = service.edit

        def intercepted(*args, **kwargs):
            if not started.is_set():
                started.set()
                def human() -> None:
                    TranslationService(store, MockProvider()).edit(project_id, "b1", "人工最新译文")
                    done.set()
                threading.Thread(target=human).start()
                done.wait(1.0)  # 人工编辑若在采纳临界区内会被阻塞
            return original_edit(*args, **kwargs)

        service.edit = intercepted  # type: ignore[method-assign]
        service.adopt_review(
            project_id, "b1", locale="zh-CN", revised=revised,
            source_hash=entry.source_hash, base_revision=entry.updated_at,
        )
        done.wait(2.0)
        final = store.load_translation(project_id).entries["b1"]
        # 人工编辑不得被旧候选覆盖；且其版本应可查看。
        self.assertEqual(final.text, "人工最新译文")
        self.assertTrue(any(revision.get("text") == "旧候选覆盖内容" for revision in final.revisions))

    def test_adopt_review_rejects_missing_basis_and_incomplete_candidate(self):
        from literature.storage import StorageError

        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "Original sentence."),
            block("t1", "table", cells=[Cell("c1", 0, 0, "first"), Cell("c2", 0, 1, "second")]),
        ])
        TranslationService(store, MockProvider()).run(project_id, parallel_workers=1)
        service = TranslationService(store, MockProvider())
        entry = store.load_translation(project_id).entries["b1"]
        # 缺版本依据 → 拒绝。
        with self.assertRaises(StorageError):
            service.adopt_review(project_id, "b1", locale="zh-CN", revised={"text": "x"}, source_hash="", base_revision="")
        # 表格候选缺一个 cell → 完整性校验拒绝。
        table_entry = store.load_translation(project_id).entries["t1"]
        incomplete = {"text": "", "items": [], "caption": [], "footnote": [], "cells": {"c1": "第一格"}}
        with self.assertRaises(StorageError):
            service.adopt_review(
                project_id, "t1", locale="zh-CN", revised=incomplete,
                source_hash=table_entry.source_hash, base_revision=table_entry.updated_at,
            )
        # 拒绝后不改库。
        self.assertEqual(store.load_translation(project_id).entries["b1"].text, entry.text)
        self.assertEqual(store.load_translation(project_id).entries["t1"].cells, table_entry.cells)

    def test_shared_coordinator_budget_is_atomic_under_stale_read(self):
        """Lead 场景：两路共享同一 coordinator，后一路不得用过期 spent 再花一笔。"""
        import threading

        from literature.request_coordinator import RequestCoordinator

        coordinator = RequestCoordinator()
        # 第一笔：reserve → reconcile 落账（模拟正常完成）。
        first = coordinator.reserve("p", cost=0.000202, budget=0.0003)
        self.assertIsNotNone(first)
        coordinator.reconcile(first, actual_cost=0.000202)
        # 第二笔：即使另一路拿着旧的 spent=0，锁内也读最新已花，必须被拦。
        self.assertIsNone(coordinator.reserve("p", cost=0.000202, budget=0.0003))
        snapshot = coordinator.snapshot("p")
        self.assertAlmostEqual(snapshot["spent_cost"], 0.000202, places=8)
        self.assertEqual(snapshot["reserved_cost"], 0.0)

        # 并发版本：50 笔小额只能通过预算允许的数量。
        concurrent = RequestCoordinator()
        granted: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            reservation = concurrent.reserve("q", cost=0.0001, budget=0.00025)
            with lock:
                granted.append(reservation is not None)
            if reservation is not None:
                concurrent.reconcile(reservation, actual_cost=0.0001)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 预算 0.00025 / 每笔 0.0001 → 最多 2 笔；在途与已花都在锁内维护，不会超卖。
        self.assertLessEqual(sum(granted), 2)
        self.assertGreaterEqual(sum(granted), 1)

    def test_inflight_budget_reservation_stops_before_overspending(self):
        """并发下只看“已花掉”会超预算；在途预留应在发出更多请求前停下。"""
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(4)
        ])
        store.update_manifest(project_id, budget_usd=0.001)
        provider = CountingProvider()
        provider.config.input_price_per_million = 1.0
        provider.config.output_price_per_million = 1.0
        job = TranslationService(store, provider).run(project_id, parallel_workers=4)
        self.assertEqual(job["status"], "budget_paused")
        # 若没有在途预留，4 路并发会先全部发出；预留后最多发出 1 块。
        self.assertLessEqual(provider.calls, 1)

    def test_force_failure_keeps_previous_translation_as_content(self):
        """force 重译失败时保留旧译文内容（不清空），但它不计为本轮完成。"""
        store, project_id = make_project(self.root, [block("b1", "paragraph", "Hello world.")])
        TranslationService(store, MockProvider()).run(project_id)
        original = store.load_translation(project_id).entries["b1"].text

        class AlwaysFails(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                raise ProviderError("服务失败")

        with self.assertRaises(ProviderError):
            TranslationService(store, AlwaysFails()).run(project_id, force=True, parallel_workers=1)
        entry = store.load_translation(project_id).entries["b1"]
        self.assertEqual(entry.status, "failed")
        self.assertTrue(entry.partial)
        # 旧译文作为保留内容留在条目里；但 partial=True 表示它不是本轮完成。
        self.assertEqual(entry.text, original)

    def test_force_three_blocks_pause_marks_untouched_as_unhandled(self):
        """Lead 场景：3 块旧译 force 重译，单路完成 1 块暂停；未发送的 2 块是 unhandled，不是 failed。"""
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{'word ' * 400}{index}") for index in range(3)
        ])
        TranslationService(store, MockProvider()).run(project_id, parallel_workers=1)
        provider = CountingProvider()
        paused = TranslationService(store, provider).run(
            project_id, scope="book", force=True, parallel_workers=1,
            should_pause=lambda: provider.calls >= 1,
        )
        self.assertEqual(paused["status"], "paused")
        counts = store.get_job(project_id, paused["job_id"])["result"]["counts"]
        self.assertEqual(counts["translate"], {
            "target": 3, "completed": 1, "failed": 0, "locked": 0, "unhandled": 2, "source_changed": 0,
        })
        # checkpoint 只记录本轮完成证据。
        self.assertEqual(store.get_job(project_id, paused["job_id"])["result"]["translate_completed"], ["b0"])

    def test_project_path_index_is_cached_and_rebuildable(self):
        from literature.storage import ProjectStore, StorageError

        root = self.root / "index"
        root.mkdir()
        store = ProjectStore(root / "library")
        pdf = root / "fixture.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% fixture")
        first = store.create_project(pdf, title="First")["project_id"]
        store.project_path(first)  # 预热索引

        original = store._build_project_index
        calls = {"n": 0}

        def counted():
            calls["n"] += 1
            return original()

        store._build_project_index = counted
        for _ in range(5):
            self.assertEqual(store.project_path(first).name, "First")
        # 已建立索引后重复查询不应再全库扫描。
        self.assertEqual(calls["n"], 0)

        # 另一实例/进程新建的项目，本实例的陈旧缓存应重建后找到。
        external = ProjectStore(store.root)
        second = external.create_project(pdf, title="Second")["project_id"]
        self.assertEqual(store.project_path(second).name, "Second")
        self.assertGreaterEqual(calls["n"], 1)

        # 移出资源库后重建索引不再命中。
        store.delete_project(second)
        with self.assertRaises(StorageError):
            store.project_path(second)

    def test_task_accounting_partition_sums_to_target(self):
        store, project_id = make_project(self.root, [block("b1", "paragraph", "Only paragraph.")])
        document = store.load_document(project_id)
        layer = store.load_translation(project_id)
        # 已完成且被人工锁定：这是 Lead 复现的重叠边界。
        layer.entries["b1"] = TranslationEntry(
            block_id="b1", status="translated", locked=True, text="人工译文",
            source_hash=block_source_digest(document.block("b1")),
        )
        counts, reasons = TranslationService._task_accounting(
            layer, target_ids=["b1"], translate_completed=["b1"],
            polish_target_ids=[], polish_completed=[], translate_failed={}, polish_failed={},
            source_changed_ids=set(),
        )
        tr = counts["translate"]
        # 已成功完成者保持 completed；locked 不再重复统计同一块。
        self.assertEqual(tr["completed"], 1)
        self.assertEqual(tr["locked"], 0)
        self.assertEqual(tr["failed"], 0)
        self.assertEqual(tr["unhandled"], 0)
        # 四类互斥且和恒等于目标。
        self.assertEqual(tr["completed"] + tr["failed"] + tr["locked"] + tr["unhandled"], counts["target_blocks"])
        self.assertEqual(reasons, {})

    def test_task_accounting_locked_unfinished_target_is_separate_class(self):
        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
        ])
        document = store.load_document(project_id)
        layer = store.load_translation(project_id)
        layer.entries["b2"] = TranslationEntry(
            block_id="b2", status="edited", locked=True, text="人工译文",
            source_hash=block_source_digest(document.block("b2")),
        )
        counts, _reasons = TranslationService._task_accounting(
            layer, target_ids=["b1", "b2"], translate_completed=["b1"],
            polish_target_ids=[], polish_completed=[], translate_failed={}, polish_failed={},
            source_changed_ids=set(),
        )
        tr = counts["translate"]
        self.assertEqual(tr["completed"], 1)
        self.assertEqual(tr["locked"], 1)
        self.assertEqual(tr["failed"], 0)
        self.assertEqual(tr["unhandled"], 0)
        self.assertEqual(tr["completed"] + tr["failed"] + tr["locked"] + tr["unhandled"], counts["target_blocks"])

    def test_resume_after_completed_target_locked_does_not_double_count(self):
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(2)
        ])
        provider = CountingProvider()
        service = TranslationService(store, provider)
        paused = service.run(project_id, parallel_workers=1, should_pause=lambda: provider.calls >= 1)
        self.assertEqual(paused["status"], "paused")
        # 暂停后把已完成段人工编辑并锁定（类似用户在恢复前介入）。
        service.edit(project_id, "b0", "人工锁定译文")
        resumed = service.run(project_id, parallel_workers=1, job_id=paused["job_id"])
        self.assertEqual(resumed["status"], "completed")
        counts = store.get_job(project_id, resumed["job_id"])["result"]["counts"]
        tr = counts["translate"]
        # 目标仍是完整 2 段，且 b0 只能在 completed/locked 中计一次。
        self.assertEqual(counts["target_blocks"], 2)
        self.assertEqual(tr["completed"] + tr["failed"] + tr["locked"] + tr["unhandled"], 2)

    def test_resume_after_polish_partial_failed_retries_missing_initial(self):
        class OnlySecondProvider(MockProvider):
            """只回 b2 的译文，b1 缺失。"""

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
                content = "\n".join(f'<seg id="{sid}">译：{text.strip()}</seg>' for sid, text in segments if sid.startswith("b2#"))
                return ProviderResponse(content=content, provider="mock", model="mock", request_id="req", finish_reason="stop")

        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
        ])
        job_id = store.create_job(project_id, "translate", {"scope": "book"}, {"polish": True})
        first = TranslationService(store, OnlySecondProvider()).run(project_id, polish=True, parallel_workers=1, job_id=job_id)
        self.assertEqual(first["status"], "partial_failed")
        checkpoint = store.get_job(project_id, job_id)["result"]
        self.assertEqual(checkpoint["pass"], "polish")
        self.assertEqual(store.load_translation(project_id).entries["b1"].status, "failed")
        # 恢复：b1 根本未初译成功，必须先补初译再润色；b2 已初译+已润色，不再重复计费。
        provider = CountingProvider()
        resumed = TranslationService(store, provider).run(project_id, polish=True, parallel_workers=1, job_id=job_id)
        self.assertEqual(resumed["status"], "completed")
        layer = store.load_translation(project_id)
        self.assertEqual(layer.entries["b1"].status, "refined")
        self.assertEqual(layer.entries["b2"].status, "refined")
        # 2 次请求：b1 初译 + b1 润色；b2 不被重新初译/润色。
        self.assertEqual(provider.calls, 2)

    def test_source_change_during_run_is_not_reported_completed(self):
        class MutatingProvider(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                document = store.load_document(project_id)
                document.block("b1").source_text = "Changed during run."
                store.save_document(document)
                return super().complete(system=system, user=user)

        store, project_id = make_project(self.root, [block("b1", "paragraph", "Original paragraph.")])
        job = TranslationService(store, MutatingProvider()).run(project_id, parallel_workers=1)
        # 源文在任务期间变化：提交守卫只保存候选，job 不能伪报 completed。
        self.assertEqual(job["status"], "partial_failed")
        counts = store.get_job(project_id, job["job_id"])["result"]["counts"]
        tr = counts["translate"]
        self.assertEqual(tr["source_changed"], 1)
        self.assertEqual(tr["completed"], 0)
        self.assertEqual(tr["failed"], 1)
        self.assertEqual(tr["locked"], 0)
        self.assertEqual(tr["unhandled"], 0)
        self.assertEqual(tr["completed"] + tr["failed"] + tr["locked"] + tr["unhandled"], counts["target_blocks"])
        entry = store.load_translation(project_id).entries["b1"]
        self.assertNotEqual(entry.status, "translated")
        self.assertTrue(entry.candidates)
        # 源稳定后恢复：以当前源重译成功，source_changed 归零，任务变 completed。
        resumed = TranslationService(store, MockProvider()).run(project_id, job_id=job["job_id"], parallel_workers=1)
        self.assertEqual(resumed["status"], "completed")
        counts = store.get_job(project_id, job["job_id"])["result"]["counts"]
        self.assertEqual(counts["translate"]["source_changed"], 0)
        self.assertEqual(counts["translate"]["completed"], 1)
        self.assertEqual(store.load_translation(project_id).entries["b1"].status, "translated")

    def test_truncated_child_never_becomes_pseudo_complete(self):
        class TruncatingProvider(MockProvider):
            """父请求与第一个子请求都截断（标签闭合但 finish_reason=length）。"""

            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                response = super().complete(system=system, user=user)
                if self.calls <= 2:
                    return ProviderResponse(
                        content=response.content, provider="mock", model="mock",
                        request_id="req", finish_reason="length",
                    )
                return response

        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
        ])
        job = TranslationService(store, TruncatingProvider()).run(project_id, parallel_workers=1)
        layer = store.load_translation(project_id)
        # 截断父批 + 截断左子批：b1 不得因标签闭合而被当作完整。
        self.assertEqual(layer.entries["b1"].status, "failed")
        self.assertEqual(layer.entries["b1"].text, "")
        self.assertEqual(layer.entries["b2"].status, "translated")
        self.assertEqual(job["status"], "partial_failed")

    def test_large_table_split_retry_keeps_every_cell(self):
        class MissingOneCellProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
                if self.calls == 1:
                    content = "\n".join(f'<seg id="{sid}">译：{text.strip()}</seg>' for sid, text in segments[1:])
                    return ProviderResponse(content=content, provider="mock", model="mock", request_id="req", finish_reason="stop")
                return super().complete(system=system, user=user)

        cells = [Cell(f"c{index}", 0, index, f"cell {index}") for index in range(15)]
        store, project_id = make_project(self.root, [block("t1", "table", cells=cells)])
        provider = MissingOneCellProvider()
        job = TranslationService(store, provider).run(project_id, parallel_workers=1)
        self.assertEqual(job["status"], "completed")
        # 15 字段 > 12 上限，首轮又漏一格，必须切分重试后才能完成。
        self.assertGreaterEqual(provider.calls, 3)
        entry = store.load_translation(project_id).entries["t1"]
        self.assertEqual(entry.status, "translated")
        for cell in cells:
            self.assertTrue(entry.cells.get(cell.cell_id), cell.cell_id)

    def test_overlong_single_text_field_is_split_and_never_saves_truncated_half(self):
        class TruncatingOnceProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                response = super().complete(system=system, user=user)
                if self.calls == 1:
                    return ProviderResponse(
                        content=response.content, provider="mock", model="mock",
                        request_id="req", finish_reason="length",
                    )
                return response

        store, project_id = make_project(self.root, [block("b1", "paragraph", "word " * 500)])
        provider = TruncatingOnceProvider()
        result = TranslationService(store, provider).run(project_id, parallel_workers=1)
        # 第一次整段响应被截断；预拆子段后可继续缩批重试并成功，而不是“单字段不可切分”直接失败。
        self.assertGreaterEqual(provider.calls, 2)
        entry = store.load_translation(project_id).entries["b1"]
        self.assertEqual(entry.status, "translated")
        self.assertEqual(result["status"], "completed")
        # 子段按序拼回同一稳定字段，不把被截断的半句落库。
        self.assertGreaterEqual(entry.text.count("译："), 2)
        self.assertNotIn("~", entry.text)

        # 若模型始终截断，仍必须失败且不保存半句。
        class AlwaysTruncating(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                response = super().complete(system=system, user=user)
                return ProviderResponse(
                    content=response.content, provider="mock", model="mock",
                    request_id="req", finish_reason="length",
                )

        always_root = self.root / "always"
        always_root.mkdir()
        store2, project2 = make_project(always_root, [block("b1", "paragraph", "word " * 500)])
        with self.assertRaises(ProviderError):
            TranslationService(store2, AlwaysTruncating()).run(project2, parallel_workers=1)
        failed = store2.load_translation(project2).entries["b1"]
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.text, "")

    def test_resume_after_polish_failure_only_retries_polish(self):
        class PolishFailsOnceProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.polish_calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                if "润色要求" in system:
                    self.polish_calls += 1
                    if self.polish_calls == 1:
                        return ProviderResponse(content="", provider="mock", model="mock", request_id="req", finish_reason="stop")
                return super().complete(system=system, user=user)

        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
        ])
        job_id = store.create_job(project_id, "translate", {"scope": "book"}, {"polish": True})
        first = TranslationService(store, PolishFailsOnceProvider()).run(project_id, polish=True, parallel_workers=1, job_id=job_id)
        self.assertEqual(first["status"], "partial_failed")
        layer = store.load_translation(project_id)
        self.assertTrue(all(entry.status == "translated" for entry in layer.entries.values()))
        # 恢复只补润色，不重新初译（避免重复计费）。
        provider = CountingProvider()
        resumed = TranslationService(store, provider).run(project_id, polish=True, parallel_workers=1, job_id=job_id)
        self.assertEqual(resumed["status"], "completed")
        layer = store.load_translation(project_id)
        self.assertTrue(all(entry.status == "refined" for entry in layer.entries.values()))
        self.assertEqual(provider.calls, 1)

    def test_f04_level_change_sends_zero_model_requests(self):
        store, project_id = make_project(self.root, [
            block("h1", "title", "Introduction", order=0),
            block("p1", "paragraph", "Body text.", order=1),
        ])
        TranslationService(store, MockProvider()).run(project_id)
        apply_patch(store, project_id, {"block_id": "h1", "operation": "set_level", "after": 2}, source="user")
        document = store.load_document(project_id)
        layer = store.load_translation(project_id)
        self.assertEqual(entry_stale_reason(document.block("h1"), layer.entries["h1"]), "structure")
        # 纯层级变化不进入重译范围，也不会发出任何模型请求。
        job = TranslationService(store, ExplodingProvider()).run(project_id)
        self.assertEqual(job["status"], "completed")
        refreshed = store.load_document(project_id)
        aligned = store.load_translation(project_id)
        self.assertEqual(entry_stale_reason(refreshed.block("h1"), aligned.entries["h1"]), None)
        self.assertEqual(aligned.entries["h1"].structure_revision, refreshed.block("h1").structure_revision)

    def test_f05_pause_during_polish_is_not_completed(self):
        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
        ])
        provider = CountingProvider()
        # 初译分块完成后（1 次请求）立刻在润色阶段暂停。
        job = TranslationService(store, provider).run(project_id, polish=True, parallel_workers=1, should_pause=lambda: provider.calls >= 1)
        self.assertEqual(job["status"], "paused")
        self.assertLess(job["progress"], 1.0)
        layer = store.load_translation(project_id)
        self.assertTrue(all(entry.status == "translated" for entry in layer.entries.values()))
        stats = translation_stats(store.load_document(project_id), layer)
        self.assertEqual(stats["done"], 2)

    def test_f05_resume_finishes_polish_pass(self):
        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "First paragraph."),
            block("b2", "paragraph", "Second paragraph."),
        ])
        provider = CountingProvider()
        job_id = store.create_job(project_id, "translate", {"scope": "book"}, {"polish": True})
        service = TranslationService(store, provider)
        paused = service.run(project_id, polish=True, parallel_workers=1, job_id=job_id, should_pause=lambda: provider.calls >= 1)
        self.assertEqual(paused["status"], "paused")
        # 恢复：初译已完成，应从润色阶段继续到完成。
        done = service.run(project_id, polish=True, parallel_workers=1, job_id=job_id)
        self.assertEqual(done["status"], "completed")
        layer = store.load_translation(project_id)
        self.assertTrue(all(entry.status == "refined" for entry in layer.entries.values()))

    def test_f02_unverified_partial_is_candidate_not_content(self):
        table = block("t1", "table", cells=[Cell("c1", 0, 0, "first"), Cell("c2", 0, 1, "second")])
        store, project_id = make_project(self.root, [table])

        class FirstMissingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls: list[str] = []
                self.drop_c1 = True

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls.append(user)
                segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
                parts = [f'<seg id="{sid}">译：{text.strip()}</seg>' for sid, text in segments if not (self.drop_c1 and "c1" in sid)]
                return ProviderResponse(content="\n".join(parts), provider="mock", model="mock", request_id="req", finish_reason="stop")

        provider = FirstMissingProvider()
        service = TranslationService(store, provider)
        with self.assertRaises(ProviderError):
            service.run(project_id)
        entry = store.load_translation(project_id).entries["t1"]
        self.assertEqual(entry.status, "failed")
        # 未通过整块校验的片段不得写入内容，只作为候选保留。
        self.assertEqual(entry.cells.get("c2", ""), "")
        self.assertTrue(entry.partial)
        self.assertTrue(any(candidate.get("cells", {}).get("c2") for candidate in entry.candidates))
        requests_before = len(provider.calls)
        provider.drop_c1 = False
        service.run(project_id, scope="failed")
        # 重试必须全字段重发（不拿未校验旧片段做补缺依据），最终各 cell 为当前源文翻译。
        retried = provider.calls[requests_before:]
        self.assertTrue(retried)
        self.assertTrue(all("c1" in request and "c2" in request for request in retried))
        final = store.load_translation(project_id).entries["t1"]
        self.assertEqual(final.status, "translated")
        self.assertFalse(final.partial)
        self.assertEqual(final.cells["c1"], "译：first")
        self.assertEqual(final.cells["c2"], "译：second")

    def test_source_changed_partial_retry_uses_current_source(self):
        table = block("t1", "table", cells=[Cell("c1", 0, 0, "OLD"), Cell("c2", 0, 1, "second")])
        store, project_id = make_project(self.root, [table])
        document = store.load_document(project_id)
        layer = store.load_translation(project_id)
        # 手工写入一个“旧源”上的失败局部内容。
        layer.entries["t1"] = TranslationEntry(
            block_id="t1", status="failed", source_hash=block_source_digest(document.block("t1")),
            cells={"c1": "旧译文", "c2": ""},
        )
        store.save_translation(layer)
        # 修改 canonical 源：c1 的 OLD -> NEW。
        changed = store.load_document(project_id)
        changed.block("t1").cells[0].text = "NEW"
        store.save_document(changed)
        TranslationService(store, MockProvider()).run(project_id)
        entry = store.load_translation(project_id).entries["t1"]
        self.assertEqual(entry.status, "translated")
        self.assertEqual(entry.cells["c1"], "译：NEW")
        self.assertEqual(entry.cells["c2"], "译：second")
        self.assertEqual(entry.source_hash, block_source_digest(store.load_document(project_id).block("t1")))

    def test_force_partial_retry_ignores_previous_partial(self):
        table = block("t1", "table", cells=[Cell("c1", 0, 0, "first"), Cell("c2", 0, 1, "second")])
        store, project_id = make_project(self.root, [table])
        document = store.load_document(project_id)
        layer = store.load_translation(project_id)
        layer.entries["t1"] = TranslationEntry(
            block_id="t1", status="translated", source_hash=block_source_digest(document.block("t1")),
            cells={"c1": "人工旧值", "c2": ""},
        )
        store.save_translation(layer)

        class RecordingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls: list[str] = []

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls.append(user)
                return super().complete(system=system, user=user)

        provider = RecordingProvider()
        TranslationService(store, provider).run(project_id, force=True)
        # 强制重译必须全字段重发，而不是只补空的 c2。
        self.assertTrue(provider.calls)
        self.assertTrue(all("c1" in request and "c2" in request for request in provider.calls))
        entry = store.load_translation(project_id).entries["t1"]
        self.assertEqual(entry.cells["c1"], "译：first")
        self.assertEqual(entry.cells["c2"], "译：second")
        self.assertFalse(entry.partial)

    def test_commit_rechecks_canonical_source_hash(self):
        # provider 在响应前修改 canonical 源：提交时复查发现源已变，不得把旧源结果写成当前完成。
        store, project_id = make_project(self.root, [block("b1", "paragraph", "OLD")])

        class SourceChangingProvider(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.done = False

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                if not self.done:
                    self.done = True
                    document = store.load_document(project_id)
                    document.block("b1").source_text = "NEW"
                    store.save_document(document)
                return super().complete(system=system, user=user)

        TranslationService(store, SourceChangingProvider()).run(project_id)
        entry = store.load_translation(project_id).entries["b1"]
        self.assertEqual(entry.status, "failed")
        self.assertTrue(entry.partial)
        self.assertTrue(entry.candidates)
        current = store.load_document(project_id).block("b1")
        self.assertEqual(entry_state(current, entry), "failed")
        # 完成的也只是“作业处理完毕”，内容维度不计为当前完成。
        stats = translation_stats(store.load_document(project_id), store.load_translation(project_id))
        self.assertEqual(stats["done"], 0)
        self.assertEqual(stats["failed"], 1)

    def test_f10_field_level_edit_round_trips(self):
        table = block("t1", "table", cells=[Cell("c1", 0, 0, "first"), Cell("c2", 0, 1, "second")])
        store, project_id = make_project(self.root, [table])
        service = TranslationService(store, MockProvider())
        service.run(project_id)
        entry = service.edit(project_id, "t1", cells={"c1": "第一格"}, items=None, caption=None, footnote=None)
        self.assertEqual(entry.cells["c1"], "第一格")
        self.assertEqual(entry.cells["c2"], "译：second")
        self.assertTrue(entry.locked)
        # 字段级人工编辑应持久化并锁定，供阅读页与后续流程读取。
        stored = store.load_translation(project_id).entries["t1"]
        self.assertEqual(stored.cells["c1"], "第一格")
        self.assertEqual(stored.cells["c2"], "译：second")
        self.assertTrue(stored.locked)

    def test_api_field_edit_round_trips(self):
        from fastapi.testclient import TestClient

        from literature.api import create_app

        table = block("t1", "table", cells=[Cell("c1", 0, 0, "first"), Cell("c2", 0, 1, "second")])
        store, project_id = make_project(self.root, [table])
        client = TestClient(create_app(store))
        headers = {"x-literature-token": client.app.state.session_token}
        TranslationService(store, MockProvider()).run(project_id)
        edited = client.put(
            f"/projects/{project_id}/translations/t1", headers=headers,
            json={"cells": {"c1": "第一格"}, "locale": "zh-CN"},
        )
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["cells"]["c1"], "第一格")
        self.assertEqual(edited.json()["cells"]["c2"], "译：second")
        # 已移除的内置导出必须明确拒绝，而不能返回半套产物。
        exported = client.post(
            f"/projects/{project_id}/exports", headers=headers,
            json={"mode": "translation", "locale": "zh-CN", "strategy": "keep_source", "formats": ["md+json"]},
        )
        self.assertEqual(exported.status_code, 410, exported.text)
        client.close()

    def test_instant_save_refuses_field_block_and_truncation(self):
        from literature.storage import StorageError

        table = block("t1", "table", cells=[Cell("c1", 0, 0, "first"), Cell("c2", 0, 1, "second")])
        store, project_id = make_project(self.root, [table, block("b1", "paragraph", "A sentence.")])
        service = TranslationService(store, MockProvider())
        # 字段级块不能被单 text 的即时翻译冒充完整译文。
        with self.assertRaises(StorageError):
            service.instant(project_id, "phrase", block_id="t1", save=True)
        # 截断的即时翻译不得保存。
        with self.assertRaises(ProviderError):
            TranslationService(store, LengthProvider()).instant(project_id, "phrase", block_id="b1", save=True)
        self.assertEqual(store.load_translation(project_id).entries, {})
        # 普通段落仍可保存为 text 译文。
        saved = service.instant(project_id, "A sentence.", block_id="b1", save=True)
        self.assertIn("译：", saved["text"])
        self.assertEqual(store.load_translation(project_id).entries["b1"].text, saved["text"])

    def test_structure_only_patch_aligns_card_state(self):
        from fastapi.testclient import TestClient

        from literature.api import create_app

        store, project_id = make_project(self.root, [
            block("h1", "title", "Introduction", order=0),
            block("p1", "paragraph", "Body text.", order=1),
        ])
        client = TestClient(create_app(store))
        headers = {"x-literature-token": client.app.state.session_token}
        TranslationService(store, MockProvider()).run(project_id)
        client.put(f"/projects/{project_id}/translations/h1", headers=headers, json={"text": "导论", "locale": "zh-CN"})
        patched = client.post(
            f"/projects/{project_id}/structure/patches", headers=headers,
            json={"block_id": "h1", "operation": "set_level", "after": 2, "source": "user"},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        # 纯层级变化应立即对齐：卡片不是 partial，内容维度也不计 stale，无需再跑翻译作业。
        translate = client.get("/projects", headers=headers).json()[0]["pipeline"]["translate"]
        self.assertEqual(translate["stale"], 0)
        self.assertEqual(translate["state"], "done")
        layer = client.get(f"/projects/{project_id}/translations/zh-CN", headers=headers).json()
        # 阅读页与卡片一致：h1 已锁定、无 stale_reason。
        self.assertEqual(layer["entries"]["h1"]["state"], "locked")
        self.assertIsNone(layer["entries"]["h1"]["stale_reason"])
        client.close()

    def test_health_capabilities_include_safety_markers(self):
        from fastapi.testclient import TestClient

        from literature.api import create_app

        store, _ = make_project(self.root, [block("b1", "paragraph", "Hello.")])
        client = TestClient(create_app(store))
        capabilities = client.get("/health").json()["capabilities"]
        self.assertIn("translation_safety_v1", capabilities)
        self.assertNotIn("export_readiness_v1", capabilities)
        client.close()


    def _scope_case(self, scope: str) -> tuple[ProjectStore, str]:
        root = self.root / f"case-{scope}"
        root.mkdir()
        store, project_id = make_project(root, [
            block("b1", "paragraph", "first sample"),
            block("b2", "paragraph", "second sample"),
        ])
        setup = TranslationService(store, MockProvider())
        setup.run(project_id, block_ids=["b2"])
        if scope == "failed":
            document = store.load_document(project_id)
            layer = store.load_translation(project_id)
            layer.entries["b1"] = TranslationEntry(
                block_id="b1", status="failed", source_hash=block_source_digest(document.block("b1")), partial=True,
            )
            store.save_translation(layer)
        elif scope == "stale":
            setup.run(project_id, block_ids=["b1"])
            document = store.load_document(project_id)
            document.block("b1").source_text = "changed first sample"
            store.save_document(document)
        return store, project_id

    def test_resume_keeps_scope_targets_across_ranges(self):
        for scope in ("untranslated", "failed", "blocks", "stale"):
            with self.subTest(scope=scope):
                store, project_id = self._scope_case(scope)
                provider = CountingProvider()
                service = TranslationService(store, provider)
                options: dict[str, object] = {"scope": scope, "polish": True, "parallel_workers": 1}
                if scope == "blocks":
                    options["block_ids"] = ["b1"]
                paused = service.run(project_id, **options, should_pause=lambda: provider.calls >= 1)
                self.assertEqual(paused["status"], "paused")
                # checkpoint 保存了本次真正要处理的范围，而不是靠恢复时重算。
                self.assertEqual(store.get_job(project_id, paused["job_id"])["result"]["target_ids"], ["b1"])
                calls_at_pause = provider.calls
                resumed = service.run(project_id, **options, job_id=paused["job_id"])
                self.assertEqual(resumed["status"], "completed")
                # 恢复只做未完成的润色，不重新初译、不重新解析范围。
                self.assertEqual(provider.calls, calls_at_pause + 1)
                layer = store.load_translation(project_id)
                self.assertEqual(layer.entries["b1"].status, "refined")
                # 本次范围之外的已译条目不应被误润色。
                self.assertEqual(layer.entries["b2"].status, "translated")

    def test_resume_force_book_polish_does_not_retranslate(self):
        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "first sample"),
            block("b2", "paragraph", "second sample"),
        ])
        provider = CountingProvider()
        service = TranslationService(store, provider)
        paused = service.run(
            project_id, scope="book", force=True, polish=True, parallel_workers=1,
            should_pause=lambda: provider.calls >= 1,
        )
        self.assertEqual(paused["status"], "paused")
        calls_at_pause = provider.calls
        self.assertEqual(calls_at_pause, 1)
        resumed = service.run(
            project_id, scope="book", force=True, polish=True, parallel_workers=1, job_id=paused["job_id"],
        )
        self.assertEqual(resumed["status"], "completed")
        # force 恢复时必须读 checkpoint：只做润色，不把已完成块重新初译（避免重复付费）。
        self.assertEqual(provider.calls, calls_at_pause + 1)
        layer = store.load_translation(project_id)
        self.assertEqual(layer.entries["b1"].status, "refined")
        self.assertEqual(layer.entries["b2"].status, "refined")

    def test_resume_force_translate_midway_skips_completed(self):
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(3)
        ])
        provider = CountingProvider()
        service = TranslationService(store, provider)
        paused = service.run(
            project_id, scope="book", force=True, parallel_workers=1,
            should_pause=lambda: provider.calls >= 1,
        )
        self.assertEqual(paused["status"], "paused")
        checkpoint = store.get_job(project_id, paused["job_id"])["result"]
        self.assertEqual(checkpoint["pass"], "translate")
        self.assertEqual(len(checkpoint["translate_completed"]), 1)
        calls_at_pause = provider.calls
        resumed = service.run(project_id, scope="book", force=True, parallel_workers=1, job_id=paused["job_id"])
        self.assertEqual(resumed["status"], "completed")
        # 只剩未完成的 2 块，已完成的 1 块不重新强制重译。
        self.assertEqual(provider.calls - calls_at_pause, 2)
        layer = store.load_translation(project_id)
        self.assertTrue(all(layer.entries[f"b{i}"].status == "translated" for i in range(3)))

    def test_resume_polish_midway_only_processes_remaining(self):
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(3)
        ])
        provider = CountingProvider()
        service = TranslationService(store, provider)
        # 3 次初译 + 1 次润色后暂停。
        paused = service.run(
            project_id, scope="book", polish=True, parallel_workers=1,
            should_pause=lambda: provider.calls >= 4,
        )
        self.assertEqual(paused["status"], "paused")
        checkpoint = store.get_job(project_id, paused["job_id"])["result"]
        self.assertEqual(checkpoint["pass"], "polish")
        self.assertEqual(checkpoint["polish_completed"], ["b0"])
        calls_at_pause = provider.calls
        self.assertEqual(calls_at_pause, 4)
        resumed = service.run(project_id, scope="book", polish=True, parallel_workers=1, job_id=paused["job_id"])
        self.assertEqual(resumed["status"], "completed")
        # 只补剩下 2 块的润色，已润色的不重做。
        self.assertEqual(provider.calls - calls_at_pause, 2)
        layer = store.load_translation(project_id)
        self.assertTrue(all(layer.entries[f"b{i}"].status == "refined" for i in range(3)))

    def test_crash_before_checkpoint_does_not_retranslate_saved_blocks(self):
        class CrashProvider(MockProvider):
            """第 6 个分块抛出 BaseException，模拟进程被中断、正常的收尾没机会执行。"""

            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                if self.calls >= 6:
                    raise KeyboardInterrupt("模拟崩溃，绕过阶段收尾")
                return super().complete(system=system, user=user)

        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(6)
        ])
        request = {"scope": "book", "block_ids": [], "locale": "zh-CN"}
        job_id = store.create_job(project_id, "translate", request, request)
        with self.assertRaises(KeyboardInterrupt):
            TranslationService(store, CrashProvider()).run(
                project_id, scope="book", parallel_workers=1, job_id=job_id,
            )
        # 崩溃现场：前 5 块已提交到磁盘，但阶段收尾没有写 checkpoint。
        layer = store.load_translation(project_id)
        self.assertEqual(len(layer.entries), 5)
        crash_checkpoint = store.get_job(project_id, job_id)["result"]
        # 修复后：请求前就已固定目标范围，恢复不靠重算 scope。
        self.assertEqual(crash_checkpoint["target_ids"], [f"b{i}" for i in range(6)])

        resume_provider = CountingProvider()
        resumed = TranslationService(store, resume_provider).run(
            project_id, scope="book", parallel_workers=1, job_id=job_id,
        )
        self.assertEqual(resumed["status"], "completed")
        # 恢复只补未保存的 1 块，不把已持久化的 5 块重新请求。
        self.assertEqual(resume_provider.calls, 1)
        layer = store.load_translation(project_id)
        self.assertTrue(all(layer.entries[f"b{i}"].status == "translated" for i in range(6)))

    def test_force_resume_treats_only_this_task_results_as_done(self):
        """Lead 复现：已有旧译文时启动 force，前 5 块写盘、第 6 块绕过收尾中断。

        旧译文即使 source_hash/status 都有效，也不代表“本次任务已完成”；恢复必须只补第 6 块，
        不能 0 请求假完成。
        """
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(6)
        ])
        # 先完整翻译一次，得到 6 条“历史有效译文”（run_token 属于上一个 job）。
        TranslationService(store, MockProvider()).run(project_id, parallel_workers=1)
        self.assertEqual(len(store.load_translation(project_id).entries), 6)

        class CrashOnSixth(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                if self.calls >= 6:
                    raise KeyboardInterrupt("模拟崩溃，绕过收尾")
                return super().complete(system=system, user=user)

        request = {"scope": "book", "block_ids": [], "locale": "zh-CN"}
        job_id = store.create_job(project_id, "translate", request, {**request, "force": True})
        with self.assertRaises(KeyboardInterrupt):
            TranslationService(store, CrashOnSixth()).run(
                project_id, scope="book", force=True, parallel_workers=1, job_id=job_id,
            )
        # 崩溃现场：5 块新结果已写盘并带上本次 run_token，第 6 块仍是旧译文。
        layer = store.load_translation(project_id)
        self.assertEqual(
            {bid for bid, entry in layer.entries.items() if entry.run_token == job_id},
            {f"b{i}" for i in range(5)},
        )
        # 关键：请求前预写的 checkpoint 不能把 force 前的旧译文算成本次完成（旧实现会是全部 6 块）。
        self.assertEqual(store.get_job(project_id, job_id)["result"]["translate_completed"], [])

        resume_provider = CountingProvider()
        resumed = TranslationService(store, resume_provider).run(
            project_id, scope="book", force=True, parallel_workers=1, job_id=job_id,
        )
        # 只补未重译的第 6 块，而不是 0 次（假完成）也不是 6 次（重复计费）。
        self.assertEqual(resume_provider.calls, 1)
        self.assertEqual(resumed["status"], "completed")
        layer = store.load_translation(project_id)
        self.assertEqual({entry.run_token for entry in layer.entries.values()}, {job_id})

    def test_refine_resume_does_not_skip_already_refined(self):
        """已有旧 refined 的块再次 refine 时，旧 refined 不算本次完成；崩溃恢复只补未做的。"""
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(6)
        ])
        TranslationService(store, MockProvider()).run(project_id, polish=True, parallel_workers=1)
        self.assertTrue(all(entry.status == "refined" for entry in store.load_translation(project_id).entries.values()))

        class CrashOnSixth(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                if self.calls >= 6:
                    raise KeyboardInterrupt("模拟崩溃，绕过收尾")
                return super().complete(system=system, user=user)

        request = {"scope": "book", "block_ids": [], "locale": "zh-CN"}
        job_id = store.create_job(project_id, "refine", request, {**request, "refine": True})
        with self.assertRaises(KeyboardInterrupt):
            TranslationService(store, CrashOnSixth()).run(project_id, refine=True, parallel_workers=1, job_id=job_id)
        # 旧 refined 带的是更早任务的 run_token，不得算本次 refine 完成。
        self.assertEqual(store.get_job(project_id, job_id)["result"]["translate_completed"], [])

        resume_provider = CountingProvider()
        resumed = TranslationService(store, resume_provider).run(
            project_id, refine=True, parallel_workers=1, job_id=job_id,
        )
        self.assertEqual(resume_provider.calls, 1)
        self.assertEqual(resumed["status"], "completed")
        self.assertTrue(all(entry.run_token == job_id for entry in store.load_translation(project_id).entries.values()))

    def test_polish_crash_resume_does_not_fake_complete(self):
        """润色阶段崩溃后恢复不得 0 请求假完成。"""
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(6)
        ])

        class CrashDuringPolish(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                if self.calls >= 8:
                    raise KeyboardInterrupt("润色阶段崩溃")
                return super().complete(system=system, user=user)

        request = {"scope": "book", "block_ids": [], "locale": "zh-CN"}
        job_id = store.create_job(project_id, "translate", request, {**request, "polish": True})
        with self.assertRaises(KeyboardInterrupt):
            TranslationService(store, CrashDuringPolish()).run(
                project_id, polish=True, parallel_workers=1, job_id=job_id,
            )
        resume_provider = CountingProvider()
        resumed = TranslationService(store, resume_provider).run(
            project_id, polish=True, parallel_workers=1, job_id=job_id,
        )
        self.assertGreaterEqual(resume_provider.calls, 1)
        self.assertEqual(resumed["status"], "completed")
        self.assertTrue(all(entry.status == "refined" for entry in store.load_translation(project_id).entries.values()))

    def test_force_paused_counts_reflect_this_round_only(self):
        """有旧译文时 force 暂停，counts 必须按本轮来源算，不能把 6 块旧译文计成本轮完成。"""
        long_text = "word " * 400
        store, project_id = make_project(self.root, [
            block(f"b{index}", "paragraph", f"{long_text}{index}") for index in range(6)
        ])
        TranslationService(store, MockProvider()).run(project_id, parallel_workers=1)
        provider = CountingProvider()
        paused = TranslationService(store, provider).run(
            project_id, scope="book", force=True, parallel_workers=1,
            should_pause=lambda: provider.calls >= 1,
        )
        self.assertEqual(paused["status"], "paused")
        counts = store.get_job(project_id, paused["job_id"])["result"]["counts"]
        self.assertEqual(counts["translate"]["target"], 6)
        self.assertEqual(counts["translate"]["completed"], 1)
        self.assertEqual(counts["translate"]["unhandled"], 5)
        resumed = TranslationService(store, CountingProvider()).run(
            project_id, scope="book", force=True, parallel_workers=1, job_id=paused["job_id"],
        )
        self.assertEqual(resumed["status"], "completed")
        counts = store.get_job(project_id, paused["job_id"])["result"]["counts"]
        self.assertEqual(counts["translate"]["completed"], 6)

    def test_resume_uses_frozen_model_glossary_and_instructions(self):
        """恢复必须按冻结快照执行：模型与术语/附加要求不被后来改动静默替换。"""
        store, project_id = make_project(self.root, [block("b1", "paragraph", "word word word")])
        request = {"scope": "book", "block_ids": [], "locale": "zh-CN"}
        job_id = store.create_job(project_id, "translate", request, {**request, "glossary": {"word": "OLD"}})

        class Recording(MockProvider):
            def __init__(self, model: str) -> None:
                super().__init__()
                self.config = ProviderConfig.preset("custom", model=model, base_url="mock://local")
                self.last: tuple[str, str] | None = None

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.last = (system, user)
                return super().complete(system=system, user=user)

        before = Recording("model-before")
        paused = TranslationService(store, before).run(
            project_id, glossary={"word": "OLD"}, parallel_workers=1, job_id=job_id,
            should_pause=lambda: True,
        )
        self.assertEqual(paused["status"], "paused")
        frozen = store.get_job(project_id, job_id)["result"]["frozen"]
        self.assertEqual(frozen["model"]["model"], "model-before")
        self.assertEqual(frozen["glossary"], {"word": "OLD"})

        # 用户改了模型与非敏感参数、术语表和附加要求后恢复同一 job。
        after = Recording("model-after")
        resumed = TranslationService(store, after).run(
            project_id, glossary={"word": "NEW"}, instructions="ignored-new",
            parallel_workers=1, job_id=job_id,
        )
        self.assertEqual(resumed["status"], "completed")
        # 实际执行用冻结模型（不是 model-after）；密钥仍运行时从服务读，不进快照。
        self.assertEqual(after.config.model, "model-before")
        self.assertIsNotNone(after.last)
        _system, user = after.last  # type: ignore[misc]
        # v3 契约：术语表注入 system prompt 稳定前缀（冻结为 OLD），不在 user prompt。
        self.assertIn("OLD", _system)
        self.assertNotIn("NEW", _system)
        self.assertNotIn("word → OLD", user)
        self.assertNotIn("ignored-new", _system)

    def test_local_quality_warnings_flag_missing_numbers_and_urls(self):
        """本地质量告警：数字/URL/引用缺失只做提示，不改译文也不阻止保存。"""
        source = "In 2020 the rate was 45% (see https://example.com/a [12])."
        store, project_id = make_project(self.root, [block("b1", "paragraph", source)])
        service = TranslationService(store, MockProvider())
        service.run(project_id)
        service.edit(project_id, "b1", "这段译文丢掉了关键数字。")
        document = store.load_document(project_id)
        entry = store.load_translation(project_id).entries["b1"]
        warnings = entry_quality_warnings(document.block("b1"), entry)
        self.assertTrue(any("2020" in warning for warning in warnings), warnings)
        self.assertTrue(any("URL 缺失" in warning for warning in warnings), warnings)
        self.assertTrue(any("[12]" in warning for warning in warnings), warnings)
        # 启发式结果不是语义判定：译文原样保留。
        self.assertEqual(entry.text, "这段译文丢掉了关键数字。")
        payload = translation_payload(document, store.load_translation(project_id))
        self.assertTrue(payload["entries"]["b1"]["quality_warnings"])
        # 子串不等于数字出现：12 在 120 里不得被判为已保留。
        number_block = block("b2", "paragraph", "See 12.")
        fake_entry = TranslationEntry(
            block_id="b2", text="See 120.", status="translated",
            source_hash=block_source_digest(number_block), structure_revision=0,
        )
        self.assertTrue(any("12" in warning for warning in entry_quality_warnings(number_block, fake_entry)))

    def test_api_resume_uses_persisted_checkpoint(self):
        import time
        from fastapi.testclient import TestClient

        from literature.api import TranslationJobInput, create_app

        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "first sample"),
            block("b2", "paragraph", "second sample"),
        ])
        setup = TranslationService(store, MockProvider())
        setup.run(project_id, block_ids=["b2"])
        setup.run(project_id, block_ids=["b1"])
        request = TranslationJobInput(scope="untranslated", locale="zh-CN", polish=True, parallel_workers=1)
        job_id = store.create_job(project_id, "translate", {"scope": "untranslated", "block_ids": [], "locale": "zh-CN"}, request.model_dump())
        # 模拟一次在润色阶段暂停的任务：初译已完成，润色未开始。
        store.update_job(project_id, job_id, status="paused", result={
            "checkpoint_version": 1, "pass": "polish", "target_ids": ["b1"],
            "translate_completed": ["b1"], "polish_target_ids": ["b1"], "polish_completed": [],
        })
        app = create_app(store)
        provider = CountingProvider()
        app.state.job_manager.provider_from = lambda service_id="", provider_input=None, model="": provider
        client = TestClient(app)
        headers = {"x-literature-token": app.state.session_token}
        resumed = client.post(f"/projects/{project_id}/translation/jobs/{job_id}/resume", headers=headers)
        self.assertEqual(resumed.status_code, 200, resumed.text)
        deadline = time.time() + 10
        job = store.get_job(project_id, job_id)
        while job["status"] not in {"completed", "failed", "paused", "budget_paused"} and time.time() < deadline:
            time.sleep(0.05)
            job = store.get_job(project_id, job_id)
        client.close()
        self.assertEqual(job["status"], "completed")
        # 恢复只做润色一次，不重新初译 b1。
        self.assertEqual(provider.calls, 1)
        self.assertEqual(store.load_translation(project_id).entries["b1"].status, "refined")

    def test_api_partial_failed_then_resume_completes(self):
        import time
        from fastapi.testclient import TestClient

        from literature.api import create_app

        class FlakyProvider(MockProvider):
            """首轮始终漏掉 b1 的译文，其余正常。"""

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
                content = "\n".join(
                    f'<seg id="{sid}">译：{text.strip()}</seg>'
                    for sid, text in segments if not sid.startswith("b1#")
                )
                return ProviderResponse(content=content, provider="mock", model="mock", request_id="req", finish_reason="stop")

        store, project_id = make_project(self.root, [
            block("b1", "paragraph", "first sample"),
            block("b2", "paragraph", "second sample"),
        ])
        app = create_app(store)
        app.state.job_manager.provider_from = lambda service_id="", provider_input=None, model="": FlakyProvider()
        client = TestClient(app)
        headers = {"x-literature-token": app.state.session_token}
        started = client.post(f"/projects/{project_id}/translation/jobs", headers=headers, json={
            "scope": "book", "locale": "zh-CN", "polish": True, "parallel_workers": 1,
        })
        self.assertEqual(started.status_code, 200, started.text)
        job_id = started.json()["job_id"]
        deadline = time.time() + 10
        job = store.get_job(project_id, job_id)
        while job["status"] not in {"completed", "partial_failed", "failed", "paused", "budget_paused"} and time.time() < deadline:
            time.sleep(0.05)
            job = store.get_job(project_id, job_id)
        # 真实 API 任务先落在 partial_failed，而不是伪 completed。
        self.assertEqual(job["status"], "partial_failed")
        self.assertTrue(job["error"])
        # 用可控 mock 恢复同一 job：先补初译 b1，再润色。
        app.state.job_manager.provider_from = lambda service_id="", provider_input=None, model="": MockProvider()
        resumed = client.post(f"/projects/{project_id}/translation/jobs/{job_id}/resume", headers=headers)
        self.assertEqual(resumed.status_code, 200, resumed.text)
        job = store.get_job(project_id, job_id)
        while job["status"] not in {"completed", "partial_failed", "failed"} and time.time() < deadline:
            time.sleep(0.05)
            job = store.get_job(project_id, job_id)
        client.close()
        self.assertEqual(job["status"], "completed")
        layer = store.load_translation(project_id)
        self.assertEqual(layer.entries["b1"].status, "refined")
        self.assertEqual(layer.entries["b2"].status, "refined")
        counts = store.get_job(project_id, job_id)["result"]["counts"]
        self.assertEqual(counts["translate"]["completed"], 2)

    def test_aimd_keeps_submitting_after_rate_limit(self):
        """AIMD：首块收到 429 后并发减半，但后续分块仍继续提交，不会整作业空转。"""
        blocks = [block(f"p{i}", "paragraph", f"Paragraph number {i}. " + "word " * 400) for i in range(6)]
        store, project_id = make_project(self.root, blocks)

        class RateLimitedOnce(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                if self.calls == 1:
                    raise ProviderError("模型请求失败：429 Too Many Requests", status_code=429)
                return super().complete(system=system, user=user)

        provider = RateLimitedOnce()
        job = TranslationService(store, provider).run(project_id, force=True, parallel_workers=2)
        # 429 只影响命中的那块（标 failed），后续分块照常翻译，不触发连续失败熔断。
        self.assertEqual(provider.calls > 2, True)
        self.assertIn(job["status"], {"partial_failed", "completed"})
        layer = store.load_translation(project_id)
        translated = sum(1 for entry in layer.entries.values() if entry.status in {"translated", "refined"})
        self.assertGreaterEqual(translated, 3)

    def test_aimd_rate_limit_burst_does_not_trip_circuit_breaker(self):
        """限流突发（连续 429 后服务恢复）：并发减半但绝不能触发全局熔断。

        旧缺陷：整块因 429 完全失败时走 fully_failed 分支，既不减并发又计入
        provider_error_streak，连续 3 次后服务已恢复仍被熔断，剩余整本书误判失败。
        """
        blocks = [block(f"p{i}", "paragraph", f"Paragraph {i}. " + "word " * 400) for i in range(12)]
        store, project_id = make_project(self.root, blocks)

        class RateLimitedBurst(MockProvider):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                if self.calls <= 3:
                    raise ProviderError("模型请求失败：429 Too Many Requests", status_code=429)
                return super().complete(system=system, user=user)

        provider = RateLimitedBurst()
        job = TranslationService(store, provider).run(project_id, force=True, parallel_workers=4)
        layer = store.load_translation(project_id)
        translated = sum(1 for entry in layer.entries.values() if entry.status in {"translated", "refined"})
        failed = sum(1 for entry in layer.entries.values() if entry.status == "failed")
        # 服务第 4 次调用起已恢复：只有真正撞上限流的前 3 块失败，其余全部译完。
        self.assertEqual(job["status"], "partial_failed", job["error"])
        self.assertGreaterEqual(translated, 9)
        self.assertLessEqual(failed, 3)
        self.assertGreater(provider.calls, 4)
        self.assertNotIn("连续失败", job.get("error") or "")


if __name__ == "__main__":
    unittest.main()
