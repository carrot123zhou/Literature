from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from literature.models import Block, Document, Page
from literature.providers import ProviderResponse, OpenAICompatibleProvider, ProviderConfig, ProviderError, ProviderTimeoutError
from literature.structure import (
    _batch_payload,
    analyze_structure,
    analyze_structure_with_provider,
    apply_patch,
    find_toc_page_candidates,
    plan_structure_analysis,
    reconcile_toc,
    revert_patch,
    run_structure_job,
    structure_model_payload,
)
from literature.storage import ProjectStore, StorageError


class HierarchicalFixtureProvider:
    def complete(self, *, system, user):
        payload = json.loads(user)
        rows = [{"block_id": "missing", "role": "h1", "parent_id": None, "confidence": .99},
                {"block_id": "sub", "role": "h3", "parent_id": "missing", "confidence": .99}]
        if payload.get("stage") == "outline-plan-v2":
            value = {"profile": {"toc_scope": "chapters only"}, "outline": rows}
        else:
            value = {"decisions": rows}
        return ProviderResponse(content=json.dumps(value), provider="mock", model="mock")


class StructureCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        import fitz
        pdf = fitz.open()
        for i in range(2):
            page = pdf.new_page(width=600, height=800)
            page.insert_text((60, 70), "Contents" if i == 0 else "Introduction", fontsize=20)
        self.source = self.root / "source.pdf"
        pdf.save(self.source)
        pdf.close()
        self.store = ProjectStore(self.root / "library")
        self.project_id = self.store.create_project(self.source)["project_id"]
        self.doc = Document(project_id=self.project_id, title="Fixture", source_language="en", page_count=2, blocks=[
            Block(block_id="toc", type="title", page_index=0, order=0, source_text="Contents", level=1, bbox=[100, 50, 800, 100]),
            Block(block_id="missing", type="paragraph", page_index=1, order=1, source_text="Introduction", bbox=[100, 50, 800, 100]),
            Block(block_id="body", type="paragraph", page_index=1, order=2, source_text="Private body text that must never be included in the model input.", bbox=[100, 150, 800, 300]),
            Block(block_id="sub", type="title", page_index=1, order=3, source_text="A short unlisted subheading", level=2, bbox=[100, 350, 800, 390]),
        ], pages=[Page(page_index=i, width=600, height=800) for i in range(2)])
        self.store.save_document(self.doc)
        self.toc = [{"title": "Introduction", "level": 1}]

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_heading_is_manual_and_unlisted_subheading_is_preserved(self):
        values = analyze_structure(self.doc, self.toc)
        self.assertEqual([item["block_id"] for item in values], ["missing"])
        self.assertEqual(values[0]["operation"], "set_title")
        self.assertFalse(values[0]["automatic"])
        self.assertEqual(analyze_structure(self.doc, []), [])

    def test_visual_plan_is_bounded_and_does_not_include_body(self):
        plan = plan_structure_analysis(self.doc, self.toc, extract_toc=True, proofread=True,
            toc_page_indexes=[0], use_vision=True, source_path=self.source)
        self.assertNotIn("Private body text", json.dumps(plan))
        self.assertEqual([item["page_index"] for item in plan["toc_pages"]], [0])
        self.assertTrue(any(item["kind"] == "toc" for item in plan["visual_evidence"]))
        self.assertTrue(any(item.get("block_id") == "missing" for item in plan["visual_evidence"]))
        self.assertFalse(any(item.get("block_id") == "body" for item in plan["visual_evidence"]))
        self.assertLessEqual(len(plan["visual_evidence"]), 7)
        with self.assertRaises(StorageError):
            plan_structure_analysis(self.doc, [], extract_toc=True, proofread=True, toc_page_indexes=[2])

    def test_auto_detected_toc_pages_over_limit_are_truncated_not_rejected(self):
        """回归（2026-10-20）：自动识别超出 6 页时不再硬报错，截到前 6 页继续。

        真实案例：原书只有 1 页目录，但目录标记页向后扩展把版式相似的相邻页
        一并计入，返回 7+ 页；旧逻辑直接抛“目录页超过 6 页，请缩小范围后重试”，
        而自动模式下用户没有范围可缩小，作业无法启动。
        """
        from literature import structure_signals as signals

        def toc_page(page_index: int, start: int) -> list[Block]:
            lines = [f"Chapter {start + i} Sample Entry {start * 3 + i}" for i in range(6)]
            return [Block(block_id=f"p{page_index}-{i}", type="paragraph", page_index=page_index,
                          order=page_index * 10 + i, source_text=line, bbox=[100, 100 + i * 40, 900, 130 + i * 40])
                    for i, line in enumerate(lines)]

        pages = [Block(block_id="mark", type="title", page_index=0, order=0,
                       source_text="Contents", level=1, bbox=[100, 50, 800, 100]),
                 Block(block_id="e0", type="paragraph", page_index=0, order=1,
                       source_text="Preface vii", bbox=[100, 120, 900, 150])]
        for i in range(1, 8):
            pages.extend(toc_page(i, i * 6 + 1))
        doc = Document(project_id=self.project_id, title="ManyToc", source_language="en", page_count=8,
                       blocks=pages, pages=[Page(page_index=i, width=600, height=800) for i in range(8)])
        self.assertGreater(len(signals.find_toc_pages(doc)), 6)
        plan = plan_structure_analysis(doc, self.toc, extract_toc=True, proofread=False)
        self.assertLessEqual(len(plan["toc_pages"]), 6)
        self.assertEqual(plan["toc_pages"][0]["page_index"], 0)

    def test_user_specified_toc_pages_over_limit_still_rejected(self):
        with self.assertRaises(StorageError):
            plan_structure_analysis(self.doc, [], extract_toc=True, proofread=False,
                                    toc_page_indexes=list(range(7)))

    def test_model_promotion_cannot_be_automatically_applied(self):
        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content=json.dumps({"suggestions": [{"block_id": "missing", "operation": "set_title", "after": 1, "confidence": 1.0}]}), provider="mock", model="mock")
        values, _ = analyze_structure_with_provider(self.doc, self.toc, Provider())
        self.assertEqual(values[0]["operation"], "set_title")
        self.assertFalse(values[0]["automatic"])

    def test_numbered_heading_outside_toc_is_a_model_candidate(self):
        from literature.structure import structure_model_payload
        self.doc.blocks[1].source_text = "2.1 An unlisted section"
        payload = structure_model_payload(self.doc, self.toc)
        self.assertIn("missing", [item["block_id"] for item in payload["title_candidates"]])
        self.assertNotIn("body", [item["block_id"] for item in payload["title_candidates"]])

    def test_conflicting_toc_levels_do_not_authorize_local_correction(self):
        self.doc.blocks[1].type = "title"
        self.doc.blocks[1].level = 3
        values = analyze_structure(self.doc, [{"title": "Introduction", "level": 1}, {"title": "Introduction", "level": 2}])
        self.assertFalse(any(item["block_id"] == "missing" for item in values))

    def test_structure_change_during_model_call_rejects_stale_results(self):
        class Provider:
            def complete(inner, **_):
                changed = self.store.load_document(self.project_id)
                changed.blocks[1].level = 3
                self.store.save_document(changed)
                return ProviderResponse(content='{"suggestions":[]}', provider="mock", model="mock")
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        with self.assertRaises(StorageError):
            run_structure_job(self.store, self.project_id, job_id,
                {"toc": self.toc, "proofread": True, "extract_toc": False, "auto_apply": True}, Provider())
        self.assertEqual(self.store.list_patches(self.project_id), [])

    def test_images_are_sent_as_chat_content_parts(self):
        config = ProviderConfig.preset("custom", base_url="https://example.invalid/v1", model="test")
        provider = OpenAICompatibleProvider(config, api_key="fixture")
        provider._stream_disabled = True  # 非流式回退路径：本用例只验证请求体组装
        from unittest.mock import Mock
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"choices": [{"message": {"content": "{}"}}]}
        response.headers = {}
        with patch("httpx.Client.post", return_value=response) as post:
            provider.complete_vision(system="calibrate", user="candidate metadata", images=[{"kind": "heading", "page_index": 1, "image_url": "data:image/png;base64,fixture"}])
        content = post.call_args.kwargs["json"]["messages"][1]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[-1]["type"], "image_url")
        self.assertEqual(content[-1]["image_url"]["detail"], "high")

    def test_read_timeout_is_separate_from_connect_and_does_not_retry(self):
        import httpx
        provider = OpenAICompatibleProvider(ProviderConfig.preset("custom", base_url="https://example.invalid/v1", model="test"), api_key="fixture")
        provider._stream_disabled = True  # 超时语义用非流式回退路径验证
        structure_provider = provider.with_read_timeout(600)
        with patch("httpx.Client.post", side_effect=httpx.ReadTimeout("timed out")) as post:
            with self.assertRaises(ProviderTimeoutError) as raised:
                structure_provider.complete(system="test", user="test")
        post.assert_called_once()
        self.assertEqual(raised.exception.kind, "read")
        self.assertEqual(raised.exception.seconds, 600)
        self.assertEqual(provider.timeout, 180)
        self.assertEqual(post.call_args.kwargs["timeout"].connect, 20)
        self.assertEqual(post.call_args.kwargs["timeout"].write, 60)
        self.assertEqual(post.call_args.kwargs["timeout"].read, 600)

    def test_connect_timeout_reports_connection_not_model_generation(self):
        import httpx
        provider = OpenAICompatibleProvider(ProviderConfig.preset("custom", base_url="https://example.invalid/v1", model="test"), api_key="fixture", timeout=600)
        provider._stream_disabled = True
        with patch("httpx.Client.post", side_effect=httpx.ConnectTimeout("timed out")) as post:
            with self.assertRaises(ProviderTimeoutError) as raised:
                provider.complete(system="test", user="test")
        post.assert_called_once()
        self.assertEqual(raised.exception.kind, "connect")
        self.assertEqual(raised.exception.seconds, 20)

    def test_legacy_project_database_is_migrated_before_job_updates(self):
        """既有项目在升级后首次写入任务前必须补齐新增列。

        否则 `update_job(message=...)` 直接报 `no such column: message`，
        结构 AI 作业会立刻中断并永远停在 queued，界面无限重连。
        """
        import sqlite3
        database = self.store.project_path(self.project_id) / "literature.sqlite"
        with sqlite3.connect(database) as connection:
            for column in ("message", "result_json", "activity_json"):
                connection.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
        reopened = ProjectStore(self.root / "library")
        job_id = reopened.create_job(self.project_id, "structure_ai", {}, {})
        reopened.update_job(self.project_id, job_id, message="载入规范文档", activity=[{"text": "ok"}])
        job = reopened.get_job(self.project_id, job_id)
        self.assertEqual(job["message"], "载入规范文档")
        self.assertEqual(job["activity"][0]["text"], "ok")

    def test_batch_patch_is_atomic_when_one_item_is_invalid(self):
        """批量中任一项非法时不得部分提交文档或修订记录。"""
        from literature.structure import apply_patch_batch
        original = self.store.load_document(self.project_id).block("sub").level
        with self.assertRaises(StorageError):
            apply_patch_batch(self.store, self.project_id, [
                {"block_id": "sub", "operation": "set_level", "after": 3},
                {"block_id": "missing", "operation": "set_level", "after": 9},
            ])
        self.assertEqual(self.store.load_document(self.project_id).block("sub").level, original)
        self.assertEqual(self.store.list_patches(self.project_id), [])

    def test_sibling_suggestions_from_same_analysis_all_apply(self):
        from literature.structure import apply_patch_batch
        patches = apply_patch_batch(self.store, self.project_id, [
            {"block_id": "sub", "operation": "set_level", "after": 3, "before": 2},
            {"block_id": "missing", "operation": "set_title", "after": 1, "before": "paragraph"},
        ])
        self.assertEqual(len(patches), 2)
        document = self.store.load_document(self.project_id)
        self.assertEqual(document.block("sub").level, 3)
        self.assertEqual(document.block("missing").type, "title")

    def test_stale_suggestion_cannot_overwrite_newer_block_change(self):
        proposal = {"block_id": "sub", "operation": "set_level", "after": 3, "before": 2, "confidence": 0.9}
        apply_patch(self.store, self.project_id, {"block_id": "sub", "operation": "set_level", "after": 4})
        with self.assertRaises(StorageError):
            apply_patch(self.store, self.project_id, proposal)
        self.assertEqual(self.store.load_document(self.project_id).block("sub").level, 4)

    def test_structure_baseline_persists_with_project(self):
        saved = self.store.save_structure_baseline(self.project_id, [
            {"title": "Introduction", "level": 1},
            {"title": "  ", "level": 2},
            {"title": "Chapter", "level": 9},
        ], source="confirmed")
        self.assertEqual(saved["toc"], [{"title": "Introduction", "level": 1}, {"title": "Chapter", "level": 6}])
        loaded = ProjectStore(self.root / "library").load_structure_baseline(self.project_id)
        self.assertEqual(loaded["toc"], saved["toc"])
        self.assertEqual(loaded["source"], "confirmed")
        self.assertTrue(loaded["updated_at"])

    def test_new_pending_supersedes_old_but_decided_stays_decided(self):
        self.store.save_structure_suggestions(self.project_id, "job1", [
            {"block_id": "sub", "operation": "set_level", "after": 3, "before": 2, "confidence": .9, "reason": "a"},
            {"block_id": "missing", "operation": "set_title", "after": 1, "before": "paragraph", "confidence": .9, "reason": "b"},
        ])
        first = self.store.list_structure_suggestions(self.project_id, "pending")
        self.assertEqual(len(first), 2)
        sub = next(item for item in first if item["block_id"] == "sub")
        self.store.decide_structure_suggestion(self.project_id, sub["suggestion_id"], "dismissed")
        self.assertEqual([item["block_id"] for item in self.store.list_structure_suggestions(self.project_id, "pending")], ["missing"])
        # 同一作业重新持久化：已忽略的不复活。
        self.store.save_structure_suggestions(self.project_id, "job1", [
            {"block_id": "sub", "operation": "set_level", "after": 3, "before": 2, "confidence": .9, "reason": "a"},
        ])
        self.assertNotIn("sub", [item["block_id"] for item in self.store.list_structure_suggestions(self.project_id, "pending")])
        # 新作业取代旧 pending。
        self.store.save_structure_suggestions(self.project_id, "job2", [
            {"block_id": "sub", "operation": "set_level", "after": 3, "before": 2, "confidence": .9, "reason": "a"},
        ])
        pending = self.store.list_structure_suggestions(self.project_id, "pending")
        self.assertEqual([item["block_id"] for item in pending], ["sub"])

    def test_job_persists_model_suggestions_as_pending_even_when_auto_apply_enabled(self):
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        run_structure_job(self.store, self.project_id, job_id,
            {"toc": self.toc, "proofread": True, "extract_toc": False, "auto_apply": True}, HierarchicalFixtureProvider())
        pending = self.store.list_structure_suggestions(self.project_id, "pending")
        self.assertIn("missing", [item["block_id"] for item in pending])
        self.assertIn("sub", [item["block_id"] for item in pending])
        applied = self.store.list_structure_suggestions(self.project_id, "applied")
        self.assertFalse(applied)
        self.assertTrue(all(item["document_updated_at"] for item in pending))
        self.assertEqual(self.store.load_document(self.project_id).block("sub").level, 2)

    def test_later_revision_must_be_reverted_first(self):
        first = apply_patch(self.store, self.project_id, {"block_id": "missing", "operation": "set_title", "after": 1})
        second = apply_patch(self.store, self.project_id, {"block_id": "missing", "operation": "set_level", "after": 3})
        with self.assertRaises(StorageError):
            revert_patch(self.store, self.project_id, first.patch_id)
        revert_patch(self.store, self.project_id, second.patch_id)
        revert_patch(self.store, self.project_id, first.patch_id)
        self.assertEqual(self.store.load_document(self.project_id).block("missing").type, "paragraph")

    def test_high_confidence_suggestion_stays_pending_when_auto_apply_is_off(self):
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"toc": self.toc, "proofread": True, "extract_toc": False, "auto_apply": False}, HierarchicalFixtureProvider())
        self.assertFalse(result["applied"])
        self.assertTrue(result["suggestions"])
        self.assertTrue(all(not item["automatic"] for item in result["suggestions"]))

    def test_nontext_blocks_cannot_be_promoted(self):
        self.doc.blocks.append(Block(block_id="image", type="image", order=4, page_index=1))
        self.store.save_document(self.doc)
        with self.assertRaises(StorageError):
            apply_patch(self.store, self.project_id, {"block_id": "image", "operation": "set_title", "after": 1})
        with self.assertRaises(StorageError):
            apply_patch(self.store, self.project_id, {"block_id": "image", "operation": "set_type", "after": "paragraph"})

    def test_text_structure_conversion_moves_content_and_is_reversible(self):
        from literature.structure import reconcile_toc
        # 正文 → 列表：文字移入 items，撤销时完整还原 source_text。
        patch = apply_patch(self.store, self.project_id, {
            "block_id": "body", "operation": "set_type", "after": "list", "reason": "人工确认列表",
        })
        converted = self.store.load_document(self.project_id).block("body")
        self.assertEqual(converted.type, "list")
        self.assertTrue(converted.items and "Private body text" in converted.items[0])
        self.assertEqual(converted.source_text, "")
        revert_patch(self.store, self.project_id, patch.patch_id)
        restored = self.store.load_document(self.project_id).block("body")
        self.assertEqual(restored.type, "paragraph")
        self.assertTrue(restored.source_text.startswith("Private body text"))
        self.assertEqual(restored.items, [])

        # 正文 → 脚注保留 source_text；标题也可降为脚注。
        footnote = apply_patch(self.store, self.project_id, {
            "block_id": "body", "operation": "set_type", "after": "footnote",
        })
        self.assertEqual(self.store.load_document(self.project_id).block("body").type, "footnote")
        revert_patch(self.store, self.project_id, footnote.patch_id)
        heading = apply_patch(self.store, self.project_id, {
            "block_id": "sub", "operation": "set_type", "after": "footnote",
        })
        self.assertEqual(self.store.load_document(self.project_id).block("sub").type, "footnote")
        revert_patch(self.store, self.project_id, heading.patch_id)
        self.assertEqual(self.store.load_document(self.project_id).block("sub").type, "title")

    def test_automatic_type_change_only_allows_demoting_a_title(self):
        with self.assertRaises(StorageError):
            apply_patch(self.store, self.project_id, {
                "block_id": "body", "operation": "set_type", "after": "list",
                "confidence": 0.99, "automatic": True,
            })
        with self.assertRaises(StorageError):
            apply_patch(self.store, self.project_id, {
                "block_id": "sub", "operation": "set_type", "after": "footnote",
                "confidence": 0.99, "automatic": True,
            })

    def test_reconcile_reports_missing_and_unlisted_headings(self):
        from literature.structure import reconcile_toc
        result = reconcile_toc(self.doc, self.toc + [{"title": "Chapter Not Present", "level": 1}])
        self.assertEqual(result["summary"]["toc_entries"], 2)
        self.assertEqual(result["summary"]["missing"], 1)
        self.assertEqual([item["title"] for item in result["missing"]], ["Chapter Not Present"])
        self.assertIn("sub", [item["block_id"] for item in result["unlisted_titles"]])
        self.assertEqual(result["summary"]["promotions"], 1)

    def test_job_result_includes_reconciliation_summary(self):
        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content='{"suggestions":[]}', provider="mock", model="mock")
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"toc": self.toc, "proofread": True, "extract_toc": False, "auto_apply": False}, Provider())
        self.assertEqual(result["reconciliation"]["toc_entries"], 1)
        self.assertIn("missing", result["reconciliation"])

    def test_unknown_model_block_ids_are_skipped(self):
        skipped: list[dict] = []

        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content=json.dumps({"suggestions": [
                    {"block_id": "does-not-exist", "operation": "set_level", "after": 2, "confidence": .99},
                    {"block_id": "sub", "operation": "set_level", "after": 3, "confidence": .99},
                ]}), provider="mock", model="mock")
        values, _ = analyze_structure_with_provider(self.doc, self.toc, Provider(), skipped=skipped)
        self.assertEqual([item["block_id"] for item in values], ["sub"])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["block_id"], "does-not-exist")
        self.assertIn("未知", skipped[0]["reason"])

    def test_skipped_model_suggestions_are_reported_in_audit_and_activity(self):
        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content=json.dumps({"suggestions": [
                    {"block_id": "ghost", "operation": "set_level", "after": 2, "confidence": .99},
                ]}), provider="mock", model="mock")
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"toc": self.toc, "proofread": True, "extract_toc": False, "auto_apply": False}, Provider())
        self.assertGreaterEqual(result["audit"]["skipped_model_suggestions"], 1)
        self.assertEqual(result["skipped_suggestions"][0]["block_id"], "ghost")
        requests = result["audit"]["requests"]
        proofread_requests = [item for item in requests if item.get("scope") == "structure_proofread"]
        self.assertTrue(proofread_requests)
        self.assertEqual(proofread_requests[-1]["rejected_count"], 1)
        self.assertIn("未知 block_id", proofread_requests[-1]["rejected_reasons"])
        job = self.store.get_job(self.project_id, job_id)
        self.assertTrue(any(item["level"] == "warning" for item in job["activity"]))

    def test_job_result_records_document_updated_at(self):
        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content='{"suggestions":[]}', provider="mock", model="mock")
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"toc": self.toc, "proofread": True, "extract_toc": False, "auto_apply": False}, Provider())
        self.assertEqual(result["document_updated_at"], self.store.load_document(self.project_id).updated_at)

    def test_recorded_document_updated_at_detects_later_structural_change(self):
        """前端恢复旧作业时依赖这个比较：后续手动改结构后修订时间必须不同，才会拒绝回填。"""
        import time

        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content='{"suggestions":[]}', provider="mock", model="mock")
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"toc": self.toc, "proofread": True, "extract_toc": False, "auto_apply": False}, Provider())
        recorded = result["document_updated_at"]
        time.sleep(0.002)
        apply_patch(self.store, self.project_id, {"block_id": "sub", "operation": "set_level", "after": 3})
        current = self.store.load_document(self.project_id).updated_at
        self.assertNotEqual(recorded, current)

    def test_invalid_confidence_and_fractional_level_are_rejected(self):
        for bad in (float("nan"), float("inf"), -0.1, 1.5, True, "high", None):
            with self.assertRaises(StorageError):
                apply_patch(self.store, self.project_id, {
                    "block_id": "sub", "operation": "set_level", "after": 2, "confidence": bad,
                })
        for bad in (0, 7, 2.5, None):
            with self.assertRaises(StorageError):
                apply_patch(self.store, self.project_id, {
                    "block_id": "sub", "operation": "set_level", "after": bad,
                })
        # 数字字符串归一化为合法整数层级是可以接受的（PatchInput.after 允许字符串）。
        normalized = apply_patch(self.store, self.project_id, {"block_id": "sub", "operation": "set_level", "after": "3"})
        self.assertEqual(normalized.after, 3)

    def test_provider_skips_invalid_confidence_and_level(self):
        class Provider:
            def complete(inner, **_):
                # NaN 在 json.loads 中能解析；应被有限性校验拒掉。
                return ProviderResponse(content=(
                    '{"suggestions":['
                    '{"block_id":"sub","operation":"set_level","after":3,"confidence":NaN},'
                    '{"block_id":"sub","operation":"set_level","after":2.5,"confidence":0.9},'
                    '{"block_id":"sub","operation":"set_level","after":3,"confidence":true},'
                    '{"block_id":"sub","operation":"set_level","after":8,"confidence":0.9}'
                    ']}'), provider="mock", model="mock")
        values, _ = analyze_structure_with_provider(self.doc, self.toc, Provider())
        self.assertEqual(values, [])

    def test_fuzzy_local_match_stays_manual(self):
        # 单一模糊候选可提建议；一个目录条目不能同时分配给多个块。
        self.doc.blocks[1].source_text = "Other text"
        self.doc.blocks[3] = Block(block_id="sub", type="title", order=3, page_index=1,
            source_text="Introductions", level=1, bbox=[100, 350, 800, 390])
        self.store.save_document(self.doc)
        values = [item for item in analyze_structure(self.doc, [{"title": "Introduction", "level": 2}]) if item["block_id"] == "sub"]
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["operation"], "set_level")
        self.assertFalse(values[0]["automatic"])

    def test_exact_local_match_is_the_only_automatic_level_change(self):
        self.doc.blocks[3] = Block(block_id="sub", type="title", order=3, page_index=1,
            source_text="Introduction", level=1, bbox=[100, 350, 800, 390])
        self.store.save_document(self.doc)
        values = [item for item in analyze_structure(self.doc, [{"title": "Introduction", "level": 2}]) if item["block_id"] == "sub"]
        self.assertEqual(len(values), 1)
        self.assertTrue(values[0]["automatic"])

    def test_structure_job_default_does_not_auto_apply(self):
        from literature.api import StructureJobInput
        self.assertFalse(StructureJobInput().auto_apply)

    def test_failed_json_still_records_usage_and_audit(self):
        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content="not json at all", provider="mock", model="mock",
                                        input_tokens=11, output_tokens=2)
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        with self.assertRaises(ProviderError):
            run_structure_job(self.store, self.project_id, job_id,
                {"toc": self.toc, "proofread": True, "extract_toc": False, "auto_apply": False}, Provider())
        job = self.store.get_job(self.project_id, job_id)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(job["result"]["audit"]["requests"])
        usages = [item for item in self.store.usage_records(self.project_id) if item["scope"] == "structure_proofread"]
        self.assertTrue(usages)
        self.assertEqual(usages[0]["input_tokens"], 11)

    def test_batches_isolate_candidates_and_images(self):
        candidates = [{"block_id": f"b{index}"} for index in range(200)]
        payload = {
            "authoritative_toc": [],
            "visual_evidence": (
                [{"kind": "toc", "page_index": 0, "image_url": "toc-image"}]
                + [{"kind": "heading", "block_id": f"b{index}", "page_index": 0, "image_url": "x"} for index in range(200)]
            ),
        }
        first = _batch_payload(payload, candidates, 0)
        self.assertEqual(len(first["title_candidates"]), 80)
        self.assertFalse(any(item["kind"] == "toc" for item in first["visual_evidence"]))
        self.assertTrue(all(item["kind"] == "heading" for item in first["visual_evidence"]))
        batch_ids = {item["block_id"] for item in first["title_candidates"]}
        self.assertTrue(all(item["block_id"] in batch_ids for item in first["visual_evidence"]))
        last = _batch_payload(payload, candidates, 160)
        self.assertEqual(len(last["title_candidates"]), 40)

    def test_extract_only_never_sends_heading_images(self):
        plan = plan_structure_analysis(self.doc, self.toc, extract_toc=True, proofread=False,
            toc_page_indexes=[0], use_vision=True, source_path=self.source)
        self.assertEqual(plan["title_candidates"], [])
        self.assertTrue(plan["visual_evidence"])
        self.assertTrue(all(item["kind"] == "toc" for item in plan["visual_evidence"]))

    def test_explicit_toc_pages_do_not_exclude_body_pages_by_candidate(self):
        # 页 1 带有目录特征（标记 + 多行页码），但用户明确只选页 0 做目录时，
        # 页 1 上的编号小标题不应因为“自动候选”而被排除。
        self.doc.blocks.extend([
            Block(block_id="tocmark", type="title", order=5, page_index=1, source_text="Contents", level=1, bbox=[100, 20, 800, 60]),
            Block(block_id="ref1", type="paragraph", order=6, page_index=1, source_text="Chapter one 12", bbox=[100, 100, 800, 130]),
            Block(block_id="ref2", type="paragraph", order=7, page_index=1, source_text="Chapter two 24", bbox=[100, 140, 800, 170]),
            Block(block_id="ref3", type="paragraph", order=8, page_index=1, source_text="Chapter three 36", bbox=[100, 180, 800, 210]),
            Block(block_id="num", type="paragraph", order=9, page_index=1, source_text="2.1 Section", bbox=[100, 500, 800, 530]),
        ])
        self.store.save_document(self.doc)
        self.assertIn(1, {item["page_index"] for item in find_toc_page_candidates(self.doc)})
        # 明确只选页 0：不把自动候选的页 1 当成目录排除。
        explicit = structure_model_payload(self.doc, self.toc, excluded_pages={0})
        self.assertIn("num", {item["block_id"] for item in explicit["title_candidates"]})
        # 默认（无明确页）仍回退到本地候选，页 1 被排除。
        auto = structure_model_payload(self.doc, self.toc)
        self.assertNotIn("num", {item["block_id"] for item in auto["title_candidates"]})
        # 明确选页 1 做目录时，该页不再进入候选。
        selected = structure_model_payload(self.doc, self.toc, excluded_pages={1})
        self.assertNotIn("num", {item["block_id"] for item in selected["title_candidates"]})

    def test_proofread_only_excludes_detected_toc_page_from_candidates(self):
        captured = {}

        class Provider:
            def complete(inner, *, system, user):
                captured["user"] = json.loads(user)
                return ProviderResponse(content='{"suggestions":[]}', provider="mock", model="mock")

        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        run_structure_job(self.store, self.project_id, job_id,
            {"toc": self.toc, "proofread": True, "extract_toc": False, "auto_apply": False}, Provider())
        ids = {item["block_id"] for item in captured["user"]["title_candidates"]}
        # 页 0 的“Contents”是本地识别出的目录页，不应作为标题候选发送（即使未显式选页）。
        self.assertNotIn("toc", ids)
        self.assertIn("missing", ids)

    def test_reconciled_promotion_stays_available_for_visual_confirmation(self):
        payload = structure_model_payload(self.doc, self.toc)
        self.assertTrue(any(item["block_id"] == "missing" and item["operation"] == "set_title"
                            for item in payload["local_suggestions"]))
        self.assertIn("missing", {item["block_id"] for item in payload["title_candidates"]})

    def test_local_and_model_conflict_keeps_more_reasonable_basis(self):
        from literature.structure import _merge_suggestion
        exact = {"block_id": "a", "operation": "set_level", "confidence": 0.99, "automatic": True, "after": 1}
        fuzzy = {"block_id": "a", "operation": "set_level", "confidence": 0.95, "automatic": False, "after": 2}
        self.assertEqual(_merge_suggestion(exact, fuzzy), exact)  # 本地自动定案优先
        weak = {"block_id": "a", "operation": "set_title", "confidence": 0.80, "automatic": False, "after": 1}
        strong = {"block_id": "a", "operation": "set_title", "confidence": 0.93, "automatic": False, "after": 1}
        self.assertEqual(_merge_suggestion(weak, strong), strong)  # 同非自动时取更高置信度

    def test_repeated_running_header_title_is_automatically_demoted(self):
        """跨页重复、位于页眉区的 title 不是真标题，应本地自动降为正文。"""
        blocks = [
            Block(block_id=f"h{index}", type="title", page_index=index, order=index,
                  source_text=f"{index + 1} - Chapter One", level=2, bbox=[100, 60, 800, 90])
            for index in range(4)
        ]
        blocks.append(Block(block_id="real", type="title", page_index=0, order=10,
                            source_text="One The Differential Tradition", level=1, bbox=[100, 300, 800, 340]))
        document = Document(project_id=self.project_id, title="F", source_language="en", page_count=4, blocks=blocks)
        values = {item["block_id"]: item for item in analyze_structure(document, [])}
        self.assertIn("h0", values)
        self.assertEqual(values["h0"]["operation"], "set_type")
        self.assertTrue(values["h0"]["automatic"])
        self.assertNotIn("real", values)

    def test_collapsed_toc_with_list_numbers_is_split_correctly(self):
        """折叠成单行、且带 1./2. 列表编号的目录，不应把编号误当页码或漏拆条目。"""
        from literature.structure import local_printed_toc
        entries_text = " ".join(f"{index}. Chapter number {index} {index * 10}" for index in range(1, 20))
        document = Document(project_id=self.project_id, title="F", source_language="en", page_count=2, blocks=[
            Block(block_id="mark", type="title", page_index=0, order=0, source_text="Contents", level=1, bbox=[100, 60, 800, 90]),
            Block(block_id="toc", type="paragraph", page_index=0, order=1, source_text=entries_text, bbox=[100, 120, 900, 700]),
        ])
        result = local_printed_toc(document)
        self.assertEqual(len(result["entries"]), 19)
        self.assertEqual(result["entries"][0]["title"], "1. Chapter number 1")
        self.assertEqual(result["entries"][0]["printed_page"], "10")
        self.assertEqual(result["entries"][18]["printed_page"], "190")

    def test_ordered_alignment_handles_duplicate_toc_titles(self):
        """同名目录条目应按出现顺序各自对齐，而不是都抢第一个块。"""
        document = Document(project_id=self.project_id, title="F", source_language="en", page_count=6, blocks=[
            Block(block_id="a", type="title", page_index=0, order=0, source_text="Introduction", level=1, bbox=[100, 300, 800, 340]),
            Block(block_id="b", type="title", page_index=5, order=1, source_text="Introduction", level=1, bbox=[100, 300, 800, 340]),
            Block(block_id="mid", type="paragraph", page_index=2, order=2, source_text="body " * 40, bbox=[100, 400, 800, 600]),
        ])
        result = reconcile_toc(document, [{"title": "Introduction", "level": 1}, {"title": "Introduction", "level": 2}])
        self.assertEqual([item["block_id"] for item in result["matched"]], ["a", "b"])

    def test_local_printed_toc_extracts_levels_and_pages(self):
        from literature.structure import local_printed_toc
        document = Document(project_id=self.project_id, title="F", source_language="en", page_count=3, blocks=[
            Block(block_id="mark", type="title", page_index=0, order=0, source_text="Contents", level=1, bbox=[100, 60, 800, 90]),
            Block(block_id="toc", type="paragraph", page_index=0, order=1, bbox=[100, 120, 900, 700],
                  source_text="Chapter One. The Differential Tradition 11\nSix patriarchs in search of a tradition 12\nChapter Two. Sudden/Gradual: A Loose Paradigm 32"),
        ])
        result = local_printed_toc(document)
        self.assertEqual(result["pages"], [0])
        self.assertEqual([entry["level"] for entry in result["entries"]], [1, 2, 1])
        self.assertEqual(result["entries"][0]["printed_page"], "11")
        self.assertEqual(result["entries"][1]["title"], "Six patriarchs in search of a tradition")

    def test_inline_footnote_marker_does_not_block_toc_match(self):
        """MinerU 把脚注标记混入标题时，匹配前必须先去标记。"""
        self.doc.blocks[1].source_text = "THE EMERGENCE OF THE TRICKSTER equation_inline^{1}"
        self.doc.blocks[1].type = "paragraph"
        self.store.save_document(self.doc)
        values = [item for item in analyze_structure(self.doc, [{"title": "The Emergence of the Trickster", "level": 2}]) if item["block_id"] == "missing"]
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["operation"], "set_title")

    def local_toc_fixture(self):
        toc_text = "\n".join(f"Chapter {i}. Title number {i} {i * 10}" for i in range(12))
        document = Document(project_id=self.project_id, title="F", source_language="en", page_count=3, blocks=[
            Block(block_id="mark", type="title", page_index=0, order=0, source_text="Contents", level=1, bbox=[100, 60, 800, 90]),
            Block(block_id="toc", type="paragraph", page_index=0, order=1, source_text=toc_text,
                  bbox=[100, 120, 900, 700]),
            Block(block_id="ch", type="title", page_index=1, order=2, source_text="Chapter 3. Title number 3", level=2, bbox=[100, 300, 800, 340]),
        ], pages=[Page(page_index=i, width=600, height=800) for i in range(3)])
        self.store.save_document(document)

    def test_local_toc_with_explicit_marker_is_trusted_without_a_model_call(self):
        """有显式目录标记页且本地能解析时直接采信，不再为“核对目录”发一次模型请求。

        模型看到的是与本地完全相同的折行文本（MinerU 不保留目录缩进），转录与层级推断
        并不比本地准；结论仍进 proposed_toc 供人确认，不直接写文档。
        """
        self.local_toc_fixture()
        scopes: list[str] = []

        class Provider:
            def complete(inner, *, system, user):
                payload = json.loads(user)
                assert "toc_pages" not in payload, "本地目录可信时不应再发目录核对请求"
                candidates = payload.get("candidates", payload.get("title_candidates", []))
                rows = [{"block_id": row["block_id"], "role": "h1", "parent_id": None,
                         "confidence": 0.9} for row in candidates]
                scopes.append("outline" if payload.get("stage") else "review")
                content = {"outline": rows, "profile": {}} if payload.get("stage") else {"decisions": rows}
                return ProviderResponse(content=json.dumps(content), provider="mock", model="mock")

        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"toc": [], "proofread": True, "extract_toc": True, "auto_apply": False}, Provider())
        self.assertGreaterEqual(len(result["proposed_toc"]), 10)
        self.assertEqual(result["baseline_source"], "local")
        self.assertEqual(result["toc_extraction"], "local")
        self.assertNotIn("structure_extract", {item["scope"] for item in self.store.usage_records(self.project_id)})

    def test_model_extraction_can_be_forced_or_used_when_no_marker_exists(self):
        self.local_toc_fixture()

        class Provider:
            def complete(inner, *, system, user):
                payload = json.loads(user)
                if "toc_pages" in payload:
                    self.assertGreaterEqual(len(payload["local_toc_draft"]), 10)
                    return ProviderResponse(content=json.dumps({"toc": payload["local_toc_draft"]}), provider="mock", model="mock")
                candidates = payload.get("candidates", payload.get("title_candidates", []))
                rows = [{"block_id": row["block_id"], "role": "h1", "parent_id": None,
                         "confidence": 0.9} for row in candidates]
                content = {"outline": rows, "profile": {}} if payload.get("stage") else {"decisions": rows}
                return ProviderResponse(content=json.dumps(content), provider="mock", model="mock")

        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"toc": [], "proofread": True, "extract_toc": True, "auto_apply": False,
             "toc_extraction": "model"}, Provider())
        self.assertGreaterEqual(len(result["proposed_toc"]), 10)
        self.assertIn("structure_extract", {item["scope"] for item in self.store.usage_records(self.project_id)})
        self.assertEqual(result["baseline_source"], "model")

        # 无显式标记（本地靠行扫描/人工指定目录页）时仍走模型核对。
        document = self.store.load_document(self.project_id)
        document.blocks[0].source_text = "Preface"
        self.store.save_document(document)
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"toc": [], "proofread": True, "extract_toc": True, "auto_apply": False,
             "toc_page_indexes": [0], "toc_extraction": "auto"}, Provider())
        self.assertEqual(result["toc_extraction"], "model")
        self.assertEqual(result["local_toc_marker_pages"], [])


    def test_alignment_drives_duplicate_level_patches(self):
        self.doc.blocks[1].type = "title"
        self.doc.blocks[1].level = 3
        self.doc.blocks[3].source_text = "Introduction"
        values = {x["block_id"]: x for x in analyze_structure(self.doc, [
            {"title": "Introduction", "level": 1}, {"title": "Introduction", "level": 2}])}
        self.assertEqual(values["missing"]["after"], 1)
        self.assertNotIn("sub", values)  # 第二个位置已是 H2。

    def test_one_toc_entry_cannot_correct_two_duplicate_blocks(self):
        self.doc.blocks[1].type = "title"
        self.doc.blocks[1].level = 3
        self.doc.blocks[3].source_text = "Introduction"
        values = analyze_structure(self.doc, self.toc)
        self.assertEqual(len([x for x in values if x["operation"] == "set_level"]), 1)

    def test_furniture_never_reenters_alignment_by_exact_text(self):
        self.doc.blocks[1].source_text = "Introduction"
        self.doc.blocks[1].format["mineru_type"] = "page_header"
        result = reconcile_toc(self.doc, self.toc)
        self.assertEqual(result["matched"], [])
        self.assertFalse(any(x["operation"] == "set_title" for x in result["suggestions"]))

    def test_raw_newlines_numbered_levels_and_illustration_lists(self):
        from literature.structure import local_printed_toc
        text = "I. Introduction .... 1\n1.1. Scope .... 2\n1.1.1. Details .... 3"
        self.doc.blocks[0].source_text = "Contents"
        self.doc.blocks.insert(1, Block(block_id="toclines", type="paragraph", order=1, page_index=0,
            source_text=text.replace("\n", " "), raw={"content": {"paragraph_content": [{"type": "text", "content": text}]}}))
        self.doc.blocks.insert(2, Block(block_id="maps", type="title", order=2, page_index=0, source_text="MAPS", level=2))
        self.doc.blocks.insert(3, Block(block_id="map", type="paragraph", order=3, page_index=0, source_text="Map of a region .... 8"))
        result = local_printed_toc(self.doc, pages=[0])
        self.assertEqual([x["level"] for x in result["entries"]], [1, 2, 3])
        self.assertEqual(len(result["entries"]), 3)

    def test_local_toc_bullets_do_not_pollute_title(self):
        from literature.structure_signals import parse_toc_lines
        result = parse_toc_lines(["Acknowledgments • xi", "Chapter 1 Earth • 1", "Unreferenced footer"])
        self.assertEqual([x.title for x in result], ["Acknowledgments", "Chapter 1 Earth"])

    def test_inferred_toc_levels_cannot_auto_apply(self):
        self.doc.blocks[3].source_text = "Introduction"
        values = analyze_structure(self.doc, [{"title": "Introduction", "level": 1, "level_source": "heuristic"}])
        self.assertTrue(values)
        self.assertTrue(all(not x["automatic"] for x in values))

    def test_explicit_pages_control_local_extraction_and_audit(self):
        # 自动目录与手选目录不同，作业必须只用手选范围。
        self.doc.blocks = [self.doc.blocks[0]]
        self.doc.blocks.append(Block(block_id="other", type="paragraph", order=5, page_index=1,
            source_text="Chapter 1 Selected .... 1\nChapter 2 Selected .... 2"))
        self.doc.blocks.append(Block(block_id="auto", type="paragraph", order=6, page_index=0,
            source_text="Chapter 1 Wrong .... 1\nChapter 2 Wrong .... 2"))
        self.store.save_document(self.doc)
        class Provider:
            def complete(inner, *, system, user):
                payload = json.loads(user)
                self.assertEqual([x["page_index"] for x in payload["toc_pages"]], [1])
                return ProviderResponse(content=json.dumps({"toc": payload["local_toc_draft"]}), provider="mock", model="mock")
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"extract_toc": True, "proofread": False, "toc_page_indexes": [1]}, Provider())
        self.assertTrue(all("Selected" in x["title"] for x in result["proposed_toc"]))
        self.assertEqual([x["page_index"] for x in result["audit"]["toc_pages"]], [1])

    def test_role_coverage_includes_unchanged_and_reports_omissions(self):
        from literature.structure import STRUCTURE_PROMPT_VERSION
        payload = structure_model_payload(self.doc, self.toc)
        payload["decision_protocol"] = STRUCTURE_PROMPT_VERSION
        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content=json.dumps({"decisions": [
                    {"block_id": "sub", "role": "h2", "confidence": .9, "reason": "保持小标题"}]}), provider="mock", model="mock")
        skipped, decisions = [], {}
        values, _ = analyze_structure_with_provider(self.doc, self.toc, Provider(), payload=payload, skipped=skipped, decisions=decisions)
        self.assertFalse(values)
        self.assertTrue(decisions["sub"]["unchanged"])
        self.assertTrue(any(x["block_id"] == "missing" for x in skipped))

    def test_model_promoted_heading_can_be_adopted_and_reverted(self):
        # 原实现为 set_title 存 before=0，人工采用时会误判类型已过时。
        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content='{"decisions":[{"block_id":"missing","role":"h1","confidence":0.99,"reason":"目录匹配"}]}', provider="mock", model="mock")
        values, _ = analyze_structure_with_provider(self.doc, self.toc, Provider())
        self.assertFalse(values[0]["automatic"])
        revision = apply_patch(self.store, self.project_id, values[0])
        self.assertEqual(self.store.load_document(self.project_id).block("missing").type, "title")
        revert_patch(self.store, self.project_id, revision.patch_id)
        self.assertEqual(self.store.load_document(self.project_id).block("missing").source_text, "Introduction")

    def test_list_promotion_preserves_all_text_and_reverts(self):
        self.doc.blocks[1].type = "list"
        self.doc.blocks[1].source_text = ""
        self.doc.blocks[1].items = ["Introduction"]
        self.store.save_document(self.doc)
        revision = apply_patch(self.store, self.project_id, {"block_id": "missing", "operation": "set_title", "after": 1})
        self.assertEqual(self.store.load_document(self.project_id).block("missing").source_text, "Introduction")
        revert_patch(self.store, self.project_id, revision.patch_id)
        self.assertEqual(self.store.load_document(self.project_id).block("missing").items, ["Introduction"])

    def test_network_failure_is_not_retried_as_truncated_json(self):
        from literature.structure import _proofread_batch
        calls = []
        class Provider:
            def complete(inner, **_):
                calls.append(1)
                raise ProviderError("服务鉴权失败")
        payload = structure_model_payload(self.doc, self.toc)
        chunk = payload["title_candidates"] * 20
        with self.assertRaises(ProviderError):
            _proofread_batch(self.doc, self.toc, Provider(), payload, chunk, [])
        self.assertEqual(len(calls), 1)

    def test_no_toc_still_reviews_heading_roles(self):
        self.doc.blocks = [Block(block_id="caption", type="title", order=0, page_index=1,
            source_text="Figure 1. Diagram", level=2, bbox=[100, 400, 800, 430])]
        self.store.save_document(self.doc)
        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content='{"decisions":[{"block_id":"caption","role":"caption","confidence":0.95,"reason":"图题"}]}', provider="mock", model="mock")
        job_id = self.store.create_job(self.project_id, "structure_ai", {}, {})
        result = run_structure_job(self.store, self.project_id, job_id,
            {"proofread": True, "extract_toc": True, "auto_apply": True}, Provider())
        self.assertEqual(len(result["suggestions"]), 1)
        self.assertFalse(result["applied"])
        self.assertEqual(result["review_coverage"], {"candidates": 1, "reviewed": 1, "second_pass": 1, "reused": 0, "needs_review": 0})

    def test_duplicate_roles_do_not_choose_arbitrary_conflict(self):
        class Provider:
            def complete(inner, **_):
                return ProviderResponse(content='{"decisions":[{"block_id":"sub","role":"h1","confidence":0.99},{"block_id":"sub","role":"body","confidence":0.99}]}', provider="mock", model="mock")
        skipped = []
        values, _ = analyze_structure_with_provider(self.doc, self.toc, Provider(), skipped=skipped)
        self.assertFalse(values)
        self.assertTrue(any("重复" in x["reason"] for x in skipped))


    def test_raw_toc_lines_do_not_split_roman_words_or_historical_years(self):
        from literature.structure import local_printed_toc
        self.doc.blocks = [Block(block_id="toc", type="paragraph", order=0, page_index=0,
            source_text="4.2. The mid Ming period .... 139\n4.2.2. The mysterious Li Fuda affair .... 155\n6.2. The 1622 White Lotus uprising .... 227")]
        result = local_printed_toc(self.doc, pages=[0])
        self.assertEqual(len(result["entries"]), 3)
        self.assertEqual([x["printed_page"] for x in result["entries"]], ["139", "155", "227"])


    def test_filtered_raw_headers_still_explain_a_single_mistagged_header(self):
        from literature.models import Page
        self.doc.blocks = [Block(block_id="mistagged", type="title", order=0, page_index=3,
            source_text="Introduction", level=2, bbox=[100, 60, 800, 85]),
            Block(block_id="chapter", type="title", order=1, page_index=0,
            source_text="Introduction", level=2, bbox=[100, 180, 800, 240])]
        self.doc.pages = [Page(page_index=p, raw={"structure_furniture": [
            {"text": "Introduction", "bbox": [100, 60, 800, 85], "type": "page_header"}]}) for p in range(3)]
        values = analyze_structure(self.doc, [])
        self.assertEqual([x["block_id"] for x in values], ["mistagged"])
        self.assertTrue(values[0]["automatic"])


    def test_raw_enrichment_reads_header_spans_without_modifying_raw(self):
        from literature.structure_signals import enrich_furniture_evidence
        project = self.store.project_path(self.project_id)
        raw = project / "ocr/raw/mineru/slice-001/content_list_v2.json"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(json.dumps([[{"type": "page_header", "bbox": [100, 20, 800, 40],
            "content": {"page_header_content": [{"type": "text", "content": "Chapter"}]}}]]))
        original = raw.read_bytes()
        self.doc.source_files = [{"path": str(raw.relative_to(project)), "representation": "content_list_v2", "page_offset": 0}]
        enriched = enrich_furniture_evidence(self.doc, project)
        self.assertEqual(enriched.pages[0].raw["structure_furniture"][0]["text"], "Chapter")
        self.assertNotIn("structure_furniture", self.doc.pages[0].raw)
        self.assertEqual(raw.read_bytes(), original)

    def test_sideways_map_label_is_not_promoted_as_a_chapter(self):
        self.doc.blocks[1].source_text = "LAY BUDDHISM IN THE SONG AND YUAN"
        self.doc.blocks[1].bbox = [955, 246, 978, 744]
        toc = [{"title": "II. Lay Buddhism in the Song and Yuan", "level": 1}]
        self.assertFalse(any(x["block_id"] == "missing" for x in analyze_structure(self.doc, toc)))
        self.assertNotIn("missing", {x["block_id"] for x in structure_model_payload(self.doc, toc)["title_candidates"]})
