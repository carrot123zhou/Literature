"""章级 AI 折叠 UI 端到端（生产 dist + 真隔离 API + 受控 provider）。

用户最高优先级路径：正常 App 中选「当前章」→ 点 AI 按钮 → 捕获唯一 target-locale POST
且 section_ids 精确 → source prerequisite fold + target 仅短请求 → 两栏各层实物/
derived 标签/证据回 source → 生成后入口常驻 → stop/partial 重试 → 刷新 0 provider 调用。

用法：.venv/bin/python tests/manual/reader_chapter_ai_check.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.manual.reader_ui_browser_check import make_real_pdf, seed_project  # noqa: E402
from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web  # noqa: E402


def main() -> int:
    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    runtime = base / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ["LITERATURE_LIBRARY"] = str(base / "library")
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""

    store, project_id = seed_project(base)
    projects = store.list_projects()
    second_id = next(item["project_id"] for item in projects if item["project_id"] != project_id)

    import uvicorn

    from literature.api import create_app
    from literature.providers import ProviderResponse

    app = create_app(store)
    manager = app.state.job_manager
    state_box = {"routes": [], "delay": 0.3, "fail_markers": set(), "failed_markers": set(),
                 "complete_calls": 0, "timeline": []}

    class FakeChat:
        def __init__(self, service_id: str, model: str) -> None:
            self._s, self._m = service_id, model
            self.config = type("C", (), {"model": model, "provider": service_id, "base_url": "https://a.invalid",
                                         "allow_plaintext_http": False, "reasoning_style": "", "reasoning_effort": "",
                                         "reasoning_budget": 0, "max_output_tokens": 4096, "temperature": 0.2,
                                         "input_price_per_million": 0, "output_price_per_million": 0})()

        def complete(self, *, system: str, user: str) -> ProviderResponse:
            state_box["complete_calls"] = state_box.get("complete_calls", 0) + 1
            state_box["timeline"].append({
                "call": state_box["complete_calls"],
                "is_derive": "do NOT translate" in user,
            })
            time.sleep(state_box["delay"])
            if "do NOT translate" in user:
                # derive 协议回复
                import re
                units = []
                current = None
                backing: list[str] = []
                for line in user.splitlines() + ["[END]"]:
                    m = re.match(r"^\[(\S+)\]", line)
                    if m:
                        if current:
                            units.append({**current, "ev_sentences": [f"译句{i}" for i in range(len(backing))]})
                        current = {"id": m.group(1), "takeaway": "译文要点", "reason": "译文理由。", "topic": "译文"}
                        backing = []
                        continue
                    bm = re.match(r"^\s*(\d+):\s+(.*)$", line)
                    if current is not None and bm:
                        backing.append(bm.group(2))
                if current:
                    units.append({**current, "ev_sentences": [f"译句{i}" for i in range(len(backing))]})
                import json
                content = json.dumps({"section": {"takeaway": "译文主线", "topic": "译文"}, "units": units},
                                     ensure_ascii=False)
            else:
                # source fold 协议回复：按提示里的单元编号生成语义式短层
                import json
                import re
                units = []
                current = None
                sentences: list[str] = []
                for line in user.splitlines() + ["[END]"]:
                    m = re.match(r"^\[(\S+)\]", line)
                    if m:
                        if current and sentences:
                            ranked = sorted(range(len(sentences)), key=lambda i: len(sentences[i]), reverse=True)
                            ev = sorted(ranked[:2])
                            core = sentences[ev[0]]
                            units.append({"id": current, "role": "claim", "takeaway": core.rstrip(".")[:80],
                                          "topic": " ".join(core.split()[:3]),
                                          "reason": "It establishes the passage claim.", "evidence": ev})
                        current = m.group(1)
                        sentences = []
                        continue
                    bm = re.match(r"^\s*(\d+):\s+(.*)$", line)
                    if current is not None and bm:
                        sentences.append(bm.group(2).strip())
                if current and sentences:
                    ranked = sorted(range(len(sentences)), key=lambda i: len(sentences[i]), reverse=True)
                    ev = sorted(ranked[:2])
                    core = sentences[ev[0]]
                    units.append({"id": current, "role": "claim", "takeaway": core.rstrip(".")[:80],
                                  "topic": " ".join(core.split()[:3]),
                                  "reason": "It establishes the passage claim.", "evidence": ev})
                content = json.dumps({"section": {"takeaway": "Section主线", "topic": "主线"},
                                      "units": units}, ensure_ascii=False)
            return ProviderResponse(content=content, provider=self._s, model=self._m,
                                    request_id="r", input_tokens=100, output_tokens=50, estimated_cost=0.0)

        def close(self) -> None:
            pass

    def provider_from(service_id: str, provider_input=None, model: str = "") -> FakeChat:
        state_box["routes"].append((service_id, model))
        return FakeChat(service_id, model or "model-c1")

    manager.provider_from = provider_from
    api_port, web_port = free_port(), free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.1)

    dist = prepare_dist(base, api_port)
    web = start_web(dist, web_port)

    import glob
    import json
    import subprocess

    packages = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/package")))
    nodes = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/node")))
    if not packages or not nodes:
        print(json.dumps({"checks": [], "error": "no ms-playwright-go node/package"}))
        return 2
    env = dict(os.environ)
    env["PW_PACKAGE_PATH"] = packages[-1]
    result = subprocess.run(
        [nodes[-1], str(ROOT / "tests/manual/reader_chapter_ai_check.cjs"),
         str(web_port), str(api_port), project_id, second_id,
         str(Path(tempfile.mkdtemp(prefix="chapter-ai-")))],
        capture_output=True, text=True, timeout=500, env=env,
    )
    provider_calls = 0
    for out_line in result.stdout.strip().splitlines():
        if out_line.startswith("provider_complete_calls: "):
            provider_calls = int(out_line.split(": ")[1])
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if line.startswith("{") and provider_calls:
        payload = json.loads(line)
        payload["provider_complete_calls"] = provider_calls
        line = json.dumps(payload, ensure_ascii=False)
    print(line or json.dumps({"checks": [], "error": f"no stdout; stderr={result.stderr[-600:]}"}))
    if not line:
        Path("/tmp/chapai-stderr.log").write_text(result.stderr or "")
    server.should_exit = True
    web.shutdown()
    temporary.cleanup()
    ok = False
    try:
        payload = json.loads(line)
        payload["provider_complete_calls"] = state_box.get("complete_calls", 0)
        payload["provider_timeline"] = state_box.get("timeline") or []
        line = json.dumps(payload, ensure_ascii=False)
        ok = bool(payload.get("checks")) and all(item["pass"] for item in payload["checks"])
    except Exception:  # noqa: BLE001
        ok = False
    print(json.dumps({"provider_complete_calls": state_box.get("complete_calls", 0),
                       "provider_timeline": state_box.get("timeline") or []}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
