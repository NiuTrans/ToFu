"""Incremental narration previews survive a transient provider failure.

A single 429 or read timeout used to disable previews for the remainder of the
turn, so a long answer showed translated narration for its first rounds and
then silently reverted to the source language.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _accumulator(monkeypatch):
    from lib.translate import incremental

    task = {
        'id': 'preview-retry', 'convId': 'conversation', '_turnId': 'turn',
        '_userId': 9, 'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    accumulator = incremental._Accumulator(task)
    monkeypatch.setattr(accumulator, '_push', lambda _payload: None)
    return incremental, accumulator, task


def test_one_preview_failure_does_not_disable_the_rest_of_the_turn(
        monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    outcomes = [RuntimeError('transient 429'), ('首个译文', 'm'),
                ('第二个译文', 'm')]

    def translate(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(accumulator, '_translate', translate)
    try:
        accumulator._process_preview_segment('thinking:llm-1', 'first reason')
        assert not accumulator._preview_disabled

        accumulator._process_preview_segment('thinking:llm-2', 'second reason')
        accumulator._process_preview_segment('thinking:llm-3', 'third reason')

        assert not accumulator._preview_disabled
        assert accumulator.translations['thinking:llm-3'] == '第二个译文'
    finally:
        incremental.cancel_incremental(task)


def test_repeated_preview_failures_still_retire_the_preview_path(
        monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)

    def always_fails(*_args, **_kwargs):
        raise RuntimeError('provider unavailable')

    monkeypatch.setattr(accumulator, '_translate', always_fails)
    try:
        for index in range(incremental._PREVIEW_FAILURE_LIMIT):
            accumulator._process_preview_segment(
                f'thinking:llm-{index}', f'reason {index}')

        assert accumulator._preview_disabled
        assert accumulator._preview_failures == (
            incremental._PREVIEW_FAILURE_LIMIT)
    finally:
        incremental.cancel_incremental(task)


def test_a_success_resets_the_consecutive_failure_count(monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    outcomes = [RuntimeError('one'), RuntimeError('two'),
                ('译文', 'm'), RuntimeError('three')]

    def translate(*_args, **_kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(accumulator, '_translate', translate)
    try:
        for index in range(4):
            accumulator._process_preview_segment(
                f'thinking:llm-{index}', f'reason {index}')

        assert not accumulator._preview_disabled
        assert accumulator._preview_failures == 1
    finally:
        incremental.cancel_incremental(task)
