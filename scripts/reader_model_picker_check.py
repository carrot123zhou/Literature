"""模型选择器（ModelPicker）两个已知缺陷的浏览器验收。

缺陷（2026-10-25 Lead 实查，designer 修）：
1. **搜索过滤不一致**：键盘索引用的 `flat` 把 `service.name` 纳入匹配，而分组渲染的过滤没纳入
   → 输入服务商名字时分组全被过滤掉（界面 0 项），但 `flat` 里仍有条目，Enter 会提交一个**看不见的**模型。
2. **Enter 绕过 disabled**：`commit(active)` 不检查该服务商是否未配密钥（`configured=false` → `disabled`）。
   鼠标点不进去，键盘 Enter 却能选中。

本脚本用真实生产构建前端 + 真实本地 API（隔离 library/runtime，只读复制生产 `model-services.json`，
密钥仍从本机 Keychain 只读），并追加一个有模型但**没有密钥**的合成服务来构造 disabled 项：

- 输入服务商名 → 该服务下的模型必须可见（≥1 项）；
- 键盘 Enter 落在 disabled 项 → 选择不变、弹层不按未授权项关闭；
- 键盘 Enter 落在可用项 → 选择生效。

用法：.venv/bin/python scripts/reader_model_picker_check.py
边界：不调任何外部模型、不写入生产配置。
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
NO_KEY_SERVICE = "svc-picker-nokey"
NO_KEY_NAME = "无密钥服务"

CHECK_JS = r"""
const { chromium } = require(process.env.PW_PACKAGE_PATH);
(async () => {
  const base = process.argv[2];
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(base + "/index.html#/p/" + process.env.PROJECT_ID + "/reader", { waitUntil: "networkidle" });
  await page.waitForTimeout(600);

  const results = [];
  const check = (name, pass, detail) => results.push({ name, pass: Boolean(pass), detail: detail || "" });
  // 拦截问答请求：只验「前端实际发了什么」，用假回包满足 UI，不发真实模型请求。
  const sent = [];
  await page.route("**/reader/chat/messages", async (route) => {
    try { sent.push(JSON.parse(route.request().postData() || "{}")); } catch { sent.push({}); }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ user_message_id: "u1", assistant_message_id: "a1",
        binding: { service_id: "intercepted", model: "intercepted" },
        chat: { conversation_id: "c1", messages: [] } }),
    });
  });
  try {

  // 先打开 AI 问答侧栏（默认收起），才能看到输入区旁的对话模型选择器。
  const aiToggle = page.locator("#btn-ai").first();
  if (await aiToggle.count()) { await aiToggle.click(); await page.waitForTimeout(400); }
  const opener = page.locator('.rdrr-mpick-btn[aria-label="对话模型"]').first();
  await opener.scrollIntoViewIfNeeded();
  await opener.click();
  await page.waitForTimeout(200);
  const search = page.locator(".rdrr-mpick-search input").first();
  check("选择器可打开且有搜索框", await search.isVisible());

  // 1) 输入「服务商名」→ 该服务下的模型必须可见
  await search.fill(process.env.NO_KEY_NAME);
  await page.waitForTimeout(200);
  const visibleItems = await page.locator(".rdrr-mpick-item").count();
  check("按服务商名搜索时能看到该服务的模型", visibleItems > 0, `可见项 ${visibleItems}`);

  // 2) 键盘 Enter 落在 disabled 项：选择不得改变
  const before = (await opener.textContent()) || "";
  const disabledFocused = await page.evaluate(() => {
    const active = document.querySelector('.rdrr-mpick-item[data-active="true"]');
    return active ? active.disabled : null;
  });
  await search.press("Enter");
  await page.waitForTimeout(200);
  const after = (await opener.textContent()) || "";
  const stillOpen = await page.locator(".rdrr-mpick-search input").count();
  if (disabledFocused === true) {
    check("Enter 不提交 disabled 项", before === after, `before=${before} after=${after}`);
  } else {
    check("Enter 不提交 disabled 项（本次 active 非 disabled，已跳过断言）", true, `active.disabled=${disabledFocused}`);
  }
  if (stillOpen) { await search.press("Escape"); await page.waitForTimeout(100); }

  // 3) 键盘选中一个可用项：必须生效且弹层关闭
  try { await opener.click({ timeout: 3000 }); } catch { await opener.click({ force: true }); }
  await page.waitForTimeout(250);
  const search2 = page.locator(".rdrr-mpick-search input").first();
  await search2.fill("");
  await page.waitForTimeout(200);
  const enabled = await page.evaluate(() => {
    const items = [...document.querySelectorAll(".rdrr-mpick-item")];
    const index = items.findIndex((el) => !el.disabled && el.textContent);
    return index >= 0 ? items[index].textContent : "";
  });
  for (let i = 0; i < 40; i += 1) {
    const isEnabled = await page.evaluate(() => {
      const active = document.querySelector('.rdrr-mpick-item[data-active="true"]');
      return active ? !active.disabled : false;
    });
    if (isEnabled) break;
    await search2.press("ArrowDown");
  }
  await search2.press("Enter");
  await page.waitForTimeout(250);
  const closed = (await page.locator(".rdrr-mpick-search input").count()) === 0;
  const picked = (await opener.textContent()) || "";
  check("Enter 选中可用项后弹层关闭且文案变化", closed && picked.trim().length > 0, `picked=${picked.trim()}`);

    // 4) D1 回归：选择器选中的绑定必须出现在请求体**顶层**
    const expected = await page.evaluate(() => {
      const el = document.querySelector(".rdrr-mpick-btn[aria-label='对话模型']");
      return el ? el.getAttribute("data-binding") : null;
    });
    const textarea = page.locator(".rdrr-ai textarea").first();
    if (await textarea.count()) {
      await textarea.fill("测试路由");
      await page.locator(".rdrr-ai-send").first().click();
      await page.waitForTimeout(500);
    }
    const body = sent.length ? sent[sent.length - 1] : {};
    const pickedLabel = (await opener.textContent()) || "";
    check(
      "问答请求体顶层带 service_id/model（D1 回归）",
      Boolean(body.service_id && body.model),
      `body=${JSON.stringify({ service_id: body.service_id, model: body.model, keys: Object.keys(body) })} picked=${pickedLabel.trim()} bindingAttr=${expected}`,
    );
    check("问答请求体不再嵌套 binding 字段", !("binding" in body), `keys=${Object.keys(body).join(",")}`);
  } catch (error) {
    check("脚本异常（已收集到的断言仍上报）", false, String(error).slice(0, 200));
  } finally {
    await browser.close();
    console.log(JSON.stringify({ results, errors }));
  }
})();
"""


def main() -> int:
    if not NODE.is_file() or not PW.is_dir():
        print("SKIP: 缺少 ms-playwright-go node/package")
        return 2
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        print("FAIL: frontend/dist 不存在，请先 vite build")
        return 1

    temporary = Path(tempfile.mkdtemp(prefix="picker-check-"))
    runtime, library = temporary / "runtime", temporary / "library"
    runtime.mkdir(); library.mkdir()
    (runtime / "session-token").write_text("", encoding="utf-8")
    production = Path.home() / "Library" / "Application Support" / "Literature" / "model-services.json"
    if production.is_file():
        config = json.loads(production.read_text(encoding="utf-8"))
    else:
        config = {"version": 2, "services": [], "roles": {}}
    # 追加一个「有模型、无密钥」的服务（configured=false → 选择器里是 disabled 项），放在最前面。
    config["services"] = [{
        "service_id": NO_KEY_SERVICE, "name": NO_KEY_NAME, "preset": "custom",
        "base_url": "https://no-key.invalid/v1", "allow_plaintext_http": False,
        "models": [{"model": "no-key-model", "context_window": 0, "max_output_tokens": 4096,
                    "temperature": 0.2, "name": "无密钥模型"}],
    }] + list(config.get("services") or [])
    (runtime / "model-services.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""

    from literature.models import Block, Document, Page
    from literature.storage import ProjectStore

    store = ProjectStore(library)
    origin = temporary / "sample.pdf"
    origin.write_bytes(b"%PDF-1.4\n% picker fixture")
    project = store.create_project(origin, title="模型选择器验收样本")
    project_id = str(project["project_id"])
    store.save_document(Document(
        project_id=project_id, title="模型选择器验收样本", source_language="en", page_count=1,
        pages=[Page(page_index=0, width=600, height=800)],
        blocks=[Block(block_id="b1", type="paragraph", order=0, page_index=0,
                      source_text="A paragraph used only for the model picker check.")],
    ))

    api_port, web_port = free_port(), free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_RUNTIME": str(runtime), "LITERATURE_LIBRARY": str(library),
        "LITERATURE_SESSION_TOKEN": "", "LITERATURE_PORT": str(api_port),
        "PW_PACKAGE_PATH": str(PW), "PROJECT_ID": project_id, "NO_KEY_NAME": NO_KEY_NAME,
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
                                   capture_output=True, text=True, env=env, timeout=240)
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
        print(f'{"PASS" if item["pass"] else "FAIL"}: {item["name"]} {item["detail"]}')
    if payload.get("errors"):
        print("脚本错误：", payload["errors"])
    ok = all(item["pass"] for item in payload["results"]) and not payload.get("errors")
    print("PASS" if ok else "FAIL")
    shutil.rmtree(temporary, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
