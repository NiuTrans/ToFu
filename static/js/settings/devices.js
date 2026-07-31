/* ═══════════════════════════════════════════════════════════════════
   settings/devices — RWA Devices 页(拍板 5A,docs/REMOTE_WORKTREE_DESIGN.md §5 P4)

   一屏管理 desktop agent(在线代理)与 bridge token:
     * GET  /api/v1/desktop/devices   → agents + tokens(元数据,不含原文)
     * POST /api/v1/desktop/token     → 颁发(原文只回这一次)
     * DELETE /api/v1/desktop/token/<id> → 吊销(仅自己的 bridge token)

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

function _populateDevicesTab() {
  var agentsEl = document.getElementById('devicesAgentsList');
  var tokensEl = document.getElementById('devicesTokensList');
  if (!agentsEl || !tokensEl) return;
  /* ── BOTH lanes change generation together ──
   * One request feeds both lists, so they must never display two different
   * answers to it. The old code reset ONLY the agents lane, which is exactly
   * what the bug screenshot shows: "正在加载…" above an already-settled
   * "还没有 bridge 令牌。" — a stale generation sitting under a live one. */
  var loading = '<p class="stg-loading">' + escapeHtml(t('settings.loading')) + '</p>';
  agentsEl.innerHTML = loading;
  tokensEl.innerHTML = loading;
  var mintBtn = /** @type {any} */ (document.getElementById('devicesMintBtn'));
  if (mintBtn && !mintBtn._devicesWired) {
    mintBtn._devicesWired = true;
    mintBtn.onclick = _devicesMintToken;
  }
  var copyBtn = /** @type {any} */ (document.getElementById('devicesCopyTokenBtn'));
  if (copyBtn && !copyBtn._devicesWired) {
    copyBtn._devicesWired = true;
    copyBtn.onclick = _devicesCopyMintedToken;
  }
  Promise.resolve(Api.desktop.devices()).then(function(d) {
    /* ── A null payload is a FAILURE, not an empty inventory ──
     * Api.desktop.devices() is declared {onError:'null'}, which api.js
     * documents as "rejections become null". So an unreachable backend
     * RESOLVES here with null and never reaches .catch — which had two
     * consequences, both wrong:
     *   1. the .catch branch below was structurally unreachable, so
     *      "loading…" could stay on screen permanently; and
     *   2. `(d && d.agents) || []` fed the EMPTY-state renderer, so a user
     *      whose agent was running fine was told "暂无设备连接" and would
     *      reasonably conclude it had died.
     * "We could not ask" and "you have none" are different facts. */
    if (!d) {
      _renderDevicesLoadFailed(null);
      return;
    }
    _renderDeviceAgents(d.agents || []);
    _renderDeviceTokens(d.tokens || []);
  }).catch(function(e) {
    // Still reachable for a genuine throw (a caller that dropped onError, or a
    // renderer bug). Both lanes report it — blanking one would silently claim
    // "no tokens" when the truth is that we never got an answer.
    _renderDevicesLoadFailed(e);
  });
}

/* Paint the SAME load-failure state into both lanes.
 *
 * Deliberately a distinct rendering from the empty state (.devices-load-failed
 * vs .stg-empty), because the two say different things and the panel used to
 * conflate them. */
function _renderDevicesLoadFailed(err) {
  var detail = (err && err.message) ? String(err.message) : '';
  var html = '<div class="devices-load-failed">' +
    '<span class="devices-load-failed-icon">⚠</span>' +
    '<span>' + escapeHtml(t('devices.loadFailed')) +
    (detail ? ' <span class="devices-load-failed-detail">' +
       escapeHtml(detail) + '</span>' : '') +
    '</span></div>';
  var agentsEl = document.getElementById('devicesAgentsList');
  var tokensEl = document.getElementById('devicesTokensList');
  if (agentsEl) agentsEl.innerHTML = html;
  if (tokensEl) tokensEl.innerHTML = html;
}

function _renderDeviceAgents(agents) {
  var el = document.getElementById('devicesAgentsList');
  if (!el) return;
  if (!agents.length) {
    el.innerHTML = '<p class="stg-empty" data-i18n="devices.empty">' +
      escapeHtml(t('devices.empty')) + '</p>';
    return;
  }
  var html = '<table class="stg-table"><thead><tr>' +
    '<th>' + escapeHtml(t('devices.colDevice')) + '</th>' +
    '<th>' + escapeHtml(t('devices.colPlatform')) + '</th>' +
    '<th>' + escapeHtml(t('devices.colRoots')) + '</th>' +
    '<th>' + escapeHtml(t('devices.colStatus')) + '</th>' +
    '</tr></thead><tbody>';
  agents.forEach(function(a) {
    var roots = (a.share_roots || []).map(function(r) { return r.name || r.path; });
    var online = !!a.online;
    html += '<tr class="devices-agent-row' + (online ? '' : ' devices-offline') + '">' +
      '<td>' + escapeHtml(a.name || a.agent_id) +
        ' <span class="stg-dim">(' + escapeHtml(String(a.agent_id || '').slice(0, 8)) + ')</span></td>' +
      '<td>' + escapeHtml(a.platform || '—') + '</td>' +
      '<td>' + (roots.length ? escapeHtml(roots.join(', ')) : '<span class="stg-dim">—</span>') + '</td>' +
      '<td>' + (online
          ? '<span class="devices-online-dot">●</span> ' + escapeHtml(t('devices.online'))
          : '<span class="stg-dim">○ ' + escapeHtml(t('devices.offline')) + '</span>') +
      '</td></tr>';
  });
  el.innerHTML = html + '</tbody></table>';
}

function _renderDeviceTokens(tokens) {
  var el = document.getElementById('devicesTokensList');
  if (!el) return;
  if (!tokens.length) {
    el.innerHTML = '<p class="stg-empty">' + escapeHtml(t('devices.noTokens')) + '</p>';
    return;
  }
  var html = '';
  tokens.forEach(function(k) {
    var created = k.created_at
      ? new Date(k.created_at * 1000).toISOString().slice(0, 10) : '—';
    html += '<div class="stg-row devices-token-row" data-key-id="' + escapeHtml(k.id) + '">' +
      '<span style="flex:1">' + escapeHtml(k.name || k.id) +
        ' <span class="stg-dim">' + escapeHtml(created) + '</span></span>' +
      '<button class="stg-btn stg-btn-danger devices-revoke-btn" data-key-id="' +
        escapeHtml(k.id) + '">' + escapeHtml(t('devices.revoke')) + '</button>' +
      '</div>';
  });
  el.innerHTML = html;
  el.querySelectorAll('.devices-revoke-btn').forEach(function(btn) {
    btn.onclick = function() { _devicesRevokeToken(btn.getAttribute('data-key-id'), btn); };
  });
}

function _devicesMintToken() {
  var nameInput = document.getElementById('devicesMintName');
  var btn = document.getElementById('devicesMintBtn');
  if (btn) btn.disabled = true;
  Api.desktop.mintToken(nameInput ? nameInput.value.trim() : '')
    .then(function(d) {
      if (btn) btn.disabled = false;
      if (!d || !d.token) {
        if (typeof showToast === 'function') showToast(t('devices.mintFailed'));
        return;
      }
      var box = document.getElementById('devicesMintedBox');
      var code = document.getElementById('devicesMintedToken');
      if (code) code.textContent = d.token;
      if (box) box.style.display = '';
      if (nameInput) nameInput.value = '';
      _populateDevicesTab();  // refresh the token list
    })
    .catch(function() {
      if (btn) btn.disabled = false;
      if (typeof showToast === 'function') showToast(t('devices.mintFailed'));
    });
}

function _devicesCopyMintedToken() {
  var code = document.getElementById('devicesMintedToken');
  if (!code || !code.textContent) return;
  var done = function() {
    if (typeof showToast === 'function') showToast(t('devices.copied'));
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(code.textContent).then(done, function() {});
  } else {
    var sel = window.getSelection();
    var range = document.createRange();
    range.selectNodeContents(code);
    sel.removeAllRanges();
    sel.addRange(range);
    done();
  }
}

function _devicesRevokeToken(keyId, btn) {
  if (!keyId) return;
  if (btn) btn.disabled = true;
  Api.desktop.revokeToken(keyId).then(function() {
    _populateDevicesTab();  // refresh
    if (typeof showToast === 'function') showToast(t('devices.revoked'));
  }).catch(function() {
    if (btn) btn.disabled = false;
    if (typeof showToast === 'function') showToast(t('devices.revokeFailed'));
  });
}
