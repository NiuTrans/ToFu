"""Recovery ownership fence for stale task checkpoint/final writes."""

from __future__ import annotations

import time
import uuid

import pytest


pytestmark = pytest.mark.unit


def _write(task, status, content):
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    return _upsert_task_row(
        task, '', content=content, thinking='', status=status,
        error_json=None, tr_json=None, meta_json=None)


def test_interrupted_recovery_row_rejects_late_checkpoint_and_finalizer():
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db

    task_id = 'fence-' + uuid.uuid4().hex
    task = {
        'id': task_id, 'convId': '', '_inline_messages': True,
        'created_at': time.time(),
    }
    db = get_thread_db(DOMAIN_CHAT)
    try:
        assert _write(task, 'running', 'partial') is True
        db_execute_with_retry(
            db,
            "UPDATE task_results SET status='interrupted' WHERE task_id=?",
            (task_id,))

        assert _write(task, 'running', 'stale checkpoint') is False
        assert _write(task, 'done', 'stale final') is False
        row = db.execute(
            'SELECT status, content FROM task_results WHERE task_id=?',
            (task_id,)).fetchone()
        assert row['status'] == 'interrupted'
        assert row['content'] == 'partial'
    finally:
        db_execute_with_retry(
            db, 'DELETE FROM task_results WHERE task_id=?', (task_id,))


def test_running_checkpoint_cannot_erase_abort_tombstone_columns():
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    from lib.tasks_pkg.manager import _registry, _state

    task_id = 'abort-fence-' + uuid.uuid4().hex
    task = {
        'id': task_id, 'convId': '', '_inline_messages': True,
        'created_at': time.time(),
    }
    db = get_thread_db(DOMAIN_CHAT)
    try:
        assert _write(task, 'running', 'partial') is True
        assert _registry.plant_abort_tombstone(
            task_id, source='concurrency-test') is True
        assert _write(task, 'running', 'newer checkpoint') is True
        row = db.execute(
            'SELECT abort_requested_at, abort_source, content '
            'FROM task_results WHERE task_id=?', (task_id,)).fetchone()
        assert int(row['abort_requested_at']) > 0
        assert row['abort_source'] == 'concurrency-test'
        assert row['content'] == 'newer checkpoint'
    finally:
        with _state._abort_tombstones_lock:
            _state._abort_tombstones.discard(task_id)
        db_execute_with_retry(
            db, 'DELETE FROM task_results WHERE task_id=?', (task_id,))


def test_fenced_checkpoint_skips_conversation_side_effect(monkeypatch):
    import lib.tasks_pkg.manager._sync as sync

    calls = []
    monkeypatch.setattr(sync, '_upsert_task_row', lambda *a, **k: False)
    monkeypatch.setattr(
        sync, '_sync_partial_to_conversation',
        lambda _task: calls.append('conversation'))
    task = {
        'id': 'fenced-checkpoint', 'convId': 'conv-fenced',
        'content': 'late partial', 'thinking': '', 'toolRounds': [],
        'status': 'running', 'created_at': time.time(),
    }
    assert sync.checkpoint_task_partial(task) is False
    assert calls == []


def test_fenced_finalizer_skips_all_downstream_writes(monkeypatch):
    import lib.tasks_pkg.manager._persist as persist
    import lib.tasks_pkg.manager._sync as sync

    calls = []
    monkeypatch.setattr(persist, '_upsert_task_row', lambda *a, **k: False)
    monkeypatch.setattr(
        sync, '_sync_result_to_conversation',
        lambda *a, **k: calls.append('conversation'))
    monkeypatch.setattr(
        sync, '_update_proactive_execution_status',
        lambda *a, **k: calls.append('proactive'))
    monkeypatch.setattr(
        sync, '_dispatch_queued_message',
        lambda *a, **k: calls.append('queue'))
    monkeypatch.setattr(
        sync, '_maybe_refresh_project_summary',
        lambda *a, **k: calls.append('summary'))
    task = {
        'id': 'fenced-finalizer', 'convId': 'conv-fenced',
        'content': 'late final', 'thinking': '', 'toolRounds': [],
        'messages': [{'role': 'user', 'content': 'x'}],
        'status': 'done', 'finishReason': 'stop',
        'created_at': time.time(),
    }
    assert persist.persist_task_result(task) is False
    assert calls == []
    assert task['messages'] is None
