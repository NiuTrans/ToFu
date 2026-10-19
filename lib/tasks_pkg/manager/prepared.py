"""Shared chat construction for server-owned, previously prepared headless input.

This port neither authorizes recovery nor submits work. Domain CAS owns those
decisions. The normal factory rebuilds routing/session resources without hooks;
per-identity synchronization prevents replacing a resident carrier while
independent restores start without a global lock deadline.
"""
from contextlib import contextmanager
from copy import deepcopy
import re
import threading

from lib.agent_core.storage_errors import StorageError
from lib.weak_lock_pool import WeakLockPool
from lib.tasks_pkg.manager.runtime import chat_task_runtime

_restore_locks = WeakLockPool(threading.Lock)
_restore_local = threading.local()


@contextmanager
def _restore_identity(task_id):
    """Serialize one globally unique carrier; retire idle synchronization keys."""
    active = getattr(_restore_local, 'active_ids', frozenset())
    if task_id in active:
        raise ValueError('prepared carrier restoration cannot recurse')
    # Keep a strong reference through acquisition: holders and waiters for one
    # task must share this same weakly retained lock, including foreign owners.
    lock = _restore_locks.lock_for(task_id)
    with lock:
        _restore_local.active_ids = active | {task_id}
        try:
            yield
        finally:
            if active:
                _restore_local.active_ids = active
            else:
                del _restore_local.active_ids


def read_interrupted_birth(task_id, *, user_id, birth_version):
    """Require startup retirement of precisely the unmodified pending birth.

The compact owner gate precedes the full read of this birth-sized row. An
administrator-interrupted row is terminal, so further abort/checkpoint writers
cannot change it. Accepted earlier abort signals remain explicit and refuse.
"""
    from lib.storage import get_storage_client
    client = get_storage_client()
    compact = client.query('task_results.replay_get',
                           {'key': task_id, 'user_id': user_id}, deadline=0.5)
    if (not compact or compact.get('task_id') != task_id
            or compact.get('user_id') != user_id or compact.get('conv_id') != ''
            or compact.get('status') != 'interrupted'
            or compact.get('version') != birth_version + 1):
        raise ValueError('startup did not retire the unchanged pending birth')
    row = client.query('record.get', {'namespace': 'task_results', 'key': task_id}, deadline=0.5)
    value = (row or {}).get('value') or {}
    if ((row or {}).get('version') != compact['version']
            or type(value.get('user_id')) is not int
            or value.get('user_id') != user_id or value.get('task_id') != task_id
            or value.get('conv_id') != '' or value.get('status') != 'interrupted'
            or value.get('interruptedReason') not in {'server_restart', 'process_killed', 'manual_restart'}
            or value.get('abort_requested_at') or value.get('abort_source')
            or value.get('content') or value.get('thinking') or value.get('error')
            or value.get('tool_rounds')):
        raise ValueError('retired task has changed, cancellation or execution evidence')
    return compact


def persist_prepared_cancellation(task, *, user_id, source):
    """Do not acknowledge restorable-task cancellation from memory alone."""
    if not (task.get('meta') or {}).get('requireDurableCancellation'):
        return
    from lib.storage import get_storage_client
    result = get_storage_client(write=True).command('task_results.abort',
        {'task_id': task['id'], 'user_id': user_id, 'source': source}, None, deadline=0.5)
    if not isinstance(result, dict) or result.get('signaled') is not True:
        raise StorageError('database_conflict', 'prepared task cancellation was not durably acknowledged')


def restore_prepared_task(task_id, messages, config, *, user_id, input_digest):
    """Reuse only an identical unstarted carrier; otherwise build it once."""
    if (not re.fullmatch(r'prepared-[a-f0-9]{48}', task_id)
            or not re.fullmatch(r'[a-f0-9]{64}', input_digest)
            or type(config.get('userId')) is not int or config.get('userId') != user_id):
        raise ValueError('invalid prepared task identity')
    with _restore_identity(task_id):
        existing = chat_task_runtime.get(task_id)
        if existing is not None:
            if (existing.get('_userId') != user_id or existing.get('status') != 'pending'
                    or existing.get('_workerStartedAt') or existing.get('aborted')
                    or existing.get('_preparedRestorationInputDigest',existing.get('_preparedInputDigest')) != input_digest):
                raise ValueError('prepared carrier identity or lifecycle changed')
            return existing
        from lib.tasks_pkg.manager._registry import _create_chat_task
        task = _create_chat_task('', deepcopy(messages), deepcopy(config),
                                 user_id=user_id, supersede=False,
                                 prepared_task_id=task_id, require_durable_cancellation=True)
        task['_preparedInputDigest'] = input_digest
        task['_preparedRestorationInputDigest'] = input_digest
        return task
