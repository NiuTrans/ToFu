"""Guards for Epic-E pt_3879f00e sub-10 — defer (almost) the ENTIRE
settings/ subpackage (~400KB), the line-closer slice, with the
branding.js boundary fix.

Census (2026-08-01/02, grep-verified): the settings family renders ONLY
inside the user-triggered Settings modal. Every programmatic
openSettings/switchSettingsTab caller outside the family is
typeof-guarded (onboarding.js:271-272, main_toolbar_ui.js:382/536,
skills_install.js:69-72) — gate+stub composition (sub-9 pattern): the
gate passes on the feature-loader stub, which loads the bundle and
dispatches. index.html's settings gear is a static onclick covered by
the same stubs. core_panel.js's bare populate chain
(_populateMcpTab/_loadOAuthStatus/_renderProvidersTab/…) is
INTRA-BUNDLE once core_panel.js itself moves.

THE BOUNDARY (this suite's centerpiece): settings/branding.js is NOT
settings-only. main.js:88 + main.js:349 call _modelShortName() BARE on
the boot/model-switch path (_applyModelUI) — deferring branding breaks
the boot model paint with ReferenceError. It therefore STAYS in core;
its brand helpers are consumed by the deferred family in the safe
deferred→core direction (visibility_defaults ×12, local_endpoints,
template_actions), and finish_info.js's cold finish-bar calls stay
always-satisfied. settings.js (the slim var head) also STAYS:
_serverConfig/_keyStatsCache/_keyStatsLoading are read by
main_input_handling.js.

Stubs (4 entry points, py + js dual tables): openSettings,
closeSettings, saveSettings, switchSettingsTab.
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-loader.js'
INDEX_HTML = ROOT / 'index.html'
MAIN_JS = ROOT / 'static' / 'js' / 'main.js'
I18N = ROOT / 'static' / 'js' / 'i18n.js'
ONBOARDING = ROOT / 'static' / 'js' / 'onboarding.js'
MCP_HTML = ROOT / 'static' / 'settings_panels' / 'mcp.html'
OAUTH_HTML = ROOT / 'static' / 'settings_panels' / 'oauth.html'

FAMILY = (
    'settings/provider_templates.js', 'settings/auto_setup.js',
    'settings/local_endpoints.js', 'settings/section_requires.js',
    'settings/core_panel.js', 'settings/provider_faces.js',
    'settings/provider_render.js', 'settings/key_stats.js',
    'settings/balance.js', 'settings/template_actions.js',
    'settings/model_edit.js', 'settings/visibility_defaults.js',
    'settings/other_tabs.js', 'settings/speech.js',
    'settings/auth_sources.js', 'settings/private_hosts.js',
    'settings/save_export.js', 'settings/system_prompt_editor.js',
    'settings/oauth.js', 'settings/mcp.js', 'settings/devices.js',
)
ENTRY_STUBS = ('openSettings', 'closeSettings',
               'saveSettings', 'switchSettingsTab')


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. the family moves; branding.js + the settings.js head STAY
# ---------------------------------------------------------------------------
def test_family_deferred_not_core():
    bundle, deferred, _ep, _crit = _manifest()
    for name in FAMILY:
        assert name in deferred, f'{name} must be in _DEFERRED_FILES'
        assert name not in bundle, (
            f'{name} must NOT remain in _BUNDLE_FILES')


def test_branding_stays_core_boot_breaker():
    bundle, deferred, _ep, _crit = _manifest()
    assert 'settings/branding.js' in bundle, (
        'settings/branding.js must STAY in _BUNDLE_FILES — main.js:88/349 '
        'call _modelShortName() BARE on the boot/model-switch path; '
        'deferring it breaks the boot model paint with ReferenceError')
    assert 'settings/branding.js' not in deferred, (
        'settings/branding.js must NOT be in _DEFERRED_FILES')


def test_branding_boot_callers_are_bare():
    """Pin the evidence: if main.js ever gates these two calls, this pin
    says the gate is fine too — but branding in core must stay either way
    (the deferred family's 14 helper call sites go deferred→core)."""
    src = MAIN_JS.read_text()
    calls = [m.start() for m in re.finditer(r'_modelShortName\(', src)]
    assert len(calls) >= 2, (
        'main.js must keep its _modelShortName call sites — the boundary '
        'evidence for branding.js staying core')


def test_settings_head_stays():
    bundle, deferred, _ep, _crit = _manifest()
    assert 'settings.js' in bundle, (
        'the settings.js slim head must STAY in core — '
        '_serverConfig/_keyStatsCache/_keyStatsLoading are read by '
        'main_input_handling.js')


# ---------------------------------------------------------------------------
# 2. the four entry-point stubs (py + js dual tables)
# ---------------------------------------------------------------------------
def test_entry_stubs_in_py_table():
    _bf, _df, entry_points, _crit = _manifest()
    missing = [s for s in ENTRY_STUBS if s not in entry_points]
    assert not missing, (
        f'_DEFERRED_ENTRY_POINTS is missing settings entry stubs: {missing}')


def test_entry_stubs_in_loader_table():
    loader = FEATURE_LOADER.read_text()
    missing = [s for s in ENTRY_STUBS if f"'{s}'" not in loader]
    assert not missing, (
        f'feature-loader.js is missing settings entry stubs: {missing}')


# ---------------------------------------------------------------------------
# 3. programmatic callers of the entry points stay gated
# ---------------------------------------------------------------------------
def test_programmatic_openers_stay_gated():
    ob = ONBOARDING.read_text()
    assert re.search(r"typeof openSettings \S+ 'function'", ob) and \
           re.search(r"typeof switchSettingsTab \S+ 'function'", ob), (
        'onboarding.js must keep openSettings/switchSettingsTab gated')


# ---------------------------------------------------------------------------
# 4. i18n repaint hooks stay gated
# ---------------------------------------------------------------------------
def test_i18n_hooks_stay_gated():
    src = I18N.read_text()
    for name in ('_renderMcpCatalog', '_renderProvidersTab',
                 '_skillsRender', '_renderMemoryCards'):
        assert f"typeof {name} === 'function'" in src, (
            f'i18n.js must keep {name} typeof-gated')


# ---------------------------------------------------------------------------
# 5. LoadGuard must NOT pre-stub the deferred entry points — a _notReady
#    stub makes _installFeatureStub skip the lazy stub (typeof guard), so
#    the settings gear would toast "please wait" until the idle prefetch
#    lands (and stay dead if the fetch ever fails). Same trap class as
#    sub-9C's toggleMemory, peer-confirmed.
# ---------------------------------------------------------------------------
def test_loadguard_drops_deferred_entry_points():
    html = INDEX_HTML.read_text()
    m = re.search(r'var stubs = \[(.*?)\];', html, re.S)
    assert m, 'LoadGuard stub list not found in index.html'
    entries = re.sub(r'/\*.*?\*/', '', m.group(1), flags=re.S)
    for name in ('openSettings', 'closeSettings', 'saveSettings',
                 'switchSettingsTab'):
        assert f"'{name}'" not in entries, (
            f"LoadGuard must NOT pre-stub '{name}' — the settings family "
            'is deferred; the pre-stub would block the lazy stub install')


# ---------------------------------------------------------------------------
# 5. static panel HTML stays wired (the 4 stubs cover the modal open;
#    every panel-internal onclick resolves post-land)
# ---------------------------------------------------------------------------
def test_static_panel_onclicks_exist():
    mcp = MCP_HTML.read_text()
    for name in ('_mcpConnectAll', '_mcpOpenAddModal', '_mcpSetScope',
                 '_mcpFilterCatalog'):
        assert f'{name}(' in mcp, f'mcp.html lost the {name} onclick'
    oa = OAUTH_HTML.read_text()
    for name in ('_oauthLogin', '_oauthLogout', '_oauthManualSubmit'):
        assert f'{name}(' in oa, f'oauth.html lost the {name} onclick'
