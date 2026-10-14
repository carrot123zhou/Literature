"""手动基准：结构校对复核模式（full / sample / off）在真实文档上的请求与输入开销。

只读真实库，把 document 复制到临时 ProjectStore，用 mock provider 跑真实代码路径
``run_structure_job``；**不调用任何模型、不写真实库、不建前端**。输出的字符数与
“模型需要逐条作答的条数”是输入/输出 token 的代理指标，不是真实账单。

用法（默认扫描本机 library 里的已知文献）：
    .venv/bin/python tests/manual/structure_review_mode_benchmark.py
    .venv/bin/python tests/manual/structure_review_mode_benchmark.py --out /tmp/review-mode.json
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from literature.models import Document  # noqa: E402
from literature.providers import ProviderResponse  # noqa: E402
from literature.storage import ProjectStore  # noqa: E402
from literature.structure import run_structure_job  # noqa: E402

PROJECTS = [
    ("WhiteLotus", "library/The White Lotus Teachings*/", "bdb72fda156045379ab8181f32cc16f3"),
    ("MetJournal", "library/The_Metropolitan_Museum*/", "31f1beb79c884e618334463cdbfbd6dc"),
    ("Authentic", "library/On Being Authentic*/", "0c0a11e1d6b3426b811e6d3cd54b1989"),
    ("WritingAuth", "library/Writing and Authority*/", "27a59a9895284ac1bd316fe3280c94cb"),
    ("Transcendents", "library/Making Transcendents*/", "908c30dfaef44a59acd07683a9770f9d"),
]


def derive_plan(document: Document) -> dict:
    """把文档自身的结构当作“第一遍规划的合理近似”：标题→h{level}（父=前一个更高级标题），
    其余块→body/footnote/caption。

    必须覆盖**每一个候选**，否则 mock 会把“我的近似没有答案”误记成生产里的硬阻塞。
    """
    nodes: dict[str, dict] = {}
    stack: list[tuple[str, int]] = []
    for block in document.blocks:
        if block.type == "title":
            level = int(block.level or 2)
            level = level if 1 <= level <= 6 else 2
            while stack and stack[-1][1] >= level:
                stack.pop()
            nodes[block.block_id] = {"role": f"h{level}", "parent_id": stack[-1][0] if stack else None,
                                     "confidence": 0.95, "reason": "doc"}
            stack.append((block.block_id, level))
        elif block.type == "footnote":
            nodes[block.block_id] = {"role": "footnote", "parent_id": None, "confidence": 0.95, "reason": "doc"}
        elif block.type == "caption":
            nodes[block.block_id] = {"role": "caption", "parent_id": None, "confidence": 0.95, "reason": "doc"}
        else:
            nodes[block.block_id] = {"role": "body", "parent_id": None, "confidence": 0.95, "reason": "doc"}
    return {"profile": {}, "nodes": nodes, "issues": [], "candidates": len(nodes), "planned": len(nodes)}


class MockProvider:
    """按真实请求体生成合法响应，并统计输入字符与“必须逐条作答”的条数。"""

    def __init__(self, document: Document):
        self.plan = derive_plan(document)
        self.requests: list[dict] = []

    def complete(self, *, system: str, user: str):
        payload = json.loads(user)
        if "toc_pages" in payload:
            rows = payload.get("local_toc_draft") or []
            content = json.dumps({"toc": rows}, ensure_ascii=False)
            purpose = "toc-extract"
        elif payload.get("stage") == "outline-plan-v2":
            wanted = {item["block_id"] for item in payload["candidates"]}
            rows = [{"block_id": bid, **{k: v for k, v in node.items() if k != "reason"}}
                    for bid, node in self.plan["nodes"].items() if bid in wanted]
            content = json.dumps({"profile": {}, "outline": rows}, ensure_ascii=False)
            purpose = "outline"
        else:
            rows = []
            for row in payload["title_candidates"]:
                node = self.plan["nodes"].get(row["block_id"]) or {
                    "role": f"h{row['level']}" if row["type"] == "title" else "body",
                    "parent_id": None, "confidence": 0.9}
                rows.append({"block_id": row["block_id"], "role": node["role"],
                             "parent_id": node["parent_id"], "confidence": 0.95})
            content = json.dumps({"decisions": rows}, ensure_ascii=False)
            purpose = "qa" if payload.get("review_purpose") == "qa" else "review"
        self.requests.append({"purpose": purpose, "chars": len(user) + len(system),
                              "rows": len(rows)})
        return ProviderResponse(content=content, provider="mock", model="mock",
                                input_tokens=0, output_tokens=0)


def run_one(label: str, pid: str, mode: str, *, extract_toc: bool = False,
            toc_extraction: str = "auto") -> dict:
    store = ProjectStore(ROOT / "library")
    document = store.load_document(pid)
    baseline = store.load_structure_baseline(pid)
    toc = baseline.get("toc") if isinstance(baseline, dict) else []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "source.html"
        source.write_text("fixture")
        temp_store = ProjectStore(root / "library")
        project = temp_store.create_project(source)["project_id"]
        temp_store.save_document(Document.from_dict({**document.to_dict(), "project_id": project}))
        provider = MockProvider(document)
        job = temp_store.create_job(project, "structure_ai", {}, {})
        result = run_structure_job(temp_store, project, job, {
            "proofread": True, "extract_toc": extract_toc, "proofread_mode": mode,
            "toc_extraction": toc_extraction, "toc": toc,
        }, provider)
    purposes = sorted({item["purpose"] for item in provider.requests})
    return {
        "label": label, "mode": mode, "extract_toc": extract_toc, "toc_entries": len(toc),
        "candidates": result["review_coverage"]["candidates"],
        "requests": len(provider.requests),
        "input_chars": sum(item["chars"] for item in provider.requests),
        "rows_to_answer": sum(item["rows"] for item in provider.requests),
        "by_purpose": {name: sum(1 for item in provider.requests if item["purpose"] == name)
                       for name in purposes},
        "toc_extraction": result.get("toc_extraction"),
        "suggestions": len(result["suggestions"]),
        "qa": (result.get("review_selection") or {}).get("qa"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="结构复核模式离线基准（只读、无模型调用）")
    parser.add_argument("--out", default="", help="可选：把 JSON 报告写到该路径")
    parser.add_argument("--modes", default="full,sample,off")
    args = parser.parse_args()
    modes = [value.strip() for value in args.modes.split(",") if value.strip()]

    report = []
    scenarios = [("full", True, "model"),           # 改造前：模型核对目录 + 全量双遍
                 ("sample", True, "auto"),        # 改造后：本地目录直接采信 + 定案+抽查
                 ("sample", False, "auto"),
                 ("off", False, "auto"),
                 ("sample", True, "model")]
    print(f"{'document':16s} {'mode':7s} {'etoc':5s} {'tocext':7s} {'cand':>5s} {'req':>4s} "
          f"{'in_chars':>10s} {'rows':>6s}  by_purpose")
    for label, pattern, pid in PROJECTS:
        if not glob.glob(str(ROOT / pattern)):
            continue
        for mode, extract_toc, toc_extraction in scenarios:
            if mode not in modes:
                continue
            row = run_one(label, pid, mode, extract_toc=extract_toc, toc_extraction=toc_extraction)
            report.append(row)
            print(f"{row['label']:16s} {row['mode']:7s} {str(row['extract_toc'])[:5]:5s} "
                  f"{str(row['toc_extraction']):7s} {row['candidates']:5d} {row['requests']:4d} "
                  f"{row['input_chars']:10d} {row['rows_to_answer']:6d}  {row['by_purpose']}")
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
