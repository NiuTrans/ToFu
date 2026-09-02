"""Atomic publication contracts for concurrent Vite builds."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from lib.vite_assets import VITE_MANIFEST, validate_vite_artifact


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'scripts/build_frontend.mjs'


def _source() -> str:
    return BUILD.read_text(encoding='utf-8')


def test_build_uses_a_private_staging_directory_and_always_cleans_it():
    source = _source()
    assert "mkdtemp(join(dirname(liveDir), '.vite-build-'))" in source
    assert 'process.env.TOFU_VITE_OUT_DIR = temporaryDir' in source
    assert 'await rm(temporaryDir, { recursive: true, force: true })' in source


def test_each_content_hashed_asset_is_published_by_atomic_rename():
    source = _source()
    assert 'async function atomicCopy(source, destination)' in source
    assert "const temporary = `${destination}.publish-${process.pid}-" in source
    assert 'await copyFile(source, temporary)' in source
    assert 'await rename(temporary, destination)' in source


def test_existing_hash_short_circuits_without_overwrite():
    source = _source()
    start = source.index('async function atomicCopy(')
    end = source.index('async function publishManifest(', start)
    block = source[start:end]
    assert "await open(destination, 'r')" in block
    assert 'return;' in block
    assert block.index("await open(destination, 'r')") < block.index('copyFile(')


def test_manifest_is_the_commit_point_after_assets_and_previous_graph():
    source = _source()
    assets_at = source.index('for (const asset of nextAssets)')
    previous_at = source.index("'previous-manifest.json'")
    manifest_at = source.index("'manifest.json'), nextManifest")
    cleanup_at = source.index('const retained = new Set(')
    assert assets_at < previous_at < manifest_at < cleanup_at


def test_live_and_previous_manifests_reference_present_files():
    current = validate_vite_artifact()
    assert current
    previous_path = Path(VITE_MANIFEST).with_name('previous-manifest.json')
    if previous_path.exists():
        previous = json.loads(previous_path.read_text(encoding='utf-8'))
        for row in previous.values():
            for relative in (row.get('file'), *(row.get('css') or ()),
                             *(row.get('assets') or ())):
                assert (previous_path.parent / relative).is_file(), relative


def test_cleanup_retains_the_current_and_previous_graphs():
    source = _source()
    assert 'new Set([...nextAssets, ...previousAssets]' in source
    assert "if (!retained.has(file)) await rm(" in source


def test_server_import_and_request_paths_never_build_frontend_code():
    import server

    source = inspect.getsource(server._check_frontend_artifact)
    assert 'validate_published_vite_artifact' in source
    assert 'subprocess' not in source
    # The actionable validation error names ``npm run build:frontend``. Guard
    # behavior rather than banning that user-facing word from the source.
    assert 'scripts/build_frontend.mjs' not in source
