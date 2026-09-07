"""Ownership guards for the Memory domain after the Vite migration."""

from __future__ import annotations

import pathlib
import re

import pytest

from tests._runtime_sections import runtime_section, runtime_section_names

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
MAIN_ENTRY = ROOT / 'frontend/src/main.ts'
MEMORY_ENTRY = ROOT / 'frontend/src/features/memory.ts'
MEMORY_PANEL = ROOT / 'frontend/src/features/memory/panel.ts'
MEMORY_PREFS = ROOT / 'frontend/src/features/memory/preferences.ts'

MEMORY_STUBS = (
    'toggleMemory', 'openMemoryModal', 'toggleMemoryAddForm',
    'closeMemoryModal', 'toggleMemoryFromModal',
    'refreshPreferences', 'savePreferences',
)


def test_esc_removed_from_memory():
    src = MEMORY_PANEL.read_text() + MEMORY_PREFS.read_text()
    assert not re.search(r'(?m)^function _esc\(', src)


def test_classic_html_safety_sections_are_retired():
    names = runtime_section_names()
    assert 'core/escape_html.js' not in names
    assert 'core/safe_html.js' not in names


def test_loadguard_drops_toggle_memory():
    html = INDEX_HTML.read_text()
    assert 'LoadGuard' not in html
    assert not re.search(r'var stubs = \[(.*?)\];', html, re.S)


def test_main_js_modal_btn_call_gated():
    src = runtime_section('main.js')
    assert re.search(r"typeof\s+_updateMemoryModalBtn\s*===\s*'function'", src)


def test_memory_stubs_in_py_table():
    entry = MAIN_ENTRY.read_text()
    missing = [name for name in MEMORY_STUBS
               if not re.search(rf"['\"]{name}['\"]", entry)]
    assert not missing, f'Vite action registry is missing memory entries: {missing}'
    assert "import('./features/memory')" in entry


def test_memory_stubs_in_loader_table():
    entry = MEMORY_ENTRY.read_text()
    assert "import './memory/panel'" in entry
    assert "import './memory/preferences'" in entry
    assert "invokeFeatureEntry('memory'" in entry
