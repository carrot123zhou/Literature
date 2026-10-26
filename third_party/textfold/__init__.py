"""Vendored TextFold core (Apache-2.0, Copyright 2026 Xia Chen).

Pinned upstream: https://github.com/chenxiachan/textfold at commit
`2182d9a8662e69790850021dd8c965d0398777c4` (2026-10-24, app version 0.10.31).

Vendored modules, all taken byte-for-byte from `textfold/` at that commit:

- `tokens.py`        tokenizer, sentence ranges, word normalisation, LCS alignment
- `kinds.py`         "paper" vs "text" wording adaptation for prompts
- `ladder.py`        the fold ladder: prompt, assemble, local fold, CJK rules, section ladder
- `fold.py`          projection to the `textfold/doc` format (units, sections, Markdown)
- `langs.py`         target-language names, per-script zoom budgets, punctuation rules
- `translate.py`     the source-ladder → translation-ladder path: per-section prompt,
                     parts, reply assemble (see the module docstring for the recorded patches)

Two deliberate patches, both recorded in `VENDOR.md`:

- `llm.py` is a Literature shim: the prompt strings (`SYSTEM`, `system_for`) and the
  reply decoders (`as_obj`, `units_of`, `Stopped`) are upstream verbatim, while the
  transport (`call`, `label`, `forget`) is not vendored, because Literature makes model
  calls through `literature/providers.py` (Keychain, plaintext-HTTP consent, usage ledger,
  budget coordinator). The stubs raise instead of silently doing nothing.
- `fold.py` drops the upstream `store` import: `document()` needs TextFold's own store,
  which Literature does not have; Literature assembles the same doc shape in
  `literature/textfold_adapter.py`. `unit()`, `section()` and `markdown()` are untouched.

Do not edit these files by hand to change reader behaviour: adapt in
`literature/textfold_adapter.py` instead, so the diff against upstream stays reviewable.
"""

__version__ = "0.10.31+vendored"
__upstream_commit__ = "2182d9a8662e69790850021dd8c965d0398777c4"
