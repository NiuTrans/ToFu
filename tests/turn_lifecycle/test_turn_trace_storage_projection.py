"""Selective replay body projection on an isolated database and backend SQL seam."""
import sqlite3
import pytest
from lib.storage_sidecar.operations_pkg._records import _event_list, _event_latest
from lib.storage_sidecar.operations_pkg._records import _append_event_row
from lib.storage import StorageError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])
def test_execution_timestamp_survives_delayed_storage_and_selective_replay(backend, monkeypatch):
    from lib.storage_sidecar.operations_pkg import _records
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE storage_events(task_id, sequence, stream_kind, '
                       'event_type, event_kind, event_json, created_at_ms, UNIQUE(task_id, sequence))')
    class Session:
        def execute(self, sql, params):
            return connection.execute(sql, params).rowcount
        def fetch_all(self, sql, params):
            return connection.execute(sql, params).fetchall()
        def fetch_one(self, sql, params):
            return connection.execute(sql, params).fetchone()
    session = Session()
    session.backend = backend
    monkeypatch.setattr(_records.time, 'time', lambda: 9000.0)
    event = {'type': 'delta', 'content': 'bounded synthetic token',
             'executionAtMs': 1_000_123, 'executionOriginAtMs': 1_000_000,
             'executionClockId': 'producer-clock'}
    try:
        assert _append_event_row(session, {'task_id': 'ours', 'sequence': 0,
                                          'event': event})['inserted']
        replay = _event_list(session, {'task_id': 'ours', 'payload_types': []})
        assert replay == [{'sequence': 0, 'event': {'type': 'delta'}, 'created_at_ms': 1_000_123}]
        assert _event_latest(session, {'task_id': 'ours', 'metadata_only': True}) == replay[0]
        assert not _append_event_row(session, {'task_id': 'ours', 'sequence': 0,
                                              'event': event})['inserted']
        for timestamp in (True, None, -1):
            with pytest.raises(StorageError):
                _append_event_row(session, {'task_id': 'ours', 'sequence': 1,
                                           'event': {**event, 'executionAtMs': timestamp}})
        assert len(_event_list(session, {'task_id': 'ours'})) == 1
    finally:
        connection.close()


@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])
def test_projection_omits_unselected_even_invalid_payloads(backend):
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE storage_events(task_id, sequence, event_type, event_json, created_at_ms)')
    connection.executemany('INSERT INTO storage_events VALUES(?,?,?,?,?)', [
        ('ours', 0, 'delta', 'intentionally not JSON', 10),
        ('ours', 1, 'round_start', '{"type":"round_start","roundNum":1}', 11),
        ('other', 0, 'round_start', '{"private":true}', 12),
    ])
    class Session:
        def fetch_all(self, sql, params):
            return connection.execute(sql, params).fetchall()
        def fetch_one(self, sql, params):
            return connection.execute(sql, params).fetchone()
    session = Session()
    session.backend = backend
    try:
        result = _event_list(session, {'task_id': 'ours', 'payload_types': ['round_start']})
        assert result == [
            {'sequence': 0, 'event': {'type': 'delta'}, 'created_at_ms': 10},
            {'sequence': 1, 'event': {'type': 'round_start', 'roundNum': 1}, 'created_at_ms': 11},
        ]
        assert _event_list(session, {'task_id': 'ours', 'payload_types': [], 'limit': 1}) == result[:1]
        assert _event_latest(session, {'task_id': 'ours', 'metadata_only': True}) == {
            'sequence': 1, 'event': {'type': 'round_start'}, 'created_at_ms': 11}
        with pytest.raises(StorageError):
            _event_list(session, {'task_id': 'ours', 'payload_types': ['x'] * 65})
    finally:
        connection.close()
