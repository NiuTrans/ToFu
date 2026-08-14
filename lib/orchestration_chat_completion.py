"""Canonical task completion projection for Flow-backed chat runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger


logger = get_logger(__name__)


class OrchestrationChatFlowCompletion:
    """Prepare and finish one chat task from a normalized flow outcome.

    ``prepare`` persists turns and exposes terminal facts before any
    projection-specific lifecycle work (Autopilot run conclusion/cleanup).
    ``finish`` emits the common endpoint/done boundary and persists the task.
    Both phases are idempotent so recovery code cannot duplicate side effects.
    """

    def __init__(
        self,
        task: dict,
        *,
        projection: str,
        outcome: Any,
        messages: list[dict],
        task_event_sink: Any,
        turn_persistence: Any,
        append_event: Callable[[dict, dict], None],
        persist_task_result: Callable[[dict], None],
        notify_terminal: Callable[[dict], None],
        stamp_terminal: Callable[..., bool],
    ):
        self._task = task
        self._projection = projection
        self._outcome = outcome
        self._messages = messages
        self._task_event_sink = task_event_sink
        self._turn_persistence = turn_persistence
        self._append_event = append_event
        self._persist_task_result = persist_task_result
        self._notify_terminal = notify_terminal
        self._stamp_terminal = stamp_terminal
        self._prepared = False
        self._finished = False

    @property
    def terminal(self):
        return self._outcome.terminal_outcome

    @property
    def iterations(self) -> int:
        return int(self._outcome.result.get('agents_run') or 0)

    def _final_assistant(self) -> str:
        for message in reversed(self._messages):
            if (isinstance(message, dict)
                    and message.get('role') == 'assistant'
                    and message.get('content')):
                return str(message['content'])
        return str(self._outcome.result.get('final') or '')

    def _capture_trace(self) -> None:
        executor = self._outcome.executor
        if executor is None:
            self._task.setdefault('_flow_trace', [])
            return
        try:
            self._task['_flow_trace'] = executor.trace
        except Exception as exc:
            logger.debug(
                '[FlowChat] trace capture failed task=%s: %s',
                str(self._task.get('id') or '')[:8], exc,
            )
            self._task.setdefault('_flow_trace', [])

    def prepare(self):
        """Project content/trace/turns and persist the final turn snapshot."""
        if self._prepared:
            return self.terminal
        terminal = self.terminal
        self._task_event_sink.replace_content(self._final_assistant())
        if terminal.runtime_error and terminal.chat_status == 'error':
            self._task['error'] = terminal.error_envelope
        self._capture_trace()
        self._task['_endpoint_turns'] = self._messages
        self._task['_flow_turns'] = self._messages
        self._turn_persistence.finalize()
        self._prepared = True
        return terminal

    def finish(self):
        """Emit canonical terminal frames and persist the completed task."""
        self.prepare()
        if self._finished:
            return self.terminal
        terminal = self.terminal
        stop_reason = terminal.stop_reason
        finish_reason = terminal.finish_reason
        outcome = terminal.as_dict()
        self._task['_orchestration_outcome'] = outcome
        if not self._stamp_terminal(
            self._task,
            status=terminal.chat_status,
            finish_reason=finish_reason,
            endpoint_reason=stop_reason,
        ):
            self._finished = True
            return terminal

        self._append_event(self._task, build_event(
            EventType.ENDPOINT_COMPLETE,
            totalIterations=self.iterations,
            reason=stop_reason,
            replanCount=0,
            flowProjection=self._projection,
            orchestrationOutcome=outcome,
        ))
        done_event = build_event(
            EventType.DONE,
            usage=self._task.get('usage', {}),
            finishReason=finish_reason,
            endpointReason=stop_reason,
            flowMode=True,
            flowProjection=self._projection,
            orchestrationOutcome=outcome,
        )
        if finish_reason == 'incomplete':
            done_event['incomplete'] = True
        if self._task.get('error'):
            done_event['error'] = self._task['error']
        if self._task.get('model'):
            done_event['model'] = self._task['model']
        self._append_event(self._task, done_event)
        self._persist_task_result(self._task)
        self._notify_terminal(self._task)
        self._finished = True
        return terminal


__all__ = ['OrchestrationChatFlowCompletion']
