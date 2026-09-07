// Install the generated typed endpoint catalogue only after the retained Api
// facade has created its stable `orchestrations` placeholder. The lightweight
// saved-Flow catalogue remains available without loading Studio or Task Mode.
installOrchestrationApiClient(Api);
const _orchestrationFlowCatalog = createOrchestrationFlowCatalog({
  api: resolveOrchestrationApiClient,
  onError: (error) => {
    console.warn('[Flow catalog] list failed:', error?.message ?? error);
  },
  onChange: (items) => {
    if (typeof _reconcileActiveFlowCatalog === 'function') {
      _reconcileActiveFlowCatalog(items);
    }
    if (typeof _syncActiveFlowLabel === 'function') _syncActiveFlowLabel();
  },
  onObserverError: (error) => {
    console.error('[Flow catalog] change observer failed:', error);
  },
});

function _commitFeatureFlags(nextFlags) {
  _featureFlags = nextFlags;
  const badge = document.getElementById('optimizerBadge');
  if (badge) badge.style.display = _featureFlags.optimizer_enabled === false ? 'none' : 'inline-flex';
  runtimeScope._applyDebugModeVisibility?.();
  runtimeScope._applyTradingVisibility?.();
  if (typeof renderConversationList === 'function') renderConversationList();
  if (typeof getActiveConv === 'function') {
    const conversation = getActiveConv();
    if (conversation) runtimeScope.requestAuthoritativeConversationRender(
      conversation.id, { forceScroll: false },
    );
  }
}

const _featureFlagsLoader = createFeatureFlagsLoader({
  current: () => _featureFlags,
  commit: _commitFeatureFlags,
  request: () => Api.request('/api/v1/features', { parse: 'response' }),
  onError: (error) => console.warn('[features] flags unavailable', error),
});

export const loadFeatureFlags = _featureFlagsLoader.load;

// BEGIN GENERATED RUNTIME ACTIONS — scripts/update_runtime_actions.mjs
const runtimeActions = Object.freeze({
  _cmdBodyToggle,
  _cmdHeaderToggle,
  _cmdInterruptClick,
  _cmdOutputToggle,
  _downloadGenImage,
  _mobileCompactNow,
  _openExternalAsset,
  _openImageFullscreen,
  _recoverOfflineConversations,
  _removeReplyQuote,
  _safeClipboardWrite,
  _swarmCopyAgentId,
  _swarmToggleClass,
  _syncRangeOutput,
  _timerWatcherToggle,
  _toggleConvGroup,
  _toggleCostPopover,
  aiCompressLog,
  applyLogClean,
  cancelAutopilotMarker,
  clearProject,
  closeApplyModal,
  closeChatModeMenu,
  closeMobileSheet,
  closePreview,
  closeSidebarSearch,
  confirmApplyCode,
  copyCode,
  copyTableMarkdown,
  cycleSearchMode,
  handleAgentModeMenuTriggerKey,
  handleFileUpload,
  handleKeyDown,
  hideLogCleanBanner,
  loadConversation,
  newChat,
  openApplyModal,
  openOrchestrationFromAgentMode,
  openVideoUrl,
  previewLogClean,
  previewPendingImage,
  previewPendingPdfText,
  removeImage,
  removePdfText,
  removeVideo,
  resolvePreference,
  resolveWriteApproval,
  scrollChatToBottom,
  selectTheme,
  selectThinkingDepth,
  sendMessage,
  setAgentMode,
  setChatMode,
  submitHumanGuidanceChoice,
  submitHumanGuidanceFreeText,
  submitStdinEof,
  submitStdinInput,
  toggleAgentModeMenu,
  toggleAutoApply,
  toggleAutoTranslate,
  toggleChatModeMenu,
  toggleCodeBlock,
  toggleHumanGuidance,
  toggleImageGenTool,
  toggleMobileSheet,
  togglePresetDropdown,
  toggleProjectBarReadOnly,
  toggleSidebar,
  toggleSidebarSearch,
  toggleSubmenu,
  undoContextChange,
  updateMobileDepth,
  updateMobileSheet,
  updateSubmenuCounts,
});
// END GENERATED RUNTIME ACTIONS

// Stable mutable port for the demand-loaded image owner. The feature registry
// caches module-owned overrides, so individual mutable values must not be
// written through that proxy: this object keeps every read live against the
// retained conversation/composer authority across navigation and resets.
const ImageGenerationComposerState = Object.freeze({
  get activeConversationId() { return activeConvId; },
  set activeConversationId(value) {
    activeConvId = value == null ? null : String(value);
  },
  get conversations() { return conversations; },
  get pendingImages() { return pendingImages; },
  set pendingImages(value) { if (Array.isArray(value)) pendingImages = value; },
  get imageGenMode() { return imageGenMode; },
  get planMode() { return planMode; },
  get autopilotEnabled() { return autopilotEnabled; },
  get activeFlow() { return activeFlow; },
  get selectedModel() { return _igSelectedModel; },
  set selectedModel(value) { _igSelectedModel = String(value || ''); },
  get selectedProviderId() { return _igSelectedProviderId; },
  set selectedProviderId(value) { _igSelectedProviderId = String(value || ''); },
  get selectedAspect() { return _igSelectedAspect; },
  set selectedAspect(value) { _igSelectedAspect = String(value || '1:1'); },
  get selectedResolution() { return _igSelectedResolution; },
  set selectedResolution(value) {
    _igSelectedResolution = String(value || '1K');
  },
  get selectedCount() { return _igSelectedCount; },
  set selectedCount(value) {
    _igSelectedCount = Math.max(1, Number(value) || 1);
  },
  get hiddenModels() { return _hiddenIgModels; },
});

// Project presentation loads long after boot but must always observe the
// current conversation and authoritative project object. A frozen live port
// avoids feature-registry override snapshots while keeping mutation funnels in
// the retained state owner.
const ProjectPresentationShellState = Object.freeze({
  get activeConversationId() { return activeConvId; },
  set activeConversationId(value) {
    activeConvId = value == null ? null : String(value);
  },
  get conversations() { return conversations; },
  get projectState() { return projectState; },
  set projectState(value) {
    if (value && typeof value === 'object') projectState = value;
  },
  get sessionStorage() {
    try { return globalThis.sessionStorage || null; }
    catch (_error) { return null; }
  },
});

// Local Control keeps the independent browser/desktop permission flags in
// retained conversation state. Its demand-loaded presentation must always
// observe the current values after navigation or a settings restore.
const LocalControlShellState = Object.freeze({
  get browserEnabled() { return browserEnabled; },
  get desktopEnabled() { return desktopEnabled; },
});

Object.assign(runtimeScope, {
  // Lazy retained feature runtimes declare and validate every lexical service
  // they consume. Keep this list explicit: a moved section must not fall back
  // to an accidental window binding that can disappear under ESM evaluation.
  Api: runtimeScope.Api,
  BASE_PATH,
  ConvCache,
  Icon,
  IconDot,
  CompactionHistoryState,
  ImageGenerationComposerState,
  DebugShellState,
  LocalControlShellState,
  ProjectPresentationShellState,
  _applyProjectData,
  _applyBrowserUI,
  _applyCodeExecUI,
  _applyDesktopUI,
  _applyFetchEnabledUI,
  _applyFlowUI,
  _lcUpdateBadge,
  _agentInteractionChangeBlocked,
  _applyModelUI,
  _applySearchModeUI,
  _brandSvg,
  _compareModelIds,
  _compareModelsByDisplayName,
  _detectBrand,
  _getConvProjectPath,
  _getCurrentTheme,
  _findRenderedNativeTurnNode,
  _loadServerConfigAndPopulate,
  _modelBrand,
  _modelRoutingDropdownModels,
  _modelShortName,
  _orchestrationFlowCatalog,
  _paperModelPickerState,
  _populateModelDropdown,
  _sortModelEntriesByDisplayName,
  _sortModelsByDisplayName,
  _sortedBrandKeys,
  _saveConvProjectPath,
  _safeClipboardWrite,
  _updateProjectUI,
  _warnModelCapsMissing,
  _setHiddenIgModels: (value) => {
    if (value instanceof Set) _hiddenIgModels = value;
  },
  _setHiddenModels: (value) => {
    if (value instanceof Set) _hiddenModels = value;
  },
  _setModelPricingCache: (value) => {
    if (value && typeof value === 'object') _modelPricingCache = value;
  },
  // Native feature owners reach retained composer settings only through this
  // injected service table. The feature must not recreate settings capture or
  // call a compatibility endpoint of its own.
  _applyImageGenUI,
  _applyMemoryUI,
  _buildConvSettings,
  _scheduleReflow,
  _waitForImageProcessing,
  brandLogoImgAttrs,
  captureActiveConversationSettings,
  config,
  buildTurnNav,
  debugLog,
  distillFallbackDetail,
  errorEnvelopeKind,
  errorEnvelopeMessage,
  fallbackCauseParts,
  fallbackKindLabel,
  formatCny,
  generateId,
  getActiveConv,
  getActiveFolderId,
  getCompactionHistory,
  isErrorEnvelope,
  loadProjectStatus,
  loadConversation,
  loadCompactionHistory,
  newChat,
  normalizeErrorEnvelope,
  onProjectAttached,
  onProjectCleared,
  prefersReducedMotion,
  pushSubscribe,
  pushUnsubscribe,
  reconcileConversationCatalogMetadata,
  refreshInputSendHint,
  resolveOrchestrationApiClient,
  renderConversationList,
  renderImagePreviews,
  renderMarkdown,
  renderSegmentTimelineHTML,
  renderToolRoundsHTML,
  scrollToBottom,
  setActiveFlow,
  showAlert,
  showChoice,
  showConfirm,
  showPrompt,
  apiUrl,
  showToast,
  updateSendButton,
  renderErrorEnvelope,
  // The per-message Export button (data-tofu-action=
  // "ExportImages.exportMessageWithPreview(...)") resolves its receiver
  // through runtimeScope — module-private here, so an unpublished name made
  // every export click a refused no-op.
  ExportImages,
});

// Lazy feature runtimes must observe current navigation/project authority,
// not a value captured when their chunk first evaluated. These accessors keep
// mutable shell state private while providing a live, declared read seam.
Object.defineProperties(runtimeScope, {
  activeConvId: {
    configurable: false,
    enumerable: false,
    get: () => activeConvId,
    set: (value) => { activeConvId = value == null ? null : String(value); },
  },
  activeFlow: {
    configurable: false,
    enumerable: false,
    get: () => activeFlow,
    set: (value) => { activeFlow = value == null ? '' : String(value); },
  },
  conversations: {
    configurable: false,
    enumerable: false,
    get: () => conversations,
    set: (value) => { if (Array.isArray(value)) conversations = value; },
  },
  _featureFlags: {
    configurable: false,
    enumerable: false,
    get: () => _featureFlags,
    set: (value) => {
      if (value && typeof value === 'object') _featureFlags = value;
    },
  },
  _hiddenIgModels: {
    configurable: false,
    enumerable: false,
    get: () => _hiddenIgModels,
  },
  _hiddenModels: {
    configurable: false,
    enumerable: false,
    get: () => _hiddenModels,
  },
  _modelPricingCache: {
    configurable: false,
    enumerable: false,
    get: () => _modelPricingCache,
  },
  _registeredModels: {
    configurable: false,
    enumerable: false,
    get: () => _registeredModels,
    set: (value) => { if (Array.isArray(value)) _registeredModels = value; },
  },
  projectState: {
    configurable: false,
    enumerable: false,
    get: () => projectState,
    set: (value) => {
      if (value && typeof value === 'object') projectState = value;
    },
  },
});

export function resolveRuntimeAction(name) {
  const candidate = runtimeActions[name] || runtimeScope[name];
  return typeof candidate === 'function' ? candidate : undefined;
}

export function getRuntimeService(name) {
  // Retained application functions live in the generated action table, while
  // typed feature owners and mutable state live in runtimeScope. Feature
  // modules need both through one private read port during the migration.
  return runtimeScope[name] ?? runtimeActions[name];
}

export function setRuntimeService(name, value) {
  runtimeScope[name] = value;
  /** @type {Record<string, unknown>} */ (globalThis)[name] = value;
}

/* Retained vanilla call sites still reach cross-module functions by bare
 * identifier (`typeof X === "function"` guards + direct calls) — names that
 * only resolved in the classic-script era, when every module wrote to
 * window. In this ESM module runtimeScope members are module-private, so an
 * unpublished name silently fails its guard: the 2026-08-14 turn-ctx
 * disappearance (rail + fold + snapshot capture + done-reconcile all dead,
 * zero console output) was exactly this seam. Publish every runtimeScope
 * member once here, after all sections have evaluated; browser/node
 * built-ins win (`in` skip). setRuntimeService() re-publishes so late
 * feature-owner registrations keep the invariant. Section-based jsdom
 * tests are structurally blind to this seam (tests/_runtime_sections.py
 * rebinds runtimeScope to window in the test view) — the honest check is
 * the real-bundle suite: tests/test_frontend_runtime_scope_global_bridge.py. */
const _globalPublishTarget = /** @type {Record<string, unknown>} */ (globalThis);
/* Mutable state used by retained classic-script probes/tests must stay live
 * across the ESM boundary.  Publishing a getter's current value would leave
 * `window.conversations` stale after the module replaces the array, and a
 * plain `activeConvId` value would not feed assignments back into this
 * module. */
if (!("conversations" in _globalPublishTarget)) {
  try {
    Object.defineProperty(_globalPublishTarget, "conversations", {
      configurable: true,
      get: () => conversations,
      set: (value) => { if (Array.isArray(value)) conversations = value; },
    });
  } catch (_err) {
    console.warn('[runtimeState] conversations bridge skipped', _err);
  }
}
if (!("activeConvId" in _globalPublishTarget)) {
  try {
    Object.defineProperty(_globalPublishTarget, "activeConvId", {
      configurable: true,
      get: () => activeConvId,
      set: (value) => { activeConvId = value == null ? null : String(value); },
    });
  } catch (_err) {
    console.warn('[runtimeState] activeConvId bridge skipped', _err);
  }
}
for (const [_name, _fn] of [
  ["_resumePendingTranslations", _resumePendingTranslations],
]) {
  if (typeof _globalPublishTarget[_name] === "function") continue;
  try { _globalPublishTarget[_name] = _fn; }
  catch (_err) { console.warn('[runtimeState] ' + _name + ' bridge skipped', _err); }
}
for (const _name of Object.keys(runtimeScope)) {
  if (_name in _globalPublishTarget) continue;
  try {
    _globalPublishTarget[_name] = runtimeScope[_name];
  } catch (_err) {
    // A poisoned accessor on ONE key must not take down app boot —
    // warn loudly and keep publishing the rest.
    console.warn('[runtimeScope] publish skipped for ' + _name, _err);
  }
}
/* The generated action table is the public surface used by legacy inline
 * handlers and by the boot readiness probe (`typeof sendMessage`). It is not
 * part of runtimeScope, so publishing only the scope leaves those names
 * module-private under the ESM bundle. Publish actions when the global slot is
 * absent or merely an undefined placeholder, while preserving real browser
 * built-ins. */
for (const _name of Object.keys(runtimeActions)) {
  if (typeof _globalPublishTarget[_name] === 'function') continue;
  try {
    _globalPublishTarget[_name] = runtimeActions[_name];
  } catch (_err) {
    console.warn('[runtimeActions] publish skipped for ' + _name, _err);
  }
}
