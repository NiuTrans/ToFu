"""Focused executable specs for the authoritative ``cancel_task`` seam.

``lib.tasks_pkg.manager.cancellation.cancel_task`` is the single owner-scoped
cancellation authority that the HTTP adapters are converging on. These specs
pin its transport-neutral receipt and its fanout to parallel scoped contexts
(owner-mismatch and missing tasks must remain indistinguishable to callers).

The tests register real ``chat_task_runtime`` tasks and clean them up again,
so they exercise the exact code path the routes will delegate to.
"""

from __future__ import annotations

import pytest

from lib.tasks_pkg.manager.runtime import chat_task_runtime
from lib.tasks_pkg.tool_runtime import context_for_task

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _cleanup_runtime_tasks():
    before = set(chat_task_runtime.task_ids())
    yield
    for task_id in set(chat_task_runtime.task_ids()) - before:
        chat_task_runtime.discard(task_id)


def _make_task(task_id: str, *, user_id: int = 1) -> dict:
    task = chat_task_runtime.create(user_id=user_id, task_id=task_id)
    chat_task_runtime.mark_running(task_id)
    return task


def test_cancel_task_missing_task_is_indistinguishable():
    from lib.tasks_pkg.manager.cancellation import cancel_task

    receipt = cancel_task('missing-task', user_id=1, source='test')
    assert receipt == {'found': False, 'task': None, 'alreadyCancelled': False}


def test_cancel_task_owner_mismatch_is_indistinguishable_from_missing():
    from lib.tasks_pkg.manager.cancellation import cancel_task

    _make_task('owner-a', user_id=1)
    receipt = cancel_task('owner-a', user_id=2, source='test')
    assert receipt['found'] is False
    assert receipt['task'] is None


def test_cancel_task_signals_and_fans_out_to_parallel_contexts():
    from lib.tasks_pkg.manager.cancellation import cancel_task

    task = _make_task('cancel-live')
    calls: list[int] = []
    for i in range(3):
        ctx = context_for_task(
            task,
            round_num=i + 1,
            tool_call_id=f'c{i}',
            tool_name='run_command',
            round_entry={'toolCallId': f'c{i}'},
        )
        ctx.register_cancel_callback((lambda i=i: calls.append(i)))

    receipt = cancel_task(task['id'], user_id=1, source='test-source')

    assert receipt['found'] is True
    assert receipt['task'] is task
    assert receipt['alreadyCancelled'] is False
    assert receipt['signalled'] is True
    assert receipt['resourceCallbacks'] == 3
    assert receipt['resourceCallbackErrors'] == 0
    assert task['aborted'] is True
    assert task['abort_event'].is_set()
    assert task['_abortSource'] == 'test-source'
    assert '_abort_timestamp' in task
    assert sorted(calls) == [0, 1, 2]


def test_cancel_task_second_call_does_not_reinvoke_callbacks():
    from lib.tasks_pkg.manager.cancellation import cancel_task

    task = _make_task('cancel-twice')
    calls: list[int] = []
    ctx = context_for_task(
        task, round_num=1, tool_call_id='c', tool_name='run_command',
        round_entry={})
    ctx.register_cancel_callback(lambda: calls.append(1))

    first = cancel_task(task['id'], user_id=1, source='test')
    assert first['resourceCallbacks'] == 1

    second = cancel_task(task['id'], user_id=1, source='test')
    assert second['resourceCallbacks'] == 0
    assert calls == [1]


def test_cancel_task_already_aborted_reports_already_cancelled():
    from lib.tasks_pkg.manager.cancellation import cancel_task

    task = _make_task('cancel-again')
    task['aborted'] = True

    receipt = cancel_task(task['id'], user_id=1, source='test')
    assert receipt['found'] is True
    assert receipt['alreadyCancelled'] is True
