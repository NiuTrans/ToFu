/* ═══════════════════════════════════════════════════════════════════
   settings/access matrix — per-(key × model-id) capability grid (2026-06).

   Some gateways (e.g. Meituan AIGC) give each API key a *different* quota
   and a *different* set of accessible models. The flat model list can't
   express that: disabling a model or aliasing it hits every key at once.

   This module renders an alternate "Access Matrix" view of a provider —
   model IDs down the side, keys across the top — where every cell is an
   independent (key, id) access control.

   ── Aliases are distinct models ──────────────────────────────────────
   On a gateway each alias can route to a genuinely DIFFERENT upstream
   model, so the matrix gives every alias its own row (grouped under its
   root model). Toggling a cell adds/removes that concrete id from the
   key's ``disabled_ids`` list — the dispatcher skips exactly those
   (key, id) slots and keeps the rest.

       model.key_access = {
         "0": { "disabled_ids": ["gpt-4o-mirror"] },   // key#0 won't serve this alias
         "1": { "rpm": 10, "capabilities": ["text"] }  // key#1 RPM/caps override
       }

   An absent index inherits the model defaults (fully backward compatible).
   ``provider.key_labels`` (index-aligned, optional) holds friendly key names.

   ── Probe & Recommend ────────────────────────────────────────────────
   The "Probe & Recommend" button starts a SERVER-OWNED background task
   that sends a tiny request to every (key × id) cell. Progress is
   persisted to disk under data/config/probe_cache/ keyed by provider id,
   so closing Settings (or restarting the server) never loses it — the UI
   re-attaches by provider id and keeps polling. Only "Retest" (force)
   discards the saved result and starts over.

   This file is concatenated by lib/js_bundler.py — symbols share the same
   window scope as every other static/js/*.js file. No imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/** Per-provider matrix view toggle state, keyed by provider index. */
var _stgMatrixOpen = {};

/** Per-provider probe snapshot, keyed by provider INDEX. Shape:
 *  ``{ status: 'running'|'done'|'error', cells: { "<keyIdx>::<id>": {key_idx,
 *      model_id, root_model_id, status, detail, recommend_disable} },
 *      summary: {ok, disable}, total, done_count, error }``. */
var _stgMatrixProbe = {};

/** Active poll-timer handles, keyed by provider index. */
var _stgMatrixProbeTimers = {};

/** Providers we've already tried to re-attach to a persisted probe this
 *  session (so re-renders don't re-fetch on every keystroke). */
var _stgMatrixProbeAttached = {};

/** Per-provider "attempts per cell" setting (filters false 429s). Default 3. */
var _stgMatrixAttempts = {};

/** The scope of the currently-running probe, keyed by provider index.
 *  Shape: ``{key_idxs?: [int], model_ids?: [string]}`` — null/absent means a
 *  full-grid probe. Drives the per-scope spinner on the row/column/cell
 *  probe buttons. Cleared when the probe reaches a terminal state. */
var _stgMatrixProbeScope = {};

/** Shared lightning-bolt glyph for every probe trigger (toolbar + scopes). */
var _MX_BOLT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg>';

/** Update the attempts setting for a provider from the toolbar selector. */
function _setMatrixAttempts(provIdx, val) {
  _stgMatrixAttempts[provIdx] = Math.max(1, Math.min(5, parseInt(val, 10) || 3));
}

/** Compose the probe-cell map key. */
function _probeCellKey(keyIdx, modelId) { return keyIdx + '::' + modelId; }

/** Stable provider id used for probe persistence (mirrors the backend). */
function _providerId(provIdx) {
  var p = _stgProviders[provIdx];
  return (p && p.id) ? p.id : ('idx_' + provIdx);
}

/** True while the running probe's scope IS exactly this row / column / cell
 *  (used to paint the spinner on the trigger the user clicked). */
function _scopeCovers(provIdx, kind, keyIdx, modelId) {
  var s = _stgMatrixProbeScope[provIdx];
  var probe = _stgMatrixProbe[provIdx];
  if (!s || !probe || probe.status !== 'running') return false;
  var ks = s.key_idxs, ms = s.model_ids;
  if (kind === 'cell') {
    return !!(ks && ms && ks.length === 1 && ks[0] === keyIdx &&
              ms.length === 1 && ms[0] === modelId);
  }
  if (kind === 'col') return !!(ks && !ms && ks.length === 1 && ks[0] === keyIdx);
  if (kind === 'row') return !!(ms && !ks && ms.length === 1 && ms[0] === modelId);
  return false;
}

/** Start a row / column / single-cell probe (merged into the saved snapshot
 *  server-side; the rest of the grid keeps its verdicts). */
function _probeMatrixScope(provIdx, only) {
  var probe = _stgMatrixProbe[provIdx];
  if (probe && probe.status === 'running') return; // one probe per provider at a time
  _runMatrixProbe(provIdx, false, only);
}

/** Memo of the last fit: the inputs the verdict was computed from, plus the
 *  verdict itself. Keyed on things our own width change can NOT alter:
 *   - the scroll ELEMENT references. Matrix content only ever changes through
 *     a full `_renderProvidersTab` rebuild, which returns a brand-new element
 *     — so the same element reference across two fits means the content (and
 *     its intrinsic width) is byte-identical. This is the ONLY truthful
 *     content signal: scrollWidth saturates to the panel width once wide, so
 *     no width reading can see a content change from inside the wide state.
 *   - the viewport width, which a real window resize changes.
 *   - the class state we last produced, so an external toggle re-fits.
 *  Never keyed on scrollWidth — the class we toggle feeds back into it. */
var _mxFitMemo = null;

/** Set while _fitMatrixPanelWidth mutates the panel, so the `resize` event our
 *  own 380px width change provokes (the overlay's scrollbar appearing or
 *  disappearing) is not treated as user intent and bounced straight back. */
var _mxFitApplying = false;
var _mxFitApplyT = null;

/** The current matrix scroll elements as a plain array (NodeList in the
 *  browser, array in the node harness). */
function _mxFitScrolls() {
  var list = document.querySelectorAll('.stg-matrix-scroll');
  var out = [];
  for (var i = 0; i < list.length; i++) out.push(list[i]);
  return out;
}

/** True when nothing the verdict depends on has changed since the last fit. */
function _mxFitUnchanged(els, vw, wasWide) {
  var m = _mxFitMemo;
  if (!m || m.vw !== vw || m.wide !== wasWide || m.els.length !== els.length) return false;
  for (var i = 0; i < els.length; i++) {
    if (m.els[i] !== els[i]) return false;
  }
  return true;
}

/** Widen the settings panel when an open matrix overflows it, so 3+ keys
 *  don't force horizontal scrolling on wide-enough screens. The class is
 *  removed as soon as no matrix overflows (matrix closed / panel wide enough). */
function _fitMatrixPanelWidth() {
  if (typeof window !== 'undefined') {
    window.__fitCount = (window.__fitCount || 0) + 1;
  }
  var panel = document.querySelector('.modal.settings-panel');
  if (!panel) return;
  var wasWide = panel.classList.contains('stg-matrix-wide');

  // Idempotence gate. A re-fit whose inputs are unchanged must cost ZERO DOM
  // writes — no class toggle, no forced reflow, no transition edit. Every
  // periodic caller (probe poll, tab switch, the resize our own width change
  // echoes back) therefore becomes a no-op once the layout has settled, which
  // closes ALL re-entry paths at once rather than the one we enumerated.
  var scrolls = _mxFitScrolls();
  var vw = (typeof window !== 'undefined' && window.innerWidth) || 0;
  if (_mxFitUnchanged(scrolls, vw, wasWide)) return;

  if (typeof window !== 'undefined') {
    window.__fitWork = (window.__fitWork || 0) + 1;
  }
  _mxFitApplying = true;
  // The overflow verdict MUST be measured at the panel's DEFAULT width, never
  // at the width the class itself produces: a re-fit while the panel is wide
  // (probe-resume re-render, 1.5s probe poll, tab switch) would otherwise read
  // "no overflow" at the widened width and shrink the panel right back —
  // the expand→narrow flicker. transition:none makes the class removal take
  // effect at the forced reflow below, and everything runs in one synchronous
  // task, so no intermediate state ever paints.
  panel.style.transition = 'none';
  panel.classList.remove('stg-matrix-wide');
  var wide = false;
  for (var i = 0; i < scrolls.length; i++) {
    // Hidden matrices (inactive settings tab / collapsed provider card) have
    // a zero layout box — they must not widen the panel for something the
    // user can't see.
    if (scrolls[i].clientWidth === 0) continue;
    if (scrolls[i].scrollWidth > scrolls[i].clientWidth + 4) { wide = true; break; }
  }
  if (wide && !wasWide) {
    // Narrow→wide edge: restore the transition BEFORE the class change so the
    // single widen still animates. Every other edge applies with the
    // transition suspended — a change that never animates cannot flicker.
    panel.style.transition = '';
    panel.classList.toggle('stg-matrix-wide', true);
  } else {
    panel.classList.toggle('stg-matrix-wide', wide);
    // Commit the final width WHILE the transition is still suspended. The
    // measurement reflow above committed the panel at its DEFAULT width, so
    // that is the value the transition engine would animate FROM: clearing
    // the transition before this commit makes every re-fit of an
    // already-wide panel animate default→wide. The 1.5s probe poll re-fits
    // forever, which turned that into a continuous narrow↔wide sweep.
    void panel.offsetWidth;
    panel.style.transition = '';
  }
  // The elements are the same objects before and after our class toggle (the
  // toggle never recreates them), so the refs captured at entry describe the
  // settled state a later no-op re-fit will observe — making the memo hit.
  _mxFitMemo = { els: scrolls, vw: vw, wide: wide };
  // The flag must OUTLIVE this function. A scrollbar toggle caused by the
  // width change is delivered as an async `resize` on a later task, so
  // clearing synchronously here would leave the guard permanently false by
  // the time the echo lands. Hold it past the resize handler's own debounce.
  if (typeof setTimeout === 'function') {
    if (_mxFitApplyT) clearTimeout(_mxFitApplyT);
    _mxFitApplyT = setTimeout(function() { _mxFitApplying = false; }, 250);
  } else {
    _mxFitApplying = false;
  }
}

// Re-fit on window resize (debounced) — a wider viewport may make the wide
// panel unnecessary; a narrower one may need it even for 2 keys. Guarded for
// node harnesses that eval this file without DOM event APIs.
(function() {
  if (typeof window.addEventListener !== 'function') return;
  var _mxResizeT = null;
  window.addEventListener('resize', function() {
    window.__resizeCount = (window.__resizeCount || 0) + 1;
    // Our own widen/narrow reflows the overlay and can toggle the modal's
    // vertical scrollbar, which fires `resize`. Bouncing that back into a
    // re-fit is a closed loop with no user input in it, so drop the echo.
    if (_mxFitApplying) {
      window.__resizeEchoDropped = (window.__resizeEchoDropped || 0) + 1;
      return;
    }
    if (_mxResizeT) clearTimeout(_mxResizeT);
    _mxResizeT = setTimeout(function() {
      if (document.querySelector('.modal.settings-panel .stg-matrix-scroll')) _fitMatrixPanelWidth();
    }, 180);
  });
})();

/** Flip between the card view and the access-matrix view for a provider. */
function _toggleMatrixView(provIdx) {
  _stgMatrixOpen[provIdx] = !_stgMatrixOpen[provIdx];
  if (_stgMatrixOpen[provIdx]) _stgMatrixProbeAttached[provIdx] = false; // allow resume on (re)open
  _renderProvidersTab();
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (card) card.classList.add('expanded');
}

/** The effective key list for a provider (mirrors the dispatcher: a local
 *  provider with no keys still has one implicit blank-key slot). */
function _matrixKeys(p) {
  var keys = (p.api_keys || []).filter(function(k) { return k != null; });
  if (keys.length === 0 && p.brand === 'local') return [''];
  return keys;
}

/** Friendly label for key #idx — the user-set label, else "#N". */
function _keyLabel(p, idx) {
  var labels = p.key_labels || [];
  var lbl = (labels[idx] || '').trim();
  return lbl || ('#' + (idx + 1));
}

/** The override cell for a (model, keyIdx), or {} when inheriting. */
function _getCell(m, keyIdx) {
  var ka = m.key_access || {};
  return ka[String(keyIdx)] || {};
}

/** Get-or-create the mutable cell object for a (model, keyIdx). */
function _ensureCell(m, keyIdx) {
  if (!m.key_access) m.key_access = {};
  if (!m.key_access[String(keyIdx)]) m.key_access[String(keyIdx)] = {};
  return m.key_access[String(keyIdx)];
}

/** The concrete ids of a model entry: root + each non-empty alias. */
function _modelRowIds(m) {
  return [m.model_id].concat((m.aliases || []).filter(function(a) { return a; }));
}

/** True when this key currently serves this concrete id. */
function _isIdEnabled(m, keyIdx, id) {
  var cell = _getCell(m, keyIdx);
  if (cell.enabled === false) return false;             // legacy whole-cell kill switch
  var dis = cell.disabled_ids || [];
  return dis.indexOf(id) < 0;
}

/** Drop empty overrides so server_config.json stays minimal. */
function _pruneCell(m, keyIdx) {
  var ka = m.key_access || {};
  var c = ka[String(keyIdx)];
  if (!c) return;
  if (c.enabled === undefined || c.enabled === true) delete c.enabled;
  if (Array.isArray(c.disabled_ids) && c.disabled_ids.length === 0) delete c.disabled_ids;
  if (Array.isArray(c.aliases) && c.aliases.length === 0) delete c.aliases;
  if (c.rpm === undefined || c.rpm === null || c.rpm === '') delete c.rpm;
  if (Array.isArray(c.capabilities) && c.capabilities.length === 0) delete c.capabilities;
  if (Object.keys(c).length === 0) delete ka[String(keyIdx)];
  if (Object.keys(ka).length === 0) delete m.key_access;
  else m.key_access = ka;
}

// ── Render ──────────────────────────────────────────────────────────────

/** Build the full access-matrix table for a provider. */
function _renderAccessMatrix(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return '';
  var models = p.models || [];
  var keys = _matrixKeys(p);

  if (keys.length === 0) {
    return '<div class="stg-matrix-empty">' + escapeHtml(t('settings.matrixNoKeys')) + '</div>';
  }
  if (models.length === 0) {
    return '<div class="stg-matrix-empty">' + escapeHtml(t('settings.noModels')) + '</div>';
  }

  // Lazily re-attach to a persisted/running probe the first time we render
  // this provider's matrix in a session.
  if (!_stgMatrixProbeAttached[provIdx]) {
    _stgMatrixProbeAttached[provIdx] = true;
    setTimeout(function() { _resumeMatrixProbe(provIdx); }, 0);
  }

  var probe = _stgMatrixProbe[provIdx] || {};
  var running = (probe.status === 'running');
  var hasResults = probe.cells && Object.keys(probe.cells).length > 0;
  var recommendCount = (probe.summary && probe.summary.disable) || 0;

  var statusTxt = '';
  if (running) {
    statusTxt = t('settings.matrixProbing') +
      (probe.total ? ' (' + (probe.done_count || 0) + '/' + probe.total + ')' : '');
  } else if (probe.status === 'error') {
    statusTxt = t('settings.matrixProbeFailed') + (probe.error ? ': ' + probe.error : '');
  } else if (hasResults) {
    statusTxt = (probe.summary.ok || 0) + ' ' + t('settings.matrixOkCount') +
      ' · ' + recommendCount + ' ' + t('settings.matrixFlaggedCount') +
      ((probe.summary.skipped || 0) > 0
        ? ' · ' + probe.summary.skipped + ' ' + t('settings.matrixSkippedCount')
        : '');
  }

  var html = '<div class="stg-matrix" data-prov-idx="' + provIdx + '">' +
    '<div class="stg-matrix-toolbar">' +
      '<div class="stg-matrix-legend">' +
        '<span class="stg-mx-leg on"><span class="stg-mx-dot"></span>' + escapeHtml(t('settings.matrixLegendOn')) + '</span>' +
        '<span class="stg-mx-leg off"><span class="stg-mx-dot"></span>' + escapeHtml(t('settings.matrixLegendOff')) + '</span>' +
        '<span class="stg-mx-leg ov"><span class="stg-mx-pip">±</span>' + escapeHtml(t('settings.matrixLegendOverride')) + '</span>' +
      '</div>' +
      '<div class="stg-matrix-tools">' +
        (statusTxt ? '<span class="stg-mx-status' + (running ? ' running' : (probe.status === 'error' ? ' error' : '')) + '">' + escapeHtml(statusTxt) + '</span>' : '') +
        (hasResults && recommendCount > 0 && !running
          ? '<button type="button" class="stg-btn-add stg-mx-apply" onclick="_applyMatrixRecommendations(' + provIdx + ')" title="' + escapeHtml(t('settings.matrixApplyHint')) + '">✓ ' + escapeHtml(t('settings.matrixApplyRec')) + ' (' + recommendCount + ')</button>'
          : '') +
        (hasResults && !running ? '<button type="button" class="stg-btn-add" onclick="_clearMatrixProbe(' + provIdx + ')" title="' + escapeHtml(t('settings.matrixClearProbe')) + '">' + escapeHtml(t('settings.matrixClearProbe')) + '</button>' : '') +
        (running ? '' :
          '<label class="stg-mx-attempts" title="' + escapeHtml(t('settings.matrixAttemptsHint')) + '">' + escapeHtml(t('settings.matrixAttempts')) +
            '<select onchange="_setMatrixAttempts(' + provIdx + ',this.value)">' +
              [1, 2, 3, 4, 5].map(function(n) {
                var sel = (n === (_stgMatrixAttempts[provIdx] || 3)) ? ' selected' : '';
                return '<option value="' + n + '"' + sel + '>×' + n + '</option>';
              }).join('') +
            '</select></label>') +
        '<button type="button" class="stg-btn-add stg-mx-probe' + (running ? ' running' : '') + '"' + (running ? ' disabled' : '') +
          ' onclick="_runMatrixProbe(' + provIdx + ',' + (hasResults ? 'true' : 'false') + ')" title="' + escapeHtml(t('settings.matrixProbeHint')) + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg> ' +
          escapeHtml(running ? t('settings.matrixProbing') : (hasResults ? t('settings.matrixRetest') : t('settings.matrixProbe'))) + '</button>' +
      '</div>' +
    '</div>' +
    '<div class="stg-matrix-scroll"><table class="stg-matrix-table"><thead><tr>' +
      '<th class="stg-mx-corner">' + escapeHtml(t('settings.matrixModelCol')) + '</th>';

  for (var ki = 0; ki < keys.length; ki++) {
    var tail = _maskApiKey(keys[ki] || '') || t('settings.matrixBlankKey');
    html += '<th class="stg-mx-keyhead" data-key-idx="' + ki + '">' +
      '<input class="stg-mx-keyname" value="' + escapeHtml(_keyLabel(p, ki)) + '" ' +
        'title="' + escapeHtml(t('settings.matrixRenameKey')) + '" ' +
        'spellcheck="false" autocomplete="off" ' +
        'onchange="_onKeyLabelEdit(' + provIdx + ',' + ki + ',this.value)">' +
      '<span class="stg-mx-keytail">' + escapeHtml(tail) + '</span>' +
      '<button type="button" class="stg-mx-zap col' + (_scopeCovers(provIdx, 'col', ki) ? ' probing' : '') + '"' +
        (running ? ' disabled' : '') +
        ' onclick="_probeMatrixScope(' + provIdx + ',{key_idxs:[' + ki + ']})" ' +
        'title="' + escapeHtml(t('settings.matrixProbeColHint')) + '">' + _MX_BOLT + '</button>' +
    '</th>';
  }
  html += '</tr></thead><tbody>';

  for (var mi = 0; mi < models.length; mi++) {
    var m = models[mi];
    var ids = _modelRowIds(m);
    var groupOpen = ids.length > 1; // only bracket models that HAVE aliases
    for (var ri = 0; ri < ids.length; ri++) {
      html += _renderMatrixRow(provIdx, mi, m, ids[ri], ri, ids.length, keys, groupOpen);
    }
  }
  html += '</tbody></table></div></div>';
  return html;
}

/** Render one matrix row: a single concrete id (root or alias) across keys.
 *
 *  ``rowPos`` is the id's index within the model group (0 = root), ``rowCount``
 *  the group size. Aliases are visually distinguished from the root AND from
 *  each other: a tree connector (├ / └), the FULL id in monospace (they're
 *  genuinely different upstream models, so the exact id matters), each id's
 *  own brand icon, and a colored per-alias index chip (A1, A2, …). */
function _renderMatrixRow(provIdx, modelIdx, m, id, rowPos, rowCount, keys, grouped) {
  var isAlias = rowPos > 0;
  var isLastInGroup = (rowPos === rowCount - 1);
  var globallyOff = (m.enabled === false);
  var brand = (typeof _detectBrand === 'function') ? _detectBrand(id) : '';
  var brandSvg = (typeof _brandSvg === 'function') ? _brandSvg(brand, 14) : '';

  // Row-scope probe button: probes exactly this concrete id across every key.
  var _rowProbe = _stgMatrixProbe[provIdx] || {};
  var _rowRunning = (_rowProbe.status === 'running');
  var rowProbeBtn = '<button type="button" class="stg-mx-zap row' +
      (_scopeCovers(provIdx, 'row', null, id) ? ' probing' : '') + '"' +
    (_rowRunning ? ' disabled' : '') +
    ' onclick="event.stopPropagation();_probeMatrixScope(' + provIdx +
      ',{model_ids:[' + JSON.stringify(id).replace(/"/g, '&quot;') + ']})" ' +
    'title="' + escapeHtml(t('settings.matrixProbeRowHint')) + '">' + _MX_BOLT + '</button>';

  var labelCell;
  if (isAlias) {
    var connector = isLastInGroup ? '└' : '├';
    // A distinct accent color per alias index, cycled, so two aliases of the
    // same model never look alike at a glance.
    var hue = (modelIdx * 47 + rowPos * 71) % 360;
    labelCell = '<td class="stg-mx-model alias' + (globallyOff ? ' model-off' : '') +
        (isLastInGroup ? ' last' : '') + '" style="--alias-hue:' + hue + '">' +
      '<span class="stg-mx-tree">' + connector + '</span>' +
      '<span class="stg-mx-aliasidx">A' + rowPos + '</span>' +
      '<span class="stg-mx-brand">' + brandSvg + '</span>' +
      '<span class="stg-mx-mid alias-id" title="' + escapeHtml(id) + '">' + escapeHtml(id) + '</span>' +
      rowProbeBtn +
    '</td>';
  } else {
    var aliasCount = rowCount - 1;
    var countBadge = aliasCount > 0
      ? '<span class="stg-mx-aliascount" title="' + escapeHtml(t('settings.matrixAliasCountHint')) + '">' +
          aliasCount + ' ' + escapeHtml(aliasCount === 1 ? t('settings.matrixAliasOne') : t('settings.matrixAliasMany')) + '</span>'
      : '';
    labelCell = '<td class="stg-mx-model root' + (globallyOff ? ' model-off' : '') + '">' +
      '<label class="stg-toggle stg-mx-gtoggle" title="' + escapeHtml(t('settings.matrixGlobalToggle')) + '" onclick="event.stopPropagation();">' +
        '<input type="checkbox"' + (globallyOff ? '' : ' checked') +
          ' onchange="_toggleModelEnabled(' + provIdx + ',' + modelIdx + ')">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
      '</label>' +
      '<span class="stg-mx-brand">' + brandSvg + '</span>' +
      '<span class="stg-mx-mid" title="' + escapeHtml(id || '') + '">' + escapeHtml(id || '(unnamed)') + '</span>' +
      countBadge +
      rowProbeBtn +
    '</td>';
  }

  var cls = 'stg-mx-row' + (globallyOff ? ' model-off' : '') +
    (isAlias ? ' is-alias' : ' is-root') +
    (grouped ? ' grouped' : '') + (isLastInGroup && grouped ? ' group-end' : '');
  var row = '<tr class="' + cls + '" data-model="' + modelIdx + '" data-id="' + escapeHtml(id) + '">' + labelCell;
  for (var k = 0; k < keys.length; k++) {
    row += _renderMatrixCell(provIdx, modelIdx, k, m, id, isAlias);
  }
  row += '</tr>';
  return row;
}

/** Probe status → {glyph, cls, label} for the cell health pip. */
function _probeStatusInfo(status) {
  switch (status) {
    case 'ok':           return { glyph: '✓', cls: 'ok',     label: t('settings.probeOk') };
    case 'rate_limited': return { glyph: '429', cls: 'rate', label: t('settings.probeRateLimited') };
    case 'unauthorized': return { glyph: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><circle cx="12" cy="12" r="10"/><path d="M4.929 4.929 19.07 19.071"/></svg>', cls: 'unauth', label: t('settings.probeUnauthorized') };
    case 'not_found':    return { glyph: '∅', cls: 'nf',     label: t('settings.probeNotFound') };
    case 'unavailable':  return { glyph: '⚠', cls: 'down',   label: t('settings.probeUnavailable') };
    case 'skipped':      return { glyph: 'N/A', cls: 'skip', label: t('settings.probeSkipped') };
    default:             return { glyph: '✕', cls: 'err',    label: t('settings.probeError') };
  }
}

/** Render one matrix cell (a single (key, id) access control). */
function _renderMatrixCell(provIdx, modelIdx, keyIdx, m, id, isAlias) {
  var on = _isIdEnabled(m, keyIdx, id);
  var cell = _getCell(m, keyIdx);
  var hasRpm = (cell.rpm !== undefined && cell.rpm !== null && cell.rpm !== '');
  var hasCaps = Array.isArray(cell.capabilities);
  // RPM/caps overrides live at the (key × model-entry) level → only annotate
  // the root row so we don't double-paint the badge on every alias row.
  var overridden = !isAlias && (hasRpm || hasCaps);

  var badges = '';
  if (overridden) {
    if (hasRpm) badges += '<span class="stg-mx-badge rpm" title="RPM">⏱' + escapeHtml(String(cell.rpm)) + '</span>';
    if (hasCaps) badges += '<span class="stg-mx-badge caps" title="capabilities">✦' + cell.capabilities.length + '</span>';
  }

  // Probe-status pip: exact (key, id) result — each alias is its own cell now.
  var probe = _stgMatrixProbe[provIdx] || {};
  var pcells = probe.cells || {};
  var running = (probe.status === 'running');
  var pip = '';
  var cellProbe = '';
  var cellOnly = '{key_idxs:[' + keyIdx + '],model_ids:[' +
    JSON.stringify(id).replace(/"/g, '&quot;') + ']}';
  var r = pcells[_probeCellKey(keyIdx, id)];
  if (_scopeCovers(provIdx, 'cell', keyIdx, id)) {
    // This cell is being probed right now — spin a bolt in place of the pip.
    cellProbe = '<span class="stg-mx-zap cell probing" title="' +
      escapeHtml(t('settings.matrixProbing')) + '">' + _MX_BOLT + '</span>';
  } else if (r) {
    var info = _probeStatusInfo(r.status);
    // The pip doubles as the re-probe trigger for its own cell.
    pip = '<span class="stg-mx-probe-pip ' + info.cls + ' clickable" role="button" ' +
      'title="' + escapeHtml(info.label + (r.detail ? ' — ' + r.detail : '') +
        '\n' + t('settings.matrixProbeCellHint')) + '" ' +
      'onclick="event.stopPropagation();_probeMatrixScope(' + provIdx + ',' + cellOnly + ')">' +
      info.glyph + '</span>';
  } else {
    // Never probed — hover reveals a single-cell probe button (bottom-left).
    cellProbe = '<button type="button" class="stg-mx-zap cell"' + (running ? ' disabled' : '') +
      ' onclick="event.stopPropagation();_probeMatrixScope(' + provIdx + ',' + cellOnly + ')" ' +
      'title="' + escapeHtml(t('settings.matrixProbeCellHint')) + '">' + _MX_BOLT + '</button>';
  }

  return '<td class="stg-mx-cell' + (on ? ' on' : ' off') + (overridden ? ' overridden' : '') +
      '" data-model="' + modelIdx + '" data-key-idx="' + keyIdx + '" data-id="' + escapeHtml(id) + '">' +
    '<button type="button" class="stg-mx-toggle" ' +
      'onclick="_toggleIdAccess(' + provIdx + ',' + modelIdx + ',' + keyIdx + ',' + JSON.stringify(id).replace(/"/g, '&quot;') + ')" ' +
      'title="' + escapeHtml(on ? t('settings.matrixClickDisable') : t('settings.matrixClickEnable')) + '">' +
      '<span class="stg-mx-dot"></span>' +
    '</button>' +
    pip +
    cellProbe +
    '<div class="stg-mx-badges">' + badges + '</div>' +
    (isAlias ? '' :
      '<button type="button" class="stg-mx-edit" ' +
        'onclick="_editMatrixCell(' + provIdx + ',' + modelIdx + ',' + keyIdx + ')" ' +
        'title="' + escapeHtml(t('settings.matrixEditCell')) + '">✎</button>') +
  '</td>';
}

// ── Interactions ──────────────────────────────────────────────────────

/** Rename a key (writes provider.key_labels[idx]). */
function _onKeyLabelEdit(provIdx, keyIdx, value) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  if (!p.key_labels) p.key_labels = [];
  while (p.key_labels.length <= keyIdx) p.key_labels.push('');
  p.key_labels[keyIdx] = (value || '').trim();
  if (p.key_labels.every(function(l) { return !l; })) delete p.key_labels;
}

/** Toggle a single (key, id) cell — add/remove id from the key's disabled_ids. */
function _toggleIdAccess(provIdx, modelIdx, keyIdx, id) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models || !p.models[modelIdx]) return;
  var m = p.models[modelIdx];
  var on = _isIdEnabled(m, keyIdx, id);
  var cell = _ensureCell(m, keyIdx);
  var dis = cell.disabled_ids || [];
  if (on) {
    // Was reachable → disable it for this key.
    if (dis.indexOf(id) < 0) dis.push(id);
    cell.disabled_ids = dis;
  } else {
    // Was disabled → re-enable. Also lift a legacy whole-cell kill switch.
    cell.disabled_ids = dis.filter(function(x) { return x !== id; });
    if (cell.enabled === false) delete cell.enabled;
  }
  _pruneCell(m, keyIdx);
  _rerenderMatrix(provIdx);
}

/** Open the inline (key × model-entry) override editor below the matrix.
 *  Scope: RPM + capabilities (alias on/off is handled by the row toggles). */
function _editMatrixCell(provIdx, modelIdx, keyIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models || !p.models[modelIdx]) return;
  var m = p.models[modelIdx];
  var cell = _getCell(m, keyIdx);

  var matrix = document.querySelector('.stg-matrix[data-prov-idx="' + provIdx + '"]');
  if (!matrix) return;
  var existing = matrix.parentElement.querySelector('.stg-mx-editor');
  if (existing) existing.remove();

  var inheritRpm = (cell.rpm === undefined || cell.rpm === null || cell.rpm === '');
  var inheritCaps = !Array.isArray(cell.capabilities);
  var baseRpm = m.rpm || 30;
  var allCaps = ['text', 'vision', 'video', 'thinking', 'cheap', 'image_gen', 'embedding', 'transcription', 'audio_chat'];
  var effCaps = inheritCaps ? (m.capabilities || []) : cell.capabilities;

  var title = escapeHtml((m.model_id || '(unnamed)')) + ' &times; ' + escapeHtml(_keyLabel(p, keyIdx));

  var html = '<div class="stg-mx-editor stg-edit-form" data-prov="' + provIdx + '" data-model="' + modelIdx + '" data-key-idx="' + keyIdx + '">' +
    '<div class="stg-mx-editor-title">' + title + '</div>' +
    '<div class="stg-mx-editor-sub">' + escapeHtml(t('settings.matrixEditorSub')) + '</div>' +

    '<div class="stg-mxe-ovrow">' +
      '<label class="stg-mxe-chk"><input type="checkbox" class="stg-mxe-rpm-ov"' + (inheritRpm ? '' : ' checked') +
        ' onchange="this.closest(\'.stg-mx-editor\').querySelector(\'.stg-mxe-rpm\').disabled=!this.checked">' +
        ' ' + escapeHtml(t('settings.matrixOverrideRpm')) + '</label>' +
      '<input type="number" class="stg-mxe-rpm" min="1" placeholder="' + baseRpm + '" ' +
        'value="' + (inheritRpm ? '' : escapeHtml(String(cell.rpm))) + '"' + (inheritRpm ? ' disabled' : '') + '>' +
      '<span class="stg-hint">' + escapeHtml(t('settings.matrixInheritHint')) + ' ' + baseRpm + '</span>' +
    '</div>' +

    '<div class="stg-mxe-ovrow caps">' +
      '<label class="stg-mxe-chk"><input type="checkbox" class="stg-mxe-caps-ov"' + (inheritCaps ? '' : ' checked') +
        ' onchange="var f=this.closest(\'.stg-mx-editor\');f.querySelectorAll(\'.stg-mxe-cap\').forEach(function(b){b.classList.toggle(\'locked\',!this.checked)}.bind(this));">' +
        ' ' + escapeHtml(t('settings.matrixOverrideCaps')) + '</label>' +
      '<div class="stg-cap-toggles">';
  for (var ci = 0; ci < allCaps.length; ci++) {
    var cap = allCaps[ci];
    var active = (effCaps || []).indexOf(cap) >= 0;
    html += '<button type="button" class="stg-cap-btn stg-mxe-cap' + (active ? ' active' : '') + (inheritCaps ? ' locked' : '') +
      '" data-cap="' + cap + '" onclick="if(!this.classList.contains(\'locked\'))this.classList.toggle(\'active\')">' + cap + '</button>';
  }
  html += '</div></div>' +

    '<div class="stg-edit-actions">' +
      '<button class="stg-btn-secondary" onclick="this.closest(\'.stg-mx-editor\').remove()">' + escapeHtml(t('settings.cancel')) + '</button>' +
      '<button class="stg-btn-primary" onclick="_saveMatrixCell(' + provIdx + ',' + modelIdx + ',' + keyIdx + ')">' + escapeHtml(t('settings.apply')) + '</button>' +
    '</div>' +
  '</div>';

  matrix.insertAdjacentHTML('afterend', html);
  var form = matrix.parentElement.querySelector('.stg-mx-editor');
  if (form) form.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
}

/** Commit the inline (key × model-entry) editor back into model.key_access. */
function _saveMatrixCell(provIdx, modelIdx, keyIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.models || !p.models[modelIdx]) return;
  var m = p.models[modelIdx];
  var form = document.querySelector('.stg-mx-editor[data-prov="' + provIdx + '"][data-model="' + modelIdx + '"][data-key-idx="' + keyIdx + '"]');
  if (!form) return;

  var rpmOv = form.querySelector('.stg-mxe-rpm-ov').checked;
  var capsOv = form.querySelector('.stg-mxe-caps-ov').checked;
  var cell = _ensureCell(m, keyIdx);

  if (rpmOv) {
    var rv = parseInt(form.querySelector('.stg-mxe-rpm').value, 10);
    if (rv > 0) cell.rpm = rv; else delete cell.rpm;
  } else { delete cell.rpm; }

  if (capsOv) {
    var caps = [];
    form.querySelectorAll('.stg-mxe-cap.active').forEach(function(b) { caps.push(b.dataset.cap); });
    cell.capabilities = caps;
  } else { delete cell.capabilities; }

  _pruneCell(m, keyIdx);
  _rerenderMatrix(provIdx);
}

/** Re-render the providers tab + keep this provider card expanded. */
function _rerenderMatrix(provIdx) {
  _renderProvidersTab();
  var card = document.querySelector('.stg-provider-card[data-prov-idx="' + provIdx + '"]');
  if (card) card.classList.add('expanded');
}

// ── Background probe: start / poll / resume / apply ───────────────────────

/** True when the model entry has no chat surface (image_gen /
 *  embedding / transcription). Reads the shared taxonomy helper when
 *  available, else the same hardcoded fallback set it ships with. */
function _matrixModelIsNonChat(m) {
  if (!m) return false;
  if (typeof window.isChatModel === 'function') return !window.isChatModel(m);
  var caps = m.capabilities || [];
  var nonChat = ['image_gen', 'embedding', 'transcription'];
  for (var i = 0; i < caps.length; i++) if (nonChat.indexOf(caps[i]) >= 0) return true;
  return false;
}

/** True when the cell carries a verdict from the model's OWN modality
 *  probe (image / transcription / embedding). Cells stamped 'chat', 'none',
 *  or carrying no stamp at all (pre-stamp snapshots) are NOT modality
 *  verdicts — for a non-chat model those are the stale kind. */
function _isFreshModalityVerdict(c) {
  return !!(c && c.probe_surface && c.probe_surface !== 'chat' &&
            c.probe_surface !== 'none');
}

/** Downgrade STALE probe cells for non-chat models to 'skipped'.
 *
 *  Snapshots persisted before the per-modality probes existed carry false
 *  'unavailable' verdicts produced by a CHAT-completions probe (the gateway
 *  deterministically 500s it for image/embedding models) with
 *  recommend_disable=true — applying them would disable WORKING image
 *  models. A cell is stale when its probe_surface is missing or 'chat';
 *  a verdict stamped with the model's OWN modality surface (e.g. an
 *  image-surface not_found) is FRESH and must reach the user untouched.
 *  Reconciliation runs on every ingest so old disk snapshots heal without
 *  forcing a retest; the original verdict is kept in the tooltip. */
function _reconcileProbeNonChat(provIdx) {
  var probe = _stgMatrixProbe[provIdx];
  var p = _stgProviders[provIdx];
  if (!probe || !probe.cells || !p || !p.models) return;
  var byRoot = {};
  for (var mi = 0; mi < p.models.length; mi++) byRoot[p.models[mi].model_id] = p.models[mi];
  var changed = false;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c || c.status === 'ok' || c.status === 'skipped') return;
    if (_isFreshModalityVerdict(c)) return;   // real modality verdict — keep
    var m = byRoot[c.root_model_id];
    if (!_matrixModelIsNonChat(m)) return;
    c.detail = 'stale chat-probe verdict discarded (non-chat model) — re-run ' +
               'the probe to test it via its real endpoint (was ' + c.status +
               (c.detail ? ': ' + c.detail : '') + ')';
    c.status = 'skipped';
    c.recommend_disable = false;
    changed = true;
  });
  if (!changed) return;
  var ok = 0, disable = 0, skipped = 0;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c) return;
    if (c.status === 'skipped') skipped++;
    else if (c.recommend_disable) disable++;
    else ok++;
  });
  probe.summary = { ok: ok, disable: disable, skipped: skipped };
}

/** Normalise a backend snapshot into the local _stgMatrixProbe entry.
 *  Returns true when the snapshot carried real probe data. */
function _ingestProbeSnapshot(provIdx, snap) {
  if (!snap || snap.status === 'none') return false;
  _stgMatrixProbe[provIdx] = {
    status: snap.status || 'done',
    cells: snap.cells || {},
    summary: snap.summary || { ok: 0, disable: 0 },
    total: snap.total || 0,
    done_count: snap.done_count || (snap.cells ? Object.keys(snap.cells).length : 0),
    attempts: snap.attempts || null,
    error: snap.error || null,
  };
  // Reflect the server's attempts setting in the selector on resume.
  if (snap.attempts && !_stgMatrixAttempts[provIdx]) _stgMatrixAttempts[provIdx] = snap.attempts;
  if (_stgMatrixProbe[provIdx].status !== 'running') delete _stgMatrixProbeScope[provIdx];
  _reconcileProbeNonChat(provIdx);
  return true;
}

/** Start (or, when not forcing, resume) a background probe for a provider.
 *  ``only`` (optional) scopes the run to rows/columns/cells:
 *  ``{key_idxs?: [int], model_ids?: [string]}`` — the backend probes exactly
 *  those cells and MERGES the verdicts into the persisted snapshot. */
function _runMatrixProbe(provIdx, force, only) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var existing = _stgMatrixProbe[provIdx];
  if (existing && existing.status === 'running') return; // one probe per provider at a time
  var keys = _matrixKeys(p);
  var models = (p.models || []).filter(function(m) { return (m.model_id || '').trim(); });
  if (!keys.length || !models.length) {
    if (typeof showToast === 'function') showToast(t('settings.matrixNothingToProbe'), 'warning');
    return;
  }

  _stgMatrixProbeScope[provIdx] = only || null;
  _stgMatrixProbe[provIdx] = { status: 'running', cells: (force ? {} : ((_stgMatrixProbe[provIdx] || {}).cells || {})),
    summary: { ok: 0, disable: 0 }, total: 0, done_count: 0, error: null };
  _rerenderMatrix(provIdx);

  var body = {
    provider_id: _providerId(provIdx),
    base_url: p.base_url || '',
    api_keys: keys,
    extra_headers: p.extra_headers || {},
    protocol: p.protocol || 'openai',
    // Alternate wire faces of this ONE account. The backend resolves each
    // cell's face from these (same resolver the dispatcher uses), so a
    // Claude model on a dual-face gateway is probed over the Anthropic wire
    // instead of returning a false 'not_found' from the OpenAI one.
    faces: p.faces || {},
    // Subscription providers carry the 'oauth-managed' sentinel key — the
    // backend resolves the live token per cell when oauth is set, rather
    // than probing the sentinel (a guaranteed 401 → false recommend-disable).
    oauth: p.oauth || '',
    // capabilities ride along so the server probes non-chat models via
    // their REAL endpoint (image / audio-transcription / embeddings)
    // instead of chat-probing them into a guaranteed false verdict.
    models: models.map(function(m) {
      return { model_id: m.model_id, aliases: (m.aliases || []),
               capabilities: (m.capabilities || []) };
    }),
    attempts: _stgMatrixAttempts[provIdx] || 3,
    // A scoped probe always refreshes its cells server-side (the cache-return
    // shortcut is skipped for it), so force stays a FULL-GRID-only flag.
    force: !!force && !only,
  };
  if (only) body.only = only;

  Api.providers.probeCellsStart(body).then(function(snap) {
    if (!_ingestProbeSnapshot(provIdx, snap)) {
      _stgMatrixProbe[provIdx] = { status: 'error', cells: {}, summary: { ok: 0, disable: 0 }, error: 'start failed' };
      if (typeof showToast === 'function') showToast(t('settings.matrixProbeFailed'), 'error');
      _rerenderMatrix(provIdx);
      return;
    }
    _rerenderMatrix(provIdx);
    if (_stgMatrixProbe[provIdx].status === 'running') _pollMatrixProbe(provIdx);
  }).catch(function(e) {
    _stgMatrixProbe[provIdx] = { status: 'error', cells: {}, summary: { ok: 0, disable: 0 }, error: String(e && e.message || e) };
    if (typeof showToast === 'function') showToast(t('settings.matrixProbeFailed') + ': ' + (e && e.message || e), 'error');
    _rerenderMatrix(provIdx);
  });
}

/** Poll a running probe until it reaches a terminal state. */
function _pollMatrixProbe(provIdx) {
  if (_stgMatrixProbeTimers[provIdx]) clearTimeout(_stgMatrixProbeTimers[provIdx]);
  _stgMatrixProbeTimers[provIdx] = setTimeout(function tick() {
    // Settings closed → stop polling; _resumeMatrixProbe re-attaches on reopen.
    if (!document.getElementById('stgProviderList')) {
      delete _stgMatrixProbeTimers[provIdx];
      _stgMatrixProbeAttached[provIdx] = false;
      return;
    }
    Api.providers.probeCellsStatus(_providerId(provIdx)).then(function(snap) {
      _ingestProbeSnapshot(provIdx, snap);
      _rerenderMatrix(provIdx);
      if (snap && snap.status === 'running') {
        _stgMatrixProbeTimers[provIdx] = setTimeout(tick, 1500);
      } else {
        delete _stgMatrixProbeTimers[provIdx];
      }
    }).catch(function() {
      _stgMatrixProbeTimers[provIdx] = setTimeout(tick, 3000);
    });
  }, 1500);
}

/** Re-attach to a persisted/running probe on (re)opening the matrix. */
function _resumeMatrixProbe(provIdx) {
  // Don't clobber a live local run.
  if (_stgMatrixProbe[provIdx] && _stgMatrixProbe[provIdx].status === 'running'
      && _stgMatrixProbeTimers[provIdx]) return;
  Api.providers.probeCellsStatus(_providerId(provIdx)).then(function(snap) {
    if (_ingestProbeSnapshot(provIdx, snap)) {
      _rerenderMatrix(provIdx);
      if (_stgMatrixProbe[provIdx].status === 'running') _pollMatrixProbe(provIdx);
    }
  }).catch(function() { /* best-effort resume */ });
}

/** Apply the probe's recommended disables: add every flagged concrete id to
 *  its key's disabled_ids. Because aliases are independent rows, a dead alias
 *  is dropped on its own while the root (and other aliases) stay reachable. */
function _applyMatrixRecommendations(provIdx) {
  var p = _stgProviders[provIdx];
  var probe = _stgMatrixProbe[provIdx];
  if (!p || !probe || !probe.cells) return;

  // Map root model_id → model index for quick lookup.
  var byRoot = {};
  for (var mi = 0; mi < (p.models || []).length; mi++) byRoot[p.models[mi].model_id] = mi;

  var applied = 0;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c || !c.recommend_disable) return;
    var idx = byRoot[c.root_model_id];
    if (idx === undefined) return;
    var m = p.models[idx];
    // A non-chat model may only be disabled on a verdict from its OWN
    // modality probe (probe_surface = image/transcription/embedding) — never
    // on a stale chat-completions verdict that cannot speak for its real
    // endpoint. A fresh modality not_found MUST be applicable: exposing
    // dead models is exactly what the per-modality probe exists for.
    if (_matrixModelIsNonChat(m) && !_isFreshModalityVerdict(c)) return;
    if (_isIdEnabled(m, c.key_idx, c.model_id)) {
      var cell = _ensureCell(m, c.key_idx);
      var dis = cell.disabled_ids || [];
      if (dis.indexOf(c.model_id) < 0) dis.push(c.model_id);
      cell.disabled_ids = dis;
      applied++;
    }
  });

  if (typeof showToast === 'function') {
    showToast(applied > 0
      ? t('settings.matrixApplied').replace('{n}', String(applied))
      : t('settings.matrixNothingApplied'), applied > 0 ? 'success' : 'info');
  }
  _rerenderMatrix(provIdx);
}

/** Hide probe results locally for this session (disk snapshot is kept;
 *  re-opening Settings re-attaches via _resumeMatrixProbe). */
function _clearMatrixProbe(provIdx) {
  if (_stgMatrixProbeTimers[provIdx]) {
    clearTimeout(_stgMatrixProbeTimers[provIdx]);
    delete _stgMatrixProbeTimers[provIdx];
  }
  delete _stgMatrixProbe[provIdx];
  delete _stgMatrixProbeScope[provIdx];
  _stgMatrixProbeAttached[provIdx] = true; // don't auto-reattach until reopen
  _rerenderMatrix(provIdx);
}
