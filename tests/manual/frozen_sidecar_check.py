"""冻结 sidecar 的隔离 API 验收（不联网、不读 Keychain、不安装）。

流程：在隔离库中构造一个“旧 normalize + 可信历史作业”的资源，然后启动打包后的
冻结 sidecar 二进制（Tauri bundle 内），核对：
- `/health` 的 runtime_revision 与 `src-tauri/binaries/manifest.json` 一致；
- 能力包含 `parse_lifecycle_v1`；
- `/projects/{id}/parse/migrate` 在冻结进程中按证据恢复覆盖（证明新代码已进入产物）。

MINERU_API_TOKEN 设为假值，避免触发 Keychain 读取；隔离 runtime 中没有可恢复作业，
调度器不会发任何外部请求。
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


def freeze_library(library: Path, runtime: Path, collector: Path) -> str:
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    from tests.manual.parse_ui_harness import make_pdf, make_result_dir
    from literature.storage import ProjectStore

    store = ProjectStore(library)
    source = make_pdf(collector / "Frozen Prefix.pdf", 6)
    project = store.create_project(source, title="Frozen Prefix")
    project_id = project["project_id"]
    from literature.mineru import import_mineru

    import_mineru(store, project_id, [make_result_dir(collector / "results", "frozen", [1, 2, 3])])
    from literature.mineru_workflow import JobState, MinerUWorkflow, SliceJob, SliceState
    from literature.storage import atomic_json

    workflow = MinerUWorkflow(runtime / "ocr-jobs")
    job = workflow.create_job(
        store.project_path(project_id) / "source" / "origin.pdf",
        store.project_path(project_id) / "ocr" / ".x.epub",
        page_ranges="1-3",
    )
    job.state = JobState.COMPLETED
    job.slices = [SliceJob(1, 1, 3, "d", "a.pdf", "/tmp/a.pdf", state=SliceState.DOWNLOADED)]
    workflow.save_job(job)
    atomic_json(workflow.jobs_root / job.job_id / "literature-bridge.json", {"project_id": project_id})
    # 另建一个从未解析的已有资源，验证冻结产物已包含“未解析+页数”的覆盖报告。
    fresh_source = make_pdf(collector / "Frozen Fresh.pdf", 5)
    store.create_project(fresh_source, title="Frozen Fresh")
    return project_id


def main() -> int:
    binary = ROOT / "src-tauri/target/release/bundle/macos/Literature.app/Contents/MacOS/literature-sidecar"
    manifest = json.loads((ROOT / "src-tauri/binaries/manifest.json").read_text(encoding="utf-8"))
    if not binary.is_file():
        print("SKIP: 冻结 sidecar 不存在：", binary)
        return 2

    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    library, runtime, collector = base / "library", base / "runtime", base / "collector"
    for path in (library, runtime, collector):
        path.mkdir(parents=True, exist_ok=True)
    project_id = freeze_library(library, runtime, collector)

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

        health = None
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2, trust_env=False).json()
                break
            except Exception:
                time.sleep(0.5)
        checks.append(["冻结 sidecar /health 可访问", bool(health)])
        if health:
            checks.append(["冻结 revision 与 manifest 一致", health.get("runtime_revision") == manifest.get("runtime_revision")])
            checks.append(["能力包含 parse_lifecycle_v1", "parse_lifecycle_v1" in (health.get("capabilities") or [])])
            checks.append(["能力包含 task_history_v1", "task_history_v1" in (health.get("capabilities") or [])])
            # 模型配置重设计（/v1/models 自动回填 + 任务级思考档位）新增的能力标记。
            checks.append(["能力包含 model_services_v2", "model_services_v2" in (health.get("capabilities") or [])])
            # 服务商 → 模型两层结构（任务内选模型）新增的能力标记。
            checks.append(["能力包含 model_services_v3", "model_services_v3" in (health.get("capabilities") or [])])
        headers = {"x-literature-token": "frozen-test"}
        projects = httpx.get(f"http://127.0.0.1:{port}/projects", headers=headers, timeout=10, trust_env=False).json()
        checks.append(["冻结 API /projects 可用", isinstance(projects, list) and len(projects) == 2])
        fresh = next((item for item in projects if item.get("title") == "Frozen Fresh"), None)
        checks.append(["冻结 API 未解析资源报告 unparsed + 页数", bool(fresh) and fresh["parse_coverage"]["state"] == "unparsed" and fresh["parse_coverage"]["source_page_count"] == 5])
        settings = httpx.get(f"http://127.0.0.1:{port}/ocr/archive-settings", headers=headers, timeout=10, trust_env=False).json()
        checks.append(["冻结 API 默认保留期 90 天/1000 条", settings == {"retention_days": 90, "max_records": 1000}])
        tasks = httpx.get(f"http://127.0.0.1:{port}/ocr/tasks", headers=headers, params={"scope": "active"}, timeout=10, trust_env=False).json()
        checks.append(["冻结 API 任务中心可用", isinstance(tasks.get("items"), list) and "counts" in tasks])
        # 免保存拉取模型的新端点：空 Base URL 应报业务错误（4xx/5xx），绝不能是 404（旧产物没有该路由）。
        probe = httpx.post(f"http://127.0.0.1:{port}/model-services/models", headers=headers, json={"base_url": ""}, timeout=10, trust_env=False)
        checks.append(["冻结产物含 /model-services/models 免保存拉取端点", probe.status_code != 404])
        # v3 契约：roles 是 {service_id, model} 绑定对象（旧产物是字符串）。
        service_view = httpx.get(f"http://127.0.0.1:{port}/model-services", headers=headers, timeout=10, trust_env=False).json()
        checks.append(["冻结产物 v3：roles 是绑定对象", isinstance(service_view.get("roles", {}).get("structure"), dict)])
        migrated = httpx.post(f"http://127.0.0.1:{port}/projects/{project_id}/parse/migrate", headers=headers, timeout=20, trust_env=False).json()
        checks.append(["冻结进程按证据迁移旧前缀资源", bool(migrated.get("migrated"))])
        checks.append(["迁移 mode=legacy_prefix", migrated.get("mode") == "legacy_prefix"])
        checks.append(["迁移结果为部分解析 3/6", migrated.get("coverage", {}).get("state") == "partial" and migrated.get("coverage", {}).get("covered_pages") == 3 and migrated.get("coverage", {}).get("source_page_count") == 6])
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    ok = True
    for label, value in checks:
        print(f"{'PASS' if value else 'FAIL'}: {label}")
        ok = ok and bool(value)
    print("manifest revision:", manifest.get("runtime_revision"), "sha256:", manifest.get("sha256"), "size:", manifest.get("size"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
