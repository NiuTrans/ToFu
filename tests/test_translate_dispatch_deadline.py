"""Translation deadlines cancel dispatch instead of orphaning model work."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_smart_chat_does_not_resurrect_aborted_dispatch(monkeypatch):
    from lib.llm import AbortedError
    from lib.llm_dispatch import api

    monkeypatch.setattr(
        api, 'dispatch_chat',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AbortedError('caller deadline')))

    def direct_chat_must_not_run(*_args, **_kwargs):
        raise AssertionError('aborted dispatch fell through to direct chat')

    monkeypatch.setattr('lib.llm.chat', direct_chat_must_not_run)
    with pytest.raises(AbortedError, match='caller deadline'):
        api.smart_chat(
            [{'role': 'user', 'content': 'translate me'}],
            abort_check=lambda: True)


def test_smart_chat_does_not_bypass_rate_limit_attempt_budget(monkeypatch):
    from lib.llm_errors import RateLimitError
    from lib.llm_dispatch import api

    budget_error = api.DispatchRateLimitBudgetExceeded(
        RateLimitError('private provider response', status_code=429),
        attempts=4,
        limit=4,
    )
    monkeypatch.setattr(
        api,
        'dispatch_chat',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(budget_error),
    )

    def direct_chat_must_not_run(*_args, **_kwargs):
        raise AssertionError('budget exhaustion fell through to direct chat')

    monkeypatch.setattr('lib.llm.chat', direct_chat_must_not_run)
    with pytest.raises(api.DispatchRateLimitBudgetExceeded):
        api.smart_chat(
            [{'role': 'user', 'content': 'translate me'}],
            max_429_attempts=4,
        )


def test_strict_billing_stop_context_prevents_direct_chat_fallback(monkeypatch):
    from lib import key_stats
    from lib.llm_dispatch import api

    dispatch_error = RuntimeError('no billing-healthy slot')
    monkeypatch.setattr(
        api,
        'dispatch_chat',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(dispatch_error),
    )

    def direct_chat_must_not_run(*_args, **_kwargs):
        raise AssertionError('strict billing stop fell through to direct chat')

    monkeypatch.setattr('lib.llm.chat', direct_chat_must_not_run)
    with key_stats.strict_billing_stop_admission():
        with pytest.raises(RuntimeError, match='no billing-healthy slot'):
            api.smart_chat([{'role': 'user', 'content': 'translate me'}])


def test_translation_dispatch_enters_strict_billing_stop_context(monkeypatch):
    from lib import key_stats
    from lib.translate.engine import _engine

    calls = []
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())

    def fake_smart_chat(*_args, **kwargs):
        calls.append(kwargs)
        assert key_stats.is_strict_billing_stop_admission() is True
        return '这是忠实翻译。', {
            '_dispatch': {'model': 'translator', 'key': 'k1'},
        }

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fake_smart_chat)
    translated, _usage = _engine._translate_one_chunk(
        'This sentence needs a Chinese translation.',
        'Translate faithfully.', source='English', target='Chinese',
        overall_deadline=10.0, use_cache=False)

    assert translated == '这是忠实翻译。'
    assert len(calls) == 1
    assert key_stats.is_strict_billing_stop_admission() is False


def test_translation_deadline_reaches_nonstream_dispatch(monkeypatch):
    from lib.llm import AbortedError
    from lib.translate.engine import _engine

    clock = {'now': 0.0}
    calls = []

    monkeypatch.setattr(_engine.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())
    monkeypatch.setattr(
        'lib.translate.policy.translation_max_429_attempts', lambda: 5)

    def fake_smart_chat(*_args, **kwargs):
        calls.append(kwargs)
        assert callable(kwargs.get('abort_check'))
        assert 0 < kwargs['timeout'] <= 1.0
        assert kwargs['max_429_attempts'] == 5
        clock['now'] = 2.0
        assert kwargs['abort_check']() is True
        raise AbortedError('translation deadline')

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fake_smart_chat)

    with pytest.raises(ValueError, match='Empty translation result'):
        _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.', source='English', target='Chinese',
            overall_deadline=1.0, use_cache=False)

    assert len(calls) == 1


def test_translation_call_can_tighten_upstream_429_attempt_budget(monkeypatch):
    from lib.translate.engine import _engine

    calls = []
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())

    def fake_smart_chat(*_args, **kwargs):
        calls.append(kwargs)
        return '这是忠实翻译。', {
            '_dispatch': {'model': 'translator', 'key': 'k1'},
        }

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fake_smart_chat)
    translated, _usage = _engine._translate_one_chunk(
        'This sentence needs a Chinese translation.',
        'Translate faithfully.', source='English', target='Chinese',
        overall_deadline=10.0, use_cache=False, max_429_attempts=1)

    assert translated == '这是忠实翻译。'
    assert len(calls) == 1
    assert calls[0]['max_429_attempts'] == 1


def test_translation_attempt_budget_is_terminal_for_outer_retry(monkeypatch):
    from lib.llm_errors import RateLimitError
    from lib.llm_dispatch import api
    from lib.translate.engine import _engine

    calls = {'n': 0}
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())
    monkeypatch.setattr(
        'lib.translate.policy.translation_max_429_attempts', lambda: 3)

    def fake_smart_chat(*_args, **kwargs):
        calls['n'] += 1
        assert kwargs['max_429_attempts'] == 3
        raise api.DispatchRateLimitBudgetExceeded(
            RateLimitError('slow down', status_code=429),
            attempts=3,
            limit=3,
        )

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fake_smart_chat)

    with pytest.raises(api.DispatchRateLimitBudgetExceeded):
        _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.',
            source='English',
            target='Chinese',
            overall_deadline=600,
            use_cache=False,
        )

    assert calls['n'] == 1


def test_no_admissible_slot_is_terminal_without_outer_backoff(monkeypatch):
    from lib.llm_dispatch import api
    from lib.translate.errors import TranslationNoAdmissibleProvider
    from lib.translate.engine import _engine

    calls = {'n': 0}
    sleeps = []
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())
    monkeypatch.setattr(_engine.time, 'sleep', lambda seconds: sleeps.append(seconds))

    def fake_smart_chat(*_args, **_kwargs):
        calls['n'] += 1
        raise api.DispatchNoAdmissibleSlot(
            'no policy-admissible translation slot')

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fake_smart_chat)

    with pytest.raises(TranslationNoAdmissibleProvider):
        _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.',
            source='English',
            target='Chinese',
            overall_deadline=600,
            use_cache=False,
        )

    assert calls['n'] == 1
    assert sleeps == []


def test_translation_owner_abort_propagates_without_outer_retry(monkeypatch):
    from lib.llm import AbortedError
    from lib.translate.engine import _engine

    cancelled = {'value': False}
    calls = {'n': 0}
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())

    def fake_smart_chat(*_args, **kwargs):
        calls['n'] += 1
        cancelled['value'] = True
        assert kwargs['abort_check']() is True
        raise AbortedError('owner stopped translation')

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fake_smart_chat)

    with pytest.raises(AbortedError, match='owner stopped'):
        _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.',
            source='English',
            target='Chinese',
            abort_check=lambda: cancelled['value'],
            use_cache=False,
        )

    assert calls['n'] == 1


@pytest.mark.parametrize('error_type,status_code', [
    ('bad_request', 400),
    ('request_scoped', 404),
])
def test_deterministic_dispatch_rejection_has_no_outer_retry(
        monkeypatch, error_type, status_code):
    from lib.llm import BadRequestError, RequestScopedError
    from lib.translate.engine import _engine

    calls = {'n': 0}
    sleeps = []
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())
    monkeypatch.setattr(
        _engine.time, 'sleep', lambda seconds: sleeps.append(seconds))
    error = (
        BadRequestError('unsupported request body')
        if error_type == 'bad_request'
        else RequestScopedError('model not found', status_code=status_code)
    )

    def reject(*_args, **_kwargs):
        calls['n'] += 1
        raise error

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', reject)

    with pytest.raises(type(error), match=str(error)):
        _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.',
            source='English',
            target='Chinese',
            overall_deadline=600,
            use_cache=False,
        )

    assert calls['n'] == 1
    assert sleeps == []
