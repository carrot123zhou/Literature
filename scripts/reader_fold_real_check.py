"""真实模型小样本：当前章五层折叠 + 译文层继承，逐层产物 + usage + 来源落盘证据。

用法：
    .venv/bin/python scripts/reader_fold_real_check.py [--project <library 下项目名>] [--sections 1]

- 模型绑定只读 Keychain 里已授权的服务（model_services role_state("reader_fold")，
  未设置时回退翻译默认），与产品主路径同一条解析规则；不改生产角色/价格。
- 默认只折 1 节（不整书、不评估不跑高费作业）；译文层只在原文节就绪后折同一节。
- 输出每层文本、unit source、模型 usage 与 reader/layers.json 落盘/复用证据。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from literature.model_services import ModelServiceStore, ROLE_READER_FOLD, ROLE_TRANSLATION  # noqa: E402
from literature.reader import ReaderLayerService, ReaderStore  # noqa: E402
from literature.storage import ProjectStore  # noqa: E402


def resolve_binding(manager: ModelServiceStore) -> tuple[dict[str, str], str]:
    """与产品主路径同一条解析规则：reader_fold → 继承翻译默认。只读，不写角色配置。

    返回 (binding, 来源说明)；reader_fold 被显式清空时如实报错（产品同样会 400），
    但可用 --fallback-translation 显式借用翻译默认模型跑证据（脚本内显式选择，不改配置）。
    """
    state = manager.role_state(ROLE_READER_FOLD)
    source = str(state.get("origin") or "")
    if source == "unset":
        inherited = manager.role_state(ROLE_TRANSLATION)
        if inherited.get("origin") == "bound":
            return dict(inherited.get("binding") or {}), "inherited_translation"
    if state.get("binding", {}).get("service_id"):
        return dict(state.get("binding") or {}), "reader_role"
    raise SystemExit(
        f"没有可用的折叠模型绑定：{state}\n"
        "（reader_fold 被显式清空；可用 --fallback-translation 借翻译默认模型跑本次证据，不改配置）"
    )


def make_provider(manager: ModelServiceStore, service_id: str, model: str):
    """与产品主路径同一条思考策略：with_thinking_disabled → 已探测变体缓存复用，
    不重新发探测请求（探测由产品作业完成并落盘）。"""
    provider = manager.config_for(service_id, model)
    if hasattr(provider, "with_thinking_disabled"):
        provider = provider.with_thinking_disabled()
    cached = manager.thinking_policy(service_id, model)
    if cached is not None and hasattr(provider, "with_reasoning"):
        provider = provider.with_reasoning(
            style=str(cached.get("style") or ""),
            effort=str(cached.get("effort") or ""),
            variant=str(cached.get("variant") or ""),
        )
    return provider


def show(service: ReaderLayerService, project_id: str, locale: str, title: str) -> None:
    doc = service.document(project_id, locale=locale)
    print(f"\n===== {title} (locale={locale}) =====")
    provenance = doc.get("provenance") or {}
    print(f"content={doc.get('content')} ai_generated={doc.get('ai_generated')} "
          f"partial={doc.get('partial')} failed={[f.get('id') for f in doc.get('failed') or []]}")
    print(f"provenance.model={provenance.get('model')} ai_units={provenance.get('ai_units')} "
          f"local_units={provenance.get('local_units')} prompt_version={provenance.get('prompt_version')}")
    for section in (doc.get("sections") or [])[:1]:
        print(f"\n[SECTION {section['id']}] {section['title']}  source={section.get('source')}")
        print(f"  主线 takeaway: {section.get('takeaway')}")
        print(f"  主题 topic:    {section.get('topic')}")
        for unit in section["units"]:
            print(f"\n  ◆ {unit['id']}  source={unit.get('source')} evidence={unit.get('evidence')}")
            print(f"    全文 full:     {(unit.get('full') or '')[:110]}{'…' if len(unit.get('full') or '') > 110 else ''}")
            print(f"    简述 brief(逐字): {' ︙ '.join(unit.get('brief') or [])[:160]}")
            print(f"    要点 key(转写): {(unit.get('key') or '')[:160]}")
            print(f"    主线 takeaway: {(unit.get('takeaway') or '')[:160]}")
            print(f"    主题 topic:    {(unit.get('topic') or '')[:60]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="")
    parser.add_argument("--sections", type=int, default=1)
    parser.add_argument("--fallback-translation", action="store_true",
                        help="reader_fold 未绑定时，显式借用翻译默认模型跑本次证据（不写配置）")
    args = parser.parse_args()

    library = ROOT / "library"
    projects = sorted(p for p in library.iterdir() if p.is_dir() and (p / "project.json").is_file())
    if args.project:
        target = library / args.project
    else:
        target = next((p for p in projects if (p / "translations" / "zh-CN" / "translation.json").is_file()), projects[0])
    # project_id 是 project.json 里的稳定 id（目录名只是展示名）。
    project_id = str(json.loads((target / "project.json").read_text(encoding="utf-8")).get("project_id") or target.name)
    print(f"项目：{target.name} ({project_id})")

    store = ProjectStore(library)
    manager = ModelServiceStore()
    if args.fallback_translation:
        inherited = manager.role_state(ROLE_TRANSLATION)
        if inherited.get("origin") != "bound":
            raise SystemExit("翻译默认模型也未绑定，无法借用来跑证据。")
        binding, bind_source = dict(inherited.get("binding") or {}), "explicit(fallback-translation)"
    else:
        binding, bind_source = resolve_binding(manager)
    print(f"折叠绑定：service={binding['service_id']} model={binding['model']} 来源={bind_source}")

    service = ReaderLayerService(store, ReaderStore(store))
    provider = make_provider(manager, binding["service_id"], binding["model"])
    usage: list[dict] = []

    def hook(response) -> None:
        usage.append({
            "model": response.model, "input": response.input_tokens, "output": response.output_tokens,
            "cached": response.cached_tokens, "reasoning": response.reasoning_tokens,
            "cost": response.estimated_cost,
        })

    # 只折第一节（当前章小样本）：先原文层，再译文层继承。
    document = store.load_document(project_id)
    from literature.reader import build_sections
    sections = build_sections(document)
    wanted = [item["id"] for item in sections[: args.sections]]
    print(f"折叠章节：{wanted}（共 {len(sections)} 节，不跑整书）")

    result = service.generate(
        project_id, provider=provider, model_label=binding["model"],
        service_id=binding["service_id"], section_ids=wanted, mode="auto",
        usage_hook=hook,
    )
    print(f"\n原文层完成：generated={result['generated_sections']} reused={result['reused_sections']} "
          f"failed={[f.get('id') for f in result.get('failed') or []]} "
          f"ai_units={result['ai_units']} local_units={result['local_units']}")

    translation = store.load_translation(project_id, "zh-CN")
    if translation is not None:
        result_tr = service.generate(
            project_id, locale="zh-CN", provider=provider, model_label=binding["model"],
            service_id=binding["service_id"], section_ids=wanted, mode="auto",
            usage_hook=hook,
        )
        print(f"译文层完成：generated={result_tr['generated_sections']} failed={[f.get('id') for f in result_tr.get('failed') or []]}")
        show(service, project_id, "zh-CN", "译文层（继承原文 evidence）")
    provider.close()

    show(service, project_id, "source", "原文层")
    print("\n===== 真实模型 usage（逐请求） =====")
    for i, item in enumerate(usage, 1):
        print(f"  #{i} model={item['model']} in={item['input']} out={item['output']} "
              f"cached={item['cached']} reasoning={item['reasoning']} est_cost={item['cost']}")
    # 落盘证据：layers.json 的 digest/provenance/failed + 复用验证
    layers = ReaderStore(store).load_layers(project_id)
    print("\n===== 落盘证据（reader/layers.json） =====")
    for locale, doc in (layers.get("docs") or {}).items():
        prov = doc.get("provenance") or {}
        print(f"  {locale}: sections={len(doc.get('sections') or [])} content={prov.get('content')} "
              f"model={prov.get('model')} prompt_version={prov.get('prompt_version')} failed={doc.get('failed')}")
    again = service.generate(
        project_id, provider=provider, model_label=binding["model"],
        service_id=binding["service_id"], section_ids=wanted, mode="auto",
    )
    print(f"\n复用验证：重跑同节 reused_sections={again['reused_sections']} generated_sections={again['generated_sections']}（应复用不重调）")
    print(json.dumps({"usage_requests": len(usage)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
