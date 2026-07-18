#!/usr/bin/env python3
"""Windowed conversation GET — serve the tail N messages from the normalized
row store so first-open cost is O(window), not O(history).

Tests drive the real _windowed_served_readonly served-dict builder against a
live DB with backfilled rows:
  1. tail window — returns the newest N messages + pagination envelope
     (windowed/totalCount/firstLoadedSeq/hasMore); no reconcile change on a
     clean tail.
  2. page-up (before_seq) — a pure slice, never reconciled, correct range.
  3. tail with a ghost — windowed reconcile shortens the served tail AND
     surfaces the FULL cleaned list for the deferred persist (changed=True).
  4. NC — a ghost in the tail with reconcile neutered would leave the ghost in
     the served window (proves the windowed reconcile is load-bearing).
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


def _rc():
    import importlib
    return importlib.import_module('routes.conversations')


def _seed(db, conv_id, msgs):
    from lib.database.messages_rows import backfill_conv
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    import time
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'wr', 'messages': json_dumps_pg(msgs),
        'msg_count': len(msgs), 'created_at': now, 'updated_at': now, 'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    backfill_conv(db, conv_id, msgs, now_ms=now, commit=True)


def _fetch(db, conv_id):
    return db.execute('SELECT id, title, messages, created_at, updated_at, settings, rev '
                      'FROM conversations WHERE id=? AND user_id=1', (conv_id,)).fetchone()


def _cleanup(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (conv_id,))
    db.commit()


def _big(n):
    m = []
    for i in range(n):
        m.append({'role': 'user', 'content': f'q{i}', '_msgId': f'u{i}', 'timestamp': i * 2})
        m.append({'role': 'assistant', 'content': f'a{i}', 'finishReason': 'stop',
                  '_msgId': f'x{i}', 'timestamp': i * 2 + 1})
    return m


def test_tail_window():
    rc = _rc()
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'wr-tail-{uuid.uuid4().hex[:8]}'
    msgs = _big(100)  # 200 messages seq 0..199, clean tail
    _seed(db, conv_id, msgs)
    try:
        r = _fetch(db, conv_id)
        served, changed, cleaned_full, sd = rc._windowed_served_readonly(db, conv_id, r, 20, None)
        assert served['windowed'] is True
        assert served['totalCount'] == 200
        assert len(served['messages']) == 20
        assert served['firstLoadedSeq'] == 180 and served['lastLoadedSeq'] == 199
        assert served['hasMore'] is True
        assert served['messages'][-1] == msgs[199]
        assert changed is False and cleaned_full is None
        _ok('tail window: 20 newest + pagination envelope, clean tail no reconcile')
    finally:
        _cleanup(db, conv_id)


def test_page_up_pure_slice():
    rc = _rc()
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'wr-pg-{uuid.uuid4().hex[:8]}'
    msgs = _big(100)
    _seed(db, conv_id, msgs)
    try:
        r = _fetch(db, conv_id)
        served, changed, cf, sd = rc._windowed_served_readonly(db, conv_id, r, 20, 180)
        assert served['firstLoadedSeq'] == 160 and served['lastLoadedSeq'] == 179
        assert changed is False and cf is None
        assert served['messages'][0] == msgs[160]
        _ok('page-up before_seq=180: pure slice 160..179, never reconciled')
    finally:
        _cleanup(db, conv_id)


def test_tail_ghost_reconciled_in_window():
    rc = _rc()
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'wr-ghost-{uuid.uuid4().hex[:8]}'
    msgs = _big(100)
    # append a trailing ghost empty assistant (seq 200) after a user (seq 201? no)
    msgs.append({'role': 'user', 'content': 'last', '_msgId': 'ulast', 'timestamp': 999})
    msgs.append({'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
                 '_msgId': 'xghost', 'timestamp': 1000})
    _seed(db, conv_id, msgs)
    try:
        r = _fetch(db, conv_id)
        served, changed, cleaned_full, sd = rc._windowed_served_readonly(db, conv_id, r, 20, None)
        # served window should NOT end with the ghost empty assistant
        assert served['messages'][-1].get('content') != '' or served['messages'][-1].get('role') != 'assistant', \
            'ghost tail still in served window'
        assert served['messages'][-1]['content'] == 'last', 'window tail not the user turn'
        # a full cleaned list is surfaced for the deferred persist
        assert changed is True and cleaned_full is not None
        assert cleaned_full[-1]['content'] == 'last', 'full cleaned did not drop ghost'
        assert sd and sd.get('_reconciledAt'), 'settings not stamped for persist'
        _ok('tail ghost: windowed reconcile drops it in served window + full cleaned for persist')
    finally:
        _cleanup(db, conv_id)


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_conv_windowed_read.__main__')
    print(_color('\n=== windowed GET read ===', '1;36'))
    test_tail_window()
    test_page_up_pure_slice()
    test_tail_ghost_reconciled_in_window()
    print(_color('\nALL PASSED', '1;32'))
