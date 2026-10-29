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
      //  - lit_pending：目标 lane 短层未生成但全文译文（z.toks）在——必须用 z.toks 原有全文
      //    译文且 p 带 locale，绝不回退源 u.toks、不走 source-fallback；
      //  - brief_source==='source-fallback'：原文回退（不错摘译文）；
      //  - ev_space==='source' 且有 derived_brief：L3 Brief 用派生独立 token 流
      //    （briefOverride：toks/sents/ev 全在派生空间，不进全文、不伪造 m）；
      //  - ev_space==='target'（translation-local）：内核原生路径（目标 sents + 目标 lad.ev）。
      if ((z.lad && z.lad.pend === true) || z.lit_pending === true || z.pending === true) {
        return { toks: z.toks, sents: z.sents, ev: u.lad.ev.filter((s) => s < z.sents.length), brief: [], tn: 0, topic: [], glue: u.lad.glue, p: `${lang}~`, pend: 1 };
      }
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

# —— lit_pending（部分失败，冻结契约 tmp/reader-partial-contract.md）——
# unit/chunk 顶层 lit_pending / lit_summary_pending 恒指【原文 lane】；tr[locale] 内恒指【目标 lane】。
# 单点收敛 levelOf：渲染（data-l/hidden）、pieces、选择锚（L/line/shown）、批注锚 level 全部按
# Full(4) 全文词空间，绝不按短层写 summary/shown/derived 锚（否则批注保存失效/409）。
LEVELOF_ANCHOR = "  const levelOf = (uid) => localLevel.has(uid) ? localLevel.get(uid) : level;"
LEVELOF_PATCHED = """  // Literature 补丁（lit_pending）：lane 独立 —— 先按当前 trOf/locale 选 lane：
  // 目标栏（有 tr[locale]）只用目标 lane 状态；源栏（无 tr）只用顶层状态。绝不 OR 两 lane
  //（源摘要缺失不得传播到目标栏，反之亦然）。
  const litPendingBase = (x) => x ? (x.lit_pending === true || x.pending === true || (x.lad && x.lad.pend === true)) : false;
  const litPendingOf = (x) => {
    if (!x) return false;
    const z = trOf(x);
    if (z) return litPendingBase(z);   // 目标 lane 在：只看目标 lane
    return litPendingBase(x);          // 源栏：只看顶层
  };
  const levelOf = (uid) => {
    if (localLevel.has(uid)) return localLevel.get(uid);
    if (litPendingOf(U[uid])) return 4;
    return level;
  };"""

UNITNODE_ANCHOR = "    const el = h(tag, `u ${u.role} r-${u.lad.role}${u.lad.model === 'local' ? ' is-local' : ''}`);"
UNITNODE_PATCHED = "    const el = h(tag, `u ${u.role} r-${u.lad.role}${u.lad.model === 'local' ? ' is-local' : ''}`);   // Literature 补丁：is-pend 徽标由 renderUnit 按 lane 同步（切语言重算）"

RENDERUNIT_ANCHOR = """  function renderUnit(uid, hot = true) {
    const L = levelOf(uid), el = unitEl.get(uid);
    el.classList.toggle('local-open', localLevel.has(uid) && L > level);"""
RENDERUNIT_PATCHED = """  function renderUnit(uid, hot = true) {
    const L = levelOf(uid), el = unitEl.get(uid);
    el.classList.toggle('local-open', localLevel.has(uid) && L > level);
    el.classList.toggle('is-pend', litPendingOf(U[uid]));   // Literature 补丁：is-pend 按当前 lane 重算（不随构建时语言陈旧）"""

STAKE_ANCHOR = """  function renderStake(c, st) {
    const lad = (trOf(c) && trOf(c).lad) || c.lad;
    st.replaceChildren();"""
STAKE_PATCHED = """  function renderStake(c, st) {
    const lad = (trOf(c) && trOf(c).lad) || c.lad;
    st.replaceChildren();
    // Literature 补丁（lit_summary_pending）：本章摘要未生成 → 不显示摘要行（标题保留），
    // 不把局部/旧 take 冒充本章摘要。lane 独立：有 tr[locale] 只用目标 lane 状态，
    // 源栏只用顶层状态，不 OR（源缺失不传播到目标，反之亦然）。
    const _cz0 = trOf(c);
    const _spendLane = _cz0
      ? (_cz0.lit_summary_pending === true || (_cz0.lad && _cz0.lad.spend === true))
      : ((c && c.lit_summary_pending === true) || (c && c.lad && c.lad.spend === true));
    if (_spendLane) { st.hidden = true; return; }
    if (!CJK_DOC) st.hidden = false;   // Literature 补丁：lane 切换后确定性复位（上游非 CJK 从不管理 hidden）"""

CHUNKCLS_ANCHOR = "        const cur = h('section', `chunk l${c.level}${c.units.length ? '' : ' empty'}${ref ? ' ref' : ''}`);"
CHUNKCLS_PATCHED = """        const _czt = trOf(c);
        const _spend = _czt
          ? (_czt.lit_summary_pending === true || (_czt.lad && _czt.lad.spend === true))   // Literature 补丁：目标 lane 在只看目标
          : (c.lit_summary_pending === true || (c.lad && c.lad.spend === true));           // 源栏只看顶层（lane 独立，不 OR）
        const cur = h('section', `chunk l${c.level}${c.units.length ? '' : ' empty'}${ref ? ' ref' : ''}${_spend ? ' is-spend' : ''}`);"""

# 暴露内核 notes API（宿主统一菜单调用，不另造批注系统）
NOTES_ANCHOR = "  function deleteNote(n) {"
NOTES_PATCHED = """  // Literature 补丁：暴露内核 notes API 给桥
  window.__tfNotes = { saveNote, deleteNote, notes: () => notes, selectionAnchors };

  function deleteNote(n) {"""

# 纯内存 apply（不 POST /api/notes——宿主 CAS 成功后广播完整 notes 数组，内核只重渲染）
APPLY_ANCHOR = "  function indexNotes() {\n    notesOf.clear();"
APPLY_PATCHED = """  // Literature: host-confirmed notes are applied in memory only; never POST here.
  const hostSectionOpen = new Set();
  const hostOpenPending = new Map();
  function hostOpen(kind, id, open) {
    const key = kind + ':' + id;
    hostOpenPending.set(key, open);
    const run = () => {
      if (!hostOpenPending.has(key)) return;
      if (busy || stage) { setTimeout(run, 60); return; }
      const value = hostOpenPending.get(key); hostOpenPending.delete(key);
      const chunk = kind === 'section' && D.chunks.find(c => c.id === id);
      const ids = chunk ? chunk.units : [id];
      for (const uid of ids) {
        if (!unitEl.has(uid)) continue;
        if (value) localLevel.set(uid, 4); else localLevel.delete(uid);
        renderUnit(uid, true);
      }
      if (chunk) {
        if (value) hostSectionOpen.add(id); else hostSectionOpen.delete(id);
        const sc = secs.get(id); if (sc) sc.st.dataset.hostOpen = String(value);
      }
      relayout();
    };
    run();
  }
  window.__tfApply = {
    applyNotes(arr) {
      const keys = new Set(notesOf.keys());
      notes = Array.isArray(arr) ? arr : [];
      notesOf.clear(); indexNotes();
      for (const key of notesOf.keys()) keys.add(key);
      notesChanged([...keys]);
    },
    setUnitOpen(uid, open) { hostOpen('unit', uid, Boolean(open)); },
    setSectionOpen(id, open) { hostOpen('section', id, Boolean(open)); },
    toggleSection(id) {
      const open = !hostSectionOpen.has(id);
      hostOpen('section', id, open);
      window.dispatchEvent(new CustomEvent('lit-open', {detail:{kind:'section', id, open}}));
    },
    getUnitState(uid) { return {localOpen:localLevel.has(uid)}; },
    getSectionState(id) { return {localOpen:hostSectionOpen.has(id)}; },
    noteAt,
  };

  function indexNotes() {"""

TOGGLE_ANCHOR = "      if (localLevel.has(uid)) localLevel.delete(uid); else localLevel.set(uid, 4);"
TOGGLE_PATCHED = TOGGLE_ANCHOR + "\n      window.dispatchEvent(new CustomEvent('lit-open', {detail:{kind:'unit', id:uid, open:localLevel.has(uid)}}));"



# 临时诊断（验后移除）：showSelbar/selectionAnchors 入口状态采样


def apply(src: str, anchor: str, patched: str, name: str) -> str:
    if src.count(anchor) != 1:
        sys.exit(f"补丁锚点校验失败（{name}）：出现 {src.count(anchor)} 次，期望 1 —— vendor 已变更，需重验")
    return src.replace(anchor, patched, 1)


def main() -> None:
    src = SRC.read_text(encoding="utf-8")
    patched = apply(src, VIEW_ANCHOR, VIEW_PATCHED, "view()")
    patched = apply(patched, LEVELOF_ANCHOR, LEVELOF_PATCHED, "levelOf lit_pending")
    patched = apply(patched, UNITNODE_ANCHOR, UNITNODE_PATCHED, "unitNode is-pend")
    patched = apply(patched, RENDERUNIT_ANCHOR, RENDERUNIT_PATCHED, "renderUnit is-pend 同步")
    patched = apply(patched, STAKE_ANCHOR, STAKE_PATCHED, "renderStake lit_summary_pending")
    patched = apply(patched, CHUNKCLS_ANCHOR, CHUNKCLS_PATCHED, "chunk is-spend")
    patched = apply(patched, DOM_ANCHOR, DOM_PATCHED, "DOM _di")
    patched = apply(patched, SEL_ANCHOR, SEL_PATCHED, "selectionAnchors _di")
    patched = apply(patched, RET_ANCHOR, RET_PATCHED, "derived anchor 构造")
    patched = apply(patched, MK_ANCHOR, MK_PATCHED, "marksFor dexact")
    patched = apply(patched, MKRET_ANCHOR, MKRET_PATCHED, "marksFor 返回 dexact")
    patched = apply(patched, HL_ANCHOR, HL_PATCHED, "pieces hl di 消费")
    patched = apply(patched, L3_ANCHOR, L3_PATCHED, "pieces(L3)")
    patched = apply(patched, BODY_ANCHOR, BODY_PATCHED, "pieces(L3 body)")
    patched = apply(patched, REPLACESTATE_ANCHOR, REPLACESTATE_PATCHED, "setLang history (srcdoc 安全)")
    # 暴露内核 notes API（宿主统一菜单调用）+ 纯内存 apply（不 POST，防递归 CAS）
    patched = apply(patched, NOTES_ANCHOR, NOTES_PATCHED, "__tfNotes API")
    patched = apply(patched, APPLY_ANCHOR, APPLY_PATCHED, "__tfApply 纯内存")
    patched = apply(patched, TOGGLE_ANCHOR, TOGGLE_PATCHED, "unit open bridge")
    patched = apply(patched, "  function showSelbar() {", "  function showSelbar() {\n    if (window.__litHostMenu) return;", "host selection menu")
    OUT.write_text(patched, encoding="utf-8")
    digest = lambda b: hashlib.sha256(b).hexdigest()[:16]  # noqa: E731
    print(f"upstream {digest(src.encode())} -> patched {digest(patched.encode())} (+{len(patched) - len(src)} bytes)")


if __name__ == "__main__":
    main()
