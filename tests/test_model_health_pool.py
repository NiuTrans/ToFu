#!/usr/bin/env python3
"""Frontend test — THE pool-availability rule for model health.

WHY
---
A logical model on a gateway is served by a POOL: every (wire id × API key)
pair is an independent dispatch slot and the dispatcher rotates over the
whole pool. ``claude-opus-4.7`` carries 3 request_ids × 3 keys = 9 slots.

The rule that shipped before this suite SUMMED the pool's ``total_requests``
/ ``total_errors`` and divided. With 8 dead slots and 1 healthy one that
yields ~11% → a red card for a model the dispatcher serves perfectly well.
One redeployed upstream (the yuju daily builds) turned whole cards
permanently red, which is how a health signal loses the user's trust.

The rule guarded here:

    ██ A logical model is USABLE iff ANY slot in its pool is usable. ██

WHAT IS GUARDED (results, not implementation — charter 2026-07-27)
------------------------------------------------------------------
  * 8 dead wire ids + 1 available → 'degraded' AND modelHealthUsable() true.
    This is the yuju-daily-build case; it must never read as a failure.
  * every slot available          → 'ok'
  * ZERO slots available          → 'down' (the ONLY "don't pick this" level)
  * no observation at all         → 'unknown' (never 'ok' — absence of a
    verdict must not look like a passing verdict)
  * PROBE cells obey the SAME rule: one 'ok' cell among failures → degraded.
  * non-verdict statuses ('skipped' non-chat, 'not_logged_in' subscription)
    are excluded from the ok/fail tally and surface as their own level, so a
    subscription model with no live token can never render as a red failure.
  * STALENESS is orthogonal and WINS over the verdict: a green verdict from
    three days ago paints 'stale', because "tested and healthy" must be
    distinguishable from "not tested" for the user to select with confidence.
  * A model never probed is NOT stale (absence of a result cannot expire).

NEUTERS (source-level, on mutated copies — the shipped file is untouched):
  * N1: level rule → "any failure means down"  → degraded case turns down (red)
  * N2: drop the staleness override            → expired green paints green (red)
  * N3: count non-verdict cells as failures    → not_logged_in reads down (red)
"""

from __future__ import annotations

import json
import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

MODEL_HEALTH_JS = os.path.join(JS_DIR, 'core', 'model_health.js')

_HTML = '<!DOCTYPE html><body></body>'

_BODY = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2]],
});

const indirectEval = eval;
const SRC = fs.readFileSync(process.argv[2], 'utf8');

/* model_health.js is an IIFE that publishes on `window` (same shape as
 * core/model_caps.js). In a browser a window property IS a global, but under
 * node it is not — so resolve through `window` on EVERY call. Aliasing once
 * would freeze the pre-NEUTER implementation and make the NEUTER blocks
 * silently assert against the original code. */
const foldRuntimeHealth = (...a) => window.foldRuntimeHealth(...a);
const foldProbeHealth = (...a) => window.foldProbeHealth(...a);
const modelHealthUsable = (...a) => window.modelHealthUsable(...a);
const modelHealthLevelClass = (...a) => window.modelHealthLevelClass(...a);

/* Build a runtime health row (as /api/v1/dispatch/model-health returns). */
function row(wireId, availableSlots, extra) {
  return Object.assign({
    wire_id: wireId,
    slots: 1,
    available_slots: availableSlots,
    total_requests: 10,
    total_errors: availableSlots > 0 ? 0 : 10,
  }, extra || {});
}

/* Build a probe cell (as probe-cells snapshots return). */
function cell(keyIdx, wireId, status, detail) {
  return { key_idx: keyIdx, model_id: wireId, status: status,
           detail: detail || '',
           proof: status === 'ok' ? 'generated_text' : null };
}

const HOUR = 3600;
const NOW = 1800000000;   // fixed clock so staleness is deterministic

try {
  // ══ 1. THE case: 8 dead wire ids + 1 available → degraded, USABLE ══
  // This is the yuju-daily-build shape. The replaced rule scored it ~11%
  // and painted it red; the dispatcher serves it from the one live slot.
  {
    const rows = [];
    for (let i = 0; i < 8; i++) rows.push(row('dead-' + i, 0));
    rows.push(row('alive', 1));
    const agg = foldRuntimeHealth(rows);
    check('pool_8dead_1alive_is_degraded', agg.level === 'degraded');
    check('pool_8dead_1alive_is_usable', modelHealthUsable(agg) === true);
    check('pool_8dead_1alive_not_down', agg.level !== 'down');
    check('pool_counts_ok_1', agg.okCount === 1);
    check('pool_counts_fail_8', agg.failCount === 8);
    check('pool_failures_enumerated', agg.failures.length === 8);
    check('pool_failure_carries_wire_id',
      agg.failures[0].wireId === 'dead-0');
  }

  // ══ 2. All slots available → ok ══
  {
    const agg = foldRuntimeHealth([row('a', 1), row('b', 2), row('c', 1)]);
    check('all_available_is_ok', agg.level === 'ok');
    check('all_available_usable', modelHealthUsable(agg) === true);
    check('all_available_no_failures', agg.failures.length === 0);
  }

  // ══ 3. ZERO available → down (the only "don't pick" verdict) ══
  {
    const rows = [];
    for (let i = 0; i < 9; i++) rows.push(row('dead-' + i, 0));
    const agg = foldRuntimeHealth(rows);
    check('zero_available_is_down', agg.level === 'down');
    check('zero_available_not_usable', modelHealthUsable(agg) === false);
  }

  // ══ 4. No observation → unknown, NOT ok ══
  {
    const agg = foldRuntimeHealth([]);
    check('empty_is_unknown', agg.level === 'unknown');
    check('empty_not_ok', agg.level !== 'ok');
    check('empty_not_usable', modelHealthUsable(agg) === false);
    const aggNull = foldRuntimeHealth(null);
    check('null_is_unknown', aggNull.level === 'unknown');
  }

  // ══ 5. PROBE cells obey the SAME rule ══
  {
    const cells = [
      cell(0, 'aws.opus-4.7', 'not_found', 'HTTP 404'),
      cell(1, 'aws.opus-4.7', 'not_found', 'HTTP 404'),
      cell(2, 'aws.opus-4.7', 'not_found', 'HTTP 404'),
      cell(0, 'yuju-opus-4.7', 'unavailable', 'HTTP 503'),
      cell(1, 'yuju-opus-4.7', 'ok', 'HTTP 200'),
    ];
    const agg = foldProbeHealth(cells, { finishedAt: NOW, now: NOW });
    check('probe_one_ok_is_degraded', agg.level === 'degraded');
    check('probe_one_ok_is_usable', modelHealthUsable(agg) === true);
    check('probe_failure_detail_kept',
      agg.failures[0].detail === 'HTTP 404');
    check('probe_failure_key_idx_kept', agg.failures[0].keyIdx === 0);
  }

  // ══ 6. All probe cells failing → down ══
  {
    const agg = foldProbeHealth(
      [cell(0, 'x', 'unauthorized'), cell(1, 'x', 'not_found')],
      { finishedAt: NOW, now: NOW });
    check('probe_all_fail_is_down', agg.level === 'down');
  }

  // ══ 7. Non-verdict statuses are NOT failures ══
  // A subscription model with no live token, and a non-chat model with no
  // probe surface, must not render as a red failure — neither is a fault
  // of the model.
  {
    const aggAuth = foldProbeHealth([cell(0, 'claude-opus', 'not_logged_in')],
                                    { finishedAt: NOW, now: NOW });
    check('not_logged_in_level', aggAuth.level === 'not_logged_in');
    check('not_logged_in_not_down', aggAuth.level !== 'down');
    check('not_logged_in_zero_failcount', aggAuth.failCount === 0);

    const aggSkip = foldProbeHealth([cell(0, 'embed-3', 'skipped')],
                                    { finishedAt: NOW, now: NOW });
    check('skipped_level', aggSkip.level === 'skipped');
    check('skipped_not_down', aggSkip.level !== 'down');

    // A non-verdict cell must not mask a REAL verdict in the same pool.
    const aggMix = foldProbeHealth(
      [cell(0, 'a', 'skipped'), cell(1, 'a', 'ok')],
      { finishedAt: NOW, now: NOW });
    check('nonverdict_does_not_mask_ok', aggMix.level === 'ok');
    check('nonverdict_excluded_from_total_ok', aggMix.okCount === 1);
  }

  // ══ 8. STALENESS wins over the verdict ══
  {
    const fresh = foldProbeHealth([cell(0, 'a', 'ok')],
      { finishedAt: NOW - HOUR, now: NOW });
    check('fresh_probe_not_stale', fresh.stale === false);
    check('fresh_class_is_ok', modelHealthLevelClass(fresh) === 'ok');

    const old = foldProbeHealth([cell(0, 'a', 'ok')],
      { finishedAt: NOW - 3 * 24 * HOUR, now: NOW });
    check('three_day_old_is_stale', old.stale === true);
    check('stale_class_overrides_ok', modelHealthLevelClass(old) === 'stale');
    check('stale_keeps_underlying_level', old.level === 'ok');
    check('stale_age_reported', Math.round(old.ageS) === 3 * 24 * HOUR);

    // Never probed → unknown, and NOT stale (absence cannot expire).
    const never = foldProbeHealth([], { finishedAt: 0, now: NOW });
    check('never_probed_unknown', never.level === 'unknown');
    check('never_probed_not_stale', never.stale === false);
    check('never_probed_class', modelHealthLevelClass(never) === 'unknown');
  }

  // ══ 9. Mixed-version defence: HTTP 400 can never paint green ══
  {
    const legacy400 = {
      key_idx: 0, model_id: 'aws.claude-fable-5', status: 'ok',
      detail: 'HTTP 400', probe_surface: 'chat'
    };
    const bad = foldProbeHealth([legacy400], {
      finishedAt: NOW, now: NOW, schemaVersion: 1
    });
    check('legacy_ok_http400_is_down', bad.level === 'down');
    check('legacy_ok_http400_not_usable', modelHealthUsable(bad) === false);
    check('legacy_ok_http400_effective_status',
      bad.failures[0].status === 'bad_request');

    const legacy200 = {
      key_idx: 0, model_id: 'old-chat', status: 'ok',
      detail: 'HTTP 200', probe_surface: 'chat'
    };
    const neutral = foldProbeHealth([legacy200], {
      finishedAt: NOW, now: NOW, schemaVersion: 1
    });
    check('legacy_unvalidated_200_is_unverified', neutral.level === 'unverified');
    check('legacy_unvalidated_200_not_usable', modelHealthUsable(neutral) === false);
  }

  // ══ NEUTER 1: level rule → "any failure means down" ══
  // The exact regression this suite exists to prevent.
  {
    const n = SRC.replace(
      "if (agg.okCount > 0) return agg.failCount > 0 ? 'degraded' : 'ok';",
      "if (agg.failCount > 0) return 'down';\n    if (agg.okCount > 0) return 'ok';");
    check('N1_applied', n !== SRC);
    indirectEval(n);
    const rows = [];
    for (let i = 0; i < 8; i++) rows.push(row('dead-' + i, 0));
    rows.push(row('alive', 1));
    const bad = foldRuntimeHealth(rows);
    check('N1_degraded_becomes_down', bad.level === 'down');
    check('N1_usable_becomes_false', modelHealthUsable(bad) === false);
    indirectEval(SRC);   // restore
    const good = foldRuntimeHealth(rows);
    check('N1_restored', good.level === 'degraded');
  }

  // ══ NEUTER 2: drop the staleness override ══
  {
    const n = SRC.replace(
      "if (agg.level !== 'unknown' && agg.ageS > limit) agg.stale = true;",
      '');
    check('N2_applied', n !== SRC);
    indirectEval(n);
    const old = foldProbeHealth([cell(0, 'a', 'ok')],
      { finishedAt: NOW - 3 * 24 * HOUR, now: NOW });
    check('N2_stale_flag_lost', old.stale === false);
    check('N2_expired_green_paints_green',
      modelHealthLevelClass(old) === 'ok');
    indirectEval(SRC);   // restore
    check('N2_restored', foldProbeHealth([cell(0, 'a', 'ok')],
      { finishedAt: NOW - 3 * 24 * HOUR, now: NOW }).stale === true);
  }

  // ══ NEUTER 3: count non-verdict cells as failures ══
  {
    const n = SRC.replace(
      'var _NON_VERDICT = { skipped: 1, not_logged_in: 1, unverified: 1 };',
      'var _NON_VERDICT = {};');
    check('N3_applied', n !== SRC);
    indirectEval(n);
    const bad = foldProbeHealth([cell(0, 'claude-opus', 'not_logged_in')],
      { finishedAt: NOW, now: NOW });
    check('N3_not_logged_in_becomes_down', bad.level === 'down');
    indirectEval(SRC);   // restore
    check('N3_restored', foldProbeHealth([cell(0, 'c', 'not_logged_in')],
      { finishedAt: NOW, now: NOW }).level === 'not_logged_in');
  }
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
} finally {
  report();
}
'''


def test_model_health_pool_rule():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=MODEL_HEALTH_JS,
        body_js=body,
        min_pass=45,
        label='model-health-pool',
    )


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
