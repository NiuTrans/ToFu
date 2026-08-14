"""Ownership and ordering guards for My Day in the Vite runtime."""

from __future__ import annotations

import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section, runtime_section_names

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
MAIN_ENTRY = ROOT / 'frontend/src/main.ts'
MYDAY_ENTRY = ROOT / 'frontend/src/features/myday.ts'
ENTRY = 'myday.js'
ENTRY_TASKS = 'myday_tasks.js'
STUBS = ('openDailyReport', 'closeDailyReport', '_mydayTriggerGenerate')


def test_myday_in_deferred_files():
    names = runtime_section_names()
    assert names.count(ENTRY) == 1
    assert MYDAY_ENTRY.is_file()


def test_myday_not_in_core_bundle_files():
    entry = MAIN_ENTRY.read_text()
    assert "import('./features/myday')" in entry
    assert 'lib/js_bundler.py' not in entry


def test_myday_tasks_moves_with_myday():
    names = runtime_section_names()
    assert names.count(ENTRY_TASKS) == 1
    assert names.index(ENTRY) < names.index(ENTRY_TASKS)


def test_entry_points_in_py_table():
    entry = MAIN_ENTRY.read_text()
    for name in STUBS:
        assert re.search(rf"['\"]{name}['\"]", entry)


def test_entry_points_in_js_table():
    owner = MYDAY_ENTRY.read_text()
    assert "invokeFeatureEntry('myday'" in owner


def test_myday_state_private_to_the_two_modules():
    users = {name for name in runtime_section_names()
             if re.search(r'\b_myday\b', runtime_section(name))}
    assert users == {ENTRY, ENTRY_TASKS}


def test_boot_side_effects_are_late_load_safe():
    src = runtime_section(ENTRY)
    assert re.search(
        r"document\.readyState === 'loading'\)\s*\{\s*document\.addEventListener\('DOMContentLoaded', _mydayScheduleReminder\)",
        src)
    assert re.search(
        r"document\.readyState === 'loading'\)\s*\{\s*document\.addEventListener\('DOMContentLoaded', _mydayBootDayDigestSoon\)",
        src)


def test_index_has_no_raw_myday_scripts():
    html = INDEX_HTML.read_text()
    assert 'static/js/myday.js' not in html
    assert 'static/js/myday_tasks.js' not in html
    assert '<!-- TOFU_APP_ASSETS -->' in html


def test_feature_bridge_exclusively_owns_opener_stub():
    html = INDEX_HTML.read_text()
    assert 'LoadGuard' not in html
    assert "'openDailyReport'" in MAIN_ENTRY.read_text()
