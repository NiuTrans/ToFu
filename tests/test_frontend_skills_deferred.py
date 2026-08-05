"""Guards for Epic-E pt_3879f00e sub-9B — defer skills.js + skills_install.js
(25.1KB, the Skills settings tab).

Census (2026-08-01): the tab populate (`_populateSkillsTab`) is typeof-gated
at settings/core_panel.js:295; the i18n re-render hook is typeof-gated
(i18n.js:4216 `typeof _skillsRender === 'function'`); the only cross-module
bare call — skills_install.js's `await _populateSkillsTab()` after a zip
install — becomes INTRA-BUNDLE once both files move together (no gate
needed). Static panel HTML (settings_panels/skills.html, server-spliced)
references `_skillsSetScope` / `_skillsFilter` — those two become
feature-loader stubs. `_populateSkillsTab` is deliberately NOT stubbed: it
is a tab-populate, gated at the switch (sub-5A acceptance — a re-switch
populates once the idle prefetch lands, and openSettings() warms the
bundle since sub-9A).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-loader.js'
I18N = ROOT / 'static' / 'js' / 'i18n.js'
SKILLS_INSTALL = ROOT / 'static' / 'js' / 'skills_install.js'
PANEL_HTML = ROOT / 'static' / 'settings_panels' / 'skills.html'

SKILLS_STUBS = ('_skillsSetScope', '_skillsFilter')


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest move — BOTH files, together, skills before install
# ---------------------------------------------------------------------------
def test_skills_deferred_installer_stays_core():
    bundle, deferred, _ep, _crit = _manifest()
    assert 'skills.js' in deferred, 'skills.js must be in _DEFERRED_FILES'
    assert 'skills.js' not in bundle, (
        'skills.js must NOT remain in _BUNDLE_FILES')
    # Shipped shape (2026-08-01): the installer STAYS CORE so a zip install
    # works even before the feature bundle lands; its post-install tab
    # refresh is typeof-gated instead. Moving it would be harmless but
    # needlessly delays the drop-zone wiring to bundle arrival.
    assert 'skills_install.js' in bundle, (
        'skills_install.js must STAY in _BUNDLE_FILES — installer-stays-core '
        'shape (drop zone wired at boot, install succeeds pre-land)')
    assert 'skills_install.js' not in deferred


# ---------------------------------------------------------------------------
# 2. stubs (py + js dual tables) for the static panel onclicks
# ---------------------------------------------------------------------------
def test_skills_stubs_in_py_table():
    _bf, _df, entry_points, _crit = _manifest()
    missing = [s for s in SKILLS_STUBS if s not in entry_points]
    assert not missing, (
        f'_DEFERRED_ENTRY_POINTS is missing skills stubs: {missing}')


def test_skills_stubs_in_loader_table():
    loader = FEATURE_LOADER.read_text()
    missing = [s for s in SKILLS_STUBS if f"'{s}'" not in loader]
    assert not missing, (
        f'feature-loader.js is missing skills stubs: {missing}')


def test_static_panel_onclicks_exist():
    html = PANEL_HTML.read_text()
    assert '_skillsSetScope(' in html and '_skillsFilter(' in html, (
        'skills.html lost the static onclicks the stubs cover')


# ---------------------------------------------------------------------------
# 3. cross-boundary pins: i18n hook gated, tab populate gated, install call
#    intra-bundle (no gate added — both files share the feature bundle)
# ---------------------------------------------------------------------------
def test_i18n_hook_stays_gated():
    src = I18N.read_text()
    assert "typeof _skillsRender === 'function'" in src, (
        'i18n.js language-change hook must keep _skillsRender typeof-gated')


def test_install_call_gated():
    """With the installer-stays-core shape, skills_install.js can run while
    skills.js is still absent — its post-install `_populateSkillsTab()`
    call MUST be typeof-gated or a pre-land zip install throws
    ReferenceError after succeeding server-side."""
    src = SKILLS_INSTALL.read_text()
    assert re.search(
        r"typeof\s+_populateSkillsTab\s*===\s*'function'", src), (
        'skills_install.js must typeof-guard _populateSkillsTab — the '
        'installer is core, the panel it refreshes is deferred')
