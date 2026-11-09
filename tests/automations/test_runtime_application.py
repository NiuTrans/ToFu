"""Real queue consumption, bounded demand growth and native shutdown recovery."""
import queue
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


def test_claimed_jobs_start_beyond_the_idle_hint_and_leave_no_poll_receipts(semantic_authority):
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
        def worker_watch(self, **kwargs):
            return factory().worker_watch(**kwargs)
        def query(self, *args, **kwargs):
            return factory().query(*args, **kwargs)
        def command(self, name, payload, receipt):
            with lock:
                command_ids.append((name, receipt))
            return factory(write=True).command(name, payload, receipt)
    registry = ExecutionRegistry([ActionAdapter(builtin_registry().get('task.show_time'), execute)])
    service = create_service(client_factory=lambda **_: Client(), registry=registry, max_workers=1)
    try:
        service.start()
        wait_until(lambda: len(entered) == 2)
        assert service.snapshot()['highWater'] >= 2
        release.set()
        assert wait_until(lambda: terminal(repo, first)).value['status'] == 'succeeded'
        assert wait_until(lambda: terminal(repo, second)).value['status'] == 'succeeded'
        assert service.stop(timeout=5)
        assert any(name == 'worker_job.claim_next' for name, _ in command_ids)
        claims = [receipt for name, receipt in command_ids if name == 'worker_job.claim_next']
        assert all(claims) and len(claims) == len(set(claims))
        assert all(receipt for name, receipt in command_ids if name == 'worker_job.complete')
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
                   if thread.ident not in before and thread.name.startswith('tofu-durable')]
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


class _CountingClient:
    """Records each completed coordinator storage call with its start wall time."""

    def __init__(self, factory):
        self._factory = factory
        self._lock = threading.Lock()
        self.calls = []

    def _record(self, kind, name, call):
        started = _wall_ms()
        answer = call()
        with self._lock:
            self.calls.append((kind, name, started))
        return answer

    def query(self, name, payload, **kwargs):
        return self._record('query', name, lambda: self._factory().query(name, payload, **kwargs))

    def command(self, name, payload, receipt, **kwargs):
        return self._record('command', name, lambda: self._factory(write=True).command(
            name, payload, receipt, **kwargs))

    def started(self, kind, name):
        with self._lock:
            return [started for call_kind, call_name, started in self.calls
                    if (call_kind, call_name) == (kind, name)]

    def count(self, kind, name):
        return len(self.started(kind, name))


def _wall_ms():
    return time.time_ns() // 1_000_000


def _exclusive_writes(factory):
    """Native exclusive-lane dispatches or SQLite writer jobs, when observable."""
    metrics = factory().metrics()
    if 'commitStages' in metrics:
        return next((stage['calls'] for stage in metrics['commitStages']
                     if stage['stage'] == 'command_execute'), 0)
    return (metrics.get('writer') or {}).get('submitted')


def _spec_service(client, kind, handler):
    from lib.automations.runtime_policy import runtime_policy
    from lib.automations.worker_service import AutomationWorkerService
    from lib.durable_worker import WorkerKindRegistration, WorkerKindSafety
    control = PrincipalContext.system(subject_id='automation-worker:claim-wake-spec',
                                      scopes={runtime_policy()['controlScope']})
    return AutomationWorkerService(principal=control, client_factory=lambda **_: client,
        registrations=(WorkerKindRegistration(kind, handler, WorkerKindSafety(True, True, True, True)),),
        max_workers=1, now_ms=lambda: int(time.time() * 1000))


def _enqueue(factory, owner, kind, *, available_at_ms=None):
    task = f'{kind}:{uuid.uuid4().hex}'
    principal = PrincipalContext.user(subject_id='claim-wake-spec', owner_user_id=owner)
    payload = {'task_id': task, 'user_id': owner, 'tenant_id': '', 'task_kind': kind,
               'idempotency_key': task, 'now_ms': int(time.time() * 1000),
               'payload': {'contract': 'claim-wake-spec/v1', 'principal': principal.to_payload()}}
    if available_at_ms is not None:
        payload['available_at_ms'] = available_at_ms
    factory(write=True).command('worker_job.enqueue', payload, 'claim-wake-spec:' + task)
    return task


def _recording_handler(claimed):
    from lib.durable_worker import WorkerJobOutcome
    def handler(context, payload):
        claimed.put((context.claim.task_id, _wall_ms()))
        return WorkerJobOutcome('succeeded')
    return handler


def _spec_kind():
    return 'claim-wake-spec-' + uuid.uuid4().hex[:12]


def test_idle_coordinator_takes_no_exclusive_writer_claim(semantic_authority):
    factory, _, _ = semantic_authority
    client, kind = _CountingClient(factory), _spec_kind()
    def forbidden(context, payload):
        raise AssertionError('An empty queue dispatched a handler')
    service = _spec_service(client, kind, forbidden)
    try:
        service.start()
        wait_until(lambda: client.count('query', 'worker_job.claim_horizon') == 1)
        writes = _exclusive_writes(factory)
        # The retired idle poll claimed through the writer every 250 ms.
        time.sleep(1.5)
        assert client.count('command', 'worker_job.claim_next') == 0
        assert client.count('query', 'worker_job.claim_horizon') == 1
        assert _exclusive_writes(factory) == writes
    finally:
        assert service.stop(timeout=5)


def test_in_process_enqueue_wakes_an_idle_coordinator(semantic_authority):
    factory, owner, _ = semantic_authority
    client, kind, claimed = _CountingClient(factory), _spec_kind(), queue.Queue()
    service = _spec_service(client, kind, _recording_handler(claimed))
    try:
        service.start()
        wait_until(lambda: client.count('query', 'worker_job.claim_horizon') == 1)
        time.sleep(.3)
        before = _wall_ms()
        task = _enqueue(factory, owner, kind)
        after = _wall_ms()
        assert claimed.get(timeout=15)[0] == task
        # An empty queue has no horizon: only the committed enqueue woke it.
        assert before <= client.started('query', 'worker_job.claim_horizon')[1] <= after + 2_000
        assert client.count('command', 'worker_job.claim_next') == 1
    finally:
        assert service.stop(timeout=5)


def test_delayed_job_is_claimed_at_its_available_time(semantic_authority):
    factory, owner, _ = semantic_authority
    client, kind, claimed = _CountingClient(factory), _spec_kind(), queue.Queue()
    service = _spec_service(client, kind, _recording_handler(claimed))
    try:
        service.start()
        wait_until(lambda: client.count('query', 'worker_job.claim_horizon') == 1)
        due = _wall_ms() + 1_500
        task = _enqueue(factory, owner, kind, available_at_ms=due)
        task_id, claimed_at = claimed.get(timeout=15)
        assert task_id == task and claimed_at >= due
        # One exclusive claim, issued when the job became claimable. The bound
        # covers the wake and one read probe, not the writer's commit latency.
        claims = client.started('command', 'worker_job.claim_next')
        assert len(claims) == 1 and due <= claims[0] <= due + 2_000
    finally:
        assert service.stop(timeout=5)


def test_enqueue_racing_an_empty_probe_is_not_lost(semantic_authority):
    factory, owner, _ = semantic_authority
    kind, claimed, raced, answers = _spec_kind(), queue.Queue(), [], []
    class RacingClient(_CountingClient):
        def query(self, name, payload, **kwargs):
            answer = super().query(name, payload, **kwargs)
            if name == 'worker_job.claim_horizon' and not raced:
                # Commit after the probe read an empty queue, before the
                # coordinator acts on that answer and goes idle.
                answers.append(answer)
                raced.append(_enqueue(factory, owner, kind))
            return answer
    service = _spec_service(RacingClient(factory), kind, _recording_handler(claimed))
    try:
        service.start()
        assert claimed.get(timeout=5)[0] == raced[0]
        assert answers == [{'claimableInMs': None}]
    finally:
        assert service.stop(timeout=5)


def test_far_horizon_is_reread_at_the_reanchor_bound(semantic_authority, monkeypatch):
    from types import MappingProxyType
    from lib.automations import worker_service
    from lib.automations.runtime_policy import runtime_policy
    factory, owner, _ = semantic_authority
    client, kind, claimed = _CountingClient(factory), _spec_kind(), queue.Queue()
    # A monotonic wait does not advance across host suspend; the coordinator
    # must re-read a far horizon instead of trusting one long sleep.
    policy = MappingProxyType({**runtime_policy(), 'horizonReanchorMilliseconds': 200})
    monkeypatch.setattr(worker_service, 'runtime_policy', lambda: policy)
    _enqueue(factory, owner, kind, available_at_ms=int(time.time() * 1000) + 3_600_000)
    service = _spec_service(client, kind, _recording_handler(claimed))
    try:
        service.start()
        wait_until(lambda: client.count('query', 'worker_job.claim_horizon') >= 3)
        assert client.count('command', 'worker_job.claim_next') == 0
        assert claimed.empty()
    finally:
        assert service.stop(timeout=5)


def test_shutdown_during_initial_proof_defers_before_handler_dispatch(semantic_authority):
    factory, repo, run = admit(semantic_authority)
    entered, release = threading.Event(), threading.Event()
    class Client:
        def worker_watch(self, **kwargs):
            return factory().worker_watch(**kwargs)
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
