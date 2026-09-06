/* ===== migrated source: settings/provider_render.js ===== */
/*
 * Model-routing v2 Settings projection.
 *
 * Responsibility: split the v2 authority at the browser boundary. The Model
 * feature receives a fresh Creator/Model-only projection; this retained owner
 * renders ProviderAccess supply and stages provider metadata edits. Model
 * supply (enable/alias/remove) is managed in the per-provider 模型管理
 * overlay. Legacy editors are migration input only.
 */

function _setModelRoutingCollectionField(collection, index, field, value, kind) {
  if (!_stgModelRouting || !Array.isArray(_stgModelRouting[collection])) return;
  var row = _stgModelRouting[collection][index];
  if (!row) return;
  if (kind === 'boolean') row[field] = !!value;
  else if (kind === 'number') row[field] = Math.max(0, Number(value) || 0);
  else row[field] = String(value == null ? '' : value);
  _renderProvidersTab();
  if (_stgProviderManagerId) _renderProviderManagerBody();
}

function _modelRoutingPriceLabel(pricing) {
  if (!pricing) return '未设置成交价';
  return (pricing.currency || 'USD') + ' ' + Number(pricing.input || 0) + ' / ' +
    Number(pricing.output || 0) + ' · 每百万 tokens';
}

function _modelRoutingRefLabel(offering, modelNames) {
  if (offering.identity_state === 'pending_identity') {
    return offering.pending_model_id || offering.offering_id;
  }
  var ref = offering.model || {};
  var key = (ref.creator_id || '') + '::' + (ref.model_id || '');
  return modelNames[key] || ref.model_id || offering.offering_id;
}

let _stgProviderManagerId = '';
let _stgProviderManagerQuery = '';
let _stgProviderManagerLimit = 80;
let _stgModelCatalogQuery = '';

let _providerTemplateRecipes = null;

async function _loadProviderTemplateRecipes() {
  if (Array.isArray(_providerTemplateRecipes)) return _providerTemplateRecipes;
  _providerTemplateRecipes = await Api.providers.templates();
  if (!Array.isArray(_providerTemplateRecipes)) _providerTemplateRecipes = [];
  return _providerTemplateRecipes;
}

function _modelRoutingHasProviderBundle(bundle) {
  if (!_stgModelRouting || !bundle || !bundle.provider) return false;
  var providerId = bundle.provider.provider_id;
  var connectionUrls = new Set((bundle.connections || []).map(function(row) {
    return String(row.base_url || '').replace(/\/$/, '');
  }));
  if ((_stgModelRouting.providers || []).some(function(row) {
    return row.provider_id === providerId;
  })) return true;
  return (_stgModelRouting.connections || []).some(function(row) {
    return connectionUrls.has(String(row.base_url || '').replace(/\/$/, ''));
  });
}

async function _stageModelRoutingProviderBundle(bundle, apiKey) {
  if (!_stgModelRouting || !bundle || !bundle.provider) return false;
  if (_modelRoutingHasProviderBundle(bundle)) {
    showAlert('该服务商或接入点已经存在，请直接编辑现有接入配置。');
    return false;
  }
  var draft = JSON.parse(JSON.stringify(bundle));
  var existingCreators = new Set((_stgModelRouting.creators || []).map(function(row) {
    return row.creator_id;
  }));
  var existingModels = new Set((_stgModelRouting.models || []).map(function(row) {
    return row.creator_id + '::' + row.model_id;
  }));
  draft.creators = (draft.creators || []).filter(function(row) {
    return !existingCreators.has(row.creator_id);
  });
  draft.models = (draft.models || []).filter(function(row) {
    return !existingModels.has(row.creator_id + '::' + row.model_id);
  });
  var extraHeaders = bundle.credential_extra_headers || {};
  var hasSecret = !!apiKey || Object.keys(extraHeaders).length > 0;
  var secretCredential = (draft.credentials || []).find(function(row) {
    return row.kind !== 'local_identity';
  });
  if (secretCredential && !hasSecret) {
    showAlert('请填写 API Key；本地无密钥服务可直接添加。');
    return false;
  }
  if (secretCredential) {
    draft.credential_secrets = {};
    draft.credential_secrets[secretCredential.credential_id] = JSON.stringify({
      format: 'tofu.credential-secret/v1',
      api_key: apiKey || '',
      oauth: '',
      extra_headers: extraHeaders,
    });
  }
  var created = await Api.modelRouting.createProvider(
    draft, _stgModelRoutingRevision);
  if (!created || !created.provider) throw new Error('服务商接入未能保存');
  _stgModelRoutingRevision = Number(created.revision || _stgModelRoutingRevision);
  await _loadModelRoutingAuthority();
  _renderProvidersTab();
  var card = Array.from(document.querySelectorAll('.stg-provider-card-v2')).find(function(candidate) {
    return candidate.dataset.providerId === String(bundle.provider.provider_id);
  });
  if (card) {
    card.open = true;
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  showToast('已添加服务商和模型供给。');
  return true;
}

async function _showTemplateMenu(btn) {
  var existing = document.getElementById('stgTemplateMenu');
  if (existing) { existing.remove(); return; }
  var templates;
  try {
    templates = await _loadProviderTemplateRecipes();
  } catch (error) {
    showAlert('加载服务商模板失败：' + String(error && error.message || error));
    return;
  }
  var menu = document.createElement('div');
  menu.id = 'stgTemplateMenu';
  menu.className = 'stg-template-menu';
  var header = document.createElement('div');
  header.className = 'stg-template-section';
  header.innerHTML = '<span class="stg-template-section-label">服务商模板</span>' +
    '<span class="stg-template-section-desc">选择模型供给并填写凭证</span>';
  menu.appendChild(header);
  var grid = document.createElement('div');
  grid.className = 'stg-template-grid';
  // Recipe-less templates (e.g. the local placeholder) cannot compile a
  // usable provider — local endpoints go through the 本地部署 flow instead.
  templates.filter(function(tpl) {
    return (tpl.offering_recipes || []).length > 0;
  }).forEach(function(tpl) {
    var item = document.createElement('button');
    item.type = 'button';
    item.className = 'stg-template-item';
    item.setAttribute('data-tpl-key', tpl.key);
    item.innerHTML = _brandSvg(tpl.brand || _detectBrand(tpl.name), 20) +
      '<span class="stg-template-info"><span class="stg-template-name">' +
      escapeHtml(tpl.name) + '</span><span class="stg-template-models">' +
      (tpl.offering_recipes || []).length + ' 个模型</span></span>';
    item.onclick = function() {
      menu.remove();
      void _openTemplateWizard(tpl.key);
    };
    grid.appendChild(item);
  });
  menu.appendChild(grid);
  btn.parentElement.style.position = 'relative';
  btn.parentElement.appendChild(menu);
  setTimeout(function() {
    document.addEventListener('click', function closeTemplateMenu(event) {
      if (!menu.isConnected) {
        document.removeEventListener('click', closeTemplateMenu);
      } else if (!menu.contains(event.target) && !btn.contains(event.target)) {
        menu.remove();
        document.removeEventListener('click', closeTemplateMenu);
      }
    });
  }, 0);
}

async function _openTemplateWizard(templateKey) {
  var templates = await _loadProviderTemplateRecipes();
  var template = templates.find(function(row) { return row.key === templateKey; });
  if (!template) return;
  var prior = document.getElementById('stgTplWizard');
  if (prior) prior.remove();
  var recipes = template.offering_recipes || [];
  var overlay = document.createElement('div');
  overlay.id = 'stgTplWizard';
  overlay.className = 'stg-modal-overlay';
  var modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard';
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    _brandSvg(template.brand || _detectBrand(template.name), 18) + ' ' +
    escapeHtml(template.name) + '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><label class="stg-tpl-wizard-keylabel">API Key' +
    '<input type="password" class="stg-tpl-wizard-key" autocomplete="new-password" ' +
    'placeholder="仅加密保存，不写入接入配置"><span class="stg-tpl-wizard-keyhint">' +
    '模板会创建接入点、模型供给和上游部署标识。</span></label>' +
    '<div class="stg-tpl-wizard-toolbar"><input type="search" class="stg-tpl-wizard-search" ' +
    'placeholder="搜索模型"><span class="stg-tpl-wizard-count"></span>' +
    '<button type="button" class="stg-tpl-wizard-link" data-kind="all">全选</button>' +
    '<button type="button" class="stg-tpl-wizard-link" data-kind="none">全不选</button></div>' +
    '<div class="stg-tpl-wizard-list"></div></div>' +
    '<div class="stg-modal-footer"><button type="button" class="stg-btn-secondary">取消</button>' +
    '<button type="button" class="stg-btn-add">添加接入配置</button></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  if (template.category === 'local') {
    var keyLabel = modal.querySelector('.stg-tpl-wizard-keylabel');
    if (keyLabel) keyLabel.style.display = 'none';
  }
  var list = modal.querySelector('.stg-tpl-wizard-list');
  var boxes = [];
  recipes.forEach(function(recipe) {
    var row = document.createElement('label');
    row.className = 'stg-tpl-wizard-row';
    row.setAttribute('data-model-id', recipe.model_id);
    var aliases = Array.from(new Set((recipe.request_ids || []).filter(function(requestId) {
      return requestId && requestId !== recipe.model_id;
    })));
    row.setAttribute('data-search', [recipe.model_id].concat(aliases).join(' '));
    row.innerHTML = '<input type="checkbox" checked><span class="stg-tpl-wizard-identity">' +
      '<span class="stg-tpl-wizard-mid">' + escapeHtml(recipe.model_id) + '</span>' +
      (aliases.length ? '<span class="stg-tpl-wizard-alias">alias · ' +
        escapeHtml(aliases.join(' · ')) + '</span>' : '') + '</span>' +
      '<span class="stg-tpl-wizard-meta">' +
      escapeHtml((recipe.capabilities || []).join(' · ')) + '</span>';
    list.appendChild(row);
    boxes.push(row.querySelector('input'));
  });
  var counter = modal.querySelector('.stg-tpl-wizard-count');
  var addButton = modal.querySelector('.stg-btn-add');
  function refreshCount() {
    var count = boxes.filter(function(box) { return box.checked; }).length;
    counter.textContent = count + ' / ' + boxes.length;
    addButton.disabled = count === 0;
  }
  boxes.forEach(function(box) { box.onchange = refreshCount; });
  modal.querySelector('[data-kind="all"]').onclick = function() {
    boxes.forEach(function(box) { box.checked = true; }); refreshCount();
  };
  modal.querySelector('[data-kind="none"]').onclick = function() {
    boxes.forEach(function(box) { box.checked = false; }); refreshCount();
  };
  modal.querySelector('.stg-tpl-wizard-search').oninput = function(event) {
    var query = event.target.value.trim().toLowerCase();
    Array.from(list.children).forEach(function(row) {
      row.style.display = !query || String(row.getAttribute('data-search')).toLowerCase().includes(query)
        ? '' : 'none';
    });
  };
  function close() { overlay.remove(); }
  overlay.onclick = function(event) { if (event.target === overlay) close(); };
  modal.querySelector('.stg-modal-close').onclick = close;
  modal.querySelector('.stg-btn-secondary').onclick = close;
  addButton.onclick = async function() {
    var selected = boxes.map(function(box, index) {
      return box.checked ? recipes[index].model_id : '';
    }).filter(Boolean);
    addButton.disabled = true;
    addButton.textContent = '正在编译…';
    try {
      var result = await Api.providers.compileTemplate(template.key, selected);
      if (!result || !result.provider_bundle) throw new Error('模板编译未返回接入配置');
      if (await _stageModelRoutingProviderBundle(
        result.provider_bundle,
        modal.querySelector('.stg-tpl-wizard-key').value.trim())) {
        close();
      }
    } catch (error) {
      showAlert('添加模板失败：' + String(error && error.message || error));
    } finally {
      addButton.disabled = false;
      addButton.textContent = '添加接入配置';
    }
  };
  refreshCount();
  modal.querySelector('.stg-tpl-wizard-key').focus();
}

async function addProvider() {
  var baseUrl = await showPrompt(
    '输入 OpenAI 兼容 API Base URL；Tofu 会先探测模型，再创建 v2 接入配置。',
    { placeholder: 'https://api.example.com/v1', title: '添加自定义服务商' });
  if (!baseUrl) return;
  var apiKey = await showPrompt(
    '输入 API Key（本地无密钥服务可留空）',
    { title: '接入凭证' });
  if (apiKey == null) return;
  try {
    var result = await Api.providers.probe(String(baseUrl).trim(), String(apiKey).trim(), '');
    if (!result || !result.provider_bundle) {
      throw new Error(result && (result.error || result.message) || '接入点探测失败');
    }
    await _stageModelRoutingProviderBundle(result.provider_bundle, String(apiKey).trim());
  } catch (error) {
    showAlert('添加自定义服务商失败：' + String(error && error.message || error));
  }
}

function _modelRoutingProviderContext(providerId) {
  if (!_stgModelRouting) return null;
  var documentValue = _stgModelRouting;
  var provider = (documentValue.providers || []).find(function(row) {
    return row.provider_id === providerId;
  });
  var accessIndex = (documentValue.provider_accesses || []).findIndex(function(row) {
    return row.provider_id === providerId;
  });
  var access = (documentValue.provider_accesses || [])[accessIndex];
  if (!provider || !access) return null;
  var accessId = access.provider_access_id;
  var connections = (documentValue.connections || []).map(function(row, index) {
    return { row: row, index: index };
  }).filter(function(item) { return item.row.provider_access_id === accessId; });
  var credentials = (documentValue.credentials || []).map(function(row, index) {
    return { row: row, index: index };
  }).filter(function(item) { return item.row.provider_access_id === accessId; });
  var offerings = (documentValue.offerings || []).map(function(row, index) {
    return { row: row, index: index };
  }).filter(function(item) { return item.row.provider_access_id === accessId; });
  var offeringIds = new Set(offerings.map(function(item) { return item.row.offering_id; }));
  var deployments = (documentValue.deployments || []).map(function(row, index) {
    return { row: row, index: index };
  }).filter(function(item) { return offeringIds.has(item.row.offering_id); });
  var modelNames = {};
  (documentValue.models || []).forEach(function(model) {
    modelNames[(model.creator_id || '') + '::' + (model.model_id || '')] =
      model.display_name || model.model_id;
  });
  var modelReleaseDates = {};
  (documentValue.models || []).forEach(function(model) {
    if (model.release_date) {
      modelReleaseDates[(model.creator_id || '') + '::' + (model.model_id || '')] =
        model.release_date;
    }
  });
  return {
    provider: provider,
    access: access,
    accessIndex: accessIndex,
    connections: connections,
    credentials: credentials,
    offerings: offerings,
    deployments: deployments,
    modelReleaseDates: modelReleaseDates,
    modelNames: modelNames,
  };
}

function _modelRoutingProviderBrand(context) {
  var provider = context.provider;
  var evidence = [provider.name, provider.provider_id].concat(
    context.connections.map(function(item) { return item.row.base_url; })).join(' ');
  var brand = provider.brand === 'oauth'
    ? _detectBrand(evidence)
    : (provider.brand || _detectBrand(evidence));
  if (provider.brand === 'oauth' && brand === 'generic') {
    brand = /codex|chatgpt|openai/i.test(evidence) ? 'openai' :
      (/claude|anthropic/i.test(evidence) ? 'claude' : brand);
  }
  return brand;
}

function _modelRoutingOfferingAliases(context, offering) {
  if (!offering || offering.identity_state !== 'confirmed' || !offering.model) return [];
  var canonicalModelId = String(offering.model.model_id || '');
  return Array.from(new Set(context.deployments.filter(function(item) {
    return item.row.offering_id === offering.offering_id;
  }).map(function(item) {
    return String(item.row.wire_model_id || '');
  }).filter(function(wireModelId) {
    return wireModelId && wireModelId !== canonicalModelId;
  })));
}

function _modelRoutingOfferingAliasRows(context, offering) {
  if (!offering || offering.identity_state !== 'confirmed' || !offering.model) return [];
  var canonicalModelId = String(offering.model.model_id || '');
  var seen = new Set();
  return context.deployments.filter(function(item) {
    if (item.row.offering_id !== offering.offering_id) return false;
    var wireModelId = String(item.row.wire_model_id || '');
    if (!wireModelId || wireModelId === canonicalModelId || seen.has(wireModelId)) return false;
    seen.add(wireModelId);
    return true;
  }).map(function(item) {
    return {
      index: item.index,
      wireModelId: String(item.row.wire_model_id || ''),
      enabled: item.row.enabled !== false,
    };
  });
}

function _renderModelRoutingProvidersTab(list) {
  if (!_stgModelRouting) {
    list.innerHTML = '<p class="stg-empty">' + escapeHtml(
      _stgModelRoutingLoadError
        ? '加载模型路由失败：' + _stgModelRoutingLoadError
        : '正在加载服务商接入配置…') + '</p>';
    return;
  }
  var document = _stgModelRouting;
  var providers = document.providers || [];
  if (!providers.length) {
    list.innerHTML = '<p class="stg-empty">尚未配置服务商。</p>';
    return;
  }

  // Refreshed in place so polling never disturbs an open card.
  var expandedIds = new Set();
  if (typeof list.querySelectorAll === 'function') {
    list.querySelectorAll('details.stg-provider-card-v2[open]').forEach(function(card) {
      expandedIds.add(String(card.getAttribute('data-provider-id') || ''));
    });
  }
  var html = '<div class="stg-v2-intro"><strong>服务商</strong>' +
    '<span>这里管理供给、请求 alias 和接入状态；官方模型身份不会因此改变。</span></div>';
  providers.forEach(function(provider) {
    var context = _modelRoutingProviderContext(provider.provider_id);
    if (!context) return;
    var access = context.access;
    var modelCount = new Set(context.offerings.filter(function(item) {
      return item.row.identity_state === 'confirmed' && !!item.row.model;
    }).map(function(item) {
      var row = item.row;
      return row.model.creator_id + '::' + row.model.model_id;
    })).size;
    var providerBrand = _modelRoutingProviderBrand(context);
    // Classic card head: base_url subtitle + credential/model badges; the red
    // off badge is the only state chip, shown only when disabled.
    var primaryConnection = context.connections.find(function(item) {
      return item.row.enabled !== false && item.row.base_url;
    }) || context.connections.find(function(item) { return item.row.base_url; });
    var headSubtitle = primaryConnection ? primaryConnection.row.base_url : provider.provider_id;
    html += '<details class="stg-provider-card stg-provider-card-v2" data-provider-id="' +
      escapeHtml(provider.provider_id) + '"' +
      (expandedIds.has(String(provider.provider_id)) ? ' open' : '') + '>' +
      '<summary class="stg-provider-head stg-provider-head-v2">' +
        '<div class="stg-provider-icon">' + _brandSvg(providerBrand, 22) + '</div>' +
        '<div class="stg-provider-info"><div class="stg-provider-name">' +
          escapeHtml(access.display_name || provider.name || provider.provider_id) + '</div>' +
          '<div class="stg-provider-url">' + escapeHtml(headSubtitle) + '</div></div>' +
        '<div class="stg-provider-badges">' +
          '<span class="stg-badge">' + context.credentials.length + ' 个凭证</span>' +
          '<span class="stg-badge stg-badge-models">' + modelCount + ' 个模型</span>' +
          (access.enabled ? '' : '<span class="stg-badge off">已停用</span>') + '</div>' +
        '<span class="stg-chevron">▾</span>' +
      '</summary>' +
      '<div class="stg-provider-body">' +
        '<div class="stg-field-grid">' +
          '<div class="stg-field"><label>显示名称</label>' +
            '<input type="text" value="' + escapeHtml(access.display_name || provider.name || '') + '" ' +
            'data-tofu-action-change="_setModelRoutingCollectionField(\'provider_accesses\',' +
            context.accessIndex + ',\'display_name\',this.value,\'string\')"></div>' +
          (primaryConnection ? '<div class="stg-field"><label>API 地址 (Base URL)</label>' +
            '<input type="text" value="' + escapeHtml(primaryConnection.row.base_url) + '" ' +
            'data-tofu-action-change="_setModelRoutingCollectionField(\'connections\',' +
            primaryConnection.index + ',\'base_url\',this.value,\'string\')"></div>' : '') +
        '</div>' +
      _renderV2KeysSection(provider, context) +
      (primaryConnection ? _renderV2HeadersSection(primaryConnection) : '') +
      (primaryConnection ? _renderV2ThinkingFormatField(primaryConnection) : '') +
      '<div class="stg-field-row">' +
        '<div class="stg-toggle-row"><span>启用</span>' +
          '<label class="stg-toggle"><input type="checkbox"' + (access.enabled ? ' checked' : '') +
          ' data-tofu-action-change="_setModelRoutingCollectionField(\'provider_accesses\',' +
          context.accessIndex + ',\'enabled\',this.checked,\'boolean\')">' +
          '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label>' +
        '</div>' +
        '<button type="button" class="stg-btn-danger" data-provider-id="' +
          escapeHtml(provider.provider_id) + '" ' +
          'data-tofu-action="_deleteModelRoutingProvider(this.dataset.providerId)">删除服务商</button>' +
      '</div>' +
      '<div class="stg-models-section">' +
        '<div class="stg-models-header"><span class="stg-models-title">模型列表</span>' +
        '<div class="stg-models-actions">' +
        (modelCount ? '<button type="button" class="stg-btn-add stg-matrix-toggle' +
          (_stgMatrixOpen[provider.provider_id] ? ' active' : '') + '" data-provider-id="' +
          escapeHtml(provider.provider_id) + '" ' +
          'data-tofu-action="_toggleMatrixView(this.dataset.providerId)" title="按凭证 × 模型查看授权矩阵">' +
          (_stgMatrixOpen[provider.provider_id] ? '收起矩阵' : '访问矩阵') + '</button>' : '') +
        (context.offerings.length ? '<button type="button" class="stg-btn-add" data-provider-id="' +
          escapeHtml(provider.provider_id) + '" ' +
          'data-tofu-action="_openProviderManager(this.dataset.providerId)">模型管理</button>' : '') +
        '</div></div>' +
      (_stgMatrixOpen[provider.provider_id]
        ? _renderAccessMatrix(provider.provider_id)
        : '<p class="stg-empty-sm">' + (context.offerings.length
          ? '共 ' + context.offerings.length + ' 个模型供给 — 在「模型管理」中启用、配置别名或移除。'
          : '尚无模型供给。') + '</p>') +
      '</div>' +
      '</div>' +
      '</details>';
  });
  list.innerHTML = html;
  _loadV2KeyStats();
}

/* ── Classic provider-card sections (v0.15.0 look, v2 data) ──
 * Key cards render only the server-held head…tail hint; plaintext enters
 * the DOM solely through the eye toggle, which reads it back from the
 * audited reveal endpoint on a deliberate click and drops it again on
 * hide or re-render. Adding/deleting a key goes through saveProvider
 * immediately because only the server can mint/clean secret references;
 * every other edit here is staged locally and persisted by the global 保存.
 */

var _V2_KEY_EYE_OPEN = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" ' +
  'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><path d="M1.6 8S4.1 3.8 8 3.8 14.4 8 14.4 8 11.9 12.2 8 12.2 1.6 8 1.6 8z"/>' +
  '<circle cx="8" cy="8" r="1.9"/></svg>';
var _V2_KEY_EYE_OFF = '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" ' +
  'stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" ' +
  'aria-hidden="true"><path d="M1.6 8S4.1 3.8 8 3.8 14.4 8 14.4 8 11.9 12.2 8 12.2 1.6 8 1.6 8z"/>' +
  '<circle cx="8" cy="8" r="1.9"/><path d="M2.5 13.5 13.5 2.5"/></svg>';

function _renderV2KeysSection(provider, context) {
  var subscriptionOnly = context.credentials.length > 0 && context.credentials.every(function(item) {
    return item.row.kind === 'oauth' || item.row.kind === 'subscription';
  });
  var canAdd = !subscriptionOnly && context.connections.length > 0;
  var hint = '加密保存在服务端；默认只显示首尾识别位，点击眼睛图标查看明文';
  var html = '<div class="stg-field stg-keys-field" data-provider-id="' +
    escapeHtml(provider.provider_id) + '">' +
    '<div class="stg-keys-header">' +
      '<label style="margin:0;">API 密钥' +
        ' <span class="stg-keys-info" tabindex="0" role="tooltip" aria-label="' + hint +
        '" title="' + hint + '">i</span></label>' +
      (canAdd ? '<button type="button" class="stg-btn-add stg-keys-tb" data-provider-id="' +
        escapeHtml(provider.provider_id) + '" ' +
        'data-tofu-action="_startNewV2ApiKey(this.dataset.providerId)" title="新增一个 API 密钥">+ 添加密钥</button>' : '') +
    '</div>';
  if (!context.credentials.length) {
    html += '<div class="stg-keys-empty">暂无 API 密钥。点击右上角 + 添加。</div>';
  } else {
    html += '<div class="stg-keys-list">' + context.credentials.map(function(item, order) {
      return _renderV2KeyCard(provider, item, order);
    }).join('') + '</div>';
  }
  return html + '</div>';
}

function _renderV2KeyCard(provider, item, order) {
  var row = item.row;
  var keyHint = String(row.key_hint || '').replace(/^…+/, '');
  var credentialId = String(row.credential_id || '');
  var isApiKey = row.kind === 'api_key';
  var display = isApiKey
    ? keyHint
    : (row.kind === 'local_identity' ? '本地身份（无需密钥）' : '订阅授权（OAuth）');
  return '<div class="stg-key-card ' + _v2KeyCardStateClass(provider.provider_id, item) +
    '" data-credential-index="' + item.index + '"' +
    ' data-provider-id="' + escapeHtml(provider.provider_id) + '"' +
    ' data-credential-id="' + escapeHtml(credentialId) + '">' +
    '<div class="stg-key-card-edit">' +
      '<span class="stg-keys-idx">#' + (order + 1) + '</span>' +
      '<input class="stg-keys-input" type="text" readonly spellcheck="false" autocomplete="off" ' +
        'value="' + escapeHtml(display) + '" placeholder="sk-…" ' +
        'data-masked="' + escapeHtml(display) + '" ' +
        'title="密文保存在服务端，默认只显示首尾识别位">' +
      (isApiKey && credentialId
        ? '<button type="button" class="stg-keys-btn stg-key-reveal" ' +
          'data-tofu-action="_toggleV2KeyReveal(this)" aria-pressed="false" ' +
          'title="' + escapeHtml(t('settings.showHideKeyTitle')) + '">' +
          _V2_KEY_EYE_OPEN + '</button>'
        : '') +
      '<button type="button" class="stg-keys-btn danger" data-provider-id="' +
        escapeHtml(provider.provider_id) + '" data-credential-index="' + item.index + '" ' +
        'data-tofu-action="_deleteV2Credential(this.dataset.providerId,Number(this.dataset.credentialIndex))" ' +
        'title="删除该密钥">✕</button>' +
    '</div>' +
    '<div class="stg-key-card-stats">' + _renderV2KeyCardStats(provider.provider_id, item) + '</div>' +
  '</div>';
}

function _toggleV2KeyReveal(button) {
  if (!button || typeof button.closest !== 'function') return;
  var card = button.closest('.stg-key-card');
  var input = card ? card.querySelector('.stg-keys-input') : null;
  if (!input) return;
  if (button.getAttribute('aria-pressed') === 'true') {
    input.value = input.getAttribute('data-masked') || '';
    button.setAttribute('aria-pressed', 'false');
    button.innerHTML = _V2_KEY_EYE_OPEN;
    return;
  }
  var credentialId = card.getAttribute('data-credential-id') || '';
  if (!credentialId || typeof Api === 'undefined' || !Api.modelRouting ||
      !Api.modelRouting.revealCredentialSecret) return;
  button.disabled = true;
  Api.modelRouting.revealCredentialSecret(credentialId).then(function(data) {
    button.disabled = false;
    if (!data || typeof data.secret !== 'string' || !data.secret) {
      showToast(t('settings.keyRevealFailed'));
      return;
    }
    input.value = data.secret;
    button.setAttribute('aria-pressed', 'true');
    button.innerHTML = _V2_KEY_EYE_OFF;
  });
}

/* ── Per-key runtime stats (today) — classic two-row key card ──
 * Source: GET /api/v1/dispatch/key-stats — daily per-credential health
 * keyed by credential_id under the owner-scoped provider namespace
 * (slot.key_stats_provider_id). The card toggle posts an immediate
 * runtime override valid for today only; a durable-disabled credential
 * (v2 document enabled=false) can only be resurrected through the staged
 * document field, because the dispatcher never mints a slot for it.
 */

var _v2KeyStatsCache = null;
var _v2KeyStatsLoading = false;

function _loadV2KeyStats() {
  if (_v2KeyStatsLoading || _v2KeyStatsCache) return;
  if (typeof Api === 'undefined' || !Api.dispatch || !Api.dispatch.keyStats) return;
  _v2KeyStatsLoading = true;
  Api.dispatch.keyStats()
    .then(function(data) {
      _v2KeyStatsCache = (data && typeof data === 'object') ? data : { providers: {} };
    })
    .catch(function() {
      _v2KeyStatsCache = { providers: {} };
    })
    .finally(function() {
      _v2KeyStatsLoading = false;
      _refreshV2KeyStatsDom();
    });
}

function _v2KeyStatsBucket(providerId) {
  var providers = (_v2KeyStatsCache && _v2KeyStatsCache.providers) || {};
  if (providers[providerId]) return { id: providerId, keys: providers[providerId] };
  var suffix = ':' + providerId;
  var names = Object.keys(providers);
  for (var i = 0; i < names.length; i++) {
    if (names[i].slice(-suffix.length) === suffix) {
      return { id: names[i], keys: providers[names[i]] };
    }
  }
  return null;
}

function _v2KeyStatRow(providerId, credentialId) {
  var bucket = _v2KeyStatsBucket(providerId);
  return bucket ? (bucket.keys[credentialId] || null) : null;
}

/* Namespace for override writes: an existing stats bucket always wins (it
 * is the exact id the dispatcher recorded under); otherwise compose from
 * the server-reported key_namespace (owner-scoped since routing v2). */
function _v2KeyNamespace(providerId) {
  var bucket = _v2KeyStatsBucket(providerId);
  if (bucket) return bucket.id;
  var prefix = (_v2KeyStatsCache && _v2KeyStatsCache.key_namespace) || '';
  return prefix ? prefix + providerId : providerId;
}

function _v2KeyCardStateClass(providerId, item) {
  if (!item.row.enabled) return 'stg-keystat-disabled';
  var row = _v2KeyStatRow(providerId, String(item.row.credential_id || ''));
  if (!row) return 'stg-keystat-idle';
  if (row.exhausted && row.override !== true) return 'stg-keystat-exhausted';
  if (!row.enabled) return 'stg-keystat-disabled';
  if (row.auto_disabled) return 'stg-keystat-warn';
  if (row.success_rate == null) return 'stg-keystat-idle';
  var minRate = (_v2KeyStatsCache && _v2KeyStatsCache.min_success_rate) || 0.5;
  if (row.success_rate >= 0.9) return 'stg-keystat-good';
  if (row.success_rate >= minRate) return 'stg-keystat-ok';
  return 'stg-keystat-warn';
}

function _renderV2KeyCardStats(providerId, item) {
  var credentialId = String(item.row.credential_id || '');
  var row = _v2KeyStatRow(providerId, credentialId);
  var effectiveEnabled = !!item.row.enabled && (row ? !!row.enabled : true);

  var total = row ? (row.total || 0) : 0;
  var succ = row ? (row.success || 0) : 0;
  var fail = row ? (row.failure || 0) : 0;
  var rl429 = row ? (row.rate_limited || 0) : 0;
  var gw = row ? (row.gateway_errors || 0) : 0;
  var cons429 = row ? (row.consecutive_429 || 0) : 0;
  var max429 = (_v2KeyStatsCache && _v2KeyStatsCache.max_consecutive_429) || 100;
  var modelStops = row ? Object.keys(row.exhausted_models || {}) : [];
  var srTxt = row && row.success_rate != null
    ? Math.round(row.success_rate * 100) + '%' : '—';

  var badges = '';
  if (row && row.override === false) {
    badges += '<span class="stg-keystat-badge off">' + escapeHtml(t('settings.keyStatOverrideOff')) + '</span>';
  } else if (row && row.override === true) {
    badges += (row.exhausted || modelStops.length)
      ? '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatOverrideVsExhaustedTip')) + '">' + escapeHtml(t('settings.keyStatOverrideVsExhausted')) + '</span>'
      : '<span class="stg-keystat-badge on">' + escapeHtml(t('settings.keyStatOverrideOn')) + '</span>';
  } else if (row && row.last_resort) {
    badges += '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatLastResortTip')) + '">' + escapeHtml(t('settings.keyStatLastResort')) + '</span>';
  } else if (row && row.exhausted) {
    badges += '<span class="stg-keystat-badge warn" title="' + escapeHtml(t('settings.keyStatExhaustedTip')) + '">' + escapeHtml(t('settings.keyStatExhausted')) + '</span>';
  } else if (row && row.auto_disabled) {
    badges += '<span class="stg-keystat-badge warn">' + escapeHtml(t('settings.keyStatAutoOff')) + '</span>';
  }
  if (row && !row.exhausted && row.override == null && modelStops.length) {
    var reasons = modelStops.map(function(model) {
      return model + ': ' + ((row.exhausted_models || {})[model] || '');
    }).join('\n');
    badges += '<span class="stg-keystat-badge warn" title="' +
      escapeHtml(t('settings.keyStatModelExhaustedTip', { reasons: reasons })) + '">' +
      escapeHtml(t('settings.keyStatModelExhausted', { models: modelStops.join('、') })) + '</span>';
  }
  if (row && !row.exhausted && cons429 >= Math.max(10, max429 / 2)) {
    badges += '<span class="stg-keystat-badge warn" title="' +
      escapeHtml(t('settings.keyStat429StreakTip')) + '">' +
      escapeHtml(t('settings.keyStat429Streak', { n: cons429 })) + '</span>';
  }
  if (row && row.last_error && (fail > 0 || row.exhausted)) {
    badges += '<span class="stg-keystat-err" title="' + escapeHtml(row.last_error) + '">' +
      escapeHtml(t('settings.keyStatLastError')) + '</span>';
  }

  var rateTitle = total > 0
    ? t('settings.keyStatRateTip', { succ: succ, total: total })
    : t('settings.keyStatNoCallsTip');
  var countChip = total > 0
    ? '<span class="stg-keystat-count" title="' + escapeHtml(t('settings.keyStatCountTip')) + '">' +
      escapeHtml(t('settings.keyStatCount', { n: total })) + '</span>'
    : '<span class="stg-keystat-count" title="' + escapeHtml(t('settings.keyStatNoCallsTip')) + '">—</span>';

  return '<span class="stg-keystat-metrics">' +
      '<span class="stg-keystat-rate" title="' + escapeHtml(rateTitle) + '">' + srTxt + '</span>' +
      countChip +
      (fail > 0 ? '<span class="stg-keystat-fail" title="' + escapeHtml(t('settings.keyStatFailTip')) + '">' +
        escapeHtml(t('settings.keyStatFail', { n: fail })) + '</span>' : '') +
      (rl429 > 0 ? '<span class="stg-keystat-429" title="' + escapeHtml(t('settings.keyStat429Tip')) + '">' +
        escapeHtml(t('settings.keyStat429', { n: rl429 })) + '</span>' : '') +
      (gw > 0 ? '<span class="stg-keystat-gateway" title="' + escapeHtml(t('settings.keyStatGatewayTip')) + '">' +
        escapeHtml(t('settings.keyStatGateway', { n: gw })) + '</span>' : '') +
    '</span>' +
    badges +
    '<span class="stg-keystat-actions">' +
      '<label class="stg-toggle stg-key-toggle" title="' + escapeHtml(t('settings.keyStatToggleTip')) + '">' +
        '<input type="checkbox"' + (effectiveEnabled ? ' checked' : '') +
          ' data-provider-id="' + escapeHtml(providerId) + '"' +
          ' data-credential-id="' + escapeHtml(credentialId) + '"' +
          ' data-tofu-action-change="_onV2KeyToggle(this.dataset.providerId,this.dataset.credentialId,this.checked)">' +
        '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>' +
      '</label>' +
      (row && row.override != null
        ? '<button type="button" class="stg-btn-link" title="' + escapeHtml(t('settings.keyStatClearOverrideTip')) + '"' +
          ' data-provider-id="' + escapeHtml(providerId) + '"' +
          ' data-credential-id="' + escapeHtml(credentialId) + '"' +
          ' data-tofu-action="_onV2KeyClearOverride(this.dataset.providerId,this.dataset.credentialId)">' +
          escapeHtml(t('settings.keyStatReset')) + '</button>'
        : '') +
    '</span>';
}

function _onV2KeyToggle(providerId, credentialId, enabled) {
  var context = _modelRoutingProviderContext(providerId);
  var item = context ? context.credentials.find(function(candidate) {
    return String(candidate.row.credential_id || '') === String(credentialId);
  }) : null;
  if (item && !item.row.enabled && enabled) {
    // Durable-disabled credential: a runtime override cannot resurrect it
    // (the dispatcher never mints a slot for a disabled credential), so
    // re-enable through the staged document field; the footer 保存 commits.
    _setModelRoutingCollectionField('credentials', item.index, 'enabled', true, 'boolean');
    showToast('已重新启用 — 保存后生效。');
    return;
  }
  _v2KeyOverride(providerId, credentialId, !!enabled);
}

function _onV2KeyClearOverride(providerId, credentialId) {
  _v2KeyOverride(providerId, credentialId, null);
}

function _v2KeyOverride(providerId, credentialId, enabled) {
  if (typeof Api === 'undefined' || !Api.dispatch || !Api.dispatch.keyOverride) return;
  var namespaced = _v2KeyNamespace(providerId);
  Api.dispatch.keyOverride({
    provider_id: namespaced,
    key_name: credentialId,
    enabled: enabled,
  }).then(function(data) {
    if (data && data.row) {
      if (!_v2KeyStatsCache) _v2KeyStatsCache = { providers: {} };
      if (!_v2KeyStatsCache.providers) _v2KeyStatsCache.providers = {};
      if (!_v2KeyStatsCache.providers[namespaced]) _v2KeyStatsCache.providers[namespaced] = {};
      _v2KeyStatsCache.providers[namespaced][credentialId] = data.row;
    } else {
      showToast('密钥切换失败，请稍后重试。');
    }
    _refreshV2KeyStatsDom();
  });
}

function _refreshV2KeyStatsDom() {
  if (typeof document === 'undefined' || typeof document.querySelectorAll !== 'function') return;
  var cards = document.querySelectorAll('.stg-key-card[data-credential-id]');
  for (var i = 0; i < cards.length; i++) {
    var card = cards[i];
    var providerId = card.getAttribute('data-provider-id') || '';
    var credentialId = card.getAttribute('data-credential-id') || '';
    var context = _modelRoutingProviderContext(providerId);
    var item = context ? context.credentials.find(function(candidate) {
      return String(candidate.row.credential_id || '') === credentialId;
    }) : null;
    if (!item) continue;
    var classes = (card.className || '').split(/\s+/).filter(function(name) {
      return name && name.indexOf('stg-keystat-') !== 0;
    });
    classes.push(_v2KeyCardStateClass(providerId, item));
    card.className = classes.join(' ');
    var statsEl = card.querySelector('.stg-key-card-stats');
    if (statsEl) statsEl.innerHTML = _renderV2KeyCardStats(providerId, item);
  }
}

function _startNewV2ApiKey(providerId) {
  var field = document.querySelector(
    '.stg-keys-field[data-provider-id="' + providerId + '"]');
  if (!field) return;
  var existing = field.querySelector('.stg-key-card--new input');
  if (existing) { existing.focus(); return; }
  var list = field.querySelector('.stg-keys-list');
  if (!list) {
    var emptyEl = field.querySelector('.stg-keys-empty');
    if (emptyEl) emptyEl.remove();
    list = document.createElement('div');
    list.className = 'stg-keys-list';
    field.appendChild(list);
  }
  var order = list.querySelectorAll('.stg-key-card').length;
  list.insertAdjacentHTML('beforeend',
    '<div class="stg-key-card stg-key-card--blank stg-key-card--new">' +
      '<div class="stg-key-card-edit">' +
        '<span class="stg-keys-idx">#' + (order + 1) + '</span>' +
        '<input class="stg-keys-input" type="text" spellcheck="false" autocomplete="off" placeholder="sk-…">' +
        '<button type="button" class="stg-keys-btn danger" title="取消">✕</button>' +
      '</div>' +
    '</div>');
  var card = list.lastElementChild;
  var input = card.querySelector('input');
  card.querySelector('.stg-keys-btn').onclick = function() { card.remove(); };
  input.addEventListener('keydown', function(event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      void _commitNewV2ApiKey(providerId, card, input);
    }
  });
  input.addEventListener('blur', function() {
    if (input.value.trim()) void _commitNewV2ApiKey(providerId, card, input);
  });
  input.focus();
}

async function _commitNewV2ApiKey(providerId, card, input) {
  var apiKey = input.value.trim();
  if (!apiKey) { input.focus(); return; }
  var context = _modelRoutingProviderContext(providerId);
  if (!context) { card.remove(); return; }
  input.disabled = true;
  try {
    await _saveNewProviderCredential(context, apiKey);
  } catch (error) {
    input.disabled = false;
    input.focus();
  }
}

async function _deleteV2Credential(providerId, credentialIndex) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var row = (_stgModelRouting.credentials || [])[credentialIndex];
  if (!row || row.provider_access_id !== context.access.provider_access_id) return;
  var order = context.credentials.findIndex(function(item) {
    return item.row.credential_id === row.credential_id;
  });
  if (!await showConfirm(
    '删除密钥 #' + (order + 1) + '？删除立即生效，使用该密钥的轮转会自动跳过它。',
    { danger: true })) return;
  var bundle = _providerBundleForSave(context);
  bundle.credentials = bundle.credentials.filter(function(candidate) {
    return candidate.credential_id !== row.credential_id;
  });
  try {
    var saved = await Api.modelRouting.saveProvider(
      context.provider.provider_id, bundle, _stgModelRoutingRevision);
    if (!saved || !saved.provider) throw new Error('密钥未能删除');
    _stgModelRoutingRevision = Number(saved.revision || _stgModelRoutingRevision);
    await _loadModelRoutingAuthority();
    _renderProvidersTab();
    if (_stgProviderManagerId) _renderProviderManagerBody();
    showToast('已删除密钥。');
  } catch (error) {
    showAlert('删除密钥失败：' + String(error && error.message || error));
  }
}

function _renderV2HeadersSection(connection) {
  var headers = connection.row.extra_headers || {};
  var entries = Object.keys(headers).map(function(name) {
    return [name, headers[name] == null ? '' : String(headers[name])];
  });
  var html = '<div class="stg-field stg-hdr-field" data-connection-index="' + connection.index + '">' +
    '<div class="stg-hdr-header">' +
      '<label style="margin:0;">自定义请求头' +
        ' <span class="stg-hint">（可选 — 每行一对，附加到本服务商的所有请求）</span></label>' +
      '<button type="button" class="stg-btn-add stg-hdr-tb" ' +
        'data-tofu-action="_addV2HeaderRow(' + connection.index + ')" title="新增一行请求头">+ 添加请求头</button>' +
    '</div>';
  if (!entries.length) {
    html += '<div class="stg-hdr-empty">暂无自定义请求头。点击右上角 + 添加。</div>';
  } else {
    html += '<div class="stg-hdr-list">' + entries.map(function(entry) {
      return _renderV2HeaderRow(connection.index, entry[0], entry[1]);
    }).join('') + '</div>';
  }
  return html + '</div>';
}

function _renderV2HeaderRow(connectionIndex, name, value) {
  return '<div class="stg-hdr-row">' +
    '<input type="text" class="stg-hdr-name" data-hdr-field="name" placeholder="Header 名称" ' +
      'spellcheck="false" autocomplete="off" value="' + escapeHtml(name || '') + '" ' +
      'data-tofu-action-change="_onV2HeaderRowEdit(' + connectionIndex + ')">' +
    '<span class="stg-hdr-sep">:</span>' +
    '<input type="text" class="stg-hdr-value" data-hdr-field="value" placeholder="Header 值" ' +
      'spellcheck="false" autocomplete="off" value="' + escapeHtml(value || '') + '" ' +
      'data-tofu-action-change="_onV2HeaderRowEdit(' + connectionIndex + ')">' +
    '<button type="button" class="stg-hdr-btn danger" ' +
      'data-tofu-action="_deleteV2HeaderRow(this,' + connectionIndex + ')" title="删除该请求头">✕</button>' +
  '</div>';
}

function _collectV2HeadersFromDom(connectionIndex) {
  var field = document.querySelector(
    '.stg-hdr-field[data-connection-index="' + connectionIndex + '"]');
  if (!field) return null;
  var out = {};
  Array.from(field.querySelectorAll('.stg-hdr-row')).forEach(function(row) {
    var nameEl = row.querySelector('input[data-hdr-field="name"]');
    var valueEl = row.querySelector('input[data-hdr-field="value"]');
    var name = (nameEl && nameEl.value || '').trim();
    if (name) out[name] = valueEl ? valueEl.value : '';
  });
  return out;
}

function _onV2HeaderRowEdit(connectionIndex) {
  if (!_stgModelRouting || !_stgModelRouting.connections[connectionIndex]) return;
  var collected = _collectV2HeadersFromDom(connectionIndex);
  if (collected === null) return;
  _stgModelRouting.connections[connectionIndex].extra_headers = collected;
}

function _addV2HeaderRow(connectionIndex) {
  var field = document.querySelector(
    '.stg-hdr-field[data-connection-index="' + connectionIndex + '"]');
  if (!field) return;
  var list = field.querySelector('.stg-hdr-list');
  if (!list) {
    var emptyEl = field.querySelector('.stg-hdr-empty');
    if (emptyEl) emptyEl.remove();
    list = document.createElement('div');
    list.className = 'stg-hdr-list';
    field.appendChild(list);
  }
  list.insertAdjacentHTML('beforeend', _renderV2HeaderRow(connectionIndex, '', ''));
  var row = list.lastElementChild;
  var nameInput = row && row.querySelector('input[data-hdr-field="name"]');
  if (nameInput) nameInput.focus();
}

function _deleteV2HeaderRow(btn, connectionIndex) {
  var row = btn && btn.closest('.stg-hdr-row');
  if (row) row.remove();
  _onV2HeaderRowEdit(connectionIndex);
  var field = document.querySelector(
    '.stg-hdr-field[data-connection-index="' + connectionIndex + '"]');
  if (field && !field.querySelectorAll('.stg-hdr-row').length) {
    var list = field.querySelector('.stg-hdr-list');
    if (list) list.remove();
    var hint = document.createElement('div');
    hint.className = 'stg-hdr-empty';
    hint.textContent = '暂无自定义请求头。点击右上角 + 添加。';
    field.appendChild(hint);
  }
}

function _renderV2ThinkingFormatField(connection) {
  var value = String(connection.row.thinking_format || '');
  var options = [
    ['', '自动检测（按模型名称）'],
    ['enable_thinking', 'enable_thinking（LongCat/Qwen 风格）'],
    ['thinking_type', 'thinking.type（Doubao/Claude 风格）'],
    ['reasoning_effort', 'reasoning_effort（Gemini 3.x 风格）'],
    ['none', '不发送思维参数'],
  ];
  return '<div class="stg-field"><label>思维参数格式' +
    ' <span class="stg-hint">（默认自动检测 — 仅当端点使用非标准格式时需配置）</span></label>' +
    '<select data-tofu-action-change="_setModelRoutingCollectionField(\'connections\',' +
    connection.index + ',\'thinking_format\',this.value,\'string\')">' +
    options.map(function(option) {
      return '<option value="' + option[0] + '"' + (value === option[0] ? ' selected' : '') +
        '>' + escapeHtml(option[1]) + '</option>';
    }).join('') + '</select></div>';
}

function _removeV2Alias(deploymentIndex) {
  if (!_stgModelRouting) return;
  var deployments = _stgModelRouting.deployments || [];
  var row = deployments[deploymentIndex];
  if (!row) return;
  var siblings = deployments.filter(function(candidate) {
    return candidate.offering_id === row.offering_id;
  });
  if (siblings.length <= 1) {
    showAlert('每个模型供给至少保留一个上游标识；要移除整个模型请用卡片右下角的 ✕。');
    return;
  }
  deployments.splice(deploymentIndex, 1);
  _renderProvidersTab();
  if (_stgProviderManagerId) _renderProviderManagerBody();
}

async function _addV2Alias(providerId, offeringId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var offering = context.offerings.find(function(item) {
    return item.row.offering_id === offeringId;
  });
  if (!offering) return;
  var alias = await showPrompt(
    '输入该模型的别名（发给服务商的 request ID）。新别名先保持未启用，通过探测后才会参与路由。',
    { title: '添加别名', placeholder: 'deepseek-v4-flash-tencent' });
  alias = String(alias || '').trim();
  if (!alias) return;
  var accessOfferingIds = new Set(context.offerings.map(function(item) {
    return item.row.offering_id;
  }));
  var duplicate = (_stgModelRouting.deployments || []).some(function(row) {
    return accessOfferingIds.has(row.offering_id) && row.wire_model_id === alias;
  });
  if (duplicate) {
    showAlert('该别名已存在于本服务商。');
    return;
  }
  var connection = context.connections.find(function(item) {
    return item.row.enabled !== false;
  }) || context.connections[0];
  if (!connection) return;
  _stgModelRouting.deployments.push({
    deployment_id: 'deployment-' + String(Date.now()).toString(36) + '-' +
      Math.random().toString(36).slice(2, 8),
    offering_id: offeringId,
    connection_id: connection.row.connection_id,
    wire_model_id: alias,
    enabled: false,
    priority: 100,
    identity_confidence: 'pending',
    probe_status: 'unprobed',
  });
  _renderProvidersTab();
  if (_stgProviderManagerId) _renderProviderManagerBody();
  showToast('已添加别名（未启用）— 保存并通过探测后参与路由。');
}

async function _removeV2Offering(providerId, offeringId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var offering = context.offerings.find(function(item) {
    return item.row.offering_id === offeringId;
  });
  if (!offering) return;
  var label = _modelRoutingRefLabel(offering.row, context.modelNames);
  if (!await showConfirm(
    '从「' + (context.access.display_name || context.provider.name || providerId) +
    '」移除模型「' + label + '」的供给？随全局保存生效。',
    { danger: true })) return;
  _stgModelRouting.offerings = (_stgModelRouting.offerings || []).filter(function(row) {
    return row.offering_id !== offeringId;
  });
  _stgModelRouting.deployments = (_stgModelRouting.deployments || []).filter(function(row) {
    return row.offering_id !== offeringId;
  });
  _renderProvidersTab();
  if (_stgProviderManagerId) _renderProviderManagerBody();
}
function _openProviderManager(providerId) {
  _stgProviderManagerId = String(providerId || '');
  _stgProviderManagerQuery = '';
  _stgProviderManagerLimit = 80;
  _renderProviderManager();
}

function _closeProviderManager() {
  _stgProviderManagerId = '';
  var overlay = document.getElementById('stgProviderManagerOverlay');
  if (overlay) overlay.remove();
}

function _renderProviderManager() {
  var prior = document.getElementById('stgProviderManagerOverlay');
  if (prior) prior.remove();
  if (!_stgProviderManagerId) return;
  var context = _modelRoutingProviderContext(_stgProviderManagerId);
  if (!context) { _stgProviderManagerId = ''; return; }
  var overlay = document.createElement('div');
  overlay.id = 'stgProviderManagerOverlay';
  overlay.className = 'stg-v2-manager-overlay';
  overlay.setAttribute('role', 'presentation');
  var panel = document.createElement('section');
  panel.className = 'stg-v2-manager';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-label', '模型管理');
  panel.innerHTML = '<header class="stg-v2-manager-head"><div class="stg-v2-manager-brand">' +
    _brandSvg(_modelRoutingProviderBrand(context), 22) + '<div><strong>' +
    escapeHtml(context.access.display_name || context.provider.name || context.provider.provider_id) +
    '</strong><span>模型管理</span></div></div>' +
    '<button type="button" class="stg-v2-close" data-tofu-action="_closeProviderManager()" aria-label="关闭">×</button></header>' +
    '<div class="stg-v2-manager-body" id="stgProviderManagerBody"></div>';
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  overlay.onclick = function(event) { if (event.target === overlay) _closeProviderManager(); };
  _renderProviderManagerBody();
}

function _renderProviderManagerBody() {
  var body = document.getElementById('stgProviderManagerBody');
  var context = _modelRoutingProviderContext(_stgProviderManagerId);
  if (!body || !context) return;
  body.innerHTML = '<div class="stg-v2-toolbar"><input id="stgProviderModelSearch" type="search" ' +
    'placeholder="搜索官方模型 ID 或别名" value="' + escapeHtml(_stgProviderManagerQuery) +
    '" data-tofu-action-input="_filterProviderManagerModels(this.value)">' +
    '<span>' + context.offerings.length + ' 个模型供给</span></div>' +
    '<div class="stg-v2-list" id="stgProviderModelRows"></div>';
  _renderProviderManagerModelRows(context);
}

function _renderProviderManagerModelRows(context) {
  var list = document.getElementById('stgProviderModelRows');
  if (!list) return;
  var query = _stgProviderManagerQuery.trim().toLowerCase();
  var filtered = context.offerings.filter(function(item) {
    var row = item.row;
    var label = _modelRoutingRefLabel(row, context.modelNames);
    var aliases = _modelRoutingOfferingAliases(context, row);
    return !query || (label + ' ' + (row.pending_model_id || '') + ' ' + aliases.join(' '))
      .toLowerCase().includes(query);
  }).sort(function(left, right) {
    return _modelRoutingRefLabel(left.row, context.modelNames).localeCompare(
      _modelRoutingRefLabel(right.row, context.modelNames), undefined,
      { numeric: true, sensitivity: 'base' });
  });
  var visible = filtered.slice(0, _stgProviderManagerLimit);
  list.innerHTML = visible.map(function(item) {
    var row = item.row;
    var pending = row.identity_state === 'pending_identity';
    var aliasRows = _modelRoutingOfferingAliasRows(context, row);
    var canonicalModelId = pending ? (row.pending_model_id || row.offering_id) : row.model.model_id;
    var releaseDate = pending ? '' : (context.modelReleaseDates[
      (row.model.creator_id || '') + '::' + (row.model.model_id || '')] || '');
    return '<article class="stg-v2-model-row' + (pending ? ' is-pending' : '') +
      (row.enabled ? '' : ' is-disabled') + '">' +
      '<div class="stg-v2-model-identity"><strong>' +
      escapeHtml(canonicalModelId) + '</strong><span>' +
      (pending ? '待确认身份，仅限当前服务商' :
        escapeHtml((row.model.creator_id || '') + '/' + (row.model.model_id || ''))) + '</span></div>' +
      '<div class="stg-v2-model-detail">' +
      '<div class="stg-v2-model-meta">' +
      (row.capabilities || []).map(function(cap) {
        return '<span class="stg-cap ' + escapeHtml(cap) + '">' + escapeHtml(cap) + '</span>';
      }).join('') +
      '<span class="stg-v2-model-stat">上下文 ' + escapeHtml(String(row.context_window || 0)) + '</span>' +
      (releaseDate ? '<span class="stg-v2-model-stat">发布 ' + escapeHtml(releaseDate) + '</span>' : '') +
      '</div>' +
      '<div class="stg-v2-model-price">' + escapeHtml(_modelRoutingPriceLabel(row.actual_pricing)) + '</div>' +
      '<div class="stg-v2-model-aliases">' +
      (aliasRows.length ? '<span class="stg-aliases-label">别名：</span>' +
        aliasRows.map(function(alias) {
          return '<span class="stg-alias-chip' + (alias.enabled ? '' : ' pending') + '"' +
            (alias.enabled ? '' : ' title="未通过探测，暂不参与路由"') + '>' +
            escapeHtml(alias.wireModelId) +
            '<span class="stg-alias-x" data-tofu-action="_removeV2Alias(' + alias.index +
              ')" title="删除该别名">×</span></span>';
        }).join('') : '') +
      (pending ? '' : '<button type="button" class="stg-alias-add" data-provider-id="' +
        escapeHtml(context.provider.provider_id) + '" data-offering-id="' +
        escapeHtml(row.offering_id) + '" ' +
        'data-tofu-action="_addV2Alias(this.dataset.providerId,this.dataset.offeringId)">+ 别名</button>') +
      '</div></div>' +
      '<div class="stg-v2-model-actions">' +
      '<label class="stg-toggle" title="' + (row.enabled ? '点击停用该模型' : '点击启用该模型') + '">' +
      '<input type="checkbox"' + (row.enabled ? ' checked' : '') +
      ' data-tofu-action-change="_setModelRoutingCollectionField(\'offerings\',' +
      item.index + ',\'enabled\',this.checked,\'boolean\')">' +
      '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label>' +
      '<button type="button" class="stg-btn-icon danger" data-provider-id="' +
      escapeHtml(context.provider.provider_id) + '" data-offering-id="' +
      escapeHtml(row.offering_id) + '" ' +
      'data-tofu-action="_removeV2Offering(this.dataset.providerId,this.dataset.offeringId)" title="从该服务商移除该模型供给">✕</button>' +
      '</div></article>';
  }).join('') + (filtered.length > visible.length
    ? '<button type="button" class="stg-v2-more" data-tofu-action="_showMoreProviderModels()">显示更多（剩余 ' +
      (filtered.length - visible.length) + '）</button>' : '');
  if (!visible.length) list.innerHTML = '<p class="stg-empty">没有匹配的模型供给。</p>';
}

function _filterProviderManagerModels(value) {
  _stgProviderManagerQuery = String(value || '');
  _stgProviderManagerLimit = 80;
  var context = _modelRoutingProviderContext(_stgProviderManagerId);
  if (context) _renderProviderManagerModelRows(context);
}

function _showMoreProviderModels() {
  _stgProviderManagerLimit += 80;
  var context = _modelRoutingProviderContext(_stgProviderManagerId);
  if (context) _renderProviderManagerModelRows(context);
}

function _providerBundleForSave(context) {
  return {
    provider: JSON.parse(JSON.stringify(context.provider)),
    provider_access: JSON.parse(JSON.stringify(context.access)),
    connections: context.connections.map(function(item) { return JSON.parse(JSON.stringify(item.row)); }),
    credentials: context.credentials.map(function(item) { return JSON.parse(JSON.stringify(item.row)); }),
    offerings: context.offerings.map(function(item) { return JSON.parse(JSON.stringify(item.row)); }),
    deployments: context.deployments.map(function(item) { return JSON.parse(JSON.stringify(item.row)); }),
    creators: [],
    models: [],
  };
}

async function _deleteModelRoutingProvider(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var name = context.access.display_name || context.provider.name || providerId;
  if (!await showConfirm('删除服务商「' + name + '」？其凭证、接入点与模型供给会一并移除。',
    { danger: true })) return;
  try {
    var result = await Api.modelRouting.deleteProvider(providerId, _stgModelRoutingRevision);
    if (result && result.revision != null) _stgModelRoutingRevision = Number(result.revision);
    if (_stgProviderManagerId === String(providerId || '')) _closeProviderManager();
    await _loadModelRoutingAuthority();
    _renderProvidersTab();
    showToast('已删除服务商。');
  } catch (error) {
    showAlert('删除服务商失败：' + String(error && error.message || error));
  }
}

async function _saveNewProviderCredential(context, apiKey) {
  var credentialId = 'credential-' + String(Date.now()).toString(36) + '-' +
    Math.random().toString(36).slice(2, 8);
  var modelRefs = [];
  var seen = new Set();
  context.offerings.forEach(function(item) {
    var row = item.row;
    if (row.identity_state !== 'confirmed' || !row.model) return;
    var key = row.model.creator_id + '::' + row.model.model_id;
    if (seen.has(key)) return;
    seen.add(key);
    modelRefs.push(JSON.parse(JSON.stringify(row.model)));
  });
  var bundle = _providerBundleForSave(context);
  bundle.credentials.push({
    credential_id: credentialId,
    provider_access_id: context.access.provider_access_id,
    kind: 'api_key',
    secret_reference: '',
    key_hint: '',
    enabled: true,
    authorization: {
      connection_ids: context.connections.map(function(item) { return item.row.connection_id; }),
      models: modelRefs,
    },
    quota_policy: {},
  });
  bundle.credential_secrets = {};
  bundle.credential_secrets[credentialId] = JSON.stringify({
    format: 'tofu.credential-secret/v1', api_key: String(apiKey).trim(), oauth: '', extra_headers: {},
  });
  try {
    var saved = await Api.modelRouting.saveProvider(
      context.provider.provider_id, bundle, _stgModelRoutingRevision);
    if (!saved || !saved.provider) throw new Error('凭证未能保存');
    _stgModelRoutingRevision = Number(saved.revision || _stgModelRoutingRevision);
    await _loadModelRoutingAuthority();
    _renderProvidersTab();
    if (_stgProviderManagerId) _renderProviderManagerBody();
    showToast('已添加凭证。');
  } catch (error) {
    showAlert('添加凭证失败：' + String(error && error.message || error));
  }
}

function _setModelCatalogSearch(value) {
  var owner = runtimeScope._setModelCatalogSearchOwner;
  if (typeof owner === 'function') return owner(value);
  _stgModelCatalogQuery = String(value || '');
  _renderModelCatalogTab();
}

function _renderModelCatalogTab() {
  var list = document.getElementById('stgModelCatalog');
  if (!list) return;
  if (!_stgModelRouting) {
    list.innerHTML = '<p class="stg-empty">' + escapeHtml(_stgModelRoutingLoadError
      ? '加载模型目录失败：' + _stgModelRoutingLoadError : '正在加载模型目录…') + '</p>';
    return;
  }
  // This is an actual data boundary, not just a view convention: the Model
  // feature receives a fresh Creator/Model-only projection, so provider-side
  // fields are unavailable to it even if the authority document contains them.
  var catalogDocument = {
    contract_version: _stgModelRouting.contract_version,
    creators: (_stgModelRouting.creators || []).map(function(creator) {
      return { creator_id: creator.creator_id, name: creator.name };
    }),
    models: (_stgModelRouting.models || []).map(function(model) {
      var pricing = model.list_pricing;
      return {
        creator_id: model.creator_id,
        model_id: model.model_id,
        display_name: model.display_name,
        capabilities: (model.capabilities || []).slice(),
        context_window: model.context_window,
        quality_rank: model.quality_rank,
        list_pricing: pricing ? {
        release_date: model.release_date || undefined,
          input: pricing.input,
          output: pricing.output,
          currency: pricing.currency,
          unit: pricing.unit,
          cache_read: pricing.cache_read,
          cache_write: pricing.cache_write,
        } : undefined,
        lifecycle: model.lifecycle,
      };
    }),
  };
  var owner = runtimeScope._renderModelCatalogPanel;
  if (typeof owner === 'function') {
    owner(catalogDocument);
    return;
  }
  // The typed owner may be absent in an embedded/test shell. Its fallback is
  // still Model-only: never infer model facts from provider supply or aliases.
  var documentValue = catalogDocument;
  var query = _stgModelCatalogQuery.trim().toLowerCase();
  var rows = (documentValue.models || []).filter(function(model) {
    var haystack = [model.display_name, model.model_id, model.creator_id].join(' ').toLowerCase();
    return !query || haystack.includes(query);
  }).sort(function(left, right) {
    return String(left.display_name || left.model_id).localeCompare(
      String(right.display_name || right.model_id), undefined,
      { numeric: true, sensitivity: 'base' });
  });
  var visible = rows.slice(0, 120);
  var input = document.getElementById('stgModelCatalogSearch');
  if (input && input.value !== _stgModelCatalogQuery) input.value = _stgModelCatalogQuery;
  list.innerHTML = '<div class="stg-v2-catalog-count">' + rows.length + ' 个官方模型' +
    (query ? ' 匹配当前搜索' : '') + '</div><div class="stg-v2-catalog-list">' +
    visible.map(function(model) {
      return '<article class="stg-v2-catalog-row"><div class="stg-v2-catalog-icon">' +
        _brandSvg(typeof _modelBrand === 'function'
          ? _modelBrand(model.model_id || '', model.creator_id)
          : _detectBrand((model.creator_id || '') + ' ' + (model.model_id || '')), 18) + '</div>' +
        '<div class="stg-v2-catalog-identity"><strong>' + escapeHtml(model.display_name || model.model_id) +
        '</strong><span>' + escapeHtml(model.creator_id + '/' + model.model_id) + '</span></div></article>';
    }).join('') + '</div>';
}

function _renderProvidersTab() {
  var list = document.getElementById('stgProviderList');
  if (list) _renderModelRoutingProvidersTab(list);
  _renderModelCatalogTab();
}
