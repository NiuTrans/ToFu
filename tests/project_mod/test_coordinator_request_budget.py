"""Original human, independent Workers and resumes conserve one finite allowance."""
from copy import deepcopy
import threading
import time

import pytest

from tests.project_mod.test_coordinator_workers import runtime as runtime, base_runtime, context, _worker_task
from lib.project_dispatch.request_budget import bind_source_account, allowance
from lib.agent_core.execution_budget import ModelBudgetExceeded

pytestmark = pytest.mark.unit


def bind(r, **settings):
    r.c.now[0] = time.time()
    r.attempts['coordinator-attempt']['createdAt'] = int(r.c.now[0] * 1000)
    r.task.update(status='running', usage={}, abort_event=threading.Event())
    return bind_source_account(r.task, repository=r.c.repo, settings={'revision': 0, 'settings': {
        'maxTokens': 1000, 'maxModelRequests': 10, 'maxToolCalls': 20, 'durationSeconds': 7200, **settings}})


def spend(task, tokens=10):
    lease = task['_executionBudget'].reserve_model(task, input_tokens=1, max_output_tokens=tokens)
    lease.mark_dispatched()
    lease.settle({'prompt_tokens': 1, 'completion_tokens': tokens - 1})


def usage(r, row):
    return allowance(r.c.repo, r.c.repo._load()[0], row['id'])


def test_initial_coordinator_and_worker_share_model_and_tool_allowance(runtime):
    r = runtime; row = bind(r)
    spend(r.task, 100)
    assert r.task['_executionBudget'].reserve_tool(r.task) is None
    r.args.update(max_tokens=700, max_model_requests=4, max_tool_calls=5)
    worker, assignment = _worker_task(r)
    state = usage(r, row)
    assert (state['observedTokens'], state['heldTokens'], state['remainingTokens']) == (100, 800, 200)
    assert (state['heldModelRequests'], state['heldToolCalls']) == (5, 6)
    lease = r.task['_executionBudget'].reserve_model(r.task, input_tokens=1, max_output_tokens=999)
    assert lease.max_output_tokens == 199
    lease.mark_dispatched(); lease.settle({'prompt_tokens': 1, 'completion_tokens': 99})
    spend(worker, 50)
    assert worker['_executionBudget'].reserve_tool(worker) is None
    r.attempts[assignment['execution']['attemptId']]['status'] = 'completed'
    state = usage(r, row)
    assert (state['heldTokens'], state['observedTokens'], state['heldModelRequests'], state['heldToolCalls']) == (250, 250, 3, 2)


def test_coordinator_can_plan_from_the_same_passive_usage_projection_as_human(runtime):
    from lib.conversations.coordinator_budget import CoordinatorBudgetSettings, read_state
    r = runtime; row = bind(r); spend(r.task)
    before = r.c.client.puts
    status = r.call('get_project_workers')
    settings = CoordinatorBudgetSettings(7, '/project', client=r.c.client, bindings=r.c.binding)
    human = read_state(7, '/project', repository=r.c.repo, settings=settings)
    assert status['request_allowance'] == human['request']
    assert status['request_allowance']['consumption']['remainingTokens'] == 990
    assert not status['workers'] and r.c.client.puts == before


def test_explicit_execution_ceiling_is_shared_only_after_source_registration(runtime):
    r = runtime
    r.task['config']['executionLimits'] = {'max_tokens': 120, 'max_tool_calls': 3, 'deadline': None}
    row = bind(r)
    spend(r.task, 20)
    assert r.task['_executionBudget'].reserve_tool(r.task) is None
    accepted = r.call('delegate_project_work', **r.args)
    assert accepted['dispatch'] == 'accepted', accepted
    worker = r.c.repo.read(accepted['assignment_id'])
    assert worker['request']['budget']['maxTokens'] == 100
    assert worker['request']['profile']['maxToolCalls'] == 2
    assert row['policy']['maxTokens'] == 120 and usage(r, row)['remainingTokens'] == 0
    with pytest.raises(ModelBudgetExceeded):
        r.task['_executionBudget'].reserve_model(r.task, input_tokens=1, max_output_tokens=1)
    assert r.task['_executionBudget'].reserve_tool(r.task) == 'execution_tool_budget_exhausted'
    assert r.call('delegate_project_work', **r.args)['assignment_id'] == accepted['assignment_id']
    changed = r.call('delegate_project_work', **{**r.args, 'max_tokens': 1})
    assert 'error' in changed and len(r.created) == 1


def test_grant_cas_fences_race_after_root_output_reservation(runtime):
    r = runtime; row = bind(r)
    lease = r.task['_executionBudget'].reserve_model(r.task, input_tokens=1, max_output_tokens=999)
    accepted = r.call('delegate_project_work', **{**r.args, 'max_tokens': 900, 'max_model_requests': 3, 'max_tool_calls': 2})
    assert accepted['dispatch'] == 'accepted'
    with pytest.raises(Exception, match='reserved or exhausted'):
        lease.mark_dispatched()
    assert usage(r, row)['heldTokens'] == 900
    assert not r.c.repo._load()[0]['requestBudgets'][0]['modelAttempts']


def test_model_request_reservations_are_shared_even_with_tokens_remaining(runtime):
    r = runtime; row = bind(r, maxModelRequests=3)
    spend(r.task)
    accepted = r.call('delegate_project_work', **{**r.args, 'max_tokens': 500, 'max_model_requests': 2})
    assert accepted['dispatch'] == 'accepted'
    lease = r.task['_executionBudget'].reserve_model(r.task, input_tokens=1, max_output_tokens=1)
    with pytest.raises(Exception, match='maxModelRequests'):
        lease.mark_dispatched()
    lease.settle(unsent=True)
    state = usage(r, row)
    assert state['modelRequests'] == 1 and state['heldModelRequests'] == 3 and state['remainingTokens'] == 490


def test_overshoot_is_charged_and_blocks_other_accounts(runtime):
    r = runtime; row = bind(r)
    r.args.update(max_tokens=800, max_model_requests=4, max_tool_calls=5)
    worker, _ = _worker_task(r)
    lease = worker['_executionBudget'].reserve_model(worker, input_tokens=1, max_output_tokens=1)
    lease.mark_dispatched(); lease.settle({'prompt_tokens': 1100, 'completion_tokens': 1})
    assert usage(r, row)['heldTokens'] == 1101
    with pytest.raises(ModelBudgetExceeded):
        r.task['_executionBudget'].reserve_model(r.task, input_tokens=1, max_output_tokens=1)
    assert r.task['_executionBudget'].reserve_tool(r.task) == 'execution_tool_budget_exhausted'


def test_same_human_input_new_attempt_keeps_spend_and_first_settings(runtime):
    r = runtime; row = bind(r)
    spend(r.task, 500)
    r.attempts['coordinator-attempt']['status'] = 'completed'
    r.turns['next-turn'] = {**r.turns['coordinator-turn'], 'turnId': 'next-turn'}
    r.attempts['next-attempt'] = {**r.attempts['coordinator-attempt'], 'attemptId': 'next-attempt',
        'turnId': 'next-turn', 'taskId': 'next-task', 'status': 'running'}
    resumed = {key: value for key, value in r.task.items() if key not in ('_coordinatorRequestBudget', '_executionBudget')}
    resumed.update(id='next-task', _turnId='next-turn', _attemptId='next-attempt', usage={})
    current = bind_source_account(resumed, repository=r.c.repo, settings={'revision': 1, 'settings': {
        'maxTokens': 9000, 'maxModelRequests': 900, 'maxToolCalls': 2000, 'durationSeconds': 9000}})
    assert current['id'] == row['id'] and current['policy'] == row['policy'] and len(current['accounts']) == 2
    lease = resumed['_executionBudget'].reserve_model(resumed, input_tokens=1, max_output_tokens=9000)
    assert lease.max_output_tokens == 499
    lease.mark_dispatched(); lease.settle({'prompt_tokens': 1, 'completion_tokens': 499})
    assert usage(r, row)['observedTokens'] == 1000


def test_plan_counts_own_usage_but_never_delegates(runtime):
    r = runtime; r.task['config']['planMode'] = True
    row = bind(r); spend(r.task)
    assert usage(r, row)['observedTokens'] == 10
    assert 'error' in r.call('delegate_project_work', **r.args)
    assert not r.created


def test_user_selected_allowance_can_scale_above_old_internal_default_ceiling(runtime):
    r = runtime; row = bind(r, maxModelRequests=10000)
    accepted = r.call('delegate_project_work', **r.args)
    assert accepted['dispatch'] == 'accepted', accepted
    request = r.c.repo.read(accepted['assignment_id'])['request']
    assert request['delegation']['policy']['maxTotalModelRequests'] == row['policy']['maxModelRequests'] == 10000


def test_unknown_source_write_ack_never_refunds_or_rebinds(runtime, monkeypatch):
    r = runtime; row = bind(r)
    change = r.c.repo._change
    def lost_ack(callback, **options):
        change(callback, **options)
        raise OSError('ack lost')
    monkeypatch.setattr(r.c.repo, '_change', lost_ack)
    with pytest.raises(OSError):
        r.task['_executionBudget'].reserve_tool(r.task)
    monkeypatch.setattr(r.c.repo, '_change', change)
    assert usage(r, row)['toolCalls'] == 1
    assert r.task['_executionBudget'].reserve_tool(r.task) == 'execution_token_usage_unknown'
    replacement = {key: value for key, value in r.task.items() if key not in ('_coordinatorRequestBudget', '_executionBudget')}
    with pytest.raises(PermissionError, match='replenished'):
        bind_source_account(replacement, repository=r.c.repo, settings={'revision': 0, 'settings': {
            'maxTokens': 1000, 'maxModelRequests': 10, 'maxToolCalls': 20, 'durationSeconds': 7200}})


@pytest.mark.parametrize('withdraw', ['stop', 'binding', 'deadline', 'source'])
def test_source_spending_revalidates_live_authority(runtime, withdraw):
    r = runtime; row = bind(r)
    if withdraw == 'stop': r.task['aborted'] = True
    if withdraw == 'binding': r.c.binding.value['revision'] += 1
    if withdraw == 'deadline': r.c.now[0] = row['policy']['deadline'] + 1
    if withdraw == 'source': r.c.turn['projectionRevision'] += 1; r.turns['human-turn'] = r.c.turn
    with pytest.raises(Exception):
        spend(r.task)
    assert usage(r, row)['observedTokens'] == 0
    if withdraw == 'stop':
        assert r.task['_executionBudget'].reserve_tool(r.task) == 'execution_owner_cancelled'
    else:
        with pytest.raises(Exception):
            r.task['_executionBudget'].reserve_tool(r.task)
    assert usage(r, row)['toolCalls'] == 0


def test_fifty_physical_calls_and_passive_reads_have_bounded_evidence(runtime):
    import json
    r = runtime; row = bind(r, maxModelRequests=128)
    start = time.perf_counter()
    for _ in range(50): spend(r.task)
    elapsed = time.perf_counter() - start
    value, _ = r.c.repo._load()
    byte_count = len(json.dumps(value, ensure_ascii=False).encode())
    before = r.c.client.puts
    threads = threading.active_count()
    for _ in range(50): assert usage(r, row)['observedTokens'] == 500
    assert r.c.client.puts == before and threading.active_count() == threads
    assert len(value['requestBudgets'][0]['modelAttempts']) == 50 and byte_count < 32000
    print(f'budget evidence: calls=50 seconds={elapsed:.4f} bytes={byte_count} passive_writes=0 added_threads=0')
