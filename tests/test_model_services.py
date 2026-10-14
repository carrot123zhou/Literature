from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from literature.providers import ProviderResponse


def span(text: str) -> dict[str, str]:
    return {"type": "text", "content": text}


def _sse_reasons(reasoning: int, content: str) -> str:
    """一个最小的 SSE 响应：用于让 provider 走真实流式解析链路。"""
    events = [
        {"choices": [{"delta": {"content": content}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 20, "completion_tokens": 5 + reasoning,
                   "completion_tokens_details": {"reasoning_tokens": reasoning}}},
    ]
    return "\n\n".join(f"data: {json.dumps(event)}" for event in events) + "\n\ndata: [DONE]\n\n"


class StructureMockProvider:
    """按调用类型返回固定 JSON：先目录提取，后标题校对。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        self.calls.append(user)
        if "toc_pages" in user:
            content = json.dumps({"toc": [{"title": "Chapter One", "level": 1}, {"title": "Chapter Two", "level": 1}]})
        else:
            payload = json.loads(user)
            candidates = payload.get("candidates", payload.get("title_candidates", []))
            rows = [{"block_id": row["block_id"], "role": "h1", "parent_id": None, "confidence": .9} for row in candidates]
            content = json.dumps({"outline": rows, "profile": {"toc_scope": "chapters only"}} if payload.get("stage") else {"decisions": rows})
        return ProviderResponse(content=content, provider="mock", model="mock", input_tokens=10, output_tokens=5, request_id="req")


class ModelServiceApiTests(unittest.TestCase):
    def test_structure_job_rejects_invalid_timeout_and_completed_reuse(self):
        service = self.create_service()
        path = f"/projects/{self.project_id}/structure/jobs"
        for seconds in (29, 1801):
            response = self.client.post(path, headers=self.headers, json={
                "service_id": service["service_id"], "request_timeout_seconds": seconds})
            self.assertEqual(response.status_code, 422, response.text)
        from literature.storage import ProjectStore
        store = ProjectStore(self.root / "library")
        previous = store.create_job(self.project_id, "structure_ai", {}, {})
        store.update_job(self.project_id, previous, status="completed")
        response = self.client.post(path, headers=self.headers, json={
            "service_id": service["service_id"], "reuse_job_id": previous})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(store.list_jobs(self.project_id)), 1)

    def test_legacy_ai_endpoint_uses_the_same_hierarchy_pipeline(self):
        service = self.create_service()
        provider = StructureMockProvider()
        with patch("literature.model_services.ModelServiceStore.config_for", return_value=provider):
            response = self.client.post(f"/projects/{self.project_id}/structure/analyze-ai", headers=self.headers,
                json={"service_id": service["service_id"], "toc": [{"title": "Chapter One", "level": 1}], "auto_apply": True, "proofread_full": True})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["audit"]["prompt_version"], "hierarchy-v2")
        self.assertEqual(result["hierarchy_review"]["checked"], 1)
        self.assertEqual(len(provider.calls), 2)
        self.assertFalse(result["applied"])
        stored = self.client.get(f"/projects/{self.project_id}/structure/jobs/{result['job_id']}", headers=self.headers).json()
        self.assertEqual(stored["result"]["hierarchy_review"]["checked"], 1)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        os.environ["LITERATURE_RUNTIME"] = str(self.root / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(self.root / "library")
        import fitz
        pdf = fitz.open()
        pdf.new_page(width=600, height=800)
        pdf.new_page(width=600, height=800)
        self.source = self.root / "book.pdf"
        pdf.save(self.source)
        pdf.close()
        self.mineru = self.root / "mineru"
        self.mineru.mkdir()
        pages = [
            [
                {"type": "title", "bbox": [40, 40, 560, 100], "content": {"level": 1, "title_content": [span("Contents")]}},
                {"type": "list", "bbox": [40, 140, 560, 360], "content": {"list_items": [span("Chapter One 1"), span("Chapter Two 5")]}},
            ],
            [
                {"type": "title", "bbox": [40, 40, 560, 100], "content": {"level": 1, "title_content": [span("Chapter One")]}},
                {"type": "paragraph", "bbox": [40, 140, 560, 400], "content": {"paragraph_content": [span("Body text for the structure job contract.")]}},
            ],
        ]
        (self.mineru / "book_content_list_v2.json").write_text(json.dumps(pages), encoding="utf-8")
        (self.mineru / "book_layout.json").write_text(json.dumps({"pdf_info": [
            {"page_idx": 0, "page_size": [600, 800]}, {"page_idx": 1, "page_size": [600, 800]},
        ]}), encoding="utf-8")

        from fastapi.testclient import TestClient
        from literature.api import create_app
        from literature.storage import ProjectStore
        self.client = TestClient(create_app(ProjectStore(self.root / "library")))
        self.headers = {"x-literature-token": self.client.app.state.session_token}
        created = self.client.post("/projects", headers=self.headers, json={"source_file": str(self.source)})
        self.assertEqual(created.status_code, 200, created.text)
        self.project_id = created.json()["project_id"]
        imported = self.client.post(f"/projects/{self.project_id}/mineru/import", headers=self.headers, json={"paths": [str(self.mineru)]})
        self.assertEqual(imported.status_code, 200, imported.text)

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()
        os.environ.pop("LITERATURE_RUNTIME", None)
        os.environ.pop("LITERATURE_LIBRARY", None)

    def create_service(self, **overrides) -> dict:
        payload = {"name": "我的中转站", "preset": "custom", "base_url": "https://relay.example.com/v1", "model": "gpt-4o-mini", **overrides}
        response = self.client.post("/model-services", headers=self.headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_model_service_reasoning_fields_validate_and_persist(self) -> None:
        service = self.create_service(reasoning_style="qwen_thinking", reasoning_effort="off", reasoning_budget=2048, context_window=262144)
        # v2：思考/上下文配置挂在模型上，服务商下可以挂多个模型。
        self.assertEqual(len(service["models"]), 1)
        entry = service["models"][0]
        self.assertEqual(entry["model"], "gpt-4o-mini")
        self.assertEqual(entry["reasoning_style"], "qwen_thinking")
        self.assertEqual(entry["reasoning_effort"], "off")
        self.assertEqual(entry["reasoning_budget"], 2048)
        # 上下文窗口来自 /v1/models，随模型持久化，供前端展示与回填。
        self.assertEqual(entry["context_window"], 262144)
        bad_style = self.client.post("/model-services", headers=self.headers, json={
            "name": "bad", "preset": "custom", "base_url": "https://relay.example.invalid/v1",
            "model": "m", "reasoning_style": "unknown"})
        self.assertGreaterEqual(bad_style.status_code, 400)
        mismatch = self.client.post("/model-services", headers=self.headers, json={
            "name": "bad", "preset": "custom", "base_url": "https://relay.example.invalid/v1",
            "model": "m", "reasoning_style": "openai_effort", "reasoning_effort": "off"})
        self.assertGreaterEqual(mismatch.status_code, 400)

    def test_structure_job_probes_thinking_fields_once_then_uses_cache(self) -> None:
        """回归：只跑结构校对的用户也必须能预热关思考缓存。

        真实网关（yellowbee / qwen3.8-flash-next）实测拒收顶层 `enable_thinking`，
        只认 `chat_template_kwargs.enable_thinking=false`；若结构路径只读缓存，
        这类用户会永远发被拒的默认字段、靠安全网摘掉→思考始终开着。
        """
        import httpx
        from literature.providers import OpenAICompatibleProvider, ProviderConfig

        service = self.create_service(model="qwen3.8-flash-next")
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            requests.append(body)
            if "enable_thinking" in body:
                return httpx.Response(400, text="unknown field enable_thinking")
            if body.get("chat_template_kwargs") == {"enable_thinking": False}:
                return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                      text=_sse_reasons(0, "该分块是合同测试用的占位响应。"))
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=_sse_reasons(80, "该分块是合同测试用的占位响应。"))

        provider = OpenAICompatibleProvider(
            ProviderConfig(provider=service["service_id"], model="qwen3.8-flash-next",
                           base_url="https://relay.example.com/v1"),
            api_key="test-key", timeout=5.0,
        )
        provider._client = httpx.Client(transport=httpx.MockTransport(handler))
        provider.max_retries = 0
        provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]

        def fake_config_for(self, service_id, model="", *, timeout=180.0):
            return provider

        with patch("literature.model_services.ModelServiceStore.config_for", fake_config_for):
            first = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers,
                                     json={"service_id": service["service_id"], "mode": "coarse"})
            self.assertEqual(first.status_code, 200, first.text)
            policy_path = Path(os.environ["LITERATURE_RUNTIME"]) / "thinking-policy.json"
            stored = json.loads(policy_path.read_text(encoding="utf-8"))
            entry = next(iter(stored.values()))
            self.assertEqual(entry["variant"], "qwen_chat_template_kwargs")
            self.assertTrue(entry["probed_at"])
            probed_calls = len(requests)
            self.assertGreater(probed_calls, 0)
            # 等第一个作业真正结束再发第二个：避免后台线程活过临时目录清理（teardown 竞态）。
            self.wait_structure_job(first.json()["job_id"])
            # 第二个作业命中缓存：不再发探测请求（只发结构作业自己的请求）。
            second = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers,
                                      json={"service_id": service["service_id"], "mode": "coarse"})
            if second.status_code == 200:
                self.wait_structure_job(second.json()["job_id"])
            self.assertNotIn("enable_thinking", [key for body in requests[probed_calls:] for key in body])

    def test_structure_job_applies_task_reasoning_mode(self) -> None:
        """端到端：结构作业的 reasoning_mode=high 让请求体真带 reasoning_effort=high。

        单元测试已覆盖 apply_reasoning_mode；本用例固定 api.py 的接线：任务级档位确实
        在 apply_thinking_policy 之前落到 provider，而不是被默认“关思考”改写。
        """
        import httpx
        from literature.providers import OpenAICompatibleProvider, ProviderConfig

        service = self.create_service(model="gpt-5-mini")
        requests: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text=_sse_reasons(0, "合同测试占位响应。"))

        def make_provider() -> OpenAICompatibleProvider:
            provider = OpenAICompatibleProvider(
                ProviderConfig(provider=service["service_id"], model="gpt-5-mini",
                               base_url="https://relay.example.com/v1"),
                api_key="test-key", timeout=5.0,
            )
            provider._client = httpx.Client(transport=httpx.MockTransport(handler))
            # structure.run 会调 with_read_timeout，而它会把 _client 置空重建、丢掉 MockTransport。
            provider.with_read_timeout = lambda seconds: provider  # type: ignore[method-assign]
            provider.max_retries = 0
            provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
            return provider

        with patch("literature.model_services.ModelServiceStore.config_for", lambda self, service_id, model="", **kw: make_provider()):
            created = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers,
                                      json={"service_id": service["service_id"], "mode": "coarse", "toc": [],
                                            "extract_toc": True, "toc_extraction": "model", "reasoning_mode": "high"})
            self.assertEqual(created.status_code, 200, created.text)
            job_id = created.json()["job_id"]
            job = None
            for _ in range(150):
                job = self.client.get(f"/projects/{self.project_id}/structure/jobs/{job_id}", headers=self.headers).json()
                if job["status"] in {"completed", "failed", "paused"}:
                    break
                time.sleep(0.05)
        self.assertTrue(requests, job)
        self.assertIn("high", [body.get("reasoning_effort") for body in requests])

    def test_task_can_override_model_within_provider(self) -> None:
        """任务级模型选择：显式 model 覆盖角色绑定；服务商可同时挂多个模型。"""
        import httpx
        from literature.providers import OpenAICompatibleProvider, ProviderConfig

        service = self.create_service(models=[
            {"model": "gpt-5-mini", "max_output_tokens": 8192},
            {"model": "gpt-5", "max_output_tokens": 16384},
        ])
        self.assertEqual([entry["model"] for entry in service["models"]], ["gpt-5-mini", "gpt-5"])
        # 第一个模型自动绑定为角色默认。
        listing = self.client.get("/model-services", headers=self.headers).json()
        self.assertEqual(listing["roles"]["structure"]["model"], "gpt-5-mini")
        resolved: list[str] = []
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_sse_reasons(0, "合同测试占位响应。"))

        def fake_config_for(self, service_id, model="", *, timeout=180.0):
            resolved.append(model)
            provider = OpenAICompatibleProvider(
                ProviderConfig(provider=service_id, model=model or "gpt-5-mini", base_url="https://relay.example.com/v1"),
                api_key="test-key", timeout=5.0,
            )
            provider._client = httpx.Client(transport=httpx.MockTransport(handler))
            provider.with_read_timeout = lambda seconds: provider  # type: ignore[method-assign]
            provider.max_retries = 0
            provider._retry_delay = lambda response, attempt: 0.0  # type: ignore[method-assign]
            return provider

        with patch("literature.model_services.ModelServiceStore.config_for", fake_config_for):
            created = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers,
                                       json={"service_id": service["service_id"], "model": "gpt-5", "mode": "coarse",
                                             "toc": [], "extract_toc": True, "toc_extraction": "model"})
            self.assertEqual(created.status_code, 200, created.text)
            job_id = created.json()["job_id"]
            for _ in range(150):
                job = self.client.get(f"/projects/{self.project_id}/structure/jobs/{job_id}", headers=self.headers).json()
                if job["status"] in {"completed", "failed", "paused"}:
                    break
                time.sleep(0.05)
        self.assertEqual(resolved, ["gpt-5"])
        self.assertIn("gpt-5", [body.get("model") for body in bodies])

    def test_model_services_crud_and_role_binding(self) -> None:
        self.assertIn("model_services_v1", self.client.get("/health").json()["capabilities"])
        self.assertIn("model_services_v3", self.client.get("/health").json()["capabilities"])
        service = self.create_service()
        self.assertTrue(service["service_id"])
        self.assertEqual(service["models"][0]["model"], "gpt-4o-mini")
        listing = self.client.get("/model-services", headers=self.headers).json()
        self.assertEqual(len(listing["services"]), 1)
        # 第一个服务商自动绑定到未设置的角色（绑到它的第一个模型），减少首次配置步骤。
        self.assertEqual(listing["roles"]["structure"], {"service_id": service["service_id"], "model": "gpt-4o-mini"})
        self.assertEqual(listing["roles"]["translation"], {"service_id": service["service_id"], "model": "gpt-4o-mini"})

        second = self.create_service(name="DeepSeek 官方", preset="deepseek")
        roles = self.client.put("/model-services/roles", headers=self.headers, json={
            "structure": {"service_id": second["service_id"], "model": "gpt-4o-mini"},
            "translation": service["service_id"],  # 兼容纯字符串（v1 形态）
        }).json()
        self.assertEqual(roles["structure"], {"service_id": second["service_id"], "model": "gpt-4o-mini"})
        self.assertEqual(roles["translation"]["service_id"], service["service_id"])

        # 服务商下的模型是可以整套替换的：改模型 ID 并追加第二个模型。
        updated = self.client.put(f"/model-services/{service['service_id']}", headers=self.headers, json={
            **{key: service[key] for key in ("name", "preset", "base_url")},
            "models": [
                {"model": "gpt-4o", "max_output_tokens": 8192},
                {"model": "gpt-4o-mini", "context_window": 128000},
            ],
        }).json()
        self.assertEqual([entry["model"] for entry in updated["models"]], ["gpt-4o", "gpt-4o-mini"])

        with patch("literature.model_services.ModelServiceStore.api_key", return_value="sk-test"):
            listing = self.client.get("/model-services", headers=self.headers).json()
            self.assertTrue(all(item["configured"] for item in listing["services"]))

        deleted = self.client.delete(f"/model-services/{service['service_id']}", headers=self.headers)
        self.assertEqual(deleted.status_code, 200)
        listing = self.client.get("/model-services", headers=self.headers).json()
        self.assertEqual(len(listing["services"]), 1)
        # 删除后引用它的角色被清空，避免悬空绑定。
        self.assertEqual(listing["roles"]["translation"], {"service_id": "", "model": ""})

    def test_v1_flat_config_migrates_and_merges_same_base_url(self) -> None:
        """旧配置里同一个 Base URL 的多条「服务」应合并成一个服务商、多个模型（对齐 Cindy）。"""
        path = Path(os.environ["LITERATURE_RUNTIME"]) / "model-services.json"
        path.write_text(json.dumps({
            "version": 1,
            "services": [
                {"service_id": "svc-1", "name": "yellowbee", "preset": "custom",
                 "base_url": "http://relay.example.com/v1", "model": "qwen3.8-flash-next",
                 "max_output_tokens": 4096, "reasoning_style": "qwen_thinking", "reasoning_effort": "off"},
                {"service_id": "svc-2", "name": "deepseek-v4-flash", "preset": "custom",
                 "base_url": "http://relay.example.com/v1", "model": "deepseek-v4-flash", "context_window": 1048576},
                {"service_id": "svc-3", "name": "other", "preset": "custom",
                 "base_url": "https://other.example.com/v1", "model": "gpt-5-mini"},
            ],
            "roles": {"structure": "svc-1", "translation": "svc-2"},
        }, ensure_ascii=False), encoding="utf-8")
        listing = self.client.get("/model-services", headers=self.headers).json()
        # 同 Base URL 的两条合并为一个服务商；不同 Base URL 的仍是独立服务商。
        self.assertEqual(len(listing["services"]), 2)
        merged = listing["services"][0]
        self.assertEqual(merged["service_id"], "svc-1")
        self.assertEqual([entry["model"] for entry in merged["models"]], ["qwen3.8-flash-next", "deepseek-v4-flash"])
        self.assertEqual(merged["models"][0]["reasoning_style"], "qwen_thinking")
        # v1 的 name 是服务名，不得当成模型显示名。
        self.assertEqual(merged["models"][0]["name"], "")
        # 角色保留到各自原来贡献的那个模型，而不是合并组的第一个。
        self.assertEqual(listing["roles"]["structure"], {"service_id": "svc-1", "model": "qwen3.8-flash-next"})
        self.assertEqual(listing["roles"]["translation"], {"service_id": "svc-1", "model": "deepseek-v4-flash"})
        # 读取即落盘为 v2，避免每次都重新合并。
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 2)

    def test_model_service_lists_remote_models(self) -> None:
        service = self.create_service()
        remote = [
            {"id": "gpt-4o", "context_window": 128000, "max_output_tokens": 16384,
             "input_price_per_million": 2.5, "output_price_per_million": 10.0},
            {"id": "gpt-4o-mini", "context_window": 128000, "max_output_tokens": 16384,
             "input_price_per_million": 0.15, "output_price_per_million": 0.6},
        ]
        with patch("literature.model_services.ModelServiceStore.api_key", return_value="sk-test"), \
                patch("literature.providers.list_remote_models", return_value=remote) as mocked:
            response = self.client.post(f"/model-services/{service['service_id']}/models", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"], remote)
        mocked.assert_called_once_with(service["base_url"], "sk-test", timeout=30.0)
        # 不存在的服务报错而不是静默返回空列表。
        missing = self.client.post("/model-services/not-a-service/models", headers=self.headers)
        self.assertEqual(missing.status_code, 502, missing.text)

    def test_probe_models_works_before_the_service_is_saved(self) -> None:
        """新建服务尚未保存时，仅凭 Base URL + API Key 也能拉取模型列表。"""
        with patch("literature.providers.list_remote_models", return_value=[
            {"id": "qwen3-max", "context_window": 262144, "max_output_tokens": 32768,
             "input_price_per_million": None, "output_price_per_million": None},
        ]) as mocked:
            response = self.client.post("/model-services/models", headers=self.headers, json={
                "base_url": "https://relay.example.com/v1", "api_key": "sk-typed",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"][0]["id"], "qwen3-max")
        mocked.assert_called_once_with("https://relay.example.com/v1", "sk-typed", timeout=30.0)

    def test_parses_context_and_pricing_from_openrouter_style_payload(self) -> None:
        """OpenRouter 风格字段（context_length / top_provider / 每 token 单价）应被收敛成元数据。"""
        from literature.providers import _model_metadata
        entry = _model_metadata({
            "id": "deepseek/deepseek-chat",
            "context_length": 163840,
            "top_provider": {"context_length": 163840, "max_completion_tokens": 8192},
            "pricing": {"prompt": "0.00000027", "completion": "0.0000011"},
        })
        self.assertEqual(entry["context_window"], 163840)
        self.assertEqual(entry["max_output_tokens"], 8192)
        self.assertAlmostEqual(entry["input_price_per_million"], 0.27)
        self.assertAlmostEqual(entry["output_price_per_million"], 1.1)

    def test_task_reasoning_mode_overrides_service_config(self) -> None:
        """任务级档位：off 强制关思考；low/medium/high 在开关型协议下等于开启。"""
        from literature.providers import ProviderConfig, apply_reasoning_mode
        config = ProviderConfig(provider="svc", model="qwen3.8-flash-next", base_url="https://relay.example.com/v1")
        self.assertIs(apply_reasoning_mode(config, "inherit"), config)
        off = apply_reasoning_mode(config, "off")
        self.assertEqual(off.reasoning_style, "qwen_thinking")
        self.assertEqual(off.reasoning_effort, "off")
        high = apply_reasoning_mode(config, "high", budget=4096)
        self.assertEqual(high.reasoning_style, "qwen_thinking")
        self.assertEqual(high.reasoning_effort, "on")
        self.assertEqual(high.reasoning_budget, 4096)
        # openai_effort 协议直接映射档位名。
        gpt = ProviderConfig(provider="svc", model="gpt-5-mini", base_url="https://api.openai.com/v1")
        self.assertEqual(apply_reasoning_mode(gpt, "medium").reasoning_effort, "medium")
        # 推断不出协议时保持原样（不发未声明字段）。
        unknown = ProviderConfig(provider="svc", model="mystery-1", base_url="https://relay.example.com/v1")
        self.assertIs(apply_reasoning_mode(unknown, "high"), unknown)

    def test_explicit_low_medium_effort_is_not_forced_off(self) -> None:
        """回归：openai_effort 下 low/medium 是“有意开启”，不得被默认关思考改写为 minimal。"""
        from literature.providers import OpenAICompatibleProvider, ProviderConfig, apply_reasoning_mode, reasoning_request_fields
        for effort in ("low", "medium", "high"):
            config = ProviderConfig(
                provider="svc", model="gpt-5-mini", base_url="https://api.openai.com/v1",
                reasoning_style="openai_effort", reasoning_effort=effort,
            )
            request_fields = reasoning_request_fields(OpenAICompatibleProvider(config, api_key="k").with_thinking_disabled().config)
            self.assertEqual(request_fields.get("reasoning_effort"), effort)
        # 任务级 low/medium 同样不被默认关思考改写。
        task = apply_reasoning_mode(ProviderConfig(provider="svc", model="gpt-5-mini", base_url="https://api.openai.com/v1"), "medium")
        request_fields = reasoning_request_fields(OpenAICompatibleProvider(task, api_key="k").with_thinking_disabled().config)
        self.assertEqual(request_fields.get("reasoning_effort"), "medium")

    def test_translation_job_accepts_task_reasoning_mode(self) -> None:
        """作业请求体接受 reasoning_mode / reasoning_budget，并冻结进任务快照。"""
        from literature.providers import MockProvider
        service = self.create_service(model="qwen3.8-flash-next")
        with patch("literature.model_services.ModelServiceStore.config_for", return_value=MockProvider()):
            response = self.client.post(f"/projects/{self.project_id}/translation/jobs", headers=self.headers, json={
                "service_id": service["service_id"], "scope": "book", "reasoning_mode": "high", "reasoning_budget": 2048,
            })
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            self.assertEqual(job["config"]["reasoning_mode"], "high")
            self.assertEqual(job["config"]["reasoning_budget"], 2048)
            # 等作业真正结束再退出，避免后台线程活过临时目录清理（teardown 竞态）。
            deadline = time.time() + 10
            while time.time() < deadline:
                jobs = self.client.get(f"/projects/{self.project_id}/translation/jobs", headers=self.headers).json()
                if any(item["job_id"] == job["job_id"] and item["status"] in {"completed", "failed", "partial_failed", "paused"} for item in jobs):
                    break
                time.sleep(0.05)

    def test_structure_plan_includes_toc_page_text_only_when_requested(self) -> None:
        plan = self.client.post(f"/projects/{self.project_id}/structure/plan", headers=self.headers, json={
            "toc": [], "extract_toc": True, "proofread": True,
        }).json()
        self.assertTrue(plan["toc_pages"])
        self.assertIn("Contents", json.dumps(plan["toc_pages"], ensure_ascii=False))
        self.assertTrue(plan["options"]["extract_toc"])

        without = self.client.post(f"/projects/{self.project_id}/structure/plan", headers=self.headers, json={
            "toc": [], "extract_toc": False, "proofread": True,
        }).json()
        self.assertEqual(without["toc_pages"], [])

    def test_structure_job_runs_in_background_with_activity(self) -> None:
        service = self.create_service()
        provider = StructureMockProvider()
        with patch("literature.model_services.ModelServiceStore.config_for", return_value=provider):
            created = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers, json={
                "service_id": service["service_id"], "toc": [], "extract_toc": True, "proofread": True, "auto_apply": True,
                "proofread_full": True, "toc_extraction": "model",
            })
            self.assertEqual(created.status_code, 200, created.text)
            job_id = created.json()["job_id"]
            job = None
            for _ in range(100):
                job = self.client.get(f"/projects/{self.project_id}/structure/jobs/{job_id}", headers=self.headers).json()
                if job["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.05)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "completed", job)
        self.assertTrue(job["activity"])
        self.assertEqual(len(job["result"]["proposed_toc"]), 2)
        self.assertTrue(job["result"]["audit"]["toc_page_text_included"])
        self.assertFalse(job["result"]["audit"]["body_text_included"])
        self.assertEqual(job["scope"]["extract_toc"], True)
        # Matching the printed TOC does not establish the whole hierarchy.
        # toc_extraction=model 显式要求模型核对目录，所以仍是 TOC / outline / role review 三次；
        # 默认 auto 在本地有显式目录标记时不再发 TOC 请求（见 test_structure_calibration）。
        self.assertEqual(len(provider.calls), 3)  # TOC, outline plan, role review

    def wait_structure_job(self, job_id: str) -> dict:
        job = None
        for _ in range(150):
            job = self.client.get(f"/projects/{self.project_id}/structure/jobs/{job_id}", headers=self.headers).json()
            if job["status"] in {"completed", "failed", "paused"}:
                break
            time.sleep(0.05)
        return job

    def test_structure_mode_resolves_preset_and_auto_applies(self) -> None:
        service = self.create_service()
        provider = StructureMockProvider()
        with patch("literature.model_services.ModelServiceStore.config_for", return_value=provider):
            created = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers, json={
                "service_id": service["service_id"], "mode": "coarse",
            })
            self.assertEqual(created.status_code, 200, created.text)
            job_id = created.json()["job_id"]
            job = self.wait_structure_job(job_id)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["scope"]["mode"], "coarse")
        self.assertEqual(job["config"]["mode"], "coarse")
        self.assertTrue(job["config"]["auto_apply"])
        self.assertEqual(job["config"]["proofread_mode"], "off")
        self.assertEqual(job["result"]["mode"], "coarse")
        self.assertEqual(job["result"]["calibration"]["mode"], "coarse")
        self.assertIn("categories", job["result"])
        # 全部自动应用后不应留下待确认建议。
        pending = self.client.get(f"/projects/{self.project_id}/structure/suggestions?decision=pending", headers=self.headers).json()
        self.assertEqual(pending, [])

    def test_structure_mode_adopts_confirmed_baseline(self) -> None:
        service = self.create_service()
        baseline = [{"title": "Chapter One", "level": 1}, {"title": "Chapter Two", "level": 1}]
        saved = self.client.put(f"/projects/{self.project_id}/structure/baseline", headers=self.headers, json={"toc": baseline, "source": "confirmed"})
        self.assertEqual(saved.status_code, 200, saved.text)
        provider = StructureMockProvider()
        with patch("literature.model_services.ModelServiceStore.config_for", return_value=provider):
            created = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers, json={
                "service_id": service["service_id"], "mode": "coarse",
            })
            job = self.wait_structure_job(created.json()["job_id"])
        self.assertEqual(job["status"], "completed", job)
        # 主流程只传 mode；后端自动采用项目已确认目录，不再重跑目录提取。
        self.assertEqual(job["config"]["toc"], baseline)
        self.assertFalse(job["config"]["extract_toc"])
        self.assertEqual(job["result"]["baseline_source"], "confirmed")
        self.assertFalse(job["result"]["audit"]["toc_page_text_included"])

    def test_structure_mode_honors_explicit_top_level_overrides(self) -> None:
        # 前端把“高级选项里被显式改动”的字段发在顶层（不是 advanced 容器）；
        # 这些必须覆盖档位预设，否则高级选项会静默失效。
        service = self.create_service()
        provider = StructureMockProvider()
        with patch("literature.model_services.ModelServiceStore.config_for", return_value=provider):
            created = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers, json={
                "service_id": service["service_id"], "mode": "coarse",
                "proofread_mode": "full", "planning_mode": "adaptive", "proofread_concurrency": 3,
                "toc_extraction": "model",
            })
            self.assertEqual(created.status_code, 200, created.text)
            job = self.wait_structure_job(created.json()["job_id"])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["config"]["mode"], "coarse")
        self.assertEqual(job["config"]["proofread_mode"], "full")
        self.assertEqual(job["config"]["planning_mode"], "adaptive")
        self.assertEqual(job["config"]["proofread_concurrency"], 3)
        self.assertEqual(job["config"]["toc_extraction"], "model")

    def test_structure_resume_route_reconstructs_paused_config(self) -> None:
        service = self.create_service()
        provider = StructureMockProvider()
        with patch("literature.model_services.ModelServiceStore.config_for", return_value=provider):
            created = self.client.post(f"/projects/{self.project_id}/structure/jobs", headers=self.headers, json={
                "service_id": service["service_id"], "mode": "fine",
            })
            job_id = created.json()["job_id"]
            self.wait_structure_job(job_id)
            # 把已完成作业改回 paused，验证继续路由会重建配置并启动新作业。
            from literature.storage import ProjectStore
            ProjectStore(self.root / "library").update_job(self.project_id, job_id, status="paused")
            resumed = self.client.post(f"/projects/{self.project_id}/structure/jobs/{job_id}/resume", headers=self.headers)
            self.assertEqual(resumed.status_code, 200, resumed.text)
            new_id = resumed.json()["job_id"]
            self.assertNotEqual(new_id, job_id)
            job = self.wait_structure_job(new_id)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["config"]["mode"], "fine")
        self.assertEqual(job["result"]["mode"], "fine")


if __name__ == "__main__":
    unittest.main()
