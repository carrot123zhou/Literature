"""MinerU 解析后端契约：把「解析方式」与工作流解耦。

现状与边界：

- ``mineru_remote``：当前默认使用的解析方式，走 MinerU 精准解析 API
  （切片 → 预签名上传 → 远端轮询 → 下载校验 → 封存 → 规范化）。
- ``mineru_local``：本地 MinerU 引擎（``literature/mineru_local.py``）。能力
  按引擎真实安装状态动态计算：引擎就绪时可用（真取消、断点续跑），未安装
  时明确不可用并给出安装引导，不伪装成可点击功能。

未来本地解析应作为独立进程/服务接入：它在自己的环境里准备输入、执行解析、报告状态、
产出结果目录；Literature 通过同一 ``prepare/execute/status/result`` 边界拿到
「可封存的 raw 结果 + source_pages 映射」，再走 :mod:`literature.mineru` 的同一套
normalize/merge，绝不把远端上传 URL、轮询等 remote 细节强加给本地实现。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

BackendExecution = Literal["remote", "local"]

REMOTE_BACKEND_ID = "mineru_remote"
LOCAL_BACKEND_ID = "mineru_local"

# 与 MinerU 官方精准解析 API 对齐的本地文件类型（见 docs/PROJECT_PLAN.md §2.2）。
_BACKEND_FORMATS: tuple[str, ...] = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".jp2", ".webp", ".gif", ".bmp", ".html",
)


@dataclass(frozen=True)
class ParseBackendCapabilities:
    """一个解析后端的能力与执行方式描述。字段区分 remote/local 的真实差异。"""

    backend_id: str
    display_name: str
    version: str
    execution: BackendExecution
    supported_formats: tuple[str, ...]
    # 是否由 Literature 本地切分（本地子集 PDF / 切片）并维护 source_pages 映射。
    slicing: bool
    # 是否需要轮询远端任务状态；本地执行可为 False。
    polling: bool
    # 是否需要预签名上传地址与 multipart 上传；本地执行不需要。
    upload_url: bool
    # 远端无法真正取消/暂停；本地执行未来可支持真正取消。
    remote_job_control: bool
    # 中断后能否从已下载/已完成切片继续。
    resumable: bool
    available: bool
    unavailable_reason: str = ""


_REMOTE = ParseBackendCapabilities(
    backend_id=REMOTE_BACKEND_ID,
    display_name="MinerU 精准解析（远端 API）",
    version="v1",
    execution="remote",
    supported_formats=_BACKEND_FORMATS,
    slicing=True,
    polling=True,
    upload_url=True,
    remote_job_control=False,
    resumable=True,
    available=True,
)

_LOCAL = ParseBackendCapabilities(
    backend_id=LOCAL_BACKEND_ID,
    display_name="本地 MinerU 引擎",
    version="unavailable",
    execution="local",
    supported_formats=_BACKEND_FORMATS,
    # 本地实现可以自己决定是否切片，但不应被要求实现远端上传/轮询。
    slicing=True,
    polling=False,
    upload_url=False,
    remote_job_control=True,
    resumable=True,
    available=False,
    unavailable_reason=(
        "本地 MinerU 引擎尚未就绪；可在设置 → 文档解析里下载安装。"
    ),
)

_BACKENDS: dict[str, ParseBackendCapabilities] = {
    _REMOTE.backend_id: _REMOTE,
    _LOCAL.backend_id: _LOCAL,
}


class ParseBackendUnavailable(RuntimeError):
    """请求了一个本轮不可用的解析后端（例如尚未接入的本地 MinerU）。"""


@dataclass(frozen=True)
class ParseResult:
    """统一的可封存解析结果：raw 结果目录 + 源页映射。

    远端与未来的本地后端都返回这个结构；seal/normalize/merge 由工作流协调层统一执行，
    后端不需要了解 canonical 图谱，也不被要求实现远端专属的上传/轮询细节。
    """

    backend_id: str
    job_id: str
    state: str
    raw_directories: tuple[str, ...]
    # raw 结果目录 -> 对应的源文件页号（1-based）；空元组表示后端未提供映射。
    source_pages: Mapping[str, tuple[int, ...]]


def _local_capabilities() -> ParseBackendCapabilities:
    """本地后端能力动态读取引擎真实安装状态；引擎模块缺失时保持预留不可用。"""
    try:
        from .mineru_local import local_capabilities
    except Exception:  # 引擎模块不可用（异常构建）：保持预留边界，不伪装可用。
        return _LOCAL
    try:
        return local_capabilities()
    except Exception:
        return _LOCAL


def describe_backends() -> list[dict[str, Any]]:
    """返回全部解析后端的描述，供 API/前端只读展示与能力探测。"""
    return [asdict(_local_capabilities() if backend_id == LOCAL_BACKEND_ID else capabilities_map)
            for backend_id, capabilities_map in _BACKENDS.items()]


def capabilities(backend_id: str) -> ParseBackendCapabilities:
    key = (backend_id or REMOTE_BACKEND_ID).strip() or REMOTE_BACKEND_ID
    if key == LOCAL_BACKEND_ID:
        return _local_capabilities()
    try:
        return _BACKENDS[key]
    except KeyError as error:
        raise ParseBackendUnavailable(f"未知的解析后端：{backend_id}") from error


def resolve_job_backend(backend_id: str | None) -> ParseBackendCapabilities:
    """解析任务里持久化的后端标识；旧任务缺字段时默认 remote（历史行为）。"""
    return capabilities(backend_id or REMOTE_BACKEND_ID)


def ensure_available(backend_id: str | None) -> ParseBackendCapabilities:
    result = resolve_job_backend(backend_id)
    if not result.available:
        raise ParseBackendUnavailable(result.unavailable_reason or f"解析后端不可用：{result.backend_id}")
    return result
