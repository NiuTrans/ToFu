#!/usr/bin/env python3
"""tests/test_settle_time_reconcile_dropped_task.py — RED reproduction for #2
of the empty-"Agent" air-bubble root fix: SETTLE-TIME reconcile keyed by taskId.

WHY (the source hole, verified in lib/tasks_pkg/manager.py)
-----------------------------------------------------------
When a task DROPS before its first token (stream dies / worker crashes / abort
before any delta), the frontend has already minted an empty assistant
placeholder ({role:'assistant', content:''}) as the stream target. The terminal
`_sync_result_to_conversation` then hits its SKIP-return at the top:

    if not content and not thinking and not error:
        return                                              # manager.py:~1023

— it returns BEFORE the DB is even opened, so the orphaned placeholder is never
cleaned. The backend GET-path reconcile (routes/conversations.py) CANNOT reach
it either, because GATE 1 skips reconcile while a task is live (a live stream
target is byte-identical to a ghost). Net: the ghost persists until a future
WARM reopen (#1) heals it — a self-healing patch, not a root fix. The
placeholder is still minted, still persisted, still synced.

THE FIX THIS TEST DRIVES (lib/tasks_pkg/manager.py — trigger only; calls the
existing pure lib/conversations/reconcile.reconcile_conversation_messages)
-------------------------------------------------------------------------
At the drop-before-first-token skip path, run a settle-time reconcile keyed by
taskId: only when THIS task is still the conv's latest (`_latest_task_for_conv
== task['id']` — no newer task owns a live placeholder), read the conv, run
reconcile_conversation_messages (whose classify_ghost_tail returns 'delete' for
a bare empty trailing assistant), persist the shorter list under a CAS guard,
clear activeTaskId, and notify_conv_changed. The ghost is swept at the SOURCE.

Invariants (encoded as controls — the whole ballgame, mirrors the GET-path
live-task gate):
  • A NEWER task superseded this one → this task must NOT touch the conv (the
    newer task owns any live placeholder). Keyed-by-taskId gate.
  • A real settled tail is NEVER deleted.

CHECKS (RED until the fix lands)
  A. dropped task, latest-for-conv, orphan empty tail  → tail SWEPT at task-end
  B. CONTROL: a NEWER task is latest for the conv       → placeholder SURVIVES (not this task's)
  C. CONTROL: settled real tail                         → never deleted
"""

import json as _json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

pytestmark = pytest.mark.unit


def _seed_conv(db, conv_id, messages, settings, *, updated_at=None):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'settle-recon-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': updated_at or now_ms,
        'settings': _json.dumps(settings, ensure_ascii=False),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _read(db, conv_id):
    row = db.execute('SELECT messages, settings FROM conversations '
                     'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    settings = _json.loads(row[1]) if row[1] and isinstance(row[1], str) else (row[1] or {})
    return msgs, settings


def _cleanup(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _uid(stem):
    """Unique per-run id so fixtures never collide across suites / re-runs in
    the same pytest process."""
    import uuid
    return f'{stem}-{uuid.uuid4().hex[:8]}'


def _dropped_task(conv_id, task_id):
    """A task that produced NO content/thinking/error — the drop-before-first-
    token shape that hits the skip-return in _sync_result_to_conversation."""
    return {
        'id': task_id, 'convId': conv_id,
        'content': '', 'thinking': '', 'error': None,
        'status': 'error', 'aborted': False,
    }


def _clear_latest(conv_id):
    from lib.tasks_pkg import manager as _mgr
    with _mgr._conv_latest_task_lock:
        _mgr._conv_latest_task.pop(conv_id, None)
    try:
        from lib.runtime_state_store import get_store
        get_store().set_value('latest', conv_id, None, 1)
    except Exception:
        pass


def test_dropped_task_sweeps_orphan_placeholder_at_settle():
    """★ A: dropped task that is still latest-for-conv → its orphan empty
    trailing placeholder is swept at task-end (settle-time reconcile)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager as _mgr
    conv_id = _uid('cv-settle-drop')
    task_id = _uid('tk-settle-drop')
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'settled', 'finishReason': 'stop', 'timestamp': 2},
        {'role': 'user', 'content': 'q2', 'timestamp': 3},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 4},   # orphan placeholder for the dropped task
    ], settings={'activeTaskId': task_id})
    _mgr._record_latest_task(conv_id, task_id)   # THIS task is the conv's latest
    try:
        _mgr._sync_result_to_conversation(_dropped_task(conv_id, task_id),
                                          {'model': 'm'})
        msgs, settings = _read(db, conv_id)
        roles = [m['role'] for m in msgs]
        assert roles == ['user', 'assistant', 'user'], (
            'EXPECTED-RED: dropped task did NOT sweep its orphan placeholder at '
            f'settle-time — roles={roles} (still 4 msgs). This is the failing '
            'test that drives #2.')
        assert not settings.get('activeTaskId'), 'activeTaskId not cleared on settle-sweep'
    finally:
        _clear_latest(conv_id)
        _cleanup(db, conv_id)


def test_superseded_task_does_not_touch_conv():
    """★ B CONTROL: a NEWER task is latest for the conv → this (superseded)
    task must NOT delete the placeholder (the newer task owns any live one)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager as _mgr
    conv_id = _uid('cv-settle-superseded')
    old_task = _uid('tk-settle-old')
    new_task = _uid('tk-settle-new')
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},   # placeholder OWNED by the newer task's live stream
    ], settings={'activeTaskId': new_task})
    _mgr._record_latest_task(conv_id, new_task)   # a NEWER task superseded old_task
    try:
        _mgr._sync_result_to_conversation(_dropped_task(conv_id, old_task),
                                          {'model': 'm'})
        msgs, _settings = _read(db, conv_id)
        assert len(msgs) == 2 and msgs[-1]['role'] == 'assistant', (
            f'superseded task DELETED the newer task\'s live placeholder '
            f'(msgs={len(msgs)}) — data-corruption regression')
    finally:
        _clear_latest(conv_id)
        _cleanup(db, conv_id)


def test_settled_tail_never_deleted_on_settle():
    """★ C CONTROL: a real settled trailing assistant is never deleted, even if
    this dropped-task sync fires (defense: reconcile only sweeps ghosts)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager as _mgr
    conv_id = _uid('cv-settle-settled')
    task_id = _uid('tk-settle-settled')
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'a complete answer', 'finishReason': 'stop',
         'timestamp': 2},
    ], settings={'activeTaskId': task_id})
    _mgr._record_latest_task(conv_id, task_id)
    try:
        _mgr._sync_result_to_conversation(_dropped_task(conv_id, task_id),
                                          {'model': 'm'})
        msgs, _settings = _read(db, conv_id)
        assert len(msgs) == 2 and msgs[-1]['content'] == 'a complete answer', (
            f'settled tail was disturbed (msgs={len(msgs)})')
    finally:
        _clear_latest(conv_id)
        _cleanup(db, conv_id)


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_settle_time_reconcile_dropped_task.__main__')
    ok = True
    for fn in (test_dropped_task_sweeps_orphan_placeholder_at_settle,
               test_superseded_task_does_not_touch_conv,
               test_settled_tail_never_deleted_on_settle):
        try:
            fn()
            print('  PASS', fn.__name__)
        except AssertionError as e:
            ok = False
            print('  FAIL', fn.__name__, '::', e)
    sys.exit(0 if ok else 1)
