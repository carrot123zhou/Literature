"""AIFold 有界批处理改造的独立回归审计（Orca regression auditor）。

只覆盖本文件（不与 tests/test_reader_progress.py / test_reader_batches.py /
test_reader_transport.py 重叠）。聚焦 reader_batches.py 集成后的边界行为：

- 检查点复用与恢复：checkpoint-only resume 必须把命中检查点的 unit **真正恢复进 ladder**，
  不能只改计数（旧存盘节缺失时 `_restore_units` 提前返回 → unit 永远无短层）；
  存盘节过期时恢复的必须是检查点匹配值，不是存盘旧值；
- 并发检查点：同一文件的多个写者实例（source lane / derive lane 各持一个）不得互相覆盖丢条目；
- 部分复用：有效存盘 unit 不重发请求，只补缺失/无效 unit；旧 local 假缓存永不复用、
  也不双倍计费；
- 截断/畸形回复：整体截断的 JSON 一个 unit 都不能收；回复漏段不能冒充完成；
  已有有效旧 unit 不被畸形回复破坏；
- 真实提示词的装箱边界：≤12 unit / 估算输入 ≤6000，覆盖完整且不重复；
- 多批节摘要：批回复的子集 section 不得落进真实 chunk；摘要请求失败不得让该节伪装 ready；
- 超长 unit 句窗：合并结果必须回到整 unit 坐标（ev/brief 不漂移、不丢窗）；
- BEYOND 场景：一个 evidence 槽位含多个自然句（ev_sentences 逐槽一条，不分裂不合并）；
- 巨型参考文献/索引节（>600 unit）：装箱有界、运行时间有限、零 local、检查点完整。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from third_party.textfold import tokens as tf_tokens

from literature.models import Block, Document, Page, TranslationEntry, TranslationLayer
from literature.reader import (
    ReaderJobRunner,
    ReaderLayerService,
    ReaderStore,
    _sections_from_ladder,
    reader_root,
)
from literature.reader_batches import (
    MAX_BATCH_UNITS,
    MAX_INPUT_TOKENS,
    ActivityLog,
    UnitCheckpoints,
    apply_consolidation,
    make_proxy_doc,
    plan_batches,
    plan_clause_windows,
    unit_clause_spans,
)
from literature import reader_apparatus as apparatus
from literature.storage import ProjectStore
from literature.textfold_adapter import (
    SOURCE_LOCALE,
    apply_derived_reply,
    build_ladder_doc,
    derive_context,
    merge_translation_doc,
    section_prompt,
    section_view,
)

from tests.test_reader_fold_run import ScriptedProvider, derive_reply_for, reply_for


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def make_units_document(project_id: str, *, units: int, sentences_per_unit: int = 2,
                        sentence: str = "Silver hoards cluster along the coast road.") -> Document:
    """单章多段文档：S1 含 `units` 个段落，每段 `sentences_per_unit` 句。"""
    blocks = [
        Block(block_id="b-title-1", type="title", order=0, page_index=0,
              source_text="Bibliography", level=1, bbox=[10, 10, 300, 40]),
    ]
    for index in range(units):
        text = " ".join(f"{sentence} (ref {index}.{s})" for s in range(sentences_per_unit))
        blocks.append(Block(
            block_id=f"b-p-{index}", type="paragraph", order=1 + index, page_index=0,
            bbox=[10, 50 + index * 10, 300, 120], source_text=text,
        ))
    return Document(
        project_id=project_id, title="Silk Road Silver", source_language="en",
        page_count=1, pages=[Page(page_index=0, width=600, height=800)],
        blocks=blocks,
    )


def make_units_translation(document: Document, locale: str = "zh-CN") -> TranslationLayer:
    entries = {}
    for block in document.blocks:
        if block.type == "paragraph":
            entries[block.block_id] = TranslationEntry(block_id=block.block_id, text=f"[译] {block.source_text}")
        elif block.type == "title":
            entries[block.block_id] = TranslationEntry(block_id=block.block_id, text=f"[译章] {block.source_text}")
    return TranslationLayer(project_id=document.project_id, locale=locale, entries=entries)


def summarize_source_reply(user: str, *, drop_units: tuple[str, ...] = ()) -> dict:
    """源层回复：逐字取每 unit 的第一句作 takeaway（保证锚点真实存在）。"""
    import re as _re
    markers = _re.findall(r"^\[U(\d+)\] (\w+)", user, flags=_re.MULTILINE)
    units = []
    for number, _role in markers:
        unit_id = f"U{number}"
        if unit_id in drop_units:
            continue
        units.append({
            "id": unit_id, "evidence": [0],
            "takeaway": "Silver hoards cluster along the coast road",
            "reason": "", "topic": "Silver hoards", "role": "claim",
        })
    return {
        "section": {"takeaway": "Silver hoards cluster along the coast road.",
                    "topic": "Silver hoards"},
        "units": units,
    }


class AuditProvider(ScriptedProvider):
    """可编程失败模式的脚本服务：整体截断 / 漏段 / 摘要失败 / 分类与归并应答。"""

    def __init__(self, *, truncated_sections: set[str] | None = None,
                 drop_units: dict[str, tuple[str, ...]] | None = None,
                 fail_summary: bool = False, classifier_kind: str = "body",
                 classifier_mixed: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.truncated_sections = set(truncated_sections or ())
        self.drop_units = dict(drop_units or {})
        self.fail_summary = fail_summary
        self.classifier_kind = classifier_kind
        self.classifier_mixed = classifier_mixed

    def complete(self, *, system: str, user: str):
        from literature.providers import ProviderResponse
        if "classifying one section of a book" in user:
            self.calls.append(("classify", "?"))
            payload = {"kind": self.classifier_kind, "mixed": self.classifier_mixed,
                       "reason": "脚本分类"}
            return ProviderResponse(
                content=json.dumps(payload, ensure_ascii=False), provider="fake",
                model="fake-model", request_id=f"req-{len(self.calls)}",
                input_tokens=10, output_tokens=5, estimated_cost=0.0001,
            )
        if "Consolidate them into ONE set" in user:
            kind = "derive" if "in windows. Consolidate" in user and "Do not translate" in user else "source"
            self.calls.append((f"consolidate-{kind}", "?"))
            if kind == "derive":
                payload = {"takeaway": "译要点整合", "reason": "", "topic": "译要点",
                           "evidence": [0]}
            else:
                payload = {"takeaway": "Silver hoards cluster along the coast road",
                           "role": "claim", "reason": "", "topic": "Silver hoards",
                           "evidence": [0]}
            return ProviderResponse(
                content=json.dumps(payload, ensure_ascii=False), provider="fake",
                model="fake-model", request_id=f"req-{len(self.calls)}",
                input_tokens=100, output_tokens=50, estimated_cost=0.001,
            )
        if self.fail_summary and ("unit takeaways in reading order" in user
                                  or "condensed summaries of consecutive groups" in user):
            raise RuntimeError("模拟章节摘要请求失败")
        kind = "derive" if "source takeaway:" in user else "source"
        section = self._section_of(kind, user)
        self.calls.append((kind, section))
        if section in self.fail_sections:
            raise RuntimeError(f"模拟 {section} 折叠失败")
        if section in self.truncated_sections:
            # 整体截断：JSON 未闭合（解析必须失败，一个 unit 都不能收）。
            raw = json.dumps(reply_for(user) if kind == "source" else derive_reply_for(user),
                             ensure_ascii=False)
            return ProviderResponse(
                content=raw[: max(8, len(raw) // 2)], provider="fake", model="fake-model",
                request_id=f"req-{len(self.calls)}", input_tokens=100, output_tokens=50,
                estimated_cost=0.001,
            )
        if kind == "source":
            payload = summarize_source_reply(user, drop_units=self.drop_units.get(section, ()))
        else:
            payload = derive_reply_for(user)
        return ProviderResponse(
            content=json.dumps(payload, ensure_ascii=False), provider="fake", model="fake-model",
            request_id=f"req-{len(self.calls)}", input_tokens=100, output_tokens=50,
            estimated_cost=0.001,
        )


class BatchAuditBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root / "library")
        self.service = ReaderLayerService(self.store)
        self.reader = ReaderStore(self.store)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # -- 便捷构造 ---------------------------------------------------------

    def _write_units_project(self, *, units: int, sentences_per_unit: int = 2,
                             sentence: str = "Silver hoards cluster along the coast road.",
                             with_translation: bool = False) -> str:
        source = self.root / "book.pdf"
        source.write_bytes(b"%PDF-1.4\n% fixture")
        project = self.store.create_project(source, title="Silk Road Silver")
        document = make_units_document(
            project["project_id"], units=units, sentences_per_unit=sentences_per_unit,
            sentence=sentence,
        )
        self.store.save_document(document)
        if with_translation:
            self.store.save_translation(make_units_translation(document))
        return project["project_id"]

    def _ladder_context(self, project_id: str, locale: str = SOURCE_LOCALE):
        translation = None if locale == SOURCE_LOCALE else self.store.load_translation(project_id, locale)
        _document, doc, sections = self.service._document_context(project_id, locale, translation)
        chunks = {chunk["id"]: chunk for chunk in doc["chunks"] if chunk["units"]}
        return doc, sections, chunks

    def _fold(self, project_id, *, doc, section, chunk, provider, lane="source",
              locale=SOURCE_LOCALE, stored_section=None, section_hash_ok=False,
              checkpoints=None, unit_ids=None, translation=None, glossary_hash="", **kwargs):
        return self.service._fold_section_units(
            project_id, section=section, chunk=chunk, doc=doc, lane=lane, locale=locale,
            provider=provider, model_label="fake-model", service_id="svc",
            translation=translation, glossary_hash=glossary_hash,
            section_hash_ok=section_hash_ok, stored_section=stored_section,
            checkpoints=checkpoints, unit_ids=unit_ids, **kwargs,
        )

    def _checkpoint_path(self, project_id: str) -> Path:
        return reader_root(self.store, project_id) / "fold_checkpoints.json"

    def _merged_context(self, project_id: str):
        """折好原文层后构造 merged（译文层派生用的 doc），并把源层 lad 同步进去。"""
        document = self.store.load_document(project_id)
        translation = self.store.load_translation(project_id, "zh-CN")
        source_ladder = build_ladder_doc(document, locale=SOURCE_LOCALE, translation=None,
                                         source_kind="pdf")
        display = build_ladder_doc(document, locale="zh-CN", translation=translation,
                                   source_kind="pdf")
        merged = merge_translation_doc(source_ladder, display, "zh-CN")
        sections = _sections_from_ladder(source_ladder)
        chunks = {chunk["id"]: chunk for chunk in merged["chunks"] if chunk["units"]}
        return merged, sections, chunks


def _brief_text(unit: dict) -> str:
    lad = unit.get("lad") or {}
    return tf_tokens.plain(lad.get("brief") or [], {})


def _projection(doc, section, chunk) -> dict:
    view = section_view(doc, chunk)
    by_id = {item["id"]: item for item in section.get("units") or []}
    for unit in view["units"]:
        meta = by_id.get(unit["id"]) or {}
        unit["fallback"] = bool(meta.get("fallback"))
        unit["type"] = meta.get("type", "paragraph")
        unit["page_index"] = int(meta.get("page_index", 0))
    return view


def _stored_takeaway(stored: dict, uid: str) -> str:
    for item in stored.get("units") or []:
        if item.get("id") == uid:
            return str(item.get("takeaway") or "")
    return ""


def _lane_units(path: Path, locale: str) -> int:
    """从检查点文件读某个 lane 的 unit 条目数（不依赖内部缓存字段）。"""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return 0
    lane = (data.get("sections") or {}).get(locale) or {}
    total = 0
    for entry in lane.values():
        if isinstance(entry, dict):
            total += len(entry.get("units") or {})
    return total


def _lane_receipts(path: Path, locale: str) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    lane = (data.get("sections") or {}).get(locale) or {}
    return [entry["apparatus"] for entry in lane.values()
            if isinstance(entry, dict) and isinstance(entry.get("apparatus"), dict)]


def _broken_pending(stored: dict, uid: str) -> dict:
    """把存盘投影里的一个 unit 标成待生成（模拟上次失败留下的无效条目）。"""
    data = json.loads(json.dumps(stored))
    for item in data["units"]:
        if item.get("id") == uid:
            item.update({"source": "pending", "takeaway": "", "key": "", "topic": "",
                         "evidence": []})
    return data


# ---------------------------------------------------------------------------
# 1. 检查点复用与恢复（checkpoint-only resume 风险）
# ---------------------------------------------------------------------------


class CheckpointRestoreAuditTests(BatchAuditBase):
    def test_checkpoint_only_resume_restores_units_into_ladder(self) -> None:
        """存盘节缺失时，命中检查点的 unit 必须真正恢复进 ladder（不只是计数复用）。"""
        project_id = self._write_units_project(units=2)
        doc, sections, chunks = self._ladder_context(project_id)
        section = sections[0]
        chunk = chunks[section["id"]]

        first = self._fold(project_id, doc=doc, section=section, chunk=chunk,
                           provider=ScriptedProvider(),
                           checkpoints=self.service.checkpoints(project_id))
        self.assertEqual(set(first["status"].values()), {"model"})
        self.assertEqual(first["calls"], 1)
        fresh = {uid: _brief_text(doc["units"][uid]) for uid in chunk["units"]}
        for uid, text in fresh.items():
            self.assertTrue(text, f"{uid}：首轮折叠后必须有真实短层")

        # checkpoint-only resume：存盘节缺失（stored_section=None）。
        doc2, sections2, chunks2 = self._ladder_context(project_id)
        section2 = sections2[0]
        chunk2 = chunks2[section2["id"]]
        second = self._fold(project_id, doc=doc2, section=section2, chunk=chunk2,
                            provider=ScriptedProvider(),
                            checkpoints=self.service.checkpoints(project_id))
        self.assertEqual(second["calls"], 0, "检查点指纹命中：不得重发请求（不重复计费）")
        self.assertEqual(second["reused_units"], len(chunk2["units"]))
        self.assertEqual(set(second["status"].values()), {"model"})
        for uid in chunk2["units"]:
            restored = _brief_text(doc2["units"][uid])
            self.assertTrue(
                restored,
                f"{uid}：检查点命中却未恢复进 ladder（_restore_units 只认存盘节，"
                f"checkpoint-only resume 后该 unit 永远无短层）",
            )
            self.assertEqual(_norm(restored), _norm(fresh[uid]),
                             "恢复出的短层必须与检查点条目一致")

    def test_checkpoint_reuse_wins_over_stale_stored_section(self) -> None:
        """存盘节过期但检查点指纹匹配：恢复的必须是检查点值，不是存盘节旧值。"""
        project_id = self._write_units_project(units=2)
        doc, sections, chunks = self._ladder_context(project_id)
        section = sections[0]
        chunk = chunks[section["id"]]

        self._fold(project_id, doc=doc, section=section, chunk=chunk,
                   provider=ScriptedProvider(), checkpoints=self.service.checkpoints(project_id))
        fresh = {uid: _brief_text(doc["units"][uid]) for uid in chunk["units"]}

        stale_section = {
            "id": section["id"], "title": section.get("title", ""),
            "takeaway": "旧章节摘要", "topic": "旧主题",
            "units": [
                {"id": uid, "role": "claim", "takeaway": f"STALE {uid}",
                 "key": f"STALE {uid}", "topic": "STALE", "evidence": [0],
                 "source": "model", "type": "paragraph", "page_index": 0, "fallback": False}
                for uid in chunk["units"]
            ],
        }
        doc2, sections2, chunks2 = self._ladder_context(project_id)
        section2 = sections2[0]
        chunk2 = chunks2[section2["id"]]
        stats = self._fold(project_id, doc=doc2, section=section2, chunk=chunk2,
                           provider=ScriptedProvider(), stored_section=stale_section,
                           section_hash_ok=False, checkpoints=self.service.checkpoints(project_id))
        self.assertEqual(stats["calls"], 0)
        for uid in chunk2["units"]:
            restored = _norm(_brief_text(doc2["units"][uid]))
            self.assertEqual(restored, _norm(fresh[uid]),
                             f"{uid}：恢复了存盘节旧值而非检查点匹配值（stale 复用风险）")
            self.assertNotEqual(restored, f"STALE {uid}")

    def test_concurrent_checkpoint_instances_do_not_lose_entries(self) -> None:
        """同一检查点文件的多个写者实例不得互相覆盖（generate 目标 lane 与
        _ensure_source_fold 各建一个实例；后保存者带着旧快照会抹掉先保存者的条目）。"""
        path = self.root / "reader" / "fold_checkpoints.json"
        first = UnitCheckpoints(path)
        second = UnitCheckpoints(path)   # 在 first.save() 之前构造：快照里没有对方的条目
        first.put_unit("source", "S1", "u1", "fp-1",
                       {"id": "u1", "source": "model", "takeaway": "a", "key": "a",
                        "topic": "a", "evidence": [0], "brief": [], "role": "claim"})
        second.put_unit("zh-CN", "S1", "u2", "fp-2",
                        {"id": "u2", "source": "model", "takeaway": "b", "key": "b",
                         "topic": "b", "evidence": [0], "brief": ["x"], "role": "claim"})
        first.save()
        second.save()
        reloaded = UnitCheckpoints(path)
        self.assertIsNotNone(reloaded.unit("source", "S1", "u1", "fp-1"),
                             "后保存实例把先保存实例的条目抹掉了（并发写丢更新）")
        self.assertIsNotNone(reloaded.unit("zh-CN", "S1", "u2", "fp-2"))

    def test_full_run_keeps_source_lane_checkpoints_after_derive(self) -> None:
        """端到端：zh run 的 source 前置折叠与目标派生都写检查点，最终两 lane 条目都在。"""
        project_id = self._write_units_project(units=2, with_translation=True)
        provider = AuditProvider()
        runner = ReaderJobRunner(self.store)
        runner.start_run(project_id, locale="zh-CN",
                         provider_factory=lambda: provider, model_label="fake-model",
                         service_id="svc", binding_source="reader_role")
        deadline = time.time() + 30
        last: dict = {}
        while time.time() < deadline:
            last = runner.current_run(project_id)
            if last.get("status") in ("ready", "failed"):
                break
            time.sleep(0.02)
        self.assertEqual(last.get("status"), "ready", last.get("errors"))
        self.assertGreaterEqual(_lane_units(self._checkpoint_path(project_id), "source"), 1,
                                "source lane 的检查点条目被 derive lane 的保存覆盖丢失")
        self.assertGreaterEqual(_lane_units(self._checkpoint_path(project_id), "zh-CN"), 1,
                                "derive lane 检查点应存在")


# ---------------------------------------------------------------------------
# 2. 部分复用：只发缺失 unit；旧 local 假缓存
# ---------------------------------------------------------------------------


class PartialReuseAuditTests(BatchAuditBase):
    def test_valid_stored_units_not_resent_only_missing_units_sent(self) -> None:
        """section_hash_ok：存盘 model unit 直接复用，请求里只有缺失 unit。"""
        project_id = self._write_units_project(units=3)
        doc, sections, chunks = self._ladder_context(project_id)
        section = sections[0]
        chunk = chunks[section["id"]]

        provider = ScriptedProvider()
        self._fold(project_id, doc=doc, section=section, chunk=chunk, provider=provider,
                   checkpoints=self.service.checkpoints(project_id))
        self.assertEqual(provider.calls, [("source", "S1")])
        stored = _projection(doc, section, chunk)
        fresh = {uid: _brief_text(doc["units"][uid]) for uid in chunk["units"]}

        # 最后一个 unit 失效（模拟上次失败），其余有效：只补它一个。
        doc2, sections2, chunks2 = self._ladder_context(project_id)
        section2 = sections2[0]
        chunk2 = chunks2[section2["id"]]
        last_uid = chunk2["units"][-1]
        provider2 = ScriptedProvider()
        stats = self._fold(project_id, doc=doc2, section=section2, chunk=chunk2,
                           provider=provider2, stored_section=_broken_pending(stored, last_uid),
                           section_hash_ok=True, checkpoints=self.service.checkpoints(project_id))
        self.assertEqual(len(provider2.calls), 1, "只补缺失 unit：恰好一个批请求")
        self.assertEqual(provider2.calls, [("source", "S1")])
        self.assertEqual(stats["status"][last_uid], "model")
        for uid in chunk2["units"][:-1]:
            self.assertEqual(stats["status"][uid], "model")
            self.assertEqual(_norm(_brief_text(doc2["units"][uid])), _norm(fresh[uid]),
                             f"{uid}：复用 unit 必须从存盘投影恢复，短层与旧值一致")

    def test_old_fake_local_cache_never_reused_and_not_double_billed(self) -> None:
        """旧 translation-local 假缓存：不复用为 AI 完成，重折且每批恰好计费一次。"""
        project_id = self._write_units_project(units=2)
        doc, sections, chunks = self._ladder_context(project_id)
        section, chunk = sections[0], chunks[sections[0]["id"]]
        stale_local = {
            "id": section["id"], "title": section.get("title", ""),
            "takeaway": "旧本地摘要", "topic": "旧主题",
            "units": [
                {"id": uid, "role": "para", "takeaway": f"本地摘句 {uid}",
                 "key": f"本地摘句 {uid}", "topic": "本地", "evidence": [0],
                 "source": "local", "type": "paragraph", "page_index": 0, "fallback": False}
                for uid in chunk["units"]
            ],
        }
        provider = ScriptedProvider()
        stats = self._fold(project_id, doc=doc, section=section, chunk=chunk,
                           provider=provider, stored_section=stale_local, section_hash_ok=True)
        self.assertEqual(set(stats["status"].values()), {"model"}, "local 假缓存不得冒充 AI 完成")
        self.assertEqual(len(provider.calls), 1, provider.calls)
        self.assertEqual(stats["batches"], 1, "2 个 unit 无失败 → 1 批，不得重试双倍计费")
        for uid in chunk["units"]:
            self.assertNotEqual(doc["units"][uid].get("source"), "local",
                                "折叠后 source 不得是 local")


# ---------------------------------------------------------------------------
# 3. 截断 / 畸形回复
# ---------------------------------------------------------------------------


class MalformedReplyAuditTests(BatchAuditBase):
    def test_truncated_json_accepts_no_units(self) -> None:
        project_id = self._write_units_project(units=2)
        doc, sections, chunks = self._ladder_context(project_id)
        section, chunk = sections[0], chunks[sections[0]["id"]]
        provider = AuditProvider(truncated_sections={"S1"})
        stats = self._fold(project_id, doc=doc, section=section, chunk=chunk, provider=provider)
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(set(stats["status"].values()), {"pending"}, "截断回复一个 unit 都不能收")
        self.assertTrue(stats["errors"])
        for uid in chunk["units"]:
            self.assertEqual(_brief_text(doc["units"][uid]), "",
                             "截断回复不得写入任何短层")

    def test_reply_missing_unit_not_accepted_and_valid_old_units_preserved(self) -> None:
        """回复漏段：缺的 unit 保持未生成（重试一轮仍缺 → pending），有效旧 unit 不被破坏。"""
        project_id = self._write_units_project(units=3)
        doc, sections, chunks = self._ladder_context(project_id)
        section, chunk = sections[0], chunks[sections[0]["id"]]

        provider1 = ScriptedProvider()
        self._fold(project_id, doc=doc, section=section, chunk=chunk, provider=provider1)
        stored = _projection(doc, section, chunk)
        fresh = {uid: _brief_text(doc["units"][uid]) for uid in chunk["units"]}

        doc2, sections2, chunks2 = self._ladder_context(project_id)
        section2 = sections2[0]
        chunk2 = chunks2[section2["id"]]
        last_uid = chunk2["units"][-1]
        provider2 = AuditProvider(drop_units={"S1": ("U0",)})
        stats = self._fold(project_id, doc=doc2, section=section2, chunk=chunk2,
                           provider=provider2, stored_section=_broken_pending(stored, last_uid),
                           section_hash_ok=True)
        self.assertEqual(stats["status"][last_uid], "pending", "回复缺失的 unit 不得冒充完成")
        self.assertTrue(doc2["units"][last_uid].get("error"),
                        "缺失 unit 必须带 per-unit 错误元数据")
        for uid in chunk2["units"][:-1]:
            self.assertEqual(_norm(_brief_text(doc2["units"][uid])), _norm(fresh[uid]),
                             f"{uid}：已有有效 unit 不得被畸形回复破坏")


# ---------------------------------------------------------------------------
# 4. 装箱边界（真实提示词）
# ---------------------------------------------------------------------------


class SizeBoundAuditTests(BatchAuditBase):
    def test_batches_respect_unit_and_input_bounds_on_real_prompts(self) -> None:
        project_id = self._write_units_project(units=30)
        doc, sections, chunks = self._ladder_context(project_id)
        section, chunk = sections[0], chunks[sections[0]["id"]]
        events: list[dict] = []
        provider = ScriptedProvider()
        stats = self._fold(project_id, doc=doc, section=section, chunk=chunk, provider=provider,
                           checkpoints=self.service.checkpoints(project_id),
                           activity=ActivityLog(_collect(events)))
        self.assertEqual(set(stats["status"].values()), {"model"})
        batch_events = [event for event in events if event.get("event") == "batch"]
        self.assertGreaterEqual(len(batch_events), 3)
        seen: list[str] = []
        for event in batch_events:
            unit_ids = list(event.get("unit_ids") or [])
            seen.extend(unit_ids)
            self.assertLessEqual(len(unit_ids), MAX_BATCH_UNITS, event)
            if len(unit_ids) > 1:
                self.assertLessEqual(int(event.get("input_tokens_est") or 0), MAX_INPUT_TOKENS,
                                     f"多 unit 批超出输入上限：{event}")
            self.assertGreater(int(event.get("input_tokens_est") or 0), 0)
        self.assertEqual(sorted(seen), sorted(chunk["units"]), "批计划必须覆盖全部 unit 且不重复")


# ---------------------------------------------------------------------------
# 5. 多批节摘要
# ---------------------------------------------------------------------------


class SectionSummaryAuditTests(BatchAuditBase):
    def test_multi_batch_subset_section_not_applied_to_chunk(self) -> None:
        """多批节：批回复的子集 section 不得落进真实 chunk（take_section=False）。"""
        project_id = self._write_units_project(units=13)
        doc, sections, chunks = self._ladder_context(project_id)
        section, chunk = sections[0], chunks[sections[0]["id"]]
        prompts: list[str] = []

        class Capturing(ScriptedProvider):
            def complete(self, *, system, user):
                prompts.append(user)
                return super().complete(system=system, user=user)

        provider = Capturing()
        stats = self._fold(project_id, doc=doc, section=section, chunk=chunk, provider=provider)
        self.assertEqual(set(stats["status"].values()), {"model"})
        self.assertGreaterEqual(len(prompts), 2, "13 个 unit 至少两个批")
        self.assertIsNone(chunk.get("lad"),
                          "多批节的批回复 section 是子集摘要，不得当整节摘要落盘")

    def test_summary_failure_does_not_masquerade_ready(self) -> None:
        """多批节的唯一一次摘要请求失败：该节不得被当作已完成/ready。"""
        project_id = self._write_units_project(units=13, with_translation=True)
        provider = AuditProvider(fail_summary=True)
        runner = ReaderJobRunner(self.store)
        runner.start_run(project_id, locale="zh-CN",
                         provider_factory=lambda: provider, model_label="fake-model",
                         service_id="svc", binding_source="reader_role")
        deadline = time.time() + 30
        last: dict = {}
        while time.time() < deadline:
            last = runner.current_run(project_id)
            if last.get("status") in ("ready", "failed", "paused"):
                break
            time.sleep(0.02)
        self.assertNotEqual(
            last.get("status"), "ready",
            f"摘要失败（沿用批内摘要 = 空摘要）的节不得让整篇伪装 ready：{last}",
        )
        surfaced = (
            last.get("errors")
            or [item.get("id") for item in (last.get("failed_sections") or [])]
            or last.get("pending_sections")
        )
        self.assertTrue(surfaced, "摘要失败必须如实进入 errors/failed/pending，不能静默")


# ---------------------------------------------------------------------------
# 6. 超长 unit 句窗
# ---------------------------------------------------------------------------


class LongWindowAuditTests(BatchAuditBase):
    def test_window_merge_maps_back_to_original_sentence_coordinates(self) -> None:
        """超长 unit 按句窗折叠：合并结果必须回到整 unit 坐标（ev 是原始句下标、不丢窗）。"""
        project_id = self._write_units_project(
            units=1, sentences_per_unit=40,
            sentence=("The particular documentary record of this window sentence about the "
                      "silver trade along the northern coast road lists assay values, assay "
                      "marks, workshop marks, the names of customs officials, the caravan "
                      "routes, the toll stations, the seasonal fairs, and the countersigning "
                      "clerks of every manifest entry during the ninth and tenth centuries. ") * 2,
        )
        doc, sections, chunks = self._ladder_context(project_id)
        section, chunk = sections[0], chunks[sections[0]["id"]]
        uid = chunk["units"][0]
        total_sentences = len(doc["units"][uid]["sents"])
        self.assertGreater(total_sentences, 24, "夹具应产生多窗")

        provider = AuditProvider()
        stats = self._fold(project_id, doc=doc, section=section, chunk=chunk, provider=provider)
        self.assertEqual(stats["status"][uid], "model", (stats["errors"], stats))
        kinds = [kind for kind, _section in provider.calls]
        self.assertGreaterEqual(kinds.count("source"), 2, "超长 unit 应拆成多个句窗请求")
        self.assertIn("consolidate-source", kinds, "窗口短层必须经一次模型归并，不是本地拼接")
        lad = doc["units"][uid]["lad"]
        self.assertTrue(lad.get("brief"))
        for index in lad.get("ev") or []:
            self.assertTrue(0 <= int(index) < total_sentences,
                            f"合并后 evidence {index} 越界（句窗坐标未搬回原 unit）")
        self.assertLessEqual(len(lad.get("ev") or []), 3,
                             "归并后的 evidence 必须是有界的（1–3 句），不是窗口 ev 的并集堆叠")

    def test_apply_consolidation_merges_parent_slots_and_bounds_evidence(self) -> None:
        """句窗归并（窗口 ev 已在父坐标时）：derive 译句按父槽合并、evidence 界内、空 takeaway 拒绝。"""
        unit = {"toks": [], "sents": [[0, 2], [2, 4], [4, 6]]}
        atoms: dict = {}
        windows = [
            {"sents_start": 0, "sents_end": 1, "tok_start": 0,
             "lad": {"ev": [0], "brief": [{"t": "win one"}], "tn": 1,
                     "brief_sentences": ["第一句译文。"]}},
            # 同一父句的第二个子句窗：译文续拼在同槽（不重复翻译、不另开槽）。
            {"sents_start": 0, "sents_end": 1, "tok_start": 1,
             "lad": {"ev": [0], "brief": [{"t": "win one cont"}], "tn": 1,
                     "brief_sentences": ["第一句译文续。"]}},
            {"sents_start": 1, "sents_end": 2, "tok_start": 2,
             "lad": {"ev": [1], "brief": [{"t": "win two"}], "tn": 1,
                     "brief_sentences": ["第二句译文。"]}},
        ]
        merged = apply_consolidation(
            {"units": {"u": unit}, "atoms": atoms, "is_cjk": False}, unit,
            {"takeaway": "整段要点", "reason": "", "topic": "整段要点", "evidence": [0, 1]},
            windows, lane="derive", locale="zh-CN", model="fake-model",
        )
        self.assertTrue(merged, "归并结果不得为空")
        self.assertEqual(merged["ev"], [0, 1], "evidence 必须落在原始句号内")
        self.assertEqual(merged["brief_sentences"],
                         ["第一句译文。 第一句译文续。", "第二句译文。"],
                         "同一父句的子句窗译文按槽拼接，不同父句各占一槽")

        # takeaway 为空的归并回复必须整 unit 拒绝（不落空层冒充完成）。
        empty = apply_consolidation(
            {"units": {"u": unit}, "atoms": atoms, "is_cjk": False}, unit,
            {"takeaway": "", "evidence": [0]}, windows,
            lane="derive", locale="zh-CN", model="fake-model",
        )
        self.assertEqual(empty, {}, "空 takeaway 的归并回复不得产出短层")

    def test_window_local_ev_must_be_shifted_to_parent_slots_before_merge(self) -> None:
        """生产窗口 lad 的 ev 是**窗口局部**下标（window_unit 把 sents 重基到 0）。

        多窗 unit（第二窗 sents_start=1）的窗口回复 ev=[0] 实际指向父句 1；归并若直接用
        原始 ev 当父槽，payload evidence=[1] 找不到任何译句 → 归并结果为空 → 整 unit
        永远 pending。窗口短层进归并前必须按 sents_start/parent_sent 搬回父坐标。
        """
        unit = {"toks": [], "sents": [[0, 2], [2, 4]]}
        atoms: dict = {}
        # 模拟 _fold_unit_windows 收集的真实 parts：窗口回复 ev 是窗口局部下标。
        parts = [
            {"sents_start": 0, "sents_end": 1, "tok_start": 0,
             "lad": {"ev": [0], "brief": [{"t": "w1"}], "tn": 1,
                     "brief_sentences": ["第一句译文。"]}},
            {"sents_start": 1, "sents_end": 2, "tok_start": 2,
             "lad": {"ev": [0], "brief": [{"t": "w2"}], "tn": 1,
                     "brief_sentences": ["第二句译文。"]}},
        ]
        merged = apply_consolidation(
            {"units": {"u": unit}, "atoms": atoms, "is_cjk": False}, unit,
            {"takeaway": "整段要点", "reason": "", "topic": "整段要点", "evidence": [0, 1]},
            parts, lane="derive", locale="zh-CN", model="fake-model",
        )
        self.assertTrue(merged,
                        "窗口局部 ev=[0]（第二窗实指父句 1）未经平移就进归并，"
                        f"父槽 1 找不到译句 → 整 unit 归并失败：{merged}")
        self.assertEqual(merged.get("brief_sentences"),
                         ["第一句译文。", "第二句译文。"])

    def test_plan_clause_windows_covers_sentence_without_loss(self) -> None:
        """单句超限的子句窗：窗口并集必须覆盖整句 token（不丢字），父句号保留。"""
        unit = {"toks": [{"t": "word"}] * 40, "sents": [[0, 40]]}
        spans = unit_clause_spans(unit, 0)
        self.assertTrue(spans)
        plans = plan_clause_windows(
            "u", sentence_index=0, sentence_count=1, clause_tokens=spans,
            build_prompt=lambda a, b: ("s", "u" * 100), output_tokens=100,
            max_parts=8,
        )
        self.assertTrue(plans)
        covered: list[tuple[int, int]] = []
        for plan in plans:
            window = plan.window or {}
            self.assertEqual(window.get("parent_sent"), 0)
            self.assertTrue(window.get("clause"))
            covered.append((int(window["tok_start"]), int(window["tok_end"])))
        covered.sort()
        self.assertEqual(covered[0][0], 0)
        self.assertEqual(covered[-1][1], 40, "子句窗必须覆盖整句（不截断丢字）")
        for (_, prev_end), (next_start, _) in zip(covered, covered[1:]):
            self.assertEqual(next_start, prev_end, "子句窗之间不得有缝隙")


# ---------------------------------------------------------------------------
# 7. BEYOND：一个 evidence 槽位含多个自然句
# ---------------------------------------------------------------------------


class EvidenceSlotAuditTests(BatchAuditBase):
    def test_derive_pending_lad_not_accepted_when_reply_missing(self) -> None:
        """派生批：回复漏段时，unit 预置的 pending lad（空短层）不得被当成模型产物接受。

        真实路径：上次部分失败的节存盘后，unit 的 tr[locale] 已有 pending lad（truthy）；
        本次回复又漏了该段 → `_apply_batch` 若只判 `if not lad` 就会把旧 pending 层
        当成派生成功（status=model、无任何短层内容）→ 节伪装 ready。"""
        from literature.reader_batches import BatchPlan
        from literature.textfold_adapter import pending_lad

        project_id = self._write_units_project(units=2, with_translation=True)
        doc, sections, chunks = self._ladder_context(project_id)
        section, chunk = sections[0], chunks[sections[0]["id"]]
        self._fold(project_id, doc=doc, section=section, chunk=chunk, provider=ScriptedProvider())
        merged, _s, merged_chunks = self._merged_context(project_id)
        source_doc, _ss, source_chunks = self._ladder_context(project_id)
        ReaderLayerService._sync_source_lads(merged, source_doc, source_chunks[chunk["id"]])
        merged_chunk = merged_chunks[chunk["id"]]
        uid = merged_chunk["units"][0]
        merged["units"][uid].setdefault("tr", {}).setdefault("zh-CN", {})["lad"] = pending_lad(
            unit=merged["units"][uid])
        _system, _user, _evidence = derive_context(merged, merged_chunk, locale="zh-CN",
                                                   translation=None)
        payload = {"section": {"takeaway": "要点。", "topic": "要点"}, "units": []}  # 漏段
        plan = BatchPlan(units=(uid,), window=None, system="s", user="u",
                         input_tokens=1, output_tokens=1)
        accepted, invalid = self.service._apply_batch(
            section=section, chunk=merged_chunk, doc=merged, plan=plan, payload=payload,
            lane="derive", locale="zh-CN", model_label="fake-model", warnings=[],
            translation=None,
        )
        self.assertEqual(accepted, {}, "pending lad 是 truthy 空层，不得被当成派生成功")
        self.assertIn(uid, invalid, "回复漏段的 unit 必须进 invalid（保持未生成）")
        lad = merged["units"][uid]["tr"]["zh-CN"].get("lad") or {}
        self.assertFalse(lad.get("brief") or lad.get("brief_sentences"),
                         "漏段 unit 不得留下任何派生短层")

    def _folded_merged(self, project_id: str):
        doc, sections, chunks = self._ladder_context(project_id)
        section, chunk = sections[0], chunks[sections[0]["id"]]
        self._fold(project_id, doc=doc, section=section, chunk=chunk, provider=ScriptedProvider())
        merged, _sections, merged_chunks = self._merged_context(project_id)
        # 用**已折好的**源层 doc 同步（fresh ladder 没有折叠产物）。
        ReaderLayerService._sync_source_lads(merged, doc, chunk)
        merged_chunk = merged_chunks[chunk["id"]]
        return merged, merged_chunk

    def test_one_slot_with_multiple_natural_sentences_accepts_single_item(self) -> None:
        """BEYOND 书场景：一个 evidence 槽位的「句」里含多个自然句，模型按槽返回一条
        ev_sentences（内含两句译文）→ 必须接受，不因条数≠句数拒绝。"""
        project_id = self._write_units_project(units=1, with_translation=True)
        merged, merged_chunk = self._folded_merged(project_id)
        uid = merged_chunk["units"][0]
        _system, _user, evidence = derive_context(merged, merged_chunk, locale="zh-CN",
                                                  translation=None)
        slots = len(evidence[0][1])
        self.assertEqual(slots, 1, "夹具源层应只有一个 evidence 槽位")
        payload = {
            "section": {"takeaway": "要点。", "topic": "要点"},
            "units": [{
                "id": "U0", "takeaway": "译要点一", "reason": "", "topic": "译要点",
                "ev_sentences": ["第一句的译文。第二句的译文也在同一条里。"],
            }],
        }
        warnings, derived, fallback = apply_derived_reply(
            merged, merged_chunk, payload, locale="zh-CN", model="fake-model",
            unit_evidence=evidence,
        )
        self.assertEqual(derived, 1, f"单槽多句应接受：{warnings}")
        self.assertEqual(fallback, 0)
        lad = merged["units"][uid]["tr"]["zh-CN"]["lad"]
        self.assertEqual(len(lad["brief_sentences"]), len(lad["ev"]),
                         "brief_sentences 与 evidence 槽位必须 1:1")
        self.assertIn("第二句的译文", lad["brief_sentences"][0],
                      "槽内的多个自然句必须整体保留在同一条里")

    def test_slot_split_into_multiple_items_rejected_not_truncated(self) -> None:
        """模型把槽拆成多条 ev_sentences：数量不一致 → 整 unit 拒绝，不裁剪不猜对应。"""
        project_id = self._write_units_project(units=1, with_translation=True)
        merged, merged_chunk = self._folded_merged(project_id)
        uid = merged_chunk["units"][0]
        _system, _user, evidence = derive_context(merged, merged_chunk, locale="zh-CN",
                                                  translation=None)
        slots = len(evidence[0][1])
        payload = {
            "section": {"takeaway": "要点。", "topic": "要点"},
            "units": [{
                "id": "U0", "takeaway": "译要点一", "reason": "", "topic": "译要点",
                "ev_sentences": [f"第 {i} 条译句。" for i in range(slots + 1)],
            }],
        }
        _warnings, _derived, fallback = apply_derived_reply(
            merged, merged_chunk, payload, locale="zh-CN", model="fake-model",
            unit_evidence=evidence,
        )
        self.assertEqual(fallback, 1, "条数与槽位数不一致必须整 unit 拒绝")
        lad = merged["units"][uid]["tr"]["zh-CN"].get("lad") or {}
        self.assertFalse(lad.get("brief_sentences"),
                         "被拒绝的 unit 不得留下任何部分派生短层")


# ---------------------------------------------------------------------------
# 8. 装置内容 AI 分类（用户 2026-10-27 批准的闸门）
# ---------------------------------------------------------------------------


class ApparatusGateAuditTests(BatchAuditBase):
    def test_explicit_titles_only_and_body_verdict_never_suppressed(self) -> None:
        """候选只认显式装置标题；分类 verdict=body 时绝不启发式自动抑制。"""
        self.assertEqual(apparatus.candidate_kind("Index"), "index")
        self.assertEqual(apparatus.candidate_kind("Subject Index"), "index")
        self.assertEqual(apparatus.candidate_kind("Bibliography"), "bibliography")
        self.assertEqual(apparatus.candidate_kind("Works Cited"), "bibliography")
        self.assertEqual(apparatus.candidate_kind("参考文献"), "bibliography")
        self.assertIsNone(apparatus.candidate_kind("Archives"),
                          "非显式 backmatter 名称不得成为候选（无启发式）")
        self.assertIsNone(apparatus.candidate_kind("The Silver Trade"))
        self.assertIsNone(apparatus.candidate_kind(""))

    def test_ai_confirmed_bibliography_suppressed_with_receipt_and_full_preserved(self) -> None:
        """AI 确认 bibliography 且不混合 → 全 unit 抑制：零折叠调用、Full 保留、收据落盘。"""
        project_id = self._write_units_project(units=4)   # 标题块就是 "Bibliography"
        provider = AuditProvider(classifier_kind="bibliography")
        result = self.service.generate(project_id, locale=SOURCE_LOCALE, provider=provider,
                                       model_label="fake-model", service_id="svc", mode="auto")
        self.assertEqual(result.get("failed_sections") or [], [], result.get("warnings"))
        kinds = [kind for kind, _ in provider.calls]
        self.assertIn("classify", kinds, "显式标题候选必须先过一次 AI 分类")
        self.assertNotIn("source", kinds,
                         "装置节确认后零短层调用（不为 4 段写 takeaway）")
        doc, sections, chunks = self._ladder_context(project_id)
        stored = self.service._stored_sections(project_id, SOURCE_LOCALE)[0]
        section_stored = stored.get(sections[0]["id"]) or {}
        for item in section_stored.get("units") or []:
            self.assertEqual(item.get("source"), "suppressed", "装置 unit 应标 suppressed")
            # Full 保留：存盘投影的 full 仍是原文逐字（装置节只是跳过短层）。
            self.assertTrue(str(item.get("full") or "").strip())
        # 收据绑定模型/服务/节 digest：换模型后指纹不匹配 → 必须重新分类。
        receipts = _lane_receipts(self._checkpoint_path(project_id), "source")
        self.assertTrue(receipts, "分类收据必须落盘")
        self.assertTrue(receipts, "分类收据必须落盘")
        receipt = receipts[0]
        self.assertEqual(receipt.get("service_id"), "svc")
        self.assertEqual(receipt.get("model"), "fake-model")
        self.assertTrue(receipt.get("affirmative"))

    def test_classifier_failure_leaves_section_pending_not_suppressed(self) -> None:
        """分类请求失败：该节如实失败/待补，绝不猜、不抑制。"""
        project_id = self._write_units_project(units=3)

        class FailingClassifier(AuditProvider):
            def complete(self, *, system: str, user: str):
                if "classifying one section of a book" in user:
                    raise RuntimeError("模拟分类服务不可用")
                return super().complete(system=system, user=user)

        result = self.service.generate(project_id, locale=SOURCE_LOCALE,
                                       provider=FailingClassifier(), model_label="fake-model",
                                       service_id="svc", mode="auto")
        failed_ids = {
            item.get("id") if isinstance(item, dict) else str(item)
            for item in result.get("failed_sections") or []
        }
        self.assertTrue(failed_ids, "分类失败必须如实进入 failed_sections")
        doc, sections, _chunks = self._ladder_context(project_id)
        stored = self.service._stored_sections(project_id, SOURCE_LOCALE)[0]
        for item in (stored.get(sections[0]["id"]) or {}).get("units") or []:
            self.assertNotEqual(item.get("source"), "suppressed",
                                "分类失败不得把正文节标成装置抑制")

    def test_mixed_verdict_keeps_normal_fold(self) -> None:
        """mixed=true 的装置候选：不得抑制，照常折叠。"""
        project_id = self._write_units_project(units=3)
        provider = AuditProvider(classifier_kind="bibliography", classifier_mixed=True)
        result = self.service.generate(project_id, locale=SOURCE_LOCALE, provider=provider,
                                       model_label="fake-model", service_id="svc", mode="auto")
        self.assertEqual(result.get("failed_sections") or [], [], result.get("warnings"))
        doc, sections, _chunks = self._ladder_context(project_id)
        stored = self.service._stored_sections(project_id, SOURCE_LOCALE)[0]
        sources = {item.get("source")
                   for item in (stored.get(sections[0]["id"]) or {}).get("units") or []}
        self.assertEqual(sources, {"model"}, "混合节必须照常折成 model，不抑制")


# ---------------------------------------------------------------------------
# 9. 巨型参考文献 / 索引节（>600 unit）
# ---------------------------------------------------------------------------


class HugeSectionAuditTests(BatchAuditBase):
    def test_over_six_hundred_units_planned_bounded_and_folded_finite(self) -> None:
        project_id = self._write_units_project(
            units=620, sentences_per_unit=1, sentence="Andersen 1971, Silk Road hoards, p. 34.",
        )
        doc, sections, chunks = self._ladder_context(project_id)
        section, chunk = sections[0], chunks[sections[0]["id"]]
        targets = list(chunk["units"])
        self.assertGreater(len(targets), 600)

        def build_prompt(batch):
            proxy_doc, proxy_chunk = make_proxy_doc(doc, chunk, list(batch))
            return section_prompt(proxy_doc, proxy_chunk)

        started = time.monotonic()
        plans = plan_batches(targets, build_prompt=build_prompt)
        plan_seconds = time.monotonic() - started
        self.assertLess(plan_seconds, 30.0, "620 unit 的批规划必须在有限时间完成")
        covered: list[str] = []
        for plan in plans:
            self.assertLessEqual(len(plan.units), MAX_BATCH_UNITS)
            if len(plan.units) > 1:
                self.assertLessEqual(plan.input_tokens, MAX_INPUT_TOKENS,
                                     f"多 unit 批输入超限：{plan.label} {plan.input_tokens}")
            covered.extend(plan.units)
        self.assertEqual(sorted(covered), sorted(targets), "批计划必须覆盖全部 unit 且不重复")

        events: list[dict] = []
        provider = ScriptedProvider()
        started = time.monotonic()
        stats = self._fold(project_id, doc=doc, section=section, chunk=chunk, provider=provider,
                           checkpoints=self.service.checkpoints(project_id),
                           activity=ActivityLog(_collect(events)))
        elapsed = time.monotonic() - started
        self.assertEqual(set(stats["status"].values()), {"model"}, stats["errors"])
        self.assertEqual(len(provider.calls), len(plans), "每个计划批恰好一次请求（无双倍计费）")
        self.assertLess(elapsed, 120.0, f"620 unit 折叠耗时过长：{elapsed:.1f}s")
        saved = UnitCheckpoints(self._checkpoint_path(project_id)).section(
            "source", section["id"]).get("units") or {}
        self.assertGreaterEqual(len(saved), 600, "巨型节的检查点必须完整落盘")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    """文本级比较用的归一化（token 重建的空格/句点差异不算内容差异）。"""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def _collect(events: list[dict]):
    def sink(payload: dict) -> None:
        events.append(payload)
    return sink


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
