from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from .mineru_capabilities import MAX_FILE_BYTES, MAX_PDF_UPLOAD_BYTES, SUPPORTED_EXTENSIONS, source_kind
from .storage import StorageError, atomic_json, safe_name


class UploadStore:
    """Short-lived server-side staging for files selected in the WebUI."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, filename: str, stream: BinaryIO, *, content_type: str = "") -> dict[str, object]:
        original = Path(filename or "").name
        suffix = Path(original).suffix.casefold()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise StorageError(f"MinerU 不支持 {suffix or '无扩展名'} 文件。")
        upload_id = uuid.uuid4().hex
        directory = self.root / upload_id
        directory.mkdir()
        stored_name = f"{safe_name(Path(original).stem)}{suffix}"
        target = directory / stored_name
        size = 0
        maximum = MAX_PDF_UPLOAD_BYTES if suffix == ".pdf" else MAX_FILE_BYTES
        try:
            with target.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > maximum:
                        limit = "Literature 当前 2GB Web 上传" if suffix == ".pdf" else "MinerU 单文件 200MB"
                        raise StorageError(f"{original} 超过{limit}限制。")
                    output.write(chunk)
            if size == 0:
                raise StorageError(f"文件为空：{original}")
            metadata: dict[str, object] = {
                "upload_id": upload_id,
                "name": original,
                "stored_name": stored_name,
                "size": size,
                "extension": suffix.removeprefix("."),
                "source_kind": source_kind(target),
                "content_type": content_type,
            }
            atomic_json(directory / "upload.json", metadata)
            return metadata
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    def resolve(self, upload_id: str) -> tuple[Path, dict[str, object]]:
        if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
            raise StorageError("无效的上传文件标识。")
        directory = self.root / upload_id
        metadata_path = directory / "upload.json"
        if not metadata_path.is_file():
            raise StorageError("上传文件已失效，请重新选择。")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        path = directory / str(metadata["stored_name"])
        if not path.is_file() or path.parent != directory:
            raise StorageError("上传暂存文件缺失，请重新选择。")
        return path, metadata

    def discard(self, upload_id: str) -> None:
        if re.fullmatch(r"[0-9a-f]{32}", upload_id):
            shutil.rmtree(self.root / upload_id, ignore_errors=True)
