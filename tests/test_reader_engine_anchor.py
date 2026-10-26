"""engine_anchor（短层笔记的原生锚持久化）针对性回归——**单一契约**（与前端
types.ts/tokenMap.ts/tokenSpaceDigest 对齐的 envelope：version/locale/space/digest/thread/anchor）。

覆盖：
- 保存 → 新 store 读回 → 更新颜色/文字 → 删除（锚全程保留/随记录消失）；
- CAS 冲突（409）不变；
- 恶意/不合法字段：白名单外字段、坏 space/digest/version、unit 与 block_id 不一致、
  空间/level 不一致、thread 超限、控制字符——扩展被丢弃但批注保留（旧记录不迁坏）；
- 产物指纹（djb2 空间 digest）：产物变化 → digest 失配 → 锚失效回退 block+quote、笔记保留；
- 索引越界（tokens 超出当前空间 toks 长度）→ 锚失效；
- token_space_digest 与前端 tokenSpaceDigest 同算法：确定性、词界敏感、产物变化即变化。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from literature.textfold_adapter import tf_tokens
from literature.reader import (
    ReaderConflict,
    ReaderLayerService,
    ReaderStore,
    sanitize_engine_anchor,
    token_space_digest,
)
from literature.storage import ProjectStore

sys.path.insert(0, str(Path(__file__).parent))
from test_reader_fold_run import ScriptedProvider, make_document, make_translation  # noqa: E402


def make_real_anchor(native: dict, **overrides) -> dict:
    """合法 brief（写行）空间锚基样：digest 与 shown 都从真实 native 派生
    （与前端 makeEngineAnchor/engineAnchorUsable 同一构造与校验路径）。"""
    atoms = native.get("atoms") or {}
    lad = native["units"]["b-p-1"]["tr"]["zh-CN"]["lad"]
    brief = lad["brief"]
    hi = min(5, len(brief) - 1)
    words = [0, hi]
    exact = _text_like(brief, words[0], words[1], atoms)
    digest = token_space_digest(brief, atoms)
    anchor = {
        "version": 1,
        "locale": "zh-CN",
        "space": "brief",
        "digest": digest,
        "anchor": {
            "on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN",
            "level": 2, "shown": {"words": words, "exact": exact},
            "quote": {"exact": exact, "prefix": "", "suffix": ""},
        },
    }
    anchor.update(overrides)
    return {key: value for key, value in anchor.items() if value is not None}


def _text_like(toks: list, a: int, b: int, atoms: dict) -> str:
    out = ""
    for index in range(a, min(b, len(toks) - 1) + 1):
        tk = toks[index]
        if isinstance(tk, dict) and tk.get("a"):
            continue   # atom token：测试书无 atom，跳过以免 tf_tokens.plain 需要 atom 表
        out += tf_tokens.plain([tk], atoms) if not isinstance(tk, dict) else (tk.get("t") or "")
        if isinstance(tk, dict) and tk.get("s") and index < b:
            out += " "
    return out


class EngineAnchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        project = self.store.create_project(source, title="Silk Road Silver")
        self.project_id = project["project_id"]
        document = make_document(self.project_id)
        self.store.save_document(document)
        self.store.save_translation(make_translation(document))
        # 生成真实 native（受控 provider，零付费）
        provider = ScriptedProvider()
        ReaderLayerService(self.store).generate(
            self.project_id, locale="zh-CN", provider=provider,
            model_label="fake-model", service_id="svc",
        )
        self.reader = ReaderStore(self.store)
        self.native = self.reader.load_native(self.project_id, "zh-CN")
        self.base_anchor = make_real_anchor(self.native)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _save_annotation(self, annotation: dict) -> dict:
        overlay = self.reader.load_overlay(self.project_id)
        overlay["annotations"] = [annotation]
        return self.reader.save_overlay(self.project_id, overlay, base_revision=overlay["revision"])

    # 1. 保存 → 新 store 读回 -------------------------------------------------

    def test_anchor_roundtrip_through_new_store(self) -> None:
        annotation = {
            "id": "a1", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "短层笔记",
            "detached": False,
            "created_at": "2026-10-26T00:00:00Z", "updated_at": "2026-10-26T00:00:00Z",
            "engine_anchor": {**self.base_anchor},
        }
        saved = self._save_annotation(annotation)
        self.assertIn("engine_anchor", saved["annotations"][0])
        self.assertEqual(saved["annotations"][0]["engine_anchor"]["digest"], self.base_anchor["digest"])
        # 新 store 读回：锚完整保留（含 anchor 快照与 thread）
        fresh = ReaderStore(self.store).load_overlay(self.project_id)
        engine = fresh["annotations"][0]["engine_anchor"]
        self.assertEqual(engine["space"], "brief")
        self.assertEqual(engine["locale"], "zh-CN")
        self.assertEqual(engine["anchor"]["shown"]["words"], self.base_anchor["anchor"]["shown"]["words"])
        self.assertEqual(engine["anchor"]["unit"], "b-p-1")

    def test_update_color_note_then_delete_keeps_anchor_semantics(self) -> None:
        annotation = {
            "id": "a1", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "短层笔记", "detached": False,
            "engine_anchor": {**self.base_anchor},
        }
        # 改色：锚保留
        overlay = self.reader.load_overlay(self.project_id)
        overlay["annotations"] = [{**annotation, "color": "g"}]
        saved = self.reader.save_overlay(self.project_id, overlay, base_revision=overlay["revision"])
        self.assertEqual(saved["annotations"][0]["color"], "g")
        self.assertIn("engine_anchor", saved["annotations"][0])
        # 改文字：锚保留
        overlay = self.reader.load_overlay(self.project_id)
        overlay["annotations"] = [{**saved["annotations"][0], "note": "改过的笔记"}]
        saved = self.reader.save_overlay(self.project_id, overlay, base_revision=overlay["revision"])
        self.assertEqual(saved["annotations"][0]["note"], "改过的笔记")
        self.assertIn("engine_anchor", saved["annotations"][0])
        # 删除：记录整体消失
        overlay = self.reader.load_overlay(self.project_id)
        overlay["annotations"] = []
        saved = self.reader.save_overlay(self.project_id, overlay, base_revision=overlay["revision"])
        self.assertEqual(saved["annotations"], [])

    # 2. CAS 冲突 ------------------------------------------------------------

    def test_cas_conflict_with_engine_anchor(self) -> None:
        annotation = {
            "id": "a-cas", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": {**self.base_anchor},
        }
        self._save_annotation(annotation)
        overlay = self.reader.load_overlay(self.project_id)
        stale_revision = int(overlay["revision"]) - 1
        with self.assertRaises(ReaderConflict):
            self.reader.save_overlay(self.project_id, {**overlay, "annotations": [annotation]},
                                     base_revision=stale_revision)

    # 3. 恶意/不合法字段：扩展丢弃、批注保留 ----------------------------------

    def test_invalid_anchor_dropped_annotation_kept(self) -> None:
        cases = [
            ("坏 space", make_real_anchor(self.native, space="tokens")),
            ("坏 version", make_real_anchor(self.native, version=2)),
            ("digest 非 8hex", make_real_anchor(self.native, digest="<script>")),
            ("digest 大写", make_real_anchor(self.native, digest="ABCDEF12")),
            ("unit 与 block_id 不一致", make_real_anchor(self.native, anchor={
                "on": "unit", "unit": "b-p-2", "section": "S1", "lang": "zh-CN", "level": 2,
                "shown": self.base_anchor["anchor"]["shown"],
            })),
            ("brief 缺 shown", make_real_anchor(self.native, anchor={
                "on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN", "level": 2,
            })),
            ("空间/level 不一致（derived 必须 level3）", make_real_anchor(self.native, **{
                "space": "derived",
                "anchor": {"on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN",
                           "level": 2, "shown": self.base_anchor["anchor"]["shown"]},
            })),
            ("shown 带控制字符", make_real_anchor(self.native, anchor={
                "on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN", "level": 2,
                "shown": {"words": [0, 5], "exact": "a\x00b"},
            })),
            ("thread 超限", make_real_anchor(self.native, thread=[{"x": 1}] * 65)),
        ]
        for name, anchor in cases:
            with self.subTest(case=name):
                saved = self._save_annotation({
                    "id": "a-x", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
                    "start": 0, "end": 0, "color": "y", "note": "笔记保留",
                    "detached": False, "engine_anchor": anchor,
                })
                self.assertEqual(len(saved["annotations"]), 1, name)
                self.assertNotIn("engine_anchor", saved["annotations"][0], name)
                self.assertEqual(saved["annotations"][0]["note"], "笔记保留", name)
        # 信封上的未知字段被白名单**剥离**（而不是整体丢弃）：合法锚原样保留，
        # 注入的 onclick/html 不进入存储。
        saved = self._save_annotation({
            "id": "a-strip", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": {**self.base_anchor, "onclick": "alert(1)", "html": "<b>x</b>"},
        })
        engine = saved["annotations"][0].get("engine_anchor") or {}
        self.assertIn("engine_anchor", saved["annotations"][0])
        self.assertNotIn("onclick", engine)
        self.assertNotIn("html", engine)
        self.assertEqual(engine.get("digest"), self.base_anchor["digest"])

    def test_sanitize_rejects_malformed_directly(self) -> None:
        self.assertIsNone(sanitize_engine_anchor("not-a-dict", block_id="b-p-1"))
        self.assertIsNone(sanitize_engine_anchor(None, block_id="b-p-1"))
        self.assertIsNone(sanitize_engine_anchor({**self.base_anchor, "locale": ""}, block_id="b-p-1"))
        self.assertIsNone(sanitize_engine_anchor({**self.base_anchor, "anchor": None}, block_id="b-p-1"))

    def test_thread_is_preserved_when_valid(self) -> None:
        thread = [{"q": "为什么", "a": "因为", "lang": "zh-CN"}, {"q": "还有呢"}]
        saved = self._save_annotation({
            "id": "a-thread", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": make_real_anchor(self.native, thread=thread),
        })
        self.assertEqual(saved["annotations"][0]["engine_anchor"]["thread"], thread)

    # 4. 旧 overlay 兼容 -------------------------------------------------------

    def test_legacy_overlay_without_anchor_unchanged(self) -> None:
        saved = self._save_annotation({
            "id": "old-1", "anchor": "range", "block_id": "b-p-1", "pane": "source",
            "start": 0, "end": 10,
            "quote": {"exact": "Silver vessels", "prefix": "", "suffix": ""},
            "color": "b", "note": "旧笔记", "detached": False,
            "created_at": "2026-10-20T00:00:00Z", "updated_at": "2026-10-20T00:00:00Z",
        })
        self.assertEqual(len(saved["annotations"]), 1)
        self.assertNotIn("engine_anchor", saved["annotations"][0])
        self.assertEqual(saved["annotations"][0]["quote"]["exact"], "Silver vessels")
        self.assertEqual(saved["annotations"][0]["color"], "b")

    # 5. 产物指纹：digest 失配 → 锚失效、笔记保留 ------------------------------

    def test_stale_anchor_invalidated_when_product_changes(self) -> None:
        # 用一个与当前产物不符的 digest（真实 8hex，但值不同）
        stale = make_real_anchor(self.native, digest="deadbeef" if self.base_anchor["digest"] != "deadbeef" else "12345678")
        saved = self._save_annotation({
            "id": "a-stale", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "笔记不丢", "detached": False,
            "engine_anchor": stale,
        })
        self.assertEqual(len(saved["annotations"]), 1)
        self.assertNotIn("engine_anchor", saved["annotations"][0])
        self.assertEqual(saved["annotations"][0]["note"], "笔记不丢")

    def test_out_of_range_tokens_invalidated_against_native(self) -> None:
        # full 空间锚：tokens[1] 超出当前空间 toks 总长 → 索引越界 → 失效
        # （digest 与越界 tokens 一致地计算——证明是索引校验在拦）
        unit = self.native["units"]["b-p-1"]
        toks = unit["tr"]["zh-CN"]["toks"]
        digest = token_space_digest(toks, self.native.get("atoms") or {})
        anchor = {
            "version": 1, "locale": "zh-CN", "space": "full", "digest": digest,
            "anchor": {"on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN",
                       "level": 4, "tokens": [0, len(toks) + 5],
                       "quote": {"exact": "x", "prefix": "", "suffix": ""}},
        }
        saved = self._save_annotation({
            "id": "a-oob", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": anchor,
        })
        self.assertNotIn("engine_anchor", saved["annotations"][0])

    def test_matching_full_space_anchor_survives(self) -> None:
        unit = self.native["units"]["b-p-1"]
        toks = unit["tr"]["zh-CN"]["toks"]
        atoms = self.native.get("atoms") or {}
        digest = token_space_digest(toks, atoms)
        hi = min(5, len(toks) - 1)
        from literature.reader import _toks_text_like
        quote_text = _toks_text_like(toks, 0, hi, atoms)
        anchor = {
            "version": 1, "locale": "zh-CN", "space": "full", "digest": digest,
            "anchor": {"on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN",
                       "level": 4, "tokens": [0, hi],
                       "quote": {"exact": quote_text, "prefix": "", "suffix": ""}},
        }
        saved = self._save_annotation({
            "id": "a-full", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": anchor,
        })
        self.assertIn("engine_anchor", saved["annotations"][0])

    # 6. digest 函数：与前端同算法（确定性、词界敏感、产物变化即变化） --------

    def test_token_space_digest_deterministic_and_word_boundary_sensitive(self) -> None:
        toks = self.native["units"]["b-p-1"]["tr"]["zh-CN"]["toks"]
        atoms = self.native.get("atoms") or {}
        first = token_space_digest(toks, atoms)
        self.assertEqual(first, token_space_digest(toks, atoms))
        self.assertRegex(first, r"^[0-9a-f]{8}$")
        # 词界敏感：相同字符不同分词 → digest 不同（+1 分隔符的作用）
        merged = [{"t": (toks[0].get("t") or "") + (toks[1].get("t") or "")}] + toks[2:]
        self.assertNotEqual(first, token_space_digest(merged, atoms))
        # 产物变化 → digest 变化
        self.assertNotEqual(first, token_space_digest([{"t": "不同的词"}], atoms))
        self.assertIsNone(token_space_digest([], atoms))
        self.assertIsNone(token_space_digest(None, atoms))


    # ---- 5 项修正的定点回归（Lead 审读发现的缺口） --------------------------

    def test_version_invalid_values_do_not_raise_500(self) -> None:
        """version='bad'/[]/True 等：严格类型拒绝（返回 None），不得让 int() 抛异常。"""
        for bad in ("bad", [], ["1"], True, False, 1.0, None, {}, "1"):
            with self.subTest(version=repr(bad)):
                payload = {**self.base_anchor, "version": bad}
                self.assertIsNone(sanitize_engine_anchor(payload, block_id="b-p-1"))

    def test_full_space_allows_level3_source_brief(self) -> None:
        """full 指 token 空间不等于只许 level4：源 Brief/translation-local Brief 的
        level3 无 shown 锚（tokens 指 Full toks）必须合法。"""
        anchor = {
            "version": 1, "locale": "zh-CN", "space": "full", "digest": self._full_digest(),
            "anchor": {"on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN",
                       "level": 3, "tokens": [0, min(5, len(self._full_toks()) - 1)],
                       "quote": {"exact": "", "prefix": "", "suffix": ""}},
        }
        saved = self._save_annotation({
            "id": "a-l3full", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": anchor,
        })
        self.assertIn("engine_anchor", saved["annotations"][0])

    def test_natural_key_anchor_with_tokens_sents_shown_roundtrip(self) -> None:
        """上游自然 selbar Key 锚（Lead 实读真实形态）：shown.words 是写行位置，
        tokens/sents 是对应 Full 来源范围，三者**共存**——保存 → 响应 → 新 store 读回
        完全保留（不是只传入体真）。"""
        source_native = self.reader.load_native(self.project_id, "source")
        lad = source_native["units"]["b-p-1"]["lad"]
        brief = lad["brief"]
        toks = source_native["units"]["b-p-1"]["toks"]
        atoms = source_native.get("atoms") or {}
        words = [4, min(14, len(brief) - 1)]
        exact = _text_like(brief, words[0], words[1], atoms)
        digest = token_space_digest(brief, atoms)
        anchor = {
            "version": 1, "locale": "en", "space": "brief", "digest": digest,
            "anchor": {"on": "unit", "unit": "b-p-1", "section": "S1", "lang": "en",
                       "level": 2, "tokens": [5, 6], "sents": [0],
                       "shown": {"words": words, "exact": exact},
                       "quote": {"exact": exact, "prefix": "", "suffix": ""}},
        }
        annotation = {
            "id": "a-natural", "anchor": "block", "block_id": "b-p-1", "pane": "source",
            "start": 0, "end": 0, "color": "y", "note": "自然写行锚", "detached": False,
            "engine_anchor": anchor,
        }
        saved = self._save_annotation(annotation)
        self.assertIn("engine_anchor", saved["annotations"][0])
        self.assertEqual(saved["annotations"][0]["engine_anchor"], anchor)
        # 新 store 读回：逐字段完全保留（tokens/sents/shown 共存不被剥离）
        fresh = ReaderStore(self.store).load_overlay(self.project_id)
        engine = fresh["annotations"][0]["engine_anchor"]
        self.assertEqual(engine, anchor)
        self.assertEqual(engine["anchor"]["tokens"], [5, 6])
        self.assertEqual(engine["anchor"]["sents"], [0])
        self.assertEqual(engine["anchor"]["shown"]["exact"], exact)

    def test_brief_sents_beyond_full_lane_invalid(self) -> None:
        """brief 锚的 sents 对 **Full lane 句数组**校验（不是 brief 长度）：越界失效。"""
        anchor = make_real_anchor(self.native, anchor={
            **self.base_anchor["anchor"], "sents": [99],
        })
        saved = self._save_annotation({
            "id": "a-sents", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": anchor,
        })
        self.assertNotIn("engine_anchor", saved["annotations"][0])

    def test_derived_space_with_sents_rejected(self) -> None:
        """derived 不得冒充 Full sents：带 sents 的 derived 锚拒绝。"""
        anchor = {
            "version": 1, "locale": "zh-CN",
            "space": "derived", "digest": "12345678",
            "anchor": {"on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN",
                       "level": 3, "tokens": [0, 2], "sents": [0],
                       "quote": {"exact": "", "prefix": "", "suffix": ""}},
        }
        saved = self._save_annotation({
            "id": "a-derived-sents", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": anchor,
        })
        self.assertNotIn("engine_anchor", saved["annotations"][0])

    def test_quote_outside_selected_token_range_invalid(self) -> None:
        # quote 文本必须落在实际所选 token 区间内：区间文本不含 quote → 失效
        unit = self.native["units"]["b-p-1"]
        toks = unit["tr"]["zh-CN"]["toks"]
        digest = token_space_digest(toks, self.native.get("atoms") or {})
        anchor = {
            "version": 1, "locale": "zh-CN", "space": "full", "digest": digest,
            "anchor": {"on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN",
                       "level": 4, "tokens": [0, 0],
                       "quote": {"exact": "整本书里不存在的引文内容", "prefix": "", "suffix": ""}},
        }
        saved = self._save_annotation({
            "id": "a-quo", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "笔记保留", "detached": False,
            "engine_anchor": anchor,
        })
        self.assertNotIn("engine_anchor", saved["annotations"][0])
        self.assertEqual(saved["annotations"][0]["note"], "笔记保留")

    def test_sents_out_of_range_invalidated(self) -> None:
        # full 空间 sents 越界：句下标超出 tr sents 长度 → 失效
        unit = self.native["units"]["b-p-1"]
        toks = unit["tr"]["zh-CN"]["toks"]
        sents_count = len(unit["tr"]["zh-CN"]["sents"])
        anchor = {
            "version": 1, "locale": "zh-CN", "space": "full", "digest": self._full_digest(),
            "anchor": {"on": "unit", "unit": "b-p-1", "section": "S1", "lang": "zh-CN",
                       "level": 3, "tokens": [0, min(5, len(toks) - 1)],
                       "sents": [sents_count + 3],
                       "quote": {"exact": "", "prefix": "", "suffix": ""}},
        }
        saved = self._save_annotation({
            "id": "a-sents-oob", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": anchor,
        })
        self.assertNotIn("engine_anchor", saved["annotations"][0])

    def test_pane_locale_mismatch_invalid(self) -> None:
        # 源/译越界：pane=source 的批注带译文 locale 锚 → 失效
        saved = self._save_annotation({
            "id": "a-panemix", "anchor": "block", "block_id": "b-p-1", "pane": "source",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": {**self.base_anchor},
        })
        self.assertNotIn("engine_anchor", saved["annotations"][0])

    def test_source_pane_anchor_with_source_locale_survives(self) -> None:
        """源 lane：前端 locale 用源语言码（en），后端归一到 native_source.json。"""
        source_native = self.reader.load_native(self.project_id, "source")
        self.assertIsNotNone(source_native)
        lad = source_native["units"]["b-p-1"]["lad"]
        brief = lad.get("brief") or []
        toks = source_native["units"]["b-p-1"]["toks"]
        anchor = {
            "version": 1, "locale": "en", "space": "full", "digest": token_space_digest(toks, source_native.get("atoms") or {}),
            "anchor": {"on": "unit", "unit": "b-p-1", "section": "S1", "lang": "en",
                       "level": 3, "tokens": [0, min(3, len(toks) - 1)],
                       "quote": {"exact": "", "prefix": "", "suffix": ""}},
        }
        saved = self._save_annotation({
            "id": "a-src", "anchor": "block", "block_id": "b-p-1", "pane": "source",
            "start": 0, "end": 0, "color": "y", "note": "源层短层笔记", "detached": False,
            "engine_anchor": anchor,
        })
        self.assertIn("engine_anchor", saved["annotations"][0])
        self.assertEqual(saved["annotations"][0]["engine_anchor"]["locale"], "en")
        # 该源 native 缺失时不得放行：删掉后重新保存 → 锚失效、笔记保留
        self.reader.native_path(self.project_id, "source").rename(
            self.reader.native_path(self.project_id, "source").with_suffix(".bak"))
        try:
            overlay = self.reader.load_overlay(self.project_id)
            overlay["annotations"] = [saved["annotations"][0]]
            resaved = self.reader.save_overlay(self.project_id, overlay, base_revision=overlay["revision"])
            self.assertNotIn("engine_anchor", resaved["annotations"][0])
            self.assertEqual(resaved["annotations"][0]["note"], "源层短层笔记")
        finally:
            self.reader.native_path(self.project_id, "source").with_suffix(".bak").rename(
                self.reader.native_path(self.project_id, "source"))

    def test_thread_whitelist_strips_transient_and_oversize(self) -> None:
        thread = [
            {"q": "第一问", "a": "第一答", "lang": "zh-CN", "cites": ["blk-1"]},
            {"q": "第二问", "a": "答案", "pending": True, "demo": True, "extra": {"deep": {"x": 1}}},
        ]
        saved = self._save_annotation({
            "id": "a-thread2", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": make_real_anchor(self.native, thread=thread),
        })
        engine = saved["annotations"][0]["engine_anchor"]
        self.assertEqual(len(engine["thread"]), 2)
        # 瞬态/未知字段剥离；必要字段保留
        self.assertEqual(set(engine["thread"][1].keys()), {"q", "a"})
        self.assertEqual(engine["thread"][0]["cites"], ["blk-1"])
        # 非法 turn（无 q / 非 dict / cites 超限）→ 整个锚丢弃
        for bad in (
            make_real_anchor(self.native, thread=[{"a": "没有问题"}]),
            make_real_anchor(self.native, thread=["不是 dict"]),
            make_real_anchor(self.native, thread=[{"q": "ok", "cites": ["x"] * 33}]),
        ):
            saved = self._save_annotation({
                "id": "a-thread-bad", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
                "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
                "engine_anchor": bad,
            })
            self.assertNotIn("engine_anchor", saved["annotations"][0])

    def test_native_missing_disallows_anchor(self) -> None:
        """native 缺失不放行：目标 lane 的 native 被删 → 锚失效回退、笔记保留。"""
        saved_before = self._save_annotation({
            "id": "a-nat", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "笔记保留", "detached": False,
            "engine_anchor": {**self.base_anchor},
        })
        self.assertIn("engine_anchor", saved_before["annotations"][0])
        native_path = self.reader.native_path(self.project_id, "zh-CN")
        backup = native_path.with_suffix(".bak")
        native_path.rename(backup)
        try:
            overlay = self.reader.load_overlay(self.project_id)
            overlay["annotations"] = [saved_before["annotations"][0]]
            resaved = self.reader.save_overlay(self.project_id, overlay, base_revision=overlay["revision"])
            self.assertNotIn("engine_anchor", resaved["annotations"][0])
            self.assertEqual(resaved["annotations"][0]["note"], "笔记保留")
        finally:
            backup.rename(native_path)

    def test_golden_digest_cross_language_with_js(self) -> None:
        """跨语言 golden：与前端 tokenSpaceDigest（真实 JS 运行取值）逐例一致——
        ASCII/CJK/emoji（非 BMP 代理对）/古文字/数学 atom/行内公式/空格/词界。"""
        golden = {
            "ascii": "ae7a3e53",
            "cjk": "bb608f90",
            "emoji": "aba7b9e0",
            "ancient": "f69c1518",
            "math_atom": "c3a952bf",
            "inline_x": "6c123fb4",
            "mixed_boundary": "f15b6334",
            "empty_text": "0002b63f",
        }
        atoms = {"m1": {"kind": "math", "alt": "x^2 + y^2 = z^2", "text": "x²+y²=z²"}}
        cases = {
            "ascii": [{"t": "Silver", "s": 1}, {"t": "vessels", "s": 1}, {"t": "travelled."}],
            "cjk": [{"t": "白"}, {"t": "银", "s": 1}, {"t": "器"}, {"t": "物", "s": 1}, {"t": "。"}],
            "emoji": [{"t": "锚", "s": 1}, {"t": "⚔️"}, {"t": "🌿"}],
            "ancient": [{"t": "𓁹", "s": 1}, {"t": "𓂀"}],
            "math_atom": [{"t": "When", "s": 1}, {"a": "m1"}, {"t": "holds", "s": 1}],
            "inline_x": [{"t": "E", "s": 1}, {"x": "mc^2"}],
            "mixed_boundary": [{"t": "ab"}, {"t": "c", "s": 1}, {"t": "abc"}],
            "empty_text": [{"t": ""}, {"t": "x"}],
        }
        for name, toks in cases.items():
            with self.subTest(case=name):
                self.assertEqual(token_space_digest(toks, atoms), golden[name])

    def _full_toks(self) -> list:
        return self.native["units"]["b-p-1"]["tr"]["zh-CN"]["toks"]

    def _full_digest(self) -> str:
        return token_space_digest(self._full_toks(), self.native.get("atoms") or {})

    # ---- derived 空间独立性（tokens 索引空间跟随 space） ---------------------

    def _synthetic_native(self, *, full_len: int, derived_len: int) -> dict:
        """构造 Full 与 derived_brief 长度可控的合成 native（直接函数级验证精确反例）。"""
        toks = [{"t": f"w{i}", "s": 1} for i in range(full_len)]
        derived = [{"t": f"d{i}", "s": 1} for i in range(derived_len)]
        return {
            "meta": {"lang": "en"},
            "atoms": {},
            "units": {"u1": {"toks": toks, "sents": [[0, full_len]],
                             "lad": {"brief": [], "tn": 0},
                             "tr": {"zh": {"toks": toks, "sents": [[0, full_len]],
                                            "lad": {"derived_brief": {"toks": derived}}}}},
            },
            "chunks": [],
        }

    def test_derived_shorter_than_full_rejected(self) -> None:
        """反例 1（Full长/derived短）：tokens 区间在 Full 内但超出 derived_brief → 拒绝
        （旧实现对 full_toks 验长度，会错误放行）。"""
        native = self._synthetic_native(full_len=10, derived_len=5)
        anchor = {
            "version": 1, "locale": "zh", "space": "derived",
            "digest": token_space_digest(
                native["units"]["u1"]["tr"]["zh"]["lad"]["derived_brief"]["toks"], {}),
            "anchor": {"on": "unit", "unit": "u1", "lang": "zh", "level": 3,
                       "tokens": [0, 7], "quote": {"exact": "", "prefix": "", "suffix": ""}},
        }
        from literature.reader import _engine_anchor_fits_native
        self.assertFalse(_engine_anchor_fits_native(anchor, native))

    def test_derived_longer_than_full_accepted(self) -> None:
        """反例 2（Full短/derived长）：tokens 区间超出 Full 但在 derived_brief 内 → 合法
        （旧实现会错误拒绝）。"""
        native = self._synthetic_native(full_len=10, derived_len=30)
        derived_toks = native["units"]["u1"]["tr"]["zh"]["lad"]["derived_brief"]["toks"]
        anchor = {
            "version": 1, "locale": "zh", "space": "derived",
            "digest": token_space_digest(derived_toks, {}),
            "anchor": {"on": "unit", "unit": "u1", "lang": "zh", "level": 3,
                       "tokens": [0, 25], "quote": {"exact": "", "prefix": "", "suffix": ""}},
        }
        from literature.reader import _engine_anchor_fits_native
        self.assertTrue(_engine_anchor_fits_native(anchor, native))

    def test_quote_full_equality_not_120_prefix(self) -> None:
        """quote 按**全量一致**验证：相同前 120 字符、尾部不同 → 拒绝；完整正确 → 通过。
        （derived 与 full 各自用所在空间核验。）"""
        native = self._synthetic_native(full_len=60, derived_len=60)
        derived_toks = native["units"]["u1"]["tr"]["zh"]["lad"]["derived_brief"]["toks"]
        from literature.reader import _toks_text_like, _engine_anchor_fits_native
        for space, toks in (("derived", derived_toks), ("full", native["units"]["u1"]["toks"])):
            with self.subTest(space=space):
                correct = _toks_text_like(toks, 0, 40, atoms={})
                wrong_tail = correct[:120] + ("X" if not correct.endswith("X") else "Y")
                if wrong_tail == correct:
                    wrong_tail = correct[:-1] + ("X" if not correct.endswith("X") else "Y")
                good = {
                    "version": 1, "locale": "zh", "space": space,
                    "digest": token_space_digest(
                        native["units"]["u1"]["tr"]["zh"]["lad"]["derived_brief"]["toks"] if space == "derived" else toks, {}),
                    "anchor": {"on": "unit", "unit": "u1", "lang": "zh", "level": 3 if space == "derived" else 4,
                               "tokens": [0, 40],
                               "quote": {"exact": correct, "prefix": "", "suffix": ""}},
                }
                bad = {**good, "anchor": {**good["anchor"], "quote": {"exact": wrong_tail, "prefix": "", "suffix": ""}}}
                self.assertTrue(_engine_anchor_fits_native(good, native))
                self.assertFalse(_engine_anchor_fits_native(bad, native))

    def test_quote_truncated_to_storage_cap_still_validated(self) -> None:
        """quote 超过 500 字存储上限：按契约截断比较（expected[:500]），不擅改截断点；
        截断后的存储值仍能通过校验，尾部伪造仍被拒。"""
        native = self._synthetic_native(full_len=140, derived_len=140)
        full_toks = native["units"]["u1"]["toks"]
        from literature.reader import _toks_text_like, _engine_anchor_fits_native, _MAX_ANCHOR_STR
        expected = _toks_text_like(full_toks, 0, 139, atoms={})
        self.assertGreater(len(expected), _MAX_ANCHOR_STR)
        stored = expected[:_MAX_ANCHOR_STR]
        good = {
            "version": 1, "locale": "zh", "space": "full",
            "digest": token_space_digest(full_toks, {}),
            "anchor": {"on": "unit", "unit": "u1", "lang": "zh", "level": 4,
                       "tokens": [0, 139],
                       "quote": {"exact": stored, "prefix": "", "suffix": ""}},
        }
        bad_tail = stored[:-1] + ("X" if not stored.endswith("X") else "Y")
        bad = {**good, "anchor": {**good["anchor"], "quote": {"exact": bad_tail, "prefix": "", "suffix": ""}}}
        self.assertTrue(_engine_anchor_fits_native(good, native))
        self.assertFalse(_engine_anchor_fits_native(bad, native))

    # ---- 章节级锚（on=summary/title，上游 stakeMarks 语义） ------------------

    def _summary_native(self) -> dict:
        return self.reader.load_native(self.project_id, "source")

    def test_natural_summary_anchor_roundtrip(self) -> None:
        """自然章节摘要锚（Lead 实测形态）：{on:summary, unit:null, section:S1,
        lang:en, level:1, tokens:[0,2]}——tokens 索引 lad.take；annotation.block_id 用
        稳定标题块；保存 → 响应 → 新 store 读回完全保留。"""
        native = self._summary_native()
        chunk = next(c for c in native["chunks"] if c["id"] == "S1")
        block_id = chunk["block_id"]
        take = (chunk.get("lad") or {}).get("take") or []
        atoms = native.get("atoms") or {}
        exact = _text_like(take, 0, 2, atoms)
        anchor = {
            "version": 1, "locale": "en", "space": "summary", "digest": token_space_digest(take, atoms),
            "anchor": {"on": "summary", "section": "S1", "lang": "en", "level": 1,
                       "tokens": [0, 2],
                       "quote": {"exact": exact, "prefix": "", "suffix": ""}},
        }
        annotation = {
            "id": "a-sum", "anchor": "block", "block_id": block_id, "pane": "source",
            "start": 0, "end": 0, "color": "y", "note": "章节摘要笔记", "detached": False,
            "engine_anchor": anchor,
        }
        saved = self._save_annotation(annotation)
        self.assertIn("engine_anchor", saved["annotations"][0])
        self.assertEqual(saved["annotations"][0]["engine_anchor"], anchor)
        fresh = ReaderStore(self.store).load_overlay(self.project_id)
        self.assertEqual(fresh["annotations"][0]["engine_anchor"], anchor)

    def test_summary_anchor_failures_keep_note(self) -> None:
        native = self._summary_native()
        chunk = next(c for c in native["chunks"] if c["id"] == "S1")
        block_id = chunk["block_id"]
        take = (chunk.get("lad") or {}).get("take") or []
        atoms = native.get("atoms") or {}

        def base(**overrides):
            anchor = {
                "version": 1, "locale": "en", "space": "summary",
                "digest": token_space_digest(take, atoms),
                "anchor": {"on": "summary", "section": "S1", "lang": "en", "level": 1,
                           "tokens": [0, 2],
                           "quote": {"exact": _text_like(take, 0, 2, atoms), "prefix": "", "suffix": ""}},
            }
            anchor.update(overrides)
            return anchor

        cases = [
            ("缺章节", base(anchor={**base()["anchor"], "section": "S999"}), "b-title-1"),
            ("tokens 越界", base(anchor={**base()["anchor"], "tokens": [0, 999]}), block_id),
            ("产物变化（digest 失配）", base(digest="deadbeef" if token_space_digest(take, atoms) != "deadbeef" else "12345678"), block_id),
            ("block_id 归属不符", base(), "b-p-1"),
            ("quote 与 take 区间不符", base(anchor={**base()["anchor"],
                "quote": {"exact": "完全不同的引文", "prefix": "", "suffix": ""}}), block_id),
        ]
        for name, anchor, ann_block in cases:
            with self.subTest(case=name):
                saved = self._save_annotation({
                    "id": "a-sum-bad", "anchor": "block", "block_id": ann_block, "pane": "source",
                    "start": 0, "end": 0, "color": "y", "note": "笔记保留", "detached": False,
                    "engine_anchor": anchor,
                })
                self.assertEqual(len(saved["annotations"]), 1, name)
                self.assertNotIn("engine_anchor", saved["annotations"][0], name)
                self.assertEqual(saved["annotations"][0]["note"], "笔记保留", name)

    def test_title_space_semantics(self) -> None:
        """title 空间：无 token 流（tokens 必须缺省）；quote 对标题文本包含核验。"""
        native = self._summary_native()
        chunk = next(c for c in native["chunks"] if c["id"] == "S1")
        block_id = chunk["block_id"]
        title = chunk.get("title") or ""
        good = {
            "version": 1, "locale": "en", "space": "title", "digest": token_space_digest([{"t": title}], {}),
            "anchor": {"on": "title", "section": "S1", "lang": "en", "level": 4,
                       "quote": {"exact": title[:6], "prefix": "", "suffix": ""}},
        }
        saved = self._save_annotation({
            "id": "a-title", "anchor": "block", "block_id": block_id, "pane": "source",
            "start": 0, "end": 0, "color": "y", "note": "标题笔记", "detached": False,
            "engine_anchor": good,
        })
        self.assertIn("engine_anchor", saved["annotations"][0])
        # title 带 tokens → 拒绝；quote 不在标题文本 → 拒绝
        bad_tokens = {**good, "anchor": {**good["anchor"], "tokens": [0, 1]}}
        saved = self._save_annotation({
            "id": "a-title-bad1", "anchor": "block", "block_id": block_id, "pane": "source",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": bad_tokens,
        })
        self.assertNotIn("engine_anchor", saved["annotations"][0])
        bad_quote = {**good, "anchor": {**good["anchor"], "quote": {"exact": "不在标题里", "prefix": "", "suffix": ""}}}
        saved = self._save_annotation({
            "id": "a-title-bad2", "anchor": "block", "block_id": block_id, "pane": "source",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": bad_quote,
        })
        self.assertNotIn("engine_anchor", saved["annotations"][0])

    def test_legacy_unit_anchor_no_regression_with_summary_support(self) -> None:
        """旧 unit 锚不回归：summary 支持加入后既有 brief/full 锚行为不变。"""
        saved = self._save_annotation({
            "id": "a-unit", "anchor": "block", "block_id": "b-p-1", "pane": "trans",
            "start": 0, "end": 0, "color": "y", "note": "", "detached": False,
            "engine_anchor": {**self.base_anchor},
        })
        self.assertIn("engine_anchor", saved["annotations"][0])
        self.assertEqual(saved["annotations"][0]["engine_anchor"]["space"], "brief")

if __name__ == "__main__":
    unittest.main()
