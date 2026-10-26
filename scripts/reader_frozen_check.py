"""冻结 sidecar 的阅读模块验收（隔离 runtime/library，真实书目副本，不调模型）。

覆盖：
- /health 含 reader_v1 / reader_v2 / reader_chat_v1 / reader_roles_v1；
- 冻结包里 TextFold 上游快照可运行：真实书目 → build_sections → 本地折叠作业完成，
  `textfold/doc` 结构齐全、evidence 0 起且在范围内、brief 逐字；
- 阅读层写入只落到 reader/，document.json 哈希不变；
- overlay revision 冲突返回 409；
- 未绑定模型时问答返回 400 且文案指向「阅读对话」；对话历史为空且可读。

用法：
    .venv/bin/python scripts/reader_frozen_check.py [--binary <sidecar 路径>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(port: int, token: str, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    import httpx

    response = httpx.request(
        method, f"http://127.0.0.1:{port}{path}",
        content=json.dumps(payload).encode() if payload is not None else None,
        headers={"x-literature-token": token, "content-type": "application/json"},
        timeout=120, trust_env=False,
    )
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"raw": response.text[:400]}


def real_book() -> Path | None:
    library = ROOT / "library"
    for path in sorted(library.iterdir() if library.is_dir() else []):
        if (path / "document" / "document.json").is_file():
            return path
    return None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", default=str(ROOT / "dist/sidecar/literature-sidecar"))
    args = parser.parse_args()
    binary = Path(args.binary)
    if not binary.is_file():
        print("SKIP: 冻结 sidecar 不存在：", binary)
        return 2
    book = real_book()
    if book is None:
        print("SKIP: library/ 里没有已解析的真实书目")
        return 2

    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))
        print(f"{'PASS' if ok else 'FAIL'}: {name}")

    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    runtime, library = base / "runtime", base / "library"
    runtime.mkdir(); library.mkdir()
    source = book / "document" / "document.json"

    from literature.storage import ProjectStore

    origin = base / "sample.pdf"
    origin.write_bytes(b"%PDF-1.4\n% reader frozen fixture")
    store = ProjectStore(library)
    project = store.create_project(origin, title=book.name[:60])
    project_id = str(project["project_id"])
    target = store.project_path(project_id) / "document" / "document.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    translation_source = book / "translations" / "zh-CN" / "translation.json"
    if translation_source.is_file():
        layer = store.translation_path(project_id, "zh-CN")
        layer.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(translation_source, layer)
    before = {path: sha256(path) for path in (source, target)}

    port = free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_LIBRARY": str(library),
        "LITERATURE_RUNTIME": str(runtime),
        "LITERATURE_SESSION_TOKEN": "reader-frozen",
        "LITERATURE_PORT": str(port),
    })
    process = subprocess.Popen([str(binary)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    token = "reader-frozen"
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
        caps = set(health.get("capabilities") or [])
        check("能力含 reader_v1 / reader_v2", {"reader_v1", "reader_v2"} <= caps)
        check("能力含 reader_chat_v1 / reader_roles_v1", {"reader_chat_v1", "reader_roles_v1"} <= caps)

        code, state = request(port, token, "GET", f"/projects/{project_id}/reader/state")
        check("GET /reader/state 可用", code == 200 and state.get("overlay", {}).get("revision") == 0)
        check("bbox_space 契约随状态返回", state.get("bbox_space") in ("mineru-1000", "none"))

        code, saved = request(port, token, "PUT", f"/projects/{project_id}/reader/overlay", {
            "overlay": {"annotations": [{
                "id": "frozen-1", "block_id": store.load_document(project_id).blocks[0].block_id,
                "pane": "source", "start": 0, "end": 4, "note": "冻结验收批注",
            }], "prefs": {"level": 3}},
            "base_revision": 0,
        })
        check("PUT /reader/overlay 首次成功且 revision=1", code == 200 and saved.get("revision") == 1)
        conflict_code, conflict = request(port, token, "PUT", f"/projects/{project_id}/reader/overlay", {
            "overlay": {"annotations": []}, "base_revision": 0,
        })
        check("过期 revision 返回 409", conflict_code == 409 and conflict.get("code") == "reader_revision_conflict")

        code, started = request(port, token, "POST", f"/projects/{project_id}/reader/layers", {
            "locale": "source", "mode": "local",
        })
        check("POST /reader/layers（本地折叠）受理", code == 200 and started.get("job_id"))
        job = None
        deadline = time.time() + 120
        while time.time() < deadline:
            _code, state = request(port, token, "GET", f"/projects/{project_id}/reader/state")
            job = state.get("layer_job") or {}
            if job.get("status") in ("completed", "failed"):
                break
            time.sleep(0.2)
        check("本地折叠作业完成（冻结包内 TextFold 快照可运行）", job.get("status") == "completed")
        check("作业汇总声明本地来源（ai_generated=false）", (job.get("result") or {}).get("ai_generated") is False)

        code, layers = request(port, token, "GET", f"/projects/{project_id}/reader/layers")
        sections = layers.get("sections") or []
        check("layers 返回 textfold/doc 结构", code == 200 and layers.get("format") == "textfold/doc" and bool(sections))
        violations = []
        for section in sections:
            for unit in section.get("units") or []:
                if unit.get("suppressed"):
                    continue
                for sentence in unit.get("brief") or []:
                    if sentence not in unit.get("full", ""):
                        violations.append(("verbatim", unit["id"]))
                if unit.get("evidence") and not set(unit["evidence"]) <= set(range(len(unit.get("sentences") or []))):
                    violations.append(("evidence", unit["id"]))
        check("brief 逐字来自原文、evidence 0 起且在范围内", not violations)

        code, markdown = request(port, token, "GET", f"/projects/{project_id}/reader/layers/markdown?level=topic")
        check("上游 fold.markdown 导出可用", code == 200 and bool(markdown))

        code, asked = request(port, token, "POST", f"/projects/{project_id}/reader/ask", {"question": "这本书讲了什么？"})
        check("未绑定模型时问答 400 且指向阅读对话", code == 400 and "阅读对话" in str(asked.get("detail")))

        code, chat = request(port, token, "GET", f"/projects/{project_id}/reader/chat")
        check("GET /reader/chat 可用（空历史）", code == 200 and chat.get("messages") == [])
        code, stopped = request(port, token, "POST", f"/projects/{project_id}/reader/chat/messages/missing/stop")
        check("对不存在消息 stop 返回 400", code == 400)

        after = {path: sha256(path) for path in (source, target)}
        check("事实来源文件哈希未变（真实库 + 隔离副本）", before == after)
        reader_dir = store.project_path(project_id) / "reader"
        check("阅读层落在 reader/ 下", reader_dir.is_dir() and (reader_dir / "overlay.json").is_file()
              and (reader_dir / "layers.json").is_file())
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        temporary.cleanup()

    failed = [name for name, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} PASS")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
