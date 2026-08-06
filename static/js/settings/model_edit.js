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

  /* ── Sectioned layout (redesign 2026-08-06) ──
   * The old flat wall of fields read as one undifferentiated list — the
   * owner's "no UI/UX soul" complaint. Sections give the eye a rhythm:
   * identity → wire → limits/pricing → capabilities → aliases → footer.
   * Every load-bearing hook (class names, save selectors, the 5-field
   * stg-edit-grid contract) is preserved — only the chrome around it grew. */
  var html = '<div class="stg-edit-form" data-prov="' + provIdx + '" data-model="' + modelIdx + '">';

  /* §1 身份 — the Model ID is the identity of the whole form: full-width
   * monospace, the one field a truncated render must never hide. */
  html += '<div class="stg-edit-sec">' +
    '<div class="stg-edit-sec-label">' + escapeHtml(t('settings.meSecIdentity')) + '</div>' +
    '<div class="stg-edit-grid">' +
      '<div class="stg-field stg-field-wide"><label>' + escapeHtml(t('settings.meModelId')) + '</label>' +
        '<input type="text" class="stg-edit-mid" value="' + escapeHtml(m.model_id || '') + '" placeholder="' + escapeHtml(t('settings.meModelIdPlaceholder')) + '" spellcheck="false" autocomplete="off" oninput="_onModelIdDraftInput(this)"></div>' +
    '</div>' +
  '</div>';

  /* §2 协议线 — THE section the owner asked for: which wire protocol this
   * model actually speaks. Always rendered now (was: only when >1 face).
   * The verdict line + pin warning are built ONCE here, not per branch —
   * the redesign suite's N1 neuter rewrites the _faceAutoNoteHTML call
   * site, and two copies would leave one branch rendering a verdict the
   * neuter already removed. */
  if (typeof _faceNamesFor === 'function') {
    var _names = _faceNamesFor(provIdx);
    var _cur = (m.face || '');
    /* Protocol of each face, for pin-option labels. 'default' reads the
     * provider's own protocol; a named face reads faces[name].protocol.
     * The stored value is shown verbatim when this build doesn't know it
     * (same preserve-unknown rule as _renderFaceRow). */
    var _faceProto = function (nm) {
      if (nm === 'default') return p.protocol || 'openai';
      var f = (p.faces && p.faces[nm]) || {};
      return f.protocol || 'openai';
    };
    var _noteHtml = '<div class="stg-face-auto-note"' + (_cur ? ' style="display:none"' : '') + '>' +
        _faceAutoNoteHTML(provIdx, m) + '</div>';
    var _warnIco = (typeof Icon === 'function') ? Icon('alertTriangle', 12) : '';
    var _warnHtml = '<div class="stg-face-warn"' + (_cur ? '' : ' style="display:none"') + '>' +
        '<span class="stg-face-warn-ic">' + _warnIco + '</span>' +
        escapeHtml(t('settings.meFacePinWarn')) + '</div>';
    html += '<div class="stg-edit-sec">' +
      '<div class="stg-edit-sec-label">' + escapeHtml(t('settings.meSecWire')) + '</div>';
    if (_names.length <= 1) {
      /* Single-face provider: a face pin is a non-choice (only the default
       * face exists), so the section leads with what the owner asked for —
       * the wire protocol itself, editable at provider level without
       * hunting the provider card. Unknown stored values are appended as
       * options, never silently rewritten. The verdict line under it shows
       * the resolved endpoint and surfaces a refusal with its reason. */
      var _provProto = p.protocol || 'openai';
      var _pOpts = ['openai', 'anthropic', 'responses'];
      if (_provProto && _pOpts.indexOf(_provProto) < 0) _pOpts.push(_provProto);
      html += '<div class="stg-field"><label>' + escapeHtml(t('settings.meProvProto')) +
          ' <span class="stg-hint">' + escapeHtml(t('settings.meProvProtoHint')) + '</span></label>' +
          '<select class="stg-edit-proto" onchange="_onModelProtoChange(' + provIdx + ', this)">';
      for (var _poi = 0; _poi < _pOpts.length; _poi++) {
        html += '<option value="' + _pOpts[_poi] + '"' +
          (_provProto === _pOpts[_poi] ? ' selected' : '') + '>' + _pOpts[_poi] + '</option>';
      }
      html += '</select></div>' + _noteHtml;
    } else {
      /* Multi-face: pin a specific wire face; options name the protocol so
       * the choice is never a blind name. Auto stays the recommended default. */
      html += '<div class="stg-field"><label>' + escapeHtml(t('settings.meFace')) +
        ' <span class="stg-hint">' + escapeHtml(t('settings.meFaceHint')) + '</span></label>' +
        '<select class="stg-edit-face" onchange="_onFacePinChange(this)">' +
        '<option value=""' + (_cur === '' ? ' selected' : '') + '>' +
          escapeHtml(t('settings.meFaceAuto')) + '</option>';
      for (var fi2 = 0; fi2 < _names.length; fi2++) {
        var _lbl2 = (_names[fi2] === 'default')
          ? t('settings.meFaceDefaultFace') : _names[fi2];
        html += '<option value="' + escapeHtml(_names[fi2]) + '"' +
          (_cur === _names[fi2] ? ' selected' : '') + '>' +
          escapeHtml(_lbl2) + ' — ' + escapeHtml(_faceProto(_names[fi2])) + '</option>';
      }
      html += '</select>' + _noteHtml + _warnHtml + '</div>';
    }
    html += '</div>';
    /* Cache miss on a fresh form: ask the backend, the note patches itself
     * when the resolution lands. Both branches show the note, so both
     * need the kick. */
    if (!_cur && m.model_id &&
        typeof _faceResolutionFor === 'function' &&
        !_faceResolutionFor(provIdx, m.model_id) &&
        typeof _refreshFaceResolutions === 'function') {
      _refreshFaceResolutions(provIdx);
    }
  }

  /* §3 配额与定价 — RPM / cost / input / output as a uniform 2×2. The
   * redesign suite counts .stg-edit-grid > .stg-field across BOTH grids:
   * §1's wide Model ID + these four = exactly 5, only the first wide. */
  html += '<div class="stg-edit-sec">' +
    '<div class="stg-edit-sec-label">' + escapeHtml(t('settings.meSecQuota')) + '</div>' +
    '<div class="stg-edit-grid">' +
      '<div class="stg-field"><label>' + escapeHtml(t('settings.meRpm')) + '</label>' +
        '<input type="number" class="stg-edit-rpm" value="' + (m.rpm || 30) + '" min="1"></div>' +
      '<div class="stg-field"><label>' + escapeHtml(t('settings.meCost')) + ' <span class="stg-hint">' + escapeHtml(t('settings.meCostHint')) + '</span>' +
        '<span class="stg-hint stg-edit-cost-derived" style="display:' + (_hasOvr ? '' : 'none') + '">' + escapeHtml(t('settings.meCostDerived')) + '</span></label>' +
        '<input type="number" class="stg-edit-cost" value="' + (m.cost || 0.01) + '" step="0.001" min="0"' + (_hasOvr ? ' readonly' : '') + '></div>' +
      '<div class="stg-field"><label>' + escapeHtml(t('settings.meInputPrice')) + ' <span class="stg-hint">' + escapeHtml(t('settings.mePriceHint')) + '</span></label>' +
        '<input type="number" class="stg-edit-pin" value="' + _ovrIn + '" step="0.01" min="0" placeholder="' + (_eff.input != null ? _eff.input : '—') + '" oninput="_onModelPriceInput(this)"></div>' +
      '<div class="stg-field"><label>' + escapeHtml(t('settings.meOutputPrice')) + ' <span class="stg-hint">' + escapeHtml(t('settings.mePriceHint')) + '</span></label>' +
        '<input type="number" class="stg-edit-pout" value="' + _ovrOut + '" step="0.01" min="0" placeholder="' + (_eff.output != null ? _eff.output : '—') + '" oninput="_onModelPriceInput(this)"></div>' +
    '</div>' +
  '</div>';

  /* §4 能力 — one toggle per capability; active = gold. Icons from the
   * central SVG registry (§3.4), never emoji; the typeof guard keeps the
   * jsdom harness (which never loads icons.js) on the plain-text path. */
  var _capIcons = { text: 'messageSquare', vision: 'eye', video: 'play',
    thinking: 'brain', cheap: 'zap', image_gen: 'image', embedding: 'package',
    transcription: 'languages', audio_chat: 'messageCircle' };
  html += '<div class="stg-edit-sec">' +
    '<div class="stg-edit-sec-label">' + escapeHtml(t('settings.meCapabilities')) + '</div>' +
    '<div class="stg-cap-toggles">';
  for (var ci = 0; ci < allCaps.length; ci++) {
    var cap = allCaps[ci];
    var active = (m.capabilities || []).indexOf(cap) >= 0;
    var _ico = (typeof Icon === 'function' && _capIcons[cap]) ? Icon(_capIcons[cap], 11) : '';
    html += '<button type="button" class="stg-cap-btn' + (active ? ' active' : '') + '" data-cap="' + cap + '" onclick="this.classList.toggle(\'active\')">' +
      (_ico ? '<span class="stg-cap-ico">' + _ico + '</span>' : '') + cap + '</button>';
  }
  html += '</div></div>';

  /* §5 别名 / 请求名池 — tag editor (one id = one chip). */
  var _field = _poolField(m);
  var _isPool = (_field === 'request_ids');
  var _poolVals = m[_field] || [];
  html += '<div class="stg-edit-sec">' +
    '<div class="stg-edit-sec-label">' +
      escapeHtml(t(_isPool ? 'settings.meRequestIds' : 'settings.meAliases')) +
      ' <span class="stg-hint">' +
      escapeHtml(t(_isPool ? 'settings.meRequestIdsHint' : 'settings.meAliasesHint')) +
      '</span></div>' +
    '<div class="stg-tag-editor" data-pool-field="' + _field + '">';
  for (var _ti = 0; _ti < _poolVals.length; _ti++) {
    html += _poolTagChipHTML(_poolVals[_ti]);
  }
  html += '<input type="text" class="stg-tag-input"' +
    ' placeholder="' + escapeHtml(t(_isPool
      ? 'settings.meRequestIdsPlaceholder' : 'settings.meAliasesPlaceholder')) + '"' +
    ' spellcheck="false" autocomplete="off"' +
    ' onkeydown="_poolTagKey(this, event)" oninput="_poolTagSplit(this)"' +
    ' onblur="_poolTagCommit(this)">' +
  '</div></div>';

  /* Footer: thinking default + actions. */
  html += '<div class="stg-edit-foot">' +
    '<div class="stg-toggle-row"><span>' + escapeHtml(t('settings.meThinkingDefault')) + '</span>' +
      '<label class="stg-toggle"><input type="checkbox" class="stg-edit-think"' + (m.thinking_default ? ' checked' : '') + '>' +
      '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label></div>' +
    '<div class="stg-edit-actions">' +
      '<button class="stg-btn-secondary" onclick="this.closest(\'.stg-edit-form\').remove()">' + escapeHtml(t('settings.cancel')) + '</button>' +
      '<button class="stg-btn-primary" onclick="_saveModelEdit(' + provIdx + ',' + modelIdx + ')">' + escapeHtml(t('settings.apply')) + '</button>' +
    '</div>' +
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

/** Live: the provider-level wire protocol select (single-face branch).
 *  Writes p.protocol directly (same seam as the provider card's own
 *  select), then re-resolves faces so the card pill and the verdict note
 *  follow. The whole form re-renders on save, so no label can go stale. */
function _onModelProtoChange(provIdx, el) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  p.protocol = String(el.value || '').trim() || 'openai';
  if (typeof _refreshFaceResolutions === 'function') _refreshFaceResolutions(provIdx);
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
  /* The auto-verdict line is the mirror image of the warning: visible
   * exactly when the pin is 'automatic'. Re-rendered from the cache (the
   * model may have been re-resolved since the form opened); on a cold
   * cache this also kicks off the resolution round-trip. */
  var note = form.querySelector('.stg-face-auto-note');
  if (note) {
    note.style.display = String(el.value || '').trim() ? 'none' : '';
    if (!String(el.value || '').trim()) {
      var provIdx = parseInt(form.getAttribute('data-prov'), 10);
      if (!isNaN(provIdx)) _repaintFaceAutoNote(provIdx);
    }
  }
}

/** The 'automatically selected: X' line for the open edit form.
 *
 *  Renders ONLY from a landed backend resolution (same rule as the card
 *  pill: an absent line is honest, a guessed one is not). Four states:
 *  pending (cache miss), skipped (not routed — no face to report),
 *  refused (the rule could not register the model), and the verdict. */
function _faceAutoNoteHTML(provIdx, m) {
  if (!m || !m.model_id) return '';
  var r = (typeof _faceResolutionFor === 'function')
    ? _faceResolutionFor(provIdx, m.model_id) : null;
  if (!r) {
    return '<span class="stg-face-auto-pending">' +
      escapeHtml(t('settings.meFaceAutoPending')) + '</span>';
  }
  if (r.skipped) {
    return '<span class="stg-face-auto-pending">' +
      escapeHtml(t('settings.meFaceAutoSkipped')) + '</span>';
  }
  if (!r.ok) {
    return '<span class="stg-face-auto-refused">' +
      escapeHtml(t('settings.meFaceAutoRefused', { error: r.error || '' })) +
      '</span>';
  }
  /* 'default' is the resolver's name for the provider's own face —
   * localize it; raw 'default 面' is jargon in a zh UI. */
  var _faceLabel = (r.face === 'default')
    ? t('settings.meFaceDefaultFace') : r.face;
  return '<span class="stg-face-auto-pick" title="' +
    escapeHtml(r.base_url || '') + '">' +
    escapeHtml(t('settings.meFaceAutoResolved', {
      protocol: r.protocol || 'openai', face: _faceLabel })) + '</span>';
}

/** Patch the auto-note of the OPEN edit form for one provider, in place.
 *
 *  THE single channel every path goes through: the resolution landing
 *  (provider_faces.js hook), the pin flipping back to 'automatic', and
 *  Model ID draft edits. DRAFT-AWARE: the cached verdict was resolved for
 *  the SAVED id, so while the mid input names a different id that verdict
 *  is a wrong claim — a renamed kimi→claude draft must NOT keep showing
 *  'openai'. A mismatch flips the note to an honest 'resolves on save'
 *  pending line; matching again restores the cached verdict. Never fires
 *  a backend request itself — _refreshFaceResolutions calls this. */
function _repaintFaceAutoNote(provIdx) {
  var form = document.querySelector(
    '.stg-edit-form[data-prov="' + provIdx + '"]');
  if (!form) return;
  var note = form.querySelector('.stg-face-auto-note');
  if (!note) return;
  var sel = form.querySelector('.stg-edit-face');
  if (sel && String(sel.value || '').trim()) {
    note.style.display = 'none';
    return;
  }
  var mi = parseInt(form.getAttribute('data-model'), 10);
  var p = _stgProviders[provIdx];
  var m = (p && p.models) ? p.models[mi] : null;
  var _midEl = form.querySelector('.stg-edit-mid');
  var _draft = _midEl ? String(_midEl.value || '').trim()
                      : ((m && m.model_id) || '');
  if (_draft !== ((m && m.model_id) || '')) {
    note.innerHTML = '<span class="stg-face-auto-pending">' +
      escapeHtml(t('settings.meFaceAutoDraft')) + '</span>';
  } else {
    note.innerHTML = _faceAutoNoteHTML(provIdx, m);
  }
  note.style.display = '';
}

/** Live: editing the Model ID invalidates the auto-note's verdict. All
 *  the logic lives in _repaintFaceAutoNote (the single channel); this is
 *  just the trigger. */
function _onModelIdDraftInput(el) {
  var form = el && el.closest ? el.closest('.stg-edit-form') : null;
  if (!form) return;
  var provIdx = parseInt(form.getAttribute('data-prov'), 10);
  if (!isNaN(provIdx)) _repaintFaceAutoNote(provIdx);
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

  var _poolEl = form.querySelector('.stg-tag-editor');
  var _saveField = (_poolEl && _poolEl.dataset.poolField) || 'aliases';
  var _parsed = _poolTagValues(form);
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

/* ══ Tag editor (wire pool / aliases) ═══════════════════════════════
 * One id = one chip. The input box is only an entry device: Enter or
 * blur commits, a pasted comma/semicolon/newline SPLITS into several
 * chips (so pasting the old comma format still works), Backspace on an
 * empty box pops the last chip. Save reads the chips, never the box. */

function _poolTagChipHTML(val) {
  return '<span class="stg-tag-chip" data-value="' + escapeHtml(val) + '">' +
    escapeHtml(val) +
    '<span class="stg-tag-x" onclick="_poolTagRemove(this)">×</span></span>';
}

/** Add one chip; returns false for blanks and exact duplicates. */
function _poolTagAdd(editor, val) {
  val = String(val == null ? '' : val).trim();
  if (!val) return false;
  var chips = editor.querySelectorAll('.stg-tag-chip');
  for (var i = 0; i < chips.length; i++) {
    if (chips[i].getAttribute('data-value') === val) return false;
  }
  var input = editor.querySelector('.stg-tag-input');
  input.insertAdjacentHTML('beforebegin', _poolTagChipHTML(val));
  return true;
}

function _poolTagRemove(xEl) {
  var chip = xEl && xEl.closest ? xEl.closest('.stg-tag-chip') : null;
  if (chip) chip.remove();
}

function _poolTagKey(input, ev) {
  if (!ev) return;
  if (ev.key === 'Enter') {
    ev.preventDefault();
    _poolTagCommit(input);
  } else if (ev.key === 'Backspace' && !input.value) {
    var chips = input.closest('.stg-tag-editor')
      .querySelectorAll('.stg-tag-chip');
    if (chips.length) chips[chips.length - 1].remove();
  }
}

/* Live delimiter split: typing/pasting a comma commits what came before
 * it — commas are accepted as a convenience, never stored. */
function _poolTagSplit(input) {
  if (!/[,，\n;；]/.test(input.value)) return;
  _poolTagCommit(input);
}

function _poolTagCommit(input) {
  var editor = input && input.closest ? input.closest('.stg-tag-editor') : null;
  if (!editor) return;
  var parts = input.value.split(/[,，\n;；]/);
  for (var i = 0; i < parts.length; i++) _poolTagAdd(editor, parts[i]);
  input.value = '';
}

/** The chips' values, in order. Anything half-typed in the box is
 *  committed first — clicking 应用 with text still in the box must not
 *  silently drop it. */
function _poolTagValues(form) {
  var editor = form ? form.querySelector('.stg-tag-editor') : null;
  if (!editor) return [];
  var input = editor.querySelector('.stg-tag-input');
  if (input && String(input.value || '').trim()) _poolTagCommit(input);
  var out = [];
  editor.querySelectorAll('.stg-tag-chip').forEach(function (c) {
    var v = c.getAttribute('data-value');
    if (v) out.push(v);
  });
  return out;
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

