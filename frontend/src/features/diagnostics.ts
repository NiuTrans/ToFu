import { getRuntimeService } from '../runtime/app-runtime.js';

export interface FrontendDiagnostics {
  loadedAt: number;
  resourceEntries: number;
  heapBytes?: number;
}

interface ConversationShell {
  id?: string;
  _turnSnapshotRequired?: boolean;
  _serverTurnCount?: number;
}

interface ConversationReadPort {
  ordered?(conversationOrId: unknown): ReadonlyArray<unknown>;
  activeAttemptIds?(conversationOrId: unknown): ReadonlyArray<string>;
  state?(conversationOrId: unknown): {
    conversationRevision?: number;
    transport?: string;
    livePhase?: unknown;
  } | null;
}

type JsonObject = Record<string, unknown>;

function activeConversationId(): string | null {
  const value = getRuntimeService('activeConvId');
  return typeof value === 'string' ? value : null;
}

function conversationList(): ConversationShell[] {
  const value = getRuntimeService('conversations');
  return Array.isArray(value) ? value as ConversationShell[] : [];
}

function safe<T>(fn: () => T, fallback: T): T {
  try {
    return fn();
  } catch {
    return fallback;
  }
}

function activeConversationSnapshot(): JsonObject {
  return safe<JsonObject>(() => {
    const id = activeConversationId();
    const conversations = conversationList();
    if (!id) return { activeConvId: id };
    const conversation = conversations.find((item) => item?.id === id);
    if (!conversation) return { activeConvId: id, found: false };
    const turnRead = getRuntimeService(
      'ConversationTurnRead',
    ) as ConversationReadPort | undefined;
    const state = turnRead?.state?.(conversation);
    return {
      activeConvId: id,
      found: true,
      turnSnapshotRequired: Boolean(conversation._turnSnapshotRequired),
      inMemoryTurnCount: turnRead?.ordered?.(conversation)?.length ?? 0,
      serverTurnCount: conversation._serverTurnCount ?? 0,
      revision: state?.conversationRevision ?? 0,
      transport: state?.transport ?? 'unavailable',
      activeAttemptCount: turnRead?.activeAttemptIds?.(conversation)?.length ?? 0,
      livePhase: state?.livePhase ?? null,
    };
  }, { error: 'activeConv snapshot failed' });
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

function conversationSyncConfiguration(): JsonObject {
  return {
    protocol: 'conversation-sync-v3',
    authority: 'sidecar-turn-store',
    browserTranscriptCache: 'none',
  };
}

function surfaceSnapshot(): JsonObject | null {
  return safe<JsonObject | null>(() => {
    const surface = document.querySelector<HTMLElement>(
      '[data-conversation-surface="turn-store"]',
    );
    if (!surface) return null;
    return {
      conversationId: surface.dataset.conversationId ?? null,
      revision: Number(surface.dataset.conversationRevision || 0),
      transport: surface.dataset.transport ?? null,
      turnNodeCount: surface.querySelectorAll(':scope [data-turn-id]').length,
    };
  }, null);
}

function liveStateProbe(): JsonObject {
  const id = safe(activeConversationId, null);
  if (!id) return { skipped: 'no active conversation' };
  const turnRead = getRuntimeService(
    'ConversationTurnRead',
  ) as ConversationReadPort | undefined;
  const state = safe(() => turnRead?.state?.(id) ?? null, null);
  if (!state) return {
    protocol: 'conversation-sync-v3',
    conversationId: id,
    skipped: 'TurnStore state unavailable',
  };
  return {
    protocol: 'conversation-sync-v3',
    conversationId: id,
    revision: state.conversationRevision ?? 0,
    transport: state.transport ?? 'unavailable',
    turnCount: safe(() => turnRead?.ordered?.(id)?.length ?? 0, 0),
    activeAttemptCount: safe(
      () => turnRead?.activeAttemptIds?.(id)?.length ?? 0,
      0,
    ),
    livePhase: state.livePhase ?? null,
  };
}

function serializeDiagnostics(blob: JsonObject): string {
  try {
    return JSON.stringify(blob, null, 2);
  } catch (error) {
    return JSON.stringify({
      collectedAt: new Date().toISOString(),
      error: `diagnostics serialization failed: ${errorMessage(error)}`,
    });
  }
}

/**
 * Collect the Android/Web diagnostics contract without trusting application
 * state to be healthy. This function resolves to JSON even when every optional
 * legacy global is absent or the live probe fails.
 */
export async function collectDiagnostics(): Promise<string> {
  try {
    const blob: JsonObject = {
      collectedAt: new Date().toISOString(),
      note: 'Tofu client diagnostics — paste this to the maintainer.',
      location: safe(() => location.href, null),
      userAgent: safe(() => navigator.userAgent, null),
      viewport: safe(() => ({
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        dpr: window.devicePixelRatio,
        vh100: document.documentElement.style.getPropertyValue('--vh100') || '(unset)',
      }), null),
      bundle: safe(() => {
        const script = document.querySelector<HTMLScriptElement>('script[src*="bundle-"]');
        return script
          ? (script.getAttribute('src') ?? '').replace(/^.*\//, '')
          : '(dev, unbundled)';
      }, null),
      conversationCount: safe(
        () => conversationList().length,
        null,
      ),
      conversationSync: conversationSyncConfiguration(),
      surface: surfaceSnapshot(),
      activeConv: activeConversationSnapshot(),
      recentLog: safe(() => {
        const ring = getRuntimeService('__tofuDiagRing');
        return Array.isArray(ring) ? ring.slice(-60) : [];
      }, []),
    };
    blob.liveStateProbe = liveStateProbe();
    return serializeDiagnostics(blob);
  } catch (error) {
    return serializeDiagnostics({
      collectedAt: new Date().toISOString(),
      error: `diagnostics collection failed: ${errorMessage(error)}`,
    });
  }
}
