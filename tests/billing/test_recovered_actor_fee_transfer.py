"""Recovered actor completion retains old debt and bills only fresh work."""
from copy import deepcopy

import pytest

from lib.agent_core.execution_budget import _ensure_execution_budget, inherit_execution_budget
from lib.billing.cost import native_task_cost, transfer_native_child_fees
from tests.billing.test_native_task_cost import task as quote_task

pytestmark = pytest.mark.unit


def root(identity, owner=7):
    task = dict(id=identity, _userId=owner, status='running', config={'model': 'alpha'},
                apiRounds=[], _nativePricingRequired=True)
    return task, _ensure_execution_budget(task)


def child(parent, identity):
    task = dict(id=identity, _userId=parent['_userId'], _parentTaskId=parent['id'], status='running',
                config={'model': 'alpha'}, apiRounds=[], _nativePricingRequired=True,
                _executionActualsComplete=True)
    inherit_execution_budget(parent, task)
    return task


def spend(task):
    lease = task['_executionBudget'].reserve_model(task, input_tokens=10000, max_output_tokens=200)
    lease.mark_dispatched()
    lease.settle({'prompt_tokens': 10000, 'completion_tokens': 200})
    row = deepcopy(quote_task()['apiRounds'][0])
    row.update(round=len(task['apiRounds']) + 1, taskId=task['id'])
    task['apiRounds'].append(row)


def recovered_family(target_cuts=None, fault=None, owner=7):
    source, original = root('source-root', owner)
    cuts = []
    original.bind_account_frontier_observer(lambda cut: cuts.append(deepcopy(cut)))
    old = child(source, 'source-actor')
    spend(old)
    cut = deepcopy(cuts[-1])
    old['status'] = 'done'
    transfer_native_child_fees(source, old)
    original.close(old)
    assert native_task_cost(source, model='alpha').micro == 7593

    target, budget = root('target-root', owner)
    budget.restore_pending_frontier(target, cut, owner=owner)
    def observe(current):
        if target_cuts is not None: target_cuts.append(deepcopy(current))
        if fault is not None and fault.get('armed'): raise OSError('frontier ACK lost')
    budget.bind_account_frontier_observer(observe)
    actor = child(target, 'target-actor')
    budget.rebind_actor_prefix(actor, source_task_id=old['id'], owner=owner)
    # The accepted native handoff must provide the original invoice root.
    # These facts are an accounting fixture, not authority to execute an actor.
    actor['apiRounds'] = deepcopy(old['apiRounds'])
    actor['apiRounds'][0]['billableTaskId'] = source['id']
    actor['_checkpointApiRounds'] = deepcopy(actor['apiRounds'])
    spend(actor)
    actor['status'] = 'done'
    return source, target, actor, budget


def test_completed_recovered_actor_keeps_original_payer_and_only_fresh_charge():
    source, target, actor, budget = recovered_family()
    original = deepcopy(actor['apiRounds'])
    transfer_native_child_fees(target, actor)
    transfer_native_child_fees(target, actor)
    budget.close(actor)
    transfer_native_child_fees(target, actor)
    assert actor['apiRounds'] == original
    assert [(row['taskId'], row['billableTaskId']) for row in target['apiRounds']] == [
        ('source-actor', source['id']), ('target-actor', target['id'])]
    assert budget.consumption_snapshot() == (20400, 0, True)
    assert native_task_cost(target, model='alpha').micro == 7593
    assert native_task_cost(target, model='alpha').snapshot['roundCount'] == 1


def test_second_handoff_preserves_every_original_producer_and_payer():
    cuts = []
    source, target, actor, budget = recovered_family(target_cuts=cuts)
    frontier = deepcopy(cuts[-1])
    successor, account = root('third-root')
    account.restore_pending_frontier(successor, frontier, owner=7)
    next_actor = child(successor, 'third-actor')
    account.rebind_actor_prefix(next_actor, source_task_id=actor['id'], owner=7)
    next_actor['apiRounds'] = deepcopy(actor['apiRounds'])
    for row in next_actor['apiRounds']:
        row.setdefault('billableTaskId', target['id'])
    next_actor['_checkpointApiRounds'] = deepcopy(next_actor['apiRounds'])
    spend(next_actor)
    next_actor['status'] = 'done'
    transfer_native_child_fees(target, actor)
    budget.close(actor)
    transfer_native_child_fees(successor, next_actor)
    account.close(next_actor)
    assert [(row['taskId'], row['billableTaskId']) for row in successor['apiRounds']] == [
        ('source-actor', source['id']), ('target-actor', target['id']), ('third-actor', successor['id'])]
    assert account.consumption_snapshot() == (30600, 0, True)
    assert [native_task_cost(task, model='alpha').micro for task in (source, target, successor)] == [7593] * 3


def test_current_root_work_can_precede_historical_actor_rows_without_rebilling_them():
    _, target, actor, budget = recovered_family()
    spend(target)
    transfer_native_child_fees(target, actor)
    budget.close(actor)
    transfer_native_child_fees(target, actor)
    assert [row['round'] for row in target['apiRounds']] == [1, 2, 3]
    assert [row.get('sourceRound') for row in target['apiRounds']] == [None, 1, 2]
    assert budget.consumption_snapshot() == (30600, 0, True)
    assert native_task_cost(target, model='alpha').micro == 15186


@pytest.mark.parametrize('damage', ['missing_payer', 'current_payer', 'fresh_foreign_payer',
                                   'source_round', 'missing_producer', 'prefix_removed', 'failed_ack'])
def test_unproven_or_changed_prefix_refuses_before_parent_projection(damage):
    fault = {}
    _, target, actor, budget = recovered_family(fault=fault)
    if damage == 'missing_payer': actor['apiRounds'][0].pop('billableTaskId')
    elif damage == 'current_payer': actor['apiRounds'][0]['billableTaskId'] = target['id']
    elif damage == 'fresh_foreign_payer': actor['apiRounds'][1]['billableTaskId'] = 'foreign-root'
    elif damage == 'source_round': actor['apiRounds'][0]['sourceRound'] = 1
    elif damage == 'missing_producer': actor['apiRounds'][0]['taskId'] = ''
    elif damage == 'failed_ack':
        fault['armed'] = True
        actor['status'] = 'running'
        with pytest.raises(OSError): budget.reserve_tool(actor)
        actor['status'] = 'done'
    actor['_checkpointApiRounds'] = [] if damage == 'prefix_removed' else deepcopy(actor['apiRounds'][:1])
    before = deepcopy(target['apiRounds'])
    with pytest.raises(ValueError): transfer_native_child_fees(target, actor)
    assert target['apiRounds'] == before and '_nativeChildFeeGrants' not in target


@pytest.mark.parametrize('damage', ['payer', 'producer', 'credit', 'duplicate', 'prefix'])
def test_transferred_history_is_immutable_in_billing_and_completion_retry(damage):
    _, target, actor, budget = recovered_family()
    transfer_native_child_fees(target, actor)
    budget.close(actor)
    if damage == 'prefix':
        actor['_checkpointApiRounds'][0]['billableTaskId'] = 'changed-root'
    elif damage == 'payer': target['apiRounds'][0]['billableTaskId'] = target['id']
    elif damage == 'producer': target['apiRounds'][0]['taskId'] = 'invented'
    elif damage == 'credit': target['apiRounds'][0]['nativeCredits']['total'] += 1
    else: target['apiRounds'].append(dict(target['apiRounds'][0], round=3))
    with pytest.raises(ValueError): transfer_native_child_fees(target, actor)
    if damage != 'prefix':
        with pytest.raises(ValueError): native_task_cost(target, model='alpha')


def test_paid_prefix_cannot_be_forged_on_an_ordinary_child():
    _, _, recovered, _ = recovered_family()
    target, budget = root('ordinary-root')
    actor = child(target, 'ordinary-actor')
    spend(actor)
    spend(actor)
    actor['apiRounds'][0] = deepcopy(recovered['apiRounds'][0])
    actor['_checkpointApiRounds'] = deepcopy(actor['apiRounds'][:1])
    actor['status'] = 'done'
    with pytest.raises(ValueError, match='acknowledged account rebind'):
        transfer_native_child_fees(target, actor)
    assert target['apiRounds'] == [] and budget.consumption()[0] == 20400
