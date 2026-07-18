#!/usr/bin/env python3
"""DELETE /conversations/<id>/messages/<idx> must resolve the target by stable
``_msgId`` (index is only the fallback) — so a client index that has DRIFTED
from the persisted list (e.g. a server-side ghost-sweep / reconcile shrank the
messages between the client's read and the request) still deletes the RIGHT
message instead of misfiring / 400-ing.

This is the delete-side twin of chat_regenerate's ``truncateToMsgId`` drift
correction (routes/chat.py). Without it, the reported "deletion often fails"
bug happens: the client sends a now-out-of-range index → 400, or an index that
points at the wrong turn → the wrong message is deleted.

Asserts, against a real seeded conversation and the REAL
``_delete_message_blocking`` body:
  * msgId present + STALE index → resolves to the msg's CURRENT index (single);
  * msgId present + STALE index + mode='turn' on a user msg → deletes the
    user + following assistant at the RESOLVED position;
  * msgId absent (older client) → falls back to the supplied index unchanged;
  * msgId that does NOT resolve → falls back to the index unchanged.

NEUTER (in-process monkeypatch of the resolver): force find_message_by_id to
return (None, None) so the by-id path is dead → the stale-index case deletes
the WRONG message → the positive assertion fails. Proves the resolution is
load-bearing.

Standalone runner (real DB, mirrors tests/test_conv_rev_monotonic.py); also
importable as pytest test functions.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed(db, conv_id, messages):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'del-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms, 'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _messages(db, conv_id):
    import json
    r = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                   (conv_id,)).fetchone()
    raw = r[0] if not isinstance(r, dict) else r['messages']
    return json.loads(raw or '[]')


def _cleanup(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _M():
    """A 4-message conv with stable ids; return (messages, id_map)."""
    msgs = [
        {'role': 'user', 'content': 'q0', '_msgId': 'm0', 'timestamp': 0},
        {'role': 'assistant', 'content': 'a0', '_msgId': 'm1', 'timestamp': 1},
        {'role': 'user', 'content': 'q1', '_msgId': 'm2', 'timestamp': 2},
        {'role': 'assistant', 'content': 'a1', '_msgId': 'm3', 'timestamp': 3},
    ]
    return msgs


def test_delete_by_msgid_corrects_stale_index_single():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from routes.conversations import _delete_message_blocking
    conv_id = 'cv-del-drift'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M())
    try:
        # Client thinks 'q1' (m2) is at index 3 (its list was 1 longer), but it
        # is really at index 2. msgId must win → q1 deleted, others intact.
        res = _delete_message_blocking(db, conv_id, 3, 'single', 'm2')
        assert res is not None
        left = _messages(db, conv_id)
        ids = [m['_msgId'] for m in left]
        assert ids == ['m0', 'm1', 'm3'], f'wrong message deleted: remaining={ids}'
    finally:
        _cleanup(db, conv_id)
    _ok('single delete resolves stale index by msgId → correct message removed')


def test_delete_by_msgid_turn_mode_at_resolved_position():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from routes.conversations import _delete_message_blocking
    conv_id = 'cv-del-turn'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M())
    try:
        # 'q1' (m2, user) at real idx 2; client sent stale idx 0. Turn mode must
        # delete m2 + the following assistant m3 at the RESOLVED position.
        res = _delete_message_blocking(db, conv_id, 0, 'turn', 'm2')
        assert res is not None
        left = _messages(db, conv_id)
        ids = [m['_msgId'] for m in left]
        assert ids == ['m0', 'm1'], f'turn delete removed wrong pair: remaining={ids}'
    finally:
        _cleanup(db, conv_id)
    _ok('turn delete resolves by msgId → removes user+assistant at real position')


def test_delete_without_msgid_uses_index():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from routes.conversations import _delete_message_blocking
    conv_id = 'cv-del-noid'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M())
    try:
        # No msgId (older client) → index is authoritative. idx 1 = m1.
        res = _delete_message_blocking(db, conv_id, 1, 'single', None)
        assert res is not None
        ids = [m['_msgId'] for m in _messages(db, conv_id)]
        assert ids == ['m0', 'm2', 'm3'], f'index-path deleted wrong msg: {ids}'
    finally:
        _cleanup(db, conv_id)
    _ok('absent msgId → index fallback deletes exactly the indexed message')


def test_delete_unresolved_msgid_falls_back_to_index():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from routes.conversations import _delete_message_blocking
    conv_id = 'cv-del-badid'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M())
    try:
        # msgId doesn't exist (message since deleted elsewhere) → fall back to
        # the supplied index unchanged (strictly additive behaviour). idx 0=m0.
        res = _delete_message_blocking(db, conv_id, 0, 'single', 'ghost-id')
        assert res is not None
        ids = [m['_msgId'] for m in _messages(db, conv_id)]
        assert ids == ['m1', 'm2', 'm3'], f'unresolved-id fallback wrong: {ids}'
    finally:
        _cleanup(db, conv_id)
    _ok('unresolved msgId → falls back to the supplied index unchanged')


_POSITIVE = [
    test_delete_by_msgid_corrects_stale_index_single,
    test_delete_by_msgid_turn_mode_at_resolved_position,
    test_delete_without_msgid_uses_index,
    test_delete_unresolved_msgid_falls_back_to_index,
]


def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception:
        import traceback
        traceback.print_exc()
        return False


def _neuter_and_subrun():
    """NC: monkeypatch find_message_by_id → (None, None) so the by-id resolver
    is dead. The stale-index single-delete then removes the WRONG message,
    failing the positive expectation → proves the msgId resolution is
    load-bearing. Restores the original afterwards.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr
    from routes.conversations import _delete_message_blocking
    conv_id = 'cv-del-nc'
    db = get_thread_db(DOMAIN_CHAT)
    _orig = mgr.find_message_by_id
    _seed(db, conv_id, _M())
    try:
        mgr.find_message_by_id = lambda messages, msg_id: (None, None)
        # Same call as the positive single test: stale idx 3 + msgId m2. With
        # the resolver neutered it falls through to idx 3 → deletes m3, NOT m2.
        _delete_message_blocking(db, conv_id, 3, 'single', 'm2')
        ids = [m['_msgId'] for m in _messages(db, conv_id)]
        # Expectation of the FIXED code was ['m0','m1','m3']; neutered gives
        # ['m0','m1','m2'] (deleted m3). Confirm the resolver mattered.
        wrong = (ids == ['m0', 'm1', 'm2'])
        return wrong, f'remaining={ids} (neutered should delete m3, not m2)'
    finally:
        mgr.find_message_by_id = _orig
        _cleanup(db, conv_id)


def main():
    print()
    print(_color('═══ DELETE message by stable msgId — drift correction + neuter ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_delete_message_by_msgid.__main__')

    print(_color('Baseline (shipped resolver):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix the by-id delete resolution first')

    print()
    print(_color('NC — neuter find_message_by_id, repeat the stale-index delete:', '36'))
    wrong, out = _neuter_and_subrun()
    if not wrong:
        _fail('NC did not confirm the resolver is load-bearing:\n' + out)
    _ok('NC: with the resolver dead, the stale index deletes the WRONG msg (resolver is load-bearing)')

    print()
    print(_color('═══ ALL DELETE-BY-MSGID TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
