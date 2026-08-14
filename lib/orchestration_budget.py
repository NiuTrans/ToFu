"""Shared, thread-safe agent budget for one orchestration run tree."""

from __future__ import annotations

import threading
from typing import Any


class OrchestrationAgentBudget:
    """Atomically cap agent starts across parent, parallel and nested flows."""

    def __init__(self, limit: int, *, lock: Any | None = None) -> None:
        self._limit = max(1, int(limit))
        self._used = 0
        self._lock = lock or threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def claim(self) -> bool:
        """Reserve one agent start, returning false without overshooting."""
        with self._lock:
            if self._used >= self._limit:
                return False
            self._used += 1
            return True

    def used(self) -> int:
        with self._lock:
            return self._used

    def remaining(self) -> int:
        with self._lock:
            return self._limit - self._used


__all__ = ['OrchestrationAgentBudget']
