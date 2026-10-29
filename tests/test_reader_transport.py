"""AIFold 传输层与适配层契约测试（Orca Worker developer，2026-10-27）。

覆盖两块，全部受控（MockTransport / 本机 loopback HTTP 服务），零付费、不触 Keychain：

一、`OpenAICompatibleProvider.with_request_limits`（单请求专用副本）
  - 副本完全隔离：连接池/流式回退状态/配置独立，原 provider 与其他请求方默认不变；
  - 时限语义：connect ≤ 20、空闲（read）= idle_seconds、单请求总时限 total_seconds
    对连续 SSE 与非流式回退都生效；全程无 Timer/线程（无悬挂定时器）；
  - 输出上限：服务端忽略 max_tokens 时按真实 usage（缺失按保守估算）强制收口，
    超限不重发（usage 挂 error.response 供记账），不隐藏重复计费；
  - on_activity：waiting/generating 事件带 request_started_at / last_response_at /
    elapsed_seconds；回调异常不影响请求。

二、`textfold_adapter`（AIFold 折叠/派生适配）
  - `apply_reply(finalize=False)` 跳过 fold_background/dress；`finalize_doc` 统一收尾；
  - per-unit 错误元数据（missing_reply/invalid_evidence/empty_takeaway/…）与投影透出；
  - 告警不再出现「已回退本地预览」（无本地摘句回退路径，保持未生成/留待补齐）；
  - 派生提示词要求每个编号 evidence 槽位恰好一条 ev_sentences 数组项
    （上标脚注可能把多个真实句子合并进一个槽位）；数量不符仍然整 unit 拒绝。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from literature.models import Block, Document, Page, TranslationEntry, TranslationLayer
from literature.providers import (
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderError,
    ProviderTimeoutError,
)
from literature.storage import ProjectStore

REPO = Path(__file__).resolve().parents[1]


# ---- 工具：受控 OpenAI 兼容服务 --------------------------------------------- #


def sse_body(events: list[dict]) -> str:
    lines = [f"data: {json.dumps(event)}" for event in events]
    return "\n\n".join(lines) + "\n\ndata: [DONE]\n\n"


def make_provider(handler, *, base_url: str = "https://example.invalid/v1"):
    """带 MockTransport 的 provider（请求体全部记录在 requests 列表里）。"""
    requests: list[dict] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return handler(request)

    provider = OpenAICompatibleProvider(
        ProviderConfig(provider="custom", model="m", base_url=base_url),
        api_key="test-key", timeout=5.0, max_retries=2,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(recording_handler))
    provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
    return provider, requests


def make_limited(handler, *, idle: float = 90.0, total: float = 180.0, cap: int = 4000,
                 on_activity=None, base_url: str = "https://example.invalid/v1"):
    """with_request_limits 副本 + 注入 MockTransport 连接池（with_request_limits 会重置连接池）。"""
    provider, requests = make_provider(handler, base_url=base_url)
    limited = provider.with_request_limits(
        idle_seconds=idle, total_seconds=total, max_output_tokens=cap, on_activity=on_activity,
    )
    limited._client = httpx.Client(transport=httpx.MockTransport(
        lambda request: (requests.append(json.loads(request.content.decode("utf-8"))),
                         handler(request))[1],
    ))
    return provider, limited, requests


def chat_response(content: str, *, usage: dict | None = None, finish: str = "stop") -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": usage if usage is not None else {"prompt_tokens": 10, "completion_tokens": 5},
        "model": "m", "id": "chatcmpl-1",
    }


class _Handler(BaseHTTPRequestHandler):
    """本机 loopback HTTP 服务：按 server.behavior 分发（真实 socket 超时才可测时限）。"""

    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:  # 静默
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            body = {}
        behavior = getattr(self.server, "behavior", None)
        try:
            if behavior is not None:
                behavior(self, body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端时限中止后服务端继续写会断管，属预期


class _LoopbackServer:
    """每测试一个独立端口的真实 HTTP 服务（127.0.0.1，无外网）。

    单线程 HTTPServer：handler 里的长等待必须用 ``server.stop_event.wait()``
    而不是 ``time.sleep()``，否则 tearDown 的 shutdown 要等当前请求睡完
    （每个用例白等 10–30s，且测试结束后仍有残留 handler 线程）。
    """

    def __init__(self, behavior) -> None:
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.server.stop_event = threading.Event()
        self.server.behavior = behavior
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.stop_event.set()      # 先放行所有等待中的 handler
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1.0)


def sse_event(event: dict) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode("utf-8")


def send_sse_start(handler: _Handler) -> None:
    handler.send_response(200)
    handler.send_header("content-type", "text/event-stream")
    handler.send_header("connection", "close")
    handler.end_headers()


# ---- 一、with_request_limits：副本隔离与默认不变 ----------------------------- #


class RequestLimitsIsolationTests(unittest.TestCase):
    def test_copy_is_isolated_and_defaults_unchanged(self) -> None:
        """副本独立连接池/回退状态/配置；原 provider 与其他请求方默认完全不变。"""
        provider, _ = make_provider(lambda request: httpx.Response(200, json=chat_response("ok")))

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("stream"):
                return httpx.Response(400, text="stream is not supported")
            return httpx.Response(200, json=chat_response("ok"))

        limited = provider.with_request_limits(idle_seconds=1.0, total_seconds=2.0, max_output_tokens=1234)
        limited._client = httpx.Client(transport=httpx.MockTransport(handler))
        self.assertIsNot(limited, provider)
        self.assertIsNot(limited.config, provider.config)
        self.assertEqual(limited.config.max_output_tokens, 1234)
        self.assertEqual(provider.config.max_output_tokens, 16384)  # 原配置不动
        self.assertEqual(limited._request_limits["max_output_tokens"], 1234.0)
        self.assertIsNot(limited._client_lock, provider._client_lock)
        self.assertTrue(limited.complete(system="s", user="u").content == "ok")
        # 副本内的流式回退不泄漏给共享实例；副本连接池独立。
        self.assertTrue(limited._stream_disabled)
        self.assertFalse(provider._stream_disabled)
        self.assertIsNot(limited._client, provider._client, "副本连接池不得与共享实例共用")
        # 其他请求方拿到的默认 provider 不带限额。
        fresh, _ = make_provider(lambda request: httpx.Response(200, json=chat_response("ok")))
        self.assertIsNone(fresh._request_limits)
        self.assertIsNone(fresh._on_activity)

    def test_request_body_carries_output_cap(self) -> None:
        _, limited, requests = make_limited(lambda request: httpx.Response(200, json=chat_response("ok")), cap=4000)
        limited.complete(system="s", user="u")
        self.assertEqual(requests[0]["max_tokens"], 4000)

    def test_inherited_capability_fallback_flags(self) -> None:
        """能力回退结论（流式/思考字段被拒）随副本继承：不重复已被拒绝的请求。"""
        nonstream_bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            self.assertFalse(body.get("stream"), "已探测到不支持流式的服务不得重试流式请求")
            nonstream_bodies.append(body)
            return httpx.Response(200, json=chat_response("ok"))

        provider, _ = make_provider(handler)
        provider._stream_disabled = True        # 能力探测结论（如 probe/历史请求得出）
        provider._drop_reasoning_fields = True
        limited = provider.with_request_limits(idle_seconds=5.0, total_seconds=6.0, max_output_tokens=4000)
        limited._client = httpx.Client(transport=httpx.MockTransport(handler))
        self.assertEqual(limited.complete(system="s", user="u").content, "ok")
        self.assertEqual(len(nonstream_bodies), 1)
        self.assertNotIn("enable_thinking", nonstream_bodies[0])

    def test_no_hidden_5xx_retry_for_limited_clone(self) -> None:
        """副本对可能已计费的 5xx 不自动重发（base 会重试 500；副本只发一次）。"""
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(500, text="upstream boom")

        base, _ = make_provider(lambda request: httpx.Response(500, text="unused"))  # max_retries=2
        limited = base.with_request_limits(idle_seconds=5.0, total_seconds=6.0, max_output_tokens=4000)
        limited._client = httpx.Client(transport=httpx.MockTransport(handler))
        with self.assertRaises(ProviderError):
            limited.complete(system="s", user="u")
        self.assertEqual(len(requests), 1, "5xx 可能已计费：不得隐藏重发")

    def test_429_still_retried_for_limited_clone(self) -> None:
        """明确未计费的 429 允许退避重试（本次退避置零，仅验证重试发生）。"""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, json=chat_response("ok"))

        provider, _ = make_provider(handler)
        limited = provider.with_request_limits(idle_seconds=5.0, total_seconds=6.0, max_output_tokens=4000)
        limited._client = httpx.Client(transport=httpx.MockTransport(handler))
        limited._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
        self.assertEqual(limited.complete(system="s", user="u").content, "ok")
        self.assertEqual(calls["n"], 2)


# ---- 一、输出上限强制收口（服务端忽略 max_tokens 时） ------------------------- #


class OutputCapTests(unittest.TestCase):
    def test_cap_enforced_by_reported_usage_without_resend(self) -> None:
        """usage 报告超限：ProviderError + error.response 保留真实 usage；只发一次请求。"""
        usage = {"prompt_tokens": 1200, "completion_tokens": 9000}
        handler = lambda request: httpx.Response(200, json=chat_response("长" * 3000, usage=usage))
        _, limited, requests = make_limited(handler, cap=4000)
        with self.assertRaises(ProviderError) as raised:
            limited.complete(system="s", user="u")
        self.assertIn("上限", str(raised.exception))
        response = raised.exception.response
        self.assertIsNotNone(response)
        self.assertEqual(response.output_tokens, 9000)
        self.assertEqual(response.input_tokens, 1200)   # 真实 usage 完整保留（供记账）
        self.assertEqual(response.content, "长" * 3000)
        self.assertEqual(response.finish_reason, "stop")   # 服务端自身 finish_reason 原样保留
        self.assertEqual(len(requests), 1, "超限不得自动重发（避免重复计费）")

    def test_cap_enforced_by_estimate_when_usage_missing(self) -> None:
        """服务不回 usage：按内容保守估算复核（CJK 按字计），超限同样拒绝。"""
        handler = lambda request: httpx.Response(200, json=chat_response("字" * 5000, usage={}))
        _, limited, requests = make_limited(handler, cap=4000)
        with self.assertRaises(ProviderError):
            limited.complete(system="s", user="u")
        self.assertEqual(len(requests), 1)

    def test_under_cap_returns_normally(self) -> None:
        _, limited, _ = make_limited(lambda request: httpx.Response(200, json=chat_response("短输出")), cap=4000)
        response = limited.complete(system="s", user="u")
        self.assertEqual(response.content, "短输出")
        self.assertEqual(response.finish_reason, "stop")

    def test_default_provider_has_no_cap_enforcement(self) -> None:
        """未挂限额的 provider（翻译/结构等）行为不变：不因内容长而被副本逻辑拒绝。"""
        handler = lambda request: httpx.Response(200, json=chat_response("长" * 3000, usage={
            "prompt_tokens": 10, "completion_tokens": 9000}))
        provider, _ = make_provider(handler)
        response = provider.complete(system="s", user="u")
        self.assertEqual(response.output_tokens, 9000)


# ---- 一、活动回调与无悬挂定时器 ---------------------------------------------- #


class ActivityCallbackTests(unittest.TestCase):
    def test_waiting_then_generating_events_with_fields(self) -> None:
        events = [
            {"choices": [{"delta": {"content": "你"}}]},
            {"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
        ]
        seen: list[dict] = []
        _, limited, _ = make_limited(
            lambda request: httpx.Response(200, text=sse_body(events),
                                           headers={"content-type": "text/event-stream"}),
            on_activity=seen.append,
        )
        limited.complete(system="s", user="u")
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen[0]["state"], "waiting")
        self.assertTrue(any(item["state"] == "generating" for item in seen))
        for item in seen:
            # state 与 stage 同值（core 侧 stage 归一化；本契约两者都给）。
            self.assertEqual(item["stage"], item["state"])
            # 时间戳为 ISO 8601 UTC 字符串（UI 可直接展示；另附 epoch 浮点供计算）。
            self.assertRegex(item["request_started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertRegex(item["last_response_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertIsInstance(item["request_started_at_epoch"], float)
            self.assertIsInstance(item["last_response_at_epoch"], float)
            self.assertGreaterEqual(item["last_response_at_epoch"], item["request_started_at_epoch"])
            self.assertGreaterEqual(item["elapsed_seconds"], 0.0)

    def test_activity_exception_never_breaks_request(self) -> None:
        def broken(activity: dict) -> None:
            raise RuntimeError("前端进度环崩了也不该影响请求")

        _, limited, _ = make_limited(
            lambda request: httpx.Response(200, json=chat_response("ok")), on_activity=broken,
        )
        self.assertEqual(limited.complete(system="s", user="u").content, "ok")

    def test_no_threads_or_timers_left_after_success(self) -> None:
        """无悬挂定时器：请求后看门狗已 cancel/join，线程数回到基线（轮询等收尾，避免竞态）。"""
        before = threading.active_count()
        _, limited, _ = make_limited(lambda request: httpx.Response(200, json=chat_response("ok")))
        for _ in range(3):
            limited.complete(system="s", user="u")
        settle_at = time.monotonic() + 5.0
        while threading.active_count() > before and time.monotonic() < settle_at:
            time.sleep(0.05)
        self.assertEqual(threading.active_count(), before)
        limited.close()


# ---- 一、时限：连续 SSE 总时限 / 空闲超时 / 非流式回退 ------------------------ #


class DeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._servers: list[_LoopbackServer] = []

    def tearDown(self) -> None:
        for server in self._servers:
            server.stop()

    def _server(self, behavior) -> _LoopbackServer:
        server = _LoopbackServer(behavior)
        self._servers.append(server)
        return server

    def _limited_on(self, url: str, **kwargs) -> OpenAICompatibleProvider:
        provider = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="m", base_url=f"{url}"),
            api_key="test-key", timeout=30.0, max_retries=0,
        )
        return provider.with_request_limits(**kwargs)

    def test_continuous_sse_cannot_bypass_total_deadline(self) -> None:
        """连续吐 token 的 SSE 流不会绕过 total_seconds（逐事件检查绝对 deadline）。"""
        def behavior(handler: _Handler, body: dict) -> None:
            send_sse_start(handler)
            piece = {"choices": [{"delta": {"content": "词"}}]}
            stop = handler.server.stop_event
            for _ in range(2000):           # 持续流，永不主动结束
                handler.wfile.write(sse_event(piece))
                handler.wfile.flush()
                if stop.wait(0.01):
                    break

        server = self._server(behavior)
        # 输出上限调大：本测试只验总时限，不与字节上限耦合。
        limited = self._limited_on(server.url, idle_seconds=5.0, total_seconds=1.0, max_output_tokens=10_000_000)
        started = time.monotonic()
        with self.assertRaises(ProviderTimeoutError) as raised:
            limited.complete(system="s", user="u")
        elapsed = time.monotonic() - started
        self.assertEqual(raised.exception.kind, "total")
        self.assertTrue(raised.exception.possible_charge)   # 流里已收到数据 → 可能已计费
        # 判别性：远小于 read 超时（idle=5s）与 2000×0.01s 的流长度。
        self.assertLess(elapsed, 3.0, f"总时限未生效（耗时 {elapsed:.1f}s）")
        limited.close()

    def test_watchdog_interrupts_blocked_recv_on_stall(self) -> None:
        """真实 socket 阻塞读也要被看门狗打断：首块后静默，total=0.3s 必须在 0.8s 内返回
        （macOS 上另一线程 close 不一定能唤醒 recv，看门狗会先 shutdown socket）。"""
        def behavior(handler: _Handler, body: dict) -> None:
            time.sleep(0.1)                      # 首块在 t≈0.1s 到达
            send_sse_start(handler)
            handler.wfile.write(sse_event({"choices": [{"delta": {"content": "开"}}]}))
            handler.wfile.flush()
            handler.server.stop_event.wait(10)   # 之后静默；idle=5 > total=0.3

        server = self._server(behavior)
        limited = self._limited_on(server.url, idle_seconds=5.0, total_seconds=0.3, max_output_tokens=4000)
        started = time.monotonic()
        with self.assertRaises(ProviderTimeoutError) as raised:
            limited.complete(system="s", user="u")
        elapsed = time.monotonic() - started
        self.assertEqual(raised.exception.kind, "total")
        # 判别性：远小于 idle=5s 与服务端 stall=10s（否则就是时限没生效），
        # 但留足调度抖动余量，不依赖 0.1s 级线程调度精度。
        self.assertLess(elapsed, 2.5, f"看门狗/socket 超时未能兑住总时限（耗时 {elapsed:.2f}s）")
        limited.close()

    def test_stall_after_last_chunk_cannot_overrun_total_deadline(self) -> None:
        """stall near deadline：末块之后静默——看门狗到点关流，总时限不被 idle 拉长。"""
        def behavior(handler: _Handler, body: dict) -> None:
            send_sse_start(handler)
            handler.wfile.write(sse_event({"choices": [{"delta": {"content": "开"}}]}))
            handler.wfile.flush()
            handler.server.stop_event.wait(30)   # 之后再无数据；idle=5 > total=1.2

        server = self._server(behavior)
        limited = self._limited_on(server.url, idle_seconds=5.0, total_seconds=1.2, max_output_tokens=4000)
        started = time.monotonic()
        with self.assertRaises(ProviderTimeoutError) as raised:
            limited.complete(system="s", user="u")
        elapsed = time.monotonic() - started
        self.assertEqual(raised.exception.kind, "total",
                         "idle(5s) > total(1.2s)：静默必须由看门狗在总时限处中止，而不是等到 idle")
        self.assertTrue(raised.exception.possible_charge)
        self.assertLess(elapsed, 4.0, f"看门狗未生效（耗时 {elapsed:.1f}s）")
        limited.close()

    def test_active_output_cap_counts_semantic_bytes_not_framing(self) -> None:
        """传输中止源于**语义文本**超限，不是 SSE 框架字节：error 里记录的已收字节数
        必须落在语义上限内（框架字节远大于它时也不提前触发）。"""
        def behavior(handler: _Handler, body: dict) -> None:
            send_sse_start(handler)
            piece = {"choices": [{"delta": {"content": "失控输出" * 64}}]}
            stop = handler.server.stop_event
            for _ in range(20000):
                handler.wfile.write(sse_event(piece))
                handler.wfile.flush()
                if stop.wait(0.001):
                    break

        server = self._server(behavior)
        limited = self._limited_on(server.url, idle_seconds=5.0, total_seconds=60.0, max_output_tokens=4000)
        with self.assertRaises(ProviderError) as raised:
            limited.complete(system="s", user="u")
        info = (raised.exception.response.raw or {}).get("output_cap_exceeded") or {}
        semantic_cap = 4000 * OpenAICompatibleProvider.OUTPUT_SEMANTIC_BYTE_PER_TOKEN + OpenAICompatibleProvider.OUTPUT_SEMANTIC_ENVELOPE
        self.assertGreater(info.get("bytes_seen", 0), semantic_cap - 8000, "中止时应已接近语义上限（而非首事件就误杀）")
        # 计数按增量推进，越界判定允许不超过一个事件（768 字节）的超出。
        self.assertLessEqual(info.get("bytes_seen", 0), semantic_cap + 768)
        self.assertEqual(info.get("cap_bytes"), semantic_cap)
        limited.close()

    def test_many_tiny_legitimate_events_pass_output_cap(self) -> None:
        """输出上限只计解码后的语义文本：大量小事件（框架字节多、语义少）必须成功。"""
        def behavior(handler: _Handler, body: dict) -> None:
            send_sse_start(handler)
            for _ in range(1000):            # 1000 个小事件：框架字节远大于语义字节
                handler.wfile.write(sse_event({"choices": [{"delta": {"content": "字"}}]}))
            handler.wfile.write(sse_event({"choices": [{"delta": {}, "finish_reason": "stop"}],
                                           "usage": {"prompt_tokens": 10, "completion_tokens": 1000}}))
            handler.wfile.write(b"data: [DONE]\n\n")
            handler.wfile.flush()

        server = self._server(behavior)
        # 语义上限 ≈ 4000×4+8192；1000 个「字」仅 3000 字节，若误把框架字节计入就会误杀。
        limited = self._limited_on(server.url, idle_seconds=5.0, total_seconds=10.0, max_output_tokens=4000)
        response = limited.complete(system="s", user="u")
        self.assertEqual(response.content, "字" * 1000)
        self.assertEqual(response.finish_reason, "stop")
        limited.close()

    def test_slow_response_headers_are_bounded(self) -> None:
        """慢响应头：总时限含初始 headers 等待（read 超时 = min(idle, total)）。"""
        def behavior(handler: _Handler, body: dict) -> None:
            handler.server.stop_event.wait(30)   # 30s 后才发响应头
            if handler.server.stop_event.is_set():
                return
            send_sse_start(handler)

        server = self._server(behavior)
        limited = self._limited_on(server.url, idle_seconds=5.0, total_seconds=1.0, max_output_tokens=4000)
        started = time.monotonic()
        with self.assertRaises(ProviderTimeoutError) as raised:
            limited.complete(system="s", user="u")
        elapsed = time.monotonic() - started
        # 判别性：远小于服务端的 30s 延迟；余量放宽到 3×total，避免全量跑时调度抖动误报。
        self.assertEqual(raised.exception.kind, "total")
        self.assertLess(elapsed, 3.0, f"等待响应头未被时限兑住（耗时 {elapsed:.1f}s）")
        limited.close()

    def test_total_deadline_spans_protocol_fallback_attempts(self) -> None:
        """总时限按整个 complete 计：400 回退后的非流式重发不重新拉满，静默由看门狗兑住。"""
        def behavior(handler: _Handler, body: dict) -> None:
            if body.get("stream"):
                handler.send_response(400)
                handler.send_header("content-type", "text/plain")
                handler.end_headers()
                handler.wfile.write(b"stream is not supported")
                return
            handler.server.stop_event.wait(30)   # 回退后静默；idle=5 > 剩余时限

        server = self._server(behavior)
        limited = self._limited_on(server.url, idle_seconds=5.0, total_seconds=1.5, max_output_tokens=4000)
        started = time.monotonic()
        with self.assertRaises(ProviderTimeoutError) as raised:
            limited.complete(system="s", user="u")
        elapsed = time.monotonic() - started
        self.assertEqual(raised.exception.kind, "total",
                         "回退重发不得重新拉满总时限（否则 400+静默可超过 180s）")
        self.assertLess(elapsed, 4.5, f"跨 attempt 总时限未生效（耗时 {elapsed:.1f}s）")
        limited.close()

    def test_idle_timeout_on_stalled_sse(self) -> None:
        """SSE 静默超过 idle_seconds：read 超时中止（按可能已计费），不挂死。"""
        def behavior(handler: _Handler, body: dict) -> None:
            send_sse_start(handler)
            handler.wfile.write(sse_event({"choices": [{"delta": {"content": "开"}}]}))
            handler.wfile.flush()
            handler.server.stop_event.wait(30)   # 之后再无数据

        server = self._server(behavior)
        limited = self._limited_on(server.url, idle_seconds=0.6, total_seconds=10.0, max_output_tokens=4000)
        started = time.monotonic()
        with self.assertRaises(ProviderTimeoutError) as raised:
            limited.complete(system="s", user="u")
        self.assertEqual(raised.exception.kind, "read")
        self.assertTrue(raised.exception.possible_charge)
        self.assertLess(time.monotonic() - started, 8.0)
        limited.close()

    def test_nonstream_fallback_within_limits_and_isolated(self) -> None:
        """非流式回退同样受时限覆盖；回退状态不泄漏给原 provider。"""
        def behavior(handler: _Handler, body: dict) -> None:
            if body.get("stream"):
                handler.send_response(400)
                handler.send_header("content-type", "text/plain")
                handler.end_headers()
                handler.wfile.write(b"stream is not supported")
                return
            time.sleep(0.1)
            payload = json.dumps(chat_response("fallback-ok")).encode("utf-8")
            handler.send_response(200)
            handler.send_header("content-type", "application/json")
            handler.send_header("content-length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

        server = self._server(behavior)
        base = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="m", base_url=server.url),
            api_key="test-key", timeout=30.0, max_retries=2,
        )
        limited = base.with_request_limits(idle_seconds=5.0, total_seconds=6.0, max_output_tokens=4000)
        response = limited.complete(system="s", user="u")
        self.assertEqual(response.content, "fallback-ok")
        self.assertTrue(limited._stream_disabled, "副本应记住非流式回退")
        self.assertFalse(base._stream_disabled, "回退状态不得泄漏给共享实例")
        limited.close()
        base.close()

    def test_nonstream_stall_aborts_at_idle_deadline(self) -> None:
        """非流式回退后服务端整段静默：idle_seconds 同样兜住（不挂到 total 之外）。"""
        def behavior(handler: _Handler, body: dict) -> None:
            if body.get("stream"):
                handler.send_response(400)
                handler.send_header("content-type", "text/plain")
                handler.end_headers()
                handler.wfile.write(b"stream is not supported")
                return
            handler.server.stop_event.wait(30)

        server = self._server(behavior)
        base = OpenAICompatibleProvider(
            ProviderConfig(provider="custom", model="m", base_url=server.url),
            api_key="test-key", timeout=30.0, max_retries=0,
        )
        limited = base.with_request_limits(idle_seconds=0.6, total_seconds=10.0, max_output_tokens=4000)
        started = time.monotonic()
        with self.assertRaises(ProviderTimeoutError) as raised:
            limited.complete(system="s", user="u")
        self.assertEqual(raised.exception.kind, "read")
        self.assertLess(time.monotonic() - started, 8.0)
        limited.close()
        base.close()

    def test_active_output_cap_aborts_runaway_stream(self) -> None:
        """服务端忽略 max_tokens 失控生成：按解码后语义文本字节数中止（不等读完 48k token）。"""
        def behavior(handler: _Handler, body: dict) -> None:
            send_sse_start(handler)
            piece = {"choices": [{"delta": {"content": "失控输出" * 64}}]}   # 每事件 ≈768 语义字节
            stop = handler.server.stop_event
            for _ in range(20000):
                handler.wfile.write(sse_event(piece))
                handler.wfile.flush()
                if stop.wait(0.001):
                    break

        server = self._server(behavior)
        limited = self._limited_on(server.url, idle_seconds=5.0, total_seconds=60.0, max_output_tokens=4000)
        started = time.monotonic()
        with self.assertRaises(ProviderError) as raised:
            limited.complete(system="s", user="u")
        elapsed = time.monotonic() - started
        self.assertTrue(raised.exception.possible_charge)   # 费用未知口径，不自动重发
        self.assertIn("上限", str(raised.exception))
        self.assertIsNotNone(raised.exception.response)
        self.assertEqual(raised.exception.response.finish_reason, "")  # 不伪造上游 finish_reason
        self.assertLess(elapsed, 10.0, f"语义输出上限未在传输中生效（耗时 {elapsed:.1f}s）")
        limited.close()

    def test_no_threads_left_after_deadline_abort(self) -> None:
        """时限中止后无悬挂看门狗线程（轮询等收尾，不依赖线程调度时序）。"""
        def behavior(handler: _Handler, body: dict) -> None:
            send_sse_start(handler)
            piece = {"choices": [{"delta": {"content": "词"}}]}
            stop = handler.server.stop_event
            for _ in range(2000):
                handler.wfile.write(sse_event(piece))
                handler.wfile.flush()
                if stop.wait(0.01):
                    break

        server = self._server(behavior)
        limited = self._limited_on(server.url, idle_seconds=5.0, total_seconds=0.8, max_output_tokens=10_000_000)
        before = threading.active_count()
        with self.assertRaises(ProviderTimeoutError):
            limited.complete(system="s", user="u")
        settle_at = time.monotonic() + 8.0
        while threading.active_count() > before and time.monotonic() < settle_at:
            time.sleep(0.05)
        # 服务端线程是本测试自建的；provider 侧（含看门狗）不应残留任何线程。
        self.assertEqual(threading.active_count(), before)
        limited.close()


# ---- 二、适配层：finalize 可选收尾 -------------------------------------------- #


def make_document() -> Document:
    blocks = [
        Block(block_id="b-title-1", type="title", order=0, page_index=0, source_text="Silver Road", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-p-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 200], source_text=(
            "The opening chapter surveys the trade routes. "
            "Silver moved westward through Sogdian intermediaries. "
            "Workshops in Chang'an adapted the imported forms."
        )),
        Block(block_id="b-p-2", type="paragraph", order=2, page_index=0, bbox=[10, 210, 300, 360], source_text=(
            "Assay records reveal a consistent alloy standard. "
            "Ninety-two percent silver coins circulated widely. "
            "Two mints have been identified near the market."
        )),
    ]
    return Document(project_id="p1", title="Silver Road", source_language="en", page_count=1,
                    pages=[Page(page_index=0, width=600, height=800)], blocks=blocks)


def good_reply() -> dict:
    return {
        "section": {"takeaway": "Silver trade and assay standards shaped the period.", "topic": "Silver trade"},
        "units": [
            {"id": "U0", "takeaway": "The chapter surveys trade routes.", "topic": "trade routes",
             "reason": "It sets the geographic stage.", "evidence": [0]},
            {"id": "U1", "takeaway": "Assay records show a fixed alloy.", "topic": "Assay records",
             "reason": "Mint output supports this.", "evidence": [0]},
        ],
    }


class ApplyReplyFinalizeTests(unittest.TestCase):
    """apply_reply(finalize=False) 跳过 fold_background/dress；finalize_doc 统一收尾。"""

    def setUp(self) -> None:
        from literature.textfold_adapter import build_ladder_doc
        self.doc = build_ladder_doc(make_document())
        self.adapter = __import__("literature.textfold_adapter", fromlist=["tf_ladder"])

    def test_finalize_false_skips_background_and_dress(self) -> None:
        chunk = self.doc["chunks"][0]
        with unittest.mock.patch.object(self.adapter.tf_ladder, "fold_background") as fb, \
                unittest.mock.patch.object(self.adapter.tf_ladder, "dress") as dress:
            warnings = self.adapter.apply_reply(self.doc, chunk, good_reply(), model="m", finalize=False)
        self.assertEqual(fb.call_count, 0)
        self.assertEqual(dress.call_count, 0)
        self.assertEqual(warnings, [])
        self.assertTrue(all(u["source"] == "model" for u in self.doc["units"].values()))

    def test_default_finalize_true_and_finalize_doc_close_all_chunks(self) -> None:
        from literature.textfold_adapter import build_ladder_doc
        other = build_ladder_doc(make_document())
        with unittest.mock.patch.object(self.adapter.tf_ladder, "fold_background") as fb, \
                unittest.mock.patch.object(self.adapter.tf_ladder, "dress") as dress:
            self.adapter.apply_reply(other, other["chunks"][0], good_reply(), model="m")
            self.assertEqual(fb.call_count, 1, "默认行为保持逐节收尾")
            self.assertEqual(dress.call_count, 1)
            # 全量收尾：finalize_doc 对每个有 unit 的 chunk 各执行一次（幂等可重复调用）。
            self.adapter.finalize_doc(other)
            expected = len([c for c in other["chunks"] if c["units"]])
            self.assertEqual(fb.call_count, 1 + expected)
            self.assertEqual(dress.call_count, 1 + expected)
            self.adapter.finalize_doc(other)
            self.assertEqual(fb.call_count, 1 + 2 * expected, "重复收尾不叠加副作用")


# ---- 二、适配层：per-unit 错误元数据 / 无「本地预览」措辞 ----------------------- #


class PerUnitErrorMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        from literature.textfold_adapter import build_ladder_doc
        self.doc = build_ladder_doc(make_document())
        self.chunk = self.doc["chunks"][0]

    def _reply(self, units: list[dict]) -> dict:
        return {"section": good_reply()["section"], "units": units}

    def test_missing_reply_marks_pending_with_metadata(self) -> None:
        from literature.textfold_adapter import apply_reply, section_view
        reply = self._reply([good_reply()["units"][0]])      # 只有 U0，缺 U1
        warnings = apply_reply(self.doc, self.chunk, reply, model="m")
        unit0, unit1 = self.doc["units"]["b-p-1"], self.doc["units"]["b-p-2"]
        self.assertEqual(unit0["source"], "model")
        self.assertNotIn("error", unit0)
        self.assertEqual(unit1["source"], "pending")
        self.assertEqual(unit1["error"]["code"], "missing_reply")
        self.assertIn("b-p-2", unit1["error"]["message"])
        self.assertEqual(unit1["lad"]["model"], "")
        joined = "\n".join(warnings)
        self.assertNotIn("回退本地预览", joined)
        self.assertNotIn("本地预览", joined)
        # 投影透出 error 元数据。
        view = section_view(self.doc, self.chunk)
        view_units = {u["id"]: u for u in view["units"]}
        self.assertEqual(view_units["b-p-2"].get("error", {}).get("code"), "missing_reply")
        self.assertEqual(view_units["b-p-2"]["source"], "pending")
        self.assertNotIn("error", view_units["b-p-1"])

    def test_invalid_evidence_and_empty_takeaway_metadata(self) -> None:
        from literature.textfold_adapter import apply_reply
        u0 = {**good_reply()["units"][0], "evidence": [99]}          # 全部越界
        u1 = {**good_reply()["units"][1], "takeaway": ""}            # 空 takeaway
        apply_reply(self.doc, self.chunk, self._reply([u0, u1]), model="m")
        self.assertEqual(self.doc["units"]["b-p-1"]["error"]["code"], "invalid_evidence")
        self.assertEqual(self.doc["units"]["b-p-2"]["error"]["code"], "empty_takeaway")
        for unit in self.doc["units"].values():
            self.assertEqual(unit["source"], "pending")
            self.assertEqual(unit["lad"]["brief"], [])

    def test_success_clears_stale_error_metadata(self) -> None:
        from literature.textfold_adapter import apply_reply
        self.doc["units"]["b-p-1"]["error"] = {"code": "missing_reply", "message": "旧错误"}
        apply_reply(self.doc, self.chunk, good_reply(), model="m")
        self.assertNotIn("error", self.doc["units"]["b-p-1"])

    def test_source_lane_projection_exposes_error(self) -> None:
        from literature.textfold_adapter import apply_reply, section_view
        reply = self._reply([good_reply()["units"][0]])
        apply_reply(self.doc, self.chunk, reply, model="m")
        view = section_view(self.doc, self.chunk)                    # 原文层投影（lang 默认 en）
        view_units = {u["id"]: u for u in view["units"]}
        self.assertEqual(view_units["b-p-2"].get("error", {}).get("code"), "missing_reply")


# ---- 二、适配层：派生 ev_sentences 槽位契约 ----------------------------------- #


class DeriveSlotContractTests(unittest.TestCase):
    """上标脚注可能让 tokenizer 把多个真实句子并进一个 evidence 槽位：
    提示词必须要求「每个编号槽位恰好一条数组项」，且数量不符仍整 unit 拒绝。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        project = self.store.create_project(source, title="Silver Road")
        self.project_id = project["project_id"]
        self.store.save_document(make_document().__class__(
            project_id=self.project_id, title="Silver Road", source_language="en", page_count=1,
            pages=[Page(page_index=0, width=600, height=800)],
            blocks=[
                Block(block_id="b-title-1", type="title", order=0, page_index=0, source_text="Silver Road", level=1, bbox=[10, 10, 300, 40]),
                Block(block_id="b-p-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 200], source_text=(
                    "Silver moved westward through intermediaries.[1] A second sentence follows the marker. "
                    "Workshops in Chang'an adapted the imported forms."
                )),
                Block(block_id="b-p-2", type="paragraph", order=2, page_index=0, bbox=[10, 210, 300, 360], source_text=(
                    "Assay records reveal a consistent alloy standard. "
                    "Ninety-two percent silver coins circulated widely."
                )),
            ],
        ))
        self.store.save_translation(TranslationLayer(project_id=self.project_id, locale="zh-CN", entries={
            "b-p-1": TranslationEntry(block_id="b-p-1", text="白银经中间商西传。长安工坊改造了外来器形。", status="translated"),
            "b-p-2": TranslationEntry(block_id="b-p-2", text="检测记录显示一致的合金标准。", status="translated"),
        }))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _merged_with_source_folded(self) -> tuple[dict, dict]:
        from literature.textfold_adapter import (
            apply_reply, build_ladder_doc, merge_translation_doc,
        )
        document = self.store.load_document(self.project_id)
        source_ladder = build_ladder_doc(document)
        reply = {
            "section": {"takeaway": "Silver trade and assay standards shaped the period.", "topic": "Silver trade"},
            "units": [
                {"id": "U0", "takeaway": "Silver moved west through intermediaries.", "topic": "Silver",
                 "reason": "Routes are established first.", "evidence": [0]},
                {"id": "U1", "takeaway": "Assay records show a fixed alloy.", "topic": "Assay records",
                 "reason": "Mint data supports it.", "evidence": [0]},
            ],
        }
        for chunk in source_ladder["chunks"]:
            if chunk["units"]:
                apply_reply(source_ladder, chunk, reply, model="m")
        merged = merge_translation_doc(
            source_ladder,
            build_ladder_doc(document, locale="zh-CN",
                             translation=self.store.load_translation(self.project_id, "zh-CN")),
            "zh-CN",
        )
        chunk = next(c for c in merged["chunks"] if c["units"])
        return merged, chunk

    def test_prompt_demands_one_array_item_per_slot(self) -> None:
        from literature.textfold_adapter import derive_context
        merged, chunk = self._merged_with_source_folded()
        system, user, unit_evidence = derive_context(merged, chunk, locale="zh-CN")
        self.assertTrue(unit_evidence)
        self.assertIn("ONE array item per numbered evidence slot", user)
        self.assertIn("len(ev_sentences) MUST equal the number of slots", user)
        self.assertIn("several real sentences", user)                     # 槽内可含多句
        self.assertIn("Never split one slot into multiple items", user)
        self.assertIn("backing sentences as numbered evidence slots", user)

    def test_ev_sentences_count_mismatch_rejected_with_slot_metadata(self) -> None:
        from literature.textfold_adapter import apply_derived_reply, derive_context
        merged, chunk = self._merged_with_source_folded()
        _, _, unit_evidence = derive_context(merged, chunk, locale="zh-CN")
        uid, ev = unit_evidence[0]
        self.assertGreaterEqual(len(ev), 1)
        reply = {"section": {"takeaway": "译文章节主线", "topic": "译文主题"},
                 "units": [{"id": "U0", "takeaway": "译文要点", "reason": "译文理由。", "topic": "译文要点",
                            "ev_sentences": [f"译句{i}" for i in range(len(ev) + 1)]}]}   # 多一条
        warnings, derived, fallback = apply_derived_reply(
            merged, chunk, reply, locale="zh-CN", model="m", unit_evidence=unit_evidence)
        self.assertEqual(derived, 0)
        self.assertEqual(fallback, len(unit_evidence))     # U0 数量不符 + U1 整段缺失
        joined = "\n".join(warnings)
        self.assertIn("槽位", joined)
        self.assertNotIn("回退本地预览", joined)
        self.assertNotIn("本地预览", joined)
        uid, ev = unit_evidence[0]
        unit = merged["units"][uid]
        tr = unit["tr"]["zh-CN"]
        self.assertNotIn("lad", tr)                       # 不写 derived lad（整 unit 拒绝）
        self.assertEqual(tr["error"]["code"], "ev_sentences_mismatch")
        self.assertEqual(list(unit["lad"]["ev"]), list(ev), "源 evidence 不得被改动")
        other_uid, _ = unit_evidence[1]
        self.assertEqual(merged["units"][other_uid]["tr"]["zh-CN"]["error"]["code"], "missing_reply")

    def test_successful_derive_clears_error_and_sets_brief(self) -> None:
        from literature.textfold_adapter import apply_derived_reply, derive_context, section_view
        merged, chunk = self._merged_with_source_folded()
        _, _, unit_evidence = derive_context(merged, chunk, locale="zh-CN")
        reply = {"section": {"takeaway": "译文章节主线", "topic": "译文主题"},
                 "units": [
                     {"id": f"U{n}", "takeaway": f"译文要点{n}", "reason": "译文理由。", "topic": f"要点{n}",
                      "ev_sentences": [f"译句{i}" for i in ev]}
                     for n, (uid, ev) in enumerate(unit_evidence)
                 ]}
        warnings, derived, fallback = apply_derived_reply(
            merged, chunk, reply, locale="zh-CN", model="m", unit_evidence=unit_evidence)
        self.assertEqual(warnings, [])
        self.assertEqual(derived, len(unit_evidence))
        self.assertEqual(fallback, 0)
        for uid, ev in unit_evidence:
            tr = merged["units"][uid]["tr"]["zh-CN"]
            self.assertNotIn("error", tr)
            self.assertEqual(tr["lad"]["brief_source"], "derived")
            self.assertEqual(len(tr["lad"]["brief_sentences"]), len(ev), "brief_sentences 与槽位 1:1")
        view = section_view(merged, chunk, lang="zh-CN")
        for unit in view["units"]:
            if unit.get("brief_source") == "derived":
                self.assertNotIn("error", unit)

    def test_missing_derive_reply_marks_tr_error_without_local_fallback(self) -> None:
        from literature.textfold_adapter import apply_derived_reply, derive_context
        merged, chunk = self._merged_with_source_folded()
        _, _, unit_evidence = derive_context(merged, chunk, locale="zh-CN")
        reply = {"section": {"takeaway": "译文章节主线", "topic": "译文主题"}, "units": []}   # 全缺
        warnings, derived, fallback = apply_derived_reply(
            merged, chunk, reply, locale="zh-CN", model="m", unit_evidence=unit_evidence)
        self.assertEqual(derived, 0)
        self.assertEqual(fallback, len(unit_evidence))
        for uid, _ in unit_evidence:
            tr = merged["units"][uid]["tr"]["zh-CN"]
            self.assertEqual(tr["error"]["code"], "missing_reply")
            self.assertNotIn("lad", tr)
        self.assertNotIn("回退本地预览", "\n".join(warnings))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
