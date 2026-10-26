"""章节索引 / 当前章折叠范围验收（零分层、零模型调用）。

要证明的是：**没有任何分层、没调过模型**时，界面也能
① 列出目录、② 选中「当前章」（id 与折叠层同源 `S1/S2…`）、③ 只折这一章。

数据来自 `/reader/state.section_index`（core `build_sections` 的轻量投影），不是前端另造 id。
模型折叠请求会打到不可达的 fixture 端点（`https://a.invalid`），只断言请求体，不产生费用。
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
from tests.manual.reader_ui_browser_check import seed_project  # noqa: E402

NODE = Path.home() / "Library/Caches/ms-playwright-go/1.57.0/node"
PW = Path.home() / "Library/Caches/ms-playwright-go/1.57.0/package"

CHECK_JS = r"""
const { chromium } = require(process.env.PW_PACKAGE_PATH);
(async () => {
  const base = process.argv[2];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const errors = []; page.on("pageerror", e => errors.push(String(e)));
  const results = [];
  const check = (name, pass, detail) => results.push({ name, pass: Boolean(pass), detail: detail || "" });
  await page.goto(base + "/index.html#/p/" + process.env.PROJECT_ID + "/reader", { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);

  const state = await page.evaluate(async ({ api, pid }) => {
    const value = await fetch(api + "/projects/" + pid + "/reader/state", { headers: { "x-literature-token": "" } })
      .then(r => r.json());
    return { index: value.section_index || [], layers: value.layers || {} };
  }, { api: process.env.API, pid: process.env.PROJECT_ID });
  check("零分层时 /reader/state 仍给出章节索引（无模型依赖）",
    state.index.length >= 2 && state.index[0].id && state.index[0].title,
    JSON.stringify(state.index.slice(0, 2)));

  const panel = await page.evaluate(() => {
    const el = document.querySelector(".rdrr-layerstatus");
    if (!el) return null;
    const select = el.querySelector("select");
    return { text: el.innerText, phase: el.getAttribute("data-phase"),
             scope: select ? select.value : "",
             options: select ? [...select.options].map(o => ({ text: o.textContent, disabled: o.disabled })) : [],
             buttons: [...el.querySelectorAll("button")].map(b => ({ text: b.textContent.trim(), disabled: b.disabled })) };
  });
  check("零分层时折叠范围默认「当前章」而不是整书",
    Boolean(panel) && panel.scope === "section", JSON.stringify(panel && { scope: panel.scope, options: panel.options }));
  check("零分层时「当前章」选项可用，且显示章节 id",
    Boolean(panel) && panel.options.some(o => o.text.includes("当前章") && !o.disabled && o.text.includes("S1")),
    JSON.stringify(panel && panel.options));

  // 目录不需要先折叠/先 AI
  await page.evaluate(() => {
    const button = [...document.querySelectorAll("button")].find(b => b.textContent.includes("目录"));
    button?.click();
  });
  await page.waitForTimeout(600);
  const toc = await page.evaluate(() => [...document.querySelectorAll(".rdrr-toc-row")].map(el => el.textContent.trim()));
  check("零分层时目录可打开并列出章节标题", toc.length >= 2, JSON.stringify(toc));

  // 只折当前章：请求体必须只有这一章的 section_ids
  const posts = [];
  page.on("request", request => {
    if (request.method() === "POST" && request.url().includes("/reader/layers")) {
      try { posts.push(JSON.parse(request.postData() || "{}")); } catch (e) { posts.push({ parse: "fail" }); }
    }
  });
  await page.evaluate(() => {
    const button = [...document.querySelectorAll(".rdrr-layerstatus button")].find(b => b.textContent.includes("用模型折叠"));
    button?.click();
  });
  await page.waitForTimeout(2500);
  const ids = posts.length ? posts[0].section_ids : null;
  check("零分层时点「用模型折叠」只发一次请求且只带当前章 section_ids",
    posts.length === 1 && Array.isArray(ids) && ids.length === 1 && state.index.some(item => item.id === ids[0]),
    JSON.stringify(posts.map(item => ({ locale: item.locale, mode: item.mode, section_ids: item.section_ids }))));

  if (errors.length) results.push({ name: "页面无脚本错误", pass: false, detail: errors.slice(0, 2).join(" | ") });
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

    temporary = Path(tempfile.mkdtemp(prefix="reader-chapter-index-"))
    runtime = temporary / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ.update(LITERATURE_RUNTIME=str(runtime), LITERATURE_LIBRARY=str(temporary / "library"),
                      LITERATURE_SESSION_TOKEN="")
    store, project_id = seed_project(temporary)

    api_port, web_port = free_port(), free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_RUNTIME": str(runtime), "LITERATURE_LIBRARY": str(temporary / "library"),
        "LITERATURE_SESSION_TOKEN": "", "LITERATURE_PORT": str(api_port),
        "PW_PACKAGE_PATH": str(PW), "PROJECT_ID": project_id,
        "API": f"http://127.0.0.1:{api_port}",
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
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    for item in payload["results"]:
        print(f'{"PASS" if item["pass"] else "FAIL"}: {item["name"]} — {item["detail"]}')
    ok = all(item["pass"] for item in payload["results"]) and not payload.get("errors")
    print("PASS" if ok else "FAIL")
    shutil.rmtree(temporary, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
