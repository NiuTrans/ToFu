"""The existing account and child policies consume one native whole-set ACK."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
from unittest.mock import AsyncMock

import pytest
from jsonschema import ValidationError

from lib.agent_core.native_process import CheckpointRequest
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from lib.tasks_pkg.native_goal_handoff import NativeGoalHandoff
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.agent_core.test_recovered_actor_prefix import root, child, source, spend
from tests.agent_core.test_native_task_terminal import principal
from tests.agent_core.test_shared_account_checkpoint import PROFILE

pytestmark=pytest.mark.unit

class Storage:
    def __init__(self):self.calls=[];self.records={};self.damage=None
    def native_checkpoint_available(self,**kwargs):return True
    def query(self,operation,payload):return deepcopy(self.records[payload['key']]) if operation=='record.get' else {'id':'conversation'}
    def native_checkpoint_control(self,operation,payload,**kwargs):
        self.calls.append((operation,deepcopy(payload)))
        if operation=='goal_handoff':
            if self.damage=='unknown':raise OSError('lost native response')
            rows=[dict(task_id=row['task_id'],capability_id=index+11) for index,row in enumerate(payload['goal_handoff']['actors'])]
            if self.damage=='partial':rows.pop()
            if self.damage=='duplicate':rows[1]['capability_id']=rows[0]['capability_id']
            if self.damage=='foreign':rows[1]['task_id']='foreign'
            return dict(accepted=True,state=dict(leases=rows))
        return dict(accepted=True,state=None)

@pytest.fixture
def prepared():
    task,account=root('target');account.restore_pending_frontier(task,source(),owner=7)
    storage=Storage();handoff=NativeGoalHandoff(principal=principal(),task=task,storage=storage,evidence_bytes=1048576,io_timeout_ms=5000)
    ordinary=[];account.bind_account_frontier_observer(ordinary.append,actor_rebind_observer=handoff.commit_frontiers,actor_rebind_bytes=1048576)
    with ThreadPoolExecutor(max_workers=2) as executor:
        policies=[]
        for suffix in ('a','b'):
            actor=child(task,'target-'+suffix)
            actor.update(_transientRuntime=True,_invocationStorageFree=False,convId='conversation',_archiveScope='invocation:7:'+actor['id'])
            birth=dict(user_id=7,conversation_id='conversation',parent_task_id='target',child_task_id=actor['id'],archive_scope=actor['_archiveScope'])
            storage.records[actor['id']]={'value':dict(child_invocation=birth,task_id=actor['id'],user_id=7,conv_id='conversation')}
            policy=NativeCheckpointPolicy(principal=principal(),task=actor,storage=storage,executor=executor,
                epoch='epoch',binding='binding',token='token-'+suffix,header_bytes=4096,snapshot_bytes=65536,
                identity_bytes=1024,io_timeout_ms=5000,restore=AsyncMock(),registered_child=True)
            policies.append((policy,'source-'+suffix))
        yield handoff,storage,account,policies,ordinary

def consume(handoff,policies):handoff.consume(policies,authorization={'claim_request':{'invoice':{'task_id':'source'}}})
def request(policy):return CheckpointRequest(principal(),policy.task_id,'load',0,'epoch','binding',threading.Event())

def test_whole_native_ack_precedes_policy_adoption_and_ordinary_load(prepared):
    handoff,storage,account,policies,ordinary=prepared;before=len(ordinary)
    consume(handoff,policies)
    assert account.consumption()==(36,5) and len(ordinary)==before
    assert [policy.capability for policy,_ in policies]==[11,12]
    assert [op for op,_ in storage.calls]==['goal_handoff']
    cuts=storage.calls[0][1]['goal_handoff']['authorization']['account_frontiers']
    assert cuts[-1]['recovery']['pending_actors']=={}
    async def run():
        for policy,_ in policies:
            assert await policy.load(request(policy)) is None
            await policy.close()
    asyncio.run(run())
    assert [op for op,_ in storage.calls]==['goal_handoff','load','forget','load','forget']
    with pytest.raises(ValueError):consume(handoff,policies)

@pytest.mark.parametrize('damage',['partial','duplicate','foreign','unknown'])
def test_incomplete_or_unknown_ack_never_adopts_any_recipient(prepared,damage):
    handoff,storage,account,policies,_=prepared;storage.damage=damage
    with pytest.raises((ValueError,OSError)):consume(handoff,policies)
    assert all(policy.capability is None for policy,_ in policies)
    assert account.reason()=='execution_account_frontier_unavailable' and account.consumption()==(36,5)

@pytest.mark.parametrize('damage',['foreign','used','missing','wrong_sink','mixed_bounds'])
def test_invalid_whole_policy_set_cannot_publish_or_move_credit(prepared,damage):
    handoff,storage,account,policies,_=prepared
    if damage=='foreign':policies[1][0].owner=8
    elif damage=='used':policies[1][0].creation_started=True
    elif damage=='missing':policies.pop()
    elif damage=='wrong_sink':account._actor_rebind_observer=lambda cuts:None
    elif damage=='mixed_bounds':policies[1][0].parameters['snapshot_bytes']=32
    with pytest.raises(ValueError):consume(handoff,policies)
    assert not storage.calls and account._rebound_actor_sources=={} and account.consumption()==(36,5)

def test_actor_policy_cannot_adopt_before_acknowledged_account_move(prepared):
    _,_,_,policies,_=prepared
    for policy,_ in policies:
        with pytest.raises(ValueError):policy.adopt_recovered_capability(11)
        assert policy.capability is None

def test_failed_policy_adoption_forgets_only_unadopted_capabilities(prepared,monkeypatch):
    handoff,storage,account,policies,_=prepared
    def refuse(capability):raise ValueError('recipient changed after native commit')
    monkeypatch.setattr(policies[1][0],'adopt_recovered_capability',refuse)
    with pytest.raises(ValueError,match='recipient changed'):consume(handoff,policies)
    assert policies[0][0].capability==11 and policies[1][0].capability is None
    assert storage.calls[-1]==('forget',{'format':'tofu.native-checkpoint-control/v1','capability_id':12})
    assert account.consumption()==(36,5)
    asyncio.run(policies[0][0].close())
    assert storage.calls[-1][1]['capability_id']==11
    handoff.close()

def test_failed_cleanup_retains_unadopted_handle_for_explicit_retry(prepared,monkeypatch):
    handoff,storage,account,policies,_=prepared
    def refuse(capability):raise ValueError('recipient changed after native commit')
    monkeypatch.setattr(policies[1][0],'adopt_recovered_capability',refuse)
    native=storage.native_checkpoint_control
    def unavailable(operation,payload,**kwargs):
        if operation=='forget':raise OSError('cleanup transport unavailable')
        return native(operation,payload,**kwargs)
    monkeypatch.setattr(storage,'native_checkpoint_control',unavailable)
    with pytest.raises(BaseExceptionGroup) as failure:consume(handoff,policies)
    assert isinstance(failure.value.exceptions[0],ValueError)
    assert isinstance(failure.value.exceptions[1],BaseExceptionGroup)
    assert handoff._capabilities==(12,) and policies[0][0].capability==11
    assert account.consumption()==(36,5)
    monkeypatch.setattr(storage,'native_checkpoint_control',native)
    handoff.close()
    assert storage.calls[-1][1]['capability_id']==12
    assert handoff._capabilities is None
    asyncio.run(policies[0][0].close())
    assert storage.calls[-1][1]['capability_id']==11

@pytest.mark.parametrize('damage',[None,'tokens','boolean','root','policy','new_spend'])
def test_actor_model_restore_only_verifies_existing_acknowledged_credit(prepared,damage):
    handoff,_,account,policies,_=prepared;consume(handoff,policies)
    policy=policies[0][0]
    state=account.capture_actor_account(owner=7,task=policy.task,counter_policy=PROFILE)
    if damage=='tokens':state['tokens']+=1
    elif damage=='boolean':state['complete']=1
    elif damage=='root':state['root_task_id']='foreign'
    elif damage=='policy':state['counter_policy']['wire_model']='foreign'
    elif damage=='new_spend':spend(policy.task,1)
    before=account.consumption()
    model=NativeModelAccountPolicy(principal=principal(),task=policy.task,executor=policy.executor)
    if damage:
        with pytest.raises((ValueError,ValidationError)):asyncio.run(model.restore_checkpoint(request(policy),state,PROFILE))
    else:asyncio.run(model.restore_checkpoint(request(policy),state,PROFILE))
    assert account.consumption()==before
