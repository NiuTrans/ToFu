import { createTaskTimePresentations } from './task-time-presentations';
import type { AutomationManagementPresentationPage } from '../../api/automation.generated';
/** Application controller binding authoritative TurnState to ConversationSurface.
 *
 * Responsibility: scheduling, presentation-local state, scroll preservation,
 * and typed intent forwarding. It never reads or adopts legacy message HTML.
 */
import type { TurnState, TurnStateUpdate } from '../domain/turn-store';
import {
  selectConversationViewModel,
  createConversationViewModelSelector,
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
  nativeRenderers?: ConversationSurfaceRenderers;
  loadTaskTimePresentations?(conversationId: string, attemptId: string, signal: AbortSignal): Promise<AutomationManagementPresentationPage>;
  taskTimePresentationError?(error: unknown): void;
  onIntent?(intent: ConversationIntent): void | Promise<unknown>;
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
  onConversationMounted?(conversation: ConversationDocument): void;
  afterConversationCommit?(
    conversation: ConversationDocument,
    state: TurnState,
    force: boolean,
    viewModel: ReturnType<typeof selectConversationViewModel>,
    mountedTurnIds: ReadonlyArray<string>,
  ): void;
}

export interface ConversationSurfaceController {
  invalidateTaskTimePresentations(conversationId: string): void;
  ownsConversation(conversationId: string): boolean;
  render(
    conversation: ConversationDocument,
    state: TurnState,
    context?: { force?: boolean; update?: TurnStateUpdate },
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
  isNearLatest(threshold?: number): boolean;
  followLatest(options?: { force?: boolean }): void;

  revealTurn(conversationId: string, turnId: string): boolean;
  disposeConversation(conversationId: string): void;
  dispose(): void;
}

/** Bind one lifecycle-scoped surface to the active conversation session. */
export function createConversationSurfaceController(
  host: ConversationSurfaceHost,
): ConversationSurfaceController {
  const selector = createConversationViewModelSelector();
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
  let scheduled: { cancel?: () => void } | null = null;
  let disposed = false;
  let surfaceOwnsScroll = false;

  const taskTimes = host.loadTaskTimePresentations ? createTaskTimePresentations({
    load: host.loadTaskTimePresentations,
    changed: () => repaintPresentation(),
    failed: host.taskTimePresentationError,
  }) : null;
  const refreshTaskTimes = (force = false, turnIds = surface?.mountedTurnIds ?? []): void => {
    if (!taskTimes || !currentConversation || !currentState
      || disposed || !host.isActive(currentConversation.id)) return;
    const targets: string[] = [];
    for (const turnId of turnIds) {
      const turn = currentState.turnsById[turnId];
      const attempt = turn?.currentAttemptId ? currentState.attemptsById[turn.currentAttemptId] : null;
      if (attempt?.startedAt && turn?.actor !== 'human') targets.push(attempt.attemptId);
    }
    taskTimes.refresh(currentConversation.id, targets, force);
  };

  const disposeSurface = (): void => {
    selector.dispose();
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

  const cancelPaint = (): void => {
    const previous = scheduled;
    scheduled = null;
    try { previous?.cancel?.(); }
    catch (error) { console.warn('[ConversationSurface] render cancellation failed', error); }
  };
  const schedulePaint = (): void => {
    if (scheduled || disposed) return;
    const frame: { cancel?: () => void } = {};
    scheduled = frame;
    try {
      frame.cancel = host.schedule(() => {
        // Cancellation can race delivery; the identity also fences a later
        // conversation's pending paint. Synchronous clocks are supported.
        if (disposed || scheduled !== frame) return;
        scheduled = null;
        flush();
      });
    } catch (error) {
      if (scheduled === frame) scheduled = null;
      throw error;
    }
  };
  const repaintPresentation = (immediate = false): void => {
    if (!surface || !currentConversation || !currentState
      || disposed || !host.isActive(currentConversation.id)) return;
    pending ??= { conversation: currentConversation, state: currentState, force: true };
    pending.force = true;
    // Explicit disclosure/display intents commit before the next action can
    // read their DOM. Async presentation receipts still coalesce with the graph.
    if (immediate) flush();
    else schedulePaint();
  };

  const ensureSurface = (
    container: HTMLElement,
    conversation: ConversationDocument,
  ): ConversationSurface => {
    if (surface && surface.root.parentElement === container
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
      onCommit(turnIds) { refreshTaskTimes(false, turnIds); },
      onIntent(intent) {
        if (intent.type === 'toggle-branch' && intent.laneId) {
          const opening = expandedBranchLaneId !== intent.laneId;
          expandedBranchLaneId = opening ? intent.laneId : null;
          host.onIntent?.({
            ...intent,
            operation: opening ? 'open' : 'close',
          });
          repaintPresentation(true);
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
            repaintPresentation(true);
            return;
          }
          if (intent.type === 'copy' || intent.type === 'copy-block') {
            host.onIntent?.({ ...intent, operation: `copy-${mode}` });
            return;
          }
        }
        return host.onIntent?.(intent);
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
    return surface;
  };

  const flush = (): void => {
    cancelPaint();
    const frame = pending;
    pending = null;
    if (!frame || disposed || !host.isActive(frame.conversation.id)) return;
    const container = host.getContainer();
    if (!container) return;
    const previousSurface = surface;
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
    const viewModel = selector.select(
      frame.state, {}, presentationState(),
    );
    activeSurface.render(viewModel);
    // A forced paint is not evidence that accepted automation output changed.
    // Only the explicit invalidation port requests a reread.
    if (useLegacyScroll) host.restoreScroll?.(scroll);
    if (previousSurface !== activeSurface) host.onConversationMounted?.(frame.conversation);
    host.afterConversationCommit?.(
      frame.conversation, frame.state, frame.force, viewModel, activeSurface.mountedTurnIds,
    );
  };

  return {
    invalidateTaskTimePresentations(conversationId) {
      if (!disposed && conversationId === surfaceConversationId) refreshTaskTimes(true);
    },
    ownsConversation(conversationId) {
      return !disposed && host.isActive(conversationId) && surfaceConversationId === conversationId;
    },
    render(conversation, state, context = {}) {
      if (disposed || conversation.id !== state.conversationId
        || !host.isActive(conversation.id) || !host.getContainer()) {
        return false;
      }
      selector.observe(state, context.update);
      pending = { conversation, state, force: Boolean(context.force)
        || (pending?.conversation.id === conversation.id && pending.force) };
      schedulePaint();
      return true;
    },
    setTranslationActivity(conversationId, turnId, activity) {
      if (disposed || !host.isActive(conversationId)
        || !turnId || conversationId !== surfaceConversationId) return;
      if (activity) {
        const previous = translationActivityByTurn.get(turnId);
        const next = mergeTranslationActivity(previous, activity);
        translationActivityByTurn.set(turnId, next);
      }
      else translationActivityByTurn.delete(turnId);
      // Use the existing graph paint scheduler. A burst merges every key but
      // derives/renders the combined preview only for the scheduled frame.
      repaintPresentation();
    },
    setExpandedBranchLane(conversationId, laneId) {
      if (disposed || !host.isActive(conversationId) || conversationId !== surfaceConversationId
          || expandedBranchLaneId === laneId) return;
      expandedBranchLaneId = laneId;
      repaintPresentation();
    },
    setArtifacts(conversationId, nextArtifactsByTurn) {
      if (disposed || !host.isActive(conversationId) || conversationId !== surfaceConversationId) return;
      artifactsByTurn.clear();
      for (const [turnId, artifacts] of nextArtifactsByTurn) {
        if (turnId && artifacts.length) artifactsByTurn.set(turnId, artifacts);
      }
      repaintPresentation();
    },
    revealTurn(conversationId, turnId) {
      if (disposed || !host.isActive(conversationId)) return false;
      if (pending?.conversation.id === conversationId) {
        flush();
      }
      return surfaceConversationId === conversationId && Boolean(surface?.revealTurn(turnId));
    },
    isNearLatest(threshold) {
      return !surface || !host.isActive(surfaceConversationId) || surface.isNearLatest(threshold);
    },
    followLatest(intent) {
      if (disposed) return;
      if (intent?.force !== false && pending && host.isActive(pending.conversation.id)) flush();
      if (!surface || !host.isActive(surfaceConversationId)) return;
      if (surfaceOwnsScroll) surface.followLatest(intent);
      else if (intent?.force !== false) host.followLatest?.();
    },
    disposeConversation(conversationId) {
      if (pending?.conversation.id === conversationId) { pending = null; cancelPaint(); }
      if (surfaceConversationId === conversationId) disposeSurface();
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      pending = null;
      cancelPaint();
      disposeSurface();
    },
  };
}
