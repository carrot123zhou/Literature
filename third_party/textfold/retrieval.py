"""TextFold's question retrieval and citation numbering, extracted from `textfold/notes.py`.

Upstream: https://github.com/chenxiachan/textfold at commit 2182d9a8 (`textfold/notes.py`).
Extracted (not rewritten) pieces, kept semantically identical:

- `STOP`, `CJK_STOP`            — the words / CJK character pairs that say nothing about *where* an answer is
- `_terms()`                    — what a question can be matched on: Latin words (symbols like D=4 kept) and
                                  pairs of CJK characters
- `retrieve()`                  — IDF-weighted (`log(n/df)`) paragraph ranking over the authors' text and, when the
                                  reader's language is another, over that translation too
- `_cited()` / `CITE`           — the answer's `[[12]]` markers turned into paragraph ids; `[[?]]` stays (the model's
                                  own inference), an unknown number becomes `[[!12]]` instead of vanishing

Why this module matters for the embedding question: TextFold ships reading Q&A **without any embedding model or
vector store**. It ranks paragraphs by shared telling words (CJK by character pairs) and gives the top k to the
model. Literature reuses that path, so no model download is needed for reading Q&A.

Deliberate adaptation (recorded here, not silently):
- `retrieve()` takes the merged per-unit photo of both languages; Literature builds one ladder doc per locale, so
  `tr` is filled by `literature/textfold_adapter.merge_translations()` instead of upstream's inline `u["tr"]`.
- `_cited()` returns `(answer, uids)`; Literature renders ids as `[#block_id]` for the UI after this mapping.
"""

import math
import re

from .tokens import plain

STOP = set("the a an of and or to in on for with by at from as is are was were be been this that these those it its we our "
           "their they which what who how why when where does do did can could would should may might not no than then "
           "there here into about over under also such only more most less very mean means meaning".split())
# the pairs of characters a question is asked with, in Chinese, Japanese and Korean (what, why, how, meaning, this...):
# they say nothing about where the answer is, and would find any paragraph that happens to contain them
CJK_STOP = set("什么 是什 么意 意思 么是 怎么 怎样 为什 如何 哪些 哪个 这个 那个 这里 这些 那些 是否 能否 可以 一下 解释 请问 "
               "指的 是指 含义 為什 甚麼 這個 這裡 とは 何で 何を どう どう いう 意味 です すか って 무엇 의미 뭐야 이것".split())

CITE = re.compile(r"\[\[\s*([^\[\]]+?)\s*\]\]")


def _terms(text):
    """The words a question can be matched on: Latin words and symbols (D=4 stays D=4), and pairs of CJK characters."""
    low = (text or "").lower()
    latin = [w for w in re.findall(r"[a-z][a-z0-9]*(?:[=<>≤≥]\d+)?|\d+(?:\.\d+)?", low) if w not in STOP and len(w) > 1]
    cjk = re.findall(r"[぀-ヿ㐀-鿿가-힯]{2,}", text or "")
    return latin + [b for w in cjk for i in range(len(w) - 1) if (b := w[i:i + 2]) not in CJK_STOP]


def _text(unit, atoms, lang):
    zone = (unit.get("tr") or {}).get(lang) if lang != "en" else None
    return plain(zone["toks"] if zone else unit["toks"], atoms)


def retrieve(doc, query, lang, skip=(), k=5):
    """The paragraphs anywhere in the paper that share the most telling words with the question and the selection
    (in the authors' text, and in the reader's language when it is another), best first."""
    units, atoms = doc["units"], doc["atoms"]
    bags = {
        uid: set(_terms(plain(u["toks"], atoms) + (" " + _text(u, atoms, lang) if lang != "en" else "")))
        for uid, u in units.items()
    }
    df = {}
    for bag in bags.values():
        for word in bag:
            df[word] = df.get(word, 0) + 1
    q, n = set(_terms(query)), len(units)
    score = {uid: sum(math.log(n / df[word]) for word in q if word in bag) for uid, bag in bags.items() if uid not in skip}
    return [uid for uid, sc in sorted(score.items(), key=lambda x: -x[1]) if sc > 0][:k]


def _cited(answer, nums, units):
    """The answer's inline citations as paragraph ids: [[12]] -> [[S3.p2.1]]. [[?]] (the model's own inference) stays;
    a number that names no paragraph becomes [[!12]], which the reader shows as a citation that went wrong rather than
    dropping it. Returns the answer and the paragraphs it cites, in order."""
    cites = []

    def one(value):
        value = value.strip().lstrip("#")
        if value == "?":
            return "[[?]]"
        uid = nums.get(value) or (value if value in units else None)
        if not uid:
            return f"[[!{value}]]"
        if uid not in cites:
            cites.append(uid)
        return f"[[{uid}]]"

    answer = CITE.sub(
        lambda match: "".join(one(item) for item in re.split(r"[,;，；]\s*", match.group(1)) if item.strip()),
        answer,
    )
    return answer, cites
