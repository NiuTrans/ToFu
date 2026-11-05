"""Active work is derived from authority indexes, independent of API carriers."""
import uuid

import pytest

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


def command(factory, operation, payload):
    return factory(write=True).command(operation, payload, uuid.uuid4().hex)


def test_activity_covers_waiting_leased_and_suspended_work_without_terminal_history(semantic_authority):
    factory, owner, _ = semantic_authority
    kind = 'activity-' + uuid.uuid4().hex
    task = uuid.uuid4().hex
    command(factory, 'worker_job.enqueue', {
        'task_id': task, 'user_id': owner, 'task_kind': kind,
        'idempotency_key': task, 'payload': {'conversationId': 'conversation'},
        'now_ms': 1})
    def activity(user_id=owner):
        return factory().query('worker_job.active.list', {'user_id': user_id, 'task_kinds': [kind]})
    rows = activity()
    assert len(rows) == 1
    assert rows[0] == {'taskId': task, 'userId': owner, 'taskKind': kind,
                       'conversationId': 'conversation', 'status': 'queued',
                       'createdAtMs': rows[0]['createdAtMs']}
    assert activity(None) == rows
    assert activity(owner + 1) == []
    job = command(factory, 'worker_job.claim_next', {
        'worker_id': 'activity-reader', 'task_kinds': [kind], 'now_ms': 1, 'lease_ms': 60000})
    assert activity()[0]['status'] == 'running'
    proof = {'task_id': task, 'worker_id': job['claimOwner'],
             'fencing_token': job['fencingToken'], 'now_ms': 1}
    command(factory, 'worker_job.defer', {
        **proof, 'available_at_ms': 1, 'replay_cursor': 0, 'observed_cancel_sequence': 0})
    assert activity()[0]['status'] == 'suspended'
    command(factory, 'worker_job.request_cancel', {
        'task_id': task, 'user_id': owner, 'reason': 'test', 'now_ms': 1})
    job = command(factory, 'worker_job.claim_next', {
        'worker_id': 'activity-reader', 'task_kinds': [kind], 'now_ms': 1, 'lease_ms': 60000})
    command(factory, 'worker_job.complete', {
        **proof, 'fencing_token': job['fencingToken'], 'terminal_status': 'cancelled'})
    assert activity() == []


@pytest.mark.parametrize('payload', [
    {'task_kinds': ['kind']}, {'user_id': True, 'task_kinds': ['kind']},
    {'user_id': 0, 'task_kinds': ['kind']}, {'user_id': None, 'task_kinds': []},
])
def test_activity_refuses_ambiguous_scope_or_kind(semantic_authority, payload):
    from lib.storage.errors import StorageError
    with pytest.raises(StorageError):
        semantic_authority[0]().query('worker_job.active.list', payload)
