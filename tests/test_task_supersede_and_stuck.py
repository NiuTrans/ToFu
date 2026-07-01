#!/usr/bin/env python3
"""Negative-control tests for the task-supersede invariant + zombie/stuck
terminal-floor fix — the root-cause fix for the "task not found" (404) incident
where a superseded-but-never-aborted task (222c0f68) wedged with 0 output for
hours, never persisted, and 404'd after a restart.

Four properties, three with a load-bearing negative control:

  A. ``create_task(supersede=True)`` (the DEFAULT) auto-aborts any prior
     running task for the same conv. This is the single invariant that now
     covers the previously-uncovered background paths — queue
     ``dispatch_next_queued`` (lib/message_queue.py) and scheduler
     ``inject_and_run_task`` (lib/scheduler/_shared.py) both call
     ``create_task`` with the default, so "a new task replaced the old one
     without aborting it" is structurally impossible.
     NC (env TOFU_NC_REVERT=abort): neuter the abort convergence → the
     superseded task stays never-aborted (the exact incident shape) → FAILS.

  B. A just-aborted task gets a terminal ``task_results`` row IMMEDIATELY, so a
     poll that outlives the in-memory task (server restart cleared the
     registry) resolves to a terminal ``aborted`` state instead of 404 (turn
     lost). NC (env TOFU_NC_REVERT=floor OR =abort): neuter the terminal-floor
     write → post-eviction poll returns 404 → FAILS.

  C. ``chat_branch_start`` passes ``supersede=False`` so a branch — an
     intentional concurrency axis — does NOT abort the main task or sibling
     branches. Protective regression: proves the exemption keeps the axis
     alive. (Always-on positive; unaffected by the NC toggles.)

  D. ``reap_stuck_running_tasks`` force-fails a purely-wedged task (0 events /
     0 content past the silence threshold — the 222c0f68 shape) AND writes a
     terminal floor, while NEVER touching a task legitimately blocked on human
     input (>=1 event emitted). The discriminator (human-waiting survives) is
     itself the load-bearing proof for the zero-events gate.

Run states (env TOFU_NC_REVERT):
  unset   → all pass
  =abort  → supersede convergence neutered → A + B FAIL
  =floor  → terminal-floor write neutered  → B FAILS
"""

import asyncio
import importlib.util
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install Flask→Quart shim before importing routes
import quart as _quart  # noqa: E402
sys.modules['flask'] = _quart


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


_REVERT = os.environ.get('TOFU_NC_REVERT', '').strip()


def _apply_negative_control():
    """Monkeypatch the load-bearing logic to a no-op per TOFU_NC_REVERT.

    Monkeypatch (not source edits) → auto-restored when the process exits, so
    the shipped source is byte-identical regardless of run state.
    """
    from lib.tasks_pkg import manager
    if _REVERT == 'abort':
        # Neuter the supersede convergence: create_task's abort sweep becomes
        # a no-op, reproducing the "never aborted" incident path.
        manager.abort_running_tasks_for_conv = lambda *a, **k: 0
        print(_color('  [NC] abort convergence NEUTERED '
                     '(create_task will NOT supersede)', '33'))
    elif _REVERT == 'floor':
        # Neuter the terminal-floor write: aborted tasks get no durable
        # task_results row, reproducing the post-restart 404.
        manager._write_aborted_terminal_floor = lambda *a, **k: None
        print(_color('  [NC] terminal-floor write NEUTERED '
                     '(no aborted row persisted)', '33'))


_APP = None


def _get_app():
    global _APP
    if _APP is not None:
        return _APP
    from lib import auth_mode as _auth_mode
    os.environ.pop('TOFU_AUTH_MODE', None)
    _auth_mode.reset_for_tests()
    _auth_mode.set_mode('open', set_by='supersede-stuck-test')
    spec = importlib.util.spec_from_file_location(
        'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'server.py'))
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    _APP = mod.app
    return _APP


def _poll_status_after_eviction(task_id):
    """Evict the task from the in-memory registry (simulate a server restart)
    then poll it over HTTP. Returns (http_status, body_status_or_None)."""
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        tasks.pop(task_id, None)
    app = _get_app()

    async def _do():
        async with app.test_client() as client:
            r = await client.get(f'/api/v1/chat/poll/{task_id}')
            body = None
            try:
                data = await r.get_json()
                body = (data or {}).get('status')
            except Exception:
                body = None
            return r.status_code, body

    return asyncio.run(_do())


def _cleanup_task_results(conv_ids):
    from lib.database import DOMAIN_CHAT, get_thread_db, db_execute_with_retry
    db = get_thread_db(DOMAIN_CHAT)
    for cid in conv_ids:
        try:
            db_execute_with_retry(db, 'DELETE FROM task_results WHERE conv_id=?', (cid,))
        except Exception:
            pass
    try:
        db.commit()
    except Exception:
        pass


# ── A. supersede invariant (queue-dispatch / scheduler fix) ──────────

def test_create_task_supersedes_prior_running_task():
    from lib.tasks_pkg.manager import create_task
    conv = 'cv-supersede-A'
    try:
        t_old = create_task(conv, [{'role': 'user', 'content': 'old'}], {})
        # t_old is the sole task → nothing aborted yet.
        assert t_old['aborted'] is False, 'first task should not self-abort'
        # A background path (queue dispatch / scheduler) creates the next task
        # for the same conv via the DEFAULT create_task — must supersede t_old.
        t_new = create_task(conv, [{'role': 'user', 'content': 'new'}], {})
        assert t_old['aborted'] is True, (
            'prior running task was NOT aborted by create_task supersede — '
            'this is the 222c0f68 "never aborted" incident shape')
        assert t_old['_abort_reason'] == 'superseded_by_new_task'
        assert t_new['aborted'] is False
    finally:
        _cleanup_task_results([conv])
    _ok('create_task(default) supersedes the prior running task for the conv')


# ── B. aborted task survives restart as a terminal state (not 404) ───

def test_superseded_task_polls_terminal_after_restart_not_404():
    from lib.tasks_pkg.manager import create_task
    conv = 'cv-supersede-B'
    try:
        t_old = create_task(conv, [{'role': 'user', 'content': 'old'}], {})
        t_old['content'] = 'partial work before wedge'
        # New task supersedes t_old → abort sweep writes t_old's terminal floor.
        create_task(conv, [{'role': 'user', 'content': 'new'}], {})

        # Simulate a server restart: the in-memory registry is cleared, so the
        # only surviving state is the task_results row (if it was written).
        http_status, body_status = _poll_status_after_eviction(t_old['id'])
        assert http_status == 200, (
            f'poll after restart returned {http_status} (expected 200 terminal); '
            f'404 here == the incident: turn lost because no terminal row exists')
        assert body_status == 'aborted', (
            f'expected terminal status=aborted, got {body_status!r}')
    finally:
        _cleanup_task_results([conv])
    _ok('superseded task polls terminal (aborted) after restart — not 404')


# ── C. branch opt-out (protective regression) ────────────────────────

def test_branch_supersede_false_does_not_abort_siblings():
    from lib.tasks_pkg.manager import create_task
    conv = 'cv-branch-C'
    try:
        t_main = create_task(conv, [{'role': 'user', 'content': 'main'}], {})
        # A branch is a deliberate concurrency axis → supersede=False.
        t_branch = create_task(conv, [{'role': 'user', 'content': 'branch'}],
                               {}, supersede=False)
        assert t_main['aborted'] is False, (
            'branch (supersede=False) must NOT abort the main task — '
            'that would break the intentional concurrency axis')
        assert t_branch['aborted'] is False
        # A sibling branch, also supersede=False, likewise leaves both alone.
        t_branch2 = create_task(conv, [{'role': 'user', 'content': 'branch2'}],
                                {}, supersede=False)
        assert t_main['aborted'] is False
        assert t_branch['aborted'] is False
        assert t_branch2['aborted'] is False
    finally:
        _cleanup_task_results([conv])
    _ok('branch supersede=False keeps main + sibling branches running')


# ── D. stuck reaper: wedged reaped, human-waiting spared ─────────────

def test_stuck_reaper_fails_wedged_but_spares_human_waiting():
    from lib.tasks_pkg.manager import (create_task, append_event,
                                        reap_stuck_running_tasks)
    conv_w, conv_h = 'cv-stuck-wedged', 'cv-stuck-human'
    try:
        # Wedged: 0 events, 0 content, ancient — the 222c0f68 shape.
        tw = create_task(conv_w, [{'role': 'user', 'content': 'q'}], {})
        tw['created_at'] = time.time() - 100000

        # Legitimately blocked on human input: ancient, but has emitted >=1
        # event (the ask_user phase), so it must be SPARED.
        th = create_task(conv_h, [{'role': 'user', 'content': 'q'}], {})
        th['created_at'] = time.time() - 100000
        append_event(th, {'type': 'phase', 'phase': 'awaiting_user',
                          'detail': 'ask_user'})

        n = reap_stuck_running_tasks()

        assert tw['aborted'] is True, 'wedged task was not reaped'
        assert tw['status'] == 'error'
        assert tw['_abort_reason'] == 'stuck_no_output'
        assert tw.get('error'), 'reaped task must carry an error envelope'
        assert th['aborted'] is False, (
            'human-waiting task (>=1 event) was WRONGLY reaped — the '
            'zero-events discriminator is not load-bearing')
        assert n >= 1

        # And the reaped task polls terminal after a restart, not 404.
        http_status, body_status = _poll_status_after_eviction(tw['id'])
        assert http_status == 200, f'reaped task 404 after restart ({http_status})'
        assert body_status == 'error', f'expected error, got {body_status!r}'
    finally:
        _cleanup_task_results([conv_w, conv_h])
    _ok('stuck reaper force-fails wedged task, spares human-waiting, no 404')


# ── E. terminal-floor write tolerates a task missing created_at ──────

def test_aborted_floor_write_survives_missing_created_at():
    """Regression: ``_write_aborted_terminal_floor`` → ``_upsert_task_row``
    must not KeyError when the task dict lacks ``created_at``.

    The incident (autopilot ``t-vu`` task) crashed at manager.py's upsert with
    ``KeyError: 'created_at'`` because that ONE column read used ``task['...']``
    while the rest of the module uses ``.get(...)``. A best-effort floor writer
    that raises loses the durable terminal row → the very 404 it exists to
    prevent. Build a synthetic task WITHOUT created_at and prove the floor is
    still written and polls terminal after a restart.
    """
    from lib.tasks_pkg.manager import (_write_aborted_terminal_floor, tasks,
                                        tasks_lock)
    conv = 'cv-floor-nocreated'
    tid = 'task-nocreated-at'
    task = {
        'id': tid, 'convId': conv, 'status': 'aborted', 'aborted': True,
        'content': 'partial', 'thinking': '', 'error': None,
        'toolRounds': [], 'events': [],
        # NOTE: deliberately NO 'created_at' key — the incident shape.
    }
    with tasks_lock:
        tasks[tid] = task
    try:
        # Must NOT raise (previously KeyError: 'created_at').
        _write_aborted_terminal_floor(task)
        http_status, body_status = _poll_status_after_eviction(tid)
        assert http_status == 200, (
            f'floor not written for created_at-less task → poll {http_status} '
            '(the KeyError swallowed the durable row → 404 incident)')
        assert body_status == 'aborted', f'expected aborted, got {body_status!r}'
    finally:
        with tasks_lock:
            tasks.pop(tid, None)
        _cleanup_task_results([conv])
    _ok('aborted terminal floor writes even when task has no created_at')


# NC-sensitive tests (fail when their guard is neutered).
_NC_SENSITIVE = {
    'abort': ('test_create_task_supersedes_prior_running_task',
              'test_superseded_task_polls_terminal_after_restart_not_404'),
    'floor': ('test_superseded_task_polls_terminal_after_restart_not_404',),
}


def main():
    print()
    label = f' (NC revert={_REVERT})' if _REVERT else ' (clean)'
    print(_color(f'═══ Task Supersede + Stuck-Reaper Tests{label} ═══', '36'))
    print()
    _apply_negative_control()
    print()

    tests = [
        test_create_task_supersedes_prior_running_task,
        test_superseded_task_polls_terminal_after_restart_not_404,
        test_branch_supersede_false_does_not_abort_siblings,
        test_stuck_reaper_fails_wedged_but_spares_human_waiting,
        test_aborted_floor_write_survives_missing_created_at,
    ]
    expect_fail = set(_NC_SENSITIVE.get(_REVERT, ()))
    any_unexpected = False
    for fn in tests:
        should_fail = fn.__name__ in expect_fail
        try:
            fn()
            if should_fail:
                _fail(f'{fn.__name__}: EXPECTED to FAIL under NC={_REVERT} but PASSED '
                      '(guard is NOT load-bearing!)')
        except AssertionError as e:
            if should_fail:
                print(' ', _color('✓ (expected FAIL)', '33'),
                      f'{fn.__name__}: {e}')
            else:
                any_unexpected = True
                print(' ', _color('✗', '31'), f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            any_unexpected = True
            print(' ', _color('✗', '31'),
                  f'{fn.__name__}: unexpected {type(e).__name__}: {e}')

    print()
    if any_unexpected:
        _fail('unexpected failures — see above')
    if _REVERT:
        print(_color(f'═══ NC={_REVERT}: expected failures observed, guards proven '
                     'load-bearing ═══', '33'))
    else:
        print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
