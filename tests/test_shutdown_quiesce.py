#!/usr/bin/env python3
"""Regression: at server shutdown, EVERY running task must be signalled to abort
BEFORE the atexit PG-stop hook fires.

WHY (the shutdown-cascade incident, 2026-07-11)
-----------------------------------------------
When ``asyncio.run(_serve())`` returned, the atexit ``stop_local_pg_if_owned``
hook stopped PostgreSQL while re-dispatched carriers were STILL running on the
agent-worker pool. Every one of their ``get_thread_db`` calls then hit
``FATAL: the database system is shutting down`` and, once the interpreter began
tearing down, ``cannot schedule new futures after interpreter shutdown`` —
dozens of tracebacks per shutdown.

THE FIX
-------
``lib.tasks_pkg.manager.quiesce_running_tasks`` sets the cooperative
``task['aborted']`` flag on every running task; the server calls it after
Hypercorn drains and gives the pool a bounded window to stop, so tasks are no
longer mid-DB-write when PG goes down.

Tests (drive the REAL manager against the runtime store; no LLM, no PG stop):
  1. quiesce marks ALL running tasks aborted + stamps reason.
  2. it leaves already-terminal tasks untouched (idempotent, no double count).
  3. NC: neuter quiesce to a no-op → a running task stays un-aborted (proves
     the shutdown drain would then race PG — the cascade returns).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _mk_running(conv_id):
    from lib.tasks_pkg.manager import create_task
    return create_task(conv_id, [{'role': 'user', 'content': 'q'}], {},
                       supersede=False, user_id=1)


def _cleanup(*task_ids):
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        for tid in task_ids:
            tasks.pop(tid, None)


def test_quiesce_aborts_all_running():
    from lib.tasks_pkg.manager import quiesce_running_tasks
    t1 = _mk_running('cv-q1')
    t2 = _mk_running('cv-q2')
    try:
        assert not t1['aborted'] and not t2['aborted']
        n = quiesce_running_tasks(reason='server_shutdown')
        assert n >= 2, f'expected >=2 aborted, got {n}'
        assert t1['aborted'] is True and t2['aborted'] is True
        assert t1['_abort_reason'] == 'server_shutdown'
        assert '_abort_timestamp' in t1
    finally:
        _cleanup(t1['id'], t2['id'])
    _ok('quiesce_running_tasks marks all running tasks aborted + stamps reason')


def test_quiesce_leaves_terminal_tasks_untouched():
    from lib.tasks_pkg.manager import quiesce_running_tasks
    done = _mk_running('cv-q-done')
    done['status'] = 'done'
    running = _mk_running('cv-q-run')
    try:
        n = quiesce_running_tasks(reason='server_shutdown')
        # The already-done task must NOT be counted or flagged.
        assert done.get('aborted') in (False, None), done.get('aborted')
        assert done.get('_abort_reason') is None
        # The running one is flagged.
        assert running['aborted'] is True
        assert n >= 1
    finally:
        _cleanup(done['id'], running['id'])
    _ok('quiesce leaves already-terminal tasks untouched (no double-abort)')


def test_NC_neutered_quiesce_leaves_task_running():
    """NC: if quiesce is a no-op, a running task stays un-aborted → the atexit
    PG-stop would race its DB writes (the cascade). Proves quiesce is
    load-bearing, not incidental."""
    import lib.tasks_pkg.manager as mgr
    orig = mgr.quiesce_running_tasks
    t = _mk_running('cv-q-nc')
    try:
        mgr.quiesce_running_tasks = lambda reason='server_shutdown': 0  # neuter
        mgr.quiesce_running_tasks(reason='server_shutdown')
        assert t['aborted'] is False, 'neutered quiesce must NOT abort (NC baseline)'
        # Now the REAL function does abort it.
        mgr.quiesce_running_tasks = orig
        mgr.quiesce_running_tasks(reason='server_shutdown')
        assert t['aborted'] is True, 'real quiesce must abort the running task'
    finally:
        mgr.quiesce_running_tasks = orig
        _cleanup(t['id'])
    _ok('NC: neutered quiesce leaves task running; real quiesce aborts it')


def main():
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_shutdown_quiesce.__main__')
    print()
    print(_color('═══ shutdown quiesce tests ═══', '36'))
    print()
    tests = [
        test_quiesce_aborts_all_running,
        test_quiesce_leaves_terminal_tasks_untouched,
        test_NC_neutered_quiesce_leaves_task_running,
    ]
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
    print(_color(f'═══ ALL {len(tests)} SHUTDOWN QUIESCE TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
