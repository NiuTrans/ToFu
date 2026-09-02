"""Closed-system ownership contracts for the Vite application graph."""

from __future__ import annotations

import re
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding='utf-8')


def test_classic_graph_is_removed_instead_of_served_as_a_second_owner():
    assert not (ROOT / 'static/js').exists()
    assert not (ROOT / 'lib/js_bundler.py').exists()
    assert not (ROOT / 'frontend/src/classic-assets.ts').exists()


def test_python_and_vite_config_declare_the_same_entries():
    from lib.vite_assets import VITE_ENTRIES

    config = _read('vite.config.mjs')
    assert VITE_ENTRIES == {
        'main': 'frontend/src/main.ts',
        'admin': 'frontend/src/admin.ts',
    }
    for name, source in VITE_ENTRIES.items():
        assert f"{name}: resolve(process.cwd(), '{source}')" in config
        assert (ROOT / source).is_file()


def test_every_manifest_row_and_reference_has_one_emitted_residency():
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
    source = _read('frontend/src/main.ts')
    domains = set(re.findall(r"import\('./features/([^']+)'\)", source))
    expected = {
        'settings', 'memory', 'skills', 'paper', 'image', 'project-brain',
        'myday', 'misc', 'orchestration', 'infrastructure', 'background',
        'debug', 'diagnostics',
    }
    assert domains == expected
    for domain in domains:
        flat = ROOT / 'frontend/src/features' / f'{domain}.ts'
        package = ROOT / 'frontend/src/features' / domain / 'index.ts'
        assert flat.is_file() or package.is_file(), domain
    assert "import('./features/legacy')" not in source


def test_feature_registry_is_module_private_and_connected_explicitly():
    registry = _read('frontend/src/feature-registry.ts')
    main = _read('frontend/src/main.ts')
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
    assert "_APP_ASSET_MARKER = '<!-- TOFU_APP_ASSETS -->'" in route
    assert 'html.count(_APP_ASSET_MARKER) != 1' in route
    assert 'get_vite_asset_tags' in route


def test_extracted_trace_stylesheet_is_shipped_after_application_styles():
    html = _read('index.html')
    application_style = 'href="static/styles.css'
    trace_style = 'href="static/request-inspector-trace.css'
    assert application_style in html
    assert trace_style in html
    assert html.index(application_style) < html.index(trace_style)

    trace_css = _read('static/request-inspector-trace.css')
    assert '.ri-trace-entry{' in trace_css
    assert '.tr-flame{' in trace_css


def test_release_retention_is_driven_only_by_valid_manifest_assets():
    source = _read('scripts/build_frontend.mjs')
    assert 'for (const key of Object.keys(manifest)) await visit(key)' in source
    assert 'new Set([...nextAssets, ...previousAssets]' in source
    assert "'previous-manifest.json'" in source
    assert 'feature-' not in source
    assert 'domain-' not in source
