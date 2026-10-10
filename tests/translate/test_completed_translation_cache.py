"""Completed cache owns paired writes and metadata-only payload retention."""
import tracemalloc

import pytest
from tests.support.translation_commit_capture import capture_translation_kwargs

from lib.translate._completed_cache import CompletedTranslationCache
from lib.translate import incremental

pytestmark = pytest.mark.unit


def test_large_cache_keeps_bodies_on_one_file_and_releases_on_clear():
    cache = CompletedTranslationCache()
    originals, translations = cache.view('originals'), cache.view('translations')
    text = 'x' * (32 * 1024)
    tracemalloc.start()
    try:
        for index in range(2049):
            cache.put_pair(index, text, text)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    file = cache._file
    try:
        assert len(originals) == len(translations) == 2049
        assert cache.snapshot()['liveBytes'] == 2049 * 64 * 1024
        assert peak < 8 * 1024 * 1024
        assert originals[2048] == translations[2048] == text
    finally:
        originals.clear()
        translations.clear()
    assert file.closed
    assert cache.snapshot() == {'originals': 0, 'translations': 0, 'liveBytes': 0,
                                'fileBytes': 0, 'fileOpen': False}


def test_partial_pair_write_preserves_previous_paid_pair():
    cache = CompletedTranslationCache()
    source, target = cache.view('originals'), cache.view('translations')
    cache.put_pair('key', 'old source', 'old translation')
    file = cache._file
    class PartialWriter:
        def __init__(self):self.calls = 0
        def __getattr__(self, name):return getattr(file, name)
        def write(self, raw):
            self.calls += 1
            if self.calls == 1:return file.write(raw[:3])
            raise OSError('injected partial write')
    cache._file = PartialWriter()
    try:
        with pytest.raises(OSError):
            cache.put_pair('key', 'new source', 'new translation')
        assert source['key'] == 'old source' and target['key'] == 'old translation'
        assert len(source) == len(target) == 1
    finally:
        source.clear()
        target.clear()
    assert file.closed


def test_replaced_pairs_reclaim_obsolete_file_bytes():
    cache = CompletedTranslationCache()
    for index in range(1000):
        cache.put_pair('key', f'{index}:source', f'{index}:translation')
    try:
        assert cache.view('originals')['key'] == '999:source'
        assert cache.view('translations')['key'] == '999:translation'
        assert cache.snapshot()['fileBytes'] <= cache.snapshot()['liveBytes'] * 2
    finally:
        cache.view('originals').clear()
        cache.view('translations').clear()


def test_more_than_old_preview_maximum_survives_recreation_and_terminal_handoff(monkeypatch):
    task = {'id': 'demand', 'convId': 'conversation', '_turnId': 'turn', '_userId': 9,
            'config': {'autoTranslate': True, 'uiLang': 'zh'}}
    calls, commits, frames = [], [], []
    monkeypatch.setattr(incremental, '_PREVIEW_MIN_NARRATION_CHARS', 1)
    def translate(self, source, **kwargs):
        calls.append(source)
        return 'translated:' + source, 'fixture'
    monkeypatch.setattr(incremental._Accumulator, '_translate', translate)
    monkeypatch.setattr(incremental._Accumulator, '_push', lambda self, payload: frames.append(payload))
    monkeypatch.setattr('lib.translate.commit.commit_translation_to_turn',
        lambda *args, **kwargs: commits.append(capture_translation_kwargs(kwargs)))
    first = incremental._Accumulator(task)
    for index in range(1050):first._process_preview_segment(f'text:llm-{index}', str(index))
    second = incremental._Accumulator(task)
    for index in range(1050, 1100):second._process_preview_segment(f'text:llm-{index}', str(index))
    assert second._cache is first._cache
    assert len(calls) == 1100
    assert second._commit_segments_only()
    assert len(commits[0]['segment_translations']) == 1100
    assert commits[0]['segment_translations']['text:llm-1099'] == 'translated:1099'
    assert commits[0]['segment_sources']['text:llm-1099'] == '1099'
    assert not second._cache.snapshot()['fileOpen']
    assert not first.translations and not first.originals
    assert frames[-1] == {'type': 'done', 'status': 'done'}


def test_failed_terminal_commit_keeps_cache_for_retry(monkeypatch):
    task = {'id': 'retry', 'convId': 'conversation', '_turnId': 'turn', '_userId': 9,
            'config': {'autoTranslate': True, 'uiLang': 'zh'}}
    accumulator = incremental._Accumulator(task)
    accumulator._cache.put_pair('block', 'source', 'paid translation')
    def fail(*args, **kwargs):raise OSError('injected storage failure')
    monkeypatch.setattr('lib.translate.commit.commit_translation_to_turn', fail)
    assert not incremental.finalize_incremental_stamp_only(task)
    assert accumulator.translations['block'] == 'paid translation'
    assert accumulator._cache.snapshot()['fileOpen']
    monkeypatch.setattr('lib.translate.commit.commit_translation_to_turn', lambda *args, **kwargs: None)
    monkeypatch.setattr(incremental._Accumulator, '_push', lambda *args: None)
    assert incremental.finalize_incremental_stamp_only(task)
    assert not accumulator._cache.snapshot()['fileOpen']
