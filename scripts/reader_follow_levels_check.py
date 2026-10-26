"""五层（L1–L5）原译滚动 → 原件跟随的验收（真实 PDF + 真实 bbox，多章节可滚动）。

背景（2026-10-25）：tester 的 U3 在 2 页/6 块夹具上「滚到底」其实**没有可滚高度**
（source scrollHeight == clientHeight），L4/L5 只渲染 2 张节卡片更不可能滚动，
于是 origin 不动是必然结果，不能据此判定“跟随没实现”。
本脚本用同一个 seed 夹具再补 8 个章节（标题 + 段落，带 bbox），让每一层都真的能滚动，
然后逐层断言：滚动原文栏后 ①原件 body scrollTop 变化 ②当前块切到新位置 ③该块 bbox 居中。

用法：.venv/bin/python scripts/reader_follow_levels_check.py
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
  const page = await browser.newPage({ viewport: { width: 1280, height: 760 } });
  const errors = []; page.on("pageerror", e => errors.push(String(e)));
  const results = [];
  const check = (name, pass, detail) => results.push({ name, pass: Boolean(pass), detail: detail || "" });
  await page.goto(base + "/index.html#/p/" + process.env.PROJECT_ID + "/reader", { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);

  // 记录所有生成请求：切层/切栏/滚动都不该发任何 POST /reader/layers
  const genPosts = [];
  page.on("request", request => {
    if (request.method() === "POST" && request.url().includes("/reader/layers")) {
      let body = {}; try { body = JSON.parse(request.postData() || "{}"); } catch (e) {}
      genPosts.push({ locale: body.locale || "", mode: body.mode || "", sections: (body.section_ids || []).length });
    }
  });

  const snapshot = async () => await page.evaluate(() => {
    const ob = document.querySelector("#rdrr-pane-origin .rdrr-pane-body");
    const sb = document.querySelector("#rdrr-pane-source .rdrr-pane-body");
    const sel = document.querySelector("#rdrr-pane-origin .bbox.selected");
    const id = sel ? sel.getAttribute("data-block-id") : "";
    const mid = ob ? ob.getBoundingClientRect().top + ob.clientHeight / 2 : 0;
    const rect = sel ? sel.getBoundingClientRect() : null;
    return {
      originTop: ob ? Math.round(ob.scrollTop) : -1,
      srcTop: sb ? Math.round(sb.scrollTop) : -1,
      srcScrollable: sb ? sb.scrollHeight > sb.clientHeight + 4 : false,
      selected: id,
      centerDist: rect ? Math.round(Math.abs((rect.top + rect.height / 2) - mid)) : null,
    };
  });

  const switchPanes = async () => {
    // 切栏：origin/source/trans 三个选框各点两次（关再开）
    for (const key of ["origin", "trans", "source"]) {
      const box = page.locator(`.rdrr-ptoggle input[aria-label="${key}"]`).first();
      if (await box.count()) { await box.click(); await page.waitForTimeout(300); await box.click(); await page.waitForTimeout(300); }
    }
  };
  await switchPanes();

  const scrollSource = async (toBottom) => {
    await page.evaluate((bottom) => {
      const sb = document.querySelector("#rdrr-pane-source .rdrr-pane-body");
      sb.scrollTop = bottom ? sb.scrollHeight : 0;
      sb.dispatchEvent(new Event("scroll", { bubbles: true }));
    }, toBottom);
    await page.waitForTimeout(900);
  };

  for (const level of [1, 2, 3, 4, 5]) {
    await page.locator("#lvlbar").focus();
    await page.keyboard.press("Digit" + level);
    await page.waitForTimeout(700);
    // 顶部作为基线（首个块贴着页首，无法再往上居中，不要求居中）
    await scrollSource(false);
    await page.waitForTimeout(400);
    const top = await snapshot();
    // 底部：原件应显著下移，且目标 bbox 仍居中（说明跟的是真实 bbox，不是页号）
    await scrollSource(true);
    const bottom = await snapshot();
    check(`L${level} 滚到底部：原件跟随下移（scrollTop > 200）且 bbox 居中（≤150px）`,
      bottom.srcScrollable && bottom.originTop > 200 && bottom.centerDist !== null && bottom.centerDist <= 150,
      JSON.stringify(bottom));
    check(`L${level} 顶部基线可滚（用于对比）`, top.srcScrollable, JSON.stringify(top));
    check(`L${level} 跟随过程中当前块随之改变（顶部≠底部）`,
      Boolean(top.selected) && Boolean(bottom.selected) && top.selected !== bottom.selected,
      `top=${top.selected} bottom=${bottom.selected}`);
  }

  // 关闭「跟随滚动」后不再联动（先回 L1，保证 source 可滚）
  await page.locator("#lvlbar").focus();
  await page.keyboard.press("Digit1");
  await page.waitForTimeout(500);
  await page.locator("button", { hasText: "Aa" }).first().click();
  await page.waitForTimeout(300);
  const box = page.locator(".rdrr-aa-check input").first();
  if (await box.count()) { await box.uncheck(); await page.waitForTimeout(300); }
  await page.locator("button", { hasText: "Aa" }).first().click();
  await page.waitForTimeout(250);
  const offBefore = await snapshot();
  await page.evaluate(() => {
    const sb = document.querySelector("#rdrr-pane-source .rdrr-pane-body");
    sb.scrollTop = 0;
    sb.dispatchEvent(new Event("scroll", { bubbles: true }));
  });
  await page.waitForTimeout(700);
  const offAfter = await snapshot();
  check("关闭跟随滚动后原件不再联动", offAfter.originTop === offBefore.originTop,
    `before=${offBefore.originTop} after=${offAfter.originTop}`);
  check("切栏/切层/滚动全程 0 折叠生成请求（切层只读缓存）",
    genPosts.length === 0, JSON.stringify(genPosts.slice(0, 4)));

  await browser.close();
  console.log(JSON.stringify({ results, errors }));
})();
"""


def ensure_local_layers(api_port: int, project_id: str, locale: str = "source", timeout: float = 120.0) -> bool:
    """新契约：首开不自动折叠。测试需要章节骨架时，必须先显式生成本地预览层。

    这里直接调 API（等价于用户点「本地预览」），不触发模型调用。
    """
    import time

    import httpx

    base = f"http://127.0.0.1:{api_port}"
    headers = {"x-literature-token": ""}
    response = httpx.post(f"{base}/projects/{project_id}/reader/layers", headers=headers,
                          json={"locale": locale, "mode": "local"}, timeout=30, trust_env=False)
    if response.status_code >= 400:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = httpx.get(f"{base}/projects/{project_id}/reader/state", headers=headers, timeout=10, trust_env=False).json()
        job = state.get("layer_job") or {}
        layers = httpx.get(f"{base}/projects/{project_id}/reader/layers?locale={locale}", headers=headers,
                           timeout=10, trust_env=False).json()
        if layers.get("generated") and job.get("status") != "running":
            return True
        time.sleep(0.3)
    return False


def main() -> int:
    if not NODE.is_file() or not PW.is_dir():
        print("SKIP: 缺少 ms-playwright-go node/package")
        return 2
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        print("FAIL: frontend/dist 不存在，请先 vite build")
        return 1

    temporary = Path(tempfile.mkdtemp(prefix="reader-follow-"))
    runtime = temporary / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ.update(LITERATURE_RUNTIME=str(runtime), LITERATURE_LIBRARY=str(temporary / "library"),
                      LITERATURE_SESSION_TOKEN="")
    store, project_id = seed_project(temporary)

    # 补章节：让 L1–L5 每一层都真的能滚动（否则“滚到底”是空操作）
    from literature.models import Block
    document = store.load_document(project_id)
    order = len(document.blocks)
    page_count = max(1, document.page_count)
    for index in range(8):
        page = index % page_count
        document.blocks.append(Block(
            block_id=f"s{index}-title", type="title", order=order, page_index=page,
            source_text=f"Supplemental Section {index + 1}", level=1,
            bbox=[20, 60, 480, 85],
        ))
        order += 1
        for part in range(3):
            document.blocks.append(Block(
                block_id=f"s{index}-p{part}", type="paragraph", order=order, page_index=page,
                source_text=(f"Section {index + 1} paragraph {part + 1}. " * 12).strip(),
                bbox=[20, 100 + part * 90, 480, 175 + part * 90],
            ))
            order += 1
    store.save_document(document)

    api_port, web_port = free_port(), free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_RUNTIME": str(runtime), "LITERATURE_LIBRARY": str(temporary / "library"),
        "LITERATURE_SESSION_TOKEN": "", "LITERATURE_PORT": str(api_port),
        "PW_PACKAGE_PATH": str(PW), "PROJECT_ID": project_id,
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
        if not ensure_local_layers(api_port, project_id):
            print("FAIL: 本地预览层未生成（章节骨架缺失，L4/L5 无法断言）")
            return 1
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
    if payload.get("errors"):
        print("脚本错误：", payload["errors"][:3])
    ok = all(item["pass"] for item in payload["results"]) and not payload.get("errors")
    print("PASS" if ok else "FAIL")
    out = ROOT / "tmp" / "reader-follow-levels.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(temporary, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
