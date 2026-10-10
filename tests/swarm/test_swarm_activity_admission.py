"""Execution admission, not pool submission, owns visible swarm status.

Demand-driven schedulers/accounting, synthetic workers and event-owned cleanup; no
model, user conversation or persistence is involved.
"""
from __future__ import annotations

import threading
import time

import pytest

from lib.swarm.execution_gate import SwarmExecutionAccounting
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubAgentResult, SubTaskSpec
from lib.swarm.rate_limiter import RateLimiter
from lib.swarm.scheduler import StreamingScheduler

pytestmark = pytest.mark.unit


def _wait(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('worker transition did not arrive')


def _session(monkeypatch, specs, gate, parallel=2):
    from lib.swarm.integration import _state

    master = MasterOrchestrator('admission-run', 'admission-conv', specs, user_id=1)
    entered = {spec.id: threading.Event() for spec in specs}
    release = {spec.id: threading.Event() for spec in specs}
    starts = []

    class Agent:
        model = 'test-model'

        def __init__(self, spec):
            self.agent_id = spec.id
            self.result = SubAgentResult()

        def run(self):
            self.result.status = 'running'
            entered[self.agent_id].set()
            assert release[self.agent_id].wait(5), 'test must release worker'
            self.result.status = 'completed'
            return self.result

    def factory(spec):
        agent = Agent(spec)
        with master._lock:
            master._agents[spec.id] = agent
        return agent

    def completed(spec, result):
        with master._lock:
            master._results_by_id[spec.id] = (spec, result)
            master._results.append((spec, result))

    scheduler = StreamingScheduler(
        agent_factory=factory, max_parallel=parallel,
        rate_limiter=RateLimiter(max_concurrent=parallel, owner_key=1, execution_gate=gate),
        on_agent_start=lambda spec: starts.append((spec.id, master.get_status()[spec.id])),
        on_agent_complete=completed)
    master._scheduler = scheduler
    monkeypatch.setattr(_state, '_get_session', lambda _: master)
    monkeypatch.setattr(_state, '_active_sessions', {'admission-conv': master})
    monkeypatch.setattr(_state, '_session_timestamps', {'admission-conv': time.time()})
    return master, scheduler, entered, release, starts


def test_dependency_ready_work_starts_despite_another_active_session(monkeypatch):
    from lib.swarm.integration import _state
    gate = SwarmExecutionAccounting(capacity=1, waiter_capacity=1)
    gate.acquire('other-session')
    specs = [SubTaskSpec(id='a', objective='inspect parser'),
             SubTaskSpec(id='b', objective='review parser', depends_on=['a'])]
    master, scheduler, entered, release, starts = _session(monkeypatch, specs, gate)
    before = time.time()
    try:
        scheduler.add_specs(specs)
        assert entered['a'].wait(3)
        assert not entered['b'].is_set()
        activity = _state.list_active_swarms(user_id=1)[0]
        status = _state.get_swarm_status('admission-conv', user_id=1)
        assert (activity['running'], activity['pending'], activity['completed']) == (1, 1, 0)
        assert (status['running'], status['pending'], status['completed']) == (1, 1, 0)
        rows = {agent['id']: agent for agent in activity['agents']}
        assert rows['a']['status'] == 'running'
        assert rows['b']['status'] == 'pending'
        assert rows['b']['waitReason'] == 'dependencies'
        assert rows['b']['startedAt'] is None
        assert starts[0][1]['status'] == 'running'
        assert starts[0][1]['startedAt'] >= int(before * 1000)
        snapshot = {agent['id']: agent for agent in master._build_agent_snapshot()['agents']}
        assert snapshot['a']['status'] == 'running'
        assert snapshot['b']['status'] == 'pending'
        release['a'].set()
        assert entered['b'].wait(3)
        activity = _state.list_active_swarms(user_id=1)[0]
        assert (activity['running'], activity['pending'], activity['completed']) == (1, 0, 1)
    finally:
        gate.release('other-session')
        for event in release.values():
            event.set()
        scheduler.shutdown(wait=True, timeout=5)
    assert gate.snapshot()['active'] == gate.snapshot()['waiting'] == 0


@pytest.mark.parametrize('raises', [False, True])
def test_retry_preflight_uses_current_attempt_not_previous_result(monkeypatch, raises):
    from lib.swarm.integration import _state

    gate = SwarmExecutionAccounting(capacity=1, waiter_capacity=4)
    spec = SubTaskSpec(id='retry', objective='inspect parser', max_retries=1)
    master, scheduler, entered, release, starts = _session(monkeypatch, [spec], gate)
    factory = scheduler._factory
    attempts, retry_observations = [], []
    held, retry_release = threading.Event(), threading.Event()

    def retry_factory(spec):
        agent = factory(spec)
        attempts.append(agent)
        if len(attempts) == 1:
            def failed_run():
                agent.result.status = 'failed'
                agent.result.error_message = 'temporary synthetic failure'
                if raises:
                    raise RuntimeError(agent.result.error_message)
                return agent.result
            agent.run = failed_run
        return agent

    def retrying(*args):
        retry_observations.append(master.get_status()[spec.id])
        master._on_retry_callback(*args)
        held.set()
        assert retry_release.wait(5), 'test releases the retry observer'


    frames = []
    master.on_progress = frames.append
    scheduler._factory = retry_factory
    scheduler._on_retry = retrying
    try:
        scheduler.add_specs([spec])
        assert held.wait(3)
        assert retry_observations[0]['status'] == 'pending', 'attempt failure is not child settlement'
        assert retry_observations[0]['error'] == '', 'attempt errors cannot masquerade as child failure'
        assert frames[0]['status'] == frames[0]['phase'] == 'pending'
        assert frames[0]['waitReason'] == 'capacity'
        activity = _state.list_active_swarms(user_id=1)[0]
        assert (activity['running'], activity['pending'], activity['completed']) == (0, 1, 0)
        row = activity['agents'][0]
        assert row['status'] == 'pending' and row['waitReason'] == 'capacity'
        assert row['startedAt'] is None and not scheduler.started_at_map()
        snapshot = master._build_agent_snapshot()['agents'][0]
        assert snapshot['status'] == row['status'] and snapshot['startedAt'] is None
        assert len(starts) == 1, 'retry start cannot publish while waiting'
        before = time.time()
        retry_release.set()
        held.clear()
        assert entered[spec.id].wait(3)
        row = master.get_status()[spec.id]
        assert row['status'] == 'running'
        assert row['startedAt'] >= int(before * 1000)
        assert len(starts) == 2
        assert attempts[0].result.status == 'failed', 'projection never rewrites attempt evidence'
    finally:
        retry_release.set()
        release[spec.id].set()
        scheduler.shutdown(wait=True, timeout=5)
    assert gate.snapshot()['active'] == gate.snapshot()['waiting'] == 0


@pytest.mark.parametrize('limited', [False, True])
def test_stop_during_start_callback_never_enters_worker_or_leaks_permit(limited):
    from types import SimpleNamespace

    abort = threading.Event()
    settled = threading.Event()
    runs, results, retries, starts = [], [], [], []
    gate = SwarmExecutionAccounting(capacity=1, waiter_capacity=4)
    limiter = RateLimiter(max_concurrent=1, owner_key=1, execution_gate=gate)
    spec = SubTaskSpec(id='stopped', objective='inspect parser', max_retries=2)

    def start(spec):
        starts.append(spec.id)
        abort.set()  # A Stop arriving while the observer publishes startup.

    def complete(spec, result):
        results.append(result)
        settled.set()

    scheduler = StreamingScheduler(
        agent_factory=lambda spec: SimpleNamespace(agent_id=spec.id,
            run=lambda: runs.append(spec.id) or SubAgentResult(status='completed')),
        max_parallel=1, rate_limiter=limiter if limited else None,
        abort_check=abort.is_set, on_agent_start=start,
        on_agent_complete=complete, on_retry=lambda *args: retries.append(args))
    try:
        scheduler.add_specs([spec])
        assert settled.wait(3)
    finally:
        abort.set()
        scheduler.shutdown(wait=True, timeout=5)
    assert starts == [spec.id]
    assert not runs, 'Stop during start publication must be checked before run()'
    assert not retries
    assert results[0].status == 'cancelled'
    assert not scheduler.started_at_map()
    assert limiter.active == gate.snapshot()['active'] == gate.snapshot()['waiting'] == 0
    # The local semaphore must also be reusable, not just the global permit.
    deadline = time.monotonic() + 1
    limiter.acquire(abort_check=lambda: time.monotonic() >= deadline)
    limiter.release()


def test_stop_removes_dependency_wait_from_rows_snapshots_and_completion_notice(monkeypatch):
    from lib.swarm import master as master_module
    from lib.swarm.integration import _state

    specs = [SubTaskSpec(id='a', objective='inspect parser'),
             SubTaskSpec(id='b', objective='review parser', depends_on=['a'])]
    gate = SwarmExecutionAccounting(capacity=1, waiter_capacity=4)
    master, scheduler, entered, release, _ = _session(monkeypatch, specs, gate)
    master._resolve_spec_model = lambda _: 'test-model'
    scheduler._abort_check = lambda: master._aborted
    factory = scheduler._factory

    def cancellable_factory(spec):
        agent = factory(spec)
        run = agent.run

        def run_with_stop():
            result = run()
            if master._aborted:
                result.status = 'cancelled'
            return result

        agent.run = run_with_stop
        return agent

    scheduler._factory = cancellable_factory
    notifications = []
    monkeypatch.setattr(master_module, 'inbox_enqueue', lambda *args, **kwargs: None)
    monkeypatch.setattr(master_module, 'format_swarm_update',
                        lambda **kwargs: notifications.append(kwargs) or 'synthetic notice')
    completed = scheduler._on_complete

    def publish(spec, result):
        completed(spec, result)
        master._publish_completion_inbox(spec, result)

    scheduler._on_complete = publish
    try:
        scheduler.add_specs(specs)
        assert entered['a'].wait(3)
        assert scheduler.pending_count == 1
        master.abort()
        assert scheduler.pending_count == 0

        assert master.get_status()['a']['status'] == 'running', 'Stop does not fake worker completion'
        assert master.get_status()['b']['status'] == 'aborted'
        # Simulate the admitted worker observing Stop, then returning normally.
        release['a'].set()
        _wait(lambda: scheduler.running_count == 0)
        assert notifications[-1]['remaining_running'] == 0
        assert notifications[-1]['remaining_pending'] == 0
        assert not entered['b'].is_set()
        row = master.get_status()['b']
        snapshot = next(row for row in master._build_agent_snapshot()['agents'] if row['id'] == 'b')
        assert row['status'] == snapshot['status'] == 'aborted'
        assert 'waitReason' not in row and 'waitReason' not in snapshot
        activity = _state.list_active_swarms(user_id=1)[0]
        assert (activity['running'], activity['pending'], activity['completed']) == (0, 0, 2)
    finally:
        for event in release.values():
            event.set()
        scheduler.shutdown(wait=True, timeout=5)
    assert gate.snapshot()['active'] == gate.snapshot()['waiting'] == 0


@pytest.mark.parametrize('outcome', ['aborted', 'stalled', 'unknown'])
def test_terminated_missing_result_never_reports_waiting_or_running(outcome):
    from types import SimpleNamespace

    spec = SubTaskSpec(id='unfinished', objective='inspect parser')
    master = MasterOrchestrator('terminated-run', 'terminated-conv', [spec], user_id=1)
    master._resolve_spec_model = lambda _: 'test-model'
    master._scheduler = SimpleNamespace(started_at_map=lambda: {spec.id: time.time() - 20})
    master._terminated = True
    master._aborted = outcome == 'aborted'
    if outcome == 'stalled':
        master._stalled_agents[spec.id] = {'silent_seconds': 900, 'note': 'no recent progress'}
    snapshot = master._build_agent_snapshot()['agents'][0]
    row = master.get_status()[spec.id]
    assert row['status'] == snapshot['status'] == outcome
    assert row['startedAt'] is None
    assert 'waitReason' not in row


def test_independent_agents_use_free_slots_without_waiting_for_parent_or_sibling(monkeypatch):
    gate = SwarmExecutionAccounting(capacity=2, waiter_capacity=4)
    specs = [SubTaskSpec(id='a', objective='inspect parser'),
             SubTaskSpec(id='b', objective='inspect transport')]
    master, scheduler, entered, release, starts = _session(monkeypatch, specs, gate)
    try:
        scheduler.add_specs(specs)
        assert entered['a'].wait(3)
        assert entered['b'].wait(3), 'both workers must enter before either completes'
        assert gate.snapshot()['active'] == gate.snapshot()['peakActive'] == 2
        assert all(row['status'] == 'running' for row in master.get_status().values())
        assert len(starts) == 2
    finally:
        for event in release.values():
            event.set()
        scheduler.shutdown(wait=True, timeout=5)
    assert gate.snapshot()['active'] == gate.snapshot()['waiting'] == 0
