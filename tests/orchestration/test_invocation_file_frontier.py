"""Paid file batches require owned operation proof, never count-only inference."""
from copy import deepcopy

import pytest

from lib.agent_core.execution_budget import _ensure_execution_budget, inherit_execution_budget
from lib.conversation_sync.turn_images import turn_image_owner_scope
from lib.orchestration.execution_journal import RecoveryRefused
from lib.orchestration.invocation_frontier import preflight_registered_actor_accounts
from tests.orchestration.test_invocation_frontier import fixture

pytestmark = pytest.mark.unit


def paid_batch(monkeypatch, count=1):
    storage, admission, record, response, reads, options = fixture()
    root = dict(id='flow-root', _userId=7, status='running', config={})
    account = _ensure_execution_budget(root); cuts = []
    account.bind_account_frontier_observer(cuts.append)
    actor = dict(id='task', _userId=7, status='running', config={})
    inherit_execution_budget(root, actor)
    state = response['state']['snapshot']
    lease = account.reserve_model(actor, input_tokens=10000, max_output_tokens=200)
    lease.mark_dispatched(); lease.settle(state['usage'][0])
    calls, references, rows = [], [], []
    for index in range(count):
        call_id, operation = f'write-{index}', f'{index:032x}'
        calls.append(dict(id=call_id, type='function', function=dict(name='write_file', arguments='{}')))
        references.append(dict(call_id=call_id, inspection=dict(conversationId='conversation',
            invocationId='task', operationId=operation, ownerScope=turn_image_owner_scope(7, 'conversation'))))
        # Only the nondeterministic file classifier is stubbed here. Owned
        # receipt lookup, metadata validation and operation binding stay real.
        rows.append(dict(taskId='task', toolName='write_file', toolCallId=call_id, roundNum=1,
            executionEffect=dict(operationId=operation, scope='invocation', reconciliation=dict(
                kind='project_file_write', changeId='a'*32, basePath='/fixture', path='file.txt',
                taskId='task', rootIdentity=[1, 2], postDigest='a'*64, postBytes=5))))
        assert account.reserve_tool(actor) is None
    state['messages'][-1]['tool_calls'] = calls
    state['tool_recovery'] = dict(format='tofu.native-file-tool-recovery/v1', producer_task_id='task', calls=references)
    record['value']['tool_rounds'] = rows
    monkeypatch.setattr('lib.project_mod.effect_reconciliation.inspect_prepared_write',
        lambda *a, **kw: ('file_published', 'prepared_file_applied'))
    return storage, admission, record, response, reads, options, cuts[-1]


def inspect(values):
    storage, admission, _, _, _, options, cut = values
    return preflight_registered_actor_accounts(storage, cut, [admission], **options)


def test_preflight_keeps_original_snapshots_and_does_not_charge_or_write(monkeypatch):
    values = paid_batch(monkeypatch, 2)
    before = deepcopy((values[2], values[3], values[6]))
    observed = inspect(values); assert inspect(values) == observed
    actor = observed['actors']['task']
    assert actor['frontier']['snapshot']['shared_model_account']['tool_calls'] == 0
    assert observed['account_frontier']['snapshot']['tool_calls'] == 2
    assert actor['pending_file_tools']['charged_tool_calls'] == 2
    assert len(actor['pending_file_tools']['observations']) == 2
    assert (values[2], values[3], values[6]) == before
    assert all(row[0] == 'frontier' for row in values[4])


@pytest.mark.parametrize('damage', [
    'missing_metadata', 'producer', 'conversation', 'owner', 'invocation', 'scope',
    'missing_effect', 'duplicate_effect', 'wrong_call', 'wrong_round', 'wrong_task',
    'unsupported', 'missing_reference', 'duplicate_operation', 'duplicate_call',
    'incomplete_batch', 'overcharged', 'undercharged', 'negative_charge', 'completed',
    'token_drift', 'model_ordinal', 'tool_ordinal', 'control_count', 'unknown',
    'registration_race', 'receipt_race',
])
def test_partial_foreign_changed_or_unknown_proof_cannot_explain_quota(monkeypatch, damage):
    values = paid_batch(monkeypatch, 2)
    storage, _, record, response, _, _, cut = values
    state = response['state']['snapshot']; rows = record['value']['tool_rounds']
    metadata = state['tool_recovery']; ref = metadata['calls'][0]['inspection']
    account = state['shared_model_account']
    if damage == 'missing_metadata': state.pop('tool_recovery')
    elif damage == 'producer': metadata['producer_task_id'] = 'foreign'
    elif damage == 'conversation': ref['conversationId'] = 'foreign'
    elif damage == 'owner': ref['ownerScope'] = turn_image_owner_scope(8, 'conversation')
    elif damage == 'invocation': ref['invocationId'] = 'foreign'
    elif damage == 'scope': ref['turnId'] = ref.pop('invocationId')
    elif damage == 'missing_effect': rows.pop()
    elif damage == 'duplicate_effect': rows.append(deepcopy(rows[0]))
    elif damage == 'wrong_call': rows[0]['toolCallId'] = 'earlier-call'
    elif damage == 'wrong_round': rows[0]['roundNum'] = 2
    elif damage == 'wrong_task': rows[0]['taskId'] = 'foreign'
    elif damage == 'unsupported': rows[0]['toolName'] = 'bash'
    elif damage == 'missing_reference': rows[0]['executionEffect'].pop('reconciliation')
    elif damage == 'duplicate_operation': metadata['calls'][1]['inspection']['operationId'] = ref['operationId']
    elif damage == 'duplicate_call': state['messages'][-1]['tool_calls'][1]['id'] = 'write-0'
    elif damage == 'incomplete_batch': metadata['calls'].pop()
    elif damage == 'overcharged':
        cut['actors']['task']['tool_calls'] += 1; cut['snapshot']['tool_calls'] += 1
    elif damage == 'undercharged':
        cut['actors']['task']['tool_calls'] -= 1; cut['snapshot']['tool_calls'] -= 1
    elif damage == 'negative_charge': account['tool_calls'] = account['control_tool_calls'] = 3
    elif damage == 'completed': response['state']['phase'] = 'completed'; state['tools_pending'] = False
    elif damage == 'token_drift': account['tokens'] += 1
    elif damage == 'model_ordinal': account['model_ordinal'] += 1
    elif damage == 'tool_ordinal': account['tool_ordinal'] += 1
    elif damage == 'control_count': account['control_tool_calls'] = 1
    else:
        def classify(*args, **kwargs):
            if damage == 'registration_race': record['version'] += 1
            if damage == 'receipt_race': rows[0]['toolCallId'] = 'changed'
            return ('unknown', 'missing_evidence') if damage == 'unknown' else ('file_published', 'prepared_file_applied')
        monkeypatch.setattr('lib.project_mod.effect_reconciliation.inspect_prepared_write', classify)
    with pytest.raises(RecoveryRefused): inspect(values)


@pytest.mark.parametrize('damage', [None, 'swapped', 'boolean', 'missing'])
def test_preflight_uses_each_tool_counter_independently_of_resumed_model_round(monkeypatch, damage):
    values = paid_batch(monkeypatch, 2)
    record, response = values[2:4]
    state = response['state']['snapshot']
    state['next_round'] = 7
    metadata = state['tool_recovery']['calls']
    for number, (item, row) in enumerate(zip(metadata, record['value']['tool_rounds']), 3):
        item['tool_round'] = row['roundNum'] = number
    if damage == 'swapped':
        metadata[0]['tool_round'], metadata[1]['tool_round'] = 4, 3
    elif damage == 'boolean':
        metadata[0]['tool_round'] = True
    elif damage == 'missing':
        metadata[0].pop('tool_round')
    before = deepcopy((record, response, values[6]))
    if damage:
        with pytest.raises(RecoveryRefused):
            inspect(values)
    else:
        observed = inspect(values)
        assert len(observed['actors']['task']['pending_file_tools']['observations']) == 2
    assert (record, response, values[6]) == before
