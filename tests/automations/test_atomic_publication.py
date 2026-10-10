"""Publication binds scoped definitions, subscriptions and clock jobs atomically.

These real-backend contracts exercise durable admission and producer authority;
no production clock loop, model invocation or external skill download runs here.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import uuid

from tests.support.worker_job_time import wait_until_ms

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.automations.errors import AutomationConflict, AutomationError
from lib.automations.publication import publication_record
from lib.automations.transitions import ExecutionFence
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from tests.support.automation_fixtures import clock_definition, trigger

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def setup(authority, *, kind='event'):
    factory, owner, _ = authority
    principal = PrincipalContext.user(subject_id='publisher',owner_user_id=owner,
        tenant_id='test-tenant',scopes={'automations:read','automations:manage','automations:execute'})
    repo = AutomationExecutionRepository(principal,client_factory=factory)
    rule = clock_definition()
    rule['id'] += '-' + uuid.uuid4().hex
    if kind == 'schedule':
        rule['trigger'] = {'kind':'schedule','schedule':'* * * * *','timezone':'UTC','catchUp':'latest'}
    elif kind == 'manual':
        rule['trigger'] = {'kind':'manual'}
    return factory,repo,rule


def get_job(factory,repo,job_id):
    return factory().query('worker_job.get',{'task_id':job_id,'user_id':repo.owner_id})


def subscriptions(factory,repo):
    return factory().query('record.list',{'namespace':'automation.subscriptions.v1',
        'prefix':repo._prefix,'user_id':repo.owner_id,'limit':64})


def claim_clock(factory, *, now=120000, worker='clock-worker'):
    job = factory(write=True).command('worker_job.claim_next',{'worker_id':worker,
        'task_kinds':['automation-clock'],'now_ms':now,'lease_ms':10000},None)
    return job,ExecutionFence(job['taskId'],job['claimOwner'],job['fencingToken'])


def publication_payload(repo,prepared, *, expected=0, request='publish', due=None):
    return {'user_id':repo.owner_id,'tenant_id':repo.principal.tenant_id,
        'definition_key':repo._key(prepared.definition['id']),'expected_version':expected,
        'request_id':request,'actor_subject_id':repo.principal.subject_id,'mode':'publish',
        'record':publication_record(prepared,repo.principal),'definition_json':prepared.definition_json,
        'actions_json':prepared.action_contracts_json,'first_due_ms':due,'now_ms':100000}


def test_event_publication_replay_replaces_index_and_pause_needs_only_manage(semantic_authority):
    factory,repo,rule = setup(semantic_authority)
    prepared = prepare_definition(rule)
    first = repo.publish(prepared,expected_version=0,request_id='first',now=100)
    replay = repo.publish(prepared,expected_version=0,request_id='first',now=105)
    assert replay.value == first.value and replay.version == 1
    rows = subscriptions(factory,repo)
    assert len(rows) == 1 and rows[0]['value']['definitionRevision'] == 1
    assert rows[0]['key'] == first.value['triggerState']['subscriptionKey']
    changed = deepcopy(rule); changed['trigger']['scope'] = 'human'
    with pytest.raises(AutomationConflict):
        repo.publish(prepare_definition(changed),expected_version=0,request_id='first',now=106)
    replacement = repo.publish(prepare_definition(changed),expected_version=1,now=106)
    assert len(subscriptions(factory,repo)) == 1
    assert subscriptions(factory,repo)[0]['key'] != rows[0]['key']
    manager = AutomationExecutionRepository(PrincipalContext.user(subject_id='manager',
        owner_user_id=repo.owner_id,tenant_id=repo.principal.tenant_id,scopes={'automations:manage'}),client_factory=factory)
    paused = manager.pause_definition(replacement,request_id='pause',now=107)
    assert paused.value['definition']['enabled'] is False and subscriptions(factory,repo) == []
    assert paused.value['compiled']['digest'] != replacement.value['compiled']['digest']
    assert manager.pause_definition(replacement,request_id='pause',now=108).value == paused.value
    retired = manager.retire_definition(paused,now=109)
    assert retired.value['deleted'] is True
    assert manager.pause_definition(retired,now=110).value['deleted'] is True


def test_schedule_clock_revision_cancellation_and_first_publication_replay(semantic_authority):
    factory,repo,rule = setup(semantic_authority,kind='schedule')
    first = repo.publish(prepare_definition(rule),expected_version=0,request_id='first',now=100)
    clock = first.value['triggerState']['clockJobId']
    job = get_job(factory,repo,clock)
    assert job['availableAtMs'] == 120000 and job['priority'] == 200
    assert job['payload']['principal'] == first.value['grant']['principal']
    assert repo.publish(prepare_definition(rule),expected_version=0,request_id='first',now=200).value == first.value
    assert get_job(factory,repo,clock) == job
    replacement = repo.publish(prepare_definition(rule),expected_version=1,now=201)
    assert replacement.value['triggerState']['clockJobId'] != clock
    assert get_job(factory,repo,clock) is None
    assert get_job(factory,repo,replacement.value['triggerState']['clockJobId'])['availableAtMs'] == 240000
    paused = repo.pause_definition(replacement,now=202)
    assert paused.value['triggerState'] == {'subscriptionKey':None,'clockJobId':None}
    assert get_job(factory,repo,replacement.value['triggerState']['clockJobId']) is None


def test_schedule_producer_fence_expiry_takeover_and_replay(semantic_authority):
    factory,repo,rule = setup(semantic_authority,kind='schedule')
    definition = repo.publish(prepare_definition(rule),expected_version=0,now=100)
    event = trigger(kind='schedule',event_id='utc-120');event['occurredAt']=120
    with pytest.raises((AutomationConflict,StorageError)):
        repo.admit(definition.id,event,now=120)
    job,fence = claim_clock(factory)
    accepted = repo.admit(definition.id,event,producer_fence=fence,now=120)
    assert get_job(factory,repo,accepted.value['jobId'])['taskKind'] == 'automation-run'
    wait_until_ms(job["leaseDeadlineMs"])
    # Accepted occurrences are observational reads even after the producer expires.
    assert repo.admit(definition.id,event,producer_fence=fence,now=131).value == accepted.value
    late = deepcopy(event);late['id']='utc-180';late['occurredAt']=180
    with pytest.raises((AutomationConflict,StorageError)):
        repo.admit(definition.id,late,producer_fence=fence,now=131)
    _,replacement = claim_clock(factory,now=131000,worker='replacement')
    with pytest.raises((AutomationConflict,StorageError)):
        repo.admit(definition.id,late,producer_fence=fence,now=131)
    assert repo.admit(definition.id,late,producer_fence=replacement,now=131).version == 1
    updated = repo.publish(prepare_definition(rule),expected_version=1,now=132)
    another = deepcopy(late);another['id']='next-revision'
    with pytest.raises((AutomationConflict,StorageError)):
        repo.admit(updated.id,another,producer_fence=replacement,now=132)


def test_pause_retires_clock_transport_and_preserves_manual_digest(semantic_authority):
    factory,repo,rule = setup(semantic_authority,kind='schedule')
    definition = repo.publish(prepare_definition(rule),expected_version=0,now=100)
    job,fence = claim_clock(factory)
    factory(write=True).command('worker_job.defer',{'task_id':job['taskId'],'worker_id':fence.worker_id,
        'fencing_token':fence.token,'now_ms':120001,'available_at_ms':180000,'replay_cursor':1,
        'observed_cancel_sequence':0},None)
    repo.pause_definition(definition,now=121)
    waiting = get_job(factory,repo,job['taskId'])
    assert waiting is None
    rule['trigger']={'kind':'manual'}
    second = repo.publish(prepare_definition(rule),expected_version=2,now=122)
    paused = repo.pause_definition(second,now=123)
    assert repo.admit(paused.id,trigger(kind='manual'),now=124).value['snapshot']['enabled'] is False


def test_publication_concurrent_same_request_creates_one_revision(semantic_authority):
    factory,repo,rule = setup(semantic_authority)
    prepared=prepare_definition(rule)
    with ThreadPoolExecutor(max_workers=4) as workers:
        rows=list(workers.map(lambda _:repo.publish(prepared,expected_version=0,request_id='same',now=100),range(4)))
    assert {row.version for row in rows} == {1}
    assert all(row.value == rows[0].value for row in rows)
    assert len(subscriptions(factory,repo)) == 1


def test_reserved_writes_and_draft_promotion(semantic_authority):
    factory,repo,rule = setup(semantic_authority)
    draft = repo.save_definition(prepare_definition(rule),expected_version=0,now=99)
    published = repo.publish(prepare_definition(rule),expected_version=draft.version,now=100)
    for operation in ('record.put','record.delete'):
        for namespace,key in [('automation.definitions.v1',repo._key(rule['id'])),
                ('automation.subscriptions.v1',published.value['triggerState']['subscriptionKey'])]:
            payload={'namespace':namespace,'key':key,'user_id':repo.owner_id}
            if operation == 'record.put': payload['value']={'draft':True}
            with pytest.raises(StorageError): factory(write=True).command(operation,payload,None)
    assert repo.get('definition',rule['id']).value == published.value


def test_publication_failure_rolls_back_prior_clock_cancellation(semantic_authority):
    from lib.automations.publication import clock_job_id
    factory,repo,rule = setup(semantic_authority,kind='schedule')
    first=repo.publish(prepare_definition(rule),expected_version=0,now=100)
    new_id=clock_job_id(repo._key(rule['id']),2)
    factory(write=True).command('worker_job.enqueue',{'task_id':new_id,'user_id':repo.owner_id,
        'tenant_id':repo.principal.tenant_id,'task_kind':'foreign-test-kind','payload':{'conflicting':True},
        'idempotency_key':new_id,'priority':200,'available_at_ms':120000,'now_ms':100000},'preseed-conflict')
    with pytest.raises((AutomationConflict,StorageError)):
        repo.publish(prepare_definition(rule),expected_version=1,now=101)
    assert repo.get('definition',rule['id']).value == first.value
    assert get_job(factory,repo,first.value['triggerState']['clockJobId'])['status'] == 'queued'


@pytest.mark.parametrize('mutation',['owner_bool','now_float','extra','proof','overscope','action'])
def test_invalid_publication_cannot_create_definition(semantic_authority,mutation):
    factory,repo,rule = setup(semantic_authority)
    payload = publication_payload(repo,prepare_definition(rule))
    if mutation=='owner_bool': payload['user_id']=True
    elif mutation=='now_float': payload['now_ms']=100000.5
    elif mutation=='extra': payload['unknown']=True
    elif mutation=='proof': payload['definition_json']+=' '
    elif mutation=='overscope': payload['record']['grant']['principal']['scopes'].append('admin')
    else: payload['record']['compiled']['actions']['task.show_time']['version']=999
    with pytest.raises(StorageError): factory(write=True).command('automation.definition.update',payload,None)
    assert repo.get('definition',rule['id']) is None and subscriptions(factory,repo)==[]


def test_original_float_json_proof_survives_publication_and_pause(semantic_authority):
    factory,repo,rule = setup(semantic_authority)
    rule['steps'][0]['when']={'source':{'from':'event','pointer':'/evidence/metric'},'operator':'eq','value':1e-7}
    first=repo.publish(prepare_definition(rule),expected_version=0,now=100)
    assert first.value['definition']['steps'][0]['when']['value']==1e-7
    paused=repo.pause_definition(first,now=101)
    assert paused.value['definition']['steps'][0]['when']['value']==1e-7
