"""Settled-turn segment enrichment: budget and rate-limit tolerance.

Enrichment is optional work, but "optional" must not mean "silently skipped for
every real multi-batch turn". The wall budget scales with the text it covers,
and one upstream 429 no longer ends the whole pass.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _segments(count, chars_per_segment):
    return [
        {
            'type': 'thinking',
            'blockId': f'thinking:llm-{index}',
            'llmRound': index,
            'text': 'x' * chars_per_segment,
        }
        for index in range(count)
    ]


def test_enrichment_budget_has_a_floor_and_a_ceiling():
    from lib.translate.runtime import _segments as segments_mod

    assert (
        segments_mod._segment_enrichment_deadline_seconds(0)
        == segments_mod._SEGMENT_ENRICHMENT_DEADLINE_FLOOR_SECONDS
    )
    assert (
        segments_mod._segment_enrichment_deadline_seconds(10_000_000)
        == segments_mod._SEGMENT_ENRICHMENT_DEADLINE_CEILING_SECONDS
    )


def test_enrichment_budget_grows_with_the_segments_it_must_cover():
    from lib.translate.runtime import _segments as segments_mod

    small = segments_mod._segment_enrichment_deadline_seconds(500)
    large = segments_mod._segment_enrichment_deadline_seconds(7000)

    assert large > small
    assert large > segments_mod._SEGMENT_ENRICHMENT_DEADLINE_FLOOR_SECONDS


def test_multi_batch_turn_budget_does_not_expire_after_the_first_batch(
        monkeypatch):
    """A 36-segment answer needs several calls; the budget must cover them."""
    from lib.translate.runtime import _segments as segments_mod

    budgets = []

    def fake_translate(text, _prompt, **kwargs):
        budgets.append(kwargs["overall_deadline"])
        return f'译:{text}', {}

    monkeypatch.setattr(segments_mod, '_translate_freetext', fake_translate)
    monkeypatch.setattr(
        segments_mod.translate_cache, 'get', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        segments_mod, 'is_predominantly_chinese', lambda _text: False)
    monkeypatch.setattr(
        segments_mod,
        '_read_turn_segments',
        lambda *_args, **_kwargs: _segments(36, 200),
    )

    result = segments_mod._build_segment_translation_map(
        'conversation', 'turn', 'prompt', 'English', 'Chinese', user_id=7,
    )

    assert result and len(result) == 36
    assert budgets
    assert max(budgets) > segments_mod._SEGMENT_ENRICHMENT_DEADLINE_FLOOR_SECONDS


def test_enrichment_carries_the_configured_rate_limit_budget(monkeypatch):
    """One upstream 429 must not end the pass.

    The budget is enforced inside the engine's dispatch (which retries and
    rotates slots); the enrichment call simply has to stop pinning it to 1.
    """
    from lib.translate.runtime import _segments as segments_mod

    monkeypatch.setattr(
        segments_mod, 'translation_max_429_attempts', lambda: 4)
    seen = []

    def fake_translate(text, _prompt, **kwargs):
        seen.append(kwargs["max_429_attempts"])
        return f'译:{text}', {}

    monkeypatch.setattr(segments_mod, '_translate_freetext', fake_translate)
    monkeypatch.setattr(
        segments_mod.translate_cache, 'get', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        segments_mod, 'is_predominantly_chinese', lambda _text: False)
    monkeypatch.setattr(
        segments_mod,
        '_read_turn_segments',
        lambda *_args, **_kwargs: _segments(2, 40),
    )

    result = segments_mod._build_segment_translation_map(
        'conversation', 'turn', 'prompt', 'English', 'Chinese', user_id=7,
    )

    assert seen and all(value == 4 for value in seen)
    assert result and len(result) == 2


@pytest.fixture
def segment_log_rows():
    """Capture ``_segments`` log rows regardless of logger propagation."""
    from lib.translate.runtime import _segments as segments_mod

    rows = []

    class _Grab(logging.Handler):
        def emit(self, record):
            rows.append((record.levelno, record.getMessage()))

    handler = _Grab()
    previous_level = segments_mod.logger.level
    segments_mod.logger.addHandler(handler)
    segments_mod.logger.setLevel(logging.DEBUG)
    try:
        yield rows
    finally:
        segments_mod.logger.removeHandler(handler)
        segments_mod.logger.setLevel(previous_level)


def _isolate_segment_pass(monkeypatch, segments_mod, translate):
    """Bypass cache/language gates; two cache misses per batch."""
    monkeypatch.setattr(segments_mod, '_translate_freetext', translate)
    monkeypatch.setattr(
        segments_mod.translate_cache, 'get', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        segments_mod, 'is_predominantly_chinese', lambda _text: False)
    monkeypatch.setattr(segments_mod, '_SEGMENT_BATCH_MAX_ITEMS', 2)


def _skip_summaries(rows):
    return [message for level, message in rows
            if level == logging.INFO and 'skipped, not failed' in message]


def test_spent_pass_budget_skips_later_batches_without_dispatch(
        monkeypatch, segment_log_rows):
    """Production 2026-10-23: 12 of 26 settled-turn passes ran past their
    180s ceiling, and every remaining batch was still handed to the engine
    with ``overall_deadline=0.0`` — 50 'batch dispatch failed' warnings for
    calls that were never sent. A call now starts only while the pass can
    still fund the engine's response-header bound; the rest is an explicit
    skip, not a failure."""
    from lib.translate.engine import translation_first_byte_timeout_seconds
    from lib.translate.runtime import _segments as segments_mod

    header_bound = translation_first_byte_timeout_seconds(80)
    clock = {'now': 1000.0}
    monkeypatch.setattr(
        segments_mod, 'time',
        SimpleNamespace(monotonic=lambda: clock['now']))
    deadlines = []
    # The first call leaves exactly the header bound (still a real share);
    # the second leaves less than it, so the third batch is never sent.
    spend = iter((60.0 - header_bound, header_bound - 1.0))

    def timed_translate(text, _prompt, **kwargs):
        deadlines.append(kwargs['overall_deadline'])
        clock['now'] += next(spend)
        return text.replace('x', '译'), {}

    _isolate_segment_pass(monkeypatch, segments_mod, timed_translate)
    result = segments_mod._translate_segments_to_map(
        _segments(6, 40), 'prompt', 'English', 'Chinese',
        overall_deadline=60.0,
    )

    assert deadlines == [60.0, header_bound]
    assert sorted(result) == [f'thinking:llm-{index}' for index in range(4)]
    rows = segment_log_rows
    assert not [row for row in rows if row[0] >= logging.WARNING], rows
    summaries = _skip_summaries(rows)
    assert len(summaries) == 1 and '2 segments' in summaries[0], rows


def test_first_call_runs_even_when_the_pass_budget_is_below_the_header_bound(
        monkeypatch, segment_log_rows):
    """The 15s floor sits under the 20s header bound; small passes still run."""
    from lib.translate.runtime import _segments as segments_mod

    clock = {'now': 1000.0}
    monkeypatch.setattr(
        segments_mod, 'time',
        SimpleNamespace(monotonic=lambda: clock['now']))
    deadlines = []

    def quick_translate(text, _prompt, **kwargs):
        deadlines.append(kwargs['overall_deadline'])
        clock['now'] += 1.0
        return text.replace('x', '译'), {}

    _isolate_segment_pass(monkeypatch, segments_mod, quick_translate)
    floor = segments_mod._SEGMENT_ENRICHMENT_DEADLINE_FLOOR_SECONDS
    result = segments_mod._translate_segments_to_map(
        _segments(4, 40), 'prompt', 'English', 'Chinese',
        overall_deadline=floor,
    )

    assert deadlines == [floor]
    assert sorted(result) == ['thinking:llm-0', 'thinking:llm-1']
    summaries = _skip_summaries(segment_log_rows)
    assert len(summaries) == 1 and '2 segments' in summaries[0]


def test_engine_deadline_exhaustion_is_counted_as_a_skip(
        monkeypatch, segment_log_rows):
    """A typed deadline stop is a budget outcome, never a failed dispatch."""
    from lib.translate.errors import TranslationDeadlineExhausted
    from lib.translate.runtime import _segments as segments_mod

    calls = []

    def expiring(_text, _prompt, **kwargs):
        calls.append(kwargs['overall_deadline'])
        raise TranslationDeadlineExhausted(
            deadline_seconds=kwargs['overall_deadline'],
            dispatched_attempts=1,
        )

    _isolate_segment_pass(monkeypatch, segments_mod, expiring)
    result = segments_mod._translate_segments_to_map(
        _segments(3, 40), 'prompt', 'English', 'Chinese',
        overall_deadline=60.0,
    )

    # One batch of two plus one singleton: neither fans out or retries.
    assert len(calls) == 2
    assert result == {}
    rows = segment_log_rows
    assert not [row for row in rows if row[0] >= logging.WARNING], rows
    summaries = _skip_summaries(rows)
    assert len(summaries) == 1 and '3 segments' in summaries[0], rows
