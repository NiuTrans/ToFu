"""Executable reproduction of the historical SQLite lock failure.

WAL allows readers alongside one writer; it does *not* allow two simultaneous
writers.  A leaked or long transaction therefore makes every second writer
wait through ``busy_timeout`` and then raise ``database is locked``.  The
single-writer/batch lane is the architectural fix; increasing the timeout only
makes the same failure slower.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def fresh_db(tmp_path):
    from lib.database import _core as core
    snapshot = core.reset_sqlite_for_tests(str(tmp_path / 'writer-lock.db'))
    try:
        yield core
    finally:
        core.restore_db_state(snapshot)


def test_wal_reader_continues_but_second_writer_times_out(fresh_db):
    core = fresh_db
    # Use raw connections to preserve the historical mechanism independently
    # of the application wrapper's new writer lane.
    owner = sqlite3.connect(core.DB_PATH, timeout=0.05,
                            check_same_thread=False, isolation_level='DEFERRED')
    contender = sqlite3.connect(core.DB_PATH, timeout=0.05,
                                check_same_thread=False, isolation_level='DEFERRED')
    try:
        owner.execute('CREATE TABLE lock_probe (id INTEGER PRIMARY KEY)')
        owner.commit()

        # Writer 1 owns SQLite's single write slot and deliberately parks it.
        owner.execute('INSERT INTO lock_probe VALUES (1)')
        assert owner.in_transaction

        # WAL still serves a consistent pre-transaction snapshot to readers.
        assert contender.execute('SELECT COUNT(*) FROM lock_probe').fetchone()[0] == 0

        # But writer 2 cannot enter.  Use a short timeout so the regression
        # test proves the mechanism without burning production's historical
        # 30-second timeout.
        contender.execute('PRAGMA busy_timeout=50')
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match='database is locked'):
            contender.execute('INSERT INTO lock_probe VALUES (2)')
        elapsed = time.monotonic() - started
        assert 0.04 <= elapsed < 1.0, (
            f'expected busy_timeout-shaped failure, observed {elapsed:.3f}s')

        # Ending the leaked/long transaction releases the single writer slot;
        # the exact same second write now succeeds.
        owner.rollback()
        contender.rollback()
        contender.execute('INSERT INTO lock_probe VALUES (2)')
        contender.commit()
        assert contender.execute('SELECT COUNT(*) FROM lock_probe').fetchone()[0] == 1
    finally:
        owner.close()
        contender.close()


def test_connection_uses_measured_wal_checkpoint_knee(
        fresh_db, tmp_path, monkeypatch):
    core = fresh_db
    # A brand-new file starts in DELETE mode, so its one persistent transition
    # to WAL must pass through the application writer lane.
    monkeypatch.setattr(core, 'DB_PATH', str(tmp_path / 'journal-init.db'))
    before = core.get_sqlite_writer_lane_stats()['acquires']
    conn = core._new_sqlite_connection()
    try:
        assert conn.execute('PRAGMA wal_autocheckpoint').fetchone()[0] == 4096
        assert conn.execute('PRAGMA auto_vacuum').fetchone()[0] == 2
        assert core.get_sqlite_writer_lane_stats()['acquires'] == before + 1, (
            'persistent journal/auto-vacuum setup bypassed the writer lane')
    finally:
        conn.close()


def test_concurrent_cold_connections_share_one_safe_wal_transition(
        fresh_db, tmp_path, monkeypatch):
    core = fresh_db
    path = tmp_path / 'cold-connect-race.db'
    monkeypatch.setattr(core, 'DB_PATH', str(path))
    barrier = threading.Barrier(6)
    errors = []

    def _open():
        conn = None
        try:
            barrier.wait(timeout=2)
            conn = core._new_sqlite_connection()
            mode = conn.execute('PRAGMA journal_mode').fetchone()[0]
            if mode != 'wal':
                errors.append(AssertionError(
                    f'cold connection selected journal_mode={mode!r}, not WAL'))
        except Exception as exc:  # pragma: no cover - asserted empty below
            errors.append(exc)
        finally:
            if conn is not None:
                conn.close()

    threads = [threading.Thread(target=_open, name=f'cold-open-{i}')
               for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=4)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    with sqlite3.connect(path) as raw:
        assert raw.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert raw.execute('PRAGMA auto_vacuum').fetchone()[0] == 2


def test_existing_sqlite_file_is_never_rewritten_for_auto_vacuum(
        fresh_db, tmp_path, monkeypatch):
    core = fresh_db
    path = tmp_path / 'legacy-no-auto-vacuum.db'
    with sqlite3.connect(path) as raw:
        raw.execute('CREATE TABLE legacy_data (id INTEGER PRIMARY KEY)')
        raw.commit()
        assert raw.execute('PRAGMA auto_vacuum').fetchone()[0] == 0

    monkeypatch.setattr(core, 'DB_PATH', str(path))
    conn = core._new_sqlite_connection()
    try:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        assert conn.execute('PRAGMA auto_vacuum').fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM legacy_data").fetchone()[0] == 0
    finally:
        conn.close()


def test_sqlite_tuning_parser_falls_back_and_clamps(fresh_db, monkeypatch):
    core = fresh_db
    monkeypatch.setattr(core, 'getenv_compat',
                        lambda _name, default=None: 'not-an-int')
    assert core._bounded_sqlite_setting('X', 17, 2, 30) == 17
    monkeypatch.setattr(core, 'getenv_compat',
                        lambda _name, default=None: '999')
    assert core._bounded_sqlite_setting('X', 17, 2, 30) == 30


def test_application_writer_lane_queues_instead_of_hitting_file_lock(fresh_db):
    core = fresh_db
    owner = core._new_sqlite_connection()
    contender = core._new_sqlite_connection()
    entered = threading.Event()
    finished = threading.Event()
    errors = []

    owner.execute('CREATE TABLE writer_lane_probe (id INTEGER PRIMARY KEY)')
    owner.commit()
    owner.execute('INSERT INTO writer_lane_probe VALUES (1)')
    assert owner._write_lane_held is True

    def _second_writer():
        entered.set()
        try:
            contender.execute('INSERT INTO writer_lane_probe VALUES (2)')
            contender.commit()
        except Exception as exc:  # pragma: no cover - asserted empty below
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=_second_writer, name='second-sqlite-writer')
    thread.start()
    assert entered.wait(1)
    time.sleep(0.05)
    assert not finished.is_set(), 'second writer should be queued behind owner'
    queued = core.get_sqlite_writer_lane_stats()
    assert queued['held'] == 1
    assert queued['waiting'] >= 1
    assert queued['owner_age_seconds'] >= 0.04

    owner.rollback()
    thread.join(timeout=2)
    try:
        assert finished.is_set()
        assert errors == []
        assert contender.execute(
            'SELECT COUNT(*) FROM writer_lane_probe').fetchone()[0] == 1
    finally:
        owner.close()
        contender.close()


def test_read_first_explicit_transaction_reserves_writer_before_snapshot(fresh_db):
    """A read-first transaction must not hit SQLITE_BUSY_SNAPSHOT on upgrade.

    Plain ``BEGIN; SELECT`` is still deferred: a second connection can commit,
    after which the first connection's INSERT fails immediately while trying
    to upgrade its now-stale snapshot.  The shared facade starts SQLite
    transactions with BEGIN IMMEDIATE, so the contender queues before touching
    the file and both writes complete in transaction order.
    """
    core = fresh_db
    first = core._new_sqlite_connection()
    second = core._new_sqlite_connection()
    contender_entered = threading.Event()
    contender_finished = threading.Event()
    errors = []
    try:
        first.execute('CREATE TABLE read_first_probe '
                      '(id INTEGER PRIMARY KEY, value TEXT)')
        first.commit()

        first.begin()
        assert first.raw.in_transaction
        assert first.execute(
            'SELECT COUNT(*) FROM read_first_probe').fetchone()[0] == 0

        def _contender():
            contender_entered.set()
            try:
                second.execute(
                    'INSERT INTO read_first_probe VALUES (?, ?)', (2, 'second'))
                second.commit()
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                contender_finished.set()

        thread = threading.Thread(target=_contender,
                                  name='read-first-contender')
        thread.start()
        assert contender_entered.wait(1)
        time.sleep(0.05)
        assert not contender_finished.is_set()

        first.execute(
            'INSERT INTO read_first_probe VALUES (?, ?)', (1, 'first'))
        first.commit()
        thread.join(timeout=2)

        assert contender_finished.is_set()
        assert errors == []
        rows = first.execute(
            'SELECT id FROM read_first_probe ORDER BY id').fetchall()
        assert [row[0] for row in rows] == [1, 2]
    finally:
        if 'thread' in locals() and thread.is_alive():
            first.rollback()
            thread.join(timeout=2)
        first.close()
        second.close()


def test_savepoint_reserves_writer_before_mirror_read_snapshot(fresh_db):
    """A top-level SAVEPOINT is a write-intent transaction boundary.

    The messages-row mirror opens a SAVEPOINT, reads the existing row count,
    and only then mutates rows.  If SAVEPOINT does not reserve the application
    writer lane, another connection can commit between that read and the first
    mutation.  SQLite then rejects the stale read->write upgrade immediately
    with SQLITE_BUSY_SNAPSHOT (surfaced as ``database is locked``); increasing
    busy_timeout cannot repair it.

    Pin the exact production ordering: the contender must queue before its
    commit, then both transactions complete without a lock error.
    """
    core = fresh_db
    mirror = core._new_sqlite_connection()
    contender = core._new_sqlite_connection()
    contender_entered = threading.Event()
    contender_finished = threading.Event()
    errors = []
    try:
        mirror.execute(
            'CREATE TABLE savepoint_probe (id INTEGER PRIMARY KEY, value TEXT)')
        mirror.execute(
            'INSERT INTO savepoint_probe VALUES (?, ?)', (1, 'original'))
        mirror.commit()

        before = core.get_sqlite_writer_lane_stats()['acquires']
        mirror.execute('SAVEPOINT tofu_messages_rows_mirror')
        assert mirror.raw.in_transaction
        assert core.get_sqlite_writer_lane_stats()['acquires'] == before + 1
        assert mirror.execute(
            'SELECT value FROM savepoint_probe WHERE id=1').fetchone()[0] == 'original'

        def _contender():
            contender_entered.set()
            try:
                contender.execute(
                    'INSERT INTO savepoint_probe VALUES (?, ?)', (2, 'contender'))
                contender.commit()
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                contender_finished.set()

        thread = threading.Thread(target=_contender,
                                  name='savepoint-mirror-contender')
        thread.start()
        assert contender_entered.wait(1)
        time.sleep(0.05)
        assert not contender_finished.is_set(), (
            'contender committed inside the mirror read/write snapshot window')

        mirror.execute(
            'UPDATE savepoint_probe SET value=? WHERE id=1', ('mirrored',))
        mirror.execute('RELEASE SAVEPOINT tofu_messages_rows_mirror')
        thread.join(timeout=2)

        assert contender_finished.is_set()
        assert errors == []
        rows = mirror.execute(
            'SELECT id, value FROM savepoint_probe ORDER BY id').fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            (1, 'mirrored'), (2, 'contender')]
    finally:
        if 'thread' in locals() and thread.is_alive():
            mirror.rollback()
            thread.join(timeout=2)
        mirror.close()
        contender.close()


def test_top_level_mirror_reserves_sqlite_writer_before_read_across_processes(
        fresh_db):
    """The mirror must reserve SQLite itself, not only the process lane.

    A process-local mutex cannot serialize a second app process.  A top-level
    SAVEPOINT is DEFERRED, so an external writer can still commit after the
    mirror's SELECT and poison its later write upgrade.  The top-level mirror
    boundary must therefore be BEGIN IMMEDIATE; nested mirrors may continue
    to use a SAVEPOINT because their outer write transaction already owns the
    physical SQLite writer slot.
    """
    from lib.database.messages_rows import _mirror_atomically

    core = fresh_db
    mirror = core._new_sqlite_connection()
    # Deliberately bypass the application wrapper to model another process,
    # whose writer lane is a distinct in-memory lock.
    external = sqlite3.connect(
        core.DB_PATH, timeout=2.0, check_same_thread=False,
        isolation_level='DEFERRED')
    mirror_read = threading.Event()
    external_finished = threading.Event()
    errors = []
    try:
        mirror.execute(
            'CREATE TABLE cross_process_mirror_probe '
            '(id INTEGER PRIMARY KEY, value TEXT)')
        mirror.execute(
            'INSERT INTO cross_process_mirror_probe VALUES (?, ?)',
            (1, 'original'))
        mirror.commit()

        def _external_writer():
            assert mirror_read.wait(1)
            try:
                external.execute(
                    'INSERT INTO cross_process_mirror_probe VALUES (?, ?)',
                    (2, 'external'))
                external.commit()
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                external_finished.set()

        thread = threading.Thread(
            target=_external_writer, name='external-sqlite-writer')
        thread.start()

        def _mirror_work():
            assert mirror.execute(
                'SELECT value FROM cross_process_mirror_probe WHERE id=1'
            ).fetchone()[0] == 'original'
            mirror_read.set()
            # On the broken top-level SAVEPOINT boundary the external commit
            # completes here and the UPDATE below raises BUSY_SNAPSHOT.  With
            # BEGIN IMMEDIATE it waits for the mirror commit instead.
            external_finished.wait(0.1)
            mirror.execute(
                'UPDATE cross_process_mirror_probe SET value=? WHERE id=1',
                ('mirrored',))

        _mirror_atomically(mirror, _mirror_work)
        mirror.commit()
        thread.join(timeout=2)

        assert external_finished.is_set()
        assert errors == []
        rows = mirror.execute(
            'SELECT id, value FROM cross_process_mirror_probe ORDER BY id'
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            (1, 'mirrored'), (2, 'external')]
    finally:
        if 'thread' in locals() and thread.is_alive():
            mirror.rollback()
            thread.join(timeout=2)
        mirror.close()
        external.close()


def test_unreserved_raw_transaction_fails_before_wrapper_write(fresh_db):
    """A bypassed read transaction must fail loudly before stale mutation.

    This models future code reaching through ``db.raw`` (or an unclassified
    transaction-control statement), opening a read snapshot without reserving
    the writer, and then returning to the public wrapper for a mutation.  The
    data layer must reject the invalid boundary deterministically instead of
    allowing a timing-dependent BUSY_SNAPSHOT.
    """
    core = fresh_db
    db = core._new_sqlite_connection()
    try:
        db.execute('CREATE TABLE discipline_probe (id INTEGER PRIMARY KEY)')
        db.commit()

        db.raw.execute('BEGIN')  # intentional data-layer bypass
        db.raw.execute('SELECT COUNT(*) FROM discipline_probe').fetchone()
        with pytest.raises(core.SQLiteWriteDisciplineError,
                           match='without reserving the writer'):
            db.execute('INSERT INTO discipline_probe VALUES (1)')
        db.rollback()

        assert db.execute(
            'SELECT COUNT(*) FROM discipline_probe').fetchone()[0] == 0
    finally:
        db.close()


def test_write_transaction_connection_is_thread_affine(fresh_db):
    """A connection holding the writer lane cannot be mutated by a peer."""
    core = fresh_db
    db = core._new_sqlite_connection()
    errors = []
    try:
        db.execute('CREATE TABLE thread_affinity_probe (id INTEGER PRIMARY KEY)')
        db.commit()
        db.begin()

        def _wrong_thread():
            try:
                db.execute('INSERT INTO thread_affinity_probe VALUES (1)')
            except Exception as exc:  # expected and asserted below
                errors.append(exc)

        thread = threading.Thread(target=_wrong_thread, name='wrong-db-thread')
        thread.start()
        thread.join(timeout=1)

        assert not thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], core.SQLiteWriteDisciplineError)
        assert 'different thread' in str(errors[0])
        db.rollback()
        assert db.execute(
            'SELECT COUNT(*) FROM thread_affinity_probe').fetchone()[0] == 0
    finally:
        db.rollback()
        db.close()


def test_writer_lane_timeout_is_bounded_and_observable(fresh_db, monkeypatch):
    core = fresh_db
    owner = core._new_sqlite_connection()
    contender = core._new_sqlite_connection()
    owner.execute('CREATE TABLE timeout_probe (id INTEGER PRIMARY KEY)')
    owner.commit()
    owner.execute('INSERT INTO timeout_probe VALUES (1)')
    before = core.get_sqlite_writer_lane_stats()
    monkeypatch.setattr(core, '_BUSY_TIMEOUT_MS', 40)
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError,
                           match='SQLite writer lane timed out'):
            contender.execute('INSERT INTO timeout_probe VALUES (2)')
        elapsed = time.monotonic() - started
        after = core.get_sqlite_writer_lane_stats()
        assert 0.03 <= elapsed < 1.0
        assert after['timeouts'] == before['timeouts'] + 1
        assert after['max_wait_seconds'] >= 0.03
        assert after['waiting'] == 0
    finally:
        owner.rollback()
        owner.close()
        contender.close()


def test_idle_write_watchdog_releases_global_lane_without_restart(fresh_db):
    """Forgotten commits are force-rolled-back and their handle is poisoned.

    This is the production shape from the trading intelligence crawler: a
    DELETE that matched zero rows still opened a transaction, then business
    code returned to long-running network work without committing it.
    """
    core = fresh_db
    owner = core._new_sqlite_connection()
    contender = core._new_sqlite_connection()
    try:
        owner.execute('CREATE TABLE idle_writer_probe (id INTEGER PRIMARY KEY)')
        owner.commit()
        owner.execute('DELETE FROM idle_writer_probe WHERE id=999')
        assert owner.raw.in_transaction
        assert owner._write_lane_held

        owner._last_used -= 10.0
        assert core._force_close_idle_write_transactions(5.0) == 1
        assert owner._closed
        assert core.get_sqlite_writer_lane_stats()['held'] == 0

        contender.execute('INSERT INTO idle_writer_probe VALUES (1)')
        contender.commit()
        assert contender.execute(
            'SELECT COUNT(*) FROM idle_writer_probe').fetchone()[0] == 1
    finally:
        owner.close()
        contender.close()


def test_idle_write_watchdog_never_interrupts_active_sql(fresh_db):
    core = fresh_db
    owner = core._new_sqlite_connection()
    try:
        owner.execute('CREATE TABLE active_writer_probe (id INTEGER PRIMARY KEY)')
        owner.commit()
        owner.execute('INSERT INTO active_writer_probe VALUES (1)')
        owner._last_used -= 10.0
        owner._operation_active = True
        assert core._force_close_idle_write_transactions(5.0) == 0
        assert not owner._closed
        assert owner._write_lane_held
    finally:
        owner._operation_active = False
        owner.rollback()
        owner.close()
