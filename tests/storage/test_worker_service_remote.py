"""A cold execution service receives work/control solely from its authority."""
import threading
import time
import uuid

import pytest

from lib.durable_worker import StorageWorkerJobStore, WorkerJobOutcome, WorkerKindRegistration, WorkerKindSafety
from lib.durable_worker.service import DurableWorkerService
from lib.identity import PrincipalContext
from lib.storage.worker_wake import StorageWorkerWakeSource

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def test_empty_service_wakes_and_routes_durable_stop(semantic_authority):
    from lib.automations.runtime_policy import runtime_policy
    factory, owner, _ = semantic_authority
    policy = runtime_policy()
    empty = threading.Event()
    entered = threading.Event()
    observed_cancel = threading.Event()
    completed = threading.Event()

    class Client:
        def __init__(self, client):
            self.client = client

        def __getattr__(self, name):
            return getattr(self.client, name)

        def query(self, operation, *args, **kwargs):
            value = self.client.query(operation, *args, **kwargs)
            if operation == 'worker_job.claim_horizon':
                empty.set()
            return value

    observed_factory = lambda **kwargs: Client(factory(**kwargs))
    source = StorageWorkerWakeSource(observed_factory, policy)

    def execute(context, payload):
        entered.set()
        assert context._cancel_requested.wait(5), 'cross-process Stop did not wake heartbeat'
        observed_cancel.set()
        return WorkerJobOutcome(status='cancelled', result_ref=context.claim.task_id)

    def settle(context, payload, outcome, now_ms):
        value = StorageWorkerJobStore(client_factory=factory).complete(
            context.claim, outcome=outcome, now_ms=now_ms)
        completed.set()
        return value

    registration = WorkerKindRegistration(task_kind='remote-wake-test',
        safety=WorkerKindSafety(True, True, True, True), handler=execute,
        cancel_handler=execute, completion_handler=settle)
    service = DurableWorkerService(
        principal=PrincipalContext.system(subject_id='test-control', scopes={policy['controlScope']}),
        client_factory=observed_factory, registrations=[registration], max_workers=1,
        now_ms=lambda: int(time.time() * 1000), policy=policy, wake_source=source)
    service.start()
    try:
        assert empty.wait(5)
        task = 'remote-' + uuid.uuid4().hex
        factory(write=True).command('worker_job.enqueue', {
            'task_id': task, 'user_id': owner, 'tenant_id': '', 'task_kind': registration.task_kind,
            'payload': {'contract': 'test/v1', 'principal': PrincipalContext.user(
                subject_id='test-owner', owner_user_id=owner).to_payload()},
            'idempotency_key': task, 'priority': 0, 'available_at_ms': 0,
            'now_ms': int(time.time() * 1000),
        }, uuid.uuid4().hex)
        assert entered.wait(5)
        factory(write=True).command('worker_job.request_cancel', {
            'task_id': task, 'user_id': owner, 'reason': 'Stop',
            'now_ms': int(time.time() * 1000),
        }, uuid.uuid4().hex)
        assert observed_cancel.wait(5)
        assert completed.wait(5)
    finally:
        assert service.stop()
    assert service.snapshot()['inflight'] == 0
    assert source.signals._active == {}
    assert factory().query('worker_job.get', {'task_id': task, 'user_id': owner})['status'] == 'cancelled'
