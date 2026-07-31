#!/usr/bin/env python3
"""Regression: the full-conv PUT must RE-DERIVE the settled-turn sidebar facts
(``lastFinishReason`` / ``lastMsgError`` / ``lastMsgHasOutput``) from the
AUTHORITATIVE posted tail — never trust the client's settings payload.

WHY (owner-reported "historical conversations sync is poor" bug)
----------------------------------------------------------------
The task-settle path (``lib/tasks_pkg/manager/_sync.py``) stamps these facts
into ``conversations.settings`` so the meta-only sidebar shell can paint an
error/incomplete dot WITHOUT loading the stripped messages. But the client's
full-conv PUT builds its settings from a whitelist that OMITS the trio
(static/js/core/conversations.js), and ``_save_conv_blocking`` used to take
``data.get('settings')`` verbatim — so EVERY client sync silently clobbered
the manager-stamped facts. DB evidence on production rows: two conversations
whose tail assistant message carries ``finishReason='error'`` (a Project-Brain
auto-dispatch whose model reply failed) had settings with NO error facts —
the unloaded sidebar shell could no longer show the red dot.

THE FIX
-------
``_save_conv_blocking`` now derives the quintet via the single-source helper
``lib.chat.persistence.settled_turn_facts(raw_messages[-1])`` (also used by
``persist_conv_messages``), at both the initial injection AND the post-
husk-sweep re-derive.

Covers:
  1. Clobber regression: seed = manager-stamped ERROR facts; client PUT with a
     whitelist settings payload (trio absent) and a NORMAL settled tail →
     facts re-derived from the tail (stop/False/True), not dropped.
  2. Error stamping: client PUT with an ERROR tail and a whitelist payload →
     the trio is stamped even though the client never sent it.
  3. Client-echo discrimination (NEUTER-equivalent): a client that ECHOES
     stale WRONG facts (error) alongside a normal tail must be IGNORED — the
     server derives from the authoritative tail. Pre-fix the echoed 'error'
     would have been trusted (badge wrongly red); derivation-from-tail is
     what makes the echo harmless.
  4. Empty tail: a 0-message write pops all five keys.
  5. Helper unit-shape checks (settled_turn_facts).

Drives the REAL shipped ``_save_conv_blocking`` against a real DB — mirrors
tests/test_conv_cas_put.py's scaffolding (no Quart app context needed).
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _M(n, tag=''):
    return [{'role': 'user' if i % 2 == 0 else 'assistant',
             'content': f'm{i}{tag}', 'timestamp': 1000 + i,
             '_msgId': f'msg-{i}{tag}'} for i in range(n)]


def _error_turn():
    """A Project-Brain-style dispatch + failed model reply, as the task-settle
    path persists it (empty content, finishReason='error', error envelope)."""
    return [
        {'role': 'user', 'content': 'Project Brain — autonomous dispatch',
         'timestamp': 2000, '_msgId': 'msg-dispatch'},
        {'role': 'assistant', 'content': '', 'finishReason': 'error',
         'error': {'kind': 'endpoint_unreachable', 'message': 'Endpoint unreachable'},
         'timestamp': 2001, '_msgId': 'msg-err'},
    ]


def _seed(db, conv_id, messages, settings=None):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'facts-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now, 'updated_at': now,
        'settings': json.dumps(settings or {}, ensure_ascii=False),
        'search_text': '',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings', 'search_text'],
        retry=True)
    db.commit()


def _settings(db, conv_id):
    r = db.execute('SELECT settings FROM conversations WHERE id=? AND user_id=1',
                   (conv_id,)).fetchone()
    raw = r[0] if not isinstance(r, dict) else r['settings']
    return json.loads(raw or '{}')


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


def _put(db, conv_id, messages, settings=None):
    from routes.conversations import _save_conv_blocking
    data = {'title': 'facts-test', 'messages': messages}
    if settings is not None:
        data['settings'] = settings
    return _defer_status(_save_conv_blocking(db, conv_id, data))


# The real client whitelist (static/js/core/conversations.js syncConversationToServer)
# — deliberately WITHOUT the settled-turn trio.
_CLIENT_WHITELIST = {'preset': 'm', 'model': 'm', 'activeTaskId': None}


def test_put_rederives_facts_from_tail_not_client_payload():
    """1. Clobber regression: manager-stamped ERROR facts must be REPLACED by
    tail-derived facts on the next client sync, not dropped with the whitelist."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-facts-rederive'
    db = get_thread_db(DOMAIN_CHAT)
    seeded = _M(2) + _error_turn()
    _seed(db, conv_id, seeded, settings={
        # what the task-settle path stamped when the dispatch turn errored
        'lastMsgRole': 'assistant', 'lastMsgTimestamp': 2001,
        'lastFinishReason': 'error', 'lastMsgError': True, 'lastMsgHasOutput': False,
    })
    try:
        # The user retries in the client; the turn now settles NORMALLY and the
        # client syncs the full conv with its whitelist settings (no trio).
        new_tail = {'role': 'assistant', 'content': 'recovered answer',
                    'finishReason': 'stop', 'timestamp': 2002, '_msgId': 'msg-ok'}
        st, payload = _put(db, conv_id, seeded + [new_tail],
                           settings=dict(_CLIENT_WHITELIST))
        assert st == 200, f'growing PUT must succeed: {payload}'
        s = _settings(db, conv_id)
        assert s.get('lastFinishReason') == 'stop', (
            f'facts must be re-derived from the authoritative tail: {s}')
        assert s.get('lastMsgError') is False, s
        assert s.get('lastMsgHasOutput') is True, s
        assert s.get('lastMsgRole') == 'assistant', s
    finally:
        _cleanup(db, conv_id)


def test_put_stamps_error_facts_when_client_omits_them():
    """2. The exact production clobber: a client sync right after the failed
    dispatch turn must still leave the ERROR facts in settings (the sidebar
    red dot for the meta-only shell depends on them)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-facts-stamp'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M(2), settings={})
    try:
        msgs = _M(2) + _error_turn()
        st, payload = _put(db, conv_id, msgs, settings=dict(_CLIENT_WHITELIST))
        assert st == 200, f'growing PUT must succeed: {payload}'
        s = _settings(db, conv_id)
        assert s.get('lastFinishReason') == 'error', s
        assert s.get('lastMsgError') is True, s
        # empty content / no thinking / no toolRounds → no output
        assert s.get('lastMsgHasOutput') is False, s
        assert s.get('lastMsgRole') == 'assistant', s
    finally:
        _cleanup(db, conv_id)


def test_put_ignores_client_echoed_stale_facts():
    """3. Discrimination (NEUTER-equivalent): a client ECHOING stale WRONG
    facts must be overridden by tail derivation. Pre-fix, the echoed 'error'
    survived → badge wrongly red. If the derivation were removed, the echo
    would win again — so this check is load-bearing for the fix."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-facts-echo'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _M(2), settings={})
    try:
        ok_tail = {'role': 'assistant', 'content': 'fine',
                   'finishReason': 'stop', 'timestamp': 2002, '_msgId': 'msg-ok2'}
        echoed = dict(_CLIENT_WHITELIST)
        echoed.update({'lastFinishReason': 'error', 'lastMsgError': True,
                       'lastMsgHasOutput': False})
        st, payload = _put(db, conv_id, _M(2) + [ok_tail], settings=echoed)
        assert st == 200, f'growing PUT must succeed: {payload}'
        s = _settings(db, conv_id)
        assert s.get('lastFinishReason') == 'stop', (
            f'client echo must NOT win over the authoritative tail: {s}')
        assert s.get('lastMsgError') is False, s
        assert s.get('lastMsgHasOutput') is True, s
    finally:
        _cleanup(db, conv_id)


def test_put_empty_tail_pops_all_fact_keys():
    """4. A 0-message write (new empty conv) pops the whole quintet."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-facts-empty'
    db = get_thread_db(DOMAIN_CHAT)
    try:
        st, payload = _put(db, conv_id, [], settings={
            'lastFinishReason': 'error', 'lastMsgError': True,
            'lastMsgHasOutput': True, 'lastMsgRole': 'assistant',
            'lastMsgTimestamp': 123,
        })
        assert st == 200, f'empty write on a fresh conv must succeed: {payload}'
        s = _settings(db, conv_id)
        for k in ('lastMsgRole', 'lastMsgTimestamp', 'lastFinishReason',
                  'lastMsgError', 'lastMsgHasOutput'):
            assert k not in s, f'{k} must be popped on an empty tail: {s}'
    finally:
        _cleanup(db, conv_id)


def test_settled_turn_facts_helper_shapes():
    """5. Helper unit checks — the single source shared by persist_conv_messages
    and _save_conv_blocking."""
    from lib.chat.persistence import settled_turn_facts
    f = settled_turn_facts({'role': 'assistant', 'timestamp': 7,
                            'finishReason': 'stop', 'content': 'x'})
    assert f == {'lastMsgRole': 'assistant', 'lastMsgTimestamp': 7,
                 'lastFinishReason': 'stop', 'lastMsgError': False,
                 'lastMsgHasOutput': True}, f
    f_err = settled_turn_facts({'role': 'assistant', 'timestamp': 8,
                                'finishReason': 'error', 'content': '',
                                'error': {'kind': 'endpoint_unreachable'}})
    assert f_err['lastMsgError'] is True and f_err['lastMsgHasOutput'] is False, f_err
    f_user = settled_turn_facts({'role': 'user', 'timestamp': 9, 'content': 'hi'})
    assert f_user['lastMsgRole'] == 'user' and f_user['lastFinishReason'] is None, f_user
