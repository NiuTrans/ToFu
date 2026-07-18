#!/usr/bin/env python3
"""load_message_window — tail-windowed read from conversation_messages rows.

Root-cause fix for slow first-open of long conversations: serve the GET from
the normalized row store, windowed to the tail, so cost is constant-in-window
instead of linear-in-history.

Tests (drive the real function against a live DB):
  1. tail window — the newest N messages, ascending, with correct
     totalCount / firstLoadedSeq / lastLoadedSeq / hasMore.
  2. page-up (before_seq) — the N messages ending just before a seq.
  3. window >= total — returns everything, hasMore False.
  4. limit<=0 — whole conversation.
  5. lossless — windowed messages are byte-identical to the original tail slice.
  6. empty conv — safe zeros.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(m): print(' ', _color('✓', '32'), m)


def _seed(db, conv_id, n):
    from lib.database.messages_rows import backfill_conv
    msgs = []
    for i in range(n):
        msgs.append({'role': 'user', 'content': f'q{i}', '_msgId': f'u{i}', 'timestamp': i * 2})
        msgs.append({'role': 'assistant', 'content': f'a{i}', 'thinking': f't{i}',
                     '_msgId': f'x{i}', 'finishReason': 'stop', 'timestamp': i * 2 + 1})
    backfill_conv(db, conv_id, msgs, now_ms=1, commit=True)
    return msgs


def _cleanup(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
    db.commit()


def test_window():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database.messages_rows import load_message_window
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'win-{uuid.uuid4().hex[:8]}'
    msgs = _seed(db, conv_id, 50)   # 100 messages, seq 0..99
    try:
        # 1. tail window of 10
        w = load_message_window(db, conv_id, limit=10)
        assert w['totalCount'] == 100, w['totalCount']
        assert len(w['messages']) == 10
        assert w['firstLoadedSeq'] == 90 and w['lastLoadedSeq'] == 99, (w['firstLoadedSeq'], w['lastLoadedSeq'])
        assert w['hasMore'] is True
        # ascending order + correct tail content
        assert w['messages'][0] == msgs[90] and w['messages'][-1] == msgs[99]
        _ok('tail window: newest 10 ascending, total=100, hasMore, byte-identical')

        # 2. page up before seq 90 → seq 80..89
        p = load_message_window(db, conv_id, limit=10, before_seq=90)
        assert len(p['messages']) == 10
        assert p['firstLoadedSeq'] == 80 and p['lastLoadedSeq'] == 89
        assert p['hasMore'] is True
        assert p['messages'][0] == msgs[80] and p['messages'][-1] == msgs[89]
        _ok('page-up before_seq=90: seq 80..89, hasMore')

        # page up to the very top: before seq 10 with window 20 → seq 0..9, hasMore False
        top = load_message_window(db, conv_id, limit=20, before_seq=10)
        assert top['firstLoadedSeq'] == 0 and top['lastLoadedSeq'] == 9
        assert top['hasMore'] is False
        _ok('page-up to top: seq 0..9, hasMore False')

        # 3. window >= total → everything, hasMore False
        allw = load_message_window(db, conv_id, limit=1000)
        assert len(allw['messages']) == 100 and allw['firstLoadedSeq'] == 0
        assert allw['hasMore'] is False
        _ok('window>=total: all 100, hasMore False')

        # 4. limit<=0 → whole conv
        z = load_message_window(db, conv_id, limit=0)
        assert len(z['messages']) == 100 and z['hasMore'] is False
        _ok('limit<=0: whole conversation')
    finally:
        _cleanup(db, conv_id)


def test_empty_conv():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database.messages_rows import load_message_window
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'win-empty-{uuid.uuid4().hex[:8]}'
    w = load_message_window(db, conv_id, limit=10)
    assert w['totalCount'] == 0 and w['messages'] == []
    assert w['firstLoadedSeq'] is None and w['hasMore'] is False
    _ok('empty conv: safe zeros')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_messages_window.__main__')
    print(_color('\n=== load_message_window ===', '1;36'))
    test_window()
    test_empty_conv()
    print(_color('\nALL PASSED', '1;32'))
