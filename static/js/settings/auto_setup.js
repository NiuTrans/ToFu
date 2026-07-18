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
        '<span class="stg-modal-title"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:5px"><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/></svg>' + escapeHtml(t('settings.asTitle')) + '</span>' +
        '<button class="stg-modal-close" onclick="document.getElementById(\'stgAutoSetupModal\').remove()">✕</button>' +
      '</div>' +
      '<div class="stg-modal-body">' +
        '<p class="stg-modal-desc">' + escapeHtml(t('settings.asDesc')) + '</p>' +
        '<div class="stg-field">' +
          '<label>' + escapeHtml(t('settings.asUrlLabel')) + ' <span class="stg-required">*</span></label>' +
          '<input type="text" id="stgAutoUrl" placeholder="https://api.deepseek.com" autocomplete="url">' +
          '<span class="stg-hint">' + escapeHtml(t('settings.asUrlHint')) + '</span>' +
        '</div>' +
        '<div class="stg-field">' +
          '<label>' + escapeHtml(t('settings.asKeyLabel')) + ' <span class="stg-required">*</span></label>' +
          '<input type="password" id="stgAutoKey" placeholder="sk-..." autocomplete="off">' +
        '</div>' +
        '<div class="stg-field">' +
          '<label>' + escapeHtml(t('settings.asModelsPathLabel')) + ' <span class="stg-hint">' + escapeHtml(t('settings.asModelsPathHint')) + '</span></label>' +
          '<input type="text" id="stgAutoModelsPath" placeholder="/models">' +
        '</div>' +
        '<div id="stgAutoStatus" class="stg-auto-status" style="display:none"></div>' +
      '</div>' +
      '<div class="stg-modal-footer">' +
        '<button class="stg-btn-secondary" onclick="document.getElementById(\'stgAutoSetupModal\').remove()">' + escapeHtml(t('settings.cancel')) + '</button>' +
        '<button class="stg-btn-primary" id="stgAutoProbeBtn" onclick="_runAutoProbe()">' + Icon('search', 13) + ' ' + escapeHtml(t('settings.asProbeBtn')) + '</button>' +
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
    _showAutoStatus('error', t('settings.fillUrl'));
    return;
  }
  if (!apiKey) {
    _showAutoStatus('error', t('settings.fillKey'));
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
    probeBtn.textContent = t('settings.asProbing');
  }
  _showAutoStatus('loading', t('settings.discoveringModels'));

  try {
    var data = await Api.providers.probe(baseUrl, apiKey, modelsPath || '');
    if (!data) {
      _showAutoStatus('error', t('settings.asProbeNetFail'));
      return;
    }

    if (!data.ok) {
      _showAutoStatus('error', data.error || t('settings.probeFailed'));
      return;
    }

    // ── Success: create the provider ──
    var models = data.models || [];
    var summary = data.summary || {};

    // Build a summary message
    var parts = [];
    if (summary.text) parts.push(t('settings.asTextModels', { n: summary.text }));
    if (summary.thinking) parts.push(t('settings.asThinkingModels', { n: summary.thinking }));
    if (summary.vision) parts.push(t('settings.asVisionModels', { n: summary.vision }));
    if (summary.cheap) parts.push(t('settings.asCheapModels', { n: summary.cheap }));
    if (summary.image_gen) parts.push(t('settings.asIgModels', { n: summary.image_gen }));
    if (summary.embedding) parts.push(t('settings.asEmbeddingModels', { n: summary.embedding }));
    var modelSummary = parts.join(t('settings.asModelsJoin')) || t('settings.asModelsCount', { n: models.length });

    _showAutoStatus('success',
      t('settings.asDiscovered', {
        n: models.length,
        summary: modelSummary,
        balance: (data.balance_url ? t('settings.asBalanceDetected') : ''),
        thinking: (data.thinking_format ? t('settings.asThinkingFormat', { fmt: data.thinking_format }) : '')
      }));

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
    _showAutoStatus('error', t('settings.asNetworkError', { error: e.message }));
  } finally {
    if (probeBtn) {
      probeBtn.disabled = false;
      probeBtn.textContent = t('settings.asProbeBtn');
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
