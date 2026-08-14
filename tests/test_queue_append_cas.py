#!/usr/bin/env python3
"""tests/test_queue_append_cas.py — the queued-message append optimistic lock
(lib/message_queue.py::_append_user_msg_with_cas).

WHY
---
dispatch_next_queued used to append the dequeued user message to
conversations.messages via a bare read-modify-write of the whole blob:
``SELECT messages`` → append → unconditional ``UPDATE``. ``_dispatch_lock``
serializes dispatches within ONE process, but does NOT guard against a
concurrent frontend / other-writer UPDATE landing between the SELECT and the
UPDATE — a last-writer-wins clobber that silently drops the OTHER write. The
fix re-reads + CAS-es on ``updated_at`` (mirroring
manager._sync_partial_to_conversation), retrying against the fresh tail.

Tests (drive the REAL helper against a real DB):
  1. ``test_append_lands_normally`` — a plain append writes the message +
     bumps msg_count.
  2. ``test_append_survives_concurrent_writer`` — a writer bumps updated_at +
     appends its OWN message BETWEEN the helper's SELECT and its UPDATE (via a
     one-shot patched db.execute). The helper must CAS-miss, retry against the
     fresh tail, and land its message WITHOUT dropping the concurrent writer's
     message. ★ the clobber-prevention proof.
     Double-neuter: revert the UPDATE's ``AND updated_at=?`` guard → the
     concurrent writer's message is CLOBBERED → this FAILS.

Env note (see project memory): run DIRECTLY
(``python tests/test_queue_append_cas.py``) — bare pytest may lack the schema.
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'cas-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now, 'updated_at': now, 'search_text': '',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'search_text'], retry=True)
    db.commit()


def _read(db, conv_id):
    row = db.execute('SELECT messages, msg_count FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return msgs, row[1]


def _cleanup(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()


def test_append_lands_normally():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.message_queue import _append_user_msg_with_cas
    conv_id = 'cv-cas-normal'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [{'role': 'user', 'content': 'first', 'timestamp': 1}])
    try:
        ok = _append_user_msg_with_cas(db, conv_id, {'role': 'user', 'content': 'queued', 'timestamp': 2})
        assert ok, 'helper returned False'
        msgs, count = _read(db, conv_id)
        assert [m['content'] for m in msgs] == ['first', 'queued'], msgs
        assert count == 2, count
    finally:
        _cleanup(db, conv_id)
    _ok('plain append lands + bumps msg_count')


def test_append_survives_concurrent_writer():
    """A concurrent writer bumps updated_at + appends its own message between
    the helper's SELECT and its UPDATE. The CAS must miss, retry against the
    fresh tail, and land the queued message WITHOUT dropping the concurrent
    writer's message."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.message_queue import _append_user_msg_with_cas
    conv_id = 'cv-cas-race'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [{'role': 'user', 'content': 'first', 'timestamp': 1}])

    # Wrap db.execute: the FIRST time the helper issues its CAS UPDATE, a
    # concurrent writer sneaks in (bumps updated_at + appends its OWN message)
    # so the helper's CAS misses and it must retry.
    orig_execute = db.execute
    state = {'raced': False}

    def _racing_execute(sql, params=()):
        if (not state['raced']) and sql.strip().upper().startswith('UPDATE CONVERSATIONS SET MESSAGES'):
            state['raced'] = True
            # Concurrent writer: read fresh, append its own msg, bump updated_at.
            row = orig_execute('SELECT messages, updated_at FROM conversations WHERE id=? AND user_id=1',
                               (conv_id,)).fetchone()
            cur_msgs = _json.loads(row[0] or '[]')
            cur_msgs.append({'role': 'assistant', 'content': 'CONCURRENT', 'timestamp': 99})
            orig_execute('UPDATE conversations SET messages=?, updated_at=?, msg_count=? WHERE id=? AND user_id=1',
                         (json_dumps_pg(cur_msgs), int(time.time() * 1000) + 5, len(cur_msgs), conv_id))
            db.commit()
        return orig_execute(sql, params)

    db.execute = _racing_execute
    try:
        ok = _append_user_msg_with_cas(db, conv_id, {'role': 'user', 'content': 'queued', 'timestamp': 2})
        assert ok, 'helper returned False'
    finally:
        db.execute = orig_execute
    try:
        assert state['raced'], 'the race injection never fired (test bug)'
        msgs, count = _read(db, conv_id)
        contents = [m['content'] for m in msgs]
        # BOTH the concurrent writer's message AND the queued message survive.
        assert 'CONCURRENT' in contents, (
            f'concurrent writer message was CLOBBERED — CAS failed: {contents}')
        assert 'queued' in contents, f'queued message lost: {contents}'
        assert contents == ['first', 'CONCURRENT', 'queued'], contents
        assert count == 3, count
    finally:
        _cleanup(db, conv_id)
    _ok('append CAS-retries past a concurrent writer without clobbering it')


def main():
    print()
    print(_color('═══ queued-message append CAS tests ═══', '36'))
    print()
    tests = [test_append_lands_normally, test_append_survives_concurrent_writer]
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
    print(_color(f'═══ ALL {len(tests)} QUEUE-CAS TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_queue_append_cas.__main__')
    main()
