"""Bounded root-agent scheduling with recoverable logical worker slots.

Responsibility
--------------
Own the in-process queue between an accepted chat task and its physical Python
worker thread.  The queue is finite, worker entry is observable, and a thread
that the task reaper has proved wedged can be quarantined behind one bounded
replacement slot.  The old thread is never killed; if it eventually returns,
it exits instead of consuming work again.

Entry points
------------
``RecoverableAgentExecutor`` implements :class:`concurrent.futures.Executor`
for ``asyncio.run_in_executor`` and adds task-addressed cancellation,
abandonment, and scheduling snapshots.

Dependencies
------------
Only the standard-library futures/threading primitives and metric helpers in
``lib.observability``.  Conversation and task lifecycle policy stays outside
this resource owner.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Executor, Future
from dataclasses import dataclass
import threading
import time
import uuid
from typing import Any, Callable

from lib.observability import (
    observe_executor_queue_wait,
    publish_executor_state,
    record_executor_abandonment,
    record_executor_idle_retirement,
    record_executor_rejection,
)


class AgentExecutorQueueFull(RuntimeError):
    """The bounded agent queue cannot accept another task."""


@dataclass(slots=True)
class _WorkItem:
    job_id: str
    future: Future[Any]
    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    queued_at: float
    started: bool = False
    abandoned: bool = False


class RecoverableAgentExecutor(Executor):
    """A finite FIFO executor whose logical capacity survives one stuck job.

    Python cannot safely terminate a running thread.  ``abandon_task`` instead
    quarantines the proven-wedged worker and starts a replacement while keeping
    logical active work at ``max_workers``.  Replacement residency is itself
    bounded by ``max_abandoned_workers``.  When a quarantined call returns, its
    worker retires before reading the queue, so recovery never permanently
    raises concurrency.
    """

    def __init__(
        self,
        *,
        max_workers: int,
        queue_capacity: int,
        max_abandoned_workers: int,
        thread_name_prefix: str = 'tofu-agent',
        metric_pool: str = 'agent',
        idle_retain_threads: int = 0,
    ) -> None:
        if max_workers <= 0:
            raise ValueError('max_workers must be positive')
        if queue_capacity <= 0:
            raise ValueError('queue_capacity must be positive')
        if max_abandoned_workers <= 0:
            raise ValueError('max_abandoned_workers must be positive')
        self._max_workers = int(max_workers)
        self.queue_capacity = int(queue_capacity)
        self.max_abandoned_workers = int(max_abandoned_workers)
        self.thread_name_prefix = str(thread_name_prefix or 'tofu-agent')
        self.metric_pool = str(metric_pool or 'agent')
        self._idle_retain_threads = max(
            0, min(self._max_workers, int(idle_retain_threads)))

        self._lifecycle_lock = threading.RLock()
        self._condition = threading.Condition(self._lifecycle_lock)
        self._pending: deque[_WorkItem] = deque()
        self._jobs_by_id: dict[str, _WorkItem] = {}
        self._running_by_id: dict[str, _WorkItem] = {}
        self._threads: set[threading.Thread] = set()
        self._next_thread_index = 0
        self._active_jobs = 0
        self._abandoned_jobs = 0
        self._last_excess_activity = time.monotonic()
        self._shutdown = False
        self._publish_locked()

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any):
        """Submit a generic job, preserving ``Executor`` compatibility.

        Chat spawn wrappers stamp ``_tofu_task_id`` on their callable.  Other
        callers receive an internal unique id and retain ordinary Future
        semantics without gaining task-addressed cancellation.
        """
        task_id = str(getattr(fn, '_tofu_task_id', '') or '')
        return self.submit_task(task_id or f'executor:{uuid.uuid4().hex}', fn,
                                *args, **kwargs)

    def submit_task(
        self,
        task_id: str,
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        """Accept one uniquely-addressed task into the finite FIFO queue."""
        job_id = str(task_id or '')
        if not job_id:
            raise ValueError('task_id must be non-empty')
        future: Future[Any] = Future()
        item = _WorkItem(
            job_id=job_id,
            future=future,
            function=fn,
            args=tuple(args),
            kwargs=dict(kwargs),
            queued_at=time.monotonic(),
        )
        with self._condition:
            if self._shutdown:
                record_executor_rejection(self.metric_pool, reason='shutdown')
                raise RuntimeError('cannot schedule new futures after shutdown')
            existing = self._jobs_by_id.get(job_id)
            if existing is not None:
                record_executor_rejection(self.metric_pool, reason='duplicate')
                raise RuntimeError(f'agent task already scheduled: {job_id}')
            if len(self._pending) >= self.queue_capacity:
                record_executor_rejection(self.metric_pool, reason='queue_full')
                raise AgentExecutorQueueFull(
                    'agent executor queue is full '
                    f'({len(self._pending)}/{self.queue_capacity})'
                )
            self._pending.append(item)
            self._jobs_by_id[job_id] = item
            self._last_excess_activity = item.queued_at
            future.add_done_callback(
                lambda completed, owner=item: self._forget_cancelled(owner, completed)
            )
            try:
                self._ensure_workers_locked()
            except BaseException:
                # Submission is an all-or-nothing ownership handoff.  If the
                # OS refuses the worker thread, leaving this item in the queue
                # would let it run after the caller has already terminally
                # settled the reported rejection.
                try:
                    self._pending.remove(item)
                except ValueError:
                    pass
                self._jobs_by_id.pop(job_id, None)
                item.future.cancel()
                self._last_excess_activity = time.monotonic()
                record_executor_rejection(
                    self.metric_pool, reason='thread_start_failed')
                self._publish_locked()
                self._condition.notify_all()
                raise
            self._publish_locked()
            self._condition.notify_all()
        return future

    def cancel_task(self, task_id: str) -> bool:
        """Cancel one task only while it is still waiting in this queue."""
        with self._condition:
            item = self._jobs_by_id.get(str(task_id or ''))
            if item is None or item.started:
                return False
            try:
                self._pending.remove(item)
            except ValueError:
                return False
            self._jobs_by_id.pop(item.job_id, None)
            cancelled = item.future.cancel()
            self._last_excess_activity = time.monotonic()
            self._publish_locked()
            self._condition.notify_all()
            return cancelled

    def abandon_task(self, task_id: str) -> bool:
        """Restore a slot whose running thread was durably declared wedged.

        Returns ``True`` only when a replacement was admitted.  A false result
        means the task was not running here, was already quarantined, or the
        explicit replacement-residency budget is exhausted.
        """
        with self._condition:
            item = self._running_by_id.get(str(task_id or ''))
            if item is None or item.abandoned:
                return False
            if self._abandoned_jobs >= self.max_abandoned_workers:
                record_executor_abandonment(
                    self.metric_pool,
                    recovered=False,
                    failure_reason='thread_start_failed',
                )
                return False
            item.abandoned = True
            self._active_jobs = max(0, self._active_jobs - 1)
            self._abandoned_jobs += 1
            self._last_excess_activity = time.monotonic()
            try:
                self._ensure_workers_locked()
            except BaseException:
                # Quarantine is committed only after its replacement thread
                # exists.  Otherwise a transient OS thread-start failure would
                # make the accounting claim a slot was restored while leaving
                # already-queued work with no healthy worker to consume it.
                item.abandoned = False
                self._active_jobs += 1
                self._abandoned_jobs = max(0, self._abandoned_jobs - 1)
                self._last_excess_activity = time.monotonic()
                self._publish_locked()
                self._condition.notify_all()
                record_executor_abandonment(self.metric_pool, recovered=False)
                raise
            self._publish_locked()
            self._condition.notify_all()
            record_executor_abandonment(self.metric_pool, recovered=True)
            return True

    def scheduling_snapshot(self, task_id: str | None = None) -> dict[str, Any]:
        """Return bounded scheduler evidence for diagnostics and presentation."""
        with self._lifecycle_lock:
            job_id = str(task_id or '')
            item = self._jobs_by_id.get(job_id) if job_id else None
            queue_position = None
            queued_for_seconds = None
            task_state = 'unknown'
            if item is not None:
                if item.abandoned:
                    task_state = 'abandoned'
                elif item.started:
                    task_state = 'running'
                else:
                    task_state = 'queued'
                    queued_for_seconds = max(
                        0.0, time.monotonic() - item.queued_at)
                    for index, queued in enumerate(self._pending, start=1):
                        if queued is item:
                            queue_position = index
                            break
            return {
                'capacity': self._max_workers,
                'active': self._active_jobs,
                'queued': len(self._pending),
                'available': max(0, self._max_workers - self._active_jobs),
                'queueCapacity': self.queue_capacity,
                'abandoned': self._abandoned_jobs,
                'replacementCapacity': self.max_abandoned_workers,
                'residentThreads': len(self._threads),
                'taskState': task_state,
                'queuePosition': queue_position,
                'queuedForSeconds': (
                    round(queued_for_seconds, 3)
                    if queued_for_seconds is not None else None),
            }

    def idle_retirement_snapshot(
        self,
        idle_seconds: float,
        *,
        now: float | None = None,
    ) -> dict[str, int | float | bool]:
        """Return the same quiescent-generation contract as the sync pool."""
        observed_at = time.monotonic() if now is None else float(now)
        with self._lifecycle_lock:
            quiet_for = max(0.0, observed_at - self._last_excess_activity)
            resident = len(self._threads)
            due = bool(
                idle_seconds > 0
                and not self._shutdown
                and not self._pending
                and self._active_jobs == 0
                and self._abandoned_jobs == 0
                and resident > self._idle_retain_threads
                and quiet_for >= idle_seconds
            )
            return {
                'due': due,
                'pending': len(self._pending),
                'active': self._active_jobs,
                'abandoned': self._abandoned_jobs,
                'resident_threads': resident,
                'retain_threads': self._idle_retain_threads,
                'quiet_for_seconds': quiet_for,
            }

    def record_idle_retirement(self, resident_threads: int) -> None:
        record_executor_idle_retirement(self.metric_pool, resident_threads)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        """Reject new work, optionally cancel queued work, and wake workers."""
        with self._condition:
            if not self._shutdown:
                self._shutdown = True
            if cancel_futures:
                pending = tuple(self._pending)
                self._pending.clear()
                for item in pending:
                    self._jobs_by_id.pop(item.job_id, None)
                    item.future.cancel()
            threads = tuple(self._threads)
            self._publish_locked()
            self._condition.notify_all()
        if wait:
            current = threading.current_thread()
            for thread in threads:
                if thread is not current:
                    thread.join()

    def _forget_cancelled(self, item: _WorkItem, future: Future[Any]) -> None:
        if not future.cancelled():
            return
        with self._condition:
            if item.started or self._jobs_by_id.get(item.job_id) is not item:
                return
            try:
                self._pending.remove(item)
            except ValueError:
                return
            self._jobs_by_id.pop(item.job_id, None)
            self._last_excess_activity = time.monotonic()
            self._publish_locked()
            self._condition.notify_all()

    def _healthy_worker_count_locked(self) -> int:
        return max(0, len(self._threads) - self._abandoned_jobs)

    def _ensure_workers_locked(self) -> None:
        if self._shutdown:
            return
        desired = min(
            self._max_workers,
            self._active_jobs + len(self._pending),
        )
        while self._healthy_worker_count_locked() < desired:
            index = self._next_thread_index
            self._next_thread_index += 1
            thread = threading.Thread(
                target=self._worker,
                name=f'{self.thread_name_prefix}_{index}',
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
        retire_after_item = False
        try:
            while True:
                with self._condition:
                    while not self._pending:
                        if self._shutdown:
                            return
                        self._condition.wait()
                    item = self._pending.popleft()
                    if not item.future.set_running_or_notify_cancel():
                        self._jobs_by_id.pop(item.job_id, None)
                        self._publish_locked()
                        continue
                    item.started = True
                    self._running_by_id[item.job_id] = item
                    self._active_jobs += 1
                    self._last_excess_activity = time.monotonic()
                    self._publish_locked()
                observe_executor_queue_wait(
                    self.metric_pool, time.monotonic() - item.queued_at,
                )

                execution_outcome: Future[Any] = Future()
                try:
                    execution_outcome.set_result(
                        item.function(*item.args, **item.kwargs))
                except BaseException as exc:
                    execution_outcome.set_exception(exc)

                # A completed Future is an observable resource-release
                # boundary: commit the scheduler bookkeeping first so a
                # waiter that immediately snapshots or retires the pool cannot
                # see the job as both completed and still active.
                with self._condition:
                    self._running_by_id.pop(item.job_id, None)
                    self._jobs_by_id.pop(item.job_id, None)
                    if item.abandoned:
                        self._abandoned_jobs = max(
                            0, self._abandoned_jobs - 1)
                        retire_after_item = True
                    else:
                        self._active_jobs = max(0, self._active_jobs - 1)
                    self._last_excess_activity = time.monotonic()
                    self._publish_locked()
                    self._condition.notify_all()
                try:
                    result = execution_outcome.result()
                except BaseException as exc:
                    item.future.set_exception(exc)
                else:
                    item.future.set_result(result)
                if retire_after_item:
                    return
        finally:
            with self._condition:
                self._threads.discard(current)
                if not self._shutdown:
                    self._ensure_workers_locked()
                self._publish_locked()
                self._condition.notify_all()

    def _publish_locked(self) -> None:
        publish_executor_state(
            self.metric_pool,
            workers=self._max_workers,
            queued=len(self._pending),
            active=self._active_jobs,
            resident_threads=len(self._threads),
            abandoned=self._abandoned_jobs,
        )


__all__ = [
    'AgentExecutorQueueFull',
    'RecoverableAgentExecutor',
]
