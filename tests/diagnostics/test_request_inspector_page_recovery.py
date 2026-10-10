"""Inspector reads shrink only native oversized pages and never invent history."""

from __future__ import annotations

import pytest

from lib.storage.errors import StorageError
from lib.tasks_pkg import request_inspector as ri

pytestmark = pytest.mark.unit


def _row(sequence: int) -> dict:
    return {'sequence': sequence, 'event': {'type': 'round_usage'},
            'created_at_ms': sequence}


def test_event_page_overflow_shrinks_at_same_cursor_and_continues(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []

        def query(self, operation, payload):
            assert operation == 'event.list'
            after, limit = payload['after_sequence'], payload['limit']
            self.calls.append((after, limit))
            if limit > 250:
                raise StorageError('database_unavailable', 'event page exceeds 8 MiB')
            rows = [_row(i) for i in range(1, 503) if i > after]
            return rows[:limit]

    client = Client()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda: client)
    rows, ok = ri._read_events_uncached('task-overflow', rebuild=False)
    assert ok is True
    assert [r['event_id'] for r in rows] == list(range(1, 503))
    assert client.calls[:3] == [(-1, 1000), (-1, 500), (-1, 250)]
    assert client.calls[-2:] == [(250, 250), (500, 250)]
    assert all(call[1] == 250 for call in client.calls[3:])


def test_single_oversized_event_is_failed_read_not_empty_history(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []

        def query(self, operation, payload):
            self.calls.append((payload['after_sequence'], payload['limit']))
            raise StorageError('database_unavailable', 'event page exceeds 8 MiB')

    client = Client()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda: client)
    rows, ok = ri._read_events_uncached('task-single-huge', rebuild=False)
    assert (rows, ok) == ([], False)
    assert client.calls[0] == (-1, 1000)
    assert client.calls[-1] == (-1, 1)
    assert len(client.calls) == 10  # 1000 → 500 → 250 → … → 1
    assert {cursor for cursor, _ in client.calls} == {-1}


def test_protocol_failure_does_not_retry_with_smaller_page(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = 0

        def query(self, operation, payload):
            self.calls += 1
            raise StorageError('database_protocol_error', 'invalid frame')

    client = Client()
    monkeypatch.setattr('lib.storage.get_storage_client', lambda: client)
    assert ri._read_events_uncached('task-corrupt', rebuild=False) == ([], False)
    assert client.calls == 1


def test_summary_read_failure_marks_unprobed_turn_retryable(monkeypatch):
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    task_id = 'task-summary-failed'
    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned', lambda **kwargs: [])

    class Client:
        def query(self, operation, payload, *, deadline=None):
            if operation == 'turn.timing_trace.list':
                return {'records': [], 'has_more': False}
            if operation == 'task_results.summary_list':
                return {'records': [{
                    'key': task_id, 'conv_id': 'conv-summary-failed',
                    'status': 'done', 'created_at': 100, 'completed_at': 101,
                }], 'capped': False}
            if operation == 'event.inspector_summary':
                raise StorageError('database_unavailable', 'storage busy')
            raise AssertionError(operation)

    monkeypatch.setattr('lib.storage.get_storage_client', lambda: Client())
    out = ri.list_conv_tasks('conv-summary-failed', user_id=1)
    assert out['readError'] is True
    assert len(out['tasks']) == 1
    assert out['tasks'][0]['readError'] is True
    wire = ri.get_conv_inspector_wire('conv-summary-failed', user_id=1)
    assert wire['readError'] is True
    assert wire['sections'][0]['readError'] is True
    assert 'expired' not in wire['sections'][0]
    assert wire['turns'][0]['readError'] is True
    assert 'expired' not in wire['turns'][0]
