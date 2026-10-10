(() => {
  'use strict';
  const byId = (id) => document.getElementById(id);
  let snapshot = null;

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {'Content-Type': 'application/json', ...(options.headers || {})},
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.message || `HTTP ${response.status}`);
    return body;
  }

  function message(value, error = false) {
    byId('message').textContent = value;
    byId('message').classList.toggle('is-error', error);
  }

  function envelope() {
    let value;
    try { value = JSON.parse(byId('routingJson').value); }
    catch (_error) { throw new Error('JSON 格式无效'); }
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
      throw new Error('接入信封必须是对象');
    }
    return value;
  }

  function render(body) {
    snapshot = body;
    const configured = Boolean(body.configured);
    byId('statusBadge').textContent = configured ? '已就绪' : '需要配置';
    byId('configState').textContent = configured ? '已配置' : '尚未配置';
    byId('configState').classList.toggle('is-ready', configured);
    byId('deleteButton').classList.toggle('is-hidden', !configured || !body.editable);
    byId('testButton').disabled = !body.editable;
    byId('saveButton').disabled = !body.editable;
    byId('routingJson').readOnly = !body.editable;
    if (body.model_routing && !byId('routingJson').value.trim()) {
      const publicValue = body.model_routing;
      byId('routingJson').value = JSON.stringify({
        model_routing: publicValue.model_routing,
        model: publicValue.model,
        routing: publicValue.routing,
        credential_secrets: {},
      }, null, 2);
      message('元数据已载入。secret 不会回显；再次保存前请重新填写 credential_secrets。');
    } else if (body.load_error) {
      message(body.load_error, true);
    }
  }

  async function refresh() {
    try { render(await api('/api/v1/setup/model-routing')); }
    catch (error) { message(error.message, true); }
  }

  byId('testButton').addEventListener('click', async () => {
    try {
      message('正在探测计算出的首选部署路由…');
      const result = await api('/api/v1/setup/model-routing/test', {
        method: 'POST', body: JSON.stringify(envelope()),
      });
      message(result.ok
        ? `探测通过：${result.provider_id} / ${result.deployment_id}（${result.latency_ms} ms）`
        : `探测失败：${result.verdict}`, !result.ok);
    } catch (error) { message(error.message, true); }
  });

  byId('routingForm').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      message('正在加密并保存接入配置…');
      render(await api('/api/v1/setup/model-routing', {
        method: 'PUT', body: JSON.stringify(envelope()),
      }));
      message('接入配置已保存，只影响之后开始的任务。');
    } catch (error) { message(error.message, true); }
  });

  byId('deleteButton').addEventListener('click', async () => {
    if (!snapshot?.configured || !window.confirm('删除已保存的接入配置？')) return;
    try {
      render(await api('/api/v1/setup/model-routing', {method: 'DELETE'}));
      byId('routingJson').value = '';
      message('接入配置已删除。');
    } catch (error) { message(error.message, true); }
  });

  refresh();
})();
