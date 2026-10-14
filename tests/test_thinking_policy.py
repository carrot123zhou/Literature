"""关思考策略：按模型名自动推断 + 字段写法探测 + 探测缓存 + 400 安全网。

背景（本机实测，2026-10-14）：默认服务 `qwen3.8-flash-next` 在翻译作业里
reasoning 占输出 93.7%（单请求平均 9,824 输出 token 中 9,202 是思考），
单请求平均延迟 152 秒。关掉思考是整条链路最大的提速杠杆。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from literature.model_services import ModelServiceStore
from literature.providers import (
    OpenAICompatibleProvider,
    ProviderConfig,
    THINKING_FIELD_VARIANTS,
    disabled_reasoning_effort,
    infer_reasoning_style,
    probe_thinking_off,
    reasoning_request_fields,
    resolve_reasoning_style,
)


def make_provider(handler, *, model: str = "qwen3.8-flash-next", **config_values):
    requests: list[dict] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return handler(request)

    config = ProviderConfig.preset(
        "custom", model=model, base_url="https://relay.example/v1", **config_values,
    )
    provider = OpenAICompatibleProvider(config, api_key="test-key", timeout=5.0)
    provider._client = httpx.Client(transport=httpx.MockTransport(recording_handler))
    provider.max_retries = 0
    provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
    return provider, requests


def completion(request: httpx.Request, *, reasoning: int, content: int = 5) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    return httpx.Response(200, json={
        "model": body.get("model"),
        "choices": [{"message": {"content": "译文"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": content + reasoning,
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        },
    })


class InferReasoningStyleTests(unittest.TestCase):
    def test_qwen_family_is_inferred(self):
        for model in ("qwen3.8-flash-next", "Qwen/Qwen3-32B", "qwq-32b", "qwen3-max"):
            self.assertEqual(infer_reasoning_style(model), "qwen_thinking", model)

    def test_glm_and_openai_families_are_inferred(self):
        self.assertEqual(infer_reasoning_style("glm-4.5"), "glm_thinking")
        self.assertEqual(infer_reasoning_style("gpt-5-mini"), "openai_effort")
        self.assertEqual(infer_reasoning_style("o3-mini"), "openai_effort")

    def test_unknown_models_stay_undeclared(self):
        for model in ("", "gpt-4o-mini", "deepseek-chat", "qwen-plus", "mock"):
            self.assertEqual(infer_reasoning_style(model), "", model)

    def test_explicit_style_wins_over_inference(self):
        config = ProviderConfig.preset("custom", model="qwen3-max", base_url="u")
        config.reasoning_style = "glm_thinking"
        self.assertEqual(resolve_reasoning_style(config), "glm_thinking")
        config.reasoning_style = "auto"
        self.assertEqual(resolve_reasoning_style(config), "qwen_thinking")


class ThinkingFieldVariantTests(unittest.TestCase):
    def config(self, **values):
        base = ProviderConfig.preset("custom", model="qwen3-max", base_url="u")
        for key, value in values.items():
            setattr(base, key, value)
        return base

    def test_variant_switches_the_wire_shape(self):
        self.assertEqual(
            reasoning_request_fields(self.config(reasoning_style="qwen_thinking", reasoning_effort="off")),
            {"enable_thinking": False},
        )
        self.assertEqual(
            reasoning_request_fields(self.config(
                reasoning_style="qwen_thinking", reasoning_effort="off",
                reasoning_field_variant="qwen_chat_template_kwargs",
            )),
            THINKING_FIELD_VARIANTS["qwen_chat_template_kwargs"],
        )
        self.assertEqual(
            reasoning_request_fields(self.config(
                reasoning_style="qwen_thinking", reasoning_effort="off",
                reasoning_field_variant="openai_effort_minimal",
            )),
            {"reasoning_effort": "minimal"},
        )

    def test_variant_never_leaks_into_thinking_on(self):
        fields = reasoning_request_fields(self.config(
            reasoning_style="qwen_thinking", reasoning_effort="on",
            reasoning_field_variant="qwen_chat_template_kwargs",
        ))
        self.assertEqual(fields, {"enable_thinking": True})

    def test_disabled_effort_per_style(self):
        self.assertEqual(disabled_reasoning_effort("qwen_thinking"), "off")
        self.assertEqual(disabled_reasoning_effort("glm_thinking"), "off")
        self.assertEqual(disabled_reasoning_effort("openai_effort"), "minimal")


class ProbeThinkingOffTests(unittest.TestCase):
    def test_first_accepted_variant_wins(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if "enable_thinking" in body:
                return httpx.Response(400, text="unknown field enable_thinking")
            if body.get("chat_template_kwargs") == {"enable_thinking": False}:
                return completion(request, reasoning=0)
            return completion(request, reasoning=80)

        provider, requests = make_provider(handler)
        policy = probe_thinking_off(provider)
        self.assertEqual(policy, {"style": "qwen_thinking", "effort": "off", "variant": "qwen_chat_template_kwargs"})
        # 探测先发一次无思考字段的前置检查，再逐候选试：最后一个请求应是生效的那个写法。
        self.assertNotIn("enable_thinking", requests[0])
        self.assertEqual(requests[-1]["chat_template_kwargs"], {"enable_thinking": False})
        # 探测请求必须很小，否则关不掉思考的模型会把探测本身变成一次昂贵调用。
        self.assertLessEqual(requests[-1]["max_tokens"], 96)

    def test_silently_ignored_field_is_rejected_by_measurement(self):
        """服务忽略未知字段且返回 200：必须靠 reasoning_tokens 占比判定“没关掉”。"""
        provider, requests = make_provider(lambda request: completion(request, reasoning=80))
        self.assertEqual(probe_thinking_off(provider), {})
        # 1 次流式能力前置检查 + qwen 的 3 个候选。
        self.assertEqual(len(requests), 4)

    def test_reasoning_only_response_still_measures_and_tries_next_candidate(self):
        """回归：候选 1 只返回思维链（ProviderError 带完整 usage，status_code=0）时，
        探测必须当“这个写法没关掉思考”而换下一个候选，而不是当临时故障直接放弃。"""
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if "enable_thinking" in body:
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": "", "reasoning_content": "思考中"}, "finish_reason": "length"}],
                    "usage": {"completion_tokens": 96, "completion_tokens_details": {"reasoning_tokens": 96}},
                })
            if body.get("chat_template_kwargs") == {"enable_thinking": False}:
                return completion(request, reasoning=0)
            return completion(request, reasoning=80)

        provider, _ = make_provider(handler)
        self.assertEqual(
            probe_thinking_off(provider),
            {"style": "qwen_thinking", "effort": "off", "variant": "qwen_chat_template_kwargs"},
        )

    def test_transient_failure_aborts_probing(self):
        """5xx/网络错误不能记成“永久不支持”，否则一次抖动就把服务钉死在慢路径上。"""
        provider, requests = make_provider(lambda request: httpx.Response(503, text="busy"))
        self.assertIsNone(probe_thinking_off(provider))
        # 只试了第一个候选就放弃（若继续试其余候选，请求数会大于 1）。
        self.assertEqual(len(requests), 1)

    def test_auth_failure_is_not_treated_as_field_rejection(self):
        """回归：401/403/404 是认证/路由问题，不能写成“字段被拒”的永久负缓存。"""
        for status, text in ((401, "invalid api key"), (403, "forbidden"), (404, "no such endpoint")):
            provider, requests = make_provider(lambda request, s=status, t=text: httpx.Response(s, text=t))
            self.assertIsNone(probe_thinking_off(provider), status)
            self.assertEqual(len(requests), 1, status)  # 不继续换候选

    def test_probe_ignores_stream_rejection(self):
        """回归：服务拒流式时，先把流式关掉再判候选，字段结论不被污染。"""
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("stream"):
                return httpx.Response(400, text="stream is not supported")
            return completion(request, reasoning=0)

        provider, requests = make_provider(handler)
        self.assertEqual(
            probe_thinking_off(provider),
            {"style": "qwen_thinking", "effort": "off", "variant": "qwen_top_level"},
        )
        self.assertTrue(provider._stream_disabled)
        # 前置检查后的所有请求都已改为非流式；带思考字段的最后一个请求仍保留字段。
        self.assertFalse(requests[-1].get("stream"))
        self.assertIn("enable_thinking", requests[-1])

    def test_generic_400_on_stream_still_finds_a_working_variant(self):
        """回归（reviewer 指出的组合缺陷）：非流式服务返回不含 “stream” 字样的通用 400 时，
        前置检查负责关流式，候选自己不会因此被误判为“字段被拒”。"""
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("stream"):
                return httpx.Response(400, text="unsupported parameter")
            return completion(request, reasoning=0)

        provider, requests = make_provider(handler)
        policy = probe_thinking_off(provider)
        self.assertIsNotNone(policy)
        self.assertEqual(policy["variant"], "qwen_top_level")
        self.assertFalse(requests[-1].get("stream"))

    def test_streaming_only_service_is_not_negative_cached(self):
        """回归：只支持流式的服务（非流式 400）不能探测失败。"""
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if not body.get("stream"):
                return httpx.Response(400, text="this endpoint only supports streaming")
            return completion(request, reasoning=0)

        provider, _ = make_provider(handler)
        self.assertEqual(
            probe_thinking_off(provider),
            {"style": "qwen_thinking", "effort": "off", "variant": "qwen_top_level"},
        )
        self.assertFalse(provider._stream_disabled)

    def test_service_without_usage_is_not_negative_cached(self):
        """回归（reviewer R1）：服务返回内容但不回 usage 时，量不出 reasoning 占比，
        必须返回 None（不写缓存），而不是把“无 usage”固化成“开关不掉思考”的永久负缓存。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "译文"}, "finish_reason": "stop"}],
                "usage": {},
            })

        provider, _ = make_provider(handler)
        self.assertIsNone(probe_thinking_off(provider))

    def test_provider_with_attempt_hooks_is_not_probed(self):
        """回归（reviewer R3）：已挂协调层钩子时不能探测（会占用服务额度并污染 attempt 状态）。"""
        provider, requests = make_provider(lambda request: completion(request, reasoning=0))
        provider.attempt_hooks = (lambda chars: None, lambda *args: None)
        self.assertIsNone(probe_thinking_off(provider))
        self.assertEqual(requests, [])

    def test_undeclared_model_is_not_probed(self):
        provider, requests = make_provider(lambda request: completion(request, reasoning=0), model="gpt-4o-mini")
        self.assertEqual(probe_thinking_off(provider), {})
        self.assertEqual(requests, [])

    def test_probe_max_tokens_bounds_a_thinking_model(self):
        provider, requests = make_provider(lambda request: completion(request, reasoning=90))
        probe_thinking_off(provider)
        self.assertTrue(requests)
        self.assertTrue(all(body["max_tokens"] <= 96 for body in requests))

    def test_probe_client_release_only_closes_own_clients(self):
        """父 provider 尚无连接池时子对象自建客户端会被释放；共享父连接池时不会误关。"""
        from literature.providers import _close_probe_client

        class FakeClient:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class Stub:
            def __init__(self, client):
                self._client = client

        # 父连接池为 None：子对象自建的客户端必须被关掉（否则每次探测漏一个 fd/连接池）。
        parent = Stub(None)
        own = Stub(FakeClient())
        _close_probe_client(parent, own)
        self.assertTrue(own._client.closed)

        # 父连接池非 None：复制出来的子对象共享同一个客户端，关闭它会把作业的连接池关掉。
        shared_client = FakeClient()
        shared_parent = Stub(shared_client)
        shared = Stub(shared_client)
        _close_probe_client(shared_parent, shared)
        self.assertFalse(shared_client.closed)

        # child 为 None（with_reasoning 就抛了）：不能报错。
        _close_probe_client(parent, None)


class ReasoningFieldRejectionSafetyNetTests(unittest.TestCase):
    def test_stream_400_does_not_drop_reasoning_fields(self):
        """回归：服务只拒流式（不拒思考字段）时，回退非流式必须带着思考字段。

        否则整个作业会静默退化成“思考全开”的慢路径——正是本次要修的性能问题。
        """
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("stream"):
                return httpx.Response(400, text="stream is not supported")
            return completion(request, reasoning=0)

        provider, requests = make_provider(handler, reasoning_style="qwen_thinking", reasoning_effort="off")
        response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "译文")
        self.assertTrue(provider._stream_disabled)
        self.assertFalse(provider._drop_reasoning_fields)
        self.assertIn("enable_thinking", requests[-1])
        self.assertFalse(requests[-1].get("stream", False))

    def test_400_with_reasoning_fields_is_retried_without_them(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if "enable_thinking" in body:
                return httpx.Response(400, text="unsupported parameter: enable_thinking")
            return completion(request, reasoning=0)

        provider, requests = make_provider(handler, reasoning_style="qwen_thinking", reasoning_effort="off")
        response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "译文")
        self.assertTrue(provider._drop_reasoning_fields)
        self.assertIn("enable_thinking", requests[0])
        self.assertNotIn("enable_thinking", requests[-1])
        # 只在 400 里提到“思考字段”时才摘字段（先摘字段保住流式）。
        self.assertFalse(provider._stream_disabled)

    def test_generic_400_keeps_reasoning_fields_when_stream_is_the_cause(self):
        """回归（reviewer R5）：命中缓存的作业（按默认字段发）遇到通用 400 时，
        优先保“关思考”而不是保流式：先关流式，字段照发。"""
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("stream"):
                return httpx.Response(400, text="bad request")
            return completion(request, reasoning=0)

        provider, requests = make_provider(handler, reasoning_style="qwen_thinking", reasoning_effort="off")
        provider.complete(system="s", user="u")
        self.assertTrue(provider._stream_disabled)
        self.assertFalse(provider._drop_reasoning_fields)
        self.assertIn("enable_thinking", requests[-1])

    def test_400_mentioning_stream_does_not_drop_reasoning_fields(self):
        """回归：“unsupported parameter: stream” 不能被误判成思考字段被拒。"""
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("stream"):
                return httpx.Response(400, text="unsupported parameter: stream")
            return completion(request, reasoning=0)

        provider, requests = make_provider(handler, reasoning_style="qwen_thinking", reasoning_effort="off")
        provider.complete(system="s", user="u")
        self.assertFalse(provider._drop_reasoning_fields)
        self.assertIn("enable_thinking", requests[-1])

    def test_400_without_reasoning_fields_still_raises(self):
        from literature.providers import ProviderError

        provider, _ = make_provider(lambda request: httpx.Response(400, text="bad request"))
        with self.assertRaises(ProviderError):
            provider.complete(system="s", user="u")


class ThinkingPolicyCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.models = ModelServiceStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_round_trip_and_forget(self):
        self.assertIsNone(self.models.thinking_policy("svc", "qwen3-max"))
        self.models.remember_thinking_policy("svc", "qwen3-max", {"style": "qwen_thinking", "effort": "off", "variant": "qwen_top_level"})
        cached = self.models.thinking_policy("svc", "qwen3-max")
        self.assertEqual(cached["variant"], "qwen_top_level")
        self.assertIsNone(self.models.thinking_policy("svc", "other-model"))
        self.models.forget_thinking_policy("svc", "qwen3-max")
        self.assertIsNone(self.models.thinking_policy("svc", "qwen3-max"))

    def test_empty_result_is_cached_so_probing_does_not_repeat(self):
        entry = self.models.remember_thinking_policy("svc", "qwen3-max", {})
        self.assertEqual(entry["variant"], "")
        self.assertIsNotNone(self.models.thinking_policy("svc", "qwen3-max"))


class ApplyThinkingPolicyTests(unittest.TestCase):
    """JobManager.apply_thinking_policy：决定关不关、探测一次、之后命中缓存。"""

    def setUp(self):
        from literature.api import JobManager
        from literature.storage import ProjectStore

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        store = ProjectStore(self.root / "library")
        self.models = ModelServiceStore(self.root)
        self.manager = JobManager(store, None, self.models)

    def tearDown(self):
        self.temporary.cleanup()

    def provider(self, handler, **config_values):
        return make_provider(handler, **config_values)[0]

    def test_auto_off_probes_once_then_uses_cache(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if "enable_thinking" in body:
                return httpx.Response(400, text="unknown field")
            return completion(request, reasoning=0)

        calls: list[dict] = []

        def counting(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content.decode("utf-8")))
            return handler(request)

        first = self.manager.apply_thinking_policy(self.provider(counting), "svc", allow_probe=True)
        self.assertEqual(first.config.reasoning_style, "qwen_thinking")
        self.assertEqual(first.config.reasoning_effort, "off")
        self.assertEqual(first.config.reasoning_field_variant, "qwen_chat_template_kwargs")
        probed = len(calls)
        self.assertGreater(probed, 0)

        # 第二个作业（新 provider 实例）应直接命中缓存，不再发探测请求。
        second = self.manager.apply_thinking_policy(self.provider(counting), "svc", allow_probe=True)
        self.assertEqual(second.config.reasoning_field_variant, "qwen_chat_template_kwargs")
        self.assertEqual(len(calls), probed)

    def test_probe_disabled_uses_default_field(self):
        provider = self.provider(lambda request: completion(request, reasoning=0))
        applied = self.manager.apply_thinking_policy(provider, "svc", allow_probe=False)
        self.assertEqual(applied.config.reasoning_effort, "off")
        self.assertEqual(applied.config.reasoning_field_variant, "")

    def test_explicit_user_choice_is_not_overridden(self):
        provider = self.provider(
            lambda request: completion(request, reasoning=0),
            reasoning_style="qwen_thinking", reasoning_effort="on",
        )
        applied = self.manager.apply_thinking_policy(provider, "svc", allow_probe=True)
        self.assertEqual(applied.config.reasoning_effort, "on")
        self.assertIsNone(self.models.thinking_policy("svc", "qwen3.8-flash-next"))

    def test_undeclared_style_with_explicit_on_is_not_turned_off(self):
        """用户只选了“开关=on”没选协议时，不能被自动推断静默改成 off。"""
        provider = self.provider(
            lambda request: completion(request, reasoning=0),
            reasoning_style="", reasoning_effort="on",
        )
        applied = self.manager.apply_thinking_policy(provider, "svc", allow_probe=True)
        self.assertEqual(applied.config.reasoning_effort, "on")
        self.assertIsNone(self.models.thinking_policy("svc", "qwen3.8-flash-next"))

    def test_transient_probe_failure_is_not_cached(self):
        """探测被 503 打断时不得写负缓存，否则一次抖动就永久关掉优化。"""
        provider = self.provider(lambda request: httpx.Response(503, text="busy"))
        self.manager.apply_thinking_policy(provider, "svc", allow_probe=True)
        self.assertIsNone(self.models.thinking_policy("svc", "qwen3.8-flash-next"))
        # 服务恢复后，下一个作业仍会重新探测。
        recovered = self.provider(lambda request: completion(request, reasoning=0))
        applied = self.manager.apply_thinking_policy(recovered, "svc", allow_probe=True)
        self.assertEqual(applied.config.reasoning_field_variant, "qwen_top_level")

    def test_uninferable_model_is_left_alone(self):
        provider = self.provider(lambda request: completion(request, reasoning=0), model="gpt-4o-mini")
        applied = self.manager.apply_thinking_policy(provider, "svc", allow_probe=True)
        self.assertIs(applied, provider)
        self.assertEqual(applied.config.reasoning_style, "")

    def test_all_candidates_failing_is_cached_as_negative(self):
        provider = self.provider(lambda request: completion(request, reasoning=90))
        applied = self.manager.apply_thinking_policy(provider, "svc", allow_probe=True)
        # 探测不到可用字段时不再发任何思考字段：继续发默认字段只会让严格服务每作业白付一次 400。
        self.assertEqual(applied.config.reasoning_field_variant, "")
        self.assertEqual(applied.config.reasoning_style, "none")
        self.assertEqual(reasoning_request_fields(applied.config), {})
        self.assertEqual(self.models.thinking_policy("svc", "qwen3.8-flash-next")["variant"], "")
        # 第二个作业命中负缓存：不再探测，也不再发字段。
        second_calls: list[dict] = []

        def counting(request: httpx.Request) -> httpx.Response:
            second_calls.append(json.loads(request.content.decode("utf-8")))
            return completion(request, reasoning=90)

        second = self.manager.apply_thinking_policy(self.provider(counting), "svc", allow_probe=True)
        self.assertEqual(second_calls, [])
        self.assertEqual(reasoning_request_fields(second.config), {})

    def test_negative_cache_suppresses_thinking_budget_too(self):
        """回归：负缓存必须连 thinking_budget 一起不发，否则仍会白吃一次 400。"""
        provider = self.provider(
            lambda request: completion(request, reasoning=90),
            reasoning_style="qwen_thinking", reasoning_effort="off", reasoning_budget=8000,
        )
        self.manager.apply_thinking_policy(provider, "svc", allow_probe=True)
        recovered = self.provider(
            lambda request: completion(request, reasoning=0),
            reasoning_style="qwen_thinking", reasoning_effort="off", reasoning_budget=8000,
        )
        applied = self.manager.apply_thinking_policy(recovered, "svc", allow_probe=True)
        self.assertEqual(reasoning_request_fields(applied.config), {})

    def test_no_service_id_skips_cache_and_probe(self):
        provider = self.provider(lambda request: completion(request, reasoning=0))
        applied = self.manager.apply_thinking_policy(provider, "", allow_probe=True)
        self.assertEqual(applied.config.reasoning_effort, "off")
        self.assertEqual(applied.config.reasoning_field_variant, "")
        self.assertEqual(self.models.thinking_policy("", "qwen3.8-flash-next"), None)


class TranslationDefaultsTests(unittest.TestCase):
    def test_translation_job_defaults_to_eight_workers(self):
        """关掉思考后单请求延迟从 ~150 s 掉到 ~10 s 量级，默认并发从 4 提到 8，
        并仍由 AIMD 在 429 时自动降速。"""
        from literature.api import TranslationJobInput
        self.assertEqual(TranslationJobInput().parallel_workers, 8)


if __name__ == "__main__":
    unittest.main()
