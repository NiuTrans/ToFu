"""An earlier model envelope cannot supersede the final host execution facts."""
from dataclasses import replace
import json

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import (
    ToolResultProgramInputV2, ToolResultRecoveryV2, split_tool_result_delivery,
    tool_result_error, typed_tool_error, with_program_input,
)
from lib.tools.result_value import capture_result, TrustedResultText

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('outcome', ['error', 'aborted', 'rejected', 'cancelled', 'failed'])
@pytest.mark.parametrize('limit', [1000, 500])
def test_existing_success_view_uses_final_verdict_before_fast_return_or_fitting(
        monkeypatch, outcome, limit):
    body = 'operation detail ' * 58
    first = _budget.budget_tool_result_v2('fixture_read', body, user_id=0,
        producer_metadata={'sourceCoverage': 'complete'})
    first = with_program_input(first, ToolResultProgramInputV2('input_' + 'a' * 32, len(body)))
    final = capture_result(first, execution_status=outcome, source_coverage='partial')
    source = TrustedResultText(first, canonical=final, envelope=True)
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', limit)
    result = _budget.budget_tool_result_v2('fixture_read', source, user_id=0)
    delivery = split_tool_result_delivery(result)
    assert delivery.evidence['status'] == 'error'
    assert delivery.evidence['executionStatus'] == outcome
    assert delivery.evidence['sourceCoverage'] == 'partial'
    assert delivery.evidence['resultId'] == final.result_id
    assert len(delivery.model_text.encode()) <= limit
    assert 'programInput' not in json.loads(result)
    assert tool_result_error(result) is None
    assert result.canonical is final and result.canonical.text() == body
    assert split_tool_result_delivery(first).evidence['status'] == 'ok'


def test_final_host_verdict_keeps_the_producer_error_and_explicit_inspection():
    failure = replace(typed_tool_error('read_interrupted', retryable=True,
        message='Original producer diagnostic.', next_action='Inspect this occurrence.'),
        recovery=ToolResultRecoveryV2(kind='source', tool='inspect_operation',
                                      arguments={'operationId': 'original'})).to_envelope_text()
    final = capture_result(failure, execution_status='aborted')
    source = TrustedResultText(failure, canonical=final, envelope=True)
    result = _budget.budget_tool_result_v2('fixture_read', source, user_id=0)
    delivery = split_tool_result_delivery(result)
    assert delivery.evidence['executionStatus'] == 'aborted'
    assert tool_result_error(result) == tool_result_error(failure)
    assert json.loads(result)['recovery'] == json.loads(failure)['recovery']
    assert result.canonical is final


def test_consistent_envelope_reuse_is_byte_stable_without_storage(monkeypatch):
    first = _budget.budget_tool_result_v2('fixture_read', 'unchanged observation', user_id=0)
    def forbidden(*args, **kwargs):
        pytest.fail('Consistent inline reuse must not acquire storage')
    monkeypatch.setattr('lib.tools.result_value.persist_result', forbidden)
    assert _budget.budget_tool_result_v2('fixture_read', first, user_id=0) is first


@pytest.mark.parametrize('limit', [400, 10000])
def test_round_admission_reconciles_an_existing_view_before_its_fast_path(monkeypatch, limit):
    body = 'original observation ' * 40
    first = _budget.budget_tool_result_v2('fixture_read', body, user_id=0)
    final = capture_result(first, execution_status='rejected')
    source = TrustedResultText(first, canonical=final, envelope=True)
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    result = _budget.enforce_round_aggregate_budget_v2(
        {'read': (source, 'fixture_read', 'read')}, user_id=0, token_budget=limit,
        recovery_by_call_id={'read': {'policy': 'none'}})['read'][0]
    delivery = split_tool_result_delivery(result)
    assert delivery.evidence['status'] == 'error'
    assert delivery.evidence['executionStatus'] == 'rejected'
    assert len(delivery.model_text.encode()) <= limit
    assert result.canonical is final
    if limit > len(body.encode()):
        assert delivery.evidence['viewCoverage'] == 'complete' and body in delivery.model_text


def test_new_host_source_coverage_does_not_reclassify_a_successful_observation():
    first = _budget.budget_tool_result_v2('fixture_read', 'observed prefix', user_id=0,
        producer_metadata={'sourceCoverage': 'complete'})
    result = _budget.budget_tool_result_v2('fixture_read', first, user_id=0,
        producer_metadata={'sourceCoverage': 'partial'})
    delivery = split_tool_result_delivery(result)
    assert delivery.model_text == 'observed prefix'
    assert delivery.evidence['status'] == 'ok'
    assert delivery.evidence['sourceCoverage'] == result.canonical.source_coverage == 'partial'
    assert result.canonical.result_id == first.canonical.result_id
    assert result.canonical.text() == first.canonical.text()


@pytest.mark.parametrize('typed', [False, True])
@pytest.mark.parametrize('policy', ['source', 'none'])
def test_aggregate_uses_canonical_failure_even_before_single_result_projection(monkeypatch, typed, policy):
    body = 'original diagnostic ' * 400
    source = (replace(typed_tool_error('read_interrupted', retryable=False,
        next_action='Inspect the original outcome.'), summary=body).to_model_text()
        if typed else TrustedResultText(body, canonical=capture_result(body, execution_status='error')))
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    result = _budget.enforce_round_aggregate_budget_v2(
        {'read': (source, 'fixture_read', 'read')}, user_id=0, token_budget=500,
        recovery_by_call_id={'read': {'policy': policy}})['read'][0]
    delivery = split_tool_result_delivery(result)
    assert delivery.evidence['status'] == 'error'
    assert delivery.evidence['executionStatus'] == 'error'
    assert not json.loads(result).get('recovery')
    assert 'rerun' not in delivery.model_text.lower()
    assert tool_result_error(result) == tool_result_error(source)
    assert len(delivery.model_text.encode()) <= 500
    assert result.canonical is source.canonical
