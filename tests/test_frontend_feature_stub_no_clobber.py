"""Source-level guards for the single-path feature bridge."""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
BRIDGE = Path(runtime_section_path('feature-bridge.js'))


def test_bridge_preserves_preinstalled_owner():
    source = BRIDGE.read_text(encoding='utf-8')
    match = re.search(
        r'function\s+_installFeatureStub\s*\(name\)\s*\{(?P<body>.*?)\n\}',
        source,
        re.DOTALL,
    )
    assert match
    body = match.group('body')
    guard = "typeof runtimeScope[name] === 'function'"
    assert guard in body
    assert body.index(guard) < body.index('runtimeScope[name] = stub')


def test_bridge_has_no_alternate_asset_loader():
    source = BRIDGE.read_text(encoding='utf-8')
    forbidden = (
        '__FEATURE_BUNDLE_SRC__',
        '_loadFeatureBundle',
        'document.createElement',
        'document.head.appendChild',
        'modules-failed',
    )
    assert not [token for token in forbidden if token in source]
