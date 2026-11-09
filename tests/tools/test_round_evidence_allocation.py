"""Independent tool observations share round space without losing recovery facts."""
from collections import Counter
from dataclasses import replace
import hashlib
import json

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import (
    ToolResultEnvelopeV2, ToolResultRecoveryV2, split_tool_result_delivery, tool_result_error,
    tool_result_observation, typed_tool_error,
)
from lib.tools.result_value import TrustedResultText, capture_result

pytestmark = pytest.mark.unit


@pytest.fixture
def byte_limits(monkeypatch):
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 2400)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', 7200)
    writes = []
    def store(content, **scope):
        writes.append((content, scope))
        return 'tool-result:' + hashlib.sha256(content.encode()).hexdigest()
    monkeypatch.setattr(_budget, '_store_tool_result_artifact', store)
    return writes


def source(index, repetitions=300):
    text = f'EVIDENCE-{index}: original "源码/🙂" observation.\n' * repetitions
    return TrustedResultText(text, canonical=capture_result(text))


def observed(value):
    delivery = split_tool_result_delivery(value)
    return tool_result_observation(delivery.model_text, delivery.evidence)


def costs(results):
    return [_budget._model_result_tokens(row[0], '') for row in results.values()]


@pytest.mark.parametrize('policy', ['source', 'artifact', 'none', 'unavailable'])
def test_every_large_observation_keeps_evidence_with_one_preparation(byte_limits, monkeypatch, policy):
    original = {str(i): (source(i), 'fixture_read', str(i)) for i in range(4)}
    if policy == 'unavailable':
        def unavailable(content, **scope):
            byte_limits.append((content, scope))
            return ''
        monkeypatch.setattr(_budget, '_store_tool_result_artifact', unavailable)
    results = _budget.enforce_round_aggregate_budget_v2(dict(original), user_id=17, token_budget=7200,
        recovery_by_call_id={key: {'policy': 'artifact' if policy == 'unavailable' else policy}
                             for key in original})
    assert 7000 < sum(costs(results)) <= 7200
    assert max(costs(results)) - min(costs(results)) < 80
    for key, (result, name, call_id) in results.items():
        assert f'EVIDENCE-{key}' in observed(result)['summary']
        assert result.canonical is original[key][0].canonical
        assert (name, call_id) == ('fixture_read', key)
        assert result.canonical.execution_status == 'ok' and tool_result_error(result) is None
    assert len(byte_limits) == (4 if policy in ('artifact', 'unavailable') else 0)
    assert all(count == 1 for count in Counter(str(value) for value, _ in byte_limits).values())
    assert all(scope['user_id'] == 17 for _, scope in byte_limits)


def test_small_results_release_space_and_declared_caps_remain_effective(byte_limits):
    small = 'Exact source version 17.'
    inputs = {'source': (source('source'), 'fixture_read', 'source'),
              'artifact': (source('artifact'), 'fixture_read', 'artifact'),
              'small': (small, 'fixture_version', 'small')}
    result = _budget.enforce_round_aggregate_budget_v2(inputs, user_id=17, token_budget=7200,
        recovery_by_call_id={'source': {'policy': 'source'}, 'artifact': {'policy': 'artifact'}})
    assert result['small'][0] is small
    assert 7000 < sum(costs(result)) <= 7200
    assert 2300 < _budget._model_result_tokens(result['artifact'][0], '') <= 2400
    assert _budget._model_result_tokens(result['source'][0], '') > 4500
    assert 'EVIDENCE-source' in observed(result['source'][0])['summary']
    assert 'EVIDENCE-artifact' in observed(result['artifact'][0])['summary']
    assert len(byte_limits) == 1


def test_allocation_is_independent_of_completion_order(byte_limits):
    inputs = {str(i): (source(i), 'fixture_read', str(i)) for i in range(4)}
    policy = {key: {'policy': 'source'} for key in inputs}
    first = _budget.enforce_round_aggregate_budget_v2(dict(inputs), user_id=17,
        token_budget=7200, recovery_by_call_id=policy)
    second = _budget.enforce_round_aggregate_budget_v2(dict(reversed(list(inputs.items()))),
        user_id=17, token_budget=7200, recovery_by_call_id=policy)
    assert first == second
    assert all(f'EVIDENCE-{key}' in observed(row[0])['summary'] for key, row in first.items())
    assert not byte_limits


def test_failed_mandatory_retention_reserves_original_before_other_previews(byte_limits, monkeypatch):
    locked = TrustedResultText('LOCKED OUTCOME. ' * 200,
                               canonical=capture_result('LOCKED OUTCOME. ' * 200))
    store = _budget._store_tool_result_artifact
    def retain(content, **scope):
        if content is locked:
            byte_limits.append((content, scope))
            return ''
        return store(content, **scope)
    monkeypatch.setattr(_budget, '_store_tool_result_artifact', retain)
    inputs = {'locked': (locked, 'write_file', 'locked'),
              'left': (source('left'), 'fixture_read', 'left'),
              'right': (source('right'), 'fixture_read', 'right')}
    result = _budget.enforce_round_aggregate_budget_v2(inputs, user_id=17, token_budget=5000,
                                                      require_retention=True)
    assert result['locked'][0] is locked
    assert sum(costs(result)) <= 5000
    assert 'EVIDENCE-left' in observed(result['left'][0])['summary']
    assert 'EVIDENCE-right' in observed(result['right'][0])['summary']
    assert len(byte_limits) == 3


def test_typed_cause_identifiers_are_reserved_before_optional_diagnostics(byte_limits):
    causes = ({'failureCodes': [f'owned_cause_{i}_' + 'x' * 180 for i in range(3)]},)
    failure = replace(typed_tool_error('tool_batch_failed', retryable=False,
        next_action='Inspect the three rejected operations.'),
        summary='Original failed operation diagnostic. ' * 500, items=causes).to_envelope_text()
    result = _budget.enforce_round_aggregate_budget_v2(
        {'failed': (failure, 'execute_tools', 'failed'),
         'left': (source('left'), 'fixture_read', 'left'),
         'right': (source('right'), 'fixture_read', 'right')}, user_id=17, token_budget=3200,
        recovery_by_call_id={'left': {'policy': 'source'}, 'right': {'policy': 'source'}})
    failed = result['failed'][0]
    assert observed(failed)['items'] == list(causes)
    assert tool_result_error(failed) == tool_result_error(failure)
    assert failed.canonical is failure.canonical
    assert 'rerun' not in split_tool_result_delivery(failed).model_text.lower()
    assert sum(costs(result)) <= 3200
    assert all(f'EVIDENCE-{key}' in observed(result[key][0])['summary'] for key in ('left', 'right'))


def test_unused_atomic_item_space_is_reclaimed_without_repeating_retention(byte_limits):
    inputs = {}
    for i in range(4):
        items = [{'record': i, 'evidence': 'original field ' * 115}]
        envelope = ToolResultEnvelopeV2(status='ok', summary='One acquired record.', items=tuple(items))
        text = envelope.to_envelope_text()
        text = TrustedResultText(text, canonical=capture_result(json.dumps(items)), envelope=True)
        inputs[str(i)] = (text, 'fixture_read', str(i))
    result = _budget.enforce_round_aggregate_budget_v2(dict(inputs), user_id=17, token_budget=4000)
    retained = [observed(row[0]).get('items') for row in result.values()]
    assert any(retained), 'Discrete item gaps must not strand the whole remaining budget'
    assert sum(costs(result)) <= 4000
    for key, row in result.items():
        assert row[0].canonical is inputs[key][0].canonical
        if observed(row[0]).get('items'):
            assert observed(row[0])['items'] == json.loads(inputs[key][0].canonical.text())
    assert len(byte_limits) <= 4
    assert all(count == 1 for count in Counter(str(value) for value, _ in byte_limits).values())


def test_fitting_round_keeps_original_carriers_without_storage(byte_limits):
    inputs = {'tiny': ('Completed once.', 'write_file', 'tiny')}
    original = dict(inputs)
    assert _budget.enforce_round_aggregate_budget_v2(inputs, user_id=17, token_budget=500) == original
    assert not byte_limits


@pytest.mark.parametrize('policy', ['source', 'artifact'])
def test_each_file_in_concurrent_batches_keeps_acquired_evidence(byte_limits, policy):
    inputs = {}
    for batch in range(4):
        items = tuple({'type': 'file_read/v1', 'path': f'src/{batch}/{index}.py',
                       'status': 'ok', 'preview': f'FILE-{batch}-{index}: acquired source.\n' * 150}
                      for index in range(2))
        value = ToolResultEnvelopeV2(status='ok', summary='Two file observations.',
                                     items=items).to_envelope_text()
        inputs[str(batch)] = (value, 'read_files', str(batch))
    result = _budget.enforce_round_aggregate_budget_v2(dict(inputs), user_id=17, token_budget=6000,
        recovery_by_call_id={key: {'policy': policy} for key in inputs})
    assert sum(costs(result)) <= 6000
    for key, row in result.items():
        items = observed(row[0])['items']
        assert len(items) == 2 and row[0].canonical is inputs[key][0].canonical
        for index, item in enumerate(items):
            assert item['path'] == f'src/{key}/{index}.py' and item['status'] == 'ok'
            assert item['preview'].startswith(f'FILE-{key}-{index}:') and item['previewTruncated']
    assert len(byte_limits) == (4 if policy == 'artifact' else 0)


def test_unknown_producer_recovery_reserves_the_original_view(byte_limits):
    original = replace(typed_tool_error('owned_failure', retryable=False,
        next_action='Inspect this occurrence.'), summary='LOCKED DIAGNOSTIC. ' * 90).to_envelope_text()
    value = json.loads(original)
    value['recovery'] = {'kind': 'future_recovery', 'tool': 'inspect_operation'}
    locked = TrustedResultText(json.dumps(value), canonical=original.canonical, envelope=True)
    result = _budget.enforce_round_aggregate_budget_v2(
        {'locked': (locked, 'write_file', 'locked'),
         'left': (source('left'), 'fixture_read', 'left'),
         'right': (source('right'), 'fixture_read', 'right')}, user_id=17, token_budget=4000,
        recovery_by_call_id={'left': {'policy': 'source'}, 'right': {'policy': 'source'}})
    assert result['locked'][0] is locked
    assert sum(costs(result)) <= 4000
    assert all(f'EVIDENCE-{key}' in observed(result[key][0])['summary'] for key in ('left', 'right'))
    assert not byte_limits


def test_irreducible_recovery_facts_remain_explicit_above_an_impossible_budget(byte_limits):
    originals = {str(i): replace(typed_tool_error('owned_failure', retryable=False,
        next_action='Inspect the original outcome.'), summary='Original diagnostic. ' * 300,
        recovery=ToolResultRecoveryV2(kind='source', tool='inspect_operation',
                                     arguments={'operationId': str(i) * 600})).to_envelope_text()
        for i in range(2)}
    result = _budget.enforce_round_aggregate_budget_v2(
        {key: (value, 'write_file', key) for key, value in originals.items()},
        user_id=17, token_budget=200)
    assert sum(costs(result)) > 200, 'Required recovery alone exceeds the requested limit'
    for key, row in result.items():
        assert row[0].canonical is originals[key].canonical
        assert observed(row[0])['recovery']['arguments'] == {'operationId': key * 600}
        assert tool_result_error(row[0]) == tool_result_error(originals[key])
        assert len(str(row[0])) < len(str(originals[key]))
    assert not byte_limits


def test_failed_verdict_republication_is_not_repeated_during_allocation(byte_limits, monkeypatch):
    body = 'FAILED OCCURRENCE: inspect prior outcome.\n' * 300
    original = ToolResultEnvelopeV2(status='ok', summary=body,
        artifact_ref='tool-result:' + 'a' * 64).to_envelope_text()
    failed = TrustedResultText(original, canonical=capture_result(body, execution_status='error'),
                                envelope=True)
    def unavailable(content, **scope):
        byte_limits.append((content, scope))
        return ''
    monkeypatch.setattr(_budget, '_store_tool_result_artifact', unavailable)
    result = _budget.enforce_round_aggregate_budget_v2(
        {'failed': (failed, 'write_file', 'failed'),
         'left': (source('left'), 'fixture_read', 'left'),
         'right': (source('right'), 'fixture_read', 'right')}, user_id=17, token_budget=3000,
        recovery_by_call_id={'left': {'policy': 'source'}, 'right': {'policy': 'source'}})
    assert len(byte_limits) == 1
    assert result['failed'][0].canonical is failed.canonical
    delivery = split_tool_result_delivery(result['failed'][0])
    assert delivery.evidence['executionStatus'] == 'error'
    assert delivery.evidence['retentionCoverage'] == 'unavailable'
    assert not delivery.evidence.get('artifactRef') and tool_result_error(result['failed'][0]) is None
    assert 'FAILED OCCURRENCE' in observed(result['failed'][0])['summary']
    assert all(f'EVIDENCE-{key}' in observed(result[key][0])['summary'] for key in ('left', 'right'))
    assert sum(costs(result)) <= 3000
