"""AIFold 进度详情（`run.activity`）前端验收服务。

隔离环境：真实生产构建前端 + 真实本地 API（临时库/runtime，无模型服务、零付费、不读 Keychain），
fold-run 的状态由 Playwright 路由注入的脚本化快照驱动（见同目录 reader_fold_activity_check.cjs
的文件头契约说明）。因此本探针只验证**前端呈现契约**：

- 阶段 / 当前节 / 批次 / 实时计时 / 待处理 / 失败 / 重试 的紧凑显示；
- 计时在「生成中」与「等待响应」两种状态下都真实推进（用后端时间戳 + 本地时钟）；
- 暂停后停表、同一按钮变「继续」，恢复后继续计时；
- 整体失败后活动行清除但错误仍可见；
- 后端**没有** activity（旧 run/旧后端）时诚实降级：不编造阶段、不崩、进度环照常；
- 单一 AIFold 按钮 + 进度环不变，窄屏不横向溢出（活动摘要落到已有副标题）。

真实后端是否写入这些字段不在此证明，后端侧另有单测/契约测试。

用法：.venv/bin/python tests/manual/reader_fold_activity_check.py
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web  # noqa: E402
from tests.manual.reader_ui_browser_check import make_document, make_real_pdf  # noqa: E402


def seed_project(base: Path) -> tuple[object, str]:
    """一个带 document + 译文的最小项目；不写 model-services（不需要也不该需要模型）。"""
    from literature.models import TranslationEntry, TranslationLayer
    from literature.storage import ProjectStore

    store = ProjectStore(base / "library")
    source = base / "fold-activity.pdf"
    make_real_pdf(source, pages=2)
    project = store.create_project(source, title="折叠进度验收样本")
    project_id = project["project_id"]
    make_document(store, project_id, "折叠进度验收样本")
    store.save_translation(TranslationLayer(project_id=project_id, locale="zh-CN", entries={
        "b-title-1": TranslationEntry(block_id="b-title-1", text="导论", status="translated"),
        "b-p-1": TranslationEntry(block_id="b-p-1", text="银器沿丝绸之路流动。", status="translated"),
        "b-p-2": TranslationEntry(block_id="b-p-2", text="检测数据显示合金含银百分之九十二。", status="translated"),
        "b-title-2": TranslationEntry(block_id="b-title-2", text="结论", status="translated"),
        "b-list": TranslationEntry(block_id="b-list", items=["窖藏集中在港口城市。", "内陆发现变少。"], status="translated"),
        "b-p-3": TranslationEntry(block_id="b-p-3", text="这一段没有译文，必须回退原文。", status="translated"),
    }))
    return store, project_id


def main() -> int:
    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    runtime = base / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")   # 生产构建前端未注入 Token
    os.environ["LITERATURE_LIBRARY"] = str(base / "library")
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""

    store, project_id = seed_project(base)

    import uvicorn

    from literature.api import create_app

    app = create_app(store)
    api_port, web_port = free_port(), free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not getattr(server, "started", False) and time.time() < deadline:
        time.sleep(0.1)

    dist = prepare_dist(Path(tempfile.mkdtemp(prefix="fold-activity-web-")), api_port)
    web = start_web(dist, web_port)

    packages = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/package")))
    nodes = sorted(glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/node")))
    if not packages or not nodes:
        print(json.dumps({"checks": [], "error": "no ms-playwright-go node/package"}))
        return 2
    env = dict(os.environ)
    env["PW_PACKAGE_PATH"] = packages[-1]
    result_path = base / "reader-fold-activity.json"
    result = subprocess.run(
        [nodes[-1], str(ROOT / "tests/manual/reader_fold_activity_check.cjs"),
         str(web_port), str(api_port), project_id, str(result_path)],
        capture_output=True, text=True, timeout=600, env=env,
    )
    line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    try:
        payload = json.loads(line)
    except Exception:  # noqa: BLE001 - 无输出时如实报错，不伪造通过
        payload = {"checks": [], "error": f"no stdout; stderr={result.stderr[-1200:]}"}
    payload["result_path"] = str(result_path)
    print(json.dumps(payload, ensure_ascii=False))

    server.should_exit = True
    web.shutdown()
    temporary.cleanup()

    checks = payload.get("checks") or []
    failed = [item for item in checks if not item.get("pass")]
    for item in failed:
        print(f"FAIL · {item.get('name')} · {item.get('detail')}", file=sys.stderr)
    ok = bool(checks) and not failed and not payload.get("pageerrors")
    print(f"{len(checks) - len(failed)}/{len(checks)} 通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
