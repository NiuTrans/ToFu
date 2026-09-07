"""Bounded owner-fair execution for durable knowledge enrichment work.

Responsibility
--------------
Retain only owner identifiers in a finite process-local scheduler.  Each turn
invokes a domain callback for at most one durable asset, then places that owner
at the back of the queue when more work may remain.  Asset bytes and lifecycle
authority stay in the knowledge repository.

Entry points
------------
``schedule`` admits or revives one explicit owner; ``stop`` cancels queued work
or signals an active callback; ``snapshot`` exposes low-cardinality budget
evidence.  Worker threads start lazily and retire after the configured idle
window.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import threading
import time

from lib.log import get_logger
from lib.observability import (
    observe_executor_queue_wait,
    publish_executor_state,
    record_executor_idle_retirement,
    record_executor_rejection,
)


logger = get_logger(__name__)


class KnowledgeEnrichmentCapacityExceeded(RuntimeError):
    """The finite process-local owner allowance has been exhausted."""


@dataclass(slots=True)
class _OwnerState:
    stop_event: threading.Event
    queued_at: float
    reschedule_requested: bool = False


class OwnerFairEnrichmentLane:
    """Run one durable asset per owner turn with finite worker residency."""

    def __init__(
        self,
        *,
        max_workers: int,
        owner_capacity: int,
        idle_seconds: float,
        processor: Callable[[int, threading.Event], bool],
    ) -> None:
        if max_workers <= 0:
            raise ValueError('max_workers must be positive')
        if owner_capacity < max_workers:
            raise ValueError('owner_capacity cannot be smaller than max_workers')
        if idle_seconds < 0:
            raise ValueError('idle_seconds cannot be negative')
        if not callable(processor):
            raise TypeError('processor must be callable')

        self.max_workers = int(max_workers)
        self.owner_capacity = int(owner_capacity)
        self.idle_seconds = float(idle_seconds)
        self._processor = processor
        self._condition = threading.Condition(threading.RLock())
        self._pending: deque[int] = deque()
        self._pending_set: set[int] = set()
        self._active: set[int] = set()
        self._states: dict[int, _OwnerState] = {}
        self._threads: set[threading.Thread] = set()
        self._thread_serial = 0
        self._halt_workers = False
        self._accepted = 0
        self._rejected = 0
        self._cancelled = 0
        self._completed_turns = 0
        self._failed_turns = 0
        self._retired_threads = 0
        self._peak_owners = 0
        self._publish_locked()

    def schedule(self, owner_user_id: int) -> bool:
        """Admit one owner or revive an in-flight owner after a stop race."""
        if isinstance(owner_user_id, bool):
            raise ValueError('owner_user_id must be a positive integer')
        owner_id = int(owner_user_id)
        if owner_id <= 0:
            raise ValueError('owner_user_id must be a positive integer')

        with self._condition:
            self._halt_workers = False
            state = self._states.get(owner_id)
            if state is not None:
                if owner_id in self._active and state.stop_event.is_set():
                    state.stop_event.clear()
                    state.reschedule_requested = True
                    self._condition.notify_all()
                    return True
                return False
            if len(self._states) >= self.owner_capacity:
                self._rejected += 1
                record_executor_rejection(
                    'knowledge-enrichment', reason='owner_capacity')
                self._publish_locked()
                raise KnowledgeEnrichmentCapacityExceeded(
                    'knowledge enrichment owner capacity is full '
                    f'({len(self._states)}/{self.owner_capacity})')

            state = _OwnerState(
                stop_event=threading.Event(), queued_at=time.monotonic())
            self._states[owner_id] = state
            self._pending.append(owner_id)
            self._pending_set.add(owner_id)
            self._accepted += 1
            self._peak_owners = max(self._peak_owners, len(self._states))
            try:
                self._ensure_workers_locked()
            except BaseException:
                self._remove_pending_locked(owner_id)
                self._states.pop(owner_id, None)
                self._accepted = max(0, self._accepted - 1)
                self._rejected += 1
                record_executor_rejection(
                    'knowledge-enrichment', reason='worker_start_failed')
                self._publish_locked()
                self._condition.notify_all()
                raise
            self._publish_locked()
            self._condition.notify_all()
            return True

    def stop(
        self,
        *,
        owner_user_id: int | None = None,
        timeout: float = 2.0,
    ) -> bool:
        """Stop one owner, or halt and drain all shared workers on shutdown."""
        try:
            wait_seconds = max(0.0, float(timeout))
        except (TypeError, ValueError, OverflowError):
            wait_seconds = 2.0
        deadline = time.monotonic() + wait_seconds

        if owner_user_id is None:
            with self._condition:
                self._halt_workers = True
                for state in self._states.values():
                    state.stop_event.set()
                    state.reschedule_requested = False
                for owner_id in tuple(self._pending):
                    self._remove_pending_locked(owner_id)
                    self._states.pop(owner_id, None)
                    self._cancelled += 1
                threads = tuple(self._threads)
                self._publish_locked()
                self._condition.notify_all()
            current = threading.current_thread()
            for thread in threads:
                if thread is current:
                    continue
                remaining = max(0.0, deadline - time.monotonic())
                thread.join(remaining)
            with self._condition:
                stopped = not self._threads
                if stopped:
                    self._halt_workers = False
                self._publish_locked()
                return stopped

        if isinstance(owner_user_id, bool):
            raise ValueError('owner_user_id must be a positive integer')
        owner_id = int(owner_user_id)
        with self._condition:
            state = self._states.get(owner_id)
            if state is None:
                return True
            state.stop_event.set()
            state.reschedule_requested = False
            if self._remove_pending_locked(owner_id):
                self._states.pop(owner_id, None)
                self._cancelled += 1
                self._publish_locked()
                self._condition.notify_all()
                return True
            while owner_id in self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return owner_id not in self._states

    def snapshot(self) -> dict[str, int | float | bool]:
        """Return finite scheduler counters without owner identifiers."""
        with self._condition:
            return {
                'workers': self.max_workers,
                'ownerCapacity': self.owner_capacity,
                'idleSeconds': self.idle_seconds,
                'active': len(self._active),
                'queued': len(self._pending),
                'retainedOwners': len(self._states),
                'residentThreads': len(self._threads),
                'accepted': self._accepted,
                'rejected': self._rejected,
                'cancelled': self._cancelled,
                'completedTurns': self._completed_turns,
                'failedTurns': self._failed_turns,
                'retiredThreads': self._retired_threads,
                'peakOwners': self._peak_owners,
                'stopping': self._halt_workers,
            }

    def _remove_pending_locked(self, owner_id: int) -> bool:
        if owner_id not in self._pending_set:
            return False
        self._pending_set.remove(owner_id)
        try:
            self._pending.remove(owner_id)
        except ValueError:
            return False
        return True

    def _ensure_workers_locked(self) -> None:
        desired = min(
            self.max_workers, len(self._active) + len(self._pending))
        while len(self._threads) < desired:
            self._thread_serial += 1
            thread = threading.Thread(
                target=self._worker,
                name=f'knowledge-enrichment-{self._thread_serial}',
                daemon=True,
            )
            self._threads.add(thread)
            try:
                thread.start()
            except BaseException:
                self._threads.discard(thread)
                raise

    def _worker(self) -> None:
        current = threading.current_thread()
        retired_for_idle = False
        try:
            while True:
                with self._condition:
                    while not self._pending:
                        if self._halt_workers:
                            return
                        if self.idle_seconds <= 0:
                            self._condition.wait()
                            continue
                        notified = self._condition.wait(self.idle_seconds)
                        if not self._pending and not notified:
                            retired_for_idle = True
                            return
                    owner_id = self._pending.popleft()
                    self._pending_set.remove(owner_id)
                    state = self._states.get(owner_id)
                    if state is None or state.stop_event.is_set():
                        self._states.pop(owner_id, None)
                        self._publish_locked()
                        continue
                    self._active.add(owner_id)
                    self._publish_locked()

                observe_executor_queue_wait(
                    'knowledge-enrichment',
                    time.monotonic() - state.queued_at,
                )
                more_work = False
                failed = False
                try:
                    more_work = bool(
                        self._processor(owner_id, state.stop_event))
                except BaseException as exc:
                    failed = True
                    logger.error(
                        '[KnowledgeVision] owner scheduler turn crashed: %s',
                        exc,
                        exc_info=True,
                    )

                with self._condition:
                    self._active.discard(owner_id)
                    current_state = self._states.get(owner_id)
                    if current_state is state:
                        should_requeue = (
                            not self._halt_workers
                            and not state.stop_event.is_set()
                            and (more_work or state.reschedule_requested)
                        )
                        state.reschedule_requested = False
                        if should_requeue:
                            state.queued_at = time.monotonic()
                            self._pending.append(owner_id)
                            self._pending_set.add(owner_id)
                        else:
                            self._states.pop(owner_id, None)
                    self._completed_turns += int(not failed)
                    self._failed_turns += int(failed)
                    self._ensure_workers_locked()
                    self._publish_locked()
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._threads.discard(current)
                if retired_for_idle:
                    self._retired_threads += 1
                    record_executor_idle_retirement(
                        'knowledge-enrichment', 1)
                if not self._halt_workers:
                    self._ensure_workers_locked()
                self._publish_locked()
                self._condition.notify_all()

    def _publish_locked(self) -> None:
        publish_executor_state(
            'knowledge-enrichment',
            workers=self.max_workers,
            queued=len(self._pending),
            active=len(self._active),
            resident_threads=len(self._threads),
        )


__all__ = [
    'KnowledgeEnrichmentCapacityExceeded',
    'OwnerFairEnrichmentLane',
]
