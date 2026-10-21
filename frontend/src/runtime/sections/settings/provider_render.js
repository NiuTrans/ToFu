/* ===== migrated source: settings/provider_render.js ===== */
/*
 * 服务商 (Providers) settings tab — provider supply for model-routing v2.
 *
 * Responsibility: everything rendered into ``#settingsTab_api``
 * (static/settings_panels/api.html): ProviderAccess cards with in-card
 * Connection/Credential editors, staged provider metadata edits, and the
 * add-model pickers. Per-offering supply (enable/alias/remove) is managed
 * inline in the per-provider access matrix (settings/access_matrix.js).
 * This file never renders the 模型 (Models) tab — the global, read-only
 * Creator/Model registry lives in settings/model_catalog_tab.js; flows
 * that reload the authority or change creators/models repaint both pages
 * by name via ``_renderModelRoutingTabs()``. Legacy editors are migration
 * input only.
 */

function _setModelRoutingCollectionField(collection, index, field, value, kind) {
  if (!_stgModelRouting || !Array.isArray(_stgModelRouting[collection])) return;
  var row = _stgModelRouting[collection][index];
  if (!row) return;
  if (kind === 'boolean') row[field] = !!value;
  else if (kind === 'number') row[field] = Math.max(0, Number(value) || 0);
  else row[field] = String(value == null ? '' : value);
  _renderProvidersTab();
}

function _modelRoutingPriceLabel(pricing) {
  if (!pricing) return '未设置成交价';
  return (pricing.currency || 'USD') + ' ' + Number(pricing.input || 0) + ' / ' +
    Number(pricing.output || 0) + ' · 每百万 tokens';
}

var _MODEL_CAP_LABELS = {
  text: '文本', thinking: '思考', vision: '视觉',
  cheap: '低价', image_gen: '图像', embedding: '向量',
};

// 上下文窗口按惯例缩写：1000000 → 1M，128000 → 128K，8192 → 8.2K。
function _modelRoutingContextLabel(tokens) {
  var n = Number(tokens) || 0;
  if (n >= 1000000) {
    var m = n / 1000000;
    return (m % 1 === 0 ? String(m) : String(Math.round(m * 10) / 10)) + 'M';
  }
  if (n >= 1000) {
    var k = n / 1000;
    return (k % 1 === 0 ? String(k) : String(Math.round(k * 10) / 10)) + 'K';
  }
  return String(n);
}
function _modelRoutingRefLabel(offering, modelNames) {
  if (offering.identity_state === 'pending_identity') {
    return offering.pending_model_id || offering.offering_id;
  }
  var ref = offering.model || {};
  var key = (ref.creator_id || '') + '::' + (ref.model_id || '');
  return modelNames[key] || ref.model_id || offering.offering_id;
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
  /* Send the compiled references intact. The server inserts missing Models
   * and preserves existing Model settings; provider claims remain scoped to
   * the Offering and are validated against the current Model authority. */
  var extraHeaders = bundle.credential_extra_headers || {};
  /* Static recipe headers are inspectable configuration: they belong on
   * each connection's extra_headers (the 自定义请求头 editor), not folded
   * into the encrypted credential envelope. */
  (draft.connections || []).forEach(function(connection) {
    connection.extra_headers = Object.assign(
      {}, connection.extra_headers || {}, extraHeaders);
  });
  // The compile-only mirror field is not part of ProviderWriteRequest.
  delete draft.credential_extra_headers;
  var secretCredential = (draft.credentials || []).find(function(row) {
    return row.kind !== 'local_identity';
  });
  if (apiKey && secretCredential) {
    draft.credential_secrets = {};
    draft.credential_secrets[secretCredential.credential_id] = JSON.stringify({
      format: 'tofu.credential-secret/v1',
      api_key: apiKey,
      oauth: '',
      extra_headers: {},
    });
  } else {
    /* Keyless add: a non-local credential without a secret_reference is
     * rejected by the v2 domain, so drop the recipe's placeholder credential;
     * the card's 「+ 添加密钥」 mints a real one with full model grants. */
    draft.credentials = (draft.credentials || []).filter(function(row) {
      return row.kind === 'local_identity';
    });
  }
  var created = await Api.modelRouting.createProvider(
    draft, _stgModelRoutingRevision);
  if (!created || !created.provider) throw new Error('服务商接入未能保存');
  _stgModelRoutingRevision = Number(created.revision || _stgModelRoutingRevision);
  await _loadModelRoutingAuthority();
  _renderModelRoutingTabs();
  var card = Array.from(document.querySelectorAll('.stg-provider-card-v2')).find(function(candidate) {
    return candidate.dataset.providerId === String(bundle.provider.provider_id);
  });
  if (card) {
    card.open = true;
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  showToast(t(apiKey ? 'providerCatalog.addedWithKey' : 'providerCatalog.addedWithoutKey'));
  return true;
}

function _showTemplateMenu(btn) {
  runtimeScope._toggleProviderTemplatePicker(btn, _addTemplateProvider, _stageModelRoutingProviderBundle);
}

async function _addTemplateProvider(templateKey) {
  /* Templates add directly: the resulting provider card exposes every
   * editable item (keys, headers, base URL, model supply), so no wizard.
   * The key lands via the card's 「+ 添加密钥」; compile with no
   * selected_model_ids returns the full recipe set. */
  try {
    var result = await Api.providers.compileTemplate(templateKey);
    if (!result || !result.provider_bundle) throw new Error('模板编译未返回接入配置');
    await _stageModelRoutingProviderBundle(result.provider_bundle, '');
  } catch (error) {
    showAlert('添加模板失败：' + String(error && error.message || error));
  }
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
  var modelListPricing = {};
  (documentValue.models || []).forEach(function(model) {
    if (model.list_pricing) {
      modelListPricing[(model.creator_id || '') + '::' + (model.model_id || '')] =
        model.list_pricing;
    }
  });
  var modelLifecycles = {};
  (documentValue.models || []).forEach(function(model) {
    if (model.lifecycle) {
      modelLifecycles[(model.creator_id || '') + '::' + (model.model_id || '')] =
        model.lifecycle;
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
    modelListPricing: modelListPricing,
    modelLifecycles: modelLifecycles,
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
  var html = '';
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
      '<div class="stg-provider-foot">' +
      (primaryConnection ? _renderV2ThinkingFormatField(primaryConnection) : '') +
      '<div class="stg-provider-foot-actions">' +
        '<div class="stg-toggle-row"><span>启用</span>' +
          '<label class="stg-toggle"><input type="checkbox"' + (access.enabled ? ' checked' : '') +
          ' data-tofu-action-change="_setModelRoutingCollectionField(\'provider_accesses\',' +
          context.accessIndex + ',\'enabled\',this.checked,\'boolean\')">' +
          '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span></label>' +
        '</div>' +
        '<button type="button" class="stg-btn-danger" data-provider-id="' +
          escapeHtml(provider.provider_id) + '" ' +
          'data-tofu-action="_deleteModelRoutingProvider(this.dataset.providerId)">删除服务商</button>' +
      '</div></div>' +
      '<div class="stg-models-section">' + _renderAccessMatrix(provider.provider_id) + '</div>' +
      '</div>' +
      '</details>';
  });
  list.innerHTML = html;
  runtimeScope.mountProviderReasoningEditors?.(list, () => _stgModelRouting);
  runtimeScope.mountRequestNameEditors?.(list, {
    document: () => _stgModelRouting,
    // The panel (name + per-request-name headers), not a prompt: a request
    // name may need its own selector header to reach its serving provider.
    // `document` is the staged routing draft in this render function; the
    // panel needs the real DOM host.
    // `context` carries the Connection's headers: they reach this request
    // name too, so the panel must show them as its effective route (and let
    // them be edited) instead of opening an empty form.
    edit: (initial, context) => runtimeScope.openRequestNamePanel({
      host: window.document.body,
      title: t('settings.requestNameEditHeaders'),
      initial: initial,
      inherited: (context && context.inherited) || {},
      sharedWith: (context && context.sharedWith) || 0,
    }),
    route: async function(deployment, wireId) {
      var offering = (_stgModelRouting.offerings || []).find(function(row) {
        return row.offering_id === deployment.offering_id;
      });
      var access = offering && (_stgModelRouting.provider_accesses || []).find(function(row) {
        return row.provider_access_id === offering.provider_access_id;
      });
      if (!access) return;
      var providerContext = _modelRoutingProviderContext(access.provider_id);
      if (!providerContext) return;
      var plans = await _planV2RequestNameFaces([wireId], providerContext);
      var connectionId = _ensureV2FaceConnection(providerContext, plans[wireId]);
      if (connectionId && deployment.connection_id !== connectionId) {
        deployment.connection_id = connectionId;
        deployment.probe_status = 'unprobed';
      }
    },
    alert: (message) => showAlert(message), render: () => _renderProvidersTab(),
  });
  /* Enhance the footer 思维参数格式 select into the unified tofu dropdown so
   * its popup matches the composer popovers. The select stays the change
   * authority; only the popup is rendered as DOM. */
  if (runtimeScope.TofuDropdown) runtimeScope.TofuDropdown.enhance(list);
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

function _v2KeyCardPendingSecret(credentialId) {
  if (typeof _stgPendingCredentialSecrets === 'undefined' ||
      !_stgPendingCredentialSecrets || !credentialId) return '';
  return String(_stgPendingCredentialSecrets[credentialId] || '');
}

/* Client mirror of the server hint contract (first4 + masks + last4), used
 * only for staged values that the server has not hashed yet. */
function _v2MaskedSecret(value) {
  var normalized = String(value || '').trim();
  if (!normalized) return '';
  if (normalized.length <= 8) return '****';
  var hidden = Math.min(normalized.length - 8, 40);
  return normalized.slice(0, 4) + new Array(hidden + 1).join('*') +
    normalized.slice(-4);
}

function _renderV2KeyCard(provider, item, order) {
  var row = item.row;
  var keyHint = String(row.key_hint || '').replace(/^…+/, '');
  var credentialId = String(row.credential_id || '');
  var isApiKey = row.kind === 'api_key';
  var storedDisplay = isApiKey
    ? (keyHint || '••••••••')
    : (row.kind === 'local_identity' ? '本地身份（无需密钥）' : '订阅授权（OAuth）');
  var staged = isApiKey ? _v2KeyCardPendingSecret(credentialId) : '';
  var display = staged ? _v2MaskedSecret(staged) : storedDisplay;
  return '<div class="stg-key-card ' + _v2KeyCardStateClass(provider.provider_id, item) +
    (staged ? ' stg-key-card--staged' : '') +
    '" data-credential-index="' + item.index + '"' +
    ' data-provider-id="' + escapeHtml(provider.provider_id) + '"' +
    ' data-credential-id="' + escapeHtml(credentialId) + '">' +
    '<div class="stg-key-card-edit">' +
      '<span class="stg-keys-idx">#' + (order + 1) + '</span>' +
      '<input class="stg-keys-input" type="text" readonly spellcheck="false" autocomplete="off" ' +
        'value="' + escapeHtml(display) + '" placeholder="sk-…" ' +
        'data-masked="' + escapeHtml(display) + '" ' +
        'data-stored="' + escapeHtml(storedDisplay) + '" ' +
        (isApiKey ? 'data-tofu-action-input="_onV2KeySecretEdit(this)" ' : '') +
        'title="密文保存在服务端，默认只显示首尾识别位">' +
      (isApiKey && credentialId
        ? '<button type="button" class="stg-keys-btn stg-key-reveal" ' +
          'data-tofu-action="_toggleV2KeyReveal(this)" aria-pressed="false" ' +
          'title="' + escapeHtml(t('settings.showHideKeyTitle')) + '">' +
          _V2_KEY_EYE_OPEN + '</button>'
        : '') +
      '<button type="button" class="stg-keys-btn danger" data-provider-id="' +
        escapeHtml(provider.provider_id) + '" data-credential-index="' + item.index + '" ' +
        'data-tofu-action="_deleteV2Credential(this.dataset.providerId,this.dataset.credentialIndex)" ' +
        'title="删除该密钥">✕</button>' +
    '</div>' +
    '<div class="stg-key-card-stats">' + _renderV2KeyCardStats(provider.provider_id, item) + '</div>' +
  '</div>';
}

function _v2KeyDisplaySecret(secret) {
  /* Credentials carrying encrypted extra_headers store a
   * tofu.credential-secret/v1 envelope; the key card reveals only the
   * usable key, never the envelope JSON chrome. */
  try {
    var decoded = JSON.parse(secret);
    if (decoded && decoded.format === 'tofu.credential-secret/v1') {
      var apiKey = String(decoded.api_key || '').trim();
      if (apiKey) return apiKey;
    }
  } catch (error) { /* raw key secrets are not JSON */ }
  return secret;
}

function _markV2KeyCardStaged(card, staged) {
  if (!card || !card.classList) return;
  if (staged) card.classList.add('stg-key-card--staged');
  else card.classList.remove('stg-key-card--staged');
}

function _showV2KeyPlaintext(button, card, input, plaintext) {
  input.value = plaintext;
  // A property, not a data attribute: an attribute would serialize the
  // secret into every DOM snapshot/diagnostic dump.
  input.__revealedSecret = plaintext;
  input.readOnly = false;
  input.title = '可直接编辑；保存后替换该密钥';
  button.setAttribute('aria-pressed', 'true');
  button.innerHTML = _V2_KEY_EYE_OFF;
  input.focus();
  if (typeof input.select === 'function') input.select();
}

function _onV2KeySecretEdit(input) {
  if (!input || typeof input.closest !== 'function' || input.readOnly) return;
  if (typeof _stgPendingCredentialSecrets === 'undefined' ||
      !_stgPendingCredentialSecrets) return;
  var card = input.closest('.stg-key-card');
  if (!card) return;
  var credentialId = card.getAttribute('data-credential-id') || '';
  if (!credentialId) return;
  var value = String(input.value || '');
  var revealed = input.__revealedSecret || '';
  var storedMask = input.getAttribute('data-stored') || '';
  if (value.trim() && value !== revealed) {
    _stgPendingCredentialSecrets[credentialId] = value;
    input.setAttribute('data-masked', _v2MaskedSecret(value));
    _markV2KeyCardStaged(card, true);
  } else {
    delete _stgPendingCredentialSecrets[credentialId];
    input.setAttribute('data-masked', storedMask || '••••••••');
    _markV2KeyCardStaged(card, false);
  }
}

function _toggleV2KeyReveal(button) {
  if (!button || typeof button.closest !== 'function') return;
  var card = button.closest('.stg-key-card');
  var input = card ? card.querySelector('.stg-keys-input') : null;
  if (!input) return;
  if (button.getAttribute('aria-pressed') === 'true') {
    input.value = input.getAttribute('data-masked') || '';
    input.readOnly = true;
    delete input.__revealedSecret;
    input.title = '密文保存在服务端，默认只显示首尾识别位';
    button.setAttribute('aria-pressed', 'false');
    button.innerHTML = _V2_KEY_EYE_OPEN;
    return;
  }
  var credentialId = card.getAttribute('data-credential-id') || '';
  if (!credentialId) return;
  // A staged replacement is already in memory; show it without a round trip.
  var staged = _v2KeyCardPendingSecret(credentialId);
  if (staged) {
    _showV2KeyPlaintext(button, card, input, staged);
    return;
  }
  if (typeof Api === 'undefined' || !Api.modelRouting ||
      !Api.modelRouting.revealCredentialSecret) return;
  button.disabled = true;
  Api.modelRouting.revealCredentialSecret(credentialId).then(function(data) {
    button.disabled = false;
    if (!data || typeof data.secret !== 'string' || !data.secret) {
      showToast(t('settings.keyRevealFailed'));
      return;
    }
    // The user may have typed while the request was in flight; never clobber it.
    if (input.readOnly === false && input.value.trim()) return;
    _showV2KeyPlaintext(button, card, input, _v2KeyDisplaySecret(data.secret));
  });
}

/* ── Per-key runtime stats (today) — classic two-row key card ──
 * Source: GET /api/v4/dispatch/key-stats — daily per-credential health
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

function _v2KeyStatsBucket(providerId, credentialId) {
  var providers = (_v2KeyStatsCache && _v2KeyStatsCache.providers) || {};
  var candidates = [];
  if (providers[providerId]) candidates.push(providerId);
  var suffix = ':' + providerId;
  var names = Object.keys(providers);
  for (var i = 0; i < names.length; i++) {
    if (names[i] !== providerId && names[i].slice(-suffix.length) === suffix) {
      candidates.push(names[i]);
    }
  }
  /* A stale legacy bucket (e.g. persistent overrides recorded pre-v2 under
   * the bare provider id) must not shadow the owner-scoped bucket that
   * actually holds this credential's rows: resolve by content first. */
  if (credentialId) {
    for (var j = 0; j < candidates.length; j++) {
      if (providers[candidates[j]][credentialId]) {
        return { id: candidates[j], keys: providers[candidates[j]] };
      }
    }
  }
  return candidates.length
    ? { id: candidates[0], keys: providers[candidates[0]] }
    : null;
}

function _v2KeyStatRow(providerId, credentialId) {
  var bucket = _v2KeyStatsBucket(providerId, credentialId);
  return bucket ? (bucket.keys[credentialId] || null) : null;
}

/* Namespace for override writes: the bucket already holding this
 * credential's rows always wins (it is the exact id the dispatcher
 * recorded under); otherwise compose from the server-reported
 * key_namespace (owner-scoped since routing v2). A legacy bucket with no
 * rows for the credential must never capture the write — the dispatcher
 * would never read an override recorded under it. */
function _v2KeyNamespace(providerId, credentialId) {
  var bucket = _v2KeyStatsBucket(providerId, credentialId);
  if (bucket && credentialId && bucket.keys[credentialId]) return bucket.id;
  var prefix = (_v2KeyStatsCache && _v2KeyStatsCache.key_namespace) || '';
  if (prefix) return prefix + providerId;
  if (bucket) return bucket.id;
  return providerId;
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

  var metrics = total > 0
    ? '<span class="stg-keystat-metrics">' +
      '<span class="stg-keystat-rate" title="' +
        escapeHtml(t('settings.keyStatRateTip', { succ: succ, total: total })) + '">' +
        srTxt + '</span>' +
      '<span class="stg-keystat-count" title="' + escapeHtml(t('settings.keyStatCountTip')) + '">' +
        escapeHtml(t('settings.keyStatCount', { n: total })) + '</span>' +
      (fail > 0 ? '<span class="stg-keystat-fail" title="' + escapeHtml(t('settings.keyStatFailTip')) + '">' +
        escapeHtml(t('settings.keyStatFail', { n: fail })) + '</span>' : '') +
      (rl429 > 0 ? '<span class="stg-keystat-429" title="' + escapeHtml(t('settings.keyStat429Tip')) + '">' +
        escapeHtml(t('settings.keyStat429', { n: rl429 })) + '</span>' : '') +
      (gw > 0 ? '<span class="stg-keystat-gateway" title="' + escapeHtml(t('settings.keyStatGatewayTip')) + '">' +
        escapeHtml(t('settings.keyStatGateway', { n: gw })) + '</span>' : '') +
      '</span>'
    : '<span class="stg-keystat-none">' + escapeHtml(t('settings.keyStatNoCallsTip')) + '</span>';

  return metrics +
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
  var namespaced = _v2KeyNamespace(providerId, credentialId);
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
  var index = Number(credentialIndex);
  var row = Number.isInteger(index) ? (_stgModelRouting.credentials || [])[index] : null;
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
    if (typeof _stgPendingCredentialSecrets !== 'undefined' && _stgPendingCredentialSecrets) {
      delete _stgPendingCredentialSecrets[row.credential_id];
    }
    _stgModelRoutingRevision = Number(saved.revision || _stgModelRoutingRevision);
    await _loadModelRoutingAuthority();
    _renderProvidersTab();
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
  return '<div class="stg-field stg-thinking-field">' +
    '<label title="默认自动检测 — 仅当端点使用非标准格式时需配置">思维参数格式</label>' +
    '<select data-tofu-dd data-tofu-action-change="_setModelRoutingCollectionField(\'connections\',' +
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
  deployments.splice(deploymentIndex, 1);
  _renderProvidersTab();
}

/* Reuse or stage the Connection that actually serves a request name.
 *
 * A wire id pinned by the template's ``request_faces`` (aigc's kunlun alias)
 * only answers on THAT face: ``/v1`` as a Responses endpoint, with
 * ``x-context-provider: kunlun``. Binding a new request name to "the first
 * enabled Connection" produced a row that could never answer — the reported
 * "added it several times, still fails". The face is a Connection property,
 * so every request name pointing at it shares one. */
function _ensureV2FaceConnection(context, plan) {
  if (!plan || !plan.base_url) return null;
  var accessId = context.access.provider_access_id;
  var url = String(plan.base_url).replace(/\/$/, '');
  var existing = (_stgModelRouting.connections || []).find(function(row) {
    if (row.provider_access_id !== accessId) return false;
    if (String(row.base_url || '').replace(/\/$/, '') !== url) return false;
    if (!plan.face) return true;
    return !row.gateway_namespace || row.gateway_namespace === plan.face;
  });
  var connectionId;
  if (existing) {
    connectionId = existing.connection_id;
    // Fill only what the face declares and the row lacks: an owner-edited
    // protocol/profile is a deliberate override, not a gap.
    if (!existing.protocol && plan.protocol) existing.protocol = plan.protocol;
    if (plan.face && !existing.gateway_namespace) existing.gateway_namespace = plan.face;
    if (plan.responses_profile && !existing.responses_profile) {
      existing.responses_profile = plan.responses_profile;
    }
    if (plan.inference_profile && !existing.inference_profile) {
      existing.inference_profile = plan.inference_profile;
    }
    var headers = existing.extra_headers || {};
    Object.keys(plan.extra_headers || {}).forEach(function(key) {
      if (!(key in headers)) headers[key] = plan.extra_headers[key];
    });
    existing.extra_headers = headers;
  } else {
    connectionId = _newV2StagedId('conn');
    var row = {
      connection_id: connectionId,
      provider_access_id: accessId,
      base_url: url,
      protocol: plan.protocol || 'openai',
      enabled: true,
      priority: 100,
      extra_headers: Object.assign({}, plan.extra_headers || {}),
    };
    if (plan.face) row.gateway_namespace = plan.face;
    if (plan.responses_profile) row.responses_profile = plan.responses_profile;
    if (plan.inference_profile) row.inference_profile = plan.inference_profile;
    _stgModelRouting.connections.push(row);
  }
  // Dispatch refuses a wire whose credential cannot use its Connection.
  (context.credentials || []).forEach(function(item) {
    var credential = item.row;
    if (!credential.authorization) {
      credential.authorization = { connection_ids: [], models: [] };
    }
    var ids = credential.authorization.connection_ids ||
      (credential.authorization.connection_ids = []);
    if (ids.indexOf(connectionId) < 0) ids.push(connectionId);
  });
  return connectionId;
}

async function _planV2RequestNameFaces(names, context) {
  if (typeof runtimeScope.planProviderRequestNameFaces !== 'function') return {};
  return await runtimeScope.planProviderRequestNameFaces(
    names, context.connections.map(function(item) { return item.row.base_url; }));
}

async function _addV2Alias(providerId, offeringId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  var offering = context.offerings.find(function(item) {
    return item.row.offering_id === offeringId;
  });
  if (!offering) return;
  var connection = context.connections.find(function(item) {
    return item.row.enabled !== false;
  }) || context.connections[0];
  if (!connection) return;
  // Panel, not prompt: a request name may need its own selector header
  // (aigc serves kunlunbedrock.* only under x-context-provider), and a
  // string prompt cannot express one. The panel validates against the same
  // header rules the server enforces, so a rejected save cannot happen after
  // a long form.
  if (typeof runtimeScope.openRequestNamePanel !== 'function') {
    showAlert(t('settings.requestNamePanelUnavailable'));
    return;
  }
  var accessOfferingIds = new Set(context.offerings.map(function(item) {
    return item.row.offering_id;
  }));
  var draft = await runtimeScope.openRequestNamePanel({
    host: document.body,
    title: t('settings.matrixAddRequestNameTitle'),
    // Duplicate names are refused INSIDE the panel so the operator can fix
    // the field instead of losing the whole form to an alert.
    apply: function(candidate) {
      var name = String(candidate.wireModelId || '').trim();
      var duplicate = (_stgModelRouting.deployments || []).some(function(row) {
        return accessOfferingIds.has(row.offering_id) && row.wire_model_id === name;
      });
      return duplicate ? t('settings.requestNameDuplicate') : '';
    },
  });
  if (!draft) return;
  var wireModelId = String(draft.wireModelId || '').trim();
  var faces = await _planV2RequestNameFaces([wireModelId], context);
  var faceConnectionId = _ensureV2FaceConnection(context, faces[wireModelId]);
  if (faceConnectionId) connection = { row: { connection_id: faceConnectionId } };
  var targetConnection = (_stgModelRouting.connections || []).find(function(row) {
    return row.connection_id === connection.row.connection_id;
  });
  // Only what this request name adds on top of its Connection: the face
  // already contributes its own selectors, so repeating them here would
  // create a second owner for the same value.
  var headers = {};
  (draft.headers || []).forEach(function(row) {
    var key = String(row.name || '').trim();
    if (!key) return;
    var value = String(row.value || '');
    if (targetConnection && (targetConnection.extra_headers || {})[key] === value) return;
    headers[key] = value;
  });
  var row = {
    deployment_id: 'deployment-' + String(Date.now()).toString(36) +
      Math.random().toString(36).slice(2, 8),
    offering_id: offeringId,
    connection_id: connection.row.connection_id,
    wire_model_id: wireModelId,
    priority: 100,
    identity_confidence: offering.row.identity_state === 'confirmed' && offering.row.model
      ? 'high' : 'pending',
    probe_status: 'unprobed',
  };
  if (Object.keys(headers).length) row.extra_headers = headers;
  _stgModelRouting.deployments.push(row);
  _renderProvidersTab();
  showToast(t('settings.matrixAliasAdded'));
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
}
/* ── 添加模型 picker：目录多选 + 自定义模型 ──
 * Stages offerings + deployments into _stgModelRouting; the global 保存
 * (Api.modelRouting.replace) persists them. New request names inherit the
 * Offering switch and credential grants; unprobed honestly records that
 * creation has not verified reachability. Confirmed offerings additionally need a
 * credential authorization grant per existing credential (routing refuses
 * ungranted refs), appended here deduped; revoke in the matrix.
 */

var _stgModelPickerProviderId = '';
var _stgModelPickerQuery = '';
var _stgModelPickerChecked = new Set();

function _newV2StagedId(prefix) {
  return prefix + '-' + String(Date.now()).toString(36) + '-' +
    Math.random().toString(36).slice(2, 8);
}

function _providerWireModelIds(context) {
  var ids = new Set();
  (context.deployments || []).forEach(function(item) {
    ids.add(String(item.row.wire_model_id || ''));
  });
  return ids;
}

function _providerAddableCatalogModels(context) {
  var offered = new Set();
  context.offerings.forEach(function(item) {
    var row = item.row;
    if (row.identity_state === 'confirmed' && row.model) {
      offered.add((row.model.creator_id || '') + '::' + (row.model.model_id || ''));
    }
  });
  return (_stgModelRouting.models || []).filter(function(model) {
    if (model.lifecycle === 'retired') return false;
    return !offered.has((model.creator_id || '') + '::' + (model.model_id || ''));
  });
}

async function _stageV2AddModels(providerId, catalogModels, customWireIds) {
  // A new browser bundle can be served before the backend restarts. Do not
  // send optional fields until the current server advertises their contract.
  var authorityCapabilities = await Api.modelRouting.get();
  var supportsInheritance = authorityCapabilities.metadata_inheritance_supported === true;
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return 0;
  var connection = context.connections.find(function(item) {
    return item.row.enabled !== false;
  }) || context.connections[0];
  if (!connection) {
    showAlert('请先为该服务商配置 API 地址，再添加模型。');
    return 0;
  }
  var stagedDocument = _stgModelRouting;
  var requestNames = runtimeScope.planProviderRequestNames
    ? await runtimeScope.planProviderRequestNames(catalogModels || [], context.connections.map(item => item.row.base_url)) : {};
  if (_stgModelRouting !== stagedDocument) return 0;
  context = _modelRoutingProviderContext(providerId);
  if (!context) return 0;
  connection = context.connections.find(item => item.row.connection_id === connection.row.connection_id);
  if (!connection) return 0;
  var wireIds = _providerWireModelIds(context);
  var granted = new Set();
  var added = 0;
  // Every request name the picker is about to add, so one template lookup
  // resolves the face each one must travel over.
  var pendingWireIds = [];
  (catalogModels || []).forEach(function(model) {
    (requestNames[model.model_id] || [String(model.model_id || '')])
      .forEach(function(id) { if (id && !wireIds.has(id)) pendingWireIds.push(id); });
  });
  (customWireIds || []).forEach(function(rawId) {
    var wireId = String(rawId || '').trim();
    if (wireId && !wireIds.has(wireId)) pendingWireIds.push(wireId);
  });
  var facePlans = await _planV2RequestNameFaces(pendingWireIds, context);
  if (_stgModelRouting !== stagedDocument) return 0;
  // A face-pinned request name (aigc kunlun) must not inherit the card's
  // default Connection: that endpoint rejects the id outright.
  var connectionForWire = function(wireId) {
    return _ensureV2FaceConnection(context, facePlans[wireId])
      || connection.row.connection_id;
  };
  var pushDeployment = function(offeringId, wireId, confidence) {
    _stgModelRouting.deployments.push({
      deployment_id: _newV2StagedId('deployment'),
      offering_id: offeringId,
      connection_id: connectionForWire(wireId),
      wire_model_id: wireId,
      identity_confidence: confidence,
      probe_status: 'unprobed',
      priority: 100,
    });
    wireIds.add(wireId);
  };
  var grantModel = function(ref) {
    var grantKey = ref.creator_id + '::' + ref.model_id;
    if (granted.has(grantKey)) return;
    granted.add(grantKey);
    context.credentials.forEach(function(item) {
      var credential = item.row;
      if (!credential.authorization) {
        credential.authorization = { connection_ids: [], models: [] };
      }
      var grants = credential.authorization.models || [];
      var exists = grants.some(function(candidate) {
        return String(candidate.creator_id || '') === String(ref.creator_id) &&
          String(candidate.model_id || '') === String(ref.model_id);
      });
      if (!exists) grants.push({ creator_id: ref.creator_id, model_id: ref.model_id });
      credential.authorization.models = grants;
    });
  };
  (catalogModels || []).forEach(function(model) {
    var names = (requestNames[model.model_id] || [String(model.model_id || '')]).filter(id => id && !wireIds.has(id));
    if (!names.length) return;
    var offeringId = _newV2StagedId('offering');
    _stgModelRouting.offerings.push({
      offering_id: offeringId,
      provider_access_id: context.access.provider_access_id,
      identity_state: 'confirmed',
      model: { creator_id: model.creator_id, model_id: model.model_id },
      enabled: true,
      stale: false,
      capabilities: (model.capabilities && model.capabilities.length
        ? model.capabilities : ['text']).slice(),
      context_window: model.context_window || 128000,
      ...(supportsInheritance ? { inherited_model_fields: ['capabilities', 'context_window'] } : {}),
      priority: 100,
    });
    names.forEach(wireId => pushDeployment(offeringId, wireId, 'high'));
    grantModel({ creator_id: model.creator_id, model_id: model.model_id });
    added++;
  });
  (customWireIds || []).forEach(function(rawId) {
    var wireId = String(rawId || '').trim();
    if (!wireId || wireId.length > 256 || /[\u0000-\u001f]/.test(wireId)) return;
    if (wireIds.has(wireId)) return;
    var offeringId = _newV2StagedId('offering');
    _stgModelRouting.offerings.push({
      offering_id: offeringId,
      provider_access_id: context.access.provider_access_id,
      identity_state: 'pending_identity',
      pending_model_id: wireId,
      enabled: true,
      stale: false,
      capabilities: ['text'],
      context_window: 128000,
      ...(supportsInheritance ? { inherited_model_fields: ['capabilities', 'context_window'] } : {}),
      priority: 100,
    });
    pushDeployment(offeringId, wireId, 'pending');
    added++;
  });
  if (added) {
    _renderProvidersTab();
    // Model adds commit immediately (like key/provider edits): the access
    // matrix probes the SERVER document, so a staged-but-unsaved row would
    // 400 with "no cells match the requested scope".
    await _persistModelRoutingNow();
  }
  return added;
}

function _openProviderModelPicker(providerId) {
  var context = _modelRoutingProviderContext(providerId);
  if (!context) return;
  _closeProviderModelPicker();
  _stgModelPickerProviderId = String(providerId);
  _stgModelPickerQuery = '';
  _stgModelPickerChecked = new Set();
  var overlay = document.createElement('div');
  overlay.id = 'stgProviderModelPickerOverlay';
  overlay.className = 'stg-v2-manager-overlay stg-v2-picker-overlay';
  overlay.setAttribute('role', 'presentation');
  var panel = document.createElement('section');
  panel.className = 'stg-v2-manager stg-v2-picker';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-modal', 'true');
  panel.setAttribute('aria-label', '添加模型');
  panel.innerHTML = '<header class="stg-v2-manager-head"><div class="stg-v2-manager-brand">' +
    _brandSvg(_modelRoutingProviderBrand(context), 22) + '<div><strong>' +
    escapeHtml(context.access.display_name || context.provider.name || context.provider.provider_id) +
    '</strong><span>添加模型</span></div></div>' +
    '<button type="button" class="stg-v2-close" data-tofu-action="_closeProviderModelPicker()" aria-label="关闭">×</button></header>' +
    '<div class="stg-v2-manager-body">' +
      '<div class="stg-v2-picker-custom"><label>自定义模型 ID</label>' +
        '<div class="stg-v2-picker-custom-row">' +
          '<input id="stgProviderModelCustomId" type="text" spellcheck="false" autocomplete="off" ' +
            'placeholder="发给服务商的模型 ID，例如 deepseek-v4-flash-tencent">' +
          '<button type="button" class="stg-btn-add" data-tofu-action="_stageV2CustomModelFromPicker()">添加</button>' +
        '</div>' +
        '<p class="stg-v2-picker-hint">' + escapeHtml(t('settings.requestNamePendingIdentity')) + '</p></div>' +
      '<div class="stg-v2-picker-catalog"><label>从模型目录选择</label>' +
        '<input class="stg-v2-picker-search" type="search" placeholder="搜索模型名称或 ID" ' +
          'data-tofu-action-input="_filterProviderModelPicker(this.value)">' +
        '<div class="stg-v2-picker-list" id="stgProviderModelPickerList"></div>' +
        '<div class="stg-v2-picker-foot"><span id="stgProviderModelPickerCount">未选择模型</span>' +
          '<button type="button" class="stg-btn-add" data-tofu-action="_stageV2CheckedCatalogModels()">添加所选</button></div>' +
      '</div>' +
    '</div>';
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  overlay.onclick = function(event) { if (event.target === overlay) _closeProviderModelPicker(); };
  var customInput = panel.querySelector('#stgProviderModelCustomId');
  if (customInput) {
    customInput.addEventListener('keydown', function(event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        _stageV2CustomModelFromPicker();
      }
    });
  }
  _renderProviderModelPickerList();
}

function _closeProviderModelPicker() {
  _stgModelPickerProviderId = '';
  var overlay = document.getElementById('stgProviderModelPickerOverlay');
  if (overlay) overlay.remove();
}

function _filterProviderModelPicker(value) {
  _stgModelPickerQuery = String(value || '');
  _renderProviderModelPickerList();
}

function _toggleProviderModelPick(key, checked) {
  key = String(key || '');
  if (!key) return;
  if (checked) _stgModelPickerChecked.add(key);
  else _stgModelPickerChecked.delete(key);
  _renderProviderModelPickerCount();
}

function _renderProviderModelPickerCount() {
  var count = document.getElementById('stgProviderModelPickerCount');
  if (count) {
    count.textContent = _stgModelPickerChecked.size
      ? '已选择 ' + _stgModelPickerChecked.size + ' 个模型'
      : '未选择模型';
  }
}

function _renderProviderModelPickerList() {
  var list = document.getElementById('stgProviderModelPickerList');
  var context = _modelRoutingProviderContext(_stgModelPickerProviderId);
  if (!list || !context) return;
  var query = _stgModelPickerQuery.trim().toLowerCase();
  var rows = _providerAddableCatalogModels(context).filter(function(model) {
    var haystack = [model.display_name, model.model_id, model.creator_id].join(' ').toLowerCase();
    return !query || haystack.includes(query);
  }).sort(function(left, right) {
    return String(left.display_name || left.model_id).localeCompare(
      String(right.display_name || right.model_id), undefined,
      { numeric: true, sensitivity: 'base' });
  });
  var visible = rows.slice(0, 120);
  list.innerHTML = visible.map(function(model) {
    var key = (model.creator_id || '') + '::' + (model.model_id || '');
    return '<label class="stg-v2-picker-row">' +
      '<input type="checkbox" data-model-key="' + escapeHtml(key) + '"' +
        (_stgModelPickerChecked.has(key) ? ' checked' : '') +
        ' data-tofu-action-change="_toggleProviderModelPick(this.dataset.modelKey,this.checked)">' +
      '<span class="stg-v2-picker-row-icon">' +
        _brandSvg(typeof _modelBrand === 'function'
          ? _modelBrand(model.model_id || '', model.creator_id)
          : _detectBrand((model.creator_id || '') + ' ' + (model.model_id || '')), 16) + '</span>' +
      '<span class="stg-v2-picker-row-id"><strong>' +
        escapeHtml(model.display_name || model.model_id) + '</strong><span>' +
        escapeHtml((model.creator_id || '') + '/' + (model.model_id || '')) + '</span></span>' +
      '<span class="stg-v2-picker-row-price">' +
        escapeHtml(model.list_pricing ? _modelRoutingPriceLabel(model.list_pricing) : '目录未挂牌') +
      '</span></label>';
  }).join('');
  if (!visible.length) {
    list.innerHTML = '<p class="stg-v2-picker-empty">' +
      (rows.length ? '没有匹配的模型。' : '目录中的模型都已添加。') + '</p>';
  }
  _renderProviderModelPickerCount();
}

async function _stageV2CustomModelFromPicker() {
  var input = document.getElementById('stgProviderModelCustomId');
  var wireId = input ? input.value.trim() : '';
  if (!wireId) { if (input) input.focus(); return; }
  var context = _modelRoutingProviderContext(_stgModelPickerProviderId);
  if (!context) return;
  if (wireId.length > 256 || /[\u0000-\u001f]/.test(wireId)) {
    showAlert('模型 ID 无效。');
    return;
  }
  if (_providerWireModelIds(context).has(wireId)) {
    showAlert('该模型 ID 已存在于本服务商。');
    return;
  }
  try {
    if (!await _stageV2AddModels(_stgModelPickerProviderId, [], [wireId])) return;
  } catch (error) {
    showAlert('模型保存失败：' + String(error && error.message || error));
    return;
  }
  input.value = '';
  input.focus();
  _renderProviderModelPickerList();
  showToast('已添加自定义模型。');
}

async function _stageV2CheckedCatalogModels() {
  if (!_stgModelPickerChecked.size) {
    showToast('请先勾选要添加的模型。');
    return;
  }
  var context = _modelRoutingProviderContext(_stgModelPickerProviderId);
  if (!context) return;
  var byKey = {};
  _providerAddableCatalogModels(context).forEach(function(model) {
    byKey[(model.creator_id || '') + '::' + (model.model_id || '')] = model;
  });
  var models = Array.from(_stgModelPickerChecked).map(function(key) {
    return byKey[key];
  }).filter(Boolean);
  var added = 0;
  try {
    added = await _stageV2AddModels(_stgModelPickerProviderId, models, []);
  } catch (error) {
    showAlert('模型保存失败：' + String(error && error.message || error));
  }
  _stgModelPickerChecked = new Set();
  _renderProviderModelPickerList();
  if (added) showToast('已添加 ' + added + ' 个模型。');
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
    await _loadModelRoutingAuthority();
    _renderModelRoutingTabs();
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
    showToast('已添加凭证。');
  } catch (error) {
    showAlert('添加凭证失败：' + String(error && error.message || error));
  }
}

/**
 * Repaint ONLY the Providers tab. The 模型 (Models) tab is not a side effect
 * of this function — authority-reloading flows call ``_renderModelRoutingTabs()``
 * (settings/model_catalog_tab.js) to repaint both pages by name.
 */
function _renderProvidersTab() {
  var list = document.getElementById('stgProviderList');
  if (list) _renderModelRoutingProvidersTab(list);
}
