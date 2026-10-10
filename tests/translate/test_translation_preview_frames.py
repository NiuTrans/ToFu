"""Per-segment transport does not reserialize completed history."""
import json

import pytest

from lib.translate import incremental

pytestmark = pytest.mark.unit


def test_large_completed_cache_does_not_expand_each_progress_frame(monkeypatch):
    task = {'id': 'fixture', 'convId': 'conversation', '_turnId': 'turn', '_userId': 9,
            'config': {'autoTranslate': True, 'uiLang': 'zh'}}
    accumulator = incremental._Accumulator(task)
    for index in range(2000):
        accumulator.translations[f'text:llm-{index}'] = 'cached-translation' * 100
    frames = []
    monkeypatch.setattr(accumulator, '_push', frames.append)
    accumulator._push_segment_progress('text:llm-2000', 'current-partial')
    assert frames == [{'type': 'running', 'status': 'running', 'statusKind': 'in_progress',
                       'partialByRound': {'text:llm-2000': 'current-partial'}}]
    assert len(json.dumps(frames[0]).encode()) < 256


def test_preview_chunks_merge_to_every_completed_block(monkeypatch):
    accumulator = incremental._Accumulator({'id': 'fixture', 'convId': 'conversation',
        '_turnId': 'turn', '_userId': 9, 'config': {'autoTranslate': True, 'uiLang': 'zh'}})
    frames = []
    monkeypatch.setattr(accumulator, '_push', frames.append)
    def translate(original, progress_cb=None, **kwargs):
        progress_cb('partial:' + original)
        return 'translated:' + original, 'fixture-model'
    monkeypatch.setattr(accumulator, '_translate', translate)
    for index in range(1000):
        accumulator._translate_segment(f'text:llm-{index}', f'source-{index}', max_429_attempts=1, defer_on_shared_contention=False)
    merged = {}
    for frame in frames:
        assert len(frame['partialByRound']) == 1
        merged.update(frame['partialByRound'])
    assert len(merged) == 1000
    assert merged['text:llm-999'] == 'translated:source-999'
    assert sum(len(json.dumps(frame).encode()) for frame in frames) < 1000 * 400
