"""Actual worker-entry publication and subscribed event transport share one commit."""
from copy import deepcopy
import uuid

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.contract import storage_schema
from lib.automations.events import task_start_context, task_started_job
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from tests.automations.test_atomic_publication import setup, get_job

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def pending(authority, monkeypatch):
    factory, owner, _ = authority
    import lib.turn_lifecycle as turns
    monkeypatch.setattr(turns, '_turn_client', lambda **kwargs: factory(**kwargs))
    created = turns.create_turn_pair('task-start-'+uuid.uuid4().hex,
        command_id='create-'+uuid.uuid4().hex, input_projection={'content':'actual start'},
        config={'model':'synthetic'}, user_id=owner,
        conversation_defaults={'allowCreate':True,'title':'Start event','settings':{}})
    attempt_id = created['attempt']['attemptId']
    task_id = 'worker-'+uuid.uuid4().hex
    assert turns.claim_attempt_start(attempt_id,user_id=owner)
    turns.bind_task(attempt_id,task_id,user_id=owner)
    return factory,owner,created,attempt_id,task_id


def context(origin='human', tenant='test-tenant', project=None):
    value={'tenantId':tenant,'origin':origin,'automationChain':[]}
    if project is not None: value['projectId']=project
    return value


def event_id(owner,ctx,created,attempt_id,task_id):
    return task_started_job(owner,ctx,attempt_id=attempt_id,task_id=task_id,
        turn_id=created['turn']['turnId'],conversation_id=created['turn']['conversationId'],started_at_ms=1000)[0]


def start(factory,owner,attempt_id,task_id,ctx):
    return factory(write=True).command('turn.attempt.start',{'user_id':owner,'attempt_id':attempt_id,
        'task_id':task_id,'automation_context':ctx},None)


def test_start_transport_uses_first_actual_timestamp_and_survives_observational_replay(semantic_authority,monkeypatch):
    factory,repo,rule=setup(semantic_authority)
    repo.publish(prepare_definition(rule),expected_version=0,now=100)
    _,owner,created,attempt,task=pending(semantic_authority,monkeypatch)
    ctx=context();job_id=event_id(owner,ctx,created,attempt,task)
    assert get_job(factory,repo,job_id) is None
    result=start(factory,owner,attempt,task,ctx)
    assert result['status']=='running' and result['startedAt']>0
    job=get_job(factory,repo,job_id);payload=job['payload']
    assert job['status']=='queued' and job['priority']==storage_schema()['x-event']['priority'] and job['availableAtMs']==result['startedAt']
    assert payload['event']=={'id':payload['event']['id'],'kind':'task.started',
        'occurredAt':result['startedAt']/1000,'origin':'human','automationChain':[],
        'evidence':{'startedAt':result['startedAt']/1000,'taskId':task,'attemptId':attempt,
                    'turnId':created['turn']['turnId'],'conversationId':created['turn']['conversationId']}}
    assert payload['principal']==PrincipalContext.system(subject_id=job_id,owner_user_id=owner,
        tenant_id='test-tenant',scopes={'automations:read','automations:execute'}).to_payload()
    assert start(factory,owner,attempt,task,context(origin='automation'))['startedAt']==result['startedAt']
    assert get_job(factory,repo,job_id)==job


@pytest.mark.parametrize('scope,origin,rule_project,event_project,expected',[
    ('all','human',None,None,True),('human','human',None,None,True),
    ('human','automation',None,None,False),('automation','automation',None,None,True),
    ('automation','human',None,None,False),('all','human','project-a','project-a',True),
    ('all','human','project-a','project-b',False),('all','human','project-a',None,False),
])
def test_probe_filters_origin_and_project_before_enqueuing(semantic_authority,monkeypatch,
        scope,origin,rule_project,event_project,expected,tmp_path):
    factory,repo,rule=setup(semantic_authority);rule['trigger']['scope']=scope
    if rule_project:
        from lib.automations.projects import AutomationProjects,project_id
        root=tmp_path/rule_project;root.mkdir()
        registered=AutomationProjects(repo.principal,client_factory=factory).update(
            {'root':str(root),'label':rule_project,'active':True,'expectedRevision':0,'requestId':'register'})
        rule['projectId']=registered['id']
        if event_project: event_project=project_id(str((tmp_path/event_project).resolve()))
    repo.publish(prepare_definition(rule),expected_version=0,now=100)
    _,owner,created,attempt,task=pending(semantic_authority,monkeypatch)
    ctx=context(origin,project=event_project)
    assert start(factory,owner,attempt,task,ctx)['status']=='running'
    assert (get_job(factory,repo,event_id(owner,ctx,created,attempt,task)) is not None)==expected


@pytest.mark.parametrize('mode',['none','paused','foreign-tenant','foreign-owner'])
def test_unsubscribed_start_adds_no_transport(semantic_authority,monkeypatch,mode):
    factory,repo,rule=setup(semantic_authority)
    if mode!='none':
        definition=repo.publish(prepare_definition(rule),expected_version=0,now=100)
        if mode=='paused':repo.pause_definition(definition,now=101)
    _,owner,created,attempt,task=pending(semantic_authority,monkeypatch)
    if mode=='foreign-owner':
        # The start belongs to a second owned conversation, not the rule owner.
        second=(factory,owner+1,None)
        if semantic_authority[2] is not None:
            # The native fixture's credential is owner-scoped: it must deny
            # creating the foreign conversation before any producer mutation.
            with pytest.raises(StorageError,match='authority_scope_mismatch'):
                pending(second,monkeypatch)
            assert factory().query('turn.attempt.get',{'user_id':owner,'attempt_id':attempt})['status']=='pending'
            assert get_job(factory,repo,event_id(owner,context(),created,attempt,task)) is None
            return
        _,owner,created,attempt,task=pending(second,monkeypatch)
    ctx=context(tenant='other-tenant' if mode=='foreign-tenant' else 'test-tenant')
    assert start(factory,owner,attempt,task,ctx)['status']=='running'
    assert factory().query('worker_job.get',{'user_id':owner,'task_id':event_id(owner,ctx,created,attempt,task)}) is None


@pytest.mark.parametrize('invalid',[{'origin':'unknown'},{'automationChain':['x']*16},
    {'automationChain':['x'*161]},{'projectId':None},{'extra':'caller-controlled'}])
def test_invalid_context_cannot_publish_partial_running_state(semantic_authority,monkeypatch,invalid):
    factory,owner,created,attempt,task=pending(semantic_authority,monkeypatch)
    bad=context();bad.update(invalid)
    with pytest.raises(StorageError):start(factory,owner,attempt,task,bad)
    assert factory().query('turn.attempt.get',{'user_id':owner,'attempt_id':attempt})['status']=='pending'
    assert factory().query('turn.get',{'user_id':owner,'conversation_id':created['turn']['conversationId'],
        'turn_id':created['turn']['turnId']})['status']=='pending'


def test_transport_identity_collision_rolls_back_attempt_and_status_event(semantic_authority,monkeypatch):
    factory,repo,rule=setup(semantic_authority)
    repo.publish(prepare_definition(rule),expected_version=0,now=100)
    _,owner,created,attempt,task=pending(semantic_authority,monkeypatch)
    ctx=context();job_id=event_id(owner,ctx,created,attempt,task)
    factory(write=True).command('worker_job.enqueue',{'user_id':owner,'task_id':job_id,
        'task_kind':'foreign-probe','tenant_id':'test-tenant','payload':{},'idempotency_key':job_id,'now_ms':100000},'collision-'+job_id)
    before=factory().query('turn.events.list',{'user_id':owner,'attempt_id':attempt,'limit':100})
    with pytest.raises(StorageError):start(factory,owner,attempt,task,ctx)
    attempt_doc=factory().query('turn.attempt.get',{'user_id':owner,'attempt_id':attempt})
    assert attempt_doc['status']=='pending' and attempt_doc['startedAt'] is None
    assert factory().query('turn.events.list',{'user_id':owner,'attempt_id':attempt,'limit':100})==before
    assert get_job(factory,repo,job_id)['taskKind']=='foreign-probe'


def test_task_creation_context_is_owned_frozen_and_config_independent():
    principal=PrincipalContext.user(subject_id='source',owner_user_id=41,tenant_id='owned-tenant')
    chain=['parent-rule'];message={'role':'user','_initiator':'timer'}
    captured=task_start_context(principal,message,automation_chain=chain,project_id='owned-project')
    chain.append('later');message['_initiator']='human'
    assert captured=={'tenantId':'owned-tenant','origin':'automation','automationChain':['parent-rule'],
        'projectId':'owned-project'}
    assert task_start_context(principal,{'role':'user'})['origin']=='human'


from tests.tasks_pkg.test_task_owner import isolated_registry as isolated_registry


def test_chat_carrier_captures_server_context_and_rejects_config_provenance(isolated_registry,monkeypatch):
    def resolve(principal,config,*,project=None):
        assert principal.owner_user_id==41 and principal.tenant_id=='owned'
        return project
    monkeypatch.setattr('lib.automations.projects.resolve_task_project',resolve)
    registry=isolated_registry
    principal=PrincipalContext.user(subject_id='task-owner',owner_user_id=41,tenant_id='owned')
    messages=[{'role':'user','content':'timed start','_initiator':'timer'}]
    task=registry.create_task('carrier-event',messages,
        {'_attemptId':'attempt-carrier','automationChain':['forged'],
         '_automationStartContext':{'tenantId':'forged','origin':'human','automationChain':[]}},
        principal=principal,supersede=False,automation_chain=('rule-parent',),automation_project_id='owned-project')
    messages[-1]['_initiator']='human';task['config']['_automationStartContext']['tenantId']='changed'
    assert task['_automationStartContext']=={'tenantId':'owned','origin':'automation',
        'automationChain':['rule-parent'],'projectId':'owned-project'}
    prepared=registry.create_task('prepared-carrier',[{'role':'user','content':'prepared'}],
        {},principal=principal,supersede=False)
    prepared['config']['_attemptId']='bound-later'
    assert prepared['_automationStartContext']=={'tenantId':'owned','origin':'human','automationChain':[]}
    unknown=registry.create_task('unknown-origin',[{'role':'user','_initiator':'unknown'}],
        {'_attemptId':'attempt-unknown'},principal=principal,supersede=False)
    assert unknown['_automationStartContext'] is None
    transient=registry.create_task('transient-event',[],{'_attemptId':'attempt-transient'},
        principal=principal,transient=True,supersede=False)
    assert transient['_automationStartContext'] is None


def test_physical_entry_reuses_durable_start_in_task_and_event(semantic_authority,monkeypatch):
    from lib.tasks_pkg.spawn import _mark_worker_started_locked
    factory,repo,rule=setup(semantic_authority)
    repo.publish(prepare_definition(rule),expected_version=0,now=100)
    _,owner,created,attempt,task_id=pending(semantic_authority,monkeypatch)
    ctx=context()
    task={'id':task_id,'convId':created['turn']['conversationId'],'_attemptId':attempt,
        '_userId':owner,'_principalContext':PrincipalContext.user(subject_id='source',
            owner_user_id=owner,tenant_id='test-tenant').to_payload(),
        '_automationStartContext':ctx,'status':'pending','config':{},'created_at':1}
    monkeypatch.setattr('lib.agent_core.events.emit_phase',lambda *args,**kwargs:None)
    assert _mark_worker_started_locked(task,None)
    stored=factory().query('turn.attempt.get',{'user_id':owner,'attempt_id':attempt})
    payload=get_job(factory,repo,event_id(owner,ctx,created,attempt,task_id))['payload']
    assert task['_workerStartedAt']==stored['startedAt']/1000==payload['event']['evidence']['startedAt']
    assert task['_workerStartedAt']>task['created_at'] and task['status']=='running'



def test_start_clock_is_distinct_from_fresh_reaper_observation(monkeypatch):
    from lib.tasks_pkg.spawn import _mark_worker_started_locked
    task={'id':'start-replay-task','_attemptId':'replayed-start','_userId':41,
        'status':'pending','config':{}}
    monkeypatch.setattr('lib.turn_lifecycle.mark_task_started',
        lambda *args,**kwargs:{'status':'running','startedAt':1000})
    monkeypatch.setattr('lib.tasks_pkg.spawn.time.time',lambda:100.0)
    monkeypatch.setattr('lib.agent_core.events.emit_phase',lambda *args,**kwargs:None)
    assert _mark_worker_started_locked(task,None)
    assert task['_workerStartedAt']==1
    assert task['_t_last_event']==task['_dispatch_heartbeat']==100


def test_generic_producers_cannot_forge_start_event_transport(semantic_authority):
    factory,owner,_=semantic_authority
    with pytest.raises(StorageError):
        factory(write=True).command('worker_job.enqueue',{'user_id':owner,'task_id':'forged-start-event',
            'task_kind':'automation-event','tenant_id':'test-tenant','payload':{},
            'idempotency_key':'forged-start-event','now_ms':100000},'forge-start-event')
    assert factory().query('worker_job.get',{'user_id':owner,'task_id':'forged-start-event'}) is None


def test_unicode_owned_identity_has_the_same_natural_transport_across_authorities(semantic_authority,monkeypatch,tmp_path):
    from lib.automations.execution_repository import AutomationExecutionRepository
    from tests.support.automation_fixtures import clock_definition
    factory,owner,_=semantic_authority
    tenant='テナント🔐';project='项目-東京'
    principal=PrincipalContext.user(subject_id='publisher',owner_user_id=owner,tenant_id=tenant,
        scopes={'automations:read','automations:manage','automations:execute'})
    repo=AutomationExecutionRepository(principal,client_factory=factory)
    from lib.automations.projects import AutomationProjects
    root=tmp_path/project;root.mkdir()
    project=AutomationProjects(principal,client_factory=factory).update(
        {'root':str(root),'label':project,'active':True,'expectedRevision':0,'requestId':'register'})['id']
    rule=clock_definition();rule['id']+='-'+uuid.uuid4().hex;rule['projectId']=project
    repo.publish(prepare_definition(rule),expected_version=0,now=100)
    _,_,created,attempt,task=pending(semantic_authority,monkeypatch)
    ctx=context(tenant=tenant,project=project)
    result=start(factory,owner,attempt,task,ctx)
    job_id,document=task_started_job(owner,ctx,attempt_id=attempt,task_id=task,
        conversation_id=created['turn']['conversationId'],turn_id=created['turn']['turnId'],
        started_at_ms=result['startedAt'])
    assert get_job(factory,repo,job_id)['payload']==document
