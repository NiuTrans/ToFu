"""Bounded execution authority for context-provider reads.

Both early project prefetch and final Context Composer acquisition submit here.
The process owns one launch-budgeted daemon pool and a fixed pending queue;
request-local leases own cancellation only, never another physical pool.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future


CONTEXT_PROVIDER_COUNT = 8


class ContextProviderCapacityError(RuntimeError):
    """The bounded process-wide provider executor has no queue capacity."""


class BoundedContextProviderExecutor:
    """Daemon worker pool with finite resident and queued work."""

    def __init__(self, *, max_workers: int, queue_capacity: int):
        self.max_workers = max(1, int(max_workers))
        self.queue_capacity = max(1, int(queue_capacity))
        self._queue: queue.Queue = queue.Queue(maxsize=self.queue_capacity)
        self._start_lock = threading.Lock()
        self._threads: tuple[threading.Thread, ...] = ()
        self._closed = False

    def _ensure_started(self) -> None:
        if self._threads:
            return
        with self._start_lock:
            if self._closed:
                raise RuntimeError("context provider executor is closed")
            if self._threads:
                return
            threads = tuple(
                threading.Thread(
                    target=self._worker,
                    name=f"context-provider-{index + 1}",
                    daemon=True,
                )
                for index in range(self.max_workers)
            )
            self._threads = threads
            for thread in threads:
                thread.start()

    def submit(self, fn, *args) -> Future:
        future = Future()
        try:
            self._ensure_started()
        except RuntimeError as exc:
            future.set_exception(exc)
            return future
        try:
            self._queue.put_nowait((future, fn, args))
        except queue.Full:
            future.set_exception(ContextProviderCapacityError(
                "context provider queue is saturated"))
        return future

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                future, fn, args = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(fn(*args))
                except BaseException as exc:
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    def shutdown(self, *, wait_for_threads: bool = True) -> None:
        """Close a non-global executor; production keeps its singleton live."""
        with self._start_lock:
            if self._closed:
                threads = self._threads
            else:
                self._closed = True
                threads = self._threads
                for _thread in threads:
                    self._queue.put(None)
        if wait_for_threads:
            for thread in threads:
                thread.join(timeout=1.0)

    def snapshot(self) -> dict[str, int]:
        return {
            "workers": self.max_workers,
            "residentThreads": sum(
                1 for thread in self._threads if thread.is_alive()
            ),
            "queued": self._queue.qsize(),
            "queueCapacity": self.queue_capacity,
        }


class ContextProviderLease:
    """Request-local cancellation handle over the shared executor."""

    def __init__(self, executor: BoundedContextProviderExecutor):
        self._executor = executor
        self._futures: list[Future] = []

    def submit(self, fn, *args, **kwargs) -> Future:
        if kwargs:
            def invoke():
                return fn(*args, **kwargs)

            future = self._executor.submit(invoke)
        else:
            future = self._executor.submit(fn, *args)
        self._futures.append(future)
        return future

    def shutdown(
        self,
        wait: bool = False,
        *,
        cancel_futures: bool = False,
    ) -> None:
        del wait
        if cancel_futures:
            for future in self._futures:
                future.cancel()
        self._futures.clear()


def _context_provider_budget() -> tuple[int, int]:
    from runtime_guards import resolve_resource_budget

    agent_workers = resolve_resource_budget(
        "TOFU_AGENT_WORKERS", minimum=1, maximum=16
    )
    return context_provider_budget_from_agent_workers(
        agent_workers
    )


def context_provider_budget_from_agent_workers(
    agent_workers: int,
) -> tuple[int, int]:
    """Return finite worker/pending counts from the launch Agent budget."""
    workers = min(
        CONTEXT_PROVIDER_COUNT,
        max(2, max(1, int(agent_workers)) * 2),
    )
    return workers, max(CONTEXT_PROVIDER_COUNT, workers * 3)


(_CONTEXT_PROVIDER_WORKERS, _CONTEXT_PROVIDER_QUEUE_CAPACITY) = (
    _context_provider_budget()
)
# TODO(enterprise): replace this bounded FIFO with owner-fair pending admission
# when authenticated multi-user execution shares one worker process.
context_provider_executor = BoundedContextProviderExecutor(
    max_workers=_CONTEXT_PROVIDER_WORKERS,
    queue_capacity=_CONTEXT_PROVIDER_QUEUE_CAPACITY,
)


def create_context_provider_lease() -> ContextProviderLease:
    return ContextProviderLease(context_provider_executor)


__all__ = [
    "BoundedContextProviderExecutor",
    "ContextProviderCapacityError",
    "ContextProviderLease",
    "context_provider_budget_from_agent_workers",
    "context_provider_executor",
    "create_context_provider_lease",
]
