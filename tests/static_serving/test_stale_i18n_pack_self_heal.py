"""Serving contracts for Vite-owned locale data assets."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from lib.http_transport.ui_languages import UI_LANGUAGES
from lib.vite_assets import VITE_MANIFEST, validate_vite_artifact
from tests._jsdom import require_shipped_graph


pytestmark = pytest.mark.unit


def _reachable_assets(manifest: dict, entry: str) -> set[str]:
    pending = [entry]
    visited: set[str] = set()
    assets: set[str] = set()
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        row = manifest[key]
        assets.update(row.get('assets') or ())
        pending.extend(row.get('imports') or ())
    return assets


def _locale_assets() -> dict[str, str]:
    require_shipped_graph()
    validate_vite_artifact()
    manifest = json.loads(Path(VITE_MANIFEST).read_text(encoding='utf-8'))
    assets = _reachable_assets(manifest, 'frontend/src/main.ts')
    return {
        language: next(
            asset for asset in assets
            if re.fullmatch(
                rf'assets/{language}\.generated-[A-Za-z0-9_-]{{8,}}\.json',
                asset,
            )
        )
        for language in UI_LANGUAGES
    }


@pytest.mark.parametrize('language', UI_LANGUAGES)
def test_current_locale_asset_is_served_immutable(flask_client, language):
    asset = _locale_assets()[language]
    response = flask_client.get('/static/vite/' + asset)
    assert response.status_code == 200
    cache = response.headers.get('Cache-Control', '')
    assert 'max-age=31536000' in cache and 'immutable' in cache


@pytest.mark.parametrize('language', UI_LANGUAGES)
def test_unknown_locale_hash_404s_without_cross_language_redirect(flask_client, language):
    response = flask_client.get(
        f'/static/vite/assets/{language}.generated-deadbeef.json',
    )
    assert response.status_code == 404
    assert not response.headers.get('Location')


def test_locale_assets_are_distinct_and_preserve_language_identity():
    assets = _locale_assets()
    assert len(set(assets.values())) == len(UI_LANGUAGES)
    for language, asset in assets.items():
        assert Path(asset).name.startswith(f'{language}.generated-')


def test_classic_i18n_pack_path_is_an_honest_404(flask_client):
    response = flask_client.get('/static/js/i18n-zh-9e07255b.js')
    assert response.status_code == 404
    assert not response.headers.get('Location')
