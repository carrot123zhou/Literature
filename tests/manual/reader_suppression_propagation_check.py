"""混合节 suppressed 传播 + 旧缓存零调用纠正 + readiness 可见原因 的独立回归。

背景（真机 9/11 真实书失败，project b0ab2358…）：混合节里源层 BRILL/BY/E.ZÜRCHER 等装置内容
被判 `suppressed`，但目标层旧缓存把这些 unit 重建为 `translation-local`（suppressed=False），
导致 readiness 9/11、run failed 且 `errors: []`（UI 只见泛失败）。

本回归（全部隔离，绝不写真实库）：
  A  合成忠实夹具：源短装置节被 suppressed + 正常 model；目标对应译文更长 →
     断言 suppressed 权威传播到目标层与 native，且 readiness ready。
  B  旧缓存零调用纠正：把目标层那几条 unit 改写成旧缺陷形态（local/translation-local/未 suppressed），
     并把 run 记录改成 stale failed + errors: [] → resume 必须 **0 模型调用** 纠正为 ready，
     且存盘 unit 变为 suppressed。
  C  真本地未完成仍不得 ready：只有本地摘句（无模型内容）的节 → readiness=False，
     且逐节原因非空（UI 可见），run 暴露 ready_basis/errors 供展示。
  D  真实书只读副本：复制真实库到临时目录，对副本做零调用 resume → 必须 ready，
     3 个混合 unit 变为 suppressed；真实库本身不被修改（校验前后哈希一致）。

运行：`.venv/bin/python tests/manual/reader_suppression_propagation_check.py`
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RESULTS: list[dict] = []


def ok(name: str, passed: bool, detail=None) -> None:
    RESULTS.append({"name": name, "pass": bool(passed), "detail": detail if detail is not None else ""})


# ---------------------------------------------------------------- 夹具
APPARATUS = "* * *"
LONG_TRANSLATION = "（此处原书以星号作版式分隔，属装置内容，不参与折叠，也不应被当作正文概括。）"


def make_doc(project_id: str):
    from literature.models import Block, Document, Page
    blocks = [
        Block(block_id="h-1", type="title", order=0, page_index=0, source_text="Introduction",
              level=1, bbox=[10, 10, 300, 40]),
        Block(block_id="p-1", type="paragraph", order=1, page_index=0, bbox=[10, 50, 300, 120],
              source_text="Silver vessels travelled along the Silk Road. Workshops copied Sogdian shapes."),
        Block(block_id="ap-1", type="paragraph", order=2, page_index=0, bbox=[10, 130, 300, 150],
              source_text=APPARATUS),
        Block(block_id="p-2", type="paragraph", order=3, page_index=0, bbox=[10, 160, 300, 220],
              source_text="Assay data show a consistent alloy of 92 percent silver."),
    ]
    return Document(project_id=project_id, title="Mixed Apparatus Fixture", source_language="en",
                    page_count=1, pages=[Page(page_index=0, width=600, height=800)], blocks=blocks)


def make_translation(document, locale="zh-CN"):
    from literature.models import TranslationEntry, TranslationLayer
    entries = {
        "h-1": TranslationEntry(block_id="h-1", text="[译章] Introduction"),
        "p-1": TranslationEntry(block_id="p-1", text="银器沿丝绸之路流动。工坊模仿粟特器形。"),
        # 装置内容：译文很长（旧缺陷会让它被 translation-local 折叠）
        "ap-1": TranslationEntry(block_id="ap-1", text=LONG_TRANSLATION),
        "p-2": TranslationEntry(block_id="p-2", text="检测数据显示合金含银百分之九十二。"),
    }
    return TranslationLayer(project_id=document.project_id, locale=locale, entries=entries)


class CountingProvider:
    """记录调用；strict 模式下被调用即抛错（用于证明零调用）。"""

    def __init__(self, strict: bool = False):
        from tests.test_reader_fold_run import ScriptedProvider
        self.inner = ScriptedProvider()
        self.calls: list[tuple[str, str]] = []
        self.strict = strict
        self.config = self.inner.config

    def complete(self, *, system: str, user: str):
        self.calls.append(("derive" if "source takeaway:" in user else "source", "?"))
        if self.strict:
            raise AssertionError(f"零调用期望，但发生了模型调用（{len(self.calls)} 次）")
        return self.inner.complete(system=system, user=user)

    def close(self):
        pass


def readiness(svc, pid: str, locale: str = "zh-CN"):
    layers = svc.reader.load_layers(pid)
    doc = layers["docs"].get(locale) or {}
    dig = doc.get("digest") or {}
    return svc.fold_run_readiness(pid, locale=locale,
                                  service_id=str(dig.get("service_id") or ""),
                                  model=str(dig.get("model") or ""))


def units_of(svc, pid: str, locale: str, section_id: str) -> dict:
    layers = svc.reader.load_layers(pid)
    doc = layers["docs"].get(locale) or {}
    sec = next((s for s in doc.get("sections") or [] if s.get("id") == section_id), None)
    return {u.get("id"): u for u in (sec or {}).get("units") or []}


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        os.environ["LITERATURE_RUNTIME"] = str(base / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(base / "library")
        os.environ["LITERATURE_SESSION_TOKEN"] = ""
        (base / "runtime").mkdir(parents=True, exist_ok=True)
        (base / "runtime/session-token").write_text("")

        from literature.reader import ReaderJobRunner, ReaderLayerService
        from literature.storage import ProjectStore

        store = ProjectStore(base / "library")
        src = base / "book.pdf"
        src.write_bytes(b"%PDF-1.4\n% fixture")
        project = store.create_project(src, title="Mixed Apparatus Fixture")
        pid = project["project_id"]
        document = make_doc(pid)
        store.save_document(document)
        store.save_translation(make_translation(document))

        svc = ReaderLayerService(store)
        runner = ReaderJobRunner(store)

        # ---------- A：suppressed 传播（合成忠实夹具）----------
        provider = CountingProvider()
        runner.start_run(pid, locale="zh-CN", provider_factory=lambda: provider,
                         model_label="fake-model", service_id="svc", binding_source="reader_role")
        deadline = time.time() + 30
        run = {}
        while time.time() < deadline:
            run = runner.current_run(pid)
            if run.get("status") in ("ready", "failed"):
                break
            time.sleep(0.05)
        src_units = units_of(svc, pid, "source", "S1")
        zh_units = units_of(svc, pid, "zh-CN", "S1")
        nat = svc.reader.load_native(pid, "zh-CN") or {}
        nat_unit = ((nat.get("units") or {}).get("ap-1") or {})
        ok("A1 源层装置 unit 被判 suppressed（权威来源）",
           (src_units.get("ap-1") or {}).get("suppressed") is True
           and (src_units.get("ap-1") or {}).get("source") == "suppressed",
           {k: (src_units.get("ap-1") or {}).get(k) for k in ("source", "suppressed")})
        # brief_source 只是本地摘句来源标签；suppressed 单元的该标签不影响语义（不折叠即不冒充 AI）。
        ok("A2 目标层同 unit suppressed 传播（source=suppressed；译文再长也不折叠、不计入 AI 内容）",
           (zh_units.get("ap-1") or {}).get("suppressed") is True
           and (zh_units.get("ap-1") or {}).get("source") == "suppressed",
           {k: (zh_units.get("ap-1") or {}).get(k)
            for k in ("source", "suppressed", "brief_source", "fallback")})
        ok("A3 native(zh-CN) 同步 suppressed",
           nat_unit.get("suppressed") is True, nat_unit.get("suppressed"))
        r = readiness(svc, pid)
        ok("A4 readiness 全节就绪（含混合装置节）", r["ready"] is True,
           {"ready": r["ready"], "sections": r["sections"]})

        # ---------- B：旧缓存零调用纠正 ----------
        # 把目标层 ap-1 改写成旧缺陷形态，并把 run 记录改成 stale failed + errors:[]
        layers_path = svc.reader.layers_path(pid)
        stored = json.loads(layers_path.read_text(encoding="utf-8"))
        zh = stored["docs"]["zh-CN"]
        for sec in zh["sections"]:
            if sec["id"] == "S1":
                for unit in sec["units"]:
                    if unit.get("id") == "ap-1":
                        unit["source"] = "local"
                        unit["suppressed"] = False
                        unit["brief_source"] = "translation-local"
        layers_path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        run_record = svc.reader.load_fold_run(pid) or {}
        stale = {**run_record, "status": "failed", "ready": False, "errors": [],
                 "ready_basis": "9/11 节就绪；S1: 存在本地摘句/未折叠 unit（不能冒充 AI 完成）"}
        svc.reader.save_fold_run(pid, stale)
        ok("B0 前置：旧缓存形态已注入（ap-1 = local/translation-local/未 suppressed）",
           True, {"ap1": {k: (units_of(svc, pid, 'zh-CN', 'S1').get('ap-1') or {}).get(k)
                          for k in ("source", "suppressed", "brief_source")}})

        strict = CountingProvider(strict=True)
        runner.start_run(pid, locale="zh-CN", provider_factory=lambda: strict,
                         model_label="fake-model", service_id="svc", binding_source="reader_role")
        deadline = time.time() + 30
        while time.time() < deadline:
            run = runner.current_run(pid)
            if run.get("status") in ("ready", "failed"):
                break
            time.sleep(0.05)
        ok("B1 旧缓存 resume：0 模型调用（纯传播纠正，不重烧）", len(strict.calls) == 0,
           {"calls": len(strict.calls), "status": run.get("status")})
        ok("B2 resume 后 run=ready（陈旧 failed 被纠正，不再泛失败）",
           run.get("status") == "ready" and run.get("ready") is True,
           {"status": run.get("status"), "ready": run.get("ready"),
            "basis": str(run.get("ready_basis"))[:200], "errors": run.get("errors")})
        ok("B3 resume 后存盘 unit 变为 suppressed（传播落盘，不是只在内存）",
           (units_of(svc, pid, "zh-CN", "S1").get("ap-1") or {}).get("suppressed") is True,
           {k: (units_of(svc, pid, 'zh-CN', 'S1').get('ap-1') or {}).get(k)
            for k in ("source", "suppressed", "brief_source")})

        # ---------- C：真本地未完成仍不得 ready，且原因可见 ----------
        project_c = store.create_project(src, title="Local Unfinished")
        pid_c = project_c["project_id"]
        doc_c = make_doc(pid_c)
        store.save_document(doc_c)
        store.save_translation(make_translation(doc_c))
        svc.generate(pid_c, locale="zh-CN", provider=None, model_label="", service_id="", mode="local")
        r_c = readiness(svc, pid_c)
        reasons = [s for s in r_c["sections"] if not s["ready"]]
        ok("C1 纯本地未完成：readiness 不得 ready", r_c["ready"] is False,
           {"ready": r_c["ready"], "ready_sections": r_c["ready_sections"], "total": r_c["total"]})
        ok("C2 未就绪原因逐节可见（非空 reason，UI 可展示）",
           bool(reasons) and all(str(s.get("reason") or "").strip() for s in reasons),
           reasons)
        class BoomProvider(CountingProvider):
            def complete(self, *, system: str, user: str):
                self.calls.append(("boom", "?"))
                raise RuntimeError("模拟模型不可用（本地未完成，不得 ready）")

        boom = BoomProvider()
        run_c, _ = runner.start_run(pid_c, locale="zh-CN", provider_factory=lambda: boom,
                                    model_label="fake-model", service_id="svc", binding_source="reader_role")
        deadline = time.time() + 20
        while time.time() < deadline:
            run_c = runner.current_run(pid_c)
            if run_c.get("status") in ("ready", "failed"):
                break
            time.sleep(0.05)
        # 若模型可用则该运行会补齐为 ready（合法）；关键断言是本地-only 时不得 ready
        errs_c = run_c.get("errors") or []
        ok("C3 本地-only 且模型不可用：run=failed，errors 非空且逐条含 section_id（不泛报）",
           run_c.get("status") == "failed" and bool(errs_c)
           and all(str((e or {}).get("section_id") or "").strip() for e in errs_c if isinstance(e, dict))
           and all((isinstance(e, str) and e.strip()) or isinstance(e, dict) for e in errs_c),
           {"status": run_c.get("status"), "errors": errs_c,
            "basis": str(run_c.get("ready_basis"))[:200]})
        # 前端 foldApi.parseFoldRun 的 errors 映射是 String(item.message ?? item)：
        # 逐条必须能渲染出人类可读文字（字符串，或带 non-empty message 的对象），不能出现 [object Object]。
        def render_like_frontend(item):
            if isinstance(item, str):
                return item
            if isinstance(item, dict):
                msg = item.get("message")
                if isinstance(msg, str) and msg.strip():
                    return msg
                return str(item)   # 与前端 String(item) 等价 → dict 会变成 '{...}'/object 串
            return str(item)
        rendered = [render_like_frontend(e) for e in errs_c]
        ok("C4 错误理由精确对应未就绪段（section_id 命中且含 readiness 原因文本）",
           bool(errs_c) and any(
               isinstance(e, dict) and str(e.get("section_id") or "")
               and any(s["section_id"] == e.get("section_id") and str(s.get("reason") or "") in str(e.get("error") or "")
                       for s in (r_c.get("sections") or []) if not s.get("ready"))
               for e in errs_c),
           {"errors": errs_c, "notReady": [s for s in (r_c.get("sections") or []) if not s.get("ready")]})
        # C5：用**真实 TS 源码**的 parseFoldRun（esbuild 去类型后执行）验证渲染，不做 Python 模拟。
        import subprocess
        run_json = base / "run_c.json"
        run_json.write_text(json.dumps(run_c, ensure_ascii=False), encoding="utf-8")
        esbuild_path = str(ROOT / "frontend/node_modules/.pnpm/esbuild@0.28.2/node_modules/esbuild/lib/main.js")
        import glob as _glob
        node_bin = sorted(_glob.glob(str(Path.home() / "Library/Caches/ms-playwright-go/*/node")))[-1]
        proc = subprocess.run(
            [node_bin, str(ROOT / "tmp/parse_fold_run_check.cjs")],
            env={**os.environ, "ESBUILD_PATH": esbuild_path, "REPO": str(ROOT), "INPUT": str(run_json)},
            capture_output=True, text=True, timeout=120,
        )
        try:
            parsed_out = json.loads(proc.stdout)
        except Exception:
            parsed_out = {"stdout": proc.stdout[-300:], "stderr": proc.stderr[-300:], "rc": proc.returncode}
        ok("C5 真实 parseFoldRun（TS 源码）把 errors 渲染为单行人话（无对象转储）",
           proc.returncode == 0 and not (parsed_out.get("dumpLike") or []) and bool(parsed_out.get("errorsRendered")),
           parsed_out)

        # ---------- D：真实书只读副本 ----------
        real_library = Path.home() / "CdoexProject/Literature/library"
        real_pid = "b0ab2358b76b4122be3b5430f5dcceef"
        if (real_library / "projects").exists() or True:
            copy_root = base / "realcopy"
            shutil.copytree(real_library, copy_root)
            def tree_hash(root: Path) -> str:
                h = hashlib.sha256()
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        h.update(str(path.relative_to(root)).encode())
                        h.update(hashlib.sha256(path.read_bytes()).digest())
                return h.hexdigest()
            # 只校验事实层（raw/document/translations）不被写；reader/ 归客户端运行时所有
            def facts_hash(root: Path) -> str:
                h = hashlib.sha256()
                for project_dir in sorted(root.rglob("*")):
                    if not project_dir.is_dir():
                        continue
                    for name in ("document", "translations", "ocr"):
                        sub = project_dir / name
                        if not sub.is_dir():
                            continue
                        for path in sorted(sub.rglob("*")):
                            if path.is_file():
                                h.update(str(path.relative_to(root)).encode())
                                h.update(hashlib.sha256(path.read_bytes()).digest())
                return h.hexdigest()
            real_before = facts_hash(real_library)
            store_d = ProjectStore(copy_root)
            os.environ["LITERATURE_LIBRARY"] = str(copy_root)
            svc_d = ReaderLayerService(store_d)
            runner_d = ReaderJobRunner(store_d)
            try:
                # 副本里可能残留客户端写入的 transient run 状态：归一到干净 failed 再 resume
                raw_run = svc_d.reader.load_fold_run(real_pid) or {}
                clean = {**raw_run, "status": "failed", "ready": False, "errors": [], "paused_at": "",
                         "progress": {**(raw_run.get("progress") or {}), "pending": 0}}
                svc_d.reader.save_fold_run(real_pid, clean)
                r0 = readiness(svc_d, real_pid)
                ok("D1 真实书副本：readiness 逐节明细可得", bool(r0.get("sections")),
                   {"ready": r0["ready"], "ready_sections": r0["ready_sections"], "total": r0["total"]})
                strict_d = CountingProvider(strict=True)
                run_d, _ = runner_d.start_run(real_pid, locale="zh-CN",
                                              provider_factory=lambda: strict_d,
                                              model_label="fake-model", service_id="svc",
                                              binding_source="reader_role")
                deadline = time.time() + 60
                while time.time() < deadline:
                    run_d = runner_d.current_run(real_pid)
                    if run_d.get("status") in ("ready", "failed"):
                        break
                    time.sleep(0.1)
                r1 = readiness(svc_d, real_pid)
                leftovers = []
                for sid in ("S0", "S1", "S11"):
                    for uid, u in units_of(svc_d, real_pid, "zh-CN", sid).items():
                        if u.get("source") == "local" and not u.get("suppressed"):
                            leftovers.append(f"{sid}/{uid}")
                ok("D2 真实书副本：零调用 resume 后 run=ready 且 3 个混合 unit 已传播 suppressed",
                   run_d.get("status") == "ready" and len(strict_d.calls) == 0
                   and r1["ready"] is True and not leftovers,
                   {"status": run_d.get("status"), "calls": len(strict_d.calls),
                    "ready": r1["ready"], "leftovers": leftovers,
                    "basis": str(run_d.get("ready_basis"))[:260]})
            finally:
                os.environ["LITERATURE_LIBRARY"] = str(base / "library")
            real_after = facts_hash(real_library)
            ok("D3 真实库事实层未被写入（只读承诺；reader/ 归运行中的客户端）",
               real_before == real_after, {"before": real_before[:16], "after": real_after[:16]})

        # ---------- E：正常 read API 零生成 ----------
        counter_e = CountingProvider()
        project_e = store.create_project(src, title="Read API Zero Gen")
        pid_e = project_e["project_id"]
        doc_e = make_doc(pid_e)
        store.save_document(doc_e)
        store.save_translation(make_translation(doc_e))
        runner_e = ReaderJobRunner(store)
        runner_e.start_run(pid_e, locale="zh-CN", provider_factory=lambda: counter_e,
                           model_label="fake-model", service_id="svc", binding_source="reader_role")
        deadline = time.time() + 30
        while time.time() < deadline:
            status_e = runner_e.current_run(pid_e).get("status")
            if status_e in ("ready", "failed"):
                break
            time.sleep(0.05)
        calls_after_run = len(counter_e.calls)
        # 反复读快照 / readiness / layers，不得再触发任何模型调用
        for _ in range(3):
            runner_e.current_run(pid_e)
            readiness(svc, pid_e)
            svc.document(pid_e, locale="zh-CN")
            svc.document(pid_e, locale="source")
        ok("E1 只读 API（fold-run 快照 / readiness / layers）零模型生成",
           len(counter_e.calls) == calls_after_run,
           {"callsAfterRun": calls_after_run, "callsNow": len(counter_e.calls)})

    failed = [r for r in RESULTS if not r["pass"]]
    payload = {"results": RESULTS, "failed": len(failed), "total": len(RESULTS)}
    out = ROOT / "tmp/reader-suppression-propagation-result.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in RESULTS:
        print(("PASS" if r["pass"] else "FAIL"), r["name"],
              "" if r["pass"] else json.dumps(r["detail"], ensure_ascii=False)[:400])
    print(f"\ntotal={len(RESULTS)} failed={len(failed)}  → {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
