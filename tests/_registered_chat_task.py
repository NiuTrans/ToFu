"""Test-only ownership wrapper for code paths that emit chat task events.

Production event sequencing is owned by ``TaskRuntime``.  Focused unit tests
that construct task dictionaries directly must therefore register those
dictionaries explicitly instead of relying on the retired detached-event
fallback.
"""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Iterator


@contextmanager
def registered_chat_task(task: dict, *, user_id: int = 1) -> Iterator[dict]:
    """Register one transient task for a unit-test event-emission window."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    task['_userId'] = user_id
    task['_transientRuntime'] = True
    previous_status = task.get('status')
    if previous_status in ('done', 'error', 'aborted'):
        task['status'] = 'running'
    assert chat_task_runtime.get_owned(task['id'], user_id=user_id) is None
    assert chat_task_runtime.adopt(task)
    assert chat_task_runtime.get_owned(task['id'], user_id=user_id) is task
    if previous_status is not None:
        task['status'] = previous_status
    try:
        yield task
    finally:
        assert chat_task_runtime.remove_owned(task['id'], user_id=user_id)
