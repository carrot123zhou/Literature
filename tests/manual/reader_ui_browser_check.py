"""阅读器（ReaderWorkspace）隔离真实浏览器验收。

与 engine_ui_browser_check 同一骨架：真实生产构建前端 + 真实本地 API + 隔离库/runtime。
不调任何外部模型：分层用「本地摘句」链路生成；对话未配模型时验证错误反馈而不是伪成功。

用法：.venv/bin/python tests/manual/reader_ui_browser_check.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web  # noqa: E402


def make_real_pdf(path: Path, pages: int = 6) -> None:
    """生成带可见文字的真 PDF（bbox 定位/原件渲染验收需要可解析页面）。"""
    import fitz

    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_text((60, 90), f"PAGE {index + 1}", fontsize=22)
        page.insert_text((60, 130), f"Paragraph one of page {index + 1} about silver trade routes.", fontsize=11)
        page.insert_text((60, 150), f"Paragraph two of page {index + 1}: assay data and mints.", fontsize=11)
    document.save(path)
    document.close()


def make_document(store, project_id: str, title: str) -> None:
    from literature.models import Block, Document, Page

    blocks = [
        Block(block_id="b-title-1", type="title", order=0, page_index=0, source_text="Introduction", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-p-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 120],
              source_text="Silver vessels travelled along the Silk Road. Workshops in Chang'an copied Sogdian shapes for decades."),
        Block(block_id="b-p-2", type="paragraph", order=2, page_index=0, bbox=[10, 130, 300, 200],
              source_text="Assay data show a consistent alloy of 92 percent silver."),
        Block(block_id="b-title-2", type="title", order=3, page_index=1, source_text="Conclusion", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-list", type="list", order=4, page_index=1, bbox=[10, 50, 300, 120],
              items=["Hoards cluster in port cities.", "Finds thin out inland."]),
        Block(block_id="b-p-3", type="paragraph", order=5, page_index=1, bbox=[10, 130, 300, 200],
              source_text="This paragraph has no translation and must fall back."),
    ]
    store.save_document(Document(
        project_id=project_id, title=title, source_language="en", target_language="zh-CN",
        page_count=2,
        pages=[Page(page_index=0, width=600, height=800), Page(page_index=1, width=600, height=800)],
        blocks=blocks,
    ))


def seed_project(root: Path) -> tuple[object, str]:
    from literature.models import Block, Document, Page, TranslationEntry, TranslationLayer
    from literature.storage import ProjectStore

    store = ProjectStore(root / "library")
    source = root / "reader-check.pdf"
    make_real_pdf(source, pages=6)
    project = store.create_project(source, title="阅读器验收样本")
    project_id = project["project_id"]
    make_document(store, project_id, "阅读器验收样本")
    blocks_unused = [
        Block(block_id="b-title-1", type="title", order=0, page_index=0, source_text="Introduction", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-p-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 120],
              source_text="Silver vessels travelled along the Silk Road. Workshops in Chang'an copied Sogdian shapes for decades."),
        Block(block_id="b-p-2", type="paragraph", order=2, page_index=0, bbox=[10, 130, 300, 200],
              source_text="Assay data show a consistent alloy of 92 percent silver."),
        Block(block_id="b-title-2", type="title", order=3, page_index=1, source_text="Conclusion", level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="b-list", type="list", order=4, page_index=1, bbox=[10, 50, 300, 120],
              items=["Hoards cluster in port cities.", "Finds thin out inland."]),
        Block(block_id="b-p-3", type="paragraph", order=5, page_index=1, bbox=[10, 130, 300, 200],
              source_text="This paragraph has no translation and must fall back."),
    ]
    store.save_translation(TranslationLayer(project_id=project_id, locale="zh-CN", entries={
        "b-title-1": TranslationEntry(block_id="b-title-1", text="导论", status="translated"),
        "b-p-1": TranslationEntry(block_id="b-p-1", text="银器沿丝绸之路流动。长安的工坊几十年里一直在模仿粟特器形。", status="translated"),
        "b-p-2": TranslationEntry(block_id="b-p-2", text="检测数据显示合金含银百分之九十二。", status="translated"),
        "b-title-2": TranslationEntry(block_id="b-title-2", text="结论", status="translated"),
        "b-list": TranslationEntry(block_id="b-list", items=["窖藏集中在港口城市。", "内陆发现变少。"], status="translated"),
    }))
    # 故意不预生成分层：由 UI 走真实的「本地摘句」折叠链路（无模型）。

    # 第二个项目（切项目竞态用）。
    source_b = root / "reader-check-b.pdf"
    make_real_pdf(source_b, pages=4)
    project_b = store.create_project(source_b, title="切换对照样本")
    make_document(store, project_b["project_id"], "切换对照样本")
    store.save_translation(TranslationLayer(project_id=project_b["project_id"], locale="zh-CN", entries={}))

    # 真实 model-services.json：reader_chat 绑定 svc-chat/model-c1（角色解析走真实
    # ModelServiceStore；Keychain/HTTP 由 main() 里的测试桩接管）。
    config = {
        "version": 2,
        "services": [
            {"service_id": "svc-chat", "name": "聊天服务A", "preset": "custom", "base_url": "https://a.invalid",
             "allow_plaintext_http": False,
             "models": [{"model": "model-c1", "name": "聊天模型一", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2},
                        {"model": "model-c2", "name": "聊天模型二", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2}]},
            {"service_id": "svc-other", "name": "聊天服务B", "preset": "custom", "base_url": "https://b.invalid",
             "allow_plaintext_http": False,
             "models": [{"model": "model-c1", "name": "同名模型", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2}]},
            {"service_id": "svc-keyless", "name": "未配密钥服务", "preset": "custom", "base_url": "https://c.invalid",
             "allow_plaintext_http": False,
             "models": [{"model": "model-k", "name": "无密钥模型", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2}]},
        ],
        "roles": {
            "structure": {}, "translation": {},
            "reader_fold": {"service_id": "svc-chat", "model": "model-c1"},
            "reader_chat": {"service_id": "svc-chat", "model": "model-c1"},
        },
    }
    (Path(root) / "runtime" / "model-services.json").write_text(json.dumps(config), encoding="utf-8")
    # 受控 picker 需要 configured=True：给两个测试服务写临时 Keychain 密钥（结束由 main 删除）。
    from literature.model_services import save_service_key
    save_service_key("svc-chat", "ui-test-key-a")
    save_service_key("svc-other", "ui-test-key-b")
    return store, project_id


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
    state_box = {"routes": [], "delay": 3.0, "fail_markers": {"RETRY_FAIL_TEST"}, "failed_markers": set()}

    class FakeChat:
        def __init__(self, service_id: str, model: str) -> None:
            self._service_id, self._model = service_id, model
            self.config = type("C", (), {"model": model, "provider": service_id, "base_url": "https://a.invalid",
                                         "allow_plaintext_http": False, "reasoning_style": "", "reasoning_effort": "",
                                         "reasoning_budget": 0, "max_output_tokens": 4096, "temperature": 0.2,
                                         "input_price_per_million": 0, "output_price_per_million": 0})()

        def complete(self, *, system: str, user: str) -> ProviderResponse:
            # 受控失败：问题里带标记时首次请求失败（重试/失败路径用），之后成功。
            for marker in state_box["fail_markers"]:
                if marker in user and marker not in state_box["failed_markers"]:
                    state_box["failed_markers"].add(marker)
                    from literature.providers import ProviderError

                    raise ProviderError(f"受控失败：{marker}")
            time.sleep(state_box["delay"])
            # 回答里带真实路由身份：前端/历史展示的答案是哪个 (service_id, model) 给的，可被断言。
            return ProviderResponse(
                content=f"routed:{self._service_id}/{self._model} 对照阅读测试回答 [#b-p-1]。",
                provider=self._service_id, model=self._model,
                request_id="r", input_tokens=10, output_tokens=5, estimated_cost=0.0,
            )

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
        [nodes[-1], str(ROOT / "tests/manual/reader_ui_check.cjs"),
         str(web_port), str(api_port), project_id, second_id,
         str(Path(tempfile.mkdtemp(prefix="reader-ui-shots-")) / "reader-ui.png")],
        capture_output=True, text=True, timeout=700, env=env,
    )
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    print(line or json.dumps({"checks": [], "error": f"no stdout; stderr={result.stderr[-800:]}"}))
    server.should_exit = True
    web.shutdown()
    from literature.model_services import delete_service_key
    for fake in ("svc-chat", "svc-other"):
        try:
            delete_service_key(fake)
        except Exception:  # noqa: BLE001 - 清理失败不影响结论
            pass
    temporary.cleanup()
    ok = False
    try:
        payload = json.loads(line)
        ok = bool(payload.get("checks")) and all(item["pass"] for item in payload["checks"])
    except Exception:  # noqa: BLE001
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
