"""Bound auxiliary evaluation work independently of the main task timeout."""
import copy

import pytest

pytestmark = pytest.mark.unit


def test_saturated_summary_stops_and_never_restarts(monkeypatch):
    from lib.tasks_pkg.compaction import _summary_budget as policy
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    import lib.llm_dispatch as dispatch
    from lib.llm_errors import AbortedError

    clock = [0.0]
    monkeypatch.setattr(policy.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    task = {'_transientRuntime': True, '_userId': 1,
            'config': {'model': 'eval-model', 'compaction': {'summaryBudgetSeconds': 2}}}
    messages = [{'role': 'user', 'content': 'preserve my original request'}]
    original = copy.deepcopy(messages)
    calls = []

    def saturated(body, **kwargs):
        calls.append(kwargs)
        assert kwargs['strict_model'] is True
        assert kwargs['prefer_model'] == 'eval-model'
        assert kwargs['max_429_attempts'] == 2
        # Same callback used by dispatch's slot wait / 429 backoff / watchdog.
        while not kwargs['abort_check']():
            clock[0] += 0.25
        raise AbortedError('deadline')

    monkeypatch.setattr(dispatch, 'dispatch_stream', saturated)
    for _ in range(3):
        assert summary._generate_query_aware_summary(messages, 'q', task=task) is None
    assert len(calls) == 1
    assert clock[0] == 2
    assert task['_summaryBudgetFailure'] == 'summary_budget_exhausted'
    assert messages == original


def test_budget_is_cumulative_and_clamped(monkeypatch):
    from lib.tasks_pkg.compaction import _summary_budget as policy
    clock = [0.0]
    monkeypatch.setattr(policy.time, 'monotonic', lambda: clock[0])
    task = {'config': {'compaction': {'summaryBudgetSeconds': 99999}}}
    first = policy.SummaryBudget(task)
    assert first.deadline == 120
    clock[0] = 30
    first.finish(True)
    second = policy.SummaryBudget(task)
    assert second.deadline == 120
    clock[0] = 120
    assert second.is_set()


@pytest.mark.parametrize('raw', [None, True, 'bad', float('nan'), float('inf')])
def test_invalid_budget_cannot_disable_deadline(raw):
    from lib.tasks_pkg.compaction._summary_budget import SummaryBudget
    budget = SummaryBudget({'config': {'compaction': {'summaryBudgetSeconds': raw}}})
    assert budget.deadline - budget.started == 60


def test_dispatch_fault_has_no_alternate_model_retry(monkeypatch):
    import lib.llm_dispatch as dispatch
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('injected provider failure')
    monkeypatch.setattr(dispatch, 'dispatch_stream', fail)
    task = {'_userId': 1, 'config': {'disableModelFallback': True, 'model': 'eval'}}
    assert summary._generate_query_aware_summary([], 'q', task=task) is None
    assert len(calls) == 1
    assert task['_summaryBudgetFailure'] == 'summary_dispatch_failed'


@pytest.mark.parametrize('tokens', [100, 200000])
def test_failed_summary_only_continues_when_prompt_fits(monkeypatch, tokens):
    from lib.tasks_pkg.compaction import _prompt_admission as admission
    from lib.llm_errors import ContextCompactionError
    task = {'_transientRuntime': True, '_summaryBudgetFailure': 'summary_budget_exhausted',
            'config': {'model': 'eval', 'compaction': {'workingSetTokens': 128000}}}
    messages = [{'role': 'user', 'content': 'original objective'}]
    original = copy.deepcopy(messages)
    monkeypatch.setattr(admission, '_measure', lambda *a, **k: {
        'totalTokens': tokens, 'messageTokens': tokens, 'toolSchemaTokens': 0,
        'method': 'test', 'messageCount': 1})
    monkeypatch.setattr(admission, 'force_compact_if_needed', lambda *a, **k: False)
    if tokens > 120000:
        with pytest.raises(ContextCompactionError, match='summary_budget_exhausted'):
            admission.enforce_dispatch_prompt_limit(messages, [], task, round_num=0, model='eval')
    else:
        assert admission.enforce_dispatch_prompt_limit(
            messages, [], task, round_num=0, model='eval')['totalTokens'] == tokens
    assert messages == original
