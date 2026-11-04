/**
 * Keyed child and singleton slot reconciliation for the conversation surface.
 * Preserves unmanaged editor hosts and retained-node focus.
 */
import type {
  ConversationBlockViewModel,
  ConversationLaneViewModel,
  ConversationQueueItemViewModel,
  ConversationTurnViewModel,
  PlanDecisionViewModel,
} from '../presentation/conversation-view-model';
import type { EntryRegionKind, GroupedTurnEntry } from '../presentation/grouped-turn-layout';
import type { SurfaceRoundOutline } from './surface-design-contracts';
import { moveRetainedNodeBefore } from './rich-card-reconciliation';
import type { ConversationSurfaceRenderers, ConversationQueueItemRenderContext } from './surface-contracts';

export type ManagedBlockElement = HTMLElement & {
  _tofuBlock?: ConversationBlockViewModel;
  _tofuBlockRenderer?: ConversationSurfaceRenderers['renderBlock'];
};

/* The per-Turn block container remembers the exact source array and window it
 * last reconciled.  The view model memoizes an untouched Turn's `blocks` array,
 * so an identical reference proves the mounted subtree is already correct and
 * the expensive structural walk can be skipped.  `blockStart` and the block
 * renderer are part of the proof because either can change while the array
 * reference holds (window expansion, hot renderer swap). */
export type ManagedBlocksContainerElement = HTMLElement & {

  _tofuRoundOutline?: SurfaceRoundOutline;
  _tofuOutlineStatus?: ConversationTurnViewModel['status'];
  _tofuBlocksSource?: ReadonlyArray<GroupedTurnEntry>;
  /** Region windows last mounted; scroll residency measures each region's
   * own nodes, so pinned head/trail entries never skew its geometry. */
  _tofuBlockWindows?: ReadonlyArray<SurfaceRegionWindow>;
  _tofuBlocksSignature?: string;
  _tofuBlocksRenderer?: ConversationSurfaceRenderers['renderBlock'];
  /** Narration well fold last painted; a reader's toggle changes it alone. */
  _tofuNarrationOpen?: boolean;
};

/** One region's mounted window. `start`/`end` are relative to the region,
 * which begins at `regionStart` in the Turn's entry list. */
export interface SurfaceRegionWindow {
  readonly key: string;
  readonly kind: EntryRegionKind;
  readonly regionStart: number;
  readonly length: number;
  readonly start: number;
  readonly end: number;
}

export function regionWindowSignature(windows: ReadonlyArray<SurfaceRegionWindow>): string {
  return windows.map(window => `${window.kind}:${window.start}:${window.end}:${window.length}`).join('|');
}

export type ManagedTurnAvatarElement = HTMLElement & {
  _tofuTurn?: ConversationTurnViewModel;
  _tofuTurnAvatarRenderer?: ConversationSurfaceRenderers['renderTurnAvatar'];
};

export type ManagedTurnHeaderElement = HTMLElement & {
  _tofuTurn?: ConversationTurnViewModel;
  _tofuTurnHeaderRenderer?: ConversationSurfaceRenderers['renderTurnHeader'];
};

export type ManagedTurnActionsElement = HTMLElement & {
  _tofuTurn?: ConversationTurnViewModel;
  _tofuTurnActionsRenderer?: ConversationSurfaceRenderers['renderTurnActions'];
};

export type ManagedTurnFooterElement = HTMLElement & {
  _tofuTurn?: ConversationTurnViewModel;
  _tofuTurnFooterRenderer?: ConversationSurfaceRenderers['renderTurnFooter'];
};

export type ManagedTurnContextRailElement = HTMLElement & {
  _tofuContextBlock?: Extract<ConversationBlockViewModel, { kind: 'context' }>;
  _tofuTurnContextRailRenderer?: ConversationSurfaceRenderers['renderTurnContextRail'];
  _tofuTurnContextRailRendered?: boolean;
};

export type ManagedPlanDecisionElement = HTMLElement & {
  _tofuPlanDecision?: PlanDecisionViewModel;
  _tofuPlanDecisionRenderer?: ConversationSurfaceRenderers['renderPlanDecision'];
};

/* A Turn node records the exact view-model object it last rendered.  A
 * memoized plain Turn returns the same object across frames; that identity plus
 * the block window, the attached plan decision, and the surface options fully
 * determine the subtree, so the whole Turn can be skipped. */
export type ManagedTurnElement = HTMLElement & {
  _tofuTurnVm?: ConversationTurnViewModel;
  /** Mounted entry list; a reader's panel toggle changes it without a new Turn. */
  _tofuTurnEntries?: ReadonlyArray<GroupedTurnEntry>;
  _tofuTurnWindowSignature?: string;
  _tofuTurnPlanDecision?: PlanDecisionViewModel | null;
  _tofuTurnNarrationOpen?: boolean;
  _tofuTurnSurfaceOptions?: unknown;
};

export type ManagedLaneHeaderElement = HTMLElement & {
  _tofuLane?: ConversationLaneViewModel;
  _tofuLaneHeaderRenderer?: ConversationSurfaceRenderers['renderLaneHeader'];
};

export type ManagedQueueElement = HTMLElement & {
  _tofuQueueItem?: ConversationQueueItemViewModel;
  _tofuQueueRenderer?: ConversationSurfaceRenderers['renderQueueItem'];
  _tofuQueueContext?: ConversationQueueItemRenderContext;
};

export function directPart(parent: Element, part: string): HTMLElement | null {
  for (let child = parent.firstElementChild; child; child = child.nextElementSibling) {
    if ((child as HTMLElement).dataset.conversationPart === part) return child as HTMLElement;
  }
  return null;
}

/** Bind singleton/key convergence to the owning Surface lifecycle once.
 * The retirement port finishes every duplicate before propagating an error;
 * raw DOM removal would orphan renderer requests and presentation references. */
export function createSurfaceDom(retireNodes: (nodes: readonly HTMLElement[]) => void) {
  function ensurePart(
    parent: HTMLElement,
    part: string,
    tagName = 'div',
  ): HTMLElement {
    /* A part is a singleton slot. Find the surviving node and drop any stale
     * duplicate in ONE sibling walk: directPart's early exit can't prune, and a
     * second Array.from(parent.children) pass re-walks the live HTMLCollection
     * (quadratic per commit). Collect duplicates during the scan, then retire
     * them through the same lifecycle as every other Surface removal. */
    let existing: HTMLElement | null = null;
    const duplicates: HTMLElement[] = [];
    for (let child = parent.firstElementChild; child;) {
      const next = child.nextElementSibling;
      const element = child as HTMLElement;
      if (element.dataset.conversationPart === part) {
        if (!existing) existing = element;
        else duplicates.push(element);
      }
      child = next;
    }
    if (duplicates.length) retireNodes(duplicates);
    if (existing) return existing;
    const node = parent.ownerDocument.createElement(tagName);
    node.dataset.conversationPart = part;
    node.className = `conversation-${part}`;
    parent.appendChild(node);
    return node;
  }

  function directKeyedChildren(
    parent: HTMLElement,
    key: 'turnId' | 'presentationId' | 'blockId' | 'laneId' | 'queueId',
  ): Map<string, HTMLElement> {
    const result = new Map<string, HTMLElement>();
    const duplicates: HTMLElement[] = [];
    /* Walk element siblings directly: Array.from(parent.children) iterates the
     * live HTMLCollection through its proxy, which engines with no indexed-access
     * cache (and jsdom) serve as an O(n) lookup per index — quadratic on Turns
     * that carry thousands of blocks. Complete the scan before retirement so
     * renderer teardown cannot interrupt discovery of later duplicates. */
    const visit = (child: Element): void => {
      const value = (child as HTMLElement).dataset[key];
      if (!value) return;
      /* A keyed child is a singleton by contract. A stale asset transition or
       * interrupted live render can leave two DOM nodes carrying the same key.
       * Map.set alone retained only the last node and made every earlier copy an
       * untracked orphan, so authoritative renders could never remove it and a
       * single tool card appeared repeatedly. Keep the first managed node (and
       * its focus/disclosure state) and converge duplicate DOM on every commit. */
      if (result.has(value)) duplicates.push(child as HTMLElement);
      else result.set(value, child as HTMLElement);
    };
    for (let child = parent.firstElementChild; child; child = child.nextElementSibling) {
      const wellBody = narrationWellBody(child);
      if (!wellBody) visit(child);
      else for (let inner = wellBody.firstElementChild; inner; inner = inner.nextElementSibling) visit(inner);
    }
    if (duplicates.length) retireNodes(duplicates);
    return result;
  }

  return { ensurePart, directKeyedChildren };
}

/** A reply's interim prose shares one scrolling well inside the blocks
 * container; its keyed blocks are children of the well's body. */
export function narrationWellBody(node: Element): HTMLElement | null {
  if ((node as HTMLElement).dataset.narrationWell === undefined) return null;
  for (let child = node.firstElementChild; child; child = child.nextElementSibling) {
    if ((child as HTMLElement).dataset.narrationWellBody !== undefined) return child as HTMLElement;
  }
  return null;
}

/** Keyed block nodes of a blocks container in DOM order, through the well. */
export function keyedBlockNodes(container: HTMLElement): HTMLElement[] {
  const nodes: HTMLElement[] = [];
  for (let child = container.firstElementChild; child; child = child.nextElementSibling) {
    const body = narrationWellBody(child);
    const members = body ? Array.from(body.children) : [child];
    for (const node of members) {
      if ((node as HTMLElement).dataset.blockId !== undefined) nodes.push(node as HTMLElement);
    }
  }
  return nodes;
}

export function placeAt(parent: HTMLElement, node: HTMLElement, index: number): void {
  const current = parent.children.item(index);
  if (current !== node) moveRetainedNodeBefore(parent, node, current ?? null);
}

/* Content may host unmanaged children between managed parts (the inline turn
 * editor host sits after turn-blocks). Index only managed parts so the
 * per-commit order normalization leaves foreign children in place: moving a
 * node that contains the focused textarea blurs it and steals the caret. */
export function placeManagedAt(
  parent: HTMLElement,
  node: HTMLElement,
  managedIndex: number,
): void {
  let managedPosition = 0;
  let referencePosition = 0;
  let reference: Element | null = null;
  for (let child = parent.firstElementChild; child; child = child.nextElementSibling) {
    if (!(child as HTMLElement).dataset.conversationPart) continue;
    if (managedPosition === managedIndex && child === node) return;
    managedPosition += 1;
    if (child !== node) {
      if (referencePosition === managedIndex) reference = child;
      referencePosition += 1;
    }
  }
  moveRetainedNodeBefore(parent, node, reference);
}

export function hasRenderableContent(node: HTMLElement): boolean {
  return node.childElementCount > 0 || Boolean(node.textContent?.trim());
}
