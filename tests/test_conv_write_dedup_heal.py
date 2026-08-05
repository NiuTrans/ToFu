#!/usr/bin/env python3
"""Durable one-time heal for race-planted duplicate user rows — write seams.

WHY (epic pt_99eeedbd40424fe6)
------------------------------
The send-path auto-translate race (optimistic frontend copy + server-built
copy sharing one ``timestamp``) plants TWO user rows for one logical turn.
``append_user_msg_idempotent`` guards the send route, but a full-conv PUT
(``_save_conv_blocking``) or any historical row predating it BYPASSES that
contract — and then the row lives in the DB forever, and every context
rebuild re-heals it in memory (``_dedup_duplicate_user_messages`` in the
message builder), forever. That is exactly the anti-pattern the owner called
out: hold incorrect content, re-deduplicate it on every read.

THE FIX THIS SUITE PINS
-----------------------
Both write seams heal the pair ONCE, durably, before persisting — the same
pure verdict the rebuild side already uses (consecutive user rows sharing a
``timestamp`` → keep the LAST, the server-built copy):

  1. ``routes.conversations._save_conv_blocking`` (the full-conv PUT — the
     seam that can re-plant the pair) sweeps its incoming payload, mirroring
     the ghost-husk sweep precedent.
  2. ``lib.chat.persistence.persist_conv_messages`` (send / regenerate /
     edit / continue writes) sweeps before assigning ids and writing.

CONTRACT
--------
  * Consecutive same-``timestamp`` user rows → the LAST (server copy, with
    its translation fields) survives; the row is healed IN THE DB so future
    rebuilds see zero duplicates.
  * Distinct timestamps → BOTH rows kept (no over-kill).
  * Same timestamp but NOT consecutive (separated by an assistant row) →
    out of contract scope, BOTH kept (branch/edit flows stay untouched).

NEUTER (standalone main): short-circuiting each heal call flips its test back
to "duplicate persisted", proving each seam is load-bearing; the distinct-
timestamp control keeps passing (no over-kill blast radius).

Standalone runner (real sqlite DB); also importable as pytest functions.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_THIS = os.path.abspath(__file__)
_ROOT = os.path.dirname(os.path.dirname(_THIS))
_TARGET_PUT = os.path.join(_ROOT, 'routes', 'conversations.py')
_TARGET_PERSIST = os.path.join(_ROOT, 'lib', 'chat', 'persistence.py')

_TS = 1783600000000


def _ok(msg): print(' ', '\033[32m✓\033[0m', msg)


# ── message builders ──
def _real(role, i):
    m = {'role': role, 'content': f'{role}-content-{i}', 'thinking': '',
         'toolRounds': [], 'timestamp': _TS + 1000 + i, '_msgId': f'msg-{i}'}
    if role == 'assistant':
        m['finishReason'] = 'stop'
    return m


def _clean_body(n):
    msgs = []
    for i in range(n - 1):
        msgs.append(_real('user' if i % 2 == 0 else 'assistant', i))
    msgs.append(_real('assistant', n - 1))
    return msgs


def _dup_pair():
    """The race shape: optimistic copy first, server-built copy second —
    SAME timestamp, consecutive."""
    return [
        {'role': 'user', 'content': '帮我看看这段代码（乐观副本）',
         'timestamp': _TS, '_msgId': 'm-opt'},
        {'role': 'user', 'content': 'help me review this code (server copy)',
         'timestamp': _TS, '_msgId': 'm-srv', '_translateDone': True,
         'originalContent': '帮我看看这段代码'},
    ]


def _seed(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'dedup-heal-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now, 'updated_at': now, 'settings': '{}', 'search_text': '',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings', 'search_text'],
        retry=True)
    db.commit()


def _read(db, conv_id):
    import json
    row = db.execute('SELECT messages, msg_count FROM conversations '
                     'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
    raw = row[0] if not isinstance(row, dict) else row['messages']
    msgs = json.loads(raw) if isinstance(raw, str) else raw
    mc = row[1] if not isinstance(row, dict) else row['msg_count']
    return msgs, mc


def _cleanup(db, *ids):
    from lib.database import db_execute_with_retry
    for cid in ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _defer_status(defer):
    from lib.api_response import api_ok
    status = getattr(defer, 'status', None)
    if defer.helper is api_ok:
        payload = {'ok': True}
        payload.update(defer.kwargs)
        if defer.args and isinstance(defer.args[0], dict):
            payload.update(defer.args[0])
        return status or 200, payload
    payload = defer.args[0] if defer.args else {}
    return status or 200, payload


def _put(db, conv_id, messages, **extra):
    from routes.conversations import _save_conv_blocking
    data = {'title': 'dedup-heal-test', 'messages': messages}
    data.update(extra)
    return _defer_status(_save_conv_blocking(db, conv_id, data))


def _tail_user_dup_count(msgs):
    """Count consecutive same-timestamp user→user adjacencies anywhere."""
    n = 0
    for i in range(1, len(msgs)):
        a, b = msgs[i - 1], msgs[i]
        if (a.get('role') == 'user' and b.get('role') == 'user'
                and a.get('timestamp') is not None
                and a.get('timestamp') == b.get('timestamp')):
            n += 1
    return n


# ───────────────────────── positive tests ─────────────────────────

def test_put_heals_same_timestamp_user_dup():
    """PUT seam: payload ends [assistant, user(optimistic), user(server)] —
    the race pair. Must persist with ONE user row: the server copy (last
    wins, translation fields kept)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-dedup-put'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _clean_body(4))
    try:
        payload = _clean_body(4) + _dup_pair()          # 4 + 2 = 6 rows
        st, pl = _put(db, conv_id, payload)
        assert st == 200, f'growth PUT must succeed, got {st}: {pl}'
        msgs, mc = _read(db, conv_id)
        assert len(msgs) == 5 and mc == 5, (
            f'race pair NOT healed on the PUT seam — persisted {len(msgs)} '
            f'(mc={mc}), expected 5')
        tail = msgs[-1]
        assert tail['role'] == 'user' and tail.get('_translateDone'), (
            f'the surviving copy must be the server-built one, got {tail}')
        assert tail.get('originalContent') == '帮我看看这段代码'
        assert _tail_user_dup_count(msgs) == 0
    finally:
        _cleanup(db, conv_id)
    _ok('PUT seam: same-timestamp user dup healed durably (server copy wins)')


def test_put_keeps_distinct_timestamp_user_rows():
    """Control / no-over-kill: two consecutive user rows with DIFFERENT
    timestamps are two logical turns (e.g. an error ghost dropped between
    them in an older copy) — BOTH must survive."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-dedup-distinct'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _clean_body(4))
    try:
        payload = _clean_body(4) + [
            {'role': 'user', 'content': '第一句', 'timestamp': _TS + 1,
             '_msgId': 'm-a'},
            {'role': 'user', 'content': '第二句', 'timestamp': _TS + 2,
             '_msgId': 'm-b'},
        ]
        st, pl = _put(db, conv_id, payload)
        assert st == 200, f'PUT must succeed, got {st}: {pl}'
        msgs, mc = _read(db, conv_id)
        assert len(msgs) == 6 and mc == 6, (
            f'distinct-timestamp user rows were wrongly collapsed '
            f'(persisted {len(msgs)}, expected 6) — over-kill!')
        assert msgs[-2]['content'] == '第一句' and msgs[-1]['content'] == '第二句'
    finally:
        _cleanup(db, conv_id)
    _ok('PUT seam: distinct-timestamp consecutive user rows both kept (no over-kill)')


def test_put_keeps_nonconsecutive_same_timestamp():
    """Contract scope: same timestamp but NOT consecutive (an assistant row
    sits between) is out of scope — branch/edit flows must stay untouched."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-dedup-nonconsec'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _clean_body(2))
    try:
        payload = [
            {'role': 'user', 'content': '原句', 'timestamp': _TS, '_msgId': 'm-1'},
            _real('assistant', 1),
            {'role': 'user', 'content': '编辑后重发', 'timestamp': _TS,
             '_msgId': 'm-2'},
            _real('assistant', 3),
        ]
        st, pl = _put(db, conv_id, payload)
        assert st == 200, f'PUT must succeed, got {st}: {pl}'
        msgs, mc = _read(db, conv_id)
        assert len(msgs) == 4, (
            f'non-consecutive same-timestamp rows wrongly collapsed '
            f'(persisted {len(msgs)}, expected 4)')
    finally:
        _cleanup(db, conv_id)
    _ok('PUT seam: non-consecutive same-timestamp rows untouched (contract scope)')


def test_persist_conv_messages_heals_dup():
    """persist_conv_messages (send/regenerate/edit/continue write path): the
    array handed to it carrying the race pair is healed BEFORE the write —
    the DB never holds the duplicate."""
    from lib.chat.persistence import persist_conv_messages
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-dedup-persist'
    db = get_thread_db(DOMAIN_CHAT)
    try:
        msgs = _clean_body(2) + _dup_pair()
        persist_conv_messages(db, conv_id, msgs, 'dedup-heal-test')
        got, mc = _read(db, conv_id)
        assert len(got) == 3 and mc == 3, (
            f'race pair NOT healed by persist_conv_messages — DB holds '
            f'{len(got)} (mc={mc}), expected 3')
        tail = got[-1]
        assert tail['role'] == 'user' and tail.get('_translateDone'), (
            f'the surviving copy must be the server-built one, got {tail}')
        assert _tail_user_dup_count(got) == 0
    finally:
        _cleanup(db, conv_id)
    _ok('persist_conv_messages: race pair healed before write (durable)')


_POSITIVE = [
    test_put_heals_same_timestamp_user_dup,
    test_put_keeps_distinct_timestamp_user_rows,
    test_put_keeps_nonconsecutive_same_timestamp,
    test_persist_conv_messages_heals_dup,
]


def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', f'\033[31m✗\033[0m {fn.__name__}: {e}')
        return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(' ', f'\033[31m✗\033[0m {fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


# ───────────────────────── on-disk-free neuters ─────────────────────────

# PUT seam: skip the heal by forcing the healed list back to the raw one.
# ★ Anchor at the FULL 8-space indent (the line lives inside the ``try``) —
# a 4-space anchor would match as a SUBSTRING of the 8-space line and splice
# a dedented NC line into the try body (SyntaxError), not a neuter.
_NC_PUT_FIND = '        _healed = _dedup_duplicate_user_messages(raw_messages)\n'
_NC_PUT_REPL = ('        _healed = _dedup_duplicate_user_messages(raw_messages)\n'
                '        _healed = raw_messages  # NC: neuter the PUT write-seam heal\n')

# persist seam: same idea on lib/chat/persistence.py.
_NC_PERSIST_FIND = '    _healed = _dedup_duplicate_user_messages(messages)\n'
_NC_PERSIST_REPL = ('    _healed = _dedup_duplicate_user_messages(messages)\n'
                    '    _healed = messages  # NC: neuter the persist heal\n')


def main():
    print()
    print('\033[36m═══ write-seam duplicate-user heal + neuters ═══\033[0m')
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_conv_write_dedup_heal.__main__')

    print('\033[36mBaseline (shipped heals):\033[0m')
    if not all([_run(fn) for fn in _POSITIVE]):
        sys.exit('baseline failed — fix the heal seams before neutering')

    print()
    print('\033[36mNC-1 — PUT seam heal is load-bearing:\033[0m')
    from tests._nc_harness import neutered_source
    with neutered_source(_TARGET_PUT, _NC_PUT_FIND, _NC_PUT_REPL):
        dup_ok = _run(test_put_heals_same_timestamp_user_dup)
        distinct_ok = _run(test_put_keeps_distinct_timestamp_user_rows)
    if dup_ok:
        sys.exit('NC-1: PUT dup test PASSED with the heal neutered — seam not load-bearing!')
    if not distinct_ok:
        sys.exit('NC-1: distinct-timestamp control failed — neuter had unintended blast radius')
    print(' ', '\033[32m✓\033[0m NC-1: PUT heal OFF → dup persists (test fails); control passes')

    print()
    print('\033[36mNC-2 — persist_conv_messages heal is load-bearing:\033[0m')
    with neutered_source(_TARGET_PERSIST, _NC_PERSIST_FIND, _NC_PERSIST_REPL):
        persist_ok = _run(test_persist_conv_messages_heals_dup)
    if persist_ok:
        sys.exit('NC-2: persist heal test PASSED with the heal neutered — seam not load-bearing!')
    print(' ', '\033[32m✓\033[0m NC-2: persist heal OFF → dup persists (test fails)')

    print()
    print('\033[36mPost-restore baseline:\033[0m')
    if not all([_run(fn) for fn in _POSITIVE]):
        sys.exit('post-restore baseline failed — module not restored correctly')
    print()
    print('\033[32m═══ ALL WRITE-SEAM HEAL TESTS + NEUTERS PASSED ═══\033[0m')
    print()


if __name__ == '__main__':
    main()
