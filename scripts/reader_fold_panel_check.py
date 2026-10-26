"""折叠状态面板与 useReaderFold 接线验收（真实 API + 生产构建前端，不调模型）。

冻结语义（Lead 2026-10-25）：
- **首开不自动折叠**：打开阅读器只读，不自动跑本地摘句、更不自动调模型；
- **本地预览是显式按钮**；本地结果**不隐藏**升级入口（local 不是成品）；
- 折叠模型绑定只读 `state.roles.fold`，并显示来源（自有/继承翻译默认）与「未指定模型 → 首模型」；
- 绑定不可用（未设置/已清空/服务失效）时按钮禁用并给出原因，**不回落聊天模型**。

覆盖：首开无自动作业 → 本地预览 → 升级入口常驻 → 绑定来源可见 → 清空/失效禁用。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web  # noqa: E402
from tests.manual.reader_ui_browser_check import seed_project  # noqa: E402

NODE = Path.home() / "Library/Caches/ms-playwright-go/1.57.0/node"
PW = Path.home() / "Library/Caches/ms-playwright-go/1.57.0/package"

CHECK_JS = r"""
const { chromium } = require(process.env.PW_PACKAGE_PATH);
(async () => {
  const base = process.argv[2];
  const url = base + "/index.html#/p/" + process.env.PROJECT_ID + "/reader";
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const errors = []; page.on("pageerror", e => errors.push(String(e)));
  const results = [];
  const check = (name, pass, detail) => results.push({ name, pass: Boolean(pass), detail: detail || "" });
  const fs = require("fs");
  const writeRoles = (roles) => {
    const path = process.env.CONFIG_PATH;
    const cfg = JSON.parse(fs.readFileSync(path, "utf8"));
    cfg.roles = roles;
    fs.writeFileSync(path, JSON.stringify(cfg, null, 2));
  };
  const open = async (first) => {
    if (first) await page.goto(url, { waitUntil: "networkidle" });
    else await page.reload({ waitUntil: "networkidle" });
    await page.waitForTimeout(2000);
  };
  const panel = async () => await page.evaluate(() => {
    const el = document.querySelector(".rdrr-layerstatus");
    if (!el) return null;
    const buttons = [...el.querySelectorAll("button")].map(b => ({ text: b.textContent.trim(), disabled: b.disabled, title: b.title }));
    return { phase: el.getAttribute("data-phase"), text: el.innerText, buttons,
             select: el.querySelector("select") ? [...el.querySelectorAll("select option")].map(o => o.textContent) : [] };
  });
  const layerOf = async (locale) => await page.evaluate(async ({ api, pid, locale }) => {
    const response = await fetch(api + "/projects/" + pid + "/reader/layers?locale=" + encodeURIComponent(locale),
      { headers: { "x-literature-token": "" } });
    const value = await response.json();
    return { generated: value.generated, content: value.provenance?.content ?? "",
             sections: (value.sections || []).length, targetDerived: value.provenance?.target_derived === true };
  }, { api: process.env.API, pid: process.env.PROJECT_ID, locale });

  const base_roles = JSON.parse(process.env.ROLES_BASE);

  // 「AI 成功态」用构造响应验证 UI（不烧模型）：把真实 layers 的 provenance 改成 model，
  // 由 page.route 返回；只验界面渲染与请求参数，不代表模型真实生成质量。
  const aiPayloads = async () => {
    const raw = await page.evaluate(async ({ api, pid }) => {
      const h = { "x-literature-token": "" };
      const state = await fetch(api + "/projects/" + pid + "/reader/state", { headers: h }).then(r => r.json());
      const layers = await fetch(api + "/projects/" + pid + "/reader/layers?locale=zh-CN", { headers: h }).then(r => r.json());
      return { state, layers };
    }, { api: process.env.API, pid: process.env.PROJECT_ID });
    const layers = { ...raw.layers, generated: true, stale: false, ai_generated: true, partial: false, failed: [], content: "model",
      provenance: { ...(raw.layers.provenance || {}), content: "model", ai_generated: true, model: "model-c1",
                    ai_units: 2, local_units: 0, partial: false } };
    // 构造的 state 来自早期快照，可能还没开译文栏：显式打开，避免读到空数组
    if (raw.state?.overlay?.prefs?.panes) raw.state.overlay.prefs.panes.trans = true;
    // 逐段混合来源：第 0 段是模型派生译句，第 1 段仍是本地译文摘句（顶层 strategy 同为 derive）
    const sections = (layers.sections || []).map(section => ({
      ...section,
      units: (section.units || []).map((unit, index) => (index === 0
        ? { ...unit, brief_source: "derived", source: "model" }
        : { ...unit, brief_source: "translation-local", source: "local" })),
    }));
    return { state: raw.state, layers: { ...layers, sections } };
  };

  // 1) 首开：不自动折叠
  writeRoles(base_roles);
  await open(true);
  const before = await layerOf("source");
  const beforeTarget = await layerOf("zh-CN");
  const p1 = await panel();
  check("首开不自动生成分层（未调模型、未跑本地摘句）",
    before.generated === false && beforeTarget.generated === false, JSON.stringify({ before, beforeTarget }));
  check("首开面板给出「用模型折叠」与「本地预览」两个显式入口",
    Boolean(p1) && p1.buttons.some(b => b.text.includes("用模型折叠")) && p1.buttons.some(b => b.text.includes("本地预览")),
    JSON.stringify(p1 && p1.buttons));
  check("面板显示折叠模型来源（自有绑定 + 模型名）",
    Boolean(p1) && /折叠模型：.*model-c1/.test(p1.text) && p1.text.includes("阅读折叠模型"), p1 ? p1.text.slice(0, 160) : "");

  // 2) 本地预览（显式点击）→ local 内容，升级入口常驻
  await page.evaluate(() => {
    const button = [...document.querySelectorAll(".rdrr-layerstatus button")].find(b => b.textContent.includes("本地预览"));
    button?.click();
  });
  // 夹具补原文层：新版后端 local 预览只处理请求的 locale，测试需要两栏都有短层
  await page.evaluate(async ({ api, pid }) => {
    await fetch(api + "/projects/" + pid + "/reader/layers", { method: "POST",
      headers: { "x-literature-token": "", "content-type": "application/json" },
      body: JSON.stringify({ locale: "source", mode: "local" }) });
  }, { api: process.env.API, pid: process.env.PROJECT_ID });
  // 等到「不再有作业在跑」：作业 running 期间生成内容可能已落盘，不能据此提前判定完成
  for (let i = 0; i < 60; i += 1) {
    await page.waitForTimeout(400);
    const running = await page.evaluate(() => Boolean(document.querySelector(".rdrr-layerstatus .rdrr-layerprog")));
    const state = await layerOf("zh-CN");
    if (state.generated && !running) break;
  }
  // 有译文时折叠 controller 跑在译文 locale（后端单作业：先确保原文层再派生短层）
  const afterLocal = await layerOf("zh-CN");
  const afterSource = await layerOf("source");
  const p2 = await panel();
  check("点「本地预览」后译文 locale 生成本地摘句（content=local）", afterLocal.generated === true && afterLocal.content === "local",
    JSON.stringify({ afterLocal, afterSource }));
  check("本地内容仍显示「升级为模型折叠」（local 不隐藏升级入口）",
    Boolean(p2) && p2.buttons.some(b => b.text.includes("升级为模型折叠")), JSON.stringify(p2 && p2.buttons));
  check("本地内容不当成 AI 完成", Boolean(p2) && p2.text.includes("本地摘句"), p2 ? p2.text.slice(0, 120) : "");
  check("译文短层标记为派生（target_derived=true，不冒充已有译文逐字摘录）", afterLocal.targetDerived === true,
    JSON.stringify(afterLocal));

  // 3) 模型折叠按钮：一次点击只发一个作业（有译文时用译文 locale 单 driver），失败要可见
  const posts = [];
  page.on("request", request => {
    const url = request.url();
    if (request.method() === "POST" && url.includes("/reader/layers")) {
      let locale = new URL(url).searchParams.get("locale") || "";
      try { locale = JSON.parse(request.postData() || "{}").locale || locale; } catch (e) {}
      posts.push(locale);
    }
  });
  await page.evaluate(() => {
    const button = [...document.querySelectorAll(".rdrr-layerstatus button")].find(b => b.textContent.includes("升级为模型折叠"));
    button?.click();
  });
  for (let i = 0; i < 90; i += 1) {
    await page.waitForTimeout(1000);
    const running = await page.evaluate(() => Boolean(document.querySelector(".rdrr-layerstatus .rdrr-layerprog")));
    if (!running) break;
  }
  const p4 = await panel();
  check("点一次模型折叠只发一个作业，且用译文 locale（无 source/target 双 POST）",
    posts.length === 1 && posts[0] === "zh-CN", JSON.stringify(posts));
  check("模型折叠失败在面板可见（不静默失败）",
    Boolean(p4) && /失败|错误|error|无法/i.test(p4.text), p4 ? p4.text.replace(/\n/g, " | ").slice(0, 200) : "");

  // 4) AI 成功态：常驻面板不能整条消失；范围/重新生成/强制重折仍可找到
  const ai = await aiPayloads();
  // 逐 unit 来源徽章：本地预览（content=local）不能被标成“分层译句”
  const badges = async (level) => {
    // 译文栏默认可能是关的：先勾上，否则读到空数组会误判
    const trans = page.locator('.rdrr-ptoggle input[aria-label="trans"]').first();
    if (await trans.count() && !(await trans.isChecked())) { await trans.click(); await page.waitForTimeout(500); }
    await page.waitForTimeout(200);
    await page.locator("#lvlbar").focus();
    await page.keyboard.press("Digit" + level);
    // mock 的 state 会把 level 拉回旧值，这里等它真的生效后立刻读，避免读到回退态
    await page.waitForFunction((want) => document.getElementById("lvlbar")?.getAttribute("aria-valuenow") === String(want),
      level, { timeout: 3000 }).catch(() => {});
    await page.waitForTimeout(250);
    return await page.evaluate(() => {
      const pane = document.getElementById("rdrr-pane-trans");
      const pick = cls => (pane ? [...pane.querySelectorAll(cls)].map(el => el.textContent.trim()) : []);
      const paneTrans = document.getElementById("rdrr-pane-trans");
    return { derived: pick(".rdrr-derivedtag"), local: pick(".rdrr-localtag"), model: pick(".rdrr-modeltag"),
      fallback: pick(".rdrr-fbtag"),
      debug: { transVisible: paneTrans ? paneTrans.getBoundingClientRect().width > 0 : false,
               transText: paneTrans ? paneTrans.innerText.slice(0, 160) : null,
               level: document.getElementById("lvlbar")?.getAttribute("aria-valuenow"),
               prefs: paneTrans ? paneTrans.className : null } };
    });
  };
  const localBadges = await badges(2);
  check("本地预览后译文段标「本地译文摘句」，不冒充「分层译句」",
    localBadges.derived.length === 0 && localBadges.local.some(t => t.includes("本地译文摘句")),
    JSON.stringify(localBadges));
  const localL3 = await badges(3);
  check("本地预览 L3 标「本地摘取」（不标模型概括）",
    localL3.local.some(t => t.includes("本地摘取")) && localL3.model.length === 0,
    JSON.stringify(localL3));

  const layersRoute = url => url.pathname.endsWith("/reader/layers");
  const stateRoute = url => url.pathname.endsWith("/reader/state");
  await page.route(layersRoute, route => route.fulfill({ status: 200, contentType: "application/json",
    body: JSON.stringify(ai.layers) }));
  await page.route(stateRoute, route => route.fulfill({ status: 200, contentType: "application/json",
    body: JSON.stringify(ai.state) }));
  await open(false);
  const pAi = await panel();
  if (!pAi) {
    results.push({ name: "DEBUG ai-missing", pass: true, detail: JSON.stringify({
      errors: errors.slice(0, 3),
      body: (await page.locator("body").innerText()).slice(0, 400),
    }) });
  }
  check("AI 成功态（content=model）仍显示常驻折叠面板",
    Boolean(pAi) && pAi.phase === "ai", JSON.stringify(pAi && { phase: pAi.phase, text: pAi.text.slice(0, 80) }));
  check("AI 成功态仍可找到「重新生成」「强制重折」与范围选择",
    Boolean(pAi) && pAi.buttons.some(b => b.text.includes("重新生成")) && pAi.buttons.some(b => b.text.includes("强制重折"))
      && pAi.select.length > 0, JSON.stringify(pAi && { buttons: pAi.buttons, select: pAi.select }));
  check("AI 成功态标明模型来源（不是只说“已就绪”）",
    Boolean(pAi) && /折叠模型：.*model-c1/.test(pAi.text), pAi ? pAi.text.replace(/\n/g, " | ").slice(0, 140) : "");
  const aiBadges = await badges(2);
  check("同一份 derive 层里逐段判定：derived 段标「分层译句」、translation-local 段标「本地译文摘句」",
    aiBadges.derived.length >= 1
      && aiBadges.local.filter(t => t.includes("本地译文摘句")).length >= 1
      && aiBadges.derived.length + aiBadges.local.filter(t => t.includes("本地译文摘句")).length
         + aiBadges.fallback.length >= 4,
    JSON.stringify(aiBadges));
  const aiL3 = await badges(3);
  check("L3 概括来源逐段判：model 段标「模型概括」、local 段标「本地摘取」",
    aiL3.model.filter(t => t.includes("模型概括")).length >= 1
      && aiL3.local.filter(t => t.includes("本地摘取")).length >= 1,
    JSON.stringify({ model: aiL3.model.length, localLocal: aiL3.local.length }));
  const aiScope = await page.evaluate(() => {
    const el = document.querySelector(".rdrr-layerstatus select");
    return el ? el.value : "";
  });
  check("有当前章时范围默认「当前章」（不静默整书）", aiScope === "section", `scope=${aiScope}`);

  // 当前章范围 → 请求只带这一章
  const scoped = [];
  const off = request => {
    if (request.method() === "POST" && request.url().includes("/reader/layers")) {
      try { scoped.push(JSON.parse(request.postData() || "{}")); } catch (e) { scoped.push({ parse: "fail" }); }
    }
  };
  page.on("request", off);
  await page.evaluate(() => {
    const button = [...document.querySelectorAll(".rdrr-layerstatus button")].find(b => b.textContent.includes("重新生成"));
    button?.click();
  });
  await page.waitForTimeout(2500);
  page.off("request", off);
  check("范围=当前章时请求只带该章 section_ids（不是整书）",
    scoped.length >= 1 && Array.isArray(scoped[0].section_ids) && scoped[0].section_ids.length === 1,
    JSON.stringify(scoped.map(item => item.section_ids)));
  await page.unroute(layersRoute);
  await page.unroute(stateRoute);

  // 5) 绑定被显式清空 → 模型按钮禁用并说明，本地预览仍在
  writeRoles({ ...base_roles, reader_fold: { service_id: "", model: "" }, reader_chat: base_roles.reader_chat });
  await open(false);
  const p3 = await panel();
  const isModelButton = text => /升级为模型折叠|重新生成|用模型折叠|重新折叠/.test(text);
  const modelButton = p3 && p3.buttons.find(b => isModelButton(b.text));
  check("reader_fold 被清空后模型折叠按钮禁用并给出原因",
    Boolean(modelButton && modelButton.disabled) && /不可用|清空/.test(p3.text),
    JSON.stringify({ buttons: p3 && p3.buttons, text: p3 ? p3.text.slice(0, 120) : "" }));
  check("清空后界面不提供可用的「升级/模型折叠」入口",
    Boolean(p3) && p3.buttons.filter(b => isModelButton(b.text)).every(b => b.disabled), JSON.stringify(p3 && p3.buttons));

  await browser.close();
  console.log(JSON.stringify({ results, errors }));
})();
"""


def main() -> int:
    if not NODE.is_file() or not PW.is_dir():
        print("SKIP: 缺少 ms-playwright-go node/package")
        return 2
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        print("FAIL: frontend/dist 不存在，请先 vite build")
        return 1

    temporary = Path(tempfile.mkdtemp(prefix="reader-fold-panel-"))
    runtime = temporary / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ.update(LITERATURE_RUNTIME=str(runtime), LITERATURE_LIBRARY=str(temporary / "library"),
                      LITERATURE_SESSION_TOKEN="")
    store, project_id = seed_project(temporary)
    config_path = runtime / "model-services.json"
    base_roles = json.loads(config_path.read_text(encoding="utf-8"))["roles"]

    api_port, web_port = free_port(), free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_RUNTIME": str(runtime), "LITERATURE_LIBRARY": str(temporary / "library"),
        "LITERATURE_SESSION_TOKEN": "", "LITERATURE_PORT": str(api_port),
        "PW_PACKAGE_PATH": str(PW), "PROJECT_ID": project_id,
        "CONFIG_PATH": str(config_path),
        "ROLES_BASE": json.dumps(base_roles, ensure_ascii=False),
        "API": f"http://127.0.0.1:{api_port}",
    })
    sidecar = subprocess.Popen([sys.executable, "-m", "literature.api"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import time

        import httpx

        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{api_port}/health", timeout=1, trust_env=False)
                break
            except Exception:
                time.sleep(0.2)
        dist = prepare_dist(temporary, api_port)
        start_web(dist, web_port)
        script = temporary / "check.cjs"
        script.write_text(CHECK_JS, encoding="utf-8")
        completed = subprocess.run([str(NODE), str(script), f"http://127.0.0.1:{web_port}"],
                                   capture_output=True, text=True, env=env, timeout=300)
    finally:
        sidecar.terminate()
        try:
            sidecar.wait(timeout=10)
        except subprocess.TimeoutExpired:
            sidecar.kill()
    if completed.returncode != 0:
        print("FAIL: 浏览器脚本失败\n", completed.stdout[-2000:], completed.stderr[-2000:])
        return 1
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    for item in payload["results"]:
        print(f'{"PASS" if item["pass"] else "FAIL"}: {item["name"]} — {item["detail"]}')
    if payload.get("errors"):
        print("脚本错误：", payload["errors"][:3])
    ok = all(item["pass"] for item in payload["results"]) and not payload.get("errors")
    print("PASS" if ok else "FAIL")
    shutil.rmtree(temporary, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
