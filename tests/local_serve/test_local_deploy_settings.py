"""Owner pins for the 本地部署 (local deployment) settings flow.

The dedicated local-deployment button was dropped during the model-routing v2
rewrite (2026-09 regression): the template menu silently swallowed the
recipe-less ``local`` placeholder template, and the managed model-path deploy
handoff disappeared with it. These pins lock the restored contract:

- api.html wires ``addLocalProvider()`` next to 从模板添加/自定义服务商;
- the typed settings feature module ``features/settings.ts`` imports the
  typed owner ``features/settings/local-deploy.ts``, and the
  settings-presenters lazy bundle injects the cross-bundle
  ``newChat``/``updateSendButton`` services the handoff reaches through the
  bridge;
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

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SECTIONS = os.path.join(PROJECT_ROOT, 'frontend', 'src', 'runtime', 'sections')
FEATURES = os.path.join(PROJECT_ROOT, 'frontend', 'src', 'features')
LOCAL_DEPLOY = os.path.join(FEATURES, 'settings', 'local-deploy.ts')
SETTINGS_FEATURE = os.path.join(FEATURES, 'settings.ts')
PROVIDER_TEMPLATE_PICKER = os.path.join(
    FEATURES, 'settings', 'provider-template-picker.ts')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _local_deploy_src():
    return _read(LOCAL_DEPLOY)


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
    # The section retired to a typed feature module (frontend migration):
    # features/settings.ts is the entry chunk that pulls the local-deploy
    # owner into the settings lazy graph.
    feature_entry = _read(SETTINGS_FEATURE)
    assert "import './settings/local-deploy';" in feature_entry
    bundle = _settings_bundle()
    services = {row['name'] for row in bundle['runtimeServices']}
    # The managed handoff leaves the lazy bundle: new shell + send-button state.
    assert {'newChat', 'updateSendButton'} <= services


def test_preset_order_keeps_custom_last_and_managed_before_it():
    src = _local_deploy_src()
    array_src = src.split('const LOCAL_DEPLOY_PRESETS: LocalDeployPreset[] = [', 1)[1].split('];', 1)[0]
    engines = re.findall(r"engine: '([^']*)'", array_src)
    assert engines == ['vllm', 'sglang', 'ollama', 'llamacpp', 'managed', '']


def test_preset_chooser_renders_grouped_card_tiles():
    src = _local_deploy_src()
    # The chooser modal gets its own scope class so the compact provider
    # template dropdown keeps its flat styling.
    assert "'stg-modal stg-tpl-wizard stg-local-deploy'" in src
    # Tiles are cards: icon tile + info + chevron affordance.
    assert 'stg-template-item stg-local-preset' in src
    assert 'stg-local-preset-icon' in src
    assert 'stg-local-preset-arrow' in src
    # Visual grouping: engines vs managed/custom, driven by i18n labels.
    assert 'data-group="engines"' in src
    assert 'data-group="other"' in src
    assert "t('settings.localPresetGroupEngines')" in src
    assert "t('settings.localPresetGroupOther')" in src


def test_page_renders_smart_sections_and_agent_handoffs():
    src = _local_deploy_src()
    # Live sections: hardware strip, ranked recommendations, managed ledger.
    assert 'data-ld="hardware"' in src
    assert 'data-ld="recs"' in src
    assert 'data-ld="instances"' in src
    assert 'Api?.localServe?.hardware()' in src
    assert 'Api?.localServe?.recommendations(' in src
    assert 'Api?.localServe?.deployments()' in src
    # Recommendation cards hand the deploy off to the chat agent; the cloud
    # escape hatch adds the DeepSeek template directly (no wizard).
    assert "t('settings.localRecDeployPrompt'" in src
    assert '_addTemplateProvider?.(' in src
    # Failed/stopped ledger rows hand diagnosis & restart to the agent.
    assert "t('settings.localRepairPrompt'" in src
    assert "t('settings.localStartPrompt'" in src
    # Purpose filter chips drive the recommendations query.
    assert 'const LD_PURPOSES' in src


def test_managed_handoff_fills_draft_after_new_chat():
    src = _local_deploy_src()
    body = src.split('function startManagedDeployChat', 1)[1]
    assert body.index('bridge.newChat?.()') < body.index('input.value =')
    assert "t('settings.managedDeployPrompt'" in body
    assert 'bridge.updateSendButton?.()' in body


def test_endpoint_batch_stages_through_provider_render_authority():
    src = _local_deploy_src()
    assert 'bridge._stageModelRoutingProviderBundle?.(bundle, apiKey)' in src
    assert 'bridge.Api?.providers?.probe(url, apiKey,' in src


def test_template_menu_drops_recipe_less_templates_and_adds_directly():
    # The template menu retired to the typed provider-template-picker owner.
    src = _read(PROVIDER_TEMPLATE_PICKER)
    assert re.search(
        r'return !!template\.key && \(\(template\.offering_recipes\?\.length \|\| 0\) > 0 \|\|',
        src)
    assert '.filter(selectableTemplate);' in src
    # No secondary wizard: a template click compiles the full recipe set and
    # stages it straight into the editable provider card.
    assert '_openTemplateWizard' not in src
    assert 'else { close(); ports.select(row.key); }' in src


def test_template_stage_strips_compile_only_fields_and_gates_placeholder_credentials():
    src = _read(os.path.join(SECTIONS, 'settings', 'provider_render.js'))
    # credential_extra_headers is CompiledProviderBundle-only; echoing it in
    # ProviderWriteRequest trips the contract (undeclared field).
    assert 'delete draft.credential_extra_headers;' in src
    # Keyless add: the v2 domain rejects a non-local credential without a
    # secret_reference, so the recipe's placeholder credential must be
    # dropped; the card's 「+ 添加密钥」 mints the real one.
    assert re.search(
        r"draft\.credentials = \(draft\.credentials \|\| \[\]\)\.filter\(function\(row\) \{\s*"
        r"return row\.kind === 'local_identity';", src)


def test_template_item_resets_button_ua_leak_and_grid_may_shrink():
    # The provider-template items are <button> elements: without an explicit
    # reset the UA stylesheet leaks a gray buttonface tile, centered text and
    # the wrong font into the menu (reported 2026-09-04 as 「样式好丑」).
    css = _read(os.path.join(
        PROJECT_ROOT, 'frontend', 'src', 'styles', 'application',
        '06-file-changes-settings-shell.css'))
    item_block = css.split('.stg-template-item {', 1)[1].split('}', 1)[0]
    for prop in ('background: transparent', 'text-align: left',
                 'font-family: inherit', 'color: inherit'):
        assert prop in item_block
    # Brand marks stay bare — an icon background box was rejected by the owner.
    assert '.stg-template-menu .stg-template-item .stg-brand-icon {' not in css
    # The menu sizes to its two full columns (its containing block is only the
    # header button row), and the grid tracks may shrink below the nowrap
    # names' min-content — otherwise the second column is clipped.
    menu_block = css.split('.stg-template-menu {', 1)[1].split('}', 1)[0]
    assert 'width: max-content' in menu_block
    grid_block = css.split('.stg-template-grid {', 1)[1].split('}', 1)[0]
    assert 'repeat(2, minmax(0, 1fr))' in grid_block


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
        'settings.localPresetGroupEngines',
        'settings.localPresetGroupOther',
        'settings.managedDeployPrompt',
        'settings.epProbingN',
        'settings.epModelsCount',
        # Redesigned page: smart sections + agent handoffs.
        'settings.localRecGroupTitle',
        'settings.localRecPurposeAll',
        'settings.localRecPurposeCoding',
        'settings.localRecLoading',
        'settings.localRecError',
        'settings.localRecEmpty',
        'settings.localRecDeploy',
        'settings.localRecFitComfortable',
        'settings.localRecFitCpu',
        'settings.localRecSub',
        'settings.localRecDeployPrompt',
        'settings.localHwVram',
        'settings.localHwNoGpu',
        'settings.localHwRam',
        'settings.localHwDisk',
        'settings.localCloudTitle',
        'settings.localCloudPrice',
        'settings.localCloudAdd',
        'settings.localManagedGroup',
        'settings.localManagedEmpty',
        'settings.localManagedRepair',
        'settings.localManagedStart',
        'settings.localRepairPrompt',
        'settings.localStartPrompt',
        'settings.localStatusRunning',
        'settings.localStatusFailed',
    }
    missing = required - set(data)
    assert not missing, f'{locale}.json missing: {sorted(missing)}'
