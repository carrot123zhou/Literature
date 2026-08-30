from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "literature-sidecar", "--distpath", str(DIST), "--workpath", str(WORK),
        "--specpath", str(WORK), "--collect-all", "fitz", "--collect-all", "docx",
        "--hidden-import", "uvicorn.logging", "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto", "--hidden-import", "uvicorn.protocols.websockets.auto",
        str(ROOT / "literature" / "sidecar.py"),
    ], cwd=ROOT, check=True)
    source = DIST / "literature-sidecar"
    BINARIES.mkdir(parents=True, exist_ok=True)
    destination = BINARIES / f"literature-sidecar-{target_triple()}"
    shutil.copy2(source, destination)
    destination.chmod(0o755)
    print(destination)


if __name__ == "__main__":
    main()
