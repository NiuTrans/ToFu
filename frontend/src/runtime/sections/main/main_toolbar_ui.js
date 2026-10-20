/* ===== migrated source: main/main_toolbar_ui.js ===== */
/* Toolbar composition: mode/agent/Flow actions and startup policy. Model-picker
 * projection, rendering, search and popup lifecycle live in the typed owner;
 * this section injects services and retains model/persistence actions until
 * their state authority migrates. Composed after the ESM prelude, before boot. */

// ══════════════════════════════════════════════════════
//  The ESM prelude installs the typed model-capability controller. Retained
//  consumers keep only a fail-open boundary; they never recreate taxonomy
//  state or a second hardcoded exclusion list.
// ══════════════════════════════════════════════════════
/** One-shot warning when an isolated entry omits the typed capability port.
 *  Kept debounced (per-page-load) so a big model list doesn't spam the console.
 *  Referenced by guarded filters here and in visibility_defaults.js. */
var _modelCapsMissingWarned = false;
function _warnModelCapsMissing() {
  if (_modelCapsMissingWarned) return;
  _modelCapsMissingWarned = true;
  try { console.warn('[Tofu] isChatModel unavailable — showing all models unfiltered (non-chat models may appear in the picker)'); } catch (_) {}
}
if (typeof window !== 'undefined') runtimeScope._warnModelCapsMissing = _warnModelCapsMissing;

// Everyday chat capabilities; aliases air/pro/studio normalize to chat.
// Keep this wire-default table in parity with lib/tasks_pkg/chat_mode.py.
const _CHAT_MODE_DEFAULTS = {
  chat: {
    searchMode: 'multi',
    fetchEnabled: true,
    codeExecEnabled: true,
    memoryEnabled: true,
  },
};
if (typeof window !== 'undefined') runtimeScope._CHAT_MODE_DEFAULTS = _CHAT_MODE_DEFAULTS;

function _applyChatModeUI(mode) {
  chatMode = 'chat';
  const d = _CHAT_MODE_DEFAULTS.chat;
  if (typeof _applySearchModeUI === 'function') _applySearchModeUI(d.searchMode);
  if (typeof _applyFetchEnabledUI === 'function') _applyFetchEnabledUI(d.fetchEnabled);
  if (typeof _applyCodeExecUI === 'function') _applyCodeExecUI(d.codeExecEnabled);
  if (typeof _applyMemoryUI === 'function') _applyMemoryUI(d.memoryEnabled);
  paintOptionalProjectAttachment(document, t);
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
}
if (typeof window !== 'undefined') runtimeScope._applyChatModeUI = _applyChatModeUI;

// Compatibility action names only: remove when the shell template binds its
// optional project action directly. No capability selection or project gating.
function toggleChatModeMenu(e) {
  if (e) e.stopPropagation();
  runtimeScope.openProjectModal();
}
if (typeof window !== 'undefined') runtimeScope.toggleChatModeMenu = toggleChatModeMenu;
function closeChatModeMenu() {
  document.getElementById('chatModeMenu')?.classList.remove('open');
}
if (typeof window !== 'undefined') runtimeScope.closeChatModeMenu = closeChatModeMenu;
function setChatMode(mode) {
  if (mode === 'studio') runtimeScope.openProjectModal();
  _applyChatModeUI('chat');
  captureActiveConversationSettings();
}
if (typeof window !== 'undefined') runtimeScope.setChatMode = setChatMode;
function onProjectAttached() {
  paintOptionalProjectAttachment(document, t);
}
if (typeof window !== 'undefined') runtimeScope.onProjectAttached = onProjectAttached;
function onProjectCleared() {
  paintOptionalProjectAttachment(document, t);
}
if (typeof window !== 'undefined') runtimeScope.onProjectCleared = onProjectCleared;


// ══════════════════════════════════════════════════════
// Agent interaction mode — Standard / Plan / Autopilot
//
// One radio surface projects the established planMode / autopilotEnabled
// wire flags. Optional project attachment is a separate workspace action.
// ══════════════════════════════════════════════════════

const _AGENT_MODE_LABEL_KEYS = Object.freeze({
  standard: 'toolbar.standardMode',
  plan: 'toolbar.planMode',
  autopilot: 'toolbar.autopilot',
});

function _paintAgentModeUI() {
  const mode = resolveAgentMode(planMode, autopilotEnabled);
  const workflowSelected = !!activeFlow;
  const trigger = document.getElementById('agentModeToggle');
  if (trigger) {
    trigger.dataset.mode = workflowSelected ? 'workflow' : mode;
    trigger.classList.toggle(
      'has-active', workflowSelected || mode !== 'standard');
  }
  const label = document.getElementById('agentModeLabel');
  const labelKey = _AGENT_MODE_LABEL_KEYS[mode];
  if (label) {
    if (workflowSelected) {
      label.removeAttribute('data-i18n');
      label.textContent = _flowDisplayName(activeFlow);
    } else {
      label.dataset.i18n = labelKey;
      label.textContent = typeof t === 'function' ? t(labelKey) : labelKey;
    }
  }
  document.querySelectorAll('#agentModeMenu .agent-mode-item').forEach((item) => {
    const selected = !workflowSelected && item.dataset.mode === mode;
    item.classList.toggle('selected', selected);
    item.classList.toggle('active', selected);
    item.setAttribute('aria-checked', selected ? 'true' : 'false');
  });
}

/* Paint-only and atomic: restore paths use this without persisting. */
function _applyAgentModeUI(mode) {
  const flags = agentModeFlags(mode);
  planMode = flags.planMode;
  autopilotEnabled = flags.autopilotEnabled;
  const autopilotBadge = document.getElementById('autopilotBadge');
  if (autopilotBadge) autopilotBadge.style.display = autopilotEnabled ? '' : 'none';
  _paintAgentModeUI();
}
if (typeof window !== 'undefined') runtimeScope._applyAgentModeUI = _applyAgentModeUI;

/* Compatibility paint ports for retained callers. User actions go through
 * setAgentMode(), which also owns persistence and Autopilot disarm. */
function _applyPlanModeUI(on) {
  _applyAgentModeUI(on ? 'plan'
    : resolveAgentMode(false, autopilotEnabled));
}
if (typeof window !== 'undefined') runtimeScope._applyPlanModeUI = _applyPlanModeUI;

/* One accepted turn owns an immutable interaction config. All controls that
 * can change the loop owner (Agent mode, Flow, or a Plan-required capability)
 * consult the same guard so a secondary switch cannot visually bypass the
 * disabled Agent-mode trigger during start/stop handshakes. */
function _agentInteractionChangeBlocked() {
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (!conv) return false;
  return Boolean(
    (typeof convIsBusy === 'function' && convIsBusy(conv))
    || conv._translating || conv._genStartCtrl || conv._finishingStream
  );
}
if (typeof window !== 'undefined') {
  runtimeScope._agentInteractionChangeBlocked = _agentInteractionChangeBlocked;
}

function _setAgentModeLocked(on) {
  const locked = !!on;
  const trigger = document.getElementById('agentModeToggle');
  [trigger, document.getElementById('flowToggle')].forEach((control) => {
    if (!control) return;
    control.disabled = locked;
    control.setAttribute('aria-disabled', locked ? 'true' : 'false');
  });
  /* A disabled trigger swallows the click outright — swap the tooltip so the
   * locked state explains itself instead of reading as a dead button. Ride
   * data-i18n-title so a language repaint keeps the locked copy while the
   * lock holds and restores the base copy after it lifts. */
  if (trigger) {
    const titleKey = locked
      ? 'toolbar.agentModeLockedTooltip' : 'toolbar.agentModeTooltip';
    trigger.setAttribute('data-i18n-title', titleKey);
    if (typeof t === 'function') trigger.title = t(titleKey);
  }
  document.querySelectorAll(
    '#agentModeMenu button, .mobile-agent-mode-item, #mobileFlow, '
      + '#flowMenuList button',
  )
    .forEach((item) => { item.disabled = locked; });
  if (locked) {
    const submenu = document.getElementById('submenuAgentMode');
    submenu?.classList.remove('open');
    submenu?.querySelector('.submenu-trigger')?.classList.remove('open');
    trigger?.setAttribute('aria-expanded', 'false');
    _flowMenuRenderGeneration++;
  }
}
if (typeof window !== 'undefined') runtimeScope._setAgentModeLocked = _setAgentModeLocked;

function setAgentMode(requestedMode) {
  if (_agentInteractionChangeBlocked()) return false;
  const mode = normalizeAgentMode(requestedMode);
  const wasAutopilot = autopilotEnabled;
  if (typeof _applyFlowUI === 'function') _applyFlowUI('');
  _applyAgentModeUI(mode);
  if (typeof _applyImageGenUI === 'function') _applyImageGenUI(false);
  if (mode === 'plan' && typeof _applyHumanGuidanceUI === 'function') {
    _applyHumanGuidanceUI(true);
  }
  if (wasAutopilot && mode !== 'autopilot') _disarmAutopilot();
  captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  _closeAgentModeMenu();
  debugLog(`Agent mode: ${mode}`, 'success');
  return true;
}
if (typeof window !== 'undefined') runtimeScope.setAgentMode = setAgentMode;

/* Legacy callable names remain for extensions and old cached markup. */
function togglePlanMode() {
  return setAgentMode(planMode ? 'standard' : 'plan');
}
if (typeof window !== 'undefined') runtimeScope.togglePlanMode = togglePlanMode;

/* Cached integrations may still ask for a mode; attachments do not select it. */
function _deriveChatModeFromFlags(conv) {
  return 'chat';
}
if (typeof window !== 'undefined') runtimeScope._deriveChatModeFromFlags = _deriveChatModeFromFlags;

/* Provider preference chip: a real popover, not a native <select> — the
 * OS popup ignores page styling entirely and rendered as an alien widget
 * inside the polished panel. The menu lives on document.body so the
 * scrolling list container can never clip it; one menu exists at a time. */
/* Temporary picker seam; delete when toolbar model actions migrate. Display,
 * search and popup resources belong to one typed owner; persistence stays here. */
let _modelPickerUi = null;
function _getModelPickerUi(list = document.getElementById('presetDropdownList') || document.getElementById('presetDropdown')) {
  if (_modelPickerUi) return _modelPickerUi;
  _modelPickerUi = createModelPicker({
    window, document, list,
    selection: () => ({ modelId: config.model || serverModel, preferredProviderId: config.preferredProviderId || '', modelRef: config.modelRef }),
    hiddenModels: () => _hiddenModels,
    isChatModel: model => { if (typeof runtimeScope.isChatModel === 'function') return runtimeScope.isChatModel(model); _warnModelCapsMissing(); return true; },
    groupKey: model => model.routing_v2 ? (model.pending_identity ? '__pending_identity__' : (model.creator_id || '__models__'))
      : runtimeScope.modelGroupKey({ brand: model.brand, name: model.provider_name }, model),
    groupLabel: (key, model) => model.routing_v2 ? (model.pending_identity ? t('settings.matrixProviderModels')
      : runtimeScope.modelCreatorLabel(model.creator_id || '', model.creator_name || '', model.model_id || '')) : runtimeScope.modelGroupLabel(key, model.provider_name),
    compare: typeof _compareModelsByDisplayName === 'function' ? _compareModelsByDisplayName : undefined,
    fold: typeof runtimeScope.modelDisplayUnits === 'function' ? runtimeScope.modelDisplayUnits : undefined,
    recentIds: () => typeof runtimeScope.recentModels === 'function' ? runtimeScope.recentModels() : [],
    shortName: id => typeof _modelShortName === 'function' ? _modelShortName(id) : id,
    brand: model => {
      const brand = (model.brand || '').trim(), credential = brand === 'oauth' || brand === 'adapter';
      if (!(model.routing_v2 && !model.pending_identity) && brand && !credential) return brand;
      if (!(model.routing_v2 && !model.pending_identity) && credential) return runtimeScope.modelGroupKey({ brand: model.brand, name: model.provider_name }, model);
      return typeof _modelBrand === 'function' ? _modelBrand(model.model_id, model.creator_id)
        : (typeof _detectBrand === 'function' ? _detectBrand(model.model_id) : 'generic');
    },
    icon: (brand, size) => typeof _brandSvg === 'function' ? _brandSvg(brand, size) : '', text: t,
    select: model => model.routing_v2 ? selectModelRoute(model) : selectModel(model.model_id),
    openSettings: () => { if (typeof runtimeScope.openSettings === 'function') runtimeScope.openSettings(); if (typeof runtimeScope.switchSettingsTab === 'function') runtimeScope.switchSettingsTab('preset'); },
  });
  if (typeof retainedCompositionLifecycle !== 'undefined') retainedCompositionLifecycle.add(() => _modelPickerUi?.destroy());
  return _modelPickerUi;
}
function _modelRoutingDropdownModels(documentValue) { return projectModelPickerCatalog(documentValue); }
function _refreshModelPickerSelection() { _modelPickerUi?.refreshSelection(); }
function _populateModelDropdown(models) {
  const dropdown = document.getElementById('presetDropdownList') || document.getElementById('presetDropdown');
  if (!dropdown || !Array.isArray(models)) return;
  _registeredModels = models; _registeredModelsLoaded = true;
  _getModelPickerUi(dropdown).populate(models);
  if (!models.length && typeof _applyImageGenAvailability === 'function') _applyImageGenAvailability();
}

/* Validate config.model against the provider list, falling back when the
 * stored value doesn't exist in this deployment (fresh open-source deploys,
 * provider removals). Provenance-aware: a PROVISIONAL config.model is a
 * painted placeholder (hardcoded boot value / server default), not a stored
 * choice — validating it would "fall back" from a model the user never
 * picked and warn every boot (the "aws.claude-opus-4.8 I never selected"
 * loop), so the provisional case validates serverModel instead. */
function _reconcileConfigModelAvailability(chatModels) {
  const availableIds = new Set((chatModels || []).map(m => m.model_id));
  const currentModel = config._modelIsProvisional
    ? serverModel
    : (config.model || serverModel);
  if (!currentModel || availableIds.has(currentModel)) return;
  console.warn('[Config] Selected model is unavailable; keeping selection:', currentModel);
}
/* Load independent config policy and owner routing in parallel. Config's
 * hidden-model policy applies before the routing catalog and effort ladder. */
function _loadServerConfigAndPopulate() {
  return _modelPickerStartup.load();
}
/* Settings Save committed and applied the routing authority itself, so the
 * post-save repaint only needs the miscellaneous config projection. Refetching
 * the full aggregate here cost a ~550KB round trip and re-parse on every save. */
function _refreshServerConfigAfterSave() {
  return _modelPickerStartup.refreshConfig();
}
let _modelPickerLastRouting = null;
const _modelPickerStartup = createModelPickerStartup({
  loadConfig: signal => Api.serverConfig.get({ signal, timeout: 15000 }),
  loadRouting: signal => Api.modelRouting.get({ signal, timeout: 15000 }),
  onBootReady: callback => {
    window.addEventListener('tofu:boot-ready', callback);
    return () => window.removeEventListener('tofu:boot-ready', callback);
  },

  isActive: () => !_effectiveHidden(),
  onActivityChange: callback => {
    document.addEventListener('visibilitychange', callback);
    return () => document.removeEventListener('visibilitychange', callback);
  },
  probeBootReady: async () => {
    const boot = await Api.get('/api/v4/boot', { onError: 'throw', timeout: 5000 });
    return boot?.status === 'ready';
  },
  applyConfig: data => {
      if (!data) throw new Error('Server config unavailable');
      /* Feature flags share this required first-screen response. Missing data
       * (older server / optional projection failure) makes the loader use its
       * dedicated endpoint fallback; unchanged refreshes repaint nothing. */
      void loadFeatureFlags(data.feature_flags);
      /* The context rail consumes this compact projection from the same
       * startup response. It must never fetch full MCP schemas just to count
       * them; the owner is loaded later in the composed script but registered
       * before this promise continuation can run. */
      if (data.mcp_tool_summary
          && typeof runtimeScope.applyMcpToolSummary === 'function') {
        runtimeScope.applyMcpToolSummary(data.mcp_tool_summary);
      }
      /* Build pricing cache from models data if available.
       * Used ONLY by settings.js to render the model-picker pricing
       * column. Cost-from-usage math is server-authoritative now
       * (lib/cost.py + the batched message-cost endpoint). */
      if (data.model_pricing) {
        _modelPricingCache = data.model_pricing;
      }
      /* (Per-provider pricing overrides used to be cached here for the
       * old client-side calcCostCny.  That's been migrated to
       * lib/pricing.py — provider overrides are resolved server-side
       * via lookup_pricing(model, provider_id).  Settings.js doesn't
       * need provider scoping for its display.) */
      /* Capture upload-shrink policy so compressImage() mirrors the backend.
       * See routes/upload.py:get_upload_policy(). Single source of truth — no
       * more frontend-vs-backend threshold drift (was 1024/q=0.85 vs 2048/q=0.90). */
      if (data.upload && typeof data.upload === 'object') {
        runtimeScope._uploadShrinkPolicy = data.upload;
      }
      /* Capture context-window policy so the Context Health Bar mirrors the
       * backend exactly. See lib/tasks_pkg/compaction.build_context_policy().
       * Single source of truth — no more frontend-vs-backend limit/threshold
       * drift (the JS table was stuck at 0.82 vs the real 0.90, and a stale
       * per-model regex table). */
      if (data.context && typeof data.context === 'object') {
        runtimeScope._contextPolicy = data.context;
        if (typeof runtimeScope.updateContextBar === 'function') runtimeScope.updateContextBar();
      }
      /* Capture translation policy (stale-partial heuristic threshold) so
       * translation.js mirrors the backend. See lib/text_lang.py. */
      if (data.translation && typeof data.translation === 'object') {
        runtimeScope._translationPolicy = data.translation;
      }
      /* Ingest capability taxonomy (SSOT for chat / non-chat capability
       * classification). Applied BEFORE the model-list filters below so
       * isChatModel(m) uses the server's shape, not the hardcoded fallback.
       * See lib/model_info/capability_taxonomy.py and the typed browser owner. */
      if (data.capability_taxonomy
          && typeof runtimeScope.applyCapabilityTaxonomy === 'function') {
        runtimeScope.applyCapabilityTaxonomy(data.capability_taxonomy);
      }
      /* Load hidden models from server config */
      _hiddenModels = new Set(data.hidden_models || []);
      _hiddenIgModels = new Set(data.hidden_ig_models || []);
      /* Refresh an already-demanded image picker now that hidden-model policy
       * is authoritative. An unopened image feature stays unloaded and makes
       * no model-catalogue request on the first screen. */
      if (typeof runtimeScope._loadIgModels === 'function') {
        void runtimeScope._loadIgModels();
      }
      /* Sync serverModel with the configured default model from Settings.
       * Without this, _resetToolsToDefaults() (called on new chat) would always
       * use the hardcoded initial serverModel instead of the user's configured
       * default model from the Settings "默认模型" dropdown. */
      const cfgDefault = data.model_defaults && data.model_defaults.default_model;
      if (cfgDefault) {
        serverModel = cfgDefault;
      }
      // Re-filter last-good authority under the new policy during refresh.
      // The retired dropdown_models shim is never selectable authority.
      _populateModelDropdown(_modelPickerLastRouting
        ? _modelRoutingDropdownModels(_modelPickerLastRouting) : []);

      /* The routing GET was launched in parallel; the coordinator applies
       * it only after hidden-model and capability policies above are ready. */
      _applyModelUI(config._modelIsProvisional ? null : config.model);
  },
  applyRouting: routingResponse => {
        const routingDocument = routingResponse && routingResponse.model_routing;

        if (!routingDocument) throw new Error('Model routing unavailable');
        _modelPickerLastRouting = routingDocument;
        _reasoningControls = Array.isArray(routingResponse?.reasoning_controls)
          ? routingResponse.reasoning_controls : undefined;
        const v2Models = _modelRoutingDropdownModels(
          routingDocument);
        /* A successful empty authority is authoritative too: never fabricate
         * a selectable model from the provisional server default. */
        _populateModelDropdown(v2Models);
        if (!v2Models.length) {
          const row = document.querySelector('#presetDropdownList .ps-dd-loading');
          if (row) {
            row.dataset.i18n = 'toolbar.noModelsAvailable';
            row.textContent = t('toolbar.noModelsAvailable');
          }
        }
        if (v2Models.length) {
          const selectedRef = config.modelRef;
          const selected = v2Models.find(row => {
            if (!selectedRef || typeof selectedRef !== 'object') return false;
            if (selectedRef.offering_id) {
              return row.provider_id === selectedRef.provider_id
                && row.offering_id === selectedRef.offering_id;
            }
            return row.creator_id === selectedRef.creator_id
              && row.model_id === selectedRef.model_id
              && (row.provider_id || '') === (config.preferredProviderId || '');
          });
          if (selected) _applyModelUI(config._modelIsProvisional ? null : selected.model_id);
          const routedChatModels = v2Models.filter(row => {
            if (_hiddenModels.has(row.model_id)) return false;
            return typeof runtimeScope.isChatModel !== 'function'
              || runtimeScope.isChatModel(row);
          });
          _reconcileConfigModelAvailability(routedChatModels);
        }
        _applyModelUI(config._modelIsProvisional ? null : config.model);
        _maybeAutoOpenSettings(routingDocument);
  },
  showFailure: error => {
    console.warn('[model picker] owner catalog load failed:', error?.message || error);
    /* Never turn a transport failure into a fake one-model authority. */
    const dropdown = document.getElementById('presetDropdownList');
    if (dropdown) {
      if (_modelPickerLastRouting && ![401, 403].includes(error?.status)) {
        _populateModelDropdown(_modelRoutingDropdownModels(_modelPickerLastRouting));
      } else {
        _modelPickerLastRouting = null;
        _registeredModels = [];
        dropdown.innerHTML = '';
      }
      dropdown.querySelectorAll('.ps-dd-loading').forEach(row => row.remove());
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'ps-dd-loading';
      retry.dataset.i18n = 'toolbar.retryLoadingModels';
      retry.textContent = t('toolbar.retryLoadingModels');
      retry.addEventListener('click', () => {
        if (!_modelPickerLastRouting) _populateModelDropdown([]);
        void _modelPickerStartup.load();
      }, { once: true });
      dropdown.appendChild(retry);
    }
    void loadFeatureFlags();
  },
});
retainedCompositionLifecycle.add(() => _modelPickerStartup.destroy());

/* Auto-open settings for explicit setup or an empty owner v2 authority. */
function _maybeAutoOpenSettings(modelRoutingDocument) {
  const params = new URLSearchParams(window.location.search);
  const fromBootstrap = params.get('setup') === '1';
  const accesses = modelRoutingDocument?.provider_accesses || [];
  const credentials = modelRoutingDocument?.credentials || [];
  const noKeys = !accesses.some(access => access.enabled)
    || !credentials.some(credential => credential.enabled);

  if (fromBootstrap || noKeys) {
    // Clean up the URL so ?setup=1 doesn't persist on reload
    if (fromBootstrap) {
      const cleanUrl = window.location.pathname + window.location.hash;
      window.history.replaceState(null, '', cleanUrl);
    }
    // Open settings after a short delay for the UI to settle
    setTimeout(() => {
      /* The first-run wizard supersedes the bare "open Settings to the
       * providers tab" behaviour: it asks the ONE question a new user can
       * answer (API key vs subscription) and drives the existing surfaces
       * from there. ?setup=1 forces it past the dismissal flag; the no-keys
       * trigger respects it so a skipped wizard never re-nags. */
      if (typeof maybeShowOnboarding === 'function') {
        // The owner exists: either it opened the wizard or the user already
        // dismissed it. Both outcomes are authoritative. Falling through on
        // the latter used to reopen the legacy Settings modal after every
        // reload, contradicting the durable onboarding dismissal.
        maybeShowOnboarding({ force: fromBootstrap });
        return;
      }
      // Wizard module absent (stale bundle) — the old surface still works.
      if (typeof runtimeScope.openSettings === 'function') {
        runtimeScope.openSettings();
        // Switch to the API/providers tab
        if (typeof runtimeScope.switchSettingsTab === 'function') {
          runtimeScope.switchSettingsTab('api');
        }
        // Show a helpful hint
        const hint = document.getElementById('settingsStatusHint');
        if (hint) {
          hint.textContent = noKeys
            ? '⚠️ No API keys configured — please add a provider to get started.'
            : '✅ Server started successfully! Review your API configuration below.';
          hint.style.color = noKeys ? '#f7768e' : '#9ece6a';
        }
      }
    }, 500);
  }
}

function togglePresetDropdown(event) { _getModelPickerUi().toggle(event); }
function selectModel(modelId) {
  const registered = _registeredModels.find(m => m.model_id === modelId);
  if (registered && registered.routing_v2) {
    return selectModelRoute(registered);
  }
  config.modelRef = null;
  config.preferredProviderId = '';
  config.routing = {};
  _applyModelUI(modelId);
  /* Record the pick through the bounded recent-model controller. An isolated
   * fixture without the composition port simply omits the recents strip. */
  if (typeof runtimeScope.pushRecentModel === 'function') {
    runtimeScope.pushRecentModel(modelId);
  }
  try { localStorage.setItem("claude_client_config", JSON.stringify(_configForPersist())); }
  catch (e) { debugLog(`[selectModel] localStorage save failed: ${e.message}`, 'error'); }
  commitComposerModel(getActiveConv(), captureComposerModel(config));
  captureActiveConversationSettings();
  const depthSuffix = _isThinkingCapable(config.model) && config.thinkingDepth
    ? ` [${config.thinkingDepth.toUpperCase()}]`
    : '';
  debugLog(`Model: ${config.model}${depthSuffix}`, "success");
}

function selectModelRoute(modelRow) {
  if (!modelRow || !modelRow.model_id) return false;
  if (modelRow.pending_identity) {
    config.modelRef = {
      provider_id: modelRow.provider_id,
      offering_id: modelRow.offering_id,
    };
  } else {
    config.modelRef = {
      creator_id: modelRow.creator_id,
      model_id: modelRow.model_id,
    };
  }
  config.preferredProviderId = modelRow.provider_id || '';
  config.routing = config.preferredProviderId
    ? { preferred_provider_id: config.preferredProviderId }
    : {};
  _applyModelUI(modelRow.model_id);
  _refreshModelPickerSelection();
  commitComposerModel(getActiveConv(), captureComposerModel(config));
  if (typeof runtimeScope.pushRecentModel === 'function') {
    runtimeScope.pushRecentModel(modelRow.model_id);
  }
  try { localStorage.setItem('claude_client_config', JSON.stringify(_configForPersist())); }
  catch (error) { debugLog(`[selectModelRoute] localStorage save failed: ${error.message}`, 'error'); }
  captureActiveConversationSettings();
  debugLog(`服务商: ${modelRow.provider_name || '自动'} · 模型: ${modelRow.model_id}`, 'success');
  return true;
}
function toggleAutoTranslate() {
  autoTranslate = !autoTranslate;
  localStorage.setItem("claude_auto_translate", JSON.stringify(autoTranslate));
  const btn = document.getElementById("translateToggle");
  const badge = document.getElementById("translateBadge");
  if (btn) btn.classList.toggle("active", autoTranslate);
  if (badge) badge.style.display = autoTranslate ? "" : "none";
  captureActiveConversationSettings();
  debugLog(`Auto-Translate: ${autoTranslate ? "ON" : "OFF"}`, "success");

  // One-time hint about <notranslate> when first enabling
  if (autoTranslate && !localStorage.getItem("claude_translate_hint_shown")) {
    localStorage.setItem("claude_translate_hint_shown", "1");
    showToast(
      "", "Translation Tip",
      "Select text and press Ctrl+J to wrap it in &lt;notranslate&gt; — that part won't be translated.",
      8000
    );
  }
}
function _applyAutoTranslateUI(enabled) {
  if (typeof enabled !== "undefined") {
    autoTranslate = !!enabled;
    localStorage.setItem(
      "claude_auto_translate",
      JSON.stringify(autoTranslate),
    );
  }
  const btn = document.getElementById("translateToggle");
  const badge = document.getElementById("translateBadge");
  if (btn) btn.classList.toggle("active", autoTranslate);
  if (badge) badge.style.display = autoTranslate ? "" : "none";
}

// ══════════════════════════════════════════════════════
// Toolbar Sub-menus — dropdown grouping for tool toggles
// ══════════════════════════════════════════════════════
function toggleSubmenu(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const wasOpen = el.classList.contains("open");
  // close all sub-menus first
  document.querySelectorAll(".toolbar-submenu.open").forEach(s => {
    s.classList.remove("open");
    const t = s.querySelector(".submenu-trigger");
    if (t) {
      t.classList.remove("open");
      t.setAttribute('aria-expanded', 'false');
    }
  });
  if (!wasOpen) {
    el.classList.add("open");
    const t = el.querySelector(".submenu-trigger");
    if (t) {
      t.classList.add("open");
      t.setAttribute('aria-expanded', 'true');
    }
  }
}
// Close sub-menus on outside click
document.addEventListener("click", (e) => {
  if (!e.target.closest(".toolbar-submenu")) {
    document.querySelectorAll(".toolbar-submenu.open").forEach(s => {
      s.classList.remove("open");
      const t = s.querySelector(".submenu-trigger");
      if (t) {
        t.classList.remove("open");
        t.setAttribute('aria-expanded', 'false');
      }
    });
  }
});

function updateSubmenuCounts() {
  /* Track whether any count pill's VISIBILITY flipped (display:none ↔
   * inline-block).  That pill is the only thing here that changes the
   * toolbar's natural content width — when it appears, the box must be
   * re-measured or the model name (.ps-label) truncates to fit the stale
   * --toolbar-w.  We reflow ONLY on an actual visibility change so plain
   * recomputes (e.g. depth toggles) stay free. */
  let widthChanged = false;
  const _setCount = (el, count) => {
    if (!el) return;
    el.textContent = count;
    const want = count > 0;
    if (el.classList.contains("visible") !== want) widthChanged = true;
    el.classList.toggle("visible", want);
  };

  // Gate the AI-drawing extra by model availability: hide the whole row when
  //   NO image-gen model is configured (a dead button otherwise). Uses the
  //   registered-model list captured at boot.
  _applyImageGenAvailability();

  // Extras drawer count = every orthogonal capability the user turned on.
  // (Scheduler + Swarm are default tools — no toggles — so they don't count.)
  const extrasCount = (autoTranslate ? 1 : 0) + (humanGuidanceEnabled ? 1 : 0)
    + (browserEnabled ? 1 : 0) + (desktopEnabled ? 1 : 0)
    + (imageGenEnabled ? 1 : 0);
  _setCount(document.getElementById("submenuExtrasCount"), extrasCount);
  const extrasTrigger = document.querySelector("#submenuExtras .submenu-trigger");
  if (extrasTrigger) extrasTrigger.classList.toggle("has-active", extrasCount > 0);

  /* Browser + desktop share ONE merged row (#localControlToggle); its summary
   * badge counts both. Repaint here so the row reflects state restored from a
   * conversation, not just state changed through the modal's switches. */
  if (typeof _lcUpdateBadge === "function") _lcUpdateBadge();

  /* A pill appeared/disappeared → toolbar's intrinsic width shifted by the
   * pill's box.  Re-measure so .ps-label gets its space back. */
  if (widthChanged && typeof _scheduleReflow === "function") _scheduleReflow();
}

/* Hide the AI-drawing toggle(s) when no image-gen model is configured — a
 * button that can't do anything is worse than an absent one. Detection reuses
 * the registered-model list (_registeredModels, replaced by the authenticated
 * v2 projection). Best-effort: if the list isn't ready yet we leave the row
 * visible; a successfully loaded empty authority is an authoritative absence. */
function _hasImageGenModel() {
  const models = (typeof _registeredModels !== 'undefined' && _registeredModels) || [];
  for (const m of models) {
    const caps = (m && m.capabilities) || [];
    for (let i = 0; i < caps.length; i++) if (caps[i] === 'image_gen') return true;
  }
  return false;
}
if (typeof window !== 'undefined') runtimeScope._hasImageGenModel = _hasImageGenModel;

function _applyImageGenAvailability() {
  const models = (typeof _registeredModels !== 'undefined' && _registeredModels) || [];
  if (!_registeredModelsLoaded) return;
  const ok = _hasImageGenModel();
  const ids = ['imageGenToggle', 'imageGenModeBtn', 'mobileImageGenToggle', 'mobileImageGenModeBtn'];
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el) el.style.display = ok ? '' : 'none';
  }
  // If image-gen was somehow enabled but no model exists, turn it off so the
  // wire config never asks for a tool the server can't honor.
  if (!ok && typeof imageGenEnabled !== 'undefined' && imageGenEnabled
      && typeof _applyImageGenToolUI === 'function') {
    _applyImageGenToolUI(false);
  }
}
if (typeof window !== 'undefined') runtimeScope._applyImageGenAvailability = _applyImageGenAvailability;

function cycleSearchMode() {
  const modes = ["off", "multi"];
  const idx = modes.indexOf(searchMode === "single" ? "multi" : searchMode);
  _applySearchModeUI(modes[(idx + 1) % modes.length]);
  captureActiveConversationSettings();
  debugLog(`Search: ${searchMode}`, "success");
}

// ══════════════════════════════════════════════════════
// Autopilot (Virtual User auto-replies until VU emits TASK_DONE)
// ══════════════════════════════════════════════════════
function _applyAutopilotUI(enabled) {
  _applyAgentModeUI(enabled ? 'autopilot'
    : resolveAgentMode(planMode, false));
}

/* Turning Goal Mode off cancels its live/queued command and clears legacy
 * marker state. Paint-only restore paths never call this mutating port. */
function _disarmAutopilot() {
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (conv && typeof Api !== 'undefined' && Api.chat?.disarmAutopilot) {
    Api.chat.disarmAutopilot(conv.id).then((response) => {
      if (typeof _applyDisarmResponse === 'function') {
        _applyDisarmResponse(conv.id, response);
      }
      if (typeof _refreshServerQueue === 'function') _refreshServerQueue(conv.id);
    }).catch((error) => console.warn(
      '[Autopilot] disarm failed:', error?.message || error));
  }
}

/**
 * Arm autopilot for the active conversation — the explicit "hand it over"
 * gesture (empty send while autopilot is ON).
 *
 * While an ordinary reply is streaming, the backend enqueues one explicit
 * Goal continuation behind that already-accepted turn. It is visible and
 * cancellable like other queued turns, survives reload, and never mutates the
 * running task's interpreter. An already-active GoalRun needs no successor.
 */
function _maybeArmAutopilot() {
  const conv = getActiveConv();
  if (!conv) return;
  if (!(typeof Api !== 'undefined' && Api.chat && Api.chat.armAutopilot)) return;
  Api.chat.armAutopilot(conv.id).then((r) => {
    if (r && r.armed) {
      debugLog("Goal Mode armed — continuation is active or queued", "success");
      if (typeof showToast === "function") {
        showToast("", t('autopilot.armedTitle'), t('autopilot.armedBody'), 4000);
      }
    }
    /* Hydrate the explicit continuation or active state projection. */
    if (typeof _refreshServerQueue === 'function') _refreshServerQueue(conv.id);
  }).catch((e) => console.warn('[Autopilot] arm failed:', e && e.message));
}
if (typeof window !== 'undefined') runtimeScope._maybeArmAutopilot = _maybeArmAutopilot;

/**
 * Kick autopilot on the active conversation when its reply has ALREADY
 * finished — the "push it forward" gesture (empty-Enter, autopilot ON, not
 * streaming). Creates a normal durable continuation turn and starts the same
 * GoalRun + FlowExecutor path used by an ordinary Goal Mode request.
 *
 * No-op when something is still streaming (the arm path covers that) or when
 * autopilot is off.
 */
async function _kickAutopilot() {
  const conv = getActiveConv();
  if (!conv) return;
  if (typeof autopilotEnabled !== 'undefined' && !autopilotEnabled) return;
  const streaming = typeof convIsBusy === 'function' && convIsBusy(conv);
  if (streaming) return;
  if (!(typeof Api !== 'undefined' && Api.chat && Api.chat.kickAutopilot)) return;
  let cfg = {};
  try {
    if (typeof _buildConvConfig === 'function') cfg = await _buildConvConfig(conv);
  } catch (e) {
    console.warn('[Autopilot] kick: _buildConvConfig failed, using defaults:', e && e.message);
  }
  try {
    const r = await Api.chat.kickAutopilot(conv.id, cfg);
    if (r && r.taskId) {
      debugLog('Autopilot taking over — virtual user is composing the next reply', 'success');
      try {
        await refreshConversationRuntime(conv.id);
      } catch (syncError) {
        console.warn(
          '[Autopilot] kick accepted; authoritative refresh will retry:',
          syncError && syncError.message,
        );
      }
      renderConversationList();
      updateSendButton();
    }
  } catch (e) {
    /* 409 = a task is already running for this conv (arm path applies). */
    console.warn('[Autopilot] kick failed:', e && e.message);
  }
}
if (typeof window !== 'undefined') runtimeScope._kickAutopilot = _kickAutopilot;

// ══════════════════════════════════════════════════════
// Debug-only saved Flow choices inside Agent Mode. Selection is exclusive.
// ══════════════════════════════════════════════════════

function _flowCatalogSnapshot() {
  return typeof _orchestrationFlowCatalog !== 'undefined'
      && _orchestrationFlowCatalog
    ? _orchestrationFlowCatalog.snapshot() : [];
}

function _applyFlowUI(flowVal) {
  /* A builtin Flow remains distinct from standalone Autopilot mode. */
  activeFlow = flowVal || '';
  // Reflect the radio-style selection in the unified dropdown list.
  document.querySelectorAll('#flowMenuList .flow-menu-item').forEach(el => {
    const selected = (el.dataset.flow || '') === activeFlow;
    el.classList.toggle('selected', selected);
    el.setAttribute('aria-selected', selected ? 'true' : 'false');
    el.setAttribute('aria-checked', selected ? 'true' : 'false');
    el.setAttribute('tabindex', selected ? '0' : '-1');
  });
  _paintAgentModeUI();
  if (typeof updateMobileSheet === 'function') updateMobileSheet();
}

function _reconcileActiveFlowCatalog(custom) {
  if (typeof reconcileOrchestrationFlowSelection !== 'function') return false;
  const next = reconcileOrchestrationFlowSelection(activeFlow, custom, true);
  if (next === activeFlow) return false;
  _applyFlowUI(next);
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (conv) {
    conv.activeFlow = next;
    if (typeof scheduleConversationSettingsPersist === 'function') {
      scheduleConversationSettingsPersist(conv);
    }
  }
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  if (typeof debugLog === 'function') {
    debugLog(t('toolbar.flowRemoved'), 'warning');
  }
  return true;
}

function _flowDisplayName(flowVal) {
  return typeof orchestrationFlowPickerDisplayName === 'function'
    ? orchestrationFlowPickerDisplayName(
      flowVal, _flowCatalogSnapshot(), t)
    : String(flowVal || '');
}

function _syncActiveFlowLabel() {
  _paintAgentModeUI();
  if (typeof updateMobileSheet === 'function') updateMobileSheet();
}

function setActiveFlow(flowVal) {
  /* Keep builtin flows on the Flow path; do not alias them to Agent Mode. */
  if (_agentInteractionChangeBlocked()) return false;
  const wasAutopilot = autopilotEnabled;
  const nextFlow = String(flowVal || '');
  if (nextFlow) {
    _applyAgentModeUI('standard');
    if (typeof _applyImageGenUI === 'function') _applyImageGenUI(false);
    if (wasAutopilot) _disarmAutopilot();
  }
  _applyFlowUI(nextFlow);
  captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog(
    activeFlow ? `Flow: ${_flowDisplayName(activeFlow)} — runs on the orchestration engine`
               : "Flow: none",
    "success",
  );
  _closeAgentModeMenu();
  return true;
}

function _closeAgentModeMenu() {
  const submenu = document.getElementById('submenuAgentMode');
  if (!submenu || !submenu.classList.contains('open')) return false;
  submenu.classList.remove('open');
  const trigger = document.getElementById('agentModeToggle');
  trigger?.classList.remove('open');
  trigger?.setAttribute('aria-expanded', 'false');
  _flowMenuRenderGeneration++;
  return true;
}

function toggleAgentModeMenu(event) {
  if (event) event.stopPropagation();
  const submenu = document.getElementById('submenuAgentMode');
  if (!submenu) return false;
  const willOpen = !submenu.classList.contains('open');
  if (!willOpen) return _closeAgentModeMenu();
  toggleSubmenu('submenuAgentMode');
  const workflows = document.getElementById('agentWorkflowSection');
  if (workflows && workflows.style.display !== 'none') _populateFlowMenu();
  return true;
}

function handleAgentModeMenuTriggerKey(event) {
  if (!event || (event.key !== 'ArrowDown' && event.key !== 'ArrowUp')) {
    return false;
  }
  event.preventDefault();
  event.stopPropagation();
  const menu = document.getElementById('submenuAgentMode');
  const wasOpen = !!menu?.classList.contains('open');
  if (!wasOpen) toggleAgentModeMenu();
  const rows = document.querySelectorAll(
    '#agentModeMenu [role="radio"]:not(:disabled)');
  if (!rows.length) return false;
  let row = Array.prototype.find.call(rows, candidate =>
    candidate.getAttribute('aria-checked') === 'true');
  if (!row) row = event.key === 'ArrowUp' ? rows[rows.length - 1] : rows[0];
  const focusRow = () => {
    if (!menu?.classList.contains('open') || !row.isConnected || row.disabled) return;
    row.focus();
  };
  const focusRowWhenVisible = (remainingFrameChecks) => {
    if (!menu?.classList.contains('open') || !row.isConnected || row.disabled) return;
    const visibility = typeof window.getComputedStyle === 'function'
      ? window.getComputedStyle(row).visibility
      : 'visible';
    if (visibility !== 'hidden') {
      focusRow();
      return;
    }
    if (remainingFrameChecks > 0) {
      window.requestAnimationFrame(() => {
        focusRowWhenVisible(remainingFrameChecks - 1);
      });
    }
  };
  /* The popover animates visibility for 160 ms. Chromium refuses focus while
   * that transition still computes to hidden, so wait for a paintable frame.
   * Keep the retry bounded in case a future style intentionally stays hidden. */
  if (!wasOpen && typeof window !== 'undefined'
      && typeof window.requestAnimationFrame === 'function') {
    window.requestAnimationFrame(() => { focusRowWhenVisible(30); });
  } else {
    focusRow();
  }
  return true;
}

let _flowMenuRenderGeneration = 0;

function openOrchestrationFromAgentMode() {
  _closeAgentModeMenu();
  if (typeof closeMobileSheet === 'function') closeMobileSheet();
  /* The Studio stylesheet lives in the lazy features/orchestration chunk.
   * Only the feature-bridge stub on runtimeScope loads that chunk; calling
   * the lexical openOrchestration directly mounts the shell unstyled. */
  const lexical = typeof openOrchestration === 'function' ? openOrchestration : null;
  const routed = (typeof runtimeScope !== 'undefined'
      && typeof runtimeScope.openOrchestration === 'function'
      && runtimeScope.openOrchestration !== lexical)
    ? runtimeScope.openOrchestration
    : lexical;
  if (!routed) return false;
  routed();
  return true;
}

function _renderFlowMenu(custom) {
  const list = document.getElementById("flowMenuList");
  if (!list || typeof projectOrchestrationFlowPickerItems !== 'function') {
    return false;
  }
  const focused = list.contains(document.activeElement)
    ? document.activeElement.getAttribute('data-flow') : null;
  const catalog = typeof _orchestrationFlowCatalog !== 'undefined'
    ? _orchestrationFlowCatalog : null;
  let catalogNoticeVisible = false;
  if (typeof renderOrchestrationFlowCatalogNotice === 'function') {
    catalogNoticeVisible = renderOrchestrationFlowCatalogNotice(
      document.getElementById('flowMenuStatus'), catalog, t);
  }
  const items = projectOrchestrationFlowPickerItems(custom, t, {
    includeNone: false,
    includeBuiltins: false,
  });
  list.innerHTML = items.length ? items.map(it =>
    '<button type="button" class="flow-menu-item' + ((it.flow === activeFlow) ? ' selected' : '') + '" '
    + 'role="radio" aria-checked="' + (it.flow === activeFlow ? 'true' : 'false') + '" '
    + 'aria-selected="' + (it.flow === activeFlow ? 'true' : 'false') + '" '
    + 'data-flow="' + escapeHtml(it.flow) + '">'
    + '<span class="flow-menu-icon">' + orchestrationFlowPickerIcon(it.flow) + '</span>'
    + '<span class="flow-menu-text"><span class="flow-menu-name">' + escapeHtml(it.name) + '</span>'
    + '<span class="flow-menu-desc">' + escapeHtml(it.desc) + '</span></span>'
    + '<span class="flow-menu-check">✓</span>'
    + '</button>'
  ).join('') : (catalogNoticeVisible ? ''
    : '<div class="agent-workflow-empty">'
      + escapeHtml(t('toolbar.noSavedWorkflows')) + '</div>');
  if (typeof wireOrchestrationFlowPicker === 'function') {
    wireOrchestrationFlowPicker(list, {
      onSelect: function (flow) {
        setActiveFlow(flow);
        const trigger = document.getElementById('agentModeToggle');
        if (trigger) trigger.focus();
      },
      onEscape: function () {
        if (!_closeAgentModeMenu()) return;
        const trigger = document.getElementById('agentModeToggle');
        if (trigger) trigger.focus();
      },
    });
  }
  if (focused !== null) {
    const rows = list.querySelectorAll('[role="radio"][data-flow]');
    const row = Array.prototype.find.call(rows, candidate =>
      (candidate.getAttribute('data-flow') || '') === focused);
    if (row) row.focus();
  }
  _paintAgentModeUI();
  return true;
}

async function _populateFlowMenu() {
  const list = document.getElementById("flowMenuList");
  if (!list) return;
  // One shared catalogue owns custom-workflow transport, projection,
  // freshness and failure fallback for desktop and mobile.
  const owner = ++_flowMenuRenderGeneration;
  const catalog = typeof _orchestrationFlowCatalog !== 'undefined'
      && _orchestrationFlowCatalog ? _orchestrationFlowCatalog : null;
  const request = catalog ? catalog.load() : Promise.resolve([]);
  _renderFlowMenu(catalog ? catalog.snapshot() : []);
  const custom = await request;
  if (owner !== _flowMenuRenderGeneration) return false;
  return _renderFlowMenu(custom);
}

/* ═══ Folder management UI ═══ */
