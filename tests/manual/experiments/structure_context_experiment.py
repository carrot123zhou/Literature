"""离线实验：AI 结构校对“压缩上下文 / 收紧复核范围”的输入开销与风险分析。

只读真实库与历史作业审计，使用临时/只读文献快照，不修改真实库/raw，不调用任何模型，
不安装、不启动、不构建、不提交。实验结论只代表离线输入开销与历史响应回放；**不能**
据此声称新提示词/新策略的准确率。

用法：
    .venv/bin/python tests/manual/experiments/structure_context_experiment.py
    .venv/bin/python tests/manual/experiments/structure_context_experiment.py --out /tmp/lit-exp
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from literature.models import Document  # noqa: E402
from literature.structure import _batch_payload, structure_model_payload  # noqa: E402
from literature.structure_reasoning import (  # noqa: E402
    OUTLINE_PROMPT, ROLES, select_review_targets, validate_hierarchy,
)
from literature import structure_signals as signals  # noqa: E402

LEAD_TMP = Path("/var/folders/2p/40xwg96s5634qgkjmhd4w7r40000gn/T/literature-structure-lead-8s_e7tiv")
PROJECTS = {
    "white_lotus": {
        "snapshot": LEAD_TMP,
        "db_glob": "library/The White Lotus Teachings*/literature.sqlite",
        # 历史全量双遍作业：348 候选，6 outline + 9 proofread，与当前快照候选 ID 完全一致。
        "job_id": "17e682665d914a0d82948c3594c18141",
    },
    "journal": {
        "snapshot": LEAD_TMP / "journal",
        "db_glob": "library/The_Metropolitan_Museum_Journal*/literature.sqlite",
        "job_id": "",
    },
    "authentic": {
        "snapshot": LEAD_TMP / "authentic",
        "db_glob": "library/On Being Authentic*/literature.sqlite",
        "job_id": "",
    },
}


# ---------------------------------------------------------------- 数据读取


def load_snapshot(path: Path) -> tuple[Document, list[dict[str, Any]], dict[str, Any]]:
    document = Document.from_dict(json.loads((path / "document.json").read_text(encoding="utf-8")))
    toc = json.loads((path / "toc.json").read_text(encoding="utf-8"))
    payload = structure_model_payload(document, toc, excluded_pages={6, 7})
    return document, toc, payload


def find_jobs(db_path: str, *, completed_only: bool = True) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    clause = "AND status='completed'" if completed_only else ""
    rows = con.execute(
        f"SELECT job_id, status, created_at, result_json FROM jobs WHERE kind='structure_ai' {clause} ORDER BY created_at"
    ).fetchall()
    con.close()
    output = []
    for row in rows:
        try:
            result = json.loads(row["result_json"])
        except ValueError:
            continue
        output.append({"job_id": row["job_id"], "status": row["status"], "created_at": row["created_at"], "result": result})
    return output


def requests_of(job: dict[str, Any], scope: str) -> list[dict[str, Any]]:
    return [r for r in job["result"].get("audit", {}).get("requests", []) if r.get("scope") == scope]


def load_job(db_path: str, job_id: str) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT job_id, status, created_at, result_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    con.close()
    return {"job_id": job_id, "status": row["status"], "created_at": row["created_at"], "result": json.loads(row["result_json"])}


def human_adoption(db_path: str, job_id: str) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT block_id, operation, after_json, decision FROM structure_suggestions WHERE job_id=?", (job_id,)
    ).fetchall()
    con.close()
    applied = {}
    for row in rows:
        try:
            after = json.loads(row["after_json"])
        except (TypeError, ValueError):
            after = None
        applied[row["block_id"]] = {"operation": row["operation"], "after": after, "decision": row["decision"]}
    return applied


# ---------------------------------------------------------------- 历史响应重建


def _valid_node(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    bid, role, parent = row.get("block_id"), row.get("role"), row.get("parent_id")
    confidence = row.get("confidence")
    if not isinstance(bid, str) or role not in ROLES or "parent_id" not in row:
        return None
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence):
        return None
    return {"block_id": bid, "role": role, "parent_id": parent, "confidence": float(confidence),
            "reason": str(row.get("reason", ""))[:350]}


def reconstruct_plan(outline_requests: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    issues, unparsed = 0, 0
    for request in outline_requests:
        if request.get("status") not in {"responded", "reused"}:
            continue
        try:
            value = json.loads(request.get("response_content") or "{}")
        except ValueError:
            unparsed += 1
            continue
        rows = value.get("outline")
        if not isinstance(rows, list):
            unparsed += 1
            continue
        for row in rows:
            node = _valid_node(row)
            if node is None:
                issues += 1
                continue
            nodes[node["block_id"]] = node
    return {"nodes": nodes, "issues": issues, "unparsed": unparsed, "candidates": len(nodes)}


def reconstruct_decisions(proof_requests: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str], int]:
    decisions: dict[str, dict[str, Any]] = {}
    conflicts: set[str] = set()
    unparsed = 0
    for request in proof_requests:
        if request.get("status") not in {"responded", "reused"}:
            continue
        try:
            value = json.loads(request.get("response_content") or "{}")
        except ValueError:
            unparsed += 1
            continue
        rows = value.get("decisions")
        if not isinstance(rows, list):
            unparsed += 1
            continue
        for row in rows:
            node = _valid_node(row)
            if node is None:
                continue
            if node["block_id"] in decisions and decisions[node["block_id"]]["role"] != node["role"]:
                conflicts.add(node["block_id"])
            decisions[node["block_id"]] = node
    return decisions, conflicts, unparsed


# ---------------------------------------------------------------- 上下文压缩（V1）


def _nearby_toc(toc: list[dict[str, Any]], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保留与本窗口候选/相邻标题相关的目录条目及相邻 ±2 条，而不是整本目录。"""
    indexes: set[int] = set()
    for item in items:
        text = str(item.get("text") or "")
        if not text:
            continue
        for index, entry in enumerate(toc):
            if signals.heading_similarity(text, str(entry.get("title", ""))) >= 0.6:
                indexes.update({index - 2, index - 1, index, index + 1, index + 2})
    return [entry for index, entry in enumerate(toc) if index in indexes]


def compact_outline_request(request: dict[str, Any]) -> dict[str, Any]:
    """压缩规划窗口上下文：全局 overview/完整目录只在第 1 窗；后续窗口只带近邻目录；
    上一轮规划只保留前 6（根/祖先）+ 最近 8；相邻上下文保留 4+4。候选本身不动。"""
    output = dict(request)
    window = int(request.get("window") or 1)
    if window > 1:
        output.pop("overview", None)
        output["toc"] = _nearby_toc(request.get("toc") or [], (request.get("candidates") or []) + (request.get("context") or []))
    previous = request.get("previous_outline") or []
    if len(previous) > 14:
        output["previous_outline"] = previous[:6] + previous[-8:]
    context = request.get("context") or []
    if len(context) > 8:
        half = len(context) // 2
        output["context"] = context[:4] + context[half: half + 4]
    return output


def compact_proofread_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """压缩逐批复核上下文：去掉每个候选重复的本地中文 reasons 与字体 layout；
    heading_context 文本截到 120 字符。候选 id/type/level/bbox/父子/目录锚点全部保留。"""
    output = dict(batch)
    compact_candidates = []
    for candidate in batch.get("title_candidates", []):
        item = dict(candidate)
        local = dict(item.get("local") or {})
        local.pop("reasons", None)
        item["local"] = local
        item.pop("layout", None)
        compact_candidates.append(item)
    output["title_candidates"] = compact_candidates
    context = []
    for item in batch.get("heading_context", []):
        entry = dict(item)
        if isinstance(entry.get("text"), str):
            entry["text"] = entry["text"][:120]
            entry["text_truncated"] = True
        context.append(entry)
    output["heading_context"] = context
    return output


# ---------------------------------------------------------------- 请求构造与计量


def measure(payload: dict[str, Any], plan: dict[str, Any] | None, selection: dict[str, Any] | None,
            compact: bool, *, outline_only: bool = False) -> dict[str, Any]:
    from literature.structure_reasoning import outline_inputs

    requests = []
    outline = outline_inputs(payload)
    for request in outline:
        request = dict(request)
        request["stage"] = "outline-plan-v2"
        if compact:
            request = compact_outline_request(request)
        requests.append(("structure_outline", request, OUTLINE_PROMPT))

    if not outline_only:
        if plan is not None:
            model_payload = dict(payload)
            model_payload["hierarchy_plan"] = {"profile": plan.get("profile", {}), "nodes": plan["nodes"]}
        else:
            model_payload = dict(payload)
        model_payload["visual_evidence"] = []
        model_payload["decision_protocol"] = "hierarchy-v2"
        review_ids = list((selection or {}).get("review_ids", []))
        review_set = set(review_ids)
        review_candidates = [c for c in payload["title_candidates"] if c["block_id"] in review_set]
        for offset in range(0, len(review_candidates), 40):
            chunk = review_candidates[offset:offset + 40]
            batch = _batch_payload(model_payload, chunk, 0)
            if compact:
                batch = compact_proofread_batch(batch)
            requests.append(("structure_proofread", batch, ""))

    total_chars = total_bytes = 0
    per_stage: dict[str, dict[str, int]] = {}
    for scope, body, system in requests:
        user = json.dumps(body, ensure_ascii=False)
        chars = len(system) + len(user)
        byte_count = len(system.encode("utf-8")) + len(user.encode("utf-8"))
        total_chars += chars
        total_bytes += byte_count
        stage = per_stage.setdefault(scope, {"requests": 0, "chars": 0, "bytes": 0})
        stage["requests"] += 1
        stage["chars"] += chars
        stage["bytes"] += byte_count
    return {"requests": len(requests), "chars": total_chars, "bytes": total_bytes, "per_stage": per_stage}


# ---------------------------------------------------------------- 分歧/风险分析


def _level(role: str) -> int | None:
    if isinstance(role, str) and len(role) == 2 and role[0] == "h" and role[1] in "123456":
        return int(role[1])
    return None


def analyze_disagreement(payload: dict[str, Any], plan: dict[str, Any], proof: dict[str, dict[str, Any]],
                         baseline_review: set[str], variant_review: set[str]) -> dict[str, Any]:
    nodes = plan["nodes"]
    matched = {item["block_id"] for item in payload.get("matched_titles") or []}
    omitted = baseline_review - variant_review
    stats = {
        "candidates_with_plan": 0, "candidates_with_proof": 0, "both": 0,
        "role_changed": 0, "level_changed": 0, "parent_changed": 0,
        "body_to_heading": 0, "heading_to_body": 0, "unlisted_heading": 0,
        "omitted": len(omitted), "omitted_disagree": 0, "omitted_body_to_heading": 0,
        "omitted_heading_to_body": 0, "omitted_unlisted_heading": 0, "omitted_level_changed": 0,
        "omitted_parent_changed": 0,
    }
    notable: list[dict[str, Any]] = []
    for candidate in payload["title_candidates"]:
        bid = candidate["block_id"]
        node = nodes.get(bid)
        decision = proof.get(bid)
        if node:
            stats["candidates_with_plan"] += 1
        if decision:
            stats["candidates_with_proof"] += 1
        if not node or not decision:
            continue
        stats["both"] += 1
        plan_role, proof_role = node["role"], decision["role"]
        plan_level, proof_level = _level(plan_role), _level(proof_role)
        changed = False
        if plan_role != proof_role:
            stats["role_changed"] += 1
            changed = True
        if plan_level is not None and proof_level is not None and plan_level != proof_level:
            stats["level_changed"] += 1
            changed = True
        if node.get("parent_id") != decision.get("parent_id"):
            stats["parent_changed"] += 1
            changed = True
        if plan_level is None and proof_level is not None:
            stats["body_to_heading"] += 1
        if plan_level is not None and proof_level is None:
            stats["heading_to_body"] += 1
        if proof_level is not None and bid not in matched:
            stats["unlisted_heading"] += 1
        if bid in omitted:
            if changed:
                stats["omitted_disagree"] += 1
            if plan_level is None and proof_level is not None:
                stats["omitted_body_to_heading"] += 1
            if plan_level is not None and proof_level is None:
                stats["omitted_heading_to_body"] += 1
            if proof_level is not None and bid not in matched:
                stats["omitted_unlisted_heading"] += 1
            if plan_level is not None and proof_level is not None and plan_level != proof_level:
                stats["omitted_level_changed"] += 1
            if node.get("parent_id") != decision.get("parent_id"):
                stats["omitted_parent_changed"] += 1
            if changed and len(notable) < 30:
                notable.append({
                    "block_id": bid, "type": candidate.get("type"), "level": candidate.get("level"),
                    "text": str(candidate.get("text") or "")[:90],
                    "plan_role": plan_role, "plan_parent": node.get("parent_id"),
                    "proof_role": proof_role, "proof_parent": decision.get("parent_id"),
                    "unlisted": proof_level is not None and bid not in matched,
                })
    stats["notable_omitted"] = notable
    composition = {"by_type": {}, "by_plan_role": {}, "local_heading_shape": 0,
                   "not_prose_like": 0, "heading_score_ge_0_3": 0, "plan_heading": 0}
    from collections import Counter
    from literature.structure_reasoning import _local_heading_shape, _prose_like
    type_counter: Counter = Counter()
    role_counter: Counter = Counter()
    by_id = {c["block_id"]: c for c in payload["title_candidates"]}
    for bid in omitted:
        candidate = by_id.get(bid)
        node = nodes.get(bid)
        if not candidate:
            continue
        type_counter[candidate.get("type")] += 1
        role_counter[node["role"] if node else "none"] += 1
        if _local_heading_shape(candidate):
            composition["local_heading_shape"] += 1
        if not _prose_like(str(candidate.get("text") or "")):
            composition["not_prose_like"] += 1
        if float((candidate.get("local") or {}).get("heading_score") or 0) >= 0.3:
            composition["heading_score_ge_0_3"] += 1
        if node and _level(node["role"]) is not None:
            composition["plan_heading"] += 1
    composition["by_type"] = dict(type_counter)
    composition["by_plan_role"] = dict(role_counter)
    stats["omitted_composition"] = composition
    return stats


def tree_issues(candidates: list[dict[str, Any]], decisions: dict[str, dict[str, Any]], plan: dict[str, Any]) -> dict[str, Any]:
    provisional = {bid: {"block_id": bid, "role": node["role"], "parent_id": node.get("parent_id")}
                   for bid, node in decisions.items()}
    result = validate_hierarchy(candidates, provisional, plan)
    return {"checked": result["checked"], "issues": len(result["issues"]),
            "issue_ids": [item["block_id"] for item in result["issues"][:20]]}


def compare_suggestion_sets(document: Document, payload: dict[str, Any], plan: dict[str, Any],
                            proof: dict[str, dict[str, Any]], baseline_review: set[str],
                            v2_review: set[str]) -> dict[str, Any]:
    """在相同的最终决策基础上，比较 B0 与 V2 会产出的建议集合。

    复用项用第一遍规划结论，复核项用历史第二遍结论（缺失时回退规划）。注意：当前快照
    可能已包含人工采纳后的状态，因此这里的建议集合是“相对当前文档”的近似，不是作业当
    时逐条 gold。
    """
    from literature.structure import _decision_suggestions

    def final(review_ids: set[str]) -> dict[str, dict[str, Any]]:
        output = {bid: dict(node) for bid, node in plan["nodes"].items()}
        for bid in review_ids:
            if bid in proof:
                output[bid] = dict(proof[bid])
        return output

    def keys(decisions: dict[str, dict[str, Any]]) -> set[Any]:
        return {(s["block_id"], s["operation"], json.dumps(s["after"], ensure_ascii=False, sort_keys=True))
                for s in _decision_suggestions(document, decisions.values())}

    baseline_keys = keys(final(baseline_review))
    v2_keys = keys(final(v2_review))
    return {"baseline_suggestions": len(baseline_keys), "variant2_suggestions": len(v2_keys),
            "missing_in_variant2": sorted(baseline_keys - v2_keys), "extra_in_variant2": sorted(v2_keys - baseline_keys)}


# ---------------------------------------------------------------- 主流程


def run_project(name: str, config: dict[str, Any]) -> dict[str, Any]:
    document, toc, payload = load_snapshot(config["snapshot"])
    db_path = glob.glob(str(ROOT / config["db_glob"]))[0]
    report: dict[str, Any] = {
        "project": name, "snapshot": str(config["snapshot"]),
        "candidates": len(payload["title_candidates"]),
        "toc_entries": len(payload["authoritative_toc"]),
        "matched": len(payload.get("matched_titles") or []),
        "unlisted_titles": len(payload.get("unlisted_titles") or []),
        "unresolved_toc": len(payload.get("unresolved_toc") or []),
    }
    job_id = config.get("job_id") or ""
    if not job_id:
        completed = find_jobs(db_path)
        report["available_jobs"] = [
            {"job_id": j["job_id"], "created_at": j["created_at"],
             "outline": len(requests_of(j, "structure_outline")), "proofread": len(requests_of(j, "structure_proofread"))}
            for j in completed
        ]
        report["replay_status"] = "no matching historical job (candidate logic version differs); input-overhead only"
        report["outline_overhead"] = {
            "baseline": measure(payload, None, None, compact=False, outline_only=True),
            "variant1_compact_context": measure(payload, None, None, compact=True, outline_only=True),
        }
        base = report["outline_overhead"]["baseline"]
        compact_value = report["outline_overhead"]["variant1_compact_context"]
        report["outline_overhead"]["variant1_saving"] = {
            "chars_pct": round(100 * (base["chars"] - compact_value["chars"]) / max(1, base["chars"]), 2),
            "bytes_pct": round(100 * (base["bytes"] - compact_value["bytes"]) / max(1, base["bytes"]), 2),
        }
        report["note"] = "当前快照候选集与历史作业候选集不一致（候选召回逻辑版本不同），故不做历史两遍回放；仅做规划阶段的输入开销分析。"
        return report

    job = load_job(db_path, job_id)
    outline_requests = requests_of(job, "structure_outline")
    proof_requests = requests_of(job, "structure_proofread")
    # 候选 ID 一致性：从历史请求里取出候选集合，与当前快照重建结果比对。
    historical_ids: set[str] = set()
    for request in outline_requests:
        historical_ids |= {row["block_id"] for row in json.loads(request["user"]).get("candidates", [])}
    historical_proof_ids: set[str] = set()
    for request in proof_requests:
        historical_proof_ids |= {row["block_id"] for row in json.loads(request["user"]).get("title_candidates", [])}
    current_ids = {c["block_id"] for c in payload["title_candidates"]}
    report["candidate_identity"] = {
        "current": len(current_ids), "historical_outline": len(historical_ids), "historical_proofread": len(historical_proof_ids),
        "outline_match": current_ids == historical_ids, "proofread_match": current_ids == historical_proof_ids,
        "symmetric_diff": len(current_ids ^ historical_proof_ids),
    }
    report["historical_job"] = {"job_id": job_id, "created_at": job["created_at"],
                                "outline_requests": len(outline_requests), "proofread_requests": len(proof_requests),
                                "status": job["status"]}

    plan = reconstruct_plan(outline_requests)
    proof, proof_conflicts, proof_unparsed = reconstruct_decisions(proof_requests)
    report["reconstruction"] = {
        "plan_nodes": len(plan["nodes"]), "plan_invalid_rows": plan["issues"], "plan_unparsed": plan["unparsed"],
        "proof_decisions": len(proof), "proof_unparsed": proof_unparsed, "proof_conflicts": sorted(proof_conflicts),
    }

    reconciliation = {"matched": payload.get("matched_titles") or [], "unlisted_titles": payload.get("unlisted_titles") or []}
    baseline_reused, baseline_review, baseline_reasons, baseline_stats = select_review_targets(
        payload["title_candidates"], plan, reconciliation)
    v2_reused, v2_review, v2_reasons, v2_stats = select_review_targets(
        payload["title_candidates"], plan, reconciliation, strict_local=False)
    report["selection"] = {
        "baseline": {"reused": len(baseline_reused), "review": len(baseline_review), "batches": math.ceil(len(baseline_review) / 40), "stats": baseline_stats},
        "variant2_anomaly_only": {"reused": len(v2_reused), "review": len(v2_review), "batches": math.ceil(len(v2_review) / 40), "stats": v2_stats},
    }

    baseline_selection = {"review_ids": baseline_review}
    v2_selection = {"review_ids": v2_review}
    full_selection = {"review_ids": [c["block_id"] for c in payload["title_candidates"]]}
    report["overhead"] = {
        "full_review_reference": measure(payload, plan, full_selection, compact=False),
        "baseline": measure(payload, plan, baseline_selection, compact=False),
        "variant1_compact_context": measure(payload, plan, baseline_selection, compact=True),
        "variant2_compact_anomaly_only": measure(payload, plan, v2_selection, compact=True),
    }
    # 各变体相对 baseline 的节省。
    base = report["overhead"]["baseline"]
    for key in ("full_review_reference", "variant1_compact_context", "variant2_compact_anomaly_only"):
        value = report["overhead"][key]
        report["overhead"][key + "_vs_baseline"] = {
            "requests": base["requests"] - value["requests"],
            "chars_pct": round(100 * (base["chars"] - value["chars"]) / max(1, base["chars"]), 2),
            "bytes_pct": round(100 * (base["bytes"] - value["bytes"]) / max(1, base["bytes"]), 2),
        }

    report["disagreement"] = analyze_disagreement(
        payload, plan, proof, set(baseline_review), set(v2_review))
    report["tree"] = {
        "plan": tree_issues(payload["title_candidates"], plan["nodes"], plan),
        "proof": tree_issues(payload["title_candidates"], proof, plan),
    }
    report["suggestion_sets"] = compare_suggestion_sets(
        document, payload, plan, proof, set(baseline_review), set(v2_review))
    adoption = human_adoption(db_path, job_id)
    omitted = set(baseline_review) - set(v2_review)
    report["human_adoption"] = {
        "suggestions": len(adoption),
        "adopted_in_omitted_set": sorted(bid for bid in adoption if bid in omitted),
        "adopted_disagreements": sorted(bid for bid in adoption if bid in proof and bid in plan["nodes"]
                                        and (proof[bid]["role"] != plan["nodes"][bid]["role"]
                                             or proof[bid].get("parent_id") != plan["nodes"][bid].get("parent_id"))),
    }
    report["historical_usage_reference_only"] = historical_usage(job, outline_requests, proof_requests)
    return report


def historical_usage(job: dict[str, Any], outline_requests: list[dict[str, Any]],
                     proof_requests: list[dict[str, Any]]) -> dict[str, Any]:
    """历史真实 usage，仅作参考；**不是**新请求的 token 计量。"""
    def summarize(requests: list[dict[str, Any]]) -> dict[str, int]:
        input_tokens = output_tokens = reasoning_tokens = 0
        for request in requests:
            metadata = request.get("response_metadata") or {}
            input_tokens += int(metadata.get("input_tokens") or 0)
            output_tokens += int(metadata.get("output_tokens") or 0)
            reasoning_tokens += int(metadata.get("reasoning_tokens") or 0)
        return {"requests": len(requests), "input_tokens": input_tokens, "output_tokens": output_tokens,
                "reasoning_tokens": reasoning_tokens}
    return {"outline": summarize(outline_requests), "proofread": summarize(proof_requests),
            "note": "历史真实 usage，仅供参考；新请求 token 未用 tokenizer，报告中为字符/字节与估算。"}


def main() -> int:
    parser = argparse.ArgumentParser(description="结构校对上下文/复核范围离线实验（只读）")
    parser.add_argument("--out", default=str(ROOT / "docs" / "verification"))
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = {"experiment": "structure-context-and-review-scope", "date": "2026-10-14",
              "mode": "offline read-only replay + input-overhead analysis (no model calls)", "projects": {}}
    for name, config in PROJECTS.items():
        report["projects"][name] = run_project(name, config)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (out_dir / "structure-context-experiment.json").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
