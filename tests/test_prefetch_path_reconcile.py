#!/usr/bin/env python3
"""Root-simplify frontend sync (epic pt_90a4a14875094c3f): the ``?meta=1&prefetch=``
branch must run the SAME server-authoritative ghost reconcile as the single-conv
GET handler, so a PREFETCHED active conversation reaches the client already
reconciled + ``_reconciledAt``-stamped — and the frontend Case-D
``_classifyGhostTail`` belt becomes genuinely dead (retired in a later increment).

WHY THIS GAP EXISTED
--------------------
``loadConversationsFromServer`` prefetches the active conv via
``?meta=1&prefetch=<id>``; the frontend applies the prefetched body and sets
``pc._needsLoad = false`` (static/js/core/conversations.js), so the reconciling
Phase-2 GET (``loadConversationMessages`` → ``GET /conversations/<id>``, which is
gated behind ``if (ac && ac._needsLoad)``) is SKIPPED for that conv. The old
prefetch branch built its payload with a bare ``_conv_row_to_dict(r)`` — NO
reconcile — so a prefetched active conv with an interrupted ghost tail was
rendered unreconciled and its ``settings._reconciledAt`` was never stamped. That
was the SOLE remaining render path the JS ``_classifyGhostTail`` belt existed
for (main_init_tasks.js Case-D is gated on ``!conv._reconciledAt``).

THE FIX (routes/conversations.py, prefetch branch of ``list_convs``): mirror the
GET handler exactly — gate on ``_conv_has_live_task`` (never sweep a live
placeholder), else run ``_reconcile_conv_on_get_blocking(db, prefetch_id, r)``.

Tests (drive the REAL shipped prefetch-branch helpers against a real DB):
  1. ``test_prefetch_idle_ghost_reconciled`` — a prefetched IDLE conv with a
     ghost empty trailing assistant → reconcile removes it, persists the shorter
     list, stamps ``settings._reconciledAt``, no ``updated_at`` bump; and the
     SERVED prefetch payload is reconciled.  ★ THE FIX.
  2. ``test_prefetch_live_task_placeholder_not_deleted`` — a prefetched conv with
     a pending/running task and an empty trailing placeholder → the live-task
     gate skips reconcile, the placeholder SURVIVES, ``_reconciledAt`` unstamped.
  3. ``test_prefetch_clean_conv_no_write`` — an already-clean prefetched conv is
     not rewritten and not stamped (no write-amplification per prefetch).

NEUTER (on-disk, real file restored byte-identical):
  Revert the prefetch branch to the OLD unreconciled shape
  (``prefetch_data = _conv_row_to_dict(r)`` unconditionally) → test #1 FAILS (the
  ghost tail survives unreconciled + ``_reconciledAt`` unstamped), while #2/#3
  still pass. Proves the reconcile call is load-bearing on the prefetch path.
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart

import pytest  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]

_THIS = os.path.abspath(__file__)
_ROOT = os.path.dirname(os.path.dirname(_THIS))
_TARGET = os.path.join(_ROOT, 'routes', 'conversations.py')


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('\u2713', '32'), msg)
def _fail(msg): print(' ', _color('\u2717', '31'), msg); sys.exit(1)


def _uid(stem):
    import uuid
    return f'{stem}-{uuid.uuid4().hex[:8]}'


def _seed_conv(db, conv_id, messages, settings, *, updated_at=None):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'prefetch-recon-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': updated_at or now_ms,
        'settings': _json.dumps(settings, ensure_ascii=False),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _read(db, conv_id):
    row = db.execute('SELECT messages, settings, updated_at FROM conversations '
                     'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    settings = _json.loads(row[1]) if row[1] and isinstance(row[1], str) else (row[1] or {})
    return msgs, settings, row[2]


def _cleanup(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _run_prefetch(conv_id):
    """Drive the REAL shipped ``_prefetch_reconciled_dict`` helper — the exact
    function the ``?meta=1&prefetch=`` branch of ``list_convs`` calls. Reads the
    row the same way the branch does, then calls the helper. Because we invoke
    the shipped symbol (resolved via ``sys.modules`` at call time), the NC that
    neuters the helper body bites this driver too.

    Returns ``(served_dict, skipped)`` where ``skipped`` reflects the live-task
    gate (probed separately for the assertion — the helper itself only returns
    the dict)."""
    import importlib
    rc = importlib.import_module('routes.conversations')
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    r = db.execute(
        'SELECT id, title, messages, created_at, updated_at, settings, rev '
        'FROM conversations WHERE id=? AND user_id=1', (conv_id,)).fetchone()
    skipped = rc._conv_has_live_task(conv_id)
    served = rc._prefetch_reconciled_dict(db, conv_id, r)
    return served, skipped


def test_prefetch_idle_ghost_reconciled():
    """★ THE FIX: a prefetched idle conv with a ghost tail is reconciled."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = _uid('cv-prefetch-idle')
    db = get_thread_db(DOMAIN_CHAT)
    orig_updated = 121212121
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'settled', 'finishReason': 'stop', 'timestamp': 2},
        {'role': 'user', 'content': 'q2', 'timestamp': 3},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [], 'timestamp': 4},  # ghost tail
    ], settings={}, updated_at=orig_updated)
    try:
        served, skipped = _run_prefetch(conv_id)
        assert not skipped, 'idle prefetched conv must NOT be gated as live'
        msgs, settings, updated_at = _read(db, conv_id)
        roles = [m['role'] for m in msgs]
        assert roles == ['user', 'assistant', 'user'], (
            f'ghost tail NOT reconciled on the PREFETCH path — roles={roles}')
        assert settings.get('_reconciledAt'), '_reconciledAt not stamped on prefetch'
        assert updated_at == orig_updated, (
            f'updated_at was BUMPED ({updated_at} != {orig_updated}) on prefetch')
        assert [m['role'] for m in served['messages']] == ['user', 'assistant', 'user'], \
            'served prefetch payload not reconciled'
    finally:
        _cleanup(db, conv_id)
    _ok('★ prefetched idle ghost tail reconciled + persisted + _reconciledAt + NO updated_at bump')


def test_prefetch_live_task_placeholder_not_deleted():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager as _mgr
    conv_id = _uid('cv-prefetch-live')
    task_id = _uid('tk-prefetch-live')
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [], 'timestamp': 2},  # LIVE placeholder
    ], settings={'activeTaskId': task_id})
    _mgr._record_latest_task(conv_id, task_id)
    _mgr._chat_runtime._tasks[task_id] = {'status': 'running', 'convId': conv_id}
    try:
        _served, skipped = _run_prefetch(conv_id)
        assert skipped, (
            'LIVE prefetched conv was NOT gated — reconcile ran on a running-task '
            'conv, which would delete+persist the live stream placeholder')
        msgs, settings, _ = _read(db, conv_id)
        assert len(msgs) == 2 and msgs[-1]['role'] == 'assistant', (
            f'live placeholder DELETED on prefetch (msgs={len(msgs)}) — corruption')
        assert not settings.get('_reconciledAt'), (
            'live prefetched conv wrongly stamped _reconciledAt')
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
    _ok('live-task placeholder NOT deleted/persisted/stamped on prefetch (gate 1)')


def test_prefetch_clean_conv_no_write():
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = _uid('cv-prefetch-clean')
    db = get_thread_db(DOMAIN_CHAT)
    orig_updated = 343434343
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', 'timestamp': 1},
        {'role': 'assistant', 'content': 'complete answer', 'finishReason': 'stop', 'timestamp': 2},
    ], settings={}, updated_at=orig_updated)
    try:
        _served, skipped = _run_prefetch(conv_id)
        assert not skipped
        msgs, settings, updated_at = _read(db, conv_id)
        assert len(msgs) == 2 and msgs[-1]['content'] == 'complete answer'
        assert not settings.get('_reconciledAt'), (
            'clean prefetched conv stamped _reconciledAt — needless write')
        assert updated_at == orig_updated, 'clean prefetched conv timestamp changed'
    finally:
        _cleanup(db, conv_id)
    _ok('clean idle prefetched conv → no reconcile write, no _reconciledAt (gate 2)')


_POSITIVE = [test_prefetch_idle_ghost_reconciled,
             test_prefetch_live_task_placeholder_not_deleted,
             test_prefetch_clean_conv_no_write]


def _run(fn):
    try:
        fn()
        return True
    except AssertionError as e:
        print(' ', _color('\u2717', '31'), f'{fn.__name__}: {e}')
        return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(' ', _color('\u2717', '31'), f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
        return False


def _neuter_ctx(find, repl):
    from tests._nc_harness import neutered_source
    return neutered_source(_TARGET, find, repl)


def main():
    print()
    print(_color('\u2550\u2550\u2550 prefetch-path reconcile \u2014 neuter \u2550\u2550\u2550', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_prefetch_path_reconcile.__main__')

    print(_color('Baseline (shipped code):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix the implementation before neutering')

    # ── NC: revert the prefetch branch to the OLD unreconciled shape.
    #    The fix wraps the assignment in a live-task gate + reconcile call; the
    #    neuter collapses it back to the bare _conv_row_to_dict(r) → the ghost
    #    tail survives unreconciled → test #1 FAILS, #2/#3 still pass. ──
    print()
    print(_color('NC — revert _prefetch_reconciled_dict to bare _conv_row_to_dict(r):', '36'))
    # The fix's idle branch delegates to _reconcile_conv_on_get_blocking; the
    # neuter forces the helper to ALWAYS return the unreconciled row dict (the
    # old behaviour) → the ghost tail survives → test #1 FAILS, #2/#3 pass.
    _fixed = (
        "    if _conv_has_live_task(conv_id):\n"
        "        return _conv_row_to_dict(r)\n"
        "    return _reconcile_conv_on_get_blocking(db, conv_id, r)")
    _old = (
        "    if _conv_has_live_task(conv_id):\n"
        "        return _conv_row_to_dict(r)\n"
        "    return _conv_row_to_dict(r)  # NC")
    with _neuter_ctx(_fixed, _old):
        idle_ok = _run(test_prefetch_idle_ghost_reconciled)
        live_ok = _run(test_prefetch_live_task_placeholder_not_deleted)
        clean_ok = _run(test_prefetch_clean_conv_no_write)
    if idle_ok:
        _fail('NC: idle-ghost test PASSED with reconcile reverted — the prefetch '
              'reconcile is not load-bearing / the test does not pin it!')
    if not (live_ok and clean_ok):
        _fail('NC: a control test failed — neuter had unintended blast radius')
    _ok('NC: idle-ghost test FAILS with reconcile reverted; live+clean controls still pass')

    print()
    print(_color('Post-restore baseline:', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('post-restore baseline failed — file not restored correctly')

    print()
    print(_color('\u2550\u2550\u2550 ALL PREFETCH-PATH RECONCILE TESTS + NEUTER PASSED \u2550\u2550\u2550', '32'))
    print()


if __name__ == '__main__':
    main()
