"""Real event workers replay admission, checkpoint batches, and retire transport."""
import threading
import time

from tests.support.worker_job_time import wait_until_ms

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.event_delivery import AutomationEventHandler
from lib.automations.events import task_started_job
from lib.automations.contract import storage_schema
from lib.durable_worker import StorageWorkerJobStore,SynchronousWorkerRunner
from lib.storage.errors import StorageError
from tests.automations.test_atomic_publication import setup,get_job
from tests.automations.test_task_started_events import pending,context,start

pytest_plugins=('tests.support.semantic_authorities',)
pytestmark=pytest.mark.api


def fixture(authority,monkeypatch, *, rules=1):
    factory,repo,rule=setup(authority)
    for index in range(rules):
        value=dict(rule);value['id']=rule['id']+f'-{index:04}'
        repo.publish(prepare_definition(value),expected_version=0,now=100)
    _,owner,created,attempt,task=pending(authority,monkeypatch)
    ctx=context();result=start(factory,owner,attempt,task,ctx)
    job_id,payload=task_started_job(owner,ctx,attempt_id=attempt,task_id=task,
        conversation_id=created['turn']['conversationId'],turn_id=created['turn']['turnId'],started_at_ms=result['startedAt'])
    clock={'now':result['startedAt']+1}
    return factory,repo,rule,job_id,clock


def runner(factory,clock, *, handler_factory=None,worker='event-worker'):
    class Store(StorageWorkerJobStore):
        def _client(self, *, write=False):return factory(write=write)
    handler=AutomationEventHandler(client_factory=handler_factory or factory,now_ms=lambda:clock['now'])
    return SynchronousWorkerRunner(worker_id=worker,store=Store(),registrations=[handler.registration()],
        now_ms=lambda:clock['now'],lease_ms=10000,heartbeat_interval_ms=1000)


def drain(worker, *, factory, repo, job_id, limit=20):
    states=[]
    for _ in range(limit):
        state=worker.run_once().state;states.append(state)
        if state=='suspended':
            waiting=get_job(factory,repo,job_id)
            if waiting is not None:wait_until_ms(waiting['availableAtMs'])
        if state=='idle':return states
    raise AssertionError('Event worker did not finish bounded fanout')


def test_event_worker_admits_frozen_runs_and_retires_only_transport(semantic_authority,monkeypatch):
    factory,repo,rule,job_id,clock=fixture(semantic_authority,monkeypatch,rules=2)
    original=get_job(factory,repo,job_id)['payload']['event']
    states=drain(runner(factory,clock),factory=factory,repo=repo,job_id=job_id)
    assert states==['suspended','suspended','succeeded','idle']
    assert get_job(factory,repo,job_id) is None
    runs=repo.page('run',limit=64)[0]
    assert len(runs)==2 and all(run.value['trigger']==original for run in runs)
    assert all(get_job(factory,repo,run.value['jobId'])['status']=='queued' for run in runs)
    assert not any(t.name.startswith('durable-worker-heartbeat:') for t in threading.enumerate())


@pytest.mark.parametrize('committed',[True,False])
def test_lost_admission_ack_replays_one_occurrence(semantic_authority,monkeypatch,committed):
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch)
    calls=[]
    class Client:
        def query(self,name,payload):return factory().query(name,payload)
        def command(self,name,payload,receipt):
            if name=='automation.run.admit' and not calls:
                calls.append(name)
                if committed:factory(write=True).command(name,payload,receipt)
                raise StorageError('database_timeout','Lost admission ACK')
            return factory(write=True).command(name,payload,receipt)
    worker=runner(factory,clock,handler_factory=lambda **kw:Client())
    assert worker.run_once().state=='suspended'
    wait_until_ms(get_job(factory,repo,job_id)['availableAtMs'])
    clock['now']+=storage_schema()['x-event']['failureRetryMs']
    states=drain(worker,factory=factory,repo=repo,job_id=job_id)
    assert states[-2:]==['succeeded','idle']
    assert len(repo.page('run',limit=64)[0])==1 and get_job(factory,repo,job_id) is None


@pytest.mark.parametrize('committed',[True,False])
def test_lost_checkpoint_ack_preserves_frozen_event_and_cursor(semantic_authority,monkeypatch,committed):
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch)
    original=get_job(factory,repo,job_id)['payload']['event'];calls=[]
    class Client:
        def query(self,name,payload):return factory().query(name,payload)
        def command(self,name,payload,receipt):
            if name=='automation.event.settle' and payload['mode']=='advance' and not calls:
                calls.append(name)
                if committed:factory(write=True).command(name,payload,receipt)
                raise StorageError('database_timeout','Lost checkpoint ACK')
            return factory(write=True).command(name,payload,receipt)
    worker=runner(factory,clock,handler_factory=lambda **kw:Client())
    assert worker.run_once().state=='suspended'
    wait_until_ms(get_job(factory,repo,job_id)['availableAtMs']);clock['now']+=1000
    assert get_job(factory,repo,job_id)['payload']['event']==original
    assert drain(worker,factory=factory,repo=repo,job_id=job_id)[-2:]==['succeeded','idle']
    assert len(repo.page('run',limit=64)[0])==1


def test_owner_cancel_retires_transport_and_preserves_admitted_runs(semantic_authority,monkeypatch):
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch)
    worker=runner(factory,clock)
    assert worker.run_once().state=='suspended'
    admitted=repo.page('run',limit=64)[0][0]
    factory(write=True).command('worker_job.request_cancel',{'user_id':repo.owner_id,
        'task_id':job_id,'reason':'owner-command','now_ms':clock['now']},'cancel-event')
    assert worker.run_once().state=='cancelled' and get_job(factory,repo,job_id) is None
    assert repo.get('run',admitted.id).value==admitted.value


def test_cursor_pages_large_fanout_without_duplicate_admission(semantic_authority,monkeypatch):
    size=storage_schema()['x-event']['batchRows']+1
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch,rules=size)
    from lib.automations.events import subscription_prefixes
    rows=factory().query('record.list',{'namespace':'automation.subscriptions.v1',
        'prefix':subscription_prefixes(repo.owner_id,context())[0],'limit':size-1})
    assert len(rows)==size-1
    worker=runner(factory,clock)
    assert worker.run_once().state=='suspended'
    cursor=get_job(factory,repo,job_id)['payload']['delivery']
    assert cursor['index']==0 and cursor['processed']==size-1 and cursor['afterKey']
    assert drain(worker,factory=factory,repo=repo,job_id=job_id)[-2:]==['succeeded','idle']
    first,next_key=repo.page('run',limit=64)
    second,_=repo.page('run',limit=64,after=next_key) if next_key else ([],None)
    assert len(first)+len(second)==size


def test_completion_joins_monitor_before_retiring_transport(semantic_authority,monkeypatch):
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch)
    observed=[]
    class Client:
        def query(self,name,payload):return factory().query(name,payload)
        def command(self,name,payload,receipt):
            if name=='automation.event.settle' and payload['mode']=='succeeded':
                observed.append(any(t.name.startswith('durable-worker-heartbeat:') for t in threading.enumerate()))
            return factory(write=True).command(name,payload,receipt)
    assert drain(runner(factory,clock,handler_factory=lambda **kw:Client()),factory=factory,repo=repo,job_id=job_id)[-2:]==['succeeded','idle']
    assert observed==[False]


def claim_event(factory,job_id,clock, *, worker='event-proof'):
    job=factory(write=True).command('worker_job.claim_next',{'worker_id':worker,
        'task_kinds':['automation-event'],'now_ms':clock['now'],'lease_ms':10000},None)
    assert job['taskId']==job_id
    return job


def settlement(job,clock, *, mode='advance',next_cursor=None):
    return {'user_id':job['userId'],'tenant_id':job['tenantId'],'job_id':job['taskId'],
        'worker_id':job['claimOwner'],'fencing_token':job['fencingToken'],
        'expected_payload':job['payload'],'next_delivery':next_cursor,
        'mode':mode,'error_kind':'event_test_failure' if mode=='failed' else '', 'now_ms':clock['now']}


def test_event_cursor_rejects_scope_jumps_forgery_and_unfinished_retirement(semantic_authority,monkeypatch):
    from copy import deepcopy
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch)
    job=claim_event(factory,job_id,clock)
    valid=settlement(job,clock,next_cursor={'index':1,'afterKey':'','processed':0})
    bad=[]
    for cursor in ({'index':2,'afterKey':'','processed':0},
                   {'index':0,'afterKey':'other-owner:cursor','processed':1},
                   {'index':1,'afterKey':'','processed':65},
                   {'index':0,'afterKey':'','processed':0}):
        bad.append({**valid,'next_delivery':cursor})
    forged=deepcopy(valid);forged['expected_payload']['event']['evidence']['conversationId']='forged'
    bad.extend([forged,settlement(job,clock,mode='succeeded'),settlement(job,clock,mode='cancelled')])
    for payload in bad:
        with pytest.raises(StorageError):factory(write=True).command('automation.event.settle',payload,None)
        assert get_job(factory,repo,job_id)==job
    assert repo.page('run',limit=64)[0]==[]


def test_live_fence_is_required_but_exact_checkpoint_replay_is_observational(semantic_authority,monkeypatch):
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch)
    job=claim_event(factory,job_id,clock)
    payload=settlement(job,clock,next_cursor={'index':1,'afterKey':'','processed':0})
    assert factory(write=True).command('automation.event.settle',payload,None)['applied']
    wait_until_ms(job['leaseDeadlineMs'])
    clock['now']+=10001
    payload['now_ms']=clock['now']
    replay=factory(write=True).command('automation.event.settle',payload,None)
    assert replay['ok'] and not replay['applied']
    current=get_job(factory,repo,job_id)
    refused=settlement(current,clock,next_cursor={'index':2,'afterKey':'','processed':0})
    assert factory(write=True).command('automation.event.settle',refused,None)['error']=='producer_fence_lost'
    replacement=claim_event(factory,job_id,clock,worker='replacement')
    assert replacement['fencingToken']>job['fencingToken']
    assert factory(write=True).command('automation.event.settle',refused,None)['ok'] is False
    accepted=settlement(replacement,clock,next_cursor={'index':2,'afterKey':'','processed':0})
    assert factory(write=True).command('automation.event.settle',accepted,None)['applied']


def test_canonical_event_admission_requires_matching_live_producer(semantic_authority,monkeypatch):
    from copy import deepcopy
    from lib.automations.errors import AutomationConflict
    from lib.automations.transitions import ExecutionFence
    factory,repo,rule,job_id,clock=fixture(semantic_authority,monkeypatch)
    definition=repo.get('definition',rule['id']+'-0000')
    event=get_job(factory,repo,job_id)['payload']['event']
    with pytest.raises(AutomationConflict):repo.admit(definition.id,event,now=clock['now']/1000)
    job=claim_event(factory,job_id,clock)
    fence=ExecutionFence(job_id,job['claimOwner'],job['fencingToken'])
    forged=deepcopy(event);forged['evidence']['conversationId']='forged'
    with pytest.raises(AutomationConflict):repo.admit(definition.id,forged,now=clock['now']/1000,producer_fence=fence)
    accepted=repo.admit(definition.id,event,now=clock['now']/1000,producer_fence=fence)
    wait_until_ms(job['leaseDeadlineMs'])
    clock['now']+=10001
    assert repo.admit(definition.id,event,now=clock['now']/1000,producer_fence=fence).value==accepted.value


def test_permanent_failure_atomically_preserves_one_owned_diagnostic(semantic_authority,monkeypatch):
    import hashlib
    from lib.automations.contract import STORAGE_NAMESPACES
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch)
    job=claim_event(factory,job_id,clock)
    payload=settlement(job,clock,mode='failed')
    first=factory(write=True).command('automation.event.settle',payload,None)
    assert first['applied'] and get_job(factory,repo,job_id) is None
    key=f"{repo.owner_id}:{hashlib.sha256(repo.principal.tenant_id.encode()).hexdigest()[:32]}:{job_id}"
    get={'namespace':STORAGE_NAMESPACES['eventFailure'],'key':key,'user_id':repo.owner_id}
    saved=factory().query('record.get',get)
    assert saved['value']['event']==job['payload']['event'] and saved['value']['kind']=='event_test_failure'
    assert not factory(write=True).command('automation.event.settle',payload,None)['applied']
    assert factory().query('record.get',get)==saved
    with pytest.raises(StorageError):
        factory(write=True).command('automation.event.settle',{**payload,'error_kind':'different_failure'},None)
    with pytest.raises(StorageError):
        factory(write=True).command('record.put',{**get,'value':{},'expected_version':saved['version']},'forge-event-failure')
    assert factory().query('record.get',get)==saved
    assert runner(factory,clock).run_once().state=='idle'


@pytest.mark.parametrize('committed',[True,False])
def test_lost_retirement_ack_and_authority_restart_preserve_runs(semantic_authority,monkeypatch,committed):
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch)
    calls=[]
    class Client:
        def query(self,name,payload):return factory().query(name,payload)
        def command(self,name,payload,receipt):
            if name=='automation.event.settle' and payload['mode']=='succeeded' and not calls:
                calls.append(name)
                if committed:factory(write=True).command(name,payload,receipt)
                raise StorageError('database_timeout','Lost retirement ACK')
            return factory(write=True).command(name,payload,receipt)
    worker=runner(factory,clock,handler_factory=lambda **kw:Client())
    assert worker.run_once().state=='suspended'
    assert worker.run_once().state=='suspended'
    assert worker.run_once().state=='terminal_unconfirmed'
    accepted=repo.page('run',limit=64)[0][0]
    engine=semantic_authority[2]
    if engine is not None:engine.crash_cycle()
    else:
        from lib.storage.service import storage_runtime
        runtime=storage_runtime();runtime.stop();runtime.start()
    # A database-server authority survives PostgreSQL Sidecar turnover. Recovery
    # must respect its existing lease rather than trusting an advanced caller.
    remaining=get_job(factory,repo,job_id)
    if remaining is not None:
        live=factory().query('worker_job.claim_state',{
            'task_id':job_id,'worker_id':remaining['claimOwner'],
            'fencing_token':remaining['fencingToken'],'now_ms':clock['now']})
        if live['ok']:wait_until_ms(remaining['leaseDeadlineMs'])
    clock['now']+=10001
    assert drain(runner(factory,clock,worker='recovery'),factory=factory,repo=repo,job_id=job_id)[-1]=='idle'
    assert get_job(factory,repo,job_id) is None
    assert repo.get('run',accepted.id).value==accepted.value
    assert get_job(factory,repo,accepted.value['jobId'])['status']=='queued'


def test_newly_published_rule_does_not_retroactively_consume_old_event(semantic_authority,monkeypatch):
    factory,repo,rule,job_id,clock=fixture(semantic_authority,monkeypatch)
    late=dict(rule);late['id']+='-late'
    repo.publish(prepare_definition(late),expected_version=0,now=(clock['now']+100)/1000)
    assert drain(runner(factory,clock),factory=factory,repo=repo,job_id=job_id)[-2:]==['succeeded','idle']
    runs=repo.page('run',limit=64)[0]
    assert len(runs)==1 and runs[0].value['definitionId']==rule['id']+'-0000'


def test_background_event_does_not_outrank_human_priority(semantic_authority,monkeypatch):
    factory,repo,_,job_id,clock=fixture(semantic_authority,monkeypatch)
    human='human-priority-'+job_id
    factory(write=True).command('worker_job.enqueue',{'task_id':human,'user_id':repo.owner_id,
        'tenant_id':repo.principal.tenant_id,'task_kind':'human-test','payload':{},
        'idempotency_key':human,'priority':100,'available_at_ms':clock['now'],'now_ms':clock['now']},'enqueue-human-priority')
    claimed=factory(write=True).command('worker_job.claim_next',{'worker_id':'fairness',
        'task_kinds':['human-test','automation-event'],'now_ms':clock['now'],'lease_ms':10000},None)
    assert claimed['taskId']==human
