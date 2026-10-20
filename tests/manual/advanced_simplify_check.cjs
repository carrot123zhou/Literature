// 高级选项精简重构验收（Playwright）。用法：node advanced_simplify_check.cjs <webPort> <apiPort> <projectId> <shotPath>
// 验证：精简后仅剩「目录基准 + 视觉开关」两个选择；已移除的控件不再出现；本地校准端到端可用。
const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);
const projectId = process.argv[4];
const shotPath = process.argv[5] || "advanced-simplify.png";
const web = `http://127.0.0.1:${webPort}`;
const api = `http://127.0.0.1:${apiPort}`;
const headers = { "x-literature-token": "" };

const getJson = async (url) => { const r = await fetch(url, { headers }); if (!r.ok) throw new Error(`${url} -> ${r.status}`); return r.json(); };

async function main() {
  const checks = [];
  const consoleErrors = [];
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));

  await page.goto(`${web}/#/p/${encodeURIComponent(projectId)}/review`, { waitUntil: "networkidle" });
  await page.waitForSelector(".review-outline", { timeout: 20000 });
  await page.waitForTimeout(1200);

  // ---- A：打开 AI 校准与高级选项 ----
  await page.locator("details.review-assistant > summary").first().click();
  await page.waitForTimeout(300);
  await page.locator(".review-advanced > summary").first().click();
  await page.waitForTimeout(400);
  const advanced = page.locator(".review-advanced").first();

  // ---- B：保留的核心控件 ----
  checks.push(["保留：目录基准选择", await advanced.locator("select[aria-label=目录基准来源]").count() === 1]);
  checks.push(["保留：限定目录页输入", await advanced.locator("input[aria-label=源文件目录页]").count() === 1]);
  checks.push(["保留：本地读取目录按钮", await advanced.locator("button", { hasText: "本地读取目录" }).count() === 1]);
  checks.push(["保留：只做本地校准按钮", await advanced.locator("button", { hasText: "只做本地校准" }).count() === 1]);
  checks.push(["保留：预览启动输入按钮", await advanced.locator("button", { hasText: "预览启动输入" }).count() === 1]);
  checks.push(["保留：对照原页图片开关", await advanced.locator(".structure-option", { hasText: "对照原页图片" }).count() === 1]);
  checks.push(["保留：确认当前审查范围", await advanced.locator("button", { hasText: "确认当前审查范围" }).count() === 1]);

  // ---- C：已移除的控件不再出现 ----
  for (const label of ["自动采用高置信修正", "复核模式", "目录来源", "规划策略", "复核并发", "单次模型响应等待", "诊断覆盖"]) {
    checks.push([`移除：${label}`, await page.getByText(label, { exact: false }).count() === 0]);
  }

  // ---- D：切到「使用已确认的目录」出现基准输入，切回恢复目录页输入 ----
  await advanced.locator("select[aria-label=目录基准来源]").selectOption("confirmed");
  await page.waitForTimeout(300);
  checks.push(["已确认模式：显示目录基准输入", await advanced.locator("textarea[aria-label=已确认的目录基准]").count() === 1]);
  checks.push(["已确认模式：隐藏目录页输入", await advanced.locator("input[aria-label=源文件目录页]").count() === 0]);
  await advanced.locator("select[aria-label=目录基准来源]").selectOption("source");
  await page.waitForTimeout(300);
  checks.push(["切回自动模式：目录页输入恢复", await advanced.locator("input[aria-label=源文件目录页]").count() === 1]);

  // ---- E：本地校准端到端（无模型）----
  await advanced.locator("input[aria-label=源文件目录页]").fill("1");
  await advanced.locator("button", { hasText: "只做本地校准" }).click();
  let localOk = false;
  try {
    await page.waitForSelector(".toast", { timeout: 20000 });
    localOk = (await page.locator(".toast").first().innerText()).length > 0;
  } catch { /* fallthrough */ }
  checks.push(["只做本地校准产生结果提示", Boolean(localOk)]);
  const status = await getJson(`${api}/projects/${projectId}/status`);
  const localJob = (status.jobs || []).find(j => j.kind === "structure_local");
  checks.push(["本地校准作业已落库完成", Boolean(localJob) && localJob.status === "completed"]);

  await page.screenshot({ path: shotPath, fullPage: false });
  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
