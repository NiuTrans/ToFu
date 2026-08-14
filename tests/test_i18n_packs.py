"""Exactness contracts for the Vite-owned locale dictionaries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / 'frontend/src/i18n/locales'
I18N_OWNER = ROOT / 'frontend/src/i18n/index.ts'


def _load(language: str) -> dict[str, str]:
    value = json.loads((LOCALES / f'{language}.json').read_text(encoding='utf-8'))
    assert isinstance(value, dict)
    return value


def test_both_locales_have_the_exact_same_complete_key_set():
    zh = _load('zh')
    en = _load('en')
    assert zh
    assert zh.keys() == en.keys()
    assert all(isinstance(key, str) and key for key in zh)
    assert all(isinstance(value, str) for value in zh.values())
    assert all(isinstance(value, str) for value in en.values())


def test_locale_json_roundtrips_without_shape_or_unicode_loss():
    for language in ('zh', 'en'):
        path = LOCALES / f'{language}.json'
        raw = path.read_text(encoding='utf-8')
        decoded = json.loads(raw)
        encoded = json.dumps(decoded, ensure_ascii=False)
        assert json.loads(encoded) == decoded
    assert any('\u4e00' <= char <= '\u9fff' for char in ''.join(_load('zh').values()))


def test_i18n_owner_loads_only_supported_locale_chunks_and_falls_back_safely():
    source = I18N_OWNER.read_text(encoding='utf-8')
    assert "zh: () => import('./locales/zh.json')" in source
    assert "en: () => import('./locales/en.json')" in source
    assert "primary?.[key] ?? fallback?.[key]" in source
    assert "language !== 'zh' && language !== 'en'" in source
    assert 'window.t' not in source


def test_referenced_locale_files_are_only_the_two_supported_languages():
    assert sorted(path.name for path in LOCALES.glob('*.json')) == ['en.json', 'zh.json']
