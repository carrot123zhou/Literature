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
        job = self.workflow.run_or_resume(self.jobs_root / job_id / "job.json")
        project_id = self._project_for_job(job_id)
        if job.state == JobState.COMPLETED:
            self.store.update_manifest(project_id, state="ready")
        return self.describe(job)

    def retry(self, job_id: str) -> dict[str, Any]:
        job = self.workflow.retry_failed(self.jobs_root / job_id / "job.json")
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
            value["project_id"] = mapped
            output.append(value)
        return output

    @staticmethod
    def describe(job: WorkflowJob) -> dict[str, Any]:
        return {
            "job_id": job.job_id, "state": job.state.value, "error": job.error,
            "page_count": job.page_count, "output": job.output_epub,
            "slices": [
                {"index": item.index, "start_page": item.start_page, "end_page": item.end_page,
                 "state": item.state.value, "error": item.error}
                for item in job.slices
            ],
            "created_at": job.created_at, "updated_at": job.updated_at,
        }


__all__ = ["MinerUService", "get_mineru_token", "save_mineru_token", "delete_mineru_token"]
