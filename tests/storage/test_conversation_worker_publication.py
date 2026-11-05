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


def test_standalone_replay_requires_claim_without_live_attempt_hint(execution):
    job = claim(execution)
    start(execution, worker_claim=proof(job))
    row = {'task_id': job['taskId'], 'sequence': 1,
           'event': {'type': 'delta', 'text': 'private until committed'}}
    for extra in ({}, {'worker_claim': {'worker_id': 'old', 'fencing_token': 1}}):
        result = command(execution, 'event.append', **row, **extra)
        assert result['inserted'] is False
        assert result['rejected'] == 'worker_claim_stale'
    assert command(execution, 'event.append', **row,
                   worker_claim=proof(job))['inserted'] is True
    # A stale duplicate cannot borrow the successful writer's acknowledgement.
    assert command(execution, 'event.append', **row)['rejected'] == 'worker_claim_stale'


def test_replay_batch_preserves_independent_siblings_and_cancel_cleanup(execution):
    job = claim(execution)
    start(execution, worker_claim=proof(job))
    rows = [
        {'task_id': job['taskId'], 'sequence': 1, 'event': {'type': 'delta', 'text': 'refused'}},
        {'task_id': 'ordinary-stream', 'sequence': 1, 'event': {'type': 'delta', 'text': 'legacy'}},
        {'task_id': job['taskId'], 'user_id': execution[1], 'sequence': 2,
         'event': {'type': 'delta', 'text': 'owned'},
         'worker_claim': proof(job)},
    ]
    result = command(execution, 'event.append_batch', events=rows)
    assert result['rejected'] == 1
    assert result['inserted'] == 2
    command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
            now_ms=wall_ms(), reason='Stop')
    for sequence, kind, inserted in ((3, 'delta', True), (4, 'aborted', True)):
        result = command(execution, 'event.append', task_id=job['taskId'],
                         sequence=sequence, event={'type': kind}, worker_claim=proof(job))
        assert result['inserted'] is inserted


def test_flow_phases_borrow_root_claim_and_settle_with_it(execution):
    from lib.orchestration.visible_identity import visible_phase_identity
    _factory, owner, pair, job = execution
    owned = claim(execution)
    start(execution, worker_claim=proof(owned))
    attempt = pair['attempt']['attemptId']
    root = pair['turn']['turnId']
    payload = {'conversation_id': pair['turn']['conversationId'], 'attempt_id': attempt,
               'root_turn_id': root, 'messages': [{'role': 'assistant', 'content': 'phase'}],
               'start_index': 1, 'live': True}
    refused_without_writes(execution, lambda: command(execution, 'turn.visible.sync', **payload))
    assert command(execution, 'turn.visible.sync', **payload, worker_claim=proof(owned))
    _child_turn, child_attempt = visible_phase_identity(root, attempt, 1)
    child = event_payload(execution, flow_root_attempt_id=attempt)
    child['attempt_id'] = child_attempt
    with pytest.raises(StorageError):
        command(execution, 'turn.event.record', **{k: v for k, v in child.items() if k != 'user_id'})
    child['worker_claim'] = proof(owned)
    assert command(execution, 'turn.event.record',
                   **{k: v for k, v in child.items() if k != 'user_id'})['applied']
    assert publish(execution, terminal=True, worker_claim=proof(owned))['applied']


def test_task_result_checkpoint_revalidates_worker_before_replay(execution):
    _factory, owner, pair, job = execution
    owned = claim(execution)
    start(execution, worker_claim=proof(owned))
    value = {'task_id': job['taskId'], 'user_id': owner,
             'conv_id': pair['turn']['conversationId'], 'status': 'running',
             'content': 'checkpoint', 'thinking': ''}
    payload = {'key': job['taskId'], 'value': value, 'expected_version': 0}
    with pytest.raises(StorageError):
        command(execution, 'task_results.checkpoint', **payload)
    assert command(execution, 'task_results.checkpoint', **payload,
                   worker_claim=proof(owned))['version'] > 0
    with pytest.raises(StorageError):
        command(execution, 'task_results.checkpoint', **payload)


def test_queued_conversation_stop_remains_claimable_until_domain_settlement(execution):
    _factory, _owner, _pair, job = execution
    result = command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
                     now_ms=wall_ms(), reason='user_stop')
    assert result['job']['status'] == 'queued'
    assert result['job']['terminalAtMs'] == 0
    owned = claim(execution)
    assert owned['cancelSequence'] == 1
    refused_without_writes(execution, lambda: start(execution, worker_claim=proof(owned)))
    assert publish(execution, terminal=True, worker_claim=proof(owned))['applied']


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


def test_unentered_cancellation_allows_only_current_claim_terminal_cleanup(execution):
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

@pytest.mark.parametrize('boundary', ['replay', 'turn', 'terminal-turn'])
def test_cancel_refuses_new_tool_entry_but_drains_prior_effect(execution, boundary):
    job = claim(execution)
    start(execution, worker_claim=proof(job))
    event = {'type': 'tool_effect', 'toolName': 'write', 'toolCallId': 'call',
             'roundNum': 1, 'executionEffect': {'operationId': 'before-stop',
                 'state': 'entered', 'recovery': 'reconcile', 'scope': 'attempt'}}
    assert command(execution, 'event.append', task_id=job['taskId'], sequence=1,
                   event=event, worker_claim=proof(job))['inserted']
    command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
            now_ms=wall_ms(), reason='Stop')
    entry = {**event, 'executionEffect': {**event['executionEffect'], 'operationId': 'after-stop'}}
    row = {'task_id': job['taskId'], 'sequence': 2, 'event': entry}
    if boundary == 'replay':
        before = snapshot(execution)
        refused = command(execution, 'event.append', **row, worker_claim=proof(job))
        assert refused['rejected'] == 'worker_claim_stale'
        assert snapshot(execution) == before
    else:
        payload = event_payload(execution, terminal=boundary == 'terminal-turn')
        payload.pop('user_id')
        refused_without_writes(execution, lambda: command(execution, 'turn.event.record',
            **payload, task_event=row, worker_claim=proof(job)))
    result = {**event, 'executionEffect': {**event['executionEffect'], 'state': 'returned'}}
    assert command(execution, 'event.append', task_id=job['taskId'], sequence=2,
                   event=result, worker_claim=proof(job))['inserted']
