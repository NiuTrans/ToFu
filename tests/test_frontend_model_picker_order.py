#!/usr/bin/env python3
"""Frontend test — the model picker must be ordered by the name the user READS.

WHY
---
``_populateModelDropdown`` (retained ``runtime/sections/main/main_toolbar_ui.js``) had no sort at
all: it rendered ``dropdown_models`` in array order, which is provider order in
``data/config/server_config.json``. That array happens to be ordered by
``model_id`` (the Settings cold sort writes it back that way), but the ROW shows
``_modelShortName(model_id)`` — a *different* string:

    model_id                       label
    ─────────────────────────────  ────────────────
    yuju-claude-opus-5-evaDaily    Claude Opus 5     ← sorted under 'y'
    aws.claude-opus-4.6            Claude Opus 4.6   ← 'aws.' prefix stripped
    hy3-preview                    Hunyuan HY3 Preview
    claude-fable-5                 Fable 5

So the picker looked unsorted. Provider SECTIONS were unordered too —
``Object.keys(grouped)`` is first-appearance order, unrelated to either id or
name.

WHAT IS GUARDED (results, not implementation — charter 2026-07-27)
-----------------------------------------------------------------
  1. Rendered ``.ps-dd-label`` sequence within a section is display-name
     ordered.
  2. Section headers (``.ps-dd-section-label``) are display-name ordered.
  3. Version numbers compare NUMERICALLY: "Gemini 3.5" before "Gemini 3.6",
     and (the case plain string compare gets wrong) "3.9" before "3.10".
  4. The comparator survives a ``_modelPricingCache`` MISS — models with no
     pricing entry (``oauth_claude``'s dated ids) sort by their stripped id
     instead of throwing, so the degraded order is stable rather than arbitrary.
  5. The v2-backed preset and default-model lists use that same policy rather
     than inheriting Provider/Offering insertion order.

The comparator is exercised through its public typed factory. Retained DOM
owners are composed against that policy exactly as the runtime prelude does.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re

import pytest

from tests._jsdom import JS_DIR, ROOT, run_harness
from tests._runtime_sections import native_module_graph, runtime_section_names

pytestmark = pytest.mark.unit

TOOLBAR_JS = os.path.join(JS_DIR, 'main', 'main_toolbar_ui.js')
CORE_PANEL_JS = os.path.join(JS_DIR, 'settings', 'core_panel.js')
VISIBILITY_JS = os.path.join(JS_DIR, 'settings', 'visibility_defaults.js')
MODEL_DISPLAY_OWNER = Path(ROOT) / 'frontend/src/core/model-display-names.ts'
PRELUDE = Path(ROOT) / 'frontend/src/runtime/sections/_prelude.js'
MODEL_PRESENTATION_JS = native_module_graph([
    ('.native/model-brand-detection-for-picker-order.js',
     Path(ROOT) / 'frontend/src/core/model-brand-detection.ts'),
    ('.native/model-brand-icons-for-picker-order.js',
     Path(ROOT) / 'frontend/src/core/model-brand-icons.ts'),
    ('.native/model-display-names-for-picker-order.js', MODEL_DISPLAY_OWNER),
    ('.native/model-group-for-picker-order.js',
     Path(ROOT) / 'frontend/src/core/model-group.ts'),
])

# Markup mirroring the shipped index.html dropdown (inner list + depth footer).
_HTML = (
    '<!DOCTYPE html><body>'
    '<div class="preset-dropdown" id="presetDropdown">'
    '<div class="preset-dropdown-list" id="presetDropdownList"></div>'
    '<div id="thinkingDepthSection"></div>'
    '</div></body>'
)

_BODY = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[4]],  // typed presentation owners
  globals: {
    BASE_PATH: '',
    config: { model: 'kimi-k3' },
    serverModel: 'kimi-k3',
    _registeredModels: [],
    _registeredModelsLoaded: false,
    _hiddenModels: new Set(),
    selectModel: function () {},
    isChatModel: function () { return true; },
    _warnModelCapsMissing: function () {},
    // Real production pricing-name subset (routes/config.py surfaces
    // MODEL_PRICING[*].name as model_pricing → _modelPricingCache).
    _modelPricingCache: {
      'aws.claude-opus-4.6': { name: 'Claude Opus 4.6' },
      'aws.claude-opus-4.8': { name: 'Claude Opus 4.8' },
      'yuju-claude-opus-5-evaDaily': { name: 'Claude Opus 5' },
      'claude-fable-5': { name: 'Fable 5' },
      'hy3-preview': { name: 'Hunyuan HY3 Preview' },
      'kimi-k3': { name: 'Kimi K3' },
      'gemini-3.5-flash': { name: 'Gemini 3.5 Flash' },
      'gemini-3.6-flash': { name: 'Gemini 3.6 Flash' },
    },
  },
});

const TOOLBAR_SRC = fs.readFileSync(process.argv[2], 'utf8');

/* Slice one named function body out of a source file (brace matching). */
function sliceFn(src, signature) {
  const start = src.indexOf(signature);
  if (start < 0) throw new Error('signature not found: ' + signature);
  let i = src.indexOf('{', start), depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) return src.slice(start, j + 1);
  }
  throw new Error('unbalanced: ' + signature);
}

const POPULATE_SIG = 'function _populateModelDropdown(models) {';
const POPULATE = sliceFn(TOOLBAR_SRC, POPULATE_SIG);
const indirectEval = eval;

/* Mirror the prelude's explicit typed-owner composition. Lookups stay live so
 * the cache-miss branch below exercises the production contract. */
const displayNames = createModelDisplayNames({
  lookupModelDisplayName: (modelId) =>
    global._modelPricingCache && global._modelPricingCache[modelId]
      ? global._modelPricingCache[modelId].name : '',
  lookupProviderDisplayName: () => '',
});
const compatibility = {
  _detectBrand: detectModelBrand,
  _brandSvg: brandIconHtml,
  _modelShortName: displayNames.modelShortName,
  _compareModelIds: displayNames.compareModelIds,
  _compareModelsByDisplayName: displayNames.compareModelsByDisplayName,
  _sortModelsByDisplayName: displayNames.sortModelsByDisplayName,
  _sortModelEntriesByDisplayName: displayNames.sortModelEntriesByDisplayName,
  _sortedBrandKeys: displayNames.sortedBrandKeys,
};
Object.assign(global, compatibility);
Object.assign(window, compatibility);
const modelGroupPolicy = createModelGroupPolicy({ detectBrand: detectModelBrand });
Object.assign(global, modelGroupPolicy);
Object.assign(window, modelGroupPolicy);
global.runtimeScope = window.runtimeScope = {
  isChatModel: global.isChatModel,
  ...modelGroupPolicy,
};

/* Two brand groups, deliberately given in the WORST section order for the
 * brand-grouping rule: the 'meituan' group is inserted FIRST, but 'Claude'
 * must sort before 'Meituan' — so the section order must be FIXED by the
 * sort, not inherited. Models within each group are in model_id order (what
 * the config file holds) which is NOT display-name order.
 *
 * The picker groups by brand through the typed core/model-group policy,
 * not provider_id, so the section key is the detected brand: the two
 * dated-id models detect as 'claude', the rest as 'meituan'.
 *
 * The fixture pins `brand` EXPLICITLY rather than relying on _detectBrand
 * matching the provider NAME: the opensource export (export.py rule 11/15)
 * historically rewrote the internal org's retained detect pattern
 * ('meituan' → 'yourprovider') while the typed label table and this
 * file ship verbatim — so a name-detection fixture groups 'Meituan' models
 * by their model_ids in the public tree (claude/gemini/kimi sections) and
 * every section assertion goes red there. An explicit brand is also the
 * PRODUCTION shape (server_config providers carry brand), so nothing is
 * lost: the oauth pair still exercises the detect fall-through (brand
 * 'oauth' → _detectBrand on the model_id → 'claude'). */
const MODELS = [
  { model_id: 'aws.claude-opus-4.6', provider_id: 'sankuai', provider_name: 'Meituan', brand: 'meituan', capabilities: ['text'] },
  { model_id: 'aws.claude-opus-4.8', provider_id: 'sankuai', provider_name: 'Meituan', brand: 'meituan', capabilities: ['text'] },
  { model_id: 'claude-fable-5', provider_id: 'sankuai', provider_name: 'Meituan', brand: 'meituan', capabilities: ['text'] },
  { model_id: 'gemini-3.5-flash', provider_id: 'sankuai', provider_name: 'Meituan', brand: 'meituan', capabilities: ['text'] },
  { model_id: 'gemini-3.6-flash', provider_id: 'sankuai', provider_name: 'Meituan', brand: 'meituan', capabilities: ['text'] },
  { model_id: 'hy3-preview', provider_id: 'sankuai', provider_name: 'Meituan', brand: 'meituan', capabilities: ['text'] },
  { model_id: 'kimi-k3', provider_id: 'sankuai', provider_name: 'Meituan', brand: 'meituan', capabilities: ['text'] },
  { model_id: 'yuju-claude-opus-5-evaDaily', provider_id: 'sankuai', provider_name: 'Meituan', brand: 'meituan', capabilities: ['text'] },
  { model_id: 'claude-opus-4-1-20250805', provider_id: 'oauth_claude', provider_name: 'Zzz Subscription', brand: 'oauth', capabilities: ['text'] },
  { model_id: 'claude-sonnet-4-5-20250929', provider_id: 'oauth_claude', provider_name: 'Zzz Subscription', brand: 'oauth', capabilities: ['text'] },
];

function labels() {
  return Array.from(document.querySelectorAll('#presetDropdownList .ps-dd-label'))
    .map((el) => el.textContent);
}
function sections() {
  return Array.from(document.querySelectorAll('#presetDropdownList .ps-dd-section-label'))
    .map((el) => el.textContent);
}
function reset() { document.getElementById('presetDropdownList').innerHTML = ''; }

try {
  // ══ 1. Shipped behaviour: models ordered by DISPLAY name ══
  indirectEval(POPULATE);
  reset();
  _populateModelDropdown(MODELS.slice());
  const L = labels();
  check('all_models_rendered', L.length === 10);

  // The Meituan section, in display-name order. This is the payload assertion:
  // "Claude Opus 5" must sit with the other Claudes even though its model_id
  // (yuju-…) sorts last, and 3.5 must precede 3.6.
  const wantMeituan = [
    'Claude Opus 4.6', 'Claude Opus 4.8', 'Claude Opus 5',
    'Fable 5', 'Gemini 3.5 Flash', 'Gemini 3.6 Flash',
    'Hunyuan HY3 Preview', 'Kimi K3',
  ];
  const gotMeituan = L.filter((x) => wantMeituan.indexOf(x) >= 0);
  check('models_in_display_name_order',
    gotMeituan.join('|') === wantMeituan.join('|'));
  check('opus5_sits_with_claudes',
    gotMeituan.indexOf('Claude Opus 5') === 2);

  // ══ 2. Section headers ordered by brand-group display name ══
  // Typed brand grouping: the dated oauth models detect as
  // 'claude' → 'Claude'; the sankuai models as 'meituan' → 'Meituan'. Even
  // though 'meituan' was inserted first, Claude must sort before it.
  const S = sections();
  check('two_sections_rendered', S.length === 2);
  check('sections_in_name_order', S.join('|') === 'Claude|Meituan');

  // ══ 3. Cache MISS models don't throw and sort by stripped id ══
  // oauth_claude's dated ids have no _modelPricingCache entry → the label IS
  // the raw id, and they must still be ordered.
  const dated = L.filter((x) => x.indexOf('2025') >= 0);
  check('cache_miss_models_rendered', dated.length === 2);
  check('cache_miss_models_ordered',
    dated.join('|') === 'claude-opus-4-1-20250805|claude-sonnet-4-5-20250929');

  // ══ 4. Numeric collation: 3.9 before 3.10 (plain string compare fails) ══
  check('numeric_version_collation',
    _compareModelsByDisplayName('m-3.9', 'm-3.10') < 0);
  check('numeric_two_digit_minor',
    _compareModelsByDisplayName('gpt-5.10', 'gpt-5.6') > 0);

  // ══ 4b. Separator weight must NOT outrank content ══
  // The collator sorts a space BEFORE a hyphen, so a friendly (spaced) label
  // would beat every raw (hyphenated) id sharing its prefix: 'Gemini 3.6 Flash'
  // landed before 'gemini-3.1-flash-lite-preview' and 'MiniMax M3' before
  // 'MiniMax-M2.5'. Only models WITH a MODEL_PRICING entry get a spaced label,
  // so both spellings interleave in every real list.
  check('separator_does_not_outrank_content',
    _compareModelsByDisplayName('Gemini 3.6 Flash', 'gemini-3.1-flash-lite-preview') > 0);
  check('separator_minimax_case',
    _compareModelsByDisplayName('MiniMax M3', 'MiniMax-M2.5') > 0);
  // Folding is sort-key-only and must not break numeric compare.
  check('separator_fold_keeps_numeric',
    _compareModelsByDisplayName('Gemini 3.5 Flash', 'Gemini 3.5 Flash-Lite') < 0);

  // ══ 5. Comparator is total + reflexive on mixed/degenerate input ══
  check('comparator_reflexive', _compareModelsByDisplayName('kimi-k3', 'kimi-k3') === 0);
  check('comparator_handles_entries',
    _compareModelsByDisplayName({ model_id: 'kimi-k3' }, { model_id: 'aws.claude-opus-4.6' }) > 0);
  check('comparator_handles_empty',
    typeof _compareModelsByDisplayName('', 'kimi-k3') === 'number');
  const providerNames = createModelDisplayNames({
    lookupModelDisplayName: () => '',
    lookupProviderDisplayName: (id) => id === 'p1' ? 'Provider One' : '',
  });
  check('provider_name_uses_injected_catalog',
    providerNames.providerDisplayName('p1') === 'Provider One');
  const failingProviderNames = createModelDisplayNames({
    lookupModelDisplayName: () => '',
    lookupProviderDisplayName: () => { throw new Error('catalog unavailable'); },
  });
  check('provider_name_lookup_fails_open',
    failingProviderNames.providerDisplayName('p1') === 'p1');

  // ══ 6. Sort survives a _modelPricingCache MISS on EVERY model ══
  // (the .catch fallback in _loadServerConfigAndPopulate + a settings-close
  //  repaint after a failed config load hit exactly this state)
  const savedCache = global._modelPricingCache;
  global._modelPricingCache = window._modelPricingCache = undefined;
  reset();
  let threw = false;
  try { _populateModelDropdown(MODELS.slice()); } catch (e) { threw = true; }
  check('no_throw_without_pricing_cache', threw === false);
  const bare = labels().filter((x) => x.indexOf('claude-opus-4.6') >= 0
                                   || x.indexOf('claude-fable-5') >= 0);
  // aws. prefix still stripped (same rule _modelShortName uses on a miss),
  // and the list is still ordered rather than arbitrary. NOTE the relative
  // order differs from the cached case — cacheless keys on the stripped id, so
  // 'claude-fable-5' < 'claude-opus-4.6'. That is the documented degradation:
  // stable and near-alphabetical, not identical to the labelled order.
  check('cacheless_strips_gateway_prefix', bare.indexOf('claude-opus-4.6') >= 0);
  check('cacheless_still_ordered',
    bare.join('|') === 'claude-fable-5|claude-opus-4.6');
  global._modelPricingCache = window._modelPricingCache = savedCache;

  // ══ 7. An authoritative empty owner model list clears boot placeholders ══
  _populateModelDropdown([]);
  check('empty_authority_clears_rows', labels().length === 0);
  check('empty_authority_clears_registry', _registeredModels.length === 0);

} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
} finally {
  report();
}
'''


def test_model_picker_ordered_by_display_name():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=TOOLBAR_JS,
        body_js=body,
        extra_targets=[MODEL_PRESENTATION_JS],
        expect_pass=22,
        label='model-picker-order',
    )



# ══════════════════════════════════════════════════════
#  Settings → Preset tab: the same three lists must be ordered
# ══════════════════════════════════════════════════════
#
# The Preset tab (index.html data-tab="preset", static/settings_panels/preset.html)
# renders THREE model-name lists from visibility_defaults.js. None of them sorted:
# they inherited whatever order _getAllModels() walked the routing Offerings in
# while the row text is _modelShortName. Models WITH a MODEL_PRICING entry
# therefore looked right by luck and models WITHOUT one were scattered.

_PRESET_HTML = (
    '<!DOCTYPE html><body>'
    '<div id="stgIgVisibility"></div>'
    '<div id="stgDropdownVisibility"></div>'
    '<select id="settingFallbackModel"></select>'
    '<select id="settingDefaultModel"></select>'
    '</body>'
)

_PRESET_BODY = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2]],
  globals: {
    BASE_PATH: '',
    _serverConfig: { hidden_models: [], hidden_ig_models: [] },
    isChatModel: function (m) {
      const EX = ['image_gen', 'embedding', 'transcription'];
      return !(m.capabilities || []).some((c) => EX.indexOf(c) >= 0);
    },
    _warnModelCapsMissing: function () {},
    _modelPricingCache: {
      'gemini-3.5-flash': { name: 'Gemini 3.5 Flash' },
      'gemini-3.6-flash': { name: 'Gemini 3.6 Flash' },
      'MiniMax-M3': { name: 'MiniMax M3' },
      'yuju-claude-opus-5-evaDaily': { name: 'Claude Opus 5' },
      'gpt-image-2': { name: 'GPT Image 2' },
    },
  },
});

const displayNames = createModelDisplayNames({
  lookupModelDisplayName: (modelId) =>
    global._modelPricingCache && global._modelPricingCache[modelId]
      ? global._modelPricingCache[modelId].name : '',
  lookupProviderDisplayName: () => '',
});
const compatibility = {
  _detectBrand: detectModelBrand,
  _brandSvg: brandIconHtml,
  _modelShortName: displayNames.modelShortName,
  _compareModelIds: displayNames.compareModelIds,
  _compareModelsByDisplayName: displayNames.compareModelsByDisplayName,
  _sortModelsByDisplayName: displayNames.sortModelsByDisplayName,
  _sortModelEntriesByDisplayName: displayNames.sortModelEntriesByDisplayName,
  _sortedBrandKeys: displayNames.sortedBrandKeys,
};
Object.assign(global, compatibility);
Object.assign(window, compatibility);
const modelGroupPolicy = createModelGroupPolicy({ detectBrand: detectModelBrand });
Object.assign(global, modelGroupPolicy);
Object.assign(window, modelGroupPolicy);
global.runtimeScope = window.runtimeScope = {
  isChatModel: global.isChatModel,
  ...modelGroupPolicy,
};

const CORE_SRC = fs.readFileSync(process.argv[4], 'utf8');
const VIS_SRC = fs.readFileSync(process.argv[5], 'utf8');
const indirectEval = eval;
indirectEval(CORE_SRC.match(/function _getAllModels[\s\S]*?\n}/)[0]);

/* Two v2 provider-access bundles in the WORST section order (Alpha inserted
 * last), each
 * with models whose model_id order differs from their label order, and a
 * deliberate mix of priced (spaced label) and unpriced (hyphenated raw id)
 * entries — the interleaving case the separator fold exists for. */
function seedModelRouting() {
  const bundles = [
    { provider_id: 'zzz', name: 'Zzz Provider', brand: 'zzzbrand', models: [
      ['gemini-3.5-flash', ['text']],
      ['gemini-3.6-flash', ['text']],
      ['gemini-3.1-flash-lite-preview', ['text']],
      ['MiniMax-M3', ['text']],
      ['MiniMax-M2.5', ['text']],
      ['yuju-claude-opus-5-evaDaily', ['text']],
      ['gpt-image-2', ['image_gen']],
      ['gpt-image-1.5', ['image_gen']],
    ] },
    { provider_id: 'alpha', name: 'Alpha Provider', brand: 'alphabrand', models: [
      ['kimi-k3', ['text']],
    ] },
  ];
  const routing = {
    providers: [], provider_accesses: [], models: [], offerings: [], deployments: [],
  };
  bundles.forEach((bundle) => {
    const accessId = bundle.provider_id + '-access';
    routing.providers.push({
      provider_id: bundle.provider_id, name: bundle.name, brand: bundle.brand,
    });
    routing.provider_accesses.push({
      provider_access_id: accessId,
      provider_id: bundle.provider_id,
      display_name: bundle.name,
      enabled: true,
    });
    bundle.models.forEach(([modelId, capabilities], modelIndex) => {
      const offeringId = bundle.provider_id + '-offering-' + modelIndex;
      routing.models.push({ creator_id: 'fixture', model_id: modelId });
      routing.offerings.push({
        offering_id: offeringId,
        provider_access_id: accessId,
        model: { creator_id: 'fixture', model_id: modelId },
        capabilities,
        enabled: true,
        stale: false,
      });
      routing.deployments.push({
        deployment_id: offeringId + '-deployment',
        offering_id: offeringId,
        enabled: true,
      });
    });
  });
  global._stgModelRouting = window._stgModelRouting = routing;
}

function dvNames(containerId) {
  return Array.from(document.querySelectorAll('#' + containerId + ' .stg-dv-name'))
    .map((el) => el.textContent);
}
function brandHeadings(containerId) {
  // _brandSvg emits <span class="stg-brand-icon"> as the FIRST child, so the
  // label is the LAST element child — not `.stg-dv-brand span`.
  return Array.from(document.querySelectorAll('#' + containerId + ' .stg-dv-brand'))
    .map((el) => el.lastElementChild.textContent);
}
function optionTexts(id) {
  return Array.from(document.querySelectorAll('#' + id + ' option'))
    .map((o) => o.textContent).slice(1);   // drop the "" placeholder
}
function isSorted(list) {
  for (let i = 1; i < list.length; i++) {
    if (_compareModelsByDisplayName(list[i - 1], list[i]) > 0) return false;
  }
  return true;
}

try {
  indirectEval(VIS_SRC);
  seedModelRouting();
  _renderPresetsTab({ model_defaults: {} });

  // ══ 1. Chat-model visibility list ══
  // 7 chat models (the 2 image_gen ones are filtered out by isChatModel).
  // The list is GROUPED, so it is sorted WITHIN each brand section, not
  // globally — 'kimi-k3' leads because Alpha Provider sorts before Zzz.
  const dv = dvNames('stgDropdownVisibility');
  check('dv_rendered', dv.length === 7);
  const dvZzz = dv.slice(1);   // the Zzz Provider section
  check('dv_display_name_ordered', isSorted(dvZzz));
  // The two payload cases: an unpriced raw id must interleave with the priced
  // spaced labels rather than being flushed to the end of the cluster.
  check('dv_unpriced_id_interleaves',
    dv.indexOf('gemini-3.1-flash-lite-preview') < dv.indexOf('Gemini 3.5 Flash'));
  check('dv_minimax_numeric_interleave',
    dv.indexOf('MiniMax-M2.5') < dv.indexOf('MiniMax M3'));
  // 'Claude Opus 5' (model_id yuju-…, which sorts LAST by id) must lead its
  // own section by label.
  check('dv_opus5_sits_with_claude', dvZzz[0] === 'Claude Opus 5');

  // ══ 2. Brand/provider group headings ordered ══
  const heads = brandHeadings('stgDropdownVisibility');
  check('dv_two_groups', heads.length === 2);
  check('dv_group_headings_ordered', heads.join('|') === 'Alpha Provider|Zzz Provider');

  // ══ 3. Image-gen visibility list ══
  const ig = dvNames('stgIgVisibility');
  check('ig_rendered', ig.length === 2);
  check('ig_display_name_ordered', isSorted(ig));
  check('ig_priced_label_used', ig.indexOf('GPT Image 2') >= 0);

  // ══ 4. Both <select>s ordered, and identically ══
  // Unlike the visibility list these are NOT grouped — one flat list, so it is
  // globally sorted. Same model SET, different presentation.
  const fb = optionTexts('settingFallbackModel');
  const df = optionTexts('settingDefaultModel');
  check('select_rendered', fb.length === 7);
  check('select_display_name_ordered', isSorted(fb));
  check('selects_agree_with_each_other', fb.join('|') === df.join('|'));
  check('select_covers_same_models_as_visibility',
    fb.slice().sort().join('|') === dv.slice().sort().join('|'));
  // 'kimi-k3' sorts into the middle here (between Gemini and MiniMax) whereas
  // the grouped list puts it first — proving the flat list really is sorted by
  // label and not just inheriting the grouped order.
  check('select_is_globally_not_group_ordered',
    fb.indexOf('kimi-k3') > 0 && fb.indexOf('kimi-k3') < fb.length - 1);

} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
} finally {
  indirectEval(VIS_SRC);
  report();
}
'''


def test_preset_tab_lists_ordered_by_display_name():
    body = _PRESET_BODY.replace('HTML_PLACEHOLDER', json.dumps(_PRESET_HTML))
    run_harness(
        target_js=MODEL_PRESENTATION_JS,
        body_js=body,
        extra_targets=[CORE_PANEL_JS, VISIBILITY_JS],
        expect_pass=15,
        label='preset-tab-order',
    )

# ══════════════════════════════════════════════════════
#  Static guard — ONE comparator, no duplicate sort logic
# ══════════════════════════════════════════════════════

def test_single_comparator_no_duplicate_sort_logic():
    """The typed comparator must live in exactly one source owner.

    A second hand-rolled model comparator anywhere else is how the picker and
    the Settings list drift apart again. Lists that render FRIENDLY names must
    call the shared ``_compareModelsByDisplayName`` (directly or via its thin
    ``_sortModels*``/``_sortedBrandKeys`` wrappers). No retained consumer may
    construct a collator of its own.
    """
    owner = MODEL_DISPLAY_OWNER.read_text()
    toolbar = open(TOOLBAR_JS, encoding='utf-8').read()
    core = open(CORE_PANEL_JS, encoding='utf-8').read()
    vis = open(VISIBILITY_JS, encoding='utf-8').read()

    assert 'createModelDisplayNames' in owner
    assert owner.count('new Intl.Collator') == 1, \
        'typed owner must construct exactly one shared collator'
    assert 'numeric: true' in owner, \
        'collator must be numeric-aware or two-digit minor versions mis-sort'

    consumers = (
        ('main_toolbar_ui.js', toolbar, ['_compareModelsByDisplayName']),
        ('core_panel.js', core, []),
        ('visibility_defaults.js', vis,
         ['_sortModelsByDisplayName', '_sortModelEntriesByDisplayName',
          '_sortedBrandKeys']),
    )
    for name, src, required in consumers:
        assert 'function _compareModelsByDisplayName' not in src, \
            f'{name} must NOT define its own copy of the comparator'
        assert 'Intl.Collator' not in src, \
            f'{name} must NOT build its own collator (share typed owner)'
        for sym in required:
            assert sym in src, \
                f'{name} must route its sort through the shared {sym}'

    # No consumer may walk a brand/provider group map in insertion order —
    # that was the section-order half of the bug.
    assert 'for (var brand in grouped)' not in vis, \
        'visibility_defaults.js still iterates brand groups in insertion order'

def test_prelude_composes_typed_owner_before_retained_consumers():
    order = runtime_section_names()
    assert 'settings/branding.js' not in order
    for name in ('settings/core_panel.js', 'main/main_toolbar_ui.js'):
        assert order.count(name) == 1, f'{name} missing from the Vite runtime'
    prelude = PRELUDE.read_text()
    assert "from '../core/model-display-names'" in prelude
    assert 'compareModelIds: _compareModelIds' in prelude
    assert 'compareModelsByDisplayName: _compareModelsByDisplayName' in prelude


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
