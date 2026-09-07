"""Authoritative owner-scoped cancellation for live chat tasks.

Transport adapters delegate here. The existing TaskRuntime abort event remains
the cancellation authority; per-tool contexts only fan that signal out to
resources such as subprocess groups.
"""

from __future__ import annotations

import time
from typing import Any

from lib.log import get_logger
from lib.task_replay import TASK_REPLAY_TERMINAL_STATUSES
from lib.tasks_pkg.manager.runtime import chat_task_runtime
from lib.tasks_pkg.tool_runtime import cancel_task_contexts

logger = get_logger(__name__)


def cancel_task(task_id: str, *, user_id: int,
                source: str = 'unknown') -> dict[str, Any]:
    """Cancel one owned task and every currently scoped runtime resource.

    Returns a transport-neutral receipt. Missing and owner-mismatched tasks are
    deliberately indistinguishable to callers.
    """
    from lib.identity import require_user_id

    owner_user_id = require_user_id(user_id, context='cancel chat task owner')
    task = chat_task_runtime.get_owned(task_id, user_id=owner_user_id)
    if task is None:
        return {'found': False, 'task': None, 'alreadyCancelled': False}


    status = str(task.get('status') or '')
    if status in TASK_REPLAY_TERMINAL_STATUSES:
        return {
            'found': True,
            'task': task,
            'alreadyCancelled': False,
            'signalled': False,
            'queuedCancelled': False,
            'resourceCallbacks': 0,
            'resourceCallbackErrors': 0,
        }
    was_pending = str(task.get('status') or '') == 'pending'
    already_cancelled = bool(task.get('aborted')) or bool(
        task.get('abort_event') and task['abort_event'].is_set())
    signalled = chat_task_runtime.abort_owned(task_id, user_id=owner_user_id)
    task['aborted'] = True
    task.setdefault('_abort_timestamp', time.time())
    task.setdefault('_abortSource', str(source or 'unknown'))
    # Keep update_fields for TaskRuntime implementations that snapshot fields
    # independently of the dict returned by get_owned.
    chat_task_runtime.update_fields(
        task_id,
        fields={
            'aborted': True,
            '_abort_timestamp': task['_abort_timestamp'],
            '_abortSource': task['_abortSource'],
        },
    )

    callbacks, callback_errors = cancel_task_contexts(task)
    if callback_errors:
        logger.warning(
            '[Task %s] %d scoped cancellation callback(s) failed',
            task_id[:8], len(callback_errors), exc_info=callback_errors[0])

    queued_cancelled = False
    if was_pending:
        from lib.tasks_pkg.spawn import cancel_queued_task
        queued_cancelled = bool(cancel_queued_task(task_id))
        if queued_cancelled:
            from lib.tasks_pkg.manager import finalize_chat_task_aborted
            finalize_chat_task_aborted(task)

    conv_id = str(task.get('convId') or '')
    if conv_id:
        try:
            from lib.conversations.change_notifications import notify_conv_changed
            notify_conv_changed(conv_id, rev=None, user_id=owner_user_id)
        except Exception as exc:
            logger.warning('[Task %s] cancel busy-notify failed: %s',
                           task_id[:8], exc)

    return {
        'found': True,
        'task': task,
        'alreadyCancelled': already_cancelled,
        'signalled': bool(signalled),
        'queuedCancelled': queued_cancelled,
        'resourceCallbacks': callbacks,
        'resourceCallbackErrors': len(callback_errors),
    }


__all__ = ['cancel_task']
