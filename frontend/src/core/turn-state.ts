import type { ProjectionTurn } from './turn-projection';

type UnknownRecord = Record<string, unknown>;

export interface AttemptRecord extends UnknownRecord {
  attemptId: string;
  turnId?: string;
  status?: string;
  lastSeq?: number;
  interactionRequest?: unknown;
}

export interface TurnEvent extends UnknownRecord {
  type: string;
  turnId?: string;
  attemptId?: string;
  seq?: number;
  projectionRevision?: number;
  payload?: {
    projection?: UnknownRecord;
    status?: string;
    settlement?: UnknownRecord;
    request?: unknown;
    turns?: ProjectionTurn[];
    attempts?: AttemptRecord[];
    [key: string]: unknown;
  };
}

export interface TurnState {
  conversationId: string;
  conversationRevision: number;
  turnsById: Record<string, ProjectionTurn | undefined>;
  laneOrder: Record<string, string[] | undefined>;
  attemptsById: Record<string, AttemptRecord | undefined>;
  pendingEventsByTurn: Record<string, TurnEvent[] | undefined>;
  commandPending: Record<string, string | null | undefined>;
  transport: string;
}

export interface TurnAction extends UnknownRecord {
  type: string;
  snapshot?: UnknownRecord;
  response?: UnknownRecord;
  event?: TurnEvent;
  turnId?: string;
  operation?: string;
  status?: string;
}

export interface ReduceTurnStateOptions {
  onUnknownTurn?(turnId: string): void;
}

export interface TurnStoreOptions {
  fetchSnapshot?(): Promise<UnknownRecord | null | undefined>;
  onResyncError?(error: unknown, turnId: string): void;
}

export interface TurnStore {
  getState(): TurnState;
  dispatch(action: TurnAction): TurnState;
  subscribe(listener: (state: TurnState) => void): () => void;
  _fetchSnapshot?: TurnStoreOptions['fetchSnapshot'];
  _snapshotLoaded?: boolean;
}

export function createTurnState(conversationId: string): TurnState {
  return {
    conversationId,
    conversationRevision: 0,
    turnsById: Object.create(null) as Record<string, ProjectionTurn>,
    laneOrder: Object.create(null) as Record<string, string[]>,
    attemptsById: Object.create(null) as Record<string, AttemptRecord>,
    pendingEventsByTurn: Object.create(null) as Record<string, TurnEvent[]>,
    commandPending: Object.create(null) as Record<string, string>,
    transport: 'idle',
  };
}

function copyState(state: TurnState): TurnState {
  return {
    ...state,
    turnsById: { ...state.turnsById },
    laneOrder: Object.fromEntries(Object.entries(state.laneOrder)
      .map(([lane, ids]) => [lane, (ids ?? []).slice()])),
    attemptsById: { ...state.attemptsById },
    pendingEventsByTurn: Object.fromEntries(
      Object.entries(state.pendingEventsByTurn)
        .map(([turnId, events]) => [turnId, (events ?? []).slice()]),
    ),
    commandPending: { ...state.commandPending },
  };
}

function putTurn(next: TurnState, turn: ProjectionTurn | null | undefined): void {
  if (!turn?.turnId) return;
  const previous = next.turnsById[turn.turnId];
  if (previous && Number(turn.projectionRevision || 0)
      < Number(previous.projectionRevision || 0)) return;
  next.turnsById[turn.turnId] = { ...previous, ...turn };
  const lane = turn.laneId || 'main';
  const ids = next.laneOrder[lane] ?? (next.laneOrder[lane] = []);
  if (!ids.includes(turn.turnId)) ids.push(turn.turnId);
  ids.sort((leftId, rightId) => (
    Number(next.turnsById[leftId]?.ordinal || 0)
      - Number(next.turnsById[rightId]?.ordinal || 0)
  ));
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

function reduceEvent(
  next: TurnState,
  event: TurnEvent | null | undefined,
  onUnknownTurn?: (turnId: string) => void,
): void {
  if (!event?.turnId || !event.attemptId) return;
  const turn = next.turnsById[event.turnId];
  if (!turn) {
    const queue = next.pendingEventsByTurn[event.turnId]
      ?? (next.pendingEventsByTurn[event.turnId] = []);
    if (!queue.some((item) => item.attemptId === event.attemptId
        && Number(item.seq) === Number(event.seq))) queue.push(event);
    onUnknownTurn?.(event.turnId);
    return;
  }
  if (turn.currentAttemptId !== event.attemptId) return;
  const previousAttempt = next.attemptsById[event.attemptId]
    ?? { attemptId: event.attemptId };
  if (Number(event.seq || 0) <= Number(previousAttempt.lastSeq || 0)) return;
  const incomingRevision = Number(event.projectionRevision || 0);
  if (incomingRevision <= Number(turn.projectionRevision || 0)) {
    next.attemptsById[event.attemptId] = {
      ...previousAttempt,
      lastSeq: Number(event.seq || 0),
    };
    return;
  }

  const payload = event.payload ?? {};
  for (const relatedTurn of payload.turns ?? []) putTurn(next, relatedTurn);
  for (const relatedAttempt of payload.attempts ?? []) putAttempt(next, relatedAttempt);
  const updated: ProjectionTurn = {
    ...turn,
    projectionRevision: incomingRevision,
  };
  if (payload.projection) updated.projection = payload.projection;
  if (event.type === 'status_changed' && payload.status) {
    updated.status = payload.status;
  } else if (event.type === 'terminal_settlement') {
    updated.status = payload.status;
    updated.settlement = payload.settlement ?? {};
  } else if (event.type === 'projection_updated'
      || event.type === 'interaction_request') {
    updated.status = 'running';
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
  delete next.commandPending[event.turnId];
}

function records(value: unknown): UnknownRecord[] {
  return Array.isArray(value)
    ? value.filter((item): item is UnknownRecord => Boolean(item && typeof item === 'object'))
    : [];
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
      next.turnsById = Object.create(null) as Record<string, ProjectionTurn>;
      next.laneOrder = Object.create(null) as Record<string, string[]>;
    }
    for (const turn of records(snapshot.turns)) putTurn(next, turn as ProjectionTurn);
    for (const [turnId, events] of Object.entries(next.pendingEventsByTurn)) {
      if (!next.turnsById[turnId] || !events) continue;
      delete next.pendingEventsByTurn[turnId];
      events.sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0));
      for (const event of events) reduceEvent(next, event, options.onUnknownTurn);
    }
  } else if (action.type === 'command_response') {
    const response = action.response ?? {};
    putTurn(next, response.submittedTurn as ProjectionTurn | undefined);
    putTurn(next, response.turn as ProjectionTurn | undefined);
    putAttempt(next, response.attempt as AttemptRecord | undefined);
    next.conversationRevision = Math.max(
      next.conversationRevision,
      Number(response.conversationRevision || 0),
    );
    const responseTurn = response.turn as ProjectionTurn | undefined;
    if (responseTurn?.turnId) delete next.commandPending[responseTurn.turnId];
  } else if (action.type === 'event') {
    reduceEvent(next, action.event, options.onUnknownTurn);
  } else if (action.type === 'command_pending' && action.turnId && action.operation) {
    next.commandPending[action.turnId] = action.operation;
  } else if (action.type === 'command_failed' && action.turnId) {
    delete next.commandPending[action.turnId];
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
  const listeners = new Set<(state: TurnState) => void>();
  const resyncing = new Set<string>();
  let store: TurnStore;

  const dispatch = (action: TurnAction): TurnState => {
    state = reduceTurnState(state, action, {
      onUnknownTurn(turnId) {
        if (resyncing.has(turnId) || !options.fetchSnapshot) return;
        resyncing.add(turnId);
        Promise.resolve()
          .then(() => options.fetchSnapshot?.())
          .then((snapshot) => {
            if (snapshot) store.dispatch({ type: 'snapshot', snapshot });
          })
          .catch((error: unknown) => options.onResyncError?.(error, turnId))
          .finally(() => resyncing.delete(turnId));
      },
    });
    for (const listener of listeners) listener(state);
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
