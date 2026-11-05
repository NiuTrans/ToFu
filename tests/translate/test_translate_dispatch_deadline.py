"""Translation deadlines cancel dispatch instead of orphaning model work."""

from __future__ import annotations

from types import SimpleNamespace

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
    from lib.llm.llm_errors import RateLimitError
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
    from lib.translate.errors import TranslationDeadlineExhausted

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

    with pytest.raises(TranslationDeadlineExhausted) as raised:
        _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.', source='English', target='Chinese',
            overall_deadline=1.0, use_cache=False)

    assert len(calls) == 1
    # The deadline cut an attempt in flight: nothing came back empty, and the
    # cause is named instead of ``last_err=None``.
    assert raised.value.dispatched_attempts == 1
    assert raised.value.last_err
    assert 'Empty translation result' not in str(raised.value)


def test_predispatch_deadline_is_reported_as_not_dispatched(monkeypatch):
    """A budget spent before the first attempt is not an empty result.

    Production 2026-10-23 logged 130 'Empty translation result for chunk
    after 1 attempts (0 dispatch fails, ..., elapsed=0s)' records for chunks
    whose caller handed over an already-spent budget: zero attempts ran."""
    from lib.translate.engine import _engine
    from lib.translate.errors import TranslationDeadlineExhausted

    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())

    def fail_dispatch(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError('dispatch must not run when deadline is spent')

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fail_dispatch)

    with pytest.raises(TranslationDeadlineExhausted) as raised:
        _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.', source='English', target='Chinese',
            overall_deadline=0.0, use_cache=False)

    error = raised.value
    assert error.dispatched_attempts == 0
    assert 'not dispatched' in str(error)
    assert 'Empty translation result' not in str(error)
    assert '1 attempts' not in str(error)


def test_deadline_never_returns_or_caches_a_rejected_candidate(monkeypatch):
    """A deadline ends the retry loop; it never accepts the reply it refused.

    The loop keeps the last candidate in hand while it retries. When the
    deadline expired before that retry, the refused verbatim echo of the
    source was returned and cached as a successful translation.
    """
    from lib.translate.engine import _engine
    from lib.translate.errors import TranslationDeadlineExhausted

    source = 'This sentence needs a Chinese translation.'
    clock = {'now': 0.0}
    stored = []
    monkeypatch.setattr(_engine.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher',
        lambda: SimpleNamespace(record_truncation=lambda *_a, **_k: None))
    monkeypatch.setattr(
        _engine.translate_cache, 'put',
        lambda *args, **_kwargs: stored.append(args))

    def echo_until_the_deadline(*_args, **_kwargs):
        clock['now'] = 11.0
        return source, {'_dispatch': {'model': 'echo-model', 'key': 'k1'}}

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', echo_until_the_deadline)

    with pytest.raises(TranslationDeadlineExhausted) as raised:
        _engine._translate_one_chunk(
            source, 'Translate faithfully.', source='English',
            target='Chinese', overall_deadline=10.0, use_cache=False)

    assert raised.value.dispatched_attempts == 1
    assert raised.value.content_fails == 1
    assert 'verbatim' in str(raised.value)
    assert stored == []


def test_owner_cancel_during_failed_dispatch_is_not_a_deadline(monkeypatch):
    """``exceeded()`` folds cancellation into the deadline predicate; a
    cancelled owner must still see an abort, not a deadline expiry."""
    from lib.llm import AbortedError
    from lib.llm.llm_errors import RateLimitError
    from lib.translate.engine import _engine

    cancelled = {'value': False}
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())

    def cancelled_while_rate_limited(*_args, **_kwargs):
        cancelled['value'] = True
        raise RateLimitError('all keys busy')

    monkeypatch.setattr(
        'lib.llm_dispatch.smart_chat', cancelled_while_rate_limited)

    with pytest.raises(AbortedError):
        _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.', source='English', target='Chinese',
            overall_deadline=600, use_cache=False,
            abort_check=lambda: cancelled['value'])


def test_whole_turn_deadline_expiry_settles_as_retryable_timeout(monkeypatch):
    from lib.translate.errors import TranslationDeadlineExhausted
    from lib.translate.runtime import _worker

    observed = {}

    def finish(task_id, **kwargs):
        observed['task_id'] = task_id
        observed.update(kwargs)

    monkeypatch.setattr(_worker._translate_runtime, 'finish', finish)
    _worker._settle_error(
        {'model': None},
        'translate-task',
        TranslationDeadlineExhausted(
            deadline_seconds=90.0, dispatched_attempts=2, dispatch_fails=1),
        '',
        '',
        '',
        'translatedContent',
    )

    assert observed['error']['kind'] == 'timeout'
    assert observed['error']['retryable'] is True


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
    from lib.llm.llm_errors import RateLimitError
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


def test_translation_dispatch_drops_conversation_affinity(monkeypatch):
    """Stateless translation never waits on / rebinds a conv's sticky key."""
    from lib.llm_dispatch import conv_affinity
    from lib.translate.engine import _engine

    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())

    def fake_smart_chat(*_args, **_kwargs):
        assert conv_affinity.get_conv_affinity() is None
        return '这是忠实翻译。', {
            '_dispatch': {'model': 'translator', 'key': 'k1'},
        }

    monkeypatch.setattr('lib.llm_dispatch.smart_chat', fake_smart_chat)
    conv_affinity.set_conv_affinity('convsticky123')
    try:
        translated, _usage = _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.',
            source='English',
            target='Chinese',
            overall_deadline=10.0,
            use_cache=False,
        )
        # The worker thread's binding is restored for the rest of the turn.
        assert conv_affinity.get_conv_affinity() == 'convsticky123'
    finally:
        conv_affinity.clear_conv_affinity()

    assert translated == '这是忠实翻译。'


def test_translation_stream_dispatch_drops_conversation_affinity(monkeypatch):
    from lib.llm_dispatch import conv_affinity
    from lib.translate.engine import _engine

    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())

    def fake_dispatch_stream(*_args, **_kwargs):
        assert conv_affinity.get_conv_affinity() is None
        return (
            {'role': 'assistant', 'content': '这是忠实翻译。'},
            'stop',
            {'_dispatch': {'model': 'translator', 'key': 'k1'}},
        )

    monkeypatch.setattr(
        'lib.llm_dispatch.dispatch_stream', fake_dispatch_stream)
    conv_affinity.set_conv_affinity('convsticky123')
    try:
        translated, _usage = _engine._translate_one_chunk(
            'This sentence needs a Chinese translation.',
            'Translate faithfully.',
            source='English',
            target='Chinese',
            overall_deadline=10.0,
            use_cache=False,
            stream=True,
            progress_cb=lambda _preview: None,
        )
        assert conv_affinity.get_conv_affinity() == 'convsticky123'
    finally:
        conv_affinity.clear_conv_affinity()

    assert translated == '这是忠实翻译。'
