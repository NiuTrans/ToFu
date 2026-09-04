// @ts-check
/* Generated lazy retained runtime: orchestration-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { t } from '../i18n/index';
import { escapeHtml } from '../html-safety';
import { HTTP_RESULT } from '../core/http-result';
import { orchestrationRegistry } from '../features/orchestration/registry';

const {
  ORCHESTRATION_LAYOUT_BREAKPOINTS,
  ORCHESTRATION_RUNTIME_SECTION_VALIDATORS,
  ORCHESTRATION_RUN_FILTERS,
  _ORCHESTRATION_HTTP_READ_PROJECTORS,
  _ORCHESTRATION_REQUEST_CONTRACTS,
  _normalizeOrchestrationReplayRead,
  _normalizeOrchestrationRuntimeStart,
  _orchestrationActionRead,
  _orchestrationActionReason,
  _orchestrationContractFieldsMatch,
  _orchestrationContractRecord,
  _orchestrationDefinitionActionRead,
  _orchestrationDefinitionEntryMatches,
  _orchestrationDefinitionFields,
  _orchestrationDefinitionListMatches,
  _orchestrationDefinitionVersion,
  _orchestrationDefinitionVersionMatches,
  _orchestrationDefinitionWriteContract,
  _orchestrationDurableListEnvelope,
  _orchestrationDurableRunFields,
  _orchestrationDurableRunMatches,
  _orchestrationHttpFailureReason,
  _orchestrationHttpRead,
  _orchestrationLiveReplayMatches,
  _orchestrationMutationLegacyValue,
  _orchestrationNormalizedHttpRead,
  _orchestrationReplayEventMatches,
  _orchestrationRequestLimitValue,
  _orchestrationRequireArray,
  _orchestrationRequireArraySubset,
  _orchestrationRequireBoolean,
  _orchestrationRequireFieldSpecs,
  _orchestrationRequireMapValues,
  _orchestrationRequireOptional,
  _orchestrationRequirePositiveInteger,
  _orchestrationRequireString,
  _orchestrationRequireStringFields,
  _orchestrationRequireStringVocabulary,
  _orchestrationRequiredResponseFieldsMatch,
  _orchestrationRunStatuses,
  _orchestrationRuntimeSectionNames,
  _orchestrationTraceContract,
  _projectOrchestrationOutcomeText,
  _tmAbortRun,
  _tmAdoptRunSnapshot,
  _tmAfterClose,
  _tmAgo,
  _tmApiClient,
  _tmBindImageFallbacks,
  _tmClearRunSurface,
  _tmContracts,
  _tmControlDef,
  _tmDeleteRun,
  _tmDuration,
  _tmEnsureActions,
  _tmEnsureCommands,
  _tmEnsureContractController,
  _tmEnsureControllerHub,
  _tmEnsureEventController,
  _tmEnsureGraphView,
  _tmEnsureInspectorView,
  _tmEnsureModal,
  _tmEnsureMutationReconciler,
  _tmEnsureNodePresentation,
  _tmEnsurePanelLayout,
  _tmEnsureRunController,
  _tmEnsureRunListController,
  _tmEnsureRunListView,
  _tmEnsureRunView,
  _tmEnsureShell,
  _tmEnsureTimelineView,
  _tmEnsureWorkspace,
  _tmEsc,
  _tmGateCard,
  _tmHumanApprove,
  _tmHumanInput,
  _tmIco,
  _tmIsTerminal,
  _tmLimitPolicy,
  _tmLine,
  _tmNodeAccent,
  _tmNodeGlyph,
  _tmNodeIconHtml,
  _tmNodeLabel,
  _tmNodeSub,
  _tmOpenRun,
  _tmOpenStudio,
  _tmProjectRunTransition,
  _tmReconcileRunMutation,
  _tmRefreshAuthoringContract,
  _tmRefreshRuns,
  _tmRenderEvent,
  _tmRenderGraph,
  _tmRenderInspector,
  _tmRenderRunList,
  _tmRenderTimelineEvent,
  _tmRenderTitle,
  _tmReportTaskFailure,
  _tmRerun,
  _tmResetEventState,
  _tmResyncRun,
  _tmRoleDef,
  _tmRunSession,
  _tmRunStore,
  _tmSelectNode,
  _tmSelectPanel,
  _tmServices,
  _tmSetRunListBusy,
  _tmSetTimelineBusy,
  _tmShowFinal,
  _tmStatusChip,
  _tmStatusLabel,
  _tmStudioClient,
  _tmSyncChip,
  _tmT,
  _tmTaskClient,
  _tmToast,
  _tmTraceDetail,
  _validateDurableRunRuntimeSection,
  _validateEventRuntimeSection,
  _validateMutationRuntimeSection,
  _validateOutcomeRuntimeSection,
  _validateReplayRuntimeSection,
  _validateRequestLimitsRuntimeSection,
  _validateRunRuntimeSection,
  _validateRuntimeStartRuntimeSection,
  _validateTraceRuntimeSection,
  applyOrchestrationInputLimit,
  applyOrchestrationStudioRequestLimits,
  applyOrchestrationTraceActivity,
  bindOrchestrationPointerSession,
  closeTaskMode,
  createOrchestrationActionLock,
  createOrchestrationApiRequestInvoker,
  createOrchestrationBoundedState,
  createOrchestrationComposerRequestClient,
  createOrchestrationCursorPoller,
  createOrchestrationDefinitionMutationCoordinator,
  createOrchestrationDefinitionRequestClient,
  createOrchestrationDefinitionSnapshotPort,
  createOrchestrationDialogFocusManager,
  createOrchestrationDirtyGuard,
  createOrchestrationDisclosureState,
  createOrchestrationDocumentController,
  createOrchestrationDocumentValidationController,
  createOrchestrationDocumentView,
  createOrchestrationDraftState,
  createOrchestrationDurableRunCommand,
  createOrchestrationEditLifecycle,
  createOrchestrationEditorControllerHub,
  createOrchestrationEditorState,
  createOrchestrationEndpointRequestClient,
  createOrchestrationEphemeralAbortController,
  createOrchestrationEphemeralRunController,
  createOrchestrationEventState,
  createOrchestrationGraphActions,
  createOrchestrationGraphSelectionActions,
  createOrchestrationGraphTools,
  createOrchestrationHistoryController,
  createOrchestrationHumanGateController,
  createOrchestrationHumanGateInteraction,
  createOrchestrationHumanGatePresentation,
  createOrchestrationHumanGateView,
  createOrchestrationKeyedActionLock,
  createOrchestrationMutationCommand,
  createOrchestrationMutationRequestClient,
  createOrchestrationNodeCatalogue,
  createOrchestrationPanelFocusReturn,
  createOrchestrationRequestLimits,
  createOrchestrationRequestReader,
  createOrchestrationRovingItemsController,
  createOrchestrationRunController,
  createOrchestrationRunDrawerView,
  createOrchestrationRunEventController,
  createOrchestrationRunFilter,
  createOrchestrationRunPlanCommand,
  createOrchestrationRunPlanView,
  createOrchestrationRunRequestClient,
  createOrchestrationRunSession,
  createOrchestrationRuntimeContractPort,
  createOrchestrationScrollState,
  createOrchestrationSelectionFocus,
  createOrchestrationSessionController,
  createOrchestrationSingleFlight,
  createOrchestrationStudioApi,
  createOrchestrationSurfaceHandoff,
  createOrchestrationTaskRequestClient,
  createOrchestrationTraceState,
  createOrchestrationValidationClient,
  createOrchestrationValidationCoordinator,
  createOrchestrationValidationState,
  createOrchestrationWorkspaceDeleteCommand,
  createOrchestrationWorkspaceLoadCommand,
  createOrchestrationWorkspacePersistence,
  createOrchestrationWorkspacePersistenceContext,
  createOrchestrationWorkspaceRequestClient,
  createOrchestrationWorkspaceSaveCommand,
  createOrchestrationWorkspaceSessionPort,
  createTaskModeActionController,
  createTaskModeCommandController,
  createTaskModeContractController,
  createTaskModeContractSession,
  createTaskModeControllerHub,
  createTaskModeEventController,
  createTaskModeGatePresentation,
  createTaskModeGateView,
  createTaskModeGraphProjection,
  createTaskModeGraphView,
  createTaskModeInspectorPresentation,
  createTaskModeInspectorView,
  createTaskModeListErrorView,
  createTaskModeListFocusController,
  createTaskModeListPaging,
  createTaskModeMutationReconciler,
  createTaskModeNodePresentation,
  createTaskModePanelLayoutController,
  createTaskModePanelSelection,
  createTaskModeRootController,
  createTaskModeRunCommandController,
  createTaskModeRunController,
  createTaskModeRunFinalView,
  createTaskModeRunListController,
  createTaskModeRunListPresentation,
  createTaskModeRunListView,
  createTaskModeRunReader,
  createTaskModeRunReplayController,
  createTaskModeRunStatusPresentation,
  createTaskModeRunStore,
  createTaskModeRunTime,
  createTaskModeRunTitleView,
  createTaskModeRunView,
  createTaskModeServices,
  createTaskModeShell,
  createTaskModeTimelineView,
  createTaskModeTransitionProjector,
  createTaskModeViewRegistry,
  createTaskModeWorkspace,
  focusOrchestrationPanel,
  formatOrchestrationEventLines,
  formatOrchestrationRichCopy,
  inspectOrchestrationWireFormat,
  normalizeOrchestrationAuthoringContractRead,
  normalizeOrchestrationBuiltinRead,
  normalizeOrchestrationComposeResult,
  normalizeOrchestrationDefinitionAdoption,
  normalizeOrchestrationDefinitionDelete,
  normalizeOrchestrationDefinitionListRead,
  normalizeOrchestrationDefinitionRead,
  normalizeOrchestrationDefinitionSave,
  normalizeOrchestrationDefinitionWrite,
  normalizeOrchestrationInspection,
  normalizeOrchestrationLayoutRead,
  normalizeOrchestrationMutation,
  normalizeOrchestrationMutationRead,
  normalizeOrchestrationOutcome,
  normalizeOrchestrationPlanRead,
  normalizeOrchestrationRunPollRead,
  normalizeOrchestrationRunStart,
  normalizeOrchestrationTaskCreate,
  normalizeOrchestrationTaskEventsRead,
  normalizeOrchestrationTaskListRead,
  normalizeOrchestrationTaskRead,
  normalizeOrchestrationValidationRead,
  normalizeTaskReplayPage,
  openTaskMode,
  orchestrationCompactMedia,
  orchestrationCompatibilityContract,
  orchestrationDefinitionSelection,
  orchestrationDefinitionWriteConflict,
  orchestrationDirectContract,
  orchestrationEventCapability,
  orchestrationEventContractSpec,
  orchestrationEventGateEffect,
  orchestrationEventPreviewLimit,
  orchestrationEventShouldReduce,
  orchestrationEventShouldTimeline,
  orchestrationEventStringCapability,
  orchestrationExecutionOptionLabel,
  orchestrationFitMinScale,
  orchestrationFlowPickerDisplayName,
  orchestrationFlowPickerIcon,
  orchestrationHttpReadProjector,
  orchestrationInspectionMatchesContract,
  orchestrationIssueMessages,
  orchestrationMutationMessage,
  orchestrationNodeTraceSnapshot,
  orchestrationOutcomeMessage,
  orchestrationPublishedContract,
  orchestrationRequestContract,
  orchestrationRequestFailureKey,
  orchestrationRequestFailureMessage,
  orchestrationRequestLimitPolicy,
  orchestrationRequestMaxDepth,
  orchestrationRequestMaxItems,
  orchestrationRequestMaxLength,
  orchestrationRequestRetainedItems,
  orchestrationResultData,
  orchestrationResultError,
  orchestrationResultOk,
  orchestrationRunContract,
  orchestrationRunHasStatus,
  orchestrationRunIsTerminal,
  orchestrationRunPresentation,
  orchestrationRunStatus,
  orchestrationRuntimeContractPort,
  orchestrationScrollScope,
  orchestrationSheetMedia,
  orchestrationSheetMediaQuery,
  orchestrationShortLandscapeMedia,
  orchestrationShortLandscapeMediaQuery,
  orchestrationSummaryNodeParam,
  orchestrationTraceHistoryLimit,
  orchestrationWireContractSpec,
  orchestrationWireFormat,
  projectOrchestrationActionState,
  projectOrchestrationControlSummary,
  projectOrchestrationDefinitionAdoption,
  projectOrchestrationDocumentStatus,
  projectOrchestrationDurableRunSnapshot,
  projectOrchestrationDurableStartOutcome,
  projectOrchestrationEventPresentation,
  projectOrchestrationEventPreview,
  projectOrchestrationFinalResult,
  projectOrchestrationFlowCatalogNotice,
  projectOrchestrationFlowPickerItems,
  projectOrchestrationHttpRead,
  projectOrchestrationInspection,
  projectOrchestrationRequestFailure,
  projectOrchestrationRoleExecutionSummary,
  projectOrchestrationRuntimeContracts,
  projectOrchestrationSavedWorkflowItems,
  projectOrchestrationSubflowSummary,
  projectOrchestrationTraceActivity,
  projectOrchestrationTraceAttemptDelta,
  projectOrchestrationTraceAttemptDeltaPresentation,
  projectOrchestrationTraceAttempts,
  projectOrchestrationTraceSections,
  projectOrchestrationTraceStatus,
  projectOrchestrationTraceStatusPresentation,
  projectOrchestrationTraceText,
  reportOrchestrationDiagnostic,
  recordOrchestrationTraceAttempt,
  reconcileOrchestrationFlowSelection,
  reduceOrchestrationEvent,
  reduceOrchestrationTraceEvent,
  registerOrchestrationHttpReadProjectors,
  resetOrchestrationEventState,
  renderOrchestrationFlowCatalogNotice,
  setOrchestrationPanelState,
  taskModeCompactMedia,
  taskModeShellMarkup,
  validateOrchestrationDurableListEnvelope,
  wireOrchestrationFlowPicker,
} = orchestrationRegistry;

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('orchestration-presenters runtime dependency is unavailable: Api');
const BASE_PATH = runtimeScope.BASE_PATH;
if (BASE_PATH === undefined) throw new Error('orchestration-presenters runtime dependency is unavailable: BASE_PATH');
const _featureFlags = runtimeScope._featureFlags;
if (!_featureFlags || typeof _featureFlags !== 'object') throw new Error('orchestration-presenters runtime dependency is unavailable: _featureFlags');
const _agentInteractionChangeBlocked = runtimeScope._agentInteractionChangeBlocked;
if (typeof _agentInteractionChangeBlocked !== 'function') throw new Error('orchestration-presenters runtime dependency is unavailable: _agentInteractionChangeBlocked');
const _orchestrationFlowCatalog = runtimeScope._orchestrationFlowCatalog;
if (!_orchestrationFlowCatalog || typeof _orchestrationFlowCatalog !== 'object') throw new Error('orchestration-presenters runtime dependency is unavailable: _orchestrationFlowCatalog');
const setActiveFlow = runtimeScope.setActiveFlow;
if (typeof setActiveFlow !== 'function') throw new Error('orchestration-presenters runtime dependency is unavailable: setActiveFlow');
const showChoice = runtimeScope.showChoice;
if (typeof showChoice !== 'function') throw new Error('orchestration-presenters runtime dependency is unavailable: showChoice');
const showConfirm = runtimeScope.showConfirm;
if (typeof showConfirm !== 'function') throw new Error('orchestration-presenters runtime dependency is unavailable: showConfirm');
const showToast = runtimeScope.showToast;
if (typeof showToast !== 'function') throw new Error('orchestration-presenters runtime dependency is unavailable: showToast');
/* ===== migrated source: orchestration-catalog.js ===== */
/* ═══════════════════════════════════
   orchestration-catalog.js — role/control/glyph/icon catalog
   Extracted from orchestration.js (2026-07). Pure data + icon-URL
   helpers, read at RUNTIME by both orchestration.js and task-mode.js
   (typeof-guarded). Plain window-scope concatenation — no exports.
   MUST load before the other orchestration-*.js consumers and task-mode.js
   in the migrated lazy-module graph. */

// ── Catalogue: role agents (tofu mascots) ──────────────────────────
// `icon` is a file under static/icons/. This catalogue is presentation-only;
// behavioral defaults (tier/isolation/emits) come from authoring-contract.
var _ORCH_ROLES = [
  { role: 'planner',     label: 'Planner',     icon: 'tofu-planner.svg',
    blurb: 'Rewrites the ask into a structured brief + checklist.' },
  { role: 'worker',      label: 'Worker',      icon: 'tofu-worker.svg',
    blurb: 'Executes the plan with full tools. Stateful across loops.' },
  { role: 'critic',      label: 'Critic',      icon: 'tofu-critic.svg',
    blurb: 'Reviews work against the checklist. Emits a verdict.' },
  { role: 'researcher',  label: 'Researcher',  icon: 'tofu-researcher',
    blurb: 'Gathers + verifies info from web sources.' },
  { role: 'coder',       label: 'Coder',       icon: 'tofu-coder',
    blurb: 'Reads / writes / edits code across files.' },
  { role: 'analyst',     label: 'Analyst',     icon: 'tofu-analyst',
    blurb: 'Quantitative analysis of on-disk data.' },
  { role: 'reviewer',    label: 'Reviewer',    icon: 'tofu-critic.svg',
    blurb: 'Fresh second-opinion read. Outputs a punch list.' },
  { role: 'writer',      label: 'Writer',      icon: 'tofu-writer',
    blurb: 'Long-form prose from raw inputs.' },
  { role: 'browser',     label: 'Browser',     icon: 'tofu-browser',
    blurb: 'Interacts with live browser tabs.' },
  { role: 'synthesizer', label: 'Synthesizer', icon: 'tofu-synthesizer',
    blurb: 'Merges many agent outputs into one converged result.' },
  { role: 'virtual_user', label: 'Virtual User', icon: 'tofu-general',
    blurb: 'Stands in for the human: auto-replies to keep a task going until done. Speaks as User.' },
  { role: 'router',      label: 'Router',      icon: 'tofu-router',
    blurb: 'Classifies each item and routes it to a branch.' },
  { role: 'general',     label: 'General',     icon: 'tofu-general',
    blurb: 'Versatile fallback when no specialist fits.' },
];

// ── Catalogue: control / structure nodes ───────────────────────────
// These carry the topology semantics. `kind` is the node type the
// backend engine will switch on. `single` = at most one per canvas.
var _ORCH_CONTROLS = [
  { kind: 'start',    label: 'Start',     glyph: 'play',    single: true,
    accent: '#10b981', blurb: 'Entry point. The user request flows in here.' },
  { kind: 'loop',     label: 'Loop',      glyph: 'loop',    single: false,
    accent: '#6e56cf', blurb: 'Repeat the wrapped step until a stop condition holds.' },
  { kind: 'parallel', label: 'Fan-out',   glyph: 'fanout',  single: false,
    accent: '#3b82f6', blurb: 'Run downstream agents in parallel, one per item.' },
  { kind: 'barrier',  label: 'Join',      glyph: 'join',    single: false,
    accent: '#14b8a6', blurb: 'Wait for all parallel branches, then continue.' },
  { kind: 'branch',   label: 'Route',     glyph: 'branch',  single: false,
    accent: '#f59e0b', blurb: 'Send the flow down a path chosen by a classifier.' },
  { kind: 'artifact', label: 'Deliverable', glyph: 'artifact', single: false,
    accent: '#ec4899', blurb: 'An expected intermediate output (file / report). '
      + 'Wire it between agents to make a deliverable the contract between them.' },
  { kind: 'human',    label: 'Human',     glyph: 'human',   single: false,
    accent: '#0ea5e9', blurb: 'A human-in-the-loop gate: pause for approval, '
      + 'collect an answer, or notify the user mid-flow.' },
  { kind: 'stop',     label: 'Stop',      glyph: 'stop',    single: true,
    accent: '#ef4444', blurb: 'Terminal. The converged result returns to chat.' },
];

// Inline SVG glyphs for control nodes (abstract, theme-colored).
var _ORCH_GLYPHS = {
  play:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  loop:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2l4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/></svg>',
  fanout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="12" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="18" cy="12" r="2"/><circle cx="18" cy="19" r="2"/><path d="M8 12h2M11 11l5-5M11 13l5 5"/></svg>',
  join:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="12" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="12" r="2"/><path d="M8 5l5 6M8 12h2M8 19l5-6"/></svg>',
  branch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="12" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="18" cy="18" r="2"/><path d="M8 12h3M11 11l5-4M11 13l5 4"/></svg>',
  stop:   '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  artifact: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/></svg>',
  human:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="7" r="4"/><path d="M5.5 21a6.5 6.5 0 0 1 13 0"/></svg>',
  group:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3" stroke-dasharray="4 3"/><rect x="7" y="7" width="4" height="4" rx="1"/><rect x="13" y="13" width="4" height="4" rx="1"/><path d="M11 9h2a2 2 0 0 1 2 2v2"/></svg>',
};

// ── Inline UI icons (SVG-only; NO emoji) ────────────────────────────
// House rule for the orchestration surface: every icon is an inline SVG
// glyph, never an emoji — even for abstract concepts. Each entry is a
// self-sized <svg class="orch-ico"> (1em, currentColor) safe to splice
// into button labels and run-log lines (which use innerHTML).
var _orchSvg = function (inner, big) {
  return '<svg class="orch-ico' + (big ? ' orch-ico-lg' : '') + '" viewBox="0 0 24 24" '
    + 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    + 'stroke-linejoin="round" aria-hidden="true">' + inner + '</svg>';
};
var _ORCH_ICONS = {
  plus:    _orchSvg('<path d="M12 5v14M5 12h14"/>'),
  minus:   _orchSvg('<path d="M5 12h14"/>'),
  fit:     _orchSvg('<path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"/><path d="M8 8h8v8H8z"/>'),
  gear:    _orchSvg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'),
  layout:  _orchSvg('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>'),
  star:    '<svg class="orch-ico" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2l2.9 6.3 6.9.7-5.1 4.6 1.4 6.8L12 17.8 5.9 20.4l1.4-6.8L2.2 9l6.9-.7z"/></svg>',
  loop:    _orchSvg('<path d="M17 2l4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>'),
  auto:    _orchSvg('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/>'),
  fanout:  _orchSvg('<circle cx="6" cy="12" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="18" cy="12" r="2"/><circle cx="18" cy="19" r="2"/><path d="M8 12h2M11 11l5-5M11 13l5 5"/>'),
  shield:  _orchSvg('<path d="M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>'),
  folder:  _orchSvg('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  wand:    _orchSvg('<path d="M15 4V2M15 10V8M11 6H9M21 6h-2M18.5 3.5l-1 1M11.5 3.5l1 1M5 21l11-11"/>'),
  save:    _orchSvg('<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/>'),
  puzzle:  _orchSvg('<path d="M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0 0 0 .474-1.68l-1.683-1.682a2.414 2.414 0 0 1 0-3.414L4.39 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0 1-.474-1.68l1.683-1.682a2.414 2.414 0 0 1 3.414 0z"/>', true),
  speak:   _orchSvg('<path d="M21 11.5a8.38 8.38 0 0 1-9 8.3 8.5 8.5 0 0 1-3.8-.9L3 20l1.1-3.3A8.38 8.38 0 0 1 12 3.5a8.5 8.5 0 0 1 9 8z"/>'),
  eye:     _orchSvg('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>'),
  rocket:  _orchSvg('<path d="M5 15c-1 1-1.5 4-1.5 4s3-.5 4-1.5"/><path d="M9 11a12 12 0 0 1 8-8c2 0 3 1 3 3a12 12 0 0 1-8 8z"/><path d="M9 11l-3 1 3 5 1-3"/><circle cx="14.5" cy="9.5" r="1.5"/>'),
  bot:     _orchSvg('<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V5M9 4h6"/><circle cx="9" cy="14" r="1"/><circle cx="15" cy="14" r="1"/>'),
  check:   '<svg class="orch-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>',
  reject:  _orchSvg('<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>'),
  warn:    _orchSvg('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>'),
  compass: _orchSvg('<circle cx="12" cy="12" r="9"/><path d="M16 8l-2 6-6 2 2-6z"/>'),
  package: _orchSvg('<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/>'),
  person:  _orchSvg('<circle cx="12" cy="8" r="4"/><path d="M5 21a7 7 0 0 1 14 0"/>'),
  flag:    _orchSvg('<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>'),
  download: _orchSvg('<path d="M12 3v12M7 10l5 5 5-5"/><path d="M5 21h14"/>'),
  refresh: _orchSvg('<path d="M20 6v5h-5"/><path d="M4 18v-5h5"/><path d="M6.1 9a7 7 0 0 1 11.7-2.6L20 9M4 15l2.2 2.6A7 7 0 0 0 17.9 15"/>'),
  undo:    _orchSvg('<path d="M9 7l-4 4 4 4"/><path d="M5 11h8a6 6 0 0 1 6 6"/>'),
  redo:    _orchSvg('<path d="M15 7l4 4-4 4"/><path d="M19 11h-8a6 6 0 0 0-6 6"/>'),
  panels:  _orchSvg('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 4v16M16 4v16"/>'),
  chevronDown: _orchSvg('<path d="M6 9l6 6 6-6"/>'),
  stop:    '<svg class="orch-ico" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
};


function _orchIconBase() {
  return (typeof BASE_PATH !== 'undefined' ? BASE_PATH : '') + '/static/icons';
}

// Resolve a role icon to a full URL. An `icon` carrying an explicit
// extension (e.g. 'tofu-worker.svg') is used as-is; otherwise '.png' is
// appended. Lets crisp SVGs and cleaned PNGs coexist in _ORCH_ROLES.
// Cache-bust token for role icons. Bump when icon art is regenerated so
// browsers re-fetch instead of serving the stale (max-age=86400) bytes.
var _ORCH_ICON_VER = '20260622a';

function _orchIconSrc(icon) {
  var name = icon || 'tofu-general';
  var file = /\.\w+$/.test(name) ? name : name + '.png';
  return _orchIconBase() + '/' + file + '?v=' + _ORCH_ICON_VER;
}

/* ===== migrated source: orchestration-authoring-metadata.generated.js ===== */
/* AUTO-GENERATED by scripts/gen_orchestration_authoring_metadata.py.
 * Canonical sources: authoring_contract.py,
 * authoring_contract_registry.py, and request_limit_contract.py.
 * DO NOT EDIT BY HAND.
 */
var ORCHESTRATION_AUTHORING_OBJECT_SECTIONS = Object.freeze([
  "roles",
  "controlSchemas",
  "personas",
  "defaultEmits",
  "executionOptions",
  "nodeDefaults",
  "nodeRuntimeDefaults",
  "eventContract",
  "runContract",
  "outcomeContract",
  "traceContract",
  "mutationContract",
  "replayContract",
  "inspectionContract",
  "definitionListContract",
  "definitionEntryContract",
  "runtimeStartContract",
  "fieldValueContract",
  "durableRunContract",
  "definitionWriteContract",
  "requestLimits",
  "ioContract"
]);
var ORCHESTRATION_RUNTIME_CONTRACT_SECTIONS = Object.freeze([
  "requestLimits",
  "nodeRuntimeDefaults",
  "eventContract",
  "runContract",
  "outcomeContract",
  "traceContract",
  "mutationContract",
  "replayContract",
  "runtimeStartContract",
  "durableRunContract"
]);
var ORCHESTRATION_AUTHORING_WIRE_SECTIONS = Object.freeze({
  "eventContract": "events",
  "runContract": "run-status",
  "outcomeContract": "outcome",
  "traceContract": "trace",
  "mutationContract": "mutation",
  "replayContract": "task-replay",
  "inspectionContract": "inspection",
  "definitionListContract": "definition-list",
  "definitionEntryContract": "definition-entry",
  "runtimeStartContract": "runtime-start",
  "fieldValueContract": "field-value",
  "durableRunContract": "durable-run",
  "definitionWriteContract": "definition-write"
});
var ORCHESTRATION_REQUEST_LIMIT_FIELDS = Object.freeze({
  "definitionName": Object.freeze(["maxLength"]),
  "definitionNodes": Object.freeze(["maxItems"]),
  "subflowDepth": Object.freeze(["maxDepth"]),
  "composeRequirement": Object.freeze(["maxLength"]),
  "composeHistory": Object.freeze(["retainedItems", "messageMaxLength"]),
  "runInput": Object.freeze(["maxLength"]),
  "humanInput": Object.freeze(["maxLength"])
});
var ORCHESTRATION_AUTHORING_VALIDATION_METADATA = (function () {
  var metadata = {
  "rollingOptionalFields": {
    "runContract": [
      "categories"
    ],
    "outcomeContract": [
      "incompleteStopReasons"
    ],
    "mutationContract": [
      "transportFailureReason",
      "clientRetryableReasons",
      "payloadFields"
    ],
    "replayContract": [
      "caughtUpField"
    ],
    "durableRunContract": [
      "listEnvelope"
    ],
    "fieldValueContract": [
      "failureCodes"
    ],
    "ioContract": [
      "failureCodes"
    ],
    "definitionWriteContract": [
      "conflictFields"
    ]
  },
  "runtimeSections": {
    "requestLimits": {
      "requiredStringFields": [],
      "requiredStringArrayFields": [],
      "requiredArrayFields": [],
      "requiredObjectFields": [
        "definitionName",
        "definitionNodes",
        "subflowDepth",
        "composeRequirement",
        "composeHistory",
        "runInput",
        "humanInput"
      ],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [],
      "requiredNonNegativeIntegerFields": []
    },
    "nodeRuntimeDefaults": {
      "requiredStringFields": [],
      "requiredStringArrayFields": [],
      "requiredArrayFields": [],
      "requiredObjectFields": [
        "role",
        "controls",
        "subflow"
      ],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [],
      "requiredNonNegativeIntegerFields": [],
      "role": {
        "requiredStringFields": [
          "tier",
          "isolation"
        ],
        "requiredStringArrayFields": [],
        "requiredArrayFields": [],
        "requiredObjectFields": [],
        "requiredBooleanFields": [],
        "requiredPositiveIntegerFields": [],
        "requiredNonNegativeIntegerFields": []
      },
      "subflow": {
        "requiredStringFields": [
          "scope"
        ],
        "requiredStringArrayFields": [],
        "requiredArrayFields": [],
        "requiredObjectFields": [],
        "requiredBooleanFields": [],
        "requiredPositiveIntegerFields": [],
        "requiredNonNegativeIntegerFields": []
      },
      "controls": {
        "loop": {
          "requiredStringFields": [
            "stop_condition"
          ],
          "requiredStringArrayFields": [],
          "requiredArrayFields": [],
          "requiredObjectFields": [],
          "requiredBooleanFields": [],
          "requiredPositiveIntegerFields": [
            "max_iterations"
          ],
          "requiredNonNegativeIntegerFields": []
        },
        "human": {
          "requiredStringFields": [
            "mode"
          ],
          "requiredStringArrayFields": [],
          "requiredArrayFields": [],
          "requiredObjectFields": [],
          "requiredBooleanFields": [],
          "requiredPositiveIntegerFields": [
            "timeout_sec"
          ],
          "requiredNonNegativeIntegerFields": []
        }
      },
      "roleExecutionAxes": {
        "tier": "tiers",
        "isolation": "isolation"
      },
      "subflowExecutionAxes": {
        "scope": "scopes"
      }
    },
    "eventContract": {
      "requiredStringFields": [],
      "requiredStringArrayFields": [],
      "requiredArrayFields": [],
      "requiredObjectFields": [
        "previewLimits",
        "types"
      ],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [],
      "requiredNonNegativeIntegerFields": [],
      "eventTypeBooleanFields": [
        "durable",
        "reduce",
        "timeline"
      ],
      "eventTypeOptionalStringFields": [
        "runStatus",
        "gateEffect"
      ]
    },
    "runContract": {
      "requiredStringFields": [
        "initial"
      ],
      "requiredStringArrayFields": [
        "statuses",
        "terminal"
      ],
      "requiredArrayFields": [],
      "requiredObjectFields": [],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [],
      "requiredNonNegativeIntegerFields": []
    },
    "outcomeContract": {
      "requiredStringFields": [],
      "requiredStringArrayFields": [
        "categories",
        "engineStatuses",
        "lifecycleStatuses",
        "chatStatuses",
        "finishReasons"
      ],
      "requiredArrayFields": [],
      "requiredObjectFields": [
        "displayLimits"
      ],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [],
      "requiredNonNegativeIntegerFields": []
    },
    "traceContract": {
      "requiredStringFields": [],
      "requiredStringArrayFields": [],
      "requiredArrayFields": [],
      "requiredObjectFields": [
        "statusMap",
        "activityFields",
        "textLimits",
        "truncationFlags"
      ],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [
        "historyLimit"
      ],
      "requiredNonNegativeIntegerFields": []
    },
    "mutationContract": {
      "requiredStringFields": [
        "reconcileField",
        "targetExistsField",
        "resourceTerminalField"
      ],
      "requiredStringArrayFields": [
        "actions",
        "reasons",
        "retryableReasons"
      ],
      "requiredArrayFields": [],
      "requiredObjectFields": [
        "httpStatusByReason"
      ],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [],
      "requiredNonNegativeIntegerFields": []
    },
    "replayContract": {
      "requiredStringFields": [
        "notFoundReason",
        "statusField",
        "nextCursorField",
        "terminalField",
        "eventsField",
        "eventTypeField",
        "eventSequenceField",
        "unknownEventTypes"
      ],
      "requiredStringArrayFields": [
        "pageFields",
        "eventRequiredFields",
        "terminalEventTypes"
      ],
      "requiredArrayFields": [],
      "requiredObjectFields": [
        "httpStatuses",
        "cursor",
        "terminalSnapshot"
      ],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [],
      "requiredNonNegativeIntegerFields": [],
      "cursor": {
        "requiredStringFields": [
          "queryField",
          "field",
          "requestedField",
          "nextField",
          "resetField",
          "unit"
        ],
        "requiredStringArrayFields": [],
        "requiredArrayFields": [],
        "requiredObjectFields": [],
        "requiredBooleanFields": [
          "producerOwned",
          "futureCursorReset"
        ],
        "requiredPositiveIntegerFields": [],
        "requiredNonNegativeIntegerFields": [
          "minimum",
          "default"
        ]
      },
      "terminalSnapshot": {
        "requiredStringFields": [
          "field"
        ],
        "requiredStringArrayFields": [],
        "requiredArrayFields": [],
        "requiredObjectFields": [
          "when"
        ],
        "requiredBooleanFields": [
          "optional"
        ],
        "requiredPositiveIntegerFields": [],
        "requiredNonNegativeIntegerFields": []
      },
      "terminalSnapshotWhen": {
        "requiredStringFields": [
          "field"
        ],
        "requiredStringArrayFields": [],
        "requiredArrayFields": [],
        "requiredObjectFields": [],
        "requiredBooleanFields": [
          "equals"
        ],
        "requiredPositiveIntegerFields": [],
        "requiredNonNegativeIntegerFields": []
      },
      "staticPageFields": [
        "format",
        "ok"
      ]
    },
    "runtimeStartContract": {
      "requiredStringFields": [
        "idField",
        "kindField"
      ],
      "requiredStringArrayFields": [
        "kinds"
      ],
      "requiredArrayFields": [],
      "requiredObjectFields": [
        "successStatuses"
      ],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [],
      "requiredNonNegativeIntegerFields": []
    },
    "durableRunContract": {
      "requiredStringFields": [
        "idField",
        "statusField",
        "terminalField",
        "outcomeField"
      ],
      "requiredStringArrayFields": [
        "listFields",
        "readFields",
        "optionalFields"
      ],
      "requiredArrayFields": [],
      "requiredObjectFields": [],
      "requiredBooleanFields": [],
      "requiredPositiveIntegerFields": [],
      "requiredNonNegativeIntegerFields": [],
      "listEnvelope": {
        "requiredStringFields": [
          "itemsField",
          "pageField",
          "limitField",
          "hasMoreField",
          "nextLimitField"
        ],
        "requiredStringArrayFields": [
          "pageFields"
        ],
        "requiredArrayFields": [],
        "requiredObjectFields": [],
        "requiredBooleanFields": [],
        "requiredPositiveIntegerFields": [
          "defaultLimit",
          "pageStep",
          "maxLimit"
        ],
        "requiredNonNegativeIntegerFields": []
      }
    }
  },
  "fieldSpecRegistrySections": [
    "roles",
    "controlSchemas"
  ],
  "fieldSpec": {
    "requiredStringFields": [
      "key",
      "kind",
      "label"
    ],
    "positiveIntegerFields": [
      "runtimeMax",
      "maxLength",
      "maxItems",
      "maxItemLength"
    ],
    "arrayFields": [
      "options"
    ],
    "objectFields": [
      "visibleWhen"
    ],
    "optionRequiredStringFields": [
      "value",
      "label"
    ],
    "optionBooleanFields": [
      "disabled"
    ]
  },
  "fieldValueContract": {
    "optionalEmpty": "omit",
    "failureCodes": {
      "unsupportedContract": "field.contract.unsupported",
      "invalidNumber": "field.type.integer",
      "invalidBoolean": "field.type.boolean",
      "maxLength": "field.max_length",
      "maxItems": "field.max_items",
      "maxItemLength": "field.max_item_length"
    },
    "kinds": [
      "text",
      "textarea",
      "select",
      "list",
      "int",
      "bool"
    ],
    "kindRequiredStringFields": [
      "wire"
    ]
  },
  "personas": {
    "requiredStringFields": [
      "prompt",
      "whenToUse",
      "tier"
    ]
  },
  "executionOptions": {
    "arrayFields": [
      "tiers",
      "isolation",
      "emits",
      "scopes"
    ]
  },
  "nodeDefaults": {
    "objectFields": [
      "roles",
      "genericRole",
      "controls",
      "subflow",
      "blankSubflow"
    ],
    "blankSubflowArrayFields": [
      "nodes",
      "edges"
    ],
    "roleExecutionAxes": {
      "tier": "tiers",
      "isolation": "isolation"
    },
    "subflowExecutionAxes": {
      "scope": "scopes"
    }
  },
  "ioContract": {
    "defaultOutputStringFields": [
      "name",
      "type"
    ],
    "failureCodes": {
      "maxPorts": "io.side.max_ports",
      "missingPort": "io.port.missing",
      "missingPortName": "io.port.name.required",
      "duplicatePortName": "io.port.name.duplicate",
      "missingPreset": "io.preset.missing"
    }
  },
  "definitionWriteContract": {
    "requiredStringFields": [
      "versionField",
      "versionResponseHeader",
      "preconditionHeader",
      "tokenSyntax",
      "conflictReason"
    ]
  }
};
  function deepFreeze(value) {
    if (!value || typeof value !== 'object'
        || Object.isFrozen(value)) return value;
    Object.keys(value).forEach(function (key) {
      deepFreeze(value[key]);
    });
    return Object.freeze(value);
  }
  return deepFreeze(metadata);
})();

/* ===== migrated source: orchestration-popup-menu.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-popup-menu.js — shared Studio popup-menu behavior

   Owns visibility/ARIA synchronization, trigger focus restoration and the
   Arrow/Home/End/Escape keyboard model for both template and stored-flow
   menus. Dynamic stored rows are discovered at interaction time.

   MUST load before orchestration-shell.js and orchestration-workspace.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationPopupMenuController(options) {
  options = options || {};
  var doc = options.document || document;
  var registered = [];

  function _element(id) {
    return id ? doc.getElementById(id) : null;
  }

  function _items(menu) {
    if (!menu) return [];
    return Array.prototype.filter.call(
      menu.querySelectorAll('[role="menuitem"]:not([disabled])'),
      function (item) {
        return item.style.display !== 'none'
          && item.getAttribute('aria-hidden') !== 'true';
      }
    );
  }

  function syncItems(menuId, preferred) {
    var menu = _element(menuId);
    var items = _items(menu);
    var active = doc.activeElement;
    var current = items.indexOf(preferred) >= 0 ? preferred
      : items.indexOf(active) >= 0 ? active
        : items.filter(function (item) { return item.tabIndex === 0; })[0]
          || items[0] || null;
    if (menu) {
      Array.prototype.forEach.call(
        menu.querySelectorAll('[role="menuitem"]'), function (item) {
          item.tabIndex = item === current ? 0 : -1;
        }
      );
    }
    return current;
  }

  function isOpen(menuId) {
    var menu = _element(menuId);
    return !!menu && menu.style.display !== 'none';
  }

  function setOpen(menuId, triggerId, open, opts) {
    opts = opts || {};
    var menu = _element(menuId);
    var trigger = _element(triggerId);
    var active = doc.activeElement;
    if (menu) menu.style.display = open ? 'block' : 'none';
    if (open) syncItems(menuId);
    if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (!open && opts.restoreFocus !== false && menu && trigger
        && (opts.restoreFocus === true || menu.contains(active))
        && typeof trigger.focus === 'function') {
      trigger.focus();
    }
    return !!open;
  }

  function focusEdge(menuId, last) {
    var items = _items(_element(menuId));
    var item = items[last ? items.length - 1 : 0];
    syncItems(menuId, item);
    if (item && typeof item.focus === 'function') item.focus();
    return item || null;
  }

  function _remember(binding) {
    var exists = registered.some(function (item) {
      return item.menuId === binding.menuId;
    });
    if (!exists) registered.push(binding);
  }

  function _close(bindings, opts) {
    var closed = false;
    (bindings || []).forEach(function (binding) {
      if (!isOpen(binding.menuId)) return;
      closed = true;
      setOpen(binding.menuId, binding.triggerId, false, opts);
    });
    return closed;
  }

  function closeAll(opts) {
    return _close(registered, opts);
  }

  function _requestOpen(binding, last) {
    var pending = isOpen(binding.menuId)
      ? true : (typeof binding.open === 'function' ? binding.open() : false);
    return Promise.resolve(pending).then(function () {
      if (!isOpen(binding.menuId)) return null;
      return focusEdge(binding.menuId, last);
    });
  }

  function _triggerKey(event, binding) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      _requestOpen(binding, event.key === 'ArrowUp');
      return;
    }
    if (event.key === 'Escape' && isOpen(binding.menuId)) {
      event.preventDefault();
      event.stopPropagation();
      setOpen(binding.menuId, binding.triggerId, false);
    }
  }

  function _menuKey(event, binding) {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      setOpen(binding.menuId, binding.triggerId, false);
      return;
    }
    if (event.key === 'Tab') {
      setOpen(binding.menuId, binding.triggerId, false);
      return;
    }
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].indexOf(event.key) === -1) {
      return;
    }
    var menu = _element(binding.menuId);
    var items = _items(menu);
    if (!items.length) return;
    event.preventDefault();
    var current = items.indexOf(doc.activeElement);
    var index;
    if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = items.length - 1;
    else if (event.key === 'ArrowDown') index = (current + 1) % items.length;
    else index = (current <= 0 ? items.length : current) - 1;
    syncItems(binding.menuId, items[index]);
    items[index].focus();
  }

  function bind(boundary, bindings) {
    (bindings || []).forEach(function (binding) {
      _remember(binding);
      var trigger = boundary && boundary.querySelector('#' + binding.triggerId);
      var menu = boundary && boundary.querySelector('#' + binding.menuId);
      if (trigger) trigger.addEventListener('keydown', function (event) {
        _triggerKey(event, binding);
      });
      if (menu) menu.addEventListener('keydown', function (event) {
        _menuKey(event, binding);
      });
      if (menu) menu.addEventListener('focusin', function (event) {
        var item = event.target && event.target.closest
          ? event.target.closest('[role="menuitem"]') : null;
        if (item && menu.contains(item)) syncItems(binding.menuId, item);
      });
      syncItems(binding.menuId);
    });
    if (boundary) boundary.addEventListener('click', function (event) {
      var target = event.target;
      var insidePopup = (bindings || []).some(function (binding) {
        var trigger = _element(binding.triggerId);
        var menu = _element(binding.menuId);
        return !!target && ((trigger && trigger.contains(target))
          || (menu && menu.contains(target)));
      });
      if (!insidePopup) _close(bindings, {restoreFocus: false});
    });
  }

  return {
    bind: bind,
    closeAll: closeAll,
    focusEdge: focusEdge,
    isOpen: isOpen,
    setOpen: setOpen,
    syncItems: syncItems,
  };
}

/* ===== migrated source: orchestration-shell-toolbar.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-shell-toolbar.js — Studio action hierarchy markup

   Groups definition, history, canvas and work-surface actions into named
   regions. The shell owns event binding; this module owns only stable,
   localized toolbar structure.
   ═══════════════════════════════════════════════════════════════════ */

function orchestrationStudioToolbarHtml(options) {
  options = options || {};
  var tx = options.tx || function (key) { return key; };
  var icons = options.icons || {};

  return ''
    + '<div class="orch-top-actions" role="toolbar" aria-orientation="horizontal" aria-label="' + tx('orch.toolbar.actions') + '">'
    +   '<div class="orch-top-actions-scroll">'
    +     '<div class="orch-action-group orch-action-group-mobile orch-m-only" role="group" aria-label="' + tx('orch.toolbar.mobilePanels') + '">'
    +       '<button type="button" class="orch-btn orch-btn-ghost orch-m-pal-btn" data-orch-shell-action="toggleMobilePalette" aria-controls="orchPalette" aria-expanded="false">' + icons.plus + ' ' + tx('orch.toolbar.nodes') + '</button>'
    +       '<button type="button" class="orch-btn orch-btn-ghost orch-m-insp-btn" data-orch-shell-action="toggleMobileInspector" aria-controls="orchInspector" aria-expanded="false">' + icons.gear + ' ' + tx('orch.toolbar.edit') + '</button>'
    +     '</div>'
    +     '<div class="orch-action-group orch-action-group-definition" role="group" aria-label="' + tx('orch.toolbar.definitionActions') + '">'
    +       '<div class="orch-tpl-wrap">'
    +         '<button type="button" class="orch-btn orch-btn-ghost" id="orchTplBtn" data-orch-shell-action="toggleTemplateMenu" aria-haspopup="menu" aria-controls="orchTplMenu" aria-expanded="false" title="' + tx('orch.toolbar.templates') + '" aria-label="' + tx('orch.toolbar.templates') + '">' + icons.wand + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.templates') + '</span> ' + icons.chevronDown + '</button>'
    +         '<div class="orch-tpl-menu" id="orchTplMenu" role="menu" aria-label="' + tx('orch.toolbar.templates') + '" style="display:none">'
    +           '<button type="button" role="menuitem" data-orch-shell-builtin="autopilot">' + icons.auto + ' ' + tx('orch.template.autopilot') + '</button>'
    +           '<button type="button" role="menuitem" data-orch-shell-builtin="fanout">' + icons.fanout + ' ' + tx('orch.template.fanout') + '</button>'
    +           '<button type="button" role="menuitem" data-orch-shell-builtin="adversarial">' + icons.shield + ' ' + tx('orch.template.adversarial') + '</button>'
    +           '<button type="button" role="menuitem" data-orch-shell-builtin="blank">' + icons.plus + ' ' + tx('orch.template.blank') + '</button>'
    +         '</div>'
    +       '</div>'
    +       '<div class="orch-tpl-wrap">'
    +         '<button type="button" class="orch-btn orch-btn-ghost" id="orchLoadBtn" data-orch-shell-action="openLoadMenu" aria-haspopup="menu" aria-controls="orchLoadMenu" aria-expanded="false" title="' + tx('orch.toolbar.open') + '" aria-label="' + tx('orch.toolbar.open') + '">' + icons.folder + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.open') + '</span> ' + icons.chevronDown + '</button>'
    +         '<div class="orch-load-menu" id="orchLoadMenu" role="menu" aria-label="' + tx('orch.toolbar.open') + '" aria-busy="false" style="display:none"></div>'
    +       '</div>'
    +       '<button type="button" class="orch-btn orch-btn-ghost" data-orch-shell-action="exportDefinition" title="' + tx('orch.toolbar.export') + '" aria-label="' + tx('orch.toolbar.export') + '">' + icons.download + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.export') + '</span></button>'
    +     '</div>'
    +     '<div class="orch-action-group orch-action-group-history" role="group" aria-label="' + tx('orch.toolbar.historyActions') + '">'
    +       '<button type="button" class="orch-btn orch-btn-ghost orch-history-btn" id="orchUndoBtn" disabled data-orch-shell-action="undo" aria-keyshortcuts="Control+Z Meta+Z" title="' + tx('orch.toolbar.undo') + '" aria-label="' + tx('orch.toolbar.undo') + '">' + icons.undo + '</button>'
    +       '<button type="button" class="orch-btn orch-btn-ghost orch-history-btn" id="orchRedoBtn" disabled data-orch-shell-action="redo" aria-keyshortcuts="Control+Shift+Z Meta+Shift+Z" title="' + tx('orch.toolbar.redo') + '" aria-label="' + tx('orch.toolbar.redo') + '">' + icons.redo + '</button>'
    +     '</div>'
    +     '<div class="orch-action-group orch-action-group-canvas" role="group" aria-label="' + tx('orch.toolbar.canvasActions') + '">'
    +       '<button type="button" class="orch-btn orch-btn-ghost" data-orch-shell-action="tidy" title="' + tx('orch.toolbar.tidyTip') + '" aria-label="' + tx('orch.toolbar.tidy') + '">' + icons.layout + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.tidy') + '</span></button>'
    +       '<div class="orch-rail-controls" role="group" aria-label="' + tx('orch.toolbar.panelLayout') + '">'
    +         '<button type="button" class="orch-btn orch-btn-ghost orch-rail-btn" id="orchPaletteRailBtn" data-orch-shell-action="togglePaletteRail" aria-controls="orchPalette" aria-expanded="true" title="' + tx('orch.toolbar.hideNodes') + '" aria-label="' + tx('orch.toolbar.hideNodes') + '">' + icons.plus + '</button>'
    +         '<button type="button" class="orch-btn orch-btn-ghost orch-focus-btn" id="orchFocusCanvasBtn" data-orch-shell-action="toggleCanvasFocus" aria-pressed="false" title="' + tx('orch.toolbar.focusCanvas') + '" aria-label="' + tx('orch.toolbar.focusCanvas') + '">' + icons.panels + '</button>'
    +         '<button type="button" class="orch-btn orch-btn-ghost orch-rail-btn" id="orchInspectorRailBtn" data-orch-shell-action="toggleInspectorRail" aria-controls="orchInspector" aria-expanded="true" title="' + tx('orch.toolbar.hideInspector') + '" aria-label="' + tx('orch.toolbar.hideInspector') + '">' + icons.gear + '</button>'
    +       '</div>'
    +     '</div>'
    +     '<div class="orch-action-group orch-action-group-work" role="group" aria-label="' + tx('orch.toolbar.workSurfaces') + '">'
    +       '<button type="button" class="orch-btn orch-btn-ghost" id="orchAiToggle" data-orch-shell-action="toggleAi" aria-controls="orchAi" aria-expanded="false" title="' + tx('orch.toolbar.aiComposer') + '" aria-label="' + tx('orch.toolbar.aiComposer') + '">' + icons.wand + ' <span class="orch-btn-label-compact">' + tx('orch.toolbar.aiComposer') + '</span></button>'
    +     '</div>'
    +   '</div>'
    +   '<div class="orch-top-actions-primary">'
    +     '<button type="button" class="orch-btn orch-btn-run" id="orchOpenRunBtn" data-orch-shell-action="openRun" aria-controls="orchRunDrawer" aria-expanded="false" title="' + tx('orch.toolbar.run') + '" aria-label="' + tx('orch.toolbar.run') + '">' + icons.rocket + ' <span class="orch-btn-label-narrow">' + tx('orch.toolbar.run') + '</span></button>'
    +     '<button type="button" class="orch-btn orch-btn-ghost" id="orchSaveBtn" data-orch-shell-action="save" aria-keyshortcuts="Control+S Meta+S" title="' + tx('orch.toolbar.save') + '" aria-label="' + tx('orch.toolbar.save') + '">' + icons.save + ' <span class="orch-btn-label-narrow">' + tx('orch.toolbar.save') + '</span></button>'
    +     '<button type="button" class="orch-btn orch-btn-primary" id="orchSaveUseBtn" data-orch-shell-action="saveAndUse" title="' + tx('orch.toolbar.saveUse') + '" aria-label="' + tx('orch.toolbar.saveUse') + '">' + icons.loop + ' <span class="orch-btn-label-narrow">' + tx('orch.toolbar.saveUse') + '</span></button>'
    +     '<span class="orch-top-sep" aria-hidden="true"></span>'
    +     '<button type="button" class="orch-btn orch-btn-close" data-orch-shell-action="close" title="' + tx('orch.tip.close') + '" aria-label="' + tx('orch.tip.close') + '">' + icons.reject + '</button>'
    +   '</div>'
    + '</div>';
}
/* ===== migrated source: orchestration-shell-work-surfaces.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-shell-work-surfaces.js — Studio work-surface markup

   Provides one frozen markup port for the Composer and Run surfaces. Their
   controllers retain visibility, rendering and request ownership; this module
   keeps the shared shell hierarchy and accessibility contract consistent.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioWorkSurfaceMarkup(options) {
  options = options || {};
  var tx = options.tx || function (key) { return key; };
  var translate = options.translate || function (key) { return key; };
  var richCopy = options.richCopy || function (value) { return String(value); };
  var icons = options.icons || {};

  function composer() {
    return ''
      + '<aside class="orch-ai orch-work-surface" id="orchAi" aria-labelledby="orchAiTitle" aria-hidden="true" inert>'
      +   '<div class="orch-work-surface-head">'
      +     '<h2 class="orch-work-surface-title" id="orchAiTitle">' + icons.wand + ' ' + tx('orch.toolbar.aiComposer') + '</h2>'
      +     '<div class="orch-work-surface-head-actions">'
      +       '<button type="button" class="orch-icon-btn" data-orch-shell-action="aiClear" title="' + tx('orch.ai.clear') + '" aria-label="' + tx('orch.ai.clear') + '">' + icons.refresh + '</button>'
      +       '<button type="button" class="orch-icon-btn" data-orch-shell-action="toggleAi" title="' + tx('orch.tip.close') + '" aria-label="' + tx('orch.tip.close') + '">' + icons.reject + '</button>'
      +     '</div>'
      +   '</div>'
      +   '<div class="orch-work-surface-log orch-work-surface-log-composer" id="orchAiLog" role="log" aria-live="polite" aria-relevant="additions" aria-busy="false"></div>'
      +   '<div class="orch-work-surface-input orch-work-surface-input-composer">'
      +     '<textarea id="orchAiText" rows="3" placeholder="' + tx('orch.ai.placeholder') + '" aria-label="' + tx('orch.ai.placeholder') + '" data-orch-shell-key="ai"></textarea>'
      +     '<button type="button" class="orch-btn orch-btn-primary orch-ai-send" id="orchAiSend" data-orch-shell-action="aiSend">' + tx('orch.ai.send') + '</button>'
      +   '</div>'
      + '</aside>';
  }

  function runDrawer() {
    return ''
      + '<div class="orch-run-drawer orch-work-surface" id="orchRunDrawer" role="region" aria-labelledby="orchRunTitle" aria-hidden="true" aria-busy="false" inert>'
      +   '<div class="orch-work-surface-head">'
      +     '<h2 class="orch-work-surface-title" id="orchRunTitle">' + icons.rocket + ' ' + tx('orch.run.title') + '</h2>'
      +     '<div class="orch-work-surface-head-actions">'
      +       '<span class="orch-run-state" id="orchRunState" role="status" aria-live="polite" aria-atomic="true" hidden><span class="orch-run-state-dot" aria-hidden="true"></span><span id="orchRunStateLabel"></span></span>'
      +       '<button type="button" class="orch-icon-btn" data-orch-shell-action="closeRun" title="' + tx('orch.tip.close') + '" aria-label="' + tx('orch.tip.close') + '">' + icons.reject + '</button>'
      +     '</div>'
      +   '</div>'
      +   '<div class="orch-work-surface-input orch-work-surface-input-run">'
      +     '<textarea id="orchRunInput" rows="3" placeholder="' + tx('orch.run.inputPlaceholder') + '" aria-label="' + tx('orch.run.inputPlaceholder') + '" aria-describedby="orchRunHint"></textarea>'
      +     '<div class="orch-run-hint" id="orchRunHint">' + icons.eye + ' ' + richCopy(translate('orch.run.hint')) + '</div>'
      +     '<div class="orch-run-actions" role="group" aria-label="' + tx('orch.run.drawerActions') + '">'
      +       '<button type="button" class="orch-btn orch-btn-ghost" id="orchRunPlanBtn" data-orch-shell-action="plan">' + icons.eye + ' ' + tx('orch.run.previewPlan') + '</button>'
      +       '<button type="button" class="orch-btn orch-btn-run" id="orchRunBtn" data-orch-shell-action="run" title="' + tx('orch.run.testRun') + '">' + icons.auto + ' ' + tx('orch.run.testRun') + '</button>'
      +       '<button type="button" class="orch-btn orch-btn-primary" id="orchRunTaskBtn" data-orch-shell-action="runAsTask" title="' + tx('orch.run.asTask') + '">' + icons.rocket + ' ' + tx('orch.run.asTask') + '</button>'
      +       '<button type="button" class="orch-btn orch-btn-danger" id="orchRunAbort" data-orch-shell-action="abortRun" style="display:none">' + icons.stop + ' ' + tx('orch.run.stop') + '</button>'
      +     '</div>'
      +   '</div>'
      +   '<div class="orch-work-surface-log orch-work-surface-log-run" id="orchRunLog" role="log" aria-live="polite" aria-relevant="additions" aria-busy="false"></div>'
      + '</div>';
  }

  return Object.freeze({ composer: composer, runDrawer: runDrawer });
}

/* ===== migrated source: orchestration-shell-command-bindings.js ===== */
/* Studio Shell DOM events projected onto its stable command vocabulary. */

function bindOrchestrationStudioShellCommands(root, commands, popupMenus) {
  commands = commands || {};

  function invoke(name) {
    var command = commands[name];
    if (typeof command !== 'function') return;
    return command.apply(null, Array.prototype.slice.call(arguments, 1));
  }

  Array.prototype.forEach.call(
    root.querySelectorAll('[data-orch-shell-action]'), function (control) {
      control.addEventListener('click', function () {
        invoke(control.getAttribute('data-orch-shell-action') || '');
      });
    }
  );
  Array.prototype.forEach.call(
    root.querySelectorAll('[data-orch-shell-builtin]'), function (control) {
      control.addEventListener('click', function () {
        invoke('chooseBuiltin',
          control.getAttribute('data-orch-shell-builtin') || '');
      });
    }
  );
  var nameInput = root.querySelector('[data-orch-shell-input="rename"]');
  if (nameInput) nameInput.addEventListener('input', function () {
    invoke('rename', nameInput.value);
  });
  var aiInput = root.querySelector('[data-orch-shell-key="ai"]');
  if (aiInput) aiInput.addEventListener('keydown', function (event) {
    invoke('aiKey', event);
  });
  if (popupMenus && typeof popupMenus.bind === 'function') {
    popupMenus.bind(root, [
      {
        triggerId: 'orchTplBtn', menuId: 'orchTplMenu',
        open: function () { return invoke('toggleTemplateMenu'); },
      },
      {
        triggerId: 'orchLoadBtn', menuId: 'orchLoadMenu',
        open: function () { return invoke('openLoadMenu', true); },
      },
    ]);
  }
}

/* ===== migrated source: orchestration-shell.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-shell.js — Orchestration Studio shell view

   Builds the stable modal/panel DOM. Focused toolbar and work-surface markup
   owners must load first. This view owns no graph state and no transport:
   orchestration.js mounts it, wires the canvas and supplies an explicit
   command interface. All DOM handlers stay local to this view.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioShell(options) {
  options = options || {};
  var doc = options.document || document;
  var icons = options.icons || {};
  var translate = options.translate || function (key) { return key; };
  var escape = options.escape || function (value) { return String(value || ''); };
  var richCopy = typeof options.richCopy === 'function'
    ? options.richCopy : function (value) { return escape(value); };
  var workSurfaceMarkup = createOrchestrationStudioWorkSurfaceMarkup({
    tx: tx, translate: translate, richCopy: richCopy, icons: icons,
  });
  var limitPolicy = orchestrationRequestLimitPolicy(
    options.limitPolicy || options.requestLimits);
  function tx(key, params) {
    return escape(translate(key, params));
  }

  var ov = doc.createElement('div');
  ov.className = 'orch-overlay';
  ov.id = 'orchModal';
  ov.style.display = 'none';
  if (typeof options.onBackdrop === 'function') {
    ov.addEventListener('click', options.onBackdrop);
  }

  ov.innerHTML = ''
    + '<div class="orch-shell" role="dialog" aria-modal="true" tabindex="-1" aria-label="' + tx('orch.shell.title') + '">'
    +   '<header class="orch-top">'
    +     '<div class="orch-top-left">'
    +       '<span class="orch-logo"><img src="' + escape(options.logoUrl || '') + '" alt="" width="22" height="22"></span>'
    +       '<input id="orchNameInput" class="orch-name-input" spellcheck="false" '
    +              'aria-label="' + tx('orch.shell.flowName') + '" data-orch-shell-input="rename" />'
    +       '<div class="orch-doc-state-wrap">'
    +         '<button type="button" id="orchDocState" class="orch-doc-state is-draft" data-orch-shell-action="showDocIssues" aria-live="polite" aria-haspopup="dialog" aria-controls="orchIssuePanel" aria-expanded="false"></button>'
    +         '<div class="orch-issues-panel" id="orchIssuePanel" role="dialog" aria-label="' + tx('orch.issues.title') + '" hidden></div>'
    +       '</div>'
    +     '</div>'
    +     orchestrationStudioToolbarHtml({ tx: tx, icons: icons })
    +   '</header>'
    +   '<div class="orch-body">'
    +     workSurfaceMarkup.composer()
    +     '<aside class="orch-palette" id="orchPalette" aria-label="' + tx('orch.toolbar.nodes') + '"></aside>'
    +     '<div class="orch-panel-resizer orch-panel-resizer-palette" id="orchPaletteResize" role="separator" tabindex="0" aria-orientation="vertical" aria-controls="orchPalette" aria-label="' + tx('orch.panel.resizeNodes') + '"></div>'
    +     '<main class="orch-canvas-wrap">'
    +       '<nav class="orch-crumb" id="orchCrumb" aria-label="' + tx('orch.crumb.label') + '" hidden></nav>'
    +       '<div class="orch-canvas" id="orchCanvas" role="group" tabindex="-1" aria-label="' + tx('orch.canvas.title') + '">'
    +         '<div class="orch-viewport-extent" id="orchViewportExtent">'
    +           '<div class="orch-viewport-scene" id="orchViewportScene">'
    +             '<svg class="orch-edges" id="orchEdges"></svg>'
    +             '<div class="orch-nodes" id="orchNodes"></div>'
    +           '</div>'
    +         '</div>'
    +         '<div class="orch-hint" id="orchHint"></div>'
    +       '</div>'
    +       '<div class="orch-viewport-tools" role="group" aria-label="' + tx('orch.viewport.controls') + '">'
    +           '<button type="button" class="orch-viewport-btn" data-orch-shell-action="fitView" title="' + tx('orch.viewport.fit') + '" aria-label="' + tx('orch.viewport.fit') + '">' + icons.fit + '</button>'
    +           '<button type="button" class="orch-viewport-btn" id="orchZoomOutBtn" data-orch-shell-action="zoomOut" title="' + tx('orch.viewport.zoomOut') + '" aria-label="' + tx('orch.viewport.zoomOut') + '">' + icons.minus + '</button>'
    +           '<button type="button" class="orch-viewport-level" id="orchZoomResetBtn" data-orch-shell-action="zoomReset" title="' + tx('orch.viewport.reset') + '" aria-label="' + tx('orch.viewport.reset') + '">100%</button>'
    +           '<button type="button" class="orch-viewport-btn" id="orchZoomInBtn" data-orch-shell-action="zoomIn" title="' + tx('orch.viewport.zoomIn') + '" aria-label="' + tx('orch.viewport.zoomIn') + '">' + icons.plus + '</button>'
    +       '</div>'
    +     '</main>'
    +     '<button type="button" class="orch-sheet-scrim" id="orchSheetScrim" data-orch-shell-action="dismissMobileSheet" aria-label="' + tx('orch.tip.close') + '" aria-hidden="true" inert></button>'
    +     '<div class="orch-panel-resizer orch-panel-resizer-inspector" id="orchInspectorResize" role="separator" tabindex="0" aria-orientation="vertical" aria-controls="orchInspector" aria-label="' + tx('orch.panel.resizeInspector') + '"></div>'
    +     '<aside class="orch-inspector" id="orchInspector" aria-label="' + tx('orch.toolbar.edit') + '"></aside>'
    +     workSurfaceMarkup.runDrawer()
    +   '</div>'
    + '</div>';

  limitPolicy.applyStudio(ov);

  var toolbar = ov.querySelector('.orch-top-actions');
  var view = doc.defaultView || null;
  var sheetMedia = view && typeof view.orchestrationSheetMedia === 'function'
    ? view.orchestrationSheetMedia(view) : null;
  var toolbarKeyboard = toolbar
    && typeof createOrchestrationRovingItemsController === 'function'
    ? createOrchestrationRovingItemsController({
      root: toolbar, selector: 'button[data-orch-shell-action]', wrap: true,
      available: function (item) {
        var mobile = !!(sheetMedia && sheetMedia.matches);
        if (item.closest('.orch-action-group-mobile')) return mobile;
        if (item.closest('.orch-rail-controls')) return !mobile;
        return true;
      },
    }) : null;
  if (toolbarKeyboard && sheetMedia
      && typeof sheetMedia.addEventListener === 'function') {
    sheetMedia.addEventListener('change', function () { toolbarKeyboard.sync(); });
  }
  if (toolbarKeyboard && view && typeof view.MutationObserver === 'function') {
    new view.MutationObserver(function () { toolbarKeyboard.sync(); }).observe(
      toolbar, { subtree: true, attributes: true,
        attributeFilter: ['disabled', 'hidden', 'aria-disabled'] });
  }

  bindOrchestrationStudioShellCommands(
    ov, options.commands, options.popupMenus);

  return ov;
}

/* ===== migrated source: orchestration-shell-commands.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-shell-commands.js — Studio toolbar command adapter

   The Shell view speaks one stable command vocabulary. This adapter maps it
   to focused Studio controllers and validates those ports up front, keeping
   toolbar wiring out of the composition root and preventing silent dead
   buttons when a controller is refactored.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioShellCommands(options) {
  options = options || {};
  var requirements = {
    studio: [
      'toggleMobilePalette', 'toggleMobileInspector',
      'dismissMobileSheet', 'close',
    ],
    document: ['showIssues'],
    workspace: [
      'toggleTemplateMenu', 'chooseBuiltin', 'openLoadMenu', 'tidy', 'save',
      'saveAndUse',
    ],
    history: ['undoAndApply', 'redoAndApply'],
    viewport: ['fit', 'zoomOut', 'reset', 'zoomIn'],
    panels: [
      'toggle', 'togglePalette', 'toggleInspector',
      'toggleComposer', 'openRun',
    ],
    composer: ['clear', 'handleKey', 'send'],
    run: ['close', 'plan', 'run', 'runAsTask', 'abort'],
    exporter: ['exportCurrent'],
  };

  Object.keys(requirements).forEach(function (portName) {
    var port = options[portName];
    requirements[portName].forEach(function (methodName) {
      if (!port || typeof port[methodName] !== 'function') {
        throw new TypeError(
          'invalid Studio shell command port: '
          + portName + '.' + methodName
        );
      }
    });
  });
  if (typeof options.rename !== 'function') {
    throw new TypeError('invalid Studio shell command port: rename');
  }

  function invoke(portName, methodName) {
    return function () {
      var port = options[portName];
      return port[methodName].apply(
        port, Array.prototype.slice.call(arguments));
    };
  }

  return Object.freeze({
    rename: options.rename,
    showDocIssues: invoke('document', 'showIssues'),
    toggleMobilePalette: invoke('studio', 'toggleMobilePalette'),
    toggleMobileInspector: invoke('studio', 'toggleMobileInspector'),
    dismissMobileSheet: invoke('studio', 'dismissMobileSheet'),
    toggleTemplateMenu: invoke('workspace', 'toggleTemplateMenu'),
    chooseBuiltin: invoke('workspace', 'chooseBuiltin'),
    openLoadMenu: invoke('workspace', 'openLoadMenu'),
    undo: invoke('history', 'undoAndApply'),
    redo: invoke('history', 'redoAndApply'),
    fitView: invoke('viewport', 'fit'),
    zoomOut: invoke('viewport', 'zoomOut'),
    zoomReset: invoke('viewport', 'reset'),
    zoomIn: invoke('viewport', 'zoomIn'),
    tidy: invoke('workspace', 'tidy'),
    togglePaletteRail: invoke('panels', 'togglePalette'),
    toggleCanvasFocus: invoke('panels', 'toggle'),
    toggleInspectorRail: invoke('panels', 'toggleInspector'),
    toggleAi: invoke('panels', 'toggleComposer'),
    openRun: invoke('panels', 'openRun'),
    exportDefinition: invoke('exporter', 'exportCurrent'),
    save: invoke('workspace', 'save'),
    saveAndUse: invoke('workspace', 'saveAndUse'),
    close: invoke('studio', 'close'),
    aiClear: invoke('composer', 'clear'),
    aiKey: invoke('composer', 'handleKey'),
    aiSend: invoke('composer', 'send'),
    closeRun: invoke('run', 'close'),
    plan: invoke('run', 'plan'),
    run: invoke('run', 'run'),
    runAsTask: invoke('run', 'runAsTask'),
    abortRun: invoke('run', 'abort'),
  });
}
/* ===== migrated source: orchestration-mobile-surface-projection.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-mobile-surface-projection.js — mobile modal semantics

   Projects the shared accessibility boundary for Studio sheets and
   fullscreen work surfaces. Feature controllers still own open/close state;
   this module alone owns mobile roles, background isolation and focus entry.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationMobileSurfaceProjection(options) {
  options = options || {};
  var doc = options.document || document;
  var surfaces = Object.freeze({
    palette: Object.freeze({
      panelId: 'orchPalette', className: 'orch-m-pal',
      action: 'toggleMobilePalette', initialFocus: '[data-palette-close]',
      desktopRole: null,
    }),
    inspector: Object.freeze({
      panelId: 'orchInspector', className: 'orch-m-insp',
      action: 'toggleMobileInspector', initialFocus: '.orch-inspector-close',
      desktopRole: null,
    }),
    composer: Object.freeze({
      panelId: 'orchAi', initialFocus: '#orchAiText', desktopRole: null,
    }),
    run: Object.freeze({
      panelId: 'orchRunDrawer', initialFocus: '#orchRunInput',
      desktopRole: 'region',
    }),
  });

  function sheet(name) {
    return name === 'palette' || name === 'inspector'
      ? surfaces[name] : null;
  }

  function activeName(state) {
    if (!state || !state.mobile) return null;
    var name = state.active || state.workSurface || null;
    return surfaces[name] ? name : null;
  }

  function activePanel(state) {
    var spec = surfaces[activeName(state)];
    return spec ? doc.getElementById(spec.panelId) : null;
  }

  function _projectRole(name, active) {
    var spec = surfaces[name];
    var panel = doc.getElementById(spec.panelId);
    if (!panel) return;
    if (active) {
      panel.setAttribute('role', 'dialog');
      panel.setAttribute('aria-modal', 'true');
    } else {
      if (spec.desktopRole) panel.setAttribute('role', spec.desktopRole);
      else panel.removeAttribute('role');
      panel.removeAttribute('aria-modal');
    }
  }

  function sync(state) {
    var name = activeName(state);
    Object.keys(surfaces).forEach(function (candidate) {
      _projectRole(candidate, candidate === name);
    });
    var header = doc.querySelector('.orch-shell > .orch-top');
    var panel = activePanel(state);
    if (panel && !panel.contains(doc.activeElement)) {
      focusOrchestrationPanel(panel, surfaces[name].initialFocus);
    }
    setOrchestrationPanelState(header, !panel, {
      document: doc, focusTarget: panel,
    });
    return panel;
  }

  return Object.freeze({
    activePanel: activePanel,
    sheet: sheet,
    sync: sync,
  });
}

/* ===== migrated source: orchestration-mobile-sheets.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-mobile-sheets.js — exclusive mobile sheet state

   Owns the one active mobile sheet and projects it atomically into shell
   classes, panel/trigger accessibility, graph isolation, scrim visibility
   and focus restoration. Global surface admission and desktop rail visibility
   remain Studio and PanelLayout policy respectively.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationMobileSheetController(options) {
  options = options || {};
  var doc = options.document || document;
  var activeName = null;
  var focusOwner = 'palette';
  var projection = options.projection
    || createOrchestrationMobileSurfaceProjection({document: doc});

  function _shell() { return doc.querySelector('.orch-shell'); }
  function _mobile() {
    return typeof options.isMobile === 'function' && !!options.isMobile();
  }
  function _trigger(spec) {
    return doc.querySelector(
      '[data-orch-shell-action="' + spec.action + '"]');
  }
  function _activeWorkSurface() {
    return typeof options.activeWorkSurface === 'function'
      ? options.activeWorkSurface() : null;
  }

  function snapshot() {
    var mobile = _mobile();
    var active = mobile ? activeName : null;
    var workSurface = mobile ? _activeWorkSurface() : null;
    return {
      mobile: mobile,
      active: active,
      sheetOpen: !!active,
      workSurface: workSurface,
      workSurfaceOpen: !!workSurface,
      backgroundBlocked: !!active || !!workSurface,
    };
  }

  function _projectSurface(shell, name, state) {
    var spec = projection.sheet(name);
    var open = state.active === name;
    if (shell) shell.classList.toggle(spec.className, open);
    var trigger = _trigger(spec);
    // Desktop rail accessibility belongs to PanelLayout. Mobile triggers are
    // still reset so a hidden phone control never advertises a stale sheet.
    if (!state.mobile) {
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
      return;
    }
    setOrchestrationPanelState(doc.getElementById(spec.panelId), open, {
      document: doc,
      trigger: trigger,
    });
  }

  function sync() {
    var shell = _shell();
    if (!_mobile()) activeName = null;
    var state = snapshot();
    // Restore the toolbar before a closing sheet tries to return focus to its
    // trigger. Opening surfaces are projected first, then isolate the toolbar.
    if (!state.backgroundBlocked) projection.sync(state);
    _projectSurface(shell, 'palette', state);
    _projectSurface(shell, 'inspector', state);

    var focusTarget = _trigger(projection.sheet(state.active || focusOwner));
    setOrchestrationPanelState(
      doc.querySelector('.orch-canvas-wrap'), !state.backgroundBlocked,
      {document: doc, focusTarget: state.sheetOpen ? focusTarget : null}
    );
    setOrchestrationPanelState(
      doc.getElementById('orchSheetScrim'), state.sheetOpen,
      {document: doc, openClass: 'is-open', focusTarget: focusTarget}
    );
    if (state.backgroundBlocked) projection.sync(state);
    if (!state.mobile && typeof options.syncDesktopPanels === 'function') {
      options.syncDesktopPanels();
    }
    if (typeof options.onChange === 'function') options.onChange(state);
    return state;
  }

  function setOpen(name, open) {
    var spec = projection.sheet(name);
    if (!spec || !_shell()) return false;
    var opening = !!open && activeName !== name;
    if (opening && _mobile() && typeof options.admitOpen === 'function'
        && options.admitOpen(name) === false) return false;
    if (open) {
      activeName = name;
      focusOwner = name;
    } else if (activeName === name) {
      activeName = null;
      focusOwner = name;
    }
    sync();
    if (opening && activeName === name) {
      focusOrchestrationPanel(
        doc.getElementById(spec.panelId), spec.initialFocus);
    }
    return activeName === name;
  }

  function toggle(name) {
    if (!projection.sheet(name) || !_shell()) return false;
    return setOpen(name, activeName !== name);
  }

  function isOpen(name) { return _mobile() && activeName === name; }

  function close(name) {
    if (!projection.sheet(name) || !_shell()) return false;
    if (activeName !== name) return true;
    setOpen(name, false);
    return activeName !== name;
  }

  function dismiss() {
    if (!_mobile() || !activeName) return false;
    return close(activeName);
  }

  return {
    active: function () { return snapshot().active; },
    activePanel: function () { return projection.activePanel(snapshot()); },
    isOpen: isOpen,
    close: close,
    dismiss: dismiss,
    setOpen: setOpen,
    snapshot: snapshot,
    sync: sync,
    toggle: toggle,
  };
}

/* ===== migrated source: orchestration-work-surfaces.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-work-surfaces.js — exclusive Studio work surfaces

   Coordinates Composer and Run through one open/close/isOpen port. The
   admitOpen hook also provides the shared handoff contract for
   other exclusive surfaces. Rail layout, DOM projection, focus restoration
   and feature state stay in their focused controllers.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationWorkSurfaceController(options) {
  options = options || {};
  var names = Array.isArray(options.names)
    ? options.names.slice() : ['composer', 'run'];
  var dismissOrder = Array.isArray(options.dismissOrder)
    ? options.dismissOrder.slice() : names.slice().reverse();

  function _surface(name) {
    if (names.indexOf(name) < 0) return null;
    var candidate = options.surfaces && options.surfaces[name];
    return typeof candidate === 'function' ? candidate() : candidate;
  }

  function _isOpen(surface) {
    return !!(surface && typeof surface.isOpen === 'function'
      && surface.isOpen());
  }

  function isOpen(name) {
    return _isOpen(_surface(name));
  }

  function close(name) {
    var surface = _surface(name);
    if (!surface || typeof surface.close !== 'function') return false;
    if (!_isOpen(surface)) return true;
    surface.close();
    return !_isOpen(surface);
  }

  function open(name) {
    var target = _surface(name);
    if (!target || typeof target.open !== 'function') return false;
    var otherOpen = names.some(function (other) {
      return other !== name && isOpen(other);
    });
    if (_isOpen(target) && !otherOpen) return true;
    if (typeof options.admitOpen === 'function'
        && options.admitOpen(name) === false) return false;
    for (var i = 0; i < names.length; i++) {
      var other = names[i];
      if (other !== name && isOpen(other) && !close(other)) return false;
    }
    if (!_isOpen(target)) target.open();
    return _isOpen(target);
  }

  function toggle(name) {
    if (isOpen(name)) { close(name); return isOpen(name); }
    return open(name);
  }

  function dismiss() {
    for (var i = 0; i < dismissOrder.length; i++) {
      if (isOpen(dismissOrder[i])) {
        return close(dismissOrder[i]);
      }
    }
    return false;
  }

  function active() {
    for (var i = 0; i < names.length; i++) {
      if (isOpen(names[i])) return names[i];
    }
    return null;
  }

  return Object.freeze({
    active: active,
    isOpen: isOpen,
    open: open,
    close: close,
    toggle: toggle,
    dismiss: dismiss,
  });
}

/* ===== migrated source: orchestration-studio-keyboard.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-studio-keyboard.js — document-level Studio key policy

   Routes focus trapping, document commands, transient dismissal and graph
   deletion through injected ports. It owns no modal or sheet lifecycle.
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationStudioKeyboardController(options) {
  options = options || {};

  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return typeof options[name] === 'function'
      ? options[name].apply(null, args) : undefined;
  }

  function keyDown(event) {
    var element = call('modal');
    if (!element || element.style.display === 'none') return;
    var dialog = element.querySelector('[role="dialog"]');
    if (event.key === 'Tab' && dialog) {
      call('trapTab', event, call('activePanel') || dialog);
      return;
    }
    var target = event.target || {};
    var tag = String(target.tagName || '').toLowerCase();
    var editing = tag === 'input' || tag === 'textarea' || tag === 'select'
      || target.isContentEditable;
    var modified = !!(event.ctrlKey || event.metaKey) && !event.altKey;
    var key = String(event.key || '').toLowerCase();

    // Save remains available while a field is focused and suppresses the
    // browser's save-page dialog.
    if (modified && key === 's') {
      event.preventDefault();
      call('save');
      return;
    }
    if (modified && !editing && !call('commandsBlocked') && key === 'z') {
      event.preventDefault();
      call(event.shiftKey ? 'redo' : 'undo');
      return;
    }
    if (modified && !editing && !call('commandsBlocked')
        && !event.shiftKey && key === 'y') {
      event.preventDefault();
      call('redo');
      return;
    }
    if (modified && !editing && !call('commandsBlocked')) {
      if (key === '+' || key === '=' || key === 'add') {
        event.preventDefault(); call('zoomIn'); return;
      }
      if (key === '-' || key === '_' || key === 'subtract') {
        event.preventDefault(); call('zoomOut'); return;
      }
      if (key === '0') {
        event.preventDefault(); call('zoomReset'); return;
      }
    }
    if (event.key === 'Escape') {
      for (var index = 0; index < 4; index += 1) {
        var action = [
          'cancelGesture', 'closePopups', 'dismissTransient',
          'dismissMobileSheet',
        ][index];
        if (call(action)) {
          event.preventDefault();
          return;
        }
      }
      return;
    }
    if (event.key !== 'Delete' && event.key !== 'Backspace') return;
    if (editing || call('commandsBlocked')) return;
    var edgeId = call('selectedEdgeId');
    var nodeId = call('selectedNodeId');
    if (edgeId) {
      event.preventDefault(); call('deleteEdge', edgeId);
    } else if (nodeId) {
      event.preventDefault(); call('deleteNode', nodeId);
    }
  }

  return Object.freeze({ keyDown: keyDown });
}

/* ===== migrated source: orchestration-studio.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-studio.js — Studio shell lifecycle + global UI policy

   Owns lazy modal mounting, open/close guards and exclusive-surface handoff.
   Document-level keyboard policy lives in orchestration-studio-keyboard.js;
   graph/document operations arrive as callbacks.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioController(options) {
  options = options || {};
  var doc = options.document || document;
  var win = options.window || window;
  var sheetMedia = typeof win.orchestrationSheetMedia === 'function'
    ? win.orchestrationSheetMedia(win) : null;
  var ready = false;
  var focusManager = createOrchestrationDialogFocusManager({
    document: doc,
    window: win,
  });
  var mobileSheets = options.mobileSheets
    || createOrchestrationMobileSheetController({
      document: doc,
      isMobile: isMobile,
      activeWorkSurface: activeWorkSurface,
      admitOpen: releaseWorkSurface,
      syncDesktopPanels: options.syncDesktopPanels,
    });

  function modal() { return doc.getElementById('orchModal'); }
  function shell() { return doc.querySelector('.orch-shell'); }
  function isReady() { return ready; }

  function syncMobileSheets() {
    return mobileSheets.sync();
  }

  function dismissMobileSheet() {
    return mobileSheets.dismiss();
  }

  function activeWorkSurface() {
    return options.workSurfaces
      && typeof options.workSurfaces.active === 'function'
      ? options.workSurfaces.active() : null;
  }

  function releaseWorkSurface() {
    if (!activeWorkSurface()) return true;
    if (!options.workSurfaces
        || typeof options.workSurfaces.dismiss !== 'function') return false;
    options.workSurfaces.dismiss();
    return !activeWorkSurface();
  }

  function releaseMobileSheet() {
    if (!mobileSheets.active()) return true;
    mobileSheets.dismiss();
    return !mobileSheets.active();
  }

  function resetTransient() {
    if (!releaseWorkSurface() || !releaseMobileSheet()) return false;
    if (typeof options.cancelGesture === 'function') {
      options.cancelGesture();
    }
    if (typeof options.closePopups === 'function') options.closePopups();
    return true;
  }

  function ensure() {
    if (ready) return modal();
    if (typeof options.createShell !== 'function') return null;
    var element = options.createShell(
      typeof options.shellOptions === 'function' ? options.shellOptions() : {}
    );
    if (!element) return null;
    doc.body.appendChild(element);
    ready = true;
    if (typeof options.onMount === 'function') options.onMount(element);
    doc.addEventListener('keydown', keyDown);
    if (sheetMedia && typeof sheetMedia.addEventListener === 'function') {
      sheetMedia.addEventListener('change', syncMobileSheets);
    }
    if (typeof options.installUnloadGuard === 'function') {
      options.installUnloadGuard(win);
    }
    return element;
  }

  function open(openOptions) {
    openOptions = openOptions || {};
    var element = ensure();
    var becameVisible = !!(element && element.style.display === 'none');
    if (element) {
      focusManager.open(element);
    }
    if (becameVisible) syncMobileSheets();
    if (!openOptions.skipInitial
        && (typeof options.hasNodes !== 'function' || !options.hasNodes())) {
      if (typeof options.loadInitial === 'function') options.loadInitial();
    }
    if (typeof options.render === 'function') options.render();
    if (typeof options.refreshContract === 'function') options.refreshContract();
  }

  async function close(event, force) {
    var element = modal();
    if (!element) return false;
    if (event && event.target !== element) return false;
    if (!force && typeof options.confirmDiscard === 'function'
        && !await options.confirmDiscard()) return false;
    if (!resetTransient()) return false;
    focusManager.close(element);
    return true;
  }

  function isMobile() {
    return !!(sheetMedia && sheetMedia.matches);
  }

  function toggleMobilePalette() {
    return mobileSheets.toggle('palette');
  }

  function closeMobilePalette() {
    return mobileSheets.close('palette');
  }

  function toggleMobileInspector() {
    return mobileSheets.toggle('inspector');
  }

  function setMobileInspectorOpen(open) {
    return mobileSheets.setOpen('inspector', !!open);
  }

  function canvasCommandsBlocked() {
    return !!(activeWorkSurface() || mobileSheets.active());
  }

  function closeMobileInspector() {
    return mobileSheets.close('inspector');
  }

  var keyboard = createOrchestrationStudioKeyboardController({
    modal: modal,
    trapTab: function (event, panel) {
      return focusManager.trapTab(event, panel);
    },
    activePanel: mobileSheets.activePanel,
    commandsBlocked: canvasCommandsBlocked,
    dismissMobileSheet: dismissMobileSheet,
    save: options.save,
    undo: options.undo,
    redo: options.redo,
    zoomIn: options.zoomIn,
    zoomOut: options.zoomOut,
    zoomReset: options.zoomReset,
    cancelGesture: options.cancelGesture,
    closePopups: options.closePopups,
    dismissTransient: options.dismissTransient,
    selectedEdgeId: options.selectedEdgeId,
    selectedNodeId: options.selectedNodeId,
    deleteEdge: options.deleteEdge,
    deleteNode: options.deleteNode,
  });

  function keyDown(event) { return keyboard.keyDown(event); }

  return {
    isReady: isReady,
    ensure: ensure,
    open: open,
    close: close,
    keyDown: keyDown,
    isMobile: isMobile,
    toggleMobilePalette: toggleMobilePalette,
    closeMobilePalette: closeMobilePalette,
    toggleMobileInspector: toggleMobileInspector,
    closeMobileInspector: closeMobileInspector,
    setMobileInspectorOpen: setMobileInspectorOpen,
    syncMobileSheets: syncMobileSheets,
    dismissMobileSheet: dismissMobileSheet,
    releaseMobileSheet: releaseMobileSheet,
  };
}

/* ===== migrated source: orchestration-panel-layout.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-panel-layout.js — Studio work-surface presentation state

   Owns desktop rails, canvas focus and transient work-surface exclusivity.
   Graph state stays untouched; callbacks resync the available canvas width.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationPanelLayoutController(options) {
  options = options || {};
  var doc = options.document || document;
  var win = options.window || window;
  var paletteExpanded = true;
  var inspectorExpanded = true;
  var lastExpanded = { palette: true, inspector: true };
  var media = typeof win.orchestrationSheetMedia === 'function'
    ? win.orchestrationSheetMedia(win) : null;
  var workSurfaces = options.workSurfaces || (
    typeof createOrchestrationWorkSurfaceController === 'function'
      ? createOrchestrationWorkSurfaceController({surfaces: {
        composer: options.composer, run: options.run,
      }}) : null);
  function desktop() { return !media || !media.matches; }
  function focused() { return !paletteExpanded && !inspectorExpanded; }
  function runOpen() {
    return !!(workSurfaces && typeof workSurfaces.isOpen === 'function'
      && workSurfaces.isOpen('run'));
  }
  function _label(key) {
    return typeof options.translate === 'function'
      ? options.translate(key) : key;
  }

  function _syncRailButton(button, expanded, hideKey, showKey) {
    if (!button) return;
    var label = _label(expanded ? hideKey : showKey);
    button.setAttribute('aria-label', label);
    button.title = label;
  }

  function sync() {
    var shell = doc.querySelector('.orch-shell');
    var button = doc.getElementById('orchFocusCanvasBtn');
    var paletteButton = doc.getElementById('orchPaletteRailBtn');
    var inspectorButton = doc.getElementById('orchInspectorRailBtn');
    var runButton = doc.getElementById('orchOpenRunBtn');
    var palette = doc.getElementById('orchPalette');
    var inspector = doc.getElementById('orchInspector');
    var isDesktop = desktop();
    var runDrawerOpen = runOpen();
    var active = focused() && isDesktop;
    var showPalette = !isDesktop || paletteExpanded;
    var showInspector = !isDesktop || (inspectorExpanded && !runDrawerOpen);
    if (shell) shell.classList.toggle('orch-focus-canvas', active);
    if (shell) {
      shell.classList.toggle(
        'orch-palette-collapsed', isDesktop && !showPalette);
      shell.classList.toggle(
        'orch-inspector-collapsed', isDesktop && !showInspector);
    }
    // Mobile sheet visibility belongs to the Studio controller. Writing it
    // here would let a focus-mode sync reopen a closed mobile sheet.
    if (isDesktop) {
      setOrchestrationPanelState(palette, showPalette, {
        document: doc,
        focusTarget: active ? button : paletteButton,
        trigger: paletteButton,
      });
      setOrchestrationPanelState(inspector, showInspector, {
        document: doc,
        focusTarget: runDrawerOpen ? runButton
          : (active ? button : inspectorButton),
        trigger: inspectorButton,
      });
    }
    _syncRailButton(
      paletteButton, showPalette,
      'orch.toolbar.hideNodes', 'orch.toolbar.showNodes');
    _syncRailButton(
      inspectorButton, showInspector,
      'orch.toolbar.hideInspector', 'orch.toolbar.showInspector');
    if (inspectorButton) {
      if (isDesktop && runDrawerOpen
          && doc.activeElement === inspectorButton) {
        var runFocusTarget = runButton || button;
        if (runFocusTarget && typeof runFocusTarget.focus === 'function')
          runFocusTarget.focus();
      }
      inspectorButton.disabled = isDesktop && runDrawerOpen;
      inspectorButton.setAttribute(
        'aria-disabled', inspectorButton.disabled ? 'true' : 'false');
    }
    if (button) {
      var key = active
        ? 'orch.toolbar.showPanels' : 'orch.toolbar.focusCanvas';
      var label = _label(key);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      button.setAttribute('aria-label', label);
      button.title = label;
    }
    if (typeof options.onChange === 'function') options.onChange(active);
    return active;
  }

  function toggle() {
    if (focused()) {
      paletteExpanded = !!lastExpanded.palette;
      inspectorExpanded = !!lastExpanded.inspector;
      if (!paletteExpanded && !inspectorExpanded) {
        paletteExpanded = true;
        inspectorExpanded = true;
      }
    } else {
      lastExpanded = {
        palette: paletteExpanded,
        inspector: inspectorExpanded,
      };
      paletteExpanded = false;
      inspectorExpanded = false;
    }
    return sync();
  }

  function _toggleRail(name) {
    var wasFocused = focused();
    if (name === 'palette') paletteExpanded = !paletteExpanded;
    else inspectorExpanded = !inspectorExpanded;
    if (!focused()) {
      lastExpanded = {
        palette: paletteExpanded,
        inspector: inspectorExpanded,
      };
    } else if (!wasFocused) {
      // The last visible rail was closed manually. Preserve that previous
      // one-rail layout so the canvas-focus control has a useful restore.
      lastExpanded = name === 'palette'
        ? { palette: true, inspector: false }
        : { palette: false, inspector: true };
    }
    sync();
    return name === 'palette' ? paletteExpanded : inspectorExpanded;
  }

  function togglePalette() { return _toggleRail('palette'); }
  function toggleInspector() { return _toggleRail('inspector'); }
  function showInspector() {
    if (runOpen() && !workSurfaces.close('run')) return false;
    if (!inspectorExpanded) {
      inspectorExpanded = true; lastExpanded.inspector = true;
    }
    sync();
    return true;
  }
  function setRunDrawerOpen() {
    sync(); return runOpen();
  }
  function toggleComposer() {
    return workSurfaces ? workSurfaces.toggle('composer') : false;
  }
  function openRun() {
    return workSurfaces ? workSurfaces.open('run') : false;
  }

  function dismissTransient() {
    if (workSurfaces && workSurfaces.dismiss()) return true;
    if (focused() && desktop()) {
      toggle();
      return true;
    }
    return false;
  }

  if (media && typeof media.addEventListener === 'function') {
    media.addEventListener('change', sync);
  }
  return {
    focused: focused,
    paletteExpanded: function () { return paletteExpanded; },
    inspectorExpanded: function () { return inspectorExpanded; },
    sync: sync,
    toggle: toggle,
    togglePalette: togglePalette,
    toggleInspector: toggleInspector,
    showInspector: showInspector,
    setRunDrawerOpen: setRunDrawerOpen,
    toggleComposer: toggleComposer,
    openRun: openRun,
    dismissTransient: dismissTransient,
  };
}

/* ===== migrated source: orchestration-panel-width-model.js ===== */
/* Pure responsive width/preference model for Studio desktop rails. */

function createOrchestrationPanelWidthModel(options) {
  options = options || {};
  var specs = options.specs || {};
  var minCanvasWidth = Number(options.minCanvasWidth) || 360;
  var handleSpace = Number(options.handleSpace) || 0;
  var customized = {};
  var preferred = {};
  var widths = {};

  function names() { return Object.keys(specs); }
  function valid(name) {
    return Object.prototype.hasOwnProperty.call(specs, name);
  }
  function compact() {
    return typeof options.compact === 'function' && !!options.compact();
  }
  function expanded(name) {
    if (typeof options.isExpanded !== 'function') return true;
    try { return options.isExpanded(name) !== false; }
    catch (_error) { return true; }
  }
  function defaultWidth(name) {
    var spec = specs[name];
    return compact() ? spec.compact : spec.normal;
  }
  function bounds(name) {
    var spec = specs[name];
    if (!spec) return { min: 0, max: 0 };
    var other = names().find(function (candidate) {
      return candidate !== name;
    });
    var available = typeof options.surfaceWidth === 'function'
      ? Math.max(0, Number(options.surfaceWidth()) || 0) : 0;
    var otherWidth = other && expanded(other) ? Number(widths[other]) || 0 : 0;
    var dynamicMax = available
      ? available - otherWidth - minCanvasWidth - handleSpace
      : spec.max;
    return Object.freeze({
      min: spec.min,
      max: Math.max(spec.min, Math.min(spec.max, dynamicMax)),
    });
  }
  function clamp(name, value) {
    var range = bounds(name);
    var numeric = Number(value);
    if (!Number.isFinite(numeric)) numeric = defaultWidth(name);
    return Math.round(Math.max(range.min, Math.min(range.max, numeric)));
  }
  function snapshot() {
    var value = {};
    names().forEach(function (name) { value[name] = widths[name] || 0; });
    return Object.freeze(value);
  }
  function persisted() {
    var value = {};
    names().forEach(function (name) {
      if (customized[name]) value[name] = preferred[name];
    });
    return Object.freeze(value);
  }
  function hydrate(stored) {
    stored = stored && typeof stored === 'object' ? stored : {};
    names().forEach(function (name) {
      var own = Object.prototype.hasOwnProperty.call(stored, name);
      var value = own ? Number(stored[name]) : NaN;
      customized[name] = Number.isFinite(value) && value > 0;
      preferred[name] = customized[name]
        ? Math.round(Math.max(specs[name].min, Math.min(specs[name].max, value)))
        : defaultWidth(name);
      widths[name] = preferred[name];
    });
    return sync();
  }
  function setWidth(name, value) {
    if (!valid(name)) return 0;
    customized[name] = true;
    widths[name] = clamp(name, value);
    preferred[name] = widths[name];
    return widths[name];
  }
  function reset(name) {
    if (!valid(name)) return 0;
    customized[name] = false;
    preferred[name] = defaultWidth(name);
    widths[name] = clamp(name, preferred[name]);
    return widths[name];
  }
  function sync() {
    names().forEach(function (name) {
      if (!customized[name]) preferred[name] = defaultWidth(name);
      widths[name] = clamp(name, preferred[name]);
    });
    return snapshot();
  }

  return Object.freeze({
    bounds: bounds,
    hydrate: hydrate,
    persisted: persisted,
    reset: reset,
    setWidth: setWidth,
    snapshot: snapshot,
    sync: sync,
  });
}

/* ===== migrated source: orchestration-panel-resize.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-panel-resize.js — persistent desktop rail sizing

   Owns DOM, storage and pointer/keyboard resizing for the two rails. The
   responsive constraint/preference state lives in the pure width model. Panel
   visibility remains in orchestration-panel-layout.js; mobile sheets remain
   in orchestration-studio.js. Widths are projected only through shell CSS
   variables so graph state and panel content never participate.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationPanelResizeController(options) {
  options = options || {};
  var doc = options.document || document;
  var win = options.window || window;
  var compactMedia = typeof win.orchestrationCompactMedia === 'function'
    ? win.orchestrationCompactMedia(win) : null;
  var storageKey = options.storageKey
    || 'tofu.orchestration.panel-widths.v1';
  var minCanvasWidth = Number(options.minCanvasWidth) || 360;
  var handleSpace = 12;
  var specs = {
    palette: {
      handleId: 'orchPaletteResize', property: '--orch-palette-width',
      min: 160, max: 360, normal: 212, compact: 160, direction: 1,
    },
    inspector: {
      handleId: 'orchInspectorResize', property: '--orch-inspector-width',
      min: 260, max: 520, normal: 300, compact: 260, direction: -1,
    },
  };
  var bound = false;
  function _shell() { return doc.querySelector('.orch-shell'); }
  function _body() { return doc.querySelector('.orch-body'); }
  function _handle(name) { return doc.getElementById(specs[name].handleId); }
  function _storage() {
    if (Object.prototype.hasOwnProperty.call(options, 'storage')) {
      return options.storage;
    }
    try { return win.localStorage; } catch (error) { return null; }
  }
  function _compact() {
    return !!(compactMedia && compactMedia.matches);
  }
  function _expanded(name) {
    if (typeof options.isExpanded !== 'function') return true;
    try { return options.isExpanded(name) !== false; }
    catch (error) { return true; }
  }
  var model = options.widthModel || createOrchestrationPanelWidthModel({
    specs: specs,
    minCanvasWidth: minCanvasWidth,
    handleSpace: handleSpace,
    compact: _compact,
    isExpanded: _expanded,
    surfaceWidth: _surfaceWidth,
  });

  function _load() {
    var storage = _storage();
    if (!storage || typeof storage.getItem !== 'function') return {};
    try {
      var value = JSON.parse(storage.getItem(storageKey) || '{}');
      return value && typeof value === 'object' ? value : {};
    } catch (error) { return {}; }
  }

  function _persist() {
    var storage = _storage();
    if (!storage) return;
    var value = model.persisted();
    try {
      if (Object.keys(value).length && typeof storage.setItem === 'function') {
        storage.setItem(storageKey, JSON.stringify(value));
      } else if (typeof storage.removeItem === 'function') {
        storage.removeItem(storageKey);
      }
    } catch (error) { /* Storage is an optional enhancement. */ }
  }

  function _surfaceWidth() {
    var element = _body() || _shell();
    if (!element) return 0;
    var rect = typeof element.getBoundingClientRect === 'function'
      ? element.getBoundingClientRect() : null;
    return Math.max(0, Number(rect && rect.width) || element.clientWidth || 0);
  }

  function _syncHandle(name) {
    var handle = _handle(name), bounds = model.bounds(name);
    if (!handle) return;
    handle.setAttribute('aria-valuemin', String(bounds.min));
    handle.setAttribute('aria-valuemax', String(bounds.max));
    var width = model.snapshot()[name];
    handle.setAttribute('aria-valuenow', String(width));
    handle.setAttribute('aria-valuetext', width + ' px');
  }

  function _project(name, width, notify) {
    var shell = _shell();
    if (!shell) return 0;
    shell.style.setProperty(specs[name].property, width + 'px');
    _syncHandle(name);
    _syncHandle(name === 'palette' ? 'inspector' : 'palette');
    if (notify && typeof options.onChange === 'function') {
      options.onChange(name, width, snapshot());
    }
    return width;
  }

  function snapshot() { return model.snapshot(); }

  function setWidth(name, value, save) {
    if (!specs[name]) return 0;
    var width = _project(name, model.setWidth(name, value), true);
    if (save !== false) _persist();
    return width;
  }

  function reset(name) {
    if (!specs[name]) return 0;
    var width = _project(name, model.reset(name), true);
    _persist();
    return width;
  }

  function sync() {
    var next = model.sync();
    _project('palette', next.palette, false);
    _project('inspector', next.inspector, false);
    return snapshot();
  }

  function _bindHandle(name) {
    var handle = _handle(name);
    if (!handle) return;
    var spec = specs[name];
    handle.addEventListener('pointerdown', function (event) {
      if (typeof event.button === 'number' && event.button !== 0) return;
      event.preventDefault();
      var startX = Number(event.clientX) || 0;
      var startWidth = snapshot()[name];
      model.setWidth(name, startWidth);
      var shell = _shell();
      if (shell) shell.classList.add('orch-panel-resizing');
      if (typeof handle.setPointerCapture === 'function'
          && event.pointerId != null) {
        try { handle.setPointerCapture(event.pointerId); } catch (error) {}
      }
      function move(moveEvent) {
        var delta = ((Number(moveEvent.clientX) || 0) - startX)
          * spec.direction;
        _project(name, model.setWidth(name, startWidth + delta), true);
      }
      var unbindPointer = function () {};
      function finish() {
        unbindPointer();
        if (shell) shell.classList.remove('orch-panel-resizing');
        _persist();
      }
      unbindPointer = bindOrchestrationPointerSession({
        pointerId: event.pointerId, moveTarget: doc, pointerTarget: doc,
        captureTarget: handle, window: win, onMove: move, onEnd: finish,
      });
    });
    handle.addEventListener('keydown', function (event) {
      var bounds = model.bounds(name);
      var step = event.shiftKey ? 32 : 12;
      var value = null;
      var width = snapshot()[name];
      if (event.key === 'ArrowLeft') {
        value = width - step * spec.direction;
      } else if (event.key === 'ArrowRight') {
        value = width + step * spec.direction;
      } else if (event.key === 'Home') {
        value = bounds.min;
      } else if (event.key === 'End') {
        value = bounds.max;
      }
      if (value == null) return;
      event.preventDefault();
      setWidth(name, value);
    });
    handle.addEventListener('dblclick', function () { reset(name); });
  }

  function bind() {
    if (bound || !_shell()) return false;
    bound = true;
    var stored = _load();
    model.hydrate(stored);
    sync();
    _bindHandle('palette');
    _bindHandle('inspector');
    if (win.addEventListener) win.addEventListener('resize', sync);
    return true;
  }

  return {
    bind: bind,
    sync: sync,
    setWidth: setWidth,
    reset: reset,
    snapshot: snapshot,
  };
}

/* ===== migrated source: orchestration-palette-presentation.js ===== */
/* Pure backend catalogue and safe HTML projection for the node palette. */

function createOrchestrationPalettePresentation(options) {
  options = options || {};
  var escape = options.escape || function (value) { return String(value || ''); };
  var translate = options.translate || function (key) { return key; };
  var icons = options.icons || {};
  var glyphs = options.glyphs || {};

  function _roles() {
    return typeof options.roles === 'function' ? options.roles() : [];
  }

  function _controls() {
    return typeof options.controls === 'function' ? options.controls() : [];
  }

  function availability() {
    var state = typeof options.contractState === 'function'
      ? options.contractState() : null;
    if (state && typeof state === 'object') {
      return {
        ready: state.ready === true,
        settled: state.settled === true,
        failed: !!state.error,
      };
    }
    return {
      ready: typeof options.available !== 'function' || !!options.available(),
      settled: typeof options.settled === 'function' && !!options.settled(),
      failed: typeof options.error === 'function' && !!options.error(),
    };
  }

  function _localizedName(prefix, name, fallback) {
    var key = prefix + name;
    var value = translate(key);
    return value && value !== key ? value : fallback;
  }

  function _searchText(parts) {
    return parts.filter(function (value) { return value != null && value !== ''; })
      .join(' ').toLowerCase();
  }

  function chipKey(chip) {
    if (!chip || typeof chip.getAttribute !== 'function') return '';
    return [
      chip.getAttribute('data-ptype') || '',
      chip.getAttribute('data-prole') || '',
      chip.getAttribute('data-pkind') || '',
    ].join('\u0000');
  }

  function _controlHtml(control) {
    var label = _localizedName('orch.controlName.', control.kind, control.label);
    return '<div class="orch-chip orch-chip-ctrl" draggable="true" '
      + 'data-ptype="control" data-pkind="' + escape(control.kind) + '" '
      + 'data-palette-search="' + escape(_searchText([
        control.kind, control.label, label, control.blurb,
      ])) + '" '
      + 'role="button" tabindex="0" aria-label="'
      + escape(translate('orch.palette.add', { name: label })) + '" '
      + 'style="--chip-accent:' + escape(control.accent) + '" title="'
      + escape(control.blurb) + '">'
      + '<span class="orch-chip-glyph">' + (glyphs[control.glyph] || '') + '</span>'
      + '<span class="orch-chip-label">' + escape(label) + '</span></div>';
  }

  function _roleHtml(role) {
    var label = _localizedName('orch.roleName.', role.role, role.label);
    var src = typeof options.iconSrc === 'function' ? options.iconSrc(role.icon) : '';
    return '<div class="orch-chip orch-chip-role" draggable="true" '
      + 'data-ptype="role" data-prole="' + escape(role.role) + '" '
      + 'data-palette-search="' + escape(_searchText([
        role.role, role.label, label, role.blurb,
      ])) + '" '
      + 'role="button" tabindex="0" aria-label="'
      + escape(translate('orch.palette.add', { name: label })) + '" '
      + 'title="' + escape(role.blurb) + '">'
      + '<span class="orch-chip-ava"><img src="' + escape(src) + '" alt="" '
      + 'data-orch-palette-avatar></span>'
      + '<span class="orch-chip-label">' + escape(label) + '</span></div>';
  }

  function _shellHtml() {
    return '<div class="orch-sheet-head orch-m-only"><span>'
      + icons.plus + ' ' + escape(translate('orch.palette.agents')) + '</span>'
      + '<button type="button" class="orch-icon-btn" data-palette-close title="'
      + escape(translate('orch.tip.close')) + '" aria-label="'
      + escape(translate('orch.tip.close')) + '">' + icons.reject + '</button></div>'
      + '<div class="orch-m-only orch-sheet-hint">'
      + escape(translate('orch.palette.tapHint')) + '</div>';
  }

  function loadingHtml(state) {
    var unavailable = state.settled || state.failed;
    return _shellHtml() + '<div class="orch-pal-loading" role="'
      + (unavailable ? 'alert' : 'status') + '">'
      + '<div class="orch-pal-loading-copy">'
      + (unavailable ? ''
        : '<span class="orch-pal-loading-dot" aria-hidden="true"></span>')
      + escape(translate(unavailable
        ? 'orch.palette.unavailable' : 'orch.palette.loading')) + '</div>'
      + (unavailable && typeof options.onRetry === 'function'
        ? '<button type="button" class="orch-btn orch-btn-ghost orch-pal-retry" '
          + 'data-orch-contract-retry>'
          + escape(translate('orch.palette.retry')) + '</button>' : '')
      + '</div>';
  }

  function readyHtml(query) {
    var html = _shellHtml()
      + '<div class="orch-pal-search"><input type="search" '
      + 'data-orch-palette-search autocomplete="off" spellcheck="false" value="'
      + escape(query) + '" placeholder="'
      + escape(translate('orch.palette.search')) + '" aria-label="'
      + escape(translate('orch.palette.search')) + '"></div>'
      + '<div class="orch-pal-section" data-palette-category-label="control">'
      + escape(translate('orch.palette.control'))
      + '</div><div class="orch-pal-grid" data-palette-category="control">';
    _controls().forEach(function (control) { html += _controlHtml(control); });
    html += '</div><div class="orch-pal-section" '
      + 'data-palette-category-label="group">'
      + escape(translate('orch.palette.group'))
      + '</div><div class="orch-pal-grid" data-palette-category="group">'
      + '<div class="orch-chip orch-chip-ctrl orch-chip-group" draggable="true" '
      + 'data-ptype="subflow" data-prole="general" role="button" tabindex="0" '
      + 'data-palette-search="' + escape(_searchText([
        'subflow', 'general', translate('orch.group.chip'),
        translate('orch.group.chipTip'),
      ])) + '" '
      + 'aria-label="' + escape(translate('orch.palette.add', {
        name: translate('orch.group.chip'),
      })) + '" style="--chip-accent:#8b5cf6" title="'
      + escape(translate('orch.group.chipTip')) + '">'
      + '<span class="orch-chip-glyph">' + (glyphs.group || '') + '</span>'
      + '<span class="orch-chip-label">' + escape(translate('orch.group.chip'))
      + '</span></div></div><div class="orch-pal-section" '
      + 'data-palette-category-label="agents">'
      + escape(translate('orch.palette.agents'))
      + '</div><div class="orch-pal-grid" data-palette-category="agents">';
    _roles().forEach(function (role) { html += _roleHtml(role); });
    return html + '</div><div class="orch-pal-empty" data-palette-empty '
      + 'role="status" aria-live="polite" hidden>'
      + escape(translate('orch.palette.noMatches'))
      + '</div><div class="orch-pal-foot">'
      + escape(translate('orch.palette.foot')) + '</div>';
  }

  return {
    availability: availability,
    chipKey: chipKey,
    loadingHtml: loadingHtml,
    readyHtml: readyHtml,
  };
}

/* ===== migrated source: orchestration-palette.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-palette.js — node-library view + input interactions

   Owns filtering, focus, retry, drag, click, keyboard and mobile-sheet
   activation. Backend catalogue/HTML projection lives in the presentation
   sibling. It emits only an add payload; graph state remains elsewhere.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationPaletteView(options) {
  options = options || {};
  var presentation = options.presentation
    || createOrchestrationPalettePresentation(options);
  var query = '';
  var focusedChipKey = '';

  function _applyFilter(element) {
    var normalized = query.trim().toLowerCase();
    var visibleCount = 0;
    element.querySelectorAll('[data-palette-search]').forEach(function (chip) {
      var text = chip.getAttribute('data-palette-search') || '';
      chip.hidden = !!normalized && text.indexOf(normalized) === -1;
      if (!chip.hidden) visibleCount += 1;
    });
    element.querySelectorAll('[data-palette-category]').forEach(function (grid) {
      var visible = Array.prototype.some.call(
        grid.querySelectorAll('[data-palette-search]'),
        function (chip) { return !chip.hidden; }
      );
      grid.hidden = !visible;
      var name = grid.getAttribute('data-palette-category');
      var heading = element.querySelector(
        '[data-palette-category-label="' + name + '"]');
      if (heading) heading.hidden = !visible;
    });
    var empty = element.querySelector('[data-palette-empty]');
    if (empty) empty.hidden = visibleCount !== 0;
    return visibleCount;
  }

  function _bindSearch(element, restoreFocus, onFilter) {
    var input = element.querySelector('[data-orch-palette-search]');
    if (!input) return;
    function update() {
      query = input.value || '';
      _applyFilter(element);
      if (typeof onFilter === 'function') onFilter();
    }
    input.addEventListener('input', update);
    input.addEventListener('search', update);
    _applyFilter(element);
    if (restoreFocus && typeof input.focus === 'function') input.focus();
  }

  function render(element) {
    if (!element) return;
    var active = element.ownerDocument && element.ownerDocument.activeElement;
    var restoreSearchFocus = !!(active && active.hasAttribute
      && active.hasAttribute('data-orch-palette-search'));
    if (active && element.contains(active) && active.classList
        && active.classList.contains('orch-chip')) {
      focusedChipKey = presentation.chipKey(active);
    }
    var availability = presentation.availability();
    if (!availability.ready) {
      var unavailable = availability.settled || availability.failed;
      element.setAttribute('aria-busy', unavailable ? 'false' : 'true');
      element.innerHTML = presentation.loadingHtml(availability);
      var loadingClose = element.querySelector('[data-palette-close]');
      if (loadingClose && typeof options.closeMobile === 'function') {
        loadingClose.addEventListener('click', options.closeMobile);
      }
      var retry = element.querySelector('[data-orch-contract-retry]');
      if (retry) retry.addEventListener('click', function () {
        retry.disabled = true;
        element.setAttribute('aria-busy', 'true');
        var result;
        try {
          result = options.onRetry();
        } catch (error) {
          result = null;
        }
        function settled() { render(element); }
        if (result && typeof result.then === 'function') {
          result.then(settled, settled);
        } else {
          settled();
        }
      });
      return;
    }
    element.removeAttribute('aria-busy');
    element.innerHTML = presentation.readyHtml(query);
    var keyboard;
    _bindSearch(element, restoreSearchFocus, function () {
      if (keyboard) keyboard.sync();
    });
    keyboard = createOrchestrationRovingItemsController({
      root: element,
      selector: '.orch-chip',
      entry: element.querySelector('[data-orch-palette-search]'),
    });
    var focusedChip = Array.prototype.filter.call(
      element.querySelectorAll('.orch-chip'), function (chip) {
        return !chip.hidden && presentation.chipKey(chip) === focusedChipKey;
      }
    )[0] || null;
    if (!restoreSearchFocus && focusedChip) {
      keyboard.sync(focusedChip);
      focusedChip.focus({ preventScroll: true });
    }

    var close = element.querySelector('[data-palette-close]');
    if (close && typeof options.closeMobile === 'function') {
      close.addEventListener('click', options.closeMobile);
    }
    element.querySelectorAll('[data-orch-palette-avatar]').forEach(function (image) {
      image.addEventListener('error', function () {
        image.style.display = 'none';
      }, { once: true });
    });
    element.querySelectorAll('.orch-chip').forEach(function (chip) {
      var dragged = false;
      function payload() {
        return {
          ptype: chip.getAttribute('data-ptype'),
          role: chip.getAttribute('data-prole') || '',
          kind: chip.getAttribute('data-pkind') || '',
        };
      }
      function add() {
        if (typeof options.onAdd === 'function') options.onAdd(payload());
        if (typeof options.isMobile === 'function' && options.isMobile()
            && typeof options.closeMobile === 'function') {
          options.closeMobile();
        }
      }
      chip.addEventListener('dragstart', function (event) {
        dragged = true;
        if (event.dataTransfer) {
          event.dataTransfer.setData('text/orch', JSON.stringify(payload()));
          event.dataTransfer.effectAllowed = 'copy';
        }
      });
      chip.addEventListener('dragend', function () {
        setTimeout(function () { dragged = false; }, 0);
      });
      chip.addEventListener('click', function () {
        if (!dragged) add();
      });
      chip.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        add();
      });
    });
  }

  return {
    query: function () { return query; },
    render: render,
  };
}

/* ===== migrated source: orchestration-write-recovery.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-write-recovery.js — stale-definition recovery flow

   A save conflict is not resolved by a toast. This controller owns the one
   user choice between keeping the local draft, exporting it before loading
   the server version, or deliberately discarding it. Document lifecycle,
   storage transport and file export remain injected ports.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationWriteRecoveryController(options) {
  options = options || {};
  var flights = createOrchestrationSingleFlight();

  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function currentId() {
    return typeof options.currentId === 'function'
      ? options.currentId() : null;
  }

  function stillCurrent(conflict, id) {
    if (currentId() !== id) return false;
    return typeof options.isCurrent !== 'function'
      || options.isCurrent(conflict);
  }

  async function run(conflict) {
    conflict = conflict && typeof conflict === 'object' ? conflict : {};
    var id = currentId();
    if (!id || (conflict.operation && conflict.operation !== 'replace')) {
      return { action: 'keep', recovered: false };
    }
    if (typeof options.choose !== 'function') {
      return { action: 'keep', recovered: false };
    }

    var action = await options.choose({
      title: translate('orch.conflict.title'),
      message: translate('orch.conflict.message'),
      dismissValue: 'keep',
      liveCheck: function () { return stillCurrent(conflict, id); },
      options: [
        {
          value: 'export_reload',
          label: translate('orch.conflict.exportReload'),
          subtitle: translate('orch.conflict.exportReloadHint'),
          accent: true,
        },
        {
          value: 'reload',
          label: translate('orch.conflict.reload'),
          subtitle: translate('orch.conflict.reloadHint'),
        },
        {
          value: 'keep',
          label: translate('orch.conflict.keep'),
          subtitle: translate('orch.conflict.keepHint'),
        },
      ],
    });

    if (action !== 'reload' && action !== 'export_reload') {
      return { action: 'keep', recovered: false };
    }
    if (!stillCurrent(conflict, id)) {
      return { action: action, recovered: false, stale: true };
    }
    if (action === 'export_reload') {
      if (typeof options.exportDraft !== 'function') {
        return { action: action, recovered: false };
      }
      var exported = await options.exportDraft();
      if (!exported) return { action: action, recovered: false };
    }
    if (typeof options.loadLatest !== 'function') {
      return { action: action, recovered: false };
    }
    var loaded = await options.loadLatest(id);
    return { action: action, recovered: !!loaded, loaded: loaded || null };
  }

  function open(conflict) {
    return flights.share('recovery', function () { return run(conflict); });
  }

  return {
    open: open,
    pending: function () { return flights.pending('recovery'); },
  };
}

/* ===== migrated source: orchestration-contract-section-registry-validation.js ===== */
/* Validation for the backend-published authoring/runtime section registry. */

function _validateOrchestrationContractSectionRegistry(
  sectionRegistry, missing
) {
  // Optional for rolling deploys against a pre-registry backend. Once
  // present, the runtime projection must be a safe subset of authoring.
  if (sectionRegistry == null) return;
  var authoring = sectionRegistry.authoring;
  if (!_orchestrationContractRecord(sectionRegistry)
      || !Array.isArray(authoring)
      || !Array.isArray(sectionRegistry.runtime)
      || !sectionRegistry.runtime.length
      || sectionRegistry.runtime.some(function (name) {
        return typeof name !== 'string'
          || !/^[A-Za-z][A-Za-z0-9]*$/.test(name)
          || authoring.indexOf(name) < 0;
      })) {
    missing.push('contractSections');
  }

  var rolling = sectionRegistry.rollingOptionalFields;
  if (rolling == null) return;
  var expected = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .rollingOptionalFields;
  if (!_orchestrationContractRecord(rolling)) {
    missing.push('contractSections.rollingOptionalFields');
    return;
  }
  Object.keys(rolling).forEach(function (sectionName) {
    var path = 'contractSections.rollingOptionalFields.' + sectionName;
    if (!/^[A-Za-z][A-Za-z0-9]*$/.test(sectionName)
        || !Array.isArray(authoring)
        || authoring.indexOf(sectionName) < 0) {
      missing.push(path);
    } else {
      _orchestrationRequireStringVocabulary(
        rolling[sectionName], path, missing);
    }
  });
  Object.keys(expected).forEach(function (sectionName) {
    if (rolling[sectionName] == null) return;
    _orchestrationRequireArray(rolling[sectionName],
      'contractSections.rollingOptionalFields.' + sectionName,
      missing, expected[sectionName]);
  });
}

/* ===== migrated source: orchestration-authoring-section-validation.js ===== */
/* Leaf schema checks for backend-owned authoring contract sections. */

function _validateInspectionAuthoringSection(section, missing) {
  var defaults = orchestrationCompatibilityContract('inspectionContract');
  if (!Array.isArray(section.diagnosticSeverities)
      || !section.diagnosticSeverities.length) {
    missing.push('inspectionContract.diagnosticSeverities');
  }
  _orchestrationRequireArray(section.diagnosticFields,
    'inspectionContract.diagnosticFields', missing,
    defaults.diagnosticFields);
  _orchestrationRequireString(section.diagnosticPathFormat,
    'inspectionContract.diagnosticPathFormat', missing);
}

function _validateDefinitionListAuthoringSection(section, missing) {
  var defaults = orchestrationCompatibilityContract(
    'definitionListContract');
  _orchestrationRequireArray(section.itemFields,
    'definitionListContract.itemFields', missing,
    defaults.itemFields);
  if (!Array.isArray(section.orderBy)) {
    missing.push('definitionListContract.orderBy');
  }
  if (typeof section.definitionIncluded !== 'boolean') {
    missing.push('definitionListContract.definitionIncluded');
  }
}

function _validateDefinitionEntryAuthoringSection(section, missing) {
  var defaults = orchestrationCompatibilityContract(
    'definitionEntryContract');
  _orchestrationRequireArray(section.fields,
    'definitionEntryContract.fields', missing, defaults.fields);
  _orchestrationRequireString(section.versionField,
    'definitionEntryContract.versionField', missing);
  if (typeof section.versionField === 'string' && section.versionField
      && Array.isArray(section.fields)
      && section.fields.indexOf(section.versionField) < 0) {
    missing.push('definitionEntryContract.fields.' + section.versionField);
  }
  if (typeof section.inspectionIncludedOnWrite !== 'boolean') {
    missing.push('definitionEntryContract.inspectionIncludedOnWrite');
  }
  if (typeof section.versionRequiredOnWrite !== 'boolean') {
    missing.push('definitionEntryContract.versionRequiredOnWrite');
  }
}

function _validateExecutionAuthoringSection(section, missing) {
  ORCHESTRATION_AUTHORING_VALIDATION_METADATA.executionOptions.arrayFields
    .forEach(function (axis) {
      if (!Array.isArray(section[axis]) || !section[axis].length) {
        missing.push('executionOptions.' + axis);
      }
    });
}

function _validateNodeDefaultsAuthoringSection(section, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA.nodeDefaults;
  metadata.objectFields.forEach(function (field) {
    if (!_orchestrationContractRecord(section[field])) {
      missing.push('nodeDefaults.' + field);
    }
  });
  var blank = section.blankSubflow || {};
  metadata.blankSubflowArrayFields.forEach(function (field) {
    if (!Array.isArray(blank[field])) {
      missing.push('nodeDefaults.blankSubflow.' + field);
    }
  });
}

function _validateIoAuthoringSection(section, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA.ioContract;
  if (!Array.isArray(section.types) || !section.types.length) {
    missing.push('ioContract.types');
  }
  if (!_orchestrationContractRecord(section.defaultOutput)) {
    missing.push('ioContract.defaultOutput');
  }
  _orchestrationRequirePositiveInteger(section.maxPorts,
    'ioContract.maxPorts', missing);
  _orchestrationRequireString(section.startRef, 'ioContract.startRef', missing);
  if (section.failureCodes != null) {
    if (!_orchestrationContractRecord(section.failureCodes)) {
      missing.push('ioContract.failureCodes');
    } else Object.keys(metadata.failureCodes).forEach(function (name) {
      if (section.failureCodes[name] !== metadata.failureCodes[name]) {
        missing.push('ioContract.failureCodes.' + name);
      }
    });
  }
}

/* ===== migrated source: orchestration-field-option-validation.js ===== */
/* Backend FieldSpec option-shape validation shared by authoring registries. */

function _validateFieldSpecOptions(field, fieldPath, missing, metadata) {
  (field.options || []).forEach(function (option, optionIndex) {
    var optionPath = fieldPath + '.options.' + optionIndex;
    if (!_orchestrationContractRecord(option)) {
      missing.push(optionPath); return;
    }
    metadata.optionRequiredStringFields.forEach(function (name) {
      _orchestrationRequireString(
        option[name], optionPath + '.' + name, missing);
    });
    metadata.optionBooleanFields.forEach(function (name) {
      if (option[name] != null && typeof option[name] !== 'boolean') {
        missing.push(optionPath + '.' + name);
      }
    });
  });
}

/* ===== migrated source: orchestration-field-value-contract-validation.js ===== */
/* Semantic validation for the backend-owned FieldValue contract. */

function _validateFieldValueAuthoringSection(section, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .fieldValueContract;
  if (section.optionalEmpty !== metadata.optionalEmpty) {
    missing.push('fieldValueContract.optionalEmpty');
  }
  if (section.failureCodes != null) {
    if (!_orchestrationContractRecord(section.failureCodes)) {
      missing.push('fieldValueContract.failureCodes');
    } else Object.keys(metadata.failureCodes).forEach(function (name) {
      if (section.failureCodes[name] !== metadata.failureCodes[name]) {
        missing.push('fieldValueContract.failureCodes.' + name);
      }
    });
  }
  if (!_orchestrationContractRecord(section.kinds)) {
    missing.push('fieldValueContract.kinds'); return;
  }
  metadata.kinds.forEach(function (kind) {
    var spec = section.kinds[kind];
    if (!_orchestrationContractRecord(spec)) {
      missing.push('fieldValueContract.kinds.' + kind);
    } else metadata.kindRequiredStringFields.forEach(function (field) {
      _orchestrationRequireString(spec[field],
        'fieldValueContract.kinds.' + kind + '.' + field, missing);
    });
  });
}

/* ===== migrated source: orchestration-authoring-policy-validation.js ===== */
/* Focused semantic validators for backend-owned Studio editor policies. */
function _validateFieldSpecList(fields, path, missing) {
  if (!Array.isArray(fields)) { missing.push(path); return; }
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA.fieldSpec;
  fields.forEach(function (field, index) {
    var fieldPath = path + '.' + index;
    if (!_orchestrationContractRecord(field)) {
      missing.push(fieldPath); return;
    }
    metadata.requiredStringFields.forEach(function (name) {
      _orchestrationRequireString(field[name], fieldPath + '.' + name, missing);
    });
    metadata.positiveIntegerFields.forEach(function (name) {
      if (field[name] != null) _orchestrationRequirePositiveInteger(
        field[name], fieldPath + '.' + name, missing);
    });
    metadata.arrayFields.forEach(function (name) {
      _orchestrationRequireOptional(
        field[name], fieldPath + '.' + name, missing, Array.isArray);
    });
    metadata.objectFields.forEach(function (name) {
      _orchestrationRequireOptional(field[name], fieldPath + '.' + name,
        missing, _orchestrationContractRecord);
    });
    _validateFieldSpecOptions(field, fieldPath, missing, metadata);
  });
}

function _validateFieldSpecRegistry(section, path, missing) {
  Object.keys(section).forEach(function (name) {
    _validateFieldSpecList(section[name], path + '.' + name, missing);
  });
}

function _validateDefinitionWriteAuthoringSection(section, missing) {
  var defaults = orchestrationCompatibilityContract(
    'definitionWriteContract');
  _orchestrationRequireStringFields(section,
    ORCHESTRATION_AUTHORING_VALIDATION_METADATA.definitionWriteContract
      .requiredStringFields, 'definitionWriteContract', missing);
  _orchestrationRequireArray(section.operations,
    'definitionWriteContract.operations', missing, defaults.operations);
  _orchestrationRequirePositiveInteger(section.conflictStatus,
    'definitionWriteContract.conflictStatus', missing);
  if (section.conflictFields != null) {
    _orchestrationRequireFieldSpecs(section.conflictFields, {
      format: 'string', reason: 'string', operation: 'string',
      expectedUpdatedAt: 'non_negative_integer',
      currentUpdatedAt: 'non_negative_integer',
    }, 'definitionWriteContract.conflictFields', missing);
    var fields = _orchestrationContractRecord(section.conflictFields)
      ? section.conflictFields : {};
    if (fields.format && fields.format.name !== 'format') {
      missing.push('definitionWriteContract.conflictFields');
    }
  }
}

function _validatePersonaAuthoringSection(section, missing) {
  var fields = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .personas.requiredStringFields;
  Object.keys(section).forEach(function (role) {
    var persona = section[role];
    if (!_orchestrationContractRecord(persona)) {
      missing.push('personas.' + role); return;
    }
    fields.forEach(function (field) {
      _orchestrationRequireString(persona[field],
        'personas.' + role + '.' + field, missing);
    });
  });
}

function _validateDefaultEmitsAuthoringSection(section, missing) {
  Object.keys(section).forEach(function (role) {
    _orchestrationRequireString(section[role],
      'defaultEmits.' + role, missing);
  });
}

function _validateAuthoringFieldKinds(body, missing) {
  var kinds = Array.isArray(body.kinds) ? body.kinds : [];
  var valueKinds = _orchestrationContractRecord(body.fieldValueContract)
    && _orchestrationContractRecord(body.fieldValueContract.kinds)
    ? Object.keys(body.fieldValueContract.kinds) : [];
  if (kinds.some(function (kind, index) {
    return typeof kind !== 'string' || !kind
      || kinds.indexOf(kind) !== index || valueKinds.indexOf(kind) < 0;
  }) || valueKinds.some(function (kind) { return kinds.indexOf(kind) < 0; })) {
    missing.push('kinds');
  }
  function validateList(fields, path) {
    if (!Array.isArray(fields)) return;
    fields.forEach(function (field, index) {
      if (_orchestrationContractRecord(field) && typeof field.kind === 'string'
          && field.kind && kinds.indexOf(field.kind) < 0) {
        missing.push(path + '.' + index + '.kind');
      }
    });
  }
  validateList(body.generic, 'generic');
  ORCHESTRATION_AUTHORING_VALIDATION_METADATA.fieldSpecRegistrySections
    .forEach(function (sectionName) {
    var section = body[sectionName];
    if (!_orchestrationContractRecord(section)) return;
    Object.keys(section).forEach(function (name) {
      validateList(section[name], sectionName + '.' + name);
    });
    });
}
/* ===== migrated source: orchestration-authoring-default-validation.js ===== */
/* Validate backend-authored authoring and runtime node defaults. */
function _validateNodeRuntimeDefaultsAuthoringSection(section, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .runtimeSections.nodeRuntimeDefaults;

  function validateRecord(record, path, fields) {
    if (!_orchestrationContractRecord(record)) {
      missing.push(path); return;
    }
    _orchestrationRequireStringFields(record,
      fields.requiredStringFields, path, missing);
    fields.requiredPositiveIntegerFields.forEach(function (field) {
      _orchestrationRequirePositiveInteger(
        record[field], path + '.' + field, missing);
    });
  }

  metadata.requiredObjectFields.forEach(function (field) {
    if (!_orchestrationContractRecord(section[field])) {
      missing.push('nodeRuntimeDefaults.' + field);
    }
  });
  validateRecord(section.role, 'nodeRuntimeDefaults.role', metadata.role);
  validateRecord(section.subflow,
    'nodeRuntimeDefaults.subflow', metadata.subflow);
  Object.keys(metadata.controls).forEach(function (kind) {
    validateRecord((section.controls || {})[kind],
      'nodeRuntimeDefaults.controls.' + kind, metadata.controls[kind]);
  });
}
function _validateAuthoringNodeDefaultAxes(body, missing) {
  var metadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA.nodeDefaults;
  var options = body.executionOptions || {};
  var defaults = body.nodeDefaults || {};
  function validate(record, path, axes) {
    if (!_orchestrationContractRecord(record)) return;
    Object.keys(axes || {}).forEach(function (field) {
      var choices = options[axes[field]];
      if (!Array.isArray(choices) || choices.indexOf(record[field]) < 0)
        missing.push(path + '.' + field);
    });
  }

  validate(defaults.genericRole, 'nodeDefaults.genericRole',
    metadata.roleExecutionAxes);
  Object.keys(defaults.roles || {}).forEach(function (role) {
    validate(defaults.roles[role], 'nodeDefaults.roles.' + role,
      metadata.roleExecutionAxes);
  });
  validate(defaults.subflow, 'nodeDefaults.subflow',
    metadata.subflowExecutionAxes);
  var runtimeDefaults = body.nodeRuntimeDefaults || {};
  var runtimeMetadata = ORCHESTRATION_AUTHORING_VALIDATION_METADATA
    .runtimeSections.nodeRuntimeDefaults;
  validate(runtimeDefaults.role, 'nodeRuntimeDefaults.role',
    runtimeMetadata.roleExecutionAxes);
  validate(runtimeDefaults.subflow, 'nodeRuntimeDefaults.subflow',
    runtimeMetadata.subflowExecutionAxes);
}

/* ===== migrated source: orchestration-authoring-catalogue-validation.js ===== */
/* Cross-section closure checks for the backend-owned authoring catalogue. */

function _authoringRegistryMatches(names, registry, path, missing) {
  if (!_orchestrationContractRecord(registry)) return;
  names.forEach(function (name) {
    if (typeof name === 'string'
        && !Object.prototype.hasOwnProperty.call(registry, name)) {
      missing.push(path + '.' + name);
    }
  });
  Object.keys(registry).forEach(function (name) {
    if (names.indexOf(name) < 0) missing.push(path + '.' + name);
  });
}

function _authoringSafeNames(value, path, missing) {
  if (!_orchestrationRequireStringVocabulary(value, path, missing)) {
    return false;
  }
  if (value.some(function (name) {
    return !/^[A-Za-z][A-Za-z0-9_]*$/.test(name);
  })) {
    missing.push(path);
    return false;
  }
  return true;
}

function _validateAuthoringRoleLinks(body, missing) {
  if (!_authoringSafeNames(body.roleNames, 'roleNames', missing)) return;
  var roleNames = body.roleNames;
  ['roles', 'personas', 'defaultEmits'].forEach(function (path) {
    _authoringRegistryMatches(roleNames, body[path], path, missing);
  });
  var defaults = body.nodeDefaults && body.nodeDefaults.roles;
  _authoringRegistryMatches(roleNames, defaults, 'nodeDefaults.roles', missing);
  var tiers = body.executionOptions && body.executionOptions.tiers;
  var emits = body.executionOptions && body.executionOptions.emits;
  roleNames.forEach(function (role) {
    var persona = body.personas && body.personas[role];
    var node = defaults && defaults[role];
    var emit = body.defaultEmits && body.defaultEmits[role];
    if (persona && (tiers || []).indexOf(persona.tier) < 0) {
      missing.push('personas.' + role + '.tier');
    }
    if (persona && node && persona.tier !== node.tier) {
      missing.push('nodeDefaults.roles.' + role + '.tier');
    }
    if (typeof emit !== 'string' || (emits || []).indexOf(emit) < 0) {
      missing.push('defaultEmits.' + role);
    }
  });
}

function _validateAuthoringControlLinks(body, missing) {
  if (!_orchestrationContractRecord(body.controls)) return;
  var names = Object.keys(body.controls);
  if (!names.length || names.some(function (name) {
    return !/^[A-Za-z][A-Za-z0-9_]*$/.test(name);
  })) missing.push('controls');
  names.forEach(function (name) {
    var spec = body.controls[name];
    if (!_orchestrationContractRecord(spec)
        || typeof spec.single !== 'boolean') {
      missing.push('controls.' + name + '.single');
    }
  });
  _authoringRegistryMatches(
    names, body.controlSchemas, 'controlSchemas', missing);
  _authoringRegistryMatches(
    names, body.nodeDefaults && body.nodeDefaults.controls,
    'nodeDefaults.controls', missing);
}

function _validateAuthoringIoLinks(body, missing) {
  _authoringSafeNames(body.builtins, 'builtins', missing);
  var contractTypes = body.ioContract && body.ioContract.types;
  var contractValid = _authoringSafeNames(
    contractTypes, 'ioContract.types', missing);
  var output = body.ioContract && body.ioContract.defaultOutput;
  if (!_orchestrationContractRecord(output)) return;
  ORCHESTRATION_AUTHORING_VALIDATION_METADATA.ioContract
    .defaultOutputStringFields.forEach(function (field) {
      _orchestrationRequireString(
        output[field], 'ioContract.defaultOutput.' + field, missing);
      if (contractValid && contractTypes.indexOf(output[field]) < 0) {
        missing.push('ioContract.defaultOutput.' + field);
      }
    });
}

function _validateAuthoringRuntimeLinks(body, missing) {
  var eventTypes = body.eventContract && body.eventContract.types;
  var runStatuses = body.runContract && body.runContract.statuses;
  if (!_orchestrationContractRecord(eventTypes)
      || !Array.isArray(runStatuses)) return;
  Object.keys(eventTypes).forEach(function (type) {
    var runStatus = eventTypes[type] && eventTypes[type].runStatus;
    if (runStatus && runStatuses.indexOf(runStatus) < 0) {
      missing.push('eventContract.types.' + type + '.runStatus');
    }
  });
}

function _validateAuthoringCatalogueLinks(body, missing) {
  _validateAuthoringRoleLinks(body, missing);
  _validateAuthoringControlLinks(body, missing);
  _validateAuthoringNodeDefaultAxes(body, missing);
  _validateAuthoringIoLinks(body, missing);
  _validateAuthoringRuntimeLinks(body, missing);
}
/* ===== migrated source: orchestration-authoring-contract-validation.js ===== */
/* Compose leaf validators into the backend authoring catalogue gate. */


var ORCHESTRATION_AUTHORING_SECTION_VALIDATORS = Object.freeze({
  roles: function (section, missing) {
    _validateFieldSpecRegistry(section, 'roles', missing);
  },
  controlSchemas: function (section, missing) {
    _validateFieldSpecRegistry(section, 'controlSchemas', missing);
  },
  personas: _validatePersonaAuthoringSection,
  defaultEmits: _validateDefaultEmitsAuthoringSection,
  inspectionContract: _validateInspectionAuthoringSection,
  definitionListContract: _validateDefinitionListAuthoringSection,
  definitionEntryContract: _validateDefinitionEntryAuthoringSection,
  executionOptions: _validateExecutionAuthoringSection,
  nodeDefaults: _validateNodeDefaultsAuthoringSection,
  fieldValueContract: _validateFieldValueAuthoringSection,
  definitionWriteContract: _validateDefinitionWriteAuthoringSection,
  ioContract: _validateIoAuthoringSection,
});


function _orchestrationAuthoringContractProblems(body) {
  if (!body || typeof body !== 'object') return ['contract'];
  var missing = [];
  ORCHESTRATION_AUTHORING_OBJECT_SECTIONS.forEach(function (field) {
    var section = body[field];
    if (!_orchestrationContractRecord(section)) {
      missing.push(field);
      return;
    }
    // Runtime-section validators are owned by the lazy typed orchestration
    // domain. They do not exist while the main ESM graph is evaluating, so a
    // top-level Object.assign would either ReferenceError at boot or freeze an
    // empty snapshot forever. Resolve them when the authoring contract is
    // actually read, after the domain loader has installed its owner.
    var runtimeValidators = ORCHESTRATION_RUNTIME_SECTION_VALIDATORS || {};
    var validate = runtimeValidators[field]
      || ORCHESTRATION_AUTHORING_SECTION_VALIDATORS[field];
    if (validate) validate(section, missing);
  });
  if (!_orchestrationContractRecord(body.controls)) missing.push('controls');
  ['roleNames', 'generic', 'kinds'].forEach(function (field) {
    if (!Array.isArray(body[field]) || !body[field].length) missing.push(field);
  });
  _validateFieldSpecList(body.generic, 'generic', missing);
  _validateAuthoringFieldKinds(body, missing);
  _validateAuthoringCatalogueLinks(body, missing);
  _orchestrationRequireString(body.schema, 'schema', missing);
  _validateOrchestrationContractSectionRegistry(
    body.contractSections, missing);

  Object.keys(ORCHESTRATION_AUTHORING_WIRE_SECTIONS).forEach(function (field) {
    var nested = body[field];
    if (_orchestrationContractRecord(nested)) {
      var wire = inspectOrchestrationWireFormat(
        ORCHESTRATION_AUTHORING_WIRE_SECTIONS[field], nested);
      if (!wire.supported) {
        missing.push(field + '.' + (wire.identityField || 'format'));
      }
    }
  });
  return missing;
}
runtimeScope._orchestrationAuthoringContractProblems =
  _orchestrationAuthoringContractProblems;
if (typeof orchestrationRegistry !== 'undefined') {
  orchestrationRegistry._orchestrationAuthoringContractProblems =
    _orchestrationAuthoringContractProblems;
}

/* ===== migrated source: orchestration-diagnostic-target.js ===== */
/* Pure backend JSON-Pointer → Studio navigation target projection. */

function _decodeOrchestrationDiagnosticPointer(path) {
  if (path === '') return [];
  if (typeof path !== 'string' || path.charAt(0) !== '/') return null;
  var tokens = path.slice(1).split('/');
  if (tokens.some(function (token) { return /~(?:[^01]|$)/.test(token); })) {
    return null;
  }
  return tokens.map(function (token) {
    return token.replace(/~1/g, '/').replace(/~0/g, '~'); });
}

function _orchestrationDiagnosticIndex(token, values) {
  if (!/^(0|[1-9]\d*)$/.test(String(token == null ? '' : token))) return -1;
  var index = Number(token);
  return Number.isSafeInteger(index) && index < values.length ? index : -1;
}

function _orchestrationDiagnosticNodeIdentity(nodes, index) {
  var node = nodes[index];
  var id = node && typeof node.id === 'string' ? node.id : '';
  var count = id ? nodes.filter(function (candidate) {
    return candidate && candidate.id === id;
  }).length : 0;
  return { node: node, id: id, navigable: count === 1 };
}

function _orchestrationDiagnosticField(tokens) {
  if (!tokens.length) return null;
  if (tokens[0] === 'name' || tokens[0] === 'role') {
    return { kind: 'param', key: tokens[0] };
  }
  if (tokens[0] !== 'params') return null;
  if (tokens[1] !== 'io') {
    return tokens[1] ? { kind: 'param', key: tokens[1] } : null;
  }
  var side = tokens[2];
  if (side !== 'inputs' && side !== 'outputs') {
    return { kind: 'io-section' };
  }
  var indexToken = tokens[3];
  var canonicalIndex = /^(0|[1-9]\d*)$/.test(
    String(indexToken == null ? '' : indexToken));
  var index = canonicalIndex ? Number(indexToken) : NaN;
  var key = tokens[4];
  if (!Number.isSafeInteger(index) || !key) {
    return { kind: 'io-section', side: side };
  }
  return { kind: 'io', side: side, index: index, key: key };
}

function resolveOrchestrationDiagnosticTarget(diagnostic, definition) {
  var tokens = _decodeOrchestrationDiagnosticPointer(
    diagnostic && diagnostic.path || '');
  if (!tokens) return null;
  var cursor = definition && typeof definition === 'object' ? definition : {};
  var groups = [];
  var offset = 0;

  while (tokens[offset] === 'nodes') {
    var nodes = Array.isArray(cursor.nodes) ? cursor.nodes : [];
    if (tokens[offset + 1] == null) {
      return { kind: 'document', groups: groups, field: null,
        path: diagnostic && diagnostic.path || '' };
    }
    var nodeIndex = _orchestrationDiagnosticIndex(tokens[offset + 1], nodes);
    if (nodeIndex < 0) return null;
    var identity = _orchestrationDiagnosticNodeIdentity(nodes, nodeIndex);
    var node = identity.node;
    var rest = tokens.slice(offset + 2);
    if (rest[0] === 'params' && rest[1] === 'definition'
        && node && node.params && node.params.definition
        && typeof node.params.definition === 'object' && rest.length > 2) {
      if (!identity.navigable) return {
        kind: 'node', id: identity.id, index: nodeIndex, groups: groups,
        navigable: false, field: null,
        path: diagnostic && diagnostic.path || '',
      };
      groups.push(identity.id);
      cursor = node.params.definition;
      offset += 4;
      continue;
    }
    return {
      kind: 'node', id: identity.id, index: nodeIndex, groups: groups,
      navigable: identity.navigable,
      field: identity.navigable ? _orchestrationDiagnosticField(rest) : null,
      path: diagnostic && diagnostic.path || '',
    };
  }

  if (tokens[offset] === 'edges') {
    var edges = Array.isArray(cursor.edges) ? cursor.edges : [];
    if (tokens[offset + 1] == null) {
      return { kind: 'document', groups: groups, field: null,
        path: diagnostic && diagnostic.path || '' };
    }
    var edgeIndex = _orchestrationDiagnosticIndex(tokens[offset + 1], edges);
    if (edgeIndex < 0) return null;
    return {
      kind: 'edge', index: edgeIndex, groups: groups,
      path: diagnostic && diagnostic.path || '',
    };
  }
  return {
    kind: 'document', groups: groups,
    field: tokens[offset] === 'name' ? { kind: 'document-name' } : null,
    path: diagnostic && diagnostic.path || '',
  };
}

function orchestrationDiagnosticTargetLabel(target, definition, translate) {
  var tr = typeof translate === 'function' ? translate : function (key) {
    return key;
  };
  if (!target) return tr('orch.issues.flowTarget');
  if (target.kind === 'edge') {
    return tr('orch.issues.edgeTarget', { n: target.index + 1 });
  }
  if (target.kind === 'document') return tr('orch.issues.flowTarget');
  var cursor = definition || {};
  target.groups.forEach(function (groupId) {
    var group = (cursor.nodes || []).filter(function (node) {
      return node.id === groupId;
    })[0];
    cursor = group && group.params && group.params.definition || {};
  });
  var node = (cursor.nodes || [])[target.index] || {};
  var label = node.name || node.id || tr('orch.issues.nodeTarget', {
    n: target.index + 1,
  });
  var field = target.field && target.field.key;
  return field ? label + ' · ' + field : label;
}

/* ===== migrated source: orchestration-diagnostic-index.js ===== */
/* Current-workspace diagnostic summaries derived from canonical targets. */

function createOrchestrationDiagnosticIndex(options) {
  options = options || {};
  var lastDiagnostics = null;
  var lastWorkspace = '';
  var cache = null;

  function _groups() {
    var value = typeof options.workspaceGroups === 'function'
      ? options.workspaceGroups() : [];
    return Array.isArray(value) ? value.filter(Boolean) : [];
  }

  function _summary() {
    return { errors: 0, warnings: 0, total: 0, nested: 0, messages: [] };
  }

  function _add(summary, diagnostic, nested) {
    var severity = diagnostic && diagnostic.severity === 'warning'
      ? 'warnings' : 'errors';
    summary[severity] += 1;
    summary.total += 1;
    if (nested) summary.nested += 1;
    var message = String(diagnostic && diagnostic.message || '');
    if (message && summary.messages.indexOf(message) < 0) {
      summary.messages.push(message);
    }
  }

  function _prefix(prefix, value) {
    if (prefix.length > value.length) return false;
    return prefix.every(function (groupId, index) {
      return value[index] === groupId;
    });
  }

  function _build(diagnostics, groups) {
    var result = {
      document: _summary(),
      nodes: Object.create(null),
      edges: Object.create(null),
    };
    var definition = typeof options.definition === 'function'
      ? options.definition() : null;
    diagnostics.forEach(function (diagnostic) {
      var target = resolveOrchestrationDiagnosticTarget(
        diagnostic, definition);
      if (!target || !_prefix(groups, target.groups || [])) return;
      if (target.groups.length > groups.length) {
        var groupId = target.groups[groups.length];
        result.nodes[groupId] = result.nodes[groupId] || _summary();
        _add(result.nodes[groupId], diagnostic, true);
      } else if (target.kind === 'node' && target.id
                 && target.navigable !== false) {
        result.nodes[target.id] = result.nodes[target.id] || _summary();
        _add(result.nodes[target.id], diagnostic, false);
      } else if (target.kind === 'edge') {
        result.edges[target.index] = result.edges[target.index] || _summary();
        _add(result.edges[target.index], diagnostic, false);
      } else {
        _add(result.document, diagnostic, false);
      }
    });
    return result;
  }

  function snapshot() {
    var diagnostics = typeof options.diagnostics === 'function'
      ? options.diagnostics() : [];
    diagnostics = Array.isArray(diagnostics) ? diagnostics : [];
    var groups = _groups();
    var workspace = groups.join('\u0000');
    if (cache && diagnostics === lastDiagnostics && workspace === lastWorkspace) {
      return cache;
    }
    lastDiagnostics = diagnostics;
    lastWorkspace = workspace;
    cache = _build(diagnostics, groups);
    return cache;
  }

  return {
    snapshot: snapshot,
    node: function (id) { return snapshot().nodes[id] || null; },
    edge: function (index) { return snapshot().edges[index] || null; },
    document: function () { return snapshot().document; },
    invalidate: function () { cache = null; },
  };
}

/* ===== migrated source: orchestration-issue-presentation.js ===== */
/* Safe, stateless issue-panel DOM projection. */

function createOrchestrationIssuePresentation(options) {
  options = options || {};
  var doc = options.document || document;

  function tr(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function resolve(diagnostic, definition) {
    var projector = typeof options.resolveTarget === 'function'
      ? options.resolveTarget : resolveOrchestrationDiagnosticTarget;
    return projector(diagnostic, definition);
  }
  function targetLabel(target, definition) {
    var projector = typeof options.targetLabel === 'function'
      ? options.targetLabel : orchestrationDiagnosticTargetLabel;
    return projector(target, definition, tr);
  }
  function element(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }
  function snapshot(state) {
    state = state || {};
    return Object.freeze({
      validation: String(state.validation || 'unknown'),
      errors: Object.freeze(Array.isArray(state.errors) ? state.errors.slice() : []),
      warnings: Object.freeze(
        Array.isArray(state.warnings) ? state.warnings.slice() : []),
      diagnostics: Object.freeze(Array.isArray(state.diagnostics)
        ? state.diagnostics.map(function (item) {
            return Object.freeze(Object.assign({}, item || {}));
          }) : []),
      contract: state.contract && typeof state.contract === 'object'
        ? Object.freeze(Object.assign({}, state.contract)) : null,
    });
  }
  function render(panel, current, definition) {
    if (!panel || !current) return false;
    panel.replaceChildren();
    var header = element('div', 'orch-issues-head');
    header.appendChild(element('strong', '', tr('orch.issues.title')));
    header.appendChild(element(
      'span', 'orch-issues-count',
      tr('orch.issues.counts', {
        errors: current.errors.length, warnings: current.warnings.length,
      })
    ));
    var closeButton = element('button', 'orch-issues-close', '×');
    closeButton.type = 'button';
    closeButton.setAttribute('data-orch-issues-close', '');
    closeButton.setAttribute('aria-label', tr('orch.tip.close'));
    header.appendChild(closeButton);
    panel.appendChild(header);

    if (!current.diagnostics.length) {
      var key = current.validation === 'valid'
        ? 'orch.issues.valid' : 'orch.issues.pending';
      panel.appendChild(element('div', 'orch-issues-empty', tr(key, {
        projection: current.contract && current.contract.projection || 'flow',
        nodes: current.contract && current.contract.nodes || 0,
      })));
      return true;
    }
    var list = element('div', 'orch-issues-list');
    current.diagnostics.forEach(function (diagnostic, index) {
      var severity = diagnostic.severity === 'warning' ? 'warning' : 'error';
      var button = element('button', 'orch-issue-item is-' + severity);
      button.type = 'button';
      button.setAttribute('data-orch-issue-index', String(index));
      button.appendChild(element('span', 'orch-issue-dot', ''));
      var copy = element('span', 'orch-issue-copy');
      var message = element(
        'span', 'orch-issue-message', String(diagnostic.message || ''));
      message.id = 'orchIssueMessage-' + index;
      button.setAttribute('data-orch-issue-message-id', message.id);
      copy.appendChild(message);
      copy.appendChild(element(
        'span', 'orch-issue-target',
        targetLabel(resolve(diagnostic, definition), definition)));
      button.appendChild(copy);
      list.appendChild(button);
    });
    panel.appendChild(list);
    return true;
  }

  return Object.freeze({ render: render, snapshot: snapshot });
}

/* ===== migrated source: orchestration-issue-navigator.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-issue-navigator.js — inspection issue list + navigation

   Consumes backend-authored JSON Pointer diagnostics. It never infers a
   target from human copy; graph selection, nested-workspace navigation and
   Inspector rendering stay behind injected commands.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationIssueNavigator(options) {
  options = options || {};
  var doc = options.document || document;
  var current = null;
  var open = false;
  var issueKeyboard = null;
  var presentation = options.presentation
    || createOrchestrationIssuePresentation(options);

  function _scrollBehavior() {
    var view = options.window || doc.defaultView;
    return view && typeof view.prefersReducedMotion === 'function'
      && view.prefersReducedMotion() ? 'auto' : 'smooth';
  }

  function resolve(diagnostic, definition) {
    var projector = typeof options.resolveTarget === 'function'
      ? options.resolveTarget : resolveOrchestrationDiagnosticTarget;
    return projector(diagnostic, definition);
  }

  function _focusTarget(target) {
    if (target && (target.kind === 'node' || target.kind === 'edge')
        && target.navigable !== false
        && typeof options.focusSelection === 'function'
        && options.focusSelection()) return true;
    var canvas = doc.getElementById('orchCanvas');
    if (!canvas || typeof canvas.focus !== 'function') return false;
    try { canvas.focus({ preventScroll: true }); }
    catch (_error) { canvas.focus(); }
    return true;
  }

  function navigate(diagnostic, descriptionId) {
    var definition = typeof options.definition === 'function'
      ? options.definition() : null;
    var target = resolve(diagnostic, definition);
    if (!target) return false;
    if (typeof options.navigateGroups === 'function'
        && options.navigateGroups(target.groups) === false) return false;
    var selected;
    var navigable = target.navigable !== false;
    if (navigable && target.kind === 'node'
        && typeof options.selectNode === 'function') {
      selected = options.selectNode(target.id);
    } else if (navigable && target.kind === 'edge'
               && typeof options.selectEdgeAt === 'function') {
      selected = options.selectEdgeAt(target.index);
    }
    if (selected === false) return false;
    if (typeof options.showInspector === 'function'
        && target.kind !== 'document' && navigable) options.showInspector();

    var focused = navigable && typeof options.focusDiagnostic === 'function'
      ? options.focusDiagnostic(
        target, diagnostic, _scrollBehavior(), descriptionId) : null;
    if (!focused) _focusTarget(target);
    close();
    return true;
  }

  function render() {
    var panel = doc.getElementById('orchIssuePanel');
    if (!panel || !current) return;
    var definition = typeof options.definition === 'function'
      ? options.definition() : null;
    presentation.render(panel, current, definition);
    var closeButton = panel.querySelector('[data-orch-issues-close]');
    if (closeButton) {
      closeButton.addEventListener('click', function () { close(true); });
    }
    Array.prototype.forEach.call(
      panel.querySelectorAll('[data-orch-issue-index]'), function (button) {
        var index = Number(button.getAttribute('data-orch-issue-index'));
        button.addEventListener('click', function () {
          navigate(current.diagnostics[index],
            button.getAttribute('data-orch-issue-message-id') || '');
        });
      }
    );
    if (issueKeyboard) issueKeyboard.sync();
  }

  function _syncExpanded() {
    var trigger = doc.getElementById('orchDocState');
    var panel = doc.getElementById('orchIssuePanel');
    if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (panel) panel.hidden = !open;
  }

  function close(restoreFocus) {
    if (!open) return false;
    open = false;
    _syncExpanded();
    var trigger = doc.getElementById('orchDocState');
    if (restoreFocus && trigger && typeof trigger.focus === 'function') {
      trigger.focus();
    }
    return true;
  }

  function show(state) {
    current = presentation.snapshot(state);
    open = !open;
    _syncExpanded();
    if (open) render();
    return open;
  }

  function sync(state) {
    current = presentation.snapshot(state);
    if (open) render();
  }

  var panel = doc.getElementById('orchIssuePanel');
  var trigger = doc.getElementById('orchDocState');
  if (panel) issueKeyboard = createOrchestrationRovingItemsController({
    root: panel,
    entry: trigger,
    selector: '.orch-issue-item',
    wrap: true,
    onEntry: function () {
      if (!current || open) return;
      open = true;
      _syncExpanded();
      render();
    },
  });
  doc.addEventListener('pointerdown', function (event) {
    if (!open) return;
    var wrap = doc.querySelector('.orch-doc-state-wrap');
    if (wrap && !wrap.contains(event.target)) close();
  });
  return {
    resolve: resolve,
    navigate: navigate,
    render: render,
    show: show,
    sync: sync,
    close: close,
    isOpen: function () { return open; },
  };
}

/* ===== migrated source: orchestration-feedback.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-feedback.js — shared Studio/Task Mode notifications

   Owns safe toast DOM construction, readable validator details and timeout
   cleanup. Feature controllers depend on its small toast/warn interface.
   Task Mode reaches it through the explicit Studio API; orchestration.js
   retains thin global compatibility facades for older extensions.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationFeedback(options) {
  options = options || {};

  function _document() {
    return options.document || document;
  }

  function _setTimeout(callback, delay) {
    var schedule = typeof options.setTimeout === 'function'
      ? options.setTimeout : setTimeout;
    return schedule(callback, delay);
  }

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function toast(text, isError, opts) {
    opts = opts || {};
    var doc = _document();
    var element = doc.createElement('div');
    element.className = 'orch-toast' + (isError ? ' is-err' : '')
      + (opts.warn ? ' is-warn' : '');
    element.appendChild(doc.createTextNode(text));

    var detail = opts.detail;
    if (detail && detail.length) {
      var lines = Array.isArray(detail) ? detail : [String(detail)];
      var box = doc.createElement('div');
      box.className = 'orch-toast-detail';
      lines.forEach(function (line) {
        var row = doc.createElement('div');
        row.textContent = String(line);
        box.appendChild(row);
      });
      element.appendChild(box);
    }

    doc.body.appendChild(element);
    var dwell = opts.dwell || 2600;
    _setTimeout(function () {
      element.style.opacity = '0';
      _setTimeout(function () { element.remove(); }, 300);
    }, dwell);
    return element;
  }

  function warn(prefix, warnings, isError) {
    var values = Array.isArray(warnings)
      ? warnings : (warnings == null ? [] : [warnings]);
    var issues = typeof options.issueMessages === 'function'
      ? options.issueMessages(values)
      : values.filter(function (warning) { return warning; });
    if (!issues.length) return toast(prefix, !!isError);
    var count = issues.length;
    var noun = isError ? ' issue' : ' warning';
    var countKey = isError
      ? 'orch.feedback.issueCount' : 'orch.feedback.warningCount';
    var countText = _translate(countKey, { count: count });
    if (!countText || countText === countKey) {
      countText = count + noun + (count > 1 ? 's' : '');
    }
    return toast(
      prefix + ' — ' + countText,
      !!isError,
      { warn: !isError, detail: issues, dwell: 6500 }
    );
  }

  return { toast: toast, warn: warn };
}

/* ===== migrated source: orchestration-export.js ===== */
/* Definition export boundary for Orchestration Studio.
 *
 * Owns JSON download naming, browser object-URL cleanup and user feedback.
 * Callers provide only a root snapshot; write-conflict recovery and toolbar
 * export therefore cannot assemble different download behavior.
 */

function createOrchestrationExportController(options) {
  options = options || {};
  var doc = options.document
    || (typeof document !== 'undefined' ? document : null);
  var Url = options.urlApi
    || (typeof URL !== 'undefined' ? URL : null);
  var BlobType = options.Blob
    || (typeof Blob !== 'undefined' ? Blob : null);
  var schedule = options.schedule
    || (typeof setTimeout === 'function' ? setTimeout : null);

  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function notify(key, params, isError) {
    if (typeof options.toast !== 'function') return;
    try {
      options.toast(translate(key, params), !!isError);
    } catch (error) {
      report('notify', error);
    }
  }

  function report(context, error) {
    return reportOrchestrationDiagnostic(options.onError, context, error);
  }

  function filenameFor(definition) {
    var raw = definition && definition.name ? definition.name : 'flow';
    var stem = String(raw).trim()
      .replace(/[^a-z0-9_-]+/gi, '_')
      .replace(/^_+|_+$/g, '')
      .toLowerCase();
    return (stem || 'flow') + '.orch.json';
  }

  function revoke(url) {
    if (!url || !Url || typeof Url.revokeObjectURL !== 'function') return;
    try { Url.revokeObjectURL(url); } catch (error) { report('revoke', error); }
  }

  function deferRevoke(url) {
    if (typeof schedule !== 'function') {
      revoke(url);
      return;
    }
    try {
      schedule(function () { revoke(url); }, 1000);
    } catch (error) {
      report('schedule-revoke', error);
      revoke(url);
    }
  }

  function exportDefinition(definition) {
    var anchor = null;
    var url = null;
    try {
      if (!definition || typeof definition !== 'object') {
        throw new Error('A definition object is required');
      }
      if (!doc || !doc.body || typeof doc.createElement !== 'function'
          || !BlobType || !Url || typeof Url.createObjectURL !== 'function') {
        throw new Error('Browser download APIs are unavailable');
      }
      var filename = filenameFor(definition);
      var payload = JSON.stringify(definition, null, 2);
      var blob = new BlobType([payload], { type: 'application/json' });
      url = Url.createObjectURL(blob);
      anchor = doc.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      doc.body.appendChild(anchor);
      anchor.click();
      deferRevoke(url);
      url = null;
      notify('orch.export.done', { file: filename }, false);
      return filename;
    } catch (error) {
      revoke(url);
      report('export', error);
      notify('orch.export.failed', null, true);
      return null;
    } finally {
      if (anchor && typeof anchor.remove === 'function') anchor.remove();
    }
  }

  function exportCurrent() {
    var definition = typeof options.snapshot === 'function'
      ? options.snapshot() : null;
    return exportDefinition(definition);
  }

  return {
    exportCurrent: exportCurrent,
    exportDefinition: exportDefinition,
    filenameFor: filenameFor,
  };
}

/* ===== migrated source: orchestration-composer-log-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-composer-log-view.js — AI Composer conversation projection

   Owns append-only history diffing, safe message nodes and ARIA-silent full
   repaints. Panel visibility, focus and input controls stay in the facade.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationComposerLogView(options) {
  options = options || {};
  var renderedHistory = [];

  function doc() { return options.document || document; }
  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function div(className, text) {
    var element = doc().createElement('div');
    element.className = className;
    if (text != null) element.textContent = String(text);
    return element;
  }
  function setRichCopy(element, value) {
    if (typeof options.richCopy === 'function') {
      element.innerHTML = options.richCopy(value);
    } else {
      element.textContent = String(value == null ? '' : value);
    }
  }
  function renderEmpty(log) {
    var empty = div('orch-ai-empty');
    var icon = div('orch-ai-empty-icon');
    // Catalog icons are trusted, code-owned SVG. Localized and remote
    // conversation content below is emitted through text nodes/safe rich copy.
    icon.innerHTML = (options.icons || {}).wand || '';
    empty.appendChild(icon);
    empty.appendChild(div(
      'orch-ai-empty-title', translate('orch.ai.emptyTitle')
    ));
    var copy = div('orch-ai-empty-text');
    setRichCopy(copy, translate('orch.ai.emptyText'));
    empty.appendChild(copy);
    log.appendChild(empty);
  }
  function renderMessage(log, message) {
    var role = message && message.role === 'user' ? 'user' : 'bot';
    log.appendChild(div(
      'orch-ai-msg orch-ai-' + role,
      message && message.content != null ? message.content : ''
    ));
  }
  function sameMessage(left, right) {
    return !!left && !!right && left.role === right.role
      && left.content === right.content;
  }
  function canAppend(history) {
    return renderedHistory.length <= history.length
      && renderedHistory.every(function (message, index) {
        return sameMessage(message, history[index]);
      });
  }
  function withoutAnnouncements(log, callback) {
    var hadLive = log.hasAttribute('aria-live');
    var live = log.getAttribute('aria-live');
    log.setAttribute('aria-live', 'off');
    callback();
    if (hadLive) log.setAttribute('aria-live', live);
    else log.removeAttribute('aria-live');
  }
  function remember(history) {
    renderedHistory = history.map(function (message) {
      return { role: message.role, content: message.content };
    });
  }

  function render(snapshot) {
    snapshot = snapshot || { history: [], busy: false };
    var log = doc().getElementById('orchAiLog');
    if (!log) return false;
    var history = Array.isArray(snapshot.history) ? snapshot.history : [];
    log.setAttribute('aria-busy', snapshot.busy ? 'true' : 'false');
    if (!history.length) {
      if (renderedHistory.length || !log.querySelector('.orch-ai-empty')) {
        withoutAnnouncements(log, function () {
          log.textContent = '';
          renderEmpty(log);
        });
      }
      renderedHistory = [];
      return true;
    }
    var appendOnly = canAppend(history);
    var typing = log.querySelector('.orch-ai-typing');
    if (typing) typing.remove();
    if (!appendOnly) {
      withoutAnnouncements(log, function () {
        log.textContent = '';
        history.forEach(function (message) { renderMessage(log, message); });
      });
    } else {
      if (!renderedHistory.length) {
        withoutAnnouncements(log, function () { log.textContent = ''; });
      }
      history.slice(renderedHistory.length).forEach(function (message) {
        renderMessage(log, message);
      });
    }
    remember(history);
    if (snapshot.busy) {
      var busyMessage = div(
        'orch-ai-msg orch-ai-bot orch-ai-typing',
        translate('orch.ai.composing') + ' '
      );
      var dot = doc().createElement('span');
      dot.className = 'orch-dot';
      dot.setAttribute('aria-hidden', 'true');
      busyMessage.appendChild(dot);
      log.appendChild(busyMessage);
    }
    log.scrollTop = log.scrollHeight;
    return true;
  }

  return Object.freeze({ render: render });
}

/* ===== migrated source: orchestration-composer-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-composer-view.js — AI Composer DOM presentation

   Owns Composer panel visibility, focus scheduling and input controls. Safe
   conversation projection lives in orchestration-composer-log-view.js.
   Request/history ownership stays in orchestration-composer.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationComposerView(options) {
  options = options || {};
  var focusTimer = null;
  var opened = false;
  var logView = createOrchestrationComposerLogView(options);
  var focusReturn = createOrchestrationPanelFocusReturn();

  function _document() {
    return options.document || document;
  }

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _schedule(callback, delay) {
    return typeof options.schedule === 'function'
      ? options.schedule(callback, delay) : setTimeout(callback, delay);
  }

  function _cancel(timer) {
    if (typeof options.cancelSchedule === 'function') {
      options.cancelSchedule(timer);
    } else {
      clearTimeout(timer);
    }
  }

  function _cancelFocus() {
    if (focusTimer == null) return;
    _cancel(focusTimer);
    focusTimer = null;
  }

  function render(snapshot) {
    return logView.render(snapshot);
  }

  function setEnabled(enabled) {
    var send = _document().getElementById('orchAiSend');
    var input = _document().getElementById('orchAiText');
    if (send) send.disabled = !enabled;
    if (input) input.disabled = !enabled;
  }

  function requirement() {
    var input = _document().getElementById('orchAiText');
    return input ? input.value : '';
  }

  function clearRequirement() {
    var input = _document().getElementById('orchAiText');
    if (input) input.value = '';
  }

  function toggle(force, snapshot) {
    var doc = _document();
    var panel = doc.getElementById('orchAi');
    var button = doc.getElementById('orchAiToggle');
    if (!panel) return false;
    var open = typeof force === 'boolean' ? force : !opened;
    if (open) focusReturn.capture(doc);
    else focusReturn.prepare(doc, panel);
    setOrchestrationPanelState(panel, open, {
      document: doc,
      openClass: 'is-open',
      trigger: button,
      triggerActiveClass: 'is-active',
    });
    opened = open;
    if (typeof options.onVisibilityChange === 'function') {
      options.onVisibilityChange(opened);
    }
    if (!open) focusReturn.restore(doc);
    _cancelFocus();
    if (open) {
      if (!snapshot || !snapshot.history || !snapshot.history.length) {
        render(snapshot);
      }
      focusTimer = _schedule(function () {
        focusTimer = null;
        var input = doc.getElementById('orchAiText');
        if (panel.classList.contains('is-open') && input && !input.disabled) {
          input.focus();
        }
      }, 50);
    }
    return open;
  }

  function destroy() {
    _cancelFocus();
    focusReturn.clear();
  }

  return {
    render: render,
    setEnabled: setEnabled,
    requirement: requirement,
    clearRequirement: clearRequirement,
    isOpen: function () { return opened; },
    toggle: toggle,
    destroy: destroy,
  };
}

/* ===== migrated source: orchestration-composer.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-composer.js — AI authoring conversation controller

   Owns Composer history, request single-flight/epoch state and revision-safe
   graph adoption. DOM, accessibility and focus behavior live in the injected
   orchestration-composer-view.js module.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationComposerController(options) {
  options = options || {};
  var state = { history: [], busy: false, epoch: 0 };
  var view = options.view || {};
  var limitPolicy = orchestrationRequestLimitPolicy(
    options.limitPolicy || options.requestLimits);
  var requests = createOrchestrationComposerRequestClient({
    api: options.api,
    normalizeComposeResult: options.normalizeComposeResult,
  });

  function normalizeInspection(value) {
    return projectOrchestrationInspection(options, value);
  }

  function _clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _requestHistory(history) {
    var retained = limitPolicy.composeHistoryLimit();
    var selected = retained == null ? history : history.slice(-retained);
    var messageLimit = limitPolicy.composeHistoryMessageLimit();
    if (messageLimit == null) return selected;
    return selected.map(function (turn) {
      return Object.assign({}, turn, {
        content: String(turn && turn.content || '')
          .trim().slice(0, messageLimit),
      });
    });
  }

  function snapshot() {
    return { history: _clone(state.history), busy: state.busy };
  }

  function setEnabled(enabled) {
    if (typeof view.setEnabled === 'function') view.setEnabled(enabled);
  }

  function render() {
    if (typeof view.render === 'function') return view.render(snapshot());
    return false;
  }

  function toggle(force) {
    if (typeof view.toggle === 'function') return view.toggle(force, snapshot());
    return false;
  }

  function isOpen() {
    return typeof view.isOpen === 'function' && view.isOpen();
  }

  function close() {
    return toggle(false);
  }

  function open() {
    return toggle(true);
  }

  function _requestFailureMessage(response) {
    return orchestrationRequestFailureMessage(
      response, _translate, '', {keys: {
        'server-failed': 'orch.ai.serverFailed',
        'request-rejected': 'orch.ai.requestRejected',
        'malformed-response': 'orch.ai.malformedResponse',
        'transport-failed': 'orch.ai.requestFailed',
      }, defaultKey: 'orch.ai.requestFailed'});
  }

  function _adoptDefinition(definition, id, opts) {
    var value;
    if (typeof options.applyDefinitionResult === 'function') {
      value = options.applyDefinitionResult(definition, id, opts);
    } else if (typeof options.applyDefinition === 'function') {
      value = options.applyDefinition(definition, id, opts);
    }
    return normalizeOrchestrationDefinitionAdoption(value);
  }

  function clear() {
    // Invalidate an in-flight request. It may finish at the transport layer,
    // but it can no longer append history or apply a graph after the user has
    // explicitly cleared the conversation.
    state.epoch++;
    state.history.splice(0, state.history.length);
    state.busy = false;
    setEnabled(true);
    render();
    return snapshot();
  }

  function handleKey(event) {
    if (event.key !== 'Enter' || event.shiftKey) return;
    event.preventDefault();
    send();
  }

  async function send(requirement) {
    if (state.busy) return { ok: false, reason: 'busy' };
    var text = String(requirement == null
      ? (typeof view.requirement === 'function' ? view.requirement() : '')
      : requirement).trim();
    if (!text) return { ok: false, reason: 'empty' };

    if (!requests.available()) {
      if (typeof options.toast === 'function') {
        options.toast(_translate('orch.api.unavailable'), true);
      }
      return { ok: false, reason: 'unavailable' };
    }

    if (typeof view.clearRequirement === 'function') view.clearRequirement();
    var priorHistory = _clone(state.history);
    state.history.push({ role: 'user', content: text });
    state.busy = true;
    var requestEpoch = ++state.epoch;
    var composeRevision = typeof options.revision === 'function'
      ? options.revision() : 0;
    var current = typeof options.currentDefinition === 'function'
      ? options.currentDefinition() : null;
    render();
    setEnabled(false);

    var response = await requests.compose(
      text, current, _requestHistory(priorHistory));
    if (response.cause) {
      reportOrchestrationDiagnostic(options.onError, 'compose', response.cause);
    }
    // clear() or a newer request invalidated this response. Do not touch busy
    // state here: a newer request may currently own the controls.
    if (requestEpoch !== state.epoch) {
      return { ok: false, reason: 'stale' };
    }

    state.busy = false;
    setEnabled(true);
    var result = response.result;
    if (!response.ok || !result) {
      state.history.push({
        role: 'assistant', content: _requestFailureMessage(response),
      });
      render();
      return {
        ok: false,
        reason: response.reason,
        status: response.status,
        error: response.error,
      };
    }
    var inspection = normalizeInspection(result);

    var reply = result.reply || (result.ok
      ? _translate('orch.ai.updated') : _translate('orch.ai.invalid'));
    var errors = orchestrationIssueMessages(result, { maxMessages: 3 });
    if (!result.ok && errors.length) {
      reply += '\n' + errors.join('; ');
    }
    state.history.push({ role: 'assistant', content: String(reply) });
    render();

    if (result.ok && result.definition) {
      var currentRevision = typeof options.revision === 'function'
        ? options.revision() : composeRevision;
      if (currentRevision !== composeRevision) {
        if (typeof options.toast === 'function') {
          options.toast(_translate('orch.doc.composeConflict'), true);
        }
        return { ok: false, reason: 'revision-conflict', result: result };
      }
      var currentId = typeof options.currentId === 'function'
        ? options.currentId() : null;
      var adoption = _adoptDefinition(result.definition, currentId, {
        dirty: true,
        inspection: inspection,
      });
      if (!adoption.ok) {
        if (adoption.cause) {
          reportOrchestrationDiagnostic(options.onError, 'adopt', adoption.cause);
        }
        state.history[state.history.length - 1].content =
          _translate('orch.store.readFailed');
        render();
        return { ok: false, reason: 'invalid-definition',
          adoption: adoption, result: result };
      }
      if (typeof options.warn === 'function') {
        options.warn(
          _translate('orch.ai.graphUpdated'),
          (inspection && inspection.warnings) || []
        );
      }
    }
    return { ok: !!result.ok, result: result };
  }

  return {
    state: state,
    snapshot: snapshot,
    render: render,
    isOpen: isOpen,
    open: open,
    toggle: toggle,
    close: close,
    clear: clear,
    handleKey: handleKey,
    send: send,
    setEnabled: setEnabled,
  };
}

/* ===== migrated source: orchestration-store-menu-focus.js ===== */
/* Semantic focus continuity for the dynamically repainted saved-flow menu. */

function createOrchestrationStoreMenuFocusController(options) {
  options = options || {};
  var entries = [];
  var intent = null;

  function _document() { return options.document || document; }
  function _menu() { return _document().getElementById('orchLoadMenu'); }

  function _snapshot() {
    var menu = _menu();
    var active = _document().activeElement;
    if (!menu || !active || !menu.contains(active)) return null;
    var attribute = active.hasAttribute('data-delete-index')
      ? 'data-delete-index' : active.hasAttribute('data-load-index')
        ? 'data-load-index' : null;
    if (!attribute) return null;
    var index = Number(active.getAttribute(attribute));
    var entry = entries[index];
    return entry ? {
      action: attribute === 'data-delete-index' ? 'delete' : 'load',
      id: entry.id,
      index: index,
    } : null;
  }

  function remember() {
    intent = _snapshot() || intent;
    return intent;
  }

  function stage(entry, index, action) {
    intent = { action: action, id: entry.id, index: index };
    return intent;
  }

  function clear(token) {
    if (intent === token) intent = null;
  }

  function cancel() { intent = null; }

  function render(nextEntries) {
    var menu = _menu();
    var saved = _snapshot() || intent;
    var target = null;
    intent = null;
    entries = nextEntries.slice();
    if (menu && saved && entries.length) {
      var index = entries.findIndex(function (entry) {
        return entry.id === saved.id;
      });
      var action = saved.action;
      if (index < 0) {
        index = Math.min(saved.index, entries.length - 1);
        action = 'load';
      }
      target = menu.querySelector('[' + (action === 'delete'
        ? 'data-delete-index' : 'data-load-index') + '="' + index + '"]');
    }
    if (options.popupMenus
        && typeof options.popupMenus.syncItems === 'function') {
      options.popupMenus.syncItems('orchLoadMenu', target);
    }
    if (target && options.popupMenus.isOpen('orchLoadMenu')) target.focus();
    return target;
  }

  function finishMessage() {
    var restore = !!intent;
    intent = null;
    entries = [];
    if (options.popupMenus
        && typeof options.popupMenus.syncItems === 'function') {
      options.popupMenus.syncItems('orchLoadMenu');
    }
    if (restore) {
      options.popupMenus.setOpen(
        'orchLoadMenu', 'orchLoadBtn', false, { restoreFocus: true });
    }
    return restore;
  }

  return {
    cancel: cancel,
    clear: clear,
    finishMessage: finishMessage,
    remember: remember,
    render: render,
    stage: stage,
  };
}

/* ===== migrated source: orchestration-store-browser-presentation.js ===== */
/* Pure saved-flow list formatting and safe HTML projection. */

function createOrchestrationStoreBrowserPresentation(options) {
  options = options || {};
  var escape = options.escape || function (value) {
    return String(value == null ? '' : value);
  };
  var translate = options.translate || function (key) { return key; };
  var icons = options.icons || {};

  function updatedTime(value) {
    var timestamp = Number(value);
    if (!Number.isSafeInteger(timestamp) || timestamp <= 0) return null;
    var date = new Date(timestamp);
    if (!Number.isFinite(date.getTime())) return null;
    var now = typeof options.now === 'function' ? options.now() : Date.now();
    var elapsed = Math.max(0, Number(now) - timestamp);
    var minutes = Math.floor(elapsed / 60000);
    var relative;
    if (minutes < 1) {
      relative = translate('orch.load.updatedJustNow');
    } else if (minutes < 60) {
      relative = translate('orch.load.updatedMinutesAgo', { n: minutes });
    } else if (minutes < 1440) {
      relative = translate('orch.load.updatedHoursAgo', {
        n: Math.floor(minutes / 60),
      });
    } else if (minutes < 10080) {
      relative = translate('orch.load.updatedDaysAgo', {
        n: Math.floor(minutes / 1440),
      });
    } else {
      try { relative = date.toLocaleDateString(); }
      catch (_error) { relative = ''; }
    }
    var absolute = '';
    try { absolute = date.toLocaleString(); }
    catch (_error) { absolute = relative; }
    return {
      relative: String(relative || ''),
      absolute: String(absolute || ''),
      datetime: date.toISOString(),
    };
  }

  function messageHtml(key, params) {
    return '<div class="orch-load-empty" role="status">'
      + escape(translate(key, params)) + '</div>';
  }

  function rowsHtml(entries, currentId) {
    return entries.map(function (entry, index) {
      var count = Number.isSafeInteger(entry.nodeCount) && entry.nodeCount >= 0
        ? entry.nodeCount
        : (entry.definition && (entry.definition.nodes || []).length || 0);
      var isCurrent = currentId != null && entry.id === currentId;
      var name = String(entry.name || translate('orch.load.untitled'));
      var updated = updatedTime(entry.updatedAt);
      var updatedHtml = updated && updated.relative
        ? '<time datetime="' + escape(updated.datetime) + '" title="'
          + escape(translate('orch.load.updatedTitle', {
            time: updated.absolute,
          })) + '">' + escape(updated.relative) + '</time>'
        : '';
      return '<div class="orch-load-row' + (isCurrent ? ' is-current' : '')
        + '" role="presentation'
        + '"><button type="button" class="orch-load-pick" role="menuitem" '
        + (isCurrent ? 'aria-current="true" ' : '')
        + 'data-load-index="' + index + '">'
        + '<span class="orch-load-title"><span class="orch-load-name">'
        + escape(name) + '</span>'
        + (isCurrent ? '<span class="orch-load-current">'
          + escape(translate('orch.load.current')) + '</span>' : '')
        + '</span>'
        + '<span class="orch-load-meta"><span>'
        + escape(translate('orch.load.nodes', { n: count })) + '</span>'
        + updatedHtml + '</span></button>'
        + '<button type="button" class="orch-load-del" role="menuitem" '
        + 'data-delete-index="' + index + '" title="'
        + escape(translate('orch.load.delete')) + '" aria-label="'
        + escape(translate('orch.load.deleteNamed', { name: name })) + '">'
        + (icons.reject || '') + '</button></div>';
    }).join('');
  }

  return {
    messageHtml: messageHtml,
    rowsHtml: rowsHtml,
    updatedTime: updatedTime,
  };
}

/* ===== migrated source: orchestration-store-browser.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-store-browser.js — saved-flow menu controller

   Owns list request fencing and safe DOM event bindings.
   Saved-flow formatting and HTML projection live in the presentation sibling.
   Load/delete commands remain injected; this module never mutates the draft.
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationStoreBrowser(options) {
  options = options || {};
  var popupMenus = options.popupMenus;
  var requestGeneration = 0;
  var presentation = options.presentation
    || createOrchestrationStoreBrowserPresentation(options);
  var menuFocus = createOrchestrationStoreMenuFocusController({
    document: options.document, popupMenus: popupMenus });
  var definitions = options.definitions ||
    createOrchestrationDefinitionRequestClient({ api: options.api });

  function _document() { return options.document || document; }
  function _menu() { return _document().getElementById('orchLoadMenu'); }
  function _isOpen() {
    return !!popupMenus && popupMenus.isOpen('orchLoadMenu');
  }
  function _setOpen(open, opts) {
    return popupMenus ? popupMenus.setOpen(
      'orchLoadMenu', 'orchLoadBtn', open, opts) : false;
  }

  function _message(menu, key, params) {
    menu.innerHTML = presentation.messageHtml(key, params);
  }

  function _runAction(button, action) {
    if (!button || button.disabled || typeof action !== 'function') return null;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    var result;
    try { result = action(); }
    catch (error) { result = Promise.reject(error); }
    return Promise.resolve(result).catch(function (error) {
      reportOrchestrationDiagnostic(options.onError, 'row-action', error);
      return null;
    }).finally(function () {
      // Successful load/delete usually replaces or hides this row. Only
      // restore a control that still belongs to the live menu DOM.
      if (!button.isConnected) return;
      button.disabled = false;
      button.removeAttribute('aria-busy');
    });
  }

  function _renderRows(menu, entries) {
    var currentId = typeof options.currentId === 'function'
      ? options.currentId() : null;
    menu.innerHTML = presentation.rowsHtml(entries, currentId);
    Array.prototype.forEach.call(
      menu.querySelectorAll('[data-load-index]'), function (button) {
        var entry = entries[Number(button.getAttribute('data-load-index'))];
        button.addEventListener('click', function () {
          _runAction(button, function () {
            return typeof options.onLoad === 'function'
              ? options.onLoad(entry.id) : null;
          });
        });
      }
    );
    Array.prototype.forEach.call(
      menu.querySelectorAll('[data-delete-index]'), function (button) {
        var index = Number(button.getAttribute('data-delete-index'));
        var entry = entries[index];
        button.addEventListener('click', function (event) {
          var intent = menuFocus.stage(entry, index, 'delete');
          var action = _runAction(button, function () {
            return typeof options.onDelete === 'function'
              ? options.onDelete(
                entry.id, event,
                entry.definitionVersion == null
                  ? entry.updatedAt : entry.definitionVersion) : null;
          });
          Promise.resolve(action).finally(function () {
            menuFocus.clear(intent);
          });
        });
      }
    );
    menuFocus.render(entries);
  }

  function close(opts) {
    requestGeneration += 1; menuFocus.cancel();
    var menu = _menu();
    if (menu) menu.setAttribute('aria-busy', 'false');
    _setOpen(false, opts);
    return false;
  }

  async function open(forceOpen) {
    var menu = _menu();
    if (!menu) return [];
    if (forceOpen !== true && _isOpen()) {
      close();
      return [];
    }
    var generation = ++requestGeneration;
    menuFocus.remember();
    if (popupMenus) popupMenus.setOpen('orchTplMenu', 'orchTplBtn', false);
    _setOpen(true);
    menu.setAttribute('aria-busy', 'true');
    _message(menu, 'orch.load.loading');
    function stillCurrent() {
      return generation === requestGeneration && _isOpen();
    }
    function settleBusy() {
      if (generation === requestGeneration) {
        menu.setAttribute('aria-busy', 'false');
      }
    }
    if (!definitions.canList()) {
      if (stillCurrent()) {
        _message(menu, 'orch.load.failed', {
          error: typeof options.translate === 'function'
            ? options.translate('orch.api.unavailable')
            : 'orch.api.unavailable',
        });
        menuFocus.finishMessage();
      }
      settleBusy();
      return [];
    }
    var result = await definitions.list();
    if (result.cause) {
      reportOrchestrationDiagnostic(options.onError, 'list', result.cause);
    }
    if (!result.ok) {
      if (stillCurrent()) {
        _message(menu, 'orch.load.failed', {
          error: result.error || (typeof options.translate === 'function'
            ? options.translate(orchestrationRequestFailureKey(result))
            : orchestrationRequestFailureKey(result)),
        });
        menuFocus.finishMessage();
      }
      settleBusy();
      return [];
    }
    var list = result.items;
    if (!stillCurrent()) {
      settleBusy();
      return [];
    }
    // definition-list/v1 is already newest-first. Retain this stable client
    // sort only for rolling servers that still return repository order.
    var entries = list.filter(function (entry) {
      return entry && entry.id != null;
    }).slice();
    if (!result.canonical) {
      entries.sort(function (left, right) {
        return (right.updatedAt || 0) - (left.updatedAt || 0);
      });
    }
    if (!entries.length) {
      _message(menu, 'orch.load.empty');
      settleBusy();
      menuFocus.finishMessage();
      return [];
    }
    _renderRows(menu, entries);
    settleBusy();
    return entries;
  }

  return {
    open: open,
    close: close,
    isOpen: _isOpen,
  };
}

/* ===== migrated source: orchestration-workspace.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-workspace.js — Studio workspace coordinator

   Owns builtin/layout authoring and popup composition. Persisted definition
   save/load/delete semantics live in orchestration-workspace-persistence.js;
   saved-flow DOM lives in orchestration-store-browser.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationWorkspaceController(options) {
  options = options || {};
  var popupMenus = options.popupMenus;
  var workspaceSession = options.workspaceSession
    || createOrchestrationWorkspaceSessionPort(options);
  var authoringRequest = options.authoringRequest ||
    createOrchestrationWorkspaceRequestClient({
      api: options.api,
      normalizeBuiltin: options.normalizeBuiltin,
      normalizeLayout: options.normalizeLayout,
    });
  var definitionRequest = options.definitionRequest ||
    createOrchestrationDefinitionRequestClient({
      api: options.api,
      normalizeList: options.normalizeList,
      normalizeRead: options.normalizeRead,
      normalizeSave: options.normalizeSave,
      normalizeDelete: options.normalizeDelete,
      definitionWriteContract: options.definitionWriteContract,
      definitionListContract: options.definitionListContract,
      definitionEntryContract: options.definitionEntryContract,
    });
  var storeBrowser = null;
  var persistence = createOrchestrationWorkspacePersistence(Object.assign(
    {},
    options,
    {
      definitionRequest: definitionRequest,
      workspaceSession: workspaceSession,
      closeStore: function () {
        return storeBrowser ? storeBrowser.close() : false;
      },
      refreshStore: function () {
        return storeBrowser ? storeBrowser.open(true) : Promise.resolve([]);
      },
    }
  ));

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _toast(message, error) {
    if (typeof options.toast === 'function') options.toast(message, error);
  }

  function _applyDefinitionResult(definition, id, opts) {
    return workspaceSession.applyDefinitionResult(definition, id, opts);
  }

  function loadFromStore(id, opts) {
    return persistence.load(id, opts);
  }

  function deleteFromStore(id, event, listedUpdatedAt) {
    if (event) event.stopPropagation();
    return persistence.remove(id, listedUpdatedAt);
  }

  storeBrowser = createOrchestrationStoreBrowser({
    document: options.document,
    popupMenus: popupMenus,
    definitions: definitionRequest,
    currentId: persistence.currentId,
    now: options.now,
    translate: options.translate,
    escape: options.escape,
    icons: options.icons,
    onError: options.onError,
    onLoad: loadFromStore,
    onDelete: deleteFromStore,
  });

  function popupIsOpen(menuId) {
    return !!popupMenus && popupMenus.isOpen(menuId);
  }

  function setPopupOpen(menuId, buttonId, open) {
    return popupMenus
      ? popupMenus.setOpen(menuId, buttonId, open) : false;
  }

  function toggleTemplateMenu(forceClose) {
    var open = !forceClose && !popupIsOpen('orchTplMenu');
    if (open) storeBrowser.close();
    setPopupOpen('orchTplMenu', 'orchTplBtn', open);
    return open;
  }

  function loadBlankDraft() {
    if (typeof options.blankDefinition !== 'function') return null;
    var definition = options.blankDefinition();
    var adoption = _applyDefinitionResult(definition, null);
    return adoption.ok ? definition : null;
  }

  async function loadBuiltin(name, opts) {
    opts = opts || {};
    if (!authoringRequest.canLoadBuiltin()) {
      if (opts.initial) return loadBlankDraft();
      _toast(_translate('orch.api.unavailable'), true);
      return null;
    }
    var result = await authoringRequest.loadBuiltin(name);
    if (result.cause) {
      reportOrchestrationDiagnostic(options.onError, 'builtin', result.cause);
    }
    if (!result.ok) {
      if (opts.initial) return loadBlankDraft();
      _toast(_translate('orch.builtin.loadFailed', { name: name })
        + ': ' + (result.error || _translate(
          orchestrationRequestFailureKey(result))), true);
      return null;
    }
    var adoption = _applyDefinitionResult(result.definition, null, {
      inspection: name === 'blank' ? null : (result.inspection || null),
    });
    if (!adoption.ok) {
      if (adoption.cause) {
        reportOrchestrationDiagnostic(options.onError, 'adopt', adoption.cause);
      }
      _toast(_translate('orch.builtin.loadFailed', { name: name })
        + ': ' + _translate('orch.store.readFailed'), true);
      return null;
    }
    if (!opts.initial) {
      _toast(_translate('orch.builtin.loaded', { name: name }));
    }
    return result.definition;
  }

  async function chooseBuiltin(name) {
    if (typeof options.confirmReplace === 'function'
        && !await options.confirmReplace()) return null;
    toggleTemplateMenu(true);
    return loadBuiltin(name);
  }

  async function tidy(opts) {
    opts = opts || {};
    var silent = !!opts.silent;
    if (typeof options.nodeCount === 'function' && !options.nodeCount()) {
      return null;
    }
    if (!authoringRequest.canLayout()) {
      if (!silent) _toast(_translate('orch.api.unavailable'), true);
      return null;
    }
    var definition = typeof options.currentLevelDefinition === 'function'
      ? options.currentLevelDefinition() : null;
    var lifecycle = options.lifecycle;
    var layoutRevision = lifecycle && typeof lifecycle.revision === 'function'
      ? lifecycle.revision() : null;
    var layoutWorkspace = typeof options.workspaceToken === 'function'
      ? options.workspaceToken() : null;
    var result = await authoringRequest.layout(definition);
    if (result.cause) {
      reportOrchestrationDiagnostic(options.onError, 'layout', result.cause);
    }
    if (!result.ok) {
      if (!silent) {
        _toast(_translate('orch.tidy.failed')
          + ': ' + (result.error || _translate(
            orchestrationRequestFailureKey(result))), true);
      }
      return null;
    }
    var currentRevision = lifecycle && typeof lifecycle.revision === 'function'
      ? lifecycle.revision() : layoutRevision;
    var currentWorkspace = typeof options.workspaceToken === 'function'
      ? options.workspaceToken() : layoutWorkspace;
    if (currentRevision !== layoutRevision
        || currentWorkspace !== layoutWorkspace) {
      if (!silent) _toast(_translate('orch.tidy.stale'));
      return null;
    }
    var projection = result.positions
      ? { ok: true, positions: result.positions }
      : projectOrchestrationLayoutPositions(result.definition, definition);
    if (!projection.ok) {
      if (projection.cause) {
        reportOrchestrationDiagnostic(options.onError, 'layout', projection.cause);
      }
      if (!silent) {
        _toast(_translate('orch.tidy.failed') + ': '
          + (projection.code || _translate('orch.store.readFailed')), true);
      }
      return null;
    }
    if (typeof options.applyPositions === 'function') {
      options.applyPositions(projection.positions);
    }
    if (!opts.preserveDocumentState
        && lifecycle && typeof lifecycle.markDirty === 'function') {
      lifecycle.markDirty();
    } else if (opts.preserveDocumentState
        && lifecycle && typeof lifecycle.syncHistory === 'function') {
      lifecycle.syncHistory();
    }
    if (typeof options.render === 'function') options.render();
    if (typeof options.fitView === 'function') options.fitView();
    if (!silent) _toast(_translate('orch.tidy.done'));
    return result.definition;
  }

  return {
    toggleTemplateMenu: toggleTemplateMenu,
    loadBuiltin: loadBuiltin,
    chooseBuiltin: chooseBuiltin,
    loadBlankDraft: loadBlankDraft,
    tidy: tidy,
    save: persistence.save,
    saveAndUse: persistence.saveAndUse,
    openLoadMenu: function (forceOpen) {
      return storeBrowser.open(forceOpen);
    },
    loadFromStore: loadFromStore,
    deleteFromStore: deleteFromStore,
  };
}
/* ===== migrated source: orchestration-run-overlay.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-run-overlay.js — Studio canvas runtime projection

   Projects shared run events onto node-card status attributes and refreshes
   the selected-node trace. Execution transport remains in orchestration-run;
   this controller owns only the canvas/Inspector presentation seam.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationRunOverlay(options) {
  options = options || {};

  function _document() {
    return options.document || document;
  }

  function _selectedNodeId() {
    return typeof options.selectedNodeId === 'function'
      ? options.selectedNodeId() : null;
  }

  function _definition() {
    return typeof options.definition === 'function'
      ? options.definition() : null;
  }

  function startSeed(definition) {
    var snapshot = definition || _definition() || {};
    var nodes = Array.isArray(snapshot.nodes) ? snapshot.nodes : [];
    var start = nodes.filter(function (node) {
      return node && node.kind === 'start';
    })[0];
    var seed = start && start.params && start.params.seed;
    return seed == null || seed === '' ? '' : String(seed);
  }

  function reset() {
    var elements = _document().querySelectorAll('.orch-node[data-run-status]');
    Array.prototype.forEach.call(elements, function (element) {
      element.removeAttribute('data-run-status');
    });
  }

  function setNodeStatus(nodeId, status) {
    if (!nodeId) return false;
    var element = _document().getElementById('orch-node-' + nodeId);
    if (!element) return false;
    element.setAttribute('data-run-status', status);
    return true;
  }

  function applyChange(state, change) {
    state = state || {};
    change = change || {};
    if (change.nodeId && change.nodeStatus) {
      setNodeStatus(change.nodeId, change.nodeStatus);
    }
    var selected = _selectedNodeId();
    if (!selected || (selected !== change.nodeId && !change.terminal)) {
      return false;
    }
    if (typeof options.renderInspector === 'function') {
      options.renderInspector();
    }
    return true;
  }

  return {
    startSeed: startSeed,
    reset: reset,
    setNodeStatus: setNodeStatus,
    applyChange: applyChange,
  };
}

/* ===== migrated source: orchestration-field-value.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-field-value.js — pure FieldSpec draft-value codec

   Owns the browser codec for the versioned field-value contract. Inspector
   renderers declare only a FieldSpec kind; every mutation surface uses this
   codec to produce the backend wire shape. No DOM or graph dependency.

   MUST load before orchestration-node-editor.js.
   ═══════════════════════════════════════════════════════════════════ */


function orchestrationFieldValueContract(contractSource) {
  return orchestrationDirectContract(contractSource);
}


function _orchestrationFieldValuePolicy(contract, kind) {
  if (!contract) return null;
  if (!orchestrationWireContractSpec('field-value', contract).supported) {
    return false;
  }
  var policy = contract.kinds && contract.kinds[kind];
  return policy && typeof policy === 'object' ? policy : false;
}


function _orchestrationFieldWireSupported(kind, policy) {
  if (!policy) return policy === null;
  var expected = {
    text: 'string', textarea: 'string', select: 'declared option',
    list: 'array<string>', int: 'integer', bool: 'boolean',
  }[kind];
  return !!expected && policy.wire === expected;
}


function _orchestrationFieldPositiveLimit(spec, key) {
  var value = spec && typeof spec === 'object' ? Number(spec[key]) : 0;
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}


var _ORCHESTRATION_FIELD_FAILURES = {
  unsupportedContract: [
    'unsupported-contract', 'field.contract.unsupported'],
  invalidNumber: ['invalid-number', 'field.type.integer'],
  invalidBoolean: ['invalid-boolean', 'field.type.boolean'],
  maxLength: ['max-length', 'field.max_length'],
  maxItems: ['max-items', 'field.max_items'],
  maxItemLength: ['max-item-length', 'field.max_item_length'],
};


function _orchestrationFieldFailure(
  contract, failure, present, value, limit
) {
  var policy = _ORCHESTRATION_FIELD_FAILURES[failure];
  var published = contract && contract.failureCodes;
  var code = published && typeof published[failure] === 'string'
    && published[failure] ? published[failure] : policy[1];
  var result = {
    ok: false, present: !!present, value: value,
    reason: policy[0], code: code,
  };
  if (limit != null) result.limit = limit;
  return result;
}


function normalizeOrchestrationFieldDraftValue(
  kind, rawValue, spec, contractSource
) {
  kind = String(kind || (typeof rawValue === 'boolean' ? 'bool' : 'text'));
  var contract = orchestrationFieldValueContract(contractSource);
  var policy = _orchestrationFieldValuePolicy(contract, kind);
  if (!_orchestrationFieldWireSupported(kind, policy)) {
    return _orchestrationFieldFailure(
      contract, 'unsupportedContract', false, null);
  }

  if (kind === 'list') {
    var source = Array.isArray(rawValue) ? rawValue
      : !policy || policy.editor === 'newline'
        ? String(rawValue == null ? '' : rawValue).split('\n') : null;
    if (!source) {
      return _orchestrationFieldFailure(
        contract, 'unsupportedContract', false, null);
    }
    var items = source
      .map(function (item) {
        item = String(item == null ? '' : item);
        return !policy || policy.trimItems === true ? item.trim() : item;
      })
      .filter(function (item) {
        return !policy || policy.dropEmptyItems === true ? !!item : true;
      });
    var maxItems = _orchestrationFieldPositiveLimit(spec, 'maxItems');
    if (maxItems != null && items.length > maxItems) {
      return _orchestrationFieldFailure(
        contract, 'maxItems', true, items, maxItems);
    }
    var maxItemLength = _orchestrationFieldPositiveLimit(
      spec, 'maxItemLength');
    if (maxItemLength != null && items.some(function (item) {
      return item.length > maxItemLength;
    })) {
      return _orchestrationFieldFailure(
        contract, 'maxItemLength', true, items, maxItemLength);
    }
    return { ok: true, present: items.length > 0, value: items };
  }

  if (rawValue == null || rawValue === '') {
    if (contract && contract.optionalEmpty !== 'omit') {
      return _orchestrationFieldFailure(
        contract, 'unsupportedContract', false, null);
    }
    return { ok: true, present: false, value: null };
  }

  if (kind === 'int') {
    var numeric = Number(rawValue);
    // Finite but non-integral values remain in the draft so the shared
    // backend inspection can explain min/max/integer violations. NaN and
    // Infinity cannot cross JSON faithfully (both collapse to null), so they
    // are rejected before they can silently erase a parameter.
    if (!Number.isFinite(numeric)) {
      return _orchestrationFieldFailure(
        contract, 'invalidNumber', false, null);
    }
    return { ok: true, present: true, value: numeric };
  }

  if (kind === 'bool') {
    if (typeof rawValue !== 'boolean') {
      return _orchestrationFieldFailure(
        contract, 'invalidBoolean', false, null);
    }
    return { ok: true, present: true, value: rawValue };
  }

  var maxLength = _orchestrationFieldPositiveLimit(spec, 'maxLength');
  if (maxLength != null && typeof rawValue === 'string'
      && rawValue.length > maxLength) {
    return _orchestrationFieldFailure(
      contract, 'maxLength', true, rawValue, maxLength);
  }

  return { ok: true, present: true, value: rawValue };
}


function orchestrationFieldDraftValuesEqual(left, right) {
  if (Array.isArray(left) || Array.isArray(right)) {
    if (!Array.isArray(left) || !Array.isArray(right)
        || left.length !== right.length) return false;
    return left.every(function (value, index) { return value === right[index]; });
  }
  return left === right;
}

/* ===== migrated source: orchestration-field-validity.js ===== */
/* Multi-source Inspector field validity projected onto accessible DOM state. */

function createOrchestrationFieldValidity() {
  function _has(field, name) {
    return typeof field.hasAttribute === 'function'
      ? field.hasAttribute(name) : field.getAttribute(name) != null;
  }

  function _sync(field) {
    var invalid = _has(field, 'data-orch-local-invalid')
      || field.getAttribute('data-orch-diagnostic-focus') === 'error';
    if (invalid) field.setAttribute('aria-invalid', 'true');
    else field.removeAttribute('aria-invalid');
    return invalid;
  }

  function _description(field, ownerAttribute, descriptionId) {
    var owned = field.getAttribute(ownerAttribute) || '';
    var tokens = String(field.getAttribute('aria-describedby') || '')
      .split(/\s+/).filter(function (token) {
        return token && token !== owned;
      });
    if (descriptionId && tokens.indexOf(descriptionId) < 0) {
      tokens.push(descriptionId);
      field.setAttribute(ownerAttribute, descriptionId);
    } else field.removeAttribute(ownerAttribute);
    if (tokens.length) field.setAttribute('aria-describedby', tokens.join(' '));
    else field.removeAttribute('aria-describedby');
  }

  function setLocal(field, accepted, descriptionId, code) {
    if (!field) return !!accepted;
    if (accepted) {
      field.removeAttribute('data-orch-local-invalid');
      field.removeAttribute('data-orch-local-failure-code');
    } else {
      field.setAttribute('data-orch-local-invalid', 'true');
      if (code) field.setAttribute('data-orch-local-failure-code', code);
      else field.removeAttribute('data-orch-local-failure-code');
    }
    _description(field, 'data-orch-local-description-id',
      accepted ? '' : descriptionId || '');
    _sync(field);
    return !!accepted;
  }

  function setDiagnostic(field, severity, descriptionId) {
    if (!field) return false;
    if (severity) {
      field.setAttribute('data-orch-diagnostic-focus', severity);
    } else field.removeAttribute('data-orch-diagnostic-focus');
    _description(field, 'data-orch-diagnostic-description-id',
      descriptionId || '');
    _sync(field);
    return true;
  }

  function clearDiagnostics(root) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    Array.prototype.forEach.call(
      root.querySelectorAll('[data-orch-diagnostic-focus]'),
      function (field) { setDiagnostic(field, ''); });
  }

  return Object.freeze({ setLocal: setLocal, setDiagnostic: setDiagnostic,
    clearDiagnostics: clearDiagnostics });
}

/* ===== migrated source: orchestration-inspector.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-inspector.js — pure Studio inspector field renderer

   Converts backend FieldSpec contracts and common node settings into safe
   form markup. It owns no selection, graph state, or executable DOM handlers;
   orchestration-inspector-view.js binds its data-marked fields after render.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationInspectorRenderer(options) {
  options = options || {};

  function esc(value) {
    return options.escape ? options.escape(value == null ? '' : value)
      : String(value == null ? '' : value);
  }
  function tr(key, params) {
    return options.translate ? options.translate(key, params) : key;
  }
  function paramAttrs(key, kind) {
    var attrs = ' data-orch-param-key="' + esc(key) + '"';
    if (kind) attrs += ' data-orch-param-kind="' + esc(kind) + '"';
    return attrs;
  }

  function selectField(label, key, value, choices) {
    var optionHtml = (choices || []).map(function (choice) {
      return '<option value="' + esc(choice[0]) + '"'
        + (choice[0] === value ? ' selected' : '')
        + (choice[2] ? ' disabled' : '') + '>'
        + esc(choice[1]) + '</option>';
    }).join('');
    return '<label class="orch-fld"><span>' + esc(label) + '</span>'
      + '<select class="orch-input"' + paramAttrs(key, 'select')
      + '>' + optionHtml + '</select></label>';
  }

  function numberField(label, key, value, spec) {
    spec = spec || {};
    var bounds = '';
    if (spec.min != null) bounds += ' min="' + esc(spec.min) + '"';
    var maximum = spec.max;
    if (spec.runtimeMax != null
        && (maximum == null || spec.runtimeMax < maximum)) {
      maximum = spec.runtimeMax;
    }
    if (maximum != null) bounds += ' max="' + esc(maximum) + '"';
    return '<label class="orch-fld">' + fieldHeading(label, spec)
      + '<input type="number" class="orch-input" value="'
      + esc(value != null ? value : '') + '"' + bounds
      + paramAttrs(key, 'int') + '></label>';
  }

  function checkField(label, key, value) {
    return '<label class="orch-fld orch-fld-check">'
      + '<span>' + esc(label) + '</span>'
      + '<span class="stg-toggle stg-dv-toggle">'
      + '<input type="checkbox"' + (value ? ' checked' : '')
      + paramAttrs(key, 'bool') + '>'
      + '<span class="stg-toggle-track"><span class="stg-toggle-thumb">'
      + '</span></span></span></label>';
  }

  function fieldLimit(spec) {
    spec = spec || {};
    if (spec.kind === 'int' && spec.runtimeMax != null) {
      return '≤ ' + spec.runtimeMax;
    }
    if (spec.kind === 'list' && spec.maxItems) {
      return tr('orch.field.limitItems', {
        n: spec.maxItems, m: spec.maxItemLength || '',
      });
    }
    return spec.maxLength
      ? tr('orch.field.limitChars', { n: spec.maxLength }) : '';
  }

  function fieldHeading(label, spec) {
    var limit = fieldLimit(spec);
    return '<span>' + esc(label) + (limit
      ? '<small class="orch-fld-limit">' + esc(limit) + '</small>' : '')
      + '</span>';
  }

  function textLimitAttrs(spec, kind) {
    spec = spec || {};
    if (spec.maxLength) return ' maxlength="' + esc(spec.maxLength) + '"';
    if (kind !== 'list') return '';
    var attrs = '';
    if (spec.maxItems) {
      attrs += ' data-orch-param-max-items="' + esc(spec.maxItems) + '"';
    }
    if (spec.maxItemLength) {
      attrs += ' data-orch-param-max-item-length="'
        + esc(spec.maxItemLength) + '"';
    }
    return attrs;
  }

  function textField(label, key, value, placeholder, spec) {
    return '<label class="orch-fld">' + fieldHeading(label, spec)
      + '<input class="orch-input" value="' + esc(value || '') + '" '
      + 'placeholder="' + esc(placeholder || '') + '"'
      + textLimitAttrs(spec, 'text')
      + paramAttrs(key, 'text') + '></label>';
  }

  function textareaField(label, key, value, placeholder, kind, rows, spec) {
    return '<label class="orch-fld">' + fieldHeading(label, spec)
      + '<textarea class="orch-input orch-ta" rows="' + (rows || 5) + '" '
      + 'placeholder="' + esc(placeholder || '') + '"'
      + textLimitAttrs(spec, kind || 'textarea')
      + paramAttrs(key, kind || 'textarea') + '>' + esc(value || '')
      + '</textarea></label>';
  }

  function labelField(node, automaticLabel) {
    return textField(tr('orch.fld.label'), 'name', node.name || '', automaticLabel);
  }

  function schemaField(spec, value) {
    spec = spec || {};
    var key = spec.key || '';
    var kind = spec.kind || 'text';
    var label = tr(spec.label || key);
    var placeholder = spec.placeholder ? tr(spec.placeholder) : '';
    if (kind === 'bool') return checkField(label, key, value === true);
    if (kind === 'int') return numberField(label, key, value, spec);
    if (kind === 'select') {
      var choices = [['', tr('orch.opt.unset')]].concat(
        (spec.options || []).map(function (choice) {
          return [
            choice.value,
            tr(choice.label || choice.value),
            choice.disabled === true,
          ];
        }));
      if (spec.allowUnknown && value != null && value !== ''
          && !choices.some(function (choice) {
            return choice[0] === value;
          })) {
        choices.push([value, String(value)]);
      }
      return selectField(label, key, value || '', choices);
    }
    if (kind === 'list') {
      var listValue = Array.isArray(value) ? value.join('\n') : (value || '');
      return textareaField(label, key, listValue, placeholder, 'list', 4, spec);
    }
    if (kind === 'textarea') {
      return textareaField(
        label, key, value || '', placeholder, 'textarea', 5, spec);
    }
    return textField(label, key, value || '', placeholder, spec);
  }

  function nodeValue(node, key, nodeParam) {
    if (typeof nodeParam === 'function') return nodeParam(node, key);
    var params = node && node.params;
    return params && typeof params === 'object'
      && Object.prototype.hasOwnProperty.call(params, key)
      ? params[key] : null;
  }

  function schemaSection(node, fields, nodeParam) {
    fields = fields || [];
    return fields.map(function (spec) {
      var visibility = spec.visibleWhen;
      if (visibility && nodeValue(node, visibility.key, nodeParam)
          !== visibility.equals) {
        return '';
      }
      return schemaField(spec, nodeValue(node, spec.key, nodeParam));
    }).join('');
  }

  function roleTaskSection(node, roleSchemas, genericSchema, nodeParam) {
    var fields = roleSchemas && roleSchemas[node.role] || genericSchema || [];
    return schemaSection(node, fields, nodeParam);
  }

  return {
    selectField: selectField,
    numberField: numberField,
    checkField: checkField,
    textField: textField,
    textareaField: textareaField,
    labelField: labelField,
    schemaField: schemaField,
    schemaSection: schemaSection,
    roleTaskSection: roleTaskSection,
  };
}

/* ===== migrated source: orchestration-io-tools.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-io-tools.js — pure Typed-I/O contract consumer

   Owns backend ioContract adoption, defaults, caps, presets and immutable
   port edits. It has no DOM or graph-state dependency; Inspector rendering
   HTML projection lives in orchestration-io-presentation.js; mutations and
   event binding live in orchestration-io.js.

   MUST load before orchestration-io.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationIoTools(initialContract) {
  var contract = null;
  var fallbackCodes = {
    maxPorts: 'io.side.max_ports', missingPort: 'io.port.missing',
    missingPortName: 'io.port.name.required', duplicatePortName:
      'io.port.name.duplicate', missingPreset: 'io.preset.missing',
  };

  function failureCode(name) {
    var published = contract && contract.failureCodes;
    return published && published[name] || fallbackCodes[name] || '';
  }

  function reject(name, reason, details) {
    return Object.assign({ ok: false, changed: false,
      code: failureCode(name), reason: reason }, details || {});
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function setContract(next) {
    if (!next || !Array.isArray(next.types) || !next.types.length) return false;
    contract = clone(next);
    return true;
  }

  function getContract() { return contract ? clone(contract) : null; }

  function types() { return contract ? contract.types.slice() : []; }

  function defaultOutput() {
    var value = contract && contract.defaultOutput;
    return { name: value && value.name || 'text',
      type: value && value.type || 'text' };
  }

  function maxPorts() { var value = contract && Number(contract.maxPorts);
    return value > 0 ? value : null; }

  function portNameRules() { var value = contract && contract.portName;
    return value && typeof value === 'object' ? clone(value) : {}; }

  function startRef() { return contract && contract.startRef || 'start'; }

  function nodeInputs(node) { var io = node && node.params && node.params.io;
    return io && Array.isArray(io.inputs) ? io.inputs : []; }

  function nodeOutputs(node) { var io = node && node.params && node.params.io;
    if (io && Array.isArray(io.outputs) && io.outputs.length) return io.outputs;
    return [defaultOutput()]; }

  function outputRef(nodeId, outputs, port) { var implicit = defaultOutput();
    return port.name === implicit.name && outputs.length === 1
      ? nodeId : nodeId + '.' + port.name; }

  function nextPortName(ports, side) {
    var stem = side === 'outputs' ? 'out' : 'in';
    var used = {}, index = 1;
    ports.forEach(function (port) { used[port && port.name] = true; });
    while (used[stem + index]) index += 1;
    return stem + index;
  }

  function addPort(io, side) {
    var next = clone(io || {});
    var ports = Array.isArray(next[side]) ? next[side].slice() : [];
    var cap = maxPorts();
    if (cap !== null && ports.length >= cap) {
      return reject('maxPorts', 'max-ports', { maxPorts: cap, io: next });
    }
    var fallback = defaultOutput();
    ports.push({ name: nextPortName(ports, side), type: fallback.type });
    next[side] = ports;
    return { ok: true, changed: true, reason: '',
      io: next, index: ports.length - 1 };
  }

  function removePort(io, side, index) {
    var next = clone(io || {});
    if (!Array.isArray(next[side]) || index < 0 || index >= next[side].length) {
      return reject('missingPort', 'missing-port', { io: next });
    }
    next[side].splice(index, 1);
    if (!next[side].length) delete next[side];
    return { ok: true, changed: true, reason: '',
      io: Object.keys(next).length ? next : null };
  }

  function setPort(io, side, index, key, value) {
    var next = clone(io || {});
    if (!Array.isArray(next[side]) || !next[side][index]) {
      return reject('missingPort', 'missing-port', { io: next });
    }
    if (key === 'name') {
      var rules = portNameRules();
      if (rules.required && (typeof value !== 'string' || !value.trim())) {
        return reject('missingPortName', 'missing-port-name', { io: next });
      }
      var duplicate = rules.uniqueWithinSide && next[side].some(
        function (port, candidateIndex) {
          return candidateIndex !== index && port && port.name === value;
        }
      );
      if (duplicate) {
        return reject('duplicatePortName', 'duplicate-port-name', { io: next });
      }
    }
    var port = next[side][index];
    if (key === 'from' && !value) {
      if (!Object.prototype.hasOwnProperty.call(port, 'from')) {
        return { ok: true, changed: false, reason: '', io: next };
      }
      delete port.from;
    } else {
      if (port[key] === value) {
        return { ok: true, changed: false, reason: '', io: next };
      }
      port[key] = value;
    }
    return { ok: true, changed: true, reason: '', io: next };
  }

  function preset(name) { var spec = contract
    && contract.presets && contract.presets[name];
    return spec ? clone(spec) : null; }

  function applyPreset(io, name) {
    var spec = preset(name);
    if (!spec) return reject('missingPreset', 'missing-preset', {
      io: clone(io || {}),
    });
    var outputs = Array.isArray(spec.outputs) ? spec.outputs : [];
    var cap = maxPorts();
    if (cap !== null && outputs.length > cap) {
      return reject('maxPorts', 'max-ports',
        { maxPorts: cap, io: clone(io || {}) });
    }
    var next = clone(io || {});
    if (JSON.stringify(next.outputs || []) === JSON.stringify(outputs)) {
      return { ok: true, changed: false, reason: '', io: next };
    }
    next.outputs = clone(outputs);
    return { ok: true, changed: true, reason: '', io: next };
  }

  setContract(initialContract);
  return {
    setContract: setContract,
    getContract: getContract,
    types: types,
    defaultOutput: defaultOutput,
    maxPorts: maxPorts,
    portNameRules: portNameRules,
    failureCode: failureCode,
    startRef: startRef,
    nodeInputs: nodeInputs,
    nodeOutputs: nodeOutputs,
    outputRef: outputRef,
    addPort: addPort,
    removePort: removePort,
    setPort: setPort,
    preset: preset,
    applyPreset: applyPreset,
  };
}

/* ===== migrated source: orchestration-io-presentation.js ===== */
/* Pure Typed-I/O Inspector option and HTML projection. */

function createOrchestrationIoPresentation(options) {
  options = options || {};
  var ioTools = options.ioTools;
  var escape = options.escape || function (value) { return String(value || ''); };
  var translate = options.translate || function (key) { return key; };
  var icons = options.icons || {};

  function _nodes() {
    return typeof options.nodes === 'function' ? options.nodes() : [];
  }

  function _edges() {
    return typeof options.edges === 'function' ? options.edges() : [];
  }

  function _find(id) {
    if (typeof options.findNode === 'function') return options.findNode(id);
    return _nodes().filter(function (node) { return node.id === id; })[0] || null;
  }

  function _label(node) {
    return typeof options.nodeLabel === 'function'
      ? options.nodeLabel(node) : (node.name || node.id);
  }

  function upstreamIds(id) {
    var seen = {};
    var stack = [id];
    while (stack.length) {
      var current = stack.pop();
      _edges().forEach(function (edge) {
        if (edge.to === current && !seen[edge.from]) {
          seen[edge.from] = true;
          stack.push(edge.from);
        }
      });
    }
    return seen;
  }

  function fromOptions(self, currentRef) {
    var upstream = upstreamIds(self.id);
    var startRef = ioTools.startRef();
    var choices = [
      ['', translate('orch.edge.bindNone')],
      [startRef, translate('orch.io.fromStart')],
    ];
    var currentListed = !currentRef || currentRef === startRef;
    _nodes().forEach(function (node) {
      if (node.id === self.id || node.kind === 'start' || node.kind === 'stop') return;
      if (!upstream[node.id]) return;
      var outputs = ioTools.nodeOutputs(node);
      outputs.forEach(function (port) {
        var ref = ioTools.outputRef(node.id, outputs, port);
        choices.push([ref, _label(node) + ' · ' + port.name]);
        if (ref === currentRef) currentListed = true;
      });
    });
    if (!currentListed) {
      var dot = currentRef.indexOf('.');
      var sourceId = dot === -1 ? currentRef : currentRef.slice(0, dot);
      var source = _find(sourceId);
      choices.push([
        currentRef,
        translate('orch.io.fromStale', {
          node: source ? _label(source) : sourceId,
        }),
      ]);
    }
    return choices.map(function (choice) {
      return '<option value="' + escape(choice[0]) + '"'
        + (choice[0] === currentRef ? ' selected' : '') + '>'
        + escape(choice[1]) + '</option>';
    }).join('');
  }

  function _typeOptions(current) {
    var types = ioTools.types();
    if (current && types.indexOf(current) === -1) types.unshift(current);
    if (!types.length) types.push(ioTools.defaultOutput().type);
    return types.map(function (type) {
      return '<option value="' + escape(type) + '"'
        + (type === current ? ' selected' : '') + '>'
        + escape(type) + '</option>';
    }).join('');
  }

  function _portRow(side, port, index) {
    var defaultType = ioTools.defaultOutput().type;
    var nameRules = typeof ioTools.portNameRules === 'function'
      ? ioTools.portNameRules() : {};
    return '<div class="orch-io-port">'
      + '<input class="orch-input orch-io-name" value="' + escape(port.name || '') + '" '
      + 'placeholder="' + escape(translate('orch.io.namePlaceholder')) + '" '
      + 'aria-label="' + escape(translate('orch.io.namePlaceholder')) + '" '
      + 'data-orch-io-action="set" data-orch-io-side="' + side + '" '
      + 'data-orch-io-index="' + index + '" data-orch-io-key="name"'
      + (nameRules.required ? ' required' : '') + '>'
      + '<select class="orch-input orch-io-type" aria-label="'
      + escape(translate('orch.io.typeLabel')) + '" '
      + 'data-orch-io-action="set" data-orch-io-side="' + side + '" '
      + 'data-orch-io-index="' + index + '" data-orch-io-key="type">'
      + _typeOptions(port.type || defaultType) + '</select>'
      + '<button type="button" class="orch-io-del" title="' + escape(translate('orch.io.removePort'))
      + '" aria-label="' + escape(translate('orch.io.removePort')) + '" '
      + 'data-orch-io-action="remove" data-orch-io-side="' + side + '" '
      + 'data-orch-io-index="' + index + '">'
      + (icons.reject || '×') + '</button></div>';
  }

  function sectionBody(node) {
    var io = node.params && node.params.io || {};
    var inputs = Array.isArray(io.inputs) ? io.inputs : [];
    var outputs = Array.isArray(io.outputs) ? io.outputs : [];
    var html = '<div class="orch-io-head">'
      + escape(translate('orch.io.outputs')) + '</div>';
    if (!outputs.length) {
      html += '<div class="orch-io-implicit">'
        + escape(translate('orch.io.implicitOut')) + '</div>';
    }
    outputs.forEach(function (port, index) {
      html += _portRow('outputs', port, index);
    });
    html += '<button type="button" class="orch-btn orch-btn-ghost orch-io-add" '
      + 'data-orch-io-action="add" data-orch-io-side="outputs">'
      + icons.plus + ' ' + escape(translate('orch.io.addOutput')) + '</button>';

    html += '<div class="orch-io-head orch-io-head-in">'
      + escape(translate('orch.io.inputs')) + '</div>';
    if (inputs.length) {
      html += '<div class="orch-io-subhint">'
        + escape(translate('orch.io.inputsHint')) + '</div>';
    }
    var upstream = upstreamIds(node.id);
    var hasUpstream = _nodes().some(function (candidate) {
      return candidate.id !== node.id && candidate.kind !== 'start'
        && candidate.kind !== 'stop' && upstream[candidate.id];
    });
    inputs.forEach(function (port, index) {
      html += '<div class="orch-io-portbox">'
        + _portRow('inputs', port, index)
        + '<div class="orch-io-fromrow"><span class="orch-io-fromlbl">'
        + escape(translate('orch.io.fromLabel')) + '</span>'
        + '<select class="orch-input orch-io-from" aria-label="'
        + escape(translate('orch.io.fromLabel')) + '" '
        + 'data-orch-io-action="set" data-orch-io-side="inputs" '
        + 'data-orch-io-index="' + index + '" data-orch-io-key="from">'
        + fromOptions(node, port.from) + '</select></div></div>';
    });
    if (inputs.length && !hasUpstream) {
      html += '<div class="orch-io-empty">'
        + escape(translate('orch.io.noUpstream')) + '</div>';
    }
    html += '<button type="button" class="orch-btn orch-btn-ghost orch-io-add" '
      + 'data-orch-io-action="add" data-orch-io-side="inputs">'
      + icons.plus + ' ' + escape(translate('orch.io.addInput')) + '</button>';

    if (node.type === 'role') {
      var preset = ioTools.preset('toolHeavyWorker');
      if (preset && (!preset.appliesTo || preset.appliesTo.indexOf('role') !== -1)) {
        html += '<div class="orch-io-subhint">'
          + escape(translate('orch.io.presetHint')) + '</div>'
          + '<button type="button" class="orch-btn orch-btn-ghost orch-io-preset" '
          + 'data-orch-io-action="preset" data-orch-io-preset="toolHeavyWorker">'
          + escape(translate('orch.io.toolHeavyPreset')) + '</button>';
      }
    }
    return html;
  }

  return {
    sectionBody: sectionBody,
    upstreamIds: upstreamIds,
    fromOptions: fromOptions,
  };
}

/* ===== migrated source: orchestration-io.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-io.js — Typed-I/O Inspector editor

   Applies immutable I/O edits and binds the Inspector controls. Pure option
   and HTML projection lives in orchestration-io-presentation.js; contract
   adoption and immutable port operations live in orchestration-io-tools.js.

   MUST load after orchestration-io-tools.js and before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationIoEditor(options) {
  options = options || {};
  var ioTools = options.ioTools;
  var translate = options.translate || function (key) { return key; };
  var validity = options.fieldValidity
    || createOrchestrationFieldValidity();
  var presentation = options.presentation
    || createOrchestrationIoPresentation(options);

  function _nodes() {
    return typeof options.nodes === 'function' ? options.nodes() : [];
  }

  function _find(id) {
    if (typeof options.findNode === 'function') return options.findNode(id);
    return _nodes().filter(function (node) { return node.id === id; })[0] || null;
  }

  function _selectedNode() {
    return typeof options.selectedNode === 'function'
      ? options.selectedNode() : null;
  }

  function _targetNode(nodeId) {
    return nodeId ? _find(nodeId) : _selectedNode();
  }

  function _notifyChange(renderInspector, renderNodes, historyGroup) {
    if (typeof options.onChange === 'function') {
      options.onChange({
        renderInspector: !!renderInspector,
        renderNodes: !!renderNodes,
        historyGroup: historyGroup || '',
      });
    }
  }

  function _reject(result) {
    var maxPortsCode = typeof ioTools.failureCode === 'function'
      ? ioTools.failureCode('maxPorts') : '';
    if (result && (result.code
      ? result.code === maxPortsCode : result.reason === 'max-ports')
        && typeof options.toast === 'function') {
      options.toast(translate('orch.io.maxPorts', { n: result.maxPorts }), true);
    }
    return false;
  }

  function _adoptResult(node, result, renderInspector, renderNodes,
                        historyGroup) {
    if (!result || !result.ok) {
      _reject(result);
      return result || { ok: false, reason: 'invalid-result' };
    }
    var changed = result.changed !== false;
    if (changed) {
      node.params = node.params || {};
      if (result.io) node.params.io = result.io;
      else delete node.params.io;
      _notifyChange(renderInspector, renderNodes, historyGroup);
    }
    return { ok: true, changed: changed,
      code: result.code || '', reason: result.reason || '' };
  }

  function _adopt(node, result, renderInspector, renderNodes, historyGroup) {
    return _adoptResult(
      node, result, renderInspector, renderNodes, historyGroup).ok;
  }

  function add(side, nodeId) {
    var node = _targetNode(nodeId);
    return node
      ? _adopt(node, ioTools.addPort(node.params && node.params.io, side), true, true)
      : false;
  }

  function remove(side, index, nodeId) {
    var node = _targetNode(nodeId);
    return node
      ? _adopt(node, ioTools.removePort(node.params && node.params.io, side, index), true, true)
      : false;
  }

  function setResult(side, index, key, value, nodeId, coalesce) {
    var node = _targetNode(nodeId);
    return node
      ? _adoptResult(node, ioTools.setPort(
          node.params && node.params.io, side, index, key, value),
          false, key !== 'name', coalesce
            ? 'io:' + node.id + ':' + side + ':' + index + ':' + key : '')
      : { ok: false, reason: 'missing-target' };
  }

  function set(side, index, key, value, nodeId, coalesce) {
    return setResult(side, index, key, value, nodeId, coalesce).ok;
  }

  function bindInput(targetId, index, ref) {
    var node = _find(targetId);
    return node
      ? _adopt(node, ioTools.setPort(node.params && node.params.io,
          'inputs', index, 'from', ref),
          false, true)
      : false;
  }

  function applyPreset(name, nodeId) {
    var node = _targetNode(nodeId);
    return node
      ? _adopt(node, ioTools.applyPreset(node.params && node.params.io, name), true, true)
      : false;
  }

  function bindSection(element, nodeId) {
    if (!element || typeof element.querySelectorAll !== 'function') return;
    Array.prototype.forEach.call(
      element.querySelectorAll('[data-orch-io-action]'), function (control) {
        var action = control.getAttribute('data-orch-io-action');
        var side = control.getAttribute('data-orch-io-side') || '';
        var index = Number(control.getAttribute('data-orch-io-index'));
        if (action === 'set') {
          var eventName = control.tagName === 'SELECT' ? 'change' : 'input';
          control.addEventListener(eventName, function () {
            var result = setResult(side, index,
                control.getAttribute('data-orch-io-key') || '', control.value,
                nodeId, eventName === 'input');
            validity.setLocal(control, result.ok, '',
              String(result.code || result.reason || ''));
          });
        } else if (action === 'add') {
          control.addEventListener('click', function () { add(side, nodeId); });
        } else if (action === 'remove') {
          control.addEventListener('click', function () { remove(side, index, nodeId); });
        } else if (action === 'preset') {
          control.addEventListener('click', function () {
            applyPreset(control.getAttribute('data-orch-io-preset') || '', nodeId);
          });
        }
      }
    );
  }

  return {
    sectionBody: presentation.sectionBody,
    upstreamIds: presentation.upstreamIds,
    fromOptions: presentation.fromOptions,
    bindSection: bindSection,
    add: add,
    remove: remove,
    set: set,
    setResult: setResult,
    bindInput: bindInput,
    applyPreset: applyPreset,
  };
}

/* ===== migrated source: orchestration-contract-sections.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-contract-sections.js — detached contract-section store

   The authoring response carries several independently consumed policy
   documents. The authoring-contract validator owns their shared name registry;
   this data-driven store owns immutable adoption and completeness state.

   Pure state only: no DOM, transport or localization dependencies.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationContractSectionStore(options) {
  options = options || {};
  var keys = Array.isArray(options.keys)
    ? options.keys.slice() : ORCHESTRATION_AUTHORING_OBJECT_SECTIONS.slice();
  var required = Array.isArray(options.required)
    ? options.required.slice() : keys.slice();
  var values = Object.create(null);

  function _clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }

  function _record(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }

  function adopt(source) {
    source = source && typeof source === 'object' ? source : {};
    keys.forEach(function (key) {
      if (_record(source[key])) values[key] = _clone(source[key]);
    });
    return snapshot();
  }

  function get(key) {
    return keys.indexOf(key) >= 0 ? _clone(values[key] || null) : null;
  }

  function has(key) {
    return _record(values[key]) && Object.keys(values[key]).length > 0;
  }

  function missing() {
    return required.filter(function (key) { return !has(key); });
  }

  function ready() {
    return missing().length === 0;
  }

  function snapshot() {
    var result = {};
    keys.forEach(function (key) { result[key] = get(key); });
    return result;
  }

  adopt(options.initial);
  return {
    adopt: adopt,
    get: get,
    has: has,
    missing: missing,
    ready: ready,
    snapshot: snapshot,
  };
}

/* ===== migrated source: orchestration-contract-loader.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-contract-loader.js — status-aware contract transport

   Owns single-flight reads, retry state and transport diagnostics. Contract
   normalization/application stays
   in orchestration-contract.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationContractLoader(options) {
  options = options || {};
  var flights = createOrchestrationSingleFlight();
  var isSettled = false;
  var lastError = null;

  function _snapshot() {
    return typeof options.snapshot === 'function' ? options.snapshot() : {};
  }

  function _failure(read, endpoint) {
    var reason = read.notFound ? 'not-found'
      : read.unsupportedFormat ? 'unsupported-format'
        : read.malformed ? 'malformed-response'
          : read.retryable ? 'temporarily-unavailable' : 'request-rejected';
    return {
      name: 'OrchestrationContractReadError',
      message: read.error || 'Authoring contract read failed',
      endpoint: endpoint,
      status: Number(read.status || 0),
      reason: reason,
      retryable: !!read.retryable,
      missingFields: Array.isArray(read.missingFields)
        ? read.missingFields.slice() : [],
    };
  }

  function load() {
    if (typeof options.ready === 'function' && options.ready()) {
      return Promise.resolve(_snapshot());
    }
    var api = typeof options.api === 'function'
      ? options.api() : (options.api || null);
    var requests = createOrchestrationEndpointRequestClient({
      api: function () { return api; },
      normalizeRead: options.normalizeRead,
    });
    var hasPrimary = requests.available('authoring-contract');
    if (!hasPrimary) {
      isSettled = false;
      lastError = {
        name: 'OrchestrationContractReadError',
        message: 'Authoring contract client is unavailable',
        endpoint: 'authoring-contract', status: 0,
        reason: 'client-contract-missing', retryable: false,
        missingFields: [],
      };
      reportOrchestrationDiagnostic(options.onError, lastError);
      return Promise.resolve(_snapshot());
    }

    return flights.share('contract', function () {
      return Promise.resolve().then(async function () {
        var read = await requests.request('authoring-contract');
        if (read.ok) return read.contract;
        throw _failure(read, 'authoring-contract');
      }).then(function (contract) {
        isSettled = true;
        lastError = null;
        return contract && typeof options.apply === 'function'
          ? options.apply(contract) : _snapshot();
      }).catch(function (error) {
        isSettled = false;
        lastError = error && typeof error === 'object' ? error : {
          name: 'OrchestrationContractReadError',
          message: String(error || 'Authoring contract read failed'),
          endpoint: '', status: 0,
          reason: 'temporarily-unavailable', retryable: true, missingFields: [],
        };
        reportOrchestrationDiagnostic(options.onError, lastError);
        return _snapshot();
      });
    });
  }

  return {
    load: load,
    settled: function () { return isSettled; },
    error: function () { return lastError; },
  };
}
/* ===== migrated source: orchestration-catalogue-projection.js ===== */
/* Merge backend authoring policy into the frontend presentation catalogue. */

function createOrchestrationCatalogueProjection(options) {
  options = options || {};

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }

  function title(name) {
    return String(name || '').replace(/_/g, ' ')
      .replace(/\b\w/g, function (character) {
        return character.toUpperCase();
      });
  }

  var roles = clone(options.roles || []);
  var controls = clone(options.controls || []);

  function adoptRoles(names, personas, nodeDefaults) {
    if (!Array.isArray(names) || !names.length) return;
    personas = personas && typeof personas === 'object' ? personas : {};
    nodeDefaults = nodeDefaults && typeof nodeDefaults === 'object'
      ? nodeDefaults : {};
    var roleDefaults = nodeDefaults.roles || {};
    var byRole = {};
    roles.forEach(function (role) { byRole[role.role] = role; });
    roles = names.map(function (name) {
      var persona = personas[name] || {};
      var role = clone(byRole[name] || {
        role: name,
        label: title(name),
        icon: 'tofu-general',
        blurb: '',
      });
      var defaults = roleDefaults[name] || nodeDefaults.genericRole || {};
      if (defaults.tier) role.tier = defaults.tier;
      if (persona.tier) role.tier = persona.tier;
      if (persona.whenToUse) role.blurb = persona.whenToUse;
      if (!role.blurb && typeof options.translate === 'function') {
        role.blurb = options.translate('orch.role.genericBlurb');
      }
      return role;
    });
  }

  function adoptControls(contractControls) {
    if (!contractControls || typeof contractControls !== 'object') return;
    var byKind = {};
    controls.forEach(function (control) { byKind[control.kind] = control; });
    var ordered = controls.map(function (control) { return control.kind; })
      .filter(function (kind) {
        return Object.prototype.hasOwnProperty.call(contractControls, kind);
      });
    Object.keys(contractControls).forEach(function (kind) {
      if (ordered.indexOf(kind) === -1) ordered.push(kind);
    });
    controls = ordered.map(function (kind) {
      var control = clone(byKind[kind] || {
        kind: kind,
        label: title(kind),
        glyph: 'branch',
        accent: '#64748b',
        blurb: typeof options.translate === 'function'
          ? options.translate('orch.control.genericBlurb') : '',
      });
      control.single = !!(contractControls[kind] || {}).single;
      return control;
    });
  }

  function snapshot() {
    return {
      roles: clone(roles),
      controls: clone(controls),
    };
  }

  function adopt(source) {
    source = source && typeof source === 'object' ? source : {};
    adoptRoles(source.roleNames, source.personas, source.nodeDefaults);
    adoptControls(source.controls);
    return snapshot();
  }

  return Object.freeze({
    adopt: adopt,
    snapshot: snapshot,
  });
}

/* ===== migrated source: orchestration-contract.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-contract.js — backend authoring-contract controller

   Owns immutable response adoption and backend/frontend catalogue merge.
   The injected loader owns status-aware transport and rolling fallback. It is
   deliberately DOM-free: orchestration.js receives one onChange callback
   and remains a renderer/editor instead of a transport/schema coordinator.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationAuthoringContractController(options) {
  options = options || {};

  function _clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }

  var _catalogue = createOrchestrationCatalogueProjection({
    roles: options.roles,
    controls: options.controls,
    translate: options.translate,
  });
  var initialSections = {};
  ORCHESTRATION_AUTHORING_OBJECT_SECTIONS.forEach(function (name) {
    initialSections[name] = options[name];
  });
  var _sections = createOrchestrationContractSectionStore({
    initial: initialSections,
  });
  var _generic = _clone(options.genericRoleSchema || []);
  var _contractSections = _clone(options.contractSections || null);
  var _loader = createOrchestrationContractLoader({
    api: options.api,
    normalizeRead: options.normalizeRead,
    ready: ready,
    apply: apply,
    snapshot: snapshot,
    onError: options.onError,
  });

  function snapshot() {
    var current = _sections.snapshot();
    var catalogue = _catalogue.snapshot();
    current.roleSchemas = current.roles;
    current.roles = catalogue.roles;
    current.controls = catalogue.controls;
    current.genericRoleSchema = _clone(_generic);
    current.executionOptions = current.executionOptions || {};
    current.nodeDefaults = current.nodeDefaults || {};
    current.requestLimits = current.requestLimits || {};
    current.contractSections = _clone(_contractSections);
    current.ready = ready();
    current.settled = settled();
    current.error = _clone(_loader.error());
    return current;
  }

  function ready() {
    var ioReady = !options.ioTools
      || (typeof options.ioTools.getContract === 'function'
          && !!options.ioTools.getContract());
    return _sections.ready() && _generic.length > 0 && ioReady;
  }

  function settled() {
    return _loader.settled();
  }

  function apply(contract) {
    if (!contract || typeof contract !== 'object') return snapshot();
    _sections.adopt(contract);
    _contractSections = _clone(contract.contractSections || null);
    if (Array.isArray(contract.generic) && contract.generic.length) {
      _generic = _clone(contract.generic);
    }
    if (options.ioTools && typeof options.ioTools.setContract === 'function') {
      var ioContract = _sections.get('ioContract');
      if (ioContract) {
        options.ioTools.setContract(ioContract);
      }
    }
    _catalogue.adopt({
      roleNames: contract.roleNames,
      personas: _sections.get('personas'),
      nodeDefaults: _sections.get('nodeDefaults'),
      controls: contract.controls,
    });

    var current = snapshot();
    if (typeof options.onChange === 'function') options.onChange(current);
    return current;
  }

  function load() {
    return _loader.load();
  }

  function roleFields(role) {
    var schemas = _sections.get('roles');
    var fields = schemas && schemas[role];
    return _clone(fields || _generic);
  }

  function controlFields(kind) {
    var schemas = _sections.get('controlSchemas');
    return _clone((schemas && schemas[kind]) || []);
  }

  function fieldSpec(ownerType, ownerName, key) {
    var fields = ownerType === 'role' ? roleFields(ownerName)
      : ownerType === 'control' ? controlFields(ownerName) : [];
    var match = fields.filter(function (spec) {
      return spec && spec.key === key;
    })[0];
    return _clone(match || null);
  }

  function persona(role) {
    var personas = _sections.get('personas');
    return _clone((personas && personas[role]) || null);
  }

  function defaultEmits(role) {
    var defaults = _sections.get('defaultEmits');
    return defaults && defaults[role] ? defaults[role] : '';
  }

  function section(name) { return _sections.get(name); }
  function executionOptions() {
    return section('executionOptions') || {};
  }
  function requestLimits() { return section('requestLimits') || {}; }

  function blankSubflowDefinition() {
    var defaults = _sections.get('nodeDefaults') || {};
    return _clone(defaults.blankSubflow);
  }

  function nodeParams(payload) {
    payload = payload || {};
    var defaults = _sections.get('nodeDefaults') || {};
    if (payload.ptype === 'role') {
      var roleParams = defaults.roles && defaults.roles[payload.role];
      return _clone(roleParams || defaults.genericRole || {});
    }
    if (payload.ptype === 'subflow') {
      var subflow = _clone(defaults.subflow || {});
      subflow.definition = blankSubflowDefinition();
      return subflow;
    }
    var controls = defaults.controls || {};
    return _clone(controls[payload.kind] || {});
  }

  var controller = {
    apply: apply,
    load: load,
    ready: ready,
    settled: settled,
    snapshot: snapshot,
    roleFields: roleFields,
    controlFields: controlFields,
    fieldSpec: fieldSpec,
    persona: persona,
    defaultEmits: defaultEmits,
    section: section,
    executionOptions: executionOptions,
    requestLimits: requestLimits,
    nodeParams: nodeParams,
    blankSubflowDefinition: blankSubflowDefinition,
  };
  ORCHESTRATION_RUNTIME_CONTRACT_SECTIONS.concat(
    Object.keys(ORCHESTRATION_AUTHORING_WIRE_SECTIONS)
  ).forEach(function (name) {
    if (Object.prototype.hasOwnProperty.call(controller, name)) return;
    controller[name] = function () { return section(name); };
  });
  return controller;
}
/* ===== migrated source: orchestration-layout-contract.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-layout-contract.js — shared display-coordinate policy

   The backend owns graph layout. Studio and Task Mode use this small
   contract only to recognize a complete backend/user layout and to remain
   renderable while a malformed or legacy draft is being repaired.
   ═══════════════════════════════════════════════════════════════════ */


function orchestrationFiniteCoordinate(value) {
  return typeof value === 'number' && Number.isFinite(value);
}


function orchestrationNodeHasPosition(node) {
  var position = node && node.pos;
  return !!position
    && orchestrationFiniteCoordinate(position.x)
    && orchestrationFiniteCoordinate(position.y);
}


function orchestrationNodePosition(node, fallback) {
  var position = node && node.pos || {};
  fallback = fallback || {};
  var fallbackX = orchestrationFiniteCoordinate(fallback.x) ? fallback.x : 20;
  var fallbackY = orchestrationFiniteCoordinate(fallback.y) ? fallback.y : 20;
  return {
    x: orchestrationFiniteCoordinate(position.x) ? position.x : fallbackX,
    y: orchestrationFiniteCoordinate(position.y) ? position.y : fallbackY,
  };
}


function projectOrchestrationLayoutPositions(definition, expectedDefinition) {
  function failure(code, path, cause) {
    var result = { ok: false, reason: 'invalid-layout', code: code, path: path };
    if (cause) result.cause = cause;
    return result;
  }
  try {
    if (!definition || typeof definition !== 'object'
        || Array.isArray(definition)) {
      return failure('definition.type.object', '');
    }
    if (!Array.isArray(definition.nodes)) {
      return failure('definition.nodes.type.array', '/nodes');
    }
    var expected = null;
    if (expectedDefinition !== undefined) {
      if (!expectedDefinition || typeof expectedDefinition !== 'object'
          || Array.isArray(expectedDefinition)
          || !Array.isArray(expectedDefinition.nodes)) {
        return failure('layout.request.definition.invalid', '');
      }
      expected = Object.create(null);
      for (var expectedIndex = 0;
           expectedIndex < expectedDefinition.nodes.length; expectedIndex++) {
        var expectedNode = expectedDefinition.nodes[expectedIndex];
        var expectedId = expectedNode && expectedNode.id;
        if (!expectedNode || typeof expectedNode !== 'object'
            || Array.isArray(expectedNode) || typeof expectedId !== 'string'
            || !expectedId) {
          return failure(
            'layout.request.node.id.required',
            '/nodes/' + expectedIndex + '/id');
        }
        if (Object.prototype.hasOwnProperty.call(expected, expectedId)) {
          return failure(
            'layout.request.node.id.duplicate',
            '/nodes/' + expectedIndex + '/id');
        }
        expected[expectedId] = expectedIndex;
      }
    }
    var positions = Object.create(null);
    var seen = Object.create(null);
    for (var index = 0; index < definition.nodes.length; index++) {
      var node = definition.nodes[index];
      var nodePath = '/nodes/' + index;
      if (!node || typeof node !== 'object' || Array.isArray(node)) {
        return failure('node.type.object', nodePath);
      }
      var id = node.id;
      if (typeof id !== 'string' || !id) {
        return failure('node.id.required', nodePath + '/id');
      }
      if (Object.prototype.hasOwnProperty.call(seen, id)) {
        return failure('node.id.duplicate', nodePath + '/id');
      }
      if (expected
          && !Object.prototype.hasOwnProperty.call(expected, id)) {
        return failure('node.id.unexpected', nodePath + '/id');
      }
      if (!orchestrationNodeHasPosition(node)) {
        return failure('node.pos.finite', nodePath + '/pos');
      }
      seen[id] = true;
      if (expected) delete expected[id];
      positions[id] = { x: node.pos.x, y: node.pos.y };
    }
    if (expected) {
      var missing = Object.keys(expected)[0];
      if (missing !== undefined) {
        return failure(
          'node.id.missing', '/nodes/' + expected[missing] + '/id');
      }
    }
    return { ok: true, positions: positions };
  } catch (cause) {
    return failure('definition.projection.failed', '', cause);
  }
}

/* ===== migrated source: orchestration-graph-topology.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-graph-topology.js — pure Studio topology policy

   Owns node lookup and immutable edge/node mutations. It has no wire-format,
   nested-workspace or DOM dependencies, so structural controllers can share
   one small policy surface.
   ═══════════════════════════════════════════════════════════════════ */


function orchestrationConnections(edges, nodeId) {
  var incoming = [];
  var outgoing = [];
  (Array.isArray(edges) ? edges : []).forEach(function (edge) {
    if (!edge || typeof edge !== 'object') return;
    if (edge.to === nodeId) incoming.push(edge);
    if (edge.from === nodeId) outgoing.push(edge);
  });
  return { incoming: incoming, outgoing: outgoing };
}


function createOrchestrationGraphTopology() {
  function findNode(nodes, id) {
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].id === id) return nodes[i];
    }
    return null;
  }

  function connect(nodes, edges, from, to, edgeId) {
    var source = findNode(nodes, from);
    var target = findNode(nodes, to);
    if (!source || !target) {
      return { ok: false, changed: false, reason: 'missing-node', edges: edges };
    }
    if (from === to) {
      return { ok: false, changed: false, reason: 'self-loop', edges: edges };
    }
    if (target.kind === 'start') {
      return { ok: false, changed: false, reason: 'start-input', edges: edges };
    }
    if (source.kind === 'stop') {
      return { ok: false, changed: false, reason: 'stop-output', edges: edges };
    }
    var duplicate = edges.some(function (edge) {
      return edge.from === from && edge.to === to;
    });
    if (duplicate) {
      return { ok: true, changed: false, reason: 'duplicate', edges: edges };
    }
    var resolvedEdgeId = typeof edgeId === 'function' ? edgeId() : edgeId;
    return {
      ok: true,
      changed: true,
      reason: '',
      edges: edges.concat([{ id: resolvedEdgeId, from: from, to: to }]),
    };
  }

  function deleteNode(nodes, edges, id) {
    return {
      nodes: nodes.filter(function (node) { return node.id !== id; }),
      edges: edges.filter(function (edge) {
        return edge.from !== id && edge.to !== id;
      }),
    };
  }

  function deleteEdge(edges, id) {
    return edges.filter(function (edge) { return edge.id !== id; });
  }

  function reverseEdge(nodes, edges, id) {
    var edge = edges.filter(function (candidate) {
      return candidate.id === id;
    })[0];
    if (!edge) {
      return { ok: false, changed: false, reason: 'missing-edge', edges: edges };
    }
    var nextSource = findNode(nodes, edge.to);
    var nextTarget = findNode(nodes, edge.from);
    if (!nextSource || !nextTarget) {
      return { ok: false, changed: false, reason: 'missing-node', edges: edges };
    }
    if (nextSource.kind === 'stop') {
      return { ok: false, changed: false, reason: 'stop-output', edges: edges };
    }
    if (nextTarget.kind === 'start') {
      return { ok: false, changed: false, reason: 'start-input', edges: edges };
    }
    var duplicate = edges.some(function (candidate) {
      return candidate.id !== id && candidate.from === edge.to
        && candidate.to === edge.from;
    });
    if (duplicate) {
      return { ok: false, changed: false, reason: 'duplicate', edges: edges };
    }
    return {
      ok: true,
      changed: true,
      reason: '',
      edges: edges.map(function (candidate) {
        return candidate.id === id
          ? { id: candidate.id, from: candidate.to, to: candidate.from }
          : candidate;
      }),
    };
  }

  return {
    connections: orchestrationConnections,
    findNode: findNode,
    connect: connect,
    deleteNode: deleteNode,
    deleteEdge: deleteEdge,
    reverseEdge: reverseEdge,
  };
}

/* ===== migrated source: orchestration-graph-workspace.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-graph-workspace.js — nested definition workspace policy

   Owns definition hydration/serialization and pure Group enter/exit/root
   transitions. Topology validation stays in orchestration-graph-topology.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationGraphWorkspace(options) {
  options = options || {};
  var topology = options.topology;
  var schemaId = options.schemaId || orchestrationWireFormat('definition');

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function _projectionFailure(code, path, cause) {
    var result = { ok: false, reason: 'invalid-definition', code: code,
      path: path };
    if (cause) result.cause = cause;
    return result;
  }

  function workspaceFromDefinitionResult(definition, fallbackName) {
    try {
      if (!definition || typeof definition !== 'object'
          || Array.isArray(definition)) {
        return _projectionFailure('definition.type.object', '');
      }
      if (!Array.isArray(definition.nodes)) {
        return _projectionFailure('definition.nodes.type.array', '/nodes');
      }
      if (!Array.isArray(definition.edges)) {
        return _projectionFailure('definition.edges.type.array', '/edges');
      }
      var sourceNodes = definition.nodes;
      var sourceEdges = definition.edges;
      var invalidNode = sourceNodes.findIndex(function (node) {
        return !node || typeof node !== 'object' || Array.isArray(node);
      });
      if (invalidNode >= 0) {
        return _projectionFailure('node.type.object', '/nodes/' + invalidNode);
      }
      var invalidEdge = sourceEdges.findIndex(function (edge) {
        return !edge || typeof edge !== 'object' || Array.isArray(edge);
      });
      if (invalidEdge >= 0) {
        return _projectionFailure('edge.type.object', '/edges/' + invalidEdge);
      }
      return { ok: true, workspace: _workspaceFromDefinition(
        definition, fallbackName) };
    } catch (cause) {
      return _projectionFailure('definition.projection.failed', '', cause);
    }
  }

  function _workspaceFromDefinition(definition, fallbackName) {
    var sourceNodes = definition.nodes;
    var sourceEdges = definition.edges;
    var sequence = 0;
    var needsLayout = false;
    var nodes = sourceNodes.map(function (node) {
      var suffix = /(\d+)$/.exec(node.id || '');
      if (suffix) sequence = Math.max(sequence, parseInt(suffix[1], 10));
      var hasPosition = orchestrationNodeHasPosition(node);
      var position = orchestrationNodePosition(node, { x: 20, y: 20 });
      if (!hasPosition) needsLayout = true;
      return {
        id: node.id,
        type: node.type,
        role: node.role || '',
        kind: node.kind || '',
        x: position.x,
        y: position.y,
        name: node.name || '',
        params: clone(node.params || {}),
      };
    });
    var edges = sourceEdges.map(function (edge) {
      sequence += 1;
      return { id: 'e' + sequence, from: edge.from, to: edge.to };
    });
    return {
      name: definition.name || fallbackName || 'Untitled Flow',
      nodes: nodes,
      edges: edges,
      selected: null,
      sequence: sequence,
      needsLayout: needsLayout,
    };
  }

  function workspaceFromDefinition(definition, fallbackName) {
    var result = workspaceFromDefinitionResult(definition, fallbackName);
    return result.ok ? result.workspace : null;
  }

  function enterGroup(workspace, stack, groupId, fallbackDefinition,
                      fallbackName) {
    var group = topology.findNode(workspace.nodes, groupId);
    if (!group || group.type !== 'subflow') return null;
    var frame = {
      nodes: clone(workspace.nodes),
      edges: clone(workspace.edges),
      sel: workspace.selected,
      seq: workspace.sequence,
      name: workspace.name,
      groupId: groupId,
    };
    var child = group.params && group.params.definition || fallbackDefinition;
    var projected = workspaceFromDefinitionResult(child, fallbackName);
    if (!projected.ok) return null;
    return {
      stack: stack.concat([frame]),
      workspace: projected.workspace,
    };
  }

  function definitionFromState(name, nodes, edges) {
    return {
      schema: schemaId,
      name: name,
      nodes: nodes.map(function (node) {
        return {
          id: node.id,
          type: node.type,
          role: node.role || undefined,
          kind: node.kind || undefined,
          name: node.name || undefined,
          pos: { x: Math.round(node.x), y: Math.round(node.y) },
          params: clone(node.params || {}),
        };
      }),
      edges: edges.map(function (edge) {
        return { from: edge.from, to: edge.to };
      }),
    };
  }

  function exitGroup(workspace, stack) {
    if (!stack.length) return null;
    var childDefinition = definitionFromState(
      workspace.name, workspace.nodes, workspace.edges
    );
    var frame = stack[stack.length - 1];
    var parentNodes = clone(frame.nodes);
    var group = topology.findNode(parentNodes, frame.groupId);
    if (group) {
      group.params = group.params || {};
      group.params.definition = childDefinition;
      delete group.params.ref;
    }
    return {
      stack: stack.slice(0, -1),
      workspace: {
        name: frame.name,
        nodes: parentNodes,
        edges: clone(frame.edges),
        selected: frame.sel,
        sequence: frame.seq,
        needsLayout: false,
      },
    };
  }

  function rootSnapshot(name, nodes, edges, stack) {
    var definition = definitionFromState(name, nodes, edges);
    for (var i = stack.length - 1; i >= 0; i--) {
      var frame = stack[i];
      var parentNodes = clone(frame.nodes);
      var group = topology.findNode(parentNodes, frame.groupId);
      if (group) {
        group.params = group.params || {};
        group.params.definition = definition;
        delete group.params.ref;
      }
      definition = definitionFromState(frame.name, parentNodes, frame.edges);
    }
    return definition;
  }

  return {
    workspaceFromDefinitionResult: workspaceFromDefinitionResult,
    workspaceFromDefinition: workspaceFromDefinition,
    enterGroup: enterGroup,
    exitGroup: exitGroup,
    definitionFromState: definitionFromState,
    rootSnapshot: rootSnapshot,
  };
}

/* ===== migrated source: orchestration-graph-action-context.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-graph-action-context.js — shared graph action ports

   Normalizes live graph/document/view callbacks once for mutation and
   selection collaborators. No graph policy or action sequencing lives here.
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationGraphActionContext(options) {
  options = options || {};

  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return typeof options[name] === 'function'
      ? options[name].apply(null, args) : undefined;
  }

  function nodes() { return call('nodes') || []; }
  function edges() { return call('edges') || []; }
  function setGraph(nextNodes, nextEdges) {
    return call('setGraph', nextNodes, nextEdges);
  }
  function setSelection(nodeId, edgeId) {
    return call('setSelection', nodeId || null, edgeId || null);
  }
  function render(name) { return call(name); }
  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function toast(key, params) {
    if (typeof options.toast === 'function') {
      options.toast(translate(key, params));
    }
  }
  function topologyError(reason, duplicateKey) {
    if (reason === 'start-input') toast('orch.toast.startNoIn');
    if (reason === 'stop-output') toast('orch.toast.stopNoOut');
    if (reason === 'self-loop') toast('orch.toast.selfLoop');
    if (reason === 'duplicate') {
      toast(duplicateKey || 'orch.toast.edgeExists');
    }
  }

  return Object.freeze({
    graph: options.graph,
    limitPolicy: options.limitPolicy || null,
    nodes: nodes,
    edges: edges,
    setGraph: setGraph,
    setSelection: setSelection,
    markDirty: function () { return call('markDirty'); },
    render: render,
    toast: toast,
    topologyError: topologyError,
    selectedNodeId: function () { return call('selectedNodeId') || null; },
    selectedEdgeId: function () { return call('selectedEdgeId') || null; },
    controls: function () { return call('controls') || []; },
    subflowDepth: function () { return call('subflowDepth') || 0; },
    nodeLimit: function () { return call('nodeLimit'); },
    subflowDepthLimit: function () { return call('subflowDepthLimit'); },
    nextId: function (prefix) { return call('nextId', prefix); },
    defaultParams: function (payload) {
      return call('defaultParams', payload) || {};
    },
    isDragging: function () { return !!call('isDragging'); },
    focusSelection: function () { return call('focusSelection'); },
  });
}

/* ===== migrated source: orchestration-graph-mutation-actions.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-graph-mutation-actions.js — structural graph commands
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationGraphMutationActions(context) {
  function findNode(id) {
    return context.graph.findNode(context.nodes(), id);
  }

  function addNode(payload, x, y) {
    payload = payload || {};
    if (payload.ptype === 'control') {
      var control = context.controls().filter(function (item) {
        return item.kind === payload.kind;
      })[0];
      if (control && control.single && context.nodes().some(function (node) {
        return node.kind === payload.kind;
      })) {
        context.toast('orch.toast.singleNode', { name: control.label });
        return null;
      }
    }
    var limitPolicy = context.limitPolicy;
    var nodeLimit = limitPolicy
      && typeof limitPolicy.definitionNodeLimit === 'function'
      ? limitPolicy.definitionNodeLimit() : context.nodeLimit();
    if (Number.isSafeInteger(nodeLimit) && nodeLimit > 0
        && context.nodes().length >= nodeLimit) {
      context.toast('orch.toast.nodeLimit', { n: nodeLimit });
      return null;
    }
    var depth = context.subflowDepth();
    var depthLimit = limitPolicy
      && typeof limitPolicy.subflowDepthLimit === 'function'
      ? limitPolicy.subflowDepthLimit() : context.subflowDepthLimit();
    if (payload.ptype === 'subflow' && Number.isSafeInteger(depthLimit)
        && depthLimit > 0 && depth >= depthLimit) {
      context.toast('orch.toast.subflowDepthLimit', { n: depthLimit });
      return null;
    }

    var prefix = payload.ptype === 'role' ? payload.role
      : payload.ptype === 'subflow' ? 'group' : payload.kind;
    var node = {
      id: context.nextId(prefix),
      type: payload.ptype,
      role: payload.role || '',
      kind: payload.kind || '',
      x: x,
      y: y,
      name: '',
      params: context.defaultParams(payload),
    };
    context.setGraph(context.nodes().concat([node]), context.edges());
    context.setSelection(node.id, null);
    context.markDirty();
    context.render('render');
    return node;
  }

  function connectNodes(from, to) {
    var result = context.graph.connect(
      context.nodes(), context.edges(), from, to, function () {
        return context.nextId('e');
      });
    if (!result.ok || !result.changed) {
      context.topologyError(result.reason);
      return result;
    }
    context.setGraph(context.nodes(), result.edges);
    context.markDirty();
    return result;
  }

  function deleteNode(id) {
    var next = context.graph.deleteNode(context.nodes(), context.edges(), id);
    context.setGraph(next.nodes, next.edges);
    var selected = context.selectedNodeId();
    context.setSelection(selected === id ? null : selected, null);
    context.markDirty();
    context.render('render');
    return next;
  }

  function deleteEdge(id) {
    var next = context.graph.deleteEdge(context.edges(), id);
    context.setGraph(context.nodes(), next);
    var selectedEdge = context.selectedEdgeId();
    context.setSelection(
      context.selectedNodeId(), selectedEdge === id ? null : selectedEdge);
    context.markDirty();
    context.render('renderEdges');
    context.render('renderInspector');
    return next;
  }

  function reverseEdge(id) {
    var result = context.graph.reverseEdge(
      context.nodes(), context.edges(), id);
    if (!result.ok) {
      context.topologyError(result.reason, 'orch.toast.dupEdge');
      return result;
    }
    context.setGraph(context.nodes(), result.edges);
    context.markDirty();
    context.render('renderEdges');
    context.render('renderInspector');
    return result;
  }

  return Object.freeze({
    findNode: findNode,
    addNode: addNode,
    connectNodes: connectNodes,
    deleteNode: deleteNode,
    deleteEdge: deleteEdge,
    reverseEdge: reverseEdge,
  });
}

/* ===== migrated source: orchestration-breadcrumb.js ===== */
/* Accessible nested-flow breadcrumb view. Graph transitions stay in the
 * navigation controller; this module only projects hierarchy and focus. */

function createOrchestrationBreadcrumbView(options) {
  options = options || {};
  var doc = options.document || document;

  function _mount() { return doc.getElementById('orchCrumb'); }
  function _fallbackName() {
    return typeof options.fallbackName === 'function'
      ? options.fallbackName() : 'Group';
  }
  function _translate(key) {
    return typeof options.translate === 'function'
      ? options.translate(key) : key;
  }

  function _separator() {
    var separator = doc.createElement('span');
    separator.className = 'orch-crumb-sep';
    separator.textContent = '\u203a';
    separator.setAttribute('aria-hidden', 'true');
    return separator;
  }

  function _ancestor(label, depth, onNavigate) {
    var button = doc.createElement('button');
    button.type = 'button';
    button.className = 'orch-crumb-item';
    button.textContent = label;
    button.addEventListener('click', function () {
      if (typeof onNavigate === 'function') onNavigate(depth);
    });
    return button;
  }

  function _current(label) {
    var current = doc.createElement('span');
    current.className = 'orch-crumb-item orch-crumb-current';
    current.textContent = label;
    current.tabIndex = -1;
    current.setAttribute('aria-current', 'page');
    return current;
  }

  function _frameLabel(frame) {
    var group = options.graph.findNode(frame.nodes || [], frame.groupId);
    var label = group && typeof options.nodeLabel === 'function'
      ? options.nodeLabel(group) : '';
    return label || _fallbackName();
  }

  function render(frames, onNavigate) {
    var element = _mount();
    if (!element) return null;
    frames = Array.isArray(frames) ? frames : [];
    element.replaceChildren();
    element.hidden = !frames.length;
    if (!frames.length) return null;

    element.appendChild(_ancestor(_translate('orch.crumb.root'), 0, onNavigate));
    frames.forEach(function (frame, index) {
      element.appendChild(_separator());
      var label = _frameLabel(frame);
      element.appendChild(index === frames.length - 1
        ? _current(label) : _ancestor(label, index + 1, onNavigate));
    });
    return element.querySelector('[aria-current="page"]');
  }

  function focusAfterNavigation(workspace) {
    var element = _mount();
    var current = element && !element.hidden
      ? element.querySelector('[aria-current="page"]') : null;
    var selected = workspace && workspace.selected;
    var card = selected ? doc.getElementById('orch-node-' + selected) : null;
    var target = current || (card && card.querySelector('.orch-node-select'))
      || doc.getElementById('orchCanvas');
    if (!target || typeof target.focus !== 'function') return false;
    try { target.focus({ preventScroll: true }); }
    catch (_error) { target.focus(); }
    return true;
  }

  return { render: render, focusAfterNavigation: focusAfterNavigation };
}

/* ===== migrated source: orchestration-navigation.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-navigation.js — nested Group workspace navigation

   Owns enter/exit/root-collapse transitions. Graph state remains in
   orchestration.js and is exchanged through explicit snapshots and mutation
   callbacks. The injected breadcrumb view owns hierarchy DOM and focus.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationNavigationController(options) {
  options = options || {};

  function _workspace() {
    return typeof options.workspace === 'function'
      ? options.workspace() : {
        name: 'Untitled Flow', nodes: [], edges: [], selected: null, sequence: 0,
      };
  }
  function _stack() {
    var value = typeof options.stack === 'function' ? options.stack() : [];
    return Array.isArray(value) ? value : [];
  }
  function _setStack(value) {
    if (typeof options.setStack === 'function') options.setStack(value);
  }
  function _adopt(workspace) {
    if (typeof options.adopt === 'function') options.adopt(workspace);
  }
  function _render() {
    if (typeof options.render === 'function') options.render();
  }
  function _notifyNavigate() {
    if (typeof options.onNavigate === 'function') options.onNavigate();
  }
  function _fallbackName() {
    return typeof options.fallbackName === 'function'
      ? options.fallbackName() : 'Group';
  }
  function _maybeLayout(workspace) {
    if (!workspace || !workspace.needsLayout || !workspace.nodes.length) return;
    if (typeof options.tidy === 'function') options.tidy({ silent: true });
  }

  function _fallbackDefinition() {
    return typeof options.blankGroupDefinition === 'function'
      ? options.blankGroupDefinition() : null;
  }

  function _focus(workspace) {
    if (options.breadcrumb
        && typeof options.breadcrumb.focusAfterNavigation === 'function') {
      options.breadcrumb.focusAfterNavigation(workspace);
    }
  }

  function _commit(transition, shouldLayout) {
    _setStack(transition.stack);
    _adopt(transition.workspace);
    _render();
    _notifyNavigate();
    if (shouldLayout) _maybeLayout(transition.workspace);
    _focus(transition.workspace);
    return transition.workspace;
  }

  function _collapse(workspace, stack, target) {
    var changed = false;
    while (stack.length > target) {
      var transition = options.graph.exitGroup(workspace, stack);
      if (!transition) break;
      workspace = transition.workspace;
      stack = transition.stack;
      changed = true;
    }
    return { workspace: workspace, stack: stack, changed: changed };
  }

  function workspaceState() { return _workspace(); }

  function loadWorkingFromDefinition(definition) {
    var result = options.graph.workspaceFromDefinitionResult(
      definition, _fallbackName());
    if (!result.ok) return null;
    var workspace = result.workspace;
    _adopt(workspace);
    _render();
    _maybeLayout(workspace);
    return workspace;
  }

  function enterGroup(groupId) {
    var transition = options.graph.enterGroup(
      _workspace(), _stack(), groupId, _fallbackDefinition(), _fallbackName()
    );
    if (!transition) return null;
    return _commit(transition, true);
  }

  function exitGroup() {
    var transition = options.graph.exitGroup(_workspace(), _stack());
    if (!transition) return null;
    return _commit(transition, false);
  }

  function crumbTo(depth) {
    var target = Number(depth);
    target = Number.isFinite(target) ? Math.max(0, Math.floor(target)) : 0;
    var transition = _collapse(_workspace(), _stack(), target);
    if (transition.changed) _commit(transition, false);
    return transition.stack.length;
  }

  function flushToRoot() { return crumbTo(0); }

  function navigateToGroups(groupIds) {
    groupIds = Array.isArray(groupIds) ? groupIds : [];
    var transition = _collapse(_workspace(), _stack(), 0);
    var valid = true;
    for (var index = 0; index < groupIds.length; index++) {
      var entered = options.graph.enterGroup(
        transition.workspace, transition.stack, groupIds[index],
        _fallbackDefinition(), _fallbackName()
      );
      if (!entered) { valid = false; break; }
      transition.workspace = entered.workspace;
      transition.stack = entered.stack;
      transition.changed = true;
    }
    if (transition.changed) _commit(transition, true);
    return valid;
  }

  function renderBreadcrumb() {
    if (!options.breadcrumb || typeof options.breadcrumb.render !== 'function') return;
    return options.breadcrumb.render(_stack(), crumbTo);
  }

  return {
    workspaceState: workspaceState,
    adoptWorkspace: _adopt,
    loadWorkingFromDefinition: loadWorkingFromDefinition,
    enterGroup: enterGroup,
    exitGroup: exitGroup,
    flushToRoot: flushToRoot,
    navigateToGroups: navigateToGroups,
    crumbTo: crumbTo,
    renderBreadcrumb: renderBreadcrumb,
  };
}

/* ===== migrated source: orchestration-viewport-geometry.js ===== */
/* Pure Canvas viewport geometry.
 *
 * Graph/model coordinates enter here; DOM measurements and style writes stay
 * in orchestration-viewport.js. Keeping the calculations free of browser
 * state makes fit/extent policy reusable and independently testable.
 */

function createOrchestrationViewportGeometry(options) {
  options = options || {};
  var minimum = Number(options.minScale) || 0.35;
  var maximum = Number(options.maxScale) || 1.5;
  var cardWidth = Number(options.cardWidth) || 188;
  var cardHeight = Number(options.cardHeight) || 76;
  var padding = Number(options.padding) || 44;

  function clampScale(value) {
    var numeric = Number(value);
    if (!Number.isFinite(numeric)) numeric = 1;
    return Math.min(maximum, Math.max(minimum, numeric));
  }

  function graphBounds(nodes, measureHeight) {
    var list = Array.isArray(nodes) ? nodes : [];
    if (!list.length) return null;
    var minX = Infinity;
    var minY = Infinity;
    var maxX = -Infinity;
    var maxY = -Infinity;
    list.forEach(function (node) {
      var x = Number(node.x) || 0;
      var y = Number(node.y) || 0;
      var measured = typeof measureHeight === 'function'
        ? Number(measureHeight(node)) : 0;
      var height = measured > 0 ? measured : cardHeight;
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x + cardWidth);
      maxY = Math.max(maxY, y + height);
    });
    return Object.freeze({
      minX: minX, minY: minY, maxX: maxX, maxY: maxY,
    });
  }

  function extent(viewport, transform, bounds) {
    viewport = viewport || {};
    transform = transform || {};
    var width = Math.max(0, Number(viewport.width) || 0);
    var height = Math.max(0, Number(viewport.height) || 0);
    var scale = clampScale(transform.scale);
    var offsetX = Number(transform.offsetX) || 0;
    var offsetY = Number(transform.offsetY) || 0;
    var maxX = bounds ? bounds.maxX + padding : 0;
    var maxY = bounds ? bounds.maxY + padding : 0;
    var modelWidth = Math.max(maxX, Math.max(1, (width - offsetX) / scale));
    var modelHeight = Math.max(maxY, Math.max(1, (height - offsetY) / scale));
    return Object.freeze({
      modelWidth: modelWidth,
      modelHeight: modelHeight,
      visualWidth: Math.max(width, offsetX + modelWidth * scale),
      visualHeight: Math.max(height, offsetY + modelHeight * scale),
      scale: scale,
      offsetX: offsetX,
      offsetY: offsetY,
    });
  }

  function fit(bounds, viewport, fitMinScale) {
    if (!bounds) return null;
    viewport = viewport || {};
    var viewportWidth = Math.max(0, Number(viewport.width) || 0);
    var viewportHeight = Math.max(0, Number(viewport.height) || 0);
    var width = Math.max(1, bounds.maxX - bounds.minX);
    var height = Math.max(1, bounds.maxY - bounds.minY);
    var candidate = Math.min(
      1,
      Math.max(1, viewportWidth - padding * 2) / width,
      Math.max(1, viewportHeight - padding * 2) / height
    );
    var scale = Math.max(clampScale(fitMinScale), clampScale(candidate));
    var visualWidth = width * scale;
    var visualHeight = height * scale;
    var offsetX = Math.max(
      padding, (viewportWidth - visualWidth) / 2 - bounds.minX * scale);
    var offsetY = Math.max(
      padding, (viewportHeight - visualHeight) / 2 - bounds.minY * scale);
    var horizontalOverflow = visualWidth + padding * 2 > viewportWidth;
    var verticalOverflow = visualHeight + padding * 2 > viewportHeight;
    return Object.freeze({
      scale: scale,
      offsetX: offsetX,
      offsetY: offsetY,
      scrollLeft: Math.max(0, horizontalOverflow
        ? bounds.minX * scale + offsetX - padding
        : (bounds.minX + bounds.maxX) / 2 * scale + offsetX
          - viewportWidth / 2),
      scrollTop: Math.max(0, verticalOverflow
        ? bounds.minY * scale + offsetY - padding
        : (bounds.minY + bounds.maxY) / 2 * scale + offsetY
          - viewportHeight / 2),
    });
  }

  return Object.freeze({
    bounds: graphBounds,
    clampScale: clampScale,
    extent: extent,
    fit: fit,
    maxScale: function () { return maximum; },
    minScale: function () { return minimum; },
    padding: function () { return padding; },
  });
}

/* ===== migrated source: orchestration-viewport.js ===== */
/* ════════════════════════════════════════════════════════════════════
   orchestration-viewport.js — Canvas zoom, extent and fit-to-view

   Owns presentation-only viewport state. Authoring coordinates remain in
   graph/model units, so zoom never dirties the document or enters history.
   Geometry consumers read transform() through one injected seam.
   ════════════════════════════════════════════════════════════════════ */


function createOrchestrationViewportController(options) {
  options = options || {};
  var doc = options.document || document;
  var scale = Number(options.defaultScale) || 1;
  var offsetX = 0;
  var offsetY = 0;
  var wired = false;
  var geometry = options.geometry || createOrchestrationViewportGeometry({
    minScale: options.minScale,
    maxScale: options.maxScale,
    cardWidth: options.cardWidth,
    cardHeight: options.cardHeight,
    padding: options.padding,
  });
  var minScale = geometry.minScale();
  var maxScale = geometry.maxScale();
  var step = Number(options.step) || 0.1;
  var padding = geometry.padding();

  function fitScaleFloor() {
    var configured = typeof options.fitMinScale === 'function'
      ? options.fitMinScale() : options.fitMinScale;
    var value = Number(configured);
    return Number.isFinite(value) && value > 0
      ? geometry.clampScale(value) : minScale;
  }

  function canvas() { return doc.getElementById('orchCanvas'); }
  function extent() { return doc.getElementById('orchViewportExtent'); }
  function scene() { return doc.getElementById('orchViewportScene'); }
  function edges() { return doc.getElementById('orchEdges'); }
  function nodesElement() { return doc.getElementById('orchNodes'); }
  function nodes() {
    var value = typeof options.nodes === 'function' ? options.nodes() : [];
    return Array.isArray(value) ? value : [];
  }
  function clamp(value) {
    return geometry.clampScale(value);
  }

  function transform() {
    return { scale: scale, offsetX: offsetX, offsetY: offsetY };
  }

  function bounds() {
    return geometry.bounds(nodes(), function (node) {
      var element = doc.getElementById('orch-node-' + node.id);
      return element && Number(element.offsetHeight) || 0;
    });
  }

  function renderControls() {
    var label = doc.getElementById('orchZoomResetBtn');
    var zoomOut = doc.getElementById('orchZoomOutBtn');
    var zoomIn = doc.getElementById('orchZoomInBtn');
    if (label) label.textContent = Math.round(scale * 100) + '%';
    if (zoomOut) zoomOut.disabled = scale <= minScale + 0.001;
    if (zoomIn) zoomIn.disabled = scale >= maxScale - 0.001;
  }

  function sync() {
    var viewport = canvas();
    var box = extent();
    var content = scene();
    if (!viewport || !box || !content) return null;
    var graphBounds = bounds();
    var projected = geometry.extent({
      width: viewport.clientWidth, height: viewport.clientHeight,
    }, transform(), graphBounds);
    var modelWidth = projected.modelWidth;
    var modelHeight = projected.modelHeight;
    content.style.width = Math.ceil(modelWidth) + 'px';
    content.style.height = Math.ceil(modelHeight) + 'px';
    content.style.transform = 'translate(' + offsetX + 'px,' + offsetY
      + 'px) scale(' + scale + ')';
    content.setAttribute('data-orch-model-width', String(Math.ceil(modelWidth)));
    content.setAttribute('data-orch-model-height', String(Math.ceil(modelHeight)));
    box.style.width = Math.ceil(projected.visualWidth) + 'px';
    box.style.height = Math.ceil(projected.visualHeight) + 'px';
    var svg = edges();
    if (svg) {
      svg.setAttribute('width', String(Math.ceil(modelWidth)));
      svg.setAttribute('height', String(Math.ceil(modelHeight)));
    }
    var nodeLayer = nodesElement();
    if (nodeLayer) {
      nodeLayer.style.width = Math.ceil(modelWidth) + 'px';
      nodeLayer.style.height = Math.ceil(modelHeight) + 'px';
    }
    viewport.style.backgroundSize = (24 * scale) + 'px ' + (24 * scale) + 'px';
    viewport.style.backgroundPosition = offsetX + 'px ' + offsetY + 'px';
    renderControls();
    return {
      width: modelWidth, height: modelHeight, bounds: graphBounds,
      scale: scale, offsetX: offsetX, offsetY: offsetY,
    };
  }

  function setScale(value, anchor) {
    var viewport = canvas();
    if (!viewport) return false;
    var next = clamp(Number(value) || scale);
    if (Math.abs(next - scale) < 0.001) return false;
    var anchorX = anchor && Number.isFinite(anchor.x)
      ? anchor.x : viewport.clientWidth / 2;
    var anchorY = anchor && Number.isFinite(anchor.y)
      ? anchor.y : viewport.clientHeight / 2;
    var modelX = (viewport.scrollLeft + anchorX - offsetX) / scale;
    var modelY = (viewport.scrollTop + anchorY - offsetY) / scale;
    scale = next;
    sync();
    viewport.scrollLeft = Math.max(0, modelX * scale + offsetX - anchorX);
    viewport.scrollTop = Math.max(0, modelY * scale + offsetY - anchorY);
    if (typeof options.onChange === 'function') options.onChange(transform());
    return true;
  }

  function zoomBy(delta, anchor) {
    return setScale(scale + delta, anchor);
  }

  function reset() {
    var viewport = canvas();
    if (!viewport) return false;
    var modelX = (viewport.scrollLeft + viewport.clientWidth / 2 - offsetX) / scale;
    var modelY = (viewport.scrollTop + viewport.clientHeight / 2 - offsetY) / scale;
    scale = 1;
    offsetX = 0;
    offsetY = 0;
    sync();
    viewport.scrollLeft = Math.max(0, modelX - viewport.clientWidth / 2);
    viewport.scrollTop = Math.max(0, modelY - viewport.clientHeight / 2);
    if (typeof options.onChange === 'function') options.onChange(transform());
    return true;
  }

  function fit() {
    var viewport = canvas();
    if (!viewport) return false;
    var graphBounds = bounds();
    if (!graphBounds) {
      scale = 1;
      offsetX = 0;
      offsetY = 0;
      sync();
      viewport.scrollLeft = 0;
      viewport.scrollTop = 0;
      return true;
    }
    var fitted = geometry.fit(graphBounds, {
      width: viewport.clientWidth, height: viewport.clientHeight,
    }, fitScaleFloor());
    scale = fitted.scale;
    offsetX = fitted.offsetX;
    offsetY = fitted.offsetY;
    sync();
    viewport.scrollLeft = fitted.scrollLeft;
    viewport.scrollTop = fitted.scrollTop;
    if (typeof options.onChange === 'function') options.onChange(transform());
    return true;
  }

  function wire() {
    var viewport = canvas();
    if (!viewport || wired) return false;
    wired = true;
    viewport.addEventListener('wheel', function (event) {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      var rect = viewport.getBoundingClientRect();
      zoomBy(event.deltaY < 0 ? step : -step, {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      });
    }, { passive: false });
    sync();
    return true;
  }

  return {
    wire: wire,
    sync: sync,
    fit: fit,
    reset: reset,
    zoomIn: function () { return zoomBy(step); },
    zoomOut: function () { return zoomBy(-step); },
    setScale: setScale,
    scale: function () { return scale; },
    transform: transform,
    bounds: bounds,
  };
}

/* ===== migrated source: orchestration-canvas.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas.js — pure canvas coordinates + edge routing

   Centralizes the geometry shared by palette drops, touch-to-add, node
   dragging, connection previews and SVG edge rendering. It owns no DOM
   listeners or graph mutations; orchestration.js supplies viewport/port
   elements and consumes deterministic points and paths.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationCanvasGeometry(options) {
  options = options || {};
  var cardWidth = options.cardWidth || 188;

  function viewportTransform() {
    var value = typeof options.viewport === 'function'
      ? options.viewport() : null;
    return {
      scale: value && Number(value.scale) > 0 ? Number(value.scale) : 1,
      offsetX: value && Number(value.offsetX) || 0,
      offsetY: value && Number(value.offsetY) || 0,
    };
  }

  function canvasPoint(canvas, clientX, clientY) {
    var rect = canvas.getBoundingClientRect();
    var viewport = viewportTransform();
    return {
      x: (clientX - rect.left + canvas.scrollLeft - viewport.offsetX)
        / viewport.scale,
      y: (clientY - rect.top + canvas.scrollTop - viewport.offsetY)
        / viewport.scale,
    };
  }

  function clampNode(point, minimum) {
    minimum = minimum == null ? 4 : minimum;
    return {
      x: Math.max(minimum, point.x),
      y: Math.max(minimum, point.y),
    };
  }

  function dropNode(canvas, clientX, clientY, headerOffset) {
    var point = canvasPoint(canvas, clientX, clientY);
    return clampNode({
      x: point.x - cardWidth / 2,
      y: point.y - (headerOffset == null ? 20 : headerOffset),
    }, 8);
  }

  function centeredNode(canvas, cardHeight) {
    var viewport = viewportTransform();
    return clampNode({
      x: (canvas.scrollLeft + canvas.clientWidth / 2 - viewport.offsetX)
        / viewport.scale - cardWidth / 2,
      y: (canvas.scrollTop + canvas.clientHeight / 2 - viewport.offsetY)
        / viewport.scale
        - (cardHeight == null ? 80 : cardHeight) / 2,
    }, 8);
  }

  function portCenter(canvas, port) {
    if (!canvas || !port) return null;
    var canvasRect = canvas.getBoundingClientRect();
    var portRect = port.getBoundingClientRect();
    var viewport = viewportTransform();
    return {
      x: (portRect.left - canvasRect.left + canvas.scrollLeft
        + portRect.width / 2 - viewport.offsetX) / viewport.scale,
      y: (portRect.top - canvasRect.top + canvas.scrollTop
        + portRect.height / 2 - viewport.offsetY) / viewport.scale,
    };
  }

  function fanOffset(index, count) {
    if (count <= 1) return 0;
    var step = Math.min(26, (cardWidth * 0.66) / (count - 1));
    return (index - (count - 1) / 2) * step;
  }

  function bezier(from, to) {
    // Ports exit the source bottom and enter the target top, so the normal
    // route is a vertical S. Near-level/back edges bow sideways to avoid
    // folding across their node cards.
    var dx = to.x - from.x;
    var dy = to.y - from.y;
    if (dy >= 30) {
      var vertical = dy * 0.5;
      return 'M ' + from.x + ' ' + from.y
        + ' C ' + from.x + ' ' + (from.y + vertical) + ' '
        + to.x + ' ' + (to.y - vertical) + ' '
        + to.x + ' ' + to.y;
    }
    var side = dx >= 0 ? 1 : -1;
    var horizontal = Math.max(70, Math.abs(dx) * 0.5);
    var backVertical = Math.max(40, Math.abs(dy) * 0.5);
    return 'M ' + from.x + ' ' + from.y
      + ' C ' + (from.x + side * horizontal) + ' '
      + (from.y + backVertical) + ' '
      + (to.x + side * horizontal) + ' '
      + (to.y - backVertical) + ' '
      + to.x + ' ' + to.y;
  }

  function edgeRoutes(edges, getPortCenter) {
    var incoming = {};
    var outgoing = {};
    var incomingSeen = {};
    var outgoingSeen = {};
    edges.forEach(function (edge) {
      incoming[edge.to] = (incoming[edge.to] || 0) + 1;
      outgoing[edge.from] = (outgoing[edge.from] || 0) + 1;
    });

    var routes = [];
    edges.forEach(function (edge) {
      var from = getPortCenter(edge.from, 'out');
      var to = getPortCenter(edge.to, 'in');
      if (!from || !to) return;
      var outIndex = outgoingSeen[edge.from] || 0;
      var inIndex = incomingSeen[edge.to] || 0;
      outgoingSeen[edge.from] = outIndex + 1;
      incomingSeen[edge.to] = inIndex + 1;
      var routedFrom = {
        x: from.x + fanOffset(outIndex, outgoing[edge.from]),
        y: from.y,
      };
      var routedTo = {
        x: to.x + fanOffset(inIndex, incoming[edge.to]),
        y: to.y,
      };
      routes.push({
        edge: edge,
        from: routedFrom,
        to: routedTo,
        path: bezier(routedFrom, routedTo),
      });
    });
    return routes;
  }

  return {
    canvasPoint: canvasPoint,
    clampNode: clampNode,
    dropNode: dropNode,
    centeredNode: centeredNode,
    portCenter: portCenter,
    fanOffset: fanOffset,
    bezier: bezier,
    edgeRoutes: edgeRoutes,
  };
}

/* ===== migrated source: orchestration-canvas-gesture-context.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-gesture-context.js — shared Canvas gesture ports
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasGestureContext(options) {
  options = options || {};
  function doc() { return options.document || document; }
  function win() { return options.window || window; }
  function canvas() { return doc().getElementById('orchCanvas'); }
  function call(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return typeof options[name] === 'function'
      ? options[name].apply(null, args) : undefined;
  }
  return Object.freeze({
    options: options,
    document: doc,
    window: win,
    canvas: canvas,
    primary: function (event) {
      return typeof event.button !== 'number' || event.button === 0;
    },
    findNode: function (id) { return call('findNode', id) || null; },
    render: function () { return call('render'); },
    renderNodes: function () { return call('renderNodes'); },
    renderEdges: function () { return call('renderEdges'); },
    renderInspector: function () { return call('renderInspector'); },
    startPointer: function (event) { return call('startPointer', event); },
    stopPointer: function () { return call('stopPointer'); },
  });
}

/* ===== migrated source: orchestration-canvas-node-drag.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-node-drag.js — transient node drag gesture
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasNodeDrag(context) {
  var drag = null;

  function start(event, id) {
    if (!context.primary(event)) return false;
    event.stopPropagation();
    var focusSelection = event.target
      && typeof event.target.closest === 'function'
      && !!event.target.closest('.orch-node-select');
    var node = context.findNode(id);
    if (!node) return false;
    var options = context.options;
    if (typeof options.selectForDrag === 'function') options.selectForDrag(id);
    var point = options.geometry.canvasPoint(
      context.canvas(), event.clientX, event.clientY);
    drag = {
      id: id,
      dx: point.x - node.x,
      dy: point.y - node.y,
      startX: node.x,
      startY: node.y,
      moved: false,
    };
    context.startPointer(event);
    var element = context.document().getElementById('orch-node-' + id);
    if (element) element.classList.add('is-dragging');
    context.renderNodes();
    context.renderEdges();
    context.renderInspector();
    if (focusSelection) {
      var refreshed = context.document().getElementById('orch-node-' + id);
      var select = refreshed && refreshed.querySelector('.orch-node-select');
      if (select) select.focus({ preventScroll: true });
    }
    return true;
  }

  function move(event) {
    if (!drag) return false;
    var options = context.options;
    var point = options.geometry.canvasPoint(
      context.canvas(), event.clientX, event.clientY);
    var node = context.findNode(drag.id);
    if (!node) return false;
    drag.moved = true;
    var next = options.geometry.clampNode({
      x: point.x - drag.dx,
      y: point.y - drag.dy,
    }, 4);
    node.x = next.x;
    node.y = next.y;
    var element = context.document().getElementById('orch-node-' + node.id);
    if (element) {
      element.style.left = node.x + 'px';
      element.style.top = node.y + 'px';
    }
    if (typeof options.syncViewport === 'function') options.syncViewport();
    context.renderEdges();
    return true;
  }

  function finish() {
    if (!drag) return false;
    var moved = !!drag.moved;
    var element = context.document().getElementById('orch-node-' + drag.id);
    if (element) element.classList.remove('is-dragging');
    drag = null;
    if (moved && typeof context.options.markDirty === 'function') {
      context.options.markDirty();
    }
    return true;
  }

  function cancel() {
    if (!drag) return false;
    var node = context.findNode(drag.id);
    if (node) {
      node.x = drag.startX;
      node.y = drag.startY;
    }
    var element = context.document().getElementById('orch-node-' + drag.id);
    if (element) element.classList.remove('is-dragging');
    drag = null;
    return true;
  }

  return Object.freeze({
    start: start,
    move: move,
    finish: finish,
    cancel: cancel,
    active: function () { return !!drag; },
  });
}

/* ===== migrated source: orchestration-canvas-connection.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-connection.js — pointer/keyboard edge gesture
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasConnection(context) {
  var connection = null;

  function startPointer(event, id) {
    if (!context.primary(event)) return false;
    event.stopPropagation();
    var point = context.options.geometry.canvasPoint(
      context.canvas(), event.clientX, event.clientY);
    connection = { from: id, x: point.x, y: point.y };
    context.startPointer(event);
    return connection;
  }

  function completePointer(event, id) {
    if (!connection) return false;
    event.stopPropagation();
    if (connection.from && connection.from !== id
        && typeof context.options.connectNodes === 'function') {
      context.options.connectNodes(connection.from, id);
    }
    connection = null;
    context.stopPointer();
    context.render();
    return true;
  }

  function keyDown(event, id, side) {
    if (side === 'out') {
      var point = typeof context.options.portCenter === 'function'
        ? context.options.portCenter(id, 'out') : null;
      if (!point) return false;
      connection = { from: id, x: point.x, y: point.y };
      context.startPointer(event);
      context.renderNodes();
      context.renderEdges();
      var sourceCard = context.document().getElementById('orch-node-' + id);
      var source = sourceCard && sourceCard.querySelector('.orch-port-out');
      if (source) source.focus();
      return true;
    }
    if (!connection || !connection.from || connection.from === id) return false;
    if (typeof context.options.connectNodes === 'function') {
      context.options.connectNodes(connection.from, id);
    }
    connection = null;
    context.stopPointer();
    context.render();
    var targetCard = context.document().getElementById('orch-node-' + id);
    var target = targetCard && targetCard.querySelector('.orch-port-in');
    if (target) target.focus();
    return true;
  }

  function move(event) {
    if (!connection) return false;
    var point = context.options.geometry.canvasPoint(
      context.canvas(), event.clientX, event.clientY);
    connection.x = point.x;
    connection.y = point.y;
    context.renderEdges();
    return true;
  }

  function finish() {
    if (!connection) return false;
    connection = null;
    context.renderNodes();
    context.renderEdges();
    return true;
  }

  function cancel() {
    if (!connection) return false;
    connection = null;
    return true;
  }

  return Object.freeze({
    startPointer: startPointer,
    completePointer: completePointer,
    keyDown: keyDown,
    move: move,
    finish: finish,
    cancel: cancel,
    value: function () { return connection; },
  });
}

/* ===== migrated source: orchestration-canvas-surface-interaction.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-surface-interaction.js — drop/deselect wiring
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasSurfaceInteraction(context) {
  var wired = false;

  function wire() {
    var canvas = context.canvas();
    if (!canvas || wired) return false;
    wired = true;
    canvas.addEventListener('dragover', function (event) {
      var transfer = event.dataTransfer;
      if (transfer && Array.prototype.indexOf.call(
        transfer.types || [], 'text/orch') !== -1) {
        event.preventDefault();
        transfer.dropEffect = 'copy';
      }
    });
    canvas.addEventListener('drop', function (event) {
      var transfer = event.dataTransfer;
      var raw = transfer && transfer.getData('text/orch');
      if (!raw) return;
      event.preventDefault();
      var payload;
      try { payload = JSON.parse(raw); } catch (_) { return; }
      var point = context.options.geometry.dropNode(
        canvas, event.clientX, event.clientY, 20);
      if (typeof context.options.addNode === 'function') {
        context.options.addNode(payload, point.x, point.y);
      }
    });
    canvas.addEventListener('pointerdown', function (event) {
      if (!context.primary(event)) return;
      if (event.target === canvas || event.target.id === 'orchNodes'
          || event.target.id === 'orchEdges') {
        if (typeof context.options.deselect === 'function') {
          context.options.deselect();
        }
      }
    });
    return true;
  }

  return Object.freeze({ wire: wire });
}

/* ===== migrated source: orchestration-canvas-interaction.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-canvas-interaction.js — stable Canvas gesture facade

   Routes one shared Pointer Session across surface, node-drag and connection
   collaborators while retaining the existing editor-facing interface.
   ═══════════════════════════════════════════════════════════════════ */
function createOrchestrationCanvasInteractionController(options) {
  options = options || {};
  var unbindPointer = null;

  function stopPointer() {
    if (unbindPointer) unbindPointer();
    unbindPointer = null;
  }
  function startPointer(event) {
    stopPointer();
    unbindPointer = bindOrchestrationPointerSession({
      pointerId: event && event.pointerId,
      moveTarget: context.document(),
      pointerTarget: context.window(),
      window: context.window(),
      onMove: onPointerMove,
      onEnd: onPointerUp,
    });
  }

  var context = createOrchestrationCanvasGestureContext(Object.assign(
    {}, options, { startPointer: startPointer, stopPointer: stopPointer }));
  var surface = createOrchestrationCanvasSurfaceInteraction(context);
  var nodeDrag = createOrchestrationCanvasNodeDrag(context);
  var connection = createOrchestrationCanvasConnection(context);

  function onPointerMove(event) {
    if (nodeDrag.active()) return nodeDrag.move(event);
    return connection.move(event);
  }

  function onPointerUp() {
    nodeDrag.finish();
    connection.finish();
    stopPointer();
  }

  function cancelGesture() {
    var consumed = nodeDrag.cancel();
    consumed = connection.cancel() || consumed;
    if (!consumed) return false;
    stopPointer();
    context.render();
    return true;
  }

  return Object.freeze({
    wireCanvas: surface.wire,
    nodeHeaderDown: nodeDrag.start,
    portDown: connection.startPointer,
    portUp: connection.completePointer,
    portKeyDown: connection.keyDown,
    onPointerMove: onPointerMove,
    onPointerUp: onPointerUp,
    cancelGesture: cancelGesture,
    connection: connection.value,
    isDragging: nodeDrag.active,
  });
}

/* ===== migrated source: orchestration-edge-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-edge-view.js — SVG connection presentation + selection

   Renders routed edges produced by orchestration-canvas.js and binds pointer
   and keyboard selection without embedding imported edge IDs in executable
   attributes. It owns no topology or selection state.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationEdgeView(options) {
  options = options || {};
  var renderedEdgeIds = [];

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }

  function _select(event, edge) {
    if (event && event.type === 'keydown'
        && event.key !== 'Enter' && event.key !== ' ') return;
    if (event && event.type === 'keydown') event.preventDefault();
    if (typeof options.onSelect === 'function') options.onSelect(edge.id);
  }

  function _issueSummary(edge, index) {
    return typeof options.issueSummary === 'function'
      ? options.issueSummary(edge, index) : null;
  }

  function _edgeLabel(edge, issueLabel) {
    var nodeLabel = typeof options.nodeLabel === 'function'
      ? options.nodeLabel : function (id) { return id; };
    var label = _translate('orch.edge.label', {
      from: nodeLabel(edge.from),
      to: nodeLabel(edge.to),
    });
    if (issueLabel) label += ' · ' + issueLabel;
    return label + ' · ' + _translate('orch.edge.clickTip');
  }

  function render(svg, canvas) {
    if (!svg || !canvas) return;
    var focusedEdgeId = null;
    var active = (options.document || document).activeElement;
    if (active && svg.contains(active)
        && active.classList.contains('orch-edge-path')) {
      var focusedIndex = Number(active.getAttribute('data-edge-index'));
      if (Number.isInteger(focusedIndex)) {
        focusedEdgeId = renderedEdgeIds[focusedIndex] || null;
      }
    }
    var geometry = options.geometry;
    var edges = typeof options.edges === 'function' ? options.edges() : [];
    var selected = typeof options.selectedEdgeId === 'function'
      ? options.selectedEdgeId() : null;
    var portCenter = options.portCenter || function () { return null; };
    var scene = svg.parentElement;
    var modelWidth = scene && scene.getAttribute('data-orch-model-width');
    var modelHeight = scene && scene.getAttribute('data-orch-model-height');
    svg.setAttribute('width', modelWidth || String(canvas.scrollWidth));
    svg.setAttribute('height', modelHeight || String(canvas.scrollHeight));

    var parts = '<defs><marker id="orchArrow" viewBox="0 0 12 12" '
      + 'refX="9.5" refY="6" markerWidth="8" markerHeight="8" '
      + 'orient="auto-start-reverse"><path class="orch-edge-arrow" '
      + 'd="M1 1 L11 6 L1 11 L4 6 Z"></path></marker></defs>';
    var routes = geometry.edgeRoutes(edges, portCenter);
    routes.forEach(function (route, index) {
      var isSelected = selected === route.edge.id;
      var edgeIndex = edges.indexOf(route.edge);
      var issues = _issueSummary(route.edge, edgeIndex);
      var issueClass = issues && issues.total
        ? (issues.errors ? ' has-errors' : ' has-warnings') : '';
      var issueLabel = issues && issues.total
        ? _translate('orch.issues.objectSummary', {
            errors: issues.errors || 0, warnings: issues.warnings || 0,
          }) : '';
      var label = _edgeLabel(route.edge, issueLabel);
      parts += '<path class="orch-edge-hit" d="' + _escape(route.path)
        + '" data-edge-index="' + index + '" aria-hidden="true"></path>';
      parts += '<path class="orch-edge-path' + (isSelected ? ' is-selected' : '')
        + issueClass
        + '" marker-end="url(#orchArrow)" d="' + _escape(route.path)
        + '" data-edge-index="' + index + '" tabindex="0" role="button" '
        + 'aria-pressed="' + isSelected + '" aria-label="'
        + _escape(label) + '"><title>'
        + _escape(label)
        + '</title></path>';
    });

    var connection = typeof options.connection === 'function'
      ? options.connection() : null;
    if (connection) {
      var source = portCenter(connection.from, 'out');
      if (source) {
        parts += '<path class="orch-edge-temp" d="'
          + _escape(geometry.bezier(source, {
            x: connection.x, y: connection.y,
          })) + '"></path>';
      }
    }
    svg.innerHTML = parts;
    renderedEdgeIds = routes.map(function (route) { return route.edge.id; });

    Array.prototype.forEach.call(
      svg.querySelectorAll('[data-edge-index]'), function (path) {
        var route = routes[Number(path.getAttribute('data-edge-index'))];
        if (!route) return;
        path.addEventListener('click', function (event) {
          _select(event, route.edge);
        });
        if (path.classList.contains('orch-edge-path')) {
          path.addEventListener('keydown', function (event) {
            _select(event, route.edge);
          });
        }
      }
    );
    var keyboard = createOrchestrationRovingItemsController({
      root: svg,
      selector: '.orch-edge-path',
    });
    var focusedEdgeIndex = renderedEdgeIds.indexOf(focusedEdgeId);
    var focusedPath = focusedEdgeIndex < 0 ? null
      : svg.querySelector('.orch-edge-path[data-edge-index="'
        + focusedEdgeIndex + '"]');
    keyboard.sync(focusedPath || svg.querySelector('.orch-edge-path.is-selected'));
    if (focusedPath && typeof focusedPath.focus === 'function') {
      focusedPath.focus({ preventScroll: true });
    }
  }

  return { render: render };
}

/* ===== migrated source: orchestration-node-presentation.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-node-presentation.js — Pure Studio node-card projection

   Converts backend-authored node/catalogue values into escaped card HTML.
   It owns no DOM listeners or graph state; orchestration-node-view.js binds
   the projected controls to editor callbacks.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationNodePresentation(options) {
  options = options || {};
  var catalogue = options.catalogue || createOrchestrationNodeCatalogue({
    controls: options.controls,
    nodeRuntimeDefaults: options.nodeRuntimeDefaults,
    roles: options.roles,
  });

  function _edges() {
    return typeof options.edges === 'function' ? options.edges() : [];
  }

  function _escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _issueSummary(nodeId) {
    return typeof options.issueSummary === 'function'
      ? options.issueSummary(nodeId) : null;
  }

  function _issueLabel(summary) {
    return _translate('orch.issues.objectSummary', {
      errors: summary.errors || 0,
      warnings: summary.warnings || 0,
    });
  }

  function _role(role) {
    return catalogue.role(role);
  }

  function _control(kind) {
    return catalogue.control(kind);
  }

  function ioBadge(node) {
    var io = node.params && node.params.io;
    if (!io) return '';
    var inputs = Array.isArray(io.inputs) ? io.inputs.length : 0;
    var outputs = Array.isArray(io.outputs) ? io.outputs.length : 0;
    if (!inputs && !outputs) return '';
    return ' · <span class="orch-io-badge">I/O '
      + inputs + '/' + outputs + '</span>';
  }

  function autoLabel(node) {
    if (node.type === 'subflow') return _translate('orch.group.defaultLabel');
    if (node.type === 'role') {
      var role = _role(node.role);
      return role ? role.label : node.role;
    }
    var control = _control(node.kind);
    return control ? control.label : node.kind;
  }

  function kindLabel(node) {
    return node.type === 'subflow' ? _translate('orch.kind.group')
      : node.type === 'role' ? _translate('orch.kind.agent')
        : _translate('orch.kind.control');
  }

  function nodeBlurb(node) {
    if (node.type === 'role') {
      var role = _role(node.role);
      return role ? role.blurb : '';
    }
    if (node.type === 'subflow') return '';
    var control = _control(node.kind);
    return control ? control.blurb : '';
  }

  function controlSubtitle(node) {
    return _escape(projectOrchestrationControlSummary(
      node, _edges(), _translate, {
        profile: 'studio',
        nodeParam: catalogue.runtimeParam,
      }).text);
  }

  function groupSubtitle(node) {
    return _escape(projectOrchestrationSubflowSummary(
      node, _translate, { nodeParam: catalogue.runtimeParam }).text)
      + ioBadge(node);
  }

  function inspectorAvatar(node) {
    var glyphs = options.glyphs || {};
    if (node.type === 'role') {
      var role = _role(node.role);
      var src = typeof options.iconSrc === 'function'
        ? options.iconSrc(role ? role.icon : 'tofu-general') : '';
      return '<img class="orch-insp-avatar" src="' + _escape(src) + '" alt="">';
    }
    if (node.type === 'subflow') {
      return '<span class="orch-insp-avatar orch-insp-glyph">'
        + (glyphs.group || '') + '</span>';
    }
    var glyph = glyphs[catalogue.controlGlyph(node)] || glyphs.play || '';
    var accent = catalogue.accent(node, 'var(--accent)');
    return '<span class="orch-insp-avatar orch-insp-glyph" '
      + 'style="--node-accent:' + _escape(accent) + '">' + glyph + '</span>';
  }

  function _presentation(node) {
    var glyphs = options.glyphs || {};
    if (node.type === 'subflow') {
      return {
        accent: catalogue.accent(node), typeClass: ' orch-node-group',
        icon: glyphs.group || '', subtitle: groupSubtitle(node),
      };
    }
    if (node.type === 'role') {
      var role = _role(node.role) || {};
      var src = typeof options.iconSrc === 'function' ? options.iconSrc(role.icon) : '';
      var summary = projectOrchestrationRoleExecutionSummary(
        node, _translate, {
          defaultEmits: options.defaultEmits,
          nodeParam: catalogue.runtimeParam,
        });
      var subtitle = _escape(summary.text);
      if (summary.emitsValue === 'user') {
        subtitle += ' · ' + ((options.icons || {}).speak || '')
          + _escape(summary.emits);
      }
      return {
        accent: catalogue.accent(node), typeClass: ' orch-node-role',
        icon: '<img src="' + _escape(src) + '" alt="">',
        subtitle: subtitle + ioBadge(node),
      };
    }
    return {
      accent: catalogue.accent(node, '#888'),
      typeClass: ' orch-node-ctrl orch-node-' + _escape(node.kind || 'ctrl'),
      icon: glyphs[catalogue.controlGlyph(node)] || '',
      subtitle: controlSubtitle(node),
    };
  }

  function cardHtml(node, selected, connectingFrom) {
    var view = _presentation(node);
    var title = _escape(node.name || autoLabel(node));
    var id = _escape(node.id);
    var hasInput = node.kind !== 'start';
    var hasOutput = node.kind !== 'stop';
    var selectedClass = selected === node.id ? ' is-selected' : '';
    var connectingClass = connectingFrom === node.id ? ' is-connecting' : '';
    var issues = _issueSummary(node.id);
    var issueClass = issues && issues.total
      ? ' has-issues ' + (issues.errors ? 'has-errors' : 'has-warnings') : '';
    var issueLabel = issues && issues.total ? _issueLabel(issues) : '';
    var accessibleTitle = title
      + (issueLabel ? ' · ' + _escape(issueLabel) : '');
    var selectedNode = selected === node.id;
    var localTab = selectedNode ? '' : ' tabindex="-1"';
    var inputTab = selectedNode || connectingFrom && connectingFrom !== node.id
      ? '' : ' tabindex="-1"';
    var html = '<div class="orch-node' + view.typeClass + selectedClass
      + connectingClass + issueClass + '" id="orch-node-' + id
      + '" data-node-id="' + id + '" '
      + 'style="left:' + Number(node.x || 0) + 'px;top:' + Number(node.y || 0)
      + 'px;--node-accent:' + _escape(view.accent) + '" role="group" '
      + 'aria-label="' + accessibleTitle
      + '"' + (selectedNode ? ' aria-current="true"' : '') + '>';
    if (node.kind === 'start') {
      html += '<span class="orch-node-ribbon orch-ribbon-in">'
        + _escape(_translate('orch.ribbon.input')) + '</span>';
    } else if (node.kind === 'stop') {
      html += '<span class="orch-node-ribbon orch-ribbon-out">'
        + _escape(_translate('orch.ribbon.result')) + '</span>';
    }
    if (hasInput) {
      html += '<button type="button" class="orch-port orch-port-in" '
        + inputTab + ' aria-label="'
        + _escape(_translate('orch.port.input', { name: title }))
        + '"></button>';
    }
    html += '<div class="orch-node-head"'
      + (node.type === 'subflow'
        ? ' title="' + _escape(_translate('orch.group.chipTip')) + '"' : '') + '>'
      + '<button type="button" class="orch-node-select" aria-label="' + accessibleTitle
      + '" aria-pressed="' + selectedNode + '">'
      + '<span class="orch-node-icon">' + view.icon + '</span>'
      + '<span class="orch-node-title">' + title + '</span>'
      + (issues && issues.total
        ? '<span class="orch-node-issues" title="' + _escape(issueLabel)
          + '" aria-hidden="true">' + (issues.errors ? '!' : '△')
          + issues.total + '</span>' : '') + '</button>'
      + '<button type="button" class="orch-node-del" title="'
      + _escape(_translate('orch.btn.deleteNode')) + '"' + localTab
      + ' aria-label="'
      + _escape(_translate('orch.btn.deleteNode')) + '">'
      + ((options.icons || {}).reject || '') + '</button></div>'
      + '<div class="orch-node-sub">' + view.subtitle + '</div>';
    if (hasOutput) {
      html += '<button type="button" class="orch-port orch-port-out" aria-label="'
        + _escape(_translate('orch.port.output', { name: title }))
        + '"' + localTab + ' aria-pressed="'
        + (connectingFrom === node.id) + '"></button>';
    }
    return html + '</div>';
  }

  return {
    cardHtml: cardHtml,
    autoLabel: autoLabel,
    kindLabel: kindLabel,
    nodeBlurb: nodeBlurb,
    inspectorAvatar: inspectorAvatar,
    controlSubtitle: controlSubtitle,
    groupSubtitle: groupSubtitle,
    ioBadge: ioBadge,
  };
}

/* ===== migrated source: orchestration-node-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-node-view.js — Studio node-card DOM interaction

   Reconciles projected cards with the Canvas and binds their local pointer,
   keyboard and focus behavior. Presentation lives in
   orchestration-node-presentation.js; graph mutations remain callbacks into
   the editor.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationNodeView(options) {
  options = options || {};
  var presentation = options.presentation
    || createOrchestrationNodePresentation(options);

  function _keyboardPort(event, id, side) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    event.stopPropagation();
    if (typeof options.onPortKeyDown === 'function') {
      options.onPortKeyDown(event, id, side);
    }
  }

  function _bindCard(card) {
    var id = card.getAttribute('data-node-id');
    card.addEventListener('pointerdown', function () {
      if (typeof options.onSelect === 'function') options.onSelect(id);
    });
    var select = card.querySelector('.orch-node-select');
    if (select) select.addEventListener('keydown', function (event) {
      if (typeof options.onNodeKeyDown === 'function') options.onNodeKeyDown(event, id);
    });
    var head = card.querySelector('.orch-node-head');
    var avatar = card.querySelector('.orch-node-icon img');
    if (avatar) avatar.addEventListener('error', function () {
      avatar.style.display = 'none';
    });
    if (head) {
      head.addEventListener('pointerdown', function (event) {
        if (typeof options.onHeaderPointerDown === 'function') {
          options.onHeaderPointerDown(event, id);
        }
      });
      head.addEventListener('dblclick', function () {
        var nodes = typeof options.nodes === 'function' ? options.nodes() : [];
        var node = nodes.filter(function (item) { return item.id === id; })[0];
        if (node && node.type === 'subflow' && typeof options.onEnterGroup === 'function') {
          options.onEnterGroup(id);
        }
      });
    }
    var remove = card.querySelector('.orch-node-del');
    if (remove) {
      remove.addEventListener('pointerdown', function (event) { event.stopPropagation(); });
      remove.addEventListener('click', function (event) {
        event.stopPropagation();
        if (typeof options.onDelete === 'function') options.onDelete(id);
      });
    }
    var input = card.querySelector('.orch-port-in');
    if (input) {
      input.addEventListener('pointerup', function (event) {
        if (typeof options.onPortUp === 'function') options.onPortUp(event, id);
      });
      input.addEventListener('keydown', function (event) {
        _keyboardPort(event, id, 'in');
      });
    }
    var output = card.querySelector('.orch-port-out');
    if (output) {
      output.addEventListener('pointerdown', function (event) {
        if (typeof options.onPortDown === 'function') options.onPortDown(event, id);
      });
      output.addEventListener('keydown', function (event) {
        _keyboardPort(event, id, 'out');
      });
    }
  }

  function render(wrap) {
    if (!wrap) return;
    var focusedNodeId = null;
    var focusedControl = '';
    var active = (options.document || document).activeElement;
    var activeCard = active && typeof active.closest === 'function'
      ? active.closest('.orch-node') : null;
    if (activeCard && wrap.contains(activeCard)) {
      focusedNodeId = activeCard.getAttribute('data-node-id');
      [
        '.orch-node-select', '.orch-node-del',
        '.orch-port-in', '.orch-port-out',
      ].some(function (selector) {
        if (!active.matches(selector)) return false;
        focusedControl = selector;
        return true;
      });
    }
    var nodes = typeof options.nodes === 'function' ? options.nodes() : [];
    var selected = typeof options.selectedId === 'function' ? options.selectedId() : null;
    var connectingFrom = typeof options.connectingFrom === 'function'
      ? options.connectingFrom() : null;
    wrap.innerHTML = nodes.map(function (node) {
      return presentation.cardHtml(node, selected, connectingFrom);
    }).join('');
    var cards = Array.prototype.slice.call(wrap.querySelectorAll('.orch-node'));
    cards.forEach(_bindCard);
    var focusedCard = cards.filter(function (card) {
      return card.getAttribute('data-node-id') === focusedNodeId;
    })[0] || null;
    var focusedElement = focusedCard && focusedControl
      ? focusedCard.querySelector(focusedControl) : null;
    var keyboard = createOrchestrationRovingItemsController({
      root: wrap,
      selector: '.orch-node-select',
    });
    keyboard.sync((focusedControl === '.orch-node-select' && focusedElement)
      || wrap.querySelector('.orch-node.is-selected .orch-node-select'));
    if (focusedElement && focusedElement.tabIndex >= 0
        && typeof focusedElement.focus === 'function') {
      focusedElement.focus({ preventScroll: true });
    }
  }

  return {
    render: render,
    autoLabel: presentation.autoLabel,
    kindLabel: presentation.kindLabel,
    nodeBlurb: presentation.nodeBlurb,
    inspectorAvatar: presentation.inspectorAvatar,
    controlSubtitle: presentation.controlSubtitle,
    groupSubtitle: presentation.groupSubtitle,
    ioBadge: presentation.ioBadge,
  };
}

/* ===== migrated source: orchestration-node-editor.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-node-editor.js — node field and parameter mutations

   Owns the canonical Inspector-to-graph write seam. It normalizes typed
   values, omits empty optional parameters, coalesces text-edit history and
   requests presentation refreshes without depending on the DOM.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationNodeEditor(options) {
  options = options || {};

  function _findNode(id) {
    return typeof options.findNode === 'function' ? options.findNode(id) : null;
  }

  function _selectedNodeId() {
    return typeof options.selectedNodeId === 'function'
      ? options.selectedNodeId() : null;
  }

  function _targetNode(nodeId) {
    return _findNode(nodeId || _selectedNodeId());
  }

  function _historyGroup(node, key, coalesce) {
    return coalesce ? 'param:' + node.id + ':' + key : '';
  }

  function _changed(node, key, coalesce, renderInspector) {
    if (typeof options.markDirty === 'function') {
      options.markDirty(_historyGroup(node, key, coalesce));
    }
    if (typeof options.renderNodes === 'function') options.renderNodes();
    if (renderInspector && typeof options.renderInspector === 'function') {
      options.renderInspector();
    }
  }

  function setParamResult(nodeId, key, value, kind, coalesce) {
    var node = _targetNode(nodeId);
    if (!node || !key) return { ok: false, reason: !node
      ? 'missing-target' : 'missing-key' };

    if (key === 'name') {
      if (node.name === value) return { ok: true };
      node.name = value;
      _changed(node, key, coalesce, false);
      return { ok: true };
    }

    // A subflow role is its outward face, so it remains a node field rather
    // than leaking into the execution params object.
    if (key === 'role') {
      if (node.role === value) return { ok: true };
      node.role = value;
      _changed(node, key, coalesce, true);
      return { ok: true };
    }

    var spec = typeof options.fieldSpec === 'function'
      ? options.fieldSpec(node, key) : null;
    var normalized = normalizeOrchestrationFieldDraftValue(
      kind, value, spec, options.fieldValueContract);
    if (!normalized.ok) return normalized;

    var params = node.params && typeof node.params === 'object'
      && !Array.isArray(node.params) ? node.params : null;
    if (!normalized.present) {
      if (!params || !Object.prototype.hasOwnProperty.call(params, key)) {
        return { ok: true };
      }
      delete params[key];
    } else {
      if (params && Object.prototype.hasOwnProperty.call(params, key)
          && orchestrationFieldDraftValuesEqual(
            params[key], normalized.value)) return { ok: true };
      if (!params) {
        params = {};
        node.params = params;
      }
      params[key] = normalized.value;
    }
    _changed(node, key, coalesce, key === 'mode');
    return { ok: true };
  }

  function setParam(nodeId, key, value, kind, coalesce) {
    return setParamResult(nodeId, key, value, kind, coalesce).ok;
  }

  return { setParam: setParam, setParamResult: setParamResult };
}

/* ===== migrated source: orchestration-inspector-content.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-inspector-content.js — Inspector read-only content provider

   Builds headers, collapsible sections, run traces, backend personas and
   control-flow summaries. Inspector selection/layout lives in
   orchestration-inspector-view.js; editable FieldSpecs live in
   orchestration-inspector.js. Loads after orchestration-graph.js so all
   surfaces partition incoming/outgoing edges through one topology seam.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationInspectorContent(options) {
  options = options || {};

  function _escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }
  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function _richCopy(value) {
    return typeof options.richCopy === 'function'
      ? options.richCopy(value) : _escape(value);
  }
  function _edges() {
    return typeof options.edges === 'function' ? options.edges() : [];
  }
  function _find(id) {
    return typeof options.findNode === 'function' ? options.findNode(id) : null;
  }
  function _nodeLabel(node) {
    return typeof options.nodeLabel === 'function'
      ? options.nodeLabel(node) : (node.name || node.id || '');
  }
  function _traceSnapshot(nodeId) {
    if (typeof options.traceSnapshotFor === 'function') {
      var snapshot = options.traceSnapshotFor(nodeId);
      if (snapshot && typeof snapshot === 'object'
          && Array.isArray(snapshot.attempts)) return snapshot;
    }
    var trace = typeof options.traceFor === 'function'
      ? options.traceFor(nodeId) : null;
    var history = typeof options.traceHistoryFor === 'function'
      ? options.traceHistoryFor(nodeId) : [];
    history = Array.isArray(history) ? history.slice() : [];
    var count = typeof options.traceCountFor === 'function'
      ? options.traceCountFor(nodeId) : 0;
    var attempts = projectOrchestrationTraceAttempts(trace, history, count);
    return Object.freeze({
      current: trace,
      history: Object.freeze(history),
      total: attempts.length ? attempts[attempts.length - 1].total : count,
      attempts: attempts,
    });
  }

  function header(node) {
    var avatar = typeof options.avatar === 'function' ? options.avatar(node) : '';
    var kind = typeof options.kindLabel === 'function'
      ? options.kindLabel(node) : '';
    var blurb = typeof options.blurb === 'function' ? options.blurb(node) : '';
    var html = '<div class="orch-insp-head">' + avatar
      + '<div class="orch-insp-htext">'
      + '<span class="orch-insp-kind">' + _escape(kind) + '</span>'
      + '<span class="orch-insp-type">' + _escape(_nodeLabel(node)) + '</span>'
      + '</div></div>';
    if (blurb) {
      html += '<div class="orch-insp-blurb">' + _escape(blurb) + '</div>';
    }
    return html;
  }

  function section(titleKey, icon, open, inner, hintKey) {
    var html = '<details class="orch-sec" data-orch-section-key="'
      + _escape(titleKey) + '"' + (open ? ' open' : '') + '>';
    html += '<summary class="orch-sec-sum">' + (icon || '')
      + '<span>' + _escape(_translate(titleKey)) + '</span>'
      + '<span class="orch-sec-chev">\u203a</span></summary>'
      + '<div class="orch-sec-body">';
    if (hintKey) {
      html += '<div class="orch-sec-hint">'
        + _richCopy(_translate(hintKey)) + '</div>';
    }
    return html + (inner || '') + '</div></details>';
  }

  function _traceAttemptBody(trace) {
    var statusProjection = projectOrchestrationTraceStatusPresentation(
      trace.status, options.traceContract, _translate);
    var status = statusProjection.status;
    var html = '<div class="orch-runtrace-row"><span class="orch-runtrace-lbl">'
      + _escape(_translate('orch.run.status')) + '</span>'
      + '<span class="orch-runtrace-status orch-runtrace-'
      + _escape(status) + '">' + _escape(statusProjection.label)
      + '</span></div>';
    var activity = projectOrchestrationTraceActivity(
      trace, options.traceContract);
    if (activity.stateChanging > 0) {
      html += '<div class="orch-runtrace-row"><span class="orch-runtrace-lbl">'
        + _escape(_translate('orch.run.actions')) + '</span><span>'
        + activity.stateChanging + '</span></div>';
    }
    var output = projectOrchestrationTraceSections(
      trace, ['output'], options.traceContract)[0];
    if (output) {
      html += '<div class="orch-runtrace-lbl orch-runtrace-outlbl">'
        + _escape(_translate('orch.run.output'))
        + (output.truncated ? ' <span class="orch-runtrace-trunc">'
          + _escape(_translate('orch.run.truncated')) + '</span>' : '')
        + '</div>'
        + '<pre class="orch-runtrace-out">'
        + _escape(output.text) + '</pre>';
    } else if (status === 'running') {
      var phaseText = '';
      if (trace.phaseDetailKey) {
        phaseText = _translate(
          trace.phaseDetailKey, trace.phaseDetailArgs || {}
        );
        if (phaseText === trace.phaseDetailKey) phaseText = '';
      }
      if (!phaseText && trace.phase) {
        var phaseKey = 'orch.run.phase.' + trace.phase;
        phaseText = _translate(phaseKey);
        if (phaseText === phaseKey) phaseText = '';
      }
      html += '<div class="orch-runtrace-waiting">'
        + _escape(phaseText || _translate('orch.run.streaming')) + '</div>';
    }
    return html;
  }

  function runTraceBody(node) {
    var attempts = _traceSnapshot(node.id).attempts;
    if (!attempts.length) return null;
    if (attempts.length === 1) return _traceAttemptBody(attempts[0].trace);
    return '<div class="orch-runtrace-attempts">'
      + attempts.map(function (attempt, index) {
        var status = projectOrchestrationTraceStatusPresentation(
          attempt.trace.status, options.traceContract, _translate);
        var delta = index > 0
          ? projectOrchestrationTraceAttemptDeltaPresentation(
            attempts[index - 1].trace, attempt.trace,
            options.traceContract, _translate
          ) : null;
        return '<details class="orch-sec orch-runtrace-attempt" '
          + 'data-orch-section-key="trace-' + _escape(attempt.key) + '"'
          + (attempt.current ? ' open' : '') + '>'
          + '<summary class="orch-sec-sum"><span>'
          + _escape(_translate('tm.trace.iter')) + ' ' + attempt.ordinal
          + ' / ' + attempt.total + ' · ' + _escape(status.label)
          + '</span>' + (delta && delta.label
            ? '<span class="orch-runtrace-delta">'
              + _escape(delta.label) + '</span>' : '')
          + '<span class="orch-sec-chev">\u203a</span></summary>'
          + '<div class="orch-sec-body">' + _traceAttemptBody(attempt.trace)
          + '</div></details>';
      }).join('') + '</div>';
  }

  function personaSectionBody(node) {
    var persona = typeof options.persona === 'function'
      ? options.persona(node.role) : null;
    if (!persona || !persona.prompt) {
      return '<div class="orch-persona-empty">'
        + _escape(_translate('orch.persona.none')) + '</div>';
    }
    return '<div class="orch-persona-lbl orch-persona-promptlbl">'
      + _escape(_translate('orch.persona.prompt')) + '</div>'
      + '<pre class="orch-persona-prompt" readonly>'
      + _escape(persona.prompt) + '</pre>';
  }

  function flowSummaryBody(node) {
    var connections = orchestrationConnections(_edges(), node.id);
    var incoming = connections.incoming
      .map(function (edge) {
        var source = _find(edge.from);
        return source ? _nodeLabel(source) : edge.from;
      });
    var outgoing = connections.outgoing
      .map(function (edge) {
        var target = _find(edge.to);
        return target ? _nodeLabel(target) : edge.to;
      });
    var inputText;
    var outputText;
    if (node.kind === 'start') {
      var seed = String(node.params && node.params.seed || '').trim();
      inputText = _escape(seed
        ? _translate('orch.flow.seedSet') : _translate('orch.flow.fromUser'));
    } else {
      inputText = incoming.length
        ? incoming.map(_escape).join(', ') : _escape(_translate('orch.flow.none'));
    }
    outputText = node.kind === 'stop'
      ? _escape(_translate('orch.flow.toChat'))
      : (outgoing.length
        ? outgoing.map(_escape).join(', ') : _escape(_translate('orch.flow.none')));
    var html = '<div class="orch-flow-row"><span class="orch-flow-arrow">\u2192</span>'
      + '<span class="orch-flow-lbl">' + _escape(_translate('orch.flow.in'))
      + '</span><span class="orch-flow-val">' + inputText + '</span></div>'
      + '<div class="orch-flow-row"><span class="orch-flow-arrow">\u2190</span>'
      + '<span class="orch-flow-lbl">' + _escape(_translate('orch.flow.out'))
      + '</span><span class="orch-flow-val">' + outputText + '</span></div>';
    var carryKey = 'orch.flow.carry.' + node.kind;
    var carry = _translate(carryKey);
    if (carry && carry !== carryKey) {
      html += '<div class="orch-flow-carry">' + _escape(carry) + '</div>';
    }
    return html;
  }

  return {
    header: header,
    section: section,
    runTraceBody: runTraceBody,
    personaSectionBody: personaSectionBody,
    flowSummaryBody: flowSummaryBody,
    traceSnapshot: _traceSnapshot,
  };
}

/* ===== migrated source: orchestration-inspector-focus.js ===== */
/* Semantic focus continuity for Inspector forms that repaint after edits. */

function createOrchestrationInspectorFocusController(options) {
  options = options || {};
  var validity = options.fieldValidity
    || createOrchestrationFieldValidity();
  var attributes = [
    'data-orch-param-key', 'data-input-index',
    'data-orch-io-action', 'data-orch-io-side', 'data-orch-io-index',
    'data-orch-io-key', 'data-orch-io-preset',
    'data-orch-inspector-action',
  ];

  function _document() { return options.document || document; }

  function capture(root) {
    var active = _document().activeElement;
    if (!root || !active || !root.contains(active)) return null;
    var snapshot = {};
    attributes.forEach(function (name) {
      if (active.hasAttribute(name)) {
        snapshot[name] = active.getAttribute(name) || '';
      }
    });
    return Object.keys(snapshot).length ? snapshot : null;
  }

  function _matching(root, selector, snapshot, ignored) {
    var ignoredNames = Array.isArray(ignored) ? ignored : [ignored || ''];
    return Array.prototype.filter.call(root.querySelectorAll(selector),
      function (control) {
        return attributes.every(function (name) {
          return ignoredNames.indexOf(name) !== -1
            || !Object.prototype.hasOwnProperty.call(
            snapshot, name) || control.getAttribute(name) === snapshot[name];
        });
      });
  }

  function _ioTarget(root, snapshot) {
    var action = snapshot['data-orch-io-action'];
    var controls = _matching(
      root, '[data-orch-io-action]', snapshot,
      action === 'add' ? ['data-orch-io-action']
        : action === 'remove'
          ? ['data-orch-io-action', 'data-orch-io-index'] : []
    );
    if (action === 'add') {
      var additions = controls.filter(function (control) {
        return control.getAttribute('data-orch-io-action') === 'set'
          && control.getAttribute('data-orch-io-key') === 'name';
      });
      return additions[additions.length - 1]
        || _matching(root, '[data-orch-io-action="add"]', snapshot)[0];
    }
    if (action === 'remove') {
      var removals = controls.filter(function (control) {
        return control.getAttribute('data-orch-io-action') === 'remove';
      });
      var oldIndex = Number(snapshot['data-orch-io-index']);
      return removals[Math.min(oldIndex, removals.length - 1)]
        || _matching(root, '[data-orch-io-action="add"]', snapshot,
          ['data-orch-io-action', 'data-orch-io-index'])[0];
    }
    return controls[0] || null;
  }

  function restore(root, snapshot) {
    if (!root || !snapshot) return null;
    var target = null;
    if (Object.prototype.hasOwnProperty.call(
      snapshot, 'data-orch-param-key')) {
      target = _matching(root, '[data-orch-param-key]', snapshot)[0];
    } else if (Object.prototype.hasOwnProperty.call(
      snapshot, 'data-input-index')) {
      target = _matching(root, '[data-input-index]', snapshot)[0];
    } else if (Object.prototype.hasOwnProperty.call(
      snapshot, 'data-orch-io-action')) {
      target = _ioTarget(root, snapshot);
    } else if (Object.prototype.hasOwnProperty.call(
      snapshot, 'data-orch-inspector-action')) {
      target = _matching(root, '[data-orch-inspector-action]', snapshot)[0];
    }
    if (target && typeof target.focus === 'function') target.focus();
    return target || null;
  }

  function clearDiagnostic() {
    validity.clearDiagnostics(_document());
  }

  function _diagnosticField(root, target) {
    if (!target || !target.field) return null;
    if (target.field.kind === 'document-name') {
      return _document().getElementById('orchNameInput');
    }
    if (!root) return null;
    if (target.field.kind === 'param') {
      return _matching(root, '[data-orch-param-key]', {
        'data-orch-param-key': target.field.key,
      })[0] || null;
    }
    if (target.field.kind === 'io-section') {
      return _matching(root, '[data-orch-io-action]', {
        'data-orch-io-side': target.field.side || '',
      }, target.field.side ? [] : ['data-orch-io-side'])[0] || null;
    }
    if (target.field.kind !== 'io') return null;
    return _matching(root, '[data-orch-io-action="set"]', {
      'data-orch-io-action': 'set',
      'data-orch-io-side': target.field.side,
      'data-orch-io-index': String(target.field.index),
      'data-orch-io-key': target.field.key,
    })[0] || null;
  }

  function focusDiagnostic(root, target, diagnostic, scrollBehavior,
                           descriptionId) {
    clearDiagnostic();
    var field = _diagnosticField(root, target);
    if (!field) return null;
    var section = typeof field.closest === 'function'
      ? field.closest('details[data-orch-section-key]') : null;
    if (section) section.open = true;
    var severity = diagnostic && diagnostic.severity === 'warning'
      ? 'warning' : 'error';
    validity.setDiagnostic(field, severity, descriptionId);
    if (typeof field.focus === 'function') {
      try { field.focus({ preventScroll: true }); }
      catch (_error) { field.focus(); }
    }
    if (typeof field.scrollIntoView === 'function') {
      field.scrollIntoView({
        block: 'center', behavior: scrollBehavior === 'smooth' ? 'smooth' : 'auto',
      });
    }
    return field;
  }

  return {
    capture: capture,
    restore: restore,
    focusDiagnostic: focusDiagnostic,
    clearDiagnostic: clearDiagnostic,
  };
}

/* ===== migrated source: orchestration-inspector-interaction.js ===== */
/* Inspector section memory, local event binding and field validity feedback. */

function createOrchestrationInspectorInteraction(options) {
  options = options || {};
  var disclosureState = options.disclosureState
    || createOrchestrationDisclosureState();
  var focus = options.focusController
    || createOrchestrationInspectorFocusController(options);
  var validity = options.fieldValidity
    || createOrchestrationFieldValidity();
  var fieldErrorSerial = 0;

  function _fieldFailureText(result) {
    result = result && typeof result === 'object' ? result : {};
    var labels = {
      'field.max_length': 'orch.field.errorMaxLength',
      'field.max_items': 'orch.field.errorMaxItems',
      'field.max_item_length': 'orch.field.errorMaxItemLength',
      'field.type.integer': 'orch.field.errorNumber',
      'field.contract.unsupported': 'orch.field.errorUnsupported',
      'max-length': 'orch.field.errorMaxLength',
      'max-items': 'orch.field.errorMaxItems',
      'max-item-length': 'orch.field.errorMaxItemLength',
      'invalid-number': 'orch.field.errorNumber',
      'unsupported-contract': 'orch.field.errorUnsupported',
    };
    var key = labels[result.code] || labels[result.reason]
      || 'orch.field.errorInvalid';
    return typeof options.translate === 'function'
      ? options.translate(key, { n: result.limit }) : key;
  }

  function _setFieldValidity(field, result) {
    var accepted = result && typeof result === 'object'
      ? result.ok !== false : result !== false;
    var errorId = field.getAttribute('data-orch-field-error-id');
    var error = errorId ? _document().getElementById(errorId) : null;
    if (accepted) {
      validity.setLocal(field, true);
      if (error) { error.hidden = true; error.textContent = ''; }
      return true;
    }
    if (!error) {
      error = _document().createElement('small');
      error.id = 'orchFieldError-' + (++fieldErrorSerial);
      error.className = 'orch-fld-error';
      error.setAttribute('role', 'alert');
      (field.closest('.orch-fld') || field.parentNode).appendChild(error);
      field.setAttribute('data-orch-field-error-id', error.id);
    }
    validity.setLocal(field, false, error.id,
      String(result.code || result.reason || ''));
    error.textContent = _fieldFailureText(result);
    error.hidden = false;
    return false;
  }

  function _document() { return options.document || document; }

  function scope(context) {
    context = context || {};
    var workspace = typeof options.workspaceToken === 'function'
      ? options.workspaceToken() : '';
    var subject = context.node
      ? 'node:' + context.node.id
      : (context.edge ? 'edge:' + context.edge.id : 'none');
    return orchestrationScrollScope([workspace || 'root', subject]);
  }

  function setMobileOpen(element, open) {
    if (typeof options.isMobile !== 'function' || !options.isMobile()) return;
    if (typeof options.setMobileOpen === 'function') {
      options.setMobileOpen(!!open);
    }
  }

  function _restoreAndBindSections(element, context) {
    disclosureState.bind(element, scope(context), {
      selector: 'details[data-orch-section-key]',
      attribute: 'data-orch-section-key',
    });
  }

  function _bindClose(element) {
    Array.prototype.forEach.call(
      element.querySelectorAll('.orch-inspector-close'), function (button) {
        button.addEventListener('click', function () {
          if (typeof options.closeMobile === 'function') options.closeMobile();
        });
      }
    );
  }

  function _bindEdge(element, edge) {
    Array.prototype.forEach.call(
      element.querySelectorAll('.orch-edge-binding'), function (select) {
        select.addEventListener('change', function () {
          if (typeof options.bindEdgeInput === 'function') {
            options.bindEdgeInput(
              edge.to, Number(select.getAttribute('data-input-index')),
              select.value
            );
          }
        });
      }
    );
    var reverse = element.querySelector(
      '[data-orch-inspector-action="reverse-edge"]');
    var remove = element.querySelector(
      '[data-orch-inspector-action="delete-edge"]');
    if (reverse) reverse.addEventListener('click', function () {
      if (typeof options.reverseEdge === 'function') {
        options.reverseEdge(edge.id);
      }
    });
    if (remove) remove.addEventListener('click', function () {
      if (typeof options.deleteEdge === 'function') options.deleteEdge(edge.id);
    });
  }

  function _bindFields(element, node) {
    Array.prototype.forEach.call(
      element.querySelectorAll('[data-orch-param-key]'), function (field) {
        var eventName = field.tagName === 'SELECT'
          || (field.tagName === 'INPUT' && field.type === 'checkbox')
          ? 'change' : 'input';
        field.addEventListener(eventName, function () {
          if (typeof options.setParam !== 'function') return;
          var value = field.type === 'checkbox' ? field.checked : field.value;
          var args = [node.id,
            field.getAttribute('data-orch-param-key') || '', value,
            field.getAttribute('data-orch-param-kind') || '',
            eventName === 'input'];
          var result = typeof options.setParamResult === 'function'
            ? options.setParamResult.apply(null, args)
            : options.setParam.apply(null, args);
          _setFieldValidity(field, result);
        });
      }
    );
  }

  function _bindNode(element, node) {
    _bindFields(element, node);
    if (typeof options.bindIoSection === 'function') {
      options.bindIoSection(element, node.id);
    }
    var enter = element.querySelector(
      '[data-orch-inspector-action="enter-group"]');
    var remove = element.querySelector(
      '[data-orch-inspector-action="delete-node"]');
    if (enter) enter.addEventListener('click', function () {
      if (typeof options.enterGroup === 'function') options.enterGroup(node.id);
    });
    if (remove) remove.addEventListener('click', function () {
      if (typeof options.deleteNode === 'function') options.deleteNode(node.id);
    });
  }

  function bind(element, context) {
    context = context || {};
    _restoreAndBindSections(element, context);
    _bindClose(element);
    if (context.edge) {
      _bindEdge(element, context.edge);
      return;
    }
    if (context.node) _bindNode(element, context.node);
  }

  return {
    bind: bind,
    captureFocus: focus.capture,
    restoreFocus: focus.restore,
    focusDiagnostic: focus.focusDiagnostic,
    scope: scope,
    setMobileOpen: setMobileOpen,
  };
}

/* ===== migrated source: orchestration-inspector-projection.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-inspector-projection.js — pure Inspector HTML projection

   Projects node, edge and empty selections from injected graph/catalogue
   ports. DOM replacement, focus, scroll and interaction binding stay in the
   Inspector View.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationInspectorProjection(options) {
  options = options || {};

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function _escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }
  function _nodes() {
    return typeof options.nodes === 'function' ? options.nodes() : [];
  }
  function _edges() {
    return typeof options.edges === 'function' ? options.edges() : [];
  }
  function findNode(id) {
    if (typeof options.findNode === 'function') return options.findNode(id);
    return _nodes().filter(function (node) { return node.id === id; })[0] || null;
  }
  function findEdge(id) {
    return _edges().filter(function (edge) { return edge.id === id; })[0] || null;
  }
  function _nodeLabel(id, node) {
    if (typeof options.nodeLabel === 'function') return options.nodeLabel(id);
    return node ? (node.name || options.autoLabel(node)) : id;
  }
  function _section(key, icon, open, inner, hint) {
    return typeof options.section === 'function'
      ? options.section(key, icon, open, inner, hint) : inner;
  }
  function _select(label, key, value, choices) {
    return typeof options.selectField === 'function'
      ? options.selectField(label, key, value, choices) : '';
  }
  function _nodeParam(node, key) {
    if (typeof options.nodeParam === 'function') {
      return options.nodeParam(node, key);
    }
    var params = node && node.params;
    return params && typeof params === 'object'
      && Object.prototype.hasOwnProperty.call(params, key)
      ? params[key] : null;
  }

  function _executionChoices(axis) {
    var contract = typeof options.executionOptions === 'function'
      ? options.executionOptions() : {};
    var values = Array.isArray(contract[axis]) ? contract[axis] : [];
    return values.map(function (value) {
      return [value, orchestrationExecutionOptionLabel(
        axis, value, _translate, 'editor')];
    });
  }

  function _roleChoices() {
    var roles = typeof options.roles === 'function' ? options.roles() : [];
    return roles.map(function (role) {
      var key = 'orch.roleName.' + role.role;
      var translated = _translate(key);
      return [role.role, translated && translated !== key
        ? translated : (role.label || role.role)];
    });
  }

  function _mobileHeader(label) {
    var icons = options.icons || {};
    return '<div class="orch-sheet-head orch-m-only"><span>'
      + (icons.gear || '') + ' ' + _escape(label) + '</span>'
      + '<button type="button" class="orch-icon-btn orch-inspector-close" title="'
      + _escape(_translate('orch.tip.close')) + '" aria-label="'
      + _escape(_translate('orch.tip.close')) + '">'
      + (icons.reject || '') + '</button></div>';
  }

  function edgeHtml(edge) {
    var from = findNode(edge.from);
    var to = findNode(edge.to);
    var fromLabel = _nodeLabel(edge.from, from);
    var toLabel = _nodeLabel(edge.to, to);
    var fromHtml = _escape(fromLabel);
    var toHtml = _escape(toLabel);
    var html = _mobileHeader(_translate('orch.edge.title'))
      + '<div class="orch-insp-head"><span class="orch-insp-kind">'
      + _escape(_translate('orch.edge.title')) + '</span>'
      + '<span class="orch-insp-type">' + fromHtml + ' → ' + toHtml
      + '</span></div><div class="orch-edge-flow"><b>' + fromHtml
      + '</b> <span class="orch-edge-arrowtxt">→</span> <b>' + toHtml
      + '</b></div>';

    var inputPorts = to && typeof options.nodeInputs === 'function'
      ? options.nodeInputs(to) : [];
    if (inputPorts.length && from) {
      var sourceOutputs = typeof options.nodeOutputs === 'function'
        ? options.nodeOutputs(from) : [];
      html += '<div class="orch-note orch-note-wire">'
        + _escape(_translate('orch.edge.bindNote')) + '</div>';
      inputPorts.forEach(function (port, index) {
        var choices = [['', _translate('orch.edge.bindNone')]];
        sourceOutputs.forEach(function (output) {
          var ref = typeof options.outputRef === 'function'
            ? options.outputRef(from.id, sourceOutputs, output) : from.id;
          choices.push([ref, output.name + ' (' + (output.type || 'any') + ')']);
        });
        var current = port.from && (port.from === from.id
          || port.from.indexOf(from.id + '.') === 0) ? port.from : '';
        var choicesHtml = choices.map(function (choice) {
          return '<option value="' + _escape(choice[0]) + '"'
            + (choice[0] === current ? ' selected' : '') + '>'
            + _escape(choice[1]) + '</option>';
        }).join('');
        html += '<label class="orch-fld"><span>'
          + _escape(_translate('orch.edge.bindTo', { port: port.name }))
          + '</span><select class="orch-input orch-edge-binding" data-input-index="'
          + index + '">' + choicesHtml + '</select></label>';
      });
    }
    return html + '<div class="orch-edge-btns">'
      + '<button type="button" class="orch-btn orch-btn-ghost orch-btn-block" '
      + 'data-orch-inspector-action="reverse-edge">'
      + _escape(_translate('orch.edge.reverse')) + '</button>'
      + '<button type="button" class="orch-btn orch-btn-danger orch-btn-block" '
      + 'data-orch-inspector-action="delete-edge">'
      + _escape(_translate('orch.edge.delete')) + '</button></div>';
  }

  function _groupHtml(node) {
    var icons = options.icons || {};
    var params = node.params || {};
    var definition = params.definition || {};
    var html = '<button type="button" class="orch-btn orch-btn-primary '
      + 'orch-btn-block orch-insp-cta" data-orch-inspector-action="enter-group">'
      + _escape(_translate('orch.group.open'))
      + ' <span class="orch-insp-cta-sub">'
      + _escape(_translate('orch.group.summary', {
        n: (definition.nodes || []).length,
        m: (definition.edges || []).length,
      })) + '</span></button>';
    var identity = options.labelField(node)
      + _select(_translate('orch.fld.groupFace'), 'role', node.role, _roleChoices());
    html += _section('orch.sec.identity', icons.gear, true, identity);
    var execution = _select(
      _translate('orch.fld.groupScope'), 'scope', _nodeParam(node, 'scope'),
      _executionChoices('scopes')
    ) + _select(
      _translate('orch.fld.emits'), 'emits', _nodeParam(node, 'emits'),
      [['', _translate('orch.emits.auto', {
        role: options.defaultEmits(node.role),
      })]].concat(_executionChoices('emits'))
    );
    html += _section('orch.sec.execution', icons.gear, false, execution,
                     'orch.note.group');
    html += _section('orch.sec.io', icons.package, false,
                     options.ioSectionBody(node), 'orch.io.note');
    return html;
  }

  function _roleHtml(node) {
    var icons = options.icons || {};
    var params = node.params || {};
    var html = _section('orch.sec.task', icons.flag, true,
                        options.roleTaskBody(node), 'orch.task.note');
    var trace = options.runTraceBody(node);
    if (trace) {
      html += _section('orch.sec.lastRun', icons.rocket, true, trace,
                       'orch.run.note');
    }
    var execution = options.labelField(node)
      + _select(_translate('orch.fld.tier'), 'tier', _nodeParam(node, 'tier'),
                _executionChoices('tiers'))
      + _select(_translate('orch.fld.context'), 'isolation',
                _nodeParam(node, 'isolation'),
                _executionChoices('isolation'))
      + _select(_translate('orch.fld.emits'), 'emits',
                _nodeParam(node, 'emits'),
        [['', _translate('orch.emits.auto', {
          role: options.defaultEmits(node.role),
        })]].concat(_executionChoices('emits')));
    html += _section('orch.sec.execution', icons.gear, false, execution,
                     'orch.note.exec');
    var ioContract = params.io;
    var hasExplicitIo = !!(ioContract && (
      Array.isArray(ioContract.inputs) && ioContract.inputs.length
      || Array.isArray(ioContract.outputs) && ioContract.outputs.length
    ));
    html += _section('orch.sec.io', icons.package, hasExplicitIo,
                     options.ioSectionBody(node), 'orch.io.note');
    html += _section('orch.sec.persona', icons.bot, false,
                     options.personaBody(node), 'orch.persona.note');
    return html;
  }

  function _controlHtml(node) {
    var icons = options.icons || {};
    var fields = typeof options.controlFields === 'function'
      ? options.controlFields(node.kind) : [];
    var settings = options.labelField(node)
      + options.controlSchemaSection(node, fields);
    var hint = ({
      loop: 'orch.note.loop', artifact: 'orch.note.artifact',
      human: 'orch.note.human', start: 'orch.note.start',
      stop: 'orch.note.stop',
    })[node.kind] || null;
    return _section('orch.sec.flow', icons.package, true,
                    options.flowSummaryBody(node), 'orch.flow.note')
      + _section('orch.sec.settings', icons.gear, true, settings, hint);
  }

  function nodeHtml(node) {
    var html = _mobileHeader(options.kindLabel(node));
    html += options.header(node);
    if (node.type === 'subflow') html += _groupHtml(node);
    else if (node.type === 'role') html += _roleHtml(node);
    else html += _controlHtml(node);

    var connections = orchestrationConnections(_edges(), node.id);
    return html + '<div class="orch-insp-foot"><div class="orch-conn-box">'
      + '<div class="orch-conn-row">' + _escape(_translate('orch.conn.in'))
      + ' <b>' + connections.incoming.length
      + '</b></div><div class="orch-conn-row">'
      + _escape(_translate('orch.conn.out')) + ' <b>'
      + connections.outgoing.length
      + '</b> →</div></div><button type="button" '
      + 'class="orch-btn orch-btn-danger orch-btn-block" '
      + 'data-orch-inspector-action="delete-node">'
      + _escape(_translate('orch.btn.deleteNode')) + '</button></div>';
  }

  function emptyHtml() {
    return _mobileHeader(_translate('orch.toolbar.edit'))
      + '<div class="orch-insp-empty"><div class="orch-insp-empty-icon">'
      + ((options.icons || {}).gear || '') + '</div>'
      + _escape(_translate('orch.insp.empty'))
      + '<div class="orch-insp-stats">'
      + _escape(_translate('orch.insp.stats', {
        n: _nodes().length, m: _edges().length,
      })) + '</div></div>';
  }

  return Object.freeze({
    findNode: findNode,
    findEdge: findEdge,
    edgeHtml: edgeHtml,
    nodeHtml: nodeHtml,
    emptyHtml: emptyHtml,
  });
}

/* ===== migrated source: orchestration-inspector-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-inspector-view.js — selected subject DOM lifecycle

   Owns selection, replacement, focus, scroll and interaction binding. Pure
   node/edge/empty HTML lives in orchestration-inspector-projection.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationInspectorView(options) {
  options = options || {};
  var projection = options.projection
    || createOrchestrationInspectorProjection(options);
  var scroll = options.scrollState || createOrchestrationScrollState();
  var interaction = options.interaction
    || createOrchestrationInspectorInteraction(options);

  function doc() { return options.document || document; }

  function render(element) {
    element = element || doc().getElementById('orchInspector');
    if (!element) return;
    var focused = interaction.captureFocus(element);
    scroll.capture(element);
    var selectedEdgeId = typeof options.selectedEdgeId === 'function'
      ? options.selectedEdgeId() : null;
    if (selectedEdgeId) {
      var edge = projection.findEdge(selectedEdgeId);
      if (edge) {
        interaction.setMobileOpen(element, true);
        element.innerHTML = projection.edgeHtml(edge);
        interaction.bind(element, { edge: edge });
        interaction.restoreFocus(element, focused);
        scroll.restore(element, interaction.scope({ edge: edge }));
        return;
      }
      if (typeof options.clearSelectedEdge === 'function') {
        options.clearSelectedEdge();
      }
    }

    var selectedNodeId = typeof options.selectedNodeId === 'function'
      ? options.selectedNodeId() : null;
    var node = selectedNodeId ? projection.findNode(selectedNodeId) : null;
    interaction.setMobileOpen(element, !!node);
    if (!node) {
      element.innerHTML = projection.emptyHtml();
      scroll.restore(element, interaction.scope({}));
      return;
    }
    element.innerHTML = projection.nodeHtml(node);
    interaction.bind(element, { node: node });
    interaction.restoreFocus(element, focused);
    scroll.restore(element, interaction.scope({ node: node }));
  }

  function focusDiagnostic(target, diagnostic, scrollBehavior, descriptionId) {
    var element = doc().getElementById('orchInspector');
    return interaction.focusDiagnostic(
      element, target, diagnostic, scrollBehavior, descriptionId);
  }

  return {
    render: render,
    focusDiagnostic: focusDiagnostic,
    edgeHtml: projection.edgeHtml,
    nodeHtml: projection.nodeHtml,
  };
}

/* ===== migrated source: orchestration-canvas-view.js ===== */
/* Orchestration canvas view composition.
 *
 * Keeps DOM refresh order and empty-state rendering in one presentation-only
 * seam. Graph mutation, persistence and transport remain outside this module.
 */

function createOrchestrationCanvasView(options) {
  options = options || {};
  var doc = options.document
    || (typeof document !== 'undefined' ? document : null);
  var icons = options.icons || {};

  function mount(id) {
    return doc && typeof doc.getElementById === 'function'
      ? doc.getElementById(id) : null;
  }

  function nodeCount() {
    return typeof options.nodeCount === 'function'
      ? Number(options.nodeCount()) || 0 : 0;
  }

  function translate(key) {
    return typeof options.translate === 'function'
      ? options.translate(key) : key;
  }

  function renderNodes() {
    var root = mount('orchNodes');
    if (!root) return;
    if (options.nodeView && typeof options.nodeView.render === 'function') {
      options.nodeView.render(root);
    }
    if (options.viewport && typeof options.viewport.sync === 'function') {
      options.viewport.sync();
    }
  }

  function renderEdges() {
    var svg = mount('orchEdges');
    var canvas = mount('orchCanvas');
    if (!svg || !canvas || !options.edgeView
        || typeof options.edgeView.render !== 'function') return;
    return options.edgeView.render(svg, canvas);
  }

  function renderInspector() {
    var root = mount('orchInspector');
    if (!root || !options.inspectorView
        || typeof options.inspectorView.render !== 'function') return;
    return options.inspectorView.render(root);
  }

  function renderHint() {
    var root = mount('orchHint');
    if (!root) return false;
    var hasNodes = nodeCount() > 0;
    root.style.display = hasNodes ? 'none' : 'block';
    root.replaceChildren();
    if (hasNodes) return false;

    var card = doc.createElement('div');
    card.className = 'orch-hint-card';
    var icon = doc.createElement('div');
    icon.className = 'orch-hint-emoji';
    icon.innerHTML = icons.puzzle || '';
    var title = doc.createElement('div');
    title.className = 'orch-hint-title';
    title.textContent = translate('orch.hint.title');
    var text = doc.createElement('div');
    text.className = 'orch-hint-text';
    if (typeof options.richCopy === 'function') {
      text.innerHTML = options.richCopy(translate('orch.hint.text'));
    } else {
      text.textContent = translate('orch.hint.text');
    }
    card.appendChild(icon);
    card.appendChild(title);
    card.appendChild(text);
    root.appendChild(card);
    return true;
  }

  function renderBreadcrumb() {
    if (!mount('orchCrumb') || !options.navigation
        || typeof options.navigation.renderBreadcrumb !== 'function') return;
    return options.navigation.renderBreadcrumb();
  }

  function render() {
    var input = mount('orchNameInput');
    var name = typeof options.name === 'function' ? options.name() : '';
    if (input && input.value !== name) input.value = name;
    renderNodes();
    renderEdges();
    renderInspector();
    renderHint();
    renderBreadcrumb();
  }

  return {
    render: render,
    renderNodes: renderNodes,
    renderEdges: renderEdges,
    renderInspector: renderInspector,
    renderHint: renderHint,
    renderBreadcrumb: renderBreadcrumb,
  };
}

/* ===== migrated source: orchestration-studio-services.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-studio-services.js — shared Studio application services

   Owns the stable browser/runtime dependencies injected into the Studio
   controller graph. API discovery stays late-bound; presentation services
   retain one identity and error reporting follows one scoped convention.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioServices(options) {
  options = options || {};
  var doc = options.document ||
    (typeof document !== 'undefined' ? document : null);
  var win = options.window ||
    (typeof window !== 'undefined' ? window : null);
  var reporters = Object.create(null);

  function api() {
    if (typeof options.api === 'function') return options.api();
    if (options.api != null) return options.api;
    return typeof runtimeScope.resolveOrchestrationApiClient === 'function'
      ? runtimeScope.resolveOrchestrationApiClient() : null;
  }

  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }

  function richCopy(value) {
    return typeof options.richCopy === 'function'
      ? options.richCopy(value) : escape(value);
  }

  function toast() {
    if (typeof options.toast === 'function') {
      return options.toast.apply(null, arguments);
    }
  }

  function warn() {
    if (typeof options.warn === 'function') {
      return options.warn.apply(null, arguments);
    }
  }

  function choose(config) {
    return typeof options.choose === 'function'
      ? options.choose(config) : Promise.resolve('keep');
  }

  function confirm(message, config, fallback) {
    return typeof options.confirm === 'function'
      ? options.confirm(message, config, fallback)
      : Promise.resolve(fallback === undefined ? true : fallback);
  }

  function reportError(scope, context, error) {
    if (typeof options.reportError === 'function') {
      return reportOrchestrationDiagnostic(
        options.reportError, scope, context, error);
    }
    var logger = options.logger;
    if (logger && typeof logger.warn === 'function') {
      return reportOrchestrationDiagnostic(
        logger.warn.bind(logger),
        '[' + scope + '] ' + context + ' failed:', error);
    }
    return false;
  }

  function reporter(scope, defaultContext) {
    var key = String(scope || 'Orchestration') + '\n'
      + String(defaultContext || 'operation');
    if (!reporters[key]) {
      reporters[key] = function (context, error) {
        if (arguments.length < 2) {
          error = context;
          context = defaultContext || 'operation';
        }
        return reportError(scope || 'Orchestration', context, error);
      };
    }
    return reporters[key];
  }

  return Object.freeze({
    document: doc,
    window: win,
    api: api,
    translate: translate,
    escape: escape,
    richCopy: richCopy,
    toast: toast,
    warn: warn,
    choose: choose,
    confirm: confirm,
    reportError: reportError,
    reporter: reporter,
  });
}

// Production environment adapter. Every global is resolved at call time where
// replacement is meaningful (API, dialogs, feedback, translation), so this
// module can load before the controller graph without capturing stale state.
var _orchServices = createOrchestrationStudioServices({
  document: typeof document !== 'undefined' ? document : null,
  window: typeof window !== 'undefined' ? window : null,
  translate: function (key, params) {
    return typeof t === 'function' ? t(key, params) : key;
  },
  escape: function (value) {
    return typeof escapeHtml === 'function'
      ? escapeHtml(value) : String(value == null ? '' : value);
  },
  richCopy: function (value) {
    if (typeof formatOrchestrationRichCopy === 'function') {
      return formatOrchestrationRichCopy(value);
    }
    return typeof escapeHtml === 'function'
      ? escapeHtml(value) : String(value == null ? '' : value);
  },
  toast: function () {
    return typeof _orchFeedback !== 'undefined' && _orchFeedback
      ? _orchFeedback.toast.apply(null, arguments) : null;
  },
  warn: function () {
    return typeof _orchFeedback !== 'undefined' && _orchFeedback
      ? _orchFeedback.warn.apply(null, arguments) : null;
  },
  choose: function (config) {
    return typeof showChoice === 'function'
      ? showChoice(config) : Promise.resolve('keep');
  },
  confirm: function (message, options, fallback) {
    return typeof showConfirm === 'function'
      ? showConfirm(message, options)
      : Promise.resolve(fallback === undefined ? true : fallback);
  },
  logger: typeof console !== 'undefined' ? console : null,
});

/* ===== migrated source: orchestration-command-bridge.js ===== */
/* Orchestration Studio command bridge.
 *
 * This file intentionally contains only the stable global commands used by
 * the shell, inline-compatible extensions and focused controller callbacks.
 * Controller construction remains in orchestration.js; command behavior
 * stays owned by the injected document/run/workspace/composer ports.
 *
 * Load before orchestration.js: controller composition passes a small number
 * of these functions directly while all referenced controller variables are
 * resolved only when a command is invoked.
 */

function _orchOnRename(v) {
  _orchEditorState.setName(v || 'Untitled Flow');
  _orchMarkDirty('flow-name');
}

// Run Drawer commands — implementation lives in orchestration-run.js.
function _orchStartSeed(definition) {
  return _orchRunOverlay.startSeed(definition);
}

function _orchResetNodeRunStatus() {
  return _orchRunOverlay.reset();
}

function _orchHandleRunStateChange(state, change) {
  return _orchRunOverlay.applyChange(state, change);
}

async function _orchLoadBuiltin(name) {
  return _orchWorkspaceController.loadBuiltin(name, arguments[1] || {});
}

async function _orchTidy(opts) {
  return _orchWorkspaceController.tidy(opts);
}

function _orchToDefinition() {
  return _orchDefinitionSnapshot.currentLevel();
}

function _orchRootDefinitionSnapshot() {
  return _orchDefinitionSnapshot.root();
}

function _orchExport() {
  return _orchExporter.exportCurrent();
}

function _orchToast(text, isErr, opts) {
  return _orchStudioApi.toast(text, isErr, opts);
}
/* ===== migrated source: orchestration-canvas-command-bridge.js ===== */
/* Orchestration Studio canvas command bridge.
 *
 * Stable global commands for palette/mobile actions, graph mutations, nested
 * navigation and edge geometry. Focused controllers own the behavior; this
 * file preserves the extension/shell surface without bloating the composition
 * root. Load before orchestration.js so its controller factories can receive
 * these functions as callbacks.
 */

function _orchRenderPalette() {
  _orchPaletteView.render(document.getElementById('orchPalette'));
}

function _orchIsMobile() {
  return _orchStudio.isMobile();
}

function _orchAddNodeAtCenter(payload) {
  var canvas = document.getElementById('orchCanvas');
  if (!canvas) return;
  var point = _orchCanvasGeometry.centeredNode(canvas, 80);
  _orchAddNode(payload, point.x, point.y);
}
function _orchCloseMobilePalette() {
  return _orchStudio.closeMobilePalette();
}
function _orchCloseMobileInspector() {
  return _orchStudio.closeMobileInspector();
}

function _orchWireCanvas() {
  return _orchCanvasInteraction.wireCanvas();
}

function _orchAddNode(payload, x, y) {
  return _orchGraphActions.addNode(payload, x, y);
}

function _orchBlankGroupDefinition() {
  return _orchAuthoring.blankSubflowDefinition();
}

function _orchDefaultParams(payload) {
  return _orchAuthoring.nodeParams(payload);
}

function _orchNodeHeaderDown(event, id) {
  return _orchCanvasInteraction.nodeHeaderDown(event, id);
}

function _orchPortDown(event, id) {
  return _orchCanvasInteraction.portDown(event, id);
}

function _orchPortUp(event, id) {
  return _orchCanvasInteraction.portUp(event, id);
}

function _orchPortKeyDown(event, id, side) {
  return _orchCanvasInteraction.portKeyDown(event, id, side);
}

function _orchConnectNodes(from, to) {
  return _orchGraphActions.connectNodes(from, to);
}

function _orchFind(id) {
  return _orchGraphActions.findNode(id);
}

function _orchDeleteNode(id) {
  return _orchGraphActions.deleteNode(id);
}

function _orchDeleteEdge(id) {
  return _orchGraphActions.deleteEdge(id);
}

function _orchWorkspaceState() {
  return _orchNavigation.workspaceState();
}

function _orchAdoptWorkspace(workspace) {
  return _orchNavigation.adoptWorkspace(workspace);
}

function _orchEnterGroup(id) {
  return _orchNavigation.enterGroup(id);
}

function _orchPortCenter(id, side) {
  var canvas = document.getElementById('orchCanvas');
  var card = document.getElementById('orch-node-' + id);
  var port = card && card.querySelector('.orch-port-' + side);
  return _orchCanvasGeometry.portCenter(canvas, port);
}

function _orchRenderEdges() {
  return _orchCanvasView ? _orchCanvasView.renderEdges() : null;
}

/* ===== migrated source: orchestration-view-bridge.js ===== */
/* Orchestration Studio canvas/inspector view bridge.
 *
 * Stable render and editor adapters over the focused node, graph, I/O and
 * Inspector ports. No rendering policy lives here. Load before
 * orchestration.js; referenced controller variables are resolved on call.
 */

function _orchRender() {
  return _orchCanvasView ? _orchCanvasView.render() : null;
}
function _orchRenderNodes() {
  return _orchCanvasView ? _orchCanvasView.renderNodes() : null;
}
function _orchAutoLabel(node) { return _orchNodeView.autoLabel(node); }
function _orchNodeLabel(node) {
  return node ? (node.name || _orchAutoLabel(node)) : '';
}
function _orchNodeLabelById(id) {
  var node = _orchEditorState.findNode(id);
  return _orchNodeLabel(node) || id;
}
function _orchKindLabel(node) { return _orchNodeView.kindLabel(node); }
function _orchNodeBlurb(node) { return _orchNodeView.nodeBlurb(node); }
function _orchInspAvatar(node) {
  return _orchNodeView.inspectorAvatar(node);
}
function _orchInspHeader(node) {
  return _orchInspectorContent.header(node);
}
function _orchSec(titleKey, icon, open, inner, hintKey) {
  return _orchInspectorContent.section(
    titleKey, icon, open, inner, hintKey);
}

function _orchSelectNode(id) { return _orchGraphActions.selectNode(id); }
function _orchNodeKeyDown(event, id) {
  return _orchGraphActions.nodeKeyDown(event, id);
}
function _orchSelectEdge(id) { return _orchGraphActions.selectEdge(id); }
function _orchReverseEdge(id) { return _orchGraphActions.reverseEdge(id); }

function _orchBindEdgeInput(targetId, index, reference) {
  return _orchIoEditor.bindInput(targetId, index, reference);
}
function _orchNodeInputs(node) { return _orchIoTools.nodeInputs(node); }
function _orchNodeOutputs(node) { return _orchIoTools.nodeOutputs(node); }
function _orchIoSectionBody(node) { return _orchIoEditor.sectionBody(node); }

function _orchRenderInspector() {
  return _orchCanvasView ? _orchCanvasView.renderInspector() : null;
}
function _orchLabelField(node) {
  return _orchInspectorFields.labelField(node, _orchAutoLabel(node));
}
function _orchSelectFld(label, key, value, options) {
  return _orchInspectorFields.selectField(label, key, value, options);
}
function _orchSetParam(key, value, isNumber, kind, nodeId, coalesce) {
  return _orchNodeEditor.setParam(
    nodeId, key, value,
    kind || (isNumber ? 'int'
      : (typeof value === 'boolean' ? 'bool' : 'text')),
    coalesce
  );
}
function _orchSetParamResult(nodeId, key, value, kind, coalesce) {
  return _orchNodeEditor.setParamResult(
    nodeId, key, value,
    kind || (typeof value === 'boolean' ? 'bool' : 'text'), coalesce
  );
}

function _orchDefaultEmits(role) {
  return _orchAuthoring.defaultEmits(role);
}
function _orchRoleTaskSectionBody(node) {
  return _orchInspectorFields.roleTaskSection(
    node, null, _orchAuthoring.roleFields(node.role),
    _orchNodeCatalogue.runtimeParam);
}
function _orchRolePersona(role) { return _orchAuthoring.persona(role); }
function _orchRunTraceBody(node) {
  return _orchInspectorContent.runTraceBody(node);
}
function _orchPersonaSectionBody(node) {
  return _orchInspectorContent.personaSectionBody(node);
}
function _orchFlowSummaryBody(node) {
  return _orchInspectorContent.flowSummaryBody(node);
}
function _orchFetchAuthoringContract() {
  return _orchStudioApi.refreshAuthoringContract();
}

/* ===== migrated source: orchestration-lifecycle-bridge.js ===== */
/* Orchestration Studio lifecycle/document/history bridge.
 *
 * Stable globals used by the feature loader, shell and compatible extensions.
 * Controller construction stays in orchestration.js; this bridge only forwards
 * commands to the composed Studio API, document and history ports. Load before
 * orchestration.js so every referenced controller is available when invoked.
 */

function openOrchestration() {
  /* Studio is an unfinished product surface. Hiding its launch controls is
   * not enough: compatibility callers and cached action markup must fail
   * closed under the same deployment flag until the product is released. */
  if (typeof _featureFlags === 'undefined'
      || _featureFlags.debug_mode !== true) return false;
  return _orchStudioApi.open();
}

async function closeOrchestration(evt, force) {
  return _orchStudioApi.close(evt, force);
}

function _orchRenderDocState() {
  _orchDocument.render();
}

function _orchMarkDirty(historyGroup) {
  return _orchEditLifecycle.markDirty(historyGroup);
}

function _orchRenderHistoryState(state) {
  state = state || _orchEditLifecycle.historyState();
  var undo = document.getElementById('orchUndoBtn');
  var redo = document.getElementById('orchRedoBtn');
  if (undo) undo.disabled = !state.canUndo;
  if (redo) redo.disabled = !state.canRedo;
}

function _orchUndo() {
  return _orchEditLifecycle.undo();
}

function _orchRedo() {
  return _orchEditLifecycle.redo();
}

function _orchConfirmReplace() {
  return _orchDocument.confirmReplace();
}
/* ===== migrated source: orchestration.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration.js — Orchestration Studio (frontend authoring canvas)
   A visual, drag-and-drop builder where users compose "orchestration
   definitions" — iterative loops, fan-out/synthesize flows, etc. —
   by wiring together ROLE agents (tofu mascots) and CONTROL nodes
   (start / loop / parallel / barrier / route / stop).
   ── Scope (this phase) ──────────────────────────────────────────────
   This is the AUTHORING layer only.  It produces a declarative
   definition object (see _orchToDefinition) that a backend engine will
   later interpret.  Per CLAUDE.md §3.2.0 the frontend stays a thin
   renderer/editor: it emits JSON, it does NOT run orchestration logic.
   Definitions are persisted through `/api/v1/orchestrations`; validation,
   built-in graphs, role schemas, layout and execution semantics are all
   owned by the backend service boundary.
   Catalogues, templates, defaults and layout are backend-owned or external
   assets; this composition module coordinates canvas state and Inspector
   controllers. Stable shell, Canvas and presentation adapters live in the
   ordered orchestration-*-bridge.js siblings; palette, node cards,
   document/run/event lifecycle and schema/I/O rendering have focused owners.
   ═══════════════════════════════════════════════════════════════════ */
// ── Editor state ────────────────────────────────────────────────────
// The state controller is authoritative. Accessor-backed legacy globals keep
// old extensions and diagnostic harnesses on that same state, never a copy.
// Typed orchestration owners intentionally consume this finite compatibility
// port while the retained controller composition is migrated. ESM module
// bindings do not become properties of ``window`` (classic ``var`` did), so
// publish the declared dependencies explicitly before constructing any typed
// controller. Keep this list aligned with the *Window contracts in
// frontend/src/features/orchestration.
Object.assign(orchestrationRegistry, {
  ORCHESTRATION_AUTHORING_VALIDATION_METADATA,
  ORCHESTRATION_REQUEST_LIMIT_FIELDS,
  ORCHESTRATION_RUNTIME_CONTRACT_SECTIONS,
  _ORCH_CONTROLS,
  _ORCH_GLYPHS,
  _ORCH_ICONS,
  _ORCH_ROLES,
  _orchIconSrc,
  _validateNodeRuntimeDefaultsAuthoringSection,
  createOrchestrationBreadcrumbView,
  createOrchestrationGraphActionContext,
  createOrchestrationGraphMutationActions,
  createOrchestrationGraphTopology,
  createOrchestrationGraphWorkspace,
  createOrchestrationNavigationController,
  escapeHtml,
  orchestrationConnections,
  orchestrationNodePosition,
  projectOrchestrationLayoutPositions,
  showConfirm,
  showToast,
  t,
});
var _orchEditorState = createOrchestrationEditorState();
_orchEditorState.installLegacyGlobals(window);
var _orchCanvasInteraction = null;  // transient drag/connect controller
var _orchCanvasView = null;   // ordered canvas DOM composition
var _orchSession = null;      // active definition id/version + adoption policy
var _orchEditLifecycle = null; // document/history/save-checkpoint boundary
var _orchIssueNavigator = null; // backend diagnostic list + field navigation

// The frozen, late-bound browser/API/presentation service port is created by
// orchestration-studio-services.js before this controller graph is composed.
var _orchRequestLimits = createOrchestrationRequestLimits({
  source: function () {
    return _orchAuthoring ? _orchAuthoring.requestLimits() : {};
  },
});
var _orchRuntimeContracts = createOrchestrationRuntimeContractPort({
  source: function () {
    return _orchAuthoring ? _orchAuthoring.snapshot() : {};
  },
});
var _orchFeedback = createOrchestrationFeedback({
  document: _orchServices.document,
  translate: _orchServices.translate,
  issueMessages: orchestrationIssueMessages,
});
var _orchExporter = createOrchestrationExportController({
  document: _orchServices.document,
  snapshot: function () { return _orchRootDefinitionSnapshot(); },
  translate: _orchServices.translate,
  toast: _orchServices.toast,
  onError: _orchServices.reporter('OrchestrationExport'),
});
var _orchPopupMenus = createOrchestrationPopupMenuController({
  document: _orchServices.document,
});
var _orchWriteRecovery = createOrchestrationWriteRecoveryController({
  currentId: function () {
    return _orchSession ? _orchSession.currentId() : null;
  },
  isCurrent: function (conflict) {
    var current = _orchDocument && _orchDocument.state.writeConflict;
    return !!current
      && current.expectedUpdatedAt === conflict.expectedUpdatedAt
      && current.currentUpdatedAt === conflict.currentUpdatedAt;
  },
  choose: _orchServices.choose,
  exportDraft: function () { return _orchExport(); },
  loadLatest: function (id) {
    return _orchWorkspaceController
      ? _orchWorkspaceController.loadFromStore(id, { skipConfirm: true })
      : Promise.resolve(null);
  },
  translate: _orchServices.translate,
});
// Document lifecycle lives in orchestration-document.js. The canvas supplies
// only adapters, so dirty/validation/save semantics can be tested and evolved
// without loading or reaching into the graph editor itself.
var _orchDocument = createOrchestrationDocumentController({
  document: _orchServices.document,
  normalizeInspection: normalizeOrchestrationInspection,
  normalizeValidationRead: normalizeOrchestrationValidationRead,
  api: _orchServices.api, inspectionContract: function () { return _orchAuthoring ? _orchAuthoring.inspectionContract() : null; },
  snapshot: function () { return _orchRootDefinitionSnapshot(); },
  nodeCount: _orchEditorState.nodeCount,
  translate: _orchServices.translate,
  toast: _orchServices.toast,
  warn: _orchServices.warn,
  showIssues: function (state) {
    return _orchIssueNavigator ? _orchIssueNavigator.show(state) : null;
  },
  syncIssues: function (state) {
    if (_orchIssueNavigator) _orchIssueNavigator.sync(state);
  },
  onInspectionChange: function () {
    if (_orchDiagnosticIndex) _orchDiagnosticIndex.invalidate();
    if (_orchCanvasView) {
      _orchCanvasView.renderNodes();
      _orchCanvasView.renderEdges();
    }
  },
  confirm: _orchServices.confirm,
  onWriteConflict: function (conflict) {
    return _orchWriteRecovery.open(conflict);
  },
  onError: _orchServices.reporter('OrchestrationDocument'),
});
// Compatibility alias for older extensions/tests that only read lifecycle
// state. All mutations in shipped code go through _orchDocument methods.
var _orchDocState = _orchDocument.state;
// Run Drawer lifecycle lives in orchestration-run.js. The editor supplies
// graph snapshots and small canvas callbacks; transport/polling/gates remain
// isolated from authoring state.
var _orchTaskModeHandoff = createOrchestrationSurfaceHandoff({
  closeSource: function () { return closeOrchestration(null, true); },
  openTarget: function (runId) {
    return typeof openTaskMode === 'function' ? openTaskMode(runId) : false;
  },
  closeTarget: function () {
    return typeof closeTaskMode === 'function' ? closeTaskMode() : false;
  },
  reopenSource: function () { return openOrchestration(); },
  report: _orchServices.reporter('OrchestrationSurfaceHandoff'),
});
var _orchRunOverlay = createOrchestrationRunOverlay({
  document: _orchServices.document,
  definition: function () { return _orchRootDefinitionSnapshot(); },
  selectedNodeId: _orchEditorState.selectedNodeId,
  renderInspector: function () { _orchRenderInspector(); },
});
function _orchSyncMobileSurfaceState() { if (_orchStudio) _orchStudio.syncMobileSheets(); }
function _orchHandleRunSurfaceState() { if (_orchPanelLayout) _orchPanelLayout.setRunDrawerOpen(); _orchSyncMobileSurfaceState(); }
var _orchRunController = createOrchestrationRunController({
  document: _orchServices.document,
  api: _orchServices.api,
  definition: function () { return _orchRootDefinitionSnapshot(); },
  currentId: function () {
    return _orchSession ? (_orchSession.currentId() || '') : '';
  },
  requireValid: function (action) { return _orchDocument.requireValid(action); },
  startSeed: function (definition) { return _orchStartSeed(definition); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  limitPolicy: _orchRequestLimits,
  contractPort: _orchRuntimeContracts,
  icon: function (name) { return _ORCH_ICONS[name] || ''; },
  toast: _orchServices.toast,
  onError: _orchServices.reporter('OrchestrationRun'),
  handoffTaskMode: _orchTaskModeHandoff.transfer,
  onResetTrace: _orchResetNodeRunStatus,
  onStateChange: _orchHandleRunStateChange,
  onSurfaceChange: _orchHandleRunSurfaceState,
});
var _orchInspectorFields = createOrchestrationInspectorRenderer({
  escape: _orchServices.escape,
  translate: _orchServices.translate,
});
var _ORCH_CARD_W = 188;       // must match .orch-node width in CSS
var _orchViewport = null;
var _orchCanvasGeometry = createOrchestrationCanvasGeometry({
  cardWidth: _ORCH_CARD_W,
  viewport: function () { return _orchViewport ? _orchViewport.transform() : null; },
});
_orchViewport = createOrchestrationViewportController({
  document: _orchServices.document,
  nodes: _orchEditorState.nodes,
  cardWidth: _ORCH_CARD_W,
  fitMinScale: function () { return orchestrationFitMinScale(_orchServices.window); },
  onChange: function () { _orchRenderEdges(); },
});
var _orchWorkSurfaces = createOrchestrationWorkSurfaceController({surfaces: {
  composer: function () { return _orchComposer; }, run: function () { return _orchRunController; },
}, admitOpen: function () {
  return !_orchStudio || _orchStudio.releaseMobileSheet();
}});
var _orchPanelLayout = createOrchestrationPanelLayoutController({
  document: _orchServices.document,
  window: _orchServices.window,
  translate: _orchServices.translate,
  onChange: function () {
    if (_orchPanelResize) _orchPanelResize.sync();
    _orchViewport.sync();
    _orchRenderEdges();
  },
  workSurfaces: _orchWorkSurfaces,
});
var _orchPanelResize = createOrchestrationPanelResizeController({
  document: _orchServices.document,
  window: _orchServices.window,
  isExpanded: function (name) {
    return name === 'palette'
      ? _orchPanelLayout.paletteExpanded()
      : _orchPanelLayout.inspectorExpanded();
  },
  onChange: function () { _orchViewport.sync(); _orchRenderEdges(); },
});
var _orchDiagnosticIndex = createOrchestrationDiagnosticIndex({
  diagnostics: function () { return _orchDocument.state.diagnostics; },
  definition: function () { return _orchRootDefinitionSnapshot(); },
  workspaceGroups: function () {
    return _orchEditorState.stack().map(function (frame) {
      return frame && frame.groupId || '';
    }).filter(Boolean);
  },
});
var _orchEdgeView = createOrchestrationEdgeView({
  geometry: _orchCanvasGeometry,
  edges: _orchEditorState.edges,
  selectedEdgeId: _orchEditorState.selectedEdgeId,
  connection: function () {
    return _orchCanvasInteraction ? _orchCanvasInteraction.connection() : null;
  },
  portCenter: function (id, side) { return _orchPortCenter(id, side); }, nodeLabel: _orchNodeLabelById,
  issueSummary: function (_edge, index) {
    return index >= 0 ? _orchDiagnosticIndex.edge(index) : null;
  },
  onSelect: function (id) { _orchSelectEdge(id); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
});
var _orchGraph = createOrchestrationGraphTools();
var _orchDefinitionSnapshot = createOrchestrationDefinitionSnapshotPort({
  graph: _orchGraph,
  workspace: _orchEditorState.workspace,
  stack: _orchEditorState.stack,
});
var _orchEditorControllers = createOrchestrationEditorControllerHub({
  document: _orchServices.document,
  graph: _orchGraph,
  editorState: _orchEditorState,
  controls: function () { return _ORCH_CONTROLS; },
  limitPolicy: _orchRequestLimits,
  defaultParams: function (payload) { return _orchDefaultParams(payload); },
  isDragging: function () {
    return !!(_orchCanvasInteraction && _orchCanvasInteraction.isDragging());
  },
  markDirty: function () { _orchMarkDirty(); },
  render: function () { _orchRender(); },
  renderNodes: function () { _orchRenderNodes(); },
  renderEdges: function () { _orchRenderEdges(); },
  renderInspector: function () { _orchRenderInspector(); },
  translate: _orchServices.translate,
  toast: _orchServices.toast,
  blankGroupDefinition: function () { return _orchBlankGroupDefinition(); },
  fallbackName: function () { return t('orch.group.defaultLabel'); },
  nodeLabel: _orchNodeLabel,
  onNavigate: function () {
    if (_orchEditLifecycle) _orchEditLifecycle.syncHistory();
    if (_orchViewport) _orchViewport.fit();
  },
  tidy: function (opts) { return _orchTidy(opts); },
});
var _orchSelectionFocus = _orchEditorControllers.selectionFocus;
var _orchGraphActions = _orchEditorControllers.graphActions;
var _orchBreadcrumb = _orchEditorControllers.breadcrumb;
var _orchNavigation = _orchEditorControllers.navigation;
_orchIssueNavigator = createOrchestrationIssueNavigator({
  document: _orchServices.document,
  definition: function () { return _orchRootDefinitionSnapshot(); },
  navigateGroups: function (groupIds) {
    return _orchNavigation.navigateToGroups(groupIds);
  },
  selectNode: function (id) { return _orchGraphActions.selectNode(id); },
  selectEdgeAt: function (index) {
    var edge = _orchEditorState.edges()[index];
    return edge ? _orchGraphActions.selectEdge(edge.id) : false;
  },
  focusSelection: _orchSelectionFocus.focus,
  focusDiagnostic: function (target, diagnostic, scrollBehavior, descriptionId) {
    return _orchInspectorView && _orchInspectorView.focusDiagnostic(
      target, diagnostic, scrollBehavior, descriptionId);
  },
  showInspector: function () { return _orchPanelLayout.showInspector(); },
  translate: _orchServices.translate,
});
var _orchHistory = createOrchestrationHistoryController({
  limit: 100,
  coalesceWindow: 700,
  capture: function () {
    return {
      workspace: _orchWorkspaceState(),
      stack: _orchEditorState.stack(),
    };
  },
  fingerprint: function (snapshot) {
    snapshot = snapshot || {};
    var workspace = snapshot.workspace || {
      name: 'Untitled Flow', nodes: [], edges: [],
    };
    return _orchGraph.rootSnapshot(
      workspace.name, workspace.nodes || [], workspace.edges || [],
      snapshot.stack || []
    );
  },
  apply: function (snapshot) {
    if (!snapshot || !snapshot.workspace) return false;
    _orchEditorState.setStack(snapshot.stack);
    _orchAdoptWorkspace(snapshot.workspace);
    _orchEditorState.setSelectedEdgeId(null);
    _orchEditLifecycle.restoreHistory();
    _orchRender();
    return true;
  },
  onChange: function (state) { _orchRenderHistoryState(state); },
});
_orchEditLifecycle = createOrchestrationEditLifecycle({
  documentLifecycle: _orchDocument,
  history: _orchHistory,
  scope: function () {
    return _orchSession ? _orchSession.documentToken() : null;
  },
});
_orchEditLifecycle.resetHistory({ persisted: false });
_orchSession = createOrchestrationSessionController({
  lifecycle: _orchEditLifecycle,
  resetStack: function () { _orchEditorState.setStack([]); },
  workspaceFromDefinition: function (definition) {
    return _orchGraph.workspaceFromDefinition(definition, 'Untitled Flow');
  },
  workspaceFromDefinitionResult: function (definition) {
    return _orchGraph.workspaceFromDefinitionResult(
      definition, 'Untitled Flow');
  },
  adoptWorkspace: function (workspace) { _orchAdoptWorkspace(workspace); },
  render: function () { _orchRender(); },
  fitView: function () { _orchViewport.fit(); },
  tidy: function (opts) { return _orchTidy(opts); },
  nodeCount: _orchEditorState.nodeCount,
});
_orchCanvasInteraction = createOrchestrationCanvasInteractionController({
  document: _orchServices.document,
  window: _orchServices.window,
  geometry: _orchCanvasGeometry,
  findNode: function (id) { return _orchFind(id); },
  addNode: function (payload, x, y) { _orchAddNode(payload, x, y); },
  connectNodes: function (from, to) { _orchConnectNodes(from, to); },
  portCenter: function (id, side) { return _orchPortCenter(id, side); },
  selectForDrag: function (id) { _orchGraphActions.selectNodeForDrag(id); },
  deselect: function () { _orchGraphActions.clearSelection(); },
  markDirty: function () { _orchMarkDirty(); },
  syncViewport: function () { _orchViewport.sync(); },
  render: function () { _orchRender(); },
  renderNodes: function () { _orchRenderNodes(); },
  renderEdges: function () { _orchRenderEdges(); },
  renderInspector: function () { _orchRenderInspector(); },
});
var _orchIoTools = createOrchestrationIoTools();
var _orchFieldValidity = createOrchestrationFieldValidity();
var _orchAuthoring = createOrchestrationAuthoringContractController({
  roles: _ORCH_ROLES,
  controls: _ORCH_CONTROLS,
  ioTools: _orchIoTools,
  api: _orchServices.api,
  translate: _orchServices.translate,
  onChange: function (contract) {
    _ORCH_ROLES = contract.roles; _ORCH_CONTROLS = contract.controls;
    _orchRequestLimits.applyStudio(document);
    if (_orchStudio && _orchStudio.isReady()) {
      _orchRenderPalette();
      _orchRenderNodes();
      if (_orchEditorState.selectedNodeId()) _orchRenderInspector();
    }
  },
  onError: function (error) {
    _orchServices.reportError(
      'OrchestrationAuthoring', 'contract fetch', error);
    _orchToast(t('orch.contract.loadFailed'), true);
  },
});
var _orchIoEditor = createOrchestrationIoEditor({
  ioTools: _orchIoTools,
  fieldValidity: _orchFieldValidity,
  nodes: _orchEditorState.nodes,
  edges: _orchEditorState.edges,
  selectedNode: function () {
    return _orchEditorState.findNode(_orchEditorState.selectedNodeId());
  },
  findNode: _orchEditorState.findNode,
  nodeLabel: _orchNodeLabel,
  escape: _orchServices.escape,
  translate: _orchServices.translate,
  icons: _ORCH_ICONS,
  toast: _orchServices.toast,
  onChange: function (change) {
    _orchMarkDirty(change.historyGroup || '');
    if (change.renderInspector) _orchRenderInspector();
    if (change.renderNodes) _orchRenderNodes();
  },
});
var _orchPaletteView = createOrchestrationPaletteView({
  roles: function () { return _ORCH_ROLES; },
  controls: function () { return _ORCH_CONTROLS; },
  contractState: function () { return _orchAuthoring.snapshot(); },
  icons: _ORCH_ICONS,
  glyphs: _ORCH_GLYPHS,
  iconSrc: function (icon) { return _orchIconSrc(icon); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  onAdd: function (payload) { _orchAddNodeAtCenter(payload); },
  onRetry: function () { return _orchFetchAuthoringContract(); },
  isMobile: function () { return _orchIsMobile(); },
  closeMobile: function () { _orchCloseMobilePalette(); },
});
var _orchNodeCatalogue = createOrchestrationNodeCatalogue({
  roles: function () { return _ORCH_ROLES; },
  controls: function () { return _ORCH_CONTROLS; },
  nodeDefaults: function () { return _orchAuthoring.snapshot().nodeDefaults; },
  nodeRuntimeDefaults: function () {
    return _orchAuthoring.snapshot().nodeRuntimeDefaults;
  },
});
var _orchNodeView = createOrchestrationNodeView({
  document: _orchServices.document,
  nodes: _orchEditorState.nodes,
  edges: _orchEditorState.edges,
  selectedId: _orchEditorState.selectedNodeId,
  connectingFrom: function () {
    var connection = _orchCanvasInteraction.connection();
    return connection && connection.from;
  },
  catalogue: _orchNodeCatalogue,
  icons: _ORCH_ICONS,
  glyphs: _ORCH_GLYPHS,
  iconSrc: function (icon) { return _orchIconSrc(icon); },
  defaultEmits: function (role) { return _orchDefaultEmits(role); },
  issueSummary: function (id) { return _orchDiagnosticIndex.node(id); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  onSelect: function (id) { _orchSelectNode(id); },
  onNodeKeyDown: function (event, id) { _orchNodeKeyDown(event, id); },
  onHeaderPointerDown: function (event, id) { _orchNodeHeaderDown(event, id); },
  onPortDown: function (event, id) { _orchPortDown(event, id); },
  onPortUp: function (event, id) { _orchPortUp(event, id); },
  onPortKeyDown: function (event, id, side) { _orchPortKeyDown(event, id, side); },
  onEnterGroup: function (id) { _orchEnterGroup(id); },
  onDelete: function (id) { _orchDeleteNode(id); },
});
var _orchNodeEditor = createOrchestrationNodeEditor({
  findNode: _orchEditorState.findNode,
  selectedNodeId: _orchEditorState.selectedNodeId,
  fieldValueContract: _orchAuthoring.fieldValueContract,
  fieldSpec: function (node, key) {
    return _orchAuthoring.fieldSpec(
      node.type, node.role || node.kind || '', key
    );
  },
  markDirty: function (historyGroup) { _orchMarkDirty(historyGroup); },
  renderNodes: function () { _orchRenderNodes(); },
  renderInspector: function () { _orchRenderInspector(); },
});
var _orchInspectorContent = createOrchestrationInspectorContent({
  edges: _orchEditorState.edges,
  findNode: _orchEditorState.findNode,
  nodeLabel: _orchNodeLabel,
  kindLabel: function (node) { return _orchKindLabel(node); },
  blurb: function (node) { return _orchNodeBlurb(node); },
  avatar: function (node) { return _orchInspAvatar(node); },
  traceSnapshotFor: function (id) {
    return _orchRunController.traceSnapshotFor(id);
  },
  persona: function (role) { return _orchRolePersona(role); }, traceContract: _orchAuthoring.traceContract,
  translate: _orchServices.translate,
  escape: _orchServices.escape, richCopy: _orchServices.richCopy,
});
var _orchComposerView = createOrchestrationComposerView({
  document: _orchServices.document, translate: _orchServices.translate, richCopy: _orchServices.richCopy,
  icons: _ORCH_ICONS,
  onVisibilityChange: _orchSyncMobileSurfaceState,
});
var _orchComposer = createOrchestrationComposerController({
  view: _orchComposerView,
  normalizeInspection: normalizeOrchestrationInspection,
  normalizeComposeResult: normalizeOrchestrationComposeResult, inspectionContract: _orchAuthoring.inspectionContract,
  api: _orchServices.api,
  limitPolicy: _orchRequestLimits,
  revision: function () { return _orchDocument.revision(); },
  currentDefinition: function () {
    return _orchEditorState.hasNodes() ? _orchRootDefinitionSnapshot() : null;
  },
  currentId: function () { return _orchSession.currentId(); },
  applyDefinition: function (definition, id, opts) {
    return _orchSession.applyDefinition(definition, id, opts);
  },
  applyDefinitionResult: function (definition, id, opts) {
    return _orchSession.applyDefinitionResult(definition, id, opts);
  },
  translate: _orchServices.translate,
  toast: _orchServices.toast,
  warn: _orchServices.warn,
  onError: _orchServices.reporter('OrchestrationComposer', 'request'),
});
var _orchWorkspaceController = createOrchestrationWorkspaceController({
  document: _orchServices.document,
  normalizeInspection: normalizeOrchestrationInspection,
  normalizeBuiltin: normalizeOrchestrationBuiltinRead,
  normalizeLayout: normalizeOrchestrationLayoutRead,
  normalizeList: normalizeOrchestrationDefinitionListRead,
  normalizeRead: normalizeOrchestrationDefinitionRead,
  normalizeSave: normalizeOrchestrationDefinitionSave,
  normalizeDelete: normalizeOrchestrationDefinitionDelete,
  definitionWriteContract: _orchAuthoring.definitionWriteContract, definitionListContract: _orchAuthoring.definitionListContract, definitionEntryContract: _orchAuthoring.definitionEntryContract, inspectionContract: _orchAuthoring.inspectionContract,
  popupMenus: _orchPopupMenus,
  api: _orchServices.api,
  lifecycle: _orchEditLifecycle,
  session: _orchSession,
  currentName: _orchEditorState.name,
  nodeCount: _orchEditorState.nodeCount,
  currentLevelDefinition: function () { return _orchToDefinition(); },
  workspaceToken: _orchEditorState.workspaceToken,
  rootDefinition: function () { return _orchRootDefinitionSnapshot(); },
  blankDefinition: function () {
    return _orchGraph.definitionFromState('Untitled Flow', [], []);
  },
  applyPositions: _orchEditorState.applyPositions,
  fitView: function () { _orchViewport.fit(); },
  render: function () { _orchRender(); },
  confirmReplace: function () { return _orchConfirmReplace(); },
  confirmDelete: function () {
    return _orchServices.confirm(
      t('orch.store.deleteConfirm'), { danger: true }, false);
  },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  icons: _ORCH_ICONS,
  toast: _orchServices.toast,
  warn: _orchServices.warn,
  onDefinitionsChanged: function () {
    if (typeof _orchestrationFlowCatalog !== 'undefined'
        && _orchestrationFlowCatalog) {
      _orchestrationFlowCatalog.invalidate();
      _orchestrationFlowCatalog.refresh();
    }
  },
  onUseDefinition: async function (id) {
    if (!(typeof _featureFlags !== 'undefined'
        && _featureFlags.debug_mode === true)) return false;
    if (typeof setActiveFlow !== 'function'
        || typeof _agentInteractionChangeBlocked === 'function'
          && _agentInteractionChangeBlocked()) return false;
    var closed = await _orchStudio.close(null, true);
    if (!closed) return false;
    if (!setActiveFlow(String(id || ''))) {
      _orchStudio.open({ skipInitial: true });
      return false;
    }
    var composer = _orchServices.document
      && _orchServices.document.getElementById('userInput');
    if (composer && typeof composer.focus === 'function') composer.focus();
    return true;
  },
  onError: _orchServices.reporter('OrchestrationWorkspace'),
});
var _orchInspectorView = createOrchestrationInspectorView({
  document: _orchServices.document,
  fieldValidity: _orchFieldValidity,
  nodes: _orchEditorState.nodes,
  edges: _orchEditorState.edges,
  selectedNodeId: _orchEditorState.selectedNodeId,
  selectedEdgeId: _orchEditorState.selectedEdgeId,
  workspaceToken: _orchEditorState.workspaceToken,
  clearSelectedEdge: function () { _orchGraphActions.clearSelectedEdge(); },
  findNode: function (id) { return _orchFind(id); },
  roles: function () { return _ORCH_ROLES; },
  executionOptions: function () { return _orchAuthoring.executionOptions(); },
  controlFields: function (kind) { return _orchAuthoring.controlFields(kind); },
  nodeParam: _orchNodeCatalogue.runtimeParam,
  autoLabel: function (node) { return _orchAutoLabel(node); }, nodeLabel: _orchNodeLabelById,
  kindLabel: function (node) { return _orchKindLabel(node); },
  header: function (node) { return _orchInspHeader(node); },
  section: function (key, icon, open, inner, hint) {
    return _orchSec(key, icon, open, inner, hint);
  },
  labelField: function (node) { return _orchLabelField(node); },
  selectField: function (label, key, value, choices) {
    return _orchSelectFld(label, key, value, choices);
  },
  controlSchemaSection: function (node, fields) {
    return _orchInspectorFields.schemaSection(
      node, fields, _orchNodeCatalogue.runtimeParam);
  },
  roleTaskBody: function (node) { return _orchRoleTaskSectionBody(node); },
  runTraceBody: function (node) { return _orchRunTraceBody(node); },
  personaBody: function (node) { return _orchPersonaSectionBody(node); },
  flowSummaryBody: function (node) { return _orchFlowSummaryBody(node); },
  ioSectionBody: function (node) { return _orchIoSectionBody(node); },
  defaultEmits: function (role) { return _orchDefaultEmits(role); },
  nodeInputs: function (node) { return _orchNodeInputs(node); },
  nodeOutputs: function (node) { return _orchNodeOutputs(node); },
  outputRef: function (nodeId, outputs, output) {
    return _orchIoTools.outputRef(nodeId, outputs, output);
  },
  setParam: function (nodeId, key, value, kind, coalesce) {
    return _orchSetParam(key, value, false, kind, nodeId, coalesce);
  },
  setParamResult: _orchSetParamResult,
  bindIoSection: function (element, nodeId) {
    _orchIoEditor.bindSection(element, nodeId);
  },
  bindEdgeInput: function (targetId, index, ref) {
    _orchBindEdgeInput(targetId, index, ref);
  },
  reverseEdge: function (id) { _orchReverseEdge(id); },
  deleteEdge: function (id) { _orchDeleteEdge(id); },
  enterGroup: function (id) { _orchEnterGroup(id); },
  deleteNode: function (id) { _orchDeleteNode(id); },
  isMobile: function () { return _orchIsMobile(); },
  setMobileOpen: function (open) {
    return _orchStudio.setMobileInspectorOpen(open);
  },
  closeMobile: function () { _orchCloseMobileInspector(); },
  translate: _orchServices.translate,
  escape: _orchServices.escape,
  icons: _ORCH_ICONS,
});
_orchCanvasView = createOrchestrationCanvasView({
  document: _orchServices.document,
  name: _orchEditorState.name,
  nodeCount: _orchEditorState.nodeCount,
  nodeView: _orchNodeView,
  edgeView: _orchEdgeView,
  inspectorView: _orchInspectorView,
  navigation: _orchNavigation,
  viewport: _orchViewport,
  translate: _orchServices.translate, richCopy: _orchServices.richCopy,
  icons: _ORCH_ICONS,
});
var _orchStudio = createOrchestrationStudioController({
  document: _orchServices.document,
  window: _orchServices.window,
  workSurfaces: _orchWorkSurfaces,
  createShell: function (options) {
    return createOrchestrationStudioShell(options);
  },
  shellOptions: function () {
    return {
      document: _orchServices.document,
      popupMenus: _orchPopupMenus,
      icons: _ORCH_ICONS,
      logoUrl: _orchIconBase() + '/tofu-planner.svg',
      translate: _orchServices.translate,
      escape: _orchServices.escape, richCopy: _orchServices.richCopy, limitPolicy: _orchRequestLimits,
      onBackdrop: function (event) { _orchStudio.close(event); },
      commands: createOrchestrationStudioShellCommands({
        studio: _orchStudio,
        document: _orchDocument,
        workspace: _orchWorkspaceController,
        history: _orchHistory,
        viewport: _orchViewport,
        panels: _orchPanelLayout,
        composer: _orchComposer,
        run: _orchRunController,
        exporter: _orchExporter,
        rename: _orchOnRename,
      }),
    };
  },
  onMount: function () {
    _orchPanelResize.bind();
    _orchRenderPalette();
    _orchWireCanvas();
    _orchViewport.wire();
    _orchRenderDocState();
    _orchRenderHistoryState();
  },
  installUnloadGuard: function (target) {
    _orchDocument.installUnloadGuard(target);
  },
  hasNodes: _orchEditorState.hasNodes,
  loadInitial: function () { _orchLoadBuiltin('blank', { initial: true }); },
  render: function () { _orchRender(); },
  refreshContract: function () { _orchFetchAuthoringContract(); },
  syncDesktopPanels: function () { return _orchPanelLayout.sync(); },
  confirmDiscard: function () { return _orchDocument.confirmDiscard(
    'orch.doc.closeConfirm'); },
  cancelGesture: function () { return _orchCanvasInteraction.cancelGesture(); },
  closePopups: function () {
    var issueClosed = !!(_orchIssueNavigator
      && _orchIssueNavigator.close(true));
    var menusClosed = _orchPopupMenus.closeAll();
    return issueClosed || menusClosed;
  },
  dismissTransient: function () { return _orchPanelLayout.dismissTransient(); },
  selectedEdgeId: _orchEditorState.selectedEdgeId,
  selectedNodeId: _orchEditorState.selectedNodeId,
  save: function () { return _orchWorkspaceController.save(); },
  undo: function () { _orchUndo(); },
  redo: function () { _orchRedo(); },
  zoomIn: function () { _orchViewport.zoomIn(); },
  zoomOut: function () { _orchViewport.zoomOut(); },
  zoomReset: function () { _orchViewport.reset(); },
  deleteEdge: function (id) { _orchDeleteEdge(id); },
  deleteNode: function (id) { _orchDeleteNode(id); },
});
var _orchStudioApi = createOrchestrationStudioApi({
  open: function (options) { return _orchStudio.open(options); },
  close: function (event, force) { return _orchStudio.close(event, force); },
  refreshAuthoringContract: function () {
    return _orchAuthoring.load().then(function (contract) {
      // Render both success and unavailable states; only a settled contract
      // or explicit legacy decision releases the palette gate.
      if (_orchStudio.isReady() && !contract.ready) _orchRenderPalette();
      return contract;
    });
  },
  loadDefinition: function (id) {
    return _orchWorkspaceController.loadFromStore(id);
  },
  toast: _orchServices.toast,
});
runtimeScope._orchServices = _orchServices;
runtimeScope._orchStudioApi = _orchStudioApi;

// BEGIN GENERATED LAZY RUNTIME PORTS — orchestration-presenters
runtimeScope.closeOrchestration = closeOrchestration;
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — orchestration-presenters
runtimeScope.openOrchestration = openOrchestration;
runtimeScope.openTaskMode = orchestrationRegistry.openTaskMode;
// END GENERATED LAZY RUNTIME ACTIONS
