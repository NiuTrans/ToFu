"""SQLite read-pool slot hygiene after statement/rollback failures."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters import sqlite as sqlite_adapter
from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
from lib.storage_authority_sql.config import SidecarConfig


pytestmark = pytest.mark.unit


def _config(tmp_path: Path) -> SidecarConfig:
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


class _PoisonedSlotConnection:
    """Read-slot connection whose failure path also fails ROLLBACK."""

    def __init__(self) -> None:
        self.rollbacks = 0
        self.closed = False
        self.progress_cleared = False

    def set_progress_handler(self, callback, _instructions) -> None:
        if callback is None:
            self.progress_cleared = True

    def execute(self, _sql, _params=()):
        raise sqlite3.OperationalError('forced read failure')

    def rollback(self):
        self.rollbacks += 1
        raise sqlite3.OperationalError('forced rollback failure')

    def close(self):
        self.closed = True


def test_read_rollback_failure_retires_slot_and_next_read_connects_on_demand(
    tmp_path, monkeypatch, caplog,
):
    """A failed read-slot rollback must not return the slot to the pool.

    The PostgreSQL adapter already treats this as broken=True; the SQLite
    adapter discards the poisoned connection, logs a WARNING, and
    creates a new connection on the next eligible read.
    """
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    # Keep reconstructible search work out of this controlled connection test;
    # the backend records its absence as degraded
    # search and continues (its existing failure containment).
    import lib.storage_authority_sql.turn_search_projection as search_projection

    class _DisabledProjection:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError('projection disabled for read-pool test')

    monkeypatch.setattr(
        search_projection, 'TurnSearchProjectionRuntime', _DisabledProjection)
    backend = SQLiteBackend(_config(tmp_path))
    backend.start()
    try:
        fake = _PoisonedSlotConnection()
        now = time.monotonic()
        backend._read_pool.put(sqlite_adapter._ReadSlot(fake, now, now))

        with caplog.at_level('WARNING', logger='tofu.storage.sidecar.sqlite'):
            with pytest.raises(StorageError) as raised:
                backend.query(
                    'system.baseline',
                    lambda _session: pytest.fail('poisoned slot ran a query'),
                    time.monotonic() + 5,
                )
        assert raised.value.code == 'database_internal'
        assert fake.rollbacks == 1
        assert fake.closed is True
        assert 'poisoned' in caplog.text

        # The poisoned connection never returns; the next read creates a fresh one.
        metrics = backend.metrics()
        assert metrics['read_pool_size'] == 0
        assert metrics['read_active_leases'] == 0
        assert metrics['queries']['pool_rotations'] == 1
        result = backend.query(
            'system.integrity_check',
            lambda session: session.fetch_one('PRAGMA integrity_check'),
            time.monotonic() + 5,
        )
        assert result is not None
    finally:
        backend.close()


def _demand_backend(tmp_path, monkeypatch):
    import lib.storage_authority_sql.turn_search_projection as projection
    def disabled(*args, **kwargs):
        raise RuntimeError('projection disabled for reader ownership test')
    monkeypatch.setattr(projection, 'TurnSearchProjectionRuntime', disabled)
    backend = SQLiteBackend(_config(tmp_path))
    backend.start()
    return backend


def test_real_readers_start_above_idle_hint_and_all_connections_retire(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    backend = _demand_backend(tmp_path, monkeypatch)
    opened = []
    connect = backend._connect
    lock = threading.Lock()
    barrier = threading.Barrier(32, timeout=10)

    def tracked(**kwargs):
        connection = connect(**kwargs)
        with lock:
            opened.append(connection)
        return connection

    def read(index):
        def operation(session):
            barrier.wait()
            return session.fetch_one('SELECT ? AS value', (index,))['value']
        return backend.query('system.baseline', operation, time.monotonic() + 15)

    monkeypatch.setattr(backend, '_connect', tracked)
    assert backend.metrics()['read_pool_size'] == 0
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=32) as executor:
            results = list(executor.map(read, range(32)))
        assert results == list(range(32))
        assert len(opened) == 32 and backend.metrics()['read_peak_leases'] == 32
        assert backend.metrics()['read_pool_capacity'] == 0
        assert backend.metrics()['read_active_leases'] == 0
        assert backend.metrics()['read_pool_size'] == 1
        print(f'32 real SQLite readers / idle hint 1: {(time.monotonic()-started)*1000:.2f} ms')
    finally:
        backend.close()
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')


@pytest.mark.parametrize('delay_connect', [False, True])
def test_close_waits_for_borrowed_or_preparing_readers(tmp_path, monkeypatch, delay_connect):
    import threading

    backend = _demand_backend(tmp_path, monkeypatch)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    results, errors = [], []
    connect = backend._connect

    def delayed(**kwargs):
        entered.set()
        assert release.wait(5)
        return connect(**kwargs)

    def operation(session):
        if not delay_connect:
            entered.set()
            assert release.wait(5)
        return session.fetch_one('SELECT 1 AS value')['value']

    def query():
        try:
            results.append(backend.query('system.baseline', operation, time.monotonic() + 10))
        except BaseException as error:
            errors.append(error)

    def close():
        backend.close()
        closed.set()

    if delay_connect:
        monkeypatch.setattr(backend, '_connect', delayed)
    reader = threading.Thread(target=query)
    closer = threading.Thread(target=close)
    try:
        reader.start()
        assert entered.wait(2)
        closer.start()
        with backend._read_condition:
            assert backend._read_condition.wait_for(lambda: backend._closed, timeout=2)
        assert not closed.is_set()
        with pytest.raises(StorageError) as raised:
            backend.query('system.baseline', lambda session: None, time.monotonic() + 1)
        assert raised.value.code == 'database_unavailable'
        release.set()
        reader.join(3)
        closer.join(3)
        assert not reader.is_alive() and not closer.is_alive()
        assert closed.is_set() and not errors and results == [1]
        assert backend.metrics()['read_active_leases'] == 0
    finally:
        release.set()
        reader.join(5)
        if closer.ident is not None:
            closer.join(5)
        backend.close()


def test_real_connection_failure_releases_preparation_ownership(tmp_path, monkeypatch):
    backend = _demand_backend(tmp_path, monkeypatch)
    error = sqlite3.OperationalError('injected physical connection failure')
    error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(backend, '_connect', fail)
    try:
        with pytest.raises(StorageError) as raised:
            backend.query('system.baseline', lambda session: pytest.fail('failed reader ran'),
                          time.monotonic() + 5)
        assert raised.value.code == 'database_unavailable'
        assert backend.metrics()['read_active_leases'] == 0
    finally:
        backend.close()


def test_progress_cleanup_failure_retires_lease_and_preserves_classified_error(tmp_path, monkeypatch):
    backend = _demand_backend(tmp_path, monkeypatch)
    class Connection:
        closed = False
        def set_progress_handler(self, callback, instructions):
            if callback is None:
                error = sqlite3.OperationalError('injected progress cleanup failure')
                error.sqlite_errorcode = sqlite3.SQLITE_IOERR
                raise error
        def execute(self, sql):
            return None
        def rollback(self):
            return None
        def close(self):
            self.closed = True
    connection = Connection()
    now = time.monotonic()
    backend._read_pool.put(sqlite_adapter._ReadSlot(connection, now, now))
    try:
        with pytest.raises(StorageError) as raised:
            backend.query('system.baseline', lambda session: {'value': 1}, time.monotonic() + 5)
        assert raised.value.code == 'database_unavailable' and connection.closed
        assert backend.metrics()['read_active_leases'] == 0
        assert backend.metrics()['read_pool_size'] == 0
    finally:
        backend.close()
