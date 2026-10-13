from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from literature.models import Block, Document
from literature.providers import (
    MockProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderResponse,
    ProviderTimeoutError,
)
from literature.storage import ProjectStore
from literature.translation import TranslationService


# ---- SSE 流式传输（providers） --------------------------------------------- #


def sse_body(events: list[dict]) -> str:
    lines = [f"data: {json.dumps(event)}" for event in events]
    return "\n\n".join(lines) + "\n\ndata: [DONE]\n\n"


def make_stream_provider(handler) -> tuple[OpenAICompatibleProvider, list[dict]]:
    requests: list[dict] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return handler(request)

    provider = OpenAICompatibleProvider(
        ProviderConfig(provider="custom", model="m", base_url="https://example.invalid/v1"),
        api_key="test-key", timeout=5.0, max_retries=2,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(recording_handler))
    provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
    return provider, requests


class StreamingProviderTests(unittest.TestCase):
    def test_sse_events_are_aggregated_with_usage_and_finish_reason(self):
        events = [
            {"choices": [{"delta": {"content": "你好"}}]},
            # 思考增量只参与保活，不计入译文内容。
            {"choices": [{"delta": {"reasoning_content": "思考过程"}}]},
            {"choices": [{"delta": {"content": "，世界"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                       "completion_tokens_details": {"reasoning_tokens": 3}},
             "model": "m", "id": "chatcmpl-x"},
        ]
        provider, requests = make_stream_provider(
            lambda request: httpx.Response(200, text=sse_body(events), headers={"content-type": "text/event-stream"})
        )
        response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "你好，世界")
        self.assertEqual(response.finish_reason, "stop")
        self.assertEqual(response.input_tokens, 11)
        self.assertEqual(response.output_tokens, 7)
        self.assertEqual(response.reasoning_tokens, 3)
        self.assertEqual(response.request_id, "chatcmpl-x")
        self.assertTrue(requests[0]["stream"])
        self.assertEqual(requests[0]["stream_options"], {"include_usage": True})

    def test_stream_rejected_with_400_falls_back_to_non_streaming(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("stream"):
                return httpx.Response(400, text="stream is not supported")
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })

        provider, requests = make_stream_provider(handler)
        response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "ok")
        self.assertTrue(provider._stream_disabled)
        self.assertFalse(requests[-1].get("stream", False))

    def test_relay_ignoring_stream_returns_full_json(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "relay-json"}, "finish_reason": "stop"}],
                "usage": {},
            })

        provider, _ = make_stream_provider(handler)
        response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "relay-json")

    def test_non_stream_2xx_invalid_json_raises_provider_error(self):
        """E 回归：200+门户 HTML 必须归成 ProviderError，否则连续失败熔断失效。"""
        from literature.providers import ProviderError
        provider, _ = make_stream_provider(
            lambda request: httpx.Response(200, text="<html>portal page</html>", headers={"content-type": "text/html"})
        )
        provider._stream_disabled = True
        with self.assertRaises(ProviderError) as raised:
            provider.complete(system="s", user="u")
        self.assertIn("模型请求失败", str(raised.exception))

    def test_relay_branch_invalid_json_raises_provider_error(self):
        from literature.providers import ProviderError
        provider, _ = make_stream_provider(
            lambda request: httpx.Response(200, text="not json at all", headers={"content-type": "application/json"})
        )
        with self.assertRaises(ProviderError) as raised:
            provider.complete(system="s", user="u")
        self.assertIn("模型请求失败", str(raised.exception))

    def test_sse_body_without_content_type_is_still_parsed(self):
        """F 边界：content-type 缺失/标错但 body 是 SSE 时按文本解析，不误报错误。"""
        events = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}]
        provider, _ = make_stream_provider(
            lambda request: httpx.Response(200, text=sse_body(events))  # 无 content-type
        )
        response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "ok")

    def test_empty_event_stream_falls_back_instead_of_saving_blank_translation(self):
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(200, text="data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "recovered"}, "finish_reason": "stop"}],
                "usage": {},
            })

        provider, _ = make_stream_provider(handler)
        response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "recovered")
        self.assertTrue(provider._stream_disabled)

    def test_read_timeout_mid_stream_maps_to_provider_timeout(self):
        def generator():
            yield b"data: " + json.dumps({"choices": [{"delta": {"content": " partial"}}]}).encode() + b"\n\n"
            raise httpx.ReadTimeout("idle too long")

        provider, _ = make_stream_provider(
            lambda request: httpx.Response(200, content=generator(), headers={"content-type": "text/event-stream"})
        )
        with self.assertRaises(ProviderTimeoutError) as raised:
            provider.complete(system="s", user="u")
        self.assertEqual(raised.exception.kind, "read")

    def test_reasoning_request_fields_are_sent_on_stream_path_too(self):
        events = [{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}}]
        provider, requests = make_stream_provider(
            lambda request: httpx.Response(200, text=sse_body(events), headers={"content-type": "text/event-stream"})
        )
        provider.config.reasoning_style = "qwen_thinking"
        provider.config.reasoning_effort = "off"
        provider.complete(system="s", user="u")
        self.assertIs(requests[0]["enable_thinking"], False)


# ---- 超时切分重试（translation） ------------------------------------------- #


def block(block_id: str, text: str, *, order: int = 0) -> Block:
    return Block(block_id=block_id, type="paragraph", order=order, page_index=0, source_text=text)


class TimeoutOnLargeBatchProvider(MockProvider):
    """一批多个 segment 就超时；单 segment 时正常翻译（模拟慢思考模型）。"""

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        if len(re.findall(r'<src\s+id=', user)) > 1:
            raise ProviderTimeoutError("read", 180)
        return super().complete(system=system, user=user)


class AlwaysTimeoutProvider(MockProvider):
    def complete(self, *, system: str, user: str) -> ProviderResponse:
        raise ProviderTimeoutError("read", 180)


class TimeoutSplitRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_project(self, blocks: list[Block]) -> tuple[ProjectStore, str]:
        store = ProjectStore(self.root / "library")
        pdf = self.root / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% fixture")
        project = store.create_project(pdf, title="Timeout Fixture")
        project_id = project["project_id"]
        document = Document(project_id=project_id, title="Timeout Fixture", source_language="en", target_language="zh-CN")
        document.blocks = blocks
        document.page_count = 1
        store.save_document(document)
        return store, project_id

    def test_timeout_is_retried_after_splitting_and_all_blocks_translate(self):
        store, project_id = self.make_project([
            block("b1", "The first paragraph.", order=0),
            block("b2", "The second paragraph.", order=1),
            block("b3", "The third paragraph.", order=2),
        ])
        job = TranslationService(store, TimeoutOnLargeBatchProvider()).run(project_id)
        self.assertEqual(job["status"], "completed")
        layer = store.load_translation(project_id, "zh-CN")
        for block_id in ("b1", "b2", "b3"):
            self.assertEqual(layer.entries[block_id].status, "translated")
            self.assertTrue(layer.entries[block_id].text)

    def test_single_segment_timeout_fails_with_actionable_reason(self):
        from literature.providers import ProviderError
        store, project_id = self.make_project([block("b1", "Only paragraph.")])
        with self.assertRaises(ProviderError):
            TranslationService(store, AlwaysTimeoutProvider()).run(project_id)
        job = store.list_jobs(project_id)[0]
        # 没有任何成功分块时整任务落 failed（不是 partial_failed）。
        self.assertEqual(job["status"], "failed")
        layer = store.load_translation(project_id, "zh-CN")
        entry = layer.entries["b1"]
        self.assertEqual(entry.status, "failed")
        self.assertIn("超时", entry.error or "")

    def test_translation_job_input_has_configurable_read_timeout(self):
        from literature.api import TranslationJobInput
        self.assertEqual(TranslationJobInput().request_timeout_seconds, 300)
        value = TranslationJobInput(request_timeout_seconds=900)
        self.assertEqual(value.request_timeout_seconds, 900)


# ---- 真实 HTTP 流语义（reviewer 复验发现的 ResponseNotRead 缺口） ---------- #


class _RealStreamServer:
    """最小本机 HTTP 服务器：真实 httpx HTTPTransport 下的流式响应语义。"""

    def __init__(self, *, content_type: str | None, body: bytes) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                self.rfile.read(length)
                self.send_response(200)
                if content_type is not None:
                    self.send_header("content-type", content_type)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class RealStreamSemanticsTests(unittest.TestCase):
    """真实 httpx 流下（非 MockTransport）：relay-JSON / 缺 content-type SSE / 200+HTML。"""

    def make_provider(self, base_url: str) -> OpenAICompatibleProvider:
        provider = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="m", base_url=base_url),
            api_key="test-key", timeout=5.0, max_retries=0,
        )
        return provider

    def test_relay_returns_json_without_event_stream_content_type(self):
        import json as jsonlib
        payload = jsonlib.dumps({
            "choices": [{"message": {"content": "relay-ok"}, "finish_reason": "stop"}],
            "usage": {},
        }).encode()
        with _RealStreamServer(content_type="application/json", body=payload) as server:
            provider = self.make_provider(server.base_url)
            response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "relay-ok")

    def test_sse_body_with_wrong_content_type_is_parsed(self):
        body = sse_body([{"choices": [{"delta": {"content": "sse-ok"}, "finish_reason": "stop"}], "usage": {}}]).encode()
        with _RealStreamServer(content_type="text/plain", body=body) as server:
            provider = self.make_provider(server.base_url)
            response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "sse-ok")

    def test_stream_200_html_raises_provider_error_not_response_not_read(self):
        from literature.providers import ProviderError
        with _RealStreamServer(content_type="text/html", body=b"<html>portal</html>") as server:
            provider = self.make_provider(server.base_url)
            with self.assertRaises(ProviderError) as raised:
                provider.complete(system="s", user="u")
        self.assertIn("模型请求失败", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
