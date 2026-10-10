"""Borrowed terminal snapshots stay immutable without decoding all cache bodies."""
import os
import threading
import tracemalloc

import pytest

from lib.translate._completed_cache import CompletedTranslationCache, _FrozenCache
from lib.translate import incremental

pytestmark = pytest.mark.unit


def test_pinned_snapshot_survives_replacement_reclamation_and_clear():
    cache = CompletedTranslationCache()
    cache.put_pair('key', 'old source', 'paid old translation')
    snapshot = cache.freeze()
    descriptor = snapshot._file.fileno()
    try:
        for index in range(100):cache.put_pair('key', f'new source {index}', f'new translation {index}')
        cache.clear()
        assert snapshot.originals['key'] == 'old source'
        assert snapshot.translations['key'] == 'paid old translation'
        assert not cache.snapshot()['fileOpen']
    finally:
        snapshot.close()
    with pytest.raises(OSError):os.fstat(descriptor)


def test_seek_fallback_reads_pinned_data_with_owner_lock(monkeypatch):
    cache = CompletedTranslationCache()
    lock = threading.Lock()
    cache.put_pair('key', 'source', 'translated')
    snapshot = cache.freeze(read_lock=lock)
    monkeypatch.setattr(os, 'pread', None)
    try:
        with lock:cache.put_pair('key', 'replacement', 'replacement')
        assert snapshot.originals['key'] == 'source'
        with lock:cache.clear()
        assert snapshot.translations['key'] == 'translated'
    finally:
        snapshot.close()


def test_terminal_reads_only_authoritative_keys_from_large_cache(monkeypatch):
    value = {'id': 'fixture', 'convId': 'conversation', '_turnId': 'turn', '_userId': 9,
             'config': {'autoTranslate': True, 'uiLang': 'zh'}}
    accumulator = incremental._Accumulator(value)
    source, translated = 'x' * (32 * 1024), 'y' * (32 * 1024)
    for index in range(2049):accumulator._cache.put_pair(f'block-{index}', source, translated)
    projection = {'segments': [{'type':'thinking','blockId':'block-2048','text':source}]}
    monkeypatch.setattr('lib.translate.commit.get_turn', lambda *args, **kwargs: {
        'status': 'completed', 'projectionRevision':1, 'projection':projection})
    committed = []
    def update(*args, **kwargs):
        committed.append(kwargs['projection'])
        return {'conversationRevision':2}
    monkeypatch.setattr('lib.translate.commit.update_turn_projection', update)
    monkeypatch.setattr('lib.conversations.notify_conv_changed', lambda *args, **kwargs: None)
    monkeypatch.setattr(accumulator, '_push', lambda *args: None)
    reads = []
    original_read = _FrozenCache._read
    def read(self, field, key):
        reads.append((field,key))
        return original_read(self,field,key)
    monkeypatch.setattr(_FrozenCache, '_read', read)
    tracemalloc.start()
    try:
        assert accumulator._commit_segments_only()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        accumulator._cache.clear()
    assert set(reads) == {('originals','block-2048'),('translations','block-2048')}
    assert len(reads) == 2
    assert peak < 8 * 1024 * 1024
    assert committed[0]['segments'][0]['translatedText'] == translated
    assert 'translatedText' not in projection['segments'][0]
    assert not accumulator._cache.snapshot()['fileOpen']
