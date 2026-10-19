"""Accepted durable child quotes constrain fresh execution without journal authority."""
import copy
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from lib.agent_core.execution_budget import ExecutionBudget
from lib.billing.cost import transfer_native_child_fees,native_task_cost
from lib.billing.native_settlement import freeze,restore_accepted_invoice_prefix
from lib.conversation_sync.dispatch_contract import AcceptedResumeLineage
from tests.billing.test_native_child_fee_transfer import family
pytestmark=pytest.mark.unit


def invoice_family(cap=30000):
    source,child,account=family()
    transfer_native_child_fees(source,child);account.close(child)
    source.update(status='aborted',convId='owned-conversation',_billingUserId='opaque-wallet',
        _billing_reservation_micro=100)
    capsule=freeze(source)
    rows=copy.deepcopy(source['apiRounds']);usage=copy.deepcopy(source['usage'])
    target=dict(id='target',_userId=7,convId=source['convId'],status='running',
        config={'model':'alpha','executionLimits':{'max_tokens':cap,'max_tool_calls':None,'deadline':None}},
        usage=usage,_checkpointUsage=copy.deepcopy(usage),apiRounds=rows,
        _checkpointApiRounds=copy.deepcopy(rows),_billingUserId='opaque-wallet',
        _nativePricingRequired=True,_nativeFrozenCostProjection=True)
    target['_executionBudget']=ExecutionBudget(target)
    lineage=AcceptedResumeLineage(7,source['convId'],'turn','target-attempt',target['id'],
        'source-attempt',source['id'])
    record=dict(task_id=source['id'],user_id=7,conv_id=source['convId'],status='aborted',
        metadata=dict(nativePricingRequired=True,usageComplete=True,apiRounds=copy.deepcopy(rows),
            nativeBillingSettlement=copy.deepcopy(capsule)))
    return target,lineage,record


def invoke(target,lineage,record):
    read=SimpleNamespace(query=lambda operation,payload,**kwargs:copy.deepcopy(record))
    with patch('lib.storage.get_storage_client',return_value=read):
        restore_accepted_invoice_prefix(target,lineage=lineage,model='alpha')


def test_durable_child_prefix_restores_spend_once_without_recharging_or_extra_budget():
    target,lineage,record=invoice_family();before=copy.deepcopy(target['apiRounds'])
    invoke(target,lineage,record);invoke(target,lineage,record)
    account=target['_executionBudget']
    assert account.consumption_snapshot()==(20400,0,True)
    assert native_task_cost(target,model='alpha').micro==0
    assert target['apiRounds']==before and target['usage']['prompt_tokens']==10000
    lease=account.reserve_model(target,input_tokens=1000,max_output_tokens=20000)
    assert lease.ordinal==1 and lease.max_output_tokens==8600
    lease.settle(None,unsent=True)
    with pytest.raises(ValueError,match='pristine'):
        invoke(target,lineage,record)
    assert account.consumption_snapshot()==(20400,0,True)


@pytest.mark.parametrize('fault',['missing','owner','conversation','live','unknown','row','capsule','wallet','round_count','usage','component_swap'])
def test_changed_or_unowned_invoice_refuses_before_budget_mutation(fault):
    target,lineage,record=invoice_family();before=copy.deepcopy(target['apiRounds'])
    if fault=='missing':record=None
    elif fault=='owner':record['user_id']=8
    elif fault=='conversation':record['conv_id']='foreign'
    elif fault=='live':record['status']='running'
    elif fault=='unknown':record['metadata']['usageComplete']=False
    elif fault=='row':record['metadata']['apiRounds'][0]['provider_id']='changed'
    elif fault=='wallet':record['metadata']['nativeBillingSettlement']['wallet_user_id']='foreign'
    elif fault=='round_count':record['metadata']['nativeBillingSettlement']['roundCount']=1
    elif fault=='usage':target['_checkpointUsage']['completion_tokens']+=1
    elif fault=='component_swap':
        values=record['metadata']['nativeBillingSettlement']['componentsMicro']
        values['input']+=1;values['output']-=1
    else:
        values=record['metadata']['nativeBillingSettlement']['componentsMicro']
        for key in ('input','baseMicro','totalMicro'):values[key]+=1
    with pytest.raises(ValueError):invoke(target,lineage,record)
    assert target['_executionBudget'].consumption_snapshot()==(10200,0,True)
    assert target['apiRounds']==before


def test_next_continue_preserves_older_child_quotes_and_validates_only_source_fresh_capsule():
    target,lineage,record=invoice_family(cap=50000)
    invoke(target,lineage,record)
    account=target['_executionBudget']
    lease=account.reserve_model(target,input_tokens=10000,max_output_tokens=200)
    lease.mark_dispatched();lease.settle({'prompt_tokens':10000,'completion_tokens':200})
    target['usage']={'prompt_tokens':20000,'completion_tokens':400}
    fresh=copy.deepcopy(target['apiRounds'][0]);fresh.update(round=3,taskId=target['id'])
    target['apiRounds'].append(fresh)
    target['status']='aborted'
    capsule=freeze(target)
    assert capsule['roundCount']==1 and capsule['componentsMicro']['totalMicro']==7593
    successor=dict(id='successor',_userId=7,convId=target['convId'],status='running',config={'model':'alpha'},
        usage=copy.deepcopy(target['usage']),_checkpointUsage=copy.deepcopy(target['usage']),
        apiRounds=copy.deepcopy(target['apiRounds']),_checkpointApiRounds=copy.deepcopy(target['apiRounds']),
        _billingUserId='opaque-wallet',_nativePricingRequired=True,_nativeFrozenCostProjection=True)
    successor['_executionBudget']=ExecutionBudget(successor)
    receipt=AcceptedResumeLineage(7,target['convId'],'turn','successor-attempt',successor['id'],
        'target-attempt',target['id'])
    stored=dict(task_id=target['id'],user_id=7,conv_id=target['convId'],status='aborted',
        metadata=dict(nativePricingRequired=True,usageComplete=True,apiRounds=copy.deepcopy(target['apiRounds']),
            nativeBillingSettlement=capsule))
    invoke(successor,receipt,stored)
    assert successor['_executionBudget'].consumption_snapshot()==(30600,0,True)
    assert native_task_cost(successor,model='alpha').micro==0
    assert successor['apiRounds'][1]['billableTaskId']==lineage.source_task_id


def test_legacy_root_origin_normalization_does_not_mutate_durable_quotes():
    target,lineage,record=invoice_family()
    record['metadata']['apiRounds'][0].pop('taskId')
    original=copy.deepcopy(record)
    read=SimpleNamespace(query=lambda *args,**kwargs:record)
    with patch('lib.storage.get_storage_client',return_value=read):
        restore_accepted_invoice_prefix(target,lineage=lineage,model='alpha')
    assert record==original
    assert target['_executionBudget'].consumption_snapshot()==(20400,0,True)
