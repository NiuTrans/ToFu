/** Conversation hydration and catalog-shell handoff.
 * Entry: createTurnHydration. Coalesces explicit snapshot requests and keeps
 * their latest view claimant; stores, transport/recovery and attempt leases
 * remain owned by the runtime composition and its existing sync coordinator.
 */
import type { ConversationSyncSnapshot } from '../api/conversation-sync.generated';
import type { ConversationSyncCoordinator } from './conversation-sync';
import { entityRecordValues } from './entity-record';
import type { RuntimeConversation, TurnRuntimeOptions, UnknownRecord } from './turn-runtime-contracts';
import type { TurnStore } from './turn-state';

interface TurnHydrationPorts {
  ensureRuntimeStore(conversationId: string): TurnStore;
  readRuntimeStore(conversationId: string): TurnStore | undefined;
  coordinatorFor(conversationId: string): Pick<ConversationSyncCoordinator, 'hydrate'> | undefined;
  isDisposing(conversationId: string): boolean;
  boundConversation(conversationId: string): RuntimeConversation | undefined;
  bindConversation(conversation: RuntimeConversation, store: TurnStore): void;
  connectAttempt(conversation: RuntimeConversation, store: TurnStore, attemptId: string): unknown;
}

function record(value: unknown): UnknownRecord {
  return value && typeof value === 'object' ? value as UnknownRecord : {};
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export function createTurnHydration(
  options: Pick<TurnRuntimeOptions, 'applySettings' | 'applySnapshotMetadata'>,
  ports: TurnHydrationPorts,
) {
  type HydrateLane = { conversation: RuntimeConversation; promise: Promise<TurnStore> };
  const hydrateLanes = new Map<string, HydrateLane>();
  const isCurrentConversation = (conversation: RuntimeConversation): boolean =>
    !hydrateLanes.has(conversation.id)
      || hydrateLanes.get(conversation.id)?.conversation === conversation;

  const applyBoundSnapshot = (store: TurnStore, snapshot: ConversationSyncSnapshot): void => {
    const conversationId = store.getState().conversationId;
    const conversation = ports.boundConversation(conversationId);
    const current = () => ports.readRuntimeStore(conversationId) === store
      && ports.boundConversation(conversationId) === conversation
      && conversation && isCurrentConversation(conversation);
    if (conversation && current()) options.applySettings?.(
      conversation, record(snapshot.settings), 'full',
    );
    if (conversation && current()) options.applySnapshotMetadata?.(conversation, snapshot);
  };

  const runHydrate = async (
    lane: HydrateLane,
  ): Promise<TurnStore> => {
    const conversationId = lane.conversation?.id;
    if (!conversationId) throw new Error('Conversation is required.');
    const store = ports.ensureRuntimeStore(conversationId);
    const coordinator = ports.coordinatorFor(conversationId);
    if (!coordinator) throw new Error('Conversation coordinator is not initialized.');
    const requireHydrationOwner = (): void => {
      if (ports.readRuntimeStore(conversationId) !== store
          || ports.isDisposing(conversationId)) {
        throw new Error('Conversation was disposed during hydration.');
      }
    };
    const snapshot = await coordinator.hydrate(false);
    requireHydrationOwner();
    // Host callbacks may request a newer catalog shell without another read.
    // Complete settings, metadata and binding only for the latest claimant.
    for (;;) {
      const current = lane.conversation;
      // Authority answers snapshots only for conversations it holds. A first
      // send whose ACK was lost (Stop while starting, a timeout) committed its
      // Turn without command adoption; left local-only, binding would pause the
      // stream and the live Turn would never receive its terminal settlement.
      current._localOnly = false;
      options.applySettings?.(current, record(snapshot.settings), 'full');
      requireHydrationOwner();
      if (lane.conversation !== current) continue;
      options.applySnapshotMetadata?.(current, snapshot);
      requireHydrationOwner();
      if (lane.conversation !== current) continue;
      ports.bindConversation(current, store);
      requireHydrationOwner();
      if (lane.conversation !== current) continue;
      for (const turn of entityRecordValues(store.getState().turnsById)) {
        if (lane.conversation !== current) break;
        const status = stringValue(turn?.status);
        const attemptId = stringValue(turn?.currentAttemptId);
        if ((status === 'pending' || status === 'running') && attemptId) {
          ports.connectAttempt(current, store, attemptId);
          requireHydrationOwner();
        }
      }
      if (lane.conversation === current) return store;
    }
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
  const hydrateConversation = (
    conversation: RuntimeConversation,
  ): Promise<TurnStore> => {
    if (!conversation?.id) return Promise.reject(
      new Error('Conversation is required.'),
    );
    const id = conversation.id;
    if (ports.isDisposing(id)) return Promise.reject(
      new Error('Conversation was disposed during hydration.'),
    );
    const existing = hydrateLanes.get(id);
    if (existing) {
      existing.conversation = conversation;
      return existing.promise;
    }
    // Promise.then defers runHydrate until after hydrateLanes.set below. This
    // ordering is the re-entrancy boundary; do not inline runHydrate here.
    const lane: HydrateLane = { conversation, promise: Promise.resolve()
      .then(() => {
        if (hydrateLanes.get(id) !== lane) {
          throw new Error('Conversation was disposed during hydration.');
        }
        return runHydrate(lane);
      })
      .finally(() => {
        if (hydrateLanes.get(id) === lane) hydrateLanes.delete(id);
      }) };
    hydrateLanes.set(id, lane);
    return lane.promise;
  };

  return {
    hydrateConversation,
    applyBoundSnapshot,
    isCurrentConversation,
    requestedConversation(conversationId: string): RuntimeConversation | undefined {
      return hydrateLanes.get(conversationId)?.conversation;
    },
    claimBoundConversation(conversation: RuntimeConversation): void {
      const lane = hydrateLanes.get(conversation.id);
      if (lane) lane.conversation = conversation;
    },
    disposeConversation(conversationId: string): void {
      hydrateLanes.delete(conversationId);
    },
  };
}
