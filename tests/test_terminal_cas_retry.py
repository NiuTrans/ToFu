#!/usr/bin/env python3
"""Regression: the terminal ``_sync_result_to_conversation`` must RETRY on a
CAS miss instead of silently dropping the final turn.

WHY
---
On a poor network the SSE `done` frame may never reach the browser, so the
frontend's ``finishStream`` sync never fires — the backend is the ONLY writer
of the final answer into ``conversations.messages``. The terminal write is
guarded by an optimistic lock (``UPDATE ... WHERE updated_at=?``). The old code
did a SINGLE-SHOT CAS and, on a miss, logged *"frontend likely synced first
(safe)"* and returned — discarding the final content. That assumption is
exactly what a flaky network violates: a concurrent partial-checkpoint /
meta-cache write can bump ``updated_at`` while the frontend did NOT sync, so
the answer is lost from the conversation (survives only in ``task_results``).

The fix gives the terminal path the SAME bounded CAS-retry loop that
``_sync_partial_to_conversation`` already has: on a miss it re-reads the fresh
row, re-applies the SAME content-length guard (if the fresh row already holds
content >= ours → genuine frontend-won → skip), otherwise re-merges onto the
fresh tail and re-CASes (up to 3×).

Tests (drive the REAL shipped ``_sync_result_to_conversation``):
  1. ``test_terminal_cas_retry_persists_after_miss`` — a concurrent writer
     bumps ``updated_at`` between our read and our first UPDATE (forcing ONE
     CAS miss); the retry must re-read and STILL persist the full answer.
     ★ THE FIX. Double-neuter: revert the loop to a single-shot CAS and this
       FAILS (content lost).
  2. ``test_terminal_cas_skips_when_frontend_won`` — the fresh row already
     holds content LONGER than ours (a real frontend win); the retry must NOT
     shrink it. ★ BEHAVIOR PRESERVATION (the historical "safe skip").
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed_conv(db, conv_id, messages):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'cas-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_tail(db, conv_id):
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return msgs


def test_terminal_cas_retry_persists_after_miss():
    """A single CAS miss (concurrent updated_at bump, frontend did NOT sync
    fuller content) must be recovered by the retry — final answer persisted."""
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-cas-retry'
    db = get_thread_db(DOMAIN_CHAT)
    # In-flight state: trailing empty assistant placeholder (short content).
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'timestamp': 2},
    ])

    # Wrap db.execute so the FIRST terminal UPDATE misses the optimistic lock:
    # right before it runs, a "concurrent writer" bumps updated_at (WITHOUT
    # writing fuller content — the flaky-network case). One-shot: subsequent
    # UPDATEs proceed unmolested so the retry can win.
    real_execute = db.execute
    state = {'bumped': False}

    def _intercept(sql, params=()):
        if (not state['bumped']
                and isinstance(sql, str)
                and 'UPDATE conversations' in sql
                and 'SET messages' in sql):
            state['bumped'] = True
            # Concurrent bump: change updated_at so our CAS `WHERE updated_at=?`
            # fails. Content stays short (frontend did NOT win).
            real_execute(
                'UPDATE conversations SET updated_at=? WHERE id=? AND user_id=1',
                (int(time.time() * 1000) + 777, conv_id))
            db.commit()
        return real_execute(sql, params)

    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
        task['content'] = 'THE FULL FINAL ANSWER that must survive a CAS miss'
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']

        db.execute = _intercept
        try:
            _sync_result_to_conversation(task, {'finishReason': 'stop'})
        finally:
            db.execute = real_execute

        assert state['bumped'], 'interceptor never fired — test did not force a CAS miss'
        msgs = _read_tail(db, conv_id)
        assert len(msgs) == 2, f'expected 2 msgs, got {len(msgs)}'
        assert msgs[1]['role'] == 'assistant'
        assert msgs[1]['content'] == 'THE FULL FINAL ANSWER that must survive a CAS miss', (
            f'terminal write LOST after CAS miss — got {msgs[1]["content"]!r}. '
            'The retry loop must re-read and re-apply.')
        assert msgs[1].get('finishReason') == 'stop'
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db.execute = real_execute
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    _ok('terminal CAS miss is retried → final answer persisted (not silently dropped)')


def test_terminal_cas_skips_when_frontend_won():
    """Behaviour preservation: when a concurrent write installed content
    LONGER than the backend's, the retry must NOT shrink it (genuine
    frontend-won race → the historical safe skip)."""
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry, json_dumps_pg
    from lib.tasks_pkg.manager import (create_task, _sync_result_to_conversation,
                                        _conv_latest_task, _conv_latest_task_lock)

    conv_id = 'cv-cas-frontend-won'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'timestamp': 2},
    ])

    real_execute = db.execute
    state = {'bumped': False}
    FRONTEND_FULL = 'FRONTEND ALREADY SYNCED A MUCH LONGER AND COMPLETE ANSWER ' * 3

    def _intercept(sql, params=()):
        if (not state['bumped']
                and isinstance(sql, str)
                and 'UPDATE conversations' in sql
                and 'SET messages' in sql):
            state['bumped'] = True
            # Concurrent FRONTEND write: installs LONGER content + bumps updated_at.
            winner = [
                {'role': 'user', 'content': 'U1', 'timestamp': 1},
                {'role': 'assistant', 'content': FRONTEND_FULL, 'timestamp': 2},
            ]
            real_execute(
                'UPDATE conversations SET messages=?, updated_at=? WHERE id=? AND user_id=1',
                (json_dumps_pg(winner), int(time.time() * 1000) + 777, conv_id))
            db.commit()
        return real_execute(sql, params)

    try:
        task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
        task['content'] = 'shorter backend answer'   # SHORTER than the frontend's
        with _conv_latest_task_lock:
            _conv_latest_task[conv_id] = task['id']

        db.execute = _intercept
        try:
            _sync_result_to_conversation(task, {'finishReason': 'stop'})
        finally:
            db.execute = real_execute

        assert state['bumped'], 'interceptor never fired'
        msgs = _read_tail(db, conv_id)
        assert msgs[1]['content'] == FRONTEND_FULL, (
            'behaviour regression: the retry SHRANK a longer frontend-won write. '
            f'got {msgs[1]["content"][:40]!r}')
    finally:
        with _conv_latest_task_lock:
            _conv_latest_task.pop(conv_id, None)
        db.execute = real_execute
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    _ok('terminal CAS skips when frontend genuinely won (longer content preserved)')


def main():
    print()
    print(_color('═══ terminal _sync_result_to_conversation CAS-retry tests ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_terminal_cas_retry.__main__')
    tests = [
        test_terminal_cas_retry_persists_after_miss,
        test_terminal_cas_skips_when_frontend_won,
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
    print(_color(f'═══ ALL {len(tests)} CAS-RETRY TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
