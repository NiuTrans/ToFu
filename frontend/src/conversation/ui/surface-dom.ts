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
import { RoundOutline } from './round-outline';
import { moveRetainedNodeBefore } from './rich-card-reconciliation';
import type { ConversationSurfaceRenderers } from './surface-contracts';

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

  _tofuRoundOutline?: RoundOutline;
  _tofuOutlineStatus?: ConversationTurnViewModel['status'];
  _tofuBlocksSource?: ReadonlyArray<ConversationBlockViewModel>;
  _tofuBlocksStart?: number;

  _tofuBlocksEnd?: number;
  _tofuBlocksRenderer?: ConversationSurfaceRenderers['renderBlock'];
};

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
  _tofuTurnBlockStart?: number;

  _tofuTurnBlockEnd?: number;
  _tofuTurnPlanDecision?: PlanDecisionViewModel | null;
  _tofuTurnSurfaceOptions?: unknown;
};

export type ManagedLaneHeaderElement = HTMLElement & {
  _tofuLane?: ConversationLaneViewModel;
  _tofuLaneHeaderRenderer?: ConversationSurfaceRenderers['renderLaneHeader'];
};

export type ManagedQueueElement = HTMLElement & {
  _tofuQueueItem?: ConversationQueueItemViewModel;
  _tofuQueueRenderer?: ConversationSurfaceRenderers['renderQueueItem'];
};

export function directPart(parent: Element, part: string): HTMLElement | null {
  for (let child = parent.firstElementChild; child; child = child.nextElementSibling) {
    if ((child as HTMLElement).dataset.conversationPart === part) return child as HTMLElement;
  }
  return null;
}

export function ensurePart(
  parent: HTMLElement,
  part: string,
  tagName = 'div',
): HTMLElement {
  /* A part is a singleton slot. Find the surviving node and drop any stale
   * duplicate in ONE sibling walk: directPart's early exit can't prune, and a
   * second Array.from(parent.children) pass re-walks the live HTMLCollection
   * (quadratic per commit). Capture nextElementSibling before removal detaches
   * the cursor, like directKeyedChildren. */
  let existing: HTMLElement | null = null;
  for (let child = parent.firstElementChild; child;) {
    const next = child.nextElementSibling;
    const element = child as HTMLElement;
    if (element.dataset.conversationPart === part) {
      if (!existing) existing = element;
      else child.remove();
    }
    child = next;
  }
  if (existing) return existing;
  const node = parent.ownerDocument.createElement(tagName);
  node.dataset.conversationPart = part;
  node.className = `conversation-${part}`;
  parent.appendChild(node);
  return node;
}

export function directKeyedChildren(
  parent: HTMLElement,
  key: 'turnId' | 'presentationId' | 'blockId' | 'laneId' | 'queueId',
): Map<string, HTMLElement> {
  const result = new Map<string, HTMLElement>();
  /* Walk element siblings directly: Array.from(parent.children) iterates the
   * live HTMLCollection through its proxy, which engines with no indexed-access
   * cache (and jsdom) serve as an O(n) lookup per index — quadratic on Turns
   * that carry thousands of blocks. Capture the next sibling before a stale
   * duplicate is removed, since removal detaches the walk cursor. */
  let child = parent.firstElementChild;
  while (child) {
    const next = child.nextElementSibling;
    const value = (child as HTMLElement).dataset[key];
    if (!value) {
      child = next;
      continue;
    }
    /* A keyed child is a singleton by contract. A stale asset transition or
     * interrupted live render can leave two DOM nodes carrying the same key.
     * Map.set alone retained only the last node and made every earlier copy an
     * untracked orphan, so authoritative renders could never remove it and a
     * single tool card appeared repeatedly. Keep the first managed node (and
     * its focus/disclosure state) and converge duplicate DOM on every commit. */
    if (result.has(value)) child.remove();
    else result.set(value, child as HTMLElement);
    child = next;
  }
  return result;
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
