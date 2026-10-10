#!/usr/bin/env python3
"""Toolbar and Settings model lists share one typed vendor-group policy.

The public policy and both real renderers are exercised against two provider
faces of the same YourProvider gateway plus OAuth/adapter credential transports.
Wire protocol and credential kind must never become user-visible vendor
groups, and row icons must agree with their section headings.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
MODEL_GROUP_JS = native_module_path(
    '.native/model-group.js', ROOT / 'frontend/src/core/model-group.ts')
BRAND_DETECTION_JS = native_module_path(
    '.native/model-brand-detection.js',
    ROOT / 'frontend/src/core/model-brand-detection.ts')
TOOLBAR_JS = runtime_section_path('main/main_toolbar_ui.js')
VISIBILITY_JS = native_module_path(
    '.native/settings-visibility-defaults.js',
    ROOT / 'frontend/src/features/settings/visibility-defaults.ts')

_HTML = ('<!DOCTYPE html><body>'
         '<div id="presetDropdownList"></div>'
         '<div id="stgDropdownVisibility"></div></body>')

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2], process.argv[4], process.argv[5]],
  globals: {
    _detectBrand: (s) => /example-corp|yourprovider|longcat|your-corp|your-mascot/i.test(s) ? 'yourprovider'
      : (/claude|anthropic|opus|sonnet|haiku|fable/i.test(s) ? 'claude'
      : (/openai|chatgpt|gpt-/i.test(s) ? 'openai' : 'generic')),
    _brandSvg: (b) => '<i data-b="' + b + '"></i>',
    _modelShortName: (id) => id,
    _modelPricingCache: {},
    isChatModel: () => true,
    _compareModelsByDisplayName: (a, b) => String(a).localeCompare(String(b)),
    _sortModelsByDisplayName: (m) => m,
    _sortModelEntriesByDisplayName: (m) => m,
    _sortedBrandKeys: (g) => Object.keys(g).sort(),
    _warnModelCapsMissing: () => {},
    debugLog: () => {},
    t: (k) => k,
    config: { model: '' }, serverModel: '',
    _hiddenModels: new Set(),
    _registeredModels: [],
    _getAllModels: null,
    _serverConfig: {},
    selectModel: () => {},
  },
});

const modelGroupPolicy = createModelGroupPolicy({
  detectBrand: global._detectBrand,
});
Object.assign(global, modelGroupPolicy);
Object.assign(window, modelGroupPolicy);

const PROVIDERS = [
  { id: 'example-corp', name: 'YourProvider',
    base_url: 'https://api.openai.com/v1', enabled: true,
    models: [{ model_id: 'kimi-k3' }, { model_id: 'deepseek-v3.2' }] },
  { id: 'example_corp_anthropic', name: 'YourProvider (Anthropic native)',
    base_url: 'https://api.openai.com/v1/anthropic', protocol: 'anthropic',
    enabled: true,
    models: [{ model_id: 'claude-opus-4.7' }, { model_id: 'claude-opus-5' }] },
  { id: 'oauth_claude', name: 'Claude (Pro/Max subscription)', brand: 'oauth',
    base_url: 'https://api.anthropic.com/v1', protocol: 'anthropic', enabled: true,
    models: [{ model_id: 'claude-opus-4-1' }] },
  { id: 'oauth_codex', name: 'ChatGPT (Plus subscription)', brand: 'oauth',
    base_url: 'https://chatgpt.com/backend-api/codex', protocol: 'responses',
    enabled: true, models: [{ model_id: 'codex-auto-review' }] },
];

function registeredModels() {
  const out = [];
  for (const provider of PROVIDERS) {
    for (const model of provider.models) {
      out.push({
        model_id: model.model_id,
        brand: provider.brand || '',
        provider_id: provider.id,
        provider_name: provider.name,
        capabilities: ['text'],
      });
    }
  }
  return out;
}

function allModelEntries() {
  const out = [];
  for (const provider of PROVIDERS) {
    for (const model of provider.models) out.push({ model, provider });
  }
  return out;
}

try {
  check('policy_is_immutable', Object.isFrozen(modelGroupPolicy));
  const brandNames = modelGroupBrandNames();
  check('brand_names_are_exposed', brandNames.yourprovider === 'YourProvider');
  brandNames.yourprovider = 'mutated';
  check('brand_names_snapshot_is_defensive',
    modelGroupBrandNames().yourprovider === 'YourProvider');
  check('unknown_brand_uses_provider_label',
    modelGroupLabel('private-vendor', 'Private Vendor') === 'Private Vendor');
  check('empty_label_is_other', modelGroupLabel('', '') === 'Other');
  check('malformed_provider_fails_safe', modelGroupKey(42, null) === 'generic');

  const openaiKey = modelGroupKey(PROVIDERS[0], PROVIDERS[0].models[0]);
  const anthropicKey = modelGroupKey(PROVIDERS[1], PROVIDERS[1].models[0]);
  check('openai_face_key_is_yourprovider', openaiKey === 'yourprovider');
  check('anthropic_face_key_is_yourprovider', anthropicKey === 'yourprovider');
  check('both_faces_share_key', openaiKey === anthropicKey);
  check('yourprovider_label_is_canonical',
    modelGroupLabel('yourprovider', 'x') === 'YourProvider');

  window._populateModelDropdown
    ? window._populateModelDropdown(registeredModels())
    : _populateModelDropdown(registeredModels());
  const dropdown = document.getElementById('presetDropdownList');
  const toolbarLabels = Array.from(
    dropdown.querySelectorAll('.ps-dd-section-label'),
  ).map((node) => node.textContent);
  check('toolbar_has_one_yourprovider_section',
    toolbarLabels.filter((label) => /YourProvider/.test(label)).length === 1);
  check('toolbar_renders_all_models',
    dropdown.querySelectorAll('.preset-dropdown-item').length === 6);

  const rowBrand = (id) => {
    const row = dropdown.querySelector(
      '.preset-dropdown-item[data-value="' + id + '"]',
    );
    const icon = row && row.querySelector('.ps-dd-icon i');
    return icon ? icon.getAttribute('data-b') : null;
  };
  check('oauth_claude_icon_is_claude',
    rowBrand('claude-opus-4-1') === 'claude');
  check('oauth_codex_icon_is_openai',
    rowBrand('codex-auto-review') === 'openai');
  check('credential_kinds_never_become_icons',
    !['oauth', 'adapter', 'generic'].includes(rowBrand('claude-opus-4-1'))
    && !['oauth', 'adapter', 'generic'].includes(rowBrand('codex-auto-review')));

  // The typed visibility owner self-bridges onto featureRegistry (which
  // native_module_path copies onto globalThis) and reads _getAllModels from
  // that same registry. renderDropdownVisibility is module-internal; the
  // production entry that renders #stgDropdownVisibility is the bridged
  // _renderPresetsTab, so drive it exactly as production does.
  featureRegistry._getAllModels = () => allModelEntries();
  featureRegistry._renderPresetsTab();
  const settings = document.getElementById('stgDropdownVisibility');
  const settingsLabels = Array.from(
    settings.querySelectorAll('.stg-dv-brand'),
  ).map((node) => node.textContent);
  check('settings_has_one_yourprovider_section',
    settingsLabels.filter((label) => /YourProvider/.test(label)).length === 1);
  check('settings_renders_all_models',
    settings.querySelectorAll('.stg-dv-item').length === 6);

  const toolbarGroups = {};
  for (const model of registeredModels()) {
    toolbarGroups[model.model_id] = modelGroupKey(
      { brand: model.brand, name: model.provider_name }, model,
    );
  }
  const settingsGroups = {};
  for (const entry of allModelEntries()) {
    settingsGroups[entry.model.model_id] = modelGroupKey(
      entry.provider, entry.model,
    );
  }
  check('toolbar_and_settings_agree_per_model',
    Object.keys(toolbarGroups).every(
      (id) => toolbarGroups[id] === settingsGroups[id],
    ));

  const oauthKey = modelGroupKey(PROVIDERS[2], PROVIDERS[2].models[0]);
  check('oauth_resolves_to_claude', oauthKey === 'claude');
  check('oauth_is_not_a_vendor_group', oauthKey !== 'oauth');

  const adapter = { brand: 'adapter', name: 'Subscription adapter' };
  const adapterKey = modelGroupKey(adapter, { model_id: 'gpt-5.6-sol' });
  check('adapter_resolves_to_openai', adapterKey === 'openai');
  check('adapter_is_not_a_vendor_group', adapterKey !== 'adapter');
} catch (error) {
  check('harness_threw: ' + (error && error.message), false);
} finally {
  report();
}
'''


def test_model_group_convergence():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=MODEL_GROUP_JS,
        body_js=body,
        extra_targets=[TOOLBAR_JS, VISIBILITY_JS],
        expect_pass=22,
        label='model-group-convergence',
    )


_BODY_V2 = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2], process.argv[4], process.argv[5]],
  globals: {
    _detectBrand: () => 'generic',
    _brandSvg: (b) => '<i data-b="' + b + '"></i>',
    _modelShortName: (id) => id,
    _modelPricingCache: {},
    isChatModel: () => true,
    _compareModelsByDisplayName: (a, b) => String(a).localeCompare(String(b)),
    _sortModelsByDisplayName: (m) => m,
    _sortModelEntriesByDisplayName: (m) => m,
    _sortedBrandKeys: (g) => Object.keys(g).sort(),
    _warnModelCapsMissing: () => {},
    debugLog: () => {},
    t: (k) => k,
    config: { model: '' }, serverModel: '',
    _hiddenModels: new Set(),
    _registeredModels: [],
    _getAllModels: null,
    _serverConfig: {},
    selectModel: () => {},
  },
});

try {
  /* Mirror the retained prelude: bridge the shared model-group policy onto
   * the runtime scope so the toolbar picker resolves Creator labels exactly
   * as production. runtimeScope is module-private inside the toolbar section
   * and aliases window, so assign on window. */
  const modelGroupPolicy = createModelGroupPolicy({ detectBrand: global._detectBrand });
  window.modelGroupKey = modelGroupPolicy.modelGroupKey;
  window.modelGroupLabel = modelGroupPolicy.modelGroupLabel;
  window.modelCreatorLabel = modelGroupPolicy.modelCreatorLabel;
  window.modelGroupBrandNames = modelGroupPolicy.modelGroupBrandNames;
  /* Same wiring as the retained prelude: the shared resolver, explicit
   * Creator hint wins. The picker must resolve v2 confirmed rows through
   * this interface, exactly like every Settings model surface. */
  const resolver = createModelBrandResolver({ lookupCreatorId: () => '' });
  global._modelBrand = window._modelBrand = resolver.modelBrand;

  const v2Document = {
    contract_version: 'tofu.model-routing/v2',
    providers: [
      { provider_id: 'p_anthropic', name: 'Anthropic', brand: 'claude' },
      { provider_id: 'p_alibaba', name: 'Alibaba', brand: 'qwen' },
      { provider_id: 'p_openai', name: 'OpenAI', brand: 'openai' },
      { provider_id: 'p_agg', name: 'OpenRouter', brand: 'openrouter' },
    ],
    provider_accesses: [
      { provider_access_id: 'pa_anthropic', provider_id: 'p_anthropic',
        display_name: 'Anthropic', enabled: true },
      { provider_access_id: 'pa_alibaba', provider_id: 'p_alibaba',
        display_name: 'Alibaba', enabled: true },
      { provider_access_id: 'pa_openai', provider_id: 'p_openai',
        display_name: 'OpenAI', enabled: true },
      { provider_access_id: 'pa_agg', provider_id: 'p_agg',
        display_name: 'OpenRouter', enabled: true },
    ],
    creators: [
      { creator_id: 'anthropic', name: 'Anthropic' },
      { creator_id: 'alibaba', name: 'Alibaba' },
      { creator_id: 'openai', name: 'OpenAI' },
    ],
    models: [
      { creator_id: 'anthropic', model_id: 'claude-opus-5' },
      { creator_id: 'alibaba', model_id: 'qwen3-max' },
      { creator_id: 'openai', model_id: 'gpt-5.6-sol' },
    ],
    deployments: [
      { offering_id: 'of_1', enabled: true },
      { offering_id: 'of_2', enabled: true },
      { offering_id: 'of_3', enabled: true },
      { offering_id: 'of_4', enabled: true },
    ],
    offerings: [
      { offering_id: 'of_1', provider_access_id: 'pa_anthropic',
        enabled: true, stale: false, identity_state: 'confirmed',
        model: { creator_id: 'anthropic', model_id: 'claude-opus-5' },
        capabilities: ['text'] },
      { offering_id: 'of_2', provider_access_id: 'pa_alibaba',
        enabled: true, stale: false, identity_state: 'confirmed',
        model: { creator_id: 'alibaba', model_id: 'qwen3-max' },
        capabilities: ['text'] },
      { offering_id: 'of_3', provider_access_id: 'pa_openai',
        enabled: true, stale: false, identity_state: 'confirmed',
        model: { creator_id: 'openai', model_id: 'gpt-5.6-sol' },
        capabilities: ['text'] },
      { offering_id: 'of_4', provider_access_id: 'pa_agg',
        enabled: true, stale: false, identity_state: 'pending',
        pending_model_id: 'mystery-model-1', capabilities: ['text'] },
    ],
  };

  const rows = _modelRoutingDropdownModels(v2Document);
  check('v2_projects_three_confirmed_plus_one_pending', rows.length === 4);
  (window._populateModelDropdown || _populateModelDropdown)(rows);
  const dropdown = document.getElementById('presetDropdownList');
  const rowBrand = (id) => {
    const row = dropdown.querySelector(
      '.preset-dropdown-item[data-value="' + id + '"]',
    );
    const icon = row && row.querySelector('.ps-dd-icon i');
    return icon ? icon.getAttribute('data-b') : null;
  };
  /* Creator ids are NOT glyph keys: 'anthropic'/'alibaba' have no
   * MODEL_BRAND_ICONS entry. Before the fix every non-openai confirmed row
   * rendered the generic box while sitting under its vendor section. */
  check('v2_anthropic_row_icon_is_claude',
    rowBrand('claude-opus-5') === 'claude');
  check('v2_alibaba_row_icon_is_qwen', rowBrand('qwen3-max') === 'qwen');
  check('v2_openai_row_icon_stays_openai',
    rowBrand('gpt-5.6-sol') === 'openai');
  check('v2_pending_row_keeps_provider_glyph',
    rowBrand('mystery-model-1') === 'openrouter');
  check('creator_namespace_never_lands_as_glyph',
    !['anthropic', 'alibaba', 'generic'].includes(rowBrand('claude-opus-5'))
    && !['anthropic', 'alibaba', 'generic'].includes(rowBrand('qwen3-max')));
} catch (error) {
  check('harness_threw: ' + (error && error.message), false);
} finally {
  report();
}
'''


def test_v2_picker_rows_resolve_creator_glyph():
    """Preset-picker v2 rows share the Settings Creator → glyph resolution.

    A confirmed v2 row carries its Creator id in ``m.brand``; rendering that
    string verbatim as an icon key only resolves for creators whose id
    coincides with a glyph key ('openai'). The row builder must route those
    rows through the shared modelBrand interface instead.
    """
    body = _BODY_V2.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=BRAND_DETECTION_JS,
        body_js=body,
        extra_targets=[MODEL_GROUP_JS, TOOLBAR_JS],
        expect_pass=6,
        label='v2-picker-creator-glyph',
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
