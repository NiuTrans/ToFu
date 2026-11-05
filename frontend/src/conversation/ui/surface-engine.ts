/** Keyed reconciliation, viewport residency and intent lifecycle. Receives
 * appearance through a typed port; no CSS, visual copy, transport or settlement.
 * Entry: createConversationSurfaceEngine; composition: conversation-surface.ts. */
import type { ConversationSurfaceDesign } from './surface-design-contracts';
import { scheduleAnimationFrame } from './animation-frame-scheduler';
import type {
  ConversationLaneViewModel,
  ConversationQueueItemViewModel,
  ConversationTurnViewModel,
  ConversationViewModel,
} from '../presentation/conversation-view-model';
import { selectRoundOutline } from '../presentation/round-outline';
import {
  entryRegions,
  groupedTurnEntries,
  groupedTurnLayout,
  type EntryRegion,
  type EntryRegionKind,
  type GroupedTurnEntry,
} from '../presentation/grouped-turn-layout';
import { createSurfaceTurnRenderer } from './surface-turn-renderer';
import { prepareSurfaceWindows, type SurfaceLaneWindow } from './surface-window-preparation';

import { moveRetainedNodeBefore } from './rich-card-reconciliation';
import { finishSurfaceCleanup } from './surface-node-lifecycle';
import type {
  ConversationWindowState,
  ConversationQueueItemRenderContext,
  ConversationSurfaceOptions,
  ConversationSurface,
} from './surface-contracts';
import {
  CONVERSATION_HISTORY_PAGE_TURNS,
  CONVERSATION_HISTORY_FILL_FRAMES,
  CONVERSATION_HISTORY_PREFETCH_VIEWPORTS,
  conversationWindowOptions,
  olderHistoryDue,
  planResidentRange,
  type ResidentBounds,
} from './surface-windowing';
import { createConversationViewportPort } from './conversation-viewport';
import {
  sameLaneHeader,
  sameQueueItem,
} from './surface-presentation-equality';
import {
  type ManagedBlocksContainerElement,
  type ManagedTurnElement,
  type ManagedLaneHeaderElement,
  type ManagedQueueElement,
  type SurfaceRegionWindow,
  directPart,
  placeAt,
  regionWindowSignature,
} from './surface-dom';

/* Presentation ids never contain NUL, so a prose window key cannot collide. */
const PROSE_WINDOW_SUFFIX = '\u0000prose';

/* What the mounted main-lane window shows about context rails, gathered while
 * its Turns render, so the decision costs no extra reads as history grows. */
interface ContextTrackEvidence {
  context: boolean;
  humanInput: boolean;
  /** The window holds every Turn of a complete history. */
  wholeHistory: boolean;
}

/* Context-track demand of a conversation that has not shown a rail yet.
 * Every human input records a context snapshot, so a conversation is railed
 * until its transcript proves otherwise: a mounted human input without a
 * snapshot (legacy history), or a whole, complete history with none. A Surface
 * still waiting for its first page, or whose byte-budgeted tail holds only a
 * reply, keeps the track reserved instead of painting centered and then
 * shifting when the round's input Turn lands. */
const contextTrackDemand = (evidence: ContextTrackEvidence): 'pending' | 'absent' => (
  !evidence.context && (evidence.humanInput || evidence.wholeHistory) ? 'absent' : 'pending');

/** Construct one lifecycle-bound owner for a conversation container. */
export function createConversationSurfaceEngine(
  container: HTMLElement,
  options: ConversationSurfaceOptions,
  design: ConversationSurfaceDesign,
): ConversationSurface {
  const root = container.ownerDocument.createElement('section');
  design.decorate(root, 'root');
  root.dataset.conversationSurface = 'turn-store';
  container.appendChild(root);
  const windowOptions = conversationWindowOptions(options.windowing);
  const ownedScrollAnchor = !options.scrollAnchor && options.scrollViewport
    ? createConversationViewportPort(options.scrollViewport, {
      ...options.viewport,
      canFollowLatest: () => root.dataset.hasLaterContent !== 'true'
        && (options.viewport?.canFollowLatest?.() ?? true),
    })
    : null;
  const scrollAnchor = options.scrollAnchor ?? ownedScrollAnchor;
  let disposed = false;
  let currentConversationId = '';
  /* Conversation whose mounted Turns have shown a context rail. The shell
   * reserves the context track per conversation, never per mounted window:
   * `present` once a rail has shown, `pending` while that is undetermined,
   * and only `absent` collapses the track. */
  let turnContextRailConversationId = '';
  const mainLaneEvidence: ContextTrackEvidence = { context: false, humanInput: false, wholeHistory: false };
  let latestViewModel: ConversationViewModel | null = null;
  let viewportBlockBudget = windowOptions.maxBlocksPerTurn;
  /* Mounted Turn range per lane. Identities re-map it across prepended history
   * pages and tail appends; scroll demand grows or retires it by measured
   * geometry, while explicit paging moves the whole range. */
  let laneWindows = new Map<string, SurfaceLaneWindow>();
  /* Initial/tail and explicit-paging Turn count per lane. */
  let laneBudgets = new Map<string, number>();
  /* Upper bound of a lane's mounted range under scroll demand. */
  let laneCaps = new Map<string, number>();
  /* Turn containers rendered by the latest commit, measured by scroll demand. */
  let laneTurnContainers = new Map<string, HTMLElement>();
  /* Turn nodes mounted by the current commit.  Counted as they are rendered so
   * the diagnostic `data-dom-turn-count` never needs a full-DOM attribute
   * scan on every frame. */
  let mountedTurnIds: string[] = [];
  let lanesById = new Map<string, ConversationLaneViewModel>();
  /* presentationId → first mounted block index. Only Turns whose block count
   * exceeds the window get an entry; a stored start can only widen the tail
   * window (streaming appends stay visible), clicking the earlier-control
   * rewinds it in batches. Cleared when the conversation changes. */
  const blockWindows = new Map<string, number>();
  /* presentationId → exclusive end once scroll residency or paging owns that
   * range; absent while the window is the initial tail-ward budget. A calls
   * range at the tail stores an infinite end so it keeps the newest calls. */
  const blockWindowEnds = new Map<string, number>();
  const clearBlockWindows = (): void => {
    blockWindows.clear();
    blockWindowEnds.clear();
  };

  /* Explicit reader choices by Turn presentation: is its tool panel open?
   * Without one the panel follows its layout default, so like reasoning it is
   * open while the Turn works and folds once it settles; a reader's choice
   * always wins over that default. */
  const toolPanelChoices = new Map<string, boolean>();
  const toolPanelOpen = (turn: ConversationTurnViewModel, defaultOpen: boolean): boolean => (
    toolPanelChoices.get(turn.presentationId || turn.turnId) ?? defaultOpen);
  /* Mounted order of a Turn: grouped zones for replies, authored order otherwise. */
  const surfaceEntries = (turn: ConversationTurnViewModel): ReadonlyArray<GroupedTurnEntry> => {
    const layout = groupedTurnLayout(turn);
    if (!layout) return turn.blocks;
    return groupedTurnEntries(layout, !layout.panel || toolPanelOpen(turn, layout.panel.defaultOpen));
  };
  const findTurn = (presentationId: string): ConversationTurnViewModel | undefined => {
    for (const lane of lanesById.values()) {
      const turn = lane.turns.find(item => (item.presentationId || item.turnId) === presentationId);
      if (turn) return turn;
    }
    return undefined;
  };
  /* Window map key of one region. An ungrouped Turn and a reply's calls keep
   * the presentation id (round reveal and the panel toggle address it); a
   * reply's prose owns a sibling key. Region-relative indices survive the
   * panel opening or closing above them. */
  const regionWindowKey = (presentationId: string, kind: EntryRegionKind): string => (
    kind === 'prose' ? `${presentationId}${PROSE_WINDOW_SUFFIX}` : presentationId);
  const resolveRegion = (key: string): { turn: ConversationTurnViewModel; region: EntryRegion } | undefined => {
    const presentationId = key.endsWith(PROSE_WINDOW_SUFFIX) ? key.slice(0, -PROSE_WINDOW_SUFFIX.length) : key;
    const turn = findTurn(presentationId);
    const region = turn && entryRegions(surfaceEntries(turn))
      .find(item => regionWindowKey(presentationId, item.kind) === key);
    return turn && region ? { turn, region } : undefined;
  };
  /* One region's mounted range, relative to the region. A following reader
   * gets the live tail; scroll residency or paging owns a stored range. */
  const regionWindow = (
    key: string, kind: EntryRegionKind, length: number, maxBlocks: number,
  ): { start: number; end: number } => {
    const residentEnd = blockWindowEnds.get(key);
    if (length > maxBlocks && residentEnd !== undefined) {
      // Scroll residency owns this range; entries appended while the reader
      // is away stay behind the later boundary, as with a paged window.
      // An infinite end is a calls range at the tail, opened there or paged
      // to it: calls appended later join it, within the hard per-Turn cap.
      const end = Math.max(1, Math.min(length, residentEnd));
      const start = Math.max(Math.min(blockWindows.get(key) ?? 0, end - 1), end - windowOptions.maxBlocksPerTurn);
      blockWindows.set(key, start);
      blockWindowEnds.set(key, Math.max(end, residentEnd));
      return { start, end };
    }
    let start = 0;
    if (length > maxBlocks) {
      start = Math.min(blockWindows.get(key) ?? Number.POSITIVE_INFINITY, Math.max(0, length - maxBlocks));
      // Small prefixes do not merit a history boundary. Oversized rounds
      // may span windows: rounding backward would hide the tail and make
      // forward navigation non-monotonic.
      if (length <= maxBlocks + 3) start = 0;
      blockWindows.set(key, start);
      // Calls page only through their in-card rows. A tail range chosen here
      // must not freeze when the follow latch drops: a later frame with more
      // calls or a smaller budget would put the newest calls behind a row the
      // reader never clicked.
      if (kind === 'process') blockWindowEnds.set(key, Number.POSITIVE_INFINITY);
    }
    const end = Math.min(length, start + Math.max(
      maxBlocks, start === 0 && length <= maxBlocks + 3 ? length : 0,
    ));
    return { start, end };
  };

  const turnRenderer = createSurfaceTurnRenderer(options, design, {
    conversationId: () => currentConversationId,
    revealBlock(laneId, presentationId, blockId) {
      if (disposed) return;
      const turn = lanesById.get(laneId)?.turns.find(item =>
        (item.presentationId || item.turnId) === presentationId);
      if (!turn) return;
      const entries = surfaceEntries(turn);
      const index = entries.findIndex(entry => entry.blockId === blockId);
      // Entries outside every region are always mounted.
      const region = entryRegions(entries).find(item => index >= item.start && index < item.end);
      if (!region) return;
      const key = regionWindowKey(presentationId, region.kind);
      scrollAnchor?.suspendFollow?.();
      const start = Math.max(0, index - region.start - Math.floor(viewportBlockBudget / 2));
      blockWindows.set(key, start);
      blockWindowEnds.set(key, Math.min(region.end - region.start, start + viewportBlockBudget));
      commit();
    },
    renderLane: (node, lane, header) => renderLane(node, lane, header),
    renderBlockBoundary: (content, key, direction, count) => renderBlockBoundary(content, key, direction, count),
    renderToolPanelBoundary(node, window, direction, count) {
      const button = design.toolPanelBoundary(node, direction, count);
      button.dataset.conversationBlockWindowAction = direction;
      button.dataset.turnKey = window.key;
      button.dataset.blockCount = String(Math.min(count, windowOptions.blockBatchSize));
    },
  });
  const removeRenderedNode = turnRenderer.removeNode;
  const { ensurePart, directKeyedChildren } = turnRenderer.dom;

  const historyFlights = new Map<string, { state: 'loading' | 'failed'; cursor: number }>();
  let historyEpoch = 0;
  let lastViewportTop = options.scrollViewport?.scrollTop ?? 0;
  let cancelHistoryFrame: (() => void) | null = null;
  let historyFrameEpoch = 0;
  /* Latest reader direction awaiting the viewport frame; null is a passive
   * residency check after a render or a partially refilled buffer. */
  let viewportDemand: 'earlier' | 'later' | null = null;
  const cancelHistoryDemand = (): void => {
    historyFrameEpoch += 1;
    viewportDemand = null;
    cancelHistoryFrame?.();
    cancelHistoryFrame = null;
  };

  const emitIntent = (event: Event): void => {
    const eventTarget = event.target instanceof Element ? event.target : null;
    if (!eventTarget || !root.contains(eventTarget)) return;
    // A reader engaging with a call keeps its panel open once the Turn settles.
    const engaged = eventTarget.closest<HTMLElement>('[data-turn-zone="process"]')
      ?.closest<HTMLElement>('[data-presentation-id]')?.dataset.presentationId;
    if (engaged && !toolPanelChoices.has(engaged)) toolPanelChoices.set(engaged, true);

    if (design.imagePreview(event, eventTarget, options.onPreviewImage)) return;
    const panelToggle = eventTarget.closest<HTMLElement>('[data-conversation-tool-panel]');
    if (panelToggle) {
      event.preventDefault();
      toggleToolPanel(
        panelToggle.closest<HTMLElement>('[data-presentation-id]')?.dataset.presentationId || '',
      );
      return;
    }
    const target = eventTarget.closest<HTMLElement>(
      '[data-conversation-action], [data-conversation-window-action],'
      + ' [data-conversation-block-window-action]',
    );
    if (!target || !root.contains(target)) return;
    const windowAction = target.dataset.conversationWindowAction;
    if (windowAction === 'earlier' || windowAction === 'later') {
      event.preventDefault();
      moveWindow(windowAction, target.dataset.laneId || 'main');
      return;
    }
    const blockAction = target.dataset.conversationBlockWindowAction;
    if (blockAction === 'earlier' || blockAction === 'later') {
      event.preventDefault();
      moveBlockWindow(target.dataset.turnKey || '', blockAction);
      return;
    }
    const turnNode = target.closest<HTMLElement>('[data-turn-id]');
    const blockNode = target.closest<HTMLElement>('[data-block-id]');
    const laneNode = target.closest<HTMLElement>('[data-lane-id]');
    const queueNode = target.closest<HTMLElement>('[data-queue-id]');
    options.onIntent?.({
      type: target.dataset.conversationAction ?? '',
      conversationId: currentConversationId,
      ...(turnNode?.dataset.turnId ? { turnId: turnNode.dataset.turnId } : {}),
      ...(blockNode?.dataset.blockId ? { blockId: blockNode.dataset.blockId } : {}),
      ...(laneNode?.dataset.laneId ? { laneId: laneNode.dataset.laneId } : {}),
      ...(queueNode?.dataset.queueId ? { queueId: queueNode.dataset.queueId } : {}),
      ...(target.dataset.operation ? { operation: target.dataset.operation } : {}),
    });
  };
  root.addEventListener('click', emitIntent);

  const onImageKeydown = (event: KeyboardEvent): void => {
    if (event.defaultPrevented || event.repeat || event.altKey || event.ctrlKey || event.metaKey
        || (event.key !== 'Enter' && event.key !== ' ')) return;
    const target = event.target instanceof Element ? event.target : null;
    if (target && design.isImagePreviewTarget(target)) emitIntent(event);
  };
  root.addEventListener('keydown', onImageKeydown);

  const renderLane = (
    laneContainer: HTMLElement,
    lane: ConversationLaneViewModel,
    includeHeader: boolean,
  ): void => {
    laneContainer.dataset.laneId = lane.laneId;
    laneContainer.dataset.laneKind = lane.kind;
    if (includeHeader) {
      design.decorate(laneContainer, 'branch-lane');
      laneContainer.dataset.laneExpanded = String(Boolean(lane.expanded));
      laneContainer.dataset.laneLive = String(Boolean(lane.live));
    }
    if (includeHeader) {
      const header = ensurePart(
        laneContainer, 'lane-header', 'header',
      ) as ManagedLaneHeaderElement;
      const renderer = design.renderers.renderLaneHeader;
      if (!sameLaneHeader(header._tofuLane, lane)
          || header._tofuLaneHeaderRenderer !== renderer) {
        renderer(header, lane, {
          conversationId: currentConversationId,
        });
        header._tofuLaneHeaderRenderer = renderer;
      }
      header._tofuLane = lane;
    }
    const windowState = currentLaneWindowState(lane);
    const visibleTurns = includeHeader && !lane.expanded ? [] : lane.turns.slice(
      windowState.start, windowState.end,
    );
    const mainLane = lane === latestViewModel?.mainLane;
    if (mainLane) {
      mainLaneEvidence.wholeHistory = windowState.start === 0 && windowState.end === windowState.total
        && lane.history?.hasMore === false;
    }
    const remoteEarlier = windowState.start === 0 && lane.history?.hasMore
      ? Math.min(
        windowState.batchSize,
        Math.max(1, lane.history.totalTurns - lane.turns.length),
      ) : 0;
    renderWindowControl(
      laneContainer,
      lane.laneId,
      'earlier',
      Math.min(windowState.batchSize, windowState.start) || remoteEarlier,
    );
    const turnsContainer = ensurePart(laneContainer, 'lane-turns');
    laneTurnContainers.set(lane.laneId, turnsContainer);
    turnsContainer.hidden = includeHeader && !lane.expanded;
    if (includeHeader) design.decorate(turnsContainer, 'branch-turns');
    const existingTurns = directKeyedChildren(turnsContainer, 'presentationId');
    // Retire the outgoing window prefix before placement. Leaving it at the
    // front moved every retained long-text subtree when just one new Turn
    // entered the 80-Turn window, invalidating retained long-text subtrees.
    const visibleIds = new Set(visibleTurns.map(
      (turn) => turn.presentationId || turn.turnId,
    ));
    for (const [identity, node] of existingTurns) {
      if (visibleIds.has(identity)) continue;
      removeRenderedNode(node);
      existingTurns.delete(identity);
    }
    let turnCursor: Element | null = turnsContainer.firstElementChild;
    visibleTurns.forEach((turn) => {
      mountedTurnIds.push(turn.turnId);
      if (mainLane) {
        if (turn.actor === 'human') mainLaneEvidence.humanInput = true;
        if (!mainLaneEvidence.context) {
          mainLaneEvidence.context = turn.blocks.some(block => block.kind === 'context');
        }
      }
      const presentationId = turn.presentationId || turn.turnId;
      let turnNode = existingTurns.get(
        presentationId,
      ) as ManagedTurnElement | undefined;
      /* Block window is part of a Turn's rendered identity: expanding it must
       * defeat the whole-Turn fast path. */
      const visibleBlocks = surfaceEntries(turn);
      // A compact round overview paints only its edge/attention rows. Keep a
      // modest complete round resident so the first four survive first paint;
      // oversized rounds still use the existing hard-bounded paging window.
      let bigRound: boolean | undefined;
      const windows = entryRegions(visibleBlocks).map((region): SurfaceRegionWindow => {
        const length = region.end - region.start;
        const key = regionWindowKey(presentationId, region.kind);
        const compactRound = region.kind !== 'prose' && length <= windowOptions.maxBlocksPerTurn
          && (bigRound ??= selectRoundOutline(turn).some(section => section.toolCount > 8));
        const maxBlocks = compactRound ? windowOptions.maxBlocksPerTurn : viewportBlockBudget;
        return {
          key, kind: region.kind, regionStart: region.start, length,
          ...regionWindow(key, region.kind, length, maxBlocks),
        };
      });
      const windowSignature = regionWindowSignature(windows);
      const activePlanDecision = latestViewModel?.planDecision;
      const planDecisionForTurn = activePlanDecision
          && activePlanDecision.sourceTurnId === turn.turnId
        ? activePlanDecision : null;
      /* Branch lanes own independent window state that the parent Turn's
       * identity does not capture, so a Turn with branches always reconciles.
       * The fast path runs before any dataset/class write: a memoized Turn's
       * attributes are already correct, and avoiding those DOM writes for every
       * untouched Turn is exactly the win this path exists for. */
      if (turnNode
          && turn.branches.length === 0
          && turnNode._tofuTurnVm === turn
          && turnNode._tofuTurnEntries === visibleBlocks
          && turnNode._tofuTurnWindowSignature === windowSignature
          && turnNode._tofuTurnPlanDecision === planDecisionForTurn
          && turnNode._tofuTurnSurfaceOptions === options) {
        if (turnCursor === turnNode) turnCursor = turnNode.nextElementSibling;
        else moveRetainedNodeBefore(turnsContainer, turnNode, turnCursor);
        existingTurns.delete(presentationId);
        return;
      }
      if (!turnNode) {
        turnNode = turnsContainer.ownerDocument.createElement('article');
        design.decorate(turnNode, 'turn');
      }
      turnRenderer.renderTurn(turnNode, turn, lane, {
        visibleBlocks, windows, windowSignature, planDecisionForTurn,
      });
      if (turnCursor === turnNode) turnCursor = turnNode.nextElementSibling;
      else moveRetainedNodeBefore(turnsContainer, turnNode, turnCursor);
      existingTurns.delete(presentationId);
    });
    for (const staleTurn of existingTurns.values()) removeRenderedNode(staleTurn);
    renderWindowControl(
      laneContainer,
      lane.laneId,
      'later',
      Math.min(
        windowState.batchSize, windowState.total - windowState.end,
      ),
    );
    const laneWindow = laneWindows.get(lane.laneId);
    if (laneWindow) {
      laneWindow.firstMountedTurnId = visibleTurns[0]?.turnId ?? '';
      laneWindow.lastMountedTurnId = visibleTurns[visibleTurns.length - 1]?.turnId ?? '';
    }
  };

  const renderQueue = (
    queueContainer: HTMLElement,
    queue: ReadonlyArray<ConversationQueueItemViewModel>,
  ): void => {
    const existing = directKeyedChildren(queueContainer, 'queueId');
    // Scan each contiguous completion run once. Keep every row as a direct
    // queueId child; ordinary instructions break the shared visual surface.
    let groupStart = 0;
    let groupEnd = 0;
    queue.forEach((item, index) => {
      let context: ConversationQueueItemRenderContext | undefined;
      if (item.source.backgroundCommand) {
        if (index >= groupEnd) {
          groupStart = index;
          groupEnd = index + 1;
          while (groupEnd < queue.length && queue[groupEnd].source.backgroundCommand) groupEnd++;
        }
        context = { backgroundCommandGroup: { count: groupEnd - groupStart, index: index - groupStart } };
      }
      let node = existing.get(item.queueId) as ManagedQueueElement | undefined;
      if (!node) {
        node = queueContainer.ownerDocument.createElement('article');
        design.decorate(node, 'queue-item');
        node.dataset.queueId = item.queueId;
      }
      const renderer = design.renderers.renderQueueItem;
      if (!sameQueueItem(node._tofuQueueItem, item)
          || node._tofuQueueContext?.backgroundCommandGroup?.count !== context?.backgroundCommandGroup?.count
          || node._tofuQueueContext?.backgroundCommandGroup?.index !== context?.backgroundCommandGroup?.index
          || node._tofuQueueRenderer !== renderer) {
        renderer(node, item, currentConversationId, context);
        node._tofuQueueRenderer = renderer;
        node._tofuQueueContext = context;
      }
      node._tofuQueueItem = item;
      node.dataset.queueId = item.queueId;
      placeAt(queueContainer, node, index);
      existing.delete(item.queueId);
    });
    for (const stale of existing.values()) removeRenderedNode(stale);
  };

  /* A shown context rail covers the Turns after it until the next shown rail
   * (info-rail track, agent dock host). Retiring that host while a covered
   * Turn stays resident drops the rail and its dock beside the reader's round,
   * so a top retirement stops at the nearest mounted host, a few Turns up. */
  const RAIL_HOST_RETENTION_TURNS = 4;
  const retainCoveringRailHost = (
    nodes: ReadonlyArray<Element>, mountedStart: number, nextStart: number,
  ): number => {
    const shownRail = (node: Element | undefined): boolean => Boolean(node && Array.from(node.children)
      .some(child => (child as HTMLElement).dataset.conversationPart === 'turn-context-rail'
        && !(child as HTMLElement).hidden));
    const lowest = Math.max(mountedStart, nextStart - RAIL_HOST_RETENTION_TURNS);
    for (let index = nextStart; index >= lowest; index -= 1) {
      if (shownRail(nodes[index - mountedStart])) return index;
    }
    return nextStart;
  };

  const maxWindowStart = (total: number, maxTurns: number): number => (
    Math.max(0, total - maxTurns)
  );

  /* Consumed by the first render only; later renders own their windows. */
  let initialTail = options.retainedTail ?? null;

  /* A retained tail range stays valid while it still mounts the prefetch
   * distance above the viewport. Content that shrank (a settled process
   * folding away) refills the budget in the same frame, as before. */
  const refillRetainedTails = (laneIds: ReadonlyArray<string>): boolean => {
    const viewport = options.scrollViewport;
    const viewportHeight = viewport?.clientHeight ?? 0;
    if (!viewport || !(viewportHeight > 0)) return false;
    const viewportTop = viewport.getBoundingClientRect().top + viewport.clientTop;
    let refilled = false;
    for (const laneId of laneIds) {
      const lane = lanesById.get(laneId);
      const laneWindow = laneWindows.get(laneId);
      const first = laneTurnContainers.get(laneId)?.querySelector<HTMLElement>(':scope > [data-turn-id]');
      if (!lane || !laneWindow || !first) continue;
      const above = viewportTop - first.getBoundingClientRect().top;
      if (above >= viewportHeight * CONVERSATION_HISTORY_PREFETCH_VIEWPORTS) continue;
      const state = currentLaneWindowState(lane);
      laneWindow.start = Math.min(laneWindow.start, maxWindowStart(state.total, state.maxTurns));
      refilled = true;
    }
    return refilled;
  };


  const currentLaneWindowState = (
    lane: ConversationLaneViewModel,
  ): ConversationWindowState => {
    const maxTurns = laneBudgets.get(lane.laneId) ?? 0;
    const total = lane.turns.length;
    const laneWindow = laneWindows.get(lane.laneId) ?? {
      start: maxWindowStart(total, maxTurns),
      end: total,
      atTail: true,
      firstMountedTurnId: '',
      lastMountedTurnId: '',
      loadedTurns: total,
    };
    laneWindows.set(lane.laneId, laneWindow);
    let start = maxTurns > 0 ? Math.min(laneWindow.start, total) : total;
    let end = maxTurns > 0 ? Math.max(start, Math.min(laneWindow.end, total)) : total;
    // Turns removed under a stale range must not leave an empty transcript.
    if (maxTurns > 0 && total > 0 && end <= start) {
      start = maxWindowStart(total, maxTurns);
      end = total;
    }
    const batchSize = maxTurns > 0
      ? Math.min(windowOptions.batchSize, maxTurns) : 0;
    return { start, end, total, maxTurns, batchSize };
  };

  const currentWindowState = (): ConversationWindowState => (
    latestViewModel
      ? currentLaneWindowState(latestViewModel.mainLane)
      : {
        start: 0, end: 0, total: 0,
        maxTurns: windowOptions.maxTurns,
        batchSize: windowOptions.batchSize,
      }
  );

  const renderBlockBoundary = (content: HTMLElement, key: string,
    direction: 'earlier' | 'later', count: number): HTMLElement => {
    const control = ensurePart(content, `turn-blocks-${direction}`, 'nav');
    control.hidden = count === 0;
    control.dataset.historyBoundary = direction;
    const button = design.blockBoundary(control, direction, count);
    button.disabled = count === 0;
    button.dataset.conversationBlockWindowAction = direction;
    button.dataset.turnKey = key;
    button.dataset.blockCount = String(Math.min(count, windowOptions.blockBatchSize));
    return control;
  };

  /* Next older page for a lane whose loaded history above the reader is
   * running out, or null. Mounted Turns are measured; loaded Turns not mounted
   * yet count at their mean height. A failed page retries only on a renewed
   * reader gesture near the loaded top, never from a refill frame or from the
   * read-ahead distance; any other flight, including a landed page awaiting
   * its render, holds the lane. */
  const olderHistoryCursor = (
    laneId: string,
    lane: ConversationLaneViewModel,
    unmountedTurns: number,
    nodes: ReadonlyArray<Element>,
    boxOf: (node: Element | undefined) => { top: number; bottom: number } | null,
    measure: { viewportHeight: number; lookahead: number; readerEvent: boolean },
  ): number | null => {
    const history = lane.history;
    if (!history?.hasMore || history.nextBeforeOrdinal === null) return null;
    const flight = historyFlights.get(laneId);
    const failed = flight?.state === 'failed';
    if (flight && !(failed && measure.readerEvent)) return null;
    const first = boxOf(nodes[0]);
    const last = boxOf(nodes[nodes.length - 1]);
    if (!first || !last) return null;
    return olderHistoryDue({
      viewportHeight: measure.viewportHeight,
      mountedTop: first.top,
      unmountedTurns,
      averageTurnHeight: (last.bottom - first.top) / nodes.length,
      lookahead: measure.lookahead,
      ...(failed ? { viewports: CONVERSATION_HISTORY_PREFETCH_VIEWPORTS } : {}),
    }) ? history.nextBeforeOrdinal : null;
  };

  const requestHistory = (laneId: string, cursor: number, readerInitiated = true): boolean => {
    if (!options.onIntent || historyFlights.get(laneId)?.state === 'loading') return false;
    const flight = { state: 'loading' as 'loading' | 'failed', cursor };
    const epoch = historyEpoch;
    historyFlights.set(laneId, flight);
    // A residency prefetch is not reader intent and leaves the follow latch.
    if (readerInitiated) scrollAnchor?.suspendFollow?.();
    commit();
    let result: void | Promise<unknown>;
    try {
      result = options.onIntent({ type: 'load-earlier-turns', conversationId: currentConversationId,
        laneId, beforeOrdinal: cursor, limit: CONVERSATION_HISTORY_PAGE_TURNS });
    } catch {
      flight.state = 'failed'; commit(); return true;
    }
    // Legacy void intent hosts own their settlement; production forwards the
    // coordinator promise. Never invent successful storage reads from DOM.
    if (!result || typeof result.then !== 'function') {
      historyFlights.delete(laneId); commit(); return true;
    }
    void result.then(() => {
      if (disposed || historyEpoch !== epoch || historyFlights.get(laneId) !== flight) return;
      // The coordinator published the page and the Store render that carries
      // it is already scheduled; its advanced cursor retires this flight in the
      // same anchored commit that mounts the page. Retiring it here painted an
      // idle boundary over the previous window first (a jump at the loaded top)
      // and let a reader frame queued before that render read the page again.
      // A page no render consumes retires on the frame after.
      const retire = (): void => {
        if (disposed || historyEpoch !== epoch || historyFlights.get(laneId) !== flight) return;
        historyFlights.delete(laneId); commit();
      };
      const view = root.ownerDocument.defaultView;
      if (view) scheduleAnimationFrame(view, retire); else retire();
    }, () => {
      if (disposed || historyEpoch !== epoch || historyFlights.get(laneId) !== flight) return;
      flight.state = 'failed'; commit();
    });
    return true;
  };

  const renderWindowControl = (
    laneNode: HTMLElement,
    laneId: string,
    direction: 'earlier' | 'later',
    count: number,
  ): void => {
    const control = ensurePart(
      laneNode, `turn-window-${direction}`, 'nav',
    );
    const flight = direction === 'earlier' ? historyFlights.get(laneId) : undefined;
    control.hidden = count === 0 && !flight;
    control.dataset.windowDirection = direction;
    control.dataset.historyBoundary = direction;
    control.dataset.historyState = flight?.state ?? 'idle';
    const button = design.windowBoundary(control, direction, count, flight?.state);
    button.disabled = count === 0 || flight?.state === 'loading';
    button.dataset.conversationWindowAction = direction;
    button.dataset.laneId = laneId;
    button.dataset.turnCount = String(count);
  };

  const commit = (): void => {
    const viewModel = latestViewModel;
    if (!viewModel) return;
    mountedTurnIds = [];
    laneTurnContainers = new Map<string, HTMLElement>();
    const anchor = scrollAnchor?.capture(root);

    const focused = root.ownerDocument.activeElement as HTMLElement | null;
    const boundaryFocus = focused?.matches('[data-conversation-window-action], [data-conversation-block-window-action]') ? focused : null;
    currentConversationId = viewModel.conversationId;
    root.dataset.conversationId = viewModel.conversationId;
    root.dataset.transport = viewModel.transport;
    root.dataset.conversationRevision = String(viewModel.conversationRevision);
    const windowState = currentWindowState();
    root.dataset.windowStart = String(windowState.start);
    root.dataset.windowEnd = String(windowState.end);
    root.dataset.windowTotal = String(windowState.total);
    root.dataset.windowMaxTurns = String(windowState.maxTurns);
    root.dataset.windowBatchSize = String(windowState.batchSize);
    const transportNotice = ensurePart(
      root, 'transport-notice', 'aside',
    ) as HTMLElement;
    const noticeState = viewModel.transportNotice;
    transportNotice.hidden = !noticeState;
    if (noticeState) {
      transportNotice.dataset.syncState = noticeState;
      design.transportNotice(transportNotice, noticeState);
    }
    placeAt(root, transportNotice, 0);
    const main = ensurePart(root, 'main-lane');
    design.decorate(main, 'main-lane');
    mainLaneEvidence.context = false;
    mainLaneEvidence.humanInput = false;
    mainLaneEvidence.wholeHistory = false;
    renderLane(main, viewModel.mainLane, false);
    const orphans = ensurePart(root, 'orphan-lanes');
    const existingOrphans = directKeyedChildren(orphans, 'laneId');
    viewModel.orphanLanes.forEach((lane, index) => {
      let laneNode = existingOrphans.get(lane.laneId);
      if (!laneNode) {
        laneNode = orphans.ownerDocument.createElement('section');
        design.decorate(laneNode, 'orphan-lane');
        laneNode.dataset.laneId = lane.laneId;
      }
      renderLane(laneNode, lane, true);
      placeAt(orphans, laneNode, index);
      existingOrphans.delete(lane.laneId);
    });
    for (const stale of existingOrphans.values()) removeRenderedNode(stale);
    renderQueue(ensurePart(root, 'queue'), viewModel.queue);
    root.dataset.domTurnCount = String(mountedTurnIds.length);
    // Retiring a far Turn or a tail window cut inside a round must not collapse
    // the track and reflow every line the reader is looking at.
    if (root.querySelector('[data-conversation-part="turn-context-rail"]:not([hidden])')) {
      turnContextRailConversationId = viewModel.conversationId;
    }
    root.dataset.turnContextRail = turnContextRailConversationId === viewModel.conversationId
      ? 'present' : contextTrackDemand(mainLaneEvidence);

    root.dataset.hasLaterContent = String(Boolean(root.querySelector(
      '[data-history-boundary="later"]:not([hidden])',
    )));
    if (anchor !== undefined) scrollAnchor?.restore(root, anchor);

    if (boundaryFocus && (!boundaryFocus.isConnected
      || (boundaryFocus as HTMLButtonElement).disabled || boundaryFocus.closest('[hidden]'))) {
      const viewportTop = options.scrollViewport?.getBoundingClientRect().top ?? 0;
      const target = [...root.querySelectorAll<HTMLElement>('[data-block-id]')]
        .find(node => !node.hidden && node.getBoundingClientRect().bottom > viewportTop);
      if (target) {
        const disclosure = target.querySelector<HTMLElement>('summary, button, a[href]');
        if (disclosure) disclosure.focus({ preventScroll: true });
        else { target.tabIndex = -1; target.focus({ preventScroll: true }); }
      }
    }

    lastViewportTop = options.scrollViewport?.scrollTop ?? 0;
    options.onCommit?.(mountedTurnIds);
  };

  const moveBlockWindow = (turnKey: string, direction: 'earlier' | 'later'): boolean => {

    if (disposed) return false;
    const current = blockWindows.get(turnKey);
    if (current == null) return false;
    const target = resolveRegion(turnKey);
    if (!target) return false;
    const step = Math.min(windowOptions.blockBatchSize, Math.max(1, Math.floor(viewportBlockBudget / 2)));
    const residentEnd = blockWindowEnds.get(turnKey);
    const length = target.region.end - target.region.start;
    if (target.region.kind === 'process') {
      // An in-card row reveals more calls at its own edge; the calls already
      // on screen stay mounted until the hard per-Turn cap.
      const cap = windowOptions.maxBlocksPerTurn;
      const end = Math.min(length, residentEnd ?? current + viewportBlockBudget);
      const start = direction === 'earlier' ? Math.max(0, current - step) : current;
      const nextEnd = direction === 'earlier' ? Math.min(end, start + cap) : Math.min(length, end + step);
      const nextStart = Math.max(start, nextEnd - cap);
      if (nextStart === current && nextEnd === end) return false;
      scrollAnchor?.suspendFollow?.();
      blockWindows.set(turnKey, nextStart);
      blockWindowEnds.set(turnKey, nextEnd === length ? Number.POSITIVE_INFINITY : nextEnd);
      commit();
      return true;
    }
    // A scroll-resident range pages whole, like the Turn window.
    const count = residentEnd === undefined ? viewportBlockBudget : Math.max(1, residentEnd - current);
    const next = Math.max(0, Math.min(length - count,
      current + (direction === 'earlier' ? -step : step)));
    if (next === current) return false;
    scrollAnchor?.suspendFollow?.();
    blockWindows.set(turnKey, next);
    if (residentEnd !== undefined) blockWindowEnds.set(turnKey, next + count);
    commit();
    return true;
  };

  /* Opening or closing a tool panel is a reader gesture: follow is suspended
   * so the scroll anchor keeps the header where it was. An oversized panel
   * opens at its newest calls; its prose window is unaffected. */
  const toggleToolPanel = (presentationId: string): void => {
    const turn = findTurn(presentationId);
    const panel = turn && groupedTurnLayout(turn)?.panel;
    if (disposed || !turn || !panel) return;
    const open = !toolPanelOpen(turn, panel.defaultOpen);
    toolPanelChoices.set(presentationId, open);
    scrollAnchor?.suspendFollow?.();
    blockWindows.delete(presentationId);
    blockWindowEnds.delete(presentationId);
    commit();
  };

  const moveWindow = (
    direction: 'earlier' | 'later',
    laneId = 'main',
  ): boolean => {
    const lane = lanesById.get(laneId);
    if (disposed || !latestViewModel || !lane) return false;
    const state = currentLaneWindowState(lane);
    if (state.maxTurns <= 0) return false;
    const laneWindow = laneWindows.get(laneId);
    if (!laneWindow) return false;
    // Explicit paging moves the whole range; scroll demand alone resizes it.
    const count = Math.max(1, state.end - state.start);
    const nextStart = Math.max(0, Math.min(
      state.total - count,
      state.start + (direction === 'earlier' ? -1 : 1)
        * Math.min(state.batchSize, Math.max(1, Math.floor(state.maxTurns / 2))),
    ));
    if (nextStart === state.start) {
      if (direction === 'earlier' && lane.history?.hasMore
          && lane.history.nextBeforeOrdinal !== null) {
        laneWindow.atTail = false;
        return requestHistory(laneId, lane.history.nextBeforeOrdinal);
      }
      return false;
    }
    laneWindow.start = nextStart;
    laneWindow.end = nextStart + count;

    scrollAnchor?.suspendFollow?.();
    laneWindow.atTail = laneWindow.end === state.total;
    commit();
    return true;
  };

  /* Per-frame scroll travel of the reader's latest gesture. Mounting looks
   * ahead by the distance the next frame may cover, so a fling never reaches
   * the mounted edge before the following residency frame. */
  let readerTravel = 0;

  /* Measured residency for every rendered lane and oversized block range.
   * One plan per range per pass; returns whether a commit changed residency.
   * Without reader demand a pass only retires far content, in both directions;
   * mounting and history requests follow the reader's direction alone. */
  const updateResidency = (
    demand: 'earlier' | 'later' | null,
    readerEvent: boolean,
    turnBudget: number,
  ): number => {
    const viewport = options.scrollViewport;
    if (!viewport || !latestViewModel) return 0;
    const viewportHeight = viewport.clientHeight;
    if (!(viewportHeight > 0)) return 0;
    const viewportTop = viewport.getBoundingClientRect().top + viewport.clientTop;
    const edgeLimit = Math.min(240, viewportHeight / 2);
    const lookahead = demand ? Math.min(viewportHeight * 4, readerTravel * 2) : 0;
    const directions = demand ? [demand] : (['earlier', 'later'] as const);
    const boxOf = (node: Element | undefined): { top: number; bottom: number } | null => {
      if (!node) return null;
      const rect = node.getBoundingClientRect();
      return rect.width > 0 || rect.height > 0
        ? { top: rect.top - viewportTop, bottom: rect.bottom - viewportTop } : null;
    };
    let changed = false;
    let mounted = 0;
    const prefetch: Array<[string, number]> = [];
    for (const [laneId, container] of laneTurnContainers) {
      const lane = lanesById.get(laneId);
      const laneWindow = laneWindows.get(laneId);
      if (!lane || !laneWindow || container.hidden || !container.isConnected) continue;
      const state = currentLaneWindowState(lane);
      const nodes = Array.from(container.children)
        .filter(node => (node as HTMLElement).dataset.turnId !== undefined);
      if (state.maxTurns <= 0 || nodes.length !== state.end - state.start) continue;
      // The snapshot's byte budget always admits the newest Turn, so a huge
      // reply can arrive without the input Turn that opened its round. Fetch
      // that page without reader demand: the round's context rail, agent dock,
      // edit and retry controls live on its input Turn.
      // Only while the reader is inside that first Turn: a lane with no input
      // Turn at all must not page its whole history unasked.
      const opening = lane.history;
      const firstBox = state.start === 0 ? boxOf(nodes[0]) : null;
      if (firstBox && firstBox.bottom > -viewportHeight
          && opening?.hasMore && opening.nextBeforeOrdinal !== null
          && !historyFlights.has(laneId) && !lane.turns.some(turn => turn.role === 'user')) {
        prefetch.push([laneId, opening.nextBeforeOrdinal]);
      }
      const older = demand === 'earlier' && !prefetch.some(([pending]) => pending === laneId)
        ? olderHistoryCursor(laneId, lane, state.start, nodes, boxOf,
          { viewportHeight, lookahead, readerEvent })
        : null;
      if (older !== null) prefetch.push([laneId, older]);
      const bounds: ResidentBounds = offset => boxOf(nodes[offset]);
      for (const direction of directions) {
        const cap = laneCaps.get(laneId) ?? state.maxTurns;
        const batch = Math.max(1, Math.min(windowOptions.batchSize, cap));
        const plan = planResidentRange(state, state.total, direction, bounds, {
          viewportHeight, cap, edgeLimit, lookahead,
          batch: demand ? Math.min(batch, Math.max(0, turnBudget - mounted)) : 0,
        });
        if (!plan.range) continue;
        const start = plan.range.start > state.start
          ? retainCoveringRailHost(nodes, state.start, plan.range.start) : plan.range.start;
        if (start === state.start && plan.range.end === state.end) continue;
        mounted += Math.max(0, state.start - start)
          + Math.max(0, plan.range.end - state.end);
        laneWindow.start = start;
        laneWindow.end = plan.range.end;
        laneWindow.atTail = plan.range.end === state.total;
        changed = true;
        break;
      }
    }
    const blockBatch = demand ? Math.max(1, Math.min(windowOptions.blockBatchSize, viewportBlockBudget)) : 0;
    // Following resets block windows to the live tail on every stream frame;
    // growing them meanwhile would mount and retire the same blocks per frame.
    const blockNodes = scrollAnchor?.following ? []
      : root.querySelectorAll<HTMLElement>('[data-presentation-id]');
    for (const node of blockNodes) {
      const blocksContainer = directPart(directPart(node, 'turn-content') ?? node, 'turn-blocks') as
        ManagedBlocksContainerElement | null;
      const windows = blocksContainer?._tofuBlockWindows;
      if (!blocksContainer || blocksContainer.hidden || !windows?.length) continue;
      const keyed = (Array.from(blocksContainer.children) as HTMLElement[])
        .filter(child => child.dataset.blockId !== undefined);
      // Each region measures only its own window's nodes; pinned head/trail
      // entries and in-place boundary rows never enter its geometry.
      for (const window of windows) {
        // Calls page only through their in-card rows, which promise a click:
        // scrolling neither mounts nor retires them.
        if (window.kind === 'process' || !blockWindows.has(window.key)) continue;
        const { start, end } = window;
        const blocks = keyed.filter(child => child.dataset.blockRegion === window.kind);
        if (blocks.length !== end - start) continue;
        for (const direction of directions) {
          const plan = planResidentRange({ start, end }, window.length, direction,
            offset => boxOf(blocks[offset]), {
              viewportHeight, cap: windowOptions.maxBlocksPerTurn, batch: blockBatch, edgeLimit, lookahead,
            });
          if (!plan.range) continue;
          mounted += Math.max(0, start - plan.range.start) + Math.max(0, plan.range.end - end);
          blockWindows.set(window.key, plan.range.start);
          blockWindowEnds.set(window.key, plan.range.end);
          changed = true;
          break;
        }
      }
    }
    if (changed) commit();
    for (const [laneId, cursor] of prefetch) requestHistory(laneId, cursor, false);
    // A retirement-only commit still counts as progress for the caller.
    return changed ? Math.max(1, mounted) : 0;
  };

  /* One frame of residency. Growth is sized from estimated item heights, so a
   * frame re-measures once after committing and tops up any shortfall within
   * the same batch budget, before the reader can see the mounted edge. */
  const runResidencyFrame = (demand: 'earlier' | 'later' | null, readerEvent: boolean): boolean => {
    const budget = Math.max(1, windowOptions.batchSize);
    const first = updateResidency(demand, readerEvent, budget);
    if (!first) return false;
    if (demand && first < budget) updateResidency(demand, false, budget - first);
    return true;
  };

  /* Coalesce residency into one pending frame. A reader gesture renews a
   * bounded refill budget; each committed frame re-measures before mounting
   * more in the same direction. */
  let fillFrames = 0;
  let readerEventPending = false;
  const scheduleResidency = (direction: 'earlier' | 'later' | null, readerEvent: boolean): void => {
    const view = options.scrollViewport?.ownerDocument.defaultView;
    if (!view || disposed) return;
    if (direction) viewportDemand = direction;
    if (readerEvent) {
      fillFrames = CONVERSATION_HISTORY_FILL_FRAMES;
      readerEventPending = true;
    }
    if (cancelHistoryFrame) return;
    const epoch = ++historyFrameEpoch;
    cancelHistoryFrame = scheduleAnimationFrame(view, () => {
      if (disposed || epoch !== historyFrameEpoch) return;
      cancelHistoryFrame = null;
      const frameDemand = viewportDemand;
      const frameReaderEvent = readerEventPending;
      viewportDemand = null;
      readerEventPending = false;
      if (runResidencyFrame(frameDemand, frameReaderEvent) && frameDemand && fillFrames > 0) {
        fillFrames -= 1;
        scheduleResidency(frameDemand, false);
      }
    });
  };

  const onHistoryScroll = (event: Event): void => {
    const viewport = options.scrollViewport;
    if (!viewport || disposed) return;
    const top = viewport.scrollTop;
    const wheelDelta = event.type === 'wheel' ? (event as WheelEvent).deltaY : 0;
    if (Math.abs(top - lastViewportTop) < 1 && wheelDelta === 0) return;
    const direction = wheelDelta !== 0 ? (wheelDelta < 0 ? 'earlier' : 'later')
      : top < lastViewportTop ? 'earlier' : 'later';
    if (event.type === 'scroll') readerTravel = Math.abs(top - lastViewportTop);
    lastViewportTop = top;
    scheduleResidency(direction, true);
  };
  options.scrollViewport?.addEventListener('scroll', onHistoryScroll, { passive: true });

  options.scrollViewport?.addEventListener('wheel', onHistoryScroll, { passive: true });

  return {
    root,
    get windowState() { return currentWindowState(); },
    get mountedTurnIds() { return mountedTurnIds; },
    get retainedTail() {
      const lane = latestViewModel?.mainLane;
      const laneWindow = lane ? laneWindows.get(lane.laneId) : undefined;
      if (!lane || !laneWindow?.atTail || laneWindow.end !== lane.turns.length) return null;
      const tailStart = maxWindowStart(lane.turns.length, laneBudgets.get(lane.laneId) ?? 0);
      const firstTurnId = lane.turns[laneWindow.start]?.turnId;
      const lastTurnId = lane.turns[lane.turns.length - 1]?.turnId;
      return laneWindow.start > tailStart && firstTurnId && lastTurnId
        ? { firstTurnId, lastTurnId } : null;
    },
    render(viewModel) {
      if (disposed) throw new Error('ConversationSurface is disposed.');
      const previous = latestViewModel;
      const reset = !previous || previous.conversationId !== viewModel.conversationId;
      if (reset) {
        clearBlockWindows();
        toolPanelChoices.clear(); historyFlights.clear(); historyEpoch += 1;
        cancelHistoryDemand();
        readerTravel = 0;
      }
      const prepared = prepareSurfaceWindows(
        viewModel, laneWindows, windowOptions, {
          reset, viewportHeight: options.scrollViewport?.clientHeight ?? 0,
          geometric: Boolean(options.scrollViewport), following: scrollAnchor?.following,
        }, initialTail,
      );
      ({ lanesById, laneWindows, laneBudgets, laneCaps, viewportBlockBudget } = prepared);
      initialTail = null;
      for (const [laneId, flight] of historyFlights) {
        const history = lanesById.get(laneId)?.history;
        if (!history?.hasMore || history.nextBeforeOrdinal !== flight.cursor) historyFlights.delete(laneId);
      }

      if (scrollAnchor?.following) clearBlockWindows();
      latestViewModel = viewModel;
      commit();
      if (prepared.retainedTails.length && refillRetainedTails(prepared.retainedTails)) commit();
      if (reset && previous) scrollAnchor?.followLatest?.(root);
      scheduleResidency(null, false);
    },
    showEarlier() { return moveWindow('earlier', 'main'); },
    showLater() { return moveWindow('later', 'main'); },
    revealTurn(turnId) {
      if (disposed || !latestViewModel) return false;
      const lane = latestViewModel.mainLane;
      const index = lane.turns.findIndex(turn => turn.turnId === turnId);
      const state = currentLaneWindowState(lane);
      if (index < 0 || state.maxTurns <= 0) return false;
      cancelHistoryDemand();
      scrollAnchor?.suspendFollow?.();
      if (index < state.start || index >= state.end) {
        const laneWindow = laneWindows.get(lane.laneId)!;
        laneWindow.start = Math.min(index, maxWindowStart(state.total, state.maxTurns));
        laneWindow.end = Math.min(state.total, laneWindow.start + state.maxTurns);
        laneWindow.atTail = laneWindow.end === state.total;
        laneWindow.firstMountedTurnId = lane.turns[laneWindow.start]?.turnId ?? '';
        laneWindow.lastMountedTurnId = lane.turns[laneWindow.end - 1]?.turnId ?? '';
        commit();
        scheduleResidency(null, false);
      }
      const aligned = scrollAnchor?.scrollToTurn?.(root, turnId, () => {
        // Native scroll events arrive later. This exact navigation is not a
        // reader gesture asking the lazy boundary to move another window.
        lastViewportTop = options.scrollViewport?.scrollTop ?? 0;
      });
      return aligned ?? true;
    },
    isNearLatest(threshold) {
      return !disposed && root.dataset.hasLaterContent !== 'true'
        && (scrollAnchor?.isNearBottom?.(threshold) ?? true);
    },
    followLatest(intent) {
      if (disposed) return;
      const force = intent?.force !== false;
      if (!force && (scrollAnchor?.following === false
        || root.dataset.hasLaterContent === 'true')) return;
      if (force) cancelHistoryDemand();
      // Routine layout nudges never rebuild a correct transcript. Explicit
      // latest intent restores every expanded lane and block window first.
      if (force && latestViewModel
        && (blockWindows.size || root.dataset.hasLaterContent === 'true')) {
        clearBlockWindows();
        for (const [laneId, laneWindow] of laneWindows) {
          const lane = lanesById.get(laneId);
          if (!lane) continue;
          const state = currentLaneWindowState(lane);
          laneWindow.start = maxWindowStart(state.total, state.maxTurns);
          laneWindow.end = state.total;
          laneWindow.atTail = true;
          laneWindow.firstMountedTurnId = lane.turns[laneWindow.start]?.turnId ?? '';
          laneWindow.lastMountedTurnId = lane.turns[state.total - 1]?.turnId ?? '';
        }
        commit();
      }
      scrollAnchor?.followLatest?.(root, { force });
      if (force) scheduleResidency(null, false);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      root.removeEventListener('click', emitIntent);

      root.removeEventListener('keydown', onImageKeydown);

      options.scrollViewport?.removeEventListener('scroll', onHistoryScroll);

      options.scrollViewport?.removeEventListener('wheel', onHistoryScroll);
      cancelHistoryDemand();
      historyEpoch += 1; historyFlights.clear();
      finishSurfaceCleanup([
        () => ownedScrollAnchor?.dispose?.(),
        () => removeRenderedNode(root),
        () => toolPanelChoices.clear(),
        () => {
          latestViewModel = null;
          lanesById.clear(); laneWindows.clear(); laneBudgets.clear(); laneCaps.clear();
          laneTurnContainers.clear(); mountedTurnIds = [];
          clearBlockWindows();
          root.replaceChildren(); root.remove();
        },
      ]);
    },
  };
}
