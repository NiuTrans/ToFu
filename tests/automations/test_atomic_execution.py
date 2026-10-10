"""Same admission, lease and replay contract against both real authorities."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import time
import uuid

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.automations.errors import AutomationConflict
from lib.automations.transitions import ExecutionFence, bind_fence, prepare_step, record_output
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from tests.support.automation_fixtures import clock_definition, trigger
from tests.support.worker_job_time import wait_until_ms

pytestmark = pytest.mark.api


pytest_plugins = ('tests.support.semantic_authorities',)


def setup_rule(authority):
    factory, owner, engine = authority
    principal = PrincipalContext.user(subject_id='publisher', owner_user_id=owner, tenant_id='test-tenant',
        scopes={'automations:read', 'automations:manage', 'automations:execute'})
    repo = AutomationExecutionRepository(principal, client_factory=factory)
    rule = clock_definition()
    rule['id'] += '-' + uuid.uuid4().hex
    now = time.time()
    saved = repo.publish(prepare_definition(rule), expected_version=0, now=now)
    event = trigger(event_id=uuid.uuid4().hex)
    event['occurredAt'] = now
    return repo, saved, event, now, factory


def claim(factory, now, worker='worker-one'):
    return factory(write=True).command('worker_job.claim_next', {
        'worker_id': worker, 'task_kinds': ['automation-run'], 'now_ms': int(now * 1000),
        'lease_ms': 10000,
    }, None)


def test_admission_concurrency_grant_and_reopen(semantic_authority):
    authority = semantic_authority
    repo, definition, event, now, factory = setup_rule(authority)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: repo.admit(definition.id, event, now=now + 0.01), range(4)))
    assert len({row.id for row in results}) == 1
    run = results[0]
    job = factory().query('worker_job.get', {'task_id': run.value['jobId'], 'user_id': repo.owner_id})
    assert job['payload']['principal']['scopes'] == ['automations:execute', 'automations:read']
    assert job['payload']['principal']['kind'] == 'system'
    assert job['priority'] == 200
    pointers = factory().query('record.list', {'namespace': 'automation.active.v1', 'prefix': repo._prefix, 'limit': 64})
    assert len(pointers) == 1
    assert 'steps' not in pointers[0]['value']
    if authority[2]:
        authority[2].crash_cycle()
        assert repo.get('run', run.id).value == run.value
        assert factory().query('worker_job.get', {'task_id': run.value['jobId'], 'user_id': repo.owner_id}) == job
    repo.retire_definition(definition, now=now + 0.1)
    assert repo.admit(definition.id, event, now=now + 0.2).value == run.value


def test_stale_definition_never_admits_job(semantic_authority):
    authority = semantic_authority
    repo, definition, event, now, factory = setup_rule(authority)
    from lib.automations.transitions import new_run
    run = new_run(definition, event, now=now)
    import hashlib
    key = repo._key(run['id'])
    run.update(ownerId=repo.owner_id, tenantId=repo.principal.tenant_id,
        jobId='automation-run:' + hashlib.sha256(key.encode()).hexdigest(),
        principal=definition.value['grant']['principal'])
    repo.retire_definition(definition, now=now + .1)
    with pytest.raises(StorageError) as error:
        factory(write=True).command('automation.run.admit', {
            'user_id': repo.owner_id, 'tenant_id': repo.principal.tenant_id,
            'run_key': key, 'run': run, 'now_ms': int(now * 1000),
            'definition_key': repo._key(definition.id), 'definition_revision': definition.version,
            'definition_digest': definition.value['compiled']['digest'],
        }, None)
    assert error.value.code == 'database_conflict'
    assert repo.get('run', run['id']) is None
    assert factory().query('worker_job.get', {'task_id': run['jobId'], 'user_id': repo.owner_id}) is None


def test_progress_is_live_fenced_and_lost_ack_replays_exact_state(semantic_authority):
    authority = semantic_authority
    repo, definition, event, now, factory = setup_rule(authority)
    first = repo.admit(definition.id, event, now=now)
    job = claim(factory, now + .1)
    fence = ExecutionFence(job['taskId'], job['claimOwner'], job['fencingToken'])
    bound = repo.advance(first, bind_fence(first.value, fence, now=now + .2), fence, now=now + .2)
    replay = repo.advance(first, bound.value, fence, now=now + .3)
    assert replay.version == bound.version
    assert replay.value == bound.value
    # The job is not reclaimed yet, but expiry alone revokes write authority.
    wait_until_ms(job['leaseDeadlineMs'])
    changed = deepcopy(bound.value)
    changed['updatedAt'] = now + 11
    with pytest.raises(AutomationConflict):
        repo.advance(bound, changed, fence, now=now + 11)
    reclaimed = claim(factory, now + 11, worker='worker-two')
    second_fence = ExecutionFence(reclaimed['taskId'], reclaimed['claimOwner'], reclaimed['fencingToken'])
    assert second_fence.token > fence.token
    current = repo.advance(bound, bind_fence(bound.value, second_fence, now=now + 11), second_fence, now=now + 11)
    with pytest.raises(AutomationConflict):
        repo.advance(current, changed, fence, now=now + 11)
    intent, step = prepare_step(current.value, second_fence, now=now + 11.1)
    # Rule deadline elapsed, so no side effect is dispatched after reclaim.
    assert step is None
    settled = repo.advance(current, intent, second_fence, now=now + 11.1)
    assert settled.value['status'] == 'cancelled'
    assert factory().query('record.list', {'namespace':'automation.active.v1', 'prefix':repo._prefix, 'limit':64}) == []


def test_publisher_cannot_grant_unheld_capabilities(semantic_authority):
    authority = semantic_authority
    repo, _, _, now, _ = setup_rule(authority)
    from tests.support.automation_fixtures import skill_definition
    with pytest.raises(PermissionError):
        repo.publish(prepare_definition(skill_definition()), expected_version=0, now=now)


def test_queue_conflict_rolls_back_run_and_indices(semantic_authority):
    authority = semantic_authority
    import hashlib
    from lib.automations.transitions import occurrence_id
    repo, definition, event, now, factory = setup_rule(authority)
    run_id = occurrence_id(definition.id, event)
    job_id = 'automation-run:' + hashlib.sha256(repo._key(run_id).encode()).hexdigest()
    factory(write=True).command('worker_job.enqueue', {
        'task_id': job_id, 'user_id': repo.owner_id, 'tenant_id': repo.principal.tenant_id,
        'task_kind': 'foreign-test-kind', 'idempotency_key': job_id,
        'payload': {'wrong': 'a conflicting earlier command'}, 'now_ms': int(now * 1000),
    }, 'conflicting-job:' + uuid.uuid4().hex)
    with pytest.raises(AutomationConflict):
        repo.admit(definition.id, event, now=now)
    assert repo.get('run', run_id) is None
    for namespace in ('automation.active.v1', 'automation.history.v1'):
        assert factory().query('record.list', {'namespace':namespace,'prefix':repo._prefix,'limit':64}) == []


def test_frozen_evidence_cannot_change_under_a_live_claim(semantic_authority):
    authority = semantic_authority
    repo, definition, event, now, factory = setup_rule(authority)
    first = repo.admit(definition.id, event, now=now)
    job = claim(factory, now + .1)
    fence = ExecutionFence(job['taskId'], job['claimOwner'], job['fencingToken'])
    bound = repo.advance(first, bind_fence(first.value, fence, now=now + .2), fence, now=now + .2)
    changed = deepcopy(bound.value)
    changed['snapshot']['name'] = 'rewritten after admission'
    with pytest.raises(StorageError) as error:
        repo.advance(bound, changed, fence, now=now + .3)
    assert error.value.code == 'database_protocol_error'
    assert repo.get('run', first.id).value == bound.value


def test_lost_admission_ack_is_proved_without_second_job(semantic_authority):
    authority = semantic_authority
    repo, definition, event, now, factory = setup_rule(authority)

    class LoseAdmissionAck:
        def __init__(self):
            self.lost = False

        def command(self, operation, payload, command_id):
            result = factory(write=True).command(operation, payload, command_id)
            if operation == 'automation.run.admit' and not self.lost:
                self.lost = True
                raise StorageError('database_timeout', 'Lost admission ACK after commit')
            return result

        def query(self, operation, payload):
            return factory().query(operation, payload)

    client = LoseAdmissionAck()
    lost_ack_repo = AutomationExecutionRepository(repo.principal, client_factory=lambda **kwargs: client)
    run = lost_ack_repo.admit(definition.id, event, now=now)
    assert run.version == 1
    assert repo.admit(definition.id, event, now=now + .1).value == run.value
    first_claim = claim(factory, now + .2)
    assert first_claim['taskId'] == run.value['jobId']
    assert claim(factory, now + .2, worker='other') is None


def test_generic_record_mutations_cannot_bypass_execution_fence(semantic_authority):
    authority = semantic_authority
    repo, definition, event, now, factory = setup_rule(authority)
    run = repo.admit(definition.id, event, now=now)
    for namespace in ('automation.runs.v1', 'automation.active.v1', 'automation.history.v1'):
        for operation in ('record.put', 'record.delete'):
            payload = {'namespace': namespace, 'key': repo._key(run.id), 'expected_version': 1}
            if operation == 'record.put':
                payload['value'] = {'status':'succeeded', 'ownerId':repo.owner_id}
            with pytest.raises(StorageError) as error:
                factory(write=True).command(operation, payload, uuid.uuid4().hex)
            assert error.value.code == 'database_protocol_error'
    assert repo.get('run', run.id).value == run.value
