/* ═══════════════════════════════════════════════════════════════════
   settings/provider faces — wire-face visibility + editing (2026-07-29)

   ONE account can expose SEVERAL wire faces (a `base_url` + `protocol`
   pair). The Meituan gateway is the canonical shape: the same keys serve
   an OpenAI-compatible face on /v1/openai/native and an Anthropic-native
   face on /v1/anthropic. Which face a model uses is a CORRECTNESS fact,
   not a preference — measured 2026-07-28, the OpenAI face streamed 111
   chunks / 33 reasoning_content / 0 signature, and a thinking block
   replayed without its signature is rejected upstream.

   The backend has owned this since charter #23 (lib/llm_dispatch/
   provider_face.py). This file makes it VISIBLE and EDITABLE:

     1. a face pill on every model card        (_faceChipHTML)
     2. a per-model pin dropdown in the editor (settings/model_edit.js)
     3. a provider-level faces{} editor        (_renderFacesSection)

   ── THE RULE THIS FILE MUST NOT BREAK ──
   The family rule ("Claude belongs on the Anthropic wire") is NEVER
   re-implemented here. Every verdict comes from POST
   /api/v1/providers/resolve-faces, which calls the SAME resolve_face the
   dispatcher uses. A second, hand-written JS copy would drift — and the
   drift direction is a pill reading "anthropic" on a model that actually
   dispatches over the OpenAI wire, i.e. the exact silent
   signature-dropping failure the resolver exists to prevent. Charter #12
   bars hand-copied backend enums for the same reason.

   Resolution is ASYNC (a network round-trip) while _renderModelCard is
   SYNC, so pills render from a per-provider cache that is refreshed after
   every mutation. A cache MISS renders nothing rather than a guess: an
   absent pill is honest, a wrong one is not.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file.
   ═══════════════════════════════════════════════════════════════════ */

// provIdx → { byModel: {model_id: resolution}, faces: [name], dualFaceHost: bool }
var _stgFaceResolutions = {};

/** Resolution record for one model card, or null when not yet resolved. */
function _faceResolutionFor(provIdx, modelId) {
  var rec = _stgFaceResolutions[provIdx];
  if (!rec || !rec.byModel) return null;
  return rec.byModel[modelId] || null;
}

/** Declared face names for a provider (default first).
 *
 *  Sourced from the backend's provider_faces() when a resolution has
 *  landed; falls back to deriving from the working copy so the pin
 *  dropdown still lists the right options on a card the user just edited
 *  (that derivation is pure key enumeration — NOT the family rule). */
function _faceNamesFor(provIdx) {
  var rec = _stgFaceResolutions[provIdx];
  if (rec && Array.isArray(rec.faces) && rec.faces.length) return rec.faces.slice();
  var p = _stgProviders[provIdx] || {};
  var names = ['default'];
  if (p.faces && typeof p.faces === 'object') {
    Object.keys(p.faces).forEach(function(n) {
      if (n && n !== 'default' && names.indexOf(n) < 0) names.push(n);
    });
  }
  return names;
}

/** Ask the backend to resolve every model of one provider, then re-render.
 *
 *  Posts the UNSAVED working copy: the panel edits a draft, so resolving
 *  against the saved config would answer a question the user did not ask
 *  (and would go stale the moment they pin a face or add a model).
 *
 *  Credentials are not part of the routing decision and are stripped
 *  before the post — the endpoint never needs them, so they should not
 *  travel. */
async function _refreshFaceResolutions(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var payload = {
    id: p.id, base_url: p.base_url || '', protocol: p.protocol || '',
    faces: p.faces || {},
    /* The dispatcher skips a whole card before it ever resolves a face
     * (provider disabled, or no usable key), and skips individual entries
     * whose own toggle is off. The endpoint mirrors those filters, so the
     * fields they read MUST travel — otherwise the skip branch is
     * unreachable from the real UI and a model the user merely switched
     * OFF gets blamed on a missing wire face.
     *
     * Credentials stay home: `api_key_count` is the smallest honest fact
     * that answers "does this card have a usable key", and `brand` is
     * needed because a keyless brand=='local' card still builds slots
     * (one blank-key slot — dispatcher.py L401). */
    enabled: p.enabled !== false,
    brand: p.brand || '',
    api_key_count: (p.api_keys || []).filter(function(k) {
      return k && String(k).trim();
    }).length,
    models: (p.models || []).map(function(m) {
      return {
        model_id: m.model_id, face: m.face || '',
        enabled: m.enabled !== false,
        request_ids: m.request_ids || [], aliases: m.aliases || [],
      };
    }),
  };
  var data = null;
  try {
    data = await Api.providers.resolveFaces(payload);
  } catch (e) {
    debugLog('[Settings] face resolution failed: ' + (e && e.message), 'warning');
    return;
  }
  if (!data || !data.ok || !Array.isArray(data.resolutions)) return;

  var byModel = {};
  for (var i = 0; i < data.resolutions.length; i++) {
    var r = data.resolutions[i];
    if (r && r.model_id) byModel[r.model_id] = r;
  }
  _stgFaceResolutions[provIdx] = {
    byModel: byModel,
    faces: Array.isArray(data.faces) ? data.faces : null,
    dualFaceHost: !!data.dual_face_host,
  };
  /* A full re-render would blow away an open edit form / half-typed face
   * row — the resolve round-trip is triggered BY those very edits, so
   * re-rendering on its return would fight the user's cursor. Patch just
   * the pills in place; a later structural render picks them up anyway
   * because _renderModelCard reads the same cache. */
  _repaintFaceChips(provIdx);
}

/** Replace the face pill of every rendered model card of one provider.
 *
 *  In-place DOM patch (no re-render) so an open edit form survives. */
function _repaintFaceChips(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var cards = document.querySelectorAll(
    '.stg-mcard[data-prov="' + provIdx + '"]');
  for (var i = 0; i < cards.length; i++) {
    var mi = parseInt(cards[i].getAttribute('data-model'), 10);
    var m = (p.models || [])[mi];
    if (!m) continue;
    var main = cards[i].querySelector('.stg-mcard-main');
    if (!main) continue;
    var old = main.querySelector('.stg-face-chip');
    if (old) old.remove();
    var html = _faceChipHTML(provIdx, m);
    if (html) main.insertAdjacentHTML('beforeend', html);
  }
}

/** Resolve faces for every provider (called once after config load).
 *
 *  Every card is resolved, including single-face ones: the refusal state
 *  is exactly the case where the provider has NO alternate face but its
 *  host offers one, so skipping "looks single-face" cards would blind the
 *  UI to the one verdict it most needs to show. _faceChipHTML decides
 *  what is worth rendering; this just makes the answer available. */
function _refreshAllFaceResolutions() {
  for (var i = 0; i < _stgProviders.length; i++) {
    if (_stgProviders[i]) _refreshFaceResolutions(i);
  }
}

/** The face pill for one model card.
 *
 *  Renders ONLY from a landed resolution — no guess on a cache miss.
 *  Three visual states, because they mean different things:
 *    • refused  — the model is NOT registered; routing cannot serve it
 *    • pinned   — an operator override beat the family rule (`forced`)
 *    • normal   — the resolver's own verdict
 *  A non-default face is always shown; the default face is shown only
 *  when the provider declares alternates (otherwise every model on every
 *  single-face provider would carry a redundant "openai" chip). */
function _faceChipHTML(provIdx, m) {
  if (!m || !m.model_id) return '';
  var r = _faceResolutionFor(provIdx, m.model_id);
  if (!r) return '';
  var names = _faceNamesFor(provIdx);
  var hasAlternates = names.length > 1;

  /* The dispatcher never resolved this entry — it was filtered out before
   * the resolver ran (card disabled / no usable key / model toggled off).
   * It therefore has NO wire face, and any pill here would assert routing
   * for something that is not routed. Silence is the accurate answer; the
   * card's own disabled styling already tells the user why. */
  if (r.skipped) return '';

  if (!r.ok) {
    return '<span class="stg-face-chip refused" title="' +
      escapeHtml(r.error || '') + '">' +
      escapeHtml(t('settings.faceChipRefused')) + '</span>';
  }
  if (r.face === 'default' && !hasAlternates) return '';

  var label = r.protocol || 'openai';
  var cls = (r.protocol === 'anthropic') ? ' anthropic'
    : (r.protocol === 'responses') ? ' responses' : '';
  var title = t('settings.faceChipTitle', { face: r.face, url: r.base_url });
  if (r.forced) {
    return '<span class="stg-face-chip pinned' + cls + '" title="' +
      escapeHtml(title + ' — ' + t('settings.faceChipPinnedTitle')) + '">' +
      escapeHtml(label) + ' ' + escapeHtml(t('settings.faceChipPinnedTag')) +
      '</span>';
  }
  return '<span class="stg-face-chip' + cls + '" title="' + escapeHtml(title) + '">' +
    escapeHtml(label) + '</span>';
}

// ══════════════════════════════════════════════════════
//  Provider-level faces{} editor
// ══════════════════════════════════════════════════════
//
// Until now the ONLY writer of faces{} was _syncFromTemplate, so a
// self-hosted or self-built dual-face gateway was inexpressible through
// the UI — the user had to hand-edit server_config.json. These rows make
// it a normal provider field, mirroring the Custom Headers editor.

/** Render the alternate-wire-faces section for one provider. */
function _renderFacesSection(provIdx, facesObj) {
  var entries = [];
  if (facesObj && typeof facesObj === 'object' && !Array.isArray(facesObj)) {
    Object.keys(facesObj).forEach(function(name) {
      var spec = facesObj[name] || {};
      entries.push([name, spec.base_url || '', spec.protocol || '']);
    });
  }

  var html = '<div class="stg-field stg-faces-field" data-prov-idx="' + provIdx + '">' +
    '<div class="stg-faces-header">' +
      '<label style="margin:0;">' + escapeHtml(t('settings.wireFaces')) +
        ' <span class="stg-hint">（' + escapeHtml(t('settings.wireFacesHint')) + '）</span></label>' +
      '<button type="button" class="stg-btn-add stg-faces-tb" ' +
        'onclick="_addFace(' + provIdx + ')" ' +
        'title="' + escapeHtml(t('settings.addFaceTitle')) + '">' +
        escapeHtml(t('settings.addFace')) + '</button>' +
    '</div>';

  if (entries.length === 0) {
    html += '<div class="stg-faces-empty">' + escapeHtml(t('settings.noFaces')) + '</div>';
  } else {
    html += '<div class="stg-faces-list">';
    for (var i = 0; i < entries.length; i++) {
      html += _renderFaceRow(provIdx, i, entries[i][0], entries[i][1], entries[i][2]);
    }
    html += '</div>';
  }
  html += '</div>';
  return html;
}

/** One face row: name + base_url + protocol select + delete. */
function _renderFaceRow(provIdx, idx, name, baseUrl, protocol) {
  var protoOpts = ['openai', 'anthropic', 'responses'];
  /* PRESERVE a stored value this build doesn't know (a future protocol):
   * append it as an option. Without this the select reports the FIRST
   * entry for an unrecognised value, and the next save writes THAT back —
   * measured 2026-07-31: a 'responses' face silently came back 'anthropic'
   * and the provider was flipped onto /messages (epic pt_b7a29ea7 S3). */
  if (protocol && protoOpts.indexOf(protocol) < 0) protoOpts.push(protocol);
  var sel = '<select class="stg-face-proto" data-face-field="protocol" ' +
    'onchange="_onFaceRowEdit(' + provIdx + ')">';
  for (var i = 0; i < protoOpts.length; i++) {
    sel += '<option value="' + protoOpts[i] + '"' +
      (protocol === protoOpts[i] ? ' selected' : '') + '>' + protoOpts[i] + '</option>';
  }
  sel += '</select>';

  return '<div class="stg-face-row" data-face-idx="' + idx + '" ' +
    'data-orig-protocol="' + escapeHtml(protocol || '') + '">' +
    '<input type="text" class="stg-face-name" data-face-field="name" ' +
      'placeholder="' + escapeHtml(t('settings.faceNamePlaceholder')) + '" ' +
      'spellcheck="false" autocomplete="off" ' +
      'value="' + escapeHtml(name || '') + '" ' +
      'onchange="_onFaceRowEdit(' + provIdx + ')">' +
    '<input type="text" class="stg-face-url" data-face-field="base_url" ' +
      'placeholder="https://gateway.example.com/v1/anthropic" ' +
      'spellcheck="false" autocomplete="off" ' +
      'value="' + escapeHtml(baseUrl || '') + '" ' +
      'onchange="_onFaceRowEdit(' + provIdx + ')">' +
    sel +
    '<button type="button" class="stg-faces-btn danger" ' +
      'onclick="_deleteFace(' + provIdx + ',' + idx + ')" ' +
      'title="' + escapeHtml(t('settings.deleteFaceTitle')) + '">✕</button>' +
  '</div>';
}

/** Re-collect the rows of one provider into provider.faces. */
function _collectFacesFromDom(provIdx) {
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return null;
  var field = card.querySelector('.stg-faces-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return {};
  var rows = field.querySelectorAll('.stg-face-row');
  var out = {};
  for (var i = 0; i < rows.length; i++) {
    var nameEl = rows[i].querySelector('[data-face-field="name"]');
    var urlEl = rows[i].querySelector('[data-face-field="base_url"]');
    var protoEl = rows[i].querySelector('[data-face-field="protocol"]');
    var n = (nameEl && nameEl.value || '').trim();
    // 'default' is the provider's own base_url/protocol, not an alternate —
    // accepting it here would create a second, contradictory source for the
    // same face.
    if (!n || n === 'default') continue;
    out[n] = {
      base_url: (urlEl && urlEl.value || '').trim(),
      /* Never collapse an unreadable select to a hard-coded protocol: keep
       * the value the row was RENDERED with (data-orig-protocol). 'openai'
       * is only the no-information default for a brand-new row. */
      protocol: (protoEl && protoEl.value) ||
        rows[i].getAttribute('data-orig-protocol') || 'openai',
    };
  }
  return out;
}

/** Live-edit handler for a face row. Re-resolves so the pills follow. */
function _onFaceRowEdit(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var collected = _collectFacesFromDom(provIdx);
  if (collected === null) return;
  if (Object.keys(collected).length === 0) delete p.faces;
  else p.faces = collected;
  _refreshFaceResolutions(provIdx);
}

/** Append a blank face row (DOM-first, like Custom Headers). */
function _addFace(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var field = card.querySelector('.stg-faces-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return;

  var list = field.querySelector('.stg-faces-list');
  if (!list) {
    var emptyEl = field.querySelector('.stg-faces-empty');
    if (emptyEl) emptyEl.remove();
    list = document.createElement('div');
    list.className = 'stg-faces-list';
    field.appendChild(list);
  }
  var nextIdx = list.querySelectorAll('.stg-face-row').length;
  list.insertAdjacentHTML('beforeend',
    _renderFaceRow(provIdx, nextIdx, '', '', 'anthropic'));
  var rows = list.querySelectorAll('.stg-face-row');
  var nameInput = rows[rows.length - 1].querySelector('[data-face-field="name"]');
  if (nameInput) nameInput.focus();
}

/** Remove one face row.
 *
 *  Deleting a face that models still resolve THROUGH is destructive in a
 *  way the row itself doesn't show: on a dual-face gateway every Claude
 *  entry becomes refused (no anthropic face left). So we name the affected
 *  models and ask first, rather than letting the picker quietly lose them. */
async function _deleteFace(provIdx, idx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (!card) return;
  var field = card.querySelector('.stg-faces-field[data-prov-idx="' + provIdx + '"]');
  if (!field) return;
  var row = field.querySelector('.stg-face-row[data-face-idx="' + idx + '"]');
  if (!row || !row.parentNode) return;

  var nameEl = row.querySelector('[data-face-field="name"]');
  var faceName = (nameEl && nameEl.value || '').trim();
  var rec = _stgFaceResolutions[provIdx];
  var affected = [];
  if (faceName && rec && rec.byModel) {
    Object.keys(rec.byModel).forEach(function(mid) {
      if (rec.byModel[mid] && rec.byModel[mid].face === faceName) affected.push(mid);
    });
  }
  if (affected.length) {
    var ok = await showConfirm(
      t('settings.faceDeleteConfirm', { face: faceName, n: affected.length,
                                        models: affected.join('、') }),
      { danger: true });
    if (!ok) return;
  }

  row.parentNode.removeChild(row);
  var remaining = field.querySelectorAll('.stg-face-row');
  for (var i = 0; i < remaining.length; i++) {
    remaining[i].setAttribute('data-face-idx', i);
    var del = remaining[i].querySelector('.stg-faces-btn.danger');
    if (del) del.setAttribute('onclick', '_deleteFace(' + provIdx + ',' + i + ')');
  }

  var collected = _collectFacesFromDom(provIdx) || {};
  if (Object.keys(collected).length === 0) delete p.faces;
  else p.faces = collected;

  var list = field.querySelector('.stg-faces-list');
  if (list && list.querySelectorAll('.stg-face-row').length === 0) {
    list.remove();
    var hint = document.createElement('div');
    hint.className = 'stg-faces-empty';
    hint.textContent = t('settings.noFaces');
    field.appendChild(hint);
  }
  _refreshFaceResolutions(provIdx);
}
