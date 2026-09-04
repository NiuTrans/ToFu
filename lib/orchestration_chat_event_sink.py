"""Project Flow-backed chat events onto the live task state.

The flow event adapter owns engine-event → chat-wire translation. This
sink owns the next boundary: applying those wire events to the in-memory task
snapshot used by reconnect/poll paths, then forwarding the unchanged event to
the task event log. Execution and persistence orchestration stay outside.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Callable


_TURN_META_KEYS = (
    'flowProjection',
    'turnRole',
    'emits',
    'vuMsgId',
    'autopilotRunId',
)
_FINAL_TURN_EVENTS = frozenset({
    'flow_planner_done',
    'flow_critic_msg',
})

_TOOL_EVENTS = frozenset({
    'tool_start', 'tool_progress', 'tool_result', 'tool_complete',
    'tool_compacted',
})


class OrchestrationChatTaskEventSink:
    """Callable live-event port for one Flow-backed chat task."""

    def __init__(
        self,
        task: dict,
        append_event: Callable[[dict, dict], None],
    ):
        self._task = task
        self._append_event = append_event

    def _content_lock(self):
        return self._task.get('content_lock') or nullcontext()

    def _reset_turn_content(self) -> None:
        with self._content_lock():
            self._task['content'] = ''
            self._task['thinking'] = ''

    def _append_delta(self, event: dict) -> None:
        with self._content_lock():
            content = event.get('content')
            thinking = event.get('thinking')
            if content:
                self._task['content'] = (
                    (self._task.get('content') or '') + str(content))
            if thinking:
                self._task['thinking'] = (
                    (self._task.get('thinking') or '') + str(thinking))

    @staticmethod
    def _tool_identity(round_entry: dict) -> tuple[str, int]:
        return (
            str(round_entry.get('toolCallId') or ''),
            int(round_entry.get('roundNum') or 0),
        )

    def _apply_tool_event(self, event: dict) -> None:
        """Keep reconnect/poll snapshot parity with the emitted live frame."""
        with self._content_lock():
            rounds = [
                dict(row) for row in self._task.get('toolRounds') or ()
                if isinstance(row, dict)
            ]
            identity = self._tool_identity(event)
            position = next((
                index for index, row in enumerate(rounds)
                if self._tool_identity(row) == identity
            ), -1)
            event_type = str(event.get('type') or '')
            if event_type == 'tool_start':
                candidate = {
                    key: event[key]
                    for key in (
                        'roundNum', 'llmRound', 'toolCallId', 'toolName',
                        'toolArgs', 'query', 'status', 'attentionKind',
                        'parentToolCallId', 'tStart',
                    )
                    if event.get(key) is not None
                }
                if position < 0:
                    rounds.append(candidate)
                else:
                    rounds[position].update(candidate)
            elif position >= 0:
                target = rounds[position]
                if event_type == 'tool_result':
                    target['results'] = event.get('results')
                    target['status'] = event.get('status') or 'done'
                elif event_type == 'tool_progress':
                    for key in (
                        'detail', 'chunk', 'seq', 'spooling', 'truncated',
                        'terminalReason', 'execStartTs', 'deadlineTs',
                    ):
                        if event.get(key) is not None:
                            target[key] = event[key]
                elif event_type == 'tool_complete':
                    target['toolContent'] = event.get('toolContent')
                    target['status'] = event.get('status') or (
                        'error' if event.get('isError') else 'done')
                elif event_type == 'tool_compacted':
                    if event.get('compactedContent') is not None:
                        target['toolContent'] = event['compactedContent']
                    target['compactionLayer'] = (
                        event.get('compactionLayer') or 'L1')
                if event.get('tEnd') is not None:
                    target['tEnd'] = event['tEnd']
            self._task['toolRounds'] = rounds

    def _finalize_turn(self, event: dict) -> None:
        with self._content_lock():
            if event.get('discard'):
                self._task['content'] = ''
                self._task['thinking'] = ''
                return
            self._task['content'] = (
                event.get('content') or self._task.get('content') or '')
            self._task['thinking'] = (
                event.get('thinking') or self._task.get('thinking') or '')

    def replace_content(self, content: str) -> None:
        """Replace the final assistant snapshot under the task content lock."""
        with self._content_lock():
            self._task['content'] = str(content or '')

    def __call__(self, event: dict) -> None:
        event_type = str(event.get('type') or '')
        if event_type == 'flow_iteration':
            self._task['_flow_phase'] = (
                event.get('phase')
                or self._task.get('_flow_phase', 'working'))
            self._task['_flow_iteration'] = event.get('iteration', 0)
            self._task['_flow_current_turn'] = {
                key: event[key]
                for key in _TURN_META_KEYS
                if event.get(key) is not None
            }
            self._task['toolRounds'] = []
            self._reset_turn_content()
        elif event_type in _TOOL_EVENTS:
            self._apply_tool_event(event)
        elif event_type == 'delta':
            self._append_delta(event)
        elif event_type in _FINAL_TURN_EVENTS:
            self._finalize_turn(event)
        self._append_event(self._task, event)


__all__ = ['OrchestrationChatTaskEventSink']
