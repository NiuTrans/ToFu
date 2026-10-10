"""Durable cross-replica abort signal for TaskRuntime workers.

**Why this module exists.** Every ``TaskRuntime`` abort is in-process:
``abort_owned`` sets the task's ``threading.Event`` and the worker polls it.
Behind an N-replica load balancer the abort request can land on a replica
that does NOT own the task — the owner lookup misses and (before this
module) the route returned 404 while the worker kept running. Chat tasks
have their own durable channel (the ``task_results`` tombstone consumed by
``make_task_abort_check``); non-chat runtimes had none.

**Design.** A flag in ``lib/runtime_state_store`` (``kind='task-abort'``,
key = task_id, value = the aborting principal's user_id, bounded TTL) is
the single authority. The route plants it on a registry miss; ONE shared
daemon watcher per process sweeps every registered runtime's live tasks
and delivers the flag through the runtime's own ``abort_owned`` (which
re-checks ownership and settles atomically against ``finish()``).

  * **Personal mode is byte-equivalent.** ``plant_durable_abort`` returns
    False and no watcher thread exists unless the deployment mode is
    ``distributed`` — the durable flag is only meaningful when a request
    can land on the wrong process.
  * **Owner-scoped.** The watcher honors a flag only when its value equals
    the task's ``_userId``; a foreign principal's plant never matches, so
    planting cannot abort someone else's task.
  * **Bounded.** One thread total (not per runtime), flag keyspace bounded
    by abort volume and reclaimed on consume or TTL expiry.
"""
from __future__ import annotations

import threading
import time
import weakref

from lib.log import get_logger

logger = get_logger(__name__)

_FLAG_KIND = 'task-abort'
_FLAG_TTL_S = 900.0
_INTERRUPT_FLAG_KIND = 'cmd-interrupt'
_INTERRUPT_FLAG_TTL_S = 60.0
_WATCH_POLL_S = 2.0

_runtimes: 'weakref.WeakSet' = weakref.WeakSet()
_runtimes_lock = threading.Lock()
_watcher_lock = threading.Lock()
_watcher_thread: threading.Thread | None = None


def _distributed_mode() -> bool:
    from runtime_guards import load_deployment_configuration
    try:
        return load_deployment_configuration().mode == 'distributed'
    except Exception as e:
        logger.debug('[task-abort-signal] deployment probe failed: %s', e)
        return False


def register_runtime(runtime) -> None:
    """Register a TaskRuntime for durable-abort delivery (idempotent)."""
    with _runtimes_lock:
        _runtimes.add(runtime)


def plant_durable_abort(task_id: str, *, user_id: int, source: str) -> bool:
    """Record an abort request for a task this process does not own.

    Returns False in personal mode (registry misses there are genuine 404s)
    and on any store error — callers keep their historical not-found path.
    """
    if not task_id:
        return False
    if not _distributed_mode():
        return False
    try:
        from lib.runtime_state_store import get_store
        get_store().set_value(
            _FLAG_KIND, str(task_id), str(int(user_id)), _FLAG_TTL_S)
    except Exception as e:
        logger.debug('[task-abort-signal] plant failed task=%s: %s',
                     str(task_id)[:8], e)
        return False
    logger.info('[task-abort-signal] ⚠️ durable abort planted for task=%s '
                '(source=%s) — the owning replica delivers it at its next '
                'watch sweep', str(task_id)[:8], source)
    try:
        from lib.log import audit_log as _audit
        _audit('task_durable_abort_planted', task_id=str(task_id),
               source=source)
    except Exception as _ae:
        logger.debug('[task-abort-signal] audit failed: %s', _ae)
    return True


def plant_durable_interrupt(task_id: str, *, user_id: int, source: str) -> bool:
    """Record a PER-COMMAND interrupt for a task this process does not own.

    The watcher delivers it as ``task['_cmd_interrupt']`` (stamped with the
    live ``_subprocess_pid``) on the owning replica, where the run_command
    read loop consumes it exactly like a locally-planted flag. When the
    task has no active command at sweep time the flag is dropped — the
    local path answers ``no_active_command`` without planting anything, so
    a stale cross-replica flag must never hit a LATER command either.
    """
    if not task_id:
        return False
    if not _distributed_mode():
        return False
    try:
        from lib.runtime_state_store import get_store
        get_store().set_value(
            _INTERRUPT_FLAG_KIND, str(task_id), str(int(user_id)),
            _INTERRUPT_FLAG_TTL_S)
    except Exception as e:
        logger.debug('[task-abort-signal] interrupt plant failed task=%s: %s',
                     str(task_id)[:8], e)
        return False
    logger.info('[task-abort-signal] durable cmd-interrupt planted for '
                'task=%s (source=%s)', str(task_id)[:8], source)
    try:
        from lib.log import audit_log as _audit
        _audit('task_durable_interrupt_planted', task_id=str(task_id),
               source=source)
    except Exception as _ae:
        logger.debug('[task-abort-signal] audit failed: %s', _ae)
    return True


def ensure_watcher() -> None:
    """Start the shared watcher thread (distributed mode only; idempotent)."""
    global _watcher_thread
    if not _distributed_mode():
        return
    with _watcher_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            return
        _watcher_thread = threading.Thread(
            target=_watch_loop, name='task-abort-signal', daemon=True)
        _watcher_thread.start()


def _sweep_once() -> None:
    from lib.runtime_state_store import get_store
    store = get_store()
    with _runtimes_lock:
        runtimes = list(_runtimes)
    for rt in runtimes:
        for task in rt.list_running():
            task_id = str(task.get('id') or '')
            if not task_id:
                continue
            try:
                value = store.get_value(_FLAG_KIND, task_id)
            except Exception as e:
                logger.debug('[task-abort-signal] probe failed task=%s: %s',
                             task_id[:8], e)
                continue
            if value is None:
                continue
            owner = int(task.get('_userId') or 0)
            if owner < 1 or value != str(owner):
                continue
            try:
                store.delete_value(_FLAG_KIND, task_id)
            except Exception as e:
                logger.debug('[task-abort-signal] flag delete failed '
                             'task=%s: %s', task_id[:8], e)
            rt.abort_owned(
                task_id, user_id=owner, source='durable_abort_signal')
        # Per-command interrupt: same flag mechanics, delivered as
        # task['_cmd_interrupt'] for the run_command read loop.
        for task in rt.list_running():
            task_id = str(task.get('id') or '')
            if not task_id:
                continue
            try:
                value = store.get_value(_INTERRUPT_FLAG_KIND, task_id)
            except Exception as e:
                logger.debug('[task-abort-signal] interrupt probe failed '
                             'task=%s: %s', task_id[:8], e)
                continue
            if value is None:
                continue
            owner = int(task.get('_userId') or 0)
            if owner < 1 or value != str(owner):
                continue  # foreign plant — never honored, TTL reclaims
            try:
                store.delete_value(_INTERRUPT_FLAG_KIND, task_id)
            except Exception as e:
                logger.debug('[task-abort-signal] interrupt flag delete '
                             'failed task=%s: %s', task_id[:8], e)
            pid = task.get('_subprocess_pid')
            if not pid:
                continue  # no active command — mirrors no_active_command
            rt.update_fields(
                task_id,
                fields={'_cmd_interrupt': {
                    'source': 'user', 'ts': time.time(), 'note': '',
                    'pid': pid,
                }},
                # Same envelope as list_running(): the pid guard above is
                # the real stale-interrupt protection.
                only_if_status=('pending', 'running'),
            )


def _watch_loop() -> None:
    while True:
        try:
            _sweep_once()
        except Exception as e:
            logger.debug('[task-abort-signal] sweep failed: %s', e)
        time.sleep(_WATCH_POLL_S)


__all__ = ['register_runtime', 'plant_durable_abort',
           'plant_durable_interrupt', 'ensure_watcher']
