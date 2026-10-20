"""Settled-turn segment enrichment: budget and rate-limit tolerance.

Enrichment is optional work, but "optional" must not mean "silently skipped for
every real multi-batch turn". The wall budget scales with the text it covers,
and one upstream 429 no longer ends the whole pass.
"""

from __future__ import annotations

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
