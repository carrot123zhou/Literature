"""折叠 / 对话的**真实路由**验收：三态角色语义必须一致（受控 provider，不调外部模型）。

冻结契约：
- `unset`（配置里从没写过）→ 继承 translation 的绑定；
- `cleared` / `invalid` → 提交期 400，**不**启动作业、**不**回落到 translation；
- 显式 `service_id/model` → 优先，且同名模型按 `service_id` 区分（不跨服务串味）；
- 存储的 `binding.model` 允许为空（= 未指定）；真正发请求时用该服务第一个模型，并标 `model_source=first_model`。

用 fake provider 记录 `provider_from(service_id, model)` 的实参，验证“实际用了哪家、哪个模型”。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from literature.models import Block, Document, Page  # noqa: E402
from literature.storage import ProjectStore  # noqa: E402

SERVICES = [
    {"service_id": "svc-a", "name": "A", "preset": "custom", "base_url": "https://a.invalid",
     "allow_plaintext_http": False,
     "models": [{"model": "shared-model", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2},
                {"model": "model-a2", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2}]},
    {"service_id": "svc-t", "name": "T", "preset": "custom", "base_url": "https://t.invalid",
     "allow_plaintext_http": False,
     "models": [{"model": "shared-model", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2}]},
]


class FakeProvider:
    config = type("C", (), {"model": "fake", "max_output_tokens": 16, "reasoning_effort": "",
                            "reasoning_style": "", "reasoning_budget": 0, "provider": "fake"})()

    def __init__(self, calls: list, service_id: str, model: str) -> None:
        self.calls = calls
        self.service_id = service_id
        self.model = model

    def complete(self, *, system: str, user: str):  # noqa: ANN201
        from literature.providers import ProviderResponse
        self.calls.append((self.service_id, self.model))
        payload = {"section": {"takeaway": "t", "topic": "t"},
                   "units": [{"id": "U0", "evidence": [0], "takeaway": "k", "reason": "r", "topic": "k", "role": "claim"}]}
        return ProviderResponse(content=json.dumps(payload), provider="fake", model="fake-1",
                                request_id="r", input_tokens=1, output_tokens=1, estimated_cost=0.0)

    def close(self) -> None:
        pass

    # 折叠/问答路径会调用的辅助方法
    def with_thinking_disabled(self):
        return self

    def with_read_timeout(self, seconds):
        return self


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="role-routing-"))
    runtime, library = root / "runtime", root / "library"
    runtime.mkdir(parents=True, exist_ok=True)
    library.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ.update(LITERATURE_RUNTIME=str(runtime), LITERATURE_LIBRARY=str(library), LITERATURE_SESSION_TOKEN="")

    from fastapi.testclient import TestClient

    from literature.api import create_app

    store = ProjectStore(library)
    src = root / "o.pdf"
    src.write_bytes(b"%PDF-1.4\n% role routing")
    project = store.create_project(src, title="role routing")
    pid = str(project["project_id"])
    store.save_document(Document(
        project_id=pid, title="role routing", source_language="en", page_count=1,
        pages=[Page(page_index=0, width=600, height=800)],
        blocks=[Block(block_id="b1", type="paragraph", order=0, page_index=0,
                      source_text="One sentence for routing. A second one follows here.")],
    ))
    client = TestClient(create_app(store))
    headers = {"x-literature-token": ""}
    manager = client.app.state.job_manager
    calls: list[tuple[str, str]] = []
    manager.provider_from = lambda service_id, provider_input=None, model="": FakeProvider(calls, service_id, model)

    def config(reader_fold=None, reader_chat=None) -> None:
        roles = {"structure": {"service_id": "svc-a", "model": "shared-model"},
                 "translation": {"service_id": "svc-t", "model": "shared-model"}}
        if reader_fold is not None:
            roles["reader_fold"] = reader_fold
        if reader_chat is not None:
            roles["reader_chat"] = reader_chat
        (runtime / "model-services.json").write_text(
            json.dumps({"version": 2, "services": SERVICES, "roles": roles}), encoding="utf-8")

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def run_fold(body: dict | None = None) -> tuple[int, dict]:
        calls.clear()
        response = client.post(f"/projects/{pid}/reader/layers", headers=headers,
                               json={"locale": "source", "mode": "auto", "force": True, **(body or {})})
        if response.status_code != 200:
            return response.status_code, response.json()
        deadline = time.time() + 10
        while time.time() < deadline:
            job = client.get(f"/projects/{pid}/reader/state", headers=headers).json().get("layer_job") or {}
            if job.get("status") in ("completed", "failed"):
                return 200, job
            time.sleep(0.05)
        return 200, {}

    # 1) unset → 继承 translation（svc-t），不落到 structure（svc-a）
    config()
    status, job = run_fold()
    check("unset：折叠继承 translation 服务（svc-t）",
          status == 200 and calls and calls[0][0] == "svc-t", f"status={status} calls={calls[:2]}")

    # 2) 显式 service/model 优先，同名模型按 service 区分
    config()
    status, job = run_fold({"service_id": "svc-a", "model": "shared-model"})
    check("显式：折叠走 svc-a/shared-model（不串到 svc-t）",
          status == 200 and calls and calls[0] == ("svc-a", "shared-model"), f"status={status} calls={calls[:2]}")

    # 3) cleared → 400，且不产生任何模型调用
    config(reader_fold={"service_id": "", "model": ""})
    status, payload = run_fold()
    check("cleared：折叠 400 且不回落 translation", status == 400 and not calls, f"status={status} calls={calls} {payload}")

    # 4) invalid → 400，且不产生任何模型调用
    config(reader_fold={"service_id": "svc-gone", "model": "m"})
    status, payload = run_fold()
    check("invalid：折叠 400 且不回落 translation", status == 400 and not calls, f"status={status} calls={calls} {payload}")

    # 5) bound 但未指定模型 → 存空、实际用该服务第一个（并标记 first_model）
    config(reader_fold={"service_id": "svc-a", "model": ""})
    status, job = run_fold()
    states = client.get("/model-services/roles/state", headers=headers).json()["roles"]
    check("未指定模型：binding 保留空、effective 用首模型、model_source=first_model",
          states["reader_fold"]["binding"]["model"] == ""
          and states["reader_fold"]["effective"]["model"] == "shared-model"
          and states["reader_fold"]["model_source"] == "first_model",
          json.dumps(states["reader_fold"], ensure_ascii=False))
    check("未指定模型：真实请求用的是首模型", status == 200 and calls and calls[0] == ("svc-a", "shared-model"),
          f"status={status} calls={calls[:2]}")

    # 6) PUT roles 保存空 model 不会被“首模型”覆盖
    saved = client.put("/model-services/roles", headers=headers,
                       json={"reader_chat": {"service_id": "svc-a", "model": ""}})
    check("PUT roles 保留空 model（不写成首模型）",
          saved.status_code == 200 and saved.json().get("reader_chat", {}).get("model") == "",
          json.dumps(saved.json().get("reader_chat"), ensure_ascii=False))

    # 7) 错误包裹 {roles:{…}} 被显式拒绝（422 且文案可读）
    wrapped = client.put("/model-services/roles", headers=headers, json={"roles": {"reader_chat": {"service_id": "svc-a", "model": ""}}})
    detail = json.dumps(wrapped.json(), ensure_ascii=False)
    check("PUT roles 拒绝 {roles:{…}} 包裹并说明正确形状",
          wrapped.status_code == 422 and ("roles" in detail and "直接传" in detail), detail[:200])

    for name, ok, text in checks:
        print(f'{"PASS" if ok else "FAIL"}: {name}' + (f" — {text}" if (text and not ok) else ""))
    ok_all = all(ok for _, ok, _ in checks)
    print("PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
