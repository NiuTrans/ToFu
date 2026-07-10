#!/usr/bin/env python3
"""Regression: _sync_partial_to_conversation must NOT rebuild search_text / FTS.

WHY (the perf fundamental fixed)
--------------------------------
_sync_partial_to_conversation runs every ``_STREAM_CHECKPOINT_INTERVAL`` (5s)
for the whole duration of an active stream. It used to rebuild the full
``search_text`` (build_search_text walks EVERY message) and DELETE+INSERT the
SQLite FTS row on every checkpoint — cost that scales with the whole
conversation, not the streamed delta, repeated ~every 5s. That is pure waste:
the terminal ``_sync_result_to_conversation`` ALWAYS rebuilds search_text from
the final messages, so a mid-stream search_text is superseded on completion.
Indexing a not-yet-final tail also makes mid-stream search hits point at
content that isn't settled.

The fix writes only the reload-critical columns on partials
(messages/updated_at/msg_count) and leaves search indexing to the terminal
sync.

Tests (drive the REAL shipped functions against a real DB):
  1. ``test_partial_does_not_touch_search_text`` — seed a conv with a stale
     search_text sentinel, run a partial checkpoint that GROWS the tail
     content → messages advance, msg_count advances, but search_text is
     UNCHANGED (the partial did not reindex the fresh content).
     Double-neuter: monkeypatch build_search_text to raise; the partial must
     STILL succeed (proving it no longer calls build_search_text on this path).
  2. ``test_terminal_sync_rebuilds_search_text`` — the terminal sync DOES
     rebuild search_text so search still indexes the settled content
     (behaviour preservation — the fix only moved indexing, didn't remove it).

Env note (see project memory): DB-backed tests warm the schema when run
DIRECTLY (``python tests/test_partial_checkpoint_no_search_text.py``); under a
bare ``pytest`` invocation the conversations table may be absent. Run directly.
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


def _seed_conv(db, conv_id, messages, search_text):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'partial-ckpt-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
        'search_text': search_text,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'search_text'], retry=True)
    db.commit()


def _read(db, conv_id):
    row = db.execute(
        'SELECT messages, search_text, msg_count FROM conversations WHERE id=? AND user_id=1',
        (conv_id,)).fetchone()
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return msgs, (row[1] or ''), row[2]


def _cleanup(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def test_partial_does_not_touch_search_text():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager as _mgr

    conv_id = 'cv-partial-st'
    db = get_thread_db(DOMAIN_CHAT)
    # A conv whose trailing assistant is a live placeholder; search_text carries
    # a sentinel that no message content matches.
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'question', 'timestamp': 1},
        {'role': 'assistant', 'content': 'par', 'thinking': '', 'timestamp': 2},
    ], search_text='__STALE_SENTINEL__')

    # A task whose content GROWS the tail (len('partial answer so far') > len('par')).
    task = {
        'id': 'tk-partial-st', 'convId': conv_id,
        'content': 'partial answer so far', 'thinking': '',
    }
    try:
        _mgr._sync_partial_to_conversation(task)
        msgs, search_text, msg_count = _read(db, conv_id)
        # messages column DID advance (reload-critical write happened).
        assert msgs[-1]['content'] == 'partial answer so far', (
            f'partial checkpoint did not grow the tail content: {msgs[-1]!r}')
        assert msg_count == 2, f'msg_count wrong: {msg_count}'
        # search_text was NOT rebuilt — still the pre-existing sentinel.
        assert search_text == '__STALE_SENTINEL__', (
            'partial checkpoint rebuilt search_text — it must leave indexing to '
            f'the terminal sync (got {search_text!r})')

        # ── DOUBLE-NEUTER: build_search_text must NOT be called on this path.
        #    If the fix regressed (partial calls build_search_text again), this
        #    exploding stub would blow up the checkpoint.
        import lib.conversations as _conv
        _orig = _conv.build_search_text

        def _boom(*_a, **_k):
            raise AssertionError('build_search_text called on the PARTIAL path')
        _conv.build_search_text = _boom
        try:
            task2 = dict(task, content='partial answer so far and then some more')
            _mgr._sync_partial_to_conversation(task2)  # must NOT raise
            msgs2, st2, _ = _read(db, conv_id)
            assert msgs2[-1]['content'].endswith('more'), 'second partial did not write'
            assert st2 == '__STALE_SENTINEL__', 'search_text changed on second partial'
        finally:
            _conv.build_search_text = _orig
    finally:
        _cleanup(db, conv_id)
    _ok('partial checkpoint writes messages but never rebuilds search_text/FTS (build_search_text not called)')


def test_terminal_sync_rebuilds_search_text():
    """Behaviour preservation: the terminal sync still indexes settled content
    so search is not broken — the fix only moved indexing off the hot partial
    path, it did not remove it."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager as _mgr

    conv_id = 'cv-terminal-st'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'question', 'timestamp': 1},
        {'role': 'assistant', 'content': 'par', 'thinking': '', 'timestamp': 2},
    ], search_text='__STALE_SENTINEL__')

    task = {
        'id': 'tk-terminal-st', 'convId': conv_id,
        'content': 'THE FINAL SETTLED ANSWER', 'thinking': '',
        'finishReason': 'stop',
    }
    meta = {'finishReason': 'stop'}
    try:
        _mgr._sync_result_to_conversation(task, meta)
        _msgs, search_text, _ = _read(db, conv_id)
        assert 'THE FINAL SETTLED ANSWER' in search_text, (
            'terminal sync did not index the settled content — search would '
            f'miss it (search_text={search_text!r})')
        assert '__STALE_SENTINEL__' not in search_text, (
            'terminal sync left the stale sentinel — it must rebuild from messages')
    finally:
        _cleanup(db, conv_id)
    _ok('terminal sync rebuilds search_text from settled content (indexing preserved)')


def main():
    print()
    print(_color('═══ partial-checkpoint search_text / FTS skip tests ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_partial_checkpoint_no_search_text.__main__')
    tests = [test_partial_does_not_touch_search_text,
             test_terminal_sync_rebuilds_search_text]
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
    print(_color(f'═══ ALL {len(tests)} PARTIAL-CHECKPOINT TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
