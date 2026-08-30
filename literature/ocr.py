from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .mineru import normalize_imported_project
from .mineru_workflow import (
    JobState, MinerUWorkflow, WorkflowJob, delete_mineru_token, get_mineru_token,
    save_mineru_token,
)
from .storage import ProjectStore, StorageError, atomic_json


LANGUAGE_MAP = {"auto": "ch", "en": "en", "ja": "japan", "fr": "latin", "de": "latin"}


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

    def _bridge_path(self, job_id: str) -> Path:
        return self.jobs_root / job_id / "literature-bridge.json"

    def _project_for_job(self, job_id: str) -> str:
        path = self._bridge_path(job_id)
        if not path.is_file():
            raise StorageError("MinerU 任务缺少 Literature 项目映射。")
        return str(json.loads(path.read_text(encoding="utf-8"))["project_id"])

    def _finish_import(self, job: WorkflowJob, inputs: list[str]) -> None:
        project_id = self._project_for_job(job.job_id)
        self.store.import_raw_mineru(project_id, inputs)
        normalize_imported_project(self.store, project_id)

    def create(
        self, project_id: str, *, language: str = "auto", model_version: str = "vlm",
        is_ocr: bool = True, enable_table: bool = True, enable_formula: bool = True,
        page_limit: int = 200,
    ) -> dict[str, Any]:
        project = self.store.project_path(project_id)
        source = project / "source" / "origin.pdf"
        job = self.workflow.create_job(
            source, project / "exports" / ".mineru-workflow.epub",
            model_version=model_version, language=LANGUAGE_MAP.get(language, language), is_ocr=is_ocr,
            enable_table=enable_table, enable_formula=enable_formula, page_limit=page_limit,
            keep_original_pdf=False,
        )
        atomic_json(self._bridge_path(job.job_id), {"project_id": project_id})
        self.store.update_manifest(project_id, state="ocr_queued", mineru_job_id=job.job_id)
        return self.describe(job)

    def run(self, job_id: str) -> dict[str, Any]:
        project_id = self._project_for_job(job_id)
        self.store.update_manifest(project_id, state="ocr_processing")
        try:
            job = self.workflow.run_or_resume(self.jobs_root / job_id / "job.json")
        except Exception:
            self.store.update_manifest(project_id, state="ocr_failed")
            raise
        if job.state == JobState.COMPLETED:
            self.store.update_manifest(project_id, state="ready")
        elif job.state == JobState.WAITING:
            self.store.update_manifest(project_id, state="ocr_waiting")
        return self.describe(job)

    def retry(self, job_id: str) -> dict[str, Any]:
        job = self.workflow.retry_failed(self.jobs_root / job_id / "job.json")
        self.store.update_manifest(self._project_for_job(job_id), state="ocr_queued")
        return self.describe(job)

    def stop(self) -> None:
        self.workflow.stop_waiting()

    def list(self, project_id: str | None = None) -> list[dict[str, Any]]:
        output = []
        for job in self.workflow.list_jobs():
            try:
                mapped = self._project_for_job(job.job_id)
            except StorageError:
                continue
            if project_id and mapped != project_id:
                continue
            value = self.describe(job)
            manifest = self.store.load_manifest(mapped)
            value.update(
                project_id=mapped,
                title=manifest.get("title") or Path(job.source_pdf).stem,
                project_state=manifest.get("state", ""),
                source_size=manifest.get("source_size", job.source_size),
            )
            output.append(value)
        return sorted(output, key=lambda item: item["created_at"], reverse=True)

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.workflow.load_job(self.jobs_root / job_id / "job.json")
        value = self.describe(job)
        project_id = self._project_for_job(job_id)
        manifest = self.store.load_manifest(project_id)
        value.update(
            project_id=project_id,
            title=manifest.get("title") or Path(job.source_pdf).stem,
            project_state=manifest.get("state", ""),
            source_size=manifest.get("source_size", job.source_size),
        )
        return value

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
        return {
            "job_id": job.job_id, "state": job.state.value, "error": job.error,
            "page_count": job.page_count, "page_limit": job.page_limit, "output": job.output_epub,
            "slice_count": len(job.slices),
            "completed_slices": sum(item.state.value in completed_states for item in job.slices),
            "downloaded_slices": sum(item.state.value == "downloaded" for item in job.slices),
            "failed_slices": failed_slices,
            "submitted_pages": submitted_pages,
            "completed_pages": completed_pages,
            "downloaded_pages": downloaded_pages,
            "stage_progress": stage_progress,
            "can_retry": job.state == JobState.FAILED or failed_slices > 0,
            "slices": [
                {"index": item.index, "start_page": item.start_page, "end_page": item.end_page,
                 "page_count": item.page_count, "state": item.state.value,
                 "remote_state": item.remote_state, "attempts": item.attempts, "error": item.error}
                for item in job.slices
            ],
            "created_at": job.created_at, "updated_at": job.updated_at,
        }


__all__ = ["MinerUService", "get_mineru_token", "save_mineru_token", "delete_mineru_token"]
