"""Real queue consumption, bounded demand growth and native shutdown recovery."""
import threading
import time
import uuid
import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.execution import ActionAdapter, ExecutionRegistry
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.automations.presentation import show_task_start_time
from lib.automations.registry import builtin_registry
from lib.automations.runtime import create_service
from lib.identity import PrincipalContext
from tests.automations.test_atomic_publication import setup, get_job
from tests.automations.test_task_started_events import pending, context, start

pytestmark = pytest.mark.api
pytest_plugins = ('tests.support.semantic_authorities',)


def wait_until(predicate, *, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('Automation service did not reach the expected durable state')


def admit(authority, *, name=None):
    factory, repo, rule = setup(authority)
    if name:
        rule['id'] = name
    now = time.time()
    definition = repo.publish(prepare_definition(rule), expected_version=0, now=now)
    started = now - 5
    event = {'id': 'start-'+uuid.uuid4().hex, 'kind': 'task.started', 'occurredAt': started,
             'origin': 'human', 'evidence': {'startedAt': started}, 'automationChain': []}
    run = repo.admit(definition.id, event, now=now)
    return factory, repo, run


def terminal(repo, entity):
    run = repo.get('run', entity.id)
    return run if run.value['status'] in {'succeeded', 'failed', 'cancelled'} else None


def test_application_consumes_actual_entry_event_and_settles_its_run(semantic_authority, monkeypatch):
    factory, repo, rule = setup(semantic_authority)
    repo.publish(prepare_definition(rule), expected_version=0, now=time.time())
    _, owner, created, attempt, task = pending(semantic_authority, monkeypatch)
    submitted_before = time.time()
    time.sleep(.025)
    actual = start(factory, owner, attempt, task, context())
    service = create_service(client_factory=factory, max_workers=2)
    try:
        assert service.start() and not service.start()
        def settled():
            runs = repo.page('run', limit=8)[0]
            return runs[0] if len(runs) == 1 and runs[0].value['status'] == 'succeeded' else None
        run = wait_until(settled)
        state = run.value
        assert state['trigger']['evidence']['taskId'] == task
        assert state['trigger']['evidence']['conversationId'] == created['turn']['conversationId']
        assert state['steps']['show']['output']['startedAt'] == actual['startedAt'] / 1000
        assert state['steps']['show']['output']['startedAt'] > submitted_before
        assert 'Asia/Shanghai' in state['steps']['show']['output']['text']
        assert state['principal']['scopes'] == ['automations:execute', 'automations:read']
        wait_until(lambda: get_job(factory, repo, state['jobId'])['status'] == 'succeeded')
        assert service.stop(timeout=5)
        assert service.snapshot()['state'] == 'stopped'
        assert not any(t.name.startswith('durable-worker-heartbeat:') for t in threading.enumerate())
        if semantic_authority[2] is not None:
            semantic_authority[2].crash_cycle()
            assert repo.get('run', run.id).value == state
    finally:
        service.stop()


def test_shutdown_awaits_unknown_write_then_reconciles_without_redispatch(semantic_authority):
    factory, original, rule = setup(semantic_authority)
    repo = AutomationExecutionRepository(PrincipalContext.user(subject_id='test-publisher',
        owner_user_id=original.owner_id, tenant_id=original.principal.tenant_id,
        scopes=set(original.principal.scopes) | {'skills:install'}), client_factory=factory)
    rule['policy']['maxRuntimeSeconds'] = 120
    rule['steps'] = [{'id': 'write', 'action': 'skills.activate',
                     'arguments': {'snapshotId': 'test-snapshot'}, 'after': []}]
    definition = repo.publish(prepare_definition(rule), expected_version=0, now=time.time())
    stamp = time.time()
    run = repo.admit(definition.id, {'id': 'write-'+uuid.uuid4().hex,
        'kind': 'task.started', 'occurredAt': stamp, 'origin': 'human',
        'evidence': {'startedAt': stamp}, 'automationChain': []}, now=stamp)
    entered, release = threading.Event(), threading.Event()
    calls, reconciled = [], []
    proof = {'activated': True, 'snapshotId': 'test-snapshot', 'revisions': [
        {'skillId': 'test-skill', 'version': '1', 'sourceId': 'test-owned', 'digest': 'a'*64}]}
    def execute(invocation):
        calls.append(invocation.effect_key)
        entered.set()
        assert release.wait(5)
        invocation.context.checkpoint()
        return proof
    def reconcile(invocation):
        reconciled.append(invocation.effect_key)
        return proof
    registry = ExecutionRegistry([ActionAdapter(builtin_registry().get('skills.activate'),
                                                execute, reconcile)])
    service = create_service(client_factory=factory, registry=registry, max_workers=1)
    try:
        service.start(); assert entered.wait(5)
        assert not service.stop(timeout=.05)
        assert get_job(factory, repo, run.value['jobId'])['status'] == 'running'
        release.set()
        assert service.stop(timeout=5)
        assert get_job(factory, repo, run.value['jobId'])['status'] == 'suspended'
        resumed = create_service(client_factory=factory, registry=registry, max_workers=1)
        try:
            resumed.start()
            settled = wait_until(lambda: terminal(repo, run))
            assert settled.value['status'] == 'succeeded'
            assert settled.value['steps']['write']['output'] == proof
            assert len(calls) == len(reconciled) == 1
            assert calls == reconciled == [settled.value['steps']['write']['effectKey']]
        finally:
            resumed.stop()
    finally:
        release.set(); service.stop()


def test_demand_growth_is_bounded_and_commands_leave_no_poll_receipts(semantic_authority):
    factory, repo, first = admit(semantic_authority, name='first-rule')
    _, _, second = admit(semantic_authority, name='second-rule')
    release = threading.Event()
    entered, command_ids = set(), []
    lock = threading.Lock()
    def execute(invocation):
        with lock:
            entered.add(invocation.effect_key)
        assert release.wait(5)
        invocation.context.checkpoint()
        return show_task_start_time(invocation.arguments, invocation.event)
    class Client:
        def query(self, *args, **kwargs):
            return factory().query(*args, **kwargs)
        def command(self, name, payload, receipt):
            with lock:
                command_ids.append((name, receipt))
            return factory(write=True).command(name, payload, receipt)
    registry = ExecutionRegistry([ActionAdapter(builtin_registry().get('task.show_time'), execute)])
    service = create_service(client_factory=lambda **_: Client(), registry=registry, max_workers=2)
    try:
        service.start()
        wait_until(lambda: len(entered) == 2)
        assert service.snapshot()['highWater'] == 2
        release.set()
        assert wait_until(lambda: terminal(repo, first)).value['status'] == 'succeeded'
        assert wait_until(lambda: terminal(repo, second)).value['status'] == 'succeeded'
        assert service.stop(timeout=5)
        assert any(name == 'worker_job.claim_next' for name, _ in command_ids)
        assert all(receipt is None for name, receipt in command_ids if name == 'worker_job.claim_next')
        assert all(receipt for name, receipt in command_ids if name == 'worker_job.complete')
        assert service.snapshot()['highWater'] <= service.max_workers
    finally:
        release.set(); service.stop()


def test_idle_service_does_not_preallocate_the_capacity(semantic_authority):
    factory, _, _ = semantic_authority
    before = {thread.ident for thread in threading.enumerate()}
    service = create_service(client_factory=factory, max_workers=97)
    try:
        service.start()
        wait_until(lambda: service.snapshot()['highWater'] == 1)
        time.sleep(.3)
        threads = [thread for thread in threading.enumerate()
                   if thread.ident not in before and thread.name.startswith('tofu-automation')]
        assert len(threads) == 2
        assert service.snapshot()['highWater'] == 1
        assert service.stop(timeout=5)
        assert not any(thread.is_alive() for thread in threads)
    finally:
        service.stop()


def test_shutdown_releases_local_dispatch_without_faking_cancellation(semantic_authority):
    factory, repo, run = admit(semantic_authority)
    entered = threading.Event()
    def execute(invocation):
        entered.set()
        while not invocation.context.authority_lost:
            time.sleep(.01)
        invocation.context.checkpoint()
    registry = ExecutionRegistry([ActionAdapter(builtin_registry().get('task.show_time'), execute)])
    service = create_service(client_factory=factory, registry=registry, max_workers=1)
    try:
        service.start(); assert entered.wait(5)
        assert service.stop(timeout=5)
        interrupted = repo.get('run', run.id)
        assert interrupted.value['status'] == 'running'
        assert interrupted.value['steps']['show']['status'] == 'dispatching'
        original_job = get_job(factory, repo, interrupted.value['jobId'])
        assert original_job['status'] == 'suspended' and original_job['cancelSequence'] == 0
        if semantic_authority[2] is not None:
            semantic_authority[2].crash_cycle()
        resumed = create_service(client_factory=factory, max_workers=1)
        try:
            resumed.start()
            settled = wait_until(lambda: terminal(repo, run))
            assert settled.value['status'] == 'succeeded'
            assert settled.value['steps']['show']['attempts'] == 2
            assert settled.value['steps']['show']['output']['startedAt'] == run.value['trigger']['occurredAt']
            wait_until(lambda: get_job(factory, repo, settled.value['jobId'])['status'] == 'succeeded')
            assert get_job(factory, repo, settled.value['jobId'])['fencingToken'] > original_job['fencingToken']
        finally:
            resumed.stop()
    finally:
        service.stop()


def test_shutdown_during_initial_proof_defers_before_handler_dispatch(semantic_authority):
    factory, repo, run = admit(semantic_authority)
    entered, release = threading.Event(), threading.Event()
    class Client:
        def query(self, name, payload, **kwargs):
            if name == 'worker_job.claim_state':
                entered.set()
                assert release.wait(5)
            return factory().query(name, payload, **kwargs)
        def command(self, *args, **kwargs):
            return factory(write=True).command(*args, **kwargs)
    def forbidden(invocation):
        raise AssertionError('Shutdown dispatched an action before initial proof exited')
    registry = ExecutionRegistry([ActionAdapter(builtin_registry().get('task.show_time'), forbidden)])
    service = create_service(client_factory=lambda **_: Client(), registry=registry, max_workers=1)
    try:
        service.start(); assert entered.wait(5)
        assert not service.stop(timeout=.05)
        release.set(); assert service.stop(timeout=5)
        saved = repo.get('run', run.id)
        assert saved.value['status'] == 'queued'
        assert saved.value['steps']['show']['status'] == 'pending'
        job = get_job(factory, repo, run.value['jobId'])
        assert job['status'] == 'suspended' and job['cancelSequence'] == 0
        resumed = create_service(client_factory=factory, max_workers=1)
        try:
            resumed.start()
            settled = wait_until(lambda: terminal(repo, run))
            assert settled.value['status'] == 'succeeded'
            assert settled.value['steps']['show']['attempts'] == 1
            wait_until(lambda: get_job(factory, repo, run.value['jobId'])['status'] == 'succeeded')
        finally:
            resumed.stop()
    finally:
        release.set(); service.stop()
