"""Ownership and ordering guards for My Day in the Vite runtime."""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section, runtime_section_names

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX_HTML = ROOT / 'index.html'
MAIN_ENTRY = ROOT / 'frontend/src/application.ts'
MYDAY_ENTRY = ROOT / 'frontend/src/features/myday.ts'
BACKGROUND_ENTRY = ROOT / 'frontend/src/features/background.ts'
MANIFEST = ROOT / 'frontend/src/runtime/sections/manifest.json'
GENERATED = ROOT / 'frontend/src/runtime/myday-presenters.generated.js'
APP_RUNTIME = ROOT / 'frontend/src/runtime/app-runtime.js'
EPILOGUE = ROOT / 'frontend/src/runtime/sections/_epilogue.js'
ENTRY = 'myday.js'
ENTRY_TASKS = 'myday_tasks.js'
GOAL_BOARD_OWNER = ROOT / 'frontend/src/features/myday/goal-board.ts'
REPORT_CACHE_OWNER = ROOT / 'frontend/src/features/myday/report-cache.ts'
BACKGROUND_OWNER = ROOT / 'frontend/src/features/myday/background-controller.ts'
PRESENTATION_ASSETS = ROOT / 'frontend/src/features/myday/presentation-assets.ts'
STUBS = ('openDailyReport', 'closeDailyReport')


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
    assert services == {'Api'}


def test_static_assets_have_one_typed_lazy_owner():
    imports = {
        row['source']: set(row['bindings'])
        for row in _presenter_bundle()['moduleImports']
    }
    assert 'frontend/src/features/myday/presentation-assets.ts' not in imports
    presenter = runtime_section(ENTRY)
    assert 'class="myday-empty-tofu"' not in presenter
    assert 'MYDAY_PRESENTATION_ASSETS' not in presenter
    board = GOAL_BOARD_OWNER.read_text(encoding='utf-8')
    assert "from './presentation-assets'" in board
    assets = PRESENTATION_ASSETS.read_text(encoding='utf-8')
    assert assets.count('class="myday-empty-tofu"') == 1


def test_myday_main_column_has_a_typed_lazy_owner():
    names = runtime_section_names()
    assert ENTRY_TASKS not in names
    assert GOAL_BOARD_OWNER.is_file()
    entry = MYDAY_ENTRY.read_text()
    assert "from './myday/goal-board'" in entry
    assert 'MyDayGoalBoard' in entry
    presenter = runtime_section(ENTRY)
    assert 'runtimeScope.MyDayGoalBoard' in presenter
    for retired in ('_mydayTriggerGenerate', '_mydayStartPolling', '_mydayRenderTasks',
                    'MyDayTaskPresentation'):
        assert retired not in presenter


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
    assert re.search(r'closeDailyReport:\s*\(\)\s*=>\s*runtimeScope\.closeDailyReport\(\)', main)
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
