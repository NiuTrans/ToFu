"""tests/test_engine_tail_settle.py — the two producer roots behind the
llm_sanitize same-role merge WARNING storm.

Measured 2026-08-04 (owner log-audit follow-up): 1,203 'UNEXPECTED pair'
warnings in ~2.5 days, 100% dispatch-family pairs at wire #2/#3. Forensics:

  * conv msco7vqmkf8yb2: two brain kickoffs appended 463 ms apart — a second
    dispatch entered right after the first spawned (tasks 17582690/cebd5669
    overlapped ~5 s). The first task ended empty-'done' (content_len=0),
    leaving a persisted user,user adjacency.
  * conv msb6ohqifdz7yj: a VU row (_isVirtualUser) whose follow-up never
    persisted a reply, then a brain kickoff appended on top.

Two root fixes pinned here:

  P1 — ``settle_unanswered_engine_tail`` (lib/chat/messages.py): inside
  ``append_user_msg_idempotent``, a genuinely-new append onto an ENGINE-
  flagged unanswered user tail first appends a NON-empty tombstone assistant
  row (an empty one would be dropped at wire-build and recreate the
  adjacency). Human tails are never settled — a human going unanswered must
  stay loud.

  P2 — the per-conv double-dispatch guard in ``dispatch_next_queued``
  (lib/message_queue.py): a live task for the conv ⇒ dispatch refused, the
  row stays queued for the completion hook.
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_QUEUE_SRC = os.path.join(ROOT, 'lib', 'message_queue.py')
_MSG_SRC = os.path.join(ROOT, 'lib', 'chat', 'messages.py')


# ── P1: settle_unanswered_engine_tail via append_user_msg_idempotent ──────

def _user(ts, **flags):
    m = {'role': 'user', 'content': 'x', 'timestamp': ts}
    m.update(flags)
    return m


def test_settle_on_brain_dispatch_tail():
    from lib.chat.messages import append_user_msg_idempotent
    msgs = [_user(1000, _brainDispatch=True)]
    appended = append_user_msg_idempotent(msgs, _user(2000, _brainDispatch=True))
    assert appended is True
    assert [m['role'] for m in msgs] == ['user', 'assistant', 'user']
    tomb = msgs[1]
    assert tomb['_engineNoReply'] is True
    assert tomb['finishReason'] == 'engine_no_output'
    assert tomb['content'].strip(), (
        'the tombstone must be non-empty — an empty one is dropped at '
        'wire-build time and the adjacency returns on the wire')


def test_settle_on_virtual_user_tail():
    """The msb6ohqifdz7yj shape: an unanswered VU row, then a kickoff."""
    from lib.chat.messages import append_user_msg_idempotent
    msgs = [_user(1000, _isVirtualUser=True)]
    append_user_msg_idempotent(msgs, _user(2000, _brainDispatch=True))
    assert [m['role'] for m in msgs] == ['user', 'assistant', 'user']


def test_no_settle_on_human_tail():
    """A human question going unanswered is a REAL incident — never paper it
    over with a tombstone (the merge WARNING must stay loud for it)."""
    from lib.chat.messages import append_user_msg_idempotent
    msgs = [_user(1000)]
    append_user_msg_idempotent(msgs, _user(2000))
    assert [m['role'] for m in msgs] == ['user', 'user']


def test_no_settle_on_assistant_tail_or_empty():
    from lib.chat.messages import append_user_msg_idempotent
    msgs = [{'role': 'assistant', 'content': 'a', 'timestamp': 900}]
    append_user_msg_idempotent(msgs, _user(2000, _brainDispatch=True))
    assert [m['role'] for m in msgs] == ['assistant', 'user']
    msgs2 = []
    append_user_msg_idempotent(msgs2, _user(2000, _brainDispatch=True))
    assert [m['role'] for m in msgs2] == ['user']


def test_twin_reconcile_never_inserts_tombstone():
    """The optimistic-copy reconcile (same timestamp) must NOT be split by a
    tombstone — it is the same logical message, not an adjacency."""
    from lib.chat.messages import append_user_msg_idempotent
    msgs = [_user(1000, _brainDispatch=True)]
    appended = append_user_msg_idempotent(msgs, _user(1000, _brainDispatch=True))
    assert appended is False
    assert len(msgs) == 1, 'twin reconcile must not append anything'


# ── P2: the double-dispatch guard ─────────────────────────────────────────

def test_dispatch_refused_while_task_live(flask_app, monkeypatch):
    """A live (non-aborted, running) task for the conv ⇒ dispatch_next_queued
    returns None and NEVER dequeues (the row stays for the completion hook)."""
    import lib.message_queue as mq
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks['livetask00000001'] = {'id': 'livetask00000001', 'convId': 'cDD',
                                     'status': 'running', 'aborted': False}
    called = []
    monkeypatch.setattr(mq, 'dequeue_next',
                        lambda c: called.append(c) or None)
    try:
        with flask_app.app_context():
            assert mq.dispatch_next_queued('cDD') is None
    finally:
        with tasks_lock:
            tasks.pop('livetask00000001', None)
    assert called == [], 'a live task must refuse BEFORE dequeuing the row'


def test_dispatch_proceeds_when_no_live_task(flask_app, monkeypatch):
    """No live task ⇒ the guard passes and the normal path runs (dequeue is
    reached; empty queue ⇒ returns None)."""
    import lib.message_queue as mq
    called = []
    monkeypatch.setattr(mq, 'dequeue_next', lambda c: called.append(c) or None)
    with flask_app.app_context():
        assert mq.dispatch_next_queued('cFREE') is None
    assert called == ['cFREE']


# ── End-to-end: a real drain settles an orphaned kickoff tail ─────────────

@pytest.fixture(scope='module', autouse=True)
def _ensure_schema(flask_app):
    from lib.database import init_db
    with flask_app.app_context():
        init_db()
    yield


@pytest.fixture(autouse=True)
def _clean(flask_app, monkeypatch):
    from lib.database import DOMAIN_CHAT, get_thread_db
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        for tbl in ('project_tasks', 'project_events', 'message_queue',
                    'conversations'):
            db.execute(f'DELETE FROM {tbl}')
        db.commit()
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
    yield


def test_real_drain_settles_orphaned_kickoff_tail(flask_app, monkeypatch):
    """DB-level: a conv whose tail is an unanswered kickoff user row; draining
    the next kickoff must persist [kickoff, tombstone, kickoff]."""
    import json as _json
    from lib.conversations.project_board import post_task
    from lib.conversations.project_dispatch import dispatch_epic, select_dispatchable
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.message_queue import dispatch_next_queued
    import lib.tasks_pkg as tp

    monkeypatch.setattr(tp, 'spawn_task', lambda task: None)
    with flask_app.app_context():
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)
        orphaned = {'role': 'user', 'content': '[Project Brain — autonomous dispatch] old',
                    'timestamp': now - 60000, '_brainDispatch': True}
        db.execute(
            'INSERT INTO conversations (id, user_id, title, messages, settings,'
            ' created_at, updated_at, search_text) VALUES (?, 1, ?, ?, ?, ?, ?, ?)',
            ('cSETTLE', 't', json_dumps_pg([orphaned]),
             json_dumps_pg({'projectPath': '/bp/settle', 'projectEnabled': True}),
             now, now, 'seed'))
        db.commit()
        # Mark busy BEFORE posting so the epic defers to the queue (mirroring
        # the provenance suite), then clear so the drain under test can run.
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            tasks['busytask-settle001'] = {'id': 'busytask-settle001',
                                           'convId': 'cSETTLE',
                                           'status': 'running', 'aborted': False}
        post_task('/bp/settle', 'cSETTLE', 'settle epic')
        with tasks_lock:
            tasks.pop('busytask-settle001', None)
        epic = select_dispatchable('/bp/settle')[0]
        assert dispatch_epic('/bp/settle', epic, 'cSETTLE')['ok']
        assert dispatch_next_queued('cSETTLE')
        row = db.execute('SELECT messages FROM conversations WHERE id=?',
                         ('cSETTLE',)).fetchone()
        roles = [m.get('role') for m in _json.loads(row['messages'])]
    assert roles == ['user', 'assistant', 'user'], (
        f'the orphaned kickoff tail must be settled before the new append, got {roles}')


# ── NC: both fixes are load-bearing ───────────────────────────────────────

from tests._nc_harness import patch_restore as _patch_restore  # noqa: E402


def test_NC_settle_call_is_load_bearing():
    """Drop the settle call → the engine tail gets no tombstone → adjacency."""
    from lib.chat.messages import append_user_msg_idempotent  # noqa

    def run():
        import lib.chat.messages as m
        msgs = [_user(1000, _brainDispatch=True)]
        m.append_user_msg_idempotent(msgs, _user(2000, _brainDispatch=True))
        assert [x['role'] for x in msgs] == ['user', 'user'], (
            'NC: without the settle call the adjacency forms')
    _patch_restore(
        _MSG_SRC,
        '    settle_unanswered_engine_tail(messages)\n    messages.append(user_msg)',
        '    messages.append(user_msg)  # NC (settle dropped)',
        run,
    )


def test_NC_live_guard_is_load_bearing(flask_app, monkeypatch):
    """Drop the live-task guard → dispatch proceeds while a task is live."""
    def run():
        import lib.message_queue as mq
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            tasks['livetask00000002'] = {'id': 'livetask00000002', 'convId': 'cNC2',
                                         'status': 'running', 'aborted': False}
        called = []
        monkeypatch.setattr(mq, 'dequeue_next', lambda c: called.append(c) or None)
        try:
            with flask_app.app_context():
                mq.dispatch_next_queued('cNC2')
        finally:
            with tasks_lock:
                tasks.pop('livetask00000002', None)
        assert called == ['cNC2'], (
            'NC: without the guard the second dispatch dequeues while live')
    _patch_restore(
        _QUEUE_SRC,
        "        if _conv_has_live_task(conv_id):\n"
        "            logger.info('[Queue] conv=%s already has a live task — dispatch '\n"
        "                        'refused; the completion hook will drain', conv_id[:8])\n"
        "            return None",
        '        pass  # NC (guard dropped)',
        run,
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
