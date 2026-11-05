/**
 * Turn command requests and response projection through the existing API/reducer.
 * Entry: createTurnCommands; the composition owner supplies store, hydration,
 * shell binding and attempt-connection ports. No transport or state copy lives here.
 */
import type {
  AppendSettledTurnRequest, CreateAttemptRequest, CreateLaneRequest, CreateTurnRequest,
  DeleteTurnsRequest, ExecutePlanRequest, FileChangesCommandRequest, UpdateTurnRequest,
  TurnRecord,
} from '../api/conversation-sync.generated';
import { assertConversationSyncSchema } from '../api/conversation-sync.generated';
import type { RequestOptions } from '../api/transport';
import { buildTurnOperationRequest, buildTurnSubmitRequest, createTurnCommandId } from './turn-command';
import type { TurnStore } from './turn-state';
import type { RuntimeConversation, TurnRuntime, TurnRuntimeOptions, UnknownRecord } from './turn-runtime-contracts';

interface TurnCommandPorts {
  ensureRuntimeStore(conversationId: string): TurnStore;
  readRuntimeStore(conversationId: string): TurnStore | null;
  isCurrentStore(store: TurnStore): boolean;
  resolveConversation(conversation: RuntimeConversation, store: TurnStore): RuntimeConversation | null;
  captureConversation(conversation: RuntimeConversation, store: TurnStore): () => RuntimeConversation | null;
  hydrateConversation(conversation: RuntimeConversation): Promise<TurnStore>;
  hydrateUnbound(conversationId: string): Promise<unknown>;
  loadCompleteTurn(store: TurnStore, turnId: string): Promise<TurnRecord>;
  bindConversation(conversation: RuntimeConversation, store: TurnStore): void;
  connectAttempt(conversation: RuntimeConversation, store: TurnStore, attemptId: string): void;
}

function record(value: unknown): UnknownRecord {
  return value && typeof value === 'object' ? value as UnknownRecord : {};
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

export type TurnCommands = Pick<TurnRuntime,
  'submit'
  | 'runOperation'
  | 'submitConversation'
  | 'appendSettledConversationTurn'
  | 'submitBranch'
  | 'operateConversation'
  | 'executeConversationPlan'
  | 'updateConversationTurn'
  | 'mutateConversationFileChanges'
  | 'createBranchLane'
  | 'deleteBranchLane'
  | 'deleteConversationTurns'
  | 'cancelQueuedTurn'
  | 'markCommandPending'
  | 'markCommandFailed'
  | 'abortConversation'
  | 'abortAttempt'>;

export function createTurnCommands(
  options: Pick<TurnRuntimeOptions, 'api' | 'applySettings'>,
  ports: TurnCommandPorts,
): TurnCommands {
  const { ensureRuntimeStore, hydrateConversation, hydrateUnbound,
    connectAttempt } = ports;

  const requireCurrentStore = (store: TurnStore): void => {
    if (!ports.isCurrentStore(store)) {
      throw new DOMException('Conversation was disposed before command dispatch.', 'AbortError');
    }
  };

  const sendCommand = async (
    store: TurnStore, request: () => Promise<unknown>,
  ): Promise<UnknownRecord> => {
    requireCurrentStore(store);
    const response = record(await request());
    // A committed command remains accepted after its view closes. Only the
    // current store may adopt it; reopening hydrates its own authoritative head.
    if (ports.isCurrentStore(store)) store.dispatch({ type: 'command_response', response });
    return response;
  };

  const handleCommandFailure = (
    store: TurnStore, error: unknown,
    context: { turnId?: string; clearPending?: boolean } = {},
  ): boolean => {
    if (!ports.isCurrentStore(store)) return false;
    if (context.clearPending && context.turnId) {
      store.dispatch({ type: 'command_failed', turnId: context.turnId });
    }
    if (!ports.isCurrentStore(store)) return false;
    const latest = latestTurnFrom(error);
    if (!latest || latest.conversationId !== store.getState().conversationId
        || (context.turnId && latest.turnId !== context.turnId)) return false;
    store.dispatch({ type: 'snapshot', snapshot: {
      conversationRevision: store.getState().conversationRevision,
      turns: [latest],
    } });
    return ports.isCurrentStore(store);
  };

  const bindCurrentConversation = (
    conversation: RuntimeConversation, store: TurnStore,
  ): void => {
    const current = ports.resolveConversation(conversation, store);
    if (current) ports.bindConversation(current, store);
  };

  const bindCommandStart = (conversation: RuntimeConversation, store: TurnStore): void => {
    requireCurrentStore(store);
    ports.bindConversation(conversation, store);
    requireCurrentStore(store);
  };

  const connectCurrentAttempt = (
    conversation: RuntimeConversation, store: TurnStore, attemptId: string,
  ): void => {
    const current = ports.resolveConversation(conversation, store);
    if (current) connectAttempt(current, store, attemptId);
  };

  const adoptPersistedConversation = (
    resolveConversation: () => RuntimeConversation | null, store: TurnStore,
  ): void => {
    const current = resolveConversation();
    if (!current) return;
    current._localOnly = false;
    ports.bindConversation(current, store);
  };

  const submit = async (
    store: TurnStore,
    inputTurn: unknown,
    config: unknown,
    extra: UnknownRecord = {},
    requestOptions: unknown = {},
  ): Promise<UnknownRecord> => {
    const payload = buildTurnSubmitRequest(inputTurn, config, extra);
    const response = await sendCommand(store, () => options.api.createTurn(
      store.getState().conversationId,
      payload as unknown as CreateTurnRequest,
      requestOptions as RequestOptions,
    ));

    return response;
  };

  const runOperation = async (
    store: TurnStore,
    turnId: string,
    operation: string,
    config: unknown = {},
    operationOptions: UnknownRecord = {},
  ): Promise<UnknownRecord> => {
    requireCurrentStore(store);
    const turn = store.getState().turnsById[turnId];
    if (!turn) throw new Error('Unknown turn; refresh the authoritative snapshot.');
    store.dispatch({ type: 'command_pending', turnId, operation });
    try {
      const response = await sendCommand(store, () => options.api.createAttempt(
        stringValue(turn.conversationId),
        turnId,
        buildTurnOperationRequest(
          turn, operation, config, operationOptions,
        ) as unknown as CreateAttemptRequest,
      ));

      return response;
    } catch (error) {
      handleCommandFailure(store, error, { turnId, clearPending: true });
      throw error;
    }
  };

  const submitConversation = async (
    conversation: RuntimeConversation,
    message: unknown,
    config: unknown,
    extra: UnknownRecord = {},
  ): Promise<UnknownRecord> => {
    if (!conversation?.id) throw new Error('Conversation is required.');
    const store = ensureRuntimeStore(conversation.id);
    const resolveConversation = ports.captureConversation(conversation, store);
    if (!store._snapshotLoaded) {
      if (!conversation._localOnly) {
        try {
          await hydrateUnbound(conversation.id);
        } catch (error) {
          if (Number(record(error).status || 0) !== 404) throw error;
        }
      }
      requireCurrentStore(store);
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
      adoptPersistedConversation(resolveConversation, store);
      connectCurrentAttempt(
        conversation,
        store,
        stringValue(record(response.attempt).attemptId),
      );
      return response;
    } catch (error) {
      if (handleCommandFailure(store, error)) {
        const current = resolveConversation();
        if (current) ports.bindConversation(current, store);
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
    bindCommandStart(conversation, store);
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
    connectCurrentAttempt(
      conversation,
      store,
      stringValue(record(response.attempt).attemptId),
    );
    return response;
  };

  const ensureTurn = async (
    conversation: RuntimeConversation,
    turnId: string,
  ): Promise<TurnStore> => {
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded || !store.getState().turnsById[turnId]) {
      await hydrateConversation(conversation);
    } else {
      bindCommandStart(conversation, store);
    }
    requireCurrentStore(store);
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
    connectCurrentAttempt(
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
    let turn = store.getState().turnsById[turnId];
    if (!turn) throw new Error('Unknown turn; refresh the authoritative snapshot.');
    try {
      let writableProjection = record(projection);
      const rounds = writableProjection.toolRounds;
      if (Array.isArray(rounds) && rounds.some(round =>
        round && typeof round === 'object' && '_snapshotToolResult' in round)) {
        // Snapshot-only result references are a readonly presentation. A whole
        // projection edit must restore complete tool lanes in ONE owner read,
        // rather than issuing hundreds of detail requests or saving omissions.
        requireCurrentStore(store);
        const complete = await ports.loadCompleteTurn(store, turnId);
        if (complete.projectionRevision !== turn.projectionRevision) {
          throw Object.assign(new Error('The turn changed; review it before saving again.'), {
            body: { latestTurn: complete },
          });
        }
        requireCurrentStore(store);
        writableProjection = {
          ...writableProjection,
          toolRounds: complete.projection.toolRounds,
          segments: complete.projection.segments,
        };
        store.dispatch({ type: 'snapshot', snapshot: {
          conversationRevision: store.getState().conversationRevision,
          turns: [complete],
        } });
        turn = complete;
      }
      const body = {
        expectedProjectionRevision: turn.projectionRevision,
        projection: writableProjection,
      } as UpdateTurnRequest;
      const response = await sendCommand(store, () => options.api.updateTurn(conversation.id, turnId, body));

      return response;
    } catch (error) {
      handleCommandFailure(store, error, { turnId });
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
    requireCurrentStore(store);
    store.dispatch({ type: 'command_pending', turnId: sourceTurnId, operation });
    try {
      const body: ExecutePlanRequest = {
        commandId: createTurnCommandId(),
        expectedProjectionRevision,
        planId: expectedPlanId,
        contextMode,
        config: record(config),
      };
      const response = await sendCommand(store, () => options.api.executePlan(
        conversation.id, sourceTurnId, body,
      ));

      const current = ports.resolveConversation(conversation, store);
      if (current) options.applySettings?.(current, {
        planMode: false,
        autopilotEnabled: false,
        delegateMode: false,
        activeFlow: '',
        imageGenMode: false,
      }, 'partial');
      bindCurrentConversation(conversation, store);
      connectCurrentAttempt(
        conversation,
        store,
        stringValue(record(response.attempt).attemptId),
      );
      return response;
    } catch (error) {
      handleCommandFailure(store, error, { turnId: sourceTurnId, clearPending: true });
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
    const resolveConversation = ports.captureConversation(conversation, store);
    if (!store._snapshotLoaded && !conversation._localOnly) {
      try {
        await hydrateConversation(conversation);
      } catch (error) {
        if (Number(record(error).status || 0) !== 404) throw error;
      }
    }
    requireCurrentStore(store);
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
    const response = await sendCommand(store, () => options.api.appendSettledTurn(
      conversation.id, body,
    ));

    adoptPersistedConversation(resolveConversation, store);
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
      const response = await sendCommand(store, () => command(conversation.id, turnId, body));

      return response;
    } catch (error) {
      handleCommandFailure(store, error, { turnId });
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
    const response = await sendCommand(store, () => options.api.createLane(
      conversation.id,
      parentTurnId,
      {
        ...descriptor,
        expectedProjectionRevision: parent.projectionRevision,
      } as CreateLaneRequest,
    ));

    return response;
  };

  const deleteBranchLane = async (
    conversation: RuntimeConversation,
    parentTurnId: string,
    laneId: string,
  ): Promise<UnknownRecord> => {
    const store = await ensureTurn(conversation, parentTurnId);
    const response = await sendCommand(store, () => options.api.deleteLane(
      conversation.id, parentTurnId, laneId,
    ));

    return response;
  };

  const deleteConversationTurns = async (
    conversation: RuntimeConversation,
    turnIds: readonly string[],
  ): Promise<UnknownRecord> => {
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded) await hydrateConversation(conversation);
    bindCommandStart(conversation, store);
    const response = await sendCommand(store, () => options.api.deleteTurns(
      conversation.id,
      { turnIds: [...turnIds] } as DeleteTurnsRequest,
    ));

    return response;
  };

  const cancelQueuedTurn = async (
    conversation: RuntimeConversation,
    queueId: string,
  ): Promise<UnknownRecord> => {
    const store = ensureRuntimeStore(conversation.id);
    if (!store._snapshotLoaded) await hydrateConversation(conversation);
    bindCommandStart(conversation, store);
    const response = await sendCommand(store, () => options.api.cancelQueue(
      conversation.id, queueId,
    ));

    return response;
  };

  return {
    submit,
    runOperation,
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
      if (!ports.isCurrentStore(store)) return;
      ports.bindConversation(conversation, store);
      if (ports.isCurrentStore(store)) store.dispatch({ type: 'command_pending', turnId, operation });
    },
    markCommandFailed(conversation: RuntimeConversation, turnId: string) {
      if (!conversation?.id || !turnId) return;
      const store = ports.readRuntimeStore(conversation.id);
      if (store && ports.isCurrentStore(store)) store.dispatch({ type: 'command_failed', turnId });
    },
    abortConversation(conversation: RuntimeConversation) {
      if (!conversation?.id) return Promise.resolve(null);
      const state = ports.readRuntimeStore(conversation.id)?.getState();
      if (!state) return Promise.resolve(null);
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
  };
}
