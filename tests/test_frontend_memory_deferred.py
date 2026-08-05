"""Guards for Epic-E pt_3879f00e sub-9C — _esc promotion + defer
memory.js + memory_skill_install.js + preferences.js (29.2KB).

Hidden coupling found by the census (2026-08-01): memory.js holds the
repo's ONLY top-level `_esc` — an `escapeHtml` alias that ~70 call sites
across 7 core modules (artifacts.js ×9, compaction-viewer.js ×36,
core/toast.js ×3, log-clean.js ×8, preferences.js ×8, ui/streaming_ui.js
×2, ui/translation_render.js ×4) resolve through window scope. (Eight
other files carry NESTED `_esc` copies — local shadows, unaffected.)
Deferring memory.js without promotion would make every one of those sites
ReferenceError the moment the core bundle runs without the feature
bundle. Fix: promote the identical 3-line definition into
core/escape_html.js (position 8 in _BUNDLE_FILES — before every consumer)
and delete memory.js's copy, so exactly one definition survives.

Second trap: index.html's LoadGuard pre-stubs `toggleMemory` with
_notReady(). feature-loader's _installFeatureStub skips installation when
`typeof window[name] === 'function'`, so a LoadGuard stub would make the
lazy stub never install and the toolbar memory button would toast
"please wait" forever. LoadGuard is for CORE functions only — deferred
entry points must be removed from its list (peer-confirmed 2026-08-01,
mirror of sub-6's openDailyReport AddGuard).

Third: main.js:453 calls `_updateMemoryModalBtn()` BARE in
_applyMemoryToolUI — typeof-gated here.

preferences.js moves in the same commit: its `_populatePreferencesTab`
calls memory.js's refreshMemoryList via _refreshPrefsMemorySection —
intra-bundle once both are deferred. Its static panel onclicks
(refreshPreferences / savePreferences / openMemoryModal in
settings_panels/preferences.html) become stubs.
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
ESCAPE_HTML = ROOT / 'static' / 'js' / 'core' / 'escape_html.js'
MEMORY_JS = ROOT / 'static' / 'js' / 'memory.js'
MAIN_JS = ROOT / 'static' / 'js' / 'main.js'

MOVED = ('memory.js', 'preferences.js')
MEMORY_STUBS = (
    'toggleMemory', 'openMemoryModal', 'openMemoryCreateForm',
    'closeMemoryModal', 'toggleMemoryFromModal',
    'refreshPreferences', 'savePreferences',
)


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest move
# ---------------------------------------------------------------------------
def test_memory_duo_deferred_installer_stays_core():
    bundle, deferred, _ep, _crit = _manifest()
    for name in MOVED:
        assert name in deferred, f'{name} must be in _DEFERRED_FILES'
        assert name not in bundle, (
            f'{name} must NOT remain in _BUNDLE_FILES')
    # Shipped shape (2026-08-01): memory_skill_install.js STAYS CORE — it
    # only DEFINES _attachMemoryDropZone, which memory.js's openMemoryModal
    # calls at runtime (post-land); staying core wires nothing early and
    # keeps the call direction deferred→core (always safe).
    assert 'memory_skill_install.js' in bundle, (
        'memory_skill_install.js must STAY in _BUNDLE_FILES')
    assert 'memory_skill_install.js' not in deferred


# ---------------------------------------------------------------------------
# 2. the _esc promotion — exactly one definition, in core, before consumers
# ---------------------------------------------------------------------------
def test_esc_promoted_to_escape_html():
    src = ESCAPE_HTML.read_text()
    assert re.search(r'(?m)^function _esc\(s\) \{\s*\n?\s*return escapeHtml\(s\);', src), (
        'core/escape_html.js must define _esc(s){return escapeHtml(s);} — '
        'the ~70 cross-module call sites resolve through window scope')


def test_esc_removed_from_memory():
    src = MEMORY_JS.read_text()
    assert not re.search(r'(?m)^function _esc\(', src), (
        'memory.js must DROP its top-level _esc — the promoted core '
        'definition replaces it (duplicate top-level definitions are the '
        'whole-bundle hazard class)')


def test_esc_position_before_consumers():
    bundle, _df, _ep, _crit = _manifest()
    ei = bundle.index('core/escape_html.js')
    for consumer in ('artifacts.js', 'compaction-viewer.js', 'core/toast.js',
                     'log-clean.js', 'ui/streaming_ui.js',
                     'ui/translation_render.js'):
        assert bundle.index(consumer) > ei, (
            f'{consumer} must load AFTER core/escape_html.js')


# ---------------------------------------------------------------------------
# 3. LoadGuard: toggleMemory must NOT be pre-stubbed (deferred entry point)
# ---------------------------------------------------------------------------
def test_loadguard_drops_toggle_memory():
    html = INDEX_HTML.read_text()
    m = re.search(r'var stubs = \[(.*?)\];', html, re.S)
    assert m, 'LoadGuard stub list not found in index.html'
    entries = re.sub(r'/\*.*?\*/', '', m.group(1), flags=re.S)
    assert "'toggleMemory'" not in entries, (
        "LoadGuard must NOT pre-stub 'toggleMemory' — _installFeatureStub "
        "skips names that are already functions, so the lazy stub would "
        'never install and the toolbar memory button dies permanently')


# ---------------------------------------------------------------------------
# 4. main.js bare call gated
# ---------------------------------------------------------------------------
def test_main_js_modal_btn_call_gated():
    src = MAIN_JS.read_text()
    assert re.search(
        r"typeof\s+_updateMemoryModalBtn\s*===\s*'function'", src), (
        'main.js _applyMemoryToolUI must typeof-guard _updateMemoryModalBtn '
        '(memory.js is absent until the feature bundle lands)')


# ---------------------------------------------------------------------------
# 5. stubs (py + js dual tables)
# ---------------------------------------------------------------------------
def test_memory_stubs_in_py_table():
    _bf, _df, entry_points, _crit = _manifest()
    missing = [s for s in MEMORY_STUBS if s not in entry_points]
    assert not missing, (
        f'_DEFERRED_ENTRY_POINTS is missing memory/preferences stubs: {missing}')


def test_memory_stubs_in_loader_table():
    loader = FEATURE_LOADER.read_text()
    missing = [s for s in MEMORY_STUBS if f"'{s}'" not in loader]
    assert not missing, (
        f'feature-loader.js is missing memory/preferences stubs: {missing}')
