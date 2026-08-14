"""tests/test_event_log_orphan_prune.py — orphaned task_events GC.

Covers the 2026-06-28 second prune pass added to ``_opportunistic_prune``.

Before the fix, ``_opportunistic_prune`` deleted only rows whose ``task_id``
JOINed a terminal ``task_results`` row. Rows written under a ``task_id`` that
NEVER gets a ``task_results`` entry (an "orphan") were structurally invisible
to that JOIN and so were never reaped — permanent litter. The timer-poll
collision bug produced ~160 such orphaned ``(tmr_*, 0/1)`` rows.

The new Pass 2 reaps orphaned rows by their OWN ``ts_ms`` age (no JOIN),
bounded by the same ``EVENT_TTL_MS`` so an in-flight unregistered task is
never reaped. These tests assert all three behaviours on the session SQLite
DB from conftest:
  * an AGED orphan (ts_ms older than TTL, no task_results row) is reaped;
  * a FRESH orphan (recent ts_ms, no task_results row) is SPARED (in-flight
    safety guard);
  * the terminal-task pass still reaps a real finished task's events.
"""

import time
import uuid

import pytest

import lib.tasks_pkg.event_log as ev
from lib.database import DOMAIN_CHAT, get_thread_db

pytestmark = pytest.mark.unit


def _insert_event(db, task_id, event_id, ts_ms):
    db.execute(
        'INSERT INTO task_events (task_id, event_id, ts_ms, type, payload) '
        'VALUES (?, ?, ?, ?, ?)',
        (task_id, event_id, ts_ms, 'tool_result', '{"type":"tool_result"}'),
    )
    db.commit()


def _count(db, task_id):
    r = db.execute('SELECT count(*) FROM task_events WHERE task_id=?', (task_id,)).fetchone()
    return r[0] if r else 0


def _cleanup(db, *task_ids):
    for tid in task_ids:
        try:
            db.execute('DELETE FROM task_events WHERE task_id=?', (tid,))
            db.execute('DELETE FROM task_results WHERE task_id=?', (tid,))
            db.commit()
        except Exception:
            db.rollback()


def test_aged_orphan_is_reaped():
    """An orphan (no task_results) older than EVENT_TTL_MS is deleted by Pass 2."""
    db = get_thread_db(DOMAIN_CHAT)
    tid = 'tmr_' + uuid.uuid4().hex[:8]
    old_ts = int(time.time() * 1000) - ev.EVENT_TTL_MS - 60_000  # 1 min past TTL
    try:
        _insert_event(db, tid, 0, old_ts)
        _insert_event(db, tid, 1, old_ts)
        assert _count(db, tid) == 2

        ev._opportunistic_prune(db)

        assert _count(db, tid) == 0, 'aged orphan rows must be reaped by Pass 2'
    finally:
        _cleanup(db, tid)


def test_fresh_orphan_is_spared():
    """A recent orphan (in-flight unregistered task) is NOT reaped — safety guard."""
    db = get_thread_db(DOMAIN_CHAT)
    tid = 'tmr_' + uuid.uuid4().hex[:8]
    fresh_ts = int(time.time() * 1000)  # just now
    try:
        _insert_event(db, tid, 0, fresh_ts)
        assert _count(db, tid) == 1

        ev._opportunistic_prune(db)

        assert _count(db, tid) == 1, (
            'a fresh orphan must be spared — its ts_ms is within EVENT_TTL_MS, '
            'so it could be an in-flight task that has not yet written task_results')
    finally:
        _cleanup(db, tid)


def test_terminal_task_events_still_reaped():
    """The original Pass 1 (terminal task_results JOIN) still reaps finished tasks."""
    db = get_thread_db(DOMAIN_CHAT)
    tid = 'task_' + uuid.uuid4().hex[:8]
    old_ts = int(time.time() * 1000) - ev.EVENT_TTL_MS - 60_000
    try:
        _insert_event(db, tid, 0, old_ts)
        # A terminal task_results row whose completed_at is past the TTL.
        # conv_id + created_at are NOT NULL with no server default — supply them.
        db.execute(
            'INSERT INTO task_results (task_id, conv_id, status, created_at, completed_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (tid, 'conv-orphan-test', 'done', old_ts, old_ts),
        )
        db.commit()
        assert _count(db, tid) == 1

        ev._opportunistic_prune(db)

        assert _count(db, tid) == 0, 'terminal-task events must still be reaped by Pass 1'
    finally:
        _cleanup(db, tid)


def test_terminal_prune_continues_after_one_tasks_short_tail():
    """The global event-first selector drains all eligible short tails."""
    db = get_thread_db(DOMAIN_CHAT)
    old_ts = int(time.time() * 1000) - ev.EVENT_TTL_MS - 60_000
    tids = ['task_tail_' + uuid.uuid4().hex[:8] for _ in range(2)]
    try:
        for tid in tids:
            for event_id in range(3):
                _insert_event(db, tid, event_id, old_ts)
            db.execute(
                'INSERT INTO task_results '
                '(task_id, conv_id, status, created_at, completed_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (tid, 'conv-tail-test', 'done', old_ts, old_ts),
            )
        db.commit()

        ev._opportunistic_prune(db, include_orphans=False)

        assert [_count(db, tid) for tid in tids] == [0, 0]
    finally:
        _cleanup(db, *tids)


def test_prune_batches_beyond_single_batch_size():
    """A backlog LARGER than _PRUNE_BATCH_ROWS is fully reaped across batches.

    Regression for the permanent-failure loop: the old unbounded single DELETE
    exceeded PG's 120s statement_timeout on a large backlog and rolled back
    WHOLE (zero progress). The batched-commit rewrite must reap a backlog that
    spans multiple batches — proving each batch's progress is durable and the
    loop drains the whole set (bounded by _PRUNE_MAX_BATCHES * _PRUNE_BATCH_ROWS).
    """
    db = get_thread_db(DOMAIN_CHAT)
    old_ts = int(time.time() * 1000) - ev.EVENT_TTL_MS - 60_000
    n = ev._PRUNE_BATCH_ROWS + 5  # just over one batch → forces a 2nd batch
    prefix = 'tmr_batch_' + uuid.uuid4().hex[:6] + '_'
    tids = [f'{prefix}{i}' for i in range(n)]
    try:
        for tid in tids:
            _insert_event(db, tid, 0, old_ts)
        remaining = db.execute(
            "SELECT count(*) FROM task_events WHERE task_id LIKE ?",
            (prefix + '%',)
        ).fetchone()[0]
        assert remaining == n

        ev._opportunistic_prune(db)

        remaining = db.execute(
            "SELECT count(*) FROM task_events WHERE task_id LIKE ?",
            (prefix + '%',)
        ).fetchone()[0]
        assert remaining == 0, (
            'a backlog larger than one batch must be fully reaped across '
            'multiple batched-commit passes')
    finally:
        _cleanup(db, *tids)


def test_prune_uses_one_set_delete_per_batch_not_executemany():
    """Cleanup work must be O(batches), not O(rows) database commands.

    This is a pure seam test: each batch first discovers immutable keys without
    a write transaction, then performs one exact-key DELETE. If per-row writes
    or a SELECT nested inside DELETE return, the command/transaction shape
    below fails.
    """
    class _Cursor:
        def __init__(self, *, rows=(), rowcount=-1):
            self._rows = list(rows)
            self.rowcount = rowcount

        def fetchall(self):
            return self._rows

    class _DB:
        def __init__(self):
            self.calls = []
            self.events = []
            self.commits = 0
            self._batches = iter((
                [(f'task-{i}', i) for i in range(ev._PRUNE_BATCH_ROWS)],
                [(f'tail-{i}', i) for i in range(5)],
            ))

        def execute(self, sql, params):
            self.calls.append((sql, params))
            self.events.append('select' if sql.startswith('SELECT') else 'delete')
            if sql.startswith('SELECT'):
                return _Cursor(rows=next(self._batches))
            return _Cursor(rowcount=len(params) // 2)

        def commit(self):
            self.commits += 1
            self.events.append('commit')

        def rollback(self):
            raise AssertionError('successful set deletes must not roll back')

    db = _DB()
    selected = (
        'SELECT te.task_id, te.event_id FROM task_events te '
        'WHERE te.ts_ms < ? ORDER BY te.ts_ms ASC LIMIT ?'
    )
    deleted = ev._prune_selected_rows(db, selected, (123,), 'unit prune')

    assert deleted == ev._PRUNE_BATCH_ROWS + 5
    assert len(db.calls) == 4
    assert db.commits == 2
    assert db.events == [
        'select', 'delete', 'commit', 'select', 'delete', 'commit']
    delete_calls = [call for call in db.calls if call[0].startswith('DELETE')]
    assert len(delete_calls) == 2
    assert all('SELECT' not in sql for sql, _params in delete_calls)
    assert all('(task_id, event_id) IN (' in sql
               for sql, _params in delete_calls)


def test_normal_maintenance_can_skip_expensive_orphan_anti_join():
    """The 15-second terminal sweep must not prove global orphan absence."""
    class _Cursor:
        rowcount = 0

        def fetchall(self):
            return []

    class _DB:
        def __init__(self):
            self.sql = []

        def execute(self, sql, _params):
            self.sql.append(sql)
            return _Cursor()

        def commit(self): pass
        def rollback(self): pass

    db = _DB()
    ev._opportunistic_prune(db, include_orphans=False)

    assert len(db.sql) == 2  # terminal streaming + terminal structural tiers
    assert all('task_results tr' in sql for sql in db.sql)
    assert all('NOT EXISTS' not in sql for sql in db.sql)
    assert 'JOIN task_results tr ON tr.task_id = te.task_id' in db.sql[0]
    assert 'ORDER BY te.ts_ms' in db.sql[0]
    assert 'probe.' not in db.sql[0]
    assert 'messages_snapshot' in db.sql[0]
    # A literal predicate is required for reliable partial-index implication
    # even under a future generic prepared plan.
    assert db.sql[0].count('?') == 3  # two cutoffs + bounded LIMIT


def test_sqlite_terminal_stream_prune_is_task_first_and_covering():
    """SQLite must not scan the historical event tier to prove no matches."""
    class _Cursor:
        rowcount = 0

        def fetchall(self):
            return []

    class _DB:
        dialect = 'sqlite'

        def __init__(self):
            self.sql = []

        def execute(self, sql, _params):
            self.sql.append(' '.join(sql.split()))
            return _Cursor()

        def commit(self): pass
        def rollback(self): pass

    db = _DB()
    ev._opportunistic_prune(db, include_orphans=False)

    streaming_sql = db.sql[0]
    assert streaming_sql.startswith(
        'SELECT te.task_id, te.event_id FROM task_results tr')
    assert 'INDEXED BY idx_task_terminal_retention' in streaming_sql
    assert 'CROSS JOIN task_events te' in streaming_sql
    assert 'INDEXED BY idx_task_events_stream_task_ts' in streaming_sql
    assert 'te.task_id=tr.task_id' in streaming_sql


def test_backlog_batches_accelerate_events_but_bound_wide_results():
    """Event catch-up must not multiply multi-MiB task-result deletion I/O."""
    assert ev._PRUNE_MAX_BATCHES >= 8
    assert ev._PRUNE_BATCH_ROWS <= 500, (
        'FUSE-backed production deletes exceeded 10s at 2,000 rows; keep each '
        'online maintenance transaction latency-bounded')
    assert ev._RESULT_PRUNE_MAX_BATCHES < ev._PRUNE_MAX_BATCHES
    assert ev._RESULT_PRUNE_BATCH_ROWS <= 25
    assert (ev._PRUNE_BATCH_ROWS * ev._PRUNE_MAX_BATCHES) <= 640_000
    assert (ev._RESULT_PRUNE_BATCH_ROWS * ev._RESULT_PRUNE_MAX_BATCHES) <= 16_000


def test_task_result_prune_waits_until_all_event_tiers_are_drained():
    """Deleting a result first must not manufacture an orphan event backlog."""
    class Cursor:
        rowcount = 0

        def fetchall(self):
            return []

    class DB:
        def __init__(self): self.sql = []
        def execute(self, sql, _params):
            self.sql.append(' '.join(sql.split()))
            return Cursor()
        def commit(self): pass
        def rollback(self): pass

    db = DB()
    ev._prune_terminal_task_results(db)
    assert len(db.sql) == 2
    assert all(
        'NOT EXISTS (SELECT 1 FROM task_events te WHERE te.task_id=tr.task_id)'
        in sql for sql in db.sql)


def test_task_result_prune_discovers_before_exact_key_delete():
    """Wide-result relation scans must finish before a short write begins."""
    class Cursor:
        def __init__(self, *, rows=(), rowcount=-1):
            self._rows = rows
            self.rowcount = rowcount

        def fetchall(self):
            return self._rows

    class DB:
        def __init__(self):
            self.calls = []
            self.events = []
            self._selection = iter((
                [('old-task',)],  # orphan pass: one short, exhaustive batch
                [],              # linked pass: exhausted
            ))

        def execute(self, sql, params):
            self.calls.append((' '.join(sql.split()), params))
            if sql.startswith('SELECT'):
                self.events.append('select')
                return Cursor(rows=next(self._selection))
            self.events.append('delete')
            return Cursor(rowcount=1)

        def commit(self):
            self.events.append('commit')

        def rollback(self):
            raise AssertionError('successful retention must not roll back')

    db = DB()
    assert ev._prune_terminal_task_results(db) == 1
    assert db.events == ['select', 'delete', 'commit', 'select']
    deletes = [sql for sql, _params in db.calls if sql.startswith('DELETE')]
    assert deletes == ['DELETE FROM task_results WHERE task_id IN (?)']
    assert 'SELECT' not in deletes[0]


def test_orphan_tiers_are_separate_indexable_queries():
    """Do not regress to the OR predicate that forces a PG seq scan."""
    class _Cursor:
        rowcount = 0

        def fetchall(self):
            return []

    class _DB:
        def __init__(self):
            self.sql = []

        def execute(self, sql, _params):
            self.sql.append(sql)
            return _Cursor()

        def commit(self): pass
        def rollback(self): pass

    db = _DB()
    ev._opportunistic_prune(db, include_orphans=True)
    orphan_sql = [sql for sql in db.sql if 'NOT EXISTS' in sql]
    assert len(orphan_sql) == 2
    assert all(' OR ' not in sql for sql in orphan_sql)
    assert 'messages_snapshot' in orphan_sql[0]
    assert orphan_sql[0].count('?') == 2  # cutoff + bounded LIMIT


def test_old_terminal_task_result_without_conversation_is_reaped():
    db = get_thread_db(DOMAIN_CHAT)
    tid = 'result_orphan_' + uuid.uuid4().hex[:8]
    old_ts = int(time.time() * 1000) - ev._ORPHAN_RESULT_TTL_MS - 60_000
    try:
        db.execute(
            'INSERT INTO task_results '
            '(task_id, conv_id, status, created_at, completed_at, content) '
            'VALUES (?, ?, ?, ?, ?, ?)',
            (tid, 'missing-conv-' + tid, 'done', old_ts, old_ts, 'large copy'),
        )
        db.commit()
        assert ev._prune_terminal_task_results(db) >= 1
        assert db.execute(
            'SELECT 1 FROM task_results WHERE task_id=?', (tid,)
        ).fetchone() is None
    finally:
        _cleanup(db, tid)


def test_task_result_retention_never_reaps_running_or_fresh_rows():
    db = get_thread_db(DOMAIN_CHAT)
    old_running = 'result_running_' + uuid.uuid4().hex[:8]
    fresh_done = 'result_fresh_' + uuid.uuid4().hex[:8]
    old_ts = int(time.time() * 1000) - ev._LINKED_RESULT_TTL_MS - 60_000
    fresh_ts = int(time.time() * 1000)
    try:
        db.execute(
            'INSERT INTO task_results '
            '(task_id, conv_id, status, created_at, completed_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (old_running, 'missing-' + old_running, 'running', old_ts, old_ts),
        )
        db.execute(
            'INSERT INTO task_results '
            '(task_id, conv_id, status, created_at, completed_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (fresh_done, 'missing-' + fresh_done, 'done', fresh_ts, fresh_ts),
        )
        db.commit()
        ev._prune_terminal_task_results(db)
        assert _count_result_rows(db, old_running) == 1
        assert _count_result_rows(db, fresh_done) == 1
    finally:
        _cleanup(db, old_running, fresh_done)


def _count_result_rows(db, task_id):
    row = db.execute(
        'SELECT count(*) FROM task_results WHERE task_id=?', (task_id,)
    ).fetchone()
    return int(row[0] if row else 0)
