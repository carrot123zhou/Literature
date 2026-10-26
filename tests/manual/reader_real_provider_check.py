"""真实外部服务验收：阅读层折叠（五层）+ 阅读问答。

用**已授权的本机生产模型服务**做一次极小样本的真实请求（不改生产配置、不落生产库）：
1. 只读复制 `~/Library/Application Support/Literature/model-services.json` 到隔离 runtime；
2. 密钥仍从 macOS Keychain 只读取（不写入、不回显）；
3. 从 `library/` 复制一本真实书目的 document.json 到隔离项目（原件只读）；
4. 折叠前 2 节（mode=auto，关思考）+ 基于真实选段提问一次；
5. 校验：brief 逐字来自原文、evidence 合法、citation id 合法、事实来源文件哈希不变。

用法：.venv/bin/python tests/manual/reader_real_provider_check.py [--sections N]
产出：tmp/reader-real-provider-result.json（含请求数、token、费用、答案摘录）。

边界：这是「真实外部服务」验收；离线/无授权服务时报不可达并说明，不伪装通过。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

PRODUCTION_RUNTIME = Path.home() / "Library" / "Application Support" / "Literature"
LIBRARY = REPO / "library"
OUT = REPO / "tmp" / "reader-real-provider-result.json"
READER_LIBRARY = Path(__file__).resolve().parents[2] / "library"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pick_source() -> Path | None:
    candidates = [path for path in sorted(LIBRARY.iterdir() if LIBRARY.is_dir() else [])
                  if (path / "document" / "document.json").is_file()]
    return candidates[0] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sections", type=int, default=2)
    parser.add_argument("--with-trans", type=int, default=1, help="额外折叠的 zh-CN 最小节数（0 关闭）")
    parser.add_argument("--service", default="", help="服务 ID（默认取 translation 角色绑定）")
    parser.add_argument("--model", default="", help="模型 ID（默认取 translation 角色绑定）")
    args = parser.parse_args()

    source_project = pick_source()
    if source_project is None:
        print("SKIP: library/ 里没有已解析的真实书目")
        return 2

    temporary = tempfile.TemporaryDirectory()
    base = Path(temporary.name)
    runtime, library = base / "runtime", base / "library"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "session-token").write_text("", encoding="utf-8")

    source_config = PRODUCTION_RUNTIME / "model-services.json"
    if not source_config.is_file():
        print("SKIP: 生产运行目录没有 model-services.json")
        return 2
    shutil.copyfile(source_config, runtime / "model-services.json")

    os.environ["LITERATURE_RUNTIME"] = str(runtime)
    os.environ["LITERATURE_LIBRARY"] = str(library)
    os.environ["LITERATURE_SESSION_TOKEN"] = ""

    from literature.model_services import ROLE_TRANSLATION, ModelServiceStore
    from literature.providers import ProviderError
    from literature.reader import ReaderLayerService, ReaderStore, answer_question, build_sections, local_fold_section
    from literature.storage import ProjectStore

    models = ModelServiceStore(runtime)
    binding = models.roles().get(ROLE_TRANSLATION, {})
    service_id = args.service or str(binding.get("service_id") or "")
    model = args.model or str(binding.get("model") or "")
    if not service_id:
        print("SKIP: 生产配置里没有 translation 角色绑定")
        return 2

    store = ProjectStore(library)
    origin = base / "sample.pdf"
    origin.write_bytes(b"%PDF-1.4\n% fixture")
    project = store.create_project(origin, title=source_project.name[:80])
    project_id = project["project_id"]
    document_path = store.project_path(project_id) / "document" / "document.json"
    document_path.parent.mkdir(parents=True, exist_ok=True)
    original_document = source_project / "document" / "document.json"
    shutil.copyfile(original_document, document_path)
    translation_source = source_project / "translations" / "zh-CN" / "translation.json"
    translation_target = None
    if translation_source.is_file():
        translation_target = store.translation_path(project_id, "zh-CN")
        translation_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(translation_source, translation_target)

    facts_before = {path: sha256(path) for path in (original_document, document_path, translation_source)}

    provider = models.config_for(service_id, model)
    provider = provider.with_thinking_disabled()
    document = store.load_document(project_id)
    all_sections = build_sections(document)
    wanted = [section["id"] for section in all_sections[: max(1, args.sections)]]

    result: dict = {
        "book": source_project.name,
        "service_id": service_id,
        "model": model,
        "base_url": provider.config.base_url,
        "plaintext": bool(provider.config.allow_plaintext_http),
        "sections_requested": wanted,
        "checks": {},
    }
    usage: list = []
    try:
        service = ReaderLayerService(store, ReaderStore(store))
        folded = service.generate(
            project_id, locale="source", provider=provider, model_label=f"{service_id}/{model}",
            section_ids=wanted, force=True, mode="auto", usage_hook=usage.append,
        )
    except ProviderError as error:
        print(f"FAIL: 模型请求失败（服务不可达或鉴权失败）：{error}")
        result["error"] = str(error)
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    result["checks"]["generated_sections"] = folded["generated_sections"]
    result["checks"]["failed_sections"] = folded["failed_sections"]
    result["checks"]["warnings"] = folded["warnings"][:5]
    verbatim_ok = True
    evidence_ok = True
    sources: set[str] = set()
    for section in folded["sections"]:
        for unit in section["units"]:
            sources.add(str(unit.get("source") or ""))
            # evidence 是 0 起的句子下标（上游 ladder 契约）；suppressed 单元可以没有 brief。
            sentences = unit.get("sentences") or []
            for number in unit.get("evidence") or []:
                if not (isinstance(number, int) and 0 <= number < len(sentences)):
                    evidence_ok = False
            brief = unit.get("brief") or []
            full = unit.get("full") or ""
            for sentence in brief:
                if sentence not in full:
                    verbatim_ok = False
            if brief and not (unit.get("evidence") or []):
                evidence_ok = False
    result["unit_sources"] = sorted(sources)
    result["checks"]["brief_verbatim"] = verbatim_ok
    result["checks"]["evidence_in_range"] = evidence_ok
    result["checks"]["provenance"] = folded["provenance"]
    result["usage_fold"] = [
        {"model": item.model, "input": item.input_tokens, "output": item.output_tokens,
         "reasoning": item.reasoning_tokens, "cost": item.estimated_cost}
        for item in usage
    ]

    # zh-CN 小样本：挑单元数最少的一节（3–5 单元优先）用真实模型折叠，控费。
    if args.with_trans > 0 and translation_target is not None:
        from literature.models import TranslationLayer as _TL

        zh_layer = store.load_translation(project_id, "zh-CN")
        zh_sections = build_sections(document, locale="zh-CN", translation=zh_layer)
        candidates = [section for section in zh_sections if section["units"]]
        candidates.sort(key=lambda section: len(section["units"]))
        if candidates:
            zh_target = candidates[0]
            zh_wanted = [zh_target["id"]]
            zh_usage: list = []
            try:
                zh_folded = service.generate(
                    project_id, locale="zh-CN", provider=provider, model_label=f"{service_id}/{model}",
                    section_ids=zh_wanted, force=True, mode="auto", usage_hook=zh_usage.append,
                )
                zh_section = next((s for s in zh_folded["sections"] if s["id"] == zh_target["id"]), None)
                zh_verbatim = True
                zh_evidence_ok = True
                fallback_ok = True
                if zh_section:
                    for unit in zh_section["units"]:
                        for sentence in unit.get("brief") or []:
                            if sentence not in (unit.get("full") or ""):
                                zh_verbatim = False
                        sentences = unit.get("sentences") or []
                        for number in unit.get("evidence") or []:
                            if not (isinstance(number, int) and 0 <= number < len(sentences)):
                                zh_evidence_ok = False
                        # 缺译单元必须显式 fallback，不得把原文冒充译文
                        if unit.get("fallback"):
                            block = next((b for b in document.blocks if b.block_id == unit["id"]), None)
                            if block is not None and unit.get("full") != (block.source_text or "").strip():
                                fallback_ok = False
                result["zh_cn"] = {
                    "section": zh_target["id"],
                    "title": zh_target.get("title", ""),
                    "units": len(zh_target["units"]),
                    "generated": bool(zh_section),
                    "brief_verbatim": zh_verbatim,
                    "evidence_in_range": zh_evidence_ok,
                    "fallback_honest": fallback_ok,
                    "usage": [{"input": u.input_tokens, "output": u.output_tokens, "reasoning": u.reasoning_tokens} for u in zh_usage],
                }
                result["checks"]["zh_brief_verbatim"] = zh_verbatim
                result["checks"]["zh_evidence_in_range"] = zh_evidence_ok
                result["checks"]["zh_fallback_honest"] = fallback_ok
            except ProviderError as error:
                result["zh_cn"] = {"error": str(error)}
                result["checks"]["zh_brief_verbatim"] = False

    # 基于真实选段提问：取第一节第一个 unit 的原文句子。
    sections_now = folded["sections"]
    quoted = next(
        (unit for section in sections_now for unit in section["units"] if unit["sentences"]), None,
    )
    if quoted is not None:
        ask_usage: list = []
        quote_text = quoted["sentences"][0][:120]
        answer = answer_question(
            document, [local_fold_section(item) for item in build_sections(document)],
            provider, question="这段话的核心主张是什么？",
            quote={"block_id": quoted["id"], "pane": "source", "text": quote_text},
            usage_hook=ask_usage.append,
        )
        known = {item["block_id"] for item in answer["citations"]}
        result["ask"] = {
            "quote_block": quoted["id"],
            "quote": quote_text,
            "answer": answer["answer"][:600],
            "citations": [item["block_id"] for item in answer["citations"]],
            "citations_valid": known <= {block.block_id for block in document.blocks},
            "model": answer["model"],
            "usage": answer["usage"],
        }
        result["checks"]["answer_citations_valid"] = bool(result["ask"]["citations_valid"])

    provider.close()
    facts_after = {path: sha256(path) for path in (original_document, document_path, translation_source)}
    result["checks"]["facts_unchanged"] = facts_before == facts_after
    result["checks"]["reader_dir"] = sorted(
        path.name for path in (store.project_path(project_id) / "reader").glob("*")
    ) if (store.project_path(project_id) / "reader").is_dir() else []

    ok = all([
        result["checks"]["brief_verbatim"], result["checks"]["evidence_in_range"],
        result["checks"]["facts_unchanged"], result["checks"].get("answer_citations_valid", True),
        result["checks"].get("zh_brief_verbatim", True),
        result["checks"].get("zh_evidence_in_range", True),
        result["checks"].get("zh_fallback_honest", True),
    ])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
    print("PASS" if ok else "FAIL")
    temporary.cleanup()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
