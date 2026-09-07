"""Per-tool structured runtime context.

One context belongs to one tool-call occurrence. It wraps the chat task's
existing abort state and owns only resources scoped to that call; detached
workers keep their independent lifecycle.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CancelCallback = Callable[[], Any]


class ToolCancelled(RuntimeError):
    """Raised at a cooperative boundary after task cancellation is requested."""


@dataclass
class ToolExecutionContext:
    """Structured state and cleanup authority for one tool-call occurrence."""

    task: dict[str, Any]
    round_num: int
    tool_call_id: str
    tool_name: str
    owner_user_id: int
    round_entry: dict[str, Any]
    deadline_monotonic: float | None = None
    started_monotonic: float = field(default_factory=time.monotonic)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _cancel_callbacks: dict[int, CancelCallback] = field(default_factory=dict, repr=False)
    _next_callback_id: int = field(default=1, repr=False)
    _terminal_state: str | None = field(default=None, repr=False)
    _progress_sink: Any = field(default=None, repr=False)

    @property
    def task_id(self) -> str:
        return str(self.task.get('id') or '')

    @property
    def abort_event(self) -> threading.Event:
        event = self.task.get('abort_event')
        if isinstance(event, threading.Event):
            return event
        # Compatibility for detached/non-chat task proxies. This is attached to
        # the task itself rather than becoming a second runtime authority.
        lock = self.task.setdefault('_toolRuntimeLock', threading.RLock())
        with lock:
            event = self.task.get('abort_event')
            if isinstance(event, threading.Event):
                return event
            event = threading.Event()
            if self.task.get('aborted'):
                event.set()
            self.task['abort_event'] = event
            return event

    @property
    def cancellation_requested(self) -> bool:
        return bool(self.task.get('aborted')) or self.abort_event.is_set()

    @property
    def deadline_exceeded(self) -> bool:
        return (
            self.deadline_monotonic is not None
            and time.monotonic() >= self.deadline_monotonic
        )

    @property
    def terminal_state(self) -> str | None:
        with self._lock:
            return self._terminal_state

    def register_cancel_callback(self, callback: CancelCallback) -> Callable[[], None]:
        """Register one scoped callback and return an idempotent unregister."""
        if not callable(callback):
            raise TypeError('cancel callback must be callable')
        invoke_now = False
        with self._lock:
            callback_id = self._next_callback_id
            self._next_callback_id += 1
            if self.cancellation_requested:
                invoke_now = True
            else:
                self._cancel_callbacks[callback_id] = callback
        if invoke_now:
            callback()

        def unregister() -> None:
            with self._lock:
                self._cancel_callbacks.pop(callback_id, None)

        return unregister

    def request_resource_cancellation(self) -> tuple[int, tuple[Exception, ...]]:
        """Invoke every currently registered scoped callback at most once."""
        with self._lock:
            callbacks = tuple(self._cancel_callbacks.values())
            self._cancel_callbacks.clear()
        failures: list[Exception] = []
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:  # cancellation must fan out best-effort
                failures.append(exc)
        return len(callbacks), tuple(failures)

    def bind_progress_sink(self, sink: Any) -> None:
        with self._lock:
            self._progress_sink = sink

    def publish_progress(self, stream: str, text: str, **fields: Any) -> None:
        sink = self._progress_sink
        if sink is not None:
            sink.publish(stream, text, **fields)

    def open_output_writer(self, **kwargs: Any):
        from lib.tool_result_artifact_writer import (
            TaskArtifactBudget,
            ToolResultArtifactWriter,
            resolve_task_quota_bytes,
        )

        lock = self.task.setdefault('_toolRuntimeLock', threading.RLock())
        with lock:
            budget = self.task.get('_toolArtifactBudget')
            if not isinstance(budget, TaskArtifactBudget):
                budget = TaskArtifactBudget(resolve_task_quota_bytes())
                self.task['_toolArtifactBudget'] = budget
        return ToolResultArtifactWriter(
            user_id=self.owner_user_id, task_budget=budget, **kwargs)

    def settle_once(self, state: str) -> bool:
        """Record one terminal state; subsequent attempts are harmless no-ops."""
        with self._lock:
            if self._terminal_state is not None:
                return False
            self._terminal_state = str(state)
            return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            callbacks = len(self._cancel_callbacks)
            terminal = self._terminal_state
        return {
            'taskId': self.task_id,
            'roundNum': self.round_num,
            'toolCallId': self.tool_call_id,
            'toolName': self.tool_name,
            'ownerUserId': self.owner_user_id,
            'startedMonotonic': self.started_monotonic,
            'deadlineMonotonic': self.deadline_monotonic,
            'cancellationRequested': self.cancellation_requested,
            'registeredResources': callbacks,
            'terminalState': terminal,
        }


def context_for_task(task: dict[str, Any], *, round_num: int,
                     tool_call_id: str, tool_name: str,
                     round_entry: dict[str, Any]) -> ToolExecutionContext:
    """Construct and register one context on the parent task."""
    from lib.identity import require_user_id

    context = ToolExecutionContext(
        task=task,
        round_num=int(round_num),
        tool_call_id=str(tool_call_id or ''),
        tool_name=str(tool_name or ''),
        owner_user_id=require_user_id(
            task.get('_userId'), context='tool execution owner'),
        round_entry=round_entry,
    )
    lock = task.setdefault('_toolRuntimeLock', threading.RLock())
    with lock:
        active = task.setdefault('_activeToolContexts', {})
        # Call IDs are batch-local and providers may recycle them. The round
        # occurrence is part of the process-local key.
        key = (int(round_num), str(tool_call_id or ''), id(round_entry))
        context._task_registry_key = key  # local implementation detail
        active[key] = context
    return context


def active_context_for_call(
    task: dict[str, Any], *, round_num: int, tool_call_id: str,
    round_entry: dict[str, Any],
) -> ToolExecutionContext | None:
    """Return the registered context for one active call occurrence."""
    lock = task.get('_toolRuntimeLock')
    if lock is None:
        return None
    key = (int(round_num), str(tool_call_id or ''), id(round_entry))
    with lock:
        active = task.get('_activeToolContexts')
        if not isinstance(active, dict):
            return None
        context = active.get(key)
    return context if isinstance(context, ToolExecutionContext) else None


def unregister_context(context: ToolExecutionContext) -> None:
    task = context.task
    lock = task.get('_toolRuntimeLock')
    if lock is None:
        return
    with lock:
        active = task.get('_activeToolContexts')
        if isinstance(active, dict):
            active.pop(getattr(context, '_task_registry_key', None), None)
            if not active:
                task.pop('_activeToolContexts', None)


def cancel_task_contexts(task: dict[str, Any]) -> tuple[int, tuple[Exception, ...]]:
    """Fan task cancellation out to a stable snapshot of scoped contexts."""
    lock = task.get('_toolRuntimeLock')
    if lock is None:
        return 0, ()
    with lock:
        active = task.get('_activeToolContexts')
        contexts = tuple(active.values()) if isinstance(active, dict) else ()
    invoked = 0
    failures: list[Exception] = []
    for context in contexts:
        count, errors = context.request_resource_cancellation()
        invoked += count
        failures.extend(errors)
    return invoked, tuple(failures)


__all__ = [
    'ToolCancelled', 'ToolExecutionContext', 'active_context_for_call',
    'cancel_task_contexts', 'context_for_task', 'unregister_context',
]
