#!/usr/bin/env python3
"""The sidebar's incomplete/errored dot on a messages-stripped (?meta=1) shell
reads three RAW settled-turn facts the backend stamps into
``conversations.settings``:

  * ``lastFinishReason`` — the tail assistant's finishReason (or null)
  * ``lastMsgError``     — bool(tail.error)
  * ``lastMsgHasOutput`` — bool(content|thinking|toolRounds|_igResults)

The CLASSIFICATION (incomplete vs errored vs done) stays in the frontend's
``_convStatusFlags`` so there is a single classifier; the backend only supplies
facts. These facts MUST be written by all THREE code paths that settle a
conversation's tail, or the field goes stale:

  1. ``persist_conv_messages``            (lib/chat/persistence.py)
  2. ``_sync_result_to_conversation``     (lib/tasks_pkg/manager.py)
  3. ``recover_stale_tasks_on_startup``   (lib/tasks_pkg/manager.py)

This test drives the REAL shipped functions against the session DB and asserts
each stamps the facts, and that a normally-completed turn (finishReason='stop')
is not misjudged. Includes a NEUTER proving the recover path's stamp is
load-bearing.

Run:  python tests/test_sidebar_settled_facts.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart

import pytest  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _settings(db, conv_id):
    row = db.execute('SELECT settings FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    raw = row[0] if row else '{}'
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def _del(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM task_results WHERE conv_id=?', (conv_id,))
    db.commit()


# ── Write point 1: persist_conv_messages ────────────────────────────────────

def test_persist_conv_messages_stamps_facts():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.chat.persistence import persist_conv_messages

    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'cv-facts-persist-{int(time.time()*1000)}'
    try:
        msgs = [
            {'role': 'user', 'content': 'q', 'timestamp': 1},
            {'role': 'assistant', 'content': 'partial', 'thinking': '',
             'finishReason': 'interrupted', 'timestamp': 2},
        ]
        persist_conv_messages(db, conv_id, msgs, 'facts-test')
        s = _settings(db, conv_id)
        assert s.get('lastMsgRole') == 'assistant', s
        assert s.get('lastFinishReason') == 'interrupted', s
        assert s.get('lastMsgError') is False, s
        assert s.get('lastMsgHasOutput') is True, s  # has content
    finally:
        _del(db, conv_id)
    _ok('persist_conv_messages stamps lastFinishReason/lastMsgError/lastMsgHasOutput')


def test_persist_conv_messages_empty_placeholder():
    """A dangling assistant placeholder (no finishReason, no output) — the
    mra8htdw edge — must record hasOutput=False so the frontend classifies it
    incomplete."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.chat.persistence import persist_conv_messages

    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'cv-facts-empty-{int(time.time()*1000)}'
    try:
        msgs = [
            {'role': 'user', 'content': 'q', 'timestamp': 1},
            {'role': 'assistant', 'content': '', 'thinking': '', 'timestamp': 2},
        ]
        persist_conv_messages(db, conv_id, msgs, 'facts-empty')
        s = _settings(db, conv_id)
        assert s.get('lastFinishReason') is None, s
        assert s.get('lastMsgHasOutput') is False, s
    finally:
        _del(db, conv_id)
    _ok('persist_conv_messages records hasOutput=False for a dangling empty placeholder')


# ── Write point 2: _sync_result_to_conversation ─────────────────────────────

def test_sync_result_stamps_facts():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        build_result_meta,
                                        _conv_latest_task, _conv_latest_task_lock)

    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'cv-facts-sync-{int(time.time()*1000)}'
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'sync-facts',
        'messages': json_dumps_pg([
            {'role': 'user', 'content': 'U', 'timestamp': 1},
            {'role': 'assistant', 'content': '', 'timestamp': 2},
        ]),
        'msg_count': 2, 'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()
    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U'}], {})
        task['content'] = 'the full answer'
        task['finishReason'] = 'stop'
        task['model'] = 'm'
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']
        _sync_result_to_conversation(task, build_result_meta(task))
        s = _settings(db, conv_id)
        # A normally-completed turn: stop → the frontend will treat it complete.
        assert s.get('lastFinishReason') == 'stop', s
        assert s.get('lastMsgError') is False, s
        assert s.get('lastMsgHasOutput') is True, s
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        _del(db, conv_id)
    _ok('_sync_result_to_conversation stamps facts (stop → not misjudged)')


# ── Write point 3: recover_stale_tasks_on_startup ───────────────────────────

def _seed_running(db, conv_id, task_content, tail_msg):
    """Seed a conv with a placeholder tail + a running task_results row (the
    crash state recover_stale_tasks_on_startup cleans up)."""
    from lib.database._core_schema import CONVERSATIONS, TASK_RESULTS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    tid = f'tk-{conv_id}'
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'recover-facts',
        'messages': json_dumps_pg([
            {'role': 'user', 'content': 'U', 'timestamp': 1},
            tail_msg,
        ]),
        'settings': json.dumps({'activeTaskId': tid}),
        'msg_count': 2, 'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'settings',
                    'msg_count', 'created_at', 'updated_at'], retry=True)
    upsert(db, TASK_RESULTS, {
        'task_id': tid, 'conv_id': conv_id, 'content': task_content,
        'thinking': '', 'status': 'running', 'created_at': now_ms,
    }, insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'status',
                    'created_at'], retry=True)
    db.commit()
    return tid


def test_recover_stamps_facts():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'cv-facts-recover-{int(time.time()*1000)}'
    _seed_running(db, conv_id, 'recovered partial answer',
                  {'role': 'assistant', 'content': '', 'timestamp': 2})
    try:
        recover_stale_tasks_on_startup()
        s = _settings(db, conv_id)
        # recover stamps finishReason='interrupted' onto the tail → facts reflect it.
        assert s.get('lastMsgRole') == 'assistant', s
        assert s.get('lastFinishReason') == 'interrupted', s
        assert s.get('lastMsgError') is False, s
        assert s.get('lastMsgHasOutput') is True, s  # merged content
    finally:
        _del(db, conv_id)
    _ok('recover_stale_tasks_on_startup stamps interrupted facts')


def test_recover_stamp_is_load_bearing_neuter():
    """NEUTER: if recover did NOT stamp the facts (as before this change), the
    settings would carry NO lastFinishReason even though the tail is
    interrupted → the sidebar could not show the dot. We simulate the pre-fix
    state by seeding a conv whose settings lack the facts and asserting the
    REAL recover fills them; then we prove the assertion bites by checking the
    pre-recover settings had none."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'cv-facts-neuter-{int(time.time()*1000)}'
    _seed_running(db, conv_id, 'partial',
                  {'role': 'assistant', 'content': '', 'timestamp': 2})
    try:
        pre = _settings(db, conv_id)
        assert 'lastFinishReason' not in pre, 'precondition: facts absent before recover'
        recover_stale_tasks_on_startup()
        post = _settings(db, conv_id)
        assert post.get('lastFinishReason') == 'interrupted', (
            'recover did NOT stamp lastFinishReason — the sidebar dot would be '
            'invisible until the user opens the conv (the bug this fixes)')
    finally:
        _del(db, conv_id)
    _ok('NEUTER: recover fills facts that were absent pre-fix (stamp is load-bearing)')


def main():
    print()
    print(_color('═══ sidebar settled-facts (settings) tests ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_sidebar_settled_facts.__main__')
    tests = [
        test_persist_conv_messages_stamps_facts,
        test_persist_conv_messages_empty_placeholder,
        test_sync_result_stamps_facts,
        test_recover_stamps_facts,
        test_recover_stamp_is_load_bearing_neuter,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
