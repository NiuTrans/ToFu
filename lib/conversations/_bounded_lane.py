"""Bounded, coalescing process-local lanes for reconstructible refresh work.

Entry point: :class:`BoundedCoalescingLane`.  Project status, watch, and
conversation-summary owners use it for best-effort refreshes whose durable
source can always be read again.  The lane bounds unique queued scopes,
coalesces updates that target an active scope, retires idle consumers, and
exposes saturation/resource metrics.
"""

from __future__ import annotations

import os
import queue
import threading
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

from lib.log import get_logger
from runtime_guards import resolve_resource_budget


Key = TypeVar('Key', bound=Hashable)
Payload = TypeVar('Payload')
_MISSING = object()
logger = get_logger(__name__)


def _default_idle_seconds() -> float:
    # The shared resource resolver treats zero as a malformed queue capacity;
    # for a lifecycle timer it is an explicit opt-out instead.
    if os.environ.get('TOFU_PROJECT_REFRESH_IDLE_SECONDS', '').strip() == '0':
        return 0.0
    return float(resolve_resource_budget(
        'TOFU_PROJECT_REFRESH_IDLE_SECONDS', maximum=86_400))


_DEFAULT_IDLE_SECONDS = _default_idle_seconds()


class BoundedCoalescingLane(Generic[Key, Payload]):
    """Run at most ``workers`` consumers with a finite pending-scope queue.

    ``capacity`` bounds keys waiting behind workers.  At most
    ``capacity + workers`` unique scopes are resident because an active key
    stays tracked until all updates submitted during its run are consumed.
    """

    def __init__(
        self,
        *,
        name: str,
        workers: int,
        capacity: int,
        merge: Callable[[Payload, Payload], Payload],
        consume: Callable[[Key, Payload], None],
        on_error: Callable[[Key, Exception], None] | None = None,
        idle_seconds: float | None = None,
    ) -> None:
        self.name = str(name)
        self.workers = max(1, int(workers))
        self.capacity = max(1, int(capacity))
        self._merge = merge
        self._consume = consume
        self._on_error = on_error
        self.idle_seconds = max(0.0, float(
            _DEFAULT_IDLE_SECONDS if idle_seconds is None else idle_seconds))
        self._queue: queue.Queue[Key] = queue.Queue(maxsize=self.capacity)
        self._condition = threading.Condition()
        self._pending: dict[Key, Payload] = {}
        self._tracked: set[Key] = set()
        self._live_workers = 0
        self._worker_serial = 0
        self._worker_starts = 0
        self._retired_workers = 0
        self._accepted = 0
        self._coalesced = 0
        self._rejected = 0
        self._peak_scopes = 0

    def submit(self, key: Key, payload: Payload) -> bool:
        """Accept or coalesce one scope without ever blocking the caller."""
        with self._condition:
            self._start_locked()
            if key in self._tracked:
                current = self._pending.get(key, _MISSING)
                self._pending[key] = (
                    payload if current is _MISSING
                    else self._merge(current, payload)  # type: ignore[arg-type]
                )
                self._coalesced += 1
                self._condition.notify_all()
                return True

            self._pending[key] = payload
            self._tracked.add(key)
            try:
                self._queue.put_nowait(key)
            except queue.Full:
                self._pending.pop(key, None)
                self._tracked.discard(key)
                self._rejected += 1
                rejected = self._rejected
                if rejected & (rejected - 1) == 0:
                    logger.warning(
                        '[%s] refresh lane saturated capacity=%d '
                        'rejected_total=%d',
                        self.name, self.capacity, rejected,
                    )
                return False
            self._accepted += 1
            self._peak_scopes = max(self._peak_scopes, len(self._tracked))
            self._condition.notify_all()
            return True

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait for all accepted scopes; diagnostics and clean shutdown only."""
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._tracked,
                timeout=max(0.0, float(timeout)),
            )

    def snapshot(self) -> dict[str, float | int | str]:
        """Return bounded-lane counters without exposing mutable internals."""
        with self._condition:
            return {
                'name': self.name,
                'workers': self.workers,
                'capacity': self.capacity,
                'idleSeconds': self.idle_seconds,
                'liveWorkers': self._live_workers,
                'workerStarts': self._worker_starts,
                'retiredWorkers': self._retired_workers,
                'queued': self._queue.qsize(),
                'trackedScopes': len(self._tracked),
                'pendingScopes': len(self._pending),
                'peakScopes': self._peak_scopes,
                'accepted': self._accepted,
                'coalesced': self._coalesced,
                'rejected': self._rejected,
            }

    def _start_locked(self) -> None:
        while self._live_workers < self.workers:
            self._worker_serial += 1
            worker_number = self._worker_serial
            self._live_workers += 1
            try:
                threading.Thread(
                    target=self._worker,
                    name=f'{self.name}-{worker_number}',
                    daemon=True,
                ).start()
            except BaseException:
                self._live_workers -= 1
                raise
            self._worker_starts += 1

    def _worker(self) -> None:
        exit_counted = False
        try:
            while True:
                try:
                    if self.idle_seconds > 0:
                        key = self._queue.get(timeout=self.idle_seconds)
                    else:
                        key = self._queue.get()
                except queue.Empty:
                    with self._condition:
                        # submit() holds this same condition while enqueueing.
                        # Whichever side wins the lock therefore either leaves
                        # a live consumer or observes the decremented count and
                        # starts a replacement; accepted work cannot strand.
                        if not self._queue.empty():
                            continue
                        self._live_workers -= 1
                        self._retired_workers += 1
                        exit_counted = True
                        self._condition.notify_all()
                        return
                try:
                    while True:
                        with self._condition:
                            payload = self._pending.pop(key, _MISSING)
                            if payload is _MISSING:
                                self._tracked.discard(key)
                                self._condition.notify_all()
                                break
                        try:
                            self._consume(key, payload)  # type: ignore[arg-type]
                        except Exception as exc:
                            if self._on_error is not None:
                                self._on_error(key, exc)
                            else:
                                logger.exception(
                                    '[%s] refresh consumer failed key=%r',
                                    self.name, key,
                                )
                finally:
                    self._queue.task_done()
        finally:
            if not exit_counted:
                with self._condition:
                    self._live_workers = max(0, self._live_workers - 1)
                    self._condition.notify_all()
