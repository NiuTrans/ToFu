"""Application lifecycle service for Flow-backed chat GoalRuns."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import time
from typing import Any

from lib.goal_runs.contract import (
    GOAL_RUN_FORMAT,
    goal_run_id_for_task,
    goal_run_policy,
    terminal_goal_transition,
)
from lib.goal_runs.objective import objective_from_task
from lib.goal_runs.repository import (
    GoalRunRepositoryError,
    GoalRunRepositoryPort,
    SidecarGoalRunRepository,
)


class GoalRunLifecycleError(RuntimeError):
    """A GoalRun could not cross its required durable lifecycle boundary."""


def _no_queued_human(_conversation_id: str, _owner_user_id: int) -> bool:
    return False


def _queued_human_waiting(
    conversation_id: str,
    owner_user_id: int,
) -> bool:
    from lib.message_queue import has_pending_human_turn

    return has_pending_human_turn(
        conversation_id, user_id=owner_user_id)


@dataclass(frozen=True)
class GoalRunServicePorts:
    repository_for_owner: Callable[[int, str | None], GoalRunRepositoryPort]
    queued_human_waiting: Callable[[str, int], bool] = _no_queued_human

    @classmethod
    def defaults(cls) -> 'GoalRunServicePorts':
        return cls(
            repository_for_owner=lambda owner_user_id, tenant_id: (
                SidecarGoalRunRepository(owner_user_id, tenant_id=tenant_id)
            ),
            queued_human_waiting=_queued_human_waiting,
        )


class GoalRunService:
    """Start and settle one goal using a single durable state machine."""

    def __init__(self, *, ports: GoalRunServicePorts | None = None) -> None:
        self._ports = ports or GoalRunServicePorts.defaults()

    @staticmethod
    def _identity(task: Mapping[str, Any]) -> tuple[int, str | None]:
        from lib.tasks_pkg.manager._registry import task_user_id

        return task_user_id(task), task.get('_tenant_id')

    def _repository(self, task: Mapping[str, Any]) -> GoalRunRepositoryPort:
        owner_user_id, tenant_id = self._identity(task)
        return self._ports.repository_for_owner(owner_user_id, tenant_id)

    def start(self, task: dict, definition: Mapping[str, Any]) -> dict:
        task_id = str(task.get('id') or '')
        conversation_id = str(task.get('convId') or '')
        objective = objective_from_task(task)
        if not objective:
            raise GoalRunLifecycleError(
                'Goal Mode requires an objective from the accepted human turn')
        run_id = goal_run_id_for_task(task_id)
        policy = goal_run_policy()
        try:
            result = self._repository(task).start(
                run_id,
                conversation_id=conversation_id,
                objective=objective,
                definition=definition,
                policy=policy,
            )
        except GoalRunRepositoryError as error:
            raise GoalRunLifecycleError(
                'GoalRun start was not durably accepted') from error
        run = result.get('run')
        if not isinstance(run, dict) or run.get('status') != 'active':
            raise GoalRunLifecycleError(
                'GoalRun start returned an invalid lifecycle projection')
        task.update({
            '_goalRunFormat': GOAL_RUN_FORMAT,
            '_goalRunId': run_id,
            '_goalObjective': objective,
            '_goalRunStatus': 'active',
            '_goalRunReason': 'started',
            '_goalRunPolicy': policy,
        })
        if (
            bool((task.get('config') or {}).get('_goalContinuationCommand'))
            and self._ports.queued_human_waiting(
                conversation_id, self._identity(task)[0])
        ):
            # Close the create-pair → registry-registration race. The newer
            # human row is already durable; starting this stale continuation
            # only to let it overwrite that objective would invert authority.
            task.update({
                'aborted': True,
                '_abort_timestamp': time.time(),
                '_abort_reason': 'superseded_by_human',
            })
            abort_event = task.get('abort_event')
            if callable(getattr(abort_event, 'set', None)):
                abort_event.set()
        return run

    def complete(self, task: dict, terminal: Any) -> dict:
        run_id = str(task.get('_goalRunId') or '')
        if not run_id:
            raise GoalRunLifecycleError(
                'Flow-backed Goal Mode completed without a GoalRun identity')
        status, reason = terminal_goal_transition(
            str(getattr(terminal, 'category', '') or ''),
            stop_reason=str(getattr(terminal, 'stop_reason', '') or ''),
            abort_reason=str(task.get('_abort_reason') or ''),
        )
        outcome = (
            terminal.as_dict()
            if callable(getattr(terminal, 'as_dict', None)) else {}
        )
        try:
            result = self._repository(task).transition(
                run_id,
                status=status,
                reason=reason,
                final=str(task.get('content') or ''),
                outcome=outcome,
            )
        except GoalRunRepositoryError as error:
            raise GoalRunLifecycleError(
                'GoalRun terminal transition was not durably accepted') from error
        run = result.get('run')
        if not isinstance(run, dict) or run.get('status') != status:
            raise GoalRunLifecycleError(
                'GoalRun transition returned an invalid lifecycle projection')
        task['_goalRunStatus'] = status
        task['_goalRunReason'] = reason
        return run

    def fail(self, task: dict, *, reason: str = 'runtime_failure') -> dict | None:
        """Best-effort terminalization used after a Flow fatal boundary."""
        run_id = str(task.get('_goalRunId') or '')
        if not run_id:
            return None
        try:
            result = self._repository(task).transition(
                run_id,
                status='failed',
                reason=reason,
                final=str(task.get('content') or ''),
                outcome={},
            )
        except GoalRunRepositoryError as error:
            raise GoalRunLifecycleError(
                'GoalRun failure transition was not durably accepted') from error
        run = result.get('run')
        if isinstance(run, dict):
            task['_goalRunStatus'] = str(run.get('status') or 'failed')
            task['_goalRunReason'] = reason
            return run
        raise GoalRunLifecycleError(
            'GoalRun failure transition returned an invalid projection')


__all__ = [
    'GoalRunLifecycleError',
    'GoalRunService',
    'GoalRunServicePorts',
]
