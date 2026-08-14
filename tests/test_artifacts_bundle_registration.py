"""Registration contracts for the single Vite application graph."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.vite_assets import validate_vite_artifact


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'frontend/src/runtime/app-runtime.js'


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding='utf-8')


def test_artifacts_owner_precedes_stream_render_consumers():
    source = RUNTIME.read_text(encoding='utf-8')
    owner = source.index('runtimeScope.Artifacts = {')
    pipeline = source.index('function dispatchSSEEvent(')
    renderer = source.index('function renderToolRoundsHTML(')
    assert owner < pipeline
    assert owner < renderer


def test_shell_has_one_vite_asset_slot_and_no_classic_inventory():
    html = _read('index.html')
    assert html.count('<!-- TOFU_APP_ASSETS -->') == 1
    assert 'static/js/' not in html
    assert 'src="static/js/' not in html


def test_vite_manifest_closes_over_every_emitted_asset():
    manifest = validate_vite_artifact()
    assert manifest['frontend/src/main.ts']['isEntry'] is True
    assert manifest['frontend/src/admin.ts']['isEntry'] is True
    assert len(manifest) > 2


def test_main_registers_runtime_before_lazy_feature_dispatch():
    source = _read('frontend/src/main.ts')
    assert "from './runtime/app-runtime.js'" in source
    runtime_at = source.index("from './runtime/app-runtime.js'")
    feature_at = source.index("import('./features/settings')")
    assert runtime_at < feature_at


def test_sse_and_image_helpers_have_one_runtime_owner():
    source = RUNTIME.read_text(encoding='utf-8')
    for symbol in (
        'dispatchSSEEvent', '_handleToolStart', '_handleSwarmPhase',
        'finishStream', '_openImageFullscreen', '_downloadGenImage',
    ):
        assert source.count(f'function {symbol}(') == 1, symbol


def test_orchestration_transport_and_views_are_explicit_vite_owners():
    core = _read('frontend/src/features/orchestration-core-owners.ts')
    views = _read('frontend/src/features/orchestration-view-owners.ts')
    studio = _read('frontend/src/features/orchestration-studio-view-owners.ts')
    assert "import './orchestration/request-contract'" in core
    assert "import './orchestration/api-request'" in core
    assert "import './orchestration/task-mode'" in views
    assert "import './orchestration/graph'" in studio
    assert "import './orchestration/editor-controller-hub'" in studio


def test_classic_bundler_and_feature_bridge_are_removed():
    assert not (ROOT / 'lib/js_bundler.py').exists()
    assert not (ROOT / 'static/js').exists()
    assert 'feature-bridge.js' not in _read('frontend/src/main.ts')


def test_feature_domains_are_dynamic_imports_not_a_monolithic_bundle():
    source = _read('frontend/src/main.ts')
    for domain in (
        'settings', 'memory', 'skills', 'paper', 'image', 'project-brain',
        'myday', 'orchestration', 'infrastructure',
    ):
        assert f"import('./features/{domain}')" in source
