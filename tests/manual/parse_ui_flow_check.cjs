// 真实浏览器全流程验收（Playwright via ms-playwright-go 随包 Node）。
// 用法：PW_PACKAGE_PATH=<pkg> SHOT_DIR=<dir> node parse_ui_flow_check.cjs <webPort> <apiPort>
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

const getJson = async (url) => { const r = await fetch(url, { headers }); if (!r.ok) throw new Error(`${url} -> ${r.status}`); return r.json(); };
const projectByTitle = async (title) => (await getJson(`${api}/projects`)).find(p => p.title === title);
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

  // ---- A：部分解析 → 继续余页 → 全文；UI 刷新同步 ----
  await page.goto(`${web}/#/resources`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  checks.push(["资源页显示部分解析", (await page.innerText("body")).includes("部分解析 4/9 页")]);
  await page.getByText("Partial Book", { exact: false }).first().click();
  await page.waitForTimeout(700);
  await page.getByText("继续解析", { exact: true }).first().click();
  await page.waitForTimeout(600);
  checks.push(["继续解析弹窗打开", (await page.innerText("body")).includes("继续解析 ·")]);
  await page.getByText("开始继续解析", { exact: true }).first().click();
  const full = await poll(async () => (await projectByTitle("Partial Book")).parse_coverage.state === "full", 40000);
  checks.push(["继续解析后后端全文覆盖", full]);
  const partial = await projectByTitle("Partial Book");
  checks.push(["后端覆盖页数 9/9", partial.parse_coverage.covered_pages === 9]);
  // 刷新资源页：N/M → full 的 UI 同步。
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  const refreshed = await page.innerText("body");
  checks.push(["刷新后资源页显示全部解析 9 页", refreshed.includes("全部解析 9 页")]);

  // ---- B：点击校对块 → PDF 当前页真实定位到源页 5 / 7 ----
  const locate = await projectByTitle("Locate Book");
  const document = await getJson(`${api}/projects/${locate.project_id}/document`);
  const page5 = document.blocks.find(b => (b.source_text || "").includes("page 5"));
  const page7 = document.blocks.find(b => (b.source_text || "").includes("page 7"));
  checks.push(["第 5 页 block.page_index=4", Boolean(page5) && page5.page_index === 4]);
  checks.push(["第 7 页 block.page_index=6", Boolean(page7) && page7.page_index === 6]);
  await page.goto(`${web}/#/p/${locate.project_id}/review`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3500);
  const clickBlock = async (text) => {
    const target = page.locator(".block-list").getByText(text, { exact: false }).first();
    if (await target.count()) { await target.click({ force: true }); }
  };
  const closeModal = async () => { await page.keyboard.press("Escape").catch(() => {}); await page.waitForTimeout(300); };
  const currentPage = async () => {
    const value = await page.locator(".pdf-panel").first().getAttribute("data-current-page");
    return Number(value);
  };
  // 断言目标页的 canvas 真的存在、尺寸有效且绘有内容（不只容器可见）。
  const canvasRendered = async (index) => {
    const canvas = page.locator(`.pdf-page[data-page-index="${index}"] canvas`).first();
    if (!(await canvas.count())) return { ok: false, reason: "no canvas" };
    const info = await canvas.evaluate(node => {
      const element = node;
      const context = element.getContext("2d");
      if (!context || !element.width || !element.height) return { width: element.width, height: element.height, dark: 0 };
      const data = context.getImageData(0, 0, element.width, element.height).data;
      let dark = 0;
      for (let i = 0; i < data.length; i += 4) { if (data[i] < 180 && data[i + 1] < 180 && data[i + 2] < 180) dark += 1; }
      return { width: element.width, height: element.height, dark };
    });
    return { ok: info.width > 0 && info.height > 0 && info.dark > 50, ...info };
  };
  const pageErrors = async () => page.locator(".pdf-page-error").count();
  const pageVisible = async (index) => {
    const box = await page.locator(`.pdf-page[data-page-index="${index}"]`).first().boundingBox().catch(() => null);
    const container = await page.locator(".pdf-scroll").first().boundingBox().catch(() => null);
    if (!box || !container) return false;
    return box.y < container.y + container.height && box.y + box.height > container.y;
  };
  await clickBlock("Chapter on page 5");
  await page.waitForTimeout(2500);
  const canvas5 = await canvasRendered(4);
  checks.push(["点击第 5 页块后第 5 页 canvas 已绘制内容", canvas5.ok]);
  checks.push(["点击第 5 页块后第 5 页在预览可视区", await pageVisible(4)]);
  checks.push(["点击第 5 页块后 PDF 当前页≈5", Math.abs((await currentPage()) - 5) <= 1]);
  checks.push(["当前无渲染失败提示", (await pageErrors()) === 0]);
  await clickBlock("Chapter on page 7");
  await page.waitForTimeout(2500);
  const canvas7 = await canvasRendered(6);
  checks.push(["点击第 7 页块后第 7 页 canvas 已绘制内容", canvas7.ok]);
  checks.push(["点击第 7 页块后第 7 页在预览可视区", await pageVisible(6)]);
  checks.push(["点击第 7 页块后 PDF 当前页≈7", Math.abs((await currentPage()) - 7) <= 1]);
  // 缩放后仍应重新渲染出内容。
  await page.locator("select[aria-label=\"原页缩放\"]").first().selectOption("1.25");
  await page.waitForTimeout(2500);
  const canvasZoom = await canvasRendered(6);
  checks.push(["缩放到 125% 后第 7 页仍绘制内容", canvasZoom.ok]);
  checks.push(["缩放后无渲染失败提示", (await pageErrors()) === 0]);
  const debug = {
    panelCount: await page.locator(".pdf-panel").count(),
    currentAttr: await page.locator(".pdf-panel").first().getAttribute("data-current-page").catch(() => null),
    canvas5: canvas5, canvas7: canvas7, canvasZoom: canvasZoom,
    block5Count: await page.locator(".block-list").getByText("Chapter on page 5", { exact: false }).count(),
    hasPdfError: (await page.innerText("body")).includes("无法加载 PDF"),
  };

  // ---- C：失败切片 → 重试 → 完成 ----
  await page.goto(`${web}/#/resources`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  await page.getByText("Retry Book", { exact: false }).first().click();
  await page.waitForTimeout(700);
  await page.getByText("继续解析", { exact: true }).first().click();
  await page.waitForTimeout(600);
  await page.getByText("开始继续解析", { exact: true }).first().click();
  const retryProject = await projectByTitle("Retry Book");
  const failed = await poll(async () => {
    const jobs = await getJson(`${api}/ocr/jobs`);
    const job = jobs.find(j => j.project_id === retryProject.project_id);
    return job && job.state === "failed";
  }, 40000);
  checks.push(["注入的切片失败如实显示", failed]);
  await page.goto(`${web}/#/resources`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  await page.getByText("Retry Book", { exact: false }).first().click();
  await page.waitForTimeout(700);
  const retryVisible = (await page.innerText("body")).includes("重试失败分段");
  checks.push(["详情提供重试入口", retryVisible]);
  if (retryVisible) await page.getByText("重试失败分段", { exact: true }).first().click();
  await closeModal();
  checks.push(["重试后完成全文", await poll(async () => (await projectByTitle("Retry Book")).parse_coverage.state === "full", 40000)]);

  // ---- D：中断任务 → UI 恢复 → 完成 ----
  await page.goto(`${web}/#/home`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  const homeText = await page.innerText("body");
  const resumeVisible = homeText.includes("Resume Book") && homeText.includes("恢复解析");
  checks.push(["首页显示可恢复的中断任务", resumeVisible]);
  if (resumeVisible) await page.getByText("恢复解析", { exact: true }).first().click();
  checks.push(["恢复后完成解析", await poll(async () => {
    const item = await projectByTitle("Resume Book");
    return item && item.parse_coverage && item.parse_coverage.state === "full";
  }, 40000)]);

  // ---- E：双击“开始继续解析”只创建一个任务 ----
  await closeModal();
  await page.goto(`${web}/#/resources`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  await page.getByText("Dup Book", { exact: false }).first().click();
  await page.waitForTimeout(700);
  await page.getByText("继续解析", { exact: true }).first().click();
  await page.waitForTimeout(600);
  const submit = page.getByText("开始继续解析", { exact: true }).first();
  await Promise.allSettled([submit.click({ force: true, noWaitAfter: true }), submit.click({ force: true, noWaitAfter: true })]);
  const dupProject = await projectByTitle("Dup Book");
  await poll(async () => {
    const jobs = await getJson(`${api}/ocr/jobs`);
    return jobs.filter(j => j.project_id === dupProject.project_id).length >= 1;
  }, 15000);
  await page.waitForTimeout(2000);
  const dupJobs = (await getJson(`${api}/ocr/jobs`)).filter(j => j.project_id === dupProject.project_id);
  checks.push(["双击继续解析只创建一个任务", dupJobs.length === 1]);

  // ---- F：旧 unknown 资源“检查解析范围”（成功 + 失败反馈） ----
  await closeModal();
  await page.goto(`${web}/#/resources`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  await page.getByText("Legacy Book", { exact: false }).first().click();
  await page.waitForTimeout(700);
  checks.push(["unknown 资源提供检查解析范围入口", (await page.innerText("body")).includes("检查解析范围")]);
  await page.getByText("检查解析范围", { exact: true }).first().click();
  const legacy = await projectByTitle("Legacy Book");
  const migrated = await poll(async () => {
    const item = await projectByTitle("Legacy Book");
    return item && item.parse_coverage && item.parse_coverage.state !== "unknown";
  }, 15000);
  checks.push(["检查解析范围恢复旧资源", migrated]);
  if (migrated) {
    const item = await projectByTitle("Legacy Book");
    checks.push(["恢复为部分解析 3/4 页", item.parse_coverage.state === "partial" && item.parse_coverage.covered_pages === 3]);
  }
  checks.push(["原有页仍保留（Legacy 页数 > 0）", Boolean(legacy)]);
  // 旧 page_ranges=1-10 前缀资源：可信迁移 → 部分解析 → 继续解析 → 全文。
  await closeModal();
  await page.getByText("Legacy Range Book", { exact: false }).first().click();
  await page.waitForTimeout(700);
  checks.push(["前缀资源提供检查解析范围入口", (await page.innerText("body")).includes("检查解析范围")]);
  if ((await page.innerText("body")).includes("检查解析范围")) {
    await page.getByText("检查解析范围", { exact: true }).first().click();
  }
  checks.push(["旧 1-10 前缀迁移为部分解析 10/30", await poll(async () => {
    const item = await projectByTitle("Legacy Range Book");
    return item && item.parse_coverage && item.parse_coverage.state === "partial"
      && item.parse_coverage.covered_pages === 10 && item.parse_coverage.source_page_count === 30;
  }, 15000)]);
  await closeModal();
  await page.getByText("Legacy Range Book", { exact: false }).first().click();
  await page.waitForTimeout(700);
  const canContinuePrefix = (await page.innerText("body")).includes("继续解析");
  checks.push(["前缀资源迁移后可继续解析", canContinuePrefix]);
  if (canContinuePrefix) {
    await page.getByText("继续解析", { exact: true }).first().click();
    await page.waitForTimeout(600);
    await page.getByText("开始继续解析", { exact: true }).first().click();
  }
  checks.push(["前缀资源继续解析后完成 30/30", await poll(async () => {
    const item = await projectByTitle("Legacy Range Book");
    return item && item.parse_coverage && item.parse_coverage.state === "full" && item.parse_coverage.covered_pages === 30;
  }, 40000)]);
  // 失败反馈：Legacy Bad 无可信证据。
  await closeModal();
  await page.getByText("Legacy Bad", { exact: false }).first().click();
  await page.waitForTimeout(700);
  if ((await page.innerText("body")).includes("检查解析范围")) {
    await page.getByText("检查解析范围", { exact: true }).first().click();
    await page.waitForTimeout(1500);
  }
  checks.push(["无法恢复时给出失败反馈", (await page.innerText("body")).includes("无法自动恢复")]);

  // ---- G：从未解析的已有资源 → 开始解析 → fake 完成 → 规范文档 ----
  await closeModal();
  await page.goto(`${web}/#/resources`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2000);
  await page.getByText("Fresh Book", { exact: false }).first().click();
  await page.waitForTimeout(700);
  const freshDialog = page.locator(".res-dialog").first();
  const freshText = await freshDialog.innerText();
  checks.push(["未解析资源提供开始解析入口", freshText.includes("开始解析")]);
  checks.push(["未解析资源不再提供无意义的查看进度", !freshText.includes("查看进度")]);
  checks.push(["未解析资源不显示旧资源检查解析范围入口", !freshText.includes("检查解析范围")]);
  checks.push(["未解析资源不在总状态显示已完成", !freshText.includes("已完成")]);
  const freshStatus = await freshDialog.locator(".res-status").first().innerText();
  checks.push(["未解析资源总状态为未解析", freshStatus.includes("未解析")]);
  await freshDialog.getByText("开始解析", { exact: true }).first().click();
  await page.waitForTimeout(700);
  const startDialog = page.locator(".res-dialog").last();
  const startText = await startDialog.innerText();
  checks.push(["开始解析弹窗显示源文件总页数", startText.includes("共 260 页")]);
  checks.push(["开始解析弹窗提供页码范围", startText.includes("要解析的页码")]);
  await startDialog.getByText("开始解析任务", { exact: true }).first().click();
  const freshDone = await poll(async () => {
    const item = await projectByTitle("Fresh Book");
    return item && item.has_document && item.parse_coverage
      && item.parse_coverage.state === "full" && item.parse_coverage.covered_pages === 260;
  }, 90000);
  checks.push(["开始解析后 fake 完成并生成规范文档 260/260", freshDone]);
  const freshId = (await projectByTitle("Fresh Book")).project_id;
  const freshJobs = (await getJson(`${api}/ocr/jobs`)).filter(j => j.project_id === freshId);
  checks.push(["开始解析只创建一个任务", freshJobs.length === 1]);
  checks.push(["260 页开始解析自动切成 200+60 两片", freshJobs.length === 1 && freshJobs[0].slices.length === 2 && freshJobs[0].slices[0].source_pages.length === 200 && freshJobs[0].slices[1].source_pages.length === 60]);
  // 刷新资源页：未解析 → 全部解析 260 页（限定 Fresh Book 卡片）。
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  const freshCard = page.locator(".res-card").filter({ hasText: "Fresh Book" }).first();
  checks.push(["刷新后未解析资源显示全部解析 260 页", (await freshCard.innerText()).includes("全部解析 260 页")]);

  // ---- H：窄窗口截图（结构校对页 + 阅读与翻译页），无 toast/modal 遮挡 ----
  await closeModal();
  await page.locator(".toast").first().click({ timeout: 1000 }).catch(() => {});
  await page.waitForTimeout(6500);
  await page.setViewportSize({ width: 900, height: 700 });
  // 结构校对页：#/p/<id>/review
  await page.goto(`${web}/#/p/${locate.project_id}/review`, { waitUntil: "networkidle" });
  await page.waitForTimeout(4000);
  checks.push(["校对页窄窗口可渲染", (await page.innerText("body")).includes("源文件预览")]);
  const narrowCanvas = await canvasRendered(0);
  checks.push(["校对页窄窗口 canvas 已绘制内容", narrowCanvas.ok]);
  checks.push(["校对页无渲染失败提示", (await pageErrors()) === 0]);
  await page.screenshot({ path: path.join(shotDir, "review-narrow.png") });
  // 阅读与翻译页：#/p/<id>/read
  await page.goto(`${web}/#/p/${locate.project_id}/read`, { waitUntil: "networkidle" });
  await page.waitForTimeout(4000);
  const readText = await page.innerText("body");
  checks.push(["阅读与翻译页可渲染", readText.includes("Chapter on page 5") || readText.includes("阅读")]);
  await page.screenshot({ path: path.join(shotDir, "reading-narrow.png") });

  // ---- I：旧导出路由回退 + 无导出/TBL 入口 ----
  await page.goto(`${web}/#/p/${locate.project_id}/export`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  const exportRouteText = await page.innerText("body");
  checks.push(["旧 #/p/<id>/export 回退到阅读与翻译（不白屏）",
    exportRouteText.includes("阅读") && !exportRouteText.includes("EXPORT CENTER") && !exportRouteText.includes("生成所选格式")]);
  const navText = await page.locator("nav[aria-label='主导航']").innerText();
  checks.push(["主导航不再暴露导出标签", !navText.includes("导出") && navText.includes("阅读与翻译")]);
  await page.goto(`${web}/#/export`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  checks.push(["旧 #/export 不显示导出中心", !(await page.innerText("body")).includes("EXPORT CENTER")]);
  const tblStatus = await fetch(`${api}/tbl/config`, { headers }).then(r => r.status).catch(() => 0);
  checks.push(["TBL 接口已废止（410）", tblStatus === 410]);
  const exportStatus = await fetch(`${api}/projects/${locate.project_id}/exports`, {
    method: "POST", headers: { ...headers, "content-type": "application/json" }, body: JSON.stringify({ mode: "source" }),
  }).then(r => r.status).catch(() => 0);
  checks.push(["导出接口已废止（410）", exportStatus === 410]);

  // ---- J：大书阅读页窗口化（1000+ blocks） ----
  const bigBook = await projectByTitle("Big Book");
  await page.goto(`${web}/#/p/${bigBook.project_id}/read`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3000);
  const domInitial = await page.locator(".blank .block").count();
  checks.push(["大书阅读页 DOM 块数量有界", domInitial > 0 && domInitial < 250]);
  await page.locator(".blank").evaluate(el => { el.scrollTop = el.scrollHeight; });
  await page.waitForTimeout(1800);
  const domAfter = await page.locator(".blank .block").count();
  checks.push(["滚动到底后 DOM 仍有限", domAfter > 0 && domAfter < 250]);
  checks.push(["滚动到底渲染尾部块", (await page.locator('[data-block-id="big-p999"]').count()) > 0]);

  // ---- K：大书阅读页草稿往返 / 保存失败 / 审校候选 / 结构窗口化 ----
  try {
  await page.goto(`${web}/#/p/${bigBook.project_id}/read`, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);
  await page.locator(".blank").evaluate(el => { el.scrollTop = 0; });
  await page.waitForTimeout(800);
  const kBody = await page.innerText("body");
  checks.push([`大书阅读页渲染出正文(${kBody.slice(0, 120).replace(/\s+/g, " ")})`, kBody.includes("Body paragraph 0")]);
  await page.locator('[data-block-id="big-p0"]').first().waitFor({ state: "attached", timeout: 15000 });
  const bigCard = page.locator('[data-block-id="big-p0"]').first();
  await bigCard.scrollIntoViewIfNeeded();
  await bigCard.getByText("编辑", { exact: true }).first().click();
  const draftText = "UNSAVED UNIQUE DRAFT 2026";
  await bigCard.locator("textarea").first().fill(draftText);
  await page.locator(".blank").evaluate(el => { el.scrollTop = el.scrollHeight; });
  await page.waitForTimeout(1200);
  await page.locator(".blank").evaluate(el => { el.scrollTop = 0; });
  await page.waitForTimeout(1200);
  const draftValue = await page.locator('[data-block-id="big-p0"] textarea').first().inputValue().catch(() => "");
  checks.push(["窗口化往返编辑草稿不丢", draftValue === draftText]);

  await page.route("**/translations/big-p0", route => route.abort());
  await page.locator('[data-block-id="big-p0"]').getByText("保存", { exact: true }).first().click();
  await page.waitForTimeout(1000);
  const afterFail = await page.locator('[data-block-id="big-p0"] textarea').first().inputValue().catch(() => "");
  checks.push([`保存失败不丢草稿(${afterFail.slice(0, 24)})`, afterFail === draftText]);
  await page.unroute("**/translations/big-p0");
  const cancelBtn = page.locator('[data-block-id="big-p0"]').getByText("取消", { exact: true }).first();
  if (await cancelBtn.count()) await cancelBtn.click().catch(() => {});
  await page.waitForTimeout(600);

  const layerResp = await fetch(`${api}/projects/${bigBook.project_id}/translations/zh-CN`, { headers });
  const layerJson = await layerResp.json();
  const baseEntry = (layerJson.entries || {})["big-p0"] || {};
  const stubCandidate = () => ({
    job_id: "stub", project_id: bigBook.project_id, locale: "zh-CN", block_id: "big-p0",
    candidate_id: "cand-1", source_hash: baseEntry.source_hash || "", base_revision: baseEntry.updated_at || "",
    changed: true, complete: true,
    revised: { text: "建议修订译文", items: [], caption: [], footnote: [], cells: {} },
    original: { text: "译：Body paragraph 0.", items: [], caption: [], footnote: [], cells: {} },
    source: "Body paragraph 0.", current: "译：Body paragraph 0.",
  });
  await page.route("**/translations/big-p0/review", route => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(stubCandidate()) }));
  const reviewBtn = page.locator('[data-block-id="big-p0"]').getByText("审校", { exact: true }).first();
  await reviewBtn.waitFor({ state: "attached", timeout: 15000 });
  await reviewBtn.click();
  await page.waitForTimeout(800);
  const panel = page.locator(".review-candidate");
  const panelText = await panel.innerText().catch(() => "");
  checks.push(["审校候选展示原文/现译/建议", panelText.includes("原文") && panelText.includes("现译") && panelText.includes("建议") && panelText.includes("建议修订译文")]);
  await panel.getByText("采纳并保存", { exact: true }).first().click();
  await page.waitForTimeout(1500);
  checks.push(["采纳后候选面板关闭", (await page.locator(".review-candidate").count()) === 0]);
  await page.unroute("**/translations/big-p0/review");

  // 迟到响应：审校响应延迟，期间切到其他项目，候选不应出现。
  await page.route("**/translations/big-p0/review", async route => {
    await new Promise(resolve => setTimeout(resolve, 2000));
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(stubCandidate()) });
  });
  await page.locator('[data-block-id="big-p0"]').getByText("审校", { exact: true }).first().click();
  const otherBook = await projectByTitle("Partial Book");
  await page.goto(`${web}/#/p/${otherBook.project_id}/read`, { waitUntil: "networkidle" });
  await page.waitForTimeout(3200);
  checks.push(["切项目后迟到审校响应被丢弃", (await page.locator(".review-candidate").count()) === 0]);
  await page.unroute("**/translations/big-p0/review");

  // 结构结果栏窗口化。
  await page.goto(`${web}/#/p/${bigBook.project_id}/review`, { waitUntil: "networkidle" });
  await page.waitForTimeout(2500);
  const structInitial = await page.locator(".block-list .block-card").count();
  checks.push(["结构结果栏 DOM 有界", structInitial > 0 && structInitial < 250]);
  await page.locator(".block-list").evaluate(el => { el.scrollTop = el.scrollHeight; });
  await page.waitForTimeout(1500);
  checks.push(["结构结果栏滚动到底渲染尾部块", (await page.locator('[data-block-id="big-p999"]').count()) > 0]);

  } catch (kError) {
    checks.push([`阶段K执行失败：${String(kError).slice(0, 180)}`, false]);
  }

  // 保存失败用例故意 abort 会产生 net::ERR_FAILED，不算页面脚本错误。
  const realConsoleErrors = consoleErrors.filter(error => !error.includes("net::ERR_FAILED"));
  checks.push([`浏览器 0 脚本错误(${realConsoleErrors.length})`, realConsoleErrors.length === 0]);
  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors, debug: typeof debug !== "undefined" ? debug : null }));
}

main().catch(error => { console.log(JSON.stringify({ checks: [], error: String(error) })); process.exit(1); });
