"""Executable resource and cost contracts for background swarm agents."""

from __future__ import annotations

import json
import threading
import time

import pytest

from lib.llm_errors import AbortedError
from lib.swarm.execution_gate import (
    OwnerFairExecutionGate,
    SwarmExecutionQueueFull,
)
from lib.swarm.protocol import SubAgentResult, SubAgentStatus, SubTaskSpec
from lib.swarm.rate_limiter import RateLimiter
from lib.swarm.scheduler import (
    StreamingScheduler,
    SwarmAgentCapacityExceeded,
)


pytestmark = pytest.mark.unit


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('condition did not become true')


def test_process_gate_round_robins_waiting_owners():
    gate = OwnerFairExecutionGate(capacity=1, waiter_capacity=4)
    gate.acquire('owner-a')
    order = []

    def run(owner, label):
        gate.acquire(owner)
        order.append(label)
        gate.release(owner)

    threads = [
        threading.Thread(target=run, args=('owner-a', 'a2')),
        threading.Thread(target=run, args=('owner-a', 'a3')),
    ]
    for thread in threads:
        thread.start()
    _wait_until(lambda: gate.snapshot()['waiting'] == 2)
    owner_b = threading.Thread(target=run, args=('owner-b', 'b1'))
    owner_b.start()
    threads.append(owner_b)
    _wait_until(lambda: gate.snapshot()['waiting'] == 3)

    gate.release('owner-a')
    for thread in threads:
        thread.join(timeout=2)

    assert order == ['a2', 'b1', 'a3']
    assert gate.snapshot()['active'] == 0
    assert gate.snapshot()['peakActive'] == 1


def test_process_gate_bounds_waiters_and_observes_abort():
    gate = OwnerFairExecutionGate(capacity=1, waiter_capacity=1)
    abort = threading.Event()
    errors = []
    gate.acquire(1)

    def wait_for_slot():
        try:
            gate.acquire(2, abort_check=abort.is_set)
        except Exception as exc:  # assertion captures the thread outcome
            errors.append(exc)

    waiter = threading.Thread(target=wait_for_slot)
    waiter.start()
    _wait_until(lambda: gate.snapshot()['waiting'] == 1)
    with pytest.raises(SwarmExecutionQueueFull):
        gate.acquire(3)

    abort.set()
    waiter.join(timeout=2)
    gate.release(1)

    assert len(errors) == 1 and isinstance(errors[0], AbortedError)
    snapshot = gate.snapshot()
    assert snapshot['waiting'] == 0
    assert snapshot['cancelled'] == 1
    assert snapshot['rejected'] == 1


def test_rate_limiters_share_the_process_execution_ceiling():
    gate = OwnerFairExecutionGate(capacity=1, waiter_capacity=2)
    active = 0
    peak = 0
    lock = threading.Lock()

    class Agent:
        def __init__(self, agent_id):
            self.agent_id = agent_id

        def run(self):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return SubAgentResult(status=SubAgentStatus.COMPLETED.value)

    limiters = [
        RateLimiter(max_concurrent=4, owner_key=1, execution_gate=gate),
        RateLimiter(max_concurrent=4, owner_key=2, execution_gate=gate),
    ]
    threads = [
        threading.Thread(target=limiter.run_agent, args=(Agent(str(index)),))
        for index, limiter in enumerate(limiters)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert peak == 1
    assert gate.snapshot()['accepted'] == 2


def test_rate_limiter_propagates_abort_while_waiting_at_process_gate():
    gate = OwnerFairExecutionGate(capacity=1, waiter_capacity=1)
    limiter = RateLimiter(
        max_concurrent=1,
        owner_key=2,
        execution_gate=gate,
    )
    abort = threading.Event()
    errors = []
    gate.acquire(1)

    class Agent:
        agent_id = 'must-not-run'

        def run(self):
            raise AssertionError('aborted waiter reached agent execution')

    def run():
        try:
            limiter.run_agent(Agent(), abort_check=abort.is_set)
        except Exception as exc:  # assertion captures the thread outcome
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    _wait_until(lambda: gate.snapshot()['waiting'] == 1)
    abort.set()
    thread.join(timeout=2)
    gate.release(1)

    assert len(errors) == 1 and isinstance(errors[0], AbortedError)
    assert limiter.active == 0


def test_scheduler_bounds_parallelism_results_and_retry_cost(monkeypatch):
    monkeypatch.setenv('TOFU_SWARM_MAX_PARALLEL', '2')
    monkeypatch.setenv('TOFU_SWARM_MAX_AGENTS_PER_SESSION', '2')
    monkeypatch.setenv('TOFU_SWARM_MAX_RETRIES', '1')
    runs = 0

    class FailedAgent:
        agent_id = 'failed'

        def run(self):
            nonlocal runs
            runs += 1
            return SubAgentResult(
                status=SubAgentStatus.FAILED.value,
                error_message='retry contract',
            )

    scheduler = StreamingScheduler(
        agent_factory=lambda _spec: FailedAgent(),
        max_parallel=999,
        max_total_agents=999,
        default_retries=999,
    )
    try:
        assert scheduler.max_parallel == 2
        assert scheduler.max_total_agents == 2
        with pytest.raises(SwarmAgentCapacityExceeded):
            scheduler.add_specs([
                SubTaskSpec(id='a', objective='a'),
                SubTaskSpec(id='b', objective='b'),
                SubTaskSpec(id='c', objective='c'),
            ])
        scheduler.add_specs([
            SubTaskSpec(id='retry', objective='retry', max_retries=999),
        ])
        results = scheduler.run_until_idle(timeout=2)
        assert len(results) == 1
        assert runs == 2
        assert scheduler._results_queue.maxsize == 2
    finally:
        scheduler.shutdown(wait=True)


def test_explicit_zero_agent_retries_is_not_replaced_by_scheduler_default(
        monkeypatch):
    monkeypatch.setenv('TOFU_SWARM_MAX_RETRIES', '4')
    runs = 0

    class FailedAgent:
        agent_id = 'no-retry'

        def run(self):
            nonlocal runs
            runs += 1
            return SubAgentResult(
                status=SubAgentStatus.FAILED.value,
                error_message='do not retry',
            )

    scheduler = StreamingScheduler(
        agent_factory=lambda _spec: FailedAgent(),
        max_parallel=1,
        max_total_agents=1,
        default_retries=4,
    )
    try:
        scheduler.add_specs([
            SubTaskSpec(id='no-retry', objective='one attempt', max_retries=0),
        ])
        scheduler.run_until_idle(timeout=2)
        assert runs == 1
    finally:
        scheduler.shutdown(wait=True)


def test_standard_spawn_rejects_oversized_wave_before_creating_session(
        monkeypatch):
    import lib.swarm.integration._tools as tools

    monkeypatch.setattr(tools, 'swarm_max_agents_per_wave', lambda: 2)
    payload = json.loads(tools.execute_swarm_tool(
        'spawn_agents',
        {'agents': [
            {'objective': 'a'}, {'objective': 'b'}, {'objective': 'c'},
        ]},
        task={'id': 'swarm-wave-cap', '_userId': 1},
    ))

    assert payload == {
        'status': 'error',
        'error': 'swarm_wave_capacity',
        'limit': 2,
        'requested': 3,
        'message': (
            'This deployment allows at most 2 agents in one wave. '
            'Combine or prioritize workstreams and retry once.'),
    }


def test_master_clamps_private_pool_and_uses_explicit_owner_gate(monkeypatch):
    from lib.swarm.master import MasterOrchestrator

    monkeypatch.setenv('TOFU_SWARM_MAX_PARALLEL', '2')
    monkeypatch.setenv('TOFU_SWARM_MAX_RETRIES', '1')
    master = MasterOrchestrator(
        task_id='bounded-master',
        conv_id='bounded-conversation',
        user_id=42,
        specs=[SubTaskSpec(id='one', objective='one')],
        max_parallel=999,
        max_retries=999,
    )

    assert master.max_parallel == 2
    assert master.max_retries == 1
    assert master.rate_limiter._owner_key == 42
    assert master.rate_limiter._execution_gate is not None


def test_session_registry_rejects_new_work_without_evicting_productive_session(
        monkeypatch):
    import lib.swarm.integration._state as state

    first = object()
    monkeypatch.setattr(state, 'MAX_SESSIONS', 1)
    monkeypatch.setattr(state, '_last_cleanup', 0.0)
    keys = ('swarm-cap-first', 'swarm-cap-second')
    try:
        state._set_session(keys[0], first)
        with pytest.raises(state.SwarmSessionCapacityExceeded):
            state._set_session(keys[1], object())
        assert state._active_sessions.get(keys[0]) is first
        assert keys[1] not in state._active_sessions
        snapshot = state.swarm_cleanup_snapshot()
        assert snapshot['sessionCapacity'] == 1
        assert snapshot['execution']['capacity'] >= 1
    finally:
        for key in keys:
            state._remove_session(key)


def test_session_registry_retires_terminal_memory_before_rejecting_new_work(
        monkeypatch):
    import lib.swarm.integration._state as state

    class TerminalSession:
        is_terminated = True

    terminal = TerminalSession()
    replacement = object()
    monkeypatch.setattr(state, 'MAX_SESSIONS', 1)
    monkeypatch.setattr(state, '_last_cleanup', 0.0)
    keys = ('swarm-terminal', 'swarm-replacement')
    try:
        state._set_session(keys[0], terminal)
        state.add_session_alias('terminal-task-id', keys[0])
        state._set_session(keys[1], replacement)
        assert keys[0] not in state._active_sessions
        assert state._active_sessions.get(keys[1]) is replacement
        assert state._resolve_key('terminal-task-id') == keys[0]
    finally:
        state._key_aliases.pop('terminal-task-id', None)
        for key in keys:
            state._remove_session(key)


def test_swarm_policy_overrides_have_hard_ceilings(monkeypatch):
    from lib.swarm import resource_policy

    for name in (
        'TOFU_SWARM_GLOBAL_WORKERS',
        'TOFU_SWARM_MAX_PARALLEL',
        'TOFU_SWARM_MAX_AGENTS_PER_WAVE',
        'TOFU_SWARM_MAX_AGENTS_PER_SESSION',
        'TOFU_SWARM_MAX_RETRIES',
        'TOFU_SWARM_SESSION_CAPACITY',
    ):
        monkeypatch.setenv(name, '999999')

    assert resource_policy.swarm_global_workers() == 32
    assert resource_policy.swarm_max_parallel() == 16
    assert resource_policy.swarm_max_agents_per_wave() == 32
    assert resource_policy.swarm_max_agents_per_session() == 128
    assert resource_policy.swarm_max_retries() == 4
    assert resource_policy.swarm_session_capacity() == 64
    monkeypatch.setenv('TOFU_SWARM_MAX_RETRIES', '0')
    assert resource_policy.swarm_max_retries() == 0


def test_spawn_tool_schema_advertises_the_runtime_wave_limit():
    from lib.swarm.resource_policy import swarm_max_agents_per_wave
    from lib.swarm.tools import SPAWN_AGENTS_TOOL

    agents = SPAWN_AGENTS_TOOL[
        'function']['parameters']['properties']['agents']
    assert agents['maxItems'] == swarm_max_agents_per_wave()


def test_read_only_projection_cannot_advertise_above_resource_wave_limit(
        monkeypatch):
    from lib.swarm.routing import project_multi_agent_wire_tools
    from lib.swarm.tools import SPAWN_AGENTS_TOOL

    monkeypatch.setenv('TOFU_SWARM_MAX_AGENTS_PER_WAVE', '2')
    projected = project_multi_agent_wire_tools(
        [],
        authority_catalog=[SPAWN_AGENTS_TOOL],
        backend='local_swarm',
        max_concurrent_agents=8,
    )
    agents = projected[0][
        'function']['parameters']['properties']['agents']
    assert agents['maxItems'] == 2
