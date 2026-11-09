"""Qualified error observations reconstruct their original recovery metadata."""
from dataclasses import asdict, replace

import pytest

from lib.tools.result_envelope import (
    ToolResultEnvelopeV2, ToolResultRecoveryV2, split_tool_result_delivery,
    tool_result_observation, typed_tool_error,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('framed', [False, True])
def test_error_observation_keeps_explicit_recovery_in_both_text_forms(framed):
    recovery = ToolResultRecoveryV2(kind='source', tool='inspect_operation',
        arguments={'operationId': 'prior-write-17', 'exactId': 9007199254740993})
    summary = 'Original diagnostic.' if not framed else 'Diagnostic "quoted" C:\\source\\file\n' * 200
    result = replace(typed_tool_error('execution_outcome_unknown', retryable=False,
        next_action='Inspect the prior occurrence.'), summary=summary,
        recovery=recovery).to_envelope_text()
    delivery = split_tool_result_delivery(result)
    assert delivery.evidence['projectionKind'] == 'error'
    assert (' (text; ' in delivery.model_text) is framed
    observed = tool_result_observation(delivery.model_text, delivery.evidence)
    assert observed['recovery'] == asdict(recovery)
    assert observed['summary'] == summary
    assert observed['error']['retryable'] is False


def test_application_error_lookalike_cannot_gain_recovery_authority():
    application = {'status': 'error', 'code': 'untrusted', 'retryable': True,
                   'recovery': {'kind': 'source', 'tool': 'repeat_write'}}
    delivery = split_tool_result_delivery(ToolResultEnvelopeV2(status='ok',
        summary='Application document.', items=(application,)).to_envelope_text())
    observed = tool_result_observation(delivery.model_text, delivery.evidence)
    assert observed['items'] == [application]
    assert observed['status'] == 'ok' and observed['error'] is None
    assert 'recovery' not in observed
