"""Optional translation previews get a deadline sized to their input.

From 2026-10-20 to 10-25 the fixed 30s limit cut 724 previews. The miss rate
rose with the source size (2% under 500 characters, 76% at 8-16k, every
preview above 16k); about half of the cut previews were streams still
producing at the cut, and 213 had lost 20s of the 30s to one silent slot's
response-header wait, leaving the replacement attempt about 10s.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _english(chars):
    text = ''.join(
        f'Reasoning step {index} checks why the failing case regressed. '
        for index in range(chars // 40 + 1))
    return text[:chars]


def _chinese(chars):
    text = ''.join(f'第{index}步检查失败用例为何回归。' for index in range(chars // 8 + 1))
    return text[:chars - 1] + '。'


@pytest.fixture
def provider(monkeypatch):
    """A fake clock plus a provider double behind the real engine.

    The double streams for ``needed_seconds`` and, like ``stream_chat``,
    returns a client-aborted result as soon as the caller's abort check fires.
    ``silent_slots`` slots first hold the call for their response-header bound
    without answering; dispatch then rotates inside the same call.
    """
    from lib.translate.engine import _engine

    clock = SimpleNamespace(now=1000.0)
    state = SimpleNamespace(calls=[], needed_seconds=1.0, silent_slots=0,
                            translation='')
    monkeypatch.setattr(_engine.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher',
        lambda: SimpleNamespace(record_truncation=lambda *_a, **_k: None))
    monkeypatch.setattr(_engine.translate_cache, 'get', lambda *_a, **_k: None)
    monkeypatch.setattr(_engine.translate_cache, 'put', lambda *_a, **_k: None)
    monkeypatch.setattr(_engine.translate_refusal, 'get', lambda *_a, **_k: None)

    def dispatch_stream(_messages, *, abort_check, on_content=None,
                        first_byte_timeout=None, attempt_timeout=None,
                        **_kwargs):
        state.calls.append({'first_byte_timeout': first_byte_timeout,
                            'attempt_timeout': attempt_timeout,
                            'started': clock.now})
        for _slot in range(state.silent_slots):
            clock.now += first_byte_timeout
        started = clock.now
        while clock.now - started < state.needed_seconds:
            clock.now += 1.0
            if abort_check():
                return {'role': 'assistant', 'content': ''}, 'aborted', {}
        if on_content is not None:
            on_content(state.translation)
        return ({'role': 'assistant', 'content': state.translation}, 'stop',
                {'_dispatch': {'model': 'slow-healthy-model', 'key': 'k1'}})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', dispatch_stream)
    return state


def _refuse_probe(*_args, **_kwargs):
    raise AssertionError('a deadline consulted the hardware probe')


def test_preview_deadline_is_sized_by_the_source_between_a_floor_and_a_cap(
        monkeypatch):
    import runtime_guards
    from lib.translate import incremental

    monkeypatch.setattr(runtime_guards, 'probe_system_resources', _refuse_probe)
    floor = runtime_guards.resolve_resource_budget(
        'TOFU_INCREMENTAL_TRANSLATE_PREVIEW_DEADLINE_SECONDS', {},
        minimum=5, maximum=300)
    monkeypatch.setattr(incremental, '_PREVIEW_DEADLINE_SECONDS', floor)
    cap = incremental._PREVIEW_DEADLINE_CAP_SECONDS
    sizes = (0, 300, 3_000, 6_000, 12_000, 19_114, 40_000)
    deadlines = [incremental._preview_deadline_seconds(_english(size))
                 for size in sizes]

    assert deadlines == sorted(deadlines)
    assert deadlines[0] == floor
    assert deadlines[sizes.index(12_000)] > floor
    assert deadlines[-1] == cap
    assert all(floor <= deadline <= cap for deadline in deadlines)

    # An operator floor above the cap is honored, never lowered by it.
    monkeypatch.setattr(incremental, '_PREVIEW_DEADLINE_SECONDS', 300)
    assert incremental._preview_deadline_seconds(_english(40_000)) == 300


def test_large_reasoning_preview_finishes_instead_of_timing_out(
        monkeypatch, provider):
    """The observed shape: a 19,114-character reasoning block whose healthy
    stream showed first content at 25.5s and was still producing at the 30s
    cut; at the provider's measured rate it needs about 52s."""
    from lib.translate import incremental

    task = {
        'id': 'preview-large-reasoning', 'convId': 'conversation',
        '_turnId': 'turn', '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    accumulator = incremental._Accumulator(task)
    monkeypatch.setattr(accumulator, '_push', lambda _payload, **_options: None)
    monkeypatch.setattr(
        'lib.translate.skip_policy.should_skip_automatic_translation',
        lambda *_args, **_kwargs: False)
    source = _english(19_114)
    provider.translation = _chinese(8_600)
    provider.needed_seconds = 52.0
    key = 'thinking:attempt-a:llm-6'
    try:
        accumulator._process_preview_segment(key, source)

        assert accumulator.translations[key] == provider.translation
        assert incremental._preview_circuit(
            accumulator._preview_state, 'reasoning').get(
                incremental._PREVIEW_STATE_FAILURES, 0) == 0
        assert len(provider.calls) == 1
    finally:
        incremental.cancel_incremental(task)


def test_a_silent_slot_cannot_spend_the_retrys_share_of_a_short_budget(
        provider):
    """A slot that never answers may hold at most half of the remaining
    budget for its response headers; the rotated healthy slot keeps the rest
    instead of the 10s a fixed 20s header wait left of a 30s budget."""
    from lib.translate.engine import _engine

    provider.translation = _chinese(400)
    provider.silent_slots = 1
    provider.needed_seconds = 12.0

    translated, _usage = _engine._translate_one_chunk(
        _english(700), 'Translate faithfully.', source='English',
        target='Chinese', overall_deadline=30.0, use_cache=False, stream=True)

    assert translated == provider.translation
    assert provider.calls[0]['first_byte_timeout'] <= 30.0 / 2


def test_a_long_budget_keeps_the_full_response_header_bound(provider):
    from lib.translate.engine import _engine
    from lib.translate.engine import translation_first_byte_timeout_seconds

    provider.translation = _chinese(400)
    _engine._translate_one_chunk(
        _english(700), 'Translate faithfully.', source='English',
        target='Chinese', overall_deadline=600.0, use_cache=False, stream=True)

    assert provider.calls[0]['first_byte_timeout'] == (
        translation_first_byte_timeout_seconds(700))


def test_a_deadline_that_cuts_a_live_stream_is_not_a_dispatch_failure(provider):
    """The stream was healthy; the caller's deadline stopped it. Reporting
    ``1 dispatch fails, last_err=dispatch error`` blamed the provider."""
    from lib.translate.engine import _engine
    from lib.translate.errors import TranslationDeadlineExhausted

    provider.translation = _chinese(400)
    provider.needed_seconds = 40.0

    with pytest.raises(TranslationDeadlineExhausted) as raised:
        _engine._translate_one_chunk(
            _english(700), 'Translate faithfully.', source='English',
            target='Chinese', overall_deadline=30.0, use_cache=False,
            stream=True)

    assert raised.value.dispatched_attempts == 1
    assert raised.value.dispatch_fails == 0
    assert 'deadline' in str(raised.value.last_err)
    assert 'dispatch error' not in str(raised.value.last_err)
