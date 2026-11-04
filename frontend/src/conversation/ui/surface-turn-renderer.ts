/** Keyed Turn content renderer owned by ConversationSurface. Composes block
 * reuse, round controls, the reply tool panel header and part render ports.
 * Surface supplies grouped entries, window ranges, panel state and navigation;
 * no store, transport or scroll state. */
import type {
  ConversationBlockViewModel, ConversationLaneViewModel, ConversationTurnViewModel,
  PlanDecisionViewModel,
} from '../presentation/conversation-view-model';
import {
  groupedTurnLayout, interimNarrationIds, turnLayoutZone, type GroupedTurnEntry, type ToolPanelEntry,
} from '../presentation/grouped-turn-layout';
import type { ConversationSurfaceOptions, ConversationTurnRenderContext } from './surface-contracts';
import type { ConversationSurfaceDesign, SurfaceRoundOutline } from './surface-design-contracts';
import { moveRetainedNodeBefore } from './rich-card-reconciliation';
import { endAnchoredScroller, setStableData } from './renderers/stable-dom';
import { finishSurfaceCleanup, releaseSurfacePresentation } from './surface-node-lifecycle';
import {
  sameBlock, sameTurnAvatar, sameTurnHeader, sameTurnActions, sameTurnFooter, samePlanDecision,
} from './surface-presentation-equality';
import {
  type ManagedBlockElement, type ManagedBlocksContainerElement, type ManagedTurnAvatarElement,
  type ManagedTurnHeaderElement, type ManagedTurnActionsElement, type ManagedTurnFooterElement,
  type ManagedTurnContextRailElement, type ManagedPlanDecisionElement, type ManagedTurnElement,
  type SurfaceRegionWindow,
  createSurfaceDom, directPart, narrationWellBody, placeAt, placeManagedAt, hasRenderableContent,
} from './surface-dom';

export interface SurfaceTurnRenderPlan {
  /** Mounted order: grouped zones for replies, authored order otherwise. */
  visibleBlocks: ReadonlyArray<GroupedTurnEntry>;
  /** One window per region; entries outside every region stay mounted. */
  windows: ReadonlyArray<SurfaceRegionWindow>;
  windowSignature: string;
  planDecisionForTurn: PlanDecisionViewModel | null;
  /** Fold of the reply's narration well: reader choice, else layout default. */
  narrationOpen: boolean;
}

interface SurfaceTurnRendererPorts {
  conversationId(): string;
  revealBlock(laneId: string, presentationId: string, blockId: string): void;
  renderLane(container: HTMLElement, lane: ConversationLaneViewModel, includeHeader: boolean): void;
  renderBlockBoundary(content: HTMLElement, key: string,
    direction: 'earlier' | 'later', count: number): HTMLElement;
  renderToolPanelBoundary(node: HTMLElement, window: SurfaceRegionWindow,
    direction: 'earlier' | 'later', count: number): void;
}

export function createSurfaceTurnRenderer(
  options: ConversationSurfaceOptions,
  design: ConversationSurfaceDesign,
  ports: SurfaceTurnRendererPorts,
) {
  const removeRenderedNode = (node: HTMLElement): void => {
    finishSurfaceCleanup([
      () => {
        if (!options.disposeBlock) return;
        const blocks = [...node.querySelectorAll<HTMLElement>('[data-block-id]')];
        if (node.hasAttribute('data-block-id')) blocks.unshift(node);
        finishSurfaceCleanup(blocks.map(block => () => options.disposeBlock!(block)));
      },
      () => releaseSurfacePresentation(node),
      () => node.remove(),
    ]);
  };
  const dom = createSurfaceDom(nodes => finishSurfaceCleanup(
    nodes.map(node => () => removeRenderedNode(node)),
  ));
  const { ensurePart, directKeyedChildren } = dom;

  /* Surface chrome, not content: renderers and the outline never see it. The
   * button is retained across repaint, so focus survives a toggle. Every commit
   * of a live Turn repaints its entries, so an unchanged value is never
   * rewritten (an equal write still queues a mutation and restyles the node). */
  const renderToolPanel = (node: HTMLElement, panel: ToolPanelEntry): void => {
    setStableData(node, 'blockKind', 'tool-panel');
    setStableData(node, 'toolPanelState', panel.open ? 'open' : 'closed');
    setStableData(node, 'toolPanelActive', panel.active ? 'true' : undefined);
    const toggle = design.toolPanelHeader(node, panel);
    setStableData(toggle, 'conversationToolPanel', 'toggle');
    const expanded = String(panel.open);
    if (toggle.getAttribute('aria-expanded') !== expanded) toggle.setAttribute('aria-expanded', expanded);
  };

  // Construct handlers in an ID-only scope, never the current render frame.
  const createBlockOutline = (container: HTMLElement, laneId: string, presentationId: string): SurfaceRoundOutline => (
    design.createRoundOutline(container,
      blockId => ports.revealBlock(laneId, presentationId, blockId))
  );

  const renderBlocks = (
    containerNode: HTMLElement,
    visibleBlocks: ReadonlyArray<GroupedTurnEntry>,
    windows: ReadonlyArray<SurfaceRegionWindow>,
    lane: ConversationLaneViewModel,
    turn: ConversationTurnViewModel,
    narrationOpen: boolean,
  ): void => {
    /* Entries outside every region (a grouped reply's head and trail) are
     * always mounted, so reasoning and the panel header never blink out while
     * a region's window follows its live tail. */
    const entries: GroupedTurnEntry[] = [];
    const regionOf = new Map<string, SurfaceRegionWindow>();
    let regionIndex = 0;
    visibleBlocks.forEach((entry, index) => {
      while (regionIndex < windows.length
          && index >= windows[regionIndex].regionStart + windows[regionIndex].length) regionIndex += 1;
      const window = windows[regionIndex];
      if (window && index >= window.regionStart) {
        const offset = index - window.regionStart;
        if (offset < window.start || offset >= window.end) return;
        regionOf.set(entry.blockId, window);
      }
      entries.push(entry);
    });
    const managed = containerNode as ManagedBlocksContainerElement;
    const outline = managed._tofuRoundOutline ??= createBlockOutline(
      containerNode, lane.laneId, turn.presentationId || turn.turnId,
    );
    outline.update(turn, entries);
    const grouped = groupedTurnLayout(turn) !== null;
    const renderVisibleBlock = (node: ManagedBlockElement, block: ConversationBlockViewModel): void => {
      const renderer = design.renderers.renderBlock;
      if (!sameBlock(node._tofuBlock, block)
          || node._tofuBlockRenderer !== renderer) {
        renderer(node, block, {
          conversationId: ports.conversationId(), lane, turn,
        });
        node._tofuBlockRenderer = renderer;
      }
      node._tofuBlock = block;
    };

    const existing = directKeyedChildren(containerNode, 'blockId');
    const staleBoundaries = new Set(
      containerNode.querySelectorAll<HTMLElement>(':scope > [data-region-boundary]'),
    );
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

    /* Interim round prose shares ONE bounded, scrolling well so a long run of
     * rounds never stretches the reply. Only settled text ever joins (the
     * latest text stays outside while it may stream), so a leaf is reparented
     * at most once, when a later round's text arrives. */
    const wellIds = grouped ? interimNarrationIds(entries) : new Set<string>();
    let well = containerNode.querySelector<HTMLElement>(':scope > [data-narration-well]');
    let wellBody = well && narrationWellBody(well);
    let wellPlaced = false;
    let wellCursor: Element | null = null;
    const placeInWell = (node: HTMLElement): void => {
      if (!well || !wellBody) {
        well = design.narrationWell(containerNode.ownerDocument);
        wellBody = narrationWellBody(well)!;
      }
      if (!wellPlaced) {
        place(well);
        wellPlaced = true;
        wellCursor = wellBody.firstElementChild;
      }
      // A node joining the well may be the outer cursor; never leave the
      // cursor pointing inside the body.
      if (cursor === node) cursor = node.nextElementSibling;
      if (wellCursor === node) wellCursor = node.nextElementSibling;
      else moveRetainedNodeBefore(wellBody, node, wellCursor);
    };

    /* A grouped reply's calls announce what their window hides in place,
     * inside the card: before the first mounted call and after the last.
     * Prose and ungrouped Turns keep scroll residency and the outer keyboard
     * fallback, so reading never meets a visible paging row. */
    const boundary = (window: SurfaceRegionWindow, direction: 'earlier' | 'later'): void => {
      if (window.kind !== 'process') return;
      const count = direction === 'earlier' ? window.start : window.length - window.end;
      if (count <= 0) return;
      const id = `${window.kind}:${direction}`;
      let node = containerNode.querySelector<HTMLElement>(`:scope > [data-region-boundary="${id}"]`);
      if (!node) {
        node = containerNode.ownerDocument.createElement('div');
        node.dataset.regionBoundary = id;
      }
      setStableData(node, 'turnZone', 'process');
      setStableData(node, 'hiddenCount', String(count));
      ports.renderToolPanelBoundary(node, window, direction, count);
      staleBoundaries.delete(node);
      place(node);
    };
    let openRegion: SurfaceRegionWindow | undefined;
    entries.forEach((block) => {
      const region = regionOf.get(block.blockId);
      if (openRegion && region !== openRegion) {
        boundary(openRegion, 'later');
        openRegion = undefined;
      }
      if (region && region !== openRegion) {
        boundary(region, 'earlier');
        openRegion = region;
      }

      let node = existing.get(block.blockId) as ManagedBlockElement | undefined;
      if (!node) {
        node = containerNode.ownerDocument.createElement('section') as ManagedBlockElement;
        node.dataset.blockId = block.blockId;
        design.decorate(node, 'block');
      }
      if (block.kind === 'tool-panel') {
        renderToolPanel(node, block);
        place(node);
        existing.delete(block.blockId);
        return;
      }
      setStableData(node, 'blockKind', block.kind);
      setStableData(node, 'identitySource', block.identitySource);
      const zone = grouped ? turnLayoutZone(block) : undefined;
      setStableData(node, 'turnZone', zone);
      setStableData(node, 'blockRegion', region?.kind);
      // Action summaries stay visible; their renderer defers evidence work
      // behind the sole native disclosure. Prose/answers never join a tool fold.
      outline.patchBlock(node, block);
      renderVisibleBlock(node, block);
      outline.completeBlock(node, block);
      const placeBlock = wellIds.has(block.blockId) ? placeInWell : place;
      for (const control of outline.beforeBlock(block.blockId)) {
        // Round controls sit among the calls: they belong to the panel card.
        setStableData(control, 'turnZone', zone);
        placeBlock(control);
      }
      placeBlock(node);
      existing.delete(block.blockId);
    });
    if (openRegion) boundary(openRegion, 'later');
    for (const node of existing.values()) removeRenderedNode(node);
    for (const node of staleBoundaries) node.remove();
    if (well && wellBody && wellPlaced) {
      /* Surface chrome like the panel header: the retained toggle keeps focus
       * across a fold. A folded body stays mounted, so its members keep their
       * identity and the well never reparents a leaf to fold it. */
      const toggle = design.paintNarrationWell(well, wellIds.size);
      setStableData(toggle, 'conversationNarration', 'toggle');
      const expanded = String(narrationOpen);
      if (toggle.getAttribute('aria-expanded') !== expanded) toggle.setAttribute('aria-expanded', expanded);
      const opening = narrationOpen && well.dataset.narrationState === 'closed';
      setStableData(well, 'narrationState', narrationOpen ? 'open' : 'closed');
      if (wellBody.hidden === narrationOpen) wellBody.hidden = !narrationOpen;
      // Newest note first: the well opens at its end and follows growth until
      // the reader scrolls up inside it; every reopening rests at the end again.
      const end = endAnchoredScroller(wellBody, wellBody);
      if (opening) end.pin();
      else end.follow();
    } else if (well) {
      if (wellBody) endAnchoredScroller(wellBody, wellBody).dispose();
      // Members already moved out or retired above; only the empty shell remains.
      for (const node of Array.from(wellBody?.children ?? []) as HTMLElement[]) removeRenderedNode(node);
      well.remove();
    }
  };

  const renderTurn = (turnNode: ManagedTurnElement, turn: ConversationTurnViewModel,
    lane: ConversationLaneViewModel, plan: SurfaceTurnRenderPlan): void => {
    const { visibleBlocks, windows, windowSignature, planDecisionForTurn, narrationOpen } = plan;
    const activePlanDecision = planDecisionForTurn;
    const presentationId = turn.presentationId || turn.turnId;
    setStableData(turnNode, 'presentationId', presentationId);
    setStableData(turnNode, 'turnId', turn.turnId);
    setStableData(turnNode, 'turnStatus', turn.status);
    setStableData(turnNode, 'turnRole', turn.role);
    setStableData(turnNode, 'laneId', turn.laneId);
    design.turnShell(turnNode, turn);

    const turnContext: ConversationTurnRenderContext = {
      conversationId: ports.conversationId(),
      lane,
    };
    const avatar = ensurePart(
      turnNode, 'turn-avatar',
    ) as ManagedTurnAvatarElement;
    const avatarRenderer = design.renderers.renderTurnAvatar;
    if (!sameTurnAvatar(avatar._tofuTurn, turn)
        || avatar._tofuTurnAvatarRenderer !== avatarRenderer) {
      avatarRenderer(avatar, turn, turnContext);
      avatar._tofuTurnAvatarRenderer = avatarRenderer;
    }
    avatar._tofuTurn = turn;

    const content = ensurePart(turnNode, 'turn-content');
    design.decorate(content, 'turn-content');
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
    const headerRenderer = design.renderers.renderTurnHeader;
    if (!sameTurnHeader(header._tofuTurn, turn)
        || header._tofuTurnHeaderRenderer !== headerRenderer) {
      headerRenderer(header, turn, turnContext);
      header._tofuTurnHeaderRenderer = headerRenderer;
    }
    header._tofuTurn = turn;
    const blocksContainer = ensurePart(content, 'turn-blocks');
    design.decorate(blocksContainer, 'turn-blocks');
    const managedBlocks = blocksContainer as ManagedBlocksContainerElement;
    if (managedBlocks._tofuBlocksSource !== visibleBlocks
        || managedBlocks._tofuOutlineStatus !== turn.status
        || managedBlocks._tofuBlocksSignature !== windowSignature
        || managedBlocks._tofuNarrationOpen !== narrationOpen
        || managedBlocks._tofuBlocksRenderer !== design.renderers.renderBlock) {
      renderBlocks(blocksContainer, visibleBlocks, windows, lane, turn, narrationOpen);
      managedBlocks._tofuBlocksSource = visibleBlocks;
      managedBlocks._tofuOutlineStatus = turn.status;
      managedBlocks._tofuBlocksSignature = windowSignature;
      managedBlocks._tofuNarrationOpen = narrationOpen;
      managedBlocks._tofuBlocksRenderer = design.renderers.renderBlock;
    }
    managedBlocks._tofuBlockWindows = windows;
    const noBlocks = visibleBlocks.length === 0;
    if (blocksContainer.hidden !== noBlocks) blocksContainer.hidden = noBlocks;
    // The outer paging boundaries serve an ungrouped Turn or a grouped
    // reply's prose; a grouped reply's calls page inside the card.
    const outer = windows.find(window => window.kind !== 'process');
    const blocksEarlier = ports.renderBlockBoundary(content, outer?.key ?? '', 'earlier', outer?.start ?? 0);
    const blocksLater = ports.renderBlockBoundary(content, outer?.key ?? '', 'later',
      outer ? outer.length - outer.end : 0);
    let planDecisionNode = directPart(
      content, 'turn-plan-decision',
    ) as ManagedPlanDecisionElement | null;
    if (activePlanDecision?.sourceTurnId === turn.turnId) {
      planDecisionNode = planDecisionNode ?? ensurePart(
        content, 'turn-plan-decision', 'aside',
      ) as ManagedPlanDecisionElement;
      const planDecisionRenderer = design.renderers.renderPlanDecision;
      if (!samePlanDecision(
        planDecisionNode._tofuPlanDecision, activePlanDecision,
      ) || planDecisionNode._tofuPlanDecisionRenderer
          !== planDecisionRenderer) {
        planDecisionRenderer(
          planDecisionNode,
          activePlanDecision,
          turnContext,
        );
        planDecisionNode._tofuPlanDecisionRenderer = planDecisionRenderer;
      }
      planDecisionNode._tofuPlanDecision = activePlanDecision;
    } else {
      planDecisionNode?.remove();
      planDecisionNode = null;
    }
    const actions = ensurePart(
      content, 'turn-actions',
    ) as ManagedTurnActionsElement;
    const actionsRenderer = design.renderers.renderTurnActions;
    if (!sameTurnActions(actions._tofuTurn, turn)
        || actions._tofuTurnActionsRenderer !== actionsRenderer) {
      actionsRenderer(actions, turn, turnContext);
      actions._tofuTurnActionsRenderer = actionsRenderer;
    }
    actions._tofuTurn = turn;
    const noActions = !hasRenderableContent(actions);
    if (actions.hidden !== noActions) actions.hidden = noActions;
    const footer = ensurePart(
      content, 'turn-footer', 'footer',
    ) as ManagedTurnFooterElement;
    const footerRenderer = design.renderers.renderTurnFooter;
    if (!sameTurnFooter(footer._tofuTurn, turn)
        || footer._tofuTurnFooterRenderer !== footerRenderer) {
      footerRenderer(footer, turn, turnContext);
      footer._tofuTurnFooterRenderer = footerRenderer;
    }
    footer._tofuTurn = turn;
    const noFooter = !hasRenderableContent(footer);
    if (footer.hidden !== noFooter) footer.hidden = noFooter;
    let branchesContainer = directPart(content, 'turn-branches');
    if (turn.branches.length) {
      branchesContainer = branchesContainer
        ?? ensurePart(content, 'turn-branches');
      const existingLanes = directKeyedChildren(branchesContainer, 'laneId');
      turn.branches.forEach((branch, branchIndex) => {
        let branchNode = existingLanes.get(branch.laneId);
        if (!branchNode) {
          branchNode = branchesContainer!.ownerDocument.createElement('section');
          design.decorate(branchNode, 'branch-lane');
          branchNode.dataset.laneId = branch.laneId;
        }
        ports.renderLane(branchNode, branch, true);
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
    placeManagedAt(content, blocksLater, contentPartIndex++);
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
    const contextRailRenderer = design.renderers.renderTurnContextRail;

    /* The rail is the track its sticky card travels along (info-rail.js
     * measures the track); the card holds the rendered facts and the
     * background-agent section, so both move with the reader. */
    const contextCard = ensurePart(contextRail, 'turn-context-card');
    const contextContent = ensurePart(contextCard, 'turn-context-content');
    if (!contextRail._tofuTurnContextRailRendered
        || contextRail._tofuContextBlock?.source !== contextBlock?.source
        || contextRail._tofuTurnContextRailRenderer !== contextRailRenderer) {
      contextRailRenderer(
        contextContent, contextBlock, turnContext,
      );
      contextRail._tofuTurnContextRailRenderer = contextRailRenderer;
      contextRail._tofuTurnContextRailRendered = true;
    }
    contextRail._tofuContextBlock = contextBlock ?? undefined;
    design.decorate(contextRail, 'context-rail');
    design.decorate(contextCard, 'context-card');
    design.decorate(contextContent, 'context-content');
    const noContext = !Array.from(contextCard.children).some(child =>
      !(child as HTMLElement).hidden && hasRenderableContent(child as HTMLElement));
    if (contextRail.hidden !== noContext) contextRail.hidden = noContext;

    placeAt(turnNode, avatar, 0);
    placeAt(turnNode, content, 1);
    placeAt(turnNode, contextRail, 2);
    turnNode._tofuTurnVm = turn;
    turnNode._tofuTurnEntries = visibleBlocks;
    turnNode._tofuTurnWindowSignature = windowSignature;
    turnNode._tofuTurnPlanDecision = planDecisionForTurn;
    turnNode._tofuTurnNarrationOpen = narrationOpen;
    turnNode._tofuTurnSurfaceOptions = options;
  };

  return { dom, renderTurn, removeNode: removeRenderedNode };
}
