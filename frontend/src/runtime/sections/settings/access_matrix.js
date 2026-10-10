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
   (POST /api/v4/providers/<id>/probe-cells/start) that resolves plaintext
   keys from the owner-scoped secret store — the browser never sees them —
   and sends a tiny request to EVERY (credential × wire id) pair, granted
   or not: discovering reachable pairs the allow-list does not grant yet
   is the matrix's whole point on gateways like Meituan. Progress is
   persisted server-side under data/config/probe_cache/, so closing
   Settings (or restarting the server) never loses it — the UI re-attaches
   by provider id and keeps polling. Only "Retest" (force) discards the
   saved result and starts over. Probe updates never edit routing state.
   Explicitly applying recommendations only removes flagged
   (credential × model) grants from the allow-list.

   This file is concatenated by Vite's module graph — symbols share the
   same window scope as every other runtime section. No imports needed.
   ═══════════════════════════════════════════════════════════════════ */

/** Per-provider management-surface state (search text + status filter
 *  chip), keyed by provider_id. The matrix is this provider's always-on
 *  model-supply manager. It is deliberately provider-scoped: the global,
 *  read-only Creator/Model registry is the 模型 (Models) tab
 *  (settings/model_catalog_tab.js + frontend/src/features/model-catalog/),
 *  which the matrix never renders. */
var _stgMatrixQuery = {};
var _stgMatrixFilter = {};

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


/** Trash glyph for the remove-offering row action. */
var _MX_TRASH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>';

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

// ── v2 data derivation ──────────────────────────────────────────────────

/** Update the matrix search text for a provider (input action). */
function _setMatrixQuery(providerId, value) {
  _stgMatrixQuery[providerId] = String(value || '');
  _rerenderMatrix(providerId);
}

/** Switch the exclusive status bucket the matrix lists. */
function _setMatrixFilter(providerId, value) {
  _stgMatrixFilter[providerId] = ['all', 'enabled', 'disabled', 'pending'].indexOf(value) >= 0
    ? value : 'all';
  _rerenderMatrix(providerId);
}

/** The credential columns of the matrix: the access's credentials in
 *  document order — the SAME order the backend's probe plan uses, so cell
 *  key ``<idx>::<wire id>`` aligns between render and probe. */
function _matrixCredentials(context) {
  return context ? context.credentials : [];
}

/** One matrix row-group per offering (confirmed AND pending — the matrix
 *  doubles as the supply manager):
 *  ``{ offering, offeringIndex, canonical, pending, retired, releaseDate,
 *      pricing, catalogPrice, wireIds, deploymentByWire, capabilities }``.
 *  ``wireIds`` contains every request name in document order, including
 *  legacy enabled:false inputs. All inherit the Offering and ModelRef grants.
 *  ``deploymentByWire`` maps ids to GLOBAL deployment indices for deletion.
 *  An offering without deployments has no request names. */
function _matrixModelRows(context) {
  var out = [];
  if (!context) return out;
  context.offerings.forEach(function(item) {
    var offering = item.row;
    var pending = offering.identity_state === 'pending_identity' || !offering.model;
    var canonical = pending
      ? String(offering.pending_model_id || offering.offering_id || '').trim()
      : String(offering.model.model_id || '').trim();
    if (!canonical) return;
    var wireIds = [], deploymentByWire = {};
    context.deployments.forEach(function(dep) {
      if (dep.row.offering_id !== offering.offering_id) return;
      var wire = String(dep.row.wire_model_id || '').trim();
      if (!wire) return;
      if (wireIds.indexOf(wire) < 0) {
        wireIds.push(wire);
        deploymentByWire[wire] = dep.index;
      }
    });
    var modelKey = pending
      ? '' : (String(offering.model.creator_id || '') + '::' + canonical);
    out.push({
      offering: offering,
      offeringIndex: item.index,
      canonical: canonical,
      pending: pending,
      retired: !pending && context.modelLifecycles[modelKey] === 'retired',
      releaseDate: pending ? '' : (context.modelReleaseDates[modelKey] || ''),
      pricing: pending ? null
        : (offering.actual_pricing || context.modelListPricing[modelKey] || null),
      catalogPrice: !pending && !offering.actual_pricing &&
        !!context.modelListPricing[modelKey],
      wireIds: wireIds,
      deploymentByWire: deploymentByWire,
      capabilities: (offering.capabilities || []).slice(),
    });
  });
  return out;
}

/** Exclusive-bucket status filter + substring match over every id the
 *  group displays (canonical + all request names). */
function _matrixGroupMatches(entry, query, filter) {
  if (filter === 'enabled' && (entry.pending || entry.offering.enabled === false)) return false;
  if (filter === 'disabled' && (entry.pending || entry.offering.enabled !== false)) return false;
  if (filter === 'pending' && !entry.pending) return false;
  if (!query) return true;
  var hay = [entry.canonical].concat(entry.wireIds)
    .join(' ').toLowerCase();
  return hay.indexOf(query) >= 0;
}

/** The wire-id pool the matrix renders/probes for one row-group. */
function _matrixRowPool(entry) {
  return entry.wireIds;
}

/** Model identity always has its own header; even an identical wire name is
 * an independently editable/removable deployment beneath it. */
function _matrixIsPureLogical(entry) {
  return true;
}

/** The model grant (ModelRef + optional wire exclusions) this credential
 *  holds for an entry, or null. */
function _matrixGrant(credentialRow, entry) {
  if (!entry.offering || !entry.offering.model) return null; // pending: no ModelRef exists
  var grants = (credentialRow.authorization && credentialRow.authorization.models) || [];
  var creator = String(entry.offering.model.creator_id || '');
  var modelId = String(entry.offering.model.model_id || '');
  for (var i = 0; i < grants.length; i++) {
    if (String(grants[i].creator_id || '') === creator &&
        String(grants[i].model_id || '') === modelId) return grants[i];
  }
  return null;
}

/** Cell state for one (credential × wire id): the model grant must exist
 *  AND this wire id must not sit in its exclusion list. When ``id`` is
 *  omitted (header aggregation), the grant alone decides. */
function _matrixCellOn(credentialRow, entry, id) {
  var grant = _matrixGrant(credentialRow, entry);
  if (!grant) return false;
  if (id === undefined || id === null) return true;
  var excluded = grant.excluded_wire_model_ids || [];
  return excluded.indexOf(String(id)) < 0;
}

/** Exclusive-bucket filter chip with a live count. */
function _mxFilterChip(providerId, active, value, label, count) {
  var on = active === value;
  return '<button type="button" class="stg-v2-filter-chip' + (on ? ' active' : '') +
    '" aria-pressed="' + (on ? 'true' : 'false') + '" data-provider-id="' + escapeHtml(providerId) +
    '" data-tofu-action="_setMatrixFilter(this.dataset.providerId,\'' + value + '\')">' +
    escapeHtml(label) + ' <em>' + count + '</em></button>';
}

/** 待确认 / 已下线 state chip for a root row (empty string when neither). */
function _mxStateChips(entry) {
  if (entry.pending) {
    return '<span class="stg-mx-chip pending" title="' + escapeHtml(t('settings.matrixPendingHint')) + '">' +
      escapeHtml(t('settings.matrixPendingChip')) + '</span>';
  }
  if (entry.retired) {
    return '<span class="stg-mx-chip retired" title="' + escapeHtml(t('settings.matrixRetiredHint')) + '">' +
      escapeHtml(t('settings.matrixRetiredChip')) + '</span>';
  }
  return '';
}

/** Compact meta line under a root row's id: capability chips + context
 *  window + release date + pricing (Model prices are labeled as reference).
 *  Pending rows render nothing — there is no catalog identity to describe. */
function _mxMetaHtml(entry) {
  if (entry.pending) return '';
  var capLabels = (typeof _MODEL_CAP_LABELS === 'object' && _MODEL_CAP_LABELS) || {};
  var caps = entry.capabilities.map(function(cap) {
    return '<span class="stg-cap ' + escapeHtml(cap) + '" title="' + escapeHtml(cap) + '">' +
      escapeHtml(capLabels[cap] || cap) + '</span>';
  }).join('');
  var bits = '';
  if (entry.offering.context_window) {
    var ctxLabel = (typeof _modelRoutingContextLabel === 'function')
      ? _modelRoutingContextLabel(entry.offering.context_window)
      : String(entry.offering.context_window);
    var contextTitle = (entry.offering.inherited_model_fields || []).indexOf('context_window') >= 0
      ? t('settings.matrixContextInheritedHint') : '';
    bits += '<span class="stg-mx-meta-bit" title="' + escapeHtml(contextTitle) + '">' + escapeHtml(t('settings.matrixContextLabel')) + ' ' +
      escapeHtml(ctxLabel) + '</span>';
  }
  if (entry.releaseDate) {
    bits += '<span class="stg-mx-meta-bit">' + escapeHtml(t('settings.matrixReleaseLabel')) + ' ' +
      escapeHtml(entry.releaseDate) + '</span>';
  }
  if (entry.pricing) {
    var priceTitle = entry.catalogPrice ? t('settings.matrixReferencePriceHint') : t('settings.matrixPriceTitle');
    bits += '<span class="stg-mx-meta-bit stg-mx-price" title="' + escapeHtml(priceTitle) + '">' +
      '<span class="stg-v2-price-cur">' + escapeHtml(entry.pricing.currency || 'USD') + '</span>' +
      '<span class="stg-v2-price-io">' + escapeHtml(t('settings.matrixPriceIn')) + '</span>' +
      escapeHtml(String(Number(entry.pricing.input || 0))) +
      '<span class="stg-v2-price-sep">/</span>' +
      '<span class="stg-v2-price-io">' + escapeHtml(t('settings.matrixPriceOut')) + '</span>' +
      escapeHtml(String(Number(entry.pricing.output || 0))) +
      (entry.catalogPrice
        ? '<span class="stg-v2-price-tag">' + escapeHtml(t('settings.matrixCatalogPrice')) + '</span>'
        : '') +
    '</span>';
  }
  if (!caps && !bits) return '';
  return '<span class="stg-mx-meta">' + caps + bits + '</span>';
}

// ── Render ──────────────────────────────────────────────────────────────

/** Build the full access-matrix table for a provider — the provider's
 *  always-on model-supply manager: search + status buckets + add-model on
 *  the manage row, probe tools on the second toolbar row. */
function _renderAccessMatrix(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return '';
  var credentials = _matrixCredentials(context);
  var allRows = _matrixModelRows(context);

  // Lazily re-attach to a persisted/running probe the first time we render
  // this provider's matrix in a session.
  if (!_stgMatrixProbeAttached[providerId]) {
    _stgMatrixProbeAttached[providerId] = true;
    setTimeout(function() { _resumeMatrixProbe(providerId); }, 0);
  }

  // Exclusive status buckets + substring search over every displayed id.
  var counts = { all: allRows.length, enabled: 0, disabled: 0, pending: 0 };
  allRows.forEach(function(entry) {
    if (entry.pending) { counts.pending++; return; }
    if (entry.offering.enabled === false) counts.disabled++;
    else counts.enabled++;
  });
  var query = String(_stgMatrixQuery[providerId] || '');
  var filter = _stgMatrixFilter[providerId] || 'all';
  var q = query.trim().toLowerCase();
  var rows = allRows.filter(function(entry) {
    return _matrixGroupMatches(entry, q, filter);
  }).sort(function(left, right) {
    // Disabled offerings sink below usable ones (pending counts as usable).
    var rank = ((right.offering.enabled === false) ? 0 : 1) -
               ((left.offering.enabled === false) ? 0 : 1);
    if (rank) return rank;
    return left.canonical.localeCompare(right.canonical, undefined,
      { numeric: true, sensitivity: 'base' });
  });

  var probe = _stgMatrixProbe[providerId] || {};
  var running = (probe.status === 'running');
  var hasResults = probe.cells && Object.keys(probe.cells).length > 0;
  var recommendCount = (probe.summary && probe.summary.disable) || 0;
  // The button counts every grant Apply would change — disables AND
  // enablements — derived from the same projection Apply executes, so an
  // enable-only recommendation surfaces the action instead of a dead end.
  var applyCount = _matrixApplyCount(providerId);

  var statusTxt = '';
  if (running) {
    statusTxt = t('settings.matrixProbing') +
      (probe.total ? ' (' + (probe.done_count || 0) + '/' + probe.total + ')' : '');
  } else if (probe.status === 'error') {
    var probeError = (typeof errorEnvelopeMessage === 'function')
      ? errorEnvelopeMessage(probe.error) : String(probe.error || '');
    statusTxt = probe.start_error ? probeError
      : t('settings.matrixProbeFailed') + (probeError ? ': ' + probeError : '');
  } else if (hasResults) {
    statusTxt = (probe.summary.ok || 0) + ' ' + t('settings.matrixOkCount') +
      ' · ' + recommendCount + ' ' + t('settings.matrixFlaggedCount') +
      ((probe.summary.skipped || 0) > 0
        ? ' · ' + probe.summary.skipped + ' ' + t('settings.matrixSkippedCount')
        : '');
  }

  var html = '<div class="stg-matrix" data-provider-id="' + escapeHtml(providerId) + '">' +
    '<div class="stg-mx-manage">' +
      (allRows.length
        ? '<input id="stgMatrixSearch-' + escapeHtml(providerId) + '" class="stg-mx-search" type="search" ' +
          'placeholder="' + escapeHtml(t('settings.matrixSearchPlaceholder')) + '" ' +
          'value="' + escapeHtml(query) + '" data-provider-id="' + escapeHtml(providerId) + '" ' +
          'data-tofu-action-input="_setMatrixQuery(this.dataset.providerId,this.value)">' +
          '<div class="stg-mx-filters" role="group" aria-label="' + escapeHtml(t('settings.matrixFilterAria')) + '">' +
            _mxFilterChip(providerId, filter, 'all', t('settings.matrixFilterAll'), counts.all) +
            _mxFilterChip(providerId, filter, 'enabled', t('settings.matrixFilterEnabled'), counts.enabled) +
            _mxFilterChip(providerId, filter, 'disabled', t('settings.matrixFilterDisabled'), counts.disabled) +
            _mxFilterChip(providerId, filter, 'pending', t('settings.matrixFilterPending'), counts.pending) +
          '</div>'
        : '') +
      '<button type="button" class="stg-btn-add stg-mx-addmodel" data-provider-id="' + escapeHtml(providerId) + '" ' +
        'data-tofu-action="_openProviderModelPicker(this.dataset.providerId)">+ ' +
        escapeHtml(t('settings.matrixAddModel')) + '</button>' +
    '</div>';

  if (!allRows.length) {
    return html + '<p class="stg-mx-empty">' + escapeHtml(t('settings.matrixEmptyModels')) + '</p></div>';
  }

  if (credentials.length) {
    html += '<div class="stg-matrix-toolbar">' +
      '<div class="stg-matrix-legend">' +
        '<span class="stg-mx-leg on"><span class="stg-mx-dot"></span>' + escapeHtml(t('settings.matrixLegendOn')) + '</span>' +
        '<span class="stg-mx-leg off"><span class="stg-mx-dot"></span>' + escapeHtml(t('settings.matrixLegendOff')) + '</span>' +
      '</div>' +
      '<div class="stg-matrix-tools">' +
        (hasResults && applyCount > 0 && !running
          ? '<button type="button" class="stg-btn-add stg-mx-apply" data-provider-id="' + escapeHtml(providerId) + '" data-tofu-action="_applyMatrixRecommendations(this.dataset.providerId)" title="' + escapeHtml(t('settings.matrixApplyHint')) + '">✓ ' + escapeHtml(t('settings.matrixApplyRec')) + ' (' + applyCount + ')</button>'
          : '') +
        (hasResults && !running ? '<button type="button" class="stg-btn-add" data-provider-id="' + escapeHtml(providerId) + '" data-tofu-action="_clearMatrixProbe(this.dataset.providerId)" title="' + escapeHtml(t('settings.matrixClearProbe')) + '">' + escapeHtml(t('settings.matrixClearProbe')) + '</button>' : '') +
        (running ? '' :
          '<label class="stg-mx-attempts" title="' + escapeHtml(t('settings.matrixAttemptsHint')) + '">' + escapeHtml(t('settings.matrixAttempts')) +
            '<select data-tofu-dd data-provider-id="' + escapeHtml(providerId) + '" data-tofu-action-change="_setMatrixAttempts(this.dataset.providerId,this.value)">' +
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
    (statusTxt ? '<div class="stg-mx-status' + (running ? ' running' : (probe.status === 'error' ? ' error' : '')) + '">' + escapeHtml(statusTxt) + '</div>' : '');
  } else {
    html += '<p class="stg-mx-nokeys">' + escapeHtml(t('settings.matrixNoKeysHint')) + '</p>';
  }

  html += '<div class="stg-matrix-scroll"><table class="stg-matrix-table"><thead><tr>' +
    '<th class="stg-mx-corner">' + escapeHtml(t(credentials.length ? 'settings.matrixModelCol' : 'settings.matrixModelColSolo')) + '</th>';

  for (var ci = 0; ci < credentials.length; ci++) {
    var cred = credentials[ci].row;
    var credKind = String(cred.kind || 'api_key');
    // The masked recognition hint (sk-…•••…wxyz) is the only way to tell two
    // same-kind credentials apart; fall back to the bare kind for hint-less
    // rows (local identity, legacy oauth snapshots).
    var credHint = String(cred.key_hint || '').trim();
    html += '<th class="stg-mx-keyhead" data-key-idx="' + ci + '">' +
      '<span class="stg-mx-credname">' + escapeHtml(t('settings.matrixCredentialN').replace('{n}', String(ci + 1))) + '</span>' +
      '<span class="stg-mx-credkind" title="' + escapeHtml(credHint ? credKind + ' · ' + credHint : credKind) + '">' +
        escapeHtml(credHint || credKind) + '</span>' +
      '<button type="button" class="stg-mx-zap col' + (_scopeCovers(providerId, 'col', ci) ? ' probing' : '') + '"' +
        (running ? ' disabled' : '') +
        ' data-provider-id="' + escapeHtml(providerId) + '"' +
        ' data-tofu-action="_probeMatrixScope(this.dataset.providerId,\'col\',' + ci + ')" ' +
        'title="' + escapeHtml(t('settings.matrixProbeColHint')) + '">' + _MX_BOLT + '</button>' +
    '</th>';
  }
  html += '</tr></thead><tbody>';

  if (!rows.length) {
    html += '<tr><td class="stg-mx-nomatch" colspan="' + (credentials.length + 1) + '">' +
      escapeHtml(t('settings.matrixNoMatch')) + '</td></tr>';
  }

  for (var ri = 0; ri < rows.length; ri++) {
    var entry = rows[ri];
    var pool = _matrixRowPool(entry);
    var displayCount = pool.length;
    var groupOpen = displayCount > 0; // only bracket offerings that HAVE a pool
    if (_matrixIsPureLogical(entry)) {
      html += _renderMatrixRow(providerId, entry, entry.canonical, -1, displayCount, credentials, groupOpen);
      for (var li = 0; li < pool.length; li++) {
        html += _renderMatrixRow(providerId, entry, pool[li], li + 1, displayCount, credentials, groupOpen);
      }
    } else {
      for (var wi = 0; wi < pool.length; wi++) {
        html += _renderMatrixRow(providerId, entry, pool[wi], wi, displayCount, credentials, groupOpen);
      }
    }
  }
  html += '</tbody></table></div></div>';
  return html;
}

/** Render one matrix row. Kinds:
 *   - LOGICAL HEADER (``rowPos === -1``): the preset-facing canonical id of
 *     an offering whose wire pool never carries it. It carries the offering
 *     toggle and the wire-id count, but NO per-credential cells — the id is
 *     never sent on the wire, so there is no (credential × id) pair to
 *     grant, deny, or probe.
 *   - WIRE ROW (``rowPos >= 0``): one concrete upstream id across
 *     credentials. ``rowPos`` is the 1-based index under a logical header,
 *     or 0 = root for a legacy-shape offering (canonical id IS a wire id).
 *  Root rows double as the supply manager: state chips (待确认/已下线),
 *  capability/pricing/context meta line, and row actions (row probe, add
 *  request name, remove offering). */
function _renderMatrixRow(providerId, entry, id, rowPos, rowCount, credentials, grouped) {
  var isLogicalHead = (rowPos === -1);
  var isAlias = rowPos > 0;
  var underHead = _matrixIsPureLogical(entry);
  var isLastInGroup = underHead ? (rowPos === rowCount) : (rowPos === rowCount - 1);
  var globallyOff = (entry.offering.enabled === false);
  // Logical headers have no wire request; provider-scoped rows remain probeable.
  var hatched = isLogicalHead;
  var brand = (typeof _modelBrand === 'function')
    ? _modelBrand(id, entry.offering && entry.offering.model && entry.offering.model.creator_id)
    : ((typeof _detectBrand === 'function') ? _detectBrand(id) : '');
  var brandSvg = (typeof _brandSvg === 'function') ? _brandSvg(brand, 14) : '';

  // Row-scope probe button: probes exactly this wire id across every credential.
  var _rowProbe = _stgMatrixProbe[providerId] || {};
  var _rowRunning = (_rowProbe.status === 'running');
  var rowProbeBtn = (hatched || !credentials.length) ? '' : '<button type="button" class="stg-mx-zap row' +
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
    var depIndex = entry.deploymentByWire[id];
    labelCell = '<td class="stg-mx-model alias' + (globallyOff ? ' model-off' : '') +
        (isLastInGroup ? ' last' : '') +
        '" style="--alias-hue:' + hue + '">' +
      '<span class="stg-mx-tree">' + connector + '</span>' +
      '<span class="stg-mx-aliasidx">' + rowPos + '</span>' +
      '<span class="stg-mx-brand">' + brandSvg + '</span>' +
      '<span class="stg-mx-mid alias-id" data-request-deployment="' +
        escapeHtml((_stgModelRouting.deployments[depIndex] || {}).deployment_id || '') + '" title="' + escapeHtml(id) + '">' + escapeHtml(id) + '</span>' +
      '<span class="stg-mx-rowacts">' + rowProbeBtn +
        (depIndex != null
          ? '<button type="button" class="stg-mx-act del" title="' + escapeHtml(t('settings.matrixRemoveAliasHint')) + '" ' +
            'data-tofu-action="event.stopPropagation();_removeV2Alias(' + depIndex + ')">×</button>'
          : '') +
      '</span>' +
    '</td>';
  } else {
    var poolLen = entry.wireIds.length;
    var countBadge = (!entry.pending)
      ? '<span class="stg-mx-aliascount" title="' + escapeHtml(t('settings.matrixAliasCountHint')) + '">' +
          poolLen + ' ' + escapeHtml(t('settings.matrixIdMany')) + '</span>'
      : '';
    var presetBadge = isLogicalHead
      ? '<span class="stg-mx-preset" title="' + escapeHtml(t('settings.matrixPresetHint')) + '">' +
          escapeHtml(t('settings.matrixPresetBadge')) + '</span>'
      : '';
    var rootActs = '<span class="stg-mx-rowacts">' + rowProbeBtn +
      '<button type="button" class="stg-mx-act add" title="' + escapeHtml(t('settings.matrixAddAliasHint')) + '" ' +
        'data-provider-id="' + escapeHtml(providerId) + '" ' +
        'data-tofu-action="event.stopPropagation();_addV2Alias(this.dataset.providerId,\'' +
          escapeHtml(entry.offering.offering_id) + '\')">+</button>' +
      '<button type="button" class="stg-mx-act del danger" title="' + escapeHtml(t('settings.matrixRemoveOfferingHint')) + '" ' +
        'data-provider-id="' + escapeHtml(providerId) + '" ' +
        'data-tofu-action="event.stopPropagation();_removeV2Offering(this.dataset.providerId,\'' +
          escapeHtml(entry.offering.offering_id) + '\')">' + _MX_TRASH + '</button>' +
      '</span>';
    labelCell = '<td class="stg-mx-model root' + (isLogicalHead ? ' logical' : '') +
        (globallyOff ? ' model-off' : '') + (entry.pending ? ' pending' : '') +
        (entry.retired ? ' retired' : '') + '">' +
      '<label class="stg-toggle stg-mx-gtoggle" title="' + escapeHtml(t('settings.matrixGlobalToggle')) + '" data-tofu-action="event.stopPropagation();">' +
        '<input type="checkbox"' + (globallyOff ? '' : ' checked') +
          ' data-tofu-action-change="_setModelRoutingCollectionField(\'offerings\',' +
          entry.offeringIndex + ',\'enabled\',this.checked,\'boolean\')">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
      '</label>' +
      '<span class="stg-mx-brand">' + brandSvg + '</span>' +
      '<span class="stg-mx-mid' + (entry.retired ? ' retired' : '') + '" title="' + escapeHtml(id || '') + '">' +
        escapeHtml(id || '(unnamed)') + '</span>' +
      _mxStateChips(entry) +
      presetBadge +
      countBadge +
      rootActs +
      _mxMetaHtml(entry) +
    '</td>';
  }

  var cls = 'stg-mx-row' + (globallyOff ? ' model-off' : '') +
    (isAlias ? ' is-alias' : ' is-root') + (isLogicalHead ? ' is-logical' : '') +
    (grouped ? ' grouped' : '') + (isLastInGroup && grouped ? ' group-end' : '') +
    (entry.pending ? ' is-pending' : '') +
    (entry.retired ? ' is-retired' : '');
  var row = '<tr class="' + cls + '" data-offering="' + entry.offeringIndex + '" data-id="' + escapeHtml(id) + '">' + labelCell;
  if (hatched) {
    var hatchTitle = isLogicalHead ? '' :
      ' title="' + escapeHtml(t('settings.matrixPendingHint')) + '"';
    for (var hk = 0; hk < credentials.length; hk++) {
      row += '<td class="stg-mx-cell logical"' + hatchTitle + '></td>';
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
  var on = _matrixCellOn(credentialRow, entry, id);

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

  return '<td class="stg-mx-cell' + (entry.pending ? ' provider-model' : (on ? ' on' : ' off')) +
      '" data-offering="' + entry.offeringIndex + '" data-key-idx="' + credIdx + '" data-id="' + escapeHtml(id) + '">' +
    (entry.pending ? '' : '<button type="button" class="stg-mx-toggle" ' +
      'data-provider-id="' + escapeHtml(providerId) + '"' +
      ' data-wire-id="' + escapeHtml(String(id)) + '"' +
      ' data-tofu-action="_toggleMatrixAccess(this.dataset.providerId,' + entry.offeringIndex + ',' + credIdx + ',this.dataset.wireId)" ' +
      'title="' + escapeHtml(on ? t('settings.matrixClickDisable') : t('settings.matrixClickEnable')) + '">' +
      '<span class="stg-mx-dot"></span>' +
    '</button>') +
    pip +
    cellProbe +
  '</td>';
}

// ── Interactions ──────────────────────────────────────────────────────

/** Toggle one (credential × wire id) cell. The grant is a model ModelRef
 *  plus an exclusion list of that model's wire ids; flipping one cell adds
 *  or removes this wire id from the exclusions. At the boundaries the model
 *  grant itself is minted (enabling when no grant exists) or dropped
 *  (disabling the last reachable wire). The change lives in
 *  ``_stgModelRouting`` and is persisted by the settings 保存 flow. */
function _toggleMatrixAccess(providerId, offeringIndex, credIdx, wireId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var offering = (_stgModelRouting.offerings || [])[offeringIndex];
  // credIdx is the column position inside THIS provider's matrix, not an
  // index into the global credentials array — resolve through the
  // provider-scoped context (its .row aliases the global object), or any
  // access whose credentials don't start at global index 0 toggles a
  // DIFFERENT provider's credential and its own dot never flips.
  var credItem = context.credentials[credIdx];
  var credential = credItem && credItem.row;
  if (!offering || !credential || !offering.model) return;
  if (!credential.authorization) credential.authorization = { connection_ids: [], models: [] };
  var grants = credential.authorization.models || [];
  var creator = String(offering.model.creator_id || '');
  var modelId = String(offering.model.model_id || '');
  // Live callers always pass the row's wire id; a bare call (legacy tests,
  // programmatic toggles) resolves to the row's single/primary wire id so
  // the exclusion can never be keyed on an empty string.
  var pool = _matrixRowPool(_matrixEntryFor(context, offeringIndex)).map(String);
  var wire = (wireId === undefined || wireId === null || wireId === '')
    ? (pool[0] || '') : String(wireId);
  var grant = null;
  for (var i = 0; i < grants.length; i++) {
    if (String(grants[i].creator_id || '') === creator &&
        String(grants[i].model_id || '') === modelId) { grant = grants[i]; break; }
  }
  if (!grant) {
    // No grant at all → enabling. Mint a grant with no exclusions (the key
    // stays absent — a grant without it reaches every wire of the model).
    grants.push(JSON.parse(JSON.stringify(offering.model)));
    credential.authorization.models = grants;
    _rerenderMatrix(providerId);
    return;
  }
  var excluded = (grant.excluded_wire_model_ids || []).slice();
  var pos = excluded.indexOf(wire);
  if (pos >= 0) {
    // Currently excluded → enable this wire by lifting the exclusion.
    excluded.splice(pos, 1);
    if (excluded.length) grant.excluded_wire_model_ids = excluded;
    else delete grant.excluded_wire_model_ids;
    _rerenderMatrix(providerId);
    return;
  }
  // Currently reachable → disable this wire. When it is the model's last
  // reachable wire for this credential, drop the whole grant instead of
  // leaving a grant that reaches nothing.
  var reachableNow = pool.filter(function(id) {
    return excluded.indexOf(String(id)) < 0;
  });
  if (reachableNow.length <= 1 && reachableNow.indexOf(wire) >= 0) {
    credential.authorization.models = grants.filter(function(ref) {
      return !(String(ref.creator_id || '') === creator && String(ref.model_id || '') === modelId);
    });
  } else {
    excluded.push(wire);
    grant.excluded_wire_model_ids = excluded;
  }
  _rerenderMatrix(providerId);
}

/** Row entry for one offering index inside this provider context. */
function _matrixEntryFor(context, offeringIndex) {
  var rows = _matrixModelRows(context);
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].offeringIndex === offeringIndex) return rows[i];
  }
  return { wireIds: [], canonical: '' };
}

/** Re-render the providers tab; open cards and per-provider matrix state
 *  are preserved by the tab renderer + ``_stgMatrixQuery``/``_stgMatrixFilter``.
 *  The matrix search box keeps focus + caret across the rebuild so typing
 *  doesn't drop the field on every keystroke. */
function _rerenderMatrix(providerId) {
  var refocus = null;
  if (typeof document !== 'undefined' && document.activeElement) {
    var active = document.activeElement;
    if (active && active.id && String(active.id).indexOf('stgMatrixSearch-') === 0) {
      refocus = { id: active.id, start: active.selectionStart, end: active.selectionEnd };
    }
  }
  _renderProvidersTab();
  if (refocus) {
    var el = document.getElementById(refocus.id);
    if (el && typeof el.focus === 'function') {
      el.focus();
      try { el.setSelectionRange(refocus.start, refocus.end); } catch (e) { /* non-text input */ }
    }
  }
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
 * that contradiction into a green pip. The legacy-status migration no-ops
 * when the typed status helper is absent (the server-side snapshot
 * normalization then owns the rule); the disabled-cell strip below is not
 * part of that migration and always runs. */
function _reconcileProbeProofContract(providerId) {
  var probe = _stgMatrixProbe[providerId];
  if (!probe || !probe.cells) return;
  var migrate = typeof runtimeScope.effectiveProbeStatus === 'function';
  var changed = false;
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c) return;
    if (migrate) {
      var effective = runtimeScope.effectiveProbeStatus(c, probe.probe_schema_version || 1);
      if (effective !== c.status) {
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
      }
    }
    // Historical discovery snapshots stay non-actionable after retiring
    // request-name enablement; fresh probes no longer emit this marker.
    if (c.deployment_enabled === false && c.recommend_disable) {
      c.recommend_disable = false;
      changed = true;
    }
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
async function _runMatrixProbe(providerId, force, only) {
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

  try {
    // Joining an existing model-add save may establish its Offering. Detect
    // never initiates a save, and a rejected add must abort instead of probing
    // an older selection. Re-read the draft after the chain settles.
    if (_stgRoutingPersistChain) await _stgRoutingPersistChain;
    var body = getManualMatrixProbe().buildCommand(
      _modelRoutingProviderContext(providerId), _stgModelRoutingSavedJson,
      { attempts: _stgMatrixAttempts[providerId] || 3, force: force, only: only });
    var snap = await Api.modelRouting.probeCellsStart(providerId, body);
    if (!_ingestProbeSnapshot(providerId, snap)) throw new Error(t('settings.matrixProbeFailed'));
    _rerenderMatrix(providerId);
    if (_stgMatrixProbe[providerId].status === 'running') _pollMatrixProbe(providerId);
  } catch (e) {
    var message = getManualMatrixProbe().startError(e);
    delete _stgMatrixProbeScope[providerId];
    _stgMatrixProbe[providerId] = { status: 'error', cells: {}, summary: { ok: 0, disable: 0 }, error: message, start_error: true };
    if (typeof showToast === 'function') showToast(message, 'error');
    _rerenderMatrix(providerId);
  }
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
  if (typeof Api === 'undefined' || !Api.modelRouting
      || typeof Api.modelRouting.probeCellsStatus !== 'function') return;
  Api.modelRouting.probeCellsStatus(providerId).then(function(snap) {
    if (_ingestProbeSnapshot(providerId, snap)) {
      _rerenderMatrix(providerId);
      if (_stgMatrixProbe[providerId].status === 'running') _pollMatrixProbe(providerId);
    }
  }).catch(function() { /* best-effort resume */ });
}

/** Explicit draft edit: fold the probe's per-wire verdicts into the
 *  credential grants. Symmetric — recommend-disable cells add wire
 *  exclusions (dropping the model grant only when every probed wire of the
 *  model is flagged), and ok cells whose wire is withheld mint grants or
 *  lift exclusions. Probe ingestion never invokes this action; the Settings
 *  Save flow is still the only persistence boundary. */
/** Pure projection: how many (credential × model) grants would the current
 *  probe's actionable cells change? Groups cells exactly like the apply path
 *  and replays the same decision logic against a credential's grants — but
 *  never mutates. Both the toolbar count and the apply action derive from
 *  this so the number on the button always equals what Apply will do. */
function _matrixApplyPlan(context, probe) {
  if (!context || !probe || !probe.cells) return [];
  var byRoot = {};
  _matrixModelRows(context).forEach(function(entry) { byRoot[entry.canonical] = entry; });
  // Group actionable cells by (credential × model) so a model loses its
  // grant only when ALL its probed wires recommend-disable.
  var groups = {}; // credIdx|creator|model → { credential, entry, disables:[], enables:[] }
  Object.keys(probe.cells).forEach(function(k) {
    var c = probe.cells[k];
    if (!c) return;
    // Historical discovery evidence must not re-arm into grant REMOVALS —
    // but the legacy marker never carried an ok verdict, so enablement of a
    // currently-off wire must still apply. Ingestion already cleared
    // recommend_disable on these cells; this is defense in depth.
    if (c.deployment_enabled === false && c.recommend_disable) return;
    var entry = byRoot[c.root_model_id];
    if (!entry || entry.pending) return;
    var item = context.credentials[c.key_idx];
    if (!item) return;
    var disable = !!c.recommend_disable;
    var enable = (c.status === 'ok');
    if (!disable && !enable) return;
    // A non-chat model may only change on a verdict from its OWN modality
    // probe — never on a stale chat-completions verdict that cannot speak
    // for its real endpoint.
    if (_matrixModelIsNonChat(entry) && !_isFreshModalityVerdict(c)) return;
    var creator = String(entry.offering.model.creator_id || '');
    var modelId = String(entry.offering.model.model_id || '');
    var key = c.key_idx + '|' + creator + '|' + modelId;
    var g = groups[key] || (groups[key] = {
      credential: item.row, entry: entry, creator: creator, modelId: modelId,
      disables: [], enables: []
    });
    (disable ? g.disables : g.enables).push(String(c.model_id));
  });

  var plan = [];
  Object.keys(groups).forEach(function(key) {
    var g = groups[key];
    var credential = g.credential;
    var grants = (credential.authorization && credential.authorization.models) || [];
    var grant = null;
    for (var i = 0; i < grants.length; i++) {
      if (String(grants[i].creator_id || '') === g.creator &&
          String(grants[i].model_id || '') === g.modelId) { grant = grants[i]; break; }
    }
    var pool = _matrixRowPool(g.entry).map(String);
    var disabledAll = g.disables.length > 0 && g.enables.length === 0 &&
      pool.every(function(id) { return g.disables.indexOf(id) >= 0; });
    if (disabledAll) {
      if (grant) plan.push({ group: g, kind: 'drop' });
      return;
    }
    if (!grant) {
      if (g.enables.length === 0) return;
      plan.push({ group: g, kind: 'mint' });
      return;
    }
    var excluded = (grant.excluded_wire_model_ids || []).slice();
    var before = excluded.join('\u0001');
    g.disables.forEach(function(id) { if (excluded.indexOf(id) < 0) excluded.push(id); });
    g.enables.forEach(function(id) {
      var p = excluded.indexOf(id); if (p >= 0) excluded.splice(p, 1);
    });
    if (excluded.join('\u0001') !== before) {
      plan.push({ group: g, kind: 'retune', excluded: excluded });
    }
  });
  return plan;
}

/** Count of grants Apply would change — drives the toolbar button. */
function _matrixApplyCount(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  var probe = _stgMatrixProbe[providerId];
  if (!context || !probe || probe.status !== 'done' ||
      (probe.total && probe.done_count !== probe.total)) return 0;
  return _matrixApplyPlan(context, probe).length;
}

function _applyMatrixRecommendations(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  var probe = _stgMatrixProbe[providerId];
  if (!context || !probe || !probe.cells) return;
  // Enablement now flows from ok verdicts, so apply must wait for a COMPLETE
  // probe — a partial/running/errored snapshot must never mint or lift a
  // grant on half the evidence. Mirrors the backend's incomplete-probe guard.
  if (probe.status !== 'done' ||
      (probe.total && probe.done_count !== probe.total)) return;

  var applied = 0;
  _matrixApplyPlan(context, probe).forEach(function(action) {
    var g = action.group;
    var credential = g.credential;
    if (!credential.authorization) credential.authorization = { connection_ids: [], models: [] };
    var grants = credential.authorization.models || (credential.authorization.models = []);
    if (action.kind === 'drop') {
      credential.authorization.models = grants.filter(function(ref) {
        return !(String(ref.creator_id || '') === g.creator && String(ref.model_id || '') === g.modelId);
      });
      applied++;
      return;
    }
    if (action.kind === 'mint') {
      var fresh = { creator_id: g.creator, model_id: g.modelId };
      if (g.disables.length) fresh.excluded_wire_model_ids = g.disables.slice();
      grants.push(fresh);
      applied++;
      return;
    }
    // retune: rewrite the existing grant's exclusions in place.
    var grant = null;
    for (var i = 0; i < grants.length; i++) {
      if (String(grants[i].creator_id || '') === g.creator &&
          String(grants[i].model_id || '') === g.modelId) { grant = grants[i]; break; }
    }
    if (!grant) return;
    if (action.excluded.length) grant.excluded_wire_model_ids = action.excluded;
    else delete grant.excluded_wire_model_ids;
    applied++;
  });

  if (typeof showToast === 'function') {
    showToast(applied > 0
      ? t('settings.matrixApplied').replace('{disabled}', String(applied))
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
