"""真实 A/B 实验：结构校对“压缩上下文 / 收紧复核范围”（会调用付费模型）。

按 `docs/verification/structure-context-experiment-2026-10-14.md` §9 的最小可审批方案执行：
单样本（White Lotus 只读快照）、单模型（结构角色绑定服务）、三臂、串行、无复用。

臂定义：
  A = baseline（当前生产：整体串行规划 + 选择性复核 strict_local=True）
  B = V1 仅压缩上下文（复核选择规则同 A）
  C = V2 压缩上下文 + 仅异常/高风险复核（strict_local=False）

硬闸门（本脚本是唯一强制点，因为该服务未填单价、budget_usd 不拦截）：
  --cap-requests  每臂请求硬上限（默认 60，发送前拦截）
  --cap-tokens    全部臂累计 token（input+output+reasoning）硬上限（默认 1,000,000）

安全边界：只读快照，不写任何项目库/raw；不打印密钥；每臂前清进程内结构缓存；
不传 reuse_job_id（runner 不走 run_structure_job，天然无跨作业复用）。

用法：
    .venv/bin/python tests/manual/experiments/structure_ab_real.py --arms A,B,C
    .venv/bin/python tests/manual/experiments/structure_ab_real.py --arms A --cap-requests 20
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from literature.models import Document  # noqa: E402
from literature import structure  # noqa: E402
from literature import structure_reasoning as reasoning  # noqa: E402
from literature.model_services import ModelServiceStore  # noqa: E402
from literature.providers import ProviderError, ProviderResponse  # noqa: E402

# White Lotus 只读快照（2026-10-14 Lead 提供；候选 ID 与历史作业 17e68266 完全一致）。
SNAPSHOT = Path("/var/folders/2p/40xwg96s5634qgkjmhd4w7r40000gn/T/literature-structure-lead-8s_e7tiv")
EXCLUDED_PAGES = {6, 7}  # 与离线实验一致：目录页不参与标题候选。
BATCH_SIZE = 40
COMPACT_ARMS = {"B", "C"}


class StubProvider:
    """--stub 模式：零费用验证 runner 机制与各臂输入字符，不产生任何真实调用。"""

    config = None

    def complete(self, *, system: str, user: str) -> Any:
        request = json.loads(user)
        if "candidates" in request and "title_candidates" not in request:
            rows = [{"block_id": item["block_id"],
                     "role": f"h{max(1, int(item.get('level') or 1))}" if item.get("type") == "title" else "body",
                     "parent_id": None, "confidence": 0.9, "reason": "stub"}
                    for item in request["candidates"]]
            content = {"outline": rows, "profile": {"document_kind": "stub"}}
        else:
            rows = [{"block_id": item["block_id"],
                     "role": f"h{max(1, int(item.get('level') or 1))}" if item.get("type") == "title" else "body",
                     "parent_id": None, "confidence": 0.9, "reason": "stub"}
                    for item in request.get("title_candidates", [])]
            content = {"decisions": rows, "suggestions": []}
        return ProviderResponse(content=json.dumps(content, ensure_ascii=False), provider="stub", model="stub",
                                input_tokens=(len(system) + len(user)) // 4, output_tokens=256)


class CapExceeded(RuntimeError):
    """硬上限触发：立即中止，不再发送任何请求。"""


class RecordingProvider:
    """包装真实 provider：逐请求记录 scope/字符/usage/耗时，并在发送前执行硬上限。"""

    def __init__(self, inner: Any, *, arm: str, cap_requests: int, cap_tokens_state: dict[str, int],
                 records: list[dict[str, Any]]) -> None:
        self.inner = inner
        self.config = getattr(inner, "config", None)
        self.arm = arm
        self.cap_requests = cap_requests
        self.cap_tokens_state = cap_tokens_state  # 跨臂共享的累计 token 计数
        self.records = records
        self.count = 0

    def complete(self, *, system: str, user: str) -> Any:
        if self.count >= self.cap_requests:
            raise CapExceeded(f"臂 {self.arm} 达到请求硬上限 {self.cap_requests}，中止（发送前拦截，不产生新费用）")
        if self.cap_tokens_state["tokens"] >= self.cap_tokens_state["cap"]:
            raise CapExceeded(f"累计 token 硬上限 {self.cap_tokens_state['cap']} 已达到，中止")
        self.count += 1
        started = time.monotonic()
        response = self.inner.complete(system=system, user=user)
        elapsed = round(time.monotonic() - started, 3)
        tokens = int(response.input_tokens or 0) + int(response.output_tokens or 0) + int(response.reasoning_tokens or 0)
        self.cap_tokens_state["tokens"] += tokens
        self.records.append({
            "arm": self.arm,
            "scope": "structure_outline" if system == reasoning.OUTLINE_PROMPT else "structure_proofread",
            "system_chars": len(system), "user_chars": len(user),
            "input_tokens": int(response.input_tokens or 0), "output_tokens": int(response.output_tokens or 0),
            "reasoning_tokens": int(response.reasoning_tokens or 0), "cached_tokens": int(response.cached_tokens or 0),
            "finish_reason": response.finish_reason, "elapsed_seconds": elapsed,
            "request_id": response.request_id,
            "cumulative_tokens": self.cap_tokens_state["tokens"],
        })
        print(f"  [{self.arm}] #{self.count} {self.records[-1]['scope']} "
              f"in={response.input_tokens} out={response.output_tokens} reasoning={response.reasoning_tokens} "
              f"{elapsed:.1f}s 累计token={self.cap_tokens_state['tokens']}")
        if self.cap_tokens_state["tokens"] >= self.cap_tokens_state["cap"]:
            raise CapExceeded(f"累计 token 硬上限 {self.cap_tokens_state['cap']} 已达到（本响应后），中止后续请求")
        return response


def load_payload() -> tuple[Document, dict[str, Any]]:
    document = Document.from_dict(json.loads((SNAPSHOT / "document.json").read_text(encoding="utf-8")))
    toc = json.loads((SNAPSHOT / "toc.json").read_text(encoding="utf-8"))
    payload = structure.structure_model_payload(document, toc, excluded_pages=EXCLUDED_PAGES)
    # 与生产 run_structure_job 对齐：目录页候选显式过滤（此处应为 no-op，断言防漂移）、
    # 无视觉证据、固定决策协议版本。
    before = len(payload["title_candidates"])
    payload["title_candidates"] = [item for item in payload["title_candidates"] if item["page_index"] not in EXCLUDED_PAGES]
    if len(payload["title_candidates"]) != before:
        print(f"注意：目录页过滤移除了 {before - len(payload['title_candidates'])} 个候选（生产路径同此行为）")
    payload["visual_evidence"] = []
    payload["decision_protocol"] = structure.STRUCTURE_PROMPT_VERSION
    return document, payload


def run_arm(name: str, document: Document, payload: dict[str, Any], provider_factory: Any,
            cap_requests: int, cap_tokens_state: dict[str, int]) -> dict[str, Any]:
    compact = name in COMPACT_ARMS
    records: list[dict[str, Any]] = []
    # 防御性清空进程内结构缓存（runner 不走 run_structure_job，正常不经过缓存）。
    structure._STRUCTURE_CACHE.clear()
    structure._STRUCTURE_CACHE_ORDER.clear()
    provider = RecordingProvider(provider_factory(), arm=name, cap_requests=cap_requests,
                                 cap_tokens_state=cap_tokens_state, records=records)
    started = time.monotonic()
    print(f"[臂 {name}] 规划开始（compact_context={compact}）")
    plan = reasoning.plan_outline(payload, provider, compact_context=compact)
    planning_elapsed = round(time.monotonic() - started, 1)
    planning_requests = provider.count
    print(f"[臂 {name}] 规划完成：{planning_requests} 请求 · {planning_elapsed}s · "
          f"planned={plan['planned']}/{plan['candidates']} · issues={len(plan['issues'])}")

    model_payload = dict(payload)
    model_payload["hierarchy_plan"] = plan
    reconciliation = {"matched": payload.get("matched_titles") or [],
                      "unlisted_titles": payload.get("unlisted_titles") or []}
    strict_local = name != "C"
    reused, review_ids, _reasons, stats = reasoning.select_review_targets(
        payload["title_candidates"], plan, reconciliation, strict_local=strict_local)
    review_set = set(review_ids)
    review_candidates = [item for item in payload["title_candidates"] if item["block_id"] in review_set]
    chunks = [review_candidates[offset:offset + BATCH_SIZE] for offset in range(0, len(review_candidates), BATCH_SIZE)]
    print(f"[臂 {name}] 选择性复核：reused={len(reused)} · review={len(review_candidates)} · {len(chunks)} 批（strict_local={strict_local}）")

    baseline = payload["authoritative_toc"]
    decisions: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    review_started = time.monotonic()
    for index, chunk in enumerate(chunks, start=1):
        batch_skipped: list[dict[str, Any]] = []
        batch_decisions: dict[str, dict[str, Any]] = {}
        structure._proofread_batch(document, baseline, provider, model_payload, chunk,
                                   batch_skipped, decisions=batch_decisions, compact_context=compact)
        decisions.update(batch_decisions)
        skipped.extend({**item, "batch": index} for item in batch_skipped)
        print(f"[臂 {name}] 复核批 {index}/{len(chunks)} 完成 · 决策 {len(batch_decisions)} · 跳过 {len(batch_skipped)}")
    review_elapsed = round(time.monotonic() - review_started, 1)

    hierarchy_review = reasoning.validate_hierarchy(payload["title_candidates"], decisions, plan)
    # 最终决策：复核答案优先，缺失回退第一遍规划（与生产合并语义一致）。
    final = {bid: dict(node) for bid, node in plan["nodes"].items()}
    final.update(decisions)
    suggestions = structure._decision_suggestions(document, final.values())

    return {
        "arm": name, "compact_context": compact, "strict_local": strict_local,
        "planning": {"requests": planning_requests, "elapsed_seconds": planning_elapsed,
                     "planned": plan["planned"], "candidates": plan["candidates"],
                     "issues": len(plan["issues"]), "profile": plan.get("profile", {})},
        "selection": {"reused": len(reused), "review": len(review_candidates),
                      "batches": len(chunks), "stats": stats},
        "review": {"requests": provider.count - planning_requests, "elapsed_seconds": review_elapsed,
                   "decisions": len(decisions), "skipped": skipped},
        "hierarchy_review": {"checked": hierarchy_review["checked"], "issues": len(hierarchy_review["issues"])},
        "suggestions": [{"block_id": s["block_id"], "operation": s["operation"], "after": s["after"]}
                        for s in suggestions],
        "final_decisions": final,
        "requests": records,
        "request_count": provider.count,
        "wall_seconds": round(time.monotonic() - started, 1),
    }


def _level(role: Any) -> int | None:
    if isinstance(role, str) and len(role) == 2 and role[0] == "h" and role[1] in "123456":
        return int(role[1])
    return None


def build_gold_review(payload: dict[str, Any], arms: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """人工 gold 核对清单：全部未收录标题 + 任意臂间分歧 + 角色/层级/父子变化。"""
    matched = {item["block_id"] for item in payload.get("matched_titles") or []}
    by_id = {item["block_id"]: item for item in payload["title_candidates"]}
    arm_names = sorted(arms)
    items: list[dict[str, Any]] = []
    for bid, candidate in by_id.items():
        per_arm = {}
        for name in arm_names:
            node = arms[name]["final_decisions"].get(bid)
            per_arm[name] = None if node is None else {
                "role": node["role"], "level": _level(node["role"]), "parent_id": node.get("parent_id")}
        values = [json.dumps(v, sort_keys=True) for v in per_arm.values()]
        unlisted_heading = any(v and v["level"] is not None and bid not in matched for v in per_arm.values())
        if len(set(values)) > 1 or unlisted_heading:
            items.append({
                "block_id": bid, "type": candidate.get("type"), "level": candidate.get("level"),
                "page_index": candidate.get("page_index"),
                "text": str(candidate.get("text") or "")[:120],
                "in_confirmed_toc": bid in matched,
                "per_arm": per_arm,
            })
    items.sort(key=lambda item: (not item["in_confirmed_toc"], item["page_index"] or 0, item["block_id"]))
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description="结构校对真实 A/B（会调用付费模型；有硬上限）")
    parser.add_argument("--arms", default="A,B,C")
    parser.add_argument("--cap-requests", type=int, default=60, help="每臂请求硬上限（发送前拦截）")
    parser.add_argument("--cap-tokens", type=int, default=1_000_000, help="全部臂累计 token 硬上限")
    parser.add_argument("--service", default="", help="模型服务 ID；默认用结构角色绑定服务")
    parser.add_argument("--stub", action="store_true", help="零费用演练：用假 provider 跑完整流程，不调真实模型")
    parser.add_argument("--out", default=str(ROOT / "docs" / "verification"))
    args = parser.parse_args()

    arms_requested = [item.strip().upper() for item in args.arms.split(",") if item.strip()]
    unknown = set(arms_requested) - {"A", "B", "C"}
    if unknown:
        raise SystemExit(f"未知臂：{sorted(unknown)}（本实验只支持 A/B/C；adaptive 臂见 adaptive 回放/后续扩展）")

    store = ModelServiceStore()
    if args.stub:
        service_id, service = "stub", {"name": "stub", "model": "stub", "base_url": "-"}
        provider_factory = StubProvider
        print("--stub 模式：零费用演练，不会调用任何真实模型")
    else:
        service_id = args.service or store.roles().get("structure", "")
        if not service_id:
            raise SystemExit("结构角色未绑定模型服务；请先在客户端设置中配置。")
        service = store.get(service_id)
        provider_factory = lambda: store.config_for(service_id)  # noqa: E731
        print(f"模型服务：{service['name']} · {service['model']} · base_url={service['base_url']}")
    print(f"硬上限：每臂 {args.cap_requests} 请求 · 累计 {args.cap_tokens} token")

    document, payload = load_payload()
    print(f"快照候选：{len(payload['title_candidates'])} · 目录条目：{len(payload['authoritative_toc'])} · "
          f"已定位 {len(payload.get('matched_titles') or [])} · 未收录 {len(payload.get('unlisted_titles') or [])}")

    cap_tokens_state = {"tokens": 0, "cap": args.cap_tokens}

    results: dict[str, dict[str, Any]] = {}
    status = "completed"
    try:
        for name in arms_requested:
            results[name] = run_arm(name, document, payload, provider_factory, args.cap_requests, cap_tokens_state)
    except CapExceeded as error:
        print(f"硬上限触发，提前结束：{error}")
        status = "cap_exceeded"
    except ProviderError as error:
        print(f"模型服务错误，提前结束：{error}")
        status = "provider_error"

    summary = {}
    for name, arm in results.items():
        in_tokens = sum(r["input_tokens"] for r in arm["requests"])
        out_tokens = sum(r["output_tokens"] for r in arm["requests"])
        reasoning_tokens = sum(r["reasoning_tokens"] for r in arm["requests"])
        in_chars = sum(r["system_chars"] + r["user_chars"] for r in arm["requests"])
        summary[name] = {
            "requests": arm["request_count"],
            "input_chars": in_chars, "input_tokens": in_tokens,
            "output_tokens": out_tokens, "reasoning_tokens": reasoning_tokens,
            "planning_requests": arm["planning"]["requests"], "review_requests": arm["review"]["requests"],
            "wall_seconds": arm["wall_seconds"],
            "hierarchy_issues": arm["hierarchy_review"]["issues"],
            "suggestions": len(arm["suggestions"]),
        }
    gold = build_gold_review(payload, results) if results else []

    report = {
        "experiment": "structure-ab-real", "date": time.strftime("%Y-%m-%d"),
        "mode": "REAL model calls (paid); serial; no reuse; per-arm cache cleared",
        "snapshot": str(SNAPSHOT), "service": {"id": service_id, "model": service["model"]},
        "caps": {"requests_per_arm": args.cap_requests, "tokens_total": args.cap_tokens},
        "status": status, "cumulative_tokens": cap_tokens_state["tokens"],
        "candidates": len(payload["title_candidates"]),
        "summary": summary, "gold_review": gold,
        "gold_review_note": "in_confirmed_toc=false 的项为未收录标题；per_arm 记录各臂最终 role/level/parent。"
                            "人工逐条判：正确/错误/无法判定，并记录依据（目录/编号/原页几何/语义）。",
        "arms": {name: {k: v for k, v in arm.items() if k != "final_decisions"} for name, arm in results.items()},
        "final_decisions": {name: arm["final_decisions"] for name, arm in results.items()},
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"structure-ab-real-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n==== 汇总 ====")
    for name, item in summary.items():
        print(f"臂 {name}: {item['requests']} 请求 · 输入 {item['input_chars']} 字符 / {item['input_tokens']} token · "
              f"输出 {item['output_tokens']} + 思考 {item['reasoning_tokens']} token · {item['wall_seconds']}s · "
              f"树问题 {item['hierarchy_issues']} · 建议 {item['suggestions']}")
    print(f"gold 核对项：{len(gold)}（其中未收录标题 {sum(1 for g in gold if not g['in_confirmed_toc'])}）")
    print(f"报告：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
