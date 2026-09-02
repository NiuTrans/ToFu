"""The main application has one required Vite API transport owner."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_retained_registry_imports_one_required_transport_owner():
    registry = runtime_section('api.js')
    main = (ROOT / 'frontend/src/main.ts').read_text(encoding='utf-8')
    runtime = (ROOT / 'frontend/src/runtime/app-runtime.js').read_text(
        encoding='utf-8')
    transport = (ROOT / 'frontend/src/api/transport.ts').read_text(encoding='utf-8')

    assert "apiTransport as requiredApiTransport" in runtime
    assert 'const _transportOwner = requiredApiTransport;' in registry
    assert 'return _transportOwner.request(path, opts || {});' in registry
    assert 'apiTransport' not in main
    assert 'window.TofuModules = Object.freeze({' in main
    assert 'export const apiTransport' in transport
    assert 'resolvePath,' in transport
    assert "const folders = {" in registry
    assert "const artifacts = {" in registry


def test_server_and_vite_tags_have_no_runtime_transport_fallback():
    from lib.vite_assets import VITE_MANIFEST, _dev_tags, _manifest_tags

    route_source = (ROOT / 'routes/common.py').read_text(encoding='utf-8')
    assert '_api_transport_bootstrap' not in route_source
    assert '__TOFU_API_FALLBACK' not in route_source

    dev_tags = _dev_tags('http://127.0.0.1:5173', 'main')
    with open(VITE_MANIFEST, encoding='utf-8') as handle:
        prod_tags = _manifest_tags(json.load(handle))
    for tags in (dev_tags, prod_tags):
        assert 'type="module"' in tags
        assert 'modules-failed' not in tags
        assert '__TOFU_VITE_FAILED__' not in tags
        assert 'onerror=' not in tags


def test_registry_has_no_transport_rollback_or_dom_injection():
    registry = runtime_section('api.js')
    assert 'transport-vite-adapter.js' not in registry
    assert 'document.createElement' not in registry
    assert '@standalone-transport' not in registry
    assert 'function _nativeTransport' not in registry
    assert 'class ApiError' not in registry
    assert 'await fetch(' not in registry
    assert 'TofuModules' not in registry
    assert not (ROOT / 'static/js/api/transport-vite-adapter.js').exists()
