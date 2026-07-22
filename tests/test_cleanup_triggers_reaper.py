"""tests/test_cleanup_triggers_reaper.py — the stuck-task reaper is actually
SCHEDULED, not merely defined.

The incident this guards against: ``reap_stuck_running_tasks`` was fully
implemented and unit-tested (see test_stuck_task_reaper.py), but the ONE line
that wired it into the periodic maintenance tick got stranded as dead code —
it sat AFTER the ``return`` in ``shed_memory_under_pressure`` (unreachable),
while the 60s ``cleanup_old_tasks`` tick that server.py actually runs kept only
a comment ("rides the same tick ... See reap_stuck_running_tasks") with the
call removed. Result: wedged ``status='running'`` zombies were never reaped, so
the self-update restart guard's ``list_running_tasks`` count grew without bound
(the "63 other conversations have running tasks" false positive) and only a
process restart ever cleared it.

The pre-existing reaper tests only proved the reaper WORKS when called — they
never proved it IS called on the maintenance tick, so the dead code hid for a
long time. These tests close that gap:

  1. ``cleanup_old_tasks()`` invokes ``reap_stuck_running_tasks`` every call.
  2. Structural (AST) guard: the reaper call lives in ``cleanup_old_tasks`` and
     is NOT stranded after ``shed_memory_under_pressure``'s return.
  3. End-to-end: a genuinely-wedged running task is flipped terminal by a plain
     ``cleanup_old_tasks()`` tick (full wiring, no direct reaper call).
"""

import ast
import inspect
import os
import threading
import time

import pytest

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────
# (1) The regression: cleanup_old_tasks MUST trigger the reaper every tick.
#     Patch the reaper at its DEFINING module (_maintenance) — that is the
#     binding cleanup_old_tasks resolves at call time — and count invocations.
# ─────────────────────────────────────────────────────────────────────────
def test_cleanup_old_tasks_triggers_reaper(monkeypatch):
    from lib.tasks_pkg.manager import _maintenance

    calls = {'n': 0}

    def _counting_reaper():
        calls['n'] += 1
        return 0

    monkeypatch.setattr(_maintenance, 'reap_stuck_running_tasks',
                        _counting_reaper, raising=True)

    _maintenance.cleanup_old_tasks()
    assert calls['n'] == 1, \
        'cleanup_old_tasks must call reap_stuck_running_tasks once per tick'

    _maintenance.cleanup_old_tasks()
    assert calls['n'] == 2, 'every cleanup tick must trigger the reaper'


# ─────────────────────────────────────────────────────────────────────────
# (2) The reaper failing must NOT break the cleanup tick (it is a best-effort
#     backstop). Guards the try/except around the call.
# ─────────────────────────────────────────────────────────────────────────
def test_reaper_failure_does_not_break_cleanup(monkeypatch):
    from lib.tasks_pkg.manager import _maintenance

    def _boom():
        raise RuntimeError('reaper blew up')

    monkeypatch.setattr(_maintenance, 'reap_stuck_running_tasks', _boom,
                        raising=True)
    # Must swallow the reaper's exception — TTL cleanup is the primary job.
    _maintenance.cleanup_old_tasks()


# ─────────────────────────────────────────────────────────────────────────
# (3) Structural guard (AST): encode the exact fix so the accident cannot
#     silently return. The reaper call must be INSIDE cleanup_old_tasks and
#     must NOT appear inside shed_memory_under_pressure (where it was stranded
#     as unreachable dead code after the return).
# ─────────────────────────────────────────────────────────────────────────
def _calls_reaper(fn) -> bool:
    """True if fn's source contains a call to reap_stuck_running_tasks()."""
    tree = ast.parse(inspect.getsource(fn))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == 'reap_stuck_running_tasks'):
            return True
    return False


def test_reaper_wired_into_cleanup_not_shed():
    from lib.tasks_pkg.manager import _maintenance

    assert _calls_reaper(_maintenance.cleanup_old_tasks), \
        'reap_stuck_running_tasks() must be called from cleanup_old_tasks'
    assert not _calls_reaper(_maintenance.shed_memory_under_pressure), \
        ('reap_stuck_running_tasks() must NOT live in shed_memory_under_pressure '
         '(it was stranded there as dead code after the return)')


# ─────────────────────────────────────────────────────────────────────────
# (4) End-to-end wiring: a genuinely-wedged running task is reaped by a plain
#     cleanup_old_tasks() tick — proving the whole chain, not just that a stub
#     got called. Stubs only the terminal-floor finalizer (DB/conv side
#     effects) so this stays a pure in-memory unit test.
# ─────────────────────────────────────────────────────────────────────────
def test_wedged_task_reaped_by_cleanup_tick(monkeypatch):
    from lib.tasks_pkg import tasks, tasks_lock
    from lib.tasks_pkg.manager import _maintenance

    monkeypatch.setenv('TOFU_STUCK_TASK_MAX_SILENT_SECS', '300')
    # Keep it in-memory: the reaper sets the terminal transition BEFORE the
    # finalizer runs, and the finalizer only does DB/conv/SSE fan-out.
    monkeypatch.setattr(_maintenance, '_finalize_reaped_stuck_task',
                        lambda t: None, raising=True)

    now = time.time()
    stale = now - 400  # both clocks past the 300s threshold
    tid = 'cleanup-wedged-1'
    task = {
        'id': tid,
        'convId': 'cv-' + tid,
        'status': 'running',
        'aborted': False,
        'content': '',
        'thinking': '',
        'events': [],
        'events_lock': threading.Lock(),
        'config': {'model': 'aws.claude-opus-4.8'},
        'created_at': stale,
        '_t_last_event': stale,
        '_dispatch_heartbeat': stale,
    }
    with tasks_lock:
        tasks[tid] = task
    try:
        _maintenance.cleanup_old_tasks()  # the plain 60s tick — no direct reap call
        with tasks_lock:
            t = dict(tasks.get(tid) or {})
        assert t.get('status') == 'error', \
            'a wedged running task must be flipped terminal by the cleanup tick'
        assert t.get('aborted') is True
        assert t.get('_abort_reason') == 'stuck_no_progress'
    finally:
        with tasks_lock:
            tasks.pop(tid, None)
