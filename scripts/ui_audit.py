#!/usr/bin/env python3
"""Literature UI v3 — 确定性审计脚本（纯标准库，可重复运行）。

用途
----
把 `docs/UI_DESIGN.md` §3/§5 的硬约束变成可重复执行的检查，给出**机器可读摘要**
（JSON）和**人类可读清单**，作为收口轮的对照基线与「删除工单」。

只读：不会修改任何 `frontend/src` 文件。

用法
----
    python3 scripts/ui_audit.py                      # 打印人类可读报告
    python3 scripts/ui_audit.py --json out.json      # 同时写出机器可读摘要
    python3 scripts/ui_audit.py --format json        # 只打印 JSON
    python3 scripts/ui_audit.py --root /path/repo    # 指定仓库根目录
    python3 scripts/ui_audit.py --max-list 200       # 每类清单最多打印条数
    python3 scripts/ui_audit.py --worklist all       # legacy 工单连 keep 也打印

检查项
------
A.1 硬编码颜色：theme.css 之外的 hex / rgb() / hsl()
A.2 圆角：不属于 {6px,10px,14px,999px,50%}（滚动条圆角为预期保留）
A.3 字号：< 11px 的 font-size（含 font 简写）
A.4 间距：非 4pt 栅格（4/8/12/16/20/24/32/40/48）的 padding/margin/gap
A.5a 溢出：grid-template-columns 固定 px 列合计 ≥ 700px
A.5b 溢出：overflow:hidden 容器 —— 收紧启发式（见 B），区分
      truncation / rounded_clip / self_healing / clip_risk
A.6 层叠冲突：同一选择器同时出现在 styles/mineru 与覆盖层
A.7 死 CSS：class 选择器在任何 TSX 字面量里都不再出现（候选）
A.8 !important 数量与 prefers-reduced-motion（reduced-motion 内的 !important 为预期）
B. legacy 覆盖图 / 可删工单：styles.css 与 mineru.css 每条选择器的
   覆盖来源、完整/部分覆盖、结论（safe_delete / partial_delete / keep）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量：设计契约
# ---------------------------------------------------------------------------

BASE_FILES = ["styles.css", "mineru.css"]
OVERRIDE_FILES = [
    "shell.css",
    "intake.css",
    "app-settings.css",
    "resource.css",
    "review/review.css",
    "workbench/reading.css",
    "workbench/export.css",
]
LATER_LAYERS = [
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
TOKEN_FILE = "theme.css"
PRIMITIVE_FILE = "base.css"

ALL_CSS_FILES = BASE_FILES + OVERRIDE_FILES + [TOKEN_FILE, PRIMITIVE_FILE]

OWNER = {
    "theme.css": "Lead",
    "base.css": "Lead",
    "styles.css": "ui-shell",
    "shell.css": "ui-shell",
    "app-settings.css": "ui-shell",
    "intake.css": "ui-intake",
    "resource.css": "ui-intake",
    "review/review.css": "ui-review",
    "workbench/reading.css": "ui-reading",
    "workbench/export.css": "ui-reading",
    "mineru.css": "legacy (Lead 收口)",
}

RADIUS_ALLOWED = {"6px", "10px", "14px", "999px", "50%"}
SPACING_GRID = {4, 8, 12, 16, 20, 24, 32, 40, 48}
MIN_FONT_PX = 11.0
GRID_FIXED_SUM_LIMIT = 700

CONDITIONAL_AT = ("@media", "@supports", "@layer", "@container")

# Lead 已拍板的特例（在工单里显式归位）
HISTORY_OWNER = "app-settings.css"
STRUCTURAL_CLIP_SELECTORS = {".review-grid"}
STRUCTURAL_CLIP_PREFIXES = (".file-settings-head", ".file-settings-table article", ".file-settings-table")

HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9a-fA-F])")
FUNC_COLOR_RE = re.compile(r"\b(?:rgba?|hsla?|color-mix)\([^)]*\)")
PX_RE = re.compile(r"(-?\d+(?:\.\d+)?)px")
CLASS_RE = re.compile(r"\.([A-Za-z_][A-Za-z0-9_-]*)")
COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
IMPORTANT_RE = re.compile(r"!\s*important")
REDUCED_MOTION_RE = re.compile(r"prefers-reduced-motion")

SPACING_PROPS = {
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "gap", "row-gap", "column-gap",
}
# 判定「有界容器」的属性：这些容器一旦裁切，内部没有滚动就会被裁死
BOUND_PROPS = {"height", "max-height", "inset"}


def is_expected_important(selector: str, context: tuple) -> bool:
    """reduced-motion 兜底块里的 !important 属预期保留。"""
    return any(REDUCED_MOTION_RE.search(item) for item in context)


def is_expected_radius(selector: str) -> bool:
    """滚动条伪元素的圆角属预期保留。"""
    return "::-webkit-scrollbar" in selector


# ---------------------------------------------------------------------------
# CSS 解析
# ---------------------------------------------------------------------------

def strip_comments_preserving_lines(text: str) -> str:
    """把 /* */ 注释替换成等量空白（保留换行），保证行号不漂移。"""

    def repl(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return COMMENT_RE.sub(repl, text)


def _scan_blocks(css: str, base: int = 0):
    """按大括号配对产出顶层块 (prelude, body, prelude_abs, body_abs)。"""
    i = 0
    n = len(css)
    while i < n:
        j = css.find("{", i)
        if j < 0:
            break
        prelude = css[i:j]
        depth = 1
        k = j + 1
        while k < n:
            ch = css[k]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        body_abs = base + j + 1
        yield prelude, css[j + 1 : k], base + i, body_abs
        i = k + 1


def iter_rules(css: str, base: int = 0, context: tuple = ()):
    """递归展开条件块，产出 (selector, body, body_abs, context)。

    context 是该规则所处的 @media/@supports 条件文本元组，用于识别
    prefers-reduced-motion 等上下文。
    """
    for prelude, body, _pre_abs, body_abs in _scan_blocks(css, base):
        sel = " ".join(prelude.split())
        if not sel:
            continue
        if sel.startswith(CONDITIONAL_AT):
            yield from iter_rules(body, body_abs, context + (sel,))
        else:
            yield sel, body, body_abs, context


DECL_RE = re.compile(r"([-\w]+)\s*:\s*([^;]*)")
SELECTOR_SPLIT_RE = re.compile(r"\s*,\s*")


def declarations(body: str):
    for match in DECL_RE.finditer(body):
        yield match.group(1).lower(), match.group(2).strip()


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def split_compounds(selector: str):
    """把选择器拆成由组合子分隔的 compound 列表。"""
    return [part for part in re.split(r"\s*[>+~]\s*|\s+", selector.strip()) if part]


def simple_parts(compound: str):
    """拆一个 compound 里的简单选择器片段（.cls / #id / element / [attr] / :pseudo）。"""
    return re.findall(r"\.[A-Za-z0-9_-]+|#[A-Za-z0-9_-]+|[A-Za-z][A-Za-z0-9-]*|\[[^\]]*\]|::?[A-Za-z-]+(?:\([^)]*\))?", compound)


# ---------------------------------------------------------------------------
# 单文件扫描
# ---------------------------------------------------------------------------

def scan_css(path: Path, rel: str):
    raw = path.read_text(encoding="utf-8", errors="replace")
    css = strip_comments_preserving_lines(raw)
    result = {
        "file": rel,
        "owner": OWNER.get(rel, "?"),
        "lines": len(raw.splitlines()),
        "colors": [],
        "radii": [],
        "font_sizes": [],
        "spacings": [],
        "grids": [],
        "important": [],
        "selectors": {},          # selector -> 首次出现行号
        "selector_decls": {},     # selector -> {prop: {"value","line"}}
        "has_reduced_motion": bool(REDUCED_MOTION_RE.search(css)),
    }

    def add(bucket, line, selector, value, **extra):
        item = {"line": line, "selector": selector, "value": value}
        item.update(extra)
        result[bucket].append(item)

    # A.1 硬编码颜色
    for match in HEX_RE.finditer(css):
        add("colors", line_of(css, match.start()), "", match.group(0))
    for match in FUNC_COLOR_RE.finditer(css):
        add("colors", line_of(css, match.start()), "", match.group(0))

    for selector, body, body_abs, context in iter_rules(css):
        line = line_of(css, body_abs)
        decls = list(declarations(body))

        # 选择器与聚合声明（后出现者覆盖同名属性）
        for single in SELECTOR_SPLIT_RE.split(selector):
            single = " ".join(single.split())
            if not single:
                continue
            result["selectors"].setdefault(single, line)
            bucket = result["selector_decls"].setdefault(single, {})
            for name, value in decls:
                bucket[name] = {"value": value, "line": line}

        # A.8 !important（reduced-motion 块内标 expected）
        if IMPORTANT_RE.search(body):
            count = len(IMPORTANT_RE.findall(body))
            add(
                "important", line, selector, "!important",
                count=count, expected=is_expected_important(selector, context),
            )

        for name, value in decls:
            # A.2 圆角
            if name == "border-radius":
                if "var(" in value or "calc(" in value:
                    continue
                for token in re.split(r"[\s/]+", value):
                    token = token.strip()
                    if not token or token in ("0", "inherit", "unset", "revert"):
                        continue
                    if token in RADIUS_ALLOWED:
                        continue
                    add(
                        "radii", line, selector, token,
                        expected=is_expected_radius(selector),
                    )

            # A.3 字号
            if name in ("font-size", "font"):
                if "var(" in value or ("calc(" in value and name == "font-size"):
                    continue
                for token in PX_RE.finditer(value):
                    if float(token.group(1)) < MIN_FONT_PX:
                        add("font_sizes", line, selector, token.group(0))

            # A.4 间距
            if name in SPACING_PROPS:
                if "calc(" in value:
                    continue
                for token in PX_RE.finditer(value):
                    number = abs(float(token.group(1)))
                    if number == 0:
                        continue
                    if number.is_integer() and int(number) in SPACING_GRID:
                        continue
                    add("spacings", line, selector, token.group(0))

            # A.5a 固定 px 网格列合计
            if name == "grid-template-columns":
                total = sum(abs(float(m.group(1))) for m in PX_RE.finditer(value))
                if total >= GRID_FIXED_SUM_LIMIT:
                    add("grids", line, selector, f"{total:.0f}px :: {value}")

    return result


# ---------------------------------------------------------------------------
# 全局层叠（跨文件合并，按载入顺序后者覆盖）
# ---------------------------------------------------------------------------

def build_cascade(scans):
    """返回 (effective_decls, selector_files)。"""
    effective = {}
    files = defaultdict(list)
    for rel in ALL_CSS_FILES:
        scan = scans.get(rel)
        if not scan:
            continue
        for selector, props in scan["selector_decls"].items():
            files[selector].append(rel)
            bucket = effective.setdefault(selector, {})
            for prop, info in props.items():
                bucket[prop] = {**info, "file": rel}
    return effective, files


def overflow_state(decls):
    """返回 (is_hidden, hidden_x, hidden_y, vertical_scroll, reason)。"""
    def axes(value):
        return value

    base = decls.get("overflow", {}).get("value", "")
    x = decls.get("overflow-x", {}).get("value", base)
    y = decls.get("overflow-y", {}).get("value", base)

    def hidden(axis_value):
        return "hidden" in axis_value and "auto" not in axis_value and "scroll" not in axis_value

    hidden_x = hidden(x) if x else False
    hidden_y = hidden(y) if y else False
    vertical_scroll = bool(y) and ("auto" in y or "scroll" in y)
    return hidden_x or hidden_y, hidden_x, hidden_y, vertical_scroll


def classify_overflow(scans, effective, all_scroll_selectors, selector_files):
    """收紧后的 overflow:hidden 分类。

    返回 dict，含每一类的条目列表与计数。
    """
    truncation, rounded_clip, self_healing, indicator_clip, utility, clip_risk = [], [], [], [], [], []

    def first_class(selector: str):
        match = CLASS_RE.search(selector)
        return match.group(1) if match else ""

    def is_indicator(selector: str, decls) -> bool:
        if selector == "progress" or selector.endswith(("progress", "prog", "bar")):
            return True
        for prop in ("height", "max-height"):
            value = decls.get(prop, {}).get("value", "")
            px = PX_RE.search(value)
            if px and float(px.group(1)) <= 24:
                return True
        return False

    for selector, decls in effective.items():
        is_hidden, hidden_x, hidden_y, vertical_scroll = overflow_state(decls)
        if not is_hidden:
            continue

        line = min(info["line"] for info in decls.values())
        file = decls.get("overflow", decls.get("overflow-y", decls.get("overflow-x", {}))).get("file", "?")

        # 该选择器子树里是否存在显式滚动区（文本启发式 + BEM 子元素）
        block = first_class(selector)
        inner_scroll = []
        for other in all_scroll_selectors:
            if other == selector:
                continue
            matched = False
            for comb in (" ", " >", " +", " ~"):
                if other.startswith(selector + comb):
                    matched = True
                    break
            if not matched and block:
                other_block = first_class(other)
                if other_block.startswith(block + "__"):
                    matched = True
            if matched:
                inner_scroll.append(other)

        text_overflow = decls.get("text-overflow", {}).get("value", "")
        white_space = decls.get("white-space", {}).get("value", "")
        radius = decls.get("border-radius", {}).get("value", "")
        bounded = any(prop in decls for prop in BOUND_PROPS) or "fixed" in decls.get("position", {}).get("value", "")

        item = {
            "selector": selector,
            "file": file,
            "line": line,
            "hidden_axes": [axis for axis, flag in (("x", hidden_x), ("y", hidden_y)) if flag],
            "inner_scroll": inner_scroll,
            "overridden_by": [f for f in LATER_LAYERS if f in selector_files.get(selector, [])],
        }

        reasons = []
        if "ellipsis" in text_overflow or "nowrap" in white_space or "u-truncate" in selector:
            reasons.append("单行省略（text-overflow:ellipsis / white-space:nowrap）")
            truncation.append({**item, "reasons": reasons})
            continue
        if selector.startswith(".u-") and ("u-clip" in selector or "u-scroll" in selector):
            reasons.append("设计基元（u-clip / u-scroll，与滚动区配套使用）")
            utility.append({**item, "reasons": reasons})
            continue
        if is_indicator(selector, decls):
            reasons.append("进度条 / 指标条（固定矮高或 progress 元素，裁切为有意为之）")
            indicator_clip.append({**item, "reasons": reasons})
            continue
        if bounded and not inner_scroll and not vertical_scroll:
            reasons.append("有界容器（height/max-height/inset/position:fixed）且无内部滚动区")
            clip_risk.append({**item, "reasons": reasons})
            continue
        if radius and not inner_scroll and not vertical_scroll:
            reasons.append("仅圆角裁边（有 border-radius、无内部滚动、无高度约束）")
            rounded_clip.append({**item, "reasons": reasons})
            continue
        if vertical_scroll or inner_scroll:
            reasons.append("有垂直滚动兜底" if vertical_scroll else "子树存在滚动区")
            self_healing.append({**item, "reasons": reasons})
            continue
        reasons.append("既无高度约束也无内部滚动区")
        clip_risk.append({**item, "reasons": reasons})

    return {
        "truncation": truncation,
        "rounded_clip": rounded_clip,
        "self_healing": self_healing,
        "indicator_clip": indicator_clip,
        "utility": utility,
        "clip_risk": clip_risk,
    }


# ---------------------------------------------------------------------------
# legacy 覆盖图 / 可删工单
# ---------------------------------------------------------------------------

def selector_covered_by(selector, later_files, scans):
    """返回 {prop: (file, kind)}：selector 的每个属性被哪些后层规则覆盖。

    kind = exact（选择器文本相同）或 higher（后层某个 compound 包含本选择器的
    全部简单片段，即更高优先权）。只对「单 compound」的 legacy 选择器做 higher 判定。
    """
    covered = {}
    kinds = {}
    if not later_files:
        return covered, kinds
    for later in later_files:
        scan = scans.get(later)
        if not scan:
            continue
        # exact
        props = scan["selector_decls"].get(selector)
        if props:
            for prop in props:
                covered[prop] = later
                kinds[prop] = "exact"
    # higher-specificity（仅当 legacy 选择器是单 compound）
    compounds = split_compounds(selector)
    if len(compounds) == 1:
        want = set(simple_parts(compounds[0]))
        if want:
            for later in later_files:
                scan = scans.get(later)
                if not scan:
                    continue
                for cand, props in scan["selector_decls"].items():
                    if cand == selector:
                        continue
                    for comp in split_compounds(cand):
                        if compound_contains(comp, want) and len(simple_parts(comp)) > len(want):
                            for prop in props:
                                covered.setdefault(prop, later)
                                kinds.setdefault(prop, "higher")
                            break
    return covered, kinds


def compound_contains(compound: str, want_parts) -> bool:
    parts = set(simple_parts(compound))
    return want_parts.issubset(parts)


def build_legacy_worklist(scans, dead_classes):
    worklist = OrderedDict()
    summary = OrderedDict()
    for rel in BASE_FILES:
        scan = scans.get(rel)
        if not scan:
            continue
        later_files = [f for f in LATER_LAYERS if f in scans]
        items = []
        counts = defaultdict(int)
        for selector in sorted(scan["selector_decls"]):
            props = scan["selector_decls"][selector]
            declared = set(props)
            covered, kinds = selector_covered_by(selector, later_files, scans)
            remaining = sorted(p for p in declared if p not in covered)
            line = min(info["line"] for info in props.values())

            if not covered:
                verdict = "keep"
            elif not remaining:
                verdict = "safe_delete"
            else:
                verdict = "partial_delete"
            counts[verdict] += 1

            notes = []
            classes = set(CLASS_RE.findall(selector))
            if selector.startswith(".history-") and rel != HISTORY_OWNER:
                notes.append(f"Lead 已裁定：保留 {HISTORY_OWNER}，本份待删")
            if selector in STRUCTURAL_CLIP_SELECTORS or any(selector.startswith(p) for p in STRUCTURAL_CLIP_PREFIXES):
                notes.append("结构性裁切路径（必删/必改）")
            if classes and classes.issubset(dead_classes):
                notes.append("无 TSX 引用（可删）")

            items.append({
                "selector": selector,
                "line": line,
                "verdict": verdict,
                "declared": sorted(declared),
                "covered_by": {p: covered[p] for p in sorted(covered)},
                "override_kind": {p: kinds.get(p, "?") for p in sorted(covered)},
                "remaining": remaining,
                "notes": notes,
            })
        worklist[rel] = items
        summary[rel] = dict(counts)
    return worklist, summary


# ---------------------------------------------------------------------------
# TSX class 使用集合（死 CSS 反查）
# ---------------------------------------------------------------------------

TS_DOUBLE_RE = re.compile(r'"(?:[^"\\\n]|\\.)*"')
TS_SINGLE_RE = re.compile(r"'(?:[^'\\\n]|\\.)*'")
# 模板字符串可跨行，单独用 re.S；双/单引号字符串不允许跨行，避免一个落单引号吞掉整个文件。
TS_TEMPLATE_RE = re.compile(r"`(?:[^`\\]|\\.)*`", re.S)
TS_STRING_RES = (TS_DOUBLE_RE, TS_SINGLE_RE, TS_TEMPLATE_RE)
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
DYNAMIC_PREFIX_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\$\{")


def collect_tsx_class_tokens(src_dir: Path):
    """返回 (字面量 token 集合, 动态类名前缀集合, TSX 文件列表)。"""
    used = set()
    dynamic_prefixes = set()
    tsx_files = sorted(src_dir.rglob("*.tsx")) + sorted(src_dir.rglob("*.ts"))
    for path in tsx_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        text = BLOCK_COMMENT_RE.sub(" ", text)
        for pattern in TS_STRING_RES:
            for match in pattern.finditer(text):
                literal = match.group(0)
                for token in TOKEN_RE.finditer(literal):
                    used.add(token.group(0))
                for prefix in DYNAMIC_PREFIX_RE.finditer(literal):
                    dynamic_prefixes.add(prefix.group(1))
    return used, dynamic_prefixes, [str(p) for p in tsx_files]


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def build_summary(src_dir: Path):
    scans = {}
    for rel in ALL_CSS_FILES:
        path = src_dir / rel
        if path.exists():
            scans[rel] = scan_css(path, rel)

    effective, selector_files = build_cascade(scans)
    all_scroll_selectors = {s for s, d in effective.items() if overflow_state(d)[3]}
    overflow = classify_overflow(scans, effective, all_scroll_selectors, selector_files)

    # 颜色（theme.css 是唯一允许硬编码处，排除）
    color_by_file = OrderedDict()
    for rel, scan in scans.items():
        if rel == TOKEN_FILE:
            continue
        if scan["colors"]:
            color_by_file[rel] = len(scan["colors"])

    # A.2 / A.8 期望项
    radius_expected = sum(1 for s in scans.values() for i in s["radii"] if i.get("expected"))
    radius_violations = [
        {"file": rel, **i} for rel, s in scans.items() for i in s["radii"] if not i.get("expected")
    ]
    important_total = sum(len(s["important"]) for s in scans.values())
    important_expected = sum(i.get("count", 1) for s in scans.values() for i in s["important"] if i.get("expected"))

    # A.6 层叠冲突
    base_set, override_set = set(BASE_FILES), set(OVERRIDE_FILES)
    cascade_conflicts = []
    for selector in sorted(selector_files):
        files = set(selector_files[selector])
        if files & base_set and files & override_set:
            cascade_conflicts.append({"selector": selector, "files": sorted(files)})

    # A.7 死 CSS
    used_classes, dynamic_prefixes, tsx_files = collect_tsx_class_tokens(src_dir)
    css_classes = defaultdict(set)
    for rel, scan in scans.items():
        for selector in scan["selectors"]:
            for match in CLASS_RE.finditer(selector):
                css_classes[match.group(1)].add(rel)
    dead = []
    for name in sorted(css_classes):
        if name in used_classes:
            continue
        if any(name.startswith(prefix) for prefix in dynamic_prefixes):
            continue
        dead.append({"class": name, "files": sorted(css_classes[name])})
    dead_classes = {item["class"] for item in dead}

    # B. legacy 工单
    worklist, worklist_summary = build_legacy_worklist(scans, dead_classes)

    # 汇总数字
    all_radii = defaultdict(int)
    for item in radius_violations:
        all_radii[item["value"]] += 1
    all_font_tokens = defaultdict(int)
    for s in scans.values():
        for item in s["font_sizes"]:
            all_font_tokens[item["value"]] += 1
    all_spacing = defaultdict(int)
    for s in scans.values():
        for item in s["spacings"]:
            all_spacing[item["value"]] += 1

    hex_values = set()
    for rel, scan in scans.items():
        if rel == TOKEN_FILE:
            continue
        for item in scan["colors"]:
            if item["value"].startswith("#"):
                hex_values.add(item["value"].lower())

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "design_contract": "docs/UI_DESIGN.md",
        "stage": "1.5",
        "files": {rel: {"owner": scan["owner"], "lines": scan["lines"]} for rel, scan in scans.items()},
        "metrics": {
            "css_files": len(scans),
            "hardcoded_color_occurrences": sum(color_by_file.values()),
            "hardcoded_color_by_file": color_by_file,
            "distinct_hardcoded_hex": len(hex_values),
            "radius_violations": len(radius_violations),
            "radius_expected_scrollbar": radius_expected,
            "radius_violation_values": dict(sorted(all_radii.items())),
            "font_below_11px": sum(all_font_tokens.values()),
            "font_below_11px_values": dict(sorted(all_font_tokens.items())),
            "spacing_off_grid": sum(all_spacing.values()),
            "spacing_off_grid_values": dict(sorted(all_spacing.items(), key=lambda kv: -kv[1])[:40]),
            "grid_fixed_ge_700": sum(len(s["grids"]) for s in scans.values()),
            "overflow_hidden_total": sum(
                1 for d in effective.values() if overflow_state(d)[0]
            ),
            "overflow_truncation": len(overflow["truncation"]),
            "overflow_rounded_clip": len(overflow["rounded_clip"]),
            "overflow_self_healing": len(overflow["self_healing"]),
            "overflow_indicator_clip": len(overflow["indicator_clip"]),
            "overflow_utility": len(overflow["utility"]),
            "overflow_clip_risk": len(overflow["clip_risk"]),
            "cascade_conflict_selectors": len(cascade_conflicts),
            "dead_class_candidates": len(dead),
            "important": important_total,
            "important_expected_reduced_motion": important_expected,
            "prefers_reduced_motion_files": [rel for rel, s in scans.items() if s["has_reduced_motion"]],
        },
        "legacy_worklist_summary": worklist_summary,
        "details": {
            "colors": {rel: scans[rel]["colors"] for rel in color_by_file},
            "radii": [item for item in radius_violations],
            "font_sizes": {rel: scans[rel]["font_sizes"] for rel, s in scans.items() if s["font_sizes"]},
            "spacings": {rel: scans[rel]["spacings"] for rel, s in scans.items() if s["spacings"]},
            "grids": {rel: scans[rel]["grids"] for rel, s in scans.items() if s["grids"]},
            "overflow": overflow,
            "cascade_conflicts": cascade_conflicts,
            "dead_classes": dead,
            "important": {rel: scans[rel]["important"] for rel, s in scans.items() if s["important"]},
            "legacy_worklist": worklist,
        },
        "tsx_files_scanned": len(tsx_files),
        "tsx_dynamic_prefixes": sorted(dynamic_prefixes),
    }
    return summary


# ---------------------------------------------------------------------------
# 人类可读输出
# ---------------------------------------------------------------------------

def render_overflow(summary, max_list):
    out = []
    m = summary["metrics"]
    ov = summary["details"]["overflow"]
    out.append("【A.5b overflow:hidden 收紧判定】")
    out.append(
        f"  overflow:hidden 生效容器 {m['overflow_hidden_total']} 个 -> "
        f"truncation {m['overflow_truncation']} / rounded_clip {m['overflow_rounded_clip']} / "
        f"indicator_clip {m['overflow_indicator_clip']} / utility {m['overflow_utility']} / "
        f"self_healing {m['overflow_self_healing']} / **clip_risk {m['overflow_clip_risk']}**"
    )
    out.append("  -- clip_risk（仍可能裁内容，需人工/收口确认）--")
    for item in ov["clip_risk"][:max_list]:
        saved = item["inner_scroll"] or ["（无显式内部滚动区）"]
        out.append(f"      {item['file']}:L{item['line']}  {item['selector']}")
        out.append(f"          原因：{'；'.join(item['reasons'])}")
        out.append(f"          兜底：{', '.join(saved)}")
    return out


def render_worklist(summary, show_all, max_list):
    out = []
    out.append("【B. legacy 覆盖图 / 可删工单】styles.css + mineru.css")
    for rel, counts in summary["legacy_worklist_summary"].items():
        out.append(
            f"  {rel}: 可安全删除 {counts.get('safe_delete', 0)} / "
            f"部分可删 {counts.get('partial_delete', 0)} / 不可删 {counts.get('keep', 0)}"
        )
    for rel, items in summary["details"]["legacy_worklist"].items():
        actionable = [x for x in items if show_all or x["verdict"] != "keep"]
        out.append(f"  --- {rel}（{'全部' if show_all else '仅需处理'}：{len(actionable)}）---")
        for item in actionable[:max_list]:
            out.append(f"      [{item['verdict']}] {item['selector']}  (L{item['line']})")
            if item["covered_by"]:
                for prop, file in sorted(item["covered_by"].items()):
                    out.append(f"          覆盖 {prop} <- {file} ({item['override_kind'][prop]})")
            if item["remaining"]:
                out.append(f"          仍在生效：{', '.join(item['remaining'])}")
            for note in item["notes"]:
                out.append(f"          注：{note}")
    return out


def render_text(summary, max_list, worklist_all=False) -> str:
    out = []
    m = summary["metrics"]
    out.append("=" * 78)
    out.append(f"Literature UI v3 · 审计报告（stage {summary['stage']}）")
    out.append(f"生成时间：{summary['generated_at']}    契约：{summary['design_contract']}")
    out.append("=" * 78)

    out.append("")
    out.append("【A.1 硬编码颜色】theme.css 之外")
    out.append(f"  总计：{m['hardcoded_color_occurrences']} 处；不同 hex：{m['distinct_hardcoded_hex']} 个")
    for rel, count in m["hardcoded_color_by_file"].items():
        out.append(f"    {count:>4}  {rel}  ({OWNER.get(rel, '?')})")

    out.append("")
    out.append("【A.2 圆角】不属于 {6,10,14,999px,50%}（滚动条圆角已排除为预期）")
    out.append(
        f"  违规：{m['radius_violations']} 处；取值分布：{m['radius_violation_values'] or '无'}"
        f"；滚动条圆角预期保留 {m['radius_expected_scrollbar']} 处"
    )

    out.append("")
    out.append("【A.3 字号 < 11px】")
    out.append(f"  违规：{m['font_below_11px']} 处；取值分布：{m['font_below_11px_values'] or '无'}")

    out.append("")
    out.append("【A.4 间距非 4pt 栅格】")
    out.append(f"  违规：{m['spacing_off_grid']} 处（Top 取值）")
    for value, count in list(m["spacing_off_grid_values"].items())[:15]:
        out.append(f"    {count:>4}  {value}")

    out.append("")
    out.append("【A.5a grid 固定 px 列合计 ≥ 700px】")
    out.append(f"  命中：{m['grid_fixed_ge_700']} 条")
    for rel, items in summary["details"]["grids"].items():
        out.append(f"  - {rel} ({OWNER.get(rel, '?')})")
        for item in items[:max_list]:
            out.append(f"      L{item['line']}  {item['selector']}  ->  {item['value']}")

    out.append("")
    out.extend(render_overflow(summary, max_list))

    out.append("")
    out.append("【A.6 层叠冲突】同一选择器同时出现在 styles/mineru 与覆盖层")
    out.append(f"  冲突选择器：{m['cascade_conflict_selectors']} 个")
    for item in summary["details"]["cascade_conflicts"][:max_list]:
        out.append(f"      {item['selector']}  <-  {', '.join(item['files'])}")

    out.append("")
    out.append("【A.7 死 CSS 候选】class 在任何 TSX 字面量里都不出现")
    out.append(f"  候选：{m['dead_class_candidates']} 个（含动态类名误报，仅供人工判断）")
    for item in summary["details"]["dead_classes"][:max_list]:
        out.append(f"      .{item['class']}  <-  {', '.join(item['files'])}")

    out.append("")
    out.append("【A.8 !important / prefers-reduced-motion】")
    out.append(
        f"  !important：{m['important']} 处（其中 reduced-motion 兜底预期保留 "
        f"{m['important_expected_reduced_motion']} 处）"
    )
    for rel, items in summary["details"]["important"].items():
        out.append(f"      {rel}：{len(items)} 处  {['L%d' % i['line'] for i in items[:10]]}")
    out.append(f"  prefers-reduced-motion：{m['prefers_reduced_motion_files'] or '未发现'}")

    out.append("")
    out.extend(render_worklist(summary, worklist_all, max_list))

    out.append("")
    out.append(f"（清单每类最多显示 {max_list} 条；完整数据见 JSON 摘要。）")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Literature UI v3 确定性审计")
    parser.add_argument("--root", default=None, help="仓库根目录（默认脚本上级目录）")
    parser.add_argument("--json", dest="json_path", default=None, help="写出机器可读 JSON 摘要")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="stdout 输出格式")
    parser.add_argument("--max-list", type=int, default=80, help="每类清单最多打印条数")
    parser.add_argument("--worklist", choices=("actionable", "all"), default="actionable",
                        help="legacy 工单是否连 keep 一起打印")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[1]
    src_dir = root / "frontend" / "src"
    if not src_dir.is_dir():
        print(f"找不到 frontend/src：{src_dir}", file=sys.stderr)
        return 2

    summary = build_summary(src_dir)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if args.format == "json":
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(render_text(summary, args.max_list, worklist_all=args.worklist == "all"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
