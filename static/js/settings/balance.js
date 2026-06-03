/* ═══════════════════════════════════════════════════════════════════
   settings/balance — extracted from settings.js (split 2026-05-28)

   Provider balance check + auto-poll + badge display.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Check the balance/billing for a provider via its balance_url.
 * Calls the backend proxy endpoint which handles auth + network.
 */
async function _checkProviderBalance(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var resultDiv = document.getElementById('stgBalanceResult_' + provIdx);
  if (!resultDiv) return;

  // Use explicit balance_url, or guess from base_url
  var balanceUrl = p.balance_url || _guessBalanceUrl(p.base_url || '');
  if (!balanceUrl) {
    resultDiv.innerHTML = '<span class="stg-balance-err">未配置余额查询地址</span>';
    return;
  }
  if (!p.api_keys || p.api_keys.length === 0) {
    resultDiv.innerHTML = '<span class="stg-balance-err">未配置 API 密钥</span>';
    return;
  }

  resultDiv.innerHTML = '<span class="stg-balance-loading">⏳ 查询中…</span>';

  try {
    var data = await Api.providers.balance({ balance_url: balanceUrl, api_key: p.api_keys[0] });
    if (!data || !data.ok) {
      resultDiv.innerHTML = '<span class="stg-balance-err">❌ ' + escapeHtml((data && data.error) || '未知错误') + '</span>';
      return;
    }

    // Render balance info using unified format
    var info = data.balance;
    resultDiv.innerHTML = _renderBalanceInfo(info);
    // Cache balance for badge display
    _balanceCache[provIdx] = { info: info, ts: Date.now() };
    _updateBalanceBadge(provIdx, info);
    // If we used a guessed URL and it worked, persist it to the provider
    if (!p.balance_url && balanceUrl) {
      p.balance_url = balanceUrl;
      _renderProvidersTab();
    }
  } catch (e) {
    resultDiv.innerHTML = '<span class="stg-balance-err">❌ 网络错误: ' + escapeHtml(e.message) + '</span>';
  }
}

/**
 * Render balance info HTML from the unified backend format.
 * Handles: OpenAI (limit+used), DeepSeek (balance_infos), OpenRouter (credits), generic, raw.
 */
function _renderBalanceInfo(info) {
  var html = '<div class="stg-balance-info">';

  if (info.limit_usd != null && info.used_usd != null) {
    // ── Format with limit + used (OpenAI, OpenRouter) ──
    var used = info.used_usd;
    var limit = info.limit_usd;
    var remaining = info.balance_usd != null ? info.balance_usd : (limit - used);
    var pct = limit > 0 ? Math.round((used / limit) * 100) : 0;
    var barColor = pct > 90 ? '#ef4444' : pct > 70 ? '#f59e0b' : '#22c55e';
    html += '<div class="stg-balance-bar-wrap">' +
      '<div class="stg-balance-bar" style="width:' + Math.min(pct, 100) + '%;background:' + barColor + '"></div>' +
    '</div>';
    html += '<div class="stg-balance-nums">' +
      '<span>已用: <b>$' + used.toFixed(2) + '</b></span>' +
      '<span>剩余: <b>$' + remaining.toFixed(2) + '</b></span>' +
      '<span>额度: <b>$' + limit.toFixed(2) + '</b></span>' +
    '</div>';
  } else if (info.balance_usd != null) {
    // ── Balance-only format (DeepSeek, generic) ──
    var bal = info.balance_usd;
    var barColor = bal > 10 ? '#22c55e' : bal > 2 ? '#f59e0b' : '#ef4444';
    html += '<div class="stg-balance-nums">';
    html += '<span>余额: <b style="color:' + barColor + '">$' + bal.toFixed(2) + '</b></span>';
    if (info.currency && info.currency !== 'USD' && info.balance_local != null) {
      html += '<span>（' + info.currency + ' ' + info.balance_local.toFixed(2) + '）</span>';
    }
    if (info.granted_balance != null) {
      html += '<span>赠送: ' + info.currency + ' ' + info.granted_balance.toFixed(2) + '</span>';
    }
    if (info.is_available === false) {
      html += '<span style="color:#ef4444;font-weight:800">⚠ 余额不足</span>';
    }
    html += '</div>';
  } else if (info.raw) {
    // ── Raw fallback ──
    html += '<span class="stg-balance-raw">' + escapeHtml(JSON.stringify(info.raw)) + '</span>';
  } else {
    html += '<span class="stg-balance-raw">' + escapeHtml(JSON.stringify(info)) + '</span>';
  }

  html += '</div>';
  return html;
}

/**
 * Format a balance value for compact badge display.
 */
function _fmtBalanceBadge(info) {
  var b = null;
  if (info.balance_usd != null) {
    b = info.balance_usd;
  } else if (info.limit_usd != null && info.used_usd != null) {
    b = info.limit_usd - info.used_usd;
  }
  if (b == null) return null;
  if (b >= 1000) return '$' + (b / 1000).toFixed(1) + 'k';
  if (b >= 100) return '$' + Math.round(b);
  return '$' + b.toFixed(2);
}

/**
 * Update the balance badge in the provider header card.
 */
function _updateBalanceBadge(provIdx, info) {
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var badges = card.querySelector('.stg-provider-badges');
  if (!badges) return;

  // Remove existing balance badge
  var existing = badges.querySelector('.stg-badge-balance');
  if (existing) existing.remove();

  var text = _fmtBalanceBadge(info);
  if (!text) return;

  var bal = info.balance_usd != null ? info.balance_usd :
            (info.limit_usd != null ? info.limit_usd - (info.used_usd || 0) : null);
  var colorClass = bal != null ? (bal > 10 ? 'ok' : bal > 2 ? 'warn' : 'low') : 'ok';

  var span = document.createElement('span');
  span.className = 'stg-badge stg-badge-balance stg-badge-bal-' + colorClass;
  span.textContent = '\uD83D\uDCB0 ' + text;
  span.title = t('settings.balanceClickRefresh');
  span.style.cursor = 'pointer';
  span.onclick = function(e) {
    e.stopPropagation();
    _checkProviderBalance(provIdx);
  };
  badges.appendChild(span);
}

// ── Balance auto-polling ──
var _balanceCache = {};  // provIdx → { info, ts }
var _balancePollTimer = null;
var _BALANCE_POLL_INTERVAL = 3 * 60 * 1000;  // 3 minutes

/**
 * Start auto-polling balance for all providers that have balance_url and api_keys.
 * Called when settings panel opens.
 */
function _startBalancePolling() {
  _stopBalancePolling();
  // Immediate first check for all eligible providers
  _pollAllBalances();
  _balancePollTimer = setInterval(_pollAllBalances, _BALANCE_POLL_INTERVAL);
}

function _stopBalancePolling() {
  if (_balancePollTimer) {
    clearInterval(_balancePollTimer);
    _balancePollTimer = null;
  }
}

async function _pollAllBalances() {
  for (var pi = 0; pi < _stgProviders.length; pi++) {
    var p = _stgProviders[pi];
    if (!p.balance_url || !p.api_keys || p.api_keys.length === 0) continue;
    if (p.enabled === false) continue;

    // Skip if recently checked (within 2 minutes)
    var cached = _balanceCache[pi];
    if (cached && (Date.now() - cached.ts) < 120000) {
      _updateBalanceBadge(pi, cached.info);
      continue;
    }

    // Fire balance check (don't await all — stagger slightly)
    (function(idx) {
      setTimeout(function() { _checkProviderBalanceSilent(idx); }, idx * 500);
    })(pi);
  }
}

/**
 * Silent balance check — updates badge without touching the result div.
 * Used by auto-polling.
 */
async function _checkProviderBalanceSilent(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.balance_url || !p.api_keys || p.api_keys.length === 0) return;

  try {
    var data = await Api.providers.balance({ balance_url: p.balance_url, api_key: p.api_keys[0] });
    if (!data || !data.ok) return;

    var info = data.balance;
    _balanceCache[provIdx] = { info: info, ts: Date.now() };
    _updateBalanceBadge(provIdx, info);

    // Also update the detail result div if it exists and is visible
    var resultDiv = document.getElementById('stgBalanceResult_' + provIdx);
    if (resultDiv && resultDiv.offsetParent !== null) {
      resultDiv.innerHTML = _renderBalanceInfo(info);
    }
  } catch (e) {
    // Silent — don't bother the user with polling errors
    debugLog('[Balance] Silent poll failed for provider ' + provIdx + ': ' + e.message, 'debug');
  }
}

