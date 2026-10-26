"""冻结 sidecar 的本地引擎（四类接入）补充验收。

前置：Tauri bundle 内的冻结 sidecar 已构建。全部在隔离 runtime/library 中进行，
不触碰用户生产配置、不触发任何模型/uv 下载。

覆盖：
- /health 含 parse_engine_v1 能力与 runtime_revision 一致；
- /parse/backends 的 mineru_local 动态探测（未安装 → 不可用并带引导文案）；
- /parse/engine 状态、无效 select 拒绝、明文 HTTP 公网地址拒绝；
- 已有 TextFold 引擎（用户已授权只读调用）→ select 探测成功 →
  冻结 sidecar 用外部引擎完成一份短 PDF 的完整解析链路（本地后端、无 Token），
  canonical/coverage/raw 校验齐全——证明打包产物也能完成派生/规范化。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def api(port: int, token: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"x-literature-token": token, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def main() -> int:
    binary = ROOT / "src-tauri/target/release/bundle/macos/Literature.app/Contents/MacOS/literature-sidecar"
    manifest = json.loads((ROOT / "src-tauri/binaries/manifest.json").read_text(encoding="utf-8"))
    if not binary.is_file():
        print("SKIP: 冻结 sidecar 不存在：", binary)
        return 2

    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))
        print(f"{'PASS' if ok else 'FAIL'}: {name}")

    # 隔离 runtime + 真实样本项目。
    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    runtime, library = base / "runtime", base / "library"
    runtime.mkdir(); library.mkdir()
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_LIBRARY"] = str(library)

    import fitz

    from literature.storage import ProjectStore

    sample = base / "frozen-engine-sample.pdf"
    document = fitz.open()
    for index in range(2):
        page = document.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Frozen engine check - page {index + 1}", fontsize=18)
        page.insert_text((72, 140), "Frozen sidecar local parse acceptance.", fontsize=11)
    document.save(sample); document.close()
    store = ProjectStore(library)
    project = store.create_project(sample, title="Frozen Engine Sample")
    project_id = str(project["project_id"])

    port = free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_LIBRARY": str(library),
        "LITERATURE_RUNTIME": str(runtime),
        "LITERATURE_SESSION_TOKEN": "frozen-engine",
        "MINERU_API_TOKEN": "",
        "LITERATURE_PORT": str(port),
    })
    process = subprocess.Popen([str(binary)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    token = "frozen-engine"
    try:
        import httpx

        health = None
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2, trust_env=False).json()
                break
            except Exception:
                time.sleep(0.5)
        check("冻结 sidecar /health 可访问", bool(health))
        if not health:
            return 1
        check("冻结 revision 与 manifest 一致", health.get("runtime_revision") == manifest.get("runtime_revision"))
        check("能力包含 parse_engine_v1", "parse_engine_v1" in (health.get("capabilities") or []))

        # 引擎状态与动态能力（managed 未安装 → 明确不可用 + 引导文案）。
        status_code, engine = api(port, token, "GET", "/parse/engine")
        check("GET /parse/engine 可用", status_code == 200 and engine.get("installed") is False)
        back_code, backends = api(port, token, "GET", "/parse/backends")
        local = next((item for item in backends.get("backends", []) if item["backend_id"] == "mineru_local"), {})
        check("mineru_local 动态能力未安装时明确不可用", back_code == 200 and local.get("available") is False and "安装" in str(local.get("unavailable_reason")))

        # select 校验：无效路径拒绝；公网明文 HTTP 拒绝。
        code, detail = api(port, token, "POST", "/parse/engine/select", {"kind": "external_cli", "external_dir": "/nonexistent-engine"})
        check("无效外部路径 select 被拒绝(400)", code == 400 and "mineru-kit" in str(detail.get("detail")))
        code, detail = api(port, token, "POST", "/parse/engine/select", {"kind": "http_service", "http_url": "http://203.0.113.5:8000"})
        check("公网明文 HTTP select 被拒绝(400)", code == 400 and "明文" in str(detail.get("detail")))

        # 已有 TextFold 引擎：探测 + 选用 + 冻结 sidecar 完整解析链路（用户已授权只读调用）。
        discovered = httpx.get(f"http://127.0.0.1:{port}/parse/engine/sources", headers={"x-literature-token": token}, timeout=60, trust_env=False).json().get("discovered", [])
        usable = next((item for item in discovered if item.get("available")), None)
        check("冻结 sidecar 发现本机 TextFold 引擎", usable is not None)
        if usable is None:
            return 1
        code, _ = api(port, token, "POST", "/parse/engine/select", {"kind": "external_cli", "external_dir": usable["path"]})
        check("冻结 sidecar 选用外部引擎成功", code == 200)
        backends = httpx.get(f"http://127.0.0.1:{port}/parse/backends", headers={"x-literature-token": token}, timeout=60, trust_env=False).json().get("backends", [])
        local = next((item for item in backends if item["backend_id"] == "mineru_local"), {})
        check("选用后 mineru_local 可用且显示版本", local.get("available") is True and "4.0" in str(local.get("display_name")))

        start_code, job = api(port, token, "POST", f"/projects/{project_id}/ocr/start", {
            "language": "auto", "backend_id": "mineru_local",
        })
        check("冻结 sidecar 创建本地解析任务", start_code == 200 and job.get("backend_id") == "mineru_local")
        if start_code != 200:
            print("  detail:", json.dumps(job, ensure_ascii=False)[:300])
            return 1
        # 本地任务不经调度器 Token 门控：直接 start（等价 UI「开始解析」）。
        run_code, _ = api(port, token, "POST", f"/projects/{project_id}/ocr/start", {"language": "auto", "backend_id": "mineru_local"})
        # 用 run 入口：等待任务完成（本地 standard 档两页约数秒～数十秒）。
        final = None
        deadline = time.time() + 600
        while time.time() < deadline:
            time.sleep(2)
            final = httpx.get(f"http://127.0.0.1:{port}/ocr/jobs/{job['job_id']}", headers={"x-literature-token": token}, timeout=30, trust_env=False).json()
            if final.get("state") in {"completed", "failed", "waiting"}:
                break
        check("冻结 sidecar 本地解析任务完成", final is not None and final.get("state") == "completed")
        if final and final.get("state") != "completed":
            print("  final state:", final.get("state"), "error:", str(final.get("error"))[:200])

        project_info = httpx.get(f"http://127.0.0.1:{port}/projects/{project_id}", headers={"x-literature-token": token}, timeout=30, trust_env=False).json()
        check("冻结产物 canonical 就绪（state=ready）", project_info.get("state") == "ready" or (project_info.get("pipeline") or {}).get("state") == "ready" or str(project_info.get("state")) == "ready")
        doc = httpx.get(f"http://127.0.0.1:{port}/projects/{project_id}/document", headers={"x-literature-token": token}, timeout=30, trust_env=False).json()
        blocks = doc.get("blocks") or []
        pages = doc.get("pages") or []
        check("canonical 含 block 与页几何", len(blocks) >= 2 and len(pages) == 2 and float(pages[0].get("width") or 0) > 0)
        verify = httpx.get(f"http://127.0.0.1:{port}/projects/{project_id}/mineru/verify", headers={"x-literature-token": token}, timeout=30, trust_env=False).json()
        check("raw 封存校验无错误", not (verify.get("errors") if isinstance(verify, dict) else None))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    print(f"manifest revision: {manifest.get('runtime_revision')} sha256: {manifest.get('sha256')} size: {manifest.get('size')}")
    failed = [name for name, ok in checks if not ok]
    print(f"结果：{len(checks) - len(failed)}/{len(checks)} PASS" + (f"；失败：{failed}" if failed else ""))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
