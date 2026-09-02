#!/usr/bin/env python3
"""Frontend test — the toolbar picker and the Settings preset tab group the
SAME models under the SAME rule, and that rule is brand (never the provider
wire protocol).

WHY
---
The toolbar model dropdown grouped by ``provider_id`` / ``provider_name``.
When Claude moved to the Anthropic-native face (``example_corp_anthropic``,
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
  * oauth/adapter-branded subscription providers resolve to the model's REAL
    vendor group, never a meaningless credential-plumbing section.
  * modelGroupLabel maps the key to the human name ('yourprovider' → 'YourProvider').
  * A row's brand ICON follows the same rule: 'oauth'/'adapter' is a
    credential kind with no _BRAND_ICONS entry — rendering it literally gave
    every ChatGPT-subscription GPT a grey generic box UNDER an "OPENAI"
    section header (owner screenshot 2026-08-10). The row must resolve the
    real vendor via the same modelGroupKey inputs the grouping pass used.

NEUTERS (source-level, on mutated copies — shipped files untouched):
  * N1: group by provider_id again (the original leak) → two YourProvider
        sections reappear (red).
  * N2: oauth falls through to the literal 'oauth' brand → an "oauth"
        section appears instead of Claude (red).
  * N3: row icon uses raw m.brand again → subscription rows render the
        literal 'oauth' credential kind (grey generic box) (red).
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
                     : (/claude|anthropic|opus|sonnet|haiku|fable/i.test(s) ? 'claude'
                     : (/openai|chatgpt|gpt-/i.test(s) ? 'openai' : 'generic')),
    // Echo the resolved brand so row-icon assertions can read it back.
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
  { id: 'example_corp_anthropic', name: 'YourProvider (Anthropic native)',
    base_url: 'https://api.openai.com/v1/anthropic', protocol: 'anthropic', enabled: true,
    models: [ { model_id: 'claude-opus-4.7' }, { model_id: 'claude-opus-5' } ] },
  { id: 'oauth_claude', name: 'Claude (Pro/Max subscription)', brand: 'oauth',
    base_url: 'https://api.anthropic.com/v1', protocol: 'anthropic', enabled: true,
    models: [ { model_id: 'claude-opus-4-1' } ] },
  // The owner-screenshot shape (2026-08-10): ChatGPT subscription, bare
  // codex-* wire id — the vendor signal lives in the provider NAME only.
  { id: 'oauth_codex', name: 'ChatGPT (Plus subscription)', brand: 'oauth',
    base_url: 'https://chatgpt.com/backend-api/codex', protocol: 'responses', enabled: true,
    models: [ { model_id: 'codex-auto-review' } ] },
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
    // All six models present.
    const items = dd.querySelectorAll('.preset-dropdown-item');
    check('toolbar_all_models_rendered', items.length === 6);
  }

  // ══ 2b. Row brand ICONS resolve the real vendor for subscription rows ══
  //    (the 2026-08-10 bug: literal 'oauth' → grey generic box under OPENAI) ══
  {
    const dd = document.getElementById('presetDropdownList');
    const rowBrand = (id) => {
      const r = dd.querySelector('.preset-dropdown-item[data-value="' + id + '"]');
      const ic = r && r.querySelector('.ps-dd-icon i');
      return ic ? ic.getAttribute('data-b') : null;
    };
    check('rowicon_oauth_claude_is_claude', rowBrand('claude-opus-4-1') === 'claude');
    check('rowicon_oauth_codex_is_openai', rowBrand('codex-auto-review') === 'openai');
    check('rowicon_subscription_never_credential_kind',
      rowBrand('claude-opus-4-1') !== 'oauth' && rowBrand('codex-auto-review') !== 'oauth'
      && rowBrand('claude-opus-4-1') !== 'generic' && rowBrand('codex-auto-review') !== 'generic');
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
    check('preset_all_models_rendered', items.length === 6);
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

  // ══ 6. adapter is also plumbing, not a vendor ══
  {
    const p = { brand: 'adapter', name: 'Subscription adapter',
                models: [{ model_id: 'gpt-5.6-sol' }] };
    const k = modelGroupKey(p, p.models[0]);
    check('adapter_resolves_to_openai', k === 'openai');
    check('adapter_never_literal_adapter', k !== 'adapter');
  }

  // ══ NEUTER 1: group by provider_id again (the original leak) ══
  {
    const n = TB_SRC.replace(
      "const gkey = _hasGroup\n      ? runtimeScope.modelGroupKey(_entryProvider, m)\n      : (m.provider_id || 'default');",
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

  // ══ NEUTER 3: row icon renders raw m.brand (the 2026-08-10 grey box) ══
  {
    const fixed = "    const _rowBrand = (m.brand || '').trim();\n"
      + "    const _rowCredKind = (_rowBrand === 'oauth' || _rowBrand === 'adapter');\n"
      + "    const brand = (_rowBrand && !_rowCredKind)\n"
      + "      ? _rowBrand\n"
      + "      : (_rowCredKind && _hasGroup)\n"
      + "        ? runtimeScope.modelGroupKey({ brand: m.brand, name: m.provider_name }, m)\n"
      + "        : (typeof _detectBrand === 'function' ? _detectBrand(m.model_id) : 'generic');";
    const n = TB_SRC.replace(fixed,
      "    const brand = m.brand || (typeof _detectBrand === 'function' ? _detectBrand(m.model_id) : 'generic');");
    check('N3_applied', n !== TB_SRC);
    indirectEval(n);
    window._populateModelDropdown ? window._populateModelDropdown(regModels())
                                  : _populateModelDropdown(regModels());
    const dd = document.getElementById('presetDropdownList');
    const r = dd.querySelector('.preset-dropdown-item[data-value="codex-auto-review"]');
    const ic = r && r.querySelector('.ps-dd-icon i');
    check('N3_row_renders_literal_oauth', ic && ic.getAttribute('data-b') === 'oauth');
    indirectEval(TB_SRC);   // restore
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
        min_pass=22,
        label='model-group-convergence',
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
