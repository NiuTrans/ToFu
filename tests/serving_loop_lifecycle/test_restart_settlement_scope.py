"""Restart settlement owns only Attempts a previous server generation left.

Personal composition runs an API host and a resident execution host. Boot
recovery and its post-serving backstop run in the execution host, but the API
host runs its own executors: HTTP-initiated turns and the continuations its
task-maintenance tick births from background-command receipts. On the
2026-11-06 restart the API host resumed two Worker turns at 11:04:22 while the
execution host was still recovering; its backstop then judged liveness by its
own gate time and its own task registry and settled both live Attempts
``interrupted`` at 11:06:43, killing them. These specs replay that sequence on
an in-memory turn authority with the same ``turn.recover`` contract as the SQL
and native authorities (frozen creation cutoff plus excluded bound task ids).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from lib.app.app_factory import create_base_app
from lib.serving_loop_lifecycle import register_serving_loop_lifecycle


pytestmark = pytest.mark.unit

PREVIOUS_GENERATION_MS = 1_000
BOOT_SCOPE_MS = 2_000
API_HOST_RESUME_MS = 3_000
EXECUTION_GATE_MS = 4_000


class _TurnAuthority:
    """``turn.recover`` exactly as both authorities implement it."""

    def __init__(self, attempts):
        self.attempts = attempts
        self.recover_payloads = []

    def command(self, operation, payload, command_id, deadline=None):
        assert operation == 'turn.recover'
        self.recover_payloads.append(dict(payload))
        excluded = set(payload.get('exclude_task_ids') or ())
        recovered = 0
        for attempt in self.attempts.values():
            if (attempt['status'] in ('pending', 'running')
                    and attempt['createdAt'] < payload['created_before_ms']
                    and attempt['taskId'] not in excluded):
                attempt['status'] = 'interrupted'
                recovered += 1
        return {'recovered': recovered, 'remaining': 0}


class _TaskResults:
    def __init__(self):
        self.pending = True

    def query(self, operation, payload, deadline=None):
        assert operation == 'task_results.recovery_pending'
        return {'pending': self.pending}

    def command(self, operation, payload, command_id, deadline=None):
        assert operation == 'task_results.recover_running'
        return {'recovered': [], 'remaining': False}


class _Runtime:
    def __init__(self):
        self.tasks = []

    def create_task(self, awaitable, *, name):
        task = asyncio.get_running_loop().create_task(awaitable, name=name)
        self.tasks.append((name, task))
        return task

    async def stop(self):
        for _name, task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*(task for _name, task in self.tasks),
                             return_exceptions=True)


@pytest.fixture
def restart(monkeypatch):
    import lib.auto_restart as auto_restart
    import lib.serving_loop_lifecycle as lifecycle
    import lib.storage
    import lib.tasks_pkg.manager._recovery as recovery
    import lib.tasks_pkg.manager.runtime as task_runtime
    import lib.turn_lifecycle as turns

    attempts = {
        # Running in the server process that the restart stopped.
        'previous-generation': {'status': 'running', 'taskId': 'project-worker:lane-a',
                                'createdAt': PREVIOUS_GENERATION_MS},
    }
    authority = _TurnAuthority(attempts)
    task_results = _TaskResults()
    clock = {'ms': BOOT_SCOPE_MS}
    monkeypatch.setattr(turns, '_turn_client', lambda write=False: authority)
    monkeypatch.setattr(turns, '_now_ms', lambda: clock['ms'])
    monkeypatch.setattr(recovery, 'time',
                        SimpleNamespace(time=lambda: clock['ms'] / 1000), raising=False)
    monkeypatch.setattr(recovery, '_restart_settlement_scope_ms', None, raising=False)
    monkeypatch.setattr(lib.storage, 'get_storage_client',
                        lambda write=False: task_results)
    monkeypatch.setattr(task_runtime, 'chat_task_runtime', SimpleNamespace(
        task_ids=lambda: frozenset({'task-registered-here'})))
    # The backstop's settle delay and the gate clock run on the fixture clock.
    monkeypatch.setattr(lifecycle, '_time', SimpleNamespace(
        time=lambda: clock['ms'] / 1000, sleep=lambda _seconds: None))
    runtime = _Runtime()
    monkeypatch.setattr(lifecycle, '_create_debug_guard',
                        lambda *_a, **_k: SimpleNamespace(stop=lambda: None))
    monkeypatch.setattr(lifecycle, '_create_loop_runtime', lambda *_a, **_k: runtime)
    monkeypatch.setattr(lifecycle, '_load_write_freshness_snapshot', lambda: None)
    monkeypatch.setattr(lifecycle, '_redispatch_orphaned_queue', lambda: [])
    monkeypatch.setattr(lifecycle, '_recover_dispatchable_attempts', lambda _cutoff: {})
    monkeypatch.setattr(auto_restart, 'stop_auto_restart_watch', lambda *, timeout: None)
    return SimpleNamespace(attempts=attempts, authority=authority, task_results=task_results,
                           clock=clock, runtime=runtime, recovery=recovery)


def _boot_recovery(restart):
    restart.clock['ms'] = BOOT_SCOPE_MS
    restart.recovery.recover_stale_tasks_on_startup(prev_shutdown={'verdict': 'clean'})


def _api_host_resumes_lane(restart):
    """The API host's maintenance tick resumes the paused lane in ITS process."""
    restart.clock['ms'] = API_HOST_RESUME_MS
    restart.attempts['resumed-by-api-host'] = {
        'status': 'running', 'taskId': 'project-worker:lane-a',
        'createdAt': API_HOST_RESUME_MS}


def _serve_and_run_backstop(restart):
    """Open the execution host's gate, then run its one backstop pass."""
    app = create_base_app('restart-settlement-scope', {'TESTING': True})
    stop = threading.Event()
    register_serving_loop_lifecycle(
        app, shutdown_requested=stop, hooks=object(), environ={},
        logger=logging.getLogger('test.restart-scope'), process_role='execution')

    async def exercise():
        async with app.test_app():
            restart.clock['ms'] = EXECUTION_GATE_MS
            app.extensions['tofu_production_startup_gate'].set()
            backstop = next(task for name, task in restart.runtime.tasks
                            if name == 'tofu-turn-recovery-backstop')
            await asyncio.wait_for(backstop, timeout=30)

    asyncio.run(exercise())


def test_backstop_keeps_a_lane_another_process_resumed_after_boot_recovery(restart):
    _boot_recovery(restart)
    assert restart.attempts['previous-generation']['status'] == 'interrupted'
    _api_host_resumes_lane(restart)

    _serve_and_run_backstop(restart)

    # The resumed Attempt is live in the API host: the execution host's
    # registry cannot vouch for it, and it is younger than the boot scope.
    assert restart.attempts['resumed-by-api-host']['status'] == 'running'
    boot_sweep, backstop_sweep = restart.authority.recover_payloads
    assert backstop_sweep['created_before_ms'] == boot_sweep['created_before_ms'] == BOOT_SCOPE_MS


def test_backstop_still_settles_what_boot_recovery_missed_inside_its_scope(restart):
    # A false-negative orphan probe skips the boot sweep entirely.
    restart.task_results.pending = False
    _boot_recovery(restart)
    assert restart.attempts['previous-generation']['status'] == 'running'
    # Accepted before the restart, then claimed after the gate by this
    # process's accepted-attempt dispatch recovery: its executor runs here.
    restart.attempts['claimed-here'] = {
        'status': 'running', 'taskId': 'task-registered-here',
        'createdAt': PREVIOUS_GENERATION_MS}
    _api_host_resumes_lane(restart)

    _serve_and_run_backstop(restart)

    assert restart.attempts['previous-generation']['status'] == 'interrupted'
    assert restart.attempts['resumed-by-api-host']['status'] == 'running'
    assert restart.attempts['claimed-here']['status'] == 'running'
    assert [payload['created_before_ms'] for payload in restart.authority.recover_payloads] == [
        BOOT_SCOPE_MS]


def test_backstop_settles_nothing_when_this_process_began_no_restart_settlement(restart):
    # E.g. a distributed read-only preview returns before boot recovery.
    _api_host_resumes_lane(restart)

    _serve_and_run_backstop(restart)

    assert restart.authority.recover_payloads == []
    assert all(attempt['status'] == 'running' for attempt in restart.attempts.values())
