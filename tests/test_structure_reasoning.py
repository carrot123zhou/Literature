from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from literature.models import Block, Document
from literature.providers import ProviderConfig, ProviderError, ProviderResponse, ProviderTimeoutError
from literature.storage import ProjectStore
from literature.structure import _batch_payload, apply_patch_batch, revert_patch, run_structure_job, structure_model_payload
from literature.structure_reasoning import outline_inputs, plan_outline, validate_hierarchy


def candidate(bid, text, level=2):
    return {"block_id": bid, "text": text, "type": "title", "level": level,
            "page_index": 1, "bbox": [100, 300, 700, 325]}


def heading(bid, level, parent):
    return {"block_id": bid, "role": f"h{level}", "parent_id": parent, "confidence": .9}


class StructureReasoningTests(unittest.TestCase):
    def test_shallow_toc_can_produce_deep_unnumbered_hierarchy(self):
        # A real pattern from the Journal: article, topic, composition, detail.
        candidates = [candidate("article", "Hephaistos Goes Home"),
                      candidate("section", "THE MAIN FIGURAL DECORATION"),
                      candidate("composition", "The Composition below the Left Handle"),
                      candidate("detail", "The nymph to the left of the volute-krater")]
        decisions = {row["block_id"]: row for row in [heading("article", 1, None),
            heading("section", 2, "article"), heading("composition", 3, "section"), heading("detail", 4, "composition")]}
        result = validate_hierarchy(candidates, decisions, {})
        self.assertEqual(result["checked"], 4)
        self.assertFalse(result["issues"])

    def test_same_level_parent_and_descendants_are_rejected(self):
        candidates = [candidate("article", "Article"), candidate("section", "Section"), candidate("detail", "Detail")]
        rows = [heading("article", 2, None), heading("section", 2, "article"), heading("detail", 3, "section")]
        result = validate_hierarchy(candidates, {r["block_id"]: r for r in rows}, {})
        self.assertEqual(result["checked_ids"], ["article"])
        self.assertEqual({r["block_id"] for r in result["issues"]}, {"section", "detail"})

    def test_parent_cannot_cross_new_article(self):
        candidates = [candidate("a", "Article A"), candidate("b", "Article B"), candidate("s", "Notes")]
        rows = [heading("a", 1, None), heading("b", 1, None), heading("s", 2, "a")]
        result = validate_hierarchy(candidates, {r["block_id"]: r for r in rows}, {})
        self.assertEqual(result["issues"][0]["block_id"], "s")

    def test_missing_parent_is_not_counted_as_verified(self):
        result = validate_hierarchy([candidate("a", "Article")], {"a": {"role": "h1"}}, {})
        self.assertEqual(result["checked"], 0)

    def test_parent_falls_back_only_when_global_role_agrees(self):
        plan = {"nodes": {"a": heading("a", 1, None)}}
        result = validate_hierarchy([candidate("a", "Article")], {"a": {"role": "h2"}}, plan)
        self.assertEqual(result["checked"], 0)
        result = validate_hierarchy([candidate("a", "Article")], {"a": {"role": "h1"}}, plan)
        self.assertEqual(result["checked"], 1)

    def test_windows_cover_every_candidate_once(self):
        candidates = [candidate(f"b{i}", "Title") for i in range(350)]
        requests = outline_inputs({"title_candidates": candidates})
        ids = [row["block_id"] for request in requests for row in request["candidates"]]
        self.assertEqual(ids, [row["block_id"] for row in candidates])
        self.assertEqual(len(requests), 6)
        self.assertTrue(all(len(value["candidates"]) <= 64 for value in requests))
        self.assertLessEqual(len(requests[0]["overview"]), 48)

    def test_window_boundary_carries_ancestors_and_reports_progress(self):
        candidates = [candidate(f"b{i}", f"Title {i}") for i in range(70)]
        calls, progress = [], []
        class Provider:
            def complete(self, *, system, user):
                payload = json.loads(user)
                calls.append(payload)
                rows = [heading(row["block_id"], 1 if row["block_id"] == "b0" else 2,
                                None if row["block_id"] == "b0" else "b0") for row in payload["candidates"]]
                return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
        result = plan_outline({"title_candidates": candidates}, Provider(), on_window=lambda *args: progress.append(args))
        self.assertEqual(result["planned"], 70)
        self.assertEqual(progress, [(1, 2, 64), (2, 2, 6)])
        self.assertIn("b0", {row["block_id"] for row in calls[1]["previous_outline"]})
        self.assertEqual(calls[1]["previous_outline"][0]["text"], "Title 0")

    def timeout_fixture(self, root, *, malformed_first=False):
        source = root / "source.html"
        source.write_text("fixture")
        store = ProjectStore(root / "library")
        project = store.create_project(source)["project_id"]
        document = Document(project_id=project, title="Fixture", source_language="en", page_count=2, blocks=[
            Block(block_id=f"b{i}", type="title", order=i, page_index=1, source_text=f"Topic {i} discussion", level=2)
            for i in range(70)])
        store.save_document(document)
        class Provider:
            config = ProviderConfig.preset("custom", base_url="mock://fixture", model="fixture")
            def __init__(self, fail=False, malformed=False):
                self.calls, self.fail, self.malformed = [], fail, malformed
            def complete(self, *, system, user):
                payload = json.loads(user)
                self.calls.append(payload)
                if self.malformed and payload.get("window") == 1:
                    return ProviderResponse(content='{"outline":', provider="mock", model="fixture", input_tokens=10)
                if self.fail and payload.get("window") == 2:
                    raise ProviderTimeoutError("read", 600)
                candidates = payload.get("candidates", payload.get("title_candidates", []))
                rows = [heading(row["block_id"], 1 if row["block_id"] == "b0" else 2,
                                None if row["block_id"] == "b0" else "b0") for row in candidates]
                return ProviderResponse(content=json.dumps({"outline" if payload.get("stage") else "decisions": rows}),
                                        provider="mock", model="fixture", input_tokens=10)
        options = {"proofread": True, "extract_toc": False, "request_timeout_seconds": 600}
        job = store.create_job(project, "structure_ai", {}, options)
        provider = Provider(fail=True, malformed=malformed_first)
        with self.assertRaisesRegex(ProviderError, "整体标题树规划.*600"):
            run_structure_job(store, project, job, options, provider)
        failed = store.get_job(project, job)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(failed["status"], "failed")
        requests = failed["result"]["audit"]["requests"]
        self.assertEqual(requests[-1]["timeout_kind"], "read")
        self.assertEqual(requests[-1]["timeout_seconds"], 600)
        self.assertIn("elapsed_seconds", requests[-1])
        self.assertEqual(store.list_patches(project), [])
        return store, project, job, options, Provider

    def test_retry_does_not_reuse_malformed_model_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project, job, options, Provider = self.timeout_fixture(Path(tmp), malformed_first=True)
            self.assertFalse(store.get_job(project, job)["result"]["audit"]["requests"][0]["cacheable"])
            retry = store.create_job(project, "structure_ai", {}, {})
            provider = Provider()
            result = run_structure_job(store, project, retry, {**options, "reuse_job_id": job}, provider)
            self.assertEqual(len(provider.calls), 4)
            self.assertEqual(result["audit"]["reused_requests"], 0)

    def test_retry_reuses_successes_across_failures_without_duplicate_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project, job, options, Provider = self.timeout_fixture(Path(tmp))
            document_before = store.load_document(project).to_dict()
            options = {**options, "reuse_job_id": job}
            retry = store.create_job(project, "structure_ai", {}, options)
            provider = Provider(fail=True)
            with self.assertRaises(ProviderError):
                run_structure_job(store, project, retry, options, provider)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(store.get_job(project, retry)["result"]["audit"]["reused_requests"], 1)
            options = {**options, "reuse_job_id": retry}
            resumed = store.create_job(project, "structure_ai", {}, options)
            provider = Provider()
            result = run_structure_job(store, project, resumed, options, provider)
            self.assertEqual(len(provider.calls), 3)  # 第二规划窗口 + 两批复核
            self.assertEqual(result["audit"]["reused_requests"], 1)
            self.assertEqual(result["hierarchy_review"]["checked"], 70)
            self.assertEqual(store.load_document(project).to_dict(), document_before)
            self.assertEqual(sum(row["input_tokens"] for row in store.usage_records(project)), 40)

    def test_retry_never_reuses_changed_document_model_or_input(self):
        for changed in ("document", "model", "input"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                store, project, job, options, Provider = self.timeout_fixture(Path(tmp))
                provider = Provider()
                if changed == "document":
                    document = store.load_document(project)
                    document.blocks[0].level = 1
                    store.save_document(document)
                elif changed == "model":
                    provider.config = ProviderConfig.preset("custom", base_url="mock://fixture", model="different")
                else:
                    options = {**options, "toc": [{"title": "Topic 0 discussion", "level": 1}]}
                options = {**options, "reuse_job_id": job}
                retry = store.create_job(project, "structure_ai", {}, options)
                result = run_structure_job(store, project, retry, options, provider)
                # 上下文（文档/模型/输入）变化后不得复用任何已缓存响应；
                # 选择性复核会减少第二次请求数量，因此这里校验缓存契约而不是固定调用次数。
                self.assertEqual(result["audit"]["reused_requests"], 0)
                self.assertTrue(result["audit"]["requests"])
                self.assertTrue(all(item.get("status") != "reused" for item in result["audit"]["requests"]))
                self.assertGreater(len(provider.calls), 0)

    def test_cross_batch_parent_is_readonly_context(self):
        candidates = [candidate(f"b{i}", "Title") for i in range(45)]
        nodes = {"b0": heading("b0", 1, None), "b44": heading("b44", 2, "b0")}
        batch = _batch_payload({"title_candidates": candidates, "hierarchy_plan": {"nodes": nodes}}, candidates, 40, 5)
        self.assertIn("b0", {row["block_id"] for row in batch["heading_context"]})
        self.assertNotIn("b0", {row["block_id"] for row in batch["title_candidates"]})

    def test_duplicate_uncertain_illegal_global_nodes_are_not_planned(self):
        class Provider:
            def complete(self, **_):
                rows = [heading("a", 1, None), heading("a", 2, None),
                        {**heading("b", 1, None), "role": "uncertain"},
                        {**heading("c", 1, None), "confidence": float("nan")}]
                return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
        result = plan_outline({"title_candidates": [candidate(x, x) for x in "abc"]}, Provider())
        self.assertEqual(result["planned"], 0)
        self.assertNotIn("a", result["nodes"])
        self.assertNotIn("c", result["nodes"])

    def test_planning_network_failure_is_not_retried(self):
        class Provider:
            calls = 0
            def complete(self, **_):
                self.calls += 1
                raise ProviderError("network error")
        provider = Provider()
        with self.assertRaises(ProviderError):
            plan_outline({"title_candidates": [candidate("a", "A")]}, provider)
        self.assertEqual(provider.calls, 1)

    def test_confirmed_local_match_still_receives_ai_review(self):
        doc = Document(project_id="f", title="F", source_language="en", page_count=2,
            blocks=[Block(block_id="a", type="title", order=0, page_index=1, source_text="Article", level=1)])
        payload = structure_model_payload(doc, [{"title": "Article", "level": 2}], excluded_pages=set())
        self.assertIn("a", {row["block_id"] for row in payload["title_candidates"]})

    def test_unumbered_short_paragraph_is_candidate_without_sending_body(self):
        doc = Document(project_id="f", title="F", source_language="en", page_count=2, blocks=[
            Block(block_id="s", type="paragraph", order=0, page_index=1, source_text="Discussion of the mule", bbox=[100, 300, 600, 316]),
            Block(block_id="body", type="paragraph", order=1, page_index=1, source_text="Private body text " * 30, bbox=[100, 350, 600, 550])])
        payload = structure_model_payload(doc, [], excluded_pages=set())
        self.assertIn("s", {row["block_id"] for row in payload["title_candidates"]})
        self.assertNotIn("Private body text", json.dumps(payload))

    def test_old_suggestions_migrate_without_losing_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "old.html"
            source.write_text("fixture")
            store = ProjectStore(root / "library")
            project = store.create_project(source)["project_id"]
            store.save_structure_suggestions(project, "old-job", [{"block_id": "a", "operation": "set_level",
                "before": 2, "after": 1, "confidence": .9, "reason": "old"}])
            row = store.list_structure_suggestions(project)[0]
            store.decide_structure_suggestion(project, row["suggestion_id"], "dismissed")
            with sqlite3.connect(store.project_path(project) / "literature.sqlite") as connection:
                connection.execute("ALTER TABLE structure_suggestions DROP COLUMN metadata_json")
            reopened = ProjectStore(store.root)
            rows = reopened.list_structure_suggestions(project)
            self.assertEqual(rows[0]["decision"], "dismissed")
            self.assertEqual(rows[0]["before"], 2)
            self.assertNotIn("parent_id", rows[0])

    def test_full_job_keeps_tree_manual_and_raw_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.html"
            source.write_text("fixture")
            store = ProjectStore(root / "library")
            project = store.create_project(source)["project_id"]
            doc = Document(project_id=project, title="Fixture", source_language="en", page_count=2, blocks=[
                Block(block_id="a", type="title", order=0, page_index=1, source_text="Article", level=2),
                Block(block_id="s", type="title", order=1, page_index=1, source_text="Section", level=2),
                Block(block_id="d", type="title", order=2, page_index=1, source_text="Detail", level=2)])
            store.save_document(doc)
            raw = store.project_path(project) / "ocr/raw/mineru/evidence.bin"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(b"immutable evidence")
            rows = [heading("a", 1, None), heading("s", 2, "a"), heading("d", 3, "s")]
            class Provider:
                def complete(self, *, system, user):
                    p = json.loads(user)
                    if p.get("stage"):
                        value = {"profile": {"toc_scope": "articles only"}, "outline": rows}
                    else:
                        self_context = p["hierarchy_plan"]["nodes"]
                        assert "a" in self_context
                        value = {"decisions": rows}
                    return ProviderResponse(content=json.dumps(value), provider="mock", model="mock", input_tokens=10)
            job = store.create_job(project, "structure_ai", {}, {})
            result = run_structure_job(store, project, job, {"proofread": True, "auto_apply": True}, Provider())
            self.assertEqual(result["hierarchy_review"]["checked"], 3)
            self.assertEqual([(r["block_id"], r["after"]) for r in result["suggestions"]], [("a", 1), ("d", 3)])
            self.assertEqual(result["suggestions"][1]["parent_title"], "Section")
            reopened = ProjectStore(store.root)
            stored = reopened.list_structure_suggestions(project, "pending")
            self.assertEqual(next(row for row in stored if row["block_id"] == "d")["parent_id"], "s")
            self.assertEqual(next(row for row in stored if row["block_id"] == "d")["parent_title"], "Section")
            self.assertFalse(result["applied"])
            self.assertEqual([b.level for b in store.load_document(project).blocks], [2, 2, 2])
            self.assertEqual({r["scope"] for r in result["audit"]["requests"]}, {"structure_outline", "structure_proofread"})
            self.assertTrue(all(r["response_content"] for r in result["audit"]["requests"]))
            patches = apply_patch_batch(store, project, result["suggestions"], source="ai")
            corrected = store.load_document(project)
            self.assertEqual(corrected.toc[0].children[0].children[0].block_id, "d")
            self.assertEqual([(b.block_id, b.source_text, b.order) for b in corrected.blocks], [(b.block_id, b.source_text, b.order) for b in doc.blocks])
            for patch in reversed(patches):
                revert_patch(store, project, patch.patch_id)
            self.assertEqual([b.level for b in store.load_document(project).blocks], [2, 2, 2])
            self.assertEqual(raw.read_bytes(), b"immutable evidence")
