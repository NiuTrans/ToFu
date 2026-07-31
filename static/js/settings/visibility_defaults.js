/* ═══════════════════════════════════════════════════════════════════
   settings/visibility defaults — extracted from settings.js (split 2026-05-28)

   Preset/visibility flags for IG models + dropdown models + model defaults.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Preset Tab — visibility controls for image gen & model dropdown
// ══════════════════════════════════════════════════════

function _renderPresetsTab(cfg) {
  // Render image gen visibility toggles (same pattern as Model Dropdown)
  _renderIgVisibility();
  // Render dropdown visibility toggles
  _renderDropdownVisibility();
  // Render model defaults (fallback model, preset defaults)
  _populateModelDefaults(cfg);
  // Repaint probe-health dots from any persisted snapshots (no new run).
  if (typeof _ddResumeProbeSnapshots === 'function') _ddResumeProbeSnapshots();
}

// ══════════════════════════════════════════════════════
//  Image Generation Visibility — choose which models show in the image gen picker
// ══════════════════════════════════════════════════════

function _renderIgVisibility() {
  var container = document.getElementById('stgIgVisibility');
  if (!container) return;

  // Collect all image_gen models from enabled providers
  var igModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    var caps = entry.model.capabilities || [];
    for (var c = 0; c < caps.length; c++) {
      if (caps[c] === 'image_gen') return true;
    }
    return false;
  });

  if (igModels.length === 0) {
    container.innerHTML = '<p class="stg-empty">' + t('settings.vdNoIgModels') + '</p>';
    return;
  }

  // Deduplicate by model_id
  var seen = {};
  var unique = [];
  for (var i = 0; i < igModels.length; i++) {
    var mid = igModels[i].model.model_id;
    if (!seen[mid]) {
      seen[mid] = true;
      unique.push(igModels[i]);
    }
  }

  // Load hidden set from server config
  var hidden = new Set((_serverConfig && _serverConfig.hidden_ig_models) || []);

  // Group by the SHARED brand rule (core/model_group.js) — the same key the
  // toolbar picker uses, so the two lists can never disagree. brandNames
  // come from the module, never re-typed here.
  var grouped = {};
  for (var i = 0; i < unique.length; i++) {
    var entry = unique[i];
    var bkey = (typeof modelGroupKey === 'function')
      ? modelGroupKey(entry.provider, entry.model)
      : (entry.provider.brand || _detectBrand((entry.provider.name || '') + ' '
          + (entry.provider.base_url || '') + ' ' + entry.model.model_id));
    var bname = (typeof modelGroupLabel === 'function')
      ? modelGroupLabel(bkey, entry.provider.name)
      : (entry.provider.name || bkey);
    if (!grouped[bkey]) grouped[bkey] = { name: bname, models: [] };
    grouped[bkey].models.push(entry.model);
  }

  var brandNames = (typeof modelGroupBrandNames === 'function')
    ? modelGroupBrandNames() : {};

  var html = '';
  var brandKeys = _sortedBrandKeys(grouped, brandNames);
  for (var bi = 0; bi < brandKeys.length; bi++) {
    var brand = brandKeys[bi];
    var group = grouped[brand];
    _sortModelsByDisplayName(group.models);
    var displayName = brandNames[brand] || group.name || brand;
    html += '<div class="stg-dv-group">';
    html += '<div class="stg-dv-brand">' + _brandSvg(brand, 14) + ' <span>' + escapeHtml(displayName) + '</span></div>';
    for (var j = 0; j < group.models.length; j++) {
      var m = group.models[j];
      var mid = m.model_id;
      var isVisible = !hidden.has(mid);
      var shortName = typeof _modelShortName === 'function' ? _modelShortName(mid) : mid;
      html += '<div class="stg-dv-item">';
      html += '  <span class="stg-dv-name" title="' + escapeHtml(mid) + '">' + escapeHtml(shortName) + '</span>';
      html += '  <label class="stg-toggle stg-dv-toggle">';
      html += '    <input type="checkbox" data-ig-model-id="' + escapeHtml(mid) + '" ' + (isVisible ? 'checked' : '') + ' onchange="_onIgVisibilityChange(this)">';
      html += '    <span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>';
      html += '  </label>';
      html += '</div>';
    }
    html += '</div>';
  }
  container.innerHTML = html;
}

function _onIgVisibilityChange(checkbox) {
  var modelId = checkbox.getAttribute('data-ig-model-id');
  var hidden = new Set((_serverConfig && _serverConfig.hidden_ig_models) || []);
  if (checkbox.checked) {
    hidden.delete(modelId);
  } else {
    hidden.add(modelId);
  }
  var arr = Array.from(hidden);
  if (_serverConfig) _serverConfig.hidden_ig_models = arr;
  // Update the global set so image gen picker reflects changes on close
  if (typeof _hiddenIgModels !== 'undefined') {
    _hiddenIgModels = hidden;
  }
}

function _toggleAllIgModels(show) {
  var container = document.getElementById('stgIgVisibility');
  if (!container) return;
  var checkboxes = container.querySelectorAll('input[type="checkbox"][data-ig-model-id]');
  var hidden = new Set();
  checkboxes.forEach(function(cb) {
    cb.checked = show;
    if (!show) hidden.add(cb.getAttribute('data-ig-model-id'));
  });
  var arr = Array.from(hidden);
  if (_serverConfig) _serverConfig.hidden_ig_models = arr;
  if (typeof _hiddenIgModels !== 'undefined') {
    _hiddenIgModels = hidden;
  }
}

// ══════════════════════════════════════════════════════
//  Model Dropdown Visibility — choose which models show in the picker
// ══════════════════════════════════════════════════════

function _renderDropdownVisibility() {
  var container = document.getElementById('stgDropdownVisibility');
  if (!container) return;

  // Collect all chat models from all enabled providers. isChatModel comes
  // from core/model_caps.js — SSOT for chat vs non-chat classification.
  // Guard: a stale/incomplete bundle can strand this filter without
  // isChatModel(); degrade to "show everything" instead of throwing and
  // leaving the settings list empty. Same rationale as main_toolbar_ui.
  var _hasCaps = (typeof isChatModel === 'function');
  if (!_hasCaps && typeof _warnModelCapsMissing === 'function') _warnModelCapsMissing();
  var allModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    return _hasCaps ? isChatModel(entry.model) : true;
  });

  if (allModels.length === 0) {
    container.innerHTML = '<p class="stg-empty">' + escapeHtml(t('settings.vdNoChatModels')) + '</p>';
    return;
  }

  // Load hidden set from server config (synced at openSettings)
  var hidden = new Set((_serverConfig && _serverConfig.hidden_models) || []);

  // Group by the SHARED brand rule (core/model_group.js) — the same key the
  // toolbar picker uses, so the two lists can never disagree. brandNames
  // come from the module, never re-typed here.
  var grouped = {};
  for (var i = 0; i < allModels.length; i++) {
    var entry = allModels[i];
    var bkey = (typeof modelGroupKey === 'function')
      ? modelGroupKey(entry.provider, entry.model)
      : (entry.provider.brand || _detectBrand((entry.provider.name || '') + ' '
          + (entry.provider.base_url || '') + ' ' + entry.model.model_id));
    var bname = (typeof modelGroupLabel === 'function')
      ? modelGroupLabel(bkey, entry.provider.name)
      : (entry.provider.name || bkey);
    if (!grouped[bkey]) grouped[bkey] = { name: bname, models: [] };
    grouped[bkey].models.push(entry.model);
  }

  var brandNames = (typeof modelGroupBrandNames === 'function')
    ? modelGroupBrandNames() : {};

  var html = '';
  var brandKeys = _sortedBrandKeys(grouped, brandNames);
  for (var bi = 0; bi < brandKeys.length; bi++) {
    var brand = brandKeys[bi];
    var group = grouped[brand];
    _sortModelsByDisplayName(group.models);
    var displayName = brandNames[brand] || group.name || brand;
    html += '<div class="stg-dv-group">';
    html += '<div class="stg-dv-brand">' + _brandSvg(brand, 14) + ' <span>' + escapeHtml(displayName) + '</span></div>';
    for (var j = 0; j < group.models.length; j++) {
      var m = group.models[j];
      var mid = m.model_id;
      var isVisible = !hidden.has(mid);
      var shortName = typeof _modelShortName === 'function' ? _modelShortName(mid) : mid;
      html += '<div class="stg-dv-item">';
      html += '  <span class="stg-dv-name" title="' + escapeHtml(mid) + '">' + escapeHtml(shortName) + '</span>';
      html += _ddHealthSpan(mid);
      html += '  <label class="stg-toggle stg-dv-toggle">';
      html += '    <input type="checkbox" data-model-id="' + escapeHtml(mid) + '" ' + (isVisible ? 'checked' : '') + ' onchange="_onDropdownVisibilityChange(this)">';
      html += '    <span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>';
      html += '  </label>';
      html += '</div>';
    }
    html += '</div>';
  }
  container.innerHTML = html;
}

function _onDropdownVisibilityChange(checkbox) {
  var modelId = checkbox.getAttribute('data-model-id');
  var hidden = new Set((_serverConfig && _serverConfig.hidden_models) || []);
  if (checkbox.checked) {
    hidden.delete(modelId);
  } else {
    hidden.add(modelId);
  }
  var arr = Array.from(hidden);
  // Update cached server config so subsequent toggles are consistent
  if (_serverConfig) _serverConfig.hidden_models = arr;
  // Update the global set so dropdown reflects changes on close
  if (typeof _hiddenModels !== 'undefined') {
    _hiddenModels = hidden;
  }
}

function _toggleAllDropdownModels(show) {
  var container = document.getElementById('stgDropdownVisibility');
  if (!container) return;
  var checkboxes = container.querySelectorAll('input[type="checkbox"][data-model-id]');
  var hidden = new Set();
  checkboxes.forEach(function(cb) {
    cb.checked = show;
    if (!show) hidden.add(cb.getAttribute('data-model-id'));
  });
  var arr = Array.from(hidden);
  if (_serverConfig) _serverConfig.hidden_models = arr;
  if (typeof _hiddenModels !== 'undefined') {
    _hiddenModels = hidden;
  }
}

// ══════════════════════════════════════════════════════
//  Model Defaults — fallback model + preset defaults
// ══════════════════════════════════════════════════════

/**
 * Populate the Model Defaults section (fallback model, preset default models).
 * Uses all chat models from all enabled providers as options.
 */
function _populateModelDefaults(cfg) {
  // Collect all chat models (exclude non-chat caps via core/model_caps.js).
  // Guard: see _renderDropdownVisibility above — same failure mode.
  var _hasCapsDef = (typeof isChatModel === 'function');
  if (!_hasCapsDef && typeof _warnModelCapsMissing === 'function') _warnModelCapsMissing();
  var chatModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    return _hasCapsDef ? isChatModel(entry.model) : true;
  });

  // Deduplicate by model_id
  var seen = {};
  var uniqueModels = [];
  for (var i = 0; i < chatModels.length; i++) {
    var mid = chatModels[i].model.model_id;
    if (!seen[mid]) {
      seen[mid] = true;
      uniqueModels.push(chatModels[i]);
    }
  }

  // Order the options by the DISPLAY name shown in the <select>, via the ONE
  // shared comparator (settings/branding.js). These previously inherited
  // whatever order _getAllModels walked the provider arrays in — model_id
  // order, which the settings cold sort writes back — while the option TEXT is
  // _modelShortName. Models with no MODEL_PRICING entry render their raw id and
  // so landed at arbitrary positions among the friendly-named ones.
  _sortModelEntriesByDisplayName(uniqueModels);

  // Read saved model_defaults from config
  var defaults = (cfg && cfg.model_defaults) || {};

  // Populate each select element
  var selectors = [
    { id: 'settingFallbackModel',  key: 'fallback_model',  emptyLabel: t('settings.vdFallbackEmpty') },
    { id: 'settingDefaultModel',   key: 'default_model',   emptyLabel: t('settings.vdDefaultEmpty') },

  ];

  for (var s = 0; s < selectors.length; s++) {
    var sel = document.getElementById(selectors[s].id);
    if (!sel) continue;
    var savedVal = defaults[selectors[s].key] || '';

    // Clear existing options and add the empty/default option
    sel.innerHTML = '<option value="">' + selectors[s].emptyLabel + '</option>';

    // Add all available chat models
    for (var m = 0; m < uniqueModels.length; m++) {
      var modelId = uniqueModels[m].model.model_id;
      var shortName = typeof _modelShortName === 'function' ? _modelShortName(modelId) : modelId;
      var opt = document.createElement('option');
      opt.value = modelId;
      opt.textContent = shortName;
      if (modelId === savedVal) opt.selected = true;
      sel.appendChild(opt);
    }

    // If the saved value doesn't match any available model, add it as a custom entry
    if (savedVal && !seen[savedVal]) {
      var customOpt = document.createElement('option');
      customOpt.value = savedVal;
      customOpt.textContent = t('settings.vdUnregistered', { model: savedVal });
      customOpt.selected = true;
      sel.appendChild(customOpt);
    }
  }
}

/**
 * Collect current model defaults from the UI for saving.
 */
// ══════════════════════════════════════════════════════
//  Dropdown Probe Health — "test each model so I can select with confidence"
// ══════════════════════════════════════════════════════
//
// Each dropdown row gets a health dot next to the visibility toggle. "测试
// 全部" runs the EXISTING probe-cells engine (no new probe machinery) — one
// task per enabled provider, each with its own protocol/oauth — and the
// results are merged back per logical model and folded with the SHARED pool
// judgment (core/model_health.js). A dot's colour answers "is this model
// usable RIGHT NOW", and the tooltip says WHEN it was last tested and WHICH
// provider/protocol the verdict came from (the two Meituan faces share keys
// but probe different protocols — the source is part of the verdict).

var _ddProbeSnaps = {};   // provider_id → { snapshot, providerName, protocol }
var _ddProbeRunning = false;

/** The provider object from _stgProviders by id (undefined when absent). */
function _ddProviderById(pid) {
  for (var i = 0; i < _stgProviders.length; i++) {
    var p = _stgProviders[i];
    if ((p && (p.id || ('idx_' + i))) === pid) return p;
  }
  return undefined;
}

/** Health dot HTML for one dropdown row. */
function _ddHealthSpan(mid) {
  return '<span class="stg-dv-health" data-health-for="' + escapeHtml(mid) + '">' +
         '<span class="stg-dv-health-dot" title="' + escapeHtml(t('settings.mhNeverProbed')) + '"></span>' +
         '</span>';
}

/** Fold one model's probe cells into a dot state and paint it.
 *  Reads _ddProbeSnaps for the provider(s) that carry this model_id. */
function _ddPaintHealth(mid) {
  var dot = document.querySelector('.stg-dv-health[data-health-for="' + CSS.escape(mid) + '"] .stg-dv-health-dot');
  if (!dot) return;
  // Find the provider(s) containing this model and gather their cells.
  var entry = null;
  var all = _getAllModels();
  for (var i = 0; i < all.length; i++) {
    if (all[i].model.model_id === mid) { entry = all[i]; break; }
  }
  if (!entry) return;
  var pid = entry.provider.id || ('idx_' + entry.provIdx);
  var rec = _ddProbeSnaps[pid];
  if (!rec || !rec.snapshot) return;  // never probed → keep the muted dot

  var cells = [];
  var snapCells = (rec.snapshot && rec.snapshot.cells) || {};
  for (var k in snapCells) {
    var c = snapCells[k];
    if (c && c.root_model_id === mid) {
      cells.push({
        key_idx: c.key_idx, model_id: c.model_id,
        status: c.status, detail: c.detail,
        provider: rec.providerName, protocol: rec.protocol,
      });
    }
  }
  if (typeof foldProbeHealth !== 'function') return;
  var agg = foldProbeHealth(cells, {
    finishedAt: (rec.snapshot && rec.snapshot.finished_at) || 0,
    now: Date.now() / 1000,
  });
  var cls = (typeof modelHealthLevelClass === 'function')
    ? modelHealthLevelClass(agg) : (agg.level || 'unknown');
  dot.className = 'stg-dv-health-dot mh-' + cls;

  // Tooltip: level + age + per-cell provider/protocol source.
  var lines = [];
  lines.push(t('settings.mhProbedAt', { t: agg.probedAt
    ? new Date(agg.probedAt * 1000).toLocaleString() : '—' }));
  if (agg.stale) lines.push(t('settings.mhStaleTip'));
  var shown = {};
  for (var j = 0; j < cells.length; j++) {
    var cc = cells[j];
    var key = cc.provider + '|' + cc.model_id + '|' + cc.status;
    if (shown[key]) continue;
    shown[key] = 1;
    var src = cc.provider + (cc.protocol ? ' · ' + cc.protocol : '');
    if (cc.status !== 'ok') lines.push(src + ' — ' + cc.model_id + ': ' + cc.status + (cc.detail ? ' (' + cc.detail + ')' : ''));
  }
  dot.title = lines.join('\n');
}

/** Re-paint every visible health dot from the current snapshots. */
function _renderDropdownProbeHealth() {
  var dots = document.querySelectorAll('.stg-dv-health[data-health-for]');
  for (var i = 0; i < dots.length; i++) {
    _ddPaintHealth(dots[i].getAttribute('data-health-for'));
  }
}

/** Kick off (or resume) the probe for ONE provider, then poll to done. */
function _ddProbeProvider(entry, force) {
  var p = entry.provider;
  var pid = p.id || ('idx_' + entry.provIdx);
  var pname = p.name || pid;
  var proto = p.protocol || 'openai';
  var chatModels = [];
  var seen = {};
  var all = _getAllModels();
  for (var i = 0; i < all.length; i++) {
    if (all[i].provider === p) {
      var m = all[i].model;
      if (!seen[m.model_id] && (typeof isChatModel !== 'function' || isChatModel(m))) {
        seen[m.model_id] = 1;
        chatModels.push({ model_id: m.model_id, aliases: (m.aliases || []),
                          capabilities: (m.capabilities || []) });
      }
    }
  }
  if (!chatModels.length) return Promise.resolve();
  var keys = (p.api_keys || []).filter(function(k) { return k != null; });
  if (!keys.length && p.brand === 'local') keys = [''];
  if (!keys.length) return Promise.resolve();

  return Api.providers.probeCellsStart({
    provider_id: pid, base_url: p.base_url || '',
    api_keys: keys, extra_headers: p.extra_headers || {},
    protocol: proto, oauth: p.oauth || '',
    faces: p.faces || {},
    models: chatModels, attempts: 3, force: !!force,
  }).then(function(snap) {
    if (!snap) return;
    _ddProbeSnaps[pid] = { snapshot: snap, providerName: pname, protocol: proto };
    // Poll until terminal (probe-cells snapshots are persisted server-side).
    return _ddPollProvider(pid);
  });
}

/** Poll one provider's probe status until it leaves 'running'. */
function _ddPollProvider(pid) {
  return Api.providers.probeCellsStatus(pid).then(function(snap) {
    if (snap && snap.status && snap.status !== 'none') {
      _ddProbeSnaps[pid].snapshot = snap;
    }
    if (snap && snap.status === 'running') {
      return new Promise(function(res) {
        setTimeout(function() { res(_ddPollProvider(pid)); }, 800);
      });
    }
  }).catch(function() { /* keep last-known snapshot */ });
}

/** "测试全部": probe every enabled provider and refresh the dots. */
function _probeAllDropdownModels() {
  if (_ddProbeRunning) return;
  _ddProbeRunning = true;
  var btn = document.getElementById('stgProbeAllModelsBtn');
  if (btn) { btn.disabled = true; btn.textContent = t('settings.mhProbing'); }

  var byProvider = {};
  var all = _getAllModels();
  for (var i = 0; i < all.length; i++) {
    var e = all[i];
    if (e.provider.enabled === false) continue;
    var pidKey = e.provider.id || ('idx_' + e.provIdx);
    if (!byProvider[pidKey]) byProvider[pidKey] = e;
  }
  var entries = [];
  for (var pid in byProvider) entries.push(byProvider[pid]);

  return Promise.all(entries.map(function(e) { return _ddProbeProvider(e, true); }))
    .catch(function() { /* per-provider failures already isolated */ })
    .finally(function() {
      _ddProbeRunning = false;
      if (btn) { btn.disabled = false; btn.textContent = t('settings.probeAllModels'); }
      _renderDropdownProbeHealth();
    });
}

/** On preset-tab open, resume any persisted probe results (no new run). */
function _ddResumeProbeSnapshots() {
  // Best-effort repaint: if the Api seam is unavailable (stale bundle), skip
  // silently rather than break the preset render — the dots stay muted, which
  // is a correct "no signal yet" state, not an error.
  if (typeof Api === 'undefined' || !Api.providers
      || typeof Api.providers.probeCellsStatus !== 'function') return;
  var byProvider = {};
  var all = _getAllModels();
  for (var i = 0; i < all.length; i++) {
    var e = all[i];
    if (e.provider.enabled === false) continue;
    var pidKey = e.provider.id || ('idx_' + e.provIdx);
    if (!byProvider[pidKey]) byProvider[pidKey] = e;
  }
  for (var pid in byProvider) {
    (function(pid, e) {
      Api.providers.probeCellsStatus(pid).then(function(snap) {
        if (snap && snap.status && snap.status !== 'none') {
          _ddProbeSnaps[pid] = {
            snapshot: snap,
            providerName: e.provider.name || pid,
            protocol: e.provider.protocol || 'openai',
          };
          _renderDropdownProbeHealth();
        }
      });
    })(pid, byProvider[pid]);
  }
}

function _collectModelDefaults() {
  var result = {};
  var fields = [
    { id: 'settingFallbackModel', key: 'fallback_model' },
    { id: 'settingDefaultModel',  key: 'default_model' },

  ];
  for (var i = 0; i < fields.length; i++) {
    var el = document.getElementById(fields[i].id);
    if (el) result[fields[i].key] = el.value || '';
  }
  return result;
}

