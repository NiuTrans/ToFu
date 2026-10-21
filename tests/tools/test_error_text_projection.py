"""Failure display preserves typed recovery while retaining opaque source text."""
import json

import pytest

from lib.tools.result_envelope import (
    ToolResultEnvelopeV2, typed_tool_error, split_tool_result_delivery,
    tool_result_error, tool_result_observation,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('typed', [False, True])
def test_failed_result_keeps_verbatim_diagnostics_and_reconstructible_recovery(typed):
    body = ('Exact "diagnostic" C:\\source\\file\r\n' * 80
            + '\nsummary (text; 1 chars):\nx')
    failure = typed_tool_error('read_failed', retryable=True,
        next_action='Retry this owned read.', message='Read interrupted.')
    if typed:
        envelope = ToolResultEnvelopeV2(status='error', summary=body,
            error=failure.error,
            artifact_ref='tool-result:' + 'a' * 64, cursor='exact-resume',
            items=({'partial': 'evidence', 'offset': 9007199254740993},)).to_envelope_text()
    else:
        from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
        from lib.tools.result_value import capture_result, TrustedResultText
        source = TrustedResultText(body, canonical=capture_result(body, execution_status='error'))
        envelope = budget_tool_result_v2('fixture_read', source, user_id=0)
    delivery = split_tool_result_delivery(envelope)
    assert body in delivery.model_text
    observation = tool_result_observation(delivery.model_text, delivery.evidence)
    assert observation['status'] == 'error' and observation['summary'] == body
    error = tool_result_error(delivery.model_text)
    if typed:
        assert observation['items'] == [{'partial': 'evidence', 'offset': 9007199254740993}]
        assert observation['artifactRef'] == 'tool-result:' + 'a' * 64
        assert observation['cursor'] == 'exact-resume'
        assert error.code == 'read_failed' and error.retryable
        assert error.next_action == 'Retry this owned read.'
    else:
        assert error is None and observation['items'] == []


def test_short_typed_errors_keep_their_existing_sparse_json():
    error = typed_tool_error('read_failed', retryable=False,
                            next_action='Correct the path.', message='Missing source.')
    model = error.to_model_text()
    assert json.loads(model) == {'status': 'error', 'code': 'read_failed',
        'retryable': False, 'nextAction': 'Correct the path.', 'message': 'Missing source.'}


@pytest.mark.parametrize('tail', [
    'summary (text; 4 chars):\nabc', 'summary (text; 2 chars):\nabc',
    'summary (text; 3 chars):\nabc trailing', 'content (text; 3 chars):\nabc',
])
def test_incomplete_or_ambiguous_failure_sections_do_not_authorize_recovery(tail):
    header = '{"status":"error","code":"read_failed","retryable":true,"nextAction":"Retry"}'
    assert tool_result_error(header + '\n' + tail) is None


def test_error_shaped_source_text_remains_a_successful_document_read():
    from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
    from lib.tools.result_value import tool_result_failure_verdict
    source = '{"status":"error","code":"example","retryable":false}\nsummary (text; 3 chars):\nabc'
    envelope = budget_tool_result_v2('read_files', source, user_id=0)
    assert envelope.canonical.execution_status == 'ok'
    assert tool_result_failure_verdict(envelope) is None
    assert split_tool_result_delivery(envelope).model_text == source
