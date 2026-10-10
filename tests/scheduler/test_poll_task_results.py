"""Owned task result consistency across status, immediate wait and timed wait."""
from __future__ import annotations

import pytest

from lib.scheduler.executor import _await as adapter
from lib.tasks_pkg.manager.runtime import chat_task_runtime

pytestmark = pytest.mark.unit


@pytest.fixture
def target(monkeypatch):
    task = {'id': 'owned-task', 'convId': 'other-conversation', 'status': 'done',
            'content': 'FIRST-EVIDENCE\n' + 'x' * 1200 + '\nLAST-EVIDENCE',
            '_userId': '7', 'created_at': 0}
    monkeypatch.setattr(chat_task_runtime, 'get_owned',
                        lambda task_id, *, user_id: task
                        if task_id == task['id'] and user_id == 7 else None)
    return task


@pytest.mark.parametrize('action', ['status', 'wait'])
@pytest.mark.parametrize('status', ['done', 'error', 'aborted', 'interrupted'])
def test_every_terminal_poll_returns_full_output_with_honest_outcome(target, action, status):
    target['status'] = status
    result = adapter._execute_await_task(
        {'action': action, 'task_id': target['id'], 'timeout': 60}, user_id=7)
    assert target['content'] in result
    assert f'Status: {status}' in result
    assert ('Final output' if status == 'done' else 'Partial output') in result
    if status != 'done':
        assert 'Final output' not in result


def test_conflicting_error_does_not_label_done_text_successful(target):
    target['error'] = 'backend failed'
    result = adapter._execute_await_task({'action': 'status', 'task_id': target['id']}, user_id=7)
    assert 'Partial output' in result and 'Final output' not in result
    assert 'backend failed' in result


def test_completion_during_wait_returns_early_with_full_evidence(target, monkeypatch):
    target['status'] = 'pending'
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr(adapter._time, 'monotonic', lambda: clock[0])

    def finish(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        target['status'] = 'done'

    monkeypatch.setattr(adapter._time, 'sleep', finish)
    result = adapter._execute_await_task(
        {'action': 'wait', 'task_id': target['id'], 'timeout': 999}, user_id=7)
    assert sleeps == [5]
    assert target['content'] in result


@pytest.mark.parametrize('status', ['pending', 'unknown', 'running', None])
def test_nonterminal_status_is_never_reported_as_finished(target, status):
    target['status'] = status
    result = adapter._execute_await_task(
        {'action': 'wait', 'task_id': target['id'], 'timeout': 0}, user_id=7)
    assert 'no terminal outcome is verified' in result
    assert 'finished' not in result and 'Final output' not in result


def test_completion_at_deadline_uses_same_full_result(target, monkeypatch):
    reads = [dict(target, status='running'), target]
    monkeypatch.setattr(chat_task_runtime, 'get_owned', lambda *a, **kw: reads.pop(0))
    result = adapter._execute_await_task(
        {'action': 'wait', 'task_id': target['id'], 'timeout': 0}, user_id=7)
    assert target['content'] in result
    assert 'Final output' in result


@pytest.mark.parametrize('action', ['status', 'wait'])
def test_foreign_or_missing_task_never_reveals_evidence(target, action):
    result = adapter._execute_await_task(
        {'action': action, 'task_id': target['id'], 'timeout': 0}, user_id=8)
    assert 'unavailable' in result
    assert 'FIRST-EVIDENCE' not in result


def test_disappearance_during_wait_is_not_success(target, monkeypatch):
    reads = [dict(target, status='running'), None]
    monkeypatch.setattr(chat_task_runtime, 'get_owned', lambda *a, **kw: reads.pop(0))
    result = adapter._execute_await_task(
        {'action': 'wait', 'task_id': target['id'], 'timeout': 0}, user_id=7)
    assert 'no terminal outcome is verified' in result


def test_aborted_parent_stops_wait_before_sleep(target, monkeypatch):
    target['status'] = 'running'
    monkeypatch.setattr(adapter._time, 'sleep', lambda *a: pytest.fail('must not sleep'))
    result = adapter._execute_await_task(
        {'action': 'wait', 'task_id': target['id'], 'timeout': 60,
         '_parent_task': {'aborted': True}}, user_id=7)
    assert 'Wait cancelled' in result
