"""The final model view budgets host verdicts, retained metadata and recovery."""
from dataclasses import replace
import json

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import (
    ToolResultEnvelopeV2, ToolResultProgramInputV2, ToolResultRecoveryV2,
    split_tool_result_delivery, tool_result_error, typed_tool_error, with_program_input,
)
from lib.tools.result_value import capture_result, TrustedResultText

pytestmark = pytest.mark.unit


@pytest.fixture
def byte_budget(monkeypatch):
    # A conservative deterministic counter exercises serialization boundaries.
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 1000)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', 1000)
    return 1000


@pytest.mark.parametrize('status', ['error', 'aborted', 'rejected'])
@pytest.mark.parametrize('policy', ['artifact', 'source', 'none'])
def test_host_verdict_is_counted_before_accepting_an_inline_result(byte_budget, status, policy):
    body = 'operation detail ' * 58
    assert len(body.encode()) < byte_budget
    source = TrustedResultText(body, canonical=capture_result(body, execution_status=status))
    result = _budget.budget_tool_result_v2('fixture_read', source, user_id=0, recovery_policy=policy)
    delivery = split_tool_result_delivery(result)
    assert _budget._model_result_tokens(result, '') <= byte_budget
    assert result.canonical is source.canonical and result.canonical.text() == body
    assert delivery.evidence['executionStatus'] == status
    assert delivery.evidence['status'] == 'error'
    assert delivery.evidence['viewCoverage'] == 'partial'


@pytest.mark.parametrize('typed', [False, True])
def test_large_failed_source_keeps_owner_advice_without_creating_a_source_retry(byte_budget, typed):
    failure = typed_tool_error('owned_failure', retryable=False, message='Source refused.',
                               next_action='Inspect the original outcome; correct the cause.')
    if typed:
        source = replace(failure, summary='Original "diagnostic".\n' * 200).to_model_text()
    else:
        body = 'The operation did not finish. ' * 200
        source = TrustedResultText(body, canonical=capture_result(body, execution_status='error'))
    result = _budget.budget_tool_result_v2('fixture_read', source, user_id=0, recovery_policy='source')
    delivery = split_tool_result_delivery(result)
    assert delivery.evidence['status'] == 'error'
    assert not json.loads(result).get('recovery'), 'A failed source is not a retry grant'
    assert 'rerun' not in delivery.model_text.lower()
    assert _budget._model_result_tokens(result, '') <= byte_budget
    if typed:
        assert tool_result_error(delivery.model_text) == failure.error
    else:
        assert tool_result_error(delivery.model_text) is None


def test_reprojection_counts_existing_recovery_before_fitting_diagnostics(byte_budget, monkeypatch):
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 500)
    recovery = ToolResultRecoveryV2(kind='source', tool='inspect_operation',
                                    arguments={'operationId': 'occurrence-' * 10})
    source = replace(typed_tool_error('owned_failure', retryable=False,
        message='Source refused.', next_action='Inspect this occurrence.'),
        summary='Original diagnostic. ' * 200, recovery=recovery).to_envelope_text()
    result = _budget.budget_tool_result_v2('write_file', source, user_id=0)
    assert _budget._model_result_tokens(result, '') <= 500
    assert json.loads(result)['recovery'] == json.loads(source)['recovery']
    assert tool_result_error(result) == tool_result_error(source)
    assert result.canonical is source.canonical


def test_retention_failure_notice_is_counted_in_the_delivered_view(byte_budget, monkeypatch):
    source = json.dumps({'agent_id': 'a', 'status': 'ok', 'final_answer': 'x' * 945})
    def unavailable(*args, **kwargs):
        raise OSError('private storage diagnostic')
    monkeypatch.setattr('lib.tools.result_value.persist_result', unavailable)
    result = _budget.budget_tool_result_v2('get_agent_result', source, user_id=17)
    delivery = split_tool_result_delivery(result)
    assert _budget._model_result_tokens(result, '') <= byte_budget
    assert delivery.evidence['retentionCoverage'] == 'unavailable'
    assert 'private storage diagnostic' not in delivery.model_text
    assert result.canonical.text() == source


def test_production_token_boundary_counts_the_failure_wrapper():
    body = 'operation detail ' * 3999
    source = TrustedResultText(body, canonical=capture_result(body, execution_status='error'))
    assert _budget._result_tokens(body, 'gpt-4o') <= _budget.TOOL_RESULT_V2_MAX_TOKENS
    result = _budget.budget_tool_result_v2('fixture_read', source, user_id=0, model='gpt-4o')
    assert _budget._model_result_tokens(result, 'gpt-4o') <= _budget.TOOL_RESULT_V2_MAX_TOKENS
    # A small failure wrapper must not throw away thousands of available
    # diagnostic tokens through an unrelated fixed intermediate summary cap.
    assert _budget._model_result_tokens(result, 'gpt-4o') > _budget.TOOL_RESULT_V2_MAX_TOKENS * .95
    assert result.canonical is source.canonical


@pytest.mark.parametrize('stage', ['first', 'reprojection', 'aggregate'])
def test_program_read_metadata_is_counted_before_diagnostics(byte_budget, stage, monkeypatch):
    limit = 400
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', limit)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', limit)
    body = 'original diagnostic ' * 400
    metadata = ToolResultProgramInputV2('input_' + 'a' * 32, len(body.encode()))
    if stage == 'first':
        result = _budget.budget_tool_result_v2('read_rows', body, user_id=0,
            recovery_policy='source', program_input=metadata)
    else:
        source = with_program_input(ToolResultEnvelopeV2(
            status='partial', summary=body, truncated=True).to_envelope_text(), metadata)
        if stage == 'reprojection':
            result = _budget.budget_tool_result_v2('read_rows', source, user_id=0)
        else:
            result = _budget.enforce_round_aggregate_budget_v2(
                {'read': (source, 'read_rows', 'read')}, user_id=0,
                token_budget=limit,
                recovery_by_call_id={'read': {'policy': 'source'}})['read'][0]
    delivery = split_tool_result_delivery(result)
    assert _budget._result_tokens(delivery.model_text, '') <= limit
    assert json.loads(delivery.model_text)['programInput'] == metadata.to_dict()


@pytest.mark.parametrize('typed', [False, True])
def test_failed_file_preview_preserves_every_identity_and_original_verdict(byte_budget, typed):
    body = 'original file diagnostic ' * 200
    source = (replace(typed_tool_error('file_failure', retryable=False,
        next_action='Inspect the original file failure.'), summary=body).to_model_text()
        if typed else TrustedResultText(body, canonical=capture_result(body, execution_status='error')))
    items = [{'type': 'file_read/v1', 'index': i + 1, 'path': f'src/{i}.py',
              'status': 'error', 'preview': body} for i in range(3)]
    result = _budget.budget_tool_result_v2('read_files', source, user_id=0,
        projection_items=items, recovery_policy='source')
    delivery = split_tool_result_delivery(result)
    value = json.loads(result)
    assert len(delivery.model_text.encode()) <= byte_budget
    assert value['status'] == 'error' and not value.get('recovery')
    assert 'rerun' not in delivery.model_text.lower()
    assert [(item['path'], item['status']) for item in value['items']] == [
        (f'src/{i}.py', 'error') for i in range(3)]
    assert tool_result_error(result) == tool_result_error(source)


def test_pipeline_can_withdraw_a_cached_program_reference_without_losing_original():
    metadata = ToolResultProgramInputV2('input_' + 'a' * 32, 8000)
    source = with_program_input(ToolResultEnvelopeV2(
        status='partial', summary='bounded preview', truncated=True).to_envelope_text(), metadata)
    preserved = _budget.budget_tool_result_v2('read_rows', source, user_id=0)
    assert json.loads(preserved)['programInput'] == metadata.to_dict()
    withdrawn = _budget.budget_tool_result_v2('read_rows', source, user_id=0, program_input=None)
    assert 'programInput' not in json.loads(withdrawn)
    assert withdrawn.canonical is source.canonical
