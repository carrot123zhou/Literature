"""Identify the exact Literature backend source loaded by an API process."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def source_revision(package_dir: Path | None = None) -> str:
    """Return a stable fingerprint for all Python sources in the backend package."""

    if package_dir is None and getattr(sys, "frozen", False):
        return (Path(__file__).parent / "runtime-revision.txt").read_text(encoding="utf-8").strip()
    root = (package_dir or Path(__file__).parent).resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:20]


# This is intentionally evaluated once at import time. A process keeps reporting
# the source it actually loaded even if files on disk change afterwards.
RUNTIME_REVISION = source_revision()


def main() -> None:
    print(source_revision())


if __name__ == "__main__":
    main()
