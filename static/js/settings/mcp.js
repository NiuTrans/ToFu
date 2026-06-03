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
var _mcpSearchQuery = '';
var _mcpInstallTarget = null;  // CatalogEntry being installed
var _mcpInstallIsReinstall = false;  // true = editing existing (stored env will be honoured)

/**
 * Load MCP tab data — fetch catalog with install/connect status.
 */
async function _populateMcpTab() {
  var grid = document.getElementById('mcpCatalogGrid');
  if (grid) grid.innerHTML = '<p class="stg-loading">正在加载…</p>';
  try {
    var r = await Api.mcp.catalogList();
    if (!r || !r.ok) throw new Error('HTTP ' + (r ? r.status : 'no response'));
    var data = await r.json();
    _mcpCatalog = data.catalog || [];

    _renderMcpCategoryBar();
    _renderMcpCatalog();
    _renderMcpInstalled();
    _mcpUpdateToolCount();
  } catch (e) {
    if (grid) grid.innerHTML = '<p class="stg-empty">加载 Apps 失败: ' + escapeHtml(e.message) + '</p>';
    debugLog('[MCP] Failed to load catalog: ' + e.message, 'error');
  }
}

/** Update the tool count badge. */
function _mcpUpdateToolCount() {
  var badge = document.getElementById('mcpToolCount');
  if (!badge) return;
  var total = 0;
  _mcpCatalog.forEach(function(e) { total += (e.tools_count || 0); });
  badge.textContent = total + ' tools';
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
  var html = '<button class="mcp-cat-pill' + (_mcpActiveCategory === 'all' ? ' active' : '') + '" onclick="_mcpSetCategory(\'all\')">全部 <span class="mcp-cat-count">' + _mcpCatalog.length + '</span></button>';
  var order = ['Development','Data & DB','Communication','Search & Web','Productivity','DevOps','Finance','Design','Other'];
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

function _mcpFilterCatalog(query) {
  _mcpSearchQuery = (query || '').toLowerCase().trim();
  _renderMcpCatalog();
}

/** Filter catalog entries by active category + search query. */
function _mcpFilteredCatalog() {
  return _mcpCatalog.filter(function(e) {
    if (_mcpActiveCategory !== 'all' && e.category !== _mcpActiveCategory) return false;
    if (_mcpSearchQuery) {
      var hay = (e.name + ' ' + e.description + ' ' + (e.tags || []).join(' ')).toLowerCase();
      return hay.indexOf(_mcpSearchQuery) !== -1;
    }
    return true;
  });
}

/** Render the main catalog grid. */
function _renderMcpCatalog() {
  var grid = document.getElementById('mcpCatalogGrid');
  if (!grid) return;
  var items = _mcpFilteredCatalog();
  if (items.length === 0) {
    grid.innerHTML = '<p class="stg-empty">没有匹配的 App。</p>';
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
    var stateClass = connected ? ' connected' : installed ? ' installed' : '';
    html += '<div class="mcp-app-card' + stateClass + '">';
    html += '<div class="mcp-app-icon">' + (e.icon || '🔌') + '</div>';
    html += '<div class="mcp-app-name"><span class="mcp-app-name-text">' + escapeHtml(e.name) + '</span>';
    if (connected) html += '<span class="mcp-app-status on"><span class="dot"></span>ON</span>';
    else if (installed) html += '<span class="mcp-app-status off">IDLE</span>';
    html += '</div>';
    html += '<div class="mcp-app-desc">' + escapeHtml(e.description || '') + '</div>';
    // Footer: repo link (left) + tools count / action buttons (right)
    html += '<div class="mcp-app-footer">';
    if (e.url) {
      html += '<a class="mcp-app-repo" href="' + escapeHtml(e.url) + '" target="_blank" rel="noopener" title="Source Repository">' + REPO_SVG + ' Repo</a>';
    } else {
      html += '<span></span>';
    }
    html += '<div class="mcp-app-action">';
    if (connected) {
      if (e.server_version) {
        html += '<span class="mcp-app-version" title="' + escapeHtml((e.server_impl_name || e.id) + ' v' + e.server_version) + '">v' + escapeHtml(e.server_version) + '</span>';
      }
      if (e.tools_count) {
        html += '<span class="mcp-app-tools-count">' + (e.tools_count || 0) + ' tools</span>';
      }
      html += '<button class="btn btn-secondary btn-xs" onclick="_mcpUninstall(\'' + escapeHtml(e.id) + '\')" title="断开连接，但保留已填写的凭据，方便下次一键重新启用">卸载</button>';
    } else if (installed) {
      html += '<button class="btn btn-primary btn-xs" onclick="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\', true)" title="编辑凭据并重新连接（已有凭据会回填为默认；留空则沿用）">连接</button>';
      html += '<button class="btn btn-secondary btn-xs" onclick="_mcpPurge(\'' + escapeHtml(e.id) + '\')" title="彻底删除配置，包括已保存的凭据">清除凭据</button>';
    } else {
      // If the catalog entry has NO required env vars, skip the modal
      // entirely and one-click-install with the built-in defaults. The
      // modal is only useful when the user must fill something in
      // (API keys, tokens, etc.) — showing a form full of optional
      // "PATH TO EXECUTABLE / TIMEOUT / MAX CONCURRENCY" tweaks for
      // servers like Hope just creates confusion.
      var _needsInput = (e.env_specs || []).some(function(s) { return s.required; });
      if (_needsInput) {
        html += '<button class="btn btn-primary btn-xs" onclick="_mcpOpenInstallModal(\'' + escapeHtml(e.id) + '\')">安装</button>';
      } else {
        html += '<button class="btn btn-primary btn-xs" onclick="_mcpQuickInstall(\'' + escapeHtml(e.id) + '\')" title="无需配置，点击直接安装并连接；如需修改默认值可在安装后点“连接”">安装</button>';
      }
    }
    html += '</div></div>';  // action + footer
    html += '</div>';  // card
  });
  grid.innerHTML = html;
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
      countEl.textContent = total + ' installed';
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
async function _mcpQuickInstall(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  if (!entry) return;
  debugLog('[MCP] Quick-installing ' + serverId + ' (no required env)…', 'info');
  try {
    var data = await Api.mcp.catalogInstall(serverId, {});
    if (data && data.ok) {
      debugLog('[MCP] Installed ' + serverId + ': ' + (data.tools_count || 0) + ' tools', 'success');
      await _populateMcpTab();
    } else {
      // Installation failed — fall back to opening the modal so the user
      // can inspect the default values and/or override them. This is the
      // safety net for "hope binary not on PATH" kinds of errors.
      debugLog('[MCP] Quick install failed (' + (data.error || 'unknown') + '); opening install modal for ' + serverId, 'warning');
      alert('一键安装失败: ' + (data.error || '未知错误') + '\n\n将打开高级设置，可手动调整参数后重试。');
      _mcpOpenInstallModal(serverId);
    }
  } catch (e) {
    debugLog('[MCP] Quick install error for ' + serverId + ': ' + e.message, 'error');
    alert('一键安装失败: ' + e.message + '\n\n将打开高级设置，可手动调整参数后重试。');
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
  var _icon = entry.icon || '🔌';
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
    var inputType = (spec.secret !== false) ? 'password' : 'text';
    var hasStored = storedKeys.indexOf(spec.key) !== -1;
    var html = '<div class="stg-field">';
    html += '<label>' + escapeHtml(spec.label || spec.key);
    if (spec.required) html += ' <span style="color:#ef4444;">*</span>';
    if (hasStored) html += ' <span style="color:#10b981;font-size:11px;">● 已保存</span>';
    html += '</label>';
    var ph = hasStored ? '已保存，留空则沿用；填写即覆盖' : (spec.hint || '');
    html += '<input type="' + inputType + '" class="mcp-env-input" data-key="' + escapeHtml(spec.key) + '" data-has-stored="' + (hasStored ? '1' : '0') + '" placeholder="' + escapeHtml(ph) + '">';
    html += '</div>';
    return html;
  }

  if (specs.length === 0) {
    fieldsHtml = '<p class="mcp-install-noenv">无需配置，直接安装即可。</p>';
  } else {
    var required = specs.filter(function(s) { return s.required; });
    var optional = specs.filter(function(s) { return !s.required; });
    required.forEach(function(spec) { fieldsHtml += _renderSpec(spec); });
    if (optional.length > 0) {
      fieldsHtml += '<details class="mcp-advanced-toggle" style="margin-top:12px;">';
      fieldsHtml += '<summary style="cursor:pointer;color:var(--text-muted);font-size:12px;user-select:none;">▸ 高级设置（可选，' + optional.length + ' 项）</summary>';
      fieldsHtml += '<div style="margin-top:8px;">';
      optional.forEach(function(spec) { fieldsHtml += _renderSpec(spec); });
      fieldsHtml += '</div></details>';
    }
  }
  document.getElementById('mcpInstallFields').innerHTML = fieldsHtml;

  var btn = document.getElementById('mcpInstallBtn');
  btn.disabled = false;
  btn.textContent = _mcpInstallIsReinstall ? '保存并连接' : '安装并连接';

  document.getElementById('mcpInstallOverlay').style.display = 'flex';
}

function _mcpCloseInstallModal(evt) {
  if (evt && evt.target !== evt.currentTarget) return;
  document.getElementById('mcpInstallOverlay').style.display = 'none';
  _mcpInstallTarget = null;
}

async function _mcpDoInstall() {
  if (!_mcpInstallTarget) return;
  var btn = document.getElementById('mcpInstallBtn');
  var status = document.getElementById('mcpInstallStatus');
  btn.disabled = true;
  btn.textContent = '安装中…';
  status.style.display = 'block';
  status.className = 'mcp-install-status info';
  status.textContent = '正在启动 ' + _mcpInstallTarget.name + '…';

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
    if (data && data.ok) {
      status.className = 'mcp-install-status success';
      status.textContent = '✓ ' + _mcpInstallTarget.name + ' 已安装 — ' + (data.tools_count || 0) + ' 个工具可用';
      debugLog('[MCP] Installed ' + _mcpInstallTarget.id + ': ' + (data.tools_count || 0) + ' tools', 'success');
      // Refresh catalog after a short delay so user sees the success
      setTimeout(function() {
        _mcpCloseInstallModal();
        _populateMcpTab();
      }, 1200);
    } else {
      status.className = 'mcp-install-status error';
      status.textContent = '✕ ' + (data.error || '安装失败');
      btn.disabled = false;
      btn.textContent = '重试';
    }
  } catch (e) {
    status.className = 'mcp-install-status error';
    status.textContent = '✕ ' + e.message;
    btn.disabled = false;
    btn.textContent = '重试';
  }
}

// ── Uninstall / Reconnect ──

// Soft uninstall: disconnect + disable, but keep credentials for easy re-enable.
async function _mcpUninstall(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  var name = entry ? entry.name : serverId;
  if (!confirm('卸载 ' + name + '？\n\n将断开连接并禁用，但会保留已填写的凭据；下次点击“连接”可一键重新启用，无需再填一遍。\n\n如需彻底清除凭据，请先卸载，再在空闲卡片上点“清除凭据”。')) return;

  try {
    var data = await Api.mcp.catalogUninstall(serverId, false);
    if (!data || !data.ok) { alert('卸载失败: ' + ((data && data.error) || '未知错误')); return; }
    debugLog('[MCP] Uninstalled ' + serverId + (data.purged ? ' (purged)' : ' (soft, env kept)'), 'info');
    await _populateMcpTab();
  } catch (e) {
    alert('卸载失败: ' + e.message);
  }
}

// Hard purge: remove config row entirely, forgetting stored credentials.
async function _mcpPurge(serverId) {
  var entry = _mcpCatalog.find(function(e) { return e.id === serverId; });
  var name = entry ? entry.name : serverId;
  if (!confirm('清除 ' + name + ' 的全部配置和已保存的凭据？\n\n此操作不可恢复，重新启用时需要再次填写所有凭据。')) return;

  try {
    var data = await Api.mcp.catalogUninstall(serverId, true);
    if (!data || !data.ok) { alert('清除失败: ' + ((data && data.error) || '未知错误')); return; }
    debugLog('[MCP] Purged ' + serverId, 'info');
    await _populateMcpTab();
  } catch (e) {
    alert('清除失败: ' + e.message);
  }
}

async function _mcpConnectAll() {
  var btn = document.getElementById('mcpConnectAllBtn');
  if (btn) { btn.disabled = true; btn.textContent = '连接中…'; }
  try {
    var data = await Api.mcp.connectAll();
    if (!data || !data.ok) { alert('连接失败: ' + ((data && data.error) || '未知错误')); return; }
    var total = data.total_tools || 0;
    var count = Object.keys(data.servers || {}).length;
    debugLog('[MCP] Connected all: ' + count + ' server(s), ' + total + ' tools', 'success');
    await _populateMcpTab();
  } catch (e) {
    alert('连接失败: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '全部连接'; }
  }
}

async function _mcpReconnect(serverId) {
  try {
    var data = await Api.mcp.connectOne(serverId);
    if (!data || !data.ok) { alert('连接失败: ' + ((data && data.error) || '未知错误')); return; }
    debugLog('[MCP] Reconnected ' + serverId + ': ' + (data.tools_count || 0) + ' tools', 'success');
    await _populateMcpTab();
  } catch (e) {
    alert('连接失败: ' + e.message);
  }
}

// ── Manual add (advanced) ──

function _mcpTransportChanged() {
  var transport = (document.getElementById('mcpNewTransport') || {}).value || 'stdio';
  var stdioFields = document.getElementById('mcpStdioFields');
  var sseFields = document.getElementById('mcpSseFields');
  if (stdioFields) stdioFields.style.display = transport === 'stdio' ? '' : 'none';
  if (sseFields) sseFields.style.display = transport === 'sse' ? '' : 'none';
}

async function _mcpSaveServer() {
  var name = (document.getElementById('mcpNewName') || {}).value || '';
  name = name.trim();
  if (!name) { alert('请输入服务器名称'); return; }

  var transport = (document.getElementById('mcpNewTransport') || {}).value || 'stdio';
  var payload = { name: name, transport: transport, enabled: true };

  if (transport === 'stdio') {
    payload.command = (document.getElementById('mcpNewCommand') || {}).value || '';
    var argsText = (document.getElementById('mcpNewArgs') || {}).value || '';
    payload.args = argsText.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
    if (!payload.command) { alert('请输入命令 (command)'); return; }
  } else {
    payload.url = (document.getElementById('mcpNewUrl') || {}).value || '';
    if (!payload.url) { alert('请输入 SSE URL'); return; }
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

  try {
    var data = await Api.mcp.serverCreate(payload);
    if (!data || !data.ok) { alert('保存失败: ' + ((data && data.error) || '未知错误')); return; }

    // Auto-connect
    await Api.mcp.connectOne(name);

    debugLog('[MCP] Server "' + name + '" saved & connected', 'success');
    await _populateMcpTab();
  } catch (e) {
    alert('保存失败: ' + e.message);
  }
}
