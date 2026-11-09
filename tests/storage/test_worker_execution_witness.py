"""Durable entry binds the physical writer once, independently of API lifetime."""
import copy

import pytest

from lib.process_owner import process_owner
from lib.storage.errors import StorageError
from tests.storage import test_conversation_worker_publication as root

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api
execution = root.execution
KEY = '_conversationWorkerExecution'


def process_worker_id(process):
    return 'tofu.worker-process/v1:' + ':'.join(str(process[key]) for key in
        ('boot', 'pid_namespace', 'pid', 'started')) + ':0123456789abcdef0123456789abcdef'


def private_attempt(execution):
    from lib.turn_lifecycle import get_attempt
    return get_attempt(execution[2]['attempt']['attemptId'], user_id=execution[1],
                       include_execution_config=True)


def start(execution, job, process):
    return root.command(execution, 'turn.attempt.start',
        attempt_id=execution[2]['attempt']['attemptId'], task_id=job['taskId'],
        worker_claim=root.proof(job), worker_process=process)


def test_entry_witness_is_atomic_immutable_and_private(execution):
    job, process = root.claim(execution), process_owner()
    assert process is not None
    first = start(execution, job, process)
    assert first == start(execution, job, process)
    assert KEY not in first and 'config' not in first
    witness = private_attempt(execution)['config'][KEY]
    assert witness == {'format': 'tofu.conversation-worker-execution/v1',
                       'claim': root.proof(job), 'process': process}
    changed = {**process, 'started': str(int(process['started']) + 1)}
    with pytest.raises(StorageError):
        start(execution, job, changed)
    assert private_attempt(execution)['config'][KEY] == witness
    # Read-only authority probes do not replace or need to repeat the witness.
    assert root.start(execution, worker_claim=root.proof(job)) == first


def test_missing_entry_witness_cannot_be_backfilled_after_effects(execution):
    job = root.claim(execution)
    root.start(execution, worker_claim=root.proof(job))
    with pytest.raises(StorageError):
        start(execution, job, process_owner())
    assert KEY not in private_attempt(execution)['config']


def test_malformed_witness_refuses_without_entering(execution):
    job, process = root.claim(execution), process_owner()
    for bad in ({}, {**process, 'pid': True}, {**process, 'started': '-1'},
                {**process, 'pid_namespace': 7}, {**process, 'extra': 'untrusted'}):
        with pytest.raises(StorageError):
            start(execution, job, bad)
        assert private_attempt(execution)['status'] == 'pending'
    before = copy.deepcopy(private_attempt(execution))
    root.command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
                 now_ms=1, reason='Stop')
    with pytest.raises(StorageError):
        start(execution, job, process)
    assert private_attempt(execution) == before


def test_entered_live_process_survives_heartbeat_deadline(execution):
    from tests.support.worker_job_time import wait_until_ms, wall_ms

    process = process_owner()
    job = root.claim(execution, worker=process_worker_id(process))
    start(execution, job, process)
    wait_until_ms(job['leaseDeadlineMs'] + 1)
    proof = dict(task_id=job['taskId'], **root.proof(job), now_ms=1)
    assert execution[0]().query('worker_job.claim_state', proof)['ok'] is True
    assert root.claim(execution, worker='rival') is None
    assert execution[0]().query('worker_job.claim_horizon', {
        'task_kinds': ['conversation-attempt']})['claimableInMs'] > 0
    assert root.publish(execution, worker_claim=root.proof(job))['applied']
    renewed = root.command(execution, 'worker_job.heartbeat', **proof, lease_ms=10_000)
    assert renewed['ok'] is True
    assert renewed['job']['leaseDeadlineMs'] > wall_ms()
    root.command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
                 now_ms=wall_ms(), reason='Stop')
    with pytest.raises(StorageError):
        root.start(execution, worker_claim=root.proof(job))
    assert root.publish(execution, terminal=True, worker_claim=root.proof(job))['applied']


def test_entered_dead_process_can_be_recovered_after_deadline(execution):
    import json
    import subprocess
    import sys
    from tests.support.worker_job_time import wait_until_ms

    code = ('import json,sys; from lib.process_owner import process_owner; '
            'print(json.dumps(process_owner()),flush=True); sys.stdin.read()')
    child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        process = json.loads(child.stdout.readline())
        job = root.claim(execution, worker=process_worker_id(process))
        start(execution, job, process)
        child.kill()
        child.wait(timeout=10)
        wait_until_ms(job['leaseDeadlineMs'] + 1)
        replacement = root.claim(execution, worker='replacement')
        assert replacement['taskId'] == job['taskId']
        assert replacement['fencingToken'] == job['fencingToken'] + 1
        assert execution[0]().query('worker_job.claim_state', dict(
            task_id=job['taskId'], **root.proof(job), now_ms=1))['ok'] is False
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def test_live_expired_holder_does_not_hide_recovery_or_queue(semantic_authority):
    import uuid
    from tests.support.worker_job_time import wait_until_ms, wall_ms
    factory, owner, _ = semantic_authority
    kind = 'process-liveness-' + uuid.uuid4().hex
    def command(name, **payload):
        return factory(write=True).command(name, payload, None)
    def enqueue(task):
        return factory(write=True).command('worker_job.enqueue', dict(
            task_id=task, user_id=owner, tenant_id='', task_kind=kind,
            idempotency_key=task, payload={}, now_ms=wall_ms()), uuid.uuid4().hex)
    def claim(worker):
        return command('worker_job.claim_next', worker_id=worker,
            task_kinds=[kind], now_ms=wall_ms(), lease_ms=10_000)
    first, second, third = [uuid.uuid4().hex for _ in range(3)]
    enqueue(first)
    live = claim(process_worker_id(process_owner()))
    enqueue(second)
    legacy = claim('legacy')
    enqueue(third)
    wait_until_ms(legacy['leaseDeadlineMs'] + 1)
    horizon = lambda: factory().query('worker_job.claim_horizon', {'task_kinds': [kind]})['claimableInMs']
    assert horizon() == 0
    # Claims take whichever job became claimable first (docs/STORAGE.md, "Claim
    # order"): the queued job since its enqueue, the legacy lease only since its
    # deadline. Neither waits behind the live holder's expired lease.
    queued = claim('queued-worker')
    assert queued['taskId'] == third
    assert horizon() == 0
    recovered = claim('recovery')
    assert recovered['taskId'] == second
    assert horizon() > 0
    # Explicit release still works after the time horizon, and advances the
    # token only on the next claim. No late holder can write after that release.
    now = wall_ms()
    assert command('worker_job.defer', task_id=first, **root.proof(live), now_ms=now,
        available_at_ms=now, replay_cursor=0, observed_cancel_sequence=0)['ok']
    next_holder = claim('next')
    assert next_holder['taskId'] == first
    assert next_holder['fencingToken'] == live['fencingToken'] + 1
    assert not command('worker_job.heartbeat', task_id=first, **root.proof(live),
        now_ms=wall_ms(), lease_ms=10_000)['ok']


def test_process_claim_lost_ack_replays_exact_original_claim(semantic_authority):
    import uuid
    from tests.support.worker_job_time import wall_ms
    factory, owner, _ = semantic_authority
    kind = uuid.uuid4().hex
    for _ in range(2):
        task = uuid.uuid4().hex
        factory(write=True).command('worker_job.enqueue', dict(task_id=task,
            user_id=owner, tenant_id='', task_kind=kind, payload={}, idempotency_key=task,
            now_ms=wall_ms()), uuid.uuid4().hex)
    command_id = uuid.uuid4().hex
    payload = dict(worker_id=process_worker_id(process_owner()), task_kinds=[kind],
                   now_ms=wall_ms(), lease_ms=10_000, retain_claim=True)
    first = factory(write=True).command('worker_job.claim_next', payload, command_id)
    replay = factory(write=True).command('worker_job.claim_next', payload, command_id)
    assert replay == first
    import json
    print('Retained acquisition response bytes:', len(json.dumps(
        first, separators=(',', ':'), ensure_ascii=False).encode()))
    from lib.durable_worker import StorageWorkerJobStore
    store = StorageWorkerJobStore(client_factory=factory)
    arguments = {key: payload[key] for key in ('worker_id', 'task_kinds', 'now_ms', 'lease_ms')}
    next_job = store.claim_next(**arguments)
    assert next_job['taskId'] != first['taskId']
    # A distinct acquisition at the same clock tick cannot alias its receipt.
    assert store.claim_next(**arguments) is None
