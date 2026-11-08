"""Immutable descendant births inherit the root's transaction-owned execution right."""
import copy
import json

import pytest

from lib.storage.errors import StorageError
from tests.storage import test_conversation_worker_publication as root
from tests.support.worker_job_time import wall_ms

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api
execution = root.execution


def birth(execution, parent, child, **extra):
    return root.command(execution, 'invocation.register_child',
        conversation_id=execution[2]['turn']['conversationId'],
        parent_task_id=parent, child_task_id=child, **extra)


def record(execution, child):
    return execution[0]().query('record.get', {
        'namespace': 'task_results', 'key': child, 'user_id': execution[1]})


def active(execution):
    job = root.claim(execution)
    root.start(execution, worker_claim=root.proof(job))
    return job


def checkpoint(execution, child, value, *, expected_version, **extra):
    return root.command(execution, 'task_results.checkpoint', key=child,
        value=value, expected_version=expected_version, **extra)


def test_child_and_nested_birth_require_current_root_claim(execution):
    job = active(execution)
    parent = job['taskId']
    for child in ('child', 'nested'):
        for extra in ({}, {'worker_claim': {'worker_id': 'stale', 'fencing_token': 1}}):
            with pytest.raises(StorageError):
                birth(execution, parent, child, **extra)
            assert record(execution, child) is None
        identity = birth(execution, parent, child, worker_claim=root.proof(job))
        assert identity == birth(execution, parent, child, worker_claim=root.proof(job))
        parent = child


def test_root_stop_closes_all_descendant_births(execution):
    job = active(execution)
    birth(execution, job['taskId'], 'child', worker_claim=root.proof(job))
    root.command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
                 now_ms=wall_ms(), reason='Stop')
    for parent, child in ((job['taskId'], 'sibling'), ('child', 'nested')):
        with pytest.raises(StorageError):
            birth(execution, parent, child, worker_claim=root.proof(job))
        assert record(execution, child) is None


@pytest.mark.parametrize('depth', [1, 3])
def test_descendant_checkpoint_and_replay_cannot_omit_root_proof(execution, depth):
    job = active(execution)
    child = job['taskId']
    for index in range(depth):
        next_child = f'child-{index}'
        birth(execution, child, next_child, worker_claim=root.proof(job))
        child = next_child
    before = record(execution, child)
    value = {**before['value'], 'status': 'running', 'content': 'owned child'}
    for extra in ({}, {'worker_claim': {'worker_id': 'stale', 'fencing_token': 1}}):
        with pytest.raises(StorageError):
            checkpoint(execution, child, value, expected_version=before['version'], **extra)
        assert record(execution, child) == before
        result = root.command(execution, 'event.append', task_id=child,
            sequence=1, event={'type': 'delta', 'text': 'refused'}, **extra)
        assert result['inserted'] is False
    checkpoint(execution, child, value, expected_version=before['version'], worker_claim=root.proof(job))
    result = root.command(execution, 'event.append', task_id=child,
        sequence=1, event={'type': 'delta', 'text': 'owned'}, worker_claim=root.proof(job))
    assert result['inserted'] is True


def test_child_stop_refuses_new_intent_but_allows_existing_effect_drain(execution):
    job = active(execution)
    birth(execution, job['taskId'], 'child', worker_claim=root.proof(job))
    row = {'taskId': 'child', 'toolName': 'controlled', 'toolCallId': 'before',
           'roundNum': 1, 'executionEffect': {'operationId': 'before-stop',
            'scope': 'invocation', 'state': 'entered'}}
    before = record(execution, 'child')
    value = {**before['value'], 'status': 'running', 'tool_rounds': json.dumps([row])}
    checkpoint(execution, 'child', value, expected_version=before['version'], worker_claim=root.proof(job))
    root.command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
                 now_ms=wall_ms(), reason='Stop')
    before = record(execution, 'child')
    new = copy.deepcopy(row)
    new['executionEffect']['operationId'] = 'after-stop'
    for status in ('running', 'interrupted'):
        with pytest.raises(StorageError):
            checkpoint(execution, 'child', {**before['value'], 'status': status,
                'tool_rounds': json.dumps([row, new])}, expected_version=before['version'],
                worker_claim=root.proof(job))
        assert record(execution, 'child') == before
    row['executionEffect']['state'] = 'returned'
    checkpoint(execution, 'child', {**before['value'], 'tool_rounds': json.dumps([row])},
        expected_version=before['version'], worker_claim=root.proof(job))


def test_descendant_batch_cannot_bypass_claim_when_all_callers_omit_proofs(execution):
    job = active(execution)
    birth(execution, job['taskId'], 'child', worker_claim=root.proof(job))
    result = root.command(execution, 'event.append_batch', events=[
        {'task_id': 'child', 'user_id': execution[1], 'sequence': 1,
         'event': {'type': 'delta', 'text': 'refused'}},
        {'task_id': 'ordinary', 'user_id': execution[1], 'sequence': 1,
         'event': {'type': 'delta', 'text': 'permitted'}},
    ])
    assert result['inserted'] == 1
    assert result['rejected'] == 1
    assert result['results'][0]['rejected'] == 'worker_claim_stale'


@pytest.mark.parametrize('operation', ['record.put', 'record.delete'])
def test_generic_records_cannot_bypass_worker_publication(execution, operation):
    job = active(execution)
    birth(execution, job['taskId'], 'child', worker_claim=root.proof(job))
    for task in (job['taskId'], 'child'):
        before = record(execution, task)
        value = (before or {}).get('value') or {'task_id': task, 'user_id': execution[1],
            'conv_id': execution[2]['turn']['conversationId'], 'status': 'running'}
        with pytest.raises(StorageError):
            root.command(execution, operation, namespace='task_results', key=task,
                value={**value, 'content': 'bypassed'}, expected_version=(before or {}).get('version', 0))
        assert record(execution, task) == before


def test_birth_cannot_turn_root_into_its_own_child(execution):
    job = active(execution)
    with pytest.raises(StorageError):
        birth(execution, job['taskId'], job['taskId'], worker_claim=root.proof(job))


def test_descendant_cannot_publish_after_root_expiry_or_takeover(execution):
    from tests.support.worker_job_time import wait_until_ms
    job = active(execution)
    birth(execution, job['taskId'], 'child', worker_claim=root.proof(job))
    before = record(execution, 'child')
    wait_until_ms(job['leaseDeadlineMs'] + 1)
    for takeover in (False, True):
        if takeover:
            replacement = root.claim(execution, 'replacement')
            assert replacement['fencingToken'] > job['fencingToken']
        with pytest.raises(StorageError):
            birth(execution, 'child', 'nested', worker_claim=root.proof(job))
        with pytest.raises(StorageError):
            checkpoint(execution, 'child', {**before['value'], 'status': 'interrupted'},
                expected_version=before['version'], worker_claim=root.proof(job))
        assert record(execution, 'child') == before
        result = root.command(execution, 'event.append', task_id='child', sequence=1,
            event={'type': 'done'}, worker_claim=root.proof(job))
        assert result['rejected'] == 'worker_claim_stale'


def test_replayed_child_effect_intent_rechecks_stop_and_echoes_its_admission(execution):
    from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
    job = active(execution)
    birth(execution, job['taskId'], 'child', worker_claim=root.proof(job))
    before = record(execution, 'child')
    row = {'taskId': 'child', 'toolName': 'controlled', 'toolCallId': 'first',
           'roundNum': 1, 'executionEffect': {'operationId': 'exact-intent',
            'scope': 'invocation', 'state': 'entered'}}
    value = {**before['value'], 'tool_rounds': json.dumps([row])}
    extra = {'worker_claim': root.proof(job), 'tool_effect_intent': 'exact-intent',
             'guard_contract': TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, 'require_parent': True}
    ack = checkpoint(execution, 'child', value, expected_version=before['version'], **extra)
    assert ack.get('tool_effect_intent') == 'exact-intent'
    assert checkpoint(execution, 'child', value, expected_version=before['version'], **extra) == ack
    root.command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
                 now_ms=wall_ms(), reason='Stop')
    with pytest.raises(StorageError):
        checkpoint(execution, 'child', value, expected_version=before['version'], **extra)


def test_child_intent_ack_requires_one_exact_entered_occurrence(execution):
    from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
    job = active(execution)
    birth(execution, job['taskId'], 'child', worker_claim=root.proof(job))
    before = record(execution, 'child')
    row = {'taskId': 'child', 'toolName': 'controlled', 'toolCallId': 'first',
           'roundNum': 1, 'executionEffect': {'operationId': 'exact-intent',
            'scope': 'invocation', 'state': 'entered'}}
    for rows in (None, [], [row, row], [{**row, 'taskId': 'foreign'}],
                 [{**row, 'executionEffect': {**row['executionEffect'], 'state': 'returned'}}]):
        value = copy.deepcopy(before['value'])
        if rows is not None:
            value['tool_rounds'] = json.dumps(rows)
        with pytest.raises(StorageError) as error:
            checkpoint(execution, 'child', value, expected_version=before['version'],
                worker_claim=root.proof(job), tool_effect_intent='exact-intent',
                guard_contract=TASK_RESULT_CHECKPOINT_GUARD_CONTRACT, require_parent=True)
        assert error.value.code == 'database_protocol_error'
        assert record(execution, 'child') == before


@pytest.mark.parametrize('nested', [False, True])
def test_inspector_stream_requires_its_exact_birth_and_current_root_claim(execution, nested):
    job = active(execution)
    parent = job['taskId']
    if nested:
        birth(execution, parent, 'middle', worker_claim=root.proof(job))
        parent = 'middle'
    birth(execution, parent, 'inspected-child', worker_claim=root.proof(job))
    stream = parent + '#agent:inspected-child'
    for proof in (None, {'worker_id': 'stale', 'fencing_token': 1}):
        row = dict(task_id=stream, sequence=0, event={'type': 'messages_snapshot', 'messages': []})
        result = root.command(execution, 'event.append', **row,
                              **({'worker_claim': proof} if proof else {}))
        assert result['inserted'] is False
    assert root.command(execution, 'event.append', task_id=stream, sequence=0,
        event={'type': 'messages_snapshot', 'messages': []}, worker_claim=root.proof(job))['inserted'] is True
    for forged in (parent + '#agent:unregistered', job['taskId'] + '#agent:middle' if not nested
                   else 'different-parent#agent:inspected-child'):
        with pytest.raises(StorageError):
            root.command(execution, 'event.append', task_id=forged, sequence=0,
                event={'type': 'messages_snapshot'}, worker_claim=root.proof(job))
    root.command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
                 now_ms=wall_ms(), reason='Stop')
    assert root.command(execution, 'event.append', task_id=stream, sequence=1,
        event={'type': 'messages_snapshot', 'messages': []}, worker_claim=root.proof(job))['inserted'] is True
