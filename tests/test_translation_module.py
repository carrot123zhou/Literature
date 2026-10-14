from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from literature.api import create_app
from literature.mineru import import_mineru
from literature.models import Block
from literature.providers import MockProvider, ProviderConfig, ProviderError, ProviderResponse
from literature.storage import ProjectStore
from literature.structure import apply_patch
from literature.translation import (
    TranslationService,
    _chars_per_token,
    _chunks,
    _clean_translated_text,
    entry_is_stale,
    entry_stale_reason,
    entry_state,
    estimate_translation,
    resolve_scope,
    translation_payload,
    translation_stats,
)


def span(text: str) -> dict[str, str]:
    return {"type": "text", "content": text}


def build_store(root: Path) -> tuple[ProjectStore, str, Path]:
    store = ProjectStore(root / "library")
    pdf = root / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4\n% fixture")
    project = store.create_project(pdf, title="Translation Fixture")
    project_id = project["project_id"]
    mineru = root / "mineru"
    mineru.mkdir()
    pages = [[
        {"type": "title", "bbox": [10, 10, 300, 35], "content": {"level": 1, "title_content": [span("Introduction")]}},
        {"type": "paragraph", "bbox": [10, 45, 300, 90], "content": {"paragraph_content": [span("The history of science is not a linear accumulation of facts.")]}},
        {"type": "title", "bbox": [10, 95, 300, 140], "content": {"level": 2, "title_content": [span("Methods and Sources")]}},
        {"type": "paragraph", "bbox": [10, 150, 300, 200], "content": {"paragraph_content": [span("A second paragraph used to exercise scopes and staleness.")]}},
    ]]
    (mineru / "book_content_list_v2.json").write_text(json.dumps(pages), encoding="utf-8")
    import_mineru(store, project_id, [mineru])
    return store, project_id, mineru


class FailingProvider(MockProvider):
    def complete(self, *, system: str, user: str) -> ProviderResponse:
        raise ProviderError("模拟远端失败")


class ScriptedProvider(MockProvider):
    """按脚本返回 <seg>：可模拟缺段、截断，并记录每次请求的 prompt。"""

    def __init__(self, *, missing: set[str] | None = None, finish_reason: str = "stop", omit_closing: bool = False) -> None:
        super().__init__()
        self.missing = missing or set()
        self.finish_reason = finish_reason
        self.omit_closing = omit_closing
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        self.calls.append((system, user))
        segments = re.findall(r'<src\s+id="([^"]+)"[^>]*>(.*?)</src>', user, flags=re.S)
        parts: list[str] = []
        for segment_id, text in segments:
            if segment_id in self.missing:
                continue
            if self.omit_closing:
                return ProviderResponse(content=f'<seg id="{segment_id}">{text[:3]}', provider="mock", model="mock", request_id="req", finish_reason=self.finish_reason)
            parts.append(f'<seg id="{segment_id}">译：{text.strip()}</seg>')
        return ProviderResponse(content="\n".join(parts), provider="mock", model="mock", request_id="req", finish_reason=self.finish_reason)


class TranslationModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.project_id, _ = build_store(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def titles(self):
        document = self.store.load_document(self.project_id)
        return [block for block in document.blocks if block.type == "title"]

    def test_stats_and_payload_after_full_translation(self):
        service = TranslationService(self.store, MockProvider())
        service.run(self.project_id)
        document = self.store.load_document(self.project_id)
        layer = self.store.load_translation(self.project_id)
        stats = translation_stats(document, layer)
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["done"], 4)
        self.assertEqual(stats["percent"], 100.0)
        self.assertEqual(stats["stale"], 0)
        payload = translation_payload(document, layer)
        for block in document.blocks:
            self.assertEqual(payload["entries"][block.block_id]["state"], "translated")
            self.assertFalse(payload["entries"][block.block_id]["stale"])

    def test_structure_patch_marks_only_target_block_stale(self):
        service = TranslationService(self.store, MockProvider())
        service.run(self.project_id)
        target = self.titles()[0]
        apply_patch(self.store, self.project_id, {"block_id": target.block_id, "operation": "set_level", "after": 2}, source="user")
        document = self.store.load_document(self.project_id)
        layer = self.store.load_translation(self.project_id)
        changed = document.block(target.block_id)
        self.assertEqual(changed.structure_revision, 1)
        self.assertTrue(entry_is_stale(changed, layer.entries[target.block_id]))
        self.assertEqual(entry_state(changed, layer.entries[target.block_id]), "stale")
        # 其它块不受影响。
        for block in document.blocks:
            if block.block_id != target.block_id:
                self.assertFalse(entry_is_stale(block, layer.entries.get(block.block_id)))
        stats = translation_stats(document, layer)
        self.assertEqual(stats["stale"], 1)
        self.assertEqual(stats["stale_block_ids"], [target.block_id])
        self.assertEqual(stats["done"], 3)

    def test_locked_entry_stays_locked_but_flagged_when_stale(self):
        service = TranslationService(self.store, MockProvider())
        service.run(self.project_id)
        target = self.titles()[0]
        service.edit(self.project_id, target.block_id, "人工锁定译文")
        apply_patch(self.store, self.project_id, {"block_id": target.block_id, "operation": "set_level", "after": 3}, source="user")
        document = self.store.load_document(self.project_id)
        layer = self.store.load_translation(self.project_id)
        entry = layer.entries[target.block_id]
        self.assertTrue(entry.locked)
        self.assertTrue(entry_is_stale(document.block(target.block_id), entry))
        self.assertEqual(entry_state(document.block(target.block_id), entry), "locked")
        self.assertIn(target.block_id, translation_stats(document, layer)["stale_block_ids"])

    def test_stale_scope_retranslates_and_clears(self):
        service = TranslationService(self.store, MockProvider())
        service.run(self.project_id)
        document = self.store.load_document(self.project_id)
        # 正文转列表会搬运 source_text -> items，属于 source stale，需要重译。
        target = next(block for block in document.blocks if block.type == "paragraph")
        apply_patch(self.store, self.project_id, {"block_id": target.block_id, "operation": "set_type", "after": "list"}, source="user")
        document = self.store.load_document(self.project_id)
        layer = self.store.load_translation(self.project_id)
        self.assertEqual(resolve_scope(document, layer, "stale", None), {target.block_id})
        service.run(self.project_id, scope="stale")
        refreshed = self.store.load_translation(self.project_id)
        block = self.store.load_document(self.project_id).block(target.block_id)
        self.assertFalse(entry_is_stale(block, refreshed.entries[target.block_id]))

    def test_scope_resolution_for_untranslated_failed_and_book(self):
        document = self.store.load_document(self.project_id)
        empty = self.store.load_translation(self.project_id)
        self.assertIsNone(resolve_scope(document, empty, "book", None))
        self.assertIsNone(resolve_scope(document, empty, "auto", None))
        self.assertEqual(resolve_scope(document, empty, "untranslated", None), {block.block_id for block in document.blocks})
        self.assertEqual(resolve_scope(document, empty, "blocks", [document.blocks[0].block_id]), {document.blocks[0].block_id})
        with self.assertRaises(Exception):
            resolve_scope(document, empty, "nonsense", None)

        service = TranslationService(self.store, MockProvider())
        service.run(self.project_id)
        failed_service = TranslationService(self.store, FailingProvider())
        with self.assertRaises(ProviderError):
            failed_service.run(self.project_id, block_ids=[document.blocks[0].block_id], force=True)
        layer = self.store.load_translation(self.project_id)
        self.assertEqual(resolve_scope(document, layer, "failed", None), {document.blocks[0].block_id})

    def test_run_with_failed_scope_retries_after_provider_recovers(self):
        document = self.store.load_document(self.project_id)
        target = document.blocks[0].block_id
        with self.assertRaises(ProviderError):
            TranslationService(self.store, FailingProvider()).run(self.project_id, scope="book")
        failed_layer = self.store.load_translation(self.project_id)
        self.assertEqual(resolve_scope(document, failed_layer, "failed", None), {block.block_id for block in document.blocks})
        TranslationService(self.store, MockProvider()).run(self.project_id, scope="failed")
        recovered = self.store.load_translation(self.project_id)
        self.assertTrue(all(entry.status == "translated" for entry in recovered.entries.values()))
        self.assertNotIn(target, translation_stats(document, recovered)["stale_block_ids"])

    def test_estimate_reports_candidates_and_cost(self):
        document = self.store.load_document(self.project_id)
        layer = self.store.load_translation(self.project_id)
        config = ProviderConfig(provider="openai", model="gpt", base_url="https://example.com/v1", input_price_per_million=1.0, output_price_per_million=2.0)
        estimate = estimate_translation(document, layer, scope="book", block_ids=None, refine=False, config=config)
        self.assertEqual(estimate["block_count"], 4)
        self.assertGreater(estimate["source_chars"], 0)
        self.assertGreater(estimate["estimated_input_tokens"], 0)
        self.assertIsNotNone(estimate["estimated_cost"])
        # 翻译完成后，非 force 的预估应为 0。
        TranslationService(self.store, MockProvider()).run(self.project_id)
        layer = self.store.load_translation(self.project_id)
        again = estimate_translation(document, layer, scope="book", block_ids=None, refine=False, config=config)
        self.assertEqual(again["block_count"], 0)
        self.assertEqual(again["source_chars"], 0)
        # force 会重译已有译文，预估必须非零（否则与实际行为不一致）。
        forced = estimate_translation(
            document, layer, scope="book", block_ids=None, refine=False, force=True, config=config,
        )
        self.assertEqual(forced["block_count"], 4)
        self.assertGreater(forced["chunk_count"], 0)
        self.assertTrue(forced["has_pricing"])
        self.assertIsNotNone(forced["estimated_cost_low"])
        self.assertLessEqual(forced["estimated_input_tokens_low"], forced["estimated_input_tokens_high"])
        self.assertGreater(forced["prompt_chars"], forced["source_chars"])  # prompt 含 system/标签开销
        plain = ProviderConfig(provider="x", model="y", base_url="https://example.com/v1")
        no_price = estimate_translation(document, layer, scope="book", block_ids=None, refine=False, config=plain)
        self.assertIsNone(no_price["estimated_cost"])

    def test_stale_reason_distinguishes_structure_from_source(self):
        service = TranslationService(self.store, MockProvider())
        service.run(self.project_id)
        title = self.titles()[0]
        # 仅层级变化：源文摘要不变，属于结构对齐，无需重译。
        apply_patch(self.store, self.project_id, {"block_id": title.block_id, "operation": "set_level", "after": 2}, source="user")
        document = self.store.load_document(self.project_id)
        layer = self.store.load_translation(self.project_id)
        self.assertEqual(entry_stale_reason(document.block(title.block_id), layer.entries[title.block_id]), "structure")
        stats = translation_stats(document, layer)
        self.assertEqual(stats["stale"], 1)
        self.assertEqual(stats["stale_structure"], 1)
        self.assertEqual(stats["stale_source"], 0)

        # 正文转列表：source_text 搬入 items，源文摘要变化，属于待复核。
        paragraph = next(block for block in document.blocks if block.type == "paragraph")
        apply_patch(self.store, self.project_id, {"block_id": paragraph.block_id, "operation": "set_type", "after": "list"}, source="user")
        document = self.store.load_document(self.project_id)
        layer = self.store.load_translation(self.project_id)
        self.assertEqual(entry_stale_reason(document.block(paragraph.block_id), layer.entries[paragraph.block_id]), "source")
        stats = translation_stats(document, layer)
        self.assertEqual(stats["stale"], 2)
        self.assertEqual(stats["stale_structure"], 1)
        self.assertEqual(stats["stale_source"], 1)
        payload = translation_payload(document, layer)
        self.assertEqual(payload["entries"][paragraph.block_id]["stale_reason"], "source")

    def test_locked_stale_is_counted_and_excluded_from_stale_scope(self):
        service = TranslationService(self.store, MockProvider())
        service.run(self.project_id)
        document = self.store.load_document(self.project_id)
        target = next(block for block in document.blocks if block.type == "paragraph")
        service.edit(self.project_id, target.block_id, "人工锁定译文")
        # 正文转列表改变源字段布局，属于 source stale，需要人工复核。
        apply_patch(self.store, self.project_id, {"block_id": target.block_id, "operation": "set_type", "after": "list"}, source="user")
        document = self.store.load_document(self.project_id)
        layer = self.store.load_translation(self.project_id)
        stats = translation_stats(document, layer)
        # 锁定条目仍计入待复核，让界面不会漏报。
        self.assertEqual(stats["locked"], 1)
        self.assertEqual(stats["stale"], 1)
        self.assertEqual(stats["stale_locked"], 1)
        self.assertIn(target.block_id, stats["stale_block_ids"])
        # 自动重译范围排除已锁定块；需要时由用户先解锁。
        self.assertEqual(resolve_scope(document, layer, "stale", None), set())
        service.unlock(self.project_id, target.block_id)
        layer = self.store.load_translation(self.project_id)
        self.assertEqual(resolve_scope(document, layer, "stale", None), {target.block_id})

    def test_translation_job_rejects_concurrent_start(self):
        from literature.api import JobManager, TranslationJobInput
        from literature.storage import StorageError
        manager = JobManager(self.store, None)
        self.store.create_job(self.project_id, "translate", {"scope": "book"}, {})
        with self.assertRaises(StorageError):
            manager.start_translation(self.project_id, TranslationJobInput(scope="book"))

    def test_translation_start_is_atomic_under_concurrency(self):
        import threading
        import time

        from literature.api import JobManager, TranslationJobInput

        manager = JobManager(self.store, None)
        # 不真正执行作业线程，否则 run() 可能立即把作业改成 failed，绕过“活动任务”检查。
        manager.executor.submit = lambda fn: None
        original_list_jobs = self.store.list_jobs

        def slow_list_jobs(project_id: str):
            time.sleep(0.05)  # 放大“先查后建”窗口，让并发竞态稳定复现
            return original_list_jobs(project_id)

        self.store.list_jobs = slow_list_jobs
        barrier = threading.Barrier(4)
        created: list[str] = []
        errors: list[Exception] = []

        def start() -> None:
            barrier.wait()
            try:
                created.append(manager.start_translation(self.project_id, TranslationJobInput(scope="book")))
            except Exception as error:  # noqa: BLE001 - 其余线程预期因互斥被拒
                errors.append(error)

        threads = [threading.Thread(target=start) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # 只允许一次领取：一个作业成功，其余并发请求被拒，项目里只有一个翻译作业。
        self.assertEqual(len(created), 1)
        self.assertEqual(len(errors), 3)
        self.assertEqual(len(self.store.list_jobs(self.project_id)), 1)

    def test_structure_job_rejects_duplicate_start(self):
        import types

        from literature.api import JobManager, StructureJobInput
        from literature.providers import MockProvider
        from literature.storage import StorageError
        manager = JobManager(self.store, None)
        # 结构作业只关心“要不要重复启动”；模型解析在这里不影响守卫，用最小桩替代。
        manager.service_id_for = lambda role, explicit="": "svc"
        manager.provider_from = lambda service_id, provider_input=None: MockProvider()
        manager.models = types.SimpleNamespace(get=lambda service_id: {"model": "mock", "name": "Mock"})
        manager.executor.submit = lambda fn: None
        self.store.create_job(self.project_id, "structure_ai", {"toc_entries": 0}, {})
        with self.assertRaises(StorageError):
            manager.start_structure(self.project_id, StructureJobInput(extract_toc=True))

    def test_translation_start_gates_new_and_resume_concurrently(self):
        import threading

        from literature.api import JobManager, TranslationJobInput
        from literature.storage import StorageError

        manager = JobManager(self.store, None)
        manager.executor.submit = lambda fn: None
        failed = self.store.create_job(self.project_id, "translate", {"scope": "book"}, {})
        self.store.update_job(self.project_id, failed, status="failed")
        barrier = threading.Barrier(2)
        created: list[str] = []
        errors: list[Exception] = []

        def start_new() -> None:
            barrier.wait()
            try:
                created.append(manager.start_translation(self.project_id, TranslationJobInput(scope="book")))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        def resume_failed() -> None:
            barrier.wait()
            try:
                created.append(manager.start_translation(
                    self.project_id, TranslationJobInput(scope="book"), existing_job_id=failed,
                ))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=start_new), threading.Thread(target=resume_failed)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # “新建”与“恢复旧失败任务”同时发生也只能有一次领取。
        self.assertEqual(len(created), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StorageError)
        active = [job for job in self.store.list_jobs(self.project_id) if job["status"] in {"queued", "running"}]
        self.assertEqual(len(active), 1)

    def test_double_resume_only_claims_once(self):
        import threading

        from literature.api import JobManager, TranslationJobInput
        from literature.storage import StorageError

        manager = JobManager(self.store, None)
        manager.executor.submit = lambda fn: None
        failed = self.store.create_job(self.project_id, "translate", {"scope": "book"}, {})
        self.store.update_job(self.project_id, failed, status="failed")
        barrier = threading.Barrier(2)
        created: list[str] = []
        errors: list[Exception] = []

        def resume() -> None:
            barrier.wait()
            try:
                created.append(manager.start_translation(
                    self.project_id, TranslationJobInput(scope="book"), existing_job_id=failed,
                ))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=resume), threading.Thread(target=resume)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(created, [failed])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StorageError)

    def test_structure_start_claims_atomically(self):
        import threading
        import types

        from literature.api import JobManager, StructureJobInput
        from literature.providers import MockProvider
        from literature.storage import StorageError

        manager = JobManager(self.store, None)
        manager.service_id_for = lambda role, explicit="": "svc"
        manager.provider_from = lambda service_id, provider_input=None: MockProvider()
        manager.models = types.SimpleNamespace(get=lambda service_id: {"model": "mock", "name": "Mock"})
        manager.executor.submit = lambda fn: None
        barrier = threading.Barrier(2)
        created: list[str] = []
        errors: list[Exception] = []

        def start() -> None:
            barrier.wait()
            try:
                created.append(manager.start_structure(self.project_id, StructureJobInput(extract_toc=True)))
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=start), threading.Thread(target=start)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # 同项目结构 AI 作业同样只能有一个 queued/running。
        self.assertEqual(len(created), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StorageError)
        structure_jobs = [job for job in self.store.list_jobs(self.project_id) if job["kind"] == "structure_ai"]
        self.assertEqual(len(structure_jobs), 1)

    def test_submit_failure_leaves_recoverable_job(self):
        from literature.api import JobManager, TranslationJobInput
        from literature.storage import StorageError

        manager = JobManager(self.store, None)

        def broken(fn):
            raise RuntimeError("线程池不可用")

        manager.executor.submit = broken
        with self.assertRaises(StorageError):
            manager.start_translation(self.project_id, TranslationJobInput(scope="book"))
        jobs = self.store.list_jobs(self.project_id)
        self.assertEqual(len(jobs), 1)
        # 不能卡在 queued（无 worker 且会被 resume 拒绝）；应变成可恢复的 failed。
        self.assertEqual(jobs[0]["status"], "failed")
        manager.executor.submit = lambda fn: None
        resumed = manager.start_translation(
            self.project_id, TranslationJobInput(scope="book"), existing_job_id=jobs[0]["job_id"],
        )
        self.assertEqual(resumed, jobs[0]["job_id"])
        self.assertEqual(self.store.get_job(self.project_id, resumed)["status"], "queued")

    def api_client(self):
        from fastapi.testclient import TestClient
        client = TestClient(create_app(self.store))
        return client, {"x-literature-token": client.app.state.session_token}

    def test_glossary_versions_and_rollback_via_api(self):
        client, headers = self.api_client()
        first = client.put(f"/projects/{self.project_id}/translations/zh-CN/glossary", json={"entries": {"Paradigm": "范式"}}, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["version"], 1)
        second = client.put(f"/projects/{self.project_id}/translations/zh-CN/glossary", json={"entries": {"Paradigm": "范例"}}, headers=headers)
        self.assertEqual(second.json()["version"], 2)
        versions = client.get(f"/projects/{self.project_id}/translations/zh-CN/glossary/versions", headers=headers).json()
        self.assertEqual([item["version"] for item in versions], [1, 2])
        rolled = client.post(f"/projects/{self.project_id}/translations/zh-CN/glossary/rollback", json={"version": 1}, headers=headers)
        self.assertEqual(rolled.status_code, 200)
        self.assertEqual(rolled.json()["entries"], {"Paradigm": "范式"})
        self.assertEqual(rolled.json()["version"], 3)
        layer = client.get(f"/projects/{self.project_id}/translations/zh-CN", headers=headers).json()
        self.assertIn("stats", layer)
        self.assertEqual(layer["glossary_snapshot"], {"Paradigm": "范式"})
        client.close()

    def test_api_layer_payload_and_estimate_endpoint(self):
        client, headers = self.api_client()
        layer = client.get(f"/projects/{self.project_id}/translations/zh-CN", headers=headers).json()
        self.assertEqual(layer["stats"]["total"], 4)
        self.assertEqual(layer["stats"]["untranslated"], 4)
        response = client.post(f"/projects/{self.project_id}/translation/estimate", json={"scope": "book", "locale": "zh-CN"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["block_count"], 4)
        client.close()

    # ---- 新引擎：协议解析、部分失败、截断、并发、润色、后处理 ----------------

    def test_partial_chunk_keeps_successful_blocks_and_fails_only_missing(self):
        document = self.store.load_document(self.project_id)
        target = next(block for block in document.blocks if block.type == "paragraph")
        provider = ScriptedProvider(missing={f"{target.block_id}#text"})
        job = TranslationService(self.store, provider).run(self.project_id)
        # 有失败段落时不再冒充“完成”：任务状态与错误栏必须如实反映失败，便于重试。
        self.assertEqual(job["status"], "partial_failed")
        self.assertIn("1 段失败", job["error"])
        layer = self.store.load_translation(self.project_id)
        self.assertEqual(layer.entries[target.block_id].status, "failed")
        self.assertIn("缺少", layer.entries[target.block_id].error)
        for block in document.blocks:
            if block.block_id == target.block_id:
                continue
            self.assertEqual(layer.entries[block.block_id].status, "translated")
        stats = translation_stats(document, layer)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["done"], 3)

    def test_truncated_unclosed_output_is_never_saved_as_translated(self):
        document = self.store.load_document(self.project_id)
        target = next(block for block in document.blocks if block.type == "paragraph")
        provider = ScriptedProvider(omit_closing=True, finish_reason="length")
        # 只请求这一段：全部失败时作业判失败并抛错，与既有语义一致。
        with self.assertRaises(ProviderError):
            TranslationService(self.store, provider).run(self.project_id, block_ids=[target.block_id])
        entry = self.store.load_translation(self.project_id).entries[target.block_id]
        self.assertEqual(entry.status, "failed")
        self.assertEqual(entry.text, "")

    def test_glossary_is_injected_as_stable_system_prefix(self):
        """v3 契约：术语表整份按稳定排序注入 system prompt（prefix caching 友好），
        user prompt 不再携带术语表，也不再按块过滤。"""
        document = self.store.load_document(self.project_id)
        provider = ScriptedProvider()
        TranslationService(self.store, provider).run(
            self.project_id, glossary={"Zebra": "斑马", "Introduction": "导论"}, force=True,
        )
        for system, user in provider.calls:
            # system 前缀逐请求一致：术语表、排序稳定，命中自动前缀缓存。
            self.assertIn("术语表", system)
            self.assertLess(system.index("Introduction → 导论"), system.index("Zebra → 斑马"))
            # user prompt 只包含跨块变化的部分。
            self.assertNotIn("术语表", user)
            self.assertNotIn("Nonexistent", system)
        self.assertIn("Introduction → 导论", provider.calls[0][0])

    def test_parallel_workers_translate_every_block(self):
        document = self.store.load_document(self.project_id)
        job = TranslationService(self.store, MockProvider()).run(self.project_id, parallel_workers=4)
        self.assertEqual(job["status"], "completed")
        layer = self.store.load_translation(self.project_id)
        self.assertEqual(translation_stats(document, layer)["done"], 4)
        for block in document.blocks:
            self.assertEqual(layer.entries[block.block_id].status, "translated")

    def test_polish_pass_marks_entries_refined(self):
        document = self.store.load_document(self.project_id)
        provider = MockProvider()
        job = TranslationService(self.store, provider).run(self.project_id, polish=True, parallel_workers=2)
        self.assertEqual(job["status"], "completed")
        layer = self.store.load_translation(self.project_id)
        for block in document.blocks:
            self.assertEqual(layer.entries[block.block_id].status, "refined")

    def test_cjk_post_processing_cleans_spacing(self):
        self.assertEqual(_clean_translated_text("这 是 译 文， 请 查 看 。"), "这是译文，请查看。")
        self.assertEqual(_clean_translated_text('```\n<seg id="a">译文</seg>\n```'), "译文")

    def test_chunk_budget_is_token_based_per_language(self):
        """同一 token 预算下：英文块可容纳更多字符（减少请求数），中文块更小（降截断）。"""
        self.assertGreater(_chars_per_token("en"), _chars_per_token("zh-CN"))
        english = [Block(block_id=f"e{i}", type="paragraph", order=i, page_index=0, source_text="word " * 200) for i in range(4)]
        chinese = [Block(block_id=f"c{i}", type="paragraph", order=i, page_index=0, source_text="字" * 1200) for i in range(4)]
        # 英文 200 词 ≈ 1200 字符 ≈ 300 token：4 块应打包进更少的分块。
        self.assertLessEqual(len(_chunks(english, language="en")), 2)
        # 中文 1200 字 ≈ 750 token：预算 900 下每块只能装 1 段。
        self.assertEqual(len(_chunks(chinese, language="zh-CN")), 4)

    def test_thinking_disabled_respects_explicit_config(self):
        from literature.providers import OpenAICompatibleProvider

        def make(style: str, effort: str) -> OpenAICompatibleProvider:
            config = ProviderConfig.preset(
                "custom", model="qwen3.8-flash-next", base_url="https://relay.example/v1",
                reasoning_style=style, reasoning_effort=effort,
            )
            return OpenAICompatibleProvider(config, api_key="k")

        # qwen 协议：未显式开思考 → 翻译任务强制 off；显式 on 尊重用户配置。
        self.assertEqual(make("qwen_thinking", "").with_thinking_disabled().config.reasoning_effort, "off")
        self.assertEqual(make("qwen_thinking", "on").with_thinking_disabled().config.reasoning_effort, "on")
        self.assertEqual(make("glm_thinking", "").with_thinking_disabled().config.reasoning_effort, "off")
        # openai_effort 协议：降到 minimal，显式 high 例外；未声明协议的服务不发送未知字段。
        self.assertEqual(make("openai_effort", "").with_thinking_disabled().config.reasoning_effort, "minimal")
        self.assertEqual(make("openai_effort", "high").with_thinking_disabled().config.reasoning_effort, "high")
        untouched = make("", "")
        self.assertIs(untouched.with_thinking_disabled(), untouched)
        # 原对象不被修改。
        original = make("qwen_thinking", "")
        original.with_thinking_disabled()
        self.assertEqual(original.config.reasoning_effort, "")


if __name__ == "__main__":
    unittest.main()
