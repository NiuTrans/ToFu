/* ===== migrated source: settings/visibility_defaults.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   settings/visibility defaults — extracted from settings.js (split 2026-05-28)

   Preset/visibility flags for IG models + dropdown models + model defaults.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
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
}

/* The settings visibility lists are the MANAGEMENT surface: every model_id
 * stays a flat, individually toggleable row. Display folds (alias mirrors /
 * version families) belong to the toolbar picker only — folding them here
 * hid rows the user came to show/hide (owner call 2026-08-23). */

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

  // Group through the required typed policy used by the toolbar picker.
  // Brand names come from that owner and are never re-typed here.
  var grouped = {};
  for (var i = 0; i < unique.length; i++) {
    var entry = unique[i];
    var bkey = runtimeScope.modelGroupKey(entry.provider, entry.model);
    var bname = runtimeScope.modelGroupLabel(bkey, entry.provider.name);
    if (!grouped[bkey]) grouped[bkey] = { name: bname, models: [] };
    grouped[bkey].models.push(entry.model);
  }

  var brandNames = runtimeScope.modelGroupBrandNames();

  var html = '';
  var brandKeys = _sortedBrandKeys(grouped, brandNames);
  for (var bi = 0; bi < brandKeys.length; bi++) {
    var brand = brandKeys[bi];
    var group = grouped[brand];
    _sortModelsByDisplayName(group.models);
    var displayName = brandNames[brand] || group.name || brand;
    html += '<div class="stg-dv-group">';
    html += '<div class="stg-dv-brand">' + _brandSvg(brand, 14) + ' <span>' + escapeHtml(displayName) + '</span></div>';
    var igRowHtml = function(m) {
      var mid = m.model_id;
      var isVisible = !hidden.has(mid);
      var shortName = typeof _modelShortName === 'function' ? _modelShortName(mid) : mid;
      var h = '<div class="stg-dv-item">';
      h += '  <span class="stg-dv-name" title="' + escapeHtml(mid) + '">' + escapeHtml(shortName) + '</span>';
      h += '  <label class="stg-toggle stg-dv-toggle">';
      h += '    <input type="checkbox" data-ig-model-id="' + escapeHtml(mid) + '" ' + (isVisible ? 'checked' : '') + ' data-tofu-action-change="_onIgVisibilityChange(this)">';
      h += '    <span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>';
      h += '  </label>';
      h += '</div>';
      return h;
    };
    for (var j = 0; j < group.models.length; j++) {
      html += igRowHtml(group.models[j]);
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
  runtimeScope._setHiddenIgModels(hidden);
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
  runtimeScope._setHiddenIgModels(hidden);
}

// ══════════════════════════════════════════════════════
//  Model Dropdown Visibility — choose which models show in the picker
// ══════════════════════════════════════════════════════

function _renderDropdownVisibility() {
  var container = document.getElementById('stgDropdownVisibility');
  if (!container) return;

  // Collect all chat models from all enabled providers. isChatModel comes
  // from the typed capability-taxonomy owner.
  // Guard: a stale/incomplete bundle can strand this filter without
  // isChatModel(); degrade to "show everything" instead of throwing and
  // leaving the settings list empty. Same rationale as main_toolbar_ui.
  var _hasCaps = (typeof runtimeScope.isChatModel === 'function');
  if (!_hasCaps && typeof _warnModelCapsMissing === 'function') _warnModelCapsMissing();
  var allModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    return _hasCaps ? runtimeScope.isChatModel(entry.model) : true;
  });

  if (allModels.length === 0) {
    container.innerHTML = '<p class="stg-empty">' + escapeHtml(t('settings.vdNoChatModels')) + '</p>';
    return;
  }

  // Load hidden set from server config (synced at openSettings)
  var hidden = new Set((_serverConfig && _serverConfig.hidden_models) || []);

  // Group through the required typed policy used by the toolbar picker.
  // Brand names come from that owner and are never re-typed here.
  var grouped = {};
  for (var i = 0; i < allModels.length; i++) {
    var entry = allModels[i];
    var bkey = runtimeScope.modelGroupKey(entry.provider, entry.model);
    var bname = runtimeScope.modelGroupLabel(bkey, entry.provider.name);
    if (!grouped[bkey]) grouped[bkey] = { name: bname, models: [] };
    grouped[bkey].models.push(entry.model);
  }

  var brandNames = runtimeScope.modelGroupBrandNames();

  var html = '';
  var brandKeys = _sortedBrandKeys(grouped, brandNames);
  for (var bi = 0; bi < brandKeys.length; bi++) {
    var brand = brandKeys[bi];
    var group = grouped[brand];
    _sortModelsByDisplayName(group.models);
    var displayName = brandNames[brand] || group.name || brand;
    html += '<div class="stg-dv-group">';
    html += '<div class="stg-dv-brand">' + _brandSvg(brand, 14) + ' <span>' + escapeHtml(displayName) + '</span></div>';
    var chatRowHtml = function(m) {
      var mid = m.model_id;
      var isVisible = !hidden.has(mid);
      var shortName = typeof _modelShortName === 'function' ? _modelShortName(mid) : mid;
      var h = '<div class="stg-dv-item">';
      h += '  <span class="stg-dv-name" title="' + escapeHtml(mid) + '">' + escapeHtml(shortName) + '</span>';
      h += '  <label class="stg-toggle stg-dv-toggle">';
      h += '    <input type="checkbox" data-model-id="' + escapeHtml(mid) + '" ' + (isVisible ? 'checked' : '') + ' data-tofu-action-change="_onDropdownVisibilityChange(this)">';
      h += '    <span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>';
      h += '  </label>';
      h += '</div>';
      return h;
    };
    for (var j = 0; j < group.models.length; j++) {
      html += chatRowHtml(group.models[j]);
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
  runtimeScope._setHiddenModels(hidden);
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
  runtimeScope._setHiddenModels(hidden);
}

// ══════════════════════════════════════════════════════
//  Model Defaults — fallback model + preset defaults
// ══════════════════════════════════════════════════════

/**
 * Populate the Model Defaults section (fallback model, preset default models).
 * Uses all chat models from all enabled providers as options.
 */
function _populateModelDefaults(cfg) {
  // Collect all chat models through the typed capability-taxonomy owner.
  // Guard: see _renderDropdownVisibility above — same failure mode.
  var _hasCapsDef = (typeof runtimeScope.isChatModel === 'function');
  if (!_hasCapsDef && typeof _warnModelCapsMissing === 'function') _warnModelCapsMissing();
  var chatModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    return _hasCapsDef ? runtimeScope.isChatModel(entry.model) : true;
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
  // shared typed model-display comparator. These previously inherited
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
