/** Typed composition ports for Turn state, rendering and command execution.
 * No runtime side effects. Existing callers import public types from turn-runtime.
 */
import type {
  AppendSettledTurnRequest, ConnectionHealth, ConversationSyncSnapshot,
  ConversationTurnPage, ConversationRoundDirectory, ConversationRoundEntry,
} from '../api/conversation-sync.generated';
import type { ProjectedConversation } from './turn-projection';
import type { presentTurnFinish, resumeTurnOptions } from './turn-presentation';
import type { TurnRenderer } from './turn-render';
import type { ConversationSyncConnection } from './conversation-sync';
import type { ConversationEventSourceFactory } from './conversation-event-multiplexer';
import type { TurnRenderObservationOptions } from './turn-render-observations';
import type {
  createTurnState, createTurnStore, reduceTurnState,
  TurnEvent, TurnState, TurnStateUpdate, TurnStore,
} from './turn-state';

export type UnknownRecord = Record<string, unknown>;

export interface RuntimeConversation extends ProjectedConversation {
  id: string;
  title?: string;
  createdAt?: number;
  _localOnly?: boolean;
  _conversationSyncHealth?: ConnectionHealth;
}


export interface TurnRuntimeOptions extends TurnRenderObservationOptions {

  snapshotViewport?(): { width: number; height: number };
  findConversation?(conversationId: string): RuntimeConversation | null;
  persist?(conversation: RuntimeConversation): void;
  isActive?(conversation: RuntimeConversation): boolean;
  /** Observe canonical state independently of DOM projection or windowing. */
  onConversationInvalidated?(conversationId: string): void;
  onStateApplied?(conversation: RuntimeConversation, state: TurnState, update?: TurnStateUpdate): void;
  isDomStale?(conversation: RuntimeConversation): boolean;
  replaceAll?(
    conversation: RuntimeConversation,
    repaint?: { force?: boolean },
  ): void;
  /** Preferred production DOM port; false explicitly requests legacy fallback. */
  renderState?(
    conversation: RuntimeConversation,
    state: TurnState,
    render?: { force?: boolean; update?: TurnStateUpdate },
  ): boolean | void;
  disposeRenderedState?(conversationId: string): void;

  deferTerminalRelease?(release: () => void): void;
  buildNavigation?(conversation: RuntimeConversation): void;
  renderConversationList?(): void;
  updateSendButton?(): void;
  onProtocolError?(error: Error): void;
  onResyncError?(error: unknown, turnId: string): void;
  /** Presentation demand; raw diagnostics remain in conversationConnectionHealth.
   * Repeated healthy live readings update the shell without redundant paints.
   * All trouble observations still reach the existing notice/debounce owner. */
  onHealth?(conversationId: string, health: ConnectionHealth): void;
  /** Diagnostic evidence frames (messages_snapshot / tool_compacted) from
   * durable attempt events — live and replay. Feeds the debug panel. */
  onDebugFrame?(conversationId: string, frame: UnknownRecord): void;
  onTurnSettled?(conversation: RuntimeConversation, turn: UnknownRecord): void;
  /** 'full' snapshot settings may clear absent keys; 'partial' never does. */
  applySettings?(
    conversation: RuntimeConversation,
    settings: UnknownRecord,
    projection: 'full' | 'partial',
  ): void;
  applySnapshotMetadata?(
    conversation: RuntimeConversation,
    snapshot: ConversationSyncSnapshot,
  ): void;
  /** Per-conversation stream source; defaults to one multiplexed page response. */
  eventSourceFactory?: ConversationEventSourceFactory;
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
  hydrateConversation(conversation: RuntimeConversation): Promise<TurnStore>;
  wakeConversation(conversation: RuntimeConversation): Promise<TurnStore>;
  roundDirectoryKey(conversationId: string): string;
  loadConversationRoundDirectory(conversation: RuntimeConversation, laneId: string,
    offset?: number, limit?: number, anchorTurnId?: string): Promise<ConversationRoundDirectory>;
  /** Page one main-lane Turn in by its ordinal, joining the snapshot first. */
  hydrateConversationRound(conversation: RuntimeConversation, laneId: string,
    entry: Pick<ConversationRoundEntry, 'turnId' | 'ordinal'>): Promise<void>;
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

  /** Called only AFTER DOM commit; accepted/scheduled renders are not evidence. */
  notifyRenderedState(
    conversationId: string, state: TurnState, mountedTurnIds: readonly string[],
  ): void;
  hasAuthoritativeCommand(conversationId: string, commandId: string): boolean;
  /** Re-evaluate only the shells affected by an external active-id change. */
  reconcileConversationActivity(
    ...conversationIds: Array<string | null | undefined>
  ): void;
  ensureRuntimeStore(conversationId: string): TurnStore;
  /** Suspend display streams without cancelling backend work or losing replay cursors. */
  setTransportSuspended(suspended: boolean): void;
  invalidateConversation(conversationId: string, cursorHint?: string): void;
  disposeConversation(conversationId: string): void;
  hasLiveConnection(conversationId: string): boolean;
  findConversation(conversationId: string): RuntimeConversation | null;
  readonly TERMINAL: ReadonlySet<string>;
}

export interface TurnConnectionHooks {
  onTerminal?(event: TurnEvent): void;
}

