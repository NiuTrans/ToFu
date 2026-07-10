#!/usr/bin/env python3
"""Regression: a human message QUEUED while a task was running must not be
LOST when the server restarts.

WHY
---
When a task is already running for a conversation, ``/api/chat/send`` stores
the new message ONLY in ``message_queue`` (never in ``conversations.messages``
— deliberate, so it doesn't render mid-stream). The queue row is durable, but
the ONLY things that drain it are the post-task-completion hook, a human send,
and the Project-Brain idle drain — NONE of which fire on a fresh boot for a
conversation whose running task died with the process. So after a restart the
message is shown in the queue bar, never processed, with no transcript trace =
total loss.

Fix: ``lib.message_queue.redispatch_orphaned_queue_on_startup()`` — called from
``recover_stale_tasks_on_startup`` (``lib/tasks_pkg/manager.py``) immediately
after the autopilot resume block — scans every conv with a dispatchable queue
row and dispatches ONE task via the SAME
``dispatch_next_queued`` seam — which appends the queued user message to
``conversations.messages`` (durable transcript home at last) and spawns the
task. Mirrors ``resume_armed_autopilot_after_crash``.

Tests (drive the REAL shipped functions against a real DB; ``spawn_task`` is
stubbed to a no-op so no LLM thread runs):
  1. ``test_redispatch_persists_and_dispatches`` — a stranded ``_user_msg``
     queue row → the msg lands in conversations.messages, the queue row is
     drained, a task_id is returned. ★ THE FIX.
  2. ``test_autopilot_only_conv_not_dispatched`` — a conv whose ONLY queue row
     is the autopilot sentinel is NOT dispatched (that lane is resumed
     separately). ★ BEHAVIOUR PRESERVATION.
  3. ``test_list_orphaned_excludes_autopilot`` — the scan lists dispatchable
     convs only.

Double-neuter (manual): stub ``dispatch_next_queued`` to a no-op inside
``redispatch_orphaned_queue_on_startup`` → test #1 FAILS (msg never persisted).
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed_conv(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'requeue-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _cleanup(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE conv_id=?', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()


def test_redispatch_persists_and_dispatches(monkeypatch):
    """A queued human msg stranded by restart is re-dispatched: appended to
    conversations.messages + a task spawned + the queue row drained."""
    import lib.tasks_pkg as tp
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib import message_queue as mq

    conv_id = 'cv-requeue-basic'
    db = get_thread_db(DOMAIN_CHAT)
    # Conversation whose last turn is the completed assistant reply — a NEW
    # user msg was queued behind the (now-dead) running task.
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'first question', 'timestamp': 1},
        {'role': 'assistant', 'content': 'first answer', 'timestamp': 2,
         'finishReason': 'stop'},
    ])

    # Stub spawn_task so no real LLM thread runs; record that it was called.
    spawned_tasks = []
    monkeypatch.setattr(tp, 'spawn_task', lambda task: spawned_tasks.append(task['id']))

    # Enqueue a pre-built user msg exactly like /api/chat/send does when a task
    # is running (the KIND_REAL path with `_user_msg`).
    queued_user_msg = {
        'role': 'user',
        'content': 'the queued question that must survive a restart',
        'timestamp': int(time.time() * 1000),
    }
    mq.enqueue_message(conv_id, {'_user_msg': queued_user_msg, 'text': queued_user_msg['content']},
                       {'model': 'gpt-4o'}, kind=mq.KIND_REAL)

    try:
        spawned = mq.redispatch_orphaned_queue_on_startup()

        # A task was dispatched for our conv.
        assert conv_id in [t for t in spawned] or len(spawned) >= 1, \
            f'expected a spawned task, got {spawned}'
        assert spawned_tasks, 'spawn_task was never called — no task started'

        # The queued user message now lives in conversations.messages (durable).
        row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                         (conv_id,)).fetchone()
        msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
        contents = [m.get('content') for m in msgs]
        assert 'the queued question that must survive a restart' in contents, (
            f'queued msg NOT persisted to conversation — lost. Got: {contents}')

        # The queue row was drained.
        depth = mq.get_queue_depth(conv_id)
        assert depth == 0, f'expected drained queue, still {depth} rows'
    finally:
        _cleanup(db, conv_id)
    _ok('stranded queued human msg re-dispatched: persisted to conv + task spawned + queue drained')


def test_autopilot_only_conv_not_dispatched(monkeypatch):
    """A conv whose ONLY queue row is the autopilot sentinel must NOT be
    dispatched by the orphaned-queue scan (that lane resumes separately)."""
    import lib.tasks_pkg as tp
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib import message_queue as mq

    conv_id = 'cv-requeue-autopilot-only'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', 'timestamp': 1},
        {'role': 'assistant', 'content': 'a', 'timestamp': 2, 'finishReason': 'stop'},
    ])

    spawned_tasks = []
    monkeypatch.setattr(tp, 'spawn_task', lambda task: spawned_tasks.append(task['id']))

    mq.arm_autopilot_marker(conv_id, {'model': 'gpt-4o'})

    try:
        # Our conv must NOT appear in the orphaned-dispatchable scan.
        assert conv_id not in mq.list_orphaned_dispatchable_convs(), (
            'autopilot-only conv wrongly listed as having a dispatchable orphaned row')
        # And a full redispatch pass must not spawn a task for it.
        mq.redispatch_orphaned_queue_on_startup()
        # (Other convs from other tests may spawn; assert OUR conv didn't get a
        #  new user msg appended — its transcript is unchanged at 2 msgs.)
        row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                         (conv_id,)).fetchone()
        msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
        assert len(msgs) == 2, f'autopilot-only conv should be untouched, got {len(msgs)} msgs'
    finally:
        _cleanup(db, conv_id)
    _ok('autopilot-only conv is NOT dispatched by the orphaned-queue scan')


def test_reaped_stuck_task_finalizes_conversation(monkeypatch):
    """A wedged task force-failed by the reaper must run the FULL terminal path:
    an assistant error bubble is synced onto the conversation EVEN when the
    trailing turn is an unanswered user message, activeTaskId is cleared, and a
    turn queued behind the wedged one is drained. This is the fix for the
    "agent stuck on a perpetual waiting bubble + queued msg stranded" incident
    (a peer/brain message dispatched into a conv whose task then wedged)."""
    import lib.tasks_pkg as tp
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.tasks_pkg import manager
    from lib import message_queue as mq

    conv_id = 'cv-reap-finalize'
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    # Conversation whose trailing turn is an UNANSWERED user message (the
    # dispatched-then-wedged shape) + activeTaskId pointing at the dead task.
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'reap-finalize',
        'messages': json_dumps_pg([
            {'role': 'user', 'content': 'earlier q', 'timestamp': 1},
            {'role': 'assistant', 'content': 'earlier a', 'timestamp': 2,
             'finishReason': 'stop'},
            {'role': 'user', 'content': 'the peer message with no reply',
             'timestamp': now_ms, '_peerMessage': True, '_fromConv': 'cSENDER'},
        ]),
        'msg_count': 3, 'created_at': now_ms, 'updated_at': now_ms,
        'settings': _json.dumps({'activeTaskId': 'task-wedged-1'}),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()

    # A SECOND turn queued behind the wedged one (the stranded-in-input-box msg).
    mq.enqueue_message(conv_id, {'_user_msg': {
        'role': 'user', 'content': 'the queued follow-up', 'timestamp': now_ms},
        'text': 'the queued follow-up'}, {'model': 'gpt-4o'}, kind=mq.KIND_REAL)

    spawned_tasks = []
    monkeypatch.setattr(tp, 'spawn_task', lambda task: spawned_tasks.append(task['id']))
    # Register the wedged task as the conv's latest so the freshness guard in
    # _sync_result_to_conversation passes (the wedged task owns the turn).
    manager._record_latest_task(conv_id, 'task-wedged-1')

    from lib.error_envelope import make_envelope
    wedged = {
        'id': 'task-wedged-1', 'convId': conv_id, 'status': 'error',
        'aborted': True, '_abort_reason': 'stuck_no_progress',
        'content': '', 'thinking': '', 'finishReason': 'error',
        'config': {'model': 'aws.claude-opus-4.8'},
        'created_at': time.time(), 'finished_at': time.time(),
        'events': [], 'error': make_envelope(
            'internal', detail='Task made no progress for 1804 seconds and was '
            'terminated as wedged.', model='aws.claude-opus-4.8',
            context='stuck-task-reaper', source='lib.tasks_pkg.manager'),
    }
    import threading
    wedged['events_lock'] = threading.Lock()

    try:
        manager._finalize_reaped_stuck_task(wedged)

        # (1) An assistant error bubble was appended for the unanswered peer
        #     turn. (It is NOT the tail — the queue-drain step below then
        #     appends the follow-up user turn after it — so assert it EXISTS,
        #     directly after the peer message it answers.)
        row = db.execute('SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
                         (conv_id,)).fetchone()
        msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
        err_bubbles = [m for m in msgs if m.get('role') == 'assistant' and m.get('error')]
        assert err_bubbles, (
            'reaper must append an assistant error bubble for the unanswered turn')
        peer_idx = next(i for i, m in enumerate(msgs) if m.get('_peerMessage'))
        assert msgs[peer_idx + 1].get('role') == 'assistant' and msgs[peer_idx + 1].get('error'), (
            'the error bubble must directly answer the unanswered peer turn')

        # (2) The DEAD task's activeTaskId is no longer pinned. (Here a
        #     follow-up was queued, so the drain spawns a NEW task that
        #     legitimately becomes activeTaskId — the point is the WEDGED task
        #     is gone, so the conv is no longer stuck attached to a dead stream.)
        st = _json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        assert st.get('activeTaskId') != 'task-wedged-1', (
            f'the dead wedged task must be unpinned, still {st.get("activeTaskId")}')

        # (3) The queued follow-up was drained (appended + a task spawned).
        contents = [m.get('content') for m in msgs]
        assert 'the queued follow-up' in contents, (
            f'the stranded queued msg must be drained into the conv, got {contents}')
        assert spawned_tasks, 'the queued follow-up must spawn a task'
        assert mq.get_queue_depth(conv_id) == 0, 'queue must be drained'
    finally:
        _cleanup(db, conv_id)
        try:
            from lib.tasks_pkg.manager import _conv_latest_task, _conv_latest_task_lock
            with _conv_latest_task_lock:
                _conv_latest_task.pop(conv_id, None)
        except Exception:
            pass
    _ok('reaped wedged task finalizes conv: error bubble + activeTaskId cleared + queue drained')


def test_NC_reaped_task_guard_still_blocks_regenerate_truncation(monkeypatch):
    """NEUTER of the guard-relaxation's SCOPE: a task aborted for a REASON OTHER
    than stuck_no_progress (e.g. a Stop→Regenerate supersede) whose trailing
    turn is role=user must STILL be dropped (no resurrected assistant slot).
    Proves the relaxation is narrowly keyed on 'stuck_no_progress', not a blanket
    'append on any aborted task' that would reintroduce the truncated-turn bug."""
    import json as _j
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.tasks_pkg import manager

    conv_id = 'cv-reap-guard-nc'
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'guard-nc',
        'messages': json_dumps_pg([
            {'role': 'user', 'content': 'regenerated question', 'timestamp': now_ms},
        ]),
        'msg_count': 1, 'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()
    manager._record_latest_task(conv_id, 'task-superseded-1')

    import threading
    superseded = {
        'id': 'task-superseded-1', 'convId': conv_id, 'status': 'aborted',
        'aborted': True, '_abort_reason': 'superseded',
        'content': 'stale content from the old turn', 'thinking': '',
        'finishReason': 'stop', 'config': {'model': 'x'},
        'created_at': time.time(), 'events': [], 'events_lock': threading.Lock(),
    }
    try:
        manager._sync_result_to_conversation(superseded, manager.build_result_meta(superseded))
        row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                         (conv_id,)).fetchone()
        msgs = _j.loads(row[0]) if isinstance(row[0], str) else row[0]
        assert len(msgs) == 1 and msgs[-1].get('role') == 'user', (
            f'a superseded (non-stuck) task must NOT append an assistant slot, got {msgs}')
    finally:
        _cleanup(db, conv_id)
        try:
            from lib.tasks_pkg.manager import _conv_latest_task, _conv_latest_task_lock
            with _conv_latest_task_lock:
                _conv_latest_task.pop(conv_id, None)
        except Exception:
            pass
    _ok('non-stuck aborted task still blocked from resurrecting a truncated turn (guard scope)')


def test_list_orphaned_excludes_autopilot(monkeypatch):
    """list_orphaned_dispatchable_convs lists convs with real/workflow/peer rows
    but excludes autopilot-only convs."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib import message_queue as mq

    real_conv = 'cv-orphan-real'
    ap_conv = 'cv-orphan-ap'
    db = get_thread_db(DOMAIN_CHAT)
    try:
        mq.enqueue_message(real_conv, {'text': 'hi'}, {'model': 'x'}, kind=mq.KIND_REAL)
        mq.arm_autopilot_marker(ap_conv, {'model': 'x'})

        listed = set(mq.list_orphaned_dispatchable_convs())
        assert real_conv in listed, 'conv with a real row should be listed'
        assert ap_conv not in listed, 'autopilot-only conv must NOT be listed'
    finally:
        from lib.database import db_execute_with_retry
        db_execute_with_retry(db, 'DELETE FROM message_queue WHERE conv_id IN (?, ?)',
                              (real_conv, ap_conv))
        db.commit()
    _ok('list_orphaned_dispatchable_convs excludes autopilot-only convs')


def main():
    print()
    print(_color('═══ queue re-dispatch after restart tests ═══', '36'))
    print()

    # ⚠️ DATA-LOSS GUARD: standalone mode skips conftest (no force-sqlite, no
    # pytest_configure gate), so a bare `python tests/x.py` would seed a
    # `requeue-test` conversation into the ambient TOFU_DB_BACKEND (production
    # PG when .env sets postgres). The shared helper forces sqlite + a throwaway
    # DB, asserts the resolved DB is a test DB, and bootstraps the schema.
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_queue_redispatch_after_restart.__main__')

    # Minimal monkeypatch shim (these tests run standalone too, not just pytest).
    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)
            self._undo = []

    tests = [
        test_redispatch_persists_and_dispatches,
        test_autopilot_only_conv_not_dispatched,
        test_reaped_stuck_task_finalizes_conversation,
        test_NC_reaped_task_guard_still_blocks_regenerate_truncation,
        test_list_orphaned_excludes_autopilot,
    ]
    for fn in tests:
        mp = _MP()
        try:
            fn(mp)
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        finally:
            mp.undo()
    print()
    print(_color(f'═══ ALL {len(tests)} QUEUE RE-DISPATCH TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
