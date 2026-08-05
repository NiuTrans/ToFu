"""Guards for pt_3879f00e sub-part 7 — split project.js (89KB): the STATE
subset stays core as project_state.js (24KB); the PANEL (67KB) defers.

Why a split, not a move (census 2026-08-01, 37 call sites across 12 files
+ ~20 index.html onclicks):
  * BOOT: main.js:1354-1355 calls loadProjectStatus() + _updateAutoApplyUI()
    BARE (the project bar is first paint when the conv has a project),
  * SSE: sse_handlers_tool.js:177 + sse_handlers_misc.js:389 call
    _applyProjectData on project events (typeof-gated, but on the hot
    streaming path — deferring would pull the whole feature bundle on
    every project event),
  * CONV LIFECYCLE: main_conv_lifecycle.js calls _restoreConvProject /
    _clearProjectStateLocal; presence.js + project-brain*.js read
    _getConvProjectPath,
  * BAR: the always-visible bar buttons + badge onclicks
    (toggleAutoApply / clearProject / toggleProjectBarReadOnly) are
    state ops, not panel UI.

Core subset (project_state.js): _scanPollTimer, toggleAutoApply,
_updateAutoApplyUI, _saveConvProjectPath, _getConvProjectPath,
_isRemotePath, _applyRemoteProjectState, _clearProjectStateLocal,
_restoreConvProject, _projectBarFolders, clearProject, _applyProjectData,
_startScanPoll, _stopScanPoll, _updateProjectUI,
_deriveConvPathsFromState, _roToggleSeq, toggleProjectBarReadOnly,
loadProjectStatus.

Reverse bare calls from the state subset into the panel are
typeof-guarded: saveRecentProject (in _restoreConvProject),
closeProjectModal + the _mpFolders/_mpReadOnly reset (in clearProject).

13 feature-loader stubs cover every chat-rendered / bar-rendered panel
entry (approval, stdin, HG, undo/redo, apply-code, openProjectModal).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
PANEL = ROOT / 'static' / 'js' / 'project.js'
STATE = ROOT / 'static' / 'js' / 'project_state.js'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-loader.js'

STATE_SYMBOLS = (
    '_scanPollTimer', 'toggleAutoApply', '_updateAutoApplyUI',
    '_saveConvProjectPath', '_getConvProjectPath', '_isRemotePath',
    '_applyRemoteProjectState', '_clearProjectStateLocal',
    '_restoreConvProject', '_projectBarFolders', 'clearProject',
    '_applyProjectData', '_startScanPoll', '_stopScanPoll',
    '_updateProjectUI', '_deriveConvPathsFromState', '_roToggleSeq',
    'toggleProjectBarReadOnly', 'loadProjectStatus',
)
PANEL_STUBS = (
    'openProjectModal', 'closeProjectModal',
    'resolveWriteApproval', 'submitStdinInput', 'submitStdinEof',
    'submitHumanGuidanceChoice', 'submitHumanGuidanceFreeText',
    'undoConvModifications', 'undoAllModifications', 'redoConvModifications',
    'openApplyModal', 'closeApplyModal', 'confirmApplyCode',
)


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


def _state_src():
    return STATE.read_text(encoding='utf-8') if STATE.exists() else ''


def _panel_src():
    return PANEL.read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# 1. manifest (failing-first drivers)
# ---------------------------------------------------------------------------
def test_state_in_core_panel_deferred():
    bundle, deferred, _ep, _crit = _manifest()
    assert 'project_state.js' in bundle, (
        "'project_state.js' must be in _BUNDLE_FILES — the boot/SSE/bar "
        'state subset is first-paint critical')
    assert 'project.js' in deferred, (
        "'project.js' (the panel) must be in _DEFERRED_FILES — 67KB out "
        'of the render-blocking core')


def test_panel_not_in_core_state_not_deferred():
    bundle, deferred, _ep, _crit = _manifest()
    assert 'project.js' not in bundle, (
        "'project.js' must NOT remain in _BUNDLE_FILES — double-load would "
        'duplicate the panel state (_mpFolders/_browseState)')
    assert 'project_state.js' not in deferred, (
        "'project_state.js' must NOT be deferred — main.js calls "
        'loadProjectStatus() BARE at boot')


def test_state_loads_at_panel_old_position():
    bundle, _df, _ep, _crit = _manifest()
    si = bundle.index('project_state.js')
    # Anchor re-pointed 2026-08-01 (Epic-E sub-9): memory.js left core —
    # the next surviving core file after project_state.js is now
    # memory_skill_install.js. The invariant is unchanged: every core
    # consumer below the panel's old slot must still resolve.
    assert bundle.index('memory_skill_install.js') > si, (
        'project_state.js must sit at the panel\'s old position (before '
        'the memory family) so every core consumer below it resolves')


# ---------------------------------------------------------------------------
# 2. the move itself
# ---------------------------------------------------------------------------
def test_state_symbols_present_in_state_file():
    src = _state_src()
    missing = [s for s in STATE_SYMBOLS
               if not re.search(r'(?m)^(?:async )?(?:function|let|var) '
                                + re.escape(s) + r'\b', src)]
    assert not missing, (
        f'project_state.js is missing state symbols: {missing}')


def test_state_symbols_absent_from_panel():
    src = _panel_src()
    present = [s for s in STATE_SYMBOLS
               if re.search(r'(?m)^(?:async )?(?:function|let|var) '
                            + re.escape(s) + r'\b', src)]
    assert not present, (
        f'project.js (panel) must not keep state-subset definitions: {present}')


def test_panel_keeps_panel_functions():
    src = _panel_src()
    for fn in ('openProjectModal', 'mpApplyFolders', 'browseDirectory',
               'resolveWriteApproval', 'undoConvModifications',
               'openApplyModal', '_renderRemoteDevicesSection',
               '_updateProjectModalStatus'):
        assert re.search(r'(?m)^(?:async )?function ' + fn + r'\b', src), (
            f'project.js (panel) lost its own function: {fn}')


# ---------------------------------------------------------------------------
# 3. reverse guards in the state subset (panel may be absent)
# ---------------------------------------------------------------------------
def test_save_recent_project_guarded():
    assert re.search(
        r"typeof\s+saveRecentProject\s*===\s*['\"]function['\"]",
        _state_src()), (
        '_restoreConvProject must typeof-guard saveRecentProject — it '
        'lives in the deferred panel now')


def test_clear_project_reverse_calls_guarded():
    src = _state_src()
    assert re.search(
        r"typeof\s+closeProjectModal\s*===\s*['\"]function['\"]", src), (
        'clearProject must typeof-guard closeProjectModal (deferred panel)')
    assert re.search(r"typeof\s+_mpFolders\s*!==\s*['\"]undefined['\"]", src), (
        'clearProject must guard the _mpFolders/_mpReadOnly reset — the '
        'modal state vars are declared in the deferred panel')


# ---------------------------------------------------------------------------
# 4. entry-point stubs, py + js dual tables
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


def test_state_functions_not_stubbed():
    """The state subset is CORE — stubbing it would lazy-load the feature
    bundle on boot-critical paths."""
    _bf, _df, entry_points, _crit = _manifest()
    for name in ('loadProjectStatus', '_applyProjectData',
                 '_restoreConvProject', 'toggleProjectBarReadOnly',
                 'toggleAutoApply', 'clearProject'):
        assert name not in entry_points, (
            f'{name} must NOT be a deferred entry point — it is CORE state')
    loader = FEATURE_LOADER.read_text()
    for name in ('loadProjectStatus', '_applyProjectData',
                 '_restoreConvProject', 'toggleProjectBarReadOnly',
                 'toggleAutoApply', 'clearProject'):
        assert f"'{name}'" not in loader


# ---------------------------------------------------------------------------
# 5. dev-fallback tags (both files, state before panel)
# ---------------------------------------------------------------------------
def test_dev_fallback_script_tags():
    html = INDEX_HTML.read_text()
    si = html.find('static/js/project_state.js')
    pi = html.find('static/js/project.js')
    assert si != -1, 'index.html lost the project_state.js dev-fallback tag'
    assert pi != -1, 'index.html lost the project.js dev-fallback tag'
    assert si < pi, (
        'the project_state.js tag must precede the project.js tag '
        '(panel calls the state subset at runtime)')
