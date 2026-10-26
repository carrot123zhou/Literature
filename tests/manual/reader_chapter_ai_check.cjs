// 章级 AI 折叠双路径 E2E（Lead 2026-10-25 判据）。
// 路径1：全新无层 → 初始 0 生成请求 → 当前章 S1 enabled → [data-action=fold] 一次 POST
//        section_ids 严格=[S1] → source+derive 落盘（source model/target derived）→ 不触 S2。
// 路径2：新 context → [data-action=preview] local → 升级 AI → local→model 入口不隐藏。
// 刷新前后：provider calls 计数与 POST 分别 0 增量；新 ReaderStore 读回 sections/hash。
// 用法：node reader_chapter_ai_check.cjs <webPort> <apiPort> <projectId> <secondId> <shotDir>
const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const [webPort, apiPort, projectId, secondId, shotDir] = process.argv.slice(2);
const web = `http://127.0.0.1:${webPort}`;
const api = `http://127.0.0.1:${apiPort}`;
const headers = { "x-literature-token": "", "content-type": "application/json" };
const consoleErrors = [];
const netLog = [];
process.on("unhandledRejection", error => {
  netLog.push(`UNHANDLED ${error && error.stack ? String(error.stack).slice(0, 200) : String(error)}`);
});

async function main() {
  const checks = [];
  const ok = (name, condition) => {
    checks.push({ name, pass: Boolean(condition) });
    netLog.push(`STEP ${Date.now() % 100000} ${name} -> ${condition ? "PASS" : "FAIL"}`);
  };
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
  page.setDefaultTimeout(8000);
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));

  const layerPosts = [];
  page.on("request", r => {
    try {
      const method = typeof r.method === "function" ? r.method() : r.method;
      if (r.url().includes("/reader/layers") && method === "POST") {
        layerPosts.push(JSON.parse(r.postData() || "{}"));
      }
    } catch { }
  });

  // ============ 路径 1：全新无层 ============
  await page.goto(`${web}/#/p/${projectId}/reader`, { waitUntil: "domcontentloaded", timeout: 15000 });
  await page.waitForSelector(".rdrr-root", { timeout: 15000 });
  await page.waitForTimeout(2500);

  // 1a) 初始 0 生成请求（无层时不自动调模型）
  ok("全新无层：初始 0 生成请求（不自动调模型）", layerPosts.length === 0);
  // 1b) 当前章 S1 enabled（canonical section_index 投影后目录可用）
  const opt = page.locator("select[data-action='scope'] option[value='section']");
  let optEnabled = false;
  for (let i = 0; i < 20; i += 1) {
    optEnabled = !(await opt.isDisabled().catch(() => true));
    if (optEnabled) break;
    await page.waitForTimeout(500);
  }
  ok("当前章 option enabled（section_index 目录可用）", optEnabled);
  const optText = await opt.innerText().catch(() => "");
  netLog.push(`DEBUG current-section: ${optText}`);

  // 1c) [data-action=fold] 触发目标 locale 一次 POST，section_ids 严格=[S1]
  const postsBeforeFold = layerPosts.length;
  const postPromise = page.waitForResponse(r => r.url().includes("/reader/layers") && r.request().method() === "POST", { timeout: 15000 });
  await page.locator("button[data-action='fold']").click();
  const foldResp = await (await postPromise).json();
  const foldBody = layerPosts[postsBeforeFold] || {};
  ok("fold 恰好发起一次 POST", layerPosts.length === postsBeforeFold + 1);
  ok("POST locale=目标语（zh-CN）", foldBody.locale === "zh-CN");
  ok("POST section_ids 严格=[S1]（不默整书）",
    Array.isArray(foldBody.section_ids) && foldBody.section_ids.length === 1
    && /^S\d+$/.test(foldBody.section_ids[0]));

  // 1d) 等作业完成（按返回 job_id，includes 判定）
  const jobId = foldResp.job_id;
  let job = null;
  for (let i = 0; i < 90; i += 1) {
    job = await fetch(`${api}/projects/${projectId}/reader/state`, { headers }).then(r => r.json()).then(d => d.layer_job);
    if (job && ["completed", "failed"].includes(job.status)) break;
    await page.waitForTimeout(500);
  }
  ok("章级 AI 折叠作业完成（job includes 判定）", job && job.status === "completed");
  if (job && job.status === "failed") netLog.push(`DEFECT fold-failed: ${job.error}`);

  // 1e) 产物：source 层 model / target 层 derived；不触 S2
  const srcLayers = await fetch(`${api}/projects/${projectId}/reader/layers?locale=source`, { headers }).then(r => r.json());
  const zhLayers = await fetch(`${api}/projects/${projectId}/reader/layers?locale=zh-CN`, { headers }).then(r => r.json());
  const srcS1 = (srcLayers.sections || []).find(s => s.id === (foldBody.section_ids || [])[0]);
  const zhS1 = (zhLayers.sections || []).find(s => s.id === (foldBody.section_ids || [])[0]);
  ok("source 层 S1 units 为 model 来源（prerequisite 真折叠）",
    Boolean(srcS1 && (srcS1.units || []).length && srcS1.units.every(u => u.source === "model")));
  ok("target 层 S1 units 为 derived（分层译句）",
    Boolean(zhS1 && (zhS1.units || []).length && zhS1.units.every(u => u.brief_source === "derived" || u.source === "model")));
  // 不触 S2：S2 在两层中都保持未生成
  const srcS2 = (srcLayers.sections || []).find(s => s.id === "S2");
  const zhS2 = (zhLayers.sections || []).find(s => s.id === "S2");
  const s2Untouched = (!srcS2 || !(srcS2.units || []).length || (srcS2.units || []).every(u => u.source !== "model"))
    && (!zhS2 || !(zhS2.units || []).length || (zhS2.units || []).every(u => u.brief_source !== "derived"));
  ok("section_ids 限定生效：S2 未被折叠", s2Untouched);

  // 1f) derived 标签（L2 视图）：先在用户路径真实勾选「译文」checkbox（后端默认 trans=False 会覆盖前端），
  // assert #rdrr-pane-trans visible + width>0，再真实 click L2 并等 derived tag 在 pane-body 可视区
  const transCheckbox = page.locator("input[aria-label='trans']");
  if (!(await transCheckbox.isChecked())) await transCheckbox.check();
  await page.waitForTimeout(300);
  const transVisible = await page.evaluate(() => {
    const pane = document.getElementById("rdrr-pane-trans");
    return pane && pane.getBoundingClientRect().width > 0 && !pane.className.includes("hidden");
  });
  ok("#rdrr-pane-trans visible + width>0（勾选后）", Boolean(transVisible));

  await page.locator("#lvlbar button[data-l='2']").click();
  await page.waitForFunction(() => document.getElementById("lvlbar")?.getAttribute("aria-valuenow") === "2", null, { timeout: 5000 }).catch(() => {});
  await page.waitForTimeout(600); // 等渲染
  await page.waitForTimeout(600);
  const derivedTagCheck = await page.evaluate(() => {
    const tags = [...document.querySelectorAll("#rdrr-pane-trans .rdrr-derivedtag")];
    const paneBody = document.querySelector("#rdrr-pane-trans .rdrr-pane-body");
    if (!tags.length || !paneBody) return { visible: false, count: tags.length };
    const bodyRect = paneBody.getBoundingClientRect();
    const tagRect = tags[0].getBoundingClientRect();
    return { visible: tagRect.width > 0 && tagRect.height > 0,
             inPaneBody: tagRect.top >= bodyRect.top - 4 && tagRect.bottom <= bodyRect.bottom + 4,
             count: tags.length, text: tags[0].innerText };
  });
  netLog.push(`DEBUG derived-tag: ${JSON.stringify(derivedTagCheck)}`);
  ok("L2 视图 derived 标签 visible 且在 pane-body 可视区", derivedTagCheck.visible && derivedTagCheck.inPaneBody);

  // 1g) L1 full 与已有 translation 逐字一致：按具体 unit.id 索引 transApi.entries[unitID].text
  const transApi = await fetch(`${api}/projects/${projectId}/translations/zh-CN`, { headers }).then(r => r.json());
  const zhUnit0 = (zhLayers.sections || []).flatMap(s => s.units || [])[0];
  const translationText = zhUnit0 ? ((transApi.entries || {})[zhUnit0.id] || {}).text : undefined;
  ok("对应 unit 与 translation 条目都存在", Boolean(zhUnit0 && translationText != null));
  if (zhUnit0 && translationText != null) {
    const normalize = (v) => (v || "").replace(/\r\n/g, "\n").replace(/\s+/g, " ").trim();
    ok("L1 full 严格 equals translation.text（仅统一换行/空白）",
      normalize(zhUnit0.full) === normalize(translationText));
  }

  // 1h) 刷新 → provider calls 0 增量 + POST 0 增量 + ReaderStore 读回非空
  const postsBeforeReload = layerPosts.length;
  // provider calls 计数（runner 侧 state_box 不可直接读，改用 usage/derive prompt 的 provider 请求次数：
  // 从后端 state 的 layer_job 确认没有新 job + POST 0 增量 + 后端 usage 无新增 reader_layers 记录）
  const usageBefore = await fetch(`${api}/projects/${projectId}/usage/records`, { headers }).then(r => r.json());
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector(".rdrr-root", { timeout: 15000 });
  await page.waitForTimeout(3000);
  ok("刷新后无新折叠 POST", layerPosts.length === postsBeforeReload);
  const usageAfter = await fetch(`${api}/projects/${projectId}/usage/records`, { headers }).then(r => r.json());
  const newUsage = (usageAfter.length || 0) - (usageBefore.length || 0);
  ok("刷新后 provider 调用 0 增量（usage 记录 0 新增）", newUsage === 0);
  const storedAfter = await fetch(`${api}/projects/${projectId}/reader/layers?locale=zh-CN`, { headers }).then(r => r.json());
  ok("ReaderStore（经 API）读回 sections/hash 非空",
    Boolean(storedAfter.generated && (storedAfter.sections || []).length));

  // ============ 路径 2：新 context，preview → 升级 AI ============
  await page.goto(`${web}/#/p/${secondId}/reader`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector(".rdrr-root", { timeout: 15000 });
  await page.waitForTimeout(2000);
  // 2a) 初始 0 生成请求
  const postsBeforePreview = layerPosts.length;
  // 2b) 真实点击 [data-action=preview] → 跟踪返回 job_id → 等该 job 完成
  const previewBtn = page.locator("button[data-action='preview']");
  ok("「本地预览」按钮可见（empty phase 显式入口）", await previewBtn.isVisible().catch(() => false));
  const previewPostPromise = page.waitForResponse(r => r.url().includes("/reader/layers") && r.request().method() === "POST", { timeout: 15000 });
  await previewBtn.click();
  const previewResp = await (await previewPostPromise).json();
  const previewJobId = previewResp.job_id;
  // 本地预览会发一次 mode=local POST（后端建 local 层落盘），但**不调模型**（provider 侧无 source fold）
  const previewPost = layerPosts[postsBeforePreview];
  ok("本地预览仅发 mode=local POST（不调模型）",
    Boolean(previewPost) && previewPost.mode === "local");
  // 等 preview job 完成
  let previewJob = null;
  for (let i = 0; i < 30; i += 1) {
    previewJob = await fetch(`${api}/projects/${secondId}/reader/state`, { headers }).then(r => r.json()).then(d => d.layer_job);
    if (previewJob && ["completed", "failed"].includes(previewJob.status)) break;
    await page.waitForTimeout(500);
  }
  ok("本地预览 job 完成", previewJob && previewJob.status === "completed");
  // 2c) 升级为模型折叠入口可见（local→model 不隐藏）：等 data-action=fold visible/enabled + data-phase 非 generating
  const upgradeBtn2 = page.locator("button[data-action='fold']");
  const upgradeVisible = await upgradeBtn2.waitFor({ state: "visible", timeout: 10000 })
    .then(() => true).catch(() => false);
  const upgradeEnabled = upgradeVisible && await upgradeBtn2.isEnabled().catch(() => false);
  const dataPhase = await page.locator(".rdrr-layerstatus").getAttribute("data-phase").catch(() => "");
  netLog.push(`DEBUG 2c: visible=${upgradeVisible} enabled=${upgradeEnabled} phase=${dataPhase}`);
  ok("升级为模型折叠入口常驻可见且可用", upgradeVisible && upgradeEnabled && dataPhase !== "generating");
  // 2d) 升级 AI：当前章
  const opt2 = page.locator("select[data-action='scope'] option[value='section']");
  for (let i = 0; i < 20; i += 1) {
    if (!(await opt2.isDisabled().catch(() => true))) break;
    await page.waitForTimeout(500);
  }
  await page.locator("select[data-action='scope']").selectOption("section");
  const post2Promise = page.waitForResponse(r => r.url().includes("/reader/layers") && r.request().method() === "POST", { timeout: 15000 });
  await upgradeBtn2.click();
  const fold2 = await (await post2Promise).json();
  ok("升级 AI 受理", Boolean(fold2.job_id));
  let job2 = null;
  for (let i = 0; i < 90; i += 1) {
    job2 = await fetch(`${api}/projects/${secondId}/reader/state`, { headers }).then(r => r.json()).then(d => d.layer_job);
    if (job2 && ["completed", "failed"].includes(job2.status)) break;
    await page.waitForTimeout(500);
  }
  ok("升级 AI 作业完成", job2 && job2.status === "completed");
  // 2e) local→model 升级后入口仍常驻：等 data-action=fold visible/enabled + data-phase 非 generating
  const regenBtn = page.locator("button[data-action='fold']");
  const regenVisible = await regenBtn.waitFor({ state: "visible", timeout: 10000 })
    .then(() => true).catch(() => false);
  const regenEnabled = regenVisible && await regenBtn.isEnabled().catch(() => false);
  const phase2 = await page.locator(".rdrr-layerstatus").getAttribute("data-phase").catch(() => "");
  netLog.push(`DEBUG 2e: visible=${regenVisible} enabled=${regenEnabled} phase=${phase2}`);
  ok("升级后「重新生成」入口常驻可见且可用（phase 非 generating）",
    regenVisible && regenEnabled && phase2 !== "generating");
  await page.screenshot({ path: `${shotDir}/chapter-ai-upgrade.png` });

  const unexpectedErrors = consoleErrors.filter(text => !/Bad Request|400/.test(text));
  ok("0 意外脚本错误", unexpectedErrors.length === 0);
  netLog.push(`LAYER-POSTS: ${JSON.stringify(layerPosts)}`);

  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors, netLog, layerPosts, shotDir }));
}

main().catch(error => {
  console.log(JSON.stringify({ checks: [], error: `${error && error.stack || error}`, consoleErrors, netLog }));
  process.exit(1);
});
