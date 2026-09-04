"""Demand-loading contract for the self-update dialog and ambient check."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
GENERAL_PANEL = ROOT / 'static/settings_panels/general.html'
MAIN = ROOT / 'frontend/src/main.ts'
MANIFEST = ROOT / 'frontend/src/runtime/sections/manifest.json'
UTILITY_FEATURE = ROOT / 'frontend/src/features/utility-panels.ts'


def _manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def test_update_has_one_lazy_utility_owner():
    manifest = _manifest()
    main = [row['source'] for row in manifest['sections']]
    utility = next(
        bundle for bundle in manifest['lazyBundles']
        if bundle['name'] == 'utility-panels'
    )
    assert 'update.js' not in main
    assert [row['source'] for row in utility['sections']].count('update.js') == 1


def test_update_entries_route_to_generated_utility_runtime():
    main = MAIN.read_text(encoding='utf-8')
    match = re.search(
        r'const utilityPanelEntries = new Set\(\[(.*?)\]\);', main, re.S,
    )
    assert match
    for name in ('openUpdateDialog', 'closeUpdateModal',
                 '_renderSettingsUpdatePill'):
        assert f"'{name}'" in match.group(1)
    feature = UTILITY_FEATURE.read_text(encoding='utf-8')
    assert "import '../runtime/utility-panels-runtime.generated.js';" in feature
    bridge = runtime_section('feature-bridge.js')
    for name in ('openUpdateDialog', 'closeUpdateModal',
                 '_renderSettingsUpdatePill'):
        assert f"'{name}'" in bridge


def test_static_update_controls_use_declarative_actions():
    html = INDEX_HTML.read_text(encoding='utf-8')
    panel = GENERAL_PANEL.read_text(encoding='utf-8')
    assert 'data-tofu-action="openUpdateDialog()"' in html
    assert 'data-tofu-action="closeUpdateModal()"' in html
    assert 'data-tofu-action="openUpdateDialog()"' in panel
    assert 'onclick="openUpdateDialog()"' not in html + panel


def test_boot_check_and_idle_preload_preserve_ambient_badge():
    update = runtime_section('update.js')
    assert re.search(
        r'_onReady\(function \(\) \{\s*setTimeout\(_updateBootCheck, 3000\)',
        update,
    )
    assert "window.addEventListener('load'" not in update
    main = MAIN.read_text(encoding='utf-8')
    assert "prepareFeature('openUpdateDialog')" in main
    assert 'idleCallback(preloadIdleFeatures' in main


def test_settings_reads_update_pill_as_an_optional_live_port():
    core = runtime_section('settings/core_panel.js')
    assert "typeof runtimeScope._renderSettingsUpdatePill === 'function'" in core
    assert 'runtimeScope._renderSettingsUpdatePill();' in core
    settings = next(
        bundle for bundle in _manifest()['lazyBundles']
        if bundle['name'] == 'settings-presenters'
    )
    services = {row['name'] for row in settings['runtimeServices']}
    assert '_renderSettingsUpdatePill' not in services
