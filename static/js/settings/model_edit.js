/* ═══════════════════════════════════════════════════════════════════
   settings/model edit — extracted from settings.js (split 2026-05-28)

   Per-model edit/delete + alias management (nested inside provider card).

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ── Model CRUD (nested inside provider) ──

function _addModel(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  if (!p.models) p.models = [];
  p.models.push({
    model_id: '', aliases: [], capabilities: ['text'], rpm: 30, cost: 0.01, thinking_default: false
  });
  _renderProvidersTab();
  // Open edit for the new model
  _editModel(provIdx, p.models.length - 1);
  // Make sure provider is expanded
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (card) card.classList.add('expanded');
}

async function _deleteModel(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models || !p.models[modelIdx]) return;
  var mid = p.models[modelIdx].model_id;
  if (!await showConfirm(t('settings.meDeleteConfirm', { name: (mid || t('settings.meUnnamed')) }), { danger: true })) return;
  p.models.splice(modelIdx, 1);
  // Clear presets pointing to this model
  for (var k in _stgPresets) {
    if (_stgPresets[k] === mid) _stgPresets[k] = '';
  }
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
}

/** Toggle the 'thinking' capability + thinking_default on a model card.
 *
 * Auto-discovery's regex only flags Qwen 3+, GLM 4.5+, DeepSeek V4+, Claude/o1/o3,
 * etc. as thinking models. Self-hosted custom builds (Qwen 2.5 finetunes, GLM 4.5
 * forks renamed, in-house reasoning models) won't match — this toggle lets the
 * user opt in without entering the edit form.
 */
function _toggleModelThinking(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;
  var caps = (m.capabilities || []).slice();
  var idx = caps.indexOf('thinking');
  if (idx >= 0) {
    caps.splice(idx, 1);
    m.thinking_default = false;
  } else {
    caps.push('thinking');
    m.thinking_default = true;
  }
  m.capabilities = caps;
  _renderProvidersTab();
}

/** Toggle the per-model enabled flag.
 *
 * A disabled model is excluded from slot-pool construction
 * (lib/llm_dispatch/dispatcher.py) AND from dropdown_models
 * (routes/config.py), so it disappears from the chat picker and
 * the dispatcher will never route to it. The model entry itself
 * stays in server_config.json so the user can re-enable it later
 * without losing aliases / pricing / RPM settings.
 */
function _toggleModelEnabled(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;
  m.enabled = (m.enabled === false);
  _renderProvidersTab();
}

function _editModel(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;

  // Find the model card and insert edit form after it
  var card = document.querySelector('.stg-mcard[data-prov="' + provIdx + '"][data-model="' + modelIdx + '"]');
  if (!card) return;
  // Remove any existing edit forms
  var existing = card.parentElement.querySelector('.stg-edit-form');
  if (existing) existing.remove();

  var allCaps = ['text', 'vision', 'video', 'thinking', 'cheap', 'image_gen', 'embedding', 'transcription', 'audio_chat'];
  // Price fields prefill ONLY from the explicit per-model override
  // (m.pricing). The placeholder shows the currently-effective price (from
  // discovery enrichment or the global table) so the user sees what the
  // model bills at without turning an implicit price into a pinned override
  // just by opening and applying the dialog.
  var _eff = _effectiveModelPrices(m);
  var _ovrIn = (m.pricing && m.pricing.input != null) ? m.pricing.input : '';
  var _ovrOut = (m.pricing && m.pricing.output != null) ? m.pricing.output : '';
  var _hasOvr = (_ovrIn !== '' && _ovrOut !== '');
  var html = '<div class="stg-edit-form">';
  html += '<div class="stg-edit-grid">' +
    '<div class="stg-field"><label>' + escapeHtml(t('settings.meModelId')) + '</label>' +
      '<input type="text" class="stg-edit-mid" value="' + escapeHtml(m.model_id || '') + '" placeholder="' + escapeHtml(t('settings.meModelIdPlaceholder')) + '"></div>' +
    '<div class="stg-field"><label>' + escapeHtml(t('settings.meRpm')) + '</label>' +
      '<input type="number" class="stg-edit-rpm" value="' + (m.rpm || 30) + '" min="1"></div>' +
    '<div class="stg-field"><label>' + escapeHtml(t('settings.meCost')) + ' <span class="stg-hint">' + escapeHtml(t('settings.meCostHint')) + '</span>' +
      '<span class="stg-hint stg-edit-cost-derived" style="display:' + (_hasOvr ? '' : 'none') + '">' + escapeHtml(t('settings.meCostDerived')) + '</span></label>' +
      '<input type="number" class="stg-edit-cost" value="' + (m.cost || 0.01) + '" step="0.001" min="0"' + (_hasOvr ? ' readonly' : '') + '></div>' +
    '<div class="stg-field"><label>' + escapeHtml(t('settings.meInputPrice')) + ' <span class="stg-hint">' + escapeHtml(t('settings.mePriceHint')) + '</span></label>' +
      '<input type="number" class="stg-edit-pin" value="' + _ovrIn + '" step="0.01" min="0" placeholder="' + (_eff.input != null ? _eff.input : '—') + '" oninput="_onModelPriceInput(this)"></div>' +
    '<div class="stg-field"><label>' + escapeHtml(t('settings.meOutputPrice')) + ' <span class="stg-hint">' + escapeHtml(t('settings.mePriceHint')) + '</span></label>' +
      '<input type="number" class="stg-edit-pout" value="' + _ovrOut + '" step="0.01" min="0" placeholder="' + (_eff.output != null ? _eff.output : '—') + '" oninput="_onModelPriceInput(this)"></div>' +
  '</div>';

  html += '<div class="stg-field"><label>' + escapeHtml(t('settings.meCapabilities')) + '</label><div class="stg-cap-toggles">';
  for (var ci = 0; ci < allCaps.length; ci++) {
    var cap = allCaps[ci];
    var active = (m.capabilities || []).indexOf(cap) >= 0;
    html += '<button type="button" class="stg-cap-btn' + (active ? ' active' : '') + '" data-cap="' + cap + '" onclick="this.classList.toggle(\'active\')">' + cap + '</button>';
  }
  html += '</div></div>';

  var _field = _poolField(m);
  var _isPool = (_field === 'request_ids');
  html += '<div class="stg-field"><label>' +
    escapeHtml(t(_isPool ? 'settings.meRequestIds' : 'settings.meAliases')) +
    ' <span class="stg-hint">' +
    escapeHtml(t(_isPool ? 'settings.meRequestIdsHint' : 'settings.meAliasesHint')) +
    '</span></label>' +
    '<input type="text" class="stg-edit-aliases" data-pool-field="' + _field + '"' +
    ' value="' + escapeHtml((m[_field] || []).join(', ')) + '"' +
    ' placeholder="' + escapeHtml(t(_isPool
      ? 'settings.meRequestIdsPlaceholder' : 'settings.meAliasesPlaceholder')) + '"></div>';

  html += '<div class="stg-toggle-row"><span>' + escapeHtml(t('settings.meThinkingDefault')) + '</span>' +
    '<label class="stg-toggle"><input type="checkbox" class="stg-edit-think"' + (m.thinking_default ? ' checked' : '') + '>' +
    '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label></div>';

  /* ── Wire-face pin ──
   * '' (auto) lets the backend family rule decide — that is what keeps a
   * future opus-6 correct the day it is added, so it stays the default.
   * A pin is the escape hatch AND the only way to force a Claude model
   * onto a non-Anthropic wire, which drops thinking-block signatures; the
   * warning under the select says so rather than leaving it to the log.
   * Options come from _faceNamesFor (backend-derived), never hand-listed. */
  if (typeof _faceNamesFor === 'function') {
    var _names = _faceNamesFor(provIdx);
    if (_names.length > 1) {
      var _cur = (m.face || '');
      html += '<div class="stg-field"><label>' + escapeHtml(t('settings.meFace')) +
        ' <span class="stg-hint">' + escapeHtml(t('settings.meFaceHint')) + '</span></label>' +
        '<select class="stg-edit-face" onchange="_onFacePinChange(this)">' +
        '<option value=""' + (_cur === '' ? ' selected' : '') + '>' +
          escapeHtml(t('settings.meFaceAuto')) + '</option>';
      for (var fi = 0; fi < _names.length; fi++) {
        html += '<option value="' + escapeHtml(_names[fi]) + '"' +
          (_cur === _names[fi] ? ' selected' : '') + '>' + escapeHtml(_names[fi]) + '</option>';
      }
      html += '</select>' +
        '<div class="stg-face-warn"' + (_cur ? '' : ' style="display:none"') + '>' +
        escapeHtml(t('settings.meFacePinWarn')) + '</div></div>';
    }
  }

  html += '<div class="stg-edit-actions">' +
    '<button class="stg-btn-secondary" onclick="this.closest(\'.stg-edit-form\').remove()">' + escapeHtml(t('settings.cancel')) + '</button>' +
    '<button class="stg-btn-primary" onclick="_saveModelEdit(' + provIdx + ',' + modelIdx + ')">' + escapeHtml(t('settings.apply')) + '</button>' +
  '</div>';
  html += '</div>';

  card.insertAdjacentHTML('afterend', html);
  // Focus model ID input
  var midInput = card.nextElementSibling.querySelector('.stg-edit-mid');
  if (midInput && !midInput.value) { midInput.focus(); }
}

/** Currently-effective per-1M prices for a model, for the price fields'
 *  placeholder text. Mirrors the card's resolution order (explicit override
 *  → discovery enrichment → global MODEL_PRICING cache). */
function _effectiveModelPrices(m) {
  if (m.pricing && m.pricing.input != null && m.pricing.output != null) {
    return { input: m.pricing.input, output: m.pricing.output };
  }
  if (m.input_price != null && m.output_price != null) {
    return { input: m.input_price, output: m.output_price };
  }
  var mp = (typeof _modelPricingCache !== 'undefined' && _modelPricingCache)
    ? _modelPricingCache[m.model_id] : null;
  return { input: mp ? mp.input : null, output: mp ? mp.output : null };
}

/** Blended composite routing cost ($/1K) from real per-1M prices — the same
 *  formula the discovery enrichment uses (blended_1m / 1000, 4dp). */
function _deriveCompositeCost(pin, pout) {
  return Math.round(((pin + pout) / 2 / 1000) * 10000) / 10000;
}

/** Live: when both price fields hold valid numbers, the composite cost is
 *  derived (readonly + recomputed); otherwise it's manual again. */
function _onModelPriceInput(el) {
  var form = el && el.closest ? el.closest('.stg-edit-form') : null;
  if (!form) return;
  var pin = parseFloat(form.querySelector('.stg-edit-pin').value);
  var pout = parseFloat(form.querySelector('.stg-edit-pout').value);
  var both = !isNaN(pin) && !isNaN(pout) && pin >= 0 && pout >= 0;
  var costEl = form.querySelector('.stg-edit-cost');
  if (costEl) {
    costEl.readOnly = both;
    if (both) costEl.value = _deriveCompositeCost(pin, pout);
  }
  var hintEl = form.querySelector('.stg-edit-cost-derived');
  if (hintEl) hintEl.style.display = both ? '' : 'none';
}

/** Live: pinning a face away from 'auto' shows the signature-drop warning.
 *
 *  The warning is not decoration — pinning a Claude model to a non-Anthropic
 *  face is legal (the resolver allows a deliberate override) and silently
 *  strips thinking-block signatures. The backend logs it; the user needs to
 *  see it at the moment of choosing. */
function _onFacePinChange(el) {
  var form = el && el.closest ? el.closest('.stg-edit-form') : null;
  if (!form) return;
  var warn = form.querySelector('.stg-face-warn');
  if (warn) warn.style.display = String(el.value || '').trim() ? '' : 'none';
}

function _saveModelEdit(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;

  var form = document.querySelector('.stg-edit-form');
  if (!form) return;

  // Price validation happens BEFORE any mutation: an invalid pair (only one
  // field filled / negative / non-numeric) REJECTS the whole save with an
  // alert and leaves the stored override untouched — the pre-fix behaviour
  // silently DELETED the saved pricing on any typo. Both-empty is the one
  // explicit "clear the override" gesture and stays allowed.
  var _pinRaw = String(form.querySelector('.stg-edit-pin').value || '').trim();
  var _poutRaw = String(form.querySelector('.stg-edit-pout').value || '').trim();
  var _pin = parseFloat(_pinRaw), _pout = parseFloat(_poutRaw);
  var _bothEmpty = (_pinRaw === '' && _poutRaw === '');
  var _bothValid = (_pinRaw !== '' && _poutRaw !== '' &&
                    !isNaN(_pin) && !isNaN(_pout) && _pin >= 0 && _pout >= 0);
  if (!_bothEmpty && !_bothValid) {
    showAlert(t('settings.mePriceInvalidWarn'));
    return;
  }

  var oldModelId = m.model_id;
  m.model_id = String(form.querySelector('.stg-edit-mid').value || '').trim();
  m.rpm = parseInt(form.querySelector('.stg-edit-rpm').value) || 30;
  m.thinking_default = form.querySelector('.stg-edit-think').checked;

  // Input/output prices → the per-model `pricing` override (registered into
  // PROVIDER_PRICING backend-side, so cost accounting honors it). The pair
  // is all-or-nothing: the backend skips a pricing row missing either axis.
  // When both are set the composite routing cost is DERIVED from them —
  // a hand-entered blended number that disagrees with real prices is how
  // the two drifted apart in the first place.
  if (_bothValid) {
    var _pr = (m.pricing && typeof m.pricing === 'object' && !Array.isArray(m.pricing)) ? m.pricing : {};
    _pr.input = _pin;
    _pr.output = _pout;
    m.pricing = _pr;
    m.cost = _deriveCompositeCost(_pin, _pout);
  } else {
    if (m.pricing && typeof m.pricing === 'object' && !Array.isArray(m.pricing)) {
      delete m.pricing.input;
      delete m.pricing.output;
      if (Object.keys(m.pricing).length === 0) delete m.pricing;
    }
    m.cost = parseFloat(form.querySelector('.stg-edit-cost').value) || 0.01;
  }

  var caps = [];
  form.querySelectorAll('.stg-cap-btn.active').forEach(function(el) { caps.push(el.dataset.cap); });
  m.capabilities = caps;

  /* Wire-face pin. Absent select (single-face provider) leaves any existing
   * pin untouched — the form never showed it, so it must not silently drop
   * a value the user set elsewhere. */
  var _faceEl = form.querySelector('.stg-edit-face');
  if (_faceEl) {
    var _faceVal = String(_faceEl.value || '').trim();
    if (_faceVal) m.face = _faceVal;
    else delete m.face;
  }

  var _aliasEl = form.querySelector('.stg-edit-aliases');
  var _saveField = (_aliasEl && _aliasEl.dataset.poolField) || 'aliases';
  var aliasStr = (_aliasEl ? _aliasEl.value || '' : '').trim();
  var _parsed = aliasStr
    ? aliasStr.split(',').map(function(s) { return s.trim(); }).filter(Boolean)
    : [];
  // Never let an edit empty a wire pool — that resolves to zero slots and the
  // model disappears from routing while still rendering in the card.
  if (_saveField === 'request_ids' && !_parsed.length) {
    showAlert(t('settings.meRequestIdsLastWarn'));
    return;
  }
  m[_saveField] = _parsed;

  // Update presets if model_id changed
  if (oldModelId && oldModelId !== m.model_id) {
    for (var k in _stgPresets) {
      if (_stgPresets[k] === oldModelId) _stgPresets[k] = m.model_id;
    }
  }

  // Re-position this model alphabetically: the id may have just been set
  // (new model) or changed (rename). Pull it out and re-insert in order.
  if (typeof _insertModelSorted === 'function') {
    p.models.splice(modelIdx, 1);
    _insertModelSorted(p.models, m);
  }

  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
  // The pin may have just changed which wire this model dispatches over —
  // re-ask the backend so the card's pill reflects the new verdict.
  if (typeof _refreshFaceResolutions === 'function') _refreshFaceResolutions(provIdx);
}
//
// The card renders `request_ids` when the entry declares one (the wire pool of
// the model-identity contract) and falls back to legacy `aliases` otherwise.
// These handlers MUST mutate whichever field the card actually rendered, or a
// chip removal is a no-op that looks like it worked.

/** Name of the field backing the chips for this entry. */
function _poolField(m) {
  return (m && m.request_ids && m.request_ids.length) ? 'request_ids' : 'aliases';
}

async function _addAlias(provIdx, modelIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;
  var field = _poolField(m);
  var alias = String(await showPrompt(t(field === 'request_ids'
    ? 'settings.meRequestIdPrompt' : 'settings.meAliasPrompt')) || '');
  if (!alias || !alias.trim()) return;
  if (!m[field]) m[field] = [];
  alias = alias.trim();
  // A wire pool MAY legitimately contain the logical model_id (an entry whose
  // logical name the gateway also accepts); legacy aliases may not, since the
  // root is implicitly in that pool already.
  var clash = (field === 'aliases') && alias === m.model_id;
  if (m[field].indexOf(alias) === -1 && !clash) {
    m[field].push(alias);
    _renderProvidersTab();
  }
}

function _removeAlias(provIdx, modelIdx, aliasIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models) return;
  var m = p.models[modelIdx];
  if (!m) return;
  var field = _poolField(m);
  if (!m[field]) return;
  // Refuse to empty a wire pool: an entry with no request id resolves to zero
  // slots, so the model silently vanishes from routing while still showing in
  // the card. Deleting the model is the explicit way to do that.
  if (field === 'request_ids' && m[field].length <= 1) {
    showAlert(t('settings.meRequestIdsLastWarn'));
    return;
  }
  m[field].splice(aliasIdx, 1);
  _renderProvidersTab();
}

