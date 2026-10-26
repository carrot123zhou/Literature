// 解析来源偏好回归（真实生产构建前端 + 真实 API）。
// 用法：node parse_source_pref_check.cjs <webPort> <shotPath>
// 覆盖：无记录+local 可用→本机；显式 remote→保持云端（刷新后仍云端）；
// 显式 local→保持本机；显式 local 但本地不可用→保持本机并提示不可用；
// 非法 localStorage 值→按无记录处理。每个场景用独立 browser context，
// 保证 localStorage 干净且不被上一次场景污染。
const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);
const fs = require("fs");
const path = require("path");

const webPort = Number(process.argv[2]);
const shotPath = process.argv[3] || "source-pref.png";
const web = `http://127.0.0.1:${webPort}`;
const engineRoot = process.env.LOCAL_ENGINE_ROOT || "";

const consoleErrors = [];

async function main() {
  const checks = [];
  const ok = (name, condition) => checks.push({ name, pass: Boolean(condition) });
  const browser = await chromium.launch({ headless: true });
  let lastPage = null;

  const readBackend = async (page) => {
    const select = page.locator(".intake-options select").first();
    await select.waitFor({ timeout: 20000 });
    await page.waitForTimeout(1000); // 等 /parse/backends 返回并完成默认策略
    return await select.inputValue();
  };

  // 每个场景独立 context：可预置 localStorage（在应用脚本运行前生效）。
  const openWith = async (saved) => {
    const context = await browser.newContext({ viewport: { width: 1440, height: 920 } });
    const page = await context.newPage();
    page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
    page.on("pageerror", e => consoleErrors.push(String(e)));
    await page.addInitScript((value) => {
      try {
        if (value === null) window.localStorage.removeItem("literature.parse.backend");
        else window.localStorage.setItem("literature.parse.backend", value);
      } catch (e) { /* ignore */ }
    }, saved);
    await page.goto(`${web}/#/`, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForSelector(".settings-button", { timeout: 20000 });
    lastPage = page;
    return { page, context };
  };

  // 1) 无记录 + 本地可用 → 默认本机。
  {
    const { page, context } = await openWith(null);
    ok("无记录 + 本地可用 → 默认本机解析", (await readBackend(page)) === "mineru_local");
    await context.close();
  }

  // 2) 显式 remote + 本地可用 → 保持云端；重新挂载（reload）后仍云端。
  {
    const { page, context } = await openWith("mineru_remote");
    ok("显式云端：加载后是云端", (await readBackend(page)) === "mineru_remote");
    await page.reload({ waitUntil: "domcontentloaded" });
    ok("显式云端：重新挂载后仍是云端", (await readBackend(page)) === "mineru_remote");
    await page.reload({ waitUntil: "domcontentloaded" });
    ok("显式云端：再次重新挂载仍不被本地覆盖", (await readBackend(page)) === "mineru_remote");
    await context.close();
  }

  // 3) 显式 local + 本地可用 → 保持本机；重新挂载后仍本机。
  {
    const { page, context } = await openWith("mineru_local");
    ok("显式本机：加载后是本机", (await readBackend(page)) === "mineru_local");
    await page.reload({ waitUntil: "domcontentloaded" });
    ok("显式本机：重新挂载后仍是本机", (await readBackend(page)) === "mineru_local");
    await context.close();
  }

  // 4) 非法 localStorage 值 → 按无记录处理（默认策略 → 本机），并清理脏值。
  {
    const { page, context } = await openWith("garbage-value");
    ok("非法记录 → 回落到默认策略（本机）", (await readBackend(page)) === "mineru_local");
    const cleaned = await page.evaluate(() => window.localStorage.getItem("literature.parse.backend"));
    ok("非法记录已被清理", cleaned === null);
    await context.close();
  }

  // 5) 显式 local 但本地不可用（移除安装标记）→ 保持本机选择 + 明确原因，不静默转云端。
  if (engineRoot) {
    const marker = path.join(engineRoot, "installed.txt");
    try { fs.unlinkSync(marker); } catch (e) { /* ignore */ }
    try {
      const { page, context } = await openWith("mineru_local");
      const value = await readBackend(page);
      const text = await page.locator(".intake-options").first().innerText();
      ok("本地不可用时仍保持本机选择（未静默转云端）", value === "mineru_local");
      ok("本地不可用给出明确提示", /未就绪|不可用|安装|连接/.test(text));
      await page.screenshot({ path: shotPath, fullPage: false });
      await context.close();
    } finally {
      try { fs.writeFileSync(marker, "mineru==4.0.10\n"); } catch (e) { /* ignore */ }
    }
  }

  if (!fs.existsSync(shotPath) && lastPage) await lastPage.screenshot({ path: shotPath, fullPage: false });
  await browser.close();
  ok("0 脚本错误", consoleErrors.length === 0);
  console.log(JSON.stringify({ checks, consoleErrors }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
