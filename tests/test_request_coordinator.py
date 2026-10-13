from __future__ import annotations

import threading
import time
import unittest

import httpx

from literature.providers import OpenAICompatibleProvider, ProviderConfig
from literature.request_coordinator import BudgetExhausted, RequestCoordinator


def streaming_free_provider(handler, *, max_retries: int = 2) -> OpenAICompatibleProvider:
    provider = OpenAICompatibleProvider(
        ProviderConfig(provider="custom", model="m", base_url="https://example.invalid/v1"),
        api_key="test-key", timeout=5.0, max_retries=max_retries,
    )
    provider._client = httpx.Client(transport=httpx.MockTransport(handler))
    provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
    return provider


class RequestCoordinatorTests(unittest.TestCase):
    def test_429_retry_calls_attempt_hooks_and_records_cooldown(self) -> None:
        attempts = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return httpx.Response(429, text="rate limited")
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })

        provider = streaming_free_provider(handler)
        coordinator = RequestCoordinator(cooldown_seconds=0.01)
        coordinator.attempt_hooks_for("svc", provider)
        response = provider.complete(system="s", user="u")
        self.assertEqual(response.content, "ok")
        # 内部重试的第 2 次 attempt 也经过公共层，且 429 触发了冷却。
        self.assertEqual(attempts["count"], 2)
        self.assertGreater(coordinator._cooldown_until.get("svc", 0.0), 0.0)

    def test_attempt_hook_blocks_when_service_slot_is_full(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {},
            })

        provider = streaming_free_provider(handler, max_retries=0)
        coordinator = RequestCoordinator()
        coordinator.set_service_limit("svc", 1)
        coordinator.attempt_hooks_for("svc", provider)
        release = threading.Event()

        def occupy() -> None:
            semaphore = coordinator.acquire_service("svc")
            release.wait(2.0)
            coordinator.release_service(semaphore)

        worker = threading.Thread(target=occupy)
        worker.start()
        time.sleep(0.05)
        started = time.monotonic()
        response = provider.complete(system="s", user="u")  # 应等待额度释放
        elapsed = time.monotonic() - started
        release.set()
        worker.join()
        self.assertEqual(response.content, "ok")
        self.assertGreater(elapsed, 0.1)

    def test_per_attempt_budget_blocks_before_http(self) -> None:
        """真实 provider 的每次 attempt 必须先过预算；不足时不得发出 HTTP。"""
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}], "usage": {}})

        provider = streaming_free_provider(handler)
        coordinator = RequestCoordinator(spent_loader=lambda pid: 0.0)
        coordinator.attempt_hooks_for(
            "svc", provider, project_id="p", budget=0.0000001, cost_of=lambda chars: 0.001,
        )
        with self.assertRaises(BudgetExhausted):
            provider.complete(system="s", user="u")
        self.assertEqual(calls["count"], 0)

    def test_attempt_reservation_prevents_overspend_under_concurrency(self) -> None:
        """真实 provider + 共享协调层：两路并发只能花一笔（剩余预算不足）。"""
        calls = {"count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 101, "completion_tokens": 101},
            })

        provider = streaming_free_provider(handler)
        provider.config.input_price_per_million = 1.0
        provider.config.output_price_per_million = 1.0
        coordinator = RequestCoordinator(spent_loader=lambda pid: 0.0)
        coordinator.attempt_hooks_for(
            "svc", provider, project_id="p", budget=0.0003, cost_of=lambda chars: 0.000202,
        )
        results: list[str] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def worker() -> None:
            barrier.wait()
            try:
                response = provider.complete(system="a", user="b")
                handle = getattr(provider._attempt_state, "handle", None)
                provider._attempt_state.handle = None
                if handle is not None:
                    coordinator.reconcile(handle, actual_cost=response.estimated_cost)
                outcome = "ok"
            except BudgetExhausted:
                outcome = "blocked"
            with results_lock:
                results.append(outcome)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("ok"), 1, results)
        self.assertEqual(results.count("blocked"), 1, results)
        self.assertEqual(calls["count"], 1)
        self.assertAlmostEqual(coordinator.snapshot("p")["spent_cost"], 0.000202, places=8)

    def test_budget_atomic_across_shared_coordinator(self) -> None:
        coordinator = RequestCoordinator()
        first = coordinator.reserve("p", cost=0.000202, budget=0.0003)
        self.assertIsNotNone(first)
        coordinator.reconcile(first, actual_cost=0.000202)
        self.assertIsNone(coordinator.reserve("p", cost=0.000202, budget=0.0003))


if __name__ == "__main__":
    unittest.main()
