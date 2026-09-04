"""Freshness and publication contracts for the prebuilt Vite graph."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from lib.vite_assets import (
    I18N_CATALOG_DIGEST_FIELD,
    VITE_AUTHORING_DIGEST_FIELD,
    VITE_ENTRIES,
    VITE_ENTRY,
    VITE_MANIFEST,
    _source_i18n_catalog_digest,
    _source_vite_authoring_digest,
    _validate_vite_authoring_digest,
    validate_published_vite_artifact,
    validate_vite_artifact,
)


pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]
ROOT = Path(__file__).resolve().parents[1]
HASHED_ASSET = re.compile(r'^assets/.+-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$')


def _frontend_inputs() -> list[Path]:
    inputs = [
        ROOT / 'package.json',
        ROOT / 'package-lock.json',
        ROOT / 'vite.config.mjs',
        ROOT / 'scripts/build_frontend.mjs',
    ]
    inputs.extend(path for path in (ROOT / 'frontend/src').rglob('*') if path.is_file())
    return inputs


def test_manifest_is_valid_complete_and_content_hashed():
    manifest = validate_vite_artifact()
    assert set(VITE_ENTRIES.values()).issubset(manifest)
    for entry in VITE_ENTRIES.values():
        row = manifest[entry]
        assert row['isEntry'] is True
        assert HASHED_ASSET.fullmatch(row['file']), row
    for row in manifest.values():
        assert HASHED_ASSET.fullmatch(row['file']), row
    assert manifest[VITE_ENTRY][I18N_CATALOG_DIGEST_FIELD] == (
        _source_i18n_catalog_digest())
    assert manifest[VITE_ENTRY][VITE_AUTHORING_DIGEST_FIELD] == (
        _source_vite_authoring_digest())


def test_runtime_graph_survives_locale_source_edits_until_atomic_publish(
        tmp_path, monkeypatch):
    from lib import vite_assets

    locale_paths = tuple(
        tmp_path / f'{language}.json' for language in ('zh', 'en'))
    for path in locale_paths:
        path.write_text('{"editedAfterPublish": true}\n', encoding='utf-8')
    monkeypatch.setattr(
        vite_assets, 'I18N_LOCALE_PATHS',
        tuple(str(path) for path in locale_paths),
    )

    with pytest.raises(vite_assets.ViteAssetError, match='i18n chunks are stale'):
        validate_vite_artifact()

    manifest = validate_published_vite_artifact()
    assert manifest[VITE_ENTRY]['isEntry'] is True


def test_manifest_is_at_least_as_fresh_as_every_frontend_input():
    manifest_mtime = Path(VITE_MANIFEST).stat().st_mtime_ns
    newest_mtime, newest_path = max(
        (path.stat().st_mtime_ns, path) for path in _frontend_inputs()
    )
    assert manifest_mtime >= newest_mtime, (
        f'Vite manifest is stale: {newest_path.relative_to(ROOT)} is newer; '
        'run `npm run build` before publishing')


def test_authoring_digest_rejects_non_i18n_content_drift_with_same_mtime(
        tmp_path, monkeypatch):
    from lib import vite_assets

    source_path = tmp_path / 'conversation.ts'
    source_path.write_text('export const revision = 1;\n', encoding='utf-8')
    original_stat = source_path.stat()
    monkeypatch.setattr(
        vite_assets, 'vite_authoring_inputs', lambda: (str(source_path),))
    manifest = {VITE_ENTRY: {
        VITE_AUTHORING_DIGEST_FIELD: _source_vite_authoring_digest(),
    }}

    source_path.write_text('export const revision = 2;\n', encoding='utf-8')
    os.utime(
        source_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )

    with pytest.raises(ValueError, match='authoring inputs are stale'):
        _validate_vite_authoring_digest(
            manifest, validate_authoring_sources=True)
    _validate_vite_authoring_digest(
        manifest, validate_authoring_sources=False)


def test_manifest_commit_point_is_written_after_all_assets():
    manifest_path = Path(VITE_MANIFEST)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    commit_mtime = manifest_path.stat().st_mtime_ns
    for row in manifest.values():
        for relative in (row.get('file'), *(row.get('css') or ()), *(row.get('assets') or ())):
            path = manifest_path.parent / relative
            assert path.is_file(), relative
            assert path.stat().st_mtime_ns <= commit_mtime, (
                f'{relative} was published after manifest.json')


def test_release_wrapper_preserves_last_good_graph_and_publishes_manifest_last():
    source = (ROOT / 'scripts/build_frontend.mjs').read_text(encoding='utf-8')
    copy_at = source.index('for (const asset of nextAssets)')
    previous_at = source.index("'previous-manifest.json'")
    manifest_at = source.index("'manifest.json'), nextManifest")
    assert copy_at < previous_at < manifest_at
    assert 'await validateManifest(nextManifest, temporaryDir)' in source
    assert 'new Set([...nextAssets, ...previousAssets]' in source
    assert 'await rm(temporaryDir, { recursive: true, force: true })' in source
