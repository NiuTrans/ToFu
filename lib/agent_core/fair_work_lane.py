"""Bounded owner-fair scheduling for optional background work.

Responsibility
--------------
Own a finite process-local queue whose pending jobs are selected round-robin
across explicit owners. Worker threads start lazily and retire after a bounded
idle interval. Domain task lifecycle, retries, and durable state stay outside.

Entry points
------------
``OwnerFairWorkLane.submit_task`` accepts an explicit job and owner identity,
with optional attended-work placement at the front of only that owner's queue;
``cancel_task`` removes work only while it is queued; ``snapshot`` exposes
bounded scheduling evidence without owner or job identifiers.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Hashable
from concurrent.futures import Future
from dataclasses import dataclass
import threading
import time
from typing import Any

from lib.observability import (
    observe_executor_queue_wait,
    publish_executor_state,
    record_executor_idle_retirement,
    record_executor_rejection,
)


class FairWorkLaneQueueFull(RuntimeError):
    """The finite pending-work allowance has been exhausted."""


@dataclass(slots=True)
class _WorkItem:
    job_id: str
    owner_key: Hashable
    future: Future[Any]
    function: Callable[[], Any]
    queued_at: float
    started: bool = False


class OwnerFairWorkLane:
    """A lazy bounded worker lane with round-robin pending-owner selection."""

    def __init__(
        self,
        *,
        max_workers: int,
        queue_capacity: int,
        idle_seconds: float,
        thread_name_prefix: str,
        metric_pool: str,
    ) -> None:
        if max_workers <= 0:
            raise ValueError('max_workers must be positive')
        if queue_capacity <= 0:
            raise ValueError('queue_capacity must be positive')
        if idle_seconds < 0:
            raise ValueError('idle_seconds cannot be negative')
        self.max_workers = int(max_workers)
        self.queue_capacity = int(queue_capacity)
        self.idle_seconds = float(idle_seconds)
        self.thread_name_prefix = str(thread_name_prefix or 'tofu-fair-work')
        self.metric_pool = str(metric_pool or 'fair-work')

        self._condition = threading.Condition(threading.RLock())
        self._pending_by_owner: dict[Hashable, deque[_WorkItem]] = {}
        self._owner_cycle: deque[Hashable] = deque()
        self._pending_count = 0
        self._jobs_by_id: dict[str, _WorkItem] = {}
        self._running_ids: set[str] = set()
        self._threads: set[threading.Thread] = set()
        self._thread_serial = 0
        self._active = 0
        self._accepted = 0
        self._rejected = 0
        self._cancelled = 0
        self._retired = 0
        self._peak_pending = 0
        self._shutdown = False
        self._publish_locked()

    def submit_task(
        self,
        job_id: str,
        owner_key: Hashable,
        function: Callable[[], Any],
        *,
        front_of_owner_queue: bool = False,
    ) -> Future[Any]:
        """Admit one uniquely addressed job without blocking the producer.

        ``front_of_owner_queue`` prioritizes attended work only among pending
        jobs for the same owner. The owner itself retains one position in the
        round-robin cycle, so this cannot buy extra service ahead of peers.
        """
        normalized_job_id = str(job_id or '')
        if not normalized_job_id:
            raise ValueError('job_id must be non-empty')
        if owner_key is None or isinstance(owner_key, bool):
            raise ValueError('owner_key must be explicit')
        try:
            hash(owner_key)
        except TypeError as exc:
            raise ValueError('owner_key must be hashable') from exc
        if not callable(function):
            raise TypeError('function must be callable')
        if not isinstance(front_of_owner_queue, bool):
            raise TypeError('front_of_owner_queue must be boolean')

        future: Future[Any] = Future()
        item = _WorkItem(
            job_id=normalized_job_id,
            owner_key=owner_key,
            future=future,
            function=function,
            queued_at=time.monotonic(),
        )
        with self._condition:
            if self._shutdown:
                record_executor_rejection(self.metric_pool, reason='shutdown')
                raise RuntimeError('cannot submit after lane shutdown')
            if normalized_job_id in self._jobs_by_id:
                record_executor_rejection(self.metric_pool, reason='duplicate')
                raise RuntimeError(
                    f'work lane job already scheduled: {normalized_job_id}')
            if self._pending_count >= self.queue_capacity:
                self._rejected += 1
                record_executor_rejection(self.metric_pool, reason='queue_full')
                self._publish_locked()
                raise FairWorkLaneQueueFull(
                    f'background work queue is full '
                    f'({self._pending_count}/{self.queue_capacity})')

            owner_queue = self._pending_by_owner.get(owner_key)
            if owner_queue is None:
                owner_queue = deque()
                self._pending_by_owner[owner_key] = owner_queue
                self._owner_cycle.append(owner_key)
            if front_of_owner_queue:
                owner_queue.appendleft(item)
            else:
                owner_queue.append(item)
            self._pending_count += 1
            self._jobs_by_id[normalized_job_id] = item
            self._accepted += 1
            self._peak_pending = max(self._peak_pending, self._pending_count)
            future.add_done_callback(
                lambda completed, queued=item: self._forget_cancelled(
                    queued, completed))
            try:
                self._ensure_workers_locked()
            except BaseException:
                # Thread.start() can fail under a host PID/thread ceiling.
                # Roll the just-admitted item back while this lock still keeps
                # any successfully started peer from observing it.
                self._remove_pending_locked(item)
                self._jobs_by_id.pop(normalized_job_id, None)
                self._accepted = max(0, self._accepted - 1)
                future.cancel()
                self._rejected += 1
                record_executor_rejection(
                    self.metric_pool, reason='worker_start_failed')
                self._publish_locked()
                self._condition.notify_all()
                raise
            self._publish_locked()
            self._condition.notify_all()
        return future

    def cancel_task(self, job_id: str) -> bool:
        """Cancel and remove one job only while it is still pending."""
        with self._condition:
            item = self._jobs_by_id.get(str(job_id or ''))
            if item is None or item.started:
                return False
            if not self._remove_pending_locked(item):
                return False
            self._jobs_by_id.pop(item.job_id, None)
            self._cancelled += 1
            cancelled = item.future.cancel()
            self._publish_locked()
            self._condition.notify_all()
            return cancelled

    def snapshot(self) -> dict[str, int | float | bool]:
        """Return resource/fairness counters without identity labels."""
        with self._condition:
            return {
                'workers': self.max_workers,
                'queueCapacity': self.queue_capacity,
                'idleSeconds': self.idle_seconds,
                'active': self._active,
                'queued': self._pending_count,
                'queuedOwners': len(self._pending_by_owner),
                'residentThreads': len(self._threads),
                'accepted': self._accepted,
                'rejected': self._rejected,
                'cancelled': self._cancelled,
                'retiredThreads': self._retired,
                'peakQueued': self._peak_pending,
                'shutdown': self._shutdown,
            }

    def shutdown(self, *, wait: bool = True, cancel_pending: bool = False) -> None:
        """Reject new work and optionally cancel every still-pending job."""
        with self._condition:
            self._shutdown = True
            if cancel_pending:
                pending = tuple(self._jobs_by_id.values())
                for item in pending:
                    if item.started or not self._remove_pending_locked(item):
                        continue
                    self._jobs_by_id.pop(item.job_id, None)
                    self._cancelled += 1
                    item.future.cancel()
            threads = tuple(self._threads)
            self._publish_locked()
            self._condition.notify_all()
        if wait:
            current = threading.current_thread()
            for thread in threads:
                if thread is not current:
                    thread.join()

    def _remove_pending_locked(self, item: _WorkItem) -> bool:
        owner_queue = self._pending_by_owner.get(item.owner_key)
        if owner_queue is None:
            return False
        try:
            owner_queue.remove(item)
        except ValueError:
            return False
        self._pending_count = max(0, self._pending_count - 1)
        if not owner_queue:
            self._pending_by_owner.pop(item.owner_key, None)
            try:
                self._owner_cycle.remove(item.owner_key)
            except ValueError:
                pass
        return True

    def _forget_cancelled(self, item: _WorkItem, future: Future[Any]) -> None:
        if not future.cancelled():
            return
        with self._condition:
            if item.started or self._jobs_by_id.get(item.job_id) is not item:
                return
            if not self._remove_pending_locked(item):
                return
            self._jobs_by_id.pop(item.job_id, None)
            self._cancelled += 1
            self._publish_locked()
            self._condition.notify_all()

    def _pop_next_locked(self) -> _WorkItem:
        owner_key = self._owner_cycle.popleft()
        owner_queue = self._pending_by_owner[owner_key]
        item = owner_queue.popleft()
        self._pending_count -= 1
        if owner_queue:
            self._owner_cycle.append(owner_key)
        else:
            self._pending_by_owner.pop(owner_key, None)
        return item

    def _ensure_workers_locked(self) -> None:
        if self._shutdown:
            return
        desired = min(self.max_workers, self._active + self._pending_count)
        while len(self._threads) < desired:
            self._thread_serial += 1
            thread = threading.Thread(
                target=self._worker,
                name=f'{self.thread_name_prefix}-{self._thread_serial}',
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
                    while self._pending_count == 0:
                        if self._shutdown:
                            return
                        if self.idle_seconds <= 0:
                            self._condition.wait()
                            continue
                        notified = self._condition.wait(self.idle_seconds)
                        if self._pending_count == 0 and not notified:
                            retired_for_idle = True
                            return
                    item = self._pop_next_locked()
                    if not item.future.set_running_or_notify_cancel():
                        self._jobs_by_id.pop(item.job_id, None)
                        self._publish_locked()
                        continue
                    item.started = True
                    self._running_ids.add(item.job_id)
                    self._active += 1
                    self._publish_locked()

                observe_executor_queue_wait(
                    self.metric_pool,
                    time.monotonic() - item.queued_at,
                )
                try:
                    result = item.function()
                except BaseException as exc:
                    item.future.set_exception(exc)
                else:
                    item.future.set_result(result)
                finally:
                    with self._condition:
                        self._running_ids.discard(item.job_id)
                        self._jobs_by_id.pop(item.job_id, None)
                        self._active = max(0, self._active - 1)
                        self._ensure_workers_locked()
                        self._publish_locked()
                        self._condition.notify_all()
        finally:
            with self._condition:
                self._threads.discard(current)
                if retired_for_idle:
                    self._retired += 1
                    record_executor_idle_retirement(self.metric_pool, 1)
                if not self._shutdown:
                    self._ensure_workers_locked()
                self._publish_locked()
                self._condition.notify_all()

    def _publish_locked(self) -> None:
        publish_executor_state(
            self.metric_pool,
            workers=self.max_workers,
            queued=self._pending_count,
            active=self._active,
            resident_threads=len(self._threads),
        )


__all__ = [
    'FairWorkLaneQueueFull',
    'OwnerFairWorkLane',
]
