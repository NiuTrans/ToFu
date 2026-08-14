import {
  createAttemptEventStream,
  type AttemptContinuation,
  type AttemptStreamConnection,
} from './attempt-stream';
import {
  buildTurnOperationRequest,
  buildTurnSubmitRequest,
  createTurnCommandId,
} from './turn-command';
import {
  applyTurnStateProjection,
  type LegacyTurnConversation,
  type ProjectionTurn,
} from './turn-projection';
import { presentTurnFinish, resumeTurnOptions } from './turn-presentation';
import { renderTurnStateInto, type TurnRenderer } from './turn-render';
import {
  createTurnState,
  createTurnStore,
  reduceTurnState,
  type TurnEvent,
  type TurnState,
  type TurnStore,
  type TurnStoreOptions,
} from './turn-state';

type UnknownRecord = Record<string, unknown>;
type RuntimeStore = TurnStore & { _snapshotLoaded?: boolean };

export interface RuntimeConversation extends LegacyTurnConversation {
  id: string;
  title?: string;
  createdAt?: number;
  _localOnly?: boolean;
  _activeAttemptId?: string | null;
  _activeBranchAttemptIds?: Set<string>;
}

export interface TurnsV2Transport {
  list(conversationId: string): Promise<unknown>;
  submit(
    conversationId: string,
    payload: UnknownRecord,
    requestOptions?: unknown,
  ): Promise<unknown>;
  attempt(
    conversationId: string,
    turnId: string,
    payload: UnknownRecord,
  ): Promise<unknown>;
  streamUrl(attemptId: string, after?: number): string;
  update(
    conversationId: string,
    turnId: string,
    payload: UnknownRecord,
  ): Promise<unknown>;
  createLane(
    conversationId: string,
    parentTurnId: string,
    payload: UnknownRecord,
  ): Promise<unknown>;
  deleteLane(
    conversationId: string,
    parentTurnId: string,
    laneId: string,
  ): Promise<unknown>;
  deleteTurns(conversationId: string, turnIds: readonly string[]): Promise<unknown>;
  abort(attemptId: string): Promise<unknown>;
}

export interface TurnRuntimeOptions {
  api: TurnsV2Transport;
  findConversation?(conversationId: string): RuntimeConversation | null;
  persist?(conversation: RuntimeConversation): void;
  isActive?(conversation: RuntimeConversation): boolean;
  replaceAll?(
    conversation: RuntimeConversation,
    repaint?: { force?: boolean },
  ): void;
  deferTerminalRelease?(release: () => void): void;
  buildNavigation?(conversation: RuntimeConversation): void;
  renderConversationList?(): void;
  updateSendButton?(): void;
  onProtocolError?(error: Error): void;
  onResyncError?(error: unknown, turnId: string): void;
  eventSourceFactory?: (url: string) => EventSource;
}

export interface TurnRuntime {
  emptyState: typeof createTurnState;
  reducer: typeof reduceTurnState;
  createStore: typeof createTurnStore;
  submit(
    store: TurnStore,
    inputTurn: unknown,
    config: unknown,
    extra?: UnknownRecord,
    requestOptions?: unknown,
  ): Promise<UnknownRecord>;
  runOperation(
    store: TurnStore,
    turnId: string,
    operation: string,
    config?: unknown,
    options?: UnknownRecord,
  ): Promise<UnknownRecord>;
  connect(
    store: TurnStore,
    attemptId: string,
    after?: number,
    hooks?: TurnConnectionHooks,
  ): AttemptStreamConnection;
  renderInto(
    container: Element,
    state: TurnState,
    renderTurn?: TurnRenderer,
  ): void;
  finishPresentation: typeof presentTurnFinish;
  resumeOptions: typeof resumeTurnOptions;
  hydrateConversation(conversation: RuntimeConversation): Promise<RuntimeStore | null>;
  submitConversation(
    conversation: RuntimeConversation,
    message: unknown,
    config: unknown,
    extra?: UnknownRecord,
  ): Promise<UnknownRecord>;
  submitBranch(
    conversation: RuntimeConversation,
    branch: UnknownRecord,
    parentTurnId: string,
    message: unknown,
    config: unknown,
    extra?: UnknownRecord,
  ): Promise<UnknownRecord>;
  operateConversation(
    conversation: RuntimeConversation,
    turnId: string,
    operation: string,
    config?: unknown,
    options?: UnknownRecord,
  ): Promise<UnknownRecord>;
  updateConversationTurn(
    conversation: RuntimeConversation,
    turnId: string,
    projection: unknown,
  ): Promise<UnknownRecord>;
  createBranchLane(
    conversation: RuntimeConversation,
    parentTurnId: string,
    descriptor?: UnknownRecord,
  ): Promise<UnknownRecord>;
  deleteBranchLane(
    conversation: RuntimeConversation,
    parentTurnId: string,
    laneId: string,
  ): Promise<UnknownRecord>;
  deleteConversationTurns(
    conversation: RuntimeConversation,
    turnIds: readonly string[],
  ): Promise<UnknownRecord>;
  markCommandPending(
    conversation: RuntimeConversation,
    turnId: string,
    operation: string,
  ): void;
  markCommandFailed(conversation: RuntimeConversation, turnId: string): void;
  abortConversation(conversation: RuntimeConversation): Promise<unknown>;
  abortAttempt(attemptId: string): Promise<unknown>;
  ensureRuntimeStore(conversationId: string): RuntimeStore;
  findConversation(conversationId: string): RuntimeConversation | null;
  readonly TERMINAL: ReadonlySet<string>;
  isCutoverActive(): boolean;
}

interface TurnConnectionHooks {
  onTerminal?(event: TurnEvent): void;
  onContinuation?(continuation: AttemptContinuation): void;
}

function record(value: unknown): UnknownRecord {
  return value && typeof value === 'object' ? value as UnknownRecord : {};
}

function records(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function latestTurnFrom(error: unknown): UnknownRecord | null {
  const failure = record(error);
  const body = record(failure.body);
  const response = record(failure.response);
  const latest = body.latestTurn || response.latestTurn;
  return latest && typeof latest === 'object' ? latest as UnknownRecord : null;
}

/** Create the complete Turn/Attempt runtime without reading ambient globals. */
export function createConversationTurnRuntime(
  options: TurnRuntimeOptions,
): TurnRuntime {
  const terminal = new Set(['completed', 'interrupted', 'truncated', 'failed']);
  const runtimeStores = new Map<string, RuntimeStore>();
  const runtimeConnections = new Map<string, AttemptStreamConnection>();
  const boundConversations = new WeakMap<RuntimeConversation, {
    store: RuntimeStore;
    unsubscribe: () => void;
  }>();
  const scheduledTerminalReleases = new WeakMap<RuntimeConversation, string>();
  let runtimeCutoverActive = false;

  const projectConversation = (
    conversation: RuntimeConversation,
    state: TurnState,
  ): void => {
    let terminalSettled = false;
    const finishingAttemptId = conversation._finishingAttemptId;
    if (conversation._finishingStream && finishingAttemptId) {
      const turnStatus = Object.values(state.turnsById).find((turn) =>
        turn?.currentAttemptId === finishingAttemptId)?.status;
      terminalSettled = terminal.has(turnStatus || '');
    }
    const active = options.isActive?.(conversation) ?? false;
    let terminalRepainted = false;
    const scheduleTerminalRelease = (): void => {
      if (!terminalSettled || !finishingAttemptId) return;
      if (scheduledTerminalReleases.get(conversation) === finishingAttemptId) return;
      scheduledTerminalReleases.set(conversation, finishingAttemptId);
      const release = (): void => {
        if (scheduledTerminalReleases.get(conversation) !== finishingAttemptId) return;
        scheduledTerminalReleases.delete(conversation);
        if (!conversation._finishingStream
            || conversation._finishingAttemptId !== finishingAttemptId) return;
        // Reconcile once more immediately before exposing Send. Async cost,
        // transport, and snapshot work may have repainted between settlement
        // and the next browser frame.
        if (active) options.replaceAll?.(conversation, { force: true });
        conversation._finishingStream = false;
        conversation._finishingAttemptId = null;
        options.updateSendButton?.();
      };
      if (options.deferTerminalRelease) options.deferTerminalRelease(release);
      else release();
    };
    applyTurnStateProjection({
      conversation,
      state,
      active,
      persist: options.persist,
      replaceAll: () => {
        options.replaceAll?.(conversation, { force: terminalSettled });
        if (terminalSettled) {
          terminalRepainted = true;
          scheduleTerminalRelease();
        }
      },
      buildNavigation: options.buildNavigation,
      renderConversationList: options.renderConversationList,
      updateSendButton: options.updateSendButton,
    });
    // A duplicate terminal snapshot can have the same projection fingerprint.
    // The DOM may still own a transient streaming bubble, so settling must force
    // one repaint even when the state projection itself is unchanged. Keep the
    // Stop latch set until that repaint returns; button polling must never
    // observe Send while the terminal answer is still absent from the DOM.
    if (terminalSettled && !terminalRepainted) {
      if (active) options.replaceAll?.(conversation, { force: true });
      scheduleTerminalRelease();
    }
  };

  const ensureRuntimeStore = (conversationId: string): RuntimeStore => {
    const existing = runtimeStores.get(conversationId);
    if (existing) return existing;
    const store = createTurnStore(conversationId, {
      fetchSnapshot: async () => record(await options.api.list(conversationId)),
      onResyncError: options.onResyncError,
    }) as RuntimeStore;
    store._snapshotLoaded = false;
    runtimeStores.set(conversationId, store);
    return store;
  };

  const bindConversation = (
    conversation: RuntimeConversation,
    store: RuntimeStore,
  ): void => {
    const previous = boundConversations.get(conversation);
    if (previous?.store === store) return;
    previous?.unsubscribe();
    const unsubscribe = store.subscribe(
      (state) => projectConversation(conversation, state),
    );
    boundConversations.set(conversation, { store, unsubscribe });
    projectConversation(conversation, store.getState());
  };

  const connect = (
    store: TurnStore,
    attemptId: string,
    after = 0,
    hooks: TurnConnectionHooks = {},
  ): AttemptStreamConnection => createAttemptEventStream({
    attemptId,
    url: options.api.streamUrl(attemptId, after),
    after,
    onTransport(status) {
      store.dispatch({ type: 'transport', status });
    },
    onEvent(event) {
      store.dispatch({ type: 'event', event: event as TurnEvent });
    },
    fetchSnapshot: store._fetchSnapshot,
    onSnapshot(snapshot) {
      store.dispatch({ type: 'snapshot', snapshot: record(snapshot) });
    },
    onTerminal(event) {
      hooks.onTerminal?.(event as TurnEvent);
    },
    onContinuation(continuation) {
      if (hooks.onContinuation) hooks.onContinuation(continuation);
      else connect(store, continuation.attemptId, 0, hooks);
    },
    onProtocolError: options.onProtocolError,
    eventSourceFactory: options.eventSourceFactory,
  });

  const connectAttempt = (
    conversation: RuntimeConversation,
    store: RuntimeStore,
    attemptId: string,
    after = 0,
  ): AttemptStreamConnection | null => {
    if (!attemptId) return null;
    const previous = runtimeConnections.get(attemptId);
    if (previous) return previous;
    const connection = connect(store, attemptId, after, {
      onTerminal() {
        runtimeConnections.delete(attemptId);
        projectConversation(conversation, store.getState());
      },
      onContinuation(continuation) {
        runtimeConnections.delete(attemptId);
        connectAttempt(conversation, store, continuation.attemptId, 0);
      },
    });
    runtimeConnections.set(attemptId, connection);
    return connection;
  };

  const submit = async (
    store: TurnStore,
    inputTurn: unknown,
    config: unknown,
    extra: UnknownRecord = {},
    requestOptions: unknown = {},
  ): Promise<UnknownRecord> => {
    const payload = buildTurnSubmitRequest(inputTurn, config, extra);
    const response = record(await options.api.submit(
      store.getState().conversationId,
      payload,
      requestOptions,
    ));
    store.dispatch({ type: 'command_response', response });
    return response;
  };

  const runOperation = async (
    store: TurnStore,
    turnId: string,
    operation: string,
    config: unknown = {},
    operationOptions: UnknownRecord = {},
  ): Promise<UnknownRecord> => {
    const turn = store.getState().turnsById[turnId];
    if (!turn) throw new Error('Unknown turn; refresh the authoritative snapshot.');
    store.dispatch({ type: 'command_pending', turnId, operation });
    try {
      const response = record(await options.api.attempt(
        stringValue(turn.conversationId),
        turnId,
        buildTurnOperationRequest(turn, operation, config, operationOptions),
      ));
      store.dispatch({ type: 'command_response', response });
      return response;
    } catch (error) {
      store.dispatch({ type: 'command_failed', turnId });
      const latest = latestTurnFrom(error);
      if (latest) {
        store.dispatch({ type: 'snapshot', snapshot: {
          conversationRevision: store.getState().conversationRevision,
          turns: [latest],
        } });
      }
      throw error;
    }
  };

  const hydrateConversation = async (
    conversation: RuntimeConversation,
  ): Promise<RuntimeStore | null> => {
    if (!conversation?.id) return null;
    const store = ensureRuntimeStore(conversation.id);
    const snapshot = record(await options.api.list(conversation.id));
    if (snapshot.cutoverActive) runtimeCutoverActive = true;
    const turns = records(snapshot.turns);
    if (!snapshot.cutoverActive && !turns.length) return null;
    store._snapshotLoaded = true;
    store.dispatch({ type: 'snapshot', snapshot });
    bindConversation(conversation, store);
    for (const turn of turns) {
      const status = stringValue(turn.status);
      const attemptId = stringValue(turn.currentAttemptId);
      if ((status === 'pending' || status === 'running') && attemptId) {
        connectAttempt(conversation, store, attemptId, 0);
      }
    }
    return store;
  };

  const submitConversation = async (
    conversation: RuntimeConversation,
    message: unknown,
    config: unknown,
    extra: UnknownRecord = {},
  ): Promise<UnknownRecord> => {
    if (!conversation?.id) throw new Error('Conversation is required.');
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded) {
      if (!conversation._localOnly) {
        try {
          store.dispatch({
            type: 'snapshot',
            snapshot: record(await options.api.list(conversation.id)),
          });
        } catch (error) {
          if (Number(record(error).status || 0) !== 404) throw error;
        }
      }
      store._snapshotLoaded = true;
    }
    try {
      const requestOptions = extra.requestOptions;
      const response = await submit(store, null, config, {
        ...extra,
        requestOptions: undefined,
        commandId: extra.commandId || createTurnCommandId(),
        message,
        conversation: {
          allowCreate: true,
          title: conversation.title || 'New Chat',
          createdAt: conversation.createdAt || Date.now(),
          settings: extra.settings || {},
        },
      }, requestOptions);
      conversation._localOnly = false;
      bindConversation(conversation, store);
      connectAttempt(
        conversation,
        store,
        stringValue(record(response.attempt).attemptId),
        Number(response.streamCursor || 0),
      );
      return response;
    } catch (error) {
      const latest = latestTurnFrom(error);
      if (latest) {
        store.dispatch({ type: 'snapshot', snapshot: {
          conversationRevision: store.getState().conversationRevision,
          turns: [latest],
        } });
        bindConversation(conversation, store);
      }
      throw error;
    }
  };

  const submitBranch = async (
    conversation: RuntimeConversation,
    branch: UnknownRecord,
    parentTurnId: string,
    message: unknown,
    config: unknown,
    extra: UnknownRecord = {},
  ): Promise<UnknownRecord> => {
    if (!conversation?.id || !branch || !parentTurnId) {
      throw new Error(
        'Branch generation requires a conversation, branch, and parent turn.',
      );
    }
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded) await hydrateConversation(conversation);
    bindConversation(conversation, store);
    const laneId = stringValue(branch._laneId || branch.laneId || branch.id);
    if (!laneId) throw new Error('Branch is missing its stable lane identity.');
    branch._laneId = laneId;
    const laneMeta = conversation._v2LaneMeta
      ?? (conversation._v2LaneMeta = Object.create(null) as Record<string, never>);
    laneMeta[laneId] = { ...branch, messages: undefined, parentTurnId };
    const response = await submit(store, null, config, {
      ...extra,
      commandId: extra.commandId || createTurnCommandId(),
      message,
      laneId,
      parentTurnId,
      actor: extra.actor || 'assistant',
      kind: extra.kind || 'branch_reply',
    });
    connectAttempt(
      conversation,
      store,
      stringValue(record(response.attempt).attemptId),
      Number(response.streamCursor || 0),
    );
    return response;
  };

  const ensureTurn = async (
    conversation: RuntimeConversation,
    turnId: string,
  ): Promise<RuntimeStore> => {
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded || !store.getState().turnsById[turnId]) {
      await hydrateConversation(conversation);
    } else {
      bindConversation(conversation, store);
    }
    return store;
  };

  const operateConversation = async (
    conversation: RuntimeConversation,
    turnId: string,
    operation: string,
    config: unknown = {},
    operationOptions: UnknownRecord = {},
  ): Promise<UnknownRecord> => {
    if (!conversation?.id || !turnId) {
      throw new Error('A stable turnId is required.');
    }
    const store = await ensureTurn(conversation, turnId);
    const response = await runOperation(
      store, turnId, operation, config, operationOptions,
    );
    connectAttempt(
      conversation,
      store,
      stringValue(record(response.attempt).attemptId),
      Number(response.streamCursor || 0),
    );
    return response;
  };

  const updateConversationTurn = async (
    conversation: RuntimeConversation,
    turnId: string,
    projection: unknown,
  ): Promise<UnknownRecord> => {
    if (!conversation?.id || !turnId) {
      throw new Error('A stable turnId is required.');
    }
    const store = await ensureTurn(conversation, turnId);
    const turn = store.getState().turnsById[turnId];
    if (!turn) throw new Error('Unknown turn; refresh the authoritative snapshot.');
    try {
      const response = record(await options.api.update(
        conversation.id,
        turnId,
        {
          expectedProjectionRevision: turn.projectionRevision,
          projection,
        },
      ));
      store.dispatch({ type: 'command_response', response });
      return response;
    } catch (error) {
      const latest = latestTurnFrom(error);
      if (latest) {
        store.dispatch({ type: 'snapshot', snapshot: {
          conversationRevision: store.getState().conversationRevision,
          turns: [latest],
        } });
      }
      throw error;
    }
  };

  const createBranchLane = async (
    conversation: RuntimeConversation,
    parentTurnId: string,
    descriptor: UnknownRecord = {},
  ): Promise<UnknownRecord> => {
    const store = await ensureTurn(conversation, parentTurnId);
    const parent = store.getState().turnsById[parentTurnId];
    if (!parent) throw new Error('Unknown parent turn.');
    const response = record(await options.api.createLane(
      conversation.id,
      parentTurnId,
      {
        ...descriptor,
        expectedProjectionRevision: parent.projectionRevision,
      },
    ));
    store.dispatch({ type: 'command_response', response });
    return response;
  };

  const deleteBranchLane = async (
    conversation: RuntimeConversation,
    parentTurnId: string,
    laneId: string,
  ): Promise<UnknownRecord> => {
    const store = await ensureTurn(conversation, parentTurnId);
    const response = record(await options.api.deleteLane(
      conversation.id, parentTurnId, laneId,
    ));
    store.dispatch({
      type: 'snapshot',
      snapshot: record(await options.api.list(conversation.id)),
    });
    return response;
  };

  const deleteConversationTurns = async (
    conversation: RuntimeConversation,
    turnIds: readonly string[],
  ): Promise<UnknownRecord> => {
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded) await hydrateConversation(conversation);
    bindConversation(conversation, store);
    const response = record(await options.api.deleteTurns(conversation.id, turnIds));
    store.dispatch({
      type: 'snapshot',
      snapshot: record(await options.api.list(conversation.id)),
    });
    return response;
  };

  return Object.freeze({
    emptyState: createTurnState,
    reducer: reduceTurnState,
    createStore: createTurnStore,
    submit,
    runOperation,
    connect,
    renderInto: renderTurnStateInto,
    finishPresentation: presentTurnFinish,
    resumeOptions: resumeTurnOptions,
    hydrateConversation,
    submitConversation,
    submitBranch,
    operateConversation,
    updateConversationTurn,
    createBranchLane,
    deleteBranchLane,
    deleteConversationTurns,
    markCommandPending(
      conversation: RuntimeConversation,
      turnId: string,
      operation: string,
    ) {
      if (!conversation?.id || !turnId) return;
      const store = ensureRuntimeStore(conversation.id);
      bindConversation(conversation, store);
      store.dispatch({ type: 'command_pending', turnId, operation });
    },
    markCommandFailed(conversation: RuntimeConversation, turnId: string) {
      if (!conversation?.id || !turnId) return;
      ensureRuntimeStore(conversation.id).dispatch({
        type: 'command_failed', turnId,
      });
    },
    abortConversation(conversation: RuntimeConversation) {
      return conversation?._activeAttemptId
        ? options.api.abort(conversation._activeAttemptId)
        : Promise.resolve(null);
    },
    abortAttempt(attemptId: string) {
      return attemptId ? options.api.abort(attemptId) : Promise.resolve(null);
    },
    ensureRuntimeStore,
    findConversation(conversationId: string) {
      return options.findConversation?.(conversationId) ?? null;
    },
    TERMINAL: terminal,
    isCutoverActive: () => runtimeCutoverActive,
  });
}
