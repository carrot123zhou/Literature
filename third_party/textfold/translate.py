"""A paper in another language, built on the English ladder.

Vendored from `textfold/translate.py` at commit 2182d9a8 (upstream blob `b4309fd0`).
Upstream verbatim: `SCHEMA`, `marked()`, `prompt_for()`, `parts()`, `unmark()`,
`assemble()`, `worded()`, `gaps()`, `_again()`.

Not vendored (recorded, not silent): `_ask()`, `build()`, `fill()`, `_titles()` and the
`llm` transport import. Upstream's orchestration makes the model calls itself through
`llm.call`; Literature makes them through `literature/providers.py` (Keychain, plaintext-
HTTP consent, budget coordinator, usage ledger), so the calling loops live in
`literature/reader.py` / `literature/textfold_adapter.py` and only the per-section
primitives above are reused here. The `concurrent.futures` import is likewise dropped:
Literature drives sections serially by default (see READER_BACKEND.md §fold).

LITERATURE ADAPTATION (recorded in literature/textfold_adapter.py): upstream `assemble()`
builds the displayed translation from the model's own `sentences` array, because in
TextFold the translation layer IS the model's output. Literature's display source of
truth is the user's `translations/<locale>/translation.json` (human-edited translations
must never be overwritten), so the translated-layer fold keeps the stored translation as
Full/Key and only rewrites the short levels (`takeaway`/`reason`/`topic`) from the reply;
the evidence indices are inherited from the source ladder. That adapted assemble lives in
`textfold_adapter.assemble_translated()`; upstream `assemble()` above is kept for
reference and for future sentence-aligned projects.
"""

from . import kinds, langs
from .ladder import KIND, align, clean, outline, section_ladder, topic_tokens, written
from .tokens import plain

SCHEMA = {
    "type": "object",
    "properties": {
        "paper_title": {"type": "string"},
        "section": {"type": "object", "properties": {"title": {"type": "string"}, "takeaway": {"type": "string"},
                                                      "topic": {"type": "string"}}, "required": ["title", "takeaway", "topic"]},
        "units": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "sentences": {"type": "array", "items": {"type": "string"}},
            "takeaway": {"type": "string"}, "reason": {"type": "string"}, "topic": {"type": "string"}},
            "required": ["id", "sentences", "takeaway", "reason", "topic"]}},
    },
    "required": ["section", "units"],
}


def marked(toks, atoms):
    """A token run for translation: every atom as a marker ⟦a12 …⟧ that shows what it is."""
    s = ""
    for k, tk in enumerate(toks):
        if "a" in tk:
            at = atoms[tk["a"]]
            inner = f"${at.get('alt', '')}$" if at["kind"] == "math" else (at.get("text") or "").strip()
            piece = f"⟦{tk['a']}{(' ' + inner) if inner else ''}⟧"
        else:
            piece = tk["t"]
        s += piece + (" " if tk.get("s") and k + 1 < len(toks) else "")
    return s.strip()


def prompt_for(doc, chunk, lang):
    U, A = doc["units"], doc["atoms"]
    L, b = langs.name(lang), langs.budget(lang)
    unit, acro = b["unit"], " (an English acronym counts as one)" if langs.cjk(lang) else ""
    blocks = []
    for n, uid in enumerate(chunk["units"]):
        u, lad = U[uid], U[uid]["lad"]
        kind = KIND.get(u["role"], "paragraph") + (f" inside {u['env']['title']}" if u.get("env") else "")
        lines = [f"[U{n}] {kind}"] + [f"  {i}: {marked(u['toks'][s:e], A)}" for i, (s, e) in enumerate(u["sents"])]
        reason = plain(lad["brief"][lad["tn"] + 1:-1], A) if len(lad["brief"]) > lad["tn"] + 1 else ""
        lines += [f"  English takeaway: {plain(lad['brief'][:lad['tn']], A)}", f"  English reason: {reason}",
                  f"  English topic: {plain(lad['topic'], A)}"]
        blocks.append("\n".join(lines))
    sec = chunk.get("lad")
    head = f"{chunk['num']} {chunk['title']}".strip()
    title_rule = f"\n- paper_title: the paper's title in {L}." if chunk["id"] == "abstract" else ""
    return kinds.adapt(f"""Translate one section of a research paper into {L} for a zoomable reader, and write its short zoom levels in {L}.

Paper: {doc['meta']['title']}

Outline (▶ marks this section):
{outline(doc, chunk['id'])}

This section: {head}
English section takeaway: {plain(sec['take'], A) if sec else ''}

Each unit (a paragraph, list item or caption) lists its numbered English sentences, then the English short levels already written for it.

{chr(10).join(blocks)}

Rules:
- sentences: translate EVERY numbered sentence into fluent, precise academic {L}: exactly one {L} sentence per English sentence, same count and order, because the reader shows them sentence by sentence. Never merge, split or drop a sentence; a sentence fragment stays a fragment.
- Markers ⟦…⟧ stand for formulas, citations and cross-references. Copy each marker exactly once and unchanged (including what is inside it) where it belongs in the {L} sentence. Do not write math of your own in the sentences.
- Terminology: keep established acronyms and proper names as the field writes them in {L} (usually unchanged, e.g. SNN, MLP, CartPole); translate other technical terms with the standard {L} terms, the same way throughout.
- takeaway: the English takeaway as natural {L} of at most {b['takeaway']} {unit}, conclusion first, not a word-by-word translation.
- reason: the English reason as natural {L} of at most {b['reason']} {unit}; "" when the English reason is empty.
- topic: at most {b['topic']} {unit}{acro}, copied VERBATIM from inside the {L} takeaway.
- section: title (the section title in {L}, without its number), takeaway (at most {b['section']} {unit}), topic (at most {b['topic']} {unit}, verbatim from that takeaway).{title_rule}
- {langs.punctuation_rule(lang)} In takeaway and reason, math only by copying a marker.

Return JSON {{"section": {{"title": "...", "takeaway": "...", "topic": "..."}}, "units": [{{"id": "U0", "sentences": ["...", ...], "takeaway": "...", "reason": "...", "topic": "..."}}, ...]}} with exactly one entry per unit, in order.""", doc["meta"])


PART_CHARS = 5000


def parts(doc, chunk):
    """A section's units in as few runs as keep each near PART_CHARS of English, the runs of about equal length."""
    U, A = doc["units"], doc["atoms"]
    sizes = [len(plain(U[uid]["toks"], A)) for uid in chunk["units"]]
    k = max(1, -(-sum(sizes) // PART_CHARS))
    if k == 1:
        return [list(chunk["units"])]
    target, out, run, acc = sum(sizes) / k, [], [], 0
    for uid, n in zip(chunk["units"], sizes):
        if run and len(out) < k - 1 and acc + n / 2 > target * (len(out) + 1):
            out.append(run)
            run = []
        run.append(uid)
        acc += n
    return out + [run]


def unmark(text, atoms):
    """A title is shown as text, not tokens: a marker the model carried into it becomes $latex$ (or its text) again."""
    def put(m):
        at = atoms.get(m.group(1), {})
        return f"${at['alt']}$" if at.get("kind") == "math" and at.get("alt") else (at.get("text") or "")
    return re.sub(r"⟦(a\d+)[^⟧]*⟧", put, text or "")


def assemble(u, o, atoms, lang, warn):
    zs = [z.strip() for z in (o.get("sentences") or [])]
    n = len(u["sents"])
    if len(zs) != n:
        warn(f"{u['id']}: {len(zs)} translated sentences for {n}")
        zs = (zs + [""] * n)[:n] if len(zs) < n else zs[:n - 1] + [" ".join(zs[n - 1:])]
    toks, sents = [], []
    for z in zs:
        start = len(toks)
        toks += written(z, u, atoms, [])
        if toks and not langs.cjk(lang):  # sentences of a spaced language are separated by a space
            toks[-1]["s"] = 1
        sents.append([start, len(toks)])
    if toks:
        toks[-1].pop("s", None)
    missing = {tk["a"] for tk in u["toks"] if "a" in tk} - {tk["a"] for tk in toks if "a" in tk}
    if missing:
        warn(f"{u['id']}: {len(missing)} formula/citation markers lost in translation")
    ev = [s for s in u["lad"]["ev"] if s < len(sents)] or [0]
    # (a line the model translated without a takeaway, a list of keywords: its first sentence stands in, as a paragraph
    # without a model's levels has its own)
    take_text, reason_text = clean(o.get("takeaway")) or next((z for z in zs if z), ""), clean(o.get("reason"))
    if not take_text or not toks:
        return None
    stop = langs.stop(lang)
    take = written(take_text, u, atoms, ev, toks, sents)
    brief = [dict(tk) for tk in take]
    if reason_text:
        brief.append({"t": stop} if langs.cjk(lang) else {"t": stop, "s": 1})
        brief += written(reason_text, u, atoms, ev, toks, sents)
    brief.append({"t": stop})
    tn = len(take)
    align(u, brief, ev, atoms, toks, sents)
    topic = topic_tokens(clean(o.get("topic")), brief, tn, u, atoms, ev, langs.budget(lang)["topic"], toks, sents)
    return {"toks": toks, "sents": sents, "lad": {"brief": brief, "tn": tn, "topic": topic}}


def worded(u):
    """A unit with words of its own to translate: not a formula or a citation alone, nor a letter left over from a
    figure ("D")."""
    return len(re.findall(r"\w", "".join(tk.get("t", "") for tk in u["toks"] if "a" not in tk))) > 1


def gaps(doc, lang):
    """The paragraphs of the sections that have no translation in lang: a part whose call failed (a timeout, a rate
    limit, a reply that was not JSON), a paragraph the model left out of its reply."""
    U = doc["units"]
    return [uid for c in doc["chunks"] for uid in c["units"] if lang not in U[uid].get("tr", {}) and worded(U[uid])]


def _again(doc, lang):
    """The gaps as parts to ask: by section, a section's missing paragraphs in as few runs as fit PART_CHARS."""
    missing = set(gaps(doc, lang))
    tasks = []
    for c in doc["chunks"]:
        us = [uid for uid in c["units"] if uid in missing]
        if us:
            tasks += [(c, k, run) for k, run in enumerate(parts(doc, {**c, "units": us}))]
    return tasks
