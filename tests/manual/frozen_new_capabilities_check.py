"""冻结 sidecar 本轮新能力补充验收（不联网、不付费、不读 Keychain）。

在隔离库 + 冻结 sidecar 上验证 2026-10-25 两项修复已进入产物：
1. 导出主文件统一文档名：markdown/html 主文件为 <文档名>.md/.html（非 full.md/index.html）。
2. AI 目录校准零高级配置契约：/structure/jobs 接受仅 mode 的请求体（无高级字段也能建作业，
   走到模型调用前因无真实服务而失败——失败点证明参数解析与自动流程已生效）。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    binary = ROOT / "launcher/Literature.app/Contents/MacOS/literature-sidecar"
    if not binary.is_file():
        print("SKIP: 已安装 sidecar 不存在")
        return 2

    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    library, runtime, collector = base / "library", base / "runtime", base / "collector"
    for path in (library, runtime, collector):
        path.mkdir(parents=True, exist_ok=True)
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    from tests.manual.parse_ui_harness import make_pdf
    from literature.storage import ProjectStore
    from literature.mineru import import_mineru
    from tests.manual.parse_ui_harness import make_result_dir

    store = ProjectStore(library)
    source = make_pdf(collector / "中文 冻结书.pdf", 3)
    project = store.create_project(source, title="中文 冻结书")
    project_id = project["project_id"]
    import_mineru(store, project_id, [make_result_dir(collector / "results", "frozen", [1, 2])])

    port = free_port()
    env = dict(os.environ)
    env.update({
        "LITERATURE_LIBRARY": str(library),
        "LITERATURE_RUNTIME": str(runtime),
        "LITERATURE_SESSION_TOKEN": "frozen-test",
        "MINERU_API_TOKEN": "fake-frozen-token",
        "LITERATURE_PORT": str(port),
    })
    process = subprocess.Popen([str(binary)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    checks: list[tuple[str, bool]] = []
    try:
        import httpx

        client = None
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False,
                                      headers={"x-literature-token": "frozen-test"})
                if client.get("/health").status_code == 200:
                    break
                client = None
            except Exception:
                time.sleep(0.5)
        checks.append(["冻结 sidecar 可访问", client is not None])
        if client is None:
            return finish(checks)

        # ---- 1. 导出主文件统一文档名 ----
        export = client.post(f"/projects/{project_id}/export",
                             json={"formats": ["markdown", "html"], "mode": "source"})
        checks.append(["导出请求成功", export.status_code == 200])
        if export.status_code == 200:
            rels = [item["rel_path"] for item in export.json()["files"]]
            checks.append(["Markdown 主文件为文档名.md", "markdown/中文 冻结书.md" in rels])
            checks.append(["HTML 主入口为文档名.html", "html/中文 冻结书.html" in rels])
            checks.append(["不再产生 full.md / index.html",
                           "markdown/full.md" not in rels and "html/index.html" not in rels])
            html_entry = (Path(export.json()["path"]) / "html" / "中文 冻结书.html")
            checks.append(["HTML 主入口存在且含章节链接",
                           html_entry.is_file() and "chapter-0001.html" in html_entry.read_text(encoding="utf-8")])
            # 重命名单文件产物同步改名（HTML 主入口现在也是 single_file）。
            renamed = client.patch(f"/projects/{project_id}/exports/{export.json()['export_id']}",
                                   json={"display_name": "新名字"})
            rels2 = [item["rel_path"] for item in renamed.json()["files"]]
            checks.append(["重命名后 HTML 主入口同步改名", "html/新名字.html" in rels2])

        # ---- 2. 零高级配置契约：仅 mode 的请求体通过参数解析与档位展开，
        # 到达“模型绑定”门控（冻结环境无角色绑定 → 502 提示配置模型）。
        job = client.post(f"/projects/{project_id}/structure/jobs", json={"mode": "coarse"})
        checks.append(["仅 mode 请求通过解析并到达模型门控（502 提示绑定模型）",
                       job.status_code == 502 and "绑定模型" in str(job.json().get("detail", ""))])
        checks.append(["未建作业、不落伪基准", not (library / project_id / "document" / "toc_baseline.json").exists()])
        return finish(checks)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def finish(checks: list[tuple[str, bool]]) -> int:
    ok = True
    for label, value in checks:
        print(f"{'PASS' if value else 'FAIL'}: {label}")
        ok = ok and bool(value)
    print("结果：", "全部通过" if ok else "存在失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
