"""Whole-account cuts preserve concurrent debt and fence failed durability."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.agent_core.account_frontier import validate_frontier
from lib.agent_core.execution_budget import _ensure_execution_budget, close_execution_budget, ModelBudgetExceeded
from lib.goal_runs.account_journal import read_account_frontier, recovery_account_snapshot
from lib.orchestration.execution_journal import RecoveryRefused
from lib.orchestration.recovery_account import prepare_recovery_account
from tests.agent_core.test_budget_snapshot import root, child, spend, receipt

pytestmark = pytest.mark.unit


def observed_root():
    task = root(); account = _ensure_execution_budget(task); rows = []
    account.bind_account_frontier_observer(rows.append)
    return task, account, rows


def test_parallel_frontier_is_one_total_with_exact_live_actor_cuts():
    task, account, rows = observed_root()
    first = child(task, 'first'); second = child(task, 'second')
    spend(first, 10, 1)
    pending = account.reserve_model(second, input_tokens=2, max_output_tokens=8)
    pending.mark_dispatched()
    saved = deepcopy(rows[-1]); assert saved['snapshot']['tokens'] == 10
    assert not saved['snapshot']['settled'] and not saved['snapshot']['complete']
    assert saved['model_requests'][0]['task_id'] == 'second'
    assert saved['actors']['first']['tokens'] == 10 and saved['actors']['second']['tokens'] == 0
    pending.settle({'prompt_tokens': 2, 'completion_tokens': 8})
    close_execution_budget(first); earlier = receipt(first)
    assert rows[-1]['closed_tokens'] == 10 and rows[-1]['actors']['second']['tokens'] == 10
    assert rows[-1]['snapshot']['tokens'] == 20
    spend(second, 5, 1); close_execution_budget(second)
    final = rows[-1]
    assert final['snapshot']['complete'] and final['snapshot']['tokens'] == 25
    assert final['closed_tokens'] == 25 and final['closed_tool_calls'] == 2
    assert set(final['actors']) == {'source'} and len(account._active) == 1
    target = root('target'); restore = prepare_recovery_account(target, {'first': earlier}, snapshot=final['snapshot'])
    restore(); assert target['_executionBudget'].consumption() == (25, 2)
    spend(target, 5); restore(); assert target['_executionBudget'].consumption() == (30, 2)
    assert [row['version'] for row in rows] == list(range(1, len(rows)+1))
    assert saved['actors']['first']['tokens'] == 10


@pytest.mark.parametrize('boundary', ['bound', 'attached', 'model_dispatched', 'model_settled', 'tool_reserved', 'detached'])
def test_failed_observation_fences_even_unbounded_admission_without_blocking_cleanup(boundary):
    task = root(); task['config'] = {}; account = _ensure_execution_budget(task); rows = []
    def observe(row):
        rows.append(deepcopy(row))
        if row['reason'] == boundary:
            raise OSError('lost durable frontier ACK')
    if boundary == 'bound':
        with pytest.raises(OSError): account.bind_account_frontier_observer(observe)
    else:
        account.bind_account_frontier_observer(observe)
        if boundary == 'attached':
            with pytest.raises(OSError): child(task, 'actor')
        elif boundary == 'tool_reserved':
            with pytest.raises(OSError): account.reserve_tool(task)
            assert account.consumption()[1] == 1
        elif boundary == 'detached':
            actor = child(task, 'actor'); spend(actor, 5); close_execution_budget(actor)
            assert set(account._active) == {'source'} and account.consumption()[0] == 5
        else:
            lease = account.reserve_model(task, input_tokens=0, max_output_tokens=5)
            if boundary == 'model_dispatched':
                with pytest.raises(OSError): lease.mark_dispatched()
            else:
                lease.mark_dispatched(); lease.settle({'completion_tokens': 5})
                assert account.consumption()[0] == 5 and not account._model_leases
    assert account.reason() == 'execution_account_frontier_unavailable'
    assert account.reserve_tool(task) == 'execution_account_frontier_unavailable'
    with pytest.raises(ModelBudgetExceeded): account.reserve_model(task, input_tokens=0, max_output_tokens=1)


def test_concurrent_callbacks_publish_a_contiguous_nonregressing_account_sequence():
    task, account, rows = observed_root(); task['config']['executionLimits']['max_tokens'] = 40
    actors = [child(task, f'actor-{index}') for index in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda actor: spend(actor, 3), actors))
        list(executor.map(close_execution_budget, actors))
    assert rows[-1]['snapshot']['tokens'] == 24
    assert [row['snapshot']['tokens'] for row in rows] == sorted(row['snapshot']['tokens'] for row in rows)
    assert [row['version'] for row in rows] == list(range(1, len(rows)+1))


@pytest.mark.parametrize('damage', ['owner','version','missing','rewind','actor_total','identity','before_bound','seed','unknown'])
def test_goal_account_reader_refuses_changed_or_incomplete_evidence(damage):
    task, account, rows = observed_root(); spend(task, 5)
    metadata = dict(launch_id='launch',task_id='source',owner_user_id=1,account_frontier_format=rows[0]['format'])
    events = [dict(type='execution_account_frontier',launch_id='launch',frontier=deepcopy(row)) for row in rows]
    if damage == 'owner': metadata['owner_user_id'] = 2
    elif damage == 'version': events[-1]['frontier']['version'] += 1
    elif damage == 'missing': events.pop(1)
    elif damage == 'rewind':
        events.append(deepcopy(events[0])); events[-1]['frontier'].update(version=4,reason='model_settled')
    elif damage == 'actor_total': events[-1]['frontier']['actors']['source']['tokens'] += 1
    elif damage == 'identity': events[-1]['launch_id'] = 'other'
    elif damage == 'before_bound': events.insert(0,dict(type='execution_effect_started'))
    elif damage == 'seed': metadata['account_seed'] = deepcopy(rows[-1]['snapshot'])
    else:
        events[-1]['frontier']['snapshot'].update(complete=False,usage_known=False)
        with pytest.raises(RecoveryRefused): recovery_account_snapshot(events,metadata)
        return
    with pytest.raises(RecoveryRefused): read_account_frontier(events,metadata)


def test_frontier_contract_is_closed_and_live_transport_does_not_copy_actors():
    from lib.orchestration.events import live_event_projection
    _, _, rows = observed_root(); valid = rows[0]; validate_frontier(valid)
    for mutation in ({'extra':True}, {'version':True}, {'closed_tokens':-1}):
        with pytest.raises(ValueError): validate_frontier({**valid,**mutation})
    event = dict(type='execution_account_frontier',launch_id='launch',frontier=valid)
    assert live_event_projection(event) == dict(type='execution_account_frontier',launch_id='launch')


def test_whole_account_cannot_be_replaced_by_a_later_receipt():
    task, account, rows = observed_root(); actor = child(task, 'worker')
    spend(actor, 5); close_execution_budget(actor)
    old = deepcopy(rows[-1]['snapshot'])
    spend(task, 5); later = receipt(actor)
    with pytest.raises(RecoveryRefused, match='older than'):
        prepare_recovery_account(root('target'), {'later': later}, snapshot=old)


def test_packaged_frontier_schema_matches_its_single_authority():
    import json
    from pathlib import Path
    from lib.agent_core.account_frontier_contract import SCHEMA
    assert SCHEMA == json.loads((Path(__file__).resolve().parents[2] /
        'contracts/execution_account_frontier_v1.schema.json').read_text())


def test_changed_actor_owner_refuses_before_a_physical_dispatch_can_return():
    task, account, rows = observed_root(); actor = child(task, 'worker')
    lease = account.reserve_model(actor, input_tokens=0, max_output_tokens=5)
    before = deepcopy(rows)
    actor['_userId'] = 2
    with pytest.raises(ValueError, match='foreign participant'): lease.mark_dispatched()
    assert rows == before and account.reason() == 'execution_account_frontier_unavailable'
