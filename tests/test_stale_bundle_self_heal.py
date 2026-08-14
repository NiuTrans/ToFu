"""Cache and miss behavior for content-hashed Vite assets."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.vite_assets import VITE_MANIFEST, validate_vite_artifact


pytestmark = pytest.mark.unit


def _current_main_asset() -> str:
    manifest = validate_vite_artifact(('main',))
    return manifest['frontend/src/main.ts']['file']


def test_current_vite_asset_is_served_immutable(flask_client):
    asset = _current_main_asset()
    response = flask_client.get('/static/vite/' + asset)
    assert response.status_code == 200
    cache = response.headers.get('Cache-Control', '')
    assert 'max-age=31536000' in cache and 'immutable' in cache


def test_unknown_content_hashed_asset_404s_honestly(flask_client):
    current = Path(_current_main_asset())
    stale = current.with_name('main-deadbeef.js').as_posix()
    assert stale != current.as_posix()
    response = flask_client.get('/static/vite/' + stale)
    assert response.status_code == 404
    assert not response.headers.get('Location')


def test_legacy_classic_bundle_path_no_longer_self_heals(flask_client):
    response = flask_client.get('/static/js/bundle-95e8203d.js')
    assert response.status_code == 404
    assert not response.headers.get('Location')


def test_manifest_is_never_cached_as_an_immutable_asset(flask_client):
    response = flask_client.get('/static/vite/' + Path(VITE_MANIFEST).name)
    assert response.status_code == 200
    cache = response.headers.get('Cache-Control', '')
    assert 'no-store' in cache
    assert 'immutable' not in cache
