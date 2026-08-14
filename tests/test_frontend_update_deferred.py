"""Guards for Epic-E pt_3879f00e sub-9A — defer update.js (48.7KB).

Census (2026-08-01): the update dialog module has exactly ONE external JS
caller — settings/core_panel.js:244 `_renderSettingsUpdatePill`, already
typeof-guarded — plus two static-HTML openers (index.html #updateBtn:262,
settings_panels/general.html #settingsUpdateBtn:205) and a static overlay
closer (index.html:1363 `closeUpdateModal`). Its boot behaviour is
SELF-initiated (`setTimeout(_updateBootCheck, 3000)` at module load), so
deferral only delays the "New" badge ~2s (idle prefetch) — the accepted
sub-3B degradation class. No other boot wiring, no window exposes.

Shared accelerant (lands in this slice): openSettings() fire-and-forgets
_loadFeatureBundle(), so opening Settings shrinks the absent-module window
for every deferred settings module (steady-state zero added network cost —
the idle prefetch would fetch the same bundle anyway).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-bridge.js'
CORE_PANEL = ROOT / 'static' / 'js' / 'settings' / 'core_panel.js'
UPDATE_JS = ROOT / 'static' / 'js' / 'update.js'

UPDATE_STUBS = ('openUpdateDialog', 'closeUpdateModal')


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest move
# ---------------------------------------------------------------------------
def test_update_js_deferred_not_core():
    bundle, deferred, _ep, _crit = _manifest()
    assert 'update.js' in deferred, (
        "update.js (48.7KB settings dialog) must be in _CLASSIC_ASSET_FILES")
    assert 'update.js' not in bundle, (
        "update.js must NOT remain in _BUNDLE_FILES — double-load would "
        "duplicate _updateState and re-arm the boot check")


# ---------------------------------------------------------------------------
# 2. entry-point stubs (py + js dual tables)
# ---------------------------------------------------------------------------
def test_update_stubs_in_py_table():
    _bf, _df, entry_points, _crit = _manifest()
    missing = [s for s in UPDATE_STUBS if s not in entry_points]
    assert not missing, (
        f'_FEATURE_ENTRY_POINTS is missing update stubs: {missing}')


def test_update_stubs_in_loader_table():
    loader = FEATURE_LOADER.read_text()
    missing = [s for s in UPDATE_STUBS if f"'{s}'" not in loader]
    assert not missing, (
        f'feature-bridge.js is missing update stubs: {missing}')


# ---------------------------------------------------------------------------
# 3. the openers the stubs cover stay wired (static HTML pins)
# ---------------------------------------------------------------------------
def test_static_openers_exist():
    html = INDEX_HTML.read_text()
    assert 'onclick="openUpdateDialog()"' in html, (
        'index.html #updateBtn lost its openUpdateDialog onclick — the '
        'stubbed entry point would have no trigger')
    assert 'closeUpdateModal()' in html, (
        'index.html #updateModal overlay lost its closeUpdateModal onclick')


# ---------------------------------------------------------------------------
# 4. shared accelerant (shipped shape): openSettings() calls
#    _renderSettingsUpdatePill typeof-gated — the gate PASSES on the
#    feature-loader stub, which loads the bundle (gate+stub composition).
#    Opening Settings therefore warms the feature bundle for every
#    deferred settings module without an explicit prefetch call.
# ---------------------------------------------------------------------------
def test_settings_pill_stubbed_as_warmer():
    _bf, _df, entry_points, _crit = _manifest()
    assert '_renderSettingsUpdatePill' in entry_points, (
        '_renderSettingsUpdatePill must be a feature-loader entry point — '
        'the gated openSettings() call passes on the stub and warms the '
        'bundle for the whole settings family')
    loader = FEATURE_LOADER.read_text()
    assert "'_renderSettingsUpdatePill'" in loader


# ---------------------------------------------------------------------------
# 5. deferral-safe self-init stays inside the module
# ---------------------------------------------------------------------------
def test_boot_check_rides_onready_not_window_load():
    src = UPDATE_JS.read_text()
    assert re.search(
        r"_onReady\(function \(\) \{\s*setTimeout\(_updateBootCheck,", src), (
        'update.js must self-init via _onReady — a deferred module lands '
        'AFTER window \'load\' fired, so a load listener would never run')


def test_core_panel_caller_stays_gated():
    src = CORE_PANEL.read_text()
    assert re.search(
        r"typeof\s+_renderSettingsUpdatePill\s*===\s*'function'", src), (
        'settings/core_panel.js must keep _renderSettingsUpdatePill '
        'typeof-gated (module is absent until the feature bundle lands)')
