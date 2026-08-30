from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .mineru_capabilities import source_kind, validate_source
from .models import Document, StructurePatch, TranslationLayer, UsageRecord, utc_now


class StorageError(RuntimeError):
    pass


def default_library_root() -> Path:
    configured = os.environ.get("LITERATURE_LIBRARY", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / "Documents" / "Literature"


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value).strip(" .-")
    return value[:120] or "Untitled"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


class ProjectStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_library_root().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    @contextmanager
    def _connect(path: Path):
        connection = sqlite3.connect(path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_projects(self) -> list[dict[str, Any]]:
        projects = []
        for manifest in self.root.glob("*/project.json"):
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
                value["path"] = str(manifest.parent)
                projects.append(value)
            except (OSError, ValueError):
                continue
        return sorted(projects, key=lambda item: item.get("updated_at", ""), reverse=True)

    def project_path(self, project_id: str) -> Path:
        for manifest in self.root.glob("*/project.json"):
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if value.get("project_id") == project_id:
                return manifest.parent
        raise StorageError(f"项目不存在：{project_id}")

    def create_project(
        self, source_file: str | Path, *, title: str = "", source_language: str = "auto",
        target_language: str = "zh-CN",
    ) -> dict[str, Any]:
        try:
            source = validate_source(source_file)
        except ValueError as error:
            raise StorageError(str(error)) from error
        name = safe_name(title or source.stem)
        destination = self.root / name
        index = 2
        while destination.exists():
            destination = self.root / f"{name} ({index})"
            index += 1
        for child in ("source", "ocr/raw/mineru", "document", "translations", "assets", "exports"):
            (destination / child).mkdir(parents=True, exist_ok=True)
        copied = destination / "source" / f"origin{source.suffix.casefold()}"
        shutil.copy2(source, copied)
        project_id = uuid.uuid4().hex
        manifest = {
            "project_id": project_id, "title": title or source.stem,
            "source_language": source_language, "source_language_mode": source_language,
            "target_language": target_language,
            "source_file": copied.relative_to(destination).as_posix(),
            "source_name": source.name, "source_type": source_kind(source),
            "source_sha256": sha256_file(copied),
            "source_size": copied.stat().st_size,
            "budget_usd": 0.0,
            "state": "created", "created_at": utc_now(), "updated_at": utc_now(),
        }
        atomic_json(destination / "project.json", manifest)
        self._init_database(destination / "literature.sqlite")
        return {**manifest, "path": str(destination)}

    def source_path(self, project_id: str) -> Path:
        project = self.project_path(project_id)
        manifest = self.load_manifest(project_id)
        relative = str(manifest.get("source_file") or manifest.get("source_pdf") or "")
        candidate = (project / relative).resolve()
        if not relative or candidate.parent != (project / "source").resolve() or not candidate.is_file():
            raise StorageError("项目源文件不存在。")
        return candidate

    def load_manifest(self, project_id: str) -> dict[str, Any]:
        path = self.project_path(project_id) / "project.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def update_manifest(self, project_id: str, **changes: Any) -> dict[str, Any]:
        project = self.project_path(project_id)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value.update(changes)
        value["updated_at"] = utc_now()
        atomic_json(project / "project.json", value)
        return value

    def save_document(self, document: Document) -> None:
        project = self.project_path(document.project_id)
        document.updated_at = utc_now()
        atomic_json(project / "document" / "document.json", document.to_dict())

    def load_document(self, project_id: str) -> Document:
        path = self.project_path(project_id) / "document" / "document.json"
        if not path.is_file():
            raise StorageError("项目尚未生成规范文档。")
        return Document.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def translation_path(self, project_id: str, locale: str) -> Path:
        return self.project_path(project_id) / "translations" / locale / "translation.json"

    def load_translation(self, project_id: str, locale: str = "zh-CN") -> TranslationLayer:
        path = self.translation_path(project_id, locale)
        if not path.is_file():
            return TranslationLayer(project_id=project_id, locale=locale)
        return TranslationLayer.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_translation(self, layer: TranslationLayer) -> None:
        layer.updated_at = utc_now()
        atomic_json(self.translation_path(layer.project_id, layer.locale), layer.to_dict())

    def import_raw_mineru(self, project_id: str, sources: Iterable[str | Path]) -> list[dict[str, Any]]:
        project = self.project_path(project_id)
        raw_root = project / "ocr" / "raw" / "mineru"
        if any(raw_root.iterdir()):
            raise StorageError("MinerU 原始目录已经封存；请新建项目以导入另一份结果。")
        staging = raw_root.parent / f".mineru-import-{uuid.uuid4().hex}"
        manifests = []
        try:
            staging.mkdir()
            for index, raw in enumerate(sources, 1):
                source = Path(raw).expanduser().resolve()
                if not source.is_dir():
                    raise StorageError(f"MinerU 结果目录不存在：{source}")
                slice_name = f"slice-{index:03d}"
                destination = staging / slice_name
                shutil.copytree(source, destination)
                files = []
                for file in sorted(item for item in destination.rglob("*") if item.is_file()):
                    final_relative = Path("ocr/raw/mineru") / slice_name / file.relative_to(destination)
                    files.append({"path": str(final_relative), "size": file.stat().st_size, "sha256": sha256_file(file)})
                    file.chmod(0o444)
                manifests.append({"slice": slice_name, "source": str(source), "files": files})
            raw_root.rmdir()
            staging.rename(raw_root)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raw_root.mkdir(parents=True, exist_ok=True)
            raise
        atomic_json(project / "ocr" / "raw" / "manifest.json", {"sealed_at": utc_now(), "slices": manifests})
        self.update_manifest(project_id, state="ocr_imported")
        return manifests

    def verify_raw(self, project_id: str) -> list[str]:
        project = self.project_path(project_id)
        manifest_path = project / "ocr" / "raw" / "manifest.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        errors = []
        for slice_info in value.get("slices", []):
            for item in slice_info.get("files", []):
                path = project / item["path"]
                if not path.is_file():
                    errors.append(f"缺少：{item['path']}")
                elif sha256_file(path) != item["sha256"]:
                    errors.append(f"哈希变化：{item['path']}")
        return errors

    def record_patch(self, project_id: str, patch: StructurePatch) -> None:
        database = self.project_path(project_id) / "literature.sqlite"
        with self._connect(database) as connection:
            connection.execute(
                """INSERT INTO structure_patches
                (patch_id, block_id, operation, before_json, after_json, confidence, source, reason, automatic, reverted_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (patch.patch_id, patch.block_id, patch.operation, json.dumps(patch.before), json.dumps(patch.after),
                 patch.confidence, patch.source, patch.reason, int(patch.automatic), patch.reverted_at, patch.created_at),
            )

    def list_patches(self, project_id: str) -> list[dict[str, Any]]:
        database = self.project_path(project_id) / "literature.sqlite"
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM structure_patches ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["before"] = json.loads(item.pop("before_json"))
            item["after"] = json.loads(item.pop("after_json"))
            item["automatic"] = bool(item["automatic"])
            result.append(item)
        return result

    def mark_patch_reverted(self, project_id: str, patch_id: str, reverted_at: str) -> None:
        database = self.project_path(project_id) / "literature.sqlite"
        with self._connect(database) as connection:
            connection.execute("UPDATE structure_patches SET reverted_at=? WHERE patch_id=?", (reverted_at, patch_id))

    def record_usage(self, record: UsageRecord) -> None:
        database = self.project_path(record.project_id) / "literature.sqlite"
        with self._connect(database) as connection:
            connection.execute(
                """INSERT INTO usage_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.usage_id, record.project_id, record.job_id, record.provider, record.model, record.scope,
                 record.input_tokens, record.output_tokens, record.cached_tokens, record.reasoning_tokens,
                 record.estimated_cost, record.currency, record.request_id, record.created_at),
            )

    def create_job(self, project_id: str, kind: str, scope: dict[str, Any], config: dict[str, Any], *, job_id: str | None = None) -> str:
        job_id = job_id or uuid.uuid4().hex
        database = self.project_path(project_id) / "literature.sqlite"
        timestamp = utc_now()
        with self._connect(database) as connection:
            connection.execute(
                """INSERT INTO jobs
                (job_id, project_id, kind, scope_json, status, progress, config_json, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', 0, ?, '', ?, ?)""",
                (job_id, project_id, kind, json.dumps(scope, ensure_ascii=False), json.dumps(config, ensure_ascii=False), timestamp, timestamp),
            )
        return job_id

    def update_job(self, project_id: str, job_id: str, *, status: str | None = None, progress: float | None = None, error: str | None = None) -> None:
        assignments = ["updated_at=?"]
        values: list[Any] = [utc_now()]
        if status is not None:
            assignments.append("status=?")
            values.append(status)
        if progress is not None:
            assignments.append("progress=?")
            values.append(max(0.0, min(1.0, float(progress))))
        if error is not None:
            assignments.append("error=?")
            values.append(error)
        values.append(job_id)
        database = self.project_path(project_id) / "literature.sqlite"
        with self._connect(database) as connection:
            connection.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id=?", values)

    def get_job(self, project_id: str, job_id: str) -> dict[str, Any]:
        database = self.project_path(project_id) / "literature.sqlite"
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise StorageError("任务不存在。")
        value = dict(row)
        value["scope"] = json.loads(value.pop("scope_json"))
        value["config"] = json.loads(value.pop("config_json"))
        return value

    def list_jobs(self, project_id: str) -> list[dict[str, Any]]:
        database = self.project_path(project_id) / "literature.sqlite"
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["scope"] = json.loads(value.pop("scope_json"))
            value["config"] = json.loads(value.pop("config_json"))
            result.append(value)
        return result

    def usage_summary(self, project_id: str) -> dict[str, Any]:
        database = self.project_path(project_id) / "literature.sqlite"
        with self._connect(database) as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                COALESCE(SUM(cached_tokens),0), COALESCE(SUM(reasoning_tokens),0),
                COALESCE(SUM(estimated_cost),0) FROM usage_records"""
            ).fetchone()
        return {"input_tokens": row[0], "output_tokens": row[1], "cached_tokens": row[2], "reasoning_tokens": row[3], "estimated_cost": row[4]}

    def usage_records(self, project_id: str) -> list[dict[str, Any]]:
        database = self.project_path(project_id) / "literature.sqlite"
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM usage_records ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _init_database(path: Path) -> None:
        with ProjectStore._connect(path) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS structure_patches (
                  patch_id TEXT PRIMARY KEY, block_id TEXT NOT NULL, operation TEXT NOT NULL,
                  before_json TEXT NOT NULL, after_json TEXT NOT NULL, confidence REAL NOT NULL,
                  source TEXT NOT NULL, reason TEXT NOT NULL, automatic INTEGER NOT NULL,
                  reverted_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                  job_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
                  scope_json TEXT NOT NULL, status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
                  config_json TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS usage_records (
                  usage_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL,
                  provider TEXT NOT NULL, model TEXT NOT NULL, scope TEXT NOT NULL,
                  input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                  cached_tokens INTEGER NOT NULL, reasoning_tokens INTEGER NOT NULL,
                  estimated_cost REAL, currency TEXT NOT NULL, request_id TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_project ON usage_records(project_id);
                """
            )
