"""Freshness and publication contracts for the prebuilt Vite graph."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lib.vite_assets import VITE_ENTRIES, VITE_MANIFEST, validate_vite_artifact


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


def test_manifest_is_at_least_as_fresh_as_every_frontend_input():
    manifest_mtime = Path(VITE_MANIFEST).stat().st_mtime_ns
    newest_mtime, newest_path = max(
        (path.stat().st_mtime_ns, path) for path in _frontend_inputs()
    )
    assert manifest_mtime >= newest_mtime, (
        f'Vite manifest is stale: {newest_path.relative_to(ROOT)} is newer; '
        'run `npm run build` before publishing')


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
