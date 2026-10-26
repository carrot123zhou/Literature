"""自然问题检索评测（免 embedding 路线的诚实边界）。

`scripts/reader_retrieval_eval.py` 的查询是从被检索段落/其译文**抽词**造出来的——
Recall 1.00 属于回检自测，不能当作「自然问题/纯语义/无译文跨语言」的准确率。
本脚本补三类更接近真实使用的查询，结果如实报告（包括失败）：

1. **自然问题**：人工写的中英文问题（不是从段落抽词），问真实书里的具体事实；
2. **同义改写**：把段落里的词换成同义词/上位词（词面不匹配，考察语义缺口）；
3. **无译文跨语言**：只对没有 zh 译文的段落用中文问题查英文正文（最坏情形）。

判定：命中 = 目标段落的 block_id 出现在 retrieve(..., k=8) 结果里；同时给出
「目标段落所在章节被命中」的宽松口径（读者能被带到正确章节也算有用）。

用法：.venv/bin/python tests/manual/reader_natural_query_eval.py
结果写 tmp/reader-natural-query-eval.json。本脚本只读真实书目，不写任何项目文件。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

LIBRARY = REPO / "library"
OUT = REPO / "tmp" / "reader-natural-query-eval.json"


def load_book(pattern: str):
    from literature.models import Document, TranslationLayer

    candidates = sorted(path for path in LIBRARY.iterdir() if (path / "document" / "document.json").is_file())
    if pattern:
        candidates = [path for path in candidates if pattern.casefold() in path.name.casefold()]
    if not candidates:
        raise SystemExit(f"library/ 里没有匹配 {pattern!r} 的书")
    project = candidates[0]
    document = Document.from_dict(json.loads((project / "document" / "document.json").read_text(encoding="utf-8")))
    translation = None
    path = project / "translations" / "zh-CN" / "translation.json"
    if path.is_file():
        translation = TranslationLayer.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return project, document, translation


def build_merged(document, translation):
    from literature.models import TranslationLayer
    from literature.textfold_adapter import build_ladder_doc

    source_ladder = build_ladder_doc(document)
    merged = {"units": {}, "atoms": {}}
    for block_id, unit in source_ladder["units"].items():
        merged["units"][block_id] = dict(unit)
    if translation:
        display = build_ladder_doc(document, locale="zh-CN", translation=translation)
        for block_id, unit in display["units"].items():
            if block_id in merged["units"]:
                merged["units"][block_id]["tr"] = {"zh": {"toks": unit["toks"]}}
    return merged


def locate(document, block_id) -> dict:
    """目标段落与其所在章节标题（宽松口径用）。"""
    current_title = ""
    for block in document.blocks:
        if block.type == "title":
            current_title = block.source_text or ""
        if block.block_id == block_id:
            return {"title": current_title, "order": block.order}
    return {"title": "", "order": -1}


def evaluate(merged, document, cases, lang) -> list[dict]:
    from third_party.textfold import retrieval as tf_retrieval

    blocks = {block.block_id: block for block in document.blocks}
    rows = []
    for case in cases:
        target = case["target"]
        begin = time.perf_counter()
        order = tf_retrieval.retrieve(merged, case["query"], lang, k=8)
        elapsed = (time.perf_counter() - begin) * 1000
        exact = target in order
        where = locate(document, target)
        loose = False
        if not exact:
            for block_id in order:
                candidate = locate(document, block_id)
                if candidate["title"] and candidate["title"] == where["title"]:
                    loose = True
                    break
        rows.append({
            "query": case["query"],
            "target": target,
            "lang": lang,
            "hit_exact": exact,
            "hit_same_section": loose,
            "rank": order.index(target) if exact else -1,
            "latency_ms": round(elapsed, 1),
            "target_excerpt": (blocks[target].source_text or "")[:100] if target in blocks else "",
            "note": case.get("note", ""),
        })
    return rows


def main() -> int:
    project, document, translation = load_book("Between Cultures")
    merged = build_merged(document, translation)
    blocks = {block.block_id: block for block in document.blocks}

    # ---- 用例来自真实书（Between Cultures），人工撰写的自然问题 ----
    # 目标段落先在真实书里核对存在，再写问题（脚本最后会断言目标存在）。
    natural_cases = [
        # Seigel 五个传主之一 Burton：著作与身份
        {"target": None, "query": "Which book did Richard Burton write about his journey to Mecca?", "note": " Burton 朝觐著作"},
        {"target": None, "query": "理查德·伯顿去麦加朝圣后写了什么书？", "note": "中文自然问题"},
        # Lawrence
        {"target": None, "query": "How did T. E. Lawrence feel about being famous?", "note": "Lawrence 对名声的态度"},
        # Massignon
        {"target": None, "query": "What role did Louis Massignon play in the Catholic Church?", "note": "Massignon 与天主教"},
        # Achebe
        {"target": None, "query": "Why did Chinua Achebe write Things Fall Apart?", "note": "Achebe 写作动机"},
    ]
    # 把自然问题钉到真实书里包含对应实体、且被检索索引收录的段落。
    anchors = {
        "Burton": r"Burton",
        "Lawrence": r"Lawrence",
        "Massignon": r"Massignon",
        "Achebe": r"Achebe",
    }

    def find_block(pattern: str, min_len: int = 120) -> str | None:
        import re

        for block in document.blocks:
            if block.type in ("paragraph", "list") and len(block.source_text or "") >= min_len \
                    and re.search(pattern, block.source_text):
                return block.block_id
        return None

    for case in natural_cases:
        for name, pattern in anchors.items():
            if name in (case.get("note") or "") or name in case["query"]:
                case["target"] = find_block(pattern)
                break

    synonym_cases = []
    for case in natural_cases:
        if case["target"]:
            synonym_cases.append({
                "target": case["target"],
                "query": case["query"].replace("book", "volume").replace("write", "compose").replace("famous", "celebrity status"),
                "note": "同义改写（词面替换）",
            })

    rows_natural = evaluate(merged, document, [c for c in natural_cases if c["target"]], "en")
    # 中文自然问题走双语词袋
    rows_cross = evaluate(merged, document, [c for c in natural_cases if c["target"] and any("\u4e00" <= ch <= "\u9fff" for ch in c["query"])], "zh")
    rows_synonym = evaluate(merged, document, synonym_cases, "en")

    # 对照组：同一目标、关键词式查询（词面检索的可用形态）
    keyword_cases = []
    keyword_map = {
        "Richard Burton": "Burton Mecca pilgrimage",
        "T. E. Lawrence": "Lawrence fame aircraft",
        "Louis Massignon": "Massignon Catholic faith",
        "Chinua Achebe": "Achebe Things Fall Apart",
    }
    for case in natural_cases:
        if case["target"]:
            for name, query in keyword_map.items():
                if name in (case.get("note") or ""):
                    keyword_cases.append({"target": case["target"], "query": query, "note": "关键词式对照"})
    rows_keyword = evaluate(merged, document, keyword_cases, "en")

    # 无译文跨语言：找没有 zh 译文的正文段，用中文问题问它（最坏情形，如实报告）
    no_trans_targets = []
    if translation:
        for block in document.blocks:
            if block.type == "paragraph" and len(block.source_text or "") > 150:
                entry = translation.entries.get(block.block_id)
                if entry is None or not (entry.text or "").strip():
                    no_trans_targets.append(block.block_id)
            if len(no_trans_targets) >= 3:
                break
    rows_no_trans = []
    for block_id in no_trans_targets:
        text = blocks[block_id].source_text or ""
        question = f"书中关于 {text[:12]} 的内容在哪里？"
        rows_no_trans.extend(evaluate(merged, document, [{"target": block_id, "query": question, "note": "无译文段落+中文问题"}], "zh"))

    def summarise(rows):
        if not rows:
            return {"n": 0}
        return {
            "n": len(rows),
            "exact_hit": sum(1 for row in rows if row["hit_exact"]),
            "same_section": sum(1 for row in rows if row["hit_exact"] or row["hit_same_section"]),
            "miss": sum(1 for row in rows if not (row["hit_exact"] or row["hit_same_section"])),
        }

    result = {
        "book": project.name,
        "embedding_model_loaded": False,
        "framing": {
            "scripts/reader_retrieval_eval.py 的 Recall@8=1.00": "查询从被检索目标/其译文抽词，属回检自测，不是自然问题准确率",
            "本脚本": "人工自然问题/同义改写/无译文跨语言，如实报告命中与缺失",
        },
        "natural_questions": {"rows": rows_natural + rows_cross, "summary": summarise(rows_natural + rows_cross)},
        "synonym_paraphrase": {"rows": rows_synonym, "summary": summarise(rows_synonym)},
        "no_translation_cross_language": {"rows": rows_no_trans, "summary": summarise(rows_no_trans),
                                          "targets_without_translation": no_trans_targets},
        "keyword_style_contrast": {"rows": rows_keyword, "summary": summarise(rows_keyword)},
        "latency_ms_median": round(sorted([row["latency_ms"] for row in rows_natural + rows_cross + rows_synonym + rows_no_trans] or [0])[len([row for row in rows_natural + rows_cross + rows_synonym + rows_no_trans]) // 2], 1),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key]["summary"] for key in ("natural_questions", "synonym_paraphrase", "no_translation_cross_language")}, ensure_ascii=False, indent=1))
    print("latency_ms_median:", result["latency_ms_median"])
    for row in (rows_natural + rows_cross + rows_synonym + rows_keyword + rows_no_trans):
        print(("HIT " if row["hit_exact"] else ("SECT" if row["hit_same_section"] else "MISS")), row["lang"], row["query"][:60], "->", row["rank"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
