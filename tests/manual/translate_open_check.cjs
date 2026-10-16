// 「开始翻译 → 确认 → 自动打开项目」验收。用法：node translate_open_check.cjs <webPort> <apiPort> <projectId> <shotPath>
const { createRequire } = require("module");

const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);
const projectId = process.argv[4];
const shotPath = process.argv[5] || "translate-open.png";
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

  await page.goto(`${web}/#/resources`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1800);

  // 切到列表视图展开 → 翻译行「开始翻译」→ 弹窗确认。
  await page.locator(".res-seg button", { hasText: "列表" }).click();
  await page.waitForTimeout(500);
  await page.locator(".res-lrow", { hasText: "Translate Open Book" }).first().click();
  await page.waitForTimeout(700);
  await page.getByText("开始翻译", { exact: true }).first().click();
  await page.waitForSelector(".res-dialog", { timeout: 8000 });
  checks.push(["翻译弹窗打开", (await page.innerText(".res-dialog")).includes("翻译 · Translate Open Book")]);
  await page.locator(".res-dialog-foot button.primary", { hasText: "开始翻译" }).click();

  // 确认成功后：弹窗关闭且自动打开该项目（对照翻译页）。
  const opened = await poll(async () => page.url().includes(`/${projectId}/read`), 10000);
  checks.push(["确认后自动打开该项目（对照翻译页）", opened]);
  await page.waitForTimeout(1500);
  const body = await page.innerText("body");
  checks.push(["页面已切换到该项目内容", body.includes("Translate Open Book")]);
  await page.screenshot({ path: shotPath, fullPage: false });

  // 后端：翻译作业已创建。
  const jobs = await getJson(`${api}/projects/${projectId}/translation/jobs`).catch(() => []);
  checks.push(["翻译作业已进入队列", Array.isArray(jobs) && jobs.length >= 1]);

  checks.push(["0 控制台错误", consoleErrors.length === 0, consoleErrors.join(" | ")]);
  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
