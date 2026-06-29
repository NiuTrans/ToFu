// ══════════════════════════════════════════════════════════════════
//  relay-admin.js — Settings → relay-admin tabs (Users / Pricing /
//  Codes / Payments).
//
//  Tabs are HIDDEN by default. They appear only when:
//    1. /api/v1/auth/mode reports mode === 'multi-user'
//    2. /api/v1/users/me reports the current principal has the
//       'admin' scope (the unified gate already ensures any logged-
//       in admin sees the underlying API endpoints).
//
//  All four tabs are pure fetch + render layers per CLAUDE.md §16.
//  Every mutation goes through /api/v1/billing/* or /api/v1/users/*;
//  the server is the source of truth.
// ══════════════════════════════════════════════════════════════════

(function() {
  'use strict';

  function _esc(s) {
    if (typeof escapeHtml === 'function') return escapeHtml(s);
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // Defensive i18n shim. This file runs on the standalone /admin page,
  // which loads i18n.js (so the global `t()` is normally present); the
  // wrapper degrades gracefully to the raw key string if it is ever
  // absent, rather than throwing a ReferenceError that would blank the
  // whole admin UI.
  /** @param {string} key @param {Object} [params] */
  function _t(key, params) {
    if (typeof t === 'function') return t(key, params);
    var s = key;
    if (params) {
      for (var k in params) {
        if (Object.prototype.hasOwnProperty.call(params, k)) {
          s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), params[k]);
        }
      }
    }
    return s;
  }

  /** @param {any} micro @param {number} [precision] */
  function _fmtMicro(micro, precision) {
    return ((micro || 0) / 1_000_000).toFixed(precision == null ? 4 : precision);
  }

  function _fmtTime(ts) {
    if (!ts) return '—';
    try { return new Date(ts * 1000).toLocaleString(); }
    catch (_) { return String(ts); }
  }

  /** @returns {Promise<any>} */
  async function _api(url, opts) {
    const r = await fetch(typeof apiUrl === 'function' ? apiUrl(url) : url,
      Object.assign({credentials: 'same-origin',
                      headers: {'Content-Type': 'application/json'}},
                     opts || {}));
    let body = null;
    try { body = await r.json(); } catch (_) {}
    if (!r.ok) {
      const msg = (body && (body.error || body.message)) || ('HTTP ' + r.status);
      throw new Error(msg);
    }
    return body;
  }

  // ── Visibility gate ─────────────────────────────────────────────

  async function _shouldShowAdminTabs() {
    try {
      const mode = await _api('/api/v1/auth/mode');
      if (mode.mode !== 'multi-user') return false;
      const me = await _api('/api/v1/users/me');
      const scopes = (me.principal && me.principal.scopes) || [];
      return scopes.includes('admin');
    } catch (e) {
      console.warn('[RelayAdmin] visibility check failed:', e);
      return false;
    }
  }

  async function refreshTabVisibility() {
    const show = await _shouldShowAdminTabs();
    document.querySelectorAll('.relay-admin-tab').forEach(el => {
      el.style.display = show ? '' : 'none';
    });
  }
  window.refreshRelayAdminTabs = refreshTabVisibility;

  // ── Users tab ───────────────────────────────────────────────────

  async function refreshUsers() {
    const block = document.getElementById('relayUsersBlock');
    if (!block) return;
    try {
      const r = await _api('/api/v1/users');
      const users = r.users || [];
      const bill = _billingEnabled;  // hide money columns in agent-only mode
      const colspan = bill ? 7 : 6;
      block.innerHTML = `
        <div class="settings-row" style="gap:8px; margin-bottom:12px;">
          <input type="text" id="newUserEmail" placeholder="email@example.com" class="settings-input" style="flex:1">
          <input type="password" id="newUserPassword" placeholder="${_esc(_t('relayAdmin.pwPlaceholder'))}" class="settings-input" style="flex:1">
          <select id="newUserRole" class="settings-input">
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
          <button class="btn btn-primary btn-sm" onclick="relayAdminCreateUser()">${_esc(_t('relayAdmin.create'))}</button>
        </div>
        <table class="settings-table">
          <thead><tr>
            <th>${_esc(_t('relayAdmin.colEmail'))}</th><th>${_esc(_t('relayAdmin.colRole'))}</th><th>${_esc(_t('relayAdmin.colStatus'))}</th>${bill ? '<th>' + _esc(_t('relayAdmin.colBalance')) + '</th>' : ''}
            <th>${_esc(_t('relayAdmin.colCreated'))}</th><th>${_esc(_t('relayAdmin.colLastLogin'))}</th><th>${_esc(_t('relayAdmin.colActions'))}</th>
          </tr></thead>
          <tbody>${users.map(u => `
            <tr data-uid="${_esc(u.id)}">
              <td>${_esc(u.email)}</td>
              <td>${_esc(u.role)}</td>
              <td>${_esc(u.status)}</td>
              ${bill ? '<td class="balance-cell">…</td>' : ''}
              <td>${_esc(_fmtTime(u.created_at))}</td>
              <td>${_esc(_fmtTime(u.last_login_at))}</td>
              <td>
                ${bill ? `<button class="btn btn-secondary btn-sm" onclick="relayAdminTopup('${_esc(u.id)}')">${_esc(_t('relayAdmin.topup'))}</button>` : ''}
                ${bill ? `<button class="btn btn-secondary btn-sm" onclick="relayAdminViewPayments('${_esc(u.id)}','${_esc(u.email)}')">${_esc(_t('relayAdmin.payments'))}</button>` : ''}
                <button class="btn btn-secondary btn-sm" onclick="relayAdminToggleStatus('${_esc(u.id)}', '${_esc(u.status)}')">${u.status === 'active' ? _esc(_t('relayAdmin.disable')) : _esc(_t('relayAdmin.enable'))}</button>
              </td>
            </tr>
            <tr class="pay-drill" data-drill="${_esc(u.id)}" style="display:none"><td colspan="${colspan}" style="background:var(--panel-2,#232a3a);padding:0;"></td></tr>`).join('') || `<tr><td colspan="${colspan}" style="text-align:center;opacity:0.5">${_esc(_t('relayAdmin.noUsers'))}</td></tr>`}</tbody>
        </table>`;
      // Lazy-load each user's balance (only in full-relay mode).
      if (bill) {
        for (const u of users) {
          try {
            const w = await _api('/api/v1/billing/wallet?user_id=' +
                                  encodeURIComponent(u.id));
            const cell = block.querySelector(`tr[data-uid="${u.id}"] .balance-cell`);
            if (cell) cell.textContent = _fmtMicro(w.balance_micro) + ' c';
          } catch (_) { /* swallow per-row errors */ }
        }
      }
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">' + _esc(_t('relayAdmin.loadFailed', { err: e.message })) + '</span>';
    }
  }

  async function relayAdminCreateUser() {
    const email = document.getElementById('newUserEmail').value.trim();
    const password = document.getElementById('newUserPassword').value;
    const role = document.getElementById('newUserRole').value;
    if (!email || !password) { showAlert(_t('relayAdmin.needEmailPw')); return; }
    try {
      await _api('/api/v1/users', {
        method: 'POST',
        body: JSON.stringify({email, password, role}),
      });
      document.getElementById('newUserEmail').value = '';
      document.getElementById('newUserPassword').value = '';
      refreshUsers();
    } catch (e) { showAlert(_t('relayAdmin.createFailed', { err: e.message })); }
  }
  window.relayAdminCreateUser = relayAdminCreateUser;

  async function relayAdminTopup(userId) {
    const amt = await showPrompt(_t('relayAdmin.topupAmount'), { defaultValue: '100' });
    if (!amt) return;
    const credits = parseFloat(String(amt));
    if (!isFinite(credits) || credits <= 0) { showAlert(_t('relayAdmin.invalidAmount')); return; }
    const note = await showPrompt(_t('relayAdmin.topupNote'), { defaultValue: _t('relayAdmin.topupNoteDefault') });
    try {
      await _api('/api/v1/billing/deposit', {
        method: 'POST',
        body: JSON.stringify({
          user_id: userId,
          amount_micro: Math.round(credits * 1_000_000),
          kind: 'adjust_credit',
          note: note || '',
        }),
      });
      refreshUsers();
    } catch (e) { showAlert(_t('relayAdmin.topupFailed', { err: e.message })); }
  }
  window.relayAdminTopup = relayAdminTopup;

  // Per-user payments drill-down. Toggles an inline row under the user
  // that lazy-loads /api/v1/billing/payments?user_id= (admin-scoped).
  async function relayAdminViewPayments(userId, email) {
    const drill = document.querySelector(`tr.pay-drill[data-drill="${CSS.escape(userId)}"]`);
    if (!drill) return;
    const cell = drill.querySelector('td');
    if (drill.style.display !== 'none') {  // toggle closed
      drill.style.display = 'none';
      return;
    }
    drill.style.display = '';
    cell.innerHTML = '<div style="padding:10px;opacity:.6;">' + _esc(_t('relayAdmin.loadingPayments')) + '</div>';
    try {
      const r = await _api('/api/v1/billing/payments?limit=100&user_id=' +
                            encodeURIComponent(userId));
      const payments = r.payments || [];
      cell.innerHTML = `
        <div style="padding:10px 14px;">
          ${_esc(_t('relayAdmin.paymentsOf', { email: email, n: payments.length }))}
          <table class="settings-table" style="margin-top:6px;">
            <thead><tr>
              <th>${_esc(_t('relayAdmin.colTime'))}</th><th>${_esc(_t('relayAdmin.colProvider'))}</th><th>${_esc(_t('relayAdmin.colAmount'))}</th><th>${_esc(_t('relayAdmin.colCurrency'))}</th>
              <th>${_esc(_t('relayAdmin.colCredited'))}</th><th>${_esc(_t('relayAdmin.colStatus'))}</th><th>${_esc(_t('relayAdmin.colExtId'))}</th>
            </tr></thead>
            <tbody>${payments.map(p => `
              <tr>
                <td>${_esc(_fmtTime(p.created_at))}</td>
                <td>${_esc(p.provider)}</td>
                <td>${(p.amount_minor / 100).toFixed(2)}</td>
                <td>${_esc(p.currency)}</td>
                <td>${_fmtMicro(p.credit_micro)} c</td>
                <td>${_esc(p.status)}</td>
                <td><code style="font-size:11px;">${_esc(p.provider_id || '—')}</code></td>
              </tr>`).join('') || '<tr><td colspan="7" style="text-align:center;opacity:0.5">' + _esc(_t('relayAdmin.noPayments')) + '</td></tr>'}</tbody>
          </table>
        </div>`;
    } catch (e) {
      cell.innerHTML = '<div style="padding:10px;color:var(--danger,#e25)">' +
        _esc(_t('relayAdmin.loadFailed', { err: e.message })) + '</div>';
    }
  }
  window.relayAdminViewPayments = relayAdminViewPayments;

  async function relayAdminToggleStatus(userId, currentStatus) {
    const next = currentStatus === 'active' ? 'suspended' : 'active';
    if (!await showConfirm(_t('relayAdmin.toggleConfirm', { status: next }))) return;
    try {
      await _api('/api/v1/users/' + encodeURIComponent(userId), {
        method: 'PATCH',
        body: JSON.stringify({status: next}),
      });
      refreshUsers();
    } catch (e) { showAlert(_t('relayAdmin.updateFailed', { err: e.message })); }
  }
  window.relayAdminToggleStatus = relayAdminToggleStatus;

  // ── Pricing tab (margin-only) ───────────────────────────────────
  //
  // Per-model RATES are NO LONGER editable here. They are authoritative in
  // lib/pricing.py (the single cost engine lib.cost.compute_cost reads them),
  // so a second writable rate table would only drift — exactly the bug the
  // 2026-06-24 unification removed. The ONLY billing knob still tunable from
  // the UI is the relay profit margin; the model rates below are read-only,
  // shown for reference.

  async function refreshPricing() {
    const block = document.getElementById('relayPricingBlock');
    if (!block) return;
    try {
      const r = await _api('/api/v1/billing/pricing');
      const models = r.models || {};
      const margin = r.default_margin || 0;
      const _roCell = (v) => `<td style="opacity:.7">${v ? _fmtMicro(v) : '—'}</td>`;
      const _roRow = (name, p) => {
        p = p || {};
        return `<tr><td><code>${_esc(name)}</code></td>` +
          _roCell(p.input_per_mtok_micro) + _roCell(p.output_per_mtok_micro) +
          _roCell(p.cache_read_per_mtok_micro) + _roCell(p.cache_write_per_mtok_micro) +
          `</tr>`;
      };
      block.innerHTML = `
        <div class="settings-row" style="gap:8px;margin-bottom:8px;align-items:center;">
          <label>${_esc(_t('relayAdmin.marginLabel'))}</label>
          <input type="number" id="priceMargin" min="0" max="10000" step="1"
                 class="settings-input" style="width:100px" value="${(margin * 100)}">
          <button class="btn btn-primary btn-sm" onclick="relayAdminSaveMargin()">${_esc(_t('relayAdmin.saveMargin'))}</button>
        </div>
        <span class="settings-desc" style="display:block;margin:0 0 12px;">${_esc(_t('relayAdmin.marginDesc'))}</span>
        <div id="pricingSaveResult"></div>
        <p class="settings-desc" style="margin:12px 0 6px;">
          ${_t('relayAdmin.ratesReadonly')}
        </p>
        <table class="settings-table">
          <thead><tr>
            <th>${_esc(_t('relayAdmin.colModel'))}</th><th>${_esc(_t('relayAdmin.colInput'))}</th><th>${_esc(_t('relayAdmin.colOutput'))}</th>
            <th>${_esc(_t('relayAdmin.colCacheHit'))}</th><th>${_esc(_t('relayAdmin.colCacheWrite'))}</th>
          </tr></thead>
          <tbody>
            ${_roRow(_t('relayAdmin.defaultModelFallback'), r.default_model || {})}
            ${Object.entries(models).map(([n, p]) => _roRow(n, p)).join('')}
          </tbody>
        </table>`;
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">' + _esc(_t('relayAdmin.loadFailed', { err: e.message })) + '</span>';
    }
  }

  async function relayAdminSaveMargin() {
    const marginPct = parseFloat(document.getElementById('priceMargin').value);
    if (!isFinite(marginPct) || marginPct < 0) { showAlert(_t('relayAdmin.invalidMargin')); return; }
    const div = document.getElementById('pricingSaveResult');
    try {
      await _api('/api/v1/billing/pricing', {
        method: 'PUT',
        body: JSON.stringify({ default_margin: marginPct / 100 }),
      });
      if (div) div.innerHTML = '<p style="color:var(--accent,#5a8);margin:8px 0 0;">' +
        _esc(_t('relayAdmin.marginSaved')) + '</p>';
    } catch (e) {
      if (div) div.innerHTML = '<p style="color:var(--danger,#e25);margin:8px 0 0;">' +
        _esc(_t('relayAdmin.saveFailed', { err: e.message })) + '</p>';
    }
  }
  window.relayAdminSaveMargin = relayAdminSaveMargin;

  // ── Redeem codes tab ────────────────────────────────────────────

  async function refreshCodes() {
    const block = document.getElementById('relayCodesBlock');
    if (!block) return;
    try {
      const r = await _api('/api/v1/billing/redeem-codes?limit=50');
      const codes = r.codes || [];
      block.innerHTML = `
        <div class="settings-row" style="gap:8px; margin-bottom:12px; flex-wrap:wrap;">
          <input type="number" id="codeBatchCount" min="1" max="1000" value="10" class="settings-input" style="width:80px" placeholder="${_esc(_t('relayAdmin.codeCountPh'))}">
          <input type="number" id="codeBatchAmount" min="1" value="100" class="settings-input" style="width:120px" placeholder="${_esc(_t('relayAdmin.codeAmountPh'))}">
          <input type="number" id="codeExpiresIn" min="0" max="365" value="30" class="settings-input" style="width:100px" placeholder="${_esc(_t('relayAdmin.codeExpiresPh'))}">
          <input type="text" id="codeBatchName" class="settings-input" style="flex:1" placeholder="${_esc(_t('relayAdmin.codeBatchPh'))}">
          <button class="btn btn-primary btn-sm" onclick="relayAdminMintCodes()">${_esc(_t('relayAdmin.generate'))}</button>
        </div>
        <div id="mintResult"></div>
        <table class="settings-table" style="margin-top:14px;">
          <thead><tr>
            <th>${_esc(_t('relayAdmin.colCode'))}</th><th>${_esc(_t('relayAdmin.colAmount'))}</th><th>${_esc(_t('relayAdmin.colBatch'))}</th><th>${_esc(_t('relayAdmin.colCreated'))}</th>
            <th>${_esc(_t('relayAdmin.colStatus'))}</th><th>${_esc(_t('relayAdmin.colRedeemer'))}</th>
          </tr></thead>
          <tbody>${codes.map(c => `
            <tr>
              <td><code>${_esc(c.code)}</code></td>
              <td>${_fmtMicro(c.amount_micro)} c</td>
              <td>${_esc(c.batch || '—')}</td>
              <td>${_esc(_fmtTime(c.created_at))}</td>
              <td>${c.redeemed_by ? _esc(_t('relayAdmin.codeRedeemed')) : _esc(_t('relayAdmin.codeUnredeemed'))}</td>
              <td>${_esc(c.redeemed_by || '—')}</td>
            </tr>`).join('') || '<tr><td colspan="6" style="text-align:center;opacity:0.5">' + _esc(_t('relayAdmin.noCodes')) + '</td></tr>'}</tbody>
        </table>`;
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">' + _esc(_t('relayAdmin.loadFailed', { err: e.message })) + '</span>';
    }
  }

  async function relayAdminMintCodes() {
    const count = parseInt(document.getElementById('codeBatchCount').value, 10);
    const amount = parseFloat(document.getElementById('codeBatchAmount').value);
    const expiresIn = parseInt(document.getElementById('codeExpiresIn').value, 10);
    const batch = document.getElementById('codeBatchName').value.trim();
    if (!isFinite(count) || count < 1 ||
        !isFinite(amount) || amount <= 0) {
      showAlert(_t('relayAdmin.invalidParams')); return;
    }
    try {
      const r = await _api('/api/v1/billing/redeem-codes', {
        method: 'POST',
        body: JSON.stringify({
          count, amount_micro: Math.round(amount * 1_000_000),
          expires_in_days: expiresIn || 0, batch,
        }),
      });
      const div = document.getElementById('mintResult');
      const text = (r.codes || []).join('\n');
      div.innerHTML = `
        <div style="margin-top:8px;padding:10px;background:rgba(80,180,160,0.06);border-left:3px solid var(--accent,#5a8);">
          <div style="margin-bottom:6px;">${_t('relayAdmin.codesGenerated', { n: r.codes.length, amount: _fmtMicro(r.amount_micro) })}
            <button class="btn btn-secondary btn-sm" onclick="navigator.clipboard.writeText(this.parentNode.querySelector('pre').textContent)">${_esc(_t('relayAdmin.copyAll'))}</button>
          </div>
          <pre style="font-family:monospace;font-size:12px;background:#000;color:#9f9;padding:8px;border-radius:4px;max-height:200px;overflow:auto;">${_esc(text)}</pre>
        </div>`;
      refreshCodes();
    } catch (e) { showAlert(_t('relayAdmin.generateFailed', { err: e.message })); }
  }
  window.relayAdminMintCodes = relayAdminMintCodes;

  // ── Payments tab ────────────────────────────────────────────────

  async function refreshPayments() {
    const block = document.getElementById('relayPaymentsBlock');
    if (!block) return;
    try {
      // No "list all payments" admin endpoint yet; iterate by user
      // would be O(N²). For phase 1 just show the operator's own
      // payments + a hint that the per-user view lives in the Users
      // tab (drill-down is a future enhancement).
      const r = await _api('/api/v1/billing/payments?limit=100');
      const payments = r.payments || [];
      block.innerHTML = `
        <p class="settings-desc">${_esc(_t('relayAdmin.paymentsDesc'))}</p>
        <table class="settings-table">
          <thead><tr>
            <th>${_esc(_t('relayAdmin.colTime'))}</th><th>${_esc(_t('relayAdmin.colProvider'))}</th><th>${_esc(_t('relayAdmin.colAmount'))}</th><th>${_esc(_t('relayAdmin.colCurrency'))}</th>
            <th>${_esc(_t('relayAdmin.colCredited'))}</th><th>${_esc(_t('relayAdmin.colStatus'))}</th><th>${_esc(_t('relayAdmin.colExtId'))}</th>
          </tr></thead>
          <tbody>${payments.map(p => `
            <tr>
              <td>${_esc(_fmtTime(p.created_at))}</td>
              <td>${_esc(p.provider)}</td>
              <td>${(p.amount_minor / 100).toFixed(2)}</td>
              <td>${_esc(p.currency)}</td>
              <td>${_fmtMicro(p.credit_micro)} c</td>
              <td>${_esc(p.status)}</td>
              <td><code style="font-size:11px;">${_esc(p.provider_id || '—')}</code></td>
            </tr>`).join('') || '<tr><td colspan="7" style="text-align:center;opacity:0.5">' + _esc(_t('relayAdmin.noPaymentsYet')) + '</td></tr>'}</tbody>
        </table>`;
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">' + _esc(_t('relayAdmin.loadFailed', { err: e.message })) + '</span>';
    }
  }

  // ── Standalone /admin page support ──────────────────────────────
  // The four panels also live on the dedicated /admin page (served from
  // static/admin.html, reusing the dashboard shell). That page exposes a
  // simple tab strip whose buttons call window.relayAdminSwitch(name) and
  // sets window.__RELAY_ADMIN_PAGE = true so we render directly instead of
  // monkey-patching the Settings modal (which doesn't exist there).
  function relayAdminSwitch(name) {
    document.querySelectorAll('.pane').forEach(p =>
      p.classList.toggle('active', p.id === 'pane-' + name));
    document.querySelectorAll('.nav-tabs button').forEach(b =>
      b.classList.toggle('active', b.dataset.pane === name));
    if (name === 'relayUsers')    refreshUsers();
    if (name === 'relayPricing')  refreshPricing();
    if (name === 'relayCodes')    refreshCodes();
    if (name === 'relayPayments') refreshPayments();
  }
  window.relayAdminSwitch = relayAdminSwitch;

  // Relay billing posture (full-relay vs agent-only). Set during boot;
  // when false we hide the money panels (Pricing/Codes/Payments) and the
  // per-user balance/top-up controls because the backend returns 404 on
  // every money-moving route in agent-only mode.
  let _billingEnabled = true;
  let _modelRelayEnabled = true;

  async function _loadRelayPolicy() {
    try {
      const caps = await _api('/api/v1/capabilities');
      const relay = (caps && (caps.relay || (caps.data && caps.data.relay))) || {};
      if (typeof relay.billing_enabled === 'boolean') {
        _billingEnabled = relay.billing_enabled;
      }
      if (typeof relay.model_relay_enabled === 'boolean') {
        _modelRelayEnabled = relay.model_relay_enabled;
      }
    } catch (e) {
      console.warn('[RelayAdmin] relay policy load failed:', e);
    }
    window.__RELAY_BILLING_ENABLED = _billingEnabled;
    window.__RELAY_MODEL_ENABLED = _modelRelayEnabled;
  }

  async function _bootStandalonePage() {
    const gate = document.getElementById('adminGate');
    const shell = document.getElementById('adminShell');
    const ok = await _shouldShowAdminTabs();
    if (!ok) {
      if (gate) gate.style.display = '';
      if (shell) shell.style.display = 'none';
      return;
    }
    if (gate) gate.style.display = 'none';
    if (shell) shell.style.display = '';
    await _loadRelayPolicy();
    // Hide the three money panels + tab buttons when billing is off.
    if (!_billingEnabled) {
      ['relayPricing', 'relayCodes', 'relayPayments'].forEach(name => {
        const btn = document.querySelector(`.nav-tabs button[data-pane="${name}"]`);
        if (btn) btn.style.display = 'none';
      });
    }
    // Surface a banner whenever the deployment is restricted on EITHER
    // axis (no billing, or BYO-only model access). The two flags are
    // independent (see lib/relay_config.py).
    if (!_billingEnabled || !_modelRelayEnabled) {
      const banner = document.getElementById('agentOnlyBanner');
      const txt = document.getElementById('agentOnlyBannerText');
      if (txt) {
        const parts = [];
        if (!_modelRelayEnabled) {
          parts.push(_t('relayAdmin.bannerByoOnly'));
        }
        if (!_billingEnabled) {
          parts.push(_t('relayAdmin.bannerNoBilling'));
        }
        txt.innerHTML = parts.join(' ');
      }
      if (banner) banner.style.display = '';
    }
    relayAdminSwitch('relayUsers');
  }

  if (window.__RELAY_ADMIN_PAGE) {
    if (document.readyState !== 'loading') {
      _bootStandalonePage();
    } else {
      document.addEventListener('DOMContentLoaded', _bootStandalonePage);
    }
    return;  // standalone page never touches the Settings modal
  }

  // ── Hook into the existing tab-switch flow (in-app Settings) ────

  const _origSwitch = window.switchSettingsTab;
  if (typeof _origSwitch === 'function') {
    window.switchSettingsTab = function(tabId) {
      _origSwitch.call(this, tabId);
      if (tabId === 'relayUsers')    refreshUsers();
      if (tabId === 'relayPricing')  refreshPricing();
      if (tabId === 'relayCodes')    refreshCodes();
      if (tabId === 'relayPayments') refreshPayments();
    };
  }

  // Refresh visibility when Settings opens.
  const _origOpenSettings = window.openSettings;
  if (typeof _origOpenSettings === 'function') {
    window.openSettings = function() {
      _origOpenSettings.apply(this, arguments);
      refreshTabVisibility();
    };
  }

  // Also refresh on initial load (in case the user is already on a
  // relay-admin tab from URL state).
  if (document.readyState !== 'loading') {
    refreshTabVisibility();
  } else {
    document.addEventListener('DOMContentLoaded', refreshTabVisibility);
  }
})();
