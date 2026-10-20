/**
 * Presentation contract for blocks, Turns, lanes, local display state and diagnostics.
 * Types only; generated DTOs remain wire authority.
 * Entry: presentation type exports.
 * Dependencies: conversation-sync.generated, errors, transient-turn, turn-finish. */
import type { ToolEffectNotice } from '../tool-effect-presentation';
import type { AutomationManagementPresentation } from '../../../api/automation.generated';
import {
  type ConversationQueueItem,
  type TurnConversationReference,
  type TurnCompaction,
  type TurnContextSnapshot,
  type TurnActivityEntry,
  type TurnDocumentAttachment,
  type TurnFileChange,
  type TurnFileChangesBlock,
  type TurnImageAttachment,
  type TurnImageGeneration,
  type TurnLastRoundUsage,
  type TurnMessageInjection,
  type TurnMediaAttachment,
  type TurnOrchestration,
  type TurnOrigin,
  type TurnPlanExecution,
  type TurnProvenance,
  type RouteSnapshot,
  type TurnProposedPlan,
  type TurnRecord,
  type TurnRolledBackEntry,
  type TurnStallInjection,
  type TurnStatus,
  type TurnToolResult,
  type TurnToolRound,
  type TurnVideoAttachment,
} from '../../../api/conversation-sync.generated';
import {
  type ErrorEnvelope,
} from '../../../api/errors';
import {
  type TransientTurnPresentation,
} from '../../domain/transient-turn';
import {
  type TurnFinishPresentation,
} from '../turn-finish';

export type UnknownRecord = Record<string, unknown>;

export type ConversationBlockKind =
  | 'text'
  | 'thinking'
  | 'tool'
  | 'program'
  | 'attachments'
  | 'injections'
  | 'provenance'
  | 'system-note'

  | 'system-note-group'
  | 'file-changes'
  | 'origin'
  | 'context'
  | 'compaction'
  | 'image-generation'
  | 'rolled-back'
  | 'proposed-plan'
  | 'plan-execution'
  | 'artifacts'
  | 'autopilot-run-notice'
  | 'activity-event'
  | 'activity-event-group'
  | 'queue-status'
  | 'live-status';

export type BlockIdentitySource = 'contract' | 'compatibility';

interface ConversationBlockBase {
  blockId: string;
  kind: ConversationBlockKind;
  identitySource: BlockIdentitySource;
  /** Immutable source identity; used to skip untouched block renderers. */
  source: object;
}

export interface TextBlockViewModel extends ConversationBlockBase {
  kind: 'text';
  /** Original model/user-authored text. */
  markdown: string;
  /** Recorded translation provider model from projection.translation.model only. */
  translationModel?: string;

  /** Canonical completed input transform retained without its original body. */
  inputTranslationCompleted?: boolean;
  translatedMarkdown?: string;
  displayMarkdown: string;
  displayMode: TranslationDisplayMode;
  /** Settled deliverable presentation; a live accumulator stays narration. */
  deliverable: boolean;
  terminal: boolean;
  resumable: boolean;
}

export interface ThinkingBlockViewModel extends ConversationBlockBase {
  kind: 'thinking';
  markdown: string;
  translatedMarkdown?: string;
  displayMarkdown: string;
  displayMode: TranslationDisplayMode;
  terminal: boolean;
  signature?: string;
  /** Projected reasoning identities represented by this disclosure, in order. */
  memberBlockIds: ReadonlyArray<string>;
}

export interface ToolBlockViewModel extends ConversationBlockBase {
  kind: 'tool';
  effectNotice?: ToolEffectNotice;
  toolCallId: string;
  name: string;
  input: unknown;
  result: TurnToolResult;
  round?: TurnToolRound;
}

/** Display-only parent for native PTC or local ToolScript child calls. */
export interface ProgramBlockViewModel extends ConversationBlockBase {
  kind: 'program';
  effectNotice?: ToolEffectNotice;
  programCallId: string;
  round: TurnToolRound;
}

export interface AttachmentsBlockViewModel extends ConversationBlockBase {
  kind: 'attachments';
  images: ReadonlyArray<TurnImageAttachment>;
  videos: ReadonlyArray<TurnVideoAttachment>;
  pdfTexts: ReadonlyArray<TurnDocumentAttachment>;
  mediaAttachments: ReadonlyArray<TurnMediaAttachment>;
  conversationReferences: ReadonlyArray<TurnConversationReference>;
  replyQuotes: ReadonlyArray<string>;
}

export interface InjectionsBlockViewModel extends ConversationBlockBase {
  kind: 'injections';
  channel: 'inbox' | 'peer' | 'user-steer' | 'background-command' | 'stall-nudge';
  items: ReadonlyArray<TurnMessageInjection | TurnStallInjection>;
  anchorLlmRound: number | null;
}

/* Engine-authored intervention (SEG_SYSTEM_NOTE on the wire): the stall
 * nudge / todo-continuation reminder, carried as a first-class segment so it
 * renders at its true insertion position instead of a recomputed chip anchor.
 * `text` records the action, without claiming model consumption. */
export interface SystemNoteBlockViewModel extends ConversationBlockBase {
  kind: 'system-note';
  noteKind: 'intent-stall' | 'todo-continuation' | 'system-intervention'
    | 'context-injection' | 'session-context';
  injectionId?: string;
  action?: 'prompt' | 'stop' | 'tool-policy' | 'continue';
  reason?: string;
  text: string;
  llmRound: number | null;
  /** Selection/acquisition metadata paired with this witness, not model text. */
  provenance?: TurnProvenance;
}

/* A maximal run of adjacent context-injection notes merged into ONE row
 * (display-only; the timeline positions of the member notes are unchanged).
 * See coalesceAdjacentSystemNoteBlocks. */
export interface SystemNoteGroupBlockViewModel extends ConversationBlockBase {
  kind: 'system-note-group';
  items: ReadonlyArray<SystemNoteBlockViewModel>;
  /** Unmatched acquisition status; does not imply a context commit. */
  provenance?: TurnProvenance;
}

export interface FileChangesBlockViewModel extends ConversationBlockBase {
  kind: 'file-changes';
  count: number;
  files: ReadonlyArray<TurnFileChange>;
  state: NonNullable<TurnFileChangesBlock['state']>;
  commandAvailable: boolean;
  truncated?: boolean;
  error?: unknown;
}

export interface ProvenanceBlockViewModel extends ConversationBlockBase {
  kind: 'provenance';
  value: TurnProvenance;
}

export interface OriginBlockViewModel extends ConversationBlockBase {
  kind: 'origin';
  value: TurnOrigin;
}

export interface ContextBlockViewModel extends ConversationBlockBase {
  kind: 'context';
  value: TurnContextSnapshot;
}

export interface CompactionBlockViewModel extends ConversationBlockBase {
  kind: 'compaction';
  value: TurnCompaction;
  summaryMarkdown: string;
}

export interface RolledBackBlockViewModel extends ConversationBlockBase {
  kind: 'rolled-back';
  value: TurnRolledBackEntry;
}

export interface ImageGenerationBlockViewModel extends ConversationBlockBase {
  kind: 'image-generation';
  value: TurnImageGeneration;
}

export interface ProposedPlanBlockViewModel extends ConversationBlockBase {
  kind: 'proposed-plan';
  value: TurnProposedPlan;
  /** Original executable plan text from the authoritative plan sidecar. */
  markdown: string;
  /** Completed or lifecycle-local partial translation of the plan body. */
  translatedMarkdown?: string;
  displayMarkdown: string;
  displayMode: TranslationDisplayMode;
  translationPending: boolean;
  translationStreaming: boolean;
}

export interface PlanExecutionBlockViewModel extends ConversationBlockBase {
  kind: 'plan-execution';
  value: TurnPlanExecution;
}

/** Read-only artifact metadata normalized at the backend adapter boundary. */
export interface ConversationArtifactViewModel {
  id: string;
  format: string;
  title: string;
  sizeBytes?: number;
  sourcePath?: string;
  version?: number;
  createdAt?: number;
}

export interface ArtifactsBlockViewModel extends ConversationBlockBase {
  kind: 'artifacts';
  artifacts: ReadonlyArray<ConversationArtifactViewModel>;
}

export type AutopilotRunNoticeReason =
  | 'worker_lost'
  | 'yielded_to_human'
  | 'aborted_mid_vu'
  | 'superseded'
  | 'budget_exhausted'
  | 'no_progress'
  | 'stuck'
  | 'stalled'
  | 'vu_blocked';

export interface AutopilotRunNotice {
  runId: string;
  reason: AutopilotRunNoticeReason;
  unsent: boolean;
  content?: string;
  /** The settings record carries a bounded excerpt; the full stop report is
   * fetchable from the durable GoalRun transition when this is set. */
  hasFullReport?: boolean;
  /** Advisory only: the server revalidates Stop, lane tail and effect evidence. */
  canRequestRecovery?: boolean;
}

export interface AutopilotRunNoticeBlockViewModel extends ConversationBlockBase {
  kind: 'autopilot-run-notice';
  value: AutopilotRunNotice;
}

export interface ActivityEventBlockViewModel extends ConversationBlockBase {
  kind: 'activity-event';
  value: TurnActivityEntry;
  /**
   * Lossless terminal authority for the latest failed attempt. Activity rows
   * remain bounded diagnostics; the UI must never mistake their compact copy
   * for the complete error that the Turn settlement already owns.
   */
  terminalError?: ErrorEnvelope;
}

/* A maximal run of adjacent failed-attempt / retry-wait activity rows merged
 * into ONE collapsed disclosure (display-only; every member row keeps its own
 * identity and evidence inside the group). See
 * coalesceAdjacentRetryActivityBlocks. */
export interface ActivityEventGroupBlockViewModel extends ConversationBlockBase {
  kind: 'activity-event-group';
  items: ReadonlyArray<ActivityEventBlockViewModel>;
}

export interface LiveStatusBlockViewModel extends ConversationBlockBase {
  kind: 'live-status';
  value: TransientTurnPresentation;
}

export interface QueueStatusBlockViewModel extends ConversationBlockBase {
  kind: 'queue-status';
  value: {
    queueId: string;
    position: number;
    text: string;
    /** True when this queued item began life as a steer ("insert now")
     * choice but the live steer lane was unavailable, so it degraded to
     * the durable queue — surfaces must say WHY it waits for the next
     * turn instead of injecting mid-reply. */
    steerFallback?: boolean;
    backgroundCommand?: ConversationQueueItem['backgroundCommand'];
  };
}

export type ConversationBlockViewModel =
  | TextBlockViewModel
  | ThinkingBlockViewModel
  | ToolBlockViewModel
  | ProgramBlockViewModel
  | AttachmentsBlockViewModel
  | InjectionsBlockViewModel
  | ProvenanceBlockViewModel
  | SystemNoteBlockViewModel

  | SystemNoteGroupBlockViewModel
  | FileChangesBlockViewModel
  | OriginBlockViewModel
  | ContextBlockViewModel
  | CompactionBlockViewModel
  | ImageGenerationBlockViewModel
  | RolledBackBlockViewModel
  | ProposedPlanBlockViewModel
  | PlanExecutionBlockViewModel
  | ArtifactsBlockViewModel
  | AutopilotRunNoticeBlockViewModel
  | ActivityEventBlockViewModel
  | ActivityEventGroupBlockViewModel
  | QueueStatusBlockViewModel
  | LiveStatusBlockViewModel;

export interface TurnMetadataViewModel {
  taskTimePresentations?: ReadonlyArray<AutomationManagementPresentation>;
  model?: string;
  preset?: string;
  providerId?: string;
  thinkingDepth?: string | number;
  usage?: UnknownRecord;
  /** Client-side cost-cache signature for settled turns whose projection
   *  lacks an authoritative cost; changes when the async batch lookup lands
   *  so the surface footer compare sees the fill. Undefined while unknown
   *  or when projection.cost already covers the render. */
  costSignature?: string;
  lastRoundUsage?: TurnLastRoundUsage;
  /** The exact credential-redacted route that authored this Turn. */
  routeSnapshot?: RouteSnapshot;
  modifiedFiles?: number;
  modifiedFileList?: ReadonlyArray<unknown>;
  todoState?: UnknownRecord;
  waitingOn?: UnknownRecord;
  orchestration?: TurnOrchestration;
  origin?: TurnOrigin;
  fallback?: {
    model?: string;
    from?: string;
    reason?: string;
    kind?: string;
  };
  /** True when the canonical timeline already renders the fallback transition. */
  fallbackInTimeline: boolean;
  translation: {
    completed: boolean;
    available: boolean;
    displayMode: TranslationDisplayMode;
    pending: boolean;
    skippedReason?: string;
    model?: string;
    error?: unknown;
  };
}

export type TranslationDisplayMode = 'original' | 'translated';

/** Lifecycle-local presentation preferences; never persisted as turn facts. */
export interface ConversationPresentationState {
  taskTimePresentationsByAttempt?: ReadonlyMap<string, ReadonlyArray<AutomationManagementPresentation>>;
  translationModeByTurn?: ReadonlyMap<string, TranslationDisplayMode>;
  translationActivityByTurn?: ReadonlyMap<string, TranslationActivity>;
  artifactsByTurn?: ReadonlyMap<
    string,
    ReadonlyArray<ConversationArtifactViewModel>
  >;
  expandedBranchLaneId?: string | null;
  /** Latest sync-channel health published by the conversation coordinator.
   *  Transport state only — it never mutates a Turn fact. */
  syncHealth?: { state?: string } | null;
  /** Per-turn client cost-cache signatures (turnId → rendered cost value).
   *  The batch fill mutates no Turn fact, so it must ride presentation
   *  state to reach the footer re-render compare. */
  costSignatureByTurnId?: ReadonlyMap<string, string>;
  /** Local debug preference; task identity still comes from Turn attempts. */
  requestInspectorEnabled?: boolean;
  /** Backend-authored run conclusions; never projected into transcript text. */
  autopilotSummaries?: Readonly<
    Record<string, Readonly<Record<string, unknown>>>
  >;
}

export interface TranslationActivity {
  status: 'pending' | 'failed';
  message?: string;
  /** Ephemeral streamed preview; durable completion remains in the Turn. */
  partial?: string;
  partialByRound?: Readonly<Record<string, string>>;
  error?: unknown;
}

export interface ConversationTurnViewModel {
  turnId: string;
  presentationId: string;
  laneId: string;
  parentTurnId: string | null;
  ordinal: number;
  actor: TurnRecord['actor'];
  role: 'user' | 'assistant';
  kind: string;
  status: TurnStatus;
  attemptId: string | null;
  taskId?: string;
  projectionRevision: number;
  commandPending: string | null;
  finish: TurnFinishPresentation | null;
  actions: ReadonlyArray<ConversationTurnActionViewModel>;
  blocks: ReadonlyArray<ConversationBlockViewModel>;
  branches: ReadonlyArray<ConversationLaneViewModel>;
  metadata: TurnMetadataViewModel;
  source: TurnRecord;
}

export type ConversationTurnAction =
  | 'copy'
  | 'inspect'
  | 'edit'
  | 'regenerate'
  | 'resume'
  | 'translate'
  | 'export'
  | 'review'
  | 'promote-decision'
  | 'branch'
  | 'delete';

export interface ConversationTurnActionViewModel {
  action: ConversationTurnAction;
  operation?: string;
  disabled: boolean;
}

export interface ConversationLaneViewModel {
  laneId: string;
  parentTurnId: string | null;
  title: string;
  label?: string;
  icon?: string;
  kind: string;
  anchorText?: string;
  parentSelection?: string;
  expanded: boolean;
  live: boolean;
  humanTurnCount: number;
  history?: {
    nextBeforeOrdinal: number | null;
    hasMore: boolean;
    totalTurns: number;
  };
  turns: ReadonlyArray<ConversationTurnViewModel>;
}

export interface ConversationQueueItemViewModel {
  queueId: string;
  position: number;
  kind: string;
  text: string;
  source: ConversationQueueItem;
}

export interface ConversationViewModel {
  conversationId: string;
  conversationRevision: number;
  transport: string;
  mainLane: ConversationLaneViewModel;
  orphanLanes: ReadonlyArray<ConversationLaneViewModel>;
  queue: ReadonlyArray<ConversationQueueItemViewModel>;
  planDecision: PlanDecisionViewModel | null;
  /** Conversation-level sync-channel degradation; null when healthy. Never a
   *  turn failure — the banner explains transcript staleness only. */
  transportNotice: 'recovering' | 'degraded' | 'offline' | null;
}

export interface PlanDecisionViewModel {
  sourceTurnId: string;
  sourceProjectionRevision: number;
  planId: string;
  pending: boolean;
}

export interface ConversationViewModelDiagnostics {
  onCompatibilityIdentity?(turnId: string, blockId: string): void;
  onLaneCycle?(laneId: string): void;
}
