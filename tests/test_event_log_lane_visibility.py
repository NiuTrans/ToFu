"""tests/test_event_log_lane_visibility.py — lane-aware read atomicity pin.

Born from the batch-lane bring-up (docs/STORAGE_REDESIGN.md §4): a reader
that takes the pending-shadow snapshot AFTER its DB query can straddle the
writer's commit→shadow-pop and see a row in NEITHER store (measured: the
cold fold lagging one burst behind the client buffer). The fix — snapshot
the shadow FIRST — is pinned here through the real manager.append_event +
instrumented push path: at EVERY push instant the row is visible via
read_events (committed OR shadowed, never neither).
"""

import uuid

import pytest

pytestmark = pytest.mark.unit


def test_lane_visibility_is_atomic_across_commit_pop():
    from lib.tasks_pkg import manager as _mgr
    import lib.tasks_pkg.event_log as ev
    from lib.agent_core import push as _push
    from lib.database import DOMAIN_CHAT, get_thread_db

    tid = f'zz-{uuid.uuid4().hex[:8]}'
    _mgr._chat_runtime.create(task_id=tid)
    misses = []
    orig_push = _push.push_event

    def _spy(channel, task_id, event):
        if task_id == tid and event.get('type') == 'delta':
            s = event.get('seq')
            with ev._TICKET_LOCK:
                shadow = sorted(r['event_id'] for r in ev._PENDING_SHADOW.values()
                                if r['task_id'] == tid)
            main_db = get_thread_db(DOMAIN_CHAT)
            raw = sorted(r[0] for r in main_db._conn.execute(
                'SELECT event_id FROM task_events WHERE task_id=?', (tid,)).fetchall())
            via = [e['event_id'] for e in ev.read_events(tid)]
            if s not in via:
                misses.append({'seq': s, 'raw': raw[-3:] if raw else [],
                               'shadow': shadow, 'via_n': len(via),
                               'in_txn': main_db._conn.in_transaction})
        return orig_push(channel, task_id, event)

    _push.push_event = _spy
    try:
        task = _mgr._chat_runtime.get(tid)
        for i in range(25):
            _mgr.append_event(task, {'type': 'delta', 'content': f'w{i} '})
    finally:
        _push.push_event = orig_push
        from lib.tasks_pkg import manager as _m2
        with _m2._chat_runtime._lock:
            _m2._chat_runtime._tasks.pop(tid, None)
        from lib.database import db_execute_with_retry
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM task_events WHERE task_id=?', (tid,))
        db.commit()

    print('\nMISSES:', misses)
    print('stats:', ev.get_batch_stats())
    assert not misses, f'{len(misses)} visibility misses: {misses[:3]}'
