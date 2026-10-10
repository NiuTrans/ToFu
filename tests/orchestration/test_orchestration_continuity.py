"""Boot continuity pass: automatic resume of restart-interrupted durable runs.

Covers docs/RUN_CONTINUITY.md phase 1: policy-filtered election from the
startup retire fence, owner-bound successor spawn through the ordinary
recovery protocol, typed continuity audit events, idempotency, per-boot
capacity, the crash-loop guard, the operator kill switch, and the budget
refund for replayed receipts (D6).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


GOAL_CREATED_BY = 'chat_goal_mode'
RETIRE_ERROR = {
    'kind': 'worker_lost',
    'message': 'Run interrupted by a server restart before completion.',
    'source': 'orchestration.startup_recovery',
}


class _FakeRuntime:
    """OrchestrationTaskRuntimePort stand-in: records spawns, never runs."""

    def __init__(self):
        self.created = []
        self.spawns = []

    def create(self, *, user_id, task_id='', meta=None):
        task = {
            'id': task_id or f'task-{len(self.created)}',
            'abort_event': threading.Event(),
            'user_id': user_id,
            'meta': meta or {},
        }
        self.created.append(task)
        return task

    def spawn(self, task_id, worker):
        self.spawns.append((task_id, worker))

    def finish(self, *args, **kwargs):
        raise AssertionError('a recorded worker never ran')


def _definition():
    return {'nodes': [], 'edges': []}


def _journal_events(definition, input_text, *, parent_run_id='', extra=()):
    from lib.orchestration.execution_journal import JOURNAL_VERSION, fingerprint
    return [
        {'type': 'execution_journal_start', 'version': JOURNAL_VERSION,
         'definition_hash': fingerprint(definition),
         'input_hash': fingerprint(input_text),
         'parent_run_id': parent_run_id, 'continuation': False},
        {'type': 'execution_journal_ready',
         'seed_hash': fingerprint({}), 'seed_count': 0},
        *extra,
    ]


def _create_run(client, run_id, *, owner, tenant, created_by, input_text='goal'):
    # Goal runs carry the canonical chat-goal orchestration id; the durable
    # one-active-goal fence keys on (tenant, owner, orch_id).
    orch_id = (f'chat-goal:{run_id}' if created_by == GOAL_CREATED_BY else '')
    assert client.command(
        'orchestration.run.create',
        {'run_id': run_id, 'user_id': owner, 'tenant_id': tenant,
         'definition': _definition(), 'input': input_text,
         'orch_id': orch_id, 'created_by': created_by},
        f'create:{run_id}',
    ) == {'created': True}


def _append_events(client, run_id, *, owner, tenant, events):
    for sequence, event in enumerate(events):
        result = client.command(
            'orchestration.event.append',
            {'run_id': run_id, 'user_id': owner, 'tenant_id': tenant,
             'sequence': sequence, 'event': event},
            None,
        )
        assert result['accepted'] is True


def _run_events(client, run_id, *, owner, tenant):
    collected = []
    cursor = 0
    while True:
        page = client.query(
            'orchestration.event.page',
            {'run_id': run_id, 'user_id': owner, 'tenant_id': tenant,
             'cursor': cursor})
        collected.extend(page['events'])
        if page['caught_up']:
            return collected
        cursor = page['next_cursor']


def _service_factories(client):
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore

    def run_service_for_owner(owner, tenant):
        return OrchestrationRunService(
            SidecarOrchestrationRunStore(
                owner, tenant_id=tenant,
                client=lambda write=False: client))

    def definition_service_for_owner(owner, tenant):
        return SimpleNamespace(get_definition=lambda ref: None)

    return run_service_for_owner, definition_service_for_owner


@pytest.fixture
def sidecar(tmp_path, monkeypatch):
    from lib.storage import StorageSupervisor
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    monkeypatch.setenv('TOFU_STORAGE_RPC_CAPACITY', '4')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


def _retire(sidecar):
    from lib.orchestration.startup_recovery import (
        retire_interrupted_orchestration_runs_detailed,
    )
    return retire_interrupted_orchestration_runs_detailed(
        error=RETIRE_ERROR,
        client=lambda write=False: sidecar.client,
    )


def _pass_kwargs(sidecar, runtime):
    run_service_for_owner, definition_service_for_owner = _service_factories(
        sidecar.client)
    return {
        'runtime': runtime,
        'run_service_for_owner': run_service_for_owner,
        'definition_service_for_owner': definition_service_for_owner,
        'auto_created_by': frozenset({GOAL_CREATED_BY}),
    }


def test_retire_reports_exactly_this_boot_generations_retired_runs(sidecar):
    client = sidecar.client
    _create_run(client, 'run-a', owner=7, tenant='tenant-a',
                created_by=GOAL_CREATED_BY)
    _create_run(client, 'run-b', owner=8, tenant='tenant-b',
                created_by='studio')
    # A durable human wait survives retirement and is never a candidate.
    _create_run(client, 'run-parked', owner=7, tenant='tenant-a',
                created_by=GOAL_CREATED_BY)
    assert client.command(
        'orchestration.run.update_status',
        {'run_id': 'run-parked', 'user_id': 7, 'tenant_id': 'tenant-a',
         'status': 'paused'},
        'pause:run-parked',
    )['changed'] is True
    _append_events(client, 'run-parked', owner=7, tenant='tenant-a', events=[
        {'type': 'human_request', 'continuation': {'request_id': 'dg.x'}}])

    retirement = _retire(sidecar)

    assert retirement.retired == 2
    candidates = {row['run_id']: row for row in retirement.candidates}
    assert set(candidates) == {'run-a', 'run-b'}
    assert candidates['run-a'] == {
        'run_id': 'run-a', 'user_id': 7, 'tenant_id': 'tenant-a',
        'created_by': GOAL_CREATED_BY}
    assert client.query(
        'orchestration.run.get',
        {'run_id': 'run-parked', 'user_id': 7, 'tenant_id': 'tenant-a'},
    )['status'] == 'paused'


def test_auto_resume_spawns_owner_bound_successor_and_audits(sidecar):
    from lib.orchestration.continuity import run_boot_continuity_pass
    from lib.orchestration.run_recovery import recovery_run_id
    client = sidecar.client
    _create_run(client, 'run-goal', owner=7, tenant='tenant-a',
                created_by=GOAL_CREATED_BY)
    _append_events(client, 'run-goal', owner=7, tenant='tenant-a',
                   events=_journal_events(_definition(), 'goal'))
    _create_run(client, 'run-manual', owner=7, tenant='tenant-a',
                created_by='studio')
    _append_events(client, 'run-manual', owner=7, tenant='tenant-a',
                   events=_journal_events(_definition(), 'goal'))
    retirement = _retire(sidecar)
    runtime = _FakeRuntime()

    summary = run_boot_continuity_pass(
        candidates=retirement.candidates, **_pass_kwargs(sidecar, runtime))

    assert summary['resumed'] == 1
    assert summary['skipped'] == 1  # the studio-policy run is never elected
    successor = recovery_run_id('run-goal')
    assert [task_id for task_id, _ in runtime.spawns] == [successor]
    assert runtime.created[0]['user_id'] == 7
    row = client.query(
        'orchestration.run.get',
        {'run_id': successor, 'user_id': 7, 'tenant_id': 'tenant-a'})
    assert row is not None and row['created_by'] == GOAL_CREATED_BY
    source_events = _run_events(client, 'run-goal', owner=7, tenant='tenant-a')
    event_types = [event['type'] for event in source_events]
    assert 'execution_recovery_claim' in event_types
    resumed = [event for event in source_events
               if event['type'] == 'continuity_resumed']
    assert resumed and resumed[0]['successor_run_id'] == successor
    manual_events = _run_events(
        client, 'run-manual', owner=7, tenant='tenant-a')
    assert not any(event['type'].startswith('continuity_')
                   for event in manual_events)


def test_ambiguous_inflight_invocation_stays_settled_and_records_refusal(
        sidecar):
    from lib.orchestration.continuity import run_boot_continuity_pass
    from lib.orchestration.run_recovery import recovery_run_id
    client = sidecar.client
    _create_run(client, 'run-ambiguous', owner=7, tenant='tenant-a',
                created_by=GOAL_CREATED_BY)
    _append_events(client, 'run-ambiguous', owner=7, tenant='tenant-a',
                   events=_journal_events(
                       _definition(), 'goal',
                       extra=[{'type': 'execution_effect_started',
                               'key': '["root","agent","n1",0,1]',
                               'kind': 'agent', 'input_hash': 'x'}]))
    retirement = _retire(sidecar)
    runtime = _FakeRuntime()

    summary = run_boot_continuity_pass(
        candidates=retirement.candidates, **_pass_kwargs(sidecar, runtime))

    assert summary['refused'] == 1
    assert runtime.spawns == []
    assert client.query(
        'orchestration.run.get',
        {'run_id': recovery_run_id('run-ambiguous'),
         'user_id': 7, 'tenant_id': 'tenant-a'}) is None
    events = _run_events(
        client, 'run-ambiguous', owner=7, tenant='tenant-a')
    refused = [event for event in events
               if event['type'] == 'continuity_refused']
    assert refused and refused[0]['reason']


def test_repeated_pass_never_spawns_twice(sidecar):
    from lib.orchestration.continuity import run_boot_continuity_pass
    client = sidecar.client
    _create_run(client, 'run-once', owner=7, tenant='tenant-a',
                created_by=GOAL_CREATED_BY)
    _append_events(client, 'run-once', owner=7, tenant='tenant-a',
                   events=_journal_events(_definition(), 'goal'))
    retirement = _retire(sidecar)
    runtime = _FakeRuntime()

    first = run_boot_continuity_pass(
        candidates=retirement.candidates, **_pass_kwargs(sidecar, runtime))
    second = run_boot_continuity_pass(
        candidates=retirement.candidates, **_pass_kwargs(sidecar, runtime))

    assert first['resumed'] == 1
    assert second['skipped'] == 1 and second['resumed'] == 0
    assert len(runtime.spawns) == 1
    events = _run_events(client, 'run-once', owner=7, tenant='tenant-a')
    assert sum(event['type'] == 'continuity_resumed'
               for event in events) == 1


def test_crash_loop_guard_records_exhaustion_and_skips_spawn(sidecar):
    from lib.orchestration.continuity import run_boot_continuity_pass
    client = sidecar.client
    # A lineage of rapid successive recoveries: c <- b <- a, all retired
    # inside the guard window by this boot's fence.
    for run_id, parent in (('run-a', ''), ('run-b', 'run-a'),
                           ('run-c', 'run-b')):
        _create_run(client, run_id, owner=7, tenant='tenant-a',
                    created_by=GOAL_CREATED_BY)
        _append_events(client, run_id, owner=7, tenant='tenant-a',
                       events=_journal_events(
                           _definition(), 'goal', parent_run_id=parent))
    retirement = _retire(sidecar)
    runtime = _FakeRuntime()
    candidate = [row for row in retirement.candidates
                 if row['run_id'] == 'run-c']

    summary = run_boot_continuity_pass(
        candidates=candidate, **_pass_kwargs(sidecar, runtime))

    assert summary['exhausted'] == 1
    assert runtime.spawns == []
    events = _run_events(client, 'run-c', owner=7, tenant='tenant-a')
    assert any(event['type'] == 'continuity_exhausted' for event in events)


def test_per_boot_capacity_defers_overflow_candidates(sidecar):
    from lib.orchestration.continuity import run_boot_continuity_pass
    client = sidecar.client
    for run_id in ('run-cap-1', 'run-cap-2'):
        _create_run(client, run_id, owner=7, tenant='tenant-a',
                    created_by=GOAL_CREATED_BY)
        _append_events(client, run_id, owner=7, tenant='tenant-a',
                       events=_journal_events(_definition(), 'goal'))
    retirement = _retire(sidecar)
    runtime = _FakeRuntime()

    summary = run_boot_continuity_pass(
        candidates=retirement.candidates,
        environ={'TOFU_CONTINUITY_AUTO_RESUME_MAX': '1'},
        **_pass_kwargs(sidecar, runtime))

    assert summary['resumed'] == 1
    assert summary['deferred'] == 1
    assert len(runtime.spawns) == 1
    deferred_events = _run_events(
        client, 'run-cap-2', owner=7, tenant='tenant-a')
    assert any(event['type'] == 'continuity_deferred'
               for event in deferred_events)


def test_launch_capacity_recovers_more_than_the_former_fixed_boot_limit(sidecar):
    from lib.orchestration.continuity import run_boot_continuity_pass
    client = sidecar.client
    for index in range(18):
        run_id = f'run-scaled-{index:02d}'
        _create_run(client, run_id, owner=7, tenant='tenant-a',
                    created_by=GOAL_CREATED_BY)
        _append_events(client, run_id, owner=7, tenant='tenant-a',
                       events=_journal_events(_definition(), 'goal'))
    retirement = _retire(sidecar)
    runtime = _FakeRuntime()

    summary = run_boot_continuity_pass(
        candidates=retirement.candidates, environ={
            'TOFU_AGENT_WORKERS': '32', 'TOFU_MAX_INFLIGHT_TASKS': '32',
            'TOFU_PROCESS_RSS_RECYCLE_MB': '65536',
            'TOFU_TASK_RSS_RESERVE_MB': '64'},
        **_pass_kwargs(sidecar, runtime))

    assert summary['resumed'] == 18
    assert summary['deferred'] == 0
    assert len(runtime.spawns) == 18
    assert len({task_id for task_id, _ in runtime.spawns}) == 18
    repeated = run_boot_continuity_pass(
        candidates=retirement.candidates, environ={
            'TOFU_AGENT_WORKERS': '32', 'TOFU_MAX_INFLIGHT_TASKS': '32',
            'TOFU_PROCESS_RSS_RECYCLE_MB': '65536',
            'TOFU_TASK_RSS_RESERVE_MB': '64'},
        **_pass_kwargs(sidecar, runtime))
    assert repeated['skipped'] == 18
    assert len(runtime.spawns) == 18


@pytest.mark.parametrize('override', ['', 'malformed', '0', '-1'])
def test_boot_capacity_uses_launch_policy_when_override_is_unusable(override):
    from lib.orchestration.continuity import max_resumes_per_boot
    assert max_resumes_per_boot({
        'TOFU_AGENT_WORKERS': '128',
        'TOFU_MAX_INFLIGHT_TASKS': '128',
        'TOFU_PROCESS_RSS_RECYCLE_MB': '65536',
        'TOFU_TASK_RSS_RESERVE_MB': '64',
        'TOFU_CONTINUITY_AUTO_RESUME_MAX': override,
    }) == 128


def test_automatic_boot_capacity_respects_memory_and_admission_limits():
    from lib.orchestration.continuity import max_resumes_per_boot
    environment = {
        'TOFU_AGENT_WORKERS': '1200', 'TOFU_MAX_INFLIGHT_TASKS': '1200',
        'TOFU_PROCESS_RSS_RECYCLE_MB': '4096',
        'TOFU_TASK_RSS_RESERVE_MB': '1024',
    }
    assert max_resumes_per_boot(environment) == 3
    environment['TOFU_MAX_INFLIGHT_TASKS'] = '2'
    assert max_resumes_per_boot(environment) == 2
    environment['TOFU_MAX_INFLIGHT_TASKS'] = '0'
    assert max_resumes_per_boot(environment) == 3


def test_automatic_boot_capacity_scales_with_the_shared_resource_probe(monkeypatch):
    import runtime_guards
    from lib.orchestration.continuity import max_resumes_per_boot
    capacities = []
    for cpus, memory_mb in ((2, 8192), (128, 262144)):
        snapshot = runtime_guards.SystemResourceSnapshot(
            host_cpu_count=cpus, affinity_cpu_count=cpus,
            cgroup_cpu_count=None, effective_cpu_count=cpus,
            host_memory_total_mb=memory_mb,
            host_memory_available_mb=memory_mb // 2,
            cgroup_memory_limit_mb=None, cgroup_memory_current_mb=None,
            effective_memory_capacity_mb=memory_mb,
            effective_memory_available_mb=memory_mb // 2,
            disk_total_mb=1048576, disk_free_mb=524288,
        )
        monkeypatch.setattr(runtime_guards, 'probe_system_resources',
                            lambda env, snapshot=snapshot: snapshot)
        capacities.append(max_resumes_per_boot({}))
    assert 1 <= capacities[0] <= 2
    assert capacities[1] > 16


def test_positive_boot_override_is_independent_and_structurally_bounded():
    from lib.orchestration.continuity import max_resumes_per_boot
    from runtime_guards import MAX_TASK_CONCURRENCY
    assert max_resumes_per_boot({
        'TOFU_AGENT_WORKERS': '2', 'TOFU_CONTINUITY_AUTO_RESUME_MAX': '128',
    }) == 128
    assert max_resumes_per_boot({
        'TOFU_CONTINUITY_AUTO_RESUME_MAX': str(MAX_TASK_CONCURRENCY * 10),
    }) == MAX_TASK_CONCURRENCY


def test_kill_switch_disables_the_pass_without_touching_storage():
    from lib.orchestration.continuity import run_boot_continuity_pass

    def forbidden(*args, **kwargs):
        raise AssertionError('disabled pass must not reach any service')

    summary = run_boot_continuity_pass(
        candidates=[{'run_id': 'run-x', 'user_id': 7, 'tenant_id': '',
                     'created_by': GOAL_CREATED_BY}],
        auto_created_by=frozenset({GOAL_CREATED_BY}),
        runtime=_FakeRuntime(),
        run_service_for_owner=forbidden,
        definition_service_for_owner=forbidden,
        environ={'TOFU_CONTINUITY_AUTO_RESUME': 'off'})

    assert summary == {'resumed': 0, 'refused': 0, 'deferred': 0,
                       'exhausted': 0, 'skipped': 1}


def test_replayed_agent_receipt_releases_the_claimed_budget():
    from lib.orchestration.budget import OrchestrationAgentBudget
    from lib.orchestration.execution_journal import (
        ExecutionJournal, read_journal,
    )
    from lib.orchestration.runner_result import OrchestrationAgentResult
    definition = _definition()
    budget = OrchestrationAgentBudget(2)
    dispatched = []
    emitted = []

    first = ExecutionJournal(emitted.append)
    first.begin(definition, 'ctx')
    first_runner = first.runner(
        lambda node, context, iteration: dispatched.append(1)
        or OrchestrationAgentResult(output='ok'),
        on_replay=budget.release)
    assert budget.claim() is True
    assert first_runner({'id': 'n1'}, 'ctx', 0).output == 'ok'
    assert dispatched == [1]

    records, parent = read_journal(
        emitted, definition=definition, initial_context='ctx')
    assert parent == ''

    second = ExecutionJournal(lambda event: None, records=records)
    second.begin(definition, 'ctx')

    def no_dispatch(*args):
        raise AssertionError('a replayed receipt must never dispatch again')

    second_runner = second.runner(no_dispatch, on_replay=budget.release)
    assert budget.claim() is True  # the claim site is reached before replay
    assert second_runner({'id': 'n1'}, 'ctx', 0).output == 'ok'
    second.verify_consumed()
    # Only the genuine dispatch retains its reservation; the replay refunded.
    assert budget.used() == 1


def test_budget_release_never_underflows():
    from lib.orchestration.budget import OrchestrationAgentBudget
    budget = OrchestrationAgentBudget(1)
    budget.release()
    assert budget.used() == 0
    assert budget.claim() is True
    budget.release()
    budget.release()
    assert budget.used() == 0
