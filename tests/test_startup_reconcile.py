#!/usr/bin/env python3
"""Phase-3 integration: recover_stale_tasks_on_startup must run the backend
ghost reconcile (lib/conversations/reconcile.py) and PERSIST the cleaned
messages in the same commit, plus stamp settings._reconciledAt so the frontend
Case-D defers to it.

WHY (the resurrect bug, structurally fixed)
-------------------------------------------
Previously the frontend swept buried ghosts and tried to persist via a
full-conv PUT with allowTruncate — which an earlier guard silently dropped, so
the ghosts RESURRECTED on every reload. Moving the sweep server-side, persisted
in the SAME commit that recovers the conversation, makes that impossible: the DB
the frontend loads is already clean, and there is no frontend PUT to lose.

Tests (drive the REAL shipped recover_stale_tasks_on_startup against a real DB):
  1. ``test_buried_ghost_swept_and_persisted`` — a conv with a stale running
     task, a buried empty-ghost assistant mid-list, and a settled tail →
     after recovery the buried ghost is GONE from the persisted messages and
     settings._reconciledAt is stamped. ★ RESURRECT coverage + marker.
     Double-neuter: revert the reconcile call to a no-op and the buried ghost
     SURVIVES → this FAILS.
  2. ``test_clean_conv_not_marked`` — a conv whose recovered messages need no
     reconcile is NOT stamped _reconciledAt (behaviour preservation; the
     frontend fallback still owns untouched convs).
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed_conv(db, conv_id, messages, settings):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'reconcile-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
        'settings': _json.dumps(settings, ensure_ascii=False),
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _seed_task(db, task_id, conv_id, content, thinking, status):
    from lib.database._core_schema import TASK_RESULTS, upsert
    now_ms = int(time.time() * 1000)
    upsert(db, TASK_RESULTS, {
        'task_id': task_id, 'conv_id': conv_id, 'content': content,
        'thinking': thinking, 'status': status, 'created_at': now_ms,
    }, insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'status',
                    'created_at'], retry=True)
    db.commit()


def _read(db, conv_id):
    row = db.execute('SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    settings = _json.loads(row[1]) if row[1] and isinstance(row[1], str) else (row[1] or {})
    return msgs, settings


def _cleanup(db, *conv_ids, task_ids=()):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    for tid in task_ids:
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (tid,))
    db.commit()


def test_buried_ghost_swept_and_persisted():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

    conv_id = 'cv-recon-buried'
    task_id = 'tk-recon-buried'
    db = get_thread_db(DOMAIN_CHAT)
    # conv with: a buried empty ghost (idx 1) + a settled tail; stale running task.
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [], 'timestamp': 2},  # buried ghost
        {'role': 'user', 'content': 'q2', 'timestamp': 3},
        {'role': 'assistant', 'content': 'the real settled answer', 'finishReason': 'stop', 'timestamp': 4},
    ], settings={'activeTaskId': task_id})
    _seed_task(db, task_id, conv_id, content='', thinking='', status='running')
    try:
        recover_stale_tasks_on_startup()
        msgs, settings = _read(db, conv_id)
        roles = [m['role'] for m in msgs]
        # The buried ghost (empty assistant at idx 1) must be GONE.
        assert roles == ['user', 'user', 'assistant'], (
            f'buried ghost NOT swept server-side — roles={roles} (resurrect bug). '
            'The reconcile must remove it and persist the shorter list.')
        assert msgs[-1]['content'] == 'the real settled answer', 'real turn lost'
        assert settings.get('_reconciledAt'), (
            'settings._reconciledAt not stamped — frontend Case-D would not defer')
        assert settings.get('activeTaskId') is None, 'stale activeTaskId not cleared'
    finally:
        _cleanup(db, conv_id, task_ids=[task_id])
    _ok('startup recovery sweeps buried ghost server-side + persists + stamps _reconciledAt (no resurrect)')


def test_reconcile_fires_history_rewrite_signal():
    """★ DELIVERABLE-1 wiring. When the startup reconcile CHANGES a conv's
    messages, it must fire notify_history_rewrite(cid) so a later
    detect_cache_break can NAME the backend rewrite. Proves the seam has a real
    production caller (not just test-only scaffolding). We spy on the symbol
    imported at the call site."""
    import lib.tasks_pkg.cache_tracking as _ct
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

    conv_id = 'cv-recon-hrsignal'
    task_id = 'tk-recon-hrsignal'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q1', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [], 'timestamp': 2},  # buried ghost
        {'role': 'user', 'content': 'q2', 'timestamp': 3},
        {'role': 'assistant', 'content': 'settled', 'finishReason': 'stop', 'timestamp': 4},
    ], settings={'activeTaskId': task_id})
    _seed_task(db, task_id, conv_id, content='', thinking='', status='running')

    fired = []
    _orig = _ct.notify_history_rewrite
    _ct.notify_history_rewrite = lambda cid: fired.append(cid)
    try:
        recover_stale_tasks_on_startup()
        assert conv_id in fired, (
            'notify_history_rewrite was NOT fired on the reconcile-changed conv '
            '— the seam has no production caller (dead scaffolding).')
    finally:
        _ct.notify_history_rewrite = _orig
        _cleanup(db, conv_id, task_ids=[task_id])
    _ok('startup reconcile fires notify_history_rewrite on the changed conv (Deliverable 1)')


def test_clean_conv_not_marked():
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

    conv_id = 'cv-recon-clean'
    task_id = 'tk-recon-clean'
    db = get_thread_db(DOMAIN_CHAT)
    # A conv whose tail is a settled assistant and has no buried ghosts — the
    # interrupted-content merge writes messages, but reconcile finds nothing.
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', 'timestamp': 1},
        {'role': 'assistant', 'content': 'already complete', 'finishReason': 'stop', 'timestamp': 2},
    ], settings={'activeTaskId': task_id})
    # Interrupted task carries LESS content than the tail → merge no-ops, reconcile no-ops.
    _seed_task(db, task_id, conv_id, content='sh', thinking='', status='running')
    try:
        recover_stale_tasks_on_startup()
        msgs, settings = _read(db, conv_id)
        assert len(msgs) == 2 and msgs[-1]['content'] == 'already complete'
        assert not settings.get('_reconciledAt'), (
            'clean conv should NOT be marked _reconciledAt (nothing was reconciled)')
    finally:
        _cleanup(db, conv_id, task_ids=[task_id])
    _ok('clean conv is not stamped _reconciledAt (behaviour preservation)')


def main():
    print()
    print(_color('═══ Phase-3 startup-recovery reconcile integration tests ═══', '36'))
    print()
    tests = [test_buried_ghost_swept_and_persisted,
             test_reconcile_fires_history_rewrite_signal,
             test_clean_conv_not_marked]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} STARTUP-RECONCILE TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
