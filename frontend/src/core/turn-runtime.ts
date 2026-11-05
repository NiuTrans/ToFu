/**
 * Conversation Turn composition: canonical stores, sync coordinators and shell
 * bindings. Entry: createConversationTurnRuntime; injected host ports own DOM
 * rendering. Render observations and receipt delivery have separate owners.
 */
import { entityRecordValues } from './entity-record';
import type { ConversationTurnPage, ConversationRoundDirectory, ConversationRoundEntry } from '../api/conversation-sync.generated';
import { conversationConnectionHealth } from './connection-health';
import { createTurnCommands } from './turn-command-execution';
import { createTurnHydration } from './turn-hydration';
import { createTurnAttemptLifecycle, type TurnAttemptLifecycle } from './turn-attempt-lifecycle';
import { createTurnProjectionReconciler } from './turn-projection';
import { presentTurnFinish, resumeTurnOptions } from './turn-presentation';
import { renderTurnStateInto } from './turn-render';
import { ConversationSyncCoordinator, type ConversationSyncConnection } from './conversation-sync';
import { createConversationEventMultiplexer } from './conversation-event-multiplexer';
import { createTurnRenderObservations } from './turn-render-observations';
import {
  createTurnState, createTurnStore, reduceTurnState,
  type TurnEvent, type TurnState, type TurnStateUpdate, type TurnStore,
} from './turn-state';
import type {
  RuntimeConversation, TurnRuntime, TurnRuntimeOptions, TurnConnectionHooks, UnknownRecord,
} from './turn-runtime-contracts';
export type { RuntimeConversation, TurnRuntime, TurnRuntimeOptions } from './turn-runtime-contracts';

function record(value: unknown): UnknownRecord {
  return value && typeof value === 'object' ? value as UnknownRecord : {};
}

function records(value: unknown): UnknownRecord[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** Create the complete Turn/Attempt runtime without reading ambient globals. */
export function createConversationTurnRuntime(
  options: TurnRuntimeOptions,
): TurnRuntime {
  const terminal = new Set(['completed', 'interrupted', 'truncated', 'failed']);
  const shellProjection = createTurnProjectionReconciler();
  const runtimeStores = new Map<string, TurnStore>();
  // Weak membership distinguishes a retired runtime store from a caller-owned
  // standalone store accepted by the low-level submit/runOperation API.
  const ownedStores = new WeakSet<TurnStore>();
  const coordinators = new Map<string, ConversationSyncCoordinator>();
  const eventSourceFactory = options.eventSourceFactory ?? createConversationEventMultiplexer({
    api: options.api,
    streamClientId: options.streamClientId ?? '',
    onProtocolError: options.onProtocolError,
  }).open;
  let transportSuspended = false;
  const attemptLifecycles = new WeakMap<TurnStore, TurnAttemptLifecycle>();
  const boundConversations = new Map<string, {
    conversation: RuntimeConversation;
    store: TurnStore;
    unsubscribe: () => void;
  }>();
  const scheduledTerminalReleases = new WeakMap<RuntimeConversation, string>();
  const disposingConversations = new Set<string>();
  const renderObservations = createTurnRenderObservations(options, {
    readState: conversationId => runtimeStores.get(conversationId)?.getState() ?? null,
    isActive(conversationId) {
      const conversation = boundConversations.get(conversationId)?.conversation;
      return Boolean(conversation && options.isActive?.(conversation));
    },
    readHealth: conversationId => conversationConnectionHealth.get(conversationId),
    terminalStatuses: terminal,
  });

  const reconcileConversationConnection = (
    conversation: RuntimeConversation,
    state: TurnState,
  ): void => {
    const binding = boundConversations.get(conversation.id);
    const coordinator = coordinators.get(conversation.id);
    const current = () => binding?.conversation === conversation
      && boundConversations.get(conversation.id) === binding
      && runtimeStores.get(conversation.id) === binding.store
      && binding.store.getState() === state
      && hydration.isCurrentConversation(conversation);
    if (!coordinator || !current()) return;
    const active = options.isActive?.(conversation) ?? false;
    if (!current()) return;
    if (conversation._localOnly) {
      coordinator.pause();
      return;
    }
    if (active || entityRecordValues(state.turnsById).some((turn) =>
      turn && (turn.status === 'pending' || turn.status === 'running'))) {
      void coordinator.resume().catch(options.onProtocolError);
    } else {
      coordinator.pause();
    }
  };

  const projectConversation = (
    conversation: RuntimeConversation,
    state: TurnState,
    update?: TurnStateUpdate,
  ): void => {
    const binding = boundConversations.get(conversation.id);
    const currentBinding = () => binding?.conversation === conversation
      && boundConversations.get(conversation.id) === binding
      && runtimeStores.get(conversation.id) === binding.store
      && hydration.isCurrentConversation(conversation);
    if (!binding || !currentBinding()) return;
    const currentProjection = () => currentBinding() && binding.store.getState() === state;
    options.onStateApplied?.(conversation, state, update);
    if (!currentProjection()) return;
    let terminalSettled = false;
    const finishingAttemptId = conversation._finishingAttemptId;
    if (conversation._finishingStream && finishingAttemptId) {
      const turnStatus = entityRecordValues(state.turnsById).find((turn) =>
        turn?.currentAttemptId === finishingAttemptId)?.status;
      terminalSettled = terminal.has(turnStatus || '');
    }
    const active = options.isActive?.(conversation) ?? false;
    const domStale = options.isDomStale?.(conversation) ?? false;
    if (!currentProjection()) return;
    let terminalRepainted = false;
    let stateRendered = false;
    const renderActiveState = (force = false, projection = state): void => {
      const current = () => currentBinding() && binding.store.getState() === projection;
      if (!current()) return;
      if (!options.isActive?.(conversation)) return;
      if (!current()) return;
      const handled = options.renderState?.(conversation, projection,
        { force, update: projection === state ? update : undefined });
      if (!current()) return;
      if (handled !== false && options.renderState) {
        stateRendered = true;
      } else if (options.replaceAll) {
        options.replaceAll(conversation, force ? { force: true } : undefined);
        stateRendered = true;
      }
      // The renderer may only have scheduled work. Perception evidence enters
      // through notifyRenderedState after the actual mounted DOM commit.
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
        renderActiveState(true, binding.store.getState());
        if (!currentBinding() || !conversation._finishingStream
            || conversation._finishingAttemptId !== finishingAttemptId) return;
        conversation._finishingStream = false;
        conversation._finishingAttemptId = null;
        options.updateSendButton?.();
      };
      if (options.deferTerminalRelease) options.deferTerminalRelease(release);
      else release();
    };
    shellProjection.apply({
      conversation,
      state,
      active,
      domStale,
      persist: () => { if (currentProjection()) options.persist?.(conversation); },
      replaceAll: () => {
        if (!currentProjection()) return;
        renderActiveState(terminalSettled);
        if (terminalSettled && currentProjection()) {
          terminalRepainted = true;
          scheduleTerminalRelease();
        }
      },
      buildNavigation: () => { if (currentProjection()) options.buildNavigation?.(conversation); },
      renderConversationList: () => { if (currentProjection()) options.renderConversationList?.(); },
      updateSendButton: () => { if (currentProjection()) options.updateSendButton?.(); },
    }, update);
    if (!currentProjection()) return;
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
    if (!currentBinding() || binding.store.getState() !== state) return;
    attemptLifecycles.get(binding.store)?.observe(state, turn => {
      const current = boundConversations.get(conversation.id);
      if (current?.store === binding.store) options.onTurnSettled?.(current.conversation, turn);
    });
    reconcileConversationConnection(conversation, state);
  };

  /* A replacing tail-window snapshot keeps the Turns this replica had loaded
   * below its window (TurnHistoryLaneState.retainedFromOrdinal). Re-read that
   * range through the continuous history frontier once the coordinator has
   * committed the snapshot cursor and opened its stream: each frontier page
   * confirms its retained Turns, replaces stale ones or removes deleted ones.
   * Both the snapshot and every stream opened while the range is unverified
   * arm a pass, so a pass skipped because the stream was paused (a hydrate
   * of an inactive conversation) or one that failed runs again; the range is
   * never left stale until reload. The coordinator's default page size is
   * the Surface's, so a reader-driven read joins it. */
  const retainedHistoryReads = new Map<
    TurnStore, ReturnType<typeof setTimeout> | 'reading' | 'superseded'
  >();
  const retainedHistoryFrontier = (
    state: TurnState,
  ): { laneId: string; beforeOrdinal: number } | null => {
    for (const [laneId, history] of Object.entries(state.historyByLane)) {
      if (history?.retainedFromOrdinal !== undefined && history.nextBeforeOrdinal !== null) {
        return { laneId, beforeOrdinal: history.nextBeforeOrdinal };
      }
    }
    return null;
  };
  const readRetainedHistory = async (conversationId: string, store: TurnStore): Promise<void> => {
    // A frontier page lowers the frontier or clears the retained range; one
    // published off the frontier does neither, which ends this pass.
    for (let previous = Number.POSITIVE_INFINITY; ;) {
      const coordinator = coordinators.get(conversationId);
      const frontier = retainedHistoryFrontier(store.getState());
      if (!frontier || frontier.beforeOrdinal >= previous || !coordinator?.connected
          || runtimeStores.get(conversationId) !== store) return;
      previous = frontier.beforeOrdinal;
      await coordinator.loadTurnPage(frontier.laneId, frontier.beforeOrdinal);
    }
  };
  const scheduleRetainedHistoryRead = (conversationId: string, store: TurnStore): void => {
    const current = retainedHistoryReads.get(store);
    // A snapshot applied during a pass can raise the frontier above the one
    // that pass is reading; that pass then stops and one more follows it.
    if (current === 'reading') retainedHistoryReads.set(store, 'superseded');
    if (current !== undefined || runtimeStores.get(conversationId) !== store
        || !retainedHistoryFrontier(store.getState())) return;
    retainedHistoryReads.set(store, setTimeout(() => {
      retainedHistoryReads.set(store, 'reading');
      // The coordinator already reported a failed read; the next opened
      // stream or reader paging retries the same frontier.
      void readRetainedHistory(conversationId, store).catch(() => undefined).finally(() => {
        const superseded = retainedHistoryReads.get(store) === 'superseded';
        retainedHistoryReads.delete(store);
        if (superseded) scheduleRetainedHistoryRead(conversationId, store);
      });
    }, 0));
  };

  const ensureRuntimeStore = (conversationId: string): TurnStore => {
    const existing = runtimeStores.get(conversationId);
    if (existing) return existing;
    let coordinator: ConversationSyncCoordinator;
    const store = createTurnStore(conversationId, {
      fetchSnapshot: async () => {
        const snapshot = await coordinator.recover('turn-store-resync');
        return { ...snapshot, authoritativeFull: true };
      },
      onResyncError: options.onResyncError,
    }) as TurnStore;
    coordinator = new ConversationSyncCoordinator({
      conversationId,
      snapshotViewport: options.snapshotViewport,
      api: options.api,
      onSnapshot(snapshot) {
        // A newer partial command ACK does not contain this snapshot's other
        // Turns. The reducer intentionally rejects an older conversation
        // revision; that rejection must keep the coordinator's reset barrier.
        if (snapshot.conversationRevision < store.getState().conversationRevision) {
          throw new Error('Conversation snapshot is stale and was not applied');
        }
        store.dispatch({ type: 'snapshot', snapshot });
        store._snapshotLoaded = true;
        hydration.applyBoundSnapshot(store, snapshot);
        scheduleRetainedHistoryRead(conversationId, store);
      },
      onStreamOpened() {
        scheduleRetainedHistoryRead(conversationId, store);
      },
      onTurnPage(page, mode) {
        // History pages add bounded older records and never carry
        // authoritativeFull. A continuous frontier page removes only the
        // Turns retained below a replacing tail window that it omits.
        store.dispatch({ type: 'snapshot', snapshot: { ...page, historyMode: mode } });
      },
      onAttemptEvent(event, receivedAt, serverPublishedAt) {
        const previousRevision = Number(
          store.getState().turnsById[event.turnId]?.projectionRevision || 0,
        );
        const observationKey = renderObservations.stageReceivedEvent(
          event, previousRevision, receivedAt, serverPublishedAt,
        );
        store.dispatch({ type: 'event', event: event as TurnEvent });
        const turn = store.getState().turnsById[event.turnId];
        const applied = Number(turn?.projectionRevision || 0)
          >= Number(event.projectionRevision || 0);
        if (event.type === 'terminal_settlement') {
          attemptLifecycles.get(store)?.deliverTerminal(event as TurnEvent);
        }
        if (!applied) renderObservations.discardReceivedEvent(observationKey);
        return applied;
      },
      onTurnDelta(delta) {
        // Upserts, tombstones and queue changes have the same revision fence
        // as patches. A vacuous patch-only check acknowledged rejected rows.
        if (Number(delta.conversationRevision || 0)
            < store.getState().conversationRevision) return false;
        store.dispatch({ type: 'snapshot', snapshot: delta });
        const state = store.getState();
        const rowsApplied = records(delta.turns).every((incoming) => {
          const local = state.turnsById[stringValue(incoming.turnId)];
          return Boolean(local && Number(local.projectionRevision)
            >= Number(incoming.projectionRevision));
        }) && (Array.isArray(delta.deletedTurnIds) ? delta.deletedTurnIds : [])
          .every((id) => typeof id === 'string' && !state.turnsById[id]);
        return rowsApplied && records(delta.turnPatches).every((change) => {
          const turnId = stringValue(change.turnId);
          const target = Number(change.targetProjectionRevision || 0);
          return Boolean(turnId && target > 0
            && Number(state.turnsById[turnId]?.projectionRevision || 0) >= target);
        });
      },
      onProtocolError: options.onProtocolError,
      onHealth(conversationId, health, demand) {
        const current = conversationConnectionHealth.get(conversationId);
        // Raw observers can re-enter pause/recovery before this callback. Never
        // regress the shell to an outer reading after a newer verdict commits.
        if (current && (current.generation !== health.generation
            || current.state !== health.state
            || current.transport !== health.transport
            || current.reason !== health.reason
            || current.observedAt !== health.observedAt
            || current.lastFrameAt !== health.lastFrameAt
            || current.retryCount !== health.retryCount)) return;
        const latest = current ?? health;
        const conversation = boundConversations.get(conversationId)?.conversation
          ?? options.findConversation?.(conversationId);
        const previous = conversation?._conversationSyncHealth;
        if (conversation) conversation._conversationSyncHealth = latest;
        if (conversation && latest.state === 'live'
            && previous?.state === 'live'
            && previous.transport === latest.transport
            && previous.reason === latest.reason
            // A healthy transport is not proof that its canonical graph has
            // committed to the active Surface. Keep the existing heartbeat
            // demand until that independent DOM owner acknowledges convergence.
            && !(demand === 'heartbeat' && options.isActive?.(conversation)
              && options.isDomStale?.(conversation))) return;
        options.onHealth?.(conversationId, latest);
        // Only the host's DOM commit port can turn this into a painted receipt.
      },
      onPushWithheld(withheld) {
        store.dispatch({ type: 'push_withheld', pushWithheld: withheld });
      },
      onDebugFrame: options.onDebugFrame,
      eventSourceFactory,
    });
    // Constructors do not publish externally. Register the complete pair
    // first so health-driven renders can safely re-enter any runtime method.
    attemptLifecycles.set(store, createTurnAttemptLifecycle(store, {
      coordinator,
      terminalStatuses: terminal,
      onProtocolError: options.onProtocolError,
      onTerminal() {
        const binding = boundConversations.get(conversationId);
        if (binding?.store === store) projectConversation(binding.conversation, store.getState());
      },
    }));
    store._snapshotLoaded = false;
    ownedStores.add(store);
    runtimeStores.set(conversationId, store);
    coordinators.set(conversationId, coordinator);
    coordinator.setSuspended(transportSuspended);
    coordinator.announceInitialHealth();
    return store;
  };

  // Pure read seam for catalog/sidebar consumers. A read must never create a
  // coordinator: construction publishes initial health, whose render callback
  // can re-enter a 500-row catalog scan and recursively construct the next
  // store until the browser stack overflows.
  const readRuntimeState = (conversationId: string): TurnState | null =>
    runtimeStores.get(conversationId)?.getState() ?? null;

  const unbindConversation = (conversationId: string): void => {
    const binding = boundConversations.get(conversationId);
    if (!binding) return;
    boundConversations.delete(conversationId);
    binding.unsubscribe();
    scheduledTerminalReleases.delete(binding.conversation);
    shellProjection.forget(binding.conversation);
  };

  const bindConversation = (
    conversation: RuntimeConversation,
    store: TurnStore,
  ): void => {
    hydration.claimBoundConversation(conversation);
    const previous = boundConversations.get(conversation.id);
    if (previous?.conversation === conversation && previous.store === store) return;
    unbindConversation(conversation.id);
    const health = conversationConnectionHealth.get(conversation.id);
    if (health) conversation._conversationSyncHealth = health;
    const unsubscribe = store.subscribe(
      (state, update) => projectConversation(conversation, state, update),
    );
    boundConversations.set(conversation.id, { conversation, store, unsubscribe });
    projectConversation(conversation, store.getState());
  };

  const requireAttemptLifecycle = (store: TurnStore): TurnAttemptLifecycle => {
    const id = store.getState().conversationId;
    const owner = attemptLifecycles.get(store);
    if (!owner || runtimeStores.get(id) !== store || disposingConversations.has(id)) {
      throw new Error('Conversation store was disposed or is not owned by this runtime.');
    }
    return owner;
  };

  const connect = (
    store: TurnStore,
    attemptId: string,
    hooks: TurnConnectionHooks = {},
  ): ConversationSyncConnection => {
    return requireAttemptLifecycle(store).connect(attemptId, hooks);
  };

  const connectAttempt = (
    _conversation: RuntimeConversation,
    store: TurnStore,
    attemptId: string,
  ): ConversationSyncConnection | null => {
    if (!attemptId) return null;
    return requireAttemptLifecycle(store).connectOnce(attemptId);
  };

  const hydration = createTurnHydration(options, {
    ensureRuntimeStore,
    readRuntimeStore: conversationId => runtimeStores.get(conversationId),
    coordinatorFor: conversationId => coordinators.get(conversationId),
    isDisposing: conversationId => disposingConversations.has(conversationId),
    boundConversation: conversationId => boundConversations.get(conversationId)?.conversation,
    bindConversation,
    connectAttempt,
  });
  const { hydrateConversation } = hydration;

  /**
   * Revalidate a previously loaded conversation from its durable SSE cursor.
   * Cold stores still need one authoritative snapshot; warm stores retain the
   * exact projection already rendered and ask the ordered stream for only the
   * missing suffix. Cursor expiry is an explicit server reset, never a local
   * guess that eagerly reloads the entire Turn graph.
   */
  const wakeConversation = async (
    conversation: RuntimeConversation,
  ): Promise<TurnStore> => {
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

  const resolveCommandConversation = (
    conversation: RuntimeConversation, store: TurnStore,
  ): RuntimeConversation | null => {
    if (runtimeStores.get(conversation.id) !== store
        || disposingConversations.has(conversation.id)) return null;
    return hydration.requestedConversation(conversation.id)
      ?? boundConversations.get(conversation.id)?.conversation ?? conversation;
  };

  const commands = createTurnCommands(options, {
    ensureRuntimeStore, hydrateConversation, bindConversation, connectAttempt,
    readRuntimeStore: conversationId => runtimeStores.get(conversationId) ?? null,
    isCurrentStore(store) {
      const id = store.getState().conversationId;
      return !ownedStores.has(store)
        || (runtimeStores.get(id) === store && !disposingConversations.has(id));
    },
    resolveConversation: resolveCommandConversation,
    captureConversation(conversation, store) {
      const binding = boundConversations.get(conversation.id);
      const requested = hydration.requestedConversation(conversation.id);
      return () => {
        const current = resolveCommandConversation(conversation, store);
        if (!current) return null;
        // The command's incoming shell wins unless a later binding claimed
        // the same store while the request was pending.
        return boundConversations.get(conversation.id) === binding
          && hydration.requestedConversation(conversation.id) === requested ? conversation : current;
      };
    },
    hydrateUnbound(conversationId) {
      const coordinator = coordinators.get(conversationId);
      if (!coordinator) throw new Error('Conversation coordinator is not initialized.');
      return coordinator.hydrate(false);
    },
    loadCompleteTurn(store, turnId) {
      const coordinator = coordinators.get(store.getState().conversationId);
      if (!coordinator) throw new Error('Conversation coordinator is not initialized.');
      return coordinator.loadCompleteTurn(turnId);
    },
  });

  return Object.freeze({
    emptyState: createTurnState,
    reducer: reduceTurnState,
    createStore: createTurnStore,
    submit: commands.submit,
    runOperation: commands.runOperation,
    connect,
    renderInto: renderTurnStateInto,
    finishPresentation: presentTurnFinish,
    resumeOptions: resumeTurnOptions,
    hydrateConversation,
    wakeConversation,
    roundDirectoryKey(conversationId: string) { return coordinators.get(conversationId)?.roundDirectoryRevision ?? ''; },
    async loadConversationRoundDirectory(conversation: RuntimeConversation, laneId: string, offset = 0, limit = 50, anchorTurnId?: string) {
      if (!conversation?.id) throw new Error('Conversation is required.');
      if (conversation._localOnly) throw new Error('Local draft has no round directory.');
      const store = ensureRuntimeStore(conversation.id);
      if (!store._snapshotLoaded) await hydrateConversation(conversation);
      const coordinator = coordinators.get(conversation.id);
      if (!coordinator) throw new Error('Conversation coordinator is not initialized.');
      return coordinator.loadRoundDirectory(laneId, offset, limit, anchorTurnId);
    },
    async hydrateConversationRound(
      conversation: RuntimeConversation, laneId: string,
      entry: Pick<ConversationRoundEntry, 'turnId' | 'ordinal'>,
    ) {
      if (!conversation?.id) throw new Error('Conversation is required.');
      const store = ensureRuntimeStore(conversation.id);
      // A just-selected conversation joins its coalesced snapshot first.
      if (!store._snapshotLoaded) await hydrateConversation(conversation);
      const coordinator = coordinators.get(conversation.id);
      if (!coordinator) throw new Error('Conversation coordinator is not initialized.');
      if (!store.getState().turnsById[entry.turnId]) {
        // The target and the Turns after it; `beforeOrdinal` is exclusive.
        await coordinator.loadTurnPage(laneId, entry.ordinal + 8, 8, 'detached');
        if (!store.getState().turnsById[entry.turnId]) {
          await coordinator.loadTurnPage(laneId, entry.ordinal + 1, 1, 'detached');
        }
      }
      const target = store.getState().turnsById[entry.turnId];
      if (!target || target.laneId !== laneId || target.ordinal !== entry.ordinal) {
        throw new Error('Round target no longer exists.');
      }
    },
    loadConversationTurnPage,
    submitConversation: commands.submitConversation,
    appendSettledConversationTurn: commands.appendSettledConversationTurn,
    submitBranch: commands.submitBranch,
    operateConversation: commands.operateConversation,
    executeConversationPlan: commands.executeConversationPlan,
    updateConversationTurn: commands.updateConversationTurn,
    mutateConversationFileChanges: commands.mutateConversationFileChanges,
    createBranchLane: commands.createBranchLane,
    deleteBranchLane: commands.deleteBranchLane,
    deleteConversationTurns: commands.deleteConversationTurns,
    cancelQueuedTurn: commands.cancelQueuedTurn,
    markCommandPending: commands.markCommandPending,
    markCommandFailed: commands.markCommandFailed,
    abortConversation: commands.abortConversation,
    abortAttempt: commands.abortAttempt,
    readRuntimeState,
    notifyRenderedState: renderObservations.notifyRenderedState,
    hasAuthoritativeCommand(conversationId: string, commandId: string) {
      const state = readRuntimeState(conversationId);
      if (!state || !commandId) return false;
      return state.queueItems.some((item) => item.sourceMessageId === commandId)
        || entityRecordValues(state.attemptsById).some(
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
        const conversation = boundConversations.get(conversationId)?.conversation;
        const store = runtimeStores.get(conversationId);
        if (conversation && store) {

          if (!options.isActive?.(conversation)) {
            renderObservations.invalidateConversation(conversationId);
          } else {
            projectConversation(conversation, store.getState());
          }
          reconcileConversationConnection(conversation, store.getState());
        }
      }
    },
    ensureRuntimeStore,
    setTransportSuspended(suspended: boolean) {
      transportSuspended = suspended;
      for (const coordinator of coordinators.values()) coordinator.setSuspended(suspended);
    },
    invalidateConversation(conversationId: string, cursorHint?: string) {
      options.onConversationInvalidated?.(conversationId);
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
      if (disposingConversations.has(conversationId)) return;
      const store = runtimeStores.get(conversationId);
      disposingConversations.add(conversationId);
      hydration.disposeConversation(conversationId);
      if (store) {
        const scheduledRead = retainedHistoryReads.get(store);
        if (scheduledRead !== undefined && typeof scheduledRead !== 'string') {
          clearTimeout(scheduledRead);
        }
        retainedHistoryReads.delete(store);
      }
      unbindConversation(conversationId);
      renderObservations.clearRenderedConversation(conversationId);
      try {
        // Preserve the final health notification before host cleanup withdraws
        // any rendering demand it schedules. Reads retain the last Turn state.
        coordinators.get(conversationId)?.close();
        // The host still needs the last Turn state to clear per-Turn UI. It
        // cannot keep subscriptions or skip resource release by throwing.
        options.disposeRenderedState?.(conversationId);
      } finally {
        try {
          renderObservations.disposeConversation(conversationId);
        } finally {
          try {
            if (store) attemptLifecycles.get(store)?.dispose();
          } finally {
            if (store) attemptLifecycles.delete(store);
            coordinators.delete(conversationId);
            runtimeStores.delete(conversationId);
            disposingConversations.delete(conversationId);
          }
        }
      }
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
