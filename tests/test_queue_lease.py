#!/usr/bin/env python3
"""Tests for the lease-based message-queue dispatch (lib/message_queue.py).

Background — the silent-message-loss bug class (epic pt_4ab943fa, owner
approved 2026-07-25, option A):
  ``dequeue_next`` used to DELETE the durable queue row BEFORE the replacement
  task was guaranteed to exist. ``dispatch_next_queued`` then had four silent
  ``return None`` exits after the delete (CAS-append failure, empty
  api_messages, spawn failure). Any of them = the durable row gone, no task,
  nothing re-dispatches — a queued human message vanished with one warning log.

The fix: dequeue takes a LEASE (``leased_until`` / ``lease_task_id`` columns)
instead of deleting; the row is deleted only after ``spawn_task`` succeeds;
failure paths release the lease immediately; a reaper
(``reap_expired_queue_leases``, riding the manager maintenance tick) reclaims
rows whose lease expired without a live task in the registry and re-dispatches
them. Owner's design requirements encoded as tests:

  1. spawn failure → row survives + lease released + re-dispatch works.
  2. crash after lease (expired lease, dead task) → reaper reclaims + spawns.
  3. registry liveness: expired lease whose task is STILL RUNNING → lease is
     extended, NO re-dispatch (no double-dispatch).
  4. fresh (unexpired) lease → dequeue skips the row (a completion hook can
     never double-dispatch a conv whose dispatch is mid-flight).
  5. KIND_AUTOPILOT sentinel is lease-immune — the reaper never touches it.
  6. three queued messages dispatch in (priority, position) order and the
     deferred delete renumbers positions exactly like the old eager delete.
  7. the two INTENTIONAL delete sites (remove_message / clear_autopilot_marker)
     keep their byte-identical semantics.
"""

import time

import pytest

import lib.message_queue as mq

pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]


def _cid():
    return f'test-lease-{time.time_ns()}'


def _row(db, queue_id):
    return db.execute(
        'SELECT id, conv_id, position, kind, leased_until, lease_task_id '
        'FROM message_queue WHERE id=?', (queue_id,)).fetchone()


def _stub_dispatch_path(monkeypatch, *, spawn_error=None):
    """Stub the post-dequeue pipeline so dispatch runs without LLM/threads.

    Returns a recorder dict: {'created': [task_id...], 'spawned': [task_id...]}.
    """
    rec = {'created': [], 'spawned': []}
    monkeypatch.setattr(mq, '_append_user_msg_with_cas',
                        lambda db, conv_id, user_msg: True)
    monkeypatch.setattr(
        'lib.tasks_pkg.conv_message_builder.build_api_messages_from_db',
        lambda conv_id, config: [{'role': 'user', 'content': 'x'}])

    def _create_task(conv_id, api_messages, config):
        tid = f'task-{len(rec["created"])}-{time.time_ns()}'
        rec['created'].append(tid)
        return {'id': tid, 'convId': conv_id, 'status': 'running',
                'config': config, 'created_at': time.time()}

    def _spawn_task(task):
        if spawn_error is not None:
            raise spawn_error
        rec['spawned'].append(task['id'])

    monkeypatch.setattr('lib.tasks_pkg.create_task', _create_task)
    monkeypatch.setattr('lib.tasks_pkg.spawn_task', _spawn_task)
    return rec


def _db():
    mq._maybe_ensure_table()
    return mq.get_thread_db(mq.DOMAIN_CHAT)


@pytest.fixture(autouse=True)
def _isolate_global_reaper_rows():
    """The reaper is intentionally global, so each test owns the whole queue.

    Other queue modules leave durable rows behind to assert their read-side
    behaviour.  Without this boundary, a later reaper test records those rows
    as additional (perfectly valid) redispatches and becomes order-dependent.
    The pytest database is throwaway and background workers are disabled.
    """
    from lib.database import db_execute_with_retry

    db_execute_with_retry(_db(), 'DELETE FROM message_queue')
    yield
    db_execute_with_retry(_db(), 'DELETE FROM message_queue')


# ── 1. spawn failure keeps the row + lease released → re-dispatch works ──

def test_spawn_failure_keeps_row_and_redispatches(monkeypatch):
    """Owner req #1: a spawn failure must NOT lose the queued message."""
    conv_id = _cid()
    qid = mq.enqueue_message(conv_id, {'text': 'hello', 'timestamp': 1000},
                             {'model': 'm'})['queueId']

    _stub_dispatch_path(monkeypatch, spawn_error=RuntimeError('boom'))
    tid = mq.dispatch_next_queued(conv_id)
    assert tid is None
    row = _row(_db(), qid)
    assert row is not None, 'queue row must survive a spawn failure'
    # Lease released → the row is immediately re-dispatchable (no 120s wait).
    assert row['leased_until'] in (None, 0), row

    # Owner req #1: the NEXT maintenance tick must retry automatically — the
    # reaper drains the released row because the errored task is not live.
    rec = _stub_dispatch_path(monkeypatch)
    spawned = mq.reap_expired_queue_leases()
    assert len(rec['spawned']) == 1 and spawned == rec['spawned'], \
        'reaper must auto-redispatch the released row on the next tick'
    assert _row(_db(), qid) is None, 'row must be deleted after successful spawn'


# ── 2. crash after lease → reaper reclaims + re-dispatches ──

def test_expired_lease_dead_task_reclaimed_and_redispatched(monkeypatch):
    """Owner req #2: process died between lease and spawn → automatic retry."""
    conv_id = _cid()
    qid = mq.enqueue_message(conv_id, {'text': 'orphan', 'timestamp': 1000},
                             {'model': 'm'})['queueId']
    # Simulate the crash artifact: lease taken, expired, task never created.
    past = int(time.time() * 1000) - 1000
    db = _db()
    db.execute('UPDATE message_queue SET leased_until=?, lease_task_id=? WHERE id=?',
               (past, 'dead-task-id', qid))
    db.commit()

    rec = _stub_dispatch_path(monkeypatch)
    spawned = mq.reap_expired_queue_leases()
    assert len(rec['spawned']) == 1, 'reaper must re-dispatch the orphaned row'
    assert spawned == rec['spawned']
    assert _row(db, qid) is None, 'reclaimed row must be deleted after its spawn'


# ── 3. expired lease but task STILL RUNNING → extend, never re-dispatch ──

def test_expired_lease_with_live_task_extended_not_redispatched(monkeypatch):
    """Owner req #3: liveness is the registry, not the clock."""
    conv_id = _cid()
    qid = mq.enqueue_message(conv_id, {'text': 'dup-guard', 'timestamp': 1000},
                             {'model': 'm'})['queueId']
    past = int(time.time() * 1000) - 1000
    live_tid = 'live-task-xyz'
    db = _db()
    db.execute('UPDATE message_queue SET leased_until=?, lease_task_id=? WHERE id=?',
               (past, live_tid, qid))
    db.commit()

    # Register a genuinely-running task under the lease's task id.
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks[live_tid] = {'id': live_tid, 'convId': conv_id,
                           'status': 'running', 'aborted': False,
                           'created_at': time.time()}
    try:
        rec = _stub_dispatch_path(monkeypatch)
        spawned = mq.reap_expired_queue_leases()
        assert spawned == [] and rec['spawned'] == [], \
            'a live-task lease must never be re-dispatched (double-dispatch)'
        row = _row(db, qid)
        assert row is not None, 'row must survive while its task runs'
        assert row['leased_until'] > int(time.time() * 1000), \
            'live-task lease must be EXTENDED, not cleared'
    finally:
        with tasks_lock:
            tasks.pop(live_tid, None)


# ── 3b. lost finalize: terminal task + leftover leased row → finish delete ──

def test_terminal_task_lease_finished_not_redispatched(monkeypatch):
    """The spawn succeeded but the deferred delete was lost (DB hiccup): the
    reaper must DELETE the leftover row, never re-answer the message."""
    conv_id = _cid()
    qid = mq.enqueue_message(conv_id, {'text': 'already answered', 'timestamp': 1000},
                             {'model': 'm'})['queueId']
    done_tid = 'done-task-xyz'
    db = _db()
    db.execute('UPDATE message_queue SET leased_until=?, lease_task_id=? WHERE id=?',
               (int(time.time() * 1000) + 60_000, done_tid, qid))
    db.commit()

    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks[done_tid] = {'id': done_tid, 'convId': conv_id,
                           'status': 'done', 'aborted': False,
                           'created_at': time.time()}
    try:
        rec = _stub_dispatch_path(monkeypatch)
        spawned = mq.reap_expired_queue_leases()
        assert spawned == [] and rec['spawned'] == [], \
            'a terminal task\'s row must never be re-dispatched'
        assert _row(db, qid) is None, 'lost finalize must be finished (row deleted)'
    finally:
        with tasks_lock:
            tasks.pop(done_tid, None)


# ── 4. fresh lease → dequeue skips (no mid-flight double-dispatch) ──

def test_fresh_lease_skipped_by_dequeue(monkeypatch):
    """Owner req #4: an in-flight dispatch's row is invisible to other drains."""
    conv_id = _cid()
    qid = mq.enqueue_message(conv_id, {'text': 'in-flight', 'timestamp': 1000},
                             {'model': 'm'})['queueId']
    future = int(time.time() * 1000) + 10 * 60 * 1000
    db = _db()
    db.execute('UPDATE message_queue SET leased_until=? WHERE id=?', (future, qid))
    db.commit()

    assert mq.dequeue_next(conv_id) is None, \
        'a freshly-leased row must not be re-dequeued'
    # Expiry makes it visible again (self-heal without waiting for the reaper).
    db.execute('UPDATE message_queue SET leased_until=? WHERE id=?',
               (int(time.time() * 1000) - 1, qid))
    db.commit()
    item = mq.dequeue_next(conv_id)
    assert item is not None and item['queueId'] == qid
    # …and the re-lease stamps a fresh expiry + clears any stale task id.
    row = _row(db, qid)
    assert row['leased_until'] > int(time.time() * 1000)
    assert (row['lease_task_id'] or '') == ''


# ── 5. autopilot sentinel is lease-immune ──

def test_autopilot_sentinel_immune_to_lease_reaper(monkeypatch):
    """Owner req #5: the sentinel is never dispatched, never reclaimed."""
    conv_id = _cid()
    qid = mq.enqueue_message(conv_id, {'text': '', 'timestamp': 1000},
                             {'model': 'm'}, kind=mq.KIND_AUTOPILOT)['queueId']
    # Force a stale lease onto the sentinel — the reaper must still ignore it.
    past = int(time.time() * 1000) - 1000
    db = _db()
    db.execute('UPDATE message_queue SET leased_until=? WHERE id=?', (past, qid))
    db.commit()

    rec = _stub_dispatch_path(monkeypatch)
    spawned = mq.reap_expired_queue_leases()
    assert spawned == [] and rec['spawned'] == []
    assert _row(db, qid) is not None, 'sentinel row must survive the reaper'


# ── 6. three queued messages dispatch in order; delete renumbers positions ──

def test_three_messages_dispatch_in_position_order(monkeypatch):
    """Owner req #6: deferred delete must not perturb (priority, position)."""
    conv_id = _cid()
    for i, text in enumerate(['first', 'second', 'third']):
        mq.enqueue_message(conv_id, {'text': text, 'timestamp': 1000 + i},
                           {'model': 'm'})

    rec = _stub_dispatch_path(monkeypatch)
    seen_texts = []
    orig_append = mq._append_user_msg_with_cas
    monkeypatch.setattr(mq, '_append_user_msg_with_cas',
                        lambda db, cid, msg: (seen_texts.append(msg['content']), True)[1])

    tids = [mq.dispatch_next_queued(conv_id) for _ in range(3)]
    assert all(tids), 'all three messages must dispatch'
    assert seen_texts == ['first', 'second', 'third'], seen_texts
    assert len(rec['spawned']) == 3
    assert mq.get_queue(conv_id) == [], 'queue must be empty after three spawns'
    _ = orig_append


def test_delete_renumbers_positions_like_before(monkeypatch):
    """The deferred delete keeps the same renumber semantics the eager one had."""
    conv_id = _cid()
    mq.enqueue_message(conv_id, {'text': 'a', 'timestamp': 1}, {'model': 'm'})
    mq.enqueue_message(conv_id, {'text': 'b', 'timestamp': 2}, {'model': 'm'})
    _stub_dispatch_path(monkeypatch)
    assert mq.dispatch_next_queued(conv_id) is not None
    remaining = mq.get_queue(conv_id)
    assert len(remaining) == 1
    assert remaining[0]['text'] == 'b'
    assert remaining[0]['position'] == 1, 'renumber must close the gap'


# ── 8. per-tick dispatch cap: 20 stranded convs → K per tick, oldest first ──

def test_stranded_drain_capped_per_tick_oldest_first(monkeypatch):
    """A mass-stranding (restart) must not slam the LLM rate limit: oldest-
    enqueued first, K dispatches per tick, the rest defer to the next tick."""
    db = _db()
    # Determinism: created_at is the CLIENT payload timestamp, so leftover
    # rows from other tests in the shared session DB can carry arbitrary tiny
    # values (1, 2, 1000…) and would out-age our convs in the oldest-first
    # scan. Wipe the table — every earlier test is finished with its rows.
    db.execute('DELETE FROM message_queue')
    # Age anchor ~4 months back so the per-conv ordering comes only from us.
    base = int(time.time() * 1000) - 10**10
    convs = []
    for i in range(20):
        cid = _cid()
        convs.append(cid)
        mq.enqueue_message(cid, {'text': f'm{i}', 'timestamp': 1000 + i},
                           {'model': 'm'})
        # Deterministic age: convs[0] oldest … convs[-1] newest.
        db.execute('UPDATE message_queue SET created_at=? WHERE conv_id=?',
                   (base + i * 1000, cid))
    db.commit()

    dispatched = []
    monkeypatch.setattr(mq, '_append_user_msg_with_cas', lambda db, c, m: True)
    monkeypatch.setattr(
        'lib.tasks_pkg.conv_message_builder.build_api_messages_from_db',
        lambda c, cfg: [{'role': 'user', 'content': 'x'}])

    def _create_task(conv_id, api_messages, config):
        dispatched.append(conv_id)
        return {'id': f't-{conv_id}', 'convId': conv_id, 'status': 'running',
                'config': config, 'created_at': time.time()}

    monkeypatch.setattr('lib.tasks_pkg.create_task', _create_task)
    monkeypatch.setattr('lib.tasks_pkg.spawn_task', lambda task: None)

    spawned = mq.reap_expired_queue_leases()
    assert len(spawned) == 4, f'default cap is 4 per tick, got {len(spawned)}'
    assert dispatched == convs[:4], 'oldest-enqueued convs must drain first'

    # Next tick drains the next batch — and the env knob is honored.
    monkeypatch.setenv('TOFU_QUEUE_REAPER_MAX_DISPATCH_PER_TICK', '2')
    dispatched.clear()
    spawned = mq.reap_expired_queue_leases()
    assert len(spawned) == 2
    assert dispatched == convs[4:6]


# ── 7. intentional deletes keep their semantics ──

def test_remove_message_and_clear_marker_still_delete():
    """The two INTENTIONAL delete sites are outside the loss window — untouched."""
    conv_id = _cid()
    qid = mq.enqueue_message(conv_id, {'text': 'cancel me', 'timestamp': 1},
                             {'model': 'm'})['queueId']
    assert mq.remove_from_queue(conv_id, qid) is True
    assert _row(_db(), qid) is None

    marker = mq.enqueue_message(conv_id, {'text': '', 'timestamp': 1},
                                {'model': 'm'}, kind=mq.KIND_AUTOPILOT)['queueId']
    assert mq.clear_autopilot_marker(conv_id) is True
    assert _row(_db(), marker) is None
