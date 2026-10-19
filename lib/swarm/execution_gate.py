"""Owner-addressed accounting for demand-driven SubAgent execution.

No permit or waiter queue decides whether authorized child work can start.
The existing scheduler owns dependencies and worker lifetime; this module
tracks active execution by explicit owner and checks cancellation at entry.
"""
from __future__ import annotations
from collections.abc import Callable, Hashable
import threading
from lib.llm.llm_errors import AbortedError


class SwarmExecutionAccounting:
    def __init__(self, *, capacity: int = 0, waiter_capacity: int = 0) -> None:
        self.capacity = 0
        self.waiter_capacity = 0
        self._lock = threading.RLock()
        self._active_by_owner: dict[Hashable, int] = {}
        self._active = self._accepted = self._cancelled = self._peak_active = 0

    @staticmethod
    def _validate_owner(owner_key: Hashable) -> None:
        if owner_key is None or isinstance(owner_key, bool):
            raise ValueError('swarm execution owner must be explicit')
        try:
            hash(owner_key)
        except TypeError as exc:
            raise ValueError('swarm execution owner must be hashable') from exc

    def acquire(self, owner_key: Hashable, *,
                abort_check: Callable[[], bool] | None = None) -> None:
        self._validate_owner(owner_key)
        if abort_check is not None and abort_check():
            with self._lock:
                self._cancelled += 1
            raise AbortedError('swarm execution cancelled before entry')
        with self._lock:
            self._active += 1
            self._accepted += 1
            self._active_by_owner[owner_key] = self._active_by_owner.get(owner_key, 0) + 1
            self._peak_active = max(self._peak_active, self._active)

    def release(self, owner_key: Hashable) -> None:
        self._validate_owner(owner_key)
        with self._lock:
            count = self._active_by_owner.get(owner_key, 0)
            if not count:
                raise RuntimeError('swarm execution released by non-owner')
            if count == 1:
                del self._active_by_owner[owner_key]
            else:
                self._active_by_owner[owner_key] = count - 1
            self._active -= 1

    def resize(self, capacity: int) -> None:
        """Old feedback must not restore an execution admission ceiling."""
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError('swarm capacity must be a positive integer')

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {'capacity': 0, 'waiterCapacity': 0,
                    'active': self._active, 'activeOwners': len(self._active_by_owner),
                    'waiting': 0, 'waitingOwners': 0, 'accepted': self._accepted,
                    'rejected': 0, 'cancelled': self._cancelled,
                    'peakActive': self._peak_active, 'peakWaiting': 0}


_PROCESS_SWARM_EXECUTION_GATE = SwarmExecutionAccounting()


def process_swarm_execution_gate() -> SwarmExecutionAccounting:
    from lib.resource_control.adapters import bind_swarm
    bind_swarm(_PROCESS_SWARM_EXECUTION_GATE)
    return _PROCESS_SWARM_EXECUTION_GATE


__all__ = ['SwarmExecutionAccounting', 'process_swarm_execution_gate']
