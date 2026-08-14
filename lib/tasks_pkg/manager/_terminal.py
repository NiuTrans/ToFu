"""Shared terminal projection for chat-task failures.

Chat workers use a ``DONE(error)`` event rather than TaskRuntime's generic
``error`` event, and they also owe persistence plus the conversation busy
projection.  Keeping that sequence here prevents each execution mode from
inventing a subtly different terminal state.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.error_envelope import make_envelope, normalize_envelope
from lib.log import get_logger

logger = get_logger(__name__)
_TERMINAL_STATUSES = frozenset({'done', 'error', 'aborted', 'interrupted'})


def stamp_chat_task_terminal(
    task: dict,
    *,
    status: str,
    finish_reason: str,
    endpoint_reason: str = '',
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
    if endpoint_reason or task.get('endpoint_mode') or '_endpoint_phase' in task:
        task['_endpoint_phase'] = 'done'
        task['_endpoint_stop_reason'] = endpoint_reason or finish_reason
    return first


def finalize_chat_task_error(
    task: dict,
    error: Any,
    *,
    endpoint_reason: str = 'error',
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
        from lib.tasks_pkg.manager._registry import notify_terminal_busy_state
        notify_terminal_fn = notify_terminal_busy_state

    first = stamp_chat_task_terminal(
        task,
        status='error',
        finish_reason='error',
        endpoint_reason=endpoint_reason,
    )
    if not first:
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
        persist_task_result_fn(task)
    except Exception as exc:
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


__all__ = ['finalize_chat_task_error', 'stamp_chat_task_terminal']
