"""Frozen native credits enter the existing wallet without repricing."""
import copy
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from lib.billing.cost import native_task_cost
from tests.tasks_pkg.test_native_cost_projection import evidence
pytestmark=pytest.mark.unit


def task():
    quote=evidence()['cost']
    amounts=dict(quote['amounts'],pricingSource='native_resolved_price')
    row=dict(round=1,cost=amounts,nativePrice=quote['price'],nativeCredits=quote['credits'])
    return dict(id='native-paid',_nativeFrozenCostProjection=True,apiRounds=[row],
        _executionBudget=SimpleNamespace(consumption_snapshot=lambda:(10200,0,True)))


def test_continue_bills_only_current_executor_while_covering_cumulative_consumption():
    first=task()
    first['apiRounds'][0]['taskId']=first['id']
    paid=native_task_cost(first,model='alpha')
    resumed=copy.deepcopy(first)
    resumed['id']='fresh-continue-executor'
    resumed['_checkpointApiRounds']=copy.deepcopy(first['apiRounds'])
    resumed['_checkpointUsage']={'prompt_tokens':10000,'completion_tokens':200}
    new=copy.deepcopy(first['apiRounds'][0]);new.update(round=2,taskId=resumed['id'])
    resumed['apiRounds'].append(new)
    resumed['_executionBudget']=SimpleNamespace(consumption_snapshot=lambda:(20400,0,True))
    cost=native_task_cost(resumed,model='alpha')
    assert cost.micro==paid.micro
    assert cost.snapshot['roundCount']==1


@pytest.mark.parametrize('fault',['changed_prefix','missing_origin','same_origin','foreign_fresh'])
def test_resume_invoice_refuses_ambiguous_or_changed_executor_attribution(fault):
    owner=task();owner['id']='new-executor'
    old=owner['apiRounds'][0];old['taskId']='old-executor'
    owner['_checkpointApiRounds']=copy.deepcopy([old])
    fresh=copy.deepcopy(old);fresh.update(round=2,taskId=owner['id'])
    owner['apiRounds'].append(fresh)
    owner['_executionBudget']=SimpleNamespace(consumption_snapshot=lambda:(20400,0,True))
    if fault=='changed_prefix':owner['_checkpointApiRounds'][0]['nativeCredits']['total']+=1
    elif fault=='missing_origin':
        old.pop('taskId');owner['_checkpointApiRounds'][0].pop('taskId')
    elif fault=='same_origin':
        old['taskId']=owner['id'];owner['_checkpointApiRounds'][0]['taskId']=owner['id']
    else:fresh['taskId']='unrelated-executor'
    with pytest.raises(ValueError):native_task_cost(owner,model='alpha')


def test_resume_without_new_usage_releases_only_its_own_reservation():
    from lib.billing.request_flow import settle_task
    owner=task();owner['apiRounds'][0]['taskId']='previous-executor'
    owner['_checkpointApiRounds']=copy.deepcopy(owner['apiRounds'])
    owner['_billing_reservation_micro']=100
    with patch('lib.relay_config.billing_enabled',return_value=True), \
         patch('lib.billing.settle',return_value=SimpleNamespace(balance_micro=1000)) as settle:
        result=settle_task(owner,user_id='wallet-owner',model='alpha',raise_on_error=True)
    assert result['cost_micro']==0
    assert settle.call_args.kwargs['actual_micro']==0
    assert owner['_executionBudget'].consumption_snapshot()[0]==10200


def test_native_credits_sum_components_and_per_round_margin_without_repricing():
    owner=task();second=copy.deepcopy(owner['apiRounds'][0]);second['round']=2
    owner['apiRounds'].append(second)
    owner['_executionBudget']=SimpleNamespace(consumption_snapshot=lambda:(20400,0,True))
    with patch('lib.billing.cost.compute_cost',side_effect=AssertionError('no price lookup')):
        result=native_task_cost(owner,model='alpha')
    assert (result.micro,result.base_micro,result.margin_micro)==(15186,13500,1686)
    assert result.components==dict(input=5000,output=4000,cache_read=4500,cache_write=0)
    assert result.snapshot['roundCount']==2


@pytest.mark.parametrize('fault',['unknown','missing','changed','boolean','overflow','duplicate','empty'])
def test_incomplete_or_invalid_native_fees_refuse(fault):
    owner=task();row=owner['apiRounds'][0]
    if fault=='unknown':owner['_executionBudget']=SimpleNamespace(consumption_snapshot=lambda:(10200,0,False))
    elif fault=='missing':owner['_executionBudget']=SimpleNamespace(consumption_snapshot=lambda:(10201,0,True))
    elif fault=='changed':row['nativeCredits']['total']+=1
    elif fault=='boolean':row['nativeCredits']['margin']=True
    elif fault=='overflow':row['nativeCredits'].update(input=9223372036854775807,base=9223372036854775807,total=9223372036854775807)
    elif fault=='duplicate':owner['apiRounds'].append(copy.deepcopy(row))
    else:row['cost']=None
    with pytest.raises(ValueError):native_task_cost(owner,model='alpha')


def test_unquoted_paid_work_never_falls_back_to_python_pricing():
    from lib.billing.request_flow import settle_task
    owner=dict(id='paid-without-quote',_nativePricingRequired=True,
        _executionBudget=SimpleNamespace(consumption_snapshot=lambda:(5,0,True)))
    with patch('lib.relay_config.billing_enabled',return_value=True), \
         patch('lib.billing.compute_request_cost') as reprice, patch('lib.billing.debit') as debit:
        with pytest.raises(ValueError):
            settle_task(owner,user_id='wallet-owner',model='alpha',raise_on_error=True)
    reprice.assert_not_called();debit.assert_not_called()


def test_unsent_native_zero_releases_existing_reservation():
    from lib.billing.request_flow import settle_task
    owner=dict(id='native-unsent',_nativePricingRequired=True,_billing_reservation_micro=100,
        _executionBudget=SimpleNamespace(consumption_snapshot=lambda:(0,0,True)))
    with patch('lib.relay_config.billing_enabled',return_value=True), \
         patch('lib.billing.compute_request_cost') as reprice, \
         patch('lib.billing.settle',return_value=SimpleNamespace(balance_micro=1000)) as settle:
        result=settle_task(owner,user_id='wallet-owner',model='alpha',raise_on_error=True)
    reprice.assert_not_called()
    assert result['cost_micro']==0
    assert settle.call_args.kwargs['actual_micro']==0


def test_combined_native_rounds_cannot_overflow_wallet_integer():
    owner=task();row=owner['apiRounds'][0]
    row['nativeCredits']=dict(input=9223372036854775807,output=0,cache_read=0,
        cache_write=0,base=9223372036854775807,margin=0,total=9223372036854775807)
    other=copy.deepcopy(row);other['round']=2;owner['apiRounds'].append(other)
    owner['_executionBudget']=SimpleNamespace(consumption_snapshot=lambda:(20400,0,True))
    with pytest.raises(ValueError,match='storage integer range'):
        native_task_cost(owner,model='alpha')
