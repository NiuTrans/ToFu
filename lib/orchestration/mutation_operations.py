"""Runtime and one-shot resolver adapters for orchestration mutations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.orchestration.mutation_result import (
    MUTATION_ACTION_ABORT_RUN,
    MUTATION_NOT_FOUND,
    MUTATION_TERMINAL,
    MUTATION_CONFLICT,
    OrchestrationMutationResult,
)
from lib.task_runtime_ports import TaskAbortRuntimePort


def resolved_mutation(
    action: str,
    target_id: str,
    resolver: Callable[[], Any],
) -> OrchestrationMutationResult:
    """Classify a one-shot resolver that reports found or not found."""
    resolved = bool(resolver())
    return OrchestrationMutationResult(
        resolved,
        '' if resolved else MUTATION_NOT_FOUND,
        action=action,
        target_id=target_id,
    )


def runtime_abort_mutation(
    runtime: TaskAbortRuntimePort,
    task_id: str,
) -> OrchestrationMutationResult:
    """Classify the TaskRuntime abort race for an orchestration run."""
    if runtime.abort(task_id):
        return OrchestrationMutationResult(
            True,
            run_status='aborting',
            action=MUTATION_ACTION_ABORT_RUN,
            target_id=task_id,
        )
    task = runtime.get(task_id)
    if task is None:
        return OrchestrationMutationResult(
            False,
            MUTATION_NOT_FOUND,
            action=MUTATION_ACTION_ABORT_RUN,
            target_id=task_id,
        )
    status = str(task.get('status') or '')
    from lib.orchestration.run_status import is_terminal_run_status
    reason = (
        MUTATION_TERMINAL
        if is_terminal_run_status(status)
        else MUTATION_CONFLICT
    )
    return OrchestrationMutationResult(
        False,
        reason,
        run_status=status,
        action=MUTATION_ACTION_ABORT_RUN,
        target_id=task_id,
    )


__all__ = ['resolved_mutation', 'runtime_abort_mutation']
