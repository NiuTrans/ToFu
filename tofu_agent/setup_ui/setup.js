(() => {
  'use strict';

  const text = {
    en: {
      controlPlane: 'Provider control plane', connecting: 'Connecting', ready: 'Ready', setupRequired: 'Setup required', locked: 'Read-only',
      eyebrow: 'ONE-TIME MODEL SETUP', title: 'Configure the model once.\nKeep app code clean.',
      subtitle: 'Choose a Provider, discover its models, and save a managed default. Every application can then call Tofu with only messages.',
      appsNeed: 'APPLICATIONS ONLY NEED', noModelConfig: 'No Provider key or model name in downstream services.',
      unlockTitle: 'Unlock remote settings', unlockCopy: 'Paste the tofu-agent Bearer token. It stays only in this tab and is never saved by the browser.',
      serverToken: 'Server token', unlock: 'Unlock', chooseProvider: 'Choose a Provider',
      chooseProviderCopy: 'Start from a known endpoint or use any OpenAI-compatible service.', managedDefault: 'MANAGED DEFAULT',
      newProvider: 'New Provider', notConfigured: 'Not configured', endpoint: 'API endpoint',
      endpointHelp: 'Paste a base URL or a full /chat/completions URL; Tofu normalizes it.', apiKey: 'API key', show: 'Show', hide: 'Hide',
      keyOptional: 'May be empty for a local model server.', clearKey: 'Clear the saved key', defaultModel: 'Default model',
      modelHelp: 'Discover the catalogue, or type the exact wire model id.', discover: 'Discover models', test: 'Test connection',
      waiting: 'Waiting for configuration', remove: 'Remove', save: 'Save Provider', encryptedTitle: 'Encrypted locally, never returned',
      encryptedCopy: 'Provider secrets are encrypted on disk. This page receives only a short key hint; saved keys and custom header values are never sent back to the browser.',
      forgetToken: 'Forget server token', storageBoundary: 'No database · No ChatUI application frontend',
      savedKey: 'Saved key {hint}; leave blank to keep it.', configured: 'Configured', templateSelected: '{name} template selected.',
      discovering: 'Reading the Provider model catalogue…', discovered: 'Discovered {count} models. Choose one and test it.',
      testing: 'Sending a small real completion…', saving: 'Encrypting and applying the Provider…', saved: 'Provider saved and active for new runs.',
      removed: 'Provider removed. Agent calls now require setup or a request-level Provider.', confirmRemove: 'Remove the saved Provider from tofu-agent?',
      tokenForgotten: 'Server token forgotten from this tab.', authFailed: 'The server token is missing or invalid.',
      lockedCopy: 'This Provider is controlled by environment variables or command-line arguments. Remove that override and restart to edit it here.',
    },
    zh: {
      controlPlane: '提供商控制面板', connecting: '正在连接', ready: '已就绪', setupRequired: '需要配置', locked: '只读配置',
      eyebrow: '一次配置模型', title: '模型只配置一次，\n业务代码保持干净。',
      subtitle: '选择提供商、发现模型并保存默认项。之后所有应用调用 Tofu 时都只需要传消息。',
      appsNeed: '业务应用只需要', noModelConfig: '下游服务不再保存模型密钥和模型名。',
      unlockTitle: '解锁远程设置', unlockCopy: '粘贴 tofu-agent 的 Bearer Token。它只保留在当前标签页内，浏览器不会保存。',
      serverToken: '服务 Token', unlock: '解锁', chooseProvider: '选择提供商',
      chooseProviderCopy: '从常用端点开始，或者使用任意 OpenAI-compatible 服务。', managedDefault: '托管默认模型',
      newProvider: '新提供商', notConfigured: '尚未配置', endpoint: 'API 端点',
      endpointHelp: '可以粘贴 Base URL 或完整的 /chat/completions 地址，Tofu 会自动规范化。', apiKey: 'API 密钥', show: '显示', hide: '隐藏',
      keyOptional: '本地模型服务可以留空。', clearKey: '清除已保存密钥', defaultModel: '默认模型',
      modelHelp: '先发现模型目录，也可以直接输入精确的模型 ID。', discover: '发现模型', test: '测试连接',
      waiting: '等待配置', remove: '移除', save: '保存提供商', encryptedTitle: '本地加密，绝不回显',
      encryptedCopy: 'Provider 密钥在磁盘中加密保存。页面只能拿到短提示；已保存密钥和自定义请求头值不会返回浏览器。',
      forgetToken: '忘记服务 Token', storageBoundary: '无数据库 · 不包含 ChatUI 应用前端',
      savedKey: '已保存密钥 {hint}；留空即可保留。', configured: '已配置', templateSelected: '已选择 {name} 模板。',
      discovering: '正在读取提供商模型目录…', discovered: '发现 {count} 个模型，请选择一个并测试。',
      testing: '正在发送一次最小真实生成请求…', saving: '正在加密保存并热应用提供商…', saved: '提供商已保存，新任务立即生效。',
      removed: '提供商已移除；Agent 调用现在需要重新配置或请求级 Provider。', confirmRemove: '确定从 tofu-agent 中移除已保存的提供商吗？',
      tokenForgotten: '已从当前标签页忘记服务 Token。', authFailed: '服务 Token 缺失或不正确。',
      lockedCopy: '当前 Provider 由环境变量或命令行参数管理。移除对应覆盖并重启后，才能在这里编辑。',
    },
  };

  const byId = (id) => document.getElementById(id);
  const state = {
    language: (navigator.language || '').toLowerCase().startsWith('zh') ? 'zh' : 'en',
    token: '', snapshot: null, selectedTemplate: '', busy: false,
  };

  class ApiError extends Error {
    constructor(message, status, body) { super(message); this.status = status; this.body = body; }
  }

  function t(key, values = {}) {
    let value = text[state.language][key] || text.en[key] || key;
    Object.entries(values).forEach(([name, replacement]) => {
      value = value.replace(`{${name}}`, String(replacement));
    });
    return value;
  }

  function applyLanguage() {
    document.documentElement.lang = state.language === 'zh' ? 'zh-CN' : 'en';
    document.querySelectorAll('[data-i18n]').forEach((node) => {
      node.textContent = t(node.dataset.i18n);
    });
    byId('languageButton').textContent = state.language === 'zh' ? 'EN' : '中文';
    renderTemplates();
    renderSnapshot();
  }

  function headers(hasBody = false) {
    const value = { Accept: 'application/json' };
    if (hasBody) value['Content-Type'] = 'application/json';
    if (state.token) value.Authorization = `Bearer ${state.token}`;
    return value;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { ...headers(options.body !== undefined), ...(options.headers || {}) },
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = body?.error?.message || `HTTP ${response.status}`;
      throw new ApiError(message, response.status, body);
    }
    return body;
  }

  function setConnection(kind, label) {
    const badge = byId('connectionBadge');
    badge.className = `status-badge is-${kind}`;
    badge.lastElementChild.textContent = label;
  }

  function setMessage(kind, message) {
    const box = byId('statusMessage');
    box.className = `status-message${kind ? ` is-${kind}` : ''}`;
    box.lastElementChild.textContent = message;
  }

  function setBusy(busy) {
    state.busy = busy;
    document.querySelectorAll('#providerForm button, #providerForm input').forEach((control) => {
      control.disabled = busy || !state.snapshot?.editable;
    });
    byId('settingsSurface').setAttribute('aria-busy', String(busy));
  }

  function renderTemplates() {
    const grid = byId('templateGrid');
    if (!grid || !state.snapshot) return;
    grid.replaceChildren();
    state.snapshot.templates.forEach((template) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `template-button${state.selectedTemplate === template.id ? ' is-selected' : ''}`;
      button.dataset.templateId = template.id;
      button.dataset.accent = template.accent;
      button.disabled = !state.snapshot.editable || state.busy;
      const name = document.createElement('strong');
      name.textContent = template.name;
      const description = document.createElement('span');
      description.textContent = state.language === 'zh' ? template.description_zh : template.description;
      button.append(name, description);
      button.addEventListener('click', () => selectTemplate(template));
      grid.appendChild(button);
    });
  }

  function selectTemplate(template) {
    const previous = byId('baseUrl').value.trim().replace(/\/+$/, '');
    const next = template.base_url;
    state.selectedTemplate = template.id;
    byId('providerName').textContent = template.name;
    byId('baseUrl').value = next;
    if (previous !== next) {
      byId('model').value = '';
      byId('modelList').replaceChildren();
      if (state.snapshot?.provider && previous === state.snapshot.provider.base_url) {
        byId('apiKeyHint').textContent = t('keyOptional');
      }
    }
    renderTemplates();
    setMessage('', t('templateSelected', { name: template.name }));
    if (!next) byId('baseUrl').focus(); else byId('apiKey').focus();
  }

  function renderSnapshot() {
    if (!state.snapshot) return;
    const provider = state.snapshot.provider;
    const ready = state.snapshot.ready;
    setConnection(
      ready ? 'ready' : (state.snapshot.editable ? 'error' : 'loading'),
      ready ? t('ready') : (state.snapshot.editable ? t('setupRequired') : t('locked')),
    );
    byId('settingsSurface').classList.remove('is-disabled');
    byId('settingsSurface').setAttribute('aria-busy', 'false');
    byId('providerStatus').textContent = provider ? t('configured') : t('notConfigured');
    byId('providerStatus').classList.toggle('is-ready', Boolean(provider));
    byId('deleteButton').classList.toggle('is-hidden', !provider || !state.snapshot.editable);
    byId('lockedNotice').classList.toggle('is-hidden', state.snapshot.editable);
    byId('lockedNotice').textContent = state.snapshot.editable ? '' : t('lockedCopy');
    byId('loadError').classList.toggle('is-hidden', !state.snapshot.load_error);
    byId('loadError').textContent = state.snapshot.load_error || '';

    if (provider) {
      byId('providerName').textContent = provider.model || t('configured');
      byId('baseUrl').value = provider.base_url;
      byId('model').value = provider.model;
      byId('apiKey').value = '';
      byId('clearApiKey').checked = false;
      byId('clearKeyRow').classList.toggle('is-hidden', !provider.has_api_key);
      byId('apiKeyHint').textContent = provider.has_api_key
        ? t('savedKey', { hint: provider.api_key_hint || '••••' }) : t('keyOptional');
    } else {
      byId('providerName').textContent = t('newProvider');
      byId('clearKeyRow').classList.add('is-hidden');
      byId('apiKeyHint').textContent = t('keyOptional');
    }
    document.querySelectorAll('#providerForm input, #providerForm button').forEach((control) => {
      control.disabled = !state.snapshot.editable || state.busy;
    });
    renderTemplates();
  }

  function providerPayload(requireModel = true) {
    const payload = { base_url: byId('baseUrl').value.trim() };
    const model = byId('model').value.trim();
    if (requireModel || model) payload.model = model;
    const key = byId('apiKey').value;
    if (key || byId('clearApiKey').checked || !state.snapshot?.provider) payload.api_key = key;
    return payload;
  }

  function showUnlock(message = '') {
    byId('unlockPanel').classList.remove('is-hidden');
    byId('settingsSurface').classList.add('is-disabled');
    setConnection('error', t('setupRequired'));
    if (message) setMessage('error', message);
    byId('accessToken').focus();
  }

  async function loadSnapshot() {
    try {
      const body = await api('/api/v1/setup/provider');
      state.snapshot = body;
      byId('unlockPanel').classList.add('is-hidden');
      byId('forgetTokenButton').classList.toggle('is-hidden', !state.token);
      renderSnapshot();
      setMessage(body.configured ? 'success' : '',
        body.configured ? t('saved') : t('waiting'));
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        showUnlock(t('authFailed'));
        return;
      }
      showUnlock(error.message);
    }
  }

  async function discoverModels() {
    setBusy(true);
    setMessage('', t('discovering'));
    try {
      const body = await api('/api/v1/setup/provider/discover', {
        method: 'POST', body: JSON.stringify(providerPayload(false)),
      });
      byId('baseUrl').value = body.base_url;
      const list = byId('modelList');
      list.replaceChildren();
      body.models.forEach((model) => {
        const option = document.createElement('option');
        option.value = model.id;
        option.label = model.owned_by || model.id;
        list.appendChild(option);
      });
      if (!byId('model').value && body.models.length) byId('model').value = body.models[0].id;
      setMessage('success', t('discovered', { count: body.count }));
    } catch (error) {
      setMessage('error', error.message);
    } finally { setBusy(false); }
  }

  async function testConnection() {
    setBusy(true);
    setMessage('', t('testing'));
    try {
      const body = await api('/api/v1/setup/provider/test', {
        method: 'POST', body: JSON.stringify(providerPayload(true)),
      });
      setMessage(body.ok ? 'success' : 'error', `${body.detail} · ${body.latency_ms} ms`);
    } catch (error) {
      setMessage('error', error.message);
    } finally { setBusy(false); }
  }

  async function saveProvider(event) {
    event.preventDefault();
    setBusy(true);
    setMessage('', t('saving'));
    try {
      const body = await api('/api/v1/setup/provider', {
        method: 'PUT', body: JSON.stringify(providerPayload(true)),
      });
      state.snapshot = body;
      byId('apiKey').value = '';
      renderSnapshot();
      setMessage('success', t('saved'));
    } catch (error) {
      setMessage('error', error.message);
    } finally { setBusy(false); }
  }

  async function removeProvider() {
    if (!window.confirm(t('confirmRemove'))) return;
    setBusy(true);
    try {
      const body = await api('/api/v1/setup/provider', { method: 'DELETE' });
      state.snapshot = body;
      byId('baseUrl').value = '';
      byId('model').value = '';
      byId('apiKey').value = '';
      byId('modelList').replaceChildren();
      state.selectedTemplate = '';
      renderSnapshot();
      setMessage('success', t('removed'));
    } catch (error) {
      setMessage('error', error.message);
    } finally { setBusy(false); }
  }

  function bindEvents() {
    byId('languageButton').addEventListener('click', () => {
      state.language = state.language === 'zh' ? 'en' : 'zh'; applyLanguage();
    });
    byId('unlockForm').addEventListener('submit', async (event) => {
      event.preventDefault(); state.token = byId('accessToken').value; await loadSnapshot();
      if (state.snapshot) { byId('accessToken').value = ''; }
    });
    byId('revealKeyButton').addEventListener('click', () => {
      const input = byId('apiKey'); input.type = input.type === 'password' ? 'text' : 'password';
      byId('revealKeyButton').textContent = t(input.type === 'password' ? 'show' : 'hide');
    });
    byId('apiKey').addEventListener('input', () => { if (byId('apiKey').value) byId('clearApiKey').checked = false; });
    byId('clearApiKey').addEventListener('change', () => { if (byId('clearApiKey').checked) byId('apiKey').value = ''; });
    byId('discoverButton').addEventListener('click', discoverModels);
    byId('testButton').addEventListener('click', testConnection);
    byId('providerForm').addEventListener('submit', saveProvider);
    byId('deleteButton').addEventListener('click', removeProvider);
    byId('forgetTokenButton').addEventListener('click', () => {
      state.token = ''; state.snapshot = null; byId('forgetTokenButton').classList.add('is-hidden');
      showUnlock(t('tokenForgotten'));
    });
  }

  function tokenFromFragment() {
    const fragment = new URLSearchParams(location.hash.replace(/^#/, ''));
    const token = fragment.get('token') || '';
    if (token) {
      state.token = token;
      history.replaceState(null, '', `${location.pathname}${location.search}`);
    }
  }

  bindEvents();
  tokenFromFragment();
  applyLanguage();
  loadSnapshot();
})();
