"""One-call translation budget: size-derived wall clock, retryable transport.

The whole-document translation is a single provider call. Its total budget must
therefore exceed the per-attempt transport timeout that same call authorizes,
otherwise the caller kills the only attempt it permitted — and because the
engine discards an aborted stream, the already-produced translation is lost and
the turn is reported as failed.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('chars', [40, 374, 3000, 6000, 7244, 20000])
def test_whole_document_budget_covers_its_own_attempt_timeout(chars):
    from lib.translate.engine import _engine

    attempt_timeout = _engine._translation_chunk_attempt_limits(chars)[1]
    budget = _engine.translation_total_deadline_seconds(chars)

    assert budget >= attempt_timeout
    assert budget >= 90.0


def test_large_document_budget_grows_past_the_flat_floor():
    from lib.translate.engine import _engine

    assert (
        _engine.translation_total_deadline_seconds(7244)
        > _engine.translation_total_deadline_seconds(374)
    )


def test_read_timeout_is_retried_instead_of_ending_the_translation(monkeypatch):
    """A read timeout is retryable transport, not a bad payload.

    Classifying it as deterministic ended the translation (and every
    enrichment segment) after one slow slot even though the same payload would
    succeed elsewhere.
    """
    from lib.llm.llm_errors import ResponseReadTimeoutError
    from lib.translate.engine import _engine

    attempts = []

    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())
    monkeypatch.setattr(_engine.time, 'sleep', lambda _seconds: None)

    def flaky_smart_chat(*_args, **_kwargs):
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise ResponseReadTimeoutError(
                base_url='https://provider.test/v1', timeout_seconds=14.0)
        return '这是完整译文。', {'_dispatch': {'model': 'translator'}}

    monkeypatch.setattr(
        'lib.llm_dispatch.smart_chat', flaky_smart_chat)

    translated, usage = _engine._translate_one_chunk(
        'A sentence that needs a Chinese translation.',
        'Translate faithfully.',
        source='English',
        target='Chinese',
        overall_deadline=600,
        use_cache=False,
    )

    assert translated == '这是完整译文。'
    assert len(attempts) == 2
    assert usage['_translate_trace']['verdict'] == 'ok'


def test_transport_retry_stays_bounded_and_reports_the_cause(monkeypatch):
    from lib.llm.llm_errors import ResponseReadTimeoutError
    from lib.translate.engine import _engine

    attempts = []

    monkeypatch.setattr('lib.mt_provider.is_mt_configured', lambda: False)
    monkeypatch.setattr(
        'lib.llm_dispatch.factory.get_dispatcher', lambda: object())
    monkeypatch.setattr(_engine.time, 'sleep', lambda _seconds: None)

    def always_times_out(*_args, **_kwargs):
        attempts.append(len(attempts))
        raise ResponseReadTimeoutError(
            base_url='https://provider.test/v1', timeout_seconds=14.0)

    monkeypatch.setattr(
        'lib.llm_dispatch.smart_chat', always_times_out)

    with pytest.raises(ResponseReadTimeoutError):
        _engine._translate_one_chunk(
            'A sentence that needs a Chinese translation.',
            'Translate faithfully.',
            source='English',
            target='Chinese',
            overall_deadline=600,
            use_cache=False,
        )

    assert len(attempts) == _engine._MAX_CONTENT_RETRIES + 1
