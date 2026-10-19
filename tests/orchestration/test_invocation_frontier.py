"""Source actor observations cannot substitute for completed or foreign leaves."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.orchestration.execution_journal import RecoveryRefused
from lib.orchestration.invocation_frontier import read_registered_actor_frontier
from tests.storage.test_native_checkpoint_frontier import actor_snapshot

pytestmark = pytest.mark.unit


def fixture():
    birth=dict(user_id=7,conversation_id='conversation',parent_task_id='flow-root',
        child_task_id='task',archive_scope='invocation:7:task')
    admission=dict(kind='agent',invocation_id='task',input_hash='a'*64,invocation_completion=dict(
        format='tofu.flow.invocation-completion/v1',nonce='a'*32,identity=birth,account_root_task_id='flow-root'))
    record=dict(version=1,value=dict(child_invocation=birth,task_id='task',user_id=7,conv_id='conversation'))
    frontier=dict(format='tofu.native-checkpoint-frontier/v1',owner_user_id=7,task_id='task',epoch='actor-epoch',
        binding='actor-policy',version=3,lease_generation=1,phase='tools_pending',snapshot=actor_snapshot())
    calls=[];response=dict(accepted=True,capability_id=None,state=frontier)
    def query(op,payload):return deepcopy(record) if op=='record.get' else {'id':'conversation'}
    def control(op,payload,**kwargs):calls.append((op,payload,kwargs));return response
    storage=SimpleNamespace(query=query,native_checkpoint_available=lambda **_:True,native_checkpoint_control=control)
    options=dict(owner_user_id=7,conversation_id='conversation',parent_task_id='flow-root',
        header_bytes=4096,snapshot_bytes=1048576,identity_bytes=1024,io_timeout_ms=1000)
    return storage,admission,record,response,calls,options


def test_observation_is_detached_and_reads_never_acquire_or_grant_successor_authority():
    storage,admission,record,response,calls,options=fixture()
    before=deepcopy(record)
    observed=read_registered_actor_frontier(storage,admission,**options)
    assert observed==dict(admission=admission,frontier=response['state'],registration_version=1)
    assert [row[0] for row in calls]==['frontier'] and record==before
    observed['frontier']['snapshot']['shared_model_account']['tokens']=0
    assert response['state']['snapshot']['shared_model_account']['tokens']==10200


@pytest.mark.parametrize('damage',['owner','parent','conversation','completion','root','actor','unknown','tree','scope','phase','capability','race','bound','missing','version'])
def test_preflight_refuses_incomplete_or_changed_source_evidence(damage):
    storage,admission,record,response,calls,options=fixture()
    account=response['state']['snapshot']['shared_model_account']
    if damage=='owner':options['owner_user_id']=8
    elif damage=='parent':options['parent_task_id']='other'
    elif damage=='conversation':options['conversation_id']='other'
    elif damage=='completion':record['value']['flow_invocation_completion']={}
    elif damage=='root':account['root_task_id']='other'
    elif damage=='actor':account['actor_task_id']='other'
    elif damage=='unknown':account['complete']=False
    elif damage=='tree':account['root_tokens']=0
    elif damage=='scope':response['state']['binding']='other'
    elif damage=='phase':response['state']['phase']='completed'
    elif damage=='capability':response['capability_id']=1
    elif damage=='bound':options['snapshot_bytes']=True
    elif damage=='missing':response['state']=None
    elif damage=='version':record['version']=True
    elif damage=='race':
        control=storage.native_checkpoint_control
        def changed(*args,**kwargs):
            result=control(*args,**kwargs);record['version']+=1;return result
        storage.native_checkpoint_control=changed
    with pytest.raises(RecoveryRefused):read_registered_actor_frontier(storage,admission,**options)
    assert all(row[0]=='frontier' for row in calls)


@pytest.mark.parametrize('damage', [None, 'missing', 'duplicate', 'counter', 'limits', 'request', 'nested'])
def test_aggregate_matches_live_actors_without_adding_closed_prefix_again(damage):
    from lib.agent_core.execution_budget import _ensure_execution_budget, inherit_execution_budget, close_execution_budget
    from lib.orchestration.invocation_frontier import preflight_registered_actor_accounts
    storage, admission, _, response, calls, options = fixture()
    root = dict(id='flow-root', _userId=7, status='running', config={})
    account = _ensure_execution_budget(root); cuts = []; account.bind_account_frontier_observer(cuts.append)
    actor = dict(id='task', _userId=7, status='running', config={})
    inherit_execution_budget(root, actor)
    lease = account.reserve_model(actor, input_tokens=10000, max_output_tokens=200)
    lease.mark_dispatched(); lease.settle(response['state']['snapshot']['usage'][0])
    completed = dict(id='completed', _userId=7, status='running', config={})
    inherit_execution_budget(root, completed)
    lease = account.reserve_model(completed, input_tokens=0, max_output_tokens=50)
    lease.mark_dispatched(); lease.settle({'prompt_tokens':0,'completion_tokens':50}); close_execution_budget(completed)
    cut = cuts[-1]; admissions = [admission]
    assert cut['closed_tokens'] == 50 and cut['snapshot']['tokens'] == 10250
    if damage == 'missing': admissions = []
    elif damage == 'duplicate': admissions.append(deepcopy(admission))
    elif damage == 'counter': response['state']['snapshot']['shared_model_account']['tokens'] -= 1
    elif damage == 'limits': response['state']['snapshot']['shared_model_account']['limits']['max_tokens'] = 20000
    elif damage == 'nested': cut['actors']['task']['parent_task_id'] = 'other'
    elif damage == 'request':
        lease = account.reserve_model(actor,input_tokens=0,max_output_tokens=1)
        lease.mark_dispatched(); cut = cuts[-1]
    if damage:
        with pytest.raises(RecoveryRefused): preflight_registered_actor_accounts(storage,cut,admissions,**options)
    else:
        result = preflight_registered_actor_accounts(storage,cut,admissions,**options)
        assert result['account_frontier']['snapshot']['tokens'] == 10250
        assert result['actors']['task']['frontier']['snapshot']['shared_model_account']['tokens'] == 10200
    assert all(row[0] == 'frontier' for row in calls)
