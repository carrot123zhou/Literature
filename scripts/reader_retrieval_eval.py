"""阅读检索评测：用真实书目测量「词面检索够不够，需不需要 embedding」。

这不是单元测试，是可复跑的评测脚本（产出 JSON 证据）。它做三件事：

1. **单语召回**：从真实书里抽 N 个正文段落，用该段最有辨识度的 3 个词造查询，
   看 TextFold 上游 `retrieve()`（IDF 词面）在 k=8 时能否找回原段（Recall@8）与名次（MRR）。
2. **跨语言召回**：同一批段落的**译文**抽词造中文查询，走 `retrieve(..., lang)` 的双语词袋，
   看能否命中原文段落（这是没有 embedding 时跨语言检索的真实上限）。
3. **成本**：每次查询的耗时与内存（词袋按需构建），以及「零下载」的事实（不加载任何模型）。

用法：
    .venv/bin/python scripts/reader_retrieval_eval.py [--book <library 下的目录名子串>] [--samples 60]

结论写进 docs/READER_BACKEND.md；本脚本不改任何项目文件（真实书目只读）。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import argparse
import json
import random
import re
import statistics
import time
from pathlib import Path

from literature.models import Block, Document, Page, TranslationLayer
from literature.reader import build_sections, local_fold_section
from literature.textfold_adapter import build_ladder_doc, split_sentences
from third_party.textfold import retrieval as tf_retrieval
from third_party.textfold import tokens as tf_tokens

REPO = REPO_ROOT
LIBRARY = REPO / "library"
STOPWORDS = tf_retrieval.STOP


def load_book(pattern: str) -> tuple[Path, Document, TranslationLayer | None]:
    candidates = sorted(path for path in LIBRARY.iterdir() if (path / "document" / "document.json").is_file())
    if pattern:
        candidates = [path for path in candidates if pattern.casefold() in path.name.casefold()]
    if not candidates:
        raise SystemExit(f"library/ 里没有匹配 {pattern!r} 的书")
    project = candidates[0]
    raw = json.loads((project / "document" / "document.json").read_text(encoding="utf-8"))
    document = Document.from_dict(raw)
    translation = None
    path = project / "translations" / "zh-CN" / "translation.json"
    if path.is_file():
        translation = TranslationLayer.from_dict(json.loads(path.read_text(encoding="utf-8")))
    return project, document, translation


def distinctive_terms(text: str, count: int = 3) -> list[str]:
    """一段文字里最有辨识度的词：长度 ≥4 的拉丁词 / 4 字以上的中文片段。"""
    latin = [word for word in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", text) if word.casefold() not in STOPWORDS]
    cjk = re.findall(r"[\u4e00-\u9fff]{4,}", text)
    picked = latin[:count] if latin else []
    if len(picked) < count:
        picked += [piece[:4] for piece in cjk[: count - len(picked)]]
    return picked[:count]


def summarise(ranks: list[int], k: int) -> dict:
    if not ranks:
        return {"n": 0}
    return {
        "n": len(ranks),
        "recall_at_k": round(sum(1 for rank in ranks if 0 <= rank < k) / len(ranks), 4),
        "mrr": round(sum(1 / (rank + 1) for rank in ranks if rank >= 0) / len(ranks), 4),
        "miss": sum(1 for rank in ranks if rank < 0),
    }


def evaluate(book_pattern: str, samples: int) -> dict:
    project, document, translation = load_book(book_pattern)
    started = time.monotonic()
    sections = build_sections(document)
    source_ladder = build_ladder_doc(document)
    display_ladder = (
        build_ladder_doc(document, locale="zh-CN", translation=translation) if translation else None
    )
    build_ms = (time.monotonic() - started) * 1000

    prose = [block for block in document.blocks if block.type in ("paragraph", "list") and len(block.source_text) > 120]
    rng = random.Random(20261025)
    picked = rng.sample(prose, min(samples, len(prose)))

    # 双语检索袋：toks 用原文，tr 用译文（与上游 retrieve(doc, q, lang) 的约定一致）。
    merged = {"units": {}, "atoms": {}}
    for block_id, unit in source_ladder["units"].items():
        merged["units"][block_id] = dict(unit)
    if display_ladder:
        for block_id, unit in display_ladder["units"].items():
            if block_id in merged["units"]:
                merged["units"][block_id]["tr"] = {"zh": {"toks": unit["toks"]}}

    mono_ranks: list[int] = []
    cross_ranks: list[int] = []
    mono_latency: list[float] = []
    cross_latency: list[float] = []
    mono_sizes: list[int] = []
    for block in picked:
        terms = distinctive_terms(block.source_text)
        if len(terms) < 2:
            continue
        query = " ".join(terms)
        begin = time.perf_counter()
        order = tf_retrieval.retrieve(merged, query, "en", k=8)
        mono_latency.append((time.perf_counter() - begin) * 1000)
        mono_sizes.append(len(order))
        mono_ranks.append(order.index(block.block_id) if block.block_id in order else -1)

        if translation:
            entry = translation.entries.get(block.block_id)
            translated = (entry.text if entry else "") or ""
            cjk_terms = distinctive_terms(translated)
            if len(cjk_terms) >= 2:
                begin = time.perf_counter()
                order = tf_retrieval.retrieve(merged, " ".join(cjk_terms), "zh", k=8)
                cross_latency.append((time.perf_counter() - begin) * 1000)
                cross_ranks.append(order.index(block.block_id) if block.block_id in order else -1)

    units = len(merged["units"])
    return {
        "book": project.name,
        "blocks": len(document.blocks),
        "sections": len(sections),
        "units_indexed": units,
        "translation": bool(translation),
        "build_ms": round(build_ms, 1),
        "monolingual": summarise(mono_ranks, 8),
        "cross_language": summarise(cross_ranks, 8),
        "latency_ms": {
            "mono_median": round(statistics.median(mono_latency), 1) if mono_latency else None,
            "mono_p95": round(sorted(mono_latency)[int(len(mono_latency) * 0.95)] if mono_latency else 0, 1),
            "cross_median": round(statistics.median(cross_latency), 1) if cross_latency else None,
        },
        "avg_candidates_returned": round(statistics.mean(mono_sizes), 2) if mono_sizes else 0,
        "embedding_model_loaded": False,
        "notes": "词面检索 = 上游 textfold/notes.py retrieve()；不加载任何模型，不下载任何权重。",
    }


# 人工撰写的**非逐字**问题：先读段落再用自己的话提问（不复用该段特色词），
# 用来暴露「词面/已译文回检 1.00」掩盖不了的真实语义漏检。期望段落可以多个（同意图的不同段）。
HANDWRITTEN = [
    {"q": "How does the Mao commentary tie poetry to political order, and what happens to the poet's own intent?",
     "expect": ["blk-p000177-0001"], "lang": "en", "note": "改写：原文用 odes/zhi/pericopes"},
    {"q": "Why was Confucius suspicious of words, and how did his followers cope with that?",
     "expect": ["blk-p000086-0001"], "lang": "en", "note": "改写：原文用 master/language/problem"},
    {"q": "How is the rise of the Ji family over the rulers of Lu justified?",
     "expect": ["blk-p000216-0002"], "lang": "en", "note": "改写：原文用 Heaven/doubles/pei er"},
    {"q": "Which minister left the details of government to others, and what did he tell his successor?",
     "expect": ["blk-p000341-0001"], "lang": "en", "note": "改写：原文用 Cao Shen/Huang-Lao"},
    {"q": "孔子为什么怀疑语言？", "expect": ["blk-p000086-0001"], "lang": "zh", "note": "跨语言改写"},
    {"q": "秦始皇的石刻怎样同时主张永恒与史无前例？", "expect": ["blk-p000043-0004"], "lang": "zh", "note": "跨语言改写"},
    {"q": "战国文献中描写君臣对话的两种立场是什么？", "expect": ["blk-p000032-0002"], "lang": "zh", "note": "跨语言改写"},
    {"q": "为什么要同时具备文与武的能力，这个标准如何被用来评价孔子？",
     "expect": ["blk-p000221-0001"], "lang": "zh", "note": "跨语言改写"},
    # 预期会漏的语义题：书里讲的是「天命/非命」与「先王之事」，问句用的是因果/正当性措辞。
    {"q": "墨子为什么认为相信命运会让人不作为？", "expect": ["blk-p000113-0004"], "lang": "zh",
     "note": "语义题：原文用 utility/ancient kings，措辞不重合"},
]


def evaluate_questions(book_pattern: str) -> dict:
    """逐条人工问题：期望段落是否进 top-8（双语词袋），以及命中的名次。"""
    project, document, translation = load_book(book_pattern)
    source_ladder = build_ladder_doc(document)
    display_ladder = (
        build_ladder_doc(document, locale="zh-CN", translation=translation) if translation else None
    )
    merged = {"units": {}, "atoms": {}}
    for block_id, unit in source_ladder["units"].items():
        merged["units"][block_id] = dict(unit)
    if display_ladder:
        for block_id, unit in display_ladder["units"].items():
            if block_id in merged["units"]:
                merged["units"][block_id]["tr"] = {"zh": {"toks": unit["toks"]}}
    rows = []
    for item in HANDWRITTEN:
        order = tf_retrieval.retrieve(merged, item["q"], "zh" if item["lang"] == "zh" else "en", k=8)
        hit = next((rank for rank, block_id in enumerate(order) if block_id in item["expect"]), -1)
        rows.append({
            "question": item["q"], "lang": item["lang"], "note": item["note"],
            "expect": item["expect"], "top8": order, "hit_rank": hit,
            "pass": hit >= 0,
        })
    return {
        "book": project.name,
        "n": len(rows),
        "hit_at_8": round(sum(1 for row in rows if row["pass"]) / len(rows), 3),
        "rows": rows,
        "note": "人工改写问题，不是从目标段抽词；用于暴露语义漏检，不作为召回率背书。",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book", default="Writing and Authority", help="library 下的书名子串")
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--out", default="tmp/reader-retrieval-eval.json")
    parser.add_argument("--questions", action="store_true", help="跑人工撰写的非逐字问题集")
    args = parser.parse_args()
    if args.questions:
        result = evaluate_questions(args.book)
        out = REPO / "tmp/reader-retrieval-questions.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        for row in result["rows"]:
            print(f'{"HIT " if row["pass"] else "MISS"} {row["lang"]} {row["question"][:60]}')
        print(json.dumps({key: result[key] for key in ("n", "hit_at_8", "note")}, ensure_ascii=False))
        return 0
    result = evaluate(args.book, args.samples)
    out = REPO / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
