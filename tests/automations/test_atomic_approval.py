"""Owned approval, wake ordering, rollback and immutable external intent."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import time
import uuid
import pytest

from lib.automations.approval import digest, validate_approved_dispatch
from lib.automations.definitions import prepare_definition
from lib.automations.execution import ActionAdapter, AutomationExecutionHandler, ExecutionRegistry
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.automations.errors import AutomationConflict
from lib.automations.registry import builtin_registry
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from lib.durable_worker import StorageWorkerJobStore, SynchronousWorkerRunner
from lib.automations.transitions import ExecutionFence, bind_fence, prepare_step
from tests.automations.test_atomic_execution import setup_rule
from tests.automations.test_execution_handler import job
from tests.support.worker_job_time import wait_until_ms, wall_ms

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def worker(factory, *, registry, store_type=StorageWorkerJobStore, lease_ms=10000):
    handler = AutomationExecutionHandler(client_factory=factory, registry=registry,
        now_ms=wall_ms, poll_interval_ms=1000)
    return SynchronousWorkerRunner(worker_id='approval-'+uuid.uuid4().hex,
        store=store_type(client_factory=factory), registrations=[handler.registration()],
        now_ms=wall_ms, lease_ms=lease_ms, heartbeat_interval_ms=min(1000, lease_ms//3))


def pending_approval(authority, *, store_type=None, lease_ms=10000,
                     action='skills.activate', arguments=None):
    initial, definition, event, now, factory = setup_rule(authority)
    repo = AutomationExecutionRepository(PrincipalContext.user(subject_id='reviewer',
        owner_user_id=initial.owner_id, tenant_id=initial.principal.tenant_id,
        scopes=set(initial.principal.scopes) | set(builtin_registry().get(action).required_scopes)), client_factory=factory)
    rule = deepcopy(definition.value['definition'])
    rule['policy'].update(permissionMode='manual', maxRuntimeSeconds=120, maxModelTokens=1000)
    rule['steps'] = [{'id':'write','action':action,'after':[],
                     'arguments':arguments if arguments is not None else {'snapshotId':'test-snapshot'}}]
    published = repo.publish(prepare_definition(rule), expected_version=definition.version, now=now)
    event['id'] += '-approval'
    run = repo.admit(published.id, event, now=now)
    calls = []
    proof = {'activated':True,'snapshotId':'test-snapshot','revisions':[
        {'skillId':'test-skill','version':'1','sourceId':'test-owned','digest':'a'*64}]}
    def execute(invocation):
        calls.append(invocation.effect_key)
        return deepcopy(proof)
    registry = ExecutionRegistry([ActionAdapter(builtin_registry().get(action), execute, execute)])
    runner = worker(factory, registry=registry, lease_ms=lease_ms,
                    **({'store_type':store_type} if store_type else {}))
    return factory, repo, run, runner, calls


def approve(repo, waiting, request='approve-once', **changes):
    return repo.approve(waiting.id, step_id='write', expected_version=waiting.version,
                        request_id=request, now=time.time(), **changes)


def test_approval_wakes_suspended_job_and_preserves_one_exact_command(semantic_authority):
    factory, repo, run, runner, calls = pending_approval(semantic_authority)
    assert runner.run_once().state == 'suspended'
    waiting = repo.get('run', run.id)
    assert waiting.value['steps']['write']['arguments'] == {'snapshotId':'test-snapshot'}
    old = job(factory, repo, run)
    assert old['status'] == 'suspended' and old['availableAtMs'] > wall_ms()
    accepted = approve(repo, waiting)
    wake = job(factory, repo, run)
    assert accepted.value['steps']['write']['status'] == 'pending'
    assert wake['wakeSequence'] == 1 and wake['cancelSequence'] == 0
    assert wake['availableAtMs'] <= wall_ms()
    assert runner.run_once().state == 'succeeded'
    settled = repo.get('run', run.id)
    assert settled.value['status'] == 'succeeded'
    assert calls == [settled.value['steps']['write']['effectKey']]
    replay = approve(repo, waiting)
    assert replay.value == settled.value
    assert job(factory, repo, run)['wakeSequence'] == 1
    with pytest.raises(AutomationConflict):
        approve(repo, waiting, request='changed-command')
    if semantic_authority[2] is not None:
        semantic_authority[2].crash_cycle()
        assert approve(repo, waiting).value == settled.value


def test_approval_before_deferral_cannot_be_overwritten_or_acknowledged_by_heartbeat(semantic_authority):
    from lib.durable_worker import StorageWorkerJobStore
    entered, release = threading.Event(), threading.Event()
    class Store(StorageWorkerJobStore):
        def defer(self, claim, **kwargs):
            entered.set()
            assert release.wait(5)
            self.heartbeat(claim, now_ms=wall_ms(), lease_ms=10000,
                           replay_cursor=kwargs['outcome'].replay_cursor)
            return super().defer(claim, **kwargs)
    factory, repo, run, runner, calls = pending_approval(semantic_authority, store_type=Store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.run_once)
        try:
            assert entered.wait(5)
            waiting = repo.get('run', run.id)
            assert job(factory, repo, run)['status'] == 'running'
            approve(repo, waiting)
            assert calls == []
        finally:
            release.set()
        assert future.result(timeout=5).state == 'suspended'
    woken = job(factory, repo, run)
    assert woken['availableAtMs'] <= wall_ms() and woken['wakeSequence'] == 1
    assert runner.run_once().state == 'succeeded'
    assert len(calls) == 1


def test_lost_approval_ack_proves_the_original_request_without_another_wake(semantic_authority):
    factory, repo, run, runner, calls = pending_approval(semantic_authority)
    assert runner.run_once().state == 'suspended'
    waiting = repo.get('run', run.id)
    class Client:
        def query(self,*a,**k):return factory().query(*a,**k)
        def command(self,name,payload,receipt):
            result = factory(write=True).command(name,payload,receipt)
            if name == 'automation.run.approve':
                raise StorageError('database_timeout','Injected lost approval acknowledgement')
            return result
    lost = AutomationExecutionRepository(repo.principal, client_factory=lambda **_:Client())
    confirmed = approve(lost, waiting)
    assert confirmed.value['steps']['write']['approval']['requestId'] == 'approve-once'
    assert job(factory, repo, run)['wakeSequence'] == 1
    assert calls == []
    assert runner.run_once().state == 'succeeded'


def test_stale_revision_and_foreign_owner_never_wake_or_approve(semantic_authority):
    factory, repo, run, runner, _ = pending_approval(semantic_authority)
    assert runner.run_once().state == 'suspended'
    waiting = repo.get('run', run.id)
    with pytest.raises(AutomationConflict):
        repo.approve(run.id, step_id='write', expected_version=waiting.version-1,
                     request_id='stale', now=time.time())
    foreign = AutomationExecutionRepository(PrincipalContext.user(subject_id='foreign',
        owner_user_id=repo.owner_id+1, tenant_id=repo.principal.tenant_id,
        scopes=repo.principal.scopes), client_factory=factory)
    with pytest.raises(ValueError):
        approve(foreign, waiting)
    assert repo.get('run', run.id).value == waiting.value
    assert job(factory, repo, run)['wakeSequence'] == 0


def live_wait(factory, repo, run):
    doc = StorageWorkerJobStore(client_factory=factory).claim_next(worker_id='approval-proof',
        task_kinds=('automation-run',), now_ms=wall_ms(), lease_ms=10000)
    assert doc['taskId'] == run.value['jobId']
    fence = ExecutionFence(doc['taskId'], doc['claimOwner'], doc['fencingToken'])
    bound = repo.advance(run, bind_fence(run.value, fence, now=time.time()), fence, now=time.time())
    waiting, selected = prepare_step(bound.value, fence, now=time.time())
    assert selected is None and waiting['status'] == 'waiting_permission'
    return repo.advance(bound, waiting, fence, now=time.time()), fence


def test_live_worker_cannot_forge_approval_or_dispatch_without_it(semantic_authority):
    factory, repo, run, _, calls = pending_approval(semantic_authority)
    waiting, fence = live_wait(factory, repo, run)
    for forged in (True, False, None):
        changed = deepcopy(waiting.value)
        state = changed['steps']['write']
        state['status'] = 'dispatching'
        state['attempts'] = 1
        state['dispatchedAt'] = time.time()
        changed['status'] = 'running'
        if forged:
            state['approval'] = {'requestId':'forged','requestDigest':'a'*64,
                'actorSubjectId':'worker','approvedAt':time.time(),'argumentsDigest':'b'*64,
                'actionDigest':'c'*64,'effectKey':state['effectKey']}
        elif forged is None:
            state['status'] = 'waiting_permission'
            changed['status'] = 'waiting_permission'
            state['approval'] = None
        with pytest.raises(StorageError) as exc:
            repo.advance(waiting, changed, fence, now=time.time())
        assert exc.value.code == 'database_protocol_error'
        assert repo.get('run', run.id).value == waiting.value
    assert calls == [] and job(factory, repo, run)['wakeSequence'] == 0


def test_approved_arguments_evidence_and_effect_identity_are_immutable(semantic_authority):
    factory, repo, run, _, _ = pending_approval(semantic_authority)
    waiting, fence = live_wait(factory, repo, run)
    accepted = approve(repo, waiting)
    for field, value in [('arguments', {'snapshotId':'other-snapshot'}),
                         ('effectKey', 'other-effect'), ('approval', None)]:
        changed = deepcopy(accepted.value)
        if value is None:
            changed['steps']['write'].pop(field)
        else:
            changed['steps']['write'][field] = value
        with pytest.raises(StorageError) as exc:
            repo.advance(accepted, changed, fence, now=time.time())
        assert exc.value.code == 'database_protocol_error'
        assert repo.get('run', run.id).value == accepted.value
    assert job(factory, repo, run)['wakeSequence'] == 1


def test_cancelled_job_rolls_back_approval_after_wake_mutation(semantic_authority):
    factory, repo, run, runner, _ = pending_approval(semantic_authority)
    assert runner.run_once().state == 'suspended'
    waiting = repo.get('run', run.id)
    factory(write=True).command('worker_job.request_cancel', {
        'task_id':run.value['jobId'],'user_id':repo.owner_id,'now_ms':wall_ms(),'reason':'fault boundary'},'cancel-fault:'+uuid.uuid4().hex)
    cancelled_job = job(factory, repo, run)
    assert cancelled_job['cancelSequence'] == 1 and cancelled_job['wakeSequence'] == 0
    with pytest.raises(AutomationConflict):
        approve(repo, waiting)
    assert repo.get('run', run.id).value == waiting.value
    assert job(factory, repo, run) == cancelled_job
    engine = semantic_authority[2]
    if engine is not None:
        engine.crash_cycle()
        assert repo.get('run', run.id).value == waiting.value
        assert job(factory, repo, run) == cancelled_job


def test_concurrent_exact_approval_and_restart_commit_one_wake(semantic_authority):
    factory, repo, run, runner, calls = pending_approval(semantic_authority)
    assert runner.run_once().state == 'suspended'
    waiting = repo.get('run', run.id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: approve(repo, waiting), range(4)))
    assert all(row.value == rows[0].value for row in rows)
    assert job(factory, repo, run)['wakeSequence'] == 1
    if semantic_authority[2] is not None:
        semantic_authority[2].crash_cycle()
        assert repo.get('run', run.id).value == rows[0].value
        assert job(factory, repo, run)['wakeSequence'] == 1
    assert runner.run_once().state == 'succeeded'
    assert len(calls) == 1


def test_current_action_scope_and_tenant_are_required(semantic_authority):
    factory, repo, run, runner, _ = pending_approval(semantic_authority)
    assert runner.run_once().state == 'suspended'
    waiting = repo.get('run', run.id)
    denied = AutomationExecutionRepository(PrincipalContext.user(subject_id='denied',
        owner_user_id=repo.owner_id, tenant_id=repo.principal.tenant_id,
        scopes=set(repo.principal.scopes)-{'skills:install'}),client_factory=factory)
    with pytest.raises(PermissionError):
        approve(denied,waiting)
    foreign = AutomationExecutionRepository(PrincipalContext.user(subject_id='other-tenant',
        owner_user_id=repo.owner_id,tenant_id='other',scopes=repo.principal.scopes),client_factory=factory)
    with pytest.raises(ValueError):
        approve(foreign,waiting)
    assert repo.get('run',run.id).value == waiting.value
    assert job(factory,repo,run)['wakeSequence'] == 0


def test_reviewed_json_bytes_survive_float_and_unicode_round_trips(semantic_authority):
    args = {'definitionId':'test-flow','inputs':{'small':1e-9,'negativeZero':-0.0,
                                              '\U0001f642':2.0,'\ue000':'中文'}}
    factory,repo,run,runner,calls = pending_approval(semantic_authority,action='flow.run',arguments=args)
    assert runner.run_once().state == 'suspended'
    waiting = repo.get('run',run.id)
    accepted = approve(repo,waiting)
    state = accepted.value['steps']['write']
    # Review the durable frozen representation: PostgreSQL JSONB normalizes -0.0.
    frozen = waiting.value['steps']['write']['arguments']
    assert frozen == args
    assert state['approval']['argumentsDigest'] == digest(frozen)
    validate_approved_dispatch(state,accepted.value['actions']['flow.run'],frozen)
    assert calls == []
    def execute(invocation):
        assert invocation.arguments == frozen
        calls.append(invocation.effect_key)
        return {'runId':'synthetic-flow','conversationId':'synthetic-conversation',
                'status':'done','resultRef':'synthetic-result'}
    normal = worker(factory,registry=ExecutionRegistry([ActionAdapter(
        builtin_registry().get('flow.run'),execute,execute)]))
    assert normal.run_once().state == 'succeeded'
    assert len(calls)==1


def test_progress_cas_race_suspends_and_reclaims_instead_of_failing(semantic_authority):
    class AbandonedStore(StorageWorkerJobStore):
        def defer(self,*args,**kwargs):
            raise StorageError('database_unavailable','Injected death before defer')
    factory,repo,run,runner,calls = pending_approval(semantic_authority,store_type=AbandonedStore)
    assert runner.run_once().state == 'suspension_unconfirmed'
    waiting = repo.get('run',run.id)
    if semantic_authority[2] is not None:
        semantic_authority[2].crash_cycle()
    else:
        wait_until_ms(job(factory,repo,run)['leaseDeadlineMs'])
    entered,release = threading.Event(),threading.Event()
    class Client:
        def query(self,*args,**kwargs):return factory().query(*args,**kwargs)
        def command(self,name,payload,receipt):
            if name=='automation.run.advance':
                entered.set()
                assert release.wait(5)
            return factory(write=True).command(name,payload,receipt)
    raced = worker(lambda **_:Client(),registry=runner._registrations['automation-run'].handler._registry)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(raced.run_once)
        try:
            assert entered.wait(5)
            approve(repo,waiting)
        finally:
            release.set()
        assert future.result(timeout=5).state == 'suspended'
    assert job(factory,repo,run)['availableAtMs'] <= wall_ms()
    # Use the original port but a normal store after the failed CAS.
    normal = worker(factory,registry=runner._registrations['automation-run'].handler._registry)
    assert normal.run_once().state == 'succeeded'
    assert len(calls)==1


def test_approval_never_resets_unknown_write_to_dispatchable(semantic_authority):
    factory,repo,run,runner,calls = pending_approval(semantic_authority)
    assert runner.run_once().state == 'suspended'
    waiting = repo.get('run',run.id)
    approve(repo,waiting)
    contract = builtin_registry().get('skills.activate')
    seen, reconciled = [],[]
    settled = {'ready':False}
    proof = {'activated':True,'snapshotId':'test-snapshot','revisions':[
        {'skillId':'test-skill','version':'1','sourceId':'test-owned','digest':'a'*64}]}
    def uncertain(invocation):
        seen.append(invocation.effect_key)
        raise RuntimeError('Injected unknown write result')
    def reconcile(invocation):
        reconciled.append(invocation.effect_key)
        return proof if settled['ready'] else None
    normal = worker(factory,registry=ExecutionRegistry([ActionAdapter(contract,uncertain,reconcile)]))
    assert normal.run_once().state == 'suspended'
    unknown = repo.get('run',run.id)
    assert unknown.value['status'] == 'reconciliation_required'
    assert approve(repo,waiting).value == unknown.value
    with pytest.raises(AutomationConflict):
        approve(repo,unknown,request='reset-unknown')
    assert job(factory,repo,run)['wakeSequence']==1
    settled['ready']=True
    wait_until_ms(job(factory,repo,run)['availableAtMs'])
    assert normal.run_once().state == 'succeeded'
    assert len(seen)==1 and seen==reconciled
    assert calls==[]


def test_storage_approval_rejects_noncanonical_or_broadened_actor_without_wake(semantic_authority):
    factory,repo,run,runner,_ = pending_approval(semantic_authority)
    assert runner.run_once().state=='suspended'
    waiting=repo.get('run',run.id)
    class Client:
        change={}
        def query(self,*args,**kwargs):return factory().query(*args,**kwargs)
        def command(self,name,payload,receipt):
            if name=='automation.run.approve':
                payload=deepcopy(payload)
                payload['actor_principal'].update(self.change)
            return factory(write=True).command(name,payload,receipt)
    client=Client()
    proxy=AutomationExecutionRepository(repo.principal,client_factory=lambda **_:client)
    for change in ({'owner_user_id':repo.owner_id+1},{'tenant_id':'other'},
                   {'kind':'admin'},{'scopes':['admin']},{'subject_id':' padded '},
                   {'owner_user_id':True},{'unexpected':True}):
        client.change=change
        with pytest.raises(StorageError) as exc:
            approve(proxy,waiting)
        assert exc.value.code=='database_protocol_error'
        assert repo.get('run',run.id).value==waiting.value
        assert job(factory,repo,run)['wakeSequence']==0


@pytest.mark.serial
def test_owned_approval_uses_selected_native_embedded_pair_and_survives_restart(tmp_path, monkeypatch):
    import os
    from lib.storage import StorageRuntime,StorageSupervisor
    binary=os.environ.get('TOFUDB_BIN')
    if not binary:
        pytest.skip('Set TOFUDB_BIN to the source-matched pair')
    monkeypatch.setenv('TOFUDB_OWNER_ID','41')
    monkeypatch.setenv('TOFUDB_TENANT_ID','1')
    monkeypatch.setenv('TOFUDB_TRANSPORT','embedded')
    runtime=StorageRuntime(StorageSupervisor(project_root=tmp_path,backend='tofudb'),auto_restart=False)
    factory=lambda **options:runtime.client(write=options.get('write',False))
    try:
        runtime.start()
        factory,repo,run,runner,calls=pending_approval((factory,41,None))
        assert runner.run_once().state=='suspended'
        waiting=repo.get('run',run.id)
        accepted=approve(repo,waiting)
        runtime.stop()
        runtime.start()
        assert repo.get('run',run.id).value==accepted.value
        assert job(factory,repo,run)['wakeSequence']==1
        assert runner.run_once().state=='succeeded'
        assert len(calls)==1
        assert approve(repo,waiting).value['status']=='succeeded'
    finally:
        runtime.stop()


def test_pending_unreviewed_step_cannot_be_approved(semantic_authority):
    factory,repo,run,_,calls=pending_approval(semantic_authority)
    with pytest.raises(AutomationConflict):
        approve(repo,run)
    assert repo.get('run',run.id).value==run.value
    assert job(factory,repo,run)['wakeSequence']==0 and calls==[]


def test_worker_cannot_reset_external_intent_or_add_an_approved_step(semantic_authority):
    factory,repo,run,_,calls=pending_approval(semantic_authority)
    waiting,fence=live_wait(factory,repo,run)
    accepted=approve(repo,waiting)
    prepared,selected=prepare_step(accepted.value,fence,now=time.time())
    assert selected=='write'
    dispatched=repo.advance(accepted,prepared,fence,now=time.time())
    for status in ('pending','waiting_permission','skipped'):
        changed=deepcopy(dispatched.value)
        changed['steps']['write']['status']=status
        with pytest.raises(StorageError) as exc:
            repo.advance(dispatched,changed,fence,now=time.time())
        assert exc.value.code=='database_protocol_error'
    extra=deepcopy(dispatched.value)
    extra['steps']['extra']=deepcopy(extra['steps']['write'])
    with pytest.raises(StorageError) as exc:
        repo.advance(dispatched,extra,fence,now=time.time())
    assert exc.value.code=='database_protocol_error'
    assert repo.get('run',run.id).value==dispatched.value
    assert calls==[] and job(factory,repo,run)['wakeSequence']==1
