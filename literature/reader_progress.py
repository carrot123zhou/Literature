"""Fold progress is a set of section states, never accumulated callback counts."""
from __future__ import annotations
from typing import Any


def section_progress(states: dict[str, str]) -> dict[str, Any]:
    total = len(states)
    completed = sum(value == "folded" for value in states.values())
    reused = sum(value == "reused" for value in states.values())
    failed = sum(value == "failed" for value in states.values())
    return {
        "total": total, "completed": completed, "reused": reused, "failed": failed,
        "pending": total - completed - reused,
        "ratio": round((completed + reused) / total, 3) if total else 0.0,
    }
