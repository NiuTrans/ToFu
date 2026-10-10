/** DOM-lifetime evidence for paint receipts; never a Turn state authority.
 * The composition injects DOM lookup/observer capabilities and owns destroy().
 * Weak node keys survive in-place commits but retire on removal/replacement,
 * including same-task remove/reinsert before MutationObserver delivery.
 */
export interface TurnMountTrackerOptions {
  root?: Node;
  observer?: typeof MutationObserver;
  findTurn(conversationId: string, turnId: string): Element | null;
}

export interface TurnMountTracker {
  getToken(conversationId: string, turnId: string): number | null;
  destroy(): void;
}

export function createTurnMountTracker(options: TurnMountTrackerOptions): TurnMountTracker {
  let tokens = new WeakMap<Node, number>();
  let sequence = 0;
  let destroyed = false;
  let observer: MutationObserver | null = null;
  const retire = (records: MutationRecord[]): void => {
    for (const record of records) {
      for (const node of record.removedNodes) {
        tokens.delete(node);
        if (node.nodeType !== 1) continue;
        for (const child of (node as Element).querySelectorAll('[data-turn-id]')) {
          tokens.delete(child);
        }
      }
    }
  };
  const ensureObserver = (): boolean => {
    if (destroyed || !options.root || !options.observer) return false;
    if (!observer) {
      observer = new options.observer(retire);
      observer.observe(options.root, { childList: true, subtree: true });
    }
    return true;
  };
  return {
    getToken(conversationId, turnId) {
      if (!ensureObserver()) return null;
      retire(observer!.takeRecords());
      const node = options.findTurn(conversationId, turnId);
      if (!node) return null;
      if (!tokens.has(node)) tokens.set(node, ++sequence);
      return tokens.get(node)!;
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      observer?.disconnect();
      observer = null;
      tokens = new WeakMap();
    },
  };
}
