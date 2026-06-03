/* ═══════════════════════════════════════════════════════════════════
   settings/auto setup — extracted from settings.js (split 2026-05-28)

   Auto-Setup modal (URL+key onboarding probe → fully-configured card).

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Auto Setup — URL-first provider onboarding flow
// ══════════════════════════════════════════════════════

/**
 * Show the Auto Setup modal. User enters only Base URL + API Key,
 * the system probes the provider and creates a fully configured card.
 */
function _showAutoSetupModal() {
  // Remove any existing modal
  var existing = document.getElementById('stgAutoSetupModal');
  if (existing) existing.remove();

  var html = '<div id="stgAutoSetupModal" class="stg-modal-overlay" onclick="if(event.target===this)this.remove()">' +
    '<div class="stg-modal">' +
      '<div class="stg-modal-header">' +
        '<span class="stg-modal-title">🚀 自动配置服务商</span>' +
        '<button class="stg-modal-close" onclick="document.getElementById(\'stgAutoSetupModal\').remove()">✕</button>' +
      '</div>' +
      '<div class="stg-modal-body">' +
        '<p class="stg-modal-desc">只需填写 API 地址和密钥，系统将自动发现模型、检测余额接口、识别服务商品牌并获取定价信息。</p>' +
        '<div class="stg-field">' +
          '<label>API 地址 (Base URL) <span class="stg-required">*</span></label>' +
          '<input type="text" id="stgAutoUrl" placeholder="https://api.deepseek.com" autocomplete="url">' +
          '<span class="stg-hint">填写 OpenAI 兼容的 API 地址，通常以 /v1 结尾</span>' +
        '</div>' +
        '<div class="stg-field">' +
          '<label>API 密钥 <span class="stg-required">*</span></label>' +
          '<input type="password" id="stgAutoKey" placeholder="sk-..." autocomplete="off">' +
        '</div>' +
        '<div class="stg-field">' +
          '<label>模型发现路径 <span class="stg-hint">（可选 — 默认 /models）</span></label>' +
          '<input type="text" id="stgAutoModelsPath" placeholder="/models">' +
        '</div>' +
        '<div id="stgAutoStatus" class="stg-auto-status" style="display:none"></div>' +
      '</div>' +
      '<div class="stg-modal-footer">' +
        '<button class="stg-btn-secondary" onclick="document.getElementById(\'stgAutoSetupModal\').remove()">取消</button>' +
        '<button class="stg-btn-primary" id="stgAutoProbeBtn" onclick="_runAutoProbe()">🔍 开始探测</button>' +
      '</div>' +
    '</div>' +
  '</div>';

  document.body.insertAdjacentHTML('beforeend', html);
  // Focus the URL input
  setTimeout(function() {
    var urlInput = document.getElementById('stgAutoUrl');
    if (urlInput) urlInput.focus();
  }, 100);
}

/**
 * Run the auto-probe: call /api/provider-probe and create the provider.
 */
async function _runAutoProbe() {
  var baseUrl = (document.getElementById('stgAutoUrl').value || '').trim();
  var apiKey = (document.getElementById('stgAutoKey').value || '').trim();
  var modelsPath = (document.getElementById('stgAutoModelsPath').value || '').trim();
  var statusDiv = document.getElementById('stgAutoStatus');
  var probeBtn = document.getElementById('stgAutoProbeBtn');

  if (!baseUrl) {
    _showAutoStatus('error', '请填写 API 地址');
    return;
  }
  if (!apiKey) {
    _showAutoStatus('error', '请填写 API 密钥');
    return;
  }

  // Normalize URL: ensure scheme
  if (!baseUrl.startsWith('http://') && !baseUrl.startsWith('https://')) {
    baseUrl = 'https://' + baseUrl;
    document.getElementById('stgAutoUrl').value = baseUrl;
  }

  // Show progress
  if (probeBtn) {
    probeBtn.disabled = true;
    probeBtn.textContent = '⏳ 正在探测…';
  }
  _showAutoStatus('loading', '正在发现模型… 这可能需要几秒钟');

  try {
    var data = await Api.providers.probe(baseUrl, apiKey, modelsPath || '');
    if (!data) {
      _showAutoStatus('error', '探测失败 (网络/超时)');
      return;
    }

    if (!data.ok) {
      _showAutoStatus('error', data.error || '探测失败');
      return;
    }

    // ── Success: create the provider ──
    var models = data.models || [];
    var summary = data.summary || {};

    // Build a summary message
    var parts = [];
    if (summary.text) parts.push(summary.text + ' 个文本');
    if (summary.thinking) parts.push(summary.thinking + ' 个推理');
    if (summary.vision) parts.push(summary.vision + ' 个视觉');
    if (summary.cheap) parts.push(summary.cheap + ' 个低价');
    if (summary.image_gen) parts.push(summary.image_gen + ' 个图片生成');
    if (summary.embedding) parts.push(summary.embedding + ' 个嵌入');
    var modelSummary = parts.join('，') || (models.length + ' 个模型');

    _showAutoStatus('success',
      '✅ 发现 ' + models.length + ' 个模型（' + modelSummary + '）' +
      (data.balance_url ? '，已检测到余额接口' : '') +
      (data.thinking_format ? '，建议思维格式: ' + data.thinking_format : ''));

    // Create the provider entry
    var provId = (data.brand || 'prov') + '_' + Date.now().toString(36);
    var newProv = {
      id: provId,
      name: data.name || 'Auto Provider',
      base_url: baseUrl,
      api_keys: [apiKey],
      enabled: true,
      models: models,
      brand: data.brand || 'generic',
      balance_url: data.balance_url || '',
    };
    if (data.thinking_format) {
      newProv.thinking_format = data.thinking_format;
    }

    _stgProviders.unshift(newProv);
    _renderProvidersTab();
    _renderPresetsTab(_serverConfig);

    // Close modal after a short delay so user sees the success message
    setTimeout(function() {
      var modal = document.getElementById('stgAutoSetupModal');
      if (modal) modal.remove();

      // Expand the new provider card and scroll to it
      var first = document.querySelector('.stg-provider-card');
      if (first) {
        first.classList.add('expanded');
        first.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }, 1500);

  } catch (e) {
    _showAutoStatus('error', '网络错误: ' + e.message);
  } finally {
    if (probeBtn) {
      probeBtn.disabled = false;
      probeBtn.textContent = '🔍 开始探测';
    }
  }
}

/** Show a status message in the auto-setup modal */
function _showAutoStatus(type, msg) {
  var div = document.getElementById('stgAutoStatus');
  if (!div) return;
  div.style.display = 'block';
  div.className = 'stg-auto-status stg-auto-' + type;
  div.textContent = msg;
}
