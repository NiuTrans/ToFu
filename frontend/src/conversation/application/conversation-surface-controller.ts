import { createTaskTimePresentations } from './task-time-presentations';
import type { AutomationManagementPresentationPage } from '../../api/automation.generated';
/** Application controller binding authoritative TurnState to ConversationSurface.
 *
 * Responsibility: scheduling, presentation-local state, scroll preservation,
 * and typed intent forwarding. It never reads or adopts legacy message HTML.
 */
import type { TurnState } from '../domain/turn-store';
import {
  selectConversationViewModel,
  type ConversationArtifactViewModel,
  type ConversationPresentationState,
  type TranslationActivity,
  type TranslationDisplayMode,
} from '../presentation/conversation-view-model';
import {
  createConversationSurface,
  type ConversationSurface,
  type ConversationSurfaceRenderers,
  type ConversationIntent,
  type ConversationViewportOptions,
  type ConversationWindowOptions,
  type ScrollAnchorPort,
} from '../ui/conversation-surface';

import { mergeTranslationActivity } from './translation-activity';
import type { RoundOutlineTranslator } from '../ui/round-outline';

export interface ConversationDocument extends Record<string, unknown> {
  id: string;
  autopilotSummaries?: Readonly<
    Record<string, Readonly<Record<string, unknown>>>
  >;
}

export interface ConversationSurfaceHost {
  isActive(conversationId: string): boolean;
  getContainer(): HTMLElement | null;
  schedule(render: () => void): () => void;
  nativeRenderers?: Pick<
    ConversationSurfaceRenderers,
    'renderBlock' | 'renderTurnHeader' | 'renderTurnActions'
      | 'renderTurnFooter' | 'renderPlanDecision'
      | 'renderLaneHeader' | 'renderQueueItem'
  >;
  loadTaskTimePresentations?(conversationId: string, attemptId: string, signal: AbortSignal): Promise<AutomationManagementPresentationPage>;
  taskTimePresentationError?(error: unknown): void;
  onIntent?(intent: ConversationIntent): void;
  requestInspectorEnabled?(): boolean;
  /** Synchronous client cost-cache signature per settled turn lacking an
   *  authoritative projection cost. The async batch fill mutates no Turn
   *  fact; the signature lets the surface footer compare see it. */
  costSignatureSnapshot?(state: TurnState): ReadonlyMap<string, string>;
  /** Localized label for the inline sync-degradation banner. */
  transportNoticeLabel?(
    state: 'recovering' | 'degraded' | 'offline',
  ): string;
  scrollAnchor?: ScrollAnchorPort;
  getScrollViewport?(): HTMLElement | null;
  viewportOptions?: ConversationViewportOptions;
  windowing?: ConversationWindowOptions;

  roundOutlineLabel?: RoundOutlineTranslator;
  captureScroll?(): unknown;
  restoreScroll?(snapshot: unknown): void;
  followLatest?(): void;
  afterConversationCommit?(
    conversation: ConversationDocument,
    state: TurnState,
    force: boolean,
    viewModel: ReturnType<typeof selectConversationViewModel>,
  ): void;
}

export interface ConversationSurfaceController {
  invalidateTaskTimePresentations(conversationId: string): void;
  ownsConversation(conversationId: string): boolean;
  render(
    conversation: ConversationDocument,
    state: TurnState,
    context?: { force?: boolean },
  ): boolean;
  setTranslationActivity(
    conversationId: string,
    turnId: string,
    activity: TranslationActivity | null,
  ): void;
  setExpandedBranchLane(conversationId: string, laneId: string | null): void;
  setArtifacts(
    conversationId: string,
    artifactsByTurn: ReadonlyMap<
      string,
      ReadonlyArray<ConversationArtifactViewModel>
    >,
  ): void;
  followLatest(): void;

  revealTurn(conversationId: string, turnId: string): boolean;
  disposeConversation(conversationId: string): void;
  dispose(): void;
}

/** Bind one lifecycle-scoped surface to the active conversation session. */
export function createConversationSurfaceController(
  host: ConversationSurfaceHost,
): ConversationSurfaceController {
  let surface: ConversationSurface | null = null;
  let surfaceConversationId = '';
  let currentConversation: ConversationDocument | null = null;
  let currentState: TurnState | null = null;
  const translationModeByTurn = new Map<string, TranslationDisplayMode>();
  const translationActivityByTurn = new Map<string, TranslationActivity>();
  const artifactsByTurn = new Map<
    string,
    ReadonlyArray<ConversationArtifactViewModel>
  >();
  let expandedBranchLaneId: string | null = null;
  let pending: {
    conversation: ConversationDocument;
    state: TurnState;
    force: boolean;
  } | null = null;
  let cancelScheduled: (() => void) | null = null;
  let disposed = false;
  let surfaceOwnsScroll = false;

  const taskTimes = host.loadTaskTimePresentations ? createTaskTimePresentations({
    load: host.loadTaskTimePresentations,
    changed: () => repaintPresentation(),
    failed: host.taskTimePresentationError,
  }) : null;
  let taskTimeObserver: MutationObserver | null = null;
  const refreshTaskTimes = (force = false): void => {
    if (!taskTimes || !surface || !currentConversation || !currentState) return;
    const targets: string[] = [];
    for (const node of surface.root.querySelectorAll<HTMLElement>('[data-turn-id]')) {
      const turn = currentState.turnsById[node.dataset.turnId ?? ''];
      const attempt = turn?.currentAttemptId ? currentState.attemptsById[turn.currentAttemptId] : null;
      if (attempt?.startedAt && turn?.actor !== 'human') targets.push(attempt.attemptId);
    }
    taskTimes.refresh(currentConversation.id, targets, force);
  };

  const disposeSurface = (): void => {
    taskTimeObserver?.disconnect(); taskTimeObserver = null;
    taskTimes?.dispose();
    surface?.dispose();
    surface = null;
    surfaceOwnsScroll = false;
    surfaceConversationId = '';
    currentConversation = null;
    currentState = null;
    translationModeByTurn.clear();
    translationActivityByTurn.clear();
    artifactsByTurn.clear();
    expandedBranchLaneId = null;
  };

  const presentationState = (): ConversationPresentationState => ({
    ...(taskTimes ? { taskTimePresentationsByAttempt: taskTimes.values } : {}),
    translationModeByTurn,
    translationActivityByTurn,
    artifactsByTurn,
    expandedBranchLaneId,
    ...(host.costSignatureSnapshot && currentState
      ? { costSignatureByTurnId: host.costSignatureSnapshot(currentState) }
      : {}),
    requestInspectorEnabled: Boolean(host.requestInspectorEnabled?.()),
    ...(currentConversation?.autopilotSummaries
      ? { autopilotSummaries: currentConversation.autopilotSummaries } : {}),
    syncHealth: (
      currentConversation?._conversationSyncHealth ?? null
    ) as ConversationPresentationState['syncHealth'],
  });

  const turnHasTranslation = (turnId: string): boolean => {
    const projection = currentState?.turnsById[turnId]?.projection;
    if (!projection) return false;
    if (translationActivityByTurn.get(turnId)?.partial) return true;
    if (projection.translatedContent || projection.originalContent) return true;
    return (projection.segments ?? []).some((segment) => (
      (segment.type === 'text' || segment.type === 'thinking')
        && Boolean(segment.translatedText)
    ));
  };

  const repaintPresentation = (): void => {
    if (!surface || !currentConversation || !currentState) return;
    const scroll = surfaceOwnsScroll ? undefined : host.captureScroll?.();
    const viewModel = selectConversationViewModel(
      currentState, {}, presentationState(),
    );
    surface.render(viewModel);
    if (!surfaceOwnsScroll) host.restoreScroll?.(scroll);
    host.afterConversationCommit?.(
      currentConversation, currentState, true, viewModel,
    );
  };

  const ensureSurface = (
    container: HTMLElement,
    conversation: ConversationDocument,
  ): ConversationSurface => {
    if (surface && surface.root.isConnected
        && surfaceConversationId === conversation.id) return surface;
    disposeSurface();
    currentConversation = conversation;
    currentState = currentState?.conversationId === conversation.id
      ? currentState : null;
    surfaceConversationId = conversation.id;
    const scrollViewport = host.getScrollViewport?.() ?? null;
    surfaceOwnsScroll = Boolean(host.scrollAnchor || scrollViewport);
    surface = createConversationSurface(container, {
      ...host.nativeRenderers,
      ...(host.scrollAnchor ? { scrollAnchor: host.scrollAnchor } : {}),
      ...(scrollViewport ? { scrollViewport } : {}),
      ...(host.viewportOptions ? { viewport: host.viewportOptions } : {}),
      ...(host.windowing ? { windowing: host.windowing } : {}),

      ...(host.roundOutlineLabel ? { roundOutlineLabel: host.roundOutlineLabel } : {}),
      ...(host.transportNoticeLabel
        ? { transportNoticeLabel: host.transportNoticeLabel }
        : {}),
      onIntent(intent) {
        if (intent.type === 'toggle-branch' && intent.laneId) {
          const opening = expandedBranchLaneId !== intent.laneId;
          expandedBranchLaneId = opening ? intent.laneId : null;
          host.onIntent?.({
            ...intent,
            operation: opening ? 'open' : 'close',
          });
          repaintPresentation();
          return;
        }
        if (intent.type === 'delete-branch'
            && intent.laneId === expandedBranchLaneId) {
          expandedBranchLaneId = null;
        }
        if (intent.turnId && turnHasTranslation(intent.turnId)) {
          const turn = currentState?.turnsById[intent.turnId];
          const fallbackMode: TranslationDisplayMode = turn?.actor === 'human'
            ? 'original' : 'translated';
          const mode = translationModeByTurn.get(intent.turnId) ?? fallbackMode;
          if (intent.type === 'translate') {
            const nextMode = intent.operation === 'show-original' ? 'original'
              : (intent.operation === 'show-translated' ? 'translated'
                : (mode === 'translated' ? 'original' : 'translated'));
            translationModeByTurn.set(intent.turnId, nextMode);
            repaintPresentation();
            return;
          }
          if (intent.type === 'copy' || intent.type === 'copy-block') {
            host.onIntent?.({ ...intent, operation: `copy-${mode}` });
            return;
          }
        }
        host.onIntent?.(intent);
      },
      /* Injection-lane thumbnails carry their image source directly; relay it
       * as a preview-image intent so the retained host opens the lightbox
       * without a positional attachments lookup. */
      onPreviewImage(source) {
        host.onIntent?.({
          type: 'preview-image',
          conversationId: currentConversation?.id ?? '',
          previewSource: source,
        });
      },
    });
    if (taskTimes && typeof MutationObserver !== 'undefined') {
      taskTimeObserver = new MutationObserver(() => refreshTaskTimes());
      taskTimeObserver.observe(surface.root, { childList: true, subtree: true });
    }
    return surface;
  };

  const flush = (): void => {
    cancelScheduled = null;
    const frame = pending;
    pending = null;
    if (!frame || disposed || !host.isActive(frame.conversation.id)) return;
    const container = host.getContainer();
    if (!container) return;
    const activeSurface = ensureSurface(container, frame.conversation);
    const useLegacyScroll = !surfaceOwnsScroll;
    const scroll = useLegacyScroll ? host.captureScroll?.() : undefined;
    currentConversation = frame.conversation;
    currentState = frame.state;
    if (expandedBranchLaneId
        && !frame.state.laneOrder[expandedBranchLaneId]) {
      expandedBranchLaneId = null;
    }
    /* The first commit transfers exclusive content ownership to this Surface. */
    for (const child of Array.from(container.children)) {
      if (child !== activeSurface.root) child.remove();
    }
    const viewModel = selectConversationViewModel(
      frame.state, {}, presentationState(),
    );
    activeSurface.render(viewModel);
    // A forced paint is not evidence that accepted automation output changed.
    // Only the explicit invalidation port requests a reread.
    refreshTaskTimes();
    if (useLegacyScroll) host.restoreScroll?.(scroll);
    host.afterConversationCommit?.(
      frame.conversation, frame.state, frame.force, viewModel,
    );
  };

  return {
    invalidateTaskTimePresentations(conversationId) {
      if (!disposed && conversationId === surfaceConversationId) refreshTaskTimes(true);
    },
    ownsConversation(conversationId) {
      return !disposed && surfaceConversationId === conversationId;
    },
    render(conversation, state, context = {}) {
      if (disposed || !host.isActive(conversation.id) || !host.getContainer()) {
        return false;
      }
      pending = { conversation, state, force: Boolean(context.force) };
      if (!cancelScheduled) cancelScheduled = host.schedule(flush);
      return true;
    },
    setTranslationActivity(conversationId, turnId, activity) {
      if (!turnId || conversationId !== surfaceConversationId) return;
      if (activity) {
        const previous = translationActivityByTurn.get(turnId);
        const next = mergeTranslationActivity(previous, activity);
        translationActivityByTurn.set(turnId, next);
      }
      else translationActivityByTurn.delete(turnId);
      if (!surface || !currentConversation || !currentState) return;
      // Use the existing graph paint scheduler. A burst merges every key but
      // derives/renders the combined preview only for the scheduled frame.
      pending ??= { conversation: currentConversation, state: currentState, force: true };
      pending.force = true;
      if (!cancelScheduled) cancelScheduled = host.schedule(flush);
    },
    setExpandedBranchLane(conversationId, laneId) {
      if (conversationId !== surfaceConversationId
          || expandedBranchLaneId === laneId) return;
      expandedBranchLaneId = laneId;
      repaintPresentation();
    },
    setArtifacts(conversationId, nextArtifactsByTurn) {
      if (conversationId !== surfaceConversationId) return;
      artifactsByTurn.clear();
      for (const [turnId, artifacts] of nextArtifactsByTurn) {
        if (turnId && artifacts.length) artifactsByTurn.set(turnId, artifacts);
      }
      repaintPresentation();
    },
    revealTurn(conversationId, turnId) {
      if (disposed || !host.isActive(conversationId)) return false;
      if (pending?.conversation.id === conversationId) {
        cancelScheduled?.();
        flush();
      }
      return surfaceConversationId === conversationId && Boolean(surface?.revealTurn(turnId));
    },
    followLatest() {
      if (surface && surfaceOwnsScroll) surface.followLatest();
      else host.followLatest?.();
    },
    disposeConversation(conversationId) {
      if (pending?.conversation.id === conversationId) pending = null;
      if (surfaceConversationId === conversationId) disposeSurface();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      pending = null;
      cancelScheduled?.();
      cancelScheduled = null;
      disposeSurface();
    },
  };
}
