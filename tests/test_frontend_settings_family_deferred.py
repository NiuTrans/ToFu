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

pytestmark = pytest.mark.unit


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUNDLER_PY = ROOT / 'lib' / 'js_bundler.py'
INDEX_HTML = ROOT / 'index.html'
FEATURE_LOADER = ROOT / 'static' / 'js' / 'feature-loader.js'
ONBOARDING = ROOT / 'static' / 'js' / 'onboarding.js'
TOOLBAR = ROOT / 'static' / 'js' / 'main' / 'main_toolbar_ui.js'
CORE_PANEL = ROOT / 'static' / 'js' / 'settings' / 'core_panel.js'
LOCAL_EP = ROOT / 'static' / 'js' / 'settings' / 'local_endpoints.js'

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


def _manifest():
    import lib.js_bundler as jb
    return jb._extract_manifest_from_source(str(BUNDLER_PY))


# ---------------------------------------------------------------------------
# 1. manifest move (failing-first drivers)
# ---------------------------------------------------------------------------
def test_family_in_deferred_files():
    _bf, deferred, _ep, _crit = _manifest()
    missing = [f for f in FAMILY if f not in deferred]
    assert not missing, (
        f'these settings-family files must be in _DEFERRED_FILES: {missing}')


def test_family_not_in_core_bundle_files():
    bundle, _df, _ep, _crit = _manifest()
    present = [f for f in FAMILY if f in bundle]
    assert not present, (
        f'these settings-family files must NOT remain in _BUNDLE_FILES: {present}')


def test_branding_stays_core():
    """main.js:88 + main.js:349 call _modelShortName() BARE on the
    boot/model-switch path — branding.js can never defer (msagblke's
    boundary catch, 2026-08-02)."""
    bundle, deferred, _ep, _crit = _manifest()
    assert 'settings/branding.js' in bundle, (
        'settings/branding.js must STAY in _BUNDLE_FILES — main.js calls '
        '_modelShortName() BARE at boot/model-switch (_applyModelUI)')
    assert 'settings/branding.js' not in deferred
    main = (ROOT / 'static' / 'js' / 'main.js').read_text()
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
    _bf, deferred, _ep, _crit = _manifest()
    def _idx(f):
        return deferred.index(f)
    assert _idx('settings/section_requires.js') < _idx('settings/core_panel.js'), (
        'section_requires must load BEFORE core_panel (the data-requires '
        'degraded-section contract is consumed by core_panel at render)')
    assert _idx('settings/provider_faces.js') < _idx('settings/provider_render.js'), (
        'provider_faces declares _faceChipHTML/_renderFacesSection consumed '
        'by provider_render — order preserved from the core manifest')
    assert _idx('widgets/chip_input.js') < _idx('settings/other_tabs.js'), (
        'chip_input is consumed by other_tabs/save_export at runtime')


# ---------------------------------------------------------------------------
# 2. entry-point stubs (failing-first drivers)
# ---------------------------------------------------------------------------
def test_stubs_in_py_table():
    _bf, _df, entry_points, _crit = _manifest()
    for name in STUBS:
        assert name in entry_points, (
            f'{name} must be a _DEFERRED_ENTRY_POINTS member — the sidebar '
            'gear / mobile sheet / onboarding flows are always-reachable')


def test_stubs_in_js_table():
    loader = FEATURE_LOADER.read_text()
    for name in STUBS:
        assert f"'{name}'" in loader, (
            f'{name} must be in feature-loader.js _DEFERRED_ENTRY_POINTS')


def test_modal_internal_handlers_NOT_stubbed():
    """System-prompt editor + _mcp* handlers are only reachable INSIDE the
    open settings modal (bundle already present) — stubbing them would
    fetch the bundle for nothing (Project Brain precedent)."""
    _bf, _df, entry_points, _crit = _manifest()
    for name in ('applySystemPromptEditor', 'closeSystemPromptEditor',
                 'resetSystemPromptBlocks', '_mcpSaveServer', '_mcpDoInstall'):
        assert name not in entry_points


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


def test_dev_fallback_script_tags_kept():
    html = INDEX_HTML.read_text()
    for f in ('static/js/settings/core_panel.js',
              'static/js/settings/branding.js',
              'static/js/widgets/chip_input.js'):
        assert f in html, (
            f'index.html must carry the {f} dev-fallback <script> tag')
