"""tests/test_event_log_batch_lane.py — write-behind batch lane for task_events.

docs/STORAGE_REDESIGN.md §4: FUSE charges per IO operation, not per byte, so
the per-delta committed row (the project's #1 write source) becomes a
write-behind lane — every event still lands as its OWN row at its real
cursor (exact-cursor cold replay unchanged), but commits happen in bursts.

Pinned behaviours:
  1. FIDELITY — N events appended through the lane come back in exact
     (task_id, event_id) order with byte-identical payloads.
  2. ★ THE ACCEPTANCE METRIC — 120 events cost ≤ a handful of commits
     (before: 120 commits). The ≥10x per-turn commit reduction the owner
     pinned is measured HERE at the lane level.
  3. DURABLE-BEFORE-VISIBLE for terminal frames: a 'done' append returns
     only after its row is durable — has_terminal_event is True IMMEDIATELY,
     no manual flush.
  4. KILL SWITCH — TOFU_EVENT_BATCH=0 (monkeypatched flag) writes
     synchronously: the row is visible with no drain.
  5. FULL-QUEUE DEGRADE — a saturated queue falls back to a synchronous
     write (no silent loss of the row).
  6. STATIC PIN — append_persistent_event must actually route through the
     lane (guards a well-meaning refactor from silently reverting to the
     per-row-commit path).

NEUTER: make ``_flush_batch`` skip ``db.commit()`` → tests 1/2/3 all fail
(rows never visible cross-connection), proving the batch commit is
load-bearing.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import uuid

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tid(prefix):
    return f'{prefix}-{uuid.uuid4().hex[:8]}'


def _rows(tid):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    return db.execute(
        'SELECT event_id, type, payload FROM task_events '
        'WHERE task_id=? ORDER BY event_id', (tid,)).fetchall()


def _cleanup(*tids):
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        for t in tids:
            db.execute('DELETE FROM task_events WHERE task_id=?', (t,))
        db.commit()
    except Exception:
        pass


def test_batch_preserves_exact_cursor_order_and_payload():
    """Fidelity: every event lands as its own row at its real cursor — the
    batch lane changes COMMIT cadence, never the stored shape."""
    import lib.tasks_pkg.event_log as ev
    tid = _tid('lane-order')
    try:
        payloads = []
        for i in range(40):
            event = {'type': 'delta', 'content': f'tok-{i}-☃'}
            payloads.append(event)
            ev.append_persistent_event(tid, i, event)
        ev.flush_pending(tid)

        rows = _rows(tid)
        assert len(rows) == 40, f'expected 40 rows, got {len(rows)}'
        for i, r in enumerate(rows):
            assert r['event_id'] == i, f'cursor hole at {i}'
            p = r['payload']
            p = p if isinstance(p, dict) else json.loads(p)
            assert p['content'] == payloads[i]['content']
    finally:
        _cleanup(tid)


def test_batch_collapses_commits():
    """★ ACCEPTANCE (owner-pinned ≥10x): the lane turns N appends into
    O(N/500 + windows) commits. 120 appends must cost ≤ 12 commits — the
    legacy path paid exactly 120."""
    import lib.tasks_pkg.event_log as ev
    if not ev._EVENT_BATCH_ENABLED:
        pytest.skip('batch lane disabled in this environment')
    tid = _tid('lane-commits')
    try:
        before = ev.get_batch_stats()
        for i in range(120):
            ev.append_persistent_event(tid, i, {'type': 'delta', 'content': f'c{i}'})
        ev.flush_pending(tid)
        after = ev.get_batch_stats()

        rows = after['flushed_rows'] - before['flushed_rows']
        commits = after['commits'] - before['commits']
        assert rows == 120, f'lane lost rows: flushed_rows delta={rows}'
        assert commits <= 12, (
            f'120 events cost {commits} commits — the batch lane is not '
            f'collapsing commits (legacy was 1 commit/event; acceptance is '
            f'≥10x reduction)')
        assert len(_rows(tid)) == 120
    finally:
        _cleanup(tid)


def test_terminal_event_is_durable_on_return():
    """Durable-before-visible: 'done' returns only after the row is durable —
    a reconnect anchoring to it must never find a hole."""
    import lib.tasks_pkg.event_log as ev
    tid = _tid('lane-term')
    try:
        ev.append_persistent_event(tid, 0, {'type': 'delta', 'content': 'x'})
        ev.append_persistent_event(tid, 1, {'type': 'done'})
        # NO manual flush — the terminal ack inside append must have landed it.
        assert ev.has_terminal_event(tid), (
            'terminal event not durable when append returned — a reconnect '
            'could anchor to a frame the DB lost')
        assert [r['event_id'] for r in _rows(tid)] == [0, 1]
    finally:
        _cleanup(tid)


def test_kill_switch_writes_synchronously(monkeypatch):
    """TOFU_EVENT_BATCH=0 semantics (flag monkeypatched): the row is visible
    with no drain at all — the legacy per-row-commit path still works."""
    import lib.tasks_pkg.event_log as ev
    monkeypatch.setattr(ev, '_EVENT_BATCH_ENABLED', False)
    tid = _tid('lane-sync')
    try:
        ev.append_persistent_event(tid, 0, {'type': 'delta', 'content': 's'})
        rows = _rows(tid)  # no flush_pending — sync path must be immediate
        assert len(rows) == 1 and rows[0]['event_id'] == 0
        assert ev.get_batch_stats()['sync_writes'] >= 1
    finally:
        _cleanup(tid)


def test_full_queue_falls_back_to_sync_write(monkeypatch):
    """A saturated queue must not lose the row: put_nowait raises Full →
    the row is written synchronously instead."""
    import lib.tasks_pkg.event_log as ev

    class _FullQ:
        def put_nowait(self, item):
            raise queue.Full

        def get(self, timeout=None):
            raise queue.Empty

        def empty(self):
            return True

    monkeypatch.setattr(ev, '_EVENT_Q', _FullQ())
    tid = _tid('lane-full')
    try:
        ev.append_persistent_event(tid, 0, {'type': 'delta', 'content': 'q'})
        rows = _rows(tid)
        assert len(rows) == 1, 'queue-full fallback lost the row'
        assert ev.get_batch_stats()['queue_full'] >= 1, 'Full must be counted'
    finally:
        _cleanup(tid)


def test_append_routes_through_the_lane_static_pin():
    """Drift guard: append_persistent_event must dispatch through _EVENT_Q —
    a refactor that silently restores per-row commits would re-open the #1
    write source this lane closed."""
    import inspect
    import lib.tasks_pkg.event_log as ev
    src = inspect.getsource(ev.append_persistent_event)
    assert '_EVENT_Q.put_nowait' in src, (
        'append_persistent_event no longer enqueues — the batch lane was '
        'bypassed; per-delta commits are back')
    assert '_wait_ticket' in src, (
        'terminal-event durability ack was removed from append — '
        'durable-before-visible no longer holds for done/error/aborted')


def test_queue_full_sync_success_resolves_tail_ticket(monkeypatch):
    """The final ticket in a process can resolve via the out-of-order sync lane.

    This is the exact exit-warning regression: the row committed, but the old
    scalar watermark never advanced because only the writer called
    _mark_flushed.
    """
    import lib.tasks_pkg.event_log as ev

    class _FullQ:
        def put_nowait(self, item):
            raise queue.Full

        def get(self, timeout=None):
            raise queue.Empty

    monkeypatch.setattr(ev, '_EVENT_Q', _FullQ())
    tid = _tid('lane-full-tail')
    try:
        ev.append_persistent_event(tid, 0, {'type': 'done'})
        assert ev._wait_through(ev._current_ticket(), 0.2) is True
        assert ev.has_terminal_event(tid)
    finally:
        _cleanup(tid)


def test_terminal_sync_failure_is_not_acknowledged(monkeypatch):
    """A permanent failure must raise, retain no fake durable ACK, and not push."""
    import lib.tasks_pkg.event_log as ev

    class _FullQ:
        def put_nowait(self, item):
            raise queue.Full

        def get(self, timeout=None):
            raise queue.Empty

    monkeypatch.setattr(ev, '_EVENT_Q', _FullQ())
    monkeypatch.setattr(ev, '_write_row_one_shot', lambda row: False)
    tid = _tid('lane-term-fail')
    with pytest.raises(ev.EventDurabilityError):
        ev.append_persistent_event(tid, 0, {'type': 'done'})


def test_runtime_withholds_terminal_push_when_persistence_raises():
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.agent_core import push as push_mod

    runtime = TaskRuntime(kind='durability-test', push_channel='chat')
    task = runtime.create(task_id=_tid('runtime-term-fail'))
    pushed = []
    original = push_mod.push_event
    push_mod.push_event = lambda channel, task_id, event: pushed.append(event)
    try:
        runtime.append_event(
            task['id'], {'type': 'done'},
            before_push=lambda _seq: (_ for _ in ()).throw(
                RuntimeError('db down')))
    finally:
        push_mod.push_event = original
    assert pushed == [], 'undurable terminal frame was exposed to the client'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
