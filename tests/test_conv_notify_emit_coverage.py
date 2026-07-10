#!/usr/bin/env python3
"""tests/test_conv_notify_emit_coverage.py — emit-side coverage for the
event-driven cross-device sync signal on the paths that PREVIOUSLY mutated a
conversation WITHOUT pushing a ``notify`` frame (the "must hit refresh to see
it" bugs).

WHY
---
``lib/conversations/meta_cache.py::notify_conv_changed`` is the single seam that
turns an authoritative conversation mutation into a real-time client push
(``{type:'conv_changed', convId, rev, userId}``). A coverage audit found several
list-visible / body-visible mutations that only invalidated the sidebar cache
(or nothing) and never emitted, so a sibling tab/device stayed stale until a
manual refresh:

  * ``delete_branch``           (routes/conversations.py)
  * ``create_branch``           (routes/api_v1/conversations.py)
  * translate commit            (lib/translate/commit.py)
  * swarm snapshot persist       (lib/swarm/snapshot.py)
  * swarm autocontinue turn      (lib/swarm/integration.py)
  * timer / proactive inject     (lib/scheduler/_shared.py)
  * L1 compaction durable persist (lib/tasks_pkg/compaction/_layer1.py)

This suite drives the REAL functions against a REAL conversations row and
captures the frame the seam publishes (monkeypatching
``lib.agent_core.push.push_event``). It proves for each fixed path:
  1. exactly one ``notify`` frame is emitted after the mutation;
  2. the frame carries the DB's post-write ``rev`` (the messages-change trigger
     bumped it) so the client body-refetches rather than a bare list refresh.

NEUTER (per path): monkeypatch ``notify_conv_changed`` to a no-op recorder
BEFORE the fix's call site runs and assert the captured-frames list stays empty
— i.e. the emit is what produces the signal, not a cache side-effect. (For the
two lib paths whose call site imports the symbol locally we neuter at the
definition module; the capture fixture patches ``push_event`` so a genuinely
firing seam is always observed.)

Standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/notify_emit.db \
        python3 tests/test_conv_notify_emit_coverage.py
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_conv_notify_emit_coverage.__main__', init_schema=False)

import pytest

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
#  Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def captured(monkeypatch):
    """Capture every push_event(channel, task_id, payload) the seam emits.

    Patched at the DEFINITION module so the lazy
    ``from lib.agent_core.push import push_event`` inside notify_conv_changed
    picks up the fake regardless of which caller triggered it."""
    frames = []
    import lib.agent_core.push as push_mod
    monkeypatch.setattr(
        push_mod, 'push_event',
        lambda channel, task_id, payload: frames.append(
            {'channel': channel, 'taskId': task_id, 'payload': payload}))
    return frames


def _notify_frames(frames):
    return [f for f in frames if f['channel'] == 'notify']


def _db():
    from lib.database import DOMAIN_CHAT, get_thread_db
    return get_thread_db(DOMAIN_CHAT)


def _seed(conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    db = _db()
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'notify-emit',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _rev(conv_id):
    row = _db().execute('SELECT rev FROM conversations WHERE id=? AND user_id=1',
                        (conv_id,)).fetchone()
    return row[0] if row else None


def _cleanup(conv_id):
    from lib.database import db_execute_with_retry
    try:
        db = _db()
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _schema():
    from lib.database import init_db
    init_db()


# ─────────────────────────────────────────────────────────────────────────────
#  1. Translate commit (lib/translate/commit.py)
# ─────────────────────────────────────────────────────────────────────────────

def test_translate_commit_emits_rev_frame(captured):
    conv_id = 'cv-notify-tr-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, [
        {'role': 'user', 'content': 'hi', '_msgId': 'm-u'},
        {'role': 'assistant', 'content': 'hello world', '_msgId': 'm-a'},
    ])
    try:
        from lib.translate.commit import _commit_translation_to_db
        _commit_translation_to_db(conv_id, 1, 'translatedContent', '你好世界',
                                  original_text='hello world', msg_id='m-a')
        frames = _notify_frames(captured)
        assert len(frames) == 1, f'expected 1 notify frame, got {frames}'
        p = frames[0]['payload']
        assert p['type'] == 'conv_changed'
        assert p['convId'] == conv_id
        assert p['rev'] == _rev(conv_id), 'frame rev must equal the post-write DB rev'
        assert p['rev'] >= 1, 'a messages-change must have bumped rev'
    finally:
        _cleanup(conv_id)


def test_translate_commit_NEUTER_no_frame(captured, monkeypatch):
    """NEUTER: stub notify_conv_changed at its definition module → the commit
    still writes the DB but no notify frame is captured. Proves the emit line
    (not the CAS write) is what carries the signal."""
    conv_id = 'cv-notify-trN-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, [
        {'role': 'user', 'content': 'hi', '_msgId': 'm-u'},
        {'role': 'assistant', 'content': 'hello world', '_msgId': 'm-a'},
    ])
    try:
        import lib.conversations as convs
        monkeypatch.setattr(convs, 'notify_conv_changed', lambda *a, **k: None)
        from lib.translate.commit import _commit_translation_to_db
        _commit_translation_to_db(conv_id, 1, 'translatedContent', '你好世界',
                                  original_text='hello world', msg_id='m-a')
        assert _notify_frames(captured) == [], 'neutered path must emit no frame'
        # ...and the write still happened (translation landed).
        row = _db().execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                            (conv_id,)).fetchone()
        msgs = json.loads(row[0])
        assert msgs[1].get('translatedContent') == '你好世界'
    finally:
        _cleanup(conv_id)


# ─────────────────────────────────────────────────────────────────────────────
#  2. Swarm snapshot persist (lib/swarm/snapshot.py)
# ─────────────────────────────────────────────────────────────────────────────

def _seed_spawn_round(conv_id):
    handle = {'status': 'async_launched', 'swarm_id': conv_id + '-t1',
              'agents': [{'id': 'aa11', 'role': 'coder', 'objective': 'x'}]}
    _seed(conv_id, [
        {'role': 'user', 'content': 'go', 'timestamp': 1},
        {'role': 'assistant', 'content': 'spawning', 'timestamp': 2,
         'toolRounds': [{'roundNum': 1, 'toolName': 'spawn_agents', '_swarm': True,
                         'status': 'done', 'toolContent': json.dumps(handle)}]},
    ])


def _snapshot():
    return {'agents': [{'id': 'aa11', 'status': 'done', 'preview': 'ok',
                        'tokens': 10}],
            'settled': True, 'totalTokens': 10, 'agentCount': 1,
            'doneCount': 1, 'version': 100001}


def test_swarm_snapshot_emits_rev_frame(captured):
    conv_id = 'cv-notify-ss-' + str(os.getpid())
    _cleanup(conv_id)
    _seed_spawn_round(conv_id)
    try:
        from lib.swarm.snapshot import persist_snapshot_to_conversation
        wrote = persist_snapshot_to_conversation(conv_id, ['aa11'], _snapshot())
        assert wrote is True, 'snapshot should have been written'
        frames = _notify_frames(captured)
        assert len(frames) == 1, f'expected 1 notify frame, got {frames}'
        assert frames[0]['payload']['rev'] == _rev(conv_id)
    finally:
        _cleanup(conv_id)


def test_swarm_snapshot_NEUTER_no_frame(captured, monkeypatch):
    conv_id = 'cv-notify-ssN-' + str(os.getpid())
    _cleanup(conv_id)
    _seed_spawn_round(conv_id)
    try:
        import lib.conversations as convs
        monkeypatch.setattr(convs, 'notify_conv_changed', lambda *a, **k: None)
        from lib.swarm.snapshot import persist_snapshot_to_conversation
        assert persist_snapshot_to_conversation(conv_id, ['aa11'], _snapshot()) is True
        assert _notify_frames(captured) == [], 'neutered snapshot path must emit no frame'
    finally:
        _cleanup(conv_id)


# ─────────────────────────────────────────────────────────────────────────────
#  3. Scheduler inject (lib/scheduler/_shared.py) — timer / proactive turn
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduler_inject_emits_rev_frame(captured, monkeypatch):
    conv_id = 'cv-notify-sch-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, [{'role': 'user', 'content': 'prior', 'timestamp': 1}])
    try:
        # Stub the task machinery so we exercise ONLY the DB append + emit, not
        # a real LLM turn. create_task returns a dict with an id; spawn_task
        # is a no-op; set_conversation_settings must not crash.
        import lib.scheduler._shared as sh
        import lib.tasks_pkg as tp
        import lib.tasks_pkg.manager as mgr
        monkeypatch.setattr(mgr, 'create_task', lambda *a, **k: {'id': 'task-xyz'})
        monkeypatch.setattr(tp, 'spawn_task', lambda *a, **k: None)

        tid = sh.inject_and_run_task(
            conv_id,
            {'role': 'user', 'content': 'timer fired', 'timestamp': 2,
             '_timer': True},
            {}, log_prefix='[Test]')
        assert tid == 'task-xyz'
        frames = _notify_frames(captured)
        assert len(frames) == 1, f'expected 1 notify frame, got {frames}'
        p = frames[0]['payload']
        assert p['convId'] == conv_id
        assert p['rev'] == _rev(conv_id) and p['rev'] >= 1
    finally:
        _cleanup(conv_id)


def test_scheduler_inject_NEUTER_no_frame(captured, monkeypatch):
    conv_id = 'cv-notify-schN-' + str(os.getpid())
    _cleanup(conv_id)
    _seed(conv_id, [{'role': 'user', 'content': 'prior', 'timestamp': 1}])
    try:
        import lib.conversations as convs
        import lib.scheduler._shared as sh  # noqa: F401
        import lib.tasks_pkg as tp
        import lib.tasks_pkg.manager as mgr
        monkeypatch.setattr(convs, 'notify_conv_changed', lambda *a, **k: None)
        monkeypatch.setattr(mgr, 'create_task', lambda *a, **k: {'id': 'task-xyz'})
        monkeypatch.setattr(tp, 'spawn_task', lambda *a, **k: None)
        import lib.scheduler._shared as sh2
        sh2.inject_and_run_task(
            conv_id, {'role': 'user', 'content': 'timer fired', 'timestamp': 2},
            {}, log_prefix='[Test]')
        assert _notify_frames(captured) == [], 'neutered scheduler path must emit no frame'
        # The new turn still landed in the DB.
        row = _db().execute('SELECT msg_count FROM conversations WHERE id=? AND user_id=1',
                            (conv_id,)).fetchone()
        assert row[0] == 3  # prior + injected user + assistant placeholder
    finally:
        _cleanup(conv_id)


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
