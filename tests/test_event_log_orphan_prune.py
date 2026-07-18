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


def test_prune_batches_beyond_single_batch_size():
    """A backlog LARGER than _PRUNE_BATCH_TASKS is fully reaped across batches.

    Regression for the permanent-failure loop: the old unbounded single DELETE
    exceeded PG's 120s statement_timeout on a large backlog and rolled back
    WHOLE (zero progress). The batched-commit rewrite must reap a backlog that
    spans multiple batches — proving each batch's progress is durable and the
    loop drains the whole set (bounded by _PRUNE_MAX_BATCHES * _PRUNE_BATCH_TASKS).
    """
    db = get_thread_db(DOMAIN_CHAT)
    old_ts = int(time.time() * 1000) - ev.EVENT_TTL_MS - 60_000
    n = ev._PRUNE_BATCH_TASKS + 5  # just over one batch → forces a 2nd batch
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
