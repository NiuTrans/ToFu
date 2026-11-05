"""Publish Flow-backed chat events through the parent's Turn authority.

The flow event adapter owns engine-event → chat-wire translation. This sink
owns the next boundary: phase admission, then forwarding each unchanged event
to the task event log. Live output is never rebuilt from relayed events.

A visible Turn has exactly one live output source: the running child task's
own state, which is what an ordinary chat Turn renders from its task. The
graph runner binds each child through the task's ``_flowLiveOutputPort``; the
sink adopts the bound child's output before every publication and once more
when it finishes. A concurrent sibling (parallel/fanout branch) is refused the
binding, publishes nothing live, and reaches the conversation through its
completed step. Execution and persistence orchestration stay outside.
"""

from __future__ import annotations

from contextlib import nullcontext, contextmanager
import threading

from lib.agent_core.publication import (
    ChildPublicationRevoked, check_publication, publication_commit)
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

class OrchestrationChatTaskEventSink:
    """Callable live-event port for one Flow-backed chat task."""

    def __init__(
        self,
        task: dict,
        append_event: Callable[[dict, dict], None],
    ):
        self._task = task
        self._append_event = append_event
        self._source: dict | None = None
        self._bind_lock = threading.Lock()
        task['_flowLiveOutputPort'] = self.bind_output_source

    @contextmanager
    def _content_lock(self):
        with self._task.get('content_lock') or nullcontext():
            with publication_commit():
                yield

    def _reset_turn_content(self) -> None:
        with self._content_lock():
            self._task['content'] = ''
            self._task['thinking'] = ''

    @contextmanager
    def bind_output_source(self, child: dict):
        """Bind ``child`` as the visible output; yields whether it won."""
        with self._bind_lock:
            bound = self._source is None
            if bound:
                self._source = child
        try:
            yield bound
        finally:
            if bound:
                try:
                    self._hand_off_final_output(child)
                finally:
                    with self._bind_lock:
                        self._source = None

    def _hand_off_final_output(self, child: dict) -> None:
        # Settlement after the last relayed frame (final segment assembly)
        # crosses once, under the child's own publication authority: a
        # disposed child must never overwrite the next phase's output.
        fence = child.get('_invocationPublicationFence')
        if fence is None:
            self._adopt_output()
            return
        try:
            fence.deliver(lambda _event: self._adopt_output(), None)
        except ChildPublicationRevoked:
            pass

    def _adopt_output(self) -> None:
        child = self._source
        if child is None:
            return
        with child.get('content_lock') or nullcontext():
            content = child.get('content') or ''
            thinking = child.get('thinking') or ''
            # Rows are copied: concurrent tool threads mutate the child's.
            rounds = [dict(row) for row in child.get('toolRounds') or ()
                      if isinstance(row, dict)]
            segments = child.get('segments')
            segments = list(segments) if isinstance(segments, list) else None
        with self._content_lock():
            self._task.update(content=content, thinking=thinking, toolRounds=rounds)
            if segments is None:
                self._task.pop('segments', None)
            else:
                self._task['segments'] = segments

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
        elif event_type in _FINAL_TURN_EVENTS:
            self._finalize_turn(event)
        else:
            self._adopt_output()
        check_publication()
        self._append_event(self._task, event)


__all__ = ['OrchestrationChatTaskEventSink']
