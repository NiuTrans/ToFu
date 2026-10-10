"""An idle-retired translation queue must reject work so callers can recover."""

import queue

import pytest
from tests.support.translation_commit_capture import capture_translation_kwargs

from lib.translate._operation_buffer import IncrementalOperationBuffer

pytestmark = pytest.mark.unit


def test_idle_retirement_rejects_terminal_handoff_and_preview():
    buffer = IncrementalOperationBuffer(2)
    with pytest.raises(queue.Empty):
        buffer.get(0)
    # The consumer has committed to exit; accepting this loses the final
    # translation while telling the coordinator no fallback is necessary.
    assert buffer.put_terminal(('finalize', 'final response'), replace=True) == -1
    assert buffer.put_segment('text:1', 'preview') == -1


def test_terminal_consumption_rejects_duplicate_handoffs():
    buffer = IncrementalOperationBuffer(2)
    assert buffer.put_terminal(('finalize', 'final response'), replace=True) == 0
    assert buffer.get(0) == ('finalize', 'final response')
    assert buffer.put_terminal(('finalize', 'duplicate'), replace=True) == -1


def test_queued_terminal_can_be_replaced_before_consumption():
    buffer = IncrementalOperationBuffer(2)
    buffer.put_terminal(('finalize', 'first'), replace=True)
    assert buffer.put_terminal(('stamp',), replace=True) == 0
    assert buffer.get(0) == ('stamp',)


def test_retired_accumulator_handoff_declines_for_whole_turn_fallback(monkeypatch):
    import lib.translate.incremental as incremental

    task = {
        'id': 'retiring-task', 'convId': 'conversation-retiring',
        '_turnId': 'turn-retiring', '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    accumulator = incremental._Accumulator(task)
    # Reproduce the gap after the consumer timed out but before its finally
    # block removes the registry entry. No sleeps or real worker are needed.
    with pytest.raises(queue.Empty):
        accumulator._queue.get(0)
    monkeypatch.setattr(incremental, '_accumulators', {task['id']: accumulator})
    assert incremental.finalize_incremental(task, 'Complete response') is False
    assert incremental.finalize_incremental_stamp_only(task) is False


def _cached_accumulator(monkeypatch):
    import lib.translate.incremental as incremental

    task = {
        'id': 'cached-retiring-task', 'convId': 'conversation-retiring',
        '_turnId': 'turn-retiring', '_attemptId': 'attempt-retiring',
        '_userId': 9, 'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    accumulator = incremental._Accumulator(task)
    key = 'thinking:attempt-attempt-retiring:llm-0'
    monkeypatch.setattr(
        accumulator, '_translate', lambda *args, **kwargs: ('已完成推理译文', 'm'),
    )
    monkeypatch.setattr(accumulator, '_push', lambda _payload: None)
    accumulator._process_preview_segment(key, 'Completed reasoning')
    monkeypatch.setattr(incremental, '_accumulators', {task['id']: accumulator})
    return incremental, task, accumulator, key


def test_idle_retirement_keeps_cache_until_terminal_without_model_work(monkeypatch):
    incremental, task, accumulator, key = _cached_accumulator(monkeypatch)
    commits = []
    monkeypatch.setattr(
        'lib.translate.commit.commit_translation_to_turn',
        lambda *args, **kwargs: commits.append((args, capture_translation_kwargs(kwargs))),
    )
    monkeypatch.setattr(
        accumulator, '_translate',
        lambda *args, **kwargs: pytest.fail('retirement must not call a provider'),
    )

    accumulator._run()

    assert incremental._accumulators == {}
    assert commits == []
    assert incremental.finalize_incremental_stamp_only(task)
    assert commits[0][0] == ('conversation-retiring', 'turn-retiring', None, '')
    assert commits[0][1]['user_id'] == 9
    assert commits[0][1]['segment_translations'] == {key: '已完成推理译文'}
    assert commits[0][1]['segment_sources'] == {key: 'Completed reasoning'}
    assert accumulator.stamp_only() is False


@pytest.mark.parametrize('failure', ['running', 'storage'])
def test_retired_cache_survives_until_settlement_without_retranslation(
        monkeypatch, failure):
    from lib.storage.errors import StorageError
    from lib.turn_lifecycle import LifecycleConflict

    incremental, task, accumulator, key = _cached_accumulator(monkeypatch)
    commits = []

    def unavailable(*args, **kwargs):
        if failure == 'running':
            raise LifecycleConflict('turn_in_progress', 'still generating')
        raise StorageError('database_unavailable', 'temporary storage outage')

    monkeypatch.setattr(
        'lib.translate.commit.commit_translation_to_turn', unavailable,
    )
    accumulator._run()
    assert incremental._accumulators == {}
    assert incremental.finalize_incremental_stamp_only(task) is False

    # A later accumulator shares the completed file-backed cache but not a worker.
    recreated = incremental._Accumulator(task)
    assert recreated.translations == {key: '已完成推理译文'}
    monkeypatch.setattr(
        'lib.translate.commit.commit_translation_to_turn',
        lambda *args, **kwargs: commits.append((args, capture_translation_kwargs(kwargs))),
    )
    assert incremental.finalize_incremental_stamp_only(task) is True
    assert commits[0][1]['segment_translations'] == {key: '已完成推理译文'}
    assert incremental._TASK_PREVIEW_STATE not in task


def test_terminal_handoff_flushes_cache_after_queue_closed_before_registry_removal(
        monkeypatch):
    incremental, task, accumulator, key = _cached_accumulator(monkeypatch)
    with pytest.raises(queue.Empty):
        accumulator._queue.get(0)
    commits = []
    monkeypatch.setattr(
        'lib.translate.commit.commit_translation_to_turn',
        lambda *args, **kwargs: commits.append((args, capture_translation_kwargs(kwargs))),
    )

    assert incremental.finalize_incremental_stamp_only(task) is True
    assert commits[0][1]['segment_translations'] == {key: '已完成推理译文'}
    assert incremental._TASK_PREVIEW_STATE not in task


def test_cancelled_retirement_does_not_commit_or_keep_cached_overlays(monkeypatch):
    incremental, task, accumulator, _key = _cached_accumulator(monkeypatch)
    with pytest.raises(queue.Empty):
        accumulator._queue.get(0)
    monkeypatch.setattr(
        'lib.translate.commit.commit_translation_to_turn',
        lambda *args, **kwargs: pytest.fail('cancel must suppress overlay writes'),
    )

    # cancel must invalidate cached data even when the closed queue rejects it.
    assert incremental.cancel_incremental(task) is False
    accumulator._run()
    assert incremental._TASK_PREVIEW_STATE not in task
    assert incremental.finalize_incremental_stamp_only(task) is False


def test_completed_cache_keeps_all_paid_results_across_retirement(monkeypatch):
    incremental, task, accumulator, key = _cached_accumulator(monkeypatch)
    monkeypatch.setattr(
        'lib.translate.commit.commit_translation_to_turn',
        lambda *args, **kwargs: None,
    )
    accumulator._process_preview_segment('thinking:two', 'second reasoning')
    accumulator._run()
    recreated = incremental._Accumulator(task)
    monkeypatch.setattr(recreated, '_translate', lambda *args, **kwargs: ('paid-result', 'fixture'))
    for number in range(100):
        recreated._process_preview_segment(f'thinking:later-{number}', 'later reasoning')
    recreated._process_preview_segment('thinking:terminal', '<notranslate>final</notranslate>')

    assert {key, 'thinking:two', 'thinking:terminal', 'thinking:later-99'} <= set(recreated.translations)
    assert len(recreated.translations) == len(recreated.originals) == 103
    assert incremental.cancel_incremental(task) is False
    assert recreated.translations == {}
    assert recreated.originals == {}


def test_complete_long_output_is_retained_in_file_cache(monkeypatch):
    incremental, task, accumulator, _key = _cached_accumulator(monkeypatch)
    translated = '完整译文。' * 30_000
    monkeypatch.setattr(accumulator, '_translate', lambda *args, **kwargs: (translated, 'm'))
    try:
        accumulator._process_preview_segment('thinking:long', 'small source')
        assert accumulator.translations['thinking:long'] == translated
        assert accumulator.originals['thinking:long'] == 'small source'
        assert not accumulator._preview_disabled
    finally:
        incremental.cancel_incremental(task)


def test_complete_long_narration_is_spooled_without_truncation(monkeypatch):
    incremental, task, accumulator, _key = _cached_accumulator(monkeypatch)
    source = 'English narration. ' * 10_000
    try:
        assert accumulator.submit('text:long', source)
        assert accumulator._queue.get(0) == ('segment', 'text:long', source)
    finally:
        incremental.cancel_incremental(task)


def test_reference_preview_cache_measures_full_file_backed_retention(monkeypatch):
    import lib.translate.incremental as incremental

    task = {
        'id': 'cache-measurement', 'convId': 'cache-measurement-conversation',
        '_turnId': 'cache-measurement-turn', '_userId': 9,
        'config': {'autoTranslate': True, 'uiLang': 'zh'},
    }
    source = 'e' * 32_000
    translated = '译' * 64_000
    accumulator = incremental._Accumulator(task)
    monkeypatch.setattr(
        accumulator, '_translate', lambda *args, **kwargs: (translated, 'm'),
    )
    monkeypatch.setattr(accumulator, '_push', lambda _payload: None)
    for number in range(100):
        accumulator._process_preview_segment(f'thinking:round-{number}', source)
    accumulator._process_preview_segment('thinking:terminal', source)

    assert len(accumulator.translations) == len(accumulator.originals) == 101
    retained_characters = sum(map(len, accumulator.translations.values())) + sum(
        map(len, accumulator.originals.values()))
    assert retained_characters == 9_696_000
    assert incremental.cancel_incremental(task) is False
    assert accumulator.translations == accumulator.originals == {}
