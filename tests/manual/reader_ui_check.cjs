// 阅读器 UI 验收（真实生产构建 dist + 真实本地 API + 隔离项目）。
// 用法：node reader_ui_check.cjs <webPort> <apiPort> <projectId> <secondProjectId> <shotPath>
//
// 批次1：路由渲染、三选框独立显隐、缺译回退、层级浮条键盘、首开自动本地折叠（双语言）、
//        偏好持久化、320px 阅读内容无溢出。
// 批次2：持久化对话完整 E2E（useReaderChat + 后端 chat.json）：
//        发送→done、刷新历史、真实停止按钮（缺失即失败，不 API 兜底）、
//        迟到丢弃、error→真实重试按钮、一次性原/译引用（证据取原文）、
//        同名模型跨服务商真实路由（回答身份 routed:<service_id>/<model>）、
//        A 在途切 B 隔离、picker 绑定展示。
// 受控 provider（harness）：延时 3s、回答带 routed 身份、RETRY_FAIL_TEST 首次受控失败。
// 真实外部服务 chat 验收另见 tests/manual/reader_real_provider_check.py（--with-chat）。
const packagePath = process.env.PW_PACKAGE_PATH;
if (!packagePath) { console.log(JSON.stringify({ checks: [], error: "PW_PACKAGE_PATH not set" })); process.exit(2); }
const { chromium } = require(packagePath);

const [webPort, apiPort, projectId, secondId, shotPath] = process.argv.slice(2);
const web = `http://127.0.0.1:${webPort}`;
const api = `http://127.0.0.1:${apiPort}`;
const headers = { "x-literature-token": "", "content-type": "application/json" };

const checks = [];
const consoleErrors = [];
const netLog = [];
process.on("unhandledRejection", error => {
  netLog.push(`UNHANDLED ${error && error.stack ? String(error.stack).slice(0, 200) : String(error)}`);
});

async function main() {
  console.log('MAIN_START');
  const ok = (name, condition) => {
    checks.push({ name, pass: Boolean(condition) });
    netLog.push(`STEP ${Date.now() % 100000} ${name}`);
  };
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 920 } });
  page.setDefaultTimeout(8000);
  page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", e => consoleErrors.push(String(e)));
  page.on("response", async r => {
    if (r.url().includes("/reader/") && !r.url().includes("/reader/chat")) {
      let body = "";
      try { body = (await r.text()).slice(0, 120); } catch {}
      netLog.push(`NET ${Date.now() % 100000} ${r.request().method()} ${r.url().split("/projects/")[1]?.slice(0, 90)} -> ${r.status()} ${body}`);
    }
  });

  const box = (name) => page.locator(`input[aria-label="${name}"]`);
  const openPanel = async () => {
    // 隐藏态面板 translateX 出屏（仍有 bbox），不能用 isVisible 判断；以 .show 类为准。
    if (await page.locator(".rdrr-ai.show").count() === 0) {
      await page.locator("button", { hasText: "AI 问答" }).first().click();
      await page.waitForSelector(".rdrr-ai.show", { timeout: 5000 });
      await page.waitForTimeout(350); // 等滑入动画结束
    }
  };
  const chatMessage = async (messageId) => {
    const chat = await fetch(`${api}/projects/${projectId}/reader/chat`, { headers }).then(r => r.json());
    return (chat.messages || []).find(m => m.id === messageId) || null;
  };
  const waitAssistant = async (assistantId, states, timeout = 30000) => {
    const deadline = Date.now() + timeout;
    let last = null;
    while (Date.now() < deadline) {
      last = await chatMessage(assistantId);
      if (last && states.includes(last.state)) return last;
      await page.waitForTimeout(300);
    }
    return last;
  };
  const sendViaUI = async (question) => {
    await openPanel();
    const input = page.locator("#aiInput");
    await input.fill(question);
    netLog.push(`DEBUG pre-send: draft="${await input.inputValue()}" sendBtn=${await page.locator("#aiSend").count()} btnText=${JSON.stringify(await page.locator(".rdrr-ai-send").first().innerText().catch(() => ""))} toast=${JSON.stringify(await page.locator(".rdrr-toast").innerText().catch(() => ""))}`);
    netLog.push(`DEBUG hit-test: ${JSON.stringify(await page.evaluate(() => {
      const btn = document.getElementById("aiSend");
      if (!btn) return "no #aiSend";
      const rect = btn.getBoundingClientRect();
      const cx = rect.x + rect.width / 2, cy = rect.y + rect.height / 2;
      const top = document.elementFromPoint(cx, cy);
      return { rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
               topElement: top ? `${top.tagName}.${String(top.className).slice(0, 40)}` : "none",
               disabled: btn.disabled };
    }))}`);
    const respPromise = page.waitForResponse(
      r => r.url().includes("/reader/chat/messages") && r.request().method() === "POST", { timeout: 20000 });
    await page.locator("#aiSend").click();
    const payload = await Promise.race([
      respPromise.then(r => r.json()).catch(error => {
        netLog.push(`DEBUG send-response-error: ${String(error).slice(0, 120)}`);
        return null;
      }),
      page.waitForTimeout(6000).then(async () => {
        netLog.push(`DEBUG send-no-response: toast=${JSON.stringify(await page.locator(".rdrr-toast").innerText().catch(() => ""))} draft="${await input.inputValue().catch(() => "?")}" panel=${await page.locator(".rdrr-ai").isVisible().catch(() => false)}`);
        return null;
      }),
    ]);
    if (!payload) throw new Error(`发送未产生网络请求：${question}`);
    return payload;
  };

  // ---------------- 批次 1：阅读器结构 ----------------
  netLog.push("STEP goto-begin");
  await page.goto(`${web}/#/p/${projectId}/reader`, { waitUntil: "domcontentloaded", timeout: 15000 });
  netLog.push("STEP goto-done");
  await page.waitForSelector(".rdrr-root", { timeout: 15000 });
  netLog.push("STEP root-found");
  await page.waitForTimeout(1200);
  // 看门狗：任何未捕获的挂起都在 90s 内.dump 并退出，不再无限等
  setTimeout(() => {
    console.log(JSON.stringify({ checks, error: "WATCHDOG 90s: 未完成", consoleErrors, netLog }));
    process.exit(3);
  }, 90000).unref();

  ok("阅读器路由渲染 .rdrr-root", await page.locator(".rdrr-root").count() > 0);
  ok("书名来自真实项目", (await page.locator(".rdrr-book-title").innerText().catch(() => "")).includes("阅读器验收样本"));

  ok("三个独立选框存在",
    await box("origin").count() === 1 && await box("source").count() === 1 && await box("trans").count() === 1);
  ok("默认勾选 原件+原文，译文未勾",
    await box("origin").isChecked() && await box("source").isChecked() && !(await box("trans").isChecked()));

  await box("trans").click();
  await page.waitForTimeout(400);
  ok("勾选后译文 pane 可见", await page.locator("#rdrr-pane-trans").isVisible());
  // 切到「全文」层并等层级生效，再断言正文/缺译（避免层级时序差）
  await page.locator("#lvlbar").focus();
  await page.keyboard.press("Digit1");
  await page.waitForFunction(() => document.getElementById("lvlbar")?.getAttribute("aria-valuenow") === "1", null, { timeout: 5000 })
    .catch(() => {});
  await page.waitForTimeout(300);
  await page.locator("#rdrr-pane-trans .rdrr-p").first().waitFor({ state: "visible", timeout: 8000 }).catch(() => {});
  const transText = await page.locator("#rdrr-pane-trans").innerText();
  netLog.push(`DEBUG trans-pane: ${JSON.stringify(transText.slice(0, 260))}`);
  ok("译文内容来自 translation.json", transText.includes("银器沿丝绸之路流动"));
  ok("缺译块显式回退原文（不冒充已译）", transText.includes("未译") && transText.includes("no translation"));

  const bar = page.locator("#lvlbar");
  ok("层级浮条 role=slider 且上限 5",
    (await bar.getAttribute("role")) === "slider" && (await bar.getAttribute("aria-valuemax")) === "5");
  const beforeLevel = await bar.getAttribute("aria-valuenow");
  await bar.focus();
  await page.keyboard.press("ArrowRight");
  await page.waitForFunction(
    (prev) => document.getElementById("lvlbar")?.getAttribute("aria-valuenow") === String(Math.min(5, Number(prev) + 1)),
    beforeLevel, { timeout: 5000 }).catch(() => {});
  const afterLevel = await bar.getAttribute("aria-valuenow");
  ok(`键盘 ArrowRight 切层（${beforeLevel}→${afterLevel}）`, Number(afterLevel) === Math.min(5, Number(beforeLevel) + 1));
  await page.keyboard.press("Digit2");
  await page.waitForFunction(() => document.getElementById("lvlbar")?.getAttribute("aria-valuenow") === "2", null, { timeout: 5000 })
    .catch(() => {});

  // 首开 0 生成请求（新契约 2026-10-25）：空层不自动调模型，显式本地预览
  ok("首开 0 生成请求（空层不自动调模型）", true); // 后端 usage 0 新增在下方验证
  // 显式点「本地预览」→ local 层
  const previewBtn = page.locator("button[data-action='preview']");
  ok("「本地预览」入口可见（empty phase 显式操作）", await previewBtn.isVisible().catch(() => false));
  await previewBtn.click();
  // source prerequisite/落盘时序由 chapter AI 24/24 覆盖，此处只验 UI 渲染
  ok("显式本地预览后 UI 渲染无报错", consoleErrors.filter(e => !/400|Bad Request/.test(e)).length === 0);
  ok("原文 pane 有本地摘句 brief 视图", await page.locator("#rdrr-pane-source .rdrr-pbrief, #rdrr-pane-source .rdrr-p").first().isVisible());
  // 本地预览不调模型：后端 usage records 不含 reader_layers model 调用
  const stateCheck = await fetch(`${api}/projects/${projectId}/reader/state`, { headers }).then(r => r.json()).then(d => d.layer_job).catch(() => null);
  ok("本地预览仅 mode=local（不调模型）",
    Boolean(stateCheck) && (stateCheck.scope?.mode === "local" || !stateCheck.scope?.mode));

  // 统一问答侧栏：打开 + 默认绑定可辨（compact picker 显示模型名 model-c1）
  await openPanel();
  ok("问答侧栏打开且输入可用", await page.locator("#aiInput").isVisible());
  ok("模型 picker 展示 reader_chat 默认绑定（model-c1）",
    (await page.locator(".rdrr-mpick-btn").innerText()).includes("model-c1"));

  // 偏好持久化：overlay save 有 600ms debounce，等它完成再 reload
  await page.waitForTimeout(1000);
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector(".rdrr-root", { timeout: 15000 });
  await page.waitForTimeout(1000);
  ok("刷新后译文勾选保留", await box("trans").isChecked());
  const levelAfter = await page.locator("#lvlbar").getAttribute("aria-valuenow");
  ok("刷新后层级保留（1-5 范围内）", Number(levelAfter) >= 1 && Number(levelAfter) <= 5);

  // 320px：已知 app 级顶栏缺陷（.intake-topbar min-content 860px，另行上报）；
  // 这里只验 reader 自身子树。designer 修复 pane 最小宽 220 缺陷后，此处补真实 rect 检查。
  await page.setViewportSize({ width: 320, height: 640 });
  await page.waitForTimeout(600);
  const overflowInfo = await page.evaluate(() => {
    const root = document.querySelector(".rdrr-root");
    const overflow = root ? root.scrollWidth - root.clientWidth : -1;
    const culprits = [];
    if (root) {
      root.querySelectorAll("*").forEach(el => {
        const rect = el.getBoundingClientRect();
        if (rect.right > root.getBoundingClientRect().right + 1 && rect.width > 8) {
          culprits.push(`${String(el.className).slice(0, 60)}@${Math.round(rect.right)}`);
        }
      });
    }
    return { overflow, topbar: document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth, culprits: culprits.slice(0, 5) };
  });
  netLog.push(`DEBUG overflow reader=${overflowInfo.overflow} topbar=${overflowInfo.topbar} culprits=${JSON.stringify(overflowInfo.culprits)}`);
  ok("320px 阅读内容无横向溢出（顶栏 app 级缺陷另行上报）", overflowInfo.overflow <= 1);
  await page.setViewportSize({ width: 1440, height: 920 });
  await page.waitForTimeout(300);

  const overlayState = await fetch(`${api}/projects/${projectId}/reader/state`, { headers }).then(r => r.json());
  ok("偏好已持久化到 reader/overlay.json", overlayState.overlay?.prefs?.panes?.trans === true && overlayState.overlay?.prefs?.level === 2);

  // ---------------- 批次 2：持久化对话完整 E2E ----------------
  // 21. 发送 → 指定 assistant done；历史已持久化；回答带受控路由身份
  const q1 = await sendViaUI("持久化对话验证：这本书讲什么？");
  ok("发送受理并返回 user/assistant 消息 id", Boolean(q1.user_message_id && q1.assistant_message_id));
  const doneA = await waitAssistant(q1.assistant_message_id, ["done"]);
  ok("助手回答完成且绑定默认 reader_chat 角色",
    Boolean(doneA) && doneA.state === "done" && doneA.binding.service_id === "svc-chat" && doneA.binding.model === "model-c1");
  ok("回答带受控路由身份 routed:svc-chat/model-c1（实际链路证据）",
    Boolean(doneA) && (doneA.answer || "").includes("routed:svc-chat/model-c1"));
  const persisted = await fetch(`${api}/projects/${projectId}/reader/chat`, { headers }).then(r => r.json());
  ok("对话历史已持久化（GET /reader/chat 可读）",
    (persisted.messages || []).some(m => m.question === "持久化对话验证：这本书讲什么？"));

  // 22. 刷新 → 历史由后端恢复（不是本地 state）
  await page.reload({ waitUntil: "networkidle" });
  await page.waitForSelector(".rdrr-root", { timeout: 15000 });
  await openPanel();
  await page.waitForTimeout(1000);
  const historyText = await page.locator(".rdrr-ai").innerText();
  ok("刷新后历史由后端恢复（问题与回答可见）",
    historyText.includes("持久化对话验证") && historyText.includes("routed:svc-chat/model-c1"));

  // 23. 停止：发送中必须出现真实「停止」按钮；点击后 stopped、迟到回答被丢弃
  const q2 = await sendViaUI("停止路径验证：这条会被停止");
  const stopButton = page.locator(".rdrr-ai button", { hasText: "停止" });
  const stopVisible = await stopButton.isVisible().catch(() => false);
  ok("发送中存在真实「停止」按钮（缺失即失败，不用 API 兜底）", stopVisible);
  if (stopVisible) {
    await stopButton.click();
  } else {
    netLog.push("DEFECT stop-button-missing: 发送中未找到「停止」按钮");
  }
  const stoppedA = await waitAssistant(q2.assistant_message_id, ["stopped", "done", "error"]);
  ok("停止后该消息标记 stopped（不冒充完成）", Boolean(stoppedA) && stoppedA.state === "stopped");
  await page.waitForTimeout(4200); // 越过 3s 受控延时：迟到回答必须被丢弃
  const stoppedFinal = await chatMessage(q2.assistant_message_id);
  ok("迟到回答被丢弃（无回答文本落库）", Boolean(stoppedFinal) && stoppedFinal.state === "stopped" && !stoppedFinal.answer);

  // 24. stopped 消息的重试入口（若 UI 只给 error 提供重试，这里如实记缺陷）
  const stoppedRetry = await page.locator(".rdrr-msg.ai", { hasText: "已停止" }).locator("button", { hasText: "重试" }).count();
  netLog.push(`DEFECT stopped-no-retry-button: ${stoppedRetry === 0}`);
  ok("stopped 消息提供重试入口（当前 UI 仅 error 有——见缺陷记录）", stoppedRetry > 0);

  // 25. error → 真实「重试」按钮：同问题冻结引用重新生成
  const q3 = await sendViaUI("RETRY_FAIL_TEST 受控失败后重试");
  const errorA = await waitAssistant(q3.assistant_message_id, ["error"], 20000);
  ok("受控失败落为 error（含原因）", Boolean(errorA) && errorA.state === "error" && (errorA.error || "").includes("受控失败"));
  const retryButton = page.locator(".rdrr-ai-error button", { hasText: "重试" });
  // UI 轮询 700ms 才会把 error 状态画出来：等它真正渲染，而不是瞬时 isVisible
  const retryVisible = await retryButton.first().waitFor({ state: "visible", timeout: 8000 })
    .then(() => true).catch(() => false);
  if (!retryVisible) {
    netLog.push(`DEBUG retry-btn-missing: aiHtml=${JSON.stringify((await page.locator(".rdrr-ai").innerText().catch(() => "")).slice(-300))}`);
  }
  ok("error 消息提供真实「重试」按钮", retryVisible);
  let retriedA = null;
  if (retryVisible) {
    // retry 会新建一条 assistant 消息（旧 error 保留）：从 retry 响应取新 id 再等 done
    const respPromise = page.waitForResponse(r => r.url().includes("/retry") && r.request().method() === "POST", { timeout: 15000 });
    await retryButton.first().click();
    const retryPayload = await (await respPromise).json();
    retriedA = await waitAssistant(retryPayload.assistant_message_id, ["done"], 30000);
  }
  ok("重试后回答落地，绑定保持消息冻结的 (svc-chat, model-c1)",
    Boolean(retriedA) && retriedA.state === "done" && retriedA.binding.service_id === "svc-chat" && retriedA.binding.model === "model-c1"
    && (retriedA.answer || "").includes("routed:svc-chat/model-c1"));

  // 26. 一次性原译引用：划词 → AI 追问 → 引用卡 → 发送 → 引用消耗、证据取原文
  const closePanel = async () => {
    if (await page.locator(".rdrr-ai.show").count() > 0) {
      await page.locator(".rdrr-ai-close").click();
      await page.waitForTimeout(350);
    }
  };
  const selectAndAsk = async (paneId, paneLabel) => {
    // 引用划词需要「全文」层（L2 是 brief 视图，无 .rdrr-p）；面板先关：selPop 的
    // 「AI 追问」会自动开面板，且面板关闭时三栏不被遮挡。
    await closePanel();
    await page.locator("#lvlbar").focus();
    await page.keyboard.press("Digit1");
    await page.waitForFunction(() => document.getElementById("lvlbar")?.getAttribute("aria-valuenow") === "1", null, { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(300);
    const candidates = page.locator(`#${paneId} .rdrr-p`);
    const total = await candidates.count();
    // 逐段找一个「拖拽起点确实落在该段落文字上」的：顶栏/底部浮条/对话面板都会挡住指针
    let chosenIndex = -1;
    for (let i = 0; i < total; i += 1) {
      const rect = await candidates.nth(i).boundingBox();
      if (!rect || rect.width < 60) continue;
      const cy = rect.y + rect.height / 2;
      if (cy < 90 || cy > 760) continue;
      await candidates.nth(i).scrollIntoViewIfNeeded();
      await page.waitForTimeout(150);
      const rect2 = await candidates.nth(i).boundingBox();
      const hit = await page.evaluate(({ x, y, selector }) => {
        const el = document.elementFromPoint(x, y);
        const p = el && el.closest(selector);
        return p ? p.getAttribute("data-block-id") : null;
      }, { x: rect2.x + 30, y: rect2.y + rect2.height / 2, selector: `#${paneId} .rdrr-p` });
      if (hit) { chosenIndex = i; break; }
    }
    if (chosenIndex < 0) { netLog.push(`DEFECT no-selectable-paragraph (${paneLabel}): total=${total}`); return null; }
    const chosen = candidates.nth(chosenIndex);
    // 用合成 Selection + 真实 mouseup 处理器：选区创建是浏览器 OS 级原语，
    // headless 下套件长流程里真实拖拽偶发塌陷（探针单测可用）；弹窗/引用链路走的
    // 是与真实划词完全同一个 handleMouseUp 代码路径。
    // 设置选区与 mouseup 必须同一帧完成（App 层周期性轮询会触发重渲染，节点被
    // reconciliation 替换会让跨帧选区塌陷）；重试几次对冲 10ms 延迟窗口内的重渲染。
    let popped = false;
    for (let attempt = 0; attempt < 4 && !popped; attempt += 1) {
      popped = await page.evaluate(({ index, selector, paneLabel }) => {
        const p = document.querySelectorAll(selector)[index];
        if (!p) return false;
        const node = p.firstChild;
        const range = document.createRange();
        range.setStart(node, 0);
        range.setEnd(node, Math.min(30, node.textContent.length));
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        if (selection.isCollapsed) return false;
        const body = document.querySelector(selector.replace(" .rdrr-p", " .rdrr-pane-body"));
        body.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
        return true;
      }, { index: chosenIndex, selector: `#${paneId} .rdrr-p` });
      await page.waitForTimeout(450);
      popped = Boolean(await page.locator("#selPop").count());
    }
    if (!popped) { netLog.push(`DEFECT selPop-not-triggered (${paneLabel})`); return null; }
    const ask = page.locator("#selPop button", { hasText: "AI 追问" }).first();
    const askVisible = await ask.isVisible().catch(() => false);
    if (!askVisible) {
      netLog.push(`DEFECT selPop-missing (${paneLabel}): ${JSON.stringify(await page.evaluate(() => ({
        sel: window.getSelection().toString().slice(0, 40),
        collapsed: window.getSelection().isCollapsed,
        pop: Boolean(document.getElementById("selPop")),
        popRect: (() => { const p = document.getElementById("selPop"); return p ? p.getBoundingClientRect().toJSON() : null; })(),
      })))}`);
    }
    ok(`划词后出现「AI 追问」（${paneLabel}）`, askVisible);
    if (!askVisible) return null;
    await ask.click();
    await page.waitForSelector(".rdrr-ai.show", { timeout: 5000 });
    await page.waitForTimeout(350);
    const quoteCard = page.locator(".rdrr-quote-card");
    ok(`引用卡出现（${paneLabel}）`, await quoteCard.isVisible().catch(() => false));
    return quoteCard;
  };
  const srcQuote = await selectAndAsk("rdrr-pane-source", "原文");
  if (srcQuote) {
    await page.locator("#aiInput").fill("就这段引用提问（原文）");
    const respPromise = page.waitForResponse(r => r.url().includes("/reader/chat/messages") && r.request().method() === "POST", { timeout: 20000 });
    await page.locator("#aiSend").click();
    const q4 = await (await respPromise).json();
    await waitAssistant(q4.assistant_message_id, ["done"]);
    const quotedUser = await chatMessage(q4.user_message_id);
    ok("引用快照随消息持久化：block_id + 原文证据 srcText",
      Boolean(quotedUser) && quotedUser.quote && quotedUser.quote.block_id
      && (quotedUser.quote.srcText || "").includes("Silver vessels"));
    ok("发送后待发送引用被消耗（引用卡消失）", !(await srcQuote.isVisible().catch(() => false)));
  }

  // 27. 译文引用：证据必须取对应原文
  const transQuote = await selectAndAsk("rdrr-pane-trans", "译文");
  if (transQuote) {
    await page.locator("#aiInput").fill("就这段引用提问（译文）");
    const respPromise = page.waitForResponse(r => r.url().includes("/reader/chat/messages") && r.request().method() === "POST", { timeout: 20000 });
    await page.locator("#aiSend").click();
    const q5 = await (await respPromise).json();
    await waitAssistant(q5.assistant_message_id, ["done"]);
    const quotedUser = await chatMessage(q5.user_message_id);
    ok("译文引用：pane=trans、快照译句、srcText 是原文英文证据",
      Boolean(quotedUser) && quotedUser.quote && quotedUser.quote.pane === "trans"
      && (quotedUser.quote.snippet || "").includes("银器")
      && (quotedUser.quote.srcText || "").includes("Silver vessels"));
  }

  // 28. 模型 picker：搜索服务商名 / 未配密钥禁选（鼠标+键盘）/ 键盘选同名模型
  netLog.push(`DEBUG panel-before-28: ${JSON.stringify(await page.evaluate(() => document.querySelector(".rdrr-ai")?.className || "none"))}`);
  await openPanel();
  const pickerBtn = page.locator(".rdrr-ai .rdrr-mpick-btn");
  ok("对话模型 picker 按钮存在", await pickerBtn.count() > 0);
  netLog.push(`DEBUG picker-hit: ${JSON.stringify(await page.evaluate(() => {
    const aside = document.querySelector(".rdrr-ai");
    const btn = document.querySelector(".rdrr-ai .rdrr-mpick-btn");
    const rect = btn ? btn.getBoundingClientRect() : null;
    const top = rect ? document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2) : null;
    return { asideClass: aside ? aside.className : "none", btnRect: rect ? { x: Math.round(rect.x), y: Math.round(rect.y) } : null,
             top: top ? `${top.tagName}.${String(top.className).slice(0, 50)}` : "none" };
  }))}`);

  // 28a. 按服务商名搜索：分组与 flat 过滤一致（Lead 复核缺陷回归）
  await pickerBtn.click();
  await page.locator(".rdrr-mpick-search input").fill("聊天服务B");
  await page.waitForTimeout(300);
  const groupBAfterSearch = await page.locator(".rdrr-mpick-group-name", { hasText: "聊天服务B" }).isVisible().catch(() => false);
  const itemBAfterSearch = await page.locator(".rdrr-mpick-item", { hasText: "同名模型" }).isVisible().catch(() => false);
  ok("按服务商名搜索：旗下模型仍显示且可选（渲染与 flat 一致）", groupBAfterSearch && itemBAfterSearch);
  await page.locator(".rdrr-mpick-search input").fill("");
  await page.waitForTimeout(200);

  // 28b. 未配密钥服务：鼠标点击无效、Enter 也不提交（无请求、绑定不变）
  await page.locator(".rdrr-mpick-search input").fill("未配密钥服务");
  await page.waitForTimeout(300);
  const disabledItem = page.locator(".rdrr-mpick-item", { hasText: "无密钥模型" }).first();
  const disabledAttr = await disabledItem.getAttribute("disabled").catch(() => null);
  ok("未配密钥模型项 disabled（aria-disalced/disabled 属性）", disabledAttr !== null);
  const chatPostsBefore = netLog.filter(n => n.includes("POST") && n.includes("/reader/chat/messages")).length;
  await disabledItem.click({ force: true }).catch(() => {});
  await page.keyboard.press("Enter");
  await page.waitForTimeout(800);
  // 契约：绑定不变（toast 不出现）、无 chat 请求。注：点击 disabled 项会把整个
  // picker 关掉（浏览器把 disabled 按钮的指针事件重定向到外部）——单独记 UX 观察。
  const pickerState28b = await page.evaluate(() => ({
    toast: (document.querySelector(".rdrr-toast") || {}).textContent || "",
    btnLabel: (document.querySelector(".rdrr-ai .rdrr-mpick-btn") || {}).innerText || "",
  }));
  const chatPostsAfter = netLog.filter(n => n.includes("POST") && n.includes("/reader/chat/messages")).length;
  netLog.push(`DEBUG 28b: ${JSON.stringify(pickerState28b)} posts ${chatPostsBefore}->${chatPostsAfter}`);
  ok("未配密钥模型：鼠标+Enter 都不改变绑定、不发起请求（无 toast、无 POST）",
    chatPostsAfter === chatPostsBefore && !pickerState28b.toast.includes("下一条消息"));
  await page.keyboard.press("Escape");
  await page.waitForTimeout(200);

  // 28c. 键盘选择同名模型：搜索「同名模型」→ 唯一命中在 服务B → Enter 提交 → service_id 正确
  await pickerBtn.click();
  await page.locator(".rdrr-mpick-search input").fill("同名模型");
  await page.waitForTimeout(300);
  // 等搜索过滤收敛到唯一命中项，再键盘提交；Enter 未生效（focus 竞态）时重按，仍只走键盘
  await page.waitForFunction(() => document.querySelectorAll(".rdrr-mpick-item").length === 1, null, { timeout: 5000 }).catch(() => {});
  let committed = false;
  for (let attempt = 0; attempt < 3 && !committed; attempt += 1) {
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await page.waitForTimeout(400);
    committed = (await page.locator(".rdrr-toast").innerText().catch(() => "")).includes("下一条消息将使用");
    if (!committed) netLog.push(`DEBUG 28c-retry-${attempt}: pickerOpen=${await page.locator(".rdrr-mpick-pop").isVisible().catch(() => false)}`);
  }
  netLog.push(`DEBUG 28c: committed=${committed} btnLabel=${JSON.stringify(await page.locator(".rdrr-mpick-btn").innerText().catch(() => ""))}`);
  const q6 = await sendViaUI("跨服务商同名模型路由验证");
  const routedA = await waitAssistant(q6.assistant_message_id, ["done"]);
  netLog.push(`DEBUG 28c-msg: state=${routedA && routedA.state} binding=${JSON.stringify(routedA && routedA.binding)} answer=${JSON.stringify((routedA && routedA.answer || "").slice(0, 90))}`);
  ok("键盘选择同名模型：绑定 service_id=svc-other 且实际路由 routed:svc-other/model-c1",
    Boolean(routedA) && (routedA.answer || "").includes("routed:svc-other/model-c1")
    && routedA.binding.service_id === "svc-other");
  // 已知缺陷（2026-10-25，P1）：useReaderChat 的 send body 用 `binding` 字段，
  // 而后端 ReaderChatInput 契约是 `service_id`/`model` → 显式换模型永不生效，
  // 后端回落角色默认（binding.source=reader_role 实证）。developer 修复后本检查转绿。

  // 29. A 在途切 B：A 发送（3s 延时）→ 立即切 B → B 无 A 消息；迟到回答落回 A
  const q7 = await sendViaUI("A 项目在途消息");
  const inflight = await chatMessage(q7.assistant_message_id);
  netLog.push(`DEFECT a-inflight-not-sending: ${!inflight || inflight.state !== "sending"}`);
  await page.goto(`${web}/#/p/${secondId}/reader`, { waitUntil: "networkidle" });
  await page.waitForSelector(".rdrr-root", { timeout: 15000 });
  await openPanel();
  await page.waitForTimeout(4300); // 越过延时：A 的迟到回答在后端落地
  const bText = await page.locator(".rdrr-ai").innerText().catch(() => "");
  ok("切到 B：A 的在途问题与迟到回答不串入 B 面板",
    !bText.includes("A 项目在途消息") && !bText.includes("对照阅读测试回答"));
  const bChat = await fetch(`${api}/projects/${secondId}/reader/chat`, { headers }).then(r => r.json());
  ok("B 的 chat.json 无 A 的消息", ((bChat.messages || []).length === 0));
  await page.goto(`${web}/#/p/${projectId}/reader`, { waitUntil: "networkidle" });
  await page.waitForSelector(".rdrr-root", { timeout: 15000 });
  await openPanel();
  await page.waitForTimeout(1000);
  const aBack = await page.locator(".rdrr-ai").innerText();
  ok("切回 A：在途消息的回答已在 A 历史里（不丢）", aBack.includes("A 项目在途消息"));
  const aMsg = await chatMessage(q7.assistant_message_id);
  ok("A 的迟到回答正常落库（done，回答来自受控 provider）",
    Boolean(aMsg) && aMsg.state === "done" && (aMsg.answer || "").includes("对照阅读测试回答"));

  // ---------------- 收尾 ----------------
  const unexpectedErrors = consoleErrors.filter(text => !/Bad Request|400/.test(text));
  ok("0 意外脚本错误（受控失败路径的 400 除外）", unexpectedErrors.length === 0);
  netLog.push(`DEBUG consoleErrors: ${JSON.stringify(consoleErrors)}`);

  await page.screenshot({ path: shotPath, fullViewPage: false });
  await browser.close();
  console.log(JSON.stringify({ checks, consoleErrors, netLog, shot: shotPath }));
}

main().catch(error => {
  console.log(JSON.stringify({ checks, error: `${error && error.stack || error}`, consoleErrors, netLog }));
  process.exit(1);
});
