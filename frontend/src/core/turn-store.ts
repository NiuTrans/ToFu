// Typed facade for the startup-critical classic state machine. It remains in
// the core bundle until send/SSE boot no longer reads it synchronously.

import type { AttemptStreamConnection } from './attempt-stream';
import type { ProjectionTurn } from './turn-projection';
import type {
  ReduceTurnStateOptions,
  TurnAction,
  TurnState,
  TurnStore,
  TurnStoreOptions,
} from './turn-state';
import type {
  ResumeOption,
  TurnFinishPresentation,
} from './turn-presentation';
import type { TurnRenderer } from './turn-render';
import { getRuntimeService } from '../runtime/app-runtime.js';

export interface TurnStoreV2Api {
  emptyState(conversationId: string): TurnState;
  reducer(
    state: TurnState,
    action: TurnAction,
    options?: ReduceTurnStateOptions,
  ): TurnState;
  createStore(conversationId: string, options?: TurnStoreOptions): TurnStore;
  submit(store: TurnStore, inputTurn: unknown, config: unknown, extra?: unknown,
    requestOptions?: unknown): Promise<unknown>;
  runOperation(store: TurnStore, turnId: string, operation: string,
    config?: unknown, options?: unknown): Promise<unknown>;
  connect(store: TurnStore, attemptId: string, after?: number,
    hooks?: unknown): AttemptStreamConnection;
  renderInto(container: Element, state: TurnState, renderTurn?: TurnRenderer): void;
  finishPresentation(turn: ProjectionTurn): TurnFinishPresentation | null;
  resumeOptions(turn: ProjectionTurn): ResumeOption[];
  hydrateConversation(conversation: unknown): Promise<unknown>;
  submitConversation(conversation: unknown, message: unknown, config: unknown,
    extra?: unknown): Promise<unknown>;
  submitBranch(conversation: unknown, branch: unknown, parentTurnId: string,
    message: unknown, config: unknown, extra?: unknown): Promise<unknown>;
  operateConversation(conversation: unknown, turnId: string, operation: string,
    config?: unknown, options?: unknown): Promise<unknown>;
  updateConversationTurn(conversation: unknown, turnId: string,
    projection: unknown): Promise<unknown>;
  createBranchLane(conversation: unknown, parentTurnId: string,
    descriptor?: unknown): Promise<unknown>;
  deleteBranchLane(conversation: unknown, parentTurnId: string,
    laneId: string): Promise<unknown>;
  deleteConversationTurns(conversation: unknown,
    turnIds: readonly string[]): Promise<unknown>;
  markCommandPending(conversation: unknown, turnId: string,
    operation: string): void;
  markCommandFailed(conversation: unknown, turnId: string): void;
  abortConversation(conversation: unknown): Promise<unknown>;
  abortAttempt(attemptId: string): Promise<unknown>;
  ensureRuntimeStore(conversationId: string): TurnStore;
  findConversation(conversationId: string): unknown;
  readonly TERMINAL: ReadonlySet<string>;
  isCutoverActive(): boolean;
}

export function getTurnStoreV2(): TurnStoreV2Api {
  const service = getRuntimeService('TurnStoreV2') as TurnStoreV2Api | undefined;
  if (!service) throw new Error('TurnStoreV2 failed to initialize');
  return service;
}
