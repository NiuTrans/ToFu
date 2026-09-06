/* ===== migrated source: settings/local_deploy.js ===== */
/*
 * Local deployment Settings projection.
 *
 * Responsibility: the dedicated 本地部署 entry — engine preset chooser, batch
 * probe-and-stage of OpenAI-compatible local endpoints, and the managed
 * model-path handoff into a fresh chat armed with the local_serve tool flow.
 * Staging authority stays with provider_render.js (_stageModelRoutingProviderBundle).
 */

var _LOCAL_DEPLOY_PRESETS = [
  { engine: 'vllm', icon: 'vllm', name: 'vLLM',
    placeholder: 'http://10.0.0.5:8000/v1',
    descKey: 'settings.localPresetVllmDesc' },
  { engine: 'sglang', icon: 'sglang', name: 'SGLang',
    placeholder: 'http://10.0.0.5:30000/v1',
    descKey: 'settings.localPresetSglangDesc' },
  { engine: 'ollama', icon: 'ollama', name: 'Ollama',
    placeholder: 'http://localhost:11434/v1',
    descKey: 'settings.localPresetOllamaDesc' },
  { engine: 'llamacpp', icon: 'llamacpp', name: 'llama.cpp',
    placeholder: 'http://localhost:8080/v1',
    descKey: 'settings.localPresetLlamacppDesc' },
  { engine: 'managed', icon: 'local',
    nameKey: 'settings.localPresetManagedName',
    descKey: 'settings.localPresetManagedDesc' },
  // Custom comes LAST (owner-ratified 2026-07-25).
  { engine: '', icon: 'local', custom: true,
    nameKey: 'settings.localPresetCustomName',
    descKey: 'settings.localPresetCustomDesc' },
];

function _localDeployWireClose(overlay, modal) {
  function close() { overlay.remove(); }
  overlay.onclick = function(event) { if (event.target === overlay) close(); };
  modal.querySelector('.stg-modal-close').onclick = close;
  return close;
}

function addLocalProvider() {
  var prev = document.getElementById('stgLocalDeployModal');
  if (prev) prev.remove();
  var overlay = document.createElement('div');
  overlay.id = 'stgLocalDeployModal';
  overlay.className = 'stg-modal-overlay';
  var modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard';
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    escapeHtml(t('settings.localPresetTitle')) +
    '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><p class="stg-modal-desc">' +
    escapeHtml(t('settings.localPresetDesc')) + '</p>' +
    '<div class="stg-template-grid"></div></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  var grid = modal.querySelector('.stg-template-grid');
  _LOCAL_DEPLOY_PRESETS.forEach(function(preset) {
    var item = document.createElement('button');
    item.type = 'button';
    item.className = 'stg-template-item';
    item.innerHTML = _brandSvg(preset.icon, 22) +
      '<span class="stg-template-info"><span class="stg-template-name">' +
      escapeHtml(preset.nameKey ? t(preset.nameKey) : preset.name) +
      '</span><span class="stg-template-models">' +
      escapeHtml(t(preset.descKey)) + '</span></span>';
    item.onclick = function() {
      overlay.remove();
      if (preset.engine === 'managed') _openManagedDeployDialog();
      else if (preset.custom) addProvider();
      else _openLocalEndpointDialog(preset);
    };
    grid.appendChild(item);
  });
  _localDeployWireClose(overlay, modal);
}

function _openLocalEndpointDialog(preset) {
  var prev = document.getElementById('stgLocalEndpointModal');
  if (prev) prev.remove();
  var overlay = document.createElement('div');
  overlay.id = 'stgLocalEndpointModal';
  overlay.className = 'stg-modal-overlay';
  var modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard';
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    _brandSvg(preset.icon, 18) + ' ' + escapeHtml(preset.name) +
    '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><p class="stg-modal-desc">' +
    escapeHtml(t(preset.descKey)) + '</p>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.localDeployEndpointsLabel')) +
    '<textarea class="stg-local-endpoints-input" rows="4" placeholder="' +
    escapeHtml(preset.placeholder) + '"></textarea></label>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.localDeployApiKeyLabel')) +
    '<input type="password" class="stg-tpl-wizard-key" autocomplete="new-password"></label>' +
    '<div class="stg-auto-status" style="display:none"></div>' +
    '<div class="stg-local-endpoint-results"></div></div>' +
    '<div class="stg-modal-footer"><button type="button" class="stg-btn-secondary">' +
    escapeHtml(t('settings.epBulkCancel')) + '</button>' +
    '<button type="button" class="stg-btn-add">' +
    escapeHtml(t('settings.localDeployProbeAdd')) + '</button></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  var close = _localDeployWireClose(overlay, modal);
  modal.querySelector('.stg-btn-secondary').onclick = close;
  var textarea = modal.querySelector('.stg-local-endpoints-input');
  var keyInput = modal.querySelector('.stg-tpl-wizard-key');
  var statusEl = modal.querySelector('.stg-auto-status');
  var resultsEl = modal.querySelector('.stg-local-endpoint-results');
  var probeBtn = modal.querySelector('.stg-btn-add');
  function setStatus(text, kind) {
    statusEl.style.display = text ? '' : 'none';
    statusEl.className = 'stg-auto-status' + (kind ? ' ' + kind : '');
    statusEl.textContent = text;
  }
  probeBtn.onclick = async function() {
    var urls = [];
    textarea.value.split('\n').forEach(function(line) {
      var url = line.trim();
      if (url && urls.indexOf(url) === -1) urls.push(url);
    });
    if (!urls.length) {
      showAlert(t('settings.localDeployNoUrl'));
      return;
    }
    var apiKey = keyInput.value.trim();
    probeBtn.disabled = true;
    resultsEl.innerHTML = '';
    setStatus(t('settings.epProbingN', { n: urls.length }), 'stg-auto-loading');
    var probes = await Promise.allSettled(urls.map(function(url) {
      return Api.providers.probe(url, apiKey, '');
    }));
    var okCount = 0;
    for (var i = 0; i < urls.length; i++) {
      var row = document.createElement('div');
      row.className = 'stg-local-endpoint-row';
      var probed = probes[i];
      var bundle = probed.status === 'fulfilled' &&
        probed.value && probed.value.provider_bundle;
      if (bundle) {
        try {
          // Sequential staging: each success bumps the v2 revision.
          if (await _stageModelRoutingProviderBundle(bundle, apiKey)) {
            okCount++;
            row.classList.add('is-ok');
            row.textContent = urls[i] + ' · ' + t('settings.epModelsCount', {
              n: (bundle.deployments || []).length,
            });
          } else {
            row.classList.add('is-fail');
            row.textContent = urls[i] + ' · ' + t('settings.localDeployDuplicate');
          }
        } catch (error) {
          row.classList.add('is-fail');
          row.textContent = urls[i] + ' · ' + String(error && error.message || error);
        }
      } else {
        var reason = probed.status === 'rejected'
          ? String(probed.reason && probed.reason.message || probed.reason)
          : String((probed.value && (probed.value.error || probed.value.message)) ||
              t('settings.epProbeFailed'));
        row.classList.add('is-fail');
        row.textContent = urls[i] + ' · ' + reason;
      }
      resultsEl.appendChild(row);
    }
    if (okCount) {
      setStatus(t('settings.localDeployAddedSummary', {
        ok: okCount, total: urls.length,
      }), 'stg-auto-success');
    } else {
      setStatus(t('settings.localDeployNoneOk'), 'stg-auto-error');
    }
    probeBtn.disabled = false;
  };
  textarea.focus();
}

function _openManagedDeployDialog() {
  var prev = document.getElementById('stgManagedDeployModal');
  if (prev) prev.remove();
  var overlay = document.createElement('div');
  overlay.id = 'stgManagedDeployModal';
  overlay.className = 'stg-modal-overlay';
  var modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard';
  var engineOptions = [
    { value: '', label: t('settings.managedDeployEngineAuto') },
    { value: 'vllm', label: 'vLLM' },
    { value: 'sglang', label: 'SGLang' },
    { value: 'ollama', label: 'Ollama' },
    { value: 'llamacpp', label: 'llama.cpp' },
  ];
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    _brandSvg('local', 18) + ' ' + escapeHtml(t('settings.managedDeployTitle')) +
    '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><p class="stg-modal-desc">' +
    escapeHtml(t('settings.managedDeployDesc')) + '</p>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.managedDeployPathLabel')) +
    '<input type="text" class="stg-tpl-wizard-key stg-managed-path" placeholder="' +
    escapeHtml(t('settings.managedDeployPathHint')) + '"></label>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.managedDeployEngineLabel')) +
    '<select class="stg-tpl-wizard-key stg-managed-engine">' +
    engineOptions.map(function(option) {
      return '<option value="' + option.value + '">' + escapeHtml(option.label) + '</option>';
    }).join('') + '</select></label></div>' +
    '<div class="stg-modal-footer"><button type="button" class="stg-btn-secondary">' +
    escapeHtml(t('settings.epBulkCancel')) + '</button>' +
    '<button type="button" class="stg-btn-add">' +
    escapeHtml(t('settings.managedDeployStart')) + '</button></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  var close = _localDeployWireClose(overlay, modal);
  modal.querySelector('.stg-btn-secondary').onclick = close;
  var pathInput = modal.querySelector('.stg-managed-path');
  var engineSelect = modal.querySelector('.stg-managed-engine');
  modal.querySelector('.stg-btn-add').onclick = function() {
    var path = pathInput.value.trim();
    if (!path) {
      showAlert(t('settings.managedDeployPathRequired'));
      return;
    }
    var engineLabel = engineSelect.options[engineSelect.selectedIndex].text;
    close();
    _startManagedDeployChat(path, engineLabel);
  };
  pathInput.focus();
}

function _startManagedDeployChat(path, engineLabel) {
  closeSettings();
  // newChat() reads the current draft to archive the previous conversation,
  // so the prompt must land in the input only after the new shell exists.
  newChat();
  var input = document.getElementById('userInput');
  if (!input) return;
  input.value = t('settings.managedDeployPrompt', { path: path, engine: engineLabel });
  input.dispatchEvent(new Event('input', { bubbles: true }));
  updateSendButton();
  input.focus();
}
