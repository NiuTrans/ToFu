"""Project Flow-backed chat events onto the live task state.

The flow event adapter owns engine-event → chat-wire translation. This
sink owns the next boundary: applying those wire events to the in-memory task
snapshot used by reconnect/poll paths, then forwarding the unchanged event to
the task event log. Execution and persistence orchestration stay outside.
"""

from __future__ import annotations

from contextlib import nullcontext, contextmanager
from lib.agent_core.publication import publication_commit, check_publication
from typing import Callable
from lib.orchestration.graph import FlowEventDeliveryError


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

    @contextmanager
    def _content_lock(self):
        with self._task.get('content_lock') or nullcontext():
            with publication_commit():
                yield

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
                        'roundNum', 'llmRound', 'agentId', 'toolCallId',
                        'toolName', 'toolArgs', 'query', 'status',
                        'attentionKind', 'parentToolCallId', 'tStart',
                        'inspectorRef', 'assistantContent', 'thinking',
                        'thinkingSignature', 'attemptId', 'taskId',
                    )
                    if event.get(key) is not None
                }
                # A relayed tool_start without a verdict is a RUNNING tool.
                # The canonical dispatcher stamps ``status='searching'`` at
                # announce time; without the same pending marker, every Turn
                # projection fold mirrored the round as
                # ``result: {content: None, status: None}`` — a shape the
                # generated snapshot contract rejects, which 500'd the whole
                # conversation on reload (2026-09-20, conv mu5p3vn9x32wcd).
                candidate.setdefault('status', 'searching')
                if position < 0:
                    rounds.append(candidate)
                else:
                    rounds[position].update(candidate)
            elif position >= 0:
                target = rounds[position]

                if isinstance(event.get('inspectorRef'), dict):
                    target.setdefault('inspectorRef', dict(event['inspectorRef']))
                if event_type == 'tool_result':
                    target['results'] = event.get('results')
                    target['status'] = event.get('status') or 'done'
                elif event_type == 'tool_progress':
                    for key in (
                        'detail', 'chunk', 'seq', 'spooling', 'truncated',
                        'terminalReason', 'execStartTs', 'deadlineTs',
                        'execTimingAtMs', 'execElapsedMs', 'deadlineRemainingMs',
                    ):
                        if event.get(key) is not None:
                            target[key] = event[key]
                elif event_type == 'tool_complete':
                    target['toolContent'] = event.get('toolContent')
                    if event.get('toolModelContent') is not None:
                        target['toolModelContent'] = event['toolModelContent']
                    target['status'] = event.get('status') or (
                        'error' if event.get('isError') else 'done')
                elif event_type == 'tool_compacted':
                    if event.get('compactedContent') is not None:
                        target['toolContent'] = event['compactedContent']
                        target.pop('toolModelContent', None)
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

        check_publication()
        event_type = str(event.get('type') or '')
        if self._task.get('_flowTurnStartFailed') and event_type != 'flow_iteration':
            raise RuntimeError('Flow phase has no writable Turn authority')
        if event_type == 'flow_iteration':
            from lib.orchestration.chat_live_turn import begin_live_flow_turn
            try:
                begin_live_flow_turn(self._task, event)
                self._task.pop('_flowTurnStartFailed', None)
            except Exception as exc:
                # A missing display authority must stop dispatch, never fall
                # back to writing a later node over the committed root.
                abort = self._task.get('abort_event')
                if abort is not None:
                    abort.set()
                self._task['_flowTurnStartFailed'] = True
                raise FlowEventDeliveryError(
                    f'Could not admit the next Goal turn: {exc}') from exc
            self._task['_flow_phase'] = (
                event.get('phase')
                or self._task.get('_flow_phase', 'working'))
            self._task['_flow_iteration'] = event.get('iteration', 0)
            self._task['_flow_current_turn'] = {
                key: event[key]
                for key in _TURN_META_KEYS
                if event.get(key) is not None
            }
            # Admission must succeed before clearing the previous output.
            # Each invocation starts with an empty structural carrier too;
            # inheriting segments would attach another phase's tool history.
            self._task['toolRounds'] = []
            self._task.pop('segments', None)
            self._reset_turn_content()
        elif event_type == 'state':
            # The invocation checkpoint is already canonical and ordered.
            # Only output fields cross: a child cannot terminate the parent.
            with self._content_lock():
                for key in ('content', 'thinking', 'toolRounds', 'segments'):
                    if key in event:
                        self._task[key] = event[key]

            # This is an in-process value handoff, not a replay frame. The
            # following structural event/final checkpoint persists its patch
            # once through Turn authority, without O(rounds²) state events.
            return
        elif event_type in {'delta_reset', 'retry_reset'}:
            self._reset_turn_content()
        elif event_type in _TOOL_EVENTS:
            self._apply_tool_event(event)
        elif event_type == 'delta':
            self._append_delta(event)
        elif event_type in _FINAL_TURN_EVENTS:
            self._finalize_turn(event)
        check_publication()
        self._append_event(self._task, event)


__all__ = ['OrchestrationChatTaskEventSink']
