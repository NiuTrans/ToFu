#!/usr/bin/env python3
"""Item #1 (patch→fundamental-fix epic pt_e02044f4ab084dff): the conversation
GET path runs the authoritative ghost reconcile so the frontend Case-D
classifiers become vestigial fallback (retired in a later gated increment).

WHY THE LIVE-TASK GATE IS THE WHOLE BALLGAME
--------------------------------------------
``classify_ghost_tail`` returns 'delete' for a ``{role:'assistant', content:''}``
tail with no finishReason/usage — which is BYTE-IDENTICAL to a fresh streaming
placeholder in the window between ``create_task`` (registers the task running
BEFORE the first delta) and the first streamed token. If a GET fires then and
reconcile runs, it would DELETE the live stream's target and PERSIST that
deletion — data corruption worse than the frontend patch being retired. So the
GET handler must gate on the runtime task state and skip reconcile for a
pending/running conv, leaving _reconciledAt unstamped (frontend keeps deferring).

Tests (drive the REAL shipped helpers against a real DB):
  1. ``test_idle_ghost_reconciled_no_updated_at_bump`` — idle conv with a ghost
     empty trailing assistant → GET-path reconcile removes it, PERSISTS the
     shorter list, stamps settings._reconciledAt, and does NOT bump updated_at.
  2. ``test_live_task_placeholder_not_deleted`` — ★ THE REGRESSION. A conv with a
     pending/running task in the runtime and an empty trailing placeholder →
     _conv_has_live_task is True, reconcile is SKIPPED, the placeholder SURVIVES
     in the DB, and _reconciledAt is NOT stamped.
  3. ``test_clean_conv_no_write`` — an already-clean idle conv is not rewritten
     and not stamped (gate 2: no write-amplification per open).

Double-neuter (on-disk, real file restored byte-identical):
  NC-1 (live-task gate): make ``_conv_has_live_task`` always return False →
        test #2 FAILS (the live placeholder gets deleted+persisted), while #1/#3
        still pass. Proves the gate is load-bearing.
  NC-2 (no-updated_at-bump): make the reconcile UPDATE also set updated_at=now →
        test #1's timestamp assertion FAILS, while #2/#3 still pass. Proves the
        sidebar-restamp guard is load-bearing.
"""

import json as _json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_THIS = os.path.abspath(__file__)
_ROOT = os.path.dirname(os.path.dirname(_THIS))
_TARGET = os.path.join(_ROOT, 'routes', 'conversations.py')


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _uid(stem):
    """Unique per-run conv/task id so fixtures never collide across suites or
    re-runs in the SAME pytest process (hardcoded ids leaked state and could
    mask a real regression in CI)."""
    import uuid
    return f'{stem}-{uuid.uuid4().hex[:8]}'


def _seed_conv(db, conv_id, messages, settings, *, updated_at=None):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'getrecon-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': updated_at or now_ms,
        'settings': _json.dumps(settings, ensure_ascii=False),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _row(db, conv_id):
    return db.execute(
        'SELECT id, title, messages, created_at, updated_at, settings '
        'FROM conversations WHERE id=? AND user_id=1', (conv_id,)).fetchone()


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


def _run_reconcile(conv_id):
    """Invoke the REAL GET-path helpers exactly as get_conv does: gate on
    _conv_has_live_task, then run _reconcile_conv_on_get_blocking off the row."""
    import importlib
    rc = importlib.import_module('routes.conversations')
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    r = _row(db, conv_id)
    if rc._conv_has_live_task(conv_id):
        return rc._conv_row_to_dict(r), True  # skipped (gated)
    return rc._reconcile_conv_on_get_blocking(db, conv_id, r), False


# ─────────────────────────── the three positive tests ───────────────────────────

def test_idle_ghost_reconciled_no_updated_at_bump():
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = _uid('cv-getrecon-idle')
    db = get_thread_db(DOMAIN_CHAT)
    orig_updated = 111222333  # a fixed, old timestamp
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': 'settled', 'finishReason': 'stop', 'timestamp': 2},
        {'role': 'user', 'content': 'q2', 'timestamp': 3},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [], 'timestamp': 4},  # ghost tail → delete
    ], settings={}, updated_at=orig_updated)
    try:
        served, skipped = _run_reconcile(conv_id)
        assert not skipped, 'idle conv must NOT be gated as live'
        msgs, settings, updated_at = _read(db, conv_id)
        roles = [m['role'] for m in msgs]
        assert roles == ['user', 'assistant', 'user'], (
            f'ghost tail NOT reconciled on GET — roles={roles}')
        assert settings.get('_reconciledAt'), '_reconciledAt not stamped'
        assert updated_at == orig_updated, (
            f'updated_at was BUMPED ({updated_at} != {orig_updated}) — sidebar '
            'would re-sort to now on every open (gate 3 violated)')
        assert [m['role'] for m in served['messages']] == ['user', 'assistant', 'user'], \
            'served payload not reconciled'
    finally:
        _cleanup(db, conv_id)
    _ok('idle ghost tail reconciled + persisted + _reconciledAt stamped + NO updated_at bump')


def test_live_task_placeholder_not_deleted():
    """★ THE REGRESSION the gate exists to prevent."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager as _mgr
    conv_id = _uid('cv-getrecon-live')
    task_id = _uid('tk-getrecon-live')
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [], 'timestamp': 2},  # LIVE placeholder
    ], settings={'activeTaskId': task_id})
    # Register a genuinely-running task in the runtime for this conv.
    _mgr._record_latest_task(conv_id, task_id)
    _mgr._chat_runtime._tasks[task_id] = {'status': 'running', 'convId': conv_id}
    try:
        served, skipped = _run_reconcile(conv_id)
        assert skipped, (
            'LIVE conv was NOT gated — reconcile ran on a running-task conv, '
            'which would delete+persist the live stream placeholder')
        msgs, settings, _ = _read(db, conv_id)
        assert len(msgs) == 2 and msgs[-1]['role'] == 'assistant', (
            f'live placeholder was DELETED from the DB (msgs={len(msgs)}) — '
            'data-corruption regression')
        assert not settings.get('_reconciledAt'), (
            'live conv wrongly stamped _reconciledAt — frontend would stop deferring')
    finally:
        _mgr._chat_runtime._tasks.pop(task_id, None)
        with _mgr._conv_latest_task_lock:
            _mgr._conv_latest_task.pop(conv_id, None)
        from lib.runtime_state_store import get_store
        get_store().delete_value('latest', conv_id)
        _cleanup(db, conv_id)
    _ok('★ live-task placeholder is NOT deleted/persisted and NOT stamped (gate 1)')


def test_clean_conv_no_write():
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = _uid('cv-getrecon-clean')
    db = get_thread_db(DOMAIN_CHAT)
    orig_updated = 444555666
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', 'timestamp': 1},
        {'role': 'assistant', 'content': 'complete answer', 'finishReason': 'stop', 'timestamp': 2},
    ], settings={}, updated_at=orig_updated)
    try:
        _served, skipped = _run_reconcile(conv_id)
        assert not skipped
        msgs, settings, updated_at = _read(db, conv_id)
        assert len(msgs) == 2 and msgs[-1]['content'] == 'complete answer'
        assert not settings.get('_reconciledAt'), (
            'clean conv stamped _reconciledAt — a needless write on every open')
        assert updated_at == orig_updated, 'clean conv timestamp changed'
    finally:
        _cleanup(db, conv_id)
    _ok('clean idle conv → no reconcile write, no _reconciledAt (gate 2)')


def test_superseded_error_husk_collapsed_on_get():
    """★ The late-recovery artifact, END-TO-END on the GET path. A conv whose
    persisted list is [user, error-husk, real-assistant] (the client showed an
    error bubble after its recovery window, then a later orphan-recovery
    reconnect appended the real reply) → GET-path reconcile COLLAPSES the error
    husk, persists [user, real-assistant], stamps _reconciledAt, no updated_at
    bump."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = _uid('cv-getrecon-errhusk')
    db = get_thread_db(DOMAIN_CHAT)
    orig_updated = 777888999
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '',
         'error': {'kind': 'internal', 'message': 'Regenerate timed out'},
         'toolRounds': [], 'timestamp': 2},  # error husk
        {'role': 'assistant', 'content': 'the real settled answer',
         'finishReason': 'stop', 'timestamp': 3},  # real reply landed after
    ], settings={}, updated_at=orig_updated)
    try:
        served, skipped = _run_reconcile(conv_id)
        assert not skipped, 'idle conv must NOT be gated as live'
        msgs, settings, updated_at = _read(db, conv_id)
        roles = [m['role'] for m in msgs]
        assert roles == ['user', 'assistant'], (
            f'superseded error husk NOT collapsed on GET — roles={roles}')
        assert msgs[-1]['content'] == 'the real settled answer', 'real reply lost'
        assert msgs[-1].get('error') is None, 'survivor is the real reply, not the husk'
        assert settings.get('_reconciledAt'), '_reconciledAt not stamped'
        assert updated_at == orig_updated, (
            f'updated_at was BUMPED ({updated_at} != {orig_updated}) — gate 3 violated')
        assert [m['role'] for m in served['messages']] == ['user', 'assistant'], \
            'served payload not collapsed'
    finally:
        _cleanup(db, conv_id)
    _ok('★ superseded error husk collapsed + persisted on GET + _reconciledAt + NO updated_at bump')


_POSITIVE = [test_idle_ghost_reconciled_no_updated_at_bump,
             test_live_task_placeholder_not_deleted,
             test_clean_conv_no_write,
             test_superseded_error_husk_collapsed_on_get]


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


def _neuter_ctx(find, repl):
    """In-memory neuter of _TARGET (routes.conversations) via the shared xdist-
    safe harness — the shipped file is opened read-only, never written."""
    from tests._nc_harness import neutered_source
    return neutered_source(_TARGET, find, repl)


def main():
    print()
    print(_color('═══ GET-path reconcile — double-neuter ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_get_path_reconcile.__main__')

    # ── Baseline: all three positive tests pass on the shipped file ──
    print(_color('Baseline (shipped code):', '36'))
    passed = all(_run(fn) for fn in _POSITIVE)
    if not passed:
        _fail('baseline failed — fix the implementation before neutering')

    # ── NC-1: neuter the live-task gate → test #2 must FAIL, #1/#3 pass ──
    print()
    print(_color('NC-1 — neuter live-task gate (_conv_has_live_task → False):', '36'))
    with _neuter_ctx(
            'def _conv_has_live_task(conv_id):\n    ',
            'def _conv_has_live_task(conv_id):\n    return False  # NC-1\n    '):
        live_ok = _run(test_live_task_placeholder_not_deleted)
        idle_ok = _run(test_idle_ghost_reconciled_no_updated_at_bump)
        clean_ok = _run(test_clean_conv_no_write)
    if live_ok:
        _fail('NC-1: live-task test PASSED with gate neutered — gate is not load-bearing!')
    if not (idle_ok and clean_ok):
        _fail('NC-1: a control test failed — neuter had unintended blast radius')
    _ok('NC-1: live-task test FAILS with gate off; idle+clean controls still pass')

    # ── NC-2: make the reconcile UPDATE bump updated_at → test #1 must FAIL ──
    print()
    print(_color('NC-2 — neuter no-bump guard (add updated_at=now to UPDATE):', '36'))
    with _neuter_ctx(
            "'UPDATE conversations SET messages=?, settings=?, msg_count=?, '\n"
            "        'search_text=? WHERE id=? AND user_id=?',\n"
            "        (messages_json, settings_json, len(cleaned), search_text,\n"
            "         conv_id, DEFAULT_USER_ID))",
            "'UPDATE conversations SET messages=?, settings=?, msg_count=?, '\n"
            "        'search_text=?, updated_at=? WHERE id=? AND user_id=?',\n"
            "        (messages_json, settings_json, len(cleaned), search_text,\n"
            "         int(time.time() * 1000), conv_id, DEFAULT_USER_ID))"):
        idle_ok = _run(test_idle_ghost_reconciled_no_updated_at_bump)
        live_ok = _run(test_live_task_placeholder_not_deleted)
        clean_ok = _run(test_clean_conv_no_write)
    if idle_ok:
        _fail('NC-2: idle test PASSED with updated_at bumped — no-bump guard not load-bearing!')
    if not (live_ok and clean_ok):
        _fail('NC-2: a control test failed — neuter had unintended blast radius')
    _ok('NC-2: idle no-bump test FAILS with updated_at bumped; live+clean controls still pass')

    # ── Post-neuter baseline (source was never mutated on disk) ──
    print()
    print(_color('Post-restore baseline:', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('post-restore baseline failed — file not restored correctly')

    print()
    print(_color('═══ ALL GET-PATH RECONCILE TESTS + DOUBLE-NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
