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
    container.innerHTML = '<p class="stg-empty">未找到图片生成模型。请在服务商中添加具有 <code>image_gen</code> 能力的模型。</p>';
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

  // Group by brand (same logic as _renderDropdownVisibility)
  var grouped = {};
  for (var i = 0; i < unique.length; i++) {
    var entry = unique[i];
    var brandHint = (entry.provider.name || '') + ' ' + (entry.provider.base_url || '') + ' ' + entry.model.model_id;
    var bkey = entry.provider.brand || _detectBrand(brandHint);
    if (!grouped[bkey]) grouped[bkey] = { name: entry.provider.name || bkey, models: [] };
    grouped[bkey].models.push(entry.model);
  }

  var brandNames = {
    claude:'Claude', openai:'OpenAI', gemini:'Gemini', qwen:'Qwen', doubao:'Doubao',
    minimax:'MiniMax', deepseek:'DeepSeek', grok:'Grok', mistral:'Mistral', glm:'GLM',
    meituan:'Meituan', generic:'Other',
  };

  var html = '';
  for (var brand in grouped) {
    var group = grouped[brand];
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

  // Collect all chat models from all enabled providers (exclude image_gen / embedding)
  var allModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    var caps = entry.model.capabilities || ['text'];
    for (var c = 0; c < caps.length; c++) {
      if (caps[c] === 'image_gen' || caps[c] === 'embedding') return false;
    }
    return true;
  });

  if (allModels.length === 0) {
    container.innerHTML = '<p class="stg-empty">未配置聊天模型。请先添加服务商。</p>';
    return;
  }

  // Load hidden set from server config (synced at openSettings)
  var hidden = new Set((_serverConfig && _serverConfig.hidden_models) || []);

  // Group by provider brand
  var grouped = {};
  for (var i = 0; i < allModels.length; i++) {
    var entry = allModels[i];
    var brandHint = (entry.provider.name || '') + ' ' + (entry.provider.base_url || '') + ' ' + entry.model.model_id;
    var bkey = entry.provider.brand || _detectBrand(brandHint);
    if (!grouped[bkey]) grouped[bkey] = { name: entry.provider.name || bkey, models: [] };
    grouped[bkey].models.push(entry.model);
  }

  var html = '';
  var brandNames = {
    claude:'Claude', openai:'OpenAI', gemini:'Gemini', qwen:'Qwen', doubao:'Doubao',
    minimax:'MiniMax', deepseek:'DeepSeek', grok:'Grok', mistral:'Mistral', glm:'GLM',
    meituan:'Meituan', generic:'Other',
  };

  for (var brand in grouped) {
    var group = grouped[brand];
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
  // Collect all chat models (exclude image_gen / embedding)
  var chatModels = _getAllModels().filter(function(entry) {
    if (entry.provider.enabled === false) return false;
    var caps = entry.model.capabilities || ['text'];
    for (var c = 0; c < caps.length; c++) {
      if (caps[c] === 'image_gen' || caps[c] === 'embedding') return false;
    }
    return true;
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

  // Read saved model_defaults from config
  var defaults = (cfg && cfg.model_defaults) || {};

  // Populate each select element
  var selectors = [
    { id: 'settingFallbackModel',  key: 'fallback_model',  emptyLabel: '（禁用自动回退）' },
    { id: 'settingDefaultModel',   key: 'default_model',   emptyLabel: '（使用环境变量）' },

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
      customOpt.textContent = savedVal + ' (未注册)';
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

