"""解析任务历史、展示分区与归档保留。

本模块把「任务生命周期」与「展示/保留期」分开：

- ``JobArchiveStore``：独立持久化归档元数据（``{runtime}/ocr-jobs/archive.json``），
  绝不去写 ``job.json``，因此不会污染 ``updated_at`` / ``finished_at``。
- 纯函数：分区过滤、排序、分页、自动归档候选选择。它们只依赖 job 字典，
  便于在没有真实 MinerU 的隔离环境里直接回归。

归档只是展示/保留期元数据，不改变 ``job.state``，也不删除 job / bridge / raw /
canonical / translation。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_RETENTION_DAYS = 90
DEFAULT_MAX_RECORDS = 1000
RETENTION_CHOICES: tuple[int | None, ...] = (30, 90, 180, None)

RUNNING_STATES = {"preparing", "uploading", "processing", "downloading", "merging"}
QUEUED_STATES = {"queued"}
RESUMABLE_STATES = {"waiting", "interrupted"}
FAILED_STATES = {"failed"}
COMPLETED_STATES = {"completed"}
FINAL_STATES = COMPLETED_STATES | FAILED_STATES
ACTIVE_STATES = RUNNING_STATES | QUEUED_STATES | RESUMABLE_STATES

RECENT_COMPLETED_HOURS = 24
RECENT_COMPLETED_LIMIT = 3
HISTORY_PAGE_SIZE = 20
ACTIVE_PAGE_SIZE = 10
AUTO_ARCHIVE_INTERVAL_HOURS = 24

_UNSET = object()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return utcnow().isoformat()


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class JobArchiveStore:
    """持久化归档记录与保留设置；所有读写串行化并原子落盘。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    # -- persistence ----------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        records = value.get("records")
        if not isinstance(records, dict):
            records = {}
        settings = value.get("settings")
        if not isinstance(settings, dict):
            settings = {}
        retention = settings.get("retention_days", DEFAULT_RETENTION_DAYS)
        if retention is not None and retention not in RETENTION_CHOICES:
            retention = DEFAULT_RETENTION_DAYS
        max_records = settings.get("max_records", DEFAULT_MAX_RECORDS)
        try:
            max_records = max(1, int(max_records))
        except (TypeError, ValueError):
            max_records = DEFAULT_MAX_RECORDS
        return {
            "settings": {"retention_days": retention, "max_records": max_records},
            "last_auto_run_at": str(value.get("last_auto_run_at") or ""),
            "records": records,
        }

    def _flush(self) -> None:  # retained for compatibility; prefer _publish
        _atomic_json(self.path, self._data)

    def _publish(self, data: dict[str, Any]) -> None:
        """先落盘、成功后再发布内存状态，避免写失败却让当前会话看到已生效的元数据。"""
        _atomic_json(self.path, data)
        self._data = data

    # -- reads ----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data["settings"])

    def record(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data["records"].get(str(job_id))
            return dict(value) if isinstance(value, dict) else None

    def is_archived(self, job_id: str) -> bool:
        return self.record(job_id) is not None

    def last_auto_run_at(self) -> str:
        with self._lock:
            return str(self._data.get("last_auto_run_at") or "")

    def due(self, *, now: datetime | None = None, interval_hours: int = AUTO_ARCHIVE_INTERVAL_HOURS) -> bool:
        last = parse_timestamp(self.last_auto_run_at())
        if last is None:
            return True
        moment = now or utcnow()
        return (moment - last) >= timedelta(hours=interval_hours)

    # -- writes ---------------------------------------------------------
    def update_settings(self, *, retention_days: Any = _UNSET, max_records: Any = _UNSET) -> dict[str, Any]:
        with self._lock:
            settings = dict(self._data["settings"])
            if retention_days is not _UNSET:
                if retention_days not in RETENTION_CHOICES:
                    raise ValueError("保留期只能是 30、90、180 天或永久。")
                settings["retention_days"] = retention_days
            if max_records is not _UNSET:
                try:
                    settings["max_records"] = max(1, int(max_records))
                except (TypeError, ValueError) as error:
                    raise ValueError("保留条数上限必须是正整数。") from error
            self._publish({**self._data, "settings": settings})
            return dict(settings)

    def archive_many(self, entries: Iterable[tuple[str, str, bool]], *, now: datetime | None = None) -> list[str]:
        """批量归档；已归档的条目保留首次 ``archived_at``/``reason``，返回本次新增的 job_id。

        写盘失败时抛错且内存状态保持旧值（copy-on-write），调用方可安全重试。
        """
        moment = (now or utcnow()).isoformat()
        added: list[str] = []
        with self._lock:
            records = dict(self._data["records"])
            for job_id, reason, auto in entries:
                key = str(job_id)
                if key in records:
                    continue
                records[key] = {"archived_at": moment, "reason": str(reason), "auto": bool(auto)}
                added.append(key)
            if added:
                self._publish({**self._data, "records": records})
        return added

    def archive_one(self, job_id: str, *, reason: str, auto: bool, now: datetime | None = None) -> dict[str, Any]:
        self.archive_many([(job_id, reason, auto)], now=now)
        return self.record(job_id) or {}

    def unarchive(self, job_id: str) -> bool:
        with self._lock:
            records = dict(self._data["records"])
            removed = records.pop(str(job_id), None)
            if removed is not None:
                self._publish({**self._data, "records": records})
            return removed is not None

    def mark_auto_run(self, *, now: datetime | None = None) -> str:
        with self._lock:
            value = (now or utcnow()).isoformat()
            self._publish({**self._data, "last_auto_run_at": value})
            return value


def job_is_active(job: dict[str, Any]) -> bool:
    return str(job.get("state") or "") in ACTIVE_STATES


def job_is_archivable(job: dict[str, Any]) -> bool:
    """手动归档只允许 completed / failed；活动与可恢复任务一律拒绝。"""
    return str(job.get("state") or "") in FINAL_STATES


def _finished_value(job: dict[str, Any]) -> datetime:
    parsed = parse_timestamp(job.get("finished_at")) or parse_timestamp(job.get("updated_at"))
    if parsed is None:
        parsed = datetime.min.replace(tzinfo=timezone.utc)
    return parsed


def _created_value(job: dict[str, Any]) -> datetime:
    return parse_timestamp(job.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc)


def active_count(job: dict[str, Any]) -> bool:
    state = str(job.get("state") or "")
    return state in ACTIVE_STATES or (state in FAILED_STATES and not job.get("archived"))


def select_auto_archive(
    jobs: Iterable[dict[str, Any]], settings: dict[str, Any], *,
    now: datetime | None = None, capacity_only: bool = False,
) -> list[tuple[str, str]]:
    """按保留期与条数上限选择要归档的 completed 任务；最老优先。"""
    moment = now or utcnow()
    completed = [
        job for job in jobs
        if str(job.get("state") or "") in COMPLETED_STATES and not job.get("archived")
    ]
    completed.sort(key=lambda job: (_finished_value(job), job.get("job_id") or ""))
    selected: dict[str, str] = {}
    if not capacity_only:
        retention = settings.get("retention_days", DEFAULT_RETENTION_DAYS)
        if retention is not None:
            cutoff = moment - timedelta(days=int(retention))
            for job in completed:
                if _finished_value(job) < cutoff:
                    selected[str(job["job_id"])] = "retention"
    remaining = [job for job in completed if str(job["job_id"]) not in selected]
    cap = int(settings.get("max_records", DEFAULT_MAX_RECORDS) or DEFAULT_MAX_RECORDS)
    if len(remaining) > cap:
        overflow = remaining[: len(remaining) - cap]
        for job in overflow:
            selected[str(job["job_id"])] = "capacity"
    return [(job_id, reason) for job_id, reason in selected.items()]


def filter_active(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [job for job in jobs if active_count(job)]


def sort_active(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def priority(job: dict[str, Any]) -> int:
        state = str(job.get("state") or "")
        if state in RUNNING_STATES:
            return 0
        if state in QUEUED_STATES:
            return 1
        if state in RESUMABLE_STATES:
            return 2
        return 3

    def key(job: dict[str, Any]) -> tuple:
        state = str(job.get("state") or "")
        priority_value = priority(job)
        if priority_value == 1:
            position = job.get("queue_position")
            return (priority_value, int(position) if isinstance(position, int) else 10_000, -_created_value(job).timestamp())
        return (priority_value, -_finished_value(job).timestamp(), job.get("job_id") or "")

    return sorted(jobs, key=key)


def filter_recent_completed(
    jobs: Iterable[dict[str, Any]], *, now: datetime | None = None,
    hours: int = RECENT_COMPLETED_HOURS, limit: int = RECENT_COMPLETED_LIMIT,
) -> list[dict[str, Any]]:
    moment = now or utcnow()
    cutoff = moment - timedelta(hours=hours)
    recent = [
        job for job in jobs
        if str(job.get("state") or "") in COMPLETED_STATES
        and not job.get("archived")
        and _finished_value(job) >= cutoff
    ]
    # finished_at 倒序；同时间用 created_at 倒序、再 job_id 倒序保证稳定。
    recent.sort(key=lambda job: (_finished_value(job), _created_value(job), job.get("job_id") or ""), reverse=True)
    return recent[:limit]


def filter_history(
    jobs: Iterable[dict[str, Any]], *, query: str = "", status: str = "",
) -> list[dict[str, Any]]:
    needle = str(query or "").strip().casefold()
    wanted = str(status or "").strip()
    output = []
    for job in jobs:
        state = str(job.get("state") or "")
        if wanted in {"completed", "failed"} and state != wanted:
            continue
        if wanted == "active" and not active_count(job):
            continue
        if wanted == "archived" and not job.get("archived"):
            continue
        if wanted in {"queued", "waiting", "interrupted"} and state != wanted:
            continue
        if wanted == "running" and state not in RUNNING_STATES:
            continue
        if needle:
            haystack = " ".join(str(job.get(field) or "") for field in ("title", "source_name", "job_id")).casefold()
            if needle not in haystack:
                continue
        output.append(job)
    return output


def sort_history(jobs: list[dict[str, Any]], sort: str = "recent") -> list[dict[str, Any]]:
    def key(job: dict[str, Any]) -> tuple:
        return (_finished_value(job), _created_value(job), job.get("job_id") or "")

    reverse = str(sort or "recent") != "oldest"
    return sorted(jobs, key=key, reverse=reverse)


def paginate(items: list[dict[str, Any]], page: int, page_size: int) -> tuple[list[dict[str, Any]], int, int, int]:
    size = max(1, int(page_size))
    total = len(items)
    pages = max(1, (total + size - 1) // size)
    current = min(max(1, int(page)), pages)
    start = (current - 1) * size
    return items[start:start + size], total, current, pages


def count_active(jobs: Iterable[dict[str, Any]]) -> dict[str, int]:
    running = queued = resumable = failed = 0
    for job in jobs:
        state = str(job.get("state") or "")
        if state in RUNNING_STATES:
            running += 1
        elif state in QUEUED_STATES:
            queued += 1
        elif state in RESUMABLE_STATES:
            resumable += 1
        elif state in FAILED_STATES and not job.get("archived"):
            failed += 1
    return {"running": running, "queued": queued, "resumable": resumable, "failed": failed, "total": running + queued + resumable + failed}
