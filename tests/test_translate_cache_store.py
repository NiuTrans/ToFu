#!/usr/bin/env python3
"""Atomic-write and malformed-payload guards for translation disk caches."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest


pytestmark = pytest.mark.unit


def test_translate_cache_failed_replace_keeps_old_entry_and_no_temp(
        monkeypatch, tmp_path):
    from lib import translate_cache as cache

    monkeypatch.setattr(cache, '_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(cache, '_initialized', False)
    monkeypatch.setattr(cache, '_ENABLED', True)
    cache.put('text', 'en', 'zh', 'old', model='old-model')
    path = Path(cache._path_for(cache._key('text', 'en', 'zh')))
    before = path.read_bytes()

    with mock.patch('lib.json_store.os.replace',
                    side_effect=OSError('injected replace failure')):
        cache.put('text', 'en', 'zh', 'new', model='new-model')

    assert path.read_bytes() == before
    assert cache.get('text', 'en', 'zh')['translated'] == 'old'
    assert not list(path.parent.glob(f'.{path.name}-*.tmp'))


@pytest.mark.parametrize('payload', [
    ['not', 'an', 'object'],
    {'translated': {'not': 'text'}, 'model': 'bad'},
    {'translated': 'text', 'model': ['not', 'text']},
])
def test_translate_cache_valid_json_with_invalid_shape_is_a_miss(
        monkeypatch, tmp_path, payload):
    from lib import translate_cache as cache

    monkeypatch.setattr(cache, '_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(cache, '_initialized', False)
    monkeypatch.setattr(cache, '_ENABLED', True)
    path = Path(cache._path_for(cache._key('text', 'en', 'zh')))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding='utf-8')

    assert cache.get('text', 'en', 'zh') is None


def test_refusal_failed_replace_keeps_old_entry_and_no_temp(
        monkeypatch, tmp_path):
    from lib import translate_refusal as refusal

    monkeypatch.setattr(refusal, '_REFUSAL_DIR', str(tmp_path))
    monkeypatch.setattr(refusal, '_ENABLED', True)
    refusal.put('text', 'en', 'zh', verdict='noop', reason='old')
    path = Path(refusal._path_for(refusal._key('text', 'en', 'zh')))
    before = path.read_bytes()

    with mock.patch('lib.json_store.os.replace',
                    side_effect=OSError('injected replace failure')):
        refusal.put('text', 'en', 'zh', verdict='flip', reason='new')

    assert path.read_bytes() == before
    assert refusal.get('text', 'en', 'zh')['reason'] == 'old'
    assert not list(path.parent.glob(f'.{path.name}-*.tmp'))


@pytest.mark.parametrize('timestamp', ['yesterday', None, True, float('nan')])
def test_refusal_malformed_timestamp_is_a_cache_miss(
        monkeypatch, tmp_path, timestamp):
    from lib import translate_refusal as refusal

    monkeypatch.setattr(refusal, '_REFUSAL_DIR', str(tmp_path))
    monkeypatch.setattr(refusal, '_ENABLED', True)
    path = Path(refusal._path_for(refusal._key('text', 'en', 'zh')))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        'verdict': 'noop', 'reason': 'bad timestamp', 'ts': timestamp,
    }), encoding='utf-8')

    assert refusal.get('text', 'en', 'zh') is None
