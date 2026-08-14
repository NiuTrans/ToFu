"""Bounded SQLite free-page reclamation for new personal installations."""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def sqlite_db(tmp_path):
    from lib.database import _core as core

    snapshot = core.reset_sqlite_for_tests(str(tmp_path / 'incremental.db'))
    db = core._new_sqlite_connection()
    try:
        yield core, db
    finally:
        db.close()
        core.restore_db_state(snapshot)


def test_incremental_vacuum_reclaims_only_a_bounded_slice(
        sqlite_db, monkeypatch):
    from lib.tasks_pkg import event_log

    core, db = sqlite_db
    assert db.execute('PRAGMA auto_vacuum').fetchone()[0] == 2
    db.execute('PRAGMA page_size=4096')
    db.execute('CREATE TABLE vacuum_probe (payload BLOB)')
    payload = os.urandom(128 * 1024)
    db.executemany('INSERT INTO vacuum_probe(payload) VALUES (?)',
                   [(payload,) for _ in range(40)])
    db.commit()
    db.execute('DELETE FROM vacuum_probe')
    db.commit()

    before = db.execute('PRAGMA freelist_count').fetchone()[0]
    assert before > 8
    monkeypatch.setattr(event_log, '_SQLITE_VACUUM_MIN_FREE_PAGES', 1)
    monkeypatch.setattr(event_log, '_SQLITE_VACUUM_PAGES', 8)

    reclaimed = event_log._sqlite_incremental_vacuum(db)
    after = db.execute('PRAGMA freelist_count').fetchone()[0]

    assert reclaimed == 8
    assert before - after == reclaimed


def test_incremental_vacuum_is_noop_for_legacy_mode(
        sqlite_db, tmp_path, monkeypatch):
    from lib.tasks_pkg import event_log

    core, db = sqlite_db
    db.close()
    legacy_path = tmp_path / 'legacy.db'
    import sqlite3
    with sqlite3.connect(legacy_path) as raw:
        raw.execute('CREATE TABLE legacy_data (id INTEGER PRIMARY KEY)')
        raw.commit()
    monkeypatch.setattr(core, 'DB_PATH', str(legacy_path))
    legacy = core._new_sqlite_connection()
    try:
        assert legacy.execute('PRAGMA auto_vacuum').fetchone()[0] == 0
        monkeypatch.setattr(event_log, '_SQLITE_VACUUM_MIN_FREE_PAGES', 1)
        assert event_log._sqlite_incremental_vacuum(legacy) == 0
        assert legacy.execute('PRAGMA auto_vacuum').fetchone()[0] == 0
    finally:
        legacy.close()


def test_incremental_vacuum_stops_at_wall_budget(monkeypatch):
    from lib.database import _core as core
    from lib.tasks_pkg import event_log

    class Cursor:
        def __init__(self, value):
            self.value = value

        def fetchone(self):
            return (self.value,)

    class FakeDB:
        def __init__(self):
            self.free = 100
            self.raw = self
            self._dirty = False
            self.acquired = 0
            self.released = 0

        def execute(self, sql, _params=()):
            if 'auto_vacuum' in sql:
                return Cursor(2)
            if 'freelist_count' in sql:
                return Cursor(self.free)
            if 'incremental_vacuum' in sql:
                self.free -= 1
                return Cursor(0)
            raise AssertionError(sql)

        def _acquire_write_lane(self, _label):
            self.acquired += 1

        def _release_write_lane_if_transaction_ended(self):
            self.released += 1

    ticks = iter((0.000, 0.006, 0.012, 0.018, 0.024))
    monkeypatch.setattr(core, '_BACKEND', 'sqlite')
    monkeypatch.setattr(event_log, '_SQLITE_VACUUM_PAGES', 50)
    monkeypatch.setattr(event_log, '_SQLITE_VACUUM_MIN_FREE_PAGES', 1)
    monkeypatch.setattr(event_log, '_SQLITE_VACUUM_BUDGET_MS', 10)
    monkeypatch.setattr(event_log.time, 'monotonic', lambda: next(ticks))
    db = FakeDB()

    assert event_log._sqlite_incremental_vacuum(db) == 1
    assert db.acquired == 1 and db.released == 1
