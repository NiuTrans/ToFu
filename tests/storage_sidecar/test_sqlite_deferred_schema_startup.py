"""SQLite optional-index startup safety contracts."""

from __future__ import annotations

import sqlite3
import time

import pytest

from lib.storage_sidecar.adapters.sqlite import (
    SQLiteBackend,
    _deferred_index_name,
)
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.schema import (
    LEGACY_TASK_EVENT_RETENTION_INDEX_NAME,
    LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT,
    TASK_EVENT_RETENTION_SPECS,
    deferred_index_statements,
)
from lib.storage_sidecar.operations_pkg._records import (
    _event_prune,
    _legacy_index_event_prune,
    _recover_legacy_blank_event_page,
)


pytestmark = pytest.mark.unit


def _config(tmp_path) -> SidecarConfig:
    data_dir = tmp_path / 'data'
    logs_dir = tmp_path / 'logs'
    data_dir.mkdir()
    logs_dir.mkdir()
    return SidecarConfig(
        project_root=tmp_path,
        data_dir=data_dir,
        logs_dir=logs_dir,
        backend='sqlite',
        deployment_mode='personal',
        process_role='all',
        replica_id=None,
        token='test-token-' * 4,
        sqlite_path=data_dir / 'tofu.db',
        postgres_dsn='',
        redis_url='',
        allow_schema_migration=True,
        read_pool_size=1,
        write_pool_size=1,
    )


def _deferred_names() -> set[str]:
    return {
        _deferred_index_name(statement)
        for statement in deferred_index_statements('sqlite')
    }


def _stored_index_names(path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index'"
            ).fetchall()
        }
    finally:
        connection.close()


def test_fresh_authority_installs_optional_indexes_before_writer_starts(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    backend = SQLiteBackend(config)
    try:
        backend.start()
        cache_pages = backend._writer._connection.execute(
            'PRAGMA cache_size').fetchone()[0]
        assert cache_pages == -(config.sqlite_writer_cache_mib * 1024)
        metrics = backend.metrics()
        assert metrics['sqlite_version'] == sqlite3.sqlite_version
        assert metrics['writer_cache_mib'] == 32
        assert metrics['writer_watchdog'] == {
            'stall_grace_s': 15.0,
            'hard_kill_s': 60.0,
        }
        assert metrics['deferred_schema'] == {
            'automatic_build': 'fresh-authority-only',
            'missing_indexes': [],
        }
    finally:
        backend.close()

    assert _deferred_names() <= _stored_index_names(config.sqlite_path)


def test_established_authority_reports_but_never_builds_missing_indexes(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    connection = sqlite3.connect(config.sqlite_path)
    connection.execute('CREATE TABLE established_marker(value TEXT NOT NULL)')
    connection.execute("INSERT INTO established_marker VALUES ('present')")
    connection.commit()
    connection.close()

    expected = _deferred_names()
    backend = SQLiteBackend(config)
    try:
        backend.start()
        assert set(backend.metrics()['deferred_schema']['missing_indexes']) == expected
        prune = backend.command(
            'event.prune', 'test-digest', None, 'maintenance',
            lambda session: _event_prune(session, {
                'created_before_ms': int(time.time() * 1000),
                'limit': 25,
                'retention_class': 'streaming',
            }),
            time.monotonic() + 5,
            receipt_required=False,
        )
        assert prune == {
            'deleted': 0,
            'deferred': True,
            'reason': 'missing_index',
            'required_index': TASK_EVENT_RETENTION_SPECS['streaming'][0],
        }
    finally:
        backend.close()

    assert expected.isdisjoint(_stored_index_names(config.sqlite_path))


def test_established_authority_defers_legacy_retention_to_offline_maintenance(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    connection = sqlite3.connect(config.sqlite_path)
    connection.execute('CREATE TABLE established_marker(value TEXT NOT NULL)')
    connection.execute("INSERT INTO established_marker VALUES ('present')")
    connection.commit()
    connection.close()

    backend = SQLiteBackend(config)
    try:
        backend.start()
    finally:
        backend.close()

    cutoff = int(time.time() * 1000)
    connection = sqlite3.connect(config.sqlite_path)
    try:
        connection.execute(
            f'CREATE INDEX {LEGACY_TASK_EVENT_RETENTION_INDEX_NAME} '
            'ON storage_events(stream_kind, event_type, created_at_ms)')
        connection.executemany(
            'INSERT INTO storage_events('
            'task_id, sequence, stream_kind, event_type, event_kind, '
            'event_json, created_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?)',
            [
                ('legacy-stream', 1, 'task', '', '',
                 '{"type":"delta"}', cutoff - 6),
                ('stream-delta', 1, 'task', 'delta', '', '{}', cutoff - 5),
                ('stream-done', 1, 'task', 'done', '', '{}', cutoff - 4),
                ('stream-future', 1, 'task', 'z_future', '', '{}', cutoff - 3),
                ('struct-blank', 1, 'task', '', '',
                 '{"type":"round_start","kind":"state"}', cutoff - 2),
                ('struct-round', 1, 'task', 'round_start', '', '{}', cutoff - 1),
                ('project-feed', 1, 'project_feed', 'delta', '', '{}', cutoff - 5),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    backend = SQLiteBackend(config)
    try:
        backend.start()

        def prune(retention_class):
            return backend.command(
                'event.prune', 'test-digest', None, 'maintenance',
                lambda session: _event_prune(session, {
                    'created_before_ms': cutoff,
                    'limit': 1,
                    'retention_class': retention_class,
                }),
                time.monotonic() + 5,
                receipt_required=False,
            )

        assert prune('streaming') == {
            'deleted': 0,
            'deferred': True,
            'reason': 'legacy_index_offline_required',
            'required_index': TASK_EVENT_RETENTION_SPECS['streaming'][0],
        }
        assert prune('structural') == {
            'deleted': 0,
            'deferred': True,
            'reason': 'legacy_index_offline_required',
            'required_index': TASK_EVENT_RETENTION_SPECS['structural'][0],
        }
        remaining = backend._writer._connection.execute(
            'SELECT COUNT(*) FROM storage_events'
        ).fetchone()[0]
        assert remaining == 7
    finally:
        backend.close()


def test_legacy_retention_type_cardinality_fails_closed_at_hard_bound():
    class Session:
        def __init__(self):
            self.types = [
                f'event_{index:03d}'
                for index in range(LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT + 1)
            ]
            self.fetch_calls = 0
            self.delete_calls = 0

        def index_exists(self, index_name):
            return index_name == LEGACY_TASK_EVENT_RETENTION_INDEX_NAME

        def fetch_one(self, _sql, params=()):
            self.fetch_calls += 1
            cursor = params[1]
            next_type = next(
                (event_type for event_type in self.types
                 if event_type > cursor),
                None,
            )
            return ({'event_type': next_type}
                    if next_type is not None else None)

        def execute(self, _sql, _params=()):
            self.delete_calls += 1
            return 0

    session = Session()
    result = _legacy_index_event_prune(
        session,
        cutoff=int(time.time() * 1000),
        limit=25,
        legacy_recovery_limit=100,
        retention_class='streaming',
        required_index=TASK_EVENT_RETENTION_SPECS['streaming'][0],
    )

    assert result == {
        'deleted': 0,
        'deferred': True,
        'reason': 'legacy_index_event_type_limit',
        'required_index': TASK_EVENT_RETENTION_SPECS['streaming'][0],
    }
    assert session.fetch_calls == LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT + 1
    assert session.delete_calls == LEGACY_TASK_EVENT_RETENTION_TYPE_LIMIT


def test_legacy_retention_stops_discovery_after_first_deletable_type():
    class Session:
        def __init__(self):
            self.types = ['event_a', 'event_b', 'event_c']
            self.fetch_cursors = []
            self.delete_types = []

        def index_exists(self, index_name):
            return index_name == LEGACY_TASK_EVENT_RETENTION_INDEX_NAME

        def fetch_one(self, _sql, params=()):
            cursor = params[1]
            self.fetch_cursors.append(cursor)
            next_type = next(
                (event_type for event_type in self.types
                 if event_type > cursor),
                None,
            )
            return ({'event_type': next_type}
                    if next_type is not None else None)

        def execute(self, _sql, params=()):
            event_type = params[1]
            self.delete_types.append(event_type)
            return int(event_type == 'event_b')

    session = Session()
    result = _legacy_index_event_prune(
        session,
        cutoff=int(time.time() * 1000),
        limit=25,
        legacy_recovery_limit=100,
        retention_class='streaming',
        required_index=TASK_EVENT_RETENTION_SPECS['streaming'][0],
    )

    assert result == {
        'deleted': 1,
        'has_more': True,
        'index_mode': 'legacy_exact_type',
    }
    assert session.fetch_cursors == ['', 'event_a']
    assert session.delete_types == ['event_a', 'event_b']


def test_legacy_blank_recovery_bounds_materialized_payload_page():
    payload_budget = 4 * 1024 * 1024

    class Session:
        backend = 'sqlite'

        def __init__(self):
            self.payload_keys = None

        def fetch_all(self, sql, params=()):
            if 'AS payload_bytes' in sql:
                return [
                    {'task_id': 'first', 'sequence': 1,
                     'payload_bytes': payload_budget},
                    {'task_id': 'second', 'sequence': 1,
                     'payload_bytes': 1},
                ]
            self.payload_keys = params
            return [{
                'task_id': 'first', 'sequence': 1,
                'event_json': b'{"type":"delta"}',
            }]

        def execute(self, sql, _params=()):
            assert sql.startswith('DELETE FROM storage_events')
            return 1

    session = Session()
    result = _recover_legacy_blank_event_page(
        session, cutoff=1234, limit=25)

    assert result['classified'] == 1
    assert result['deleted'] == 1
    assert result['payload_bytes'] == payload_budget
    assert session.payload_keys == ('first', 1)


def test_legacy_blank_recovery_marks_opaque_payload_without_deleting():
    class Session:
        backend = 'sqlite'

        def __init__(self):
            self.update = None

        def fetch_all(self, sql, _params=()):
            if 'AS payload_bytes' in sql:
                return [{
                    'task_id': 'opaque', 'sequence': 1,
                    'payload_bytes': 2,
                }]
            return [{
                'task_id': 'opaque', 'sequence': 1, 'event_json': b'{}',
            }]

        def execute(self, sql, params=()):
            assert sql.startswith('UPDATE storage_events SET event_kind')
            self.update = params
            return 1

    session = Session()
    result = _recover_legacy_blank_event_page(
        session, cutoff=1234, limit=25)

    assert result['classified'] == 1
    assert result['deleted'] == 0
    assert result['opaque'] == 1
    assert session.update == ('__tofu_legacy_opaque__', 'opaque', 1)


def test_legacy_blank_recovery_never_materializes_oversized_stored_row():
    payload_budget = 4 * 1024 * 1024

    class Session:
        backend = 'sqlite'

        def __init__(self):
            self.fetch_calls = 0
            self.update = None

        def fetch_all(self, sql, _params=()):
            self.fetch_calls += 1
            assert 'AS payload_bytes' in sql
            return [{
                'task_id': 'oversized',
                'sequence': 1,
                'payload_bytes': payload_budget + 1,
            }]

        def execute(self, sql, params=()):
            assert sql.startswith('UPDATE storage_events SET event_kind')
            self.update = params
            return 1

    session = Session()
    result = _recover_legacy_blank_event_page(
        session, cutoff=1234, limit=25)

    assert result == {
        'deleted': 0,
        'classified': 1,
        'recovered_types': 0,
        'opaque': 1,
        'payload_bytes': 0,
        'oversize_opaque': 1,
        'oversize_stored_bytes': payload_budget + 1,
        'has_more': True,
        'index_mode': 'legacy_blank_type_recovery',
    }
    assert session.fetch_calls == 1
    assert session.update == ('__tofu_legacy_opaque__', 'oversized', 1)


def test_legacy_blank_recovery_never_decodes_oversized_expansion(monkeypatch):
    import lib.storage_sidecar.operations_pkg._records as records
    from lib.storage_sidecar.task_event_codec import encode_task_event_payload

    payload_budget = 4 * 1024 * 1024
    raw = b'{"type":"delta","content":"' + b'x' * payload_budget + b'"}'
    encoded = encode_task_event_payload(raw)
    assert len(encoded) < payload_budget

    class Session:
        backend = 'sqlite'

        def __init__(self):
            self.update = None

        def fetch_all(self, sql, _params=()):
            if 'AS payload_bytes' in sql:
                return [{
                    'task_id': 'compressed',
                    'sequence': 1,
                    'payload_bytes': len(encoded),
                }]
            return [{
                'task_id': 'compressed',
                'sequence': 1,
                'event_json': encoded,
            }]

        def execute(self, sql, params=()):
            assert sql.startswith('UPDATE storage_events SET event_kind')
            self.update = params
            return 1

    monkeypatch.setattr(
        records,
        'decode_task_event_payload',
        lambda _value: pytest.fail('oversized expansion must not be decoded'),
    )
    session = Session()
    result = _recover_legacy_blank_event_page(
        session, cutoff=1234, limit=25)

    assert result['classified'] == 1
    assert result['deleted'] == 0
    assert result['opaque'] == 1
    assert result['payload_bytes'] == len(encoded)
    assert result['oversize_opaque'] == 1
    assert result['oversize_stored_bytes'] == len(encoded)
    assert session.update == ('__tofu_legacy_opaque__', 'compressed', 1)


def test_legacy_blank_recovery_limit_is_independent_of_delete_page(
        monkeypatch):
    import lib.storage_sidecar.operations_pkg._records as records

    class Session:
        def index_exists(self, index_name):
            return index_name == LEGACY_TASK_EVENT_RETENTION_INDEX_NAME

        def fetch_one(self, _sql, _params=()):
            return None

    recovered = {
        'deleted': 0,
        'classified': 100,
        'has_more': True,
        'index_mode': 'legacy_blank_type_recovery',
    }
    observed = {}

    def recover(_session, *, cutoff, limit):
        observed.update(cutoff=cutoff, limit=limit)
        return recovered

    monkeypatch.setattr(records, '_recover_legacy_blank_event_page', recover)
    result = _legacy_index_event_prune(
        Session(),
        cutoff=1234,
        limit=25,
        legacy_recovery_limit=100,
        retention_class='streaming',
        required_index=TASK_EVENT_RETENTION_SPECS['streaming'][0],
    )

    assert result is recovered
    assert observed == {'cutoff': 1234, 'limit': 100}

    observed.clear()
    result = _legacy_index_event_prune(
        Session(),
        cutoff=5678,
        limit=1000,
        legacy_recovery_limit=100,
        retention_class='streaming',
        required_index=TASK_EVENT_RETENTION_SPECS['streaming'][0],
    )

    assert result is recovered
    assert observed == {'cutoff': 5678, 'limit': 100}


def test_event_retention_indexes_are_tier_partial_without_temp_sort(
        tmp_path, monkeypatch):
    """The empty-backlog probe must not scan/sort the payload table."""
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    backend = SQLiteBackend(config)
    try:
        backend.start()
    finally:
        backend.close()

    connection = sqlite3.connect(config.sqlite_path)
    try:
        plans = {}
        columns = {}
        for retention_class, (index_name, predicate) in (
                TASK_EVENT_RETENTION_SPECS.items()):
            columns[retention_class] = [
                row[2] for row in connection.execute(
                    f"PRAGMA index_xinfo('{index_name}')")
                if row[2] is not None
            ]
            plans[retention_class] = [str(row[3]) for row in connection.execute(
                'EXPLAIN QUERY PLAN '
                'SELECT task_id, sequence FROM storage_events '
                f'WHERE {predicate} '
                'AND created_at_ms < ? ORDER BY created_at_ms LIMIT ?',
                (int(time.time() * 1000), 25),
            )]
    finally:
        connection.close()

    for retention_class, (index_name, _predicate) in (
            TASK_EVENT_RETENTION_SPECS.items()):
        assert columns[retention_class] == ['created_at_ms']
        assert any(index_name in detail for detail in plans[retention_class])
        assert not any(
            'TEMP B-TREE' in detail for detail in plans[retention_class])


def test_schema_failure_rolls_back_before_version_publication(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)

    def fail_after_partial_write(session):
        session.execute('CREATE TABLE partial_migration(value TEXT NOT NULL)')
        session.execute("INSERT INTO partial_migration VALUES ('not-published')")
        raise RuntimeError('injected migration failure')

    monkeypatch.setattr(
        'lib.storage_sidecar.adapters.sqlite.initialize_schema',
        fail_after_partial_write,
    )
    backend = SQLiteBackend(config)
    with pytest.raises(RuntimeError, match='injected migration failure'):
        backend.start()

    connection = sqlite3.connect(config.sqlite_path)
    try:
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='partial_migration'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' "
            "AND name='storage_meta'"
        ).fetchone() is None
    finally:
        connection.close()
