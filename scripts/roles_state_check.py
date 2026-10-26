"""全局角色真状态 DTO 验收（不依赖项目、不输出密钥）。

约定（2026-10-25 冻结）：
- `GET /model-services` 向后兼容追加 `role_states`；
- `GET /model-services/roles/state` → `{"roles": {...}}`，两个端点同构；
- 每个角色：`{role, origin, source, binding, effective, model_source, problem}`
  - `origin`: bound | unset | cleared | invalid（配置里到底有没有、是否失效）
  - `source`: role | inherited_translation | unset | cleared | invalid
    （**只有阅读角色且 origin=unset** 才回退 translation；cleared/invalid 绝不继承）
  - `binding`: 配置里真实存的绑定；`effective`: 实际会用（未指定模型时按服务第一个，`model_source=first_model`）
- `PUT /model-services/roles` 必须能保存 reader_fold / reader_chat。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(REPO))


def build(store_root: Path, roles: dict) -> None:
    """只写模型配置；session-token 在 create_app 之前由 main() 建好（否则会被随机令牌覆盖）。"""
    runtime = store_root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = {
        "version": 2,
        "services": [{
            "service_id": "svc-a", "name": "svc A", "preset": "custom",
            "base_url": "https://a.invalid", "allow_plaintext_http": False,
            "models": [
                {"model": "model-a1", "name": "模型 A1", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2},
                {"model": "model-a2", "name": "模型 A2", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2},
            ],
        }],
        "roles": roles,
    }
    (runtime / "model-services.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="roles-state-"))
    (root / "runtime").mkdir(parents=True, exist_ok=True)
    (root / "runtime" / "session-token").write_text("", encoding="utf-8")
    os.environ.update(LITERATURE_RUNTIME=str(root / "runtime"), LITERATURE_LIBRARY=str(root / "library"),
                      LITERATURE_SESSION_TOKEN="")
    from fastapi.testclient import TestClient

    from literature.api import create_app
    from literature.storage import ProjectStore

    client = TestClient(create_app(ProjectStore(root / "library")))
    headers = {"x-literature-token": ""}
    legacy = {"structure": {"service_id": "svc-a", "model": "model-a1"},
              "translation": {"service_id": "svc-a", "model": "model-a1"}}
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    build(root, legacy)
    payload = client.get("/model-services", headers=headers).json()
    states = payload.get("role_states") or {}
    check("GET /model-services 含 role_states（四角色）",
          set(states) == {"structure", "translation", "reader_fold", "reader_chat"}, ",".join(sorted(states)))
    check("旧配置 reader_fold 标为继承翻译默认",
          states["reader_fold"]["source"] == "inherited_translation"
          and states["reader_fold"]["binding"] == {"service_id": "", "model": ""}
          and states["reader_fold"]["effective"]["model"] == "model-a1",
          json.dumps(states["reader_fold"], ensure_ascii=False))
    check("structure 不继承（source=role）", states["structure"]["source"] == "role", states["structure"]["source"])
    check("响应不含密钥字段", "api_key" not in json.dumps(payload).lower() and '"key"' not in json.dumps(payload), "")

    build(root, {**legacy, "reader_chat": {"service_id": "", "model": ""}})
    states = client.get("/model-services/roles/state", headers=headers).json()["roles"]
    check("显式清空 → source=cleared 且 effective 为空（不继承）",
          states["reader_chat"]["source"] == "cleared"
          and states["reader_chat"]["effective"] == {"service_id": "", "model": ""},
          json.dumps(states["reader_chat"], ensure_ascii=False))

    build(root, {**legacy, "reader_fold": {"service_id": "svc-gone", "model": "m"}})
    states = client.get("/model-services/roles/state", headers=headers).json()["roles"]
    check("服务已删除 → source=invalid 且保留原 binding",
          states["reader_fold"]["source"] == "invalid"
          and states["reader_fold"]["binding"] == {"service_id": "svc-gone", "model": "m"},
          json.dumps(states["reader_fold"], ensure_ascii=False))

    build(root, {**legacy, "reader_fold": {"service_id": "svc-a", "model": ""}})
    states = client.get("/model-services/roles/state", headers=headers).json()["roles"]
    check("绑定服务未指定模型 → model_source=first_model 且 effective 补首模型",
          states["reader_fold"]["model_source"] == "first_model"
          and states["reader_fold"]["effective"] == {"service_id": "svc-a", "model": "model-a1"}
          and states["reader_fold"]["binding"]["model"] == "",
          json.dumps(states["reader_fold"], ensure_ascii=False))

    build(root, legacy)
    saved = client.put("/model-services/roles", headers=headers,
                       json={"reader_chat": {"service_id": "svc-a", "model": "model-a2"}})
    check("PUT /model-services/roles 能保存 reader_chat",
          saved.status_code == 200 and saved.json().get("reader_chat", {}).get("model") == "model-a2",
          json.dumps(saved.json().get("reader_chat"), ensure_ascii=False))

    for name, ok, detail in checks:
        print(f'{"PASS" if ok else "FAIL"}: {name}' + (f" — {detail}" if (detail and not ok) else ""))
    ok_all = all(ok for _, ok, _ in checks)
    print("PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
