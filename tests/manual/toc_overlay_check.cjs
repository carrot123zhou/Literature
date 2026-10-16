// 真实浏览器验收：目录页 bbox 不再堆叠 + 分栏最小宽度 + 已移除“内容块”提示条。
// 用法：PW_PACKAGE_PATH=<pkg> node toc_overlay_check.cjs <webPort> <apiPort> <projectId> [screenshotPath]
// 仅本地：不联网、不读 Keychain。
const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);
const projectId = process.argv[4];
const screenshotPath = process.argv[5] || "";
const web = `http://127.0.0.1:${webPort}`;

async function waitFor(page, fn, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try { if (await fn()) return true; } catch { /* retry */ }
    await page.waitForTimeout(150);
  }
  return false;
}

async function main() {
  const checks = [];
  const consoleErrors = [];
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));

  await page.goto(`${web}/#/p/${projectId}/review`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  const ready = await waitFor(page, async () => (await page.locator(".pdf-overlay .bbox").count()) >= 14);
  checks.push(["目录页 14 个 bbox 全部渲染", ready]);
  checks.push(["已移除「N 个内容块 · 点击后调整层级」提示条", (await page.locator(".result-toolbar").count()) === 0]);

  // 1) 矮行不再被全局 button{min-height:32px} 撑高。
  const metrics = await page.evaluate(() => {
    const heights = Array.from(document.querySelectorAll(".pdf-overlay .bbox")).map(b => b.getBoundingClientRect().height).filter(h => h > 0);
    return { min: Math.min(...heights), count: heights.length };
  });
  checks.push(["最矮目录 bbox 未被撑到 32px", metrics.min > 0 && metrics.min < 24]);

  // 2) 相邻 bbox 不再互相堆叠（任意两框交集面积近 0）。
  const worst = await page.evaluate(() => {
    const rects = Array.from(document.querySelectorAll(".pdf-overlay .bbox")).map(b => b.getBoundingClientRect());
    let worst = 0;
    for (let i = 0; i < rects.length; i += 1) {
      for (let j = i + 1; j < rects.length; j += 1) {
        const a = rects[i], b = rects[j];
        const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (w > 0 && h > 0) worst = Math.max(worst, w * h);
      }
    }
    return worst;
  });
  checks.push(["相邻目录框不再重叠", worst < 1]);

  // 3) 三栏头部等高、字号一致
  const headers = await page.evaluate(() => {
    const pick = selector => {
      const el = document.querySelector(selector);
      if (!el) return null;
      const label = el.querySelector("strong, button");
      return { h: el.getBoundingClientRect().height, font: label ? getComputedStyle(label).fontSize : "" };
    };
    return {
      outline: pick(".review-outline .panel-title"),
      viewer: pick(".pdf-panel .viewer-toolbar"),
      tabs: pick(".inspector .result-tabs"),
    };
  });
  const headerHeights = [headers.outline?.h, headers.viewer?.h, headers.tabs?.h];
  checks.push(["三栏头部等高", headerHeights.every(h => h && Math.abs(h - headerHeights[0]) <= 1), headerHeights]);
  const headerFonts = [headers.outline?.font, headers.viewer?.font, headers.tabs?.font];
  checks.push(["三栏标题字号一致", headerFonts.every(Boolean) && new Set(headerFonts).size === 1, headerFonts]);
  if (screenshotPath) await page.screenshot({ path: screenshotPath.replace(/\.png$/, "-wide.png"), fullPage: false });

  // 4) 分栏最小宽度：把分隔条拖到最左，各栏不被拖没。
  const dragTo = async (index, x) => {
    const handle = page.locator(".split-handle").nth(index);
    const box = await handle.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + 220);
    await page.mouse.down();
    await page.mouse.move(x, box.y + 220, { steps: 8 });
    await page.mouse.up();
    await page.waitForTimeout(200);
  };
  await dragTo(0, 5);
  const sidebar = await page.locator(".review-outline").boundingBox();
  checks.push(["左栏拖到最左仍保留最小宽度", sidebar.width >= 190]);
  await dragTo(1, 5);
  const pdfPanel = await page.locator(".pdf-panel").boundingBox();
  checks.push(["预览栏拖到最左仍保留最小宽度", pdfPanel.width >= 340]);

  // 5) 中栏工具栏保持单行；放不下时横向滚动而不是换行。
  const toolbar = await page.evaluate(() => {
    const el = document.querySelector(".pdf-panel .viewer-toolbar");
    const controls = document.querySelector(".pdf-panel .review-viewer-controls");
    return { h: el.getBoundingClientRect().height, scrollW: controls.scrollWidth, clientW: controls.clientWidth };
  });
  checks.push(["中栏工具栏保持单行未换行", toolbar.h <= 56, Math.round(toolbar.h)]);
  checks.push(["中栏工具栏放不下时可横向滚动", toolbar.scrollW > toolbar.clientW]);

  checks.push(["浏览器 0 脚本错误", consoleErrors.length === 0]);

  if (screenshotPath) await page.screenshot({ path: screenshotPath, fullPage: false });
  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
