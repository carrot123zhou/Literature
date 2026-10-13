"""持久化解析调度队列。

设计对齐微软《Background jobs》指南：把“排队的作业”和“正在执行的作业”分开持久化，
队列顺序即真实执行顺序（默认单文档串行），进程重启后按同一顺序恢复；运行中的作业
不会被后加入的作业抢占。MinerU 远端不提供 cancel/pause，本模块只负责“先执行哪个”。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable, Iterable

POSITIONS = {"top", "up", "down", "end"}


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class ParseQueue:
    """线程安全、可持久化的作业顺序表。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._order = self._read()

    # -- persistence -----------------------------------------------------
    def _read(self) -> list[str]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        order = value.get("order") if isinstance(value, dict) else None
        if not isinstance(order, list):
            return []
        seen: set[str] = set()
        result: list[str] = []
        for item in order:
            job_id = str(item)
            if job_id and job_id not in seen:
                seen.add(job_id)
                result.append(job_id)
        return result

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"order": self._order, "updated_at": _utcnow()}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    # -- queries ---------------------------------------------------------
    def order(self) -> list[str]:
        with self._lock:
            return list(self._order)

    def position(self, job_id: str) -> int | None:
        with self._lock:
            try:
                return self._order.index(job_id)
            except ValueError:
                return None

    # -- mutations -------------------------------------------------------
    def enqueue(self, job_id: str, position: str = "end") -> int:
        job_id = str(job_id)
        if not job_id:
            raise ValueError("job_id 不能为空")
        if position not in POSITIONS:
            position = "end"
        with self._lock:
            if job_id in self._order:
                self._order.remove(job_id)
            if position == "top":
                self._order.insert(0, job_id)
            else:
                self._order.append(job_id)
            self._write_locked()
            return self._order.index(job_id)

    def move(self, job_id: str, position: str) -> int:
        job_id = str(job_id)
        with self._lock:
            if job_id not in self._order:
                self._order.append(job_id)
            index = self._order.index(job_id)
            if position == "top":
                index = 0
            elif position == "up":
                index = max(0, index - 1)
            elif position == "down":
                index = min(len(self._order) - 1, index + 1)
            else:
                index = len(self._order) - 1
            self._order.remove(job_id)
            self._order.insert(index, job_id)
            self._write_locked()
            return index

    def remove(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._order:
                return False
            self._order.remove(job_id)
            self._write_locked()
            return True

    def prune(self, keep: Callable[[str], bool]) -> list[str]:
        """移除不再需要排队/恢复的 job_id（例如已完成或 job.json 丢失）。"""
        with self._lock:
            removed = [job_id for job_id in self._order if not keep(job_id)]
            if removed:
                self._order = [job_id for job_id in self._order if keep(job_id)]
                self._write_locked()
            return removed


def select_next_runnable(
    order: Iterable[str], state_of: Callable[[str], str | None], runnable_states: Iterable[str],
) -> str | None:
    """按队列顺序返回第一个可运行作业；`state_of` 返回 None 表示作业不存在。"""
    allowed = set(runnable_states)
    for job_id in order:
        state = state_of(job_id)
        if state is not None and state in allowed:
            return job_id
    return None


__all__ = ["ParseQueue", "select_next_runnable", "POSITIONS"]
