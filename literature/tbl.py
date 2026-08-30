from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .exporters import export_docx, export_epub
from .storage import ProjectStore, StorageError, atomic_json


class TBLBridge:
    """Build a handoff from the canonical graph; TBL never reads or writes it directly."""

    def __init__(self, store: ProjectStore, *, command: str | None = None, url: str | None = None) -> None:
        self.store = store
        runtime = Path(os.environ.get("LITERATURE_RUNTIME", "")).expanduser() if os.environ.get("LITERATURE_RUNTIME") else Path.home() / "Library" / "Application Support" / "Literature"
        self.config_path = runtime / "tbl.json"
        saved: dict[str, str] = {}
        if self.config_path.is_file():
            try:
                saved = json.loads(self.config_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = {}
        self.command = (command if command is not None else os.environ.get("LITERATURE_TBL_COMMAND", saved.get("command", ""))).strip()
        self.url = (url if url is not None else os.environ.get("LITERATURE_TBL_URL", saved.get("url", "http://127.0.0.1:5001"))).strip()
        self.process: subprocess.Popen[Any] | None = None

    def configure(self, *, command: str, url: str) -> dict[str, Any]:
        self.command = command.strip()
        self.url = url.strip() or "http://127.0.0.1:5001"
        atomic_json(self.config_path, {"command": self.command, "url": self.url})
        return self.config()

    def config(self) -> dict[str, Any]:
        return {"command": self.command, "url": self.url, "configured": bool(self.command)}

    def prepare(self, project_id: str, *, format_name: str = "epub", mode: str = "source") -> dict[str, Any]:
        if format_name == "epub":
            input_file = export_epub(self.store, project_id, mode=mode)  # type: ignore[arg-type]
        elif format_name == "docx":
            input_file = export_docx(self.store, project_id, mode=mode)  # type: ignore[arg-type]
        else:
            raise StorageError("TBL 接力只支持 EPUB 或 DOCX。")
        project = self.store.project_path(project_id)
        manifest = {
            "project_id": project_id, "input": str(input_file), "format": format_name,
            "mode": mode, "tbl_url": self.url,
            "data_basis": "document/document.json + assets",
            "note": "该文件由校准后的规范文档图谱派生，绝非原 PDF 直转；TBL 输出不会回写 Literature block 图谱。",
        }
        atomic_json(project / "exports" / "tbl-handoff.json", manifest)
        return manifest

    def start(self) -> dict[str, Any]:
        if self.process and self.process.poll() is None:
            return self.status()
        if not self.command:
            return {"state": "not_configured", "url": self.url, "message": "请在设置中配置 TBL 启动命令，或直接使用已有 TBL。"}
        command = shlex.split(self.command)
        if not command:
            raise StorageError("TBL 启动命令为空。")
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return self.status()

    def status(self) -> dict[str, Any]:
        if self.process is None:
            return {"state": "stopped", "url": self.url, "configured": bool(self.command)}
        code = self.process.poll()
        return {"state": "running" if code is None else "stopped", "pid": self.process.pid, "exit_code": code, "url": self.url, "configured": bool(self.command)}
