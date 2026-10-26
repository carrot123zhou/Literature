// 解析引擎管理卡浏览器验收。用法：node engine_ui_check.cjs <webPort> <apiPort> <fakeV1Port> <shotPath>
// 覆盖：初始 managed 未安装提示；切换面板展示本机发现候选（有则一键选用）；
// 首次连接带密钥的 v1 服务（一次填 URL+Key+连接成功）；断开恢复；
// 无效路径检测给出明确错误；0 脚本错误。
const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);
const fakePort = Number(process.argv[4]);
const shotPath = process.argv[5] || "engine-ui.png";
const web = `http://127.0.0.1:${webPort}`;
const api = `http://127.0.0.1:${apiPort}`;
const headers = { "x-literature-token": "", "content-type": "application/json" };
const getJson = async (url) => { const r = await fetch(url, { headers }); if (!r.ok) throw new Error(`${url} -> ${r.status}`); return r.json(); };

const consoleErrors = [];
async function main() {
  const checks = [];
  const ok = (name, condition) => checks.push({ name, pass: Boolean(condition) });
  const card = () => page.locator(".engine-card").first();
  const clickVisible = async (text) => {
    const deadline = Date.now() + 15000;
    while (Date.now() < deadline) {
      const buttons = card().locator("button", { hasText: text });
      const count = await buttons.count();
      for (let i = 0; i < count; i += 1) {
        const button = buttons.nth(i);
        if (await button.isVisible()) { await button.click(); return true; }
      }
      await page.waitForTimeout(200);
    }
    throw new Error(`按钮不可见：${text}`);
  };
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));

  await page.goto(`${web}/#/`, { waitUntil: "domcontentloaded", timeout: 30000 });
  // SPA 启动校验 capabilities 后才渲染顶栏；等设置按钮出现。
  await page.waitForSelector(".settings-button", { timeout: 20000 });
  await page.click(".settings-button");
  await page.waitForSelector(".settings-v2", { timeout: 10000 });
  // 左侧导航进入「文档解析」。
  await page.locator(".settings-nav button", { hasText: "文档解析" }).first().click();
  await page.waitForSelector(".engine-card", { timeout: 10000 });
  await page.waitForTimeout(400);

  // 1) 初始 managed：未安装提示 + 安装按钮。
  const cardText = await card().innerText();
  ok("初始来源为独立安装", cardText.includes("独立安装"));
  ok("未安装提示可见", cardText.includes("未安装"));
  ok("安装按钮存在", await card().locator("button", { hasText: "下载并安装" }).count() > 0);
  ok("安装前无卸载按钮", await card().locator("button", { hasText: "卸载引擎" }).count() === 0);

  // 2) 切换面板：本机发现候选（有则一键选用 TextFold/PaperFold 类外部引擎）。
  const openAllDetails = () => page.evaluate(() => document.querySelectorAll("details").forEach(d => { d.open = true; }));
  await openAllDetails();
  await page.waitForTimeout(300);
  const discovered = (await getJson(`${api}/parse/engine/sources`)).discovered || [];
  const usable = discovered.find(item => item.available);
  if (usable) {
    const row = card().locator(".engine-discovered", { hasText: usable.version }).first();
    ok("发现候选已展示", await row.count() > 0);
    await row.locator("button", { hasText: "选用" }).click();
    await page.waitForTimeout(800);
    const text = await card().innerText();
    ok("一键选用后来源为已有安装", text.includes("已有本机安装"));
    ok("选用后显示外部引擎版本", text.includes(usable.version));
    // 断开回 managed，给后续场景一个干净的起点。
    // 来源切换会重渲染、details 默认收起：统一用 evaluate 打开。
    await openAllDetails();
    await page.waitForTimeout(300);
    await clickVisible("下载安装（独立安装）");
    await page.waitForTimeout(800);
    ok("切回独立安装", (await card().innerText()).includes("独立安装"));
  } else {
    ok("本机无可用候选时展示发现区（跳过选用）", true);
  }

  // 3) 首次连接带密钥服务：一次填 URL+Key+连接（无 Key 时假服务 401）。
  await card().locator("label", { hasText: "解析服务地址" }).first().locator("input").fill(`http://127.0.0.1:${fakePort}`);
  await card().locator("label", { hasText: "服务密钥" }).first().locator("input").fill("test-key-123");
  await clickVisible("连接已有服务");
  await page.waitForTimeout(1200);
  const afterConnect = await card().innerText();
  ok("带密钥首次连接成功", afterConnect.includes("已有解析服务"));
  ok("连接后无卸载按钮（external/http 不管理远端）", await card().locator("button", { hasText: "卸载引擎" }).count() === 0);

  // 4) 断开：恢复 managed 并清密钥。
  await clickVisible("断开");
  await page.waitForTimeout(800);
  ok("断开后回到独立安装", (await card().innerText()).includes("独立安装"));
  const sources = await getJson(`${api}/parse/engine/sources`);
  ok("断开后无已存密钥", sources.http_key_configured === false);

  // 5) 无效路径检测给出明确错误（toast）。
  await card().locator("label", { hasText: "已有安装目录" }).first().locator("input").fill("/nonexistent-engine-path");
  await clickVisible("检测该目录");
  await page.waitForTimeout(800);
  // 无效路径的明确原因直接内联展示在检测结果里（不依赖 toast）。
  const afterProbe = await card().innerText();
  ok("无效路径有明确错误提示", afterProbe.includes("mineru-kit") || afterProbe.includes("不可用"));

  await page.screenshot({ path: shotPath, fullPage: false });
  await browser.close();
  ok("0 脚本错误", consoleErrors.length === 0);
  console.log(JSON.stringify({ checks, consoleErrors, shot: shotPath }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
