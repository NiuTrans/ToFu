"""Pure lifecycle policy for durable orchestration-run mutations.

Persistence and runtime adapters report facts (current header, committed row,
runtime abort exception). This module is the single owner of how those facts
map onto the versioned orchestration mutation contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.orchestration.run_status import (
    INITIAL_RUN_STATUS,
    is_terminal_run_status,
)
from lib.orchestration.mutation_result import (
    MUTATION_ACTION_ABORT_RUN,
    MUTATION_ACTION_DELETE_RUN,
    MUTATION_ACTION_TRANSITION_RUN,
    MUTATION_ACTIVE,
    MUTATION_CONFLICT,
    MUTATION_NOT_FOUND,
    MUTATION_PERSISTENCE_FAILED,
    MUTATION_TERMINAL,
    RunMutationResult,
)


@dataclass(frozen=True)
class RunLifecycle:
    status: str
    terminal: bool

    @classmethod
    def from_run(cls, run: dict) -> RunLifecycle:
        status = str(run.get('status') or INITIAL_RUN_STATUS)
        return cls(
            status=status,
            terminal=bool(run.get('terminal'))
            or is_terminal_run_status(status),
        )


def _mutation(ok: bool, *, action: str, run_id: str,
              reason: str = '', status: str = '') -> RunMutationResult:
    return RunMutationResult(
        ok,
        reason,
        run_status=status,
        action=action,
        target_id=run_id,
    )


def classify_transition(
    run_id: str,
    requested_status: str,
    *,
    committed: bool,
    current: dict | None = None,
    final: str | None = None,
    error: dict | str | None = None,
) -> RunMutationResult:
    """Classify a status write, including terminal retries and races."""
    action = MUTATION_ACTION_TRANSITION_RUN
    if committed:
        return _mutation(
            True, action=action, run_id=run_id, status=requested_status)
    if current is None:
        return _mutation(
            False, action=action, run_id=run_id, reason=MUTATION_NOT_FOUND)

    lifecycle = RunLifecycle.from_run(current)
    if lifecycle.terminal:
        if lifecycle.status != requested_status:
            return _mutation(
                False,
                action=action,
                run_id=run_id,
                reason=MUTATION_CONFLICT,
                status=lifecycle.status,
            )
        final_matches = final is None or current.get('final') == final
        error_matches = error is None or current.get('error') == error
        if final_matches and error_matches:
            return _mutation(
                True,
                action=action,
                run_id=run_id,
                status=lifecycle.status,
            )

    return _mutation(
        False,
        action=action,
        run_id=run_id,
        reason=MUTATION_PERSISTENCE_FAILED,
        status=lifecycle.status,
    )


def abort_precondition(
    run_id: str,
    current: dict | None,
) -> RunMutationResult | None:
    """Return a rejection, or ``None`` when runtime abort may proceed."""
    action = MUTATION_ACTION_ABORT_RUN
    if current is None:
        return _mutation(
            False, action=action, run_id=run_id, reason=MUTATION_NOT_FOUND)
    lifecycle = RunLifecycle.from_run(current)
    if lifecycle.terminal:
        return _mutation(
            False,
            action=action,
            run_id=run_id,
            reason=MUTATION_TERMINAL,
            status=lifecycle.status,
        )
    return None


def abort_runtime_conflict(run_id: str, current: dict) -> RunMutationResult:
    return _mutation(
        False,
        action=MUTATION_ACTION_ABORT_RUN,
        run_id=run_id,
        reason=MUTATION_CONFLICT,
        status=RunLifecycle.from_run(current).status,
    )


def classify_abort_transition(
    run_id: str,
    previous: dict,
    transition: RunMutationResult,
) -> RunMutationResult:
    """Scope a durable transition to abort and preserve missing-row honesty."""
    if transition.reason == MUTATION_NOT_FOUND:
        return _mutation(
            False,
            action=MUTATION_ACTION_ABORT_RUN,
            run_id=run_id,
            reason=MUTATION_PERSISTENCE_FAILED,
            status=RunLifecycle.from_run(previous).status,
        )
    return transition.scoped(MUTATION_ACTION_ABORT_RUN, run_id)


def delete_precondition(
    run_id: str,
    current: dict | None,
) -> RunMutationResult | None:
    """Return a rejection, or ``None`` when persistence delete may proceed."""
    action = MUTATION_ACTION_DELETE_RUN
    if current is None:
        return _mutation(
            False, action=action, run_id=run_id, reason=MUTATION_NOT_FOUND)
    lifecycle = RunLifecycle.from_run(current)
    if not lifecycle.terminal:
        return _mutation(
            False,
            action=action,
            run_id=run_id,
            reason=MUTATION_ACTIVE,
            status=lifecycle.status,
        )
    return None


def classify_delete_commit(
    run_id: str,
    current: dict,
    *,
    deleted: bool,
) -> RunMutationResult:
    lifecycle = RunLifecycle.from_run(current)
    return _mutation(
        deleted,
        action=MUTATION_ACTION_DELETE_RUN,
        run_id=run_id,
        reason='' if deleted else MUTATION_PERSISTENCE_FAILED,
        status=lifecycle.status,
    )


__all__ = [
    'RunLifecycle',
    'classify_transition',
    'abort_precondition',
    'abort_runtime_conflict',
    'classify_abort_transition',
    'delete_precondition',
    'classify_delete_commit',
]
