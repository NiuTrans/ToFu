/** One ordered, replayable SSE owner for a turn-native conversation. */

import {
  CONVERSATION_SYNC_STREAM_POLICY,
  assertConversationSyncSchema,
  decodeConversationSyncEvent,
  type AttemptEvent,
  type ConversationChange,
  type ConversationSyncApi,
  type ConversationSyncEvent,
  type ConversationSyncSnapshot,
  type ConnectionHealth,
  type TurnProjectionChange,
} from '../api/conversation-sync.generated';
import { createLifecycleScope, type LifecycleScope } from '../lifecycle';
import { conversationConnectionHealth } from './connection-health';

type JsonRecord = Record<string, unknown>;

export interface ConversationSyncCoordinatorOptions {
  conversationId: string;
  streamClientId?: string;
  api: ConversationSyncApi;
  onSnapshot(snapshot: ConversationSyncSnapshot & { authoritativeFull: true }): void;
  onAttemptEvent(event: AttemptEvent): boolean;
  onTurnDelta(delta: JsonRecord): boolean;
  onProtocolError?(error: Error): void;
  onHealth?(conversationId: string, health: ConnectionHealth): void;
  /* Server-stamped delivery-wedge signal (sync heartbeat/snapshot
   * `pushWithheld`): the live task's authoritative frames are withheld on a
   * storage-write wedge. Rides read-side frames because withheld frames can
   * never carry it. */
  onPushWithheld?(withheld: boolean): void;
  eventSourceFactory?: (url: string) => EventSource;
}

export interface ConversationSyncConnection {
  close(): void;
  readonly cursor: string;
}

const CHANGE_EVENT_NAMES = [
  'turn.upsert',
  'turn.patch',
  'turn.deleted',
  'attempt.event',
  'conversation.activity',
  'sync.heartbeat',
  'sync.reset_required',
] as const;

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonRecord : {};
}

/**
 * Owns snapshot/cursor/SSE/recovery for exactly one conversation.
 * Push and BroadcastChannel callers may only call `invalidate`; projections
 * enter the store through this stream (or its authoritative reset snapshot).
 */
export class ConversationSyncCoordinator implements ConversationSyncConnection {
  private readonly options: ConversationSyncCoordinatorOptions;
  private source: EventSource | null = null;
  private sourceScope: LifecycleScope | null = null;
  private snapshotPromise: Promise<ConversationSyncSnapshot> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private silenceTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private shouldConnect = false;
  private generation = 0;
  private retryCount = 0;
  private sequence = 0;
  private cursorValue = '';
  private hasPublishedHealth = false;

  constructor(options: ConversationSyncCoordinatorOptions) {
    if (!options.conversationId) throw new Error('Conversation sync requires an id');
    this.options = options;
  }

  /** Announce idle only after the owning runtime has published this instance. */
  announceInitialHealth(): void {
    if (this.closed || this.hasPublishedHealth) return;
    this.publishHealth('idle');
  }

  get cursor(): string {
    return this.cursorValue;
  }

  get connected(): boolean {
    return this.source !== null;
  }

  async resume(): Promise<void> {
    if (this.closed) return;
    this.shouldConnect = true;
    if (!this.cursorValue) {
      await this.hydrate(true);
      return;
    }
    this.open();
  }

  async hydrate(connect = true): Promise<ConversationSyncSnapshot> {
    this.shouldConnect = this.shouldConnect || connect;
    const snapshot = await this.fetchSnapshot('hydrate');
    if (this.shouldConnect) this.open();
    return snapshot;
  }

  async recover(reason = 'snapshot-recovery'): Promise<ConversationSyncSnapshot> {
    this.shouldConnect = true;
    this.closeSource();
    this.publishHealth('recovering', reason);
    try {
      const snapshot = await this.fetchSnapshot(reason);
      this.retryCount = 0;
      this.open();
      return snapshot;
    } catch (error) {
      this.scheduleReconnect(reason);
      throw error;
    }
  }

  invalidate(_cursorHint?: string): void {
    if (this.closed || !this.shouldConnect) return;
    const health = conversationConnectionHealth.get(this.options.conversationId);
    const frameAge = Date.now() - Number(health?.lastFrameAt || 0);
    if (this.source && frameAge
        > Number(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs) / 2) {
      this.closeSource();
    }
    if (!this.source) this.scheduleReconnect('invalidation-wakeup', 0);
  }

  pause(): void {
    this.shouldConnect = false;
    this.closeSource();
    if (!this.closed) this.publishHealth('idle');
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.shouldConnect = false;
    this.closeSource();
    this.clearReconnectTimer();
    this.publishHealth('closed');
    conversationConnectionHealth.clear(this.options.conversationId);
  }

  private async fetchSnapshot(reason: string): Promise<ConversationSyncSnapshot> {
    if (this.snapshotPromise) return this.snapshotPromise;
    if (this.closed) throw new Error('Conversation sync coordinator is closed');
    let startSnapshot!: () => void;
    const operation = new Promise<ConversationSyncSnapshot>((resolve, reject) => {
      startSnapshot = () => {
        try {
          this.publishHealth(
            reason === 'hydrate' ? 'connecting' : 'recovering', reason,
          );
          const request = this.options.api.snapshot(this.options.conversationId)
            .then((snapshot) => {
              if (snapshot.conversationId !== this.options.conversationId) {
                throw new Error('Conversation snapshot identity mismatch');
              }
              this.sequence = snapshot.syncSeq;
              this.cursorValue = snapshot.cursor;
              this.options.onPushWithheld?.(snapshot.pushWithheld === true);
              this.options.onSnapshot({ ...snapshot, authoritativeFull: true });
              if (this.shouldConnect) this.touchFrame();
              else this.publishHealth('idle');
              return snapshot;
            })
            .catch((cause: unknown) => {
              this.protocolError(cause);
              throw cause;
            });
          void request.then(resolve, reject);
        } catch (cause) {
          reject(cause);
        }
      };
    });
    const flight = operation.finally(() => { this.snapshotPromise = null; });
    // Claim the flight before publishing health. Health observers may render
    // synchronously and re-enter hydrate/resume; they must join this request.
    this.snapshotPromise = flight;
    startSnapshot();
    return flight;
  }

  private open(): void {
    if (this.closed || !this.shouldConnect || this.source) return;
    this.clearReconnectTimer();
    this.generation += 1;
    const sourceGeneration = this.generation;
    const factory = this.options.eventSourceFactory
      ?? ((url: string) => new EventSource(url));
    const source = factory(this.options.api.eventsUrl(
      this.options.conversationId,
      this.cursorValue,
      this.options.streamClientId,
      this.options.streamClientId ? sourceGeneration : 0,
    ));
    const scope = createLifecycleScope();
    this.source = source;
    this.sourceScope = scope;
    scope.add(() => source.close());
    this.publishHealth('connecting');

    const ingest = (message: MessageEvent<string>): void => {
      if (this.closed || sourceGeneration !== this.generation) return;
      let event: ConversationSyncEvent;
      try {
        event = decodeConversationSyncEvent(JSON.parse(message.data) as unknown);
      } catch (error) {
        this.protocolError(error);
        void this.recover('invalid-stream-frame').catch(() => undefined);
        return;
      }
      this.ingest(event, message.lastEventId || '');
    };

    source.onopen = () => {
      if (sourceGeneration !== this.generation) return;
      this.retryCount = 0;
      this.publishHealth('live');
      this.touchFrame();
    };
    source.onmessage = ingest as (event: MessageEvent) => void;
    for (const name of CHANGE_EVENT_NAMES) {
      scope.listen(source, name, ingest as EventListener);
    }
    source.onerror = () => {
      if (this.closed || sourceGeneration !== this.generation) return;
      // Native EventSource resumes with Last-Event-ID.  Brief transport flaps
      // remain "recovering" and do not poison the badge; the silence deadline
      // below promotes only a genuinely stalled pipe to degraded.
      this.retryCount += 1;
      this.publishHealth('recovering', 'event-source-error');
      this.armSilenceTimer();
    };
  }

  private ingest(event: ConversationSyncEvent, lastEventId: string): void {
    if (event.conversationId !== this.options.conversationId) {
      this.protocolError(new Error('Conversation stream identity mismatch'));
      void this.recover('identity-mismatch').catch(() => undefined);
      return;
    }
    this.touchFrame();
    if (event.type === 'sync.heartbeat') {
      this.cursorValue = event.cursor;
      /* pushWithheld marks a WRITE-side wedge (authoritative pushes withheld
       * on storage retries) — distinct from the read-side degradation that
       * `degraded` alone has always meant. Both stall delivery, so both map
       * to the degraded badge, but the reason tells them apart. */
      const wedged = event.pushWithheld === true;
      this.publishHealth(event.degraded || wedged ? 'degraded' : 'live',
        wedged ? 'storage-write-wedged'
          : event.degraded ? 'storage-read-degraded' : undefined);
      this.options.onPushWithheld?.(wedged);
      return;
    }
    if (event.type === 'sync.reset_required') {
      this.cursorValue = event.cursor;
      void this.recover(event.reason).catch(() => undefined);
      return;
    }
    this.applyChange(event, lastEventId);
  }

  private applyChange(event: ConversationChange, lastEventId: string): void {
    if (!lastEventId) {
      this.protocolError(new Error('Conversation change has no replay cursor'));
      void this.recover('missing-event-cursor').catch(() => undefined);
      return;
    }
    if (event.syncSeq <= this.sequence) return;
    if (event.syncSeq !== this.sequence + 1) {
      void this.recover('sequence-gap').catch(() => undefined);
      return;
    }
    const payload = record(event.payload);
    if (payload.requiresSnapshot === true || event.type === 'conversation.activity') {
      void this.recover('authoritative-refresh-required').catch(() => undefined);
      return;
    }
    let applied = true;
    if (event.type === 'attempt.event') {
      try {
        const attemptEvent = assertConversationSyncSchema<AttemptEvent>(
          'AttemptEvent', payload.event,
        );
        applied = this.options.onAttemptEvent(attemptEvent);
      } catch (error) {
        this.protocolError(error);
        applied = false;
      }
    } else if (event.type === 'turn.upsert' || event.type === 'turn.patch'
        || event.type === 'turn.deleted') {
      let turnPatches: TurnProjectionChange[] = [];
      try {
        turnPatches = Array.isArray(payload.turnPatches)
          ? payload.turnPatches.map((change) =>
            assertConversationSyncSchema<TurnProjectionChange>(
              'TurnProjectionChange', change,
            ))
          : [];
        if (event.type === 'turn.patch' && turnPatches.length === 0) {
          throw new Error('Conversation turn.patch carries no projection patch');
        }
      } catch (error) {
        this.protocolError(error);
        applied = false;
      }
      if (applied) applied = this.options.onTurnDelta({
        conversationRevision: Number(payload.conversationRevision || 0),
        turns: Array.isArray(payload.turns) ? payload.turns : [],
        turnPatches,
        attempts: Array.isArray(payload.attempts) ? payload.attempts : [],
        deletedTurnIds: Array.isArray(payload.deletedTurnIds)
          ? payload.deletedTurnIds : [],
      });
    }
    if (!applied) {
      void this.recover('projection-revision-gap').catch(() => undefined);
      return;
    }
    this.sequence = event.syncSeq;
    this.cursorValue = lastEventId;
    this.publishHealth('live');
  }

  private touchFrame(): void {
    this.publishHealth('live');
    this.armSilenceTimer();
  }

  private armSilenceTimer(): void {
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    if (!this.source || this.closed) return;
    this.silenceTimer = setTimeout(() => {
      this.silenceTimer = null;
      if (this.closed || !this.shouldConnect) return;
      this.publishHealth('degraded', 'heartbeat-timeout');
      this.closeSource();
      this.scheduleReconnect('heartbeat-timeout');
    }, Number(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs));
  }

  private scheduleReconnect(reason: string, delay?: number): void {
    if (this.closed || !this.shouldConnect || this.reconnectTimer) return;
    const backoff = delay ?? Math.min(500 * (2 ** Math.min(this.retryCount, 5)), 15_000);
    this.publishHealth('recovering', reason);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.open();
    }, backoff);
  }

  private clearReconnectTimer(): void {
    if (!this.reconnectTimer) return;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private closeSource(): void {
    this.generation += 1;
    this.sourceScope?.destroy();
    this.sourceScope = null;
    this.source = null;
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    this.silenceTimer = null;
  }

  private publishHealth(
    state: ConnectionHealth['state'], reason?: string,
  ): void {
    this.hasPublishedHealth = true;
    const previous = conversationConnectionHealth.get(this.options.conversationId);
    const now = Date.now();
    const health: ConnectionHealth = {
      state,
      transport: 'conversation-sse',
      observedAt: now,
      generation: this.generation,
      retryCount: this.retryCount,
      lastFrameAt: state === 'live' ? now : previous?.lastFrameAt,
      reason,
    };
    conversationConnectionHealth.set(this.options.conversationId, health);
    this.options.onHealth?.(this.options.conversationId, health);
  }

  private protocolError(cause: unknown): void {
    const error = cause instanceof Error ? cause : new Error(String(cause));
    this.options.onProtocolError?.(error);
  }
}
