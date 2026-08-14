import { createLifecycleScope } from '../lifecycle';

export type AttemptTransportStatus = 'connecting' | 'connected' | 'reconnecting';

export interface AttemptContinuation {
  attemptId: string;
  [key: string]: unknown;
}

export interface AttemptEventEnvelope {
  type: string;
  seq?: number;
  requestId?: string;
  taskId?: string;
  attemptId?: string;
  payload?: {
    settlement?: { continuation?: AttemptContinuation | null };
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export interface AttemptStreamOptions<TSnapshot = unknown> {
  attemptId: string;
  url: string;
  after?: number;
  onEvent(event: AttemptEventEnvelope): void;
  onTransport(status: AttemptTransportStatus): void;
  fetchSnapshot?(): Promise<TSnapshot | null | undefined>;
  onSnapshot?(snapshot: TSnapshot): void;
  onTerminal?(event: AttemptEventEnvelope): void;
  onContinuation?(continuation: AttemptContinuation): void;
  onProtocolError?(error: Error): void;
  eventSourceFactory?: (url: string) => EventSource;
}

export interface AttemptStreamConnection {
  close(): void;
  readonly cursor: number;
}

const eventNames = [
  'projection_updated',
  'interaction_request',
  'status_changed',
  'terminal_settlement',
] as const;

function initialCursor(value: unknown): number {
  const cursor = Number(value || 0);
  return Number.isFinite(cursor) && cursor > 0 ? Math.floor(cursor) : 0;
}

/** Own one attempt EventSource, its replay cursor, snapshot recovery and close. */
export function createAttemptEventStream<TSnapshot = unknown>(
  options: AttemptStreamOptions<TSnapshot>,
): AttemptStreamConnection {
  if (!options.attemptId || !options.url) {
    throw new Error('Attempt stream requires attemptId and url');
  }
  const scope = createLifecycleScope();
  const createSource = options.eventSourceFactory ?? ((url: string) => new EventSource(url));
  const source = createSource(options.url);
  let cursor = initialCursor(options.after);
  let closed = false;
  let snapshotInFlight: Promise<void> | null = null;

  options.onTransport('connecting');

  const close = (): void => {
    if (closed) return;
    closed = true;
    scope.destroy();
  };
  scope.add(() => source.close());

  const protocolError = (cause: unknown): void => {
    const error = cause instanceof Error ? cause : new Error(String(cause));
    options.onProtocolError?.(error);
  };

  const ingest = (raw: MessageEvent<string>): void => {
    if (closed) return;
    let event: AttemptEventEnvelope;
    try {
      const value: unknown = JSON.parse(raw.data);
      if (!value || typeof value !== 'object' ||
          typeof (value as Record<string, unknown>).type !== 'string') {
        throw new Error('Attempt stream event has no type');
      }
      event = value as AttemptEventEnvelope;
    } catch (error) {
      protocolError(error);
      return;
    }

    // A shared proxy, browser extension or stale EventSource must never route
    // one attempt's projection into another attempt's store. The server
    // envelope is authoritative; missing attemptId stays compatible with
    // older replay frames, but an explicit mismatch is a protocol failure.
    if (event.attemptId && event.attemptId !== options.attemptId) {
      protocolError(new Error(
        `Attempt stream identity mismatch: expected ${options.attemptId}, got ${event.attemptId}`,
      ));
      return;
    }

    const sequence = Number(event.seq || 0);
    if (Number.isFinite(sequence) && sequence > cursor) cursor = Math.floor(sequence);
    options.onEvent(event);
    if (event.type !== 'terminal_settlement') return;

    close();
    options.onTerminal?.(event);
    const continuation = event.payload?.settlement?.continuation;
    if (continuation?.attemptId && continuation.attemptId !== options.attemptId) {
      options.onContinuation?.(continuation);
    }
  };

  source.onopen = () => {
    if (!closed) options.onTransport('connected');
  };
  source.onmessage = ingest as (event: MessageEvent) => void;
  for (const name of eventNames) {
    scope.listen(source, name, ingest as EventListener);
  }
  source.onerror = () => {
    if (closed) return;
    options.onTransport('reconnecting');
    if (!options.fetchSnapshot || snapshotInFlight) return;
    snapshotInFlight = Promise.resolve(options.fetchSnapshot())
      .then((snapshot) => {
        if (!closed && snapshot != null) options.onSnapshot?.(snapshot);
      })
      .catch(protocolError)
      .finally(() => { snapshotInFlight = null; });
  };

  return {
    close,
    get cursor() { return cursor; },
  };
}
