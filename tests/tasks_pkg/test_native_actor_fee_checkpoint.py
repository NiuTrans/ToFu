"""Actor-local native checkpoints retain original payers across paid generations.

Accounting fixtures supply already accepted/rebound lineage. They grant neither
native actor takeover nor automatic Goal execution; those owners stay fenced.
"""
import asyncio
from copy import deepcopy

import pytest
from jsonschema import ValidationError

from lib.agent_core.native_process import RoundRequest, RoundSettlement
from lib.billing.cost import native_task_cost, transfer_native_child_fees
from lib.billing.native_actor_fees import capture
from lib.tasks_pkg.native_cost_projection import NativeTaskCostPolicy
from tests.agent_core.test_native_task_terminal import principal
from tests.billing.test_recovered_actor_fee_transfer import root, child
from tests.tasks_pkg.test_native_cost_projection import Consumer, evidence

pytestmark = pytest.mark.unit
PROFILE = dict(encoding='cl100k_base', output_field='max_tokens', wire_model='alpha-wire')


def policy_for(task):
    policy = NativeTaskCostPolicy(principal=principal(), task=task, policy=Consumer())
    policy.bind_pricing_route(dict(owner_user_id=7, logical_model='alpha', provider_id='provider-a'))
    return policy


def request(task, ordinal=0):
    return RoundRequest(principal(), task['id'], 'round-'+str(ordinal), ordinal, ordinal+1)


async def spend(task, policy):
    budget = task['_executionBudget']
    lease = budget.reserve_model(task, input_tokens=10000, max_output_tokens=200)
    lease.mark_dispatched(); lease.settle(evidence()['usage'])
    await policy.settle(RoundSettlement(request(task, len(task.get('apiRounds', []))), evidence()))


def checkpoint(task, policy):
    state = dict(owner_user_id=7, task_id=task['id'], next_round=len(task['apiRounds']),
        usage=[deepcopy(row['usage']) for row in task['apiRounds']],
        shared_model_account=task['_executionBudget'].capture_actor_account(
            owner=7, task=task, counter_policy=PROFILE))
    state['host_fee_projection'] = policy.capture_fee_checkpoint(request(task), state)
    return state


async def generation(number, previous=None):
    parent, account = root('root-'+str(number))
    cuts = []
    if previous:
        account.restore_pending_frontier(parent, previous['cut'], owner=7)
    account.bind_account_frontier_observer(lambda cut: cuts.append(deepcopy(cut)))
    actor = child(parent, 'actor-'+str(number))
    if previous:
        account.rebind_actor_prefix(actor, source_task_id=previous['actor']['id'], owner=7)
    policy = policy_for(actor)
    if previous:
        state = deepcopy(previous['state'])
        state['task_id'] = actor['id']
        state['host_fee_projection']['task_id'] = actor['id']
        state['shared_model_account'] = account.capture_actor_account(owner=7, task=actor, counter_policy=PROFILE)
        before = deepcopy(account.continuity_snapshot())
        commit = policy.prepare_fee_checkpoint_restore(request(actor), state)
        assert not actor['apiRounds']
        commit()
        assert account.continuity_snapshot() == before
        assert all(row['billableTaskId'] != parent['id'] for row in actor['_checkpointApiRounds'])
    await spend(actor, policy)
    return dict(parent=parent, actor=actor, policy=policy, state=checkpoint(actor, policy), cut=cuts[-1], cuts=cuts)


@pytest.mark.parametrize('damage', [None, 'producer', 'new_spend', 'batch'])
def test_pending_file_fee_restore_preserves_already_charged_tool_prefix(damage):
    async def run():
        from lib.conversation_sync.turn_images import turn_image_owner_scope
        original=await generation(0)
        assert original['actor']['_executionBudget'].reserve_tool(original['actor']) is None
        parent,account=root('successor-root')
        account.restore_pending_frontier(parent,original['cuts'][-1],owner=7)
        account.bind_account_frontier_observer(lambda cut:None)
        actor=child(parent,'successor-actor')
        actor['convId']='conversation'
        account.rebind_actor_prefix(actor,source_task_id=original['actor']['id'],owner=7)
        state=deepcopy(original['state'])
        state['task_id']=actor['id'];state['host_fee_projection']['task_id']=actor['id']
        state['shared_model_account'].update(root_task_id=parent['id'],actor_task_id=actor['id'])
        state.update(tools_pending=True,messages=[dict(role='assistant',tool_calls=[dict(
            id='call',type='function',function=dict(name='write_file',arguments='{}'))])],
            tool_recovery=dict(format='tofu.native-file-tool-recovery/v1',producer_task_id='actor-0',calls=[
                dict(call_id='call',inspection=dict(conversationId='conversation',invocationId='actor-0',
                    operationId='a'*32,ownerScope=turn_image_owner_scope(7,'conversation')))]))
        if damage=='producer':state['tool_recovery']['producer_task_id']='foreign'
        elif damage=='new_spend':assert account.reserve_tool(actor) is None
        elif damage=='batch':state['tool_recovery']['calls']=[]
        before=account.consumption()
        commit=policy_for(actor).prepare_fee_checkpoint_restore(request(actor),state)
        if damage:
            with pytest.raises((ValueError,ValidationError)):commit()
            assert actor['apiRounds']==[]
        else:
            commit()
            assert len(actor['_checkpointApiRounds'])==1
            assert actor['apiRounds'][0]['billableTaskId']=='root-0'
        assert account.consumption()==before
    asyncio.run(run())


def test_three_actor_generations_keep_local_rounds_and_independent_original_invoices():
    async def run():
        previous, family = None, []
        for number in range(3):
            current = await generation(number, previous)
            actor, parent = current['actor'], current['parent']
            fees = current['state']['host_fee_projection']
            assert 'child_rounds' not in fees and 'wallet_settlement' not in fees
            assert [(row['task_id'], row['billable_task_id']) for row in fees['rounds']] == [
                ('actor-'+str(i), 'root-'+str(i)) for i in range(number+1)]
            assert current['state']['shared_model_account']['tokens'] == 10200*(number+1)
            actor['status'] = 'done'
            transfer_native_child_fees(parent, actor)
            parent['_executionBudget'].close(actor)
            assert native_task_cost(parent, model='alpha').micro == 7593
            proof = capture(actor)
            assert [row['payer_task_id'] for row in proof['rounds']] == ['root-'+str(i) for i in range(number+1)]
            family.append(current); previous = current
        assert len(family[-1]['actor']['_checkpointApiRounds']) == 2
    asyncio.run(run())


@pytest.mark.parametrize('damage', ['missing', 'empty', 'producer', 'current_payer',
                                   'historical_payer', 'public_round', 'root_scope', 'prefix', 'unbound'])
def test_invalid_actor_payer_transport_refuses_without_projecting_or_spending(damage):
    async def run():
        original = await generation(0)
        current = await generation(1, original)
        actor = current['actor']; state = deepcopy(current['state'])
        fees = state['host_fee_projection']
        if damage == 'missing': fees['rounds'][0].pop('billable_task_id')
        elif damage == 'empty': fees['rounds'][0]['billable_task_id'] = ''
        elif damage == 'producer': fees['rounds'][0]['task_id'] = current['parent']['id']
        elif damage == 'current_payer': fees['rounds'][1]['billable_task_id'] = 'root-0'
        elif damage == 'historical_payer': fees['rounds'][0]['billable_task_id'] = current['parent']['id']
        elif damage == 'public_round': fees['rounds'][0]['public_round'] = 1
        elif damage == 'root_scope': state['shared_model_account'].pop('actor_task_id')
        elif damage == 'prefix': actor['_checkpointApiRounds'][0]['billableTaskId'] = 'changed'
        else:
            # The same spent total is insufficient without an acknowledged rebind.
            fresh_parent, _ = root('root-1')
            actor = child(fresh_parent, 'actor-1')
            policy = policy_for(actor)
            await spend(actor, policy); await spend(actor, policy)
            actor.update(apiRounds=[], usage={}, cost=None)
        policy = policy_for(actor)
        before = deepcopy(actor['apiRounds'])
        budget_before = deepcopy(actor['_executionBudget'].continuity_snapshot())
        with pytest.raises((ValueError, ValidationError)):
            commit = policy.prepare_fee_checkpoint_restore(request(actor), state)
            commit()
        assert actor['apiRounds'] == before
        assert actor['_executionBudget'].continuity_snapshot() == budget_before
    asyncio.run(run())


def test_local_fee_restore_does_not_require_sibling_invoice_or_hide_unknown_usage():
    async def run():
        current = await generation(0)
        actor, parent = current['actor'], current['parent']
        sibling = child(parent, 'sibling')
        account = actor['_executionBudget']
        lease = account.reserve_model(sibling, input_tokens=10, max_output_tokens=2)
        lease.mark_dispatched(); lease.settle(None)
        before = deepcopy(account.continuity_snapshot())
        rows = deepcopy(actor['apiRounds'])
        actor.update(apiRounds=[], usage={}, cost=None)
        policy = policy_for(actor)
        policy.prepare_fee_checkpoint_restore(request(actor), current['state'])()
        assert actor['apiRounds'] == rows
        assert account.continuity_snapshot() == before and not account.actuals_complete()
    asyncio.run(run())


def test_actor_capture_cannot_swap_historical_payer_after_the_accepted_prefix():
    async def run():
        current = await generation(1, await generation(0))
        current['actor']['apiRounds'][0]['billableTaskId'] = 'another-root'
        with pytest.raises(ValueError, match='accepted prefix'):
            checkpoint(current['actor'], current['policy'])
    asyncio.run(run())
