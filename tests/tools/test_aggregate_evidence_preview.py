"""Round pressure spends its remaining allowance on acquired evidence."""
from dataclasses import replace
import json

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import (
    ToolResultEnvelopeV2, split_tool_result_delivery, tool_result_error,
    tool_result_observation, typed_tool_error,
)
from lib.tools.result_value import TrustedResultText, capture_result

pytestmark = pytest.mark.unit
ARTIFACT = 'tool-result:' + 'a' * 64


@pytest.fixture
def retained(monkeypatch):
    monkeypatch.setattr(_budget, '_result_tokens', lambda text, model: len(text.encode()))
    writes = []

    def store(content, **scope):
        writes.append((content, scope))
        return ARTIFACT

    monkeypatch.setattr(_budget, '_store_tool_result_artifact', store)
    return writes


def observation(result):
    delivery = split_tool_result_delivery(result)
    return tool_result_observation(delivery.model_text, delivery.evidence)


@pytest.mark.parametrize('policy', ['artifact', 'source', 'none', 'unavailable'])
def test_round_retains_plain_evidence_inside_the_final_allowance(retained, monkeypatch, policy):
    if policy == 'unavailable':
        monkeypatch.setattr(_budget, '_store_tool_result_artifact', lambda *a, **kw: '')
    body = 'Acquired evidence: entry = {"path": "源码/🙂", "value": 9007199254740993}\n' * 200
    limit = 1800
    result = _budget.enforce_round_aggregate_budget_v2(
        {'read': (body, 'fixture_read', 'read')}, user_id=17, token_budget=limit,
        recovery_by_call_id={'read': {'policy': 'artifact' if policy == 'unavailable' else policy}},
    )['read'][0]
    view = observation(result)
    assert 'Acquired evidence' in view['summary']
    assert limit * .85 < _budget._model_result_tokens(result, '') <= limit
    assert tool_result_error(result) is None
    if policy == 'artifact':
        assert len(retained) == 1 and retained[0] == (body, {'user_id': 17, 'observed_at_ms': 0})
        cursor = int(view['cursor'])
        assert 0 < cursor < len(body.encode())
        assert view['summary'].startswith(body.encode()[:cursor].decode())
    elif policy == 'source':
        assert view['recovery']['tool'] == 'fixture_read'
        assert 'narrower' in view['summary']
        assert not retained
    else:
        assert 'prior outcome' in view['summary']
        assert not view.get('recovery') and not retained


def test_refitted_manifest_preview_keeps_safe_cursor_without_rereading_or_republishing(retained, monkeypatch):
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 4000)
    body = 'Evidence line: "源码/🙂" = 9007199254740993\n' * 200
    first = _budget.budget_tool_result_v2('fixture_read', body, user_id=17)
    first_view = observation(first)
    assert first_view['cursor'] == '0'
    result = _budget.enforce_round_aggregate_budget_v2(
        {'read': (first, 'fixture_read', 'read')}, user_id=17, token_budget=1800,
    )['read'][0]
    view = observation(result)
    assert 'Evidence line' in view['summary']
    assert view['cursor'] == '0', 'A canonical manifest is not a raw byte artifact'
    shown = view['summary'].split('\n… [truncated;')[0]
    assert body.startswith(shown) and len(shown) < len(first_view['summary'])
    assert 'points past this preview' not in view['summary']
    assert result.canonical is first.canonical and result.canonical.text() == body
    assert len(retained) == 1
    assert _budget._model_result_tokens(result, '') <= 1800


def test_structured_success_keeps_fitting_items_and_the_complete_canonical_source(retained):
    rows = [{'path': f'src/{i}.py', 'exactId': 9007199254740993 + i, 'evidence': 'verified ' * 10}
            for i in range(40)]
    body = json.dumps(rows)
    source = _budget.budget_tool_result_v2('fixture_read', body, user_id=17)
    result = _budget.enforce_round_aggregate_budget_v2(
        {'read': (source, 'fixture_read', 'read')}, user_id=17, token_budget=1800,
    )['read'][0]
    view = observation(result)
    assert view['items'] and len(view['items']) < len(rows)
    assert view['items'] == rows[:len(view['items'])]
    assert view['cursor'] == '0', 'Synthesized structured summaries are not source byte offsets'
    assert result.canonical is source.canonical and result.canonical.text() == body
    assert _budget._model_result_tokens(result, '') <= 1800
    assert len(retained) == 1


@pytest.mark.parametrize('status', ['ok', 'error', 'aborted'])
@pytest.mark.parametrize('policy', ['artifact', 'source'])
def test_file_batch_keeps_each_available_preview_and_original_outcome(retained, status, policy):
    items = tuple({'type': 'file_read/v1', 'index': i + 1, 'path': f'src/{i}.py',
                   'status': 'ok' if status == 'ok' else 'error',
                   'preview': f'File {i} original evidence. ' * 200}
                  for i in range(3))
    envelope = (typed_tool_error('file_failure', retryable=False,
                                next_action='Inspect the prior outcome.') if status == 'error'
                else ToolResultEnvelopeV2(status='ok', summary=''))
    source = replace(envelope, summary='Three file observations.', items=items).to_envelope_text()
    source = TrustedResultText(source, canonical=capture_result(source, execution_status=status),
                               envelope=True)
    result = _budget.enforce_round_aggregate_budget_v2(
        {'read': (source, 'read_files', 'read')}, user_id=17, token_budget=2200,
        recovery_by_call_id={'read': {'policy': policy}},
    )['read'][0]
    view = observation(result)
    assert [(item['path'], item['status']) for item in view['items']] == [
        (item['path'], item['status']) for item in items]
    for original, item in zip(items, view['items'], strict=True):
        assert item['preview'] and original['preview'].startswith(item['preview'])
        assert item['previewTruncated']
    assert result.canonical is source.canonical
    assert result.canonical.execution_status == status
    assert tool_result_error(result) == tool_result_error(source)
    assert _budget._model_result_tokens(result, '') <= 2200
    if status != 'ok':
        assert 'rerun' not in split_tool_result_delivery(result).model_text.lower()
        assert not view.get('recovery')


def test_round_preserves_small_siblings_and_reuses_only_available_space(retained):
    small = 'The source version is 42.'
    body = 'Large acquired evidence.\n' * 400
    original = {'small': (small, 'fixture_version', 'small'),
                'large': (body, 'fixture_read', 'large')}
    fitting = dict(original)
    _budget.enforce_round_aggregate_budget_v2(fitting, user_id=17, token_budget=20000)
    assert fitting == original and not retained
    result = _budget.enforce_round_aggregate_budget_v2(original, user_id=17, token_budget=1800)
    assert result['small'] == fitting['small']
    assert 'Large acquired evidence' in observation(result['large'][0])['summary']
    assert sum(_budget._model_result_tokens(row[0], '') for row in result.values()) <= 1800
    assert len(retained) == 1
