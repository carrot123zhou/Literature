"""后端安全与数据保护回归（2026-10-15 加固轮）。

覆盖 lead 指定的 P0/P1：locale 路径穿越、service_id 探测的密钥归属、明文 HTTP 携带
密钥、冻结服务身份恢复、请求异常归还并发名额、即时保存尊重人工译文、SSE 结束标记与
error 事件、模型服务并发写、活动作业下的项目重命名/更新、列表合并不自动应用。
全部测试在隔离临时库中运行（见 tests/conftest.py），不触碰真实库、不调用付费服务。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from literature.models import Block, Document
from literature.model_services import ModelServiceStore
from literature.providers import (
    MockProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    ProviderResponse,
    ProviderTimeoutError,
    _aggregate_sse_events,
    _SSEErrorEvent,
    _StreamInterrupted,
    assert_key_transport_safe,
    plaintext_http_blocked,
)
from literature.request_coordinator import RequestCoordinator
from literature.storage import ProjectStore, StorageError, safe_locale
from literature.structure import _prepare_patch, _proposal_merges_body
from literature.translation import TranslationService


def block(block_id: str, type: str, text: str = "", *, items: list[str] | None = None, level: int = 0) -> Block:
    return Block(block_id=block_id, type=type, order=0, page_index=0, source_text=text, items=items or [], level=level)


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


class LocalePathSafetyTests(unittest.TestCase):
    def test_safe_locale_rejects_traversal_and_accepts_bcp47(self):
        self.assertEqual(safe_locale("zh-CN"), "zh-CN")
        self.assertEqual(safe_locale("pt_BR"), "pt_BR")
        for bad in ("", "..", "../evil", "zh/../..", "/etc/passwd", "a" * 40, "zh CN"):
            with self.assertRaises(StorageError, msg=bad):
                safe_locale(bad)

    def test_translation_path_cannot_escape_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            with self.assertRaises(StorageError):
                store.translation_path(project_id, "../../other/translations")
            with self.assertRaises(StorageError):
                store.glossary_versions_path(project_id, "..")
            # 合法 locale 仍在项目目录内
            path = store.translation_path(project_id, "zh-CN")
            self.assertTrue(str(path).startswith(str(store.project_path(project_id))))

    def test_api_edit_with_traversal_locale_is_400(self):
        from fastapi.testclient import TestClient
        from literature.api import create_app

        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            client = TestClient(create_app(store))
            headers = {"x-literature-token": client.app.state.session_token}
            response = client.put(
                f"/projects/{project_id}/translations/b1", headers=headers,
                json={"text": "x", "locale": "../../evil"},
            )
            self.assertEqual(response.status_code, 400, response.text)
            client.close()

    def test_locale_dir_rejects_symlinks_pointing_outside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, project_id = make_project(root, [block("b1", "paragraph", "hello")])
            project = store.project_path(project_id)
            outside = root / "outside"
            outside.mkdir()
            # translations/<locale> 是字段链接 → 拒绝
            shutil.rmtree(project / "translations")
            (project / "translations").mkdir()
            (project / "translations" / "zh-CN").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(StorageError):
                store.translation_path(project_id, "zh-CN")
            # translations 整个目录是字段链接 → 拒绝
            shutil.rmtree(project / "translations")
            (project / "translations").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(StorageError):
                store.translation_path(project_id, "zh-CN")


class ModelProbeKeyTests(unittest.TestCase):
    def make_service(self, root: Path) -> tuple[ModelServiceStore, str]:
        store = ModelServiceStore(root / "runtime")
        service = store.create({
            "name": "Saved", "preset": "custom", "base_url": "https://saved.example/v1",
            "models": [{"model": "m"}],
        })
        return store, service["service_id"]

    def test_service_id_never_sends_stored_key_to_caller_base_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, service_id = self.make_service(Path(tmp))
            calls: list[tuple[str, str]] = []

            def fake(base: str, key: str, *, timeout: float = 30.0, allow_plaintext_http: bool = False):
                calls.append((base, key))
                return []

            with patch("literature.providers.list_remote_models", side_effect=fake), \
                 patch.object(ModelServiceStore, "api_key", return_value="SECRET"):
                # 地址不一致又没给调用方 key → 直接拒绝，且不发出任何请求。
                with self.assertRaises(ProviderError):
                    store.probe_models(service_id=service_id, base_url="http://evil.example/v1")
                self.assertEqual(calls, [])
                # 调用方自带 key → 用调用方地址与 key，绝不用已存密钥。
                store.probe_models(service_id=service_id, base_url="http://evil.example/v1", api_key="CALLER")
                self.assertEqual(calls[-1], ("http://evil.example/v1", "CALLER"))
                # 地址一致 → 复用已保存地址与密钥。
                store.probe_models(service_id=service_id, base_url="https://saved.example/v1")
                self.assertEqual(calls[-1], ("https://saved.example/v1", "SECRET"))

    def test_probe_models_forwards_saved_plaintext_optin(self):
        """服务上确认过「允许明文 HTTP」后，拉模型列表才能向 http 远程地址发密钥。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ModelServiceStore(Path(tmp) / "runtime")
            service = store.create({
                "name": "Intranet", "preset": "custom", "base_url": "http://maas.intranet.example/v1",
                "allow_plaintext_http": True, "models": [{"model": "m"}],
            })
            self.assertTrue(service["allow_plaintext_http"])
            seen: list[dict] = []

            def fake(base: str, key: str, *, timeout: float = 30.0, allow_plaintext_http: bool = False):
                seen.append({"base": base, "allow": allow_plaintext_http})
                return []

            with patch("literature.providers.list_remote_models", side_effect=fake), \
                 patch.object(ModelServiceStore, "api_key", return_value="SECRET"):
                store.probe_models(service_id=service["service_id"])
                self.assertEqual(seen[-1], {"base": "http://maas.intranet.example/v1", "allow": True})

    def test_plaintext_optin_defaults_to_false_for_old_configs(self):
        """旧配置文件没有该字段：必须按 False 读，不能因为缺字段而意外放行。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runtime" / "model-services.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "version": 2,
                "services": [{"service_id": "svc", "name": "old", "preset": "custom",
                              "base_url": "http://maas.intranet.example/v1", "models": [{"model": "m"}]}],
                "roles": {},
            }), encoding="utf-8")
            store = ModelServiceStore(Path(tmp) / "runtime")
            self.assertFalse(store.get("svc")["allow_plaintext_http"])
            with patch.object(ModelServiceStore, "api_key", return_value="SECRET"):
                provider = store.config_for("svc", "m")
            self.assertFalse(provider.config.allow_plaintext_http)


class KeyTransportTests(unittest.TestCase):
    def test_plaintext_http_blocked_for_remote_only(self):
        self.assertTrue(plaintext_http_blocked("http://evil.example/v1"))
        self.assertFalse(plaintext_http_blocked("http://127.0.0.1:8000/v1"))
        self.assertFalse(plaintext_http_blocked("http://localhost:8000/v1"))
        self.assertFalse(plaintext_http_blocked("https://evil.example/v1"))
        self.assertFalse(plaintext_http_blocked("mock://local"))
        # 前缀 127. 的公网域名不得被当作本机
        self.assertTrue(plaintext_http_blocked("http://127.attacker.example/v1"))
        # 0.0.0.0 不是 loopback（通配地址），保守拦截
        self.assertTrue(plaintext_http_blocked("http://0.0.0.0:8000/v1"))
        self.assertFalse(plaintext_http_blocked("http://[::1]:8000/v1"))

    def test_assert_key_transport_safe(self):
        with self.assertRaises(ProviderError):
            assert_key_transport_safe("http://evil.example/v1", "k")
        assert_key_transport_safe("http://127.0.0.1:9000/v1", "k")
        assert_key_transport_safe("https://evil.example/v1", "k")
        # 无密钥时明文地址不拦截（无凭据可泄漏）
        assert_key_transport_safe("http://evil.example/v1", "")

    def test_plaintext_http_allowed_only_with_explicit_optin(self):
        """内网/自建服务的 http 地址：默认拦截，用户显式确认后放行。"""
        self.assertTrue(plaintext_http_blocked("http://maas.intranet.example/v1"))
        self.assertFalse(plaintext_http_blocked("http://maas.intranet.example/v1", allow_plaintext=True))
        # 放行只影响明文判断；https 与 loopback 行为不变。
        self.assertFalse(plaintext_http_blocked("https://evil.example/v1", allow_plaintext=True))
        self.assertFalse(plaintext_http_blocked("http://127.0.0.1:8000/v1", allow_plaintext=True))
        with self.assertRaises(ProviderError):
            assert_key_transport_safe("http://maas.intranet.example/v1", "k")
        assert_key_transport_safe("http://maas.intranet.example/v1", "k", allow_plaintext=True)
        # 没有密钥时连“放行”也不需要。
        assert_key_transport_safe("http://maas.intranet.example/v1", "", allow_plaintext=True)

    def test_plaintext_provider_completes_only_with_optin(self):
        """完整请求路径：默认拦在发请求前，显式确认后才真的带着密钥发出。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "model": "m",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            })

        blocked = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="m", base_url="http://maas.intranet.example/v1"),
            api_key="k", timeout=5.0,
        )
        blocked._client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError) as raised:
            blocked.complete(system="s", user="u")
        self.assertIn("明文 HTTP", str(raised.exception))

        allowed = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="m", base_url="http://maas.intranet.example/v1",
                           allow_plaintext_http=True),
            api_key="k", timeout=5.0,
        )
        allowed._client = httpx.Client(transport=httpx.MockTransport(handler))
        self.assertEqual(allowed.complete(system="s", user="u").content, "ok")


class FrozenServiceIdentityTests(unittest.TestCase):
    def test_resume_refuses_key_with_old_base_url_after_service_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            provider = MockProvider()
            provider.config = ProviderConfig.preset("custom", model="m", base_url="https://a.example/v1")
            service = TranslationService(store, provider, service_id="svc-A")
            # 服务身份不同 → 拒绝。
            with self.assertRaises(ProviderError):
                service._apply_frozen_model({"service_id": "svc-B", "base_url": "https://a.example/v1", "model": "m"})
            # 同一服务身份 + 地址一致 → 按冻结值恢复。
            service._apply_frozen_model({"service_id": "svc-A", "base_url": "https://a.example/v1", "model": "m2"})
            self.assertEqual(provider.config.model, "m2")
            # 同一服务身份但地址改动 → 也必须拒绝（不能只比较身份）。
            with self.assertRaises(ProviderError):
                service._apply_frozen_model({"service_id": "svc-A", "base_url": "https://b.example/v1", "model": "m3"})

    def test_legacy_frozen_without_identity_rejects_mismatched_base_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            provider = MockProvider()
            provider.config = ProviderConfig.preset("custom", model="m", base_url="https://a.example/v1")
            service = TranslationService(store, provider)
            with self.assertRaises(ProviderError):
                service._apply_frozen_model({"base_url": "https://other.example/v1"})
            # 地址一致时旧快照仍可恢复。
            service._apply_frozen_model({"base_url": "https://a.example/v1", "model": "m3"})
            self.assertEqual(provider.config.model, "m3")

    def test_freeze_manifest_records_service_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            service = TranslationService(store, MockProvider(), service_id="svc-A")
            document = store.load_document(project_id)
            layer = store.load_translation(project_id)
            frozen = service._freeze_manifest(document, layer, ["b1"], "zh-CN", False)
            self.assertEqual(frozen["service_id"], "svc-A")


class RequestSlotReleaseTests(unittest.TestCase):
    def test_unexpected_exception_releases_service_slot(self):
        coordinator = RequestCoordinator(default_service_limit=1, acquire_timeout_seconds=0.5)
        provider = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="m", base_url="https://example.invalid/v1"), api_key="k",
        )
        provider._stream_disabled = True

        class BoomClient:
            def post(self, *args, **kwargs):
                raise RuntimeError("boom")

        provider._client_instance = lambda httpx_module: BoomClient()
        coordinator.attempt_hooks_for("svc", provider)
        with self.assertRaises(RuntimeError):
            provider.complete(system="s", user="u")
        # 名额必须已经归还：再次获取不应超时。
        semaphore = coordinator.acquire_service("svc", timeout=0.5)
        coordinator.release_service(semaphore)

    def test_budget_failure_still_releases_service_slot(self):
        coordinator = RequestCoordinator(default_service_limit=1, acquire_timeout_seconds=0.5)
        provider = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="m", base_url="https://example.invalid/v1"), api_key="k",
        )
        coordinator.attempt_hooks_for("svc", provider, project_id="p", budget=0.000001,
                                      cost_of=lambda chars: 1.0)
        with self.assertRaises(Exception):
            provider.attempt_hooks[0](10)
        semaphore = coordinator.acquire_service("svc", timeout=0.5)
        coordinator.release_service(semaphore)

    def test_mid_stream_unexpected_exception_keeps_reservation_as_charged(self):
        coordinator = RequestCoordinator(default_service_limit=1)
        provider = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="m", base_url="https://example.invalid/v1"), api_key="k",
        )

        class FakeResponse:
            status_code = 200
            headers = {"content-type": "text/event-stream"}

            def iter_lines(self):
                yield 'data: {"choices":[{"delta":{"content":"x"}}]}'
                raise ValueError("unexpected mid-stream")

            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeClient:
            def stream(self, *args, **kwargs):
                return FakeResponse()

        provider._client_instance = lambda httpx_module: FakeClient()
        coordinator.attempt_hooks_for("svc", provider, project_id="p", budget=1000.0, cost_of=lambda chars: 0.25)
        with self.assertRaises(ValueError):
            provider.complete(system="s", user="u")
        # 已读到数据后遇到未预期异常：预留必须保留（交调用方按未知费用对账），而不是当零成本释放。
        self.assertAlmostEqual(coordinator.reserved("p"), 0.25)
        handle = provider._attempt_state.handle
        self.assertIsNotNone(handle)
        coordinator.release(handle)
        self.assertEqual(coordinator.reserved("p"), 0.0)


class InstantSaveSafetyTests(unittest.TestCase):
    def test_instant_save_refuses_locked_manual_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello world")])
            service = TranslationService(store, MockProvider())
            service.edit(project_id, "b1", "人工译文")
            with self.assertRaises(StorageError):
                service.instant(project_id, "hello world", block_id="b1", save=True)
            entry = store.load_translation(project_id).entries["b1"]
            self.assertEqual(entry.text, "人工译文")
            self.assertTrue(entry.locked)

    def test_instant_save_rejects_empty_content(self):
        class EmptyProvider(MockProvider):
            def complete(self, *, system: str, user: str) -> ProviderResponse:
                return ProviderResponse(content="", provider="mock", model="mock")

        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            service = TranslationService(store, EmptyProvider())
            with self.assertRaises(ProviderError):
                service.instant(project_id, "hello", block_id="b1", save=True)
            self.assertNotIn("b1", store.load_translation(project_id).entries)

    def test_instant_save_preserves_previous_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            service = TranslationService(store, MockProvider())
            service.instant(project_id, "hello", block_id="b1", save=True)
            first = store.load_translation(project_id).entries["b1"]
            service.instant(project_id, "hello", block_id="b1", save=True)
            second = store.load_translation(project_id).entries["b1"]
            self.assertGreaterEqual(len(second.revisions), 1)
            self.assertEqual(second.revisions[-1]["text"], first.text)


class SseStreamTests(unittest.TestCase):
    def test_stream_without_terminal_signal_is_incomplete(self):
        body = 'data: ' + json.dumps({"choices": [{"delta": {"content": "hi"}}]})
        value = _aggregate_sse_events(body.splitlines())
        self.assertEqual(value["choices"][0]["finish_reason"], "incomplete_stream")

    def test_stream_without_terminal_marker_is_incomplete_even_with_usage(self):
        done_body = (
            'data: ' + json.dumps({"choices": [{"delta": {"content": "hi"}}]}) + "\n\n"
            "data: [DONE]\n\n"
        )
        self.assertEqual(_aggregate_sse_events(done_body.splitlines())["choices"][0]["finish_reason"], "")
        # usage 不等于正常终止：缺 [DONE] / finish_reason 仍要报不完整，
        # 否则即时翻译会把截断输出当完整结果保存。
        usage_body = 'data: ' + json.dumps({
            "choices": [{"delta": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })
        self.assertEqual(
            _aggregate_sse_events(usage_body.splitlines())["choices"][0]["finish_reason"], "incomplete_stream",
        )

    def test_sse_error_event_raises_with_mapped_status(self):
        body = 'data: ' + json.dumps({"error": {"message": "rate limit exceeded", "type": "rate_limit_error"}})
        with self.assertRaises(_SSEErrorEvent) as context:
            _aggregate_sse_events(body.splitlines())
        self.assertEqual(context.exception.status_code, 429)

    def test_mid_stream_break_after_data_is_interrupted(self):
        def lines():
            yield 'data: {"choices":[{"delta":{"content":"a"}}]}'
            raise httpx.ReadError("boom")

        with self.assertRaises(_StreamInterrupted):
            OpenAICompatibleProvider._aggregate_stream_lines(lines())

    def test_break_before_any_data_keeps_original_error(self):
        def lines():
            raise httpx.ReadError("boom")
            yield ""  # pragma: no cover

        with self.assertRaises(httpx.ReadError):
            OpenAICompatibleProvider._aggregate_stream_lines(lines())


class ModelServiceConcurrencyTests(unittest.TestCase):
    def test_concurrent_create_does_not_lose_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ModelServiceStore(Path(tmp) / "runtime")
            errors: list[BaseException] = []

            def create(index: int) -> None:
                try:
                    store.create({
                        "name": f"svc-{index}", "preset": "custom",
                        "base_url": f"https://host{index}.example/v1", "models": [{"model": "m"}],
                    })
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            threads = [threading.Thread(target=create, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(store.list()), 8)


class ProjectRenameGuardTests(unittest.TestCase):
    def test_rename_and_update_rejected_while_job_active(self):
        from fastapi.testclient import TestClient
        from literature.api import create_app

        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            client = TestClient(create_app(store))
            headers = {"x-literature-token": client.app.state.session_token}
            job_id = store.create_job(project_id, "translate", {}, {})
            response = client.patch(f"/projects/{project_id}", headers=headers, json={"title": "Renamed"})
            self.assertEqual(response.status_code, 409, response.text)
            response = client.patch(f"/projects/{project_id}", headers=headers, json={"source_language": "en"})
            self.assertEqual(response.status_code, 409, response.text)
            store.update_job(project_id, job_id, status="completed")
            response = client.patch(f"/projects/{project_id}", headers=headers, json={"title": "Renamed"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["title"], "Renamed")
            client.close()


class AutoApplyMergeGuardTests(unittest.TestCase):
    def test_proposal_that_merges_list_items_is_not_auto_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("l1", "list", items=["One", "Two"])])
            document = store.load_document(project_id)
            proposal = {"block_id": "l1", "operation": "set_title", "after": 2, "confidence": 0.99}
            self.assertTrue(_proposal_merges_body(document, proposal))
            with self.assertRaises(StorageError):
                _prepare_patch(document, proposal, "ai", automatic_policy="all")
            # 单列表项不构成合并。
            document.block("l1").items = ["Only"]
            self.assertFalse(_proposal_merges_body(document, proposal))
            # set_level 不涉及合并。
            document.block("l1").type = "title"
            self.assertFalse(_proposal_merges_body(document, {"block_id": "l1", "operation": "set_level"}))


class ErrorRedactionTests(unittest.TestCase):
    def test_error_text_masks_secrets_and_truncates(self):
        from literature.providers import redact_error_text
        masked = redact_error_text("bad key sk-abcdef123456 and Bearer abcdefghijklmn")
        self.assertNotIn("sk-abcdef123456", masked)
        self.assertNotIn("Bearer abcdefghijklmn", masked)
        long = redact_error_text("word " * 200)
        self.assertTrue(long.endswith("…(已截断)"))
        self.assertLessEqual(len(long), 300 + len("…(已截断)"))


class ProbeAccountingTests(unittest.TestCase):
    def test_probe_reports_attempt_usage(self):
        from literature.providers import probe_thinking_off

        class Stub:
            attempt_hooks = None
            _drop_reasoning_fields = False
            _stream_disabled = True  # 跳过前置流式检查，只走候选探测
            config = ProviderConfig.preset("custom", model="qwen3-max", base_url="https://x.example/v1")

            def with_reasoning(self, *, style: str = "", effort: str = "", variant: str = ""):
                return self

            def with_max_output_tokens(self, value: int):
                return self

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                return ProviderResponse(content="hi", provider="stub", model="qwen3-max",
                                        input_tokens=10, output_tokens=2, reasoning_tokens=0)

        seen: list[tuple[object, object]] = []
        result = probe_thinking_off(Stub(), on_attempt=lambda response, error: seen.append((response, error)))
        self.assertIsNotNone(result)
        self.assertTrue(seen)
        self.assertIsNotNone(seen[0][0])

    def test_probe_closes_child_even_when_callback_raises(self):
        from literature.providers import probe_thinking_off

        closed: list[bool] = []

        class ChildClient:
            def close(self):
                closed.append(True)

        class Child:
            attempt_hooks = None
            _drop_reasoning_fields = False
            _stream_disabled = True  # 跳过前置流式检查，直接走候选
            timeout = 5.0
            config = ProviderConfig.preset("custom", model="qwen3-max", base_url="https://x.example/v1")

            def __init__(self):
                self._client = ChildClient()

            def with_reasoning(self, **kwargs):
                return Child()

            def with_max_output_tokens(self, value):
                return self

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                return ProviderResponse(content="hi", provider="p", model="m", input_tokens=1, output_tokens=1)

        def boom(response, error):
            raise RuntimeError("callback boom")

        parent = Child()
        parent._client = None
        # 回调抛异常不能漏关子 provider；异常被吞并记录，探测继续。
        result = probe_thinking_off(parent, on_attempt=boom)
        self.assertTrue(closed)
        self.assertIsNotNone(result)

    def test_record_probe_outcome_unknown_cost_semantics(self):
        from literature.api import create_app

        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            app = create_app(store)
            manager = app.state.job_manager
            job_id = store.create_job(project_id, "translate", {}, {})
            # 1) 有 usage → 写真实用量
            manager.record_probe_outcome(
                project_id, job_id,
                ProviderResponse(content="x", provider="p", model="m", input_tokens=10, output_tokens=2, estimated_cost=0.001),
                None,
            )
            self.assertGreater(float(store.usage_summary(project_id)["estimated_cost"] or 0), 0)
            mark = store.pending_cost_records(project_id)
            # 2) 成功但无 usage → 必须写未知
            manager.record_probe_outcome(
                project_id, job_id, ProviderResponse(content="x", provider="p", model="m"), None,
            )
            self.assertEqual(store.pending_cost_records(project_id), mark + 1)
            # 3) 读超时 / 已开始的流 → 必须写未知
            manager.record_probe_outcome(project_id, job_id, None, ProviderTimeoutError("read", 180.0))
            manager.record_probe_outcome(project_id, job_id, None, _StreamInterrupted("断流"))
            # 4) status=0 的未知原因 → 保守按可能计费（写未知）
            manager.record_probe_outcome(project_id, job_id, None, ProviderError("boom"))
            self.assertEqual(store.pending_cost_records(project_id), mark + 4)
            # 5) 确定不计费：4xx、连接超时、预算/并发未放行 → 不写未知
            mark = store.pending_cost_records(project_id)
            manager.record_probe_outcome(project_id, job_id, None, ProviderError("bad request", status_code=400))
            manager.record_probe_outcome(project_id, job_id, None, ProviderTimeoutError("connect", 20.0))
            manager.record_probe_outcome(project_id, job_id, None, ProviderError("连接失败", possible_charge=False))
            import literature.request_coordinator as rc
            manager.record_probe_outcome(project_id, job_id, None, rc.BudgetExhausted("no budget"))
            manager.record_probe_outcome(project_id, job_id, None, rc.ServiceBusyError("busy"))
            self.assertEqual(store.pending_cost_records(project_id), mark)

    def test_probe_respects_per_attempt_budget_when_prices_unknown(self):
        """价格未知时不能用 0 放行：保守占额超过预算 → 不发出探测请求、不写缓存。"""
        import httpx
        from literature.api import create_app

        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            app = create_app(store)
            manager = app.state.job_manager
            store.update_manifest(project_id, budget_usd=0.0001)
            calls = {"count": 0}

            def handler(request: httpx.Request) -> httpx.Response:
                calls["count"] += 1
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: [DONE]\n\n")

            provider = OpenAICompatibleProvider(
                ProviderConfig(provider="custom", model="qwen3-max", base_url="https://relay.example.com/v1"),
                api_key="k", timeout=5.0,
            )
            provider._client = httpx.Client(transport=httpx.MockTransport(handler))
            manager.apply_thinking_policy(provider, "svc", allow_probe=True, project_id=project_id, budget=0.0001)
            self.assertEqual(calls["count"], 0)
            self.assertIsNone(manager.models.thinking_policy("svc", "qwen3-max"))

    def test_probe_uses_shared_coordinator_and_reconciles(self):
        """探测经共享协调层逐 attempt 预留：完成后 reserved 归零，有 usage 记入账本。"""
        import json as _json
        import httpx
        from literature.api import create_app

        def sse(events: list[dict]) -> str:
            return "\n\n".join("data: " + _json.dumps(event) for event in events) + "\n\ndata: [DONE]\n\n"

        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            app = create_app(store)
            manager = app.state.job_manager
            store.update_manifest(project_id, budget_usd=100.0)

            def handler(request: httpx.Request) -> httpx.Response:
                body = _json.loads(request.content.decode("utf-8"))
                if "enable_thinking" in body:
                    return httpx.Response(400, text="unknown field enable_thinking")
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      text=sse([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                                                 "usage": {"prompt_tokens": 5, "completion_tokens": 1}}]))

            provider = OpenAICompatibleProvider(
                ProviderConfig(provider="custom", model="qwen3-max", base_url="https://relay.example.com/v1",
                               input_price_per_million=1.0, output_price_per_million=2.0),
                api_key="k", timeout=5.0,
            )
            provider._client = httpx.Client(transport=httpx.MockTransport(handler))
            provider.max_retries = 0
            provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
            manager.apply_thinking_policy(
                provider, "svc", allow_probe=True, project_id=project_id, budget=100.0,
                on_probe_usage=lambda response, error: manager.record_probe_outcome(project_id, "", response, error),
            )
            # 预留全部对账完毕，不泄漏
            self.assertEqual(manager.coordinator.snapshot(project_id)["reserved_cost"], 0.0)
            # 探测的真实费用已进账本（真实路径会把 on_probe_usage 接到 record_probe_outcome）
            self.assertTrue(any(item.get("scope") == "probe" for item in store.usage_records(project_id)))

    def test_probe_stops_after_unknown_cost_under_budget(self):
        """有限预算下遇到一次不可量化费用后停止后续探测、不写策略缓存；跨调用也先查持久 unknown。"""
        import json as _json
        import httpx
        from literature.api import create_app

        def sse(events: list[dict]) -> str:
            return "\n\n".join("data: " + _json.dumps(event) for event in events) + "\n\ndata: [DONE]\n\n"

        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            app = create_app(store)
            manager = app.state.job_manager
            store.update_manifest(project_id, budget_usd=100.0)
            calls = {"count": 0}

            def handler(request: httpx.Request) -> httpx.Response:
                calls["count"] += 1
                # 有 usage 但 ProviderConfig 未配单价 → estimated_cost=None（未知费用）
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      text=sse([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                                                 "usage": {"prompt_tokens": 5, "completion_tokens": 1}}]))

            provider = OpenAICompatibleProvider(
                ProviderConfig(provider="custom", model="qwen3-max", base_url="https://relay.example.com/v1"),
                api_key="k", timeout=5.0,
            )
            provider._client = httpx.Client(transport=httpx.MockTransport(handler))
            provider.max_retries = 0
            provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
            manager.apply_thinking_policy(
                provider, "svc", allow_probe=True, project_id=project_id, budget=100.0,
                on_probe_usage=lambda response, error: manager.record_probe_outcome(project_id, "", response, error),
            )
            # 前置 settle 请求即产生未知费用 → 不再发候选请求。
            self.assertEqual(calls["count"], 1)
            self.assertIsNone(manager.models.thinking_policy("svc", "qwen3-max"))
            self.assertGreaterEqual(store.pending_cost_records(project_id) + store.usage_summary(project_id)["unknown_cost_records"], 1)
            # 跨调用：已有持久 unknown 时，下一次探测直接跳过（不再发任何请求）。
            before = calls["count"]
            manager.apply_thinking_policy(
                provider, "svc2", allow_probe=True, project_id=project_id, budget=100.0,
                on_probe_usage=lambda response, error: manager.record_probe_outcome(project_id, "", response, error),
            )
            self.assertEqual(calls["count"], before)

    @staticmethod
    def _wiring_handler():
        """MockTransport 处理器：探测请求不走网络。"""
        import json as _json
        import httpx

        def sse(events: list[dict]) -> str:
            return "\n\n".join("data: " + _json.dumps(event) for event in events) + "\n\ndata: [DONE]\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            body = _json.loads(request.content.decode("utf-8"))
            if "enable_thinking" in body:
                return httpx.Response(400, text="unknown field enable_thinking")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=sse([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                                             "usage": {"prompt_tokens": 5, "completion_tokens": 1}}]))
        return handler

    @staticmethod
    def _mock_transport_patch(handler):
        """在真实 client 创建边界（httpx.Client）注入 MockTransport。

        `with_read_timeout` 是生产行为，会 copy provider 并把 `_client` 置 None；因此不能
        预先给 provider 塞 `_client`（克隆后丢失），而要在 `_client_instance` 真正建 client
        的那一步拦：这样克隆出的 provider 仍然拿到 MockTransport，任何请求都不会走网络。
        """
        import httpx
        from unittest.mock import patch

        real_client = httpx.Client

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)
        return patch("httpx.Client", new=factory)

    def _wiring_provider(self):
        # 不预置 _client：交给 _mock_transport_patch 在真实创建 client 时注入。
        provider = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="qwen3-max", base_url="https://relay.example.com/v1",
                           input_price_per_million=1.0, output_price_per_million=2.0),
            api_key="k", timeout=5.0,
        )
        provider.max_retries = 0
        provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
        return provider

    def test_start_structure_wires_probe_usage_recording(self):
        """真实启动路径 start_structure 会把探测用量接到账本（不是只测最小单测）。"""
        from unittest.mock import patch
        from literature.api import StructureJobInput, create_app

        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            app = create_app(store)
            manager = app.state.job_manager
            store.update_manifest(project_id, budget_usd=100.0)
            service = manager.models.create({
                "name": "svc", "preset": "custom", "base_url": "https://relay.example.com/v1",
                "models": [{"model": "qwen3-max"}],
            })
            provider = self._wiring_provider()
            with self._mock_transport_patch(self._wiring_handler()), \
                 patch.object(manager, "provider_from", return_value=provider), \
                 patch("literature.api.run_structure_job", lambda *args, **kwargs: None):
                job_id = manager.start_structure(project_id, StructureJobInput(
                    service_id=service["service_id"], mode="coarse", model="qwen3-max",
                ))
                # mock 作用域必须覆盖后台任务全生命周期：用线程池 shutdown(wait=True)，不靠 sleep 猜时间。
                manager.executor.shutdown(wait=True)
                job = store.get_job(project_id, job_id)
                self.assertFalse(job.get("error"), job.get("error"))
            self.assertNotEqual(store.get_job(project_id, job_id).get("status"), "failed")
            self.assertTrue(any(item.get("scope") == "probe" for item in store.usage_records(project_id)))

    def test_start_translation_wires_probe_usage_recording(self):
        from unittest.mock import patch
        from literature.api import TranslationJobInput, create_app

        with tempfile.TemporaryDirectory() as tmp:
            store, project_id = make_project(Path(tmp), [block("b1", "paragraph", "hello")])
            app = create_app(store)
            manager = app.state.job_manager
            store.update_manifest(project_id, budget_usd=100.0)
            service = manager.models.create({
                "name": "svc", "preset": "custom", "base_url": "https://relay.example.com/v1",
                "models": [{"model": "qwen3-max"}],
            })
            provider = self._wiring_provider()
            with self._mock_transport_patch(self._wiring_handler()), \
                 patch.object(manager, "provider_from", return_value=provider), \
                 patch("literature.translation.TranslationService.run", lambda self, *args, **kwargs: None):
                job_id = manager.start_translation(project_id, TranslationJobInput(
                    service_id=service["service_id"], model="qwen3-max", locale="zh-CN", parallel_workers=1,
                ))
                # `with_read_timeout` 会把 provider 克隆并清空 _client；mock 建在 httpx.Client 边界，
                # 且作用域覆盖到后台线程真正完工，因此克隆后的 provider 也不会访问真实网络。
                manager.executor.shutdown(wait=True)
                job = store.get_job(project_id, job_id)
                self.assertFalse(job.get("error"), job.get("error"))
            self.assertNotEqual(store.get_job(project_id, job_id).get("status"), "failed")
            self.assertTrue(any(item.get("scope") == "probe" for item in store.usage_records(project_id)))

    def test_probe_does_not_cache_when_candidate_cost_unknown(self):
        """前置 attempt 已计量，但某候选响应不可量化时，不得把变体写进缓存。"""
        from literature.api import probe_cost_is_unknown
        from literature.providers import probe_thinking_off

        class Stub:
            attempt_hooks = None
            _drop_reasoning_fields = False
            _stream_disabled = False
            timeout = 5.0
            config = ProviderConfig.preset("custom", model="qwen3-max", base_url="https://x.example/v1")

            def __init__(self):
                self.calls = 0
                self._client = None

            def with_reasoning(self, **kwargs):
                return self

            def with_max_output_tokens(self, value):
                return self

            def complete(self, *, system: str, user: str) -> ProviderResponse:
                self.calls += 1
                if self.calls == 1:
                    # 前置 settle 请求：费用已知（有单价且 simulated cost=0.0）
                    return ProviderResponse(content="ok", provider="p", model="m",
                                            input_tokens=5, output_tokens=5, estimated_cost=0.0, finish_reason="stop")
                # 候选响应：有 usage 但无单价（estimated_cost=None）→ 不可量化；
                # 若不重查 should_continue，会被当作“成功变体”写进缓存。
                return ProviderResponse(content="ok", provider="p", model="m",
                                        input_tokens=5, output_tokens=5, reasoning_tokens=0,
                                        estimated_cost=None, finish_reason="stop")

        state = {"stop": False}

        def on_attempt(response, error):
            if probe_cost_is_unknown(response, error):
                state["stop"] = True

        provider = Stub()
        result = probe_thinking_off(
            provider, on_attempt=on_attempt, should_continue=lambda: not state["stop"],
        )
        self.assertTrue(state["stop"])
        self.assertIsNone(result)


class TestEnvironmentIsolationTests(unittest.TestCase):
    def test_library_and_runtime_point_at_temporary_dirs(self):
        import os
        library = os.environ.get("LITERATURE_LIBRARY", "")
        runtime = os.environ.get("LITERATURE_RUNTIME", "")
        self.assertTrue(library and runtime)
        real = Path.home() / "CdoexProject" / "Literature" / "library"
        self.assertNotEqual(Path(library), real)
        self.assertIn("literature-tests-", library)


if __name__ == "__main__":
    unittest.main()
