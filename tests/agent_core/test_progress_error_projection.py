"""Progress classifies the same failure across every supported error projection."""
from dataclasses import replace
import json

import pytest

from lib.agent_core.progress_evidence import nonretryable_failure_signature
from lib.tools.result_envelope import typed_tool_error
from lib.tools.result_value import TrustedResultText, capture_result

pytestmark = pytest.mark.unit


def batch_failure(code='source_refused'):
    return replace(typed_tool_error('tool_batch_failed', retryable=False,
        next_action='Inspect the original failed operations.'),
        summary='Original "diagnostic".\n' * 40,
        items=({'failureCodes': [code]},))


def batch_call():
    return {'function': {'name': 'execute_tools', 'arguments': {
        'calls': [{'name': 'read_file', 'arguments': {'path': 'source.py'}}]}}}


def test_batch_failure_projection_preserves_operation_and_cause_identity():
    failure = batch_failure()
    framed = failure.to_model_text()
    with pytest.raises(json.JSONDecodeError):
        json.loads(framed)
    expected = nonretryable_failure_signature(batch_call(), failure.to_envelope_text())
    assert expected
    assert nonretryable_failure_signature(batch_call(), framed) == expected
    assert nonretryable_failure_signature(batch_call(), replace(failure, summary='short').to_model_text()) == expected
    changed = nonretryable_failure_signature(batch_call(), batch_failure('source_changed').to_model_text())
    assert changed and changed != expected


@pytest.mark.parametrize('mutation', ['short_body', 'long_body', 'other_field', 'extra_metadata'])
def test_ambiguous_batch_error_text_cannot_stop_an_execution(mutation):
    framed = batch_failure().to_model_text()
    if mutation == 'short_body':
        framed = framed[:-1]
    elif mutation == 'long_body':
        framed += 'extra'
    elif mutation == 'other_field':
        framed = framed.replace('summary (text;', 'content (text;')
    else:
        framed = framed.replace('\nsummary (text;', '\n{}\nsummary (text;')
    assert nonretryable_failure_signature(batch_call(), framed) is None


@pytest.mark.parametrize('projection', ['model', 'envelope'])
def test_successful_error_example_is_data_despite_exact_failure_shape(projection):
    failure = batch_failure()
    text = str(failure.to_model_text() if projection == 'model' else failure.to_envelope_text())
    document = TrustedResultText(text, canonical=capture_result(text))
    assert document.canonical.execution_status == 'ok'
    assert nonretryable_failure_signature(batch_call(), document) is None


@pytest.mark.parametrize('status', ['ok', 'done', 'unknown', '', 'running'])
def test_nonfailure_or_missing_settlement_cannot_authorize_failure_streak(status):
    assert nonretryable_failure_signature(
        batch_call(), batch_failure().to_model_text(), execution_status=status) is None


@pytest.mark.parametrize('status', ['error', 'failed', 'rejected', 'aborted', 'cancelled', 'unanswerable'])
def test_settled_failure_keeps_original_operation_and_recovery_identity(status):
    text = str(batch_failure().to_model_text())
    expected = nonretryable_failure_signature(batch_call(), text)
    assert expected
    assert nonretryable_failure_signature(batch_call(), text, execution_status=status) == expected
    assert nonretryable_failure_signature(batch_call(), 'opaque failure', execution_status=status) is None


@pytest.mark.parametrize('tool,field', [('read_tool_artifact', 'reads'), ('search_tool_artifact', 'searches')])
@pytest.mark.parametrize('projection', ['model', 'envelope'])
def test_declared_native_batch_requires_every_cause_and_keeps_business_arguments(tool, field, projection):
    failure = batch_failure()
    text = failure.to_model_text() if projection == 'model' else failure.to_envelope_text()
    first = {'function': {'name': tool, 'arguments': {field: [{'artifact_ref': 'source', 'cursor': '0'}]}}}
    original = nonretryable_failure_signature(first, text)
    assert original
    changed = {'function': {'name': tool, 'arguments': {field: [{'artifact_ref': 'source', 'cursor': '1'}]}}}
    assert nonretryable_failure_signature(changed, text) != original
    missing_cause = {'function': {'name': tool, 'arguments': {field: [{}, {}]}}}
    assert nonretryable_failure_signature(missing_cause, text) is None
