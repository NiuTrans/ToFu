"""A durable conversation claim fences Turn publication inside its transaction.

These specifications use the same semantic commands on SQLite, PostgreSQL and
native TofuDB. A separate lease read is deliberately insufficient evidence.
"""
import uuid

import pytest

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from tests.support.worker_job_time import wall_ms, wait_until_ms

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.fixture
def execution(semantic_authority, monkeypatch):
    from lib import turn_lifecycle as turns

    factory, owner, _engine = semantic_authority
    monkeypatch.setattr(turns, '_turn_client', lambda **kw: factory(**kw))
    nonce = uuid.uuid4().hex
    pair = turns.create_turn_pair(
        nonce, command_id=nonce, input_projection={'content': 'work'},
        config={'model': 'controlled'}, user_id=owner,
        conversation_defaults={'allowCreate': True, 'title': 'Worker', 'settings': {}},
    )
    principal = PrincipalContext.user(subject_id=nonce, owner_user_id=owner,
                                      tenant_id='publication-test')
    dispatched = turns.dispatch_attempt_to_worker(principal, pair['attempt']['attemptId'])
    return factory, owner, pair, dispatched['job']


def command(execution, operation, **payload):
    factory, owner, _pair, _job = execution
    return factory(write=True).command(operation, dict(user_id=owner, **payload),
                                       uuid.uuid4().hex)


def claim(execution, worker='first'):
    return command(execution, 'worker_job.claim_next', worker_id=worker,
                   task_kinds=['conversation-attempt'], now_ms=wall_ms(), lease_ms=10_000)


def proof(job):
    return {'worker_id': job['claimOwner'], 'fencing_token': job['fencingToken']}


def start(execution, **kw):
    from lib.turn_lifecycle import mark_task_started

    _factory, owner, pair, job = execution
    return mark_task_started(pair['attempt']['attemptId'], job['taskId'], user_id=owner, **kw)


def event_payload(execution, *, terminal=False, **kw):
    _factory, owner, pair, job = execution
    return dict(user_id=owner, attempt_id=pair['attempt']['attemptId'], task_id=job['taskId'],
                terminal=terminal, status='completed' if terminal else 'running',
                slim=not terminal, content='published', thinking='',
                projection={'content': 'published'}, event_type='completed' if terminal else 'projection_updated',
                event_payload={}, **kw)


def publish(execution, **kw):
    payload = event_payload(execution, **kw)
    payload.pop('user_id')
    return command(execution, 'turn.event.record', **payload)


def snapshot(execution):
    factory, owner, pair, job = execution
    return (factory().query('turn.sync.snapshot', {
        'conversation_id': pair['turn']['conversationId'], 'user_id': owner,
    }), factory().query('event.list', {'task_id': job['taskId'], 'after': 0, 'limit': 10}))


def refused_without_writes(execution, action):
    before = snapshot(execution)
    with pytest.raises(StorageError) as caught:
        action()
    assert caught.value.code == 'database_conflict'
    assert snapshot(execution) == before


def test_queued_job_cannot_publish_even_with_a_guessed_claim(execution):
    for extra in ({}, {'worker_claim': {'worker_id': 'first', 'fencing_token': 1}}):
        refused_without_writes(execution, lambda: start(execution, **extra))
        refused_without_writes(execution, lambda: publish(execution, **extra))
        refused_without_writes(execution, lambda: publish(execution, terminal=True, **extra))


def test_only_current_claim_can_start_progress_and_settle(execution):
    job = claim(execution)
    for extra in ({}, {'worker_claim': {'worker_id': 'other', 'fencing_token': 1}},
                  {'worker_claim': {'worker_id': 'first', 'fencing_token': 2}}):
        refused_without_writes(execution, lambda: start(execution, **extra))
    assert start(execution, worker_claim=proof(job))['status'] == 'running'
    assert start(execution, worker_claim=proof(job))['status'] == 'running'
    for extra in ({}, {'worker_claim': {'worker_id': 'other', 'fencing_token': 1}}):
        refused_without_writes(execution, lambda: publish(execution, **extra))
        refused_without_writes(execution, lambda: publish(execution, terminal=True, **extra))
    assert publish(execution, worker_claim=proof(job))['applied'] is True
    assert publish(execution, terminal=True, worker_claim=proof(job))['applied'] is True
    factory, owner, _pair, _job = execution
    # Publication adds no worker ledger/receipt or implicit job settlement.
    assert factory().query('worker_job.get', {'task_id': job['taskId'], 'user_id': owner}) == job


def test_malformed_claim_is_refused_without_mutation(execution):
    claim(execution)
    for bad in ({}, {'worker_id': 'first', 'fencing_token': True},
                {'worker_id': 'first', 'fencing_token': 0},
                {'worker_id': 'first', 'fencing_token': 2**63},
                {'worker_id': '', 'fencing_token': 1},
                {'worker_id': 'first', 'fencing_token': 1, 'task_id': 'other'}):
        before = snapshot(execution)
        with pytest.raises(StorageError) as caught:
            start(execution, worker_claim=bad)
        assert caught.value.code == 'database_protocol_error'
        assert snapshot(execution) == before


def test_cancellation_allows_only_current_claim_terminal_cleanup(execution):
    job = claim(execution)
    command(execution, 'worker_job.request_cancel', task_id=job['taskId'], now_ms=wall_ms(), reason='stop')
    refused_without_writes(execution, lambda: start(execution, worker_claim=proof(job)))
    refused_without_writes(execution, lambda: publish(execution, worker_claim=proof(job)))
    refused_without_writes(execution, lambda: publish(execution, terminal=True))
    assert publish(execution, terminal=True, worker_claim=proof(job))['applied'] is True


def test_expired_claim_cannot_publish_before_or_after_takeover(execution):
    old = claim(execution)
    start(execution, worker_claim=proof(old))
    wait_until_ms(old['leaseDeadlineMs'] + 1)
    refused_without_writes(execution, lambda: publish(execution, worker_claim=proof(old), now=1))
    current = claim(execution, 'replacement')
    assert current['fencingToken'] == old['fencingToken'] + 1
    # Even the same naturally idempotent start command must recheck authority.
    refused_without_writes(execution, lambda: start(execution, worker_claim=proof(old)))
    refused_without_writes(execution, lambda: publish(execution, terminal=True, worker_claim=proof(old)))
    assert start(execution, worker_claim=proof(current))['status'] == 'running'
    assert publish(execution, worker_claim=proof(current))['applied'] is True


def test_batch_rolls_back_a_valid_prefix_when_a_later_claim_is_missing(execution):
    job = claim(execution)
    entries = [event_payload(execution, worker_claim=proof(job)), event_payload(execution)]
    # Structural batch admission requires ordinary durable event rows in order.
    for sequence, entry in enumerate(entries, 1):
        entry.update(slim=False)
        entry['task_event'] = {'task_id': job['taskId'], 'sequence': sequence, 'event': {'type': 'phase'}}
    refused_without_writes(execution, lambda: command(execution, 'turn.event.record_batch', entries=entries))
    entries[1]['worker_claim'] = proof(job)
    assert all(row['applied'] for row in command(execution, 'turn.event.record_batch', entries=entries))
