from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

from .mineru import coverage_pages, coverage_payload, merge_imported_project
from .mineru_workflow import (
    ACTIVE_JOB_STATES, RESUMABLE_JOB_STATES, JobState, MinerUWorkflow, SliceState,
    WorkflowJob, delete_mineru_token, format_page_ranges, get_mineru_token,
    parse_page_ranges, save_mineru_token,
)
from .parse_backends import REMOTE_BACKEND_ID, ParseResult
from .parse_queue import ParseQueue, select_next_runnable
from .storage import ProjectStore, StorageError, atomic_json
from .task_history import (
    ACTIVE_PAGE_SIZE, HISTORY_PAGE_SIZE, RECENT_COMPLETED_LIMIT,
    JobArchiveStore, count_active, filter_active, filter_history, filter_recent_completed,
    job_is_archivable, paginate, select_auto_archive, sort_active, sort_history, utcnow,
)


LANGUAGE_MAP = {"auto": "ch", "en": "en", "ja": "japan", "fr": "latin", "de": "latin"}
logger = logging.getLogger(__name__)

# 参与“单项目同时至多一个活动作业”约束的状态。
_ACTIVE_PARSE_STATES = set(RESUMABLE_JOB_STATES) | set(ACTIVE_JOB_STATES)


def default_runtime_root() -> Path:
    configured = os.environ.get("LITERATURE_RUNTIME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / "Library" / "Application Support" / "Literature"


class MinerUService:
    def __init__(self, store: ProjectStore, runtime_root: str | Path | None = None) -> None:
        self.store = store
        self.runtime_root = Path(runtime_root).expanduser().resolve() if runtime_root else default_runtime_root().resolve()
        self.jobs_root = self.runtime_root / "ocr-jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self.workflow = MinerUWorkflow(self.jobs_root, merge_callback=self._finish_import)
        self.queue = ParseQueue(self.jobs_root / "queue.json")
        self.archive = JobArchiveStore(self.jobs_root / "archive.json")
        self._scheduler_thread: threading.Thread | None = None
        self._scheduler_lock = threading.Lock()
        self._scheduler_event = threading.Event()
        self._scheduler_stop = threading.Event()
        self._running_lock = threading.Lock()
        self._running_job_id: str | None = None
        # 统一调度锁：保护“选择候选 → 领取 → 从队列移除”与入队/调序/重试状态修改，
        # 保证调序与领取不会交错。不持锁跑网络。
        self._schedule_lock = threading.RLock()
        # 同一项目的“检查活动作业 + 创建/启动/重试”必须是一个临界区；
        # 同一 job 的启动/重试/调序也必须串行。
        self._project_locks: dict[str, threading.RLock] = {}
        self._project_locks_guard = threading.Lock()
        self._job_locks: dict[str, threading.RLock] = {}
        self._job_locks_guard = threading.Lock()
        # 合并/追加解析改变了 block 总数后，外部可注册回调刷新翻译摘要。
        self.on_document_changed: Any = None

    def project_lock(self, project_id: str) -> threading.RLock:
        with self._project_locks_guard:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.RLock()
                self._project_locks[project_id] = lock
            return lock

    def job_lock(self, job_id: str) -> threading.RLock:
        with self._job_locks_guard:
            lock = self._job_locks.get(job_id)
            if lock is None:
                lock = threading.RLock()
                self._job_locks[job_id] = lock
            return lock

    # -- project / job mapping ------------------------------------------
    def _bridge_path(self, job_id: str) -> Path:
        return self.jobs_root / job_id / "literature-bridge.json"

    def _project_for_job(self, job_id: str) -> str:
        path = self._bridge_path(job_id)
        if not path.is_file():
            raise StorageError("MinerU 任务缺少 Literature 项目映射。")
        return str(json.loads(path.read_text(encoding="utf-8"))["project_id"])

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_root / job_id / "job.json"

    def job_exists(self, job_id: str) -> bool:
        return bool(job_id) and self._job_path(str(job_id)).is_file()

    def _finish_import(self, job: WorkflowJob, parse_result: ParseResult) -> None:
        """合并回调：消费统一 ParseResult（raw 目录 + source_pages），只规范化本次切片并追加。"""
        project_id = self._project_for_job(job.job_id)
        project = self.store.project_path(project_id)
        # 兼容旧任务：ParseResult 未带页映射时回退到切片推导。
        by_directory = {
            str(Path(item.result_directory).resolve()): item
            for item in job.slices if item.result_directory
        }
        entries: list[dict[str, Any]] = []
        for directory in parse_result.raw_directories:
            if not directory:
                continue
            resolved = str(Path(directory).resolve())
            pages = list(parse_result.source_pages.get(directory) or parse_result.source_pages.get(resolved) or [])
            if not pages:
                pages = self._derive_source_pages(job, by_directory.get(resolved))
            entries.append({"directory": Path(directory), "source_pages": pages})
        if not entries:
            raise StorageError("本次解析没有可合并的结果。")
        append = bool(job.append) or (project / "document" / "document.json").is_file()
        sealed = self.store.seal_raw_mineru(project_id, [entry["directory"] for entry in entries])
        for entry, record in zip(entries, sealed):
            entry["directory"] = Path(record["destination"])
        merge_imported_project(
            self.store, project_id, entries,
            source_page_count=self._source_page_count(job), append=append,
        )
        if self.on_document_changed is not None:
            try:
                self.on_document_changed(project_id)
            except Exception:
                logger.exception("Refresh translation summary failed for %s", project_id)

    @staticmethod
    def _derive_source_pages(job: WorkflowJob, item: Any) -> list[int]:
        """旧任务没有 source_pages 时，仅在证据可信时按连续 start/end 恢复。

        历史 ``page_ranges`` 作业把整源 PDF 上传、本地 start/end 不映射真实源页，
        因此不得猜；只有“无 page_ranges 且连续 start/end”才可恢复。
        """
        if item is None:
            return []
        if item.source_pages:
            return [int(page) for page in item.source_pages]
        if str(getattr(job, "page_ranges", "")).strip():
            return []
        if item.start_page >= 1 and item.end_page >= item.start_page:
            return list(range(item.start_page, item.end_page + 1))
        return []

    @staticmethod
    def _source_page_count(job: WorkflowJob) -> int:
        if int(job.source_page_count or 0) > 0:
            return int(job.source_page_count)
        if Path(job.source_pdf).suffix.casefold() == ".pdf":
            try:
                from .mineru_workflow import _open_pdf_page_count
                return _open_pdf_page_count(Path(job.source_pdf))
            except Exception:
                return 0
        return 0

    # -- create ---------------------------------------------------------
    def create(
        self, project_id: str, *, language: str = "auto", model_version: str = "vlm",
        is_ocr: bool = True, enable_table: bool = True, enable_formula: bool = True,
        page_ranges: str = "", extra_formats: list[str] | None = None,
        page_limit: int = 200, requested_pages: list[int] | None = None,
        append: bool = False, enforce_single: bool = False,
    ) -> dict[str, Any]:
        with self.project_lock(project_id):
            if enforce_single:
                active = self.active_job(project_id)
                if active is not None:
                    raise StorageError("该资源已有正在进行的解析任务；请等待完成或先停止恢复后再继续。")
            return self._create_locked(
                project_id, language=language, model_version=model_version, is_ocr=is_ocr,
                enable_table=enable_table, enable_formula=enable_formula, page_ranges=page_ranges,
                extra_formats=extra_formats, page_limit=page_limit, requested_pages=requested_pages,
                append=append,
            )

    def _create_locked(
        self, project_id: str, *, language: str = "auto", model_version: str = "vlm",
        is_ocr: bool = True, enable_table: bool = True, enable_formula: bool = True,
        page_ranges: str = "", extra_formats: list[str] | None = None,
        page_limit: int = 200, requested_pages: list[int] | None = None,
        append: bool = False,
    ) -> dict[str, Any]:
        project = self.store.project_path(project_id)
        source = self.store.source_path(project_id)
        is_pdf = source.suffix.casefold() == ".pdf"
        is_html = source.suffix.casefold() == ".html"
        job: WorkflowJob | None = None
        try:
            job = self.workflow.create_job(
                source, project / "ocr" / ".mineru-workflow.epub",
                model_version=model_version, language=LANGUAGE_MAP.get(language, language), is_ocr=is_ocr,
                enable_table=enable_table, enable_formula=enable_formula,
                page_ranges=page_ranges if is_pdf else "",
                extra_formats=[] if is_html else extra_formats, page_limit=page_limit,
                keep_original_pdf=False,
                requested_pages=requested_pages, requested_ranges=page_ranges,
                append=append,
                backend_id=REMOTE_BACKEND_ID,
                backend_config={
                    "model_version": model_version,
                    "language_pack": LANGUAGE_MAP.get(language, language),
                    "is_ocr": is_ocr, "enable_table": enable_table, "enable_formula": enable_formula,
                    "page_limit": page_limit,
                    "page_ranges": page_ranges if is_pdf else "",
                    "extra_formats": [] if is_html else list(extra_formats or []),
                },
            )
            atomic_json(self._bridge_path(job.job_id), {"project_id": project_id})
            self.store.update_manifest(project_id, state="ocr_queued", mineru_job_id=job.job_id)
            return self.describe(job)
        except Exception:
            # 未返回给调用方的任务不得留在活动 runtime 索引里。
            if job is not None:
                shutil.rmtree(self.jobs_root / job.job_id, ignore_errors=True)
            raise

    def run(self, job_id: str) -> dict[str, Any]:
        project_id = self._project_for_job(job_id)
        self.store.update_manifest(project_id, state="ocr_processing")
        try:
            job = self.workflow.run_or_resume(self._job_path(job_id))
        except Exception:
            self.store.update_manifest(project_id, state="ocr_failed")
            raise
        if job.state == JobState.COMPLETED:
            self.store.update_manifest(project_id, state="ready")
            self.queue.remove(job_id)
            # 新增完成后处理超量（仅条数上限，不计入每日一次的时间保留）。
            try:
                self.auto_archive(capacity_only=True)
            except Exception:
                logger.exception("Auto archive capacity check failed after %s", job_id)
        elif job.state == JobState.WAITING:
            self.store.update_manifest(project_id, state="ocr_waiting")
        elif job.state == JobState.INTERRUPTED:
            self.store.update_manifest(project_id, state="ocr_waiting")
        return self.describe(job)

    def retry(self, job_id: str) -> dict[str, Any]:
        job = self.workflow.retry_failed(self._job_path(job_id))
        # 重试已归档的失败任务时清除归档标记，保证首页/历史状态一致。
        self.archive.unarchive(job_id)
        self.store.update_manifest(self._project_for_job(job_id), state="ocr_queued")
        return self.describe(job)

    def stop(self) -> None:
        self.workflow.stop_waiting()

    def archive_project_jobs(self, project_id: str) -> int:
        """Move finished OCR job state out of the active runtime index."""
        archived = 0
        trash = self.jobs_root / ".trash"
        for bridge in self.jobs_root.glob("*/literature-bridge.json"):
            try:
                value = json.loads(bridge.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if value.get("project_id") != project_id:
                continue
            trash.mkdir(parents=True, exist_ok=True)
            workspace = bridge.parent
            self.queue.remove(workspace.name)
            workspace.rename(trash / f"{uuid.uuid4().hex}-{workspace.name}")
            archived += 1
        return archived

    # -- listing --------------------------------------------------------
    def list(self, project_id: str | None = None) -> list[dict[str, Any]]:
        order = self.queue.order()
        positions = {job_id: index for index, job_id in enumerate(order)}
        running = self.running_job_id()
        output = []
        for job in self.workflow.list_jobs():
            try:
                mapped = self._project_for_job(job.job_id)
                manifest = self.store.load_manifest(mapped)
            except (StorageError, OSError, ValueError, KeyError):
                # 一个孤儿任务不能拖垮整个任务列表或启动恢复。
                continue
            if project_id and mapped != project_id:
                continue
            value = self.describe(job)
            value.update(
                project_id=mapped,
                title=manifest.get("title") or Path(job.source_pdf).stem,
                project_state=manifest.get("state", ""),
                source_size=manifest.get("source_size", job.source_size),
                source_name=manifest.get("source_name", Path(job.source_pdf).name),
                source_type=manifest.get("source_type", "pdf"),
                parse_coverage=manifest.get("parse_coverage"),
                queue_position=None if job.job_id == running else positions.get(job.job_id),
                running=job.job_id == running,
            )
            value.update(self._archive_meta(job.job_id))
            output.append(value)
        return sorted(output, key=lambda item: item["created_at"], reverse=True)

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.workflow.load_job(self._job_path(job_id))
        value = self.describe(job)
        project_id = self._project_for_job(job_id)
        manifest = self.store.load_manifest(project_id)
        value.update(
            project_id=project_id,
            title=manifest.get("title") or Path(job.source_pdf).stem,
            project_state=manifest.get("state", ""),
            source_size=manifest.get("source_size", job.source_size),
            source_name=manifest.get("source_name", Path(job.source_pdf).name),
            source_type=manifest.get("source_type", "pdf"),
            parse_coverage=manifest.get("parse_coverage"),
            queue_position=None if job.job_id == self.running_job_id() else self.queue.position(job_id),
            running=job.job_id == self.running_job_id(),
        )
        value.update(self._archive_meta(job.job_id))
        return value

    def _archive_meta(self, job_id: str) -> dict[str, Any]:
        record = self.archive.record(job_id)
        return {
            "archived": record is not None,
            "archived_at": str((record or {}).get("archived_at") or ""),
            "archive_reason": str((record or {}).get("reason") or ""),
        }

    # -- task center / archive ------------------------------------------
    def task_center(self, *, scope: str = "active", status: str = "", query: str = "",
                    sort: str = "recent", page: int = 1, page_size: int = 0) -> dict[str, Any]:
        items = self.list()
        if scope == "recent_completed":
            selected = filter_recent_completed(items)
            return {
                "items": selected, "total": len(selected), "page": 1,
                "page_size": RECENT_COMPLETED_LIMIT, "pages": 1,
                "counts": {"completed": len(selected), "total": len(selected)},
            }
        if scope == "history":
            size = int(page_size) if page_size else HISTORY_PAGE_SIZE
            filtered = sort_history(filter_history(items, query=query, status=status), sort)
            window, total, current, pages = paginate(filtered, page, size)
            return {"items": window, "total": total, "page": current, "page_size": size,
                    "pages": pages, "counts": {"total": total}}
        size = int(page_size) if page_size else ACTIVE_PAGE_SIZE
        active = sort_active(filter_active(items))
        window, total, current, pages = paginate(active, page, size)
        return {"items": window, "total": total, "page": current, "page_size": size,
                "pages": pages, "counts": count_active(items)}

    def archive_settings(self) -> dict[str, Any]:
        return self.archive.settings()

    def set_archive_settings(self, **changes: Any) -> dict[str, Any]:
        return self.archive.update_settings(**changes)

    def archive_job(self, job_id: str, *, reason: str = "manual", auto: bool = False) -> dict[str, Any]:
        # 与 retry/start/调度共用锁序（schedule_lock → job_lock），并在锁内重查状态：
        # 否则并发 retry 可能先把任务变为 queued 并清除标记，归档随后给活动任务加标记。
        with self._schedule_lock, self.job_lock(job_id):
            value = self.get(job_id)
            if not job_is_archivable(value):
                raise StorageError(
                    f"当前状态（{value.get('state')}）不能归档：只有已完成或失败的任务可以归档；"
                    "排队/运行/等待恢复中的任务不能归档。"
                )
            self.archive.archive_one(job_id, reason=reason or "manual", auto=auto)
            return self.get(job_id)

    def unarchive_job(self, job_id: str) -> dict[str, Any]:
        with self._schedule_lock, self.job_lock(job_id):
            self.get(job_id)
            self.archive.unarchive(job_id)
            return self.get(job_id)

    def auto_archive(self, *, force: bool = False, now: Any = None, capacity_only: bool = False) -> dict[str, Any]:
        """自动归档：保留期 + 条数上限。最老优先；不碰活动/可恢复/未解决失败任务。

        在 schedule_lock 内完成“快照 → 选择 → 写入”，避免与手动归档/取消/重试交错。
        completed 不能被 start/retry 变为活动状态，因此候选不会在写入前变成活动任务。
        """
        moment = now or utcnow()
        if not capacity_only and not force and not self.archive.due(now=moment):
            return {"skipped": True, "archived": [], "candidates": 0}
        with self._schedule_lock:
            settings = self.archive.settings()
            selected = select_auto_archive(self.list(), settings, now=moment, capacity_only=capacity_only)
            added = self.archive.archive_many(
                [(job_id, reason, True) for job_id, reason in selected], now=moment,
            )
            if not capacity_only:
                self.archive.mark_auto_run(now=moment)
        return {"skipped": False, "archived": added, "candidates": len(selected)}

    # -- coverage / continue -------------------------------------------
    def project_coverage(self, project_id: str) -> dict[str, Any]:
        manifest = self.store.load_manifest(project_id)
        coverage = manifest.get("parse_coverage")
        if isinstance(coverage, dict) and coverage:
            return coverage
        project = self.store.project_path(project_id)
        document_path = project / "document" / "document.json"
        if document_path.is_file():
            try:
                document = self.store.load_document(project_id)
            except Exception:
                document = None
            if document is not None and isinstance(document.coverage, dict) and document.coverage:
                return document.coverage
            # 有规范文档但没有可信 coverage 证据：仍按“旧已解析资源未知”保守处理。
            return {"state": "unknown", "source_page_count": 0, "covered_pages": 0, "ranges": []}
        # 从未生成规范文档：区分“尚未开始解析”和“页数未知（非 PDF / 无法读取）”。
        total = int(manifest.get("source_page_count") or 0)
        if total <= 0:
            try:
                source = self.store.source_path(project_id)
            except StorageError:
                source = None
            if source is not None and source.suffix.casefold() == ".pdf":
                try:
                    from .mineru_workflow import _open_pdf_page_count
                    total = int(_open_pdf_page_count(source) or 0)
                except Exception:
                    total = 0
        if total > 0:
            return {"state": "unparsed", "source_page_count": total, "covered_pages": 0, "ranges": []}
        return {"state": "unknown", "source_page_count": 0, "covered_pages": 0, "ranges": []}

    def migrate_project(self, project_id: str) -> dict[str, Any]:
        """按可信证据恢复旧资源的 coverage 与原页映射。

        必须同时满足：源文件 hash 与创建时一致；canonical `source_files` 与不可变 raw
        manifest 一致且 `verify_raw` 无错；存在已完成的、无 `page_ranges` 的旧作业，
        且其切片能按顺序与 `source_files` 的页数一一对应。任一不满足则保守 unknown。
        迁移会按上述对应同时修正 `page.page_index/source_page_index` 与 `block.page_index`，
        block_id/人工校对/译文保持不变。
        """
        project = self.store.project_path(project_id)
        if not (project / "document" / "document.json").is_file():
            return {"migrated": False, "reason": "no_document", "coverage": None}
        manifest = self.store.load_manifest(project_id)
        existing = manifest.get("parse_coverage")
        if isinstance(existing, dict) and existing.get("state") not in (None, "", "unknown"):
            return {"migrated": False, "reason": "already_known", "coverage": existing}

        # 1) 源文件 hash 必须与创建时一致。
        try:
            source = self.store.source_path(project_id)
            from .storage import sha256_file
            current_hash = sha256_file(source)
        except Exception:
            return {"migrated": False, "reason": "source_missing", "coverage": None}
        recorded_hash = str(manifest.get("source_sha256") or "")
        if recorded_hash and current_hash != recorded_hash:
            return {"migrated": False, "reason": "source_changed", "coverage": None}

        # 2) 不可变 raw 与 canonical source_files 一致，且历史哈希未被篡改。
        try:
            errors = self.store.verify_raw(project_id)
        except Exception:
            return {"migrated": False, "reason": "raw_manifest_missing", "coverage": None}
        if errors:
            return {"migrated": False, "reason": "raw_verification_failed", "coverage": None, "errors": errors[:3]}
        document = self.store.load_document(project_id)
        source_files = [item for item in document.source_files if isinstance(item, dict)]
        if not source_files or not document.pages:
            return {"migrated": False, "reason": "no_canonical_evidence", "coverage": None}
        raw_manifest = self.store.read_raw_manifest(project_id)
        sealed = [item for item in raw_manifest.get("slices", []) if isinstance(item, dict)]
        if len(sealed) != len(source_files):
            return {"migrated": False, "reason": "raw_slice_count_mismatch", "coverage": None}
        sealed_hashes = {str(file.get("sha256")) for item in sealed for file in item.get("files", []) if isinstance(file, dict)}
        if any(str(item.get("sha256")) not in sealed_hashes for item in source_files):
            return {"migrated": False, "reason": "raw_hash_mismatch", "coverage": None}
        expected_pages = sum(int(item.get("page_count") or 0) for item in source_files)
        if expected_pages != len(document.pages):
            return {"migrated": False, "reason": "canonical_page_count_mismatch", "coverage": None}

        source_total = 0
        if source.suffix.casefold() == ".pdf":
            try:
                from .mineru_workflow import _open_pdf_page_count
                source_total = _open_pdf_page_count(source)
            except Exception:
                source_total = 0

        # 3) 找一个能按顺序与 source_files 页数一一对应的已完成旧作业。
        page_mapping: dict[int, int] = {}
        trusted_job = ""
        mode = "trusted_job"
        completed_seen = False
        prefix_failure = ""
        for job in self.workflow.list_jobs():
            try:
                if self._project_for_job(job.job_id) != project_id:
                    continue
            except Exception:
                continue
            if job.state != JobState.COMPLETED:
                continue
            completed_seen = True
            if job.source_sha256 and job.source_sha256 != current_hash:
                continue
            ranges = str(job.page_ranges).strip()
            if ranges:
                # 旧 page_ranges 作业：仅接受有证据的连续前缀 1-N（其他范围不猜）。
                prefix_n = self._legacy_prefix_pages(ranges)
                if prefix_n is None:
                    prefix_failure = prefix_failure or "page_ranges_not_trusted_prefix"
                    continue
                ok, reason = self._legacy_prefix_evidence(
                    project, job, document, source_files, prefix_n, source_total,
                )
                if not ok:
                    prefix_failure = prefix_failure or reason
                    continue
                page_mapping = {page.page_index: page.page_index for page in document.pages}
                trusted_job = job.job_id
                mode = "legacy_prefix"
                break
            if len(job.slices) != len(source_files):
                continue
            slice_pages: list[list[int]] = []
            usable = True
            for item, source_file in zip(job.slices, source_files):
                if item.source_pages:
                    pages = [int(page) for page in item.source_pages]
                elif item.start_page >= 1 and item.end_page >= item.start_page:
                    pages = list(range(item.start_page, item.end_page + 1))
                else:
                    usable = False
                    break
                if len(pages) != int(source_file.get("page_count") or 0):
                    usable = False
                    break
                slice_pages.append(pages)
            if not usable:
                continue
            candidate: dict[int, int] = {}
            cursor = 0
            for pages in slice_pages:
                for source_page in pages:
                    candidate[document.pages[cursor].page_index] = source_page - 1
                    cursor += 1
            if len(candidate) != len(document.pages):
                continue
            page_mapping = candidate
            trusted_job = job.job_id
            break
        if not page_mapping:
            if prefix_failure:
                return {"migrated": False, "reason": prefix_failure, "coverage": None}
            if completed_seen:
                return {"migrated": False, "reason": "completed_but_unverifiable", "coverage": None}
            return {"migrated": False, "reason": "no_trusted_evidence", "coverage": None}

        covered = set(page_mapping.values())
        if source_total and max(covered) >= source_total:
            return {"migrated": False, "reason": "pages_exceed_source", "coverage": None}
        total = source_total or (max(covered) + 1)
        payload = coverage_payload(total, covered)
        with self.store.document_lock(project_id):
            document = self.store.load_document(project_id)
            if any(page.page_index not in page_mapping for page in document.pages):
                return {"migrated": False, "reason": "page_mapping_incomplete", "coverage": None}
            for page in document.pages:
                mapped = page_mapping[page.page_index]
                page.source_page_index = mapped
                page.page_index = mapped
            document.pages = [page for _orig, page in sorted(enumerate(document.pages), key=lambda item: (item[1].page_index, item[0]))]
            for block in document.blocks:
                if block.page_index in page_mapping:
                    block.page_index = page_mapping[block.page_index]
            document.blocks = [block for _orig, block in sorted(enumerate(document.blocks), key=lambda item: (item[1].page_index, item[0]))]
            document.coverage = payload
            document.page_count = total or document.page_count
            self.store.save_document(document)
        self.store.update_manifest(
            project_id, state="ready", source_page_count=total or None, parse_coverage=payload,
        )
        return {
            "migrated": True, "reason": mode, "mode": mode, "coverage": payload,
            "jobs": [trusted_job], "pages": sorted(covered),
        }

    @staticmethod
    def _legacy_prefix_pages(page_ranges: str) -> int | None:
        """旧 `page_ranges` 是否为可信连续前缀 `1` 或 `1-N`；其他范围返回 None。"""
        match = re.fullmatch(r"\s*1\s*(?:-\s*(\d+)\s*)?", page_ranges or "")
        if not match:
            return None
        return int(match.group(1)) if match.group(1) else 1

    def _legacy_prefix_evidence(
        self, project: Path, job: Any, document: Any, source_files: list[dict[str, Any]],
        prefix_n: int, source_total: int,
    ) -> tuple[bool, str]:
        """校验旧 `1-N` 前缀作业能否可信恢复。不靠文字或文件名猜页序，只看结构证据。"""
        if len(job.slices) != 1 or len(source_files) != 1:
            return False, "prefix_slice_count_mismatch"
        item = job.slices[0]
        if item.state.value not in {"downloaded", "remote_done"}:
            return False, "prefix_slice_not_downloaded"
        if item.start_page != 1 or item.end_page != prefix_n:
            return False, "prefix_slice_range_mismatch"
        if int(job.page_count or 0) not in (0, prefix_n):
            return False, "prefix_job_page_count_mismatch"
        if int(source_files[0].get("page_count") or 0) != prefix_n:
            return False, "prefix_canonical_page_mismatch"
        if len(document.pages) != prefix_n or int(document.page_count or 0) not in (0, prefix_n):
            return False, "prefix_document_page_mismatch"
        if source_total and prefix_n > source_total:
            return False, "prefix_pages_exceed_source"
        source_path = project / str(source_files[0].get("path") or "")
        slice_dir = source_path.parent
        if not slice_dir.is_dir() or not source_path.is_file():
            return False, "prefix_raw_slice_missing"
        v2_candidates = sorted(slice_dir.glob("*_content_list_v2.json")) or sorted(slice_dir.glob("content_list_v2.json"))
        if not v2_candidates:
            return False, "prefix_content_missing"
        try:
            value = json.loads(v2_candidates[0].read_text(encoding="utf-8"))
        except Exception:
            return False, "prefix_content_unreadable"
        if not isinstance(value, list) or len(value) != prefix_n:
            return False, "prefix_content_page_mismatch"
        geometry_ok = False
        layout_candidates = sorted(slice_dir.glob("*_layout.json")) + sorted(slice_dir.glob("layout.json"))
        if layout_candidates:
            try:
                payload = json.loads(layout_candidates[0].read_text(encoding="utf-8"))
                info = payload.get("pdf_info") if isinstance(payload, dict) else None
                geometry_ok = isinstance(info, list) and len(info) == prefix_n
            except Exception:
                geometry_ok = False
            if not geometry_ok:
                return False, "prefix_layout_page_mismatch"
        origin_ok = False
        origin_candidates = sorted(slice_dir.glob("*_origin.pdf"))
        if origin_candidates:
            try:
                from .mineru_workflow import _open_pdf_page_count
                origin_ok = _open_pdf_page_count(origin_candidates[0]) == prefix_n
            except Exception:
                origin_ok = False
            if not origin_ok:
                return False, "prefix_origin_page_mismatch"
        if not geometry_ok and not origin_ok:
            return False, "prefix_no_page_evidence"
        return True, ""

    def continue_parse(
        self, project_id: str, *, page_ranges: str = "", language: str = "",
        model_version: str = "", page_limit: int = 200, extra_formats: list[str] | None = None,
    ) -> tuple[dict[str, Any], list[int]]:
        """对同一资源继续解析未覆盖的源页；返回 (job, 被过滤的已覆盖页)。

        整个“检查活动作业 + 创建新作业”在一个项目临界区内完成，两个并发 continue
        只会有一个成功，另一个明确冲突（不产生两个并存作业）。
        """
        with self.project_lock(project_id):
            return self._continue_parse_locked(
                project_id, page_ranges=page_ranges, language=language,
                model_version=model_version, page_limit=page_limit, extra_formats=extra_formats,
            )

    def _continue_parse_locked(
        self, project_id: str, *, page_ranges: str = "", language: str = "",
        model_version: str = "", page_limit: int = 200, extra_formats: list[str] | None = None,
    ) -> tuple[dict[str, Any], list[int]]:
        project = self.store.project_path(project_id)
        if not (project / "document" / "document.json").is_file():
            raise StorageError("该资源还没有规范文档；要首次解析请使用“开始解析”。")
        coverage = self.project_coverage(project_id)
        total = int(coverage.get("source_page_count") or 0)
        if total <= 0 or coverage.get("state") == "unknown":
            # 旧资源尝试按可信历史证据迁移；无法证实才保持 unknown。
            migration = self.migrate_project(project_id)
            coverage = migration.get("coverage") or self.project_coverage(project_id)
            total = int(coverage.get("source_page_count") or 0)
        if total <= 0 or coverage.get("state") == "unknown":
            raise StorageError(
                "无法自动确认该资源的原页映射：历史任务缺少可核对的页码证据"
                "（例如源文件已变动、缺少可对应的已封存 raw）。"
                "请先在资源详情点击“检查解析范围”尝试按历史证据恢复；若仍失败，"
                "需要人工核对原 PDF 已解析的页码范围后再继续。"
            )
        covered: set[int] = set()
        for item in coverage.get("ranges") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                try:
                    start, end = int(item[0]), int(item[1])
                except (TypeError, ValueError):
                    continue
                covered.update(range(start - 1, end))
        remaining = [page for page in range(1, total + 1) if (page - 1) not in covered]
        if not remaining:
            raise StorageError("该资源已经完整解析，无需继续。")
        if page_ranges.strip():
            requested = parse_page_ranges(page_ranges, total)
        else:
            requested = remaining
        skipped = sorted({page for page in requested if page not in remaining})
        selected = sorted({page for page in requested if page in remaining})
        if not selected:
            raise StorageError("所选页面都已经解析完成，没有需要继续的页面。")

        previous = self.list(project_id)
        template = next((job for job in previous if job.get("append")), previous[0] if previous else None)
        resolved_language = language or (template or {}).get("language") or "ch"
        resolved_model = model_version or (template or {}).get("model_version") or "vlm"
        resolved_formats = list(extra_formats) if extra_formats is not None else list((template or {}).get("extra_formats") or [])
        job = self.create(
            project_id,
            language=resolved_language, model_version=resolved_model,
            is_ocr=bool((template or {}).get("is_ocr", True)),
            enable_table=bool((template or {}).get("enable_table", True)),
            enable_formula=bool((template or {}).get("enable_formula", True)),
            page_ranges="", requested_pages=selected, append=True,
            page_limit=page_limit, extra_formats=resolved_formats,
            enforce_single=True,
        )
        return job, skipped

    # -- first parse / queue / scheduler --------------------------------
    def start_parse(
        self, project_id: str, *, page_ranges: str = "", language: str = "ch",
        model_version: str = "vlm", is_ocr: bool = True, enable_table: bool = True,
        enable_formula: bool = True, page_limit: int = 200,
        extra_formats: list[str] | None = None,
    ) -> dict[str, Any]:
        """为从未生成规范文档的资源创建首个解析任务（语义不同于“继续解析”）。

        这是一个完整的服务端操作：在锁内依次完成 Token 校验、活动作业检查、
        无规范文档检查、源文件存在/hash 校验、创建与调度。锁序遵守
        ``schedule_lock → project_lock``（schedule_lock 为可重入锁，
        ``start_job`` 再取一次也不会死锁）。两个并发点击只会创建一个作业；
        创建成功但调度失败时任务会保留为可恢复的 queued 状态并报出具体错误。
        """
        with self._schedule_lock, self.project_lock(project_id):
            if not get_mineru_token():
                # 没有 Token 时不创建任何作业，避免孤儿任务。
                raise StorageError("请先配置 MinerU Token。")
            job = self._start_parse_locked(
                project_id, page_ranges=page_ranges, language=language,
                model_version=model_version, is_ocr=is_ocr, enable_table=enable_table,
                enable_formula=enable_formula, page_limit=page_limit, extra_formats=extra_formats,
            )
            try:
                self.start_job(job["job_id"])
            except Exception as error:
                raise StorageError(
                    f"解析任务已经创建（{job['job_id']}），但未能加入调度队列：{error}。"
                    "该任务会保留为可恢复状态，请在配置 MinerU Token 后从资源详情恢复或重试。"
                ) from error
            return self.get(job["job_id"])

    def _start_parse_locked(
        self, project_id: str, *, page_ranges: str, language: str, model_version: str,
        is_ocr: bool, enable_table: bool, enable_formula: bool, page_limit: int,
        extra_formats: list[str] | None,
    ) -> dict[str, Any]:
        project = self.store.project_path(project_id)
        if (project / "document" / "document.json").is_file():
            raise StorageError("该资源已经生成规范文档；如需补齐未解析页，请使用“继续解析”。")
        if self.active_job(project_id) is not None:
            raise StorageError("该资源已有正在进行的解析任务；请等待完成或先恢复/重试。")
        try:
            source = self.store.source_path(project_id)
        except StorageError as error:
            raise StorageError(f"无法开始解析：{error}") from error
        manifest = self.store.load_manifest(project_id)
        recorded_hash = str(manifest.get("source_sha256") or "")
        if recorded_hash:
            from .storage import sha256_file
            if sha256_file(source) != recorded_hash:
                raise StorageError(
                    "该资源的源文件与创建时不一致（可能被替换或修改）；"
                    "为避免把错误文件解析成规范文档，请重新导入资源后再解析。"
                )
        selected: list[int] = []
        if source.suffix.casefold() == ".pdf":
            page_count = 0
            try:
                from .mineru_workflow import _open_pdf_page_count
                page_count = int(_open_pdf_page_count(source) or 0)
            except Exception as error:
                raise StorageError(f"无法读取源 PDF 页数：{error}") from error
            if page_ranges.strip():
                selected = parse_page_ranges(page_ranges, page_count)
                if not selected:
                    raise StorageError("所选页码为空，请检查页码范围。")
        elif page_ranges.strip():
            raise StorageError("非 PDF 文件不支持指定页码范围，请留空解析整份文件。")
        return self.create(
            project_id, language=language or "ch", model_version=model_version or "vlm",
            is_ocr=is_ocr, enable_table=enable_table, enable_formula=enable_formula,
            page_ranges="", requested_pages=selected or None,
            append=False, page_limit=page_limit, extra_formats=extra_formats,
            enforce_single=True,
        )

    def active_job(self, project_id: str) -> dict[str, Any] | None:
        for job in self.list(project_id):
            if job.get("state") in _ACTIVE_PARSE_STATES:
                return job
        return None

    def queue_snapshot(self) -> dict[str, Any]:
        order = self.queue.order()
        entries = []
        for index, job_id in enumerate(order):
            try:
                value = self.get(job_id)
            except Exception:
                entries.append({"job_id": job_id, "position": index, "state": None})
                continue
            entries.append({
                "job_id": job_id, "position": index, "state": value.get("state"),
                "project_id": value.get("project_id"), "title": value.get("title"),
                "source_name": value.get("source_name"),
                "running": value.get("running", False), "queue_position": value.get("queue_position"),
            })
        return {"order": order, "entries": entries, "running": self.running_job_id()}

    def running_job_id(self) -> str | None:
        with self._running_lock:
            return self._running_job_id

    def is_running(self, job_id: str) -> bool:
        return self.running_job_id() == job_id

    def enqueue(self, job_id: str, *, position: str = "end") -> int:
        with self._schedule_lock:
            index = self.queue.enqueue(job_id, position)
        self._scheduler_event.set()
        return index

    def wake(self) -> None:
        """仅唤醒调度器，不改变队列顺序（用于已在队列中的作业）。"""
        self._scheduler_event.set()

    def move_in_queue(self, job_id: str, position: str) -> int:
        with self._schedule_lock:
            if self.is_running(job_id):
                raise StorageError("该解析任务正在执行，不能调整顺序。")
            index = self.queue.move(job_id, position)
        self._scheduler_event.set()
        return index

    def start_job(self, job_id: str) -> str:
        """原子启动：任务不存在报错；运行中返回 already_running；非法状态拒绝。"""
        with self._schedule_lock, self.job_lock(job_id):
            job = self.get(job_id)
            if self.is_running(job_id):
                return "already_running"
            state = job.get("state")
            if state not in {"queued", "waiting", "interrupted"}:
                raise StorageError(
                    f"当前状态（{state}）不能启动：已完成的任务请用“继续解析”，失败请用“重试失败分段”。"
                )
            if self.queue.position(job_id) is None:
                self.queue.enqueue(job_id, position="end")
            self._scheduler_event.set()
            return "queued"

    def retry_job(self, job_id: str) -> dict[str, Any]:
        """原子重试：不存在报错、运行中冲突；重置失败切片后入队（已在队列不重复）。"""
        with self._schedule_lock, self.job_lock(job_id):
            current = self.get(job_id)
            if self.is_running(job_id):
                raise StorageError("该解析任务正在执行，无法重试。")
            # 已在等待执行的作业无需重复重置/入队（去重）。
            if current.get("state") in {"queued", "waiting", "interrupted"} and self.queue.position(job_id) is not None:
                return current
            self.retry(job_id)
            if self.queue.position(job_id) is None:
                self.queue.enqueue(job_id, position="end")
            self._scheduler_event.set()
            return self.get(job_id)

    def reorder_job(self, job_id: str, position: str) -> int:
        """原子调序：只有等待/排队/中断中的任务可调序；运行/已完成/失败拒绝。"""
        with self._schedule_lock, self.job_lock(job_id):
            job = self.get(job_id)
            if self.is_running(job_id):
                raise StorageError("该解析任务正在执行，不能调整顺序。")
            if job.get("state") not in {"queued", "waiting", "interrupted"}:
                raise StorageError(
                    f"只有等待中的解析任务可以调整顺序（当前 {job.get('state')}）。"
                )
            return self.queue.move(job_id, position)

    def resume_all(self) -> int:
        """把等待/中断/排队中的作业按队列顺序恢复；跳过无法解析的孤儿任务。"""
        with self._schedule_lock:
            self.queue.prune(lambda value: self._job_state(value) is not None)
            added = 0
            for job in sorted(self.workflow.list_jobs(), key=lambda item: item.created_at):
                if job.state not in RESUMABLE_JOB_STATES:
                    continue
                if self.queue.position(job.job_id) is not None:
                    continue
                try:
                    self._project_for_job(job.job_id)
                except Exception:
                    continue
                if not self._job_path(job.job_id).is_file():
                    continue
                self.queue.enqueue(job.job_id)
                added += 1
        if added:
            self._scheduler_event.set()
        return added

    def recover_interrupted(self) -> int:
        recovered = 0
        for job in self.workflow.list_jobs():
            if job.state in ACTIVE_JOB_STATES:
                self.workflow.mark_interrupted(job)
                recovered += 1
        return recovered

    def start_scheduler(self) -> None:
        with self._scheduler_lock:
            if self._scheduler_thread and self._scheduler_thread.is_alive():
                return
            self._scheduler_stop.clear()
            self._scheduler_thread = threading.Thread(
                target=self._scheduler_loop, name="literature-parse-scheduler", daemon=True,
            )
            self._scheduler_thread.start()
        self._scheduler_event.set()

    def stop_scheduler(self) -> None:
        self._scheduler_stop.set()
        self._scheduler_event.set()
        thread = self._scheduler_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            # 给当前作业一个安全边界退出的机会；超时也不能强制中断正在写库的批次。
            thread.join(timeout=10.0)

    def _job_state(self, job_id: str) -> str | None:
        try:
            return self.workflow.load_job(self._job_path(job_id)).state.value
        except Exception:
            return None

    def _scheduler_loop(self) -> None:
        runnable = {state.value for state in RESUMABLE_JOB_STATES}
        while not self._scheduler_stop.is_set():
            self._scheduler_event.wait(timeout=1.0)
            self._scheduler_event.clear()
            if self._scheduler_stop.is_set():
                break
            # 运行中每天最多一次的自动归档；due() 只读内存中的持久化时间。
            if self.archive.due():
                try:
                    self.auto_archive()
                except Exception:
                    logger.exception("Auto archive daily pass failed")
            if not get_mineru_token():
                # 没有 Token 时不消耗轮询；保存 Token 后由 resume_all 唤醒。
                continue
            # 第一次候选选择不持锁（可能并发调序）。
            if not select_next_runnable(self.queue.order(), self._job_state, runnable):
                with self._schedule_lock:
                    self.queue.prune(lambda value: self._job_state(value) in runnable)
                continue
            # 领取临界区：重新检查队首 + 确认没有运行任务 + 停止标志，然后从队列移除。
            with self._schedule_lock:
                job_id = select_next_runnable(self.queue.order(), self._job_state, runnable)
                if not job_id:
                    continue
                if self._running_job_id is not None:
                    continue
                if self._scheduler_stop.is_set():
                    break
                self._running_job_id = job_id
                # 领取后从等待队列移除，使等待序号真实反映排队任务。
                self.queue.remove(job_id)
            try:
                self.workflow._stop_event.clear()
                self.run(job_id)
            except Exception:
                logger.exception("MinerU job %s failed", job_id)
            finally:
                with self._running_lock:
                    self._running_job_id = None

    @staticmethod
    def describe(job: WorkflowJob) -> dict[str, Any]:
        total_pages = job.page_count or sum(item.page_count for item in job.slices)
        submitted_states = {"uploaded", "processing", "remote_done", "downloaded"}
        completed_states = {"remote_done", "downloaded"}
        submitted_pages = sum(item.page_count for item in job.slices if item.state.value in submitted_states)
        completed_pages = sum(item.page_count for item in job.slices if item.state.value in completed_states)
        downloaded_pages = sum(item.page_count for item in job.slices if item.state.value == "downloaded")
        failed_slices = sum(item.state.value == "failed" for item in job.slices)
        if job.state == JobState.COMPLETED:
            stage_progress: float | None = 1.0
        elif job.state == JobState.UPLOADING and total_pages:
            stage_progress = submitted_pages / total_pages
        elif job.state == JobState.PROCESSING and total_pages:
            stage_progress = completed_pages / total_pages
        elif job.state == JobState.DOWNLOADING and total_pages:
            stage_progress = downloaded_pages / total_pages
        else:
            stage_progress = None
        can_resume = job.state in {JobState.WAITING, JobState.INTERRUPTED}
        finished_at = job.finished_at
        finished_at_estimated = False
        if job.state == JobState.COMPLETED and not finished_at:
            # 旧任务没有 finished_at：回退 updated_at，并标注兼容依据（绝不用 created_at）。
            finished_at = job.updated_at
            finished_at_estimated = True
        return {
            "job_id": job.job_id, "state": job.state.value, "error": job.error,
            "page_count": job.page_count, "page_limit": job.page_limit, "output": job.output_epub,
            "model_version": job.model_version, "language": job.language,
            "is_ocr": job.is_ocr, "enable_table": job.enable_table,
            "enable_formula": job.enable_formula, "page_ranges": job.page_ranges,
            "extra_formats": job.extra_formats,
            "source_page_count": job.source_page_count,
            "requested_pages": job.requested_pages,
            "requested_ranges": job.requested_ranges,
            "append": job.append,
            "backend_id": job.backend_id,
            "backend_config": job.backend_config,
            "this_task_pages": len(job.requested_pages) or job.page_count,
            "slice_count": len(job.slices),
            "completed_slices": sum(item.state.value in completed_states for item in job.slices),
            "downloaded_slices": sum(item.state.value == "downloaded" for item in job.slices),
            "failed_slices": failed_slices,
            "submitted_pages": submitted_pages,
            "completed_pages": completed_pages,
            "downloaded_pages": downloaded_pages,
            "stage_progress": stage_progress,
            "can_retry": job.state == JobState.FAILED or failed_slices > 0,
            "can_resume": can_resume,
            "can_start": job.state in {JobState.QUEUED, JobState.WAITING, JobState.INTERRUPTED},
            "slices": [
                {"index": item.index, "start_page": item.start_page, "end_page": item.end_page,
                 "page_count": item.page_count, "state": item.state.value,
                 "remote_state": item.remote_state, "attempts": item.attempts, "error": item.error,
                 "source_pages": item.source_pages}
                for item in job.slices
            ],
            "created_at": job.created_at, "updated_at": job.updated_at,
            "finished_at": finished_at, "finished_at_estimated": finished_at_estimated,
        }


__all__ = ["MinerUService", "get_mineru_token", "save_mineru_token", "delete_mineru_token", "format_page_ranges"]
