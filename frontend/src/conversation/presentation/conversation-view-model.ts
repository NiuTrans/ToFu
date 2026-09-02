/**
 * Pure TurnStore -> ConversationViewModel selector.
 *
 * The selector owns render identity and lane joins.  It performs no DOM work,
 * reads no browser globals, and never mutates the normalized store document.
 */
import type {
  BranchLaneDescriptor,
  ConversationQueueItem,
  TurnConversationReference,
  TurnCompaction,
  TurnContentSegment,
  TurnContextSnapshot,
  TurnActivityEntry,
  TurnActivityTimeline,
  TurnDocumentAttachment,
  TurnFileChange,
  TurnFileChangesBlock,
  TurnImageAttachment,
  TurnImageGeneration,
  TurnMessageInjection,
  TurnOrchestration,
  TurnOrigin,
  TurnPlanExecution,
  TurnProvenance,
  TurnProjection,
  TurnProposedPlan,
  TurnRecord,
  TurnStallInjection,
  TurnStatus,
  TurnTranslation,
  TurnToolResult,
  TurnToolRound,
  TurnVideoAttachment,
} from '../../api/conversation-sync.generated';
import {
  normalizeErrorEnvelope,
  type ErrorEnvelope,
} from '../../api/errors';
import type { TurnState } from '../domain/turn-store';
import {
  transientTurnPresentation,
  type TransientTurnPresentation,
} from '../domain/transient-turn';
import {
  presentTurnFinish,
  type TurnFinishPresentation,
} from './turn-finish';

type UnknownRecord = Record<string, unknown>;

export type ConversationBlockKind =
  | 'text'
  | 'thinking'
  | 'tool'
  | 'attachments'
  | 'injections'
  | 'provenance'
  | 'file-changes'
  | 'origin'
  | 'context'
  | 'compaction'
  | 'image-generation'
  | 'proposed-plan'
  | 'plan-execution'
  | 'artifacts'
  | 'autopilot-run-notice'
  | 'activity-event'
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
  translatedMarkdown?: string;
  displayMarkdown: string;
  displayMode: TranslationDisplayMode;
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
}

export interface ToolBlockViewModel extends ConversationBlockBase {
  kind: 'tool';
  toolCallId: string;
  name: string;
  input: unknown;
  result: TurnToolResult;
  round?: TurnToolRound;
}

export interface AttachmentsBlockViewModel extends ConversationBlockBase {
  kind: 'attachments';
  images: ReadonlyArray<TurnImageAttachment>;
  videos: ReadonlyArray<TurnVideoAttachment>;
  pdfTexts: ReadonlyArray<TurnDocumentAttachment>;
  conversationReferences: ReadonlyArray<TurnConversationReference>;
  replyQuotes: ReadonlyArray<string>;
}

export interface InjectionsBlockViewModel extends ConversationBlockBase {
  kind: 'injections';
  channel: 'inbox' | 'peer' | 'user-steer' | 'stall-nudge';
  items: ReadonlyArray<TurnMessageInjection | TurnStallInjection>;
  anchorLlmRound: number | null;
}

export interface FileChangesBlockViewModel extends ConversationBlockBase {
  kind: 'file-changes';
  count: number;
  files: ReadonlyArray<TurnFileChange>;
  state: NonNullable<TurnFileChangesBlock['state']>;
  commandAvailable: boolean;
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
  | 'yielded_to_human'
  | 'aborted_mid_vu'
  | 'superseded'
  | 'budget_exhausted'
  | 'no_progress'
  | 'stuck';

export interface AutopilotRunNotice {
  runId: string;
  reason: AutopilotRunNoticeReason;
  unsent: boolean;
  content?: string;
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

export interface LiveStatusBlockViewModel extends ConversationBlockBase {
  kind: 'live-status';
  value: TransientTurnPresentation;
}

export type ConversationBlockViewModel =
  | TextBlockViewModel
  | ThinkingBlockViewModel
  | ToolBlockViewModel
  | AttachmentsBlockViewModel
  | InjectionsBlockViewModel
  | ProvenanceBlockViewModel
  | FileChangesBlockViewModel
  | OriginBlockViewModel
  | ContextBlockViewModel
  | CompactionBlockViewModel
  | ImageGenerationBlockViewModel
  | ProposedPlanBlockViewModel
  | PlanExecutionBlockViewModel
  | ArtifactsBlockViewModel
  | AutopilotRunNoticeBlockViewModel
  | ActivityEventBlockViewModel
  | LiveStatusBlockViewModel;

export interface TurnMetadataViewModel {
  model?: string;
  preset?: string;
  providerId?: string;
  thinkingDepth?: string | number;
  usage?: UnknownRecord;
  lastRoundUsage?: UnknownRecord;
  modifiedFiles?: number;
  modifiedFileList?: ReadonlyArray<unknown>;
  todoState?: UnknownRecord;
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
  translationModeByTurn?: ReadonlyMap<string, TranslationDisplayMode>;
  translationActivityByTurn?: ReadonlyMap<string, TranslationActivity>;
  artifactsByTurn?: ReadonlyMap<
    string,
    ReadonlyArray<ConversationArtifactViewModel>
  >;
  expandedBranchLaneId?: string | null;
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

function asArray<Item>(value: ReadonlyArray<Item> | undefined): ReadonlyArray<Item> {
  return value ?? [];
}

function roleFor(turn: TurnRecord): 'user' | 'assistant' {
  return turn.actor === 'human' || turn.actor === 'critic'
    || turn.actor === 'virtual_user' ? 'user' : 'assistant';
}

function withoutAuthoritativeProposedPlan(
  markdown: string,
  expectedText?: string,
): string {
  const pattern = /<proposed_plan>\s*\n?([\s\S]*?)\n?\s*<\/proposed_plan>/gi;
  let selected: { index: number; length: number } | null = null;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(markdown)) !== null) {
    if (expectedText === undefined || match[1].trim() === expectedText.trim()) {
      selected = { index: match.index, length: match[0].length };
    }
  }
  if (!selected) return markdown.trimEnd();
  const before = markdown.slice(0, selected.index);
  const after = markdown.slice(selected.index + selected.length);
  let visible = `${before}${after}`;
  if (!before.trim()) {
    visible = visible.replace(/^(?:[ \t]*\r?\n)+/, '');
  }
  return visible.replace(/\n[ \t]*\n[ \t]*\n+/g, '\n\n').trimEnd();
}

/**
 * Read the last translated plan body without granting it execution authority.
 * Completed projections require the closing tag. Lifecycle-local previews may
 * expose the body after the opening tag while the translator is still writing.
 */
function translatedProposedPlanBody(
  markdown: string | undefined,
  allowIncomplete: boolean,
): string | undefined {
  if (!markdown) return undefined;
  const openPattern = /<proposed_plan>\s*\n?/gi;
  let open: RegExpExecArray | null = null;
  let candidate: RegExpExecArray | null;
  while ((candidate = openPattern.exec(markdown)) !== null) open = candidate;
  if (!open) return undefined;
  const tail = markdown.slice(open.index + open[0].length);
  const close = tail.search(/\n?\s*<\/proposed_plan>/i);
  if (close < 0 && !allowIncomplete) return undefined;
  const body = (close < 0 ? tail : tail.slice(0, close))
    .replace(/\n?\s*<\/?proposed_plan[^>]*$/i, '');
  const normalized = body.trim();
  return normalized || undefined;
}

function untaggedPlanTranslation(
  markdown: string | undefined,
): string | undefined {
  if (!markdown || /<\/?proposed_plan\b/i.test(markdown)) return undefined;
  return markdown.trim() || undefined;
}

function translatedPlanPreview(
  projection: TurnProjection,
  proposedPlan: TurnProposedPlan,
  activity?: TranslationActivity,
): { markdown?: string; pending: boolean; streaming: boolean } {
  const pending = activity?.status === 'pending';
  const originalOutsidePlan = withoutAuthoritativeProposedPlan(
    projection.content ?? '', proposedPlan.text,
  ).trim();
  const planOnly = originalOutsidePlan.length === 0;
  const completed = translatedProposedPlanBody(
    projection.translatedContent, false,
  ) ?? (planOnly
    ? untaggedPlanTranslation(projection.translatedContent) : undefined);
  const partial = pending
    ? translatedProposedPlanBody(activity?.partial, true)
      ?? (planOnly ? untaggedPlanTranslation(activity?.partial) : undefined)
    : undefined;
  return {
    markdown: partial ?? completed,
    pending,
    streaming: Boolean(partial),
  };
}

function claimedBlockId(
  turnId: string,
  proposed: string,
  identitySource: BlockIdentitySource,
  claims: Map<string, number>,
  diagnostics: ConversationViewModelDiagnostics,
): string {
  const count = (claims.get(proposed) ?? 0) + 1;
  claims.set(proposed, count);
  const blockId = count === 1 ? proposed : `${proposed}~${count}`;
  if (identitySource === 'compatibility') {
    diagnostics.onCompatibilityIdentity?.(turnId, blockId);
  }
  return blockId;
}

function blockFromSegment(
  turn: TurnRecord,
  segment: TurnContentSegment,
  occurrence: number,
  claims: Map<string, number>,
  diagnostics: ConversationViewModelDiagnostics,
  translationMode: TranslationDisplayMode,
  translationActivity?: TranslationActivity,
): ConversationBlockViewModel {
  const identitySource: BlockIdentitySource = 'contract';
  const blockId = claimedBlockId(
    turn.turnId, segment.blockId,
    identitySource,
    claims,
    diagnostics,
  );
  if (segment.type === 'text') {
    const authoritative = (segment.terminal || segment.deliverable)
      ? turn.projection.content ?? segment.text : segment.text;
    const humanOriginal = turn.actor === 'human'
      && (segment.terminal || segment.deliverable)
      ? turn.projection.originalContent : undefined;
    const rawOriginal = humanOriginal || authoritative;
    const translated = humanOriginal ? authoritative : segment.translatedText
      || ((segment.terminal || segment.deliverable)
        ? turn.projection.translatedContent : undefined);
    const ownsProposedPlan = Boolean(
      turn.projection.proposedPlan && (segment.terminal || segment.deliverable),
    );
    const original = ownsProposedPlan
      ? withoutAuthoritativeProposedPlan(
        rawOriginal, turn.projection.proposedPlan?.text,
      ) : rawOriginal;
    const visibleTranslation = translated && ownsProposedPlan
      ? (translatedProposedPlanBody(translated, false)
        ? withoutAuthoritativeProposedPlan(translated) : undefined)
      : translated;
    const displayMode = visibleTranslation && translationMode === 'translated'
      ? 'translated' : 'original';
    return {
      blockId,
      kind: 'text',
      identitySource,
      source: segment,
      markdown: original,
      ...(visibleTranslation ? { translatedMarkdown: visibleTranslation } : {}),
      displayMarkdown: displayMode === 'translated'
        ? visibleTranslation ?? original : original,
      displayMode,
      deliverable: Boolean(segment.deliverable),
      terminal: Boolean(segment.terminal),
      resumable: Boolean(segment.resumable),
    };
  }
  if (segment.type === 'thinking') {
    const markdown = segment.terminal
      ? turn.projection.thinking ?? segment.text : segment.text;
    /* Closed reasoning is immutable history, so its translation is pinned
     * onto the segment (translatedText). While the translation worker is
     * still catching up, the live per-round preview (partialByRound, keyed
     * by the segment's blockId) already renders in its place — the reader
     * never has to reopen the turn to see reasoning in the UI language.
     * The preview is presentation-only: it never becomes translatedMarkdown,
     * so toggle/availability semantics keep tracking durable facts. */
    const durable = segment.translatedText;
    const preview = !durable && translationActivity?.status === 'pending'
      ? translationActivity.partialByRound?.[segment.blockId] : undefined;
    const livePreview = preview && preview.trim() ? preview : undefined;
    const displayMode = (durable || livePreview)
      && translationMode === 'translated' ? 'translated' : 'original';
    return {
      blockId,
      kind: 'thinking',
      identitySource,
      source: segment,
      markdown,
      ...(durable ? { translatedMarkdown: durable } : {}),
      displayMarkdown: displayMode === 'translated'
        ? durable ?? livePreview ?? markdown : markdown,
      displayMode,
      terminal: Boolean(segment.terminal),
      ...(segment.signature ? { signature: segment.signature } : {}),
    };
  }
  const richRound = segment._round
    ?? turn.projection.toolRounds?.find((round) => (
      Boolean(segment.id) && round.toolCallId === segment.id
    ));
  return {
    blockId,
    kind: 'tool',
    identitySource,
    source: segment,
    toolCallId: segment.id,
    name: segment.name,
    input: segment.input,
    result: segment.result,
    ...(richRound ? { round: richRound } : {}),
  };
}

function addProjectionSidecarBlocks(
  turn: TurnRecord,
  blocks: ConversationBlockViewModel[],
  claims: Map<string, number>,
  diagnostics: ConversationViewModelDiagnostics,
  translationMode: TranslationDisplayMode,
  translationActivity?: TranslationActivity,
): void {
  const projection = turn.projection;
  const contractBlockId = (proposed: string): string => claimedBlockId(
    turn.turnId, proposed, 'contract', claims, diagnostics,
  );
  if (projection.images?.length || projection.videos?.length
      || projection.pdfTexts?.length || projection.convRefs?.length
      || projection.replyQuotes?.length) {
    blocks.unshift({
      blockId: contractBlockId('attachments'),
      kind: 'attachments',
      identitySource: 'contract',
      source: projection,
      images: asArray(projection.images),
      videos: asArray(projection.videos),
      pdfTexts: asArray(projection.pdfTexts),
      conversationReferences: asArray(projection.convRefs),
      replyQuotes: projection.replyQuotes ?? [],
    });
  }
  if (projection.provenance) {
    blocks.unshift({
      blockId: claimedBlockId(
        turn.turnId,
        projection.provenance.blockId,
        'contract',
        claims,
        diagnostics,
      ),
      kind: 'provenance',
      identitySource: 'contract',
      source: projection.provenance,
      value: projection.provenance,
    });
  }
  const leadingBlocks: ConversationBlockViewModel[] = [];
  if (projection.origin) {
    leadingBlocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.origin.blockId, 'contract', claims, diagnostics,
      ),
      kind: 'origin',
      identitySource: 'contract',
      source: projection.origin,
      value: projection.origin,
    });
  }
  if (projection.contextSnapshot) {
    leadingBlocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.contextSnapshot.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'context',
      identitySource: 'contract',
      source: projection.contextSnapshot,
      value: projection.contextSnapshot,
    });
  }
  if (leadingBlocks.length) blocks.unshift(...leadingBlocks);
  const injections: ReadonlyArray<[
    InjectionsBlockViewModel['channel'],
    ReadonlyArray<TurnMessageInjection | TurnStallInjection> | undefined,
  ]> = [
    ['inbox', projection._inboxInjects],
    ['peer', projection._peerInjects],
    ['user-steer', projection._userSteerInjects],
    ['stall-nudge', projection._stallNudges],
  ];
  for (const [channel, items] of injections) {
    if (!items?.length) continue;
    for (const item of items) {
      const round = Number.isInteger(item.round) && Number(item.round) >= 0
        ? Number(item.round) : null;
      const anchorLlmRound = round == null ? null : round - 1;
      const block: InjectionsBlockViewModel = {
        blockId: claimedBlockId(
          turn.turnId,
          item.blockId,
          'contract',
          claims,
          diagnostics,
        ),
        kind: 'injections',
        identitySource: 'contract',
        source: item,
        channel,
        items: [item],
        anchorLlmRound,
      };
      const anchorIndex = anchorLlmRound == null ? -1 : blocks.findIndex((candidate) => {
        if (candidate.kind === 'attachments' || candidate.kind === 'injections'
            || candidate.kind === 'provenance') return false;
        const source = candidate.source as { llmRound?: number | null };
        const candidateRound = candidate.kind === 'tool'
          ? candidate.round?.llmRound ?? source.llmRound : source.llmRound;
        return candidateRound === anchorLlmRound;
      });
      blocks.splice(anchorIndex >= 0 ? anchorIndex : blocks.length, 0, block);
    }
  }
  const fileChanges = projection.fileChanges;
  if (fileChanges && fileChanges.files.length) {
    const files = fileChanges.files;
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId,
        fileChanges.blockId,
        'contract',
        claims,
        diagnostics,
      ),
      kind: 'file-changes',
      identitySource: 'contract',
      source: fileChanges,
      count: Math.max(fileChanges.count, projection.modifiedFiles ?? 0, files.length),
      files,
      state: fileChanges.state ?? 'applied',
      commandAvailable: Boolean(fileChanges.taskId),
      ...(fileChanges.error == null ? {} : { error: fileChanges.error }),
    });
  }
  if (projection.compaction) {
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.compaction.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'compaction',
      identitySource: 'contract',
      source: projection.compaction,
      value: projection.compaction,
      summaryMarkdown: projection.content ?? '',
    });
  }
  if (projection.imageGeneration) {
    blocks.unshift({
      blockId: claimedBlockId(
        turn.turnId, projection.imageGeneration.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'image-generation',
      identitySource: 'contract',
      source: projection.imageGeneration,
      value: projection.imageGeneration,
    });
  }
  if (projection.proposedPlan) {
    const translated = translatedPlanPreview(
      projection, projection.proposedPlan, translationActivity,
    );
    const displayMode = translated.markdown && translationMode === 'translated'
      ? 'translated' : 'original';
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.proposedPlan.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'proposed-plan',
      identitySource: 'contract',
      source: projection.proposedPlan,
      value: projection.proposedPlan,
      markdown: projection.proposedPlan.text,
      ...(translated.markdown
        ? { translatedMarkdown: translated.markdown } : {}),
      displayMarkdown: displayMode === 'translated'
        ? translated.markdown ?? projection.proposedPlan.text
        : projection.proposedPlan.text,
      displayMode,
      translationPending: translated.pending,
      translationStreaming: translated.streaming,
    });
  }
  if (projection.planExecution) {
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, projection.planExecution.blockId,
        'contract', claims, diagnostics,
      ),
      kind: 'plan-execution',
      identitySource: 'contract',
      source: projection.planExecution,
      value: projection.planExecution,
    });
  }
  const activityTimeline = projection.activityTimeline;
  const activityEntries = visibleActivityEntries(
    activityTimeline, projection.toolRounds ?? [],
  );
  const terminalError = turn.status === 'failed'
    ? normalizeErrorEnvelope(turn.settlement?.error) : null;
  const terminalErrorEntry = terminalError
    ? [...activityEntries].reverse().find((entry) => (
      entry.kind === 'error' && entry.status === 'failed'
    ))
    : undefined;
  if (activityTimeline && activityEntries.length) {
    /* Diagnostic rows anchor inline where they happened: a tool-linked row
     * sits right under its tool block, everything else rides its 0-based
     * model round so the transcript keeps the interleaved
     * thinking → content → tool flow instead of one consolidated tail. */
    const anchorWindows = activityAnchorWindows(activityTimeline, blocks);
    for (const entry of activityEntries) {
      const anchor = activityAnchorIndex(
        blocks, entry, anchorWindows.get(entry.id),
      );
      blocks.splice(anchor, 0, {
        blockId: claimedBlockId(
          turn.turnId, `activity:${entry.id}`, 'contract', claims, diagnostics,
        ),
        kind: 'activity-event',
        identitySource: 'contract',
        source: entry,
        value: entry,
        ...(entry.id === terminalErrorEntry?.id && terminalError
          ? { terminalError } : {}),
      });
    }
    const droppedCount = activityTimeline.droppedCount ?? 0;
    if (droppedCount > 0) {
      const droppedEntry: TurnActivityEntry = {
        id: 'dropped',
        spanId: 'dropped',
        seq: activityEntries[0]?.seq ?? 0,
        occurredAt: activityEntries[0]?.occurredAt ?? 0,
        kind: 'system',
        status: 'skipped',
        severity: 'info',
        count: 1,
        summary: `${droppedCount} older timeline events were compacted`,
        summaryKey: 'activity.timeline.olderCompacted',
        summaryArgs: { count: droppedCount },
      };
      const firstEvent = blocks.findIndex((block) => (
        block.kind === 'activity-event'
      ));
      blocks.splice(firstEvent >= 0 ? firstEvent : blocks.length, 0, {
        blockId: claimedBlockId(
          turn.turnId, 'activity:dropped', 'contract', claims, diagnostics,
        ),
        kind: 'activity-event',
        identitySource: 'contract',
        source: droppedEntry,
        value: droppedEntry,
      });
    }
  }
  if (terminalError && !terminalErrorEntry) {
    const terminalEntry: TurnActivityEntry = {
      id: 'terminal-error',
      spanId: 'terminal-error',
      seq: activityEntries[activityEntries.length - 1]?.seq ?? 0,
      occurredAt: turn.updatedAt,
      kind: 'error',
      status: 'failed',
      severity: terminalError.severity === 'warning' ? 'warning' : 'error',
      count: 1,
      summary: 'Turn failed',
      summaryKey: 'activity.error.failed',
      reasonCode: terminalError.kind,
      ...(terminalError.model ? { model: terminalError.model } : {}),
    };
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, 'activity:terminal-error', 'contract', claims, diagnostics,
      ),
      kind: 'activity-event',
      identitySource: 'contract',
      source: terminalEntry,
      value: terminalEntry,
      terminalError,
    });
  }
}

function recordValue(value: unknown): UnknownRecord | null {
  if (typeof value === 'string') {
    try {
      const parsed: unknown = JSON.parse(value);
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
        ? parsed as UnknownRecord : null;
    } catch {
      return null;
    }
  }
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null;
}

function boundedActivityText(value: unknown, maxChars: number): string {
  return typeof value === 'string'
    ? value.replace(/\s+/g, ' ').trim().slice(0, maxChars) : '';
}

function legacyGatewayValidationEntries(
  entry: TurnActivityEntry,
  toolRounds: ReadonlyArray<TurnToolRound>,
): ReadonlyArray<TurnActivityEntry> {
  const round = toolRounds.find((candidate) => (
    candidate.toolCallId === entry.toolCallId
    && candidate.toolName === 'execute_tools'
  ));
  const envelope = recordValue(round?.toolContent);
  const items = Array.isArray(envelope?.items) ? envelope.items : [];
  const payload = recordValue(items[0]) ?? envelope;
  const errors = Array.isArray(payload?.errors) ? payload.errors.slice(0, 8) : [];
  return errors.flatMap((rawError, index) => {
    const error = recordValue(rawError);
    if (!error) return [];
    const toolName = boundedActivityText(
      error.name ?? error.attempted, 160,
    ) || 'tool request';
    const message = boundedActivityText(error.message, 260);
    const retryHint = boundedActivityText(
      error.retry_hint ?? error.next_action, 220,
    );
    const detail = boundedActivityText(
      [message, retryHint ? `Next: ${retryHint}` : '']
        .filter(Boolean).join(' '),
      400,
    );
    return [{
      ...entry,
      id: `${entry.id}:validation-${index}`,
      spanId: `${entry.spanId}:validation-${index}`,
      kind: 'tool',
      status: 'skipped',
      severity: 'warning',
      summary: `${toolName} skipped`,
      summaryKey: 'activity.tool.skipped',
      summaryArgs: { tool: toolName },
      ...(detail ? { detail } : {}),
      reasonCode: boundedActivityText(error.code, 160) || 'gateway_validation',
      toolName,
    } satisfies TurnActivityEntry];
  });
}

function visibleActivityEntries(
  timeline: TurnActivityTimeline | undefined,
  toolRounds: ReadonlyArray<TurnToolRound>,
): ReadonlyArray<TurnActivityEntry> {
  /* Routine info facts are already owned by inline tool blocks and live
   * status. A settled compaction receipt is the deliberate exception: it is a
   * durable context boundary with accounting + archive identity, not another
   * progress beat. Protocol adapters are excluded as a cold-replay defense. */
  return (timeline?.entries ?? []).flatMap((entry) => {
    if (!activityEntryIsVisible(entry)) return [];
    if (entry.toolName !== 'execute_tools') return [entry];
    return legacyGatewayValidationEntries(entry, toolRounds);
  });
}

function activityEntryIsVisible(entry: TurnActivityEntry): boolean {
  return entry.severity !== 'info' || (
    entry.reasonCode === 'context_compaction'
    && Boolean(entry.archiveId)
  );
}

function activityBlockLlmRound(
  block: ConversationBlockViewModel,
): number | null {
  const source = block.source as { llmRound?: number | null };
  const round = block.kind === 'tool'
    ? block.round?.llmRound ?? source.llmRound
    : source.llmRound;
  return typeof round === 'number' && Number.isInteger(round) ? round : null;
}

/**
 * Chronological bounds for one diagnostic row, derived from the durable
 * timeline rather than from round numbers.  ``llmRound`` is only monotonic
 * within ONE execution attempt: a continued/resumed turn restarts the model
 * round counter at 0, so an unbounded round scan can anchor an earlier
 * attempt's error after content that did not exist yet (the resumed run's
 * fresh round-0/1/2 tool blocks sort below a round-19 failure).  Tool rows
 * in the timeline pin tool blocks to real chronology: a diagnostic sits
 * after the latest tool row that precedes it and before the earliest tool
 * row that follows it.  Round-riding semantics stay intact inside that
 * window.
 */
interface ActivityAnchorWindow {
  afterToolCallId?: string;
  beforeToolCallId?: string;
}

function activityAnchorWindows(
  timeline: TurnActivityTimeline,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
): Map<string, ActivityAnchorWindow> {
  const toolCallIds = new Set<string>();
  for (const block of blocks) {
    if (block.kind === 'tool' && block.toolCallId) {
      toolCallIds.add(block.toolCallId);
    }
  }
  const entries = timeline.entries ?? [];
  const windows = new Map<string, ActivityAnchorWindow>();
  let spine = '';
  for (const entry of entries) {
    if (activityEntryIsVisible(entry)) {
      windows.set(entry.id, spine ? { afterToolCallId: spine } : {});
    }
    if (entry.toolCallId && toolCallIds.has(entry.toolCallId)) {
      spine = entry.toolCallId;
    }
  }
  let limit = '';
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (activityEntryIsVisible(entry)) {
      const anchorWindow = windows.get(entry.id);
      if (anchorWindow && limit) anchorWindow.beforeToolCallId = limit;
    }
    if (entry.toolCallId && toolCallIds.has(entry.toolCallId)) {
      limit = entry.toolCallId;
    }
  }
  return windows;
}

function activityAnchorIndex(
  blocks: ReadonlyArray<ConversationBlockViewModel>,
  entry: TurnActivityEntry,
  anchorWindow?: ActivityAnchorWindow,
): number {
  if (entry.toolCallId) {
    const toolIndex = blocks.findIndex((candidate) => (
      candidate.kind === 'tool' && candidate.toolCallId === entry.toolCallId
    ));
    if (toolIndex >= 0) {
      let end = toolIndex + 1;
      while (end < blocks.length) {
        const next = blocks[end];
        if (next.kind !== 'activity-event'
            || next.value.toolCallId !== entry.toolCallId) break;
        end += 1;
      }
      return end;
    }
  }
  /* Confine the scan to blocks the timeline proves existed when this row
   * was recorded; a missing bound degrades to the legacy unbounded scan. */
  let start = 0;
  let end = blocks.length;
  if (anchorWindow?.afterToolCallId) {
    const spineIndex = blocks.findIndex((candidate) => (
      candidate.kind === 'tool'
      && candidate.toolCallId === anchorWindow.afterToolCallId
    ));
    if (spineIndex >= 0) start = spineIndex + 1;
  }
  if (anchorWindow?.beforeToolCallId) {
    const limitIndex = blocks.findIndex((candidate) => (
      candidate.kind === 'tool'
      && candidate.toolCallId === anchorWindow.beforeToolCallId
    ));
    if (limitIndex >= 0) end = Math.min(end, Math.max(start, limitIndex));
  }
  if (start >= end) {
    /* The window brackets no content (one tool row bounding both sides, or
     * adjacent tool rows): round numbers cannot order the rows sharing it,
     * so keep timeline order by appending after the rows already there. */
    let index = start;
    while (index < blocks.length && blocks[index].kind === 'activity-event') {
      index += 1;
    }
    return index;
  }
  const round = entry.llmRound;
  if (round != null) {
    for (let index = end - 1; index >= start; index -= 1) {
      if (activityBlockLlmRound(blocks[index]) === round) return index + 1;
    }
    for (let index = end - 1; index >= start; index -= 1) {
      const candidateRound = activityBlockLlmRound(blocks[index]);
      if (candidateRound != null && candidateRound < round) return index + 1;
    }
    return start;
  }
  /* Unanchored diagnostics happened before any model request (preflight
   * schema isolation) or the turn never produced round-anchored content
   * (terminal error): place them before the first anchored content block,
   * after any earlier unanchored rows, falling back to the window end. */
  for (let index = start; index < end; index += 1) {
    const candidate = blocks[index];
    if (candidate.kind === 'text' || candidate.kind === 'thinking'
        || candidate.kind === 'tool'
        || (candidate.kind === 'activity-event'
          && candidate.value.llmRound != null)) {
      return index;
    }
  }
  return end;
}

/**
 * Reconcile thinking-block activity with the turn lifecycle.
 *
 * ``segment.terminal`` marks the TERMINAL round's reasoning accumulator (a
 * channel-projection concern — see ``derive_thinking``); it does NOT mean
 * "this reasoning text may still grow". Inter-round thinking segments are
 * stamped closed onto their tool-round batch, so on a settled turn every
 * thinking block is complete. On a live turn a thinking block stays active
 * only while it remains the tail of the activity stream: once narration or
 * a tool round lands after it, that reasoning round is finished and the
 * block closes immediately (previously it stayed on "Thinking…" until the
 * NEXT reasoning round started or the whole turn settled — a full round
 * late). Diagnostic/injection rows anchored after it do not close it. The
 * renderer keys its label / disclosure default / pulse off this flag.
 */
function settleThinkingActivity(
  turn: TurnRecord,
  blocks: ConversationBlockViewModel[],
): void {
  const settled = turn.status === 'completed' || turn.status === 'interrupted'
    || turn.status === 'truncated' || turn.status === 'failed';
  let laterContent = false;
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block.kind === 'thinking') {
      block.terminal = settled || laterContent;
    }
    if (block.kind === 'thinking' || block.kind === 'text'
        || block.kind === 'tool') {
      laterContent = true;
    }
  }
}

export function selectTurnBlocks(
  turn: TurnRecord,
  diagnostics: ConversationViewModelDiagnostics = {},
  translationMode: TranslationDisplayMode = turn.actor === 'human'
    ? 'original' : 'translated',
  translationActivity?: TranslationActivity,
): ReadonlyArray<ConversationBlockViewModel> {
  const segments = turn.projection.segments ?? [];
  const claims = new Map<string, number>();
  const blocks = segments.flatMap((segment, index) => {
    if ((segment.type === 'tool_use' && segment.name === 'execute_tools')
        || (turn.projection.compaction && segment.type === 'text'
          && (segment.terminal || segment.deliverable))) return [];
    const block = blockFromSegment(
      turn, segment, index, claims, diagnostics, translationMode,
      translationActivity,
    );
    /* Empty stream placeholders have no visual authority. Live progress is a
     * dedicated block, so retaining blank prose/thinking here only creates an
     * empty body between the header and the status surface. */
    if ((block.kind === 'text' || block.kind === 'thinking')
        && !block.displayMarkdown.trim()) return [];
    return [block];
  });
  addProjectionSidecarBlocks(
    turn, blocks, claims, diagnostics, translationMode, translationActivity,
  );
  settleThinkingActivity(turn, blocks);
  const transient = transientTurnPresentation(turn);
  if (transient) {
    blocks.push({
      blockId: claimedBlockId(
        turn.turnId, 'live-status', 'contract', claims, diagnostics,
      ),
      kind: 'live-status',
      identitySource: 'contract',
      source: transient,
      value: transient,
    });
  }
  return blocks;
}

function metadataFrom(
  projection: TurnProjection,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
  preferredTranslationMode: TranslationDisplayMode,
  activity?: TranslationActivity,
): TurnMetadataViewModel {
  const hasFallback = Boolean(
    projection.fallbackModel || projection.fallbackFrom
    || projection.fallbackReason || projection.fallbackKind,
  );
  const fallbackInTimeline = (projection.activityTimeline?.entries ?? [])
    .some((entry) => entry.status === 'switched');
  const translationAvailable = blocks.some((block) => (
    (block.kind === 'text' || block.kind === 'thinking')
      && Boolean(block.translatedMarkdown)
  ) || (
    block.kind === 'proposed-plan' && Boolean(block.translatedMarkdown)
  ));
  const translation = projection.translation as TurnTranslation | undefined;
  return {
    ...(projection.model ? { model: projection.model } : {}),
    ...(projection.preset ? { preset: projection.preset } : {}),
    ...((projection.providerId || projection.provider_id)
      ? { providerId: projection.providerId || projection.provider_id } : {}),
    ...(projection.thinkingDepth != null
      ? { thinkingDepth: projection.thinkingDepth } : {}),
    ...(projection.usage ? { usage: projection.usage } : {}),
    ...(projection.lastRoundUsage
      ? { lastRoundUsage: projection.lastRoundUsage } : {}),
    ...(projection.modifiedFiles != null
      ? { modifiedFiles: projection.modifiedFiles } : {}),
    ...(projection.modifiedFileList
      ? { modifiedFileList: projection.modifiedFileList } : {}),
    ...(projection.todoState ? { todoState: projection.todoState } : {}),
    ...(projection.orchestration
      ? { orchestration: projection.orchestration } : {}),
    ...(projection.origin ? { origin: projection.origin } : {}),
    ...(hasFallback ? { fallback: {
      ...(projection.fallbackModel ? { model: projection.fallbackModel } : {}),
      ...(projection.fallbackFrom ? { from: projection.fallbackFrom } : {}),
      ...(projection.fallbackReason ? { reason: projection.fallbackReason } : {}),
      ...(projection.fallbackKind ? { kind: projection.fallbackKind } : {}),
    } } : {}),
    fallbackInTimeline,
    translation: {
      completed: translation?.status === 'completed'
        || translation?.status === 'skipped',
      available: translationAvailable,
      displayMode: translationAvailable ? preferredTranslationMode : 'original',
      pending: activity?.status === 'pending'
        || (translation?.status === 'pending' && !translationAvailable),
      ...(translation?.skippedReason
        ? { skippedReason: translation.skippedReason } : {}),
      ...(translation?.model ? { model: translation.model } : {}),
      ...((activity?.error ?? translation?.error) == null
        ? {} : { error: activity?.error ?? translation?.error }),
    },
  };
}

function actionsFor(
  actor: TurnRecord['actor'],
  laneId: string,
  commandPending: string | null,
  finish: TurnFinishPresentation | null,
  translation: TurnMetadataViewModel['translation'],
  status: TurnStatus,
  taskId: string | undefined,
  requestInspectorEnabled: boolean,
): ConversationTurnActionViewModel[] {
  const disabled = Boolean(commandPending);
  if (laneId !== 'main') {
    return [{ action: 'copy', disabled: false }];
  }
  if (status === 'pending' || status === 'running') return [];
  const actions: ConversationTurnActionViewModel[] = [
    { action: 'copy', disabled: false },
    ...(requestInspectorEnabled && taskId
      ? [{ action: 'inspect' as const, operation: taskId, disabled: false }]
      : []),
    { action: 'edit', disabled },
  ];
  if (actor === 'human') {
    actions.push({ action: 'regenerate', disabled });
  } else {
    const resume = finish?.resumeOptions?.[0];
    if (resume?.operation) {
      actions.push({ action: 'resume', operation: resume.operation, disabled });
    }
    actions.push(
      {
        action: 'translate',
        ...(translation.available ? {
          operation: translation.displayMode === 'translated'
            ? 'show-original' : 'show-translated',
        } : {}),
        disabled,
      },
      { action: 'export', disabled: false },
      { action: 'branch', disabled },
    );
  }
  actions.push({ action: 'delete', disabled });
  return actions;
}

interface LaneOwner {
  parentTurnId: string;
  descriptor?: BranchLaneDescriptor;
  order: number;
}

const AUTOPILOT_NOTICE_REASONS = new Set<AutopilotRunNoticeReason>([
  'yielded_to_human',
  'aborted_mid_vu',
  'superseded',
  'budget_exhausted',
  'no_progress',
  'stuck',
]);

function autopilotNoticeReason(value: unknown): AutopilotRunNoticeReason | null {
  return typeof value === 'string'
    && AUTOPILOT_NOTICE_REASONS.has(value as AutopilotRunNoticeReason)
    ? value as AutopilotRunNoticeReason : null;
}

function liveAttemptPresentation(value: unknown): TransientTurnPresentation {
  const phase = value && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : {};
  const stringValue = (field: string): string => (
    typeof phase[field] === 'string' ? phase[field] as string : ''
  );
  const numberValue = (field: string): number | undefined => {
    const parsed = Number(phase[field]);
    return Number.isFinite(parsed) ? parsed : undefined;
  };
  const stringArray = (field: string): string[] | undefined => (
    Array.isArray(phase[field])
      ? (phase[field] as unknown[]).filter(
        (item): item is string => typeof item === 'string',
      ) : undefined
  );
  const detailArgs = phase.detailArgs && typeof phase.detailArgs === 'object'
      && !Array.isArray(phase.detailArgs)
    ? Object.fromEntries(Object.entries(phase.detailArgs as UnknownRecord)
      .filter((entry): entry is [string, string | number] => (
        typeof entry[1] === 'string' || typeof entry[1] === 'number'
      ))) : undefined;
  return {
    kind: 'attempt',
    phase: stringValue('phase') || 'waiting',
    ...(numberValue('seq') == null ? {} : { seq: numberValue('seq') }),
    label: stringValue('label') || 'Waiting for the agent…',
    detail: stringValue('detail'),
    ...(stringValue('detailKey') ? { detailKey: stringValue('detailKey') } : {}),
    ...(detailArgs && Object.keys(detailArgs).length ? { detailArgs } : {}),
    ...(stringArray('tools')?.length ? { tools: stringArray('tools') } : {}),
    ...(stringValue('toolContext')
      ? { toolContext: stringValue('toolContext') } : {}),
    ...(stringArray('toolContextTools')?.length
      ? { toolContextTools: stringArray('toolContextTools') } : {}),
    ...(numberValue('attempt') == null
      ? {} : { attempt: numberValue('attempt') }),
    ...(numberValue('statusCode') == null
      ? {} : { statusCode: numberValue('statusCode') }),
    ...(stringValue('model') ? { model: stringValue('model') } : {}),
    ...(numberValue('thinkingLength') == null
      ? {} : { thinkingLength: numberValue('thinkingLength') }),
  };
}

/**
 * True when the live-status row is the bare default "waiting" placeholder
 * (no phase frame has arrived from the attempt) AND a thinking/answer block
 * is still streaming at the turn tail — in that state the streaming block
 * itself is the honest live surface, so the placeholder is pure noise.
 */
function waitingPlaceholderRedundant(
  liveStatus: TransientTurnPresentation,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
): boolean {
  if (liveStatus.phase !== 'waiting') return false;
  if (liveStatus.detail || liveStatus.detailKey || liveStatus.model
      || liveStatus.toolContext || liveStatus.tools?.length) return false;
  const tail = blocks[blocks.length - 1];
  if (!tail) return false;
  if (tail.kind !== 'thinking' && tail.kind !== 'text') return false;
  return !tail.terminal;
}

/**
 * Join backend run conclusions to the last Turn they describe.
 *
 * `_autopilotRunId` identifies persisted virtual-user participants. The run
 * tail also owns following non-human assistant Turns until a real human Turn
 * or another explicitly stamped run starts. This replaces the old DOM sibling
 * scan with a pure, stable-id join over the authoritative main lane.
 */
function selectAutopilotRunNotices(
  state: TurnState,
  presentation: ConversationPresentationState,
): ReadonlyMap<string, ReadonlyArray<AutopilotRunNoticeBlockViewModel>> {
  const summaries = presentation.autopilotSummaries;
  if (!summaries) return new Map();
  const turns = (state.laneOrder.main ?? []).flatMap((turnId) => {
    const turn = state.turnsById[turnId];
    return turn ? [turn] : [];
  });
  const lastStampedIndexByRun = new Map<string, number>();
  turns.forEach((turn, index) => {
    const runId = turn.projection._autopilotRunId;
    if (runId) lastStampedIndexByRun.set(runId, index);
  });
  const noticesByTurn = new Map<string, AutopilotRunNoticeBlockViewModel[]>();
  for (const [runId, stampedIndex] of lastStampedIndexByRun) {
    const source = summaries[runId];
    if (!source || typeof source !== 'object' || Array.isArray(source)
        || source.status !== 'concluded') continue;
    const reason = autopilotNoticeReason(source.reason);
    if (!reason) continue;
    let targetIndex = stampedIndex;
    for (let index = stampedIndex + 1; index < turns.length; index += 1) {
      const candidate = turns[index];
      if (candidate.actor === 'human') break;
      const candidateRunId = candidate.projection._autopilotRunId;
      if (candidateRunId && candidateRunId !== runId) break;
      targetIndex = index;
    }
    const target = turns[targetIndex];
    if (!target) continue;
    const content = source.content == null ? '' : String(source.content);
    const notice: AutopilotRunNoticeBlockViewModel = {
      blockId: `${target.turnId}:autopilot-run-notice:${runId}`,
      kind: 'autopilot-run-notice',
      identitySource: 'contract',
      source,
      value: {
        runId,
        reason,
        unsent: Boolean(source.unsent && content),
        ...(content ? { content } : {}),
      },
    };
    const existing = noticesByTurn.get(target.turnId) ?? [];
    existing.push(notice);
    noticesByTurn.set(target.turnId, existing);
  }
  return noticesByTurn;
}

/** Select the complete lane tree without relying on message array positions. */
export function selectConversationViewModel(
  state: TurnState,
  diagnostics: ConversationViewModelDiagnostics = {},
  presentation: ConversationPresentationState = {},
): ConversationViewModel {
  const autopilotNoticesByTurn = selectAutopilotRunNotices(state, presentation);
  const latestAttemptByTurn = new Map<string, {
    taskId: string;
    createdAt: number;
  }>();
  for (const attempt of Object.values(state.attemptsById)) {
    if (!attempt?.turnId || !attempt.taskId) continue;
    const current = latestAttemptByTurn.get(attempt.turnId);
    const createdAt = Number(attempt.createdAt || 0);
    if (!current || createdAt >= current.createdAt) {
      latestAttemptByTurn.set(attempt.turnId, {
        taskId: attempt.taskId,
        createdAt,
      });
    }
  }
  const mainLiveTurnId = [...(state.laneOrder.main ?? [])].reverse()
    .find((turnId) => {
      const turn = state.turnsById[turnId];
      return turn?.status === 'pending' || turn?.status === 'running';
    }) ?? null;
  const laneOwners = new Map<string, LaneOwner>();
  let descriptorOrder = 0;
  for (const turn of Object.values(state.turnsById)) {
    if (!turn) continue;
    for (const descriptor of turn.projection._branchLanes ?? []) {
      if (!descriptor.laneId || laneOwners.has(descriptor.laneId)) continue;
      laneOwners.set(descriptor.laneId, {
        parentTurnId: turn.turnId,
        descriptor,
        order: descriptorOrder++,
      });
    }
  }
  for (const [laneId, turnIds] of Object.entries(state.laneOrder)) {
    if (laneId === 'main' || laneOwners.has(laneId)) continue;
    const first = (turnIds ?? []).map((turnId) => state.turnsById[turnId])
      .find((turn): turn is TurnRecord => Boolean(turn));
    if (first?.parentTurnId) {
      laneOwners.set(laneId, {
        parentTurnId: first.parentTurnId,
        order: descriptorOrder++,
      });
    }
  }
  const childrenByTurn = new Map<string, string[]>();
  for (const [laneId, owner] of laneOwners) {
    const lanes = childrenByTurn.get(owner.parentTurnId) ?? [];
    lanes.push(laneId);
    childrenByTurn.set(owner.parentTurnId, lanes);
  }
  for (const lanes of childrenByTurn.values()) {
    lanes.sort((left, right) => (
      (laneOwners.get(left)?.order ?? 0) - (laneOwners.get(right)?.order ?? 0)
      || left.localeCompare(right)
    ));
  }

  const selectedLanes = new Set<string>();
  const selectLane = (
    laneId: string,
    stack: ReadonlySet<string>,
  ): ConversationLaneViewModel => {
    if (stack.has(laneId)) {
      diagnostics.onLaneCycle?.(laneId);
      return {
        laneId,
        parentTurnId: laneOwners.get(laneId)?.parentTurnId ?? null,
        title: laneOwners.get(laneId)?.descriptor?.title ?? 'Branch',
        kind: 'cycle-rejected',
        expanded: false,
        live: false,
        humanTurnCount: 0,
        turns: [],
      };
    }
    selectedLanes.add(laneId);
    const nextStack = new Set(stack);
    nextStack.add(laneId);
    const owner = laneOwners.get(laneId);
    const descriptor = owner?.descriptor;
    const turns = (state.laneOrder[laneId] ?? []).flatMap(
      (turnId): ConversationTurnViewModel[] => {
        const turn = state.turnsById[turnId];
        if (!turn) return [];
        const branches = (childrenByTurn.get(turn.turnId) ?? [])
          .map((childLaneId) => selectLane(childLaneId, nextStack));
        const role = roleFor(turn);
        const taskId = latestAttemptByTurn.get(turn.turnId)?.taskId;
        const commandPending = state.commandPending[turn.turnId] ?? null;
        const finish = presentTurnFinish(turn);
        const preferredTranslationMode = presentation.translationModeByTurn
          ?.get(turn.turnId) ?? (turn.actor === 'human' ? 'original' : 'translated');
        const translationActivity = presentation.translationActivityByTurn
          ?.get(turn.turnId);
        const blocks = [
          ...selectTurnBlocks(
            turn, diagnostics, preferredTranslationMode, translationActivity,
          ),
          ...(autopilotNoticesByTurn.get(turn.turnId) ?? []),
        ];
        const artifacts = presentation.artifactsByTurn?.get(turn.turnId);
        if (artifacts?.length) {
          blocks.push({
            blockId: `${turn.turnId}:artifacts`,
            kind: 'artifacts',
            identitySource: 'contract',
            source: artifacts,
            artifacts,
          });
        }
        if (turn.turnId === mainLiveTurnId
            && !blocks.some((block) => block.kind === 'live-status')) {
          const phaseSource = state.livePhase && typeof state.livePhase === 'object'
            ? state.livePhase as object : turn;
          const liveStatus = liveAttemptPresentation(state.livePhase);
          /* A withheld-push wedge means no newer frame can arrive, so any
           * livePhase on record is stale. Present the honest storage-wedge
           * status instead of the misleading generic waiting placeholder
           * (2026-08-26: a 4-minute storage wedge read as "submitted to the
           * agent worker…" while the task was actually retrying writes). */
          if (state.pushWithheld) {
            liveStatus.phase = 'storage_wedged';
            liveStatus.label = '';
            liveStatus.detail = '';
            delete liveStatus.detailKey;
            delete liveStatus.detailArgs;
            delete liveStatus.tools;
            delete liveStatus.toolContext;
            delete liveStatus.toolContextTools;
          }
          /* The bare default 'waiting' placeholder means "no phase frame
           * has arrived". While a thinking or answer block is still
           * streaming at the tail, that block IS the live status — the
           * placeholder beneath it only restates a stall that is not
           * happening. Real phases (waiting_model / retrying / tool_exec /…)
           * carry detail/tools/model and never match; the wedge override
           * above must always render. */
          if (!waitingPlaceholderRedundant(liveStatus, blocks)) {
            blocks.push({
              blockId: 'live-status',
              kind: 'live-status',
              identitySource: 'contract',
              source: phaseSource,
              value: liveStatus,
            });
          }
        }
        const metadata = metadataFrom(
          turn.projection, blocks, preferredTranslationMode,
          translationActivity,
        );
        return [{
          turnId: turn.turnId,
          laneId: turn.laneId || laneId,
          parentTurnId: turn.parentTurnId ?? null,
          ordinal: Number(turn.ordinal || 0),
          actor: turn.actor,
          role,
          kind: turn.kind,
          status: turn.status,
          attemptId: turn.currentAttemptId ?? null,
          ...(taskId ? { taskId } : {}),
          projectionRevision: Number(turn.projectionRevision || 0),
          commandPending,
          finish,
          actions: actionsFor(
            turn.actor, turn.laneId || laneId, commandPending, finish,
            metadata.translation, turn.status, taskId,
            Boolean(presentation.requestInspectorEnabled),
          ),
          blocks,
          branches,
          metadata,
          source: turn,
        }];
      },
    );
    return {
      laneId,
      parentTurnId: owner?.parentTurnId ?? null,
      title: laneId === 'main' ? 'Conversation' : descriptor?.title ?? 'Branch',
      ...(descriptor?.label ? { label: descriptor.label } : {}),
      ...(descriptor?.icon ? { icon: descriptor.icon } : {}),
      kind: laneId === 'main' ? 'main' : descriptor?.kind ?? 'branch',
      ...(descriptor?.anchorText ? { anchorText: descriptor.anchorText } : {}),
      ...(descriptor?.parentSelection
        ? { parentSelection: descriptor.parentSelection } : {}),
      expanded: laneId === 'main'
        || presentation.expandedBranchLaneId === laneId,
      live: turns.some((turn) => turn.status === 'pending' || turn.status === 'running'),
      humanTurnCount: turns.filter((turn) => turn.actor === 'human').length,
      turns,
    };
  };

  const mainLane = selectLane('main', new Set());
  const orphanLanes = Object.keys(state.laneOrder)
    .filter((laneId) => laneId !== 'main' && !selectedLanes.has(laneId))
    .sort()
    .map((laneId) => selectLane(laneId, new Set()));
  const queue = state.queueItems.filter((item) => item.kind !== 'autopilot')
    .sort((left, right) => Number(left.position || 0) - Number(right.position || 0))
    .map((item) => ({
      queueId: item.queueId,
      position: item.position,
      kind: item.kind,
      text: item.text,
      source: item,
    }));
  const decisionLaneId = presentation.expandedBranchLaneId || 'main';
  const lanesToVisit = [mainLane, ...orphanLanes];
  let decisionLane: ConversationLaneViewModel | undefined;
  while (lanesToVisit.length > 0 && !decisionLane) {
    const candidate = lanesToVisit.shift();
    if (!candidate) continue;
    if (candidate.laneId === decisionLaneId) {
      decisionLane = candidate;
      break;
    }
    for (const turn of candidate.turns) lanesToVisit.push(...turn.branches);
  }
  const tail = decisionLane?.turns.at(-1);
  const proposedPlan = tail?.source.projection.proposedPlan;
  const laneCanDecide = decisionLane?.laneId === 'main'
    ? queue.length === 0 : Boolean(decisionLane);
  const planDecision = tail && proposedPlan && laneCanDecide
      && !decisionLane?.live
      && tail.status === 'completed'
      && (tail.actor === 'assistant' || tail.actor === 'planner')
    ? {
      sourceTurnId: tail.turnId,
      sourceProjectionRevision: tail.projectionRevision,
      planId: proposedPlan.planId,
      pending: Boolean(tail.commandPending),
    } : null;
  return {
    conversationId: state.conversationId,
    conversationRevision: state.conversationRevision,
    transport: state.transport,
    mainLane,
    orphanLanes,
    queue,
    planDecision,
  };
}
