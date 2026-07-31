/* ═══════════════════════════════════════════════════════════════════
   settings/key stats — extracted from settings.js (split 2026-05-28)

   Per-key today's runtime state: success-rate / 429 / override toggle.

   The legacy "today's key status" block (separate `<div class="stg-keystats">`
   that mirrored every API key) has been merged into the API-key editor
   itself — every key is now one card with two rows (editor + stats).
   This file therefore exposes:

     _loadKeyStats(), _onKeyToggle(), _onKeyClearOverride()
       — unchanged: fetch / mutate today's stats
     _keyStatsClass(row)
       — picks the colour-bar class for the merged card wrapper
     _getKeyStatRowFor(provIdx, keyIdx)
       — convenience accessor consumed by provider_render.js
     _renderKeyCardStatsHTML(provIdx, keyIdx)
       — inner HTML for a card's stats sub-row
     _keyStatsHelpText(isLocal)
       — tooltip text shown next to the API Keys section title
     _renderProviderKeyStats(provIdx)
       — refresh the stats sub-rows of all cards in one provider in place

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file.
   ═══════════════════════════════════════════════════════════════════ */

/** Fetch today's per-key stats from backend and re-render the visible
 *  provider cards. Called once on settings open and after any user override. */
function _loadKeyStats() {
  if (_keyStatsLoading) return Promise.resolve(_keyStatsCache);
  _keyStatsLoading = true;
  return Api.dispatch.keyStats()
    .then(function(data) {
      if (data && typeof data === 'object') {
        _keyStatsCache = {
          day: data.day || '',
          providers: data.providers || {},
          min_attempts: data.min_attempts || 5,
          min_success_rate: data.min_success_rate || 0.5,
        };
      }
    })
    .catch(function(e) {
      debugLog('[Settings] Failed to load key stats: ' + (e && e.message), 'warning');
    })
    .finally(function() {
      _keyStatsLoading = false;
      for (var i = 0; i < _stgProviders.length; i++) _renderProviderKeyStats(i);
    });
}

/** Format the numeric success rate as a percentage string (or '—' for N/A). */
function _fmtSuccessRate(sr) {
  if (sr == null) return '—';
  return Math.round(sr * 100) + '%';
}

/** Pick the wrapper-class that drives the merged card's left colour bar. */
function _keyStatsClass(row) {
  if (!row) return 'stg-keystat-idle';
  if (row.exhausted && row.override !== true) return 'stg-keystat-exhausted';
  if (!row.enabled) return 'stg-keystat-disabled';
  if (row.auto_disabled) return 'stg-keystat-warn';
  if (row.success_rate == null) return 'stg-keystat-idle';
  if (row.success_rate >= 0.9) return 'stg-keystat-good';
  if (row.success_rate >= _keyStatsCache.min_success_rate) return 'stg-keystat-ok';
  return 'stg-keystat-warn';
}

/** Return the stat row for (provider_id, key_name) or null. */
function _getKeyStatRow(providerId, keyName) {
  var provs = _keyStatsCache.providers || {};
  var p = provs[providerId] || provs['default'];
  if (!p) return null;
  return p[keyName] || null;
}

/** Convenience: stat row by (provIdx, keyIdx). */
function _getKeyStatRowFor(provIdx, keyIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return null;
  var providerId = p.id || 'default';
  return _getKeyStatRow(providerId, providerId + '_key_' + keyIdx);
}

/** Tooltip body for the ⓘ icon next to the section title.
 *  Carries both the editor hint and the auto-disable policy. */
function _keyStatsHelpText(isLocal) {
  var base = isLocal ? t('settings.apiKeysHintLocal') : t('settings.apiKeysHint');
  var day = _keyStatsCache && _keyStatsCache.day;
  if (!day) return base;
  var minSrPct = Math.round((_keyStatsCache.min_success_rate || 0.5) * 100);
  return base + '\n\n' + t('settings.keyStatAutoDisablePolicy', { day: day, pct: minSrPct });
}

/** Render the stats sub-row HTML for one key card.
 *
 *  Returns '' for never-called keys (no row yet) AND for cards whose
 *  current input value is blank — both cases are handled by the
 *  .stg-key-card--blank modifier on the wrapper, which collapses this
 *  sub-row entirely. We still emit content for "idle" rows that have a
 *  value set so the user can flip the override toggle pre-emptively. */
function _renderKeyCardStatsHTML(provIdx, keyIdx) {
  var row = _getKeyStatRowFor(provIdx, keyIdx);

  var total = row ? (row.total || 0) : 0;
  var succ = row ? (row.success || 0) : 0;
  var fail = row ? (row.failure || 0) : 0;
  var rl429 = row ? (row.rate_limited || 0) : 0;
  var cons429 = row ? (row.consecutive_429 || 0) : 0;
  var srTxt = row ? _fmtSuccessRate(row.success_rate) : '—';
  var enabled = row ? !!row.enabled : true;
  var autoOff = !!(row && row.auto_disabled && row.override == null);
  var exhausted = !!(row && row.exhausted);
  var lastResort = !!(row && row.last_resort);

  var badge = '';
  if (row && row.override === false) badge = '<span class="stg-keystat-badge off">' + t('settings.keyStatOverrideOff') + '</span>';
  else if (row && row.override === true) badge = '<span class="stg-keystat-badge on">' + t('settings.keyStatOverrideOn') + '</span>';
  else if (lastResort) badge = '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatLastResortTip')) + '">' + t('settings.keyStatLastResort') + '</span>';
  else if (exhausted) badge = '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatExhaustedTip')) + '">' + t('settings.keyStatExhausted') + '</span>';
  else if (autoOff) badge = '<span class="stg-keystat-badge warn">' + t('settings.keyStatAutoOff') + '</span>';

  // Informational streak chip — 429s never disable the key (owner policy
  // 2026-07-29), so there is no threshold to approach; ≥10 is just "worth
  // telling the user the upstream is pushing back".
  var streakBadge = '';
  if (!exhausted && cons429 >= 10) {
    streakBadge = '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStat429StreakTip')) + '">' + t('settings.keyStat429Streak', { n: cons429 }) + '</span>';
  }

  // Per-model billing-stops (aggregating-gateway isolation): a quota-dead
  // model is stopped WITHOUT taking down sibling models on the same key —
  // show exactly which models are stopped and why.
  var em = (row && row.exhausted_models) ? Object.keys(row.exhausted_models) : [];
  var emBadge = '';
  if (em.length) {
    var emReasons = em.map(function(m) {
      var r = String(row.exhausted_models[m] || '').slice(0, 80);
      return m + (r ? ': ' + r : '');
    }).join('\n');
    emBadge = '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatModelExhaustedTip', { reasons: emReasons })) + '">' + escapeHtml(t('settings.keyStatModelExhausted', { models: em.join(', ') })) + '</span>';
  }

  // A manual ON keeps winning over billing-stops (user supremacy), but a
  // stale override must not silently defeat a FRESH quota error — surface
  // the conflict instead of letting the key look healthy while the
  // provider reports it out of credit.
  var conflictBadge = '';
  if (row && row.override === true && (exhausted || em.length)) {
    conflictBadge = '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatOverrideVsExhaustedTip')) + '">' + escapeHtml(t('settings.keyStatOverrideVsExhausted')) + '</span>';
  }

  var showErr = row && row.last_error && (fail > 0 || exhausted);
  var lastErr = showErr ? ('<span class="stg-keystat-err" title="' + escapeHtml(row.last_error) + '">' + t('settings.keyStatLastError') + '</span>') : '';

  var rateTitle = total > 0
    ? t('settings.keyStatRateTip', { succ: succ, total: total })
    : t('settings.keyStatNoCallsTip');
  var countChip = total > 0
    ? '<span class="stg-keystat-count" title="' + escapeHtml(t('settings.keyStatCountTip')) + '">' + t('settings.keyStatCount', { n: total }) + '</span>'
    : '<span class="stg-keystat-count" title="' + escapeHtml(t('settings.keyStatNoCallsTip')) + '">—</span>';

  return '<span class="stg-keystat-metrics">' +
      '<span class="stg-keystat-rate" title="' + rateTitle + '">' + srTxt + '</span>' +
      countChip +
      (fail > 0 ? '<span class="stg-keystat-fail" title="' + escapeHtml(t('settings.keyStatFailTip')) + '">' + t('settings.keyStatFail', { n: fail }) + '</span>' : '') +
      (rl429 > 0 ? '<span class="stg-keystat-429" title="' + escapeHtml(t('settings.keyStat429Tip')) + '">' + t('settings.keyStat429', { n: rl429 }) + '</span>' : '') +
    '</span>' +
    streakBadge + emBadge + conflictBadge + badge + lastErr +
    '<span class="stg-keystat-actions">' +
      '<label class="stg-toggle stg-key-toggle" title="' + escapeHtml(t('settings.keyStatToggleTip')) + '">' +
        '<input type="checkbox"' + (enabled ? ' checked' : '') +
            ' onchange="_onKeyToggle(' + provIdx + ',' + keyIdx + ',this.checked)">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
      '</label>' +
      (row && row.override != null
        ? '<button class="stg-btn-link" title="' + escapeHtml(t('settings.keyStatClearOverrideTip')) + '" onclick="_onKeyClearOverride(' + provIdx + ',' + keyIdx + ')">' + t('settings.keyStatReset') + '</button>'
        : '') +
    '</span>';
}

/** Refresh the stats sub-row + colour-bar class for every card of one
 *  provider, in place — does NOT re-render the editor input (which would
 *  blow away the user's cursor / show-hide state). */
function _renderProviderKeyStats(provIdx) {
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var field = card.querySelector('.stg-keys-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return;

  // Refresh the section-title tooltip (day label may have just loaded).
  var p = _stgProviders[provIdx];
  var brand = p && p.brand;
  var info = field.querySelector('.stg-keys-info');
  if (info) {
    var helpTxt = _keyStatsHelpText(brand === 'local');
    info.setAttribute('title', helpTxt);
    info.setAttribute('aria-label', helpTxt);
  }

  var cards = field.querySelectorAll('.stg-key-card');
  for (var i = 0; i < cards.length; i++) {
    var statRow = _getKeyStatRowFor(provIdx, i);
    var stateCls = _keyStatsClass(statRow);

    // Remove any previous state class, keep --blank.
    var classes = (cards[i].className || '').split(/\s+/).filter(function(c) {
      return c && c.indexOf('stg-keystat-') !== 0;
    });
    classes.push(stateCls);
    cards[i].className = classes.join(' ');

    var statsEl = cards[i].querySelector('.stg-key-card-stats');
    if (statsEl) statsEl.innerHTML = _renderKeyCardStatsHTML(provIdx, i);
  }
}

/** Toggle a single key on/off for today. Sends an explicit override. */
function _onKeyToggle(provIdx, keyIdx, enabled) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var providerId = p.id || 'default';
  var keyName = providerId + '_key_' + keyIdx;
  Api.dispatch.keyOverride({ provider_id: providerId, key_name: keyName, enabled: !!enabled })
    .then(function(data) {
      if (data && data.row) {
        if (!_keyStatsCache.providers[providerId]) _keyStatsCache.providers[providerId] = {};
        _keyStatsCache.providers[providerId][keyName] = data.row;
        _renderProviderKeyStats(provIdx);
      }
    })
    .catch(function(e) {
      debugLog('[Settings] Key toggle failed: ' + (e && e.message), 'error');
    });
}

// ── Per-model runtime health (success rate + error throttling) ─────────
//
// The dispatcher throttles a (key, wire-model) slot after repeated errors
// (cooldown_until / cooldown_reason in lib/llm_dispatch/slot.py). That state
// was previously invisible unless you opened the per-key rows or the raw
// logs. /api/v1/dispatch/model-health folds slots per (provider, wire id);
// here we fold one step further — a model CARD covers a whole request-id
// pool, so the card's strip merges every wire id the card can route to.
//
// Refresh is in-place (only the .stg-mcard-health strips), never a full
// _renderProvidersTab() — a full re-render would blow away an open edit form.

var _modelHealthCache = {};      // {provider_id: {wire_model: row}}
var _modelHealthTs = 0;          // Date.now() of the last successful fetch
var _modelHealthLoading = false;
var _modelHealthTimer = null;
var _modelHealthInflight = false;

/** Fetch per-model health and refresh visible card strips in place. */
function _loadModelHealth() {
  if (_modelHealthLoading) return Promise.resolve(_modelHealthCache);
  _modelHealthLoading = true;
  return Api.dispatch.modelHealth()
    .then(function(data) {
      if (data && typeof data === 'object' && data.providers) {
        _modelHealthCache = data.providers || {};
        _modelHealthTs = Date.now();
      }
    })
    .catch(function(e) {
      debugLog('[Settings] Failed to load model health: ' + (e && e.message), 'warning');
    })
    .finally(function() {
      _modelHealthLoading = false;
      _refreshAllModelCardHealth();
    });
}

/** Poll model health while Settings is open (mirrors the endpoint-metrics
 *  poller cadence so a "cooling 42s" chip counts down toward recovery). */
function _startModelHealthPolling() {
  if (_modelHealthTimer) return;
  _loadModelHealth();
  _modelHealthTimer = setInterval(function() {
    if (_modelHealthInflight) return;
    _modelHealthInflight = true;
    _loadModelHealth().finally(function() { _modelHealthInflight = false; });
  }, 10000);
}

function _stopModelHealthPolling() {
  if (_modelHealthTimer) {
    clearInterval(_modelHealthTimer);
    _modelHealthTimer = null;
  }
}

/** The wire-id pool a card routes to — mirrors the backend contract:
 *  explicit request_ids win; otherwise [model_id] + legacy aliases. */
function _modelWireIds(m) {
  if (m.request_ids && m.request_ids.length) return m.request_ids.slice();
  var ids = m.model_id ? [m.model_id] : [];
  return ids.concat(m.aliases || []);
}

/** Fold the health rows of every wire id one card can route to into a
 *  single aggregate. Returns null when no wire id has any row (model never
 *  routed since boot → the strip renders the muted "no traffic" state). */
function _modelCardHealthRow(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  var m = p && p.models && p.models[modelIdx];
  if (!m) return null;
  var pid = p.id || 'default';
  var rows = _modelHealthCache[pid] || {};
  var ids = _modelWireIds(m);
  var elapsed = _modelHealthTs ? (Date.now() - _modelHealthTs) / 1000 : 0;
  var agg = null;
  for (var i = 0; i < ids.length; i++) {
    var r = rows[ids[i]];
    if (!r) continue;
    if (!agg) {
      agg = { slots: 0, available_slots: 0, total_requests: 0, total_errors: 0,
              contention_errors: 0, consecutive_errors: 0, inflight: 0,
              cooldown_remaining_s: 0, cooldown_reason: '',
              last_error_msg: '', last_error_ts: 0, success_rate: null,
              verdict: /** @type {any} */ (null) };
    }
    agg.slots += r.slots || 0;
    agg.available_slots += r.available_slots || 0;
    agg.total_requests += r.total_requests || 0;
    agg.total_errors += r.total_errors || 0;
    agg.contention_errors += r.contention_errors || 0;
    agg.inflight += r.inflight || 0;
    if ((r.consecutive_errors || 0) > agg.consecutive_errors) {
      agg.consecutive_errors = r.consecutive_errors;
    }
    // Count the server-reported remaining down by the time since the fetch,
    // so a chip rendered between polls doesn't overstate the cooldown.
    var rem = Math.max(0, (r.cooldown_remaining_s || 0) - elapsed);
    if (rem > agg.cooldown_remaining_s) {
      agg.cooldown_remaining_s = rem;
      agg.cooldown_reason = r.cooldown_reason || '';
    }
    if ((r.last_error_ts || 0) > agg.last_error_ts) {
      agg.last_error_ts = r.last_error_ts;
      agg.last_error_msg = r.last_error_msg || '';
    }
  }
  if (!agg) return null;
  agg.success_rate = (agg.total_requests >= 3)
    ? Math.max(0, 1 - agg.total_errors / agg.total_requests)
    : null;

  /* THE availability verdict comes from the pool rule (core/model_health.js)
   * — "any usable slot ⇒ usable model" — NOT from the pooled success_rate
   * above. The rate is INFORMATIONAL: it tells the user how lossy the pool
   * is, and it must NOT decide the strip's colour. A gateway that redeploys
   * one upstream (the yuju daily builds) would otherwise drive a model the
   * dispatcher still serves into the red. Degrade to the legacy success_rate
   * judgment only if the shared module failed to load (stale bundle). */
  if (typeof foldRuntimeHealth === 'function') {
    var _verdictRows = [];
    for (var _i = 0; _i < ids.length; _i++) {
      var _r = rows[ids[_i]];
      if (_r) {
        _verdictRows.push({
          wire_id: ids[_i],
          available_slots: _r.available_slots || 0,
          total_requests: _r.total_requests || 0,
          total_errors: _r.total_errors || 0,
          cooldown_reason: _r.cooldown_reason || '',
          last_error_msg: _r.last_error_msg || '',
        });
      }
    }
    agg.verdict = foldRuntimeHealth(_verdictRows);
  } else {
    agg.verdict = null;
  }
  return agg;
}

var _MH_REASON_KEYS = {
  rate_limit: 'settings.mhReasonRateLimit',
  upstream:   'settings.mhReasonUpstream',
  error:      'settings.mhReasonError',
  quota:      'settings.mhReasonQuota',
  contention: 'settings.mhReasonContention',
};

/** Inner HTML of one card's health strip (state class lives on the strip). */
function _modelCardHealthHTML(provIdx, modelIdx) {
  // Before the first successful fetch there is no signal at all — render an
  // empty (hidden) strip rather than a misleading "no traffic" on every card.
  if (!_modelHealthTs) return '';
  var agg = _modelCardHealthRow(provIdx, modelIdx);
  if (!agg || agg.total_requests === 0 && agg.cooldown_remaining_s <= 0) {
    return '<span class="stg-mh-chip muted">' + escapeHtml(t('settings.mhNoTraffic')) + '</span>';
  }
  var html = '';
  if (agg.cooldown_remaining_s > 0) {
    var reason = _MH_REASON_KEYS[agg.cooldown_reason]
      ? t(_MH_REASON_KEYS[agg.cooldown_reason]) : agg.cooldown_reason;
    html += '<span class="stg-mh-chip cool" title="' + escapeHtml(agg.last_error_msg || '') + '">' +
      '⏳ ' + escapeHtml(t('settings.mhCooldown', { s: Math.ceil(agg.cooldown_remaining_s) })) +
      (reason ? ' · ' + escapeHtml(reason) : '') + '</span>';
  }
  if (agg.success_rate != null) {
    var pct = Math.round(agg.success_rate * 100);
    var cls = pct >= 98 ? 'good' : (pct >= 90 ? 'ok' : 'warn');
    html += '<span class="stg-mh-chip ' + cls + '" title="' +
      escapeHtml(t('settings.mhRequestsTip', { n: agg.total_requests })) + '">' +
      escapeHtml(t('settings.mhSuccessRate')) + ' ' + pct + '%</span>';
  }
  // External shared-project contention — rendered SEPARATELY from the
  // success rate: the pipe was filled by other tenants, not by this model
  // failing (2026-07-28: 782 such 429s made a healthy model look 24%).
  if (agg.contention_errors > 0) {
    html += '<span class="stg-mh-chip muted" title="' +
      escapeHtml(t('settings.mhContentionTip')) + '">' +
      escapeHtml(t('settings.mhContention', { n: agg.contention_errors })) + '</span>';
  }
  if (agg.cooldown_remaining_s <= 0 && agg.consecutive_errors > 0) {
    html += '<span class="stg-mh-chip warn" title="' + escapeHtml(agg.last_error_msg || '') + '">' +
      escapeHtml(t('settings.mhConsecErrors', { n: agg.consecutive_errors })) + '</span>';
  }
  if (agg.inflight > 0) {
    html += '<span class="stg-mh-chip muted">' +
      escapeHtml(t('settings.mhInflight', { n: agg.inflight })) + '</span>';
  }
  return html;
}

/** State class for the strip wrapper — drives the left accent of the row.
 *  Colour = the pool-verdict (any usable slot ⇒ usable), NOT the pooled
 *  success rate. A model with 8 dead slots + 1 live one is 'degraded'
 *  (amber, still working), never 'warn' (red). Falls back to the legacy
 *  rate-based judgement only when the shared module is unavailable. */
function _modelCardHealthCls(provIdx, modelIdx) {
  var agg = _modelCardHealthRow(provIdx, modelIdx);
  if (!agg) return 'muted';
  if (agg.cooldown_remaining_s > 0) return 'cool';
  if (agg.verdict) {
    var lv = agg.verdict.level;
    if (lv === 'ok') return 'good';
    if (lv === 'degraded') return 'ok';
    if (lv === 'down') return 'warn';
    return 'muted';   // unknown / skipped / not_logged_in — no colour claim
  }
  // Legacy fallback (shared module not loaded).
  if (agg.success_rate != null && agg.success_rate < 0.9) return 'warn';
  if (agg.consecutive_errors > 0) return 'warn';
  if (agg.total_requests > 0) return 'good';
  return 'muted';
}

/** Refresh every visible health strip in place (never re-render the tab). */
function _refreshAllModelCardHealth() {
  var strips = document.querySelectorAll('.stg-mcard-health');
  for (var i = 0; i < strips.length; i++) {
    var pi = parseInt(strips[i].getAttribute('data-prov'), 10);
    var mi = parseInt(strips[i].getAttribute('data-model'), 10);
    if (isNaN(pi) || isNaN(mi)) continue;
    strips[i].innerHTML = _modelCardHealthHTML(pi, mi);
    strips[i].className = 'stg-mcard-health ' + _modelCardHealthCls(pi, mi);
  }
}

/** Clear the manual override, reverting to automatic health-based logic. */
/** Clear the manual override, reverting to automatic health-based logic. */
function _onKeyClearOverride(provIdx, keyIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var providerId = p.id || 'default';
  var keyName = providerId + '_key_' + keyIdx;
  Api.dispatch.keyOverride({ provider_id: providerId, key_name: keyName, enabled: null })
    .then(function(data) {
      if (data && data.row) {
        if (!_keyStatsCache.providers[providerId]) _keyStatsCache.providers[providerId] = {};
        _keyStatsCache.providers[providerId][keyName] = data.row;
        _renderProviderKeyStats(provIdx);
      }
    })
    .catch(function(e) {
      debugLog('[Settings] Key override clear failed: ' + (e && e.message), 'error');
    });
}
