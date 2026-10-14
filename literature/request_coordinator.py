"""跨模块共用的模型请求协调层：服务级并发/退避冷却与项目预算预留。

设计要点：

- **服务级并发**：按 ``service_id`` 限制同时在途请求数，避免同一模型服务被多本书/多角色
  叠加打满；429/5xx 时记录冷却截止时间，冷却期内新请求等待。
- **项目预算预留（inflight）**：请求发出前原子 ``reserve``，把估算费用计入在途；请求完成、
  用量写账后再 ``release``，因此“已花 + 在途”不会超过预算，也不会出现完成请求先 release
  再等主线程晚写 usage 的重复花钱窗口。
- **未知费用不冒充零成本**：无 usage / 无单价 / 超时的请求计入 ``unknown_cost_requests``，
  预算不足以硬性拦截时明确表达“费用未知”，而不是当成 0 继续花。

所有协调都通过显式锁与信号量完成，不依赖调用顺序；本模块不绑定具体 provider 实现。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator


class BudgetExhausted(RuntimeError):
    """项目预算（含在途预留）不足以发出下一个请求。"""


def prompt_cost_estimator(config: Any) -> "Callable[[int], float]":
    """根据 provider 单价估算一次请求的费用（价格未知时返回 0，不作硬预算）。"""
    def estimate(prompt_chars: int) -> float:
        if config.input_price_per_million is None or config.output_price_per_million is None:
            return 0.0
        input_tokens = prompt_chars // 2 + 200
        output_tokens = prompt_chars // 2
        return (input_tokens * config.input_price_per_million + output_tokens * config.output_price_per_million) / 1_000_000
    return estimate


@dataclass
class Reservation:
    project_id: str
    cost: float
    service_id: str = ""
    released: bool = False


class RequestCoordinator:
    def __init__(
        self, *, default_service_limit: int = 4, cooldown_seconds: float = 5.0,
        spent_loader: "Callable[[str], float] | None" = None,
    ) -> None:
        self.default_service_limit = max(1, int(default_service_limit))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        # 已花金额由协调层自己维护（进程内唯一来源），首次用时从持久账本 seed 一次；
        # 不能再由各 TranslationService 缓存自己的一份，否则 reserve 会拿到过期标量。
        self._spent_loader = spent_loader
        self._lock = threading.Lock()
        self._service_limits: dict[str, int] = {}
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._cooldown_until: dict[str, float] = {}
        self._spent: dict[str, float] = {}
        self._reserved: dict[str, float] = defaultdict(float)
        self._unknown_cost_requests: dict[str, int] = defaultdict(int)
    # ---- 服务级并发与冷却 ------------------------------------------------ #

    def set_service_limit(self, service_id: str, limit: int) -> None:
        with self._lock:
            self._service_limits[service_id] = max(1, int(limit))
            self._semaphores.pop(service_id, None)  # 下次按新上限重建

    def service_limit(self, service_id: str) -> int:
        with self._lock:
            return int(self._service_limits.get(service_id, self.default_service_limit))

    def raise_service_limit(self, service_id: str, limit: int) -> int:
        """只在需要更大额度时上调服务级并发上限；只升不降，避免压到运行中作业的额度。"""
        limit = max(1, int(limit))
        with self._lock:
            current = int(self._service_limits.get(service_id, self.default_service_limit))
            if limit <= current:
                return current
            self._service_limits[service_id] = limit
            self._semaphores.pop(service_id, None)  # 下次按新上限重建
            return limit

    def _semaphore(self, service_id: str) -> threading.BoundedSemaphore:
        with self._lock:
            semaphore = self._semaphores.get(service_id)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(self._service_limits.get(service_id, self.default_service_limit))
                self._semaphores[service_id] = semaphore
            return semaphore

    def note_rate_limit(self, service_id: str, seconds: float | None = None) -> None:
        """收到 429/5xx 时调用：在冷却期内让同服务的新请求先等待。"""
        wait = self.cooldown_seconds if seconds is None else max(0.0, float(seconds))
        with self._lock:
            self._cooldown_until[service_id] = max(self._cooldown_until.get(service_id, 0.0), time.monotonic() + wait)

    @contextmanager
    def limit(self, service_id: str) -> Iterator[None]:
        while True:
            with self._lock:
                until = self._cooldown_until.get(service_id, 0.0)
            remaining = until - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 0.25))
                continue
            break
        semaphore = self._semaphore(service_id)
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    def acquire_service(self, service_id: str) -> threading.BoundedSemaphore:
        """等待冷却并占用一个服务级并发额度；返回信号量供 release_service。"""
        while True:
            with self._lock:
                until = self._cooldown_until.get(service_id, 0.0)
            remaining = until - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 0.25))
                continue
            break
        semaphore = self._semaphore(service_id)
        semaphore.acquire()
        return semaphore

    def release_service(self, semaphore: threading.BoundedSemaphore | None) -> None:
        if semaphore is None:
            return
        try:
            semaphore.release()
        except ValueError:
            pass

    def attempt_hooks_for(
        self, service_id: str, provider: Any, *,
        project_id: str = "", budget: float = 0.0, cost_of: "Callable[[int], float] | None" = None,
    ) -> None:
        """把服务级并发/冷却 + 项目预算（预算可在每次 attempt 预留）挂到真实 provider。"""
        def begin(prompt_chars: int = 0) -> Any:
            semaphore = self.acquire_service(service_id)
            reservation = None
            if project_id and budget > 0:
                cost = cost_of(prompt_chars) if cost_of is not None else 0.0
                reservation = self.reserve(project_id, cost=cost, budget=budget)
                if reservation is None:
                    self.release_service(semaphore)
                    raise BudgetExhausted("项目预算不足，已停止提交新的模型请求")
            return (semaphore, reservation)

        def end(handle: Any, response: Any, error: Exception | None, final: bool = True) -> None:
            semaphore, reservation = handle if isinstance(handle, tuple) else (handle, None)
            status = getattr(response, "status_code", None)
            if status in (429, 500, 502, 503, 504):
                self.note_rate_limit(service_id)
            if final:
                # final attempt：预算预留不在 end 释放，留给调用方写账后原子 reconcile，
                # 避免“end 释放→caller commit”的重复花钱窗口；只释放服务额度。
                self.release_service(semaphore)
                if reservation is not None:
                    try:
                        provider._attempt_state.handle = reservation
                    except Exception:
                        self.release(reservation)
            else:
                # 会被重试的 attempt（429/5xx/连接错误）：本次无实际费用，释放预留与额度。
                self.release(reservation)
                self.release_service(semaphore)

        try:
            if not getattr(provider, "supports_attempt_hooks", False):
                return
            provider.attempt_hooks = (begin, end)
            provider._hooks_include_budget = bool(project_id and budget > 0)
        except Exception:
            pass

    def commit(self, project_id: str, actual_cost: float | None) -> None:
        """成功拿到 usage 后，把实际费用计入已花（未知则计 unknown）。"""
        with self._lock:
            if actual_cost is None:
                self._unknown_cost_requests[project_id] += 1
            else:
                base = self._spent_for(project_id)
                self._spent[project_id] = (base if base is not None else 0.0) + float(actual_cost)

    # ---- 项目预算预留（单一一致性边界） --------------------------------- #

    def _spent_for(self, project_id: str) -> float | None:
        if project_id not in self._spent:
            try:
                self._spent[project_id] = float(self._spent_loader(project_id)) if self._spent_loader else 0.0
            except Exception:
                # 账本不可读：不缓存 0（否则会 seg0 继续花），返回 None 让调用方保守拒绝。
                return None
        return self._spent[project_id]

    def reserve(self, project_id: str, *, cost: float, budget: float) -> Reservation | None:
        """在锁内一次完成“取最新已花 + 预留 + 判定”，返回 None 表示预算不足/不可判定。"""
        amount = max(0.0, float(cost or 0.0))
        with self._lock:
            spent = self._spent_for(project_id)
            if spent is None:
                if budget > 0:
                    return None  # 账本不可读：不当作可花额度
                spent = 0.0
            if budget > 0 and (spent >= budget or spent + self._reserved[project_id] + amount > budget):
                return None
            self._reserved[project_id] += amount
        return Reservation(project_id=project_id, cost=amount)

    def reconcile(self, reservation: Reservation | None, *, actual_cost: float | None) -> None:
        """响应后对账：在锁内释放预留并累计已花（实际未知则计 unknown，不当作 0）。"""
        if reservation is None or reservation.released:
            return
        with self._lock:
            self._reserved[reservation.project_id] = max(0.0, self._reserved[reservation.project_id] - reservation.cost)
            if actual_cost is None:
                self._unknown_cost_requests[reservation.project_id] += 1
            else:
                base = self._spent_for(reservation.project_id)
                self._spent[reservation.project_id] = (base if base is not None else 0.0) + float(actual_cost)
            reservation.released = True

    def release(self, reservation: Reservation | None) -> None:
        """安全网：仅释放预留，不改已花/unknown（已 reconcile 过的为幂等 no-op）。"""
        if reservation is None or reservation.released:
            return
        with self._lock:
            self._reserved[reservation.project_id] = max(0.0, self._reserved[reservation.project_id] - reservation.cost)
            reservation.released = True

    def note_unknown_cost(self, project_id: str) -> None:
        """无 usage / 无单价 / 超时：明确记录为“费用未知”，不当作零消耗。"""
        with self._lock:
            self._unknown_cost_requests[project_id] += 1

    def reserved(self, project_id: str) -> float:
        with self._lock:
            return self._reserved[project_id]

    def unknown_cost_requests(self, project_id: str) -> int:
        with self._lock:
            return self._unknown_cost_requests[project_id]

    def snapshot(self, project_id: str) -> dict[str, Any]:
        with self._lock:
            return {
                "project_id": project_id,
                "spent_cost": self._spent_for(project_id),
                "reserved_cost": self._reserved[project_id],
                "unknown_cost_requests": self._unknown_cost_requests[project_id],
            }
