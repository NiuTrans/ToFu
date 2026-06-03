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

  function _fmtMicro(micro, precision) {
    return ((micro || 0) / 1_000_000).toFixed(precision == null ? 4 : precision);
  }

  function _fmtTime(ts) {
    if (!ts) return '—';
    try { return new Date(ts * 1000).toLocaleString(); }
    catch (_) { return String(ts); }
  }

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
      block.innerHTML = `
        <div class="settings-row" style="gap:8px; margin-bottom:12px;">
          <input type="text" id="newUserEmail" placeholder="email@example.com" class="settings-input" style="flex:1">
          <input type="password" id="newUserPassword" placeholder="临时密码" class="settings-input" style="flex:1">
          <select id="newUserRole" class="settings-input">
            <option value="user">user</option>
            <option value="admin">admin</option>
          </select>
          <button class="btn btn-primary btn-sm" onclick="relayAdminCreateUser()">创建</button>
        </div>
        <table class="settings-table">
          <thead><tr>
            <th>邮箱</th><th>角色</th><th>状态</th><th>余额</th>
            <th>注册时间</th><th>最近登录</th><th>操作</th>
          </tr></thead>
          <tbody>${users.map(u => `
            <tr data-uid="${_esc(u.id)}">
              <td>${_esc(u.email)}</td>
              <td>${_esc(u.role)}</td>
              <td>${_esc(u.status)}</td>
              <td class="balance-cell">…</td>
              <td>${_esc(_fmtTime(u.created_at))}</td>
              <td>${_esc(_fmtTime(u.last_login_at))}</td>
              <td>
                <button class="btn btn-secondary btn-sm" onclick="relayAdminTopup('${_esc(u.id)}')">+充值</button>
                <button class="btn btn-secondary btn-sm" onclick="relayAdminToggleStatus('${_esc(u.id)}', '${_esc(u.status)}')">${u.status === 'active' ? '停用' : '启用'}</button>
              </td>
            </tr>`).join('') || '<tr><td colspan="7" style="text-align:center;opacity:0.5">还没有用户</td></tr>'}</tbody>
        </table>`;
      // Lazy-load each user's balance.
      for (const u of users) {
        try {
          const w = await _api('/api/v1/billing/wallet?user_id=' +
                                encodeURIComponent(u.id));
          const cell = block.querySelector(`tr[data-uid="${u.id}"] .balance-cell`);
          if (cell) cell.textContent = _fmtMicro(w.balance_micro) + ' c';
        } catch (_) { /* swallow per-row errors */ }
      }
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">加载失败:' + _esc(e.message) + '</span>';
    }
  }

  async function relayAdminCreateUser() {
    const email = document.getElementById('newUserEmail').value.trim();
    const password = document.getElementById('newUserPassword').value;
    const role = document.getElementById('newUserRole').value;
    if (!email || !password) { alert('需要邮箱和密码'); return; }
    try {
      await _api('/api/v1/users', {
        method: 'POST',
        body: JSON.stringify({email, password, role}),
      });
      document.getElementById('newUserEmail').value = '';
      document.getElementById('newUserPassword').value = '';
      refreshUsers();
    } catch (e) { alert('创建失败:' + e.message); }
  }
  window.relayAdminCreateUser = relayAdminCreateUser;

  async function relayAdminTopup(userId) {
    const amt = prompt('充值金额(credits,正数):', '100');
    if (!amt) return;
    const credits = parseFloat(amt);
    if (!isFinite(credits) || credits <= 0) { alert('无效金额'); return; }
    const note = prompt('备注(可选):', '管理员充值');
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
    } catch (e) { alert('充值失败:' + e.message); }
  }
  window.relayAdminTopup = relayAdminTopup;

  async function relayAdminToggleStatus(userId, currentStatus) {
    const next = currentStatus === 'active' ? 'suspended' : 'active';
    if (!confirm(`将该用户改为 ${next}?`)) return;
    try {
      await _api('/api/v1/users/' + encodeURIComponent(userId), {
        method: 'PATCH',
        body: JSON.stringify({status: next}),
      });
      refreshUsers();
    } catch (e) { alert('更新失败:' + e.message); }
  }
  window.relayAdminToggleStatus = relayAdminToggleStatus;

  // ── Pricing tab ─────────────────────────────────────────────────

  async function refreshPricing() {
    const block = document.getElementById('relayPricingBlock');
    if (!block) return;
    try {
      const r = await _api('/api/v1/billing/pricing');
      const models = r.models || {};
      const margin = r.default_margin || 0;
      block.innerHTML = `
        <p class="settings-desc">默认利润率: <strong>${(margin * 100).toFixed(0)}%</strong>
          (基础价 × (1 + 利润率) = 客户最终价)</p>
        <table class="settings-table">
          <thead><tr>
            <th>模型</th>
            <th>输入(µ/Mtok)</th>
            <th>输出(µ/Mtok)</th>
            <th>缓存命中(µ/Mtok)</th>
            <th>缓存写入(µ/Mtok)</th>
          </tr></thead>
          <tbody>${Object.entries(models).map(([name, p]) => `
            <tr>
              <td><code>${_esc(name)}</code></td>
              <td>${(p.input_per_mtok_micro || 0).toLocaleString()}</td>
              <td>${(p.output_per_mtok_micro || 0).toLocaleString()}</td>
              <td>${(p.cache_read_per_mtok_micro || 0).toLocaleString() || '—'}</td>
              <td>${(p.cache_write_per_mtok_micro || 0).toLocaleString() || '—'}</td>
            </tr>`).join('')}</tbody>
        </table>
        <p class="settings-desc" style="margin-top:14px;">
          目前只读。直接编辑 <code>data/config/pricing.json</code> 后,任意 API 请求会触发热重载——无需重启。
          1 credit ≈ US $0.001 (默认换算)。
        </p>`;
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">加载失败:' + _esc(e.message) + '</span>';
    }
  }

  // ── Redeem codes tab ────────────────────────────────────────────

  async function refreshCodes() {
    const block = document.getElementById('relayCodesBlock');
    if (!block) return;
    try {
      const r = await _api('/api/v1/billing/redeem-codes?limit=50');
      const codes = r.codes || [];
      block.innerHTML = `
        <div class="settings-row" style="gap:8px; margin-bottom:12px; flex-wrap:wrap;">
          <input type="number" id="codeBatchCount" min="1" max="1000" value="10" class="settings-input" style="width:80px" placeholder="个数">
          <input type="number" id="codeBatchAmount" min="1" value="100" class="settings-input" style="width:120px" placeholder="单张金额(credits)">
          <input type="number" id="codeExpiresIn" min="0" max="365" value="30" class="settings-input" style="width:100px" placeholder="N 天后过期">
          <input type="text" id="codeBatchName" class="settings-input" style="flex:1" placeholder="批次名(可选)">
          <button class="btn btn-primary btn-sm" onclick="relayAdminMintCodes()">生成</button>
        </div>
        <div id="mintResult"></div>
        <table class="settings-table" style="margin-top:14px;">
          <thead><tr>
            <th>代码</th><th>金额</th><th>批次</th><th>创建时间</th>
            <th>状态</th><th>使用人</th>
          </tr></thead>
          <tbody>${codes.map(c => `
            <tr>
              <td><code>${_esc(c.code)}</code></td>
              <td>${_fmtMicro(c.amount_micro)} c</td>
              <td>${_esc(c.batch || '—')}</td>
              <td>${_esc(_fmtTime(c.created_at))}</td>
              <td>${c.redeemed_by ? '已使用' : '未使用'}</td>
              <td>${_esc(c.redeemed_by || '—')}</td>
            </tr>`).join('') || '<tr><td colspan="6" style="text-align:center;opacity:0.5">还没有兑换码</td></tr>'}</tbody>
        </table>`;
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">加载失败:' + _esc(e.message) + '</span>';
    }
  }

  async function relayAdminMintCodes() {
    const count = parseInt(document.getElementById('codeBatchCount').value, 10);
    const amount = parseFloat(document.getElementById('codeBatchAmount').value);
    const expiresIn = parseInt(document.getElementById('codeExpiresIn').value, 10);
    const batch = document.getElementById('codeBatchName').value.trim();
    if (!isFinite(count) || count < 1 ||
        !isFinite(amount) || amount <= 0) {
      alert('参数无效'); return;
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
          <div style="margin-bottom:6px;">已生成 <strong>${r.codes.length}</strong> 张兑换码,每张 ${_fmtMicro(r.amount_micro)} credits。
            <button class="btn btn-secondary btn-sm" onclick="navigator.clipboard.writeText(this.parentNode.querySelector('pre').textContent)">复制全部</button>
          </div>
          <pre style="font-family:monospace;font-size:12px;background:#000;color:#9f9;padding:8px;border-radius:4px;max-height:200px;overflow:auto;">${_esc(text)}</pre>
        </div>`;
      refreshCodes();
    } catch (e) { alert('生成失败:' + e.message); }
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
        <p class="settings-desc">显示当前管理员账号的支付记录。如需查看某个用户的支付记录,请在「用户」标签页中筛选(规划中)。</p>
        <table class="settings-table">
          <thead><tr>
            <th>时间</th><th>提供商</th><th>金额</th><th>币种</th>
            <th>入账</th><th>状态</th><th>外部 ID</th>
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
            </tr>`).join('') || '<tr><td colspan="7" style="text-align:center;opacity:0.5">还没有支付记录</td></tr>'}</tbody>
        </table>`;
    } catch (e) {
      block.innerHTML = '<span style="color:var(--accent-danger,#e25)">加载失败:' + _esc(e.message) + '</span>';
    }
  }

  // ── Hook into the existing tab-switch flow ──────────────────────

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
