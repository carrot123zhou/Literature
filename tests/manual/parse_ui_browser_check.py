"""真实浏览器（Playwright）对本地 API + 生产构建前端的全流程验收。

在允许子进程的环境（本地终端）中运行：
    source .venv/bin/activate && python tests/manual/parse_ui_browser_check.py

外部解析服务使用能真正跑完整条流水线的 ``tests/manual/fake_mineru.py``（不联网、
不读 Keychain）。覆盖：继续解析到全文与 UI 刷新同步、点击校对块真实定位源页 5/7、
失败切片重试、中断恢复、双击继续解析、旧 unknown 迁移（成功与失败反馈）、
队列 UI 调序并核对 fake 实际执行序列、窄窗口阅读截图、0 脚本错误。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def prepare_dist(base: Path, api_port: int) -> Path:
    target = base / "dist"
    shutil.copytree(ROOT / "frontend" / "dist", target)
    for file in target.rglob("*"):
        if file.suffix in {".js", ".html", ".css"}:
            text = file.read_text(encoding="utf-8", errors="ignore")
            if "127.0.0.1:41731" in text:
                file.write_text(text.replace("127.0.0.1:41731", f"127.0.0.1:{api_port}"), encoding="utf-8")
    return target


def start_web(dist: Path, port: int) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(dist), **kwargs)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def node_env() -> dict[str, str]:
    packages = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/package")))
    env = dict(os.environ)
    if packages:
        env["PW_PACKAGE_PATH"] = packages[-1]
    return env


def node_binary() -> str | None:
    nodes = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/node")))
    return nodes[-1] if nodes else None


def run_node(script: str, args: list[str], env: dict[str, str], timeout: int = 400) -> dict:
    import subprocess

    node = node_binary()
    if not node or "PW_PACKAGE_PATH" not in env:
        return {"checks": [], "error": "no ms-playwright-go node/package"}
    result = subprocess.run([node, str(ROOT / "tests/manual" / script), *args], capture_output=True, text=True, timeout=timeout, env=env)
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if not line:
        return {"checks": [], "error": f"no stdout; stderr={result.stderr[-800:]}"}
    try:
        return json.loads(line)
    except ValueError:
        return {"checks": [], "error": f"non-json: {line[:500]}"}


def make_interrupted_job(app) -> str:
    from tests.manual.parse_ui_harness import make_pdf

    store = app.state.store
    service = app.state.ocr_service
    collector = Path(os.environ["LITERATURE_COLLECTOR"])
    source = make_pdf(collector / "Resume Book.pdf", 4)
    project = store.create_project(source, title="Resume Book")
    job = service.create(project["project_id"], language="ch")
    path = service._job_path(job["job_id"])
    value = json.loads(path.read_text(encoding="utf-8"))
    value["state"] = "interrupted"
    path.write_text(json.dumps(value), encoding="utf-8")
    service.queue.remove(job["job_id"])
    return job["job_id"]


def run_reorder_phase(app, web_port: int, api_port: int, env: dict[str, str]) -> list[list]:
    """队列 UI 调序 → 发布 fake → 核对 fake 实际执行顺序。"""
    from tests.manual.fake_mineru import FakeMinerU

    store = app.state.store
    service = app.state.ocr_service
    titles = ["Queue A", "Queue B", "Queue C"]
    by_title = {p["title"]: p["project_id"] for p in store.list_projects()}
    checks: list[list] = []
    hold = threading.Event()
    FakeMinerU.hold = hold
    job_ids: dict[str, str] = {}
    for title in titles:
        job, _skipped = service.continue_parse(by_title[title], page_ranges="")
        job_ids[title] = job["job_id"]
        service.start_job(job["job_id"])
    deadline = time.time() + 20
    while time.time() < deadline:
        if service.is_running(job_ids["Queue A"]) and service.queue.order() == [job_ids["Queue B"], job_ids["Queue C"]]:
            break
        time.sleep(0.1)
    checks.append(["队列中 A 运行、B/C 按序等待", service.is_running(job_ids["Queue A"]) and service.queue.order() == [job_ids["Queue B"], job_ids["Queue C"]]])
    payload = run_node("parse_ui_queue_reorder.cjs", [str(web_port), str(api_port), "Queue C"], env, timeout=120)
    checks.extend([(label, bool(value)) for label, value in payload.get("checks", [])])
    if not payload.get("checks"):
        checks.append([f"队列 UI 调序脚本失败：{payload.get('error')}", False])
    FakeMinerU.hold = None
    hold.set()
    deadline = time.time() + 60
    while time.time() < deadline:
        if all(service._job_state(job_id) == "completed" for job_id in job_ids.values()):
            break
        time.sleep(0.1)
    executed = []
    for batch in FakeMinerU.executed:
        job_id = FakeMinerU.batches.get(batch, [""])[0].rsplit("-001", 1)[0]
        if job_id in job_ids.values():
            executed.append(next(title for title, value in job_ids.items() if value == job_id))
    checks.append(["fake 实际执行顺序为 A→C→B", executed == ["Queue A", "Queue C", "Queue B"]])
    return checks


def run_history_phase(web_port: int, api_port: int, env: dict[str, str]) -> tuple[list[list], dict]:
    """解析历史 / 首页分区 / 保留期设置的真实浏览器验收。"""
    payload = run_node("parse_ui_history_check.cjs", [str(web_port), str(api_port)], env, timeout=420)
    return [(label, bool(value)) for label, value in payload.get("checks", [])], payload


def main() -> int:
    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    library, runtime, collector = base / "library", base / "runtime", base / "collector"
    for path in (library, runtime, collector):
        path.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_COLLECTOR"] = str(collector)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""

    from literature.storage import ProjectStore
    from tests.manual.fake_mineru import FakeMinerU
    from tests.manual.parse_ui_harness import configure, seed

    store = ProjectStore(library)
    seed(store, collector, runtime)

    api_port = free_port()
    web_port = free_port()
    dist = prepare_dist(base, api_port)
    app, _service = configure(store, runtime, run_jobs=True)
    # 仅让 Retry Book 的继续范围 3-20 首次上传失败一次；精确集合匹配，
    # 不会影响 Fresh Book 等包含第 10 页的跨 200 页全量解析。
    FakeMinerU.fail_once_pages = set(range(3, 21))  # Retry Book 继续范围

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(80):
        if server.started:
            break
        time.sleep(0.25)
    make_interrupted_job(app)
    start_web(dist, web_port)

    shot_dir = ROOT / "docs" / "verification"
    shot_dir.mkdir(parents=True, exist_ok=True)
    env = node_env()
    env["SHOT_DIR"] = str(shot_dir)

    payload = run_node("parse_ui_flow_check.cjs", [str(web_port), str(api_port)], env, timeout=500)
    checks = payload.get("checks", [])
    if not checks:
        print("FLOW FAILED:", payload.get("error", "unknown"))
        server.should_exit = True
        return 2
    checks.extend(run_reorder_phase(app, web_port, api_port, env))
    history_checks, history_payload = run_history_phase(web_port, api_port, env)
    checks.extend(history_checks)
    if history_payload.get("consoleErrors"):
        payload.setdefault("consoleErrors", []).extend(history_payload["consoleErrors"])

    ok = True
    for label, value in checks:
        print(f"{'PASS' if value else 'FAIL'}: {label}")
        ok = ok and bool(value)
    print("console errors:", payload.get("consoleErrors"))
    print("flow debug:", payload.get("debug"))
    print("screenshots:", shot_dir / "review-narrow.png", shot_dir / "reading-narrow.png")
    print("结果：", "全部通过" if ok else "存在失败")
    server.should_exit = True
    temporary.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
