/* ═══════════════════════════════════════════════════════════════════
   settings/local endpoints — extracted from settings.js (split 2026-05-28)

   Local providers: per-endpoint metrics, status pulses, bulk edit, model discovery.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */


// ══════════════════════════════════════════════════════
//  Local Providers — single card with multiple endpoints
// ══════════════════════════════════════════════════════

/**
 * Per-endpoint probe-status cache, keyed by URL.
 * Used while a probe is in flight (single-row test) so we can show an
 * "amber pulsing" indicator. Once metrics come back from the
 * /api/dispatch/endpoint-metrics poller, _localEndpointMetrics takes over
 * and is the source of truth for the inline status strip.
 *
 * Shape: { url: { status: 'pending'|'error', message: string, ts: number } }
 */
var _localEndpointStatus = {};

/**
 * Live per-endpoint metrics from the dispatcher (auto-polled).
 * Shape (per URL — see /api/dispatch/endpoint-metrics):
 *   { ttft_ms, latency_ms, throughput_tps, success_rate, total_requests,
 *     total_errors, rpm_current, rpm_limit, inflight, available,
 *     last_success_ts, last_error_ts, last_error_msg, consecutive_errors }
 */
var _localEndpointMetrics = {};
var _localEndpointMetricsTs = 0;
var _localMetricsTimer = null;
var _localMetricsInflight = false;

/** Fetch dispatcher per-endpoint metrics and refresh visible rows in-place. */
async function _refreshLocalEndpointMetrics() {
  if (_localMetricsInflight) return;
  _localMetricsInflight = true;
  try {
    var data = await Api.dispatch.endpointMetrics();
    if (!data) return;
    _localEndpointMetrics = data.endpoints || {};
    _localEndpointMetricsTs = Date.now();
    _refreshAllLocalEndpointRows();
  } catch (e) {
    // Silent — metrics are advisory; next tick will retry.
  } finally {
    _localMetricsInflight = false;
  }
}

/** Start polling /api/dispatch/endpoint-metrics every 10s while Settings open. */
function _startLocalMetricsPolling() {
  if (_localMetricsTimer) return;
  // First refresh fires immediately so values appear without waiting 10s.
  _refreshLocalEndpointMetrics();
  _localMetricsTimer = setInterval(_refreshLocalEndpointMetrics, 10000);
}

/** Stop the metrics poller (call when Settings closes). */
function _stopLocalMetricsPolling() {
  if (_localMetricsTimer) {
    clearInterval(_localMetricsTimer);
    _localMetricsTimer = null;
  }
}

/** Refresh all visible local-endpoint rows in-place using cached metrics. */
function _refreshAllLocalEndpointRows() {
  for (var pi = 0; pi < _stgProviders.length; pi++) {
    var p = _stgProviders[pi];
    if (!p || p.brand !== 'local') continue;
    var eps = p.endpoints || [];
    for (var ei = 0; ei < eps.length; ei++) {
      _refreshLocalEndpointRow(pi, ei);
    }
  }
}

/** Look up the metrics + probe status for a URL, normalized. */
function _epMetricsLookup(url) {
  if (!url) return null;
  var trimmed = url.trim();
  if (!trimmed) return null;
  // Try literal, then with no trailing slash (matches backend bucketing)
  var stripped = trimmed.replace(/\/+$/, '');
  return _localEndpointMetrics[trimmed] || _localEndpointMetrics[stripped] || null;
}

/** Add a new local-deployment provider (structured row UI). */
function addLocalProvider() {
  // Reuse the existing 'local' provider entry if there already is one — most
  // people have a single homogeneous fleet, so accumulating duplicate cards
  // would be confusing.
  for (var i = 0; i < _stgProviders.length; i++) {
    if (_stgProviders[i].brand === 'local') {
      _renderProvidersTab();
      var card = document.querySelector('.stg-provider-card[data-prov-idx="' + i + '"]');
      if (card) {
        card.classList.add('expanded');
        card.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
      return;
    }
  }
  _stgProviders.unshift({
    id: 'local_' + Date.now().toString(36),
    name: '本地部署模型',
    brand: 'local',
    enabled: true,
    endpoints: [''],
    base_url: '',
    api_keys: [''],
    models: [],
    thinking_format: '',
  });
  _renderProvidersTab();
  var first = document.querySelector('.stg-provider-card');
  if (first) {
    first.classList.add('expanded');
    first.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var inp = first.querySelector('input[data-local-endpoint]');
    if (inp) inp.focus();
  }
}

/** Sync provider.base_url with the first endpoint after any list mutation. */
function _syncLocalBaseUrl(p) {
  var firstNonEmpty = '';
  for (var i = 0; i < (p.endpoints || []).length; i++) {
    if (p.endpoints[i] && p.endpoints[i].trim()) {
      firstNonEmpty = p.endpoints[i].trim();
      break;
    }
  }
  p.base_url = firstNonEmpty;
}

/** Update a single endpoint URL in-place (called on row input blur). */
function _onLocalEndpointEdit(provIdx, epIdx, value) {
  var p = _stgProviders[provIdx];
  if (!p || !p.endpoints) return;
  var v = String(value || '').trim();
  // Cleared field → drop the row entirely so we don't show empty rows.
  if (!v) {
    p.endpoints.splice(epIdx, 1);
    if (p.endpoints.length === 0) p.endpoints.push('');
  } else {
    p.endpoints[epIdx] = v;
  }
  _syncLocalBaseUrl(p);
  _renderProvidersTab();
}

/** Append a new blank endpoint row. */
function _addLocalEndpoint(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  if (!Array.isArray(p.endpoints)) p.endpoints = [];
  p.endpoints.push('');
  _renderProvidersTab();
  // Focus the freshly-added blank input.
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (card) {
    var inputs = card.querySelectorAll('input[data-local-endpoint]');
    if (inputs.length) inputs[inputs.length - 1].focus();
  }
}

/** Remove a single endpoint row. */
function _deleteLocalEndpoint(provIdx, epIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.endpoints) return;
  p.endpoints.splice(epIdx, 1);
  if (p.endpoints.length === 0) p.endpoints.push('');
  _syncLocalBaseUrl(p);
  _renderProvidersTab();
}

/** Clear ALL endpoint rows (with confirm). */
async function _clearLocalEndpoints(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.endpoints) return;
  var n = p.endpoints.filter(function(u) { return u && u.trim(); }).length;
  if (n === 0) return;
  if (!await showConfirm('确定要清空全部 ' + n + ' 个端点 URL 吗？此操作不会立即保存到服务器。', { danger: true })) return;
  p.endpoints = [''];
  _syncLocalBaseUrl(p);
  _renderProvidersTab();
}

/** Open the bulk-edit modal — paste/edit a flat list of URLs. */
function _openBulkEditEndpoints(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var existing = (p.endpoints || []).filter(function(u) { return u && u.trim(); }).join('\n');
  var existingId = 'stgBulkEdit_' + provIdx;
  var prev = document.getElementById(existingId);
  if (prev) prev.remove();

  var html = '<div id="' + existingId + '" class="stg-modal-overlay" onclick="if(event.target===this)this.remove()">' +
    '<div class="stg-modal" style="max-width:640px;">' +
      '<div class="stg-modal-header">' +
        '<span class="stg-modal-title">📝 批量编辑端点 URL</span>' +
        '<button class="stg-modal-close" onclick="document.getElementById(\'' + existingId + '\').remove()">✕</button>' +
      '</div>' +
      '<div class="stg-modal-body">' +
        '<p class="stg-modal-desc">每行一个 URL。粘贴 <code>/chat/completions</code> 链接也会自动转换为 base URL。</p>' +
        '<textarea id="stgBulkEditTa_' + provIdx + '" rows="10" ' +
          'style="width:100%;font-family:ui-monospace,monospace;font-size:12px;" ' +
          'placeholder="http://10.0.0.5:8000/v1&#10;http://10.0.0.6:8000/v1">' +
          escapeHtml(existing) +
        '</textarea>' +
      '</div>' +
      '<div class="stg-modal-footer">' +
        '<button class="stg-btn-secondary" onclick="document.getElementById(\'' + existingId + '\').remove()">取消</button>' +
        '<button class="stg-btn-primary" onclick="_applyBulkEditEndpoints(' + provIdx + ')">应用</button>' +
      '</div>' +
    '</div>' +
  '</div>';
  document.body.insertAdjacentHTML('beforeend', html);
  setTimeout(function() {
    var ta = document.getElementById('stgBulkEditTa_' + provIdx);
    if (ta) ta.focus();
  }, 50);
}

function _applyBulkEditEndpoints(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var ta = document.getElementById('stgBulkEditTa_' + provIdx);
  if (!ta) return;
  var urls = String(ta.value || '').split(/\s*[\n,]\s*/)
    .map(function(s) { return s.trim(); })
    .filter(Boolean);
  // Dedupe while preserving order
  var seen = {}, deduped = [];
  for (var i = 0; i < urls.length; i++) {
    var k = urls[i].toLowerCase();
    if (!seen[k]) { seen[k] = true; deduped.push(urls[i]); }
  }
  p.endpoints = deduped.length ? deduped : [''];
  _syncLocalBaseUrl(p);
  var modal = document.getElementById('stgBulkEdit_' + provIdx);
  if (modal) modal.remove();
  _renderProvidersTab();
}

/** Force an immediate refresh of live metrics for the row's endpoint.
 *
 * The dispatcher exposes per-endpoint stats (TTFT, latency, throughput,
 * success rate) recorded automatically from real chat traffic — no
 * synthetic /models probe is needed. We just bump the global poller so
 * the user sees the freshest numbers without waiting for the next 10s tick.
 */
async function _probeLocalEndpoint(provIdx, epIdx) {
  await _refreshLocalEndpointMetrics();
}

/** In-place refresh of a single endpoint row's light + status text. */
function _refreshLocalEndpointRow(provIdx, epIdx) {
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var item = card.querySelector('.stg-ep-item[data-ep-idx="' + epIdx + '"]');
  if (!item) return;
  var p = _stgProviders[provIdx];
  if (!p) return;
  var url = (p.endpoints || [])[epIdx] || '';
  var light = item.querySelector('.stg-ep-light');
  if (light) {
    light.className = 'stg-ep-light ' + _epLightClass(url);
    light.title = _epLightTitle(url);
  }
  var msg = item.querySelector('.stg-ep-status');
  if (msg) msg.innerHTML = _epStatusInline(url);
  var badge = card.querySelector('.stg-ep-badge');
  if (badge) {
    var nonEmpty = (p.endpoints || []).filter(function(u) { return u && u.trim(); });
    badge.outerHTML = _localEndpointBadge(nonEmpty);
  }
}

/** Compute the light state for a row from live metrics + transient probe state. */
function _epRowState(url) {
  var probe = url ? _localEndpointStatus[url] : null;
  if (probe && probe.status === 'pending') {
    return { cls: 'pending', title: t('settings.epProbing'), kind: 'pending' };
  }
  var m = _epMetricsLookup(url);
  if (probe && probe.status === 'error' && (!m || !(m.total_requests > 0))) {
    return { cls: 'error',
             title: t('settings.epError') + ' — ' + (probe.message || ''),
             kind: 'probe-error' };
  }
  if (!m) {
    return { cls: 'unknown', title: t('settings.epNoTraffic'),
             kind: 'cold', metrics: null };
  }
  var sr = m.success_rate;
  var n = m.total_requests || 0;
  var recentlyFailing = (m.consecutive_errors > 0
                         && m.last_error_ts > m.last_success_ts);
  var lowSuccess = (sr !== null && sr !== undefined && n >= 5 && sr < 0.8);
  if (recentlyFailing || lowSuccess) {
    return { cls: 'error',
             title: m.last_error_msg
                    ? (t('settings.epRecentFailure') + ' — ' + m.last_error_msg)
                    : t('settings.epRecentFailure'),
             kind: 'unhealthy', metrics: m };
  }
  if (n === 0) {
    return { cls: 'unknown', title: t('settings.epNoTraffic'),
             kind: 'cold', metrics: m };
  }
  return { cls: 'ok', title: t('settings.epHealthy'),
           kind: 'healthy', metrics: m };
}

function _epLightClass(stateOrUrl) {
  // Backwards-compat: still accepts the old probe-status object.
  if (stateOrUrl && typeof stateOrUrl === 'object' && 'status' in stateOrUrl) {
    if (stateOrUrl.status === 'pending') return 'pending';
    return stateOrUrl.ok ? 'ok' : 'error';
  }
  return _epRowState(stateOrUrl).cls;
}

function _epLightTitle(stateOrUrl) {
  if (stateOrUrl && typeof stateOrUrl === 'object' && 'status' in stateOrUrl) {
    if (stateOrUrl.status === 'pending') return t('settings.epProbing');
    return stateOrUrl.ok
      ? t('settings.epHealthy')
      : (t('settings.epError') + ' — ' + (stateOrUrl.message || ''));
  }
  return _epRowState(stateOrUrl).title;
}

function _fmtMs(v) {
  if (v == null || v <= 0) return '—';
  if (v >= 1000) return (v / 1000).toFixed(v >= 10000 ? 0 : 1) + 's';
  return Math.round(v) + 'ms';
}
function _fmtTps(v) {
  if (v == null || v <= 0) return '—';
  if (v >= 100) return Math.round(v) + ' t/s';
  return v.toFixed(1) + ' t/s';
}
function _fmtPct(v) {
  if (v == null) return '—';
  return Math.round(v * 100) + '%';
}

/** Render the inline stats strip for one endpoint row. */
function _epStatusInline(stateOrUrl) {
  // Backwards-compat: legacy callers may pass the probe-status object.
  if (stateOrUrl && typeof stateOrUrl === 'object' && 'status' in stateOrUrl) {
    if (stateOrUrl.status === 'pending') {
      return '<span class="stg-ep-status-text muted">' +
             escapeHtml(t('settings.epProbing')) + '</span>';
    }
    return stateOrUrl.ok
      ? '<span class="stg-ep-status-text ok">' + escapeHtml(stateOrUrl.message || '') + '</span>'
      : '<span class="stg-ep-status-text err">' +
        escapeHtml(stateOrUrl.message || t('settings.epProbeFailed')) + '</span>';
  }
  var url = stateOrUrl;
  var st = _epRowState(url);
  var probe = url ? _localEndpointStatus[url] : null;
  if (st.kind === 'pending') {
    return '<span class="stg-ep-status-text muted">' +
           escapeHtml(t('settings.epProbing')) + '</span>';
  }
  if (st.kind === 'probe-error') {
    return '<span class="stg-ep-status-text err">' +
           escapeHtml(t('settings.epProbeFailed')) + ' · ' +
           escapeHtml((probe && probe.message) || '') + '</span>';
  }
  var m = st.metrics;
  if (!m || (m.total_requests || 0) === 0) {
    return '<span class="stg-ep-status-text muted">' +
           escapeHtml(t('settings.epNoTraffic')) + '</span>';
  }
  var pieces = [];
  if (m.ttft_ms != null)        pieces.push('TTFT <b>' + _fmtMs(m.ttft_ms) + '</b>');
  if (m.latency_ms != null)     pieces.push(escapeHtml(t('settings.epLatency')) + ' <b>' + _fmtMs(m.latency_ms) + '</b>');
  if (m.throughput_tps != null) pieces.push(escapeHtml(t('settings.epThroughput')) + ' <b>' + _fmtTps(m.throughput_tps) + '</b>');
  if (m.success_rate != null)   pieces.push(escapeHtml(t('settings.epSuccessRate')) + ' <b>' + _fmtPct(m.success_rate) + '</b>');
  if (m.inflight)               pieces.push(escapeHtml(t('settings.epInflight')) + ' ' + m.inflight);
  pieces.push((m.total_requests || 0) + ' ' + escapeHtml(t('settings.epRequests')));

  var cls = (st.cls === 'error') ? 'err' : (st.cls === 'ok' ? 'ok' : 'muted');
  var head = '<span class="stg-ep-status-text ' + cls + '">' + pieces.join(' · ') + '</span>';
  var when = '';
  if (st.kind === 'unhealthy' && m.last_error_ts) {
    when = ' <span class="stg-ep-status-when">· ' +
           escapeHtml(m.last_error_msg || t('settings.epError')) +
           ' · ' + _fmtRelative(m.last_error_ts * 1000) + '</span>';
  } else if (m.last_success_ts) {
    when = ' <span class="stg-ep-status-when">· ' +
           escapeHtml(t('settings.epLastSeen')) + ' ' +
           _fmtRelative(m.last_success_ts * 1000) + '</span>';
  }
  return head + when;
}

/** Build an endpoint count badge with an aggregate status summary. */
function _localEndpointBadge(urls) {
  var n = urls.length;
  if (n === 0) return '<span class="stg-badge">0 ' + escapeHtml(t('settings.endpointsSuffix')) + '</span>';
  var okCount = 0, errCount = 0, coldCount = 0;
  for (var i = 0; i < n; i++) {
    var st = _epRowState(urls[i]);
    if (st.cls === 'ok') okCount++;
    else if (st.cls === 'error') errCount++;
    else coldCount++;
  }
  var cls = 'unknown', dotTitle = t('settings.epNotProbed');
  var partial = t('settings.epPartialOk').replace('{ok}', String(okCount)).replace('{total}', String(n));
  if (errCount > 0 && okCount === 0) { cls = 'error'; dotTitle = t('settings.epAllFailed'); }
  else if (errCount > 0) { cls = 'mixed'; dotTitle = partial; }
  else if (okCount === n) { cls = 'ok'; dotTitle = t('settings.epAllOk'); }
  else if (okCount > 0) { cls = 'mixed'; dotTitle = partial; }
  return '<span class="stg-badge stg-ep-badge" title="' + escapeHtml(dotTitle) + '">' +
    '<span class="stg-ep-dot ' + cls + '"></span>' + n + ' ' + escapeHtml(t('settings.endpointsSuffix')) + '</span>';
}

/** Format a millisecond timestamp as a short relative string ("1分钟前"). */
function _fmtRelative(ts) {
  var diff = Math.max(0, (Date.now() - ts) / 1000);
  if (diff < 5) return t('settings.relJustNow');
  if (diff < 60) return Math.floor(diff) + ' ' + t('settings.relSecAgo');
  if (diff < 3600) return Math.floor(diff / 60) + ' ' + t('settings.relMinAgo');
  if (diff < 86400) return Math.floor(diff / 3600) + ' ' + t('settings.relHourAgo');
  return Math.floor(diff / 86400) + ' ' + t('settings.relDayAgo');
}

/** Render a single endpoint row (status light + URL input + actions). */
function _renderLocalEndpointRow(provIdx, epIdx, url) {
  var lightCls = _epLightClass(url);
  var lightTitle = _epLightTitle(url);
  var html = '<div class="stg-ep-item" data-ep-idx="' + epIdx + '">' +
    '<div class="stg-ep-row">' +
      '<span class="stg-ep-light ' + lightCls + '" title="' + escapeHtml(lightTitle) + '"></span>' +
      '<input type="text" data-local-endpoint value="' + escapeHtml(url || '') + '" ' +
        'placeholder="http://10.0.0.5:8000/v1" ' +
        'spellcheck="false" autocomplete="off" ' +
        'onchange="_onLocalEndpointEdit(' + provIdx + ',' + epIdx + ',this.value)">' +
      '<button class="stg-ep-btn" onclick="_probeLocalEndpoint(' + provIdx + ',' + epIdx + ')" ' +
        'title="' + escapeHtml(t('settings.testEndpointTitle')) + '">↻</button>' +
      '<button class="stg-ep-btn danger" onclick="_deleteLocalEndpoint(' + provIdx + ',' + epIdx + ')" ' +
        'title="' + escapeHtml(t('settings.deleteEndpointTitle')) + '">✕</button>' +
    '</div>' +
    '<div class="stg-ep-status">' + _epStatusInline(url) + '</div>' +
  '</div>';
  return html;
}

/** Render the entire endpoints section for a local provider. */
function _renderLocalEndpointsSection(provIdx, endpointList) {
  var html = '<div class="stg-field stg-ep-field">' +
    '<div class="stg-ep-header">' +
      '<label style="margin:0;">' + escapeHtml(t('settings.endpointUrlList')) + '</label>' +
      '<div class="stg-ep-toolbar">' +
        '<button class="stg-btn-add stg-ep-tb" onclick="_addLocalEndpoint(' + provIdx + ')" ' +
          'title="' + escapeHtml(t('settings.addEndpointTitle')) + '">' + escapeHtml(t('settings.addEndpoint')) + '</button>' +
        '<button class="stg-btn-add stg-ep-tb" onclick="_openBulkEditEndpoints(' + provIdx + ')" ' +
          'title="' + escapeHtml(t('settings.bulkEditTitle')) + '">' + escapeHtml(t('settings.bulkEdit')) + '</button>' +
        '<button class="stg-btn-add stg-ep-tb" onclick="_discoverLocalModels(' + provIdx + ')" ' +
          'title="' + escapeHtml(t('settings.probeAllTitle')) + '">' + escapeHtml(t('settings.probeAll')) + '</button>' +
        '<button class="stg-btn-add stg-ep-tb danger" onclick="_clearLocalEndpoints(' + provIdx + ')" ' +
          'title="' + escapeHtml(t('settings.clearAllTitle')) + '">' + escapeHtml(t('settings.clearAll')) + '</button>' +
      '</div>' +
    '</div>';

  // Visible rows. If endpointList is empty, show one blank row so the user
  // has somewhere to type.
  var rows = (endpointList && endpointList.length) ? endpointList : [''];
  html += '<div class="stg-ep-list">';
  for (var i = 0; i < rows.length; i++) {
    html += _renderLocalEndpointRow(provIdx, i, rows[i]);
  }
  html += '</div>';

  html += '<span class="stg-hint">' + escapeHtml(t('settings.localEndpointsHint')) + '</span>';
  html += '</div>';
  return html;
}

/** Discover models across all endpoints and merge them into the provider. */
async function _discoverLocalModels(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var urls = (p.endpoints && p.endpoints.length) ? p.endpoints : (p.base_url ? [p.base_url] : []);
  if (!urls.length) {
    showAlert('请先在"端点 URL 列表"中添加至少一个 URL。');
    return;
  }
  var statusEl = document.getElementById('stgLocalStatus_' + provIdx);
  if (statusEl) {
    statusEl.style.display = 'block';
    statusEl.className = 'stg-auto-status stg-auto-loading';
    statusEl.textContent = '正在并行探测 ' + urls.length + ' 个端点…';
  }

  var apiKey = (p.api_keys && p.api_keys[0]) || '';

  try {
    var data = await Api.providers.probeBulk(urls, apiKey);
    if (!data || !data.ok) {
      if (statusEl) {
        statusEl.className = 'stg-auto-status stg-auto-error';
        statusEl.textContent = data.error || '探测失败';
      }
      return;
    }

    // Per-endpoint status rows.
    var resultsList = [];
    var liveUrls = [];
    var unionModels = {};   // model_id -> model entry (first-seen wins)
    var firstThinking = '';
    var nowTs = Date.now();
    for (var i = 0; i < (data.results || []).length; i++) {
      var r = data.results[i] || {};
      var origUrl = urls[i];
      var normUrl = r.base_url || origUrl;
      if (r.ok) {
        liveUrls.push(normUrl);
        if (!firstThinking && r.thinking_format) firstThinking = r.thinking_format;
        for (var j = 0; j < (r.models || []).length; j++) {
          var m = r.models[j];
          if (m && m.model_id && !unionModels[m.model_id]) {
            unionModels[m.model_id] = m;
          }
        }
        resultsList.push({ url: normUrl, ok: true, n: (r.models || []).length });
        // Cache under both raw and normalized URL keys.
        _localEndpointStatus[origUrl] = _localEndpointStatus[normUrl] = {
          ok: true, status: 'ok',
          message: (r.models || []).length + ' 个模型',
          ts: nowTs, n_models: (r.models || []).length,
        };
      } else {
        resultsList.push({ url: normUrl, ok: false, error: r.error || '探测失败' });
        _localEndpointStatus[origUrl] = _localEndpointStatus[normUrl] = {
          ok: false, status: 'error',
          message: r.error || '探测失败',
          ts: nowTs, n_models: 0,
        };
      }
    }

    // Replace endpoints with normalized live ones (preserves dead ones too —
    // user may have just temporarily restarted a box).  We keep the original
    // order but use the normalized URL when probe returned one.
    var normByOriginal = {};
    for (var k = 0; k < (data.results || []).length; k++) {
      normByOriginal[urls[k]] = data.results[k] && data.results[k].base_url
        ? data.results[k].base_url : urls[k];
    }
    var newEndpoints = [];
    for (var u = 0; u < urls.length; u++) {
      newEndpoints.push(normByOriginal[urls[u]] || urls[u]);
    }
    p.endpoints = newEndpoints;
    p.base_url = newEndpoints[0] || '';

    // Merge model list (union across live endpoints).
    var existingById = {};
    for (var em = 0; em < (p.models || []).length; em++) {
      var em_id = p.models[em] && p.models[em].model_id;
      if (em_id) existingById[em_id] = p.models[em];
    }
    var merged = [];
    Object.keys(unionModels).forEach(function(mid) {
      merged.push(existingById[mid] || unionModels[mid]);
    });
    // Keep models that existed but weren't returned by any live endpoint
    // (could mean the only endpoint hosting them is currently down).
    Object.keys(existingById).forEach(function(mid) {
      if (!unionModels[mid]) merged.push(existingById[mid]);
    });
    p.models = merged;

    if (firstThinking && !p.thinking_format) p.thinking_format = firstThinking;

    if (statusEl) {
      var okN = liveUrls.length;
      var totN = (data.results || []).length;
      statusEl.className = okN > 0 ? 'stg-auto-status stg-auto-success' : 'stg-auto-status stg-auto-error';
      statusEl.textContent = '探测完成：' + okN + '/' + totN + ' 个端点存活，模型并集 ' +
        Object.keys(unionModels).length + ' 个（详见各端点状态指示）。';
    }

    _renderProvidersTab();
    _renderPresetsTab(_serverConfig);
  } catch (e) {
    if (statusEl) {
      statusEl.className = 'stg-auto-status stg-auto-error';
      statusEl.textContent = '网络错误: ' + e.message;
    }
  }
}

// ══════════════════════════════════════════════════════
//  Live state
// ══════════════════════════════════════════════════════

// providers[]: each has { id, name, base_url, api_keys:[], enabled, models:[], extra_headers:{} }
//   models[]: each has { model_id, aliases:[], capabilities:[], rpm, cost, thinking_default }
let _stgProviders = [];
let _stgPresets = {};  // kept for backward-compat save/load, but no longer used for preset→model mapping

