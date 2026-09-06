import type {
  AppendSettledTurnRequest,
  ConnectionHealth,
  ConversationSyncApi,
  ConversationSyncSnapshot,
  CreateAttemptRequest,
  CreateLaneRequest,
  CreateTurnRequest,
  DeleteTurnsRequest,
  ExecutePlanRequest,
  FileChangesCommandRequest,
  ConversationTurnPage,
  UpdateTurnRequest,
  TurnRecord,
} from '../api/conversation-sync.generated';
import { assertConversationSyncSchema } from '../api/conversation-sync.generated';
import type { RequestOptions } from '../api/transport';
import {
  buildTurnOperationRequest,
  buildTurnSubmitRequest,
  createTurnCommandId,
} from './turn-command';
import {
  applyTurnStateProjection,
  type ProjectedConversation,
} from './turn-projection';
import { presentTurnFinish, resumeTurnOptions } from './turn-presentation';
import { renderTurnStateInto, type TurnRenderer } from './turn-render';
import {
  ConversationSyncCoordinator,
  type ConversationSyncConnection,
} from './conversation-sync';
import {
  createTurnPerceptionRecorder,
  type PerceptionAttemptIdentity,
} from './perception-recorder';
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
type RuntimeStore = TurnStore & {
  _snapshotLoaded?: boolean;
};

export interface RuntimeConversation extends ProjectedConversation {
  id: string;
  title?: string;
  createdAt?: number;
  _localOnly?: boolean;
}

export interface TurnRuntimeOptions {
  api: ConversationSyncApi;
  streamClientId?: string;
  findConversation?(conversationId: string): RuntimeConversation | null;
  persist?(conversation: RuntimeConversation): void;
  isActive?(conversation: RuntimeConversation): boolean;
  isDomStale?(conversation: RuntimeConversation): boolean;
  replaceAll?(
    conversation: RuntimeConversation,
    repaint?: { force?: boolean },
  ): void;
  /** Preferred production DOM port; false explicitly requests legacy fallback. */
  renderState?(
    conversation: RuntimeConversation,
    state: TurnState,
    render?: { force?: boolean },
  ): boolean | void;
  disposeRenderedState?(conversationId: string): void;
  deferTerminalRelease?(release: () => void): void;
  buildNavigation?(conversation: RuntimeConversation): void;
  renderConversationList?(): void;
  updateSendButton?(): void;
  onProtocolError?(error: Error): void;
  onResyncError?(error: unknown, turnId: string): void;
  onHealth?(conversationId: string, health: ConnectionHealth): void;
  onTurnSettled?(conversation: RuntimeConversation, turn: UnknownRecord): void;
  applySettings?(conversation: RuntimeConversation, settings: UnknownRecord): void;
  applySnapshotMetadata?(
    conversation: RuntimeConversation,
    snapshot: ConversationSyncSnapshot,
  ): void;
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
    hooks?: TurnConnectionHooks,
  ): ConversationSyncConnection;
  renderInto(
    container: Element,
    state: TurnState,
    renderTurn?: TurnRenderer,
  ): void;
  finishPresentation: typeof presentTurnFinish;
  resumeOptions: typeof resumeTurnOptions;
  hydrateConversation(conversation: RuntimeConversation): Promise<RuntimeStore>;
  wakeConversation(conversation: RuntimeConversation): Promise<RuntimeStore>;
  loadConversationTurnPage(
    conversation: RuntimeConversation,
    laneId: string,
    beforeOrdinal?: number,
    limit?: number,
  ): Promise<ConversationTurnPage>;
  submitConversation(
    conversation: RuntimeConversation,
    message: unknown,
    config: unknown,
    extra?: UnknownRecord,
  ): Promise<UnknownRecord>;
  appendSettledConversationTurn(
    conversation: RuntimeConversation,
    actor: AppendSettledTurnRequest['actor'],
    projection: unknown,
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
  executeConversationPlan(
    conversation: RuntimeConversation,
    sourceTurnId: string,
    expectedPlanId: string,
    expectedProjectionRevision: number,
    contextMode: 'current' | 'fresh',
    config?: unknown,
  ): Promise<UnknownRecord>;
  updateConversationTurn(
    conversation: RuntimeConversation,
    turnId: string,
    projection: unknown,
  ): Promise<UnknownRecord>;
  mutateConversationFileChanges(
    conversation: RuntimeConversation,
    turnId: string,
    operation: 'undo' | 'redo',
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
  cancelQueuedTurn(
    conversation: RuntimeConversation,
    queueId: string,
  ): Promise<UnknownRecord>;
  markCommandPending(
    conversation: RuntimeConversation,
    turnId: string,
    operation: string,
  ): void;
  markCommandFailed(conversation: RuntimeConversation, turnId: string): void;
  abortConversation(conversation: RuntimeConversation): Promise<unknown>;
  abortAttempt(attemptId: string): Promise<unknown>;
  readRuntimeState(conversationId: string): TurnState | null;
  hasAuthoritativeCommand(conversationId: string, commandId: string): boolean;
  /** Re-evaluate only the shells affected by an external active-id change. */
  reconcileConversationActivity(
    ...conversationIds: Array<string | null | undefined>
  ): void;
  ensureRuntimeStore(conversationId: string): RuntimeStore;
  invalidateConversation(conversationId: string, cursorHint?: string): void;
  disposeConversation(conversationId: string): void;
  hasLiveConnection(conversationId: string): boolean;
  findConversation(conversationId: string): RuntimeConversation | null;
  readonly TERMINAL: ReadonlySet<string>;
}

interface TurnConnectionHooks {
  onTerminal?(event: TurnEvent): void;
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

/**
 * A new-conversation shell owns a display placeholder, not a durable title.
 * Leave that placeholder empty on the first Turn command so the command
 * service can derive the authoritative fallback from the user's message.
 */
function titleForTurnConversationCreate(
  conversation: RuntimeConversation,
): string {
  const title = stringValue(conversation.title).trim();
  if (conversation._localOnly && (!title || title === 'New Chat')) return '';
  return title || 'New Chat';
}

function latestTurnFrom(error: unknown): TurnRecord | null {
  const failure = record(error);
  const body = record(failure.body);
  const response = record(failure.response);
  const latest = body.latestTurn || response.latestTurn;
  if (!latest || typeof latest !== 'object') return null;
  try {
    return assertConversationSyncSchema<TurnRecord>('TurnRecord', latest);
  } catch {
    return null;
  }
}

/* Bounded abort command: the send pipeline awaits abortConversation /
 * abortAttempt inside its catch recovery, and the typed transport only
 * enforces a declared timeout — an unbounded stalled abort POST held the
 * composer send lock pending until the page was reloaded. */
const ABORT_COMMAND_TIMEOUT_MS = 15_000;

/** Create the complete Turn/Attempt runtime without reading ambient globals. */
export function createConversationTurnRuntime(
  options: TurnRuntimeOptions,
): TurnRuntime {
  const terminal = new Set(['completed', 'interrupted', 'truncated', 'failed']);
  const runtimeStores = new Map<string, RuntimeStore>();
  const coordinators = new Map<string, ConversationSyncCoordinator>();
  const attemptConnectionLeases = new Map<string, ConversationSyncConnection>();
  const attemptHooks = new Map<string, TurnConnectionHooks>();
  const boundConversations = new WeakMap<RuntimeConversation, {
    store: RuntimeStore;
    unsubscribe: () => void;
  }>();
  const scheduledTerminalReleases = new WeakMap<RuntimeConversation, string>();
  const boundConversationById = new Map<string, RuntimeConversation>();
  const observedLiveAttempts = new Set<string>();
  const perceptionRecorder = createTurnPerceptionRecorder({
    api: options.api,
    clientId: options.streamClientId,
  });

  const perceptionIdentity = (
    conversationId: string,
  ): PerceptionAttemptIdentity | null => {
    const state = runtimeStores.get(conversationId)?.getState();
    if (!state) return null;
    const attempts = Object.values(state.attemptsById)
      .filter((attempt) => attempt?.attemptId && attempt.turnId
        && (attempt.status === 'pending' || attempt.status === 'running'))
      .sort((left, right) => {
        const leftLive = left?.status === 'pending' || left?.status === 'running';
        const rightLive = right?.status === 'pending' || right?.status === 'running';
        if (leftLive !== rightLive) return leftLive ? 1 : -1;
        return Number(left?.settledAt || left?.startedAt || left?.createdAt || 0)
          - Number(right?.settledAt || right?.startedAt || right?.createdAt || 0);
      });
    const attempt = attempts.at(-1);
    if (!attempt?.attemptId || !attempt.turnId) return null;
    return {
      attemptId: attempt.attemptId,
      turnId: attempt.turnId,
      projectionRevision: Number(
        state.turnsById[attempt.turnId]?.projectionRevision || 0,
      ),
    };
  };

  const reconcileConversationConnection = (
    conversation: RuntimeConversation,
    state: TurnState,
    active = options.isActive?.(conversation) ?? false,
  ): void => {
    const coordinator = coordinators.get(conversation.id);
    if (!coordinator) return;
    const hasLiveTurn = Object.values(state.turnsById).some((turn) =>
      turn && (turn.status === 'pending' || turn.status === 'running'));
    if (active || hasLiveTurn) {
      void coordinator.resume().catch(options.onProtocolError);
    } else {
      coordinator.pause();
    }
  };

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
    let stateRendered = false;
    const renderActiveState = (force = false): void => {
      if (!active) return;
      const handled = options.renderState?.(conversation, state, { force });
      if (handled !== false && options.renderState) {
        stateRendered = true;
        return;
      }
      options.replaceAll?.(conversation, force ? { force: true } : undefined);
    };
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
        renderActiveState(true);
        conversation._finishingStream = false;
        conversation._finishingAttemptId = null;
        options.updateSendButton?.();
      };
      if (options.deferTerminalRelease) options.deferTerminalRelease(release);
      else release();
    };
    const projectionChanged = applyTurnStateProjection({
      conversation,
      state,
      active,
      domStale: options.isDomStale?.(conversation) ?? false,
      persist: options.persist,
      replaceAll: () => {
        renderActiveState(terminalSettled);
        if (terminalSettled) {
          terminalRepainted = true;
          scheduleTerminalRelease();
        }
      },
      buildNavigation: options.buildNavigation,
      renderConversationList: options.renderConversationList,
      updateSendButton: options.updateSendButton,
    });
    /* Transport-only frames may not change the shell fingerprint, but the
     * typed surface still consumes the authoritative state directly. */
    if (active && options.renderState && !stateRendered) {
      renderActiveState(terminalSettled);
    }
    // A duplicate terminal snapshot can have the same projection fingerprint.
    // The DOM may still own a transient streaming bubble, so settling must force
    // one repaint even when the state projection itself is unchanged. Keep the
    // Stop latch set until that repaint returns; button polling must never
    // observe Send while the terminal answer is still absent from the DOM.
    if (terminalSettled && !terminalRepainted) {
      renderActiveState(true);
      scheduleTerminalRelease();
    }
    for (const turn of Object.values(state.turnsById)) {
      const attemptId = stringValue(turn?.currentAttemptId);
      if (!turn || !attemptId) continue;
      if (turn.status === 'pending' || turn.status === 'running') {
        observedLiveAttempts.add(attemptId);
      } else if (terminal.has(turn.status || '') && observedLiveAttempts.delete(attemptId)) {
        options.onTurnSettled?.(conversation, turn);
      }
    }
    reconcileConversationConnection(conversation, state, active);
  };

  const ensureRuntimeStore = (conversationId: string): RuntimeStore => {
    const existing = runtimeStores.get(conversationId);
    if (existing) return existing;
    let coordinator: ConversationSyncCoordinator;
    const store = createTurnStore(conversationId, {
      fetchSnapshot: async () => {
        const snapshot = await coordinator.recover('turn-store-resync');
        return { ...snapshot, authoritativeFull: true };
      },
      onResyncError: options.onResyncError,
    }) as RuntimeStore;
    coordinator = new ConversationSyncCoordinator({
      conversationId,
      streamClientId: options.streamClientId,
      api: options.api,
      onSnapshot(snapshot) {
        store._snapshotLoaded = true;
        const conversation = boundConversationById.get(conversationId);
        if (conversation) options.applySettings?.(
          conversation, record(snapshot.settings),
        );
        if (conversation) options.applySnapshotMetadata?.(
          conversation, snapshot,
        );
        store.dispatch({ type: 'snapshot', snapshot });
      },
      onTurnPage(page) {
        // History pages add bounded older records. They are never deletion
        // authority and therefore intentionally omit authoritativeFull.
        store.dispatch({ type: 'snapshot', snapshot: page });
      },
      onAttemptEvent(event, receivedAt, serverPublishedAt) {
        const previousRevision = Number(
          store.getState().turnsById[event.turnId]?.projectionRevision || 0,
        );
        store.dispatch({ type: 'event', event: event as TurnEvent });
        const turn = store.getState().turnsById[event.turnId];
        const applied = Number(turn?.projectionRevision || 0)
          >= Number(event.projectionRevision || 0);
        if (event.type === 'terminal_settlement') {
          attemptHooks.get(event.attemptId)?.onTerminal?.(event as TurnEvent);
          attemptHooks.delete(event.attemptId);
          attemptConnectionLeases.delete(event.attemptId);
        }
        if (applied && previousRevision < Number(event.projectionRevision || 0)) {
          perceptionRecorder.observeAttemptEvent(
            event,
            receivedAt,
            serverPublishedAt,
          );
        }
        return applied;
      },
      onTurnDelta(delta) {
        store.dispatch({ type: 'snapshot', snapshot: delta });
        const state = store.getState();
        return records(delta.turnPatches).every((change) => {
          const turnId = stringValue(change.turnId);
          const target = Number(change.targetProjectionRevision || 0);
          return Boolean(turnId && target > 0
            && Number(state.turnsById[turnId]?.projectionRevision || 0) >= target);
        });
      },
      onProtocolError: options.onProtocolError,
      onHealth(conversationId, health) {
        options.onHealth?.(conversationId, health);
        perceptionRecorder.observeHealth(
          conversationId,
          health,
          perceptionIdentity(conversationId),
        );
      },
      onPushWithheld(withheld) {
        store.dispatch({ type: 'push_withheld', pushWithheld: withheld });
      },
      eventSourceFactory: options.eventSourceFactory,
    });
    // Constructors do not publish externally. Register the complete pair
    // first so health-driven renders can safely re-enter any runtime method.
    store._snapshotLoaded = false;
    runtimeStores.set(conversationId, store);
    coordinators.set(conversationId, coordinator);
    coordinator.announceInitialHealth();
    return store;
  };

  // Pure read seam for catalog/sidebar consumers. A read must never create a
  // coordinator: construction publishes initial health, whose render callback
  // can re-enter a 500-row catalog scan and recursively construct the next
  // store until the browser stack overflows.
  const readRuntimeState = (conversationId: string): TurnState | null =>
    runtimeStores.get(conversationId)?.getState() ?? null;

  const bindConversation = (
    conversation: RuntimeConversation,
    store: RuntimeStore,
  ): void => {
    const previous = boundConversations.get(conversation);
    if (previous?.store === store) return;
    previous?.unsubscribe();
    boundConversationById.set(conversation.id, conversation);
    const unsubscribe = store.subscribe(
      (state) => projectConversation(conversation, state),
    );
    boundConversations.set(conversation, { store, unsubscribe });
    projectConversation(conversation, store.getState());
  };

  const connect = (
    store: TurnStore,
    attemptId: string,
    hooks: TurnConnectionHooks = {},
  ): ConversationSyncConnection => {
    const conversationId = store.getState().conversationId;
    const coordinator = coordinators.get(conversationId);
    if (!coordinator) throw new Error('Conversation coordinator is not initialized.');
    if (attemptId) attemptHooks.set(attemptId, hooks);
    void coordinator.resume().catch(options.onProtocolError);
    let leaseClosed = false;
    return {
      close() {
        if (leaseClosed) return;
        leaseClosed = true;
        if (attemptId) attemptHooks.delete(attemptId);
      },
      get cursor() { return coordinator.cursor; },
    };
  };

  const connectAttempt = (
    conversation: RuntimeConversation,
    store: RuntimeStore,
    attemptId: string,
  ): ConversationSyncConnection | null => {
    if (!attemptId) return null;
    const previous = attemptConnectionLeases.get(attemptId);
    if (previous) return previous;
    const connection = connect(store, attemptId, {
      onTerminal() {
        attemptConnectionLeases.delete(attemptId);
        projectConversation(conversation, store.getState());
      },
    });
    attemptConnectionLeases.set(attemptId, connection);
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
    const response = record(await options.api.createTurn(
      store.getState().conversationId,
      payload as unknown as CreateTurnRequest,
      requestOptions as RequestOptions,
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
      const response = record(await options.api.createAttempt(
        stringValue(turn.conversationId),
        turnId,
        buildTurnOperationRequest(
          turn, operation, config, operationOptions,
        ) as unknown as CreateAttemptRequest,
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

  const runHydrate = async (
    conversation: RuntimeConversation,
  ): Promise<RuntimeStore> => {
    if (!conversation?.id) throw new Error('Conversation is required.');
    const store = ensureRuntimeStore(conversation.id);
    const coordinator = coordinators.get(conversation.id);
    if (!coordinator) throw new Error('Conversation coordinator is not initialized.');
    const snapshot = await coordinator.hydrate(false);
    const turns = records(snapshot.turns);
    options.applySettings?.(conversation, record(snapshot.settings));
    options.applySnapshotMetadata?.(conversation, snapshot);
    bindConversation(conversation, store);
    if (options.isActive?.(conversation)) void coordinator.resume();
    for (const turn of turns) {
      const status = stringValue(turn.status);
      const attemptId = stringValue(turn.currentAttemptId);
      if ((status === 'pending' || status === 'running') && attemptId) {
        connectAttempt(conversation, store, attemptId);
      }
    }
    return store;
  };

  /* Explicit snapshot calls coalesce into one lane. Live changes never enter
   * this scheduler: the conversation coordinator owns their ordered SSE.
   *
   * The lane MUST be published before runHydrate starts. runHydrate announces
   * `connecting` synchronously; presentation hooks may re-enter
   * hydrateConversation while handling that health frame. Starting the work
   * before claiming the lane recursively launched hundreds of full snapshots
   * on a cold page and eventually overflowed the browser stack.
   *
   * Overlapping callers share the same authoritative snapshot. A trailing
   * snapshot is unnecessary: the coordinator opens its ordered event stream
   * from the snapshot cursor, so commits racing the read are replayed there. */
  const hydrateLanes = new Map<string, Promise<RuntimeStore>>();

  const hydrateConversation = (
    conversation: RuntimeConversation,
  ): Promise<RuntimeStore> => {
    if (!conversation?.id) return Promise.reject(
      new Error('Conversation is required.'),
    );
    const id = conversation.id;
    const existing = hydrateLanes.get(id);
    if (existing) return existing;
    // Promise.then defers runHydrate until after hydrateLanes.set below. This
    // ordering is the re-entrancy boundary; do not inline runHydrate here.
    const running = Promise.resolve()
      .then(() => runHydrate(conversation))
      .finally(() => {
        if (hydrateLanes.get(id) === running) hydrateLanes.delete(id);
      });
    hydrateLanes.set(id, running);
    return running;
  };

  /**
   * Revalidate a previously loaded conversation from its durable SSE cursor.
   * Cold stores still need one authoritative snapshot; warm stores retain the
   * exact projection already rendered and ask the ordered stream for only the
   * missing suffix. Cursor expiry is an explicit server reset, never a local
   * guess that eagerly reloads the entire Turn graph.
   */
  const wakeConversation = async (
    conversation: RuntimeConversation,
  ): Promise<RuntimeStore> => {
    if (!conversation?.id) throw new Error('Conversation is required.');
    const store = ensureRuntimeStore(conversation.id);
    // A local-only draft has no server row yet: hydrating it 404s and the
    // reconcile loop retries it forever. The in-memory store is authoritative
    // until the first submit persists the conversation.
    if (conversation._localOnly) return store;
    if (!store._snapshotLoaded) return hydrateConversation(conversation);
    const coordinator = coordinators.get(conversation.id);
    if (!coordinator) throw new Error('Conversation coordinator is not initialized.');
    bindConversation(conversation, store);
    await coordinator.wake();
    return store;
  };

  const loadConversationTurnPage = async (
    conversation: RuntimeConversation,
    laneId: string,
    beforeOrdinal?: number,
    limit = 64,
  ): Promise<ConversationTurnPage> => {
    if (!conversation?.id) throw new Error('Conversation is required.');
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded) await hydrateConversation(conversation);
    const coordinator = coordinators.get(conversation.id);
    if (!coordinator) throw new Error('Conversation coordinator is not initialized.');
    bindConversation(conversation, store);
    return coordinator.loadTurnPage(laneId, beforeOrdinal, limit);
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
          const coordinator = coordinators.get(conversation.id);
          if (!coordinator) throw new Error(
            'Conversation coordinator is not initialized.');
          await coordinator.hydrate(false);
        } catch (error) {
          if (Number(record(error).status || 0) !== 404) throw error;
        }
      }
      store._snapshotLoaded = true;
    }
    try {
      const {
        requestOptions,
        settings,
        ...commandExtra
      } = extra;
      const response = await submit(store, null, config, {
        ...commandExtra,
        commandId: extra.commandId || createTurnCommandId(),
        message,
        conversation: {
          allowCreate: true,
          title: titleForTurnConversationCreate(conversation),
          createdAt: conversation.createdAt || Date.now(),
          settings: settings || {},
        },
      }, requestOptions);
      conversation._localOnly = false;
      bindConversation(conversation, store);
      connectAttempt(
        conversation,
        store,
        stringValue(record(response.attempt).attemptId),
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
      const response = record(await options.api.updateTurn(
        conversation.id,
        turnId,
        {
          expectedProjectionRevision: turn.projectionRevision,
          projection: record(projection),
        } as UpdateTurnRequest,
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

  const executeConversationPlan = async (
    conversation: RuntimeConversation,
    sourceTurnId: string,
    expectedPlanId: string,
    expectedProjectionRevision: number,
    contextMode: 'current' | 'fresh',
    config: unknown = {},
  ): Promise<UnknownRecord> => {
    if (!conversation?.id || !sourceTurnId || !expectedPlanId) {
      throw new Error('An exact proposed-plan identity is required.');
    }
    const store = await ensureTurn(conversation, sourceTurnId);
    const source = store.getState().turnsById[sourceTurnId];
    const proposedPlan = source?.projection.proposedPlan;
    if (!source || source.projectionRevision !== expectedProjectionRevision
        || proposedPlan?.planId !== expectedPlanId) {
      throw new Error('The proposed plan changed; review the latest plan first.');
    }
    const operation = `execute-plan-${contextMode}`;
    store.dispatch({ type: 'command_pending', turnId: sourceTurnId, operation });
    try {
      const body: ExecutePlanRequest = {
        commandId: createTurnCommandId(),
        expectedProjectionRevision,
        planId: expectedPlanId,
        contextMode,
        config: record(config),
      };
      const response = record(await options.api.executePlan(
        conversation.id, sourceTurnId, body,
      ));
      store.dispatch({ type: 'command_response', response });
      options.applySettings?.(conversation, {
        planMode: false,
        autopilotEnabled: false,
        activeFlow: '',
        imageGenMode: false,
      });
      bindConversation(conversation, store);
      connectAttempt(
        conversation,
        store,
        stringValue(record(response.attempt).attemptId),
      );
      return response;
    } catch (error) {
      store.dispatch({ type: 'command_failed', turnId: sourceTurnId });
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

  const appendSettledConversationTurn = async (
    conversation: RuntimeConversation,
    actor: AppendSettledTurnRequest['actor'],
    projection: unknown,
    extra: UnknownRecord = {},
  ): Promise<UnknownRecord> => {
    if (!conversation?.id) throw new Error('Conversation is required.');
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded && !conversation._localOnly) {
      try {
        await hydrateConversation(conversation);
      } catch (error) {
        if (Number(record(error).status || 0) !== 404) throw error;
      }
    }
    store._snapshotLoaded = true;
    const body: AppendSettledTurnRequest = {
      commandId: stringValue(extra.commandId) || createTurnCommandId(),
      actor,
      projection: record(projection),
      ...(extra.kind ? { kind: stringValue(extra.kind) } : {}),
      ...(extra.status ? {
        status: extra.status as AppendSettledTurnRequest['status'],
      } : {}),
      ...(extra.settlement ? {
        settlement: extra.settlement as AppendSettledTurnRequest['settlement'],
      } : {}),
      ...(Number.isFinite(Number(extra.createdAt)) ? {
        createdAt: Number(extra.createdAt),
      } : {}),
      ...(extra.laneId ? { laneId: stringValue(extra.laneId) } : {}),
      ...(extra.runId ? { runId: stringValue(extra.runId) } : {}),
      conversation: {
        allowCreate: true,
        title: titleForTurnConversationCreate(conversation),
        createdAt: conversation.createdAt || Date.now(),
        settings: record(extra.settings),
      },
    };
    const response = record(await options.api.appendSettledTurn(
      conversation.id, body,
    ));
    store.dispatch({ type: 'command_response', response });
    conversation._localOnly = false;
    bindConversation(conversation, store);
    return response;
  };

  const mutateConversationFileChanges = async (
    conversation: RuntimeConversation,
    turnId: string,
    operation: 'undo' | 'redo',
  ): Promise<UnknownRecord> => {
    if (!conversation?.id || !turnId) {
      throw new Error('A stable turnId is required.');
    }
    const store = await ensureTurn(conversation, turnId);
    const turn = store.getState().turnsById[turnId];
    if (!turn) throw new Error('Unknown turn; refresh the authoritative snapshot.');
    const body: FileChangesCommandRequest = {
      commandId: createTurnCommandId(),
      expectedProjectionRevision: turn.projectionRevision,
    };
    const command = operation === 'undo'
      ? options.api.undoFileChanges
      : options.api.redoFileChanges;
    try {
      const response = record(await command(conversation.id, turnId, body));
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
      } as CreateLaneRequest,
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
    store.dispatch({ type: 'command_response', response });
    return response;
  };

  const deleteConversationTurns = async (
    conversation: RuntimeConversation,
    turnIds: readonly string[],
  ): Promise<UnknownRecord> => {
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded) await hydrateConversation(conversation);
    bindConversation(conversation, store);
    const response = record(await options.api.deleteTurns(
      conversation.id,
      { turnIds: [...turnIds] } as DeleteTurnsRequest,
    ));
    store.dispatch({ type: 'command_response', response });
    return response;
  };

  const cancelQueuedTurn = async (
    conversation: RuntimeConversation,
    queueId: string,
  ): Promise<UnknownRecord> => {
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded) await hydrateConversation(conversation);
    bindConversation(conversation, store);
    const response = record(await options.api.cancelQueue(
      conversation.id, queueId,
    ));
    store.dispatch({ type: 'command_response', response });
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
    wakeConversation,
    loadConversationTurnPage,
    submitConversation,
    appendSettledConversationTurn,
    submitBranch,
    operateConversation,
    executeConversationPlan,
    updateConversationTurn,
    mutateConversationFileChanges,
    createBranchLane,
    deleteBranchLane,
    deleteConversationTurns,
    cancelQueuedTurn,
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
      if (!conversation?.id) return Promise.resolve(null);
      const state = ensureRuntimeStore(conversation.id).getState();
      const activeAttemptId = [...(state.laneOrder.main ?? [])].reverse()
        .map((turnId) => state.turnsById[turnId])
        .find((turn) => turn && (turn.status === 'pending' || turn.status === 'running')
          && turn.currentAttemptId)?.currentAttemptId;
      return activeAttemptId
        ? options.api.abortAttempt(activeAttemptId, {
          timeout: ABORT_COMMAND_TIMEOUT_MS,
        })
        : Promise.resolve(null);
    },
    abortAttempt(attemptId: string) {
      return attemptId
        ? options.api.abortAttempt(attemptId, {
          timeout: ABORT_COMMAND_TIMEOUT_MS,
        })
        : Promise.resolve(null);
    },
    readRuntimeState,
    hasAuthoritativeCommand(conversationId: string, commandId: string) {
      const state = readRuntimeState(conversationId);
      if (!state || !commandId) return false;
      return state.queueItems.some((item) => item.sourceMessageId === commandId)
        || Object.values(state.attemptsById).some(
          (attempt) => attempt?.commandId === commandId,
        );
    },
    reconcileConversationActivity(
      ...conversationIds: Array<string | null | undefined>
    ) {
      const reconciled = new Set<string>();
      for (const candidate of conversationIds) {
        const conversationId = stringValue(candidate);
        if (!conversationId || reconciled.has(conversationId)) continue;
        reconciled.add(conversationId);
        const conversation = boundConversationById.get(conversationId);
        const store = runtimeStores.get(conversationId);
        if (conversation && store) {
          reconcileConversationConnection(conversation, store.getState());
        }
      }
    },
    ensureRuntimeStore,
    invalidateConversation(conversationId: string, cursorHint?: string) {
      /* Push/Broadcast frames are wake hints, never projection authority.
       * The coordinator either keeps consuming its ordered stream or reopens
       * that stream from the durable cursor; an expired cursor will produce
       * sync.reset_required and exactly one authoritative snapshot. Issuing a
       * snapshot here as well made every structural task event reload the
       * entire conversation, even while the SSE was current. Long Turns then
       * transferred and decoded the same multi-megabyte projection once per
       * tool/phase event. */
      coordinators.get(conversationId)?.invalidate(cursorHint);
    },
    disposeConversation(conversationId: string) {
      options.disposeRenderedState?.(conversationId);
      perceptionRecorder.disposeConversation(conversationId);
      const conversation = options.findConversation?.(conversationId) ?? null;
      if (conversation) {
        boundConversations.get(conversation)?.unsubscribe();
        boundConversations.delete(conversation);
      }
      const state = runtimeStores.get(conversationId)?.getState();
      for (const attemptId of Object.keys(state?.attemptsById ?? {})) {
        attemptConnectionLeases.get(attemptId)?.close();
        attemptConnectionLeases.delete(attemptId);
        attemptHooks.delete(attemptId);
      }
      coordinators.get(conversationId)?.close();
      coordinators.delete(conversationId);
      runtimeStores.delete(conversationId);
      hydrateLanes.delete(conversationId);
      boundConversationById.delete(conversationId);
    },
    hasLiveConnection(conversationId: string) {
      return coordinators.get(conversationId)?.connected ?? false;
    },
    findConversation(conversationId: string) {
      return options.findConversation?.(conversationId) ?? null;
    },
    TERMINAL: terminal,
  });
}
