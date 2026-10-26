"""聊天面板的模型来源展示：必须以服务端 role_state 为准（继承/清空/失效/自有绑定）。

复现的真实缺陷（2026-10-25）：
- 面板只按 `translationBinding` 判断，于是 `reader_chat` 被**显式清空**或绑定的服务**已删除**时，
  界面仍显示「继承翻译绑定」，把「不可用」说成「继承」；
- `reader_chat` 绑定了服务但没指定模型时，服务端会按该服务第一个模型兜底，界面也不说明。

本脚本用隔离 runtime + 真实本地 API + 生产构建前端，依次写入四种角色配置并断言 `.rdrr-ai-sub` 文案。
不调模型、不写生产库。
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

NODE = Path.home() / "Library/Caches/ms-playwright-go/1.57.0/node"
PW = Path.home() / "Library/Caches/ms-playwright-go/1.57.0/package"

CHECK_JS = r"""
const { chromium } = require(process.env.PW_PACKAGE_PATH);
(async () => {
  const base = process.argv[2];
  const url = base + "/index.html#/p/" + process.env.PROJECT_ID + "/reader";
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1360, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const results = [];
  const check = (name, pass, detail) => results.push({ name, pass: Boolean(pass), detail: detail || "" });
  const fs = require("fs");
  const writeConfig = (roles) => {
    const path = process.env.CONFIG_PATH;
    const cfg = JSON.parse(fs.readFileSync(path, "utf8"));
    cfg.roles = roles;
    fs.writeFileSync(path, JSON.stringify(cfg, null, 2));
  };
  let first = true;
  const readSub = async () => {
    // 同一 hash URL 的 goto 不会重新请求文档；改用 reload（首次除外）才能读到最后写入的配置。
    if (first) { await page.goto(url, { waitUntil: "networkidle" }); first = false; }
    else { await page.reload({ waitUntil: "networkidle" }); }
    await page.waitForTimeout(2200);
    // 用 DOM click：Playwright 的可操作/稳定性检查会因面板打开引起的布局位移而超时
    const opened = await page.evaluate(() => {
      const button = document.getElementById("btn-ai");
      if (!button) return false;
      button.click();
      return true;
    });
    if (!opened) throw new Error("#btn-ai 不存在（阅读器未渲染）");
    await page.waitForTimeout(500);
    const sub = page.locator(".rdrr-ai-sub").first();
    await sub.waitFor({ state: "visible", timeout: 5000 });
    return (await sub.innerText()).trim();
  };

  const base_roles = JSON.parse(process.env.ROLES_BASE);

  writeConfig(base_roles);
  const inherited = await readSub();
  check("旧配置（无阅读角色）显示「继承翻译默认」", inherited.includes("继承翻译默认"), inherited);

  writeConfig({ ...base_roles, reader_chat: { service_id: "", model: "" } });
  const cleared = await readSub();
  check("显式清空 reader_chat 显示未配置/已清空，不伪装成继承",
    cleared.includes("显式清空") && !cleared.includes("继承翻译默认"), cleared);

  writeConfig({ ...base_roles, reader_chat: { service_id: "svc-gone", model: "m" } });
  const invalid = await readSub();
  check("reader_chat 指向已删除服务显示不可用，不伪装成继承",
    invalid.includes("不可用") && !invalid.includes("继承翻译默认"), invalid);

  writeConfig({ ...base_roles, reader_chat: { service_id: "svc-a", model: "" } });
  const firstModel = await readSub();
  check("绑定服务未指定模型时说明「用第一个模型」",
    firstModel.includes("阅读对话模型") && firstModel.includes("第一个模型"), firstModel);

  writeConfig({ ...base_roles, reader_chat: { service_id: "svc-a", model: "model-a1" } });
  const bound = await readSub();
  check("自有阅读对话绑定显示「阅读对话模型」与实际模型",
    bound.includes("阅读对话模型") && bound.includes("svc A") && bound.includes("模型 A1"), bound);

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

    temporary = Path(tempfile.mkdtemp(prefix="reader-role-ui-"))
    runtime, library = temporary / "runtime", temporary / "library"
    runtime.mkdir(); library.mkdir()
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ.update(LITERATURE_RUNTIME=str(runtime), LITERATURE_LIBRARY=str(library), LITERATURE_SESSION_TOKEN="")

    base_roles = {
        "structure": {"service_id": "svc-a", "model": "model-a1"},
        "translation": {"service_id": "svc-a", "model": "model-a1"},
    }
    config = {
        "version": 2,
        "services": [{
            "service_id": "svc-a", "name": "svc A", "preset": "custom",
            "base_url": "https://a.invalid", "allow_plaintext_http": False,
            "models": [
                {"model": "model-a1", "name": "模型 A1", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2},
                {"model": "model-a2", "name": "模型 A2", "context_window": 0, "max_output_tokens": 4096, "temperature": 0.2},
            ],
        }],
        "roles": base_roles,
    }
    (runtime / "model-services.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    from literature.models import Block, Document, Page
    from literature.storage import ProjectStore

    store = ProjectStore(library)
    origin = temporary / "origin.pdf"
    origin.write_bytes(b"%PDF-1.4\n% role ui fixture")
    project = store.create_project(origin, title="角色来源展示验收")
    project_id = str(project["project_id"])
    store.save_document(Document(
        project_id=project_id, title="角色来源展示验收", source_language="en", page_count=1,
        pages=[Page(page_index=0, width=600, height=800)],
        blocks=[Block(block_id="b1", type="paragraph", order=0, page_index=0,
                      source_text="A paragraph used only for the reader role display check.")],
    ))

    api_port, web_port = free_port(), free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_RUNTIME": str(runtime), "LITERATURE_LIBRARY": str(library),
        "LITERATURE_SESSION_TOKEN": "", "LITERATURE_PORT": str(api_port),
        "PW_PACKAGE_PATH": str(PW), "PROJECT_ID": project_id,
        "CONFIG_PATH": str(runtime / "model-services.json"),
        "ROLES_BASE": json.dumps(base_roles, ensure_ascii=False),
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
    out = ROOT / "tmp" / "reader-role-ui-result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(temporary, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
