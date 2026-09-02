export async function loadFeatureFlags() {
  try {
    const response = await window.Api.request('/api/v1/features', { parse: 'response' });
    if (!response.ok) return;
    _featureFlags = await response.json();
    const badge = document.getElementById('optimizerBadge');
    if (badge) badge.style.display = _featureFlags.optimizer_enabled === false ? 'none' : 'inline-flex';
    _applyDebugModeVisibility();
    _applyTradingVisibility();
    if (typeof renderConversationList === 'function') renderConversationList();
    if (typeof getActiveConv === 'function') {
      const conversation = getActiveConv();
      if (conversation) runtimeScope.requestAuthoritativeConversationRender(
        conversation.id, { forceScroll: false },
      );
    }
  } catch (error) {
    console.warn('[features] flags unavailable', error);
  }
}

// BEGIN GENERATED RUNTIME ACTIONS — scripts/update_runtime_actions.mjs
const runtimeActions = Object.freeze({
  _addAlias,
  _addApiKey,
  _addExtraHeader,
  _addFace,
  _addLocalEndpoint,
  _addModel,
  _applyBulkEditEndpoints,
  _applyMatrixRecommendations,
  _autofillOpenReview,
  _browserAccessDenyRead,
  _cancelTimer,
  _clearConvCacheFromSettings,
  _clearLocalEndpoints,
  _clearMatrixProbe,
  _clearRecentSearch,
  _cmdBodyToggle,
  _cmdHeaderToggle,
  _cmdInterruptClick,
  _cmdOutputToggle,
  _copyPaperRebuttal,
  _copyPaperReport,
  _copyPaperReview,
  _copyUpdateLog,
  _deleteApiKey,
  _deleteExtraHeader,
  _deleteFace,
  _deleteLocalEndpoint,
  _deleteModel,
  _deleteProvider,
  _discoverLocalModels,
  _discoverModels,
  _downloadGenImage,
  _editMatrixCell,
  _editModel,
  _exportPaperReport,
  _exportPaperReview,
  _filterRecentProjects,
  _generatePaperRebuttal,
  _generatePaperReport,
  _generatePaperReview,
  _getMdTemp,
  _handlePaperFileUpload,
  _jumpToTimerConv,
  _knowledgeApplyCatalog,
  _knowledgeClearCatalog,
  _knowledgeDebounceCatalog,
  _knowledgeDismissUploadReport,
  _knowledgeDrag,
  _knowledgeDrop,
  _knowledgeEsc,
  _knowledgeGoPage,
  _knowledgeLoadMoreContent,
  _knowledgeRefresh,
  _knowledgeReindex,
  _knowledgeRemove,
  _knowledgeSearch,
  _knowledgeSetCategory,
  _knowledgeSetSort,
  _knowledgeToggle,
  _knowledgeToggleContent,
  _knowledgeToggleVisual,
  _knowledgeUpload,
  _lcDecide,
  _lcEnsureAgentRelay,
  _logoutManagedProvider,
  _mcpApplyUpdate,
  _mcpCloseAddModal,
  _mcpCloseInstallModal,
  _mcpConnectAll,
  _mcpDoInstall,
  _mcpEnvPresetChanged,
  _mcpFilterCatalog,
  _mcpOpenAddModal,
  _mcpOpenInstallModal,
  _mcpPurge,
  _mcpQuickInstall,
  _mcpReconnect,
  _mcpSaveServer,
  _mcpSetAllTools,
  _mcpSetCategory,
  _mcpSetScope,
  _mcpToggleTool,
  _mcpToggleToolsPanel,
  _mcpTransportChanged,
  _mcpUninstall,
  _mobileCompactNow,
  _mpRemove,
  _mpToggleReadOnly,
  _mydayAddTodo,
  _mydayCalNext,
  _mydayCalPrev,
  _mydayDeleteInheritedTodo,
  _mydayDeleteTodo,
  _mydaySelectDay,
  _mydayStartTodoConv,
  _mydayStartTodoConvInherited,
  _mydayStartTodoConvUnfinished,
  _mydayToggleInheritedTodo,
  _mydayToggleStreamStatus,
  _mydayToggleTodo,
  _mydayTriggerGenerate,
  _oauthCopyAuthLink,
  _oauthCopyDeviceCode,
  _oauthDeviceLogin,
  _oauthLogin,
  _oauthLogout,
  _oauthManualSubmit,
  _onApiKeyRowEdit,
  _onDropdownVisibilityChange,
  _onExtraHeaderRowEdit,
  _onFacePinChange,
  _onFaceProtoChange,
  _onFaceRowEdit,
  _onIgVisibilityChange,
  _onKeyLabelEdit,
  _onLocalEndpointEdit,
  _onModelIdDraftInput,
  _onModelProtoChange,
  _onProvField,
  _onRebuttalInputChange,
  _openActiveCompaction,
  _openBulkEditEndpoints,
  _openExternalAsset,
  _openImageFullscreen,
  _openSkillsStoreFromMemory,
  _optApprove,
  _optimizerRunNow,
  _optReject,
  _optRevert,
  _optToggleHistory,
  _pickLocalPreset,
  _podcastExportScript,
  _podcastSeekSegment,
  _podcastSleepTimerChange,
  _poolTagCommit,
  _poolTagKey,
  _poolTagRemove,
  _poolTagSplit,
  _probeAllDropdownModels,
  _probeLocalEndpoint,
  _probeMatrixScope,
  _proxyBypassDelete,
  _proxyBypassRefreshCount,
  _proxyPoolDelete,
  _proxyPoolMove,
  _proxyPoolSyncMeta,
  _proxyPoolTest,
  _proxyPoolToggleEditor,
  _proxyPoolToggleUrlVisibility,
  _proxyPoolUrlChanged,
  _pvEsc,
  _recoverOfflineConversations,
  _refreshCostExperimentReport,
  _regeneratePaperRebuttal,
  _regeneratePaperReport,
  _regeneratePaperReview,
  _removeAlias,
  _removeReplyQuote,
  _runMatrixProbe,
  _runUpdateCheck,
  _safeClipboardWrite,
  _saveMatrixCell,
  _saveModelEdit,
  _scrollReportToHeading,
  _searchProfileChanged,
  _setMatrixAttempts,
  _setReportLang,
  _showPaperLandingForNew,
  _showTemplateMenu,
  _skillsInstallFromInput,
  _swarmCopyAgentId,
  _swarmToggleClass,
  _switchMtProvider,
  _syncCostExperimentUi,
  _syncFromTemplate,
  _syncRangeOutput,
  _syncResponsesExperimentUi,
  _testMtProvider,
  _testSearchBrowser,
  _timerWatcherToggle,
  _toggleAllDropdownModels,
  _toggleAllIgModels,
  _toggleApiKeyVisibility,
  _toggleConvGroup,
  _toggleCostPopover,
  _toggleIdAccess,
  _toggleMatrixView,
  _toggleModelCatalogSync,
  _toggleModelEnabled,
  _toggleModelThinking,
  _togglePaperReportExportMenu,
  _togglePaperReportModelDropdown,
  _togglePaperReviewExportMenu,
  _togglePaperReviewModelDropdown,
  _toggleProviderExpand,
  _toggleReviewVenueDropdown,
  _toggleStgProvFold,
  _triggerTimer,
  _viewTimerLog,
  addLocalProvider,
  addProvider,
  aiCompressLog,
  applyLogClean,
  applySystemPromptEditor,
  applyUpdate,
  BackendOfflineMonitorProbeNow,
  BackendOfflineMonitorSnooze,
  browseDirectory,
  browseParent,
  cancelAutopilotMarker,
  clearProject,
  clearRecentProjects,
  closeApplyModal,
  closeChatModeMenu,
  closeDailyReport,
  closeDebug,
  closeKnowledgeBase,
  closeLocalControlModal,
  closeMobileSheet,
  closePreview,
  closeProjectModal,
  closeSettings,
  closeSidebarSearch,
  closeSystemPromptEditor,
  closeUpdateModal,
  confirmApplyCode,
  copyCode,
  copyDebugContent,
  copyTableMarkdown,
  cycleSearchMode,
  enterImageGenMode,
  escapeHtml,
  exitImageGenMode,
  exportServerConfig,
  extractFencedBlocks,
  generateImageDirect,
  getRuntimeService,
  handleAgentModeMenuTriggerKey,
  handleFileUpload,
  handleKeyDown,
  hideLogCleanBanner,
  highlightCodeInHtml,
  importServerConfig,
  installSkillFromFileInput,
  loadConversation,
  mpAddBrowsedPath,
  mpAddFolder,
  mpApplyFolders,
  mpDeleteFolder,
  mpNewFolder,
  newChat,
  openApplyModal,
  openDailyReport,
  openKnowledgeBase,
  openLocalControlModal,
  openOrchestration,
  openOrchestrationFromAgentMode,
  openProjectModal,
  openSettings,
  openSystemPromptEditor,
  openToolDebugPanel,
  openTradingMode,
  openUpdateDialog,
  openVideoUrl,
  pmMobileTab,
  previewLogClean,
  previewPendingImage,
  previewPendingPdfText,
  raw,
  removeImage,
  removePdfText,
  removePendingQueueItem,
  removeVideo,
  resetSystemPromptBlocks,
  resolvePreference,
  resolveRuntimeAction,
  resolveWriteApproval,
  restartServer,
  saveSettings,
  scrollChatToBottom,
  selectBrowsedFolder,
  selectIgAspect,
  selectIgCount,
  selectIgModel,
  selectIgResolution,
  selectRecentProject,
  selectTheme,
  selectThinkingDepth,
  sendMessage,
  setAgentMode,
  setChatMode,
  setRuntimeService,
  shutdownServer,
  submitHumanGuidanceChoice,
  submitHumanGuidanceFreeText,
  submitStdinEof,
  submitStdinInput,
  switchSettingsTab,
  toggleAgentModeMenu,
  toggleAutoApply,
  toggleAutoTranslate,
  toggleBrowserFromLocalModal,
  toggleChatModeMenu,
  toggleCodeBlock,
  toggleDebug,
  toggleDesktopFromLocalModal,
  toggleHiddenDirs,
  toggleHumanGuidance,
  toggleIgModelDropdown,
  toggleImageGenTool,
  toggleMobileSheet,
  togglePendingQueueCollapsed,
  togglePresetDropdown,
  toggleProjectBarReadOnly,
  toggleSidebar,
  toggleSidebarSearch,
  toggleSubmenu,
  toggleTimerPanel,
  undoContextChange,
  updateMobileDepth,
  updateMobileSheet,
  updateSubmenuCounts,
});
// END GENERATED RUNTIME ACTIONS

Object.assign(runtimeScope, {
  // Native feature owners reach retained composer settings only through this
  // injected service table. The feature must not recreate settings capture or
  // call a compatibility endpoint of its own.
  _applyMemoryUI,
  captureActiveConversationSettings,
  buildTurnNav,
  closeRequestInspector,
  openRequestInspector,
  // The per-message Export button (data-tofu-action=
  // "ExportImages.exportMessageWithPreview(...)") resolves its receiver
  // through runtimeScope — module-private here, so an unpublished name made
  // every export click a refused no-op.
  ExportImages,
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
