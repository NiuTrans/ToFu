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
  createTransientStatusTurn,
  type CreateTransientStatusTurnInput,
} from './application/transient-status-turn';
export {
  createOptimisticUserTurn,
  optimisticUserTurnId,
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
