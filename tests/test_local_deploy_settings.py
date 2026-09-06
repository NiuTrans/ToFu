"""Owner pins for the 本地部署 (local deployment) settings flow.

The dedicated local-deployment button was dropped during the model-routing v2
rewrite (2026-09 regression): the template menu silently swallowed the
recipe-less ``local`` placeholder template, and the managed model-path deploy
handoff disappeared with it. These pins lock the restored contract:

- api.html wires ``addLocalProvider()`` next to 从模板添加/自定义服务商;
- the settings-presenters lazy bundle registers ``settings/local_deploy.js``
  and injects the cross-bundle ``newChat``/``updateSendButton`` services;
- the preset chooser keeps the owner-ratified tile order (custom LAST);
- the managed handoff fills the chat draft only AFTER ``newChat()`` —
  ``newChat`` reads the current input to archive the previous conversation,
  so filling earlier would attach the prompt to the wrong conversation;
- provider_render keeps the recipe-less template filter and the zero-model
  disabled state of the template wizard;
- both locales carry the new ``settings.localDeploy*`` keys plus the reused
  preset/managed keys.

Run isolated (project convention): PYTEST_DISABLE_PLUGIN_AUTOLOAD=1.
"""
from __future__ import annotations

import json
import os
import re

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECTIONS = os.path.join(PROJECT_ROOT, 'frontend', 'src', 'runtime', 'sections')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _local_deploy_src():
    return _read(os.path.join(SECTIONS, 'settings', 'local_deploy.js'))


def _settings_bundle():
    manifest = json.loads(_read(os.path.join(SECTIONS, 'manifest.json')))
    for bundle in manifest['lazyBundles']:
        if bundle['name'] == 'settings-presenters':
            return bundle
    raise AssertionError('settings-presenters bundle missing from manifest')


def test_api_panel_wires_local_deploy_button_between_template_and_custom():
    panel = _read(os.path.join(PROJECT_ROOT, 'static', 'settings_panels', 'api.html'))
    assert 'data-tofu-action="addLocalProvider()"' in panel
    assert 'data-i18n="settings.localProvider"' in panel
    # Neighbours must survive: the local button ADDS a path, not replaces one.
    assert 'data-tofu-action="_showTemplateMenu(this)"' in panel
    assert 'data-tofu-action="addProvider()"' in panel


def test_settings_bundle_registers_section_and_cross_bundle_services():
    bundle = _settings_bundle()
    sections = [row['path'] for row in bundle['sections']]
    assert 'settings/local_deploy.js' in sections
    services = {row['name'] for row in bundle['runtimeServices']}
    # The managed handoff leaves the lazy bundle: new shell + send-button state.
    assert {'newChat', 'updateSendButton'} <= services


def test_preset_order_keeps_custom_last_and_managed_before_it():
    src = _local_deploy_src()
    array_src = src.split('var _LOCAL_DEPLOY_PRESETS = [', 1)[1].split('];', 1)[0]
    engines = re.findall(r"engine: '([^']*)'", array_src)
    assert engines == ['vllm', 'sglang', 'ollama', 'llamacpp', 'managed', '']


def test_managed_handoff_fills_draft_after_new_chat():
    src = _local_deploy_src()
    body = src.split('function _startManagedDeployChat', 1)[1]
    assert body.index('newChat()') < body.index('input.value =')
    assert "t('settings.managedDeployPrompt'" in body
    assert 'updateSendButton()' in body


def test_endpoint_batch_stages_through_provider_render_authority():
    src = _local_deploy_src()
    assert '_stageModelRoutingProviderBundle(bundle, apiKey)' in src
    assert 'Api.providers.probe(url, apiKey,' in src


def test_template_menu_drops_recipe_less_templates_and_wizard_gates_empty():
    src = _read(os.path.join(SECTIONS, 'settings', 'provider_render.js'))
    assert re.search(r'filter\(function\(tpl\) \{\s*return \(tpl\.offering_recipes \|\| \[\]\)\.length > 0;', src)
    assert 'addButton.disabled = count === 0;' in src


@pytest.mark.parametrize('locale', ['zh', 'en'])
def test_locales_carry_local_deploy_keys(locale):
    data = json.loads(_read(os.path.join(
        PROJECT_ROOT, 'frontend', 'src', 'i18n', 'locales', locale + '.json')))
    required = {
        'settings.localDeployEndpointsLabel',
        'settings.localDeployApiKeyLabel',
        'settings.localDeployProbeAdd',
        'settings.localDeployNoUrl',
        'settings.localDeployDuplicate',
        'settings.localDeployNoneOk',
        'settings.localDeployAddedSummary',
        # Reused keys the restored flow depends on.
        'settings.localProvider',
        'settings.localPresetTitle',
        'settings.localPresetManagedName',
        'settings.managedDeployPrompt',
        'settings.epProbingN',
        'settings.epModelsCount',
    }
    missing = required - set(data)
    assert not missing, f'{locale}.json missing: {sorted(missing)}'
