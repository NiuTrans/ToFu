#!/usr/bin/env python3
"""Frontend test — the toolbar picker and the Settings preset tab group the
SAME models under the SAME rule, and that rule is brand (never the provider
wire protocol).

WHY
---
The toolbar model dropdown grouped by ``provider_id`` / ``provider_name``.
When Claude moved to the Anthropic-native face (``example-corp_anthropic``,
2026-07-28) — same gateway, same API keys, only a different wire protocol —
the picker split into TWO "YourProvider" sections. That is a backend
implementation detail (which protocol a socket speaks) leaking straight into
the user's model list. The Settings preset tab grouped by brand and never
split. Two lists of the SAME data must never disagree about grouping.

This suite drives the REAL functions (``modelGroupKey`` from
core/model_group.js, plus the toolbar ``_populateModelDropdown`` and the
preset ``_renderDropdownVisibility``) against a two-face provider and
asserts every model lands in ONE brand group.

WHAT IS GUARDED (results, not implementation)
------------------------------------------------------------------
  * Both YourProvider faces (openai + anthropic protocol) fold into ONE
    'yourprovider' group; the dropdown shows a single "YourProvider" section header.
  * The preset tab groups the same models under the same single brand key.
  * An oauth-branded subscription provider (brand='oauth') resolves to the
    model's REAL vendor group (claude), never a meaningless "oauth" section.
  * modelGroupLabel maps the key to the human name ('yourprovider' → 'YourProvider').

NEUTERS (source-level, on mutated copies — shipped files untouched):
  * N1: group by provider_id again (the original leak) → two YourProvider
        sections reappear (red).
  * N2: oauth falls through to the literal 'oauth' brand → an "oauth"
        section appears instead of Claude (red).
"""

from __future__ import annotations

import json
import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

MODEL_GROUP_JS = os.path.join(JS_DIR, 'core', 'model_group.js')
TOOLBAR_JS = os.path.join(JS_DIR, 'main', 'main_toolbar_ui.js')
VISIBILITY_JS = os.path.join(JS_DIR, 'settings', 'visibility_defaults.js')

_HTML = ('<!DOCTYPE html><body>'
         '<div id="presetDropdownList"></div>'
         '<div id="stgDropdownVisibility"></div></body>')

_BODY = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2], process.argv[4], process.argv[5]],
  globals: {
    // Real brand detection is what folds the two faces together.
    _detectBrand: (s) => /example-corp|yourprovider|longcat|your-corp|your-mascot/i.test(s) ? 'yourprovider'
                     : (/claude|anthropic|opus|sonnet|haiku|fable/i.test(s) ? 'claude' : 'generic'),
    _brandSvg: () => '',
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
    _getAllModels: null,          // seeded below
    _serverConfig: {},
    selectModel: () => {},
  },
});
const indirectEval = eval;
const MG_SRC = fs.readFileSync(process.argv[2], 'utf8');
const TB_SRC = fs.readFileSync(process.argv[4], 'utf8');

/* Bridge window-published model_group into node global scope (browser parity). */
global.modelGroupKey = window.modelGroupKey;
global.modelGroupLabel = window.modelGroupLabel;
global.modelGroupBrandNames = window.modelGroupBrandNames;

/* Two YourProvider faces — same gateway + keys, two protocols. The bug shape. */
const PROVIDERS = [
  { id: 'example-corp', name: 'YourProvider', base_url: 'https://api.openai.com/v1',
    protocol: '', enabled: true,
    models: [ { model_id: 'kimi-k3' }, { model_id: 'deepseek-v3.2' } ] },
  { id: 'example-corp_anthropic', name: 'YourProvider (Anthropic native)',
    base_url: 'https://api.openai.com/v1/anthropic', protocol: 'anthropic', enabled: true,
    models: [ { model_id: 'claude-opus-4.7' }, { model_id: 'claude-opus-5' } ] },
  { id: 'oauth_claude', name: 'Claude (Pro/Max subscription)', brand: 'oauth',
    base_url: 'https://api.anthropic.com/v1', protocol: 'anthropic', enabled: true,
    models: [ { model_id: 'claude-opus-4-1' } ] },
];

/* The registered-models shape the toolbar consumes. */
function regModels() {
  const out = [];
  for (const p of PROVIDERS) {
    for (const m of p.models) {
      out.push({ model_id: m.model_id, brand: p.brand || '',
                 provider_id: p.id, provider_name: p.name,
                 capabilities: ['text'] });
    }
  }
  return out;
}

/* The {model, provider} shape the preset tab consumes. */
function allModelEntries() {
  const out = [];
  for (const p of PROVIDERS) {
    for (const m of p.models) out.push({ model: m, provider: p });
  }
  return out;
}

try {
  // ══ 1. modelGroupKey folds both YourProvider faces into ONE brand key ══
  {
    const kOpenai = modelGroupKey(PROVIDERS[0], PROVIDERS[0].models[0]);
    const kAnth = modelGroupKey(PROVIDERS[1], PROVIDERS[1].models[0]);
    check('openai_face_key_is_yourprovider', kOpenai === 'yourprovider');
    check('anthropic_face_key_is_yourprovider', kAnth === 'yourprovider');
    check('both_faces_same_key', kOpenai === kAnth);
    check('label_yourprovider', modelGroupLabel('yourprovider', 'x') === 'YourProvider');
  }

  // ══ 2. Toolbar dropdown renders ONE "YourProvider" section (not two) ══
  {
    window._populateModelDropdown ? window._populateModelDropdown(regModels())
                                  : _populateModelDropdown(regModels());
    const dd = document.getElementById('presetDropdownList');
    const labels = Array.from(dd.querySelectorAll('.ps-dd-section-label'))
      .map((d) => d.textContent);
    const yourproviderCount = labels.filter((s) => /YourProvider/.test(s)).length;
    check('toolbar_one_yourprovider_section', yourproviderCount === 1);
    // All five models present.
    const items = dd.querySelectorAll('.preset-dropdown-item');
    check('toolbar_all_models_rendered', items.length === 5);
  }

  // ══ 3. Preset tab groups the same models under the same single brand ══
  {
    global._getAllModels = window._getAllModels = () => allModelEntries();
    _renderDropdownVisibility();
    const cont = document.getElementById('stgDropdownVisibility');
    const brands = Array.from(cont.querySelectorAll('.stg-dv-brand'))
      .map((d) => d.textContent);
    const yourproviderCount = brands.filter((s) => /YourProvider/.test(s)).length;
    check('preset_one_yourprovider_section', yourproviderCount === 1);
    const items = cont.querySelectorAll('.stg-dv-item');
    check('preset_all_models_rendered', items.length === 5);
  }

  // ══ 4. Toolbar and preset agree on the group of every model ══
  {
    // For each model, the toolbar group key == the preset group key.
    const tbGrouped = {};
    for (const m of regModels()) {
      const k = modelGroupKey({ brand: m.brand, name: m.provider_name }, m);
      tbGrouped[m.model_id] = k;
    }
    const prGrouped = {};
    for (const e of allModelEntries()) {
      prGrouped[e.model.model_id] = modelGroupKey(e.provider, e.model);
    }
    let agree = true;
    for (const id of Object.keys(tbGrouped)) {
      if (tbGrouped[id] !== prGrouped[id]) agree = false;
    }
    check('toolbar_and_preset_agree_per_model', agree);
  }

  // ══ 5. oauth subscription resolves to the REAL vendor group ══
  {
    const k = modelGroupKey(PROVIDERS[2], PROVIDERS[2].models[0]);
    check('oauth_resolves_to_claude', k === 'claude');
    check('oauth_never_literal_oauth', k !== 'oauth');
  }

  // ══ NEUTER 1: group by provider_id again (the original leak) ══
  {
    const n = TB_SRC.replace(
      "const gkey = _hasGroup\n      ? modelGroupKey(_entryProvider, m)\n      : (m.provider_id || 'default');",
      "const gkey = (m.provider_id || 'default');");
    check('N1_applied', n !== TB_SRC);
    indirectEval(n);
    window._populateModelDropdown ? window._populateModelDropdown(regModels())
                                  : _populateModelDropdown(regModels());
    const dd = document.getElementById('presetDropdownList');
    const labels = Array.from(dd.querySelectorAll('.ps-dd-section-label'))
      .map((d) => d.textContent);
    const yourproviderCount = labels.filter((s) => /YourProvider/.test(s)).length;
    check('N1_two_yourprovider_sections_return', yourproviderCount === 2);
    indirectEval(TB_SRC);   // restore
  }

  // ══ NEUTER 2: oauth falls through to the literal 'oauth' brand ══
  {
    const n = MG_SRC.replace("brand && brand !== 'oauth'", 'brand');
    check('N2_applied', n !== MG_SRC);
    indirectEval(n);
    const k = window.modelGroupKey(PROVIDERS[2], PROVIDERS[2].models[0]);
    check('N2_oauth_group_appears', k === 'oauth');
    indirectEval(MG_SRC);   // restore
  }
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
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
        min_pass=15,
        label='model-group-convergence',
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
