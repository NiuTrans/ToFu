"""Failure deduplication binds the complete request and quote, not just its model."""
from copy import deepcopy

import pytest

from lib.llm.stream_result import ProviderStreamResult

pytestmark = pytest.mark.unit


def test_a_fresh_valid_quote_is_not_suppressed_by_an_old_quote_refusal(monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _summary as summary

    task = {'id': 'quote-fixture-task', 'convId': 'quote-fixture-conversation',
            '_userId': 41, 'aborted': False, 'model': 'fixture-summary',
            'config': {'model': 'fixture-summary', 'compaction': {'summaryPolicy': 'main'}}}
    route = {'modelId': 'fixture-summary', 'providerId': 'fixture-provider',
             'policy': 'main', 'reason': 'main_explicit', 'projectedCostUsd': 0.02}
    old_quote = {**route, 'projectedCostUsd': 0.01}
    source = [{'role': 'user', 'content': 'Keep the complete original objective.'}]
    original = deepcopy(source)
    monkeypatch.setattr('lib.tasks_pkg.compaction._summary_routing.select_summary_route',
                        lambda *args, **kwargs: dict(route))
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12_000)
    calls = []

    def stream(*args, **kwargs):
        calls.append(kwargs)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nKeep the complete original objective.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    assert summary._generate_query_aware_summary(
        source, 'continue', task=task, expected_route=old_quote,
        usage_out=usage, on_delta=lambda chunk: None) is None
    failed_key = usage['_summaryRequestKey']
    assert calls == [] and source == original
    result = summary._generate_query_aware_summary(
        source, 'continue', task=task, expected_route=route,
        usage_out=usage, on_delta=lambda chunk: None)
    assert result == '### Objective\nKeep the complete original objective.'
    assert len(calls) == 1 and source == original
    assert usage['_summaryRequestKey'] != failed_key
    assert '_summaryFailure' not in usage


def test_retained_parent_members_are_part_of_the_private_request_identity():
    from lib.tasks_pkg.compaction._layer2._summary import _summary_request_key

    route = {'modelId': 'fixture-summary', 'providerId': 'fixture-parent', 'policy': 'main',
             '_parentRouteIdentities': [{'owner_user_id': 41, 'tenant_id': None,
                 'revision': 9, 'credentialId': 'primary'}, {'owner_user_id': 41,
                 'tenant_id': None, 'revision': 9, 'credentialId': 'secondary'}]}
    arguments = {'messages': [{'role': 'user', 'content': 'Stable source.'}],
                 'owner_user_id': 41, 'model': 'fixture-summary',
                 'pinned_provider': None, 'codex_provider': '', 'config': {}}
    previous = _summary_request_key(summary_route=route, **arguments)
    changed = deepcopy(route)
    changed['_parentRouteIdentities'][1]['credentialId'] = 'new-secondary'
    assert _summary_request_key(summary_route=changed, **arguments) != previous
