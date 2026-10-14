// 真实浏览器验收：AI 结构校对的复核参数、运行计时/阶段、覆盖口径与思考配置。
// 用法：PW_PACKAGE_PATH=<pkg> node structure_ui_check.cjs <webPort> <apiPort> <projectId>
// 仅接收 JSON、不联网、不读 Keychain；结构作业响应由本地路由 stub。
const { createRequire } = require("module");

const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);
const projectId = process.argv[4];
const web = `http://127.0.0.1:${webPort}`;
const api = `http://127.0.0.1:${apiPort}`;
const headers = { "x-literature-token": "" };

const getJson = async (url) => { const r = await fetch(url, { headers }); if (!r.ok) throw new Error(`${url} -> ${r.status}`); return r.json(); };

async function main() {
  const checks = [];
  const consoleErrors = [];
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));

  const START_MS = Date.now() - 95000;
  const INFLIGHT_MS = Date.now() - 45000;
  const isoAt = (ms) => new Date(ms).toISOString();
  let captured = null;
  let mode = "running";

  const runningJob = () => ({
    job_id: "fake-struct-1", kind: "structure_ai", status: "running", progress: 0.86,
    error: "", scope: {}, message: "标题角色复核进行中",
    created_at: isoAt(START_MS), updated_at: isoAt(Date.now()),
    activity: [
      { at: isoAt(START_MS + 5000), level: "model", text: "整体标题树规划第 1/2 批 · 64 个候选 · 已用时 30s" },
      { at: isoAt(INFLIGHT_MS), level: "model", text: "标题角色复核完成 1/3 批 · 已处理 40/90 个候选 · 已用时 12s" },
    ],
    result: { audit: { requests: [
      { scope: "structure_outline", status: "responded", started_at: isoAt(START_MS + 5000) },
      { scope: "structure_proofread", status: "started", started_at: isoAt(INFLIGHT_MS) },
    ], planning: { mode: "adaptive", fallback: false, framework: { state: "reliable" },
                  reliable_regions: 11, ambiguous_regions: 5, requests: 16, local_upgrades: 0,
                  missing: 0, overlap_ignored: 3 } } },
  });
  const completedJob = () => ({
    ...runningJob(), status: "completed", progress: 1.0, message: "结构分析完成",
    result: {
      review_coverage: { candidates: 348, reviewed: 340, needs_review: 0, reused: 87, second_pass: 261 },
      review_selection: { stats: { reused: 87, review: 279, qa: 18 }, reasons: {}, full_review: false,
                          review_mode: "full", second_pass_ids: [], deferred: 0,
                          qa: { checked: 18, agree: 18, disagree: 0, agreement: 1.0, disagreements: [] } },
      toc_extraction: "local",
      local_toc_marker_pages: [5],
      audit: { requests: [], planning: { mode: "adaptive", fallback: false, framework: { state: "reliable" },
                                        reliable_regions: 11, ambiguous_regions: 5, requests: 16,
                                        local_upgrades: 0, missing: 0, overlap_ignored: 3 } },
    },
  });

  await page.route("**/structure/jobs", async route => {
    const request = route.request();
    if (request.method() !== "POST") return route.continue();
    try { captured = JSON.parse(request.postData() || "{}"); } catch { captured = {}; }
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(runningJob()) });
  });
  await page.route("**/structure/jobs/fake-struct-1", async route => {
    const body = mode === "running" ? runningJob() : completedJob();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  await page.goto(`${web}/#/p/${projectId}/review`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  checks.push(["结构校对页可打开", (await page.innerText("body")).includes("AI 校准")]);

  // 展开高级选项：复核模式选全量双遍、目录来源选总是让模型核对，并发 3 路、规划自适应。
  await page.getByText("校对选项", { exact: false }).first().click();
  await page.waitForTimeout(300);
  checks.push(["旧的“全量复核每个候选”复选框已移除", await page.locator('label:has-text("全量复核每个候选")').count() === 0]);
  await page.locator('select[aria-label="复核模式"]').selectOption("full");
  await page.locator('select[aria-label="目录来源"]').selectOption("model");
  await page.locator('input[aria-label="复核并发"]').fill("3");
  await page.locator('select[aria-label="规划策略"]').selectOption("adaptive");
  await page.waitForTimeout(200);

  const runButton = page.getByRole("button", { name: /AI 校准结构|AI 校对标题/ }).first();
  checks.push(["运行按钮可用", await runButton.isEnabled()]);
  await runButton.click();
  const gotPost = await (async () => {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline && !captured) await page.waitForTimeout(100);
    return Boolean(captured);
  })();
  checks.push(["POST /structure/jobs 已发出", gotPost]);
  checks.push(["POST 传复核模式 proofread_mode=full", captured?.proofread_mode === "full"]);
  checks.push(["POST 传目录来源 toc_extraction=model", captured?.toc_extraction === "model"]);
  checks.push(["POST 传并发 proofread_concurrency=3", captured?.proofread_concurrency === 3]);
  checks.push(["POST 传规划策略 planning_mode=adaptive", captured?.planning_mode === "adaptive"]);

  // 运行中计时与当前阶段耗时（基于 created_at / audit.requests.started_at，应随真实时间递增）。
  const readElapsed = (text, anchor) => {
    const m = text.match(new RegExp(anchor + "\\s*(?:(\\d+)\\s*分\\s*)?(\\d+)\\s*秒"));
    return m ? Number(m[1] || 0) * 60 + Number(m[2]) : null;
  };
  await page.waitForTimeout(2500);
  const runningText = await page.innerText("body");
  const total1 = readElapsed(runningText, "已运行");
  const stage1 = readElapsed(runningText, "标题角色复核进行中");
  checks.push(["运行中显示已运行总耗时", total1 !== null]);
  checks.push(["运行中显示当前阶段（标题角色复核进行中）耗时", stage1 !== null]);
  await page.waitForTimeout(4000);
  const runningText2 = await page.innerText("body");
  const total2 = readElapsed(runningText2, "已运行");
  const stage2 = readElapsed(runningText2, "标题角色复核进行中");
  checks.push(["总耗时真实递增", total1 !== null && total2 !== null && total2 > total1]);
  checks.push(["阶段耗时真实递增", stage1 !== null && stage2 !== null && stage2 > stage1]);

  // 完成后覆盖口径：分别显示第一遍定案数与第二遍必须定案数，而不是 completion 比例。
  mode = "completed";
  await page.waitForTimeout(2000);
  const completedText = await page.innerText("body");
  checks.push(["覆盖口径显示候选总数", completedText.includes("候选 348 个")]);
  checks.push(["覆盖口径显示第一遍规划定案 87", completedText.includes("第一遍规划定案 87")]);
  checks.push(["覆盖口径显示第二遍必须定案 261 个", completedText.includes("第二遍必须定案 261 个")]);
  checks.push(["抽查一致性指标可见且声明不产生建议", completedText.includes("抽查 18 项一致性 18/18") && completedText.includes("抽查结论只做指标")]);
  checks.push(["不再显示 second_pass/candidates 比例", !completedText.includes("261/348")]);
  checks.push(["本地目录采信时标注层级来源", completedText.includes("目录来自本地解析")]);
  checks.push(["显示自适应规划路径与边界", completedText.includes("规划路径：自适应分支") && completedText.includes("可靠区域 11") && completedText.includes("局部升级 0")]);

  // 思考配置保存 + 兼容默认。
  await page.getByRole("button", { name: "配置" }).first().click();
  await page.waitForTimeout(500);
  checks.push(["模型服务对话框打开", (await page.innerText("body")).includes("思考模式协议")]);
  const defaultStyle = await page.locator('.model-services-dialog select').first().inputValue().catch(() => "");
  await page.locator('label:has-text("思考模式协议") select').selectOption("qwen_thinking");
  await page.locator('label:has-text("思考开关") select').selectOption("off");
  await page.locator('label:has-text("思考预算") input').fill("2048");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForTimeout(1200);
  const services = await getJson(`${api}/model-services`);
  const saved = services.services.find(s => s.reasoning_style === "qwen_thinking");
  checks.push(["思考配置已保存到服务", Boolean(saved)]);
  checks.push(["思考开关与预算已保存", saved?.reasoning_effort === "off" && Number(saved?.reasoning_budget) === 2048]);
  // 新增服务默认不发送思考字段（兼容默认）。
  await page.getByRole("button", { name: "+ 新增" }).click();
  await page.waitForTimeout(300);
  const newStyle = await page.locator('label:has-text("思考模式协议") select').inputValue().catch(() => "x");
  checks.push(["新增服务思考协议默认留空", newStyle === ""]);
  checks.push(["兼容默认提示存在", defaultStyle === "" || newStyle === ""]);
  checks.push(["浏览器 0 脚本错误", consoleErrors.length === 0]);

  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
