"""Demand execution and request-local cancellation for context-provider reads.

Early project prefetch and final acquisition share the tofu_workers kernel via
its application observation adapter. Idle hints never gate independent reads;
physical worker ownership survives request deadlines until work returns.
"""
from __future__ import annotations

from concurrent.futures import Future
import sys


class DemandContextProviderExecutor:
    """Lazy demand workers with failed-Future submission and joined close."""

    def __init__(self, *, max_workers: int, queue_capacity: int = 0):
        from lib.agent_core.worker_executor import RecoverableAgentExecutor
        self.max_workers = max(1, int(max_workers))
        self.queue_capacity = 0  # retired compatibility argument
        self._pool = RecoverableAgentExecutor(
            max_workers=self.max_workers, queue_capacity=0,
            max_abandoned_workers=self.max_workers,
            thread_name_prefix="context-provider", metric_pool="context-provider",
            daemon_workers=True,
        )

    def submit(self, fn, *args) -> Future:
        try:
            return self._pool.submit(fn, *args)
        except Exception as error:
            # The kernel withdrew unstarted work before reporting failure.
            # Preserve the original physical error in the caller's Future.
            future = Future()
            future.set_exception(error)
            return future

    def shutdown(self, *, wait_for_threads: bool = True) -> None:
        """Fence new work; waiting close joins every physical peer."""
        self._pool.shutdown(wait=wait_for_threads)

    def worker_threads_snapshot(self):
        return self._pool.worker_threads_snapshot()

    def snapshot(self) -> dict[str, int]:
        state = self._pool.scheduling_snapshot()
        return {
            "workers": self.max_workers,
            "residentThreads": state["residentThreads"],
            "queued": state["queued"],
            "queueCapacity": 0,
        }


class ContextProviderLease:
    """Request-local cancellation handle over the shared executor."""

    def __init__(self, executor: DemandContextProviderExecutor):
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
    return context_provider_budget_from_agent_workers(resolve_resource_budget(
        "TOFU_AGENT_WORKERS", minimum=1, maximum=sys.maxsize,
    ))


def context_provider_budget_from_agent_workers(agent_workers: int) -> tuple[int, int]:
    """Derive an idle reuse hint and report zero local queue admission quota."""
    return max(1, int(agent_workers)) * 2, 0


_CONTEXT_PROVIDER_WORKERS, _ = _context_provider_budget()
context_provider_executor = DemandContextProviderExecutor(
    max_workers=_CONTEXT_PROVIDER_WORKERS,
)


def create_context_provider_lease() -> ContextProviderLease:
    return ContextProviderLease(context_provider_executor)


__all__ = [
    "DemandContextProviderExecutor", "ContextProviderLease",
    "context_provider_budget_from_agent_workers", "context_provider_executor",
    "create_context_provider_lease",
]
