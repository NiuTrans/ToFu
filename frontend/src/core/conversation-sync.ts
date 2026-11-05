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
  type TurnRecord,
} from '../api/conversation-sync.generated';
import {
  materializeSnapshotReferences,
} from '../conversation/domain/tool-segment-references';
import { createLifecycleScope, type LifecycleScope } from '../lifecycle';
import { conversationConnectionHealth, SERVER_REPORTED_DEGRADATION } from './connection-health';
import {
  CONVERSATION_STREAM_EVENT_NAMES,
  conversationStreamRetryDelayMs,
  type ConversationEventSource,
  type ConversationEventSourceFactory,
} from './conversation-event-multiplexer';
import { notifyObserver } from './observer';

import {
  conversationLoadBudget, nextHistoryPageBytes, type ConversationSnapshotViewport,
} from './conversation-load-budget';
type JsonRecord = Record<string, unknown>;

export interface ConversationSyncCoordinatorOptions {
  conversationId: string;

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
  /** A heartbeat may request repair without an accompanying canonical change.
   * Ordered changes already carry their own Store/Surface demand. */
  onHealth?(conversationId: string, health: ConnectionHealth, demand?: 'heartbeat'): void;
  /* Server-stamped delivery-wedge signal (sync heartbeat/snapshot
   * `pushWithheld`): the live task's authoritative frames are withheld on a
   * storage-write wedge. Rides read-side frames because withheld frames can
   * never carry it. */
  onPushWithheld?(withheld: boolean): void;
  /** A new source now carries this replica's replay head, so reads fenced to
   * that head (such as re-reading Turns retained below a tail window) can run.
   * Best-effort: the owner must tolerate the source closing again. */
  onStreamOpened?(): void;
  /** This conversation's ordered replay; the page shares one response. */
  eventSourceFactory: ConversationEventSourceFactory;
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

/** A stream that delivered no valid frame within reconnectGraceMs. */
const STREAM_STALL_REASON = 'heartbeat-timeout';

function record(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as JsonRecord : {};
}

/* `turnPage` reads against this replica's applied replay head. A streaming
 * reply commits an attempt event every few hundred milliseconds and this
 * replica trails the server by the stream latency, so the server answers at
 * the newest head that is not older than ours (`syncSeq` names it). A page
 * read ahead of the replica publishes when the live stream applies exactly
 * that head, so it never runs ahead of replay. Streamed attempt events patch
 * only the live Turn, never older Turns or lane membership: only a Turn
 * change or snapshot (`roundRevision`) applied after the page's head
 * overtakes it. A 409 (`history_page_stale`, carrying the server head) is
 * left for a head that kept moving through the server's bounded re-reads or
 * that this replica is ahead of. A head the live stream is about to deliver
 * is awaited and the read repeated, with no snapshot: a repair snapshot also
 * replaces every loaded history page, including the one a jump is hydrating.
 * Only a head the stream cannot deliver (paused, wedged, unreported) is
 * repaired from one authoritative snapshot; a wedged stream is exactly the
 * case that never self-healed. Both budgets are bounded: a racing head
 * converges, a broken one never. */
const HISTORY_HEAD_REPAIR_ATTEMPTS = 3;
const HISTORY_HEAD_CATCH_UP_ATTEMPTS = 8;
const HISTORY_HEAD_CATCH_UP_MS = 2_000;

/** A Turn change or snapshot reached the replica after the page's head. */
class HistoryPageOvertaken extends Error {
  constructor() { super('Conversation history page is stale'); }
}

/** The live stream did not apply the head a page was read at in time. */
class HistoryHeadUndelivered extends Error {
  constructor() { super('Conversation history page is ahead of the replay stream'); }
}

function historyPageIsStale(cause: unknown): boolean {
  return Boolean(cause && typeof cause === 'object'
    && (cause as { status?: unknown }).status === 409);
}

function staleHistoryHead(cause: unknown): number {
  const head = Number(record(record(cause).body).currentSyncSeq);
  return Number.isSafeInteger(head) && head > 0 ? head : 0;
}

/**
 * Owns snapshot/cursor/SSE/recovery for exactly one conversation.
 * Push and BroadcastChannel callers may only call `invalidate`; projections
 * enter the store through this stream (or its authoritative reset snapshot).
 */
let roundDirectoryScope = 0;

export class ConversationSyncCoordinator implements ConversationSyncConnection {
  private readonly options: ConversationSyncCoordinatorOptions;
  private source: ConversationEventSource | null = null;
  private sourceScope: LifecycleScope | null = null;
  private snapshotPromise: Promise<ConversationSyncSnapshot> | null = null;
  // A reset is an obligation to replace the projection, not a cursor ACK.
  // Keep this barrier across read failures, transport wakes and suspension.
  private snapshotRequired = true;
  private readonly reads = new AbortController();
  private readonly turnPageFlights = new Map<'continuous' | 'detached', {
    key: string;
    promise: Promise<ConversationTurnPage>;
  }>();
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private silenceTimer: ReturnType<typeof setTimeout> | null = null;
  private closed = false;
  private shouldConnect = false;
  private suspended = false;
  private generation = 0;
  private retryCount = 0;
  private sequence = 0;
  private cursorValue = '';
  private serverBootId = '';
  private lastFrameAt: number | undefined;
  private sourceOpenedAt = 0;
  /* Set by the silence deadline; only a valid frame or released demand clears
   * it, so a reconnect cannot present a stalled stream as a transient one. */
  private stalled = false;
  private roundRevision = 0;
  private readonly roundScope = ++roundDirectoryScope;
  private roundOwner = 0;

  get roundDirectoryRevision(): string { return `${this.roundScope}:${this.roundOwner}:${this.roundRevision}`; }
  // History reads awaiting a head the live stream reports; one per read flight.
  private readonly headWaiters = new Set<{ sequence: number; settle(reached: boolean): void }>();
  /* Byte budget for the continuous page that chains from the last published
   * one; any other read starts from the viewport budget. */
  private historyChain: { laneId: string; beforeOrdinal: number | null; windowBytes: number } | null = null;
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
      const wasStalled = this.stalled;
      this.stalled = false;
      this.closeSource();
      this.clearReconnectTimer();
      if (wasConnected || wasStalled) this.publishHealth('idle', 'transport-suspended');
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
   * The same reset re-anchors a cursor whose missed changes would cost more
   * than one snapshot of this viewport's window.
   */
  async wake(): Promise<void> {
    if (this.closed) return;
    this.shouldConnect = true;
    if (this.suspended) return;
    this.retireSilentSource();
    await this.resume();
  }

  async hydrate(connect = true): Promise<ConversationSyncSnapshot> {
    this.shouldConnect = this.shouldConnect || connect;
    const snapshot = await this.fetchSnapshot('hydrate');
    if (this.shouldConnect) this.open('snapshot');
    return snapshot;
  }

  /** Complete projection for an explicit edit; shares owned read cancellation. */
  async loadCompleteTurn(turnId: string): Promise<TurnRecord> {
    if (this.closed) throw new DOMException('Conversation was disposed during Turn read.', 'AbortError');
    const response = await this.options.api.turn(this.options.conversationId, turnId, {
      timeout: SNAPSHOT_FETCH_TIMEOUT_MS, signal: this.reads.signal,
    });
    if (this.closed) throw new DOMException('Conversation was disposed during Turn read.', 'AbortError');
    if (response.turn.conversationId !== this.options.conversationId || response.turn.turnId !== turnId) {
      throw new Error('Turn read identity is mismatched.');
    }
    return response.turn;
  }

  async recover(reason = 'snapshot-recovery'): Promise<ConversationSyncSnapshot> {
    this.shouldConnect = true;
    const snapshot = await this.fetchSnapshot(reason);
    this.open('snapshot');
    return snapshot;
  }

  /**
   * Fetch one older lane page read at a head not older than the locally
   * applied one. Equal callers share one request. A different overlapping
   * page of the same mode is rejected so user input cannot grow a hidden
   * request queue; the continuous window and detached jumps hold one slot
   * each, so a jump never fails against the Surface's own history read.
   * Publication stays inside the fence check's synchronous turn: no
   * stream/snapshot microtask can advance local authority between validation
   * and Store ingestion.
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
    const inFlight = this.turnPageFlights.get(mode);
    if (inFlight) {
      if (inFlight.key === key) return inFlight.promise;
      throw new Error('A different conversation history page is already loading');
    }
    const request = this.readHistory(
      () => this.readTurnPage(normalizedLaneId, beforeOrdinal, limit, mode),
    ).catch((cause: unknown) => {
      this.protocolError(cause);
      throw cause;
    });
    const flight = request.finally(() => { this.turnPageFlights.delete(mode); });
    this.turnPageFlights.set(mode, { key, promise: flight });
    return flight;
  }

  private readTurnPage(
    laneId: string,
    beforeOrdinal: number | undefined,
    limit: number,
    mode: 'continuous' | 'detached',
  ): Promise<ConversationTurnPage> {
    const expectedSequence = this.sequence;
    const ranks = this.roundDirectoryRevision;
    const viewportBytes = conversationLoadBudget(this.options.snapshotViewport?.()).windowBytes;
    const chain = this.historyChain;
    const windowBytes = mode === 'continuous' && chain?.laneId === laneId
        && chain.beforeOrdinal === (beforeOrdinal ?? null)
      ? Math.max(viewportBytes, chain.windowBytes) : viewportBytes;
    /* Same wedged-flight guard as fetchSnapshot: an unbounded stalled page
     * read kept its flight slot forever, rejecting every later page with
     * "already loading" until reload. */
    return this.options.api.turnPage(
      this.options.conversationId,
      laneId,
      expectedSequence,
      beforeOrdinal,
      limit,
      { timeout: SNAPSHOT_FETCH_TIMEOUT_MS, signal: this.reads.signal, query: { windowBytes } },
    ).then(async (wirePage) => {
      if (this.closed) throw new Error('Conversation sync coordinator is closed');
      const page = materializeSnapshotReferences(wirePage);
      if (page.conversationId !== this.options.conversationId
          || page.laneId !== laneId) {
        throw new Error('Conversation history page identity mismatch');
      }
      if (page.syncSeq < expectedSequence
          || (beforeOrdinal !== undefined && page.beforeOrdinal !== beforeOrdinal)) {
        throw new Error('Conversation history page is stale');
      }
      /* The page reflects every Turn change up to its head. A page read ahead
       * of this replica is fenced when the stream applies exactly that head,
       * so a continuous page never moves the lane window or conversation
       * revision ahead of deltas still in flight; one this replica already
       * reached is fenced where its read began. */
      const fence = page.syncSeq > this.sequence
        ? await this.awaitExactHead(page.syncSeq) : ranks;
      if (this.closed) throw new Error('Conversation sync coordinator is closed');
      if (fence === null) throw new HistoryHeadUndelivered();
      if (this.roundDirectoryRevision !== fence) throw new HistoryPageOvertaken();
      this.options.onTurnPage?.(page, mode);
      if (mode === 'continuous') {
        this.historyChain = { laneId, beforeOrdinal: page.nextBeforeOrdinal ?? null,
          windowBytes: nextHistoryPageBytes(viewportBytes, {
            windowBytes, turns: page.turns.length, limit, hasMore: page.hasMore }) };
      }
      return page;
    });
  }

  private async readHistory(
    read: () => Promise<ConversationTurnPage>,
  ): Promise<ConversationTurnPage> {
    for (let repairs = 1, catchUps = 0; ;) {
      try {
        return await read();
      } catch (cause) {
        const overtaken = cause instanceof HistoryPageOvertaken;
        const undelivered = cause instanceof HistoryHeadUndelivered;
        if (this.closed || !(overtaken || undelivered || historyPageIsStale(cause))) throw cause;
        // An undelivered page head was already awaited for the whole window.
        const head = overtaken ? this.sequence : undelivered ? 0 : staleHistoryHead(cause);
        if (head > 0 && catchUps < HISTORY_HEAD_CATCH_UP_ATTEMPTS
            && await this.awaitAppliedHead(head)) {
          catchUps += 1;
        } else {
          if (repairs >= HISTORY_HEAD_REPAIR_ATTEMPTS) throw cause;
          repairs += 1;
          await this.hydrate(false);
        }
        if (this.closed) throw cause;
      }
    }
  }

  /** Whether this replica applies `sequence` through its live stream in time. */
  private awaitAppliedHead(sequence: number): Promise<boolean> {
    if (this.sequence >= sequence) return Promise.resolve(true);
    return this.awaitHead(sequence, reached => reached);
  }

  /** The round revision at the moment the live stream applies exactly
   * `sequence`, read before any later frame can apply; null when the stream
   * does not apply exactly that head in time. */
  private awaitExactHead(sequence: number): Promise<string | null> {
    return this.awaitHead(sequence, reached =>
      reached && this.sequence === sequence ? this.roundDirectoryRevision : null);
  }

  private awaitHead<T>(sequence: number, observe: (reached: boolean) => T): Promise<T> {
    if (this.closed || !this.source) return Promise.resolve(observe(false));
    return new Promise((resolve) => {
      const waiter = {
        sequence,
        settle: (reached: boolean) => {
          clearTimeout(timer);
          this.headWaiters.delete(waiter);
          resolve(observe(reached));
        },
      };
      const timer = setTimeout(() => waiter.settle(false), HISTORY_HEAD_CATCH_UP_MS);
      this.headWaiters.add(waiter);
    });
  }

  private settleHeadWaiters(streamLost = false): void {
    for (const waiter of [...this.headWaiters]) {
      if (this.sequence >= waiter.sequence) waiter.settle(true);
      else if (streamLost || this.closed) waiter.settle(false);
    }
  }

  /**
   * Exact input ranks for the reader. Ranks can change only with
   * `roundRevision` (every Turn change or replacement snapshot), while the
   * replay head also advances on every streamed attempt event, faster than
   * one read's round trip. The server therefore answers at the newest head
   * not older than ours, and a page stays current until a Turn change is
   * applied here; one it already includes reaches this replica through the
   * stream, and the reader's key change re-reads its view.
   */
  async loadRoundDirectory(
    laneId: string, offset = 0, limit = 50, anchorTurnId?: string,
  ): Promise<ConversationRoundDirectory> {
    // A replacement in flight answers the reader once applied; its failure
    // leaves the barrier below to report.
    if (this.snapshotPromise) await this.snapshotPromise.catch(() => undefined);
    if (this.closed || this.snapshotRequired || !this.cursorValue || !this.roundOwner) {
      throw new Error('Round directory requires an active snapshot with owner scope');
    }
    const expected = this.sequence;
    const revision = this.roundDirectoryRevision;
    const owner = this.roundOwner;
    const page = await this.options.api.roundDirectory(
      this.options.conversationId, laneId, expected, offset, limit, anchorTurnId,
      { timeout: SNAPSHOT_FETCH_TIMEOUT_MS, signal: this.reads.signal },
    );
    if (this.closed || this.roundDirectoryRevision !== revision || page.ownerId !== owner
        || page.stale || page.syncSeq < expected
        || page.conversationId !== this.options.conversationId || page.laneId !== laneId
        || page.offset !== offset) throw new Error('Round directory is stale or mismatched');
    return page;
  }

  invalidate(_cursorHint?: string): void {
    if (this.closed || this.suspended || !this.shouldConnect) return;
    this.retireSilentSource();
    if (!this.source) this.scheduleReconnect('invalidation-wakeup', 0);
  }

  /** Wakes may run before a throttled browser timer, but share its deadline.
   * A new source receives its own grace even after a native reconnect error;
   * the previous generation's last frame cannot expire its replacement. */
  private retireSilentSource(): void {
    if (this.source && Date.now() - Math.max(this.sourceOpenedAt, this.lastFrameAt ?? 0)
        >= Number(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs)) {
      this.closeSource();
    }
  }

  pause(): void {
    this.shouldConnect = false;
    this.stalled = false;
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
              this.serverBootId = snapshot.serverBootId;
              this.snapshotRequired = false;
              // Generated clients validate scope. Keep legacy injected snapshot
              // ports usable, but never admit their unscoped rank navigation.
              this.roundOwner = Number(snapshot.scope?.ownerId || 0);
              if (!Number.isSafeInteger(this.roundOwner) || this.roundOwner <= 0) this.roundOwner = 0;
              this.roundRevision++;
              this.settleHeadWaiters();
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

  /** `snapshot` marks a cursor just minted by the snapshot this source follows:
   * its first connection replays that round trip without the catch-up budget.
   * Every other open resumes a retained cursor, whose missed changes the
   * server replays only while they cost less than this viewport's snapshot. */
  private open(cursorSource?: 'snapshot'): void {
    if (this.closed || this.suspended || !this.shouldConnect || this.source
        || this.snapshotPromise || this.snapshotRequired) return;
    this.clearReconnectTimer();
    this.generation += 1;
    const sourceGeneration = this.generation;
    let source: ConversationEventSource;
    try {
      source = this.options.eventSourceFactory({
        conversationId: this.options.conversationId,
        after: this.cursorValue,
        windowBytes: conversationLoadBudget(this.options.snapshotViewport?.()).windowBytes,
        ...(cursorSource ? { cursorSource } : {}),
      });
    } catch (error) {
      this.retryCount += 1;
      this.protocolError(error);
      this.scheduleReconnect('event-source-setup-failed');
      throw error;
    }
    const scope = createLifecycleScope();
    this.source = source;
    this.sourceOpenedAt = Date.now();
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
      this.publishHealth('connecting');
      // Opening a socket is not receipt of a valid frame. Neither repeated
      // opens nor errors may postpone the existing last-frame deadline.
      this.armSilenceTimer(false);
    };
    source.onmessage = ingest as (event: MessageEvent) => void;
    for (const name of CONVERSATION_STREAM_EVENT_NAMES) {
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
    if (sourceGeneration !== this.generation) return;
    this.armSilenceTimer(false);
    notifyObserver('conversation-stream-opened', this.options.onStreamOpened);
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
    if (event.type === 'sync.heartbeat' && (event.cursor !== this.cursorValue
        || event.serverBootId !== this.serverBootId)) {
      // Liveness is not a projection acknowledgement. A skipped suffix or
      // restarted source cannot move the only recovery cursor past the Store.
      void this.recover('heartbeat-cursor-mismatch').catch(() => undefined);
      return;
    }
    this.touchFrame(event.type !== 'sync.heartbeat');
    if (this.closed || generation !== this.generation) return;
    if (event.type === 'sync.heartbeat') {
      /* pushWithheld marks a WRITE-side wedge (authoritative pushes withheld
       * on storage retries) — distinct from the read-side degradation that
       * `degraded` alone has always meant. Both stall this conversation's
       * delivery, so both publish degraded; the reason tells them apart and,
       * because this frame arrived, keeps them off the network badge. */
      const wedged = event.pushWithheld === true;
      this.publishHealth(event.degraded || wedged ? 'degraded' : 'live',
        wedged ? SERVER_REPORTED_DEGRADATION.storageWriteWedged
          : event.degraded ? SERVER_REPORTED_DEGRADATION.storageRead : undefined, 'heartbeat');
      if (this.closed || generation !== this.generation) return;
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
        // Streamed progress patches only the live Turn; related Turns the
        // attempt announces (critic, virtual user) are Turn changes.
        const related = record(attemptEvent.payload).turns;
        if (applied && Array.isArray(related) && related.length) this.roundRevision++;
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
      // Every Turn change, not only creation or deletion: a projection patch
      // can also delete Turns, and it overtakes a detached page read before it.
      if (applied) this.roundRevision++;
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
    this.settleHeadWaiters();
    this.publishHealth('live');
  }

  private touchFrame(announceLive: boolean): void {
    this.stalled = false;
    this.lastFrameAt = Date.now();
    this.retryCount = 0;
    // Heartbeats publish their final live/degraded verdict once below. An
    // intermediate live callback duplicates Surface repair demand and can
    // momentarily erase a storage-wedge notice on every degraded heartbeat.
    if (announceLive) this.publishHealth('live');
    this.armSilenceTimer();
  }

  private armSilenceTimer(reset = true): void {
    if (!reset && this.silenceTimer) return;
    if (this.silenceTimer) clearTimeout(this.silenceTimer);
    if (!this.source || this.closed) return;
    this.silenceTimer = setTimeout(() => {
      this.silenceTimer = null;
      if (this.closed || !this.shouldConnect) return;
      this.stalled = true;
      this.publishHealth('degraded', STREAM_STALL_REASON);
      this.retryCount += 1;
      this.closeSource();
      this.scheduleReconnect(STREAM_STALL_REASON);
    }, Number(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs));
  }

  private scheduleReconnect(reason: string, delay?: number): void {
    if (this.closed || this.suspended || !this.shouldConnect || this.reconnectTimer) return;
    const backoff = delay ?? conversationStreamRetryDelayMs(this.retryCount);
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
    this.settleHeadWaiters(true);
  }

  private publishHealth(
    state: ConnectionHealth['state'], reason?: string, demand?: 'heartbeat',
  ): void {
    if (this.closed && state !== 'closed') return;
    this.hasPublishedHealth = true;
    const now = Date.now();
    // A stalled stream stays degraded through its reconnect until a valid
    // frame proves delivery again.
    const held = this.stalled && (state === 'connecting' || state === 'recovering');
    const health: ConnectionHealth = {
      state: held ? 'degraded' : state,
      transport: 'conversation-sse',
      observedAt: now,
      generation: this.generation,
      retryCount: this.retryCount,
      lastFrameAt: this.lastFrameAt,
      reason: held ? reason ?? STREAM_STALL_REASON : reason,
    };
    conversationConnectionHealth.set(this.options.conversationId, health);
    if (!this.closed || state === 'closed') {
      notifyObserver('conversation-health', this.options.onHealth, this.options.conversationId, health, demand);
    }
  }

  private protocolError(cause: unknown): void {
    if (this.closed) return;
    const error = cause instanceof Error ? cause : new Error(String(cause));
    notifyObserver('conversation-protocol-error', this.options.onProtocolError, error);
  }
}
