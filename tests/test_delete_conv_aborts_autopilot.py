#!/usr/bin/env python3
"""Deleting a conversation must STOP its live work before wiping the rows.

A conversation under autopilot is a self-spawning loop: each finished turn's
end-of-turn hook (``maybe_run_autopilot`` → ``_start_followup_task``) spawns a
follow-up task. ``routes/conversations.py:_delete_conv_blocking`` historically
did only the three ``DELETE``s + ``_notify_conv_changed`` — it never aborted the
loop. That left a follow-up task running against a conv that no longer exists
(burning tokens) whose late terminal write re-inserted an orphan
``task_results`` row (the "Conversation not found in DB — cannot sync result
back" signature in app.log).

Prong 1 (this file) asserts, against the REAL ``_delete_conv_blocking`` body +
a real seeded conv:
  * a running task for the conv is force-aborted by the delete;
  * the conv's autopilot armed-marker is disarmed by the delete;
  * an abort sweep that THROWS does not prevent the delete from completing
    (the abort/disarm are best-effort, never block the delete).

NEUTER: monkeypatch ``abort_running_tasks_for_conv`` in ``lib.tasks_pkg`` to a
no-op so the delete path's abort call does nothing → the seeded running task is
LEFT un-aborted → the positive assertion fails. Proves the abort call is
load-bearing (not incidentally satisfied by some other sweep).

Standalone runner (real DB, mirrors tests/test_delete_message_by_msgid.py);
also importable as pytest test functions.
"""

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed_conv(db, conv_id, messages=None):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    if messages is None:
        messages = [{'role': 'user', 'content': 'hi'},
                    {'role': 'assistant', 'content': 'yo'}]
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'del-abort-test',
        'messages': json_dumps_pg(messages),
        'msg_count': len(messages), 'created_at': now_ms, 'updated_at': now_ms,
        'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _seed_running_task(conv_id):
    """Register a fake running task in the in-memory registry for ``conv_id``."""
    import lib.tasks_pkg.manager as mgr
    tid = 'tk-' + uuid.uuid4().hex[:12]
    with mgr.tasks_lock:
        mgr.tasks[tid] = {
            'id': tid, 'convId': conv_id, 'status': 'running',
            'content': 'partial', 'created_at': time.time(),
        }
    return tid


def _task_aborted(tid):
    import lib.tasks_pkg.manager as mgr
    with mgr.tasks_lock:
        t = mgr.tasks.get(tid)
        return bool(t and t.get('aborted'))


def _cleanup_task(tid):
    import lib.tasks_pkg.manager as mgr
    with mgr.tasks_lock:
        mgr.tasks.pop(tid, None)


def _cleanup_conv(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (cid,))
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE conv_id=?', (cid,))
        db_execute_with_retry(db, 'DELETE FROM message_queue WHERE conv_id=?', (cid,))
    db.commit()


def test_delete_aborts_running_task():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from routes.conversations import _delete_conv_blocking
    conv_id = 'cv-del-abort'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id)
    tid = _seed_running_task(conv_id)
    try:
        _delete_conv_blocking(db, conv_id)
        assert _task_aborted(tid), 'running task was NOT aborted by delete'
        # And the conv row is actually gone.
        row = db.execute('SELECT 1 FROM conversations WHERE id=? AND user_id=1',
                         (conv_id,)).fetchone()
        assert row is None, 'conv row survived the delete'
    finally:
        _cleanup_task(tid)
        _cleanup_conv(db, conv_id)
    _ok('delete aborts the conv\'s running task and removes the conv row')


def test_delete_cascades_message_mirror_in_same_transaction():
    """A deleted conversation must not leave row-store history behind."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database.messages_rows import backfill_conv
    from routes.conversations import _delete_conv_blocking

    conv_id = 'cv-del-message-mirror'
    db = get_thread_db(DOMAIN_CHAT)
    mirror_messages = [
        {'role': 'user', 'content': 'mirror me'},
        {'role': 'assistant', 'content': 'then cascade me'},
    ]
    _seed_conv(db, conv_id, mirror_messages)
    backfill_conv(db, conv_id, mirror_messages)
    try:
        before = db.execute(
            'SELECT COUNT(*) AS n FROM conversation_messages WHERE conv_id=?',
            (conv_id,)).fetchone()['n']
        assert before == 2, f'precondition failed: expected 2 mirror rows, got {before}'

        _delete_conv_blocking(db, conv_id)

        parent = db.execute(
            'SELECT 1 FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)).fetchone()
        mirror = db.execute(
            'SELECT COUNT(*) AS n FROM conversation_messages WHERE conv_id=?',
            (conv_id,)).fetchone()['n']
        assert parent is None, 'conversation row survived delete'
        assert mirror == 0, 'conversation_messages rows survived delete'
    finally:
        _cleanup_conv(db, conv_id)
    _ok('delete cascades the message-row mirror')


def test_delete_disarms_autopilot_marker():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import arm_autopilot_marker, has_autopilot_marker
    from routes.conversations import _delete_conv_blocking
    conv_id = 'cv-del-disarm'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id)
    arm_autopilot_marker(conv_id, {'model': 'x'})
    assert has_autopilot_marker(conv_id), 'precondition: marker should be armed'
    try:
        _delete_conv_blocking(db, conv_id)
        assert not has_autopilot_marker(conv_id), \
            'autopilot marker NOT disarmed by delete'
    finally:
        _cleanup_conv(db, conv_id)
    _ok('delete disarms the conv\'s autopilot armed-marker')


def test_delete_survives_abort_throwing():
    """The abort/disarm are best-effort — an abort sweep that raises must not
    prevent the delete from completing (else the user can never remove the
    conversation)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg as tp
    from routes.conversations import _delete_conv_blocking
    conv_id = 'cv-del-abort-throws'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id)
    _orig = tp.abort_running_tasks_for_conv
    try:
        def _boom(cid, exclude_task_id=None):
            raise RuntimeError('simulated abort-sweep failure')
        tp.abort_running_tasks_for_conv = _boom
        _delete_conv_blocking(db, conv_id)
        row = db.execute('SELECT 1 FROM conversations WHERE id=? AND user_id=1',
                         (conv_id,)).fetchone()
        assert row is None, 'delete did not complete when abort threw'
    finally:
        tp.abort_running_tasks_for_conv = _orig
        _cleanup_conv(db, conv_id)
    _ok('delete completes even when the abort sweep raises (best-effort)')


_POSITIVE = [
    test_delete_aborts_running_task,
    test_delete_disarms_autopilot_marker,
    test_delete_survives_abort_throwing,
]


def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def _neuter_and_subrun():
    """NC: monkeypatch abort_running_tasks_for_conv (the name the delete path
    imports) to a no-op. The seeded running task is then LEFT un-aborted →
    proves the delete's abort call is what stops it."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg as tp
    from routes.conversations import _delete_conv_blocking
    conv_id = 'cv-del-abort-nc'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id)
    tid = _seed_running_task(conv_id)
    _orig = tp.abort_running_tasks_for_conv
    try:
        tp.abort_running_tasks_for_conv = lambda cid, exclude_task_id=None: 0
        _delete_conv_blocking(db, conv_id)
        still_running = not _task_aborted(tid)
        return still_running, f'aborted={_task_aborted(tid)} (neutered → should stay un-aborted)'
    finally:
        tp.abort_running_tasks_for_conv = _orig
        _cleanup_task(tid)
        _cleanup_conv(db, conv_id)


def main():
    print()
    print(_color('═══ delete_conv aborts autopilot loop + disarms marker + neuter ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_delete_conv_aborts_autopilot.__main__')

    print(_color('Baseline (shipped delete path):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix the delete-path abort/disarm first')

    print()
    print(_color('NC — neuter abort_running_tasks_for_conv, repeat the delete:', '36'))
    still_running, out = _neuter_and_subrun()
    if not still_running:
        _fail('NC did not confirm the abort call is load-bearing:\n' + out)
    _ok('NC: with the abort call dead, the running task survives the delete (abort is load-bearing)')

    print()
    print(_color('═══ ALL DELETE-CONV ABORT/DISARM TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
