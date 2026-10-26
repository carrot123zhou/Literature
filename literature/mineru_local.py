"""本地 MinerU 引擎：客户端内下载、安装、卸载与本地解析后端。

设计对齐 ``docs/LOCAL_MINERU_BACKEND.md`` 的接入口径：

- 引擎完全独立于主 sidecar：uv 引导托管 Python 3.12 venv → 安装 pinned
  ``mineru==4.0.10`` → ``mineru-kit models download`` 下载标准档模型。
  主程序（PyInstaller 冻结 sidecar）不需要 Python/pip/uv 预装；uv 缺失时
  从 astral-sh/uv GitHub Release 下载固定版本二进制并做 sha256 校验。
- 安装位置：``<runtime_root>/mineru-local/``（默认
  ``~/Library/Application Support/Literature/mineru-local/``），不进仓库、
  不进文献库；卸载只删除该目录，不触碰 raw/canonical/译文。
- 安装状态持久化在 ``state.json``；``installed.txt`` 最后写入——没有标记的
  目录一律视为未完成安装。每个步骤幂等，取消/失败后可从断点续装。
- ``LocalMinerUBackend`` 实现既有 ``ParseBackend`` 协议（prepare/execute/
  status/result），切片逻辑复用工作流；执行时调用 ``mineru-kit parse``，
  取消为真取消（终止进程）。MinerU 4 kit 的 ZIP 输出（markdown.md、
  middle_json.json、structured_content.json、images/）**原样保留**，并
  **派生** ``content_list_v2.json``（官方 renderer）与 ``layout.json``
  （页尺寸/旋转，来自切片 PDF 本身），接入同一套 seal/normalize/merge，
  绝不绕过 canonical 与稳定 block/cell ID。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .mineru_workflow import (
    DEFAULT_MAX_EXTRACTED_BYTES, JobState, SliceState, WorkflowError, WorkflowJob,
    _is_result_directory, _utcnow,
)
from .parse_backends import LOCAL_BACKEND_ID, ParseBackendCapabilities, ParseResult

MINERU_VERSION = "4.0.10"
ENGINE_PYTHON = "3.12"
MODEL_TIER = "standard"
UV_VERSION = "0.13.5"
# astral-sh/uv GitHub Release 固定版本二进制的 sha256（2026-10-25 校验）。
UV_SHA256: dict[str, str] = {
    "aarch64-apple-darwin": "743f85035a1996ecb350659e01cd9faea9709829be09b1b4b03c73eaf3c9cd73",
    "x86_64-apple-darwin": "b0f52b0e3945b4107d89024ea38e625683011fbfc05a0d98f9fa2d6dcee28c66",
    "x86_64-unknown-linux-gnu": "f68d291c75783122dad58ab77def0e13f3233f358731db5d6d7e8715603b928f",
    "x86_64-pc-windows-msvc": "a38b8d1902b7cfbc76b423ba1bb456f623f42064cdc72628b6075b89afd1019a",
}
MARKER_NAME = "installed.txt"
STATE_NAME = "state.json"
PROCESS_JOURNAL = "procs.json"
ENGINE_STEPS = ("uv", "python", "packages", "models", "verify")
STEP_LABELS = {
    "uv": "下载安装工具 uv",
    "python": "下载托管 Python 运行时",
    "packages": "安装 MinerU 引擎依赖",
    "models": "下载 MinerU 标准档模型",
    "verify": "安装自检",
}

# 在引擎 venv 里执行的官方 Content List V2 派生脚本：
# middle_json.json（kit 原样输出）→ content_list_v2.json（派生适配文件）。
# 官方 renderer 输出的表格 HTML 放在 content.html；Literature 规范化层读取
# table_content，这里做只增不改的别名补充，原始键全部保留。
DERIVE_SCRIPT = r"""
import json, sys

from mineru.render import render_content_list_v2
from mineru.types import MiddleJson

middle_path, out_path = sys.argv[1], sys.argv[2]
with open(middle_path, encoding="utf-8") as stream:
    data = json.load(stream)
middle = MiddleJson.from_dict(data)
pages = render_content_list_v2(middle, asset_base_url="")
for page in pages:
    for item in page:
        content = item.get("content")
        if isinstance(content, dict) and "html" in content and "table_content" not in content:
            content["table_content"] = content["html"]
with open(out_path, "w", encoding="utf-8") as stream:
    json.dump(pages, stream, ensure_ascii=False)
"""


class EngineBusyError(RuntimeError):
    """引擎被占用（安装中或本地解析任务执行中），操作被拒绝。"""


def default_engine_root() -> Path:
    configured = os.environ.get("LITERATURE_RUNTIME", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / "Library" / "Application Support" / "Literature"
    return base / "mineru-local"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def platform_key() -> str:
    if sys.platform == "darwin":
        import platform
        return "aarch64-apple-darwin" if platform.machine() == "arm64" else "x86_64-apple-darwin"
    if sys.platform == "win32":
        return "x86_64-pc-windows-msvc"
    return "x86_64-unknown-linux-gnu"


@dataclass
class _RunningProcess:
    handle: subprocess.Popen[Any]
    command: tuple[str, ...]
    # install=安装步骤；parse=本地解析。安装取消只终止 install 进程，
    # 绝不误杀正在执行的解析进程（解析取消走工作流 stop 边界）。
    tag: str = "install"


class LocalMinerUEngine:
    """本地引擎管理器：安装/取消/修复/卸载、状态持久化与进程生命周期。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_engine_root().resolve()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._cancel_event = threading.Event()
        self._processes: dict[int, _RunningProcess] = {}
        self._active_jobs: set[str] = set()
        self._size_cache: tuple[float, int] | None = None
        self._reap_leftover_processes()

    # -- paths -----------------------------------------------------------
    @property
    def venv_dir(self) -> Path:
        return self.root / "venv"

    @property
    def models_dir(self) -> Path:
        return self.root / "mineru-home" / "models"

    @property
    def marker_file(self) -> Path:
        return self.root / MARKER_NAME

    @property
    def state_file(self) -> Path:
        return self.root / STATE_NAME

    def _bin(self, name: str) -> Path:
        directory = "Scripts" if sys.platform == "win32" else "bin"
        suffix = ".exe" if sys.platform == "win32" else ""
        return self.venv_dir / directory / (name + suffix)

    @property
    def python_bin(self) -> Path:
        return self._bin("python")

    @property
    def kit_bin(self) -> Path:
        return self._bin("mineru-kit")

    # -- status ----------------------------------------------------------
    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            state = self._read_state()
            state.update(changes)
            state["updated_at"] = _utcnow()
            _atomic_json(self.state_file, state)
            return state

    def is_ready(self) -> bool:
        """ready 判定：kit 二进制 + 模型目录 + 完成标记三者齐备。"""
        return self.kit_bin.exists() and self.models_dir.is_dir() and self.marker_file.is_file()

    def installed_version(self) -> str:
        try:
            first = self.marker_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            return ""
        return first.removeprefix("mineru==").strip() or MINERU_VERSION

    def bytes_used(self) -> int:
        if not self.root.is_dir():
            return 0
        try:
            stamp = self.venv_dir.stat().st_mtime if self.venv_dir.is_dir() else 0.0
        except OSError:
            stamp = 0.0
        with self._lock:
            if self._size_cache and self._size_cache[0] == stamp:
                return self._size_cache[1]
        total = 0
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    continue
        with self._lock:
            self._size_cache = (stamp, total)
        return total

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        installing = bool(state.get("installing")) and self._thread is not None and self._thread.is_alive()
        ready = self.is_ready()
        return {
            "backend_id": LOCAL_BACKEND_ID,
            "installed": ready,
            "version": self.installed_version() if ready else str(state.get("version") or MINERU_VERSION),
            "engine_root": str(self.root),
            "bytes": self.bytes_used(),
            "installing": installing,
            "phase": str(state.get("phase") or ("done" if ready else "idle")),
            "step_index": int(state.get("step_index") or 0),
            "steps": list(ENGINE_STEPS),
            "step_labels": dict(STEP_LABELS),
            "progress": state.get("progress"),
            "message": str(state.get("message") or ""),
            "error": str(state.get("error") or ""),
            "cancel_requested": self._cancel_event.is_set(),
            "active_jobs": sorted(self._active_jobs),
            "supported": platform_key() in UV_SHA256,
            "updated_at": str(state.get("updated_at") or ""),
        }

    # -- environment -----------------------------------------------------
    def env(self) -> dict[str, str]:
        values = dict(os.environ)
        values.update({
            "MINERU_HOME": str(self.root / "mineru-home"),
            "UV_PYTHON_INSTALL_DIR": str(self.root / "python"),
            "UV_CACHE_DIR": str(self.root / ".uv-cache"),
            # 字节码缓存统一落在 Literature 目录：外部引擎（共享 venv/解释器）
            # 运行时向其 Python 目录写 __pycache__ 的行为被彻底隔离（实测验证）。
            "PYTHONPYCACHEPREFIX": str(self.root / "pycache"),
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "DO_NOT_TRACK": "1",
            "PYTHONUNBUFFERED": "1",
            "TQDM_MININTERVAL": "0.5",
        })
        return values

    # -- process lifecycle ----------------------------------------------
    def _journal_processes(self) -> None:
        with self._lock:
            payload = {
                str(pid): {"command": list(item.command), "tag": item.tag, "started_at": _utcnow()}
                for pid, item in self._processes.items()
            }
        try:
            _atomic_json(self.root / PROCESS_JOURNAL, payload)
        except OSError:
            pass

    def _reap_leftover_processes(self) -> None:
        """进程崩溃/退出可能遗留后台推理进程：启动时按进程日志清理。"""
        journal = self.root / PROCESS_JOURNAL
        try:
            payload = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for raw_pid in list(payload):
            try:
                pid = int(raw_pid)
                os.kill(pid, 0)
            except (ValueError, ProcessLookupError, PermissionError):
                continue
            try:
                os.kill(pid, 15)
            except OSError:
                continue
        try:
            journal.unlink(missing_ok=True)
        except OSError:
            pass

    def _terminate_processes(self, tag: str | None = None) -> None:
        """终止登记的子进程；给定 tag 时只终止该类进程（None=全部，仅用于启动清理）。"""
        with self._lock:
            items = [(pid, item) for pid, item in self._processes.items() if tag is None or item.tag == tag]
            for pid, _item in items:
                self._processes.pop(pid, None)
        for _pid, item in items:
            try:
                item.handle.terminate()
            except OSError:
                pass
        self._journal_processes()

    # -- install ---------------------------------------------------------
    def install(self, *, force: bool = False) -> dict[str, Any]:
        """开始（或恢复）安装；已在安装中时直接返回当前状态。

        ``force=True`` 是「修复/重装」：引擎已就绪时也重跑各步骤（幂等，
        完成的步骤会跳过）；修复与本地解析任务互斥，避免破坏运行中任务。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            if self.is_ready() and not force:
                return self.status()
            if force and self._active_jobs:
                raise EngineBusyError("有本地解析任务正在使用引擎；请等待任务完成后再修复。")
            self._cancel_event.clear()
            state = self._write_state(
                installing=True, phase="uv", step_index=0, error="", message="开始准备本地 MinerU 引擎",
                version=MINERU_VERSION, started_at=_utcnow(), progress=None,
            )
            thread = threading.Thread(target=self._install_worker, name="literature-engine-install", daemon=True)
            self._thread = thread
        thread.start()
        return state

    def cancel(self) -> dict[str, Any]:
        self._cancel_event.set()
        return self.status()

    def _install_worker(self) -> None:
        try:
            self._run_install_steps()
        except _InstallCancelled:
            self._terminate_processes("install")
            self._write_state(installing=False, phase="cancelled", error="安装已取消；已下载内容保留，可随时继续安装。")
        except Exception as error:
            self._terminate_processes("install")
            self._write_state(installing=False, phase="failed", error=str(error))
        finally:
            with self._lock:
                self._thread = None
            self._cancel_event.clear()

    def _check_cancel(self) -> None:
        if self._cancel_event.is_set():
            raise _InstallCancelled()

    def _run_install_steps(self) -> None:
        for index, step in enumerate(ENGINE_STEPS):
            self._check_cancel()
            self._write_state(phase=step, step_index=index, message=f"正在{STEP_LABELS[step]}…", progress=None)
            handler = getattr(self, f"_step_{step}")
            handler()
        self._check_cancel()
        marker = f"mineru=={MINERU_VERSION}\n{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n"
        _atomic_text(self.marker_file, marker)
        self._write_state(
            installing=False, phase="done", step_index=len(ENGINE_STEPS), progress=None,
            message=f"本地 MinerU 引擎 {MINERU_VERSION} 已就绪", error="",
        )

    # individual steps（幂等：重跑会跳过已完成的部分）
    def _step_uv(self) -> None:
        found = shutil.which("uv")
        if found:
            return
        target = self._bundled_uv()
        if target is not None:
            return
        self._download_uv()

    def _bundled_uv(self) -> Path | None:
        directory = "Scripts" if sys.platform == "win32" else "bin"
        suffix = ".exe" if sys.platform == "win32" else ""
        for candidate in sorted((self.root / "uv").glob(f"**/uv{suffix}")):
            if candidate.name == f"uv{suffix}":
                try:
                    candidate.chmod(0o755)
                except OSError:
                    pass
                return candidate
        return None

    def _download_uv(self) -> None:
        key = platform_key()
        digest = UV_SHA256.get(key)
        if not digest:
            raise WorkflowError(f"当前平台（{key}）暂未提供本地引擎安装包。")
        suffix = "zip" if key.endswith("windows-msvc") else "tar.gz"
        asset = f"uv-{key}.tar.gz" if suffix == "tar.gz" else f"uv-{key}.zip"
        url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/{asset}"
        archive = self.root / "uv" / asset
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.is_file():
            self._download_file(url, archive, label="下载 uv 安装工具")
        actual = _sha256_file(archive)
        if actual != digest:
            archive.unlink(missing_ok=True)
            raise WorkflowError(f"uv 安装包校验失败（sha256 不匹配），已删除并中止：{actual}")
        self._extract_archive(archive, self.root / "uv", digest_present=True)
        if self._bundled_uv() is None:
            raise WorkflowError("uv 安装包解压后未找到可执行文件。")

    def _download_file(self, url: str, target: Path, *, label: str) -> None:
        import httpx

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        try:
            with httpx.Client(timeout=httpx.Timeout(60.0, connect=20.0), follow_redirects=True) as client:
                with client.stream("GET", url) as response:
                    if response.status_code >= 300:
                        raise WorkflowError(f"{label}失败：下载服务返回 HTTP {response.status_code}。")
                    total_header = response.headers.get("Content-Length")
                    total = int(total_header) if total_header and total_header.isdigit() else None
                    done = 0
                    with temporary.open("wb") as stream:
                        for block in response.iter_bytes(chunk_size=1024 * 1024):
                            self._check_cancel()
                            if not block:
                                continue
                            stream.write(block)
                            done += len(block)
                            if total:
                                self._report_subprogress(done / total, label)
        except _InstallCancelled:
            raise
        except WorkflowError:
            raise
        except Exception as error:
            temporary.unlink(missing_ok=True)
            raise WorkflowError(f"{label}失败：{error}") from error
        temporary.replace(target)

    def _extract_archive(self, archive: Path, destination: Path, *, digest_present: bool = False) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            if archive.suffix == ".zip":
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extractall(destination)
            else:
                import tarfile

                with tarfile.open(archive) as bundle:
                    bundle.extractall(destination, filter="data")
        except Exception as error:
            raise WorkflowError(f"解压安装工具失败：{error}") from error

    def _report_subprogress(self, fraction: float, label: str) -> None:
        self._write_state(progress=max(0.0, min(1.0, fraction)), message=f"正在{label}…")

    def _step_python(self) -> None:
        if self.python_bin.exists():
            return
        uv = self._uv_binary()
        self._stream([uv, "venv", str(self.venv_dir), "--python", ENGINE_PYTHON, "--python-preference", "only-managed"],
                     label=STEP_LABELS["python"])

    def _uv_binary(self) -> str:
        found = shutil.which("uv")
        if found:
            return found
        bundled = self._bundled_uv()
        if bundled is not None:
            return str(bundled)
        # 上一步已保证 uv 存在；这里兜底重下载一次。
        self._download_uv()
        bundled = self._bundled_uv()
        if bundled is None:
            raise WorkflowError("无法获得 uv 安装工具。")
        return str(bundled)

    def _step_packages(self) -> None:
        uv = self._uv_binary()
        self._stream(
            [uv, "pip", "install", "--python", str(self.python_bin), f"mineru=={MINERU_VERSION}"],
            label=STEP_LABELS["packages"],
        )

    def _step_models(self) -> None:
        self._stream(
            [str(self.kit_bin), "models", "download", "--tier", MODEL_TIER],
            label=STEP_LABELS["models"],
        )
        if not self.models_dir.is_dir():
            raise WorkflowError("模型下载步骤结束但没有找到模型目录；安装未完成。")

    def _step_verify(self) -> None:
        output = self._capture([str(self.python_bin), "-c", "import mineru; print(mineru.__version__)"])
        version = output.strip().splitlines()[-1].strip() if output.strip() else ""
        if version != MINERU_VERSION:
            raise WorkflowError(f"引擎自检失败：期望 mineru=={MINERU_VERSION}，实际 {version or '(无输出)'}。")
        model_files = [item for item in self.models_dir.rglob("*") if item.is_file()] if self.models_dir.is_dir() else []
        if not model_files:
            raise WorkflowError("引擎自检失败：模型目录为空。")
        self._write_state(message="自检通过", progress=None)

    # -- uninstall -------------------------------------------------------
    def uninstall(self) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise EngineBusyError("引擎正在安装或修复；请先取消安装再卸载。")
            if self._active_jobs:
                raise EngineBusyError("有本地解析任务正在使用引擎；请等待任务完成或停止后再卸载。")
            if self.root.exists():
                shutil.rmtree(self.root, ignore_errors=False)
            self._size_cache = None
        return self.status()

    # -- job mutex -------------------------------------------------------
    def register_job(self, job_id: str) -> None:
        with self._lock:
            self._active_jobs.add(str(job_id))

    def unregister_job(self, job_id: str) -> None:
        with self._lock:
            self._active_jobs.discard(str(job_id))

    # -- subprocess plumbing --------------------------------------------
    _PERCENT = re.compile(r"(\d{1,3})%")

    def _stream(self, command: list[str], *, label: str) -> int:
        """执行一个安装步骤：stdout 合流解析进度，取消时立即终止进程。"""
        self._check_cancel()
        self.root.mkdir(parents=True, exist_ok=True)
        log_path = self.root / "logs" / "install.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            [str(item) for item in command],
            env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=sys.platform != "win32",
        )
        with self._lock:
            self._processes[process.pid] = _RunningProcess(process, tuple(str(item) for item in command), tag="install")
        self._journal_processes()
        try:
            assert process.stdout is not None
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"\n$ {' '.join(str(item) for item in command)}\n")
                log.flush()
                buffer = b""
                while True:
                    if self._cancel_event.is_set():
                        process.terminate()
                    chunk = process.stdout.read1(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    *lines, buffer = re.split(rb"[\r\n]", buffer)
                    for raw in lines:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        log.write(line + "\n")
                        match = self._PERCENT.search(line)
                        if match:
                            self._report_subprogress(int(match.group(1)) / 100.0, label)
            code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
            with self._lock:
                self._processes.pop(process.pid, None)
            self._journal_processes()
        if self._cancel_event.is_set():
            raise _InstallCancelled()
        if code:
            raise WorkflowError(f"{label}失败（退出码 {code}）；详情见安装日志。")
        return code

    def _capture(self, command: list[str]) -> str:
        result = subprocess.run(
            [str(item) for item in command], env=self.env(),
            capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode:
            raise WorkflowError(f"自检命令失败：{result.stderr.strip() or result.stdout.strip()}")
        return result.stdout


class _InstallCancelled(Exception):
    pass


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# -- shared engine instance ---------------------------------------------
_default_engine: LocalMinerUEngine | None = None
_default_engine_lock = threading.Lock()


def shared_engine() -> LocalMinerUEngine:
    global _default_engine
    with _default_engine_lock:
        if _default_engine is None:
            _default_engine = LocalMinerUEngine()
        return _default_engine


def set_shared_engine(engine: LocalMinerUEngine | None) -> None:
    global _default_engine
    with _default_engine_lock:
        _default_engine = engine


# -- engine source selection（managed / external_cli / http_service）------
ENGINE_KINDS = ("managed", "external_cli", "http_service")
SELECTION_NAME = "parse-engine.json"
HTTP_KEYCHAIN_SERVICE = "local.literature.mineru-http"
HTTP_KEYCHAIN_ACCOUNT = "engine-api-key"


def runtime_root() -> Path:
    configured = os.environ.get("LITERATURE_RUNTIME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / "Library" / "Application Support" / "Literature"


def selection_file() -> Path:
    return runtime_root() / SELECTION_NAME


def _read_selection() -> dict[str, Any]:
    try:
        value = json.loads(selection_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"kind": "managed"}
    if not isinstance(value, dict) or value.get("kind") not in ENGINE_KINDS:
        return {"kind": "managed"}
    return value


def _write_selection(value: dict[str, Any]) -> None:
    path = selection_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def get_engine_selection() -> dict[str, Any]:
    return _read_selection()


def validate_http_endpoint(url: str, *, allow_plaintext: bool = False) -> str:
    """校验并规范化解析服务地址；返回不含尾斜杠的规范 URL。

    复用模型服务的回环判定规则（`providers._is_loopback_host`）：只有精确
    localhost 与真正回环 IP（127.0.0.0/8、::1）豁免明文 HTTP；`0.0.0.0`、
    `*.local` 一律不算本机。URL 内的 userinfo / query / fragment 属于歧义
    来源，直接拒绝；调用方不得跟随重定向（防止 HTTPS→HTTP 绕过）。
    """
    from urllib.parse import urlsplit

    from .providers import _is_loopback_host

    raw = (url or "").strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise WorkflowError("服务地址必须以 http:// 或 https:// 开头。")
    if parts.username or parts.password:
        raise WorkflowError("服务地址不应包含用户名/密码（userinfo）；请把密钥填在「服务密钥」里。")
    if parts.query or parts.fragment:
        raise WorkflowError("服务地址不应包含查询参数（?…）或片段（#…）。")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise WorkflowError("服务地址缺少主机名。")
    try:
        port = parts.port
    except ValueError as error:
        raise WorkflowError("服务地址的端口不合法。") from error
    if port is not None and not (1 <= port <= 65535):
        raise WorkflowError("服务地址的端口不合法。")
    if parts.scheme == "http" and not _is_loopback_host(host) and not allow_plaintext:
        raise WorkflowError(
            "远程地址使用明文 HTTP；为保护密钥，请改用 HTTPS，或在确认内网安全后勾选「允许明文 HTTP」。"
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def set_engine_selection(kind: str, *, external_dir: str = "", http_url: str = "",
                         allow_plaintext_http: bool = False, http_key: str = "") -> dict[str, Any]:
    """保存解析引擎来源；外部 CLI 与 HTTP 服务必须先通过探测验证。

    连接验证成功才会写 selection；验证失败不落任何配置、不动已有密钥。
    ``http_key`` 是用户本次显式输入的密钥：验证通过后按规范化地址绑定保存。
    """
    if kind not in ENGINE_KINDS:
        raise WorkflowError(f"未知的引擎来源：{kind}")
    if kind == "external_cli":
        probe = probe_external_engine(external_dir)
        if not probe["compatible"]:
            raise WorkflowError(probe["reason"] or "该路径不是可用的 MinerU CLI 引擎。")
        value = {"kind": kind, "external_dir": str(probe["path"]), "allow_plaintext_http": False}
    elif kind == "http_service":
        normalized = validate_http_endpoint(http_url, allow_plaintext=allow_plaintext_http)
        probe = probe_http_service(normalized, key=http_key.strip() or None,
                                   allow_plaintext=allow_plaintext_http)
        if not probe["compatible"]:
            raise WorkflowError(probe["reason"] or "该服务不是受支持的 MinerU 解析 API。")
        if http_key.strip():
            save_http_service_key(normalized, http_key.strip())
        value = {"kind": kind, "http_url": normalized, "allow_plaintext_http": bool(allow_plaintext_http)}
    else:
        value = {"kind": "managed", "allow_plaintext_http": False}
    _write_selection(value)
    return value


def source_identity_snapshot() -> dict[str, Any]:
    """当前来源的完整身份快照（不含密钥）：冻结进任务 backend_config。

    HTTP 服务冻结规范化 URL；外部 CLI 冻结解析后的 kit/python/模型路径与
    版本；独立安装冻结引擎根目录。同 kind 不同地址/路径 → 快照不同 → 拒绝。
    """
    kind = active_source_kind()
    selection = get_engine_selection()
    if kind == "http_service":
        url = validate_http_endpoint(
            str(selection.get("http_url", "")),
            allow_plaintext=bool(selection.get("allow_plaintext_http")),
        )
        return {"kind": kind, "http_url": url}
    if kind == "external_cli":
        probe = probe_external_engine(str(selection.get("external_dir", "")))
        if not probe.get("available"):
            raise WorkflowError(probe.get("reason") or "已选的本地 CLI 引擎不可用；请在设置里重新检测。")
        return {
            "kind": kind, "external_dir": str(probe["path"]),
            "kit_binary": str(probe["kit_binary"]), "python_binary": str(probe.get("python_binary") or ""),
            "models_dir": str(probe.get("models_dir") or ""), "version": str(probe.get("version") or ""),
        }
    return {"kind": "managed", "engine_root": str(shared_engine().root)}


# HTTP 服务密钥：与 MinerU Token 相同的 Keychain 模式，不进项目/日志/快照。
# 密钥按规范化服务地址绑定（account = engine-api-key-<url 摘要>）；
# 换地址不会读到旧服务的密钥。
def _http_key_account(url: str) -> str:
    import hashlib

    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{HTTP_KEYCHAIN_ACCOUNT}-{digest}"


def get_http_service_key(url: str) -> str:
    override = os.environ.get("LITERATURE_MINERU_HTTP_KEY", "").strip()
    if override:
        return override
    account = _http_key_account(validate_http_endpoint(url, allow_plaintext=True))
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", HTTP_KEYCHAIN_SERVICE, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError, WorkflowError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def save_http_service_key(url: str, key: str) -> None:
    key = key.strip()
    if not key:
        raise WorkflowError("服务密钥不能为空。")
    account = _http_key_account(validate_http_endpoint(url, allow_plaintext=True))
    try:
        result = subprocess.run(
            ["/usr/bin/security", "add-generic-password", "-U", "-s", HTTP_KEYCHAIN_SERVICE,
             "-a", account, "-w", key],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError, WorkflowError) as error:
        raise WorkflowError(f"无法访问 macOS Keychain：{error}") from error
    if result.returncode:
        raise WorkflowError(f"无法将服务密钥保存到 macOS Keychain（状态 {result.returncode}）。")


def delete_http_service_key(url: str) -> None:
    try:
        account = _http_key_account(validate_http_endpoint(url, allow_plaintext=True))
    except WorkflowError:
        return
    try:
        subprocess.run(
            ["/usr/bin/security", "delete-generic-password", "-s", HTTP_KEYCHAIN_SERVICE, "-a", account],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# -- external CLI probe --------------------------------------------------
EXTERNAL_DISCOVERY_PATHS = (
    Path.home() / "Library" / "Application Support" / "TextFold" / "engine",
    Path.home() / "Library" / "Application Support" / "PaperFold" / "engine",
)


def _find_kit_binary(root: Path) -> Path | None:
    """在引擎目录里找 mineru-kit 可执行文件（venv 布局或直接指定二进制）。"""
    if root.is_file() and root.name.startswith("mineru-kit"):
        return root
    for sub, name in (("bin", "mineru-kit"), ("Scripts", "mineru-kit.exe")):
        candidate = root / "venv" / sub / name
        if candidate.is_file():
            return candidate
    return None


def _find_engine_python(root: Path) -> Path | None:
    for sub, name in (("bin", "python"), ("Scripts", "python.exe")):
        candidate = root / "venv" / sub / name
        if candidate.is_file():
            return candidate
    return None


def _find_external_models(root: Path) -> Path | None:
    for candidate in (root / "mineru-home" / "models", root / "models"):
        if candidate.is_dir():
            return candidate
    return None


def probe_external_engine(path: str | Path) -> dict[str, Any]:
    """探测一个已有 CLI 引擎目录：版本、模型、输出兼容性。只读，不写外部环境。"""
    root = Path(path).expanduser()
    kit = _find_kit_binary(root)
    if kit is None:
        return {"available": False, "compatible": False, "path": str(root),
                "reason": "该目录下没有找到 mineru-kit 可执行文件（需要 <目录>/venv/bin/mineru-kit 或直接指向 kit 二进制）。"}
    python = _find_engine_python(root)
    version = ""
    # 版本探测：优先 installed.txt（TextFold/PaperFold 布局），否则运行 kit --version
    #（带 PYTHONPYCACHEPREFIX 隔离字节码缓存，见实测结论）。
    for marker in (root / MARKER_NAME, root.parent / MARKER_NAME):
        try:
            first = marker.read_text(encoding="utf-8").splitlines()[0].strip()
        except (OSError, IndexError):
            continue
        if first.startswith("mineru=="):
            version = first.removeprefix("mineru==").strip()
            break
    if not version:
        try:
            env = dict(os.environ)
            env["PYTHONPYCACHEPREFIX"] = str(shared_engine().root / "pycache")
            result = subprocess.run(
                [str(kit), "--version"], env=env, capture_output=True, text=True, timeout=30, check=False,
            )
            match = re.search(r"(\d+\.\d+\.\d+)", result.stdout + result.stderr)
            version = match.group(1) if match else ""
        except (OSError, subprocess.SubprocessError):
            version = ""
    models = _find_external_models(root)
    models_ready = bool(models and any(item.is_file() for item in models.rglob("*")))
    compatible = version.startswith("4.0.")
    reason = ""
    if not version:
        reason = "无法确认该引擎的 MinerU 版本；Literature 仅支持已验证的 4.0.x 输出协议。"
    elif not compatible:
        reason = f"该引擎的 MinerU 版本为 {version}；Literature 当前仅支持 4.0.x（已验证 4.0.10）。"
    elif not models_ready:
        reason = "该引擎没有已下载的模型（未找到非空 models 目录）；请先在其原应用里完成模型下载。"
    return {
        "available": compatible and models_ready,
        "compatible": compatible,
        "kind": "external_cli",
        "path": str(kit.parent.parent.parent if python else root),
        "kit_binary": str(kit),
        "python_binary": str(python) if python else "",
        "models_dir": str(models) if models else "",
        "version": version,
        "models_ready": models_ready,
        "reason": reason,
    }


def discover_external_engines() -> list[dict[str, Any]]:
    """发现本机常见的已有引擎安装（TextFold/PaperFold 只是候选，不是特判入口）。"""
    found = []
    seen: set[str] = set()
    for candidate in EXTERNAL_DISCOVERY_PATHS:
        if not candidate.is_dir():
            continue
        probe = probe_external_engine(candidate)
        key = probe.get("path", str(candidate))
        if key in seen:
            continue
        seen.add(key)
        found.append(probe)
    return found


# -- HTTP service probe（官方 MinerU v1 自托管 API 协议）------------------
HTTP_PROBE_TIMEOUT = 8.0


def probe_http_service(url: str, *, key: str | None = None,
                       allow_plaintext: bool | None = None) -> dict[str, Any]:
    """探测一个自建 MinerU 解析服务（官方 mineru-api 的 /v1 协议）。

    兼容判定基于官方明确协议：/v1/health 返回 version 且 /v1/tiers 可读。
    不声称任意版本兼容；探不到或协议不符时给出明确原因。
    ``key`` 是本次显式提供的密钥；缺省时只读取**绑定到该地址**的已存密钥，
    绝不把别的服务密钥发往新地址。不跟随重定向（防 HTTPS→HTTP 绕过）。
    """
    import httpx

    selection = get_engine_selection()
    if allow_plaintext is None:
        allow_plaintext = bool(selection.get("allow_plaintext_http")) and str(selection.get("http_url") or "").rstrip("/") == url.rstrip("/")
    url = validate_http_endpoint(url, allow_plaintext=allow_plaintext)
    headers: dict[str, str] = {}
    resolved_key = (key or "").strip() or get_http_service_key(url)
    if resolved_key:
        headers["Authorization"] = f"Bearer {resolved_key}"
    try:
        # 本机/局域网服务直连，不读环境代理（NO_PROXY 里的 IPv6 条目会导致 httpx 解析失败）。
        with httpx.Client(timeout=HTTP_PROBE_TIMEOUT, follow_redirects=False, trust_env=False) as client:
            response = client.get(f"{url}/v1/health", headers=headers)
            if 300 <= response.status_code < 400:
                return {"available": False, "compatible": False, "kind": "http_service",
                        "url": url, "reason": "服务返回重定向；已拒绝（不允许跳转，防止 HTTPS→HTTP 绕过）。"}
            if response.status_code in (401, 403):
                return {"available": False, "compatible": False, "kind": "http_service",
                        "url": url, "reason": "服务要求 API 密钥或密钥不正确；请在连接时填写服务密钥。"}
            if response.status_code >= 300:
                return {"available": False, "compatible": False, "kind": "http_service",
                        "url": url, "reason": f"健康检查失败（HTTP {response.status_code}）；该地址可能不是 MinerU 解析服务。"}
            payload = response.json()
            version = str((payload or {}).get("version") or "")
            tiers_response = client.get(f"{url}/v1/tiers", headers=headers)
            tiers = []
            if tiers_response.status_code < 300:
                data = (tiers_response.json() or {}).get("data") or []
                tiers = [str(item.get("id")) for item in data if isinstance(item, dict)]
    except WorkflowError:
        raise
    except Exception as error:
        return {"available": False, "compatible": False, "kind": "http_service", "url": url,
                "reason": f"无法连接服务：{error}"}
    compatible = bool(version) and bool(tiers)
    return {
        "available": compatible,
        "compatible": compatible,
        "kind": "http_service",
        "url": url,
        "version": version,
        "tiers": tiers,
        "reason": "" if compatible else "服务可达但缺少 MinerU v1 协议特征（version/tiers）；暂不支持该协议。",
    }


def active_source_kind() -> str:
    return str(_read_selection().get("kind") or "managed")


def external_engine_ready() -> bool:
    selection = _read_selection()
    if selection.get("kind") != "external_cli":
        return False
    probe = probe_external_engine(selection.get("external_dir", ""))
    return bool(probe.get("available"))


def http_service_ready() -> bool:
    selection = _read_selection()
    if selection.get("kind") != "http_service":
        return False
    return bool(probe_http_service(str(selection.get("http_url", ""))).get("available"))


def local_engine_ready() -> bool:
    """mineru_local 后端是否可执行（任一来源就绪）。"""
    kind = active_source_kind()
    if kind == "external_cli":
        return external_engine_ready()
    if kind == "http_service":
        return http_service_ready()
    return shared_engine().is_ready()


# -- dynamic capabilities ------------------------------------------------
def local_capabilities() -> ParseBackendCapabilities:
    from .parse_backends import _BACKEND_FORMATS

    kind = active_source_kind()
    if kind == "http_service":
        selection = get_engine_selection()
        try:
            probe = probe_http_service(str(selection.get("http_url", "")))
        except WorkflowError as error:
            probe = {"available": False, "reason": str(error)}
        if probe.get("available"):
            return ParseBackendCapabilities(
                backend_id=LOCAL_BACKEND_ID,
                display_name=f"MinerU 解析服务（自建 {probe.get('version') or ''}）".strip(),
                version=str(probe.get("version") or "unknown"),
                execution="local",
                supported_formats=_BACKEND_FORMATS,
                slicing=True,
                polling=True,
                upload_url=True,
                remote_job_control=True,
                resumable=True,
                available=True,
            )
        return ParseBackendCapabilities(
            backend_id=LOCAL_BACKEND_ID,
            display_name="MinerU 解析服务（自建）",
            version="unavailable",
            execution="local",
            supported_formats=_BACKEND_FORMATS,
            slicing=True,
            polling=True,
            upload_url=True,
            remote_job_control=True,
            resumable=True,
            available=False,
            unavailable_reason=probe.get("reason") or "解析服务不可用；请在设置 → 文档解析里检查连接。",
        )
    if kind == "external_cli":
        selection = get_engine_selection()
        probe = probe_external_engine(str(selection.get("external_dir", "")))
        if probe.get("available"):
            return ParseBackendCapabilities(
                backend_id=LOCAL_BACKEND_ID,
                display_name=f"本地 CLI 引擎（外部安装 {probe.get('version') or ''}）".strip(),
                version=str(probe.get("version") or "unknown"),
                execution="local",
                supported_formats=_BACKEND_FORMATS,
                slicing=True,
                polling=False,
                upload_url=False,
                remote_job_control=True,
                resumable=True,
                available=True,
            )
        return ParseBackendCapabilities(
            backend_id=LOCAL_BACKEND_ID,
            display_name="本地 CLI 引擎（外部安装）",
            version="unavailable",
            execution="local",
            supported_formats=_BACKEND_FORMATS,
            slicing=True,
            polling=False,
            upload_url=False,
            remote_job_control=True,
            resumable=True,
            available=False,
            unavailable_reason=probe.get("reason") or "外部引擎不可用；请在设置 → 文档解析里重新检测。",
        )
    engine = shared_engine()
    ready = engine.is_ready()
    state = engine.status()
    if ready:
        return ParseBackendCapabilities(
            backend_id=LOCAL_BACKEND_ID,
            display_name=f"本地 MinerU 引擎（{engine.installed_version()}）",
            version=engine.installed_version(),
            execution="local",
            supported_formats=_BACKEND_FORMATS,
            slicing=True,
            polling=False,
            upload_url=False,
            remote_job_control=True,
            resumable=True,
            available=True,
        )
    if not state.get("supported", True):
        reason = f"当前平台（{platform_key()}）暂未提供本地引擎安装包。"
    elif state.get("installing"):
        reason = "本地 MinerU 引擎正在安装，安装完成后即可选择本地解析。"
    elif state.get("phase") == "failed":
        reason = f"本地 MinerU 引擎未就绪：上次安装失败（{state.get('error') or '未知错误'}），可在设置里重试。"
    else:
        reason = "本地 MinerU 引擎尚未安装；可在设置 → 文档解析里下载安装，或改用已有安装/已有服务。"
    return ParseBackendCapabilities(
        backend_id=LOCAL_BACKEND_ID,
        display_name="本地 MinerU 引擎",
        version="unavailable",
        execution="local",
        supported_formats=_BACKEND_FORMATS,
        slicing=True,
        polling=False,
        upload_url=False,
        remote_job_control=True,
        resumable=True,
        available=False,
        unavailable_reason=reason,
    )


# -- parse backend -------------------------------------------------------
class LocalMinerUBackend:
    """本地解析适配器：接入 prepare/execute/status/result 协议。

    切片（prepare）复用工作流共享逻辑；执行阶段逐片调用 ``mineru-kit parse``，
    片内取消为真取消；结果在切片目录内**原样保留 kit ZIP 输出**并派生
    ``content_list_v2.json``（官方 renderer）与 ``layout.json``（页几何），
    之后由工作流协调层统一 seal/normalize/merge。
    """

    def __init__(self, workflow: Any, engine: LocalMinerUEngine | None = None,
                 http_client_factory: Callable[[dict[str, str]], Any] | None = None) -> None:
        self.workflow = workflow
        self.engine = engine or shared_engine()
        self._http_client_factory = http_client_factory

    def capabilities(self) -> ParseBackendCapabilities:
        return local_capabilities()

    def prepare(self, job: WorkflowJob, *, progress: Callable[[WorkflowJob, str], None] | None = None) -> WorkflowJob:
        self._require_ready()
        self.workflow._prepare(job, progress)
        return job

    def execute(self, job: WorkflowJob, *, token: str | None = None,
                progress: Callable[[WorkflowJob, str], None] | None = None) -> bool:
        self._require_ready()
        self.engine.register_job(job.job_id)
        try:
            pending = [item for item in job.slices if item.state == SliceState.PREPARED]
            if not pending:
                return True
            job.state = JobState.PROCESSING
            self.workflow._notify(job, progress, f"本地引擎开始解析 {len(pending)} 个切片")
            for item in pending:
                if self.workflow._stop_event.is_set():
                    job.state = JobState.WAITING
                    self.workflow._notify(job, progress, "已停止本地解析；已完成切片保留，可稍后继续")
                    return False
                self._parse_slice(job, item, progress)
            return True
        finally:
            self.engine.unregister_job(job.job_id)

    def status(self, job: WorkflowJob) -> WorkflowJob:
        return job

    def result(self, job: WorkflowJob, *, progress: Callable[[WorkflowJob, str], None] | None = None) -> ParseResult:
        slices = sorted(job.slices, key=lambda item: item.start_page)
        raw_directories = tuple(item.result_directory for item in slices if item.result_directory)
        source_pages = {
            item.result_directory: tuple(item.source_pages)
            for item in slices if item.result_directory and item.source_pages
        }
        return ParseResult(
            backend_id=job.backend_id, job_id=job.job_id, state=job.state.value,
            raw_directories=raw_directories, source_pages=source_pages,
        )

    # -- internals -------------------------------------------------------
    def _require_ready(self) -> None:
        if local_engine_ready():
            return
        kind = active_source_kind()
        if kind == "http_service":
            raise WorkflowError("MinerU 解析服务不可用；请在设置 → 文档解析里检查连接。")
        if kind == "external_cli":
            raise WorkflowError("已选的本地 CLI 引擎不可用或已被移动/升级；请在设置 → 文档解析里重新检测或改用独立安装。")
        state = self.engine.status()
        if state.get("installing"):
            raise WorkflowError("本地 MinerU 引擎正在安装，请等待安装完成后再开始解析。")
        raise WorkflowError("本地 MinerU 引擎尚未安装或已损坏；请在设置 → 文档解析里安装或修复。")

    def _check_source_snapshot(self, job: WorkflowJob) -> dict[str, Any]:
        """任务创建时冻结的引擎来源不得被静默切换；返回冻结的来源配置供执行使用。

        比较 kind 与完整来源身份（HTTP 规范化 URL / CLI 引擎与模型路径+版本 /
        独立安装引擎根目录）。同 kind 但地址/路径不同同样拒绝；旧任务没有
        identity 快照时只比 kind（历史兼容）。执行一律使用冻结配置，
        不在执行期重读全局 selection（消除 TOCTOU）。
        """
        config = job.backend_config or {}
        snapshot_kind = str(config.get("engine_source") or "managed")
        current_kind = active_source_kind()
        if snapshot_kind != current_kind:
            raise WorkflowError(
                f"该任务创建时使用的解析来源（{snapshot_kind}）与当前选择（{current_kind}）不一致；"
                "为避免结果不可追溯，请在设置里切回原来源后重试，或新建解析任务。"
            )
        snapshot = config.get("engine_source_config")
        if not isinstance(snapshot, dict):
            # 旧任务：无 identity 快照，仅 kind 校验。
            return {"kind": snapshot_kind}
        try:
            current = source_identity_snapshot()
        except WorkflowError as error:
            raise WorkflowError(
                f"该任务要求的解析来源当前不可用：{error}请在设置里恢复原来源后重试。"
            ) from error
        if current != snapshot:
            raise WorkflowError(
                "该任务创建时冻结的解析来源配置已发生变化（地址/引擎路径/版本不一致）；"
                "为避免把同一文档发给不同服务，请在设置里切回原来源后重试，或新建解析任务。"
            )
        return snapshot

    def _parse_slice(self, job: WorkflowJob, item: Any,
                     progress: Callable[[WorkflowJob, str], None] | None) -> None:
        source = Path(item.local_pdf)
        if not source.is_file():
            # 失败重试路径会重建切片；这里兜底重建。
            base = self.workflow._verify_source(job)
            if item.source_pages:
                from .mineru_workflow import _write_pdf_pages
                _write_pdf_pages(base, source, item.source_pages)
            elif item.start_page >= 1:
                from .mineru_workflow import _write_pdf_range
                _write_pdf_range(base, source, item.start_page, item.end_page)
            else:
                shutil.copy2(base, source)
        frozen = self._check_source_snapshot(job)
        kind = frozen["kind"]
        scope = f"p{item.start_page:06d}-{item.end_page:06d}" if item.start_page else "file"
        slices_root = Path(job.raw_output) / "slices"
        result_dir = slices_root / f"{item.index:03d}_{scope}"
        archive = Path(job.workspace) / "downloads" / f"{item.index:03d}.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        item.state = SliceState.PROCESSING
        item.error = ""
        self.workflow.save_job(job)
        try:
            if kind == "http_service":
                self._run_http_parse(job, item, source, archive, progress, frozen)
            else:
                self._run_kit_parse(job, item, source, archive, progress, frozen)
            _extract_kit_zip(archive, result_dir)
            self._derive_result_files(result_dir, source, kind, frozen)
            if not _is_result_directory(result_dir):
                raise WorkflowError("本地引擎输出缺少可用的 content_list_v2；已中止合并。")
            item.state = SliceState.DOWNLOADED
            item.result_zip = str(archive)
            item.result_directory = str(result_dir)
            item.error = ""
            self.workflow._notify(job, progress, f"本地引擎已完成切片 {item.index}/{len(job.slices)}")
        except Exception as error:
            item.error = str(error)
            item.state = SliceState.FAILED
            self.workflow.save_job(job)
            raise

    # -- CLI 引擎工具链（managed / external_cli 共用；外部环境只读调用）------
    # 一律从任务冻结的来源快照构建；执行期不读全局 selection（TOCTOU 消除）。
    def _cli_toolchain(self, frozen: dict[str, Any]) -> dict[str, Any]:
        if frozen.get("kind") == "external_cli":
            kit = Path(str(frozen.get("kit_binary") or ""))
            python = Path(str(frozen.get("python_binary") or "")) if frozen.get("python_binary") else None
            models = Path(str(frozen.get("models_dir") or ""))
            if not kit.is_file():
                raise WorkflowError(
                    f"任务冻结的引擎可执行文件不存在：{kit}；外部应用可能已升级或卸载，"
                    "请在设置 → 文档解析里重新检测或改用独立安装。"
                )
            env = self.engine.env()
            config_path = self.engine.root / "external-config.yaml"
            _ensure_external_config(models, config_path)
            env["MINERU_CONFIG"] = str(config_path)
            # doclib 数据库/socket/日志等默认写入位置隔离到 Literature 自己目录。
            env["MINERU_HOME"] = str(self.engine.root / "external-home")
            Path(env["MINERU_HOME"]).mkdir(parents=True, exist_ok=True)
            return {"kit": kit, "python": python, "env": env, "label": "外部 CLI 引擎"}
        return {
            "kit": self.engine.kit_bin,
            "python": self.engine.python_bin,
            "env": self.engine.env(),
            "label": "本地引擎",
        }

    def _run_kit_parse(self, job: WorkflowJob, item: Any, source: Path, archive: Path,
                       progress: Callable[[WorkflowJob, str], None] | None,
                       frozen: dict[str, Any] | None = None) -> None:
        stop = self.workflow._stop_event
        toolchain = self._cli_toolchain(frozen or {"kind": active_source_kind()})
        command = [str(toolchain["kit"]), "parse", str(source), "-o", str(archive), "--format", "zip"]
        if source.suffix.casefold() == ".pdf":
            # 官方能力：standard 档仅适用于 PDF；非 PDF/图片由 kit 按格式自定
            #（flash-only 格式会被强制 flash），不强行传 tier。
            command += ["--tier", MODEL_TIER, "--ocr-mode", "ocr" if job.is_ocr else "auto"]
        command += ["--disable-image-analysis"]
        log_path = Path(job.workspace) / "logs"
        log_path.mkdir(parents=True, exist_ok=True)
        log_file = log_path / f"slice-{item.index:03d}.log"
        import subprocess as sp

        process = sp.Popen(
            command, env=toolchain["env"], stdout=sp.PIPE, stderr=sp.STDOUT,
            start_new_session=sys.platform != "win32",
        )
        with self.engine._lock:
            self.engine._processes[process.pid] = _RunningProcess(process, tuple(command), tag="parse")
        self.engine._journal_processes()
        percent = re.compile(r"(\d{1,3})%\|")

        def watch() -> None:
            while process.poll() is None:
                if stop.wait(0.5):
                    process.terminate()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            assert process.stdout is not None
            with log_file.open("a", encoding="utf-8") as log:
                log.write(f"\n$ {' '.join(command)}\n")
                log.flush()
                buffer = b""
                while True:
                    chunk = process.stdout.read1(4096)
                    if not chunk:
                        break
                    buffer += chunk
                    *lines, buffer = re.split(rb"[\r\n]", buffer)
                    for raw in lines:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        log.write(line + "\n")
                        match = percent.search(line)
                        if match and progress:
                            fraction = int(match.group(1)) / 100.0
                            self.workflow._notify(job, progress, f"本地引擎解析切片 {item.index}：{match.group(1)}%")
            code = process.wait()
        finally:
            watcher.join(timeout=2)
            if process.poll() is None:
                process.kill()
            with self.engine._lock:
                self.engine._processes.pop(process.pid, None)
            self.engine._journal_processes()
        if stop.is_set():
            job.state = JobState.WAITING
            raise WorkflowError("本地解析已在安全边界停止。")
        if code:
            raise WorkflowError(f"本地引擎解析失败（退出码 {code}）；详情见任务日志 slice-{item.index:03d}.log。")
        if not archive.is_file():
            raise WorkflowError("本地引擎未产出结果文件。")

    def _derive_result_files(self, result_dir: Path, slice_pdf: Path, kind: str = "managed",
                             frozen: dict[str, Any] | None = None) -> None:
        middle = result_dir / "middle_json.json"
        if not middle.is_file():
            raise WorkflowError("本地引擎输出缺少 middle_json.json，无法派生结构化结果。")
        target = result_dir / "content_list_v2.json"
        if not target.is_file():
            if kind == "http_service":
                # HTTP 服务没有引擎 venv 可用：用内置适配器从 middle_json 派生。
                payload = json.loads(middle.read_text(encoding="utf-8"))
                pages = middle_json_to_content_list(payload)
                target.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
            else:
                script_file = result_dir / ".derive_content_list.py"
                script_file.write_text(DERIVE_SCRIPT, encoding="utf-8")
                try:
                    self._run_derive(script_file, middle, target, frozen)
                finally:
                    script_file.unlink(missing_ok=True)
        if not target.is_file():
            raise WorkflowError("content_list_v2 派生失败：引擎 renderer 没有产出结果。")
        _validate_derived_content(target, middle)
        layout = result_dir / "layout.json"
        if not layout.is_file():
            # 页几何优先取 structured_content 的官方 docvortex_layout（实测含每页 pt 尺寸），
            # 缺失时回退用 PyMuPDF 读切片 PDF。
            structured = result_dir / "structured_content.json"
            if structured.is_file() and _write_layout_from_structured(structured, layout):
                return
            if slice_pdf.suffix.casefold() == ".pdf" and slice_pdf.is_file():
                _write_layout_from_pdf(slice_pdf, layout)

    def _run_derive(self, script_file: Path, middle: Path, target: Path, frozen: dict[str, Any] | None = None) -> None:
        toolchain = self._cli_toolchain(frozen or {"kind": active_source_kind()})
        python = toolchain.get("python")
        if python is None or not Path(python).is_file():
            raise WorkflowError("引擎缺少可用的 Python 解释器，无法运行官方 renderer 派生。")
        result = subprocess.run(
            [str(python), str(script_file), str(middle), str(target)],
            env=toolchain["env"], capture_output=True, text=True, timeout=600, check=False,
        )
        if result.returncode:
            raise WorkflowError(f"content_list_v2 派生失败：{result.stderr.strip()[-500:] or result.stdout.strip()[-500:]}")

    # -- HTTP 服务执行（官方 mineru-api /v1 协议）--------------------------
    # 断点恢复边界（如实声明）：远端 job_id 只存活在本次执行内；应用重启后
    # 进行中的切片会重新提交（已完成切片保留，不重复合并），不做远端任务续接。
    def _run_http_parse(self, job: WorkflowJob, item: Any, source: Path, archive: Path,
                        progress: Callable[[WorkflowJob, str], None] | None,
                        frozen: dict[str, Any] | None = None) -> None:
        import httpx

        frozen = frozen or {}
        base = str(frozen.get("http_url") or "").rstrip("/")
        if not base:
            raise WorkflowError("任务缺少冻结的解析服务地址；请新建解析任务。")
        selection = get_engine_selection()
        allow_plaintext = bool(selection.get("allow_plaintext_http")) and str(selection.get("http_url") or "").rstrip("/") == base
        # 每次执行前重新校验传输边界（不只依赖 select 入口检查）。
        base = validate_http_endpoint(base, allow_plaintext=allow_plaintext)
        headers: dict[str, str] = {}
        # 密钥只读取绑定到该地址的条目；换地址不会带旧服务的密钥。
        key = get_http_service_key(base)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        stop = self.workflow._stop_event
        poll_interval = max(1.0, float(self.workflow.poll_interval))
        factory = self._http_client_factory or (lambda headers: httpx.Client(
            timeout=httpx.Timeout(120.0, connect=20.0), follow_redirects=False, trust_env=False,
        ))
        try:
            with factory(headers) as client:
                mime = "application/pdf" if source.suffix.casefold() == ".pdf" else "application/octet-stream"
                create = client.post(f"{base}/v1/uploads", headers=headers, json={
                    "filename": source.name, "bytes": source.stat().st_size,
                    "mime_type": mime, "purpose": "parse",
                })
                if 300 <= create.status_code < 400:
                    raise WorkflowError("解析服务返回重定向；已拒绝（不允许跳转，防止 HTTPS→HTTP 绕过）。")
                if create.status_code >= 300:
                    raise WorkflowError(f"解析服务上传会话创建失败（HTTP {create.status_code}）。")
                upload_id = str((create.json() or {}).get("id") or "")
                if not upload_id:
                    raise WorkflowError("解析服务没有返回上传会话 ID。")
                put = client.put(
                    f"{base}/v1/uploads/{upload_id}/content", headers=headers,
                    content=source.read_bytes(),
                )
                if put.status_code >= 300:
                    raise WorkflowError(f"解析服务文件上传失败（HTTP {put.status_code}）。")
                complete = client.post(f"{base}/v1/uploads/{upload_id}/complete", headers=headers, json={})
                if complete.status_code >= 300:
                    raise WorkflowError(f"解析服务上传完成确认失败（HTTP {complete.status_code}）。")
                completed_payload = complete.json() or {}
                file_id = str(((completed_payload.get("file") or {}).get("id")) or "")
                if not file_id:
                    raise WorkflowError("解析服务没有返回文件 ID。")
                job_payload: dict[str, Any] = {
                    "files": [{"source": {"type": "file_id", "file_id": file_id}}],
                    "ocr_mode": "ocr" if job.is_ocr else "auto",
                    "output_formats": ["zip"],
                }
                if source.suffix.casefold() == ".pdf":
                    # standard 档仅适用于 PDF；非 PDF/图片由服务按格式自定。
                    job_payload["tier"] = MODEL_TIER
                created = client.post(f"{base}/v1/parse/jobs", headers=headers, json=job_payload)
                if created.status_code not in (200, 202):
                    raise WorkflowError(f"解析服务任务创建失败（HTTP {created.status_code}）。")
                created_payload = created.json() or {}
                remote_job = str(created_payload.get("job_id") or "")
                if not remote_job:
                    raise WorkflowError("解析服务没有返回任务 ID。")
                payload: dict[str, Any] = {}
                # 轮询任务状态；停止/取消时尽力取消远端任务（真取消）。
                while True:
                    if stop.is_set():
                        try:
                            client.post(f"{base}/v1/parse/jobs/{remote_job}/cancel", headers=headers)
                        except Exception:
                            pass
                        job.state = JobState.WAITING
                        raise WorkflowError("本地解析已在安全边界停止。")
                    current = client.get(f"{base}/v1/parse/jobs/{remote_job}", headers=headers)
                    if 300 <= current.status_code < 400:
                        raise WorkflowError("解析服务返回重定向；已拒绝（不允许跳转，防止 HTTPS→HTTP 绕过）。")
                    if current.status_code >= 300:
                        raise WorkflowError(f"解析服务状态查询失败（HTTP {current.status_code}）。")
                    payload = current.json() or {}
                    state = str(payload.get("status") or "")
                    task_progress = payload.get("progress") or {}
                    done = int(task_progress.get("completed") or 0)
                    total = int(task_progress.get("total") or 0)
                    if total:
                        self.workflow._notify(job, progress, f"解析服务已完成 {done}/{total} 页")
                    if state in {"completed", "partial"}:
                        break
                    if state in {"failed", "canceled"}:
                        error_text = "解析服务返回失败。"
                        for entry in payload.get("files") or []:
                            detail = (entry or {}).get("error") or {}
                            if detail.get("message"):
                                error_text = str(detail["message"])
                                break
                        raise WorkflowError(f"解析服务任务未完成（{state}）：{error_text}")
                    self.workflow.sleep(poll_interval)
                entries = [entry for entry in (payload.get("files") or [])
                           if (entry or {}).get("status") == "completed"]
                ref = ((entries[0].get("output_files") or {}).get("zip") or {}) if entries else {}
                zip_file_id = str(ref.get("file_id") or "")
                if not zip_file_id:
                    raise WorkflowError("解析服务没有返回 ZIP 结果文件。")
                content = client.get(f"{base}/v1/files/{zip_file_id}/content", headers=headers)
                if 300 <= content.status_code < 400:
                    raise WorkflowError("解析服务返回重定向；已拒绝（不允许跳转，防止 HTTPS→HTTP 绕过）。")
                if content.status_code >= 300:
                    raise WorkflowError(f"解析服务结果下载失败（HTTP {content.status_code}）。")
                data = content.content
                if not data:
                    raise WorkflowError("解析服务返回了空结果。")
                archive.write_bytes(data)
        except httpx.HTTPError as error:
            raise WorkflowError(f"连接解析服务失败：{error}") from error


def _validate_derived_content(target: Path, middle: Path) -> None:
    try:
        pages = json.loads(target.read_text(encoding="utf-8"))
        middle_payload = json.loads(middle.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WorkflowError(f"派生的 content_list_v2 无法读取：{error}") from error
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise WorkflowError("派生的 content_list_v2 顶层格式错误。")
    middle_pages = middle_payload.get("pages") if isinstance(middle_payload, dict) else None
    if isinstance(middle_pages, list) and len(pages) != len(middle_pages):
        raise WorkflowError(
            f"派生的 content_list_v2 页数（{len(pages)}）与 middle_json 页数（{len(middle_pages)}）不一致。"
        )


def _ensure_external_config(models_dir: Path, config_path: Path) -> None:
    """为外部 CLI 引擎生成 Literature 自有 config：模型目录只读共享，
    其余（doclib 数据库/socket/日志/锁）全部落在 Literature 自己目录。"""
    payload = {"model": {"base_dir": str(models_dir), "source": "local"}}
    desired = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        current = config_path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if current != desired:
        _atomic_text(config_path, desired)


def _write_layout_from_structured(structured: Path, layout: Path) -> bool:
    """从 kit 输出 structured_content.json 的官方 docvortex_layout 派生页几何。

    实测（MinerU 4.0.10 standard 档）extensions.docvortex.pages 携带每页
    width_pt/height_pt；没有该字段时返回 False，调用方回退 PyMuPDF。
    """
    try:
        payload = json.loads(structured.read_text(encoding="utf-8"))
        pages = payload["extensions"]["docvortex_layout"]["pages"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(pages, list) or not pages:
        return False
    info = []
    for index, page in enumerate(pages):
        width = float(page.get("width_pt") or 0)
        height = float(page.get("height_pt") or 0)
        info.append({
            "page_idx": int(page.get("page_idx") or index),
            "page_size": [width, height],
            "rotation": int(page.get("rotation") or 0),
        })
    _atomic_json(layout, {"pdf_info": info})
    return True


def _norm_bbox(value: Any) -> list[int] | None:
    """middle_json 的 0-1 归一化 bbox → content_list_v2 的 0-1000 整数框。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        numbers = [float(item) for item in value]
    except (TypeError, ValueError):
        return None
    return [max(0, min(1000, int(item * 1000))) for item in numbers]


def _annotation_spans(block: dict[str, Any]) -> list[Any]:
    content = block.get("content")
    return content if isinstance(content, list) else []


def _body_field(body: dict[str, Any]) -> str:
    """取视觉 body 的字符串内容（表格 HTML / 代码等）；span 列表则拼接文本。"""
    content = body.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for span in content:
            if isinstance(span, dict) and isinstance(span.get("content"), str):
                parts.append(span["content"])
        return "".join(parts)
    return ""


def _flatten_list_children(block: dict[str, Any], depth: int = 0) -> list[tuple[dict[str, Any], int]]:
    leaves: list[tuple[dict[str, Any], int]] = []
    for child in block.get("content") or []:
        if not isinstance(child, dict):
            continue
        if str(child.get("type")) == "list":
            leaves.extend(_flatten_list_children(child, depth + 1))
        else:
            leaves.append((child, depth))
    return leaves


def middle_json_to_content_list(payload: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """内置适配器：把 MinerU 4 的 middle_json 转为按页 content_list_v2。

    仅用于 HTTP 服务来源（没有引擎 venv 可运行官方 renderer）。类型映射与
    官方 renderer 的 v2 输出对齐（段落/标题 span 结构、视觉块注记、列表/目录
    展平、bbox 0-1000）；无法识别的块按正文文本收敛，不丢弃内容。
    """
    pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(pages, list):
        raise WorkflowError("middle_json 缺少 pages，无法适配。")
    output: list[list[dict[str, Any]]] = []
    for page in pages:
        items: list[dict[str, Any]] = []
        for block in page.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            item = _convert_block(block)
            if item is not None:
                items.append(item)
        output.append(items)
    return output


_VISUAL_TYPES = {"image", "table", "chart", "code"}
_ANNOTATION_TYPES = {
    "image_caption", "image_footnote", "table_caption", "table_footnote",
    "chart_caption", "chart_footnote", "code_caption", "code_footnote",
}
_PAGE_TEXT_TYPES = {
    "header": "page_header", "footer": "page_footer",
    "page_number": "page_number", "aside_text": "page_aside_text",
}


def _convert_block(block: dict[str, Any]) -> dict[str, Any] | None:
    block_type = str(block.get("type") or "")
    spans = block.get("content") if isinstance(block.get("content"), list) else []

    def with_bbox(item: dict[str, Any]) -> dict[str, Any]:
        if bbox := _norm_bbox(block.get("bbox")):
            item["bbox"] = bbox
        return item

    if block_type in {"text", "ref_text"}:
        return with_bbox({"type": "paragraph", "content": {"paragraph_content": spans or []}})
    if block_type in {"doc_title", "paragraph_title"}:
        try:
            level = max(1, min(6, int(block.get("level") or 1)))
        except (TypeError, ValueError):
            level = 1
        return with_bbox({"type": "title", "content": {"title_content": spans or [], "level": level}})
    if block_type in _PAGE_TEXT_TYPES:
        mapped = _PAGE_TEXT_TYPES[block_type]
        return with_bbox({"type": mapped, "content": {f"{mapped}_content": spans or []}})
    if block_type == "page_footnote":
        return with_bbox({"type": "page_footnote", "content": {"page_footnote_content": spans or []}})
    if block_type == "equation":
        math = block.get("content")
        math = math if isinstance(math, str) else ""
        return with_bbox({"type": "equation_interline",
                          "content": {"math_content": math.strip(), "math_type": "latex"}})
    if block_type in _VISUAL_TYPES:
        children = block.get("content") if isinstance(block.get("content"), list) else []
        body = next((child for child in children if str(child.get("type", "")).endswith("_body")), None)
        annotations = [child for child in children if str(child.get("type", "")) in _ANNOTATION_TYPES]

        def spans_for(suffix: str) -> list[Any]:
            found = next((child for child in annotations if str(child.get("type")).endswith(suffix)), None)
            return _annotation_spans(found) if found else []

        image_path = (body or {}).get("image_path") or ""
        if block_type == "image":
            content: dict[str, Any] = {
                "image_source": {"path": image_path},
                "image_caption": spans_for("caption"),
                "image_footnote": spans_for("footnote"),
            }
            return with_bbox({"type": "image", "content": content})
        if block_type in {"table", "chart"}:
            content = {
                "image_source": {"path": image_path},
                "table_content": _body_field(body or {}),
                "table_caption": spans_for("caption"),
                "table_footnote": spans_for("footnote"),
            }
            return with_bbox({"type": "table", "content": content})
        # code / algorithm
        return with_bbox({"type": "code", "content": {"code_content": _body_field(body or {})}})
    if block_type == "list":
        leaves = _flatten_list_children(block)
        sub_type = str(block.get("sub_type") or "")
        list_type = "reference_list" if sub_type == "ref_text" else "text_list"
        items = [
            {"item_type": "text", "item_content": leaf.get("content") or []}
            for leaf, _depth in leaves if isinstance(leaf.get("content"), list)
        ]
        content: dict[str, Any] = {"list_type": list_type, "list_items": items}
        if list_type == "text_list":
            content["attribute"] = "unordered"
        return with_bbox({"type": "list", "content": content})
    if block_type == "index":
        leaves = _flatten_list_children(block)
        items = [
            {"item_type": "text", "item_content": leaf.get("content") or []}
            for leaf, _depth in leaves if isinstance(leaf.get("content"), list)
        ]
        return with_bbox({"type": "index", "content": {"list_type": "text_list", "list_items": items}})
    # 未知类型：不丢弃，按正文文本收敛（span 列表或字符串）。
    if isinstance(block.get("content"), str):
        text = block["content"].strip()
        if not text:
            return None
        return with_bbox({"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": text}]}})
    if spans:
        return with_bbox({"type": "paragraph", "content": {"paragraph_content": spans}})
    return None


def _write_layout_from_pdf(slice_pdf: Path, layout: Path) -> None:
    """从切片 PDF 读取页尺寸/旋转，派生 layout.json（页面几何适配文件）。

    MinerU 4 的 middle_json 不再携带页面尺寸；本地切片 PDF 就在任务工作区里，
    用 PyMuPDF 直接读取，保证 canonical 页面几何与源页一致。
    """
    try:
        import fitz
    except ImportError as error:
        raise WorkflowError("本地解析需要 PyMuPDF 读取页面几何；请从 Literature 启动器重新安装。") from error
    document = fitz.open(slice_pdf)
    try:
        info = []
        for index in range(document.page_count):
            page = document[index]
            info.append({
                "page_idx": index,
                "page_size": [float(page.rect.width), float(page.rect.height)],
                "rotation": int(page.rotation or 0),
            })
    finally:
        document.close()
    _atomic_json(layout, {"pdf_info": info})


def _extract_kit_zip(archive: Path, destination: Path) -> None:
    """安全解压 kit 结果 ZIP（路径穿越与解压体积校验），幂等覆盖。"""
    temporary = destination.with_name(destination.name + ".extracting").resolve()
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as bundle:
            expanded = 0
            for member in bundle.infolist():
                target = (temporary / member.filename).resolve()
                if target != temporary and temporary not in target.parents:
                    raise WorkflowError("本地引擎 ZIP 包含不安全的路径。")
                expanded += max(0, member.file_size)
                if expanded > DEFAULT_MAX_EXTRACTED_BYTES:
                    raise WorkflowError("本地引擎 ZIP 解压后超过安全上限。")
            bundle.extractall(temporary)
        if not (temporary / "structured_content.json").is_file():
            raise WorkflowError("本地引擎 ZIP 缺少 structured_content.json，输出不完整。")
        if destination.exists():
            shutil.rmtree(destination)
        temporary.replace(destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
