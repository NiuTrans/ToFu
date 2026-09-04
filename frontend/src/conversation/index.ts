/** Public composition boundary for the typed browser conversation feature. */
export {
  bindConversationSession,
  type BindConversationSessionOptions,
  type ConversationRenderScheduler,
  type ConversationSessionBinding,
} from './application/conversation-session';
export {
  createTurnState,
  createTurnStore,
  reduceTurnState,
  type TurnAction,
  type TurnState,
  type TurnStore,
} from './domain/turn-store';
export {
  selectConversationViewModel,
  selectTurnBlocks,
  type ConversationBlockViewModel,
  type ConversationLaneViewModel,
  type ConversationPresentationState,
  type TranslationActivity,
  type ConversationTurnViewModel,
  type ConversationViewModel,
  type TranslationDisplayMode,
} from './presentation/conversation-view-model';
export {
  presentTurnFinish,
  resumeTurnOptions,
} from './presentation/turn-finish';
export {
  computeExecutionBatches,
  computeToolBatches,
  countToolTurns,
  presentToolExecutionPanel,
  siblingTitleDiscriminators,
  shouldCollapseToolBatch,
  summarizeToolAttention,
  toolParentCallId,
  toolRoundAttention,
  toolGroupRoundDisplay,
  toolGroupRoundNumber,
  toolGroupRoundTitle,
  toolExecutionLlmRound,
  type ToolExecutionBatch,
  type ToolAttentionLevel,
  type ToolAttentionSummary,
  type ToolGroupTranslator,
  type ToolPanelPresentation,
  type ToolPanelTranslator,
  type ToolRoundBatch,
} from './presentation/tool-execution-groups';
export {
  handleToolExecutionDisclosureClick,
} from './ui/tool-execution-disclosure';
export {
  BROWSER_TOOL_PRESENTATION_NAMES,
  CONVERSATION_METADATA_TOOL_NAMES,
  EXPLICIT_TOOL_ROUND_DISPLAY_NAMES,
  MOTION_TOOL_PRESENTATION_NAMES,
  PROJECT_TOOL_PRESENTATION_NAMES,
  explicitToolRoundDisplay,
  imageGenerationMode,
  isBrowserToolRound,
  isCodeExecutionToolRound,
  isConversationMetadataToolRound,
  isFetchToolRound,
  isImageGenerationToolRound,
  isMotionToolRound,
  isProgramToolRound,
  isProjectToolRound,
  isSearchToolRound,
  isSwarmToolRound,
  isToolSearchRound,
  plainToolStatus,
  programDisplayValue,
  toolRoundDisplay,
  toolRoundIconKey,
  type ToolRoundDisplay,
} from './presentation/tool-round-presentation';
export {
  imageGenerationChipSvg,
  toolRoundSvg,
} from './presentation/tool-round-icons';
export {
  presentConversationRateLimit,
  type ConversationRateLimitPresentation,
} from './presentation/live-phase';
export {
  createAnimationFrameScheduler,
} from './ui/animation-frame-scheduler';
export {
  createConversationSurfaceController,
  type ConversationSurfaceController,
  type ConversationSurfaceHost,
} from './application/conversation-surface-controller';
export {
  createConversationCatalogReconciler,
  type ConversationCatalogReconcilerPorts,
  type MutableCatalogConversation,
  type ReconcileConversationCatalog,
} from './application/conversation-catalog-reconciliation';
export {
  CONVERSATION_CATALOG_CACHE_WRITE_BUDGET,
  createConversationCatalogLoader,
  type ConversationCatalogLoader,
  type ConversationCatalogLoaderPorts,
  type ConversationCatalogRequest,
  type ConversationCatalogResponse,
} from './application/conversation-catalog-loader';
export {
  CONVERSATION_CATALOG_REFRESH_DELAY_MS,
  CONVERSATION_CATALOG_REVISION_BUDGET,
  createConversationCatalogRevisionGate,
  type ConversationCatalogRevisionGate,
  type ConversationCatalogRevisionGatePorts,
} from './application/conversation-catalog-revision-gate';
export {
  createConversationStartup,
  type ConversationStartupController,
  type ConversationStartupPorts,
  type StartupConversationReference,
} from './application/conversation-startup';
export {
  createBranchComposerSession,
  type BranchComposerSession,
  type BranchComposerTarget,
} from './application/branch-composer';
export {
  activeConversationAttemptIds,
  activeMainConversationAttemptId,
  conversationHasActor,
  latestConversationTurn,
  orderedConversationTurns,
} from './application/conversation-read-model';
export {
  createHumanGuidancePresentationStore,
  type HumanGuidancePresentation,
  type HumanGuidancePresentationStore,
} from './application/human-guidance-presentation';
export {
  createTransientTurnOverlay,
  type TransientTurnOverlay,
} from './application/transient-turn-overlay';
export {
  createSwarmPushPresentationController,
  createSwarmPushRuntime,
  swarmPresentationOverlay,
  type SwarmConversationReference,
  type SwarmOverlayReader,
  type SwarmPresentationContext,
  type SwarmPresentationController,
  type SwarmPushFrame,
  type SwarmPushPresentationPorts,
  type SwarmPushRuntime,
  type SwarmPushRuntimePorts,
} from './application/swarm-presentation-overlay';
export {
  createTransientStatusTurn,
  type CreateTransientStatusTurnInput,
} from './application/transient-status-turn';
export {
  createOptimisticUserTurn,
  createOptimisticTurnPair,
  optimisticAssistantTurnId,
  optimisticUserTurnId,
  withOptimisticAssistantPreparation,
  type OptimisticTurnPair,
  type CreateOptimisticUserTurnInput,
} from './application/optimistic-user-turn';
export {
  autopilotVuTransientTurnId,
  createAutopilotVuTransientTurn,
  maskAutopilotVuMachineTokens,
  reduceAutopilotVuTransientTurn,
  settleAutopilotVuTransientTurn,
  type AutopilotVuLifecycleEvent,
  type CreateAutopilotVuTransientInput,
} from './application/autopilot-vu-transient';
export {
  transientTurnPresentation,
  type TransientTurnPresentation,
  type TransientTurnRecord,
} from './domain/transient-turn';
export {
  CONVERSATION_DOM_WINDOW_BATCH_TURNS,
  CONVERSATION_DOM_WINDOW_MAX_TURNS,
  createConversationSurface,
  createConversationViewportPort,
  type ConversationIntent,
  type ConversationScrollSnapshot,
  type ConversationSurface,
  type ConversationSurfaceOptions,
  type ConversationViewportOptions,
  type ConversationWindowOptions,
  type ConversationWindowState,
  type ScrollAnchorPort,
} from './ui/conversation-surface';
export {
  createPlanDecisionBar,
  type PlanDecisionBar,
  type PlanDecisionBarCopy,
  type PlanExecutionContextMode,
} from './ui/plan-decision-bar';
export {
  createClassicConversationRenderers,
} from './ui/classic-conversation-renderers';
export {
  openTurnInlineEditor,
  reconcileTurnInlineEditors,
  type TurnInlineEditorOptions,
  type TurnInlineEditorSession,
  type TurnInlineEditorSubmit,
} from './ui/turn-inline-editor';
