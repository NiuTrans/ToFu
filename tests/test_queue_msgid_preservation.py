#!/usr/bin/env python3
"""tests/test_queue_msgid_preservation.py — the QUEUED send lane preserves the
client-supplied ``_msgId`` end-to-end (enqueue → dispatch_next_queued → append).

WHY (the second lost-ACK path)
------------------------------
There are TWO persist paths for a user turn:
  • immediate — ``/api/chat/send`` when no task is running (fixed via
    lib/chat/turn_builder.build_user_msg_from_payload preserving _msgId);
  • QUEUED — send-while-a-task-is-running: the user sends on a slow network, it
    hangs, they send again → the turn is enqueued and later persisted by
    ``dispatch_next_queued`` (lib/message_queue.py), NOT by the immediate path.

If the queued lane drops ``_msgId``, a queued-then-rescued message duplicates on
a poor network exactly like the immediate path did before the fix: the client's
rescue-PUT rebase (keyed on ``_msgId``) can't match the server's persisted copy
and appends a second user bubble.

The queued lane has two sub-paths, BOTH must preserve _msgId:
  1. pre-built (``payload['_user_msg']`` from /api/chat/send) — carries _msgId
     because build_user_msg_from_payload now sets it; this test proves it
     SURVIVES the enqueue→JSON round-trip→dispatch append.
  2. legacy (``/api/chat/queue`` old API — no _user_msg) — dispatch_next_queued
     builds the user_msg from scratch; the fix carries ``payload['_msgId']``.

Drives the REAL ``enqueue_message`` + ``dispatch_next_queued`` against a real DB,
patching create_task/spawn_task to no-ops (the user-msg PERSIST happens BEFORE
task creation, so the persisted conversations.messages row is asserted directly).

NEUTER: strip the legacy ``if payload.get('_msgId'): user_msg['_msgId'] = …``
line → the persisted queued message loses its _msgId → the assertion FAILS.

Env note: run DIRECTLY (``python tests/test_queue_msgid_preservation.py``) —
bare pytest may lack the schema bootstrap.
"""

import json as _json
import os
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'q-msgid',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now, 'updated_at': now, 'search_text': '',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'search_text'], retry=True)
    db.commit()


def _read(db, conv_id):
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    return _json.loads(row[0]) if isinstance(row[0], str) else row[0]


def _cleanup(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM message_queue WHERE conv_id=?', (conv_id,))
    db.commit()


class _FakeTask(dict):
    pass


def _dispatch_with_stubbed_spawn(conv_id):
    """Run the REAL dispatch_next_queued but stub create_task/spawn_task so no
    real task thread starts. The user-message PERSIST happens BEFORE create_task,
    so the persisted row is fully written by the time we stub-return."""
    import lib.message_queue as mq
    fake_task = _FakeTask(id='task-stub-1234')
    with mock.patch('lib.tasks_pkg.create_task', return_value=fake_task), \
         mock.patch('lib.tasks_pkg.spawn_task', return_value=None), \
         mock.patch('lib.tasks_pkg.conv_message_builder.build_api_messages_from_db',
                    return_value=[{'role': 'user', 'content': 'x'}]), \
         mock.patch('lib.conversations.set_conversation_settings', return_value=None), \
         mock.patch('lib.conversations.invalidate_meta_cache', return_value=None):
        return mq.dispatch_next_queued(conv_id)


def test_prebuilt_path_preserves_msgId():
    """Pre-built (_user_msg from /api/chat/send): _msgId survives the
    enqueue→JSON→dispatch round-trip."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import enqueue_message
    conv_id = 'cv-q-prebuilt'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [{'role': 'user', 'content': 'q0', 'timestamp': 1, '_msgId': 'm-q0'}])
    try:
        # /api/chat/send builds user_msg (now WITH _msgId) and stashes it as
        # _user_msg in the queue payload.
        user_msg = {'role': 'user', 'content': 'hello', 'timestamp': 2000, '_msgId': 'tmp_client_pre'}
        enqueue_message(conv_id, {'text': 'hello', 'timestamp': 2000,
                                  '_msgId': 'tmp_client_pre', '_user_msg': user_msg}, {})
        tid = _dispatch_with_stubbed_spawn(conv_id)
        assert tid == 'task-stub-1234', f'dispatch did not run/return: {tid}'
        msgs = _read(db, conv_id)
        appended = [m for m in msgs if m.get('content') == 'hello']
        assert appended, f'queued msg not persisted: {msgs}'
        assert appended[-1].get('_msgId') == 'tmp_client_pre', (
            f'pre-built path dropped _msgId: {appended[-1]}')
    finally:
        _cleanup(db, conv_id)
    _ok('pre-built (_user_msg) path preserves client _msgId through enqueue→dispatch')


def test_legacy_path_preserves_msgId():
    """Legacy (/api/chat/queue old API, no _user_msg): dispatch builds the
    user_msg from scratch and must carry payload['_msgId']."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import enqueue_message
    conv_id = 'cv-q-legacy'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [{'role': 'user', 'content': 'q0', 'timestamp': 1, '_msgId': 'm-q0'}])
    try:
        # No _user_msg → legacy build path. autoTranslate off (empty config).
        enqueue_message(conv_id, {'text': 'legacy hello', 'timestamp': 3000,
                                  '_msgId': 'tmp_client_legacy'}, {})
        tid = _dispatch_with_stubbed_spawn(conv_id)
        assert tid == 'task-stub-1234', f'dispatch did not run/return: {tid}'
        msgs = _read(db, conv_id)
        appended = [m for m in msgs if m.get('content') == 'legacy hello']
        assert appended, f'queued msg not persisted: {msgs}'
        assert appended[-1].get('_msgId') == 'tmp_client_legacy', (
            f'legacy path dropped _msgId: {appended[-1]}')
    finally:
        _cleanup(db, conv_id)
    _ok('legacy path preserves client _msgId (dispatch_next_queued build)')


_POSITIVE = [test_prebuilt_path_preserves_msgId, test_legacy_path_preserves_msgId]


def _run(fn):
    try:
        fn(); return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}'); return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(' ', _color('✗', '31'), f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


def _neuter_legacy_and_subrun():
    """NC: strip the legacy _msgId-carry line, re-run the legacy test in a
    subprocess, and assert it FAILS (persisted msg loses _msgId). Proves the
    carry line is load-bearing. Restores the file byte-identical."""
    import subprocess
    target = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'lib', 'message_queue.py')
    with open(target, encoding='utf-8') as f:
        src = f.read()
    anchor = "if payload.get('_msgId'):\n                user_msg['_msgId'] = payload['_msgId']"
    assert anchor in src, 'NC anchor (legacy _msgId carry) not found'
    neut = src.replace(anchor, "if False:  # NC\n                user_msg['_msgId'] = payload['_msgId']", 1)
    with open(target, 'w', encoding='utf-8') as f:
        f.write(neut)
    try:
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import quart; sys.modules['flask']=quart\n"
            "import tests.test_queue_msgid_preservation as T\n"
            "try:\n"
            "    T.test_legacy_path_preserves_msgId()\n"
            "    print('NC_UNEXPECTED_PASS'); sys.exit(1)\n"
            "except AssertionError:\n"
            "    print('NC_FAILED_AS_EXPECTED'); sys.exit(0)\n"
        ) % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))),)
        r = subprocess.run([sys.executable, '-c', code],
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           capture_output=True, text=True)
        return 'NC_FAILED_AS_EXPECTED' in r.stdout, r.stdout + r.stderr
    finally:
        with open(target, 'w', encoding='utf-8') as f:
            f.write(src)


def main():
    print()
    print(_color('═══ queued-lane _msgId preservation (both sub-paths) ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_queue_msgid_preservation.__main__')

    print(_color('Baseline (shipped queue lane):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix the queue lane before neutering')

    print()
    print(_color('NC — strip the legacy _msgId carry, re-run legacy test:', '36'))
    ok, out = _neuter_legacy_and_subrun()
    if not ok:
        _fail('NC did not confirm the carry line is load-bearing:\n' + out)
    _ok('NC: without the carry line the legacy queued msg loses _msgId (load-bearing)')

    print()
    print(_color('═══ ALL QUEUE-MSGID TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
