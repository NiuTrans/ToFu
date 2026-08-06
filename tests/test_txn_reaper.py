"""tests/test_txn_reaper.py — the leaked-write-transaction reaper (pt_3e3ff7dae98047fe).

WHY: pytest-timeout kills a test's MAIN thread, never its background threads.
A thread stopped mid-write-transaction holds it forever, and in WAL one such
zombie write-locks the whole per-worker DB — every later test's INSERT then
burns its 30s busy_timeout and fails 'database is locked' (CI cascade
2026-08-06: test_error_result_model_metadata / test_task_birth_row /
test_tool_exec_failure_verdict, three suites, two legs).

PINNED: ``_core.reap_idle_write_transactions`` rolls back exactly the ZOMBIE
txn (idle past the threshold) and leaves a live one alone; every pooled
connection is reachable via ``_LIVE_SQLITE_CONNS`` (the reaper's enum seam).
NEUTER: without the reap call the zombie txn survives — proving the belt is
load-bearing, not incidental GC.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_txn_reaper.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def fresh_db(tmp_path):
    from lib.database import _core as c
    snap = c.reset_sqlite_for_tests(str(tmp_path / 'reap.db'))
    try:
        yield c
    finally:
        c.restore_db_state(snap)


def _zombie_conn(c, *, idle_s):
    """A connection holding an open write txn, last active idle_s ago."""
    w = c._new_sqlite_connection()
    w.execute('CREATE TABLE IF NOT EXISTS reap_probe (x INTEGER)')
    w.commit()
    w.execute('INSERT INTO reap_probe VALUES (1)')   # txn left OPEN (leak)
    assert w._conn.in_transaction
    w._last_used -= idle_s
    return w


def test_factory_registers_every_connection(fresh_db):
    """The reaper's enumeration seam: every new wrapper lands in the WeakSet."""
    w = fresh_db._new_sqlite_connection()
    assert w in list(fresh_db._LIVE_SQLITE_CONNS)
    w.close()


def test_reaper_rolls_back_only_the_zombie(fresh_db):
    c = fresh_db
    live = c._new_sqlite_connection()
    live.execute('CREATE TABLE IF NOT EXISTS reap_live (x INTEGER)')
    live.commit()
    zombie = _zombie_conn(c, idle_s=10.0)   # DDL 也需写锁——先建表再开僵尸事务

    reaped = c.reap_idle_write_transactions(idle_s=1.0)

    assert reaped == 1
    assert not zombie._conn.in_transaction, 'zombie txn must be rolled back'

    live.execute('INSERT INTO reap_live VALUES (1)')   # lock-free now
    assert live._conn.in_transaction
    reaped2 = c.reap_idle_write_transactions(idle_s=1.0)
    assert reaped2 == 0, 'a live txn must survive the reaper'
    assert live._conn.in_transaction

    # The zombie's uncommitted row is gone for everyone.
    live.rollback()
    fresh = c._new_sqlite_connection()
    n = fresh.execute('SELECT COUNT(*) FROM reap_probe').fetchone()[0]
    assert n == 0, 'rolled-back rows must not become visible'
    zombie.close()
    live.close()
    fresh.close()


def test_NEUTER_without_reap_zombie_survives(fresh_db):
    """Negative control: skip the reap and the leaked txn persists — the belt
    is what saves the worker DB, not connection GC or timeouts."""
    zombie = _zombie_conn(fresh_db, idle_s=10.0)
    assert zombie._conn.in_transaction
    zombie._conn.rollback()   # manual cleanup so the fixture chain stays clean
    zombie.close()
