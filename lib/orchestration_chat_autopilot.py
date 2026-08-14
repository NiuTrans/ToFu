"""Autopilot-specific completion port for Flow-backed chat runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class OrchestrationAutopilotCompletionPorts:
    emit_concluded: Callable[..., None]
    clear_marker: Callable[[str], None]
    clear_run_id: Callable[[str], None]

    @classmethod
    def defaults(cls) -> 'OrchestrationAutopilotCompletionPorts':
        """Resolve live Autopilot side effects lazily at the adapter edge."""
        from lib.message_queue import clear_autopilot_marker
        from lib.tasks_pkg.autopilot_run_lifecycle import (
            _emit_run_concluded_event,
        )
        from lib.tasks_pkg.autopilot_state import _clear_run_id

        return cls(
            emit_concluded=_emit_run_concluded_event,
            clear_marker=clear_autopilot_marker,
            clear_run_id=_clear_run_id,
        )


@dataclass(frozen=True)
class OrchestrationAutopilotCompletionResult:
    reason: str
    concluded_emitted: bool
    marker_cleared: bool
    run_id_cleared: bool

    @property
    def ok(self) -> bool:
        return (
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

    concluded = marker = run_id = True
    try:
        ports.emit_concluded(
            task, conversation_id, task_id, reason=reason)
    except Exception as exc:
        concluded = False
        logger.warning(
            '[FlowChat] autopilot run conclusion failed '
            '(non-fatal) task=%s: %s', short_id, exc)
    try:
        ports.clear_marker(conversation_id)
    except Exception as exc:
        marker = False
        logger.warning(
            '[FlowChat] autopilot marker cleanup failed '
            '(non-fatal) task=%s: %s', short_id, exc)
    try:
        ports.clear_run_id(conversation_id)
    except Exception as exc:
        run_id = False
        logger.warning(
            '[FlowChat] autopilot run-id cleanup failed '
            '(non-fatal) task=%s: %s', short_id, exc)

    return OrchestrationAutopilotCompletionResult(
        reason=reason,
        concluded_emitted=concluded,
        marker_cleared=marker,
        run_id_cleared=run_id,
    )


__all__ = [
    'OrchestrationAutopilotCompletionPorts',
    'OrchestrationAutopilotCompletionResult',
    'complete_orchestration_autopilot_flow',
]
