"""Typed summary failures retain safe diagnostic facts, never provider bodies."""
from copy import deepcopy

import pytest

from lib.llm.llm_errors import PermissionError_, RateLimitError, RetryableAPIError
from lib.llm_dispatch._api_errors import DispatchNoAdmissibleSlot, DispatchRateLimitBudgetExceeded
from lib.llm.stream_result import ProviderStreamResult

pytestmark = pytest.mark.unit


class SpoofedStatusError(RuntimeError):
    @property
    def status_code(self):
        raise AssertionError('Untyped exception attributes are not provider evidence')


@pytest.mark.parametrize('failure,expected', [
    (RateLimitError('private-provider-body', status_code=429),
     {'code': 'summary_rate_limited', 'statusCode': 429}),
    (RateLimitError('private-provider-body'), {'code': 'summary_rate_limited'}),
    (RateLimitError('private-provider-body', status_code=503, is_gateway=True),
     {'code': 'summary_provider_unavailable', 'statusCode': 503}),
    (RetryableAPIError('private-provider-body', status_code=502),
     {'code': 'summary_provider_unavailable', 'statusCode': 502}),
    (PermissionError_('private-provider-body', status_code=403),
     {'code': 'summary_permission_denied', 'statusCode': 403}),
    (DispatchNoAdmissibleSlot('private-provider-body'),
     {'code': 'summary_route_unavailable', 'attempts': 0}),
    (DispatchRateLimitBudgetExceeded(RateLimitError(status_code=429), attempts=2, limit=2),
     {'code': 'summary_rate_limited', 'statusCode': 429, 'attempts': 2, 'limit': 2}),
    (SpoofedStatusError('private-provider-body'), {'code': 'summary_dispatch_failed'}),
    (None, {'code': 'summary_empty'}),
    ('length', {'code': 'summary_incomplete'}),
])
def test_summary_settles_current_typed_failure_and_preserves_source(
        monkeypatch, caplog, failure, expected):
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    from lib.tasks_pkg.compaction._summary_budget import summary_failure_reason
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure

    source = [{'role': 'user', 'content': 'Preserve every original objective and constraint.'}]
    original = deepcopy(source)
    task = {'id': 'failure-fixture-task', 'convId': 'failure-fixture-conversation',
            '_userId': 41, 'aborted': False, 'model': 'fixture-summary',
            'config': {'model': 'fixture-summary', 'compaction': {'summaryPolicy': 'main'}}}
    route = {'modelId': 'fixture-summary', 'providerId': 'fixture-provider',
             'policy': 'main', 'reason': 'main_explicit', 'projectedCostUsd': None}
    monkeypatch.setattr('lib.tasks_pkg.compaction._summary_routing.select_summary_route',
                        lambda *args, **kwargs: route)
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12_000)
    calls = []

    def stream(*args, **kwargs):
        calls.append(kwargs)
        if isinstance(failure, Exception):
            raise failure
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Partial text' if failure == 'length' else '  '},
            'length' if failure == 'length' else 'stop',
            {'prompt_tokens': 20, 'completion_tokens': 10})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    assert summary._generate_query_aware_summary(
        source, 'continue', task=task, usage_out=usage, on_delta=lambda chunk: None) is None
    assert source == original and len(calls) == 1
    assert summary_failure_reason(task) == expected['code']
    assert usage.get('_summaryFailure') == expected
    assert reconstructed_summary_failure(task, summary_usage=usage) is None
    receipt = build_compaction_receipt(
        trigger='working_set', status='failed', strategy='unchanged',
        implementation='summary_dispatch', summary_usage=usage,
        outcome_reason=summary_failure_reason(task))
    assert receipt['summary']['failure'] == expected
    assert receipt['summary']['accepted'] is False
    assert 'private-provider-body' not in caplog.text
    assert 'private-provider-body' not in str(receipt)
