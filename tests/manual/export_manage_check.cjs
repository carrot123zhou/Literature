// 导出管理 + 顶栏验收（Playwright）。用法：node export_manage_check.cjs <webPort> <apiPort> <projectId> <shotPath>
const { createRequire } = require("module");

const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);
const projectId = process.argv[4];
const shotPath = process.argv[5] || "export-manage.png";
const web = `http://127.0.0.1:${webPort}`;
const api = `http://127.0.0.1:${apiPort}`;
const headers = { "x-literature-token": "" };

const getJson = async (url) => { const r = await fetch(url, { headers }); if (!r.ok) throw new Error(`${url} -> ${r.status}`); return r.json(); };
async function poll(fn, timeoutMs, intervalMs = 400) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try { if (await fn()) return true; } catch { /* retry */ }
    await new Promise(r => setTimeout(r, intervalMs));
  }
  return false;
}

async function main() {
  const checks = [];
  const consoleErrors = [];
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));

  // ---- A：顶栏高度 60px + 应用图标 ----
  await page.goto(`${web}/#/resources`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  const topbar = page.locator("header.home-topbar");
  checks.push(["顶栏高度 60px", Math.round(await topbar.boundingBox().then(b => b.height)) === 60]);
  const icon = page.locator(".topbar-appicon");
  checks.push(["顶栏 Literature 前有应用图标", await icon.isVisible() && await icon.evaluate(el => el.naturalWidth > 0)]);
  checks.push(["品牌文字仍是 Literature", (await page.locator(".intake-brand").innerText()).includes("Literature")]);

  // ---- B：打开导出弹窗，默认 EPUB + 对照版 ----
  const card = page.locator(".res-card", { hasText: "Between Cultures" }).first();
  await card.locator(".res-mini-btn", { hasText: "导出" }).click();
  await page.waitForSelector(".export-dialog", { timeout: 10000 });
  await page.waitForTimeout(1200);
  const chipOn = await page.locator(".export-chip.on").allInnerTexts();
  checks.push(["默认只勾选 EPUB", chipOn.length === 1 && chipOn[0].includes("EPUB")]);
  const modeOn = await page.locator(".radio label.on").innerText();
  checks.push(["默认内容版本 = 对照版", modeOn.includes("对照版")]);

  // ---- C：开始导出，结果行不溢出卡片 ----
  await page.locator(".export-dialog .res-dialog-foot button.primary", { hasText: "开始导出" }).click();
  const done = await poll(async () => await page.locator(".export-result").count(), 20000);
  checks.push(["导出完成出现结果卡片", Boolean(done)]);
  const overflow = await page.evaluate(() => {
    const bad = [];
    document.querySelectorAll(".export-block").forEach(block => {
      const b = block.getBoundingClientRect();
      block.querySelectorAll(".export-file").forEach(row => {
        const r = row.getBoundingClientRect();
        if (r.right > b.right + 0.5 || r.left < b.left - 0.5) bad.push(row.textContent.slice(0, 40));
      });
    });
    return bad;
  });
  checks.push(["导出文件行均在卡片边框内", overflow.length === 0, overflow.join(" | ")]);
  const dialogScroll = await page.evaluate(() => {
    const body = document.querySelector(".export-dialog .res-dialog-body");
    return body.scrollWidth <= body.clientWidth + 1;
  });
  checks.push(["弹窗主体无横向溢出", dialogScroll]);
  await page.screenshot({ path: shotPath, fullPage: false });

  // ---- D：历史记录管理（浏览器态无「显示位置」，桌面态才有） ----
  await page.locator(".export-history .export-toggle").click();
  await page.waitForSelector(".export-hist", { timeout: 5000 });
  const acts = await page.locator(".export-hist-acts").first().innerText();
  checks.push(["历史记录有重命名/删除", acts.includes("重命名") && acts.includes("删除")]);
  await page.locator(".export-hist-acts button", { hasText: "重命名" }).first().click();
  await page.waitForSelector(".export-hist-rename input", { timeout: 5000 });
  await page.locator(".export-hist-rename input").fill("我的对照版");
  await page.locator(".export-hist-rename button.primary", { hasText: "保存" }).click();
  const renamed = await poll(async () => (await page.locator(".export-hist-head b").first().innerText()) === "我的对照版", 8000);
  checks.push(["重命名后历史标题更新", renamed]);
  const listing = await getJson(`${api}/projects/${projectId}/exports`);
  checks.push(["后端 manifest 记录 display_name", listing.length === 1 && listing[0].display_name === "我的对照版"]);

  // ---- E：删除（两步确认） ----
  await page.locator(".export-hist-acts button", { hasText: "删除" }).first().click();
  await page.locator(".export-hist-acts button", { hasText: "确认删除" }).first().click();
  const gone = await poll(async () => (await page.locator(".export-hist").count()) === 0, 8000);
  checks.push(["确认删除后记录消失", gone]);
  const after = await getJson(`${api}/projects/${projectId}/exports`);
  checks.push(["后端导出历史清空", after.length === 0]);

  checks.push(["0 控制台错误", consoleErrors.length === 0, consoleErrors.join(" | ")]);
  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
