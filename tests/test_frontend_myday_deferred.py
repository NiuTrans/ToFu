"""Ownership and ordering guards for My Day in the Vite runtime."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section, runtime_section_names

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
MAIN_ENTRY = ROOT / 'frontend/src/main.ts'
MYDAY_ENTRY = ROOT / 'frontend/src/features/myday.ts'
BACKGROUND_ENTRY = ROOT / 'frontend/src/features/background.ts'
MANIFEST = ROOT / 'frontend/src/runtime/sections/manifest.json'
GENERATED = ROOT / 'frontend/src/runtime/myday-presenters.generated.js'
APP_RUNTIME = ROOT / 'frontend/src/runtime/app-runtime.js'
EPILOGUE = ROOT / 'frontend/src/runtime/sections/_epilogue.js'
ENTRY = 'myday.js'
ENTRY_TASKS = 'myday_tasks.js'
TASK_OWNER = ROOT / 'frontend/src/features/myday/task-actions.ts'
REPORT_CACHE_OWNER = ROOT / 'frontend/src/features/myday/report-cache.ts'
BACKGROUND_OWNER = ROOT / 'frontend/src/features/myday/background-controller.ts'
PRESENTATION_ASSETS = ROOT / 'frontend/src/features/myday/presentation-assets.ts'
STUBS = ('openDailyReport', 'closeDailyReport', '_mydayTriggerGenerate')


def _manifest():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def _presenter_bundle():
    return next(
        bundle for bundle in _manifest()['lazyBundles']
        if bundle['name'] == 'myday-presenters'
    )


def test_myday_presenter_has_one_demand_loaded_owner():
    manifest = _manifest()
    main_sources = {row['source'] for row in manifest['sections']}
    sources = [row['source'] for row in _presenter_bundle()['sections']]
    assert sources == [ENTRY]
    assert ENTRY not in main_sources
    assert sum(
        ENTRY == row['source']
        for bundle in manifest['lazyBundles']
        for row in bundle['sections']
    ) == 1

    marker = f'/* ===== migrated source: {ENTRY} ===== */'
    assert marker in GENERATED.read_text(encoding='utf-8')
    assert marker not in APP_RUNTIME.read_text(encoding='utf-8')
    assert "import '../runtime/myday-presenters.generated.js';" in (
        MYDAY_ENTRY.read_text(encoding='utf-8')
    )


def test_myday_not_in_core_bundle_files():
    entry = MAIN_ENTRY.read_text()
    assert "import('./features/myday')" in entry
    assert 'lib/js_bundler.py' not in entry


def test_presenter_dependencies_are_explicit_retained_services():
    services = {row['name'] for row in _presenter_bundle()['runtimeServices']}
    expected = {
        'Api', '_applyBrowserUI', '_applyCodeExecUI', '_applyFetchEnabledUI',
        '_applySearchModeUI', 'newChat', 'updateSendButton',
    }
    assert services == expected
    epilogue = EPILOGUE.read_text(encoding='utf-8')
    for name in expected - {'Api'}:
        assert re.search(rf'^  {re.escape(name)},$', epilogue, re.M)


def test_static_empty_asset_has_one_typed_lazy_owner():
    imports = {
        row['source']: set(row['bindings'])
        for row in _presenter_bundle()['moduleImports']
    }
    assert imports['frontend/src/features/myday/presentation-assets.ts'] == {
        'MYDAY_PRESENTATION_ASSETS',
    }
    presenter = runtime_section(ENTRY)
    assert '_STATUS_ICONS' not in presenter
    assert 'class="myday-empty-tofu"' not in presenter
    assert presenter.count('${MYDAY_PRESENTATION_ASSETS.emptyIllustration}') == 3
    for local_name in ('checkSvg', 'delSvg', 'launchSvg', 'dashCircle'):
        assert f'const {local_name}' not in presenter
    assets = PRESENTATION_ASSETS.read_text(encoding='utf-8')
    assert assets.count('class="myday-empty-tofu"') == 1


def test_myday_task_mutations_have_a_typed_lazy_owner():
    names = runtime_section_names()
    assert ENTRY_TASKS not in names
    assert TASK_OWNER.is_file()
    assert "from './myday/task-actions'" in MYDAY_ENTRY.read_text()


def test_entry_points_in_py_table():
    entry = MAIN_ENTRY.read_text()
    for name in STUBS:
        assert re.search(rf"['\"]{name}['\"]", entry)


def test_entry_points_in_js_table():
    owner = MYDAY_ENTRY.read_text()
    assert "invokeFeatureEntry('myday'" in owner


def test_myday_state_stays_private_to_the_retained_panel():
    users = {name for name in runtime_section_names()
             if re.search(r'\b_myday\b', runtime_section(name))}
    assert users == {ENTRY}


def test_boot_side_effects_are_typed_and_idle_loaded():
    src = runtime_section(ENTRY)
    assert '_mydayScheduleReminder' not in src
    assert '_mydayBootDayDigest' not in src
    assert '_mydayIDB' not in src
    assert REPORT_CACHE_OWNER.is_file()
    assert BACKGROUND_OWNER.is_file()
    background = BACKGROUND_ENTRY.read_text()
    assert "from './myday/background-controller'" in background
    assert 'await prepareMyDayBackground()' in background
    assert 'myday-presenters.generated.js' not in background


def test_retained_escape_handler_uses_the_late_feature_port():
    main = runtime_section('main.js')
    assert 'runtimeScope.closeDailyReport();' in main
    assert re.search(r'(?<![\w.])closeDailyReport\(\);', main) is None


def test_index_has_no_raw_myday_scripts():
    html = INDEX_HTML.read_text()
    assert 'static/js/myday.js' not in html
    assert 'static/js/myday_tasks.js' not in html
    assert '<!-- TOFU_APP_ASSETS -->' in html


def test_feature_bridge_exclusively_owns_opener_stub():
    html = INDEX_HTML.read_text()
    assert 'LoadGuard' not in html
    assert "'openDailyReport'" in MAIN_ENTRY.read_text()
