"""Owned physical-start publication, rollback, recovery and presentation reads."""
from copy import deepcopy
import time

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.management import AutomationManagement
from lib.automations.management_contract import validate
from lib.automations.presentation import show_task_start_time
from lib.automations.transitions import ExecutionFence, bind_fence, prepare_step, record_output
from lib.storage.errors import StorageError
from tests.automations.test_atomic_publication import setup, get_job
from tests.automations.test_task_started_events import pending, start, context, event_id

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def prepared(authority, monkeypatch, *, two_steps=False, tenant='test-tenant'):
    factory, repo, rule = setup(authority)
    if tenant != repo.principal.tenant_id:
        from lib.identity import PrincipalContext
        from lib.automations.execution_repository import AutomationExecutionRepository
        principal = PrincipalContext.from_payload({**repo.principal.to_payload(),'tenant_id':tenant})
        repo = AutomationExecutionRepository(principal,client_factory=factory)
    if two_steps:
        rule['steps'].append({'id':'show2','action':'task.show_time','after':['show'],'arguments':{}})
    definition = repo.publish(prepare_definition(rule),expected_version=0,now=time.time())
    _, owner, created, attempt, task = pending(authority,monkeypatch)
    ctx=context(tenant=repo.principal.tenant_id or '')
    start(factory,owner,attempt,task,ctx)
    job=get_job(factory,repo,event_id(owner,ctx,created,attempt,task))
    event=job['payload']['event']
    producer=factory(write=True).command('worker_job.claim_next',{'worker_id':'event-producer',
        'task_kinds':['automation-event'],'now_ms':int(time.time()*1000),'lease_ms':60000},None)
    producer_fence=ExecutionFence(producer['taskId'],producer['claimOwner'],producer['fencingToken'])
    first=repo.admit(definition.id,event,now=time.time(),producer_fence=producer_fence)
    claim=factory(write=True).command('worker_job.claim_next',{'worker_id':'time-publisher',
        'task_kinds':['automation-run'],'now_ms':int(time.time()*1000),'lease_ms':60000},None)
    fence=ExecutionFence(claim['taskId'],claim['claimOwner'],claim['fencingToken'])
    bound=repo.advance(first,bind_fence(first.value,fence,now=time.time()),fence,now=time.time())
    intent,step=prepare_step(bound.value,fence,now=time.time())
    dispatch=repo.advance(bound,intent,fence,now=time.time())
    output=show_task_start_time(intent['steps'][step]['arguments'],event)
    completed=record_output(dispatch.value,step,output,fence,now=time.time())
    return factory,repo,definition,dispatch,completed,fence,created,attempt


def query(factory,repo,created,**kwargs):
    service=AutomationManagement(repo.principal,client_factory=factory)
    page=service.presentations(created['turn']['conversationId'],created['attempt']['attemptId'],**kwargs)
    validate('PresentationPage',page)
    return page


def test_output_pointer_commit_replay_and_cold_restart(semantic_authority,monkeypatch):
    factory,repo,definition,dispatch,completed,fence,created,attempt=prepared(semantic_authority,monkeypatch)
    assert query(factory,repo,created)['items']==[]
    saved=repo.advance(dispatch,completed,fence,now=time.time())
    page=query(factory,repo,created)
    item=page['items'][0]
    assert item['attemptId']==attempt and item['turnId']==created['turn']['turnId']
    assert item['output']==saved.value['steps']['show']['output']
    assert item['output']['startedAt']*1000==factory().query('turn.attempt.get',{
        'user_id':repo.owner_id,'attempt_id':attempt})['startedAt']
    assert 'effectKey' not in item and 'ownerId' not in item
    assert repo.advance(dispatch,completed,fence,now=time.time()).version==saved.version
    repo.retire_definition(definition,now=time.time())
    assert query(factory,repo,created)==page
    if semantic_authority[2]:
        semantic_authority[2].crash_cycle()
        assert query(factory,repo,created)==page
    paged=query(factory,repo,created,limit=1)
    assert paged['items']==page['items'] and paged['nextCursor']==item['id']
    assert query(factory,repo,created,limit=1,after=item['id'])=={'items':[],'nextCursor':''}


@pytest.mark.parametrize('tamper',['time','timezone','effect','intent'])
def test_bad_publication_rolls_back_output_and_reference(semantic_authority,monkeypatch,tamper):
    factory,repo,_,dispatch,completed,fence,created,_=prepared(semantic_authority,monkeypatch)
    bad=deepcopy(completed)
    state=bad['steps']['show']
    if tamper=='time':state['output']['startedAt']+=1
    elif tamper=='timezone':state['output']['timezone']='UTC'
    elif tamper=='effect':state['effectKey']='forged'
    else:state['status']='succeeded';state.pop('arguments')
    with pytest.raises((StorageError,ValueError)):
        repo.advance(dispatch,bad,fence,now=time.time())
    assert repo.get('run',dispatch.id).value==dispatch.value
    assert query(factory,repo,created)['items']==[]


def test_index_is_not_generic_write_authority(semantic_authority,monkeypatch):
    factory,repo,_,dispatch,completed,fence,created,_=prepared(semantic_authority,monkeypatch)
    for operation,extra in [('record.put',{'value':{},'expected_version':0}),('record.delete',{})]:
        with pytest.raises(StorageError):
            factory(write=True).command(operation,{'namespace':'automation.presentations.v1',
                'key':repo._prefix+'forged','user_id':repo.owner_id,**extra},None)
    repo.advance(dispatch,completed,fence,now=time.time())
    assert len(query(factory,repo,created)['items'])==1
    foreign=type(repo.principal).user(subject_id='foreign-tenant',owner_user_id=repo.owner_id,
        tenant_id='foreign',scopes={'automations:read'})
    assert AutomationManagement(foreign,client_factory=factory).presentations(
        created['turn']['conversationId'],created['attempt']['attemptId'])['items']==[]


def test_late_invalid_step_rolls_back_first_staged_reference(semantic_authority,monkeypatch):
    factory,repo,_,dispatch,completed,fence,created,_=prepared(semantic_authority,monkeypatch,two_steps=True)
    completed['steps']['show2']['status']='succeeded'
    with pytest.raises(StorageError):repo.advance(dispatch,completed,fence,now=time.time())
    assert query(factory,repo,created)['items']==[]
    assert repo.get('run',dispatch.id).value==dispatch.value


def test_published_output_cannot_be_rewritten_by_worker(semantic_authority,monkeypatch):
    factory,repo,_,dispatch,completed,fence,created,_=prepared(semantic_authority,monkeypatch)
    saved=repo.advance(dispatch,completed,fence,now=time.time())
    before=query(factory,repo,created)
    changed=deepcopy(saved.value);changed['steps']['show']['output']['text']='rewritten'
    with pytest.raises(StorageError):repo.advance(saved,changed,fence,now=time.time())
    assert query(factory,repo,created)==before
