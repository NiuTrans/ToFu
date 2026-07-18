#!/usr/bin/env python3
"""Windowed-read mutation safety — the index-misalignment landmine.

Under windowed reads the client's ``conv.messages`` holds only a TAIL window
(plus any scrolled-up pages), so a local array index is NOT the absolute DB
index. Index-addressed mutation routes (delete / branch) would therefore hit
the WRONG absolute message. The fix: those routes resolve a stable ``_msgId``
to the current absolute index (``find_message_by_id``), index is only a
fallback.

Tests drive the REAL blocking handlers against a live DB:
  1. delete: a windowed client wants to delete absolute msg #97, but its local
     window index is 7 (the 8th of a 10-msg tail). Sending msgId=id97 with the
     WRONG index 7 deletes #97 (not #7). Proves stable-id addressing.
  2. delete_branch: same — msgId resolves the anchor's absolute index; a branch
     is removed from the CORRECT message, not the local-index one.
  3. NC: WITHOUT msgId (index-only), the wrong local index 7 deletes absolute
     #7 — proving the msgId path is load-bearing (this is exactly the data
     corruption that would occur if the mutation routes stayed index-only).
"""

import json as _json
import os
import sys
import time
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


def _seed(db, conv_id, messages):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'wm', 'messages': json_dumps_pg(messages),
        'msg_count': len(messages), 'created_at': now, 'updated_at': now, 'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _read_msgs(db, conv_id):
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    raw = row[0] if row else '[]'
    return _json.loads(raw) if isinstance(raw, str) else (raw or [])


def _cleanup(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db.commit()


def _big(n):
    m = []
    for i in range(n):
        m.append({'role': 'user', 'content': f'q{i}', '_msgId': f'id{i}', 'timestamp': i})
    return m


def test_delete_by_msgid_hits_absolute_not_local_index():
    rc = _rc()
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'wm-del-{uuid.uuid4().hex[:8]}'
    msgs = _big(100)  # absolute indices 0..99, _msgId id0..id99
    _seed(db, conv_id, msgs)
    try:
        # Windowed client: tail window of 10 = absolute 90..99. It wants to
        # delete absolute #97 (id97), whose LOCAL window index is 7 (wrong).
        result = rc._delete_message_blocking(db, conv_id, 7, 'single', msg_id='id97')
        remaining = _read_msgs(db, conv_id)
        ids = [m['_msgId'] for m in remaining]
        assert 'id97' not in ids, 'msgId target #97 was NOT deleted'
        assert 'id7' in ids, 'WRONG message #7 (local index) was deleted'
        assert len(remaining) == 99
        _ok('delete: msgId=id97 + wrong local index 7 → deleted absolute #97, spared #7')
    finally:
        _cleanup(db, conv_id)


def test_delete_branch_by_msgid_hits_absolute():
    rc = _rc()
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'wm-br-{uuid.uuid4().hex[:8]}'
    msgs = _big(100)
    # give absolute #97 two branches, #7 one branch (so a wrong hit is visible)
    msgs[97]['branches'] = [{'id': 'b97a', 'title': 'A'}, {'id': 'b97b', 'title': 'B'}]
    msgs[7]['branches'] = [{'id': 'b7', 'title': 'seven'}]
    _seed(db, conv_id, msgs)
    try:
        # delete branch 0 of absolute #97 using wrong local index 7 + msgId
        rc._delete_branch_blocking(db, conv_id, 7, 0, msg_id='id97')
        remaining = _read_msgs(db, conv_id)
        assert [b['id'] for b in remaining[97].get('branches', [])] == ['b97b'], \
            'branch not removed from absolute #97'
        assert [b['id'] for b in remaining[7].get('branches', [])] == ['b7'], \
            'WRONG message #7 branches were touched'
        _ok('delete_branch: msgId=id97 + wrong index 7 → removed branch from #97, spared #7')
    finally:
        _cleanup(db, conv_id)


def test_NC_index_only_deletes_wrong_message():
    """NC: WITHOUT msgId, the wrong local index deletes the wrong absolute
    message — the exact corruption the stable-id path prevents."""
    rc = _rc()
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    conv_id = f'wm-nc-{uuid.uuid4().hex[:8]}'
    msgs = _big(100)
    _seed(db, conv_id, msgs)
    try:
        # index-only (no msgId): local index 7 deletes absolute #7, NOT #97.
        rc._delete_message_blocking(db, conv_id, 7, 'single', msg_id=None)
        remaining = _read_msgs(db, conv_id)
        ids = [m['_msgId'] for m in remaining]
        assert 'id7' not in ids, 'NC sanity: index-only should have deleted #7'
        assert 'id97' in ids, 'NC sanity: #97 should be untouched by index-only'
        _ok('NC: index-only (no msgId) deletes WRONG msg #7 → proves msgId path is load-bearing')
    finally:
        _cleanup(db, conv_id)


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_windowed_mutation_stable_id.__main__')
    print(_color('\n=== windowed mutation stable-id safety ===', '1;36'))
    test_delete_by_msgid_hits_absolute_not_local_index()
    test_delete_branch_by_msgid_hits_absolute()
    test_NC_index_only_deletes_wrong_message()
    print(_color('\nALL PASSED', '1;32'))
