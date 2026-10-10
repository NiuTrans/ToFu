/*
 * Responsibility: own the open Debug drawer's flat-wire request lifecycle.
 * Entry point: createDiagnosticsWireController. The retained presenter owns
 * DOM/polling; this controller owns request coalescing and last-good evidence.
 */

export interface DiagnosticsWire {
  readonly messages: readonly unknown[];
  readonly sections: readonly unknown[];
  readonly turns: readonly unknown[];
  readonly readError?: boolean;
  readonly [key: string]: unknown;
}

export interface DiagnosticsWireView {
  readonly wire: DiagnosticsWire | null;
  readonly readError: boolean;
}

export interface DiagnosticsWireController {
  select(conversationId: string | null): void;
  load(): Promise<DiagnosticsWireView | null>;
}

function isWire(value: unknown): value is DiagnosticsWire {
  if (value === null || typeof value !== 'object') return false;
  const wire = value as Partial<DiagnosticsWire>;
  return Array.isArray(wire.messages)
    && wire.messages.every((message: unknown) => message !== null
      && typeof message === 'object' && !Array.isArray(message))
    && Array.isArray(wire.sections)
    && wire.sections.every((section: unknown) => section !== null
      && typeof section === 'object' && !Array.isArray(section))
    && Array.isArray(wire.turns)
    && wire.turns.every((turn: unknown) => turn !== null
      && typeof turn === 'object' && !Array.isArray(turn));
}

export function createDiagnosticsWireController(
  read: (conversationId: string) => Promise<unknown>,
): DiagnosticsWireController {
  let conversationId: string | null = null;
  let epoch = 0;
  let intent = 0;
  let flight: Promise<unknown> | null = null;
  let lastGood: DiagnosticsWire | null = null;

  return {
    select(nextConversationId: string | null): void {
      // Even re-opening the same conversation starts a new presentation epoch.
      epoch += 1;
      intent += 1;
      conversationId = nextConversationId;
      lastGood = null;
      flight = null;
    },
    async load(): Promise<DiagnosticsWireView | null> {
      const id = conversationId;
      if (!id) return { wire: { messages: [], sections: [], turns: [] }, readError: false };
      const selectedEpoch = epoch;
      const selectedIntent = ++intent;
      // Poll, store invalidation and manual retry share one wire read.
      const active = flight ?? Promise.resolve().then(() => read(id));
      flight = active;
      let result: unknown;
      try {
        result = await active;
      } catch {
        // Network/server failures have the same retryable presentation as a
        // null response; no exception escapes into a background poll.
        result = null;
      } finally {
        if (flight === active) flight = null;
      }
      if (selectedEpoch !== epoch || selectedIntent !== intent) return null;
      if (!isWire(result)) return { wire: lastGood, readError: true };
      if (result.readError) {
        // A partial storage read cannot replace already-visible evidence.
        // With no last-good wire, show whatever succeeded, marked incomplete.
        const wire = lastGood && Array.isArray(result.eventReadBounds)
          && result.eventReadBounds.length > 0
          ? { ...lastGood, coverage: 'partial', coverageReason: result.coverageReason,
              eventReadBounds: result.eventReadBounds }
          : lastGood ?? result;
        return { wire, readError: true };
      }
      lastGood = result;
      return { wire: result, readError: false };
    },
  };
}
