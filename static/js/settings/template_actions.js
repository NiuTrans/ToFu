/* ═══════════════════════════════════════════════════════════════════
   settings/template actions — extracted from settings.js (split 2026-05-28)

   Provider template actions: addProvider, _showTemplateMenu, _syncFromTemplate, _discoverModels, _offerTemplateUpdate.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

function _deleteProvider(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  if (!confirm('确定删除服务商“' + (p.name || p.id) + '”及其 ' + (p.models || []).length + ' 个模型吗？')) return;
  _stgProviders.splice(provIdx, 1);
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
}

function addProvider() {
  var id = 'prov_' + Date.now().toString(36);
  _stgProviders.unshift({
    id: id, name: '新服务商', base_url: '', api_keys: [], enabled: true, models: []
  });
  _renderProvidersTab();
  // Expand the new provider (now first card)
  var first = document.querySelector('.stg-provider-card');
  if (first) {
    first.classList.add('expanded');
    first.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var nameInput = first.querySelector('input');
    if (nameInput) { nameInput.select(); nameInput.focus(); }
  }
}

/**
 * Show the provider template dropdown menu anchored to the button.
 * Clicking a template calls addProviderFromTemplate(key).
 */
async function _showTemplateMenu(btn) {
  // Ensure external templates are loaded before showing menu
  await _loadExternalProviderTemplates();

  // Remove any existing menu
  var existing = document.getElementById('stgTemplateMenu');
  if (existing) { existing.remove(); return; }

  var menu = document.createElement('div');
  menu.id = 'stgTemplateMenu';
  menu.className = 'stg-template-menu';

  // ── Group templates by category ──
  var _CATEGORY_META = {
    official: { label: '官方 API',  icon: '🏢', desc: '直连模型厂商' },
    relay:    { label: '中转 API',  icon: '🔀', desc: '聚合多家模型' },
    _other:   { label: '其他',      icon: '📦', desc: '' },
  };
  var _CAT_ORDER = ['official', 'relay', '_other'];
  var grouped = {};
  for (var i = 0; i < _PROVIDER_TEMPLATES.length; i++) {
    var cat = _PROVIDER_TEMPLATES[i].category || '_other';
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(_PROVIDER_TEMPLATES[i]);
  }

  for (var ci = 0; ci < _CAT_ORDER.length; ci++) {
    var catKey = _CAT_ORDER[ci];
    var catItems = grouped[catKey];
    if (!catItems || catItems.length === 0) continue;
    var meta = _CATEGORY_META[catKey] || _CATEGORY_META._other;

    // ── Section header ──
    var header = document.createElement('div');
    header.className = 'stg-template-section';
    header.innerHTML =
      '<span class="stg-template-section-icon">' + meta.icon + '</span>' +
      '<span class="stg-template-section-label">' + meta.label + '</span>' +
      (meta.desc ? '<span class="stg-template-section-desc">' + meta.desc + '</span>' : '');
    menu.appendChild(header);

    // ── Items grid ──
    var grid = document.createElement('div');
    grid.className = 'stg-template-grid';
    for (var j = 0; j < catItems.length; j++) {
      var tpl = catItems[j];
      var item = document.createElement('div');
      item.className = 'stg-template-item';
      item.setAttribute('data-tpl-key', tpl.key);
      item.innerHTML = _brandSvg(tpl.brand, 20) +
        '<div class="stg-template-info">' +
          '<span class="stg-template-name">' + escapeHtml(tpl.name) + '</span>' +
          '<span class="stg-template-models">' + tpl.models.length + ' 个模型</span>' +
        '</div>';
      item.onclick = (function(key) {
        return function() {
          addProviderFromTemplate(key);
          menu.remove();
        };
      })(tpl.key);
      grid.appendChild(item);
    }
    menu.appendChild(grid);
  }

  // Position below button
  btn.parentElement.style.position = 'relative';
  btn.parentElement.appendChild(menu);

  // Close on outside click
  setTimeout(function() {
    document.addEventListener('click', function _closeMenu(e) {
      if (!menu.contains(e.target) && e.target !== btn) {
        menu.remove();
        document.removeEventListener('click', _closeMenu);
      }
    });
  }, 0);
}

// ══════════════════════════════════════════════════════
//  Client-side pricing-tier tag normalization
// ══════════════════════════════════════════════════════
//
// Mirrors lib.llm_dispatch.config.reevaluate_pricing_tags() on the
// client so templates applied from _PROVIDER_TEMPLATES get correct
// 'cheap' / future-tier tags even when the hardcoded JS literal is
// stale.  Defence in depth — the static rewriter in
// debug/reeval_pricing_tags.py + CI check is still the primary
// defense; this ensures the UI never SHOWS a stale tag even if
// someone hand-edits the JS template and forgets to run the rewriter.
//
// Keeps the single threshold pair in sync with the backend:
//   'cheap': input < $3/1M AND output < $15/1M  (strict)
// Add future tiers here AND in lib/llm_dispatch/config.py::PRICING_TIERS.

var _PRICING_TIERS_JS = [
  // [tag, input_max, output_max]  — per $/1M tokens
  ['cheap', 3.0, 15.0],
];
var _MANAGED_TIER_TAGS_JS = new Set(_PRICING_TIERS_JS.map(function(t) { return t[0]; }));

/**
 * Return the set of pricing-tier tags that apply to *modelId*.
 * Uses _modelPricingCache (populated from /api/server-config) for real
 * input/output prices.  Falls back to `blendedCostPer1k * 1000` against
 * the midpoint threshold when only a blended cost is available.
 *
 * Returns an Array (callers typically merge it into a capabilities set).
 */
function _getPricingTiersJS(modelId, blendedCostPer1k) {
  var mp = (typeof _modelPricingCache !== 'undefined' && _modelPricingCache)
    ? _modelPricingCache[modelId] : null;
  var inp = mp && mp.input != null ? +mp.input : null;
  var out = mp && mp.output != null ? +mp.output : null;
  var tags = [];
  for (var i = 0; i < _PRICING_TIERS_JS.length; i++) {
    var tier = _PRICING_TIERS_JS[i];
    var tag = tier[0], inMax = tier[1], outMax = tier[2];
    if (inp != null && out != null) {
      if (inp < inMax && out < outMax) tags.push(tag);
    } else if (blendedCostPer1k != null && blendedCostPer1k > 0) {
      var blended1m = blendedCostPer1k * 1000.0;
      if (blended1m <= (inMax + outMax) / 2.0) tags.push(tag);
    }
  }
  return tags;
}

/**
 * Normalize pricing-tier tags on a model dict shaped like
 * ``{model_id, capabilities, cost, ...}``.  Mutates the capabilities
 * array in place: strips stale managed tags, adds desired ones.
 * Skips non-chat models (image_gen / embedding).
 */
function _normalizeModelPricingTags(m) {
  if (!m || !m.model_id) return;
  var caps = new Set(m.capabilities || []);
  if (caps.has('image_gen') || caps.has('embedding')) return;
  var desired = new Set(_getPricingTiersJS(m.model_id, m.cost));
  var changed = false;
  _MANAGED_TIER_TAGS_JS.forEach(function(tag) {
    if (caps.has(tag) && !desired.has(tag)) { caps.delete(tag); changed = true; }
    if (!caps.has(tag) && desired.has(tag)) { caps.add(tag); changed = true; }
  });
  if (changed) m.capabilities = Array.from(caps);
}

/**
 * Apply _normalizeModelPricingTags to every model in an array.
 */
function _normalizeModelsPricingTags(models) {
  if (!Array.isArray(models)) return;
  for (var i = 0; i < models.length; i++) _normalizeModelPricingTags(models[i]);
}

/**
 * Add a pre-configured provider from a template.
 * Pre-fills base_url and models; user just needs to add their API key.
 */
function addProviderFromTemplate(templateKey) {
  var tpl = null;
  for (var i = 0; i < _PROVIDER_TEMPLATES.length; i++) {
    if (_PROVIDER_TEMPLATES[i].key === templateKey) { tpl = _PROVIDER_TEMPLATES[i]; break; }
  }
  if (!tpl) return;

  // Check if this provider is already added
  for (var j = 0; j < _stgProviders.length; j++) {
    if (_stgProviders[j].base_url === tpl.base_url) {
      if (!confirm(tpl.name + '（相同 API 地址）似乎已添加。要再添加一个吗？')) return;
      break;
    }
  }

  var id = tpl.key + '_' + Date.now().toString(36);
  var models = tpl.models.map(function(m) {
    return {
      model_id: m.model_id,
      aliases: m.aliases || [],
      capabilities: (m.capabilities || ['text']).slice(),
      rpm: m.rpm || 30,
      cost: m.cost || 0.01,
      thinking_default: (m.capabilities || []).indexOf('thinking') >= 0,
    };
  });
  // Defense-in-depth: re-evaluate pricing-tier tags against live pricing
  // so a stale hardcoded template can't sneak an incorrect 'cheap' tag
  // (or a missing one) into a new provider card.
  _normalizeModelsPricingTags(models);

  var newProv = {
    id: id, name: tpl.name, base_url: tpl.base_url,
    balance_url: tpl.balance_url || '',
    brand: tpl.brand || '',
    api_keys: [], enabled: true, models: models,
  };
  if (tpl.extra_headers && Object.keys(tpl.extra_headers).length > 0) {
    newProv.extra_headers = JSON.parse(JSON.stringify(tpl.extra_headers));
  }
  if (tpl.thinking_format) {
    newProv.thinking_format = tpl.thinking_format;
  }
  if (tpl.protocol) {
    newProv.protocol = tpl.protocol;
  }
  _stgProviders.unshift(newProv);
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);

  // Expand the new provider (now first card) and focus the API key textarea
  var first = document.querySelector('.stg-provider-card');
  if (first) {
    first.classList.add('expanded');
    first.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var textarea = first.querySelector('textarea');
    if (textarea) {
      textarea.focus();
      textarea.placeholder = '在此粘贴你的 ' + tpl.name + ' API 密钥';
    }
  }
}

// ── Template Sync: merge new models from matching template into existing provider ──

/**
 * Find the matching template for a provider by base_url or brand+key.
 * Returns the template object, or null if no match.
 */
function _findMatchingTemplate(provider) {
  if (!provider) return null;
  var url = (provider.base_url || '').replace(/\/+$/, '');
  // 1. Exact base_url match
  for (var i = 0; i < _PROVIDER_TEMPLATES.length; i++) {
    var tUrl = (_PROVIDER_TEMPLATES[i].base_url || '').replace(/\/+$/, '');
    if (tUrl && url && tUrl === url) return _PROVIDER_TEMPLATES[i];
  }
  // 2. Fallback: match by brand (if explicitly set from a previous template apply)
  if (provider.brand) {
    for (var j = 0; j < _PROVIDER_TEMPLATES.length; j++) {
      if (_PROVIDER_TEMPLATES[j].brand === provider.brand || _PROVIDER_TEMPLATES[j].key === provider.brand) {
        return _PROVIDER_TEMPLATES[j];
      }
    }
  }
  return null;
}

// ── Template Sync: merge new models from template into existing provider ──

/**
 * Sync models from the matching built-in template into the provider.
 * Adds any models present in the template but missing from the provider.
 * Updates capabilities/cost for existing models if the template has newer info.
 */
async function _syncFromTemplate(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  await _loadExternalProviderTemplates();
  var tpl = _findMatchingTemplate(p);
  if (!tpl) {
    alert('找不到匹配的内置模板。');
    return;
  }
  var existingIds = new Set((p.models || []).map(function(m) { return m.model_id; }));
  var added = 0;
  var updated = 0;
  var aliasesAdded = 0;
  // Aliases the user has on a model but which the template does NOT list.
  // These are usually hand-added and may be dead deployments — surface them
  // so the user can review and prune.
  var userOnlyAliases = [];
  var tplModels = tpl.models || [];
  for (var i = 0; i < tplModels.length; i++) {
    var tm = tplModels[i];
    if (existingIds.has(tm.model_id)) {
      // Update capabilities, cost, and aliases for existing model
      for (var j = 0; j < p.models.length; j++) {
        if (p.models[j].model_id === tm.model_id) {
          var changed = false;
          if (tm.capabilities && JSON.stringify(p.models[j].capabilities) !== JSON.stringify(tm.capabilities)) {
            p.models[j].capabilities = tm.capabilities.slice();
            changed = true;
          }
          if (tm.cost != null && p.models[j].cost !== tm.cost) {
            p.models[j].cost = tm.cost;
            changed = true;
          }
          // ── Alias reconciliation (additive) ──
          // Add any template aliases the user is missing. Do NOT remove
          // user-added aliases automatically (they may be intentional),
          // but record them in userOnlyAliases for the report so the user
          // can review and prune dead ones.
          var existingAliases = (p.models[j].aliases || []).slice();
          var existingAliasSet = new Set(existingAliases);
          var tplAliases = (tm.aliases || []);
          var tplAliasSet = new Set(tplAliases);
          for (var ai = 0; ai < tplAliases.length; ai++) {
            if (!existingAliasSet.has(tplAliases[ai])) {
              existingAliases.push(tplAliases[ai]);
              existingAliasSet.add(tplAliases[ai]);
              aliasesAdded++;
              changed = true;
            }
          }
          p.models[j].aliases = existingAliases;
          for (var ui = 0; ui < existingAliases.length; ui++) {
            if (!tplAliasSet.has(existingAliases[ui])) {
              userOnlyAliases.push(tm.model_id + ' → ' + existingAliases[ui]);
            }
          }
          if (changed) updated++;
          break;
        }
      }
      continue;
    }
    p.models.push({
      model_id: tm.model_id,
      aliases: (tm.aliases || []).slice(),
      capabilities: (tm.capabilities || ['text']).slice(),
      rpm: tm.rpm || 30,
      cost: tm.cost || 0.01,
      thinking_default: (tm.capabilities || []).indexOf('thinking') >= 0,
    });
    added++;
  }
  // Defense-in-depth: re-evaluate pricing-tier tags on the entire
  // provider after merge.  Catches both newly-added entries and any
  // existing entries whose caps were overwritten above.
  _normalizeModelsPricingTags(p.models);
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
  var msg = '模板同步完成：';
  var parts = [];
  if (added > 0) parts.push('新增 ' + added + ' 个模型');
  if (updated > 0) parts.push('更新 ' + updated + ' 个模型');
  if (aliasesAdded > 0) parts.push('补全 ' + aliasesAdded + ' 个别名');
  msg += parts.length ? parts.join('，') : '所有模型已是最新，无需更新。';
  if (userOnlyAliases.length > 0) {
    msg += '\n\n⚠ 以下别名是你手动添加的（模板中没有），请确认它们在网关上确实存在；不存在的会一直返回 HTTP 400：\n  • ' +
      userOnlyAliases.join('\n  • ');
  }
  msg += '\n\n记得点击「保存」按钮来保存更改。';
  alert(msg);
}

// ── Model Auto-Discovery ──

async function _discoverModels(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;

  var baseUrl = (p.base_url || '').trim();
  var apiKey = (p.api_keys && p.api_keys[0]) || '';

  if (!baseUrl) {
    alert('请先设置 API 地址 (Base URL) 再进行模型发现。');
    return;
  }
  if (!apiKey) {
    alert('请先添加至少一个 API 密钥再进行模型发现。');
    return;
  }

  var modelsPath = (p.models_path || '').trim();

  // Find the discover button and show loading state
  var cards = document.querySelectorAll('.stg-provider-card');
  var card = cards[provIdx];
  var btn = card ? card.querySelector('button[onclick*="_discoverModels"]') : null;
  var oldText = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = '⏳ 发现中…';
  }

  try {
    var data = await Api.providers.discoverModels(baseUrl, apiKey, modelsPath || '');
    if (!data) {
      alert('发现失败 (网络/超时)');
      return;
    }

    if (!data.ok) {
      alert('发现失败: ' + (data.error || '未知错误'));
      return;
    }

    var discovered = data.models || [];
    if (discovered.length === 0) {
      alert('在 ' + baseUrl + ' 未找到模型');
      return;
    }

    // Merge: add only models not already present
    if (!p.models) p.models = [];
    var existing = new Set(p.models.map(function(m) { return m.model_id; }));
    var added = 0;
    for (var i = 0; i < discovered.length; i++) {
      if (!existing.has(discovered[i].model_id)) {
        p.models.push(discovered[i]);
        existing.add(discovered[i].model_id);
        added++;
      }
    }

    _renderProvidersTab();
    // Expand the provider to show results
    var newCards = document.querySelectorAll('.stg-provider-card');
    if (newCards[provIdx]) newCards[provIdx].classList.add('expanded');

    var nCheap = discovered.filter(function(m) { return (m.capabilities || []).indexOf('cheap') >= 0; }).length;
    var msg = '✅ 发现 ' + discovered.length + ' 个模型（' + nCheap + ' 个标记为低价）。\n' +
              '新增 ' + added + ' 个模型' + (added < discovered.length ? '，' + (discovered.length - added) + ' 个已存在。' : '。');
    alert(msg);

    // Offer to persist discovered models into the hardcoded template
    var tpl = _findMatchingTemplate(p);
    if (tpl && added > 0) {
      _offerTemplateUpdate(tpl.key, p.models);
    }

  } catch (e) {
    alert('发现出错: ' + e.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }
}

// ── Persist Discovered Models into Hardcoded Template ──

/**
 * Offer to update the hardcoded provider template with the current model list.
 * Called after discovery finds new models for a template-matched provider.
 */
async function _offerTemplateUpdate(templateKey, models) {
  var ok = confirm(
    '是否将当前模型列表（' + models.length + ' 个）写入源码模板？\n\n' +
    '这样新部署时就自带最新模型，无需再次发现。'
  );
  if (!ok) return;

  try {
    var data = await Api.providers.updateTemplate(templateKey, models);
    if (data && data.ok) {
      alert('✅ 模板已更新：' + data.model_count + ' 个模型写入 ' + (data.updated_files || []).join(', ') + '。');
    } else {
      alert('模板更新失败: ' + (data.error || '未知错误'));
    }
  } catch (e) {
    alert('模板更新出错: ' + e.message);
  }
}

