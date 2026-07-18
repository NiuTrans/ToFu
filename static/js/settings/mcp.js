/* ═══════════════════════════════════════════════════════════════════
   settings/mcp — extracted from settings.js (split 2026-05-28)

   MCP catalog UI: render, install modal, save server, reconnect.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  MCP Apps Tab — App Store style UI
// ══════════════════════════════════════════════════════

/** Cached catalog & state */
var _mcpCatalog = [];
var _mcpActiveCategory = 'all';
var _mcpScope = 'all';         // 'all' | 'installed' | 'available'
var _mcpSearchQuery = '';
var _mcpInstallTarget = null;  // CatalogEntry being installed
var _mcpInstallIsReinstall = false;  // true = editing existing (stored env will be honoured)
var _mcpBreakerRefreshTimer = null;  // single-shot re-fetch while a breaker is counting down
var _mcpBreakerTickTimer = null;     // 1s interval that ticks the live "retry in N" countdowns

/**
 * Load MCP tab data — fetch catalog with install/connect status.
 */
async function _populateMcpTab() {
  var grid = document.getElementById('mcpCatalogGrid');
  if (grid) grid.innerHTML = '<p class="stg-loading">' + escapeHtml(t('mcp.loading')) + '</p>';
  try {
    var r = await Api.mcp.catalogList();
    if (!r || !r.ok) throw new Error('HTTP ' + (r ? r.status : 'no response'));
    var data = await r.json();
    _mcpCatalog = data.catalog || [];

    _renderMcpCategoryBar();
    _renderMcpCatalog();
    _renderMcpInstalled();
    _mcpUpdateToolCount();
    // Keep the per-turn context capsule's MCP chips in sync with the live
    // connection state (install / connect / uninstall all funnel through a
    // _populateMcpTab refresh). See info-rail.js::refreshMcpRailState.
    if (typeof refreshMcpRailState === 'function') refreshMcpRailState();
  } catch (e) {
    if (grid) grid.innerHTML = '<p class="stg-empty">' + escapeHtml(t('mcp.loadFailed', { err: e.message })) + '</p>';
    debugLog('[MCP] Failed to load catalog: ' + e.message, 'error');
  }
}

/** Update the tool count badge. */
function _mcpUpdateToolCount() {
  var badge = document.getElementById('mcpToolCount');
  if (!badge) return;
  var total = 0;
  _mcpCatalog.forEach(function(e) { total += (e.tools_count || 0); });
  badge.textContent = t('mcp.toolsCount', { n: total });
}

/** Render category filter pills. */
function _renderMcpCategoryBar() {
  var bar = document.getElementById('mcpCategoryBar');
  if (!bar) return;
  var cats = {};
  _mcpCatalog.forEach(function(e) {
    var c = e.category || 'Other';
    cats[c] = (cats[c] || 0) + 1;
  });
  var html = '<button class="mcp-cat-pill' + (_mcpActiveCategory === 'all' ? ' active' : '') + '" onclick="_mcpSetCategory(\'all\')">' + escapeHtml(t('mcp.scopeAll')) + ' <span class="mcp-cat-count">' + _mcpCatalog.length + '</span></button>';
  var order = ['Development','Data & DB','Communication','Search & Web','Productivity','DevOps','Finance','Design','Other','Custom'];
  order.forEach(function(c) {
    if (!cats[c]) return;
    html += '<button class="mcp-cat-pill' + (_mcpActiveCategory === c ? ' active' : '') + '" onclick="_mcpSetCategory(\'' + c + '\')">' + escapeHtml(c) + ' <span class="mcp-cat-count">' + cats[c] + '</span></button>';
  });
  bar.innerHTML = html;
}

function _mcpSetCategory(cat) {
  _mcpActiveCategory = cat;
  _renderMcpCategoryBar();
  _renderMcpCatalog();
}

/** Switch the installed/available scope filter and re-render. */
function _mcpSetScope(scope) {
  _mcpScope = scope;
  var tabs = document.getElementById('mcpScopeTabs');
  if (tabs) {
    tabs.querySelectorAll('.skills-scope-tab').forEach(function(t) {
      t.classList.toggle('active', t.getAttribute('data-scope') === scope);
    });
  }
  _renderMcpCatalog();
}

function _mcpFilterCatalog(query) {
  _mcpSearchQuery = (query || '').toLowerCase().trim();
  _renderMcpCatalog();
}

/** Filter catalog entries by active category + search query. */
function _mcpFilteredCatalog() {
  return _mcpCatalog.filter(function(e) {
    if (_mcpActiveCategory !== 'all' && e.category !== _mcpActiveCategory) return false;
    // Scope filter: an entry counts as "installed" if it is configured
    // (installed) regardless of live connection state.
    if (_mcpScope === 'installed' && !e.installed) return false;
    if (_mcpScope === 'available' && e.installed) return false;
    if (_mcpSearchQuery) {
      var hay = (e.name + ' ' + e.description + ' ' + (e.tags || []).join(' ')).toLowerCase();
      return hay.indexOf(_mcpSearchQuery) !== -1;
    }
    return true;
  });
}

/**
 * Format a number of seconds-until-retry into a short human label.
 * `secs <= 0` → "retrying…"; under a minute → "retry in Ns"; else
 * "retry in N min" (rounded up).
 */
function _mcpRetryLabel(secs) {
  secs = Math.max(0, Math.round(secs || 0));
  if (secs <= 0) return t('mcp.retryNow');
  if (secs < 60) return t('mcp.retryInSec').replace('{n}', String(secs));
  return t('mcp.retryInMin').replace('{n}', String(Math.ceil(secs / 60)));
}

/**
 * Build the inner HTML for a live countdown span. The span carries the
 * absolute retry deadline (epoch ms) in ``data-retry-at`` so a 1s ticker
 * (`_mcpTickBreakers`) can recompute the remaining time and update the
 * text in place — no full grid re-render. Returns '' for no breaker.
 *
 * `breaker` shape (from the backend): {failures, retry_in, next_retry_ts}.
 * We derive the deadline from `retry_in` relative to *now* rather than
 * trusting `next_retry_ts` (server/client clocks may differ).
 */
function _mcpBreakerCountdownSpan(breaker) {
  if (!breaker) return '';
  var secs = Math.max(0, breaker.retry_in || 0);
  var deadline = Date.now() + secs * 1000;
  return '<span class="mcp-breaker-countdown" data-retry-at="' + deadline + '">' +
    escapeHtml(_mcpRetryLabel(secs)) + '</span>';
}

/** Render the main catalog grid. */
function _renderMcpCatalog() {
  var grid = document.getElementById('mcpCatalogGrid');
  if (!grid) return;
  var items = _mcpFilteredCatalog();
  if (items.length === 0) {
    var emptyMsg = _mcpScope === 'installed' ? t('mcp.emptyInstalled')
      : _mcpScope === 'available' ? t('mcp.emptyAvailable')
      : t('mcp.emptyNoMatch');
    grid.innerHTML = '<p class="stg-empty">' + emptyMsg + '</p>';
    return;
  }
  // Show featured first, then alphabetical
  items.sort(function(a, b) {
    if (a.featured && !b.featured) return -1;
    if (!a.featured && b.featured) return 1;
    return (a.name || '').localeCompare(b.name || '');
  });
  var REPO_SVG = '<svg width="11" height="11" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>';
  var html = '';
  items.forEach(function(e) {
    var installed = e.installed;
    var connected = e.connected;
    // A breaker is "active" only when the server is installed but not
    // currently connected and its automatic reconnect is failing.
    var breaker = (!connected && installed) ? e.breaker : null;
    // Credential health is a SECOND axis: a server can be connected (live
    // subprocess) yet its stored session cookie/token has expired, so every
    // real tool call fails. Only surface it while connected — a disconnected
    // card already tells its own story via breaker / idle state.
    var credExpired = connected && e.cred_health && e.cred_health.status === 'expired';
    var stateClass = connected ? (credExpired ? ' connected cred-expired' : ' connected')
      : breaker ? ' installed reconnecting'
      : installed ? ' installed' : '';
    html += '<div class="mcp-app-card' + stateClass + '">';
    html += '<div class="mcp-app-icon">' + (e.icon || Icon('plug', 26)) + '</div>';
    html += '<div class="mcp-app-name"><span class="mcp-app-name-text">' + escapeHtml(e.name) + '</span>';
    if (credExpired) {
      html += '<span class="mcp-app-status cred-expired" title="' +
        escapeHtml(t('mcp.credExpiredTitle')) + '">' +
        Icon('alertTriangle', 12) + ' ' + escapeHtml(t('mcp.credExpired')) + '</span>';
    } else if (connected) {
      html += '<span class="mcp-app-status on"><span class="dot"></span>' + escapeHtml(t('mcp.statusOn')) + '</span>';
    } else if (breaker) {
      html += '<span class="mcp-app-status reconnecting" title="' +
        escapeHtml(t('mcp.reconnecting') + ' · ' + t('mcp.retryFailCount').replace('{n}', String(breaker.failures || 0))) +
        '">⟳ ' + _mcpBreakerCountdownSpan(breaker) + '</span>';
    } else if (installed) {
      html += '<span class="mcp-app-status off">' + escapeHtml(t('mcp.statusIdle')) + '</span>';
    }
    html += '</div>';
    html += '<div class="mcp-app-desc">' + escapeHtml(e.description || '') + '</div>';
    // Footer: repo link (left) + tools count / action buttons (right)
    html += '<div class="mcp-app-footer">';
    if (e.url) {
      html += '<a class="mcp-app-repo" href="' + escapeHtml(e.url) + '" target="_blank" rel="noopener" title="' + escapeHtml(t('mcp.repoTitle')) + '">' + REPO_SVG + ' ' + escapeHtml(t('mcp.repo')) + '</a>';
    } else {
      html += '<span></span>';
    }
    html += '<div class="mcp-app-action">';
    if (connected) {
      if (credExpired) {
        // The subprocess is live but the stored cookie/token no longer
        // authenticates. Offer a one-click path to re-enter credentials —
        // the same reinstall modal used for editing an installed server
        // (existing env prefills, blank leaves it unchanged).
        html += '<button class="btn btn-primary btn-xs" onclick="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\', true)" title="' + escapeHtml(t('mcp.credExpiredTitle')) + '">' + escapeHtml(t('mcp.updateCreds')) + '</button>';
      }
      if (e.server_version) {
        html += '<span class="mcp-app-version" title="' + escapeHtml((e.server_impl_name || e.id) + ' v' + e.server_version) + '">v' + escapeHtml(e.server_version) + '</span>';
      }
      if (e.tools_count) {
        html += '<span class="mcp-app-tools-count">' + escapeHtml(t('mcp.toolsCount', { n: (e.tools_count || 0) })) + '</span>';
      }
      html += '<button class="btn btn-secondary btn-xs" onclick="_mcpUninstall(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.uninstallTitle')) + '">' + escapeHtml(t('mcp.uninstall')) + '</button>';
    } else if (installed) {
      if (breaker) {
        html += '<span class="mcp-app-reconnecting-note" title="' +
          escapeHtml(t('mcp.reconnecting')) + '">⟳ ' +
          _mcpBreakerCountdownSpan(breaker) + '</span>';
      }
      if (e.custom) {
        // Custom servers have no catalog entry, so the catalog install
        // endpoint would 404. Reconnect straight through connectOne.
        html += '<button class="btn btn-primary btn-xs" onclick="_mcpReconnect(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.connectCustomTitle')) + '">' + escapeHtml(t('mcp.connect')) + '</button>';
      } else {
        html += '<button class="btn btn-primary btn-xs" onclick="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\', true)" title="' + escapeHtml(t('mcp.connectReinstallTitle')) + '">' + escapeHtml(t('mcp.connect')) + '</button>';
      }
      html += '<button class="btn btn-secondary btn-xs" onclick="_mcpPurge(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.purgeTitle')) + '">' + escapeHtml(t('mcp.purge')) + '</button>';
    } else {
      // If the catalog entry has NO required env vars, skip the modal
      // entirely and one-click-install with the built-in defaults. The
      // modal is only useful when the user must fill something in
      // (API keys, tokens, etc.) — showing a form full of optional
      // "PATH TO EXECUTABLE / TIMEOUT / MAX CONCURRENCY" tweaks for
      // servers like Hope just creates confusion.
      var _needsInput = (e.env_specs || []).some(function(s) { return s.required; });
      if (_needsInput) {
        html += '<button class="btn btn-primary btn-xs" onclick="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\')">' + escapeHtml(t('mcp.install')) + '</button>';
      } else {
        html += '<button class="btn btn-primary btn-xs" onclick="_mcpQuickInstall(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.quickInstallTitle')) + '">' + escapeHtml(t('mcp.install')) + '</button>';
      }
    }
    html += '</div></div>';  // action + footer
    html += '</div>';  // card
  });
  grid.innerHTML = html;
  _mcpScheduleBreakerRefresh();
}

/**
 * While any installed-but-disconnected server has an active circuit
 * breaker, schedule a single re-fetch so the "retry in N" countdown stays
 * fresh and the card flips to ON automatically once auto-reconnect
 * succeeds. Self-cancelling: re-poll cadence is capped at 15s and the
 * timer stops as soon as no breaker remains or the grid leaves the DOM
 * (settings panel closed / tab switched).
 */
function _mcpScheduleBreakerRefresh() {
  if (_mcpBreakerRefreshTimer) {
    clearTimeout(_mcpBreakerRefreshTimer);
    _mcpBreakerRefreshTimer = null;
  }
  var active = _mcpCatalog.filter(function(e) {
    return e.installed && !e.connected && e.breaker;
  });
  if (active.length === 0) { _mcpStopBreakerTick(); return; }

  // Re-poll a bit after the soonest retry is due (so the next fetch sees
  // the post-attempt state), clamped to [3s, 15s] to avoid hammering.
  var soonest = Math.min.apply(null, active.map(function(e) {
    return Math.max(0, e.breaker.retry_in || 0);
  }));
  var delayMs = Math.min(15000, Math.max(3000, (soonest + 1) * 1000));

  _mcpBreakerRefreshTimer = setTimeout(function() {
    _mcpBreakerRefreshTimer = null;
    var grid = document.getElementById('mcpCatalogGrid');
    // Bail if the MCP tab is no longer visible — no point polling a
    // detached / hidden grid.
    if (!grid || !grid.isConnected || grid.offsetParent === null) return;
    _populateMcpTab();
  }, delayMs);

  // Start the per-second countdown ticker so the "retry in N" text
  // decrements smoothly between server re-polls (a frozen number looks
  // broken). The ticker only touches the small countdown spans, never
  // re-renders the grid.
  _mcpStartBreakerTick();
}

/**
 * Update every live breaker-countdown span from its ``data-retry-at``
 * deadline. Runs once per second. Self-stops when no spans remain or the
 * grid is no longer visible (settings closed / tab switched), so it never
 * leaks a timer.
 */
function _mcpTickBreakers() {
  var grid = document.getElementById('mcpCatalogGrid');
  if (!grid || !grid.isConnected || grid.offsetParent === null) {
    _mcpStopBreakerTick();
    return;
  }
  var spans = grid.querySelectorAll('.mcp-breaker-countdown');
  if (spans.length === 0) {
    _mcpStopBreakerTick();
    return;
  }
  var now = Date.now();
  for (var i = 0; i < spans.length; i++) {
    var deadline = parseInt(spans[i].getAttribute('data-retry-at'), 10) || 0;
    var label = _mcpRetryLabel((deadline - now) / 1000);
    if (spans[i].textContent !== label) spans[i].textContent = label;
  }
}

function _mcpStartBreakerTick() {
  if (_mcpBreakerTickTimer) return;  // already ticking
  _mcpBreakerTickTimer = setInterval(_mcpTickBreakers, 1000);
}

function _mcpStopBreakerTick() {
  if (_mcpBreakerTickTimer) {
    clearInterval(_mcpBreakerTickTimer);
    _mcpBreakerTickTimer = null;
  }
}

/** Update "installed" badge and "connect all" button in the header. */
function _renderMcpInstalled() {
  var countEl = document.getElementById('mcpInstalledCount');
  var connectAllBtn = document.getElementById('mcpConnectAllBtn');

  var connectedApps = _mcpCatalog.filter(function(e) { return e.connected; });
  var installedNotConnected = _mcpCatalog.filter(function(e) { return e.installed && !e.connected; });
  var total = connectedApps.length + installedNotConnected.length;

  if (countEl) {
    if (total > 0) {
      countEl.textContent = t('mcp.installedCount', { n: total });
      countEl.style.display = '';
    } else {
      countEl.style.display = 'none';
    }
  }
  if (connectAllBtn) {
    connectAllBtn.style.display = installedNotConnected.length > 0 ? '' : 'none';
  }
}

// ── Install Modal ──

/**
 * One-click install: POST /api/mcp/catalog/install with an empty env so
 * the backend uses every env_spec's default. Intended for catalog entries
 * that have zero `required: true` env_specs (e.g. Hope, where all four
 * fields — HOPE_BIN, HOPE_MCP_TIMEOUT, HOPE_MCP_MAX_PARALLEL,
 * HOPE_MCP_DRY_RUN_DEFAULT — have sensible built-in defaults). Skips the
 * modal entirely. Users who want to tweak the defaults can still do so
 * by clicking the "连接" (Reconnect) button after installation, which
 * opens the same modal pre-filled.
 *
 * NOTE: We intentionally do NOT show a confirmation dialog — the whole
 * point is that a one-click install should feel instant. Status is
 * surfaced via the grid's own connect/disconnect animation and the
 * app-level debugLog, same as the "Connect All" button.
 */
/**
 * Extract a human-readable failure reason from an error thrown by the
 * Api layer. catalogInstall now lets HTTP 500s throw an ApiError whose
 * `.body` carries the backend's rich `{error, stderr_tail}` payload —
 * we prefer that over the generic "HTTP 500 on ..." message so the user
 * sees the actual connection failure (e.g. a launcher traceback tail).
 */
function _mcpErrDetail(e) {
  var body = e && e.body;
  if (body && typeof body === 'object') {
    var msg = body.error || e.message || t('mcp.unknownError');
    if (body.stderr_tail) msg += '\n\n' + t('mcp.serverOutputLabel') + '\n' + body.stderr_tail;
    return msg;
  }
  return (e && e.message) || t('mcp.unknownErrorNoConn');
}

async function _mcpQuickInstall(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  if (!entry) return;
  debugLog('[MCP] Quick-installing ' + serverId + ' (no required env)…', 'info');
  try {
    var data = await Api.mcp.catalogInstall(serverId, {});
    if (data && data.ok && data.status === 'installing') {
      debugLog('[MCP] ' + serverId + ' installing deps in background; polling…', 'info');
      data = await _mcpPollInstall(serverId, null);
    }
    if (data && data.ok && data.status !== 'error') {
      debugLog('[MCP] Installed ' + serverId + ': ' + (data.tools_count || 0) + ' tools', 'success');
      await _populateMcpTab();
    } else {
      // Installation failed — fall back to opening the modal so the user
      // can inspect the default values and/or override them. This is the
      // safety net for "hope binary not on PATH" kinds of errors.
      var _err = (data && data.error) || t('mcp.unknownErrorNoConn');
      debugLog('[MCP] Quick install failed (' + _err + '); opening install modal for ' + serverId, 'warning');
      showAlert(t('mcp.quickInstallFailed', { err: _err }));
      _mcpOpenInstallModal(serverId);
    }
  } catch (e) {
    var _detail = _mcpErrDetail(e);
    debugLog('[MCP] Quick install error for ' + serverId + ': ' + _detail, 'error');
    showAlert(t('mcp.quickInstallFailed', { err: _detail }));
    _mcpOpenInstallModal(serverId);
  }
}

function _mcpOpenInstallModal(serverId, isReinstall) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  if (!entry) return;
  _mcpInstallTarget = entry;
  _mcpInstallIsReinstall = !!isReinstall;

  // Icon may be either an emoji (e.g. '🐙') OR an inline SVG string (e.g.
  // '<svg viewBox="0 0 24 24">…</svg>' for brand logos like Meituan/Hope).
  // Using .textContent would render the SVG source as literal text, which
  // is the "path d=…" garble users saw when opening brand-icon apps.
  // Catalog icons are server-owned (lib/mcp/registry.py), not user input,
  // so innerHTML is safe here — matches how _renderMcpCatalog() already
  // emits the same icon strings into the grid cards on L3609.
  var _icon = entry.icon || Icon('plug', 34);
  document.getElementById('mcpInstallIcon').innerHTML =
    (typeof _icon === 'string' && _icon.trim().startsWith('<'))
      ? _icon
      : escapeHtml(_icon);
  document.getElementById('mcpInstallTitle').textContent = entry.name;
  document.getElementById('mcpInstallDesc').textContent = entry.description || '';
  var repoLink = document.getElementById('mcpInstallRepo');
  if (repoLink) {
    if (entry.url) {
      repoLink.href = entry.url;
      repoLink.innerHTML = '<svg width="9" height="9" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>' + escapeHtml(entry.url.replace(/^https?:\/\//, ''));
      repoLink.style.display = '';
    } else {
      repoLink.style.display = 'none';
    }
  }
  document.getElementById('mcpInstallStatus').style.display = 'none';

  // Build env fields. Split into required + optional so the modal shows
  // only what the user MUST configure by default, and hides the
  // advanced knobs (timeouts, paths, …) behind a <details> toggle. For
  // Hope this reduces the visible form from 4 confusing fields to one
  // clear "username" prompt.
  var fieldsHtml = '';
  var specs = entry.env_specs || [];
  var storedKeys = (entry.stored_env_keys || []);

  function _renderSpec(spec) {
    var hasStored = storedKeys.indexOf(spec.key) !== -1;
    if (spec.type === 'select' && Array.isArray(spec.options)) {
      return _renderSelectSpec(spec, hasStored);
    }
    var inputType = (spec.secret !== false) ? 'password' : 'text';
    var html = '<div class="stg-field">';
    html += '<label>' + escapeHtml(spec.label || spec.key);
    if (spec.required) html += ' <span style="color:#ef4444;">*</span>';
    if (hasStored) html += ' <span style="color:#10b981;font-size:11px;">' + escapeHtml(t('mcp.savedBadge')) + '</span>';
    html += '</label>';
    var ph = hasStored ? t('mcp.savedHint') : (spec.hint || '');
    html += '<input type="' + inputType + '" class="mcp-env-input" data-key="' + escapeHtml(spec.key) + '" data-has-stored="' + (hasStored ? '1' : '0') + '" placeholder="' + escapeHtml(ph) + '">';
    html += '</div>';
    return html;
  }

  // A select-type spec renders a provider dropdown that drives a companion
  // (normally hidden) text input carrying the real env value. Picking a known
  // provider fills the host AND any sibling fields named in the option's
  // `autofill` map (e.g. SMTP host + ports). The "__custom__" option reveals
  // the text input for manual entry. The dropdown itself is class
  // `mcp-env-select` (NOT `mcp-env-input`), so it is never collected as a
  // value at install time — only the companion input is.
  function _renderSelectSpec(spec, hasStored) {
    var html = '<div class="stg-field">';
    html += '<label>' + escapeHtml(spec.label || spec.key);
    if (spec.required) html += ' <span style="color:#ef4444;">*</span>';
    if (hasStored) html += ' <span style="color:#10b981;font-size:11px;">' + escapeHtml(t('mcp.savedBadge')) + '</span>';
    html += '</label>';
    html += '<select class="mcp-env-select" data-select-for="' + escapeHtml(spec.key) + '" onchange="_mcpEnvPresetChanged(this)">';
    html += '<option value="">' + escapeHtml(t('mcp.selectProvider')) + '</option>';
    spec.options.forEach(function(opt) {
      var af = opt.autofill ? escapeHtml(JSON.stringify(opt.autofill)) : '';
      html += '<option value="' + escapeHtml(opt.value) + '" data-autofill="' + af + '">' + escapeHtml(opt.label) + '</option>';
    });
    html += '</select>';
    var ph = hasStored ? t('mcp.savedHint') : (spec.hint || '');
    html += '<input type="text" class="mcp-env-input" data-key="' + escapeHtml(spec.key) + '" data-has-stored="' + (hasStored ? '1' : '0') + '" placeholder="' + escapeHtml(ph) + '" style="margin-top:6px;display:none;">';
    html += '</div>';
    return html;
  }

  if (specs.length === 0) {
    fieldsHtml = '<p class="mcp-install-noenv">' + escapeHtml(t('mcp.noEnvNeeded')) + '</p>';
  } else {
    var required = specs.filter(function(s) { return s.required; });
    var optional = specs.filter(function(s) { return !s.required; });
    required.forEach(function(spec) { fieldsHtml += _renderSpec(spec); });
    if (optional.length > 0) {
      fieldsHtml += '<details class="mcp-advanced-toggle" style="margin-top:12px;">';
      fieldsHtml += '<summary style="cursor:pointer;color:var(--text-muted);font-size:12px;user-select:none;">' + escapeHtml(t('mcp.advancedToggle', { n: optional.length })) + '</summary>';
      fieldsHtml += '<div style="margin-top:8px;">';
      optional.forEach(function(spec) { fieldsHtml += _renderSpec(spec); });
      fieldsHtml += '</div></details>';
    }
  }
  document.getElementById('mcpInstallFields').innerHTML = fieldsHtml;

  var btn = document.getElementById('mcpInstallBtn');
  btn.disabled = false;
  btn.textContent = _mcpInstallIsReinstall ? t('mcp.saveConnect') : t('mcp.installConnect');

  document.getElementById('mcpInstallOverlay').style.display = 'flex';
}

/**
 * Provider-dropdown change handler. Drives the companion host input for the
 * select's own key, and applies the chosen option's `autofill` map to sibling
 * env inputs (e.g. SMTP host + ports). Choosing "__custom__" clears and
 * reveals the host input for manual entry; choosing the blank prompt hides it.
 */
function _mcpEnvPresetChanged(sel) {
  var fields = document.getElementById('mcpInstallFields');
  if (!fields) return;
  var key = sel.getAttribute('data-select-for');
  var hostInput = fields.querySelector('.mcp-env-input[data-key="' + key + '"]');
  var opt = sel.options[sel.selectedIndex];
  var value = sel.value;

  if (!value) {
    if (hostInput) { hostInput.value = ''; hostInput.style.display = 'none'; }
    return;
  }
  if (value === '__custom__') {
    if (hostInput) { hostInput.value = ''; hostInput.style.display = ''; hostInput.focus(); }
    return;
  }
  // Known provider: set host, keep the input hidden (value still collected).
  if (hostInput) { hostInput.value = value; hostInput.style.display = 'none'; }

  var afRaw = opt ? opt.getAttribute('data-autofill') : '';
  if (afRaw) {
    var autofill = {};
    try { autofill = JSON.parse(afRaw); }
    catch (e) { debugLog('[MCP] bad autofill JSON: ' + e.message, 'warning'); }
    Object.keys(autofill).forEach(function(k) {
      var sib = fields.querySelector('.mcp-env-input[data-key="' + k + '"]');
      if (sib) sib.value = autofill[k];
    });
  }
}

function _mcpCloseInstallModal(evt) {
  if (evt && evt.target !== evt.currentTarget) return;
  document.getElementById('mcpInstallOverlay').style.display = 'none';
  _mcpInstallTarget = null;
}

/**
 * Poll an async catalog install until it finishes (ready / error).
 * Returns the final parsed body ({ok, status, tools_count, error, stderr_tail}).
 * Bounded so a stuck install eventually surfaces an error rather than spinning
 * forever (cold pip is minutes, not hours).
 */
async function _mcpPollInstall(serverId, statusEl) {
  var DEADLINE_MS = 6 * 60 * 1000;   // 6 min hard cap
  var INTERVAL_MS = 2500;
  var t0 = Date.now();
  while (Date.now() - t0 < DEADLINE_MS) {
    await new Promise(function(r) { setTimeout(r, INTERVAL_MS); });
    if (statusEl) {
      var secs = Math.round((Date.now() - t0) / 1000);
      statusEl.textContent = t('mcp.installingDeps', { n: secs });
    }
    var resp = await Api.mcp.catalogInstallStatus(serverId);
    if (!resp) continue;                       // transient network blip — retry
    var body = await resp.json().catch(function() { return null; });
    if (!body) continue;
    if (body.status === 'installing') continue;
    // ready / error / unknown → return for the caller to render.
    return body;
  }
  return { ok: false, status: 'error', error: t('mcp.installTimeout') };
}

async function _mcpDoInstall() {
  if (!_mcpInstallTarget) return;
  var btn = document.getElementById('mcpInstallBtn');
  var status = document.getElementById('mcpInstallStatus');
  btn.disabled = true;
  btn.textContent = t('mcp.installing');
  status.style.display = 'block';
  status.className = 'mcp-install-status info';
  status.textContent = t('mcp.startingFirstInstall', { name: _mcpInstallTarget.name });

  // Collect env values
  var env = {};
  var inputs = document.querySelectorAll('#mcpInstallFields .mcp-env-input');
  for (var i = 0; i < inputs.length; i++) {
    var key = inputs[i].getAttribute('data-key');
    var val = inputs[i].value.trim();
    if (val) env[key] = val;
  }

  try {
    var data = await Api.mcp.catalogInstall(_mcpInstallTarget.id, env);
    // Async path: server kicked off a background pip install. Poll status.
    if (data && data.ok && data.status === 'installing') {
      status.textContent = t('mcp.installingDepsName', { name: _mcpInstallTarget.name });
      data = await _mcpPollInstall(_mcpInstallTarget.id, status);
    }
    if (data && data.ok && data.status !== 'error') {
      status.className = 'mcp-install-status success';
      status.textContent = t('mcp.installedSuccess', { name: _mcpInstallTarget.name, n: (data.tools_count || 0) });
      debugLog('[MCP] Installed ' + _mcpInstallTarget.id + ': ' + (data.tools_count || 0) + ' tools', 'success');
      // Refresh catalog after a short delay so user sees the success
      setTimeout(function() {
        _mcpCloseInstallModal();
        _populateMcpTab();
      }, 1200);
    } else {
      var _msg = (data && data.error) || t('mcp.installFailedNoConn');
      if (data && data.stderr_tail) _msg += '\n\n' + t('mcp.serverOutputLabel') + '\n' + data.stderr_tail;
      status.className = 'mcp-install-status error';
      status.textContent = '✕ ' + _msg;
      btn.disabled = false;
      btn.textContent = t('mcp.retry');
    }
  } catch (e) {
    status.className = 'mcp-install-status error';
    status.textContent = '✕ ' + _mcpErrDetail(e);
    btn.disabled = false;
    btn.textContent = t('mcp.retry');
  }
}

// ── Uninstall / Reconnect ──

// Soft uninstall: disconnect + disable, but keep credentials for easy re-enable.
async function _mcpUninstall(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  var name = entry ? entry.name : serverId;
  if (!await showConfirm(t('mcp.uninstallConfirm', { name: name }))) return;

  try {
    var data = await Api.mcp.catalogUninstall(serverId, false);
    if (!data || !data.ok) { showAlert(t('mcp.uninstallFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    debugLog('[MCP] Uninstalled ' + serverId + (data.purged ? ' (purged)' : ' (soft, env kept)'), 'info');
    await _populateMcpTab();
  } catch (e) {
    showAlert(t('mcp.uninstallFailed', { err: e.message }));
  }
}

// Hard purge: remove config row entirely, forgetting stored credentials.
async function _mcpPurge(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  var name = entry ? entry.name : serverId;
  if (!await showConfirm(t('mcp.purgeConfirm', { name: name }), { danger: true })) return;

  try {
    var data = await Api.mcp.catalogUninstall(serverId, true);
    if (!data || !data.ok) { showAlert(t('mcp.purgeFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    debugLog('[MCP] Purged ' + serverId, 'info');
    await _populateMcpTab();
  } catch (e) {
    showAlert(t('mcp.purgeFailed', { err: e.message }));
  }
}

async function _mcpConnectAll() {
  var btn = document.getElementById('mcpConnectAllBtn');
  if (btn) { btn.disabled = true; btn.textContent = t('mcp.connecting'); }
  try {
    var data = await Api.mcp.connectAll();
    if (!data || !data.ok) { showAlert(t('mcp.connectFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    var total = data.total_tools || 0;
    var count = Object.keys(data.servers || {}).length;
    debugLog('[MCP] Connected all: ' + count + ' server(s), ' + total + ' tools', 'success');
    await _populateMcpTab();
  } catch (e) {
    showAlert(t('mcp.connectFailed', { err: e.message }));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t('mcp.connectAll'); }
  }
}

async function _mcpReconnect(serverId) {
  try {
    var data = await Api.mcp.connectOne(serverId);
    if (!data || !data.ok) { showAlert(t('mcp.connectFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    debugLog('[MCP] Reconnected ' + serverId + ': ' + (data.tools_count || 0) + ' tools', 'success');
    await _populateMcpTab();
  } catch (e) {
    showAlert(t('mcp.connectFailed', { err: e.message }));
  }
}

// ── Manual add (code-free custom server) ──

/** Open the add-custom-server modal, resetting all fields. */
function _mcpOpenAddModal() {
  ['mcpNewName', 'mcpNewCommand', 'mcpNewArgs', 'mcpNewUrl', 'mcpNewEnv', 'mcpNewDesc'].forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.value = '';
  });
  var transport = document.getElementById('mcpNewTransport');
  if (transport) transport.value = 'stdio';
  _mcpTransportChanged();
  var status = document.getElementById('mcpAddStatus');
  if (status) { status.style.display = 'none'; status.textContent = ''; }
  var btn = document.getElementById('mcpAddSaveBtn');
  if (btn) { btn.disabled = false; btn.textContent = t('mcp.saveConnect'); }
  var overlay = document.getElementById('mcpAddOverlay');
  if (overlay) overlay.style.display = 'flex';
}

function _mcpCloseAddModal(evt) {
  if (evt && evt.target !== evt.currentTarget) return;
  var overlay = document.getElementById('mcpAddOverlay');
  if (overlay) overlay.style.display = 'none';
}

function _mcpTransportChanged() {
  var transport = (document.getElementById('mcpNewTransport') || {}).value || 'stdio';
  var stdioFields = document.getElementById('mcpStdioFields');
  var sseFields = document.getElementById('mcpSseFields');
  if (stdioFields) stdioFields.style.display = transport === 'stdio' ? '' : 'none';
  if (sseFields) sseFields.style.display = transport === 'sse' ? '' : 'none';
}

async function _mcpSaveServer() {
  var status = document.getElementById('mcpAddStatus');
  var saveBtn = document.getElementById('mcpAddSaveBtn');
  function _fail(msg) {
    if (status) {
      status.style.display = 'block';
      status.className = 'mcp-install-status error';
      status.textContent = '✕ ' + msg;
    } else {
      showAlert(msg);
    }
    if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = t('mcp.saveConnect'); }
  }

  var name = (document.getElementById('mcpNewName') || {}).value || '';
  name = name.trim();
  if (!name) { _fail(t('mcp.needName')); return; }

  var transport = (document.getElementById('mcpNewTransport') || {}).value || 'stdio';
  var payload = { name: name, transport: transport, enabled: true };

  if (transport === 'stdio') {
    payload.command = (document.getElementById('mcpNewCommand') || {}).value || '';
    var argsText = (document.getElementById('mcpNewArgs') || {}).value || '';
    payload.args = argsText.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
    if (!payload.command) { _fail(t('mcp.needCommand')); return; }
  } else {
    payload.url = (document.getElementById('mcpNewUrl') || {}).value || '';
    if (!payload.url) { _fail(t('mcp.needUrl')); return; }
  }

  // Parse env vars
  var envText = (document.getElementById('mcpNewEnv') || {}).value || '';
  if (envText.trim()) {
    payload.env = {};
    envText.split('\n').forEach(function(line) {
      var eq = line.indexOf('=');
      if (eq > 0) {
        payload.env[line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
      }
    });
  }

  var desc = (document.getElementById('mcpNewDesc') || {}).value || '';
  if (desc.trim()) payload.description = desc.trim();

  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = t('mcp.saving'); }
  if (status) {
    status.style.display = 'block';
    status.className = 'mcp-install-status info';
    status.textContent = t('mcp.startingName', { name: name });
  }

  try {
    var data = await Api.mcp.serverCreate(payload);
    if (!data || !data.ok) { _fail((data && data.error) || t('mcp.saveFailedNoConn')); return; }

    // Auto-connect
    await Api.mcp.connectOne(name);

    debugLog('[MCP] Server "' + name + '" saved & connected', 'success');
    if (status) {
      status.className = 'mcp-install-status success';
      status.textContent = t('mcp.savedConnected', { name: name });
    }
    setTimeout(function() {
      _mcpCloseAddModal();
      _populateMcpTab();
    }, 900);
  } catch (e) {
    _fail(_mcpErrDetail(e));
  }
}
