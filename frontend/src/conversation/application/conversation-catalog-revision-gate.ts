/**
 * Revision-aware debounce for authoritative conversation catalog refreshes.
 *
 * Push frames are wake hints. A newer TurnState may satisfy the same revision
 * during the debounce, making a 500-row catalog read redundant. Invalid,
 * metadata-only, unknown, overflowed, or still-stale hints retain the full
 * refresh path. This owner has no DOM, transport, or conversation storage.
 */

export const CONVERSATION_CATALOG_REVISION_BUDGET = 64;
export const CONVERSATION_CATALOG_REFRESH_DELAY_MS = 150;

export interface ConversationCatalogRevisionGatePorts {
  readRevision(conversationId: string): number | null;
  refreshCatalog(): unknown | PromiseLike<unknown>;
  isVisible(): boolean;
  setTimeout?(callback: () => void, delayMs: number): unknown;
  clearTimeout?(handle: unknown): void;
  warn?(message: string): void;
}

export interface ConversationCatalogRevisionGate {
  reached(conversationId: unknown, revision: unknown): boolean;
  schedule(conversationId: unknown, revision: unknown): void;
  destroy(): void;
}

interface RevisionRequest {
  conversationId: string;
  revision: number;
}

function revisionRequest(
  conversationId: unknown,
  revision: unknown,
): RevisionRequest | null {
  return typeof conversationId === 'string' && conversationId.length > 0
    && Number.isSafeInteger(revision) && Number(revision) > 0
    ? { conversationId, revision: Number(revision) }
    : null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function createConversationCatalogRevisionGate(
  ports: ConversationCatalogRevisionGatePorts,
): ConversationCatalogRevisionGate {
  const scheduleTimeout = ports.setTimeout
    ?? ((callback, delayMs) => globalThis.setTimeout(callback, delayMs));
  const cancelTimeout = ports.clearTimeout
    ?? ((handle) => globalThis.clearTimeout(handle as number));
  const revisions = new Map<string, number>();
  let timer: unknown = null;
  let forced = false;
  let destroyed = false;

  const warn = (error: unknown): void => {
    try { ports.warn?.(errorMessage(error)); } catch { /* diagnostic only */ }
  };

  const reached = (conversationId: unknown, revision: unknown): boolean => {
    const request = revisionRequest(conversationId, revision);
    if (!request) return false;
    try {
      const current = ports.readRevision(request.conversationId);
      return Number.isSafeInteger(current) && Number(current) >= request.revision;
    } catch (error) {
      warn(error);
      return false;
    }
  };

  const flush = (): void => {
    timer = null;
    const refreshRequired = forced || [...revisions].some(
      ([conversationId, revision]) => !reached(conversationId, revision),
    );
    forced = false;
    revisions.clear();
    if (destroyed || !refreshRequired) return;
    let visible = false;
    try {
      visible = ports.isVisible();
    } catch (error) {
      warn(error);
      return;
    }
    if (!visible) return;
    try {
      Promise.resolve(ports.refreshCatalog()).catch(warn);
    } catch (error) {
      warn(error);
    }
  };

  const schedule = (conversationId: unknown, revision: unknown): void => {
    if (destroyed) return;
    const request = revisionRequest(conversationId, revision);
    if (request && reached(request.conversationId, request.revision)) return;
    if (!request) {
      forced = true;
      revisions.clear();
    } else if (!forced) {
      if (!revisions.has(request.conversationId)
          && revisions.size >= CONVERSATION_CATALOG_REVISION_BUDGET) {
        forced = true;
        revisions.clear();
      } else {
        revisions.set(
          request.conversationId,
          Math.max(revisions.get(request.conversationId) ?? 0, request.revision),
        );
      }
    }
    if (timer !== null) cancelTimeout(timer);
    timer = scheduleTimeout(flush, CONVERSATION_CATALOG_REFRESH_DELAY_MS);
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    if (timer !== null) cancelTimeout(timer);
    timer = null;
    forced = false;
    revisions.clear();
  };

  return Object.freeze({ reached, schedule, destroy });
}
