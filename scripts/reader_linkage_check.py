"""阅读器跨栏联动 / hover / 点击标记 bar 的真机前置验收（真实 PDF + 真实 bbox）。

覆盖用户真机反馈的四点：
1. **原文/译文滚动 → 原件跟随**：按真实 bbox 居中，而不是按估算页高或页号滚；
2. **原件滚动 → 原文/译文跟随**（bbox 中心最近的块）；
3. **hover 只做视觉联动**：对应 bbox 出现轻描边类，滚动位置不变；
4. **点击（含重复点击同一块）每次都定位并使 bbox 居中**，且不用先划词就弹出 5 色 + 笔记 + 追问 bar；
   Esc 关闭；展开按钮/已有高亮不误触发。

用真实书目副本（`library/` 下第一个带 PDF 与 bbox 的项目）+ 真实本地 API + 生产构建前端。
不调模型、不写生产库。

用法：.venv/bin/python scripts/reader_linkage_check.py
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
  const targetBlock = process.env.TARGET_BLOCK;     // 有真实 bbox 的段
  const farBlock = process.env.FAR_BLOCK;           // 页号更靠后的段（验证跨页定位）
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const results = [];
  const check = (name, pass, detail) => results.push({ name, pass: Boolean(pass), detail: detail || "" });

  await page.goto(base + "/index.html#/p/" + pid + "/reader", { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  // 显式切到 L1（全文）：默认层级来自阅读偏好，不应假设
  await page.keyboard.press("1");
  await page.waitForTimeout(500);

  const origin = page.locator("#rdrr-pane-origin .rdrr-pane-body");
  const source = page.locator("#rdrr-pane-source .rdrr-pane-body");
  const trans = page.locator("#rdrr-pane-trans .rdrr-pane-body");

  const originState = async () => await origin.evaluate((el) => ({ top: el.scrollTop, height: el.clientHeight }));
  const bboxInfo = async (blockId) => await page.evaluate((id) => {
    const body = document.querySelector("#rdrr-pane-origin .rdrr-pane-body");
    const box = body && body.querySelector(`[data-block-id="${CSS.escape(id)}"]`);
    if (!body || !box) return null;
    const br = body.getBoundingClientRect(), r = box.getBoundingClientRect();
    return { centerOffset: (r.top + r.height / 2) - (br.top + br.height / 2), visible: r.height > 0, page: box.closest(".pdf-page")?.getAttribute("data-page-index") };
  }, blockId);

  // ---- 1) 原文滚动 → 原件按 bbox 居中 ----
  const beforeOrigin = await originState();
  await source.evaluate((el) => { el.scrollTop = 260; el.dispatchEvent(new Event("scroll", { bubbles: true })); });
  await page.waitForTimeout(900);
  const afterOrigin = await originState();
  check("L1 原文滚动使原件滚动（不是只切页号）", Math.abs(afterOrigin.top - beforeOrigin.top) > 4,
    `before=${beforeOrigin.top} after=${afterOrigin.top}`);

  // 找到原文栏顶部当前块，验证其原件 bbox 落在视口中线附近
  const blockAtTop = await page.evaluate(() => {
    const body = document.querySelector("#rdrr-pane-source .rdrr-pane-body");
    const blocks = [...body.querySelectorAll("[data-block-id]")].filter((el) => el.getClientRects().length);
    const bodyRect = body.getBoundingClientRect();
    let best = null, bestDist = Infinity;
    for (const el of blocks) {
      const r = el.getBoundingClientRect();
      const d = Math.abs(r.top - (bodyRect.top + 110));
      if (r.bottom > bodyRect.top && d < bestDist) { best = el.dataset.blockId; bestDist = d; }
    }
    return best;
  });
  const box = blockAtTop ? await bboxInfo(blockAtTop) : null;
  check("L1 跟随到位后，对应原件 bbox 居中（|偏移| ≤ 150px）", Boolean(box) && Math.abs(box.centerOffset) <= 150,
    `block=${blockAtTop} offset=${box ? Math.round(box.centerOffset) : "no-bbox"}`);

  // ---- 2) 原件滚动 → 文本栏跟随（当前块随原件中心更新，原文栏滚到该块） ----
  const textPosBefore = await source.evaluate((el) => el.scrollTop);
  await origin.evaluate((el) => { el.scrollTop = Math.round(el.scrollHeight * 0.55); el.dispatchEvent(new Event("scroll", { bubbles: true })); });
  await page.waitForTimeout(900);
  const followInfo = await page.evaluate(() => {
    const body = document.querySelector("#rdrr-pane-origin .rdrr-pane-body");
    const src = document.querySelector("#rdrr-pane-source .rdrr-pane-body");
    const selected = body.querySelector(".pdf-overlay .bbox.selected");
    const id = selected ? selected.getAttribute("data-block-id") : "";
    const inSrc = id ? Boolean(src.querySelector(`[data-block-id="${CSS.escape(id)}"]`)) : false;
    return { id, inSrc, srcTop: src.scrollTop };
  });
  check("L1 原件滚动→当前块更新（bbox 出现 selected）", Boolean(followInfo.id), JSON.stringify(followInfo));
  check("L1 原件滚动→原文栏跟到该块（块可见且滚动变化）",
    followInfo.inSrc && Math.abs(followInfo.srcTop - textPosBefore) > 4,
    `before=${textPosBefore} after=${followInfo.srcTop} inSrc=${followInfo.inSrc}`);

  // ---- 3) hover 视觉联动、不滚动 ----
  await source.evaluate((el) => { el.scrollTop = 120; el.dispatchEvent(new Event("scroll", { bubbles: true })); });
  await page.waitForTimeout(700);
  const hoverTarget = await page.evaluate(() => {
    const body = document.querySelector("#rdrr-pane-source .rdrr-pane-body");
    const el = [...body.querySelectorAll("[data-block-id]")].find((n) => n.getClientRects().length);
    return el ? el.dataset.blockId : "";
  });
  await page.waitForTimeout(600);
  const originBeforeHover2 = await originState();
  const sourceBeforeHover2 = await source.evaluate((el) => el.scrollTop);
  // 用合成 mouseover 触发（React 的 onMouseEnter 走 mouseover 委托）；
  // Playwright 的 .hover() 会先 scrollIntoViewIfNeeded，会把滚动位置改掉，污染这条断言。
  await page.evaluate((id) => {
    const el = document.querySelector(`#rdrr-pane-source [data-block-id="${CSS.escape(id)}"]`);
    el?.dispatchEvent(new MouseEvent("mouseover", { bubbles: true }));
  }, hoverTarget);
  await page.waitForTimeout(400);
  const hoverClasses = await page.evaluate((id) => {
    const textHit = document.querySelector(`#rdrr-pane-source [data-block-id="${CSS.escape(id)}"]`)?.classList.contains("rdrr-hover");
    const boxHit = document.querySelector(`#rdrr-pane-origin [data-block-id="${CSS.escape(id)}"]`)?.classList.contains("rdrr-bbox-hover");
    return { textHit, boxHit };
  }, hoverTarget);
  const originAfterHover = await originState();
  const sourceAfterHover = await source.evaluate((el) => el.scrollTop);
  check("hover 给原文块加可见状态类", hoverClasses.textHit, JSON.stringify(hoverClasses));
  check("hover 给原件 bbox 加轻描边类", hoverClasses.boxHit, JSON.stringify(hoverClasses));
  check("hover 不触发任何滚动", originAfterHover.top === originBeforeHover2.top && sourceAfterHover === sourceBeforeHover2,
    `origin ${originBeforeHover2.top}->${originAfterHover.top}, source ${sourceBeforeHover2}->${sourceAfterHover}`);

  // ---- 4) 点击段落（未划词）→ bar + bbox 居中；重复点击仍居中 ----
  await page.locator(`#rdrr-pane-source [data-block-id="${hoverTarget}"]`).first().click();
  await page.waitForTimeout(500);
  const bar1 = await page.locator(".rdrr-blockbar").count();
  const barParts = bar1 ? await page.evaluate(() => {
    const bar = document.querySelector(".rdrr-blockbar");
    return {
      swatches: bar.querySelectorAll(".rdrr-bb-sw").length,
      note: [...bar.querySelectorAll(".rdrr-bb-act")].some((b) => b.textContent.includes("笔记")),
      ask: [...bar.querySelectorAll(".rdrr-bb-act")].some((b) => b.textContent.includes("追问")),
    };
  }) : { swatches: 0, note: false, ask: false };
  const firstBox = await bboxInfo(hoverTarget);
  check("点击段落无需划词即出现 bar", bar1 === 1, `bars=${bar1}`);
  check("bar 含 5 色 + 笔记 + 追问", barParts.swatches === 5 && barParts.note && barParts.ask, JSON.stringify(barParts));
  check("L1 点击后原件 bbox 居中（|偏移| ≤ 150px）", Boolean(firstBox) && Math.abs(firstBox.centerOffset) <= 120,
    `offset=${firstBox ? Math.round(firstBox.centerOffset) : "no-bbox"}`);

  // 先把原件滚走，再点同一块：必须重新居中（不是“已选中就跳过”）
  await origin.evaluate((el) => { el.scrollTop = 0; el.dispatchEvent(new Event("scroll", { bubbles: true })); });
  await page.waitForTimeout(700);
  await page.locator(`#rdrr-pane-source [data-block-id="${hoverTarget}"]`).first().click();
  await page.waitForTimeout(800);
  const secondBox = await bboxInfo(hoverTarget);
  check("L1 重复点击同一块会再次居中", Boolean(secondBox) && Math.abs(secondBox.centerOffset) <= 150,
    `offset=${secondBox ? Math.round(secondBox.centerOffset) : "no-bbox"}`);

  // Esc 关闭 bar
  await page.keyboard.press("Escape");
  await page.waitForTimeout(200);
  check("Esc 关闭块级 bar", (await page.locator(".rdrr-blockbar").count()) === 0);

  // ---- 5) 跨页定位：点靠后的段，原件要切到真实页并居中 ----
  const far = await page.evaluate((id) => {
    const body = document.querySelector("#rdrr-pane-source .rdrr-pane-body");
    const el = body.querySelector(`[data-block-id="${CSS.escape(id)}"]`);
    if (!el) return false;
    el.scrollIntoView({ block: "center" });
    return true;
  }, farBlock);
  if (far) {
    await page.waitForTimeout(500);
    await page.locator(`#rdrr-pane-source [data-block-id="${farBlock}"]`).first().click({ force: true });
    await page.waitForTimeout(1400);
    const farBox = await bboxInfo(farBlock);
    check("跨页目标：原件切到真实页并居中", Boolean(farBox) && Math.abs(farBox.centerOffset) <= 140,
      `page=${farBox ? farBox.page : "?"} offset=${farBox ? Math.round(farBox.centerOffset) : "no-bbox"}`);
  } else {
    check("跨页目标：原文栏能找到该块", false, farBlock);
  }

  // ---- 6) L4 节锚点：点节卡片 → 用节标题块 bbox 居中，且 bar 明说“摘要无逐字范围” ----
  await page.keyboard.press("4");
  await page.waitForTimeout(600);
  const spine = await page.evaluate(() => {
    const body = document.querySelector("#rdrr-pane-source .rdrr-pane-body");
    const el = body?.querySelector(".rdrr-spine-item[data-block-id]");
    return el ? el.getAttribute("data-block-id") : "";
  });
  if (spine) {
    await page.locator(`#rdrr-pane-source .rdrr-spine-item[data-block-id="${spine}"]`).first().click();
    await page.waitForTimeout(900);
    const spineBox = await bboxInfo(spine);
    const hint = await page.locator(".rdrr-blockbar .rdrr-bb-hint").count();
    check("L4 节卡片点击后原件按节标题 bbox 居中（|偏移| ≤ 150px）",
      Boolean(spineBox) && Math.abs(spineBox.centerOffset) <= 150,
      `anchor=${spine} offset=${spineBox ? Math.round(spineBox.centerOffset) : "no-bbox"}`);
    check("L4 摘要 bar 明确标注“摘要无逐字范围”", hint === 1, `hint=${hint}`);
    await page.keyboard.press("Escape");
  } else {
    check("L4 节卡片存在（带节锚点 data-block-id）", false, "no spine item");
  }

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

    library = ROOT / "library"
    candidates = []
    for path in sorted(library.iterdir()):
        doc = path / "document" / "document.json"
        pdf = path / "source" / "origin.pdf"
        if doc.is_file() and pdf.is_file():
            data = json.loads(doc.read_text(encoding="utf-8"))
            if not any(len(b.get("bbox") or []) == 4 for b in data.get("blocks", [])):
                continue
            # 优先小书（页面少 → 渲染与滚动更快，断言更稳定）
            candidates.append((int(data.get("page_count") or 0), path, data))
    if not candidates:
        print("SKIP: library/ 里没有带真实 PDF 与 bbox 的项目")
        return 2
    candidates.sort(key=lambda item: item[0] or 10**6)
    _pages, book, document = candidates[0]

    temporary = Path(tempfile.mkdtemp(prefix="reader-linkage-"))
    runtime, libdir = temporary / "runtime", temporary / "library"
    runtime.mkdir(); libdir.mkdir()
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ.update(LITERATURE_RUNTIME=str(runtime), LITERATURE_LIBRARY=str(libdir), LITERATURE_SESSION_TOKEN="")

    from literature.storage import ProjectStore

    store = ProjectStore(libdir)
    source = temporary / "origin.pdf"
    shutil.copyfile(book / "source" / "origin.pdf", source)
    project = store.create_project(source, title=book.name[:60])
    project_id = str(project["project_id"])
    target_doc = store.project_path(project_id) / "document" / "document.json"
    target_doc.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(book / "document" / "document.json", target_doc)
    translation = book / "translations" / "zh-CN" / "translation.json"
    if translation.is_file():
        into = store.translation_path(project_id, "zh-CN")
        into.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(translation, into)

    blocks = [b for b in document["blocks"] if b.get("type") in ("paragraph", "list") and len(b.get("bbox") or []) == 4]
    blocks.sort(key=lambda b: (b["page_index"], b["bbox"][1]))
    target_block = blocks[0]["block_id"]
    last_page = max(b["page_index"] for b in blocks)
    far = [b for b in blocks if b["page_index"] >= max(1, last_page - 1)]
    far_block = (far[len(far) // 2] if far else blocks[-1])["block_id"]

    api_port, web_port = free_port(), free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_RUNTIME": str(runtime), "LITERATURE_LIBRARY": str(libdir),
        "LITERATURE_SESSION_TOKEN": "", "LITERATURE_PORT": str(api_port),
        "PW_PACKAGE_PATH": str(PW), "PROJECT_ID": project_id,
        "TARGET_BLOCK": target_block, "FAR_BLOCK": far_block,
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
    out = ROOT / "tmp" / "reader-linkage-result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({**payload, "book": book.name[:60], "target": target_block, "far": far_block},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(temporary, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
