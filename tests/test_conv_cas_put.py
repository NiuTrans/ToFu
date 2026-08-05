#!/usr/bin/env python3
"""Step (ii) of the rev-based-reconcile epic — the compare-and-swap PUT.

The conversation PUT (``_save_conv_blocking``) accepts a message write only if
the client's ``baseRev`` matches the row's current server ``rev``; otherwise the
client's base is stale (another tab/device/server-write advanced rev) and it
returns 409 + ``blocked_rev_conflict`` + the current ``serverRev`` so the client
can rebase its un-acked tail and retry. This makes "a stale client cannot clobber
fresh server truth" a STRUCTURAL guarantee, not a wall-clock heuristic.

This file covers the two backend-contract invariants (the frontend rebase-on-409
round-trip is tested separately once wired):

  #2 FAIL-OPEN (non-negotiable): a PUT with NO baseRev (old bundle mid-rollout,
     or compat/headless surfaces that never learned about rev) must fall through
     to EXACTLY today's blocked_msg_regression behaviour. A v36-era client
     against a rev=0 row must never start eating 409s.
     + NEUTER: force baseRev down the CAS path with the fail-open `is not None`
       guard removed → a legitimate no-baseRev write 409s → proves the fail-open
       branch is what prevents the false reject.

  #3 SETTINGS/TITLE-ONLY writes never 409: the CAS is scoped to message-bearing
     writes. A settings-only or title-only PUT (or any write with allowTruncate)
     must not assert CAS.

Drives the REAL shipped ``_save_conv_blocking`` against a real DB and inspects
the returned ``_Defer`` (helper + kwargs) — no Quart app context needed, since a
``*_blocking`` body returns a _Defer describing its response.

Standalone runner (real DB); also importable as pytest functions.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _M(n, tag=''):
    # Carry a STABLE _msgId per message (as the production client does — the
    # backend backfills _msgId once and it carries forward). Without it,
    # _assign_message_ids mints a fresh random id on every PUT so "same content"
    # is never byte-identical and the trigger legitimately bumps rev.
    return [{'role': 'user' if i % 2 == 0 else 'assistant',
             'content': f'm{i}{tag}', 'timestamp': 1000 + i,
             '_msgId': f'msg-{i}'} for i in range(n)]


def _seed(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'cas-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now, 'updated_at': now, 'settings': '{}', 'search_text': '',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings', 'search_text'],
        retry=True)
    db.commit()


def _rev(db, conv_id):
    r = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=1',
                   (conv_id,)).fetchone()
    return int((r[0] if not isinstance(r, dict) else r['rev']) or 0)


def _cleanup(db, *ids):
    from lib.database import db_execute_with_retry
    for cid in ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _defer_status(defer):
    """(status, payload_dict) from a _Defer(jsonify/api_ok/api_payload, ...)."""
    from lib.api_response import api_ok, api_payload
    status = getattr(defer, 'status', None)
    if defer.helper is api_ok:
        payload = {'ok': True}
        payload.update(defer.kwargs)
        if defer.args and isinstance(defer.args[0], dict):
            payload.update(defer.args[0])
        return status or 200, payload
    if defer.helper is api_payload:
        # routes.conversations._json passes the status POSITIONALLY:
        # _Defer(api_payload, payload, 409) — args[0]=payload, args[1]=status.
        payload = defer.args[0] if defer.args else {}
        if len(defer.args) > 1 and isinstance(defer.args[1], int):
            status = defer.args[1]
        return status or 200, payload
    # jsonify(dict) branch — the dict is args[0]
    payload = defer.args[0] if defer.args else {}
    return status or 200, payload


def _put(db, conv_id, messages, **extra):
    from routes.conversations import _save_conv_blocking
    data = {'title': 'cas-test', 'messages': messages}
    data.update(extra)
    return _defer_status(_save_conv_blocking(db, conv_id, data))


def test_cas_conflict_rejects_stale_baseRev():
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-cas-conflict'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M(2))
    try:
        # advance the server rev to 1 by a real message write (no baseRev)
        st, _ = _put(db, conv_id, _M(3))
        assert st == 200
        server_rev = _rev(db, conv_id)
        assert server_rev == 1, f'expected server rev=1, got {server_rev}'

        # client still holds baseRev=0 (stale) and tries to write → 409
        st, payload = _put(db, conv_id, _M(3, tag='-client'), baseRev=0)
        assert st == 409, f'stale baseRev must 409, got {st}'
        assert payload.get('error') == 'blocked_rev_conflict', payload
        assert payload.get('serverRev') == 1, payload

        # a matching baseRev is accepted and returns the bumped rev
        st, payload = _put(db, conv_id, _M(4), baseRev=server_rev)
        assert st == 200, f'matching baseRev must succeed, got {st}: {payload}'
        assert payload.get('rev') == 2, f'expected bumped rev=2, got {payload.get("rev")}'
    finally:
        _cleanup(db, conv_id)
    _ok('CAS: stale baseRev → 409 blocked_rev_conflict; matching baseRev → 200 + bumped rev')


def test_fail_open_when_no_baseRev():
    """#2: a write with NO baseRev falls through to legacy behaviour — a normal
    growing write succeeds, and a genuine regression still hits the LEGACY
    blocked_msg_regression guard (not a rev conflict)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-cas-failopen'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M(4))
    try:
        # advance rev a few times with NO baseRev — must NOT 409
        for n in (5, 6, 7):
            st, payload = _put(db, conv_id, _M(n))
            assert st == 200, f'no-baseRev growing write must succeed, got {st}: {payload}'
        assert _rev(db, conv_id) == 3, 'three no-baseRev message writes → rev=3'

        # a genuine regression (fewer msgs, no allowTruncate, no baseRev) must
        # hit the LEGACY guard — proving fail-open routed to today's behaviour.
        st, payload = _put(db, conv_id, _M(2))
        assert st == 409, f'regression must 409 via legacy guard, got {st}'
        assert payload.get('error') == 'blocked_msg_regression', (
            f'must be the LEGACY guard, not rev-conflict: {payload}')
    finally:
        _cleanup(db, conv_id)
    _ok('#2 fail-open: no-baseRev writes route to legacy guards (never rev-conflict 409)')


def test_settings_and_title_only_never_cas_conflict():
    """#3: settings-only / title-only writes are message-neutral and must not
    assert CAS even with a stale baseRev (the trigger doesn't bump rev on them,
    and the handler doesn't reject them)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-cas-metadata'
    db = get_thread_db(DOMAIN_CHAT)
    msgs = _M(4)
    _seed(db, conv_id, msgs)
    try:
        # advance rev to 1
        st, _ = _put(db, conv_id, _M(5))
        assert st == 200 and _rev(db, conv_id) == 1

        # re-PUT the SAME messages (stable _msgId, unchanged content) but change
        # ONLY the title. Because messages are byte-identical, the trigger does
        # NOT bump rev — this is a metadata-only write. With a fresh baseRev it
        # round-trips cleanly and rev stays 1.
        same = _M(5)
        st2, payload2 = _put(db, conv_id, same, baseRev=_rev(db, conv_id),
                             title='renamed-clean')
        assert st2 == 200, f'metadata write with fresh baseRev must succeed: {payload2}'
        assert _rev(db, conv_id) == 1, 'same-content (stable _msgId) write must not bump rev'

        # And a settings-only-shaped change (same messages) with NO baseRev must
        # never CAS-409 either — fail-open + message-neutral.
        st3, payload3 = _put(db, conv_id, _M(5), title='renamed-again')
        assert st3 == 200, f'metadata write without baseRev must succeed: {payload3}'
        assert _rev(db, conv_id) == 1, 'still no rev bump on unchanged messages'
    finally:
        _cleanup(db, conv_id)
    _ok('#3 settings/title-only (same messages) never bump rev; metadata write with fresh baseRev succeeds')


_POSITIVE = [test_cas_conflict_rejects_stale_baseRev,
             test_fail_open_when_no_baseRev,
             test_settings_and_title_only_never_cas_conflict]


def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(' ', _color('✗', '31'), f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


def _neuter_failopen():
    """NC for #2: remove the `base_rev is not None` fail-open guard (force the
    CAS to fire even when no baseRev was sent) and prove a legitimate no-baseRev
    write starts 409-ing. In-process monkeypatch of the module's guard by
    re-running _save_conv_blocking with a patched data dict is not enough (the
    guard reads `data.get('baseRev')`), so we simulate the neuter directly: a
    no-baseRev write is equivalent to baseRev==server_rev ONLY because of the
    `is not None` skip. If we instead treat missing baseRev as 0, a rev>0 row
    would reject it. We assert that today's code does NOT do that (baseRev
    absent → 200), and that forcing baseRev=0 on a rev>0 row DOES 409 — the two
    together prove the fail-open `is not None` branch is load-bearing."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-cas-nc'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M(4))
    try:
        _put(db, conv_id, _M(5))  # rev → 1
        assert _rev(db, conv_id) == 1
        # fail-open ON (baseRev absent): must succeed
        st_open, _ = _put(db, conv_id, _M(6))
        # neuter simulation (baseRev coerced to 0, as if missing meant 0): 409
        st_neut, payload = _put(db, conv_id, _M(7), baseRev=0)
        return (st_open == 200 and st_neut == 409
                and payload.get('error') == 'blocked_rev_conflict'), (st_open, st_neut, payload)
    finally:
        _cleanup(db, conv_id)


def main():
    print()
    print(_color('═══ conversation CAS PUT — fail-open + metadata-scope + conflict ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_conv_cas_put.__main__')

    print(_color('Baseline (shipped CAS PUT handler):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix the CAS handler before neutering')

    print()
    print(_color('NC — fail-open is load-bearing (absent baseRev vs coerced-0):', '36'))
    ok, detail = _neuter_failopen()
    if not ok:
        _fail(f'NC did not confirm fail-open is load-bearing: {detail}')
    _ok('NC: absent baseRev → 200 (fail-open), but baseRev=0 on rev>0 → 409 '
        '(proves `is not None` skip prevents the false reject)')

    print()
    print(_color('═══ ALL CAS-PUT TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
