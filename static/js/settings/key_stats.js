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
          max_consecutive_429: data.max_consecutive_429 || 100,
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
  var max429 = _keyStatsCache.max_consecutive_429 || 100;
  var minSrPct = Math.round((_keyStatsCache.min_success_rate || 0.5) * 100);
  return base + '\n\n' + t('settings.keyStatAutoDisablePolicy', { day: day, n: max429, pct: minSrPct });
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
  var max429 = _keyStatsCache.max_consecutive_429 || 100;

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
  else if (exhausted) badge = '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatExhaustedTip', { n: max429 })) + '">' + t('settings.keyStatExhausted') + '</span>';
  else if (autoOff) badge = '<span class="stg-keystat-badge warn">' + t('settings.keyStatAutoOff') + '</span>';

  var streakBadge = '';
  if (!exhausted && cons429 >= Math.max(10, max429 / 2)) {
    streakBadge = '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStat429StreakTip', { max: max429 })) + '">' + t('settings.keyStat429Streak', { n: cons429 }) + '</span>';
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
      (rl429 > 0 ? '<span class="stg-keystat-429" title="' + escapeHtml(t('settings.keyStat429Tip', { max: max429 })) + '">' + t('settings.keyStat429', { n: rl429 }) + '</span>' : '') +
    '</span>' +
    streakBadge + badge + lastErr +
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
