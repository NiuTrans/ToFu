/* ===== migrated source: core/model_health.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/model_health.js — THE model-availability judgment (SSOT)

   WHY THIS EXISTS
   ---------------
   A logical model on a gateway is served by a POOL: every (wire id × API
   key) pair is an independent slot, and the dispatcher rotates over the
   whole pool. `claude-opus-4.7` carries 3 request_ids and the provider has
   3 keys → 9 slots. `deepseek-v3.2` carries 4 ids → 12 slots.

   Therefore the ONLY correct availability rule is:

       ██ A logical model is USABLE iff ANY slot in its pool is usable. ██

   The rule this replaces summed `total_requests` / `total_errors` across
   the pool and divided. With 8 dead slots and 1 healthy one that yields
   ~11% → a red "warn" card for a model the dispatcher serves perfectly
   well. A gateway that redeploys ONE upstream (the yuju daily builds) made
   every affected card permanently red, which is exactly how a health signal
   loses the user's trust. A pooled AVERAGE answers "how lossy is the pool",
   which is a cost/latency question; it does NOT answer "can I select this
   model with confidence", which is what the card is for.

   TWO INPUTS, ONE RULE
   --------------------
   Both axes feed the same judgment so the two can never disagree:

     * RUNTIME  (/api/v1/dispatch/model-health rows, per wire id) —
       a slot is usable when `available_slots > 0`. Passive: reflects
       traffic that already happened.
     * PROBE    (probe-cells snapshot cells, per key × wire id) —
       a cell is usable when `status === 'ok'`. Active: answers
       "is it usable RIGHT NOW" even with zero traffic.

   LEVELS
   ------
     ok            — every observed slot usable
     degraded      — SOME slots usable (dispatcher still serves; the pool
                     is thinner than it looks). Never render this as a
                     failure — it is a working model.
     down          — NO slot usable. The only level that means "don't pick".
     unknown       — no observation at all (never probed / never routed)
     skipped       — non-chat modality with no probe surface implemented
     unverified    — a legacy 2xx result that never validated output content
     not_logged_in — subscription (OAuth) provider with no live token; the
                     pool cannot be judged, and this is NOT a model fault

   STALENESS is orthogonal to the level: a green verdict from three days ago
   must not read as "good now". `stale` is computed from the snapshot's
   `finished_at` and callers render it as its own muted state — the whole
   point of the feature is telling "tested, healthy" apart from "not tested".

   Pure module: no DOM, no network, no window state read at load. Consumed
   by settings/key_stats.js (runtime strip) and the preset-tab probe dots.
   Concatenated by Vite's module graph — window scope, no imports.
   ═══════════════════════════════════════════════════════════════════ */

(function() {
  /* A probe snapshot older than this reads as stale (seconds). One day: long
   * enough that a morning probe still counts after lunch, short enough that
   * yesterday's green never vouches for today's gateway. */
  var STALE_AFTER_S = 24 * 3600;

  /* Cell/row statuses that are NOT a verdict on the model itself and so are
   * excluded from the ok/fail tally (they'd otherwise read as failures). */
  var _NON_VERDICT = { skipped: 1, not_logged_in: 1, unverified: 1 };

  function _emptyAgg() {
    return {
      level: 'unknown',
      stale: false,
      okCount: 0,
      failCount: 0,
      total: 0,
      skippedCount: 0,
      notLoggedInCount: 0,
      unverifiedCount: 0,
      failures: [],
      probedAt: 0,
      ageS: null,
    };
  }

  /* Decide the level from a tally. THE pool rule lives here and nowhere
   * else: any usable slot ⇒ usable model. */
  function _levelFor(agg) {
    if (agg.okCount > 0) return agg.failCount > 0 ? 'degraded' : 'ok';
    if (agg.failCount > 0) return 'down';
    // Nothing countable: report WHY there is no verdict rather than 'ok'.
    if (agg.notLoggedInCount > 0) return 'not_logged_in';
    if (agg.unverifiedCount > 0) return 'unverified';
    if (agg.skippedCount > 0) return 'skipped';
    return 'unknown';
  }

  /** Defensive status normalization for mixed-version deployments.
   *
   * v1 backends emitted ``status: ok, detail: HTTP 400``. The frontend must
   * never paint that green even while an old server or persisted snapshot is
   * still in the fleet. A legacy chat HTTP 200 also stays neutral until the
   * strict v2 backend re-tests it and supplies semantic ``proof``. */
  function effectiveProbeStatus(cell, schemaVersion) {
    if (!cell) return '';
    var st = cell.status || '';
    if (st !== 'ok') return st;
    var http = Number(cell.http_status || 0);
    if (!http) {
      var match = String(cell.detail || '').match(/\bHTTP\s+(\d{3})\b/i);
      if (match) http = Number(match[1]);
    }
    if (http >= 400) {
      return [400, 409, 415, 422].indexOf(http) >= 0
        ? 'bad_request' : 'error';
    }
    var version = Number(schemaVersion || 1);
    var surface = cell.probe_surface || 'chat';
    if (version < 2 && !cell.proof && (surface === 'chat' || !surface)) {
      return 'unverified';
    }
    return 'ok';
  }

  /**
   * Fold RUNTIME health rows (one per wire id) into one model verdict.
   *
   * @param {object[]} rows Rows from /api/v1/dispatch/model-health, already
   *   selected for this model's wire-id pool. A row is usable when it still
   *   has an uncooled slot (`available_slots > 0`).
   * @returns {object} the aggregate (see module header for `level`).
   */
  function foldRuntimeHealth(rows) {
    var agg = _emptyAgg();
    if (!rows || !rows.length) return agg;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (!r) continue;
      agg.total++;
      if ((r.available_slots || 0) > 0) {
        agg.okCount++;
      } else {
        agg.failCount++;
        agg.failures.push({
          wireId: r.wire_id || r.model || '',
          status: r.cooldown_reason || 'unavailable',
          detail: r.last_error_msg || '',
        });
      }
    }
    agg.level = _levelFor(agg);
    return agg;
  }

  /**
   * Fold PROBE cells into one model verdict.
   *
   * @param {object[]} cells probe-cells entries for this model's pool, each
   *   ``{key_idx, model_id, status, detail}``.
   * @param {object} [opts] ``{finishedAt, now, staleAfterS}`` — epoch
   *   SECONDS (matching the backend snapshot), used for the stale flag.
   * @returns {object} the aggregate.
   */
  function foldProbeHealth(cells, opts) {
    var agg = _emptyAgg();
    var o = opts || {};
    if (cells && cells.length) {
      for (var i = 0; i < cells.length; i++) {
        var c = cells[i];
        if (!c) continue;
        agg.total++;
        var st = effectiveProbeStatus(c, o.schemaVersion);
        if (st === 'ok') {
          agg.okCount++;
        } else if (_NON_VERDICT[st]) {
          if (st === 'skipped') agg.skippedCount++;
          else if (st === 'not_logged_in') agg.notLoggedInCount++;
          else agg.unverifiedCount++;
        } else {
          agg.failCount++;
          agg.failures.push({
            keyIdx: c.key_idx,
            wireId: c.model_id || '',
            status: st,
            detail: c.detail || '',
          });
        }
      }
    }
    agg.level = _levelFor(agg);

    /* Staleness — only meaningful once something WAS observed. An unknown
     * verdict is already "no signal"; calling it stale on top would imply a
     * result had expired when none ever existed. */
    var finished = Number(o.finishedAt || 0);
    agg.probedAt = finished > 0 ? finished : 0;
    if (agg.probedAt > 0) {
      var nowS = Number(o.now || (Date.now() / 1000));
      var limit = Number(o.staleAfterS != null ? o.staleAfterS : STALE_AFTER_S);
      agg.ageS = Math.max(0, nowS - agg.probedAt);
      if (agg.level !== 'unknown' && agg.ageS > limit) agg.stale = true;
    }
    return agg;
  }

  /**
   * The CSS state token for an aggregate. Staleness WINS over the verdict —
   * an expired green must not paint green.
   */
  function modelHealthLevelClass(agg) {
    if (!agg) return 'unknown';
    if (agg.stale) return 'stale';
    return agg.level || 'unknown';
  }

  /** True when the dispatcher can still serve this model (ok or degraded). */
  function modelHealthUsable(agg) {
    return !!agg && (agg.level === 'ok' || agg.level === 'degraded');
  }

  runtimeScope.foldRuntimeHealth = foldRuntimeHealth;
  runtimeScope.foldProbeHealth = foldProbeHealth;
  runtimeScope.effectiveProbeStatus = effectiveProbeStatus;
  runtimeScope.modelHealthLevelClass = modelHealthLevelClass;
  runtimeScope.modelHealthUsable = modelHealthUsable;
  runtimeScope.MODEL_HEALTH_STALE_AFTER_S = STALE_AFTER_S;
})();

