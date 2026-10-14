"""结构校对档位（粗校/精校）、候选收窄、全部自动应用与暂停/继续回归。

背景（2026-10-）：用户要求把 AI 结构校对重定义为“以原书目录为基准的差分校准”，
只暴露粗校/精校 + 开始/暂停/继续，其余策略由程序决定；修正建议全部自动应用，
安全网是可逆补丁 + 修订历史。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from literature.models import Block, Document
from literature.providers import ProviderConfig, ProviderResponse
from literature.storage import ProjectStore, StorageError
from literature.structure import (
    apply_all_suggestions,
    apply_automatic,
    apply_patch_batch,
    run_structure_job,
    scope_calibration_candidates,
    structure_mode_config,
)

TOC = [
    {"title": "Foundations", "level": 1},
    {"title": "Section Alpha", "level": 2},
    {"title": "Concluding Remarks", "level": 1},
]
ROLES = {"b0": ("h1", None), "b1": ("h2", "b0"), "b3": ("h1", None)}


class ModeConfigTests(unittest.TestCase):
    def test_coarse_preset_is_local_first_and_auto_applies(self):
        config = structure_mode_config("coarse")
        self.assertEqual(config["mode"], "coarse")
        self.assertTrue(config["extract_toc"])
        self.assertTrue(config["proofread"])
        self.assertTrue(config["auto_apply"])
        self.assertEqual(config["toc_extraction"], "auto")
        self.assertEqual(config["proofread_mode"], "off")
        self.assertEqual(config["planning_mode"], "global")
        self.assertFalse(config["use_vision"])

    def test_fine_differs_from_coarse_only_by_mode_flag(self):
        coarse = structure_mode_config("coarse")
        fine = structure_mode_config("fine")
        self.assertEqual(fine["mode"], "fine")
        for key in coarse:
            if key != "mode":
                self.assertEqual(coarse[key], fine[key], key)

    def test_advanced_overrides_preset_but_mode_stays(self):
        config = structure_mode_config("coarse", {"use_vision": False, "request_timeout_seconds": 900, "proofread_mode": "full"})
        self.assertEqual(config["request_timeout_seconds"], 900)
        self.assertEqual(config["proofread_mode"], "full")
        self.assertEqual(config["mode"], "coarse")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(StorageError):
            structure_mode_config("fastest")


class CandidateScopeTests(unittest.TestCase):
    def payload(self):
        def candidate(bid, kind="title", role="heading", score=0.7, text=None):
            return {"block_id": bid, "type": kind, "text": text or bid,
                    "local": {"role": role, "heading_score": score}}
        return {
            "title_candidates": [
                candidate("matched", "title", "heading", 0.9),
                candidate("promotion", "paragraph", "paragraph", 0.1),
                candidate("clean_unlisted", "title", "heading", 0.8, "A perfectly normal heading"),
                candidate("suspicious_unlisted", "title", "furniture", 0.1, "Running Header 42"),
            ],
            "matched_titles": [{"block_id": "matched"}, {"block_id": "promotion"}],
            "local_suggestions": [],
            "unlisted_titles": [{"block_id": "clean_unlisted"}, {"block_id": "suspicious_unlisted"}],
        }

    def test_coarse_keeps_toc_related_and_drops_clean_unlisted(self):
        scoped = scope_calibration_candidates(self.payload())
        kept = {item["block_id"] for item in scoped["title_candidates"]}
        self.assertEqual(kept, {"matched", "promotion", "suspicious_unlisted"})
        self.assertEqual(scoped["candidate_scope"]["before"], 4)
        self.assertEqual(scoped["candidate_scope"]["after"], 3)


class AutoApplyTests(unittest.TestCase):
    def fixture(self, root):
        source = root / "source.html"
        source.write_text("fixture")
        store = ProjectStore(root / "library")
        project = store.create_project(source)["project_id"]
        document = Document(project_id=project, title="Fixture", source_language="en", page_count=1, blocks=[
            Block(block_id="p", type="paragraph", order=0, page_index=0, source_text="Promote Me"),
            Block(block_id="t", type="title", order=1, page_index=0, source_text="Demote Me", level=2),
        ])
        store.save_document(document)
        return store, project

    def test_strict_boundary_rejects_model_style_promotion(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project = self.fixture(Path(tmp))
            with self.assertRaises(StorageError):
                apply_automatic(store, project, [{
                    "block_id": "p", "operation": "set_title", "after": 2, "before": None,
                    "confidence": 0.99, "reason": "model", "automatic": True,
                }])

    def test_apply_all_lands_promotion_and_demotion_reversibly(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project = self.fixture(Path(tmp))
            patches = apply_all_suggestions(store, project, [
                {"block_id": "p", "operation": "set_title", "after": 2, "before": None,
                 "confidence": 0.4, "reason": "promotion", "automatic": True},
                {"block_id": "t", "operation": "set_type", "after": "footnote", "before": None,
                 "confidence": 0.4, "reason": "mis-tag", "automatic": True},
            ])
            self.assertEqual(len(patches), 2)
            document = store.load_document(project)
            self.assertEqual(document.block("p").type, "title")
            self.assertEqual(document.block("p").level, 2)
            self.assertEqual(document.block("t").type, "footnote")
            self.assertEqual(len(store.list_patches(project)), 2)


class ProviderStub:
    config = ProviderConfig.preset("custom", base_url="mock://fixture", model="fixture")

    def __init__(self, *, unplanned=(), roles=None):
        self.calls = []
        self.unplanned = set(unplanned)
        self.roles = roles or ROLES

    def complete(self, *, system, user):
        payload = json.loads(user)
        self.calls.append(payload)
        if payload.get("stage") == "outline-plan-v2":
            rows = []
            for row in payload.get("candidates", []):
                if row["block_id"] in self.unplanned:
                    continue
                role, parent = self.roles.get(row["block_id"], ("h2", None))
                rows.append({"block_id": row["block_id"], "role": role, "parent_id": parent,
                             "confidence": 0.9, "reason": "stub"})
            return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="fixture")
        rows = []
        for row in payload.get("title_candidates", []):
            role, parent = self.roles.get(row["block_id"], ("h2", None))
            rows.append({"block_id": row["block_id"], "role": role, "parent_id": parent,
                         "confidence": 0.9, "reason": "stub"})
        return ProviderResponse(content=json.dumps({"decisions": rows}), provider="mock", model="fixture")


def build_fixture(root: Path):
    source = root / "source.html"
    source.write_text("fixture")
    store = ProjectStore(root / "library")
    project = store.create_project(source)["project_id"]
    document = Document(project_id=project, title="Fixture", source_language="en", page_count=1, blocks=[
        Block(block_id="b0", type="title", order=0, page_index=0, source_text="Foundations", level=2, bbox=[100, 300, 700, 325]),
        Block(block_id="b1", type="paragraph", order=1, page_index=0, source_text="Section Alpha", bbox=[100, 400, 700, 425]),
        Block(block_id="b2", type="title", order=2, page_index=0, source_text="A Perfectly Normal Unlisted Heading", level=3, bbox=[100, 500, 700, 525]),
        Block(block_id="b3", type="paragraph", order=3, page_index=0, source_text="Concluding Remarks", bbox=[100, 600, 700, 625]),
    ])
    store.save_document(document)
    return store, project


def coarse_options():
    return {**structure_mode_config("coarse", {"extract_toc": False}), "toc": TOC, "request_timeout_seconds": 600}


class CoarseJobTests(unittest.TestCase):
    def test_coarse_scopes_candidates_and_auto_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project = build_fixture(Path(tmp))
            options = coarse_options()
            job = store.create_job(project, "structure_ai", {}, options)
            provider = ProviderStub()
            result = run_structure_job(store, project, job, options, provider)
            self.assertEqual(result["mode"], "coarse")
            self.assertEqual(result["calibration"]["mode"], "coarse")
            # 未收录的正常标题 b2 没有发送给模型。
            sent = {row["block_id"] for call in provider.calls for row in call.get("candidates", [])}
            self.assertEqual(sent, {"b0", "b1", "b3"})
            self.assertEqual(result["calibration"]["level_corrections"], 1)
            self.assertEqual(result["calibration"]["added_missing"], 2)
            self.assertEqual(result["calibration"]["removed_mistags"], 0)
            # 界面单行摘要读扁平 categories。
            self.assertEqual(result["categories"], {"removed": 0, "added": 2, "level": 1})
            # 全部自动应用，没有待确认项。
            self.assertEqual(result["calibration"]["applied"], 3)
            self.assertEqual(store.list_structure_suggestions(project, "pending"), [])
            document = store.load_document(project)
            self.assertEqual(document.block("b0").level, 1)
            self.assertEqual(document.block("b1").type, "title")
            self.assertEqual(document.block("b3").type, "title")
            self.assertEqual(document.block("b2").type, "title")  # 未被粗校触碰

    def test_pause_at_request_boundary_then_resume_reuses_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project = build_fixture(Path(tmp))
            options = coarse_options()
            # b3 未获规划 → 第二遍硬阻塞，保证存在一个“批”边界可用于暂停。
            provider = ProviderStub(unplanned={"b3"})
            job = store.create_job(project, "structure_ai", {}, options)
            run_structure_job(store, project, job, options, provider, should_pause=lambda: len(provider.calls) >= 1)
            paused = store.get_job(project, job)
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(len(paused["result"]["audit"]["requests"]), 1)

            resumed = store.create_job(project, "structure_ai", {}, {})
            result = run_structure_job(store, project, resumed, {**options, "reuse_job_id": job}, ProviderStub())
            self.assertGreaterEqual(result["audit"]["reused_requests"], 1)
            self.assertEqual(store.get_job(project, resumed)["status"], "completed")

    def test_pause_before_first_request_then_resume(self):
        # 引擎层复用判据已扩展到 paused；这里确认在请求发生前暂停也能继续。
        with tempfile.TemporaryDirectory() as tmp:
            store, project = build_fixture(Path(tmp))
            options = coarse_options()
            provider = ProviderStub()
            job = store.create_job(project, "structure_ai", {}, options)
            run_structure_job(store, project, job, options, provider, should_pause=lambda: True)
            self.assertEqual(store.get_job(project, job)["status"], "paused")
            resumed = store.create_job(project, "structure_ai", {}, {})
            result = run_structure_job(store, project, resumed, {**options, "reuse_job_id": job}, ProviderStub())
            self.assertEqual(store.get_job(project, resumed)["status"], "completed")
            self.assertEqual(result["mode"], "coarse")

    def test_model_unchanged_vetoes_local_wrong_edit(self):
        # 模型明确判断 b0"保持 h2 不变"时，本地"按目录改 h1"的错误改动必须被否决，
        # 不能因为模型对 unchanged 不产出建议就让本地改动留在"全部自动应用"里。
        with tempfile.TemporaryDirectory() as tmp:
            store, project = build_fixture(Path(tmp))
            options = coarse_options()
            job = store.create_job(project, "structure_ai", {}, options)
            provider = ProviderStub(roles={**ROLES, "b0": ("h2", None)})
            result = run_structure_job(store, project, job, options, provider)
            document = store.load_document(project)
            self.assertEqual(document.block("b0").level, 2)
            self.assertEqual(result["calibration"]["level_corrections"], 0)

    def test_legacy_options_without_mode_keep_previous_behaviour(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project = build_fixture(Path(tmp))
            options = {"proofread": True, "extract_toc": False, "toc": TOC, "request_timeout_seconds": 600}
            job = store.create_job(project, "structure_ai", {}, options)
            result = run_structure_job(store, project, job, options, ProviderStub())
            self.assertEqual(result["mode"], "legacy")
            # 旧路径不做全部自动应用，模型建议保持待确认。
            self.assertTrue(store.list_structure_suggestions(project, "pending"))


if __name__ == "__main__":
    unittest.main()
