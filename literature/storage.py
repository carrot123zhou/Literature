from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
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
    return Path(configured).expanduser() if configured else Path.home() / "CdoexProject" / "Literature" / "library"


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value).strip(" .-")
    return value[:120] or "Untitled"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_text(path: Path, text: str) -> None:
    """原子写入文本：临时文件 + ``os.replace``，读者永远看不到半文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(text)
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def atomic_json(path: Path, value: Any) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class ProjectStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_library_root().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # 每个项目数据库只跑一次幂等迁移，避免旧项目在新增列后写任务时失败。
        self._migrated_databases: set[str] = set()
        # 同一项目+语言的译文文件只能有一个写入者：翻译作业、人工编辑、术语表冻结
        # 共用这把锁，避免整层快照互相覆盖（丢失更新）。
        self._translation_locks: dict[str, threading.RLock] = {}
        self._translation_locks_guard = threading.Lock()
        # document.json 的读改写只能有一个写入者：追加解析合并、结构补丁/撤销、
        # 语言/层级修改共用这把锁，避免并发覆盖（丢失更新）。
        self._document_locks: dict[str, threading.RLock] = {}
        self._document_locks_guard = threading.Lock()
        # raw 封存是“复制→写 manifest→移动目录”的事务，同一项目的封存必须串行。
        self._raw_locks: dict[str, threading.RLock] = {}
        self._raw_locks_guard = threading.Lock()
        # 待对账费用记录（超时/无 usage/写账失败）的追写锁。
        self._pending_lock = threading.Lock()
        # project_id → 项目目录的可重建索引：`project_path` 原本每次都要扫描全部
        # project.json，在大库/逐项目查询时形成二次增长。索引可失效重建，不是事实来源。
        self._project_index: dict[str, Path] | None = None
        self._project_index_guard = threading.Lock()

    def _build_project_index(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        for manifest in self.root.glob("*/project.json"):
            try:
                project_id = str(json.loads(manifest.read_text(encoding="utf-8")).get("project_id") or "")
            except (OSError, ValueError):
                continue
            if project_id:
                index[project_id] = manifest.parent
        return index

    def _remember_project(self, project_id: str, project: Path) -> None:
        with self._project_index_guard:
            if self._project_index is not None and project_id:
                self._project_index[project_id] = project

    def _forget_project(self, project_id: str) -> None:
        with self._project_index_guard:
            if self._project_index is not None:
                self._project_index.pop(project_id, None)

    def translation_lock(self, project_id: str, locale: str) -> threading.RLock:
        """返回某项目+语言的译文写锁；调用方在读改写整层前后持有它。"""
        key = f"{project_id}:{locale}"
        with self._translation_locks_guard:
            lock = self._translation_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._translation_locks[key] = lock
            return lock

    def document_lock(self, project_id: str) -> threading.RLock:
        """返回某项目的 document 写锁；读改写 document 的调用方必须持有它。"""
        with self._document_locks_guard:
            lock = self._document_locks.get(project_id)
            if lock is None:
                lock = threading.RLock()
                self._document_locks[project_id] = lock
            return lock

    def raw_lock(self, project_id: str) -> threading.RLock:
        """返回某项目的 raw 封存锁，保证封存事务与 manifest 写入串行。"""
        with self._raw_locks_guard:
            lock = self._raw_locks.get(project_id)
            if lock is None:
                lock = threading.RLock()
                self._raw_locks[project_id] = lock
            return lock

    def assert_accessible(self) -> None:
        """Fail fast when macOS lets us address the library but not enumerate it.

        A process launched from a restricted development host can sometimes
        create a file at a known Documents path while directory enumeration is
        filtered.  Project lookup is ID-based and therefore depends on a
        trustworthy directory listing; continuing in that state can overwrite
        an existing same-name project and then report that its new ID is
        missing.
        """
        probe = self.root / f".literature-access-{uuid.uuid4().hex}.tmp"
        try:
            with probe.open("x", encoding="utf-8") as handle:
                handle.write("Literature library access probe\n")
            if not any(item.name == probe.name for item in self.root.iterdir()):
                raise OSError("directory enumeration did not return the access probe")
            if not probe.is_file() or not probe.read_text(encoding="utf-8"):
                raise OSError("access probe could not be read back")
        except OSError as error:
            raise StorageError(
                f"Literature 无法完整读写项目库 {self.root}。请完全退出 Literature，"
                "确认项目仓库仍在原位置且当前用户拥有读写权限后再重新打开。"
            ) from error
        finally:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass

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
        index: dict[str, Path] = {}
        for manifest in self.root.glob("*/project.json"):
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
                projects.append(self._describe_project(manifest.parent, value))
                if value.get("project_id"):
                    index[str(value["project_id"])] = manifest.parent
            except (OSError, ValueError):
                continue
        with self._project_index_guard:
            # 列表遍历已经把全部项目读了一遍，顺手刷新索引，避免后续 project_path 再次全扫。
            self._project_index = index
        return sorted(projects, key=lambda item: item.get("updated_at", ""), reverse=True)

    @staticmethod
    def _describe_project(project: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        value = dict(manifest)
        value["path"] = str(project)
        value["has_document"] = (project / "document" / "document.json").is_file()
        value["asset_count"] = sum(1 for item in (project / "assets").glob("*") if item.is_file())
        value["translation"] = ProjectStore._translation_summary(project, str(manifest.get("target_language") or "zh-CN"))
        return value

    @staticmethod
    def _translation_summary(project: Path, locale: str) -> dict[str, Any] | None:
        """读取持久化的轻量摘要，避免项目列表为每份文档重算全部译文状态。"""
        path = project / "translations" / locale / "summary.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def project_path(self, project_id: str) -> Path:
        """返回项目目录，优先用可重建的 project_id→path 索引，避免每次全库扫描。"""
        with self._project_index_guard:
            if self._project_index is None:
                self._project_index = self._build_project_index()
            cached = self._project_index.get(project_id)
        if cached is not None and (cached / "project.json").is_file():
            return cached
        # 缓存未命中：可能是外部新建/移动了项目目录。重建一次后再决定，仍无则报不存在。
        with self._project_index_guard:
            self._project_index = self._build_project_index()
            found = self._project_index.get(project_id)
        if found is not None:
            return found
        raise StorageError(f"项目不存在：{project_id}")

    def project_database(self, project_id: str) -> Path:
        """返回项目 SQLite 路径，并确保它已建表/补齐后续版本新增的列。

        `create_project` 只在建库时迁移一次；既有项目在升级后首次读写任务前
        必须补跑迁移，否则 `update_job(message=...)` 这类新增列会直接报
        `no such column`，让后台作业永远停在 queued。
        """
        database = self.project_path(project_id) / "literature.sqlite"
        key = str(database)
        if key not in self._migrated_databases:
            self._init_database(database)
            self._migrated_databases.add(key)
        return database

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
        for child in ("source", "ocr/raw/mineru", "document", "translations", "assets"):
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
        self._remember_project(project_id, destination)
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

    def describe_project(self, project_id: str) -> dict[str, Any]:
        project = self.project_path(project_id)
        return self._describe_project(project, self.load_manifest(project_id))

    def rename_project(self, project_id: str, title: str) -> dict[str, Any]:
        title = str(title).strip()
        if not title:
            raise StorageError("项目名称不能为空。")
        if len(title) > 240:
            raise StorageError("项目名称不能超过 240 个字符。")

        project = self.project_path(project_id)
        destination = self.root / safe_name(title)
        if destination.exists():
            try:
                is_current = destination.samefile(project)
            except OSError:
                is_current = destination.resolve() == project.resolve()
        else:
            is_current = False
        if not is_current:
            base = destination
            index = 2
            while destination.exists():
                destination = self.root / f"{base.name} ({index})"
                index += 1
            project.rename(destination)
            project = destination

        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["title"] = title
        value["updated_at"] = utc_now()
        atomic_json(project / "project.json", value)

        document_path = project / "document" / "document.json"
        if document_path.is_file():
            document = Document.from_dict(json.loads(document_path.read_text(encoding="utf-8")))
            document.title = title
            self.save_document(document)
        self._remember_project(project_id, project)
        return self._describe_project(project, value)

    def delete_project(self, project_id: str) -> dict[str, Any]:
        """Move a project out of the active library into a recoverable local trash."""
        project = self.project_path(project_id)
        value = json.loads((project / "project.json").read_text(encoding="utf-8"))
        value["deleted_at"] = utc_now()
        atomic_json(project / "project.json", value)
        trash = self.root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        destination = trash / f"{uuid.uuid4().hex}-{project.name}"
        project.rename(destination)
        self._forget_project(project_id)
        return {
            "project_id": project_id,
            "title": value.get("title", project.name),
            "deleted_at": value["deleted_at"],
            "recoverable": True,
            "trash_path": str(destination),
        }

    def save_document(self, document: Document) -> None:
        project = self.project_path(document.project_id)
        document.updated_at = utc_now()
        path = project / "document" / "document.json"
        atomic_json(path, document.to_dict())
        # 顺手把“文档时间戳”的快速读取缓存刷新掉：缓存以真实文件签名为键，
        # 就算这一步失败，下次读取也会因为签名不匹配而重新读文件（不会假命中）。
        try:
            info = path.stat()
            self._remember_document_state(document.project_id, document.updated_at, f"{info.st_size}:{info.st_mtime_ns}")
        except OSError:
            pass

    DOCUMENT_STATE_KEY = "document"

    @staticmethod
    def _read_state(connection: sqlite3.Connection, key: str) -> dict[str, Any]:
        try:
            row = connection.execute("SELECT value_json FROM project_state WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return {}
        if row is None:
            return {}
        try:
            value = json.loads(row[0] or "{}")
        except (ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write_state(connection: sqlite3.Connection, key: str, value: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO project_state (key, value_json) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def _remember_document_state(self, project_id: str, updated_at: str, signature: str) -> None:
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            self._write_state(connection, self.DOCUMENT_STATE_KEY,
                              {"signature": signature, "updated_at": updated_at})

    def document_updated_at(self, project_id: str) -> str:
        """只取规范文档的 ``updated_at``，不解码整份 ``document.json``。

        ``document.json`` 单份可达 8 MB，而项目列表/状态接口只需要一个时间戳字符串：
        实测每 3 秒轮询时为整库多花约 130 ms 在 JSON 解码上。这里以
        ``(size, mtime_ns)`` 作为缓存键存在项目库的 ``project_state``：
        命中直接返回；未命中（新项目、文档刚改、旧库首次升级）才读一次文件并回填。

        缓存键取自真实文件，所以不存在“缓存说未变、文件其实已变”的假有效；
        读不到文件时返回空串（与“尚无规范文档”语义一致）。
        """
        path = self.project_path(project_id) / "document" / "document.json"
        try:
            info = path.stat()
        except OSError:
            return ""
        signature = f"{info.st_size}:{info.st_mtime_ns}"
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            cached = self._read_state(connection, self.DOCUMENT_STATE_KEY)
        if cached.get("signature") == signature and isinstance(cached.get("updated_at"), str):
            return str(cached["updated_at"])
        try:
            document = self.load_document(project_id)
        except StorageError:
            return ""
        self._remember_document_state(project_id, document.updated_at, signature)
        return document.updated_at

    def load_structure_baseline(self, project_id: str) -> dict[str, Any]:
        """已确认的目录基准随项目保存，迁移客户端或清空 WebView 偏好后仍可复用。"""
        path = self.project_path(project_id) / "document" / "toc_baseline.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"toc": [], "source": "", "updated_at": ""}
        toc = value.get("toc") if isinstance(value, dict) else None
        entries: list[dict[str, Any]] = []
        for item in toc if isinstance(toc, list) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            try:
                level = max(1, min(6, int(item.get("level", 1))))
            except (TypeError, ValueError):
                level = 1
            entries.append({"title": title[:300], "level": level})
        return {
            "toc": entries,
            "source": str(value.get("source", "")) if isinstance(value, dict) else "",
            "updated_at": str(value.get("updated_at", "")) if isinstance(value, dict) else "",
        }

    def save_structure_baseline(self, project_id: str, toc: Any, source: str = "") -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for item in toc if isinstance(toc, list) else []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            if not title:
                continue
            try:
                level = max(1, min(6, int(item.get("level", 1))))
            except (TypeError, ValueError):
                level = 1
            entries.append({"title": title[:300], "level": level})
        value = {"toc": entries, "source": source, "updated_at": utc_now()}
        atomic_json(self.project_path(project_id) / "document" / "toc_baseline.json", value)
        return value

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
            # 空译文层用稳定的 updated_at，避免同一状态每次调用产生不同 revision。
            return TranslationLayer(project_id=project_id, locale=locale, updated_at="")
        return TranslationLayer.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_translation(self, layer: TranslationLayer) -> None:
        layer.updated_at = utc_now()
        atomic_json(self.translation_path(layer.project_id, layer.locale), layer.to_dict())

    def save_translation_summary(self, project_id: str, locale: str, summary: dict[str, Any]) -> None:
        """写入项目列表可廉价读取的译文统计摘要。"""
        path = self.project_path(project_id) / "translations" / locale / "summary.json"
        atomic_json(path, {**summary, "locale": locale, "updated_at": utc_now()})

    # --- 术语表版本 ---------------------------------------------------------

    def glossary_versions_path(self, project_id: str, locale: str) -> Path:
        return self.project_path(project_id) / "translations" / locale / "glossary_versions.json"

    def list_glossary_versions(self, project_id: str, locale: str) -> list[dict[str, Any]]:
        path = self.glossary_versions_path(project_id, locale)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        versions = value.get("versions") if isinstance(value, dict) else value
        return [item for item in (versions or []) if isinstance(item, dict)]

    def save_glossary_version(self, project_id: str, locale: str, entries: dict[str, str], *, source: str = "freeze") -> dict[str, Any]:
        """冻结或回滚时追加一个术语表版本；版本号递增，旧版本不可变。"""
        versions = self.list_glossary_versions(project_id, locale)
        version = max([int(item.get("version", 0)) for item in versions] or [0]) + 1
        record = {
            "version": version,
            "entries": {str(key): str(value) for key, value in entries.items()},
            "source": source,
            "created_at": utc_now(),
        }
        versions.append(record)
        atomic_json(self.glossary_versions_path(project_id, locale), {"versions": versions})
        return record

    def load_glossary_version(self, project_id: str, locale: str, version: int) -> dict[str, Any]:
        for item in self.list_glossary_versions(project_id, locale):
            if int(item.get("version", 0)) == int(version):
                return item
        raise StorageError(f"术语表版本不存在：{version}")

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

    def read_raw_manifest(self, project_id: str) -> dict[str, Any]:
        path = self.project_path(project_id) / "ocr" / "raw" / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"slices": []}
        return value if isinstance(value, dict) else {"slices": []}

    @staticmethod
    def _next_slice_index(slices: list[dict[str, Any]]) -> int:
        highest = 0
        for item in slices:
            match = re.fullmatch(r"slice-(\d+)", str(item.get("slice") or ""))
            if match:
                highest = max(highest, int(match.group(1)))
        return highest + 1

    @staticmethod
    def _slice_number(item: dict[str, Any]) -> int:
        match = re.fullmatch(r"slice-(\d+)", str(item.get("slice") or ""))
        return int(match.group(1)) if match else 10 ** 9

    def _slice_record(self, destination: Path, source: str, slice_name: str) -> dict[str, Any]:
        files = []
        for file in sorted(item for item in destination.rglob("*") if item.is_file()):
            final_relative = Path("ocr/raw/mineru") / slice_name / file.relative_to(destination)
            files.append({"path": str(final_relative), "size": file.stat().st_size, "sha256": sha256_file(file)})
            file.chmod(0o444)
        return {"slice": slice_name, "source": source, "files": files}

    def _reconcile_raw_slices(self, raw_root: Path, slices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把 manifest 与磁盘对齐：丢掉目录缺失的记录，收养崩溃遗留的孤儿切片目录。

        封存事务“先写 manifest、再移动目录”，因此正常崩溃不会留下孤儿；
        兼容旧实现（先移动后写 manifest）遗留下的完整孤儿目录。
        """
        kept: list[dict[str, Any]] = []
        names: set[str] = set()
        for item in slices:
            name = str(item.get("slice") or "")
            if name and name not in names and (raw_root / name).is_dir():
                kept.append(item)
                names.add(name)
        if raw_root.is_dir():
            for child in sorted(raw_root.iterdir()):
                if not child.is_dir() or child.name.startswith(".") or child.name in names:
                    continue
                if not re.fullmatch(r"slice-\d+", child.name):
                    continue
                kept.append(self._slice_record(child, "", child.name))
                names.add(child.name)
        kept.sort(key=self._slice_number)
        return kept

    def append_raw_mineru(self, project_id: str, sources: Iterable[str | Path]) -> list[dict[str, Any]]:
        """继续解析的追加封存；与幂等封存是同一事务（见 seal_raw_mineru）。"""
        return self.seal_raw_mineru(project_id, sources)

    def seal_raw_mineru(self, project_id: str, sources: Iterable[str | Path]) -> list[dict[str, Any]]:
        """幂等、可恢复的 raw 封存事务。

        事务顺序：复制到暂存并算哈希 → **先写 manifest** → 再移动目录到 raw。
        - 崩溃在写 manifest 前：无任何副作用，重试重新封存。
        - 崩溃在写 manifest 后、移动前：manifest 有记录但目录缺失，重试时
          ``_reconcile_raw_slices`` 丢掉缺失记录并重新封存（源文件仍在）。
        - 同一 ``source`` 重复出现只封存一次；已存在的切片目录不覆盖。
        返回列表与传入 sources 顺序一致，每项含 ``destination`` 与 ``reused``。
        """
        resolved = [Path(raw).expanduser().resolve() for raw in sources]
        for source in resolved:
            if not source.is_dir():
                raise StorageError(f"MinerU 结果目录不存在：{source}")
        project = self.project_path(project_id)
        raw_root = project / "ocr" / "raw" / "mineru"
        manifest_path = project / "ocr" / "raw" / "manifest.json"
        with self.raw_lock(project_id):
            raw_root.mkdir(parents=True, exist_ok=True)
            existing = self.read_raw_manifest(project_id)
            original_slices = [item for item in existing.get("slices", []) if isinstance(item, dict)]
            existing_slices = self._reconcile_raw_slices(raw_root, original_slices)
            by_source = {str(item.get("source")): item for item in existing_slices if item.get("source")}
            by_destination = {str(raw_root / str(item.get("slice"))): item for item in existing_slices}
            next_index = self._next_slice_index(existing_slices)
            staging = raw_root.parent / f".mineru-seal-{uuid.uuid4().hex}"
            results: list[dict[str, Any]] = []
            appended: list[dict[str, Any]] = []
            try:
                staging.mkdir()
                for source in resolved:
                    key = str(source)
                    reuse = by_source.get(key) or by_destination.get(key)
                    if reuse is not None and (raw_root / str(reuse["slice"])).is_dir():
                        results.append({**reuse, "destination": str(raw_root / str(reuse["slice"])), "reused": True})
                        continue
                    if any(item["source"] == key for item in appended):
                        record = next(item for item in appended if item["source"] == key)
                        results.append({**record, "destination": str(raw_root / record["slice"]), "reused": True})
                        continue
                    slice_name = f"slice-{next_index + len(appended):03d}"
                    draft = staging / slice_name
                    shutil.copytree(source, draft)
                    record = self._slice_record(draft, key, slice_name)
                    appended.append(record)
                    results.append({**record, "destination": str(raw_root / slice_name), "reused": False})
                # 只要 reconcile 改变或新增了切片，就必须回写 manifest；否则“manifest 有记录
                # 但目录缺失”会永久卡住 verify_raw，即使本次没有任何新复制。
                original_names = [str(item.get("slice")) for item in original_slices]
                reconciled_names = [str(item.get("slice")) for item in existing_slices]
                if appended or original_names != reconciled_names:
                    atomic_json(manifest_path, {
                        "sealed_at": existing.get("sealed_at") or utc_now(),
                        "appended_at": utc_now(),
                        "slices": existing_slices + appended,
                    })
                if appended:
                    for record in appended:
                        destination = raw_root / record["slice"]
                        draft = staging / record["slice"]
                        if destination.is_dir():
                            shutil.rmtree(draft, ignore_errors=True)
                        else:
                            shutil.move(str(draft), str(destination))
            finally:
                shutil.rmtree(staging, ignore_errors=True)
            return results

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
        self.record_patches(project_id, [patch])

    def record_patches(self, project_id: str, patches: list[StructurePatch]) -> None:
        """在一个事务里写入多条修订，供批量结构修改保持原子性。"""
        if not patches:
            return
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            connection.executemany(
                """INSERT INTO structure_patches
                (patch_id, block_id, operation, before_json, after_json, confidence, source, reason, automatic, reverted_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [(patch.patch_id, patch.block_id, patch.operation, json.dumps(patch.before), json.dumps(patch.after),
                  patch.confidence, patch.source, patch.reason, int(patch.automatic), patch.reverted_at, patch.created_at)
                 for patch in patches],
            )

    def list_patches(self, project_id: str) -> list[dict[str, Any]]:
        database = self.project_database(project_id)
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
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            connection.execute("UPDATE structure_patches SET reverted_at=? WHERE patch_id=?", (reverted_at, patch_id))

    def record_usage(self, record: UsageRecord) -> None:
        database = self.project_database(record.project_id)
        with self._connect(database) as connection:
            connection.execute(
                """INSERT INTO usage_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record.usage_id, record.project_id, record.job_id, record.provider, record.model, record.scope,
                 record.input_tokens, record.output_tokens, record.cached_tokens, record.reasoning_tokens,
                 record.estimated_cost, record.currency, record.request_id, record.created_at),
            )

    def create_job(self, project_id: str, kind: str, scope: dict[str, Any], config: dict[str, Any], *, job_id: str | None = None) -> str:
        job_id = job_id or uuid.uuid4().hex
        database = self.project_database(project_id)
        timestamp = utc_now()
        with self._connect(database) as connection:
            connection.execute(
                """INSERT INTO jobs
                (job_id, project_id, kind, scope_json, status, progress, config_json, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', 0, ?, '', ?, ?)""",
                (job_id, project_id, kind, json.dumps(scope, ensure_ascii=False), json.dumps(config, ensure_ascii=False), timestamp, timestamp),
            )
        return job_id

    def update_job(self, project_id: str, job_id: str, *, status: str | None = None, progress: float | None = None, error: str | None = None, message: str | None = None, result: dict[str, Any] | None = None, activity: list[dict[str, Any]] | None = None) -> None:
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
        if message is not None:
            assignments.append("message=?")
            values.append(message)
        if result is not None:
            assignments.append("result_json=?")
            values.append(json.dumps(result, ensure_ascii=False))
        if activity is not None:
            assignments.append("activity_json=?")
            values.append(json.dumps(activity, ensure_ascii=False))
        values.append(job_id)
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            connection.execute(f"UPDATE jobs SET {', '.join(assignments)} WHERE job_id=?", values)

    def get_job(self, project_id: str, job_id: str) -> dict[str, Any]:
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise StorageError("任务不存在。")
        return self._decode_job(row)

    def list_jobs(self, project_id: str) -> list[dict[str, Any]]:
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._decode_job(row) for row in rows]

    def list_job_summaries(self, project_id: str) -> list[dict[str, Any]]:
        """轻量作业列表：只投影状态字段 + 小 scope（不读取/JSON 解码 result/activity 审计）。"""
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT job_id, project_id, kind, status, progress, message, error, scope_json, created_at, updated_at "
                "FROM jobs ORDER BY created_at DESC"
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            try:
                value["scope"] = json.loads(value.pop("scope_json", "{}") or "{}")
            except (ValueError, TypeError):
                value["scope"] = {}
            values.append(value)
        return values

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["scope"] = json.loads(value.pop("scope_json"))
        value["config"] = json.loads(value.pop("config_json"))
        value["result"] = json.loads(value.pop("result_json", "{}") or "{}")
        value["activity"] = json.loads(value.pop("activity_json", "[]") or "[]")
        value.setdefault("message", "")
        return value

    def usage_summary(self, project_id: str) -> dict[str, Any]:
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                COALESCE(SUM(cached_tokens),0), COALESCE(SUM(reasoning_tokens),0),
                COALESCE(SUM(estimated_cost),0),
                COALESCE(SUM(CASE WHEN estimated_cost IS NULL THEN 1 ELSE 0 END),0)
                FROM usage_records"""
            ).fetchone()
        return {
            "input_tokens": row[0], "output_tokens": row[1], "cached_tokens": row[2],
            "reasoning_tokens": row[3], "estimated_cost": row[4],
            # 已发生但价格/usage 未知的请求数：estimated_cost 不能当成“确定零成本”。
            "unknown_cost_records": row[5],
            # 额外待对账记录（超时/无 usage/写账失败），重启后仍可统计。
            "pending_cost_records": self.pending_cost_records(project_id),
        }

    def pending_cost_path(self, project_id: str) -> Path:
        return self.project_path(project_id) / "usage_pending.jsonl"

    def record_pending_cost(
        self, project_id: str, *, job_id: str = "", reason: str = "",
        provider: str = "", model: str = "", request_id: str = "",
        input_tokens: int = 0, output_tokens: int = 0, estimated_cost: float | None = None,
    ) -> None:
        """持久化“本次请求费用未知”的待对账证据（重启后仍明确 unknown）。

        包含可对账身份（provider/model/request_id）与已知 usage/cost，便于事后核对。
        """
        path = self.pending_cost_path(project_id)
        record = {
            "job_id": job_id, "reason": reason, "provider": provider, "model": model,
            "request_id": request_id, "input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
            "estimated_cost": estimated_cost, "created_at": utc_now(),
        }
        try:
            with self._pending_lock:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def pending_cost_records(self, project_id: str) -> int:
        try:
            with self.pending_cost_path(project_id).open(encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except OSError:
            return 0

    def usage_records(self, project_id: str) -> list[dict[str, Any]]:
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM usage_records ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _suggestion_id(job_id: str, value: dict[str, Any]) -> str:
        seed = json.dumps([job_id, value.get("block_id", ""), value.get("operation", ""), value.get("after")], ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _decode_suggestion(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["before"] = json.loads(value.pop("before_json"))
        value["after"] = json.loads(value.pop("after_json"))
        metadata = json.loads(value.pop("metadata_json", "{}"))
        value.update({key: metadata[key] for key in ("parent_id", "parent_title", "evidence_role") if key in metadata})
        return value

    def save_structure_suggestions(self, project_id: str, job_id: str, suggestions: list[dict[str, Any]], document_updated_at: str = "") -> None:
        """持久化一次分析产出的建议；新的 pending 取代旧项目内 pending，已处理结论不复活。"""
        database = self.project_database(project_id)
        now = utc_now()
        rows = []
        for value in suggestions:
            suggestion_id = self._suggestion_id(job_id, value)
            value["suggestion_id"] = suggestion_id
            value["job_id"] = job_id
            rows.append((
                suggestion_id, project_id, job_id, str(value.get("block_id", "")), str(value.get("operation", "")),
                json.dumps(value.get("before"), ensure_ascii=False), json.dumps(value.get("after"), ensure_ascii=False),
                float(value.get("confidence", 0) or 0), str(value.get("reason", "")),
                str(value.get("document_updated_at") or document_updated_at), "pending", now,
                json.dumps({key: value[key] for key in ("parent_id", "parent_title", "evidence_role") if key in value}, ensure_ascii=False),
            ))
        with self._connect(database) as connection:
            connection.execute("DELETE FROM structure_suggestions WHERE project_id=? AND decision='pending'", (project_id,))
            if rows:
                # INSERT OR IGNORE 保留已采用/已忽略的旧结论，不让同一建议复活。
                connection.executemany(
                    """INSERT OR IGNORE INTO structure_suggestions
                    (suggestion_id, project_id, job_id, block_id, operation, before_json, after_json,
                     confidence, reason, document_updated_at, decision, created_at, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )

    def list_structure_suggestions(self, project_id: str, decision: str | None = None) -> list[dict[str, Any]]:
        database = self.project_database(project_id)
        query = "SELECT * FROM structure_suggestions WHERE project_id=?"
        parameters: list[Any] = [project_id]
        if decision:
            query += " AND decision=?"
            parameters.append(decision)
        query += " ORDER BY created_at, block_id"
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(query, parameters).fetchall()
        return [self._decode_suggestion(row) for row in rows]

    def structure_review_summary(self, project_id: str) -> dict[str, Any]:
        """项目列表用的轻量校对摘要。

        `done` 必须有**显式审查确认**且文档结构版本未变；单个补丁/曾 dismiss 只能说“部分”。
        - `todo`：提取完成但无建议/补丁/确认；
        - `confirm`：有 N 条待人工确认的建议；
        - `partial`：有已处理建议或补丁，但无对应当前文档版本的完整确认；
        - `done`：已确认全量审查且文档此后未改。
        """
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            pending = int(connection.execute(
                "SELECT COUNT(*) FROM structure_suggestions WHERE project_id=? AND decision='pending'",
                (project_id,),
            ).fetchone()[0])
            decided = int(connection.execute(
                "SELECT COUNT(*) FROM structure_suggestions WHERE project_id=? AND decision IN ('applied','dismissed')",
                (project_id,),
            ).fetchone()[0])
            patches = int(connection.execute("SELECT COUNT(*) FROM structure_patches").fetchone()[0])
        confirmation = self.structure_review_confirmation(project_id)
        # 只取时间戳，不解码整份 document.json（本函数在项目列表轮询里被逐项目调用）。
        document_updated_at = self.document_updated_at(project_id)
        confirmation_valid = bool(confirmation) and confirmation.get("document_updated_at") == document_updated_at
        if pending:
            state = "confirm"
        elif confirmation_valid and confirmation.get("scope") == "full":
            state = "done"
        elif decided or patches or confirmation:
            state = "partial"
        else:
            state = "todo"
        return {
            "state": state, "pending": pending,
            "confirmed_at": confirmation.get("confirmed_at", "") if confirmation_valid else "",
            "confirmed_scope": confirmation.get("scope", "") if confirmation_valid else "",
        }

    def structure_review_path(self, project_id: str) -> Path:
        return self.project_path(project_id) / "structure" / "review.json"

    def confirm_structure_review(self, project_id: str, *, scope: str = "full") -> dict[str, Any]:
        """记录显式审查确认：绑定当前文档结构版本、范围与时间；文档改后自动失效。"""
        if scope not in {"full", "partial"}:
            raise StorageError("审查确认范围必须是 full 或 partial。")
        document = self.load_document(project_id)
        record = {"scope": scope, "document_updated_at": document.updated_at, "confirmed_at": utc_now()}
        atomic_json(self.structure_review_path(project_id), record)
        return record

    def structure_review_confirmation(self, project_id: str) -> dict[str, Any] | None:
        path = self.structure_review_path(project_id)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def get_structure_suggestion(self, project_id: str, suggestion_id: str) -> dict[str, Any]:
        database = self.project_database(project_id)
        with self._connect(database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM structure_suggestions WHERE project_id=? AND suggestion_id=?", (project_id, suggestion_id),
            ).fetchone()
        if row is None:
            raise StorageError("结构建议不存在。")
        return self._decode_suggestion(row)

    def decide_structure_suggestion(self, project_id: str, suggestion_id: str, decision: str) -> dict[str, Any]:
        self.decide_structure_suggestions(project_id, [suggestion_id], decision)
        return self.get_structure_suggestion(project_id, suggestion_id)

    def decide_structure_suggestions(self, project_id: str, suggestion_ids: list[str], decision: str) -> None:
        if not suggestion_ids:
            return
        database = self.project_database(project_id)
        now = utc_now()
        with self._connect(database) as connection:
            connection.executemany(
                "UPDATE structure_suggestions SET decision=?, decided_at=? WHERE project_id=? AND suggestion_id=?",
                [(decision, now, project_id, suggestion_id) for suggestion_id in suggestion_ids],
            )

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
                CREATE TABLE IF NOT EXISTS structure_suggestions (
                  suggestion_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL,
                  block_id TEXT NOT NULL, operation TEXT NOT NULL, before_json TEXT NOT NULL,
                  after_json TEXT NOT NULL, confidence REAL NOT NULL, reason TEXT NOT NULL,
                  document_updated_at TEXT NOT NULL, decision TEXT NOT NULL DEFAULT 'pending',
                  created_at TEXT NOT NULL, decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_structure_suggestions_project ON structure_suggestions(project_id, decision);
                CREATE TABLE IF NOT EXISTS project_state (
                  key TEXT PRIMARY KEY, value_json TEXT NOT NULL
                );
                """
            )
        ProjectStore._migrate_database(path)

    @staticmethod
    def _migrate_database(path: Path) -> None:
        """幂等增量迁移：为既有数据库补齐后续版本新增的列。"""
        with ProjectStore._connect(path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
            additions = {
                "message": "ALTER TABLE jobs ADD COLUMN message TEXT NOT NULL DEFAULT ''",
                "result_json": "ALTER TABLE jobs ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'",
                "activity_json": "ALTER TABLE jobs ADD COLUMN activity_json TEXT NOT NULL DEFAULT '[]'",
            }
            for name, statement in additions.items():
                if name not in columns:
                    connection.execute(statement)
            suggestion_columns = {row[1] for row in connection.execute("PRAGMA table_info(structure_suggestions)").fetchall()}
            if "metadata_json" not in suggestion_columns:
                connection.execute("ALTER TABLE structure_suggestions ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
