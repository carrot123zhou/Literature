"""离线自适应规划回放：框架/区域/请求开销与候选覆盖，无任何模型调用。

- White Lotus：用历史第一遍规划响应回放（候选 ID 与当前重建一致），验证自适应分支
  把每个候选恰好规划一次、请求数与输入字符，并对比全局串行规划。
- Journal / Authentic：历史作业候选逻辑版本不同，只做框架/区域统计与“当前文档层级”
  机械覆盖（非历史回放），并明确标注不是模型质量证明。

用法：
    .venv/bin/python tests/manual/experiments/structure_adaptive_replay.py [--out DIR]
"""
from __future__ import annotations

import argparse
import glob
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from literature.models import Document  # noqa: E402
from literature.providers import ProviderResponse  # noqa: E402
from literature.structure import _batch_payload, structure_model_payload  # noqa: E402
from literature.structure_adaptive import build_framework, partition_regions, run_adaptive_planning  # noqa: E402
from literature.structure_reasoning import OUTLINE_PROMPT, outline_inputs, select_review_targets  # noqa: E402
from tests.manual.experiments.structure_context_experiment import PROJECTS, find_jobs, requests_of  # noqa: E402


class ReplayProvider:
    """按候选 ID 返回历史/机械规划行；统计请求数与输入字符，不调用模型。"""

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.requests: list[dict[str, Any]] = []
        self.chars = 0

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        request = json.loads(user)
        self.requests.append(request)
        self.chars += len(system) + len(user)
        output = []
        for candidate in request.get("candidates", []):
            node = self.rows.get(candidate["block_id"])
            if not node:
                continue
            output.append({key: node[key] for key in ("block_id", "role", "parent_id", "confidence") if key in node})
        return ProviderResponse(content=json.dumps({"outline": output}), provider="replay", model="historical-or-mechanical")


def historical_plan_rows(db_path: str, job_id: str) -> dict[str, dict[str, Any]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    result = json.loads(con.execute("SELECT result_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()["result_json"])
    con.close()
    rows: dict[str, dict[str, Any]] = {}
    for request in requests_of({"result": result}, "structure_outline"):
        try:
            value = json.loads(request.get("response_content") or "{}")
        except ValueError:
            continue
        for row in value.get("outline", []):
            if isinstance(row, dict) and isinstance(row.get("block_id"), str):
                rows[row["block_id"]] = row
    return rows


def mechanical_rows(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """无模型机械规划：现有标题按当前层级，其它按正文。仅验证覆盖/区域机制。"""
    rows = {}
    for item in candidates:
        if item["type"] == "title":
            rows[item["block_id"]] = {"block_id": item["block_id"], "role": f"h{max(1, min(6, int(item['level'] or 1)))}",
                                      "parent_id": None, "confidence": 0.9}
        else:
            rows[item["block_id"]] = {"block_id": item["block_id"], "role": "body", "parent_id": None, "confidence": 0.9}
    return rows


def global_overhead(payload: dict[str, Any]) -> dict[str, int]:
    requests = outline_inputs(payload)
    chars = 0
    for request in requests:
        request = dict(request)
        request["stage"] = "outline-plan-v2"
        chars += len(OUTLINE_PROMPT) + len(json.dumps(request, ensure_ascii=False))
    return {"requests": len(requests), "chars": chars}


def second_pass_overhead(payload: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """给定规划节点，估算第二遍复核（strict_local 选择）的批数与 user 字符。

    第二遍未改动：规划阶段变快不等于端到端变快；这里把两阶段分开报告。
    """
    reconciliation = {"matched": payload.get("matched_titles") or [],
                      "unlisted_titles": payload.get("unlisted_titles") or []}
    reused, review, _reasons, _stats = select_review_targets(payload["title_candidates"], {"nodes": nodes}, reconciliation)
    model_payload = {**payload, "hierarchy_plan": {"profile": {}, "nodes": nodes},
                     "visual_evidence": [], "decision_protocol": "hierarchy-v2"}
    review_candidates = [item for item in payload["title_candidates"] if item["block_id"] in set(review)]
    chars = 0
    batches = 0
    for offset in range(0, len(review_candidates), 40):
        batch = _batch_payload(model_payload, review_candidates[offset:offset + 40], 0)
        chars += len(json.dumps(batch, ensure_ascii=False))
        batches += 1
    return {"reused": len(reused), "review": len(review), "batches": batches, "user_chars": chars}


def run_case(name: str, snapshot: Path, db_glob: str, job_id: str) -> dict[str, Any]:
    document = Document.from_dict(json.loads((snapshot / "document.json").read_text(encoding="utf-8")))
    toc = json.loads((snapshot / "toc.json").read_text(encoding="utf-8"))
    payload = structure_model_payload(document, toc, excluded_pages={6, 7})
    candidates = payload["title_candidates"]
    candidate_ids = {item["block_id"] for item in candidates}
    report: dict[str, Any] = {
        "project": name, "candidates": len(candidates),
        "toc_entries": len(payload["authoritative_toc"]),
        "matched": len(payload.get("matched_titles") or []),
        "unlisted_titles": len(payload.get("unlisted_titles") or []),
    }
    framework = build_framework(payload)
    report["framework"] = framework.to_dict()
    regions = partition_regions(candidates, framework)
    report["regions"] = {
        "total": len(regions), "reliable": sum(1 for r in regions if r.reliable),
        "serial": sum(1 for r in regions if not r.reliable),
        "primary_total": sum(len(r.primary_ids) for r in regions),
        "context_total": sum(len(r.context_ids) for r in regions),
    }
    report["global_overhead"] = global_overhead(payload)

    def replay(rows: dict[str, dict[str, Any]], label: str) -> dict[str, Any]:
        provider = ReplayProvider(rows)
        plan, meta = run_adaptive_planning(payload, provider)
        nodes = set(plan["nodes"])
        global_nodes = {bid: rows[bid] for bid in candidate_ids if bid in rows}
        return {
            "label": label,
            "fallback": meta["fallback"], "fallback_reason": meta.get("fallback_reason", ""),
            "requests": meta.get("requests", len(provider.requests)),
            "chars": provider.chars,
            "main_requests": meta.get("main_requests"), "upgrade_requests": meta.get("upgrade_requests"),
            "secondary_requests": meta.get("secondary_requests"),
            "gate": meta.get("gate"),
            "estimated_chars": meta.get("estimated_chars"),
            "reliable_regions": meta.get("reliable_regions"), "ambiguous_regions": meta.get("ambiguous_regions"),
            "local_upgrades": meta.get("local_upgrades"), "second_round": meta.get("second_round"),
            "missing": meta.get("missing"),
            "candidate_coverage_unchanged": nodes == candidate_ids,
            "extra_nodes": sorted(nodes - candidate_ids),
            "overlap_only_rows_ignored": meta.get("overlap_ignored", 0),
            "planned": plan.get("planned"),
            "planning": {"requests": meta.get("requests") or len(provider.requests), "chars": provider.chars},
            "second_pass_global_plan": second_pass_overhead(payload, global_nodes),
            "second_pass_adaptive_plan": second_pass_overhead(payload, plan["nodes"]),
        }

    db_path = glob.glob(str(ROOT / db_glob))[0] if glob.glob(str(ROOT / db_glob)) else ""
    if job_id and db_path:
        history = historical_plan_rows(db_path, job_id)
        if set(history) & candidate_ids:
            report["replay_history"] = replay(history, "historical outline responses")
    report["replay_mechanical"] = replay(mechanical_rows(candidates), "current document levels (mechanical, not model quality)")
    # 端到端审计：规划与第二遍复核分开报告（第二遍未改，仍用 strict_local 选择）。
    for key in ("replay_history", "replay_mechanical"):
        if key not in report:
            continue
        value = report[key]
        global_plan = report["global_overhead"]
        gp_second = value["second_pass_global_plan"]
        ap_second = value["second_pass_adaptive_plan"]
        value["end_to_end"] = {
            "global": {"plan_requests": global_plan["requests"], "plan_chars": global_plan["chars"],
                       "proofread_batches": gp_second["batches"], "proofread_user_chars": gp_second["user_chars"],
                       "review": gp_second["review"],
                       "total_requests": global_plan["requests"] + gp_second["batches"],
                       "total_chars": global_plan["chars"] + gp_second["user_chars"]},
            "adaptive": {"plan_requests": value["planning"]["requests"], "plan_chars": value["planning"]["chars"],
                         "proofread_batches": ap_second["batches"], "proofread_user_chars": ap_second["user_chars"],
                         "review": ap_second["review"],
                         "total_requests": value["planning"]["requests"] + ap_second["batches"],
                         "total_chars": value["planning"]["chars"] + ap_second["user_chars"]},
        }
    if not db_path:
        report["note"] = "真实库未找到该项目的 sqlite；仅机械覆盖。"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="自适应规划离线回放（只读，无模型调用）")
    parser.add_argument("--out", default=str(ROOT / "docs" / "verification"))
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {"experiment": "structure-adaptive-replay", "date": "2026-10-14",
              "mode": "offline read-only framework + region analysis and historical/mechanical replay; no model calls",
              "projects": {}}
    for name, config in PROJECTS.items():
        report["projects"][name] = run_case(name, config["snapshot"], config["db_glob"], config.get("job_id", ""))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (out_dir / "structure-adaptive-replay.json").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
