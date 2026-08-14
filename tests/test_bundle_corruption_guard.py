"""Fail-closed corruption guards for the prebuilt Vite application graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _install_graph(tmp_path: Path, monkeypatch, manifest: object, files=()):
    import lib.vite_assets as assets

    out = tmp_path / 'vite'
    (out / 'assets').mkdir(parents=True)
    for name in files:
        path = out / 'assets' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('// emitted\n', encoding='utf-8')
    manifest_path = out / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(assets, 'VITE_OUT_DIR', str(out))
    monkeypatch.setattr(assets, 'VITE_MANIFEST', str(manifest_path))
    assets.clear_vite_asset_cache()
    return assets


@pytest.mark.parametrize('value', (
    '../escape.js', 'assets/../escape.js', 'assets\\escape.js',
    '/assets/main.js', 'assets/main.js?old=1', 'assets/main.js#fragment',
    'assets//main.js', '', None,
))
def test_asset_paths_reject_escaping_or_ambiguous_values(value):
    from lib.vite_assets import _safe_asset_path

    assert _safe_asset_path(value) == ''


def test_asset_paths_accept_only_requested_suffixes():
    from lib.vite_assets import _safe_asset_path

    assert _safe_asset_path('assets/main-AbCd1234.js', ('.js', '.mjs'))
    assert not _safe_asset_path('assets/main-AbCd1234.css', ('.js', '.mjs'))


def test_manifest_rejects_missing_entry_file(tmp_path, monkeypatch):
    manifest = {
        'frontend/src/main.ts': {'file': 'assets/missing.js', 'isEntry': True},
        'frontend/src/admin.ts': {'file': 'assets/admin.js', 'isEntry': True},
    }
    assets = _install_graph(tmp_path, monkeypatch, manifest, ('admin.js',))
    with pytest.raises(assets.ViteAssetError, match='missing.js'):
        assets.validate_vite_artifact()


def test_manifest_rejects_dangling_import(tmp_path, monkeypatch):
    manifest = {
        'frontend/src/main.ts': {
            'file': 'assets/main.js', 'isEntry': True,
            'imports': ['frontend/src/missing.ts'],
        },
        'frontend/src/admin.ts': {'file': 'assets/admin.js', 'isEntry': True},
    }
    assets = _install_graph(tmp_path, monkeypatch, manifest, ('main.js', 'admin.js'))
    with pytest.raises(assets.ViteAssetError, match='reference.*missing'):
        assets.validate_vite_artifact()


@pytest.mark.parametrize('field,value', (
    ('imports', 'not-an-array'), ('dynamicImports', [42]),
))
def test_manifest_rejects_malformed_reference_lists(tmp_path, monkeypatch, field, value):
    manifest = {
        'frontend/src/main.ts': {'file': 'assets/main.js', 'isEntry': True, field: value},
        'frontend/src/admin.ts': {'file': 'assets/admin.js', 'isEntry': True},
    }
    assets = _install_graph(tmp_path, monkeypatch, manifest, ('main.js', 'admin.js'))
    with pytest.raises(assets.ViteAssetError, match=field):
        assets.validate_vite_artifact()


def test_manifest_rejects_unmarked_entry(tmp_path, monkeypatch):
    manifest = {
        'frontend/src/main.ts': {'file': 'assets/main.js'},
        'frontend/src/admin.ts': {'file': 'assets/admin.js', 'isEntry': True},
    }
    assets = _install_graph(tmp_path, monkeypatch, manifest, ('main.js', 'admin.js'))
    with pytest.raises(assets.ViteAssetError, match='has no entry'):
        assets.validate_vite_artifact()


def test_live_vite_graph_passes_the_same_strict_validator():
    from lib.vite_assets import validate_vite_artifact

    manifest = validate_vite_artifact()
    assert manifest['frontend/src/main.ts']['isEntry'] is True
    assert manifest['frontend/src/admin.ts']['isEntry'] is True


def test_release_build_validates_staging_before_atomic_manifest_commit():
    source = (ROOT / 'scripts/build_frontend.mjs').read_text(encoding='utf-8')
    build_at = source.index('await build(')
    validate_at = source.index('await validateManifest(nextManifest, temporaryDir)')
    copy_at = source.index('for (const asset of nextAssets)')
    commit_at = source.index("'manifest.json'), nextManifest")
    assert build_at < validate_at < copy_at < commit_at
    assert 'await rename(temporary, destination)' in source
    assert 'await rm(temporaryDir, { recursive: true, force: true })' in source
