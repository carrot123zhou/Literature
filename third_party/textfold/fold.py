"""A document as the TextFold format (docs/format.md): its sections and units at the five levels, each level marked as
the author's words or a model's, every written line anchored to the sentences it rests on, with where it came from and
what wrote it. And the same document as Markdown at one level, for a person or an agent to read: the whole map first,
then the sections that matter, deeper (`textfold get <link> --level key --section 3`).

Made from the stored layers alone, never by a model call; a document whose levels are not written yet gets the local
ones (the author's own first sentences), and says so."""
import time

from . import __version__
from .tokens import plain

LEVELS = ["topic", "takeaway", "key", "brief", "full"]   # coarsest first, as a reader zooms in
WRITTEN = {"topic": True, "takeaway": True, "key": True, "brief": False, "full": False}
KINDS = {"para": "paragraph", "item": "list item", "caption": "caption", "abstract": "abstract"}


def _text(toks, atoms):
    return plain(toks, atoms) if toks else ""


def _view(u, lang):
    """A unit's text and levels in a language: its translation's when there is one, else the original's."""
    z = (u.get("tr") or {}).get(lang) if lang != "en" else None
    if z:
        return z["toks"], z["sents"], [s for s in u["lad"].get("ev", []) if s < len(z["sents"])], z["lad"]
    return u["toks"], u["sents"], u["lad"].get("ev", []), u["lad"]


def unit(doc, uid, lang="en"):
    A = doc["atoms"]
    u = doc["units"][uid]
    toks, sents, ev, lad = _view(u, lang)
    sentences = [_text(toks[a:b], A) for a, b in sents]
    brief, tn = lad.get("brief") or [], lad.get("tn") or 0
    return {
        "id": uid, "kind": KINDS.get(u.get("role"), "paragraph"), "role": lad.get("role") or u.get("role"),
        "full": " ".join(sentences), "sentences": sentences,
        "brief": [sentences[s] for s in ev if s < len(sentences)], "evidence": [s for s in ev if s < len(sentences)],
        "key": _text(brief, A), "takeaway": _text(brief[:tn], A), "topic": _text(lad.get("topic") or [], A),
    }


def section(doc, c, lang="en"):
    A = doc["atoms"]
    lad = ((c.get("tr") or {}).get(lang) or {}).get("lad") if lang != "en" else None
    lad = lad or c.get("lad") or {}
    take = lad.get("take") or []
    title = ((c.get("tr") or {}).get(lang) or {}).get("title") if lang != "en" else None
    return {"id": c["id"], "num": c.get("num") or "", "title": title or c.get("title") or "", "depth": max(1, int(c.get("level") or 2) - 1),
            "takeaway": _text(take, A), "topic": _text([take[i] for i in lad.get("topic") or [] if i < len(take)], A),
            "units": [unit(doc, uid, lang) for uid in c["units"] if uid in doc["units"]]}


def document(pid, lang="en"):
    """The whole document in the TextFold format, version 0.

    LITERATURE PATCH: upstream reads the assembled doc from TextFold's own store
    (`store.assemble`). Literature has no such store: its doc comes from the canonical
    `document.json` + translation layer, assembled by `literature/textfold_adapter.py`,
    which builds the same shape and calls `section()` below. This entry point is therefore
    not usable here and says so instead of importing a store that does not exist.
    """
    raise NotImplementedError(
        "fold.document() 需要 TextFold 自身的 store；Literature 用 textfold_adapter.build_format_doc()。"
    )


def _upstream_document(pid, lang="en"):
    """Upstream body kept for reference/diffing against textfold/fold.py."""
    meta = doc["meta"]
    en = _store().read(pid, "en")
    tr = _store().read(pid, f"tr-{lang}") if lang != "en" else None
    written_by = (tr or en or {}).get("model") or ""
    return {
        "format": "textfold/doc", "version": 0, "id": pid, "lang": lang if (lang == "en" or tr) else "en",
        "title": (meta.get("titles") or {}).get(lang) or meta.get("title", ""), "authors": meta.get("authors", []),
        "date": meta.get("date", ""),
        "source": {"kind": (meta.get("source") or {}).get("kind") or ("arxiv" if _store().ARXIV_ID.fullmatch(pid) else "paper"),
                   "url": meta.get("abs_url", ""), "snapshot": meta.get("snapshot", ""),
                   "label": meta.get("label") or (f"arXiv:{pid}{meta.get('version') or ''}" if _store().ARXIV_ID.fullmatch(pid) else ""),
                   "license": meta.get("license", "")},
        "levels": LEVELS, "written": [lv for lv in LEVELS if WRITTEN[lv]],
        "provenance": {
            "made_with": f"TextFold {__version__}", "exported": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            # the written levels: a language model's (EU AI Act, art. 50(2): marked as generated, machine-readably)
            "ai_generated": bool(written_by), "model": written_by,
            "levels_made": (tr or en or {}).get("version", ""),
            "local": not en,   # no model has written its levels yet: the written ones are the author's first sentences
        },
        "sections": [section(doc, c, lang) for c in doc["chunks"]],
        "links": [{"from": e["f"], "to": e["t"], "rel": e["rel"]} for e in doc.get("edges", []) if e.get("tk") == "unit"],
    }


def _store():  # LITERATURE PATCH: the upstream store is not vendored; see document().
    raise NotImplementedError("TextFold store is not vendored in Literature.")


def _pick(sections, which):
    """A section by its number ("3.2"), its id ("S3") or its place in the list (1 is the first)."""
    for s in sections:
        if which in (s["num"], s["id"]):
            return s
    if which.isdigit() and 0 < int(which) <= len(sections):
        return sections[int(which) - 1]
    raise ValueError(f"no section {which}")


def markdown(d, level="topic", which=None):
    """The document at one level, as Markdown. Its sections carry their ids, so a reader (or an agent) asks for the
    next level of just the one it needs."""
    if level not in LEVELS:
        raise ValueError(f"level is one of {', '.join(LEVELS)}")
    out = [f"# {d['title']}", ""]
    src = d["source"]
    byline = " · ".join(x for x in ["; ".join(d["authors"][:4]) + (" et al." if len(d["authors"]) > 4 else ""), src["label"], src["url"]] if x)
    if byline:
        out += [byline, ""]
    who = d["provenance"]["model"] or "the author's first sentences (no model yet)"
    out += [f"*Level: {level}. " + (f"Written by {who}; each line rests on the sentences marked in the full text.*" if WRITTEN[level]
                                    else "The author's own words.*"), ""]
    sections = [_pick(d["sections"], which)] if which else d["sections"]
    for s in sections:
        head = f"{s['num']} {s['title']}".strip() or "(untitled)"
        out.append(f"{'#' * min(6, 1 + s['depth'])} {head}  `[{s['id']}]`")
        if level == "topic":
            out.append(f"**{s['topic'] or s['takeaway']}**" if (s["topic"] or s["takeaway"]) else "")
            seen, topics = set(), []
            for u in s["units"]:   # each subject once (a section names its subject in several units)
                if u["topic"] and u["topic"].lower() not in seen and u["topic"].lower() != (s["topic"] or "").lower():
                    seen.add(u["topic"].lower())
                    topics.append(u["topic"])
            if topics:
                out.append("; ".join(topics))
        elif level == "takeaway":
            if s["takeaway"]:
                out.append(f"**{s['takeaway']}**")
            out += [f"- {u['takeaway']}" for u in s["units"] if u["takeaway"]]
        else:
            if s["takeaway"] and level == "key":
                out.append(f"**{s['takeaway']}**")
            for u in s["units"]:
                text = u["key"] if level == "key" else " … ".join(u["brief"]) if level == "brief" else u["full"]
                if not text:
                    continue
                out.append("")
                out.append(f"- {text}" if u["kind"] == "list item" else f"*{text}*" if u["kind"] == "caption" else text)
        out.append("")
    if not which and level != "full":
        out.append(f"*Deeper: the same command with --level {LEVELS[LEVELS.index(level) + 1]} --section <id>.*")
    return "\n".join(out).rstrip() + "\n"
