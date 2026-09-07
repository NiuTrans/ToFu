"""Optional reconstructible LLM enrichments honor recorded billing stops.

These jobs all have an existing empty/deterministic fallback and must not
challenge a known 402 merely because Settings retains a manual-ON override.
Attended Agent and explicit scheduler prompt execution are intentionally out
of scope.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.unit


def _assert_context_restored(key_stats) -> None:
    assert key_stats.is_strict_billing_stop_admission() is False


def _assert_optional_attempt_budget(kwargs: dict) -> None:
    from lib.production.llm_policy import optional_llm_max_429_attempts

    assert kwargs['max_429_attempts'] == optional_llm_max_429_attempts()


def _assert_immediate_optional_policy(kwargs: dict) -> None:
    _assert_optional_attempt_budget(kwargs)
    assert kwargs['defer_on_shared_contention'] is True


def test_title_generation_dispatch_uses_strict_billing_admission(monkeypatch):
    from lib import key_stats
    from lib.conversations import title_gen

    def dispatch(_messages, **kwargs):
        assert key_stats.is_strict_billing_stop_admission() is True
        _assert_immediate_optional_policy(kwargs)
        return 'Parser repair', {
            'finish_reason': 'stop',
            '_dispatch': {'model': 'cheap-title-model'},
        }

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', dispatch)
    result = title_gen.generate_conversation_title([
        {'role': 'user', 'content': 'Fix the parser.'},
        {'role': 'assistant', 'content': 'Implemented and tested.'},
    ], lang='en')

    assert result == 'Parser repair'
    _assert_context_restored(key_stats)


def test_daily_report_dispatch_uses_strict_billing_admission(monkeypatch):
    from lib import key_stats
    from lib.daily_report import llm

    def dispatch(_messages, **kwargs):
        assert key_stats.is_strict_billing_stop_admission() is True
        _assert_optional_attempt_budget(kwargs)
        return json.dumps({
            'streams': [{'title': 'Parser repair'}],
            'tomorrow': [],
            'yesterday_done': [],
        }), {}

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', dispatch)
    streams, tomorrow, yesterday_done, error = llm._run_llm_analysis(
        'Summarize today.', 1)

    assert streams == [{'title': 'Parser repair'}]
    assert tomorrow == []
    assert yesterday_done == []
    assert error is None
    _assert_context_restored(key_stats)


def test_optimizer_dispatch_uses_strict_billing_admission(monkeypatch):
    from lib import key_stats
    from lib.optimizer import analyzer, proposer

    def dispatch(*_args, **kwargs):
        assert key_stats.is_strict_billing_stop_admission() is True
        _assert_optional_attempt_budget(kwargs)
        return json.dumps({'proposals': [{
            'title': 'Review parser retries',
            'rationale': 'Repeated parser failures were observed.',
            'action_type': 'other',
            'action_args': {},
            'severity': 'low',
            'confidence': 0.8,
            'ttl_days': 7,
        }]}), {}

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', dispatch)
    evidence = analyzer.EvidenceBundle(
        window_hours=24,
        generated_at='2026-08-28T00:00:00',
    )

    proposals = proposer.propose(evidence)
    assert [proposal['title'] for proposal in proposals] == [
        'Review parser retries']
    _assert_context_restored(key_stats)


def test_optional_attempt_budget_is_profiled_and_hard_capped():
    from lib.production.llm_policy import optional_llm_max_429_attempts

    assert optional_llm_max_429_attempts({
        'TOFU_DEPLOYMENT_MODE': 'personal',
    }) == 2
    assert optional_llm_max_429_attempts({
        'TOFU_DEPLOYMENT_MODE': 'distributed',
    }) == 8
    assert optional_llm_max_429_attempts({
        'TOFU_DEPLOYMENT_MODE': 'distributed',
        'TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS': '999',
    }) == 16


@pytest.mark.parametrize(
    'error_kind', ['no_slot', 'rate_limit_budget', 'shared_contention'])
def test_title_terminal_dispatch_failure_does_not_double_attempt_budget(
        monkeypatch, error_kind):
    from lib.conversations import title_gen
    from lib.llm_dispatch import (
        DispatchNoAdmissibleSlot,
        DispatchRateLimitBudgetExceeded,
        DispatchSharedContentionDeferred,
    )
    from lib.llm_errors import RateLimitError

    calls = 0

    def dispatch(_messages, **kwargs):
        nonlocal calls
        calls += 1
        _assert_immediate_optional_policy(kwargs)
        if error_kind == 'no_slot':
            raise DispatchNoAdmissibleSlot('strict admission rejected all slots')
        if error_kind == 'shared_contention':
            raise DispatchSharedContentionDeferred(retry_after_s=3.0)
        raise DispatchRateLimitBudgetExceeded(
            RateLimitError('slow down', status_code=429),
            attempts=kwargs['max_429_attempts'],
            limit=kwargs['max_429_attempts'],
        )

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', dispatch)
    result = title_gen.generate_conversation_title([
        {'role': 'user', 'content': 'Fix the parser without losing data.'},
        {'role': 'assistant', 'content': 'Implemented and verified.'},
    ], lang='en')

    assert result
    assert calls == 1


def test_title_quality_retry_reserves_only_one_429_attempt(monkeypatch):
    from lib.conversations import title_gen

    budgets = []

    def dispatch(_messages, **kwargs):
        budgets.append(kwargs['max_429_attempts'])
        if len(budgets) == 1:
            return '', {
                'finish_reason': 'length',
                '_dispatch': {'model': 'burned-title-model'},
            }
        return 'Parser recovery', {
            'finish_reason': 'stop',
            '_dispatch': {'model': 'backup-title-model'},
        }

    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', dispatch)
    result = title_gen.generate_conversation_title([
        {'role': 'user', 'content': 'Fix the parser without losing data.'},
        {'role': 'assistant', 'content': 'Implemented and verified.'},
    ], lang='en')

    from lib.production.llm_policy import optional_llm_max_429_attempts
    assert result == 'Parser recovery'
    assert budgets == [optional_llm_max_429_attempts(), 1]


def test_title_budget_policy_failure_degrades_without_dispatch(monkeypatch):
    from lib.conversations import title_gen

    def unavailable_policy():
        raise KeyError('old resource manifest')

    def dispatch_must_not_run(*_args, **_kwargs):
        raise AssertionError('dispatch ran without a resolved request budget')

    monkeypatch.setattr(
        'lib.production.llm_policy.optional_llm_max_429_attempts',
        unavailable_policy,
    )
    monkeypatch.setattr('lib.llm_dispatch.dispatch_chat', dispatch_must_not_run)

    result = title_gen.generate_conversation_title([
        {'role': 'user', 'content': 'Fix the parser without losing data.'},
        {'role': 'assistant', 'content': 'Implemented and verified.'},
    ], lang='en')

    assert result == 'Fix the parser without losing data.'
