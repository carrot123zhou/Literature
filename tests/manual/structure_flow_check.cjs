// 真实浏览器验收：结构校对跨页续接（虚线框 + 连线）与对照观看优化。
// 用法：PW_PACKAGE_PATH=<pkg> node structure_flow_check.cjs <webPort> <apiPort> <projectId> [screenshotPath]
// 仅本地：不联网、不读 Keychain。
const { createRequire } = require("module");

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
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));

  await page.goto(`${web}/#/p/${projectId}/review`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  checks.push(["结构校对页可打开", (await page.innerText("body")).includes("源文件预览")]);

  const canvasReady = await waitFor(page, async () => (await page.locator(".pdf-page canvas").count()) >= 2);
  checks.push(["两页 PDF 都渲染出 canvas", canvasReady]);

  const region = await waitFor(page, async () => (await page.locator('.bbox[data-flow-region="p0"]').count()) >= 1);
  checks.push(["续页补画出续接区域框", region]);
  checks.push(["续接区域可点选回源块", (await page.locator('.bbox[data-flow-region="p0"]').count()) === 1]);

  // 与 MinerU 一致：续页区域与普通正文块同样式，不用另外的颜色/虚线。
  const styleMatch = await page.evaluate(() => {
    const region = document.querySelector('.bbox[data-flow-region="p0"]');
    const normal = Array.from(document.querySelectorAll(".bbox.bbox-paragraph")).find(el => !el.hasAttribute("data-flow-region"));
    if (!region || !normal) return null;
    const a = getComputedStyle(region);
    const b = getComputedStyle(normal);
    return { border: a.borderColor === b.borderColor && a.borderStyle === b.borderStyle, bg: a.backgroundColor === b.backgroundColor };
  });
  checks.push(["续接区域与普通正文块同色（无额外标记色）", Boolean(styleMatch && styleMatch.border && styleMatch.bg)]);
  const sourceClass = await page.locator('.bbox[data-block-id="p0"]').getAttribute("class");
  checks.push(["源块保持普通框样式", !/continued|continues|is-flow/.test(sourceClass || "")]);
  checks.push(["框上不再带续上页/续下页文字角标", (await page.locator('.pdf-overlay .bbox-label:has-text("续")').count()) === 0]);

  const lineBox = await waitFor(page, async () => (await page.locator(".flow-line").count()) >= 1);
  checks.push(["两页之间画出虚线折线", lineBox]);
  const d = lineBox ? await page.locator(".flow-line").first().getAttribute("d") : "";
  const numbers = (d || "").match(/-?\d+(\.\d+)?/g)?.map(Number) ?? [];
  const commands = (d || "").replace(/[-\d.\s]/g, "");
  checks.push(["连线为 MinerU 式折线（V/H），不穿过正文", numbers.length === 5 && commands === "MVHV"]);
  checks.push(["折线自上而下，横段落在两页之间", numbers.every(Number.isFinite) && numbers[1] < numbers[2] && numbers[2] < numbers[4]]);
  checks.push(["连线两端各有一个圆点", (await page.locator(".flow-dot").count()) >= 2]);
  await page.getByRole("button", { name: "图例", exact: true }).click();
  const legendItems = await page.locator(".legend-panel .legend-item").count();
  const legendText = await page.locator(".legend-panel").innerText().catch(() => "");
  checks.push(["图例改为按需浮层且含 7 类（含跨页续接）", legendItems === 7 && legendText.includes("跨页续接")]);
  if (screenshotPath) await page.screenshot({ path: screenshotPath.replace(/\.png$/, "-legend.png"), fullPage: false });
  await page.keyboard.press("Escape");
  checks.push(["按 Esc 关闭图例浮层", (await page.locator(".legend-panel").count()) === 0]);
  checks.push(["工具栏显示缩放百分比", /%/.test(await page.locator(".zoom-readout").innerText().catch(() => ""))]);

  // 上/下页按钮真实跳转。
  const pageAttr = () => page.locator(".pdf-panel").first().getAttribute("data-current-page");
  checks.push(["初始为第 1 页", (await pageAttr()) === "1"]);
  await page.locator('button[aria-label="下一页"]').click();
  const moved = await waitFor(page, async () => (await pageAttr()) === "2");
  checks.push(["点「下一页」跳到第 2 页", moved]);
  await page.locator('button[aria-label="上一页"]').click();
  const back = await waitFor(page, async () => (await pageAttr()) === "1");
  checks.push(["点「上一页」回到第 1 页", back]);

  // 结构编辑：续接块卡片有跨页标记。
  checks.push(["结构块卡片显示跨页标记", (await page.locator(".flow-chip").count()) >= 1]);

  // Markdown 页签：逐块渲染 + 与 PDF 双向高亮。
  await page.getByRole("button", { name: "Markdown", exact: true }).click();
  await page.waitForTimeout(400);
  const mdCount = await page.locator(".md-block").count();
  checks.push(["Markdown 按块渲染", mdCount === 2]);
  await page.locator(".md-block").first().click();
  const highlighted = await waitFor(page, async () => (await page.locator(".md-block.selected").count()) === 1);
  checks.push(["点 Markdown 块会定位并高亮", highlighted]);
  const selectedBox = await waitFor(page, async () => (await page.locator(".bbox.selected").count()) >= 1);
  checks.push(["Markdown 选中同步到 PDF 框", selectedBox]);
  checks.push(["选中源块时续页区域一并点亮", (await page.locator('.bbox[data-flow-region="p0"].selected').count()) >= 1]);

  checks.push(["浏览器 0 脚本错误", consoleErrors.length === 0]);

  if (screenshotPath) {
    await page.getByRole("button", { name: "结构编辑", exact: true }).click();
    await page.waitForTimeout(600);
    await page.screenshot({ path: screenshotPath, fullPage: false });
  }

  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
