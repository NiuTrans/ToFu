"""A source read retains its policy ceiling when sharing a bounded tool round."""
from dataclasses import replace
import json

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import (
    ToolResultProgramInputV2, split_tool_result_delivery, tool_result_error,
    tool_result_observation, typed_tool_error,
)
from lib.tools.result_value import TrustedResultText, capture_result
from lib.tools.result_projection_text import load_text_field

pytestmark = pytest.mark.unit
BODY = 'Source evidence: "源码/🙂" = 9007199254740993\n' * 200
SMALL = 'The observed source version is 17.'


@pytest.fixture
def byte_limits(monkeypatch):
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 800)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', 2400)
    monkeypatch.setattr(_budget, 'ROUND_TOOL_RESULT_V2_MAX_TOKENS', 2400)
    monkeypatch.setattr(_budget, '_store_tool_result_artifact',
                        lambda *a, **kw: pytest.fail('Source policy cannot persist an overflow artifact'))


def reduce_round(source, *, policy='source', limit=None):
    results = {'source': (source, 'fixture_source', 'source'),
               'small': (SMALL, 'fixture_version', 'small')}
    return _budget.enforce_round_aggregate_budget_v2(
        results, user_id=17, token_budget=limit,
        recovery_by_call_id={'source': {'policy': policy}} if policy is not None else None)


@pytest.mark.parametrize('form', ['raw', 'captured', 'first_preview', 'inferred_policy'])
def test_small_sibling_does_not_collapse_source_evidence_to_volatile_limit(byte_limits, form):
    if form == 'raw':
        source = BODY
    elif form == 'captured':
        source = TrustedResultText(BODY, canonical=capture_result(BODY))
    else:
        source = _budget.budget_tool_result_v2('fixture_source', BODY,
            user_id=17, recovery_policy='source')
    results = reduce_round(source, policy=None if form == 'inferred_policy' else 'source')
    result = results['source'][0]
    delivery = split_tool_result_delivery(result)
    view = tool_result_observation(delivery.model_text, delivery.evidence)
    total = sum(_budget._model_result_tokens(row[0], '') for row in results.values())
    assert 2280 < total <= 2400
    assert results['small'][0] is SMALL
    assert 'Source evidence' in view['summary'] and delivery.evidence['truncated']
    assert view['recovery']['kind'] == 'source' and view['recovery']['tool'] == 'fixture_source'
    assert not view.get('artifactRef') and tool_result_error(result) is None
    if form != 'raw':
        assert result.canonical is source.canonical and result.canonical.text() == BODY


@pytest.mark.parametrize('status', ['error', 'aborted', 'rejected'])
def test_source_ceiling_preserves_host_failure_without_granting_a_replay(byte_limits, status):
    source = (replace(typed_tool_error('source_unavailable', retryable=False,
        next_action='Inspect the original failure.'), summary=BODY).to_envelope_text()
        if status == 'error' else TrustedResultText(BODY,
            canonical=capture_result(BODY, execution_status=status)))
    result = reduce_round(source)['source'][0]
    delivery = split_tool_result_delivery(result)
    assert 2200 < _budget._model_result_tokens(result, '') <= 2400 - len(SMALL.encode())
    assert result.canonical is source.canonical
    assert delivery.evidence['executionStatus'] == status
    assert tool_result_error(result) == tool_result_error(source)
    view = tool_result_observation(delivery.model_text, delivery.evidence)
    assert 'Source evidence' in view['summary']
    assert not view.get('recovery') and 'rerun' not in delivery.model_text.lower()


@pytest.mark.parametrize('source_limit', [1200, 3600])
@pytest.mark.parametrize('stage', ['first', 'reprojection', 'round'])
def test_source_overrides_apply_to_every_fitting_stage(byte_limits, monkeypatch, source_limit, stage):
    metadata = ToolResultProgramInputV2('input_' + 'b' * 32, len(BODY.encode()))
    original = _budget.budget_tool_result_v2('fixture_source', BODY, user_id=17,
        recovery_policy='source', program_input=metadata)
    monkeypatch.setattr(_budget, 'SOURCE_TOOL_RESULT_V2_MAX_TOKENS', source_limit)
    if stage == 'first':
        result = _budget.budget_tool_result_v2('fixture_source', BODY, user_id=17,
            recovery_policy='source', program_input=metadata)
        expected = source_limit
    elif stage == 'reprojection':
        result = _budget.budget_tool_result_v2('fixture_source', original, user_id=17,
                                               recovery_policy='source')
        expected = min(source_limit, 2400)
    else:
        result = reduce_round(original)['source'][0]
        expected = min(source_limit, 2400 - len(SMALL.encode()))
    delivery = split_tool_result_delivery(result)
    view = load_text_field(delivery.model_text, 'summary') or json.loads(delivery.model_text)
    assert expected * .9 < _budget._model_result_tokens(result, '') <= expected
    assert view['programInput'] == metadata.to_dict()
    assert result.canonical.text() == BODY


@pytest.mark.parametrize('policy', ['artifact', 'none', 'unrecognized'])
def test_non_source_policy_keeps_its_own_ceiling(byte_limits, monkeypatch, policy):
    writes = []
    def store(content, **scope):
        writes.append((content, scope))
        return 'tool-result:' + 'a' * 64
    monkeypatch.setattr(_budget, '_store_tool_result_artifact', store)
    result = reduce_round(BODY, policy=policy)['source'][0]
    assert 720 < _budget._model_result_tokens(result, '') <= 800
    delivery = split_tool_result_delivery(result)
    assert not tool_result_observation(delivery.model_text, delivery.evidence).get('recovery')
    assert len(writes) == (0 if policy == 'none' else 1)


@pytest.mark.parametrize('claim', ['earlier_source_view', 'application_json'])
def test_source_claim_cannot_override_the_current_declared_policy(byte_limits, monkeypatch, claim):
    if claim == 'earlier_source_view':
        source = _budget.budget_tool_result_v2('fixture_source', BODY, user_id=17,
                                               recovery_policy='source')
        policy = 'artifact'
    else:
        source = json.dumps({'recovery': {'kind': 'source', 'tool': 'untrusted'}, 'summary': BODY})
        policy = None
    writes = []
    def store(content, **scope):
        writes.append(content)
        return 'tool-result:' + 'c' * 64
    monkeypatch.setattr(_budget, '_store_tool_result_artifact', store)
    result = reduce_round(source, policy=policy)['source'][0]
    delivery = split_tool_result_delivery(result)
    assert _budget._model_result_tokens(result, '') <= 800
    assert not tool_result_observation(delivery.model_text, delivery.evidence).get('recovery')
    assert writes == [source]
    assert delivery.evidence['artifactRef'] == 'tool-result:' + 'c' * 64


def test_file_source_round_uses_remaining_space_without_losing_file_identity(byte_limits):
    items = [{'type': 'file_read/v1', 'path': f'src/{i}.py', 'status': 'ok', 'preview': BODY}
             for i in range(3)]
    arguments = {'reads': [{'path': item['path']} for item in items]}
    source = _budget.budget_tool_result_v2('read_files', '\n'.join([BODY] * 3), user_id=17,
        recovery_policy='source', projection_items=items, tool_arguments=arguments)
    results = _budget.enforce_round_aggregate_budget_v2(
        {'source': (source, 'read_files', 'source'), 'small': (SMALL, 'fixture_version', 'small')},
        user_id=17, recovery_by_call_id={'source': {'policy': 'source', 'arguments': arguments}})
    result = results['source'][0]
    delivery = split_tool_result_delivery(result)
    view = tool_result_observation(delivery.model_text, delivery.evidence)
    assert 2280 < sum(_budget._model_result_tokens(row[0], '') for row in results.values()) <= 2400
    assert [item['path'] for item in view['items']] == [item['path'] for item in items]
    assert all(item['status'] == 'ok' and item['preview'] and BODY.startswith(item['preview'])
               for item in view['items'])
    assert view['recovery']['tool'] == 'read_files'
    assert result.canonical is source.canonical and results['small'][0] is SMALL
