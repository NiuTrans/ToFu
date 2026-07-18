#!/usr/bin/env python3
"""A late ``task_results`` write for a DELETED conversation must NOT resurrect
an orphan row.

The delete path (``_delete_conv_blocking``) does ``DELETE FROM task_results
WHERE conv_id=?``. Cooperative abort means a still-winding-down task can reach
its terminal / checkpoint persist AFTER that delete — and ``_upsert_task_row``
historically re-inserted the row unconditionally, keyed on ``task_id`` (an
UPSERT with no existence check against ``conversations``). That orphan row is
the delete-vs-persist race tail this guard closes.

``_sync_result_to_conversation`` already guards the CONVERSATIONS-messages write
with ``if not row: return``; prong 2 adds the same conv-existence guard to the
``task_results`` upsert via the ``_conv_row_exists`` seam.

Asserts, against the REAL ``_upsert_task_row`` body:
  * conv exists → the row IS written (guard doesn't over-fire);
  * conv row deleted → a subsequent ``_upsert_task_row`` writes NOTHING (no
    orphan row survives);
  * an INLINE-message task (``_inline_messages=True``, no conversations row by
    design) is NOT guarded — its row is still written (external callers read
    results straight from task_results).

NEUTER: monkeypatch ``_conv_row_exists`` → always True so the guard can't fire.
The late write for the deleted conv then DOES resurrect an orphan row → the
positive assertion fails. Proves the existence check is load-bearing.

Standalone runner (real DB); also importable as pytest test functions.
"""

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
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _seed_conv(db, conv_id):
    from lib.database._core_schema import CONVERSATIONS, upsert
    from lib.database import json_dumps_pg
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'orphan-guard-test',
        'messages': json_dumps_pg([{'role': 'user', 'content': 'hi'}]),
        'msg_count': 1, 'created_at': now_ms, 'updated_at': now_ms,
        'settings': '{}',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'settings'], retry=True)
    db.commit()


def _delete_conv_row(db, conv_id):
    from lib.database import db_execute_with_retry
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM task_results WHERE conv_id=?', (conv_id,))
    db.commit()


def _task(conv_id, *, inline=False):
    t = {'id': 'tk-' + uuid.uuid4().hex[:12], 'convId': conv_id,
         'created_at': time.time()}
    if inline:
        t['_inline_messages'] = True
    return t


def _row_count(db, task_id):
    r = db.execute('SELECT COUNT(*) FROM task_results WHERE task_id=?',
                   (task_id,)).fetchone()
    return (r[0] if not isinstance(r, dict) else list(r.values())[0])


def _upsert(task, conv_id, status='done'):
    from lib.tasks_pkg.manager import _upsert_task_row
    _upsert_task_row(task, conv_id, content='late', thinking='',
                     status=status, error_json=None, tr_json=None,
                     meta_json=None)


def _cleanup(db, *task_ids):
    from lib.database import db_execute_with_retry
    for tid in task_ids:
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (tid,))
    db.commit()


def test_upsert_writes_when_conv_exists():
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-orphan-live'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id)
    task = _task(conv_id)
    try:
        _upsert(task, conv_id)
        assert _row_count(db, task['id']) == 1, \
            'guard over-fired — row not written for a LIVE conv'
    finally:
        _cleanup(db, task['id'])
        _delete_conv_row(db, conv_id)
    _ok('conv exists → task_results row is written (guard does not over-fire)')


def test_upsert_skips_when_conv_deleted():
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-orphan-dead'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id)
    task = _task(conv_id)
    try:
        _delete_conv_row(db, conv_id)   # simulate _delete_conv_blocking ran
        _upsert(task, conv_id)          # late terminal write races in
        assert _row_count(db, task['id']) == 0, \
            'ORPHAN task_results row resurrected after conv delete'
    finally:
        _cleanup(db, task['id'])
    _ok('conv deleted → late upsert writes NO orphan task_results row')


def test_inline_message_task_not_guarded():
    """External inline-message tasks have no conversations row by design and
    read results straight from task_results — the guard must NOT block them."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    conv_id = 'cv-orphan-inline'   # deliberately never seeded
    db = get_thread_db(DOMAIN_CHAT)
    task = _task(conv_id, inline=True)
    try:
        _upsert(task, conv_id)
        assert _row_count(db, task['id']) == 1, \
            'inline-message task was wrongly guarded (row not written)'
    finally:
        _cleanup(db, task['id'])
    _ok('inline-message task is NOT guarded → its row is still written')


_POSITIVE = [
    test_upsert_writes_when_conv_exists,
    test_upsert_skips_when_conv_deleted,
    test_inline_message_task_not_guarded,
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
    """NC: force _conv_row_exists → True so the guard can't fire. The late
    write for the deleted conv then resurrects an orphan row → proves the
    existence check is load-bearing."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr
    conv_id = 'cv-orphan-nc'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id)
    task = _task(conv_id)
    _orig = mgr._conv_row_exists
    try:
        _delete_conv_row(db, conv_id)
        mgr._conv_row_exists = lambda _db, _cid: True
        _upsert(task, conv_id)
        orphan = (_row_count(db, task['id']) == 1)
        return orphan, f'row_count={_row_count(db, task["id"])} (neutered → should resurrect orphan)'
    finally:
        mgr._conv_row_exists = _orig
        _cleanup(db, task['id'])
        _delete_conv_row(db, conv_id)


def main():
    print()
    print(_color('═══ _upsert_task_row orphan guard (delete-vs-persist race) + neuter ═══', '36'))
    print()
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_upsert_task_row_orphan_guard.__main__')

    print(_color('Baseline (shipped orphan guard):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix the _upsert_task_row conv-existence guard first')

    print()
    print(_color('NC — force _conv_row_exists→True, repeat the post-delete write:', '36'))
    orphan, out = _neuter_and_subrun()
    if not orphan:
        _fail('NC did not confirm the guard is load-bearing:\n' + out)
    _ok('NC: with the existence check dead, the late write resurrects an orphan row (guard is load-bearing)')

    print()
    print(_color('═══ ALL ORPHAN-GUARD TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
