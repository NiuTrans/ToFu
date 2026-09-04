"""Completion-window clamp reuses fresh admission evidence when available."""

from __future__ import annotations

import pytest

from lib.llm.body._clamp import _clamp_completion_to_context_window

pytestmark = pytest.mark.unit


def test_precomputed_input_avoids_rescanning_messages(monkeypatch):
    import lib.tasks_pkg.compaction._tokens as token_policy
    import lib.token_counter.heuristic as heuristic

    monkeypatch.setattr(
        token_policy, 'resolve_model_context_limit', lambda *a, **k: 100_000)
    monkeypatch.setattr(
        heuristic,
        'cheap_estimate',
        lambda *a, **k: pytest.fail('fresh admission must avoid a second scan'),
    )

    result = _clamp_completion_to_context_window(
        'gpt-5.6-sol',
        [{'role': 'user', 'content': 'large prompt'}],
        40_000,
        precomputed_input_tokens=60_000,
    )

    assert result == 33_488


@pytest.mark.parametrize('invalid', [None, 0, -1, True, 1.5, '60000'])
def test_invalid_precomputed_input_retains_local_estimation(
    monkeypatch,
    invalid,
):
    import lib.tasks_pkg.compaction._tokens as token_policy
    import lib.token_counter.heuristic as heuristic

    calls = []
    monkeypatch.setattr(
        token_policy, 'resolve_model_context_limit', lambda *a, **k: 100_000)
    monkeypatch.setattr(
        heuristic,
        'cheap_estimate',
        lambda messages: calls.append(messages) or 60_000,
    )

    result = _clamp_completion_to_context_window(
        'gpt-5.6-sol',
        [{'role': 'user', 'content': 'large prompt'}],
        40_000,
        precomputed_input_tokens=invalid,
    )

    assert result == 33_488
    assert len(calls) == 1
