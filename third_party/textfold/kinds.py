"""What a document is, for the words used about it. A research paper (arXiv, Europe PMC, bioRxiv, medRxiv) is a
paper; anything else (a web page, a Wikipedia article, a README, a PDF, a Markdown file) is a text, which may be a
letter or a report as well as an article. The prompts were written for papers: a paper's stay word for word what they
were (and so do their cached answers); a text's are the same prompts with the paper's words exchanged for a text's."""
import re

PAPERS = {"europepmc", "biorxiv", "medrxiv"}

# the paper's words in the prompts, and a text's, most specific first
TEXT = [
    ("sections of research papers", "sections of documents of every kind (articles, web pages, READMEs, reports, letters)"),
    ("understand a research paper", "understand a text (an article, a web page, a README, a report, a letter)"),
    ("Paper abstract, for context:", "Its opening, for context:"),
    ("Paper: ", "Text: "),
    ("a research paper", "a text"),
    ("research paper", "text"),
    ("fluent, precise academic", "fluent, precise"),
    ("the paper title itself", "the title itself"),
    ("titles of the paper", "titles of the text"),
    ("the paper's title", "the text's title"),
    ("the paper's own text", "the text's own words"),
    ("\nAbstract: ", "\nOpening: "),
    ("the paper's", "the text's"),
    ("the whole paper", "the whole text"),
    ("this paper", "this text"),
    ("of the paper", "of the text"),
    ("in the paper", "in the text"),
    ("from the paper", "from the text"),
    ("The paper,", "The text,"),
    ("the paper", "the text"),
    ("the authors' text", "the original text"),
    ("the authors' own", "the author's own"),
    ("the authors'", "the author's"),
    ("The paper", "The text"),
]


def is_paper(meta):
    kind = (meta.get("source") or {}).get("kind")
    return kind in PAPERS or (not kind and bool(re.fullmatch(r"\d{4}\.\d{4,5}", meta.get("id") or "")))


def adapt(prompt, meta):
    """A prompt written for a paper, as it is for a paper, and in a text's words for anything else."""
    if is_paper(meta):
        return prompt
    for a, b in TEXT:
        prompt = prompt.replace(a, b)
    return prompt
