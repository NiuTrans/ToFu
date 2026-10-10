"""Safety capacities are not prices; only complete summaries replace context."""
from copy import deepcopy

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('finish', ['stop', 'length'])
def test_planned_summary_allowance_reaches_transport_without_accepting_truncation(monkeypatch, finish):
    import lib.llm_dispatch as dispatch
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    calls = []
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    def chat(*args, **kwargs):
        calls.append(kwargs)
        return '### Objective\nPreserve the recovery state.', {'prompt_tokens': 100,
            'completion_tokens': 30, 'finish_reason': finish}
    monkeypatch.setattr(dispatch, 'dispatch_chat', chat)
    source = [{'role': 'user', 'content': 'Preserve the recovery state.'}]
    result = summary._generate_query_aware_summary(
        source, 'continue', task={'_userId': 1, 'config': {'model': 'test-model'}},
        output_budget_tokens=36000)
    assert len(calls) == 1 and calls[0]['max_tokens'] == 36000
    assert bool(result) == (finish == 'stop')


@pytest.mark.parametrize('finish', ['length', 'max_tokens', 'content_filter', 'tool_calls', ''])
def test_nonstream_incomplete_output_is_rejected_and_charged(monkeypatch, finish):
    import lib.llm_dispatch as dispatch
    from lib.tasks_pkg.compaction import _compaction_usage
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(dispatch, 'dispatch_chat', lambda *a, **kw: (
        '### Objective\nIncomplete', {'prompt_tokens': 100, 'completion_tokens': 30,
                                     'finish_reason': finish}))
    charged = []
    monkeypatch.setattr(_compaction_usage, 'record_compaction_usage',
                        lambda *a, **kw: charged.append(a))
    task = {'_userId': 1, 'config': {'model': 'test-model'}}
    source = [{'role': 'user', 'content': 'All source evidence must survive.'}]
    original = deepcopy(source)
    assert summary._generate_query_aware_summary(source, 'continue', task=task) is None
    assert source == original
    assert len(charged) == 1
    assert '_summaryAttemptBudget' not in task


@pytest.mark.parametrize('round_num', [0, 1, 10])
def test_first_round_and_economic_target_do_not_reduce_route_capacity(monkeypatch, round_num):
    from lib.tasks_pkg.compaction import _admission_budget as policy
    import lib.llm_dispatch.context_policy as routes
    monkeypatch.setattr(routes, 'task_context_profile', lambda task: {
        'window': 1_050_000, 'source': 'declared_route'})
    monkeypatch.setattr(policy, '_compaction_trigger_threshold',
                        lambda *a, **kw: (272_000, 966_000, 272_000))
    task = {'config': {'compaction': {'workingSetTokens': 272_000}}}
    assert policy.resolve_admission_budget(task, round_num=round_num) == (966_000, 966_000)
    assert task['_contextBudgetDecision']['economicTargetTokens'] == 272_000
    assert task['_contextBudgetDecision']['contextWindowTokens'] == 1_050_000


def test_receipt_keeps_capacity_pricing_target_and_cost_unknown_separate():
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt
    receipt = build_compaction_receipt(
        trigger='dispatch_guard', status='applied', strategy='model_summary',
        implementation='model_summary', summary_generated=True, summary_text='Complete summary',
        context_budget={'contextWindowTokens': 1_050_000, 'hardCeilingTokens': 966_000,
                        'totalTokens': 975_000, 'toolSchemaTokens': 15_000,
                        'economicTargetTokens': 272_000, 'decisionKind': 'safety'})
    assert receipt['contextBudget'] == {
        'routeWindowTokens': 1_050_000, 'hardCeilingTokens': 966_000,
        'renderedPromptTokens': 975_000, 'toolSchemaTokens': 15_000,
        'economicTargetTokens': 272_000, 'reserveTokens': 84_000,
        'decisionKind': 'safety', 'contextWindowSource': ''}
    assert receipt['summary']['cost']['costUsd'] is None
    assert receipt['summary']['complete'] is True


@pytest.mark.parametrize('streaming', [False, True])
def test_selected_summary_route_is_strict_and_pin_is_restored(monkeypatch, streaming):
    import lib.llm_dispatch as dispatch
    from lib.llm.stream_result import ProviderStreamResult
    from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
    from lib.tasks_pkg.compaction import _summary_routing
    from lib.tasks_pkg.compaction._layer2 import _summary as summary

    selected = {'modelId': 'qualified-summary', 'providerId': 'owner-route-group',
                'reason': 'qualified_lower_projected_cost',
                'policy': 'qualified-cheapest', 'projectedCostUsd': 0.01,
                'costBasis': {'source': 'declared_offering_pricing'}}
    monkeypatch.setattr(_summary_routing, 'select_summary_route', lambda *a, **kw: selected)
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    calls = []
    usage = {'prompt_tokens': 100, 'completion_tokens': 30, 'finish_reason': 'stop'}
    content = '### Objective\nKeep the complete summary.'

    def chat(*args, **kwargs):
        calls.append(('chat', get_pinned_provider(), kwargs))
        return content, usage

    def stream(*args, **kwargs):
        calls.append(('stream', get_pinned_provider(), kwargs))
        return ProviderStreamResult.from_legacy({'role': 'assistant', 'content': content},
                                                'stop', usage)

    monkeypatch.setattr(dispatch, 'dispatch_chat', chat)
    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    task = {'_userId': 1, 'config': {'model': 'main-model'}}
    original = deepcopy(task)
    previous_pin = get_pinned_provider()
    with provider_pin('owner-route-group'):
        result = summary._generate_query_aware_summary(
            [{'role': 'user', 'content': 'Preserve this evidence.'}], 'continue',
            task=task, on_delta=(lambda chunk: None) if streaming else None)
        assert get_pinned_provider() == 'owner-route-group'
    assert get_pinned_provider() == previous_pin
    assert result == content
    assert len(calls) == 1
    kind, pin, options = calls[0]
    assert kind == ('stream' if streaming else 'chat')
    assert pin == 'owner-route-group'
    assert options['prefer_model'] == 'qualified-summary'
    assert options['strict_model'] is True
    assert options['capability'] == 'text'
    assert options['thinking_enabled'] is False
    assert task['config'] == original['config']


@pytest.mark.parametrize('streaming', [False, True])
def test_summary_route_conflict_never_sends_upstream(monkeypatch, streaming):
    import lib.llm_dispatch as dispatch
    from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
    from lib.tasks_pkg.compaction import _summary_routing
    from lib.tasks_pkg.compaction._layer2 import _summary as summary

    monkeypatch.setattr(_summary_routing, 'select_summary_route', lambda *a, **kw: {
        'modelId': 'qualified-summary', 'providerId': 'foreign-route', 'reason': 'main_default'})
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    calls = []
    monkeypatch.setattr(dispatch, 'dispatch_chat', lambda *a, **kw: calls.append('chat'))
    monkeypatch.setattr(dispatch, 'dispatch_stream', lambda *a, **kw: calls.append('stream'))
    source = [{'role': 'user', 'content': 'Never send this to a foreign provider.'}]
    original = deepcopy(source)
    with provider_pin('authorized-route'):
        assert summary._generate_query_aware_summary(
            source, 'continue', task={'_userId': 1, 'config': {'model': 'main-model'}},
            on_delta=(lambda chunk: None) if streaming else None) is None
        assert get_pinned_provider() == 'authorized-route'
    assert calls == []
    assert source == original


@pytest.mark.parametrize('task,expected', [
    (None, ''),
    ({'_summaryBudgetFailure': 'summary_dispatch_failed'}, 'summary_dispatch_failed'),
    ({'_summaryBudgetFailure': 'old_failure',
      '_summaryAttemptBudget': {'requestKey': 'changed-request'}}, ''),
    ({'_summaryBudgetFailure': 'old_failure',
      '_summaryAttemptBudget': {'requestKey': 'current',
                                '_summaryBudgetFailure': 'summary_incomplete'}},
     'summary_incomplete'),
])
def test_current_attempt_failure_reason_does_not_revive_stale_failure(task, expected):
    from lib.tasks_pkg.compaction._summary_budget import summary_failure_reason

    assert summary_failure_reason(task) == expected


@pytest.mark.parametrize('task_kind', ['utility', 'live', 'transient'])
@pytest.mark.parametrize('mismatch', ['model', 'provider', 'policy', 'cost', 'unknown', 'none'])
def test_optional_summary_revalidates_route_and_quote_before_spending(
        monkeypatch, mismatch, task_kind):
    import lib.llm_dispatch as dispatch_api
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.compaction._layer2 import _summary as mod

    monkeypatch.setattr(mod, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(mod, '_summary_input_char_budget', lambda task: 12000)
    monkeypatch.setattr(mod, '_archived_prior_receipt', lambda task: '')
    task = {'_userId': 1, 'config': {'model': 'main'}}

    if task_kind == 'live':
        task.update(id='quote-test', convId='quote-conversation', aborted=False)
    elif task_kind == 'transient':
        task['_transientRuntime'] = True
    expected = {'modelId': 'main', 'providerId': 'provider',
                'policy': 'main', 'projectedCostUsd': 1.0}
    actual = dict(expected)
    if mismatch == 'model':
        actual['modelId'] = 'other'
    elif mismatch == 'provider':
        actual['providerId'] = 'other'
    elif mismatch == 'policy':
        actual['policy'] = 'qualified-cheapest'
    elif mismatch == 'cost':
        actual['projectedCostUsd'] = 1.01
    elif mismatch == 'unknown':
        actual['projectedCostUsd'] = None
    route_calls, dispatch_calls = [], []

    def route(task, **kwargs):
        route_calls.append(kwargs)
        return actual

    def dispatch(*args, **kwargs):
        dispatch_calls.append(kwargs)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Complete summary.'},
            'stop', {'prompt_tokens': 50, 'completion_tokens': 10})

    monkeypatch.setattr('lib.tasks_pkg.compaction._summary_routing.select_summary_route', route)
    monkeypatch.setattr(dispatch_api, 'dispatch_stream', dispatch)
    source = [{'role': 'user', 'content': 'original objective'}]
    original = deepcopy(source)
    result = mod._generate_query_aware_summary(
        source, 'continue', task=task, expected_route=expected,
        on_delta=lambda chunk: None)
    assert source == original
    assert route_calls[0]['output_tokens'] == 2_200
    assert route_calls[0]['capacity_output_tokens'] == 65_536
    if mismatch == 'none':
        assert result == 'Complete summary.'
        assert len(dispatch_calls) == 1
    else:
        assert result is None
        assert dispatch_calls == []
        if task_kind != 'utility':
            from lib.tasks_pkg.compaction._summary_budget import summary_failure_reason
            assert summary_failure_reason(task) == 'summary_route_revalidation_failed'
            assert mod._generate_query_aware_summary(
                source, 'continue', task=task, expected_route=expected,
                on_delta=lambda chunk: None) is None
            assert dispatch_calls == []
            assert source == original
            assert summary_failure_reason(task) == 'summary_route_revalidation_failed'
