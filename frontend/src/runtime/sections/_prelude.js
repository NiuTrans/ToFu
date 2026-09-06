// @ts-check
/* Generated once during the classic-to-ESM migration. Vite owns this module. */
import { _applyI18n, _i18nLang, _onLanguageChange, _syncLangPicker, setLanguage, t } from '../i18n';
import { iconHtml, statusDotIconHtml, welcomePillsHtml } from '../icons';
import { escapeHtml, raw, safeHtml } from '../html-safety';
import { createDialogServices } from '../lazy-dialog-controller';
import { createBackendAvailabilityMonitor } from '../backend-availability-monitor';
import { createAvailabilityHealthProbeCoordinator } from '../availability-health-probe';
import { createFeatureFlagsLoader } from '../core/feature-flags-loader';
import { createImageViewerActionsController } from '../image-viewer-actions';
import { installMarkdownPolicy } from '../markdown-policy';
import { createStorageAvailabilityMonitor } from '../storage-availability-monitor';
import {
  createErrorEnvelopePresentation,
  distillFallbackDetail,
} from '../error-presentation';
import {
  errorEnvelopeFingerprint,
  isErrorEnvelope as isTypedErrorEnvelope,
  normalizeErrorEnvelope as normalizeTypedErrorEnvelope,
} from '../api/errors';
import { apiTransport as requiredApiTransport } from '../api/transport';
import {
  CONVERSATION_SYNC_STREAM_POLICY,
  conversationSyncApi,
  decodeConversationInvalidation,
} from '../api/conversation-sync.generated';
import { createLifecycleScope } from '../lifecycle';
import {
  DEFAULT_ASYNC_POOL_CONCURRENCY,
  runWithConcurrency,
} from '../core/async-pool';
import { conversationConnectionHealth } from '../core/connection-health';
import {
  resolveBrowserIndexedDb,
  resolveBrowserLocalStorage,
} from '../core/browser-storage';
import {
  createLazyConversationMetadataCache,
} from '../core/conversation-metadata-cache-lazy';
import { createCompactionHistoryState } from '../core/compaction-history-state';
import { HTTP_RESULT } from '../core/http-result';
import { createCurrentUserIdentityController } from '../core/current-user';
import {
  frameBelongsToOwner,
  narrowConvCatalogFrame,
  narrowFoldersChangedFrame,
} from '../core/frame-identity';
import {
  CHAT_EXCLUDED_CAPS_FALLBACK,
  createModelCapabilityTaxonomy,
} from '../core/model-capability-taxonomy';
import {
  createModelBrandResolver,
  detectModelBrand,
} from '../core/model-brand-detection';
import { brandIconHtml } from '../core/model-brand-icons';
import { createModelDisplayNames } from '../core/model-display-names';
import { createModelGroupPolicy } from '../core/model-group';
import { modelDisplayUnits } from '../core/model-display-fold';
import { createRecentModelsController } from '../core/recent-models';
import { createRoleAvatarIcons } from '../core/role-avatar-icons';
import { createCookieCaptureConsentController } from '../core/cookie-capture-consent';
import { createBuildWatchController } from '../core/build-watch-controller';
import {
  NATIVE_BRIDGE_POLICY,
  createNativeVisibility,
} from '../core/native-bridge';
import { clientLogFlushBaseDelayMs, createClientLogFlushScheduler } from '../core/client-log-flush-scheduler';
import { createDebugRuntimeOwner } from '../core/debug-runtime-owner';
import { TRANSLATION_CLAIMS } from '../core/translation-claim-registry';
import {
  buildTurnSubmissionExtra,
  rebindTurnInputContext,
} from '../core/turn-command';
import { createConversationTurnRuntime } from '../core/turn-runtime';
import { createConversationRefresh } from '../conversation/application/conversation-refresh';
import { createConversationWakeRecovery } from '../conversation/application/conversation-wake-recovery';
import { createSendPreparationOverlayController } from '../conversation/application/send-preparation-overlay';
import { createPreferenceActionsController } from '../features/memory/preference-actions';
import { createPresenceSummaryController } from '../features/presence-summary-controller';
import {
  conversationFullIdById as queryConversationFullIdById,
  conversationTitleById as queryConversationTitleById,
  resolveConversationAutoTranslate,
} from '../conversation/application/conversation-catalog-queries';
import {
  createConversationCatalogReconciler,
} from '../conversation/application/conversation-catalog-reconciliation';
import {
  createConversationCatalogLoader,
} from '../conversation/application/conversation-catalog-loader';
import {
  buildConversationSettingsSnapshot,
  createConversationSettingsResolution,
} from '../conversation/application/conversation-settings-resolution';
import { createConversationCatalogRevisionGate } from '../conversation/application/conversation-catalog-revision-gate';
import { createConversationSurfaceController } from '../conversation/application/conversation-surface-controller';
import { createConversationStartup } from '../conversation/application/conversation-startup';
import { createBranchComposerSession } from '../conversation/application/branch-composer';
import {
  agentModeFlags,
  normalizeAgentMode,
  normalizeConversationInteractionModes,
  resolveAgentMode,
} from '../conversation/application/agent-mode';
import {
  activeConversationAttemptIds,
  activeMainConversationAttemptId,
  conversationHasActor,
  latestConversationTurn,
  orderedConversationTurns,
} from '../conversation/application/conversation-read-model';
import { createTransientTurnOverlay } from '../conversation/application/transient-turn-overlay';
import { createSwarmPushRuntime } from '../conversation/application/swarm-presentation-overlay';
import {
  createSwarmReconciliationScheduler,
  SWARM_RECONCILIATION_POLICY,
} from '../conversation/application/swarm-reconciliation-scheduler';
import {
  createDemandScopedPresentationTicker,
} from '../conversation/application/demand-scoped-presentation-ticker';
import { createTransientStatusTurn } from '../conversation/application/transient-status-turn';
import {
  createOptimisticTurnPair,
  withOptimisticAssistantPreparation,
} from '../conversation/application/optimistic-user-turn';
import { createHumanGuidancePresentationStore } from '../conversation/application/human-guidance-presentation';
import {
  createClassicConversationRenderers,
} from '../conversation/ui/classic-conversation-renderers';
import {
  createHumanGuidanceActions,
} from '../conversation/ui/human-guidance-actions';
import { updateComposerSendControls } from '../conversation/ui/composer-send-controls';
import {
  openTurnInlineEditor,
  reconcileTurnInlineEditors,
} from '../conversation/ui/turn-inline-editor';
import { presentConversationRateLimit } from '../conversation/presentation/live-phase';
import {
  computeExecutionBatches,
  computeToolBatches,
  presentToolExecutionPanel,
  siblingTitleDiscriminators,
  shouldCollapseToolBatch,
  summarizeToolAttention,
  toolParentCallId,
  toolGroupRoundDisplay,
  toolGroupRoundNumber,
  toolGroupRoundTitle,
  toolExecutionLlmRound,
} from '../conversation/presentation/tool-execution-groups';
import {
  handleToolExecutionDisclosureClick,
} from '../conversation/ui/tool-execution-disclosure';
import {
  EXPLICIT_TOOL_ROUND_DISPLAY_NAMES,
  explicitToolRoundDisplay,
  imageGenerationMode,
  isBrowserToolRound,
  isCodeExecutionToolRound,
  isConversationMetadataToolRound,
  isFetchToolRound,
  isMotionToolRound,
  isProgramToolRound,
  isProjectToolRound,
  isSearchToolRound,
  isSwarmToolRound,
  isToolSearchRound,
  programDisplayValue,
  toolRoundDisplay,
  toolRoundIconKey,
} from '../conversation/presentation/tool-round-presentation';
import {
  TOOL_ROUND_CHEVRON_RIGHT_SVG,
  toolRoundSvg,
} from '../conversation/presentation/tool-round-icons';
import {
  createTurnProvenancePresentation,
} from '../conversation/presentation/turn-provenance';
import {
  agentApiRoundCount,
  latestAgentApiRoundUsage,
  resolveTurnServingRoute,
} from '../conversation/presentation/turn-serving-route';
import {
  createToolResultPresentation,
} from '../conversation/presentation/tool-result-presentation';
import {
  createToolSearchPresentation,
} from '../conversation/presentation/tool-search-presentation';
import {
  createToolImagePresentation,
} from '../conversation/presentation/tool-image-presentation';
import {
  createToolBrowserExecutionPresentation,
} from '../conversation/presentation/tool-browser-execution-presentation';
import {
  createToolCommandExecutionPresentation,
} from '../conversation/presentation/tool-command-execution-presentation';
import {
  createToolApprovalPresentation,
} from '../conversation/presentation/tool-approval-presentation';
import {
  createToolInjectionPresentation,
} from '../conversation/presentation/tool-injection-presentation';
import {
  createToolHumanGuidancePresentation,
} from '../conversation/presentation/tool-human-guidance-presentation';
import {
  createWriteGateRefusalPresentation,
} from '../conversation/presentation/write-gate-refusal';
import {
  conversationDisplayTitle as _conversationDisplayTitle,
  conversationTimestampLabels,
  stripNoTranslateTags,
} from '../conversation/presentation/shell-localization';
import { createPlanDecisionBar } from '../conversation/ui/plan-decision-bar';
import { createSendStartupLease } from '../core/send-startup';
import {
  DOMPurify, ensureHljsLanguage, hljs, loadHtml2Canvas, loadKatex, marked,
} from '../vendor-runtime';
// The retained service registry is infrastructure, not a feature initialized
// in manifest order. Declare it before any eager composition so adding a new
// publisher can never create an import-time temporal-dead-zone failure.
const runtimeScope = Object.create(null);
runtimeScope.t = t;
installMarkdownPolicy(marked);
// Temporary lexical names consumed by retained sections. The typed icon owner
// remains module-private; only `Icon` is injected into declared lazy features.
const Icon = iconHtml;
const turnProvenancePresentation = createTurnProvenancePresentation({
  translate: t,
  iconHtml,
});
const {
  inlineMarkdown: _tpInlineMd,
  renderMcpLoginHintHtml,
  renderTurnProvenanceHtml,
  renderPreferenceLearnedHtml,
} = turnProvenancePresentation;
const writeGateRefusalPresentation = createWriteGateRefusalPresentation({
  translate: t,
  iconHtml,
});
const {
  resolveRefusal: _refusalInfo,
  renderBadgeHtml: _renderGateRefusalBadgeHtml,
  renderNoticeHtml: _renderGateNotice,
} = writeGateRefusalPresentation;
const toolResultPresentation = createToolResultPresentation({
  translate: t,
  writeGateRefusal: writeGateRefusalPresentation,
});
const {
  renderCompactionLabelHtml: renderToolResultCompactionLabelHtml,
  renderWriteResultHtml: renderWriteToolResultHtml,
  renderGenericResultHtml: renderGenericToolResultHtml,
} = toolResultPresentation;
const toolSearchPresentation = createToolSearchPresentation({
  translate: t,
  iconHtml,
});
const { renderSearchHtml: renderToolSearchHtml } = toolSearchPresentation;
const toolImagePresentation = createToolImagePresentation({
  translate: t,
  iconHtml,
});
const { renderImageHtml: renderToolImageHtml } = toolImagePresentation;
const toolBrowserExecutionPresentation = createToolBrowserExecutionPresentation({
  translate: t,
});
const {
  renderBrowserExecutionHtml: renderToolBrowserExecutionHtml,
} = toolBrowserExecutionPresentation;
const toolCommandExecutionPresentation = createToolCommandExecutionPresentation({
  translate: t,
});
const {
  renderRunningCommandHtml: renderRunningToolCommandHtml,
  renderSettledCommandHtml: renderSettledToolCommandHtml,
} = toolCommandExecutionPresentation;
const toolApprovalPresentation = createToolApprovalPresentation({
  translate: t,
});
const { renderApprovalHtml: renderToolApprovalHtml } = toolApprovalPresentation;
const toolInjectionPresentation = createToolInjectionPresentation({
  translate: t,
  renderMarkdown: (source) => renderMarkdown(source),
  iconHtml,
  resolveConversationTitle: (conversationId) => convTitleById(conversationId),
});
const { renderInjectionHtml: renderToolInjectionHtml } =
  toolInjectionPresentation;
const toolHumanGuidancePresentation = createToolHumanGuidancePresentation({
  translate: t,
  renderMarkdown: (source) => renderMarkdown(source),
});
const { renderGuidanceHtml: renderToolHumanGuidanceHtml } =
  toolHumanGuidancePresentation;
const humanGuidancePresentationState =
  createHumanGuidancePresentationStore();
const humanGuidancePresentation = Object.freeze({
  read: (conversationId, guidanceId) =>
    humanGuidancePresentationState.read(conversationId, guidanceId),
  patch(conversationId, guidanceId, patch) {
    const next = humanGuidancePresentationState.patch(
      conversationId,
      guidanceId,
      patch || {},
    );
    runtimeScope.requestAuthoritativeConversationRender?.(
      conversationId,
      { forceScroll: false },
    );
    return next;
  },
  decorate: (conversationId, round) =>
    humanGuidancePresentationState.decorate(conversationId, round),
  clearConversation: (conversationId) =>
    humanGuidancePresentationState.clearConversation(conversationId),
});
const humanGuidanceActions = createHumanGuidanceActions({
  translate: t,
  activeConversation: () => {
    const conversation = typeof getActiveConv === 'function'
      ? getActiveConv()
      : null;
    const conversationId = typeof conversation?.id === 'string'
      ? conversation.id
      : '';
    return conversationId
      ? {
        conversationId,
        autoTranslate: typeof convAutoTranslate === 'function'
          ? Boolean(convAutoTranslate(conversation))
          : false,
      }
      : null;
  },
  translateResponse: (source) =>
    _callTranslateAPI(source, 'English', 'Chinese'),
  submitResponse: ({ conversationId, guidanceId, responseText }) =>
    Api.chat.humanResponse(guidanceId, responseText, conversationId),
  submitLateAnswer: ({ conversationId, turnId, responseText }) => {
    const conversation = typeof getActiveConv === 'function'
      ? getActiveConv()
      : null;
    if (
      !conversation
      || conversation.id !== conversationId
      || typeof runtimeScope.answerHumanGuidanceLate !== 'function'
    ) {
      return Promise.reject(new Error('Conversation is not active.'));
    }
    return runtimeScope.answerHumanGuidanceLate(
      conversation,
      turnId,
      responseText,
    );
  },
  markSubmitted: (conversationId, guidanceId, originalResponse) => {
    humanGuidancePresentation.patch(
      conversationId,
      guidanceId,
      { submittedResponse: originalResponse },
    );
  },
  requestExpiredRender: (conversationId) => {
    runtimeScope.requestAuthoritativeConversationRender?.(
      conversationId,
      { force: true, forceScroll: false },
    );
  },
  renderConversationList: () => {
    if (typeof renderConversationList === 'function') {
      renderConversationList();
    }
  },
  showToast: (message, kind) => {
    if (typeof showToast === 'function') showToast(message, kind);
  },
  log: (level, message) => {
    if (typeof debugLog === 'function') debugLog(message, level);
  },
  schedule: (callback, delayMs) => window.setTimeout(callback, delayMs),
});
function submitHumanGuidanceChoice(element) {
  return humanGuidanceActions.submitChoice(element);
}

function submitHumanGuidanceFreeText(element) {
  return humanGuidanceActions.submitFreeText(element);
}

const imageViewerActions = createImageViewerActionsController({ document });
const retainedCompositionLifecycle = createLifecycleScope();
retainedCompositionLifecycle.add(() => imageViewerActions.destroy());
retainedCompositionLifecycle.add(() => humanGuidanceActions.destroy());
const buildWatchController = createBuildWatchController({
  subscribeBuildId: (listener) => (
    typeof pushOnBuildId === 'function' ? pushOnBuildId(listener) : () => {}
  ),
  loadedBuildId: () => (
    typeof _loadedBuildId === 'function' ? _loadedBuildId() : null
  ),
  isBusy: () => (
    typeof _buildWatchBusy === 'function' ? _buildWatchBusy() : true
  ),
  now: () => Date.now(),
  readReloadGuard: (key) => sessionStorage.getItem(key),
  writeReloadGuard: (key, buildId) => sessionStorage.setItem(key, buildId),
  showPendingNotice: () => {
    if (typeof showToast === 'function') {
      showToast('', t('buildWatch.title'), t('buildWatch.body'), 8000);
    }
  },
  reload: () => {
    if (typeof _reloadPage === 'function') _reloadPage();
  },
  onError: (error) => console.debug(
    '[BuildWatch] lifecycle error:', error?.message || error,
  ),
});
retainedCompositionLifecycle.add(() => buildWatchController.destroy());
retainedCompositionLifecycle.listen(
  window,
  'beforeunload',
  () => retainedCompositionLifecycle.destroy(),
  { once: true },
);

const appDialogController = createDialogServices({
  document,
  schedule: {
    setTimeout: (callback, delayMs) => window.setTimeout(callback, delayMs),
    clearTimeout: (handle) => window.clearTimeout(handle),
    setInterval: (callback, delayMs) => window.setInterval(callback, delayMs),
    clearInterval: (handle) => window.clearInterval(handle),
    requestAnimationFrame: (callback) => window.requestAnimationFrame(callback),
    cancelAnimationFrame: (handle) => window.cancelAnimationFrame(handle),
  },
  copy: {
    confirm: () => t('dialog.confirm'),
    cancel: () => t('dialog.cancel'),
    ok: () => t('dialog.ok'),
  },
  log: console,
});
retainedCompositionLifecycle.add(() => appDialogController.destroy());
const showConfirm = appDialogController.showConfirm;
const showAlert = appDialogController.showAlert;
const showPrompt = appDialogController.showPrompt;
const showChoice = appDialogController.showChoice;

const backendAvailabilitySchedule = Object.freeze({
  now: () => Date.now(),
  setTimeout: (callback, delayMs) => window.setTimeout(callback, delayMs),
  clearTimeout: (handle) => window.clearTimeout(handle),
  setInterval: (callback, delayMs) => window.setInterval(callback, delayMs),
  clearInterval: (handle) => window.clearInterval(handle),
});
const availabilityHealthProbe = createAvailabilityHealthProbeCoordinator({
  request: (timeoutMs) => Api.health.check({
    signal: AbortSignal.timeout(timeoutMs),
  }),
});

// Android native shell bridge: a WebView keeps document.visibilityState
// 'visible' while the app is backgrounded, so the shell's
// tofu:native-visibility flips are the only background signal. Fold them
// into one effective-hidden predicate every budget layer below shares.
const nativeVisibility = createNativeVisibility({
  subscribeNativeVisibility: (listener) => {
    document.addEventListener(NATIVE_BRIDGE_POLICY.visibilityEvent, (event) => {
      listener(
        /** @type {CustomEvent|undefined} */ (event)?.detail?.hidden === true,
      );
    });
  },
  documentHidden: () => document.hidden === true,
  native: /** @type {any} */ (window).TofuNative,
  onError: (error) => console.warn('[native-bridge]', error),
});
// Re-run every visibilitychange consumer on a shell flip: layers that read
// the effective predicate suspend/resume correctly; layers still reading
// document.visibilityState directly observe exactly what they see today.
nativeVisibility.subscribe(() => {
  try {
    document.dispatchEvent(new Event('visibilitychange'));
  } catch (error) {
    console.warn('[native-bridge] visibilitychange relay failed:', error);
  }
});
const backendAvailabilityMonitor = createBackendAvailabilityMonitor({
  document,
  browserEvents: window,
  schedule: backendAvailabilitySchedule,
  log: console,
  offlineIconHtml: () => iconHtml('bot', 18),
  isVisible: () => !nativeVisibility.isEffectivelyHidden(),
  isNetworkOnline: () => navigator.onLine !== false,
  probeHealth: availabilityHealthProbe.probe,
  subscribePushReading: (listener) => (
    typeof pushOnLatency === 'function' ? pushOnLatency(listener) : undefined
  ),
  subscribePushReconnect: (listener) => (
    typeof pushOnReconnect === 'function' ? pushOnReconnect(listener) : undefined
  ),
  nudgePushConnection: () => (
    typeof pushConnect === 'function' ? pushConnect() : undefined
  ),
  probeStuckStreams: (reason) => (
    typeof _probeAllStuckStreamsOnWake === 'function'
      ? _probeAllStuckStreamsOnWake(reason)
      : undefined
  ),
  recoverOfflineConversations: (reason) => (
    typeof _recoverOfflineConversations === 'function'
      ? _recoverOfflineConversations(reason)
      : undefined
  ),
  revalidateOnResume: (reason) => (
    typeof _revalidateOnResume === 'function'
      ? _revalidateOnResume(reason)
      : undefined
  ),
  notifyRecovery: ({ title, description, durationMs }) => (
    typeof showToast === 'function'
      ? showToast('✅', title, description, durationMs)
      : undefined
  ),
  copy: {
    backendOfflineTitle: () => t('conn.backendOfflineTitle'),
    backendOfflineDescription: (retrySeconds) => t(
      'conn.backendOfflineDesc', { n: retrySeconds },
    ),
    networkOfflineTitle: () => t('conn.networkOfflineTitle'),
    networkOfflineDescription: () => t('conn.networkOfflineDesc'),
    offlineElapsed: (duration) => t(
      'conn.backendOfflineElapsed', { t: duration },
    ),
    retryNow: () => t('conn.backendRetryNow'),
    snooze: () => t('conn.backendSnooze'),
    restoredTitle: () => t('conn.backendRestored'),
    restoredDescription: () => t('conn.backendRestoredDesc'),
    backendTitlePrefix: () => t('conn.backendOfflineTitlePrefix'),
    networkTitlePrefix: () => t('conn.networkOfflineTitlePrefix'),
  },
});
// The update dialog is demand-loaded in the utility-panels chunk. Publish one
// narrow lifecycle bridge instead of letting that chunk reach across module
// scope to the typed monitor or duplicate its liveness state.
const backendAvailabilityRestartScope = Object.freeze({
  begin: () => backendAvailabilityMonitor.beginPlannedInterruption(),
  end: (backendReachable) => (
    backendAvailabilityMonitor.endPlannedInterruption(backendReachable)
  ),
});
const storageAvailabilityMonitor = createStorageAvailabilityMonitor({
  document,
  schedule: backendAvailabilitySchedule,
  log: console,
  warningIconHtml: () => iconHtml('alertTriangle', 18),
  isVisible: () => !nativeVisibility.isEffectivelyHidden(),
  probeHealth: availabilityHealthProbe.probe,
  copy: {
    unavailableTitle: () => t('conn.storageUnavailableTitle'),
    unavailableDescription: () => t('conn.storageUnavailableDesc'),
    dismiss: () => t('conn.dismiss'),
  },
});
retainedCompositionLifecycle.add(() => backendAvailabilityMonitor.destroy());
retainedCompositionLifecycle.add(() => storageAvailabilityMonitor.destroy());

/** @param {unknown} source */
function _openImageFullscreen(source) {
  imageViewerActions.openImageFullscreen(String(source ?? ''));
}

/** @param {Element|null} button */
function _downloadGenImage(button) {
  imageViewerActions.downloadGeneratedImage(button);
}

const sendPreparationOverlay = createSendPreparationOverlayController({
  getActiveConversation: () => (
    typeof getActiveConv === 'function' ? getActiveConv() : null
  ),
  findConversation: (conversationId) => (
    typeof conversations === 'undefined'
      ? null
      : conversations.find((item) => item?.id === conversationId)
  ),
  resolveTransientTurns: () => runtimeScope.ConversationTransientTurns,
  translateTranslatingLabel: () => t('sidebar.translating'),
  scrollToLatest: () => {
    const container = document.getElementById('chatContainer');
    if (container) container.scrollTop = container.scrollHeight;
  },
});

/** @param {string} [label] */
function _renderTranslatingBubble(label) {
  sendPreparationOverlay.show(label);
}

/** @param {string} [conversationId] */
function _removeTranslatingBubble(conversationId) {
  sendPreparationOverlay.remove(conversationId);
}

const preferenceActionsController = createPreferenceActionsController({
  resolvePendingPreference: async (pendingId, accept) => {
    await Api.profile.resolvePending(pendingId, accept);
  },
  undoContextChange: async (changeId) => {
    await Api.userContext.undo(changeId);
  },
  translate: (key) => t(key),
  iconHtml: (name, size) => Icon(name, size),
  reportResolveFailure: (error) => {
    console.warn('[resolvePreference] failed', error);
    if (typeof showToast === 'function') {
      showToast('⚠️', 'Error', String(error), 4000);
    }
  },
  reportUndoFailure: (error) => {
    const detail = error && typeof error === 'object' && 'message' in error
      ? error.message : error;
    if (typeof showToast === 'function') {
      showToast('⚠️', t('context.undoFailed'), String(detail), 4000);
    }
  },
});

/** @param {Element|null} button @param {string} pendingId @param {boolean} accept */
async function resolvePreference(button, pendingId, accept) {
  await preferenceActionsController.resolvePreference(
    button, pendingId, Boolean(accept),
  );
}

/** @param {HTMLButtonElement|null} button @param {string} changeId */
async function undoContextChange(button, changeId) {
  await preferenceActionsController.undoContextChange(button, changeId);
}

/** @param {any} conversation */
function convAutoTranslate(conversation) {
  return resolveConversationAutoTranslate(
    conversation,
    typeof autoTranslate === 'undefined' ? undefined : autoTranslate,
  );
}

/** @param {unknown} conversationId */
function convTitleById(conversationId) {
  return queryConversationTitleById(
    conversations,
    conversationId,
    t('toast.untitledConv'),
  );
}

/** @param {unknown} conversationId */
function convFullIdById(conversationId) {
  return queryConversationFullIdById(conversations, conversationId);
}
// Catalog mutation remains page-local metadata. This typed owner orders the
// live array, publishes only a wake hint, and bounds sidebar frame requests.
const reconcileConversationCatalogMetadata = createConversationCatalogReconciler({
  readConversations: () => conversations,
  isConversationBusy: convIsBusy,
  compareConversations: _convSorter,
  publishCatalogInvalidation: (conversationId) => {
    _broadcastToTabs('conv_saved', { convId: conversationId });
  },
  requestSidebarRender: (render) => { requestAnimationFrame(render); },
  renderSidebar: renderConversationList,
  now: Date.now,
});
const IconDot = statusDotIconHtml;
// Temporary short alias consumed by retained presentation helpers. The
// escaping policy itself remains module-private and typed.
const _esc = escapeHtml;
const _welcomePillsHtml = () => welcomePillsHtml(t);
const moduleTranslate = t;
const errorEnvelopePresentation = createErrorEnvelopePresentation({
  translate: t,
  iconHtml,
});
const {
  errorEnvelopeKind,
  errorEnvelopeKindLabel,
  errorEnvelopeMessage,
  fallbackCauseParts,
  fallbackKindLabel,
  renderErrorEnvelope,
} = errorEnvelopePresentation;
const isErrorEnvelope = isTypedErrorEnvelope;
const normalizeErrorEnvelope = normalizeTypedErrorEnvelope;
// Temporary lexical command consumed by retained toolbar/swarm adapters. The
// application owner receives live catalog/runtime/presentation ports without
// publishing another browser-global refresh authority.
const refreshConversationRuntime = createConversationRefresh({
  findConversation: (conversationId) =>
    conversations.find((item) => item.id === conversationId),
  resolveHydrator: () => runtimeScope.ConversationTurnStore,
  reportFailure: (error) => {
    const detail = error && typeof error === 'object' && 'message' in error
      ? error.message : error;
    console.warn('[ConversationSync] refresh failed:', detail);
    const plannedInterruption = (() => {
      try {
        return backendAvailabilityMonitor.snapshot().plannedInterruption === true;
      } catch (_) {
        return false;
      }
    })();
    if (!plannedInterruption && typeof showToast === 'function') {
      showToast(t('conn.reconnecting'), 'error');
    }
  },
});
// Push state is initialized by a later retained section. Defer subscription
// until this ESM module has finished evaluating, then tie it to page lifetime.
const cookieCaptureConsentLifecycle = createLifecycleScope();
cookieCaptureConsentLifecycle.listen(
  window,
  'beforeunload',
  () => cookieCaptureConsentLifecycle.destroy(),
  { once: true },
);
queueMicrotask(() => {
  if (cookieCaptureConsentLifecycle.signal.aborted) return;
  const controller = createCookieCaptureConsentController({
    subscribe: (channel, taskId, handler) =>
      pushSubscribe(channel, taskId, handler),
    unsubscribe: (channel, taskId, handler) =>
      pushUnsubscribe(channel, taskId, handler),
    showToast: (message, kind) => showToast(message, kind),
    translate: (key) => t(key),
  });
  cookieCaptureConsentLifecycle.add(() => controller.destroy());
});
// The saved-Flow picker is part of the startup toolbar, but the Studio and
// Task Mode controller graphs are user-triggered. Keep only this small typed
// presentation seam eager; frontend/src/features/orchestration.ts owns the
// complete Orchestration registry and retained runtime.
import {
  createOrchestrationFlowCatalog,
} from '../features/orchestration/flow-catalog';
import {
  installOrchestrationApiClient,
  resolveOrchestrationApiClient,
} from '../features/orchestration/api-client';
import {
  orchestrationFlowPickerDisplayName,
  orchestrationFlowPickerIcon,
  projectOrchestrationFlowPickerItems,
  reconcileOrchestrationFlowSelection,
  renderOrchestrationFlowCatalogNotice,
  wireOrchestrationFlowPicker,
} from '../features/orchestration/flow-picker';

// Retained sections still use these lexical names while their DOM owners are
// migrated. The policy and asset registries are typed, module-private, and
// composed once; no compatibility name is published to `window`.
const _detectBrand = detectModelBrand;
const _brandSvg = brandIconHtml;
// The one brand-resolution interface for every model surface: explicit
// Creator identity wins, the registered-models catalog covers id-only
// callers, and name-pattern detection remains as the fallback for ids the
// catalog has never seen.
const modelBrandResolver = createModelBrandResolver({
  lookupCreatorId(modelId) {
    const match = Array.isArray(_registeredModels)
      ? _registeredModels.find((model) => model
        && model.model_id === modelId && model.creator_id)
      : null;
    return match ? match.creator_id : '';
  },
});
const _modelBrand = modelBrandResolver.modelBrand;
const modelDisplayNames = createModelDisplayNames({
  lookupModelDisplayName(modelId) {
    const pricing = _modelPricingCache && _modelPricingCache[modelId];
    return pricing && pricing.name;
  },
  lookupProviderDisplayName(providerId) {
    const match = Array.isArray(_registeredModels)
      ? _registeredModels.find((model) => model
        && model.provider_id === providerId
        && model.provider_name)
      : null;
    return match && match.provider_name;
  },
});
const {
  compareModelIds: _compareModelIds,
  compareModelsByDisplayName: _compareModelsByDisplayName,
  modelShortName: _modelShortName,
  providerDisplayName: _providerDisplayName,
  sortModelEntriesByDisplayName: _sortModelEntriesByDisplayName,
  sortModelsByDisplayName: _sortModelsByDisplayName,
  sortedBrandKeys: _sortedBrandKeys,
} = modelDisplayNames;

// Former file-scope exports live here instead of leaking onto `window`.
// The ESM entry exposes only selected functions through TofuModules v3.
runtimeScope.nativeVisibility = nativeVisibility;
runtimeScope.BackendAvailabilityRestartScope = backendAvailabilityRestartScope;
retainedCompositionLifecycle.add(() => {
  if (runtimeScope.BackendAvailabilityRestartScope ===
      backendAvailabilityRestartScope) {
    delete runtimeScope.BackendAvailabilityRestartScope;
  }
});
// Connection-health state stays typed. Retained net-latency presentation gets
// one lexical subscription port; no browser-global health API is published.
const streamHealthSubscribe = (listener) => (
  conversationConnectionHealth.subscribeAggregate(listener)
);
const conversationWakeRecovery = createConversationWakeRecovery({
  readConversations: () => conversations,
  activeAttemptIds: (conversation) => (
    runtimeScope.ConversationTurnRead?.activeAttemptIds?.(conversation) || []
  ),
  wakeConversation: (conversation) => (
    runtimeScope.ConversationTurnStore.wakeConversation(conversation)
  ),
  warn: (error) => {
    console.warn('[ConversationSync] wake recovery failed:', error);
  },
});
const _probeAllStuckStreamsOnWake = () => conversationWakeRecovery.probe();
conversationWakeRecovery.start(window);
const swarmPushRuntime = createSwarmPushRuntime({
  findConversation: (conversationId) => (
    conversations.find((item) => item?.id === conversationId) || null
  ),
  readTurnState: (conversationId) => (
    runtimeScope.ConversationTurnStore
      ?.ensureRuntimeStore?.(conversationId)?.getState?.() || null
  ),
  readOverlay: (conversationId, turnId) => (
    runtimeScope.ConversationTransientTurns?.get?.(
      conversationId, turnId,
    ) || null
  ),
  upsertOverlay: (conversation, turn) => {
    runtimeScope.ConversationTransientTurns?.upsert?.(conversation, turn);
  },
  removeOverlay: (conversation, turnId) => {
    runtimeScope.ConversationTransientTurns?.remove?.(conversation, turnId);
  },
  hydrateConversation: (conversation) => {
    const service = runtimeScope.ConversationTurnStore;
    return service?.hydrateConversation
      ? service.hydrateConversation(conversation) : null;
  },
  attachAutoContinue: (conversationId) => {
    if (conversations.some((item) => item?.id === conversationId)) {
      void refreshConversationRuntime(conversationId).catch(() => {});
    }
  },
  reducePhase: _handleSwarmPhase,
  reduceAgent: _handleSwarmAgent,
  debug: (message, detail) => { console.debug(message, detail); },
  warn: (message, detail) => { console.warn(message, detail); },
  subscribe: (handler) => { pushSubscribe('swarm', '*', handler); },
  unsubscribe: (handler) => { pushUnsubscribe('swarm', '*', handler); },
});
runtimeScope.ConversationSwarmPresentation = swarmPushRuntime.presentation;
window.addEventListener(
  'beforeunload',
  swarmPushRuntime.destroy,
  { once: true },
);
queueMicrotask(swarmPushRuntime.start);
const conversationStartup = createConversationStartup({
  loadConversationCatalog,
  loadFolders,
  migratePinnedToFolder: _migratePinnedToFolder,
  scheduleFolderLoadRetry: _scheduleFolderLoadRetry,
  hasTurnHydrator: () => (
    typeof runtimeScope.ConversationTurnStore?.hydrateConversation === 'function'
  ),
  activeConversationId: () => activeConvId,
  activeConversation: getActiveConv,
  isConversationBusy: convIsBusy,
  showStreamingPresentation: showStreamingUIForConv,
  requestAuthoritativeRender: (conversationId) => {
    runtimeScope.requestAuthoritativeConversationRender(conversationId);
  },
  renderPendingQueue: renderPendingQueueUI,
  warnFolderLoad: (error) => {
    console.warn('[initActiveTasks] folder load failed:', error);
  },
  warnCatalogLoad: (error) => {
    console.warn(
      '[initActiveTasks] startup catalog initialization failed:',
      error,
    );
  },
});
const initActiveTasks = () => conversationStartup.initialize();
const modelCapabilityTaxonomy = createModelCapabilityTaxonomy();
const {
  applyCapabilityTaxonomy,
  getChatExcludedCaps,
  getKnownCapabilities,
  isChatModel,
} = modelCapabilityTaxonomy;
// Temporary retained/lazy compatibility ports. The controller and its state
// stay module-private; the fallback snapshot cannot mutate controller state.
runtimeScope.applyCapabilityTaxonomy = applyCapabilityTaxonomy;
runtimeScope.getChatExcludedCaps = getChatExcludedCaps;
runtimeScope.getKnownCapabilities = getKnownCapabilities;
runtimeScope.isChatModel = isChatModel;
runtimeScope.CHAT_EXCLUDED_CAPS_FALLBACK = [
  ...CHAT_EXCLUDED_CAPS_FALLBACK,
];
const modelGroupPolicy = createModelGroupPolicy({ detectBrand: detectModelBrand });
runtimeScope.modelGroupKey = modelGroupPolicy.modelGroupKey;
runtimeScope.modelGroupLabel = modelGroupPolicy.modelGroupLabel;
runtimeScope.modelGroupBrandNames = modelGroupPolicy.modelGroupBrandNames;
runtimeScope.modelDisplayUnits = modelDisplayUnits;
const recentModelsController = createRecentModelsController({
  resolveStorage: () => resolveBrowserLocalStorage() ?? null,
});
runtimeScope.recentModels = recentModelsController.recentModels;
runtimeScope.pushRecentModel = recentModelsController.pushRecentModel;
runtimeScope._currentUserId = null;
const currentUserIdentity = createCurrentUserIdentityController({
  loadCurrentUser: () => Api.users.me(),
  onOwnerChanged: (ownerId) => { runtimeScope._currentUserId = ownerId; },
  log: (message, level) => {
    if (typeof debugLog === 'function') debugLog(message, level);
  },
});
const initCurrentUserId = () => currentUserIdentity.resolve();
const resetCurrentUserIdForTests = () => currentUserIdentity.reset();
runtimeScope.initCurrentUserId = initCurrentUserId;
runtimeScope.resetCurrentUserIdForTests = resetCurrentUserIdForTests;
const ConvCache = createLazyConversationMetadataCache({
  isCapabilityAvailable: () => resolveBrowserIndexedDb() !== undefined,
  load: async () => {
    const cacheModule = await import('../core/conversation-metadata-cache');
    return cacheModule.createConversationMetadataCache({
      storage: cacheModule.createIndexedDbConversationMetadataCacheStorage(
        resolveBrowserIndexedDb(),
      ),
      resolveOwnerId: initCurrentUserId,
    });
  },
});
retainedCompositionLifecycle.add(() => ConvCache.close());
const _frameIsOurs = (frameOwnerId) => frameBelongsToOwner(
  runtimeScope._currentUserId,
  frameOwnerId,
);
runtimeScope._frameIsOurs = _frameIsOurs;

let katex;
let html2canvas;
let _featureFlags = {};

let markRuntimeReady;
export const runtimeReady = new Promise((resolve) => { markRuntimeReady = resolve; });
function _markScriptsLoaded() { markRuntimeReady(); }
