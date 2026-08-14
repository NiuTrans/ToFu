"""Regression coverage for the overnight-suspend stale-PG-connection bug.

Symptom (from production logs)
------------------------------
After the machine/FUSE mount is frozen overnight (laptop lid close / VM pause /
device disconnect) and the user reconnects the next day, the FIRST Send/Regen
fails with::

    psycopg2.OperationalError: server closed the connection unexpectedly

and the UI shows "offline" for ~16 minutes (POST /api/client-error … SLOW
965s) while the pool slowly drains its dead connections one request at a time.

Two root causes, both fixed and covered here:

1. ``_test_pg_connection`` trusted ``time.monotonic()`` to decide whether a
   pooled connection was recently-used enough to SKIP the ``SELECT 1`` probe.
   ``time.monotonic()`` FREEZES across an OS suspend, so a connection whose
   backend PG killed overnight still looked "used seconds ago" → the probe was
   skipped → a DEAD connection was handed to the request. The fix cross-checks
   the WALL clock (``_last_used_wall``), which advances across a suspend.

2. Raw ``db.execute()`` read sites (e.g. ``persist_conv_messages`` settings
   SELECT) that do NOT route through ``db_execute_with_retry`` had no reconnect
   path, so a dead pooled connection surfaced as a hard 500. ``PgConnection``
   now transparently reconnects once on a dead-connection error when the
   connection has issued no write yet.

These tests exercise the PURE logic with lightweight fakes — no live PG needed.

Run:  pytest tests/test_db_stale_conn_recovery.py -v
"""
from __future__ import annotations

import time

import pytest

import lib.database._core as core
from lib.database._wrappers import PgConnection


class OperationalError(Exception):
    """Fake psycopg2 OperationalError (matched by name in the belt logic)."""


class _FakeCursor:
    def __init__(self, raw):
        self._raw = raw
        self.description = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self._raw.probe_count += 1
        if self._raw.fail_probe:
            raise OperationalError('server closed the connection unexpectedly')
        return self

    def fetchone(self):
        return (1,)

    def fetchall(self):
        return [(1,)]

    def close(self):
        pass


class _FakeRaw:
    """Minimal stand-in for a psycopg2 connection, tracking probe activity."""

    def __init__(self, closed=False, fail_probe=False):
        self.closed = closed
        self.fail_probe = fail_probe
        self.probe_count = 0
        self.rollback_count = 0
        self.commit_count = 0

    def rollback(self):
        self.rollback_count += 1

    def commit(self):
        self.commit_count += 1

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def _make_pgconn(*, monotonic_idle, wall_idle, fail_probe=False):
    """Build a PgConnection-like wrapper with controllable idle times."""
    raw = _FakeRaw(fail_probe=fail_probe)
    conn = PgConnection.__new__(PgConnection)
    conn._conn = raw
    conn._closed = False
    conn._dirty = False
    conn._transaction_pinned = False
    now_mono = time.monotonic()
    conn._created_at = now_mono - 1.0          # young connection (not age-recycled)
    conn._last_used = now_mono - monotonic_idle
    conn._last_used_wall = time.time() - wall_idle
    conn.row_factory = None
    return conn, raw


@pytest.mark.unit
class TestSuspendStaleGuard:
    """_test_pg_connection must probe after a WALL-clock gap even when the
    monotonic clock (frozen across suspend) says the conn was just used."""

    def test_recent_by_both_clocks_skips_probe(self):
        """Genuinely recently-used conn: neither clock past threshold → trust,
        no SELECT 1 probe (the fast path this optimization exists for)."""
        conn, raw = _make_pgconn(monotonic_idle=1.0, wall_idle=1.0)
        assert core._test_pg_connection(conn) is True
        assert raw.probe_count == 0, 'should NOT probe a genuinely fresh conn'

    def test_suspend_frozen_monotonic_forces_probe(self):
        """THE BUG: monotonic says 2s idle (frozen across suspend) but the wall
        clock shows an overnight gap → the guard MUST force the SELECT 1 probe
        instead of blindly trusting the connection."""
        conn, raw = _make_pgconn(monotonic_idle=2.0, wall_idle=36000.0)
        core._test_pg_connection(conn)
        assert raw.probe_count == 1, (
            'wall-clock gap must force a health probe even when monotonic idle '
            'is below threshold (suspend froze the monotonic clock)')

    def test_dead_conn_after_suspend_reports_unhealthy(self):
        """A connection whose backend died overnight: the forced probe fails →
        _test_pg_connection returns False so the pool discards it (instead of
        handing it to the request, which was the production 500)."""
        conn, raw = _make_pgconn(monotonic_idle=2.0, wall_idle=36000.0, fail_probe=True)
        assert core._test_pg_connection(conn) is False
        assert raw.probe_count == 1

    def test_probe_refreshes_wall_clock(self):
        """A successful probe updates BOTH clocks so the next call within the
        window can take the fast path again."""
        conn, raw = _make_pgconn(monotonic_idle=40.0, wall_idle=40.0)
        assert core._test_pg_connection(conn) is True
        assert raw.probe_count == 1
        # Wall clock refreshed → an immediate re-check skips the probe.
        assert core._test_pg_connection(conn) is True
        assert raw.probe_count == 1, 'second check within window must not re-probe'

    def test_successful_probe_finishes_its_own_transaction(self):
        """ROLLBACK; SELECT 1 must end with another ROLLBACK, not an idle tx."""
        conn, raw = _make_pgconn(monotonic_idle=40.0, wall_idle=40.0)
        assert core._test_pg_connection(conn) is True
        assert raw.rollback_count == 2, (
            'health probe must rollback both prior residue and its SELECT txn')

    @pytest.mark.parametrize('state_attr', ['_dirty', '_transaction_pinned'])
    def test_health_check_never_rolls_back_caller_owned_transaction(
            self, state_attr):
        conn, raw = _make_pgconn(monotonic_idle=40.0, wall_idle=40.0)
        setattr(conn, state_attr, True)

        assert core._test_pg_connection(conn) is True
        assert raw.rollback_count == 0
        assert raw.probe_count == 0


@pytest.mark.unit
class TestReadTransactionHygiene:
    """Plain reads auto-finish; writes and explicit/locking txns stay pinned."""

    def test_plain_select_is_rolled_back_before_result_is_consumed(self):
        conn, raw = _make_pgconn(monotonic_idle=0, wall_idle=0)
        cur = conn.execute('SELECT 1')
        assert raw.rollback_count == 1
        assert cur.fetchone() == (1,), 'rollback must not invalidate buffered rows'
        assert conn._dirty is False

    def test_write_then_read_remains_atomic_until_commit(self):
        conn, raw = _make_pgconn(monotonic_idle=0, wall_idle=0)
        conn.execute('UPDATE conversations SET title=title')
        conn.execute('SELECT 1').fetchone()
        assert raw.rollback_count == 0, 'read after a write belongs to the write txn'
        assert conn._dirty is True
        conn.commit()
        assert raw.commit_count == 1
        assert conn._dirty is False

    def test_locking_select_stays_open_until_commit(self):
        conn, raw = _make_pgconn(monotonic_idle=0, wall_idle=0)
        conn.execute('SELECT balance_micro FROM billing_wallets FOR UPDATE').fetchone()
        assert raw.rollback_count == 0
        assert conn._transaction_pinned is True
        conn.commit()
        assert conn._transaction_pinned is False

    def test_explicit_read_first_transaction_is_not_split(self):
        conn, raw = _make_pgconn(monotonic_idle=0, wall_idle=0)
        conn.begin()
        conn.execute('SELECT 1').fetchone()
        conn.execute('UPDATE conversations SET title=title')
        assert raw.rollback_count == 0
        conn.rollback()
        assert raw.rollback_count == 1
        assert conn._dirty is False
        assert conn._transaction_pinned is False

    def test_dead_raw_connection_return_releases_pool_slot(self, monkeypatch):
        """A PG-killed raw connection still owns the wrapper's semaphore slot."""
        conn, raw = _make_pgconn(monotonic_idle=0, wall_idle=0)
        raw.closed = True

        class _Sem:
            def __init__(self):
                self.releases = 0

            def release(self):
                self.releases += 1

        sem = _Sem()
        conn._semaphore = sem
        monkeypatch.setattr(core, '_BACKEND', 'pg')
        monkeypatch.setattr(core, '_conn_count', 1)

        core._pool_put(conn)

        assert conn._closed is True
        assert sem.releases == 1
        assert core._conn_count == 0


@pytest.mark.unit
class TestTransparentReconnectBelt:
    """PgConnection.execute reconnects once on a dead-connection error when the
    connection has issued no write yet — rescuing raw-SELECT call sites."""

    def test_reconnects_on_dead_error_when_clean(self, monkeypatch):
        raw_dead = _FakeRaw(fail_probe=True)
        conn = PgConnection.__new__(PgConnection)
        conn._conn = raw_dead
        conn._closed = False
        conn._dirty = False
        conn._transaction_pinned = False
        conn._created_at = time.monotonic()
        conn._last_used = time.monotonic()
        conn._last_used_wall = time.time()
        conn.row_factory = None

        raw_live = _FakeRaw(fail_probe=False)

        def _fake_reconnect(db):
            db._conn = raw_live
            db._dirty = False
            return True

        monkeypatch.setattr(core, '_reconnect_pg_inplace', _fake_reconnect)
        # First cursor.execute raises a dead-signature error; belt reconnects
        # to raw_live and retries → the SELECT succeeds.
        cur = conn.execute('SELECT settings FROM conversations WHERE id=?', ('c1',))
        assert cur.fetchone() == (1,)
        assert conn._conn is raw_live, 'connection should have been swapped'

    def test_does_not_retry_when_dirty(self, monkeypatch):
        """A connection that already issued a write must NOT be silently
        reconnected+retried (that would replay/lose the pending write)."""
        raw_dead = _FakeRaw(fail_probe=True)
        conn = PgConnection.__new__(PgConnection)
        conn._conn = raw_dead
        conn._closed = False
        conn._dirty = True  # ← a write already happened in this txn
        conn._transaction_pinned = False
        conn._created_at = time.monotonic()
        conn._last_used = time.monotonic()
        conn._last_used_wall = time.time()
        conn.row_factory = None

        called = {'n': 0}
        monkeypatch.setattr(core, '_reconnect_pg_inplace',
                            lambda db: called.__setitem__('n', called['n'] + 1) or True)
        with pytest.raises(Exception):
            conn.execute('UPDATE conversations SET x=1')
        assert called['n'] == 0, 'must not reconnect a dirty connection'
