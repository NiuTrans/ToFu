"""Bound auxiliary evaluation work independently of the main task timeout."""
import copy

import pytest

pytestmark = pytest.mark.unit


def test_slow_complete_summary_finishes_without_deadline(monkeypatch):
    from lib.tasks_pkg.compaction import _summary_budget as policy
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    import lib.llm_dispatch as dispatch
    from lib.llm.stream_result import ProviderStreamResult

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
        clock[0] += 600
        assert not kwargs['abort_check']()
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nPreserve original request.'},
            'stop', {'prompt_tokens': 100, 'completion_tokens': 30})

    monkeypatch.setattr(dispatch, 'dispatch_stream', saturated)
    for _ in range(3):
        assert summary._generate_query_aware_summary(messages, 'q', task=task)
    assert len(calls) == 3
    assert clock[0] == 1800
    assert '_summaryBudgetFailure' not in task
    assert messages == original


def test_elapsed_time_is_telemetry_not_a_deadline(monkeypatch):
    from lib.tasks_pkg.compaction import _summary_budget as policy
    clock = [0.0]
    monkeypatch.setattr(policy.time, 'monotonic', lambda: clock[0])
    task = {'config': {'compaction': {'summaryBudgetSeconds': 99999}}}
    first = policy.SummaryBudget(task)
    assert not first.is_set()
    clock[0] = 30
    first.finish(True)
    second = policy.SummaryBudget(task)
    assert task['_summaryBudgetSpentSeconds'] == 30
    clock[0] = 12000
    assert not second.is_set()


@pytest.mark.parametrize('raw', [0, 2, None, True, 'bad', float('nan'), float('inf')])
def test_legacy_budget_values_are_ignored(raw):
    from lib.tasks_pkg.compaction._summary_budget import SummaryBudget
    budget = SummaryBudget({'config': {'compaction': {'summaryBudgetSeconds': raw}}})
    assert not budget.is_set()
    assert not hasattr(budget, 'deadline')


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
    assert task['_summaryAttemptBudget']['_summaryBudgetFailure'] == 'summary_dispatch_failed'


def test_failed_summary_defers_when_prompt_physically_fits(monkeypatch):
    """TB2.1 v10 contract: a failed summary is fatal only when the prompt
    genuinely cannot fit the window; with headroom the round proceeds
    uncompacted and compaction retries next round (the working set is
    advisory to the ROI gate, never an admission clamp)."""
    from lib.tasks_pkg.compaction import _prompt_admission as admission
    task = {'_transientRuntime': True, '_summaryBudgetFailure': 'summary_budget_exhausted',
            'config': {'model': 'eval', 'maxTokens': 1024,
                       'compaction': {'workingSetTokens': 128000}}}
    messages = [{'role': 'user', 'content': 'original objective'}]
    original = copy.deepcopy(messages)
    monkeypatch.setattr(admission, '_measure', lambda *a, **k: {
        'totalTokens': 200000, 'messageTokens': 200000, 'toolSchemaTokens': 0,
        'method': 'test', 'messageCount': 1})
    monkeypatch.setattr(admission, 'force_compact_if_needed', lambda *a, **k: False)
    result = admission.enforce_dispatch_prompt_limit(
        messages, [], task, round_num=0, model='eval')
    assert result['totalTokens'] == 200000
    assert messages == original


def test_failed_summary_is_fatal_only_when_prompt_plus_output_exceeds_window(
        monkeypatch):
    from lib.tasks_pkg.compaction import _prompt_admission as admission
    from lib.llm_errors import ContextCompactionError
    task = {'_transientRuntime': True, '_summaryBudgetFailure': 'summary_budget_exhausted',
            'config': {'model': 'eval', 'maxTokens': 900_000,
                       'compaction': {'workingSetTokens': 128000}}}
    messages = [{'role': 'user', 'content': 'original objective'}]
    monkeypatch.setattr(admission, '_measure', lambda *a, **k: {
        'totalTokens': 250000, 'messageTokens': 250000, 'toolSchemaTokens': 0,
        'method': 'test', 'messageCount': 1})
    monkeypatch.setattr(admission, 'force_compact_if_needed', lambda *a, **k: False)
    with pytest.raises(ContextCompactionError, match='summary_budget_exhausted'):
        admission.enforce_dispatch_prompt_limit(messages, [], task, round_num=0, model='eval')
