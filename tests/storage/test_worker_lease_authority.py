"""Public worker RPCs use storage-owned lease time and native restart fencing."""
import uuid

import pytest

from tests.support.worker_job_time import wall_ms, wait_until_ms

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def command(factory, name, payload):
    return factory(write=True).command(name, payload, uuid.uuid4().hex)


def owned_claim(authority):
    factory, owner, engine = authority
    task = uuid.uuid4().hex
    kind = 'authority-' + task
    command(factory, 'worker_job.enqueue', dict(task_id=task, user_id=owner,
        tenant_id='owned-tenant', task_kind=kind, idempotency_key=task,
        payload={}, now_ms=1))
    before = wall_ms()
    job = command(factory, 'worker_job.claim_next', dict(worker_id='holder',
        task_kinds=[kind], now_ms=1, lease_ms=10000))
    assert before + 10000 <= job['leaseDeadlineMs'] <= wall_ms() + 10000
    return factory, owner, engine, kind, job


def proof(job, observed=1):
    return dict(task_id=job['taskId'], worker_id=job['claimOwner'],
        fencing_token=job['fencingToken'], now_ms=observed)


def test_old_caller_timestamp_cannot_extend_an_expired_lease(semantic_authority):
    factory, _, _, kind, job = owned_claim(semantic_authority)
    # Even a permitted future observation cannot trigger early takeover.
    assert command(factory, 'worker_job.claim_next', dict(worker_id='rival',
        task_kinds=[kind], now_ms=wall_ms()+3600000, lease_ms=10000)) is None
    wait_until_ms(job['leaseDeadlineMs'])
    assert factory().query('worker_job.claim_state', proof(job))['ok'] is False
    assert command(factory, 'worker_job.heartbeat', dict(proof(job), lease_ms=60000))['ok'] is False
    assert command(factory, 'worker_job.complete', dict(proof(job), terminal_status='succeeded'))['ok'] is False
    assert command(factory, 'worker_job.defer', dict(proof(job), available_at_ms=2,
        replay_cursor=1, observed_cancel_sequence=0))['ok'] is False
    replacement = command(factory, 'worker_job.claim_next', dict(worker_id='rival',
        task_kinds=[kind], now_ms=1, lease_ms=10000))
    assert replacement['fencingToken'] == job['fencingToken'] + 1


def test_native_restart_invalidates_and_recovers_an_unexpired_holder(semantic_authority):
    if semantic_authority[2] is None:
        pytest.skip('Native authority restart contract')
    factory, owner, engine, kind, job = owned_claim(semantic_authority)
    engine.crash_cycle()
    saved = factory().query('worker_job.get', dict(task_id=job['taskId'], user_id=owner))
    assert saved == job
    assert factory().query('worker_job.claim_state', proof(job))['ok'] is False
    assert command(factory, 'worker_job.heartbeat', dict(proof(job), lease_ms=60000))['ok'] is False
    replacement = command(factory, 'worker_job.claim_next', dict(worker_id='replacement',
        task_kinds=[kind], now_ms=1, lease_ms=10000))
    assert replacement['taskId'] == job['taskId']
    assert replacement['fencingToken'] == job['fencingToken'] + 1
