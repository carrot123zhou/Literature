#!/usr/bin/env python3
"""UI v3 legacy 瘦身执行器（保守、可证、可回滚）。

只处理 `styles.css` 与 `mineru.css`（两张历史大表）。删除条件必须同时满足：

  1. 规则的**每一个**逗号分隔选择器都在 `scripts/ui_audit.py` 的 `legacy_worklist`
     里被标为 `safe_delete`，且 `remaining` 为空；
  2. 该选择器的每一个已声明属性，其 `override_kind` 为 `exact`，
     **或**同一选择器文本在更晚载入的 CSS 文件里也有定义
     （同特异度 + 更晚载入 = 层叠上必然胜出，可数学证明）。

任一条件不满足 → 整条规则保留（绝不猜测）。因此本脚本的删除量是
`safe_delete` 的一个**可证子集**，剩余的按 `--report` 输出为待人工判断项。

用法：
  python3 scripts/ui_slim_legacy.py --report          # 只统计，不改文件
  python3 scripts/ui_slim_legacy.py --apply           # 执行删除
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"

# main.tsx 里冻结的载入顺序；越靠后优先级越高
LOAD_ORDER = [
    "styles.css",
    "mineru.css",
    "resource.css",
    "review/review.css",
    "workbench/reading.css",
    "workbench/export.css",
    "theme.css",
    "base.css",
    "shell.css",
    "intake.css",
    "app-settings.css",
]
TARGETS = ["styles.css", "mineru.css"]


# --------------------------------------------------------------------------
# 极简 CSS 块解析器：只识别 `prelude { body }`，跳过注释与字符串；
# @media/@supports/@layer 递归，@keyframes 视为不可动的不透明块。
# 输出 (start, end, selector, is_leaf_rule) —— start/end 是整条规则的跨度。
# --------------------------------------------------------------------------
def _strip_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


def parse_rules(text: str, start: int = 0, end: int | None = None, out=None, in_atrule: bool = False):
    """递归解析；叶规则的 in_atrule=True 表示它位于 @media/@supports/@layer 内部。"""
    if out is None:
        out = []
    if end is None:
        end = len(text)
    i = start
    prelude_start = i
    while i < end:
        ch = text[i]
        if ch == "/" and i + 1 < end and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = end if j < 0 else j + 2
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            while i < end:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            continue
        if ch == ";":
            # at-statement（@import 等），忽略
            i += 1
            prelude_start = i
            continue
        if ch == "}":
            i += 1
            prelude_start = i
            continue
        if ch == "{":
            prelude = text[prelude_start:i]
            depth = 1
            j = i + 1
            while j < end and depth:
                c = text[j]
                if c == "/" and j + 1 < end and text[j + 1] == "*":
                    k = text.find("*/", j + 2)
                    j = end if k < 0 else k + 2
                    continue
                if c in "\"'":
                    quote = c
                    j += 1
                    while j < end:
                        if text[j] == "\\":
                            j += 2
                            continue
                        if text[j] == quote:
                            j += 1
                            break
                        j += 1
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            body_end = j  # 指向配对的 '}'
            block_end = j + 1
            name = _strip_comments(prelude).strip()
            if name.startswith("@") and not name.startswith("@keyframes") and not name.startswith("@font-face"):
                parse_rules(text, i + 1, body_end, out, in_atrule=True)
            elif not name.startswith("@"):
                out.append((prelude_start, block_end, name, in_atrule))
            i = block_end
            prelude_start = i
            continue
        i += 1
    return out


def split_selectors(selector: str):
    parts, depth, cur = [], 0, []
    for ch in selector:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def load_worklist() -> dict:
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ui_audit.py"), "--json", "/tmp/ui-slim-worklist.json"],
        capture_output=True, text=True, cwd=ROOT,
    )
    if out.returncode != 0:
        raise SystemExit("ui_audit.py 运行失败：\n" + out.stderr[-2000:])
    data = json.load(open("/tmp/ui-slim-worklist.json"))
    return data["details"]["legacy_worklist"]


def selectors_declared_later() -> set:
    """所有在 `styles.css` / `mineru.css` 之后的文件里出现过的选择器文本。"""
    seen = set()
    for rel in LOAD_ORDER[2:]:
        p = SRC / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for _, _, sel, _ in parse_rules(text):
            for s in split_selectors(sel):
                seen.add(re.sub(r"\s+", " ", _strip_comments(s)))
    return seen


def plan():
    worklist = load_worklist()
    later = selectors_declared_later()
    report = {"files": {}, "spans": {}}
    for rel in TARGETS:
        entries = {re.sub(r"\s+", " ", e["selector"]): e for e in worklist[rel]}
        text = (SRC / rel).read_text(encoding="utf-8")
        rules = parse_rules(text)
        delete_spans, stats = [], {"total": len(rules), "delete": 0, "keep": 0, "skip_missing": 0,
                                   "skip_not_safe": 0, "skip_non_exact": 0, "skip_in_atrule": 0}
        for start, end, sel, in_atrule in rules:
            sels = [_strip_comments(s).strip() for s in split_selectors(sel)]
            if in_atrule:
                # 断点/媒体上下文不同，覆盖判定不成立 —— 一律不删
                stats["skip_in_atrule"] += 1
                stats["keep"] += 1
                continue
            ok = True
            for s in sels:
                key = re.sub(r"\s+", " ", s)
                e = entries.get(key)
                if e is None:
                    stats["skip_missing"] += 1
                    ok = False
                    break
                if e["verdict"] != "safe_delete" or e.get("remaining"):
                    stats["skip_not_safe"] += 1
                    ok = False
                    break
                kinds = set((e.get("override_kind") or {}).values())
                if kinds - {"exact"} and key not in later:
                    stats["skip_non_exact"] += 1
                    ok = False
                    break
            if ok:
                delete_spans.append((start, end, sel))
                stats["delete"] += 1
            else:
                stats["keep"] += 1
        report["files"][rel] = stats
        report["spans"][rel] = delete_spans
    return report


def apply(report):
    for rel, spans in report["spans"].items():
        p = SRC / rel
        text = p.read_text(encoding="utf-8")
        keep = []
        last = 0
        for start, end, _sel in sorted(spans):
            keep.append(text[last:start])
            # 顺带吃掉规则后到换行为止的空白，避免留下空行
            nxt = end
            while nxt < len(text) and text[nxt] in " \t":
                nxt += 1
            if nxt >= len(text) or text[nxt] == "\n":
                last = nxt
            else:
                last = end
        keep.append(text[last:])
        p.write_text("".join(keep), encoding="utf-8")
        print(f"  已改写 {rel}: 删除 {len(spans)} 条规则")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    report = plan()
    total_del = 0
    for rel, st in report["files"].items():
        total_del += st["delete"]
        print(f"[{rel}] 规则总数 {st['total']}  可证删除 {st['delete']}  保留 {st['keep']}")
        print(f"    跳过原因：工作单缺失 {st['skip_missing']} / 非 safe_delete {st['skip_not_safe']}"
              f" / 含近似覆盖 {st['skip_non_exact']} / @media 内 {st['skip_in_atrule']}")
    print(f"合计可证删除 {total_del} 条规则")
    if args.apply:
        apply(report)
    else:
        print("（--report 模式，未改动文件）")


if __name__ == "__main__":
    main()
