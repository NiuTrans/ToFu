"""Shipped standalone dependencies have exact identities, not size heuristics."""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from lib.http_transport import frontend_vendor

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_shipped_vendor_graph_matches_its_generated_manifest():
    assert frontend_vendor.vendor_asset_mismatches(ROOT) == []


def test_verifier_observes_same_size_corruption_and_missing_assets(tmp_path, monkeypatch):
    path = tmp_path / 'asset.js'
    path.write_bytes(b'correct')
    monkeypatch.setattr(frontend_vendor, 'KATEX_STATIC_SHA256', {
        'asset.js': sha256(b'correct').hexdigest(),
    })
    assert frontend_vendor.vendor_asset_mismatches(tmp_path) == []
    path.write_bytes(b'corrupt')
    assert frontend_vendor.vendor_asset_mismatches(tmp_path) == [
        'asset.js: differs from locked dependency bytes',
    ]
    path.unlink()
    assert frontend_vendor.vendor_asset_mismatches(tmp_path)[0].startswith('asset.js:')
