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
        except Exception:
            pass
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


_POSITIVE = [test_idle_husk_bloated_put_persists_swept,
             test_live_task_placeholder_not_swept,
             test_clean_shorter_put_vs_bloated_row_not_regression_rejected]


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


def _neuter(find, repl, label):
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert find in src, f'NC anchor not found for {label}: {find!r}'
    with open(_TARGET, 'w', encoding='utf-8') as f:
        f.write(src.replace(find, repl, 1))
    return src


def _restore(src):
    with open(_TARGET, 'w', encoding='utf-8') as f:
        f.write(src)


def _subrun(test_name):
    """Run ONE positive test in a FRESH subprocess (imports the neutered module
    cleanly). Returns (passed, output)."""
    import subprocess
    code = (
        'import tests.test_save_conv_husk_sweep as t; '
        f'import sys; sys.exit(0 if t._run(t.{test_name}) else 1)'
    )
    env = dict(os.environ)
    env['TOFU_DB_BACKEND'] = 'sqlite'
    env.setdefault('TOFU_DB_PATH', os.environ.get('TOFU_DB_PATH', ''))
    r = subprocess.run([sys.executable, '-c', code], cwd=_ROOT,
                       capture_output=True, text=True, env=env)
    return r.returncode == 0, r.stdout + r.stderr


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
    backup = _neuter(_NC_FIND, _NC_REPL, 'write-seam sweep')
    try:
        idle_ok, idle_out = _subrun('test_idle_husk_bloated_put_persists_swept')
        live_ok, _ = _subrun('test_live_task_placeholder_not_swept')
        clean_ok, _ = _subrun('test_clean_shorter_put_vs_bloated_row_not_regression_rejected')
        if idle_ok:
            _fail('NC: idle husk test PASSED with sweep neutered — sweep is not load-bearing!')
        if not live_ok:
            _fail('NC: live-task control failed — neuter had unintended blast radius')
        # The clean-shorter control legitimately changes behaviour when the
        # sweep is off (the existing-row husk-free recompute still relaxes the
        # guard, so the clean PUT is still accepted); we only require it not to
        # crash. Accept either outcome but surface it.
        _ok(f'NC: idle husk test FAILS with sweep off (persists 202); '
            f'live control still passes (clean_ctrl_pass={clean_ok})')
    finally:
        _restore(backup)

    with open(_TARGET, encoding='utf-8') as f:
        _ = f.read()
    print()
    print(_color('Post-restore baseline:', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('post-restore baseline failed — file not restored correctly')

    print()
    print(_color('═══ ALL SAVE_CONV HUSK-SWEEP TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
