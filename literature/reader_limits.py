"""AIFold-specific service concurrency, shared by all books in this process."""
from __future__ import annotations
import threading
import time
from .providers import ProviderError


class FoldRequestGate:
    def __init__(self):
        self._condition = threading.Condition()
        self._active: dict[str, int] = {}
        self._limited_until: dict[str, float] = {}

    def acquire(self, service_id: str, timeout: float = 180.0) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                limit = 1 if self._limited_until.get(service_id, 0) > time.monotonic() else 2
                if self._active.get(service_id, 0) < limit:
                    self._active[service_id] = self._active.get(service_id, 0) + 1
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError('等待折叠请求名额超时，已保留完成内容，可继续处理。', possible_charge=False)
                self._condition.wait(min(remaining, 1.0))

    def release(self, service_id: str, status: int = 0) -> None:
        with self._condition:
            self._active[service_id] = max(0, self._active.get(service_id, 0) - 1)
            if status == 429:
                self._limited_until[service_id] = time.monotonic() + 60.0
            self._condition.notify_all()
