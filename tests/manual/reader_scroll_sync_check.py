"""普通阅读连续跟随：生产前端、隔离 API/库、真实滚轮，无模型或 Keychain 调用。"""
import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tests.manual.engine_ui_browser_check import free_port, prepare_dist, start_web


def main():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        (base / "runtime").mkdir()
        (base / "runtime/session-token").write_text("")
        os.environ.update(LITERATURE_RUNTIME=str(base / "runtime"),
                          LITERATURE_LIBRARY=str(base / "library"), LITERATURE_SESSION_TOKEN="")
        import literature.model_services as ms
        ms.save_service_key = lambda *a, **k: None
        ms.ModelServiceStore.api_key = lambda *a, **k: None
        from tests.manual.reader_ui_browser_check import seed_project
        store, pid = seed_project(base)
        document = store.load_document(pid)
        translation = store.load_translation(pid)
        for block in document.blocks:
            if block.type == "paragraph":
                block.source_text = (block.source_text + " ") * 65
                if block.block_id in translation.entries:
                    translation.entries[block.block_id].text *= 90
        store.save_document(document)
        store.save_translation(translation)
        from literature.api import create_app
        import uvicorn
        app = create_app(store)
        ap, wp = free_port(), free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=ap, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.1)
        web = start_web(prepare_dist(base, ap), wp)
        try:
            cache = str(Path.home() / "Library/Caches/ms-playwright-go")
            result = subprocess.run([
                sorted(glob.glob(cache + "/*/node"))[-1],
                str(ROOT / "tests/manual/reader_scroll_sync_check.cjs"), str(wp), pid,
            ], env={**os.environ, "PW_PACKAGE_PATH": sorted(glob.glob(cache + "/*/package"))[-1]},
                capture_output=True, text=True, timeout=100)
            print(result.stdout)
            print(result.stderr)
            (ROOT / "tmp/reader-scroll-sync-result.json").write_text(result.stdout)
            return result.returncode
        finally:
            server.should_exit = True
            web.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
