"""SQLite online reclamation stays inside a steady-state writer budget."""

from __future__ import annotations

import pytest

from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.operations_pkg._common import _system_reclaim
from lib.storage_sidecar.operations_pkg._optimizer import _log_aggregate_flush
from lib.storage_sidecar.reclaim_policy import (
    copy_capacity_requirement,
    online_reclaim_allowed,
    requires_offline_compaction,
)


pytestmark = pytest.mark.unit


def test_reclaim_and_copy_thresholds_are_pure_and_bounded():
    assert requires_offline_compaction(1_048_576, 4_194_304) is True
    assert requires_offline_compaction(1_048_575, 4_194_300) is False
    assert requires_offline_compaction(2_000_000, 10_000_000) is False

    small = copy_capacity_requirement(10)
    assert small['reserve_bytes'] == 1024 ** 3
    large = copy_capacity_requirement(1000 * 1024 ** 3)
    assert large['reserve_bytes'] == 8 * 1024 ** 3
    assert large['required_free_bytes'] == 1008 * 1024 ** 3

    assert online_reclaim_allowed('local-block') is True
    assert online_reclaim_allowed('container-overlay') is True
    assert online_reclaim_allowed('memory-filesystem') is True
    assert online_reclaim_allowed('network-filesystem') is False
    assert online_reclaim_allowed('userspace-filesystem') is False
    assert online_reclaim_allowed('unknown') is False


def test_network_authority_reclaim_never_enters_sqlite_writer():
    from lib.storage_sidecar.adapters.sqlite import SQLiteBackend

    backend = object.__new__(SQLiteBackend)
    backend._authority_storage_class = 'network-filesystem'
    backend._authority_filesystem_type = 'beegfs'
    backend._authority_path = __import__('pathlib').Path('/authority/tofu.db')

    class _WriterMustNotRun:
        def submit(self, *_args, **_kwargs):  # pragma: no cover - safety guard
            raise AssertionError('network reclaim entered the SQLite writer')

    backend._writer = _WriterMustNotRun()
    result = backend.command(
        'system.reclaim', '', None, 'maintenance',
        lambda _session: (_ for _ in ()).throw(
            AssertionError('reclaim operation executed')),
        __import__('time').monotonic() + 1,
        receipt_required=False,
    )

    assert result['offline_required'] is True
    assert result['reason_code'] == 'unsupported_storage_topology'
    assert result['storage_class'] == 'network-filesystem'


class _PragmaSession:
    backend = 'sqlite'

    def __init__(self, values):
        self.values = values
        self.executed = []

    def fetch_one(self, sql, _params=()):
        return {'value': self.values[sql]}

    def execute(self, sql, _params=()):
        self.executed.append(sql)
        return 0


def test_bulk_freelist_requires_offline_compaction_without_page_moves():
    session = _PragmaSession({
        'PRAGMA auto_vacuum': 2,
        'PRAGMA freelist_count': 2_000_000,
        'PRAGMA page_count': 3_000_000,
        'PRAGMA page_size': 4096,
    })

    result = _system_reclaim(session, {
        # Even the largest explicit online slice cannot bypass the bulk-file
        # classification boundary.
        'max_pages': 1_048_576,
        'min_free_pages': 1024,
        'budget_ms': 250,
    })

    assert result['offline_required'] is True
    assert result['freelist_ratio'] == pytest.approx(2 / 3, abs=1e-6)
    assert result['freelist_bytes'] == 2_000_000 * 4096
    assert session.executed == []


def test_small_freelist_below_floor_does_not_probe_or_reclaim():
    session = _PragmaSession({
        'PRAGMA auto_vacuum': 2,
        'PRAGMA freelist_count': 100,
    })

    result = _system_reclaim(session, {
        'max_pages': 8192,
        'min_free_pages': 1024,
        'budget_ms': 250,
    })

    assert result == {'reclaimed': 0, 'freelist': 100, 'auto_vacuum': 2}
    assert session.executed == []


def test_log_aggregate_sweep_deletes_at_most_one_bounded_batch():
    import sqlite3

    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE log_aggregates ('
        'fingerprint TEXT PRIMARY KEY, level TEXT NOT NULL, '
        'logger TEXT NOT NULL, template TEXT NOT NULL, sample TEXT NOT NULL, '
        'count BIGINT NOT NULL, first_seen BIGINT NOT NULL, '
        'last_seen BIGINT NOT NULL)')
    connection.execute(
        'CREATE INDEX idx_log_aggregates_last_seen '
        'ON log_aggregates(last_seen, fingerprint)')
    connection.executemany(
        'INSERT INTO log_aggregates VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        [(f'fp-{index:04d}', 'ERROR', 'test', 't', 's', 1, 1, 1)
         for index in range(600)],
    )

    result = _log_aggregate_flush(
        SQLiteSession(connection), {'rows': [], 'cutoff_ms': 2})

    assert result == {'flushed': 0, 'swept': 500}
    assert connection.execute(
        'SELECT count(*) FROM log_aggregates').fetchone()[0] == 100
    connection.close()
