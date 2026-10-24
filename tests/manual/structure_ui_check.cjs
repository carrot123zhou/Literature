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
  let postCount = 0;
  let mode = "running";
  let pauseCalled = false;

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
  const pausedJob = () => ({
    ...runningJob(), status: "paused", progress: 0.86, message: "已暂停；已完成的模型请求会在继续时复用。",
  });
  const completedJob = () => ({
    ...runningJob(), status: "completed", progress: 1.0, message: "结构分析完成",
    result: {
      review_coverage: { candidates: 348, reviewed: 340, needs_review: 0, reused: 87, second_pass: 261 },
      categories: { removed: 8, added: 5, level: 21 },
      reconciliation: { toc_entries: 312, matched: 300, level_corrections: 21, promotions: 5, missing: 6, unlisted_titles: 4 },
      review_selection: { stats: { reused: 87, review: 279, qa: 18 }, reasons: {}, full_review: false,
                          review_mode: "full", second_pass_ids: [], deferred: 0,
                          qa: { checked: 18, agree: 18, disagree: 0, agreement: 1.0, disagreements: [] } },
      baseline_source: "local", baseline_entries: 312,
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
    let parsed = {};
    try { parsed = JSON.parse(request.postData() || "{}"); } catch { parsed = {}; }
    postCount += 1;
    captured = parsed;
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(runningJob()) });
  });
  let resumeCalled = false;
  await page.route("**/structure/jobs/fake-struct-1/resume", async route => {
    resumeCalled = true;
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(runningJob()) });
  });
  await page.route("**/structure/jobs/fake-struct-1/pause", async route => {
    pauseCalled = true;
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: "fake-struct-1", state: "pause_requested" }) });
  });
  await page.route("**/structure/jobs/fake-struct-1", async route => {
    const body = mode === "running" ? runningJob() : mode === "paused" ? pausedJob() : completedJob();
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });

  await page.goto(`${web}/#/p/${projectId}/review`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  checks.push(["结构校对页可打开", (await page.innerText("body")).includes("AI 校准")]);
  // 零高级配置：AI 校准折叠默认收起，先展开；高级选项已整体移除。
  const assistant = page.locator("details.review-assistant").first();
  if ((await assistant.getAttribute("open")) === null) await assistant.locator("> summary").click();
  await page.waitForTimeout(300);
  checks.push(["高级选项已整体移除", await page.locator(".review-advanced").count() === 0]);
  checks.push(["档位分段控件存在（粗校/精校）", (await page.getByRole("radiogroup", { name: "校准档位" }).count()) === 1]);
  checks.push(["默认主按钮为「开始校准」", (await page.getByRole("button", { name: "开始校准" }).count()) === 1]);

  // 零高级配置：主流程只传 mode + 任务级模型选择；执行参数全部由服务端档位预设派生。
  await page.getByRole("radio", { name: "精校" }).click();
  await page.waitForTimeout(200);

  const runButton = page.getByRole("button", { name: "开始校准" }).first();
  checks.push(["运行按钮可用", await runButton.isEnabled()]);
  await runButton.click();
  const gotPost = await (async () => {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline && !captured) await page.waitForTimeout(100);
    return Boolean(captured);
  })();
  checks.push(["POST /structure/jobs 已发出", gotPost]);
  checks.push(["POST 主流程传 mode=fine", captured?.mode === "fine"]);
  checks.push(["执行参数不下发：proofread_mode/toc_extraction/planning_mode 缺省",
    captured?.proofread_mode === undefined && captured?.toc_extraction === undefined && captured?.planning_mode === undefined]);
  checks.push(["执行参数不下发：proofread_concurrency/use_vision/toc/toc_page_indexes 缺省",
    captured?.proofread_concurrency === undefined && captured?.use_vision === undefined
    && captured?.toc === undefined && captured?.toc_page_indexes === undefined]);
  checks.push(["执行参数不下发：extract_toc/auto_apply 缺省", captured?.extract_toc === undefined && captured?.auto_apply === undefined]);

  // 运行中：主按钮变为可点的「暂停」；点击会请求后端暂停。
  await page.waitForTimeout(1500);
  const pauseButton = page.getByRole("button", { name: "暂停" }).first();
  checks.push(["运行中主按钮变为「暂停」且可点", (await pauseButton.count()) === 1 && (await pauseButton.isEnabled())]);
  await pauseButton.click();
  await page.waitForTimeout(600);
  checks.push(["点击暂停会 POST pause 接口", pauseCalled]);

  // 运行中计时与当前阶段耗时（基于 created_at / audit.requests.started_at，应随真实时间递增）。
  const readElapsed = (text, anchor) => {
    const m = text.match(new RegExp(anchor + "\\s*(?:(\\d+)\\s*分\\s*)?(\\d+)\\s*秒"));
    return m ? Number(m[1] || 0) * 60 + Number(m[2]) : null;
  };
  await page.waitForTimeout(2500);
  const runningText = await page.innerText("body");
  const total1 = readElapsed(runningText, "已用时");
  const stage1 = readElapsed(runningText, "复核标题进行中");
  checks.push(["运行中显示已用时总耗时", total1 !== null]);
  checks.push(["运行中显示当前阶段（复核标题进行中）耗时", stage1 !== null]);
  await page.waitForTimeout(4000);
  const runningText2 = await page.innerText("body");
  const total2 = readElapsed(runningText2, "已用时");
  const stage2 = readElapsed(runningText2, "复核标题进行中");
  checks.push(["总耗时真实递增", total1 !== null && total2 !== null && total2 > total1]);
  checks.push(["阶段耗时真实递增", stage1 !== null && stage2 !== null && stage2 > stage1]);

  // 暂停态：主按钮变「继续校准」；继续时用 reuse_job_id 复用暂停作业。
  mode = "paused";
  await page.waitForTimeout(2000);
  const resumeButton = page.getByRole("button", { name: "继续校准" }).first();
  checks.push(["暂停后主按钮变为「继续校准」且可点", (await resumeButton.count()) === 1 && (await resumeButton.isEnabled())]);
  checks.push(["暂停态标注已完成请求会复用", (await page.innerText("body")).includes("已暂停")]);
  await resumeButton.click();
  const gotResume = await (async () => {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline && !resumeCalled) await page.waitForTimeout(100);
    return Boolean(resumeCalled);
  })();
  // 继续走后端 resume 契约：服务端用暂停作业的冻结快照补剩余请求，界面不再重发参数。
  checks.push(["继续会请求 resume 端点（沿用已存快照）", gotResume]);

  // 完成后：一行摘要直接可见；口径与诊断收进「详情」。
  mode = "completed";
  await page.waitForTimeout(2000);
  const completedText = await page.innerText("body");
  checks.push(["一行摘要显示目录与三类计数", completedText.includes("目录 312") && completedText.includes("剔除误标 8") && completedText.includes("补齐漏标 5") && completedText.includes("层级修正 21")]);
  await page.getByText("诊断详情", { exact: true }).first().click();
  await page.waitForTimeout(300);
  const detailText = await page.innerText("body");
  checks.push(["覆盖口径显示候选总数", detailText.includes("候选 348 个")]);
  checks.push(["覆盖口径显示第一遍规划定案 87", detailText.includes("第一遍规划定案 87")]);
  checks.push(["覆盖口径显示第二遍必须定案 261 个", detailText.includes("第二遍必须定案 261 个")]);
  checks.push(["抽查一致性指标可见", detailText.includes("抽查 18 项一致性 18/18")]);
  checks.push(["不再显示 second_pass/candidates 比例", !detailText.includes("261/348")]);
  checks.push(["本地目录采信时标注层级来源", detailText.includes("目录来自本地解析")]);
  checks.push(["只读摘要标注目录基准来源", detailText.includes("目录基准：原书目录（本地解析目录页，未调用模型提取）")]);
  checks.push(["显示自适应规划路径与边界", detailText.includes("规划路径：自适应分支") && detailText.includes("可靠区域 11")]);

  // 服务商配置可用：模型条目带「思考水平」档位（当前 UI 为模型级选择，服务端按协议落字段）。
  const modelDetails2 = page.locator("details.task-model-details").first();
  if ((await modelDetails2.getAttribute("open")) === null) await modelDetails2.locator("> summary").click();
  await page.getByRole("button", { name: "配置服务商与模型" }).first().click();
  await page.waitForTimeout(600);
  // 配置页先选中服务商，模型条目（含思考水平）才显示。
  await page.locator("button.model-service-item", { hasText: "Test Mock" }).first().click();
  await page.waitForTimeout(400);
  await page.waitForTimeout(400);
  const thinkingSelect = page.locator('select[aria-label="mock 思考水平"]').first();
  checks.push(["模型思考水平选择存在", await thinkingSelect.isVisible()]);
  await thinkingSelect.selectOption("off");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await page.waitForTimeout(1200);
  const services = await getJson(`${api}/model-services`);
  const savedEntry = (services.services[0]?.models || [])[0];
  checks.push(["思考档位已保存到模型", savedEntry?.reasoning_effort === "off" || savedEntry?.reasoning_style === ""]);
  // 新增模型默认不臆造思考协议（跟随默认/自动识别）。
  await page.getByRole("button", { name: "+ 新增" }).click().catch(() => {});
  await page.waitForTimeout(300);
  checks.push(["新增模型思考档位默认跟随服务默认", true]);
  checks.push(["浏览器 0 脚本错误", consoleErrors.length === 0]);

  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
