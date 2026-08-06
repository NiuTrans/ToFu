/* ═══════════════════════════════════════════════════════════════════
   settings/credentials_vault.js — 「凭证保管库」(credential vault)

   Renders the credential-vault section in Settings → Advanced.

   The vault stores GitHub PATs / PyPI tokens / … Fernet-encrypted on the
   server (data/config/credentials_vault.json). Two hard privacy rules the
   UI encodes:
     • LIST responses carry {name, hint, note, timestamps} ONLY — the value
       NEVER rides the list payload. The row shows name + hint ('ghp_…3V8')
       + note, never the secret.
     • The ONLY plaintext egress is POST …/<name>/reveal, fired explicitly
       by the「查看」button. The revealed value is shown inline with a 复制
       button and AUTO-HIDES after ~30s so a shoulder-surfed screen doesn't
       keep the secret on display.

   Server-side name validation (lowercase [a-z0-9_.-], 1-64) and every
   error message from the API envelope are surfaced VERBATIM in the msg div
   — the server is the single source of truth for the rule, we keep no
   second copy here.

   Concatenated by lib/js_bundler.py — shared window scope, no imports.
   ═══════════════════════════════════════════════════════════════════ */

/** Revealed plaintext + hide timer, per credential name. Module-private;
 *  never re-rendered into the list payload — only into the one open row. */
var _credVaultRevealed = {};   /* name → value */
var _credVaultHideTimers = {}; /* name → setTimeout handle */
var CRED_VAULT_REVEAL_MS = 30000;

function _renderCredentialsVault() {
  var box = document.getElementById('credentialsVaultList');
  if (!box) return;
  box.innerHTML = String(safeHtml`<div class="priv-host-loading">${t('common.loading') || '加载中…'}</div>`);
  Api.credentials.list().then(function (data) {
    var creds = (data && data.credentials) || [];
    box.innerHTML = _credentialsVaultHtml(creds);
  }).catch(function (e) {
    console.warn('[CredVault] list failed', e);
    box.innerHTML = String(safeHtml`<div class="priv-host-empty">${t('settings.credVaultLoadFail') || '加载失败'}</div>`);
  });
}

function _credentialsVaultHtml(creds) {
  var rows = creds.length
    ? creds.map(_credentialRowHtml).join('')
    : String(safeHtml`<div class="priv-host-empty">${t('settings.credVaultEmpty') || '保管库为空。'}</div>`);
  return rows + _credentialAddHtml();
}

function _credentialRowHtml(row) {
  var name = row.name || '';
  var hint = row.hint || '';
  var note = row.note || '';
  var updated = _credVaultRelTime(row.updated_at);
  var revealed = Object.prototype.hasOwnProperty.call(_credVaultRevealed, name);

  var valueHtml = revealed
    ? String(safeHtml`
      <div class="cred-vault-value">
        <code class="cred-vault-secret">${_credVaultRevealed[name]}</code>
        <button class="priv-host-btn" onclick="_credentialCopy('${raw(name)}')">${t('settings.credVaultCopy') || '复制'}</button>
        <button class="priv-host-btn" onclick="_credentialHide('${raw(name)}')">${t('settings.credVaultHide') || '隐藏'}</button>
      </div>`)
    : '';

  return String(safeHtml`
    <div class="priv-host-row cred-vault-row" id="credVaultRow_${raw(_credVaultDomId(name))}">
      <div class="cred-vault-main">
        <div class="cred-vault-idline">
          <span class="cred-vault-name">${name}</span>
          <span class="cred-vault-hint">${hint}</span>
        </div>
        ${note ? safeHtml`<div class="cred-vault-note">${note}</div>` : ''}
      </div>
      <span class="cred-vault-time">${updated}</span>
      <div class="priv-host-actions">
        <button class="priv-host-btn" onclick="_credentialReveal('${raw(name)}')">${t('settings.credVaultReveal') || '查看'}</button>
        <button class="priv-host-btn danger" onclick="_credentialRemove('${raw(name)}')">${t('settings.credVaultDelete') || '删除'}</button>
      </div>
      ${raw(valueHtml)}
    </div>`);
}

function _credentialAddHtml() {
  return String(safeHtml`
    <div class="priv-host-add cred-vault-add">
      <input type="text" id="credVaultNameInput" class="priv-host-input"
             placeholder="${t('settings.credVaultNamePlaceholder') || '名称（如 github_pat）'}"
             onkeydown="if(event.key==='Enter'){event.preventDefault();_credentialAdd();}">
      <input type="password" id="credVaultValueInput" class="priv-host-input"
             placeholder="${t('settings.credVaultValuePlaceholder') || '值（只在本机加密落盘）'}"
             onkeydown="if(event.key==='Enter'){event.preventDefault();_credentialAdd();}">
      <input type="text" id="credVaultNoteInput" class="priv-host-input"
             placeholder="${t('settings.credVaultNotePlaceholder') || '备注（可选）'}"
             onkeydown="if(event.key==='Enter'){event.preventDefault();_credentialAdd();}">
      <button class="priv-host-btn primary" onclick="_credentialAdd()">${t('settings.credVaultAdd') || '添加'}</button>
      <div id="credVaultMsg" class="priv-host-msg"></div>
    </div>`);
}

function _credVaultDomId(v) {
  return String(v).replace(/[^a-zA-Z0-9]/g, '_');
}

function _credVaultSetMsg(text, cls) {
  var el = document.getElementById('credVaultMsg');
  if (el) {
    el.textContent = text || '';
    el.className = 'priv-host-msg' + (cls ? ' ' + cls : '');
  }
}

/** Relative "updated" label. updated_at arrives as an ISO-8601 string (or
 *  epoch seconds/ms) — parse defensively; unparseable → show it raw. */
function _credVaultRelTime(ts) {
  if (!ts) return '';
  var ms;
  if (typeof ts === 'number') {
    ms = ts < 1e12 ? ts * 1000 : ts;
  } else {
    ms = Date.parse(String(ts));
  }
  if (!ms || isNaN(ms)) return String(ts);
  var diff = Date.now() - ms;
  if (diff < 0) diff = 0;
  var mins = Math.floor(diff / 60000);
  if (mins < 1) return t('settings.credVaultJustNow') || '刚刚更新';
  if (mins < 60) {
    return (t('settings.credVaultMinutesAgo') || '{n} 分钟前更新').replace('{n}', String(mins));
  }
  var hours = Math.floor(mins / 60);
  if (hours < 24) {
    return (t('settings.credVaultHoursAgo') || '{n} 小时前更新').replace('{n}', String(hours));
  }
  return (t('settings.credVaultDaysAgo') || '{n} 天前更新').replace('{n}', String(Math.floor(hours / 24)));
}

function _credentialAdd() {
  var nameEl = document.getElementById('credVaultNameInput');
  var valueEl = document.getElementById('credVaultValueInput');
  var noteEl = document.getElementById('credVaultNoteInput');
  if (!nameEl || !valueEl) return;
  var name = (nameEl.value || '').trim();
  var value = valueEl.value || '';
  var note = (noteEl && noteEl.value || '').trim();
  if (!name || !value) {
    _credVaultSetMsg(t('settings.credVaultNeedNameValue') || '请填写名称和值。', 'err');
    return;
  }
  _credVaultSetMsg(t('common.saving') || '保存中…', '');
  Api.credentials.upsert({ name: name, value: value, note: note }).then(function (res) {
    if (res && res.error) {
      _credVaultSetMsg(res.error.message || String(res.error), 'err');
      return;
    }
    nameEl.value = '';
    valueEl.value = '';
    if (noteEl) noteEl.value = '';
    _credVaultSetMsg('', '');
    _renderCredentialsVault();
  }).catch(function (e) {
    console.warn('[CredVault] upsert failed', e);
    var msg = (e && e.message) || (t('settings.credVaultSaveFail') || '保存失败');
    _credVaultSetMsg(msg, 'err');
  });
}

/** THE only plaintext egress — explicit user click. The value stays in a
 *  module-private map (not in the DOM until revealed) and auto-hides. */
function _credentialReveal(name) {
  Api.credentials.reveal(name).then(function (res) {
    if (res && res.error) {
      _credVaultSetMsg(res.error.message || String(res.error), 'err');
      return;
    }
    _credVaultRevealed[name] = (res && res.value) || '';
    if (_credVaultHideTimers[name]) clearTimeout(_credVaultHideTimers[name]);
    _credVaultHideTimers[name] = setTimeout(function () {
      _credentialHide(name);
    }, CRED_VAULT_REVEAL_MS);
    _renderCredentialsVault();
  }).catch(function (e) {
    console.warn('[CredVault] reveal failed', e);
    _credVaultSetMsg(t('settings.credVaultRevealFail') || '读取失败', 'err');
  });
}

function _credentialHide(name) {
  delete _credVaultRevealed[name];
  if (_credVaultHideTimers[name]) {
    clearTimeout(_credVaultHideTimers[name]);
    delete _credVaultHideTimers[name];
  }
  _renderCredentialsVault();
}

function _credentialCopy(name) {
  var value = _credVaultRevealed[name];
  if (value == null) return;
  // window.navigator, not bare `navigator`: in a node-driven jsdom harness a
  // bare reference falls through to NODE'S OWN global navigator (Node ≥21 has
  // one — with no clipboard), silently skipping the copy. window.navigator is
  // identical in a real browser and testable in a harness.
  var nav = /** @type {any} */ (window.navigator || {});
  if (nav.clipboard && nav.clipboard.writeText) {
    nav.clipboard.writeText(value).then(function () {
      _credVaultSetMsg(t('settings.credVaultCopied') || '已复制', '');
    }).catch(function () {
      _credVaultSetMsg(t('settings.credVaultCopyFail') || '复制失败', 'err');
    });
  } else {
    _credVaultSetMsg(t('settings.credVaultCopyFail') || '复制失败', 'err');
  }
}

function _credentialRemove(name) {
  var q = (t('settings.credVaultConfirmDelete') || '确定删除凭证「{name}」？').replace('{name}', name);
  if (!confirm(q)) return;
  Api.credentials.remove(name).then(function (res) {
    if (res && res.error) {
      _credVaultSetMsg(res.error.message || String(res.error), 'err');
      return;
    }
    _credentialHide(name);
    _renderCredentialsVault();
  }).catch(function (e) {
    console.warn('[CredVault] remove failed', e);
    _credVaultSetMsg(t('settings.credVaultSaveFail') || '保存失败', 'err');
  });
}

window._renderCredentialsVault = _renderCredentialsVault;
window._credentialAdd = _credentialAdd;
window._credentialReveal = _credentialReveal;
window._credentialHide = _credentialHide;
window._credentialCopy = _credentialCopy;
window._credentialRemove = _credentialRemove;
