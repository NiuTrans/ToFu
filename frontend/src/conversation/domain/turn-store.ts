/**
 * Conversation domain facade.
 *
 * This is the target import boundary for normalized TurnStore state.  The
 * implementation remains in core/turn-state during the strangler migration;
 * consumers under conversation/ must never reach into retained runtime code.
 */
export {
  createTurnState,
  createTurnStore,
  reduceTurnState,
} from '../../core/turn-state';

export type {
  AttemptRecord,
  ConversationQueueItem,
  ReduceTurnStateOptions,
  TurnAction,
  TurnCommandResult,
  TurnEvent,
  TurnSnapshotInput,
  TurnState,
  TurnStore,
  TurnStoreOptions,
} from '../../core/turn-state';
