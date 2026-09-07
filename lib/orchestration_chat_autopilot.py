"""Autopilot-specific completion port for Flow-backed chat runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class OrchestrationAutopilotCompletionPorts:
    complete_goal_run: Callable[..., dict]
    emit_concluded: Callable[..., None]
    clear_marker: Callable[[dict, str], None]
    clear_run_id: Callable[[dict, str], None]

    @classmethod
    def defaults(cls) -> 'OrchestrationAutopilotCompletionPorts':
        """Resolve live Autopilot side effects lazily at the adapter edge."""
        from lib.goal_runs.service import GoalRunService
        from lib.message_queue import clear_autopilot_marker
        from lib.tasks_pkg.manager._registry import task_user_id
        from lib.tasks_pkg.autopilot_run_lifecycle import (
            _emit_run_concluded_event,
        )
        from lib.tasks_pkg.autopilot_state import _clear_run_id

        return cls(
            complete_goal_run=lambda task, terminal: (
                GoalRunService().complete(task, terminal)
            ),
            emit_concluded=_emit_run_concluded_event,
            clear_marker=lambda task, conversation_id: clear_autopilot_marker(
                conversation_id, user_id=task_user_id(task)),
            clear_run_id=lambda task, conversation_id: _clear_run_id(
                conversation_id, user_id=task_user_id(task)),
        )


@dataclass(frozen=True)
class OrchestrationAutopilotCompletionResult:
    reason: str
    goal_transitioned: bool
    concluded_emitted: bool
    marker_cleared: bool
    run_id_cleared: bool

    @property
    def ok(self) -> bool:
        return (
            self.goal_transitioned
            and
            self.concluded_emitted
            and self.marker_cleared
            and self.run_id_cleared
        )


def complete_orchestration_autopilot_flow(
    task: dict,
    terminal: Any,
    *,
    ports: OrchestrationAutopilotCompletionPorts | None = None,
) -> OrchestrationAutopilotCompletionResult:
    """Emit the run boundary and release both persistent Autopilot controls.

    Every side effect is attempted independently and remains non-fatal to the
    already-completed chat flow. A failure to clear the arm marker must not
    prevent clearing the run pin (or vice versa).
    """
    ports = ports or OrchestrationAutopilotCompletionPorts.defaults()
    task_id = str(task.get('id') or '')
    short_id = task_id[:8]
    conversation_id = str(task.get('convId') or '')
    reason = (
        'task_done' if terminal.category == 'success'
        else str(terminal.stop_reason or '')
    )

    # This is the required lifecycle boundary.  Unlike the historical marker
    # cleanup below, failure propagates so a chat task cannot announce success
    # while its durable GoalRun remains active or records another outcome.
    run = ports.complete_goal_run(task, terminal)
    goal_run_id = str(
        task.get('_goalRunId')
        or (run.get('runId') if isinstance(run, dict) else '')
        or task_id
    )

    # The fold's report content is the run's own terminal VU verdict (machine
    # tokens stripped), stashed by the chat-flow runtime before this boundary.
    report = str(task.get('_goalStopReport') or '')
    concluded = marker = run_id = True
    try:
        ports.emit_concluded(
            task, conversation_id, goal_run_id, reason=reason, report=report)
    except Exception as exc:
        concluded = False
        logger.warning(
            '[FlowChat] autopilot run conclusion failed '
            '(non-fatal) task=%s: %s', short_id, exc)
    try:
        ports.clear_marker(task, conversation_id)
    except Exception as exc:
        marker = False
        logger.warning(
            '[FlowChat] autopilot marker cleanup failed '
            '(non-fatal) task=%s: %s', short_id, exc)
    try:
        ports.clear_run_id(task, conversation_id)
    except Exception as exc:
        run_id = False
        logger.warning(
            '[FlowChat] autopilot run-id cleanup failed '
            '(non-fatal) task=%s: %s', short_id, exc)

    return OrchestrationAutopilotCompletionResult(
        reason=reason,
        goal_transitioned=True,
        concluded_emitted=concluded,
        marker_cleared=marker,
        run_id_cleared=run_id,
    )


__all__ = [
    'OrchestrationAutopilotCompletionPorts',
    'OrchestrationAutopilotCompletionResult',
    'complete_orchestration_autopilot_flow',
]
