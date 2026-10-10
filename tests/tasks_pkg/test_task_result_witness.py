"""Regression tests for the backend-neutral task-result witness read.

The 2026-09-16 tofudb cutover wedged every chat turn: ``_upsert_task_row``
witnessed the CAS version through the generic ``record.get`` document path,
which the native authority deliberately refuses for the ``task_results``
namespace (its field-projection/ownership semantics live in the domain ops).
The witness now goes through ``task_results.replay_get`` — served by every
backend — and abort-tombstone preservation stays inside the guarded
checkpoint transaction on the authority. These tests pin that contract
against the real storage authority.
"""

from __future__ import annotations

import time
import uuid

import pytest


pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


def _task(task_id, *, status='done', user_id=7):
    return {
        'id': task_id,
        'convId': '',
        '_inline_messages': [{'role': 'user', 'content': 'question'}],
        '_userId': user_id,
        'status': status,
        'content': 'answer',
        'thinking': 'reasoning',
        'error': None,
        'model': 'model-x',
        'config': {'model': 'model-x'},
        'created_at': time.time() - 1,
        'toolRounds': [],
    }


def _replay(task_id, *, user_id=7, **flags):
    from lib.storage import get_storage_client
    return get_storage_client().query(
        'task_results.replay_get',
        {'key': task_id, 'user_id': user_id, **flags})


def _checkpoint(task_id, status, *, user_id=7, version=0):
    from lib.storage import get_storage_client
    now_ms = int(time.time() * 1000)
    get_storage_client(write=True).command(
        'task_results.checkpoint', {
            'key': task_id,
            'value': {
                'task_id': task_id,
                'conv_id': '',
                'user_id': user_id,
                'content': '',
                'thinking': '',
                'error': None,
                'status': status,
                'tool_rounds': None,
                'metadata': None,
                'segments': None,
                'created_at': now_ms - 1_000,
                'completed_at': now_ms if status not in (
                    'pending', 'running') else 0,
            },
            'expected_version': version,
        }, None)


def test_terminal_result_persists_through_replay_witness(chat_sidecar):
    from lib.tasks_pkg.manager._persist import persist_task_result

    task_id = f'witness-{uuid.uuid4().hex}'
    assert persist_task_result(_task(task_id)) is True

    row = _replay(
        task_id, include_terminal_payload=True, include_metadata=True)
    assert row is not None
    assert row['status'] == 'done'
    assert row['content'] == 'answer'
    assert row['thinking'] == 'reasoning'
    assert row['metadata']['model'] == 'model-x'
    assert row['version'] == 1


def test_interrupted_row_fences_late_terminal_write(chat_sidecar):
    from lib.tasks_pkg.manager._persist import persist_task_result

    task_id = f'witness-{uuid.uuid4().hex}'
    _checkpoint(task_id, 'interrupted')

    assert persist_task_result(_task(task_id)) is False
    assert _replay(task_id)['status'] == 'interrupted'


def test_terminal_row_fences_nonterminal_regression(chat_sidecar):
    from lib.tasks_pkg.manager._persist import persist_task_result

    task_id = f'witness-{uuid.uuid4().hex}'
    _checkpoint(task_id, 'done')

    assert persist_task_result(_task(task_id, status='running')) is False
    assert _replay(task_id)['status'] == 'done'


def test_abort_tombstone_survives_terminal_checkpoint(chat_sidecar):
    from lib.tasks_pkg.manager._persist import persist_task_result
    from lib.storage import get_storage_client

    task_id = f'witness-{uuid.uuid4().hex}'
    _checkpoint(task_id, 'running')
    client = get_storage_client(write=True)
    signaled = client.command(
        'task_results.abort',
        {'task_id': task_id, 'user_id': 7, 'source': 'test'}, None)
    assert signaled.get('signaled') is True

    assert persist_task_result(_task(task_id)) is True

    tombstone = client.query(
        'task_results.abort_requested', {'task_id': task_id, 'user_id': 7})
    assert tombstone, 'abort tombstone read must return a record'
    assert tombstone.get('requested') is True


def test_terminal_listener_observes_committed_row_once(chat_sidecar, monkeypatch):
    from lib.tasks_pkg.manager import _terminal, _persist
    monkeypatch.setattr(_terminal, '_TERMINAL_LISTENERS', [])
    seen = []
    def observe(task):
        seen.append(_replay(task['id'])['status'])
    assert _terminal.register_terminal_listener(observe)
    assert not _terminal.register_terminal_listener(observe)
    task = _task(f'listener-{uuid.uuid4().hex}')
    assert _persist.persist_task_result(task)
    _terminal.notify_terminal_listeners(task)
    assert seen == ['done']


@pytest.mark.parametrize('outcome', ['failed', 'fenced'])
def test_terminal_listener_never_observes_uncommitted_or_fenced_task(
        chat_sidecar, monkeypatch, outcome):
    from lib.tasks_pkg.manager import _terminal, _persist
    monkeypatch.setattr(_terminal, '_TERMINAL_LISTENERS', [])
    seen = []
    _terminal.register_terminal_listener(lambda task: seen.append(task['id']))
    def write(*args, **kwargs):
        if outcome == 'failed':
            raise OSError('injected persistence failure')
        return False
    monkeypatch.setattr(_persist, '_upsert_task_row', write)
    assert _persist.persist_task_result(_task(f'failed-{uuid.uuid4().hex}')) is False
    assert seen == []


def test_terminal_listener_errors_are_isolated_without_count_quota(monkeypatch):
    from lib.tasks_pkg.manager import _terminal
    monkeypatch.setattr(_terminal, '_TERMINAL_LISTENERS', [])
    seen = []
    def failing(task):
        raise RuntimeError('observer failure')
    _terminal.register_terminal_listener(failing)
    for i in range(65):
        _terminal.register_terminal_listener(lambda task, i=i: seen.append(i))
    assert _terminal.register_terminal_listener(lambda task: None)
    task = _task('listeners')
    _terminal.notify_terminal_listeners(dict(task, status='running'))
    _terminal.notify_terminal_listeners(dict(task, _transientRuntime=True))
    assert seen == []
    _terminal.notify_terminal_listeners(task)
    assert seen == list(range(65))
