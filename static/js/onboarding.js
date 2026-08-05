/* ═══════════════════════════════════════════════════════════════════
   onboarding.js — first-run setup wizard.

   Shown once when the server has ZERO configured API keys (fresh install),
   or unconditionally when bootstrap redirects here with ?setup=1. It asks
   the ONE question a new user can actually answer — "API key or
   subscription?" — and then drives the EXISTING surfaces:

     API path   → probe (Api.providers.probe) + persist via
                  Api.serverConfig.update({providers}) — the same partial
                  merge the Settings save uses, so hot-reload applies and no
                  settings session is needed.
     OAuth path → closes itself and hands off to Settings → 订阅登录, where
                  the battle-tested flow (popup / manual paste / curl helper
                  / egress line) takes over. Re-implementing any of that
                  inside the wizard would fork a surface that already works.

   ── Dismissal contract ──
   Skipping (or completing) the wizard sets a localStorage flag so a keyless
   server does not re-nag on every reload. ?setup=1 from bootstrap ignores
   the flag — that redirect is an explicit setup intent.

   ── e2e contract (DO NOT regress) ──
   The overlay element MUST carry the classes `modal-overlay open`:
   tests/conftest.py::_dismiss_onboarding_modals strips `.open` from exactly
   that selector to keep clicks from being intercepted on keyless test
   servers. A custom overlay class would resurrect the 12-failure class that
   helper exists to kill.

   This file is concatenated by lib/js_bundler.py — symbols share the same
   window scope as every other static/js/*.js file. No imports/exports.
   ═══════════════════════════════════════════════════════════════════ */

var _OB_DISMISS_KEY = 'tofu_onboarding_v1_done';

function _obDismissed() {
  try { return localStorage.getItem(_OB_DISMISS_KEY) === '1'; }
  catch (e) { return false; }
}

function _obMarkDismissed() {
  try { localStorage.setItem(_OB_DISMISS_KEY, '1'); }
  catch (e) { /* private mode — the wizard simply reappears next load */ }
}

/* Entry point called by _maybeAutoOpenSettings. Returns true when the
 * wizard took over (so the caller skips the legacy open-Settings fallback).
 * `opts.force` (?setup=1 from bootstrap) bypasses the dismissal flag. */
function maybeShowOnboarding(opts) {
  opts = opts || {};
  if (!opts.force && _obDismissed()) return false;
  if (document.getElementById('onboardingModal')) return true;
  _obRender('choose');
  return true;
}

function _obClose() {
  var m = document.getElementById('onboardingModal');
  if (m) m.remove();
  _obMarkDismissed();
}

function _obT(key) { return t(key); }

// ══════════════════════════════════════════════════════
//  Step renderer
// ══════════════════════════════════════════════════════

function _obRender(step, data) {
  data = data || {};
  var existing = document.getElementById('onboardingModal');
  if (existing) existing.remove();

  var steps = { choose: 1, api: 2, oauth: 2, apiDone: 3 };
  var totals = { choose: 3, api: 3, oauth: 3, apiDone: 3 };
  var body = '';
  var footer = '';

  if (step === 'choose') {
    body =
      '<p class="stg-modal-desc">' + escapeHtml(_obT('onboard.chooseTitle')) + '</p>' +
      '<div class="ob-cards">' +
        '<div class="ob-card" id="obCardApi" role="button" tabindex="0">' +
          '<div class="ob-card-icon">' + Icon('plug', 20) + '</div>' +
          '<div class="ob-card-title">' + escapeHtml(_obT('onboard.apiCardTitle')) + '</div>' +
          '<div class="ob-card-desc">' + escapeHtml(_obT('onboard.apiCardDesc')) + '</div>' +
        '</div>' +
        '<div class="ob-card" id="obCardOauth" role="button" tabindex="0">' +
          '<div class="ob-card-icon">' + Icon('star', 20) + '</div>' +
          '<div class="ob-card-title">' + escapeHtml(_obT('onboard.oauthCardTitle')) + '</div>' +
          '<div class="ob-card-desc">' + escapeHtml(_obT('onboard.oauthCardDesc')) + '</div>' +
        '</div>' +
      '</div>';
    footer =
      '<button class="stg-btn-secondary" id="obSkip">' + escapeHtml(_obT('onboard.skip')) + '</button>';
  } else if (step === 'api') {
    body =
      '<p class="stg-modal-desc">' + escapeHtml(_obT('onboard.apiTitle')) + '</p>' +
      '<div class="stg-field">' +
        '<label>' + escapeHtml(_obT('onboard.apiUrlLabel')) + ' <span class="stg-required">*</span></label>' +
        '<input type="text" id="obApiUrl" placeholder="https://api.deepseek.com" autocomplete="url">' +
        '<span class="stg-hint">' + escapeHtml(_obT('onboard.apiUrlHint')) + '</span>' +
      '</div>' +
      '<div class="stg-field">' +
        '<label>' + escapeHtml(_obT('onboard.apiKeyLabel')) + ' <span class="stg-required">*</span></label>' +
        '<input type="password" id="obApiKey" placeholder="sk-..." autocomplete="off">' +
      '</div>' +
      '<div id="obApiStatus" class="stg-auto-status" style="display:none"></div>';
    footer =
      '<button class="stg-btn-secondary" id="obBack">' + escapeHtml(_obT('onboard.back')) + '</button>' +
      '<button class="stg-btn-primary" id="obApiGo">' + Icon('search', 13) + ' ' + escapeHtml(_obT('onboard.apiProbe')) + '</button>';
  } else if (step === 'oauth') {
    body =
      '<p class="stg-modal-desc">' + escapeHtml(_obT('onboard.oauthTitle')) + '</p>' +
      '<div class="ob-cards">' +
        '<div class="ob-card" id="obCardClaude" role="button" tabindex="0">' +
          '<div class="ob-card-icon">' + Icon('brain', 20) + '</div>' +
          '<div class="ob-card-title">Claude Pro / Max</div>' +
          '<div class="ob-card-desc">' + escapeHtml(_obT('onboard.oauthClaudeDesc')) + '</div>' +
        '</div>' +
        '<div class="ob-card" id="obCardCodex" role="button" tabindex="0">' +
          '<div class="ob-card-icon">' + Icon('messageCircle', 20) + '</div>' +
          '<div class="ob-card-title">ChatGPT Plus / Pro</div>' +
          '<div class="ob-card-desc">' + escapeHtml(_obT('onboard.oauthCodexDesc')) + '</div>' +
        '</div>' +
      '</div>' +
      '<p class="ob-note">' + escapeHtml(_obT('onboard.oauthNote')) + '</p>';
    footer =
      '<button class="stg-btn-secondary" id="obBack">' + escapeHtml(_obT('onboard.back')) + '</button>';
  } else if (step === 'apiDone') {
    body =
      '<div class="ob-done">' +
        '<div class="ob-done-icon">' + Icon('check', 28) + '</div>' +
        '<div class="ob-done-title">' + escapeHtml(_obT('onboard.doneTitle')) + '</div>' +
        '<p class="stg-modal-desc">' +
          escapeHtml(t('onboard.doneApiDesc', { name: data.name || '', n: data.n || 0 })) +
        '</p>' +
      '</div>';
    footer =
      '<button class="stg-btn-primary" id="obStart">' + escapeHtml(_obT('onboard.doneStart')) + '</button>';
  }

  var stepLine = (steps[step] && step !== 'choose')
    ? '<span class="ob-step-line">' + escapeHtml(t('onboard.stepOf', { a: steps[step], b: totals[step] })) + '</span>'
    : '';

  var html =
    '<div id="onboardingModal" class="modal-overlay open">' +
      '<div class="stg-modal ob-modal">' +
        '<div class="stg-modal-header">' +
          '<span class="stg-modal-title">' + Icon('rocket', 15) + ' ' + escapeHtml(_obT('onboard.title')) + '</span>' +
          '<button class="stg-modal-close" id="obCloseX">✕</button>' +
        '</div>' +
        '<div class="stg-modal-body">' + stepLine + body + '</div>' +
        (footer ? '<div class="stg-modal-footer">' + footer + '</div>' : '') +
      '</div>' +
    '</div>';

  document.body.insertAdjacentHTML('beforeend', html);
  _obWire(step, data);
}

function _obWire(step, data) {
  var modal = document.getElementById('onboardingModal');
  if (!modal) return;
  // Overlay click = dismiss (same affordance as every other modal here).
  modal.addEventListener('click', function(ev) {
    if (ev.target === modal) _obClose();
  });
  var byId = function(id) { return document.getElementById(id); };
  if (byId('obCloseX')) byId('obCloseX').onclick = _obClose;
  if (byId('obSkip')) byId('obSkip').onclick = _obClose;
  if (byId('obBack')) byId('obBack').onclick = function() { _obRender('choose'); };
  if (byId('obCardApi')) byId('obCardApi').onclick = function() { _obRender('api'); };
  if (byId('obCardOauth')) byId('obCardOauth').onclick = function() { _obRender('oauth'); };
  if (byId('obCardClaude')) byId('obCardClaude').onclick = function() { _obStartOAuth('claude'); };
  if (byId('obCardCodex')) byId('obCardCodex').onclick = function() { _obStartOAuth('codex'); };
  if (byId('obApiGo')) byId('obApiGo').onclick = _obApiSubmit;
  if (byId('obStart')) byId('obStart').onclick = _obClose;
  if (step === 'api') {
    setTimeout(function() {
      var u = document.getElementById('obApiUrl');
      if (u) u.focus();
    }, 100);
  }
}

// ══════════════════════════════════════════════════════
//  API path — probe, then persist through the server-config merge
// ══════════════════════════════════════════════════════

function _obApiStatus(type, msg) {
  var div = document.getElementById('obApiStatus');
  if (!div) return;
  div.style.display = 'block';
  div.className = 'stg-auto-status stg-auto-' + type;
  div.textContent = msg;
}

async function _obApiSubmit() {
  var urlEl = document.getElementById('obApiUrl');
  var keyEl = document.getElementById('obApiKey');
  var goBtn = document.getElementById('obApiGo');
  var baseUrl = ((urlEl && urlEl.value) || '').trim();
  var apiKey = ((keyEl && keyEl.value) || '').trim();

  if (!baseUrl || !apiKey) {
    _obApiStatus('error', _obT('onboard.apiFillBoth'));
    return;
  }
  if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
    baseUrl = 'https://' + baseUrl;
    urlEl.value = baseUrl;
  }

  if (goBtn) { goBtn.disabled = true; goBtn.textContent = _obT('onboard.apiProbing'); }
  _obApiStatus('loading', _obT('onboard.apiProbing'));

  try {
    var data = await Api.providers.probe(baseUrl, apiKey, '');
    if (!data || !data.ok) {
      _obApiStatus('error', t('onboard.apiProbeFailed', { msg: (data && data.error) || 'network' }));
      return;
    }

    var models = data.models || [];
    var newProv = {
      id: (data.brand || 'prov') + '_' + Date.now().toString(36),
      name: data.name || 'Auto Provider',
      base_url: baseUrl,
      api_keys: [apiKey],
      enabled: true,
      models: models,
      brand: data.brand || 'generic',
      balance_url: data.balance_url || '',
    };
    if (data.thinking_format) newProv.thinking_format = data.thinking_format;

    /* Persist through the SAME partial-merge route the Settings save uses:
     * GET the live providers list, append, POST back. The server hot-reloads
     * (dispatcher reset included), so the model dropdown is usable at once —
     * no Settings session, no restart. */
    var cfg = await Api.serverConfig.get();
    var providers = (cfg && cfg.providers) || [];
    var r = await Api.serverConfig.update({ providers: providers.concat([newProv]) });
    var res = r ? await r.json().catch(function() { return {}; }) : {};
    if (!res.ok) {
      _obApiStatus('error', t('onboard.apiSaveFailed', { msg: res.error || 'unknown' }));
      return;
    }

    // Refresh the toolbar model dropdown so the new models are selectable
    // the moment the wizard closes.
    if (typeof _loadServerConfigAndPopulate === 'function') {
      _loadServerConfigAndPopulate();
    }
    _obRender('apiDone', { name: newProv.name, n: models.length });
  } catch (e) {
    _obApiStatus('error', t('onboard.apiProbeFailed', { msg: e.message }));
  } finally {
    if (goBtn) { goBtn.disabled = false; goBtn.textContent = _obT('onboard.apiProbe'); }
  }
}

// ══════════════════════════════════════════════════════
//  OAuth path — hand off to the existing Settings → 订阅登录 surface
// ══════════════════════════════════════════════════════

function _obStartOAuth(provider) {
  _obClose();
  if (typeof openSettings !== 'function') return;
  openSettings();
  if (typeof switchSettingsTab === 'function') switchSettingsTab('oauth');
  /* Auto-kick the login so the wizard's one click is the ONLY click before
   * the provider's own auth page. _oauthLogin drives the full existing flow
   * (popup + manual paste + curl helper), and the card's egress line keeps
   * explaining WHY a login cannot reach the provider when egress is down. */
  if (typeof _oauthLogin === 'function') _oauthLogin(provider);
}

if (typeof window !== 'undefined') {
  window.maybeShowOnboarding = maybeShowOnboarding;
  window._obClose = _obClose;
  window._obRender = _obRender;
  window._obApiSubmit = _obApiSubmit;
  window._obStartOAuth = _obStartOAuth;
}
