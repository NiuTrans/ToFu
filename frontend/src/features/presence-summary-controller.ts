/**
 * Responsibility: own the Collaboration Bar's bounded peer mirror and the
 * latest displayed-scope summary request. Entry point:
 * createPresenceSummaryController. Dependencies: injected scope, fetch,
 * presentation-notification, and diagnostic ports. No DOM or timer access.
 */

export const PRESENCE_SUMMARY_LIMITS = Object.freeze({
  peerRoots: 32,
  peersPerRoot: 128,
  rootLength: 4_096,
  conversationIdLength: 256,
});

export interface PresenceSummaryScope {
  readonly root: string;
  readonly selfConversationId: string;
}

export interface PresenceSummaryControllerPorts {
  currentScope(): PresenceSummaryScope | null;
  fetchSummary(root: string, selfConversationId: string): unknown;
  onSummaryChanged(): void;
  onError?(error: unknown): void;
}

export interface PresenceSummaryControllerSnapshot {
  readonly peerRoots: number;
  readonly maxPeers: number;
  readonly summaryRoot: string;
  readonly summarySelfId: string;
  readonly flightKey: string;
}

export interface PresenceSummaryController {
  refresh(): Promise<unknown | null>;
  summaryFor(root: string, selfConversationId: string): unknown | null;
  peersFor(root: string): readonly string[];
  updatePeer(root: string, conversationId: string): void;
  removePeer(root: string, conversationId: string): void;
  replacePeers(root: string, conversationIds: readonly unknown[]): void;
  adoptSummary(
    root: string,
    selfConversationId: string,
    summary: unknown,
  ): void;
  snapshot(): PresenceSummaryControllerSnapshot;
  destroy(): void;
}

interface SummaryFlight {
  readonly key: string;
  readonly generation: number;
  readonly promise: Promise<unknown | null>;
}

function normalizedRoot(value: unknown): string {
  const root = String(value ?? '').replace(/[/\\]+$/, '');
  if (!root || root.length > PRESENCE_SUMMARY_LIMITS.rootLength) return '';
  return root;
}

function normalizedConversationId(value: unknown): string {
  const conversationId = String(value ?? '');
  if (!conversationId ||
      conversationId.length > PRESENCE_SUMMARY_LIMITS.conversationIdLength) {
    return '';
  }
  return conversationId;
}

function scopeKey(scope: PresenceSummaryScope): string {
  return `${scope.root}\u0000${scope.selfConversationId}`;
}

export function createPresenceSummaryController(
  ports: PresenceSummaryControllerPorts,
): PresenceSummaryController {
  const peerConversations = new Map<string, Set<string>>();
  let destroyed = false;
  let generation = 0;
  let summaryKey = '';
  let summaryRoot = '';
  let summarySelfId = '';
  let summaryValue: unknown | null = null;
  let flight: SummaryFlight | null = null;

  const reportError = (error: unknown): void => {
    try {
      ports.onError?.(error);
    } catch {
      // Optional diagnostics cannot own this projection's lifecycle.
    }
  };

  const currentScope = (): PresenceSummaryScope | null => {
    try {
      const candidate = ports.currentScope();
      if (!candidate) return null;
      const root = normalizedRoot(candidate.root);
      if (!root) return null;
      const selfConversationId = candidate.selfConversationId
        ? normalizedConversationId(candidate.selfConversationId)
        : '';
      return Object.freeze({ root, selfConversationId });
    } catch (error: unknown) {
      reportError(error);
      return null;
    }
  };

  const notifySummaryChanged = (): void => {
    try {
      ports.onSummaryChanged();
    } catch (error: unknown) {
      reportError(error);
    }
  };

  const touchPeerRoot = (root: string, peers: Set<string>): void => {
    if (peerConversations.has(root)) peerConversations.delete(root);
    peerConversations.set(root, peers);
    while (peerConversations.size > PRESENCE_SUMMARY_LIMITS.peerRoots) {
      const displayedRoot = currentScope()?.root ?? '';
      let victim = '';
      for (const candidate of peerConversations.keys()) {
        if (candidate !== displayedRoot) {
          victim = candidate;
          break;
        }
      }
      if (!victim) victim = peerConversations.keys().next().value ?? '';
      if (!victim) break;
      peerConversations.delete(victim);
    }
  };

  const refresh = (): Promise<unknown | null> => {
    if (destroyed) return Promise.resolve(null);
    const scope = currentScope();
    if (!scope) return Promise.resolve(null);
    const key = scopeKey(scope);
    if (flight?.key === key) return flight.promise;
    const requestGeneration = ++generation;
    let request: unknown;
    try {
      request = ports.fetchSummary(scope.root, scope.selfConversationId);
    } catch (error: unknown) {
      reportError(error);
      return Promise.resolve(null);
    }
    const promise = Promise.resolve(request).then((summary: unknown) => {
      if (destroyed || requestGeneration !== generation ||
          scopeKey(currentScope() ?? { root: '', selfConversationId: '' }) !== key) {
        return null;
      }
      summaryKey = key;
      summaryRoot = scope.root;
      summarySelfId = scope.selfConversationId;
      summaryValue = summary ?? null;
      notifySummaryChanged();
      return summaryValue;
    }).catch((error: unknown) => {
      reportError(error);
      return null;
    }).finally(() => {
      if (flight?.generation === requestGeneration) flight = null;
    });
    flight = Object.freeze({ key, generation: requestGeneration, promise });
    return promise;
  };

  const summaryFor = (
    rootValue: string,
    selfConversationIdValue: string,
  ): unknown | null => {
    const root = normalizedRoot(rootValue);
    const selfConversationId = selfConversationIdValue
      ? normalizedConversationId(selfConversationIdValue)
      : '';
    if (!root || scopeKey({ root, selfConversationId }) !== summaryKey) return null;
    return summaryValue;
  };

  const peersFor = (rootValue: string): readonly string[] => {
    const root = normalizedRoot(rootValue);
    return root ? Object.freeze(Array.from(peerConversations.get(root) ?? [])) : [];
  };

  const updatePeer = (rootValue: string, conversationIdValue: string): void => {
    if (destroyed) return;
    const root = normalizedRoot(rootValue);
    const conversationId = normalizedConversationId(conversationIdValue);
    if (!root || !conversationId) return;
    const peers = peerConversations.get(root) ?? new Set<string>();
    if (peers.has(conversationId) ||
        peers.size < PRESENCE_SUMMARY_LIMITS.peersPerRoot) {
      peers.add(conversationId);
    }
    touchPeerRoot(root, peers);
  };

  const removePeer = (rootValue: string, conversationIdValue: string): void => {
    if (destroyed) return;
    const root = normalizedRoot(rootValue);
    const conversationId = normalizedConversationId(conversationIdValue);
    const peers = peerConversations.get(root);
    if (!root || !conversationId || !peers) return;
    peers.delete(conversationId);
    if (peers.size === 0) peerConversations.delete(root);
    else touchPeerRoot(root, peers);
  };

  const replacePeers = (rootValue: string, values: readonly unknown[]): void => {
    if (destroyed) return;
    const root = normalizedRoot(rootValue);
    if (!root) return;
    const peers = new Set<string>();
    for (const value of values ?? []) {
      const conversationId = normalizedConversationId(value);
      if (!conversationId || peers.has(conversationId)) continue;
      if (peers.size >= PRESENCE_SUMMARY_LIMITS.peersPerRoot) break;
      peers.add(conversationId);
    }
    touchPeerRoot(root, peers);
  };

  const adoptSummary = (
    rootValue: string,
    selfConversationIdValue: string,
    summary: unknown,
  ): void => {
    if (destroyed) return;
    const root = normalizedRoot(rootValue);
    const selfConversationId = selfConversationIdValue
      ? normalizedConversationId(selfConversationIdValue)
      : '';
    if (!root) return;
    generation += 1;
    flight = null;
    summaryKey = scopeKey({ root, selfConversationId });
    summaryRoot = root;
    summarySelfId = selfConversationId;
    summaryValue = summary ?? null;
  };

  const snapshot = (): PresenceSummaryControllerSnapshot => {
    let maxPeers = 0;
    for (const peers of peerConversations.values()) {
      maxPeers = Math.max(maxPeers, peers.size);
    }
    return Object.freeze({
      peerRoots: peerConversations.size,
      maxPeers,
      summaryRoot,
      summarySelfId,
      flightKey: flight?.key ?? '',
    });
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    generation += 1;
    flight = null;
    summaryKey = '';
    summaryRoot = '';
    summarySelfId = '';
    summaryValue = null;
    peerConversations.clear();
  };

  return Object.freeze({
    refresh,
    summaryFor,
    peersFor,
    updatePeer,
    removePeer,
    replacePeers,
    adoptSummary,
    snapshot,
    destroy,
  });
}
