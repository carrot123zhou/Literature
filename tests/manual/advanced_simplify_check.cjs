// 零高级配置验收（Playwright）。用法：node advanced_simplify_check.cjs <webPort> <apiPort> <projectId> <shotPath>
// 验证：高级选项整体移除（目录基准/视觉开关/本地工具按钮均不存在）；
// 档位 + 开始校准 + 按需展开的模型选择与纠错入口保留；
// 零配置启动行为：未配置模型时按钮禁用，配置后可启动并落库 coarse 作业；
// 已保存基准复用；纠错目录页优先于已保存基准；清空目录页恢复基准复用。
const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const webPort = Number(process.argv[2]);
const apiPort = Number(process.argv[3]);
const projectId = process.argv[4];
const shotPath = process.argv[5] || "advanced-simplify.png";
const web = `http://127.0.0.1:${webPort}`;
const api = `http://127.0.0.1:${apiPort}`;
const headers = { "x-literature-token": "", "content-type": "application/json" };

const getJson = async (url) => { const r = await fetch(url, { headers }); if (!r.ok) throw new Error(`${url} -> ${r.status}`); return r.json(); };
const sendJson = async (url, method, body) => {
  const r = await fetch(url, { method, headers, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${method} ${url} -> ${r.status}: ${await r.text()}`);
  return r.json();
};
const listJobs = async () => ((await getJson(`${api}/projects/${projectId}/status`)).jobs || []).filter(j => j.kind === "structure_ai");
const waitNewJob = async (knownIds, timeoutMs = 60000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const job = (await listJobs()).find(j => !knownIds.includes(j.job_id));
    if (job) return job;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`未创建新作业；已知=${JSON.stringify(knownIds)}`);
};
const waitJobStatus = async (jobId, statuses, timeoutMs = 60000) => {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const job = await getJson(`${api}/projects/${projectId}/structure/jobs/${jobId}`);
    if (statuses.includes(job.status)) return job;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`job ${jobId} 未在时限内到达 ${statuses}`);
};

const consoleErrors = [];
async function main() {
  const checks = [];
  let opened = false;
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));

  const openAssistant = async () => {
    // 首次 goto 进入；其后一律 reload（SPA 同 URL goto 不一定重新加载，角色绑定等状态不会刷新）。
    if (opened) await page.reload({ waitUntil: "networkidle" });
    else { await page.goto(`${web}/#/p/${encodeURIComponent(projectId)}/review`, { waitUntil: "networkidle" }); opened = true; }
    await page.waitForSelector(".review-outline", { timeout: 20000 });
    await page.waitForTimeout(1000);
    // SPA 同 URL goto 可能不重挂载 DOM：只在未展开时点 summary，避免把已展开的区块点关。
    const assistant = page.locator("details.review-assistant").first();
    if ((await assistant.getAttribute("open")) === null) await assistant.locator("> summary").click();
    await page.waitForTimeout(300);
  };

  // ---- A：移除的高级控件不再出现 ----
  await openAssistant();
  for (const [label, selector] of [
    ["高级选项折叠", ".review-advanced"],
    ["目录基准选择", "select[aria-label=目录基准来源]"],
    ["已确认目录输入", "textarea[aria-label=已确认的目录基准]"],
    ["待确认目录草稿", ".review-baseline"],
  ]) checks.push([`移除：${label}`, await page.locator(selector).count() === 0]);
  for (const label of ["对照原页图片", "只做本地校准", "本地读取目录", "预览启动输入", "确认当前审查范围", "自动采用高置信修正", "复核模式", "诊断覆盖"]) {
    checks.push([`移除：${label}`, await page.getByText(label, { exact: false }).count() === 0]);
  }

  // ---- B：保留的核心交互 ----
  checks.push(["保留：粗校/精校档位", await page.locator(".calib-modes button").count() === 2]);
  checks.push(["保留：开始校准按钮（未配置模型时禁用）", await page.locator("button.primary.block-button", { hasText: "开始校准" }).isDisabled()]);
  checks.push(["模型选择按需展开（未配置时展开）", await page.locator("details.task-model-details[open]").count() === 1]);
  const correct = page.locator("details.calib-correct").first();
  checks.push(["保留：目录识别不准纠错入口", await correct.count() === 1]);
  if ((await correct.getAttribute("open")) === null) await correct.locator("summary").click();
  await page.waitForTimeout(200);
  checks.push(["纠错入口：目录页输入", await correct.locator("input[aria-label=源文件目录页]").count() === 1]);
  await correct.locator("input[aria-label=源文件目录页]").fill("1");
  checks.push(["纠错入口：可填写目录页", (await correct.locator("input[aria-label=源文件目录页]").inputValue()) === "1"]);
  await correct.locator("input[aria-label=源文件目录页]").fill("");

  // ---- C：零配置启动（配置模型服务后）----
  const service = await sendJson(`${api}/model-services`, "POST", {
    name: "验收桩服务", preset: "custom", base_url: "http://127.0.0.1:1",
    models: [{ model: "mock-model", name: "Mock Model" }],
  });
  await sendJson(`${api}/model-services/${service.service_id}/key`, "PUT", { value: "sk-check" });
  await sendJson(`${api}/model-services/roles`, "PUT", { structure: { service_id: service.service_id, model: "mock-model" } });
  await openAssistant();
  checks.push(["配置后：开始校准可点", await page.locator("button.primary.block-button", { hasText: "开始校准" }).isEnabled()]);
  checks.push(["配置后：模型选择默认折叠", await page.locator("details.task-model-details[open]").count() === 0]);
  const clickStart = async () => {
    // 轮询落库可能领先 UI 状态（900ms 节奏），等按钮恢复可点再点。
    const btn = page.locator("button.primary.block-button", { hasText: "开始校准" }).first();
    for (let i = 0; i < 40 && !(await btn.isEnabled()); i += 1) await page.waitForTimeout(250);
    await btn.click();
  };
  await clickStart();
  // POST 返回前要做思考策略解析等准备工作，轮询等待作业落库。
  let jobs0 = [];
  for (let i = 0; i < 60; i += 1) {
    jobs0 = ((await getJson(`${api}/projects/${projectId}/status`)).jobs || []).filter(j => j.kind === "structure_ai");
    if (jobs0.length) break;
    await page.waitForTimeout(500);
  }
  const status0 = { jobs: jobs0 };
  checks.push(["零高级配置：作业已创建", jobs0.length === 1]);
  if (!jobs0.length) {
    let toast = "";
    try { toast = await page.locator(".toast").first().innerText({ timeout: 3000 }); } catch { /* 无提示 */ }
    throw new Error(`点击开始校准后未创建 structure_ai 作业；toast="${toast}"；status.jobs=${JSON.stringify((status0.jobs || []).map(j => ({ kind: j.kind, status: j.status })))}`);
  }
  const job1 = await waitJobStatus(jobs0[0].job_id, ["failed", "completed"]);
  const config1 = job1.config || {};
  checks.push(["启动请求只带档位与模型（mode=coarse）", config1.mode === "coarse" && config1.model === "mock-model"]);
  // config 是后端 StructureJobInput 展开后的完整参数快照：默认值（use_vision=false、toc=[]）在，
  // 但前端请求没有显式覆盖视觉或目录。
  checks.push(["启动请求不含视觉/已确认目录覆盖", config1.use_vision === false && (!config1.toc || config1.toc.length === 0)]);

  // ---- D：已保存基准复用；纠错目录页优先；清空恢复 ----
  await sendJson(`${api}/projects/${projectId}/structure/baseline`, "PUT", { toc: [{ title: "Chapter One", level: 1 }], source: "confirmed" });
  if ((await correct.getAttribute("open")) === null) await correct.locator("summary").click();
  await correct.locator("input[aria-label=源文件目录页]").fill("1");
  await clickStart();
  const job2 = await waitNewJob([job1.job_id]);
  if (!job2) throw new Error(`纠错启动未创建新作业：jobs=${JSON.stringify(jobs1.map(j => ({ id: j.job_id, status: j.status })))}, first=${job1.job_id}`);
  const config2 = (await waitJobStatus(job2.job_id, ["failed", "completed"])).config || {};
  checks.push(["纠错目录页优先：显式页生效", JSON.stringify(config2.toc_page_indexes) === "[0]"]);
  checks.push(["纠错目录页优先：不被已保存基准截住", (!config2.toc || config2.toc.length === 0) && config2.extract_toc === true]);
  // 失败后纠错面板重新折叠，先展开再清空。
  if ((await correct.getAttribute("open")) === null) await correct.locator("summary").click();
  await correct.locator("input[aria-label=源文件目录页]").fill("");
  await clickStart();
  const job3 = await waitNewJob([job1.job_id, job2.job_id]);
  const config3 = (await waitJobStatus(job3.job_id, ["failed", "completed"])).config || {};
  checks.push(["清空目录页：恢复复用已保存基准", (config3.toc || [])[0]?.title === "Chapter One" && config3.extract_toc === false]);

  await page.screenshot({ path: shotPath, fullPage: false });
  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: `${String(error)} | consoleErrors=${JSON.stringify(consoleErrors.slice(-5))}` })); process.exit(1); });
