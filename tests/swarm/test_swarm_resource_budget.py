"""Executable resource and cost contracts for background swarm agents."""

from __future__ import annotations

import json
import threading
import time

import pytest

from lib.llm.llm_errors import AbortedError
from lib.swarm.execution_gate import (
    SwarmExecutionAccounting,
)
from lib.swarm.protocol import SubAgentResult, SubAgentStatus, SubTaskSpec
from lib.swarm.rate_limiter import RateLimiter
from lib.swarm.scheduler import (
    StreamingScheduler,
)


pytestmark = pytest.mark.unit


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('condition did not become true')


def test_execution_accounting_starts_every_owner_without_capacity_wait():
    gate = SwarmExecutionAccounting(capacity=1, waiter_capacity=1)
    gate.acquire('first')
    gate.resize(1)
    for index in range(24):
        gate.acquire(('owner', index))
    snapshot = gate.snapshot()
    assert snapshot['active'] == 25
    assert snapshot['waiting'] == snapshot['rejected'] == 0
    for index in range(24):
        gate.release(('owner', index))
    with pytest.raises(RuntimeError, match='non-owner'):
        gate.release('foreign')
    assert gate.snapshot()['active'] == 1
    gate.release('first')
    assert gate.snapshot()['activeOwners'] == 0


def test_abort_before_entry_allocates_no_execution_accounting():
    gate = SwarmExecutionAccounting(capacity=1, waiter_capacity=1)
    gate.acquire('live')
    with pytest.raises(AbortedError):
        gate.acquire('stopped', abort_check=lambda: True)
    assert gate.snapshot()['active'] == 1
    assert gate.snapshot()['cancelled'] == 1
    gate.release('live')


def test_scheduler_starts_burst_above_old_pool_wave_and_result_limits(monkeypatch):
    monkeypatch.setenv('TOFU_SWARM_MAX_PARALLEL', '1')
    monkeypatch.setenv('TOFU_SWARM_MAX_AGENTS_PER_SESSION', '1')
    gate = SwarmExecutionAccounting(capacity=1, waiter_capacity=1)
    limiter = RateLimiter(max_concurrent=1, owner_key=42, execution_gate=gate)
    release = threading.Event()
    entered = [threading.Event() for _ in range(24)]
    class Agent:
        def __init__(self, spec):
            self.agent_id = spec.id
        def run(self):
            entered[int(self.agent_id)].set()
            assert release.wait(5)
            return SubAgentResult(status='completed', final_answer=self.agent_id)
    scheduler = StreamingScheduler(
        agent_factory=Agent, rate_limiter=limiter,
        max_parallel=1, max_total_agents=1)
    try:
        scheduler.add_specs([SubTaskSpec(id=str(index), objective=f'work {index}')
                             for index in range(24)])
        assert all(event.wait(2) for event in entered)
        assert gate.snapshot()['active'] == limiter.active == 24
        release.set()
        results = scheduler.run_until_idle(timeout=3)
        assert len(results) == 24
        assert {spec.id for spec, result in results} == {str(i) for i in range(24)}
        assert all(result.status == 'completed' for spec, result in results)
        assert gate.snapshot()['active'] == limiter.active == 0
    finally:
        release.set()
        scheduler.shutdown(wait=True)


def test_scheduler_retains_finite_retry_cost_without_result_admission(monkeypatch):
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
        assert scheduler.max_total_agents == 0
        scheduler.add_specs([
            SubTaskSpec(id='retry', objective='retry', max_retries=999),
        ])
        results = scheduler.run_until_idle(timeout=2)
        assert len(results) == 1
        assert runs == 2
        assert scheduler._results_queue.maxsize == 0
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


def test_session_registry_accepts_new_work_without_evicting_productive_session(
        monkeypatch):
    import lib.swarm.integration._state as state

    first = object()
    monkeypatch.setattr(state, 'MAX_SESSIONS', 1)
    monkeypatch.setattr(state, '_last_cleanup', 0.0)
    keys = ('swarm-cap-first', 'swarm-cap-second')
    try:
        state._set_session(keys[0], first)
        state._set_session(keys[1], object())
        assert state._active_sessions.get(keys[0]) is first
        assert keys[1] in state._active_sessions
        snapshot = state.swarm_cleanup_snapshot()
        assert snapshot['sessionCapacity'] == 1
        assert snapshot['execution']['capacity'] >= 0
    finally:
        for key in keys:
            state._remove_session(key)


def test_session_registry_retires_terminal_cache_while_accepting_new_work(
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
        import lib.swarm.integration as facade
        monkeypatch.setattr(facade, '_last_cleanup', 0)
        state._cleanup_stale_sessions()
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

    assert resource_policy.swarm_max_parallel() == 64
    assert resource_policy.swarm_max_retries() == 4
    assert resource_policy.swarm_terminal_cache_target() == 128
    monkeypatch.setenv('TOFU_SWARM_MAX_RETRIES', '0')
    assert resource_policy.swarm_max_retries() == 0


def test_spawn_tool_schema_advertises_the_runtime_wave_limit():
    from lib.swarm.tools import SPAWN_AGENTS_TOOL

    agents = SPAWN_AGENTS_TOOL[
        'function']['parameters']['properties']['agents']
    assert 'maxItems' not in agents


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
    assert 'maxItems' not in agents


def test_capacity_cleanup_cannot_abort_productive_sessions(monkeypatch):
    from types import SimpleNamespace
    import lib.swarm.integration as facade
    from lib.swarm.integration import _state as state
    first = SimpleNamespace(is_terminated=False, abort=lambda: pytest.fail('live task revoked'))
    second = SimpleNamespace(is_terminated=False, abort=lambda: pytest.fail('live task revoked'))
    monkeypatch.setattr(state, '_active_sessions', {'first': first, 'second': second})
    monkeypatch.setattr(state, '_session_timestamps', {'first': time.time(), 'second': time.time()})
    monkeypatch.setattr(state, '_key_aliases', {})
    monkeypatch.setattr(state, 'MAX_SESSIONS', 1)
    monkeypatch.setattr(facade, '_last_cleanup', 0)
    monkeypatch.setattr('lib.swarm.persistence.delete_session', lambda key: pytest.fail('durable state deleted'))
    state._cleanup_stale_sessions()
    assert state._active_sessions == {'first': first, 'second': second}


def test_read_only_waves_and_followups_accept_above_old_count_without_widening_tools(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import lib.swarm.integration._tools as tools
    import lib.swarm.persistence as persistence
    from lib.swarm.master import MasterOrchestrator
    sessions = {}
    monkeypatch.setattr(tools, '_get_session', lambda key: sessions.get(key))
    monkeypatch.setattr(tools, '_set_session', lambda key, session, **kwargs:
                        sessions.__setitem__(key, session))
    monkeypatch.setattr(tools, '_resolve_output_dir', lambda task_id: str(tmp_path))
    monkeypatch.setattr(persistence, 'save_session', lambda *args, **kwargs: None)
    monkeypatch.setattr(MasterOrchestrator, 'run_in_background', lambda self:
                        setattr(self, '_scheduler', SimpleNamespace(add_specs=lambda specs: specs)))
    task = {'id': 'read-only-count', 'convId': 'read-only-conversation',
            '_userId': 7, 'status': 'running', 'config': {'model': 'fixture-model'},
            '_toolOrchestration': {'multiAgent': 'read_only', 'maxConcurrentAgents': 1}}
    write_tool = {'type': 'function', 'function': {'name': 'run_command',
                 'parameters': {'type': 'object', 'properties': {}}}}
    first = json.loads(tools.execute_swarm_tool('spawn_agents',
        {'agents': [{'objective': f'inspect {index}'} for index in range(3)]},
        task=task, all_tools=[write_tool]))
    assert first['status'] == 'async_launched'
    assert len(first['agents']) == 3
    session = sessions['read-only-conversation']
    assert all(tool['function']['name'] != 'run_command' for tool in session.all_tools)
    task.pop('_toolOrchestration')
    followup = json.loads(tools.execute_swarm_tool('spawn_agents',
        {'agents': [{'objective': f'review {index}'} for index in range(3)]},
        task=task, all_tools=[write_tool]))
    assert followup['status'] == 'async_launched'
    assert followup['is_followup'] is True
    assert followup['orchestration']['mode'] == 'read_only'
    assert len(followup['agents']) == 3
