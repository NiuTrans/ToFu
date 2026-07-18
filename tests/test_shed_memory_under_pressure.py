#!/usr/bin/env python3
"""Active memory back-pressure valve (lever C for the shared-cgroup OOM).

When the cgroup nears its limit, ``shed_memory_under_pressure`` must reclaim
our own RSS BEFORE the kernel OOM-killer fires — because we are typically the
single largest RSS in the shared pod (highest oom_score) even at a small % of
the cgroup, so shedding lowers OUR kill probability.

Asserts, against the REAL ``lib.tasks_pkg.manager`` / ``TaskRuntime``:

  * ``cleanup_stale(max_age=0)`` evicts EVERY terminal task immediately,
    bypassing the ttl=3600s retention window (the shed's core move);
  * ``cleanup_stale()`` with no override still honours the normal TTL (a fresh
    terminal task is NOT evicted) — the aggressive path is opt-in;
  * a RUNNING task is never evicted by either path;
  * ``shed_memory_under_pressure`` returns a diagnostic dict and drops the
    terminal tasks from the registry while leaving the running one.

NC: neuter the max_age override (force it back to self.ttl) → a shed pass then
evicts NOTHING, proving the override is the load-bearing part of the valve.

Uses an isolated TaskRuntime for the cleanup_stale unit tests (no global
state); the shed_memory test uses the real _chat_runtime and cleans up after.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _fresh_runtime():
    from lib.agent_core.task_runtime import TaskRuntime
    return TaskRuntime('shed-test', ttl=3600)


def _add_terminal(rt, tid, *, status='done', age=0.0):
    t = rt.create(task_id=tid)
    t['status'] = status
    t['finished_at'] = time.time() - age
    return t


def test_max_age_zero_evicts_all_terminal_now():
    rt = _fresh_runtime()
    _add_terminal(rt, 'a', status='done', age=0.0)     # just finished
    _add_terminal(rt, 'b', status='error', age=1.0)
    _add_terminal(rt, 'c', status='aborted', age=2.0)
    assert rt.task_count == 3
    n = rt.cleanup_stale(max_age=0)
    assert n == 3, f'max_age=0 should evict all 3 terminal tasks, evicted {n}'
    assert rt.task_count == 0
    _ok('cleanup_stale(max_age=0) evicts every terminal task immediately')


def test_normal_ttl_keeps_fresh_terminal():
    rt = _fresh_runtime()
    _add_terminal(rt, 'a', status='done', age=0.0)   # fresh — inside 3600s TTL
    n = rt.cleanup_stale()                            # normal path, no override
    assert n == 0, 'normal TTL wrongly evicted a fresh terminal task'
    assert rt.task_count == 1
    _ok('cleanup_stale() (no override) still honours the normal TTL')


def test_running_task_never_evicted():
    rt = _fresh_runtime()
    t = rt.create(task_id='run1')
    t['status'] = 'running'
    n = rt.cleanup_stale(max_age=0)   # even the aggressive path
    assert n == 0, 'a RUNNING task was evicted — must never happen'
    assert rt.task_count == 1
    _ok('running task is never evicted, even under max_age=0')


def test_shed_evicts_terminal_keeps_running():
    from lib.tasks_pkg.manager import shed_memory_under_pressure, _chat_runtime
    rt = _chat_runtime
    tids = ['shed-done-1', 'shed-err-1', 'shed-run-1']
    # clean slate for our ids
    for tid in tids:
        rt._tasks.pop(tid, None)
    d = rt.create(task_id='shed-done-1'); d['status'] = 'done'; d['finished_at'] = time.time()
    e = rt.create(task_id='shed-err-1'); e['status'] = 'error'; e['finished_at'] = time.time()
    r = rt.create(task_id='shed-run-1'); r['status'] = 'running'
    try:
        res = shed_memory_under_pressure()
        assert isinstance(res, dict) and 'evicted' in res, 'shed returned no diagnostic dict'
        assert res['evicted'] >= 2, f'shed evicted only {res["evicted"]} (expected >=2)'
        assert rt.get('shed-done-1') is None and rt.get('shed-err-1') is None, \
            'terminal tasks survived the shed'
        assert rt.get('shed-run-1') is not None, 'shed wrongly evicted the running task'
    finally:
        for tid in tids:
            rt._tasks.pop(tid, None)
    _ok('shed_memory_under_pressure evicts terminal tasks, keeps running (returns diagnostic)')


_POSITIVE = [
    test_max_age_zero_evicts_all_terminal_now,
    test_normal_ttl_keeps_fresh_terminal,
    test_running_task_never_evicted,
    test_shed_evicts_terminal_keeps_running,
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


def _neuter_and_check():
    """NC: monkeypatch cleanup_stale to IGNORE max_age (always use self.ttl).
    A shed pass then evicts a just-finished terminal task = 0, proving the
    max_age override is load-bearing for the valve."""
    from lib.agent_core.task_runtime import TaskRuntime
    rt = _fresh_runtime()
    _add_terminal(rt, 'x', status='done', age=0.0)
    orig = TaskRuntime.cleanup_stale
    try:
        # Neutered: drop the override, always sweep at the full TTL.
        TaskRuntime.cleanup_stale = lambda self, max_age=None: orig(self, None)
        n = rt.cleanup_stale(max_age=0)   # caller asks for aggressive, neuter ignores
        return (n == 0 and rt.task_count == 1), f'evicted={n} count={rt.task_count}'
    finally:
        TaskRuntime.cleanup_stale = orig


def main():
    print()
    print(_color('═══ memory back-pressure valve (shed) + neuter ═══', '36'))
    print()
    print(_color('Baseline (shipped shed + max_age override):', '36'))
    if not all(_run(fn) for fn in _POSITIVE):
        _fail('baseline failed — fix cleanup_stale(max_age)/shed first')
    print()
    print(_color('NC — neuter the max_age override:', '36'))
    held, out = _neuter_and_check()
    if not held:
        _fail('NC did not confirm the override is load-bearing:\n' + out)
    _ok('NC: with max_age ignored, the aggressive shed evicts nothing (override is load-bearing)')
    print()
    print(_color('═══ ALL SHED TESTS + NEUTER PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
