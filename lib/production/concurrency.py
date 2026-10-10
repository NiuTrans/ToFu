"""Owned demand executors for production sibling stages and search queries.

Callers drain all owned work. Launch policy sizes idle reuse only; accepted
work gets a worker without a count quota. The generic worker executor owns
pre-entry cancellation and unstarted thread failures. Observations aggregate
live pool owners without per-job metric labels; retirement waits for workers.
"""
from __future__ import annotations

from contextlib import contextmanager
import sys

from lib.observability import (ExecutorObservation, _EXECUTOR_OBSERVATIONS,
                               publish_executor_state)
from lib.log import get_logger

logger = get_logger(__name__)


_POOL_SNAPSHOTS = _EXECUTOR_OBSERVATIONS


class _PoolObservation(ExecutorObservation):
    def __init__(self, name):
        super().__init__(name, publisher=lambda pool, **snapshot:
                         publish_executor_state(pool, **snapshot))


@contextmanager
def production_executor(*, name: str, idle_reuse_hint=None):
    from lib.agent_core.worker_executor import RecoverableAgentExecutor
    from runtime_guards import resolve_resource_budget

    if idle_reuse_hint is None:
        idle_reuse_hint = resolve_resource_budget('TOFU_AGENT_WORKERS', maximum=sys.maxsize)
    try:
        idle_reuse_hint = max(1, int(idle_reuse_hint))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('idle reuse hint must be an integer') from error
    observation = _PoolObservation(name)
    try:
        with RecoverableAgentExecutor(
                max_workers=idle_reuse_hint, queue_capacity=0,
                max_abandoned_workers=idle_reuse_hint,
                thread_name_prefix=name, metric_pool=name,
                state_publisher=observation.publish) as executor:
            yield executor
    finally:
        # An interrupted join retains physical ownership. A later worker
        # snapshot repays the observation when its last carrier actually exits.
        observation.retire()


@contextmanager
def owned_results(jobs, run_job, *, name, should_stop=None, idle_reuse_hint=None,
                  inherit_context=False):
    """Yield an iterator of (job, Future) while owning and draining its workers.

    Submit all eligible independent work without a window. Yield ready results
    between submissions so callers can commit and observe errors immediately.
    A stop cancels only unstarted work; started outcomes still reach the caller.
    Leaving the context, even on an exception, joins all published workers.
    """
    from concurrent.futures import Future, FIRST_COMPLETED, wait
    stop = should_stop or (lambda: False)
    with production_executor(name=name, idle_reuse_hint=idle_reuse_hint) as pool:
        def results():
            active = {}
            pending = iter(enumerate(jobs))
            exhausted = False
            while active or not exhausted:
                stopped = bool(stop())
                if stopped:
                    exhausted = True
                    for future in active:
                        future.cancel()
                if not exhausted:
                    try:
                        ordinal, job = next(pending)
                    except StopIteration:
                        exhausted = True
                    else:
                        try:
                            if inherit_context:
                                from contextvars import copy_context
                                future = pool.submit(copy_context().run, run_job, job)
                            else:
                                future = pool.submit(run_job, job)
                        except Exception as error:
                            # The generic executor guarantees failed submit
                            # has not published the rejected callable.
                            future = Future()
                            future.set_exception(error)
                        active[future] = (ordinal, job)
                done = [future for future in active if future.done()]
                if not done and not exhausted:
                    continue
                if not done and active:
                    done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda item: active[item][0]):
                    _, job = active.pop(future)
                    if not future.cancelled():
                        yield job, future
        yield results()
