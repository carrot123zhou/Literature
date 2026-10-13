"""Remove only reproducible development artifacts; never traverse the library."""
from __future__ import annotations
import argparse
import shutil
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ["build", "dist", "frontend/dist", "src-tauri/target/debug", "src-tauri/target/release/build", "src-tauri/target/release/deps", "src-tauri/target/release/.fingerprint", "src-tauri/target/release/incremental", "src-tauri/gen", ".pytest_cache"]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Delete listed artifacts (default: preview)")
    args = parser.parse_args()
    candidates = [ROOT / path for path in CANDIDATES]
    for directory in [ROOT / "literature", ROOT / "tests", ROOT / "scripts"]:
        candidates.extend(directory.rglob("__pycache__"))
    for directory, children, files in os.walk(ROOT, followlinks=False):
        children[:] = [name for name in children if name not in {"library", ".venv", "node_modules", ".git", "launcher", ".pnpm-store"}]
        if ".DS_Store" in files: candidates.append(Path(directory) / ".DS_Store")
    total = 0
    for path in candidates:
        # Do not follow symlinks or inspect user evidence/dependencies.
        if not path.exists() or path.is_symlink() or any(part in {"library", ".venv", "node_modules", ".git", "launcher"} for part in path.relative_to(ROOT).parts):
            continue
        size = sum(file.stat().st_size for file in path.rglob("*") if file.is_file() and not file.is_symlink()) if path.is_dir() else path.stat().st_size
        total += size
        print(f"{'remove' if args.apply else 'preview'} {size / 1024 / 1024:.1f} MiB {path.relative_to(ROOT)}")
        if args.apply:
            if path.is_dir(): shutil.rmtree(path)
            else: path.unlink()
    print(f"{'Removed' if args.apply else 'Reclaimable'}: {total / 1024 / 1024:.1f} MiB")

if __name__ == "__main__": main()
