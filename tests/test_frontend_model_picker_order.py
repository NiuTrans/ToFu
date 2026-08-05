#!/usr/bin/env python3
"""Frontend test — the model picker must be ordered by the name the user READS.

WHY
---
``_populateModelDropdown`` (static/js/main/main_toolbar_ui.js) had no sort at
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
  5. The Settings provider model list sorts by the RAW model_id — the string
     ``_renderModelCard`` renders — NOT the friendly pricing name. (2026-08-04
     incident: ``claude-fable-5`` is named "Fable 5" in lib/pricing/_tables.py,
     and a display-name sort parked the card between Doubao and gemini —
     alphabetical to the machine, scrambled to the reader. The friendly-name
     comparator remains correct for lists that RENDER friendly names: this
     picker, the preset tab, the default-model selects.)

NEUTERS (source-level, on mutated copies — shipped files untouched):
  * strip the model sort out of _populateModelDropdown  → order goes red
  * strip the provider-group sort                       → section order red
  * drop `numeric: true` from the collator              → 3.9-vs-3.10 red
"""

from __future__ import annotations

import json
import os
import re

import pytest

from tests._jsdom import JS_DIR, ROOT, run_harness

pytestmark = pytest.mark.unit

TOOLBAR_JS = os.path.join(JS_DIR, 'main', 'main_toolbar_ui.js')
BRANDING_JS = os.path.join(JS_DIR, 'settings', 'branding.js')
CORE_PANEL_JS = os.path.join(JS_DIR, 'settings', 'core_panel.js')
VISIBILITY_JS = os.path.join(JS_DIR, 'settings', 'visibility_defaults.js')
MODEL_GROUP_JS = os.path.join(JS_DIR, 'core', 'model_group.js')

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
  targets: [process.argv[4], process.argv[6]],  // branding.js + model_group.js
  globals: {
    BASE_PATH: '',
    config: { model: 'kimi-k3' },
    serverModel: 'kimi-k3',
    _registeredModels: [],
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
const CORE_SRC = fs.readFileSync(process.argv[5], 'utf8');

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

/* model_group.js (argv[6]) publishes on window; the picker's bare-global
 * `typeof modelGroupKey` guard resolves only via node global scope here. */
global.modelGroupKey = window.modelGroupKey;
global.modelGroupLabel = window.modelGroupLabel;
global.modelGroupBrandNames = window.modelGroupBrandNames;

/* Two brand groups, deliberately given in the WORST section order for the
 * brand-grouping rule: the 'meituan' group is inserted FIRST, but 'Claude'
 * must sort before 'Meituan' — so the section order must be FIXED by the
 * sort, not inherited. Models within each group are in model_id order (what
 * the config file holds) which is NOT display-name order.
 *
 * (2026-07-28, pt_464f2baf) The picker now groups by BRAND (core/model_group)
 * not provider_id, so the section key is the detected brand: the two
 * dated-id models detect as 'claude', the rest as 'meituan'. */
const MODELS = [
  { model_id: 'aws.claude-opus-4.6', provider_id: 'sankuai', provider_name: 'Meituan', capabilities: ['text'] },
  { model_id: 'aws.claude-opus-4.8', provider_id: 'sankuai', provider_name: 'Meituan', capabilities: ['text'] },
  { model_id: 'claude-fable-5', provider_id: 'sankuai', provider_name: 'Meituan', capabilities: ['text'] },
  { model_id: 'gemini-3.5-flash', provider_id: 'sankuai', provider_name: 'Meituan', capabilities: ['text'] },
  { model_id: 'gemini-3.6-flash', provider_id: 'sankuai', provider_name: 'Meituan', capabilities: ['text'] },
  { model_id: 'hy3-preview', provider_id: 'sankuai', provider_name: 'Meituan', capabilities: ['text'] },
  { model_id: 'kimi-k3', provider_id: 'sankuai', provider_name: 'Meituan', capabilities: ['text'] },
  { model_id: 'yuju-claude-opus-5-evaDaily', provider_id: 'sankuai', provider_name: 'Meituan', capabilities: ['text'] },
  { model_id: 'claude-opus-4-1-20250805', provider_id: 'oauth_claude', provider_name: 'Zzz Subscription', capabilities: ['text'] },
  { model_id: 'claude-sonnet-4-5-20250929', provider_id: 'oauth_claude', provider_name: 'Zzz Subscription', capabilities: ['text'] },
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
  // Brand grouping (core/model_group): the dated oauth models detect as
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

  // ══ 6. Settings cold sort orders by the RAW model_id the card shows ══
  // The settings card renders `m.model_id` verbatim, so the sort key must be
  // that same string — not _modelShortName. The pricing cache below names
  // claude-fable-5 "Fable 5" and yuju-claude-opus-5-evaDaily "Claude Opus 5";
  // under the old (buggy) contract those friendly names moved the cards to
  // 'f'/'c' positions the reader cannot see.
  indirectEval(CORE_SRC.match(/function _compareModelEntries[\s\S]*?\n}/)[0]);
  indirectEval(CORE_SRC.match(/function _coldSortModels[\s\S]*?\n}/)[0]);
  const settingsList = MODELS.filter((m) => m.provider_id === 'sankuai')
    .map((m) => ({ model_id: m.model_id }));
  _coldSortModels(settingsList);
  const settingsIds = settingsList.map((m) => m.model_id);
  const wantIds = [
    'aws.claude-opus-4.6', 'aws.claude-opus-4.8', 'claude-fable-5',
    'gemini-3.5-flash', 'gemini-3.6-flash', 'hy3-preview', 'kimi-k3',
    'yuju-claude-opus-5-evaDaily',
  ];
  check('settings_cold_sort_by_shown_model_id',
    settingsIds.join('|') === wantIds.join('|'));
  // The 2026-08-04 incident pins: 'Fable 5' (pricing name) must NOT pull the
  // card to 'f' — it sorts under 'c', right after the other claude ids; and
  // the yuju- id sorts under 'y', where the reader sees it.
  check('settings_pricing_name_does_not_move_card',
    settingsIds.indexOf('claude-fable-5') === 2);
  check('settings_gateway_id_sorts_where_shown',
    settingsIds.indexOf('yuju-claude-opus-5-evaDaily') === wantIds.length - 1);

  // ══ 7. Sort survives a _modelPricingCache MISS on EVERY model ══
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

  // ══ NEUTER 1: remove the per-group model sort → order regresses ══
  {
    const n = POPULATE.replace(
      'if (_canSort) group.models.sort(_compareModelsByDisplayName);', '');
    check('N1_applied', n !== POPULATE);
    indirectEval(n);
    reset();
    _populateModelDropdown(MODELS.slice());
    const got = labels().filter((x) => wantMeituan.indexOf(x) >= 0);
    check('N1_model_order_regresses', got.join('|') !== wantMeituan.join('|'));
  }

  // ══ NEUTER 2: remove the brand-group sort → section order regresses ══
  {
    const n = POPULATE.replace(/  if \(_canSort\) \{\n    groupKeys\.sort[\s\S]*?\n  \}\n/,
                               '');
    check('N2_applied', n !== POPULATE);
    indirectEval(n);
    reset();
    _populateModelDropdown(MODELS.slice());
    check('N2_section_order_regresses',
      sections().join('|') === 'Meituan|Claude');
  }

  // ══ NEUTER 3: drop numeric collation → 3.9 vs 3.10 goes wrong ══
  {
    const BRAND_SRC = fs.readFileSync(process.argv[4], 'utf8');
    const n = BRAND_SRC.replace('{ numeric: true, sensitivity: \'base\' }',
                                '{ sensitivity: \'base\' }');
    check('N3_applied', n !== BRAND_SRC);
    indirectEval(n);
    check('N3_numeric_collation_regresses',
      _compareModelsByDisplayName('m-3.9', 'm-3.10') > 0);
  }

  // ══ NEUTER 4: drop the separator fold → spaced labels jump the queue ══
  {
    const BRAND_SRC = fs.readFileSync(process.argv[4], 'utf8');
    const n = BRAND_SRC.replace(".replace(/[-_\\/]+/g, ' ')", '');
    check('N4_applied', n !== BRAND_SRC);
    indirectEval(n);
    check('N4_separator_fold_regresses',
      _compareModelsByDisplayName('MiniMax M3', 'MiniMax-M2.5') < 0);
    indirectEval(BRAND_SRC);
  }
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
} finally {
  // Re-eval the pristine sources so a neuter never leaks to another test.
  indirectEval(POPULATE);
  report();
}
'''


def test_model_picker_ordered_by_display_name():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=TOOLBAR_JS,
        body_js=body,
        extra_targets=[BRANDING_JS, CORE_PANEL_JS, MODEL_GROUP_JS],
        min_pass=22,
        label='model-picker-order',
    )



# ══════════════════════════════════════════════════════
#  Settings → Preset tab: the same three lists must be ordered
# ══════════════════════════════════════════════════════
#
# The Preset tab (index.html data-tab="preset", static/settings_panels/preset.html)
# renders THREE model-name lists from visibility_defaults.js. None of them sorted:
# they inherited whatever order _getAllModels() walked the provider arrays in,
# i.e. model_id order (the settings cold sort writes that back) while the row
# text is _modelShortName. Models WITH a MODEL_PRICING entry therefore looked
# right by luck and models WITHOUT one were scattered.

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
  targets: [process.argv[2]],          // branding.js — comparator + wrappers
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

const CORE_SRC = fs.readFileSync(process.argv[4], 'utf8');
const VIS_SRC = fs.readFileSync(process.argv[5], 'utf8');
const indirectEval = eval;
indirectEval(CORE_SRC.match(/function _getAllModels[\s\S]*?\n}/)[0]);

/* Two providers given in the WORST section order (Alpha inserted last), each
 * with models whose model_id order differs from their label order, and a
 * deliberate mix of priced (spaced label) and unpriced (hyphenated raw id)
 * entries — the interleaving case the separator fold exists for. */
function seedProviders() {
  global._stgProviders = window._stgProviders = [
    { id: 'zzz', name: 'Zzz Provider', brand: 'zzzbrand', enabled: true, models: [
      { model_id: 'gemini-3.5-flash', capabilities: ['text'] },
      { model_id: 'gemini-3.6-flash', capabilities: ['text'] },
      { model_id: 'gemini-3.1-flash-lite-preview', capabilities: ['text'] },
      { model_id: 'MiniMax-M3', capabilities: ['text'] },
      { model_id: 'MiniMax-M2.5', capabilities: ['text'] },
      { model_id: 'yuju-claude-opus-5-evaDaily', capabilities: ['text'] },
      { model_id: 'gpt-image-2', capabilities: ['image_gen'] },
      { model_id: 'gpt-image-1.5', capabilities: ['image_gen'] },
    ] },
    { id: 'alpha', name: 'Alpha Provider', brand: 'alphabrand', enabled: true, models: [
      { model_id: 'kimi-k3', capabilities: ['text'] },
    ] },
  ];
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
  seedProviders();
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

  // ══ NEUTER P1: drop the model sort in _renderDropdownVisibility ══
  {
    const n = VIS_SRC.replace(/    _sortModelsByDisplayName\(group\.models\);\n/g, '');
    check('NP1_applied', n !== VIS_SRC);
    indirectEval(n);
    seedProviders();
    _renderPresetsTab({ model_defaults: {} });
    check('NP1_dv_order_regresses',
      isSorted(dvNames('stgDropdownVisibility').slice(1)) === false);
  }

  // ══ NEUTER P2: drop the brand-group sort → insertion order returns ══
  {
    const n = VIS_SRC.replace(
      /  var brandKeys = _sortedBrandKeys\(grouped, brandNames\);\n/g,
      '  var brandKeys = Object.keys(grouped);\n');
    check('NP2_applied', n !== VIS_SRC);
    indirectEval(n);
    seedProviders();
    _renderPresetsTab({ model_defaults: {} });
    check('NP2_group_order_regresses',
      brandHeadings('stgDropdownVisibility').join('|') === 'Zzz Provider|Alpha Provider');
  }

  // ══ NEUTER P3: drop the <select> sort ══
  {
    const n = VIS_SRC.replace('  _sortModelEntriesByDisplayName(uniqueModels);\n', '');
    check('NP3_applied', n !== VIS_SRC);
    indirectEval(n);
    seedProviders();
    _renderPresetsTab({ model_defaults: {} });
    check('NP3_select_order_regresses',
      isSorted(optionTexts('settingFallbackModel')) === false);
  }
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
        target_js=BRANDING_JS,
        body_js=body,
        extra_targets=[CORE_PANEL_JS, VISIBILITY_JS],
        min_pass=20,
        label='preset-tab-order',
    )

# ══════════════════════════════════════════════════════
#  Static guard — ONE comparator, no duplicate sort logic
# ══════════════════════════════════════════════════════

def test_single_comparator_no_duplicate_sort_logic():
    """The comparator must live in exactly one place.

    A second hand-rolled model comparator anywhere else is how the picker and
    the Settings list drift apart again. Lists that render FRIENDLY names must
    call the shared ``_compareModelsByDisplayName`` (directly or via its thin
    ``_sortModels*``/``_sortedBrandKeys`` wrappers); the Settings card list
    renders raw model_ids and must order them through the same shared
    ``_MODEL_NAME_COLLATOR``. None may re-implement a `<`/`>` compare or build
    a collator of its own.
    """
    brand = open(BRANDING_JS, encoding='utf-8').read()
    toolbar = open(TOOLBAR_JS, encoding='utf-8').read()
    core = open(CORE_PANEL_JS, encoding='utf-8').read()
    vis = open(VISIBILITY_JS, encoding='utf-8').read()

    assert 'function _compareModelsByDisplayName' in brand, \
        'the shared comparator must be defined in settings/branding.js'
    assert brand.count('function _compareModelsByDisplayName') == 1, \
        'comparator defined more than once'
    assert 'numeric: true' in brand, \
        'collator must be numeric-aware or two-digit minor versions mis-sort'

    consumers = (
        ('main_toolbar_ui.js', toolbar, ['_compareModelsByDisplayName']),
        # The settings card list sorts by the raw model_id it renders — via
        # the shared collator, NOT the friendly-name comparator (2026-08-04).
        ('core_panel.js', core, ['_MODEL_NAME_COLLATOR']),
        ('visibility_defaults.js', vis,
         ['_sortModelsByDisplayName', '_sortModelEntriesByDisplayName',
          '_sortedBrandKeys']),
    )
    for name, src, required in consumers:
        assert 'function _compareModelsByDisplayName' not in src, \
            f'{name} must NOT define its own copy of the comparator'
        assert 'Intl.Collator' not in src, \
            f'{name} must NOT build its own collator (share branding.js)'
        for sym in required:
            assert sym in src, \
                f'{name} must route its sort through the shared {sym}'

    # The settings cards show raw ids — sorting them by the invisible pricing
    # name was the 2026-08-04 bug. core_panel must never key its list off
    # _modelShortName/_compareModelsByDisplayName again.
    assert '_compareModelsByDisplayName' not in core, \
        'core_panel.js must not sort the card list by friendly display name'
    assert '_modelShortName' not in core, \
        'core_panel.js must not key its sort off the pricing display name'

    # No consumer may walk a brand/provider group map in insertion order —
    # that was the section-order half of the bug.
    assert 'for (var brand in grouped)' not in vis, \
        'visibility_defaults.js still iterates brand groups in insertion order'

    # The old id-based sort key is gone (it WAS the bug).
    assert '_modelSortKey' not in core, \
        'core_panel.js still carries the model_id sort key that caused the ' \
        'display order to disagree with the labels'


def test_bundler_loads_branding_before_consumers():
    """branding.js defines the comparator; both consumers are plain window-scope
    concatenation, so the bundler must place it FIRST or the comparator is in
    the TDZ when core_panel/main_toolbar_ui run their sorts at call time."""
    bundler = open(os.path.join(ROOT, 'lib', 'js_bundler.py'), encoding='utf-8').read()
    order = {}
    for name in ('settings/branding.js', 'settings/core_panel.js',
                 'main/main_toolbar_ui.js'):
        m = re.search(r"'" + re.escape(name) + r"'", bundler)
        assert m, f'{name} missing from the bundler manifest'
        order[name] = m.start()
    assert order['settings/branding.js'] < order['settings/core_panel.js'], \
        'branding.js must be bundled before core_panel.js'
    assert order['settings/branding.js'] < order['main/main_toolbar_ui.js'], \
        'branding.js must be bundled before main_toolbar_ui.js'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
