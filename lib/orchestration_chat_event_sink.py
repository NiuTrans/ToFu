"""Project Flow-backed chat events onto the live task state.

The endpoint event adapter owns engine-event → chat-wire translation. This
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
    'endpoint_planner_done',
    'endpoint_critic_msg',
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
        if event_type == 'endpoint_iteration':
            self._task['_endpoint_phase'] = (
                event.get('phase')
                or self._task.get('_endpoint_phase', 'working'))
            self._task['_endpoint_iteration'] = event.get('iteration', 0)
            self._task['_flow_current_turn'] = {
                key: event[key]
                for key in _TURN_META_KEYS
                if event.get(key) is not None
            }
            self._reset_turn_content()
        elif event_type == 'delta':
            self._append_delta(event)
        elif event_type in _FINAL_TURN_EVENTS:
            self._finalize_turn(event)
        self._append_event(self._task, event)


__all__ = ['OrchestrationChatTaskEventSink']
