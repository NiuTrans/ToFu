import type {
  ActiveAttemptPhase,
  AttemptEvent as ContractAttemptEvent,
  AttemptRecord as ContractAttemptRecord,
  ConversationQueueItem as ContractConversationQueueItem,
  ConversationTurnWindow,
  TurnProjectionChange,
  TurnStatus,
} from '../api/conversation-sync.generated';
import type { LiveRoundUsage, ProjectionTurn } from './turn-projection';
import { applyProjectionPatch } from './projection-patch';

type UnknownRecord = Record<string, unknown>;

export type AttemptRecord = Partial<ContractAttemptRecord> & {
  attemptId: string;
  lastSeq?: number;
  interactionRequest?: unknown;
};

export type ConversationQueueItem = ContractConversationQueueItem;

export type TurnEvent = ContractAttemptEvent;

export interface TurnSnapshotInput extends UnknownRecord {
  conversationRevision?: number;
  authoritativeFull?: boolean;
  activeAttemptPhase?: ActiveAttemptPhase;
  turns?: ReadonlyArray<ProjectionTurn>;
  attempts?: ReadonlyArray<AttemptRecord>;
  queueItems?: ReadonlyArray<ConversationQueueItem>;
  queueItemUpserts?: ReadonlyArray<ConversationQueueItem>;
  removedQueueIds?: ReadonlyArray<string>;
  deletedTurnIds?: ReadonlyArray<string>;
  turnPatches?: ReadonlyArray<TurnProjectionChange>;
  turnWindow?: ConversationTurnWindow;
  laneId?: string;
  nextBeforeOrdinal?: number | null;
  hasMore?: boolean;
  totalTurns?: number;
  /* Server-stamped delivery-wedge signal (sync snapshot authority). Delta
   * snapshots never carry it; the fold below only applies a present key. */
  pushWithheld?: boolean;
}

export interface TurnHistoryLaneState {
  nextBeforeOrdinal: number | null;
  hasMore: boolean;
  totalTurns: number;
}

export interface TurnCommandResult extends UnknownRecord {
  conversationRevision?: number;
  submittedTurn?: ProjectionTurn;
  turn?: ProjectionTurn;
  turns?: ReadonlyArray<ProjectionTurn>;
  attempt?: AttemptRecord;
  attempts?: ReadonlyArray<AttemptRecord>;
  queueItem?: ConversationQueueItem;
  deletedTurnIds?: ReadonlyArray<string>;
}

export interface TurnState {
  conversationId: string;
  conversationRevision: number;
  turnsById: Record<string, ProjectionTurn | undefined>;
  laneOrder: Record<string, string[] | undefined>;
  historyByLane: Record<string, TurnHistoryLaneState | undefined>;
  attemptsById: Record<string, AttemptRecord | undefined>;
  queueItems: ConversationQueueItem[];
  pendingEventsByTurn: Record<string, TurnEvent[] | undefined>;
  commandPending: Record<string, string | null | undefined>;
  liveRoundUsageByTurn: Record<string, LiveRoundUsage | undefined>;
  transport: string;
  /**
   * Live phase snapshot (waiting_model / llm_thinking / tool_exec / …) of the
   * conversation's current attempt.  It rides the EVENT payload (`phase` key,
   * `null` clears) — deliberately never the persisted turn projection, so a
   * stale phase can never survive into a settled document.  Replaying an
   * attempt log reconstructs the latest phase deterministically.
   */
  livePhase?: unknown;
  /**
   * Delivery-wedge flag: the live task's authoritative frames are being
   * withheld server-side (durable-before-visible, storage write wedge).
   * Rides sync heartbeats/snapshots — the withheld frames themselves can
   * never carry it. The live-status block renders the honest wedge label
   * instead of the generic waiting placeholder while true.
   */
  pushWithheld?: boolean;
}

export type TurnAction =
  | { type: 'snapshot'; snapshot: TurnSnapshotInput }
  | { type: 'command_response'; response: TurnCommandResult }
  | { type: 'event'; event: TurnEvent }
  | { type: 'command_pending'; turnId: string; operation: string }
  | { type: 'command_failed'; turnId: string }
  | { type: 'push_withheld'; pushWithheld: boolean }
  | { type: 'transport'; status: string };

export interface ReduceTurnStateOptions {
  onUnknownTurn?(turnId: string): void;
  onProjectionGap?(turnId: string): void;
}

export interface TurnStoreOptions {
  fetchSnapshot?(): Promise<TurnSnapshotInput | null | undefined>;
  onResyncError?(error: unknown, turnId: string): void;
}

/** Ephemeral reducer transition; subscribers do not own another state copy. */
export interface TurnStateUpdate {
  previousState: TurnState;
  action: TurnAction;
}

export interface TurnStore {
  getState(): TurnState;
  dispatch(action: TurnAction): TurnState;
  subscribe(listener: (state: TurnState, update: TurnStateUpdate) => void): () => void;
  _fetchSnapshot?: TurnStoreOptions['fetchSnapshot'];
  _snapshotLoaded?: boolean;
}

export function createTurnState(conversationId: string): TurnState {
  return {
    conversationId,
    conversationRevision: 0,
    turnsById: Object.create(null) as Record<string, ProjectionTurn>,
    laneOrder: Object.create(null) as Record<string, string[]>,
    historyByLane: Object.create(null) as Record<string, TurnHistoryLaneState>,
    attemptsById: Object.create(null) as Record<string, AttemptRecord>,
    queueItems: [],
    pendingEventsByTurn: Object.create(null) as Record<string, TurnEvent[]>,
    commandPending: Object.create(null) as Record<string, string>,
    liveRoundUsageByTurn: Object.create(null) as Record<string, LiveRoundUsage>,
    transport: 'idle',
    pushWithheld: false,
  };
}

function copyState(state: TurnState): TurnState {
  return {
    ...state,
    turnsById: { ...state.turnsById },
    laneOrder: Object.fromEntries(Object.entries(state.laneOrder)
      .map(([lane, ids]) => [lane, (ids ?? []).slice()])),
    historyByLane: Object.fromEntries(Object.entries(state.historyByLane ?? {})
      .map(([lane, history]) => [lane, history ? { ...history } : undefined])),
    attemptsById: { ...state.attemptsById },
    queueItems: state.queueItems.map((item) => ({ ...item })),
    pendingEventsByTurn: Object.fromEntries(
      Object.entries(state.pendingEventsByTurn)
        .map(([turnId, events]) => [turnId, (events ?? []).slice()]),
    ),
    commandPending: { ...state.commandPending },
    liveRoundUsageByTurn: { ...state.liveRoundUsageByTurn },
  };
}

function putTurn(next: TurnState, turn: ProjectionTurn | null | undefined): void {
  if (!turn?.turnId) return;
  const previous = next.turnsById[turn.turnId];
  if (previous && Number(turn.projectionRevision || 0)
      < Number(previous.projectionRevision || 0)) return;
  const merged = { ...previous, ...turn };
  next.turnsById[turn.turnId] = merged;
  const liveUsage = next.liveRoundUsageByTurn[turn.turnId];
  if (liveUsage && (liveUsage.attemptId !== merged.currentAttemptId
      || ['completed', 'interrupted', 'truncated', 'failed']
        .includes(merged.status || ''))) {
    delete next.liveRoundUsageByTurn[turn.turnId];
  }
  const lane = turn.laneId || 'main';
  const ids = next.laneOrder[lane] ?? (next.laneOrder[lane] = []);
  if (!ids.includes(turn.turnId)) ids.push(turn.turnId);
  ids.sort((leftId, rightId) => (
    Number(next.turnsById[leftId]?.ordinal || 0)
      - Number(next.turnsById[rightId]?.ordinal || 0)
  ));
  const history = next.historyByLane[lane];
  if (!previous && history) {
    history.totalTurns = Math.max(ids.length, history.totalTurns + 1);
  }
}

const attemptStatusRank: Record<string, number> = {
  pending: 0,
  running: 1,
  completed: 2,
  interrupted: 2,
  truncated: 2,
  failed: 2,
  superseded: 3,
};

const TURN_STATUSES = new Set<TurnStatus>([
  'pending', 'running', 'completed', 'interrupted', 'truncated', 'failed',
]);

function turnStatus(value: unknown, fallback: TurnStatus): TurnStatus {
  return typeof value === 'string' && TURN_STATUSES.has(value as TurnStatus)
    ? value as TurnStatus
    : fallback;
}

function putAttempt(next: TurnState, attempt: AttemptRecord | null | undefined): void {
  if (!attempt?.attemptId) return;
  const previous = next.attemptsById[attempt.attemptId] ?? { attemptId: attempt.attemptId };
  const previousRank = attemptStatusRank[previous.status || ''] ?? 0;
  const incomingRank = attemptStatusRank[attempt.status || ''] ?? 0;
  const incoming = incomingRank < previousRank
    ? { ...attempt, status: previous.status }
    : attempt;
  next.attemptsById[attempt.attemptId] = { ...previous, ...incoming };
}

// Recovery is authoritative; unknown-turn transport data is only a bounded
// bridge until that snapshot arrives, never a second replay log.
const MAX_PENDING_EVENTS = 256;
const MAX_PENDING_EVENT_BYTES = 1024 * 1024;

function hasDeferredTerminalProjection(turn: ProjectionTurn): boolean {
  // Active snapshots are complete. Only the transition out of a readonly
  // terminal view needs recovery, so live deltas never scan historical rounds.
  if (turn.status !== 'completed') return false;
  const rounds = turn.projection?.toolRounds;
  return Array.isArray(rounds) && rounds.some(round =>
    round && typeof round === 'object' && '_snapshotToolResult' in round);
}

function reduceEvent(
  next: TurnState,
  event: TurnEvent | null | undefined,
  onUnknownTurn?: (turnId: string) => void,
  onProjectionGap?: (turnId: string) => void,
): void {
  if (!event?.turnId || !event.attemptId) return;
  const turn = next.turnsById[event.turnId];
  if (!turn) {
    const queue = next.pendingEventsByTurn[event.turnId]
      ?? (next.pendingEventsByTurn[event.turnId] = []);
    if (!queue.some((item) => item.attemptId === event.attemptId
        && Number(item.seq) === Number(event.seq))) {
      const pending = Object.values(next.pendingEventsByTurn).flatMap(
        (events) => events ?? [],
      );
      if (pending.length < MAX_PENDING_EVENTS
          && JSON.stringify([...pending, event]).length <= MAX_PENDING_EVENT_BYTES / 2) {
        queue.push(event);
      } else {
        // Drop reconstructible transport data, not durable Turn state. The
        // existing recovery callback below supplies the missing authority.
        next.pendingEventsByTurn = Object.create(null) as Record<string, TurnEvent[]>;
      }
    }
    onUnknownTurn?.(event.turnId);
    return;
  }
  // Wire-envelope parity: the canonical frame nests the body under `payload`
  // (legacy turn_lifecycle._append_event + the sidecar after the 2026-08-18
  // parity fix).  Attempt logs written by the sidecar BEFORE that fix stored
  // the body SPREAD at the envelope's top level; treating the envelope itself
  // as the payload lets those replays still apply their projection/status/
  // settlement instead of being silently dropped.
  const payload = event.payload;
  const turnState = payload.turnState;
  const adoptsAttempt = Number(event.seq || 0) === 1
    && turnState?.turnId === event.turnId
    && turnState.currentAttemptId === event.attemptId;
  if (turn.currentAttemptId !== event.attemptId && !adoptsAttempt) return;
  const previousAttempt = next.attemptsById[event.attemptId]
    ?? { attemptId: event.attemptId };
  if (previousAttempt.turnId && previousAttempt.turnId !== event.turnId) return;
  if (previousAttempt.status === 'superseded') return;
  if (Number(event.seq || 0) <= Number(previousAttempt.lastSeq || 0)) return;
  const incomingRevision = Number(event.projectionRevision || 0);
  if (incomingRevision <= Number(turn.projectionRevision || 0)) {
    next.attemptsById[event.attemptId] = {
      ...previousAttempt,
      lastSeq: Number(event.seq || 0),
    };
    return;
  }
  const updated: ProjectionTurn = {
    ...turn,
    projectionRevision: incomingRevision,
  };
  if (payload.projection) {
    // Compatibility/full-recovery frames win when both forms are present.
    updated.projection = payload.projection;
  } else if (payload.projectionPatch) {
    const patch = payload.projectionPatch;
    const baseRevision = Number(patch.baseRevision || 0);
    const targetRevision = Number(patch.targetRevision || 0);
    if (baseRevision !== Number(turn.projectionRevision || 0)
        || targetRevision !== incomingRevision) {
      onProjectionGap?.(event.turnId);
      return;
    }
    if (hasDeferredTerminalProjection(turn)) {
      onProjectionGap?.(event.turnId);
      return;
    }
    const patched = applyProjectionPatch(turn.projection || {}, patch);
    if (!patched) {
      onProjectionGap?.(event.turnId);
      return;
    }
    updated.projection = patched;
  } else {
    // Every retained v3 attempt change advances a projection revision through
    // an explicit (possibly empty) patch. Advancing metadata without that
    // proof would hide a producer regression and make later patches diverge.
    onProjectionGap?.(event.turnId);
    return;
  }
  if (turnState?.turnId === event.turnId) {
    updated.status = turnStatus(turnState.status, turn.status);
    if (typeof turnState.actor === 'string') updated.actor = turnState.actor;
    if (typeof turnState.kind === 'string') updated.kind = turnState.kind;
    updated.currentAttemptId = turnState.currentAttemptId;
    updated.settlement = turnState.settlement;
    updated.updatedAt = turnState.updatedAt;
  }
  if (event.type === 'status_changed' && payload.status) {
    updated.status = turnStatus(payload.status, updated.status);
  } else if (event.type === 'terminal_settlement') {
    updated.status = turnStatus(payload.status, updated.status);
    updated.settlement = payload.settlement ?? {};
  } else if (event.type === 'projection_updated'
      || event.type === 'interaction_request') {
    updated.status = 'running';
  }
  // Related authorities are part of the accepted event, not a bypass around
  // duplicate, stale-attempt or patch-base validation.
  for (const relatedTurn of payload.turns ?? []) putTurn(next, relatedTurn);
  for (const relatedAttempt of payload.attempts ?? []) putAttempt(next, relatedAttempt);
  // The receipt and queue acknowledgement are one server commit. Apply
  // removals only after the same attempt/revision/patch guards as the block;
  // never infer delivery from visible DOM, command text, or stale events.
  if (payload.removedQueueIds?.length) {
    const removed = new Set(payload.removedQueueIds);
    // Storage renumbers the remaining owner-scoped queue in this commit.
    // Derive the same contiguous ranks so live cards agree with a cold snapshot.
    next.queueItems = next.queueItems
      .filter((item) => !removed.has(item.queueId))
      .sort((left, right) => left.priority - right.priority
        || left.position - right.position
        || (left.queueId < right.queueId ? -1 : left.queueId > right.queueId ? 1 : 0))
      .map((item, index) => ({ ...item, position: index + 1 }));
  }
  next.turnsById[event.turnId] = updated;
  next.attemptsById[event.attemptId] = {
    ...previousAttempt,
    attemptId: event.attemptId,
    turnId: event.turnId,
    lastSeq: Number(event.seq || 0),
    status: updated.status,
    interactionRequest: event.type === 'interaction_request'
      ? payload.request
      : previousAttempt.interactionRequest,
  };
  // Phase frames from the CURRENT attempt only (all guards above passed).
  // `null` is meaningful — it folds the stage text exactly when v1 would
  // (first content delta / terminal frame).
  if ('phase' in payload) {
    next.livePhase = (payload as UnknownRecord).phase ?? null;
  }
  if (event.type === 'terminal_settlement' || adoptsAttempt) {
    next.livePhase = event.type === 'terminal_settlement' ? null
      : (payload as UnknownRecord).phase ?? null;
  }
  delete next.commandPending[event.turnId];
}

function removeTurns(
  next: TurnState,
  turnIds: ReadonlyArray<string> | undefined,
): void {
  for (const turnId of turnIds ?? []) {
    const removed = next.turnsById[turnId];
    delete next.turnsById[turnId];
    delete next.pendingEventsByTurn[turnId];
    delete next.commandPending[turnId];
    delete next.liveRoundUsageByTurn[turnId];
    for (const [attemptId, attempt] of Object.entries(next.attemptsById)) {
      if (attempt?.turnId === turnId) delete next.attemptsById[attemptId];
    }
    for (const lane of Object.keys(next.laneOrder)) {
      const ids = next.laneOrder[lane];
      if (!ids) continue;
      const index = ids.indexOf(turnId);
      if (index >= 0) ids.splice(index, 1);
      if (index >= 0 && removed && next.historyByLane[lane]) {
        const history = next.historyByLane[lane];
        history.totalTurns = Math.max(ids.length, history.totalTurns - 1);
      }
      if (!ids.length) delete next.laneOrder[lane];
    }
  }
}

function applyTurnProjectionChanges(
  next: TurnState,
  values: ReadonlyArray<TurnProjectionChange> | undefined,
  onUnknownTurn?: (turnId: string) => void,
  onProjectionGap?: (turnId: string) => void,
): void {
  for (const change of values ?? []) {
    const turnId = change.turnId;
    const turn = next.turnsById[turnId];
    if (!turn) {
      onUnknownTurn?.(turnId);
      continue;
    }
    const baseRevision = Number(change.baseProjectionRevision);
    const targetRevision = Number(change.targetProjectionRevision);
    const currentRevision = Number(turn.projectionRevision || 0);
    if (!Number.isInteger(baseRevision) || !Number.isInteger(targetRevision)
        || targetRevision <= baseRevision) {
      onProjectionGap?.(turnId);
      continue;
    }
    // The initiating tab may already have adopted the full command response
    // before its durable replay event arrives. Replaying that older/equal
    // patch is an idempotent no-op; only a forward gap triggers recovery.
    if (targetRevision <= currentRevision) continue;
    const patch = change.projectionPatch;
    const patchRecord = patch && typeof patch === 'object'
      ? patch as UnknownRecord : {};
    if (baseRevision !== currentRevision
        || Number(patchRecord.baseRevision) !== baseRevision
        || Number(patchRecord.targetRevision) !== targetRevision) {
      onProjectionGap?.(turnId);
      continue;
    }
    if (hasDeferredTerminalProjection(turn)) {
      onProjectionGap?.(turnId);
      continue;
    }
    const projection = applyProjectionPatch(turn.projection || {}, patch);
    if (!projection) {
      onProjectionGap?.(turnId);
      continue;
    }
    next.turnsById[turnId] = {
      ...turn,
      projection,
      projectionRevision: targetRevision,
      updatedAt: Number(change.updatedAt || turn.updatedAt || 0),
    };
  }
}

/** Immutable reducer for every command, snapshot and event ingress. */
export function reduceTurnState(
  state: TurnState,
  action: TurnAction | null | undefined,
  options: ReduceTurnStateOptions = {},
): TurnState {
  const next = copyState(state);
  if (!action?.type) return next;
  if (action.type === 'snapshot') {
    const snapshot = action.snapshot ?? {};
    if (Number(snapshot.conversationRevision || 0)
        < Number(next.conversationRevision || 0)) return next;
    next.conversationRevision = Number(snapshot.conversationRevision || 0);
    if (snapshot.authoritativeFull) {

      next.livePhase = null;
      next.liveRoundUsageByTurn = Object.create(null) as Record<string, LiveRoundUsage>;
      next.turnsById = Object.create(null) as Record<string, ProjectionTurn>;
      next.laneOrder = Object.create(null) as Record<string, string[]>;
      next.historyByLane = Object.create(null) as Record<
        string, TurnHistoryLaneState
      >;
      next.attemptsById = Object.create(null) as Record<string, AttemptRecord>;
      next.pendingEventsByTurn = Object.create(null) as Record<string, TurnEvent[]>;
    }
    /* Delta-sync tombstones: a non-authoritative (incremental) snapshot
     * cannot sweep absent ids, so deletions arrive explicitly.  Removal is
     * idempotent and also drops any queued pending events for the turn. */
    removeTurns(next, snapshot.deletedTurnIds);
    applyTurnProjectionChanges(
      next,
      snapshot.turnPatches,
      options.onUnknownTurn,
      options.onProjectionGap,
    );
    for (const turn of snapshot.turns ?? []) putTurn(next, turn);
    for (const attempt of snapshot.attempts ?? []) putAttempt(next, attempt);
    if (snapshot.authoritativeFull && snapshot.activeAttemptPhase) {
      const active = snapshot.activeAttemptPhase;
      const turn = next.turnsById[active.turnId];
      const attempt = next.attemptsById[active.attemptId];
      if (turn?.laneId === 'main' && turn.status === 'running'
          && turn.currentAttemptId === active.attemptId
          && attempt?.turnId === turn.turnId && attempt.status === 'running'
          && active.projectionRevision <= Number(turn.projectionRevision || 0)) {
        next.livePhase = active.phase;
      }
    }
    const pageWindow = snapshot.turnWindow;
    if (pageWindow) {
      next.historyByLane[pageWindow.laneId] = {
        nextBeforeOrdinal: pageWindow.nextBeforeOrdinal,
        hasMore: pageWindow.hasMore,
        totalTurns: pageWindow.totalTurns,
      };
    } else if (snapshot.laneId && 'hasMore' in snapshot
        && 'totalTurns' in snapshot) {
      next.historyByLane[snapshot.laneId] = {
        nextBeforeOrdinal: snapshot.nextBeforeOrdinal ?? null,
        hasMore: Boolean(snapshot.hasMore),
        totalTurns: Math.max(
          Number(snapshot.totalTurns || 0),
          next.laneOrder[snapshot.laneId]?.length ?? 0,
        ),
      };
    } else if (snapshot.authoritativeFull) {
      for (const [laneId, ids] of Object.entries(next.laneOrder)) {
        next.historyByLane[laneId] = {
          nextBeforeOrdinal: null,
          hasMore: false,
          totalTurns: ids?.length ?? 0,
        };
      }
    }
    if (snapshot.queueItems) {
      next.queueItems = snapshot.queueItems
        .filter((item) => Boolean(item.queueId))
        .map((item) => ({ ...item }));
    }
    for (const item of snapshot.queueItemUpserts ?? []) {
      if (!item.queueId) continue;
      next.queueItems = [
        ...next.queueItems.filter((current) => current.queueId !== item.queueId),
        { ...item },
      ];
    }
    if (snapshot.removedQueueIds?.length) {
      const removedQueueIds = new Set(snapshot.removedQueueIds);
      next.queueItems = next.queueItems.filter(
        (item) => !removedQueueIds.has(item.queueId),
      );
    }
    /* Authoritative snapshots ship the key explicitly (true AND false —
     * false is what clears a recovered wedge); delta snapshots omit it. */
    if ('pushWithheld' in snapshot) {
      next.pushWithheld = Boolean(snapshot.pushWithheld);
    }
    for (const [turnId, events] of Object.entries(next.pendingEventsByTurn)) {
      if (!next.turnsById[turnId] || !events) continue;
      delete next.pendingEventsByTurn[turnId];
      events.sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0));
      for (const event of events) {
        reduceEvent(
          next, event, options.onUnknownTurn, options.onProjectionGap,
        );
      }
    }
  } else if (action.type === 'command_response') {
    const response = action.response ?? {};
    // An ACK is one conversation commit. Fence it before ingesting ANY
    // rows: a deleted suffix has no per-turn revision left to reject an old
    // response. Ordered Sync supplies the causal proof, including deletions.
    // Legacy/local responses without a revision retain their existing path.
    const responseRevision = Number(
      response.conversationRevision ?? next.conversationRevision,
    );
    if (responseRevision < Number(next.conversationRevision || 0)) return next;
    putTurn(next, response.submittedTurn);
    putTurn(next, response.turn);
    for (const turn of response.turns ?? []) putTurn(next, turn);
    putAttempt(next, response.attempt);
    for (const attempt of response.attempts ?? []) putAttempt(next, attempt);
    const queueItem = response.queueItem;
    if (queueItem?.queueId) {
      next.queueItems = [
        ...next.queueItems.filter((item) => item.queueId !== queueItem.queueId),
        { ...queueItem },
      ].sort((left, right) => Number(left.position || 0) - Number(right.position || 0));
    }
    if (response.cancelled && typeof response.queueId === 'string') {
      next.queueItems = next.queueItems.filter(
        (item) => item.queueId !== response.queueId,
      );
    }
    removeTurns(next, response.deletedTurnIds);
    next.conversationRevision = Math.max(
      next.conversationRevision,
      Number(response.conversationRevision || 0),
    );
    const responseTurn = response.turn;
    if (responseTurn?.turnId) delete next.commandPending[responseTurn.turnId];
  } else if (action.type === 'event') {
    reduceEvent(
      next, action.event, options.onUnknownTurn, options.onProjectionGap,
    );
  } else if (action.type === 'command_pending' && action.turnId && action.operation) {
    next.commandPending[action.turnId] = action.operation;
  } else if (action.type === 'command_failed' && action.turnId) {
    delete next.commandPending[action.turnId];
  } else if (action.type === 'push_withheld') {
    next.pushWithheld = Boolean(action.pushWithheld);
  } else if (action.type === 'transport' && action.status) {
    next.transport = action.status;
  }
  return next;
}

/** Own subscriptions and one snapshot-recovery request per unknown turn. */
export function createTurnStore(
  conversationId: string,
  options: TurnStoreOptions = {},
): TurnStore {
  let state = createTurnState(conversationId);
  const listeners = new Set<(state: TurnState, update: TurnStateUpdate) => void>();
  let resyncing = false;
  let store: TurnStore;

  const requestResync = (turnId: string): void => {
    if (resyncing || !options.fetchSnapshot) return;
    resyncing = true;
    Promise.resolve()
      .then(() => options.fetchSnapshot?.())
      .then((snapshot) => {
        if (snapshot) store.dispatch({ type: 'snapshot', snapshot });
      })
      .catch((error: unknown) => options.onResyncError?.(error, turnId))
      .finally(() => { resyncing = false; });
  };

  const dispatch = (action: TurnAction): TurnState => {
    const previousState = state;
    state = reduceTurnState(state, action, {
      onUnknownTurn: requestResync,
      onProjectionGap: requestResync,
    });
    const appliedState = state;
    const update = { previousState, action };
    for (const listener of listeners) listener(appliedState, update);
    return state;
  };

  store = {
    getState: () => state,
    dispatch,
    _fetchSnapshot: options.fetchSnapshot,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  return store;
}
