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


@pytest.mark.parametrize('terminalizing', [False, True])
def test_cancel_rejected_settlement_does_not_mutate_or_fan_out(monkeypatch, terminalizing):
    from lib.tasks_pkg.manager.cancellation import cancel_task

    task = _make_task('cancel-settlement')
    calls = []
    ctx = context_for_task(task, round_num=1, tool_call_id='c',
                           tool_name='run_command', round_entry={})
    ctx.register_cancel_callback(lambda: calls.append('cancel'))
    original = chat_task_runtime.abort_owned

    def settlement_wins(task_id, **kwargs):
        if terminalizing:
            task['_executionTerminalizing'] = True
        else:
            task['status'] = 'done'
        return original(task_id, **kwargs)

    monkeypatch.setattr(chat_task_runtime, 'abort_owned', settlement_wins)
    try:
        receipt = cancel_task(task['id'], user_id=1)
        assert receipt['signalled'] is False
        assert calls == []
        assert not task.get('aborted')
        assert not task['abort_event'].is_set()
        assert '_abort_timestamp' not in task
        assert '_abortSource' not in task
    finally:
        task['_executionTerminalizing'] = False


def test_cancel_does_not_signal_replacement_registry_task(monkeypatch):
    from lib.tasks_pkg.manager.cancellation import cancel_task

    task = _make_task('cancel-replaced')
    original = chat_task_runtime.abort_owned
    replacement = []

    def replace_before_signal(task_id, **kwargs):
        chat_task_runtime.discard(task_id)
        replacement.append(_make_task(task_id))
        return original(task_id, **kwargs)

    monkeypatch.setattr(chat_task_runtime, 'abort_owned', replace_before_signal)
    assert cancel_task(task['id'], user_id=1)['signalled'] is False
    assert not replacement[0]['abort_event'].is_set()
    assert not task['abort_event'].is_set()


@pytest.mark.parametrize('dequeued', [False, True])
def test_pending_cancel_only_finalizes_when_queue_removal_wins(monkeypatch, dequeued):
    from lib.tasks_pkg.manager.cancellation import cancel_task

    task = chat_task_runtime.create(user_id=1, task_id='pending-cancel')
    finalized = []
    monkeypatch.setattr('lib.tasks_pkg.spawn.cancel_queued_task', lambda tid: dequeued)
    monkeypatch.setattr('lib.tasks_pkg.manager.finalize_chat_task_aborted',
                        lambda task: finalized.append(task))
    receipt = cancel_task(task['id'], user_id=1)
    assert receipt['queuedCancelled'] is dequeued
    assert task['abort_event'].is_set()
    assert finalized == ([task] if dequeued else [])


@pytest.mark.parametrize('task_id', ['', 'registry-miss'])
def test_v3_registry_miss_preserves_owner_tombstone_and_projection(monkeypatch, task_id):
    import lib.turn_lifecycle as lifecycle
    from types import SimpleNamespace

    row = {'attemptId': 'attempt', 'taskId': task_id, 'status': 'running',
           'conversationId': 'conv', 'turnId': 'turn'}
    tombstones, events, revoked = [], [], []
    monkeypatch.setattr('lib.project_dispatch.recovery.revoke_conversation_dispatch',
                        lambda cid, **kwargs: revoked.append((cid, kwargs)))
    monkeypatch.setattr(lifecycle, '_turn_client', lambda: SimpleNamespace(
        query=lambda operation, payload: row))
    monkeypatch.setattr(lifecycle, 'get_turn', lambda *a, **kw: {
        'projection': {'content': 'partial'}})
    monkeypatch.setattr(lifecycle, 'record_task_event',
                        lambda task, event: events.append((task, event)))
    monkeypatch.setattr(lifecycle, '_notify_abort_busy_projection', lambda *a: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.plant_abort_tombstone',
                        lambda tid, **kw: tombstones.append((tid, kw)))
    assert lifecycle.abort_attempt('attempt', user_id=7)['status'] == 'abort_signaled'
    assert tombstones == ([(task_id, {'source': 'turn_attempt_abort', 'user_id': 7})]
                          if task_id else [])
    assert revoked == [('conv', {'user_id': 7})]
    assert events[0][0]['_userId'] == 7
    assert events[0][0]['content'] == 'partial'


@pytest.mark.parametrize('session_owner', [1, 2])
def test_stop_signals_detached_swarm_without_await(monkeypatch, session_owner):
    from types import SimpleNamespace
    from lib.tasks_pkg.manager.cancellation import cancel_task

    task = _make_task('later-turn')
    task['convId'] = 'shared-conversation'
    signals, lookups = [], []
    session = SimpleNamespace(
        user_id=session_owner, task_id='earlier-turn',
        abort=lambda: signals.append('abort'))
    def lookup(key):
        lookups.append(key)
        return session
    monkeypatch.setattr('lib.swarm.integration.get_active_session', lookup)
    monkeypatch.setattr(
        'lib.conversations.change_notifications.notify_conv_changed',
        lambda *a, **kw: None)
    assert cancel_task(task['id'], user_id=1)['signalled']
    assert lookups == ['shared-conversation']
    assert signals == (['abort'] if session_owner == 1 else [])


def test_callback_failure_isolated_and_first_signal_metadata_preserved():
    from lib.tasks_pkg.manager.cancellation import cancel_task

    task = _make_task('callback-failure')
    signals = []
    ctx = context_for_task(task, round_num=1, tool_call_id='c',
                           tool_name='run_command', round_entry={})

    def failed_signal():
        raise RuntimeError('resource gone')

    ctx.register_cancel_callback(failed_signal)
    ctx.register_cancel_callback(lambda: signals.append('child'))
    first = cancel_task(task['id'], user_id=1, source='first')
    timestamp = task['_abort_timestamp']
    second = cancel_task(task['id'], user_id=1, source='duplicate')
    assert first['resourceCallbackErrors'] == 1
    assert signals == ['child']
    assert second['alreadyCancelled'] is True
    assert second['resourceCallbacks'] == 0
    assert task['_abort_timestamp'] == timestamp
    assert task['_abortSource'] == 'first'
