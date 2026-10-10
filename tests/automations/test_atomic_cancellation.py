"""Owner cancellation couples run, canonical job and indexes on real stores.

Agent receipts are synthetic at the external boundary. These tests prove
durable intent/settlement, not live Agent execution or a rendered interface.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import uuid

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.automations.errors import AutomationConflict, AutomationError
from lib.automations.transitions import (
    ExecutionFence, bind_fence, prepare_step, record_output, record_failure, request_cancel,
)
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from tests.support.automation_fixtures import clock_definition, trigger

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def setup_run(authority, *, agent=False):
    factory, owner, _ = authority
    principal = PrincipalContext.user(subject_id='publisher', owner_user_id=owner,
        tenant_id='test-tenant', scopes={'automations:read','automations:manage','automations:execute','conversation.execute'})
    repo = AutomationExecutionRepository(principal, client_factory=factory)
    rule = clock_definition()
    rule['id'] += '-' + uuid.uuid4().hex
    if agent:
        rule['policy']['maxModelTokens'] = 1000
        rule['steps'][0].update(action='agent.run', arguments={
            'objective':'Synthetic cancellation boundary', 'conversationPolicy':'new',
        })
    definition = repo.publish(prepare_definition(rule), expected_version=0, now=100)
    return repo, repo.admit(definition.id, trigger(event_id=uuid.uuid4().hex), now=100), factory


def job(factory, repo, run):
    return factory().query('worker_job.get', {'task_id':run.value['jobId'],'user_id':repo.owner_id})


def active(factory, repo):
    return factory().query('record.list', {'namespace':'automation.active.v1',
        'prefix':repo._prefix,'user_id':repo.owner_id,'limit':64})


def claim(factory, run):
    doc = factory(write=True).command('worker_job.claim_next', {
        'worker_id':'cancel-test-worker','task_kinds':['automation-run'],'now_ms':100000,'lease_ms':10000,
    }, None)
    assert doc['taskId'] == run.value['jobId']
    return ExecutionFence(doc['taskId'],doc['claimOwner'],doc['fencingToken'])


def intent(repo, run, factory, *, waiting=False):
    fence = claim(factory,run)
    run = repo.advance(run,bind_fence(run.value,fence,now=100.1),fence,now=100.1)
    dispatching, step = prepare_step(run.value,fence,now=100.2)
    run = repo.advance(run,dispatching,fence,now=100.2)
    if waiting:
        receipt = {'conversationId':'test-conversation','taskId':'test-task',
            'turnId':'test-turn','attemptId':'test-attempt','status':'queued'}
        run = repo.advance(run,record_output(run.value,step,receipt,fence,now=100.3),fence,now=100.3)
        deferred = factory(write=True).command('worker_job.defer', {
            'task_id':fence.job_id,'worker_id':fence.worker_id,'fencing_token':fence.token,
            'now_ms':100400,'available_at_ms':109000,'replay_cursor':run.version,'observed_cancel_sequence':0,
        }, None)
        assert deferred['ok'] is True
    return run, fence, step


def test_queued_cancel_is_atomic_and_preserves_first_command(semantic_authority):
    repo,run,factory = setup_run(semantic_authority)
    cancelled = repo.cancel(run.id,request_id='first',reason='owner stop',now=101)
    assert cancelled.value['status'] == 'cancelled'
    assert cancelled.value['steps']['show']['status'] == 'cancelled'
    assert job(factory,repo,run)['status'] == 'cancelled'
    assert job(factory,repo,run)['cancelSequence'] == 1
    assert active(factory, repo) == []
    assert repo.cancel(run.id,request_id='first',reason='owner stop',now=102).value == cancelled.value
    assert repo.cancel(run.id,request_id='later',reason='later stop',now=103).value == cancelled.value
    with pytest.raises(AutomationConflict):
        repo.cancel(run.id,request_id='first',reason='changed evidence',now=104)
    assert repo.get('run',run.id).version == cancelled.version


def test_claimed_no_effect_cannot_dispatch_after_owner_cancel(semantic_authority):
    repo,run,factory = setup_run(semantic_authority)
    fence = claim(factory,run)
    old = bind_fence(run.value,fence,now=100.1)
    cancelled = repo.cancel(run.id,request_id='first',reason='stop before effect',now=101)
    assert cancelled.value['status'] == 'cancelled'
    assert job(factory,repo,run)['status'] == 'running'
    assert job(factory,repo,run)['cancelSequence'] == 1
    with pytest.raises(AutomationConflict):
        repo.advance(run,old,fence,now=101.1)


def test_waiting_agent_is_woken_and_remains_cancelling_across_restart(semantic_authority):
    repo,run,factory = setup_run(semantic_authority,agent=True)
    run,fence,step = intent(repo,run,factory,waiting=True)
    cancelled = repo.cancel(run.id,request_id='first',reason='stop agent',now=101)
    assert cancelled.value['status'] == 'cancelling'
    assert cancelled.value['steps'][step]['status'] == 'waiting_agent'
    assert 'completedAt' not in cancelled.value
    waiting = job(factory,repo,run)
    assert waiting['status'] == 'suspended'
    assert waiting['availableAtMs'] == 101000
    assert waiting['terminalAtMs'] == 0
    assert len(active(factory, repo)) == 1
    engine = semantic_authority[2]
    if engine is not None:
        engine.crash_cycle()
    else:
        from lib.storage.service import storage_runtime
        runtime = storage_runtime()
        runtime.stop()
        runtime.start()
    assert repo.get('run',run.id).value == cancelled.value
    assert job(factory,repo,run) == waiting
    assert repo.cancel(run.id,request_id='retry',reason='retry after restart',now=102).value == cancelled.value
    assert job(factory,repo,run)['cancelSequence'] == 1


def test_effect_settlement_preserves_cancel_command_and_failure_evidence(semantic_authority):
    repo,run,factory = setup_run(semantic_authority,agent=True)
    run,fence,step = intent(repo,run,factory)
    cancelled = repo.cancel(run.id,request_id='first',reason='stop effect',now=101)
    pending = record_failure(cancelled.value,step,{'code':'unknown','message':'External outcome unknown'},
        fence,now=101.1,uncertain=True)
    assert pending['status'] == 'cancelling'
    pending = repo.advance(cancelled,pending,fence,now=101.1)
    settled = record_failure(pending.value,step,{'code':'cancel_ack','message':'External task settled'},fence,now=101.2)
    assert settled['status'] == 'cancelled'
    assert settled['steps'][step]['status'] == 'failed'
    assert settled['steps'][step]['error']['code'] == 'cancel_ack'
    final = repo.advance(pending,settled,fence,now=101.2)
    assert final.value['cancelCommand'] == cancelled.value['cancelCommand']
    assert active(factory, repo) == []


def test_worker_cannot_erase_or_relabel_owner_cancellation(semantic_authority):
    repo,run,factory = setup_run(semantic_authority,agent=True)
    run,fence,step = intent(repo,run,factory)
    cancelled = repo.cancel(run.id,request_id='first',reason='owner reason',now=101)
    assert request_cancel(cancelled.value,reason='deadline_exceeded',now=102) == cancelled.value
    for field in ('cancelCommand','cancelReason'):
        changed = deepcopy(cancelled.value)
        changed.pop(field)
        with pytest.raises(StorageError) as error:
            repo.advance(cancelled,changed,fence,now=101.1)
        assert error.value.code == 'database_protocol_error'


def test_owner_cancel_cannot_be_failed_or_terminal_with_unknown_effect(semantic_authority):
    repo,run,factory = setup_run(semantic_authority,agent=True)
    run,fence,step = intent(repo,run,factory)
    cancelled = repo.cancel(run.id,request_id='first',reason='owner stop',now=101)
    for status,code in [('failed','database_conflict'),('cancelled','database_protocol_error')]:
        changed = deepcopy(cancelled.value)
        changed.update(status=status,completedAt=101.1)
        with pytest.raises((StorageError,AutomationConflict)) as error:
            repo.advance(cancelled,changed,fence,now=101.1)
        if code == 'database_conflict':
            assert isinstance(error.value,AutomationConflict)
        else:
            assert error.value.code == code
        assert repo.get('run',run.id).value == cancelled.value
        assert job(factory,repo,run)['cancelSequence'] == 1


def test_cancel_after_run_success_leaves_worker_completion_untouched(semantic_authority):
    repo,run,factory = setup_run(semantic_authority)
    run,fence,step = intent(repo,run,factory)
    output = record_output(run.value,step,{'startedAt':100,'timezone':'UTC','text':'time'},fence,now=100.3)
    run = repo.advance(run,output,fence,now=100.3)
    settled,_ = prepare_step(run.value,fence,now=100.4)
    final = repo.advance(run,settled,fence,now=100.4)
    before = job(factory,repo,run)
    assert repo.cancel(run.id,request_id='late',reason='late owner stop',now=101).value == final.value
    assert job(factory,repo,run) == before
    assert before['cancelSequence'] == 0
    assert factory(write=True).command('worker_job.complete', {
        'task_id':fence.job_id,'worker_id':fence.worker_id,'fencing_token':fence.token,
        'now_ms':101000,'terminal_status':'succeeded',
    },'cancel-test-complete-'+uuid.uuid4().hex)['ok'] is True


def test_concurrent_cancel_has_one_durable_command(semantic_authority):
    repo,run,factory = setup_run(semantic_authority)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda i:repo.cancel(run.id,request_id=f'command-{i}',reason='owner stop',now=101),range(4)))
    assert len({row.value['cancelCommand']['id'] for row in rows}) == 1
    assert len({row.version for row in rows}) == 1
    assert job(factory,repo,run)['cancelSequence'] == 1


@pytest.mark.parametrize('committed',[True,False])
def test_lost_ack_only_succeeds_with_authoritative_cancel_readback(semantic_authority,committed):
    repo,run,factory = setup_run(semantic_authority)
    calls = []

    class UnknownAck:
        def query(self,*args,**kwargs):
            return factory().query(*args,**kwargs)

        def command(self,*args,**kwargs):
            calls.append(args[0])
            if committed:
                factory(write=True).command(*args,**kwargs)
            raise StorageError('database_timeout','Synthetic lost acknowledgement')

    uncertain = AutomationExecutionRepository(repo.principal,client_factory=lambda **kwargs:UnknownAck())
    if committed:
        assert uncertain.cancel(run.id,request_id='first',reason='owner stop',now=101).value['status'] == 'cancelled'
    else:
        with pytest.raises(StorageError):
            uncertain.cancel(run.id,request_id='first',reason='owner stop',now=101)
        assert repo.get('run',run.id).value == run.value
    assert calls == ['automation.run.cancel']


def test_cancel_requires_management_scope_and_owned_run(semantic_authority):
    repo,run,factory = setup_run(semantic_authority)
    reader = PrincipalContext.user(subject_id='reader',owner_user_id=repo.owner_id,
        tenant_id='test-tenant',scopes={'automations:read'})
    with pytest.raises(PermissionError):
        AutomationExecutionRepository(reader,client_factory=factory).cancel(run.id,request_id='no',reason='no',now=101)
    other = PrincipalContext.user(subject_id='other-tenant',owner_user_id=repo.owner_id,
        tenant_id='other-tenant',scopes={'automations:manage'})
    with pytest.raises(AutomationError) as error:
        AutomationExecutionRepository(other,client_factory=factory).cancel(run.id,request_id='no',reason='no',now=101)
    assert error.value.code == 'automation_not_found'
    assert repo.get('run',run.id).value == run.value


def test_cancellation_rejects_malformed_evidence_without_mutation(semantic_authority):
    repo,run,factory = setup_run(semantic_authority)
    payload = {'user_id':repo.owner_id,'tenant_id':'test-tenant','run_key':repo._key(run.id),
        'now_ms':101000,'request_id':'first','reason':'owner stop'}
    for field,value in [('request_id','first\n'),('reason','bad\x00reason'),('reason','x'*201),
                        ('now_ms',True),('user_id',float(repo.owner_id)),('extra','unexpected')]:
        changed = {**payload,field:value}
        with pytest.raises(StorageError) as error:
            factory(write=True).command('automation.run.cancel',changed,None)
        assert error.value.code == 'database_protocol_error'
    assert repo.get('run',run.id).value == run.value
    assert job(factory,repo,run)['cancelSequence'] == 0
