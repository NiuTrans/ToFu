/* ═══════════════════════════════════════════════════════════
   tools_panel.js — Settings → 工具 tab (live registry inventory)

   Renders GET /api/v1/tools (lib/tools/registry/_introspect.py):
   every tool family registered in this process, grouped by category,
   with each family's gate state evaluated by its own build() under
   the server's reference context. Pure-read panel — no mutations.

   This file is concatenated by lib/js_bundler.py (_DEFERRED_FILES);
   symbols share the global window scope. No exports / imports.
   ═══════════════════════════════════════════════════════════ */

var _toolsInvData = null;        // last inventory payload from /api/v1/tools
var _toolsInvFilter = 'all';     // 'all' | 'on' | 'off'
var _toolsInvQuery = '';

// ── Population (called from openSettings → _populateToolsTab) ──
async function _populateToolsTab() {
  var body = document.getElementById('toolsInvBody');
  try {
    var data = await Api.tools.inventory();
    if (!data) throw new Error(t('toolsInv.noResponse'));
    _toolsInvData = data;
    _toolsInvRender();
  } catch (e) {
    debugLog('[ToolsPanel] Failed to load inventory: ' + (e && e.message), 'error');
    if (body) body.innerHTML = '<p class="stg-empty">' +
      escapeHtml(t('toolsInv.loadFailed', { err: (e && e.message) || '?' })) + '</p>';
  }
}

function _toolsInvSetFilter(f) {
  _toolsInvFilter = f;
  document.querySelectorAll('[data-tools-filter]').forEach(function (el) {
    el.classList.toggle('active', el.getAttribute('data-tools-filter') === f);
  });
  _toolsInvRender();
}

function _toolsInvSearch(q) {
  _toolsInvQuery = (q || '').toLowerCase().trim();
  _toolsInvRender();
}

// ── Rendering (pure string builders — jsdom-testable) ─────────

function _toolsInvStateLabel(state) {
  var key = {
    on: 'toolsInv.stateOn',
    off: 'toolsInv.stateOff',
    standby: 'toolsInv.stateStandby',
    error: 'toolsInv.stateError',
  }[state] || 'toolsInv.stateOff';
  return t(key);
}

function _toolsInvStateIcon(state) {
  // Inline SVG glyphs (§3.4: no emoji, no unicode glyphs as affordances).
  if (state === 'on') {
    return '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  }
  if (state === 'standby') {
    return '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>';
  }
  if (state === 'error') {
    return '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
  }
  return '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><line x1="5.6" y1="5.6" x2="18.4" y2="18.4"/></svg>';
}

function _toolsInvToolMatches(tool, q) {
  if (!q) return true;
  var hay = (tool.name + ' ' + (tool.description || '')).toLowerCase();
  return hay.indexOf(q) !== -1;
}

function _toolsInvFamilyCounts(fam) {
  var on = 0, total = 0;
  (fam.tools || []).forEach(function (tl) { total++; if (tl.enabled) on++; });
  (fam.mcp_tools || []).forEach(function (tl) { total++; if (tl.enabled) on++; });
  return { on: on, total: total };
}

function _toolsInvFamilyVisible(fam, filter, q) {
  var c = _toolsInvFamilyCounts(fam);
  if (filter === 'on' && c.on === 0) return false;
  if (filter === 'off' && c.on > 0) return false;
  if (q) {
    var hay = (fam.key + ' ' + (fam.description || '') + ' ' + (fam.gate || '')).toLowerCase();
    if (hay.indexOf(q) !== -1) return true;
    // Family matches when ANY of its tools matches the query.
    return (fam.tools || []).some(function (tl) { return _toolsInvToolMatches(tl, q); }) ||
           (fam.mcp_tools || []).some(function (tl) { return _toolsInvToolMatches(tl, q); });
  }
  return true;
}

function _toolsInvRenderToolRow(tl, mcp) {
  var cls = 'tools-inv-tool' + (tl.enabled ? '' : ' is-off');
  var html = '<div class="' + cls + '">';
  html += '<div class="tools-inv-tool-head">';
  html += '<code class="tools-inv-tool-name">' + escapeHtml(tl.name) + '</code>';
  if (tl.write) {
    html += '<span class="tools-inv-badge is-write" title="' + escapeHtml(t('toolsInv.writeTitle')) + '">' + escapeHtml(t('toolsInv.writeBadge')) + '</span>';
  }
  if (mcp && tl.server) {
    html += '<span class="tools-inv-badge is-mcp">' + escapeHtml(tl.server) + '</span>';
  }
  // The 已禁用 badge is reserved for USER-level disables (an MCP tool the
  // operator toggled off in Settings → MCP). A built-in row that is off
  // because its FAMILY gate is closed needs no per-row badge — the family
  // card's state pill + gate line already say it, and a badge per row reads
  // as if someone had disabled each tool by hand.
  if (!tl.enabled && mcp) {
    html += '<span class="tools-inv-badge is-disabled">' + escapeHtml(t('toolsInv.disabledBadge')) + '</span>';
  }
  html += '</div>';
  if (tl.description) {
    html += '<div class="tools-inv-tool-desc">' + escapeHtml(tl.description) + '</div>';
  }
  if (tl.required && tl.required.length) {
    html += '<div class="tools-inv-tool-req">' + escapeHtml(t('toolsInv.required')) + ' ' +
      tl.required.map(function (p) { return '<code>' + escapeHtml(p) + '</code>'; }).join(' ') + '</div>';
  }
  html += '</div>';
  return html;
}

function _toolsInvRenderFamily(fam, q) {
  var c = _toolsInvFamilyCounts(fam);
  var state = fam.gate_state || 'off';
  var html = '<div class="tools-inv-family is-' + escapeHtml(state) + '">';
  html += '<div class="tools-inv-family-head">';
  html += '<span class="tools-inv-state is-' + escapeHtml(state) + '" title="' + escapeHtml(state) + '">' +
          _toolsInvStateIcon(state) + ' ' + escapeHtml(_toolsInvStateLabel(state)) + '</span>';
  html += '<span class="tools-inv-family-name">' + escapeHtml(fam.key) + '</span>';
  if (fam.source === 'plugin') {
    html += '<span class="tools-inv-badge is-plugin">' + escapeHtml(t('toolsInv.pluginBadge')) +
            (fam.plugin_name ? ' · ' + escapeHtml(fam.plugin_name) : '') + '</span>';
  }
  html += '<span class="tools-inv-family-desc">' + escapeHtml(fam.description || '') + '</span>';
  html += '<span class="tools-inv-family-count">' + c.on + '/' + c.total + '</span>';
  html += '</div>';
  // Gate line: how to turn this family on (human-readable, from the spec).
  if (state !== 'on' && fam.gate) {
    html += '<div class="tools-inv-gate">' + escapeHtml(t('toolsInv.gateLabel')) + ' ' + escapeHtml(fam.gate) + '</div>';
  }
  if (state === 'error' && fam.gate_reason) {
    html += '<div class="tools-inv-gate is-error">' + escapeHtml(fam.gate_reason) + '</div>';
  }
  var rows = (fam.tools || []).filter(function (tl) { return _toolsInvToolMatches(tl, q); });
  var mcpRows = (fam.mcp_tools || []).filter(function (tl) { return _toolsInvToolMatches(tl, q); });
  if (rows.length || mcpRows.length) {
    html += '<div class="tools-inv-tools">';
    rows.forEach(function (tl) { html += _toolsInvRenderToolRow(tl, false); });
    mcpRows.forEach(function (tl) { html += _toolsInvRenderToolRow(tl, true); });
    html += '</div>';
  } else if (!c.total) {
    html += '<div class="tools-inv-empty">' + escapeHtml(t('toolsInv.familyEmpty')) + '</div>';
  }
  html += '</div>';
  return html;
}

// Group display order + i18n titles. Display PREFERENCE only (mirrors
// skills.js::_skillsOrderedCategories): a category absent here still renders,
// appended alphabetically — a new spec category must never be silently hidden.
var _TOOLS_INV_GROUP_ORDER = ['search', 'project', 'browser', 'desktop', 'image',
                              'video', 'conversation', 'human', 'memory', 'skills',
                              'task', 'scheduler', 'swarm', 'mcp', 'custom'];

function _toolsInvGroupTitle(gid) {
  var key = 'toolsInv.group.' + gid;
  var v = t(key);
  // t() returns the key itself when missing — fall back to the raw id.
  return (v === key) ? gid : v;
}

function _toolsInvOrderedGroups(groups) {
  var byId = {};
  groups.forEach(function (g) { byId[g.id] = g; });
  var known = _TOOLS_INV_GROUP_ORDER.filter(function (id) { return byId[id]; });
  var extra = Object.keys(byId).filter(function (id) {
    return _TOOLS_INV_GROUP_ORDER.indexOf(id) === -1;
  }).sort();
  return known.concat(extra).map(function (id) { return byId[id]; });
}

function _toolsInvRender() {
  var body = document.getElementById('toolsInvBody');
  if (!body || !_toolsInvData) return;
  var inv = _toolsInvData;

  // Header badges
  var totalEl = document.getElementById('toolsInvTotalCount');
  var activeEl = document.getElementById('toolsInvActiveCount');
  if (totalEl) totalEl.textContent = t('toolsInv.countTotal', { n: inv.totals.tools });
  if (activeEl) activeEl.textContent = t('toolsInv.countActive', { n: inv.totals.active });

  var q = _toolsInvQuery;
  var filter = _toolsInvFilter;
  var html = '';
  var anyFamily = false;
  _toolsInvOrderedGroups(inv.groups || []).forEach(function (g) {
    var fams = (g.families || []).filter(function (f) {
      return _toolsInvFamilyVisible(f, filter, q);
    });
    if (!fams.length) return;
    anyFamily = true;
    html += '<div class="tools-inv-group">';
    html += '<div class="tools-inv-group-title">' + escapeHtml(_toolsInvGroupTitle(g.id)) + '</div>';
    fams.forEach(function (f) { html += _toolsInvRenderFamily(f, q); });
    html += '</div>';
  });
  if (!anyFamily) {
    html = '<p class="stg-empty">' + escapeHtml(t('toolsInv.noMatch')) + '</p>';
  }
  body.innerHTML = html;
}
