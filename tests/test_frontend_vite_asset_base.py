"""Vite assets must retain an opaque reverse-proxy path prefix."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
_AUDIT_SYNTHETIC_REPO_PATHS = {'static/vite/assets/main-hash.js'}


def test_vite_build_and_entry_tags_use_relative_asset_paths():
    config = (ROOT / 'vite.config.mjs').read_text(encoding='utf-8')
    assert "base: './'" in config
    assets = (ROOT / 'lib/vite_assets.py').read_text(encoding='utf-8')
    assert 'src="static/vite/{html.escape(source, quote=True)}"' in assets
    assert 'href="static/vite/{html.escape(asset, quote=True)}"' in assets
    assert 'src="/static/vite/' not in assets


def test_relative_entry_url_keeps_vscode_proxy_prefix():
    page = 'https://example.test/proxy/15000/'
    asset = 'static/vite/assets/main-hash.js'
    assert urljoin(page, asset) == (
        'https://example.test/proxy/15000/static/vite/assets/main-hash.js')


def test_startup_diagnostics_capture_failed_module_url():
    index = (ROOT / 'index.html').read_text(encoding='utf-8')
    assert 'var asset = eventAsset(event)' in index
    assert 'firstAsset = firstAsset || asset' in index
    assert 'target.src || target.href' in index
    assert 'diagnose(error, errorAsset(error) || firstAsset)' in index


def test_stale_absolute_entry_self_heals_under_proxy_prefix():
    index = (ROOT / 'index.html').read_text(encoding='utf-8')
    assert "failed.pathname.indexOf('/static/vite/')" in index
    assert "new URL('./', location.href)" in index
    assert "replacement.setAttribute('data-tofu-proxy-retry', '1')" in index
    assert 'if (retryProxyAsset(event, asset))' in index
