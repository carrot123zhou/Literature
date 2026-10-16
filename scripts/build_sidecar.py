from __future__ import annotations

import shutil
import json
import hashlib
import subprocess
import sys
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORK = ROOT / "build" / "sidecar"
DIST = ROOT / "dist" / "sidecar"
BINARIES = ROOT / "src-tauri" / "binaries"


def target_triple() -> str:
    result = subprocess.run(
        ["rustc", "--print", "host-tuple"], capture_output=True, text=True, check=True,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("rustc 没有返回 target triple。")
    return value


def main() -> None:
    from literature.runtime_revision import source_revision
    WORK.mkdir(parents=True, exist_ok=True)
    revision = WORK / "runtime-revision.txt"
    revision.write_text(source_revision(), encoding="utf-8")
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(WORK / "cache")
    # PyInstaller 的 --add-data 分隔符在 POSIX 上是 ':'、Windows 上是 ';'。
    separator = ";" if os.name == "nt" else ":"
    suffix = ".exe" if os.name == "nt" else ""
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "literature-sidecar", "--distpath", str(DIST), "--workpath", str(WORK),
        "--specpath", str(WORK), "--collect-all", "pymupdf", "--collect-all", "docx",
        "--add-data", f"{revision}{separator}literature",
        "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto", "--hidden-import", "uvicorn.protocols.websockets.auto",
        str(ROOT / "literature" / "sidecar.py"),
    ], cwd=ROOT, check=True)
    source = DIST / f"literature-sidecar{suffix}"
    BINARIES.mkdir(parents=True, exist_ok=True)
    destination = BINARIES / f"literature-sidecar-{target_triple()}{suffix}"
    shutil.copy2(source, destination)
    if os.name != "nt":
        destination.chmod(0o755)
    manifest = {"target": target_triple(), "runtime_revision": revision.read_text(), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "size": destination.stat().st_size}
    (BINARIES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(destination)


if __name__ == "__main__":
    main()
