"""Release installs consume a verified frontend graph without Node/npm."""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _installer() -> str:
    return (ROOT / 'install.sh').read_text(encoding='utf-8')


def _frontend_block() -> str:
    source = _installer()
    start = source.index('Step 8.4: Prebuilt frontend')
    end = source.index('Step 8.5:', start)
    return source[start:end]


def test_release_installer_does_not_install_node_or_npm_packages():
    source = _installer()
    assert 'Installing Node.js + esbuild' not in source
    assert 'npm install' not in source
    assert 'npm config' not in source
    assert '--skip-node           Legacy no-op' in source


def test_frontend_download_is_versioned_and_checksum_verified():
    block = _frontend_block()
    assert 'frontend-dist-${_FRONTEND_VERSION}.tar.gz' in block
    assert 'releases/download/v${_FRONTEND_VERSION}' in block
    assert 'sha256sum -c' in block
    assert 'shasum -a 256 -c' in block
    assert 'curl -fL --retry 3 --connect-timeout 15' in block
    assert 'wget --tries=3 --timeout=30' in block


def test_frontend_is_extracted_and_validated_with_python_only():
    block = _frontend_block()
    assert 'tar xzf "$_FRONTEND_ARCHIVE" -C "$INSTALL_DIR"' in block
    assert 'scripts/verify_frontend_dist.py' in block
    assert 'Node/npm not required' in block
    assert 'command -v node' not in block
    assert 'npm install' not in block
