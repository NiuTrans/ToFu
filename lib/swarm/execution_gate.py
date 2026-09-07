"""Process-wide owner-fair admission for expensive SubAgent execution.

Each live swarm keeps its own dependency scheduler, but every actual agent run
crosses this one gate. This prevents several background conversations from
multiplying provider calls and resident agent state beyond the launch-probed
process budget. Owner identity is explicit so a future multi-user deployment
does not have to unwind a single-user global semaphore.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Hashable
import threading

from lib.llm_errors import AbortedError
from lib.swarm.resource_policy import (
    swarm_global_workers,
    swarm_max_parallel,
    swarm_session_capacity,
)


class SwarmExecutionQueueFull(RuntimeError):
    """The bounded process-wide swarm waiter allowance is exhausted."""


class OwnerFairExecutionGate:
    """Round-robin owner admission with finite active and waiting counts."""

    def __init__(self, *, capacity: int, waiter_capacity: int) -> None:
        if capacity <= 0 or waiter_capacity <= 0:
            raise ValueError('swarm execution capacities must be positive')
        self.capacity = int(capacity)
        self.waiter_capacity = int(waiter_capacity)
        self._condition = threading.Condition(threading.RLock())
        self._pending_by_owner: dict[Hashable, deque[object]] = {}
        self._owner_cycle: deque[Hashable] = deque()
        self._waiting = 0
        self._active = 0
        self._active_by_owner: dict[Hashable, int] = {}
        self._accepted = 0
        self._rejected = 0
        self._cancelled = 0
        self._peak_active = 0
        self._peak_waiting = 0

    @staticmethod
    def _validate_owner(owner_key: Hashable) -> None:
        if owner_key is None or isinstance(owner_key, bool):
            raise ValueError('swarm execution owner must be explicit')
        try:
            hash(owner_key)
        except TypeError as exc:
            raise ValueError('swarm execution owner must be hashable') from exc

    def acquire(
        self,
        owner_key: Hashable,
        *,
        abort_check: Callable[[], bool] | None = None,
    ) -> None:
        """Wait for one owner-fair permit, observing cooperative aborts."""
        self._validate_owner(owner_key)
        ticket = object()
        with self._condition:
            if self._waiting >= self.waiter_capacity:
                self._rejected += 1
                raise SwarmExecutionQueueFull(
                    'process swarm execution queue is full')
            queue = self._pending_by_owner.get(owner_key)
            if queue is None:
                queue = deque()
                self._pending_by_owner[owner_key] = queue
                self._owner_cycle.append(owner_key)
            queue.append(ticket)
            self._waiting += 1
            self._accepted += 1
            self._peak_waiting = max(self._peak_waiting, self._waiting)

            while True:
                if abort_check is not None and abort_check():
                    self._remove_waiter_locked(owner_key, ticket)
                    self._cancelled += 1
                    self._condition.notify_all()
                    raise AbortedError(
                        'swarm execution cancelled before admission')
                owner_queue = self._pending_by_owner.get(owner_key)
                owner_turn = bool(
                    self._owner_cycle and self._owner_cycle[0] == owner_key)
                ticket_turn = bool(owner_queue and owner_queue[0] is ticket)
                if self._active < self.capacity and owner_turn and ticket_turn:
                    owner_queue.popleft()
                    self._waiting -= 1
                    if owner_queue:
                        self._owner_cycle.rotate(-1)
                    else:
                        self._pending_by_owner.pop(owner_key, None)
                        self._owner_cycle.popleft()
                    self._active += 1
                    self._active_by_owner[owner_key] = (
                        self._active_by_owner.get(owner_key, 0) + 1)
                    self._peak_active = max(self._peak_active, self._active)
                    self._condition.notify_all()
                    return
                self._condition.wait(timeout=0.25 if abort_check else None)

    def _remove_waiter_locked(self, owner_key: Hashable, ticket: object) -> None:
        queue = self._pending_by_owner.get(owner_key)
        if queue is None:
            return
        try:
            queue.remove(ticket)
        except ValueError:
            return
        self._waiting = max(0, self._waiting - 1)
        if not queue:
            self._pending_by_owner.pop(owner_key, None)
            try:
                self._owner_cycle.remove(owner_key)
            except ValueError:
                pass

    def release(self, owner_key: Hashable) -> None:
        """Release one permit held by ``owner_key``."""
        self._validate_owner(owner_key)
        with self._condition:
            owner_active = self._active_by_owner.get(owner_key, 0)
            if owner_active <= 0:
                raise RuntimeError('swarm execution permit released by non-owner')
            if owner_active == 1:
                self._active_by_owner.pop(owner_key, None)
            else:
                self._active_by_owner[owner_key] = owner_active - 1
            self._active = max(0, self._active - 1)
            self._condition.notify_all()

    def snapshot(self) -> dict[str, int]:
        """Return low-cardinality capacity and lifecycle evidence."""
        with self._condition:
            return {
                'capacity': self.capacity,
                'waiterCapacity': self.waiter_capacity,
                'active': self._active,
                'activeOwners': len(self._active_by_owner),
                'waiting': self._waiting,
                'waitingOwners': len(self._pending_by_owner),
                'accepted': self._accepted,
                'rejected': self._rejected,
                'cancelled': self._cancelled,
                'peakActive': self._peak_active,
                'peakWaiting': self._peak_waiting,
            }


_PROCESS_SWARM_EXECUTION_GATE = OwnerFairExecutionGate(
    capacity=swarm_global_workers(),
    waiter_capacity=min(
        512,
        swarm_session_capacity() * swarm_max_parallel(),
    ),
)


def process_swarm_execution_gate() -> OwnerFairExecutionGate:
    """Return the process singleton used by every live swarm session."""
    return _PROCESS_SWARM_EXECUTION_GATE


__all__ = [
    'OwnerFairExecutionGate',
    'SwarmExecutionQueueFull',
    'process_swarm_execution_gate',
]
