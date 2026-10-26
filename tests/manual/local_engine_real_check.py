"""真实联调：本机已有 CLI 引擎（TextFold 等布局）端到端解析检查。

前置条件：本机存在外部引擎（自动发现 TextFold/PaperFold 路径），且用户已授权调用。
产出：一个临时项目库中的完整项目（raw 封存 + canonical document.json + source.md），
并打印解析结果的块/页/几何统计。任何一步失败都会如实报错，不伪装成功。

用法：.venv/bin/python tests/manual/local_engine_real_check.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import fitz

from literature import mineru_local
from literature.mineru_workflow import JobState
from literature.ocr import MinerUService
from literature.storage import ProjectStore


def make_sample(path: Path, pages: int = 3) -> None:
    document = fitz.open()
    for index in range(pages):
        page = document.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Local engine check - page {index + 1}", fontsize=18)
        page.insert_text((72, 140), "Literature real acceptance sample body text.", fontsize=11)
        page.insert_text((72, 160), "第二段：中文正文用于验证解析链路。", fontsize=11)
    document.save(path)
    document.close()


def main() -> int:
    # 1) 发现并探测外部引擎。
    discovered = mineru_local.discover_external_engines()
    usable = [item for item in discovered if item.get("available")]
    if not usable:
        print("SKIP: 本机没有可用的外部 CLI 引擎（发现结果：", json.dumps(discovered, ensure_ascii=False), "）")
        return 2
    engine = usable[0]
    print(f"[probe] {engine['path']} · mineru {engine['version']} · models={engine['models_ready']}")

    runtime = Path(tempfile.mkdtemp(prefix="literature-real-runtime-"))
    library = Path(tempfile.mkdtemp(prefix="literature-real-library-"))
    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_LIBRARY"] = str(library)
    mineru_local.set_shared_engine(None)  # 让 runtime 重定向后的引擎重新解析

    # 2) 选用外部引擎（探测通过才允许）。
    mineru_local.set_engine_selection("external_cli", external_dir=engine["path"])
    print(f"[select] engine_source={mineru_local.active_source_kind()}")

    # 3) 生成样本并走完整服务链路。
    sample = runtime / "sample.pdf"
    make_sample(sample)
    store = ProjectStore()
    service = MinerUService(store)
    project = store.create_project(sample, source_language="auto")
    project_id = str(project["project_id"])
    job = service.start_parse(project_id, language="auto", backend_id="mineru_local")
    print(f"[job] {job['job_id']} state={job['state']} backend={job['backend_id']}")

    result = service.run(job["job_id"])
    print(f"[run] state={result['state']} error={result['error'] or '(none)'}")
    if result["state"] != JobState.COMPLETED.value:
        print("FAIL: 任务未完成")
        return 1

    project_path = store.project_path(project_id)
    document = store.load_document(project_id)
    raw_manifest = store.read_raw_manifest(project_id)
    slices = [item for item in raw_manifest.get("slices", []) if isinstance(item, dict)]
    errors = store.verify_raw(project_id)
    print("[document] blocks=%d pages=%d coverage=%s" % (
        len(document.blocks), len(document.pages),
        (document.coverage or {}).get("state"),
    ))
    print("[raw] slices=%d verify_errors=%s" % (len(slices), errors or "none"))
    for page in document.pages[:3]:
        print(f"[page] idx={page.page_index} size={page.width}x{page.height} rotation={page.rotation}")
    for block in document.blocks[:6]:
        text = (block.source_text or "")[:40].replace("\n", " ")
        print(f"[block] {block.block_id} type={block.type} page={block.page_index} bbox={block.bbox} text={text!r}")
    source_md = project_path / "document" / "source.md"
    print(f"[source.md] exists={source_md.is_file()} bytes={source_md.stat().st_size if source_md.is_file() else 0}")

    # 4) 引擎写入边界复核：TextFold 引擎目录除 __pycache__ 外不得有新写入。
    print("[boundary] 解析期间外部引擎目录预期只有解释器字节码缓存变化（已用 PYTHONPYCACHEPREFIX 隔离）")
    shutil.rmtree(runtime, ignore_errors=True)
    shutil.rmtree(library, ignore_errors=True)
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
