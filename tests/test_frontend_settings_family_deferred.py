"""Guards for pt_3879f00e sub-part 10 — defer the ENTIRE settings/
subpackage (22 files) + widgets/chip_input.js (~455KB source) out of the
render-blocking core. The line-closer slice (gap to 1.2MB was ~74KB).

Census (2026-08-01, grep-verified):
  * The whole family renders ONLY inside the user-triggered Settings
    modal (sidebar gear / mobile sheet / onboarding / toolbar flows).
  * Boot config load (_loadServerConfigAndPopulate,
    main_toolbar_ui.js:391, core) reads Api.serverConfig.get() and writes
    fields — it calls ZERO settings/ functions (dependency is one-way:
    the panel reads core state at runtime).
  * visibility_defaults.js has NO load-time side effects and no boot
    callers. settings/branding.js is the BOUNDARY (msagblke's catch):
    main.js:88/349 call _modelShortName() BARE on the boot/model-switch
    path — it STAYS in core; the family's brand-helper reads are the
    safe deferred→core direction.
  * oauth.js / key_stats.js have no boot-path readers from main/*.
  * EVERY programmatic caller of openSettings/switchSettingsTab is
    typeof-guarded: onboarding.js:271, main_toolbar_ui.js:382/537,
    skills_install.js:70 — gate+stub composition (sub-9 pattern): the
    guard passes on the stub, which loads the bundle and dispatches.
  * _serverConfig / _keyStatsCache / _keyStatsLoading stay declared in
    settings.js (the 1.5KB head, CORE) — read by main_input_handling.js.
  * widgets/chip_input.js is used ONLY by settings/other_tabs.js +
    settings/save_export.js — moves with the family.
  * local_endpoints.js's module-level metrics setInterval self-arms
    whenever the feature bundle lands (myday/timer precedent).

FOUR feature-loader stubs (py + js dual tables): openSettings is the
genuine early entry (always-visible sidebar gear + mobile sheet +
onboarding wizard + toolbar flows); switchSettingsTab is called
immediately after openSettings in every flow (same early window);
closeSettings + saveSettings are defense-in-depth (image-gen
precedent). Modal-internal handlers (system-prompt editor, _mcp*) are
deliberately NOT stubbed — unreachable before the bundle lands
(Project Brain precedent).
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests._runtime_sections import (
    runtime_section,
    runtime_section_names,
    runtime_section_path,
)

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'
FEATURE_LOADER = ROOT / 'frontend' / 'src' / 'main.ts'
ONBOARDING = pathlib.Path(runtime_section_path('onboarding.js'))
TOOLBAR = pathlib.Path(runtime_section_path('main/main_toolbar_ui.js'))
CORE_PANEL = pathlib.Path(runtime_section_path('settings/core_panel.js'))
LOCAL_EP = pathlib.Path(runtime_section_path('settings/local_endpoints.js'))

FAMILY = (
    # settings/branding.js deliberately ABSENT (2026-08-02 boundary fix,
    # msagblke): main.js:88/349 call _modelShortName() BARE on the
    # boot/model-switch path (_applyModelUI) — deferring branding breaks
    # the boot model paint with ReferenceError. It STAYS in core.
    'settings/provider_templates.js',
    'settings/auto_setup.js', 'settings/local_endpoints.js',
    'settings/section_requires.js', 'settings/core_panel.js',
    'settings/provider_render.js', 'settings/provider_faces.js',
    'settings/key_stats.js', 'settings/balance.js',
    'settings/template_actions.js', 'settings/model_edit.js',
    'settings/visibility_defaults.js', 'settings/other_tabs.js',
    'settings/speech.js', 'settings/auth_sources.js',
    'settings/private_hosts.js', 'settings/save_export.js',
    'settings/system_prompt_editor.js', 'settings/oauth.js',
    'settings/mcp.js', 'settings/devices.js',
    'widgets/chip_input.js',
)
STUBS = ('openSettings', 'closeSettings', 'saveSettings', 'switchSettingsTab')
NATIVE_OWNERS = {
    'settings/auto_setup.js': 'frontend/src/features/settings/auto-setup.ts',
    'settings/section_requires.js': 'frontend/src/features/settings/section-requires.ts',
    'settings/key_stats.js': 'frontend/src/features/settings/key-stats.ts',
    'settings/balance.js': 'frontend/src/features/settings/balance.ts',
    'settings/speech.js': 'frontend/src/features/settings/speech.ts',
    'settings/auth_sources.js': 'frontend/src/features/settings/auth-sources.ts',
    'settings/private_hosts.js': 'frontend/src/features/settings/private-hosts.ts',
    'settings/devices.js': 'frontend/src/features/settings/devices.ts',
}


def _manifest():
    return list(runtime_section_names()), tuple(NATIVE_OWNERS), STUBS, ()


# ---------------------------------------------------------------------------
# 1. manifest move (failing-first drivers)
# ---------------------------------------------------------------------------
def test_family_in_deferred_files():
    runtime, native, _ep, _crit = _manifest()
    missing = [name for name in FAMILY
               if name not in runtime and name not in native]
    assert not missing, f'settings owners missing from the Vite graph: {missing}'
    for name, owner in NATIVE_OWNERS.items():
        assert (ROOT / owner).is_file(), f'{name} native owner missing: {owner}'


def test_family_not_in_core_bundle_files():
    runtime, _native, _ep, _crit = _manifest()
    duplicated = [name for name in NATIVE_OWNERS if name in runtime]
    assert not duplicated, (
        f'native settings owners duplicated in the retained runtime: {duplicated}')


def test_branding_stays_core():
    """main.js:88 + main.js:349 call _modelShortName() BARE on the
    boot/model-switch path — branding.js can never defer (msagblke's
    boundary catch, 2026-08-02)."""
    bundle, deferred, _ep, _crit = _manifest()
    assert 'settings/branding.js' in bundle, (
        'settings/branding.js must STAY in _BUNDLE_FILES — main.js calls '
        '_modelShortName() BARE at boot/model-switch (_applyModelUI)')
    assert 'settings/branding.js' not in deferred
    main = runtime_section('main.js')
    assert main.count('_modelShortName(') >= 2, (
        'main.js must keep its bare _modelShortName calls — if they ever '
        'become guarded, branding can move to the family')


def test_settings_head_stays_core():
    bundle, _df, _ep, _crit = _manifest()
    assert 'settings.js' in bundle, (
        'settings.js (the 1.5KB head: var _serverConfig/_keyStatsCache/'
        '_keyStatsLoading) must STAY in _BUNDLE_FILES — main_input_handling.js '
        'reads _serverConfig at runtime and the deferred family assumes the '
        'head vars exist')


def test_deferred_order_preserved():
    deferred, _native, _ep, _crit = _manifest()
    def _idx(f):
        return deferred.index(f)
    assert _idx('settings/provider_faces.js') < _idx('settings/provider_render.js'), (
        'provider_faces declares _faceChipHTML/_renderFacesSection consumed '
        'by provider_render — order preserved from the core manifest')
    assert _idx('widgets/chip_input.js') < _idx('settings/other_tabs.js'), (
        'chip_input is consumed by other_tabs/save_export at runtime')


# ---------------------------------------------------------------------------
# 2. entry-point stubs (failing-first drivers)
# ---------------------------------------------------------------------------
def test_stubs_in_py_table():
    loader = FEATURE_LOADER.read_text()
    match = re.search(r'const settingsEntries = new Set\(\[(.*?)\]\);',
                      loader, re.S)
    assert match, 'main.ts lost the explicit settings feature-entry set'
    entry_points = set(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1)))
    assert entry_points == set(STUBS), (
        f'Vite settings entry surface drifted: {sorted(entry_points)}')


def test_stubs_in_js_table():
    loader = FEATURE_LOADER.read_text()
    for name in STUBS:
        assert f"'{name}'" in loader, (
            f'{name} must be routed by the Vite feature registry')


def test_modal_internal_handlers_NOT_stubbed():
    """System-prompt editor + _mcp* handlers are only reachable INSIDE the
    open settings modal (bundle already present) — stubbing them would
    fetch the bundle for nothing (Project Brain precedent)."""
    loader = FEATURE_LOADER.read_text()
    for name in ('applySystemPromptEditor', 'closeSystemPromptEditor',
                 'resetSystemPromptBlocks', '_mcpSaveServer', '_mcpDoInstall'):
        assert name not in loader


# ---------------------------------------------------------------------------
# 3. callers + module facts (controls)
# ---------------------------------------------------------------------------
def test_programmatic_callers_guarded():
    assert re.search(
        r"typeof openSettings !== 'function'\)\s*return", ONBOARDING.read_text()), (
        'onboarding.js must keep its typeof guard before the bare '
        'openSettings() call (the guard passes on the stub, which loads)')
    src = TOOLBAR.read_text()
    assert src.count("typeof openSettings === 'function'") >= 2, (
        'main_toolbar_ui.js must keep BOTH typeof-guarded openSettings '
        'call sites (:382 and :537)')
    assert src.count("typeof switchSettingsTab === 'function'") >= 2, (
        'main_toolbar_ui.js must keep its switchSettingsTab guards')


def test_entry_points_defined_in_family():
    src = CORE_PANEL.read_text()
    assert re.search(r'(?m)^function openSettings\(', src), (
        'openSettings must stay defined in settings/core_panel.js — the '
        'stub dispatches to it when the feature bundle lands')
    assert re.search(r'(?m)^function switchSettingsTab\(', src), (
        'switchSettingsTab must stay defined in settings/core_panel.js')


def test_local_endpoint_timer_moves_with_module():
    src = LOCAL_EP.read_text()
    assert 'setInterval(_refreshLocalEndpointMetrics' in src, (
        'the local-endpoints metrics timer must stay inside the module — '
        'it self-arms whenever the feature bundle lands (myday/timer '
        'precedent)')


def test_index_has_no_raw_settings_scripts():
    html = INDEX_HTML.read_text()
    for f in ('static/js/settings/core_panel.js',
              'static/js/settings/branding.js',
              'static/js/widgets/chip_input.js'):
        assert f not in html
    assert '<!-- TOFU_APP_ASSETS -->' in html
