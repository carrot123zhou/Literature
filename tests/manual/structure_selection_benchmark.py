"""手动基准：AI 结构校对“本地候选构建 + 选择性复核边界”。

不属于 pytest 全量回归（不依赖真实库、不调用模型）。用法：

    .venv/bin/python tests/manual/structure_selection_benchmark.py <样本目录>

样本目录需包含：
    document.json          —— Document.from_dict 的规范文档
    toc.json               —— 已确认目录（list[{title, level}]）
    historical-plan.json   —— 可选，历史第一遍规划响应 {"nodes": {...}}；
                              缺失时只报告本地候选构建耗时，不做复用边界统计

参数可选：
    --excluded 6,7         排除页（默认无）
    --repeat N             重复本地构建次数，报告 cold/warm 耗时

输出 JSON：本地构建耗时（cold/warm）、候选数、完整 payload sha256、
（带历史规划时）复用/复核数量与第二批批数。报告时务必注明：这是历史响应回放与
本地候选构建，不是新的真实模型 A/B，也不能代表 AI 总时长。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from literature.models import Document  # noqa: E402
from literature.structure import structure_model_payload  # noqa: E402
from literature.structure_reasoning import select_review_targets  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="结构校对本地候选构建基准（只读）")
    parser.add_argument("sample", help="包含 document.json / toc.json 的样本目录")
    parser.add_argument("--excluded", default="", help="排除页，逗号分隔，例如 6,7")
    parser.add_argument("--repeat", type=int, default=2, help="本地构建重复次数（cold/warm）")
    args = parser.parse_args()

    root = Path(args.sample)
    document = Document.from_dict(json.loads((root / "document.json").read_text(encoding="utf-8")))
    toc = json.loads((root / "toc.json").read_text(encoding="utf-8"))
    excluded = {int(value) for value in args.excluded.split(",") if value.strip()} or None

    report: dict[str, object] = {"sample": str(root), "excluded_pages": sorted(excluded or [])}
    timings = []
    payload = None
    for _ in range(max(1, args.repeat)):
        started = time.perf_counter()
        payload = structure_model_payload(document, toc, excluded_pages=excluded)
        timings.append(time.perf_counter() - started)
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    report["local_seconds"] = [round(value, 4) for value in timings]
    report["candidates"] = len(payload["title_candidates"])
    report["payload_sha256"] = hashlib.sha256(blob.encode()).hexdigest()

    plan_path = root / "historical-plan.json"
    if plan_path.is_file():
        nodes = json.loads(plan_path.read_text(encoding="utf-8"))["nodes"]
        reused, review, reasons, stats = select_review_targets(
            payload["title_candidates"], {"nodes": nodes},
            {"matched": payload.get("matched_titles") or [], "unlisted_titles": payload.get("unlisted_titles") or []})
        report["selection"] = {
            "reused": len(reused),
            "review": len(review),
            "review_batches": (len(review) + 39) // 40,
            "stats": stats,
            "reason_counts": {key: list(reasons.values()).count(key) for key in set(reasons.values())},
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
