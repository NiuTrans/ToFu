"""Wiring pins for the settings-footer save feedback loop (2026-09-02).

Symptom: the footer 保存 button ("saveSettings()") looked dead when clicked.
Three compounding gaps in ``settings/save_export.js``:

1. The awaited save chain (STT persist → model-routing replace → credential
   secrets → server-config update → reload) gave ZERO in-flight feedback —
   the button stayed enabled with no "saving" state, so on a slow network
   the user clicked again.
2. No concurrency latch: a second click raced ``modelRouting.replace`` with
   the same ``expected_revision`` and lost to a 409 conflict.
3. Failures surfaced only in an 11px footer hint — and anything thrown
   OUTSIDE ``_saveServerConfig``'s try (DOM reads, ``_persistSttProvider``,
   collectors) reached no UI at all: the action registry just console.errors
   rejected promises, and ``debugLog`` is console/diagnostics-only (see
   frontend/src/core/debug-runtime-owner.ts), not a toast.

Fix being pinned: a busy latch that disables the button + shows
``common.saving`` in the footer hint, a body wrapper that funnels EVERY
throw into that hint, and a disabled style for the footer buttons.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""
from __future__ import annotations

import json
import os
import re

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAVE_EXPORT = os.path.join(
    PROJECT_ROOT, 'frontend', 'src', 'runtime', 'sections', 'settings', 'save_export.js')
SETTINGS_STATE = os.path.join(
    PROJECT_ROOT, 'frontend', 'src', 'runtime', 'sections', 'settings.js')
CORE_PANEL = os.path.join(
    PROJECT_ROOT, 'frontend', 'src', 'runtime', 'sections', 'settings', 'core_panel.js')
SETTINGS_CSS = os.path.join(
    PROJECT_ROOT, 'frontend', 'src', 'styles', 'application',
    '07-settings-providers-onboarding.css')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _save_export():
    return _read(SAVE_EXPORT)


def test_footer_save_button_has_stable_id():
    """The busy latch looks the button up by id; the id must stay on the
    footer save button in index.html."""
    html = _read(os.path.join(PROJECT_ROOT, 'index.html'))
    match = re.search(r'<button[^>]*data-tofu-action="saveSettings\(\)"[^>]*>', html)
    assert match, 'footer save button with data-tofu-action="saveSettings()" not found'
    assert 'id="settingsSaveBtn"' in match.group(0), (
        'footer save button lost id="settingsSaveBtn" — the busy latch would '
        'silently stop disabling it.'
    )


def test_save_has_concurrency_latch():
    src = _save_export()
    assert re.search(r'var _settingsSaveBusy = false;', src), 'busy latch var missing'
    body = re.search(r'async function saveSettings\(\) \{(?P<body>.*?)\n\}', src, re.S)
    assert body, 'saveSettings() not found'
    assert 'if (_settingsSaveBusy) return;' in body.group('body'), (
        'saveSettings() no longer refuses re-entry while a save is in flight.'
    )


def test_save_body_is_wrapped_and_errors_reach_footer_hint():
    """Every throw in the save body — not just _saveServerConfig's try — must
    land in the footer hint; the action registry only console.errors."""
    src = _save_export()
    assert re.search(
        r'async function saveSettings\(\) \{.*?\}\s*catch \(e\) \{\s*_settingsSaveFailed\(e\);',
        src, re.S,
    ), 'saveSettings() no longer funnels body exceptions into _settingsSaveFailed'
    assert 'async function _saveSettingsBody()' in src, (
        'save body wrapper _saveSettingsBody() missing.'
    )
    failed = re.search(r'function _settingsSaveFailed\(e\) \{(?P<body>.*?)\n\}', src, re.S)
    assert failed, '_settingsSaveFailed() not found'
    assert "getElementById('settingsStatusHint')" in failed.group('body')
    assert '保存失败：' in failed.group('body'), (
        '_settingsSaveFailed() must write the failure into settingsStatusHint.'
    )


def test_server_config_failure_uses_the_same_funnel():
    """_saveServerConfig's own catch must reuse _settingsSaveFailed (single
    failure funnel) instead of a divergent hand-rolled hint write."""
    src = _save_export()
    catch = re.search(
        r'function _saveServerConfig\(\).*?\} catch \(e\) \{(?P<body>.*?)\n  \}\n\}', src, re.S)
    assert catch, '_saveServerConfig catch block not found'
    assert '_settingsSaveFailed(e);' in catch.group('body')
    assert 'return false;' in catch.group('body')


def test_busy_state_disables_button_and_shows_saving_hint():
    src = _save_export()
    busy = re.search(r'function _setSettingsSaveBusy\(busy\) \{(?P<body>.*?)\n\}', src, re.S)
    assert busy, '_setSettingsSaveBusy() not found'
    body = busy.group('body')
    assert "getElementById('settingsSaveBtn')" in body and 'btn.disabled = busy' in body, (
        'busy state must disable the footer save button.'
    )
    assert "t('common.saving')" in body, (
        'busy state must show the common.saving hint so a click has immediate feedback.'
    )


def test_saving_hint_cleared_only_if_untouched():
    """On completion the latch may clear the hint ONLY while it still shows
    the saving text — a success ('settings.saved') or failure ('保存失败：')
    message written mid-flight must survive."""
    src = _save_export()
    busy = re.search(r'function _setSettingsSaveBusy\(busy\) \{(?P<body>.*?)\n\}', src, re.S)
    assert busy
    assert re.search(
        r"else if \(hint\.textContent === t\('common\.saving'\)\) hint\.textContent = '';",
        busy.group('body'),
    )


def test_save_waits_for_model_routing_authority_before_feature_persistence():
    """Settings opens with parallel reads; Save must join/retry the authority
    read before the typed STT owner tries to mutate it."""
    src = _save_export()
    body = re.search(
        r'async function _saveSettingsBody\(\) \{(?P<body>.*?)\n\}', src, re.S)
    assert body, '_saveSettingsBody() not found'
    save_body = body.group('body')
    ready = save_body.index('await _loadModelRoutingAuthority()')
    speech = save_body.index('await _persistSttProvider()')
    replace = save_body.index('await _saveServerConfig()')
    assert ready < speech < replace
    assert '_stgModelRoutingLoadError' in save_body
    assert '_stgModelRoutingLoadPromise || !_stgModelRouting' in save_body


def test_model_routing_authority_reads_are_coalesced_and_retryable():
    state = _read(SETTINGS_STATE)
    core = _read(CORE_PANEL)
    loader = core[core.index('function _loadModelRoutingAuthority()'):]
    loader = loader[:loader.index('\nfunction _refreshSubscriptionModelCatalog()')]
    assert 'let _stgModelRoutingLoadPromise = null;' in state
    assert 'if (_stgModelRoutingLoadPromise)' in loader
    assert '_stgModelRoutingLoadPromise = (async function()' in loader
    assert 'finally' in loader
    assert '_stgModelRoutingLoadPromise = null;' in loader


def test_footer_button_disabled_style_exists():
    css = _read(SETTINGS_CSS)
    assert re.search(r'\.settings-footer \.btn:disabled', css), (
        'disabled style for settings-footer buttons missing from '
        '07-settings-providers-onboarding.css.'
    )


def test_common_saving_key_in_locales():
    for lang in ('zh', 'en'):
        data = json.loads(_read(os.path.join(
            PROJECT_ROOT, 'frontend', 'src', 'i18n', 'locales', f'{lang}.json')))
        assert data.get('common.saving'), f'common.saving missing from {lang}.json'
