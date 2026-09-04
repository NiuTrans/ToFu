"""Vite assets must retain an opaque reverse-proxy path prefix."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import urljoin

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
_AUDIT_SYNTHETIC_REPO_PATHS = {'static/vite/assets/main-hash.js'}


def test_vite_build_uses_a_relative_asset_base():
    probe = subprocess.run(
        [
            'node', '--input-type=module', '-e',
            "import config from './vite.config.mjs'; "
            "process.stdout.write(JSON.stringify(config.base));",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(probe.stdout) == './'


def test_entry_tag_behavior_keeps_asset_urls_relative(monkeypatch, tmp_path):
    from lib import vite_assets

    output = tmp_path / 'vite'
    (output / 'assets').mkdir(parents=True)
    (output / 'assets' / 'main-hash.js').write_text('export {};')
    (output / 'assets' / 'main-hash.css').write_text('body {}')
    manifest_path = output / 'manifest.json'
    manifest_path.write_text('{}')
    manifest = {
        vite_assets.VITE_ENTRY: {
            'isEntry': True,
            'file': 'assets/main-hash.js',
            'css': ['assets/main-hash.css'],
        },
    }
    monkeypatch.setattr(vite_assets, 'VITE_OUT_DIR', str(output))
    monkeypatch.setattr(vite_assets, 'VITE_MANIFEST', str(manifest_path))
    monkeypatch.setattr(
        vite_assets, '_load_manifest', lambda _entries, **_options: manifest)
    vite_assets.clear_vite_asset_cache()

    tags = vite_assets.get_vite_asset_tags()

    assert 'src="static/vite/assets/main-hash.js"' in tags
    assert 'href="static/vite/assets/main-hash.css"' in tags
    assert 'src="/static/vite/' not in tags
    assert 'href="/static/vite/' not in tags


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
