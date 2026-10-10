"""Shared terminal projection for chat-task failures.

Chat workers use a ``DONE(error)`` event rather than TaskRuntime's generic
``error`` event, and they also owe persistence plus the conversation busy
projection.  Keeping that sequence here prevents each execution mode from
inventing a subtly different terminal state.
"""

from __future__ import annotations

import time
import threading
from collections.abc import Callable
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.error_envelope import make_envelope, normalize_envelope
from lib.log import get_logger

logger = get_logger(__name__)
_TERMINAL_STATUSES = frozenset({'done', 'error', 'aborted', 'interrupted'})


# Only in-process service adapters register here. Callbacks must enqueue bounded
# follow-up work; they must not run model calls, commands or long I/O inline.
_TERMINAL_LISTENERS: list[Callable[[dict], Any]] = []
_TERMINAL_LISTENER_LOCK = threading.Lock()
_TERMINAL_LISTENER_CHANGED = threading.Condition(_TERMINAL_LISTENER_LOCK)
_TERMINAL_CALLBACK_CONTEXT = threading.local()


class _TerminalListenerRegistration:
    """One registration generation; withdrawn snapshots cannot claim it."""

    def __init__(self, listener):
        self.listener = listener
        self.active = True
        self.invocations: dict[int, int] = {}


_TERMINAL_REGISTRATIONS: dict[int, _TerminalListenerRegistration] = {}
_TERMINAL_WITHDRAWN: set[_TerminalListenerRegistration] = set()


def _sync_terminal_registrations() -> None:
    """Keep the retained fault-injection callable list and lifecycle state aligned."""
    live = {id(listener) for listener in _TERMINAL_LISTENERS}
    for key in tuple(_TERMINAL_REGISTRATIONS):
        if key not in live:
            registration = _TERMINAL_REGISTRATIONS.pop(key)
            registration.active = False
            if registration.invocations:
                _TERMINAL_WITHDRAWN.add(registration)
    for listener in _TERMINAL_LISTENERS:
        if id(listener) not in _TERMINAL_REGISTRATIONS:
            _TERMINAL_REGISTRATIONS[id(listener)] = _TerminalListenerRegistration(listener)


def register_terminal_listener(listener: Callable[[dict], Any]) -> bool:
    if not callable(listener):
        raise TypeError('terminal listener must be callable')
    with _TERMINAL_LISTENER_LOCK:
        _sync_terminal_registrations()
        if listener in _TERMINAL_LISTENERS:
            return False
        _TERMINAL_LISTENERS.append(listener)
        _TERMINAL_REGISTRATIONS[id(listener)] = _TerminalListenerRegistration(listener)
        return True


def unregister_terminal_listener(listener: Callable[[dict], Any]) -> bool:
    """Withdraw an observer and join its running callbacks.

    A callback may withdraw observers without joining any invocation: waiting there
    could deadlock with another callback withdrawing this observer. External
    service teardown joins all invocations before releasing its dependencies.
    """
    with _TERMINAL_LISTENER_CHANGED:
        _sync_terminal_registrations()
        registered = next((item for item in _TERMINAL_LISTENERS
                           if item == listener), None)
        changed = registered is not None
        if registered is not None:
            _TERMINAL_LISTENERS.remove(registered)
            registration = _TERMINAL_REGISTRATIONS.pop(id(registered))
            registration.active = False
            if registration.invocations:
                _TERMINAL_WITHDRAWN.add(registration)
        draining = tuple(item for item in _TERMINAL_WITHDRAWN
                         if item.listener == listener)
        if not getattr(_TERMINAL_CALLBACK_CONTEXT, 'depth', 0):
            _TERMINAL_LISTENER_CHANGED.wait_for(
                lambda: all(not item.invocations for item in draining))
        return changed


def notify_terminal_listeners(task: dict) -> None:
    """Publish an already-owned durable terminal write once per execution.

    Persistence failures, recovery-fenced writers, transient work and nonterminal
    checkpoints never enter this seam. Failed observers cannot roll back a commit.
    """
    if task.get('status') not in _TERMINAL_STATUSES:
        return
    from lib.agent_core.terminal_delivery import deliver_terminal
    deliver_terminal(task)
    if task.get('_transientRuntime'):
        return
    receipt = (task.get('id'), task.get('_attemptId'), task.get('finished_at'),
               task.get('status'))
    with _TERMINAL_LISTENER_LOCK:
        _sync_terminal_registrations()
        if not _TERMINAL_LISTENERS or task.get('_terminalListenerReceipt') == receipt:
            return
        task['_terminalListenerReceipt'] = receipt
        registrations = tuple(_TERMINAL_REGISTRATIONS[id(listener)]
                              for listener in _TERMINAL_LISTENERS)
    thread_id = threading.get_ident()
    for registration in registrations:
        with _TERMINAL_LISTENER_LOCK:
            if not registration.active:
                continue
            registration.invocations[thread_id] = registration.invocations.get(thread_id, 0) + 1
        _TERMINAL_CALLBACK_CONTEXT.depth = getattr(_TERMINAL_CALLBACK_CONTEXT, 'depth', 0) + 1
        try:
            registration.listener(task)
        except Exception as exc:
            logger.warning('[Task %s] terminal observer failed: %s',
                           str(task.get('id') or '')[:8], type(exc).__name__)
        finally:
            _TERMINAL_CALLBACK_CONTEXT.depth -= 1
            if not _TERMINAL_CALLBACK_CONTEXT.depth:
                del _TERMINAL_CALLBACK_CONTEXT.depth
            with _TERMINAL_LISTENER_CHANGED:
                remaining = registration.invocations[thread_id] - 1
                if remaining:
                    registration.invocations[thread_id] = remaining
                else:
                    del registration.invocations[thread_id]
                if not registration.invocations:
                    _TERMINAL_WITHDRAWN.discard(registration)
                _TERMINAL_LISTENER_CHANGED.notify_all()


def stamp_chat_task_terminal(
    task: dict,
    *,
    status: str,
    finish_reason: str,
    flow_reason: str = '',
) -> bool:
    """Atomically stamp terminal facts without allowing terminal rewrites.

    Returns ``True`` only for the first terminal transition. Repeating the
    same transition merely heals missing timestamp/phase fields; attempting
    to replace one terminal outcome with another is rejected unchanged.
    """
    current = str(task.get('status') or '')
    stamped = bool(task.get('_chat_terminal_stamped'))
    if current in _TERMINAL_STATUSES and current != status:
        logger.warning(
            '[Task %s] rejected terminal rewrite %s -> %s',
            (task.get('id') or '?')[:8], current, status,
        )
        return False
    first = not stamped
    if stamped and task.get('finishReason') not in (None, '', finish_reason):
        logger.warning(
            '[Task %s] rejected finish-reason rewrite %s -> %s',
            (task.get('id') or '?')[:8], task.get('finishReason'), finish_reason,
        )
        return False
    task['status'] = status
    task['finishReason'] = finish_reason
    task['_chat_terminal_stamped'] = True
    if not task.get('finished_at'):
        task['finished_at'] = time.time()
    if flow_reason or task.get('flow_mode') or '_flow_phase' in task:
        task['_flow_phase'] = 'done'
        task['_flow_stop_reason'] = flow_reason or finish_reason
    return first


def finalize_chat_task_error(
    task: dict,
    error: Any,
    *,
    flow_reason: str = 'error',
    append_event_fn: Callable[[dict, dict], Any] | None = None,
    persist_task_result_fn: Callable[[dict], Any] | None = None,
    notify_terminal_fn: Callable[[dict], Any] | None = None,
) -> dict:
    """Fail one chat task and publish every terminal projection exactly once.

    The dependency hooks keep the boundary independently testable and retain
    the long-standing monkeypatch surface used by endpoint tests.
    """
    if append_event_fn is None:
        from lib.tasks_pkg.manager._events import append_event
        append_event_fn = append_event
    if persist_task_result_fn is None:
        from lib.tasks_pkg.manager._persist import persist_task_result
        persist_task_result_fn = persist_task_result
    if notify_terminal_fn is None:
        from lib.tasks_pkg.manager._registry import notify_terminal_conversation_change
        notify_terminal_fn = notify_terminal_conversation_change

    persistence_was_pending = bool(task.get('_terminalPersistencePending'))
    task['_terminalPersistencePending'] = True
    first = stamp_chat_task_terminal(
        task,
        status='error',
        finish_reason='error',
        flow_reason=flow_reason,
    )
    if not first:
        if not persistence_was_pending:
            task.pop('_terminalPersistencePending', None)
        return None
    envelope = normalize_envelope(
        error, context='chat-task-terminal', source='chat',
    ) or make_envelope(
        'generic', context='chat-task-terminal', source='chat',
    )
    task['error'] = envelope

    event = build_event(EventType.DONE, error=envelope, finishReason='error')
    if task.get('preset'):
        event['preset'] = task['preset']
    if task.get('model'):
        event['model'] = task['model']

    try:
        append_event_fn(task, event)
    except Exception as exc:
        logger.error(
            '[Task %s] terminal error event failed: %s',
            (task.get('id') or '?')[:8], exc, exc_info=True,
        )
    try:
        persisted = persist_task_result_fn(task)
        if persisted is False:
            task['_terminalPersistencePending'] = True
            task['_terminalPersistenceRetryReady'] = True
    except Exception as exc:
        task['_terminalPersistencePending'] = True
        task['_terminalPersistenceRetryReady'] = True
        logger.error(
            '[Task %s] terminal error persistence failed: %s',
            (task.get('id') or '?')[:8], exc, exc_info=True,
        )
    try:
        notify_terminal_fn(task)
    except Exception as exc:
        logger.debug(
            '[Task %s] terminal busy projection failed: %s',
            (task.get('id') or '?')[:8], exc,
        )
    return event


def finalize_chat_task_aborted(
    task: dict,
    *,
    append_event_fn: Callable[[dict, dict], Any] | None = None,
    persist_task_result_fn: Callable[[dict], Any] | None = None,
    notify_terminal_fn: Callable[[dict], Any] | None = None,
) -> dict | None:
    """Settle a user-cancelled task that never acquired a worker thread."""
    if append_event_fn is None:
        from lib.tasks_pkg.manager._events import append_event
        append_event_fn = append_event
    if persist_task_result_fn is None:
        from lib.tasks_pkg.manager._persist import persist_task_result
        persist_task_result_fn = persist_task_result
    if notify_terminal_fn is None:
        from lib.tasks_pkg.manager._registry import notify_terminal_conversation_change
        notify_terminal_fn = notify_terminal_conversation_change

    task['aborted'] = True
    persistence_was_pending = bool(task.get('_terminalPersistencePending'))
    task['_terminalPersistencePending'] = True
    first = stamp_chat_task_terminal(
        task,
        status='done',
        finish_reason='aborted',
        flow_reason='aborted',
    )
    if not first:
        if not persistence_was_pending:
            task.pop('_terminalPersistencePending', None)
        return None
    event = build_event(EventType.DONE, finishReason='aborted')
    try:
        append_event_fn(task, event)
    except Exception as exc:
        logger.error(
            '[Task %s] queued-abort event failed: %s',
            (task.get('id') or '?')[:8], exc, exc_info=True,
        )
    try:
        persisted = persist_task_result_fn(task)
        if persisted is False:
            task['_terminalPersistencePending'] = True
            task['_terminalPersistenceRetryReady'] = True
    except Exception as exc:
        task['_terminalPersistencePending'] = True
        task['_terminalPersistenceRetryReady'] = True
        logger.error(
            '[Task %s] queued-abort persistence failed: %s',
            (task.get('id') or '?')[:8], exc, exc_info=True,
        )
    try:
        notify_terminal_fn(task)
    except Exception as exc:
        logger.debug(
            '[Task %s] queued-abort busy projection failed: %s',
            (task.get('id') or '?')[:8], exc,
        )
    return event


def reject_unstarted_chat_task(
    task: dict,
    error: Any,
    *,
    cause: str,
    conv_id: str = '',
) -> dict | None:
    """Settle, durably fail, then unregister work rejected before spawn.

    ``create_task`` is durable-at-birth. A route that fails billing,
    admission, resource binding, or synchronous spawn must therefore close
    both authorities: the ExecutionSession and the durable task row. Merely
    returning an HTTP error would strand a pending registry/storage record.
    """
    try:
        from lib.agent_core.execution_session import (
            ExecutionPhase,
            execution_session_for_task,
        )
        session = execution_session_for_task(task)
    except ValueError:
        session = None
    if (session is not None
            and session.dispatch_started
            and not session.is_terminal):
        session.request_cancel('unstarted_rejection_after_dispatch')
        logger.error(
            '[Task %s] refused pre-spawn rejection after dispatch started',
            (task.get('id') or '?')[:8],
        )
        return None
    if session is not None:
        session.settle(ExecutionPhase.FAILED, cause=cause)

    # ``finalize_chat_task_error`` deliberately treats persistence as
    # best-effort for an already-running worker. Pre-spawn rejection is
    # stricter: removing the registry record is safe only after Turn settlement
    # and the terminal task-result row acknowledge. Otherwise retain the
    # terminal task in memory so maintenance can retry instead of exposing a
    # durable ``pending`` orphan after restart.
    task['_terminalPersistencePending'] = True
    from lib.tasks_pkg.manager._persist import persist_task_result
    persisted = False

    def _persist_with_receipt(owner: dict) -> bool:
        nonlocal persisted
        persisted = persist_task_result(owner) is True
        return persisted

    event = finalize_chat_task_error(
        task,
        error,
        flow_reason=cause,
        persist_task_result_fn=_persist_with_receipt,
    )
    if persisted:
        task.pop('_terminalPersistencePending', None)
        task.pop('_terminalPersistenceRetryReady', None)
        from lib.tasks_pkg.manager._registry import discard_task
        discard_task(str(task.get('id') or ''), conv_id)
    else:
        task['_terminalPersistencePending'] = True
        task['_terminalPersistenceRetryReady'] = True
        logger.error(
            '[Task %s] rejected task retained: terminal persistence pending',
            (task.get('id') or '?')[:8],
        )
    return event


__all__ = [
    'finalize_chat_task_aborted',
    'finalize_chat_task_error',
    'reject_unstarted_chat_task',
    'stamp_chat_task_terminal',
]
