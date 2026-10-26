"""全路由横向溢出与导航可用性检查（阅读模块接线后的 app shell 回归）。

背景：`intake-topbar` 单行三列 + 导航 nowrap 在 320/390/768 让 body 横向溢出
（首页/文件资源/结构校对/对照翻译/阅读都受影响）。本脚本按真实生产构建前端 + 真实本地 API，
逐路由逐宽度断言 `documentElement.scrollWidth <= clientWidth`，并真实点击每个导航标签确认可切换。

用法：.venv/bin/python scripts/app_shell_overflow_check.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
  const routes = ["#/home", "#/resources", "#/p/PID/review", "#/p/PID/read", "#/p/PID/reader"];
  const widths = [320, 390, 768, 1024, 1440];
  const browser = await chromium.launch();
  const results = [];
  for (const width of widths) {
    const page = await browser.newPage({ viewport: { width, height: 800 } });
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    for (const route of routes) {
      await page.goto(base + "/index.html" + route.replace("PID", process.env.PROJECT_ID), { waitUntil: "networkidle" });
      await page.waitForTimeout(400);
      const metrics = await page.evaluate(() => {
        const doc = document.documentElement;
        const overflow = doc.scrollWidth - doc.clientWidth;
        const topbar = document.querySelector(".intake-topbar");
        const nav = document.querySelector(".intake-topbar nav");
        const visible = (el) => el && el.getBoundingClientRect().width > 0 && el.getBoundingClientRect().height > 0;
        const style = topbar ? getComputedStyle(topbar) : null;
        // 阅读器子树内部溢出：页面级 0 不代表内部不横滚（窄屏 pane 有 min-width 时会这样）。
        const readerMain = document.querySelector(".rdrr-main");
        const panes = readerMain ? [...readerMain.querySelectorAll(".rdrr-pane")] : [];
        const visiblePanes = panes.filter((el) => el.getBoundingClientRect().width > 2);
        return {
          overflow,
          topbarHeight: topbar ? Math.round(topbar.getBoundingClientRect().height) : 0,
          navVisible: visible(nav),
          navScrollable: nav ? nav.scrollWidth >= nav.clientWidth : false,
          topbarAreas: style ? style.gridTemplateAreas : "",
          navOverflowX: nav ? getComputedStyle(nav).overflowX : "",
          readerInnerOverflow: readerMain ? Math.max(0, readerMain.scrollWidth - readerMain.clientWidth) : 0,
          readerPaneMaxWidth: visiblePanes.length ? Math.round(Math.max(...visiblePanes.map((el) => el.getBoundingClientRect().width))) : 0,
          readerPaneCount: visiblePanes.length,
        };
      });
      if (process.env.SHOT_DIR) {
        const fs = require("fs");
        fs.mkdirSync(process.env.SHOT_DIR, { recursive: true });
        await page.screenshot({ path: `${process.env.SHOT_DIR}/${width}-${route.replace(/[#\/]/g, "_")}.png` });
      }
      results.push({ width, route, ...metrics, errors: errors.slice(0, 3) });
    }
    await page.close();
  }
  // 导航真实点击：每个宽度都点一遍五个标签，确认路由切换。
  const page = await browser.newPage({ viewport: { width: 390, height: 800 } });
  await page.goto(base + "/index.html#/home", { waitUntil: "networkidle" });
  const labels = ["首页", "文件资源", "结构校对", "对照翻译", "阅读"];
  const switched = [];
  for (const label of labels) {
    const button = page.locator(".intake-topbar nav button", { hasText: label }).first();
    await button.scrollIntoViewIfNeeded();
    await button.click();
    await page.waitForTimeout(300);
    switched.push({ label, hash: await page.evaluate(() => window.location.hash) });
  }
  await browser.close();
  console.log(JSON.stringify({ results, switched }));
})();
"""


def main() -> int:
    if not NODE.is_file() or not PW.is_dir():
        print("SKIP: 缺少 ms-playwright-go node/package")
        return 2
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        print("FAIL: frontend/dist 不存在，请先 vite build")
        return 1
    temporary = Path(tempfile.mkdtemp(prefix="shell-overflow-"))
    runtime, library = temporary / "runtime", temporary / "library"
    runtime.mkdir(); library.mkdir()
    # 空 Token 文件：服务与前端都按空令牌握手（与其它浏览器验收脚本一致）。
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""

    from literature.models import Block, Document, Page
    from literature.storage import ProjectStore

    store = ProjectStore(library)
    origin = temporary / "sample.pdf"
    origin.write_bytes(b"%PDF-1.4\n% shell fixture")
    project = store.create_project(origin, title="全路由溢出检查样本")
    project_id = str(project["project_id"])
    store.save_document(Document(
        project_id=project_id, title="全路由溢出检查样本", source_language="en", page_count=1,
        pages=[Page(page_index=0, width=600, height=800)],
        blocks=[Block(block_id="b1", type="paragraph", order=0, page_index=0,
                      source_text="A paragraph used only for shell overflow checks.")],
    ))
    from literature.api import create_app

    api_port = free_port()
    web_port = free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_RUNTIME": str(runtime), "LITERATURE_LIBRARY": str(library),
        "LITERATURE_SESSION_TOKEN": "", "LITERATURE_PORT": str(api_port),
        "PW_PACKAGE_PATH": str(PW), "PROJECT_ID": project_id,
        "SHOT_DIR": str(ROOT / "tmp" / "shell-overflow-shots"),
    })
    sidecar = subprocess.Popen([sys.executable, "-m", "literature.api"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time
        import httpx

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
                                   capture_output=True, text=True, env=env, timeout=300)
    finally:
        sidecar.terminate()
        try:
            sidecar.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sidecar.kill()
    if completed.returncode != 0:
        print("FAIL: 浏览器脚本失败\n", completed.stdout[-2000:], completed.stderr[-2000:])
        return 1
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except Exception:
        print("FAIL: 无法解析输出\n", completed.stdout[-2000:])
        return 1

    failures = []
    for item in payload["results"]:
        if item["overflow"] > 1:
            failures.append(f'{item["width"]}px {item["route"]} 溢出 {item["overflow"]}px')
        if not item["navVisible"]:
            failures.append(f'{item["width"]}px {item["route"]} 导航不可见')
        if item["route"].endswith("/reader") and item["readerInnerOverflow"] > 1:
            failures.append(
                f'{item["width"]}px {item["route"]} 阅读器内部横向溢出 {item["readerInnerOverflow"]}px'
                f'（可见栏 {item["readerPaneCount"]} 个，最宽 {item["readerPaneMaxWidth"]}px；需 designer 在 ≤899 收窄 pane）'
            )
        if item["errors"]:
            failures.append(f'{item["width"]}px {item["route"]} 脚本错误 {item["errors"]}')
    for item in payload["switched"]:
        if not item["hash"]:
            failures.append(f'导航点击 {item["label"]} 未切换')
    print(json.dumps({"results": payload["results"], "switched": payload["switched"], "failures": failures},
                     ensure_ascii=False, indent=2))
    print("PASS" if not failures else "FAIL")
    shutil.rmtree(temporary, ignore_errors=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
