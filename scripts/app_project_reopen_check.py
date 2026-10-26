"""App 接线：同一项目「解析/加载失败 → 就绪」后，点工作区标签必须重新拉取并能进入。

复现的真实缺陷（2026-10-25）：
- 项目刚导入、`document.json` 还没生成时打开工作区 → 读取失败，界面停在「无法打开任务」；
- 解析随后完成，用户点顶栏「对照翻译 / 阅读」标签（同一 projectId）→ 旧实现只改 hash，
  `activeProjectId` 未变 → 项目加载 effect 不重跑 → 一直停在上一次的错误/占位页。

本脚本用真实本地 API + 生产构建前端，在隔离库里：
1. 建项目但先不写 document.json；
2. 打开 `#/p/<id>/review`，断言出现错误/占位态；
3. 通过 API 写入 document.json；
4. 点顶栏「阅读」标签，断言阅读器真正渲染（`.rdrr-root` 且不是加载失败），且请求了 `/document`。
不调模型、不写生产库。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web  # noqa: E402

NODE = Path.home() / "Library/Caches/ms-playwright-go/1.57.0/node"
PW = Path.home() / "Library/Caches/ms-playwright-go/1.57.0/package"

CHECK_JS = r"""
const { chromium } = require(process.env.PW_PACKAGE_PATH);
(async () => {
  const base = process.argv[2];
  const pid = process.env.PROJECT_ID;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 860 } });
  const errors = [];
  const docRequests = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("request", (req) => { if (req.url().includes("/document")) docRequests.push(req.url()); });
  const results = [];
  const check = (name, pass, detail) => results.push({ name, pass: Boolean(pass), detail: detail || "" });

  await page.goto(base + "/index.html#/p/" + pid + "/review", { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  const beforeText = (await page.locator("body").innerText()).slice(0, 400);
  check("缺 document 时给出可见的失败/占位提示（不是白屏）",
    /无法打开任务|正在载入|选择一个已完成处理/.test(beforeText), beforeText.slice(0, 120));

  // 解析完成：写入 document.json
  const seed = await page.request.post("http://127.0.0.1:" + process.env.API_PORT + "/projects/" + pid + "/__seed_document", { headers: { "x-literature-token": "" } });

  const before = docRequests.length;
  await page.locator(".intake-topbar nav button", { hasText: "阅读" }).first().click();
  await page.waitForTimeout(1500);
  const after = docRequests.length;
  const hasReader = await page.locator("#rdrr-root").count();
  const readerText = hasReader ? (await page.locator("#rdrr-root").innerText()).slice(0, 300) : "";
  check("点「阅读」标签后重新请求 /document", after > before, `before=${before} after=${after} seed=${seed.status()}`);
  check("点「阅读」标签后真正进入阅读器（不是仍停在占位/错误）",
    hasReader === 1 && !/阅读层加载失败/.test(readerText), readerText.slice(0, 120));

  await browser.close();
  console.log(JSON.stringify({ results, errors }));
})();
"""


def main() -> int:
    if not NODE.is_file() or not PW.is_dir():
        print("SKIP: 缺少 ms-playwright-go node/package")
        return 2
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        print("FAIL: frontend/dist 不存在，请先 vite build")
        return 1

    temporary = Path(tempfile.mkdtemp(prefix="app-reopen-"))
    runtime, library = temporary / "runtime", temporary / "library"
    runtime.mkdir(); library.mkdir()
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ.update(LITERATURE_RUNTIME=str(runtime), LITERATURE_LIBRARY=str(library), LITERATURE_SESSION_TOKEN="")

    from literature.models import Block, Document, Page
    from literature.storage import ProjectStore

    store = ProjectStore(library)
    origin = temporary / "origin.pdf"
    origin.write_bytes(b"%PDF-1.4\n% reopen fixture")
    project = store.create_project(origin, title="解析后续打开验收")
    project_id = str(project["project_id"])

    # 补一个只在验收里存在的种子端点：真实产品没有它，这里用最小 FastAPI 包装同一个 store。
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from literature.api import create_app

    app = create_app(store)

    @app.post("/projects/{project_id}/__seed_document")
    def seed_document(project_id: str):  # noqa: ANN202 - 测试专用
        store.save_document(Document(
            project_id=project_id, title="解析后续打开验收", source_language="en", page_count=1,
            pages=[Page(page_index=0, width=600, height=800)],
            blocks=[Block(block_id="b1", type="paragraph", order=0, page_index=0,
                          source_text="A paragraph that appears only after parsing finishes.")],
        ))
        return {"seeded": True}

    api_port, web_port = free_port(), free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_RUNTIME": str(runtime), "LITERATURE_LIBRARY": str(library),
        "LITERATURE_SESSION_TOKEN": "", "LITERATURE_PORT": str(api_port),
        "PW_PACKAGE_PATH": str(PW), "PROJECT_ID": project_id, "API_PORT": str(api_port),
    })
    # 同进程起 uvicorn（带测试专用种子端点）；前端与 API 都使用空会话令牌。
    import threading
    import time

    import httpx
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{api_port}/health", timeout=1, trust_env=False)
                break
            except Exception:
                time.sleep(0.2)
        dist = prepare_dist(temporary, api_port)
        start_web(dist, web_port)
        script = temporary / "check.cjs"
        script.write_text(CHECK_JS, encoding="utf-8")
        completed = subprocess.run([str(NODE), str(script), f"http://127.0.0.1:{web_port}"],
                                   capture_output=True, text=True, env=env, timeout=240)
    finally:
        server.should_exit = True
    if completed.returncode != 0:
        print("FAIL: 浏览器脚本失败\n", completed.stdout[-2000:], completed.stderr[-2000:])
        return 1
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    for item in payload["results"]:
        print(f'{"PASS" if item["pass"] else "FAIL"}: {item["name"]} — {item["detail"]}')
    if payload.get("errors"):
        print("脚本错误：", payload["errors"][:3])
    ok = all(item["pass"] for item in payload["results"]) and not payload.get("errors")
    print("PASS" if ok else "FAIL")
    shutil.rmtree(temporary, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
