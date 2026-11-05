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
  type ConversationRetainedTail,
  type ConversationSurface,
  type ConversationSurfaceRenderers,
  type ConversationIntent,
  type ConversationViewportOptions,
  type ConversationWindowOptions,
  type ScrollAnchorPort,
} from '../ui/conversation-surface';

import { mergeTranslationActivity } from './translation-activity';
import type { RoundOutlineTranslator } from '../ui/round-outline';

const RETAINED_TAIL_CONVERSATIONS = 64;

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
  /** Physical active host for bounded presentation reads, not a sync ACK. */
  isMounted(conversationId: string): boolean;
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
  let surfaceContainer: HTMLElement | null = null;
  let currentConversation: ConversationDocument | null = null;
  let currentState: TurnState | null = null;
  const translationModeByTurn = new Map<string, TranslationDisplayMode>();
  /* Running-turn previews arrive once per block and are never re-sent, so
   * they are kept per conversation, mounted or not: a switch must not discard
   * what a later visit would otherwise show in the source language. The
   * settling done/aborted frame or conversation disposal releases them. */
  const translationActivityByConversation = new Map<
    string,
    Map<string, TranslationActivity>
  >();
  const noTranslationActivity: ReadonlyMap<string, TranslationActivity> = new Map();
  const surfaceTranslationActivity = (): ReadonlyMap<string, TranslationActivity> => (
    translationActivityByConversation.get(surfaceConversationId) ?? noTranslationActivity
  );
  const artifactsByTurn = new Map<
    string,
    ReadonlyArray<ConversationArtifactViewModel>
  >();
  /* Tail ranges residency retired, per conversation: a revisit mounts that
   * range rather than the whole Turn budget it would retire again after
   * first paint. Two Turn IDs per entry; the oldest entry is evicted first. */
  const retainedTails = new Map<string, ConversationRetainedTail>();
  const rememberRetainedTail = (): void => {
    if (!surface || !surfaceConversationId) return;
    const tail = surface.retainedTail;
    retainedTails.delete(surfaceConversationId);
    if (!tail) return;
    retainedTails.set(surfaceConversationId, tail);
    if (retainedTails.size > RETAINED_TAIL_CONVERSATIONS) {
      retainedTails.delete(retainedTails.keys().next().value as string);
    }
  };
  let expandedBranchLaneId: string | null = null;
  let pending: {
    conversation: ConversationDocument;
    state: TurnState;
    force: boolean;
  } | null = null;
  let scheduled: { cancel?: () => void } | null = null;
  let disposed = false;
  let committing = false;
  let surfaceOwnsScroll = false;
  let mountAcknowledged = false;
  let commitFailed = false;
  let retryScheduled = false;

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
    rememberRetainedTail();
    selector.dispose();
    taskTimes?.dispose();
    surface?.dispose();
    surface = null;
    surfaceOwnsScroll = false;
    mountAcknowledged = false;
    commitFailed = false;
    retryScheduled = false;
    surfaceConversationId = '';
    surfaceContainer = null;
    currentConversation = null;
    currentState = null;
    translationModeByTurn.clear();
    artifactsByTurn.clear();
    expandedBranchLaneId = null;
  };

  const presentationState = (): ConversationPresentationState => ({
    ...(taskTimes ? { taskTimePresentationsByAttempt: taskTimes.values } : {}),
    translationModeByTurn,
    translationActivityByTurn: surfaceTranslationActivity(),
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
    if (surfaceTranslationActivity().get(turnId)?.partial) return true;
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
    if (scheduled || disposed || committing) return;
    const frame: { cancel?: () => void } = {};
    scheduled = frame;
    try {
      frame.cancel = host.schedule(() => {
        // Cancellation can race delivery; the identity also fences a later
        // conversation's pending paint. Synchronous clocks are supported.
        if (disposed || scheduled !== frame) return;
        scheduled = null;
        tryCommit();
      });
    } catch (error) {
      if (scheduled === frame) scheduled = null;
      throw error;
    }
  };
  const tryCommit = (operation?: () => boolean): boolean => {
    if (committing) return false;
    committing = true;
    const demand = pending;
    let repair = false;
    try { return operation ? operation() : flush(); }
    catch (error) {
      commitFailed = true;
      selector.dispose();
      // Navigation can commit a different DOM window without a pending graph.
      // Its failure still owes the latest canonical graph a successful commit;
      // never replace newer demand that arrived while the operation was running.
      if (!pending && currentConversation && currentState && !disposed
          && host.isActive(currentConversation.id)) {
        pending = { conversation: currentConversation, state: currentState, force: true };
      }
      console.warn('[ConversationSurface] commit failed; latest graph retained', error);
      // One immediate repair per failure streak, not an unbounded paint loop.
      // Further canonical/health/UI demand retries the retained newest graph.
      if (pending && !retryScheduled && !disposed
          && host.isActive(pending.conversation.id)) {
        retryScheduled = true;
        repair = true;
      }
      return false;
    } finally {
      committing = false;
      // Refused/absent mounts retain their graph but cannot create a paint loop.
      // Only a new demand token or this streak's bounded repair earns a callback.
      const replacedGraph = pending && demand
        && (pending.state !== demand.state
          || pending.conversation.id !== demand.conversation.id);
      if (repair || (pending && pending !== demand && (!commitFailed || replacedGraph) && !disposed
          && host.isActive(pending.conversation.id) && host.getContainer())) schedulePaint();
    }
  };
  const repaintPresentation = (immediate = false): void => {
    if (!surface || !currentConversation || !currentState
      || disposed || !host.isActive(currentConversation.id)) return;
    // A callback may invalidate presentation while the current frame commits.
    // Give that demand its own identity so acknowledging this frame cannot drop it.
    pending = pending ? { ...pending, force: true }
      : { conversation: currentConversation, state: currentState, force: true };
    // Explicit disclosure/display intents commit before the next action can
    // read their DOM. Async presentation receipts still coalesce with the graph.
    if (immediate) tryCommit();
    else schedulePaint();
  };

  const ensureSurface = (
    container: HTMLElement,
    conversation: ConversationDocument,
  ): ConversationSurface => {
    if (surface && surface.root.parentElement === container
        && surfaceConversationId === conversation.id) return surface;
    // Keep a failed mount's owner before Surface construction. Retrying the
    // same host must not reset the bounded streak simply because no root exists.
    if (surface || surfaceConversationId !== conversation.id || surfaceContainer !== container) {
      disposeSurface();
      surfaceConversationId = conversation.id;
      surfaceContainer = container;
    }
    currentConversation = conversation;
    currentState = currentState?.conversationId === conversation.id
      ? currentState : null;
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
      ...(retainedTails.has(conversation.id)
        ? { retainedTail: retainedTails.get(conversation.id) }
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

  const flush = (): boolean => {
    cancelPaint();
    const frame = pending;
    if (!frame || disposed || !host.isActive(frame.conversation.id)) {
      if (pending === frame) pending = null;
      return false;
    }
    const container = host.getContainer();
    if (!container) return false;
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
    const stillOwnsMount = (): boolean => !disposed && surface === activeSurface
      && surfaceConversationId === frame.conversation.id
      && host.isActive(frame.conversation.id)
      && activeSurface.root.parentElement === host.getContainer();
    // A forced paint is not evidence that accepted automation output changed.
    // Only the explicit invalidation port requests a reread.
    if (useLegacyScroll) host.restoreScroll?.(scroll);
    if (!stillOwnsMount()) return false;
    if (!mountAcknowledged) {
      host.onConversationMounted?.(frame.conversation);
      if (!stillOwnsMount()) return false;
      mountAcknowledged = true;
    }
    host.afterConversationCommit?.(
      frame.conversation, frame.state, frame.force, viewModel, activeSurface.mountedTurnIds,
    );
    if (!stillOwnsMount()) return false;
    // DOM, mount and required commit callbacks must all succeed before ACK.
    // Reentrant demand may carry a newer graph; acknowledge only this frame.
    if (pending === frame) pending = null;
    commitFailed = false;
    retryScheduled = false;
    return true;
  };

  const isMounted = (conversationId: string): boolean => !disposed
    && host.isActive(conversationId) && surfaceConversationId === conversationId
    && Boolean(surface && surface.root.parentElement === host.getContainer());

  return {
    isMounted,
    invalidateTaskTimePresentations(conversationId) {
      if (!disposed && conversationId === surfaceConversationId) refreshTaskTimes(true);
    },
    ownsConversation(conversationId) {
      return !committing && !commitFailed && !pending && isMounted(conversationId);
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
      if (disposed || !conversationId || !turnId) return;
      let byTurn = translationActivityByConversation.get(conversationId);
      if (activity) {
        if (!byTurn) {
          byTurn = new Map();
          translationActivityByConversation.set(conversationId, byTurn);
        }
        byTurn.set(turnId, mergeTranslationActivity(byTurn.get(turnId), activity));
      } else {
        if (!byTurn || !byTurn.delete(turnId)) return;
        if (!byTurn.size) translationActivityByConversation.delete(conversationId);
      }
      if (conversationId !== surfaceConversationId) return;
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
      if (disposed || committing || !host.isActive(conversationId)) return false;
      if (pending?.conversation.id === conversationId && !tryCommit()) return false;
      const activeSurface = surface;
      if (surfaceConversationId !== conversationId || !activeSurface) return false;
      return tryCommit(() => activeSurface.revealTurn(turnId));
    },
    isNearLatest(threshold) {
      return !surface || !host.isActive(surfaceConversationId) || surface.isNearLatest(threshold);
    },
    followLatest(intent) {
      if (disposed || committing) return;
      if (intent?.force !== false && pending && host.isActive(pending.conversation.id)
          && !tryCommit()) return;
      if (!surface || !host.isActive(surfaceConversationId)) return;
      const activeSurface = surface;
      tryCommit(() => {
        if (surfaceOwnsScroll) activeSurface.followLatest(intent);
        else if (intent?.force !== false) host.followLatest?.();
        return true;
      });
    },
    disposeConversation(conversationId) {
      translationActivityByConversation.delete(conversationId);
      if (pending?.conversation.id === conversationId) {
        pending = null; cancelPaint(); commitFailed = false; retryScheduled = false;
      }
      if (surfaceConversationId === conversationId) disposeSurface();
      retainedTails.delete(conversationId);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      pending = null;
      cancelPaint();
      disposeSurface();
      translationActivityByConversation.clear();
      retainedTails.clear();
    },
  };
}
