"""Atomic automation commands through the application's selected storage client."""
import pytest
from lib.automations.definitions import prepare_definition
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.automations.transitions import ExecutionFence
from lib.identity import PrincipalContext
from lib.storage import get_storage_client
from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.support.automation_fixtures import clock_definition,trigger

pytestmark = pytest.mark.api


def test_publication_clock_admission_and_pause_through_application_adapter(application_storage):
    principal=PrincipalContext.user(subject_id='app-contract',owner_user_id=1,tenant_id='app-contract',
        scopes={'automations:read','automations:manage','automations:execute'})
    repo=AutomationExecutionRepository(principal)
    rule=clock_definition()
    rule['trigger']={'kind':'schedule','schedule':'* * * * *','timezone':'UTC','catchUp':'latest'}
    definition=repo.publish(prepare_definition(rule),expected_version=0,now=100)
    claim=get_storage_client(write=True).command('worker_job.claim_next',{'worker_id':'app-clock',
        'task_kinds':['automation-clock'],'now_ms':120000,'lease_ms':10000},None)
    assert claim['taskId'] == definition.value['triggerState']['clockJobId']
    event=trigger(kind='schedule',event_id='app-utc-120');event['occurredAt']=120
    run=repo.admit(definition.id,event,producer_fence=ExecutionFence(claim['taskId'],claim['claimOwner'],claim['fencingToken']),now=120)
    cancelled=repo.cancel(run.id,request_id='app-stop',reason='Application contract',now=121)
    assert cancelled.value['status']=='cancelled'
    paused=repo.pause_definition(definition,now=122)
    assert paused.value['definition']['enabled'] is False
    clock=get_storage_client().query('worker_job.get',{'task_id':claim['taskId'],'user_id':principal.owner_user_id})
    assert clock is None


def test_canonical_clock_handler_uses_application_storage_and_leaves_no_wait_thread(application_storage):
    import threading
    from lib.automations.clock import AutomationClockHandler
    from lib.durable_worker import StorageWorkerJobStore,SynchronousWorkerRunner
    principal=PrincipalContext.user(subject_id='clock-app-contract',owner_user_id=1,tenant_id='clock-app-contract',
        scopes={'automations:read','automations:manage','automations:execute'})
    repo=AutomationExecutionRepository(principal)
    rule=clock_definition()
    rule['trigger']={'kind':'schedule','schedule':'* * * * *','timezone':'UTC','catchUp':'latest'}
    definition=repo.publish(prepare_definition(rule),expected_version=0,now=100)
    clock={'now':120000}
    handler=AutomationClockHandler(client_factory=get_storage_client,now_ms=lambda:clock['now'])
    worker=SynchronousWorkerRunner(worker_id='actual-adapter-clock',store=StorageWorkerJobStore(),
        registrations=[handler.registration()],lease_ms=10000,heartbeat_interval_ms=1000,now_ms=lambda:clock['now'])
    assert worker.run_once().state=='suspended'
    clock['now']=180000
    assert worker.run_once().state=='suspended'
    assert len(repo.page('run',limit=64)[0])==2
    repo.pause_definition(definition,now=181)
    clock['now']=181000
    assert worker.run_once().state=='idle'
    assert not any(thread.name.startswith('durable-worker-heartbeat:') for thread in threading.enumerate())
