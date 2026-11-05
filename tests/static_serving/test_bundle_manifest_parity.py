"""Closed-system ownership contracts for the Vite application graph."""

from __future__ import annotations

import re
import json
from pathlib import Path

import pytest

from tests._jsdom import require_shipped_graph


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding='utf-8')


def test_classic_graph_is_removed_instead_of_served_as_a_second_owner():
    assert not (ROOT / 'static/js').exists()
    assert not (ROOT / 'lib/js_bundler.py').exists()
    assert not (ROOT / 'frontend/src/classic-assets.ts').exists()


def test_declared_frontend_entries_match_the_authoring_contract():
    from lib.vite_assets import VITE_ENTRIES

    contract = json.loads((ROOT / 'frontend/authoring-inputs.json').read_text())
    assert VITE_ENTRIES == contract['entries']
    assert set(VITE_ENTRIES) == {'main', 'admin', 'plugin'}
    for source in VITE_ENTRIES.values():
        assert (ROOT / source).is_file()


def test_every_manifest_row_and_reference_has_one_emitted_residency():
    require_shipped_graph()
    from lib.vite_assets import VITE_OUT_DIR, validate_vite_artifact

    manifest = validate_vite_artifact()
    emitted = Path(VITE_OUT_DIR)
    referenced = set()
    for key, row in manifest.items():
        assert (emitted / row['file']).is_file(), key
        for field in ('css', 'assets'):
            for value in row.get(field) or ():
                assert (emitted / value).is_file(), (key, field, value)
        for field in ('imports', 'dynamicImports'):
            for value in row.get(field) or ():
                assert value in manifest, (key, field, value)
                referenced.add(value)
    assert referenced, 'the application graph unexpectedly collapsed to no shared/dynamic chunks'


def test_main_routes_every_declared_domain_to_an_explicit_esm_owner():
    source = _read('frontend/src/application.ts')
    domains = set(re.findall(r"import\('./features/([^']+)'\)", source))
    expected = {
        'settings', 'memory', 'skills', 'paper', 'image', 'project-brain',
        'myday', 'misc', 'orchestration', 'infrastructure', 'background',
        'diagnostics', 'knowledge', 'utility-panels', 'project',
        'local-control', 'diagnostics-presenters', 'compaction-viewer',
        'paper-preview', 'evolution', 'slides',
    }
    assert domains == expected
    for domain in domains:
        flat = ROOT / 'frontend/src/features' / f'{domain}.ts'
        package = ROOT / 'frontend/src/features' / domain / 'index.ts'
        assert flat.is_file() or package.is_file(), domain
    assert "import('./features/legacy')" not in source


def test_feature_registry_is_module_private_and_connected_explicitly():
    registry = _read('frontend/src/feature-registry.ts')
    main = _read('frontend/src/application.ts')
    assert 'new Proxy(overrides' in registry
    assert 'connectFeatureRuntime(' in registry
    assert 'connectFeatureRuntime(' in main
    assert "(name: string) => name === 't' ? t : getRuntimeService(name)" in main
    assert 'setRuntimeService,' in main
    assert 'window.featureRegistry' not in registry
    assert 'Object.assign(window' not in registry


def test_index_has_one_server_owned_module_asset_slot():
    html = _read('index.html')
    assert html.count('<!-- TOFU_APP_ASSETS -->') == 1
    assert not re.search(r'<script[^>]+src="static/js/', html)
    route = _read('routes/common.py')
    assert '_APP_ASSET_MARKER = "<!-- TOFU_APP_ASSETS -->"' in route
    assert 'html.count(_APP_ASSET_MARKER) != 1' in route
    assert 'get_vite_asset_tags' in route
