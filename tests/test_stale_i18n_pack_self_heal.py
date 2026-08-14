"""Serving contracts for Vite-owned locale chunks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.vite_assets import VITE_MANIFEST, validate_vite_artifact


pytestmark = pytest.mark.unit
LOCALE_KEYS = {
    language: f'frontend/src/i18n/locales/{language}.json'
    for language in ('zh', 'en')
}


def _locale_assets() -> dict[str, str]:
    validate_vite_artifact()
    manifest = json.loads(Path(VITE_MANIFEST).read_text(encoding='utf-8'))
    return {language: manifest[key]['file'] for language, key in LOCALE_KEYS.items()}


@pytest.mark.parametrize('language', ('zh', 'en'))
def test_current_locale_chunk_is_served_immutable(flask_client, language):
    asset = _locale_assets()[language]
    response = flask_client.get('/static/vite/' + asset)
    assert response.status_code == 200
    cache = response.headers.get('Cache-Control', '')
    assert 'max-age=31536000' in cache and 'immutable' in cache


@pytest.mark.parametrize('language', ('zh', 'en'))
def test_unknown_locale_hash_404s_without_cross_language_redirect(flask_client, language):
    response = flask_client.get(f'/static/vite/assets/{language}-deadbeef.js')
    assert response.status_code == 404
    assert not response.headers.get('Location')


def test_locale_chunks_are_distinct_and_preserve_language_identity():
    assets = _locale_assets()
    assert assets['zh'] != assets['en']
    assert Path(assets['zh']).name.startswith('zh-')
    assert Path(assets['en']).name.startswith('en-')


def test_classic_i18n_pack_path_is_an_honest_404(flask_client):
    response = flask_client.get('/static/js/i18n-zh-9e07255b.js')
    assert response.status_code == 404
    assert not response.headers.get('Location')
