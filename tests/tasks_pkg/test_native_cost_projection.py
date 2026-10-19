"""Native fee snapshots reuse task fields, survive retry and never reprice."""
import asyncio
import copy
from unittest.mock import patch
import pytest
from lib.agent_core.native_process import RoundRequest,RoundSettlement
from lib.tasks_pkg.native_cost_projection import NativeTaskCostPolicy
from tests.agent_core.test_native_task_terminal import principal
pytestmark=pytest.mark.unit

def evidence():
    return {'kind':'returned','usage':{'prompt_tokens':10000,'completion_tokens':200,'cache_read_tokens':9000},'usage_observation':{'tokens':10200,'complete':True},'cost':{
        'amounts':dict(costUsd=.0068,costCny=.0489,inputTokens=1000,outputTokens=200,totalInputTokens=10000,cacheWriteTokens=0,cacheReadTokens=9000,thinkingTokens=0,inputCostUsd=.0025,outputCostUsd=.002,cacheWriteCostUsd=0.,cacheReadCostUsd=.00225,inputCostCny=.0181,outputCostCny=.01448,cacheWriteCostCny=0.,cacheReadCostCny=.01629,cacheSavingsUsd=.02025,cacheSavingsCny=.14661),
        'price':dict(prompt_tokens=10000,input=2.5,output=10.,cache_write_multiplier=1.25,cache_read_multiplier=.1,usd_to_cny=7.24,currency='USD',margin=.125),
        'credits':dict(input=2500,output=2000,cache_read=2250,cache_write=0,base=6750,margin=843,total=7593)}}
class Consumer:
    def __init__(self):self.calls=0;self.fail=False
    async def reserve(self,request):return True
    async def settle(self,result):
        self.calls+=1
        if self.fail:raise OSError('existing delivery failed')

def setup():
    task={'id':'task','_userId':7,'status':'running'};consumer=Consumer()
    policy=NativeTaskCostPolicy(principal=principal(),task=task,policy=consumer)
    policy.bind_pricing_route(dict(owner_user_id=7,logical_model='alpha',provider_id='provider-a'))
    result=RoundSettlement(RoundRequest(principal(),'task','round-0',0,1),evidence())
    return task,consumer,policy,result

def test_snapshots_retry_once_and_do_not_call_price_arithmetic():
    async def run():
        task,consumer,policy,result=setup()
        with patch('lib.cost.compute_cost',side_effect=AssertionError('must not reprice')):
            await policy.settle(result);await policy.settle(result)
        assert consumer.calls==1 and len(task['apiRounds'])==1
        assert task['cost']['costUsd']==.0068
        assert task['apiRounds'][0]['nativeCredits']['total']==7593
        assert task['apiRounds'][0]['cost']['pricingSnapshot']['model']=='alpha'
        from lib.storage.storage_projection import sanitize_api_rounds_for_persist
        assert sanitize_api_rounds_for_persist(task['apiRounds'])[0]['nativeCredits']['total']==7593
    asyncio.run(run())

def test_delegate_failure_keeps_paid_projection_and_retry_never_appends_twice():
    async def run():
        task,consumer,policy,result=setup();consumer.fail=True
        with pytest.raises(OSError):await policy.settle(result)
        assert len(task['apiRounds'])==1 and task['cost']['costUsd']==.0068
        with pytest.raises(ValueError,match='delivery debt'):
            await policy.reserve(result.request)
        assert consumer.calls==1
        consumer.fail=False;await policy.settle(result)
        assert await policy.reserve(result.request) is True
        assert len(task['apiRounds'])==1 and consumer.calls==2
        changed=copy.deepcopy(result.evidence);changed['cost']['credits']['total']=0
        with pytest.raises(ValueError):await policy.settle(RoundSettlement(result.request,changed))
        assert len(task['apiRounds'])==1
    asyncio.run(run())

@pytest.mark.parametrize('change',['owner','unknown','amount','credit'])
def test_unproven_quote_refuses_before_task_projection(change):
    async def run():
        task,consumer,policy,result=setup();raw=copy.deepcopy(result.evidence)
        if change=='owner':task['_userId']=8
        elif change=='unknown':raw['usage_observation']['complete']=False
        elif change=='amount':raw['cost']['amounts']['inputCostUsd']=float('inf')
        else:raw['cost']['credits']['total']+=1
        with pytest.raises(ValueError):await policy.settle(RoundSettlement(result.request,raw))
        assert 'apiRounds' not in task and consumer.calls==0
    asyncio.run(run())


def checkpoint_state(owner_task, result):
    from lib.agent_core.execution_budget import ExecutionBudget
    budget=ExecutionBudget(owner_task);owner_task['_executionBudget']=budget
    lease=budget.reserve_model(owner_task,input_tokens=10000,max_output_tokens=200)
    lease.mark_dispatched();lease.settle(result.evidence['usage'])
    return dict(owner_user_id=7,task_id='task',next_round=1,usage=[result.evidence['usage']],
        shared_model_account=budget.capture_shared_account(owner=7,task=owner_task,
            counter_policy=dict(encoding='cl100k_base',output_field='max_tokens',wire_model='alpha-wire')))


@pytest.mark.parametrize('damage', [None, 'root', 'actor', 'tree', 'unknown', 'reservation'])
def test_actor_fee_checkpoint_preserves_only_its_original_quote(damage):
    async def run():
        from lib.agent_core.execution_budget import _ensure_execution_budget, inherit_execution_budget
        task, consumer, policy, result = setup(); task['config'] = {}
        root = dict(id='flow-root', _userId=7, config={}, status='running')
        budget = _ensure_execution_budget(root); inherit_execution_budget(root, task)
        sibling = dict(id='sibling', _userId=7, config={}, usage={'prompt_tokens': 20400}, status='running')
        inherit_execution_budget(root, sibling)
        lease = budget.reserve_model(task, input_tokens=10000, max_output_tokens=200)
        lease.mark_dispatched(); lease.settle(result.evidence['usage'])
        await policy.settle(result)
        state = dict(owner_user_id=7, task_id=task['id'], next_round=1, usage=[result.evidence['usage']],
            shared_model_account=budget.capture_actor_account(owner=7, task=task,
                counter_policy=dict(encoding='cl100k_base', output_field='max_tokens', wire_model='alpha-wire')))
        saved = state['shared_model_account']
        if damage == 'root': saved['root_task_id'] = 'foreign'
        elif damage == 'actor': saved['actor_task_id'] = 'sibling'
        elif damage == 'tree': saved['root_tokens'] = 10200
        elif damage == 'unknown': saved['complete'] = False
        elif damage == 'reservation': task['_billing_reservation_micro'] = 100
        if damage:
            with pytest.raises(ValueError): policy.capture_fee_checkpoint(result.request, state)
        else:
            fees = policy.capture_fee_checkpoint(result.request, state)
            assert fees['rounds'][0]['task_id'] == task['id']
            assert fees['rounds'][0]['billable_task_id'] == root['id']
            assert fees['rounds'][0]['cost']['credits']['total'] == 7593
            assert fees['wallet_user_id'] == '' and 'wallet_settlement' not in fees
            assert saved['tokens'] == 10200 and budget.consumption()[0] == 30600
        assert len(task['apiRounds']) == consumer.calls == 1
    asyncio.run(run())


def test_paid_round_append_does_not_mutate_accepted_projection_prefix():
    async def run():
        task,consumer,policy,result=setup()
        await policy.settle(result)
        accepted_prefix=task['apiRounds']
        frozen=copy.deepcopy(accepted_prefix)
        request=RoundRequest(principal(),'task','next',1,2)
        await policy.settle(RoundSettlement(request,evidence()))
        assert accepted_prefix==frozen and len(accepted_prefix)==1
        assert len(task['apiRounds'])==2
        assert task['apiRounds'] is not accepted_prefix
        assert consumer.calls==2
    asyncio.run(run())


def test_new_continue_execution_appends_paid_round_without_renumbering_or_repricing_prefix():
    async def run():
        task,consumer,policy,result=setup()
        await policy.settle(result)
        prefix=copy.deepcopy(task['apiRounds'])
        task['_checkpointApiRounds']=copy.deepcopy(prefix)
        next_policy=NativeTaskCostPolicy(principal=principal(),task=task,policy=consumer)
        next_policy.bind_pricing_route(dict(owner_user_id=7,logical_model='alpha',provider_id='provider-a'))
        restarted=RoundSettlement(RoundRequest(principal(),'task','new-attempt-first',0,1),evidence())
        with patch('lib.cost.compute_cost',side_effect=AssertionError('no repricing accepted prefix')):
            await next_policy.settle(restarted)
            await next_policy.settle(restarted)
        assert task['apiRounds'][0]==prefix[0]
        assert [row['round'] for row in task['apiRounds']]==[1,2]
        assert consumer.calls==2
    asyncio.run(run())


@pytest.mark.parametrize('legacy_round', [False, True])
def test_checkpoint_fee_restore_follows_account_and_preserves_old_price(legacy_round):
    async def run():
        task,consumer,policy,result=setup()
        state=checkpoint_state(task,result)
        await policy.settle(result)
        state['host_fee_projection']=policy.capture_fee_checkpoint(result.request,state)
        fresh,delivery,restored,_=setup()
        from lib.agent_core.execution_budget import ExecutionBudget
        fresh['_executionBudget']=ExecutionBudget(fresh)
        with patch('lib.cost.compute_cost',side_effect=AssertionError('no repricing')):
            assert state['host_fee_projection']['rounds'][0]['task_id']=='task'
            if legacy_round:
                state['host_fee_projection']['rounds'][0].pop('task_id')
            commit=restored.prepare_fee_checkpoint_restore(result.request,state)
            with pytest.raises(ValueError,match='account restore'):commit()
            assert 'apiRounds' not in fresh
            fresh['_executionBudget'].restore_shared_account(state['shared_model_account'],owner=7,
                task=fresh,counter_policy=state['shared_model_account']['counter_policy'])
            assert commit() is None
        assert fresh['apiRounds']==task['apiRounds']
        assert fresh['cost']==task['cost'] and fresh['usage']==task['usage']
        assert fresh['_executionBudget'].consumption_snapshot()==(10200,0,True)
        assert delivery.calls==0
        next_result=RoundSettlement(RoundRequest(principal(),'task','round-1',1,1),evidence())
        await restored.settle(next_result)
        assert len(fresh['apiRounds'])==2 and delivery.calls==1
    asyncio.run(run())


def test_hydrated_checkpoint_keeps_inherited_quote_executor_origin():
    async def run():
        task,consumer,policy,result=setup()
        state=checkpoint_state(task,result)
        await policy.settle(result)
        task['apiRounds'][0]['taskId']='original-quote-executor'
        state['host_fee_projection']=policy.capture_fee_checkpoint(result.request,state)
        assert state['host_fee_projection']['rounds'][0]['task_id']=='original-quote-executor'
        fresh,delivery,restored,_=setup()
        from lib.agent_core.execution_budget import ExecutionBudget
        fresh['_executionBudget']=ExecutionBudget(fresh)
        for target,source in (('apiRounds','apiRounds'),('_checkpointApiRounds','apiRounds'),
                ('usage','usage'),('_checkpointUsage','usage'),('cost','cost')):
            fresh[target]=copy.deepcopy(task[source])
        commit=restored.prepare_fee_checkpoint_restore(result.request,state)
        fresh['_executionBudget'].restore_shared_account(state['shared_model_account'],owner=7,
            task=fresh,counter_policy=state['shared_model_account']['counter_policy'])
        commit()
        assert fresh['apiRounds']==task['apiRounds']
        from lib.billing.cost import native_task_cost
        assert native_task_cost(fresh,model='alpha').micro==0
        assert delivery.calls==0
    asyncio.run(run())


@pytest.mark.parametrize('fault',[None,'row','usage','cost','authority','changed_after_prepare'])
def test_hydrated_continue_fees_must_equal_journal_before_account_recovery(fault):
    async def run():
        task,consumer,policy,result=setup()
        state=checkpoint_state(task,result)
        await policy.settle(result)
        task['apiRounds'][0]['taskId']='prior-executor'
        state['host_fee_projection']=policy.capture_fee_checkpoint(result.request,state)
        fresh,delivery,restored,_=setup()
        from lib.agent_core.execution_budget import ExecutionBudget
        fresh['_executionBudget']=ExecutionBudget(fresh)
        for target,source in (('apiRounds','apiRounds'),('_checkpointApiRounds','apiRounds'),
                ('usage','usage'),('_checkpointUsage','usage'),('cost','cost')):
            fresh[target]=copy.deepcopy(task[source])
        if fault=='row':fresh['apiRounds'][0]['nativePrice']['input']+=1
        elif fault=='usage':fresh['usage']['completion_tokens']+=1
        elif fault=='cost':fresh['cost']['costUsd']+=1
        elif fault=='authority':fresh.pop('_checkpointApiRounds')
        before=copy.deepcopy({key:fresh.get(key) for key in ('apiRounds','usage','cost')})
        if fault in {'row','usage','cost','authority'}:
            with pytest.raises(ValueError,match='hydrated checkpoint'):
                restored.prepare_fee_checkpoint_restore(result.request,state)
            assert not fresh['_executionBudget']._model_account_restored
            assert fresh['_executionBudget']._model_ordinal==0
        else:
            commit=restored.prepare_fee_checkpoint_restore(result.request,state)
            fresh['_executionBudget'].restore_shared_account(state['shared_model_account'],owner=7,
                task=fresh,counter_policy=state['shared_model_account']['counter_policy'])
            if fault=='changed_after_prepare':
                fresh['_checkpointUsage']['completion_tokens']+=1
                with pytest.raises(ValueError,match='task changed'):commit()
                assert not fresh.get('_nativeFrozenCostProjection')
            else:
                commit()
                assert len(fresh['apiRounds'])==1
                await restored.settle(RoundSettlement(RoundRequest(principal(),'task','next',1,1),evidence()))
                assert len(fresh['apiRounds'])==2
                assert fresh['apiRounds'][0]==before['apiRounds'][0]
        if fault is not None:
            assert {key:fresh.get(key) for key in before}==before
            assert delivery.calls==0
    asyncio.run(run())


@pytest.mark.parametrize('fault',['missing','route','owner','tenant','usage','tokens','round','credit','unknown',
    'empty_origin','nonstring_origin'])
def test_invalid_fee_checkpoint_refuses_before_account_restore(fault):
    async def run():
        task,consumer,policy,result=setup();state=checkpoint_state(task,result)
        await policy.settle(result)
        state['host_fee_projection']=policy.capture_fee_checkpoint(result.request,state)
        state=copy.deepcopy(state);projection=state['host_fee_projection']
        if fault=='missing':state.pop('host_fee_projection')
        elif fault=='route':projection['provider_id']='foreign'
        elif fault=='owner':projection['owner_user_id']=8
        elif fault=='tenant':projection['tenant_id']='foreign'
        elif fault=='usage':state['usage'][0]['prompt_tokens']=9000
        elif fault=='tokens':state['shared_model_account']['tokens']+=1
        elif fault=='round':projection['rounds'][0]['round']=2
        elif fault=='credit':projection['rounds'][0]['cost']['credits']['total']+=1
        elif fault=='empty_origin':projection['rounds'][0]['task_id']=''
        elif fault=='nonstring_origin':projection['rounds'][0]['task_id']=False
        else:state['shared_model_account']['complete']=False
        fresh,delivery,restored,_=setup()
        from lib.agent_core.execution_budget import ExecutionBudget
        from jsonschema.exceptions import ValidationError
        fresh['_executionBudget']=ExecutionBudget(fresh)
        with pytest.raises((ValueError,ValidationError)):
            restored.prepare_fee_checkpoint_restore(result.request,state)
        assert fresh['_executionBudget'].consumption_snapshot()==(0,0,True)
        assert 'apiRounds' not in fresh and delivery.calls==0
    asyncio.run(run())


def test_zero_fee_restore_still_requires_original_attempt_ordinal():
    async def run():
        task,consumer,policy,result=setup()
        raw=evidence();raw['usage']={'prompt_tokens':0,'completion_tokens':0}
        raw['cost']['amounts']=None;raw['cost']['price']['prompt_tokens']=0
        raw['cost']['credits']={key:0 for key in raw['cost']['credits']}
        result=RoundSettlement(result.request,raw)
        state=checkpoint_state(task,result);await policy.settle(result)
        state['host_fee_projection']=policy.capture_fee_checkpoint(result.request,state)
        fresh,delivery,restored,_=setup()
        from lib.agent_core.execution_budget import ExecutionBudget
        fresh['_executionBudget']=ExecutionBudget(fresh)
        commit=restored.prepare_fee_checkpoint_restore(result.request,state)
        with pytest.raises(ValueError,match='account restore'):commit()
        assert 'apiRounds' not in fresh
        fresh['_executionBudget'].restore_shared_account(state['shared_model_account'],owner=7,
            task=fresh,counter_policy=state['shared_model_account']['counter_policy'])
        commit()
        assert fresh['apiRounds'][0]['nativeCredits']['total']==0
        assert state['shared_model_account']['model_ordinal']==1
        assert fresh['cost'] is None
    asyncio.run(run())


async def tree_checkpoint_fixture(child_model="alpha",child_provider="provider-a"):
    from lib.billing.cost import transfer_native_child_fees
    task, consumer, policy, result = setup()
    state = checkpoint_state(task, result)
    await policy.settle(result)
    child = dict(id='native-child', _userId=7, status='running', config={},
        _parentTaskId=task['id'], _executionBudget=task['_executionBudget'],
        usage={}, _executionActualsComplete=True)
    child_policy = NativeTaskCostPolicy(principal=principal(), task=child, policy=Consumer())
    child_policy.bind_pricing_route(dict(owner_user_id=7, logical_model=child_model, provider_id=child_provider))
    await child_policy.settle(RoundSettlement(RoundRequest(principal(),child['id'],'child-round',0,1),evidence()))
    account = task['_executionBudget']
    account.attach(child, parent_task_id=task['id']); child['status']='done'
    transfer_native_child_fees(task, child); account.close(child)
    state['shared_model_account'] = account.capture_shared_account(owner=7, task=task,
        counter_policy=state['shared_model_account']['counter_policy'])
    state['host_fee_projection'] = policy.capture_fee_checkpoint(result.request, state)
    assert state['shared_model_account']['root_tokens']==10200
    assert state['shared_model_account']['tokens']==20400
    assert state['host_fee_projection']['child_rounds'][0]['task_id']==child['id']
    return task, policy, result, state


@pytest.mark.parametrize("child_model,child_provider", [("alpha","provider-a"),("beta","provider-b")])
def test_tree_fee_checkpoint_retains_child_rows_and_restores_one_wallet_coverage(child_model,child_provider):
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.billing.cost import native_task_cost
    async def run():
        task, policy, result, state = await tree_checkpoint_fixture(child_model,child_provider)
        original = copy.deepcopy(task['apiRounds'])
        if child_model!='alpha':
            assert state['host_fee_projection']['child_rounds'][0]['route']==dict(logical_model=child_model,provider_id=child_provider)
        fresh, delivery, restored, _ = setup()
        fresh['_executionBudget'] = ExecutionBudget(fresh)
        commit = restored.prepare_fee_checkpoint_restore(result.request, state)
        fresh['_executionBudget'].restore_shared_account(state['shared_model_account'],owner=7,
            task=fresh,counter_policy=state['shared_model_account']['counter_policy'])
        commit()
        assert fresh['apiRounds']==original
        assert native_task_cost(fresh,model='alpha').micro==15186
        assert fresh['usage']==task['usage']
        await restored.settle(RoundSettlement(RoundRequest(principal(),'task','root-next',1,1),evidence()))
        assert [row['round'] for row in fresh['apiRounds']]==[1,2,3]
        assert fresh['apiRounds'][:2]==original
        assert delivery.calls==1
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['executor', 'duplicate', 'receiver', 'root_tokens', 'public_round', 'unknown', 'route_missing_provider', 'route_empty_model', 'route_unknown_field'])
def test_invalid_tree_fee_checkpoint_refuses_before_account_or_projection_mutation(fault):
    from lib.agent_core.execution_budget import ExecutionBudget
    async def run():
        _, _, result, state = await tree_checkpoint_fixture()
        state = copy.deepcopy(state)
        child = state['host_fee_projection']['child_rounds'][0]
        if fault == 'executor':
            child['task_id'] = 'task'
        elif fault == 'duplicate':
            state['host_fee_projection']['child_rounds'].append(copy.deepcopy(child))
        elif fault == 'receiver':
            child['billable_task_id'] = 'foreign-root'
        elif fault == 'root_tokens':
            state['shared_model_account']['root_tokens'] += 1
        elif fault == 'public_round':
            child['round'] = 1
        elif fault=='unknown':
            child['usage'] = {}
        else:
            child['route']=dict(logical_model='beta',provider_id='provider-b')
            if fault=='route_missing_provider':child['route'].pop('provider_id')
            elif fault=='route_empty_model':child['route']['logical_model']=''
            else:child['route']['credential']='unissued'
        fresh, _, restored, _ = setup()
        fresh['_executionBudget'] = ExecutionBudget(fresh)
        from jsonschema import ValidationError
        with pytest.raises((ValueError,ValidationError)):
            restored.prepare_fee_checkpoint_restore(result.request, state)
        assert fresh['_executionBudget'].consumption_snapshot() == (0, 0, True)
        assert 'apiRounds' not in fresh and '_nativeChildFeeGrants' not in fresh
    asyncio.run(run())


def test_same_executor_recovery_bills_original_child_work_without_foreign_task_discount():
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.billing.cost import native_task_cost
    async def run():
        source, _, request, state = await tree_checkpoint_fixture()
        fresh, _, restored, _ = setup()
        for target, origin in (('apiRounds','apiRounds'),('_checkpointApiRounds','apiRounds'),
                ('usage','usage'),('_checkpointUsage','usage'),('cost','cost')):
            fresh[target] = copy.deepcopy(source[origin])
        fresh['_executionBudget'] = ExecutionBudget(fresh)
        commit = restored.prepare_fee_checkpoint_restore(request.request,state)
        fresh['_executionBudget'].restore_shared_account(state['shared_model_account'],owner=7,
            task=fresh,counter_policy=state['shared_model_account']['counter_policy'])
        commit()
        assert fresh['apiRounds'] == source['apiRounds']
        assert native_task_cost(fresh,model='alpha').micro == 15186
        assert not fresh.get('_checkpointApiRounds')
        assert fresh['usage'] == source['usage']
    asyncio.run(run())


def test_same_executor_recovery_keeps_older_executor_prefix_paid_elsewhere():
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.billing.cost import native_task_cost
    from lib.cost import compute_api_rounds_cost
    async def run():
        source, _, result, state = await tree_checkpoint_fixture()
        old = copy.deepcopy(source['apiRounds'])
        old[0]['taskId']='older-root'
        old[1].update(taskId='older-child',billableTaskId='older-root')
        current = copy.deepcopy(source['apiRounds'])
        for row in current:row['round']+=2
        rows=[*old,*current]
        native_old=copy.deepcopy(state['host_fee_projection']['rounds'][0])
        native_old.update(task_id='older-root',public_round=1)
        native_current=copy.deepcopy(state['host_fee_projection']['rounds'][0])
        native_current.update(round=2,public_round=3)
        child_old=copy.deepcopy(state['host_fee_projection']['child_rounds'][0])
        child_old.update(task_id='older-child',billable_task_id='older-root')
        child_current=copy.deepcopy(state['host_fee_projection']['child_rounds'][0])
        child_current['round']=4
        state['host_fee_projection']['rounds']=[native_old,native_current]
        state['host_fee_projection']['child_rounds']=[child_old,child_current]
        state['usage']=[copy.deepcopy(result.evidence['usage']) for _ in range(2)]
        state['next_round']=2
        state['shared_model_account'].update(tokens=40800,root_tokens=20400,model_ordinal=4)
        fresh, _, restored, _=setup()
        usage={}
        from lib.cost import accumulate_usage_into
        for report in state['usage']:accumulate_usage_into(usage,report)
        fresh.update(apiRounds=rows,_checkpointApiRounds=copy.deepcopy(rows),usage=usage,
            _checkpointUsage=copy.deepcopy(usage),cost=compute_api_rounds_cost(rows,use_frozen_costs=True))
        fresh['_executionBudget']=ExecutionBudget(fresh)
        commit=restored.prepare_fee_checkpoint_restore(result.request,state)
        fresh['_executionBudget'].restore_shared_account(state['shared_model_account'],owner=7,
            task=fresh,counter_policy=state['shared_model_account']['counter_policy'])
        commit()
        assert fresh['_checkpointApiRounds']==old
        assert fresh['apiRounds']==rows
        assert native_task_cost(fresh,model='alpha').micro==15186
    asyncio.run(run())


def test_same_executor_recovery_refuses_foreign_billing_interleaved_after_current_work():
    from lib.agent_core.execution_budget import ExecutionBudget
    async def run():
        source, _, result, state=await tree_checkpoint_fixture()
        source['apiRounds'][1]['billableTaskId']='foreign-root'
        state['host_fee_projection']['child_rounds'][0]['billable_task_id']='foreign-root'
        fresh, _, restored, _=setup()
        fresh.update(apiRounds=copy.deepcopy(source['apiRounds']),
            _checkpointApiRounds=copy.deepcopy(source['apiRounds']),usage=copy.deepcopy(source['usage']),
            _checkpointUsage=copy.deepcopy(source['usage']),cost=copy.deepcopy(source['cost']))
        fresh['_executionBudget']=ExecutionBudget(fresh)
        with pytest.raises(ValueError,match='interleaves'):
            restored.prepare_fee_checkpoint_restore(result.request,state)
        assert fresh['_executionBudget'].consumption_snapshot()[0]==10200
        assert fresh['_checkpointApiRounds']==source['apiRounds']
    asyncio.run(run())


def test_wallet_checkpoint_projects_current_invoice_without_freezing_running_task():
    async def run():
        from lib.billing.native_settlement import freeze
        task, consumer, policy, result = setup()
        task.update(_billingUserId='opaque-wallet', _billing_reservation_micro=100)
        state = checkpoint_state(task, result)
        await policy.settle(result)
        projection = policy.capture_fee_checkpoint(result.request, state)
        capsule = projection['wallet_settlement']
        assert capsule['task_id'] == 'task' and capsule['wallet_user_id'] == 'opaque-wallet'
        assert capsule['reserved_micro'] == 100 and capsule['roundCount'] == 1
        assert capsule['componentsMicro']['totalMicro'] == 7593
        assert '_nativeBillingSettlement' not in task
        lease = task['_executionBudget'].reserve_model(task, input_tokens=10000, max_output_tokens=200)
        lease.mark_dispatched(); lease.settle(result.evidence['usage'])
        second = RoundSettlement(RoundRequest(principal(), 'task', 'round-1', 1, 1), evidence())
        await policy.settle(second)
        final = freeze(task)
        assert final['roundCount'] == 2 and final['componentsMicro']['totalMicro'] == 15186
        assert capsule['roundCount'] == 1 and capsule['componentsMicro']['totalMicro'] == 7593
    asyncio.run(run())


@pytest.mark.parametrize('fault', ['none', 'task', 'owner', 'wallet', 'reserve', 'fee', 'rounds', 'model'])
def test_restored_wallet_invoice_is_bound_to_original_settled_quotes(fault):
    async def run():
        task, _, policy, result = setup()
        task.update(_billingUserId='opaque-wallet', _billing_reservation_micro=100)
        state = checkpoint_state(task, result)
        await policy.settle(result)
        state['host_fee_projection'] = policy.capture_fee_checkpoint(result.request, state)
        capsule = state['host_fee_projection']['wallet_settlement']
        if fault in {'task', 'owner', 'wallet', 'reserve', 'rounds', 'model'}:
            key = dict(task='task_id', owner='owner_user_id', wallet='wallet_user_id',
                reserve='reserved_micro', rounds='roundCount', model='model')[fault]
            capsule[key] = capsule[key] + 1 if type(capsule[key]) is int else capsule[key] + '-forged'
        elif fault == 'fee':
            # Keep schema and arithmetic valid; the original quotes still disagree.
            for key in ('input', 'baseMicro', 'totalMicro'): capsule['componentsMicro'][key] += 1
        fresh, _, restored, _ = setup()
        fresh.update(_billingUserId='opaque-wallet', _billing_reservation_micro=100)
        from lib.agent_core.execution_budget import ExecutionBudget
        budget = ExecutionBudget(fresh); fresh['_executionBudget'] = budget
        def restore():
            commit = restored.prepare_fee_checkpoint_restore(result.request, state)
            budget.restore_shared_account(state['shared_model_account'], owner=7,
                task=fresh, counter_policy=state['shared_model_account']['counter_policy'])
            commit()
        with patch('lib.cost.compute_cost', side_effect=AssertionError('must not reprice')):
            if fault == 'none':
                restore()
                assert fresh['apiRounds'] == task['apiRounds']
                assert '_nativeBillingSettlement' not in fresh
            else:
                with pytest.raises(ValueError): restore()
                assert 'apiRounds' not in fresh and '_nativeBillingSettlement' not in fresh
    asyncio.run(run())
