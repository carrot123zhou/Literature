// 队列 UI 调序验收：点击指定等待任务的“置顶”，返回调序后的队列。
// 用法：PW_PACKAGE_PATH=<pkg> node parse_ui_queue_reorder.cjs <webPort> <apiPort> <title>
const { createRequire } = require("module");
const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);
const title = process.argv[4];
const web = `http://127.0.0.1:${webPort}`;
const api = `http://127.0.0.1:${apiPort}`;
const headers = { "x-literature-token": "" };

async function main() {
  const checks = [];
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(`${web}/#/home`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3000);
  const jobs = await (await fetch(`${api}/ocr/jobs`, { headers })).json();
  const titleById = Object.fromEntries(jobs.map(j => [j.job_id, j.title]));
  const targetJobId = (jobs.find(j => j.title === title) || {}).job_id;
  const before = (await (await fetch(`${api}/ocr/queue`, { headers })).json()).order;
  const row = page.locator(".ocr-job", { hasText: title }).first();
  const top = row.getByText("置顶", { exact: true }).first();
  checks.push(["等待任务行可见并可置顶", (await top.count()) > 0]);
  if (await top.count()) await top.click();
  await page.waitForTimeout(1500);
  const after = (await (await fetch(`${api}/ocr/queue`, { headers })).json()).order;
  checks.push(["置顶后目标任务在队首", after[0] === targetJobId]);
  checks.push(["队列顺序已改变（调序前≠后或本就在首）", JSON.stringify(before) !== JSON.stringify(after) || before[0] === targetJobId]);
  await browser.close();
  console.log(JSON.stringify({ checks, before, after, titleById }));
}
main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
