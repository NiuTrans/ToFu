/* ===== migrated source: settings/access_matrix.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/access matrix — per-(credential × wire id) capability grid.

   Some gateways (e.g. Meituan AIGC) give each credential a *different*
   quota and a *different* set of accessible models. The flat model list
   can't express that: the v2 authority stores the grant as
   ``credential.authorization.models`` (an allow-list of ModelRefs), which
   is exactly a (credential × model) matrix.

   This module renders that matrix for one ProviderAccess — confirmed
   offerings down the side (one row per upstream wire id, grouped under
   the canonical model), credentials across the top. A cell dot reflects
   the credential's authorization grant; toggling it adds/removes the
   offering's ModelRef in ``_stgModelRouting`` (persisted by the settings
   保存 flow, same as every other v2 card edit).

   ── Probe & Recommend ────────────────────────────────────────────────
   The probe button starts a SERVER-OWNED background task
   (POST /api/v1/providers/<id>/probe-cells/start) that resolves plaintext
   keys from the owner-scoped secret store — the browser never sees them —
   and sends a tiny request to EVERY (credential × wire id) pair, granted
   or not: discovering reachable pairs the allow-list does not grant yet
   is the matrix's whole point on gateways like Meituan. Progress is
   persisted server-side under data/config/probe_cache/, so closing
   Settings (or restarting the server) never loses it — the UI re-attaches
   by provider id and keeps polling. Only "Retest" (force) discards the
   saved result and starts over. Applying recommendations removes the
   flagged (credential × model) grants from the allow-list.

   This file is concatenated by Vite's module graph — symbols share the
   same window scope as every other runtime section. No imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/** Per-provider matrix view toggle state, keyed by provider_id. */
var _stgMatrixOpen = {};

/** Per-provider probe snapshot, keyed by provider_id. Shape:
 *  ``{ status: 'running'|'done'|'error', cells: { "<credIdx>::<wireId>":
 *      {key_idx, model_id, root_model_id, status, detail,
 *      recommend_disable} }, summary: {ok, disable}, total, done_count,
 *      error }``. */
var _stgMatrixProbe = {};

/** Active poll-timer handles, keyed by provider_id. */
var _stgMatrixProbeTimers = {};

/** Providers we've already tried to re-attach to a persisted probe this
 *  session (so re-renders don't re-fetch on every keystroke). */
var _stgMatrixProbeAttached = {};

/** Per-provider "attempts per cell" setting (filters false 429s). Default 3. */
var _stgMatrixAttempts = {};

/** The scope of the currently-running probe, keyed by provider_id.
 *  Shape: ``{key_idxs?: [int], model_ids?: [string]}`` — null/absent means a
 *  full-grid probe. Drives the per-scope spinner on the row/column/cell
 *  probe buttons. Cleared when the probe reaches a terminal state. */
var _stgMatrixProbeScope = {};

/** Shared lightning-bolt glyph for every probe trigger (toolbar + scopes). */
var _MX_BOLT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg>';

/** Update the attempts setting for a provider from the toolbar selector. */
function _setMatrixAttempts(providerId, val) {
  _stgMatrixAttempts[providerId] = Math.max(1, Math.min(5, parseInt(val, 10) || 3));
}

/** Compose the probe-cell map key. */
function _probeCellKey(keyIdx, modelId) { return keyIdx + '::' + modelId; }

/** True while the running probe's scope IS exactly this row / column / cell
 *  (used to paint the spinner on the trigger the user clicked). */
function _scopeCovers(providerId, kind, keyIdx, modelId) {
  var s = _stgMatrixProbeScope[providerId];
  var probe = _stgMatrixProbe[providerId];
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
 *  server-side; the rest of the grid keeps its verdicts). The scope arrives
 *  as scalars — the action registry has no object-literal syntax, so the
 *  ``only`` object is assembled here. */
function _probeMatrixScope(providerId, kind, first, second) {
  var probe = _stgMatrixProbe[providerId];
  if (probe && probe.status === 'running') return; // one probe per provider at a time
  var only = {};
  if (kind === 'col') only.key_idxs = [first];
  else if (kind === 'row') only.model_ids = [first];
  else if (kind === 'cell') { only.key_idxs = [first]; only.model_ids = [second]; }
  else return;
  _runMatrixProbe(providerId, false, only);
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
 *  own width change provokes (the overlay's scrollbar appearing or
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

/** Widen the settings panel when an open matrix overflows it, so 3+
 *  credential columns don't force horizontal scrolling on wide-enough
 *  screens. The class is removed as soon as no matrix overflows. */
function _fitMatrixPanelWidth() {
  var panel = document.querySelector('.modal.settings-panel');
  if (!panel) return;
  var wasWide = panel.classList.contains('stg-matrix-wide');

  // Idempotence gate. A re-fit whose inputs are unchanged must cost ZERO DOM
  // writes — no class toggle, no forced reflow, no transition edit. Every
  // periodic caller (probe poll, tab switch, the resize our own width change
  // echoes back) therefore becomes a no-op once the layout has settled.
  var scrolls = _mxFitScrolls();
  var vw = (typeof window !== 'undefined' && window.innerWidth) || 0;
  if (_mxFitUnchanged(scrolls, vw, wasWide)) return;

  _mxFitApplying = true;
  // The overflow verdict MUST be measured at the panel's DEFAULT width, never
  // at the width the class itself produces: a re-fit while the panel is wide
  // would otherwise read "no overflow" at the widened width and shrink the
  // panel right back — the expand→narrow flicker. transition:none makes the
  // class removal take effect at the forced reflow below, and everything
  // runs in one synchronous task, so no intermediate state ever paints.
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
    // single widen still animates.
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
// panel unnecessary; a narrower one may need it even for 2 columns. Guarded
// for node harnesses that eval this file without DOM event APIs.
(function() {
  if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') return;
  var _mxResizeT = null;
  window.addEventListener('resize', function() {
    // Our own widen/narrow reflows the overlay and can toggle the modal's
    // vertical scrollbar, which fires `resize`. Bouncing that back into a
    // re-fit is a closed loop with no user input in it, so drop the echo.
    if (_mxFitApplying) return;
    if (_mxResizeT) clearTimeout(_mxResizeT);
    _mxResizeT = setTimeout(function() {
      if (document.querySelector('.modal.settings-panel .stg-matrix-scroll')) _fitMatrixPanelWidth();
    }, 180);
  });
})();

/** Flip between the model-list view and the access-matrix view. */
function _toggleMatrixView(providerId) {
  providerId = String(providerId || '');
  if (!providerId) return;
  _stgMatrixOpen[providerId] = !_stgMatrixOpen[providerId];
  if (_stgMatrixOpen[providerId]) _stgMatrixProbeAttached[providerId] = false; // allow resume on (re)open
  _renderProvidersTab();
}

// ── v2 data derivation ──────────────────────────────────────────────────

/** The credential columns of the matrix: the access's credentials in
 *  document order — the SAME order the backend's probe plan uses, so cell
 *  key ``<idx>::<wire id>`` aligns between render and probe. */
function _matrixCredentials(context) {
  return context ? context.credentials : [];
}

/** De-duplicated list of trimmed non-empty strings, order stable. */
function _mxDedupe(list) {
  var seen = {}, out = [];
  for (var i = 0; i < (list || []).length; i++) {
    var v = (typeof list[i] === 'string') ? list[i].trim() : '';
    if (v && !seen[v]) { seen[v] = true; out.push(v); }
  }
  return out;
}

/** One matrix row-group per confirmed offering:
 *  ``{ offering, offeringIndex, canonical, wireIds, capabilities }``.
 *  ``wireIds`` are the ENABLED deployments' upstream ids in document order
 *  (mirroring the backend probe plan); an offering without any enabled
 *  deployment falls back to probing its canonical model id, the same
 *  legacy shape the backend uses. */
function _matrixModelRows(context) {
  var out = [];
  if (!context) return out;
  context.offerings.forEach(function(item) {
    var offering = item.row;
    if (offering.identity_state !== 'confirmed' || !offering.model) return;
    var canonical = String(offering.model.model_id || '').trim();
    if (!canonical) return;
    var wireIds = _mxDedupe(context.deployments.filter(function(dep) {
      return dep.row.offering_id === offering.offering_id && dep.row.enabled !== false;
    }).map(function(dep) {
      return dep.row.wire_model_id;
    }));
    out.push({
      offering: offering,
      offeringIndex: item.index,
      canonical: canonical,
      wireIds: wireIds,
      capabilities: (offering.capabilities || []).slice(),
    });
  });
  return out;
}

/** The wire-id pool the matrix renders/probes for one row-group. */
function _matrixRowPool(entry) {
  return entry.wireIds.length ? entry.wireIds : [entry.canonical];
}

/** THE logical-header judgment: the canonical model id is a PURE preset
 *  identity when the offering HAS wire ids but none of them IS the
 *  canonical id — it never goes on the wire, so it gets a header row
 *  (global toggle + count, no per-credential cells). When the canonical id
 *  is in the pool it is a genuine wire id and renders as the root wire row
 *  — one row per id, never two. */
function _matrixIsPureLogical(entry) {
  return entry.wireIds.length > 0 && entry.wireIds.indexOf(entry.canonical) < 0;
}

/** Cell state: is the offering's ModelRef in this credential's
 *  authorization allow-list? The grant is per (credential × model), so
 *  every wire row of one offering shares the same state. */
function _matrixCellOn(credentialRow, entry) {
  var grants = (credentialRow.authorization && credentialRow.authorization.models) || [];
  var creator = String(entry.offering.model.creator_id || '');
  var modelId = String(entry.offering.model.model_id || '');
  for (var i = 0; i < grants.length; i++) {
    if (String(grants[i].creator_id || '') === creator &&
        String(grants[i].model_id || '') === modelId) return true;
  }
  return false;
}

// ── Render ──────────────────────────────────────────────────────────────

/** Build the full access-matrix table for a provider. */
function _renderAccessMatrix(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return '';
  var credentials = _matrixCredentials(context);
  var rows = _matrixModelRows(context);

  if (credentials.length === 0) {
    return '<div class="stg-matrix-empty">' + escapeHtml(t('settings.matrixNoKeys')) + '</div>';
  }
  if (rows.length === 0) {
    return '<div class="stg-matrix-empty">' + escapeHtml(t('settings.noModels')) + '</div>';
  }

  // Lazily re-attach to a persisted/running probe the first time we render
  // this provider's matrix in a session.
  if (!_stgMatrixProbeAttached[providerId]) {
    _stgMatrixProbeAttached[providerId] = true;
    setTimeout(function() { _resumeMatrixProbe(providerId); }, 0);
  }

  var probe = _stgMatrixProbe[providerId] || {};
  var running = (probe.status === 'running');
  var hasResults = probe.cells && Object.keys(probe.cells).length > 0;
  var recommendCount = (probe.summary && probe.summary.disable) || 0;

  var statusTxt = '';
  if (running) {
    statusTxt = t('settings.matrixProbing') +
      (probe.total ? ' (' + (probe.done_count || 0) + '/' + probe.total + ')' : '');
  } else if (probe.status === 'error') {
    var probeError = (typeof errorEnvelopeMessage === 'function')
      ? errorEnvelopeMessage(probe.error) : String(probe.error || '');
    statusTxt = t('settings.matrixProbeFailed') + (probeError ? ': ' + probeError : '');
  } else if (hasResults) {
    statusTxt = (probe.summary.ok || 0) + ' ' + t('settings.matrixOkCount') +
      ' · ' + recommendCount + ' ' + t('settings.matrixFlaggedCount') +
      ((probe.summary.skipped || 0) > 0
        ? ' · ' + probe.summary.skipped + ' ' + t('settings.matrixSkippedCount')
        : '');
  }

  var html = '<div class="stg-matrix" data-provider-id="' + escapeHtml(providerId) + '">' +
    '<div class="stg-matrix-toolbar">' +
      '<div class="stg-matrix-legend">' +
        '<span class="stg-mx-leg on"><span class="stg-mx-dot"></span>' + escapeHtml(t('settings.matrixLegendOn')) + '</span>' +
        '<span class="stg-mx-leg off"><span class="stg-mx-dot"></span>' + escapeHtml(t('settings.matrixLegendOff')) + '</span>' +
      '</div>' +
      '<div class="stg-matrix-tools">' +
        (hasResults && recommendCount > 0 && !running
          ? '<button type="button" class="stg-btn-add stg-mx-apply" data-provider-id="' + escapeHtml(providerId) + '" data-tofu-action="_applyMatrixRecommendations(this.dataset.providerId)" title="' + escapeHtml(t('settings.matrixApplyHint')) + '">✓ ' + escapeHtml(t('settings.matrixApplyRec')) + ' (' + recommendCount + ')</button>'
          : '') +
        (hasResults && !running ? '<button type="button" class="stg-btn-add" data-provider-id="' + escapeHtml(providerId) + '" data-tofu-action="_clearMatrixProbe(this.dataset.providerId)" title="' + escapeHtml(t('settings.matrixClearProbe')) + '">' + escapeHtml(t('settings.matrixClearProbe')) + '</button>' : '') +
        (running ? '' :
          '<label class="stg-mx-attempts" title="' + escapeHtml(t('settings.matrixAttemptsHint')) + '">' + escapeHtml(t('settings.matrixAttempts')) +
            '<select data-provider-id="' + escapeHtml(providerId) + '" data-tofu-action-change="_setMatrixAttempts(this.dataset.providerId,this.value)">' +
              [1, 2, 3, 4, 5].map(function(n) {
                var sel = (n === (_stgMatrixAttempts[providerId] || 3)) ? ' selected' : '';
                return '<option value="' + n + '"' + sel + '>×' + n + '</option>';
              }).join('') +
            '</select></label>') +
        '<button type="button" class="stg-btn-add stg-mx-probe' + (running ? ' running' : '') + '"' + (running ? ' disabled' : '') +
          ' data-provider-id="' + escapeHtml(providerId) + '"' +
          ' data-tofu-action="_runMatrixProbe(this.dataset.providerId,' + (hasResults ? 'true' : 'false') + ')" title="' + escapeHtml(t('settings.matrixProbeHint')) + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/></svg> ' +
          escapeHtml(running ? t('settings.matrixProbing') : (hasResults ? t('settings.matrixRetest') : t('settings.matrixProbe'))) + '</button>' +
      '</div>' +
    '</div>' +
    (statusTxt ? '<div class="stg-mx-status' + (running ? ' running' : (probe.status === 'error' ? ' error' : '')) + '">' + escapeHtml(statusTxt) + '</div>' : '') +
    '<div class="stg-matrix-scroll"><table class="stg-matrix-table"><thead><tr>' +
      '<th class="stg-mx-corner">' + escapeHtml(t('settings.matrixModelCol')) + '</th>';

  for (var ci = 0; ci < credentials.length; ci++) {
    var cred = credentials[ci].row;
    html += '<th class="stg-mx-keyhead" data-key-idx="' + ci + '">' +
      '<span class="stg-mx-credname">' + escapeHtml(t('settings.matrixCredentialN').replace('{n}', String(ci + 1))) + '</span>' +
      '<span class="stg-mx-credkind">' + escapeHtml(cred.kind || 'api_key') + '</span>' +
      '<button type="button" class="stg-mx-zap col' + (_scopeCovers(providerId, 'col', ci) ? ' probing' : '') + '"' +
        (running ? ' disabled' : '') +
        ' data-provider-id="' + escapeHtml(providerId) + '"' +
        ' data-tofu-action="_probeMatrixScope(this.dataset.providerId,\'col\',' + ci + ')" ' +
        'title="' + escapeHtml(t('settings.matrixProbeColHint')) + '">' + _MX_BOLT + '</button>' +
    '</th>';
  }
  html += '</tr></thead><tbody>';

  for (var ri = 0; ri < rows.length; ri++) {
    var entry = rows[ri];
    var pool = _matrixRowPool(entry);
    var groupOpen = pool.length > 1; // only bracket offerings that HAVE a pool
    if (_matrixIsPureLogical(entry)) {
      html += _renderMatrixRow(providerId, entry, entry.canonical, -1, pool.length, credentials, groupOpen);
      for (var li = 0; li < pool.length; li++) {
        html += _renderMatrixRow(providerId, entry, pool[li], li + 1, pool.length, credentials, groupOpen);
      }
    } else {
      for (var wi = 0; wi < pool.length; wi++) {
        html += _renderMatrixRow(providerId, entry, pool[wi], wi, pool.length, credentials, groupOpen);
      }
    }
  }
  html += '</tbody></table></div></div>';
  return html;
}

/** Render one matrix row. Two kinds:
 *   - LOGICAL HEADER (``rowPos === -1``): the preset-facing canonical id of
 *     an offering whose wire pool never carries it. It carries the offering
 *     toggle and the wire-id count, but NO per-credential cells — the id is
 *     never sent on the wire, so there is no (credential × id) pair to
 *     grant, deny, or probe.
 *   - WIRE ROW (``rowPos >= 0``): one concrete upstream id across
 *     credentials. ``rowPos`` is the 1-based index under a logical header,
 *     or 0 = root for a legacy-shape offering (canonical id IS a wire id). */
function _renderMatrixRow(providerId, entry, id, rowPos, rowCount, credentials, grouped) {
  var isLogicalHead = (rowPos === -1);
  var isAlias = rowPos > 0;
  var underHead = _matrixIsPureLogical(entry);
  var isLastInGroup = underHead ? (rowPos === rowCount) : (rowPos === rowCount - 1);
  var globallyOff = (entry.offering.enabled === false);
  var brand = (typeof _modelBrand === 'function')
    ? _modelBrand(id, entry.offering && entry.offering.model && entry.offering.model.creator_id)
    : ((typeof _detectBrand === 'function') ? _detectBrand(id) : '');
  var brandSvg = (typeof _brandSvg === 'function') ? _brandSvg(brand, 14) : '';

  // Row-scope probe button: probes exactly this wire id across every credential.
  var _rowProbe = _stgMatrixProbe[providerId] || {};
  var _rowRunning = (_rowProbe.status === 'running');
  var rowProbeBtn = isLogicalHead ? '' : '<button type="button" class="stg-mx-zap row' +
      (_scopeCovers(providerId, 'row', null, id) ? ' probing' : '') + '"' +
    (_rowRunning ? ' disabled' : '') +
    ' data-provider-id="' + escapeHtml(providerId) + '"' +
    ' data-tofu-action="event.stopPropagation();_probeMatrixScope(this.dataset.providerId' +
      ',\'row\',' + JSON.stringify(id).replace(/"/g, '&quot;') + ')" ' +
    'title="' + escapeHtml(t('settings.matrixProbeRowHint')) + '">' + _MX_BOLT + '</button>';

  var labelCell;
  if (isAlias) {
    var connector = isLastInGroup ? '└' : '├';
    // A distinct accent color per wire-id index, cycled, so two ids of the
    // same offering never look alike at a glance.
    var hue = (entry.offeringIndex * 47 + rowPos * 71) % 360;
    labelCell = '<td class="stg-mx-model alias' + (globallyOff ? ' model-off' : '') +
        (isLastInGroup ? ' last' : '') + '" style="--alias-hue:' + hue + '">' +
      '<span class="stg-mx-tree">' + connector + '</span>' +
      '<span class="stg-mx-aliasidx">' + rowPos + '</span>' +
      '<span class="stg-mx-brand">' + brandSvg + '</span>' +
      '<span class="stg-mx-mid alias-id" title="' + escapeHtml(id) + '">' + escapeHtml(id) + '</span>' +
      rowProbeBtn +
    '</td>';
  } else {
    var countBadge = rowCount > 0
      ? '<span class="stg-mx-aliascount" title="' + escapeHtml(t('settings.matrixAliasCountHint')) + '">' +
          rowCount + ' ' + escapeHtml(rowCount === 1 ? t('settings.matrixIdOne') : t('settings.matrixIdMany')) + '</span>'
      : '';
    var presetBadge = isLogicalHead
      ? '<span class="stg-mx-preset" title="' + escapeHtml(t('settings.matrixPresetHint')) + '">' +
          escapeHtml(t('settings.matrixPresetBadge')) + '</span>'
      : '';
    labelCell = '<td class="stg-mx-model root' + (isLogicalHead ? ' logical' : '') +
        (globallyOff ? ' model-off' : '') + '">' +
      '<label class="stg-toggle stg-mx-gtoggle" title="' + escapeHtml(t('settings.matrixGlobalToggle')) + '" data-tofu-action="event.stopPropagation();">' +
        '<input type="checkbox"' + (globallyOff ? '' : ' checked') +
          ' data-tofu-action-change="_setModelRoutingCollectionField(\'offerings\',' +
          entry.offeringIndex + ',\'enabled\',this.checked,\'boolean\')">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
      '</label>' +
      '<span class="stg-mx-brand">' + brandSvg + '</span>' +
      '<span class="stg-mx-mid" title="' + escapeHtml(id || '') + '">' + escapeHtml(id || '(unnamed)') + '</span>' +
      presetBadge +
      countBadge +
      rowProbeBtn +
    '</td>';
  }

  var cls = 'stg-mx-row' + (globallyOff ? ' model-off' : '') +
    (isAlias ? ' is-alias' : ' is-root') + (isLogicalHead ? ' is-logical' : '') +
    (grouped ? ' grouped' : '') + (isLastInGroup && grouped ? ' group-end' : '');
  var row = '<tr class="' + cls + '" data-offering="' + entry.offeringIndex + '" data-id="' + escapeHtml(id) + '">' + labelCell;
  if (isLogicalHead) {
    for (var hk = 0; hk < credentials.length; hk++) {
      row += '<td class="stg-mx-cell logical"></td>';
    }
  } else {
    for (var k = 0; k < credentials.length; k++) {
      row += _renderMatrixCell(providerId, entry, k, credentials[k].row, id);
    }
  }
  row += '</tr>';
  return row;
}

/** Probe status → {glyph, cls, label} for the cell health pip. */
function _probeStatusInfo(status) {
  switch (status) {
    case 'ok':           return { glyph: '✓', cls: 'ok',     label: t('settings.probeOk') };
    case 'bad_request':  return { glyph: '400', cls: 'err',  label: t('settings.probeBadRequest') };
    case 'invalid_response': return { glyph: '∅', cls: 'err', label: t('settings.probeInvalidResponse') };
    case 'rate_limited': return { glyph: '429', cls: 'rate', label: t('settings.probeRateLimited') };
    case 'unauthorized': return { glyph: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><circle cx="12" cy="12" r="10"/><path d="M4.929 4.929 19.07 19.071"/></svg>', cls: 'unauth', label: t('settings.probeUnauthorized') };
    case 'not_found':    return { glyph: '∅', cls: 'nf',     label: t('settings.probeNotFound') };
    case 'unavailable':  return { glyph: '⚠', cls: 'down',   label: t('settings.probeUnavailable') };
    case 'skipped':      return { glyph: 'N/A', cls: 'skip', label: t('settings.probeSkipped') };
    case 'unverified':   return { glyph: '?', cls: 'skip',   label: t('settings.probeUnverified') };
    case 'not_logged_in': return { glyph: '↪', cls: 'skip',  label: t('settings.probeNotLoggedIn') };
    default:             return { glyph: '✕', cls: 'err',    label: t('settings.probeError') };
  }
}

/** Render one matrix cell (a single (credential × wire id) access view).
 *  The dot reflects the credential's authorization grant for the row's
 *  offering (per-model, so all wire rows of an offering share it); the pip
 *  is the exact (credential, wire id) probe verdict. */
function _renderMatrixCell(providerId, entry, credIdx, credentialRow, id) {
  var on = _matrixCellOn(credentialRow, entry);

  // Probe-status pip: exact (credential, wire id) result.
  var probe = _stgMatrixProbe[providerId] || {};
  var pcells = probe.cells || {};
  var running = (probe.status === 'running');
  var pip = '';
  var cellProbe = '';
  var cellOnly = '\'cell\',' + credIdx + ',' +
    JSON.stringify(id).replace(/"/g, '&quot;');
  var r = pcells[_probeCellKey(credIdx, id)];
  if (_scopeCovers(providerId, 'cell', credIdx, id)) {
    // This cell is being probed right now — spin a bolt in place of the pip.
    cellProbe = '<span class="stg-mx-zap cell probing" title="' +
      escapeHtml(t('settings.matrixProbing')) + '">' + _MX_BOLT + '</span>';
  } else if (r) {
    var info = _probeStatusInfo(r.status);
    // The pip doubles as the re-probe trigger for its own cell.
    pip = '<span class="stg-mx-probe-pip ' + info.cls + ' clickable" role="button" ' +
      'title="' + escapeHtml(info.label + (r.detail ? ' — ' + r.detail : '') +
        '\n' + t('settings.matrixProbeCellHint')) + '" ' +
      'data-provider-id="' + escapeHtml(providerId) + '"' +
      'data-tofu-action="event.stopPropagation();_probeMatrixScope(this.dataset.providerId,' + cellOnly + ')">' +
      info.glyph + '</span>';
  } else {
    // Never probed — hover reveals a single-cell probe button (bottom-left).
    cellProbe = '<button type="button" class="stg-mx-zap cell"' + (running ? ' disabled' : '') +
      ' data-provider-id="' + escapeHtml(providerId) + '"' +
      ' data-tofu-action="event.stopPropagation();_probeMatrixScope(this.dataset.providerId,' + cellOnly + ')" ' +
      'title="' + escapeHtml(t('settings.matrixProbeCellHint')) + '">' + _MX_BOLT + '</button>';
  }

  return '<td class="stg-mx-cell' + (on ? ' on' : ' off') +
      '" data-offering="' + entry.offeringIndex + '" data-key-idx="' + credIdx + '" data-id="' + escapeHtml(id) + '">' +
    '<button type="button" class="stg-mx-toggle" ' +
      'data-provider-id="' + escapeHtml(providerId) + '"' +
      ' data-tofu-action="_toggleMatrixAccess(this.dataset.providerId,' + entry.offeringIndex + ',' + credIdx + ')" ' +
      'title="' + escapeHtml(on ? t('settings.matrixClickDisable') : t('settings.matrixClickEnable')) + '">' +
      '<span class="stg-mx-dot"></span>' +
    '</button>' +
    pip +
    cellProbe +
  '</td>';
}

// ── Interactions ──────────────────────────────────────────────────────

/** Toggle a single (credential × model) grant — add/remove the offering's
 *  ModelRef in the credential's authorization allow-list. The change lives
 *  in ``_stgModelRouting`` and is persisted by the settings 保存 flow. */
function _toggleMatrixAccess(providerId, offeringIndex, credIdx) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var offering = (_stgModelRouting.offerings || [])[offeringIndex];
  var credential = (_stgModelRouting.credentials || [])[credIdx];
  if (!offering || !credential || !offering.model) return;
  if (!credential.authorization) credential.authorization = { connection_ids: [], models: [] };
  var grants = credential.authorization.models || [];
  var creator = String(offering.model.creator_id || '');
  var modelId = String(offering.model.model_id || '');
  var kept = grants.filter(function(ref) {
    return !(String(ref.creator_id || '') === creator && String(ref.model_id || '') === modelId);
  });
  if (kept.length === grants.length) {
    kept.push(JSON.parse(JSON.stringify(offering.model)));
  }
  credential.authorization.models = kept;
  _rerenderMatrix(providerId);
}

/** Re-render the providers tab; open cards and the matrix view state are
 *  preserved by the tab renderer + ``_stgMatrixOpen``. */
function _rerenderMatrix(providerId) {
  _renderProvidersTab();
  if (typeof _fitMatrixPanelWidth === 'function') _fitMatrixPanelWidth();
}

// ── Background probe: start / poll / resume / apply ───────────────────────

/** True when the offering has no chat surface (image_gen / embedding /
 *  transcription). Reads the shared taxonomy helper when available, else
 *  the same hardcoded fallback set it ships with. */
function _matrixModelIsNonChat(entry) {
  if (!entry) return false;
  if (typeof runtimeScope.isChatModel === 'function') {
    return !runtimeScope.isChatModel({ capabilities: entry.capabilities });
  }
  var nonChat = ['image_gen', 'embedding', 'transcription', 'tts'];
  for (var i = 0; i < entry.capabilities.length; i++) {
    if (nonChat.indexOf(entry.capabilities[i]) >= 0) return true;
  }
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
function _reconcileProbeNonChat(providerId) {
  var probe = _stgMatrixProbe[providerId];
  var context = _modelRoutingProviderContext(providerId);
  if (!probe || !probe.cells || !context) return;
  var byRoot = {};
  _matrixModelRows(context).forEach(function(entry) { byRoot[entry.canonical] = entry; });
  var changed = false;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c || c.status === 'ok' || c.status === 'skipped') return;
    if (_isFreshModalityVerdict(c)) return;   // real modality verdict — keep
    var entry = byRoot[c.root_model_id];
    if (!_matrixModelIsNonChat(entry)) return;
    c.detail = 'stale chat-probe verdict discarded (non-chat model) — re-run ' +
               'the probe to test it via its real endpoint (was ' + c.status +
               (c.detail ? ': ' + c.detail : '') + ')';
    c.status = 'skipped';
    c.recommend_disable = false;
    changed = true;
  });
  if (changed) _mxRecountSummary(probe);
}

/** Enforce the strict proof contract in the browser as well as the server.
 * This is load-bearing during rolling deploys and for old persisted cache:
 * an old backend can still send ``ok + HTTP 400`` and the UI must never turn
 * that contradiction into a green pip. No-ops when the typed status helper
 * is absent (the server-side snapshot normalization then owns the rule). */
function _reconcileProbeProofContract(providerId) {
  var probe = _stgMatrixProbe[providerId];
  if (!probe || !probe.cells || typeof runtimeScope.effectiveProbeStatus !== 'function') return;
  var changed = false;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c) return;
    var effective = runtimeScope.effectiveProbeStatus(c, probe.probe_schema_version || 1);
    if (effective === c.status) return;
    var previous = c.status;
    c.status = effective;
    c.recommend_disable = (effective === 'bad_request' ||
      effective === 'invalid_response' || effective === 'error');
    if (previous === 'ok' && effective === 'bad_request' &&
        String(c.detail || '').indexOf('false-positive corrected') < 0) {
      c.detail = (c.detail || 'HTTP 400') +
        ' — legacy false-positive corrected; provider rejected the request';
    } else if (previous === 'ok' && effective === 'unverified') {
      c.detail = (c.detail || 'HTTP 2xx') +
        ' — legacy result did not validate generated content; re-test required';
    }
    changed = true;
  });
  if (changed) _mxRecountSummary(probe);
}

/** Recompute probe.summary over ALL current cells (mirrors the backend's
 *  ``_recount_summary``): shared by the non-chat reconcile and the
 *  stale-cell prune, which must never drift apart. */
function _mxRecountSummary(probe) {
  var ok = 0, disable = 0, skipped = 0, neutral = 0, failed = 0;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c) return;
    if (c.status === 'ok') ok++;
    else if (c.status === 'skipped') skipped++;
    else if (c.status === 'unverified' || c.status === 'not_logged_in') neutral++;
    else failed++;
    if (c.recommend_disable) disable++;
  });
  probe.summary = { ok: ok, disable: disable, skipped: skipped,
                    neutral: neutral, failed: failed };
}

/** Drop probe cells whose (credential × wire id) no longer exists in the
 *  provider's CURRENT grid. A persisted snapshot outlives the config it
 *  measured: a deleted offering/credential/deployment leaves cells with no
 *  row at all, and rendering such a ghost makes a stale 'reachable ✓' look
 *  like real coverage. Mirrors the backend's scoped-probe seed prune. */
function _pruneProbeCellsToGrid(providerId) {
  var probe = _stgMatrixProbe[providerId];
  var context = _modelRoutingProviderContext(providerId);
  if (!probe || !probe.cells || !context) return;
  var credentials = _matrixCredentials(context);
  if (!credentials.length) return; // no columns → no grid; nothing to validate against
  var valid = {};
  for (var ci = 0; ci < credentials.length; ci++) {
    _matrixModelRows(context).forEach(function(entry) {
      var pool = _matrixRowPool(entry);
      for (var ri = 0; ri < pool.length; ri++) valid[_probeCellKey(ci, pool[ri])] = true;
    });
  }
  var changed = false;
  Object.keys(probe.cells).forEach(function(k) {
    if (!valid[k]) { delete probe.cells[k]; changed = true; }
  });
  if (changed) _mxRecountSummary(probe);
}

/** Normalise a backend snapshot into the local _stgMatrixProbe entry.
 *  Returns true when the snapshot carried real probe data. */
function _ingestProbeSnapshot(providerId, snap) {
  if (!snap || snap.status === 'none') return false;
  _stgMatrixProbe[providerId] = {
    probe_schema_version: snap.probe_schema_version || 1,
    status: snap.status || 'done',
    cells: snap.cells || {},
    summary: snap.summary || { ok: 0, disable: 0 },
    total: snap.total || 0,
    done_count: snap.done_count || (snap.cells ? Object.keys(snap.cells).length : 0),
    attempts: snap.attempts || null,
    error: snap.error || null,
  };
  // Reflect the server's attempts setting in the selector on resume.
  if (snap.attempts && !_stgMatrixAttempts[providerId]) _stgMatrixAttempts[providerId] = snap.attempts;
  if (_stgMatrixProbe[providerId].status !== 'running') delete _stgMatrixProbeScope[providerId];
  _reconcileProbeProofContract(providerId);
  _pruneProbeCellsToGrid(providerId);
  _reconcileProbeNonChat(providerId);
  return true;
}

/** Start (or, when not forcing, resume) a background probe for a provider.
 *  ``only`` (optional) scopes the run to rows/columns/cells:
 *  ``{key_idxs?: [int], model_ids?: [string]}`` — the backend probes exactly
 *  those cells and MERGES the verdicts into the persisted snapshot. */
function _runMatrixProbe(providerId, force, only) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var existing = _stgMatrixProbe[providerId];
  if (existing && existing.status === 'running') return; // one probe per provider at a time
  if (!_matrixCredentials(context).length || !_matrixModelRows(context).length) {
    if (typeof showToast === 'function') showToast(t('settings.matrixNothingToProbe'), 'warning');
    return;
  }

  _stgMatrixProbeScope[providerId] = only || null;
  _stgMatrixProbe[providerId] = { status: 'running', cells: (force ? {} : ((_stgMatrixProbe[providerId] || {}).cells || {})),
    summary: { ok: 0, disable: 0 }, total: 0, done_count: 0, error: null };
  _rerenderMatrix(providerId);

  var body = {
    attempts: _stgMatrixAttempts[providerId] || 3,
    // A scoped probe always refreshes its cells server-side (the cache-return
    // shortcut is skipped for it), so force stays a FULL-GRID-only flag.
    force: !!force && !only,
  };
  if (only) body.only = only;

  Api.modelRouting.probeCellsStart(providerId, body).then(function(snap) {
    if (!_ingestProbeSnapshot(providerId, snap)) {
      _stgMatrixProbe[providerId] = { status: 'error', cells: {}, summary: { ok: 0, disable: 0 }, error: 'start failed' };
      if (typeof showToast === 'function') showToast(t('settings.matrixProbeFailed'), 'error');
      _rerenderMatrix(providerId);
      return;
    }
    _rerenderMatrix(providerId);
    if (_stgMatrixProbe[providerId].status === 'running') _pollMatrixProbe(providerId);
  }).catch(function(e) {
    _stgMatrixProbe[providerId] = { status: 'error', cells: {}, summary: { ok: 0, disable: 0 }, error: String(e && e.message || e) };
    if (typeof showToast === 'function') showToast(t('settings.matrixProbeFailed') + ': ' + (e && e.message || e), 'error');
    _rerenderMatrix(providerId);
  });
}

/** Poll a running probe until it reaches a terminal state. */
function _pollMatrixProbe(providerId) {
  if (_stgMatrixProbeTimers[providerId]) clearTimeout(_stgMatrixProbeTimers[providerId]);
  _stgMatrixProbeTimers[providerId] = setTimeout(function tick() {
    // Settings closed → stop polling; _resumeMatrixProbe re-attaches on reopen.
    if (!document.getElementById('stgProviderList')) {
      delete _stgMatrixProbeTimers[providerId];
      _stgMatrixProbeAttached[providerId] = false;
      return;
    }
    Api.modelRouting.probeCellsStatus(providerId).then(function(snap) {
      _ingestProbeSnapshot(providerId, snap);
      _rerenderMatrix(providerId);
      if (snap && snap.status === 'running') {
        _stgMatrixProbeTimers[providerId] = setTimeout(tick, 1500);
      } else {
        delete _stgMatrixProbeTimers[providerId];
      }
    }).catch(function() {
      _stgMatrixProbeTimers[providerId] = setTimeout(tick, 3000);
    });
  }, 1500);
}

/** Re-attach to a persisted/running probe on (re)opening the matrix. */
function _resumeMatrixProbe(providerId) {
  // Don't clobber a live local run.
  if (_stgMatrixProbe[providerId] && _stgMatrixProbe[providerId].status === 'running'
      && _stgMatrixProbeTimers[providerId]) return;
  Api.modelRouting.probeCellsStatus(providerId).then(function(snap) {
    if (_ingestProbeSnapshot(providerId, snap)) {
      _rerenderMatrix(providerId);
      if (_stgMatrixProbe[providerId].status === 'running') _pollMatrixProbe(providerId);
    }
  }).catch(function() { /* best-effort resume */ });
}

/** Apply the probe's recommended disables: remove every flagged
 *  (credential × model) grant from the credential's authorization
 *  allow-list. */
function _applyMatrixRecommendations(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  var probe = _stgMatrixProbe[providerId];
  if (!context || !probe || !probe.cells) return;

  // Map canonical model_id → row entry for quick lookup.
  var byRoot = {};
  _matrixModelRows(context).forEach(function(entry) { byRoot[entry.canonical] = entry; });

  var applied = 0;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c || !c.recommend_disable) return;
    var entry = byRoot[c.root_model_id];
    if (!entry) return;
    // A non-chat model may only lose its grant on a verdict from its OWN
    // modality probe (probe_surface = image/transcription/embedding) —
    // never on a stale chat-completions verdict that cannot speak for its
    // real endpoint. A fresh modality not_found MUST be applicable:
    // exposing dead models is exactly what the per-modality probe exists for.
    if (_matrixModelIsNonChat(entry) && !_isFreshModalityVerdict(c)) return;
    var item = context.credentials[c.key_idx];
    if (!item) return;
    var credential = item.row;
    if (!_matrixCellOn(credential, entry)) return;
    var grants = (credential.authorization && credential.authorization.models) || [];
    var creator = String(entry.offering.model.creator_id || '');
    var modelId = String(entry.offering.model.model_id || '');
    credential.authorization.models = grants.filter(function(ref) {
      return !(String(ref.creator_id || '') === creator && String(ref.model_id || '') === modelId);
    });
    applied++;
  });

  if (typeof showToast === 'function') {
    showToast(applied > 0
      ? t('settings.matrixApplied').replace('{n}', String(applied))
      : t('settings.matrixNothingApplied'), applied > 0 ? 'success' : 'info');
  }
  _rerenderMatrix(providerId);
}

/** Hide probe results locally for this session (disk snapshot is kept;
 *  re-opening Settings re-attaches via _resumeMatrixProbe). */
function _clearMatrixProbe(providerId) {
  if (_stgMatrixProbeTimers[providerId]) {
    clearTimeout(_stgMatrixProbeTimers[providerId]);
    delete _stgMatrixProbeTimers[providerId];
  }
  delete _stgMatrixProbe[providerId];
  delete _stgMatrixProbeScope[providerId];
  _stgMatrixProbeAttached[providerId] = true; // don't auto-reattach until reopen
  _rerenderMatrix(providerId);
}
