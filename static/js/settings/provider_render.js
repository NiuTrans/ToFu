/* ═══════════════════════════════════════════════════════════════════
   settings/provider render — extracted from settings.js (split 2026-05-28)

   Provider card rendering: _renderProvidersTab, _renderModelCard, balance-URL guessing.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

function _renderProvidersTab() {
  var list = document.getElementById('stgProviderList');
  if (!list) return;

  if (_stgProviders.length === 0) {
    list.innerHTML = '<p class="stg-empty">' + escapeHtml(t('settings.noProviders')) + '</p>';
    return;
  }

  // Capture currently-expanded provider IDs so re-render preserves the
  // user's current view (e.g. after editing the local-deployment card).
  var prevExpanded = {};
  var existing = list.querySelectorAll('.stg-provider-card');
  if (existing.length) {
    for (var ex = 0; ex < existing.length; ex++) {
      var idx = existing[ex].getAttribute('data-prov-idx');
      if (existing[ex].classList.contains('expanded')) {
        var pid = (_stgProviders[parseInt(idx, 10)] || {}).id || ('idx_' + idx);
        prevExpanded[pid] = true;
      }
    }
  } else {
    // First render of this Settings session — fall back to "first card open".
    var firstId = (_stgProviders[0] && _stgProviders[0].id) || 'idx_0';
    prevExpanded[firstId] = true;
  }

  var html = '';
  for (var pi = 0; pi < _stgProviders.length; pi++) {
    var p = _stgProviders[pi];
    var models = p.models || [];
    var keyCount = (p.api_keys || []).length;
    // Use explicit brand if stored (from template), else detect from hints
    var brand = p.brand || _detectBrand(p.name + ' ' + (p.base_url || ''));
    var isLocal = (brand === 'local');
    var endpointList = (p.endpoints && p.endpoints.length)
      ? p.endpoints
      : (p.base_url ? [p.base_url] : []);
    // Filter out blank rows for header/badge display only.
    var nonEmptyEndpoints = endpointList.filter(function(u) { return u && u.trim(); });
    var headerSubtitle = isLocal
      ? (nonEmptyEndpoints.length === 0 ? '—'
         : (nonEmptyEndpoints.length === 1 ? nonEmptyEndpoints[0]
            : (nonEmptyEndpoints[0] + '  +' + (nonEmptyEndpoints.length - 1) + ' ' + t('settings.moreEndpointsSuffix'))))
      : (p.base_url || '—');

    var _provKey = p.id || ('idx_' + pi);
    var _expCls = prevExpanded[_provKey] ? ' expanded' : '';
    html += '<div class="stg-provider-card' + _expCls + '" data-prov-idx="' + pi + '">';

    // ── Header ──
    html += '<div class="stg-provider-head" onclick="_toggleProviderExpand(this.parentElement)">' +
      '<div class="stg-provider-icon">' + _brandSvg(brand, 22) + '</div>' +
      '<div class="stg-provider-info">' +
        '<div class="stg-provider-name">' + escapeHtml(p.name || 'Unnamed') + '</div>' +
        '<div class="stg-provider-url">' + escapeHtml(headerSubtitle) + '</div>' +
      '</div>' +
      '<div class="stg-provider-badges">' +
        (isLocal ? _localEndpointBadge(nonEmptyEndpoints)
                 : '<span class="stg-badge">' + keyCount + ' ' + t('settings.keys') + '</span>') +
        '<span class="stg-badge">' + models.length + ' ' + t('settings.models') + '</span>' +
        (p.enabled === false ? '<span class="stg-badge off">' + t('settings.disabled') + '</span>' : '') +
      '</div>' +
      '<span class="stg-chevron">▾</span>' +
    '</div>';

    // ── Expanded body ──
    html += '<div class="stg-provider-body">';

    if (isLocal) {
      // ── Local provider: name + structured endpoint rows + optional shared key ──
      html += '<div class="stg-field"><label>' + escapeHtml(t('settings.displayName')) + '</label>' +
        '<input type="text" value="' + escapeHtml(p.name || '') + '" onchange="_onProvField(' + pi + ',\'name\',this.value)"></div>';

      html += _renderLocalEndpointsSection(pi, endpointList);
    } else {
      // Provider fields (cloud)
      html += '<div class="stg-field-grid">' +
        '<div class="stg-field"><label>' + escapeHtml(t('settings.displayName')) + '</label>' +
          '<input type="text" value="' + escapeHtml(p.name || '') + '" onchange="_onProvField(' + pi + ',\'name\',this.value)"></div>' +
        '<div class="stg-field"><label>' + escapeHtml(t('settings.baseUrl')) + '</label>' +
          '<input type="text" value="' + escapeHtml(p.base_url || '') + '" placeholder="https://api.openai.com/v1" onchange="_onProvField(' + pi + ',\'base_url\',this.value)"></div>' +
      '</div>';
    }

    html += _renderApiKeysSection(pi, p.api_keys || [], isLocal);

    if (!isLocal) {
      // ── Balance URL field + Check Balance button ──
      var balancePlaceholder = (p.base_url && _guessBalanceUrl(p.base_url))
        ? escapeHtml(_guessBalanceUrl(p.base_url))
        : 'https://api.example.com/v1/dashboard/billing/subscription';
      html += '<div class="stg-field"><label>' + escapeHtml(t('settings.balanceUrl')) +
        ' <span class="stg-hint">（' + escapeHtml(t('settings.balanceUrlHint')) + '）</span></label>' +
        '<div class="stg-balance-row">' +
          '<input type="text" value="' + escapeHtml(p.balance_url || '') + '" placeholder="' + balancePlaceholder + '" onchange="_onProvField(' + pi + ',\'balance_url\',this.value)">' +
          '<button class="stg-btn-balance" onclick="_checkProviderBalance(' + pi + ')" title="' + escapeHtml(t('settings.checkBalanceTitle')) + '">' + escapeHtml(t('settings.checkBalance')) + '</button>' +
        '</div>' +
        '<div class="stg-balance-result" id="stgBalanceResult_' + pi + '"></div>' +
      '</div>';

      // ── Models Discovery Path (optional, for non-standard /v1/models paths) ──
      var modelsPlaceholder = p.base_url ? escapeHtml(p.base_url.replace(/\/+$/, '') + '/models') : '/models';
      html += '<div class="stg-field"><label>' + escapeHtml(t('settings.modelsPath')) +
        ' <span class="stg-hint">（' + escapeHtml(t('settings.modelsPathHint')) + '）</span></label>' +
        '<input type="text" value="' + escapeHtml(p.models_path || '') + '" placeholder="' + modelsPlaceholder + '" onchange="_onProvField(' + pi + ',\'models_path\',this.value)"></div>';

      // ── Extra Headers (optional, for provider-specific gateway headers) ──
      html += _renderExtraHeadersSection(pi, p.extra_headers || {});
    } else {
      // Local providers — show probe status pane (filled by _discoverLocalModels).
      html += '<div id="stgLocalStatus_' + pi + '" class="stg-auto-status" style="display:none;font-family:ui-monospace,monospace;font-size:12px;"></div>';
    }

    // ── Thinking Format (per-provider thinking parameter style) ──
    var tfVal = p.thinking_format || '';
    html += '<div class="stg-field"><label>' + escapeHtml(t('settings.thinkingFormat')) +
      ' <span class="stg-hint">（' + escapeHtml(t('settings.thinkingFormatHint')) + '）</span></label>' +
      '<select onchange="_onProvField(' + pi + ',\'thinking_format\',this.value)">' +
        '<option value=""'  + (tfVal === '' ? ' selected' : '') + '>' + escapeHtml(t('settings.thinkingFormatAuto')) + '</option>' +
        '<option value="enable_thinking"' + (tfVal === 'enable_thinking' ? ' selected' : '') + '>' + escapeHtml(t('settings.thinkingFormatEnable')) + '</option>' +
        '<option value="thinking_type"' + (tfVal === 'thinking_type' ? ' selected' : '') + '>' + escapeHtml(t('settings.thinkingFormatType')) + '</option>' +
        '<option value="reasoning_effort"' + (tfVal === 'reasoning_effort' ? ' selected' : '') + '>' + escapeHtml(t('settings.thinkingFormatReasoningEffort')) + '</option>' +
        '<option value="none"' + (tfVal === 'none' ? ' selected' : '') + '>' + escapeHtml(t('settings.thinkingFormatNone')) + '</option>' +
      '</select></div>';

    html += '<div class="stg-field-row">' +
      '<div class="stg-toggle-row"><span>' + escapeHtml(t('settings.enabled')) + '</span>' +
        '<label class="stg-toggle"><input type="checkbox"' + (p.enabled !== false ? ' checked' : '') + ' onchange="_onProvField(' + pi + ',\'enabled\',this.checked)">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label>' +
      '</div>' +
      '<button class="stg-btn-danger" onclick="_deleteProvider(' + pi + ')">' + escapeHtml(t('settings.deleteProvider')) + '</button>' +
    '</div>';

    // ── Nested Model List ──
    var matrixOn = (typeof _stgMatrixOpen !== 'undefined') && _stgMatrixOpen[pi];
    var canMatrix = (typeof _renderAccessMatrix === 'function') && ((p.api_keys || []).length > 1 || isLocal);
    html += '<div class="stg-models-section">' +
      '<div class="stg-models-header">' +
        '<span class="stg-models-title">' + escapeHtml(t('settings.modelList')) + '</span>' +
        '<div class="stg-models-actions">' +
          (canMatrix ? '<button class="stg-btn-add stg-matrix-toggle' + (matrixOn ? ' active' : '') + '" onclick="_toggleMatrixView(' + pi + ')" title="' + escapeHtml(t('settings.matrixToggleHint')) + '">⊞ ' + escapeHtml(matrixOn ? t('settings.matrixViewCards') : t('settings.matrixViewMatrix')) + '</button>' : '') +
          (_findMatchingTemplate(p) ? '<button class="stg-btn-add" onclick="_syncFromTemplate(' + pi + ')" title="' + escapeHtml(t('settings.syncTemplateTitle')) + '">' + Icon('clipboard', 12) + ' ' + escapeHtml(t('settings.syncTemplate')) + '</button>' : '') +
          (isLocal
            ? '<button class="stg-btn-add" onclick="_discoverLocalModels(' + pi + ')" title="' + escapeHtml(t('settings.probeAllEndpointsTitle')) + '">' + escapeHtml(t('settings.probeAllEndpoints')) + '</button>'
            : '<button class="stg-btn-add" onclick="_discoverModels(' + pi + ')" title="' + escapeHtml(t('settings.autoDiscoverHint')) + '">' + escapeHtml(t('settings.autoDiscover')) + '</button>') +
          '<button class="stg-btn-add" onclick="_addModel(' + pi + ')">' + escapeHtml(t('settings.addModel')) + '</button>' +
        '</div>' +
      '</div>';

    if (matrixOn && canMatrix) {
      html += _renderAccessMatrix(pi);
    } else if (models.length === 0) {
      html += '<p class="stg-empty-sm">' + escapeHtml(t('settings.noModels')) + '</p>';
    } else {
      html += '<div class="stg-model-list">';
      for (var mi = 0; mi < models.length; mi++) {
        html += _renderModelCard(pi, mi, models[mi]);
      }
      html += '</div>';
    }
    html += '</div>'; // /stg-models-section

    html += '</div>'; // /stg-provider-body
    html += '</div>'; // /stg-provider-card
  }
  list.innerHTML = html;
}

/** Format a $/1M-tokens price for compact display */
function _fmtPrice(val) {
  if (val === 0 || val === '0') return t('settings.free');
  if (val == null) return '—';
  var n = parseFloat(val);
  if (isNaN(n)) return '—';
  // ≥1: show up to 2 decimals;  <1: show up to 3 significant digits
  var s;
  if (n >= 1) {
    s = n.toFixed(2).replace(/\.?0+$/, '');
  } else {
    s = n.toPrecision(3).replace(/\.?0+$/, '');
    // toPrecision can return scientific notation for very small numbers
    if (s.indexOf('e') >= 0) s = n.toFixed(4).replace(/\.?0+$/, '');
  }
  return '$' + s;
}

function _renderModelCard(provIdx, modelIdx, m) {
  var brand = _detectBrand(m.model_id);
  var caps = m.capabilities || [];
  var aliases = m.aliases || [];
  var isDisabled = (m.enabled === false);

  var html = '<div class="stg-mcard' + (isDisabled ? ' disabled' : '') +
    '" data-prov="' + provIdx + '" data-model="' + modelIdx + '">';

  // Brand icon
  html += '<div class="stg-mcard-icon">' + _brandSvg(brand, 18) + '</div>';

  // Body
  html += '<div class="stg-mcard-body">';

  // Model ID line
  html += '<div class="stg-mcard-main">' +
    '<span class="stg-mcard-id">' + escapeHtml(m.model_id || '(unnamed)') + '</span>';

  html += '</div>';

  // Capabilities + RPM. The "thinking" badge is rendered as a clickable
  // toggle so users can enable thinking depth on a self-hosted model whose
  // ID didn't match the auto-detection regex (e.g. Qwen 2.5 / custom builds)
  // without opening the edit form.
  html += '<div class="stg-mcard-caps">';
  for (var ci = 0; ci < caps.length; ci++) {
    if (caps[ci] === 'thinking') continue;
    html += '<span class="stg-cap ' + caps[ci] + '">' + escapeHtml(caps[ci]) + '</span>';
  }
  var hasThinking = caps.indexOf('thinking') >= 0;
  // Always render the thinking-toggle pill so it's discoverable even when
  // the model wasn't auto-tagged.
  if (!hasThinking) {
    html += '<button type="button" class="stg-cap stg-cap-toggle thinking off" ' +
      'onclick="event.stopPropagation();_toggleModelThinking(' + provIdx + ',' + modelIdx + ')" ' +
      'title="' + escapeHtml(t('settings.enableThinkingHint')) + '">+ thinking</button>';
  } else {
    html += '<button type="button" class="stg-cap stg-cap-toggle thinking on" ' +
      'onclick="event.stopPropagation();_toggleModelThinking(' + provIdx + ',' + modelIdx + ')" ' +
      'title="' + escapeHtml(t('settings.disableThinkingHint')) + '">thinking</button>';
  }
  if (m.rpm) html += '<span class="stg-mcard-stat">' + Icon('timer', 11) + ' ' + m.rpm + ' rpm</span>';
  html += '</div>';

  // Pricing row — look up real input/output from pricing cache
  var mp = (typeof _modelPricingCache !== 'undefined' && _modelPricingCache) ? _modelPricingCache[m.model_id] : null;
  if (mp && (mp.input != null || mp.output != null)) {
    var isFree = (mp.input === 0 && mp.output === 0);
    if (isFree) {
      html += '<div class="stg-mcard-pricing"><span class="stg-price-free">' + escapeHtml(t('settings.free')) + '</span></div>';
    } else {
      html += '<div class="stg-mcard-pricing">' +
        '<span class="stg-price-label">' + escapeHtml(t('settings.input')) + '</span>' +
        '<span class="stg-price-val in">' + _fmtPrice(mp.input) + '</span>' +
        '<span class="stg-price-sep">/</span>' +
        '<span class="stg-price-label">' + escapeHtml(t('settings.output')) + '</span>' +
        '<span class="stg-price-val out">' + _fmtPrice(mp.output) + '</span>' +
        '<span class="stg-price-unit">' + escapeHtml(t('settings.perMillionTokens')) + '</span>' +
      '</div>';
    }
  } else {
    html += '<div class="stg-mcard-pricing"><span class="stg-price-na">' + escapeHtml(t('settings.noPricing')) + '</span></div>';
  }

  // Aliases
  html += '<div class="stg-mcard-aliases">';
  if (aliases.length > 0) {
    html += '<span class="stg-aliases-label">' + escapeHtml(t('settings.aliases')) + '</span>';
    for (var ai = 0; ai < aliases.length; ai++) {
      html += '<span class="stg-alias-chip">' +
        escapeHtml(aliases[ai]) +
        '<span class="stg-alias-x" onclick="event.stopPropagation();_removeAlias(' + provIdx + ',' + modelIdx + ',' + ai + ')">×</span>' +
      '</span>';
    }
  }
  html += '<button class="stg-alias-add" onclick="event.stopPropagation();_addAlias(' + provIdx + ',' + modelIdx + ')">' + escapeHtml(t('settings.addAlias')) + '</button>';
  html += '</div>';

  html += '</div>'; // /stg-mcard-body

  // Actions
  var enabledTitle = isDisabled ? t('settings.modelDisabledTitle') : t('settings.modelEnabledTitle');
  html += '<div class="stg-mcard-actions">' +
    '<label class="stg-toggle stg-mcard-toggle" title="' + escapeHtml(enabledTitle) + '" onclick="event.stopPropagation();">' +
      '<input type="checkbox"' + (isDisabled ? '' : ' checked') +
        ' onchange="_toggleModelEnabled(' + provIdx + ',' + modelIdx + ')">' +
      '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
    '</label>' +
    '<button class="stg-btn-icon" onclick="_editModel(' + provIdx + ',' + modelIdx + ')" title="' + escapeHtml(t('settings.editTitle')) + '">✎</button>' +
    '<button class="stg-btn-icon danger" onclick="_deleteModel(' + provIdx + ',' + modelIdx + ')" title="' + escapeHtml(t('settings.deleteTitle')) + '">✕</button>' +
  '</div>';

  html += '</div>'; // /stg-mcard
  return html;
}

// ── Provider CRUD ──

// ── URL Guess Helpers — generate best-guess balance/models URLs from base_url ──

/**
 * Known provider-specific balance URL patterns.
 * Key: substring to match in base_url (lowercase).
 * Value: function(baseUrl) → full balance URL.
 */
var _BALANCE_URL_RULES = [
  // DeepSeek uses a non-standard /user/balance endpoint
  { match: 'deepseek.com',   fn: function(b) { return _urlOrigin(b) + '/user/balance'; } },
  // OpenRouter uses /api/v1/credits
  { match: 'openrouter.ai',  fn: function(b) { return _urlOrigin(b) + '/api/v1/credits'; } },
  // Google Gemini has no billing API
  { match: 'googleapis.com', fn: function() { return ''; } },
];

/** Extract origin (scheme + host) from a URL string. */
function _urlOrigin(url) {
  try {
    var u = new URL(url);
    return u.origin;
  } catch (e) {
    return url.replace(/\/+$/, '');
  }
}

/**
 * Guess the balance/billing URL from a base_url.
 * Uses known provider rules first, then falls back to
 * base_url + '/dashboard/billing/subscription'.
 */
function _guessBalanceUrl(baseUrl) {
  if (!baseUrl) return '';
  var lower = baseUrl.toLowerCase();
  for (var i = 0; i < _BALANCE_URL_RULES.length; i++) {
    if (lower.indexOf(_BALANCE_URL_RULES[i].match) >= 0) {
      return _BALANCE_URL_RULES[i].fn(baseUrl);
    }
  }
  // Default: append /dashboard/billing/subscription to base_url
  return baseUrl.replace(/\/+$/, '') + '/dashboard/billing/subscription';
}

/**
 * Guess the models discovery path from a base_url.
 * Most OpenAI-compatible providers use /models (appended to base_url).
 * Returns empty string to use the default behavior.
 */
function _guessModelsPath(baseUrl) {
  // Default /models works for almost all providers — return empty
  // to let the backend use its default logic.
  return '';
}

function _toggleProviderExpand(card) {
  card.classList.toggle('expanded');
}

function _onProvField(provIdx, field, value) {
  if (!_stgProviders[provIdx]) return;
  _stgProviders[provIdx][field] = value;

  // When base_url changes, auto-fill balance_url and models_path if empty
  if (field === 'base_url' && value) {
    var p = _stgProviders[provIdx];
    if (!p.balance_url) {
      p.balance_url = _guessBalanceUrl(value);
    }
    if (!p.models_path) {
      p.models_path = _guessModelsPath(value);
    }
  }

  // Re-render header to reflect name/badge changes
  _renderProvidersTab();
}

function _onProvExtraHeaders(provIdx, value) {
  if (!_stgProviders[provIdx]) return;
  var trimmed = value.trim();
  if (!trimmed) {
    delete _stgProviders[provIdx].extra_headers;
    return;
  }
  try {
    var parsed = JSON.parse(trimmed);
    if (typeof parsed === 'object' && !Array.isArray(parsed)) {
      _stgProviders[provIdx].extra_headers = parsed;
    } else {
      debugLog('[Settings] Custom Headers must be a JSON object', 'error');
    }
  } catch (e) {
    debugLog('[Settings] Invalid JSON for Custom Headers: ' + e.message, 'error');
  }
}

function _onProvKeys(provIdx, value) {
  if (!_stgProviders[provIdx]) return;
  _stgProviders[provIdx].api_keys = value.split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
  // Re-render so that the per-key stats rows reflect the new key count/order.
  _renderProviderKeyStats(provIdx);
}

// ── API Keys — structured one-per-row editor ───────────────────────────
//
// Replaces the old <textarea> with a list of single-line rows (one per
// key). Mirrors the Custom Headers / Local Endpoints UX so a provider
// card stays a list of structured fields rather than a mix of free-text
// blobs.
//
// Persistence: rows are committed to provider.api_keys via
// _collectApiKeysFromDom() on every input change. _onProvKeys(value) is
// preserved as a programmatic entry (e.g. for legacy code paths that
// still want to set the whole list at once).

/** Render the API Keys section: one merged 2-row card per key.
 *  Row 1 = editor (input + show/hide + delete). Row 2 = today's runtime
 *  state (success rate, counts, override toggle, reset). The two used to
 *  be rendered as separate sibling blocks (`stg-keys-list` +
 *  `stg-keystats`); they are now the same card so each key has a single
 *  visual identity with one health-state colour bar on the left. */
function _renderApiKeysSection(provIdx, keys, isLocal) {
  var clean = (keys || []).filter(function(k) { return k; });
  var helpTxt = (typeof _keyStatsHelpText === 'function')
    ? _keyStatsHelpText(isLocal) : (isLocal ? t('settings.apiKeysHintLocal') : t('settings.apiKeysHint'));

  var html = '<div class="stg-field stg-keys-field" data-prov-idx="' + provIdx + '">' +
    '<div class="stg-keys-header">' +
      '<label style="margin:0;">' + escapeHtml(t('settings.apiKeys')) +
        ' <span class="stg-keys-info" tabindex="0" role="tooltip" aria-label="' + escapeHtml(helpTxt) + '" title="' + escapeHtml(helpTxt) + '">i</span>' +
      '</label>' +
      '<button type="button" class="stg-btn-add stg-keys-tb" ' +
        'onclick="_addApiKey(' + provIdx + ')" ' +
        'title="' + escapeHtml(t('settings.addApiKeyTitle')) + '">' +
        escapeHtml(t('settings.addApiKey')) + '</button>' +
    '</div>';

  if (clean.length === 0) {
    html += '<div class="stg-keys-empty">' +
      escapeHtml(isLocal ? t('settings.noApiKeysLocal') : t('settings.noApiKeys')) +
    '</div>';
  } else {
    html += '<div class="stg-keys-list">';
    for (var i = 0; i < clean.length; i++) {
      html += _renderApiKeyCard(provIdx, i, clean[i]);
    }
    html += '</div>';
  }
  html += '</div>';
  return html;
}

/** Mask an API key for display, leaking only the last 4 characters.
 *  Keys of length <= 4 are shown verbatim (nothing meaningful to hide). */
function _maskApiKey(v) {
  v = v || '';
  if (v.length <= 4) return v;
  var dots = Math.min(v.length - 4, 32);
  return new Array(dots + 1).join('•') + v.slice(-4);
}

/** Render one merged API-key card (editor row + runtime stats row).
 *
 *  The card carries a state class (stg-keystat-good / ok / warn / disabled
 *  / idle / exhausted) on the wrapper itself so the left colour bar
 *  reflects health. The blank-value case (just-added empty card) hides
 *  the stats sub-row via the .stg-key-card--blank modifier. */
function _renderApiKeyCard(provIdx, idx, value) {
  var statRow = (typeof _getKeyStatRowFor === 'function')
    ? _getKeyStatRowFor(provIdx, idx) : null;
  var stateCls = (typeof _keyStatsClass === 'function') ? _keyStatsClass(statRow) : '';
  var blankCls = (value || '').trim() ? '' : ' stg-key-card--blank';
  var statsHTML = (typeof _renderKeyCardStatsHTML === 'function')
    ? _renderKeyCardStatsHTML(provIdx, idx) : '';

  // Non-blank keys render masked (••••…last4) and readonly; the eye button
  // reveals the full plaintext. Blank (just-added) keys start editable so the
  // user can type immediately. The real value lives in data-key-real while
  // masked — _collectApiKeysFromDom reads it from there.
  var hasVal = !!(value || '').trim();
  var inputAttrs = hasVal
    ? 'type="text" readonly data-masked="1" data-key-real="' + escapeHtml(value || '') + '" ' +
      'value="' + escapeHtml(_maskApiKey(value || '')) + '" '
    : 'type="text" data-masked="0" data-key-real="" value="" ';

  return '<div class="stg-key-card ' + stateCls + blankCls + '" data-key-idx="' + idx + '">' +
    '<div class="stg-key-card-edit">' +
      '<span class="stg-keys-idx">#' + (idx + 1) + '</span>' +
      '<input class="stg-keys-input" data-key-field="value" ' +
        'spellcheck="false" autocomplete="off" autocapitalize="off" autocorrect="off" ' +
        'placeholder="sk-…" ' + inputAttrs +
        'oninput="_onApiKeyRowEdit(' + provIdx + ')">' +
      '<button type="button" class="stg-keys-btn" ' +
        'onclick="_toggleApiKeyVisibility(this)" ' +
        'title="' + escapeHtml(t('settings.showHideKeyTitle')) + '" ' +
        'aria-label="' + escapeHtml(t('settings.showHideKeyTitle')) + '">' + Icon('eye', 13) + '</button>' +
      '<button type="button" class="stg-keys-btn danger" ' +
        'onclick="_deleteApiKey(' + provIdx + ',' + idx + ')" ' +
        'title="' + escapeHtml(t('settings.deleteApiKeyTitle')) + '">✕</button>' +
    '</div>' +
    '<div class="stg-key-card-stats">' + statsHTML + '</div>' +
  '</div>';
}

/** Re-collect the keys from this provider's cards into provider.api_keys. */
function _collectApiKeysFromDom(provIdx) {
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return null;
  var field = card.querySelector('.stg-keys-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return null;
  var cards = field.querySelectorAll('.stg-key-card');
  var out = [];
  for (var i = 0; i < cards.length; i++) {
    var inp = cards[i].querySelector('input[data-key-field="value"]');
    var raw = inp && inp.getAttribute('data-masked') === '1'
      ? (inp.getAttribute('data-key-real') || '')
      : (inp && inp.value || '');
    var v = raw.trim();
    if (v) out.push(v);
  }
  return out;
}

/** Live-edit handler bound to each card's input.
 *  Updates provider.api_keys, toggles the per-card --blank modifier so
 *  the stats sub-row appears as soon as the input has any text, and
 *  refreshes any visible stats (e.g. card index labels post-delete). */
function _onApiKeyRowEdit(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  // Keep data-key-real in sync for any revealed (editable) input so the eye
  // toggle and collect logic always see the latest typed value.
  var editCard = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (editCard) {
    var editInputs = editCard.querySelectorAll('.stg-keys-field[data-prov-idx="' + provIdx + '"] input[data-key-field="value"]');
    for (var ei = 0; ei < editInputs.length; ei++) {
      if (editInputs[ei].getAttribute('data-masked') !== '1') {
        editInputs[ei].setAttribute('data-key-real', editInputs[ei].value);
      }
    }
  }

  var collected = _collectApiKeysFromDom(provIdx);
  if (collected === null) return;
  p.api_keys = collected;

  // Toggle .stg-key-card--blank per card based on its current input value.
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (card) {
    var cards = card.querySelectorAll('.stg-keys-field[data-prov-idx="' + provIdx + '"] .stg-key-card');
    for (var ci = 0; ci < cards.length; ci++) {
      var inp = cards[ci].querySelector('input[data-key-field="value"]');
      var hasVal = !!(inp && inp.value && inp.value.trim());
      cards[ci].classList.toggle('stg-key-card--blank', !hasVal);
    }
  }

  _renderProviderKeyStats(provIdx);
}

/** Append a new blank API-key row.
 *
 * Inserts directly into the DOM so a freshly-added blank row can coexist
 * with the existing data model — the row is committed to api_keys on first
 * keystroke via _onApiKeyRowEdit. */
function _addApiKey(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var field = card.querySelector('.stg-keys-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return;

  var list = field.querySelector('.stg-keys-list');
  if (!list) {
    var emptyEl = field.querySelector('.stg-keys-empty');
    if (emptyEl) emptyEl.remove();
    list = document.createElement('div');
    list.className = 'stg-keys-list';
    field.appendChild(list);
  }

  var nextIdx = list.querySelectorAll('.stg-key-card').length;
  list.insertAdjacentHTML('beforeend', _renderApiKeyCard(provIdx, nextIdx, ''));

  var cards = list.querySelectorAll('.stg-key-card');
  var newCard = cards[cards.length - 1];
  var newInput = newCard && newCard.querySelector('input[data-key-field="value"]');
  if (newInput) newInput.focus();
}

/** Remove a single API-key card in-place + recommit api_keys. */
function _deleteApiKey(provIdx, idx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var field = card.querySelector('.stg-keys-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return;
  var row = field.querySelector('.stg-key-card[data-key-idx="' + idx + '"]');
  if (!row || !row.parentNode) return;
  row.parentNode.removeChild(row);

  // Re-number remaining cards so #N stays sequential and the delete button
  // dispatches with the correct (post-shift) index.
  var remaining = field.querySelectorAll('.stg-key-card');
  for (var i = 0; i < remaining.length; i++) {
    remaining[i].setAttribute('data-key-idx', i);
    var idxEl = remaining[i].querySelector('.stg-keys-idx');
    if (idxEl) idxEl.textContent = '#' + (i + 1);
    var del = remaining[i].querySelector('.stg-keys-btn.danger');
    if (del) del.setAttribute('onclick',
      '_deleteApiKey(' + provIdx + ',' + i + ')');
  }

  p.api_keys = _collectApiKeysFromDom(provIdx) || [];

  var list = field.querySelector('.stg-keys-list');
  if (list && list.querySelectorAll('.stg-key-card').length === 0) {
    list.remove();
    var hint = document.createElement('div');
    hint.className = 'stg-keys-empty';
    var brand = (_stgProviders[provIdx] || {}).brand;
    hint.textContent = (brand === 'local') ? t('settings.noApiKeysLocal') : t('settings.noApiKeys');
    field.appendChild(hint);
  }

  _renderProviderKeyStats(provIdx);
}

/** Toggle one card's input between password and text (eye button). */
function _toggleApiKeyVisibility(btn) {
  var holder = btn && (btn.closest('.stg-key-card') || btn.closest('.stg-key-card-edit'));
  if (!holder) return;
  var inp = holder.querySelector('input[data-key-field="value"]');
  if (!inp) return;
  if (inp.getAttribute('data-masked') === '1') {
    // Reveal: show full plaintext and allow editing.
    inp.value = inp.getAttribute('data-key-real') || '';
    inp.removeAttribute('readonly');
    inp.setAttribute('data-masked', '0');
  } else {
    // Hide: commit the current value, then show masked (last-4) and lock.
    var real = inp.value;
    inp.setAttribute('data-key-real', real);
    inp.value = _maskApiKey(real);
    inp.setAttribute('readonly', 'readonly');
    inp.setAttribute('data-masked', '1');
  }
}

// ── Custom Headers — structured key/value rows ──────────────────────────

/** Render the Custom Headers section as a list of name/value rows. */
function _renderExtraHeadersSection(provIdx, headersObj) {
  var entries = [];
  if (headersObj && typeof headersObj === 'object' && !Array.isArray(headersObj)) {
    var keys = Object.keys(headersObj);
    for (var i = 0; i < keys.length; i++) {
      entries.push([keys[i], headersObj[keys[i]] == null ? '' : String(headersObj[keys[i]])]);
    }
  }

  var html = '<div class="stg-field stg-hdr-field" data-prov-idx="' + provIdx + '">' +
    '<div class="stg-hdr-header">' +
      '<label style="margin:0;">' + escapeHtml(t('settings.customHeaders')) +
        ' <span class="stg-hint">（' + escapeHtml(t('settings.customHeadersHint')) + '）</span></label>' +
      '<button type="button" class="stg-btn-add stg-hdr-tb" ' +
        'onclick="_addExtraHeader(' + provIdx + ')" ' +
        'title="' + escapeHtml(t('settings.addHeaderTitle')) + '">' +
        escapeHtml(t('settings.addHeader')) + '</button>' +
    '</div>';

  if (entries.length === 0) {
    html += '<div class="stg-hdr-empty">' + escapeHtml(t('settings.noHeaders')) + '</div>';
  } else {
    html += '<div class="stg-hdr-list">';
    for (var ei = 0; ei < entries.length; ei++) {
      html += _renderExtraHeaderRow(provIdx, ei, entries[ei][0], entries[ei][1]);
    }
    html += '</div>';
  }
  html += '</div>';
  return html;
}

/** Render a single Custom Header row (name + value + delete). */
function _renderExtraHeaderRow(provIdx, idx, name, value) {
  return '<div class="stg-hdr-row" data-hdr-idx="' + idx + '">' +
    '<input type="text" class="stg-hdr-name" data-hdr-field="name" ' +
      'placeholder="' + escapeHtml(t('settings.headerNamePlaceholder')) + '" ' +
      'spellcheck="false" autocomplete="off" ' +
      'value="' + escapeHtml(name || '') + '" ' +
      'onchange="_onExtraHeaderRowEdit(' + provIdx + ')">' +
    '<span class="stg-hdr-sep">:</span>' +
    '<input type="text" class="stg-hdr-value" data-hdr-field="value" ' +
      'placeholder="' + escapeHtml(t('settings.headerValuePlaceholder')) + '" ' +
      'spellcheck="false" autocomplete="off" ' +
      'value="' + escapeHtml(value || '') + '" ' +
      'onchange="_onExtraHeaderRowEdit(' + provIdx + ')">' +
    '<button type="button" class="stg-hdr-btn danger" ' +
      'onclick="_deleteExtraHeader(' + provIdx + ',' + idx + ')" ' +
      'title="' + escapeHtml(t('settings.deleteHeaderTitle')) + '">✕</button>' +
  '</div>';
}

/** Re-collect the rows of one provider into provider.extra_headers. */
function _collectExtraHeadersFromDom(provIdx) {
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return null;
  var field = card.querySelector('.stg-hdr-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return {};
  var rows = field.querySelectorAll('.stg-hdr-row');
  var out = {};
  for (var i = 0; i < rows.length; i++) {
    var nameEl = rows[i].querySelector('input[data-hdr-field="name"]');
    var valEl  = rows[i].querySelector('input[data-hdr-field="value"]');
    var n = (nameEl && nameEl.value || '').trim();
    var v = (valEl && valEl.value || '');
    if (n) out[n] = v;
  }
  return out;
}

/** Live-edit handler bound to each row's name/value input. */
function _onExtraHeaderRowEdit(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var collected = _collectExtraHeadersFromDom(provIdx);
  if (collected === null) return;
  if (Object.keys(collected).length === 0) {
    delete p.extra_headers;
  } else {
    p.extra_headers = collected;
  }
}

/** Append a new blank header row.
 *
 * Inserts the row directly into the DOM instead of round-tripping through
 * the {name: value} data model — that way multiple blank rows can coexist
 * (an object can't hold two '' keys) and we never need synthetic
 * placeholder names like "X-Header-1" leaking into the input. The row
 * is committed to provider.extra_headers as soon as the user types a name. */
function _addExtraHeader(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var field = card.querySelector('.stg-hdr-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return;

  var list = field.querySelector('.stg-hdr-list');
  if (!list) {
    // Was empty — replace the placeholder hint with a fresh list container.
    var emptyEl = field.querySelector('.stg-hdr-empty');
    if (emptyEl) emptyEl.remove();
    list = document.createElement('div');
    list.className = 'stg-hdr-list';
    field.appendChild(list);
  }

  var nextIdx = list.querySelectorAll('.stg-hdr-row').length;
  list.insertAdjacentHTML('beforeend', _renderExtraHeaderRow(provIdx, nextIdx, '', ''));

  var rows = list.querySelectorAll('.stg-hdr-row');
  var newRow = rows[rows.length - 1];
  var nameInput = newRow && newRow.querySelector('input[data-hdr-field="name"]');
  if (nameInput) nameInput.focus();
}

/** Remove a single header row.
 *
 * Drops the row directly from the DOM rather than re-rendering, for two
 * reasons:
 *   1. Rows with an empty Header-Name are stripped by
 *      _collectExtraHeadersFromDom, so re-rendering from the (filtered)
 *      data model would erase any other blank rows the user is still
 *      editing — surprising behaviour when only one row was deleted.
 *   2. It lets the user delete a freshly-added blank row that has not
 *      yet made it into the data model. */
function _deleteExtraHeader(provIdx, idx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var field = card.querySelector('.stg-hdr-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return;
  var row = field.querySelector('.stg-hdr-row[data-hdr-idx="' + idx + '"]');
  if (!row || !row.parentNode) return;
  row.parentNode.removeChild(row);

  var collected = _collectExtraHeadersFromDom(provIdx) || {};
  if (Object.keys(collected).length === 0) {
    delete p.extra_headers;
  } else {
    p.extra_headers = collected;
  }

  // If no rows remain, swap the list back to the empty-state hint.
  var list = field.querySelector('.stg-hdr-list');
  if (list && list.querySelectorAll('.stg-hdr-row').length === 0) {
    list.remove();
    var hint = document.createElement('div');
    hint.className = 'stg-hdr-empty';
    hint.textContent = t('settings.noHeaders');
    field.appendChild(hint);
  }
}

