"""Incremental previews recover from provider failures within a running turn.

A single 429 or read timeout used to disable previews for the remainder of the
turn, so a long answer showed translated narration for its first rounds and
then silently reverted to the source language. Three consecutive misses later
retired previews for the whole turn, although the provider was usually healthy
again seconds later. Misses now pause previews for a bounded, doubling window,
and the blocks a failure or pause skipped are retried on later submissions.
Narration and reasoning count their misses separately: in one day's logs 120
of 121 preview failures were reasoning blocks, and their shared circuit kept
pausing narration.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def _task(task_id):
    return {
        'id': task_id, 'convId': 'conversation', '_turnId': 'turn',
        '_userId': 9, 'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }


def _accumulator(monkeypatch, task_id='preview-retry'):
    from lib.translate import incremental

    task = _task(task_id)
    accumulator = incremental._Accumulator(task)
    monkeypatch.setattr(accumulator, '_push', lambda _payload, **_options: None)
    return incremental, accumulator, task


def _clock(monkeypatch, incremental):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        incremental, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def _scripted(outcomes, calls=None):
    def translate(text, **_kwargs):
        if calls is not None:
            calls.append(text)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return translate


def _state(incremental, task):
    return task[incremental._TASK_PREVIEW_STATE]


def _deferred(incremental, task):
    return dict(
        _state(incremental, task).get(incremental._PREVIEW_STATE_DEFERRED)
        or {})


def _failures(incremental, task, kind):
    circuits = _state(incremental, task).get(
        incremental._PREVIEW_STATE_CIRCUITS) or {}
    return (circuits.get(kind) or {}).get(incremental._PREVIEW_STATE_FAILURES, 0)


def test_one_preview_failure_does_not_pause_the_rest_of_the_turn(
        monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    monkeypatch.setattr(accumulator, '_translate', _scripted([
        RuntimeError('transient 429'), ('首个译文', 'm'), ('第二个译文', 'm')]))
    try:
        accumulator._process_preview_segment('thinking:llm-1', 'first reason')
        assert not incremental._preview_paused(
            _state(incremental, task), 'reasoning')

        accumulator._process_preview_segment('thinking:llm-2', 'second reason')
        accumulator._process_preview_segment('thinking:llm-3', 'third reason')

        assert not incremental._preview_paused(
            _state(incremental, task), 'reasoning')
        assert accumulator.translations['thinking:llm-3'] == '第二个译文'
        assert _deferred(incremental, task) == {
            'thinking:llm-1': 'first reason'}
    finally:
        incremental.cancel_incremental(task)


def test_repeated_failures_pause_previews_until_the_window_ends(monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    clock = _clock(monkeypatch, incremental)
    calls = []
    monkeypatch.setattr(accumulator, '_translate', _scripted(
        [RuntimeError('provider unavailable')]
        * incremental._PREVIEW_FAILURE_LIMIT + [('恢复后的译文', 'm')], calls))
    try:
        for index in range(incremental._PREVIEW_FAILURE_LIMIT):
            accumulator._process_preview_segment(
                f'thinking:llm-{index}', f'reason {index}')
        state = _state(incremental, task)
        assert incremental._preview_paused(state, 'reasoning')
        assert _failures(incremental, task, 'reasoning') == (
            incremental._PREVIEW_FAILURE_LIMIT)

        accumulator._process_preview_segment('thinking:llm-8', 'paused reason')
        assert len(calls) == incremental._PREVIEW_FAILURE_LIMIT
        assert _deferred(incremental, task)['thinking:llm-8'] == 'paused reason'

        clock.now += incremental._PREVIEW_PAUSE_BASE_SECONDS
        assert not incremental._preview_paused(state, 'reasoning')
        accumulator._process_preview_segment('thinking:llm-9', 'fresh reason')

        assert accumulator.translations['thinking:llm-9'] == '恢复后的译文'
        assert _failures(incremental, task, 'reasoning') == 0
        circuit = state[incremental._PREVIEW_STATE_CIRCUITS]['reasoning']
        assert incremental._PREVIEW_STATE_PAUSE_SECONDS not in circuit
        assert incremental._PREVIEW_STATE_PAUSED_UNTIL not in circuit
    finally:
        incremental.cancel_incremental(task)


def test_each_failed_probe_doubles_the_pause_up_to_its_cap(monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    clock = _clock(monkeypatch, incremental)
    monkeypatch.setattr(
        accumulator, '_translate', _scripted([RuntimeError('down')] * 16))
    try:
        for index in range(incremental._PREVIEW_FAILURE_LIMIT):
            accumulator._process_preview_segment(f'thinking:llm-{index}', 'x')
        circuit = _state(incremental, task)[
            incremental._PREVIEW_STATE_CIRCUITS]['reasoning']
        pauses = []
        for probe in range(6):
            paused_until = circuit[incremental._PREVIEW_STATE_PAUSED_UNTIL]
            pauses.append(paused_until - clock.now)
            clock.now = paused_until
            accumulator._process_preview_segment(f'thinking:probe-{probe}', 'x')

        assert pauses == [60.0, 120.0, 240.0, 480.0, 600.0, 600.0]
    finally:
        incremental.cancel_incremental(task)


def test_a_success_resets_the_consecutive_failure_count(monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    monkeypatch.setattr(accumulator, '_translate', _scripted([
        RuntimeError('one'), RuntimeError('two'), ('译文', 'm'),
        RuntimeError('three')]))
    try:
        for index in range(4):
            accumulator._process_preview_segment(
                f'thinking:llm-{index}', f'reason {index}')

        assert not incremental._preview_paused(
            _state(incremental, task), 'reasoning')
        assert _failures(incremental, task, 'reasoning') == 1
    finally:
        incremental.cancel_incremental(task)


def test_deferred_blocks_queue_behind_the_next_accepted_submission(
        monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    monkeypatch.setattr(
        accumulator, '_translate', _scripted([RuntimeError('429')]))
    try:
        accumulator._process_preview_segment('thinking:llm-1', 'first reason')
        assert accumulator.submit('thinking:llm-2', 'second reason')

        assert [accumulator._queue.get(0) for _ in range(2)] == [
            ('segment', 'thinking:llm-2', 'second reason'),
            ('segment', 'thinking:llm-1', 'first reason'),
        ]
        assert _deferred(incremental, task) == {}
    finally:
        incremental.cancel_incremental(task)


def test_a_newer_submission_supersedes_its_deferred_source(monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    monkeypatch.setattr(
        accumulator, '_translate', _scripted([RuntimeError('429')]))
    try:
        accumulator._process_preview_segment('thinking:llm-1', 'old reason')
        assert accumulator.submit('thinking:llm-1', 'edited reason')

        assert accumulator._queue.get(0) == (
            'segment', 'thinking:llm-1', 'edited reason')
        assert accumulator._queue.snapshot()['depth'] == 0
    finally:
        incremental.cancel_incremental(task)


def test_a_block_stops_retrying_after_its_attempt_budget(monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    calls = []
    outcomes = []
    for _ in range(incremental._PREVIEW_MAX_ATTEMPTS_PER_BLOCK):
        outcomes += [RuntimeError('rejected block'), ('健康', 'm')]
    monkeypatch.setattr(accumulator, '_translate', _scripted(outcomes, calls))
    try:
        for attempt in range(incremental._PREVIEW_MAX_ATTEMPTS_PER_BLOCK):
            accumulator._process_preview_segment('thinking:bad', 'bad source')
            # A healthy neighbour keeps the circuit closed between retries.
            accumulator._process_preview_segment(
                f'thinking:ok-{attempt}', f'good source {attempt}')

        assert calls.count('bad source') == (
            incremental._PREVIEW_MAX_ATTEMPTS_PER_BLOCK)
        assert 'thinking:bad' not in _deferred(incremental, task)
        assert accumulator.submit('thinking:next', 'next source')
        assert accumulator._queue.get(0)[1] == 'thinking:next'
        assert accumulator._queue.snapshot()['depth'] == 0
    finally:
        incremental.cancel_incremental(task)


def test_paused_submissions_resume_with_their_backlog(monkeypatch):
    from lib.translate import incremental

    clock = _clock(monkeypatch, incremental)
    monkeypatch.setattr(incremental, '_accumulators', {})
    started = []
    monkeypatch.setattr(
        incremental._Accumulator, 'start', lambda self: started.append(self))
    task = _task('paused-backlog')
    incremental._preview_circuit(
        incremental._task_preview_state(task, create=True), 'narration')[
        incremental._PREVIEW_STATE_PAUSED_UNTIL] = clock.now + 60
    try:
        assert incremental.submit_round_segment(
            task, 1, 'narration during the pause') is False
        assert incremental._accumulators == {}

        clock.now += 60
        assert incremental.submit_round_segment(
            task, 2, 'narration after the pause') is True

        accumulator = incremental._accumulators[task['id']]
        assert started == [accumulator]
        assert [accumulator._queue.get(0)[1:] for _ in range(2)] == [
            (incremental._model_batch_segment_key(task, 'text', 2),
             'narration after the pause'),
            (incremental._model_batch_segment_key(task, 'text', 1),
             'narration during the pause'),
        ]
    finally:
        incremental.cancel_incremental(task)


@pytest.mark.parametrize('failing_kind, failing_key, healthy_kind, healthy_key', [
    ('reasoning', 'thinking:llm-{}', 'narration', 'text:llm-{}'),
    ('narration', 'text:llm-{}', 'reasoning', 'thinking:llm-{}'),
])
def test_one_kinds_failures_never_pause_or_defer_the_other(
        monkeypatch, failing_kind, failing_key, healthy_kind, healthy_key):
    incremental, accumulator, task = _accumulator(
        monkeypatch, f'per-kind-{failing_kind}')
    _clock(monkeypatch, incremental)
    calls = []
    monkeypatch.setattr(accumulator, '_translate', _scripted(
        [RuntimeError('provider rejected the block')]
        * incremental._PREVIEW_FAILURE_LIMIT + [('健康译文', 'm')], calls))
    try:
        failed = [failing_key.format(index)
                  for index in range(incremental._PREVIEW_FAILURE_LIMIT)]
        for key in failed:
            accumulator._process_preview_segment(key, f'source of {key}')
        state = _state(incremental, task)
        assert incremental._preview_paused(state, failing_kind)
        assert not incremental._preview_paused(state, healthy_kind)

        healthy = healthy_key.format(9)
        accumulator._process_preview_segment(healthy, 'healthy source')

        assert calls[-1] == 'healthy source'
        assert accumulator.translations[healthy] == '健康译文'
        assert set(_deferred(incremental, task)) == set(failed)
        # The other kind's success neither lifts nor resets this pause.
        assert incremental._preview_paused(state, failing_kind)
        assert _failures(incremental, task, failing_kind) == (
            incremental._PREVIEW_FAILURE_LIMIT)
    finally:
        incremental.cancel_incremental(task)


def test_a_reasoning_pause_admits_narration_ahead_of_the_reasoning_backlog(
        monkeypatch):
    from lib.translate import incremental

    clock = _clock(monkeypatch, incremental)
    monkeypatch.setattr(incremental, '_accumulators', {})
    monkeypatch.setattr(incremental._Accumulator, 'start', lambda self: None)
    task = _task('reasoning-paused-producer')
    incremental._preview_circuit(
        incremental._task_preview_state(task, create=True), 'reasoning')[
        incremental._PREVIEW_STATE_PAUSED_UNTIL] = clock.now + 60

    def text(round_number):
        return incremental._model_batch_segment_key(task, 'text', round_number)

    def thinking(round_number):
        return incremental._model_batch_segment_key(
            task, 'thinking', round_number)

    try:
        assert incremental.submit_thinking_segment(
            task, 'thinking:llm-1', 'paused reasoning') is False
        assert incremental.submit_round_segment(task, 1, 'narration') is True

        accumulator = incremental._accumulators[task['id']]
        assert accumulator._queue.get(0)[1:] == (text(1), 'narration')
        assert accumulator._queue.snapshot()['depth'] == 0
        assert _deferred(incremental, task) == {thinking(1): 'paused reasoning'}

        clock.now += 60
        assert incremental.submit_thinking_segment(
            task, 'thinking:llm-2', 'fresh reasoning')
        assert incremental.submit_round_segment(task, 2, 'later narration')

        assert [accumulator._queue.get(0)[1:] for _ in range(3)] == [
            (text(2), 'later narration'),
            (thinking(2), 'fresh reasoning'),
            (thinking(1), 'paused reasoning'),
        ]
    finally:
        incremental.cancel_incremental(task)


def test_handoff_and_cancellation_release_deferred_sources(monkeypatch):
    incremental, accumulator, task = _accumulator(monkeypatch)
    monkeypatch.setattr(
        accumulator, '_translate', _scripted([RuntimeError('429')] * 2))
    accumulator._process_preview_segment('thinking:llm-1', 'first reason')
    deferred = _state(incremental, task)[incremental._PREVIEW_STATE_DEFERRED]
    assert len(deferred) == 1

    incremental._clear_task_preview_state(task)

    assert len(deferred) == 0
    assert incremental._TASK_PREVIEW_STATE not in task

    incremental, accumulator, task = _accumulator(monkeypatch, 'cancelled')
    monkeypatch.setattr(
        accumulator, '_translate', _scripted([RuntimeError('429')]))
    accumulator._process_preview_segment('thinking:llm-1', 'first reason')
    deferred = _state(incremental, task)[incremental._PREVIEW_STATE_DEFERRED]

    incremental.cancel_incremental(task)

    assert len(deferred) == 0
    assert incremental._defer_preview(
        accumulator._preview_state, 'thinking:llm-2', 'late reason') is False
