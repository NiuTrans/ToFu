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
      ' · ' + recommendCount + ' ' + t('settings.matrixFlaggedCount');
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
          ' onclick="_runMatrixProbe(' + provIdx + ',' + (hasResults ? 'true' : 'false') + ')" title="' + escapeHtml(t('settings.matrixProbeHint')) + '">⚡ ' +
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
    case 'unauthorized': return { glyph: '⛔', cls: 'unauth', label: t('settings.probeUnauthorized') };
    case 'not_found':    return { glyph: '∅', cls: 'nf',     label: t('settings.probeNotFound') };
    case 'unavailable':  return { glyph: '⚠', cls: 'down',   label: t('settings.probeUnavailable') };
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
  var pip = '';
  var r = pcells[_probeCellKey(keyIdx, id)];
  if (r) {
    var info = _probeStatusInfo(r.status);
    pip = '<span class="stg-mx-probe-pip ' + info.cls + '" title="' + escapeHtml(info.label + (r.detail ? ' — ' + r.detail : '')) + '">' + info.glyph + '</span>';
  }

  return '<td class="stg-mx-cell' + (on ? ' on' : ' off') + (overridden ? ' overridden' : '') +
      '" data-model="' + modelIdx + '" data-key-idx="' + keyIdx + '" data-id="' + escapeHtml(id) + '">' +
    '<button type="button" class="stg-mx-toggle" ' +
      'onclick="_toggleIdAccess(' + provIdx + ',' + modelIdx + ',' + keyIdx + ',' + JSON.stringify(id).replace(/"/g, '&quot;') + ')" ' +
      'title="' + escapeHtml(on ? t('settings.matrixClickDisable') : t('settings.matrixClickEnable')) + '">' +
      '<span class="stg-mx-dot"></span>' +
    '</button>' +
    pip +
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
  var allCaps = ['text', 'vision', 'thinking', 'cheap', 'image_gen', 'embedding'];
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
  return true;
}

/** Start (or, when not forcing, resume) a background probe for a provider. */
function _runMatrixProbe(provIdx, force) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  var keys = _matrixKeys(p);
  var models = (p.models || []).filter(function(m) { return (m.model_id || '').trim(); });
  if (!keys.length || !models.length) {
    if (typeof showToast === 'function') showToast(t('settings.matrixNothingToProbe'), 'warning');
    return;
  }

  _stgMatrixProbe[provIdx] = { status: 'running', cells: (force ? {} : ((_stgMatrixProbe[provIdx] || {}).cells || {})),
    summary: { ok: 0, disable: 0 }, total: 0, done_count: 0, error: null };
  _rerenderMatrix(provIdx);

  var body = {
    provider_id: _providerId(provIdx),
    base_url: p.base_url || '',
    api_keys: keys,
    extra_headers: p.extra_headers || {},
    protocol: p.protocol || 'openai',
    models: models.map(function(m) { return { model_id: m.model_id, aliases: (m.aliases || []) }; }),
    attempts: _stgMatrixAttempts[provIdx] || 3,
    force: !!force,
  };

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
      ? t('settings.matrixApplied').replace('{n}', applied)
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
  _stgMatrixProbeAttached[provIdx] = true; // don't auto-reattach until reopen
  _rerenderMatrix(provIdx);
}
