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


def test_translation_deadline_reaches_nonstream_dispatch(monkeypatch):
    from lib.llm import AbortedError
    from lib.translate.engine import _engine

    clock = {'now': 0.0}
    calls = []

    monkeypatch.setattr(_engine.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())

    def fake_smart_chat(*_args, **kwargs):
        calls.append(kwargs)
        assert callable(kwargs.get('abort_check'))
        assert 0 < kwargs['timeout'] <= 1.0
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
