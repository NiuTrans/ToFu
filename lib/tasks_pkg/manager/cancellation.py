"""Authoritative owner-scoped cancellation for live chat tasks.

Transport adapters delegate here. The existing TaskRuntime abort event remains
the cancellation authority; per-tool contexts only fan that signal out to
resources such as subprocess groups.
"""

from __future__ import annotations

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
    signalled = chat_task_runtime.abort_owned(
        task_id, user_id=owner_user_id, source=source, expected_task=task)
    if not signalled:
        # finish() may have claimed settlement after get_owned(). Never
        # rewrite its outcome or cancel resources after that decision.
        return {
            'found': True, 'task': task,
            'alreadyCancelled': already_cancelled, 'signalled': False,
            'queuedCancelled': False, 'resourceCallbacks': 0,
            'resourceCallbackErrors': 0,
        }

    from lib.tasks_pkg.manager.prepared import persist_prepared_cancellation
    persistence_error = None
    try:
        persist_prepared_cancellation(task, user_id=owner_user_id, source=source)
    except Exception as exc:
        # Local cancellation and resource fan-out still finish. A transport
        # error must not claim that this cancellation survives process loss.
        persistence_error = exc

    # A conversation-scoped swarm may originate in an earlier, normally
    # completed turn. Signal it now rather than waiting for this turn's
    # finalizer or for an await_agents call to bridge the new parent.
    from lib.swarm.integration import get_active_session, swarm_key_for

    session = get_active_session(swarm_key_for(task))
    if session is not None and session.user_id == owner_user_id:
        session.abort()

    # Synchronous, signal-only callbacks: no joins, provider/network waits or
    # terminal settlement here. Resource owners perform bounded cleanup in
    # their workers. Never run callbacks under TaskRuntime's authority lock.
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

    if persistence_error is not None:
        raise persistence_error
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
