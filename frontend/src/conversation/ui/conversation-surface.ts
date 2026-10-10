/**
 * Lifecycle composition and keyed conversation DOM owner. Delegates scrolling,
 * equality, fallback markup and DOM primitives to explicit owners; no
 * transport or settlement.
 */
import './conversation-surface.css';
import type {
  ConversationBlockViewModel,
  ConversationLaneViewModel,
  ConversationQueueItemViewModel,
  ConversationTurnViewModel,
  ConversationViewModel,
} from '../presentation/conversation-view-model';
import { RoundOutline } from './round-outline';
import { blockRoundIdentity } from '../presentation/round-outline';
import { moveRetainedNodeBefore } from './rich-card-reconciliation';
import type {
  ConversationTurnRenderContext,
  ConversationWindowState,
  ConversationSurfaceOptions,
  ConversationSurface,
} from './surface-contracts';
import {
  CONVERSATION_HISTORY_PAGE_TURNS,
  CONVERSATION_BLOCK_ESTIMATED_HEIGHT,
  CONVERSATION_TURN_ESTIMATED_HEIGHT,
  CONVERSATION_TURN_INITIAL_MINIMUM,
  CONVERSATION_BLOCK_OVERSCAN_VIEWPORTS,
  CONVERSATION_BLOCK_INITIAL_MINIMUM,
  conversationWindowOptions,
} from './surface-windowing';
import { createConversationViewportPort } from './conversation-viewport';
import {
  sameBlock,
  sameTurnHeader,
  sameTurnAvatar,
  sameTurnActions,
  sameTurnFooter,
  samePlanDecision,
  sameLaneHeader,
  sameQueueItem,
} from './surface-presentation-equality';
import {
  renderDefaultBlock,
  renderDefaultTurnAvatar,
  renderDefaultTurnHeader,
  renderDefaultTurnActions,
  renderDefaultTurnFooter,
  renderDefaultTurnContextRail,
  renderDefaultPlanDecision,
  renderDefaultLaneHeader,
  renderDefaultQueueItem,
} from './surface-default-renderers';
import {
  type ManagedBlockElement,
  type ManagedBlocksContainerElement,
  type ManagedTurnAvatarElement,
  type ManagedTurnHeaderElement,
  type ManagedTurnActionsElement,
  type ManagedTurnFooterElement,
  type ManagedTurnContextRailElement,
  type ManagedPlanDecisionElement,
  type ManagedTurnElement,
  type ManagedLaneHeaderElement,
  type ManagedQueueElement,
  directPart,
  ensurePart,
  directKeyedChildren,
  placeAt,
  placeManagedAt,
  hasRenderableContent,
} from './surface-dom';

export type {
  ConversationIntent,
  ConversationBlockRenderContext,
  ConversationTurnRenderContext,
  ConversationLaneRenderContext,
  ConversationSurfaceRenderers,
  ScrollAnchorPort,
  ConversationWindowOptions,
  ConversationWindowState,
  ConversationViewportOptions,
  ConversationScrollSnapshot,
  ConversationSurfaceOptions,
  ConversationSurface,
} from './surface-contracts';
export {
  CONVERSATION_DOM_WINDOW_MAX_TURNS,
  CONVERSATION_DOM_WINDOW_BATCH_TURNS,
  CONVERSATION_HISTORY_PAGE_TURNS,
  CONVERSATION_BLOCK_WINDOW_MAX_BLOCKS,
  CONVERSATION_BLOCK_WINDOW_BATCH_BLOCKS,
} from './surface-windowing';
export { createConversationViewportPort } from './conversation-viewport';

const DEFAULT_TRANSPORT_NOTICE_LABELS: Record<
  'recovering' | 'degraded' | 'offline',
  string
> = {
  recovering: 'Reconnecting — the transcript catches up automatically; '
    + 'in-flight replies are not lost.',
  degraded: 'Connection degraded — the transcript below may be stale '
    + 'while it resyncs.',
  offline: 'You appear to be offline — the transcript resyncs '
    + 'when the connection returns.',
};

/** Construct one lifecycle-bound owner for a conversation container. */
export function createConversationSurface(
  container: HTMLElement,
  options: ConversationSurfaceOptions = {},
): ConversationSurface {
  const root = container.ownerDocument.createElement('section');
  root.className = 'conversation-surface';
  root.dataset.conversationSurface = 'turn-store';
  container.appendChild(root);
  const removeRenderedNode = (node: HTMLElement): void => {
    if (options.disposeBlock) {
      if (node.hasAttribute('data-block-id')) options.disposeBlock(node);
      node.querySelectorAll<HTMLElement>('[data-block-id]').forEach(
        block => options.disposeBlock!(block),
      );
    }
    node.remove();
  };
  const windowOptions = conversationWindowOptions(options.windowing);
  const ownedScrollAnchor = !options.scrollAnchor && options.scrollViewport
    ? createConversationViewportPort(options.scrollViewport, options.viewport)
    : null;
  const scrollAnchor = options.scrollAnchor ?? ownedScrollAnchor;
  let disposed = false;
  let currentConversationId = '';
  let latestViewModel: ConversationViewModel | null = null;
  let viewportBlockBudget = windowOptions.maxBlocksPerTurn;
  const laneWindows = new Map<string, {
    start: number;
    atTail: boolean;
    firstVisibleTurnId: string;
    loadedTurns: number;
  }>();
  let laneBudgets = new Map<string, number>();
  /* Turn nodes mounted by the current commit.  Counted as they are rendered so
   * the diagnostic `data-dom-turn-count` never needs a full-DOM attribute
   * scan on every frame. */
  let mountedTurnCount = 0;
  let lanesById = new Map<string, ConversationLaneViewModel>();
  /* presentationId → first mounted block index. Only Turns whose block count
   * exceeds the window get an entry; a stored start can only widen the tail
   * window (streaming appends stay visible), clicking the earlier-control
   * rewinds it in batches. Cleared when the conversation changes. */
  const blockWindows = new Map<string, number>();

  const emitIntent = (event: Event): void => {
    const eventTarget = event.target instanceof Element ? event.target : null;
    if (!eventTarget || !root.contains(eventTarget)) return;
    /* Injection-lane thumbnails (steer/peer image attachments) carry their
     * own data-preview-src instead of an action attribute or a positional
     * attachments index — their previews live on the injection block, not the
     * turn's images[]. Resolve them before the action lookup so a bare
     * thumbnail still bridges straight to the lightbox. */
    const previewSource = eventTarget.closest<HTMLElement>('[data-preview-src]');
    if (previewSource) {
      const rawSource = (previewSource.dataset.previewSrc || '').trim();
      const safeSource = /^(?:https?:|blob:)/i.test(rawSource)
        || /^data:image\/(?:avif|gif|jpe?g|png|webp);base64,/i.test(rawSource)
        || /^(?:\/|\.\.?\/)/.test(rawSource)
        ? rawSource
        : '';
      if (safeSource) {
        event.preventDefault();
        options.onPreviewImage?.(safeSource);
      }
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
    if (target.dataset.conversationBlockWindowAction === 'earlier') {
      event.preventDefault();
      expandBlockWindow(target.dataset.turnKey || '');
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

  const renderBlocks = (
    containerNode: HTMLElement,
    blocks: ReadonlyArray<ConversationBlockViewModel>,
    lane: ConversationLaneViewModel,
    turn: ConversationTurnViewModel,
  ): void => {
    const managed = containerNode as ManagedBlocksContainerElement;
    const outline = managed._tofuRoundOutline ??= new RoundOutline(
      containerNode, options.roundOutlineLabel,
    );
    outline.update(turn, blocks);
    const renderVisibleBlock = (node: ManagedBlockElement, block: ConversationBlockViewModel): void => {
      const renderer = options.renderBlock;
      if (!sameBlock(node._tofuBlock, block)
          || node._tofuBlockRenderer !== renderer) {
        (renderer ?? renderDefaultBlock)(node, block, {
          conversationId: currentConversationId, lane, turn,
        });
        node._tofuBlock = block;
        node._tofuBlockRenderer = renderer;
      }
    };

    const existing = directKeyedChildren(containerNode, 'blockId');
    /* placeAt's live-HTMLCollection item(index) is an O(index) walk per block,
     * and every insertion invalidates the engine's collection cache, so a Turn
     * carrying thousands of tool blocks converged quadratically (multi-second
     * commits on long conversations). Blocks arrive in DOM order, so a cursor
     * advanced once per block places each in O(1). */
    let cursor: Element | null = containerNode.firstElementChild;
    const place = (node: HTMLElement): void => {
      if (cursor === node) cursor = node.nextElementSibling;
      else moveRetainedNodeBefore(containerNode, node, cursor);
    };
    place(outline.toolbar);
    blocks.forEach((block) => {

      let node = existing.get(block.blockId) as ManagedBlockElement | undefined;
      if (!node) {
        node = containerNode.ownerDocument.createElement('section') as ManagedBlockElement;
        node.dataset.blockId = block.blockId;
        node.className = 'conversation-block';
      }
      node.dataset.blockKind = block.kind;
      node.dataset.identitySource = block.identitySource;
      // Action summaries stay visible; their renderer defers evidence work
      // behind the sole native disclosure. Prose/answers never join a tool fold.
      outline.patchBlock(node, block);
      renderVisibleBlock(node, block);
      outline.completeBlock(node, block);
      place(node);
      existing.delete(block.blockId);
    });
    for (const node of existing.values()) removeRenderedNode(node);
  };

  const renderLane = (
    laneContainer: HTMLElement,
    lane: ConversationLaneViewModel,
    includeHeader: boolean,
  ): void => {
    laneContainer.dataset.laneId = lane.laneId;
    laneContainer.dataset.laneKind = lane.kind;
    if (includeHeader) {
      laneContainer.className = 'conversation-lane conversation-lane--branch branch-panel';
      laneContainer.dataset.laneExpanded = String(Boolean(lane.expanded));
      laneContainer.dataset.laneLive = String(Boolean(lane.live));
    }
    if (includeHeader) {
      const header = ensurePart(
        laneContainer, 'lane-header', 'header',
      ) as ManagedLaneHeaderElement;
      const renderer = options.renderLaneHeader;
      if (!sameLaneHeader(header._tofuLane, lane)
          || header._tofuLaneHeaderRenderer !== renderer) {
        (renderer ?? renderDefaultLaneHeader)(header, lane, {
          conversationId: currentConversationId,
        });
        header._tofuLane = lane;
        header._tofuLaneHeaderRenderer = renderer;
      }
    }
    const windowState = currentLaneWindowState(lane);
    const visibleTurns = includeHeader && !lane.expanded ? [] : lane.turns.slice(
      windowState.start, windowState.end,
    );
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
    turnsContainer.hidden = includeHeader && !lane.expanded;
    if (includeHeader) turnsContainer.className = 'conversation-lane-turns branch-messages';
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
      mountedTurnCount += 1;
      const presentationId = turn.presentationId || turn.turnId;
      let turnNode = existingTurns.get(
        presentationId,
      ) as ManagedTurnElement | undefined;
      /* Block window is part of a Turn's rendered identity: expanding it must
       * defeat the whole-Turn fast path. */
      const maxBlocks = viewportBlockBudget;
      let blockStart = 0;
      let blockTurnKey = '';
      if (turn.blocks.length > maxBlocks) {
        blockTurnKey = presentationId;
        blockStart = Math.min(
          blockWindows.get(blockTurnKey) ?? Number.POSITIVE_INFINITY,
          Math.max(0, turn.blocks.length - maxBlocks),
        );
        // A window counts renderable blocks, not model rounds. Do not hide one
        // internal note or cut the prose off its first tool and call it history.
        if (blockStart < 4) blockStart = 0;
        else {
          const boundary = blockRoundIdentity(turn.blocks[blockStart]);
          while (boundary && blockStart > 0
              && blockRoundIdentity(turn.blocks[blockStart - 1])?.key === boundary.key) {
            blockStart -= 1;
          }
        }
        blockWindows.set(blockTurnKey, blockStart);
      }
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
          && turnNode._tofuTurnBlockStart === blockStart
          && turnNode._tofuTurnPlanDecision === planDecisionForTurn
          && turnNode._tofuTurnSurfaceOptions === options) {
        if (turnCursor === turnNode) turnCursor = turnNode.nextElementSibling;
        else moveRetainedNodeBefore(turnsContainer, turnNode, turnCursor);
        existingTurns.delete(presentationId);
        return;
      }
      if (!turnNode) {
        turnNode = turnsContainer.ownerDocument.createElement('article');
        turnNode.className = 'conversation-turn';
      }
      turnNode.dataset.presentationId = presentationId;
      turnNode.dataset.turnId = turn.turnId;
      turnNode.dataset.turnStatus = turn.status;
      turnNode.dataset.turnRole = turn.role;
      turnNode.dataset.laneId = turn.laneId;
      turnNode.className = `conversation-turn message${
        turn.actor === 'human' ? ' user-msg' : ''}${
        turn.actor !== 'human'
          && (turn.status === 'failed' || turn.status === 'interrupted')
          ? ' turn-failed' : ''}`;

      const turnContext: ConversationTurnRenderContext = {
        conversationId: currentConversationId,
        lane,
      };
      const avatar = ensurePart(
        turnNode, 'turn-avatar',
      ) as ManagedTurnAvatarElement;
      const avatarRenderer = options.renderTurnAvatar;
      if (!sameTurnAvatar(avatar._tofuTurn, turn)
          || avatar._tofuTurnAvatarRenderer !== avatarRenderer) {
        (avatarRenderer ?? renderDefaultTurnAvatar)(avatar, turn, turnContext);
        avatar._tofuTurn = turn;
        avatar._tofuTurnAvatarRenderer = avatarRenderer;
      }

      const content = ensurePart(turnNode, 'turn-content');
      content.className = 'conversation-turn-content message-content';
      /* Adopt nodes created by the short-lived flat Surface schema. This makes
       * a hot asset swap converge without duplicating one Turn's content.
       * Collapse to a single node per part: if content already owns the part
       * (or a prior swap left more than one flat copy), keep exactly one and
       * drop the rest, otherwise a stray flat footer would persist beside the
       * managed one and render the telemetry strip twice. */
      for (const part of [
        'turn-header', 'turn-blocks-earlier', 'turn-blocks',
        'turn-plan-decision', 'turn-branches', 'turn-actions', 'turn-footer',
      ]) {
        const flatNode = directPart(turnNode, part);
        if (flatNode) {
          const adopted = directPart(content, part);
          if (adopted && adopted !== flatNode) {
            removeRenderedNode(flatNode);
          } else {
            content.appendChild(flatNode);
          }
        }
        let surviving: HTMLElement | null = null;
        for (const node of Array.from(content.children) as HTMLElement[]) {
          if (node.dataset.conversationPart !== part) continue;
          if (surviving) removeRenderedNode(node);
          else surviving = node;
        }
      }
      const header = ensurePart(
        content, 'turn-header', 'header',
      ) as ManagedTurnHeaderElement;
      const headerRenderer = options.renderTurnHeader;
      if (!sameTurnHeader(header._tofuTurn, turn)
          || header._tofuTurnHeaderRenderer !== headerRenderer) {
        (headerRenderer ?? renderDefaultTurnHeader)(header, turn, turnContext);
        header._tofuTurn = turn;
        header._tofuTurnHeaderRenderer = headerRenderer;
      }
      const blocksContainer = ensurePart(content, 'turn-blocks');
      blocksContainer.className = 'conversation-turn-blocks message-body';
      const managedBlocks = blocksContainer as ManagedBlocksContainerElement;
      if (managedBlocks._tofuBlocksSource !== turn.blocks
          || managedBlocks._tofuOutlineStatus !== turn.status
          || managedBlocks._tofuBlocksStart !== blockStart
          || managedBlocks._tofuBlocksRenderer !== options.renderBlock) {
        renderBlocks(
          blocksContainer, turn.blocks.slice(blockStart), lane, turn,
        );
        managedBlocks._tofuBlocksSource = turn.blocks;
        managedBlocks._tofuOutlineStatus = turn.status;
        managedBlocks._tofuBlocksStart = blockStart;
        managedBlocks._tofuBlocksRenderer = options.renderBlock;
      }
      blocksContainer.hidden = turn.blocks.length === 0;
      const blocksEarlier = ensurePart(content, 'turn-blocks-earlier', 'nav');
      blocksEarlier.hidden = blockStart === 0;
      const blocksEarlierButton = ensurePart(
        blocksEarlier, 'turn-blocks-earlier-button', 'button',
      ) as HTMLButtonElement;
      blocksEarlierButton.type = 'button';
      blocksEarlierButton.disabled = blockStart === 0;
      blocksEarlierButton.dataset.conversationBlockWindowAction = 'earlier';
      blocksEarlierButton.dataset.turnKey = blockTurnKey;
      const earlierCount = Math.min(
        windowOptions.blockBatchSize, blockStart,
      );
      blocksEarlierButton.dataset.blockCount = String(earlierCount);
      blocksEarlierButton.textContent = options.windowing?.blockLabel?.(
        earlierCount,
      ) ?? `Show ${earlierCount} earlier blocks`;
      let planDecisionNode = directPart(
        content, 'turn-plan-decision',
      ) as ManagedPlanDecisionElement | null;
      if (activePlanDecision?.sourceTurnId === turn.turnId) {
        planDecisionNode = planDecisionNode ?? ensurePart(
          content, 'turn-plan-decision', 'aside',
        ) as ManagedPlanDecisionElement;
        const planDecisionRenderer = options.renderPlanDecision;
        if (!samePlanDecision(
          planDecisionNode._tofuPlanDecision, activePlanDecision,
        ) || planDecisionNode._tofuPlanDecisionRenderer
            !== planDecisionRenderer) {
          (planDecisionRenderer ?? renderDefaultPlanDecision)(
            planDecisionNode,
            activePlanDecision,
            turnContext,
          );
          planDecisionNode._tofuPlanDecision = activePlanDecision;
          planDecisionNode._tofuPlanDecisionRenderer = planDecisionRenderer;
        }
      } else {
        planDecisionNode?.remove();
        planDecisionNode = null;
      }
      const actions = ensurePart(
        content, 'turn-actions',
      ) as ManagedTurnActionsElement;
      const actionsRenderer = options.renderTurnActions;
      if (!sameTurnActions(actions._tofuTurn, turn)
          || actions._tofuTurnActionsRenderer !== actionsRenderer) {
        (actionsRenderer ?? renderDefaultTurnActions)(actions, turn, turnContext);
        actions._tofuTurn = turn;
        actions._tofuTurnActionsRenderer = actionsRenderer;
      }
      actions.hidden = !hasRenderableContent(actions);
      const footer = ensurePart(
        content, 'turn-footer', 'footer',
      ) as ManagedTurnFooterElement;
      const footerRenderer = options.renderTurnFooter;
      if (!sameTurnFooter(footer._tofuTurn, turn)
          || footer._tofuTurnFooterRenderer !== footerRenderer) {
        (footerRenderer ?? renderDefaultTurnFooter)(footer, turn, turnContext);
        footer._tofuTurn = turn;
        footer._tofuTurnFooterRenderer = footerRenderer;
      }
      footer.hidden = !hasRenderableContent(footer);
      let branchesContainer = directPart(content, 'turn-branches');
      if (turn.branches.length) {
        branchesContainer = branchesContainer
          ?? ensurePart(content, 'turn-branches');
        const existingLanes = directKeyedChildren(branchesContainer, 'laneId');
        turn.branches.forEach((branch, branchIndex) => {
          let branchNode = existingLanes.get(branch.laneId);
          if (!branchNode) {
            branchNode = branchesContainer!.ownerDocument.createElement('section');
            branchNode.className = 'conversation-lane conversation-lane--branch';
            branchNode.dataset.laneId = branch.laneId;
          }
          renderLane(branchNode, branch, true);
          placeAt(branchesContainer!, branchNode, branchIndex);
          existingLanes.delete(branch.laneId);
        });
        for (const staleLane of existingLanes.values()) removeRenderedNode(staleLane);
      } else {
        if (branchesContainer) removeRenderedNode(branchesContainer);
        branchesContainer = null;
      }

      /* The content order mirrors the retained message contract. The plan
       * decision stays attached to its authorizing plan, while branch history
       * remains above the turn's action and finish shelves. */
      let contentPartIndex = 0;
      placeManagedAt(content, header, contentPartIndex++);
      placeManagedAt(content, blocksEarlier, contentPartIndex++);
      placeManagedAt(content, blocksContainer, contentPartIndex++);
      if (planDecisionNode) {
        placeManagedAt(content, planDecisionNode, contentPartIndex++);
      }
      if (branchesContainer) {
        placeManagedAt(content, branchesContainer, contentPartIndex++);
      }
      placeManagedAt(content, actions, contentPartIndex++);
      placeManagedAt(content, footer, contentPartIndex++);

      const contextBlock = turn.blocks.find(
        (block): block is Extract<ConversationBlockViewModel, { kind: 'context' }> => (
          block.kind === 'context'
        ),
      ) ?? null;
      const contextRail = ensurePart(
        turnNode, 'turn-context-rail', 'aside',
      ) as ManagedTurnContextRailElement;
      const contextRailRenderer = options.renderTurnContextRail;
      if (!contextRail._tofuTurnContextRailRendered
          || contextRail._tofuContextBlock?.source !== contextBlock?.source
          || contextRail._tofuTurnContextRailRenderer !== contextRailRenderer) {
        (contextRailRenderer ?? renderDefaultTurnContextRail)(
          contextRail, contextBlock, turnContext,
        );
        contextRail._tofuContextBlock = contextBlock ?? undefined;
        contextRail._tofuTurnContextRailRenderer = contextRailRenderer;
        contextRail._tofuTurnContextRailRendered = true;
      }
      contextRail.hidden = !hasRenderableContent(contextRail);

      placeAt(turnNode, avatar, 0);
      placeAt(turnNode, content, 1);
      placeAt(turnNode, contextRail, 2);
      turnNode._tofuTurnVm = turn;
      turnNode._tofuTurnBlockStart = blockStart;
      turnNode._tofuTurnPlanDecision = planDecisionForTurn;
      turnNode._tofuTurnSurfaceOptions = options;
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
    if (laneWindow) laneWindow.firstVisibleTurnId = visibleTurns[0]?.turnId ?? '';
  };

  const renderQueue = (
    queueContainer: HTMLElement,
    queue: ReadonlyArray<ConversationQueueItemViewModel>,
  ): void => {
    const existing = directKeyedChildren(queueContainer, 'queueId');
    queue.forEach((item, index) => {
      let node = existing.get(item.queueId) as ManagedQueueElement | undefined;
      if (!node) {
        node = queueContainer.ownerDocument.createElement('article');
        node.className = 'conversation-queue-item';
        node.dataset.queueId = item.queueId;
      }
      const renderer = options.renderQueueItem;
      if (!sameQueueItem(node._tofuQueueItem, item)
          || node._tofuQueueRenderer !== renderer) {
        (renderer ?? renderDefaultQueueItem)(node, item, currentConversationId);
        node._tofuQueueItem = item;
        node._tofuQueueRenderer = renderer;
      }
      node.dataset.queueId = item.queueId;
      placeAt(queueContainer, node, index);
      existing.delete(item.queueId);
    });
    for (const stale of existing.values()) removeRenderedNode(stale);
  };

  const maxWindowStart = (total: number, maxTurns: number): number => (
    Math.max(0, total - maxTurns)
  );

  const collectLanes = (viewModel: ConversationViewModel): {
    all: Map<string, ConversationLaneViewModel>;
    expanded: ConversationLaneViewModel[];
  } => {
    const all = new Map<string, ConversationLaneViewModel>();
    const expanded: ConversationLaneViewModel[] = [];
    const visit = (lane: ConversationLaneViewModel, main = false): void => {
      if (all.has(lane.laneId)) return;
      all.set(lane.laneId, lane);
      if (!main && !lane.expanded) return;
      expanded.push(lane);
      for (const turn of lane.turns) {
        for (const branch of turn.branches) visit(branch);
      }
    };
    visit(viewModel.mainLane, true);
    for (const lane of viewModel.orphanLanes) visit(lane);
    return { all, expanded };
  };

  const prepareLaneWindows = (
    viewModel: ConversationViewModel,
    reset: boolean,
  ): void => {
    const lanes = collectLanes(viewModel);
    lanesById = lanes.all;
    laneBudgets = new Map<string, number>();
    const laneCount = Math.max(1, lanes.expanded.length);
    // Read geometry once before DOM reconciliation, never between insertions.
    const viewportHeight = options.scrollViewport?.clientHeight ?? 0;
    viewportBlockBudget = viewportHeight > 0
      ? Math.min(windowOptions.maxBlocksPerTurn, Math.max(
        CONVERSATION_BLOCK_INITIAL_MINIMUM,
        Math.ceil(viewportHeight / CONVERSATION_BLOCK_ESTIMATED_HEIGHT)
          * CONVERSATION_BLOCK_OVERSCAN_VIEWPORTS,
      ))
      : windowOptions.maxBlocksPerTurn;
    const visibleTurnBudget = viewportHeight > 0
      ? Math.min(windowOptions.maxTurns, Math.max(
        CONVERSATION_TURN_INITIAL_MINIMUM,
        Math.ceil(viewportHeight / CONVERSATION_TURN_ESTIMATED_HEIGHT)
          * CONVERSATION_BLOCK_OVERSCAN_VIEWPORTS,
      ))
      : windowOptions.maxTurns;
    const base = Math.floor(visibleTurnBudget / laneCount);
    let remainder = visibleTurnBudget % laneCount;
    for (const lane of lanes.expanded) {
      const budget = base + (remainder > 0 ? 1 : 0);
      if (remainder > 0) remainder -= 1;
      laneBudgets.set(lane.laneId, budget);
    }
    for (const [laneId, lane] of lanes.all) {
      const maxTurns = laneBudgets.get(laneId) ?? 0;
      const maximumStart = maxWindowStart(lane.turns.length, maxTurns);
      const previous = laneWindows.get(laneId);
      let start = previous?.start ?? maximumStart;
      const atTail = reset || !previous ? true : previous.atTail;
      if (atTail) start = maximumStart;
      else if (previous?.firstVisibleTurnId) {
        const anchoredIndex = lane.turns.findIndex(
          (turn) => turn.turnId === previous.firstVisibleTurnId,
        );
        if (anchoredIndex >= 0) {
          const prepended = lane.turns.length > previous.loadedTurns
            && anchoredIndex > previous.start && previous.start === 0;
          start = prepended
            ? Math.max(0, anchoredIndex - windowOptions.batchSize)
            : anchoredIndex;
        }
      }
      laneWindows.set(laneId, {
        start: Math.min(maximumStart, Math.max(0, start)),
        atTail,
        firstVisibleTurnId: previous?.firstVisibleTurnId ?? '',
        loadedTurns: lane.turns.length,
      });
    }
    for (const laneId of [...laneWindows.keys()]) {
      if (!lanes.all.has(laneId)) laneWindows.delete(laneId);
    }
  };

  const currentLaneWindowState = (
    lane: ConversationLaneViewModel,
  ): ConversationWindowState => {
    const maxTurns = laneBudgets.get(lane.laneId) ?? 0;
    const laneWindow = laneWindows.get(lane.laneId) ?? {
      start: maxWindowStart(lane.turns.length, maxTurns),
      atTail: true,
      firstVisibleTurnId: '',
      loadedTurns: lane.turns.length,
    };
    laneWindows.set(lane.laneId, laneWindow);
    const start = Math.min(
      laneWindow.start, maxWindowStart(lane.turns.length, maxTurns),
    );
    const batchSize = maxTurns > 0
      ? Math.min(windowOptions.batchSize, maxTurns) : 0;
    return {
      start,
      end: Math.min(lane.turns.length, start + maxTurns),
      total: lane.turns.length,
      maxTurns,
      batchSize,
    };
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

  const renderWindowControl = (
    laneNode: HTMLElement,
    laneId: string,
    direction: 'earlier' | 'later',
    count: number,
  ): void => {
    const control = ensurePart(
      laneNode, `turn-window-${direction}`, 'nav',
    );
    control.hidden = count === 0;
    control.dataset.windowDirection = direction;
    const button = ensurePart(
      control,
      'turn-window-button',
      'button',
    ) as HTMLButtonElement;
    button.type = 'button';
    button.disabled = count === 0;
    button.dataset.conversationWindowAction = direction;
    button.dataset.laneId = laneId;
    button.dataset.turnCount = String(count);
    button.textContent = options.windowing?.label?.(direction, count)
      ?? String(count);
  };

  const commit = (): void => {
    const viewModel = latestViewModel;
    if (!viewModel) return;
    mountedTurnCount = 0;
    const anchor = scrollAnchor?.capture(root);
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
      transportNotice.className = 'conversation-surface__transport-notice'
        + ` conversation-surface__transport-notice--${noticeState}`;
      transportNotice.dataset.syncState = noticeState;
      transportNotice.setAttribute('role', 'status');
      const label = options.transportNoticeLabel?.(noticeState)
        ?? DEFAULT_TRANSPORT_NOTICE_LABELS[noticeState];
      if (transportNotice.textContent !== label) {
        transportNotice.textContent = label;
      }
    }
    placeAt(root, transportNotice, 0);
    const main = ensurePart(root, 'main-lane');
    main.className = 'conversation-lane conversation-lane--main';
    renderLane(main, viewModel.mainLane, false);
    const orphans = ensurePart(root, 'orphan-lanes');
    const existingOrphans = directKeyedChildren(orphans, 'laneId');
    viewModel.orphanLanes.forEach((lane, index) => {
      let laneNode = existingOrphans.get(lane.laneId);
      if (!laneNode) {
        laneNode = orphans.ownerDocument.createElement('section');
        laneNode.className = 'conversation-lane conversation-lane--orphan';
        laneNode.dataset.laneId = lane.laneId;
      }
      renderLane(laneNode, lane, true);
      placeAt(orphans, laneNode, index);
      existingOrphans.delete(lane.laneId);
    });
    for (const stale of existingOrphans.values()) removeRenderedNode(stale);
    renderQueue(ensurePart(root, 'queue'), viewModel.queue);
    root.dataset.domTurnCount = String(mountedTurnCount);
    if (anchor !== undefined) scrollAnchor?.restore(root, anchor);
  };

  const expandBlockWindow = (turnKey: string): void => {
    const current = blockWindows.get(turnKey);
    if (current == null) return;
    const next = Math.max(0, current - windowOptions.blockBatchSize);
    if (next === current) return;
    blockWindows.set(turnKey, next);
    commit();
  };

  const moveWindow = (
    direction: 'earlier' | 'later',
    laneId = 'main',
  ): boolean => {
    const lane = lanesById.get(laneId);
    if (!latestViewModel || !lane) return false;
    const state = currentLaneWindowState(lane);
    if (state.maxTurns <= 0) return false;
    const laneWindow = laneWindows.get(laneId);
    if (!laneWindow) return false;
    const nextStart = Math.max(0, Math.min(
      maxWindowStart(state.total, state.maxTurns),
      laneWindow.start + (direction === 'earlier'
        ? -state.batchSize : state.batchSize),
    ));
    if (nextStart === laneWindow.start) {
      if (direction === 'earlier' && lane.history?.hasMore
          && lane.history.nextBeforeOrdinal !== null) {
        options.onIntent?.({
          type: 'load-earlier-turns',
          conversationId: currentConversationId,
          laneId,
          beforeOrdinal: lane.history.nextBeforeOrdinal,
          limit: CONVERSATION_HISTORY_PAGE_TURNS,
        });
        return true;
      }
      return false;
    }
    laneWindow.start = nextStart;
    laneWindow.atTail = nextStart === maxWindowStart(state.total, state.maxTurns);
    commit();
    return true;
  };

  return {
    root,
    get windowState() { return currentWindowState(); },
    render(viewModel) {
      if (disposed) throw new Error('ConversationSurface is disposed.');
      const previous = latestViewModel;
      const reset = !previous || previous.conversationId !== viewModel.conversationId;
      if (reset) blockWindows.clear();
      prepareLaneWindows(viewModel, reset);
      latestViewModel = viewModel;
      commit();
    },
    showEarlier() { return moveWindow('earlier', 'main'); },
    showLater() { return moveWindow('later', 'main'); },
    revealTurn(turnId) {
      if (disposed || !latestViewModel) return false;
      const lane = latestViewModel.mainLane;
      const index = lane.turns.findIndex(turn => turn.turnId === turnId);
      const state = currentLaneWindowState(lane);
      if (index < 0 || state.maxTurns <= 0) return false;
      scrollAnchor?.suspendFollow?.();
      if (index >= state.start && index < state.end) return true;
      const laneWindow = laneWindows.get(lane.laneId)!;
      laneWindow.start = Math.min(index, maxWindowStart(state.total, state.maxTurns));
      laneWindow.atTail = laneWindow.start === maxWindowStart(state.total, state.maxTurns);
      laneWindow.firstVisibleTurnId = lane.turns[laneWindow.start]?.turnId ?? '';
      commit();
      return true;
    },
    followLatest() {
      if (latestViewModel) {
        const lane = latestViewModel.mainLane;
        const state = currentLaneWindowState(lane);
        const laneWindow = laneWindows.get(lane.laneId)!;
        laneWindow.start = maxWindowStart(state.total, state.maxTurns);
        laneWindow.atTail = true;
        commit();
      }
      scrollAnchor?.followLatest?.(root);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      root.removeEventListener('click', emitIntent);
      ownedScrollAnchor?.dispose?.();
      removeRenderedNode(root);
    },
  };
}
