"""Focused fan-out scheduling boundary for orchestration graphs.

The graph interpreter decides *when* a parallel node is entered.  This module
owns how its branch walks are scheduled, how structural branch crashes become
honest node failures, how outputs reconverge, and where execution resumes after
the common barrier.  All graph/outcome/event dependencies are injected ports.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Protocol

from lib.error_envelope import make_envelope
from lib.log import get_logger


logger = get_logger(__name__)


class OrchestrationParallelAborted(Exception):
    """Translate an injected interpreter abort without importing the engine."""


class OrchestrationParallelNavigatorPort(Protocol):
    def find_common_barrier(self, branches: list[str]) -> str | None: ...

    def single_next(self, node_id: str) -> str | None: ...


class OrchestrationParallelOutcomePort(Protocol):
    def record_node_failure(
        self, *, node_id: str, role: str | None, error: str,
    ) -> None: ...


class OrchestrationParallelRuntime:
    """Run one fan-out and return its merged context plus post-join node."""

    def __init__(
        self,
        *,
        navigator: OrchestrationParallelNavigatorPort,
        branches: Callable[[str], list[str]],
        walk: Callable[..., str],
        outcomes: OrchestrationParallelOutcomePort,
        emit: Callable[[dict], None],
        max_parallel: int,
        current_iteration: Callable[[], int],
        abort_errors: tuple[type[Exception], ...] = (),
    ) -> None:
        self._navigator = navigator
        self._branches = branches
        self._walk = walk
        self._outcomes = outcomes
        self._emit = emit
        self._max_parallel = max(1, int(max_parallel))
        self._current_iteration = current_iteration
        self._abort_errors = abort_errors

    def _is_abort(self, error: Exception) -> bool:
        return bool(self._abort_errors) and isinstance(
            error, self._abort_errors)

    def _branch_failure(self, branch_id: str, error: Exception) -> str:
        logger.error(
            '[FlowParallel] branch %s failed: %s',
            branch_id, error, exc_info=True,
        )
        detail = f'{type(error).__name__}: {error}'
        self._outcomes.record_node_failure(
            node_id=branch_id,
            role=None,
            error=detail,
        )
        self._emit({
            'type': 'error',
            'node_id': branch_id,
            'error': make_envelope(
                'generic',
                message=(
                    '并行分支执行失败\n'
                    'Parallel orchestration branch failed'
                ),
                detail=f'parallel branch failed: {error}',
                context='orchestration:parallel',
                source='lib.orchestration_parallel_runtime',
                raw=detail,
                retryable=False,
            ),
        })
        return f'[branch {branch_id} FAILED: {detail}]'

    @staticmethod
    def _merge(context: str, outputs: list[str]) -> str:
        merged = context
        for output in outputs:
            merged = output if not merged else merged + '\n\n' + output
        return merged

    def run(self, parallel_id: str, context: str) -> tuple[str, str | None]:
        branches = list(self._branches(parallel_id))
        barrier = self._navigator.find_common_barrier(branches)
        self._emit({
            'type': 'parallel_start',
            'node_id': parallel_id,
            'branches': len(branches),
        })
        logger.info(
            '[FlowParallel] %s → %d branches, barrier=%s',
            parallel_id, len(branches), barrier,
        )

        iteration = max(0, int(self._current_iteration()))
        if len(branches) > 1 and iteration > 0:
            logger.debug(
                '[FlowParallel] %s runs %d concurrent branches inside a loop '
                '(iteration=%d): deliverable counts are aggregated; feedback/'
                'directive injection remains order-dependent for verdict-'
                'feeding producers',
                parallel_id, len(branches), iteration,
            )

        def run_branch(entry: str) -> str:
            return self._walk(entry, context, stop_at=barrier)

        outputs: list[str] = []
        if len(branches) == 1:
            try:
                outputs.append(run_branch(branches[0]))
            except Exception as error:
                if self._is_abort(error):
                    raise OrchestrationParallelAborted() from error
                raise
        elif branches:
            workers = min(self._max_parallel, len(branches))
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix='flow-par',
            ) as pool:
                futures = {
                    pool.submit(run_branch, branch): branch
                    for branch in branches
                }
                for future in as_completed(futures):
                    try:
                        outputs.append(future.result())
                    except Exception as error:
                        if self._is_abort(error):
                            raise OrchestrationParallelAborted() from error
                        outputs.append(self._branch_failure(
                            futures[future], error))

        merged = self._merge(context, outputs)
        next_node = self._navigator.single_next(barrier) if barrier else None
        return merged, next_node


__all__ = [
    'OrchestrationParallelAborted',
    'OrchestrationParallelNavigatorPort',
    'OrchestrationParallelOutcomePort',
    'OrchestrationParallelRuntime',
]
