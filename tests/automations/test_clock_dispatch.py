"""Real canonical workers dispatch UTC slots, replay lost ACKs and suspend."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from lib.automations.clock import AutomationClockHandler
from lib.automations.definitions import prepare_definition
from lib.durable_worker import StorageWorkerJobStore,SynchronousWorkerRunner
from lib.storage.errors import StorageError
from tests.automations.test_atomic_publication import setup,get_job
from tests.support.worker_job_time import wall_ms,wait_until_ms

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def fixture(authority, *, expression='* * * * *', catch_up='latest'):
    factory,repo,rule=setup(authority,kind='schedule')
    rule['trigger'].update(schedule=expression,catchUp=catch_up)
    # Retain the original synthetic slot offsets, anchored to real UTC.
    # Caller observations cannot advance the storage authority's queue clock.
    anchor=wall_ms()//60000*60000-120000
    if expression=='0 * * * *':
        anchor=wall_ms()//3600000*3600000-3600000
    definition=repo.publish(prepare_definition(rule),expected_version=0,now=(anchor+100000)/1000)
    clock={'now':anchor+120000,'anchor':anchor}
    return factory,repo,rule,definition,clock


def runner(factory,clock, *, worker='clock-worker', handler_factory=None,store_type=StorageWorkerJobStore):
    class Store(store_type):
        def _client(self, *, write=False): return factory(write=write)
    handler=AutomationClockHandler(client_factory=handler_factory or factory,now_ms=lambda:clock['now'])
    return SynchronousWorkerRunner(worker_id=worker,store=Store(),registrations=[handler.registration()],
        lease_ms=10000,heartbeat_interval_ms=1000,now_ms=lambda:clock['now'])


def runs(repo):
    return repo.page('run',limit=64)[0]


def no_wait_threads():
    assert not any(thread.name.startswith('durable-worker-heartbeat:') for thread in threading.enumerate())


def test_clock_reuses_one_suspended_job_and_admits_distinct_utc_slots(semantic_authority):
    factory,repo,_,definition,clock=fixture(semantic_authority)
    worker=runner(factory,clock)
    assert worker.run_once().state=='suspended'
    run=runs(repo)[0]
    slot=clock['anchor']+120000
    assert run.value['trigger']=={'id':f'utc:{slot}','kind':'schedule','occurredAt':slot/1000,
        'origin':'automation','evidence':{'scheduledAt':slot/1000},'automationChain':[]}
    job=get_job(factory,repo,definition.value['triggerState']['clockJobId'])
    assert job['status']=='suspended' and job['leaseDeadlineMs']==0
    assert job['availableAtMs']==clock['anchor']+180000 and job['replayCursor']==slot
    assert worker.run_once().state=='idle' and len(runs(repo))==1
    wait_until_ms(job['availableAtMs'])
    clock['now']=clock['anchor']+180012
    assert worker.run_once().state=='suspended' and len(runs(repo))==2
    later=get_job(factory,repo,job['taskId'])
    assert later['taskId']==job['taskId'] and later['availableAtMs']==clock['anchor']+240000 and later['replayCursor']==clock['now']
    no_wait_threads()


@pytest.mark.parametrize('catch_up,expected', [('latest',1),('skip',0)])
def test_clock_downtime_selects_latest_or_skips_past_minute(semantic_authority,catch_up,expected):
    factory,repo,_,definition,clock=fixture(semantic_authority,expression='0 * * * *',catch_up=catch_up)
    clock['now']=clock['anchor']+3660000
    assert runner(factory,clock).run_once().state=='suspended'
    accepted=runs(repo)
    assert len(accepted)==expected
    if expected: assert accepted[0].value['trigger']['occurredAt']==(clock['anchor']+3600000)/1000
    job=get_job(factory,repo,definition.value['triggerState']['clockJobId'])
    assert job['availableAtMs']==clock['anchor']+7200000 and job['replayCursor']==clock['now']


def test_two_workers_cannot_dispatch_one_slot_twice(semantic_authority):
    factory,repo,_,_,clock=fixture(semantic_authority)
    workers=[runner(factory,clock,worker=f'clock-{index}') for index in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda worker:worker.run_once().state,workers))
    assert sorted(results)==['idle','suspended'] and len(runs(repo))==1
    no_wait_threads()


def test_pause_retires_clock_without_cancelling_accepted_run(semantic_authority):
    factory,repo,_,definition,clock=fixture(semantic_authority)
    worker=runner(factory,clock)
    assert worker.run_once().state=='suspended'
    accepted=runs(repo)[0]
    repo.pause_definition(definition,now=(clock['anchor']+121000)/1000)
    clock['now']=clock['anchor']+121000
    assert worker.run_once().state=='idle'
    job=get_job(factory,repo,definition.value['triggerState']['clockJobId'])
    assert job is None
    assert repo.get('run',accepted.id).value==accepted.value
    assert get_job(factory,repo,accepted.value['jobId'])['status']=='queued'
    no_wait_threads()


@pytest.mark.parametrize('committed',[True,False])
def test_lost_admission_ack_keeps_first_occurrence_or_retries_durable_wait(semantic_authority,committed):
    factory,repo,_,definition,clock=fixture(semantic_authority)
    calls=[]
    class Client:
        def command(self,name,payload,receipt):
            if name=='automation.run.admit' and not calls:
                calls.append(name)
                if committed: factory(write=True).command(name,payload,receipt)
                raise StorageError('database_timeout','Synthetic lost admission ACK')
            return factory(write=True).command(name,payload,receipt)
        def query(self,name,payload): return factory().query(name,payload)
    worker=runner(factory,clock,handler_factory=lambda **_:Client())
    assert worker.run_once().state=='suspended'
    job=get_job(factory,repo,definition.value['triggerState']['clockJobId'])
    if committed:
        accepted=runs(repo)[0]
        assert accepted.value['createdAt']==clock['now']/1000 and job['availableAtMs']==clock['anchor']+180000
        assert len(calls)==1
    else:
        assert runs(repo)==[] and job['availableAtMs']==clock['anchor']+121000 and job['replayCursor']==0
        wait_until_ms(job['availableAtMs'])
        clock['now']=clock['anchor']+121000
        assert worker.run_once().state=='suspended'
        accepted=runs(repo)[0]
        assert accepted.value['createdAt']==clock['now']/1000 and accepted.value['trigger']['occurredAt']==(clock['anchor']+120000)/1000
    assert len(runs(repo))==1


def test_expired_claim_during_definition_read_cannot_use_old_clock_observation(semantic_authority):
    factory,repo,_,definition,clock=fixture(semantic_authority)
    class Client:
        def command(self,name,payload,receipt): return factory(write=True).command(name,payload,receipt)
        def query(self,name,payload):
            value=factory().query(name,payload)
            if name=='record.get' and payload['namespace']=='automation.definitions.v1':
                job=get_job(factory,repo,definition.value['triggerState']['clockJobId'])
                wait_until_ms(job['leaseDeadlineMs'])
                clock['now']=clock['anchor']+130001
            return value
    class DroppedHeartbeats(StorageWorkerJobStore):
        def heartbeat(self,claim,**kwargs):
            # Model a disconnected writer: observation continues, renewal does not.
            return {'ok':True,'job':get_job(factory,repo,claim.task_id)}
    assert runner(factory,clock,handler_factory=lambda **_:Client(),store_type=DroppedHeartbeats).run_once().state=='lease_lost'
    assert runs(repo)==[]
    assert get_job(factory,repo,definition.value['triggerState']['clockJobId'])['status']=='running'
    assert runner(factory,clock,worker='takeover').run_once().state=='suspended'
    assert len(runs(repo))==1


def test_crash_after_admission_before_suspend_replays_frozen_utc_evidence(semantic_authority):
    factory,repo,_,definition,clock=fixture(semantic_authority)
    class InterruptedStore(StorageWorkerJobStore):
        def defer(self,*args,**kwargs): raise SystemExit('Injected worker death before durable defer')
    with pytest.raises(SystemExit): runner(factory,clock,store_type=InterruptedStore).run_once()
    accepted=runs(repo)[0]
    engine=semantic_authority[2]
    if engine is not None: engine.crash_cycle()
    else:
        job=get_job(factory,repo,definition.value['triggerState']['clockJobId'])
        wait_until_ms(job['leaseDeadlineMs'])
    clock['now']=clock['anchor']+130001
    assert runner(factory,clock,worker='recovered').run_once().state=='suspended'
    assert len(runs(repo))==1 and runs(repo)[0].value==accepted.value
    assert get_job(factory,repo,definition.value['triggerState']['clockJobId'])['replayCursor']==clock['now']
    no_wait_threads()


def test_definition_replacement_during_dispatch_retires_old_clock_without_new_run(semantic_authority):
    factory,repo,rule,definition,clock=fixture(semantic_authority)
    replacement=[]
    class Client:
        def command(self,name,payload,receipt): return factory(write=True).command(name,payload,receipt)
        def query(self,name,payload):
            value=factory().query(name,payload)
            if (name=='record.get' and payload['namespace']=='automation.definitions.v1' and not replacement):
                replacement.append(repo.publish(prepare_definition(rule),expected_version=1,now=clock['now']/1000))
            return value
    assert runner(factory,clock,handler_factory=lambda **_:Client()).run_once().state=='lease_lost'
    assert runs(repo)==[]
    assert get_job(factory,repo,definition.value['triggerState']['clockJobId']) is None
    assert get_job(factory,repo,replacement[0].value['triggerState']['clockJobId'])['status']=='queued'


def test_pause_between_admission_and_defer_revokes_clock_while_retaining_run(semantic_authority):
    factory,repo,_,definition,clock=fixture(semantic_authority)
    class PausingStore(StorageWorkerJobStore):
        def defer(self,*args,**kwargs):
            repo.pause_definition(definition,now=clock['now']/1000)
            return super().defer(*args,**kwargs)
    assert runner(factory,clock,store_type=PausingStore).run_once().state=='lease_lost'
    clock_job=get_job(factory,repo,definition.value['triggerState']['clockJobId'])
    assert clock_job is None
    assert runner(factory,clock,worker='cleanup').run_once().state=='idle'
    assert len(runs(repo))==1 and runs(repo)[0].value['status']=='queued'


def test_backward_clock_and_early_poll_preserve_cursor_without_duplicates(semantic_authority):
    factory,repo,_,definition,clock=fixture(semantic_authority)
    worker=runner(factory,clock)
    clock['now']=clock['anchor']+119999
    # The slot is already due at the real store, but the earlier calendar
    # observation must still admit no occurrence.
    assert worker.run_once().state=='suspended' and runs(repo)==[]
    clock['now']=clock['anchor']+120000
    assert worker.run_once().state=='suspended'
    clock['now']=clock['anchor']+90000
    assert worker.run_once().state=='idle' and len(runs(repo))==1
    assert get_job(factory,repo,definition.value['triggerState']['clockJobId'])['replayCursor']==clock['anchor']+120000
