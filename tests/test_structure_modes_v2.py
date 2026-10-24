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
from literature.providers import ProviderConfig, ProviderError, ProviderResponse
from literature.storage import ProjectStore, StorageError
from literature.structure import (
    apply_all_suggestions,
    apply_automatic,
    apply_patch_batch,
    revert_patch,
    run_structure_job,
    scope_calibration_candidates,
    structure_mode_config,
    structure_model_payload,
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


class StructureBaselinePriorityTests(unittest.TestCase):
    """零高级配置下的基准来源优先级：显式纠错目录页 > 已保存基准 > 自动定位目录页。"""

    def manager(self, store: ProjectStore):
        import types

        from literature.api import JobManager, StructureJobInput  # noqa: F401
        from literature.providers import MockProvider
        manager = JobManager(store, None)
        manager.service_id_for = lambda role, explicit="": "svc"
        manager.provider_from = lambda service_id, provider_input=None, model="": MockProvider()
        manager.models = types.SimpleNamespace(get=lambda service_id: {"model": "mock", "name": "Mock"}, roles=lambda: {})
        manager.executor.submit = lambda fn: None
        return manager

    def test_mode_only_reuses_saved_baseline(self):
        from literature.api import StructureJobInput
        with tempfile.TemporaryDirectory() as tmp:
            store, project = build_fixture(Path(tmp))
            store.save_structure_baseline(project, TOC, "confirmed")
            manager = self.manager(store)
            job_id = manager.start_structure(project, StructureJobInput(mode="coarse"))
            config = store.get_job(project, job_id)["config"]
            self.assertEqual(config.get("toc"), TOC)
            self.assertFalse(config.get("extract_toc"))
            self.assertEqual(config.get("toc_source"), "confirmed")

    def test_auto_extract_baseline_reports_auto_source_not_confirmed(self):
        # 上次校准自动提取的基准被复用时，来源仍是 auto_extract，不得谎报为已确认。
        from literature.api import StructureJobInput
        with tempfile.TemporaryDirectory() as tmp:
            store, project = build_fixture(Path(tmp))
            store.save_structure_baseline(project, TOC, "auto_extract")
            manager = self.manager(store)
            job_id = manager.start_structure(project, StructureJobInput(mode="coarse"))
            config = store.get_job(project, job_id)["config"]
            self.assertEqual(config.get("toc"), TOC)
            self.assertEqual(config.get("toc_source"), "auto_extract")

    def test_explicit_correction_pages_override_saved_baseline(self):
        # 用户在纠错入口指定真实目录页时必须重新提取，不能被已保存基准静默截住。
        from literature.api import StructureJobInput
        with tempfile.TemporaryDirectory() as tmp:
            store, project = build_fixture(Path(tmp))
            store.save_structure_baseline(project, TOC, "confirmed")
            manager = self.manager(store)
            job_id = manager.start_structure(project, StructureJobInput(mode="coarse", toc_page_indexes=[1]))
            config = store.get_job(project, job_id)["config"]
            self.assertEqual(config.get("toc_page_indexes"), [1])
            self.assertTrue(config.get("extract_toc"))
            self.assertFalse(config.get("toc"))

    def test_cleared_pages_restore_saved_baseline_predictably(self):
        # 失败后清空纠错页重试：回到“已保存基准优先”的默认行为，不把失败旧快照带回新任务。
        from literature.api import StructureJobInput
        with tempfile.TemporaryDirectory() as tmp:
            store, project = build_fixture(Path(tmp))
            store.save_structure_baseline(project, TOC, "confirmed")
            manager = self.manager(store)
            failed = store.create_job(project, "structure_ai", {}, {})
            store.update_job(project, failed, status="failed")
            job_id = manager.start_structure(project, StructureJobInput(mode="fine", reuse_job_id=failed))
            config = store.get_job(project, job_id)["config"]
            self.assertEqual(config.get("toc"), TOC)
            self.assertEqual(config.get("mode"), "fine")
            self.assertEqual(config.get("reuse_job_id"), failed)


class TocExtractionAutoPathTests(unittest.TestCase):
    """自动流程的目录提取路径：本地采信自动落库、模型提取空回退、无目录不落库。"""

    def toc_fixture(self, root: Path):
        source = root / "source.pdf"
        source.write_bytes(b"fixture")
        store = ProjectStore(root / "library")
        project = store.create_project(source)["project_id"]
        document = Document(project_id=project, title="Fixture", source_language="en", page_count=2, blocks=[
            Block(block_id="mark", type="title", order=0, page_index=0, source_text="Contents", level=1, bbox=[100, 50, 800, 90]),
            Block(block_id="e0", type="paragraph", order=1, page_index=0, source_text="One Alpha 1", bbox=[100, 120, 800, 150]),
            Block(block_id="e1", type="paragraph", order=2, page_index=0, source_text="Two Beta 7", bbox=[100, 160, 800, 190]),
            Block(block_id="e2", type="paragraph", order=3, page_index=0, source_text="Three Gamma 12", bbox=[100, 200, 800, 230]),
            Block(block_id="h0", type="title", order=4, page_index=1, source_text="Alpha", level=2, bbox=[100, 300, 700, 325]),
            Block(block_id="h1", type="paragraph", order=5, page_index=1, source_text="Beta", bbox=[100, 400, 700, 425]),
            Block(block_id="p0", type="paragraph", order=6, page_index=1, source_text="Body text of the first chapter.", bbox=[100, 500, 700, 560]),
        ])
        store.save_document(document)
        return store, project

    class TocStub:
        """能应答目录提取（含空提取）、整体规划与逐项复核的最小桩。"""

        def __init__(self, toc=None, roles=None):
            self.calls = []
            self.toc = toc
            self.roles = roles or {}

        def complete(self, *, system, user):
            payload = json.loads(user)
            self.calls.append(payload)
            if "toc_pages" in payload and "stage" not in payload:
                return ProviderResponse(content=json.dumps({"toc": self.toc if self.toc is not None else [
                    {"title": "Alpha", "level": 1}, {"title": "Beta", "level": 2}]}), provider="mock", model="mock")
            if payload.get("stage") == "outline-plan-v2":
                rows = []
                for row in payload.get("candidates", []):
                    role, parent = self.roles.get(row["block_id"], ("h2", None))
                    rows.append({"block_id": row["block_id"], "role": role, "parent_id": parent,
                                 "confidence": 0.9, "reason": "stub"})
                return ProviderResponse(content=json.dumps({"outline": rows}), provider="mock", model="mock")
            rows = []
            for row in payload.get("title_candidates", []):
                role, parent = self.roles.get(row["block_id"], ("h2", None))
                rows.append({"block_id": row["block_id"], "role": role, "parent_id": parent,
                             "confidence": 0.9, "reason": "stub"})
            return ProviderResponse(content=json.dumps({"decisions": rows}), provider="mock", model="mock")

    def test_local_trusted_extraction_auto_saves_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project = self.toc_fixture(Path(tmp))
            options = structure_mode_config("coarse")
            job = store.create_job(project, "structure_ai", {}, options)
            result = run_structure_job(store, project, job, options, self.TocStub())
            self.assertEqual(result["baseline_source"], "local")
            self.assertEqual(result["toc_extraction"], "local")
            stored = store.load_structure_baseline(project)
            self.assertEqual(stored["source"], "auto_extract")
            self.assertEqual([item["title"] for item in stored["toc"]], ["One Alpha", "Two Beta", "Three Gamma"])

    def test_model_extract_empty_falls_back_to_local_draft_and_completes(self):
        # 疑似目录页存在（无显式标记）但模型提取为空：不硬失败，回退本地草稿继续校准；
        # 本地草稿只是模型只读线索，不作为 authoritative_toc，不收窄候选、不持久化；
        # 自动疑似页在提取失败后恢复候选，页上的真实标题不丢。
        with tempfile.TemporaryDirectory() as tmp:
            store, project = self.toc_fixture(Path(tmp))
            # 去掉显式目录标记页，让 trust_local 不成立、走模型提取路径。
            document = store.load_document(project)
            document.block("mark").source_text = "Preface"
            store.save_document(document)
            options = structure_mode_config("coarse")
            job = store.create_job(project, "structure_ai", {}, options)
            stub = self.TocStub(toc=[])
            result = run_structure_job(store, project, job, options, stub)
            self.assertEqual(store.get_job(project, job)["status"], "completed")
            self.assertEqual(result["baseline_source"], "local_draft")
            self.assertEqual(store.load_structure_baseline(project)["toc"], [])
            # 草稿只进模型请求的只读线索字段，不冒充 authoritative 目录。
            outline = next(call for call in stub.calls if call.get("stage") == "outline-plan-v2")
            self.assertEqual(len(outline["local_toc_draft"]), 3)
            self.assertEqual(outline["toc"], [])
            # 未被疑似目录条目命中的真实标题块仍在候选里（提取失败后恢复候选），且粗校没有按伪目录收窄。
            sent = {row["block_id"] for call in stub.calls for row in call.get("candidates", [])}
            self.assertLessEqual({"h0", "h1", "mark"}, sent)
            self.assertIsNone(result["calibration"].get("candidate_scope"))

    def test_provider_failure_during_extract_fails_job_without_outline(self):
        # 鉴权/网络类真实服务故障不得被吞成「提取空」继续跑：作业失败、
        # 不再发 outline/proofread 请求、不落任何基准。
        with tempfile.TemporaryDirectory() as tmp:
            store, project = self.toc_fixture(Path(tmp))
            document = store.load_document(project)
            document.block("mark").source_text = "Preface"
            store.save_document(document)
            options = structure_mode_config("coarse")
            job = store.create_job(project, "structure_ai", {}, options)

            class AuthFailStub(self.TocStub):
                def complete(inner, *, system, user):
                    payload = json.loads(user)
                    if "toc_pages" in payload and "stage" not in payload:
                        raise ProviderError("服务鉴权失败")
                    raise AssertionError("提取失败后不应继续发 outline/proofread 请求")

            with self.assertRaises(ProviderError):
                run_structure_job(store, project, job, options, AuthFailStub())
            self.assertEqual(store.get_job(project, job)["status"], "failed")
            self.assertEqual(store.load_structure_baseline(project)["toc"], [])

    def test_no_toc_paper_runs_without_extract_or_fake_baseline(self):
        # 无目录论文：不发送 structure_extract 请求、不产生伪印刷目录、标题候选不丢失、
        # 粗/精两档都能自动完成并自动应用；并断言实际输出层级（不是只看 applied>0）。
        roles = {
            "t_main": ("h1", None), "t_authors": ("body", None), "t_abstract": ("h2", "t_main"),
            "t_keywords": ("body", None), "t_intro": ("h2", "t_main"), "t_method": ("h2", "t_main"),
            "t_setup": ("h3", "t_method"), "t_conclusion": ("h2", "t_main"), "t_references": ("h2", "t_main"),
            "ref1": ("body", None), "ref2": ("body", None), "t_figure": ("caption", None),
        }
        for mode in ("coarse", "fine"):
            with tempfile.TemporaryDirectory() as tmp_mode:
                # 每档用全新 fixture：粗校已把误标自动改正，精校再跑就不是同样的候选了。
                store, project = self.paper_fixture(Path(tmp_mode))
                options = structure_mode_config(mode)
                job = store.create_job(project, "structure_ai", {}, options)
                stub = self.TocStub(roles=roles)
                result = run_structure_job(store, project, job, options, stub)
                extract_calls = [call for call in stub.calls if "toc_pages" in call and "stage" not in call]
                self.assertEqual(extract_calls, [], mode)
                self.assertEqual(result["baseline_source"], "none", mode)
                self.assertEqual(result["toc_extraction"], "none", mode)
                # 参考文献条目与作者行没有被整页排除，仍然送给了模型判断。
                sent = {row["block_id"] for call in stub.calls for row in call.get("candidates", []) if "stage" not in call or call.get("stage") == "outline-plan-v2"}
                self.assertLessEqual({"ref1", "ref2", "t_authors", "t_keywords", "t_figure"}, sent, mode)
                # 自动应用完成，无待确认项；无目录推断不落伪基准。
                self.assertEqual(store.get_job(project, job)["status"], "completed", mode)
                self.assertGreater(result["calibration"]["applied"], 0, mode)
                self.assertEqual(store.load_structure_baseline(project)["toc"], [], mode)
                # 实际输出层级：正文节校准为 H2、子节 H3（MinerU 原误标 H3/正文），作者/关键词/
                # 参考文献条目/图题降为正文；父子关系校验后无丢弃。
                document = store.load_document(project)
                self.assertEqual(document.block("t_intro").level, 2, mode)
                self.assertEqual(document.block("t_setup").type, "title", mode)
                self.assertEqual(document.block("t_setup").level, 3, mode)
                self.assertEqual(document.block("t_authors").type, "paragraph", mode)
                self.assertEqual(document.block("t_keywords").type, "paragraph", mode)
                self.assertEqual(document.block("ref1").type, "paragraph", mode)
                self.assertEqual(document.block("t_figure").type, "paragraph", mode)
                self.assertTrue(result["hierarchy_review"], mode)
                # 可逆：撤销全部修订后误标块回到初始状态。
                for patch in store.list_patches(project):
                    if not patch["reverted_at"]:
                        revert_patch(store, project, patch["patch_id"])
                document = store.load_document(project)
                self.assertEqual(document.block("ref1").type, "title", mode)
                self.assertEqual(document.block("t_authors").type, "title", mode)
                self.assertEqual(document.block("t_intro").level, 3, mode)

    def test_chinese_no_toc_paper_levels_and_reversibility(self):
        # 中文无目录论文：摘要/关键词/章与节/参考文献，断言候选送达、实际层级与可逆。
        roles = {
            "zh_main": ("h1", None), "zh_authors": ("body", None), "zh_abstract": ("h2", "zh_main"),
            "zh_kw": ("body", None), "zh_intro": ("h2", "zh_main"), "zh_method": ("h2", "zh_main"),
            "zh_setup": ("h3", "zh_method"), "zh_conclusion": ("h2", "zh_main"), "zh_refs": ("h2", "zh_main"),
            "zh_ref1": ("body", None), "zh_ref2": ("body", None),
        }
        with tempfile.TemporaryDirectory() as tmp:
            store, project = self.chinese_paper_fixture(Path(tmp))
            options = structure_mode_config("coarse")
            job = store.create_job(project, "structure_ai", {}, options)
            stub = self.TocStub(roles=roles)
            result = run_structure_job(store, project, job, options, stub)
            extract_calls = [call for call in stub.calls if "toc_pages" in call and "stage" not in call]
            self.assertEqual(extract_calls, [])
            self.assertEqual(result["baseline_source"], "none")
            sent = {row["block_id"] for call in stub.calls for row in call.get("candidates", []) if "stage" not in call or call.get("stage") == "outline-plan-v2"}
            self.assertLessEqual({"zh_authors", "zh_kw", "zh_ref1", "zh_ref2", "zh_intro", "zh_setup"}, sent)
            self.assertEqual(store.get_job(project, job)["status"], "completed")
            self.assertGreater(result["calibration"]["applied"], 0)
            # 实际输出：章 H2（原误标 H3）、节 H3、作者/关键词/参考文献条目降为正文。
            document = store.load_document(project)
            self.assertEqual(document.block("zh_intro").type, "title")
            self.assertEqual(document.block("zh_intro").level, 2)
            self.assertEqual(document.block("zh_setup").level, 3)
            self.assertEqual(document.block("zh_authors").type, "paragraph")
            self.assertEqual(document.block("zh_kw").type, "paragraph")
            self.assertEqual(document.block("zh_ref1").type, "paragraph")
            self.assertEqual(document.block("zh_ref2").type, "paragraph")
            self.assertEqual(store.load_structure_baseline(project)["toc"], [])
            # 可逆。
            for patch in store.list_patches(project):
                if not patch["reverted_at"]:
                    revert_patch(store, project, patch["patch_id"])
            document = store.load_document(project)
            self.assertEqual(document.block("zh_intro").level, 3)
            self.assertEqual(document.block("zh_authors").type, "title")
            self.assertEqual(document.block("zh_ref1").type, "title")

    def paper_fixture(self, root: Path):
        source = root / "source.pdf"
        source.write_bytes(b"fixture")
        store = ProjectStore(root / "library")
        project = store.create_project(source)["project_id"]
        abstract = "We study structure calibration for scanned documents and report findings across corpora with careful analysis of failure modes." * 2
        document = Document(project_id=project, title="Fixture Paper", source_language="en", page_count=3, blocks=[
            Block(block_id="t_main", type="title", order=0, page_index=0, source_text="Attention Is All You Need", level=1, bbox=[100, 60, 800, 100]),
            Block(block_id="t_authors", type="title", order=1, page_index=0, source_text="Alice Author, Bob Author and Carol Colleague", level=2, bbox=[100, 120, 800, 150]),
            Block(block_id="t_abstract", type="title", order=2, page_index=0, source_text="Abstract", level=2, bbox=[100, 180, 800, 210]),
            Block(block_id="p_abstract", type="paragraph", order=3, page_index=0, source_text=abstract, bbox=[100, 230, 800, 420]),
            Block(block_id="t_keywords", type="title", order=4, page_index=0, source_text="Keywords: attention; transformers; calibration", level=2, bbox=[100, 440, 800, 470]),
            Block(block_id="t_intro", type="title", order=5, page_index=1, source_text="1 Introduction", level=3, bbox=[100, 80, 800, 110]),
            Block(block_id="p_intro", type="paragraph", order=6, page_index=1, source_text=abstract, bbox=[100, 130, 800, 320]),
            Block(block_id="t_figure", type="title", order=7, page_index=1, source_text="Figure 1. System overview.", level=2, bbox=[100, 340, 800, 370]),
            Block(block_id="t_method", type="title", order=8, page_index=1, source_text="2 Method", level=2, bbox=[100, 400, 800, 430]),
            Block(block_id="t_setup", type="title", order=9, page_index=1, source_text="2.1 Setup", level=3, bbox=[100, 450, 800, 478]),
            Block(block_id="t_conclusion", type="title", order=10, page_index=2, source_text="3 Conclusion", level=2, bbox=[100, 80, 800, 110]),
            Block(block_id="t_references", type="title", order=11, page_index=2, source_text="References", level=2, bbox=[100, 140, 800, 170]),
            Block(block_id="ref1", type="title", order=12, page_index=2, source_text="[1] Smith, J. On Books. Journal of Pages, 12: 3-20.", level=2, bbox=[100, 190, 800, 218]),
            Block(block_id="ref2", type="title", order=13, page_index=2, source_text="[2] Doe, A. More Notes. Annals of Things, 8: 55-70.", level=2, bbox=[100, 230, 800, 258]),
        ])
        store.save_document(document)
        return store, project

    def chinese_paper_fixture(self, root: Path):
        source = root / "source.pdf"
        source.write_bytes(b"fixture")
        store = ProjectStore(root / "library")
        project = store.create_project(source)["project_id"]
        abstract = "针对扫描文献中标题误标、漏标与层级错乱问题，本文提出一种以差分校准为核心的结构校准方法，" \
                   "结合印刷目录证据、标题群形态与版面几何进行有界推断，并在真实语料上验证了方法的有效性与可逆性。" * 2
        document = Document(project_id=project, title="中文论文Fixture", source_language="zh-CN", page_count=3, blocks=[
            Block(block_id="zh_main", type="title", order=0, page_index=0, source_text="基于大模型的文献结构校准研究", level=1, bbox=[100, 60, 800, 100]),
            Block(block_id="zh_authors", type="title", order=1, page_index=0, source_text="张三、李四", level=2, bbox=[100, 120, 800, 150]),
            Block(block_id="zh_abstract", type="title", order=2, page_index=0, source_text="摘要", level=2, bbox=[100, 180, 800, 210]),
            Block(block_id="p_zh_abs", type="paragraph", order=3, page_index=0, source_text=abstract, bbox=[100, 230, 800, 430]),
            Block(block_id="zh_kw", type="title", order=4, page_index=0, source_text="关键词：结构校准；大模型；目录", level=2, bbox=[100, 450, 800, 480]),
            Block(block_id="zh_intro", type="title", order=5, page_index=1, source_text="1 引言", level=3, bbox=[100, 80, 800, 110]),
            Block(block_id="p_zh_body", type="paragraph", order=6, page_index=1, source_text=abstract, bbox=[100, 130, 800, 330]),
            Block(block_id="zh_method", type="title", order=7, page_index=1, source_text="2 方法", level=2, bbox=[100, 360, 800, 390]),
            Block(block_id="zh_setup", type="title", order=8, page_index=1, source_text="2.1 数据", level=2, bbox=[100, 410, 800, 438]),
            Block(block_id="zh_conclusion", type="title", order=9, page_index=2, source_text="3 结论", level=2, bbox=[100, 80, 800, 110]),
            Block(block_id="zh_refs", type="title", order=10, page_index=2, source_text="参考文献", level=2, bbox=[100, 140, 800, 170]),
            Block(block_id="zh_ref1", type="title", order=11, page_index=2, source_text="[1] 王五. 数字人文视域下的古籍整理研究. 中国图书馆学报, 2021, 47(5): 12-28.", level=2, bbox=[100, 190, 800, 218]),
            Block(block_id="zh_ref2", type="title", order=12, page_index=2, source_text="[2] 赵六. 深度学习在文档分析中的应用. 计算机学报, 2020, 43(3): 55-70.", level=2, bbox=[100, 230, 800, 258]),
        ])
        store.save_document(document)
        return store, project


class BaselineExclusionConsistencyTests(unittest.TestCase):
    """目录页兜底排除与自动识别同口径：参考文献等误识别页上的真实标题不被整页抹掉。"""

    def test_reference_page_titles_survive_without_marker(self):
        blocks = [Block(block_id=f"r{i}", type="title", order=i, page_index=0,
                        source_text=f"[{'i' if False else i}] Smith, J. Long Essay Title Here. Journal of Pages, 12: 3-{20 + i}.",
                        level=2, bbox=[100, 60 + i * 40, 800, 88 + i * 40]) for i in range(1, 5)]
        blocks.append(Block(block_id="t_refs", type="title", order=0, page_index=0,
                            source_text="References", level=2, bbox=[100, 20, 800, 50]))
        document = Document(project_id="p", title="F", source_language="en", page_count=1, blocks=blocks)
        payload = structure_model_payload(document, [])
        sent = {item["block_id"] for item in payload["title_candidates"]}
        self.assertLessEqual({"r1", "r2", "t_refs"}, sent)


if __name__ == "__main__":
    unittest.main()
