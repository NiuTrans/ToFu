"""Progress classifies the same failure across every supported error projection."""
from dataclasses import replace
import json

import pytest

from lib.agent_core.progress_evidence import nonretryable_failure_signature
from lib.tools.result_envelope import typed_tool_error

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
