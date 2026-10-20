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
  type ConversationTurnPage,
  type ConversationRoundDirectory,
  type ConnectionHealth,
  type TurnProjectionChange,
} from '../api/conversation-sync.generated';
import {
  materializeSnapshotReferences,
} from '../conversation/domain/tool-segment-references';
import { createLifecycleScope, type LifecycleScope } from '../lifecycle';
import { conversationConnectionHealth } from './connection-health';
import { notifyObserver } from './observer';

import { conversationLoadBudget, type ConversationSnapshotViewport } from './conversation-load-budget';
type JsonRecord = Record<string, unknown>;

export interface ConversationSyncCoordinatorOptions {
  conversationId: string;
  streamClientId?: string;

  snapshotViewport?(): ConversationSnapshotViewport;
  api: ConversationSyncApi;
  onSnapshot(snapshot: ConversationSyncSnapshot & { authoritativeFull: true }): void;
  /** Publishes one sequence-checked history page synchronously. */
  onTurnPage?(page: ConversationTurnPage, mode?: 'continuous' | 'detached'): void;
  onAttemptEvent(
    event: AttemptEvent,
    receivedAt: number,
    serverPublishedAt: number,
  ): boolean;
  /** Diagnostic evidence frame (messages_snapshot, tool_compacted) carried
   * inside its durable attempt event (server: turn_lifecycle
   * _DEBUG_EVIDENCE_EVENTS). Never mutates turn state; delivered on live
   * push AND on replay so the debug panel can rebuild either way. */
  onDebugFrame?(conversationId: string, frame: JsonRecord): void;
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

/* Hard ceiling for one snapshot read. The typed transport only enforces a
 * timeout when one is declared, and an unbounded stalled GET never settles:
 * fetchSnapshot coalesces callers into `snapshotPromise` and the turn
 * runtime coalesces hydrations into one lane, so a single wedged read kept
 * every current and future hydrate pending until the page was reloaded —
 * the composer send lock then stayed latched and Enter/Send silently did
 * nothing. A bounded read rejects instead, clearing both flights so the
 * next hydrate retries against a healthy connection. 30s is far above a
 * healthy tail-window snapshot yet far below any user tolerating a dead
 * composer. */
const SNAPSHOT_FETCH_TIMEOUT_MS = 30_000;

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

/* `turnPage` fences the page against the applied replay head and answers 409
 * (`history_page_stale`) when a write committed past it. That is a routine
 * race while an attempt streams, not a broken projection: repair the head from
 * one authoritative snapshot and retry the same cursor. Without this, the
 * Surface kept retrying a sequence the server had already left behind, so
 * earlier turns stayed unreachable until the page was reloaded. A wedged
 * stream (no frames, so no sequence advance) is exactly the case that never
 * self-healed. Bounded because each repair is a full snapshot read. */
const HISTORY_HEAD_REPAIR_ATTEMPTS = 3;

function historyPageIsStale(cause: unknown): boolean {
  return Boolean(cause && typeof cause === 'object'
    && (cause as { status?: unknown }).status === 409);
}

async function readWithRepairedHead<T>(
  read: () => Promise<T>,
  repair: () => Promise<unknown>,
  closed: () => boolean,
): Promise<T> {
  for (let attempt = 1; ; attempt++) {
    try {
      return await read();
    } catch (cause) {
      if (closed() || !historyPageIsStale(cause)
          || attempt >= HISTORY_HEAD_REPAIR_ATTEMPTS) throw cause;
      await repair();
      if (closed()) throw cause;
    }
  }
}

/**
 * Owns snapshot/cursor/SSE/recovery for exactly one conversation.
 * Push and BroadcastChannel callers may only call `invalidate`; projections
 * enter the store through this stream (or its authoritative reset snapshot).
 */
let roundDirectoryScope = 0;

export class ConversationSyncCoordinator implements ConversationSyncConnection {
  private readonly options: ConversationSyncCoordinatorOptions;
  private source: EventSource | null = null;
  private sourceScope: LifecycleScope | null = null;
  private snapshotPromise: Promise<ConversationSyncSnapshot> | null = null;
  // A reset is an obligation to replace the projection, not a cursor ACK.
  // Keep this barrier across read failures, transport wakes and suspension.
  private snapshotRequired = true;
  private readonly reads = new AbortController();
  private turnPageFlight: {
    key: string;
    promise: Promise<ConversationTurnPage>;
  } | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private silenceTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private shouldConnect = false;
  private suspended = false;
  private generation = 0;
  private retryCount = 0;
  private sequence = 0;
  private cursorValue = '';
  private roundRevision = 0;
  private readonly roundScope = ++roundDirectoryScope;
  private roundOwner = 0;

  get roundDirectoryRevision(): string { return `${this.roundScope}:${this.roundOwner}:${this.roundRevision}`; }
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

  /** Release transport capacity while retaining demand, projection and cursor. */
  setSuspended(suspended: boolean): void {
    if (this.closed || this.suspended === suspended) return;
    this.suspended = suspended;
    if (suspended) {
      const wasConnected = this.source !== null;
      this.closeSource();
      this.clearReconnectTimer();
      if (wasConnected) this.publishHealth('idle', 'transport-suspended');
    } else if (this.shouldConnect) {
      // The shared read/open path already reports and schedules failures.
      void this.resume().catch(() => undefined);
    }
  }

  async resume(): Promise<void> {
    if (this.closed) return;
    this.shouldConnect = true;
    if (this.suspended || this.reconnectTimer) return;
    if (this.snapshotRequired || !this.cursorValue) {
      await this.hydrate(true);
      return;
    }
    this.open();
  }

  /**
   * Resume after a browser/network wake without crossing the snapshot boundary.
   * A healthy source is retained; a stale or missing source reopens from the
   * last durable cursor. The server remains responsible for proving cursor
   * expiry with sync.reset_required, which then performs one recovery snapshot.
   */
  async wake(): Promise<void> {
    if (this.closed) return;
    this.shouldConnect = true;
    if (this.suspended) return;
    const health = conversationConnectionHealth.get(this.options.conversationId);
    const frameAge = Date.now() - Number(health?.lastFrameAt || 0);
    // A source opened by an immediately preceding wake has not had a chance
    // to publish its first frame yet. Keep that connecting generation; using
    // the pre-sleep lastFrameAt to judge it stale would churn one EventSource
    // per pageshow/online hint after a long browser suspension.
    if (this.source && health?.state !== 'connecting' && frameAge
        > Number(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs) / 2) {
      this.closeSource();
    }
    await this.resume();
  }

  async hydrate(connect = true): Promise<ConversationSyncSnapshot> {
    this.shouldConnect = this.shouldConnect || connect;
    const snapshot = await this.fetchSnapshot('hydrate');
    if (this.shouldConnect) this.open();
    return snapshot;
  }

  async recover(reason = 'snapshot-recovery'): Promise<ConversationSyncSnapshot> {
    this.shouldConnect = true;
    const snapshot = await this.fetchSnapshot(reason);
    this.open();
    return snapshot;
  }

  /**
   * Fetch one older lane page against the exact locally-applied replay head.
   * Equal callers share one request. A different overlapping page is rejected
   * so user input cannot grow a hidden request queue. Publication stays inside
   * the sequence check's synchronous turn: no stream/snapshot microtask can
   * advance local authority between validation and Store ingestion.
   */
  async loadTurnPage(
    laneId: string,
    beforeOrdinal?: number,
    limit = 64,
    mode: 'continuous' | 'detached' = 'continuous',
  ): Promise<ConversationTurnPage> {
    if (this.closed) throw new Error('Conversation sync coordinator is closed');
    if (this.snapshotRequired || !this.cursorValue) {
      throw new Error('Conversation history requires an authoritative snapshot');
    }
    const normalizedLaneId = laneId.trim();
    if (!normalizedLaneId) throw new Error('Conversation history requires a lane');
    if (beforeOrdinal !== undefined
        && (!Number.isSafeInteger(beforeOrdinal) || beforeOrdinal < 0)) {
      throw new Error('Conversation history has an invalid before ordinal');
    }
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 256) {
      throw new Error('Conversation history has an invalid page limit');
    }
    const key = `${normalizedLaneId}:${beforeOrdinal ?? 'head'}:${limit}:${mode}`;
    if (this.turnPageFlight) {
      if (this.turnPageFlight.key === key) return this.turnPageFlight.promise;
      throw new Error('A different conversation history page is already loading');
    }
    const request = readWithRepairedHead(
      () => this.readTurnPage(normalizedLaneId, beforeOrdinal, limit, mode),
      () => this.hydrate(false), () => this.closed,
    ).catch((cause: unknown) => {
      this.protocolError(cause);
      throw cause;
    });
    const flight = request.finally(() => { this.turnPageFlight = null; });
    this.turnPageFlight = { key, promise: flight };
    return flight;
  }

  private readTurnPage(
    laneId: string,
    beforeOrdinal: number | undefined,
    limit: number,
    mode: 'continuous' | 'detached',
  ): Promise<ConversationTurnPage> {
    const expectedSequence = this.sequence;
    const expectedGeneration = this.generation;
    /* Same wedged-flight guard as fetchSnapshot: an unbounded stalled page
     * read kept `turnPageFlight` forever, rejecting every later page with
     * "already loading" until reload. */
    return this.options.api.turnPage(
      this.options.conversationId,
      laneId,
      expectedSequence,
      beforeOrdinal,
      limit,
      { timeout: SNAPSHOT_FETCH_TIMEOUT_MS, signal: this.reads.signal, query: {
        windowBytes: conversationLoadBudget(this.options.snapshotViewport?.()).windowBytes,
      } },
    ).then((wirePage) => {
      if (this.closed) throw new Error('Conversation sync coordinator is closed');
      const page = materializeSnapshotReferences(wirePage);
      if (page.conversationId !== this.options.conversationId
          || page.laneId !== laneId) {
        throw new Error('Conversation history page identity mismatch');
      }
      if (page.syncSeq !== expectedSequence || this.sequence !== expectedSequence
          || this.generation !== expectedGeneration
          || (beforeOrdinal !== undefined && page.beforeOrdinal !== beforeOrdinal)) {
        throw new Error('Conversation history page is stale');
      }
      this.options.onTurnPage?.(page, mode);
      return page;
    });
  }

  async loadRoundDirectory(
    laneId: string, offset = 0, limit = 50, anchorTurnId?: string,
  ): Promise<ConversationRoundDirectory> {
    if (this.closed || this.snapshotRequired || !this.cursorValue || !this.roundOwner) {
      throw new Error('Round directory requires an active snapshot with owner scope');
    }
    const expected = this.sequence;
    const expectedGeneration = this.generation;
    const owner = this.roundOwner;
    const page = await this.options.api.roundDirectory(
      this.options.conversationId, laneId, expected, offset, limit, anchorTurnId,
      { timeout: SNAPSHOT_FETCH_TIMEOUT_MS, signal: this.reads.signal },
    );
    if (this.closed || this.generation !== expectedGeneration
        || this.roundOwner !== owner || page.ownerId !== owner
        || this.sequence !== expected || page.stale || page.syncSeq !== expected
        || page.conversationId !== this.options.conversationId || page.laneId !== laneId
        || page.offset !== offset) throw new Error('Round directory is stale or mismatched');
    return page;
  }

  invalidate(_cursorHint?: string): void {
    if (this.closed || this.suspended || !this.shouldConnect) return;
    const health = conversationConnectionHealth.get(this.options.conversationId);
    const frameAge = Date.now() - Number(health?.lastFrameAt || 0);
    if (this.source && health?.state !== 'connecting' && frameAge
        > Number(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs) / 2) {
      this.closeSource();
    }
    if (!this.source) this.scheduleReconnect('invalidation-wakeup', 0);
  }

  pause(): void {
    this.shouldConnect = false;
    this.closeSource();
    this.clearReconnectTimer();
    if (!this.closed) this.publishHealth('idle');
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.shouldConnect = false;
    this.closeSource();
    this.clearReconnectTimer();
    this.reads.abort();
    this.publishHealth('closed');
    conversationConnectionHealth.clear(this.options.conversationId);
  }

  private async fetchSnapshot(reason: string): Promise<ConversationSyncSnapshot> {
    if (this.closed) throw new Error('Conversation sync coordinator is closed');
    if (this.snapshotPromise) return this.snapshotPromise;
    this.snapshotRequired = true;
    this.clearReconnectTimer();
    let startSnapshot!: () => void;
    // A snapshot captures an earlier replay head than events arriving while
    // its request is in flight. Fence that generation before the read, then
    // resume from the snapshot cursor; never roll an active projection back.
    this.closeSource();
    const operation = new Promise<ConversationSyncSnapshot>((resolve, reject) => {
      startSnapshot = () => {
        try {
          this.publishHealth(
            reason === 'hydrate' ? 'connecting' : 'recovering', reason,
          );
          if (this.closed) throw new Error('Conversation sync coordinator is closed');
          const request = this.options.api.snapshot(
            this.options.conversationId,
            { timeout: SNAPSHOT_FETCH_TIMEOUT_MS, signal: this.reads.signal,
              query: conversationLoadBudget(this.options.snapshotViewport?.()),
            },
          )
            .then((wireSnapshot) => {
              if (this.closed) throw new Error('Conversation sync coordinator is closed');
              const snapshot = materializeSnapshotReferences(
                wireSnapshot,
              );
              if (snapshot.conversationId !== this.options.conversationId) {
                throw new Error('Conversation snapshot identity mismatch');
              }
              // Ingestion is required, unlike health/debug observers. Never
              // acknowledge a read whose projection was rejected by its owner.
              this.options.onSnapshot({ ...snapshot, authoritativeFull: true });
              if (this.closed) throw new Error('Conversation sync coordinator is closed');
              this.sequence = snapshot.syncSeq;
              this.cursorValue = snapshot.cursor;
              this.snapshotRequired = false;
              // Generated clients validate scope. Keep legacy injected snapshot
              // ports usable, but never admit their unscoped rank navigation.
              this.roundOwner = Number(snapshot.scope?.ownerId || 0);
              if (!Number.isSafeInteger(this.roundOwner) || this.roundOwner <= 0) this.roundOwner = 0;
              this.roundRevision++;
              notifyObserver('conversation-push-withheld', this.options.onPushWithheld,
                snapshot.pushWithheld === true);
              if (this.closed) return snapshot;
              // A successful read repairs state, but does not prove that the
              // stream can open. Only a valid stream frame resets backoff.
              if (this.shouldConnect && !this.suspended) this.publishHealth('connecting');
              else this.publishHealth('idle');
              return snapshot;
            });
          void request.then(resolve, reject);
        } catch (cause) {
          reject(cause);
        }
      };
    });
    const flight = operation.catch((cause: unknown) => {
      if (!this.closed) {
        this.retryCount += 1;
        this.protocolError(cause);
        this.scheduleReconnect(reason);
      }
      throw cause;
    }).finally(() => { this.snapshotPromise = null; });
    // Claim the flight before publishing health. Health observers may render
    // synchronously and re-enter hydrate/resume; they must join this request.
    this.snapshotPromise = flight;
    startSnapshot();
    return flight;
  }

  private open(): void {
    if (this.closed || this.suspended || !this.shouldConnect || this.source
        || this.snapshotPromise || this.snapshotRequired) return;
    this.clearReconnectTimer();
    this.generation += 1;
    const sourceGeneration = this.generation;
    const factory = this.options.eventSourceFactory
      ?? ((url: string) => new EventSource(url));
    let source: EventSource;
    try {
      source = factory(this.options.api.eventsUrl(
        this.options.conversationId,
        this.cursorValue,
        this.options.streamClientId,
        this.options.streamClientId ? sourceGeneration : 0,
      ));
    } catch (error) {
      this.retryCount += 1;
      this.protocolError(error);
      this.scheduleReconnect('event-source-setup-failed');
      throw error;
    }
    const scope = createLifecycleScope();
    this.source = source;
    this.sourceScope = scope;
    scope.add(() => {
      source.onopen = null;
      source.onmessage = null;
      source.onerror = null;
      source.close();
    });

    const ingest = (message: MessageEvent<string>): void => {
      if (this.closed || sourceGeneration !== this.generation) return;
      const receivedAt = Date.now();
      let event: ConversationSyncEvent;
      try {
        event = decodeConversationSyncEvent(JSON.parse(message.data) as unknown);
      } catch (error) {
        this.protocolError(error);
        void this.recover('invalid-stream-frame').catch(() => undefined);
        return;
      }
      try {
        this.ingest(event, message.lastEventId || '', receivedAt);
      } catch (error) {
        this.protocolError(error);
        void this.recover('projection-ingestion-failed').catch(() => undefined);
      }
    };

    source.onopen = () => {
      if (sourceGeneration !== this.generation) return;
      this.publishHealth('live');
      // Opening a socket is not receipt of a valid frame. Neither repeated
      // opens nor errors may postpone the existing last-frame deadline.
      this.armSilenceTimer(false);
    };
    source.onmessage = ingest as (event: MessageEvent) => void;
    for (const name of CHANGE_EVENT_NAMES) {
      scope.listen(source, name, ingest as EventListener);
    }
    source.onerror = () => {
      if (this.closed || sourceGeneration !== this.generation) return;
      if (source.readyState === 2 /* EventSource.CLOSED */) {
        // HTTP 204 and fatal transport refusals stop native retry permanently.
        // Repair the displayed authority before opening a replacement. Retain
        // backoff across successful snapshots so persistent refusal cannot
        // turn task wake hints into a read/reconnect storm.
        this.retryCount += 1;
        this.snapshotRequired = true;
        this.closeSource();
        this.scheduleReconnect('event-source-closed');
        return;
      }
      // Native EventSource resumes with Last-Event-ID.  Brief transport flaps
      // remain "recovering" and do not poison the badge; the silence deadline
      // below promotes only a genuinely stalled pipe to degraded.
      this.retryCount += 1;
      this.publishHealth('recovering', 'event-source-error');
      this.armSilenceTimer(false);
    };
    this.publishHealth('connecting');
    if (sourceGeneration === this.generation) this.armSilenceTimer(false);
  }

  private ingest(
    event: ConversationSyncEvent,
    lastEventId: string,
    receivedAt: number,
  ): void {
    if (event.conversationId !== this.options.conversationId) {
      this.protocolError(new Error('Conversation stream identity mismatch'));
      void this.recover('identity-mismatch').catch(() => undefined);
      return;
    }
    const generation = this.generation;
    this.touchFrame();
    if (this.closed || generation !== this.generation) return;
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
      notifyObserver('conversation-push-withheld', this.options.onPushWithheld, wedged);
      return;
    }
    if (event.type === 'sync.reset_required') {
      void this.recover(event.reason).catch(() => undefined);
      return;
    }
    this.applyChange(event, lastEventId, receivedAt);
  }

  private applyChange(
    event: ConversationChange,
    lastEventId: string,
    receivedAt: number,
  ): void {
    const generation = this.generation;
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
        applied = this.options.onAttemptEvent(
          attemptEvent,
          receivedAt,
          Number(event.occurredAt || 0),
        );
        if (this.closed || generation !== this.generation) return;
        const debugFrame = record(record(attemptEvent.payload).event);
        if (typeof debugFrame.type === 'string' && debugFrame.type) {
          notifyObserver('conversation-debug-frame', this.options.onDebugFrame,
            this.options.conversationId, debugFrame,
          );
        }
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
      if (applied && event.type !== 'turn.patch') this.roundRevision++;
      if (applied) applied = this.options.onTurnDelta({
        conversationRevision: Number(payload.conversationRevision || 0),
        turns: Array.isArray(payload.turns) ? payload.turns : [],
        turnPatches,
        attempts: Array.isArray(payload.attempts) ? payload.attempts : [],
        queueItemUpserts: Array.isArray(payload.queueItemUpserts)
          ? payload.queueItemUpserts : [],
        removedQueueIds: Array.isArray(payload.removedQueueIds)
          ? payload.removedQueueIds : [],
        deletedTurnIds: Array.isArray(payload.deletedTurnIds)
          ? payload.deletedTurnIds : [],
      });
    }
    if (this.closed || generation !== this.generation) return;
    if (!applied) {
      void this.recover('projection-revision-gap').catch(() => undefined);
      return;
    }
    this.sequence = event.syncSeq;
    this.cursorValue = lastEventId;
    this.publishHealth('live');
  }

  private touchFrame(): void {
    this.retryCount = 0;
    this.publishHealth('live');
    this.armSilenceTimer();
  }

  private armSilenceTimer(reset = true): void {
    if (!reset && this.silenceTimer) return;
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    if (!this.source || this.closed) return;
    this.silenceTimer = setTimeout(() => {
      this.silenceTimer = null;
      if (this.closed || !this.shouldConnect) return;
      this.publishHealth('degraded', 'heartbeat-timeout');
      this.retryCount += 1;
      this.closeSource();
      this.scheduleReconnect('heartbeat-timeout');
    }, Number(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs));
  }

  private scheduleReconnect(reason: string, delay?: number): void {
    if (this.closed || this.suspended || !this.shouldConnect || this.reconnectTimer) return;
    const backoff = delay ?? Math.min(500 * (2 ** Math.min(this.retryCount, 5)), 15_000);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      // Wakes and retries use the same snapshot barrier as explicit resume.
      // Failures are reported and scheduled by fetchSnapshot/open themselves.
      void this.resume().catch(() => undefined);
    }, backoff);
    // Claim before observers run: a synchronous invalidation must join this
    // timer, and pause/close must be able to cancel it immediately.
    this.publishHealth('recovering', reason);
  }

  private clearReconnectTimer(): void {
    if (!this.reconnectTimer) return;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private closeSource(): void {
    this.generation += 1;
    const scope = this.sourceScope;
    this.sourceScope = null;
    this.source = null;
    scope?.destroy();
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    this.silenceTimer = null;
  }

  private publishHealth(
    state: ConnectionHealth['state'], reason?: string,
  ): void {
    if (this.closed && state !== 'closed') return;
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
    if (!this.closed || state === 'closed') {
      notifyObserver('conversation-health', this.options.onHealth, this.options.conversationId, health);
    }
  }

  private protocolError(cause: unknown): void {
    if (this.closed) return;
    const error = cause instanceof Error ? cause : new Error(String(cause));
    notifyObserver('conversation-protocol-error', this.options.onProtocolError, error);
  }
}
