/* ===== migrated source: settings/mcp.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/mcp — extracted from settings.js (split 2026-05-28)

   MCP catalog UI: render, install modal, save server, reconnect.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
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
/* In-flight per-card operations (): serverId →
 *   'connecting' | 'uninstalling' | 'installing'. Consulted by
 *   _renderMcpCatalog, so the busy state SURVIVES any concurrent repopulate
 *   (the breaker-refresh timer fires _populateMcpTab mid-operation) — a
 *   one-off DOM patch would be silently reverted by that re-render. */
var _mcpPending = {};
/* Per-tool toggle state (): which cards have their tool
 * list expanded, and the fetched per-server tool rows. Both survive the
 * full-grid re-render that every populate/breaker-refresh triggers, for the
 * same reason _mcpPending does. */
var _mcpToolsOpen = {};
var _mcpToolsCache = {};

/* Latest upstream-version sightings, filled ASYNCHRONOUSLY after the grid
 * first paints (the tab would otherwise block on one npm/PyPI round-trip
 * per installed server). serverId → check_server_update dict; a card only
 * grows an update button once this lands and re-renders. */
var _mcpUpdates = {};
var _mcpUpdatesInFlight = false;

function _mcpSetPending(serverId, kind) {
  _mcpPending[serverId] = kind;
  _renderMcpCatalog();
}
function _mcpClearPending(serverId, skipRender) {
  if (delete _mcpPending[serverId] && !skipRender) _renderMcpCatalog();
}


function _mcpPendingLabel(pending) {
  return pending === 'connecting' ? t('mcp.connecting')
    : pending === 'installing' ? t('mcp.installing')
    : pending === 'updating' ? t('mcp.updating')
    : t('mcp.uninstalling');
}

/**
 * Refresh the upstream-version sightings, then re-render so eligible cards
 * grow their update button. Fire-and-forget from _populateMcpTab — the
 * catalog render never waits on registry round-trips.
 */
async function _mcpCheckUpdates() {
  if (_mcpUpdatesInFlight) return;
  _mcpUpdatesInFlight = true;
  try {
    var r = await Api.mcp.updatesCheck();
    if (!r || !r.ok) return;
    var data = await r.json();
    _mcpUpdates = (data && data.updates) || {};
    _renderMcpCatalog();
  } catch (e) {
    debugLog('[MCP] update check failed: ' + ((e && e.message) || e), 'warning');
  } finally {
    _mcpUpdatesInFlight = false;
  }
}

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
    if (data.mcp_tool_summary
        && typeof runtimeScope.applyMcpToolSummary === 'function') {
      runtimeScope.applyMcpToolSummary(data.mcp_tool_summary);
    }

    _renderMcpCategoryBar();
    _renderMcpCatalog();
    _renderMcpInstalled();
    _mcpUpdateToolCount();
    _mcpCheckUpdates();   // async — cards gain update buttons on re-render
  } catch (e) {
    if (grid) grid.innerHTML = '<p class="stg-empty">' + escapeHtml(t('mcp.loadFailed', { err: e.message })) + '</p>';
    debugLog('[MCP] Failed to load catalog: ' + e.message, 'error');
  }
}

/** Update the tool count badge. */
function _mcpUpdateToolCount() {
  var badge = document.getElementById('mcpToolCount');
  if (!badge) return;
  // Count what the MODEL actually gets: discovered minus per-tool-disabled.
  var total = 0;
  _mcpCatalog.forEach(function(e) {
    total += Math.max(0, (e.tools_count || 0) - (e.disabled_tools || []).length);
  });
  badge.textContent = t('mcp.toolsCount', { n: total });
}

/**
 * Render category filter pills.
 *
 * The pill set is DERIVED from the categories the catalog actually returned,
 * never from a hand-copied list. A literal whitelist here silently swallowed
 * every category the backend added later: `lib/mcp/registry.py::CATEGORIES`
 * grew to 12 while this list still held 10, so 'Local Life & Travel (China)'
 * (5 entries) and 'Science & Research' (2) had NO pill at all — their cards
 * existed but were unreachable by filtering. `_CAT_ORDER` is a display
 * PREFERENCE only; anything missing from it still renders, appended
 * alphabetically after the known ones.
 */
var _CAT_ORDER = ['Development','Data & DB','Communication','Search & Web',
                  'Productivity','DevOps','Finance','Design',
                  'Science & Research','Local Life & Travel (China)',
                  'Other','Custom'];

function _mcpOrderedCategories(cats) {
  var known = _CAT_ORDER.filter(function(c) { return cats[c]; });
  var extra = Object.keys(cats).filter(function(c) {
    return _CAT_ORDER.indexOf(c) === -1;
  }).sort();
  return known.concat(extra);
}

function _renderMcpCategoryBar() {
  var bar = document.getElementById('mcpCategoryBar');
  if (!bar) return;
  var cats = {};
  _mcpCatalog.forEach(function(e) {
    var c = e.category || 'Other';
    cats[c] = (cats[c] || 0) + 1;
  });
  var html = '<button class="mcp-cat-pill' + (_mcpActiveCategory === 'all' ? ' active' : '') + '" data-tofu-action="_mcpSetCategory(\'all\')">' + escapeHtml(t('mcp.scopeAll')) + ' <span class="mcp-cat-count">' + _mcpCatalog.length + '</span></button>';
  _mcpOrderedCategories(cats).forEach(function(c) {
    html += '<button class="mcp-cat-pill' + (_mcpActiveCategory === c ? ' active' : '') + '" data-tofu-action="_mcpSetCategory(\'' + escapeHtml(c).replace(/'/g, "\\'") + '\')">' + escapeHtml(c) + ' <span class="mcp-cat-count">' + cats[c] + '</span></button>';
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

/**
 * Entries worth suggesting to someone who has installed nothing yet.
 *
 * Restricted to servers a user can provably finish installing on their own:
 * either they need no credential at all, or every required credential
 * declares an `obtain_url` (so the card can hand them a real link). Anything
 * gated behind a process we cannot show a route for is EXCLUDED — recommending
 * it would send the user down a path that dead-ends, which is worse than not
 * recommending at all.
 *
 * Deliberately NOT an in-conversation intent detector: a phrase-matching
 * trigger was measured at 60% false positives on a sibling epic
 * (), so suggestions stay inside the panel the user
 * already opened.
 */
function _mcpSelfServeSuggestions(limit) {
  var out = _mcpCatalog.filter(function(e) {
    if (e.installed || e.custom) return false;
    var specs = e.env_specs || [];
    var required = specs.filter(function(s) { return s.required; });
    if (required.length === 0) return true;   // nothing to obtain
    return required.every(function(s) { return !!s.obtain_url; });
  });
  out.sort(function(a, b) {
    if (a.featured && !b.featured) return -1;
    if (!a.featured && b.featured) return 1;
    return (a.name || '').localeCompare(b.name || '');
  });
  return out.slice(0, limit || 6);
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
    var emptyHtml = '<p class="stg-empty">' + emptyMsg + '</p>';
    // "Nothing installed" is the one empty state where the user has a next
    // action rather than a failed filter — offer it instead of a dead end.
    if (_mcpScope === 'installed') {
      var picks = _mcpSelfServeSuggestions(6);
      if (picks.length > 0) {
        emptyHtml += '<div class="mcp-suggest">';
        emptyHtml += '<div class="mcp-suggest-title">' +
          escapeHtml(t('mcp.suggestTitle')) + '</div>';
        emptyHtml += '<div class="mcp-suggest-row">';
        picks.forEach(function(e) {
          emptyHtml += '<button class="mcp-suggest-chip" data-tofu-action="_mcpSetScope(\'available\');_mcpFilterCatalog(' +
            JSON.stringify(e.name || e.id).replace(/"/g, '&quot;') + ')" title="' +
            escapeHtml(e.description || '') + '">' +
            (e.icon && !/^</.test(e.icon) ? escapeHtml(e.icon) + ' ' : '') +
            escapeHtml(e.name || e.id) + '</button>';
        });
        emptyHtml += '</div></div>';
      }
    }
    grid.innerHTML = emptyHtml;
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
    // Older peers remain fully usable through the SDK v2 negotiation fallback.
    // Surface one calm, actionable note instead of downgrading the healthy ON
    // state or emitting repeated toast/log warnings.
    var legacyCompat = connected && e.compatibility_notice &&
      e.compatibility_notice.kind === 'legacy_protocol' &&
      e.compatibility_notice.update_recommended === true;
    /* An in-flight click operation outranks every derived state — the
     *   card paints the busy label + NO action buttons until the handler
     *   clears it (success → repopulate; failure → restore). */
    var pending = _mcpPending[e.id] || null;
    var stateClass = connected ? (credExpired ? ' connected cred-expired' : ' connected')
      : breaker ? ' installed reconnecting'
      : installed ? ' installed' : '';
    if (pending) stateClass += ' mcp-pending';
    /* An open tools drawer spans the full grid row (.mcp-app-card.tools-open
     * + grid-auto-flow:dense) so a tall expanded card never stretches its
     * grid-row neighbour into a blank void. */
    var toolsOpen = connected && !pending && !!_mcpToolsOpen[e.id];
    if (toolsOpen) stateClass += ' tools-open';
    html += '<div class="mcp-app-card' + stateClass + '">';
    html += '<div class="mcp-app-icon">' + (e.icon || Icon('plug', 26)) + '</div>';
    html += '<div class="mcp-app-name"><span class="mcp-app-name-text">' + escapeHtml(e.name) + '</span>';
    if (pending) {
      html += '<span class="mcp-app-status pending">' + escapeHtml(
        _mcpPendingLabel(pending)) + '</span>';
    } else if (credExpired) {
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
    // Getting-started note (how to obtain a key, what the server can/can't do).
    // This field was authored in lib/mcp/registry.py from the start but had NO
    // renderer anywhere in the frontend, so every note written for a user was
    // invisible to them.
    if (e.install_note) {
      html += '<div class="mcp-app-note">' + escapeHtml(e.install_note) + '</div>';
    }
    if (legacyCompat) {
      html += '<div class="mcp-app-compat-notice" title="' +
        escapeHtml(t('mcp.legacyProtocolTitle', {
          version: e.compatibility_notice.protocol_version || e.protocol_version || '?',
          target: e.compatibility_notice.target_protocol || '2026-07-28'
        })) + '">' + Icon('info', 12) + '<span>' +
        escapeHtml(t('mcp.legacyProtocolNotice', {
          version: e.compatibility_notice.protocol_version || e.protocol_version || '?'
        })) + '</span></div>';
    }
    // Footer: repo link (left) + tools count / action buttons (right)
    html += '<div class="mcp-app-footer">';
    if (e.url) {
      html += '<a class="mcp-app-repo" href="' + escapeHtml(e.url) + '" target="_blank" rel="noopener" title="' + escapeHtml(t('mcp.repoTitle')) + '">' + REPO_SVG + ' ' + escapeHtml(t('mcp.repo')) + '</a>';
    } else {
      html += '<span></span>';
    }
    html += '<div class="mcp-app-action">';
    if (pending) {
      html += '<span class="mcp-app-pending-note">' + escapeHtml(
        _mcpPendingLabel(pending)) + '</span>';
    } else if (connected) {
      if (credExpired) {
        // The subprocess is live but the stored cookie/token no longer
        // authenticates. Offer a one-click path to re-enter credentials —
        // the same reinstall modal used for editing an installed server
        // (existing env prefills, blank leaves it unchanged).
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\', true)" title="' + escapeHtml(t('mcp.credExpiredTitle')) + '">' + escapeHtml(t('mcp.updateCreds')) + '</button>';
      }
      if (e.server_version) {
        html += '<span class="mcp-app-version" title="' + escapeHtml((e.server_impl_name || e.id) + ' v' + e.server_version) + '">v' + escapeHtml(e.server_version) + '</span>';
      }
      if (e.tools_count) {
        var _disabledCount = (e.disabled_tools || []).length;
        var _countLabel = _disabledCount > 0
          ? t('mcp.toolsCountOf', { a: (e.tools_count - _disabledCount), b: e.tools_count })
          : t('mcp.toolsCount', { n: (e.tools_count || 0) });
        html += '<button class="mcp-app-tools-count mcp-tools-toggle" data-tofu-action="_mcpToggleToolsPanel(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.toolsToggleTitle')) + '">' +
          (_mcpToolsOpen[e.id] ? '▾ ' : '▸ ') + escapeHtml(_countLabel) + '</button>';
      }
      html += _mcpUpdateButtonHtml(e);
      html += '<button class="btn btn-secondary btn-xs" data-tofu-action="_mcpUninstall(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.uninstallTitle')) + '">' + escapeHtml(t('mcp.uninstall')) + '</button>';
    } else if (installed) {
      if (breaker) {
        html += '<span class="mcp-app-reconnecting-note" title="' +
          escapeHtml(t('mcp.reconnecting')) + '">⟳ ' +
          _mcpBreakerCountdownSpan(breaker) + '</span>';
      }
      if (e.custom) {
        // Custom servers have no catalog entry, so the catalog install
        // endpoint would 404. Reconnect straight through connectOne.
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpReconnect(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.connectCustomTitle')) + '">' + escapeHtml(t('mcp.connect')) + '</button>';
      } else {
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\', true)" title="' + escapeHtml(t('mcp.connectReinstallTitle')) + '">' + escapeHtml(t('mcp.connect')) + '</button>';
      }
      html += _mcpUpdateButtonHtml(e);
      html += '<button class="btn btn-secondary btn-xs" data-tofu-action="_mcpPurge(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.purgeTitle')) + '">' + escapeHtml(t('mcp.purge')) + '</button>';
    } else {
      // If the catalog entry has NO required env vars, skip the modal
      // entirely and one-click-install with the built-in defaults. The
      // modal is only useful when the user must fill something in
      // (API keys, tokens, etc.) — showing a form full of optional
      // "PATH TO EXECUTABLE / TIMEOUT / MAX CONCURRENCY" tweaks for
      // servers like Hope just creates confusion.
      var _needsInput = (e.env_specs || []).some(function(s) { return s.required; });
      if (_needsInput) {
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\')">' + escapeHtml(t('mcp.install')) + '</button>';
      } else {
        html += '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpQuickInstall(\'' + escapeHtml(e.id) + '\')" title="' + escapeHtml(t('mcp.quickInstallTitle')) + '">' + escapeHtml(t('mcp.install')) + '</button>';
      }
    }
    html += '</div></div>';  // action + footer
    if (connected && !pending && _mcpToolsOpen[e.id]) {
      html += _renderMcpToolPanel(e);
    }
    html += '</div>';  // card
  });
  grid.innerHTML = html;
  _mcpScheduleBreakerRefresh();
}

/* ═══════════════════════════════════════════════════════════════════
   Per-tool toggles ()
   A connected card's tools badge expands into a checkbox list — one row
   per upstream tool, checked = offered to the model. Toggling saves the
   FULL disabled list immediately (full-replacement PUT semantics), with
   optimistic UI and revert-on-failure.
   ═══════════════════════════════════════════════════════════════════ */

function _mcpToggleToolsPanel(serverId) {
  if (_mcpToolsOpen[serverId]) {
    delete _mcpToolsOpen[serverId];
  } else {
    _mcpToolsOpen[serverId] = true;
    if (!_mcpToolsCache[serverId]) _mcpLoadTools(serverId);
  }
  _renderMcpCatalog();
}

async function _mcpLoadTools(serverId) {
  try {
    var r = await Api.mcp.toolsListForServer(serverId);
    if (!r || !r.ok) throw new Error('HTTP ' + (r ? r.status : 'no response'));
    var data = await r.json();
    _mcpToolsCache[serverId] = data.tools || [];
  } catch (e) {
    debugLog('[MCP] Failed to load tools for ' + serverId + ': ' + e.message, 'error');
    _mcpToolsCache[serverId] = [];
  }
  if (_mcpToolsOpen[serverId]) _renderMcpCatalog();
}

function _renderMcpToolPanel(e) {
  var rows = _mcpToolsCache[e.id];
  var html = '<div class="mcp-tool-panel">';
  if (!rows) {
    html += '<div class="mcp-tool-loading">' + escapeHtml(t('mcp.loading')) + '</div>';
  } else if (rows.length === 0) {
    html += '<div class="mcp-tool-loading">—</div>';
  } else {
    var enabledCount = rows.filter(function(x) { return x.enabled; }).length;
    /* Header is a pinned toolbar: live enabled/total count on the left,
     * bulk enable/disable on the right; only the tool grid below scrolls,
     * so the count never scrolls away on long lists. */
    html += '<div class="mcp-tool-panel-head">' +
      '<span class="mcp-tool-panel-count">' +
      escapeHtml(t('mcp.toolsEnabledOf', { a: enabledCount, b: rows.length })) + '</span>' +
      '<span class="mcp-tool-bulk">' +
      '<button type="button" class="mcp-tool-bulk-btn" data-tofu-action="_mcpSetAllTools(\'' + escapeHtml(e.id) + '\',true)">' + escapeHtml(t('mcp.toolsEnableAll')) + '</button>' +
      '<button type="button" class="mcp-tool-bulk-btn" data-tofu-action="_mcpSetAllTools(\'' + escapeHtml(e.id) + '\',false)">' + escapeHtml(t('mcp.toolsDisableAll')) + '</button>' +
      '</span></div>';
    html += '<div class="mcp-tool-grid">';
    rows.forEach(function(x) {
      /* The visible control is the house .stg-toggle (visually-hidden input
       * + span track/thumb): ambient modal/panel input resets (.modal input,
       * [data-theme] .modal:not(...) input:not(...)) paint the 1px hidden
       * input and cannot deform the switch. */
      html += '<label class="mcp-tool-row" title="' + escapeHtml(x.description || '') + '">' +
        '<span class="mcp-tool-name">' + escapeHtml(x.name) + '</span>' +
        '<span class="stg-toggle">' +
        '<input type="checkbox" ' + (x.enabled ? 'checked ' : '') +
        'data-tofu-action-change="_mcpToggleTool(\'' + escapeHtml(e.id) + '\',\'' + escapeHtml(x.name) + '\',this.checked)">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
        '</span></label>';
    });
    html += '</div>';
  }
  html += '</div>';
  return html;
}

/* Shared persist path for per-tool toggles and bulk enable/disable:
 * the mutation has already been applied optimistically to the cache; this
 * PUTs the FULL disabled list (full-replacement semantics) and rolls back
 * via the caller-supplied undo closure on failure. */
async function _mcpSaveTools(serverId, undo) {
  var rows = _mcpToolsCache[serverId];
  var disabled = rows.filter(function(x) { return !x.enabled; })
    .map(function(x) { return x.name; });
  try {
    var r = await Api.mcp.serverToolsSet(serverId, disabled);
    if (!r || !r.ok) throw new Error('HTTP ' + (r ? r.status : 'no response'));
    var data = typeof r.json === 'function'
      ? await r.json().catch(function() { return {}; }) : {};
    var entry = _mcpCatalog.filter(function(x) { return x.id === serverId; })[0];
    if (entry) entry.disabled_tools = disabled;
    if (data.mcp_tool_summary
        && typeof runtimeScope.applyMcpToolSummary === 'function') {
      runtimeScope.applyMcpToolSummary(data.mcp_tool_summary);
    }
    _renderMcpCatalog();
    _mcpUpdateToolCount();
  } catch (err) {
    undo();
    showAlert(t('mcp.toolsToggleFailed', { err: err.message }));
    _renderMcpCatalog();
  }
}

async function _mcpToggleTool(serverId, toolName, enabled) {
  var rows = _mcpToolsCache[serverId];
  if (!rows) return;
  // Optimistic update; revert on failure.
  rows.forEach(function(x) { if (x.name === toolName) x.enabled = enabled; });
  debugLog('[MCP] ' + serverId + ': ' + (enabled ? 'enabled' : 'disabled') + ' tool ' + toolName, 'success');
  await _mcpSaveTools(serverId, function() {
    rows.forEach(function(x) { if (x.name === toolName) x.enabled = !enabled; });
  });
}

async function _mcpSetAllTools(serverId, enabled) {
  var rows = _mcpToolsCache[serverId];
  if (!rows) return;
  var prev = rows.map(function(x) { return x.enabled; });
  rows.forEach(function(x) { x.enabled = enabled; });
  debugLog('[MCP] ' + serverId + ': ' + (enabled ? 'enabled' : 'disabled') + ' ALL tools', 'success');
  await _mcpSaveTools(serverId, function() {
    rows.forEach(function(x, i) { x.enabled = prev[i]; });
  });
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
  /* INSTANT-UI (): the card shows 安装中… on the CLICK
   *   frame — the old code only wrote to the debug panel and the card sat
   *   static until the install + poll finished. */
  _mcpSetPending(serverId, 'installing');
  debugLog('[MCP] Quick-installing ' + serverId + ' (no required env)…', 'info');
  try {
    var data = await Api.mcp.catalogInstall(serverId, {});
    if (data && data.ok && data.status === 'installing') {
      debugLog('[MCP] ' + serverId + ' installing deps in background; polling…', 'info');
      data = await _mcpPollInstall(serverId, null);
    }
    if (data && data.ok && data.status !== 'error') {
      debugLog('[MCP] Installed ' + serverId + ': ' + (data.tools_count || 0) + ' tools', 'success');
      _mcpClearPending(serverId, true);   // repopulate renders the fresh state
      await _populateMcpTab();
    } else {
      // Installation failed — fall back to opening the modal so the user
      // can inspect the default values and/or override them. This is the
      // safety net for "hope binary not on PATH" kinds of errors.
      _mcpClearPending(serverId);         // restore the card first
      var _err = (data && data.error) || t('mcp.unknownErrorNoConn');
      debugLog('[MCP] Quick install failed (' + _err + '); opening install modal for ' + serverId, 'warning');
      showAlert(t('mcp.quickInstallFailed', { err: _err }));
      _mcpOpenInstallModal(serverId);
    }
  } catch (e) {
    _mcpClearPending(serverId);           // restore the card first
    var _detail = _mcpErrDetail(e);
    debugLog('[MCP] Quick install error for ' + serverId + ': ' + _detail, 'error');
    showAlert(t('mcp.quickInstallFailed', { err: _detail }));
    _mcpOpenInstallModal(serverId);
  }
}

/**
 * The placeholder for a credential input.
 *
 * Always the spec's own hint when it has one. Previously this returned the
 * "already saved, leave blank to keep" notice INSTEAD of the hint whenever a
 * value was stored — which silently removed the guidance at the one moment a
 * user is most likely to need it (rotating an expired key). The saved notice
 * is now rendered as its own line by the caller, so neither fact evicts the
 * other.
 */
function _mcpPlaceholder(spec, hasStored) {
  if (spec.hint) return spec.hint;
  return hasStored ? t('mcp.savedHint') : '';
}

/**
 * Render "where do I get this credential" as a real link (+ optional ordered
 * steps), instead of a breadcrumb crammed into the placeholder.
 *
 * A placeholder cannot be clicked, is truncated by the input width, and
 * disappears the moment the user types — so a console path written there made
 * the user hand-transcribe a URL. Returns '' when the entry declares no
 * route, so nothing is invented for servers that need no credential.
 */
function _mcpObtainBlock(spec) {
  var url = spec.obtain_url || '';
  var steps = Array.isArray(spec.obtain_steps) ? spec.obtain_steps : [];
  if (!url && steps.length === 0) return '';
  var html = '<div class="mcp-obtain">';
  if (url) {
    // Only http(s) — a catalog entry is server-owned, but this is the one
    // place a URL becomes a clickable target, so reject anything that could
    // carry a javascript: payload.
    var safe = /^https?:\/\//i.test(url) ? url : '';
    if (safe) {
      html += '<a class="mcp-obtain-link" href="' + escapeHtml(safe) +
        '" target="_blank" rel="noopener noreferrer">' +
        escapeHtml(t('mcp.obtainKey')) + ' ↗</a>';
    }
  }
  if (steps.length > 0) {
    html += '<ol class="mcp-obtain-steps">';
    steps.forEach(function(s) {
      html += '<li>' + escapeHtml(String(s)) + '</li>';
    });
    html += '</ol>';
  }
  html += '</div>';
  return html;
}

/**
 * Catalog entries that ALREADY have a stored value for `key`, excluding
 * `selfId`. Two cards can legitimately share one credential (RollingGo hotel
 * + flight share ROLLINGGO_API_KEY; github + github-batch share a PAT), and
 * without this the second install asks for a key the user already gave us —
 * so they go re-apply for a duplicate.
 */
function _mcpSharedCredentialSources(key, selfId) {
  return _mcpCatalog.filter(function(e) {
    return e.id !== selfId
      && (e.stored_env_keys || []).indexOf(key) !== -1;
  });
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
  var noteEl = document.getElementById('mcpInstallNote');
  if (noteEl) {
    if (entry.install_note) {
      noteEl.textContent = entry.install_note;
      noteEl.style.display = '';
    } else {
      noteEl.textContent = '';
      noteEl.style.display = 'none';
    }
  }
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
    html += _mcpObtainBlock(spec);
    html += '<input type="' + inputType + '" class="mcp-env-input" data-key="' + escapeHtml(spec.key) + '" data-has-stored="' + (hasStored ? '1' : '0') + '" placeholder="' + escapeHtml(_mcpPlaceholder(spec, hasStored)) + '">';
    if (hasStored) {
      // The "leave blank to keep" notice used to be pushed into the
      // PLACEHOLDER, which meant it REPLACED spec.hint — so on a reinstall
      // (exactly when someone is rotating a credential) the guidance about
      // what to type disappeared. Both facts matter, so both are shown.
      html += '<span class="stg-hint mcp-env-saved-note">' + escapeHtml(t('mcp.savedHint')) + '</span>';
    } else {
      // Not stored for THIS entry — but a sibling entry may already hold the
      // very same credential. Say so, otherwise the user re-applies for a key
      // they already have.
      var shared = _mcpSharedCredentialSources(
        spec.key, entry && entry.id);
      if (shared.length > 0) {
        html += '<span class="stg-hint mcp-env-shared-note">' +
          escapeHtml(t('mcp.sharedCredential', {
            name: shared.map(function(s) { return s.name || s.id; }).join('、'),
          })) + '</span>';
      }
    }
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
    html += '<select class="mcp-env-select" data-select-for="' + escapeHtml(spec.key) + '" data-tofu-action-change="_mcpEnvPresetChanged(this)">';
    html += '<option value="">' + escapeHtml(t('mcp.selectProvider')) + '</option>';
    spec.options.forEach(function(opt) {
      var af = opt.autofill ? escapeHtml(JSON.stringify(opt.autofill)) : '';
      html += '<option value="' + escapeHtml(opt.value) + '" data-autofill="' + af + '">' + escapeHtml(opt.label) + '</option>';
    });
    html += '</select>';
    html += _mcpObtainBlock(spec);
    html += '<input type="text" class="mcp-env-input" data-key="' + escapeHtml(spec.key) + '" data-has-stored="' + (hasStored ? '1' : '0') + '" placeholder="' + escapeHtml(_mcpPlaceholder(spec, hasStored)) + '" style="margin-top:6px;display:none;">';
    if (hasStored) {
      html += '<span class="stg-hint mcp-env-saved-note">' + escapeHtml(t('mcp.savedHint')) + '</span>';
    }
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

  /* INSTANT-UI (): the card shows 卸载中… the moment
   *   the confirm closes — the old code sat static for the whole DELETE RTT. */
  _mcpSetPending(serverId, 'uninstalling');
  try {
    var data = await Api.mcp.catalogUninstall(serverId, false);
    if (!data || !data.ok) { _mcpClearPending(serverId); showAlert(t('mcp.uninstallFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    debugLog('[MCP] Uninstalled ' + serverId + (data.purged ? ' (purged)' : ' (soft, env kept)'), 'info');
    _mcpClearPending(serverId, true);     // repopulate renders the fresh state
    await _populateMcpTab();
  } catch (e) {
    _mcpClearPending(serverId);
    showAlert(t('mcp.uninstallFailed', { err: e.message }));
  }
}

// Hard purge: remove config row entirely, forgetting stored credentials.
async function _mcpPurge(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  var name = entry ? entry.name : serverId;
  if (!await showConfirm(t('mcp.purgeConfirm', { name: name }), { danger: true })) return;

  /* INSTANT-UI (): same pending shape as uninstall. */
  _mcpSetPending(serverId, 'uninstalling');
  try {
    var data = await Api.mcp.catalogUninstall(serverId, true);
    if (!data || !data.ok) { _mcpClearPending(serverId); showAlert(t('mcp.purgeFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    debugLog('[MCP] Purged ' + serverId, 'info');
    _mcpClearPending(serverId, true);     // repopulate renders the fresh state
    await _populateMcpTab();
  } catch (e) {
    _mcpClearPending(serverId);
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
  /* INSTANT-UI (): the card shows 连接中… on the CLICK
   *   frame — MCP cold start is measured at 27-55s (JOURNAL), the longest
   *   dead window in the app. */
  _mcpSetPending(serverId, 'connecting');
  try {
    var data = await Api.mcp.connectOne(serverId);
    if (!data || !data.ok) { _mcpClearPending(serverId); showAlert(t('mcp.connectFailed', { err: ((data && data.error) || t('mcp.unknownError')) })); return; }
    debugLog('[MCP] Reconnected ' + serverId + ': ' + (data.tools_count || 0) + ' tools', 'success');
    _mcpClearPending(serverId, true);     // repopulate renders the fresh state
    await _populateMcpTab();
  } catch (e) {
    _mcpClearPending(serverId);
    showAlert(t('mcp.connectFailed', { err: e.message }));
  }
}


/**
 * Update button for an installed card whose upstream registry has a newer
 * release than the stored launch spec would start. `_mcpUpdates` fills in
 * after the grid paints, so the button appears on the check's re-render.
 */
function _mcpUpdateButtonHtml(e) {
  var upd = _mcpUpdates[e.id];
  if (!upd || !upd.update_available || !upd.latest) return '';
  return '<button class="btn btn-primary btn-xs" data-tofu-action="_mcpApplyUpdate(\'' +
    escapeHtml(e.id) + '\')" title="' +
    escapeHtml(t('mcp.updateTitle', { current: upd.current || '?', latest: upd.latest })) + '">' +
    escapeHtml(t('mcp.updateTo', { version: upd.latest })) + '</button>';
}

/**
 * One-click upstream update: the backend rewrites the stored launch args to
 * pin the registry-latest release (credentials preserved) and reconnects,
 * so the new code is what actually runs. Same instant-pending shape as
 * reconnect/uninstall ().
 */
async function _mcpApplyUpdate(serverId) {
  _mcpSetPending(serverId, 'updating');
  try {
    var data = await Api.mcp.updateApply(serverId);
    if (!data || !data.ok) {
      _mcpClearPending(serverId);
      showAlert(t('mcp.updateFailed', { err: ((data && data.error) || t('mcp.unknownError')) }));
      return;
    }
    if (!data.updated && data.already_latest) {
      debugLog('[MCP] ' + serverId + ' already on latest v' + (data.version || '?'), 'info');
    } else {
      debugLog('[MCP] Updated ' + serverId + ' to v' + (data.version || '?') +
        (data.reconnected ? ' — reconnected with ' + (data.tools_count || 0) + ' tools' : ''), 'success');
    }
    _mcpClearPending(serverId, true);     // repopulate renders the fresh state
    await _populateMcpTab();
  } catch (e) {
    _mcpClearPending(serverId);
    showAlert(t('mcp.updateFailed', { err: _mcpErrDetail(e) }));
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

function _applyDebugModeVisibility() {
  const visible = Boolean(runtimeScope._featureFlags?.debug_mode);
  for (const id of ['studioTopbarBtn', 'tasksTopbarBtn', 'mobileStudio',
    'mobileTasks', 'agentWorkflowSection', 'mobileWorkflowSection']) {
    const element = document.getElementById(id);
    if (element) element.style.display = visible ? '' : 'none';
  }
  /* A hidden experimental workflow must never keep owning future turns. The
   * already-accepted turn has an immutable config, so this paint/persist reset
   * only affects the next message when Debug Mode is switched off. */
  if (!visible && runtimeScope.activeFlow) {
    if (typeof _applyFlowUI === 'function') _applyFlowUI('');
    if (typeof captureActiveConversationSettings === 'function') {
      captureActiveConversationSettings();
    }
  }
}

function _applyTradingVisibility() {
  const element = document.getElementById('tradingModeBtn');
  if (element) element.style.display = runtimeScope._featureFlags?.trading_enabled ? '' : 'none';
}

function openTradingMode() { window.location.href = 'trading.html'; }

function _openActiveCompaction() {
  if (typeof runtimeScope.openCompactionViewer === 'function' && runtimeScope.activeConvId) {
    return runtimeScope.openCompactionViewer(runtimeScope.activeConvId);
  }
}
