/* ===== migrated source: settings/provider_render.js ===== */
/*
 * Model-routing v2 Settings projection.
 *
 * Responsibility: split the v2 authority at the browser boundary. The Model
 * feature receives a fresh Creator/Model-only projection; this retained owner
 * renders ProviderAccess supply, stages provider metadata edits, and queues
 * credential-secret replacements. Legacy editors are migration input only.
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

function _queueModelRoutingCredentialSecret(index, value) {
  if (!_stgModelRouting || !_stgModelRouting.credentials[index]) return;
  var credentialId = _stgModelRouting.credentials[index].credential_id;
  if (value) _stgPendingCredentialSecrets[credentialId] = value;
  else delete _stgPendingCredentialSecrets[credentialId];
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
let _stgProviderManagerTab = 'models';
let _stgProviderManagerQuery = '';
let _stgProviderManagerLimit = 80;
let _stgProviderDiagnosticLimit = 80;
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
  templates.forEach(function(tpl) {
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
    addButton.disabled = recipes.length > 0 && count === 0;
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
  return {
    provider: provider,
    access: access,
    accessIndex: accessIndex,
    connections: connections,
    credentials: credentials,
    offerings: offerings,
    deployments: deployments,
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

function _modelRoutingEligibleProviderCounts(documentValue) {
  var accessById = new Map((documentValue.provider_accesses || []).map(function(access) {
    return [access.provider_access_id, access];
  }));
  var deploymentsByOffering = new Map();
  (documentValue.deployments || []).forEach(function(deployment) {
    var rows = deploymentsByOffering.get(deployment.offering_id) || [];
    rows.push(deployment);
    deploymentsByOffering.set(deployment.offering_id, rows);
  });
  var providersByModel = new Map();
  (documentValue.offerings || []).forEach(function(offering) {
    if (offering.identity_state !== 'confirmed' || !offering.model ||
        offering.enabled === false || offering.stale === true) return;
    var access = accessById.get(offering.provider_access_id);
    if (!access || access.enabled === false) return;
    var hasHealthyDeployment = (deploymentsByOffering.get(offering.offering_id) || []).some(function(deployment) {
      return deployment.enabled !== false && deployment.probe_status === 'passed';
    });
    if (!hasHealthyDeployment) return;
    var identity = offering.model.creator_id + '::' + offering.model.model_id;
    var providerIds = providersByModel.get(identity) || new Set();
    providerIds.add(access.provider_id);
    providersByModel.set(identity, providerIds);
  });
  return new Map(Array.from(providersByModel, function(entry) {
    return [entry[0], entry[1].size];
  }));
}

function _modelRoutingProviderModelRows(context, eligibleProviderCounts, limit) {
  var byIdentity = new Map();
  context.offerings.forEach(function(item) {
    var offering = item.row;
    if (offering.identity_state !== 'confirmed' || !offering.model) return;
    var identity = offering.model.creator_id + '::' + offering.model.model_id;
    if (byIdentity.has(identity)) return;
    byIdentity.set(identity, {
      model: offering.model,
      offeringIndex: item.index,
      enabled: offering.enabled !== false,
      capabilities: (offering.capabilities || []).slice(),
      contextWindow: offering.context_window || 0,
      pricing: offering.actual_pricing,
      aliases: _modelRoutingOfferingAliases(context, offering),
      eligibleProviderCount: eligibleProviderCounts.get(identity) || 0,
    });
  });
  var rows = Array.from(byIdentity.values()).sort(function(left, right) {
    return String(left.model.model_id).localeCompare(String(right.model.model_id), undefined, {
      numeric: true, sensitivity: 'base',
    });
  });
  return { rows: rows.slice(0, limit), total: rows.length };
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
  var eligibleProviderCounts = _modelRoutingEligibleProviderCounts(document);
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
    var modelRows = _modelRoutingProviderModelRows(context, eligibleProviderCounts, 6);
    var subscriptionOnly = context.credentials.length > 0 && context.credentials.every(function(item) {
      return item.row.kind === 'oauth' || item.row.kind === 'subscription';
    });
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
          '<div class="stg-field"><label>名称</label>' +
            '<input type="text" value="' + escapeHtml(access.display_name || provider.name || '') + '" ' +
            'data-tofu-action-change="_setModelRoutingCollectionField(\'provider_accesses\',' +
            context.accessIndex + ',\'display_name\',this.value,\'string\')"></div>' +
          (primaryConnection ? '<div class="stg-field"><label>Base URL</label>' +
            '<input type="text" value="' + escapeHtml(primaryConnection.row.base_url) + '" ' +
            'data-tofu-action-change="_setModelRoutingCollectionField(\'connections\',' +
            primaryConnection.index + ',\'base_url\',this.value,\'string\')"></div>' : '') +
        '</div>' +
      // Credential rows keep the classic one-per-row look; key hints and
      // secret references stay inside the manager modal, never on the card.
      (context.credentials.length || context.connections.length
        ? '<div class="stg-models-section">' +
          '<div class="stg-models-header"><span class="stg-models-title">凭证</span>' +
          (subscriptionOnly || !context.connections.length ? '' :
            '<button type="button" class="stg-btn-add" data-provider-id="' +
            escapeHtml(provider.provider_id) + '" ' +
            'data-tofu-action="_addProviderCredential(this.dataset.providerId)">+ 添加 API Key</button>') +
          '</div>' +
          (subscriptionOnly
            ? '<p class="stg-empty-sm">该接入使用订阅登录，授权请在“订阅登录”中管理。</p>'
            : (context.credentials.length ? '<div class="stg-model-list">' +
              context.credentials.map(function(item, order) {
                var row = item.row;
                var authorization = row.authorization || {};
                return '<div class="stg-mcard stg-v2-cred">' +
                  '<div class="stg-mcard-body">' +
                    '<div class="stg-mcard-main"><span class="stg-mcard-id">凭证 ' + (order + 1) + '</span>' +
                      '<span class="stg-cap">' + escapeHtml(row.kind) + '</span></div>' +
                    '<div class="stg-mcard-caps"><span class="stg-mcard-stat">授权 ' +
                      (authorization.connection_ids || []).length + ' 个接入点 · ' +
                      (authorization.models || []).length + ' 个官方模型</span></div>' +
                    (row.kind === 'local_identity' ? '' :
                      '<input type="password" class="stg-v2-cred-secret" autocomplete="new-password" ' +
                      'placeholder="替换凭证 · 保持留空" ' +
                      'data-tofu-action-input="_queueModelRoutingCredentialSecret(' + item.index + ',this.value)">') +
                  '</div>' +
                  '<div class="stg-mcard-actions"><label class="stg-v2-inline-check">' +
                    '<input type="checkbox" ' + (row.enabled ? 'checked ' : '') +
                    'data-tofu-action-change="_setModelRoutingCollectionField(\'credentials\',' +
                    item.index + ',\'enabled\',this.checked,\'boolean\')">启用</label></div>' +
                '</div>';
              }).join('') + '</div>'
            : '<p class="stg-empty-sm">尚无凭证。</p>')) +
        '</div>' : '') +
      '<div class="stg-models-section">' +
        '<div class="stg-models-header"><span class="stg-models-title">模型列表</span>' +
        (modelRows.total ? '<button type="button" class="stg-btn-add" data-provider-id="' +
          escapeHtml(provider.provider_id) + '" ' +
          'data-tofu-action="_openProviderManager(this.dataset.providerId,\'models\')">管理全部 ' +
          modelRows.total + ' 个模型</button>' : '') +
        '</div>' +
      (!modelRows.total ? '<p class="stg-empty-sm">尚无已确认的模型供给。</p>' :
        '<div class="stg-model-list">' + modelRows.rows.map(function(previewRow) {
          var canonicalId = previewRow.model.model_id;
          var modelBrand = _detectBrand((previewRow.model.creator_id || '') + ' ' + canonicalId);
          return '<div class="stg-mcard' + (previewRow.enabled ? '' : ' disabled') + '">' +
            '<div class="stg-mcard-icon">' + _brandSvg(modelBrand, 18) + '</div>' +
            '<div class="stg-mcard-body">' +
              '<div class="stg-mcard-main"><span class="stg-mcard-id">' + escapeHtml(canonicalId) + '</span>' +
                (previewRow.eligibleProviderCount > 1
                  ? '<span class="stg-provider-cross-candidate">跨 Provider 候选</span>' : '') +
              '</div>' +
              '<div class="stg-mcard-caps">' +
                previewRow.capabilities.map(function(cap) {
                  return '<span class="stg-cap ' + escapeHtml(cap) + '">' + escapeHtml(cap) + '</span>';
                }).join('') +
                '<span class="stg-mcard-stat">' +
                  escapeHtml(previewRow.model.creator_id + '/' + canonicalId) + '</span>' +
                (previewRow.contextWindow
                  ? '<span class="stg-mcard-stat">上下文 ' + previewRow.contextWindow + '</span>' : '') +
              '</div>' +
              '<div class="stg-mcard-pricing">' +
                escapeHtml(_modelRoutingPriceLabel(previewRow.pricing)) + '</div>' +
              (previewRow.aliases.length
                ? '<div class="stg-mcard-aliases"><span class="stg-provider-model-alias">alias · ' +
                  escapeHtml(previewRow.aliases.join(' · ')) + '</span></div>' : '') +
            '</div>' +
            '<div class="stg-mcard-actions"><label class="stg-v2-inline-check">' +
              '<input type="checkbox" ' + (previewRow.enabled ? 'checked ' : '') +
              'data-tofu-action-change="_setModelRoutingCollectionField(\'offerings\',' +
              previewRow.offeringIndex + ',\'enabled\',this.checked,\'boolean\')">启用</label></div>' +
          '</div>';
        }).join('') + '</div>' +
        (modelRows.total > modelRows.rows.length
          ? '<button type="button" class="stg-provider-model-more" data-provider-id="' +
            escapeHtml(provider.provider_id) + '" ' +
            'data-tofu-action="_openProviderManager(this.dataset.providerId,\'models\')">查看全部 ' +
            modelRows.total + ' 个模型</button>' : '')) +
      '</div>' +
      '</div>' +
      '<div class="stg-provider-v2-foot">' +
        '<label class="stg-v2-switch"><span>启用</span><input type="checkbox" ' +
          (access.enabled ? 'checked ' : '') +
          'data-tofu-action-change="_setModelRoutingCollectionField(\'provider_accesses\',' +
          context.accessIndex + ',\'enabled\',this.checked,\'boolean\')"></label>' +
        '<span class="stg-v2-foot-spacer"></span>' +
        '<button type="button" class="stg-btn-danger" data-provider-id="' +
          escapeHtml(provider.provider_id) + '" ' +
          'data-tofu-action="_deleteModelRoutingProvider(this.dataset.providerId)">删除服务商</button>' +
        '<button type="button" class="stg-v2-manage" data-provider-id="' +
          escapeHtml(provider.provider_id) + '" data-tofu-action="_openProviderManager(this.dataset.providerId)">' +
          '管理</button>' +
      '</div></details>';
  });
  list.innerHTML = html;
}

function _openProviderManager(providerId, tabName) {
  _stgProviderManagerId = String(providerId || '');
  _stgProviderManagerTab = tabName || 'models';
  _stgProviderManagerQuery = '';
  _stgProviderManagerLimit = 80;
  _stgProviderDiagnosticLimit = 80;
  _renderProviderManager();
}

function _closeProviderManager() {
  _stgProviderManagerId = '';
  var overlay = document.getElementById('stgProviderManagerOverlay');
  if (overlay) overlay.remove();
}

function _switchProviderManagerTab(tabName) {
  _stgProviderManagerTab = tabName;
  _stgProviderManagerQuery = '';
  _stgProviderManagerLimit = 80;
  _stgProviderDiagnosticLimit = 80;
  _renderProviderManager();
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
  panel.setAttribute('aria-label', '管理服务商');
  var tabs = [
    ['models', '可用模型'],
    ['credentials', '凭证'],
    ['connections', '接入点'],
    ['diagnostics', '路由诊断'],
  ];
  panel.innerHTML = '<header class="stg-v2-manager-head"><div class="stg-v2-manager-brand">' +
    _brandSvg(_modelRoutingProviderBrand(context), 22) + '<div><strong>' +
    escapeHtml(context.access.display_name || context.provider.name || context.provider.provider_id) +
    '</strong><span>接入配置</span></div></div>' +
    '<button type="button" class="stg-v2-close" data-tofu-action="_closeProviderManager()" aria-label="关闭">×</button></header>' +
    '<nav class="stg-v2-manager-tabs" aria-label="服务商管理">' +
    tabs.map(function(tab) {
      return '<button type="button" class="' +
        (tab[0] === _stgProviderManagerTab ? 'active' : '') +
        '" data-manager-tab="' + tab[0] +
        '" data-tofu-action="_switchProviderManagerTab(this.dataset.managerTab)">' +
        tab[1] + '</button>';
    }).join('') + '</nav><div class="stg-v2-manager-body" id="stgProviderManagerBody"></div>';
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  overlay.onclick = function(event) { if (event.target === overlay) _closeProviderManager(); };
  _renderProviderManagerBody();
}

function _renderProviderManagerBody() {
  var body = document.getElementById('stgProviderManagerBody');
  var context = _modelRoutingProviderContext(_stgProviderManagerId);
  if (!body || !context) return;
  if (_stgProviderManagerTab === 'models') {
    body.innerHTML = '<div class="stg-v2-toolbar"><input id="stgProviderModelSearch" type="search" ' +
      'placeholder="搜索官方模型 ID" value="' + escapeHtml(_stgProviderManagerQuery) +
      '" data-tofu-action-input="_filterProviderManagerModels(this.value)">' +
      '<span>' + context.offerings.length + ' 个模型供给</span></div>' +
      '<div class="stg-v2-list" id="stgProviderModelRows"></div>';
    _renderProviderManagerModelRows(context);
    return;
  }
  if (_stgProviderManagerTab === 'credentials') {
    var subscriptionOnly = context.credentials.length > 0 && context.credentials.every(function(item) {
      return item.row.kind === 'oauth' || item.row.kind === 'subscription';
    });
    body.innerHTML = '<div class="stg-v2-body-head"><div><strong>凭证</strong>' +
      '<span>同一服务商内会先轮转可用凭证。</span></div>' +
      (subscriptionOnly ? '' : '<button type="button" class="stg-btn-add" ' +
        'data-tofu-action="_addProviderCredential()">+ 添加 API Key</button>') + '</div>' +
      (subscriptionOnly ? '<p class="stg-v2-note">该接入使用订阅登录，授权请在“订阅登录”中管理。</p>' : '') +
      '<div class="stg-v2-list">' + context.credentials.map(function(item, order) {
        var row = item.row;
        var authorization = row.authorization || {};
        return '<article class="stg-v2-detail-row"><div class="stg-v2-detail-main"><strong>凭证 ' +
          (order + 1) + '</strong><span>' + escapeHtml(row.kind) +
          (row.key_hint ? ' · ' + escapeHtml(row.key_hint) : '') + '</span></div>' +
          '<div class="stg-v2-detail-meta">授权 ' +
          (authorization.connection_ids || []).length + ' 个接入点 · ' +
          (authorization.models || []).length + ' 个官方模型</div>' +
          (row.kind === 'local_identity' ? '' : '<label class="stg-v2-secret-field">替换凭证' +
            '<input type="password" autocomplete="new-password" placeholder="保持留空" ' +
            'data-tofu-action-input="_queueModelRoutingCredentialSecret(' + item.index + ',this.value)"></label>') +
          '<label class="stg-v2-inline-check"><input type="checkbox" ' +
          (row.enabled ? 'checked ' : '') +
          'data-tofu-action-change="_setModelRoutingCollectionField(\'credentials\',' +
          item.index + ',\'enabled\',this.checked,\'boolean\')">启用</label></article>';
      }).join('') + '</div>';
    return;
  }
  if (_stgProviderManagerTab === 'connections') {
    body.innerHTML = '<div class="stg-v2-body-head"><div><strong>接入点</strong>' +
      '<span>只在运维服务商时需要修改。</span></div></div><div class="stg-v2-list">' +
      context.connections.map(function(item, order) {
        var row = item.row;
        return '<article class="stg-v2-detail-row"><div class="stg-v2-detail-main"><strong>接入点 ' +
          (order + 1) + '</strong><span>' + escapeHtml(row.protocol) + '</span></div>' +
          '<label class="stg-v2-wide-field">Base URL<input value="' + escapeHtml(row.base_url) + '" ' +
          'data-tofu-action-change="_setModelRoutingCollectionField(\'connections\',' +
          item.index + ',\'base_url\',this.value,\'string\')"></label>' +
          '<label class="stg-v2-inline-check"><input type="checkbox" ' +
          (row.enabled ? 'checked ' : '') +
          'data-tofu-action-change="_setModelRoutingCollectionField(\'connections\',' +
          item.index + ',\'enabled\',this.checked,\'boolean\')">启用</label></article>';
      }).join('') + '</div>';
    return;
  }
  var visibleDeployments = context.deployments.slice(0, _stgProviderDiagnosticLimit);
  body.innerHTML = '<div class="stg-v2-diagnostic-note"><strong>上游部署标识</strong>' +
    '<span>这些是发给服务商的真实 request ID，只用于探测和排障，普通聊天不会锁定它们。</span></div>' +
    '<div class="stg-v2-list">' + visibleDeployments.map(function(item) {
      var row = item.row;
      var offering = context.offerings.find(function(candidate) {
        return candidate.row.offering_id === row.offering_id;
      });
      return '<article class="stg-v2-detail-row stg-v2-diagnostic-row"><div class="stg-v2-detail-main"><strong>' +
        escapeHtml(offering ? _modelRoutingRefLabel(offering.row, context.modelNames) : row.offering_id) +
        '</strong><span>' + escapeHtml(row.probe_status) + ' · ' +
        escapeHtml(row.identity_confidence) + '</span></div>' +
        '<label class="stg-v2-wide-field">上游标识<input value="' + escapeHtml(row.wire_model_id) + '" ' +
        'data-tofu-action-change="_setModelRoutingCollectionField(\'deployments\',' +
        item.index + ',\'wire_model_id\',this.value,\'string\')"></label>' +
        '<div class="stg-v2-detail-meta">接入点：' + escapeHtml(row.connection_id) + '</div>' +
        '<label class="stg-v2-inline-check"><input type="checkbox" ' +
        (row.enabled ? 'checked ' : '') +
        'data-tofu-action-change="_setModelRoutingCollectionField(\'deployments\',' +
        item.index + ',\'enabled\',this.checked,\'boolean\')">启用此上游标识</label></article>';
    }).join('') + '</div>' +
    (context.deployments.length > visibleDeployments.length
      ? '<button type="button" class="stg-v2-more" data-tofu-action="_showMoreProviderDiagnostics()">显示更多（剩余 ' +
        (context.deployments.length - visibleDeployments.length) + '）</button>' : '');
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
    var aliases = _modelRoutingOfferingAliases(context, row);
    var canonicalModelId = pending ? (row.pending_model_id || row.offering_id) : row.model.model_id;
    return '<article class="stg-v2-model-row' + (pending ? ' is-pending' : '') + '">' +
      '<div class="stg-v2-model-identity"><strong>' +
      escapeHtml(canonicalModelId) + '</strong><span>' +
      (pending ? '待确认身份，仅限当前服务商' :
        escapeHtml((row.model.creator_id || '') + '/' + (row.model.model_id || ''))) + '</span>' +
      (aliases.length ? '<span class="stg-v2-model-alias">alias · ' +
        escapeHtml(aliases.join(' · ')) + '</span>' : '') + '</div>' +
      '<div class="stg-v2-model-meta">' +
      escapeHtml((row.capabilities || []).join(' · ') || '未声明能力') +
      '<span>上下文 ' + escapeHtml(String(row.context_window || 0)) + '</span></div>' +
      '<div class="stg-v2-model-price">' + escapeHtml(_modelRoutingPriceLabel(row.actual_pricing)) + '</div>' +
      '<label class="stg-v2-inline-check"><input type="checkbox" ' +
      (row.enabled ? 'checked ' : '') +
      'data-tofu-action-change="_setModelRoutingCollectionField(\'offerings\',' +
      item.index + ',\'enabled\',this.checked,\'boolean\')">启用</label></article>';
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

function _showMoreProviderDiagnostics() {
  _stgProviderDiagnosticLimit += 80;
  _renderProviderManagerBody();
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

async function _addProviderCredential(providerId) {
  var context = _modelRoutingProviderContext(providerId || _stgProviderManagerId);
  if (!context || !context.connections.length) return;
  var apiKey = await showPrompt('输入新的 API Key。它会独立加密保存，并授权给该服务商的当前模型。', {
    title: '添加凭证', placeholder: 'sk-…',
  });
  if (!apiKey) return;
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
    if (_stgProviderManagerId) _openProviderManager(context.provider.provider_id, 'credentials');
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
        _brandSvg(_detectBrand((model.creator_id || '') + ' ' + (model.model_id || '')), 18) + '</div>' +
        '<div class="stg-v2-catalog-identity"><strong>' + escapeHtml(model.display_name || model.model_id) +
        '</strong><span>' + escapeHtml(model.creator_id + '/' + model.model_id) + '</span></div></article>';
    }).join('') + '</div>';
}

function _renderProvidersTab() {
  var list = document.getElementById('stgProviderList');
  if (list) _renderModelRoutingProvidersTab(list);
  _renderModelCatalogTab();
}
