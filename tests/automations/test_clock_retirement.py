"""Definition supersession reclaims only pure clock transport, never run state."""
import pytest
import time

from lib.automations.definitions import prepare_definition
from lib.automations.contract import storage_schema
from lib.storage.errors import StorageError
from tests.automations.test_atomic_publication import setup,get_job,claim_clock
from tests.support.automation_fixtures import trigger

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('state',['queued','running','suspended','succeeded','failed','cancelled'])
def test_supersession_releases_clock_document_and_all_claim_indexes(semantic_authority,state):
    factory,repo,rule=setup(semantic_authority,kind='schedule')
    original=repo.publish(prepare_definition(rule),expected_version=0,now=100)
    clock_id=original.value['triggerState']['clockJobId']
    if state!='queued':
        job,fence=claim_clock(factory)
        if state=='suspended':
            factory(write=True).command('worker_job.defer',{'task_id':clock_id,'worker_id':fence.worker_id,
                'fencing_token':fence.token,'now_ms':120001,'available_at_ms':180000,'replay_cursor':120000,
                'observed_cancel_sequence':0},None)
        elif state in {'succeeded','failed','cancelled'}:
            factory(write=True).command('worker_job.complete',{'task_id':clock_id,'worker_id':fence.worker_id,
                'fencing_token':fence.token,'now_ms':120001,'terminal_status':state,
                'result_ref':'','error':{'kind':'synthetic_clock_failure'} if state=='failed' else {},
                'replay_cursor':120000},'clock-terminal-'+state)
    # A separately accepted manual run must survive clock transport retirement.
    accepted=repo.admit(original.id,trigger(kind='manual'),now=120)
    replacement_time=time.time()
    replacement=repo.publish(prepare_definition(rule),expected_version=1,now=replacement_time)
    assert get_job(factory,repo,clock_id) is None
    assert repo.get('run',accepted.id).value == accepted.value
    assert get_job(factory,repo,accepted.value['jobId'])['status']=='queued'
    assert repo.publish(prepare_definition(rule),expected_version=1,now=replacement_time).value == replacement.value
    # Lease/availability indexes must neither return the deleted job nor poison
    # queue summaries. Only the new revision's future slot remains claimable.
    assert factory(write=True).command('worker_job.claim_next',{'worker_id':'probe','task_kinds':['automation-clock'],
        'now_ms':140000,'lease_ms':10000},None) is None
    current=get_job(factory,repo,replacement.value['triggerState']['clockJobId'])
    assert current['status']=='queued' and current['availableAtMs']>int(replacement_time*1000)
    # Releasing the obsolete idempotency reference permits an unrelated owned
    # transport identity; this cannot resurrect a reserved automation kind.
    recycled=factory(write=True).command('worker_job.enqueue',{'task_id':clock_id,'user_id':repo.owner_id,
        'tenant_id':repo.principal.tenant_id,'task_kind':'retirement-probe','idempotency_key':clock_id,
        'payload':{'probe':True},'now_ms':181000},'recycled-clock-id')
    assert recycled['created'] is True and recycled['job']['taskKind']=='retirement-probe'


def test_retirement_keeps_latest_bounded_failure_across_pause_and_republication(semantic_authority):
    factory,repo,rule=setup(semantic_authority,kind='schedule')
    original=repo.publish(prepare_definition(rule),expected_version=0,now=100)
    job,fence=claim_clock(factory)
    factory(write=True).command('worker_job.complete',{'task_id':job['taskId'],'worker_id':fence.worker_id,
        'fencing_token':fence.token,'now_ms':120001,'terminal_status':'failed','result_ref':'',
        'error':{'kind':'failure_'+'x'*200,'detail':'diagnostic transport detail'},'replay_cursor':0},'failed-clock')
    paused=repo.pause_definition(original,now=121)
    failure=paused.value['lastClockFailure']
    assert failure=={'jobId':job['taskId'],'revision':1,'failedAtMs':120001,
        'kind':('failure_'+'x'*200)[:storage_schema()['x-clock']['supersession']['failureKindCharacters']]}
    assert get_job(factory,repo,job['taskId']) is None
    republished=repo.publish(prepare_definition(rule),expected_version=2,now=122)
    assert republished.value['lastClockFailure']==failure
    retired=repo.retire_definition(republished,now=123)
    assert retired.value['lastClockFailure']==failure


@pytest.mark.parametrize('kind',['automation-clock','automation-run'])
def test_generic_enqueue_cannot_create_or_resurrect_an_automation_kind(semantic_authority,kind):
    factory,repo,rule=setup(semantic_authority,kind='schedule')
    definition=repo.publish(prepare_definition(rule),expected_version=0,now=100)
    clock_id=definition.value['triggerState']['clockJobId']
    repo.pause_definition(definition,now=101)
    with pytest.raises(StorageError):
        factory(write=True).command('worker_job.enqueue',{'task_id':clock_id,'user_id':repo.owner_id,
            'tenant_id':repo.principal.tenant_id,'task_kind':kind,'idempotency_key':clock_id,
            'payload':{},'now_ms':102000},'resurrection-'+kind)
    assert get_job(factory,repo,clock_id) is None


def test_many_edits_keep_one_clock_and_pause_releases_the_last(semantic_authority):
    factory,repo,rule=setup(semantic_authority,kind='schedule')
    retired_ids=[]
    current=repo.publish(prepare_definition(rule),expected_version=0,now=100)
    for index in range(8):
        previous_id=current.value['triggerState']['clockJobId']
        rule['name']=f'Clock revision {index+2}'
        current=repo.publish(prepare_definition(rule),expected_version=current.version,now=101+index)
        retired_ids.append(previous_id)
        assert all(get_job(factory,repo,task_id) is None for task_id in retired_ids)
        assert get_job(factory,repo,current.value['triggerState']['clockJobId'])['status']=='queued'
    final_id=current.value['triggerState']['clockJobId']
    repo.pause_definition(current,now=110)
    assert get_job(factory,repo,final_id) is None
    assert factory(write=True).command('worker_job.claim_next',{'worker_id':'empty-queue',
        'task_kinds':['automation-clock'],'now_ms':120000,'lease_ms':10000},None) is None


def test_noncallable_draft_cannot_forge_durable_clock_failure_metadata(semantic_authority):
    factory,repo,rule=setup(semantic_authority)
    prepared=prepare_definition(rule)
    draft=repo.save_definition(prepared,expected_version=0,now=99)
    value=draft.value;value['lastClockFailure']={'kind':'forged'}
    modified=repo.put('definition',draft.id,value,expected_version=draft.version)
    published=repo.publish(prepared,expected_version=modified.version,now=100)
    assert 'lastClockFailure' not in published.value
