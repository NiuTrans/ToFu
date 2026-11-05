/**
 * Attempt callback leases and live-to-settled observations for one TurnStore.
 * Entry: createTurnAttemptLifecycle. Canonical rows own retention; the supplied
 * conversation coordinator remains the only transport. No transcript is copied.
 */
import { entityRecordValues } from './entity-record';
import type { ConversationSyncCoordinator, ConversationSyncConnection } from './conversation-sync';
import type { TurnConnectionHooks } from './turn-runtime-contracts';
import type { TurnEvent, TurnState, TurnStore } from './turn-state';

interface AttemptConnection {
  turnId: string | null;
  hooks: TurnConnectionHooks | undefined;
  connection: ConversationSyncConnection;
}

interface TurnAttemptLifecycleOptions {
  coordinator: Pick<ConversationSyncCoordinator, 'resume' | 'cursor'>;
  terminalStatuses: ReadonlySet<string>;
  onProtocolError?(error: Error): void;
  onTerminal(): void;
}

export function createTurnAttemptLifecycle(store: TurnStore, options: TurnAttemptLifecycleOptions) {
  const callbacks = new Map<string, AttemptConnection>();
  const automaticConnections = new Map<string, AttemptConnection>();
  const observedLiveAttempts = new Map<string, string>();
  let disposed = false;

  const findTurnId = (state: TurnState, attemptId: string): string | null =>
    state.attemptsById[attemptId]?.turnId
      ?? entityRecordValues(state.turnsById).find(turn => turn?.currentAttemptId === attemptId)?.turnId
      ?? null;

  const reconcile = (state: TurnState): void => {
    if (disposed || state !== store.getState()) return;
    const retireMissing = (attemptId: string, entry: AttemptConnection): void => {
      entry.turnId ??= findTurnId(state, attemptId);
      // A caller can subscribe before its ACK materializes an attempt. Once
      // canonical identity is known, its removal withdraws that subscription.
      if (entry.turnId && !state.attemptsById[attemptId]
          && state.turnsById[entry.turnId]?.currentAttemptId !== attemptId) {
        entry.connection.close();
      }
    };
    for (const [id, entry] of callbacks) retireMissing(id, entry);
    for (const [id, entry] of automaticConnections) retireMissing(id, entry);
    for (const [attemptId, turnId] of observedLiveAttempts) {
      if (state.turnsById[turnId]?.currentAttemptId !== attemptId) observedLiveAttempts.delete(attemptId);
    }
  };
  const unsubscribe = store.subscribe(reconcile);

  const register = (attemptId: string, hooks: TurnConnectionHooks): AttemptConnection => {
    if (disposed) throw new Error('Conversation attempt owner was disposed.');
    let closed = false;
    const connection: ConversationSyncConnection = {
      close() {
        if (closed) return;
        closed = true;
        if (callbacks.get(attemptId) === entry) callbacks.delete(attemptId);
        if (automaticConnections.get(attemptId) === entry) automaticConnections.delete(attemptId);
        entry.hooks = undefined;
      },
      get cursor() { return options.coordinator.cursor; },
    };
    const entry: AttemptConnection = { turnId: findTurnId(store.getState(), attemptId), hooks, connection };
    if (attemptId) callbacks.set(attemptId, entry);
    return entry;
  };
  const resume = (): void => { void options.coordinator.resume().catch(options.onProtocolError); };

  return {
    connect(attemptId: string, hooks: TurnConnectionHooks = {}): ConversationSyncConnection {
      const entry = register(attemptId, hooks);
      resume();
      return entry.connection;
    },
    connectOnce(attemptId: string): ConversationSyncConnection {
      const previous = automaticConnections.get(attemptId);
      if (previous) return previous.connection;
      const entry = register(attemptId, { onTerminal: options.onTerminal });
      // Claim before resume: health callbacks can synchronously re-enter or
      // dispose the owner, so publication must not follow those callbacks.
      automaticConnections.set(attemptId, entry);
      resume();
      return entry.connection;
    },
    deliverTerminal(event: TurnEvent): void {
      const entry = callbacks.get(event.attemptId);
      const hooks = entry?.hooks;
      // Consume before invoking required user code. Throwing or registering a
      // replacement must not retain this callback or erase the replacement.
      entry?.connection.close();
      automaticConnections.get(event.attemptId)?.connection.close();
      hooks?.onTerminal?.(event);
    },
    observe(state: TurnState, settled: (turn: NonNullable<TurnState['turnsById'][string]>) => void): void {
      if (disposed || state !== store.getState()) return;
      for (const turn of entityRecordValues(state.turnsById)) {
        if (!turn?.currentAttemptId) continue;
        if (turn.status === 'pending' || turn.status === 'running') {
          observedLiveAttempts.set(turn.currentAttemptId, turn.turnId);
        } else if (options.terminalStatuses.has(turn.status || '')
            && observedLiveAttempts.delete(turn.currentAttemptId)) {
          settled(turn);
          if (disposed || state !== store.getState()) return;
        }
      }
    },
    dispose(): void {
      if (disposed) return;
      disposed = true;
      unsubscribe();
      for (const entry of callbacks.values()) entry.connection.close();
      for (const entry of automaticConnections.values()) entry.connection.close();
      callbacks.clear();
      automaticConnections.clear();
      observedLiveAttempts.clear();
    },
  };
}

export type TurnAttemptLifecycle = ReturnType<typeof createTurnAttemptLifecycle>;
