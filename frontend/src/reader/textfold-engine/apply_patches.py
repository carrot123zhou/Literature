#!/usr/bin/env python3
"""TextFold 内核兼容补丁（可追踪、范围极小；原件 upstream-reader.js 保持逐字不动）。

补丁目的（backend 契约 docs/READER_BACKEND.md §4.8b，Literature 目标 lane）：
1. **L3 Brief 派生句**（tr[locale].lad.ev_space==='source' 且存在 derived_brief）：
   Brief 层渲染 derived_brief 独立 token 流（sents 为其内部范围、逐项对应 source_ev），
   不触碰 Full 层的 z.toks/z.sents，也不把派生句塞进全文——派生译句与译文全文无词级
   对应，按产品要求不伪造 m/不污染全文。
2. **source-fallback 原文回退**：brief_source==='source-fallback' 的 unit，目标栏 Brief/
   Key/Topic 一律回退原文（u.toks/u.sents/u.lad），不错摘译文。

补丁以精确锚替换实现：锚缺失/被改即报错退出（vendor 升级时重验）。生成 engine-patched.js；
原始副本 upstream-reader.js 保持可校验。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "upstream-reader.js"
OUT = HERE / "engine-patched.js"

VIEW_ANCHOR = """    if (z) return { toks: z.toks, sents: z.sents, ev: u.lad.ev.filter((s) => s < z.sents.length), brief: l.brief, tn: l.tn, topic: l.topic, glue: l.glue, p: `${lang}~` };
    return { toks: u.toks, sents: u.sents, ev: u.lad.ev, brief: l.brief, tn: l.tn, topic: l.topic, glue: l.glue, p: '' };"""

VIEW_PATCHED = """    if (z) {
      // Literature 补丁（backend §4.8b）：目标 lane 分派 ——
      //  - brief_source==='source-fallback'：原文回退（不错摘译文）；
      //  - ev_space==='source' 且有 derived_brief：L3 Brief 用派生独立 token 流
      //    （briefOverride：toks/sents/ev 全在派生空间，不进全文、不伪造 m）；
      //  - ev_space==='target'（translation-local）：内核原生路径（目标 sents + 目标 lad.ev）。
      if (z.lad && z.lad.brief_source === 'source-fallback') {
        return { toks: u.toks, sents: u.sents, ev: u.lad.ev, brief: u.lad.brief, tn: u.lad.tn, topic: u.lad.topic, glue: u.lad.glue, p: '', fb: 1 };
      }
      const db = z.lad && z.lad.ev_space === 'source' ? z.lad.derived_brief : null;
      const briefOverride = db && Array.isArray(db.toks) && db.toks.length && Array.isArray(db.sents)
        ? { toks: db.toks, sents: db.sents, ev: db.sents.map((_r, i) => i) } : null;
      // ev_space==='target'（translation-local）时 ev 用目标 lane 自己的 lad.ev（原文 ev 只用于回跳原文）
      const laneEv = z.lad && z.lad.ev_space === 'target' && Array.isArray(z.lad.ev) ? z.lad.ev : u.lad.ev;
      return { toks: z.toks, sents: z.sents, ev: laneEv.filter((s) => s < z.sents.length), brief: l.brief, tn: l.tn, topic: l.topic, glue: l.glue, p: `${lang}~`, briefOverride };
    }
    return { toks: u.toks, sents: u.sents, ev: u.lad.ev, brief: l.brief, tn: l.tn, topic: l.topic, glue: l.glue, p: '' };"""

L3_ANCHOR = """      const keep = new Set(v.brief.filter((t) => t.m !== undefined).map((t) => t.m));
      out = [];
      let prevEnd = null;
      const sents = mine ? [...new Set([...v.ev, ...v.sents.map((r, k) => [r, k]).filter(([r]) => mine.some((i) => i >= r[0] && i < r[1])).map(([, k]) => k)])].sort((a, b) => a - b) : v.ev;
      for (const s of sents) {
        const [a, b] = v.sents[s];"""

L3_PATCHED = """      const keep = new Set(v.brief.filter((t) => t.m !== undefined).map((t) => t.m));
      out = [];
      let prevEnd = null;
      // Literature 补丁：派生 Brief 有独立 token 空间（briefOverride）时，本层只画它；
      // 句选择/切分/渲染全部在派生空间内（ev=派生句下标），绝不索引全文 toks/sents。
      const bov = v.briefOverride || null;
      const bsents = bov ? bov.sents : v.sents;
      const btoks = bov ? bov.toks : v.toks;
      const sents = bov ? bov.ev : (mine ? [...new Set([...v.ev, ...v.sents.map((r, k) => [r, k]).filter(([r]) => mine.some((i) => i >= r[0] && i < r[1])).map(([, k]) => k)])].sort((a, b) => a - b) : v.ev);
      for (const s of sents) {
        const [a, b] = bsents[s];"""

BODY_ANCHOR = """        for (let i = a; i < b; i++) {
          if (!CJK_DOC) { out.push({ id: p + i, i, tok: v.toks[i], lead: keep.has(i) }); continue; }"""

REPLACESTATE_ANCHOR = "    if (D.app) history.replaceState(null, '', `?lang=${encodeURIComponent(l)}${location.hash}`);"
REPLACESTATE_PATCHED = """    if (D.app) { try { history.replaceState(null, '', `?lang=${encodeURIComponent(l)}${location.hash}`); } catch { /* srcdoc iframe：无 history 权限（Literature 补丁） */ } }"""

BODY_PATCHED = """        for (let i = a; i < b; i++) {
          if (bov) { out.push({ id: `${p}db${i}`, tok: btoks[i], lead: false, di: i }); continue; }   // 派生 token 独立身份（di 为 derived 空间下标，不映射全文偏移）
          if (!CJK_DOC) { out.push({ id: p + i, i, tok: v.toks[i], lead: keep.has(i) }); continue; }"""

# —— derived 独立索引贯穿（di/dbtokens 内部一致，持久化用 anchor.tokens=派生下标） ——
DOM_ANCHOR = "      s._i = p.i;\n      s._j = p.j;"
DOM_PATCHED = "      s._i = p.i;\n      s._j = p.j;\n      if (p.di !== undefined) s._di = p.di;   // Literature 补丁：derived 独立下标"

SEL_ANCHOR = """      for (const [a, b, piece] of segments(tx)) {
        if (!piece || b <= from || a >= to) continue;
        if (piece._i !== undefined) { lo = Math.min(lo, piece._i); hi = Math.max(hi, piece._i); }
        if (piece._j !== undefined) { jlo = Math.min(jlo, piece._j); jhi = Math.max(jhi, piece._j); }
      }"""
SEL_PATCHED = """      let dlo = Infinity, dhi = -Infinity;   // Literature 补丁：derived 独立空间下标收集
      for (const [a, b, piece] of segments(tx)) {
        if (!piece || b <= from || a >= to) continue;
        if (piece._i !== undefined) { lo = Math.min(lo, piece._i); hi = Math.max(hi, piece._i); }
        if (piece._j !== undefined) { jlo = Math.min(jlo, piece._j); jhi = Math.max(jhi, piece._j); }
        if (piece._di !== undefined) { dlo = Math.min(dlo, piece._di); dhi = Math.max(dhi, piece._di); }
      }"""

RET_ANCHOR = """      return { unit: uid, section: chunkOf[uid] ? chunkOf[uid].id : null, lang, level: L >= 1 && L <= 2 && !line && tokens ? 4 : L, tokens, sents, quote, ...(shown ? { shown } : {}) };"""
RET_PATCHED = """      // Literature 补丁：纯 derived 选择 → tokens 为派生空间下标（持久化 engine_anchor.space='derived'），
      // sents 置 null（不冒充全文句），quote 来自 db 文本本身。
      if (dlo <= dhi && lo > hi && v.briefOverride) {
        const dbText = toksText(v.briefOverride.toks, dlo, dhi);
        return { unit: uid, section: chunkOf[uid] ? chunkOf[uid].id : null, lang, level: L, tokens: [dlo, dhi], sents: null,
                 quote: { exact: dbText, prefix: '', suffix: '' } };
      }
      return { unit: uid, section: chunkOf[uid] ? chunkOf[uid].id : null, lang, level: L >= 1 && L <= 2 && !line && tokens ? 4 : L, tokens, sents, quote, ...(shown ? { shown } : {}) };"""

MK_ANCHOR = """    for (const n of list) {
      const a = n.anchor, c = shadeOf(n);
      if (a.shown) {"""
MK_PATCHED = """    const dexact = new Set();   // Literature 补丁：derived 空间标记（anchor.tokens → di，仅派生 lane 消费）
    for (const n of list) {
      const a = n.anchor, c = shadeOf(n);
      if (a.level === 3 && !a.shown && a.tokens && v.briefOverride && a.lang === lang) {   // a.lang 必须是当前内核语言（防其他语言/源 Brief token 误入 derived）
        for (let i = a.tokens[0]; i <= a.tokens[1]; i++) dexact.add(i);
        continue;   // derived 锚只作用于派生空间，不进 Full exact/soft（防错标）
      }
      if (a.shown) {"""
MKRET_ANCHOR = "    return exact.size || soft.size || line.size ? { exact, soft, old, line, sh, lineSh } : null;"
MKRET_PATCHED = "    return exact.size || soft.size || line.size || dexact.size ? { exact, soft, old, line, sh, lineSh, dexact } : null;"
HL_ANCHOR = """    if (mk) for (const x of out) {
      if (L <= 2 && !x.kept) {"""
HL_PATCHED = """    if (mk) for (const x of out) {
      if (x.di !== undefined) { x.hl = mk.dexact && mk.dexact.has(x.di) ? 1 : 0; continue; }   // Literature 补丁：derived piece 着色走 dexact
      if (L <= 2 && !x.kept) {"""


# 临时诊断（验后移除）：showSelbar/selectionAnchors 入口状态采样


def apply(src: str, anchor: str, patched: str, name: str) -> str:
    if src.count(anchor) != 1:
        sys.exit(f"补丁锚点校验失败（{name}）：出现 {src.count(anchor)} 次，期望 1 —— vendor 已变更，需重验")
    return src.replace(anchor, patched, 1)


def main() -> None:
    src = SRC.read_text(encoding="utf-8")
    patched = apply(src, VIEW_ANCHOR, VIEW_PATCHED, "view()")
    patched = apply(patched, DOM_ANCHOR, DOM_PATCHED, "DOM _di")
    patched = apply(patched, SEL_ANCHOR, SEL_PATCHED, "selectionAnchors _di")
    patched = apply(patched, RET_ANCHOR, RET_PATCHED, "derived anchor 构造")
    patched = apply(patched, MK_ANCHOR, MK_PATCHED, "marksFor dexact")
    patched = apply(patched, MKRET_ANCHOR, MKRET_PATCHED, "marksFor 返回 dexact")
    patched = apply(patched, HL_ANCHOR, HL_PATCHED, "pieces hl di 消费")
    patched = apply(patched, L3_ANCHOR, L3_PATCHED, "pieces(L3)")
    patched = apply(patched, BODY_ANCHOR, BODY_PATCHED, "pieces(L3 body)")
    patched = apply(patched, REPLACESTATE_ANCHOR, REPLACESTATE_PATCHED, "setLang history (srcdoc 安全)")
    OUT.write_text(patched, encoding="utf-8")
    digest = lambda b: hashlib.sha256(b).hexdigest()[:16]  # noqa: E731
    print(f"upstream {digest(src.encode())} -> patched {digest(patched.encode())} (+{len(patched) - len(src)} bytes)")


if __name__ == "__main__":
    main()
