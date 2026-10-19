"""Accepted actor bindings consume original occurrence proofs without replay."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
import threading
from types import SimpleNamespace

import pytest

from lib.agent_core.native_model_control import ModelControl
from lib.agent_core.native_process import CheckpointRequest
from lib.conversation_sync.turn_images import turn_image_owner_scope
from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.agent_core.test_native_goal_handoff import prepared as prepared, principal
from tests.agent_core.test_recovered_actor_tools import pending
from tests.agent_core.test_shared_account_checkpoint import PROFILE

pytestmark = pytest.mark.unit


def recovered(prepared, monkeypatch):
    budget, task, account, cuts = pending(prepared)
    _, storage, _, policies, _ = prepared
    task['abort_event']=threading.Event(); executor=policies[0][0].executor
    producer='source-a'; operation='a'*32; call='original-file-call'
    reference=dict(conversationId='conversation',invocationId=producer,operationId=operation,
        ownerScope=turn_image_owner_scope(7,'conversation'))
    state=dict(task_id=task['id'],tools_pending=True,next_round=1,shared_model_account=account,
        messages=[dict(role='assistant',tool_calls=[dict(id=call,type='function',function=dict(name='write_file',arguments='{}'))])],
        tool_recovery=dict(format='tofu.native-file-tool-recovery/v1',producer_task_id=producer,
            calls=[dict(call_id=call,inspection=reference)]))
    birth=dict(user_id=7,conversation_id='conversation',parent_task_id='source',child_task_id=producer,archive_scope='invocation:7:'+producer)
    storage.records[producer]=dict(version=4,value=dict(child_invocation=birth,task_id=producer,user_id=7,conv_id='conversation',
        tool_rounds=[dict(taskId=producer,toolCallId=call,roundNum=1,executionEffect=dict(operationId=operation))]))
    storage.reads=[];query=storage.query
    def observed_query(operation,payload):
        storage.reads.append((operation,payload.get('key')))
        return query(operation,payload)
    storage.query=observed_query
    gateway=SimpleNamespace(task=task,executor=executor,owner=7,_closed=False,permissions={'write_file':'write'},_progress=None)
    recovery=NativeFileToolRecovery(principal=principal(),task=task,executor=executor,registered_child=True,storage=storage)
    recovery.bind_gateway(gateway)
    inspected=[]
    def inspect(rows, **scope):
        inspected.append(deepcopy(scope))
        return 'file_published','undo_committed'
    monkeypatch.setattr('lib.conversation_sync.tool_effects.inspect_effect_rows',inspect)
    request=CheckpointRequest(principal(),task['id'],'load',0,'epoch','binding',threading.Event())
    return recovery,request,state,budget,storage,cuts,inspected


@pytest.mark.parametrize('tool_round', [None, 1])
def test_model_restore_retains_pending_snapshot_and_reconciliation_reuses_original_charge(prepared,monkeypatch,tool_round):
    recovery,request,state,budget,storage,cuts,inspected=recovered(prepared,monkeypatch)
    if tool_round is not None:
        state['next_round']=2
        state['tool_recovery']['calls'][0]['tool_round']=tool_round
    model=NativeModelAccountPolicy(principal=principal(),task=recovery.task,executor=recovery.executor)
    control=ModelControl(None,model,recovery.task_id);control.counter_policy=PROFILE
    before=budget.consumption(); length=len(cuts); source=deepcopy(storage.records['source-a'])
    async def run():
        await control.restore_checkpoint(request,state)
        assert request.state is None
        return await recovery.reconcile(replace(request,state=state,action='reconcile'))
    observations=asyncio.run(run())
    assert observations[0]['call_id']=='original-file-call'
    assert json.loads(observations[0]['content'])['invocationId']=='source-a'
    assert inspected[0]['expected_task_id']=='source-a'
    assert storage.reads.count(('record.get','source-a'))==2
    assert storage.records['source-a']==source and budget.consumption()==before and len(cuts)==length
    assert recovery.consumption_proved
    with pytest.raises(ValueError,match='one-shot'):
        asyncio.run(recovery.reconcile(replace(request,state=state)))


@pytest.mark.parametrize('damage', [None, 'receipt', 'parent', 'version'])
def test_unentered_batch_requires_uncharged_prefix_and_unchanged_absent_intent(prepared,monkeypatch,damage):
    recovery,request,state,budget,storage,_,inspected=recovered(prepared,monkeypatch)
    state['shared_model_account']['tool_calls']+=1
    state['shared_model_account']['control_tool_calls']+=1
    if damage != 'receipt':
        storage.records['source-a']['value']['tool_rounds']=[]
    if damage=='parent':
        storage.records['source-a']['value']['child_invocation']['parent_task_id']='foreign'
    if damage=='version':
        query=storage.query
        reads=[]
        def changing(operation,payload):
            if payload.get('key')=='source-a':
                reads.append(True)
                if len(reads)==2:storage.records['source-a']['version']+=1
            return query(operation,payload)
        storage.query=changing
    before=budget.consumption()
    if damage:
        with pytest.raises(ValueError):
            asyncio.run(recovery.reconcile(replace(request,state=state,action='reconcile')))
        assert budget.consumption()==before and not recovery.consumption_proved
        assert budget.continuity_snapshot()['unknown_usage']
    else:
        observations=asyncio.run(recovery.reconcile(replace(request,state=state,action='reconcile')))
        result=json.loads(observations[0]['content'])
        assert result['outcome']=='file_not_published' and result['reason']=='checkpoint_batch_unentered'
        assert budget.consumption()==(before[0],before[1]+1)
    assert not inspected


@pytest.mark.parametrize('damage',['unknown','registration_race','foreign_parent','producer','round','tool_round','tool_round_bool','missing','counter','authority','phase'])
def test_unverified_child_effect_never_returns_a_replayable_observation(prepared,monkeypatch,damage):
    recovery,request,state,budget,storage,_,_=recovered(prepared,monkeypatch)
    if damage=='unknown':monkeypatch.setattr('lib.conversation_sync.tool_effects.inspect_effect_rows',lambda *a,**k:('unknown','missing_evidence'))
    elif damage=='registration_race':
        def change(*args,**kwargs):
            storage.records['source-a']['version']+=1
            return 'file_published','undo_committed'
        monkeypatch.setattr('lib.conversation_sync.tool_effects.inspect_effect_rows',change)
    elif damage=='foreign_parent':storage.records['source-a']['value']['child_invocation']['parent_task_id']='foreign'
    elif damage=='producer':state['tool_recovery']['producer_task_id']='source-b'
    elif damage=='round':state['next_round']=2
    elif damage=='tool_round':state['tool_recovery']['calls'][0]['tool_round']=2
    elif damage=='tool_round_bool':state['tool_recovery']['calls'][0]['tool_round']=True
    elif damage=='missing':storage.records['source-a']['value']['tool_rounds']=[]
    elif damage=='counter':state['shared_model_account']['tokens']+=1
    elif damage=='phase':state['tools_pending']=False
    else:recovery.storage=None
    before=budget.consumption()
    with pytest.raises(Exception):asyncio.run(recovery.reconcile(replace(request,state=state)))
    assert budget.consumption()==before and not recovery.consumption_proved
    assert budget.continuity_snapshot()['unknown_usage']
    assert budget.reason()=='execution_account_frontier_unavailable'


def test_reconciliation_cancellation_joins_entered_domain_inspection(prepared,monkeypatch):
    recovery,request,state,budget,_,_,_=recovered(prepared,monkeypatch)
    entered=threading.Event();release=threading.Event()
    def inspect(*args,**kwargs):
        entered.set();assert release.wait(5)
        return 'file_published','undo_committed'
    monkeypatch.setattr('lib.conversation_sync.tool_effects.inspect_effect_rows',inspect)
    async def run():
        work=asyncio.create_task(recovery.reconcile(replace(request,state=state)))
        assert await asyncio.to_thread(entered.wait,5)
        work.cancel();await asyncio.sleep(0)
        assert not work.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):await work
    try:asyncio.run(run())
    finally:release.set()
    assert recovery.consumption_proved and budget.consumption()==(36,5)


def test_file_recovery_cannot_replace_its_checkpoint_authority(prepared,monkeypatch):
    recovery,*_=recovered(prepared,monkeypatch)
    with pytest.raises(ValueError,match='authority'):recovery.bind_checkpoint_storage(object())

@pytest.mark.parametrize('damage', [None, 'unbound', 'parent', 'producer'])
def test_original_effect_producer_is_separate_from_current_account_source(prepared, monkeypatch, damage):
    recovery, request, state, budget, storage, cuts, inspected = recovered(prepared, monkeypatch)
    original = storage.records.pop('source-a')
    original['value']['task_id'] = 'original'
    original['value']['child_invocation'].update(child_task_id='original', parent_task_id='original-root', archive_scope='invocation:7:original')
    original['value']['tool_rounds'][0]['taskId'] = 'original'
    storage.records['original'] = original
    state['tool_recovery']['producer_task_id'] = 'original'
    state['tool_recovery']['calls'][0]['inspection']['invocationId'] = 'original'
    origin = dict(task_id='original', parent_task_id='original-root')
    if damage == 'parent': origin['parent_task_id'] = 'foreign'
    elif damage == 'producer': origin['task_id'] = 'foreign'
    if damage != 'unbound': recovery.bind_effect_origin(origin)
    before = budget.consumption(); count = len(cuts)
    if damage:
        with pytest.raises(ValueError): asyncio.run(recovery.reconcile(replace(request, state=state)))
        assert not recovery.consumption_proved
    else:
        model = NativeModelAccountPolicy(principal=principal(), task=recovery.task, executor=recovery.executor)
        control = ModelControl(None, model, recovery.task_id); control.counter_policy = PROFILE
        async def run():
            await control.restore_checkpoint(request, state)
            return await recovery.reconcile(replace(request, state=state))
        result = asyncio.run(run())
        assert json.loads(result[0]['content'])['invocationId'] == 'original'
        assert inspected[0]['expected_task_id'] == 'original'
    assert budget.consumption() == before and len(cuts) == count
