#!/usr/bin/env python3
"""Provider-preference chip renders a styled popover, not a native select.

The v2 picker's per-row provider switcher used to be a <select> whose OS
popup ignored page styling entirely. It is now a chip button plus a
body-anchored menu: opening, toggling, picking a provider (routing with
identity), outside-click dismissal and Escape all run against the retained
toolbar section in jsdom.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
BRAND_DETECTION_JS = native_module_path(
    '.native/model-brand-detection.js',
    ROOT / 'frontend/src/core/model-brand-detection.ts')
TOOLBAR_JS = runtime_section_path('main/main_toolbar_ui.js')

_HTML = '<!DOCTYPE html><body><div id="presetDropdownList"></div></body>'

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2], process.argv[4]],
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
    /* Externals of the section's own selectModelRoute: stubbed so the real
     * routing function runs to completion and mutates the config global,
     * which is what these checks observe. */
    _applyModelUI: () => {},
    commitComposerModel: () => {},
    getActiveConv: () => null,
    captureComposerModel: () => ({}),
    captureActiveConversationSettings: () => {},
    _configForPersist: () => ({}),
    /* The materialized section bundle declares `var runtimeScope = window`,
     * so runtimeScope-seam reads resolve off the jsdom WINDOW — the
     * model-group policy API this flow hits must be a top-level global
     * (owned by _prelude.js in the real runtime). */
    modelCreatorLabel: (creatorId, creatorName, modelId) =>
      creatorName || creatorId || modelId || 'generic',
  },
});

try {
  const resolver = createModelBrandResolver({ lookupCreatorId: () => '' });
  global._modelBrand = window._modelBrand = resolver.modelBrand;

  const v2Document = {
    contract_version: 'tofu.model-routing/v2',
    providers: [
      { provider_id: 'p_anthropic', name: 'Anthropic', brand: 'claude' },
      { provider_id: 'p_agg', name: 'OpenRouter', brand: 'openrouter' },
    ],
    provider_accesses: [
      { provider_access_id: 'pa_anthropic', provider_id: 'p_anthropic',
        display_name: 'Anthropic', enabled: true },
      { provider_access_id: 'pa_agg', provider_id: 'p_agg',
        display_name: 'OpenRouter', enabled: true },
    ],
    creators: [{ creator_id: 'anthropic', name: 'Anthropic' }],
    models: [{ creator_id: 'anthropic', model_id: 'claude-opus-5' }],
    deployments: [
      { offering_id: 'of_1', enabled: true },
      { offering_id: 'of_2', enabled: true },
    ],
    offerings: [
      { offering_id: 'of_1', provider_access_id: 'pa_anthropic',
        enabled: true, stale: false, identity_state: 'confirmed',
        model: { creator_id: 'anthropic', model_id: 'claude-opus-5' },
        capabilities: ['text'] },
      { offering_id: 'of_2', provider_access_id: 'pa_agg',
        enabled: true, stale: false, identity_state: 'confirmed',
        model: { creator_id: 'anthropic', model_id: 'claude-opus-5' },
        capabilities: ['text'] },
    ],
  };

  const rows = _modelRoutingDropdownModels(v2Document);
  (window._populateModelDropdown || _populateModelDropdown)(rows);
  const dropdown = document.getElementById('presetDropdownList');
  const row = dropdown.querySelector(
    '.preset-dropdown-item[data-value="claude-opus-5"]');

  const chip = row && row.querySelector('.ps-dd-provider-chip');
  check('chip_rendered_no_native_select',
    !!chip && !row.querySelector('select'));
  check('chip_label_is_auto_when_nothing_selected',
    !!chip && chip.querySelector('.ps-dd-provider-chip-label')
      .textContent === '自动服务商');

  chip.click();
  const menu = document.getElementById('psProviderMenu');
  check('menu_opens_on_chip_click',
    !!menu && menu.getAttribute('role') === 'listbox'
      && chip.getAttribute('aria-expanded') === 'true');
  const items = menu ? Array.from(
    menu.querySelectorAll('.ps-dd-provider-menu-item')) : [];
  check('menu_lists_auto_plus_all_providers', items.length === 3);
  check('auto_option_active_with_checkmark',
    items.length === 3
      && items[0].classList.contains('active')
      && items[0].getAttribute('aria-selected') === 'true'
      && !!items[0].querySelector('.ps-dd-provider-check svg'));

  chip.click();
  check('second_chip_click_toggles_closed',
    !document.getElementById('psProviderMenu')
      && chip.getAttribute('aria-expanded') === 'false');

  chip.click();
  const reopened = document.getElementById('psProviderMenu');
  reopened.querySelectorAll('.ps-dd-provider-menu-item')[1].click();
  check('provider_pick_routes_with_identity',
    config.preferredProviderId === 'p_anthropic'
      && config.routing.preferred_provider_id === 'p_anthropic'
      && config.modelRef.model_id === 'claude-opus-5');
  check('menu_closes_after_pick', !document.getElementById('psProviderMenu'));

  chip.click();
  document.body.click();
  check('outside_click_closes_without_reroute',
    !document.getElementById('psProviderMenu')
      && config.preferredProviderId === 'p_anthropic');

  chip.click();
  document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
  check('escape_closes_and_restores_chip_focus',
    !document.getElementById('psProviderMenu')
      && document.activeElement === chip);
} catch (error) {
  check('harness_threw: ' + (error && error.message), false);
} finally {
  report();
}
'''


def test_provider_preference_chip_popover():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=BRAND_DETECTION_JS,
        body_js=body,
        extra_targets=[TOOLBAR_JS],
        expect_pass=10,
        label='provider-preference-chip',
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
