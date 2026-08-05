"""Guards for pt_3879f00e sub-part 9 — defer the six settings-panel
modules (update.js 48.7KB / skills.js 21.5KB / memory.js 16.9KB /
optimizer.js 13.9KB / timer.js 13.6KB / preferences.js 8.1KB ≈ 123KB)
out of the render-blocking core.

Census (2026-08-01, grep-verified):
  * Every panel is user-triggered (topbar badge / settings tab / mobile
    sheet); ZERO boot-path bare calls. settings/core_panel.js already
    typeof-gates _populateSkillsTab / _populatePreferencesTab /
    _renderSettingsUpdatePill — with a feature-loader stub installed the
    gate passes and the stub LOADS the bundle instead of silently
    skipping (gate+stub composition, sub-9 pattern).
  * BOOT WIRING (deferral hazards, fixed in the same slice):
      - update.js ran its version check off `window 'load'` — a deferred
        module lands AFTER load fired, so the listener would never run.
        Converted to _onReady (feature-loader.js, core).
      - timer.js `_startTimerPolling()` + optimizer.js
        `_startOptimizerPolling()` / badge-bind IIFE run top-level — they
        self-arm whenever the feature bundle lands (idle prefetch ~2s),
        matching the myday precedent; badge binds branch on readyState.
  * mobile_panels.js wraps window.toggleTimerPanel / toggleOptimizerPanel
    at ITS load — capturing the STUB once the two modules defer, and the
    real function then CLOBBERs the wrapper when the bundle lands
    (mobile bottom-sheet behaviour lost). Fix: the wrap is re-runnable +
    identity-tracked and re-runs on 'tofu:feature-bundle-loaded'
    (dispatched by feature-loader.js); a pre-land mobile open kicks the
    load and fills the sheet on resolve.
  * skills_install.js (stays core) called _populateSkillsTab BARE after
    a zip install — typeof-gated here (install succeeds regardless).

11 feature-loader stubs (py + js dual tables): openUpdateDialog,
toggleTimerPanel, toggleOptimizerPanel, toggleMemory, openMemoryModal,
closeMemoryModal, toggleMemoryAddForm, toggleMemoryFromModal,
_populateSkillsTab, _populatePreferencesTab, _renderSettingsUpdatePill.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-loader.js'
UPDATE_JS = ROOT / 'static' / 'js' / 'update.js'
SKILLS_INSTALL = ROOT / 'static' / 'js' / 'skills_install.js'
MOBILE_PANELS = ROOT / 'static' / 'js' / 'mobile_panels.js'

PANEL_FILES = ('update.js', 'skills.js', 'memory.js',
               'optimizer.js', 'timer.js', 'preferences.js')
PANEL_STUBS = (
    'openUpdateDialog', 'toggleTimerPanel', 'toggleOptimizerPanel',
    'toggleMemory', 'openMemoryModal', 'closeMemoryModal',
    'toggleMemoryAddForm', 'toggleMemoryFromModal',
    '_populateSkillsTab', '_populatePreferencesTab',
    '_renderSettingsUpdatePill',
)
# Internal render/refresh functions must stay OUT of the entry-point
# table — they are dispatched at runtime by their own (now-deferred)
# modules or by typeof-gated callers, never raw-clicked pre-land.
NOT_STUBBED = ('_refreshTimerPanel', '_refreshOptimizerPanel',
               '_renderUpdateBadge', '_prefsRender', '_skillsRender')


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest (failing-first drivers)
# ---------------------------------------------------------------------------
def test_six_files_in_deferred():
    _bf, deferred, _ep, _crit = _manifest()
    missing = [f for f in PANEL_FILES if f not in deferred]
    assert not missing, (
        f'{missing} must move to _DEFERRED_FILES — ~123KB of '
        f'user-triggered settings panels out of the render-blocking core')


def test_six_files_out_of_core():
    bundle, _df, _ep, _crit = _manifest()
    present = [f for f in PANEL_FILES if f in bundle]
    assert not present, (
        f'{present} must NOT remain in _BUNDLE_FILES — double-loading '
        f'would duplicate singleton state (panel-open flags, poll timers)')


# ---------------------------------------------------------------------------
# 2. entry-point stubs, py + js dual tables
# ---------------------------------------------------------------------------
def test_panel_stubs_in_py_table():
    _bf, _df, entry_points, _crit = _manifest()
    missing = [s for s in PANEL_STUBS if s not in entry_points]
    assert not missing, (
        f'_DEFERRED_ENTRY_POINTS is missing panel stubs: {missing}')


def test_panel_stubs_in_loader_table():
    loader = FEATURE_LOADER.read_text()
    missing = [s for s in PANEL_STUBS if f"'{s}'" not in loader]
    assert not missing, (
        f'feature-loader.js is missing panel stubs: {missing}')


def test_internals_not_stubbed():
    _bf, _df, entry_points, _crit = _manifest()
    bad = [s for s in NOT_STUBBED if s in entry_points]
    assert not bad, (
        f'{bad} must NOT be entry points — internal render/refresh '
        f'functions are dispatched post-land, not raw-clicked pre-land')


# ---------------------------------------------------------------------------
# 3. boot-safety edits (deferral hazards fixed in the same slice)
# ---------------------------------------------------------------------------
def test_update_boot_check_rides_onready():
    src = UPDATE_JS.read_text()
    assert re.search(r"_onReady\(function \(\) \{\s*setTimeout\(_updateBootCheck, 3000\)", src), (
        'update.js boot check must ride _onReady — a deferred module '
        'lands AFTER window load, so a load listener would never fire')
    assert "window.addEventListener('load'" not in src, (
        'the window-load listener must go — silent no-op post-deferral')


def test_skills_install_populate_gated():
    src = SKILLS_INSTALL.read_text()
    assert re.search(r"typeof _populateSkillsTab === 'function'\) await _populateSkillsTab\(\)", src), (
        'skills_install.js must typeof-gate _populateSkillsTab — '
        'skills.js is deferred; the zip install must never ReferenceError')


# ---------------------------------------------------------------------------
# 4. mobile_panels re-wrap mechanics (deferred-module clobber survival)
# ---------------------------------------------------------------------------
def test_mobile_wrap_re_runnable_and_tracked():
    src = MOBILE_PANELS.read_text()
    assert '_wrapPanelToggles' in src, (
        'mobile_panels must factor the toggle wraps into a re-runnable fn')
    assert 'tofu:feature-bundle-loaded' in src, (
        'mobile_panels must re-wrap on feature-bundle land — the real '
        'toggle clobbers the wrapper installed over the stub')
    assert '_capturedImpl' in src and '_installedWrap' in src, (
        'the wrap must be identity-tracked (never double-wrap, never '
        'capture its own wrapper)')


def test_feature_loader_dispatches_land_event():
    loader = FEATURE_LOADER.read_text()
    assert 'tofu:feature-bundle-loaded' in loader, (
        'feature-loader must dispatch tofu:feature-bundle-loaded on '
        'bundle land — mobile_panels re-wraps its captured toggles on it')


def test_mobile_preland_kick():
    src = MOBILE_PANELS.read_text()
    assert '_loadFeatureBundle()' in src, (
        'a pre-land mobile open must kick the feature-bundle load and '
        'fill the sheet on resolve (never an empty dead-end panel)')


# ---------------------------------------------------------------------------
# 5. dev-fallback tags (six files keep their index.html script tags)
# ---------------------------------------------------------------------------
def test_dev_fallback_script_tags_kept():
    html = INDEX_HTML.read_text()
    missing = [f for f in PANEL_FILES if f'static/js/{f}' not in html]
    assert not missing, (
        f'index.html lost dev-fallback tags for {missing} — deferred '
        f'modules keep their tags so the dev fallback still loads them')
