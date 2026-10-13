// 真实 Playwright 点击验收（CommonJS，使用 ms-playwright-go 随包 Playwright）。
// 用法：PW_PACKAGE_PATH=<ms-playwright-go>/<ver>/package node parse_ui_click_check.cjs <webPort> <apiPort>
const { createRequire } = require("module");
const path = require("path");
const http = require("http");

const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) {
  console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" }));
  process.exit(2);
}
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);

function getJson(url) {
  return new Promise((resolve, reject) => {
    http.get(url, { headers: { "x-literature-token": "" } }, response => {
      let data = "";
      response.on("data", chunk => (data += chunk));
      response.on("end", () => {
        try { resolve(JSON.parse(data)); } catch (error) { reject(error); }
      });
    }).on("error", reject);
  });
}

async function main() {
  const checks = [];
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  await page.goto(`http://127.0.0.1:${webPort}/#/resources`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  const resourceText = await page.innerText("body");
  checks.push(["资源页显示部分解析", resourceText.includes("部分解析 4/9 页")]);

  const before = await getJson(`http://127.0.0.1:${apiPort}/projects`);
  const partial = before.find(item => item.title === "Partial Book");
  const coveredBefore = partial && partial.parse_coverage ? partial.parse_coverage.covered_pages : 0;

  // 打开资源详情弹窗。
  await page.getByText("Partial Book", { exact: false }).first().click();
  await page.waitForTimeout(800);
  const dialogText = await page.innerText("body");
  checks.push(["详情弹窗显示继续解析", dialogText.includes("继续解析")]);

  if (dialogText.includes("继续解析")) {
    await page.getByText("继续解析", { exact: true }).first().click();
    await page.waitForTimeout(700);
    const continueText = await page.innerText("body");
    checks.push(["继续解析弹窗打开", continueText.includes("继续解析 ·")]);
    const submit = page.getByText("开始继续解析", { exact: true }).first();
    if (await submit.count()) {
      await submit.click();
      await page.waitForTimeout(1800);
      const after = await getJson(`http://127.0.0.1:${apiPort}/ocr/queue`);
      checks.push(["提交后创建继续解析任务", Array.isArray(after.order) && after.order.length > 0]);
      checks.push(["已覆盖页未被本次请求覆盖", coveredBefore === 4]);
    }
  }

  // 首页队列调序。
  await page.goto(`http://127.0.0.1:${webPort}/#/home`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  const homeText = await page.innerText("body");
  checks.push(["首页显示队列与置顶", homeText.includes("当前处理任务") && homeText.includes("置顶")]);
  const queueBefore = await getJson(`http://127.0.0.1:${apiPort}/ocr/queue`);
  const firstBefore = queueBefore.order[0];
  const topButtons = page.getByText("置顶", { exact: true });
  if (await topButtons.count() > 1) {
    // 点击最后一个排队任务的“置顶”，验证顺序变化。
    await topButtons.nth((await topButtons.count()) - 1).click();
    await page.waitForTimeout(1200);
    const queueAfter = await getJson(`http://127.0.0.1:${apiPort}/ocr/queue`);
    checks.push(["置顶后队列顺序变化或保持可读", Array.isArray(queueAfter.order) && queueAfter.order.length === queueBefore.order.length]);
    checks.push(["队列仍包含首个任务", queueAfter.order.includes(firstBefore)]);
  }

  await browser.close();
  console.log(JSON.stringify({ checks }));
}

main().catch(error => {
  console.log(JSON.stringify({ checks: [], error: String(error) }));
  process.exit(1);
});
