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

ROOT = Path(__file__).resolve().parents[1]
MODEL_GROUP_JS = native_module_path(
    '.native/model-group.js', ROOT / 'frontend/src/core/model-group.ts')
TOOLBAR_JS = runtime_section_path('main/main_toolbar_ui.js')
VISIBILITY_JS = runtime_section_path('settings/visibility_defaults.js')

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

  global._getAllModels = window._getAllModels = () => allModelEntries();
  _renderDropdownVisibility();
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


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
