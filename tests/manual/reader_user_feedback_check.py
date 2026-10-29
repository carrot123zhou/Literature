"""用户反馈定向验收 runner（4 项交互 + 真实 run 错误 provenance，只读真实库副本）。"""
from __future__ import annotations
import glob, json, os, subprocess, sys, tempfile, threading, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web
from tests.manual.reader_ui_browser_check import make_real_pdf
from tests.test_reader_fold_run import make_document, make_translation, ScriptedProvider
from literature.models import Block, Document, Page
from literature.storage import ProjectStore


def make_long_document(project_id: str) -> Document:
    """U3 用长文夹具：5 章 × 6 段 × 约 500 字符，跨 page_index 0..4，保证折叠全文层可跨页滚动。"""
    base = make_document(project_id)
    blocks = list(base.blocks)
    order = max(b.order for b in blocks) + 1
    para = base.blocks[1]  # 参照段落样式
    filler = ("Silver vessels travelled along the Silk Road and the workshops copied "
              "Sogdian shapes for decades until demand fell after the ninth century "
              "and the assay records show a consistent alloy of ninety-two percent "
              "silver mixed with trace copper from the eastern mines. ") * 3
    for sec in range(3, 8):
        blocks.append(Block(block_id=f"b-title-{sec}", type="title", order=order,
                            page_index=sec % 5, source_text=f"Chapter {sec}", level=1,
                            bbox=[10, 10, 300, 40]))
        order += 1
        for k in range(6):
            blocks.append(Block(block_id=f"b-p-{sec}-{k}", type="paragraph", order=order,
                                page_index=sec % 5, bbox=[10, 50, 300, 900],
                                source_text=f"Section {sec} passage {k}. {filler}"))
            order += 1
    return Document(project_id=project_id, title=base.title, source_language="en",
                    page_count=5,
                    pages=[Page(page_index=i, width=600, height=800) for i in range(5)],
                    blocks=blocks)

def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        os.environ["LITERATURE_RUNTIME"] = str(base / "runtime")
        os.environ["LITERATURE_LIBRARY"] = str(base / "library")
        os.environ["LITERATURE_SESSION_TOKEN"] = ""
        (base / "runtime").mkdir(parents=True, exist_ok=True)
        (base / "runtime/session-token").write_text("")
        import literature.model_services as ms
        ms.ModelServiceStore.api_key = lambda self, sid: "ufeedback-not-a-real-key"
        store = ProjectStore(base / "library")
        src = base / "book.pdf"; make_real_pdf(src, pages=6)
        project = store.create_project(src, title="Silk Road Silver")
        pid = project["project_id"]
        document = make_long_document(pid)
        store.save_document(document)
        store.save_translation(make_translation(document))
        from literature.api import create_app
        import uvicorn
        app = create_app(store)
        from tests.test_reader_fold_run import ScriptedProvider
        provider = ScriptedProvider()
        app.state.job_manager.provider_from = lambda *a, **k: provider
        from fastapi.testclient import TestClient
        boot = TestClient(app)
        headers = {"x-literature-token": app.state.session_token}
        svc = boot.post("/model-services", json={
            "name": "ufeedback-fake", "preset": "custom",
            "base_url": "http://127.0.0.1:1/v1", "models": [{"model": "fake-model"}],
        }, headers=headers)
        assert svc.status_code == 200, svc.text
        roles = boot.put("/model-services/roles", json={
            "reader_fold": {"service_id": svc.json()["service_id"], "model": "fake-model"},
        }, headers=headers)
        assert roles.status_code == 200, roles.text
        run = boot.post(f"/projects/{pid}/reader/fold-runs", json={"locale": "zh-CN"}, headers=headers)
        assert run.status_code == 200, run.text
        snap = {}
        for _ in range(300):
            snap = boot.get(f"/projects/{pid}/reader/fold-run", headers=headers).json()
            if snap.get("status") in ("ready", "failed"): break
            time.sleep(0.05)
        assert snap.get("status") == "ready", snap
        ap, wp = free_port(), free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ap, log_level="error"))
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(100):
            if server.started: break
            time.sleep(0.1)
        web = start_web(prepare_dist(base, ap), wp)
        result_path = ROOT / "tmp/reader-user-feedback-result.json"
        try:
            cache = str(Path.home() / "Library/Caches/ms-playwright-go")
            node = sorted(glob.glob(cache + "/*/node"))[-1]
            pkg = sorted(glob.glob(cache + "/*/package"))[-1]
            args = [node, str(ROOT / "tests/manual/reader_user_feedback_check.cjs"),
                    str(wp), str(ap), json.dumps([pid]), str(result_path),
                    app.state.session_token]
            proc = subprocess.run(args, env={**os.environ, "PW_PACKAGE_PATH": pkg},
                                  capture_output=True, text=True, timeout=420)
            (ROOT / "tmp/reader-user-feedback-stdout.txt").write_text(
                proc.stdout + "\n--stderr--\n" + proc.stderr, encoding="utf-8")
            print(proc.stdout[-2500:])
            if proc.stderr.strip(): print("STDERR:", proc.stderr[-600:])
            try:
                parsed = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:
                parsed = {"parse_error": True}
            import hashlib
            def sha(path):
                try: return hashlib.sha256(Path(path).read_bytes()).hexdigest()
                except Exception: return None
            evidence = {
                "fingerprints": {
                    "served_assets_at_run": {
                        "dist_js": sorted((ROOT / "frontend/dist/assets").glob("index-*.js"))[0].name,
                        "dist_js_sha256": sha(sorted((ROOT / "frontend/dist/assets").glob("index-*.js"))[0]),
                        "note": "运行时实际 served 的资产哈希（版本归属以此为准，非当前源码）",
                    },
                    "current_source_at_run": {
                        "workspace_tsx": sha(ROOT / "frontend/src/reader/ReaderWorkspace.tsx"),
                        "textfold_pane": sha(ROOT / "frontend/src/reader/TextFoldPane.tsx"),
                        "reader_py": sha(ROOT / "literature/reader.py"), "api_py": sha(ROOT / "literature/api.py"),
                        "note": "运行时刻的源码快照；源码在途变更后此哈希即过期",
                    },
                },
                "pid": pid,
                "results": parsed.get("results", parsed.get("assertions", [])),
                "failed": parsed.get("failed"),
                "network_timeline": parsed.get("timeline", []),
            }
            out = ROOT / "tmp/reader-user-feedback-result.json"
            out.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
            print("EVIDENCE:", out)
            return proc.returncode
        finally:
            server.should_exit = True
            web.shutdown()

if __name__ == "__main__":
    sys.exit(main())
