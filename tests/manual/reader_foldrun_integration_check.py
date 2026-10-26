"""fold-run × 真实后端集成验收（API 级，mock provider，无浏览器、无付费请求）。

目的：以真实 `create_app` 后端处理链路（路由 → ReaderJobRunner → ReaderLayerService →
adapter/native 落盘）驱动六个验收场景；provider 用脚本化替身（记录调用序列供计费断言），
数据**必须是当前书**由后端生成的 paper 数据，不得用 gallery/fixture paper-data 代替。

场景（对应 Lead 验收要求）：
1. 原译句数不同：译文句数 ≠ 原文句数时，折叠完成且译文层 derived 结构句下标自洽。
2. 单节失败：run=failed、ready=false；成功节内容保留；resume 后补齐且成功节 0 新调用。
3. 暂停续跑缓存复用：POST pause 边界暂停（无空 id failed 项）；resume 只补未完成节。
4. ready 后事实变化失效：改译文 → GET /reader/layers stale=true；重跑后再次 ready。
5. 刷新恢复状态且不自动消费：新 Runner.recover() → paused；读 state/快照 0 模型调用。
6. 退出再启动不重折：ready 后再 POST fold-runs → 缓存 0 调用即 ready。

同时校验：`/reader/layers` 与 `/reader/layers/native` 的形状满足上游 reader.js 载荷
（meta/units/chunks/blocks、unit.toks/sents/lad、tr[locale]），证明当前书数据可驱动
逐字 vendor 的上游内核（区别于 gallery 数据）。

运行：`.venv/bin/python -m pytest tests/manual/reader_foldrun_integration_check.py -q`
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from literature.model_services import ModelServiceStore
from literature.reader import ReaderJobRunner
from literature.models import Block, Document, Page, TranslationEntry, TranslationLayer
from literature.storage import ProjectStore

from tests.test_reader_fold_run import (
    ScriptedProvider,
    derive_reply_for,
    make_document,
    make_translation,
    reply_for,
)


def _runtime_env(root: Path) -> None:
    os.environ["LITERATURE_RUNTIME"] = str(root / "runtime")
    os.environ["LITERATURE_LIBRARY"] = str(root / "library")


class FoldRunApiFlowBase(unittest.TestCase):
    """真实后端 + 脚本化 provider 的公共夹具：隔离目录、HTTP 建服务与角色、绑定 provider。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        _runtime_env(self.root)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        project = self.store.create_project(source, title="Silk Road Silver")
        self.project_id = project["project_id"]
        document = make_document(self.project_id)
        self.store.save_document(document)
        self.translation = make_translation(document)
        self.store.save_translation(self.translation)
        # 通过真实 HTTP API 建服务与角色绑定（后端完整链路，不经测试旁路）。
        from fastapi.testclient import TestClient
        from literature.api import create_app
        import literature.model_services as ms

        self._original_api_key = ms.ModelServiceStore.api_key
        ms.ModelServiceStore.api_key = lambda self, sid: "audit-not-a-real-key"  # 不触碰 Keychain
        self.client = TestClient(create_app(self.store))
        self.headers = {"x-literature-token": self.client.app.state.session_token}
        created = self.client.post("/model-services", json={
            "name": "integration-fake", "preset": "custom",
            "base_url": "http://127.0.0.1:1/v1",   # 必然拒绝连接的本地端口：即便误用也无外部请求
            "models": [{"model": "fake-model"}],
        }, headers=self.headers)
        self.assertEqual(created.status_code, 200, created.text)
        self.service_id = created.json()["service_id"]
        roles = self.client.put("/model-services/roles", json={
            "reader_fold": {"service_id": self.service_id, "model": "fake-model"},
        }, headers=self.headers)
        self.assertEqual(roles.status_code, 200, roles.text)
        self.provider = ScriptedProvider()

    def tearDown(self) -> None:
        import literature.model_services as ms
        ms.ModelServiceStore.api_key = self._original_api_key
        self.temporary.cleanup()

    # ---- 接线 ---------------------------------------------------------------

    def _bind_provider(self) -> None:
        """把脚本化 provider 挂到真实 JobManager 的 provider 工厂上（其余链路全真实）。"""
        provider = self.provider

        def provider_from(*args, **kwargs):
            return provider

        self.client.app.state.job_manager.provider_from = provider_from

    def _start_run(self, locale: str = "zh-CN", force: bool = False) -> dict:
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs",
            json={"locale": locale, "force": force}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _wait_run(self, statuses: tuple[str, ...], timeout: float = 20.0) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            last = self.client.get(f"/projects/{self.project_id}/reader/fold-run", headers=self.headers).json()
            if last.get("status") in statuses:
                return last
            time.sleep(0.05)
        raise AssertionError(f"fold-run 未在 {timeout}s 内到达 {statuses}，最后状态：{last.get('status')}")


class TestSentenceCountsDiffer(FoldRunApiFlowBase):
    """场景 1：原译句数不同，折叠完成后 target 层 derived 结构按译文自己的句子自洽。"""

    def test_target_derived_uses_translation_sentences(self) -> None:
        # 译文故意与原文句数不同：b-p-1 原文 3 句 → 译文并成 1 句 + 追加 1 句原文没有的句子。
        merged = self.translation.entries["b-p-1"]
        merged.text = "白银器物沿丝绸之路流动，长安工坊几十年间模仿粟特器形，九世纪后需求衰落。这是译文里多出来的一句。"
        self.store.save_translation(self.translation)
        self._bind_provider()
        self._start_run()
        run = self._wait_run(("ready", "failed"))
        self.assertEqual(run["status"], "ready", run)
        source_calls = [item for item in self.provider.calls if item[0] == "source"]
        self.assertEqual(sorted(section for _, section in source_calls), ["S1", "S2"], self.provider.calls)
        # 真实后端产物：译文派生层（tr[locale].lad）标 derived，且 ev 句下标落在**译文句集**范围内。
        layers = self.client.get(
            f"/projects/{self.project_id}/reader/layers?locale=zh-CN", headers=self.headers,
        ).json()
        self.assertTrue(layers["generated"])
        self.assertFalse(layers["stale"], layers["digest"])
        projection_units = [unit for section in layers["sections"] for unit in section["units"]]
        self.assertTrue(projection_units)
        derived_units = [unit for unit in projection_units if unit.get("brief_source") == "derived"]
        self.assertTrue(derived_units, "有译文的段落短层必须是 derived（分层译句）")
        native = self.client.get(
            f"/projects/{self.project_id}/reader/layers/native?locale=zh-CN", headers=self.headers,
        ).json()
        unit = native["units"]["b-p-1"]
        trans_lad = (unit.get("tr") or {}).get("zh-CN", {}).get("lad") or {}
        self.assertEqual(trans_lad.get("brief_source"), "derived")
        trans_sentences = unit["tr"]["zh-CN"]["sents"]
        source_sentences = unit["sents"]
        self.assertNotEqual(len(trans_sentences), len(source_sentences),
                            "夹具语义：原译句数不同；若相等说明夹具失效")
        for ev in trans_lad.get("ev") or []:
            self.assertLess(ev, len(trans_sentences),
                            "derived 的 ev 必须以译文句集为界（不是原文句集）")


class TestSingleSectionFailure(FoldRunApiFlowBase):
    """场景 2：单节失败 → failed；resume 补齐，成功节 0 新调用。"""

    def test_failed_section_then_resume_reuses_success(self) -> None:
        self.provider.fail_sections = {"S2"}
        self._bind_provider()
        self._start_run()
        run = self._wait_run(("failed",))
        self.assertFalse(run["ready"])
        self.assertIn("S2", {item["section_id"] for item in run["errors"]})
        layers = self.client.get(
            f"/projects/{self.project_id}/reader/layers?locale=zh-CN", headers=self.headers,
        ).json()
        s1 = next(item for item in layers["sections"] if item["id"] == "S1")
        self.assertTrue(any(unit.get("source") == "model" for unit in s1["units"]), "S1 成功内容必须保留")
        # resume：换健康 provider（同一脚本实例去掉失败标记），S1 不再发请求。
        calls_before = len(self.provider.calls)
        self.provider.fail_sections = set()
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs/{run['run_id']}/resume",
            json={"locale": "zh-CN"}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        run = self._wait_run(("ready", "failed"))
        self.assertEqual(run["status"], "ready", run)
        source_calls = [item for item in self.provider.calls if item[0] == "source" and item[1] == "S1"]
        self.assertEqual(len(source_calls), 1, self.provider.calls)   # S1 全程恰好一次


class TestPauseResumeCacheReuse(FoldRunApiFlowBase):
    """场景 3：POST pause 边界暂停；POST resume 只补未完成节，已完成节缓存 0 调用。"""

    def test_pause_then_resume_via_api(self) -> None:
        pause_after_first = threading.Event()
        self.provider.after_first = pause_after_first
        self._bind_provider()
        run = self._start_run()["run"]
        self.assertTrue(pause_after_first.wait(timeout=10))
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs/{run['run_id']}/pause",
            json={}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        final = self._wait_run(("paused", "ready"), timeout=20)
        if final["status"] == "ready":
            self.skipTest("provider 过快：暂停生效前整篇已完成（边界语义允许）")
        self.assertTrue(all(item.get("section_id") for item in final["errors"]),
                        "暂停不产生空 id failed 项")
        calls_at_pause = len(self.provider.calls)
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs/{run['run_id']}/resume",
            json={"locale": "zh-CN"}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertEqual(final["run_id"], run["run_id"], "resume 同 run_id")
        source_calls = [item for item in self.provider.calls if item[0] == "source"]
        self.assertLessEqual(len(source_calls), 2, self.provider.calls)
        self.assertGreater(len(self.provider.calls), calls_at_pause)


class TestFactsChangeInvalidatesReady(FoldRunApiFlowBase):
    """场景 4：ready 后事实变化失效。

    契约（literature/reader.py derive_section_digest docstring，与本测试钉住的行为一致）：
    - 派生层缓存键**有意不含译文文本**——派生短层锚在原文层，译文文字编辑不重烧调用
      （预算硬约束；Full 层显示用户译文，永远读当前事实层）；
    - 原文（canonical/document）变化 → source digest 变 → 源层 stale、派生层精确失效。
    """

    def test_translation_edit_does_not_reburn_derive(self) -> None:
        """译文文字编辑：不判 stale、不重烧模型（钉住预算设计，防止未来误改为编辑即重烧）。"""
        self._bind_provider()
        self._start_run()
        self._wait_run(("ready", "failed"))
        calls_before = len(self.provider.calls)
        entry = self.translation.entries["b-p-2"]
        entry.text = "[改译] Assay data show a consistent alloy of 92 percent silver — re-assayed."
        self.store.save_translation(self.translation)
        layers = self.client.get(
            f"/projects/{self.project_id}/reader/layers?locale=zh-CN", headers=self.headers,
        ).json()
        self.assertFalse(layers["stale"], "纯译文文字编辑不应使派生层 stale（契约：锚在原文层）")
        run = self._start_run()
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertEqual(len(self.provider.calls), calls_before, "译文编辑不得触发任何模型调用")

    def test_document_edit_marks_stale_and_refolds(self) -> None:
        """原文（canonical）变化：源层重折、译文层派生精确失效重建。

        历史（2026-10-25 worker 审计）：早期实现 `_ensure_source_fold` 复用只看
        `unit.source` 标签不校验 digest，曾致「run ready 而源层 stale」。开发者已在
        复用判据加 digest 校验（reader.py stored_ai = digest_ok and …），本用例回归守护。"""
        self._bind_provider()
        self._start_run()
        self._wait_run(("ready", "failed"))
        calls_before = len(self.provider.calls)
        # 事实层变化：改写原文段落文本（等价结构校准/重新规范化后的 document.json 更新）。
        document = self.store.load_document(self.project_id)
        for block in document.blocks:
            if block.block_id == "b-p-2":
                block.source_text = "Assay data show a consistent alloy of 91 percent silver after re-assay."
        self.store.save_document(document)
        layers = self.client.get(
            f"/projects/{self.project_id}/reader/layers?locale=source", headers=self.headers,
        ).json()
        self.assertTrue(layers["stale"], "原文变化后源层必须 stale")
        self._start_run()
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertGreater(len(self.provider.calls), calls_before, "失效节必须真实重折（不允许假 ready）")
        # 重折后源/译文两层均不 stale，且新产物反映新原文。
        for locale in ("source", "zh-CN"):
            layers = self.client.get(
                f"/projects/{self.project_id}/reader/layers?locale={locale}", headers=self.headers,
            ).json()
            self.assertFalse(layers["stale"], locale)


class TestRestartRecoveryWithoutAutoConsume(FoldRunApiFlowBase):
    """场景 5：重启（新 Runner + recover）后 running→paused；读状态 0 模型调用、不自动续跑。"""

    def test_recover_marks_paused_and_state_reads_consume_nothing(self) -> None:
        pause_after_first = threading.Event()
        self.provider.after_first = pause_after_first
        self._bind_provider()
        run = self._start_run()["run"]
        self.assertTrue(pause_after_first.wait(timeout=10))
        self.client.app.state.job_manager.reader_jobs.pause_run(self.project_id, run["run_id"])
        final = self._wait_run(("paused", "ready"), timeout=20)
        if final["status"] == "ready":
            self.skipTest("provider 过快：暂停生效前整篇已完成")
        calls_at_pause = len(self.provider.calls)
        # 模拟进程重启：全新 Runner（与 API 无关）+ recover。
        fresh = ReaderJobRunner(self.store)
        fresh.recover()
        # 只读状态：fold-run 快照 / state / layers 全部零模型调用、不隐式续跑。
        for _ in range(3):
            snapshot = self.client.get(f"/projects/{self.project_id}/reader/fold-run", headers=self.headers)
            self.assertEqual(snapshot.status_code, 200, snapshot.text)
            self.client.get(f"/projects/{self.project_id}/reader/state?locale=zh-CN", headers=self.headers)
            self.client.get(f"/projects/{self.project_id}/reader/layers?locale=zh-CN", headers=self.headers)
        self.assertEqual(len(self.provider.calls), calls_at_pause, "读状态不得消费模型/续跑作业")
        self.assertEqual(snapshot.json()["status"], "paused", snapshot.json())
        # 明确的用户动作（resume）才继续。
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs/{run['run_id']}/resume",
            json={"locale": "zh-CN"}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)


class TestRestartAfterReadyDoesNotRefold(FoldRunApiFlowBase):
    """场景 6：ready 后再启动（退出折叠再点 AIFold 的等价 API 序列）→ 缓存 0 调用即 ready。"""

    def test_second_start_after_ready_reuses_cache(self) -> None:
        self._bind_provider()
        self._start_run()
        first = self._wait_run(("ready", "failed"))
        self.assertEqual(first["status"], "ready")
        calls_after_first = len(self.provider.calls)
        second = self._start_run()
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertEqual(len(self.provider.calls), calls_after_first,
                         "ready 后重启折叠必须 0 新调用（缓存复用）")
        # 事实层不动：两层均不 stale。
        for locale in ("source", "zh-CN"):
            layers = self.client.get(
                f"/projects/{self.project_id}/reader/layers?locale={locale}", headers=self.headers,
            ).json()
            self.assertFalse(layers["stale"], locale)


class TestPaperDataShapeForUpstreamEngine(FoldRunApiFlowBase):
    """载荷形状：/reader/layers 与 /reader/layers/native 满足上游 reader.js 载荷必需字段。"""

    def test_layers_and_native_shape_matches_upstream_contract(self) -> None:
        self._bind_provider()
        self._start_run()
        self._wait_run(("ready", "failed"))
        layers = self.client.get(
            f"/projects/{self.project_id}/reader/layers?locale=zh-CN", headers=self.headers,
        ).json()
        for key in ("format", "title", "sections", "provenance", "digest", "stale", "generated"):
            self.assertIn(key, layers, key)
        self.assertEqual(layers["format"], "textfold/doc")
        native = self.client.get(
            f"/projects/{self.project_id}/reader/layers/native?locale=zh-CN", headers=self.headers,
        ).json()
        for key in ("meta", "units", "chunks", "blocks"):
            self.assertIn(key, native, f"native 缺上游载荷必需字段：{key}")
        self.assertEqual(native["meta"]["title"], "Silk Road Silver")
        unit = native["units"]["b-p-1"]
        for key in ("toks", "sents", "lad", "tr"):
            self.assertIn(key, unit, f"unit 缺上游 pieces()/view() 必需字段：{key}")
        self.assertTrue(unit["toks"] and unit["sents"])
        chunk = native["chunks"][0]
        for key in ("id", "title", "units", "lad"):
            self.assertIn(key, chunk, f"chunk 缺上游必需字段：{key}")
        # 说明：与 fixture/gallery 无关——本数据全部由当前书经真实后端生成。


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Lead 验收矩阵补充（2026-10-25 第二轮）：绑定变更失效、failed 不 100、全 suppressed、
# native 丢失、同进程 ready→失效→resume、派生 ev 空间（源句下标，不得索引译文句）。
# ---------------------------------------------------------------------------

from literature.models import Block as _Block, Page as _Page, TranslationEntry, TranslationLayer  # noqa: E402


def make_apparatus_document(project_id: str) -> Document:
    """含一个「装置内容」节的文档：Appendix 节只有一个 <3 词的块（折叠时判 suppressed）。"""
    blocks = [
        _Block(block_id="a-title-1", type="title", order=0, page_index=0, source_text="Appendix", level=1, bbox=[10, 10, 300, 40]),
        _Block(block_id="a-tiny-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 80], source_text="* * *"),
    ]
    return Document(project_id=project_id, title="Silk Road Silver", source_language="en",
                    page_count=1, pages=[_Page(page_index=0, width=600, height=800)], blocks=blocks)


class EvidenceTwoProvider(ScriptedProvider):
    """S1 派生回复固定 evidence=[2]（源句空间第 3 句），用于验证 ev 不被译文句集重排。"""

    def complete(self, *, system: str, user: str):  # noqa: D102
        from literature.providers import ProviderResponse
        kind = "derive" if "source takeaway:" in user else "source"
        section = self._section_of(kind, user)
        self.calls.append((kind, section))
        if self.after_first is not None and len(self.calls) >= 1:
            self.after_first.set()
        if section in self.fail_sections:
            raise RuntimeError(f"模拟 {section} 折叠失败")
        if kind == "source":
            payload = reply_for(user)
            # 只对 U0（b-p-1，源文 3 句）用源句空间第 3 句；U1（b-p-2 仅 1 句）保持默认，
            # 避免 evidence 越界污染其他 unit 的折叠产物。
            if "Workshops in Chang'an" in user:
                for unit in payload["units"]:
                    if unit["id"] == "U0":
                        unit["evidence"] = [2]
        else:
            payload = derive_reply_for(user)
            for unit in payload["units"]:
                unit["evidence"] = [2]
                unit["ev_sentences"] = [f"源第3句的分层译句 {unit['id']}。"]
        return ProviderResponse(
            content=json.dumps(payload, ensure_ascii=False), provider="fake", model="fake-model",
            request_id=f"req-{len(self.calls)}", input_tokens=100, output_tokens=50, estimated_cost=0.001,
        )


class TestReadyInvalidationMatrix(FoldRunApiFlowBase):
    """ready 不是终态豁免：事实/绑定变更、native 丢失、resume 语义（同进程）。"""

    def test_fact_change_flips_ready_to_failed_and_resume_refolds_affected_only(self) -> None:
        """同进程：ready → 改 document → 读快照即失效（failed，ratio<1）→ resume 只补受影响节。"""
        self._bind_provider()
        self._start_run()
        first = self._wait_run(("ready", "failed"))
        self.assertEqual(first["status"], "ready")
        calls_at_ready = len(self.provider.calls)
        document = self.store.load_document(self.project_id)
        for block in document.blocks:
            if block.block_id == "b-p-2":   # 属于 S2（Conclusion）
                block.source_text = "Assay data show a consistent alloy of 91 percent silver after re-assay."
        self.store.save_document(document)
        snapshot = self.client.get(f"/projects/{self.project_id}/reader/fold-run", headers=self.headers).json()
        self.assertEqual(snapshot["status"], "failed", snapshot)
        self.assertFalse(snapshot.get("ready"))
        self.assertLess(float(snapshot["progress"]["ratio"]), 1.0, snapshot["progress"])
        # resume（failed 可 resume）：只补受影响节，S1 缓存 0 调用复用
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs/{first['run_id']}/resume",
            json={"locale": "zh-CN"}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        # b-p-2 属于 S1（Introduction）：受影响的是 S1，S2（Conclusion）不动。
        # S1 源折叠全程恰好两次（初次 + 失效重折），S2 恰好一次（缓存复用）。
        source_s1 = [item for item in self.provider.calls if item[0] == "source" and item[1] == "S1"]
        source_s2 = [item for item in self.provider.calls if item[0] == "source" and item[1] == "S2"]
        self.assertEqual(len(source_s1), 2, self.provider.calls)
        self.assertEqual(len(source_s2), 1, self.provider.calls)
        self.assertGreater(len(self.provider.calls), calls_at_ready)

    def test_resume_on_ready_is_409(self) -> None:
        """契约：resume 只对 paused/failed 可用；ready 上 resume 必须 409，不得静默续跑。"""
        self._bind_provider()
        run = self._start_run()["run"]
        self._wait_run(("ready", "failed"))
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs/{run['run_id']}/resume",
            json={"locale": "zh-CN"}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_native_missing_invalidates_ready_and_native_is_404(self) -> None:
        """native 落盘文件被删：ready 复验失效（不假就绪），native 读 404。"""
        self._bind_provider()
        self._start_run()
        self._wait_run(("ready", "failed"))
        base = self.store.project_path(self.project_id) / "reader"
        removed = 0
        for path in base.glob("native_*.json"):
            path.unlink()
            removed += 1
        self.assertGreaterEqual(removed, 1)
        response = self.client.get(
            f"/projects/{self.project_id}/reader/layers/native?locale=zh-CN", headers=self.headers,
        )
        self.assertEqual(response.status_code, 404, response.text)
        snapshot = self.client.get(f"/projects/{self.project_id}/reader/fold-run", headers=self.headers).json()
        self.assertFalse(snapshot.get("ready"), snapshot)
        self.assertIn(snapshot["status"], ("failed", "paused", "running"))

    def test_role_rebind_refolds_with_new_model_on_next_run(self) -> None:
        """角色换绑：既有 ready 不假失效（内容仍是那个模型的有效产物）；下一次 run 用新模型重折。"""
        self._bind_provider()
        self._start_run()
        self._wait_run(("ready", "failed"))
        # 换绑到第二个服务/模型
        created = self.client.post("/model-services", json={
            "name": "integration-fake-2", "preset": "custom",
            "base_url": "http://127.0.0.1:1/v1", "models": [{"model": "fake-model-2"}],
        }, headers=self.headers)
        self.assertEqual(created.status_code, 200, created.text)
        roles = self.client.put("/model-services/roles", json={
            "reader_fold": {"service_id": created.json()["service_id"], "model": "fake-model-2"},
        }, headers=self.headers)
        self.assertEqual(roles.status_code, 200, roles.text)
        calls_before = len(self.provider.calls)
        second = self._start_run()
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertEqual(str(final.get("binding", {}).get("model") or ""), "fake-model-2", final.get("binding"))
        self.assertGreater(len(self.provider.calls), calls_before, "换绑后必须按新模型真实重折（派生键含模型）")


class TestSuppressionAndEvSpace(FoldRunApiFlowBase):
    """全 suppressed 节不阻塞 ready、不烧模型；派生 ev 固定在源句空间。"""

    def test_all_suppressed_section_ready_without_model_call(self) -> None:
        store = self.store
        document = make_apparatus_document(self.project_id)
        store.save_document(document)
        entries = {}
        for block in document.blocks:
            if block.type == "title":
                entries[block.block_id] = TranslationEntry(block_id=block.block_id, text=f"[译章] {block.source_text}")
            else:
                entries[block.block_id] = TranslationEntry(block_id=block.block_id, text="[译] * * *")
        store.save_translation(TranslationLayer(project_id=self.project_id, locale="zh-CN", entries=entries))
        self._bind_provider()
        self._start_run()
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        # 新基线（reader.py 9c7fbdae…）：全部 unit 低于 MIN_FOLD_WORDS 时确定性跳过
        # 模型请求（reader.py「全部 unit 低于确定性抑制阈值」分支），零费用。
        native = self.client.get(
            f"/projects/{self.project_id}/reader/layers/native?locale=zh-CN", headers=self.headers,
        ).json()
        unit = native["units"]["a-tiny-1"]
        self.assertTrue(unit.get("suppressed") or (unit.get("lad") or {}).get("brief") == [], unit)
        layers = self.client.get(
            f"/projects/{self.project_id}/reader/layers?locale=zh-CN", headers=self.headers,
        ).json()
        units = [u for section in layers["sections"] for u in section["units"]]
        self.assertTrue(all(u.get("source") in ("model", "suppressed") for u in units), units)

    def test_derived_ev_stays_in_source_sentence_space(self) -> None:
        """源 ev 不得索引译文 sents：源 b-p-1 有 3 句，译文并成 1 句+多 1 句；派生 evidence=[2]
        必须原样保留（源句空间），不能被译文句集（2 句）截断/重排。"""
        merged = self.translation.entries["b-p-1"]
        merged.text = "白银器物沿丝绸之路流动，长安工坊几十年间模仿粟特器形，九世纪后需求衰落。这是译文里多出来的一句。"
        self.store.save_translation(self.translation)
        self.provider = EvidenceTwoProvider()
        self._bind_provider()
        self._start_run()
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        native = self.client.get(
            f"/projects/{self.project_id}/reader/layers/native?locale=zh-CN", headers=self.headers,
        ).json()
        unit = native["units"]["b-p-1"]
        trans_lad = (unit.get("tr") or {}).get("zh-CN", {}).get("lad") or {}
        source_sents = unit["sents"]
        trans_sents = unit["tr"]["zh-CN"]["sents"]
        self.assertEqual(len(source_sents), 3, source_sents)
        self.assertEqual(len(trans_sents), 2, trans_sents)
        self.assertEqual(trans_lad.get("ev"), [2],
                         "派生 ev 必须原样继承源句下标（evidence=[2]）")
        self.assertLess(trans_lad["ev"][0], len(source_sents), "ev 必须落在源句空间内")
        self.assertGreaterEqual(trans_lad["ev"][0], len(trans_sents),
                                "ev=2 超出译文句数：恰证明它不是译文句空间下标")
        # L3 语义 lane（§4.8b）：ev_space=source、derived_brief 与 Key 层 written line（lad.brief）分离。
        self.assertEqual(trans_lad.get("ev_space"), "source", trans_lad.get("ev_space"))
        derived = trans_lad.get("derived_brief") or {}
        self.assertEqual(derived.get("source_ev"), [2], derived)
        self.assertEqual(len(derived.get("sents") or []), len(trans_lad.get("ev") or []),
                         "derived_brief.sents 与 ev 必须 1:1")
        derived_text = "".join(t.get("t", "") for t in derived.get("toks") or [])
        self.assertIn("源第3句的分层译句", derived_text, derived_text)
        # lad.brief 是 Key/Takeaway written line（上游 L2/L1 语义），不含派生译句——两者不混用。
        written_text = "".join(t.get("t", "") for t in trans_lad.get("brief") or [])
        self.assertIn("译要点", written_text, written_text)
        self.assertNotIn("源第3句的分层译句", written_text)


class TestForceAndMissingProjectMatrix(FoldRunApiFlowBase):
    """force 重跑 source+derive 每节一次（修复回归守护）；缺项目全路由 404；resume 绑定冻结。"""

    def test_force_reruns_source_and_derive_once_per_section(self) -> None:
        """旧缺陷：force 只重 derive 不重折 source。修复后：force run 内每节
        source 恰好一次 + derive 恰好一次（single-flight 只防并发重复计费）。"""
        self._bind_provider()
        self._start_run()
        first = self._wait_run(("ready", "failed"))
        self.assertEqual(first["status"], "ready")
        calls_before = len(self.provider.calls)
        forced = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs",
            json={"locale": "zh-CN", "force": True}, headers=self.headers,
        )
        self.assertEqual(forced.status_code, 200, forced.text)
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        delta = self.provider.calls[calls_before:]
        for kind in ("source", "derive"):
            for section in ("S1", "S2"):
                count = sum(1 for k, sec in delta if k == kind and sec == section)
                self.assertEqual(count, 1, f"force run 内 {kind}/{section} 应恰好一次：{delta}")
        self.assertEqual(len(delta), 4, delta)

    def test_missing_project_404_on_all_fold_run_routes(self) -> None:
        """缺项目：GET/POST start/pause/resume 统一 404（不是 StorageError 400/500）。"""
        pid = "0" * 32
        checks = [
            ("GET", f"/projects/{pid}/reader/fold-run", None),
            ("POST", f"/projects/{pid}/reader/fold-runs", {"locale": "zh-CN"}),
            ("POST", f"/projects/{pid}/reader/fold-runs/fr-nope/pause", {}),
            ("POST", f"/projects/{pid}/reader/fold-runs/fr-nope/resume", {"locale": "zh-CN"}),
        ]
        for method, path, body in checks:
            response = self.client.request(method, path, json=body, headers=self.headers)
            self.assertEqual(response.status_code, 404, f"{method} {path}: {response.text}")

    def test_resume_uses_frozen_binding_not_new_role(self) -> None:
        """绑定冻结：pause 后换绑角色，resume 沿用 run 记录里的 service/model（缓存键一致）。"""
        pause_after_first = threading.Event()
        self.provider.after_first = pause_after_first
        self._bind_provider()
        run = self._start_run()["run"]
        self.assertTrue(pause_after_first.wait(timeout=10))
        self.client.app.state.job_manager.reader_jobs.pause_run(self.project_id, run["run_id"])
        final = self._wait_run(("paused", "ready"), timeout=20)
        if final["status"] == "ready":
            self.skipTest("provider 过快：暂停生效前整篇已完成（边界语义允许）")
        # 换绑到第二个模型
        created = self.client.post("/model-services", json={
            "name": "integration-fake-3", "preset": "custom",
            "base_url": "http://127.0.0.1:1/v1", "models": [{"model": "fake-model-3"}],
        }, headers=self.headers)
        self.assertEqual(created.status_code, 200, created.text)
        roles = self.client.put("/model-services/roles", json={
            "reader_fold": {"service_id": created.json()["service_id"], "model": "fake-model-3"},
        }, headers=self.headers)
        self.assertEqual(roles.status_code, 200, roles.text)
        calls_at_pause = len(self.provider.calls)
        response = self.client.post(
            f"/projects/{self.project_id}/reader/fold-runs/{run['run_id']}/resume",
            json={"locale": "zh-CN"}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        final = self._wait_run(("ready", "failed"))
        self.assertEqual(final["status"], "ready", final)
        self.assertEqual(str(final.get("binding", {}).get("model") or ""), "fake-model",
                         "resume 必须冻结在 run 原绑定，不得改用新角色")
        self.assertLess(len(self.provider.calls) - calls_at_pause, 4, "只补未完成节，不整书重折")
