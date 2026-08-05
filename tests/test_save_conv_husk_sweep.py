#!/usr/bin/env python3
"""Backend-authoritative ghost-husk sweep on the ``save_conv`` PUT seam.

WHY
---
Conversation ``mrbf9px2g5mct3`` showed a wall of ~6 empty "Agent" bubbles all
stamped the same minute. Ground truth: they were PERSISTED (202 rows, indices
196-201, contiguous trailing, each ``{role:'assistant', content:'', thinking:'',
toolRounds:[], _msgId:'tmp_…'}`` with NO finishReason/usage/model/_ep marker).

Writer = a frontend reconnect / queue-drain recovery path
(``main_send_pipeline.js`` _checkForQueuedTask / _recoverTimedOutChatTask)
pushing empty assistant placeholders; a flaky tunnel re-entered it ~6× → the
husks were PUT verbatim. The PUT (``_save_conv_blocking``) had NO anti-ghost
guard: its only guards are 0-msg / count-regression / equal-count-stale-
checkpoint, so a count-GROWTH PUT (195→202) carrying trailing empties sailed
through and persisted. The GET-path reconcile + rev-CAS cleaned it up AFTER the
fact — but the window they miss is exactly what the user saw.

THE FIX (this suite guards it)
------------------------------
``_save_conv_blocking`` now runs the SAME pure verdict as the GET path
(``reconcile_conversation_messages``: buried-ghost sweep + tail delete/interrupt)
on the incoming payload BEFORE the write, so husks can never land — not even
transiently. Gated on ``_conv_has_live_task`` (never delete a live streaming
placeholder), skipped on ``allowTruncate``, cache-neutral via
``get_cache_prefix_count``.

Tests (drive the REAL shipped ``_save_conv_blocking`` against a real sqlite DB):
  1. ``test_idle_husk_bloated_put_persists_swept`` — the reported case: an idle
     conv, PUT a 202-msg payload with 6 buried/trailing empty husks → persists
     196 with a real content tail (no husks).
  2. ``test_live_task_placeholder_not_swept`` — ★ THE REGRESSION. A conv with a
     pending/running task in the runtime + an empty trailing placeholder → the
     sweep is GATED off, the placeholder SURVIVES the PUT.
  3. ``test_clean_shorter_put_vs_bloated_row_not_regression_rejected`` — the
     ordering trap: a clean 196-msg PUT arriving against an already-husk-bloated
     202-msg row must NOT trip ``blocked_msg_regression`` (the guard compares the
     husk-FREE existing count) — else the guard would actively PRESERVE husks.

On-disk neuter (real file restored byte-identical, subprocess so the neutered
module imports cleanly):
  NC (remove the sweep call): test #1 FAILS with "still 202 / husks present",
     while #2 (live gate is upstream of the sweep) and the clean-conv control
     still pass. Proves the sweep is load-bearing.

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
_TARGET = os.path.join(_ROOT, 'routes', 'conversations.py')


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── message builders ──
def _real(role, i):
    """A settled, content-bearing message with a stable id."""
    m = {'role': role, 'content': f'{role}-content-{i}', 'thinking': '',
         'toolRounds': [], 'timestamp': 1000 + i, '_msgId': f'msg-{i}'}
    if role == 'assistant':
        m['finishReason'] = 'stop'
    return m


def _husk(i):
    """A byte-faithful empty ghost husk (exactly the shape found in the DB:
    tmp_ id, no model, no finishReason/usage, no _ep marker)."""
    return {'role': 'assistant', 'content': '', 'thinking': '',
            'toolRounds': [], 'timestamp': 1783576548000 + i,
            '_msgId': f'tmp_{i:08x}'}


def _clean_body(n):
    """A clean alternating user/assistant list of length n ending on a real
    content-bearing assistant."""
    msgs = []
    for i in range(n - 1):
        msgs.append(_real('user' if i % 2 == 0 else 'assistant', i))
    msgs.append(_real('assistant', n - 1))  # real content tail
    return msgs


def _seed(db, conv_id, messages, *, updated_at=None):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now = updated_at or int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'husk-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now, 'updated_at': now, 'settings': '{}', 'search_text': '',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings', 'search_text'],
        retry=True)
    db.commit()


def _read(db, conv_id):
    import json
    row = db.execute('SELECT messages, msg_count, updated_at FROM conversations '
                     'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
    raw = row[0] if not isinstance(row, dict) else row['messages']
    msgs = json.loads(raw) if isinstance(raw, str) else raw
    mc = row[1] if not isinstance(row, dict) else row['msg_count']
    up = row[2] if not isinstance(row, dict) else row['updated_at']
    return msgs, mc, up


def _cleanup(db, *ids):
    from lib.database import db_execute_with_retry
    for cid in ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _defer_status(defer):
    from lib.api_response import api_ok, api_payload
    # Mirror `_finish`: the helper MUST be callable — a missing import (the
    # 2026-08-03 jsonify production 500) leaves it unbound/None and the real
    # materialization would crash into a 500. Unpacking args without this
    # check lets exactly that bug sail through the harness.
    if not callable(defer.helper):
        raise TypeError(f'_Defer helper {defer.helper!r} is not callable '
                        '(missing import? — the real _finish would 500 here)')
    status = getattr(defer, 'status', None)
    if defer.helper is api_payload and len(defer.args) > 1:
        # api_payload carries its HTTP status positionally (the _Defer status
        # kwarg is bookkeeping-only and must stay None for these).
        status = defer.args[1]
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
    data = {'title': 'husk-test', 'messages': messages}
    data.update(extra)
    return _defer_status(_save_conv_blocking(db, conv_id, data))


def _is_husk(m):
    return (m.get('role') == 'assistant'
            and not (m.get('content') or m.get('finishReason')
                     or m.get('usage') or m.get('error'))
            and not any((r or {}).get('status') == 'done' or (r or {}).get('toolContent')
                        for r in (m.get('toolRounds') or [])))


# ─────────────────────────── positive tests ───────────────────────────

def test_idle_husk_bloated_put_persists_swept():
    """The reported case: idle conv, PUT 202 msgs = 196 real + 6 husks (5 buried
    + 1 trailing) → persists 196, real content tail, zero husks."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-husk-idle'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _clean_body(195))
    try:
        base = _clean_body(196)                 # real, ends on content assistant
        # 5 buried husks spliced BEFORE the last real msg + 1 trailing husk =
        # 202 total, the exact shape from the incident.
        payload = base[:-1] + [_husk(i) for i in range(5)] + [base[-1]] + [_husk(99)]
        assert len(payload) == 202
        st, pl = _put(db, conv_id, payload)
        assert st == 200, f'growth PUT must succeed, got {st}: {pl}'
        msgs, mc, _ = _read(db, conv_id)
        assert mc == 196 and len(msgs) == 196, (
            f'husks NOT swept on the write seam — persisted {len(msgs)} (mc={mc}), '
            'expected 196')
        assert not any(_is_husk(m) for m in msgs), 'a ghost husk survived the PUT'
        assert msgs[-1]['role'] == 'assistant' and msgs[-1].get('content'), (
            'tail is not a real content-bearing assistant after sweep')
    finally:
        _cleanup(db, conv_id)
    _ok('idle husk-bloated PUT (202) persists SWEPT (196) with a real content tail')


def test_live_task_placeholder_not_swept():
    """★ THE REGRESSION the live-task gate prevents: a conv with a pending/
    running task + an empty trailing placeholder → the sweep is gated OFF and
    the placeholder SURVIVES (it is byte-identical to a ghost tail)."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager as _mgr
    conv_id = 'cv-husk-live'
    task_id = 'tk-husk-live'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, [_real('user', 0)])
    _mgr._record_latest_task(conv_id, task_id)
    _mgr._chat_runtime._tasks[task_id] = {'status': 'running', 'convId': conv_id}
    try:
        # Client PUTs the user turn + a fresh empty streaming placeholder.
        payload = [_real('user', 0), _husk(0)]
        st, pl = _put(db, conv_id, payload)
        assert st == 200, f'live PUT must succeed, got {st}: {pl}'
        msgs, mc, _ = _read(db, conv_id)
        assert mc == 2 and len(msgs) == 2, (
            f'live streaming placeholder was SWEPT (persisted {len(msgs)}) — '
            'the live-task gate failed, this would corrupt the live stream')
        assert msgs[-1]['role'] == 'assistant' and not msgs[-1].get('content'), (
            'the live empty placeholder must be preserved intact')
    finally:
        _mgr._chat_runtime._tasks.pop(task_id, None)
        with _mgr._conv_latest_task_lock:
            _mgr._conv_latest_task.pop(conv_id, None)
        try:
            from lib.runtime_state_store import get_store
            get_store().set_value('latest', conv_id, None, 1)
        except Exception as _e:
            # teardown best-effort — visible, never a bare pass
            print(f'  (runtime-state cleanup best-effort failed: {_e})')
        _cleanup(db, conv_id)
    _ok('★ live-task empty placeholder is NOT swept by the PUT (gate 1)')


def test_clean_shorter_put_vs_bloated_row_not_regression_rejected():
    """The ordering trap: a CLEAN 196-msg PUT arriving against an already-husk-
    bloated 202-msg row must NOT be rejected as a regression. The guard compares
    the husk-FREE existing count (196), so 196 == 196 is accepted — otherwise the
    guard would actively PRESERVE the husks it should let the client overwrite."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-husk-bloated-row'
    db = get_thread_db(DOMAIN_CHAT)
    # Seed the row ALREADY bloated with husks (as if a prior buggy PUT landed).
    base = _clean_body(196)
    bloated = base[:-1] + [_husk(i) for i in range(5)] + [base[-1]] + [_husk(99)]
    _seed(db, conv_id, bloated)
    try:
        # Client sends the CLEAN 196 (fewer than the stored 202).
        st, pl = _put(db, conv_id, _clean_body(196))
        assert st == 200, (
            f'clean 196 PUT vs bloated 202 row wrongly rejected ({st}: {pl}) — '
            'regression guard compared against the RAW 202 instead of the '
            'husk-free 196, so it actively preserved the husks')
        msgs, mc, _ = _read(db, conv_id)
        assert mc == 196 and not any(_is_husk(m) for m in msgs), (
            f'row not clean after accept (mc={mc})')
    finally:
        _cleanup(db, conv_id)
    _ok('clean shorter PUT vs husk-bloated row accepted (husk-free count) — '
        'guard does not preserve husks')


def test_stale_baserev_put_returns_409_not_500():
    """PRODUCTION BUG GUARD (2026-08-03 13:05:01 — 'name jsonify is not
    defined' on PUT /api/v1/conversations): every save_conv 409 returns via
    ``_Defer(jsonify, …)``; with ``jsonify`` unimported the rejection was a
    500 and the client's rebase-and-retry contract broke. Pin: stale baseRev
    → 409 + error=blocked_rev_conflict + serverRev, row untouched."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-409-rev'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _clean_body(4))
    try:
        row = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=1',
                         (conv_id,)).fetchone()
        server_rev = row[0] if not isinstance(row, dict) else row['rev']
        st, pl = _put(db, conv_id, _clean_body(5), baseRev=int(server_rev) + 999)
        assert st == 409, f'rev-conflict PUT must 409 (not 500), got {st}: {pl}'
        assert pl.get('error') == 'blocked_rev_conflict', pl
        assert pl.get('serverRev') == server_rev
        msgs, mc, _ = _read(db, conv_id)
        assert mc == 4 and len(msgs) == 4, 'rejected PUT must not mutate the row'
    finally:
        _cleanup(db, conv_id)
    _ok('stale baseRev PUT → 409 blocked_rev_conflict (not 500)')


def test_empty_overwrite_put_returns_409_not_500():
    """Same jsonify-import guard on the blocked_empty_overwrite path: a 0-msg
    PUT against a non-empty row → 409, not 500."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-409-empty'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _clean_body(4))
    try:
        st, pl = _put(db, conv_id, [])
        assert st == 409, f'empty-overwrite PUT must 409 (not 500), got {st}: {pl}'
        assert pl.get('error') == 'blocked_empty_overwrite', pl
        msgs, mc, _ = _read(db, conv_id)
        assert mc == 4 and len(msgs) == 4
    finally:
        _cleanup(db, conv_id)
    _ok('empty-overwrite PUT → 409 blocked_empty_overwrite (not 500)')


def test_msg_regression_put_returns_409_not_500():
    """Same guard on the blocked_msg_regression path: a shorter PUT without
    allowTruncate → 409, not 500."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-409-regress'
    db = get_thread_db(DOMAIN_CHAT)
    _seed(db, conv_id, _clean_body(6))
    try:
        st, pl = _put(db, conv_id, _clean_body(4))
        assert st == 409, f'regression PUT must 409 (not 500), got {st}: {pl}'
        assert pl.get('error') == 'blocked_msg_regression', pl
        assert pl.get('serverMsgCount') == 6
        msgs, mc, _ = _read(db, conv_id)
        assert mc == 6 and len(msgs) == 6
    finally:
        _cleanup(db, conv_id)
    _ok('msg-regression PUT → 409 blocked_msg_regression (not 500)')


_POSITIVE = [test_idle_husk_bloated_put_persists_swept,
             test_live_task_placeholder_not_swept,
             test_clean_shorter_put_vs_bloated_row_not_regression_rejected,
             test_stale_baserev_put_returns_409_not_500,
             test_empty_overwrite_put_returns_409_not_500,
             test_msg_regression_put_returns_409_not_500]


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


# ─────────────────────────── on-disk neuter ───────────────────────────

# The anchor is the load-bearing sweep-application line. Removing it (short-
# circuit `_changed` to False) leaves the incoming payload unswept.
_NC_FIND = '            _cleaned, _changed = reconcile_conversation_messages(raw_messages, _prefix_n)\n'
_NC_REPL = ('            _cleaned, _changed = reconcile_conversation_messages(raw_messages, _prefix_n)\n'
            '            _changed = False  # NC: neuter the write-seam sweep\n')


def _neuter_ctx(find, repl):
    """In-memory neuter of _TARGET (routes.conversations) via the shared xdist-
    safe harness — the shipped file is opened read-only, never written."""
    from tests._nc_harness import neutered_source
    return neutered_source(_TARGET, find, repl)


def _subrun(test_fn):
    """Run ONE positive test IN-PROCESS (under the active in-memory neuter).
    Returns (passed, ''). No subprocess, no on-disk source mutation."""
    return _run(test_fn), ''


def main():
    print()
    print(_color('═══ save_conv ghost-husk sweep — write-seam symmetry + neuter ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_save_conv_husk_sweep.__main__')

    print(_color('Baseline (shipped save_conv sweep):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix the sweep before neutering')

    print()
    print(_color('NC — sweep is load-bearing (force _changed=False):', '36'))
    with _neuter_ctx(_NC_FIND, _NC_REPL):
        idle_ok, _ = _subrun(test_idle_husk_bloated_put_persists_swept)
        live_ok, _ = _subrun(test_live_task_placeholder_not_swept)
        clean_ok, _ = _subrun(test_clean_shorter_put_vs_bloated_row_not_regression_rejected)
    if idle_ok:
        _fail('NC: idle husk test PASSED with sweep neutered — sweep is not load-bearing!')
    if not live_ok:
        _fail('NC: live-task control failed — neuter had unintended blast radius')
    # The clean-shorter control legitimately changes behaviour when the sweep is
    # off (the existing-row husk-free recompute still relaxes the guard, so the
    # clean PUT is still accepted); we only require it not to crash.
    _ok(f'NC: idle husk test FAILS with sweep off (persists 202); '
        f'live control still passes (clean_ctrl_pass={clean_ok})')

    # NC-2 (production-bug class): break the ``_json`` passthrough helper →
    # every 409 path materializes via a non-callable → the three 409 guards
    # FAIL (harness mirrors _finish), while the 200 paths (which never route
    # through it) keep passing.
    _NC2_FIND = '    return _Defer(api_payload, payload, status or 200)\n'
    _NC2_REPL = ('    return _Defer(None, payload, status or 200)  # NC: break '
                 'the 409 passthrough materialization\n')
    print()
    print(_color('NC-2 — _json passthrough is load-bearing (all 409 paths):', '36'))
    with _neuter_ctx(_NC2_FIND, _NC2_REPL):
        rev_ok, _ = _subrun(test_stale_baserev_put_returns_409_not_500)
        empty_ok, _ = _subrun(test_empty_overwrite_put_returns_409_not_500)
        regress_ok, _ = _subrun(test_msg_regression_put_returns_409_not_500)
        idle_ok2, _ = _subrun(test_idle_husk_bloated_put_persists_swept)
    if rev_ok or empty_ok or regress_ok:
        _fail('NC-2: a 409 test PASSED without the jsonify import — not load-bearing!')
    if not idle_ok2:
        _fail('NC-2: 200-path control failed — neuter had unintended blast radius')
    _ok('NC-2: all three 409 guards FAIL without the import; 200-path control passes')

    print()
    print(_color('Post-restore baseline:', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('post-restore baseline failed — file not restored correctly')

    print()
    print(_color('═══ ALL SAVE_CONV HUSK-SWEEP TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
