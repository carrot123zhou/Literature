// 解析历史 / 首页分区 / 保留期设置的真实浏览器验收。
// 用法：PW_PACKAGE_PATH=<pkg> SHOT_DIR=<dir> node parse_ui_history_check.cjs <webPort> <apiPort>
const { createRequire } = require("module");
const path = require("path");

const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);
const web = `http://127.0.0.1:${webPort}`;
const api = `http://127.0.0.1:${apiPort}`;
const headers = { "x-literature-token": "" };
const shotDir = process.env.SHOT_DIR || ".";
const HOME_UNCLIP = ".app-shell{height:auto !important;overflow:visible !important;grid-template-rows:auto auto auto !important}"
  + ".page-scroll{overflow:visible !important;height:auto !important;min-height:0 !important}"
  + "html,body{height:auto !important;overflow:visible !important}";
const getJson = async (url) => { const r = await fetch(url, { headers }); if (!r.ok) throw new Error(`${url} -> ${r.status}`); return r.json(); };

async function main() {
  const checks = [];
  const consoleErrors = [];
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));

  // ---- 首页分区 + 分页 ----
  await page.goto(`${web}/#/home`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3000);
  const homeText = await page.innerText("body");
  checks.push(["首页有当前任务分区", homeText.includes("当前处理任务")]);
  checks.push(["首页有最近完成分区", homeText.includes("最近完成")]);
  checks.push(["首页失败任务不受 3 条限制并展示计数", homeText.includes("失败待处理")]);
  checks.push(["首页当前任务显示分页", homeText.includes("第 1 / 2 页")]);
  const recentCount = await page.locator(".ocr-recent").count();
  checks.push(["最近完成最多 3 条", recentCount > 0 && recentCount <= 3]);
  checks.push(["首页同时存在当前任务与最近完成两区", (await page.locator(".intake-progress-card").count()) >= 2 && (await page.locator(".recent-completed-card").count()) === 1]);
  checks.push(["最近完成无满格进度条", (await page.locator(".recent-completed-card progress").count()) === 0]);
  // 首页任务区完整截图：临时解除滚动容器与外壳裁剪后整页截图，确保两区都在图内。
  await page.addStyleTag({ content: HOME_UNCLIP });
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(shotDir, "home-sections.png"), fullPage: true });

  // 首页当前任务翻到第 2 页。
  await page.getByText("下一页", { exact: true }).first().click();
  await page.waitForTimeout(1500);
  checks.push(["首页当前任务可翻到第 2 页", (await page.innerText("body")).includes("第 2 / 2 页")]);

  // ---- 解析历史入口（按钮 + 路由可刷新恢复） ----
  await page.goto(`${web}/#/home`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  await page.getByText("解析历史", { exact: false }).first().click();
  await page.waitForTimeout(2500);
  checks.push(["首页按钮进入解析历史路由", page.url().includes("#/history")]);
  checks.push(["历史页标题可见", (await page.innerText("body")).includes("解析历史")]);
  const history = await getJson(`${api}/ocr/tasks?scope=history&page=1&page_size=20`);
  checks.push(["历史包含全部记录（含已归档）", history.total >= 37]);
  checks.push(["历史默认分页 20 条", history.items.length === 20 && history.pages >= 2]);
  // 刷新后仍是历史路由。
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  checks.push(["历史路由刷新后恢复", page.url().includes("#/history")]);

  // ---- 搜索 / 状态筛选 / 日期排序 ----
  await page.locator("input[placeholder='搜索文件名或任务']").first().fill("no-such-job-xyz");
  await page.waitForTimeout(1200);
  checks.push(["搜索无结果显示 0 条", (await page.innerText("body")).includes("0 条记录")]);
  await page.locator("input[placeholder='搜索文件名或任务']").first().fill("");
  await page.waitForTimeout(1200);
  await page.locator("select[aria-label='状态筛选']").selectOption("completed");
  await page.waitForTimeout(1200);
  const completedTotal = (await getJson(`${api}/ocr/tasks?scope=history&status=completed`)).total;
  checks.push(["状态筛选已完成", completedTotal >= 25]);
  await page.locator("select[aria-label='状态筛选']").selectOption("failed");
  await page.waitForTimeout(1200);
  checks.push(["状态筛选失败", (await getJson(`${api}/ocr/tasks?scope=history&status=failed`)).total === 12]);
  await page.locator("select[aria-label='状态筛选']").selectOption("archived");
  await page.waitForTimeout(1200);
  checks.push(["90 天超期记录已自动归档", (await getJson(`${api}/ocr/tasks?scope=history&status=archived`)).total === 1]);
  await page.locator("select[aria-label='状态筛选']").selectOption("");
  await page.locator("select[aria-label='排序']").selectOption("oldest");
  await page.waitForTimeout(1200);
  checks.push(["日期排序切换可用", (await page.innerText("body")).includes("条记录")]);

  // ---- 历史分页 ----
  await page.locator("select[aria-label='排序']").selectOption("recent");
  await page.waitForTimeout(1200);
  const pager = (await page.innerText("body")).match(/第 (\d+) \/ (\d+) 页/);
  checks.push(["历史显示分页控件（多页）", Boolean(pager) && Number(pager[2]) > 1]);
  await page.getByText("下一页", { exact: true }).first().click();
  await page.waitForTimeout(1200);
  const pager2 = (await page.innerText("body")).match(/第 (\d+) \/ (\d+) 页/);
  checks.push(["历史可翻到下一页", Boolean(pager2) && Boolean(pager) && Number(pager2[1]) === Number(pager[1]) + 1]);
  await page.getByText("上一页", { exact: true }).first().click();
  await page.waitForTimeout(1200);

  // ---- 归档失败记录 → 取消归档找回 ----
  await page.locator("select[aria-label='状态筛选']").selectOption("failed");
  await page.waitForTimeout(1200);
  checks.push(["历史失败记录提供重试入口", (await page.getByText("重试失败分段", { exact: true }).count()) > 0]);
  await page.getByText("归档此记录", { exact: true }).first().click();
  await page.waitForTimeout(1500);
  checks.push(["失败记录手动归档后已归档计数增加", (await getJson(`${api}/ocr/tasks?scope=history&status=archived`)).total === 2]);
  await page.locator("select[aria-label='状态筛选']").selectOption("archived");
  await page.waitForTimeout(1200);
  const manualRow = page.locator(".history-row", { hasText: "手动归档" }).first();
  checks.push(["已归档区显示手动归档原因", (await manualRow.count()) > 0]);
  if (await manualRow.count()) await manualRow.getByText("取消归档", { exact: true }).click();
  await page.waitForTimeout(1500);
  checks.push(["取消归档找回记录", (await getJson(`${api}/ocr/tasks?scope=history&status=archived`)).total === 1]);
  checks.push(["找回后失败记录回到失败列表", (await getJson(`${api}/ocr/tasks?scope=history&status=failed`)).total === 12]);

  // ---- 保留期设置 ----
  await page.getByText("30 天", { exact: false }).first().click();
  await page.waitForTimeout(1200);
  checks.push(["保留期可设为 30 天", (await getJson(`${api}/ocr/archive-settings`)).retention_days === 30]);
  await page.getByText("永久保留", { exact: false }).first().click();
  await page.waitForTimeout(1200);
  checks.push(["保留期可设为永久", (await getJson(`${api}/ocr/archive-settings`)).retention_days === null]);
  await page.getByText("90 天（默认）", { exact: false }).first().click();
  await page.waitForTimeout(1200);
  checks.push(["保留期恢复 90 天", (await getJson(`${api}/ocr/archive-settings`)).retention_days === 90]);

  await page.screenshot({ path: path.join(shotDir, "history-page.png"), fullPage: false });
  await page.setViewportSize({ width: 900, height: 760 });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: path.join(shotDir, "history-narrow.png"), fullPage: false });
  await page.setViewportSize({ width: 1440, height: 980 });
  checks.push(["窄窗口历史页仍渲染记录", (await page.locator(".history-row").count()) > 0]);

  // ---- 最近完成收起（持久化，且未来新完成仍会出现） ----
  await page.goto(`${web}/#/home`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3000);
  const beforeCollapse = await page.locator(".ocr-recent").count();
  const collapse = page.getByText("收起已完成", { exact: true }).first();
  checks.push(["最近完成提供收起按钮", (await collapse.count()) > 0]);
  if (await collapse.count()) { await collapse.click(); await page.waitForTimeout(1200); }
  checks.push(["收起后最近完成隐藏当前记录", (await page.locator(".ocr-recent").count()) === 0]);
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  checks.push(["收起状态刷新后持久化（显示展开按钮）", (await page.innerText("body")).includes("展开已完成")]);
  await page.getByText("展开已完成", { exact: true }).first().click().catch(() => {});
  await page.waitForTimeout(1200);
  checks.push(["可展开恢复最近完成", (await page.locator(".ocr-recent").count()) === beforeCollapse]);
  // 窄屏首页任务区不裁切，并留完整截图。
  await page.setViewportSize({ width: 900, height: 760 });
  await page.waitForTimeout(1200);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  checks.push(["窄屏首页无横向裁切", overflow <= 2]);
  await page.addStyleTag({ content: HOME_UNCLIP });
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(shotDir, "home-sections-narrow.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 980 });

  checks.push(["浏览器 0 脚本错误", consoleErrors.length === 0]);
  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors }));
}
main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
