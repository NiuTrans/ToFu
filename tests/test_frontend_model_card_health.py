#!/usr/bin/env python3
"""Frontend test — model cards show per-model health + user-configured prices.

WHY
---
Two Settings gaps closed together:
  1. The dispatcher's error-rate throttling (slot cooldowns) was invisible on
     the model list — you had to read logs to learn a model was cooling. The
     card now carries a health strip fed by /api/v1/dispatch/model-health,
     folded over the card's whole request-id pool.
  2. Input/output prices were read-only (static MODEL_PRICING table) while
     the edit dialog only exposed the blended composite cost. The dialog now
     writes the per-model `pricing` override and DERIVES the composite cost
     from it, so the two can never disagree.

WHAT IS GUARDED (results, not implementation — charter 2026-07-27)
------------------------------------------------------------------
  * Card pricing row: m.pricing override wins over the global cache (with a
    'custom' tag); discovery's input_price/output_price beats the cache too.
  * Health strip folds EVERY wire id in the card's pool: cooldown chip with
    remaining seconds + reason, success rate from pooled totals, inflight,
    no-traffic muted state, and EMPTY (hidden) before the first fetch.
  * _refreshAllModelCardHealth updates strips in place (no tab re-render).
  * Edit dialog: prefills only the explicit override, derives composite cost
    live, saves m.pricing + derived cost, and clearing the fields removes
    the override cleanly. INVALID input (only one axis / negative /
    non-numeric) is REJECTED with an alert BEFORE any mutation — a typo
    must never silently delete the saved pricing.

NEUTERS (source-level, on mutated copies — shipped files untouched):
  * N1: drop the m.pricing preference      → override price lost (red)
  * N2: drop the cooldown merge in the fold → cooldown chip gone (red)
  * N3: stop deriving the composite cost    → cost stays manual (red)
  * N4: drop the invalid-input reject        → override silently deleted (red)
"""

from __future__ import annotations

import json
import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

PROVIDER_RENDER_JS = os.path.join(JS_DIR, 'settings', 'provider_render.js')
KEY_STATS_JS = os.path.join(JS_DIR, 'settings', 'key_stats.js')
MODEL_EDIT_JS = os.path.join(JS_DIR, 'settings', 'model_edit.js')
MODEL_HEALTH_JS = os.path.join(JS_DIR, 'core', 'model_health.js')

_HTML = '<!DOCTYPE html><body><div id="stgProviderList"></div></body>'

_BODY = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2], process.argv[4], process.argv[5], process.argv[6]],
  globals: {
    _detectBrand: () => 'claude',
    _brandSvg: () => '',
    _endpointShort: (u) => u,
    debugLog: () => {},
    Api: { dispatch: { modelHealth: () => Promise.resolve(null) } },
    // Interpolating echo so chips carry their params ({s=42}) deterministically.
    t: (k, o) => {
      let s = k;
      if (o) for (const q of Object.keys(o)) s += '{' + q + '=' + o[q] + '}';
      return s;
    },
    _modelPricingCache: {
      'opus-logical': { input: 100, output: 200, name: 'Cached Opus' },
      'kimi-k3':      { input: 2,   output: 8,   name: 'Kimi K3' },
    },
    _stgPresets: {},
    _serverConfig: {},
    _renderProvidersTab: () => {},
    _renderPresetsTab: () => {},
    showAlert: (m) => { (window._alertCalls = window._alertCalls || []).push(m); },
  },
});

const indirectEval = eval;

/* model_health.js (argv[6]) is an IIFE publishing on `window`. In a browser
 * those props are globals; under node they are NOT, so key_stats.js's bare
 * `typeof foldRuntimeHealth === 'function'` guard would degrade to null and
 * the verdict would never materialise. Bridge them into node global scope —
 * this simulates the browser environment the product actually runs in. */
global.foldRuntimeHealth = window.foldRuntimeHealth;
global.foldProbeHealth = window.foldProbeHealth;
global.modelHealthUsable = window.modelHealthUsable;
global.modelHealthLevelClass = window.modelHealthLevelClass;
function neutralizeRerenders() {
  global._renderProvidersTab = window._renderProvidersTab = function () {};
  global._renderPresetsTab = window._renderPresetsTab = function () {};
}
neutralizeRerenders();

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

function seedProviders() {
  global._stgProviders = window._stgProviders = [
    { id: 'provA', name: 'A', brand: 'claude', enabled: true, api_keys: ['k1'],
      models: [
        { model_id: 'opus-logical', request_ids: ['aws.opus', 'vertex.opus'],
          capabilities: ['text'], rpm: 30, cost: 0.045,
          pricing: { input: 5, output: 25 } },
        { model_id: 'kimi-k3', aliases: [], capabilities: ['text'], rpm: 30,
          cost: 0.002 },
        { model_id: 'disc-model', aliases: [], capabilities: ['text'], rpm: 30,
          cost: 0.001, input_price: 0.5, output_price: 1.5 },
      ]},
  ];
}

function renderCards() {
  const list = document.getElementById('stgProviderList');
  list.innerHTML = _stgProviders[0].models
    .map((m, i) => _renderModelCard(0, i, m)).join('');
  return list;
}

function seedHealth() {
  global._modelHealthCache = window._modelHealthCache = {
    provA: {
      'aws.opus': { slots: 1, available_slots: 0, total_requests: 10,
                    total_errors: 5, consecutive_errors: 3, inflight: 2,
                    contention_errors: 782,
                    cooldown_remaining_s: 42, cooldown_reason: 'error',
                    last_error_msg: 'boom', last_error_ts: 10 },
      'vertex.opus': { slots: 1, available_slots: 1, total_requests: 5,
                       total_errors: 0, consecutive_errors: 0, inflight: 0,
                       cooldown_remaining_s: 0, cooldown_reason: '',
                       last_error_msg: '', last_error_ts: 0 },
    },
  };
  global._modelHealthTs = window._modelHealthTs = Date.now();
}

const PR_SRC = fs.readFileSync(process.argv[2], 'utf8');
const KS_SRC = fs.readFileSync(process.argv[4], 'utf8');
const ME_SRC = fs.readFileSync(process.argv[5], 'utf8');
const CARD_FN = sliceFn(PR_SRC, 'function _renderModelCard(provIdx, modelIdx, m) {');
const ROW_FN = sliceFn(KS_SRC, 'function _modelCardHealthRow(provIdx, modelIdx) {');
const SAVE_FN = sliceFn(ME_SRC, 'function _saveModelEdit(provIdx, modelIdx) {');

try {
  // ══ 1. Pricing row: explicit override wins, with the custom tag ══
  seedProviders();
  let list = renderCards();
  let cards = list.querySelectorAll('.stg-mcard');
  check('three_cards_rendered', cards.length === 3);
  const priceRow0 = cards[0].querySelector('.stg-mcard-pricing').textContent;
  check('override_input_price_shown', priceRow0.indexOf('$5') >= 0);
  check('override_output_price_shown', priceRow0.indexOf('$25') >= 0);
  check('override_beats_cache', priceRow0.indexOf('$100') < 0);
  check('custom_tag_shown',
    cards[0].querySelector('.stg-price-custom') !== null);

  // ══ 2. Pricing row: global cache is the fallback, no custom tag ══
  const priceRow1 = cards[1].querySelector('.stg-mcard-pricing').textContent;
  check('cache_prices_shown',
    priceRow1.indexOf('$2') >= 0 && priceRow1.indexOf('$8') >= 0);
  check('no_custom_tag_on_cache',
    cards[1].querySelector('.stg-price-custom') === null);

  // ══ 3. Pricing row: discovery input/output beats the cache-less void ══
  const priceRow2 = cards[2].querySelector('.stg-mcard-pricing').textContent;
  check('discovery_prices_shown',
    priceRow2.indexOf('$0.5') >= 0 && priceRow2.indexOf('$1.5') >= 0);

  // ══ 4. Health strip: cooldown folds across the wire pool ══
  seedHealth();
  list = renderCards();
  cards = list.querySelectorAll('.stg-mcard');
  const strip0 = cards[0].querySelector('.stg-mcard-health');
  const cool = strip0.querySelector('.stg-mh-chip.cool');
  check('cooldown_chip_present', cool !== null);
  check('cooldown_seconds_shown', cool && cool.textContent.indexOf('42') >= 0);
  check('cooldown_reason_shown',
    cool && cool.textContent.indexOf('mhReasonError') >= 0);

  // ══ 5. Pool rule (pt_464f2baf): 67% is INFORMATIONAL; the strip is NOT
  // red. The seed folds 'aws.opus' (5/10 err, cooling) + 'vertex.opus'
  // (1 live slot) — under the OLD sum-and-divide rule that is 67% → warn.
  // Under the pool rule a live slot exists → the model is USABLE, so the
  // strip class must be available (cool, because a slot IS cooling), never
  // warn/down. The rate chip still SHOWS 67% (the pool genuinely is lossy). ══
  const expectedPct = Math.round((1 - 5 / 15) * 100);
  const srChip5 = strip0.querySelector('.stg-mh-chip.warn');
  check('pooled_success_rate_shown', srChip5 !== null &&
    srChip5.textContent.indexOf(expectedPct + '%') >= 0);
  // The strip wrapper is cooling → 'cool' — but the verdict is NOT warn.
  const stripCls = strip0.className;
  check('pool_rule_strip_not_warn', stripCls.indexOf('warn') < 0);
  check('pool_rule_strip_available', /stg-mcard-health (cool|ok|good)/.test(stripCls));
  // Direct verdict check: one live slot ⇒ not 'down'.
  {
    const agg = _modelCardHealthRow(0, 0);
    check('pool_rule_verdict_degraded', agg.verdict && agg.verdict.level === 'degraded');
    check('pool_rule_verdict_usable',
      typeof modelHealthUsable === 'function' ? modelHealthUsable(agg.verdict) : true);
    check('pool_rule_strip_cls_degraded', _modelCardHealthCls(0, 0) === 'ok' ||
      _modelCardHealthCls(0, 0) === 'cool');  // cooling overrides the left accent
  }

  // ══ 5b. Contention chip renders SEPARATELY from the success rate ══
  // (2026-07-28: 782 external 429s made a healthy model look 24% — they
  // must surface as their own chip, never inside the success rate.)
  check('contention_chip_present',
    strip0.textContent.indexOf('mhContention') >= 0);
  check('contention_chip_count', strip0.textContent.indexOf('n=782') >= 0);

  // ══ 6. Inflight chip ══
  check('inflight_chip', strip0.textContent.indexOf('n=2') >= 0);

  // ══ 7. Unrouted model → muted no-traffic chip ══
  const strip1 = cards[1].querySelector('.stg-mcard-health');
  check('no_traffic_chip', strip1.querySelector('.stg-mh-chip.muted') !== null);

  // ══ 8. Before the first fetch the strip is EMPTY (hidden, not "no traffic") ══
  global._modelHealthTs = window._modelHealthTs = 0;
  list = renderCards();
  const cold = list.querySelectorAll('.stg-mcard-health');
  check('strip_empty_before_fetch', cold[0].innerHTML === '');

  // ══ 9. In-place refresh: cooldown clears without re-render ══
  seedHealth();
  list = renderCards();
  const stripBefore = list.querySelector('.stg-mcard-health');
  global._modelHealthCache.provA['aws.opus'].cooldown_remaining_s = 0;
  global._modelHealthCache.provA['aws.opus'].consecutive_errors = 0;
  global._modelHealthCache.provA['aws.opus'].total_errors = 0;
  _refreshAllModelCardHealth();
  const stripAfter = list.querySelector('.stg-mcard-health');
  check('refresh_same_node', stripBefore === stripAfter);
  check('refresh_clears_cooldown', stripAfter.querySelector('.stg-mh-chip.cool') === null);
  check('refresh_success_now_good', stripAfter.querySelector('.stg-mh-chip.good') !== null);

  // ══ 10. Edit dialog: override prefilled, cost readonly-derived ══
  seedProviders();
  list = renderCards();
  _editModel(0, 0);
  const form = document.querySelector('.stg-edit-form');
  check('edit_form_opened', form !== null);
  check('price_fields_prefilled',
    form.querySelector('.stg-edit-pin').value === '5' &&
    form.querySelector('.stg-edit-pout').value === '25');
  check('cost_readonly_with_override',
    form.querySelector('.stg-edit-cost').readOnly === true);

  // ══ 11. Live derivation: typing both prices recomputes composite cost ══
  const pinEl = form.querySelector('.stg-edit-pin');
  pinEl.value = '1';
  form.querySelector('.stg-edit-pout').value = '3';
  _onModelPriceInput(pinEl);
  check('live_cost_derived',
    parseFloat(form.querySelector('.stg-edit-cost').value) === 0.002);

  // ══ 12. Save: pricing override written, composite cost derived ══
  _saveModelEdit(0, 0);
  const m0 = _stgProviders[0].models[0];
  check('pricing_saved', m0.pricing && m0.pricing.input === 1 && m0.pricing.output === 3);
  check('cost_derived_on_save', m0.cost === 0.002);

  // ══ 13. Clearing the fields removes the override, cost is manual again ══
  _editModel(0, 0);
  const form2 = document.querySelector('.stg-edit-form');
  form2.querySelector('.stg-edit-pin').value = '';
  form2.querySelector('.stg-edit-pout').value = '';
  _onModelPriceInput(form2.querySelector('.stg-edit-pin'));
  check('cost_editable_after_clear',
    form2.querySelector('.stg-edit-cost').readOnly === false);
  form2.querySelector('.stg-edit-cost').value = '0.077';
  window._alertCalls = [];
  _saveModelEdit(0, 0);
  const m0b = _stgProviders[0].models[0];
  check('override_removed', m0b.pricing === undefined);
  check('manual_cost_kept', m0b.cost === 0.077);
  check('explicit_clear_is_not_rejected', window._alertCalls.length === 0);

  // ══ 15. Invalid price input is REJECTED — the override survives typos ══
  // (Pre-fix: only-one-filled / negative / non-numeric all fell into the
  // clear branch and SILENTLY deleted the saved pricing with no alert.)
  seedProviders();          // model 0: pricing {input:5, output:25}, cost 0.045
  renderCards();
  _editModel(0, 0);
  const f4 = document.querySelector('.stg-edit-form');
  // negative
  window._alertCalls = [];
  f4.querySelector('.stg-edit-pin').value = '-5';
  f4.querySelector('.stg-edit-pout').value = '3';
  _saveModelEdit(0, 0);
  check('negative_price_alerts', window._alertCalls.length === 1);
  const mNeg = _stgProviders[0].models[0];
  check('negative_price_override_preserved',
    mNeg.pricing && mNeg.pricing.input === 5 && mNeg.pricing.output === 25);
  check('negative_price_cost_preserved', mNeg.cost === 0.045);
  // partial — only one axis filled
  window._alertCalls = [];
  f4.querySelector('.stg-edit-pin').value = '1';
  f4.querySelector('.stg-edit-pout').value = '';
  _saveModelEdit(0, 0);
  check('partial_price_alerts', window._alertCalls.length === 1);
  check('partial_price_override_preserved',
    _stgProviders[0].models[0].pricing.input === 5);
  // non-numeric garbage
  window._alertCalls = [];
  f4.querySelector('.stg-edit-pin').value = 'abc';
  f4.querySelector('.stg-edit-pout').value = '3';
  _saveModelEdit(0, 0);
  check('nan_price_alerts', window._alertCalls.length === 1);
  check('nan_price_override_preserved',
    _stgProviders[0].models[0].pricing.input === 5);

  // ══ 14. Wire-pool contract: request_ids win, else root + aliases ══
  const idsPool = _modelWireIds({ model_id: 'x', request_ids: ['a', 'b'], aliases: ['c'] });
  const idsLegacy = _modelWireIds({ model_id: 'x', aliases: ['c'] });
  check('request_ids_win', idsPool.join(',') === 'a,b');
  check('legacy_root_plus_aliases', idsLegacy.join(',') === 'x,c');

  // ══ NEUTER 1: drop the m.pricing preference → override lost ══
  {
    const n = CARD_FN.replace(
      'if (m.pricing && m.pricing.input != null && m.pricing.output != null) {',
      'if (false) {');
    check('N1_applied', n !== CARD_FN);
    indirectEval(n);
    seedProviders();
    const rl = renderCards();
    const row = rl.querySelectorAll('.stg-mcard')[0]
      .querySelector('.stg-mcard-pricing').textContent;
    check('N1_override_lost', row.indexOf('$5') < 0 && row.indexOf('$100') >= 0);
    indirectEval(CARD_FN);   // restore
  }

  // ══ NEUTER 2: drop the cooldown merge → chip gone ══
  {
    const n = ROW_FN.replace('if (rem > agg.cooldown_remaining_s) {', 'if (false) {');
    check('N2_applied', n !== ROW_FN);
    indirectEval(n);
    seedProviders();
    seedHealth();
    const rl2 = renderCards();
    const strip = rl2.querySelectorAll('.stg-mcard')[0]
      .querySelector('.stg-mcard-health');
    check('N2_cooldown_gone', strip.querySelector('.stg-mh-chip.cool') === null);
    indirectEval(ROW_FN);    // restore
  }

  // ══ NEUTER 3: stop deriving the composite cost → stays manual ══
  {
    const n = SAVE_FN.replace('m.cost = _deriveCompositeCost(_pin, _pout);',
                              'm.cost = 0.01;');
    check('N3_applied', n !== SAVE_FN);
    indirectEval(n);
    seedProviders();
    renderCards();
    _editModel(0, 0);
    const f3 = document.querySelector('.stg-edit-form');
    f3.querySelector('.stg-edit-pin').value = '1';
    f3.querySelector('.stg-edit-pout').value = '3';
    _saveModelEdit(0, 0);
    check('N3_cost_not_derived', _stgProviders[0].models[0].cost !== 0.002);
    indirectEval(SAVE_FN);   // restore
  }

  // ══ NEUTER 4: drop the invalid-input reject → override silently deleted ══
  {
    const n = SAVE_FN.replace('if (!_bothEmpty && !_bothValid) {', 'if (false) {');
    check('N4_applied', n !== SAVE_FN);
    indirectEval(n);
    seedProviders();          // model 0: pricing {input:5, output:25}
    renderCards();
    _editModel(0, 0);
    const f5 = document.querySelector('.stg-edit-form');
    f5.querySelector('.stg-edit-pin').value = '-5';
    f5.querySelector('.stg-edit-pout').value = '3';
    window._alertCalls = [];
    _saveModelEdit(0, 0);
    check('N4_silent_delete_returns',
      _stgProviders[0].models[0].pricing === undefined &&
      window._alertCalls.length === 0);
    indirectEval(SAVE_FN);   // restore
  }

  // ══ NEUTER 5: drop the contention fold → chip gone ══
  {
    const n = ROW_FN.replace(
      'agg.contention_errors += r.contention_errors || 0;',
      '');
    check('N5_applied', n !== ROW_FN);
    indirectEval(n);
    seedProviders();
    seedHealth();
    const rl5 = renderCards();
    const strip5 = rl5.querySelectorAll('.stg-mcard')[0]
      .querySelector('.stg-mcard-health');
    check('N5_contention_gone', strip5.textContent.indexOf('mhContention') < 0);
    indirectEval(ROW_FN);   // restore
  }

  // ══ NEUTER 6: revert strip colour to the pooled success_rate → the
  // degraded pool turns warn (red) again. Guards the pool rule in
  // _modelCardHealthCls, not just the chip. ══
  {
    const CLS_FN = sliceFn(KS_SRC, 'function _modelCardHealthCls(provIdx, modelIdx) {');
    const n = CLS_FN.replace(
      "if (agg.verdict) {",
      "if (agg.success_rate != null && agg.success_rate < 0.9) return 'warn';\n    if (agg.verdict) {");
    check('N6_applied', n !== CLS_FN);
    indirectEval(n);
    seedProviders();
    seedHealth();
    // Cooldown short-circuits to 'cool' before the success-rate branch, so
    // clear it — otherwise the NEUTER would test the cooldown override, not
    // the success_rate reversion it means to prove.
    global._modelHealthCache.provA['aws.opus'].cooldown_remaining_s = 0;
    check('N6_degraded_turns_warn', _modelCardHealthCls(0, 0) === 'warn');
    indirectEval(CLS_FN);   // restore
    // Re-seed fully — the cleared cooldown above must not leak into the
    // restored code path or the earlier section-5 'degraded' expectations.
    global._modelHealthCache.provA['aws.opus'].cooldown_remaining_s = 42;
    check('N6_restored', _modelCardHealthCls(0, 0) !== 'warn');
  }
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
} finally {
  report();
}
'''


def test_model_card_health_and_pricing():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=PROVIDER_RENDER_JS,
        body_js=body,
        extra_targets=[KEY_STATS_JS, MODEL_EDIT_JS, MODEL_HEALTH_JS],
        min_pass=52,
        label='model-card-health',
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
