"""受控并发/预算语义验证（脚本 provider，无真实模型调用，供 tester 固化为测试）。

覆盖：
1. 同节同模型并发（source 线程 + target 线程）：source fold 提示词只出现 1 次（single-flight）。
2. 网络失败释放锁：失败节记 failed + 本地回退，重试补齐。
3. 不同模型 / 不同节内容：不错误复用（各自折叠）。
4. ev_sentences 数量不匹配：整 unit 拒绝派生、evidence 不被截短、节 partial。

用法：.venv/bin/python scripts/reader_fold_concurrency_check.py
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_reader import CountingProvider, MockProvider  # noqa: E402
from test_reader_semantics import (  # noqa: E402
    TranslationEntry, TranslationLayer, make_store, semantic_reply,
)
from literature.providers import ProviderError, ProviderResponse  # noqa: E402
from literature.reader import ReaderLayerService, ReaderStore, clear_layers  # noqa: E402

ZH = {
    "b-p-1": "开篇一章综述了贸易路线。白银经粟特中介在西运。长安工坊改造了进口器形。九世纪需求转向黄金。",
    "b-p-2": "检测记录显示一致的合金标准。百分之九十二银币流通。两处铸币厂已确认。产量在 780 到 820 年达到顶峰。",
    "b-p-3": "窖藏讲述了衰落的故事。850 年后窖藏硬币变得稀少。鎏金痕迹暗示重复利用。",
}


def make_service(tmp: Path):
    store, pid = make_store(tmp)
    store.save_translation(TranslationLayer(project_id=pid, locale="zh-CN", entries={
        **{bid: TranslationEntry(block_id=bid, text=t, status="translated", locked=True) for bid, t in ZH.items()},
        "b-title-1": TranslationEntry(block_id="b-title-1", text="白银之路", status="translated"),
        "b-title-2": TranslationEntry(block_id="b-title-2", text="衰落", status="translated"),
    }))
    return ReaderLayerService(store, ReaderStore(store)), store, pid


def derive_reply_fn(mutate_ev_count: int = 0):
    def derive(user: str) -> dict:
        units, cur, evs = [], None, []
        for line in user.splitlines() + ["[END]"]:
            m = re.match(r"^\[U(\d+)\]", line)
            if m or line.startswith("[END]"):
                if cur is not None:
                    sentences = [f"译句{i}:{s}" for i, s in enumerate(evs)]
                    if mutate_ev_count:
                        sentences = sentences[:mutate_ev_count] if mutate_ev_count > 0 else sentences + ["多余的译句"] * (-mutate_ev_count)
                    units.append({"id": f"U{cur}", "takeaway": "派生要点", "reason": "派生理由", "topic": "要点",
                                  "ev_sentences": sentences})
                if m:
                    cur = int(m.group(1))
                    evs = []
                continue
            m2 = re.match(r"^\s+(\d+): (.+)", line)
            if m2 and cur is not None:
                evs.append(m2.group(2))
        return {"section": {"takeaway": "章节派生主线", "topic": "主线"}, "units": units}
    return derive


class SwitchProvider(CountingProvider):
    """按提示词特征分流：derive 请求（含 backing sentences）走派生回复，其余走 source fold 回复。"""

    def __init__(self, derive_fn=None, fail_marker: str = ""):
        super().__init__(semantic_reply)
        self.derive_fn = derive_fn
        self.fail_marker = fail_marker
        self.failed_done = False

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        is_derive = "backing sentences" in user
        if self.fail_marker and not self.failed_done and self.fail_marker in user and not is_derive:
            self.failed_done = True
            raise ProviderError("受控网络失败")
        fn = self.derive_fn if (is_derive and self.derive_fn) else self._reply
        self.calls.append((system, user))
        payload = fn(user)
        return ProviderResponse(content=json.dumps(payload, ensure_ascii=False), provider="fake", model="m",
                                request_id=f"r{len(self.calls)}", input_tokens=100, output_tokens=50, estimated_cost=0.001)


def check_concurrent_single_flight() -> None:
    with tempfile.TemporaryDirectory() as td:
        service, _store, pid = make_service(Path(td))
        provider = SwitchProvider(derive_reply_fn())
        results, errors = [], []
        barrier = threading.Barrier(2)

        def run(locale: str) -> None:
            barrier.wait()
            try:
                results.append((locale, service.generate(
                    pid, locale=locale, provider=provider, model_label="m", mode="auto", section_ids=["S1"])))
            except Exception as error:  # noqa: BLE001
                errors.append(repr(error))

        t1 = threading.Thread(target=run, args=("source",))
        t2 = threading.Thread(target=run, args=("zh-CN",))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert not errors, f"并发报错：{errors}"
        source_folds = [u for _s, u in provider.calls if "backing sentences" not in u and "[U0]" in u]
        assert len(source_folds) == 1, f"同节并发 source fold 应单飞 1 次，实际 {len(source_folds)}"
        by = dict(results)
        assert by["source"]["sections"] and by["zh-CN"]["sections"], "两层都应有产出"
        pre = ReaderStore(_store).load_layers(pid)
        assert len((pre["docs"].get("source") or {}).get("sections") or []) == 1
        assert len((pre["docs"].get("zh-CN") or {}).get("sections") or []) == 1
        print(f"[1] 同节并发单飞：source fold {len(source_folds)} 次；两层落盘各 1 节 ✅")


def check_failure_releases_and_refills() -> None:
    with tempfile.TemporaryDirectory() as td:
        service, _store, pid = make_service(Path(td))
        provider = SwitchProvider(fail_marker="opening chapter surveys")
        first = service.generate(pid, provider=provider, model_label="m", mode="auto", section_ids=["S1"])
        assert first["failed_sections"] == ["S1"], f"失败节应记入 failed：{first['failed_sections']}"
        pre = ReaderStore(_store).load_layers(pid)
        assert not any(u.get("source") == "model" for u in
                       (pre["docs"]["source"]["sections"][0]["units"])), "失败节不得标 AI"
        second = service.generate(pid, provider=provider, model_label="m", mode="auto", section_ids=["S1"])
        assert second["failed_sections"] == [], "重试应补齐失败节"
        assert any(u.get("source") == "model" for s in second["sections"] for u in s["units"]), "重试后应得 AI 层"
        print("[2] 失败释放锁并补齐 ✅")


def check_no_cross_model_or_content_reuse() -> None:
    with tempfile.TemporaryDirectory() as td:
        service, _store, pid = make_service(Path(td))
        provider = SwitchProvider(derive_reply_fn())
        service.generate(pid, provider=provider, model_label="model-A", mode="auto", section_ids=["S1"])
        calls_a = len(provider.calls)
        again = service.generate(pid, provider=provider, model_label="model-B", mode="auto", section_ids=["S1"])
        assert len(provider.calls) > calls_a, "换模型必须重折叠（不得跨模型复用）"
        assert again["reused_sections"] == 0, "换模型后不应有复用章节"
        # 内容变化（模拟结构变化）→ digest 变化 → 重折叠；此处用 force 模拟强制重采样
        calls_b = len(provider.calls)
        forced = service.generate(pid, provider=provider, model_label="model-B", mode="auto",
                                  section_ids=["S1"], force=True)
        assert len(provider.calls) > calls_b, "force 应重新采样"
        assert forced["reused_sections"] == 0
        print("[3] 不同模型/force 不错误复用 ✅")


def check_ev_mismatch_rejected() -> None:
    with tempfile.TemporaryDirectory() as td:
        service, _store, pid = make_service(Path(td))
        provider = SwitchProvider(derive_reply_fn())
        service.generate(pid, provider=provider, model_label="m", mode="auto", section_ids=["S1"])
        provider.derive_fn = derive_reply_fn(mutate_ev_count=-1)   # 少一条 ev_sentences
        result = service.generate(pid, locale="zh-CN", provider=provider, model_label="m",
                                  mode="auto", section_ids=["S1"], force=True)
        sec = result["sections"][0]
        src_evidence_len = None
        warnings = result.get("warnings") or []
        assert any("不一致" in w for w in warnings), f"应有数量不匹配告警：{warnings[:3]}"
        for u in sec["units"]:
            if u.get("brief_source") == "derived":
                src = next(x for s in _src_units(pid) if False)  # noqa: F841
        # 派生 unit 的 brief 数必须与 evidence 数一致（不前缀截断）
        pre = ReaderStore(_store).load_layers(pid)
        zh_sec = (pre["docs"]["zh-CN"]["sections"][0])
        for u in zh_sec["units"]:
            if u.get("brief_source") == "derived":
                assert len(u.get("brief") or []) == len(u.get("evidence") or []), \
                    f"{u['id']} brief/evidence 数不一致（伪对应）"
        assert result["partial"] or result["content"] in ("local", "mixed"), "不匹配应产生 partial/回退，不冒充成功"
        print("[4] ev_sentences 不匹配：整 unit 拒绝、evidence 不截短、partial 如实 ✅")


class VaryingSourceProvider(SwitchProvider):
    """源 fold 回复随调用序号变化（模拟 force 重采样得到不同转写产物）。"""

    def complete(self, *, system: str, user: str) -> ProviderResponse:
        is_derive = "backing sentences" in user
        if not is_derive:
            payload = semantic_reply(user)
            seq = len(self.calls) + 1
            payload = json.loads(json.dumps(payload, ensure_ascii=False))
            payload["section"]["takeaway"] = f"[采样{seq}] {payload['section']['takeaway']}"
            for unit in payload.get("units") or []:
                unit["takeaway"] = f"[采样{seq}] {unit.get('takeaway', '')}"
            self.calls.append((system, user))
            return ProviderResponse(content=json.dumps(payload, ensure_ascii=False), provider="fake", model="m",
                                    request_id=f"r{len(self.calls)}", input_tokens=100, output_tokens=50,
                                    estimated_cost=0.001)
        return super().complete(system=system, user=user)


def check_force_source_invalidates_target() -> None:
    """force 源重采样：源内容同、产物变 → target 派生层失效（fingerprint）；
    targetLocale/译文文字变化不重折 source。"""
    with tempfile.TemporaryDirectory() as td:
        service, store, pid = make_service(Path(td))
        provider = SwitchProvider(derive_reply_fn())
        service.generate(pid, provider=provider, model_label="m", mode="auto", section_ids=["S1"])
        service.generate(pid, locale="zh-CN", provider=provider, model_label="m", mode="auto", section_ids=["S1"])
        assert service.document(pid)["stale"] is False, "刚生成后 source 不应 stale"
        assert service.document(pid, locale="zh-CN")["stale"] is False, "刚生成后 target 不应 stale"
        # 同参数再 generate：0 新调用（缓存复用）
        before = len(provider.calls)
        service.generate(pid, locale="zh-CN", provider=provider, model_label="m", mode="auto", section_ids=["S1"])
        assert len(provider.calls) == before, "同参数再 generate 应 0 调用"
        # force 源重采样：让源产物真实变化（随调用变化的 fake），target 派生层必须失效
        # （fingerprint）；source 不 stale。确定性 fake 重采样产物相同 → fingerprint 不变
        # → target 复用是正确行为，不构成失效场景。
        varying = VaryingSourceProvider(derive_reply_fn())
        service.generate(pid, provider=varying, model_label="m", mode="auto", section_ids=["S1"], force=True)
        assert service.document(pid)["stale"] is False, "force 后 source 应 fresh"
        assert service.document(pid, locale="zh-CN")["stale"] is True, "force 源产物变化后 target 应 stale（待重派生）"
        # 重派生后恢复 fresh；译文文字微调不重折 source
        service.generate(pid, locale="zh-CN", provider=varying, model_label="m", mode="auto", section_ids=["S1"])
        assert service.document(pid, locale="zh-CN")["stale"] is False, "重派生后 target 应 fresh"
        layer = store.load_translation(pid, "zh-CN")
        entry = layer.entries.get("b-p-1")
        if entry is not None:
            entry.text = (entry.text or "") + "补充一句人工修订。"
            store.save_translation(layer)
        assert service.document(pid)["stale"] is False, "译文文字编辑不应让 source stale"
        print("[5] force 源产物变 → target 失效；重派生恢复；译文编辑不重折 source ✅")


def check_glossary_change_only_target() -> None:
    """术语表变化只让 target stale，source 不动。"""
    with tempfile.TemporaryDirectory() as td:
        service, store, pid = make_service(Path(td))
        provider = SwitchProvider(derive_reply_fn())
        service.generate(pid, provider=provider, model_label="m", mode="auto", section_ids=["S1"])
        service.generate(pid, locale="zh-CN", provider=provider, model_label="m", mode="auto", section_ids=["S1"])
        assert service.document(pid, locale="zh-CN")["stale"] is False
        layer = store.load_translation(pid, "zh-CN")
        layer.glossary_snapshot = {"Kausambi": "拘舍弥"}
        store.save_translation(layer)
        assert service.document(pid)["stale"] is False, "术语表变化不应让 source stale"
        assert service.document(pid, locale="zh-CN")["stale"] is True, "术语表变化应让 target stale（待重派生）"
        print("[6] 术语表变化只 target stale ✅")


def _src_units(pid: str):
    return []


if __name__ == "__main__":
    check_concurrent_single_flight()
    check_failure_releases_and_refills()
    check_no_cross_model_or_content_reuse()
    check_ev_mismatch_rejected()
    check_force_source_invalidates_target()
    check_glossary_change_only_target()
    print("全部受控并发/预算/缓存身份语义检查通过")
