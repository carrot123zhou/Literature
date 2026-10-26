"""复现「章级 AI 折叠刚成功，layers 却被判 stale」的缓存键不一致（只读诊断，无网络/无计费）。

用途：把 core 的**写盘 digest**与 `document()` 的**读盘 expected digest**逐节摊开对比，
定位是哪一节、哪一个键不一致。用受控 provider（协议回复，不走任何真实服务）。

复现：章级折叠 S1（`section_ids=["S1"]`）后立即 GET `/reader/layers?locale=zh-CN|source`，
`stale=true` 同时源文档/译文未变，属于状态误报（UI 会显示「重新折叠（内容已过期）」）。

用法：
    .venv/bin/python scripts/reader_stale_probe.py            # 人类可读表格
    .venv/bin/python scripts/reader_stale_probe.py --json     # 机读 JSON（给 core）
退出码：0 = 全部 MATCH（无该问题）；1 = 存在 MISMATCH（可复现）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCE_LOCALE = "source"
TARGET_LOCALE = "zh-CN"


class _ProtocolProvider:
    """按上游提示词协议回复的受控 provider：source fold 一支、target derive 一支。"""

    def __init__(self, service_id: str, model: str) -> None:
        self._service_id, self._model = service_id, model
        self.config = type("C", (), {
            "model": model, "provider": service_id, "base_url": "https://probe.invalid",
            "allow_plaintext_http": False, "reasoning_style": "", "reasoning_effort": "",
            "reasoning_budget": 0, "max_output_tokens": 4096, "temperature": 0.2,
            "input_price_per_million": 0, "output_price_per_million": 0,
        })()
        self.calls = 0

    def _units(self, user: str) -> list[dict[str, object]]:
        units: list[dict[str, object]] = []
        current: str | None = None
        sentences: list[str] = []
        for line in user.splitlines() + ["[END]"]:
            header = re.match(r"^\[(\S+)\]", line)
            if header:
                if current and sentences:
                    ranked = sorted(range(len(sentences)), key=lambda i: len(sentences[i]), reverse=True)
                    evidence = sorted(ranked[:2])
                    core = sentences[evidence[0]]
                    units.append({
                        "id": current, "role": "claim", "takeaway": core.rstrip(".")[:80],
                        "topic": " ".join(core.split()[:3]), "reason": "It states the claim.",
                        "evidence": evidence,
                    })
                current, sentences = header.group(1), []
                continue
            body = re.match(r"^\s*(\d+):\s+(.*)$", line)
            if current is not None and body:
                sentences.append(body.group(2).strip())
        if current and sentences:
            ranked = sorted(range(len(sentences)), key=lambda i: len(sentences[i]), reverse=True)
            evidence = sorted(ranked[:2])
            core = sentences[evidence[0]]
            units.append({
                "id": current, "role": "claim", "takeaway": core.rstrip(".")[:80],
                "topic": " ".join(core.split()[:3]), "reason": "It states the claim.",
                "evidence": evidence,
            })
        return units

    def complete(self, *, system: str, user: str):
        from literature.providers import ProviderResponse

        self.calls += 1
        units = self._units(user)
        if "do NOT translate" in user:
            for unit in units:
                unit["ev_sentences"] = [f"译句{index}" for index in range(len(unit.get("evidence") or []))]
        payload = {"section": {"takeaway": "主线", "topic": "主线"}, "units": units}
        return ProviderResponse(content=json.dumps(payload, ensure_ascii=False),
                                provider=self._service_id, model=self._model, request_id="probe",
                                input_tokens=10, output_tokens=5, estimated_cost=0.0)

    def close(self) -> None:
        pass


def _wait_job(client, headers, project_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    job: dict = {}
    while time.time() < deadline:
        job = client.get(f"/projects/{project_id}/reader/state", headers=headers).json().get("layer_job") or {}
        if job.get("status") in ("completed", "failed"):
            return job
        time.sleep(0.1)
    return job


def _snapshot(client, store, service, project_id: str, document, headers) -> dict:
    """当前 layers 的逐节 digest 对比（写盘 vs document() 重算）。"""
    from literature.reader import SOURCE_LOCALE as CORE_SOURCE, build_sections, section_digest

    stored_hashes = service.reader.load_layers(project_id).get("section_hashes", {})
    result: dict = {"locales": {}, "mismatches": []}
    for locale in (TARGET_LOCALE, CORE_SOURCE):
        payload = client.get(f"/projects/{project_id}/reader/layers?locale={locale}", headers=headers).json()
        meta = payload.get("digest") or {}
        translation = store.load_translation(project_id, locale) if locale != CORE_SOURCE else None
        sections = build_sections(document, locale=locale, translation=translation)
        stored_by_id = {item.get("id"): item for item in payload.get("sections") or []}
        hashes = stored_hashes.get(locale, {})
        rows = []
        for section in sections:
            section_id = section["id"]
            stored_section = stored_by_id.get(section_id) or {}
            has_ai = any(unit.get("source") == "model" for unit in stored_section.get("units") or [])
            if not has_ai:
                kind, expected = "local", service._local_digest(section, locale)
            elif locale != CORE_SOURCE:
                kind = "derive"
                expected = service._derive_digest_for(project_id, section, locale,
                                                      service_id=meta.get("service_id", ""),
                                                      model=meta.get("model", ""), translation=translation)
            else:
                kind = "fold"
                expected = section_digest(section, locale, service_id=meta.get("service_id", ""),
                                          model=meta.get("model", ""))
            stored = hashes.get(section_id) or ""
            rows.append({"section": section_id, "kind": kind, "has_ai": has_ai,
                         "units": len(stored_section.get("units") or []),
                         "stored": stored, "expected": expected, "match": stored == expected})
        result["locales"][locale] = {"stale": payload.get("stale"), "content": payload.get("content"),
                                     "digest_meta": meta, "sections": rows}
        result["mismatches"].extend({"locale": locale, **row} for row in rows if not row["match"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="复现章级折叠后 layers 被误判 stale 的缓存键不一致")
    parser.add_argument("--json", action="store_true", help="追加一行机读 JSON（最后一行，便于管道解析）")
    parser.add_argument("--out", default="", help="把机读 JSON 写到该文件（干净、无第三方 warning 干扰）")
    parser.add_argument("--service", default="", help="折叠角色绑定的服务商 id（默认沿用 fixture 的 svc-chat）")
    parser.add_argument("--model", default="", help="折叠角色绑定的模型 id（默认沿用 fixture 的 model-c1）")
    args = parser.parse_args()

    root = Path(tempfile.mkdtemp(prefix="reader-stale-probe-"))
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")
    os.environ.update(LITERATURE_RUNTIME=str(runtime), LITERATURE_LIBRARY=str(root / "library"),
                      LITERATURE_SESSION_TOKEN="")

    from fastapi.testclient import TestClient

    from literature.api import create_app
    from literature.reader import (
        SOURCE_LOCALE as CORE_SOURCE,
        ReaderLayerService,
        build_sections,
        section_digest,
    )
    from tests.manual.reader_ui_browser_check import seed_project

    store, project_id = seed_project(root)
    if args.service or args.model:
        # 让同一份 service/model 贯穿：改写 fixture 的 reader_fold 角色绑定并落盘，
        # core 复现时可以用自己的服务/模型跑出同样的 digest_meta。
        config_path = runtime / "model-services.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        roles = config.setdefault("roles", {})
        roles["reader_fold"] = {"service_id": args.service or roles.get("reader_fold", {}).get("service_id", ""),
                                "model": args.model or roles.get("reader_fold", {}).get("model", "")}
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    app = create_app(store)
    provider = _ProtocolProvider("svc-chat", "model-c1")
    app.state.job_manager.provider_from = lambda service_id, provider_input=None, model="": _ProtocolProvider(
        service_id, model or "model-c1")
    client = TestClient(app)
    headers = {"x-literature-token": ""}

    client.post(f"/projects/{project_id}/reader/layers", headers=headers,
                json={"locale": TARGET_LOCALE, "mode": "local"})
    _wait_job(client, headers, project_id)
    service = ReaderLayerService(store)
    document = store.load_document(project_id)
    baseline = _snapshot(client, store, service, project_id, document, headers)

    started = client.post(f"/projects/{project_id}/reader/layers", headers=headers,
                          json={"locale": TARGET_LOCALE, "mode": "auto", "section_ids": ["S1"]}).json()
    job = _wait_job(client, headers, project_id)
    after = _snapshot(client, store, service, project_id, document, headers)

    report: dict = {
        "project_id": project_id,
        "fold_request": {"locale": TARGET_LOCALE, "mode": "auto", "section_ids": ["S1"], "job_id": started.get("job_id")},
        "job": {"status": job.get("status"), "error": job.get("error") or "", "scope": job.get("scope")},
        "provider_calls": provider.calls,
        "baseline_local_only": baseline,
        "after_chapter_fold": after,
        "locales": after["locales"],
        "mismatches": after["mismatches"],
    }

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 已写入 {args.out}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    if not args.json:
        base = report["baseline_local_only"]
        print(f"job: {report['job']}  provider_calls={report['provider_calls']}")
        print("baseline（只有本地预览时）: " + ", ".join(
            f"{locale} stale={block['stale']} mismatch={sum(1 for row in block['sections'] if not row['match'])}"
            for locale, block in base["locales"].items()))
        for locale, block in report["locales"].items():
            print(f"\n[{locale}] stale={block['stale']} content={block['content']} digest_meta={json.dumps(block['digest_meta'], ensure_ascii=False)}")
            for row in block["sections"]:
                flag = "MATCH   " if row["match"] else "MISMATCH"
                print(f"  {flag} {row['section']:<4} kind={row['kind']:<7} has_ai={str(row['has_ai']):<5} "
                      f"stored={(row['stored'] or '(空)')[:12]:<14} expected={row['expected'][:12]}")
        if report["mismatches"]:
            print("\n结论：存在写盘/读盘 digest 不一致 → layers 被判 stale，但源文档与译文未变（状态误报）。")
        else:
            print("\n结论：全部 MATCH，未复现 stale。")
    return 1 if report["mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
