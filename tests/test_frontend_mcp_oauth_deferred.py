"""Guards for Epic-E pt_3879f00e sub-10 — defer (almost) the ENTIRE
settings/ subpackage (~400KB), the line-closer slice.

Census (2026-08-01/02, grep-verified): the settings family renders ONLY
inside the user-triggered Settings modal. Programmatic
openSettings/switchSettingsTab callers outside the family resolve through the
feature bridge. index.html's settings gear is a static action covered by the
same registry. core_panel.js's bare populate chain
(_populateMcpTab/_loadOAuthStatus/_renderProvidersTab/…) is
INTRA-BUNDLE once core_panel.js itself moves.

Brand/name presentation now belongs to typed core modules imported by the
runtime prelude. Retained bare helper calls resolve to module-private aliases,
so there is no settings-owned boot boundary left. settings.js (the slim var
head) still STAYS:
_serverConfig/_keyStatsCache/_keyStatsLoading are read by
main_input_handling.js.

Stubs (4 entry points, py + js dual tables): openSettings,
closeSettings, saveSettings, switchSettingsTab.
"""

from __future__ import annotations

import pathlib
import json
import re

import pytest

from tests._runtime_sections import runtime_section, runtime_section_names, runtime_section_path

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
FEATURE_LOADER = ROOT / 'frontend' / 'src' / 'main.ts'
INDEX_HTML = ROOT / 'index.html'
MAIN_JS = pathlib.Path(runtime_section_path('main.js'))
PRELUDE = ROOT / 'frontend/src/runtime/sections/_prelude.js'
I18N = ROOT / 'frontend' / 'src' / 'i18n' / 'index.ts'
ONBOARDING = pathlib.Path(runtime_section_path('onboarding.js'))
MCP_HTML = ROOT / 'static' / 'settings_panels' / 'mcp.html'
OAUTH_HTML = ROOT / 'static' / 'settings_panels' / 'oauth.html'

FAMILY = (
    'settings/section_requires.js',
    'settings/core_panel.js',
    'settings/provider_render.js', 'settings/visibility_defaults.js',
    'settings/other_tabs.js', 'settings/speech.js',
    'settings/auth_sources.js', 'settings/private_hosts.js',
    'settings/save_export.js', 'settings/system_prompt_editor.js',
    'settings/oauth.js', 'settings/mcp.js', 'settings/devices.js',
)
ENTRY_STUBS = ('openSettings', 'closeSettings',
               'saveSettings', 'switchSettingsTab', '_oauthLogin')

NATIVE_OWNERS = {
    'settings/section_requires.js': 'frontend/src/features/settings/section-requires.ts',
    'settings/speech.js': 'frontend/src/features/settings/speech.ts',
    'settings/auth_sources.js': 'frontend/src/features/settings/auth-sources.ts',
    'settings/private_hosts.js': 'frontend/src/features/settings/private-hosts.ts',
    'settings/devices.js': 'frontend/src/features/settings/devices.ts',
}


def _manifest():
    return list(runtime_section_names()), (), ENTRY_STUBS, ()


# ---------------------------------------------------------------------------
# 1. the family moves; typed brand owners replace branding.js
# ---------------------------------------------------------------------------
def test_family_deferred_not_core():
    bundle, _deferred, _ep, _crit = _manifest()
    for name in FAMILY:
        if name in NATIVE_OWNERS:
            assert (ROOT / NATIVE_OWNERS[name]).is_file(), (
                f'{name} has neither a retained runtime section nor its native owner')
            assert name not in bundle, f'{name} was duplicated beside its native owner'
        else:
            assert bundle.count(name) == 1, f'{name} must occur once in the Vite runtime'


def test_typed_brand_owners_replace_retained_boot_breaker():
    bundle, _deferred, _ep, _crit = _manifest()
    assert 'settings/branding.js' not in bundle
    prelude = PRELUDE.read_text()
    assert "from '../core/model-brand-detection'" in prelude
    assert "from '../core/model-brand-icons'" in prelude
    assert "from '../core/model-display-names'" in prelude


def test_branding_boot_callers_use_composed_alias():
    """Retained boot callers remain wired through the prelude alias."""
    src = MAIN_JS.read_text()
    calls = [m.start() for m in re.finditer(r'_modelShortName\(', src)]
    assert len(calls) >= 2, (
        'main.js must keep its _modelShortName call sites during migration')
    assert 'modelShortName: _modelShortName' in PRELUDE.read_text()


def test_settings_head_moves_with_its_live_ports():
    manifest = json.loads((
        ROOT / 'frontend/src/runtime/sections/manifest.json'
    ).read_text(encoding='utf-8'))
    main = {row['source'] for row in manifest['sections']}
    settings = next(
        bundle for bundle in manifest['lazyBundles']
        if bundle['name'] == 'settings-presenters'
    )
    lazy = {row['source'] for row in settings['sections']}
    assert 'settings.js' not in main
    assert {'settings.js', 'settings/mcp.js'} <= lazy


# ---------------------------------------------------------------------------
# 2. the four entry-point stubs (py + js dual tables)
# ---------------------------------------------------------------------------
def test_entry_stubs_in_py_table():
    _bf, _df, entry_points, _crit = _manifest()
    missing = [s for s in ENTRY_STUBS if s not in entry_points]
    assert not missing, (
        f'_FEATURE_ENTRY_POINTS is missing settings entry stubs: {missing}')


def test_entry_stubs_in_loader_table():
    loader = FEATURE_LOADER.read_text()
    missing = [s for s in ENTRY_STUBS if not re.search(rf"['\"]{s}['\"]", loader)]
    assert not missing, (
        f'feature-bridge.js is missing settings entry stubs: {missing}')


# ---------------------------------------------------------------------------
# 3. programmatic callers of the entry points stay gated
# ---------------------------------------------------------------------------
def test_programmatic_openers_stay_gated():
    ob = ONBOARDING.read_text()
    assert "typeof runtimeScope.openSettings !== 'function'" in ob and \
           "typeof runtimeScope.switchSettingsTab === 'function'" in ob and \
           "typeof runtimeScope._oauthLogin === 'function'" in ob, (
        'onboarding.js must keep openSettings/switchSettingsTab gated')


# ---------------------------------------------------------------------------
# 4. i18n repaint hooks stay gated
# ---------------------------------------------------------------------------
def test_i18n_hooks_stay_gated():
    src = I18N.read_text()
    assert "tofu:language-change" in src
    owners = '\n'.join((ROOT / path).read_text() for path in (
        'frontend/src/features/settings.ts',
        'frontend/src/features/skills.ts',
        'frontend/src/features/skills/panel.ts',
        'frontend/src/features/memory/panel.ts',
    ))
    for name in ('_renderMcpCatalog', '_renderProvidersTab',
                 'render()', 'renderMemoryCards(memoryCache)'):
        assert name in owners, f'language-change repaint owner missing {name}'


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
    assert not m and 'LoadGuard' not in html, (
        'the removed classic LoadGuard must not return beside the Vite action registry')
    entries = ''
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
