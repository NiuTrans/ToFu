"""Small structured facts release the rest of their allowance to source text."""
from dataclasses import replace
import hashlib

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import (
    ToolResultEnvelopeV2, ToolResultRecoveryV2, split_tool_result_delivery,
    tool_result_error, tool_result_observation, typed_tool_error,
)
from lib.tools.result_value import TrustedResultText, capture_result

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('stage', ['single', 'aggregate'])
@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('policy', ['artifact', 'source'])
def test_small_structured_facts_leave_the_remaining_space_for_evidence(
        monkeypatch, stage, failed, policy):
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 2400)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', 2400)
    writes = []
    def retain(content, **kwargs):
        writes.append(content)
        return 'tool-result:' + hashlib.sha256(content.encode()).hexdigest()
    monkeypatch.setattr(_budget, '_store_tool_result_artifact', retain)
    envelope = (typed_tool_error('prior_operation_unknown', retryable=False,
        next_action='Inspect the previous outcome before deciding whether to retry.')
        if failed else ToolResultEnvelopeV2(status='ok', summary=''))
    recovery = (ToolResultRecoveryV2(kind='source', tool='inspect_operation',
                                   arguments={'operationId': 9007199254740993})
                if policy == 'source' else None)
    facts = ({'failureCodes': ['operation_42_unknown'], 'exactValue': 9007199254740993},)
    body = 'EVIDENCE: original "源码/🙂" diagnostic from the acquired result.\n' * 400
    source = replace(envelope, summary=body, items=facts, recovery=recovery).to_envelope_text()
    source = TrustedResultText(source, canonical=capture_result(source), envelope=True)
    if stage == 'single':
        result = _budget.budget_tool_result_v2('fixture_operation', source, user_id=17,
                                               recovery_policy=policy)
    else:
        result = _budget.enforce_round_aggregate_budget_v2(
            {'call': (source, 'fixture_operation', 'call')}, user_id=17, token_budget=2400,
            recovery_by_call_id={'call': {'policy': policy}})['call'][0]
    delivery = split_tool_result_delivery(result)
    observed = tool_result_observation(delivery.model_text, delivery.evidence)
    assert 2280 < len(delivery.model_text.encode()) <= 2400
    assert 'EVIDENCE:' in observed['summary'] and observed['items'] == list(facts)
    assert result.canonical is source.canonical
    assert tool_result_error(result) == tool_result_error(source)
    assert observed['status'] == ('error' if failed else 'partial')
    assert len(writes) == (1 if policy == 'artifact' else 0)
    if failed and policy == 'source':
        assert observed['recovery']['arguments'] == {'operationId': 9007199254740993}
